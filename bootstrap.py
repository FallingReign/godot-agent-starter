#!/usr/bin/env python3
"""bootstrap.py - one-time setup detector and automator.

Run by the coding agent, not usually by hand. It answers two questions:

    kit doctor        inspect readiness without changing state
    kit setup repair  apply safe, repository-local fixes only

Network access, editor settings, Git initialisation, engine imports and source
formatting are deliberately separate opt-ins.  Diagnosis never performs them.

Standard library only, so it runs before anything is installed.
Every check is independent, so a partial environment reports precisely
which parts are missing rather than failing at the first gap.

Exit codes:
    0  all checks pass, setup complete
    1  one or more checks incomplete (normal before setup)
    2  bad usage
"""

import argparse
import hashlib
import importlib.metadata
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

INVOLVEMENT_LEVELS = ("hands-off", "module", "file", "function")

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))
import engine_discovery  # noqa: E402
import native_engine  # noqa: E402

EXPECTED_GODOT = engine_discovery.EXPECTED_GODOT_VERSION
DEPENDENCY_LOCK = ROOT / "dependencies.lock.json"
MAX_ARCHIVE_FILES = 2_000
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
KIT_CONFIG = ROOT / "kit.config.json"
KIT_MARKER = ROOT / ".agent-kit.json"

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


def _version_in(text):
    match = re.search(r"(?<![0-9])(\d+\.\d+\.\d+)(?![0-9])", text or "")
    return match.group(1) if match else ""


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


