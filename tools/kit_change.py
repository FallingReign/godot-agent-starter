#!/usr/bin/env python3
"""Transactional installation and upgrade support for the portable agent kit.

The module deliberately owns no command-line interface.  Public launchers can
present the small user workflow while this file keeps one safety contract:

* ``preview`` is read-only and binds every proposed write to one SHA-256;
* ``apply`` re-creates that preview under an OS lock before writing;
* exact prior bytes are durable before the first project-facing mutation;
* an interrupted pre-activation change rolls back, while an activated change
  can be resumed without guessing;
* rollback never overwrites a third version of a file.

No function in this module starts Godot, uses the network, or mutates Git.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping

try:
    from tools import managed_launcher, process_supervisor, release
except ImportError:  # pragma: no cover - direct execution/import from tools/
    import managed_launcher  # type: ignore[no-redef]
    import process_supervisor  # type: ignore[no-redef]
    import release  # type: ignore[no-redef]


INSTALL_MANIFEST = "INSTALL-MANIFEST.json"
RELEASE_MANIFEST = "RELEASE-MANIFEST.json"
MARKER = ".agent-kit.json"
LEGACY_VERSION = "VERSION"
MANAGED_ROOT = ".agent-kit"
STABLE_LAUNCHER_PATH = ".agent-kit/launcher.py"
CURRENT_STATE = ".agent-kit/current.json"
RELEASES_ROOT = ".agent-kit/releases"
UPGRADE_ROOT = ".kit/runtime/upgrade"
TRANSACTIONS_ROOT = ".kit/runtime/upgrade/transactions"
CHANGE_LOCK = ".kit/runtime/upgrade/change.lock"

INSTALL_SCHEMA = 1
PREVIEW_SCHEMA = 1
STATE_SCHEMA = 1
BACKUP_SCHEMA = 1
JOURNAL_SCHEMA = 2
MAX_MANAGED_FILE_BYTES = 4 * 1024 * 1024
MAX_BACKUP_BYTES = 64 * 1024 * 1024
MAX_BACKUP_ENTRIES = 1024
MAX_HISTORY = 32
MAX_PROJECT_SCAN_ENTRIES = 20_000
MAX_PROJECT_SCAN_DEPTH = 12

SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
TRANSACTION_RE = re.compile(r"[0-9a-f]{32}\Z")
VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?\Z")
SAFE_PATH_RE = re.compile(r"[A-Za-z0-9._/-]+\Z")
MODE_RE = re.compile(r"0[0-7]{3}\Z")
CONFIG_KEY_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,79}\Z")

CREATE_ONLY_PATHS = frozenset({".gdlintrc", "ARCHITECTURE.md", "arch.rules.json"})
SCHEMA_JSON_PATH = "kit.config.json"
PROJECT_SETTING_PATHS = frozenset({"export_presets.cfg", "project.godot"})
RETIRED_PROTECTED_PATHS = frozenset({
    "COPYING",
    "COPYING.md",
    INSTALL_MANIFEST,
    "LICENSE",
    "LICENSE.md",
    "LICENSE.txt",
    "README.md",
    "VERSION",
    "project.shape.json",
    "proposal.json",
    RELEASE_MANIFEST,
})
RETIRED_PROTECTED_PREFIXES = (
    ".godot/",
    ".kit/",
    "docs/design/",
    "docs/retro/",
    "src/",
)
PROJECT_SETTING_CASEFOLDS = frozenset(path.casefold() for path in PROJECT_SETTING_PATHS)
RETIRED_PROTECTED_CASEFOLDS = frozenset(path.casefold() for path in RETIRED_PROTECTED_PATHS)
PROJECT_SCAN_SKIP_NAMES = frozenset({
    ".agent-kit",
    ".git",
    ".godot",
    ".kit",
    "node_modules",
})
GAME_ROOT_RESERVED_NAMES = frozenset({
    ".agent-kit",
    ".agents",
    ".checklogs",
    ".git",
    ".github",
    ".godot",
    ".kit",
    "docs",
    "tools",
})
SURFACE_PROTECTED_PREFIXES = (
    ".agent-kit/",
    ".godot/",
    ".kit/",
    "docs/design/",
    "docs/retro/",
    "src/",
)

TERMINAL_STATES = frozenset({"applied", "completed", "rolled_back"})
JOURNAL_STATES = frozenset({
    "prepared",
    "core_staged",
    "applying",
    "activated",
    "applied",
    "completed",
    "rolling_back",
    "rolled_back",
    "blocked",
})


class KitChangeError(RuntimeError):
    """A kit change was refused without hiding its actionable reason."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail

    def as_json(self) -> dict[str, str | bool]:
        return {"ok": False, "code": self.code, "detail": self.detail}


@dataclass(frozen=True)
class Surface:
    id: str
    path: str
    strategy: str
    source: str
    mode: str
    legacy_sha256: str | None
    begin: str | None = None
    end: str | None = None
    schema_key: str | None = None
    target_schema: int | None = None
    supported_schemas: tuple[int, ...] = ()
    defaults: Mapping[str, Any] | None = None
    allowed_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class RetiredFile:
    path: str
    sha256: str


@dataclass
class ChangePlan:
    root: Path
    preview: dict[str, Any]
    report: dict[str, Any]
    members: Mapping[str, Any]
    manifest: dict[str, Any]
    replacements: dict[str, bytes | None]
    modes: dict[str, str | None]
    current: dict[str, Any]
    target_state: bytes
    noop: bool


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise KitChangeError("path-unreadable", f"cannot inspect {path}: {exc}") from exc
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return bool(int(getattr(info, "st_file_attributes", 0)) & marker)


def _safe_relative(raw: object, *, label: str = "path") -> str:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise KitChangeError("unsafe-path", f"{label} is not a portable relative path")
    if len(raw) > 240 or not SAFE_PATH_RE.fullmatch(raw):
        raise KitChangeError("unsafe-path", f"{label} uses unsupported characters: {raw!r}")
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or any(part in ("", ".", "..") for part in candidate.parts):
        raise KitChangeError("unsafe-path", f"{label} escapes the project: {raw!r}")
    if candidate.parts and ":" in candidate.parts[0]:
        raise KitChangeError("unsafe-path", f"{label} is absolute: {raw!r}")
    normal = candidate.as_posix()
    if normal != raw:
        raise KitChangeError("unsafe-path", f"{label} is not canonical: {raw!r}")
    return normal


def _canonical_root(root: Path) -> Path:
    try:
        return managed_launcher.canonical_directory(root, "project root")
    except managed_launcher.LauncherError as exc:
        raise KitChangeError("unsafe-project", str(exc)) from exc


def _case_checked_child(parent: Path, component: str, relative: str) -> Path:
    if parent.exists():
        try:
            collisions = [
                child.name
                for child in parent.iterdir()
                if child.name.casefold() == component.casefold() and child.name != component
            ]
        except OSError as exc:
            raise KitChangeError("path-unreadable", f"cannot inspect {relative}: {exc}") from exc
        if collisions:
            raise KitChangeError(
                "path-case-collision",
                f"{relative} collides with existing path {collisions[0]!r}",
            )
    return parent / component


def _target(root: Path, relative: str, *, leaf: str = "file") -> Path:
    portable = _safe_relative(relative)
    cursor = root
    parts = PurePosixPath(portable).parts
    for index, component in enumerate(parts):
        cursor = _case_checked_child(cursor, component, portable)
        if not cursor.exists() and not cursor.is_symlink():
            continue
        if cursor.is_symlink() or _is_reparse(cursor):
            raise KitChangeError("redirected-path", f"refusing redirected path: {portable}")
        try:
            info = cursor.lstat()
        except OSError as exc:
            raise KitChangeError("path-unreadable", f"cannot inspect {portable}: {exc}") from exc
        final = index == len(parts) - 1
        if not final and not stat.S_ISDIR(info.st_mode):
            raise KitChangeError("unsafe-path", f"parent is not a directory: {portable}")
        if final and leaf == "file" and not stat.S_ISREG(info.st_mode):
            raise KitChangeError("unsafe-path", f"target is not a regular file: {portable}")
        if final and leaf == "directory" and not stat.S_ISDIR(info.st_mode):
            raise KitChangeError("unsafe-path", f"target is not a regular directory: {portable}")
    try:
        cursor.resolve(strict=False).relative_to(root)
    except (OSError, ValueError) as exc:
        raise KitChangeError("unsafe-path", f"target escapes the project: {portable}") from exc
    return cursor


def _safe_game_root(root: Path, raw: object) -> str:
    if raw == ".":
        return "."
    relative = _safe_relative(raw, label="game_root")
    first = PurePosixPath(relative).parts[0].casefold()
    if first in GAME_ROOT_RESERVED_NAMES:
        raise KitChangeError("game-root-invalid", f"game_root is reserved: {relative}")
    target = _target(root, relative, leaf="directory")
    if target.exists() and not target.is_dir():
        raise KitChangeError("game-root-invalid", f"game_root is not a directory: {relative}")
    return relative


