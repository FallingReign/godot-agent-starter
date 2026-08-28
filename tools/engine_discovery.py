#!/usr/bin/env python3
"""Select Godot safely, then authenticate it at an explicit native boundary.

``select_godot`` is deliberately read-only so ``kit doctor`` can locate an
engine without starting it.  Selection is not execution authority: every
non-gate native operation must call ``authenticate_godot`` first.  That probe
is shell-free, bounded by the shared native-process boundary, and binds the
resolved path, the engine-reported patch version, and the file identity which
was authenticated.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

EXPECTED_GODOT_VERSION = "4.7.2"

_VERSIONED_NAME = re.compile(
    r"(?i)godot(?:[_-]?v)?(?P<version>\d+\.\d+\.\d+)"
)
_REPORTED_VERSION = re.compile(r"(?<![0-9])(\d+\.\d+\.\d+)(?![0-9])")
_ENGINE_ERROR = re.compile(r"(?im)^\s*(?:SCRIPT ERROR|ERROR|FATAL|CRASH):")


class EngineAuthenticationError(RuntimeError):
    """A selected executable did not prove it is the supported engine."""

    def __init__(self, message: str, *, status: str) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class EngineFileIdentity:
    """Stable-enough identity for the exact executable file which was probed."""

    path: Path
    device: int
    inode: int
    size: int
    modified_ns: int


@dataclass(frozen=True)
class AuthenticatedEngine:
    """Execution authority for one selected path and reported patch version."""

    path: Path
    version: str
    source: str
    identity: EngineFileIdentity

    def assert_unchanged(self) -> None:
        """Refuse if the executable was replaced after its version probe."""
        current = _file_identity(self.path)
        if current != self.identity:
            raise EngineAuthenticationError(
                "selected Godot changed after version authentication; refusing launch",
                status="engine_identity_changed",
            )


@dataclass(frozen=True)
class EngineSelection:
    """One deterministic, non-executing engine-discovery result."""

    path: Path | None
    source: str
    requested: str | None = None
    requested_version: str | None = None
    selected_version: str | None = None

    @property
    def known_mismatch(self) -> bool:
        return bool(
            self.path is not None
            and self.selected_version
            and self.selected_version != EXPECTED_GODOT_VERSION
        )

    @property
    def replaced_stale_request(self) -> bool:
        return bool(
            self.requested
            and self.requested_version
            and self.requested_version != EXPECTED_GODOT_VERSION
            and self.selected_version == EXPECTED_GODOT_VERSION
        )


def version_from_name(value: str | Path) -> str | None:
    """Return a version encoded in an official-style executable name."""
    match = _VERSIONED_NAME.search(Path(value).name)
    return match.group("version") if match else None


def _file_identity(path: Path) -> EngineFileIdentity:
    try:
        resolved = path.expanduser().resolve(strict=True)
        stat = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise EngineAuthenticationError(
            "selected Godot executable is no longer readable; refusing launch",
            status="engine_unavailable",
        ) from exc
    if not resolved.is_file():
        raise EngineAuthenticationError(
            "selected Godot path is not a file; refusing launch",
            status="engine_unavailable",
        )
    return EngineFileIdentity(
        path=resolved,
        device=int(stat.st_dev),
        inode=int(stat.st_ino),
        size=int(stat.st_size),
        modified_ns=int(stat.st_mtime_ns),
    )


def _selection_for_candidate(root: Path, candidate: str | Path) -> EngineSelection:
    identity = _file_identity(
        Path(candidate) if Path(candidate).is_absolute() else root / Path(candidate)
    )
    named_version = version_from_name(identity.path)
    return EngineSelection(
        path=identity.path,
        source="provided",
        requested=str(candidate),
        requested_version=named_version,
        selected_version=named_version,
    )


def authenticate_godot(
    root: Path,
    *,
    selection: EngineSelection | None = None,
    candidate: str | Path | None = None,
    operation: str,
    timeout: int = 30,
    runner: Callable[..., Any] | None = None,
) -> AuthenticatedEngine:
    """Authenticate one selected executable before a non-gate native launch.

    A version encoded in a filename is useful discovery evidence but is never
    accepted as process evidence.  The exact resolved executable is run once
    with ``--headless --version`` through the bounded native runner.  No shell
    is involved.  A non-zero exit, native failure, ambiguous/missing version,
    reported mismatch, or executable replacement fails closed.

    ``candidate`` exists for internal commands which receive the public kit's
    selected path.  Callers may instead pass the complete read-only
    ``selection``.  Supplying neither performs a fresh selection.
    """
    if selection is not None and candidate is not None:
        raise ValueError("pass either selection or candidate, not both")
    canonical_root = root.expanduser().resolve(strict=True)
    selected = (
        selection
        if selection is not None
        else _selection_for_candidate(canonical_root, candidate)
        if candidate is not None
        else select_godot(canonical_root)
    )
    if selected.path is None:
        raise EngineAuthenticationError(
            f"{operation} needs Godot {EXPECTED_GODOT_VERSION}; no executable was selected",
            status="engine_unavailable",
        )
    if selected.known_mismatch:
        raise EngineAuthenticationError(
            f"{operation} refused Godot {selected.selected_version} at {selected.path}; "
            f"expected exactly {EXPECTED_GODOT_VERSION}",
            status="engine_version_mismatch",
        )

    before = _file_identity(selected.path)
    if runner is None:
        import native_engine

        runner = native_engine.run_godot
    result = runner(
        before.path,
        ["--headless", "--version"],
        root=canonical_root,
        cwd=canonical_root,
        timeout=timeout,
    )
    failure_class = str(getattr(result, "failure_class", "") or "")
    if failure_class:
        try:
            import native_engine

            native_engine.persist_native_failure(
                canonical_root,
                result,
                operation=f"{operation}-version-probe",
            )
        except (AttributeError, TypeError):
            # A test runner or third-party result may intentionally implement
            # only the probe-result protocol. Authentication still fails.
            pass
        raise EngineAuthenticationError(
            f"{operation} could not authenticate Godot at the safe native boundary "
            f"({failure_class})",
            status="engine_probe_failed",
        )

    exit_code = int(getattr(result, "exit_code", 1))
    output = str(getattr(result, "output", "") or "")
    if exit_code != 0 or _ENGINE_ERROR.search(output):
        raise EngineAuthenticationError(
            f"{operation} version probe failed; the selected executable was not launched "
            "for the requested operation",
            status="engine_probe_failed",
        )

    lines = [line.strip() for line in output.splitlines() if line.strip()]
    versions = set(_REPORTED_VERSION.findall(lines[-1] if lines else ""))
    if len(versions) != 1:
        raise EngineAuthenticationError(
            f"{operation} could not determine one engine-reported patch version; "
            "refusing launch",
            status="engine_version_unknown",
        )
    reported = next(iter(versions))
    if reported != EXPECTED_GODOT_VERSION:
        raise EngineAuthenticationError(
            f"{operation} refused engine-reported Godot {reported} at {before.path}; "
            f"expected exactly {EXPECTED_GODOT_VERSION}",
            status="engine_version_mismatch",
        )

    after = _file_identity(before.path)
    if after != before:
        raise EngineAuthenticationError(
            "selected Godot changed during version authentication; refusing launch",
            status="engine_identity_changed",
        )
    return AuthenticatedEngine(
        path=before.path,
        version=reported,
        source=selected.source,
        identity=before,
    )


def _official_names(system_name: str) -> tuple[str, ...]:
    if system_name == "Windows":
        return (
            f"Godot_v{EXPECTED_GODOT_VERSION}-stable_win64_console.exe",
            f"Godot_v{EXPECTED_GODOT_VERSION}-stable_win64.exe",
        )
    if system_name == "Linux":
        return (
            f"Godot_v{EXPECTED_GODOT_VERSION}-stable_linux.x86_64",
            f"Godot_v{EXPECTED_GODOT_VERSION}-stable_linux.x86_64_console",
        )
    return ()


def _generic_names(system_name: str) -> tuple[str, ...]:
    if system_name == "Windows":
        return ("godot", "godot4", "Godot", "godot.exe", "Godot.exe")
    return ("godot", "godot4", "Godot")


def _official_directory(system_name: str) -> str | None:
    if system_name == "Windows":
        return f"Godot_v{EXPECTED_GODOT_VERSION}-stable_win64"
    if system_name == "Linux":
        return f"Godot_v{EXPECTED_GODOT_VERSION}-stable_linux.x86_64"
    return None


def _resolved_file(value: str | Path, root: Path) -> Path | None:
    raw = Path(value).expanduser()
    candidate = raw if raw.is_absolute() else root / raw
    try:
        return candidate.resolve(strict=True) if candidate.is_file() else None
    except OSError:
        return None


def _which_file(name: str, which: Callable[[str], str | None]) -> Path | None:
    found = which(name)
    if not found:
        return None
    try:
        candidate = Path(found).expanduser().resolve(strict=True)
    except OSError:
        return None
    return candidate if candidate.is_file() else None


def _candidate_directories(
    root: Path,
    environment: Mapping[str, str],
    system_name: str,
    requested: str | None,
) -> tuple[Path, ...]:
    candidates: list[Path] = []
    if requested:
        raw = Path(requested).expanduser()
        if raw.is_absolute() or raw.parent != Path("."):
            candidates.append(raw.parent if raw.is_absolute() else root / raw.parent)
    candidates.extend((root.parent, root))
    if system_name == "Windows":
        candidates.append(Path(r"C:\tools\godot"))
        program_files = environment.get("ProgramFiles")
        local_app_data = environment.get("LOCALAPPDATA")
        if program_files:
            candidates.append(Path(program_files) / "Godot")
        if local_app_data:
            candidates.append(Path(local_app_data) / "Programs" / "Godot")

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = os.path.normcase(str(candidate.resolve(strict=False)))
        except OSError:
            key = os.path.normcase(str(candidate))
        if key not in seen:
            seen.add(key)
            unique.append(candidate)
    return tuple(unique)


def _find_exact(
    root: Path,
    environment: Mapping[str, str],
    system_name: str,
    requested: str | None,
    which: Callable[[str], str | None],
) -> tuple[Path | None, str]:
    names = _official_names(system_name)
    directories = _candidate_directories(
        root, environment, system_name, requested
    )
    nested_directory = _official_directory(system_name)
    for name in names:
        for directory in directories:
            direct = _resolved_file(directory / name, root)
            if direct:
                requested_parent = (
                    Path(requested).expanduser().parent if requested else None
                )
                source = (
                    "environment-sibling"
                    if requested_parent is not None
                    and directory.resolve(strict=False)
                    == requested_parent.resolve(strict=False)
                    else "project-adjacent"
                )
                return direct, source
            if nested_directory:
                nested = _resolved_file(
                    directory / nested_directory / name,
                    root,
                )
                if nested:
                    return nested, "project-adjacent"
        found = _which_file(name, which)
        if found:
            return found, "path-exact"
    return None, ""


def select_godot(
    root: Path,
    *,
    environment: Mapping[str, str] | None = None,
    system_name: str | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> EngineSelection:
    """Select Godot without launching it.

    An explicit ``GODOT_BIN`` remains authoritative when its filename is
    unversioned or names the supported patch.  When it clearly names an older
    patch, an exact-version sibling or adjacent official binary is preferred.
    If no replacement exists, the known mismatch is returned so the caller can
    refuse before starting the native process.
    """
    canonical_root = root.resolve(strict=True)
    env = dict(os.environ if environment is None else environment)
    current_system = system_name or platform.system()
    requested = env.get("GODOT_BIN") or None
    explicit: Path | None = None
    if requested:
        explicit = _resolved_file(requested, canonical_root)
        if explicit is None and Path(requested).parent == Path("."):
            explicit = _which_file(requested, which)
        explicit_version = version_from_name(explicit or requested)
        if explicit and (
            explicit_version is None
            or explicit_version == EXPECTED_GODOT_VERSION
        ):
            return EngineSelection(
                explicit,
                "environment",
                requested,
                explicit_version,
                explicit_version,
            )
        exact, source = _find_exact(
            canonical_root, env, current_system, requested, which
        )
        if exact:
            return EngineSelection(
                exact,
                source,
                requested,
                explicit_version,
                version_from_name(exact),
            )
        if explicit:
            return EngineSelection(
                explicit,
                "environment",
                requested,
                explicit_version,
                explicit_version,
            )

    exact, source = _find_exact(
        canonical_root, env, current_system, requested, which
    )
    if exact:
        return EngineSelection(
            exact,
            source,
            requested,
            version_from_name(requested) if requested else None,
            version_from_name(exact),
        )

    for name in _generic_names(current_system):
        found = _which_file(name, which)
        if found:
            return EngineSelection(
                found,
                "path-generic",
                requested,
                version_from_name(requested) if requested else None,
                version_from_name(found),
            )

    if current_system == "Darwin":
        app_binary = _resolved_file(
            "/Applications/Godot.app/Contents/MacOS/Godot", canonical_root
        )
        if app_binary:
            return EngineSelection(app_binary, "known-location", requested)
    elif current_system == "Linux":
        for location in ("/usr/local/bin/godot", str(Path.home() / ".local/bin/godot")):
            binary = _resolved_file(location, canonical_root)
            if binary:
                return EngineSelection(binary, "known-location", requested)

    return EngineSelection(
        None,
        "not-found",
        requested,
        version_from_name(requested) if requested else None,
    )