def run(cmd, timeout=60, environment=None):
    """Run a command, return (rc, combined output). Never raises."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=str(ROOT), env=environment)
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
    """Locate Godot without launching it or trusting a known-stale version."""
    selected = engine_discovery.select_godot(ROOT)
    return str(selected.path) if selected.path else None


# --------------------------------------------------------------------------
# individual checks
# --------------------------------------------------------------------------

def check_python():
    v = sys.version_info
    if v >= (3, 10):
        record("python", OK, f"{v.major}.{v.minor}.{v.micro}")
    else:
        record("python", MANUAL, f"{v.major}.{v.minor} too old",
               "Install Python 3.10+ from https://www.python.org/downloads/")


def check_kit_context():
    """Resolve the portable game root from the explicit marker and config."""
    global SRC
    try:
        marker = json.loads(KIT_MARKER.read_text(encoding="utf-8"))
        if marker != {"kind": "portable-agent-kit-root", "schema": 1}:
            raise ValueError(".agent-kit.json has an unsupported contract")
        config = json.loads(KIT_CONFIG.read_text(encoding="utf-8"))
        if config.get("schema") != 1:
            raise ValueError("kit.config.json schema must be 1")
        layout = config.get("game_root")
        if layout not in (".", "src"):
            raise ValueError("kit.config.json game_root must be '.' or 'src'")
        runtime = config.get("runtime_root")
        runtime_path = Path(runtime) if isinstance(runtime, str) else Path(".")
        if (not isinstance(runtime, str) or runtime_path.is_absolute()
                or ".." in runtime_path.parts or runtime_path.as_posix() in ("", ".")):
            raise ValueError("kit.config.json runtime_root must be a private relative path")
        candidate = (ROOT / layout).resolve()
        if not candidate.is_relative_to(ROOT.resolve()):
            raise ValueError("configured game root escapes the kit root")
        if not candidate.is_dir() or not (candidate / "project.godot").is_file():
            raise ValueError(
                f"configured game root does not contain project.godot: {candidate}"
            )
        SRC = candidate
        record("kit-context", OK, f"game={layout}, runtime={runtime}")
        return config
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        detail = str(exc)
        remedy = (
            "Run kit setup layout root or kit setup layout src for the existing Godot project"
            if "configured game root does not contain project.godot" in detail
            else "Restore .agent-kit.json and kit.config.json from a trusted kit release"
        )
        record("kit-context", MANUAL, f"invalid: {detail}", remedy)
        return None


def configure_game_layout(layout):
    """Apply one explicit project-layout choice without touching game files."""
    if layout not in (".", "src"):
        record("game-layout", MANUAL, f"unsupported layout: {layout!r}")
        return
    target = ROOT if layout == "." else ROOT / "src"
    if not (target / "project.godot").is_file():
        label = "the kit root" if layout == "." else "src/"
        record(
            "game-layout",
            MANUAL,
            f"{label} does not contain project.godot",
            "Choose the layout that already contains the Godot project; setup will not move it",
        )
        return
    if KIT_CONFIG.is_symlink() or not KIT_CONFIG.is_file():
        record(
            "game-layout",
            MANUAL,
            "kit.config.json is not a regular file",
            "Restore it from a trusted kit release before selecting a layout",
        )
        return
    try:
        config = json.loads(KIT_CONFIG.read_text(encoding="utf-8"))
        if not isinstance(config, dict) or config.get("schema") != 1:
            raise ValueError("kit.config.json must be a schema-1 object")
        config["game_root"] = layout
        rendered = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode(
            "utf-8"
        )
        _atomic_write_bytes(KIT_CONFIG, rendered)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        record(
            "game-layout",
            MANUAL,
            f"could not update kit.config.json: {exc}",
            "Repair the configuration explicitly; setup left game files unchanged",
        )
        return
    record(
        "game-layout",
        OK,
        "game at kit root" if layout == "." else "game under src/",
    )


def check_dependency_lock():
    """Load the closed dependency lock or fail before any network action."""
    try:
        data = json.loads(DEPENDENCY_LOCK.read_text(encoding="utf-8"))
        if data.get("schema") != 1:
            raise ValueError("schema must be 1")
        if data.get("python", {}).get("minimum") != "3.10":
            raise ValueError("python.minimum must be 3.10")
        gdtoolkit = data.get("tools", {}).get("gdtoolkit", {})
        version = gdtoolkit.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
            raise ValueError("tools.gdtoolkit.version must be exact")
        for key in ("url", "sha256", "bytes", "filename"):
            if key not in gdtoolkit:
                raise ValueError(f"tools.gdtoolkit.{key} is required")
        if not re.fullmatch(r"[0-9a-f]{64}", str(gdtoolkit["sha256"])):
            raise ValueError("tools.gdtoolkit.sha256 is invalid")
        if not isinstance(gdtoolkit["bytes"], int) or gdtoolkit["bytes"] <= 0:
            raise ValueError("tools.gdtoolkit.bytes is invalid")
        expected_wheel = f"gdtoolkit-{version}-py3-none-any.whl"
        if gdtoolkit["filename"] != expected_wheel:
            raise ValueError(f"tools.gdtoolkit.filename must be {expected_wheel}")
        tool_url = urllib.parse.urlsplit(str(gdtoolkit["url"]))
        if (tool_url.scheme != "https"
                or tool_url.hostname != "files.pythonhosted.org"
                or not tool_url.path.endswith("/" + expected_wheel)
                or tool_url.username or tool_url.password):
            raise ValueError("tools.gdtoolkit.url must be the locked canonical PyPI wheel")
        for name in ("gut", "mermaid"):
            artifact = data.get("artifacts", {}).get(name, {})
            required = ("version", "url", "sha256", "bytes")
            if any(key not in artifact for key in required):
                raise ValueError(f"artifacts.{name} is incomplete")
            if not re.fullmatch(r"[0-9a-f]{64}", str(artifact["sha256"])):
                raise ValueError(f"artifacts.{name}.sha256 is invalid")
            if not isinstance(artifact["bytes"], int) or artifact["bytes"] <= 0:
                raise ValueError(f"artifacts.{name}.bytes is invalid")
            parsed = urllib.parse.urlsplit(str(artifact["url"]))
            if parsed.scheme != "https" or parsed.username or parsed.password:
                raise ValueError(f"artifacts.{name}.url must be an official HTTPS URL")
            if name == "gut":
                expected_path = f"/bitwes/Gut/archive/refs/tags/v{artifact['version']}.zip"
                if parsed.hostname != "github.com" or parsed.path != expected_path:
                    raise ValueError("artifacts.gut.url must be the canonical tagged GitHub archive")
                for key in ("archive_prefix", "commit", "license", "tree_sha256", "files"):
                    if key not in artifact:
                        raise ValueError(f"artifacts.gut.{key} is required")
                if not re.fullmatch(r"[0-9a-f]{40}", str(artifact["commit"])):
                    raise ValueError("artifacts.gut.commit is invalid")
                if not re.fullmatch(r"[0-9a-f]{64}", str(artifact["tree_sha256"])):
                    raise ValueError("artifacts.gut.tree_sha256 is invalid")
                if not isinstance(artifact["files"], int) or artifact["files"] <= 0:
                    raise ValueError("artifacts.gut.files is invalid")
            else:
                expected_path = f"/mermaid/-/mermaid-{artifact['version']}.tgz"
                if parsed.hostname != "registry.npmjs.org" or parsed.path != expected_path:
                    raise ValueError("artifacts.mermaid.url must be the canonical npm registry tarball")
                for key in ("member", "member_sha256", "member_bytes",
                            "license_member", "license_sha256", "license_bytes"):
                    if key not in artifact:
                        raise ValueError(f"artifacts.mermaid.{key} is required")
                if artifact["member"] != "package/dist/mermaid.min.js":
                    raise ValueError("artifacts.mermaid.member is unsupported")
                if artifact["license_member"] != "package/LICENSE":
                    raise ValueError("artifacts.mermaid.license_member is unsupported")
                for key in ("member_sha256", "license_sha256"):
                    if not re.fullmatch(r"[0-9a-f]{64}", str(artifact[key])):
                        raise ValueError(f"artifacts.mermaid.{key} is invalid")
                if not isinstance(artifact["member_bytes"], int) or artifact["member_bytes"] <= 0:
                    raise ValueError("artifacts.mermaid.member_bytes is invalid")
                if not isinstance(artifact["license_bytes"], int) or artifact["license_bytes"] <= 0:
                    raise ValueError("artifacts.mermaid.license_bytes is invalid")
        record("dependency-lock", OK, "exact versions, sizes and SHA-256 hashes present")
        return data
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        record("dependency-lock", MANUAL, f"invalid: {exc}",
               "Restore dependencies.lock.json from a trusted kit release")
        return None


def check_git(initialise=False):
    """Inspect Git; initialise only after the explicit ``--init-git`` opt-in."""
    if not shutil.which("git"):
        record("git", MANUAL, "not on PATH",
               "Install Git from https://git-scm.com/downloads then reopen the terminal")
        return False
    rc, out = run(["git", "--version"])
    ver = out.strip()

    rc, inside = run(["git", "rev-parse", "--is-inside-work-tree"])
    repository_exists = rc == 0 and inside.strip().lower() == "true"

    if not repository_exists:
        # A linked worktree stores .git as a file, and a kit may live below a
        # monorepo root.  If Git sees neither because metadata is inaccessible,
        # fail safely instead of creating a nested repository over existing
        # developer state.
        try:
            has_git_marker = any((parent / ".git").exists()
                                 for parent in (ROOT, *ROOT.parents))
        except OSError:
            has_git_marker = True
        if has_git_marker:
            record("git-repo", MANUAL, "Git metadata exists but is not usable",
                   "Repair Git access explicitly; bootstrap will not initialise over it")
            return False
        if not initialise:
            record("git-repo", MISSING, f"{ver}, no repository here",
                   "Run kit setup repository if this folder should be a repository")
            return False
        rc, out = run(["git", "init"])
        if rc != 0:
            record("git-repo", MANUAL, "git init failed", out.strip()[:200])
            return False
        record("git-repo", OK, "initialised without staging or committing files")
        return True

    rc, out = run(["git", "rev-parse", "HEAD"])
    if rc != 0:
        record("git-repo", OK, f"{ver}, repository present (no commits yet)")
        return False
    record("git-repo", OK, f"{ver}, repository present")
    return False


def check_uv():
    """Discover uv without installing it or invoking a network-capable tool run."""
    if shutil.which("uv") or shutil.which("uvx"):
        rc, out = run(["uv", "--version"])
        record("uv", OK, out.strip() or "present")
        return
    if shutil.which("gdlint"):
        # uv is a convenience, not a requirement. If gdtoolkit already resolves,
        # a missing uv must not keep reporting the setup as incomplete.
        record("uv", OK, "not installed, not needed (gdlint already on PATH)")
        return
    record("uv", MISSING, "not installed",
           "Optional: install uv from https://docs.astral.sh/uv/ if you want isolated tool management",
           advisory=True)


def _gdtoolkit_environment(version):
    """Return the private, versioned tool environment and its entry points."""
    config = json.loads(KIT_CONFIG.read_text(encoding="utf-8"))
    runtime = Path(config["runtime_root"])
    if runtime.is_absolute() or ".." in runtime.parts:
        raise ValueError("unsafe runtime_root")
    target = ROOT / runtime / "tooling" / f"gdtoolkit-{version}"
    bindir = target / ("Scripts" if WIN else "bin")
    suffix = ".exe" if WIN else ""
    return target, bindir / f"gdlint{suffix}", bindir / f"python{suffix}"


def _install_gdtoolkit(lock):
    """Install one locked wheel into private runtime after explicit opt-in.

    The direct package bytes are fetched from the locked canonical PyPI URL and
    verified before pip sees them. Dependencies resolve only from the canonical
    PyPI index and must have wheels; no source checkout or global environment is
    used. The virtual environment is built at its final path because Python
    entry-point launchers embed that path and are not safely relocatable.
    """
    artifact = lock["tools"]["gdtoolkit"]
    version = artifact["version"]
    target, _gdlint, _python = _gdtoolkit_environment(version)
    if target.exists():
        record(
            "gdtoolkit", MANUAL,
            f"private tool path exists but is not a valid locked {version} install",
            f"Review {target}; setup will not replace it automatically",
        )
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir()
    except FileExistsError:
        record(
            "gdtoolkit", MANUAL,
            f"private tool path was claimed concurrently: {target}",
            "Wait for the other setup operation, then run kit doctor",
        )
        return
    except OSError as exc:
        record("gdtoolkit", MANUAL, f"cannot create private tool path: {exc}")
        return
    try:
        rc, out = run(
            [sys.executable, "-I", "-m", "venv", str(target)], timeout=180
        )
        if rc != 0:
            raise RuntimeError("private environment creation failed: "
                               + " | ".join(out.strip().splitlines()[-3:]))
        bindir = target / ("Scripts" if WIN else "bin")
        suffix = ".exe" if WIN else ""
        private_python = bindir / f"python{suffix}"
        private_gdlint = bindir / f"gdlint{suffix}"
        wheel = target / artifact["filename"]
        if not QUIET:
            print(f"  {DIM}downloading locked gdtoolkit {version} from canonical PyPI...{RST}")
        _atomic_write_bytes(wheel, _download_locked(artifact, "gdtoolkit"))
        pip_environment = {
            key: value for key, value in os.environ.items()
            if not key.upper().startswith("PIP_")
        }
        pip_environment["PIP_CONFIG_FILE"] = os.devnull
        rc, out = run([
            str(private_python), "-I", "-m", "pip", "--isolated", "install",
            "--disable-pip-version-check", "--no-input", "--no-cache-dir",
            "--only-binary=:all:",
            "--index-url", "https://pypi.org/simple", str(wheel),
        ], timeout=600, environment=pip_environment)
        if rc != 0:
            raise RuntimeError("canonical PyPI install failed: "
                               + " | ".join(out.strip().splitlines()[-5:]))
        wheel.unlink(missing_ok=True)
        rc, out = run([str(private_gdlint), "--version"], timeout=60)
        if rc != 0 or _version_in(out) != version:
            raise RuntimeError(
                f"installed tool reported {_version_in(out) or 'no version'}; expected {version}"
            )
        record("gdtoolkit", OK,
               f"locked {version} installed in project-private runtime")
    except Exception as exc:                                  # noqa: BLE001
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        record("gdtoolkit", MANUAL, f"verified installation failed: {exc}",
               "Check approved network access and canonical PyPI availability; no global install was changed")


def check_gdtoolkit(lock, install=False):
    """Check exact formatter sources; fetch only after explicit setup opt-in."""
    version = lock.get("tools", {}).get("gdtoolkit", {}).get("version") if lock else None
    requirement = f"gdtoolkit=={version}" if version else "the locked gdtoolkit version"
    if lock and version:
        try:
            _target, local_gdlint, _local_python = _gdtoolkit_environment(version)
            if local_gdlint.is_file():
                rc, out = run([str(local_gdlint), "--version"])
                if rc == 0 and _version_in(out) == version:
                    record("gdtoolkit", OK,
                           f"locked {version} in project-private runtime")
                    return
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pass
    found = shutil.which("gdlint")
    if found:
        rc, out = run([found, "--version"])
        detail = out.strip().splitlines()[-1][:80] if out.strip() else "gdlint on PATH"
        installed = _version_in(out)
        if rc == 0 and installed == version:
            record("gdtoolkit", OK, detail)
            return
        if not install:
            record("gdtoolkit", MANUAL,
                   f"found {installed or 'unknown version'}; lock requires {version}",
                   f"Run kit setup dependency gdtoolkit for a private pinned {version} install",
                   advisory=True)
            return
    try:
        installed = importlib.metadata.version("gdtoolkit")
    except importlib.metadata.PackageNotFoundError:
        installed = ""
    if installed == version:
        record("gdtoolkit", OK, f"{requirement} installed for this Python")
        return
    if installed and not install:
        record("gdtoolkit", MANUAL,
               f"found {installed}; lock requires {version}",
               f"Run kit setup dependency gdtoolkit for a private pinned {version} install",
               advisory=True)
        return
    if install:
        if not lock:
            record("gdtoolkit", MANUAL,
                   "installation refused because dependency lock is invalid")
            return
        _install_gdtoolkit(lock)
        return
    record(
        "gdtoolkit",
        MISSING,
        "optional style adapter not installed locally",
        "Run kit setup dependency gdtoolkit after approving the canonical PyPI download; strict verification requires it",
        advisory=True,
    )


def check_godot(selected_binary=None):
    """Locate Godot without starting the native executable.

    Doctor and ordinary setup diagnosis must remain safe even when the local
    engine binary itself is unstable. Exact engine health/version evidence is
    gathered only at the explicit engine boundary in ``kit verify``.
    """
    if selected_binary:
        try:
            selected_path = Path(selected_binary).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            selected_path = None
        binary = str(selected_path) if selected_path and selected_path.is_file() else None
    else:
        binary = find_godot()
    if not binary:
        record("godot", MANUAL, "not found",
               f"Download Godot {EXPECTED_GODOT} Standard (NOT .NET) from "
               "https://godotengine.org/download and either add it to PATH "
               "or set GODOT_BIN to the executable")
        return None

    selected_version = engine_discovery.version_from_name(binary)
    requested = os.environ.get("GODOT_BIN")
    requested_version = (
        engine_discovery.version_from_name(requested) if requested else None
    )
    if selected_version and selected_version != EXPECTED_GODOT:
        record(
            "godot",
            MANUAL,
            f"selected {selected_version} at {binary}; exact {EXPECTED_GODOT} required; "
            "executable not launched during diagnosis",
            f"Point GODOT_BIN to Godot {EXPECTED_GODOT} Standard or place the exact "
            "official binary beside the selected executable",
        )
        return None

    if (
        requested
        and requested_version
        and requested_version != EXPECTED_GODOT
        and selected_version == EXPECTED_GODOT
    ):
        detail = (
            f"required {EXPECTED_GODOT} selected at {binary}; ignored stale "
            f"GODOT_BIN ({requested_version}); executable not launched during diagnosis"
        )
    elif selected_version == EXPECTED_GODOT:
        detail = (
            f"required {EXPECTED_GODOT} present at {binary}; executable not launched "
            "during diagnosis"
        )
    else:
        detail = (
            f"present at {binary}; version will be confirmed by kit verify; "
            "executable not launched during diagnosis"
        )
    record(
        "godot",
        OK,
        detail,
    )
    return binary


def _download_locked(artifact, label):
    """Fetch one exact artifact with a strict size cap and digest check."""
    expected_size = int(artifact["bytes"])
    if expected_size > MAX_ARCHIVE_BYTES:
        raise RuntimeError(f"locked {label} size exceeds safety limit")
    request = urllib.request.Request(
        str(artifact["url"]),
        headers={"User-Agent": "godot-agent-kit-bootstrap/1"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        declared = response.headers.get("Content-Length")
        if declared is not None and int(declared) != expected_size:
            raise RuntimeError(
                f"{label} Content-Length {declared} != locked {expected_size}"
            )
        blob = response.read(expected_size + 1)
    if len(blob) != expected_size:
        raise RuntimeError(f"{label} size {len(blob)} != locked {expected_size}")
    actual = hashlib.sha256(blob).hexdigest()
    if actual != artifact["sha256"]:
        raise RuntimeError(f"{label} SHA-256 mismatch")
    return blob


def _atomic_write_bytes(destination, blob):
    """Publish bytes without leaving a partial trusted artifact behind."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.bootstrap-{os.getpid()}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _regular_tree_digest(root):
    """Return ``(files, bytes, digest)`` for a symlink-free installed tree."""
    root_info = root.lstat()
    root_attributes = getattr(root_info, "st_file_attributes", 0)
    if (root.is_symlink()
            or root_attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            or not stat.S_ISDIR(root_info.st_mode)):
        raise RuntimeError(f"dependency tree root is not a regular directory: {root}")
    records = []
    total = 0
    for current, directories, filenames in os.walk(root, followlinks=False):
        current_path = Path(current)
        for name in list(directories):
            candidate = current_path / name
            info = candidate.lstat()
            attributes = getattr(info, "st_file_attributes", 0)
            if candidate.is_symlink() or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise RuntimeError(f"dependency tree contains a linked directory: {candidate}")
            if not stat.S_ISDIR(info.st_mode):
                raise RuntimeError(f"dependency tree contains a non-directory entry: {candidate}")
        for name in filenames:
            candidate = current_path / name
            info = candidate.lstat()
            attributes = getattr(info, "st_file_attributes", 0)
            if (candidate.is_symlink()
                    or attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                    or not stat.S_ISREG(info.st_mode)):
                raise RuntimeError(f"dependency tree contains a non-regular file: {candidate}")
            relative = candidate.relative_to(root).as_posix()
            if not relative or relative.startswith("../"):
                raise RuntimeError(f"dependency tree contains an unsafe path: {candidate}")
            content = candidate.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            records.append((relative, len(content), digest))
            total += len(content)
    return len(records), total, _tree_records_digest(records)


