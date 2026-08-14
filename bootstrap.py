#!/usr/bin/env python3
"""bootstrap.py - one-time setup detector and automator.

Run by the coding agent, not usually by hand. It answers two questions:

    python bootstrap.py            what state is this machine in?
    python bootstrap.py --fix      do everything that can be done unattended

Standard library only, so it runs before anything is installed.
Every check is independent, so a partial environment reports precisely
which parts are missing rather than failing at the first gap.

Exit codes:
    0  all checks pass, setup complete
    1  one or more checks incomplete (normal before setup)
    2  bad usage
"""

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

INVOLVEMENT_LEVELS = ("hands-off", "module", "file", "function")

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
EXPECTED_GODOT = "4.7"          # major.minor, patch not enforced
GUT_VERSION = "9.7.1"
GUT_URL = f"https://github.com/bitwes/Gut/archive/refs/tags/v{GUT_VERSION}.zip"
MERMAID_VERSION = "11"
MERMAID_URL = (f"https://cdn.jsdelivr.net/npm/mermaid@{MERMAID_VERSION}"
               "/dist/mermaid.min.js")

WIN = platform.system() == "Windows"

if WIN:
    os.system("")               # enable ANSI on Windows consoles

_TTY = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
GRN = "\033[32m" if _TTY else ""
RED = "\033[31m" if _TTY else ""
YEL = "\033[33m" if _TTY else ""
DIM = "\033[2m" if _TTY else ""
RST = "\033[0m" if _TTY else ""

OK, MISSING, MANUAL = "OK", "MISSING", "MANUAL"
QUIET = "--json" in sys.argv

results = []


def record(name, state, detail, remedy="", advisory=False):
    """Log one check.

    advisory=True means "report this, never block on it". Reserved for things
    the gate cannot own: a file another tool rewrites at will. Gating on those
    produces an unpassable check, and an unpassable check that AGENTS.md says to
    fix produces an infinite --fix loop. That happened: 25 bootstrap runs in one
    session, 12 of them --fix, because editor settings can never report OK.
    """
    results.append({"name": name, "state": state, "detail": detail,
                    "remedy": remedy, "advisory": advisory})


