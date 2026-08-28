#!/usr/bin/env python3
"""Verification gate for a Godot 4.7.2 project using this portable kit.

Cross-platform: Windows, macOS and Linux, on Python 3.10+.
Python is used rather than a shell script because gdtoolkit already requires
it, so this adds no new dependency, and because Godot's own failure modes need
real logic rather than shell plumbing.

Godot's exit codes are not reliable:
  - `--headless --import --quit` can exit 1 on a completely clean import
  - other commands can exit 0 while printing hard errors
So every stage scans output for error markers, and every invocation has a
hard timeout because Godot can hang on import.

Public usage:
  kit verify                         run every stage
  kit verify --stage typecheck       run one stage (repeatable)
  kit verify --fast                  skip import (use when only .gd files changed)

Stages: format lint import typecheck grep gut smoke
Exit codes: 0 gate passed, 1 gate failed, 2 could not run (missing Godot)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.metadata
import json
import os
import re
import datetime as _dt
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# ROOT is the repository: gate scripts, docs, rule files, skills.
# PROJECT_DIR is the Godot project: everything the engine loads. Keeping them
# separate means an agent working on game code never has the gate's own files
# in scope, and `godot --path src` cannot import a tool script by accident.
ROOT = Path(__file__).resolve().parent
KIT_CONFIG_FILE = ROOT / "kit.config.json"
DEPENDENCY_LOCK_FILE = ROOT / "dependencies.lock.json"


def _load_portable_contracts() -> Tuple[Path, str, str, str, int]:
    """Resolve portable project facts from their two authoritative files."""
    errors: List[str] = []
    game_layout = "src"
    gdtoolkit_version = ""
    note_threshold = 10
    try:
        config = json.loads(KIT_CONFIG_FILE.read_text(encoding="utf-8"))
        if config.get("schema") != 1:
            errors.append("kit.config.json schema must be 1")
        candidate = config.get("game_root")
        if candidate not in (".", "src"):
            errors.append("kit.config.json game_root must be '.' or 'src'")
        else:
            game_layout = candidate
        runtime = config.get("runtime_root")
        runtime_path = Path(runtime) if isinstance(runtime, str) else Path(".")
        if (not isinstance(runtime, str) or runtime_path.is_absolute()
                or ".." in runtime_path.parts or runtime_path.as_posix() in ("", ".")):
            errors.append("kit.config.json runtime_root must be a private relative path")
        threshold = config.get("note_threshold", note_threshold)
        if (not isinstance(threshold, int) or isinstance(threshold, bool)
                or threshold <= 0):
            errors.append("kit.config.json note_threshold must be a positive integer")
        else:
            note_threshold = threshold
    except (OSError, ValueError, TypeError) as exc:
        errors.append(f"kit.config.json is unreadable: {exc}")
    try:
        lock = json.loads(DEPENDENCY_LOCK_FILE.read_text(encoding="utf-8"))
        if lock.get("schema") != 1:
            errors.append("dependencies.lock.json schema must be 1")
        value = lock.get("tools", {}).get("gdtoolkit", {}).get("version")
        if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
            errors.append("dependencies.lock.json must pin an exact gdtoolkit version")
        else:
            gdtoolkit_version = value
    except (OSError, ValueError, TypeError) as exc:
        errors.append(f"dependencies.lock.json is unreadable: {exc}")
    project_dir = (ROOT / game_layout).resolve()
    if not project_dir.is_relative_to(ROOT.resolve()):
        errors.append("configured game root escapes the kit root")
        project_dir = ROOT / "src"
    return (
        project_dir,
        game_layout,
        gdtoolkit_version,
        "; ".join(errors),
        note_threshold,
    )


(
    PROJECT_DIR,
    GAME_LAYOUT,
    GDTOOLKIT_VERSION,
    PORTABLE_CONTRACT_ERROR,
    RETRO_NOTE_THRESHOLD,
) = _load_portable_contracts()
LOG_DIR = ROOT / ".checklogs"
VERIFY_RUN_ID = os.environ.get("KIT_VERIFY_NONCE", "").strip()
VERIFY_RUN_ID_VALID = bool(
    not VERIFY_RUN_ID or re.fullmatch(r"[0-9a-f]{32}", VERIFY_RUN_ID)
)
VERIFY_AUTH_KEY = os.environ.get("KIT_VERIFY_AUTH_KEY", "").strip()
VERIFY_AUTH_KEY_VALID = bool(
    not VERIFY_RUN_ID or re.fullmatch(r"[0-9a-f]{64}", VERIFY_AUTH_KEY)
)
VERIFY_REPOSITORY_SHA256 = os.environ.get(
    "KIT_VERIFY_REPOSITORY_SHA256", ""
).strip()
VERIFY_REPOSITORY_SHA256_VALID = bool(
    not VERIFY_RUN_ID
    or re.fullmatch(r"[0-9a-f]{64}", VERIFY_REPOSITORY_SHA256)
)
EXPECTED_VERSION = "4.7.2"
STAGES = ["integrity", "skills", "schema", "shape", "design", "conformance", "format", "lint",
          "sanitise", "import", "typecheck", "grep", "types", "arch", "tests",
          "assets", "resources", "gut", "smoke"]
ENGINE_STAGES = ("import", "typecheck", "resources", "gut", "smoke")
STATIC_STAGES = tuple(stage for stage in STAGES if stage not in ENGINE_STAGES)
CONFORMANCE_COMPLETION_REQUIRED = True
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


def repository_game_relative(path: str) -> Optional[str]:
    """Map one Git-reported repository path into configured ``res://`` space."""
    rendered = path.strip().replace("\\", "/")
    first = rendered.split("/", 1)[0] if rendered else ""
    if (not rendered or rendered.startswith("/") or ".." in rendered.split("/")
            or ":" in first):
        return None
    if GAME_LAYOUT == ".":
        return rendered
    prefix = GAME_LAYOUT + "/"
    return rendered[len(prefix):] if rendered.startswith(prefix) else None

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
    r"|CRITICAL|Condition \".*\" is true|USER ERROR|NATIVE ENGINE CRASH"
    r"|ENGINE LAUNCH REFUSED|TIMEOUT after"
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
RUN_DIAGNOSTICS: Dict[str, List[dict]] = {
    "native_crashes": [],
    "engine_refusals": [],
    "engine_start_failures": [],
    "timeouts": [],
}


def _cap(text: str) -> str:
    if len(text) <= LOG_CAP_BYTES:
        return text
    keep = text[-LOG_CAP_BYTES:]
    dropped = len(text) - LOG_CAP_BYTES
    return (f"[log truncated: {dropped} bytes dropped from the head, "
            f"showing the last {LOG_CAP_BYTES} bytes]\n" + keep)


def run(
    cmd: Sequence[str], timeout: int, log: Path, *, native: bool = False
) -> Tuple[int, str]:
    """Run a command, capture combined output to `log`, never raise.

    stdin is closed. Godot's debugger prompts for input on a parse error; with
    an inherited stdin it reads garbage, resumes, breaks again, and spins until
    the timeout. DEVNULL turns that infinite loop into a clean immediate exit.
    """
    try:
        if native:
            if not cmd:
                raise OSError("native engine command is empty")
            from tools import native_engine

            result = native_engine.run_godot(
                str(cmd[0]),
                [str(value) for value in cmd[1:]],
                root=ROOT,
                cwd=PROJECT_DIR,
                timeout=timeout,
            )
            native_engine.persist_native_failure(
                ROOT,
                result,
                operation="verification",
                verification_run_id=VERIFY_RUN_ID or None,
            )
            out = result.output
            code = result.exit_code
            diagnostic = result.diagnostic()
            if result.failure_class == "native-crash":
                RUN_DIAGNOSTICS["native_crashes"].append(diagnostic)
            elif result.failure_class == "engine-timeout":
                RUN_DIAGNOSTICS["timeouts"].append(diagnostic)
            elif result.failure_class == "concurrent-engine-launch":
                RUN_DIAGNOSTICS["engine_refusals"].append(diagnostic)
            elif result.failure_class == "engine-start-failed":
                RUN_DIAGNOSTICS["engine_start_failures"].append(diagnostic)
        else:
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
    except (FileNotFoundError, PermissionError, OSError) as exc:
        out = f"executable not found: {cmd[0]}\n"
        if not isinstance(exc, FileNotFoundError):
            out = f"executable could not start: {Path(str(cmd[0])).name}: {type(exc).__name__}\n"
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
    names = (
        f"Godot_v{EXPECTED_VERSION}-stable_win64_console.exe",
        f"Godot_v{EXPECTED_VERSION}-stable_win64.exe",
        "godot",
        "godot4",
        "Godot",
        "godot.exe",
        "Godot.exe",
    )
    for name in names:
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