def _tree_records_digest(records):
    """Digest canonical path/size/content records with unambiguous framing."""
    framed = bytearray()
    for relative, size, digest in sorted(records, key=lambda item: item[0]):
        framed.extend(f"{relative}\0{size}\0{digest}\n".encode("utf-8"))
    return hashlib.sha256(framed).hexdigest()


def _gut_archive_payloads(blob, artifact):
    """Authenticate and return the exact regular GUT files selected by the lock."""
    import zipfile

    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        prefix = artifact["archive_prefix"]
        members = [
            info
            for info in archive.infolist()
            if info.filename.startswith(prefix) and not info.is_dir()
        ]
        if len(members) != artifact["files"] or len(members) > MAX_ARCHIVE_FILES:
            raise RuntimeError(f"unsafe archive member count: {len(members)}")
        if sum(info.file_size for info in members) > MAX_ARCHIVE_BYTES:
            raise RuntimeError("unpacked archive exceeds safety limit")

        payloads = []
        records = []
        seen = set()
        for info in members:
            raw_relative = info.filename[len(prefix):]
            if "\\" in raw_relative:
                raise RuntimeError(f"unsafe archive path: {info.filename}")
            relative = PurePosixPath(raw_relative)
            if (
                relative.is_absolute()
                or not relative.parts
                or any(part in ("", ".", "..") or ":" in part for part in relative.parts)
            ):
                raise RuntimeError(f"unsafe archive path: {info.filename}")
            mode = info.external_attr >> 16
            if mode and (mode & 0o170000) not in (0, 0o100000):
                raise RuntimeError(f"non-file archive member: {info.filename}")
            if info.flag_bits & 0x1:
                raise RuntimeError(f"encrypted archive member: {info.filename}")
            canonical = relative.as_posix()
            if canonical in seen:
                raise RuntimeError(f"duplicate archive path: {info.filename}")
            seen.add(canonical)
            with archive.open(info) as source:
                content = source.read(info.file_size + 1)
            if len(content) != info.file_size:
                raise RuntimeError(f"archive member size changed: {info.filename}")
            digest = hashlib.sha256(content).hexdigest()
            records.append((canonical, len(content), digest))
            payloads.append((canonical, content))

    tree_digest = _tree_records_digest(records)
    if len(payloads) != artifact["files"] or tree_digest != artifact["tree_sha256"]:
        raise RuntimeError(
            "verified archive contents do not match the locked GUT tree: "
            f"{len(payloads)} files/{tree_digest}"
        )
    return payloads


