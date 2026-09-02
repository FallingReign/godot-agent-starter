#!/usr/bin/env python3
"""Select and launch one flat or managed agent-kit core without mutation.

This file is deliberately self-contained.  The installed copy lives at
``.agent-kit/launcher.py`` and must be able to select a verified release before
any release-owned module is imported.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Sequence

MARKER_NAME = ".agent-kit.json"
MARKER_KIND = "portable-agent-kit-root"
MARKER_SCHEMA = 1
MANAGED_DIRECTORY = ".agent-kit"
CURRENT_NAME = "current.json"
RELEASES_DIRECTORY = "releases"
MANIFEST_NAME = "RELEASE-MANIFEST.json"
INSTALL_MANIFEST_NAME = "INSTALL-MANIFEST.json"
CURRENT_SCHEMA = 1
CURRENT_KIND = "agent-kit-install-state"
MANIFEST_SCHEMA = 2
PROJECT_ROOT_ENV = "AGENT_KIT_PROJECT_ROOT"
CORE_ROOT_ENV = "AGENT_KIT_CORE_ROOT"
MAX_MARKER_BYTES = 16 * 1024
MAX_CURRENT_BYTES = 4 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_MEMBER_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 512

SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")
VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?\Z")
SAFE_MEMBER_RE = re.compile(r"[A-Za-z0-9._/-]+\Z")
INSTALLATION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")
SURFACE_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
MIGRATION_ID_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,119}\Z")
MANAGED_STRATEGIES = frozenset({
    "replace", "create-only", "schema-json", "managed-block", "owned-file",
})
LEGAL_FILES = frozenset({
    "LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "COPYING.md",
})
PROJECT_RECEIPT_TRUST = frozenset({
    "local-audit-matched",
    "portable-policy",
    "no-exact-authority-event",
    "not-applicable-no-project-state",
})


class LauncherError(ValueError):
    """The requested project cannot select one trusted kit core."""


@dataclass(frozen=True)
class Installation:
    project_root: Path
    core_root: Path
    marker_path: Path
    mode: str
    release_sha256: str | None = None
    current_path: Path | None = None
    manifest_path: Path | None = None
    version: str | None = None
    source_commit: str | None = None

    @property
    def kit_root(self) -> Path:
        """Compatibility alias for the project root."""
        return self.project_root

    @property
    def code_root(self) -> Path:
        """Compatibility alias for the selected core root."""
        return self.core_root


def _is_reparse(info: os.stat_result) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return bool(int(getattr(info, "st_file_attributes", 0)) & marker)


def _lstat(path: Path, label: str) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise LauncherError(f"{label} is unavailable: {path}: {exc}") from exc


def _require_directory(path: Path, label: str) -> None:
    info = _lstat(path, label)
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise LauncherError(f"{label} must be an unredirected directory: {path}")


def _require_regular_file(path: Path, label: str, *, maximum: int | None = None) -> bytes:
    info = _lstat(path, label)
    if (stat.S_ISLNK(info.st_mode) or _is_reparse(info)
            or not stat.S_ISREG(info.st_mode)):
        raise LauncherError(f"{label} must be an unredirected regular file: {path}")
    if int(getattr(info, "st_nlink", 1)) != 1:
        raise LauncherError(f"{label} may not be a hard link: {path}")
    if maximum is not None and info.st_size > maximum:
        raise LauncherError(f"{label} is too large: {path}")
    try:
        content = path.read_bytes()
    except OSError as exc:
        raise LauncherError(f"{label} cannot be read: {path}: {exc}") from exc
    after = _lstat(path, label)
    identity_fields = (
        "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_nlink",
    )
    before_identity = tuple(getattr(info, field, None) for field in identity_fields)
    after_identity = tuple(getattr(after, field, None) for field in identity_fields)
    if before_identity != after_identity or _is_reparse(after):
        raise LauncherError(f"{label} changed while it was being read: {path}")
    if len(content) != info.st_size:
        raise LauncherError(f"{label} changed while it was being read: {path}")
    return content


def _matching_names(parent: Path, expected: str, label: str) -> list[str]:
    try:
        matches = sorted(
            item.name for item in parent.iterdir()
            if item.name.casefold() == expected.casefold()
        )
    except OSError as exc:
        raise LauncherError(f"cannot inspect {label}: {parent}: {exc}") from exc
    if len(matches) > 1:
        raise LauncherError(
            f"{label} has a case collision for {expected!r}: {', '.join(matches)}"
        )
    if matches and matches[0] != expected:
        raise LauncherError(
            f"{label} uses unsafe casing for {expected!r}: {matches[0]!r}"
        )
    return matches


def _require_child(parent: Path, expected: str, label: str) -> Path:
    matches = _matching_names(parent, expected, label)
    if not matches:
        raise LauncherError(f"{label} is missing: {parent / expected}")
    return parent / expected


def _canonical_json(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _canonical_archive_sha256(
    members: Mapping[str, tuple[bytes, int]],
) -> str:
    """Return the canonical release identity for one exact member set."""
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.comment = b""
        for name in sorted(members):
            content, mode = members[name]
            info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | mode) << 16
            info.flag_bits |= 0x800
            archive.writestr(info, content, compress_type=zipfile.ZIP_STORED)
    return hashlib.sha256(payload.getvalue()).hexdigest()


def _json_object(content: bytes, label: str) -> dict:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LauncherError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LauncherError(f"{label} must contain one JSON object")
    return value


def _validate_marker(path: Path) -> None:
    value = _json_object(
        _require_regular_file(path, "kit marker", maximum=MAX_MARKER_BYTES),
        "kit marker",
    )
    if value.get("schema") != MARKER_SCHEMA or value.get("kind") != MARKER_KIND:
        raise LauncherError(
            f"kit marker must declare kind {MARKER_KIND!r} and schema {MARKER_SCHEMA}"
        )


def canonical_directory(
    value: str | os.PathLike[str], label: str = "directory"
) -> Path:
    """Return one existing directory only when no path component redirects it."""
    raw = Path(value).expanduser()
    candidate = (raw if raw.is_absolute() else Path.cwd() / raw).absolute()
    _require_directory(candidate, label)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise LauncherError(f"{label} cannot be resolved: {candidate}: {exc}") from exc
    if resolved != candidate:
        raise LauncherError(f"{label} may not use redirected components: {candidate}")
    return resolved


def _canonical_start(value: str | os.PathLike[str]) -> Path:
    return canonical_directory(value, "project path")


def locate_project_root(project: str | os.PathLike[str]) -> tuple[Path, Path]:
    """Return the nearest marked project root and its marker."""
    start = _canonical_start(project)
    for candidate in (start, *start.parents):
        matches = _matching_names(candidate, MARKER_NAME, "project marker")
        if matches:
            marker = candidate / MARKER_NAME
            _validate_marker(marker)
            return candidate, marker
    raise LauncherError(
        f"no {MARKER_NAME} marker found from {start} to the filesystem root"
    )


def _safe_member_parts(value: object) -> tuple[str, ...]:
    if (not isinstance(value, str) or not value or len(value) > 240
            or "\\" in value or "\x00" in value
            or not SAFE_MEMBER_RE.fullmatch(value)):
        raise LauncherError("release manifest contains an unsafe file path")
    path = PurePosixPath(value)
    if (path.is_absolute() or str(path) != value or not path.parts
            or any(part in ("", ".", "..") for part in path.parts)
            or ":" in path.parts[0]):
        raise LauncherError(f"release manifest contains an unsafe file path: {value!r}")
    return path.parts


def _member_file(core: Path, parts: tuple[str, ...]) -> Path:
    cursor = core
    rendered = "/".join(parts)
    for component in parts[:-1]:
        cursor = _require_child(cursor, component, f"release member {rendered}")
        _require_directory(cursor, f"release member directory {rendered}")
    result = _require_child(cursor, parts[-1], f"release member {rendered}")
    return result


def _validate_core_tree(core: Path, manifest_paths: Sequence[str]) -> None:
    """Reject anything in the selected core that the manifest did not name."""
    expected_files = set(manifest_paths) | {MANIFEST_NAME}
    expected_directories: set[str] = set()
    for relative in expected_files:
        parent = PurePosixPath(relative).parent
        while str(parent) != ".":
            expected_directories.add(parent.as_posix())
            parent = parent.parent

    actual_files: set[str] = set()
    actual_directories: set[str] = set()
    pending: list[tuple[Path, str]] = [(core, "")]
    inspected = 0
    while pending:
        directory, prefix = pending.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise LauncherError(
                f"release core cannot be enumerated: {directory}: {exc}"
            ) from exc
        folded: set[str] = set()
        for child in children:
            inspected += 1
            if inspected > MAX_MEMBERS * 4:
                raise LauncherError("release core contains too many entries")
            if child.name.casefold() in folded:
                raise LauncherError(
                    f"release core contains a case collision: {child.name}"
                )
            folded.add(child.name.casefold())
            relative = f"{prefix}/{child.name}" if prefix else child.name
            info = _lstat(child, f"release core entry {relative}")
            if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                raise LauncherError(
                    f"release core entry must not be redirected: {relative}"
                )
            if stat.S_ISDIR(info.st_mode):
                actual_directories.add(relative)
                pending.append((child, relative))
            elif stat.S_ISREG(info.st_mode):
                if int(getattr(info, "st_nlink", 1)) != 1:
                    raise LauncherError(
                        f"release core entry may not be a hard link: {relative}"
                    )
                actual_files.add(relative)
            else:
                raise LauncherError(
                    f"release core contains a non-regular entry: {relative}"
                )

    extra_files = sorted(actual_files - expected_files)
    missing_files = sorted(expected_files - actual_files)
    extra_directories = sorted(actual_directories - expected_directories)
    missing_directories = sorted(expected_directories - actual_directories)
    if extra_files or missing_files or extra_directories or missing_directories:
        details: list[str] = []
        if extra_files:
            details.append("unlisted file " + extra_files[0])
        if missing_files:
            details.append("missing file " + missing_files[0])
        if extra_directories:
            details.append("unlisted directory " + extra_directories[0])
        if missing_directories:
            details.append("missing directory " + missing_directories[0])
        raise LauncherError("release core member set differs: " + "; ".join(details))


def _validate_manifest(core: Path, release: dict, manifest_path: Path) -> dict:
    manifest_content = _require_regular_file(
        manifest_path, "release manifest", maximum=MAX_MANIFEST_BYTES
    )
    actual_manifest_sha = hashlib.sha256(manifest_content).hexdigest()
    if actual_manifest_sha != release["release_manifest_sha256"]:
        raise LauncherError("release manifest SHA-256 does not match current.json")
    manifest = _json_object(manifest_content, "release manifest")
    if manifest_content != _canonical_json(manifest):
        raise LauncherError("release manifest is not canonically encoded")
    expected_keys = {
        "schema", "version", "source", "authority_evidence", "license_files",
        "normalization", "files",
    }
    if set(manifest) != expected_keys or manifest.get("schema") != MANIFEST_SCHEMA:
        raise LauncherError("release manifest fields or schema are malformed")
    if manifest.get("version") != release["kit_version"]:
        raise LauncherError("release version does not match current.json")
    source = manifest.get("source")
    if (not isinstance(source, dict) or set(source) != {"commit", "dirty"}
            or source.get("commit") != release["source_commit"]
            or source.get("dirty") is not False):
        raise LauncherError("release source does not match current.json")
    authority = manifest.get("authority_evidence")
    if (not isinstance(authority, dict) or set(authority) != {
            "receipt_trust", "identity_model", "project_receipt_trust"
            }
            or authority.get("receipt_trust") != "portable-policy"
            or authority.get("identity_model") != "portable-policy-audit"
            or authority.get("project_receipt_trust") not in PROJECT_RECEIPT_TRUST):
        raise LauncherError("release authority evidence is malformed")
    if manifest.get("normalization") != {
        "line_endings": "lf",
        "regular_mode": "0644",
        "executable_mode": "0755",
        "timestamps": "fixed",
    }:
        raise LauncherError("release normalization metadata is malformed")

    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > MAX_MEMBERS:
        raise LauncherError("release manifest file list is malformed")
    paths: list[str] = []
    folded_paths: set[str] = set()
    total_bytes = 0
    entries: list[tuple[dict, tuple[str, ...]]] = []
    for entry in files:
        if (not isinstance(entry, dict)
                or set(entry) != {"path", "bytes", "sha256", "mode"}):
            raise LauncherError("release manifest contains a malformed file entry")
        parts = _safe_member_parts(entry.get("path"))
        rendered = "/".join(parts)
        if rendered == MANIFEST_NAME:
            raise LauncherError("release manifest may not list itself as a member")
        folded = rendered.casefold()
        if folded in folded_paths:
            raise LauncherError(f"release manifest has a case collision: {rendered}")
        folded_paths.add(folded)
        size = entry.get("bytes")
        if (not isinstance(size, int) or isinstance(size, bool)
                or size < 0 or size > MAX_MEMBER_BYTES):
            raise LauncherError(f"release manifest has an invalid size: {rendered}")
        if not SHA256_RE.fullmatch(str(entry.get("sha256") or "")):
            raise LauncherError(f"release manifest has an invalid SHA-256: {rendered}")
        if entry.get("mode") not in ("0644", "0755"):
            raise LauncherError(f"release manifest has an invalid mode: {rendered}")
        expected_mode = "0755" if rendered.endswith(".py") or rendered == "kit" else "0644"
        if entry.get("mode") != expected_mode:
            raise LauncherError(f"release manifest has an unsafe mode: {rendered}")
        total_bytes += size
        if total_bytes > MAX_TOTAL_BYTES:
            raise LauncherError("release manifest exceeds the total size limit")
        paths.append(rendered)
        entries.append((entry, parts))
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise LauncherError("release manifest paths must be unique and sorted")
    if "kit.py" not in paths:
        raise LauncherError("release manifest does not contain kit.py")
    licenses = manifest.get("license_files")
    if (not isinstance(licenses, list) or not licenses or licenses != sorted(licenses)
            or len(licenses) != len(set(licenses))
            or any(not isinstance(item, str) or item not in LEGAL_FILES or item not in paths
                   for item in licenses)):
        raise LauncherError("release legal metadata is malformed")

    _validate_core_tree(core, paths)

    archive_members: dict[str, tuple[bytes, int]] = {
        MANIFEST_NAME: (manifest_content, 0o644)
    }
    for entry, parts in entries:
        rendered = "/".join(parts)
        path = _member_file(core, parts)
        content = _require_regular_file(
            path, f"release member {rendered}", maximum=MAX_MEMBER_BYTES
        )
        if len(content) != entry["bytes"]:
            raise LauncherError(f"release member size does not match: {rendered}")
        if hashlib.sha256(content).hexdigest() != entry["sha256"]:
            raise LauncherError(f"release member SHA-256 does not match: {rendered}")
        if os.name != "nt":
            actual_mode = format(stat.S_IMODE(path.lstat().st_mode), "04o")
            if actual_mode != entry["mode"]:
                raise LauncherError(f"release member mode does not match: {rendered}")
        if b"\r" in content:
            raise LauncherError(f"release member does not use LF line endings: {rendered}")
        archive_members[rendered] = (content, int(entry["mode"], 8))
    if _canonical_archive_sha256(archive_members) != release["archive_sha256"]:
        raise LauncherError("release member set does not match archive_sha256")
    return manifest


def _validate_release_state(value: object, label: str) -> dict:
    expected = {
        "kit_version",
        "archive_sha256",
        "release_manifest_sha256",
        "install_manifest_sha256",
        "source_commit",
        "core_path",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise LauncherError(f"{label} fields are malformed")
    archive_sha = value.get("archive_sha256")
    if not VERSION_RE.fullmatch(str(value.get("kit_version") or "")):
        raise LauncherError(f"{label} kit_version is malformed")
    for field in (
        "archive_sha256", "release_manifest_sha256", "install_manifest_sha256"
    ):
        if not SHA256_RE.fullmatch(str(value.get(field) or "")):
            raise LauncherError(f"{label} {field} is malformed")
    if not COMMIT_RE.fullmatch(str(value.get("source_commit") or "")):
        raise LauncherError(f"{label} source_commit is malformed")
    expected_core = f"{MANAGED_DIRECTORY}/{RELEASES_DIRECTORY}/{archive_sha}"
    if value.get("core_path") != expected_core:
        raise LauncherError(f"{label} core_path does not match archive_sha256")
    return value


def _validate_managed_surfaces(value: object) -> None:
    if not isinstance(value, list):
        raise LauncherError("current.json managed_surfaces must be a list")
    identifiers: list[str] = []
    folded_paths: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "id", "path", "strategy", "base_sha256", "applied_sha256"
        }:
            raise LauncherError("current.json contains a malformed managed surface")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not SURFACE_ID_RE.fullmatch(identifier):
            raise LauncherError("current.json contains an unsafe managed surface id")
        parts = _safe_member_parts(item.get("path"))
        rendered = "/".join(parts)
        folded = rendered.casefold()
        if folded in folded_paths:
            raise LauncherError(f"current.json managed surface path collision: {rendered}")
        folded_paths.add(folded)
        if item.get("strategy") not in MANAGED_STRATEGIES:
            raise LauncherError(f"current.json managed surface strategy is malformed: {identifier}")
        for field in ("base_sha256", "applied_sha256"):
            if not SHA256_RE.fullmatch(str(item.get(field) or "")):
                raise LauncherError(
                    f"current.json managed surface {field} is malformed: {identifier}"
                )
        identifiers.append(identifier)
    if identifiers != sorted(identifiers) or len(identifiers) != len(set(identifiers)):
        raise LauncherError("current.json managed surfaces must have sorted unique ids")


def _validate_applied_migrations(value: object) -> None:
    if not isinstance(value, list):
        raise LauncherError("current.json applied_migrations must be a list")
    identifiers: list[str] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "id", "input_sha256", "output_sha256"
        }:
            raise LauncherError("current.json contains a malformed applied migration")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not MIGRATION_ID_RE.fullmatch(identifier):
            raise LauncherError("current.json contains an unsafe migration id")
        for field in ("input_sha256", "output_sha256"):
            if not SHA256_RE.fullmatch(str(item.get(field) or "")):
                raise LauncherError(
                    f"current.json migration {field} is malformed: {identifier}"
                )
        identifiers.append(identifier)
    if identifiers != sorted(identifiers) or len(identifiers) != len(set(identifiers)):
        raise LauncherError("current.json migrations must have sorted unique ids")


def _validate_current(project_root: Path, managed_root: Path) -> Installation:
    current_path = _require_child(managed_root, CURRENT_NAME, "managed current pointer")
    current_content = _require_regular_file(
        current_path, "managed current pointer", maximum=MAX_CURRENT_BYTES
    )
    current = _json_object(current_content, "managed current pointer")
    expected = {
        "schema",
        "kind",
        "installation_id",
        "install_schema",
        "layout_schema",
        "config_schema",
        "active_release",
        "previous_release",
        "managed_surfaces",
        "applied_migrations",
    }
    if (set(current) != expected or current.get("schema") != CURRENT_SCHEMA
            or current.get("kind") != CURRENT_KIND):
        raise LauncherError("current.json fields or schema are malformed")
    if current_content != _canonical_json(current):
        raise LauncherError("current.json is not canonically encoded")
    if not INSTALLATION_ID_RE.fullmatch(str(current.get("installation_id") or "")):
        raise LauncherError("current.json installation_id is malformed")
    for field in ("install_schema", "layout_schema", "config_schema"):
        field_value = current.get(field)
        if (not isinstance(field_value, int) or isinstance(field_value, bool)
                or field_value <= 0):
            raise LauncherError(f"current.json {field} must be a positive integer")
    active = _validate_release_state(current.get("active_release"), "active release")
    previous = current.get("previous_release")
    if previous is not None:
        _validate_release_state(previous, "previous release")
    _validate_managed_surfaces(current.get("managed_surfaces"))
    _validate_applied_migrations(current.get("applied_migrations"))
    release_sha = active["archive_sha256"]

    releases = _require_child(managed_root, RELEASES_DIRECTORY, "managed releases")
    _require_directory(releases, "managed releases")
    core = _require_child(releases, release_sha, "selected release")
    _require_directory(core, "selected release")
    manifest_path = _require_child(core, MANIFEST_NAME, "release manifest")
    _validate_manifest(core, active, manifest_path)
    install_manifest_path = _require_child(
        core, INSTALL_MANIFEST_NAME, "install manifest"
    )
    install_manifest_content = _require_regular_file(
        install_manifest_path, "install manifest", maximum=MAX_MANIFEST_BYTES
    )
    if hashlib.sha256(install_manifest_content).hexdigest() != active["install_manifest_sha256"]:
        raise LauncherError("install manifest SHA-256 does not match current.json")
    return Installation(
        project_root=project_root,
        core_root=core,
        marker_path=project_root / MARKER_NAME,
        mode="managed",
        release_sha256=release_sha,
        current_path=current_path,
        manifest_path=manifest_path,
        version=active["kit_version"],
        source_commit=active["source_commit"],
    )


def resolve_installation(project: str | os.PathLike[str]) -> Installation:
    """Resolve one project to its flat source or verified managed core."""
    project_root, marker_path = locate_project_root(project)
    managed_matches = _matching_names(
        project_root, MANAGED_DIRECTORY, "managed kit directory"
    )
    if not managed_matches:
        return Installation(
            project_root=project_root,
            core_root=project_root,
            marker_path=marker_path,
            mode="flat",
        )
    managed_root = project_root / MANAGED_DIRECTORY
    _require_directory(managed_root, "managed kit directory")
    return _validate_current(project_root, managed_root)


def _same_path(value: str, expected: Path, label: str) -> None:
    try:
        candidate = _canonical_start(value)
    except LauncherError as exc:
        raise LauncherError(f"bound {label} is unavailable: {value}: {exc}") from exc
    if candidate != expected:
        raise LauncherError(
            f"bound {label} does not match selected project: {candidate} != {expected}"
        )


def bound_environment(
    installation: Installation, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Return a child environment bound to the selected project and core."""
    environment = dict(os.environ if base is None else base)
    existing_project = environment.get(PROJECT_ROOT_ENV)
    existing_core = environment.get(CORE_ROOT_ENV)
    if bool(existing_project) != bool(existing_core):
        raise LauncherError(
            f"{PROJECT_ROOT_ENV} and {CORE_ROOT_ENV} must be supplied together"
        )
    if existing_project:
        _same_path(existing_project, installation.project_root, "project root")
    if existing_core:
        _same_path(existing_core, installation.core_root, "core root")
    environment[PROJECT_ROOT_ENV] = str(installation.project_root)
    environment[CORE_ROOT_ENV] = str(installation.core_root)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _looks_like_release_root(path: Path) -> bool:
    return (
        SHA256_RE.fullmatch(path.name) is not None
        and path.parent.name == RELEASES_DIRECTORY
        and path.parent.parent.name == MANAGED_DIRECTORY
    )


