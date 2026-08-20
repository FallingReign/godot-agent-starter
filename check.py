#!/usr/bin/env python3
"""Verification gate for the Godot 4.7.1 co-op project.

Cross-platform: Windows, macOS and Linux, on any Python 3.8+.
Python is used rather than a shell script because gdtoolkit already requires
it, so this adds no new dependency, and because Godot's own failure modes need
real logic rather than shell plumbing.

Godot's exit codes are not reliable:
  - `--headless --import --quit` can exit 1 on a completely clean import
  - other commands can exit 0 while printing hard errors
So every stage scans output for error markers, and every invocation has a
hard timeout because Godot can hang on import.

Usage:
  python check.py                     run every stage
  python check.py --only typecheck    run one stage (repeatable, or comma-separated)
  python check.py --fast              skip import (use when only .gd files changed)
  python check.py --list              show stage names and exit

Stages: format lint import typecheck grep gut smoke
Exit codes: 0 gate passed, 1 gate failed, 2 could not run (missing Godot)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import datetime as _dt
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# ROOT is the repository: gate scripts, docs, rule files, skills.
# PROJECT_DIR is the Godot project: everything the engine loads. Keeping them
# separate means an agent working on game code never has the gate's own files
# in scope, and `godot --path src` cannot import a tool script by accident.
ROOT = Path(__file__).resolve().parent
PROJECT_DIR = ROOT / "src"
LOG_DIR = ROOT / ".checklogs"
EXPECTED_VERSION = "4.7.1"
STAGES = ["integrity", "skills", "schema", "shape", "design", "conformance", "format", "lint",
          "sanitise", "import", "typecheck", "grep", "types", "arch", "tests",
          "assets", "resources", "gut", "smoke"]
GATE_RULES_FILE = ROOT / "gate.rules.json"
MANIFEST_FILE = ROOT / ".gate.sha256"

# Files whose integrity is asserted before any other stage runs. A deny list is
# something an agent can be configured around; a hash is a tripwire that shows
# up in the diff.
#
# RULE: never pin a file whose canonical form is defined by a tool this script
# cannot run. tools/validate_resources.gd was pinned in an earlier revision and
# also had to satisfy gdformat, which produced an unpassable gate: the format
# stage demanded a change the integrity stage forbade. Only files this script
# alone owns belong here.
# project.godot was pinned in an earlier revision. Godot itself rewrites that
# file whenever files are added or settings change, so integrity failed with no
# legitimate route to green: the agent could not proceed and could not
# re-baseline. Same rule as above, applied to the engine as the external tool.
# It is instead reported as advisory drift below, and stays on the deny list.
GATE_FILES = ["check.py", "arch.py", "sanitise.py", "gate.rules.json"]

# Files whose changes are reported but never fail the gate. Two reasons a file
# lands here rather than in GATE_FILES: an external tool owns its canonical form
# (Godot rewrites project.godot whenever files are added), or a legitimate change
# to it is part of normal work. arch.rules.json declares intended module
# boundaries, so adding a module is a real architecture change with a real need
# to edit the file -- pinning it made that change impossible to make honestly.
# A diff still surfaces it for review, which is the point.
ADVISORY_FILES = ["project.godot", "arch.rules.json"]

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# Asset source extensions that must carry a committed .import sidecar.
ASSET_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".svg", ".bmp", ".tga", ".exr",
              ".hdr", ".ktx", ".dds", ".ogg", ".wav", ".mp3", ".glb", ".gltf",
              ".fbx", ".obj", ".dae", ".ttf", ".otf", ".woff", ".woff2"}

# Directories never scanned or type-checked.
EXCLUDED_DIRS = {".godot", "addons", "build", "export", ".git", ".checklogs"}

# Error markers. Deliberately broad: a false alarm costs a re-read, a missed
# error costs a wrong "done" claim from the agent.
ERROR_RE = re.compile(
    r"SCRIPT ERROR|Parse Error|ERROR:|Failed to load|Cannot open file"
    r"|CRITICAL|Condition \".*\" is true|USER ERROR"
)
# Lines that match the above but are harmless noise on a clean run.
IGNORE_RE = re.compile(
    r"ERROR: Cannot open file .*\.uid"
    r"|editor/editor_node"
    r"|No loader found"
    r"|Unable to open file: res://\.godot"
)
# Continuation lines Godot emits directly after an error, e.g.
#   "          at: GDScript::reload (res://scripts/logic/foo.gd:42)"
CONT_RE = re.compile(r"^\s+at:\s")
LOC_RE = re.compile(r"\((res://[^:)]+):(\d+)\)")
# Signature normalisation for grouping: strip quoted content and digits.
SIG_STRIP = re.compile(r'"[^"]*"')
NUM_STRIP = re.compile(r"\b\d+\b")

# Banned idioms live in gate.rules.json so the kit can be tailored per game
# without editing this script. Several of these parse cleanly in Godot 4 and
# fail silently at runtime, which is why a grep stage exists at all.
# Add `# gate:allow` to the end of a line to suppress a false positive.
DEFAULT_BANNED: List[Tuple[str, str]] = [
    (r'(^|[^A-Za-z0-9_])connect\(\s*"[^"]*"\s*,',
     'four-argument connect() -- parses, never fires. Use signal.connect(callable)'),
]


def load_banned() -> List[Tuple[re.Pattern, str, str]]:
    """Load banned patterns, falling back to a minimal built-in set."""
    if not GATE_RULES_FILE.is_file():
        return [(re.compile(p), m, "builtin") for p, m in DEFAULT_BANNED]
    try:
        data = json.loads(GATE_RULES_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"could not read {GATE_RULES_FILE.name}: {exc}")
        return [(re.compile(p), m, "builtin") for p, m in DEFAULT_BANNED]
    out: List[Tuple[re.Pattern, str, str]] = []
    for rule in data.get("rules", []):
        try:
            out.append((re.compile(rule["pattern"]), rule["message"],
                        rule.get("id", "?")))
        except (re.error, KeyError) as exc:
            print(f"skipping malformed rule {rule.get('id', '?')}: {exc}")
    return out


SUPPRESS = re.compile(r"#\s*gate:allow")


# --------------------------------------------------------------------- output

def _supports_colour() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if not sys.stdout.isatty():
        return False
    if os.name == "nt":
        # Enable ANSI on Windows 10+ consoles; fall back to plain text.
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            return False
    return True


if _supports_colour():
    RED, GRN, YEL, DIM, RST = (
        "\033[31m", "\033[32m", "\033[33m", "\033[2m", "\033[0m",
    )
else:
    RED = GRN = YEL = DIM = RST = ""


class Results:
    def __init__(self) -> None:
        self.lines: List[str] = []
        self.failed = False

    def passed(self, name: str) -> None:
        self.lines.append(f"{GRN}PASS{RST}  {name}")

    def fail(self, name: str) -> None:
        self.lines.append(f"{RED}FAIL{RST}  {name}")
        self.failed = True

    def skip(self, name: str) -> None:
        self.lines.append(f"{YEL}SKIP{RST}  {name}")


RESULTS = Results()


def head(title: str) -> None:
    print(f"\n{DIM}== {title} {RST}", flush=True)


SKILL_DIR = ".agents/skills"


def skill(name: str, why: str) -> None:
    """Point at the on-demand skill for this failure.

    Advertisement alone does not get a skill loaded -- three sessions of
    evidence showed reference docs going unread while the agent guessed.
    A pointer printed at the moment of failure lands where attention
    already is.
    """
    print(f"  skill: {SKILL_DIR}/{name}/SKILL.md  ({why})")


def indent(text: str, limit: int = 40) -> None:
    lines = [ln for ln in text.splitlines() if ln.strip()][:limit]
    for ln in lines:
        print(f"  {ln}", flush=True)


# ----------------------------------------------------------------- processes

# Hard ceiling on any single log file. Godot's interactive debugger (-d) can
# emit millions of identical break messages when a parse error is present; an
# uncapped log reached 165 MB in the field. The cap keeps the tail, because the
# tail is where a timeout notice or a summary line lives.
LOG_CAP_BYTES = 2 * 1024 * 1024


def _cap(text: str) -> str:
    if len(text) <= LOG_CAP_BYTES:
        return text
    keep = text[-LOG_CAP_BYTES:]
    dropped = len(text) - LOG_CAP_BYTES
    return (f"[log truncated: {dropped} bytes dropped from the head, "
            f"showing the last {LOG_CAP_BYTES} bytes]\n" + keep)


def run(cmd: Sequence[str], timeout: int, log: Path) -> Tuple[int, str]:
    """Run a command, capture combined output to `log`, never raise.

    stdin is closed. Godot's debugger prompts for input on a parse error; with
    an inherited stdin it reads garbage, resumes, breaks again, and spins until
    the timeout. DEVNULL turns that infinite loop into a clean immediate exit.
    """
    try:
        proc = subprocess.run(
            list(cmd),
            cwd=str(PROJECT_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or b""
        if isinstance(partial, str):
            partial = partial.encode()
        out = partial.decode("utf-8", errors="replace")
        out += f"\nTIMEOUT after {timeout}s\n"
        code = 124
    except FileNotFoundError:
        out = f"executable not found: {cmd[0]}\n"
        code = 127
    # newline="" prevents Windows translating Godot's \r\n into \r\r\n.
    # ANSI escapes are stripped: the log is read back by an agent, not a TTY.
    clean = _cap(ANSI_RE.sub("", out))
    with log.open("w", encoding="utf-8", errors="replace", newline="") as fh:
        fh.write(clean)
    return code, out


def group_errors(out: str) -> List[dict]:
    """Collapse engine output into one entry per distinct error message.

    Godot emits each parse error as TWO lines: the message, then an indented
    `at:` line carrying file and line number. An earlier revision matched only
    the message line, so every location was silently discarded and the agent
    never saw a line number for any error. Continuation lines are now attached
    to their parent.

    Identical messages are grouped by signature (quoted content and digits
    stripped) so a run with 57 errors of 4 kinds prints 4 entries with all
    locations, not 57 near-identical lines. No catalogue, no per-error rules.
    """
    if "TIMEOUT after" in out:
        return [{"msg": "timed out", "locs": [], "count": 1}]
    lines = out.splitlines()
    groups: dict = {}
    order: List[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if not (ERROR_RE.search(ln) and not IGNORE_RE.search(ln)):
            i += 1
            continue
        locs: List[str] = []
        j = i + 1
        while j < len(lines) and CONT_RE.match(lines[j]):
            hit = LOC_RE.search(lines[j])
            if hit:  # project files only; engine-internal .cpp paths are noise
                locs.append(f"{hit.group(1)}:{hit.group(2)}")
            j += 1
        sig = NUM_STRIP.sub("N", SIG_STRIP.sub('"_"', ln)).strip()
        if sig not in groups:
            groups[sig] = {"msg": ln, "locs": [], "count": 0}
            order.append(sig)
        groups[sig]["count"] += 1
        for loc in locs:  # same error at the same line twice is noise
            if loc not in groups[sig]["locs"]:
                groups[sig]["locs"].append(loc)
        i = j
    return [groups[k] for k in order]


def render_errors(groups: List[dict], max_groups: int = 12,
                  max_locs: int = 8) -> List[str]:
    """Format grouped errors for the console, newest information first."""
    out: List[str] = []
    for g in groups[:max_groups]:
        tag = f"  [x{g['count']}]" if g["count"] > 1 else ""
        out.append(g["msg"] + tag)
        shown = g["locs"][:max_locs]
        out.extend(f"  {loc}" for loc in shown)
        rest = len(g["locs"]) - len(shown)
        if rest > 0:
            out.append(f"  ... +{rest} more location(s)")
    rest_g = len(groups) - max_groups
    if rest_g > 0:
        out.append(f"... +{rest_g} more distinct error(s)")
    return out


def scan(out: str) -> List[str]:
    """Flat error list for stages that only need a pass/fail signal."""
    return render_errors(group_errors(out))


def find_godot() -> Optional[str]:
    env = os.environ.get("GODOT_BIN")
    if env:
        if Path(env).is_file():
            return env
        found = shutil.which(env)
        if found:
            return found
        return None
    for name in ("godot", "godot4", "Godot", "godot.exe", "Godot.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def gd_files() -> List[Path]:
    out: List[Path] = []
    for path in sorted(PROJECT_DIR.rglob("*.gd")):
        rel = path.relative_to(PROJECT_DIR)
        if EXCLUDED_DIRS.intersection(rel.parts):
            continue
        out.append(path)
    return out


def gdtool(name: str) -> Optional[List[str]]:
    """Resolve gdformat/gdlint.

    Order of preference:
      1. the binary on PATH, fastest
      2. `uvx --from gdtoolkit==4.*`, which needs nothing installed permanently
         and pins the major version to match Godot 4
      3. `python -m`, the fallback when the package is pip-installed but
         pip's Scripts directory is missing from PATH, which is common on Windows

    Set GDTOOLKIT_OFFLINE=1 to skip the uvx path entirely on a machine with no
    network access, so the stage degrades to SKIP quickly instead of timing out.
    """
    found = shutil.which(name)
    if found:
        return [found]

    if not os.environ.get("GDTOOLKIT_OFFLINE"):
        runner = shutil.which("uvx")
        base = [runner] if runner else None
        if base is None and shutil.which("uv"):
            base = [shutil.which("uv"), "tool", "run"]
        if base:
            cmd = base + ["--from", "gdtoolkit==4.*", name]
            try:
                probe = subprocess.run(
                    cmd + ["--version"], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=300, check=False)
                if probe.returncode == 0:
                    return cmd
            except (subprocess.TimeoutExpired, OSError):
                pass

    module = {"gdformat": "gdtoolkit.formatter", "gdlint": "gdtoolkit.linter"}[name]
    probe = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if probe.returncode == 0:
        return [sys.executable, "-m", module]
    return None


# -------------------------------------------------------------------- stages

def _manifest_hash(rel: str) -> Optional[str]:
    # Gate files live at the repo root; project.godot lives inside the Godot
    # project. Resolve against ROOT first, then PROJECT_DIR, so both are
    # covered after the src/ split.
    path = ROOT / rel
    if not path.is_file():
        path = PROJECT_DIR / rel
    if not path.is_file():
        return None
    raw = path.read_bytes()
    # Normalise line endings so a CRLF checkout on Windows does not report
    # permanent drift against a manifest baselined on LF.
    norm = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(norm).hexdigest()


def write_manifest() -> None:
    lines = []
    # Advisory files are hashed too. Without a baseline there is nothing to
    # compare against, so a change to them would pass unnoticed rather than
    # being surfaced as a note.
    for rel in GATE_FILES + ADVISORY_FILES:
        digest = _manifest_hash(rel)
        if digest is not None:
            lines.append(f"{digest}  {rel}")
    with MANIFEST_FILE.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("# Integrity manifest for the verification gate.\n")
        fh.write("# Regenerate deliberately: python check.py --accept-gate-changes\n")
        fh.write("\n".join(lines) + "\n")


def stage_integrity() -> None:
    head("gate integrity")
    if not MANIFEST_FILE.is_file():
        write_manifest()
        print(f"  no manifest, created {MANIFEST_FILE.name} from current files")
        print("  commit it: this is the baseline the gate is measured against")
        RESULTS.passed("integrity (baselined)")
        return

    expected: Dict[str, str] = {}
    for line in MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            expected[parts[1].strip()] = parts[0]

    drift, missing = [], []
    for rel, want in expected.items():
        got = _manifest_hash(rel)
        if got is None:
            missing.append(rel)
        elif got != want:
            drift.append(rel)

    advisory = [rel for rel in ADVISORY_FILES
                if rel in expected and _manifest_hash(rel) != expected[rel]]
    drift = [rel for rel in drift if rel not in ADVISORY_FILES]
    missing = [rel for rel in missing if rel not in ADVISORY_FILES]

    if not drift and not missing:
        print(f"  {len(expected)} gate file(s) unchanged")
        for rel in advisory:
            print(f"  {DIM}note: {rel} changed (advisory, review the diff){RST}")
        RESULTS.passed("integrity")
        return

    for rel in drift:
        print(f"  {RED}MODIFIED  {rel}{RST}")
    for rel in missing:
        print(f"  {RED}MISSING   {rel}{RST}")
    print()
    print("  The gate has been modified. This is not automatically wrong, but it")
    print("  must be a deliberate human decision, never an agent's route to green.")
    print("  Review with:  git diff -- " + " ".join(sorted(drift + missing)))
    print("  Then either revert, or accept:  python check.py --accept-gate-changes")
    RESULTS.fail("integrity (gate modified)")


def stage_skills() -> None:
    """Validate on-demand skill files.

    A skill whose frontmatter `name` does not match its folder is silently
    ignored by the agent runtimes that read this directory -- no error, the
    knowledge simply never loads. That failure is invisible, so it is checked.
    """
    head("skills (on-demand knowledge)")
    base = ROOT / SKILL_DIR
    if not base.is_dir():
        print(f"  no {SKILL_DIR}/ directory, stage disabled")
        RESULTS.skip("skills")
        return
    files = sorted(base.glob("*/SKILL.md"))
    if not files:
        print(f"  no SKILL.md files under {SKILL_DIR}/")
        RESULTS.skip("skills")
        return
    problems: List[str] = []
    for f in files:
        folder = f.parent.name
        text = f.read_text(encoding="utf-8", errors="replace")
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
        if m is None:
            problems.append(f"{folder}: no YAML frontmatter")
            continue
        fm = m.group(1)
        nm = re.search(r"^name:\s*(.+)$", fm, re.M)
        ds = re.search(r"^description:\s*(.+)$", fm, re.M)
        if nm is None:
            problems.append(f"{folder}: frontmatter has no 'name'")
        elif nm.group(1).strip() != folder:
            problems.append(f"{folder}: name '{nm.group(1).strip()}' != folder name "
                            f"(skill will not load)")
        elif not re.fullmatch(r"[a-z0-9-]+", nm.group(1).strip()):
            problems.append(f"{folder}: name must be lowercase-hyphenated")
        if ds is None:
            problems.append(f"{folder}: frontmatter has no 'description' "
                            f"(nothing for an agent to match against)")
        elif len(ds.group(1).strip()) > 1024:
            problems.append(f"{folder}: description over 1024 chars")
    print(f"  checked {len(files)} skill(s)")
    if problems:
        for prob in problems[:20]:
            print(f"    {RED}{prob}{RST}")
        RESULTS.fail("skills")
    else:
        RESULTS.passed("skills")


def stage_sanitise() -> None:
    head("sanitise (scene/resource hygiene)")
    script = ROOT / "sanitise.py"
    if not script.is_file():
        print("  sanitise.py not present, stage disabled")
        RESULTS.skip("sanitise")
        return
    code, out = run([sys.executable, str(script)], 60, LOG_DIR / "sanitise.log")
    indent(out, 40)
    if code == 0:
        RESULTS.passed("sanitise")
    else:
        print("  apply the automatic fixes with: python sanitise.py --write")
        print("  structural errors are NOT auto-fixable and need a real correction")
        skill("godot-scene-files", "what is safe to author and what is engine-owned")
        RESULTS.fail("sanitise")


def stage_tests() -> None:
    """Test files the runner would silently ignore, and untested modules.

    GUT collects only files matching its configured dirs/prefix/suffix. A file
    named `deglyph_test.gd` is not collected and produces NO error - the suite
    looks healthy while testing nothing. That is a false green, so it blocks.

    The pairing check (does each interior/boundary module have a test file) is
    advisory: whether a trivial data holder needs its own test is judgement.
    """
    head("test discovery (files the runner would ignore)")
    cfg_path = PROJECT_DIR / ".gutconfig.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  cannot read .gutconfig.json: {exc}")
        RESULTS.fail("tests")
        return
    dirs = [str(d).replace("res://", "") for d in cfg.get("dirs", [])]
    prefix = str(cfg.get("prefix", "test_"))
    suffix = str(cfg.get("suffix", ".gd"))
    subdirs = bool(cfg.get("include_subdirs", True))

    def collected(rel: str) -> bool:
        for d in dirs:
            d = d.rstrip("/") + "/"
            if not rel.startswith(d):
                continue
            tail = rel[len(d):]
            if not subdirs and "/" in tail:
                continue
            base = tail.rsplit("/", 1)[-1]
            if base.startswith(prefix) and base.endswith(suffix):
                return True
        return False

    # Scripts attached to a scene are run by that scene (e.g. the smoke
    # harness), not by GUT. Not collecting them is correct, not a false green.
    scene_scripts: set[str] = set()
    for tscn in PROJECT_DIR.rglob("*.tscn"):
        if EXCLUDED_DIRS.intersection(tscn.relative_to(PROJECT_DIR).parts):
            continue
        try:
            body = tscn.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'path="res://([^"]+\.gd)"', body):
            scene_scripts.add(m.group(1))

    orphans: list[str] = []
    collected_count = 0
    for path in gd_files():
        rel = path.relative_to(PROJECT_DIR).as_posix()
        if collected(rel):
            collected_count += 1
            continue
        base = path.name.lower()
        # Word-boundary match: `untested_mod.gd` is not a test file.
        looks_like_test = (bool(re.search(r"(?:^|[_\-])(?:test|tests|spec)"
                                          r"(?:[_\-.]|$)", base))
                           or rel.startswith("tests/"))
        if (looks_like_test and not rel.startswith("tests/harness")
                and rel not in scene_scripts):
            orphans.append(rel)

    print(f"  {collected_count} file(s) collected from "
          f"{', '.join(dirs) or '(no dirs configured)'}")

    interior, boundary = _layer_dirs()
    tested: set[str] = set()
    for path in gd_files():
        rel = path.relative_to(PROJECT_DIR).as_posix()
        if collected(rel):
            tested.add(path.stem.removeprefix(prefix.removesuffix("_") + "_"))
    untested: list[str] = []
    for path in gd_files():
        rel = path.relative_to(PROJECT_DIR).as_posix()
        if not any(rel.startswith(d) for d in interior + boundary):
            continue
        if path.stem not in tested:
            untested.append(rel)
    if untested:
        print(f"  {YEL}note{RST}: module(s) with no matching "
              f"{prefix}<name>{suffix} in {dirs[0] if dirs else 'tests/'}")
        for rel in untested:
            print(f"    {rel}")
        print("  Advisory. A module with no test cannot fail a test.")

    if orphans:
        print(f"  {RED}test-shaped file(s) the runner will NOT collect{RST}")
        for rel in orphans:
            print(f"    {rel}")
        print(f"""
  These are silently ignored: the suite reports success having never run them.
  Rename to {prefix}<name>{suffix} and place under {dirs[0] if dirs else 'the configured dir'}
  (subdirs {'allowed' if subdirs else 'NOT allowed'}), or move genuine helpers
  to tests/harness/. Scripts attached to a scene are exempt automatically.""")
        skill("godot-headless-verification",
              "what a headless run proves and what it does not")
        RESULTS.fail("tests")
    else:
        RESULTS.passed("tests")


def stage_assets() -> None:
    head("assets (sidecar completeness)")
    problems: List[str] = []
    assets = 0
    for path in sorted(PROJECT_DIR.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(PROJECT_DIR)
        if EXCLUDED_DIRS.intersection(rel.parts):
            continue
        if path.suffix.lower() in ASSET_EXTS:
            assets += 1
            if not path.with_name(path.name + ".import").is_file():
                problems.append(f"{rel.as_posix()}: no .import sidecar"
                                f" -- run: python check.py --only import")
    # .uid sidecars for scripts are advisory: whether headless import generates
    # them is engine-version dependent, and a missing one degrades to
    # path-based resolution rather than breaking anything.
    missing_uid = [
        p.relative_to(PROJECT_DIR).as_posix()
        for p in gd_files()
        if not p.with_name(p.name + ".uid").is_file()
    ]
    print(f"  {assets} asset file(s), {len(gd_files())} script(s)")
    if missing_uid:
        print(f"  {YEL}{len(missing_uid)} script(s) without a .uid sidecar"
              f" (advisory){RST}")
        for rel in missing_uid[:5]:
            print(f"    {rel}")
        print("  Godot assigns these when the editor next saves the script.")
    if problems:
        indent("\n".join(problems), 20)
        RESULTS.fail("assets")
    else:
        RESULTS.passed("assets")


def stage_resources(godot: str) -> None:
    head("resources (headless load + instantiate)")
    # GDScript run by the engine must live inside src/, because res:// resolves
    # to the Godot project root, not the repo root.
    script = PROJECT_DIR / "tools" / "validate_resources.gd"
    if not script.is_file():
        print("  src/tools/validate_resources.gd not present, stage disabled")
        RESULTS.skip("resources")
        return
    _, out = run(
        [godot, "--headless", "--path", str(PROJECT_DIR),
         "-s", "res://tools/validate_resources.gd"],
        120, LOG_DIR / "resources.log",
    )
    fails = [ln for ln in out.splitlines() if ln.startswith("RESVAL: FAIL")]
    summary = [ln for ln in out.splitlines() if ln.startswith("RESVAL: SUMMARY")]
    if summary:
        print("  " + summary[-1].replace("RESVAL: ", ""))
    invalid_uid = [ln for ln in out.splitlines() if "invalid UID" in ln]
    if invalid_uid:
        print(f"  {YEL}{len(invalid_uid)} invalid UID warning(s);"
              f" resolved by path, not fatal{RST}")
    if fails:
        indent("\n".join(fails[:15]), 15)
        skill("godot-scene-files", "structural rules for .tscn / .tres")
        RESULTS.fail("resources")
    elif "RESVAL: PASS" in out:
        RESULTS.passed("resources")
    else:
        # No verdict line at all means the validator did not finish.
        indent("\n".join(out.splitlines()[-15:]), 15)
        print(f"  {RED}validator produced no verdict{RST}")
        RESULTS.fail("resources (no verdict)")



def stage_schema() -> None:
    """Validate the JSON artefacts against the declared field shape.

    proposal.json shipped as empty arrays with the item shape documented only in
    prose. An agent filling it invented reasonable key names, and every reader
    silently discarded what it did not recognise: five files each carried a
    written justification and the plan showed none of them, while conformance
    skipped every file because `action` was absent.

    A wrong key must fail here, naming the right one, rather than vanishing at
    render time. Everything downstream -- the plan view, the conformance diff, a
    retrospective -- reasons about these files, so they have to mean what they
    say.
    """
    head("schema (artefact field shapes)")
    tool = ROOT / "tools" / "schema.py"
    if not tool.exists():
        print("  tools/schema.py missing")
        RESULTS.skip("schema")
        return
    code, out = run([sys.executable, str(tool)], 30, LOG_DIR / "schema.log")
    body = (out or "").strip()
    for line in body.splitlines()[:24]:
        line = line.rstrip()
        if line.startswith("error:"):
            print(f"  {RED}{line}{RST}")
        elif line.startswith("note:"):
            print(f"  {DIM}{line}{RST}")
        elif line:
            print(f"  {line}")
    if code != 0:
        print(f"  {DIM}field list: python tools/schema.py --describe proposal"
              f"{RST}")
        RESULTS.fail("schema")
        return
    RESULTS.passed("schema")


def stage_shape() -> None:
    """The decision log must be honest: real decisions, no placeholders.

    This stage never demands a decision. Demanding one is what produces a
    fabricated answer, and a fabricated decision is worse than an absent one
    because every later agent reads it as settled fact. An absent decision is
    a question a skill will ask when the work makes it answerable.

    What it does enforce is that entries are well formed and that nobody wrote
    "unknown" or "tbd" into an answer to make a prompt go away.

    The log may inform a recommendation. It may never relax a check.
    """
    head("decision log")
    path = ROOT / "project.shape.json"
    if not path.is_file():
        print("  no project.shape.json - agents will infer intent instead")
        RESULTS.skip("shape (absent)")
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"  {RED}cannot parse project.shape.json{RST}: {exc}")
        RESULTS.fail("shape")
        return

    # Onboarding (name + pitch) is bootstrap's job to block on. The gate does
    # not double-block it: a fresh clone must be able to reach green before the
    # human has named anything, or the first experience of the kit is a failure
    # it cannot fix by itself.
    pending = []
    if str(data.get("name", "")).strip() in ("", "Untitled"):
        pending.append("name")
    if not str(data.get("pitch", "")).strip():
        pending.append("pitch")
    inv = str(data.get("involvement", "")).strip()
    if not inv:
        pending.append("involvement")

    problems: list[str] = []
    decisions = data.get("decisions")
    if decisions is None:
        decisions = []
    if not isinstance(decisions, list):
        problems.append("decisions must be a list")
        decisions = []

    valid_inv = ("hands-off", "module", "file", "function")
    if inv and inv not in valid_inv:
        problems.append(
            f"involvement is {inv!r}, which is not one of "
            f"{', '.join(valid_inv)}. A typo here silently drops the human to "
            "hands-off, so it is treated as an error rather than a default.")

    placeholders = {"unknown", "unknown yet", "tbd", "todo", "n/a", "na",
                    "none", "?", "-", "not sure", "undecided"}
    required = ("id", "question", "answer", "because", "decided_by", "date")
    seen: dict[str, int] = {}
    for i, d in enumerate(decisions):
        where = f"decisions[{i}]"
        if not isinstance(d, dict):
            problems.append(f"{where} is not an object")
            continue
        for key in required:
            if not str(d.get(key, "")).strip():
                problems.append(f"{where} missing {key}")
        ans = str(d.get("answer", "")).strip().lower().rstrip(".")
        if ans in placeholders:
            problems.append(
                f"{where} answer is a placeholder ({d.get('answer')!r}). "
                "Delete the entry - an undecided question is an absent entry, "
                "not a recorded non-answer")
        who = str(d.get("decided_by", "")).strip().lower()
        if who and who not in ("human", "agent"):
            problems.append(f"{where} decided_by must be 'human' or 'agent'")
        # Without invalidation conditions every decision reads as permanent,
        # including the provisional ones. Six months on nobody can tell which
        # were load-bearing and which were the cheapest thing that unblocked a
        # slice. "permanent" is a valid answer -- refusing to think about it
        # is not.
        rev = str(d.get("revisit_if", "")).strip()
        if not rev:
            problems.append(
                f"{where} missing revisit_if. State what would invalidate this, "
                "or 'permanent' if nothing would. An unqualified decision reads "
                "as settled forever")
        elif rev.lower().rstrip(".") in placeholders:
            problems.append(
                f"{where} revisit_if is a placeholder ({d.get('revisit_if')!r}). "
                "Name the condition, or say 'permanent'")
        did = str(d.get("id", "")).strip()
        if did:
            if did in seen:
                problems.append(
                    f"{where} duplicate id {did!r} (also decisions[{seen[did]}]). "
                    "To change a decision, append a new entry with a new id and "
                    "set supersedes")
            else:
                seen[did] = i

    for d in decisions:
        if isinstance(d, dict):
            sup = str(d.get("supersedes", "")).strip()
            if sup and sup not in seen:
                problems.append(f"supersedes {sup!r} refers to no known decision id")
            # docs_at graduates a decision into a design document. A dangling
            # pointer is worse than none: the plan stops showing the item on the
            # promise that it is written down somewhere.
            doc = str(d.get("docs_at", "")).strip()
            if doc and not (ROOT / doc).is_file():
                problems.append(
                    f"docs_at {doc!r} does not exist. A graduated decision must "
                    "point at a real document, or the plan hides an item that "
                    "was never written down")

    # direction[] and questions[] are the other two tenses. Same honesty rule:
    # a placeholder is worse than an absent entry, because a later agent reads
    # it as real.
    direction = data.get("direction") or []
    if not isinstance(direction, list):
        problems.append("direction must be a list")
        direction = []
    for i, d in enumerate(direction):
        where = f"direction[{i}]"
        if not isinstance(d, dict):
            problems.append(f"{where} is not an object")
            continue
        for key in ("id", "heading", "accommodate"):
            if not str(d.get(key, "")).strip():
                problems.append(f"{where} missing {key}")
        acc = str(d.get("accommodate", "")).strip().lower().rstrip(".")
        if acc in placeholders:
            problems.append(
                f"{where} accommodate is a placeholder ({d.get('accommodate')!r})."
                " Direction that constrains nothing is noise; delete it")

    questions = data.get("questions") or []
    if not isinstance(questions, list):
        problems.append("questions must be a list")
        questions = []
    q_ids: dict[str, int] = {}
    for i, q in enumerate(questions):
        where = f"questions[{i}]"
        if not isinstance(q, dict):
            problems.append(f"{where} is not an object")
            continue
        for key in ("id", "question", "blocks"):
            if not str(q.get(key, "")).strip():
                problems.append(f"{where} missing {key}")
        qid = str(q.get("id", "")).strip()
        if qid:
            if qid in q_ids:
                problems.append(f"{where} duplicate id {qid!r}")
            else:
                q_ids[qid] = i
        # An answered question must MOVE, not linger. A question sitting next to
        # a decision that answers it is how a stale frontier is born.
        if qid and qid in seen:
            problems.append(
                f"{where} id {qid!r} is also a decision. An answered question is"
                " DELETED from questions[] - the decision carries its text")

    if problems:
        for line in problems:
            print(f"  {RED}{line}{RST}")
        print("  skill: .agents/skills/godot-project-decisions/SKILL.md")
        RESULTS.fail("shape")
        return

    if pending:
        print(f"  {YEL}not onboarded yet{RST}: {' and '.join(pending)} not chosen")
        print("  Ask the human: what is it called, what is the game in a"
              " sentence or two, and how much say do they want over structure"
              " before code is written: hands-off, module, file or function).")
        print("  skill: .agents/skills/godot-project-decisions/SKILL.md")
        RESULTS.skip("shape (not onboarded)")
        return

    live = [d for d in decisions if isinstance(d, dict)]
    superseded = {str(d.get("supersedes", "")).strip() for d in live}
    current = [d for d in live if str(d.get("id", "")) not in superseded]
    duty = {
        "hands-off": "build and report; propose nothing",
        "module": "propose modules, boundaries and crossing data BEFORE code",
        "file": "propose modules AND every file you create/modify BEFORE code",
        "function": "propose modules, files AND function signatures BEFORE code",
    }.get(inv, "")
    print(f"  {data.get('name')}: {str(data.get('pitch'))[:70]}")
    if duty:
        print(f"  involvement {YEL}{inv}{RST}: {duty}")
        if inv != "hands-off":
            print("  record the approved structure in proposal.json before code")
    print(f"  {len(current)} current decision(s)"
          + (f", {len(live) - len(current)} superseded" if len(live) != len(current) else ""))
    for d in current[-5:]:
        print(f"    {d.get('id')}: {str(d.get('answer'))[:60]}")
    live_dir = [d for d in direction if isinstance(d, dict)]
    if live_dir:
        print(f"  {len(live_dir)} direction note(s) - read before inventing a"
              " persisted format")
        for d in live_dir[:4]:
            print(f"    {DIM}{d.get('id')}: {str(d.get('accommodate'))[:58]}{RST}")
    live_q = [q for q in questions if isinstance(q, dict)]
    if live_q:
        # Age matters. A question raised this week is work in progress; one
        # raised six weeks ago and never closed is a crumb nobody swept up, and
        # it will keep silently shaping decisions until someone answers it.
        today = _dt.date.today()
        aged = []
        for q in live_q:
            try:
                raised = _dt.date.fromisoformat(str(q.get("raised", ""))[:10])
                aged.append(((today - raised).days, q))
            except Exception:
                aged.append((-1, q))
        aged.sort(key=lambda t: -t[0])
        stale = [(d, q) for d, q in aged if d >= 14]
        print(f"  {YEL}{len(live_q)} open question(s){RST}"
              + (f", {len(stale)} unanswered 14+ days" if stale else ""))
        for days, q in aged[:5]:
            age = f"{days}d" if days >= 0 else "no date"
            mark = RED if days >= 14 else DIM
            print(f"    {mark}[{age}]{RST} {q.get('id')}: "
                  f"{str(q.get('question'))[:56]}")
        if stale:
            print("  Answer it, or delete it and record why it stopped"
                  " mattering. An open question nobody revisits is a decision"
                  " being made by default.")
    RESULTS.passed("shape")


def stage_format() -> None:
    head("format (gdformat --check)")
    tool = gdtool("gdformat")
    if tool is None:
        print("  gdformat unavailable. Run: python bootstrap.py --fix")
        RESULTS.skip("format")
        return
    files = [str(p) for p in gd_files()]
    if not files:
        RESULTS.passed("format")
        return
    code, out = run(tool + ["--line-length=120", "--check"] + files, 120,
                    LOG_DIR / "format.log")
    indent(out, 20)
    if code == 0:
        RESULTS.passed("format")
    else:
        # Deliberately not "gdformat ." -- that is unscoped and reformats
        # addons/, which is third-party code the gate excludes. --fix-format
        # reuses this stage's own filtered file list.
        print("  fix with: python check.py --fix-format")
        RESULTS.fail("format")


def fix_format() -> int:
    """Format exactly the files the format stage checks, and nothing else."""
    head("fix-format (gdformat, project files only)")
    tool = gdtool("gdformat")
    if tool is None:
        print(f"  {RED}gdformat unavailable{RST}. Run: python bootstrap.py --fix")
        return 2
    files = [str(p) for p in gd_files()]
    if not files:
        print("  no .gd files found")
        return 0
    print(f"  {len(files)} file(s) in scope (addons/ excluded)")
    code, out = run(tool + ["--line-length=120"] + files, 180,
                    LOG_DIR / "fix-format.log")
    indent(out, 30)
    if code == 0:
        print(f"  {GRN}formatted{RST}. Review with: git diff")
        return 0
    print(f"  {RED}gdformat exited {code}{RST}")
    return 1


def stage_lint() -> None:
    head("lint (gdlint)")
    tool = gdtool("gdlint")
    if tool is None:
        print("  gdlint unavailable. Run: python bootstrap.py --fix")
        RESULTS.skip("lint")
        return
    # gdlint honours .gdlintrc excluded_directories; gdformat does not, so both
    # are given the same explicitly filtered list for consistency.
    targets = [str(p) for p in gd_files()] or [str(PROJECT_DIR)]
    code, out = run(tool + targets, 120, LOG_DIR / "lint.log")
    indent(out, 30)
    if code == 0:
        RESULTS.passed("lint")
    elif re.search(r"NotImplementedError|Traceback", out):
        # gdlint lags new GDScript syntax and has crashed on valid code.
        print(f"  {YEL}gdlint crashed rather than reported a violation{RST}")
        print("  Known gdtoolkit issue with newer syntax. Not blocking.")
        RESULTS.skip("lint (tool crash)")
    else:
        RESULTS.fail("lint")


def stage_import(godot: str) -> None:
    head("import (cache warm-up)")
    # Exit code deliberately ignored: known to return 1 on a clean import.
    _, out = run(
        [godot, "--headless", "--path", str(PROJECT_DIR), "--import", "--quit"],
        180, LOG_DIR / "import.log",
    )
    errs = scan(out)
    if errs:
        indent("\n".join(errs))
        RESULTS.fail("import")
    else:
        RESULTS.passed("import")


def stage_typecheck(godot: str) -> None:
    head("typecheck (--check-only, per file)")
    # Per-file rather than whole-project: --check-only reports one file at a
    # time, so a single invocation hides every error after the first.
    files = gd_files()
    if not files:
        print("  no .gd files found")
        RESULTS.skip("typecheck")
        return
    # Accumulate raw output across every file, then group once. Grouping
    # per file would print the same error 20 times for 20 files.
    combined: List[str] = []
    for path in files:
        rel = path.relative_to(PROJECT_DIR).as_posix()
        _, out = run(
            [godot, "--headless", "--path", str(PROJECT_DIR),
             "--check-only", "--script", f"res://{rel}", "--quit"],
            60, LOG_DIR / "_tc_one.log",
        )
        if ERROR_RE.search(out):
            combined.append(out)
    (LOG_DIR / "_tc_one.log").unlink(missing_ok=True)
    groups = group_errors("\n".join(combined))
    total = sum(g["count"] for g in groups)
    log_lines = [f"checked {len(files)} file(s)",
                 f"{total} error(s) in {len(groups)} distinct kind(s)", ""]
    for g in groups:
        log_lines.append(f"[x{g['count']}] {g['msg']}")
        log_lines.extend(f"    {loc}" for loc in g["locs"])
    (LOG_DIR / "typecheck.log").write_text("\n".join(log_lines) + "\n",
                                           encoding="utf-8", newline="")
    print(f"  checked {len(files)} file(s)")
    if groups:
        print(f"  {total} error(s), {len(groups)} distinct kind(s)")
        indent("\n".join(render_errors(groups)), 80)
        blob = " ".join(g["msg"] for g in groups).lower()
        if "variant" in blob or "infer" in blob:
            skill("godot-typed-data-boundary",
                  "Variant / inference errors -- convert at the boundary, "
                  "not at use sites")
        if "not present" in blob or "not declared" in blob or "identifier" in blob:
            skill("godot-api-lookup",
                  "verify the API against the local engine reference before "
                  "guessing")
        RESULTS.fail("typecheck")
    else:
        RESULTS.passed("typecheck")


def stage_grep() -> None:
    head("banned idioms")
    banned = load_banned()
    source = GATE_RULES_FILE.name if GATE_RULES_FILE.is_file() else "built-in fallback"
    print(f"  {len(banned)} rule(s) from {source}")
    if not GATE_RULES_FILE.is_file():
        print(f"  {YEL}gate.rules.json missing -- only the critical rule is active{RST}")
    hits = 0
    for path in gd_files():
        rel = path.relative_to(PROJECT_DIR).as_posix()
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            print(f"  could not read {rel}: {exc}")
            continue
        for lineno, line in enumerate(lines, 1):
            if SUPPRESS.search(line):
                continue
            for pattern, msg, rule_id in banned:
                if pattern.search(line):
                    print(f"  {RED}[{rule_id}] {msg}{RST}")
                    print(f"    {rel}:{lineno}: {line.strip()[:120]}")
                    hits += 1
    if hits:
        print(f"\n  {hits} banned idiom(s). Suppress a false positive with"
              " a trailing  # gate:allow")
        skill("godot-api-lookup", "these are Godot 3 APIs -- confirm the Godot 4 equivalent")
        RESULTS.fail("grep")
    else:
        RESULTS.passed("grep")


# Loose containers in a signature: Dictionary/Array with no element type, or a
# bare Variant. These are the shapes that let untyped external data travel into
# typed code, where every downstream use site becomes a separate type error.
LOOSE_SIG = re.compile(
    r"(?:->\s*(?:Variant|Dictionary|Array)\s*:"          # loose return
    r"|:\s*(?:Variant|Dictionary|Array)\s*(?=[,)=])"      # loose param
    r")")
# Calls that produce Variant from outside the program.
PARSE_CALL = re.compile(
    r"\b(?:JSON\.parse_string|JSON\.parse|str_to_var|bytes_to_var"
    r"|ConfigFile|FileAccess\.open|ResourceLoader\.load)\b")
# Layer directories come from arch.rules.json so a project can reorganise
# (feature folders, extra tiers) without editing a hash-pinned gate file.
_DEFAULT_INTERIOR = ("scripts/logic/",)
_DEFAULT_BOUNDARY = ("scripts/data/",)


def _layer_dirs():
    """(interior, boundary) tuples, from arch.rules.json if declared."""
    path = ROOT / "arch.rules.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tb = data.get("type_boundary") or {}
        inter = tuple(str(d) for d in tb.get("interior", []))
        bound = tuple(str(d) for d in tb.get("boundary", []))
        if inter:
            return inter, bound
    except (OSError, ValueError, AttributeError):
        pass
    return _DEFAULT_INTERIOR, _DEFAULT_BOUNDARY


def _typed_generic(line: str, col: int) -> bool:
    """True if the match is actually a typed generic like Array[int]."""
    tail = line[col:]
    return bool(re.match(r"\s*(?:Variant|Dictionary|Array)\s*\[", tail))


def stage_types() -> None:
    """Untyped external data must not reach typed code.

    Two different things wear the same syntax, so they are graded differently:

    * A loose container in a file that ALSO parses external data is the
      untyped-data problem. It converges by containment and it blocks.
    * A loose container in a parse-free file is heterogeneous storage - a
      deliberate design choice (component registries, generic stores) that
      no amount of boundary work removes. Advisory, with different advice.
    """
    interior, boundary = _layer_dirs()
    head("type boundary (untyped data must not reach typed code)")
    blocking: list[str] = []
    advisory: list[str] = []
    checked = 0
    for path in gd_files():
        rel = path.relative_to(PROJECT_DIR).as_posix()
        if not any(rel.startswith(d) for d in interior):
            continue
        if any(rel.startswith(d) for d in boundary):
            continue
        checked += 1
        try:
            lines = path.read_text(encoding="utf-8",
                                   errors="replace").splitlines()
        except OSError as exc:
            print(f"  could not read {rel}: {exc}")
            continue
        parses = any(PARSE_CALL.search(ln) for ln in lines
                     if not SUPPRESS.search(ln)
                     and not ln.strip().startswith("#"))
        for lineno, line in enumerate(lines, 1):
            if SUPPRESS.search(line):
                continue
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith(("func ", "static func ", "signal ")):
                m = LOOSE_SIG.search(line)
                if m and not _typed_generic(line, m.start()):
                    entry = f"    {rel}:{lineno}: {stripped[:110]}"
                    (blocking if parses else advisory).append(entry)
            if PARSE_CALL.search(line):
                blocking.append(
                    f"    {rel}:{lineno}: {stripped[:110]}   <- parse call")
    print(f"  checked {checked} file(s) under {', '.join(interior)}")
    if advisory:
        print(f"  {YEL}note{RST}: loose container(s) in parse-free file(s) - "
              f"heterogeneous storage, not an untyped-data leak")
        for line in advisory:
            print(line)
        print("""  This is a storage design choice, not a boundary bug. Do NOT try to
  convert at a boundary: there is no external data here. Confine it behind
  a typed facade so callers never handle the loose value, and suppress the
  facade's own signature with  # gate:allow  once you are satisfied.""")
    if blocking:
        print(f"  {RED}untyped external data reaching typed code{RST}")
        for line in blocking:
            print(line)
        print(f"""
  {len(blocking)} boundary violation(s). Parse and convert external data ONCE,
  at the boundary ({', '.join(boundary) or 'your boundary layer'}), into typed
  objects. Pass only typed values inward. Fixing individual use sites does not
  converge: there is no safe cast from Variant in GDScript, so each fix reveals
  the next. See docs/GDSCRIPT.md.
  Suppress a deliberate exception with a trailing  # gate:allow""")
        skill("godot-typed-data-boundary", "the rule and how to apply it")
        RESULTS.fail("types")
    else:
        RESULTS.passed("types")


