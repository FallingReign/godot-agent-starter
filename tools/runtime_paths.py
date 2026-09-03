#!/usr/bin/env python3
"""Resolve private runtime paths from the portable kit configuration.

Runtime data is mutable, machine-local, and may contain private session
evidence. It belongs under the configured project-local runtime root, never in
``docs/`` and never in a release archive. This module only resolves paths;
callers opt in to creating directories.
"""
from __future__ import annotations

import json
import os
import stat
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TOOLS = Path(__file__).resolve().parent
CORE_ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
import project_context  # noqa: E402

# Runtime state always belongs to the marked project, not to the immutable
# release directory that happens to contain this module.
DEFAULT_ROOT = project_context.resolve_active_installation(CORE_ROOT).project_root
CONFIG_NAME = "kit.config.json"
CONFIG_SCHEMA = 1
DEFAULT_RUNTIME = ".kit/runtime"
PRIVATE_ROOT = ".kit"
CONTROLLER_RUNTIME_ENV = "AGENT_KIT_CONTROLLER_RUNTIME"


class RuntimeConfigError(ValueError):
    """The runtime configuration is absent, malformed, or unsafe."""


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _is_reparse(info: object) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return bool(int(getattr(info, "st_file_attributes", 0)) & marker)


def _require_unredirected_directories(root: Path, relative: Path) -> None:
    """Reject an existing runtime component that can redirect or hold data."""
    cursor = root
    for component in relative.parts:
        cursor = cursor / component
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            break
        except OSError as exc:
            raise RuntimeConfigError(
                f"runtime_root component is unreadable: {cursor}: {exc}"
            ) from exc
        if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
            raise RuntimeConfigError(
                f"runtime_root component must be an unredirected directory: {cursor}"
            )


