#!/usr/bin/env python3
"""Build, inspect, and verify deterministic sanitized kit archives.

The source set is intentionally closed.  Adding a tracked file does not add it
to a release; a maintainer must add its exact path or narrow path pattern here.
Project/game state and runtime retrospective state are never candidates.

    kit release build ../godot-agent-kit.zip
    kit release inspect ../godot-agent-kit.zip
    kit release verify ../godot-agent-kit.zip
"""
from __future__ import annotations

import argparse
import binascii
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = "RELEASE-MANIFEST.json"
SCHEMA = 2
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
MAX_MEMBERS = 512
MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024

# ``ARCHITECTURE.md`` in a working project is generated from that game's code.
# It remains a required kit member so a newly installed project has the normal
# architecture workflow, but release bytes must not disclose the source game's
# module names or edges. Verification binds this complete template rather than
# trusting a manifest to authorize arbitrary project-generated graph content.
CANONICAL_ARCHITECTURE = b"""# Architecture

This distributable starts with no game modules or dependency edges. The file is
regenerated from the target project's code; it is not a snapshot of the project
that built the kit.

Regenerate it after adding game code:

```text
kit architecture update
```

## Module graph

<!-- BEGIN GENERATED GRAPH -->

```mermaid
graph TD
```

<!-- END GENERATED GRAPH -->

## Project boundaries

Record allowed module dependencies in `arch.rules.json`. The generated graph
then shows what the target project actually contains, while `kit verify --stage
arch` checks those observed edges against the declared policy.
"""

# ``arch.rules.json`` is mutable project architecture just as much as the
# generated graph above.  Shipping the source copy would disclose a project's
# module names, descriptions and dependency policy, even when no matching game
# file happened to make that residue visible to the text scanner.  A release
# therefore carries this fixed, genre-neutral starting policy instead.
CANONICAL_ARCH_RULES = b"""{
  "_comment": [
    "Default genre-neutral module boundaries for a newly installed kit.",
    "Paths are relative to the configured game root (Godot res://).",
    "Revise these defaults deliberately for the destination project, then run kit architecture update."
  ],
  "module_depth": 2,
  "modules": {
    "scenes": {
      "description": "scene files and presentation",
      "may_depend_on": [
        "scripts",
        "scripts/logic",
        "scripts/data"
      ]
    },
    "scripts": {
      "description": "node layer and presentation wiring",
      "may_depend_on": [
        "scripts/logic",
        "scripts/data"
      ]
    },
    "scripts/data": {
      "description": "typed configuration and external-data boundary",
      "may_depend_on": []
    },
    "scripts/logic": {
      "description": "pure rules with no scene, node or autoload dependency",
      "may_depend_on": [
        "scripts/data"
      ]
    },
    "tests": {
      "description": "project test harnesses",
      "may_depend_on": [
        "scripts",
        "scripts/logic",
        "scripts/data"
      ]
    },
    "tests/unit": {
      "description": "logic and data tests",
      "may_depend_on": [
        "scripts/logic",
        "scripts/data"
      ]
    },
    "tools": {
      "description": "headless project validators not shipped in the game",
      "may_depend_on": []
    }
  },
  "forbid_autoload_use_in": [
    "scripts/logic/",
    "scripts/data/"
  ],
  "type_boundary": {
    "_note": "Rename or extend these paths when the destination project deliberately adopts another layout.",
    "boundary": [
      "scripts/data/"
    ],
    "interior": [
      "scripts/logic/"
    ]
  }
}
"""

LEGAL_FILES = frozenset({
    "LICENSE", "LICENSE.md", "LICENSE.txt", "COPYING", "COPYING.md",
})

# Closed review list. In particular, this excludes every non-allowlisted game
# file regardless of root/src layout, project.shape.json, proposal.json,
# generated plan/retro HTML, and all mutable retrospective data.
ROOT_FILES = frozenset({
    ".agent-kit.json",
    ".claude/settings.json",
    ".gate.sha256",
    ".gitattributes",
    ".gdlintrc",
    ".gitignore",
    ".github/copilot-instructions.md",
    "AGENTS.md",
    "ARCHITECTURE.md",
    "CLAUDE.md",
    "README.md",
    "SETUP.md",
    "VERIFY.md",
    "VERSION",
    "arch.py",
    "arch.rules.json",
    "bootstrap.py",
    "check.py",
    "gate.rules.json",
    "import_profiles.json",
    "dependencies.lock.json",
    "kit.config.json",
    "kit",
    "kit.cmd",
    "kit.py",
    "sanitise.py",
}) | LEGAL_FILES

DOC_FILES = frozenset({
    "docs/DECISIONS.md",
    "docs/DESIGN.md",
    "docs/GATE.md",
    "docs/GDSCRIPT.md",
    "docs/RULES.md",
    "docs/SCENES.md",
    "docs/WORKFLOW.md",
    "docs/retro/README.md",
    "docs/retro/notes/README.md",
})

TOOL_FILES = frozenset({
    "tools/authored_scope.py",
    "tools/board.py",
    "tools/board_client.py",
    "tools/cockpit.py",
    "tools/design.py",
    "tools/engine_discovery.py",
    "tools/friction.py",
    "tools/gddoc.py",
    "tools/gd_signature.py",
    "tools/gdls.py",
    "tools/gen_gdscript_doc.py",
    "tools/md.py",
    "tools/native_engine.py",
    "tools/plan_html.py",
    "tools/process_supervisor.py",
    "tools/project_context.py",
    "tools/proposal_authority.py",
    "tools/providers.py",
    "tools/release.py",
    "tools/retro.py",
    "tools/retro_due.py",
    "tools/retro_html.py",
    "tools/retro_ledger.py",
    "tools/retro_queue.py",
    "tools/retro_rank.py",
    "tools/retro_sdk.py",
    "tools/run_result.py",
    "tools/runtime_paths.py",
    "tools/schema.py",
    "tools/session_digest.py",
    "tools/session_evidence.py",
    "tools/strict_verify.py",
})