def stage_arch() -> None:
    head("architecture (generated graph + boundary rules)")
    script = ROOT / "arch.py"
    if not script.is_file():
        print("  arch.py not present, architecture stage disabled")
        RESULTS.skip("arch")
        return
    code, out = run([sys.executable, str(script), "--check"], 60,
                    LOG_DIR / "arch.log")
    indent(out, 30)
    if "undeclared module" in out:
        print("  skill: .agents/skills/godot-human-involvement/SKILL.md")
    if code == 0:
        RESULTS.passed("arch")
    else:
        print("  regenerate the diagram with: python arch.py --write")
        print("  or fix the boundary violation; see arch.rules.json")
        RESULTS.fail("arch")


def involvement() -> str:
    """Declared involvement level, or "" if unset. Read by the conformance
    stage to decide how deep to diff."""
    try:
        raw = json.loads((ROOT / "project.shape.json").read_text(encoding="utf-8"))
        return str(raw.get("involvement", "") or "").strip()
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def arch_depth() -> int:
    """module_depth from arch.rules.json, so conformance groups files the same
    way arch.py does."""
    path = ROOT / "arch.rules.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        value = int(raw.get("module_depth", 2))
        return max(1, value)
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return 2


EXPECTED_ERRORS_REL = "tests/expected_errors.json"