def _tar_member(blob, name, expected_size, expected_sha256):
    """Read one exact regular member without extracting any archive path."""
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        matches = [member for member in archive.getmembers() if member.name == name]
        if len(matches) != 1:
            raise RuntimeError(f"archive must contain exactly one {name!r} member")
        member = matches[0]
        if not member.isfile() or member.issym() or member.islnk():
            raise RuntimeError(f"archive member is not a regular file: {name}")
        if member.size != expected_size:
            raise RuntimeError(
                f"archive member {name} size {member.size} != locked {expected_size}"
            )
        source = archive.extractfile(member)
        if source is None:
            raise RuntimeError(f"archive member cannot be read: {name}")
        content = source.read(expected_size + 1)
    if len(content) != expected_size:
        raise RuntimeError(f"archive member {name} size changed while reading")
    if hashlib.sha256(content).hexdigest() != expected_sha256:
        raise RuntimeError(f"archive member {name} SHA-256 mismatch")
    return content


def audit_dependencies(lock):
    """Download and authenticate every direct lock artifact without publishing it."""
    if not lock:
        record("dependency-audit", MANUAL,
               "source audit refused because dependency lock is invalid")
        return
    try:
        gdtoolkit = lock["tools"]["gdtoolkit"]
        _download_locked(gdtoolkit, "gdtoolkit")

        gut = lock["artifacts"]["gut"]
        gut_blob = _download_locked(gut, "GUT")
        _gut_archive_payloads(gut_blob, gut)

        mermaid = lock["artifacts"]["mermaid"]
        mermaid_blob = _download_locked(mermaid, "mermaid")
        _tar_member(
            mermaid_blob,
            mermaid["member"],
            mermaid["member_bytes"],
            mermaid["member_sha256"],
        )
        _tar_member(
            mermaid_blob,
            mermaid["license_member"],
            mermaid["license_bytes"],
            mermaid["license_sha256"],
        )
        record(
            "dependency-audit",
            OK,
            "canonical upstream sources authenticated for "
            f"gdtoolkit {gdtoolkit['version']}, GUT {gut['version']} and Mermaid {mermaid['version']}",
        )
    except Exception as exc:                                  # noqa: BLE001
        record(
            "dependency-audit",
            MANUAL,
            f"canonical source authentication failed: {exc}",
            "Review the lock and upstream release; no dependency was installed or replaced",
        )


