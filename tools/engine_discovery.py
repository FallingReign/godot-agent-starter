#!/usr/bin/env python3
"""Read-only selection of the Godot executable used by public kit commands.

The selected engine is still authenticated by ``check.py --headless --version``
before any project stage runs.  This module only resolves paths without starting
an executable, so ``kit doctor`` can remain offline and safe.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

EXPECTED_GODOT_VERSION = "4.7.2"

_VERSIONED_NAME = re.compile(
    r"(?i)godot(?:[_-]?v)?(?P<version>\d+\.\d+\.\d+)"
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