def load_expected_errors() -> Tuple[List[Dict[str, str]], str]:
    """Error lines a passing test is expected to emit.

    Returns (entries, problem). Each entry needs a `match` substring and a
    `why`. Malformed or missing file yields no allowances, so the default is
    always fail-closed: an undeclared error still fails the stage.
    """
    path = PROJECT_DIR / EXPECTED_ERRORS_REL
    if not path.is_file():
        return [], ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [], f"{EXPECTED_ERRORS_REL} unreadable ({exc}); allowing nothing"
    items = raw.get("expected") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return [], f"{EXPECTED_ERRORS_REL}: expected a list under 'expected'"
    out: List[Dict[str, str]] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        match = entry.get("match")
        why = entry.get("why")
        if not isinstance(match, str) or not match.strip():
            return [], f"{EXPECTED_ERRORS_REL}: every entry needs a 'match' string"
        if not isinstance(why, str) or not why.strip():
            return [], (f"{EXPECTED_ERRORS_REL}: entry {match!r} needs a 'why'"
                        " -- an unexplained allowance is how errors get hidden")
        out.append({"match": match, "why": why})
    return out, ""


def write_plan() -> None:
    """Regenerate plan.html.

    Run from the gate rather than by hand: a view a human must remember to
    refresh is a view that is silently stale, and a stale plan is worse than
    no plan because it reads as current.
    """
    script = ROOT / "tools" / "plan_html.py"
    if not script.is_file():
        return
    code, _ = run([sys.executable, str(script)], 30, LOG_DIR / "plan.log")
    if code == 0:
        print(f"  {DIM}plan.html regenerated{RST}")