def check_mermaid(download: bool, lock) -> None:
    """Vendor mermaid.js so plan.html renders diagrams offline.

    Vendored rather than loaded from a CDN because the plan is opened as a local
    file, often on a machine behind a proxy, and a CDN script tag would silently
    fail there. Advisory, never MANUAL: a missing renderer degrades the diagram
    to readable source text, which is a cosmetic loss rather than a broken kit.
    """
    dest = ROOT / "tools" / "vendor" / "mermaid.min.js"
    license_dest = ROOT / "tools" / "vendor" / "mermaid.LICENSE.txt"
    if dest.is_file() and dest.stat().st_size > 100_000:
        if lock:
            expected = lock["artifacts"]["mermaid"]
            actual = hashlib.sha256(dest.read_bytes()).hexdigest()
            bundle_ok = (dest.stat().st_size == expected["member_bytes"]
                         and actual == expected["member_sha256"])
            license_ok = (license_dest.is_file()
                          and license_dest.stat().st_size == expected["license_bytes"]
                          and hashlib.sha256(license_dest.read_bytes()).hexdigest()
                          == expected["license_sha256"])
            if bundle_ok and license_ok:
                record("mermaid", OK,
                       f"locked {expected['version']} bundle and license present")
                return
            if bundle_ok and not download:
                record("mermaid", MISSING,
                       f"locked {expected['version']} bundle present; license sidecar absent",
                       "The next explicit Mermaid acquisition adds the official license sidecar",
                       advisory=True)
                return
            if not bundle_ok:
                record("mermaid", MANUAL, "vendored file does not match the lock",
                       "Review the local change before any explicit reacquisition")
                return
        else:
            record("mermaid", MANUAL, "present but dependency lock is invalid")
            return
    if not download:
        record("mermaid", MISSING, "plan.html diagrams will render as text",
               "Run kit setup dependency mermaid to fetch the locked artifact",
               advisory=True)
        return
    if not lock:
        record("mermaid", MANUAL, "download refused because dependency lock is invalid")
        return
    try:
        artifact = lock["artifacts"]["mermaid"]
        if not QUIET:
            print(f"  {DIM}downloading locked mermaid {artifact['version']}...{RST}")
        blob = _download_locked(artifact, "mermaid")
        bundle = _tar_member(blob, artifact["member"], artifact["member_bytes"],
                             artifact["member_sha256"])
        license_blob = _tar_member(
            blob, artifact["license_member"], artifact["license_bytes"],
            artifact["license_sha256"],
        )
        _atomic_write_bytes(dest, bundle)
        _atomic_write_bytes(license_dest, license_blob)
        record("mermaid", OK,
               f"verified and vendored {len(bundle) // 1024} KB plus license to tools/vendor")
    except Exception as exc:                                  # noqa: BLE001
        record("mermaid", MANUAL, f"verified download failed: {exc}",
               "Check network access and the trusted dependency lock; no partial file was published")


