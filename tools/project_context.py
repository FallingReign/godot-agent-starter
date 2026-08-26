#!/usr/bin/env python3
"""Portable project-root discovery and path-policy primitives.

The kit is located by a versioned marker rather than by game content or a
particular repository layout.  This module is deliberately read-only: callers
decide when to create the marker and runtime directory.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Sequence

SCHEMA = 1
MARKER_NAME = ".agent-kit.json"
MARKER_KIND = "portable-agent-kit-root"
CONFIG_NAME = "kit.config.json"
GAME_LAYOUTS = (".", "src")
DEFAULT_RUNTIME_ROOT = ".kit/runtime"


class ProjectContextError(ValueError):
    """A project path or marker cannot establish a safe context."""


def marker_document() -> dict:
    """Return the complete stable marker contract for a new distribution."""
    return {"kind": MARKER_KIND, "schema": SCHEMA}


def _portable(path: Path) -> str:
    return path.as_posix()


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _canonical_directory(value: str | Path, label: str) -> Path:
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectContextError(f"{label} is not a readable path: {value!s}") from exc
    if not path.is_dir():
        raise ProjectContextError(f"{label} must be a directory: {path}")
    return path


def _relative_has_traversal(value: str | Path) -> bool:
    return not Path(value).is_absolute() and ".." in Path(value).parts


def _resolve_within(root: Path, value: str | Path, label: str,
                    *, allow_root: bool = True) -> Path:
    raw = Path(value).expanduser()
    if _relative_has_traversal(raw):
        raise ProjectContextError(f"{label} may not contain '..': {value!s}")
    candidate = raw if raw.is_absolute() else root / raw
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ProjectContextError(f"{label} cannot be resolved: {value!s}") from exc
    if not _is_within(resolved, root):
        raise ProjectContextError(f"{label} escapes {root}: {resolved}")
    if not allow_root and resolved == root:
        raise ProjectContextError(f"{label} must be private to, not equal to, {root}")
    return resolved


def _validate_marker(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ProjectContextError(f"kit marker must be a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProjectContextError(f"kit marker is not readable JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ProjectContextError(f"kit marker root must be an object: {path}")
    if value.get("schema") != SCHEMA or value.get("kind") != MARKER_KIND:
        raise ProjectContextError(
            f"unsupported kit marker contract in {path}; expected {marker_document()}"
        )


def locate_kit_root(project: str | Path) -> tuple[Path, Path]:
    """Return ``(kit_root, marker_path)`` nearest to an existing project path."""
    project_root = _canonical_directory(project, "project")
    for candidate in (project_root, *project_root.parents):
        marker = candidate / MARKER_NAME
        if marker.exists() or marker.is_symlink():
            _validate_marker(marker)
            return candidate.resolve(strict=True), marker.resolve(strict=True)
    raise ProjectContextError(
        f"no {MARKER_NAME} marker found from {project_root} to the filesystem root"
    )


@dataclass(frozen=True)
class PathPolicy:
    """Canonical allow/deny roots for verifying repository-relative diffs.

    Forbidden roots always win over owned roots.  A candidate inside the
    policy anchor but outside every owned root is ``unowned``; traversal and
    paths outside the anchor are rejected as malformed input.
    """

    anchor: Path
    owned_roots: tuple[Path, ...]
    forbidden_roots: tuple[Path, ...]

    @classmethod
    def create(cls, anchor: Path, owned: Iterable[str | Path],
               forbidden: Iterable[str | Path] = ()) -> "PathPolicy":
        canonical_anchor = _canonical_directory(anchor, "policy anchor")
        owned_roots = tuple(sorted(
            {_resolve_within(canonical_anchor, value, "owned path") for value in owned},
            key=_portable,
        ))
        forbidden_roots = tuple(sorted(
            {_resolve_within(canonical_anchor, value, "forbidden path")
             for value in forbidden},
            key=_portable,
        ))
        if not owned_roots:
            raise ProjectContextError("a dispatch path policy needs at least one owned root")
        return cls(canonical_anchor, owned_roots, forbidden_roots)

    def classify(self, value: str | Path) -> str:
        candidate = _resolve_within(self.anchor, value, "candidate path")
        if any(_is_within(candidate, root) for root in self.forbidden_roots):
            return "forbidden"
        if any(_is_within(candidate, root) for root in self.owned_roots):
            return "owned"
        return "unowned"

    def allows(self, value: str | Path) -> bool:
        return self.classify(value) == "owned"

    def violations(self, values: Iterable[str | Path]) -> tuple[str, ...]:
        errors: set[str] = set()
        for value in values:
            rendered = str(value).replace("\\", "/")
            try:
                classification = self.classify(value)
            except ProjectContextError as exc:
                errors.add(f"{rendered}: {exc}")
                continue
            if classification != "owned":
                errors.add(f"{rendered}: {classification}")
        return tuple(sorted(errors))

    def status(self) -> dict:
        return {
            "anchor": _portable(self.anchor),
            "forbidden_roots": [_portable(path) for path in self.forbidden_roots],
            "owned_roots": [_portable(path) for path in self.owned_roots],
        }


@dataclass(frozen=True)
class ProjectContext:
    kit_root: Path
    project_root: Path
    game_root: Path
    runtime_root: Path
    marker_path: Path
    game_layout: str

    @property
    def git_pathspec(self) -> str:
        """Repository-relative pathspec that contains the configured game."""
        return self.game_layout

    def game_relative(self, repository_path: str | Path) -> str | None:
        """Map one canonical repository path into ``res://`` space."""
        rendered = str(repository_path).replace("\\", "/").strip()
        candidate = PurePosixPath(rendered)
        if (not rendered or rendered == "." or candidate.is_absolute()
                or (candidate.parts and ":" in candidate.parts[0])):
            return None
        parts = candidate.parts
        if any(part in ("", ".", "..") for part in parts):
            return None
        if self.game_layout == ".":
            return PurePosixPath(*parts).as_posix()
        prefix = self.game_layout + "/"
        if not rendered.startswith(prefix):
            return None
        relative = rendered[len(prefix):]
        return relative or None

    def path_policy(self, owned: Iterable[str | Path],
                    forbidden: Iterable[str | Path] = ()) -> PathPolicy:
        """Build a policy from paths relative to the kit root."""
        return PathPolicy.create(self.kit_root, owned, forbidden)

    def status(self, policy: PathPolicy | None = None) -> dict:
        value = {
            "game_layout": self.game_layout,
            "game_root": _portable(self.game_root),
            "kit_root": _portable(self.kit_root),
            "marker": {
                "kind": MARKER_KIND,
                "path": _portable(self.marker_path),
                "schema": SCHEMA,
            },
            "project_root": _portable(self.project_root),
            "runtime_root": _portable(self.runtime_root),
            "schema": SCHEMA,
        }
        if policy is not None:
            if policy.anchor != self.kit_root:
                raise ProjectContextError("status policy belongs to a different kit root")
            value["path_policy"] = policy.status()
        return value

    def to_json(self, policy: PathPolicy | None = None) -> str:
        return json.dumps(
            self.status(policy), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


def resolve_project_context(project: str | Path, *, game_layout: str = "src",
                            runtime_root: str | Path = DEFAULT_RUNTIME_ROOT) -> ProjectContext:
    project_root = _canonical_directory(project, "project")
    kit_root, marker_path = locate_kit_root(project_root)
    if not _is_within(project_root, kit_root):
        raise ProjectContextError(f"project root is outside kit root: {project_root}")
    if game_layout not in GAME_LAYOUTS:
        raise ProjectContextError(
            f"game layout must be one of {', '.join(GAME_LAYOUTS)}: {game_layout!r}"
        )
    game_root = _resolve_within(project_root, game_layout, "game root")
    resolved_runtime = _resolve_within(
        project_root, runtime_root, "runtime root", allow_root=False
    )
    return ProjectContext(
        kit_root=kit_root,
        project_root=project_root,
        game_root=game_root,
        runtime_root=resolved_runtime,
        marker_path=marker_path,
        game_layout=game_layout,
    )


def load_configured_context(project: str | Path) -> ProjectContext:
    """Resolve the kit root and load its one authoritative path configuration."""
    kit_root, _marker = locate_kit_root(project)
    config_path = kit_root / CONFIG_NAME
    if config_path.is_symlink() or not config_path.is_file():
        raise ProjectContextError(
            f"kit configuration must be a regular file: {config_path}"
        )
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ProjectContextError(
            f"kit configuration is not readable JSON: {config_path}"
        ) from exc
    if not isinstance(config, dict) or config.get("schema") != SCHEMA:
        raise ProjectContextError("kit.config.json must be a schema-1 object")
    game_layout = config.get("game_root")
    runtime_root = config.get("runtime_root")
    if game_layout not in GAME_LAYOUTS:
        raise ProjectContextError(
            f"kit.config.json game_root must be one of {', '.join(GAME_LAYOUTS)}"
        )
    if not isinstance(runtime_root, str) or not runtime_root.strip():
        raise ProjectContextError(
            "kit.config.json runtime_root must be a non-empty relative path"
        )
    return resolve_project_context(
        kit_root,
        game_layout=game_layout,
        runtime_root=runtime_root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve portable agent-kit project context")
    parser.add_argument("--project", required=True, help="project directory or path")
    parser.add_argument("--game-root", choices=GAME_LAYOUTS, default="src",
                        help="game root relative to the project")
    parser.add_argument("--runtime-root", default=DEFAULT_RUNTIME_ROOT,
                        help="private runtime root relative to the project")
    parser.add_argument("--owned", action="append", default=[],
                        help="owned path relative to the kit root; repeatable")
    parser.add_argument("--forbidden", action="append", default=[],
                        help="forbidden path relative to the kit root; repeatable")
    args = parser.parse_args(argv)
    try:
        context = resolve_project_context(
            args.project, game_layout=args.game_root, runtime_root=args.runtime_root
        )
        policy = None
        if args.owned or args.forbidden:
            policy = context.path_policy(args.owned, args.forbidden)
        print(context.to_json(policy))
        return 0
    except ProjectContextError as exc:
        print(json.dumps({"error": str(exc), "ok": False}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