def stage_design() -> None:
    """Regenerate the design index and tunable bindings, then report drift.

    Two projections, neither a source of truth. The index exists so an agent can
    find the right section without loading the whole design body -- as design
    grows, loading all of it stops being possible, and an index that is one line
    per section stays cheap forever.

    Bindings connect a design-declared tunable to whichever symbol claims it via
    a `## @tune <id>` comment. Design names the tunable; code claims it. Design
    never contains a path, so code is free to move.

    Regenerates rather than failing on staleness. ARCHITECTURE.md fails because
    module structure changes rarely; tuning values change constantly, and a gate
    that fails on every balance tweak trains people to stop tuning or stop
    regenerating. Same reasoning as plan.html, which is rewritten silently.

    Two things are reported rather than regenerated away:

      unbound     A design-declared tunable no code claims. Either unbuilt, or
                  the knob was removed and design does not know.
      undeclared  A tunable claimed in code that no section declares. A knob
                  nobody designed. This is the more useful signal: a 3px inset
                  invented during implementation, appearing in no design and no
                  experience contract, is exactly what this surfaces.
    """
    head("design index and bindings")
    tool = ROOT / "tools" / "design.py"
    design_dir = PROJECT_DIR.parent / "docs" / "design"
    if not tool.is_file():
        print("  tools/design.py not found")
        RESULTS.skip("design")
        return
    code, out = run([sys.executable, str(tool), "--json"], 60, LOG_DIR / "design.log")
    if code != 0:
        print(indent(out or "(no output)"))
        RESULTS.fail("design (generator failed)")
        return
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        print(f"  {RED}design.py produced unparseable output{RST}")
        RESULTS.fail("design (bad generator output)")
        return

    sections = data.get("sections") or []
    if not sections:
        print("  no design sections yet")
        RESULTS.skip("design")
        return

    # Write mode: the --json call above is read-only, so run once more to emit.
    run([sys.executable, str(tool)], 60, LOG_DIR / "design_write.log")

    declared = {t for sec in sections for t in (sec.get("declared_tunables") or [])}
    print(f"  {len(sections)} section(s), {len(declared)} tunable(s) declared")

    for t in data.get("unbound") or []:
        print(f"  {YEL}note: `{t}` declared in design, claimed by no code{RST}")
    for t in data.get("undeclared") or []:
        b = (data.get("bound") or {}).get(t) or {}
        loc = f"{b.get('file', '?')}:{b.get('line', '?')}"
        print(f"  {YEL}note: `{t}` claimed at {loc}, declared in no section{RST}")
        print("        a knob nobody designed -- either declare it or inline it")
    for t in data.get("const_bound") or []:
        print(f"  {YEL}note: `{t}` is declared tunable but bound to a const{RST}")
        print("        design says this is adjustable; a const cannot be adjusted"
              " without a rebuild")
    for p in data.get("unstated_resolution") or []:
        print(f"  {YEL}note: {p} states no resolution line{RST}")
        print("        a reader cannot tell a leaning from a commitment")

    ready = data.get("ready") or []
    if ready:
        print(f"  {len(ready)} section(s) with work derivable from design"
              " (see docs/design/INDEX.md)")
    RESULTS.passed("design")


