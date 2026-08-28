#!/usr/bin/env python3
"""Provider-neutral authored game-file classification.

The conformance gate and the plan view must agree on which ``res://`` inputs a
human or agent authored.  Use an inverse policy: exclude known generated,
private, third-party and engine-owned surfaces, then include every
other regular file regardless of extension.  This keeps custom content formats
inside approval scope instead of silently treating an unfamiliar suffix as
generated.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Callable


COMMON_SKIP_PREFIXES = (
    "addons/",
    ".checklogs/",
    ".git/",
    ".kit/",
    ".godot/",
    ".godot_doc/",
)
KIT_OWNED_GAME_FILES = frozenset(("tools/validate_resources.gd",))
ROOT_KIT_PREFIXES = (
    ".agents/",
    ".github/",
    "docs/",
    "plan/",
)
GENERATED_SUFFIXES = (".import", ".uid")
ENGINE_OWNED_ROOT = frozenset(("project.godot", "export_presets.cfg"))
PROJECT_CONTROL_ROOT = frozenset(
    (
        ".agent-kit.json",
        ".gdlintrc",
        ".gutconfig.json",
        ".kit-maintainer-fixture",
        "AGENTS.md",
        "ARCHITECTURE.md",
        "CLAUDE.md",
        "README.md",
        "SETUP.md",
        "VERIFY.md",
        "arch.rules.json",
        "dependencies.lock.json",
        "gate.rules.json",
        "import_profiles.json",
        "kit.config.json",
        "plan.html",
        "project.shape.json",
        "proposal.json",
        "retro.config.json",
        "retro.html",
    )
)


def normalize_relative(value: str) -> str | None:
    """Return one canonical forward-slash relative path, or ``None``."""
    if not isinstance(value, str):
        return None
    rendered = value.strip()
    if (
        not rendered
        or "\\" in rendered
        or rendered.startswith("/")
        or rendered.endswith("/")
        or "\x00" in rendered
    ):
        return None
    pure = PurePosixPath(rendered)
    if (
        pure.is_absolute()
        or any(part in ("", ".", "..") for part in pure.parts)
        or ":" in pure.parts[0]
        or pure.as_posix() != rendered
    ):
        return None
    return rendered


def normalize_directory(value: str) -> str | None:
    """Return one canonical non-root directory path, or ``None``."""
    if not isinstance(value, str):
        return None
    rendered = value.strip()
    if rendered == "(root)":
        return rendered
    if not rendered or rendered.endswith("/") or rendered.endswith("\\"):
        return None
    marker = normalize_relative(rendered + "/__kit_scope_marker__")
    if marker is None:
        return None
    return marker.rsplit("/", 1)[0]


def is_authored_game_file(
    value: str,
    *,
    game_layout: str,
    is_kit_file: Callable[[str], bool] | None = None,
) -> bool:
    """Whether ``value`` is an authored, game-root-relative regular file path."""
    relative = normalize_relative(value)
    if relative is None:
        return False
    lowered = relative.lower()
    if (
        relative in ENGINE_OWNED_ROOT
        or relative in PROJECT_CONTROL_ROOT
        or relative in KIT_OWNED_GAME_FILES
    ):
        return False
    if lowered.endswith(GENERATED_SUFFIXES):
        return False
    if any(relative.startswith(prefix) for prefix in COMMON_SKIP_PREFIXES):
        return False
    if game_layout == "." and (
        any(relative.startswith(prefix) for prefix in ROOT_KIT_PREFIXES)
        or (is_kit_file is not None and is_kit_file(relative))
    ):
        return False
    return True
