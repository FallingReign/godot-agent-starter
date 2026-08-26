#!/usr/bin/env python3
"""Resolve private runtime paths from the portable kit configuration.

Runtime data is mutable, machine-local, and may contain private session
evidence. It belongs under the configured project-local runtime root, never in
``docs/`` and never in a release archive. This module only resolves paths;
callers opt in to creating directories.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_NAME = "kit.config.json"
CONFIG_SCHEMA = 1
DEFAULT_RUNTIME = ".kit/runtime"


class RuntimeConfigError(ValueError):
    """The runtime configuration is absent, malformed, or unsafe."""


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


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
    candidate = Path(raw)
    if not raw or candidate.is_absolute() or ".." in candidate.parts:
        raise RuntimeConfigError("runtime_root must be a non-empty project-relative path without '..'")
    resolved = (root / candidate).resolve(strict=False)
    if not _inside(resolved, root) or resolved == root:
        raise RuntimeConfigError("runtime_root must remain private to the project root")
    return resolved


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