def _is_example(item) -> bool:
    """True for a template example: any item with an "_" key.

    The template documents its own field shape inline, because an empty array
    taught an agent nothing and it invented key names that every reader then
    discarded. Examples must not read as data, or a fresh clone looks like it
    has proposed structure.
    """
    return isinstance(item, dict) and any(
        k.startswith("_") for k in item)


def _acks(prop: dict) -> dict:
    """Warnings the human consciously accepted, for THIS slice only.

    An ack is scoped to the slice it was made in. Carrying it forward would
    turn one considered decision into a permanent blind spot -- the human
    accepted building movement without design, not building everything
    without design, forever.
    """
    out: dict = {}
    cur = str(prop.get("slice", "") or "").strip()
    raw = prop.get("acknowledged")
    if not isinstance(raw, list):
        return out
    for a in raw:
        if not isinstance(a, dict):
            continue
        if str(a.get("slice", "") or "").strip() != cur:
            continue
        w = str(a.get("warning", "") or "").strip()
        if w:
            out[w] = a
    return out


def _ack_line(a: dict) -> None:
    why = str(a.get("why", "") or "").strip()
    who = str(a.get("by", "") or "").strip()
    when = str(a.get("on", "") or "").strip()
    tail = " · ".join(x for x in (who, when) if x)
    print(f"  {DIM}accepted by human: {why}{RST}")
    if tail:
        print(f"  {DIM}  ({tail}){RST}")