def check_gut(download=False, lock=None):
    gut = SRC / "addons" / "gut"
    if (gut / "gut_cmdln.gd").is_file():
        if not lock:
            record("gut", MANUAL, "addons/gut present but dependency lock is invalid")
            return
        try:
            expected = lock["artifacts"]["gut"]
            count, total, digest = _regular_tree_digest(gut)
            if count != expected["files"] or digest != expected["tree_sha256"]:
                raise RuntimeError(
                    f"installed tree is {count} files/{digest}; expected "
                    f"{expected['files']} files/{expected['tree_sha256']}"
                )
            record("gut", OK,
                   f"locked {expected['version']} tree present ({count} files, {total} bytes)")
        except (OSError, RuntimeError, KeyError) as exc:
            record("gut", MANUAL, f"installed tree does not match the lock: {exc}",
                   "Review local GUT changes or explicitly reacquire the locked release")
        return
    if not download:
        record("gut", MISSING, "addons/gut not found",
               "Run kit setup dependency gut to fetch the locked artifact",
               advisory=True)
        return
    if not lock:
        record("gut", MANUAL, "download refused because dependency lock is invalid")
        return
    try:
        artifact = lock["artifacts"]["gut"]
        if not QUIET:
            print(f"  {DIM}downloading locked GUT {artifact['version']}...{RST}")
        blob = _download_locked(artifact, "GUT")
        payloads = _gut_archive_payloads(blob, artifact)
        dest = SRC / "addons" / "gut"
        if dest.exists():
            raise RuntimeError(f"refusing to replace existing path: {dest}")
        staging = dest.with_name(f".{dest.name}.bootstrap-{os.getpid()}.tmp")
        if staging.exists():
            raise RuntimeError(f"staging path already exists: {staging}")
        staging.mkdir(parents=True)
        for relative, content in payloads:
            target = staging / Path(relative)
            if not target.resolve().is_relative_to(staging.resolve()):
                raise RuntimeError(f"archive path escapes destination: {relative}")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        if not (staging / "gut_cmdln.gd").is_file():
            raise RuntimeError("verified archive did not contain gut_cmdln.gd")
        tree_count, _tree_bytes, tree_digest = _regular_tree_digest(staging)
        if tree_count != artifact["files"] or tree_digest != artifact["tree_sha256"]:
            raise RuntimeError("verified archive contents do not match the locked GUT tree")
        os.replace(staging, dest)
        record("gut", OK, f"verified and installed {len(payloads)} files to addons/gut")
    except Exception as exc:                                  # noqa: BLE001
        staging = SRC / "addons" / f".gut.bootstrap-{os.getpid()}.tmp"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        record("gut", MANUAL, f"verified download failed: {exc}",
               "Check network access and the trusted dependency lock; no partial install was published")


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