def _discover_game_root(root: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    blockers: list[dict[str, Any]] = []
    try:
        root_project = _target(root, "project.godot")
        if root_project.exists():
            return {"value": ".", "source": "root-project"}, blockers
    except KitChangeError as exc:
        return {"value": None, "source": "unresolved"}, [
            {"code": exc.code, "path": "project.godot", "detail": exc.detail}
        ]

    candidates: list[str] = []
    inspected = 0
    try:
        for current, directories, files in os.walk(root, topdown=True, followlinks=False):
            base = Path(current)
            relative_base = base.relative_to(root)
            depth = len(relative_base.parts)
            kept: list[str] = []
            for name in sorted(directories):
                inspected += 1
                child = base / name
                if (
                    name.casefold() in PROJECT_SCAN_SKIP_NAMES
                    or depth >= MAX_PROJECT_SCAN_DEPTH
                    or child.is_symlink()
                    or _is_reparse(child)
                ):
                    continue
                kept.append(name)
            directories[:] = kept
            inspected += len(files)
            if inspected > MAX_PROJECT_SCAN_ENTRIES:
                return {"value": None, "source": "unresolved"}, [{
                    "code": "game-root-scan-bounded",
                    "path": None,
                    "detail": "project search reached its safety limit; choose game_root explicitly",
                }]
            for name in sorted(files):
                if name.casefold() != "project.godot":
                    continue
                relative_file = (relative_base / name).as_posix()
                if name != "project.godot":
                    return {"value": None, "source": "unresolved"}, [{
                        "code": "path-case-collision",
                        "path": relative_file,
                        "detail": "project.godot uses non-portable casing",
                    }]
                target = _target(root, relative_file)
                if target.is_symlink() or _is_reparse(target) or not target.is_file():
                    return {"value": None, "source": "unresolved"}, [{
                        "code": "game-root-project-unsafe",
                        "path": relative_file,
                        "detail": "project.godot is not a regular unredirected file",
                    }]
                parent = relative_base.as_posix()
                candidates.append("." if parent == "." else parent)
    except (OSError, KitChangeError) as exc:
        detail = exc.detail if isinstance(exc, KitChangeError) else str(exc)
        code = exc.code if isinstance(exc, KitChangeError) else "game-root-scan-failed"
        return {"value": None, "source": "unresolved"}, [
            {"code": code, "path": None, "detail": detail}
        ]
    candidates = sorted(set(candidates))
    if len(candidates) > 1:
        return {"value": None, "source": "unresolved"}, [{
            "code": "game-root-ambiguous",
            "path": None,
            "detail": "multiple Godot projects exist; choose game_root explicitly",
        }]
    if candidates:
        return {"value": candidates[0], "source": "unique-project"}, blockers
    return {"value": "src", "source": "new-project-default"}, blockers


def _select_game_root(
    root: Path, requested: str | None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    explicit = _safe_game_root(root, requested) if requested is not None else None
    try:
        config_path = _target(root, SCHEMA_JSON_PATH)
    except KitChangeError as exc:
        return {"value": None, "source": "unresolved"}, [
            {"code": exc.code, "path": SCHEMA_JSON_PATH, "detail": exc.detail}
        ]
    if config_path.exists():
        try:
            existing = _strict_json(
                _stable_bytes(config_path),
                code="schema-json-invalid",
                label=SCHEMA_JSON_PATH,
            )
            if not isinstance(existing, dict):
                raise KitChangeError(
                    "schema-json-invalid", f"{SCHEMA_JSON_PATH} must contain an object"
                )
            configured = existing.get("game_root")
            if configured is not None:
                selected = _safe_game_root(root, configured)
                blockers: list[dict[str, Any]] = []
                if explicit is not None and explicit != selected:
                    blockers.append({
                        "code": "game-root-choice-conflict",
                        "path": SCHEMA_JSON_PATH,
                        "detail": "explicit game_root conflicts with the existing project configuration",
                    })
                return {"value": selected, "source": "existing-config"}, blockers
        except KitChangeError as exc:
            return {"value": None, "source": "unresolved"}, [
                {"code": exc.code, "path": SCHEMA_JSON_PATH, "detail": exc.detail}
            ]
    if explicit is not None:
        return {"value": explicit, "source": "explicit"}, []
    return _discover_game_root(root)


def _stable_bytes(path: Path, *, limit: int = MAX_MANAGED_FILE_BYTES) -> bytes:
    try:
        path_before = path.lstat()
        if (
            stat.S_ISLNK(path_before.st_mode)
            or _is_reparse(path)
            or not stat.S_ISREG(path_before.st_mode)
        ):
            raise KitChangeError(
                "unsafe-path", f"managed target is not a regular file: {path}"
            )
        if int(getattr(path_before, "st_nlink", 1)) != 1:
            raise KitChangeError(
                "unsafe-hardlink", f"managed target may not be a hard link: {path}"
            )
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            content = handle.read(limit + 1)
            after = os.fstat(handle.fileno())
        path_after = path.lstat()
    except KitChangeError:
        raise
    except OSError as exc:
        raise KitChangeError("file-unreadable", f"cannot read {path}: {exc}") from exc
    if len(content) > limit:
        raise KitChangeError("file-too-large", f"managed file exceeds {limit} bytes: {path}")
    path_identity_fields = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_size",
        "st_mtime_ns",
        "st_nlink",
    )
    handle_identity_fields = (
        "st_dev",
        "st_ino",
        "st_size",
        "st_mtime_ns",
        "st_nlink",
    )
    stable_path_identity_fields = (*path_identity_fields, "st_ctime_ns")
    stable_handle_identity_fields = (*handle_identity_fields, "st_ctime_ns")
    path_before_identity = tuple(
        getattr(path_before, field, None) for field in stable_path_identity_fields
    )
    path_handle_identity = tuple(
        getattr(path_before, field, None) for field in handle_identity_fields
    )
    before_path_identity = tuple(
        getattr(before, field, None) for field in handle_identity_fields
    )
    before_identity = tuple(
        getattr(before, field, None) for field in stable_handle_identity_fields
    )
    after_identity = tuple(
        getattr(after, field, None) for field in stable_handle_identity_fields
    )
    path_after_identity = tuple(
        getattr(path_after, field, None) for field in stable_path_identity_fields
    )
    if (
        path_before_identity != path_after_identity
        or path_handle_identity != before_path_identity
        or before_identity != after_identity
        or _is_reparse(path)
        or len(content) != before.st_size
    ):
        raise KitChangeError("file-changed", f"file changed while being read: {path}")
    return content


def _snapshot(path: Path) -> dict[str, Any]:
    if not path.exists() and not path.is_symlink():
        return {"kind": "absent"}
    if path.is_symlink() or _is_reparse(path) or not path.is_file():
        raise KitChangeError("unsafe-path", f"managed target is not a regular file: {path}")
    content = _stable_bytes(path)
    if os.name == "nt":
        mode = "0444" if not os.access(path, os.W_OK) else "0644"
    else:
        mode = format(stat.S_IMODE(path.stat().st_mode), "04o")
    return {
        "kind": "file",
        "bytes": len(content),
        "sha256": _sha256(content),
        "mode": mode,
    }


def _validate_snapshot(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise KitChangeError("journal-invalid", "file state must be an object")
    if value.get("kind") == "absent":
        if set(value) != {"kind"}:
            raise KitChangeError("journal-invalid", "absent file state is malformed")
        return value
    if set(value) != {"kind", "bytes", "sha256", "mode"} or value.get("kind") != "file":
        raise KitChangeError("journal-invalid", "file state is malformed")
    if (
        not isinstance(value.get("bytes"), int)
        or isinstance(value.get("bytes"), bool)
        or not 0 <= value["bytes"] <= MAX_MANAGED_FILE_BYTES
        or not SHA256_RE.fullmatch(str(value.get("sha256") or ""))
        or not MODE_RE.fullmatch(str(value.get("mode") or ""))
    ):
        raise KitChangeError("journal-invalid", "file state values are malformed")
    return value


def _snapshot_matches(path: Path, expected: dict[str, Any]) -> bool:
    try:
        return _snapshot(path) == expected
    except KitChangeError:
        return False


def _member_bytes(members: Mapping[str, Any], name: str) -> bytes:
    member = members.get(name)
    content = getattr(member, "content", None)
    if member is None or not isinstance(content, bytes):
        raise KitChangeError("release-invalid", f"verified release member is missing: {name}")
    return content


def _member_mode(members: Mapping[str, Any], name: str) -> int:
    member = members.get(name)
    mode = getattr(member, "mode", None)
    if not isinstance(mode, int):
        raise KitChangeError("release-invalid", f"verified release mode is missing: {name}")
    return mode


def _read_verified_archive(path: Path) -> tuple[dict[str, Any], Mapping[str, Any]]:
    """Adapter for the public API root will expose, with current compatibility."""
    reader = getattr(release, "read_verified_archive", None)
    if reader is None:
        reader = getattr(release, "_verified_archive", None)
    if reader is None:
        raise KitChangeError("release-unavailable", "release verifier cannot return verified members")
    try:
        result = reader(path)
    except Exception as exc:
        release_error = getattr(release, "ReleaseError", ValueError)
        if isinstance(exc, release_error):
            raise KitChangeError("release-invalid", str(exc)) from exc
        raise
    if not isinstance(result, tuple) or len(result) != 2:
        raise KitChangeError("release-invalid", "release verifier returned a malformed result")
    report, members = result
    if not isinstance(report, dict) or not isinstance(members, Mapping):
        raise KitChangeError("release-invalid", "release verifier returned a malformed result")
    return report, members


def _require_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise KitChangeError("install-manifest-invalid", f"{label} fields are malformed")
    return value


def _strict_json(content: bytes, *, code: str, label: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key {key!r}")
            value[key] = item
        return value

    try:
        return json.loads(content.decode("utf-8"), object_pairs_hook=object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise KitChangeError(code, f"{label} is not unambiguous UTF-8 JSON: {exc}") from exc


def _nullable_sha(value: object, label: str) -> str | None:
    if value is None:
        return None
    text = str(value)
    if not SHA256_RE.fullmatch(text):
        raise KitChangeError("install-manifest-invalid", f"{label} must be SHA-256 or null")
    return text


def _validate_surface_destination(path: str) -> None:
    folded = path.casefold()
    if folded == STABLE_LAUNCHER_PATH.casefold():
        return
    if (
        folded in {MANAGED_ROOT.casefold(), ".godot", ".kit"}
        or any(folded.startswith(prefix.casefold()) for prefix in SURFACE_PROTECTED_PREFIXES)
        or folded in PROJECT_SETTING_CASEFOLDS
    ):
        raise KitChangeError(
            "install-manifest-invalid", f"project or private path may not be kit-owned: {path}"
        )


def _surface_from_owned(raw: object, members: Mapping[str, Any]) -> Surface:
    if not isinstance(raw, dict):
        raise KitChangeError("install-manifest-invalid", "owned file fields are malformed")
    strategy = str(raw.get("strategy") or "")
    common = {"id", "path", "source", "mode", "strategy", "legacy_sha256"}
    schema_fields = {
        "schema_key",
        "target_schema",
        "supported_schemas",
        "defaults",
        "allowed_keys",
    }
    expected = common | schema_fields if strategy == "schema-json" else common
    value = _require_keys(raw, expected, "owned file")
    identifier = str(value["id"])
    path = _safe_relative(value["path"], label=f"owned file {identifier}")
    _validate_surface_destination(path)
    source = _safe_relative(value["source"], label=f"owned source {identifier}")
    mode = str(value["mode"])
    if not identifier or len(identifier) > 80 or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", identifier):
        raise KitChangeError("install-manifest-invalid", "owned file id is malformed")
    if mode not in {"0644", "0755"}:
        raise KitChangeError("install-manifest-invalid", f"owned file mode is invalid: {identifier}")
    if strategy not in {"replace", "create-only", "schema-json"}:
        raise KitChangeError(
            "install-manifest-invalid", f"owned file strategy is invalid: {identifier}"
        )
    if source in {INSTALL_MANIFEST, RELEASE_MANIFEST}:
        raise KitChangeError(
            "install-manifest-invalid", "control manifests cannot be installed as project files"
        )
    source_bytes = _member_bytes(members, source)
    legacy_sha = _nullable_sha(
        value["legacy_sha256"], f"owned file {identifier} legacy_sha256"
    )
    if path in CREATE_ONLY_PATHS and strategy != "create-only":
        raise KitChangeError(
            "install-manifest-invalid", f"{path} must use create-only ownership"
        )
    if path == SCHEMA_JSON_PATH and strategy != "schema-json":
        raise KitChangeError(
            "install-manifest-invalid", f"{SCHEMA_JSON_PATH} must use schema-json ownership"
        )
    if strategy == "create-only":
        if path not in CREATE_ONLY_PATHS or legacy_sha is not None:
            raise KitChangeError(
                "install-manifest-invalid",
                f"create-only ownership is restricted to project architecture files: {path}",
            )
        return Surface(identifier, path, strategy, source, mode, None)
    if strategy == "replace":
        return Surface(identifier, path, strategy, source, mode, legacy_sha)

    if path != SCHEMA_JSON_PATH or legacy_sha is not None:
        raise KitChangeError(
            "install-manifest-invalid",
            "schema-json ownership is restricted to kit.config.json without a legacy hash",
        )
    schema_key = value["schema_key"]
    target_schema = value["target_schema"]
    supported = value["supported_schemas"]
    defaults = value["defaults"]
    allowed = value["allowed_keys"]
    if not isinstance(schema_key, str) or CONFIG_KEY_RE.fullmatch(schema_key) is None:
        raise KitChangeError("install-manifest-invalid", "schema-json schema_key is malformed")
    if not isinstance(target_schema, int) or isinstance(target_schema, bool) or target_schema <= 0:
        raise KitChangeError("install-manifest-invalid", "schema-json target_schema is malformed")
    if (
        not isinstance(supported, list)
        or supported != sorted(set(supported))
        or any(not isinstance(item, int) or isinstance(item, bool) or item < 0 for item in supported)
        or target_schema not in supported
    ):
        raise KitChangeError(
            "install-manifest-invalid", "schema-json supported_schemas is malformed"
        )
    if (
        not isinstance(allowed, list)
        or allowed != sorted(set(allowed))
        or any(not isinstance(item, str) or CONFIG_KEY_RE.fullmatch(item) is None for item in allowed)
        or len({item.casefold() for item in allowed}) != len(allowed)
    ):
        raise KitChangeError("install-manifest-invalid", "schema-json allowed_keys is malformed")
    if not isinstance(defaults, dict) or set(defaults) != set(allowed):
        raise KitChangeError(
            "install-manifest-invalid", "schema-json defaults must define every allowed key"
        )
    if defaults.get(schema_key) != target_schema:
        raise KitChangeError(
            "install-manifest-invalid", "schema-json default does not declare its target schema"
        )
    source_default = _strict_json(
        source_bytes,
        code="install-manifest-invalid",
        label=f"schema-json source {identifier}",
    )
    if source_default != defaults or source_bytes != _canonical(defaults):
        raise KitChangeError(
            "install-manifest-invalid",
            "schema-json source must be the canonical form of its declared defaults",
        )
    return Surface(
        identifier,
        path,
        strategy,
        source,
        mode,
        None,
        schema_key=schema_key,
        target_schema=target_schema,
        supported_schemas=tuple(supported),
        defaults=defaults,
        allowed_keys=tuple(allowed),
    )


def _surface_from_block(raw: object, members: Mapping[str, Any]) -> Surface:
    value = _require_keys(
        raw,
        {"id", "path", "source", "mode", "legacy_file_sha256", "begin", "end"},
        "managed block",
    )
    identifier = str(value["id"])
    path = _safe_relative(value["path"], label=f"managed block {identifier}")
    _validate_surface_destination(path)
    source = _safe_relative(value["source"], label=f"managed block source {identifier}")
    mode = str(value["mode"])
    begin = str(value["begin"])
    end = str(value["end"])
    if not identifier or len(identifier) > 80 or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", identifier):
        raise KitChangeError("install-manifest-invalid", "managed block id is malformed")
    if mode not in {"0644", "0755"}:
        raise KitChangeError("install-manifest-invalid", f"managed block mode is invalid: {identifier}")
    if source in {INSTALL_MANIFEST, RELEASE_MANIFEST}:
        raise KitChangeError(
            "install-manifest-invalid", "control manifests cannot be managed block sources"
        )
    if not begin or not end or begin == end or "\n" in begin or "\n" in end:
        raise KitChangeError("install-manifest-invalid", f"managed block markers are invalid: {identifier}")
    block = _member_bytes(members, source)
    try:
        text = block.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KitChangeError("install-manifest-invalid", f"managed block is not UTF-8: {identifier}") from exc
    if text.count(begin) != 1 or text.count(end) != 1 or text.index(begin) >= text.index(end):
        raise KitChangeError("install-manifest-invalid", f"managed block source is malformed: {identifier}")
    if text[: text.index(begin)].strip() or text[text.index(end) + len(end) :].strip():
        raise KitChangeError(
            "install-manifest-invalid",
            f"managed block source contains content outside its markers: {identifier}",
        )
    return Surface(
        identifier,
        path,
        "managed-block",
        source,
        mode,
        _nullable_sha(
            value["legacy_file_sha256"],
            f"managed block {identifier} legacy_file_sha256",
        ),
        begin,
        end,
    )


def _retired_file(raw: object, surface_paths: set[str]) -> RetiredFile:
    value = _require_keys(raw, {"path", "sha256"}, "legacy retired file")
    path = _safe_relative(value["path"], label="legacy retired file")
    digest = str(value["sha256"] or "")
    allowlisted = getattr(release, "is_allowlisted", None)
    folded = path.casefold()
    protected = (
        folded in {item.casefold() for item in surface_paths}
        or folded in RETIRED_PROTECTED_CASEFOLDS
        or folded in PROJECT_SETTING_CASEFOLDS
        or any(folded.startswith(prefix.casefold()) for prefix in RETIRED_PROTECTED_PREFIXES)
    )
    if (
        not SHA256_RE.fullmatch(digest)
        or protected
        or not callable(allowlisted)
        or not bool(allowlisted(path))
    ):
        raise KitChangeError(
            "install-manifest-invalid", f"legacy retirement target is not safe: {path}"
        )
    return RetiredFile(path, digest)


def _has_parent_child_collision(paths: list[str]) -> bool:
    folded_parts = [
        tuple(part.casefold() for part in PurePosixPath(path).parts) for path in paths
    ]
    for index, left in enumerate(folded_parts):
        for right in folded_parts[index + 1 :]:
            shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
            if len(shorter) < len(longer) and longer[: len(shorter)] == shorter:
                return True
    return False


def _install_manifest(
    report: dict[str, Any], members: Mapping[str, Any]
) -> tuple[dict[str, Any], list[Surface], list[RetiredFile]]:
    value = _strict_json(
        _member_bytes(members, INSTALL_MANIFEST),
        code="install-manifest-invalid",
        label=INSTALL_MANIFEST,
    )
    manifest = _require_keys(
        value,
        {
            "schema",
            "kind",
            "kit_version",
            "install_schema",
            "layout_schema",
            "config_schema",
            "supported_legacy_versions",
            "supported_install_schemas",
            "core_layout",
            "owned_files",
            "managed_blocks",
            "legacy_retired_files",
        },
        "install manifest",
    )
    if (
        manifest["schema"] != INSTALL_SCHEMA
        or manifest["kind"] != "agent-kit-install-manifest"
        or manifest["install_schema"] != INSTALL_SCHEMA
        or manifest["core_layout"] != "versioned-by-archive-sha256"
        or not isinstance(manifest["layout_schema"], int)
        or not isinstance(manifest["config_schema"], int)
        or manifest["layout_schema"] <= 0
        or manifest["config_schema"] <= 0
    ):
        raise KitChangeError("install-manifest-invalid", "install manifest contract is unsupported")
    version = str(manifest["kit_version"])
    if version != str(report.get("version") or "") or VERSION_RE.fullmatch(version) is None:
        raise KitChangeError("install-manifest-invalid", "install and release versions do not match")
    legacy = manifest["supported_legacy_versions"]
    supported = manifest["supported_install_schemas"]
    if (
        not isinstance(legacy, list)
        or legacy != sorted(set(legacy))
        or any(not isinstance(item, str) or VERSION_RE.fullmatch(item) is None for item in legacy)
        or not isinstance(supported, list)
        or supported != sorted(set(supported))
        or any(not isinstance(item, int) or isinstance(item, bool) or item <= 0 for item in supported)
    ):
        raise KitChangeError("install-manifest-invalid", "install compatibility list is malformed")
    if (
        not isinstance(manifest["owned_files"], list)
        or not isinstance(manifest["managed_blocks"], list)
        or not isinstance(manifest["legacy_retired_files"], list)
    ):
        raise KitChangeError("install-manifest-invalid", "install surfaces must be lists")
    surfaces = [
        *(_surface_from_owned(raw, members) for raw in manifest["owned_files"]),
        *(_surface_from_block(raw, members) for raw in manifest["managed_blocks"]),
    ]
    ids = [surface.id for surface in surfaces]
    paths = [surface.path for surface in surfaces]
    if len(ids) != len(set(ids)):
        raise KitChangeError("install-manifest-invalid", "install surface ids must be unique")
    case_paths = [path.casefold() for path in paths]
    if len(case_paths) != len(set(case_paths)):
        raise KitChangeError("install-manifest-invalid", "install surface paths collide by case")
    strategies = {surface.path: surface.strategy for surface in surfaces}
    required_strategies = {
        SCHEMA_JSON_PATH: "schema-json",
        **{path: "create-only" for path in CREATE_ONLY_PATHS},
    }
    for path, strategy in required_strategies.items():
        if strategies.get(path) != strategy:
            raise KitChangeError(
                "install-manifest-invalid", f"install manifest must declare {path} as {strategy}"
            )
    surface_paths = set(paths)
    retired = [
        _retired_file(raw, surface_paths) for raw in manifest["legacy_retired_files"]
    ]
    retired_paths = [item.path for item in retired]
    if retired_paths != sorted(retired_paths) or len({item.casefold() for item in retired_paths}) != len(
        retired_paths
    ):
        raise KitChangeError(
            "install-manifest-invalid", "legacy retired files must be unique and sorted"
        )
    if _has_parent_child_collision([*paths, *retired_paths]):
        raise KitChangeError(
            "install-manifest-invalid", "install destinations collide as parent and child"
        )
    member_paths = list(members)
    if len(member_paths) != len({str(path).casefold() for path in member_paths}):
        raise KitChangeError("release-invalid", "release member paths collide by case")
    return manifest, sorted(surfaces, key=lambda surface: surface.id), retired


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = VERSION_RE.fullmatch(value)
    if match is None:
        raise KitChangeError("version-invalid", f"kit version is not semantic: {value!r}")
    return tuple(int(match.group(index)) for index in (1, 2, 3))


def _load_state(root: Path) -> tuple[dict[str, Any], bytes] | None:
    path = _target(root, CURRENT_STATE)
    if not path.exists():
        return None
    raw = _stable_bytes(path)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KitChangeError("install-state-invalid", f"{CURRENT_STATE} is invalid: {exc}") from exc
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
    if not isinstance(value, dict) or set(value) != expected:
        raise KitChangeError("install-state-invalid", "installed kit state fields are malformed")
    if (
        value["schema"] != STATE_SCHEMA
        or value["kind"] != "agent-kit-install-state"
        or not TRANSACTION_RE.fullmatch(str(value["installation_id"] or ""))
        or any(
            not isinstance(item, int) or isinstance(item, bool) or item <= 0
            for item in (
                value["install_schema"],
                value["layout_schema"],
                value["config_schema"],
            )
        )
        or not isinstance(value["managed_surfaces"], list)
        or not isinstance(value["applied_migrations"], list)
    ):
        raise KitChangeError("install-state-invalid", "installed kit state is malformed")
    _validate_release_state(value["active_release"])
    if value["previous_release"] is not None:
        _validate_release_state(value["previous_release"])
    ids: list[str] = []
    paths: list[str] = []
    for surface in value["managed_surfaces"]:
        item = _require_state_keys(
            surface,
            {"id", "path", "strategy", "base_sha256", "applied_sha256"},
            "managed surface",
        )
        identifier = str(item["id"] or "")
        path = _safe_relative(item["path"])
        if item["strategy"] not in {
            "owned-file",
            "replace",
            "create-only",
            "schema-json",
            "managed-block",
        }:
            raise KitChangeError("install-state-invalid", "managed surface strategy is invalid")
        if not SHA256_RE.fullmatch(str(item["base_sha256"] or "")) or not SHA256_RE.fullmatch(
            str(item["applied_sha256"] or "")
        ):
            raise KitChangeError("install-state-invalid", "managed surface hashes are invalid")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", identifier):
            raise KitChangeError("install-state-invalid", "managed surface id is invalid")
        ids.append(identifier)
        paths.append(path)
    if (
        ids != sorted(ids)
        or len(ids) != len(set(ids))
        or len(paths) != len({item.casefold() for item in paths})
    ):
        raise KitChangeError("install-state-invalid", "managed surface ids are malformed")
    migration_ids: list[str] = []
    for migration in value["applied_migrations"]:
        item = _require_state_keys(
            migration,
            {"id", "input_sha256", "output_sha256"},
            "applied migration",
        )
        identifier = str(item["id"] or "")
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,119}", identifier)
            or not SHA256_RE.fullmatch(str(item["input_sha256"] or ""))
            or not SHA256_RE.fullmatch(str(item["output_sha256"] or ""))
        ):
            raise KitChangeError("install-state-invalid", "applied migration is malformed")
        migration_ids.append(identifier)
    if migration_ids != sorted(migration_ids) or len(migration_ids) != len(set(migration_ids)):
        raise KitChangeError("install-state-invalid", "applied migration ids are malformed")
    return value, raw


def _require_state_keys(value: object, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != expected:
        raise KitChangeError("install-state-invalid", f"{label} fields are malformed")
    return value


def _validate_release_state(value: object) -> dict[str, Any]:
    item = _require_state_keys(
        value,
        {
            "kit_version",
            "archive_sha256",
            "release_manifest_sha256",
            "install_manifest_sha256",
            "source_commit",
            "core_path",
        },
        "release state",
    )
    if (
        VERSION_RE.fullmatch(str(item["kit_version"] or "")) is None
        or not SHA256_RE.fullmatch(str(item["archive_sha256"] or ""))
        or not SHA256_RE.fullmatch(str(item["release_manifest_sha256"] or ""))
        or not SHA256_RE.fullmatch(str(item["install_manifest_sha256"] or ""))
        or not re.fullmatch(r"[0-9a-f]{40,64}", str(item["source_commit"] or ""))
    ):
        raise KitChangeError("install-state-invalid", "release state identity is malformed")
    core = _safe_relative(item["core_path"])
    expected_core = f"{RELEASES_ROOT}/{item['archive_sha256']}"
    if core != expected_core:
        raise KitChangeError("install-state-invalid", "release core path does not match its archive")
    return item


def _authenticated_active_surfaces(
    root: Path,
    state: dict[str, Any],
    raw_state: bytes,
) -> list[Surface]:
    """Bind managed ownership claims to the exact authenticated active core."""
    try:
        installation = managed_launcher.resolve_installation(root)
    except managed_launcher.LauncherError as exc:
        raise KitChangeError(
            "installed-kit-untrusted",
            f"the active kit cannot be authenticated; use kit change recovery: {exc}",
        ) from exc
    active = state["active_release"]
    if (
        installation.mode != "managed"
        or installation.current_path is None
        or installation.manifest_path is None
        or installation.release_sha256 != active["archive_sha256"]
        or installation.version != active["kit_version"]
        or installation.source_commit != active["source_commit"]
    ):
        raise KitChangeError(
            "installed-kit-untrusted",
            "the active kit selection does not match its install record; use kit change recovery",
        )
    if _stable_bytes(installation.current_path) != raw_state:
        raise KitChangeError(
            "installed-kit-untrusted",
            "the install record changed while it was authenticated; retry or use kit change recovery",
        )

    manifest_content = _stable_bytes(installation.manifest_path)
    try:
        release_manifest = json.loads(manifest_content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KitChangeError(
            "installed-kit-untrusted", "the active release manifest is unreadable"
        ) from exc
    files = release_manifest.get("files") if isinstance(release_manifest, dict) else None
    if (
        _sha256(manifest_content) != active["release_manifest_sha256"]
        or not isinstance(release_manifest, dict)
        or _canonical(release_manifest) != manifest_content
        or not isinstance(files, list)
    ):
        raise KitChangeError(
            "installed-kit-untrusted", "the active release manifest identity changed"
        )
    members: dict[str, Any] = {
        RELEASE_MANIFEST: release.ArchiveMember(
            RELEASE_MANIFEST, manifest_content, 0o644
        )
    }
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {
            "path", "bytes", "sha256", "mode"
        }:
            raise KitChangeError(
                "installed-kit-untrusted", "the active release file list is malformed"
            )
        relative = _safe_relative(entry.get("path"), label="active release member")
        mode = entry.get("mode")
        if mode not in {"0644", "0755"}:
            raise KitChangeError(
                "installed-kit-untrusted", "the active release member mode is malformed"
            )
        member_path = installation.core_root.joinpath(*PurePosixPath(relative).parts)
        content = _stable_bytes(
            member_path,
            limit=getattr(release, "MAX_FILE_BYTES", MAX_MANAGED_FILE_BYTES),
        )
        if (
            not isinstance(entry["bytes"], int)
            or isinstance(entry["bytes"], bool)
            or entry["bytes"] != len(content)
            or not isinstance(entry["sha256"], str)
            or _sha256(content) != entry["sha256"]
        ):
            raise KitChangeError(
                "installed-kit-untrusted",
                f"the active release member {relative} changed after authentication",
            )
        members[relative] = release.ArchiveMember(
            relative,
            content,
            int(mode, 8),
        )
    if not _core_matches(installation.core_root, members):
        raise KitChangeError(
            "installed-kit-untrusted",
            "the active kit core changed while it was authenticated; use kit change recovery",
        )
    report = {"version": active["kit_version"]}
    _manifest, surfaces, _retired = _install_manifest(report, members)
    if (
        _sha256(_member_bytes(members, INSTALL_MANIFEST))
        != active["install_manifest_sha256"]
        or _stable_bytes(installation.current_path) != raw_state
    ):
        raise KitChangeError(
            "installed-kit-untrusted",
            "the active kit identity changed while upgrade was being prepared",
        )
    return surfaces


def _validate_managed_ownership(
    root: Path,
    state: dict[str, Any],
    surfaces: list[Surface],
) -> None:
    claimed = state["managed_surfaces"]
    by_id = {str(item["id"]): item for item in claimed}
    expected_ids = [surface.id for surface in surfaces]
    if sorted(by_id) != expected_ids or len(by_id) != len(claimed):
        raise KitChangeError(
            "installed-kit-untrusted",
            "the install record does not name the active kit surfaces; use kit change recovery",
        )
    for surface in surfaces:
        item = by_id[surface.id]
        source_path = root / MANAGED_ROOT / "releases" / state["active_release"][
            "archive_sha256"
        ] / PurePosixPath(surface.source)
        source = _stable_bytes(
            source_path,
            limit=getattr(release, "MAX_FILE_BYTES", MAX_MANAGED_FILE_BYTES),
        )
        if surface.strategy == "managed-block" and not source.endswith(b"\n"):
            source += b"\n"
        expected_base = _sha256(source)
        if (
            item["path"] != surface.path
            or item["strategy"] != surface.strategy
            or item["base_sha256"] != expected_base
        ):
            raise KitChangeError(
                "installed-kit-untrusted",
                f"the ownership record for {surface.path} does not match the active kit",
            )

        if surface.strategy == "replace":
            current = _snapshot(_target(root, surface.path))
            if (
                current.get("kind") != "file"
                or current.get("sha256") != item["applied_sha256"]
                or item["applied_sha256"] != expected_base
            ):
                raise KitChangeError(
                    "installed-kit-untrusted",
                    f"the kit-owned file {surface.path} no longer matches its ownership record",
                )
        elif surface.strategy == "managed-block":
            path = _target(root, surface.path)
            if not path.is_file():
                raise KitChangeError(
                    "installed-kit-untrusted",
                    f"the managed section in {surface.path} is missing",
                )
            _text, block, _outside = _block_parts(_stable_bytes(path), surface)
            if (
                block is None
                or _sha256(block.encode("utf-8")) != item["applied_sha256"]
                or item["applied_sha256"] != expected_base
            ):
                raise KitChangeError(
                    "installed-kit-untrusted",
                    f"the managed section in {surface.path} no longer matches its ownership record",
                )


def _legacy_context(
    root: Path, surfaces: list[Surface], retired: list[RetiredFile]
) -> dict[str, Any] | None:
    marker = _target(root, MARKER)
    if not marker.exists():
        return None
    version_path = _target(root, LEGACY_VERSION)
    if not version_path.is_file():
        raise KitChangeError("legacy-invalid", "legacy kit marker exists without VERSION")
    try:
        version = _stable_bytes(version_path).decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise KitChangeError("legacy-invalid", "legacy VERSION is not UTF-8") from exc
    if VERSION_RE.fullmatch(version) is None:
        raise KitChangeError("legacy-invalid", "legacy VERSION is malformed")
    inventory: list[dict[str, Any]] = []
    for relative in sorted(
        {
            MARKER,
            LEGACY_VERSION,
            *(surface.path for surface in surfaces),
            *(item.path for item in retired),
        }
    ):
        inventory.append({"path": relative, "state": _snapshot(_target(root, relative))})
    return {
        "mode": "legacy",
        "kit_version": version,
        "install_state_sha256": _sha256(_canonical(inventory)),
        "active_archive_sha256": None,
        "state": None,
        "raw": None,
    }


def _current_context(
    root: Path, surfaces: list[Surface], retired: list[RetiredFile]
) -> dict[str, Any]:
    loaded = _load_state(root)
    if loaded is not None:
        state, raw = loaded
        active_surfaces = _authenticated_active_surfaces(root, state, raw)
        _validate_managed_ownership(root, state, active_surfaces)
        active = state["active_release"]
        return {
            "mode": "managed",
            "kit_version": active["kit_version"],
            "install_state_sha256": _sha256(raw),
            "active_archive_sha256": active["archive_sha256"],
            "state": state,
            "raw": raw,
        }
    managed = _target(root, MANAGED_ROOT, leaf="directory")
    if managed.exists():
        try:
            has_content = any(managed.iterdir())
        except OSError as exc:
            raise KitChangeError("install-state-invalid", f"cannot inspect {MANAGED_ROOT}: {exc}") from exc
        if has_content:
            raise KitChangeError("install-state-missing", f"{MANAGED_ROOT} exists without current.json")
    legacy = _legacy_context(root, surfaces, retired)
    if legacy is not None:
        return legacy
    return {
        "mode": "none",
        "kit_version": None,
        "install_state_sha256": None,
        "active_archive_sha256": None,
        "state": None,
        "raw": None,
    }


def _scope_sha256(root: Path) -> str:
    rendered = os.path.normcase(str(root)).replace("\\", "/").rstrip("/")
    return _sha256(rendered.encode("utf-8", errors="surrogatepass"))


def _surface_state(current: dict[str, Any], identifier: str) -> dict[str, Any] | None:
    state = current.get("state")
    if not isinstance(state, dict):
        return None
    for item in state.get("managed_surfaces", []):
        if isinstance(item, dict) and item.get("id") == identifier:
            return item
    return None


def _block_parts(content: bytes, surface: Surface) -> tuple[str, str | None, str | None]:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise KitChangeError("shared-file-invalid", f"{surface.path} is not UTF-8") from exc
    assert surface.begin is not None and surface.end is not None
    begins = [match.start() for match in re.finditer(re.escape(surface.begin), text)]
    ends = [match.start() for match in re.finditer(re.escape(surface.end), text)]
    if not begins and not ends:
        return text, None, None
    if len(begins) != 1 or len(ends) != 1 or begins[0] >= ends[0]:
        raise KitChangeError("managed-block-invalid", f"{surface.path} has malformed kit markers")
    end_index = ends[0] + len(surface.end)
    if end_index < len(text) and text[end_index : end_index + 2] == "\r\n":
        end_index += 2
    elif end_index < len(text) and text[end_index] == "\n":
        end_index += 1
    return text, text[begins[0] : end_index], text[: begins[0]] + "\0" + text[end_index:]


def _append_block(existing: str, block: str) -> str:
    if not existing:
        return block
    if existing.endswith("\n\n") or existing.endswith("\r\n\r\n"):
        return existing + block
    if existing.endswith("\n") or existing.endswith("\r\n"):
        return existing + "\n" + block
    return existing + "\n\n" + block


def _replace_block(existing: str, old_block: str, new_block: str) -> str:
    start = existing.index(old_block)
    return existing[:start] + new_block + existing[start + len(old_block) :]


def _same_json_kind(value: Any, default: Any) -> bool:
    if default is None:
        return True
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


def _merge_json_defaults(defaults: Any, existing: Any, path: str) -> Any:
    if not _same_json_kind(existing, defaults):
        raise KitChangeError(
            "schema-json-value-invalid", f"{path} has a value of the wrong kind"
        )
    if not isinstance(defaults, dict):
        return existing
    assert isinstance(existing, dict)
    existing_by_case: dict[str, str] = {}
    default_by_case = {key.casefold(): key for key in defaults}
    for key in existing:
        folded = key.casefold()
        prior = existing_by_case.get(folded)
        if prior is not None and prior != key:
            raise KitChangeError(
                "schema-json-key-unsafe", f"{path} contains case-colliding keys"
            )
        expected = default_by_case.get(folded)
        if expected is not None and expected != key:
            raise KitChangeError(
                "schema-json-key-unsafe", f"{path}.{key} conflicts with known key {expected}"
            )
        existing_by_case[folded] = key
    merged = dict(existing)
    for key, default in defaults.items():
        child_path = f"{path}.{key}"
        if key not in existing:
            merged[key] = default
        else:
            merged[key] = _merge_json_defaults(default, existing[key], child_path)
    return merged


def _schema_json_bytes(
    path: Path,
    surface: Surface,
    selected_game_root: str | None,
) -> bytes:
    assert surface.schema_key is not None
    assert surface.target_schema is not None
    assert surface.defaults is not None
    if path.exists():
        existing = _strict_json(
            _stable_bytes(path),
            code="schema-json-invalid",
            label=surface.path,
        )
        if not isinstance(existing, dict):
            raise KitChangeError("schema-json-invalid", f"{surface.path} must contain an object")
        current_schema = existing.get(surface.schema_key, 0)
        if (
            not isinstance(current_schema, int)
            or isinstance(current_schema, bool)
            or current_schema not in surface.supported_schemas
        ):
            raise KitChangeError(
                "schema-json-schema-unsupported",
                f"{surface.path} schema {current_schema!r} is not supported",
            )
    else:
        existing = {}
    merged = _merge_json_defaults(surface.defaults, existing, surface.path)
    assert isinstance(merged, dict)
    merged[surface.schema_key] = surface.target_schema
    if selected_game_root is not None:
        merged["game_root"] = selected_game_root
    return _canonical(merged)


def _mode_snapshot(content: bytes, mode: str) -> dict[str, Any]:
    if os.name == "nt":
        mode = "0644"
    return {"kind": "file", "bytes": len(content), "sha256": _sha256(content), "mode": mode}


def _change_entry(
    surface: Surface,
    action: str,
    before: dict[str, Any],
    after: dict[str, Any],
    base_sha256: str,
    source_sha256: str,
) -> dict[str, Any]:
    return {
        "id": surface.id,
        "path": surface.path,
        "ownership": "kit" if surface.strategy in {"replace", "retire-file"} else "shared",
        "strategy": surface.strategy,
        "action": action,
        "before": before,
        "after": after,
        "base_sha256": base_sha256,
        "source_sha256": source_sha256,
        "migration_id": None,
        "reversible": True,
    }


def _core_matches(core: Path, members: Mapping[str, Any]) -> bool:
    if core.is_symlink() or _is_reparse(core) or not core.is_dir():
        return False
    expected = sorted(str(name) for name in members)
    actual: list[str] = []
    for current, directories, filenames in os.walk(core, topdown=True, followlinks=False):
        base = Path(current)
        for name in list(directories):
            child = base / name
            if child.is_symlink() or _is_reparse(child):
                return False
        for name in filenames:
            path = base / name
            if path.is_symlink() or _is_reparse(path) or not path.is_file():
                return False
            actual.append(path.relative_to(core).as_posix())
    if sorted(actual) != expected:
        return False
    for name in expected:
        path = core.joinpath(*PurePosixPath(name).parts)
        try:
            if _stable_bytes(path, limit=getattr(release, "MAX_FILE_BYTES", MAX_MANAGED_FILE_BYTES)) != _member_bytes(members, name):
                return False
        except KitChangeError:
            return False
        if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != _member_mode(members, name):
            return False
    return True


def _release_identity(
    report: dict[str, Any], members: Mapping[str, Any], archive_sha256: str
) -> dict[str, Any]:
    source = report.get("source")
    commit = source.get("commit") if isinstance(source, dict) else None
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40,64}", commit) is None:
        raise KitChangeError("release-invalid", "verified release source commit is malformed")
    version = str(report.get("version") or "")
    if VERSION_RE.fullmatch(version) is None:
        raise KitChangeError("release-invalid", "verified release version is malformed")
    if not SHA256_RE.fullmatch(archive_sha256):
        raise KitChangeError("release-invalid", "verified archive SHA-256 is malformed")
    return {
        "kit_version": version,
        "archive_sha256": archive_sha256,
        "release_manifest_sha256": _sha256(_member_bytes(members, RELEASE_MANIFEST)),
        "install_manifest_sha256": _sha256(_member_bytes(members, INSTALL_MANIFEST)),
        "source_commit": commit,
        "core_path": f"{RELEASES_ROOT}/{archive_sha256}",
    }


def _build_plan(
    root: Path, archive: Path, *, game_root: str | None = None
) -> ChangePlan:
    canonical_root = _canonical_root(root)
    report, members = _read_verified_archive(archive)
    manifest, surfaces, retired = _install_manifest(report, members)
    archive_sha256 = str(report.get("archive_sha256") or "")
    identity = _release_identity(report, members, archive_sha256)
    current = _current_context(canonical_root, surfaces, retired)
    game_root_selection, game_root_blockers = _select_game_root(canonical_root, game_root)
    blockers: list[dict[str, Any]] = list(game_root_blockers)

    current_version = current.get("kit_version")
    if isinstance(current_version, str):
        if _version_tuple(identity["kit_version"]) < _version_tuple(current_version):
            blockers.append({
                "code": "downgrade-refused",
                "path": None,
                "detail": "older releases can be restored only through rollback",
            })
        if identity["kit_version"] == current_version:
            active_hash = current.get("active_archive_sha256")
            if active_hash is not None and active_hash != archive_sha256:
                blockers.append({
                    "code": "version-provenance-conflict",
                    "path": None,
                    "detail": "the same kit version has different archive bytes",
                })
    if current["mode"] == "legacy" and current_version not in manifest["supported_legacy_versions"]:
        blockers.append({
            "code": "legacy-version-unsupported",
            "path": None,
            "detail": f"legacy kit {current_version} is not supported by this release",
        })
    if current["mode"] == "managed":
        installed_schema = current["state"]["install_schema"]
        if installed_schema not in manifest["supported_install_schemas"]:
            blockers.append({
                "code": "install-schema-unsupported",
                "path": CURRENT_STATE,
                "detail": f"installed schema {installed_schema} is unsupported",
            })

    core_path = _target(canonical_root, identity["core_path"], leaf="directory")
    if core_path.exists():
        core_action = "reuse"
        if not _core_matches(core_path, members):
            blockers.append({
                "code": "managed-core-modified",
                "path": identity["core_path"],
                "detail": "existing managed core does not match the verified release",
            })
    else:
        core_action = "create"

    replacements: dict[str, bytes | None] = {}
    modes: dict[str, str | None] = {}
    changes: list[dict[str, Any]] = []
    managed_state: list[dict[str, Any]] = []

    for surface in surfaces:
        try:
            path = _target(canonical_root, surface.path)
            before = _snapshot(path)
            source = _member_bytes(members, surface.source)
            old = _surface_state(current, surface.id)
            if surface.strategy == "replace":
                after_bytes = source
                after = _mode_snapshot(after_bytes, surface.mode)
                base_sha = _sha256(source)
                applied_sha = after["sha256"]
                allowed = before["kind"] == "absent" or before == after
                if old is not None:
                    allowed = allowed or (
                        old.get("strategy") in {"owned-file", "replace"}
                        and old.get("path") == surface.path
                        and old.get("applied_sha256") == before.get("sha256")
                    )
                if current["mode"] == "legacy" and surface.legacy_sha256 is not None:
                    allowed = allowed or before.get("sha256") == surface.legacy_sha256
                if not allowed:
                    blockers.append({
                        "code": "owned-file-modified",
                        "path": surface.path,
                        "detail": "existing file is not a known kit-owned version",
                    })
            elif surface.strategy == "create-only":
                base_sha = _sha256(source)
                if before["kind"] == "absent":
                    after_bytes = source
                    after = _mode_snapshot(after_bytes, surface.mode)
                else:
                    after_bytes = _stable_bytes(path)
                    after = before
                applied_sha = after["sha256"]
            elif surface.strategy == "schema-json":
                base_sha = _sha256(source)
                after_bytes = _schema_json_bytes(
                    path,
                    surface,
                    game_root_selection["value"],
                )
                after = _mode_snapshot(after_bytes, before.get("mode", surface.mode))
                applied_sha = after["sha256"]
            else:
                assert surface.strategy == "managed-block"
                block = source if source.endswith(b"\n") else source + b"\n"
                base_sha = _sha256(block)
                applied_sha = base_sha
                if before["kind"] == "absent":
                    after_bytes = block
                else:
                    existing_bytes = _stable_bytes(path)
                    existing, old_block, _outside = _block_parts(existing_bytes, surface)
                    block_text = block.decode("utf-8")
                    if old_block is None:
                        if (
                            current["mode"] == "legacy"
                            and surface.legacy_sha256 is not None
                            and before.get("sha256") == surface.legacy_sha256
                        ):
                            after_bytes = block
                        else:
                            after_bytes = _append_block(existing, block_text).encode("utf-8")
                    elif _sha256(old_block.encode("utf-8")) == base_sha:
                        after_bytes = existing_bytes
                    elif (
                        old is not None
                        and old.get("strategy") == "managed-block"
                        and old.get("path") == surface.path
                        and old.get("applied_sha256") == _sha256(old_block.encode("utf-8"))
                    ):
                        after_bytes = _replace_block(existing, old_block, block_text).encode("utf-8")
                    else:
                        blockers.append({
                            "code": "managed-block-modified",
                            "path": surface.path,
                            "detail": "the existing kit block was changed locally",
                        })
                        after_bytes = existing_bytes
                after = _mode_snapshot(after_bytes, before.get("mode", surface.mode))

            managed_state.append({
                "id": surface.id,
                "path": surface.path,
                "strategy": surface.strategy,
                "base_sha256": base_sha,
                "applied_sha256": applied_sha,
            })
            if before != after:
                action = "create" if before["kind"] == "absent" else "modify"
                changes.append(
                    _change_entry(surface, action, before, after, base_sha, _sha256(source))
                )
                replacements[surface.path] = after_bytes
                modes[surface.path] = after["mode"]
        except KitChangeError as exc:
            blockers.append({"code": exc.code, "path": surface.path, "detail": exc.detail})

    if current["mode"] == "legacy":
        for index, item in enumerate(retired, start=1):
            try:
                path = _target(canonical_root, item.path)
                before = _snapshot(path)
                if before["kind"] == "absent":
                    continue
                if before.get("sha256") != item.sha256:
                    blockers.append({
                        "code": "legacy-retired-file-modified",
                        "path": item.path,
                        "detail": "legacy kit file has unknown bytes and will be preserved",
                    })
                    continue
                surface = Surface(
                    f"legacy-retire-{index:04d}",
                    item.path,
                    "retire-file",
                    item.path,
                    before["mode"],
                    item.sha256,
                )
                after = {"kind": "absent"}
                changes.append(
                    _change_entry(
                        surface,
                        "delete",
                        before,
                        after,
                        item.sha256,
                        item.sha256,
                    )
                )
                replacements[item.path] = None
                modes[item.path] = None
            except KitChangeError as exc:
                blockers.append({"code": exc.code, "path": item.path, "detail": exc.detail})

    same_release = (
        current["mode"] == "managed"
        and current.get("active_archive_sha256") == archive_sha256
    )
    scope = _scope_sha256(canonical_root)
    previous_release = (
        current["state"]["active_release"]
        if current["mode"] == "managed" and not same_release
        else current["state"].get("previous_release")
        if current["mode"] == "managed"
        else None
    )
    installation_id = (
        current["state"]["installation_id"]
        if current["mode"] == "managed"
        else _sha256((scope + ":installation").encode("ascii"))[:32]
    )
    migrations = []
    applied_migrations = (
        list(current["state"].get("applied_migrations", []))
        if current["mode"] == "managed"
        else []
    )
    if current["mode"] == "legacy":
        migration = {
            "id": f"legacy-{current_version}-to-managed-1",
            "kind": "layout",
            "from_schema": 0,
            "to_schema": 1,
            "input_sha256": current["install_state_sha256"],
            "output_sha256": _sha256(_canonical(identity)),
        }
        migrations.append(migration)
        applied_migrations.append({
            "id": migration["id"],
            "input_sha256": migration["input_sha256"],
            "output_sha256": migration["output_sha256"],
        })

    target_state_value = {
        "schema": STATE_SCHEMA,
        "kind": "agent-kit-install-state",
        "installation_id": installation_id,
        "install_schema": manifest["install_schema"],
        "layout_schema": manifest["layout_schema"],
        "config_schema": manifest["config_schema"],
        "active_release": identity,
        "previous_release": previous_release,
        "managed_surfaces": sorted(managed_state, key=lambda item: item["id"]),
        "applied_migrations": sorted(applied_migrations, key=lambda item: item["id"]),
    }
    target_state = _canonical(target_state_value)
    state_path = _target(canonical_root, CURRENT_STATE)
    state_before = _snapshot(state_path)
    state_after = _mode_snapshot(target_state, "0644")
    if state_before != state_after:
        state_surface = Surface(
            "install-state",
            CURRENT_STATE,
            "replace",
            INSTALL_MANIFEST,
            "0644",
            None,
        )
        changes.append(
            _change_entry(
                state_surface,
                "create" if state_before["kind"] == "absent" else "modify",
                state_before,
                state_after,
                _sha256(target_state),
                _sha256(target_state),
            )
        )
        replacements[CURRENT_STATE] = target_state
        modes[CURRENT_STATE] = "0644"

    changes.sort(key=lambda item: (item["path"] == CURRENT_STATE, item["path"], item["id"]))
    blockers = [
        dict(item)
        for item in {
            (item["code"], item.get("path"), item["detail"]): item for item in blockers
        }.values()
    ]
    blockers.sort(key=lambda item: (str(item.get("path") or ""), item["code"], item["detail"]))
    operation = "install" if current["mode"] == "none" else "upgrade"
    authority = report.get("authority_evidence")
    receipt_trust = authority.get("receipt_trust") if isinstance(authority, dict) else None
    material = {
        "operation": operation,
        "target_scope_sha256": scope,
        "current": {
            "mode": current["mode"],
            "kit_version": current_version,
            "install_state_sha256": current["install_state_sha256"],
            "active_archive_sha256": current["active_archive_sha256"],
        },
        "game_root": game_root_selection,
        "target_release": {
            "kit_version": identity["kit_version"],
            "archive_sha256": archive_sha256,
            "release_manifest_sha256": identity["release_manifest_sha256"],
            "install_manifest_sha256": identity["install_manifest_sha256"],
            "source_commit": identity["source_commit"],
            "receipt_trust": receipt_trust,
            "install_schema": manifest["install_schema"],
            "layout_schema": manifest["layout_schema"],
            "config_schema": manifest["config_schema"],
        },
        "core": {
            "action": core_action,
            "path": identity["core_path"],
            "archive_sha256": archive_sha256,
        },
        "changes": changes,
        "migrations": migrations,
        "blockers": blockers,
    }
    approval_sha = _sha256(_canonical(material))
    preview_value = {
        "schema": PREVIEW_SCHEMA,
        "kind": "agent-kit-change-preview",
        "material": material,
        "approval": {
            "algorithm": "sha256-canonical-json-v1",
            "sha256": approval_sha,
            "approvable": not blockers,
        },
    }
    return ChangePlan(
        canonical_root,
        preview_value,
        report,
        members,
        manifest,
        replacements,
        modes,
        current,
        target_state,
        noop=not blockers and core_action == "reuse" and not changes,
    )


def preview(
    root: Path, archive: Path, *, game_root: str | None = None
) -> dict[str, Any]:
    """Return the stable read-only install or upgrade decision."""
    return _build_plan(root, archive, game_root=game_root).preview


def _ensure_directory(root: Path, relative: str, created: list[str]) -> Path:
    portable = _safe_relative(relative)
    cursor = root
    for component in PurePosixPath(portable).parts:
        next_path = _case_checked_child(cursor, component, portable)
        if next_path.exists() or next_path.is_symlink():
            if next_path.is_symlink() or _is_reparse(next_path) or not next_path.is_dir():
                raise KitChangeError("redirected-path", f"unsafe directory: {portable}")
        else:
            try:
                next_path.mkdir()
            except OSError as exc:
                raise KitChangeError("directory-write-failed", f"cannot create {portable}: {exc}") from exc
            created.append(next_path.relative_to(root).as_posix())
        cursor = next_path
    return cursor


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _replace_file(temporary: Path, destination: Path) -> None:
    for attempt in range(5):
        try:
            os.replace(temporary, destination)
            _sync_directory(destination.parent)
            return
        except PermissionError as exc:
            if (
                os.name != "nt"
                or getattr(exc, "winerror", None) not in (5, 32)
                or attempt == 4
            ):
                raise
            time.sleep(0.02 * (attempt + 1))


def _atomic_bytes(path: Path, content: bytes, mode: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            temporary.chmod(int(mode, 8))
        except OSError:
            if os.name != "nt":
                raise
        _replace_file(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _durable_json(path: Path, value: Any) -> None:
    _atomic_bytes(path, _canonical(value), "0644")


def _runtime_paths(root: Path, *, create: bool) -> tuple[Path, Path, Path]:
    created: list[str] = []
    upgrade = _target(root, UPGRADE_ROOT, leaf="directory")
    transactions = _target(root, TRANSACTIONS_ROOT, leaf="directory")
    lock = _target(root, CHANGE_LOCK)
    if create:
        upgrade = _ensure_directory(root, UPGRADE_ROOT, created)
        transactions = _ensure_directory(root, TRANSACTIONS_ROOT, created)
    return upgrade, transactions, lock


@contextmanager
def _change_guard(root: Path) -> Iterator[None]:
    _upgrade, _transactions, lock = _runtime_paths(root, create=True)
    try:
        with process_supervisor.exclusive_file_lock(lock, label="kit change"):
            yield
    except process_supervisor.ExclusiveLockUnavailable as exc:
        raise KitChangeError("change-busy", str(exc)) from exc
    except OSError as exc:
        raise KitChangeError("change-lock-failed", f"cannot establish kit change lock: {exc}") from exc


def _history(journal: dict[str, Any], state: str, code: str) -> None:
    if state not in JOURNAL_STATES:
        raise ValueError(f"unsupported journal state: {state}")
    history = list(journal.get("history") or [])
    history.append({
        "sequence": len(history) + 1,
        "state": state,
        "at": _now(),
        "code": code,
    })
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
        for index, item in enumerate(history, start=1):
            item["sequence"] = index
    journal["state"] = state
    journal["history"] = history


def _journal_path(root: Path, transaction_id: str) -> Path:
    if TRANSACTION_RE.fullmatch(transaction_id) is None:
        raise KitChangeError("transaction-invalid", "transaction id is malformed")
    return _target(root, f"{TRANSACTIONS_ROOT}/{transaction_id}/journal.json")


def _backup_path(root: Path, transaction_id: str) -> Path:
    return _target(root, f"{TRANSACTIONS_ROOT}/{transaction_id}/backup.json")


def _transaction_directory(root: Path, transaction_id: str, *, create: bool) -> Path:
    relative = f"{TRANSACTIONS_ROOT}/{transaction_id}"
    if create:
        return _ensure_directory(root, relative, [])
    return _target(root, relative, leaf="directory")


def _journal_write(path: Path, journal: dict[str, Any]) -> None:
    _durable_json(path, journal)


def _created_target_directories(root: Path, paths: list[str]) -> list[str]:
    found: set[str] = set()
    for relative in paths:
        parts = PurePosixPath(relative).parts[:-1]
        for index in range(1, len(parts) + 1):
            candidate = PurePosixPath(*parts[:index]).as_posix()
            path = _target(root, candidate, leaf="directory")
            if not path.exists():
                found.add(candidate)
    return sorted(found, key=lambda value: (len(PurePosixPath(value).parts), value))


def _prepare_backup(plan: ChangePlan, transaction_id: str) -> tuple[dict[str, Any], str]:
    tx_dir = _transaction_directory(plan.root, transaction_id, create=True)
    _ensure_directory(plan.root, f"{TRANSACTIONS_ROOT}/{transaction_id}/blobs", [])
    entries: list[dict[str, Any]] = []
    total = 0
    for relative, replacement in plan.replacements.items():
        path = _target(plan.root, relative)
        before = next(
            change["before"]
            for change in plan.preview["material"]["changes"]
            if change["path"] == relative
        )
        if _snapshot(path) != before:
            raise KitChangeError("target-changed", f"{relative} changed after Preview")
        blob: str | None = None
        if before["kind"] == "file":
            content = _stable_bytes(path)
            total += len(content)
            if total > MAX_BACKUP_BYTES:
                raise KitChangeError("backup-too-large", "kit change backup exceeds its safety limit")
            digest = _sha256(content)
            blob = f"blobs/{digest}"
            blob_path = tx_dir / "blobs" / digest
            if not blob_path.exists():
                with blob_path.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
            if _sha256(_stable_bytes(blob_path)) != digest:
                raise KitChangeError("backup-failed", f"backup blob did not verify: {relative}")
        entries.append({"path": relative, "before": before, "blob": blob})
    if len(entries) > MAX_BACKUP_ENTRIES:
        raise KitChangeError("backup-too-large", "kit change has too many backup entries")
    created = _created_target_directories(
        plan.root,
        [*plan.replacements, plan.preview["material"]["core"]["path"] + "/member"],
    )
    value = {
        "schema": BACKUP_SCHEMA,
        "kind": "agent-kit-change-backup",
        "transaction_id": transaction_id,
        "preview_sha256": plan.preview["approval"]["sha256"],
        "entries": entries,
        "created_directories": created,
        "total_bytes": total,
    }
    path = _backup_path(plan.root, transaction_id)
    _durable_json(path, value)
    content = _stable_bytes(path)
    return value, _sha256(content)


def _journal_for(
    plan: ChangePlan,
    transaction_id: str,
    backup_sha256: str,
) -> dict[str, Any]:
    changes = {item["path"]: item for item in plan.preview["material"]["changes"]}
    entries = []
    for sequence, relative in enumerate(plan.replacements, start=1):
        change = changes[relative]
        entries.append({
            "sequence": sequence,
            "path": relative,
            "phase": "activation" if relative == CURRENT_STATE else "project",
            "action": change["action"],
            "before": change["before"],
            "after": change["after"],
            "backup_blob": (
                f"blobs/{change['before']['sha256']}"
                if change["before"]["kind"] == "file"
                else None
            ),
        })
    core_members = [
        {
            "path": str(name),
            "bytes": len(_member_bytes(plan.members, str(name))),
            "sha256": _sha256(_member_bytes(plan.members, str(name))),
            "mode": format(_member_mode(plan.members, str(name)), "04o"),
        }
        for name in sorted(plan.members)
    ]
    journal = {
        "schema": JOURNAL_SCHEMA,
        "kind": "agent-kit-change-transaction",
        "transaction_id": transaction_id,
        "operation": plan.preview["material"]["operation"],
        "preview_sha256": plan.preview["approval"]["sha256"],
        "target_scope_sha256": plan.preview["material"]["target_scope_sha256"],
        "state": "prepared",
        "backup_manifest": "backup.json",
        "backup_manifest_sha256": backup_sha256,
        "core": {
            "path": plan.preview["material"]["core"]["path"],
            "archive_sha256": plan.preview["material"]["core"]["archive_sha256"],
            "state": (
                "create_pending"
                if plan.preview["material"]["core"]["action"] == "create"
                else "reused"
            ),
            "members": core_members,
        },
        "entries": entries,
        "history": [],
        "failure": None,
    }
    _history(journal, "prepared", "backup-complete")
    return journal


def _stage_core(plan: ChangePlan, transaction_id: str) -> None:
    final = _target(plan.root, plan.preview["material"]["core"]["path"], leaf="directory")
    if final.exists():
        if not _core_matches(final, plan.members):
            raise KitChangeError("managed-core-modified", "existing managed core failed verification")
        return
    stage_relative = f"{TRANSACTIONS_ROOT}/{transaction_id}/staged-core"
    stage = _target(plan.root, stage_relative, leaf="directory")
    if stage.exists():
        raise KitChangeError("staging-conflict", "transaction staging directory already exists")
    _ensure_directory(plan.root, stage_relative, [])
    for name in sorted(plan.members):
        relative = _safe_relative(str(name), label="release member")
        destination = stage.joinpath(*PurePosixPath(relative).parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            raise KitChangeError("release-invalid", f"duplicate staged member: {relative}")
        with destination.open("xb") as handle:
            handle.write(_member_bytes(plan.members, relative))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            destination.chmod(_member_mode(plan.members, relative))
        except OSError:
            if os.name != "nt":
                raise
    if not _core_matches(stage, plan.members):
        raise KitChangeError("core-stage-failed", "staged managed core did not verify")
    _ensure_directory(plan.root, RELEASES_ROOT, [])
    try:
        _replace_file(stage, final)
    except OSError as exc:
        raise KitChangeError("core-stage-failed", f"could not publish managed core: {exc}") from exc
    if not _core_matches(final, plan.members):
        raise KitChangeError("core-stage-failed", "published managed core did not verify")


def _apply_entry(plan: ChangePlan, relative: str, expected: dict[str, Any]) -> None:
    path = _target(plan.root, relative)
    before = expected["before"]
    after = expected["after"]
    if _snapshot(path) != before:
        raise KitChangeError("target-changed", f"{relative} changed during Apply")
    replacement = plan.replacements[relative]
    if replacement is None:
        try:
            path.unlink()
            _sync_directory(path.parent)
        except OSError as exc:
            raise KitChangeError("target-write-failed", f"cannot remove {relative}: {exc}") from exc
    else:
        parent = PurePosixPath(relative).parent.as_posix()
        if parent != ".":
            _ensure_directory(plan.root, parent, [])
        try:
            _atomic_bytes(path, replacement, str(plan.modes[relative]))
        except OSError as exc:
            raise KitChangeError("target-write-failed", f"cannot replace {relative}: {exc}") from exc
    if _snapshot(path) != after:
        raise KitChangeError("target-write-failed", f"replacement did not verify: {relative}")


def _failpoint(_name: str) -> None:
    """Crash-injection seam used by focused transaction tests."""


def _journal_files(root: Path) -> list[Path]:
    transactions = _target(root, TRANSACTIONS_ROOT, leaf="directory")
    if not transactions.exists():
        return []
    found: list[Path] = []
    for child in transactions.iterdir():
        if child.is_symlink() or _is_reparse(child) or not child.is_dir():
            raise KitChangeError("transaction-invalid", "upgrade transaction directory is unsafe")
        if TRANSACTION_RE.fullmatch(child.name) is None:
            continue
        journal = child / "journal.json"
        if journal.is_file():
            found.append(journal)
    return sorted(found)


def _load_journal(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(_stable_bytes(path).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KitChangeError("journal-invalid", f"transaction journal is damaged: {exc}") from exc
    expected = {
        "schema",
        "kind",
        "transaction_id",
        "operation",
        "preview_sha256",
        "target_scope_sha256",
        "state",
        "backup_manifest",
        "backup_manifest_sha256",
        "core",
        "entries",
        "history",
        "failure",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise KitChangeError("journal-invalid", "transaction journal fields are malformed")
    transaction_id = str(value.get("transaction_id") or "")
    if (
        value["schema"] != JOURNAL_SCHEMA
        or value["kind"] != "agent-kit-change-transaction"
        or TRANSACTION_RE.fullmatch(transaction_id) is None
        or path.parent.name != transaction_id
        or value["operation"] not in {"install", "upgrade"}
        or not SHA256_RE.fullmatch(str(value["preview_sha256"] or ""))
        or not SHA256_RE.fullmatch(str(value["target_scope_sha256"] or ""))
        or value["state"] not in JOURNAL_STATES
        or value["backup_manifest"] != "backup.json"
        or not SHA256_RE.fullmatch(str(value["backup_manifest_sha256"] or ""))
        or not isinstance(value["entries"], list)
        or not isinstance(value["history"], list)
    ):
        raise KitChangeError("journal-invalid", "transaction journal is malformed")
    core = value["core"]
    if not isinstance(core, dict) or set(core) != {
        "path", "archive_sha256", "state", "members"
    }:
        raise KitChangeError("journal-invalid", "transaction core state is malformed")
    _safe_relative(core["path"])
    if not SHA256_RE.fullmatch(str(core["archive_sha256"] or "")) or core["state"] not in {
        "create_pending",
        "created",
        "reused",
    }:
        raise KitChangeError("journal-invalid", "transaction core identity is malformed")
    members = core["members"]
    if not isinstance(members, list) or not members or len(members) > MAX_BACKUP_ENTRIES:
        raise KitChangeError("journal-invalid", "transaction core members are malformed")
    member_paths: list[str] = []
    for member in members:
        if not isinstance(member, dict) or set(member) != {
            "path", "bytes", "sha256", "mode"
        }:
            raise KitChangeError("journal-invalid", "transaction core member is malformed")
        member_path = _safe_relative(member["path"], label="transaction core member")
        if (
            not isinstance(member["bytes"], int)
            or isinstance(member["bytes"], bool)
            or not 0 <= member["bytes"] <= getattr(
                release, "MAX_FILE_BYTES", MAX_MANAGED_FILE_BYTES
            )
            or SHA256_RE.fullmatch(str(member["sha256"] or "")) is None
            or member["mode"] not in {"0644", "0755"}
        ):
            raise KitChangeError(
                "journal-invalid", "transaction core member values are malformed"
            )
        member_paths.append(member_path)
    if member_paths != sorted(member_paths) or len(member_paths) != len(set(member_paths)):
        raise KitChangeError(
            "journal-invalid", "transaction core members must be sorted and unique"
        )
    sequences: list[int] = []
    paths: list[str] = []
    for raw in value["entries"]:
        if not isinstance(raw, dict) or set(raw) != {
            "sequence",
            "path",
            "phase",
            "action",
            "before",
            "after",
            "backup_blob",
        }:
            raise KitChangeError("journal-invalid", "transaction entry is malformed")
        relative = _safe_relative(raw["path"])
        if raw["phase"] not in {"project", "activation"} or raw["action"] not in {
            "create",
            "modify",
            "delete",
        }:
            raise KitChangeError("journal-invalid", "transaction entry action is malformed")
        _validate_snapshot(raw["before"])
        _validate_snapshot(raw["after"])
        blob = raw["backup_blob"]
        if blob is not None:
            safe_blob = _safe_relative(blob, label="backup blob")
            if not safe_blob.startswith("blobs/") or not SHA256_RE.fullmatch(safe_blob[6:]):
                raise KitChangeError("journal-invalid", "backup blob path is malformed")
        sequences.append(raw["sequence"])
        paths.append(relative)
    if sequences != list(range(1, len(sequences) + 1)) or len(paths) != len(set(paths)):
        raise KitChangeError("journal-invalid", "transaction entries are not unique and ordered")
    return value


def _load_backup(root: Path, journal: dict[str, Any]) -> dict[str, Any]:
    path = _backup_path(root, journal["transaction_id"])
    content = _stable_bytes(path)
    if _sha256(content) != journal["backup_manifest_sha256"]:
        raise KitChangeError("backup-invalid", "backup manifest hash does not match the journal")
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KitChangeError("backup-invalid", f"backup manifest is damaged: {exc}") from exc
    expected = {
        "schema",
        "kind",
        "transaction_id",
        "preview_sha256",
        "entries",
        "created_directories",
        "total_bytes",
    }
    if not isinstance(value, dict) or set(value) != expected:
        raise KitChangeError("backup-invalid", "backup manifest fields are malformed")
    if (
        value["schema"] != BACKUP_SCHEMA
        or value["kind"] != "agent-kit-change-backup"
        or value["transaction_id"] != journal["transaction_id"]
        or value["preview_sha256"] != journal["preview_sha256"]
        or not isinstance(value["entries"], list)
        or not isinstance(value["created_directories"], list)
        or not isinstance(value["total_bytes"], int)
    ):
        raise KitChangeError("backup-invalid", "backup manifest is malformed")
    expected_entries = {entry["path"]: entry for entry in journal["entries"]}
    backup_paths: set[str] = set()
    for raw in value["entries"]:
        if not isinstance(raw, dict) or set(raw) != {"path", "before", "blob"}:
            raise KitChangeError("backup-invalid", "backup entry is malformed")
        relative = _safe_relative(raw["path"])
        before = _validate_snapshot(raw["before"])
        if relative not in expected_entries or before != expected_entries[relative]["before"]:
            raise KitChangeError("backup-invalid", "backup entry does not match the journal")
        blob = raw["blob"]
        if before["kind"] == "file":
            if not isinstance(blob, str) or not blob.startswith("blobs/"):
                raise KitChangeError("backup-invalid", "backup blob is missing")
            safe_blob = _safe_relative(blob, label="backup blob")
            digest = safe_blob[6:]
            if not SHA256_RE.fullmatch(digest):
                raise KitChangeError("backup-invalid", "backup blob identity is malformed")
            blob_path = path.parent.joinpath(*PurePosixPath(safe_blob).parts)
            if _sha256(_stable_bytes(blob_path)) != digest or digest != before["sha256"]:
                raise KitChangeError("backup-invalid", f"backup blob is damaged: {relative}")
        elif blob is not None:
            raise KitChangeError("backup-invalid", "absent file has an unexpected backup blob")
        backup_paths.add(relative)
    if backup_paths != set(expected_entries):
        raise KitChangeError("backup-invalid", "backup manifest entry set is incomplete")
    for directory in value["created_directories"]:
        _safe_relative(directory, label="created directory")
    return value


def _mark_blocked(root: Path, path: Path, journal: dict[str, Any], error: KitChangeError) -> None:
    journal["failure"] = {"code": error.code, "at": _now(), "detail": error.detail}
    _history(journal, "blocked", error.code)
    _journal_write(path, journal)


def _restore_entry(root: Path, entry: dict[str, Any], backup: dict[str, Any]) -> None:
    relative = entry["path"]
    path = _target(root, relative)
    current = _snapshot(path)
    before = entry["before"]
    after = entry["after"]
    if current == before:
        return
    if current != after:
        raise KitChangeError("rollback-conflict", f"{relative} changed after Apply")
    if before["kind"] == "absent":
        try:
            path.unlink()
            _sync_directory(path.parent)
        except OSError as exc:
            raise KitChangeError("rollback-failed", f"cannot remove {relative}: {exc}") from exc
        return
    backup_entry = next(item for item in backup["entries"] if item["path"] == relative)
    blob = str(backup_entry["blob"])
    blob_path = _backup_path(root, backup["transaction_id"]).parent.joinpath(
        *PurePosixPath(blob).parts
    )
    content = _stable_bytes(blob_path)
    try:
        _atomic_bytes(path, content, before["mode"])
    except OSError as exc:
        raise KitChangeError("rollback-failed", f"cannot restore {relative}: {exc}") from exc
    if _snapshot(path) != before:
        raise KitChangeError("rollback-failed", f"restored bytes did not verify: {relative}")


def _core_matches_journal(core: Path, members: list[dict[str, Any]]) -> bool:
    if core.is_symlink() or _is_reparse(core) or not core.is_dir():
        return False
    expected = [str(member["path"]) for member in members]
    actual: list[str] = []
    for current, directories, filenames in os.walk(core, topdown=True, followlinks=False):
        base = Path(current)
        for name in list(directories):
            child = base / name
            if child.is_symlink() or _is_reparse(child) or not child.is_dir():
                return False
        for name in filenames:
            child = base / name
            if child.is_symlink() or _is_reparse(child) or not child.is_file():
                return False
            actual.append(child.relative_to(core).as_posix())
    if sorted(actual) != expected:
        return False
    by_path = {str(member["path"]): member for member in members}
    for relative in expected:
        path = core.joinpath(*PurePosixPath(relative).parts)
        expected_member = by_path[relative]
        try:
            content = _stable_bytes(
                path,
                limit=getattr(release, "MAX_FILE_BYTES", MAX_MANAGED_FILE_BYTES),
            )
        except KitChangeError:
            return False
        if (
            len(content) != expected_member["bytes"]
            or _sha256(content) != expected_member["sha256"]
        ):
            return False
        if os.name != "nt" and format(stat.S_IMODE(path.stat().st_mode), "04o") != expected_member["mode"]:
            return False
    return True


def _remove_created_core(root: Path, journal: dict[str, Any]) -> None:
    core_state = journal["core"]["state"]
    if core_state == "reused":
        return
    relative = str(journal["core"]["path"])
    core = _target(root, relative, leaf="directory")
    if not core.exists():
        return
    members = journal["core"]["members"]
    if not _core_matches_journal(core, members):
        raise KitChangeError(
            "rollback-conflict", "new managed core changed after Apply"
        )
    for member in reversed(members):
        path = core.joinpath(*PurePosixPath(str(member["path"])).parts)
        try:
            path.unlink()
        except OSError as exc:
            raise KitChangeError(
                "rollback-failed", f"cannot remove managed core member {member['path']}: {exc}"
            ) from exc
    directory_set: set[str] = set()
    for member in members:
        parent = PurePosixPath(str(member["path"])).parent
        while parent.as_posix() != ".":
            directory_set.add(parent.as_posix())
            parent = parent.parent
    directories = sorted(
        directory_set,
        key=lambda value: (-len(PurePosixPath(value).parts), value),
    )
    for directory in directories:
        try:
            core.joinpath(*PurePosixPath(directory).parts).rmdir()
        except OSError as exc:
            raise KitChangeError(
                "rollback-failed", f"cannot remove managed core directory {directory}: {exc}"
            ) from exc
    try:
        core.rmdir()
        _sync_directory(core.parent)
    except OSError as exc:
        raise KitChangeError("rollback-failed", f"cannot remove managed core: {exc}") from exc


def _rollback_locked(root: Path, path: Path, journal: dict[str, Any]) -> dict[str, Any]:
    try:
        backup = _load_backup(root, journal)
        core = _target(root, str(journal["core"]["path"]), leaf="directory")
        if (
            journal["core"]["state"] != "reused"
            and core.exists()
            and not _core_matches_journal(core, journal["core"]["members"])
        ):
            raise KitChangeError(
                "rollback-conflict", "new managed core changed after Apply"
            )
        for entry in journal["entries"]:
            current = _snapshot(_target(root, entry["path"]))
            if current not in (entry["before"], entry["after"]):
                raise KitChangeError("rollback-conflict", f"{entry['path']} has a third version")
        _history(journal, "rolling_back", "rollback-started")
        _journal_write(path, journal)

        project_entries = [entry for entry in journal["entries"] if entry["phase"] == "project"]
        activation_entries = [
            entry for entry in journal["entries"] if entry["phase"] == "activation"
        ]
        for entry in [*reversed(project_entries), *reversed(activation_entries)]:
            _restore_entry(root, entry, backup)

        _remove_created_core(root, journal)

        for relative in sorted(
            backup["created_directories"],
            key=lambda value: (-len(PurePosixPath(value).parts), value),
        ):
            directory = _target(root, relative, leaf="directory")
            if directory.is_dir():
                try:
                    directory.rmdir()
                except OSError:
                    pass
        for entry in journal["entries"]:
            if _snapshot(_target(root, entry["path"])) != entry["before"]:
                raise KitChangeError("rollback-failed", f"rollback did not verify: {entry['path']}")
        journal["failure"] = None
        _history(journal, "rolled_back", "prior-state-restored")
        _journal_write(path, journal)
        return {
            "ok": True,
            "status": "rolled_back",
            "transaction_id": journal["transaction_id"],
            "preview_sha256": journal["preview_sha256"],
        }
    except KitChangeError as exc:
        if journal.get("state") != "blocked":
            _mark_blocked(root, path, journal, exc)
        raise


def _open_transactions(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in _journal_files(root):
        journal = _load_journal(path)
        if journal["state"] not in TERMINAL_STATES:
            found.append((path, journal))
    return found


def apply(
    root: Path,
    archive: Path,
    confirm_sha256: str,
    *,
    game_root: str | None = None,
) -> dict[str, Any]:
    """Apply one exact Preview after re-reading every input under a process lock."""
    canonical_root = _canonical_root(root)
    if not SHA256_RE.fullmatch(str(confirm_sha256 or "")):
        raise KitChangeError("approval-invalid", "Apply needs the full Preview SHA-256")
    with _change_guard(canonical_root):
        pending = _open_transactions(canonical_root)
        if pending:
            raise KitChangeError(
                "recovery-required",
                f"transaction {pending[0][1]['transaction_id']} must be resumed or rolled back first",
            )
        plan = _build_plan(canonical_root, archive, game_root=game_root)
        actual = plan.preview["approval"]["sha256"]
        if not hmac.compare_digest(actual, confirm_sha256):
            raise KitChangeError("approval-mismatch", "project or release changed after Preview")
        blockers = plan.preview["material"]["blockers"]
        if blockers:
            raise KitChangeError("preview-blocked", blockers[0]["detail"])
        if plan.noop:
            return {
                "ok": True,
                "status": "already_current",
                "transaction_id": None,
                "preview_sha256": actual,
            }

        transaction_id = uuid.uuid4().hex
        backup, backup_sha = _prepare_backup(plan, transaction_id)
        del backup
        journal = _journal_for(plan, transaction_id, backup_sha)
        journal_path = _journal_path(canonical_root, transaction_id)
        _journal_write(journal_path, journal)
        try:
            _failpoint("after-prepared")
            _stage_core(plan, transaction_id)
            if journal["core"]["state"] == "create_pending":
                journal["core"]["state"] = "created"
            _history(journal, "core_staged", "verified-core-staged")
            _journal_write(journal_path, journal)
            _failpoint("after-core-staged")

            _history(journal, "applying", "project-write-started")
            _journal_write(journal_path, journal)
            changes = {item["path"]: item for item in plan.preview["material"]["changes"]}
            for relative in plan.replacements:
                _apply_entry(plan, relative, changes[relative])
                _failpoint(f"after-entry:{relative}")
                if relative == CURRENT_STATE:
                    _history(journal, "activated", "managed-core-activated")
                    _journal_write(journal_path, journal)
                    _failpoint("after-activated")
            _history(journal, "applied", "exact-change-applied")
            _journal_write(journal_path, journal)
            return {
                "ok": True,
                "status": "applied",
                "transaction_id": transaction_id,
                "preview_sha256": actual,
                "verification": "pending",
            }
        except Exception as exc:
            try:
                _rollback_locked(canonical_root, journal_path, journal)
            except KitChangeError as recovery:
                raise KitChangeError(
                    "apply-recovery-failed",
                    f"Apply failed and rollback is incomplete: {recovery.detail}",
                ) from exc
            if isinstance(exc, KitChangeError):
                raise
            raise KitChangeError("apply-failed", f"Apply failed and prior bytes were restored: {exc}") from exc


def _select_transaction(root: Path, transaction_id: str | None, *, include_terminal: bool) -> tuple[Path, dict[str, Any]]:
    journals = [(path, _load_journal(path)) for path in _journal_files(root)]
    if transaction_id is not None:
        path = _journal_path(root, transaction_id)
        if not path.is_file():
            raise KitChangeError("transaction-missing", f"transaction does not exist: {transaction_id}")
        return path, _load_journal(path)
    candidates = [
        (path, journal)
        for path, journal in journals
        if include_terminal or journal["state"] not in TERMINAL_STATES
    ]
    if not candidates:
        raise KitChangeError("transaction-missing", "no matching kit change transaction exists")
    if len(candidates) == 1:
        return candidates[0]
    current = _snapshot(_target(root, CURRENT_STATE))
    matching = [
        (path, journal)
        for path, journal in candidates
        if any(
            entry["phase"] == "activation" and entry["after"] == current
            for entry in journal["entries"]
        )
    ]
    if len(matching) == 1:
        return matching[0]
    raise KitChangeError("transaction-ambiguous", "name the exact transaction to change")


def rollback(root: Path, transaction_id: str | None = None) -> dict[str, Any]:
    """Restore the exact prior bytes for one applied or interrupted change."""
    canonical_root = _canonical_root(root)
    with _change_guard(canonical_root):
        path, journal = _select_transaction(canonical_root, transaction_id, include_terminal=True)
        if journal["state"] == "rolled_back":
            return {
                "ok": True,
                "status": "rolled_back",
                "transaction_id": journal["transaction_id"],
                "preview_sha256": journal["preview_sha256"],
            }
        if journal["state"] == "blocked":
            failure = journal.get("failure")
            retryable = (
                transaction_id is not None
                and isinstance(failure, dict)
                and set(failure) == {"code", "at", "detail"}
                and failure.get("code") in {"rollback-conflict", "rollback-failed"}
            )
            if not retryable:
                raise KitChangeError(
                    "transaction-blocked", "blocked transaction needs manual review"
                )
        return _rollback_locked(canonical_root, path, journal)


def resume(root: Path) -> dict[str, Any]:
    """Recover one interrupted transaction without choosing a new direction."""
    canonical_root = _canonical_root(root)
    with _change_guard(canonical_root):
        path, journal = _select_transaction(canonical_root, None, include_terminal=False)
        state = journal["state"]
        if state == "blocked":
            raise KitChangeError("transaction-blocked", "blocked transaction needs manual review")
        if state in {"prepared", "core_staged", "applying", "rolling_back"}:
            return _rollback_locked(canonical_root, path, journal)
        if state == "activated":
            for entry in journal["entries"]:
                current = _snapshot(_target(canonical_root, entry["path"]))
                if current != entry["after"]:
                    return _rollback_locked(canonical_root, path, journal)
            _history(journal, "applied", "activated-change-recovered")
            _journal_write(path, journal)
            return {
                "ok": True,
                "status": "applied",
                "transaction_id": journal["transaction_id"],
                "preview_sha256": journal["preview_sha256"],
                "verification": "pending",
            }
        raise KitChangeError("transaction-invalid", f"cannot resume transaction state {state}")


def inspect_transaction(
    root: Path,
    *,
    preview_sha256: str,
    target_scope_sha256: str,
) -> dict[str, Any]:
    """Find one exact durable transaction after a controller interruption."""
    if SHA256_RE.fullmatch(str(preview_sha256 or "")) is None:
        raise KitChangeError("preview-invalid", "transaction lookup needs a full Preview SHA-256")
    if SHA256_RE.fullmatch(str(target_scope_sha256 or "")) is None:
        raise KitChangeError("scope-invalid", "transaction lookup needs a full target-scope SHA-256")
    canonical_root = _canonical_root(root)
    with _change_guard(canonical_root):
        matches: list[dict[str, Any]] = []
        for path in _journal_files(canonical_root):
            journal = _load_journal(path)
            if (
                hmac.compare_digest(journal["preview_sha256"], preview_sha256)
                and hmac.compare_digest(
                    journal["target_scope_sha256"], target_scope_sha256
                )
            ):
                matches.append(journal)
        if not matches:
            raise KitChangeError(
                "transaction-missing", "no transaction matches the exact reviewed change"
            )
        if len(matches) != 1:
            raise KitChangeError(
                "transaction-ambiguous", "more than one transaction matches the reviewed change"
            )
        journal = matches[0]
        return {
            "ok": True,
            "status": journal["state"],
            "transaction_id": journal["transaction_id"],
            "preview_sha256": journal["preview_sha256"],
            "target_scope_sha256": journal["target_scope_sha256"],
        }