def stage_conformance() -> None:
    """Diff the real tree against the approved proposal.

    An approval that lives only in a chat message evaporates when the turn
    ends: the human approves one structure, gets another, and nothing notices.
    proposal.json makes the approved shape durable so the diff is mechanical.

    Reports rather than fails. Deviation while building is legitimate -- what
    is not legitimate is silent deviation, and a report the agent must address
    is the mechanism for that. Depth follows involvement: a module-level human
    is not shown unproposed function signatures they never approved.
    """
    head("proposal conformance")
    path = ROOT / "proposal.json"
    level = involvement()
    if not path.is_file():
        print("  no proposal.json")
        RESULTS.skip("conformance")
        return
    try:
        prop = json.loads(path.read_text(encoding="utf-8"))
        for key in ("modules", "files", "functions", "design_refs",
                    "considered_existing", "revisions"):
            if isinstance(prop.get(key), list):
                prop[key] = [i for i in prop[key] if not _is_example(i)]
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  {RED}proposal.json unreadable: {exc}{RST}")
        RESULTS.fail("conformance (unreadable proposal)")
        return
    if not isinstance(prop, dict):
        print(f"  {RED}proposal.json must be an object{RST}")
        RESULTS.fail("conformance (bad proposal shape)")
        return

    def listed(key: str, field: str) -> set:
        items = prop.get(key)
        if not isinstance(items, list):
            return set()
        return {str(i[field]).strip().replace("\\", "/")
                for i in items if isinstance(i, dict) and i.get(field)}

    prop_mods = listed("modules", "path")
    prop_files = listed("files", "path")
    status = str(prop.get("status", "") or "").strip().lower()

    if status and status not in ("draft", "approved"):
        print(f"  {RED}status '{status}' is not 'draft' or 'approved'{RST}")
        RESULTS.fail("conformance (bad status)")
        return

    # Experience before structure. Every other artefact in this repo describes
    # structure; nothing else captures intended feel. Without it, feel gets
    # inferred from whatever the renderer happens to do -- a cell-based
    # renderer reads as cell-stepped movement, which may be the opposite of
    # what was wanted. Required at every level, including hands-off, because
    # the inference is just as wrong when nobody is watching.
    exp = prop.get("experience")
    has_structure = bool(prop_mods or prop_files)
    if has_structure:
        if not isinstance(exp, dict):
            print(f"  {RED}proposal has structure but no 'experience' object{RST}")
            print("  Describe what the player does and how it should feel BEFORE"
                  " proposing modules.")
            skill("godot-human-involvement",
                  "structure chosen before intended feel is a guess")
            RESULTS.fail("conformance (no experience contract)")
            return
        missing = [k for k in ("player_does", "feels_like")
                   if not str(exp.get(k, "") or "").strip()]
        if missing:
            print(f"  {RED}experience is missing: {', '.join(missing)}{RST}")
            print("  These two are the minimum. Structure without them is chosen"
                  " on an inference.")
            skill("godot-human-involvement",
                  "intended feel must be stated, not inferred")
            RESULTS.fail("conformance (incomplete experience contract)")
            return
        print(f"  experience: {str(exp.get('player_does'))[:64]}")

    # A mockup is conditional, not required -- raster art, materials and
    # physics feel cannot be drawn as vectors. But an empty mockup with no
    # stated reason is the case where nobody considered it, so ask once.
    mock = prop.get("mockup")
    if has_structure and isinstance(mock, dict):
        svg = str(mock.get("svg", "") or "").strip()
        why_not = str(mock.get("not_possible", "") or "").strip()
        if svg:
            print("  mockup: present")
        elif not why_not:
            print(f"  {YEL}note: no mockup and no reason given{RST}")
            print("  If the outcome is vector-approximable (shapes, grid, layout)"
                  " draw it; rejecting")
            print("  a picture is far cheaper than rejecting an implementation."
                  " If it cannot be")
            print("  drawn, say why in mockup.not_possible.")

    # Design ancestry. Every slice is either descended from a design choice or
    # is a throwaway prototype informing one. It does not have to WRITE design;
    # it has to point at the design it serves. A slice with no such pointer is
    # work whose purpose lives only in a chat message.
    #
    # A dangling pointer is a hard failure -- a reference to a file that does
    # not exist is worse than no reference, because it reads as though the
    # rationale was written down. Absence is only a warning, because a design
    # body that is genuinely thin is a real state and failing here would only
    # produce fabricated ancestry.
    if has_structure:
        refs = prop.get("design_refs")
        refs = refs if isinstance(refs, list) else []
        dangling: List[str] = []
        resolved: List[str] = []
        for r in refs:
            if not isinstance(r, dict):
                continue
            sec = str(r.get("section", "") or "").strip().replace("\\", "/")
            if not sec:
                continue
            cand = ROOT / sec
            if cand.is_file():
                resolved.append(sec)
            else:
                dangling.append(sec)
        if dangling:
            print(f"  {RED}design_refs point at files that do not exist:{RST}")
            for s in dangling:
                print(f"    {s}")
            print("  A reference to a missing document reads as though the reason"
                  " was written down.")
            print("  Either write the section or remove the reference.")
            skill("godot-design-discovery",
                  "ancestry must point at something a reader can open")
            RESULTS.fail("conformance (dangling design_refs)")
            return
        acks = _acks(prop)
        if resolved:
            print(f"  design_refs: {len(resolved)} resolved")
        elif "no-design-refs" in acks:
            print(f"  {YEL}note: no design_refs — building without design,"
                  f" accepted{RST}")
            _ack_line(acks["no-design-refs"])
        else:
            print(f"  {YEL}note: no design_refs — why does this work exist?{RST}")
            print("  Point at the design section establishing the end state this"
                  " serves. If nothing")
            print("  in docs/design/ does, that is a signal the design body is"
                  " thin here: enter")
            print("  discovery rather than writing a rationale from the feature"
                  " above it.")
            print("  If building without it is genuinely the right call, ask the"
                  " human, then record")
            print("  it in acknowledged[] with their reason. Do not accept it on"
                  " their behalf.")
            skill("godot-design-discovery",
                  "work with no stated end state gets built on an inference")

    # A draft is a proposal awaiting review, not a contract. Diffing it would
    # report deviation from something nobody agreed to, which trains the human
    # to ignore the report.
    if status == "draft" and level not in ("", "hands-off"):
        n = len(prop_mods) + len(prop_files)
        print(f"  {YEL}proposal is a DRAFT awaiting review"
              f" ({n} item(s)){RST}")
        print("  open plan.html; approval sets status to 'approved'")
        write_plan()
        RESULTS.skip("conformance")
        return

    if level in ("", "hands-off"):
        print(f"  involvement '{level or 'unset'}': proposal is a record, not a"
              " contract")
        if prop_mods or prop_files:
            print(f"  recorded: {len(prop_mods)} module(s),"
                  f" {len(prop_files)} file(s)")
        write_plan()
        RESULTS.skip("conformance")
        return

    if not (prop_mods or prop_files):
        print(f"  {YEL}proposal.json is empty at involvement '{level}'{RST}")
        print("  Write it as a draft, render plan.html, then wait for approval.")
        skill("godot-human-involvement", "an unapproved structure is unreviewable")
        write_plan()
        RESULTS.skip("conformance")
        return

    if not status:
        print(f"  {YEL}note: no status field; treating as approved{RST}")

    # Actual state, from src/ only. Gate scripts and docs are not game code.
    all_real = {str(f.relative_to(PROJECT_DIR)).replace("\\", "/")
                for f in gd_files()}
    all_real = {f for f in all_real
                if not f.startswith("tests/") and not f.startswith("addons/")}

    # A per-slice proposal cannot claim files that predate it. Scope the diff to
    # what changed since approval, using the git sha recorded at approval time.
    # Without a baseline the diff covers the whole tree and says so, because a
    # silently over-broad diff trains the agent to ignore this stage.
    baseline = str(prop.get("baseline_sha", "") or "").strip()
    scoped = False
    if baseline and shutil.which("git"):
        code, out = run(["git", "-C", str(ROOT), "diff", "--name-only",
                         baseline, "--", "src"], 30, LOG_DIR / "conformance.log")
        # git diff shows tracked changes only. New work is usually UNTRACKED,
        # so omitting it scopes the diff to nothing and the stage reports a
        # perfect match over files it never looked at.
        _, untracked = run(["git", "-C", str(ROOT), "ls-files", "--others",
                            "--exclude-standard", "--", "src"], 30,
                           LOG_DIR / "conformance.log")
        if code == 0:
            changed = set()
            for line in (out + "\n" + untracked).splitlines():
                line = line.strip().replace("\\", "/")
                if line.startswith("src/"):
                    changed.add(line[len("src/"):])
            real_files = all_real & changed
            scoped = True
            print(f"  {DIM}scoped to {len(real_files)} file(s) changed since"
                  f" {baseline[:8]}{RST}")
        else:
            print(f"  {YEL}baseline_sha {baseline[:8]!r} not found in git;"
                  f" comparing whole tree{RST}")
            real_files = all_real
    else:
        real_files = all_real
        if not baseline:
            print(f"  {DIM}no baseline_sha in proposal; comparing whole tree{RST}")

    # Each file declares create or modify. Nothing checked it, so `create` on a
    # file that already existed slipped through twice and a human caught it both
    # times. It is a two-line comparison against the tree at the baseline, and a
    # wrong action means the agent has not looked at what is already there --
    # which is exactly when extend-before-create gets skipped.
    action_wrong: List[str] = []
    if baseline and shutil.which("git"):
        code, listing = run(["git", "-C", str(ROOT), "ls-tree", "-r",
                             "--name-only", baseline, "--", "src"], 30,
                            LOG_DIR / "conformance.log")
        if code == 0:
            at_baseline = set()
            for line in listing.splitlines():
                line = line.strip().replace("\\", "/")
                if line.startswith("src/"):
                    at_baseline.add(line[len("src/"):])
            for item in (prop.get("files") or []):
                if not isinstance(item, dict):
                    continue
                rel = str(item.get("path", "") or "").strip().replace("\\", "/")
                act = str(item.get("action", "") or "").strip().lower()
                if not rel:
                    continue
                # Skipping a missing action meant this whole check never ran:
                # two real proposals omitted the field entirely, so "modify"
                # vanished from the plan and nothing compared anything.
                if not act:
                    action_wrong.append(
                        f"{rel}: no action declared (expected 'new' or 'modify')")
                    continue
                if act not in ("new", "create", "modify"):
                    action_wrong.append(
                        f"{rel}: action {act!r} is not 'new' or 'modify'")
                    continue
                existed = rel in at_baseline
                if act in ("new", "create") and existed:
                    action_wrong.append(f"{rel}: marked '{act}' but it already existed")
                elif act == "modify" and not existed:
                    action_wrong.append(f"{rel}: marked 'modify' but it did not exist")

    depth = arch_depth()
    # A module is a DIRECTORY. Truncating a file path to `depth` segments turns
    # scripts/main.gd into a phantom module, so derive from the parent.
    real_mods = set()
    for rel in real_files:
        parent = str(Path(rel).parent).replace("\\", "/")
        if parent in ("", "."):
            continue
        real_mods.add("/".join(parent.split("/")[:depth]))

    notes = 0
    if action_wrong:
        notes += 1
        print(f"  {YEL}wrong action on {len(action_wrong)} file(s):{RST}")
        for line in action_wrong[:8]:
            print(f"    {line}")
        print("  Check what exists before proposing. Extending beats creating a"
              " second copy beside it.")

    unproposed_mods = sorted(real_mods - prop_mods)
    if unproposed_mods:
        notes += 1
        print(f"  {YEL}module(s) in code but not in the proposal:{RST}")
        for m in unproposed_mods[:8]:
            print(f"    {m}")
    unbuilt_mods = sorted(prop_mods - real_mods)
    if unbuilt_mods:
        print(f"  {DIM}proposed, not yet built: {', '.join(unbuilt_mods[:6])}{RST}")

    if level in ("file", "function"):
        unproposed = sorted(real_files - prop_files)
        if unproposed:
            notes += 1
            print(f"  {YEL}file(s) in code but not in the proposal:{RST}")
            for f in unproposed[:12]:
                print(f"    {f}")
            if len(unproposed) > 12:
                print(f"    ... and {len(unproposed) - 12} more")
        unbuilt = sorted(prop_files - real_files)
        if unbuilt:
            print(f"  {DIM}proposed, not yet built: {len(unbuilt)} file(s){RST}")

    if notes:
        print(f"  Deviation is fine; SILENT deviation is not. Report what"
              f" differs, revise proposal.json, re-approve at '{level}'.")
        skill("godot-human-involvement", "the approved structure changed")
    else:
        print(f"  matches the approved proposal ({len(prop_mods)} module(s)"
              + (f", {len(prop_files)} file(s)" if level in ("file", "function") else "")
              + ")")
    write_plan()
    RESULTS.passed("conformance")