def resolve_bound_installation(
    core_root: str | os.PathLike[str],
    environment: Mapping[str, str] | None = None,
) -> Installation:
    """Resolve a consumer's core without trusting process environment paths.

    Callers pass the core directory derived from their own ``__file__``.  A
    managed core is accepted only when both launcher bindings are present,
    resolve back through ``current.json``, and select that exact directory.
    """
    actual_core = _canonical_start(core_root)
    values = os.environ if environment is None else environment
    bound_project = values.get(PROJECT_ROOT_ENV)
    bound_core = values.get(CORE_ROOT_ENV)
    if bool(bound_project) != bool(bound_core):
        raise LauncherError(
            f"{PROJECT_ROOT_ENV} and {CORE_ROOT_ENV} must be supplied together"
        )
    if bound_project and bound_core:
        selected = resolve_installation(bound_project)
        _same_path(bound_core, selected.core_root, "core root")
        if actual_core != selected.core_root:
            raise LauncherError(
                f"running core does not match selected core: {actual_core} != {selected.core_root}"
            )
        return selected
    if _looks_like_release_root(actual_core):
        raise LauncherError("a managed core requires validated launcher bindings")
    selected = resolve_installation(actual_core)
    if (selected.mode != "flat" or selected.project_root != actual_core
            or selected.core_root != actual_core):
        raise LauncherError("an unbound core must be the flat marked project root")
    return selected