VALIDATION_FILES = frozenset({
    ".github/workflows/ci.yml",
    "tools/tests/browser_check.py",
    "tools/tests/dom_harness.js",
    "tools/tests/mock_board.py",
    "tools/tests/page_parts.py",
    "tools/tests/test_board_api.py",
    "tools/tests/test_authored_scope.py",
    "tools/tests/test_bootstrap.py",
    "tools/tests/test_cockpit.py",
    "tools/tests/test_design_conformance.py",
    "tools/tests/test_design_governance.py",
    "tools/tests/test_engine_discovery.py",
    "tools/tests/test_engine_boundary.py",
    "tools/tests/test_frontend.py",
    "tools/tests/test_friction.py",
    "tools/tests/test_gate_receipt.py",
    "tools/tests/test_gd_signature.py",
    "tools/tests/test_integration.py",
    "tools/tests/test_kit_cli.py",
    "tools/tests/test_layout_consumers.py",
    "tools/tests/test_native_engine.py",
    "tools/tests/test_native_process_containment.py",
    "tools/tests/test_project_context.py",
    "tools/tests/test_providers.py",
    "tools/tests/test_release.py",
    "tools/tests/test_retro_due.py",
    "tools/tests/test_retro_ledgers.py",
    "tools/tests/test_retro_queue.py",
    "tools/tests/test_retro_workflow.py",
    "tools/tests/test_run_result.py",
    "tools/tests/test_runtime_paths.py",
    "tools/tests/test_session_evidence.py",
    "tools/tests/test_signature_consumers.py",
    "tools/tests/test_strict_verify.py",
})

FIXED_FILES = ROOT_FILES | DOC_FILES | TOOL_FILES | VALIDATION_FILES
REQUIRED_AGENT_FILES = frozenset({
    ".github/agents/game-builder.agent.md",
    ".github/agents/kit-builder.agent.md",
    ".github/agents/retrospective.agent.md",
})
REQUIRED_SKILL_FILES = frozenset({
    ".agents/skills/godot-api-lookup/SKILL.md",
    ".agents/skills/godot-content-pipeline/SKILL.md",
    ".agents/skills/godot-data-layout/SKILL.md",
    ".agents/skills/godot-design-discovery/SKILL.md",
    ".agents/skills/godot-design-retrieval/SKILL.md",
    ".agents/skills/godot-design-sections/SKILL.md",
    ".agents/skills/godot-headless-verification/SKILL.md",
    ".agents/skills/godot-human-involvement/SKILL.md",
    ".agents/skills/godot-increment-size/SKILL.md",
    ".agents/skills/godot-lifecycle-and-signals/SKILL.md",
    ".agents/skills/godot-multiplayer-authority/SKILL.md",
    ".agents/skills/godot-node-or-resource/SKILL.md",
    ".agents/skills/godot-performance-evidence/SKILL.md",
    ".agents/skills/godot-project-decisions/SKILL.md",
    ".agents/skills/godot-resolving-ambiguity/SKILL.md",
    ".agents/skills/godot-retrospective/SKILL.md",
    ".agents/skills/godot-scene-files/SKILL.md",
    ".agents/skills/godot-tooling-friction/SKILL.md",
    ".agents/skills/godot-typed-data-boundary/SKILL.md",
    ".agents/skills/shell-compat/SKILL.md",
})
# Every reviewed fixed surface is required.  Otherwise deleting a validator,
# workflow, runbook, or control-plane module could silently produce a smaller
# but apparently valid distribution.  Legal metadata is handled separately so
# a maintainer may choose any one of the approved top-level filenames.
REQUIRED_KIT_FILES = (
    (FIXED_FILES - LEGAL_FILES) | REQUIRED_AGENT_FILES | REQUIRED_SKILL_FILES
)
SAFE_ARCHIVE_PATH_RE = re.compile(r"[A-Za-z0-9._/-]+\Z")
VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
ARCHIVE_RECEIPT_TRUST = "portable-policy"
AUTHORITY_IDENTITY_MODEL = "portable-policy-audit"
NO_PROJECT_RECEIPT = "not-applicable-no-project-state"
PROJECT_RECEIPT_TRUST = frozenset({
    "local-audit-matched",
    "portable-policy",
    "no-exact-authority-event",
    NO_PROJECT_RECEIPT,
})
PROJECT_TEXT_SUFFIXES = frozenset({
    ".cfg", ".gd", ".gdshader", ".json", ".md", ".tres", ".tscn",
})
PROJECT_SCAN_SKIP_DIRS = frozenset({
    ".agents", ".checklogs", ".git", ".github", ".godot", ".godot_doc",
    ".kit", ".pytest_cache", "__pycache__", "addons", "build", "docs",
    "export", "tools",
})
GENERIC_PROJECT_PATHS = frozenset({
    "scenes/main.tscn",
    "scripts/main.gd",
    "tests/expected_errors.json",
    "tests/smoke_test.gd",
    "tests/smoke_test.tscn",
    "tools/validate_resources.gd",
})
PROJECT_NAME_RE = re.compile(
    r'^\s*config/name\s*=\s*("(?:[^"\\]|\\.)*")\s*$', re.MULTILINE
)
MAX_RESIDUE_MARKERS = 4096


class ReleaseError(ValueError):
    """A release cannot be built or trusted."""


@dataclass(frozen=True)
class ReleaseFile:
    path: str
    content: bytes
    mode: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True)
class ArchiveMember:
    path: str
    content: bytes
    mode: int | None