def stage_gut(godot: str) -> None:
    head("unit tests (GUT)")
    if not (PROJECT_DIR / "addons" / "gut" / "gut_cmdln.gd").is_file():
        print("  GUT not installed. Run: python bootstrap.py --fix")
        RESULTS.skip("gut")
        return
    code, out = run(
        [godot, "--headless", "--path", str(PROJECT_DIR), "-d",
         "-s", "res://addons/gut/gut_cmdln.gd",
         "-gconfig=res://.gutconfig.json", "-gexit"],
        60, LOG_DIR / "gut.log",
    )
    # GUT needs -d for asserts, which also puts Godot in the interactive
    # debugger. An ordinary assertion failure therefore prints a debugger break
    # and a `debug>` prompt, which reads as a crash rather than a failed test.
    # stdin is closed so it cannot loop, but the noise makes a routine failure
    # look like infrastructure trouble. Collapse it to one honest line.
    lines = out.splitlines()
    breaks = [ln for ln in lines if "Debugger Break" in ln]
    if breaks:
        keep, dropped = [], 0
        for ln in lines:
            if ("Debugger Break" in ln or ln.strip() == "debug>"
                    or 'Enter "help" for assistance' in ln
                    or ln.lstrip().startswith("*Frame ")):
                dropped += 1
                continue
            keep.append(ln)
        shown = "\n".join(keep[-25:])
        indent(shown, 25)
        reasons = []
        for ln in breaks:
            m = re.search(r"Reason: '(.+)'\s*$", ln)
            r = m.group(1) if m else ln.strip()
            if r not in reasons:
                reasons.append(r)
        print(f"  {YEL}{len(breaks)} debugger break(s) — this is a failed test,"
              f" not a crash{RST}")
        for r in reasons[:4]:
            print(f"    {r}")
        print("  GUT needs -d for asserts, so a failing test stops in the"
              " debugger. Fix the test or the code;")
        print("  the break itself is not the problem.")
    else:
        indent("\n".join(lines[-25:]), 25)

    # Errors are scanned on EVERY branch, including the success path. A gate
    # that ignores errors when tests pass is the exact failure it exists to
    # prevent: GUT will print "All tests passed!" and a [GUT ERROR] together.
    errs = scan(out)
    gut_errs = [ln for ln in out.splitlines() if "[GUT ERROR]" in ln]
    hard = errs + [e for e in gut_errs if e not in errs]

    # A test that asserts bad input is REJECTED will legitimately produce error
    # output: push_error is the correct way for a parser to report a bad file,
    # and the test proving it fires is a passing test. Failing on that text
    # pressures the agent into deleting push_error and inventing a silent
    # error field instead, which is a worse design caused by the gate.
    # So expected errors are declared explicitly, per project, with a reason.
    # Anything NOT declared still fails closed.
    expected, exp_err = load_expected_errors()
    if exp_err:
        print(f"  {YEL}{exp_err}{RST}")
    if expected:
        kept, matched = [], set()
        for line in hard:
            hit = next((e for e in expected if e["match"] in line), None)
            if hit is None:
                kept.append(line)
            else:
                matched.add(hit["match"])
        if len(kept) < len(hard):
            print(f"  {len(hard) - len(kept)} expected error line(s) allowed by"
                  f" {EXPECTED_ERRORS_REL}")
        stale = [e["match"] for e in expected if e["match"] not in matched]
        if stale:
            print(f"  {YEL}note: declared expected error never occurred:"
                  f" {stale[0]!r}{RST}")
            print(f"  Remove it from {EXPECTED_ERRORS_REL} -- a stale allowance"
                  " hides a real error later.")
        hard = kept

    verdict: Optional[bool] = None
    if re.search(r"^\s*Totals", out, re.M):
        # Anchored to line start: an unanchored "Tests\s+0" also matches the
        # "Failing Tests   0" line of a perfectly healthy run.
        if re.search(r"^\s*Scripts\s+0\b", out, re.M) or re.search(r"\b0 scripts\b", out):
            print(f"  {RED}collected zero test scripts -- check .gutconfig.json dirs{RST}")
            skill("godot-headless-verification",
                  "a run that collects nothing can still exit 0")
            RESULTS.fail("gut (zero tests collected)")
            return
        failing = re.search(r"^\s*Failing [Tt]ests\s+(\d+)", out, re.M)
        if failing is not None:
            verdict = int(failing.group(1)) == 0
        elif "All tests passed!" in out:
            verdict = True
        elif re.search(r"^\s*Passing [Tt]ests\s+(\d+)", out, re.M):
            verdict = True
        else:
            verdict = False
    elif "All tests passed!" in out:
        verdict = True
    else:
        # No recognisable GUT summary. Exit code alone is not evidence a suite
        # ran: a misconfigured or early-exiting runner can produce no output and
        # still exit 0, which would otherwise read as a green gate over nothing.
        print(f"  {RED}no recognisable GUT summary in output{RST}")
        print("  Check .gutconfig.json 'dirs' and that tests/unit contains tests.")
        RESULTS.fail("gut (no summary; cannot confirm tests ran)")
        return

    if hard:
        indent("\n".join(hard[:15]), 15)
        print(f"  {RED}error(s) present in test output; failing regardless of"
              f" test counts{RST}")
        RESULTS.fail("gut (errors in output)")
    elif verdict:
        RESULTS.passed("gut")
    else:
        RESULTS.fail("gut")