def run(cmd, timeout=60):
    """Run a command, return (rc, combined output). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=str(ROOT))
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except Exception as exc:                                  # noqa: BLE001
        return 1, str(exc)


# --------------------------------------------------------------------------
# tool discovery
# --------------------------------------------------------------------------

def find_godot():
    """Locate the Godot binary. GODOT_BIN wins, then PATH, then known dirs."""
    env = os.environ.get("GODOT_BIN")
    if env:
        p = Path(env)
        if p.is_file():
            return str(p)
        found = shutil.which(env)
        if found:
            return found
    for name in ("godot", "godot4", "Godot", "godot.exe", "Godot_v4.7.1-stable_win64.exe"):
        found = shutil.which(name)
        if found:
            return found
    candidates = []
    if WIN:
        for base in (r"C:\tools\godot", r"C:\Program Files\Godot",
                     os.path.expandvars(r"%LOCALAPPDATA%\Programs\Godot")):
            candidates += list(Path(base).glob("*.exe")) if Path(base).is_dir() else []
    elif platform.system() == "Darwin":
        candidates += [Path("/Applications/Godot.app/Contents/MacOS/Godot")]
    else:
        candidates += [Path("/usr/local/bin/godot"), Path.home() / ".local/bin/godot"]
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def godot_version(binary):
    rc, out = run([binary, "--version"], timeout=30)
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
    return m.group(0) if m else None


# --------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------

def check_python():
    v = sys.version_info
    if v >= (3, 8):
        record("python", OK, f"{v.major}.{v.minor}.{v.micro}")
    else:
        record("python", MANUAL, f"{v.major}.{v.minor} too old",
               "Install Python 3.8+ from https://www.python.org/downloads/")


def check_git(fix=False):
    if not shutil.which("git"):
        record("git", MANUAL, "not on PATH",
               "Install Git from https://git-scm.com/downloads then reopen the terminal")
        return
    rc, out = run(["git", "--version"])
    ver = out.strip()

    if not (ROOT / ".git").is_dir():
        if not fix:
            record("git-repo", MISSING, f"{ver}, no repository here",
                   "bootstrap.py --fix will run git init and make a baseline commit")
            return
        rc, out = run(["git", "init"])
        if rc != 0:
            record("git-repo", MANUAL, "git init failed", out.strip()[:200])
            return
        # identity is required for the first commit and may not be set globally
        rc, who = run(["git", "config", "user.email"])
        if rc != 0 or not who.strip():
            run(["git", "config", "user.email", "agent@localhost"])
            run(["git", "config", "user.name", "Starter Kit"])
        run(["git", "add", "-A"])
        rc, out = run(["git", "commit", "-m", "chore: starter kit baseline"])
        if rc != 0 and "nothing to commit" not in out:
            record("git-repo", MANUAL, "baseline commit failed", out.strip()[:200])
            return
        record("git-repo", OK, "initialised, baseline commit created")
        return

    rc, out = run(["git", "rev-parse", "HEAD"])
    if rc != 0:
        record("git-repo", MISSING, "repository exists but has no commits",
               "bootstrap.py --fix will create the baseline commit")
        return
    record("git-repo", OK, f"{ver}, repository present")


def check_uv(fix=False):
    """uv is preferred over bare pip: no venv, no PATH surprises, pinned tool runs."""
    if shutil.which("uv") or shutil.which("uvx"):
        rc, out = run(["uv", "--version"])
        record("uv", OK, out.strip() or "present")
        return
    if shutil.which("gdlint"):
        # uv is a convenience, not a requirement. If gdtoolkit already resolves,
        # a missing uv must not keep reporting the setup as incomplete.
        record("uv", OK, "not installed, not needed (gdlint already on PATH)")
        return
    if not fix:
        record("uv", MISSING, "not installed",
               "bootstrap.py --fix will install it, or see https://docs.astral.sh/uv/")
        return
    # uv's own installer needs network; try pip as the dependency-free path
    rc, out = run([sys.executable, "-m", "pip", "install", "--user", "uv"], timeout=300)
    if rc == 0 and (shutil.which("uv") or shutil.which("uvx")):
        record("uv", OK, "installed via pip")
    elif rc == 0:
        record("uv", MANUAL, "installed but not on PATH",
               "Add the Python user scripts directory to PATH, or the gate will use pip instead")
    elif shutil.which("gdlint"):
        record("uv", OK, "install failed, but gdlint is already on PATH")
    else:
        record("uv", MANUAL, "install failed and gdtoolkit is unavailable",
               "Install either: uv (https://docs.astral.sh/uv/) or "
               "python -m pip install --user \"gdtoolkit==4.*\"")


def check_gdtoolkit():
    """gdtoolkit is run through uvx, so nothing needs to be installed permanently."""
    if shutil.which("uvx") or shutil.which("uv"):
        runner = ["uvx", "--from", "gdtoolkit==4.*", "gdlint", "--version"]
        if not shutil.which("uvx"):
            runner = ["uv", "tool", "run", "--from", "gdtoolkit==4.*", "gdlint", "--version"]
        rc, out = run(runner, timeout=300)
        if rc == 0:
            record("gdtoolkit", OK, f"available via uvx ({out.strip().splitlines()[-1][:40]})")
            return
        record("gdtoolkit", MANUAL, "uvx could not fetch gdtoolkit",
               "Check network access. The gate downgrades format and lint to SKIP without it.")
        return
    if shutil.which("gdlint"):
        record("gdtoolkit", OK, "gdlint on PATH")
        return
    record("gdtoolkit", MISSING, "no uvx and no gdlint",
           "Install uv, or: python -m pip install --user \"gdtoolkit==4.*\"")


def check_godot():
    binary = find_godot()
    if not binary:
        record("godot", MANUAL, "not found",
               f"Download Godot {EXPECTED_GODOT}.x Standard (NOT .NET) from "
               "https://godotengine.org/download and either add it to PATH "
               "or set GODOT_BIN to the executable")
        return None
    ver = godot_version(binary)
    if ver and not ver.startswith(EXPECTED_GODOT):
        record("godot", OK, f"{ver} at {binary} "
                            f"(kit pins {EXPECTED_GODOT}.x, mismatch is a warning only)")
    else:
        record("godot", OK, f"{ver or 'unknown version'} at {binary}")
    return binary


def check_mermaid(fix: bool) -> None:
    """Vendor mermaid.js so plan.html renders diagrams offline.

    Vendored rather than loaded from a CDN because the plan is opened as a local
    file, often on a machine behind a proxy, and a CDN script tag would silently
    fail there. Advisory, never MANUAL: a missing renderer degrades the diagram
    to readable source text, which is a cosmetic loss rather than a broken kit.
    """
    dest = ROOT / "tools" / "vendor" / "mermaid.min.js"
    if dest.is_file() and dest.stat().st_size > 100_000:
        record("mermaid", OK, "tools/vendor/mermaid.min.js present")
        return
    if not fix:
        record("mermaid", MISSING, "plan.html diagrams will render as text",
               "bootstrap.py --fix downloads it, or save "
               f"{MERMAID_URL} to tools/vendor/mermaid.min.js")
        return
    try:
        if not QUIET:
            print(f"  {DIM}downloading mermaid {MERMAID_VERSION}...{RST}")
        with urllib.request.urlopen(MERMAID_URL, timeout=120) as resp:
            blob = resp.read()
        if len(blob) < 100_000:
            raise RuntimeError(f"suspiciously small response ({len(blob)} bytes)")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        record("mermaid", OK, f"vendored {len(blob) // 1024} KB to tools/vendor")
    except Exception as exc:                                  # noqa: BLE001
        # Deliberately OK, not MANUAL: the kit works without it.
        record("mermaid", OK, f"skipped ({exc}); diagrams render as text",
               f"save {MERMAID_URL} to tools/vendor/mermaid.min.js to enable them")


def check_gut(fix=False):
    gut = SRC / "addons" / "gut"
    if (gut / "gut_cmdln.gd").is_file():
        record("gut", OK, "addons/gut present")
        return
    if not fix:
        record("gut", MISSING, "addons/gut not found",
               "bootstrap.py --fix will download it, or install GUT from the editor AssetLib")
        return
    try:
        import io
        import zipfile
        if not QUIET:
            print(f"  {DIM}downloading GUT {GUT_VERSION}...{RST}")
        with urllib.request.urlopen(GUT_URL, timeout=120) as resp:
            blob = resp.read()
        zf = zipfile.ZipFile(io.BytesIO(blob))
        prefix = None
        for n in zf.namelist():
            if "/addons/gut/" in n:
                prefix = n.split("/addons/gut/")[0] + "/addons/gut/"
                break
        if not prefix:
            raise RuntimeError("addons/gut not found inside the archive")
        dest = SRC / "addons" / "gut"
        dest.mkdir(parents=True, exist_ok=True)
        count = 0
        for n in zf.namelist():
            if not n.startswith(prefix) or n.endswith("/"):
                continue
            rel = n[len(prefix):]
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(n))
            count += 1
        record("gut", OK, f"downloaded {count} files to addons/gut")
    except Exception as exc:                                  # noqa: BLE001
        record("gut", MANUAL, f"download failed: {exc}",
               "Install GUT from the editor AssetLib, or copy addons/gut from "
               "https://github.com/bitwes/Gut manually")


# --------------------------------------------------------------------------
# editor settings
# --------------------------------------------------------------------------

EDITOR_SETTINGS = {
    "text_editor/behavior/files/auto_reload_scripts_on_external_change": "true",
    "text_editor/behavior/files/restore_scripts_on_load": "true",
    "docks/filesystem/import_resources_when_unfocused": "true",
    "text_editor/behavior/files/autosave_interval_secs": "0",
}
# Save-on-focus-loss is the dangerous one. Its key differs across builds, so it is
# reported for the human rather than patched blind.
FOCUS_LOSS_KEYS = (
    "text_editor/behavior/files/save_on_focus_loss",
    "interface/editor/save_on_focus_loss",
)


def editor_settings_path():
    sysname = platform.system()
    if sysname == "Windows":
        base = Path(os.environ.get("APPDATA", "")) / "Godot"
    elif sysname == "Darwin":
        base = Path.home() / "Library" / "Application Support" / "Godot"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "godot"
    if not base.is_dir():
        return None
    cands = sorted(base.glob("editor_settings-*.tres"))
    return cands[-1] if cands else None


def godot_running():
    """Best-effort check that a Godot *editor* is holding the settings file.

    Returns (running, certain). `certain` is False when no process listing
    worked, in which case the caller must not assume it is safe.

    Deliberately narrow, because a false positive blocks setup for no reason:
      - the preferred probe includes the process state column, so defunct
        (zombie) entries are excluded outright rather than by string matching
      - headless invocations are ignored; those are the gate's own runs and
        never touch editor settings
      - bracketed names (kernel and zombie renderings) are ignored

    Portability note: BusyBox pgrep rejects -i and `ps aux` is not universal,
    so several forms are tried and the first that works ends the search.
    """
    if WIN:
        attempts = [(["tasklist"], False)]
    else:
        attempts = [(["ps", "-A", "-o", "stat=,comm="], True),
                    (["ps", "ax"], False),
                    (["pgrep", "-l", "godot"], False)]

    for cmd, has_state in attempts:
        rc, out = run(cmd, timeout=20)
        if rc not in (0, 1) or not out:
            continue
        if ("not found" in out or "timed out" in out
                or "unrecognized option" in out or "Usage:" in out):
            continue
        for line in out.splitlines():
            low = line.lower()
            if "godot" not in low:
                continue
            if has_state and low.split()[0].startswith("z"):
                continue                      # zombie, not a live editor
            if "defunct" in low or "--headless" in low:
                continue
            if "[godot" in low:
                continue
            return True, True
        return False, True
    return False, False


def check_editor_settings(fix=False):
    path = editor_settings_path()
    if path is None:
        record("editor-settings", MISSING, "no editor settings file yet",
               "Open the project in Godot once, close it, then re-run "
               "bootstrap.py --fix. Advisory: this never blocks setup.",
               advisory=True)
        return
    text = path.read_text(encoding="utf-8", errors="replace")

    wrong = [k for k, v in EDITOR_SETTINGS.items()
             if not re.search(rf"^{re.escape(k)}\s*=\s*{v}\s*$", text, re.M)]
    focus = [k for k in FOCUS_LOSS_KEYS
             if re.search(rf"^{re.escape(k)}\s*=\s*true\s*$", text, re.M)]

    if not wrong and not focus:
        record("editor-settings", OK, f"correct in {path.name}")
        return

    if not fix:
        record("editor-settings", MISSING,
               f"{len(wrong) + len(focus)} setting(s) need changing in {path.name}",
               "bootstrap.py --fix patches them while Godot is closed. Godot "
               "rewrites this file on exit, so it may report MISSING again "
               "after every editor session. Advisory: it never blocks setup, "
               "and re-running --fix on it achieves nothing.",
               advisory=True)
        return

    running, certain = godot_running()
    if running:
        record("editor-settings", MANUAL, "Godot is running",
               "Close Godot, then re-run bootstrap.py --fix. "
               "Godot rewrites this file on exit and would discard the patch.",
               advisory=True)
        return
    if not certain:
        if not QUIET:
            print(f"  {YEL}could not verify Godot is closed; patching anyway. "
                  f"If Godot was open, close it and re-run.{RST}")

    backup = path.with_suffix(f".tres.bak-{int(time.time())}")
    backup.write_text(text, encoding="utf-8")

    lines = text.splitlines()
    for key, val in EDITOR_SETTINGS.items():
        pat = re.compile(rf"^{re.escape(key)}\s*=")
        for i, ln in enumerate(lines):
            if pat.match(ln):
                lines[i] = f"{key} = {val}"
                break
        else:
            lines.append(f"{key} = {val}")
    for key in FOCUS_LOSS_KEYS:
        pat = re.compile(rf"^{re.escape(key)}\s*=")
        for i, ln in enumerate(lines):
            if pat.match(ln):
                lines[i] = f"{key} = false"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    record("editor-settings", OK,
           f"patched {path.name}, backup at {backup.name}")


# --------------------------------------------------------------------------
# project state
# --------------------------------------------------------------------------

def _set_shape_name(name):
    """Keep project.shape.json name in step with config/name."""
    sp = ROOT / "project.shape.json"
    if not sp.is_file():
        return
    try:
        data = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if data.get("name") in ("Untitled", "", None):
        data["name"] = name
        try:
            sp.write_text(json.dumps(data, indent=2) + "\n",
                          encoding="utf-8", newline="")
        except OSError:
            pass


def check_project_shape():
    """Onboarding asks three things: name, pitch, and how involved you want to be.

    Nothing else. A brand new project cannot honestly answer how many entities
    update per frame, or whether content will be player-authored - and a gate
    that demands an answer gets a fabricated one, which every later agent then
    trusts. Those questions belong to the skill that needs them, asked when
    real work makes them answerable.

    Involvement is different, and belongs here. It is not a fact about the game
    that the project might later discover; it is a working preference the human
    already holds. Asked on day one it gets a real answer, and it changes what
    the agent does on its very first task.
    """
    sp = ROOT / "project.shape.json"
    if not sp.is_file():
        record("project-shape", OK, "absent (agents will ask instead)")
        return
    try:
        data = json.loads(sp.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        record("project-shape", MANUAL, f"unreadable: {exc}",
               "fix the JSON or delete the file")
        return
    missing = []
    if str(data.get("name", "")).strip() in ("", "Untitled"):
        missing.append("name")
    if not str(data.get("pitch", "")).strip():
        missing.append("pitch")
    inv = str(data.get("involvement", "")).strip()
    if inv not in INVOLVEMENT_LEVELS:
        missing.append("involvement")
    if not missing:
        n = len(data.get("decisions") or [])
        record("project-shape", OK,
               f"{data.get('name')} [{inv}] ({n} decision(s) logged)")
        return
    remedy = (
        "GREET FIRST, then ask. Acknowledge the person, tell them this is a "
        "fresh\n"
        "      project needing a one-time setup of about two minutes, say you "
        "need three\n"
        "      answers from them, then ask. Do not open a session with a bare "
        "question.\n"
        "      ASK THE USER, do not infer any of these:\n"
        "      1. What is the project called?\n"
        "      2. One or two sentences: what is the game?\n"
        "      3. How much say do you want over structure before code is "
        "written?\n"
        "         hands-off - build it, report what you built\n"
        "         module   - propose modules, boundaries and the data that "
        "crosses them first\n"
        "         file     - as module, plus every file you create or modify\n"
        "         function - as file, plus function signatures and how they "
        "connect\n"
        "      Write them into project.shape.json as name, pitch and "
        "involvement.\n"
        "      Do NOT ask about entity counts, netcode or content pipeline "
        "now - those\n"
        "      are logged later as decisions, when the work makes them "
        "answerable."
    )
    record("project-shape", MANUAL,
           f"{' and '.join(missing)} not yet chosen", remedy)


def check_project_name(fix=False):
    """The project name must be chosen, not inherited from the kit.

    config/name is the most authoritative identity signal in a Godot repo: an
    agent reading project.godot to work out what it is building will believe
    whatever is there. Shipping a real name would silently brand every project
    made from this kit, so the kit ships "Untitled" and setup requires a choice.
    """
    pg = SRC / "project.godot"
    if not pg.is_file():
        record("project-name", MANUAL, "project.godot missing",
               "this does not look like a Godot project root")
        return
    text = pg.read_text(encoding="utf-8", errors="replace")
    m = re.search(r'^config/name="([^"]*)"', text, re.M)
    current = m.group(1) if m else ""
    if current and current != "Untitled":
        record("project-name", OK, current)
        return
    chosen = os.environ.get("GODOT_PROJECT_NAME", "").strip()
    if fix and chosen:
        text = re.sub(r'^config/name="[^"]*"', f'config/name="{chosen}"',
                      text, count=1, flags=re.M)
        pg.write_text(text, encoding="utf-8", newline="")
        _set_shape_name(chosen)
        record("project-name", OK, f"set to {chosen}")
        return
    record("project-name", MANUAL, "still the placeholder 'Untitled'",
           "ask the user what this project is called, then re-run with "
           "GODOT_PROJECT_NAME=\"Their Name\" python bootstrap.py --fix")


def check_warnings_block():
    pg = SRC / "project.godot"
    if not pg.is_file():
        record("warnings-block", MANUAL, "project.godot missing", "Kit is incomplete")
        return
    text = pg.read_text(encoding="utf-8", errors="replace")
    required = ["untyped_declaration=2", "unsafe_method_access=2",
                "unsafe_property_access=2", "unsafe_call_argument=2"]
    missing = [r for r in required if r.replace("=", "=") not in text.replace(" ", "")]
    if missing:
        record("warnings-block", MANUAL,
               f"{len(missing)} strict warning(s) not set to error",
               "project.godot is human-owned. Restore the [debug] block from the kit.")
    else:
        record("warnings-block", OK, "strict warnings set to error")


def check_import(binary):
    if not binary:
        record("import-cache", MISSING, "skipped, Godot not found", "")
        return
    if (SRC / ".godot" / "global_script_class_cache.cfg").is_file():
        record("import-cache", OK, ".godot cache present")
        return
    record("import-cache", MISSING, "project never imported",
           "bootstrap.py --fix will run the import")


def do_import(binary):
    if not binary:
        return
    if not QUIET:
        print(f"  {DIM}importing project (first run can take a minute)...{RST}")
    # Exit code is unreliable here; Godot has returned 1 on a clean first import.
    run([binary, "--headless", "--path", str(SRC), "--import", "--quit"], timeout=600)


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def report(as_json=False):
    incomplete_n = len([r for r in results
                        if r["state"] != OK and not r.get("advisory")])
    manual_n = len([r for r in results
                    if r["state"] == MANUAL and not r.get("advisory")])
    if as_json:
        # Nothing else may reach stdout in this mode, or the agent's parse breaks.
        print(json.dumps({
            "complete": incomplete_n == 0,
            "needs_human": manual_n > 0,
            "results": results,
        }, indent=2))
        return 0 if incomplete_n == 0 else 1
    if True:
        print()
        for r in results:
            colour = {OK: GRN, MISSING: YEL, MANUAL: RED}[r["state"]]
            adv = f" {DIM}(advisory){RST}" if r.get("advisory") and r["state"] != OK else ""
            print(f"  {colour}{r['state']:<8}{RST} {r['name']:<18} {r['detail']}{adv}")
            if r["remedy"] and r["state"] != OK:
                print(f"           {DIM}{r['remedy']}{RST}")
        print()
    incomplete = [r for r in results
                  if r["state"] != OK and not r.get("advisory")]
    manual = [r for r in results
              if r["state"] == MANUAL and not r.get("advisory")]
    if not incomplete:
        print(f"{GRN}Setup complete.{RST} Run: python check.py")
        return 0
    if manual:
        print(f"{RED}{len(manual)} item(s) need a human.{RST} "
              "The agent should walk the user through these, then re-run.")
    else:
        print(f"{YEL}{len(incomplete)} item(s) can be fixed automatically.{RST} "
              "Run: python bootstrap.py --fix")
    return 1



def apply_import_profile(name):
    """Write [importer_defaults] into project.godot from import_profiles.json.

    Must happen before the first asset is imported: Godot writes every parameter
    into each .import sidecar individually rather than recording "uses default",
    so a later change does not propagate to existing assets.
    """
    src = ROOT / "import_profiles.json"
    if not src.is_file():
        print("import_profiles.json not found")
        return 2
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"could not read import_profiles.json: {exc}")
        return 2
    profiles = data.get("profiles", {})
    if name not in profiles:
        print(f"unknown profile: {name}")
        print("available: " + ", ".join(sorted(profiles)))
        return 2

    proj = SRC / "project.godot"
    text = proj.read_text(encoding="utf-8")

    block = ["[importer_defaults]", ""]
    for importer, settings in profiles[name]["settings"].items():
        block.append(f"{importer}={{")
        items = list(settings.items())
        for i, (key, value) in enumerate(items):
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, str):
                rendered = f'"{value}"'
            else:
                rendered = str(value)
            comma = "," if i < len(items) - 1 else ""
            block.append(f'&"{key}": {rendered}{comma}')
        block.append("}")
        block.append("")
    new_block = "\n".join(block).rstrip() + "\n"

    marker = "[importer_defaults]"
    if marker in text:
        start = text.index(marker)
        rest = text[start + len(marker):]
        nxt = rest.find("\n[")
        end = len(text) if nxt == -1 else start + len(marker) + nxt + 1
        text = text[:start] + new_block + text[end:]
    else:
        text = text.rstrip() + "\n\n" + new_block

    proj.write_text(text, encoding="utf-8", newline="\n")
    already = list(SRC.rglob("*.import"))
    already = [q for q in already if ".godot" not in q.parts and "addons" not in q.parts]
    print(f"applied import profile '{name}' to project.godot")
    print(f"  {profiles[name]['description']}")
    if already:
        print(f"\n  WARNING: {len(already)} asset(s) are already imported. These keep")
        print("  their old settings. To adopt the new profile, delete their .import")
        print("  files and re-run: python check.py --only import")
    return 0


def normalise_formatting():
    """Run gdformat once so the repo matches whatever the LOCAL gdtoolkit
    considers canonical.

    This exists because formatting is defined by gdtoolkit, not by this repo.
    Shipping pre-formatted GDScript means guessing that tool's output, and a
    wrong guess used to produce an unpassable gate. Let the tool decide.
    """
    if not (ROOT / "check.py").is_file():
        return
    if not QUIET:
        print(f"  {DIM}normalising GDScript formatting with local gdtoolkit...{RST}")
    code, out = run([sys.executable, str(ROOT / "check.py"), "--fix-format"],
                    timeout=240)
    if code != 0 and not QUIET:
        print(f"  {YEL}gdformat unavailable or failed; formatting left as shipped{RST}")
        for line in out.strip().splitlines()[-3:]:
            print(f"    {DIM}{line}{RST}")


def seed_manifest():
    """Create .gate.sha256 from the current gate files, if absent.

    Generated locally rather than shipped, so the baseline is always the real
    post-format state of this checkout.
    """
    manifest = ROOT / ".gate.sha256"
    if manifest.is_file():
        return
    code, _ = run([sys.executable, str(ROOT / "check.py"),
                   "--accept-gate-changes"], timeout=60)
    if code == 0 and not QUIET:
        print(f"  {DIM}created .gate.sha256 baseline{RST}")


def main():
    ap = argparse.ArgumentParser(description="One-time setup detector and automator.")
    ap.add_argument("--fix", action="store_true",
                    help="perform every step that can be done unattended")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--import-profile", metavar="NAME",
                    help="write [importer_defaults] from import_profiles.json"
                         " (do this before the first asset lands)")
    ap.add_argument("--list-import-profiles", action="store_true",
                    help="list available import profiles and exit")
    args = ap.parse_args()

    if args.list_import_profiles:
        src = ROOT / "import_profiles.json"
        if not src.is_file():
            print("import_profiles.json not found")
            return 2
        for name, prof in json.loads(src.read_text(encoding="utf-8"))["profiles"].items():
            print(f"{name:14s} {prof['description']}")
        return 0

    if args.import_profile:
        return apply_import_profile(args.import_profile)

    if not args.json:
        print(f"\n{DIM}bootstrap: {ROOT}{RST}")

    check_python()
    check_git(fix=args.fix)
    check_uv(fix=args.fix)
    check_gdtoolkit()
    binary = check_godot()
    check_gut(fix=args.fix)
    check_mermaid(args.fix)
    check_project_name(fix=args.fix)
    check_project_shape()
    check_warnings_block()

    if args.fix and binary and not (SRC / ".godot" / "global_script_class_cache.cfg").is_file():
        do_import(binary)
    check_import(binary)

    # Deliberately last: Godot rewrites editor_settings-*.tres when it exits,
    # so patching before any Godot invocation silently discards the change.
    check_editor_settings(fix=args.fix)

    # Formatting and the integrity baseline are derived from the local
    # toolchain, in this order: format first, then hash the result.
    if args.fix:
        normalise_formatting()
        seed_manifest()

    if args.fix and shutil.which("git") and (ROOT / ".git").is_dir():
        _, status = run(["git", "status", "--porcelain"])
        if status.strip():
            rc, count = run(["git", "rev-list", "--count", "HEAD"])
            # rev-list fails on a repo with no commits yet; treat that as first.
            n = int(count.strip()) if (rc == 0 and count.strip().isdigit()) else 0
            # <=1 commit means the only thing on the branch is the baseline this
            # same run just created, so this is still first-time setup.
            msg = ("chore: bootstrap setup" if n <= 1
                   else "chore: bootstrap re-run adjustments")
            run(["git", "add", "-A"])
            run(["git", "commit", "-m", msg])

    return report(as_json=args.json)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(2)