def is_allowlisted(path: str) -> bool:
    """Return whether a normalized repository-relative path may ship."""
    return path in REQUIRED_KIT_FILES or path in LEGAL_FILES


def _is_reparse_point(path: Path) -> bool:
    """Return whether Windows may redirect this path through a reparse point."""
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    return bool(flag and attributes & flag)


def _safe_member_path(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise ReleaseError("archive contains an empty member path")
    if len(name) > 240 or "\x00" in name or "\\" in name:
        raise ReleaseError(f"unsafe archive member path: {name!r}")
    if name.startswith("/") or re.match(r"[A-Za-z]:", name):
        raise ReleaseError(f"absolute archive member path: {name!r}")
    if not SAFE_ARCHIVE_PATH_RE.fullmatch(name):
        raise ReleaseError(f"unsupported archive member path: {name!r}")
    raw_parts = name.split("/")
    if any(part in ("", ".", "..") for part in raw_parts):
        raise ReleaseError(f"traversing archive member path: {name!r}")
    normalized = PurePosixPath(name).as_posix()
    if normalized != name:
        raise ReleaseError(f"non-canonical archive member path: {name!r}")
    return normalized


def _source_path(root: Path, relative: str) -> Path:
    _safe_member_path(relative)
    root_resolved = root.resolve()
    cursor = root_resolved
    for component in relative.split("/"):
        cursor = cursor / component
        if cursor.is_symlink():
            raise ReleaseError(f"allowlisted source is a symlink: {relative}")
        if _is_reparse_point(cursor):
            raise ReleaseError(f"allowlisted source is a reparse point: {relative}")
    try:
        cursor.resolve().relative_to(root_resolved)
    except ValueError as exc:
        raise ReleaseError(f"allowlisted source escapes repository: {relative}") from exc
    if not cursor.is_file():
        raise ReleaseError(f"allowlisted source is not a regular file: {relative}")
    return cursor


def _read_stable(path: Path, relative: str) -> bytes:
    try:
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            content = source.read(MAX_FILE_BYTES + 1)
            after = os.fstat(source.fileno())
    except OSError as exc:
        raise ReleaseError(f"cannot read allowlisted source {relative}: {exc}") from exc
    if len(content) > MAX_FILE_BYTES:
        raise ReleaseError(f"allowlisted source exceeds {MAX_FILE_BYTES} bytes: {relative}")
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise ReleaseError(f"allowlisted source changed while reading: {relative}")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseError(f"allowlisted source is not UTF-8 text: {relative}") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _candidate_paths(root: Path) -> list[str]:
    candidates = {
        path for path in FIXED_FILES
        if (root / PurePosixPath(path)).exists()
        or (root / PurePosixPath(path)).is_symlink()
        or _is_reparse_point(root / PurePosixPath(path))
    }
    for candidate in root.glob(".agents/skills/*/SKILL.md"):
        candidates.add(candidate.relative_to(root).as_posix())
    for candidate in root.glob(".github/agents/*.agent.md"):
        candidates.add(candidate.relative_to(root).as_posix())
    return sorted(candidates)


def _read_version(root: Path) -> str:
    path = root / "VERSION"
    if not path.exists() and not path.is_symlink() and not _is_reparse_point(path):
        raise ReleaseError("required VERSION metadata is absent")
    content = _read_stable(_source_path(root, "VERSION"), "VERSION")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - _read_stable already proves it
        raise ReleaseError("VERSION is not UTF-8") from exc
    lines = text.splitlines()
    if len(lines) != 1 or lines[0] != lines[0].strip() or not VERSION_RE.fullmatch(lines[0]):
        raise ReleaseError("VERSION must be one line of 1-64 safe version characters")
    return lines[0]


def _legal_paths(root: Path) -> list[str]:
    present = sorted(
        path for path in LEGAL_FILES
        if ((root / path).exists() or (root / path).is_symlink()
            or _is_reparse_point(root / path)))
    if not present:
        raise ReleaseError(
            "required legal metadata is absent; add an approved top-level LICENSE or COPYING")
    for relative in present:
        content = _read_stable(_source_path(root, relative), relative)
        if not content.strip():
            raise ReleaseError(f"legal metadata is empty: {relative}")
    return present


def _private_runtime_root(root: Path) -> Path:
    """Resolve the one source-local directory allowed to hold release scratch."""
    try:
        try:
            import runtime_paths
        except ImportError:
            from tools import runtime_paths  # type: ignore[no-redef]
        return runtime_paths.resolve(root).runtime
    except (OSError, ValueError, RuntimeError) as exc:
        raise ReleaseError(f"cannot resolve private runtime_root: {exc}") from exc


def _configured_game_root(root: Path) -> Path | None:
    """Resolve an existing configured game root without following a link."""
    try:
        config = json.loads((root / "kit.config.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"cannot resolve game_root for release isolation: {exc}") from exc
    value = config.get("game_root") if isinstance(config, dict) else None
    relative = Path(value) if isinstance(value, str) else Path(".")
    if (not isinstance(value, str) or not value.strip() or relative.is_absolute()
            or ".." in relative.parts):
        raise ReleaseError("kit.config.json game_root is unsafe")
    root_resolved = root.resolve()
    game_root = (root_resolved / relative).resolve(strict=False)
    try:
        game_root.relative_to(root_resolved)
    except ValueError as exc:
        raise ReleaseError("kit.config.json game_root escapes the release root") from exc
    if not game_root.exists():
        return None
    if game_root.is_symlink() or _is_reparse_point(game_root) or not game_root.is_dir():
        raise ReleaseError("configured game_root is not a regular directory")
    return game_root


def _walk_project_files(base: Path, repository: Path) -> list[Path]:
    """Return project-owned files without traversing links, caches, or kit dirs."""
    found: list[Path] = []
    for current, directories, filenames in os.walk(base, topdown=True, followlinks=False):
        current_path = Path(current)
        safe_directories: list[str] = []
        for name in directories:
            candidate = current_path / name
            if (name in PROJECT_SCAN_SKIP_DIRS or name.startswith(".")
                    or candidate.is_symlink() or _is_reparse_point(candidate)):
                continue
            safe_directories.append(name)
        directories[:] = safe_directories
        for name in filenames:
            candidate = current_path / name
            if candidate.is_symlink() or _is_reparse_point(candidate) or not candidate.is_file():
                continue
            try:
                relative = candidate.relative_to(repository).as_posix()
            except ValueError:
                continue
            if is_allowlisted(relative):
                continue
            found.append(candidate)
    return sorted(found)


def _residue_text(path: Path) -> str:
    """Read bounded UTF-8 project text for marker derivation."""
    try:
        before = path.stat()
        if before.st_size > MAX_FILE_BYTES:
            return ""
        text = path.read_text(encoding="utf-8")
        after = path.stat()
    except (OSError, UnicodeError):
        return ""
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        raise ReleaseError(f"project source changed during release isolation scan: {path.name}")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _project_residue_markers(root: Path) -> list[tuple[str, str]]:
    """Derive semantic markers from state that the release deliberately excludes."""
    root = root.resolve()
    game_root = _configured_game_root(root)
    markers: dict[str, str] = {}

    def add(label: str, value: str, minimum: int = 12) -> None:
        normalized = " ".join(value.strip().split())
        if minimum <= len(normalized) <= 500:
            markers.setdefault(normalized.casefold().replace("\\", "/"), label)

    if game_root is not None:
        project_file = game_root / "project.godot"
        if project_file.is_file() and not project_file.is_symlink():
            project_text = _residue_text(project_file)
            match = PROJECT_NAME_RE.search(project_text)
            if match is not None:
                try:
                    name = json.loads(match.group(1))
                except json.JSONDecodeError:
                    name = ""
                if isinstance(name, str):
                    add("configured project name", name, 3)

        for path in _walk_project_files(game_root, root):
            relative = path.relative_to(game_root).as_posix()
            if "/" in relative and relative not in GENERIC_PROJECT_PATHS:
                add("game file path", relative)
            if path.suffix.lower() not in PROJECT_TEXT_SUFFIXES:
                continue
            for line in _residue_text(path).splitlines():
                stripped = line.strip()
                if (len(stripped) >= 32 and not stripped.startswith(("#", ";", "["))):
                    add("game source line", stripped)

    design_root = root / "docs" / "design"
    if design_root.is_dir() and not design_root.is_symlink() and not _is_reparse_point(design_root):
        for path in _walk_project_files(design_root, root):
            relative = path.relative_to(design_root).as_posix()
            if "/" in relative:
                add("design document path", relative)
            if (path.name in {"INDEX.md", "README.md"}
                    or path.suffix.lower() not in PROJECT_TEXT_SUFFIXES):
                continue
            for line in _residue_text(path).splitlines():
                stripped = line.strip()
                if (len(stripped) >= 80
                        and not stripped.startswith(("#", "|", "```", "<!--"))):
                    add("design statement", stripped)

    shape_path = root / "project.shape.json"
    if shape_path.is_file() and not shape_path.is_symlink():
        try:
            shape = json.loads(_residue_text(shape_path))
        except json.JSONDecodeError:
            shape = {}
        if isinstance(shape, dict):
            for key in ("name", "pitch"):
                value = shape.get(key)
                if isinstance(value, str):
                    add(f"project {key}", value, 3 if key == "name" else 12)

    return [(label, value) for value, label in list(markers.items())[:MAX_RESIDUE_MARKERS]]


def _assert_no_project_residue(root: Path, files: list[ReleaseFile]) -> None:
    """Refuse a release whose reviewed kit surface repeats excluded project state."""
    markers = _project_residue_markers(root)
    if not markers:
        return
    for release_file in files:
        text = release_file.content.decode("utf-8").casefold().replace("\\", "/")
        compact = " ".join(text.split())
        for label, marker in markers:
            if marker in text or marker in compact:
                raise ReleaseError(
                    f"release file contains excluded project-derived content: "
                    f"{release_file.path} ({label})"
                )


def collect_files(root: Path) -> tuple[str, list[str], list[ReleaseFile]]:
    """Collect normalized release files from the closed allowlist."""
    if root.is_symlink() or _is_reparse_point(root) or not root.is_dir():
        raise ReleaseError(f"release root is not a regular directory: {root}")
    version = _read_version(root)
    legal_paths = _legal_paths(root)
    candidates = _candidate_paths(root)
    missing = sorted(REQUIRED_KIT_FILES - set(candidates))
    if missing:
        raise ReleaseError("required kit file(s) absent: " + ", ".join(missing))

    files: list[ReleaseFile] = []
    total = 0
    for relative in candidates:
        if not is_allowlisted(relative):
            raise ReleaseError(f"internal allowlist error for path: {relative}")
        content = _read_stable(_source_path(root, relative), relative)
        if relative == "ARCHITECTURE.md":
            content = CANONICAL_ARCHITECTURE
        elif relative == "arch.rules.json":
            content = CANONICAL_ARCH_RULES
        total += len(content)
        if total > MAX_TOTAL_BYTES:
            raise ReleaseError(f"release content exceeds {MAX_TOTAL_BYTES} bytes")
        mode = 0o755 if relative.endswith(".py") or relative == "kit" else 0o644
        files.append(ReleaseFile(relative, content, mode))
    _assert_no_project_residue(root, files)
    return version, legal_paths, files


def _git_state(root: Path) -> tuple[dict, str]:
    def run(*arguments: str) -> str:
        try:
            completed = subprocess.run(
                ["git", "-C", str(root), *arguments],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReleaseError(f"cannot read source Git metadata: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise ReleaseError(f"cannot read source Git metadata: {detail}")
        return completed.stdout

    commit = run("rev-parse", "--verify", "HEAD").strip().lower()
    if not COMMIT_RE.fullmatch(commit):
        raise ReleaseError("source Git commit is missing or malformed")
    porcelain = run("status", "--porcelain=v1", "--untracked-files=all")
    source = {"commit": commit, "dirty": bool(porcelain.strip())}
    fingerprint = hashlib.sha256(porcelain.encode("utf-8")).hexdigest()
    return source, fingerprint


def _source_authority_evidence(root: Path) -> dict[str, str]:
    """Describe the authority evidence available before project state is excluded.

    A release archive deliberately carries no proposal, design, decision ledger or
    private receipt.  Its own trust label therefore describes only the portable
    policy audit.  When both project authority inputs exist in the source tree, we
    additionally project their exact receipt state before exclusion and refuse a
    present local mismatch.  No value here claims to authenticate a person.
    """
    proposal_path = root / "proposal.json"
    shape_path = root / "project.shape.json"
    proposal_present = (
        proposal_path.exists()
        or proposal_path.is_symlink()
        or _is_reparse_point(proposal_path)
    )
    shape_present = (
        shape_path.exists()
        or shape_path.is_symlink()
        or _is_reparse_point(shape_path)
    )
    if not proposal_present and not shape_present:
        project_receipt_trust = NO_PROJECT_RECEIPT
    else:
        if proposal_present != shape_present:
            raise ReleaseError(
                "cannot evaluate pre-exclusion authority receipt: proposal.json and "
                "project.shape.json must either both exist or both be absent"
            )
        try:
            proposal = json.loads(
                _read_stable(
                    _source_path(root, "proposal.json"), "proposal.json"
                ).decode("utf-8")
            )
            shape = json.loads(
                _read_stable(
                    _source_path(root, "project.shape.json"), "project.shape.json"
                ).decode("utf-8")
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ReleaseError(
                f"cannot evaluate pre-exclusion authority receipt: {exc}"
            ) from exc
        if not isinstance(proposal, dict) or not isinstance(shape, dict):
            raise ReleaseError(
                "cannot evaluate pre-exclusion authority receipt: proposal and shape "
                "roots must be JSON objects"
            )
        try:
            try:
                import proposal_authority
            except ImportError:
                from tools import proposal_authority  # type: ignore[no-redef]
            state = proposal_authority.exact_approval_state(root, proposal, shape)
        except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
            raise ReleaseError(
                f"cannot evaluate pre-exclusion authority receipt: {exc}"
            ) from exc
        project_receipt_trust = str(state.get("receipt_trust") or "")
        if project_receipt_trust == "invalid":
            reasons = state.get("receipt_reasons")
            details = "; ".join(
                str(reason) for reason in reasons
                if isinstance(reason, str) and reason.strip()
            ) if isinstance(reasons, list) else ""
            suffix = f": {details}" if details else ""
            raise ReleaseError(
                "pre-exclusion authority receipt is invalid" + suffix
            )
        if project_receipt_trust not in PROJECT_RECEIPT_TRUST:
            raise ReleaseError(
                "pre-exclusion authority receipt returned an unsupported trust state"
            )
    return {
        "receipt_trust": ARCHIVE_RECEIPT_TRUST,
        "identity_model": AUTHORITY_IDENTITY_MODEL,
        "project_receipt_trust": project_receipt_trust,
    }


def _manifest(version: str, legal_paths: list[str], files: list[ReleaseFile],
              source: dict, authority_evidence: dict[str, str]) -> dict:
    return {
        "schema": SCHEMA,
        "version": version,
        "source": source,
        "authority_evidence": authority_evidence,
        "license_files": legal_paths,
        "normalization": {
            "line_endings": "lf",
            "regular_mode": "0644",
            "executable_mode": "0755",
            "timestamps": "fixed",
        },
        "files": [
            {
                "path": release_file.path,
                "bytes": len(release_file.content),
                "sha256": release_file.sha256,
                "mode": format(release_file.mode, "04o"),
            }
            for release_file in files
        ],
    }


def _canonical_json(value: dict) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8")


def _archive_kind(path: Path) -> str:
    lower = path.name.lower()
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith(".tar.gz") or lower.endswith(".tgz"):
        return "tar.gz"
    if lower.endswith(".tar"):
        return "tar"
    raise ReleaseError("output must end in .zip, .tar, .tar.gz, or .tgz")


def _write_zip(path: Path, members: dict[str, tuple[bytes, int]]) -> None:
    with path.open("wb") as raw:
        # Stored members are intentionally uncompressed. DEFLATE output is not
        # canonical across compressor/library versions, so fixed metadata alone
        # cannot make a production archive cross-host reproducible.
        with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.comment = b""
            for name in sorted(members):
                content, mode = members[name]
                info = zipfile.ZipInfo(name, FIXED_ZIP_TIME)
                info.compress_type = zipfile.ZIP_STORED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | mode) << 16
                info.flag_bits |= 0x800
                archive.writestr(info, content, compress_type=zipfile.ZIP_STORED)
        raw.flush()
        os.fsync(raw.fileno())


def _tar_info(name: str, content: bytes, mode: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = mode
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    info.type = tarfile.REGTYPE
    return info


def _stored_gzip(content: bytes) -> bytes:
    """Return one canonical RFC 1952 stream using only stored DEFLATE blocks."""
    encoded = bytearray(b"\x1f\x8b\x08\x00\x00\x00\x00\x00\x00\xff")
    chunks = [content[index:index + 65535] for index in range(0, len(content), 65535)]
    if not chunks:
        chunks = [b""]
    for index, chunk in enumerate(chunks):
        # BFINAL followed by BTYPE=00 and zero padding to the byte boundary.
        encoded.append(1 if index == len(chunks) - 1 else 0)
        length = len(chunk)
        encoded.extend(length.to_bytes(2, "little"))
        encoded.extend((length ^ 0xFFFF).to_bytes(2, "little"))
        encoded.extend(chunk)
    encoded.extend((binascii.crc32(content) & 0xFFFFFFFF).to_bytes(4, "little"))
    encoded.extend((len(content) & 0xFFFFFFFF).to_bytes(4, "little"))
    return bytes(encoded)


def _write_tar(path: Path, members: dict[str, tuple[bytes, int]], compressed: bool) -> None:
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name in sorted(members):
            content, mode = members[name]
            archive.addfile(_tar_info(name, content, mode), io.BytesIO(content))
    payload = _stored_gzip(tar_bytes.getvalue()) if compressed else tar_bytes.getvalue()
    with path.open("wb") as raw:
        raw.write(payload)
        raw.flush()
        os.fsync(raw.fileno())


def build_release(root: Path, output: Path) -> dict:
    """Build an atomic deterministic archive and return its verified report."""
    root_input = root.absolute()
    if root_input.is_symlink() or _is_reparse_point(root_input):
        raise ReleaseError(f"release root is not a regular directory: {root_input}")
    root = root.resolve()
    output = output.absolute()
    output_resolved = output.resolve(strict=False)
    try:
        output_resolved.relative_to(root)
    except ValueError:
        pass
    else:
        runtime = _private_runtime_root(root)
        try:
            output_resolved.relative_to(runtime)
        except ValueError as exc:
            raise ReleaseError(
                "release output must be outside the source repository or inside its "
                "configured private runtime_root"
            ) from exc
    if output.exists() and (output.is_symlink() or _is_reparse_point(output)):
        raise ReleaseError(f"release output is a link or reparse point: {output}")

    kind = _archive_kind(output)
    source_before, status_before = _git_state(root)
    if source_before["dirty"]:
        raise ReleaseError(
            "production release requires a clean source repository; "
            "commit or otherwise settle every tracked and untracked change first"
        )
    authority_before = _source_authority_evidence(root)
    version, legal_paths, files = collect_files(root)
    authority_after = _source_authority_evidence(root)
    if authority_before != authority_after:
        raise ReleaseError(
            "pre-exclusion authority receipt changed while building release inputs"
        )
    source_after, status_after = _git_state(root)
    if source_before != source_after or status_before != status_after:
        raise ReleaseError("source repository changed while building release inputs")

    manifest = _manifest(
        version, legal_paths, files, source_after, authority_after
    )
    members = {release_file.path: (release_file.content, release_file.mode)
               for release_file in files}
    members[MANIFEST_PATH] = (_canonical_json(manifest), 0o644)

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent)
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        if kind == "zip":
            _write_zip(temporary, members)
        else:
            _write_tar(temporary, members, compressed=kind == "tar.gz")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return verify_archive(output)


def _bounded_archive(path: Path) -> None:
    if path.is_symlink() or _is_reparse_point(path):
        raise ReleaseError(f"archive is a link or reparse point: {path}")
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ReleaseError(f"cannot read archive: {exc}") from exc
    if size > MAX_ARCHIVE_BYTES:
        raise ReleaseError(f"archive exceeds {MAX_ARCHIVE_BYTES} bytes")


def _read_zip(path: Path) -> tuple[str, dict[str, ArchiveMember]]:
    members: dict[str, ArchiveMember] = {}
    total = 0
    try:
        with zipfile.ZipFile(path, "r") as archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS:
                raise ReleaseError(f"archive has more than {MAX_MEMBERS} members")
            for info in infos:
                name = _safe_member_path(info.filename)
                if name in members:
                    raise ReleaseError(f"archive contains duplicate member: {name}")
                unix_mode = info.external_attr >> 16
                if stat.S_ISLNK(unix_mode):
                    raise ReleaseError(f"archive contains symlink: {name}")
                if info.is_dir() or (unix_mode and not stat.S_ISREG(unix_mode)):
                    raise ReleaseError(f"archive contains non-regular member: {name}")
                if info.flag_bits & 0x1:
                    raise ReleaseError(f"archive contains encrypted member: {name}")
                if info.file_size > MAX_FILE_BYTES:
                    raise ReleaseError(f"archive member exceeds size limit: {name}")
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    raise ReleaseError("archive expanded content exceeds size limit")
                with archive.open(info, "r") as source:
                    content = source.read(MAX_FILE_BYTES + 1)
                if len(content) != info.file_size:
                    raise ReleaseError(f"archive member size mismatch: {name}")
                mode = stat.S_IMODE(unix_mode) if unix_mode else None
                members[name] = ArchiveMember(name, content, mode)
    except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
        if isinstance(exc, ReleaseError):
            raise
        raise ReleaseError(f"invalid zip archive: {exc}") from exc
    return "zip", members


def _read_tar(path: Path) -> tuple[str, dict[str, ArchiveMember]]:
    members: dict[str, ArchiveMember] = {}
    total = 0
    try:
        with tarfile.open(path, "r:*") as archive:
            infos = archive.getmembers()
            if len(infos) > MAX_MEMBERS:
                raise ReleaseError(f"archive has more than {MAX_MEMBERS} members")
            for info in infos:
                name = _safe_member_path(info.name)
                if name in members:
                    raise ReleaseError(f"archive contains duplicate member: {name}")
                if info.issym() or info.islnk():
                    raise ReleaseError(f"archive contains symlink or hardlink: {name}")
                if not info.isreg():
                    raise ReleaseError(f"archive contains non-regular member: {name}")
                if info.size > MAX_FILE_BYTES:
                    raise ReleaseError(f"archive member exceeds size limit: {name}")
                total += info.size
                if total > MAX_TOTAL_BYTES:
                    raise ReleaseError("archive expanded content exceeds size limit")
                source = archive.extractfile(info)
                if source is None:
                    raise ReleaseError(f"cannot read archive member: {name}")
                content = source.read(MAX_FILE_BYTES + 1)
                if len(content) != info.size:
                    raise ReleaseError(f"archive member size mismatch: {name}")
                members[name] = ArchiveMember(name, content, stat.S_IMODE(info.mode))
    except (OSError, tarfile.TarError) as exc:
        if isinstance(exc, ReleaseError):
            raise
        raise ReleaseError(f"invalid tar archive: {exc}") from exc
    kind = "tar.gz" if path.name.lower().endswith((".tar.gz", ".tgz")) else "tar"
    return kind, members


def _read_archive(path: Path) -> tuple[str, dict[str, ArchiveMember]]:
    _bounded_archive(path)
    if zipfile.is_zipfile(path):
        return _read_zip(path)
    if tarfile.is_tarfile(path):
        return _read_tar(path)
    raise ReleaseError("file is neither a readable zip nor tar archive")


def _parse_manifest(member: ArchiveMember) -> dict:
    try:
        manifest = json.loads(member.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"release manifest is not valid UTF-8 JSON: {exc}") from exc
    expected_keys = {
        "schema", "version", "source", "authority_evidence", "license_files",
        "normalization", "files",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected_keys:
        raise ReleaseError("release manifest fields are malformed")
    if manifest.get("schema") != SCHEMA:
        raise ReleaseError(f"release manifest schema must be {SCHEMA}")
    if not VERSION_RE.fullmatch(str(manifest.get("version") or "")):
        raise ReleaseError("release manifest version is missing or malformed")
    source = manifest.get("source")
    if not isinstance(source, dict) or set(source) != {"commit", "dirty"}:
        raise ReleaseError("release manifest source metadata is malformed")
    if not COMMIT_RE.fullmatch(str(source.get("commit") or "")):
        raise ReleaseError("release manifest source commit is malformed")
    if not isinstance(source.get("dirty"), bool):
        raise ReleaseError("release manifest dirty indicator is malformed")
    authority_evidence = manifest.get("authority_evidence")
    if not isinstance(authority_evidence, dict) or set(authority_evidence) != {
        "receipt_trust", "identity_model", "project_receipt_trust",
    }:
        raise ReleaseError("release manifest authority evidence is malformed")
    if authority_evidence.get("receipt_trust") != ARCHIVE_RECEIPT_TRUST:
        raise ReleaseError(
            "release manifest receipt_trust must be portable-policy"
        )
    if authority_evidence.get("identity_model") != AUTHORITY_IDENTITY_MODEL:
        raise ReleaseError(
            "release manifest authority identity model is malformed"
        )
    if authority_evidence.get("project_receipt_trust") not in PROJECT_RECEIPT_TRUST:
        raise ReleaseError(
            "release manifest project receipt trust is malformed"
        )
    if manifest.get("normalization") != {
            "line_endings": "lf",
            "regular_mode": "0644",
            "executable_mode": "0755",
            "timestamps": "fixed",
    }:
        raise ReleaseError("release manifest normalization metadata is malformed")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ReleaseError("release manifest files must be a list")
    paths: list[str] = []
    for entry in files:
        if not isinstance(entry, dict) or set(entry) != {"path", "bytes", "sha256", "mode"}:
            raise ReleaseError("release manifest contains a malformed file entry")
        path = _safe_member_path(entry.get("path"))
        if path == MANIFEST_PATH or not is_allowlisted(path):
            raise ReleaseError(f"release manifest contains non-allowlisted path: {path}")
        if not isinstance(entry.get("bytes"), int) or not 0 <= entry["bytes"] <= MAX_FILE_BYTES:
            raise ReleaseError(f"release manifest has invalid size for: {path}")
        if not SHA256_RE.fullmatch(str(entry.get("sha256") or "")):
            raise ReleaseError(f"release manifest has invalid SHA-256 for: {path}")
        if entry.get("mode") not in ("0644", "0755"):
            raise ReleaseError(f"release manifest has invalid mode for: {path}")
        paths.append(path)
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise ReleaseError("release manifest paths must be unique and sorted")
    if not REQUIRED_KIT_FILES.issubset(paths):
        raise ReleaseError("release manifest is missing required kit files")
    licenses = manifest.get("license_files")
    if (not isinstance(licenses, list) or not licenses or licenses != sorted(licenses)
            or len(licenses) != len(set(licenses))):
        raise ReleaseError("release manifest legal metadata is missing or malformed")
    if any(path not in LEGAL_FILES or path not in paths for path in licenses):
        raise ReleaseError("release manifest legal metadata does not match its files")
    return manifest


def inspect_archive(path: Path) -> dict:
    """Inspect archive structure safely without extracting any member."""
    kind, members = _read_archive(path)
    if MANIFEST_PATH not in members:
        raise ReleaseError(f"archive is missing {MANIFEST_PATH}")
    manifest = _parse_manifest(members[MANIFEST_PATH])
    return {
        "format": kind,
        "member_count": len(members),
        "members": [
            {
                "path": name,
                "bytes": len(member.content),
                "sha256": hashlib.sha256(member.content).hexdigest(),
                "mode": format(member.mode, "04o") if member.mode is not None else None,
            }
            for name, member in sorted(members.items())
        ],
        "manifest": manifest,
    }


def _verified_archive(path: Path) -> tuple[dict, dict[str, ArchiveMember]]:
    """Return a verification report and the exact members it authenticated."""
    kind, members = _read_archive(path)
    manifest_member = members.get(MANIFEST_PATH)
    if manifest_member is None:
        raise ReleaseError(f"archive is missing {MANIFEST_PATH}")
    manifest = _parse_manifest(manifest_member)
    if manifest["source"]["dirty"]:
        raise ReleaseError(
            "release manifest records dirty source and is not production provenance"
        )
    if manifest_member.content != _canonical_json(manifest) or manifest_member.mode != 0o644:
        raise ReleaseError("release manifest is not canonically encoded")
    expected = {entry["path"]: entry for entry in manifest["files"]}
    actual_paths = set(members) - {MANIFEST_PATH}
    if actual_paths != set(expected):
        missing = sorted(set(expected) - actual_paths)
        extra = sorted(actual_paths - set(expected))
        raise ReleaseError(f"archive member set differs from manifest; missing={missing}, extra={extra}")
    for name, expected_entry in expected.items():
        member = members[name]
        if len(member.content) != expected_entry["bytes"]:
            raise ReleaseError(f"archive size does not match manifest: {name}")
        if hashlib.sha256(member.content).hexdigest() != expected_entry["sha256"]:
            raise ReleaseError(f"archive SHA-256 does not match manifest: {name}")
        if member.mode is None or format(member.mode, "04o") != expected_entry["mode"]:
            raise ReleaseError(f"archive mode does not match manifest: {name}")
        expected_mode = "0755" if name.endswith(".py") or name == "kit" else "0644"
        if expected_entry["mode"] != expected_mode:
            raise ReleaseError(f"archive mode violates normalization policy: {name}")
        if b"\r" in member.content:
            raise ReleaseError(f"archive content does not use LF line endings: {name}")
    version_content = members["VERSION"].content.decode("utf-8").strip()
    if version_content != manifest["version"]:
        raise ReleaseError("VERSION content does not match release manifest")
    if members["ARCHITECTURE.md"].content != CANONICAL_ARCHITECTURE:
        raise ReleaseError(
            "ARCHITECTURE.md is not the canonical empty release template"
        )
    if members["arch.rules.json"].content != CANONICAL_ARCH_RULES:
        raise ReleaseError(
            "arch.rules.json is not the canonical genre-neutral release template"
        )
    for license_path in manifest["license_files"]:
        if not members[license_path].content.strip():
            raise ReleaseError(f"archive legal metadata is empty: {license_path}")
    archive_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    report = {
        "ok": True,
        "format": kind,
        "version": manifest["version"],
        "source": manifest["source"],
        "authority_evidence": manifest["authority_evidence"],
        "files": len(expected),
        "archive_sha256": archive_hash,
    }
    return report, members


def read_verified_archive(path: Path) -> tuple[dict, dict[str, ArchiveMember]]:
    """Return authenticated archive metadata and members for the installer.

    Installation and upgrade need the same bounded reader as release
    verification.  Keeping one public entry point prevents a second extractor
    from drifting away from the release allowlist, size limits and hash checks.
    Callers receive immutable in-memory member values and must never use a
    general-purpose ``extractall`` operation.
    """
    return _verified_archive(path)


def verify_archive(path: Path) -> dict:
    """Verify allowlist, manifest, hashes, sizes, modes, legal data, and version."""
    report, _members = _verified_archive(path)
    return report


def smoke_archive(path: Path, workspace: Path) -> dict:
    """Extract a verified archive once and start its public platform launcher."""
    report, members = _verified_archive(path)
    workspace = workspace.absolute()
    if workspace.exists() or workspace.is_symlink() or _is_reparse_point(workspace):
        raise ReleaseError(f"release smoke workspace already exists: {workspace}")
    if not workspace.parent.is_dir():
        raise ReleaseError(
            f"release smoke workspace parent is unavailable: {workspace.parent}"
        )
    try:
        workspace.mkdir()
        for name, member in sorted(members.items()):
            target = workspace.joinpath(*name.split("/"))
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                output.write(member.content)
            if member.mode is not None:
                target.chmod(member.mode)
    except OSError as exc:
        raise ReleaseError(f"could not extract verified release for smoke test: {exc}") from exc

    if os.name == "nt":
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "kit.cmd", "--help"]
        launcher = "kit.cmd"
    else:
        command = [str(workspace / "kit"), "--help"]
        launcher = "kit"
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        completed = subprocess.run(
            command,
            cwd=str(workspace),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            check=False,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(f"extracted public launcher could not run: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise ReleaseError(
            "extracted public launcher failed"
            + (f": {detail[-1000:]}" if detail else "")
        )
    return {
        **report,
        "smoke": {
            "launcher": launcher,
            "exit_code": completed.returncode,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT,
                        help="repository root (default: repository containing this tool)")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build", help="build and verify a release archive")
    build_parser.add_argument("archive", type=Path)
    inspect_parser = subparsers.add_parser("inspect", help="safely inspect an archive")
    inspect_parser.add_argument("archive", type=Path)
    verify_parser = subparsers.add_parser("verify", help="fully verify an archive")
    verify_parser.add_argument("archive", type=Path)
    smoke_parser = subparsers.add_parser(
        "smoke", help="verify, extract, and start the public launcher"
    )
    smoke_parser.add_argument("archive", type=Path)
    smoke_parser.add_argument("workspace", type=Path)
    arguments = parser.parse_args()

    try:
        if arguments.command == "build":
            result = build_release(arguments.root, arguments.archive)
        elif arguments.command == "inspect":
            result = inspect_archive(arguments.archive)
        elif arguments.command == "smoke":
            result = smoke_archive(arguments.archive, arguments.workspace)
        else:
            result = verify_archive(arguments.archive)
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