def load_config(root: Path = DEFAULT_ROOT) -> dict[str, Any]:
    """Load and minimally validate the project-owned kit configuration."""
    root = root.resolve(strict=True)
    path = root / CONFIG_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeConfigError(f"cannot read {CONFIG_NAME}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeConfigError(f"{CONFIG_NAME} root must be an object")
    if value.get("schema") != CONFIG_SCHEMA:
        raise RuntimeConfigError(
            f"unsupported {CONFIG_NAME} schema {value.get('schema')!r}; expected {CONFIG_SCHEMA}"
        )
    return value


def _relative_private_root(root: Path, configured: object) -> Path:
    raw = str(configured or DEFAULT_RUNTIME).strip()
    candidate = Path(raw.replace("\\", "/"))
    if not raw or candidate.is_absolute() or ".." in candidate.parts:
        raise RuntimeConfigError("runtime_root must be a non-empty project-relative path without '..'")
    portable = candidate.as_posix()
    if (not portable.startswith(PRIVATE_ROOT + "/")
            or portable == PRIVATE_ROOT):
        raise RuntimeConfigError(
            "runtime_root must be a descendant of the project-local .kit directory"
        )
    _require_unredirected_directories(root, candidate)
    resolved = (root / candidate).resolve(strict=False)
    private = (root / PRIVATE_ROOT).resolve(strict=False)
    if (not _inside(resolved, root) or not _inside(resolved, private)
            or resolved == private):
        raise RuntimeConfigError(
            "runtime_root must be a descendant of the project-local .kit directory"
        )
    return resolved


def _paths_overlap(first: Path, second: Path) -> bool:
    return _inside(first, second) or _inside(second, first)


def ensure_private_directory(paths: "RuntimePaths", relative: str) -> Path:
    """Create one runtime child without following an existing redirect.

    ``Path.mkdir(parents=True)`` follows a symlink or junction already present
    below the runtime root.  Callers which create executable scratch space must
    instead validate every existing component as it is traversed.
    """
    raw = str(relative).strip().replace("\\", "/")
    candidate = Path(raw)
    if (
        not raw
        or candidate.is_absolute()
        or candidate == Path(".")
        or any(part in ("", ".", "..") for part in candidate.parts)
    ):
        raise RuntimeConfigError(
            "private runtime directory must be a non-empty relative path without '..'"
        )
    try:
        runtime_relative = paths.runtime.relative_to(paths.root)
    except ValueError as exc:
        raise RuntimeConfigError("runtime root is outside the project") from exc
    _require_unredirected_directories(paths.root, runtime_relative)
    paths.runtime.mkdir(parents=True, exist_ok=True)
    _require_unredirected_directories(paths.root, runtime_relative)

    cursor = paths.runtime
    for component in candidate.parts:
        child = cursor / component
        try:
            info = child.lstat()
        except FileNotFoundError:
            try:
                child.mkdir()
                info = child.lstat()
            except OSError as exc:
                raise RuntimeConfigError(
                    f"private runtime directory is unavailable: {child}: {exc}"
                ) from exc
        except OSError as exc:
            raise RuntimeConfigError(
                f"private runtime directory is unreadable: {child}: {exc}"
            ) from exc
        if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
            raise RuntimeConfigError(
                f"private runtime directory must be unredirected: {child}"
            )
        cursor = child

    try:
        resolved_runtime = paths.runtime.resolve(strict=True)
        resolved = cursor.resolve(strict=True)
    except OSError as exc:
        raise RuntimeConfigError(
            f"private runtime directory is unavailable: {cursor}: {exc}"
        ) from exc
    if not _inside(resolved, resolved_runtime):
        raise RuntimeConfigError("private runtime directory escaped the runtime root")
    return cursor


def _absolute_private_root(root: Path, configured: object) -> Path:
    if not isinstance(configured, str) or not configured.strip():
        raise RuntimeConfigError(
            f"{CONTROLLER_RUNTIME_ENV} must be a canonical absolute directory"
        )
    candidate = Path(configured)
    if not candidate.is_absolute() or ".." in candidate.parts:
        raise RuntimeConfigError(
            f"{CONTROLLER_RUNTIME_ENV} must be a canonical absolute directory"
        )
    try:
        canonical = candidate.resolve(strict=True)
    except OSError as exc:
        raise RuntimeConfigError(
            f"{CONTROLLER_RUNTIME_ENV} directory is unavailable: {exc}"
        ) from exc
    if os.path.normcase(os.path.abspath(str(candidate))) != os.path.normcase(str(canonical)):
        raise RuntimeConfigError(
            f"{CONTROLLER_RUNTIME_ENV} must name its canonical directory"
        )
    anchor = Path(candidate.anchor)
    _require_unredirected_directories(anchor, Path(*candidate.parts[1:]))
    project = root.resolve(strict=True)
    core = CORE_ROOT.resolve(strict=True)
    if _paths_overlap(canonical, project) or _paths_overlap(canonical, core):
        raise RuntimeConfigError(
            f"{CONTROLLER_RUNTIME_ENV} must be outside the project and release directories"
        )
    return canonical


@dataclass(frozen=True)
class RuntimePaths:
    root: Path
    runtime: Path

    @property
    def evidence(self) -> Path:
        return self.runtime / "evidence"

    @property
    def session_evidence(self) -> Path:
        return self.evidence / "sessions"

    @property
    def evidence_packs(self) -> Path:
        return self.evidence / "packs"

    @property
    def evidence_pointer(self) -> Path:
        return self.evidence / "latest.json"

    @property
    def retro_queue(self) -> Path:
        return self.runtime / "retro" / "queue"

    @property
    def retro_sdk(self) -> Path:
        return self.runtime / "retro" / "sdk"

    @property
    def retro_thread(self) -> Path:
        return self.runtime / "retro" / "thread.json"

    @property
    def retro_ran(self) -> Path:
        return self.runtime / "retro" / "ran.json"

    @property
    def retro_completion_lock(self) -> Path:
        return self.runtime / "retro" / "completion.lock"

    @property
    def board_state(self) -> Path:
        return self.runtime / "board" / "state.json"

    @property
    def board_lock(self) -> Path:
        return self.runtime / "board" / "board.lock"

    @property
    def board_log(self) -> Path:
        return self.runtime / "board" / "board.log"

    @property
    def board_runs(self) -> Path:
        return self.runtime / "board" / "runs"

    @property
    def verification_runs(self) -> Path:
        return self.runtime / "verification" / "runs"

    @property
    def verification_latest(self) -> Path:
        return self.runtime / "verification" / "latest.json"

    @property
    def plan_decisions(self) -> Path:
        return self.runtime / "cockpit" / "plan-decisions"

    @property
    def plan_decision_lock(self) -> Path:
        return self.runtime / "cockpit" / "decision.lock"

    @property
    def plan_decision_transaction(self) -> Path:
        return self.runtime / "cockpit" / "decision-transaction.json"

    @property
    def dispatch_workspaces(self) -> Path:
        return self.runtime / "dispatch" / "workspaces"

    def ensure(self) -> "RuntimePaths":
        """Create only private runtime directories, never project content."""
        for path in (
            self.session_evidence,
            self.evidence_packs,
            self.retro_queue,
            self.retro_sdk,
            self.board_runs,
            self.verification_runs,
            self.plan_decisions,
            self.dispatch_workspaces,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self

    def status(self) -> dict[str, str | int]:
        return {
            "root": self.root.as_posix(),
            "runtime": self.runtime.as_posix(),
            "schema": CONFIG_SCHEMA,
        }


def resolve(root: Path = DEFAULT_ROOT, *, create: bool = False) -> RuntimePaths:
    canonical = root.resolve(strict=True)
    config = load_config(canonical)
    paths = RuntimePaths(canonical, _relative_private_root(canonical, config.get("runtime_root")))
    return paths.ensure() if create else paths


def resolve_controller(
    root: Path = DEFAULT_ROOT,
    *,
    environment: Mapping[str, str] | None = None,
) -> RuntimePaths:
    """Resolve the lifecycle controller runtime without moving project state."""
    project_paths = resolve(root)
    source = os.environ if environment is None else environment
    if CONTROLLER_RUNTIME_ENV not in source or source[CONTROLLER_RUNTIME_ENV] == "":
        return project_paths
    runtime = _absolute_private_root(project_paths.root, source[CONTROLLER_RUNTIME_ENV])
    return RuntimePaths(project_paths.root, runtime)