def check_editor_settings(configure=False):
    path = editor_settings_path()
    if path is None:
        record("editor-settings", MISSING, "no editor settings file yet",
               "Open the project in Godot once, close it, then run "
               "kit setup editor. Advisory: this never blocks setup.",
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

    if not configure:
        record("editor-settings", MISSING,
               f"{len(wrong) + len(focus)} setting(s) need changing in {path.name}",
               "kit setup editor patches them while Godot is closed. Godot "
               "rewrites this file on exit, so it may report MISSING again "
               "after every editor session. Advisory: it never blocks setup, "
               "and re-running --configure-editor while Godot is open achieves nothing.",
               advisory=True)
        return

    running, certain = godot_running()
    if running:
        record("editor-settings", MANUAL, "Godot is running",
               "Close Godot, then re-run kit setup editor. "
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


def check_project_name(chosen=None):
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
    chosen = (chosen or "").strip()
    if chosen:
        if any(char in chosen for char in ('"', "\r", "\n")):
            record("project-name", MANUAL, "requested name contains unsafe characters",
                   "Choose a single-line name without a double quote")
            return
        text = re.sub(r'^config/name="[^"]*"', f'config/name="{chosen}"',
                      text, count=1, flags=re.M)
        pg.write_text(text, encoding="utf-8", newline="")
        _set_shape_name(chosen)
        record("project-name", OK, f"set to {chosen}")
        return
    record("project-name", MANUAL, "still the placeholder 'Untitled'",
           "Ask the user what this project is called, then run "
           "kit setup name \"Their Name\"")


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


def check_import(binary, attempted=None):
    if not binary:
        record("import-cache", MISSING, "skipped, Godot not found", "")
        return
    if (
        attempted is not None
        and attempted.failure_class == "engine-authentication-failed"
    ):
        record(
            "import-cache",
            MISSING,
            attempted.output.strip() or "Godot authentication failed",
            f"Point GODOT_BIN to an executable which reports exactly {EXPECTED_GODOT}, "
            "then run kit setup import again.",
        )
        return
    if attempted is not None and attempted.failure_class:
        record(
            "import-cache",
            MISSING,
            f"Godot import stopped at the safe native boundary ({attempted.failure_class})",
            "The unresolved native warning must be cleared by one explicitly approved "
            "bounded kit verify after the process issue is understood.",
        )
        return
    if attempted is not None and re.search(
        r"(?im)^\s*(?:SCRIPT ERROR|ERROR|FATAL|CRASH):", attempted.output
    ):
        record(
            "import-cache",
            MISSING,
            "Godot import reported an engine or script error",
            "Fix the first reported error, then run kit setup import again.",
        )
        return
    if (SRC / ".godot" / "global_script_class_cache.cfg").is_file():
        record("import-cache", OK, ".godot cache present")
        return
    record("import-cache", MISSING, "project never imported",
           "Run kit setup import")


def do_import(binary):
    if not binary:
        return None
    if not QUIET:
        print(f"  {DIM}importing project (first run can take a minute)...{RST}")
    try:
        authenticated = engine_discovery.authenticate_godot(
            ROOT,
            candidate=binary,
            operation="setup-import",
        )
        authenticated.assert_unchanged()
    except engine_discovery.EngineAuthenticationError as exc:
        return native_engine.NativeResult(
            exit_code=native_engine.REFUSED_EXIT,
            output=str(exc),
            executable=Path(str(binary)).name,
            started=False,
            failure_class="engine-authentication-failed",
            failure_code=exc.status,
        )
    # Exit code is unreliable here; Godot has returned 1 on a clean first import.
    # The shared boundary classifies process failures and the caller proves the
    # operation from the fresh cache plus scanned output.
    result = native_engine.run_godot(
        authenticated.path,
        ["--headless", "--path", str(SRC), "--import", "--quit"],
        root=ROOT,
        cwd=ROOT,
        timeout=600,
    )
    native_engine.persist_native_failure(ROOT, result, operation="setup-import")
    if result.failure_class and not QUIET:
        for line in result.output.strip().splitlines()[-6:]:
            print(f"  {YEL}{line}{RST}")
    return result


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
        print(f"{GRN}Setup complete.{RST} Run: kit verify")
        return 0
    if manual:
        print(f"{RED}{len(manual)} item(s) need a human.{RST} "
              "The agent should walk the user through these, then re-run.")
    else:
        print(f"{YEL}{len(incomplete)} item(s) remain.{RST} "
              "Use the explicit remedy shown for each item.")
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
        print("  files and re-run: kit verify --stage import")
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


def main():
    ap = argparse.ArgumentParser(description="One-time setup detector and automator.")
    ap.add_argument("--fix", action="store_true",
                    help="apply safe repository-local fixes only (never network, editor, Git, import or format)")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--download-dep", action="append",
                    choices=("gdtoolkit", "gut", "mermaid"),
                    default=[], metavar="{gdtoolkit,gut,mermaid}",
                    help="explicitly fetch one hash-locked dependency")
    ap.add_argument(
        "--audit-dependencies",
        action="store_true",
        help="download and authenticate every direct lock artifact without installing it",
    )
    ap.add_argument("--configure-editor", action="store_true",
                    help="explicitly patch the current user's Godot editor settings")
    ap.add_argument("--init-git", action="store_true",
                    help="explicitly run git init; never stages or commits")
    ap.add_argument("--import-project", action="store_true",
                    help="explicitly run Godot's headless project import")
    ap.add_argument("--engine-bin", help=argparse.SUPPRESS)
    ap.add_argument("--format-game", action="store_true",
                    help="explicitly format game GDScript with the local toolchain")
    ap.add_argument("--project-name", metavar="NAME",
                    help="explicitly replace the Untitled project name")
    ap.add_argument("--game-layout", choices=(".", "src"),
                    help="explicitly select the existing Godot project layout")
    ap.add_argument("--import-profile", metavar="NAME",
                    help="write [importer_defaults] from import_profiles.json"
                         " (do this before the first asset lands)")
    ap.add_argument("--list-import-profiles", action="store_true",
                    help="list available import profiles and exit")
    ap.add_argument(
        "--operation-target",
        choices=("repair", "editor-settings", "git-repo", "import-cache",
                 "gdtoolkit", "gut", "mermaid", "dependency-audit",
                 "project-name", "game-layout"),
        help=argparse.SUPPRESS,
    )
    args = ap.parse_args()

    mutations = (
        bool(args.download_dep),
        args.audit_dependencies,
        args.configure_editor,
        args.init_git,
        args.import_project,
        args.format_game,
        args.project_name is not None,
        args.game_layout is not None,
        args.import_profile is not None,
    )
    if args.json and any(mutations):
        ap.error("--json is read-only and cannot be combined with state-changing options")

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
    if args.game_layout is not None:
        configure_game_layout(args.game_layout)
    check_kit_context()
    lock = check_dependency_lock()
    if args.audit_dependencies:
        audit_dependencies(lock)
    check_git(initialise=args.init_git)
    check_uv()
    requested_downloads = set(args.download_dep)
    check_gdtoolkit(lock, install="gdtoolkit" in requested_downloads)
    binary = check_godot(selected_binary=args.engine_bin)
    check_gut(download="gut" in requested_downloads, lock=lock)
    check_mermaid(download="mermaid" in requested_downloads, lock=lock)
    check_project_name(chosen=args.project_name)
    check_project_shape()
    check_warnings_block()

    import_result = None
    if args.import_project and binary and not (SRC / ".godot" / "global_script_class_cache.cfg").is_file():
        import_result = do_import(binary)
    check_import(binary, attempted=import_result)

    # Deliberately last: Godot rewrites editor_settings-*.tres when it exits,
    # so patching before any Godot invocation silently discards the change.
    check_editor_settings(configure=args.configure_editor)

    # Formatting is derived from the local toolchain.  The integrity manifest
    # is shipped trust data and is never created or accepted by bootstrap.
    if args.format_game:
        normalise_formatting()

    overall = report(as_json=args.json)
    if args.operation_target:
        # The unified CLI asks whether the one explicitly authorized operation
        # succeeded. Overall readiness is still printed, but an unrelated
        # missing prerequisite must not turn a successful operation into a
        # false failure. `repair` is intentionally a safe no-op when the
        # released allowlisted files already exist.
        if args.operation_target == "repair":
            return 0
        target = next(
            (item for item in results if item.get("name") == args.operation_target),
            None,
        )
        return 0 if target and target.get("state") == OK else 2
    return overall


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(2)