def is_authored_game_relative(relative: str) -> bool:
    """Use the same inverse authored-input policy as the living plan."""
    from tools import authored_scope

    is_kit_file = None
    if GAME_LAYOUT == ".":
        from tools import release as kit_release

        is_kit_file = kit_release.is_allowlisted
    return authored_scope.is_authored_game_file(
        relative,
        game_layout=GAME_LAYOUT,
        is_kit_file=is_kit_file,
    )


def authored_game_files() -> List[Path]:
    """Return every current authored game input, independent of file suffix."""
    out: List[Path] = []
    for path in sorted(PROJECT_DIR.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(PROJECT_DIR).as_posix()
        if is_authored_game_relative(relative):
            out.append(path)
    return out


def _private_gdtool(name: str) -> Optional[Path]:
    """Resolve the exact project-private tool installed by public setup."""
    if not GDTOOLKIT_VERSION:
        return None
    try:
        config = json.loads(KIT_CONFIG_FILE.read_text(encoding="utf-8"))
        runtime = Path(config["runtime_root"])
        if runtime.is_absolute() or ".." in runtime.parts:
            return None
        bindir = (ROOT / runtime / "tooling" / f"gdtoolkit-{GDTOOLKIT_VERSION}"
                  / ("Scripts" if os.name == "nt" else "bin"))
        candidate = bindir / (name + (".exe" if os.name == "nt" else ""))
        return candidate if candidate.is_file() else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def gdtool(name: str) -> Optional[List[str]]:
    """Resolve gdformat/gdlint.

    Order of preference:
      1. the exact project-private environment installed by public setup
      2. an exact-version binary on PATH
      3. an offline uv tool run of the exact version in dependencies.lock.json
      4. `python -m`, only when that Python has the exact locked package
         pip's Scripts directory is missing from PATH, which is common on Windows

    Verification never downloads implicitly. Every uv fallback is forced into
    offline mode; acquisition belongs to a separate, explicitly approved setup
    operation.
    """
    private = _private_gdtool(name)
    if private is not None:
        try:
            probe = subprocess.run(
                [str(private), "--version"], capture_output=True, text=True,
                timeout=30, check=False,
            )
            match = re.search(r"(?<![0-9])(\d+\.\d+\.\d+)(?![0-9])",
                              (probe.stdout or "") + (probe.stderr or ""))
            if (probe.returncode == 0 and match
                    and match.group(1) == GDTOOLKIT_VERSION):
                return [str(private)]
        except (OSError, subprocess.SubprocessError):
            pass

    found = shutil.which(name)
    if found:
        try:
            probe = subprocess.run(
                [found, "--version"], capture_output=True, text=True,
                timeout=30, check=False,
            )
            match = re.search(
                r"(?<![0-9])(\d+\.\d+\.\d+)(?![0-9])",
                (probe.stdout or "") + (probe.stderr or ""),
            )
            if (probe.returncode == 0 and match is not None
                    and match.group(1) == GDTOOLKIT_VERSION):
                return [found]
        except (OSError, subprocess.SubprocessError):
            pass

    if GDTOOLKIT_VERSION:
        runner = shutil.which("uvx")
        base = [runner] if runner else None
        if base is None and shutil.which("uv"):
            base = [shutil.which("uv"), "tool", "run"]
        if base:
            base.append("--offline")
            cmd = base + ["--from", f"gdtoolkit=={GDTOOLKIT_VERSION}", name]
            try:
                probe = subprocess.run(
                    cmd + ["--version"], stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=300, check=False)
                if probe.returncode == 0:
                    return cmd
            except (subprocess.TimeoutExpired, OSError):
                pass

    try:
        installed = importlib.metadata.version("gdtoolkit")
    except importlib.metadata.PackageNotFoundError:
        installed = ""
    if installed != GDTOOLKIT_VERSION:
        return None
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
        fh.write("# Regenerate deliberately: kit integrity accept\n")
        fh.write("\n".join(lines) + "\n")


def stage_integrity() -> None:
    head("gate integrity")
    if not MANIFEST_FILE.is_file():
        print(f"  {RED}MISSING   {MANIFEST_FILE.name}{RST}")
        print()
        print("  The shipped trust manifest is absent, so there is no trusted")
        print("  baseline against which to verify the gate. Restore it from the")
        print("  distribution or version control; never baseline unknown files.")
        RESULTS.fail("integrity (manifest missing)")
        return

    expected: Dict[str, str] = {}
    for line in MANIFEST_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            expected[parts[1].strip()] = parts[0]

    required = set(GATE_FILES + ADVISORY_FILES)
    unlisted = sorted(required - set(expected))
    if unlisted:
        for rel in unlisted:
            print(f"  {RED}UNLISTED  {rel}{RST}")
        print()
        print("  The shipped trust manifest is incomplete. Restore it from the")
        print("  distribution or version control; an incomplete baseline cannot")
        print("  establish that every gate file is unchanged.")
        RESULTS.fail("integrity (manifest incomplete)")
        return

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
    print("  Then either revert, or accept:  kit integrity accept")
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
    named `project_rule_test.gd` is not collected and produces NO error - the suite
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
                                f" -- run: kit verify --stage import")
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
    # GDScript run by the engine must live inside the configured game root,
    # because res:// cannot reach a sibling kit directory.
    script = PROJECT_DIR / "tools" / "validate_resources.gd"
    if not script.is_file():
        print("  res://tools/validate_resources.gd not present, stage disabled")
        RESULTS.skip("resources")
        return
    _, out = run(
        [godot, "--headless", "--path", str(PROJECT_DIR),
         "-s", "res://tools/validate_resources.gd"],
        120, LOG_DIR / "resources.log", native=True,
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
    if PORTABLE_CONTRACT_ERROR:
        print(f"  {RED}error: {PORTABLE_CONTRACT_ERROR}{RST}")
        RESULTS.fail("schema")
        return
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
        print(f"  {DIM}field list: kit schema describe proposal"
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
        print("  gdformat unavailable. Install the locked gdtoolkit version shown by: kit doctor")
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
        print("  fix with: kit setup format")
        RESULTS.fail("format")


def fix_format() -> int:
    """Format exactly the files the format stage checks, and nothing else."""
    head("fix-format (gdformat, project files only)")
    tool = gdtool("gdformat")
    if tool is None:
        print(f"  {RED}gdformat unavailable{RST}. Install the locked version shown by: kit doctor")
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
        print("  gdlint unavailable. Install the locked gdtoolkit version shown by: kit doctor")
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
        180, LOG_DIR / "import.log", native=True,
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
            60, LOG_DIR / "_tc_one.log", native=True,
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
    design_dir = ROOT / "docs" / "design"
    if not tool.is_file():
        print("  tools/design.py not found")
        RESULTS.skip("design")
        return
    code, out = run([sys.executable, str(tool), "--json"], 60, LOG_DIR / "design.log")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        if code != 0:
            print(indent(out or "(no output)"))
        print(f"  {RED}design.py produced unparseable output{RST}")
        RESULTS.fail("design (bad generator output)")
        return
    if code != 0:
        problems = data.get("metadata_errors") or []
        if problems:
            print(f"  {RED}design authority metadata is invalid:{RST}")
            for problem in problems:
                print(f"    {problem}")
        else:
            print(indent(out or "(no output)"))
        skill("godot-design-sections",
              "design authorship and authority must be explicit")
        RESULTS.fail("design (authority metadata)")
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
    for warning in data.get("metadata_warnings") or []:
        print(f"  {YEL}note: {warning}{RST}")
        print("        legacy design may be read, but it cannot silently grant"
              " implementation authority")

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
    tail = " | ".join(x for x in (who, when) if x)
    print(f"  {DIM}accepted by human: {why}{RST}")
    if tail:
        print(f"  {DIM}  ({tail}){RST}")


def proposal_approval_sha256(proposal: dict) -> str:
    """Bind every final proposal field except the digest's own slot."""
    material = dict(proposal)
    material.pop("approval_sha256", None)
    encoded = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _unbound_authored_changes() -> Tuple[Optional[List[str]], str]:
    """Return authored game changes at ``HEAD`` when no proposal exists.

    An absent proposal is a valid planning state only while there is no
    implementation to authorize.  Current files alone cannot answer that
    question: an adopted project may already contain a committed game, and a
    deletion is visible only through Git.  Compare the configured game root to
    the current commit and include untracked inputs using the same inverse
    authored-file policy as ordinary conformance.

    ``None`` means the comparison could not be proven.  A repository-free,
    empty game root is the one safe exception because there is no authored
    input on either side of a possible comparison.
    """
    current = {
        path.relative_to(PROJECT_DIR).as_posix()
        for path in authored_game_files()
    }
    if not shutil.which("git"):
        if not current and not (ROOT / ".git").exists():
            return [], ""
        return None, "Git is unavailable; authored changes cannot be compared to a baseline"

    verify_code, verified_head = run(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", "HEAD^{commit}"],
        30,
        LOG_DIR / "conformance.log",
    )
    verified_head = verified_head.strip().lower()
    if verify_code != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", verified_head):
        if not current and not (ROOT / ".git").exists():
            return [], ""
        return None, "the current Git baseline is unavailable"

    tracked_code, tracked = run(
        [
            "git",
            "-C",
            str(ROOT),
            "diff",
            "--name-only",
            verified_head,
            "--",
            GAME_LAYOUT,
        ],
        30,
        LOG_DIR / "conformance.log",
    )
    if tracked_code != 0:
        return None, "Git could not derive tracked authored changes"
    untracked_code, untracked = run(
        [
            "git",
            "-C",
            str(ROOT),
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            GAME_LAYOUT,
        ],
        30,
        LOG_DIR / "conformance.log",
    )
    if untracked_code != 0:
        return None, "Git could not derive untracked authored changes"

    changed: set[str] = set()
    for line in (tracked + "\n" + untracked).splitlines():
        relative = repository_game_relative(line)
        if relative is not None and is_authored_game_relative(relative):
            changed.add(relative)
    return sorted(changed), ""


def stage_conformance() -> None:
    """Diff the real tree against the approved proposal.

    An approval that lives only in a chat message evaporates when the turn
    ends: the human approves one structure, gets another, and nothing notices.
    proposal.json makes the approved shape durable so the diff is mechanical.

    Structural deviation reports rather than fails. Deviation while building
    is legitimate; missing design authority, a stale design digest, or crossing
    a go/no-go without approval is not. Depth follows involvement: a module-
    level human is not shown unproposed function signatures they never
    approved.
    """
    head("proposal conformance")
    path = ROOT / "proposal.json"
    level = involvement()
    if not path.is_file():
        changed, problem = _unbound_authored_changes()
        if changed is None:
            print(f"  {RED}{problem}{RST}")
            print("  Without proposal.json the gate must prove that no authored"
                  " game input changed; it cannot infer design authority.")
            RESULTS.fail("conformance (unbound scope unavailable)")
            return
        if changed:
            print(f"  {RED}authored game changes exist without proposal.json:{RST}")
            for relative in changed[:16]:
                print(f"    {relative}")
            if len(changed) > 16:
                print(f"    ... and {len(changed) - 16} more")
            print("  No proposal means no implementation authority. Retrieve or"
                  " write the relevant design, then record the reversible plan.")
            skill("godot-design-retrieval",
                  "authored work has no proposal or exact design ancestry")
            RESULTS.fail("conformance (implementation without design authority)")
            return
        print("  no proposal.json; no authored game changes")
        RESULTS.skip("conformance")
        return
    try:
        prop = json.loads(path.read_text(encoding="utf-8"))
        for key in ("scope", "modules", "files", "functions", "design_refs",
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
        return {str(i[field]).strip()
                for i in items if isinstance(i, dict) and i.get(field)}

    prop_mods = listed("modules", "path")
    prop_files = listed("files", "path")
    status = str(prop.get("status", "") or "").strip().lower()

    from tools import authored_scope, proposal_authority

    path_errors: List[str] = []
    seen_scope: set[Tuple[str, str]] = set()
    for item in (prop.get("scope") or []):
        if not isinstance(item, dict):
            continue
        raw = str(item.get("path", "") or "").strip()
        kind = str(item.get("kind", "") or "").strip()
        action = str(item.get("action", "") or "").strip().lower()
        normal = (
            authored_scope.normalize_directory(raw)
            if kind == "directory"
            else authored_scope.normalize_relative(raw)
            if kind == "file"
            else None
        )
        if normal != raw:
            path_errors.append(
                f"hands-off scope {raw!r}: expected one canonical {kind or 'file/directory'} path"
            )
        if kind not in ("file", "directory"):
            path_errors.append(
                f"hands-off scope {raw!r}: kind must be 'file' or 'directory'"
            )
        if action not in ("new", "modify", "delete"):
            path_errors.append(
                f"hands-off scope {raw!r}: action must be 'new', 'modify' or 'delete'"
            )
        identity = (kind, raw)
        if identity in seen_scope:
            path_errors.append(f"hands-off scope {raw!r}: duplicate boundary")
        seen_scope.add(identity)
    for item in (prop.get("modules") or []):
        if not isinstance(item, dict):
            continue
        raw = str(item.get("path", "") or "").strip()
        if authored_scope.normalize_directory(raw) != raw:
            path_errors.append(
                f"module {raw!r}: expected one canonical game-root-relative directory"
            )
        dependencies = item.get("may_depend_on")
        if not isinstance(dependencies, list):
            path_errors.append(f"module {raw!r}: may_depend_on must be a list")
        else:
            for dependency in dependencies:
                if (
                    not isinstance(dependency, str)
                    or authored_scope.normalize_directory(dependency) != dependency
                ):
                    path_errors.append(
                        f"module {raw!r}: invalid dependency path {dependency!r}"
                    )
        if not str(item.get("boundary_data", "") or "").strip():
            path_errors.append(
                f"module {raw!r}: boundary_data must name typed crossing data or say none"
            )
    for item in (prop.get("files") or []):
        if not isinstance(item, dict):
            continue
        raw = str(item.get("path", "") or "").strip()
        if (
            authored_scope.normalize_relative(raw) != raw
            or not is_authored_game_relative(raw)
        ):
            path_errors.append(
                f"file {raw!r}: expected one canonical authored game-root-relative path"
            )
    for item in (prop.get("functions") or []):
        if not isinstance(item, dict):
            continue
        raw = str(item.get("file", "") or "").strip()
        if (
            authored_scope.normalize_relative(raw) != raw
            or not raw.endswith(".gd")
            or not is_authored_game_relative(raw)
        ):
            path_errors.append(
                f"function file {raw!r}: expected one canonical authored .gd path"
            )
    if path_errors:
        print(f"  {RED}proposal paths or module boundaries are invalid:{RST}")
        for error in path_errors[:16]:
            print(f"    {error}")
        RESULTS.fail("conformance (invalid proposal scope)")
        return

    if status not in ("draft", "recorded", "approved"):
        print(f"  {RED}status '{status or '(missing)'}' is not 'draft',"
              f" 'recorded' or 'approved'{RST}")
        RESULTS.fail("conformance (bad status)")
        return

    # Experience before structure. Every other artefact in this repo describes
    # structure; nothing else captures intended feel. Without it, feel gets
    # inferred from the first plausible implementation. Structurally valid work
    # can still deliver the wrong interaction. Required at every level,
    # including hands-off, because the inference is just as wrong when nobody
    # is watching.
    exp = prop.get("experience")
    prop_scope = [
        item for item in (prop.get("scope") or []) if isinstance(item, dict)
    ]
    has_structure = bool(prop_scope or prop_mods or prop_files or prop.get("functions"))
    requires_authority = has_structure or status in ("recorded", "approved")
    if requires_authority:
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

    # Design ancestry. Every slice descends from design; a throwaway probe
    # descends from the written design question it exists to answer. A slice
    # with no pointer is work whose purpose lives only in a chat message.
    #
    # A dangling pointer is a hard failure -- a reference to a file that does
    # not exist is worse than no reference, because it reads as though the
    # rationale was written down. An unbound or out-of-date pointer is equally
    # unsafe: approval of one design must not survive a later design edit.
    # Missing ancestry is now blocking. The old acknowledgement escape hatch
    # remains parseable for migration, but it can never turn silence into
    # product authority.
    if requires_authority:
        refs = prop.get("design_refs")
        refs = refs if isinstance(refs, list) else []
        dangling: List[str] = []
        resolved: List[str] = []
        resolved_digests: Dict[str, str] = {}
        invalid_refs: List[str] = []
        parsed_designs: List[dict] = []
        design_root = (ROOT / "docs" / "design").resolve()
        from tools import design as design_contract

        for r in refs:
            if not isinstance(r, dict):
                continue
            try:
                sec, cand = proposal_authority.canonical_design_reference(
                    ROOT, r.get("section")
                )
            except proposal_authority.AuthorityError as exc:
                invalid_refs.append(str(exc))
                continue
            expected = str(r.get("sha256", "") or "").strip()
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                invalid_refs.append(
                    f"{sec}: sha256 must be a canonical lowercase 64-hex digest")
                continue
            try:
                actual = design_contract.design_sha256(
                    cand.read_text(encoding="utf-8")
                )
            except (OSError, UnicodeError) as exc:
                invalid_refs.append(f"{sec}: cannot read canonical design: {exc}")
                continue
            if actual != expected:
                invalid_refs.append(
                    f"{sec}: design changed after binding "
                    f"(proposal {expected[:12]}, current {actual[:12]})")
                continue
            try:
                parsed = design_contract.parse_design(cand)
            except (OSError, UnicodeError, ValueError) as exc:
                invalid_refs.append(f"{sec}: cannot parse design authority: {exc}")
                continue
            for problem in parsed.get("metadata_errors") or []:
                invalid_refs.append(f"{sec}: {problem}")
            if not parsed.get("implementation_eligible"):
                warnings = parsed.get("metadata_warnings") or []
                reason = "; ".join(str(item) for item in warnings)
                invalid_refs.append(
                    f"{sec}: design is not implementation-eligible"
                    + (f" ({reason})" if reason else "")
                )
                continue
            parsed_designs.append(parsed)
            resolved.append(sec)
            resolved_digests[sec] = expected
        if invalid_refs:
            print(f"  {RED}design_refs are not bound to the reviewed design:{RST}")
            for problem in invalid_refs:
                print(f"    {problem}")
            print("  Re-read the design, update its digest deliberately, and"
                  " re-evaluate the reversible envelope.")
            skill("godot-design-retrieval",
                  "implementation authority is bound to exact design content")
            RESULTS.fail("conformance (stale design_refs)")
            return
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
        if resolved:
            print(f"  design_refs: {len(resolved)} resolved")
        else:
            print(f"  {RED}no design_refs - implementation has no authority{RST}")
            old_ack = _acks(prop).get("no-design-refs")
            if old_ack:
                print(f"  {YEL}legacy acknowledgement found; it no longer authorizes"
                      f" implementation{RST}")
                _ack_line(old_ack)
            print("  Enter design discovery. Do not build from a plausible guess"
                  " about intent.")
            skill("godot-design-discovery",
                  "work with no stated end state cannot be implemented")
            RESULTS.fail("conformance (no design authority)")
            return

        authority = prop.get("design_authority")
        if not isinstance(authority, dict):
            print(f"  {RED}proposal has no design_authority object{RST}")
            print("  State whether the cited design is human-confirmed or an"
                  " agent-authored provisional inference.")
            RESULTS.fail("conformance (missing design authority)")
            return
        authority_kind = str(authority.get("authority", "") or "").strip()
        authored_by = str(authority.get("authored_by", "") or "").strip()
        confidence = str(authority.get("confidence", "") or "").strip()
        if authority_kind not in ("human-confirmed", "agent-provisional"):
            print(f"  {RED}design_authority.authority must be human-confirmed or"
                  f" agent-provisional{RST}")
            RESULTS.fail("conformance (bad design authority)")
            return
        if authored_by not in ("human", "agent"):
            print(f"  {RED}design_authority.authored_by must be human or agent{RST}")
            RESULTS.fail("conformance (bad design authorship)")
            return
        if confidence not in ("very-high", "high", "medium", "low"):
            print(f"  {RED}design_authority.confidence must be very-high, high,"
                  f" medium or low{RST}")
            RESULTS.fail("conformance (bad design confidence)")
            return
        if authority_kind == "agent-provisional" and (
                authored_by != "agent" or confidence != "very-high"):
            print(f"  {RED}agent-provisional implementation requires agent"
                  f" authorship and exact very-high confidence{RST}")
            print("  Lower confidence may be recorded as design evidence, but it"
                  " cannot authorize delivery.")
            RESULTS.fail("conformance (provisional design not eligible)")
            return

        actual_authorities = {
            str(item.get("authority", "")) for item in parsed_designs
        }
        expected_authority = (
            "agent-provisional"
            if "agent-provisional" in actual_authorities
            else "human-confirmed"
        )
        expected_author = (
            "agent"
            if any(item.get("authored_by") == "agent" for item in parsed_designs)
            else "human"
        )
        confidence_rank = {"low": 0, "medium": 1, "high": 2, "very-high": 3}
        relevant_confidence = [
            str(item.get("confidence", ""))
            for item in parsed_designs
            if expected_authority == "human-confirmed"
            or item.get("authority") == "agent-provisional"
        ]
        expected_confidence = min(
            relevant_confidence,
            key=lambda item: confidence_rank.get(item, -1),
            default="",
        )
        mismatches: List[str] = []
        if authority_kind != expected_authority:
            mismatches.append(
                f"authority says {authority_kind!r}, cited design requires"
                f" {expected_authority!r}"
            )
        if authored_by != expected_author:
            mismatches.append(
                f"authored_by says {authored_by!r}, cited design requires"
                f" {expected_author!r}"
            )
        if confidence != expected_confidence:
            mismatches.append(
                f"confidence says {confidence!r}, cited design requires"
                f" {expected_confidence!r}"
            )
        if mismatches:
            print(f"  {RED}proposal design_authority contradicts its cited"
                  f" design:{RST}")
            for mismatch in mismatches:
                print(f"    {mismatch}")
            RESULTS.fail("conformance (design authority mismatch)")
            return

        reversibility = prop.get("reversibility")
        if not isinstance(reversibility, dict):
            print(f"  {RED}proposal has no reversibility object{RST}")
            RESULTS.fail("conformance (missing reversible envelope)")
            return
        reversible_state = str(reversibility.get("state", "") or "").strip()
        if reversible_state not in ("reversible", "go-no-go"):
            print(f"  {RED}reversibility.state must be reversible or go-no-go{RST}")
            RESULTS.fail("conformance (bad reversible envelope)")
            return
        missing_boundary = [key for key in (
            "veto_scope", "hard_to_undo", "next_go_no_go"
        ) if not str(reversibility.get(key, "") or "").strip()]
        if missing_boundary:
            print(f"  {RED}reversibility is missing:"
                  f" {', '.join(missing_boundary)}{RST}")
            RESULTS.fail("conformance (incomplete reversible envelope)")
            return

        shape_path = ROOT / "project.shape.json"
        try:
            shape = json.loads(shape_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  {RED}cannot read the durable decision ledger: {exc}{RST}")
            RESULTS.fail("conformance (approval ledger unreadable)")
            return
        if not isinstance(shape, dict):
            print(f"  {RED}project.shape.json must contain an object{RST}")
            RESULTS.fail("conformance (approval ledger unreadable)")
            return
        try:
            rejection = proposal_authority.proposal_authority_rejection(
                ROOT, prop, shape
            )
        except proposal_authority.AuthorityError as exc:
            print(f"  {RED}cannot project current human authority: {exc}{RST}")
            RESULTS.fail("conformance (authority projection unavailable)")
            return
        if status in ("recorded", "approved") and rejection is not None:
            scope = str(rejection.get("authority_scope") or "design-and-plan")
            print(
                f"  {RED}human rejection is active for the current design intent "
                f"or exact plan{RST}"
            )
            print(f"    scope: {scope}; decision: {rejection.get('id', '(unidentified)')}")
            print("  Changing only status or approval metadata cannot resume rejected intent."
                  " Change the substantive design/plan or obtain a new exact cockpit confirmation.")
            RESULTS.fail("conformance (human rejection active)")
            return

        approved_by = str(prop.get("approved_by", "") or "").strip()
        approved_on = str(prop.get("approved_on", "") or "").strip()
        if status == "recorded":
            if level != "hands-off":
                print(f"  {RED}recorded status is only valid at hands-off"
                      f" involvement{RST}")
                RESULTS.fail("conformance (record used as approval)")
                return
            if reversible_state != "reversible":
                print(f"  {RED}hands-off work reached a go/no-go boundary{RST}")
                print("  Stop. A human must approve the exact bound design before"
                      " work can continue.")
                RESULTS.fail("conformance (go-no-go approval required)")
                return
            if approved_by or approved_on:
                print(f"  {RED}recorded work must not carry approval metadata{RST}")
                RESULTS.fail("conformance (record conflates approval)")
                return
        elif status == "approved":
            if not approved_by or not approved_on:
                print(f"  {RED}approved proposal must name who approved it and"
                      f" when{RST}")
                RESULTS.fail("conformance (approval attribution missing)")
                return
            if authority_kind != "human-confirmed":
                print(f"  {RED}approved work must bind human-confirmed design{RST}")
                RESULTS.fail("conformance (approval did not confirm design)")
                return
            try:
                exact_approval = proposal_authority.exact_approval_state(
                    ROOT, prop, shape
                )
            except proposal_authority.AuthorityError as exc:
                print(f"  {RED}cannot project exact approval: {exc}{RST}")
                RESULTS.fail("conformance (approval contract unbound)")
                return
            if not exact_approval["approved"]:
                print(f"  {RED}stored approved status is not exact authority evidence:{RST}")
                for reason in exact_approval["reasons"][:12]:
                    print(f"    {reason}")
                RESULTS.fail("conformance (stale plan approval)")
                return

    # A draft is a proposal awaiting review, never implementation authority.
    # It blocks completion at every involvement level. Hands-off autonomy uses
    # `recorded` only while the declared envelope remains reversible.
    if status == "draft":
        n = len(prop_scope) + len(prop_mods) + len(prop_files) + len(prop.get("functions") or [])
        print(f"  {RED}proposal is a DRAFT awaiting review"
              f" ({n} item(s)){RST}")
        print("  Open the cockpit. Confirm, veto or request changes before any"
              " work crosses this boundary.")
        write_plan()
        RESULTS.fail("conformance (decision required)")
        return

    if level == "":
        print(f"  {RED}project involvement is unset{RST}")
        RESULTS.fail("conformance (involvement unset)")
        return

    if not has_structure:
        print(f"  {RED}proposal.json grants implementation status but declares"
              f" no structure{RST}")
        print("  Record at least the modules, files or functions this slice"
              " changes. Empty authority cannot be conformed to real work.")
        skill("godot-human-involvement", "implementation scope must be reviewable")
        write_plan()
        RESULTS.fail("conformance (empty implementation scope)")
        return

    depth_missing = (
        "at least one coarse scope boundary" if level == "hands-off" and not prop_scope
        else "module declarations" if level == "module" and not prop_mods
        else "module and file declarations" if level == "file" and (not prop_mods or not prop_files)
        else "module, file and function declarations"
        if level == "function" and (
            not prop_mods or not prop_files or not (prop.get("functions") or [])
        )
        else ""
    )
    if depth_missing:
        print(f"  {RED}{level} involvement requires {depth_missing}{RST}")
        print("  Record the contract at exactly the configured involvement depth;")
        print("  a coarser proposal cannot prove that the work stayed inside it.")
        RESULTS.fail("conformance (involvement scope incomplete)")
        return

    # Actual state, relative to the configured res:// root.
    current_files = {
        str(path.relative_to(PROJECT_DIR)).replace("\\", "/")
        for path in authored_game_files()
    }

    # A per-slice proposal cannot claim files that predate it. Scope the diff to
    # what changed since approval, using the git sha recorded at approval time.
    # Without a baseline the diff covers the whole tree and says so, because a
    # silently over-broad diff trains the agent to ignore this stage.
    baseline = str(prop.get("baseline_sha", "") or "").strip()
    if not baseline:
        print(f"  {RED}proposal has no baseline_sha{RST}")
        print("  Bind the slice to the repository state it started from; an"
              " unscoped comparison cannot support approval.")
        RESULTS.fail("conformance (missing baseline)")
        return
    if not re.fullmatch(r"[0-9a-f]{40,64}", baseline):
        print(f"  {RED}baseline_sha is not an exact lowercase commit id{RST}")
        RESULTS.fail("conformance (invalid baseline)")
        return
    if not shutil.which("git"):
        print(f"  {RED}Git is unavailable; changed-file scope cannot be proven{RST}")
        RESULTS.fail("conformance (git unavailable)")
        return
    verify_code, verified_baseline = run(
        ["git", "-C", str(ROOT), "rev-parse", "--verify", f"{baseline}^{{commit}}"],
        30,
        LOG_DIR / "conformance.log",
    )
    verified_baseline = verified_baseline.strip().lower()
    if verify_code != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", verified_baseline):
        print(f"  {RED}baseline_sha {baseline[:12]!r} is not a commit in this"
              f" repository{RST}")
        RESULTS.fail("conformance (baseline unavailable)")
        return
    pathspec = GAME_LAYOUT
    code, out = run(
        ["git", "-C", str(ROOT), "diff", "--name-only", verified_baseline,
         "--", pathspec],
        30,
        LOG_DIR / "conformance.log",
    )
    if code != 0:
        print(f"  {RED}Git could not derive tracked changes from the baseline{RST}")
        RESULTS.fail("conformance (tracked scope unavailable)")
        return
    untracked_code, untracked = run(
        ["git", "-C", str(ROOT), "ls-files", "--others", "--exclude-standard",
         "--", pathspec],
        30,
        LOG_DIR / "conformance.log",
    )
    if untracked_code != 0:
        print(f"  {RED}Git could not derive untracked changes; scope would be"
              f" incomplete{RST}")
        RESULTS.fail("conformance (untracked scope unavailable)")
        return
    changed: set[str] = set()
    for line in (out + "\n" + untracked).splitlines():
        relative = repository_game_relative(line)
        if relative is not None and is_authored_game_relative(relative):
            changed.add(relative)
    real_files = changed
    print(f"  {DIM}scoped to {len(real_files)} file(s) changed since"
          f" {verified_baseline[:8]}{RST}")

    # Each file declares create or modify. Nothing checked it, so `create` on a
    # file that already existed slipped through twice and a human caught it both
    # times. It is a two-line comparison against the tree at the baseline, and a
    # wrong action means the agent has not looked at what is already there --
    # which is exactly when extend-before-create gets skipped.
    action_wrong: List[str] = []
    code, listing = run(
        ["git", "-C", str(ROOT), "ls-tree", "-r", "--name-only",
         verified_baseline, "--", GAME_LAYOUT],
        30,
        LOG_DIR / "conformance.log",
    )
    if code != 0:
        print(f"  {RED}Git could not read the baseline tree; create/modify"
              f" declarations cannot be proven{RST}")
        RESULTS.fail("conformance (baseline tree unavailable)")
        return
    at_baseline: set[str] = set()
    for line in listing.splitlines():
        relative = repository_game_relative(line)
        if relative is not None and is_authored_game_relative(relative):
            at_baseline.add(relative)

    def scope_contains(boundary: dict, relative: str) -> bool:
        raw = str(boundary.get("path") or "")
        if boundary.get("kind") == "file":
            return relative == raw
        if raw == "(root)":
            return True
        return relative == raw or relative.startswith(raw.rstrip("/") + "/")

    def scope_specificity(boundary: dict) -> Tuple[int, int]:
        raw = str(boundary.get("path") or "")
        if boundary.get("kind") == "file":
            return (2, len(raw.split("/")))
        if raw == "(root)":
            return (0, 0)
        return (1, len(raw.split("/")))

    def changed_action(relative: str) -> str:
        existed = relative in at_baseline
        exists_now = relative in current_files
        if not existed and exists_now:
            return "new"
        if existed and not exists_now:
            return "delete"
        return "modify"

    scope_unbuilt: List[str] = []
    unscoped_hands_off: List[str] = []
    if level == "hands-off":
        claimed: Dict[int, List[str]] = {
            index: [] for index, _boundary in enumerate(prop_scope)
        }
        for relative in sorted(real_files):
            matching = [
                (index, boundary)
                for index, boundary in enumerate(prop_scope)
                if scope_contains(boundary, relative)
            ]
            if not matching:
                unscoped_hands_off.append(relative)
                continue
            owner_index, owner = max(
                matching, key=lambda item: scope_specificity(item[1])
            )
            actual = changed_action(relative)
            declared = str(owner.get("action") or "").strip().lower()
            if declared != actual:
                action_wrong.append(
                    f"{relative}: hands-off scope declares {declared!r} but the "
                    f"observed action is {actual!r}"
                )
                continue
            claimed[owner_index].append(relative)
        for index, boundary in enumerate(prop_scope):
            if not claimed.get(index):
                scope_unbuilt.append(
                    f"{boundary.get('kind')} {boundary.get('path')} "
                    f"({boundary.get('action')})"
                )
    for item in (prop.get("files") or []):
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path", "") or "").strip()
        act = str(item.get("action", "") or "").strip().lower()
        if not rel:
            continue
        # Skipping a missing action meant this whole check never ran: two real
        # proposals omitted it, so "modify" vanished and nothing was compared.
        if not act:
            action_wrong.append(
                f"{rel}: no action declared (expected 'new', 'modify' or 'delete')")
            continue
        if act not in ("new", "modify", "delete"):
            action_wrong.append(
                f"{rel}: action {act!r} is not 'new', 'modify' or 'delete'"
            )
            continue
        existed = rel in at_baseline
        exists_now = rel in current_files
        changed_now = rel in changed
        if act == "new" and existed:
            action_wrong.append(f"{rel}: marked 'new' but it already existed")
        elif act in ("modify", "delete") and not existed:
            action_wrong.append(
                f"{rel}: marked '{act}' but it did not exist at the baseline"
            )
        elif changed_now and act == "new" and not exists_now:
            action_wrong.append(f"{rel}: marked 'new' but no current file exists")
        elif changed_now and act == "modify" and not exists_now:
            action_wrong.append(
                f"{rel}: was deleted but is marked 'modify' instead of 'delete'"
            )
        elif changed_now and act == "delete" and exists_now:
            action_wrong.append(
                f"{rel}: still exists but is marked 'delete'"
            )

    depth = arch_depth()
    def module_for(relative: str) -> str:
        parent = str(Path(relative).parent).replace("\\", "/")
        if parent in ("", "."):
            return "(root)"
        return "/".join(parent.split("/")[:depth])

    for key, field in (("files", "path"), ("functions", "file")):
        for item in (prop.get(key) or []):
            if not isinstance(item, dict) or not item.get(field):
                continue
            declared_module = str(item.get("module", "") or "").strip()
            if not declared_module:
                continue
            expected_module = module_for(str(item[field]).strip())
            if declared_module != expected_module:
                action_wrong.append(
                    f"{item[field]}: module {declared_module!r} does not match"
                    f" path-derived {expected_module!r}"
                )

    # A module is a DIRECTORY. Truncating a file path to `depth` segments turns
    # scripts/main.gd into a phantom module, so derive from the parent.
    def modules_for(files: set[str]) -> set[str]:
        modules: set[str] = set()
        for rel in files:
            modules.add(module_for(rel))
        return modules

    real_mods = modules_for(real_files)
    current_mods = modules_for(current_files)
    baseline_mods = modules_for(at_baseline)
    for item in (prop.get("modules") or []):
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path", "") or "").strip()
        act = str(item.get("action", "") or "").strip().lower()
        if not rel:
            continue
        if act not in ("new", "modify", "delete"):
            action_wrong.append(
                f"{rel}/: action {act!r} is not 'new', 'modify' or 'delete'"
            )
            continue
        existed = rel in baseline_mods
        exists_now = rel in current_mods
        changed_now = rel in real_mods
        if act == "new" and existed:
            action_wrong.append(f"{rel}/: marked 'new' but the module already existed")
        elif act in ("modify", "delete") and not existed:
            action_wrong.append(
                f"{rel}/: marked '{act}' but the module did not exist at the baseline"
            )
        elif changed_now and act == "new" and not exists_now:
            action_wrong.append(f"{rel}/: marked 'new' but no current module exists")
        elif changed_now and act == "modify" and not exists_now:
            action_wrong.append(
                f"{rel}/: was deleted but is marked 'modify' instead of 'delete'"
            )
        elif changed_now and act == "delete" and exists_now:
            action_wrong.append(f"{rel}/: still exists but is marked 'delete'")

    graph_code, graph_output = run(
        [sys.executable, str(ROOT / "arch.py"), "--json"],
        60,
        LOG_DIR / "conformance-architecture.log",
    )
    try:
        graph = json.loads(graph_output)
    except json.JSONDecodeError:
        graph = None
    graph_modules = graph.get("modules") if isinstance(graph, dict) else None
    if graph_code not in (0, 1) or not isinstance(graph_modules, dict):
        print(f"  {RED}the current module dependency graph is unavailable{RST}")
        print("  Proposal boundaries cannot be compared without arch.py --json.")
        RESULTS.fail("conformance (module graph unavailable)")
        return
    dependency_wrong: List[str] = []
    for item in (prop.get("modules") or []):
        if not isinstance(item, dict):
            continue
        module = str(item.get("path", "") or "").strip()
        allowed_raw = item.get("may_depend_on")
        allowed = {
            str(value).strip()
            for value in allowed_raw
            if isinstance(value, str) and value.strip()
        } if isinstance(allowed_raw, list) else set()
        actual_raw = graph_modules.get(module, [])
        if not isinstance(actual_raw, list):
            dependency_wrong.append(
                f"{module}/: architecture graph returned a malformed dependency list"
            )
            continue
        actual = {str(value) for value in actual_raw}
        for dependency in sorted(actual - allowed):
            dependency_wrong.append(
                f"{module}/: depends on unapproved module {dependency!r}"
            )
    action_wrong.extend(dependency_wrong)

    function_entries = [
        item for item in (prop.get("functions") or [])
        if isinstance(item, dict)
    ]
    function_unbuilt: List[str] = []
    if level == "function" or function_entries:
        from tools import gd_signature

        proposed_functions: Dict[Tuple[str, str], dict] = {}
        for item in function_entries:
            relative = str(item.get("file", "") or "").strip()
            signature = str(item.get("signature", "") or "").strip()
            class_scope = str(item.get("class_scope", "") or "").strip()
            action = str(item.get("action", "") or "").strip().lower()
            try:
                parsed_signature = gd_signature.parse_proposal_signature(signature)
            except gd_signature.SignatureError as exc:
                action_wrong.append(
                    f"{relative or '(missing file)'}: invalid function signature: {exc}"
                )
                continue
            if signature != parsed_signature.canonical:
                action_wrong.append(
                    f"{relative}::{parsed_signature.name}: signature is not canonical; "
                    f"use {parsed_signature.canonical!r}"
                )
            if action not in ("new", "modify", "delete"):
                action_wrong.append(
                    f"{relative}::{parsed_signature.name}: action {action!r} is not "
                    "'new', 'modify' or 'delete'"
                )
            source_identity = (
                f"{class_scope}.{parsed_signature.name}"
                if class_scope else parsed_signature.name
            )
            identity = (relative, source_identity)
            if identity in proposed_functions:
                action_wrong.append(
                    f"{relative}::{source_identity}: duplicate function proposal"
                )
            proposed_functions[identity] = {
                "action": action,
                "signature": parsed_signature.canonical,
            }

        function_files = {relative for relative, _name in proposed_functions}
        if level == "function":
            function_files.update(
                relative for relative in changed if relative.endswith(".gd")
            )
        source_maps: Dict[
            str,
            Tuple[
                Dict[str, gd_signature.SourceFunction],
                Dict[str, gd_signature.SourceFunction],
            ],
        ] = {}
        function_source_error = ""
        for relative in sorted(function_files):
            before_functions: Dict[str, gd_signature.SourceFunction] = {}
            current_functions: Dict[str, gd_signature.SourceFunction] = {}
            if relative in at_baseline:
                repository_path = (
                    relative if GAME_LAYOUT == "." else f"{GAME_LAYOUT}/{relative}"
                )
                show_code, baseline_source = run(
                    [
                        "git",
                        "-C",
                        str(ROOT),
                        "show",
                        f"{verified_baseline}:{repository_path}",
                    ],
                    30,
                    LOG_DIR / "conformance-functions.log",
                )
                if show_code != 0:
                    function_source_error = (
                        f"Git could not read baseline function source for {relative}"
                    )
                    break
                try:
                    before_functions = gd_signature.parse_source_functions(
                        baseline_source
                    )
                except gd_signature.SignatureError as exc:
                    function_source_error = (
                        f"baseline {relative} has an unrepresentable function: {exc}"
                    )
                    break
            if relative in current_files:
                try:
                    current_source = (PROJECT_DIR / relative).read_text(
                        encoding="utf-8"
                    )
                    current_functions = gd_signature.parse_source_functions(
                        current_source
                    )
                except (OSError, UnicodeError, gd_signature.SignatureError) as exc:
                    function_source_error = (
                        f"current {relative} has an unrepresentable function: {exc}"
                    )
                    break
            source_maps[relative] = (before_functions, current_functions)
        if function_source_error:
            print(f"  {RED}{function_source_error}{RST}")
            print("  Function-level conformance fails closed rather than omitting it.")
            RESULTS.fail("conformance (function source unavailable)")
            return

        actual_function_changes: Dict[Tuple[str, str], dict] = {}
        for relative, (before_functions, current_functions) in source_maps.items():
            for name in sorted(set(before_functions) | set(current_functions)):
                before_function = before_functions.get(name)
                current_function = current_functions.get(name)
                if before_function is None and current_function is not None:
                    actual_function_changes[(relative, name)] = {
                        "action": "new",
                        "signature": current_function.signature,
                    }
                elif before_function is not None and current_function is None:
                    actual_function_changes[(relative, name)] = {
                        "action": "delete",
                        "signature": before_function.signature,
                    }
                elif (
                    before_function is not None
                    and current_function is not None
                    and (
                        before_function.signature != current_function.signature
                        or before_function.body_sha256 != current_function.body_sha256
                    )
                ):
                    actual_function_changes[(relative, name)] = {
                        "action": "modify",
                        "signature": current_function.signature,
                    }

        for identity, declared in proposed_functions.items():
            relative, name = identity
            before_functions, current_functions = source_maps.get(
                relative, ({}, {})
            )
            before_function = before_functions.get(name)
            current_function = current_functions.get(name)
            action = str(declared["action"])
            signature = str(declared["signature"])
            if action == "new":
                if before_function is not None:
                    action_wrong.append(
                        f"{relative}::{name}: marked 'new' but existed at baseline"
                    )
                elif current_function is None:
                    function_unbuilt.append(f"{relative}::{name}")
                elif current_function.signature != signature:
                    action_wrong.append(
                        f"{relative}::{name}: implemented signature "
                        f"{current_function.signature!r}, proposed {signature!r}"
                    )
            elif action == "modify":
                if before_function is None:
                    action_wrong.append(
                        f"{relative}::{name}: marked 'modify' but did not exist at baseline"
                    )
                elif current_function is None:
                    action_wrong.append(
                        f"{relative}::{name}: was deleted but is marked 'modify'"
                    )
                elif current_function.signature != signature:
                    action_wrong.append(
                        f"{relative}::{name}: implemented signature "
                        f"{current_function.signature!r}, proposed {signature!r}"
                    )
                elif identity not in actual_function_changes:
                    function_unbuilt.append(f"{relative}::{name}")
            elif action == "delete":
                if before_function is None:
                    action_wrong.append(
                        f"{relative}::{name}: marked 'delete' but did not exist at baseline"
                    )
                elif before_function.signature != signature:
                    action_wrong.append(
                        f"{relative}::{name}: deletion must cite baseline signature "
                        f"{before_function.signature!r}"
                    )
                elif current_function is not None:
                    function_unbuilt.append(f"{relative}::{name}")

        if level == "function":
            for identity, actual in actual_function_changes.items():
                declared = proposed_functions.get(identity)
                if declared is None:
                    action_wrong.append(
                        f"{identity[0]}::{identity[1]}: changed as "
                        f"{actual['action']} with unproposed signature "
                        f"{actual['signature']!r}"
                    )
                elif (
                    declared.get("action") != actual.get("action")
                    or declared.get("signature") != actual.get("signature")
                ):
                    action_wrong.append(
                        f"{identity[0]}::{identity[1]}: actual "
                        f"{actual['action']} {actual['signature']!r} does not match "
                        f"proposal {declared.get('action')} "
                        f"{declared.get('signature')!r}"
                    )

    notes = 0
    if action_wrong:
        notes += 1
        print(f"  {YEL}wrong action on {len(action_wrong)} scope item(s):{RST}")
        for line in action_wrong[:8]:
            print(f"    {line}")
        print("  Check what exists before proposing. Extending beats creating a"
              " second copy beside it.")

    if unscoped_hands_off:
        notes += 1
        print(f"  {YEL}authored changes outside the recorded hands-off scope:{RST}")
        for relative in unscoped_hands_off[:12]:
            print(f"    {relative}")
    unproposed_mods = (
        sorted(real_mods - prop_mods)
        if level in ("module", "file", "function") else []
    )
    if unproposed_mods:
        notes += 1
        print(f"  {YEL}module(s) in code but not in the proposal:{RST}")
        for m in unproposed_mods[:8]:
            print(f"    {m}")
    unbuilt_mods = (
        sorted(prop_mods - real_mods)
        if level in ("module", "file", "function") else []
    )
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
    if function_unbuilt:
        print(
            f"  {DIM}proposed, not yet built: {len(function_unbuilt)} "
            f"function(s){RST}"
        )

    incomplete = [
        *(f"scope {item}" for item in scope_unbuilt),
        *(f"module {item}" for item in unbuilt_mods),
        *(f"function {item}" for item in function_unbuilt),
    ]
    if level in ("file", "function"):
        incomplete.extend(f"file {item}" for item in sorted(prop_files - real_files))
    if incomplete and CONFORMANCE_COMPLETION_REQUIRED:
        print(f"  {RED}declared implementation is not fully observed:{RST}")
        for item in incomplete[:16]:
            print(f"    {item}")
        print("  Full and strict verification are completion proof; they cannot pass"
              " while approved or recorded work is still only planned.")
        write_plan()
        RESULTS.fail("conformance (implementation incomplete)")
        return
    if incomplete:
        print(f"  {DIM}static planning proof: {len(incomplete)} declared item(s)"
              f" remain unbuilt{RST}")

    if notes:
        if status == "recorded":
            print("  A deviation may be legitimate, but it is not inside the"
                  " recorded envelope yet. Revise proposal.json and re-evaluate"
                  " whether the work remains reversible.")
        else:
            print(f"  A deviation may be legitimate, but the human did not"
                  f" approve it. Revise proposal.json and re-approve at"
                  f" '{level}'.")
        skill(
            "godot-human-involvement",
            "the approved or recorded structure changed",
        )
        write_plan()
        RESULTS.fail("conformance (unrecorded deviation)")
        return
    else:
        contract_label = (
            "recorded reversible plan" if status == "recorded"
            else "approved proposal"
        )
        print(f"  matches the {contract_label} ({len(prop_mods)} module(s)"
              + (f", {len(prop_files)} file(s)" if level in ("file", "function") else "")
              + (f", {len(function_entries)} function(s)" if level == "function" else "")
              + ")")
    write_plan()
    RESULTS.passed("conformance")


def stage_gut(godot: str) -> None:
    head("unit tests (GUT)")
    if not (PROJECT_DIR / "addons" / "gut" / "gut_cmdln.gd").is_file():
        print("  GUT not installed. Run explicitly: kit setup dependency gut")
        RESULTS.skip("gut")
        return
    code, out = run(
        [godot, "--headless", "--path", str(PROJECT_DIR), "-d",
         "-s", "res://addons/gut/gut_cmdln.gd",
         "-gconfig=res://.gutconfig.json", "-gexit"],
        60, LOG_DIR / "gut.log", native=True,
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
        120, LOG_DIR / "smoke.log", native=True,
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
    threshold = RETRO_NOTE_THRESHOLD
    if n >= threshold:
        print(f"{YEL}retrospective due{RST} {DIM}({n} unarchived slice note(s),"
              f" threshold {threshold}){RST}")
        print(f"{DIM}  kit retro run{RST}")


def finish() -> int:
    """Print the one authoritative summary after a bounded verification run."""
    summary = {
        "schema": 2,
        "run_id": VERIFY_RUN_ID or None,
        "repository_sha256": VERIFY_REPOSITORY_SHA256 or None,
        "failed": RESULTS.failed,
        "results": [ANSI_RE.sub("", line) for line in RESULTS.lines],
        "diagnostics": RUN_DIAGNOSTICS,
    }
    diagnostics_persisted = False
    try:
        if not VERIFY_RUN_ID_VALID:
            raise OSError("KIT_VERIFY_NONCE is malformed")
        if not VERIFY_AUTH_KEY_VALID:
            raise OSError("KIT_VERIFY_AUTH_KEY is missing or malformed")
        if not VERIFY_REPOSITORY_SHA256_VALID:
            raise OSError("KIT_VERIFY_REPOSITORY_SHA256 is missing or malformed")
        if VERIFY_RUN_ID:
            canonical = json.dumps(
                summary,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            summary["auth_sha256"] = hmac.new(
                bytes.fromhex(VERIFY_AUTH_KEY), canonical, hashlib.sha256
            ).hexdigest()
        else:
            summary["auth_sha256"] = None
        suffix = f"-{VERIFY_RUN_ID}" if VERIFY_RUN_ID else ""
        summary_path = LOG_DIR / f"run-summary{suffix}.json"
        temporary = summary_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(summary, indent=2) + "\n",
            encoding="utf-8",
            newline="",
        )
        os.replace(temporary, summary_path)
        diagnostics_persisted = True
    except OSError as exc:
        print(f"{RED}error: could not persist gate run diagnostics: {exc}{RST}")
    print(f"\n{DIM}================ summary ================{RST}")
    for line in RESULTS.lines:
        print(line)
    print(f"{DIM}logs: {LOG_DIR}{RST}")
    if RESULTS.failed or not diagnostics_persisted:
        print(f"{RED}GATE FAILED{RST}")
        return 1
    print(f"{GRN}GATE PASSED{RST}")
    retro_nudge()
    return 0


def main() -> int:
    global CONFORMANCE_COMPLETION_REQUIRED
    parser = argparse.ArgumentParser(
        description="Verification gate for a portable Godot project.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="stages: " + " ".join(STAGES),
    )
    parser.add_argument("--only", action="append", default=[],
                        help="run only this stage (repeatable or comma-separated)")
    parser.add_argument("--fast", action="store_true",
                        help="skip import; use when only .gd files changed")
    parser.add_argument("--static", action="store_true",
                        help="run every non-engine stage without discovering or launching Godot")
    parser.add_argument("--list", action="store_true", help="list stages and exit")
    parser.add_argument("--fix-format", action="store_true",
                        help="run gdformat over project .gd files (addons excluded)")
    parser.add_argument("--accept-gate-changes", action="store_true",
                        help="re-baseline the integrity manifest (HUMAN ONLY)")
    args = parser.parse_args()
    CONFORMANCE_COMPLETION_REQUIRED = not args.static

    for diagnostic_items in RUN_DIAGNOSTICS.values():
        diagnostic_items.clear()

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
    if args.static and (selected or args.fast):
        parser.error("--static cannot be combined with --only or --fast")
    unknown = [s for s in selected if s not in STAGES]
    if unknown:
        print(f"unknown stage(s): {', '.join(unknown)}")
        print(f"valid stages: {' '.join(STAGES)}")
        return 2
    active = list(STATIC_STAGES) if args.static else (selected or list(STAGES))

    LOG_DIR.mkdir(exist_ok=True)

    head("prerequisites")
    print(f"python: {sys.version.split()[0]} on {sys.platform}")
    needs_godot = any(stage in active for stage in ENGINE_STAGES)
    if not needs_godot:
        print("godot:  not discovered or launched (no selected stage needs it)")

    # Integrity is a trust boundary, not merely another diagnostic. If it
    # fails, stop before importing tools, scanning the project, or touching the
    # native engine. The human must first review the protected-file diff.
    if "integrity" in active:
        stage_integrity()
        if RESULTS.failed:
            for stage in active:
                if stage != "integrity":
                    RESULTS.skip(f"{stage} (skipped: integrity review required)")
            return finish()

    # Every process-light stage completes before the native-engine boundary.
    # This both gives useful kit evidence without Godot and ensures a static
    # failure cannot cascade into one or more native crash dialogs.
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

    if needs_godot:
        if RESULTS.failed:
            head("engine boundary")
            print("godot:  not discovered or launched because static verification failed")
            for stage in ENGINE_STAGES:
                if stage in active:
                    RESULTS.skip(f"{stage} (skipped: static verification failed)")
        elif os.environ.get("KIT_ENGINE_DISABLED") == "1":
            head("engine boundary")
            print("godot:  not discovered or launched; native engine disabled by caller")
            RESULTS.fail("engine-boundary (native engine disabled by caller)")
            for stage in ENGINE_STAGES:
                if stage in active:
                    RESULTS.skip(f"{stage} (skipped: native engine disabled by caller)")
        else:
            head("engine boundary")
            godot = find_godot()
            if godot is None:
                print(f"{RED}godot not on PATH{RST}")
                print("Set GODOT_BIN to the executable, or run: kit doctor")
                return 2
            print(f"godot:  {godot}")
            code, version_output = run(
                [godot, "--headless", "--version"], 30,
                LOG_DIR / "version.log", native=True
            )
            version_errors = scan(version_output)
            version_line = (
                version_output.strip().splitlines()[-1]
                if version_output.strip() else "no version output"
            )
            if code != 0 or version_errors:
                print(f"{RED}engine health check failed{RST}: {version_line}")
                indent("\n".join(version_errors), 12)
                RESULTS.fail("engine-health")
            else:
                print(f"version: {version_line}")
                match = re.search(
                    r"(?<![0-9])(\d+\.\d+\.\d+)(?![0-9])",
                    version_line,
                )
                reported_version = match.group(1) if match else "unknown"
                if reported_version != EXPECTED_VERSION:
                    print(
                        f"{RED}unsupported engine version {reported_version}; "
                        f"expected exactly {EXPECTED_VERSION}{RST}"
                    )
                    RESULTS.fail("engine-version")

            engine_functions = {
                "import": stage_import,
                "typecheck": stage_typecheck,
                "resources": stage_resources,
                "gut": stage_gut,
                "smoke": stage_smoke,
            }
            engine_blocked = RESULTS.failed
            for stage in ENGINE_STAGES:
                if stage not in active:
                    continue
                if stage == "import" and args.fast:
                    RESULTS.skip("import (--fast)")
                    continue
                if engine_blocked:
                    RESULTS.skip(
                        f"{stage} (skipped: previous native-engine check failed)"
                    )
                    continue
                engine_functions[stage](godot)
                engine_blocked = RESULTS.failed

    return finish()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)