def _project_argument(arguments: Sequence[str]) -> str | None:
    found: list[str] = []
    index = 0
    while index < len(arguments):
        item = arguments[index]
        if item == "--project":
            if index + 1 >= len(arguments) or not arguments[index + 1]:
                raise LauncherError("--project requires one directory")
            found.append(arguments[index + 1])
            index += 2
            continue
        if item.startswith("--project="):
            value = item.partition("=")[2]
            if not value:
                raise LauncherError("--project requires one directory")
            found.append(value)
        elif item.startswith("--") and "--project".startswith(item):
            raise LauncherError("spell --project in full so the launcher can bind it safely")
        index += 1
    if len(found) > 1:
        raise LauncherError("--project may be supplied only once")
    return found[0] if found else None


def _default_project() -> Path:
    location = Path(__file__).resolve()
    core = location.parent.parent
    if _looks_like_release_root(core):
        raise LauncherError(
            "a managed release cannot be launched directly; use the project kit launcher"
        )
    return core


def launch(arguments: Sequence[str] | None = None) -> int:
    """Select the core, then start it without changing any project file."""
    forwarded = list(sys.argv[1:] if arguments is None else arguments)
    project = _project_argument(forwarded) or str(_default_project())
    installation = resolve_installation(project)
    entrypoint = _member_file(installation.core_root, ("kit.py",))
    _require_regular_file(entrypoint, "kit entry point", maximum=MAX_MEMBER_BYTES)
    environment = bound_environment(installation)
    try:
        completed = subprocess.run(
            [sys.executable, str(entrypoint), *forwarded],
            cwd=installation.project_root,
            env=environment,
            check=False,
        )
    except OSError as exc:
        raise LauncherError(f"kit process could not start: {exc}") from exc
    return int(completed.returncode)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return launch(argv)
    except LauncherError as exc:
        print(f"kit: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