def stage_smoke(godot: str) -> None:
    head("smoke test (headless boot)")
    _, out = run(
        [godot, "--headless", "--path", str(PROJECT_DIR), "res://tests/smoke_test.tscn"],
        120, LOG_DIR / "smoke.log",
    )
    indent("\n".join(out.splitlines()[-15:]), 15)
    if "SMOKE: PASS" in out and "SMOKE: FAIL" not in out:
        RESULTS.passed("smoke")
    else:
        RESULTS.fail("smoke")


# ---------------------------------------------------------------------- main

def retro_nudge() -> None:
    """Report whether a retrospective is due, from accumulated slice notes.

    Never calls a model and never fires per slice. A retrospective looks for
    patterns across slices, so triggering one after a single slice produces a
    reviewer wearing a retrospective's name: it cannot see a correction made in
    slice 3 and again in slice 4, which is the only class of finding that
    justifies a separate agent.

    The builder writes a note at slice end. This counts them.
    """
    tool = ROOT / "tools" / "retro_due.py"
    notes = ROOT / "docs" / "retro" / "notes"
    if not tool.exists() or not notes.is_dir():
        return
    n = len([q for q in notes.glob("*.md") if q.name.lower() != "readme.md"])
    if not n:
        return
    threshold = 10
    try:
        cfg = json.loads((ROOT / "retro.config.json").read_text(encoding="utf-8"))
        threshold = int(cfg.get("note_threshold", threshold)) or threshold
    except (OSError, ValueError, TypeError):
        pass
    if n >= threshold:
        print(f"{YEL}retrospective due{RST} {DIM}({n} unarchived slice note(s),"
              f" threshold {threshold}){RST}")
        print(f"{DIM}  copilot --agent retrospective -p \"Run a retrospective"
              f" over the unarchived notes.\"{RST}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verification gate for the Godot co-op project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="stages: " + " ".join(STAGES),
    )
    parser.add_argument("--only", action="append", default=[],
                        help="run only this stage (repeatable or comma-separated)")
    parser.add_argument("--fast", action="store_true",
                        help="skip import; use when only .gd files changed")
    parser.add_argument("--list", action="store_true", help="list stages and exit")
    parser.add_argument("--fix-format", action="store_true",
                        help="run gdformat over project .gd files (addons excluded)")
    parser.add_argument("--accept-gate-changes", action="store_true",
                        help="re-baseline the integrity manifest (HUMAN ONLY)")
    args = parser.parse_args()

    if args.list:
        print("\n".join(STAGES))
        return 0

    if args.fix_format:
        LOG_DIR.mkdir(exist_ok=True)
        return fix_format()

    if args.accept_gate_changes:
        write_manifest()
        print(f"re-baselined {MANIFEST_FILE.name} against the current gate files.")
        print("Commit it in the same commit as the gate change, so review sees both.")
        return 0

    selected = []
    for item in args.only:
        selected.extend(s.strip() for s in item.split(",") if s.strip())
    unknown = [s for s in selected if s not in STAGES]
    if unknown:
        print(f"unknown stage(s): {', '.join(unknown)}")
        print(f"valid stages: {' '.join(STAGES)}")
        return 2
    active = selected or list(STAGES)

    LOG_DIR.mkdir(exist_ok=True)

    head("prerequisites")
    print(f"python: {sys.version.split()[0]} on {sys.platform}")
    godot = find_godot()
    needs_godot = any(s in active for s in
                      ("import", "typecheck", "gut", "smoke", "resources"))
    if godot is None:
        if needs_godot:
            print(f"{RED}godot not on PATH{RST}")
            print("Set GODOT_BIN to the executable, or run: python bootstrap.py")
            return 2
        print(f"{YEL}godot not found, but no stage needs it{RST}")
    else:
        _, ver = run([godot, "--version"], 30, LOG_DIR / "version.log")
        version_line = ver.strip().splitlines()[-1] if ver.strip() else "unknown"
        print(f"godot:  {version_line}  ({godot})")
        if EXPECTED_VERSION not in version_line:
            print(f"{YEL}warning: expected {EXPECTED_VERSION};"
                  f" AGENTS.md and CI assume it{RST}")

    if "integrity" in active:
        stage_integrity()
    if "skills" in active:
        stage_skills()
    if "schema" in active:
        stage_schema()
    if "shape" in active:
        stage_shape()
    if "design" in active:
        stage_design()
    if "conformance" in active:
        stage_conformance()
    if "format" in active:
        stage_format()
    if "lint" in active:
        stage_lint()
    if "sanitise" in active:
        stage_sanitise()
    if "import" in active:
        if args.fast:
            RESULTS.skip("import (--fast)")
        else:
            stage_import(godot)  # type: ignore[arg-type]
    if "typecheck" in active:
        stage_typecheck(godot)  # type: ignore[arg-type]
    # Fail-fast barrier. Stages after this point load and execute GDScript.
    # Running them against code that does not parse produces noise at best and
    # a debugger spin at worst, and tells the agent nothing it does not already
    # know from typecheck. Cheap text-only stages still run so one gate pass
    # reports every static problem at once.
    parse_broken = any(
        line.startswith("FAIL") and (" typecheck" in line or " import" in line)
        for line in RESULTS.lines)
    if "grep" in active:
        stage_grep()
    if "types" in active:
        stage_types()
    if "arch" in active:
        stage_arch()
    if "tests" in active:
        stage_tests()
    if "assets" in active:
        stage_assets()
    if "resources" in active:
        if parse_broken:
            RESULTS.skip("resources (skipped: fix parse errors first)")
        else:
            stage_resources(godot)  # type: ignore[arg-type]
    if "gut" in active:
        if parse_broken:
            RESULTS.skip("gut (skipped: fix parse errors first)")
        else:
            stage_gut(godot)  # type: ignore[arg-type]
    if "smoke" in active:
        if parse_broken:
            RESULTS.skip("smoke (skipped: fix parse errors first)")
        else:
            stage_smoke(godot)  # type: ignore[arg-type]

    print(f"\n{DIM}================ summary ================{RST}")
    for line in RESULTS.lines:
        print(line)
    print(f"{DIM}logs: {LOG_DIR}{RST}")
    if RESULTS.failed:
        print(f"{RED}GATE FAILED{RST}")
        return 1
    print(f"{GRN}GATE PASSED{RST}")
    retro_nudge()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
