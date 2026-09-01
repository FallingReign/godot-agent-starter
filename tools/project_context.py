#!/usr/bin/env python3
"""Portable project-root discovery and path-policy primitives.

The kit is located by a versioned marker rather than by game content or a
particular repository layout.  This module is deliberately read-only: callers
decide when to create the marker and runtime directory.
"""
from __future__ import annotations

import argparse
import json
import stat
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence

try:
    import managed_launcher
except ImportError:  # package import in tests and installed cores
    from tools import managed_launcher  # type: ignore[no-redef]

SCHEMA = managed_launcher.MARKER_SCHEMA
MARKER_NAME = managed_launcher.MARKER_NAME
MARKER_KIND = managed_launcher.MARKER_KIND
CONFIG_NAME = "kit.config.json"
# Kept as common presets for older callers.  It is no longer a whitelist.
GAME_LAYOUTS = (".", "src")
DEFAULT_RUNTIME_ROOT = ".kit/runtime"
PRIVATE_RUNTIME_CONTAINER = ".kit"
RESERVED_GAME_ROOTS = frozenset({
    ".agent-kit",
    ".agents",
    ".checklogs",
    ".claude",
    ".git",
    ".github",
    ".godot_doc",
    ".kit",
    "docs",
    "plan",
    "tools",
})


class ProjectContextError(ValueError):
    """A project path or marker cannot establish a safe context."""


ActiveInstallation = managed_launcher.Installation


def marker_document() -> dict:
    """Return the complete stable marker contract for a new distribution."""
    return {"kind": MARKER_KIND, "schema": SCHEMA}


def _portable(path: Path) -> str:
    return path.as_posix()


def _is_within(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _is_reparse(info: object) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return bool(int(getattr(info, "st_file_attributes", 0)) & marker)


def _canonical_directory(value: str | Path, label: str) -> Path:
    raw = Path(value).expanduser()
    try:
        info = raw.lstat()
        path = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectContextError(f"{label} is not a readable path: {value!s}") from exc
    if (stat.S_ISLNK(info.st_mode) or _is_reparse(info)
            or not stat.S_ISDIR(info.st_mode)):
        raise ProjectContextError(f"{label} must be an unredirected directory: {path}")
    return path


def _matching_names(parent: Path, expected: str, label: str) -> list[str]:
    try:
        matches = sorted(
            item.name for item in parent.iterdir()
            if item.name.casefold() == expected.casefold()
        )
    except OSError as exc:
        raise ProjectContextError(f"cannot inspect {label}: {parent}: {exc}") from exc
    if len(matches) > 1:
        raise ProjectContextError(
            f"{label} has a case collision for {expected!r}: {', '.join(matches)}"
        )
    if matches and matches[0] != expected:
        raise ProjectContextError(
            f"{label} uses unsafe casing for {expected!r}: {matches[0]!r}"
        )
    return matches


def _portable_relative(value: str | Path, label: str, *, allow_root: bool) -> str:
    rendered = str(value).strip().replace("\\", "/")
    if not rendered or "\x00" in rendered:
        raise ProjectContextError(f"{label} must be a non-empty repository-relative path")
    path = PurePosixPath(rendered)
    if (path.is_absolute() or (path.parts and ":" in path.parts[0])
            or ".." in path.parts):
        raise ProjectContextError(f"{label} must not escape the project")
    if rendered == ".":
        if allow_root:
            return "."
        raise ProjectContextError(f"{label} must be below the project root")
    if (not path.parts or any(part in ("", ".", "..") for part in path.parts)
            or path.as_posix() != rendered):
        raise ProjectContextError(f"{label} must use one canonical repository-relative path")
    return path.as_posix()


def _inspect_existing_path(
    root: Path, relative: str, label: str, *, require_final: bool
) -> Path:
    """Resolve a child while rejecting redirection and case ambiguity."""
    cursor = root
    parts = () if relative == "." else PurePosixPath(relative).parts
    for index, component in enumerate(parts):
        matches = _matching_names(cursor, component, label)
        if not matches:
            if require_final:
                raise ProjectContextError(f"{label} does not exist: {cursor / component}")
            return cursor.joinpath(*parts[index:])
        cursor = cursor / component
        try:
            info = cursor.lstat()
        except OSError as exc:
            raise ProjectContextError(f"{label} is unreadable: {cursor}: {exc}") from exc
        if (stat.S_ISLNK(info.st_mode) or _is_reparse(info)
                or not stat.S_ISDIR(info.st_mode)):
            raise ProjectContextError(
                f"{label} must use unredirected directories: {cursor}"
            )
    if require_final and relative == ".":
        _canonical_directory(root, label)
    return cursor


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


def runtime_root_relative(value: str | Path) -> str:
    """Validate and return the portable project-relative private runtime path.

    Runtime data can contain session testimony, sealed prompts and detached Git
    metadata.  Merely keeping it somewhere below the repository is not a
    privacy boundary: it can overlap game or durable documentation paths and a
    separately maintained dispatch deny-list can miss it.  The one portable
    private container is therefore ``.kit/`` and the runtime must be a proper
    descendant of it.
    """
    relative = _portable_relative(value, "runtime root", allow_root=False)
    portable = PurePosixPath(relative)
    if (portable.parts[0] != PRIVATE_RUNTIME_CONTAINER or len(portable.parts) < 2):
        raise ProjectContextError(
            "runtime root must be a descendant of the project-local .kit directory"
        )
    return portable.as_posix()


def locate_kit_root(project: str | Path) -> tuple[Path, Path]:
    """Return ``(project_root, marker_path)`` for compatibility callers."""
    try:
        return managed_launcher.locate_project_root(project)
    except managed_launcher.LauncherError as exc:
        raise ProjectContextError(str(exc)) from exc


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
    core_root: Path | None = None
    install_mode: str = "flat"
    release_sha256: str | None = None
    current_path: Path | None = None

    def __post_init__(self) -> None:
        if self.core_root is None:
            object.__setattr__(self, "core_root", self.kit_root)

    @property
    def code_root(self) -> Path:
        """Compatibility alias for the selected core root."""
        assert self.core_root is not None
        return self.core_root

    @property
    def mode(self) -> str:
        """Compatibility alias for the installation mode."""
        return self.install_mode

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
            "core_root": _portable(self.code_root),
            "game_layout": self.game_layout,
            "game_root": _portable(self.game_root),
            "install_mode": self.install_mode,
            "kit_root": _portable(self.kit_root),
            "marker": {
                "kind": MARKER_KIND,
                "path": _portable(self.marker_path),
                "schema": SCHEMA,
            },
            "project_root": _portable(self.project_root),
            "release_sha256": self.release_sha256,
            "runtime_root": _portable(self.runtime_root),
            "schema": SCHEMA,
        }
        if self.current_path is not None:
            value["current"] = _portable(self.current_path)
        if policy is not None:
            if policy.anchor != self.kit_root:
                raise ProjectContextError("status policy belongs to a different kit root")
            value["path_policy"] = policy.status()
        return value

    def to_json(self, policy: PathPolicy | None = None) -> str:
        return json.dumps(
            self.status(policy), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )


def _context_for_installation(
    installation: managed_launcher.Installation,
    *,
    game_layout: str,
    runtime_root: str | Path,
) -> ProjectContext:
    project_root = installation.project_root
    game_relative = _portable_relative(game_layout, "game root", allow_root=True)
    if game_relative != ".":
        first = PurePosixPath(game_relative).parts[0]
        if first.casefold() in {item.casefold() for item in RESERVED_GAME_ROOTS}:
            raise ProjectContextError(
                f"game root uses reserved kit path {first!r}: {game_relative}"
            )
    game_root = _inspect_existing_path(
        project_root, game_relative, "game root", require_final=True
    )
    runtime_relative = runtime_root_relative(runtime_root)
    resolved_runtime = _inspect_existing_path(
        project_root, runtime_relative, "runtime root", require_final=False
    )
    return ProjectContext(
        kit_root=project_root,
        project_root=project_root,
        game_root=game_root,
        runtime_root=resolved_runtime,
        marker_path=installation.marker_path,
        game_layout=game_relative,
        core_root=installation.core_root,
        install_mode=installation.mode,
        release_sha256=installation.release_sha256,
        current_path=installation.current_path,
    )


def resolve_project_context(project: str | Path, *, game_layout: str = "src",
                            runtime_root: str | Path = DEFAULT_RUNTIME_ROOT) -> ProjectContext:
    try:
        installation = managed_launcher.resolve_installation(project)
    except managed_launcher.LauncherError as exc:
        raise ProjectContextError(str(exc)) from exc
    return _context_for_installation(
        installation, game_layout=game_layout, runtime_root=runtime_root
    )


def _load_installation_config(
    installation: managed_launcher.Installation,
) -> ProjectContext:
    project_root = installation.project_root
    matches = _matching_names(project_root, CONFIG_NAME, "kit configuration")
    config_path = project_root / CONFIG_NAME
    if not matches:
        raise ProjectContextError(f"kit configuration is missing: {config_path}")
    try:
        info = config_path.lstat()
    except OSError as exc:
        raise ProjectContextError(f"kit configuration is unreadable: {config_path}") from exc
    if (stat.S_ISLNK(info.st_mode) or _is_reparse(info)
            or not stat.S_ISREG(info.st_mode)
            or int(getattr(info, "st_nlink", 1)) != 1):
        raise ProjectContextError(
            f"kit configuration must be an unredirected regular file: {config_path}"
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
    if not isinstance(game_layout, str) or not game_layout.strip():
        raise ProjectContextError(
            "kit.config.json game_root must be a non-empty relative path"
        )
    if not isinstance(runtime_root, str) or not runtime_root.strip():
        raise ProjectContextError(
            "kit.config.json runtime_root must be a non-empty relative path"
        )
    return _context_for_installation(
        installation,
        game_layout=game_layout,
        runtime_root=runtime_root,
    )


def load_configured_context(project: str | Path) -> ProjectContext:
    """Resolve a requested project and load its authoritative path configuration."""
    try:
        installation = managed_launcher.resolve_installation(project)
    except managed_launcher.LauncherError as exc:
        raise ProjectContextError(str(exc)) from exc
    return _load_installation_config(installation)


def load_active_context(
    core_root: str | Path,
    environment: Mapping[str, str] | None = None,
) -> ProjectContext:
    """Load context for code running from a flat or launcher-bound core.

    Downstream modules pass the core root derived from their own ``__file__``.
    Managed mode requires both launcher environment bindings and revalidates
    those paths through ``current.json`` before trusting either one.
    """
    installation = resolve_active_installation(core_root, environment)
    return _load_installation_config(installation)


def resolve_active_installation(
    core_root: str | Path,
    environment: Mapping[str, str] | None = None,
) -> ActiveInstallation:
    """Resolve only project/core roots for setup and other pre-config commands."""
    try:
        return managed_launcher.resolve_bound_installation(core_root, environment)
    except managed_launcher.LauncherError as exc:
        raise ProjectContextError(str(exc)) from exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve portable agent-kit project context")
    parser.add_argument("--project", required=True, help="project directory or path")
    parser.add_argument("--game-root", default="src",
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
