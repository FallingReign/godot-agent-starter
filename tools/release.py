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
    "tools/brownfield.py",
    "tools/cockpit.py",
    "tools/design.py",
    "tools/engine_discovery.py",
    "tools/friction.py",
    "tools/gddoc.py",
    "tools/gd_signature.py",
    "tools/gdls.py",
    "tools/gen_gdscript_doc.py",
    "tools/kit_change.py",
    "tools/kit_change_check.py",
    "tools/kit_change_controller.py",
    "tools/kit_change_html.py",
    "tools/managed_launcher.py",
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
    "tools/tests/test_brownfield.py",
    "tools/tests/test_brownfield_gate.py",
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
    "tools/tests/test_kit_change.py",
    "tools/tests/test_kit_change_board.py",
    "tools/tests/test_kit_change_check.py",
    "tools/tests/test_kit_change_controller.py",
    "tools/tests/test_kit_change_html.py",
    "tools/tests/test_kit_cli.py",
    "tools/tests/test_layout_consumers.py",
    "tools/tests/test_managed_launcher.py",
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
INSTALL_MANIFEST_PATH = "INSTALL-MANIFEST.json"
INSTALL_SOURCE_PATHS = frozenset({
    "install/agents.block.md",
    "install/copilot.block.md",
    "install/gitattributes.block",
    "install/gitignore.block",
    "install/kit",
    "install/kit.cmd",
    "install/kit.config.default.json",
})
GENERATED_INSTALL_FILES = INSTALL_SOURCE_PATHS | {INSTALL_MANIFEST_PATH}
INSTALL_SUPPORTED_FROM = (0, 3, 0)
INSTALL_SCHEMA = 1
INSTALL_LAYOUT_SCHEMA = 1
INSTALL_CONFIG_SCHEMA = 1
INSTALL_BLOCK_BEGIN = "<!-- BEGIN GODOT AGENT KIT -->"
INSTALL_BLOCK_END = "<!-- END GODOT AGENT KIT -->"
INSTALL_TEXT_BEGIN = "# BEGIN GODOT AGENT KIT"
INSTALL_TEXT_END = "# END GODOT AGENT KIT"

# The project launchers are deliberately smaller and more stable than the
# release they select.  They start the self-contained managed launcher, which
# authenticates current.json and every active release member before importing
# the release's kit.py.
MANAGED_UNIX_LAUNCHER = b"""#!/bin/sh
set -eu

export PYTHONDONTWRITEBYTECODE=1
KIT_PROJECT_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
KIT_MANAGED_LAUNCHER="$KIT_PROJECT_ROOT/.agent-kit/launcher.py"

if [ ! -f "$KIT_MANAGED_LAUNCHER" ]; then
    echo "kit: the managed launcher is missing; use the kit change recovery action." >&2
    exit 3
fi
if [ -n "${KIT_PYTHON:-}" ]; then
    case "$KIT_PYTHON" in
        /*) ;;
        *) echo "kit: KIT_PYTHON must name one absolute interpreter file." >&2; exit 3 ;;
    esac
    if [ ! -f "$KIT_PYTHON" ] || [ ! -x "$KIT_PYTHON" ]; then
        echo "kit: KIT_PYTHON does not name an executable interpreter file." >&2
        exit 3
    fi
    exec "$KIT_PYTHON" "$KIT_MANAGED_LAUNCHER" "$@"
fi
if command -v python3 >/dev/null 2>&1; then
    exec python3 "$KIT_MANAGED_LAUNCHER" "$@"
fi
if command -v python >/dev/null 2>&1; then
    exec python "$KIT_MANAGED_LAUNCHER" "$@"
fi
echo "kit: its internal runtime is unavailable; Python 3.10 or newer is required." >&2
exit 3
"""

MANAGED_WINDOWS_LAUNCHER = br"""@echo off
setlocal
set "KIT_PROJECT_ROOT=%~dp0"
set "KIT_MANAGED_LAUNCHER=%KIT_PROJECT_ROOT%.agent-kit\launcher.py"
set "PYTHONDONTWRITEBYTECODE=1"

if exist "%KIT_MANAGED_LAUNCHER%" goto launcher_exists
echo kit: the managed launcher is missing; use the kit change recovery action. 1>&2
exit /b 3

:launcher_exists
if defined KIT_PYTHON goto use_kit_python
where py >nul 2>&1
if not errorlevel 1 goto use_py
where python3 >nul 2>&1
if not errorlevel 1 goto use_python3
where python >nul 2>&1
if not errorlevel 1 goto use_python
echo kit: its internal runtime is unavailable; Python 3.10 or newer is required. 1>&2
exit /b 3

:use_kit_python
for %%I in ("%KIT_PYTHON%") do set "KIT_PYTHON_RESOLVED=%%~fI"
if /I not "%KIT_PYTHON%"=="%KIT_PYTHON_RESOLVED%" goto kit_python_invalid
if not exist "%KIT_PYTHON%" goto kit_python_invalid
for %%I in ("%KIT_PYTHON%") do set "KIT_PYTHON_ATTRIBUTES=%%~aI"
if /I "%KIT_PYTHON_ATTRIBUTES:~0,1%"=="d" goto kit_python_invalid
"%KIT_PYTHON%" "%KIT_MANAGED_LAUNCHER%" %*
exit /b %errorlevel%

:kit_python_invalid
echo kit: KIT_PYTHON must name one absolute interpreter file. 1>&2
exit /b 3

:use_py
py -3 "%KIT_MANAGED_LAUNCHER%" %*
exit /b %errorlevel%
:use_python3
python3 "%KIT_MANAGED_LAUNCHER%" %*
exit /b %errorlevel%
:use_python
python "%KIT_MANAGED_LAUNCHER%" %*
exit /b %errorlevel%
"""

MANAGED_AGENT_BLOCK_BODY = """# Managed Godot Agent Kit

This project uses a versioned kit under `.agent-kit/`.

Before planning, running commands, editing files, or delegating work:

1. Run `kit doctor --json`.
2. Read `.agent-kit/current.json`.
3. Read the complete `AGENTS.md` inside the `active_release.core_path` named there.

Those active rules are provider-neutral and apply to Codex, Copilot, and every
delegated agent. Never edit files inside `.agent-kit/` by hand. Use the kit
change workflow to install, upgrade, restore, or roll back the kit.

No game code may be written without design backing. If the design is missing,
follow the active rules for disclosure, confidence, veto, and go/no-go approval.
"""

MANAGED_COPILOT_BLOCK_BODY = """# Managed Godot Agent Kit bridge

@../AGENTS.md

The provider-neutral project contract is `../AGENTS.md`. Read it in full before
planning, running commands, editing files, or delegating work. If this client
cannot load that file, stop and tell the human; do not substitute chat history,
model defaults, or silence.
"""

DEFAULT_KIT_CONFIG = {
    "schema": 1,
    "game_root": "src",
    "runtime_root": ".kit/runtime",
    "note_threshold": 10,
    "providers": {
        "analyzer": {
            "kind": "manual",
            "model": "",
            "timeout_minutes": 30,
        },
        "worker": {
            "kind": "manual",
            "model": "",
            "persona": "kit-builder",
            "timeout_minutes": 30,
        },
    },
    "dispatch_policy": {
        "owned": [
            ".agents",
            ".github/agents",
            ".github/copilot-instructions.md",
            "docs",
            "tools",
            "AGENTS.md",
            "ARCHITECTURE.md",
            "CLAUDE.md",
            "README.md",
            "SETUP.md",
            "VERIFY.md",
            "arch.py",
            "arch.rules.json",
            "bootstrap.py",
            "check.py",
            "gate.rules.json",
            "import_profiles.json",
            "kit.config.json",
            "sanitise.py",
        ],
        "forbidden": [
            ".agent-kit.json",
            ".agents",
            ".gate.sha256",
            ".github/agents",
            ".github/copilot-instructions.md",
            ".github/workflows",
            ".kit",
            "AGENTS.md",
            "CLAUDE.md",
            "arch.py",
            "arch.rules.json",
            "bootstrap.py",
            "check.py",
            "dependencies.lock.json",
            "docs/design",
            "docs/retro",
            "gate.rules.json",
            "import_profiles.json",
            "kit",
            "kit.cmd",
            "kit.config.json",
            "kit.py",
            "project.shape.json",
            "proposal.json",
            "sanitise.py",
            "src",
            "tools",
        ],
    },
}

SAFE_ARCHIVE_PATH_RE = re.compile(r"[A-Za-z0-9._/-]+\Z")
VERSION_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._+-]{0,63}\Z")
SEMVER_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.-]+)?\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")

# These hashes bind the one flat release that the managed lifecycle knows how
# to migrate.  They were read from the verified 0.2.0 archive whose immutable
# identity is recorded below.  Runtime files are never consulted by finished
# release or install code.
LEGACY_0_2_0_ARCHIVE_SHA256 = (
    "b5a2f7dafd4a21db099d30f295caceea3a20d4303e9b885d1e0fe24873f5e4dc"
)
LEGACY_0_2_0_SOURCE_COMMIT = "17b1ecb0f0b3595a2dbe75d7fb514f960f2b8699"
LEGACY_0_2_0_SURFACE_SHA256 = {
    ".agent-kit.json": "ee9e7cdb8dde605177b16bae4d0dced46820d7bb4b9ece2ee93182ccb9a7d6e2",
    ".agents/skills/godot-api-lookup/SKILL.md": "f2a5b27bcbf0d57b8ce60f305512618d7adeb128929f89d54b9f9d205045ed8b",
    ".agents/skills/godot-content-pipeline/SKILL.md": "1d493f01b14b79a5fbf6cc9b1fad01331b001e5f3b31e15dd05c823d2de58c26",
    ".agents/skills/godot-data-layout/SKILL.md": "712626cd421b1499c4876b829245bc17a6f16bb1587fad68eefb7f6edf221470",
    ".agents/skills/godot-design-discovery/SKILL.md": "8a0383f78272bec4b23a6eda07f7146e01123ae55c395131d41a54d9aeb5d6c4",
    ".agents/skills/godot-design-retrieval/SKILL.md": "c3b6461192fde2faa15482e346e5339c2302465845a2adc58d535abb13096976",
    ".agents/skills/godot-design-sections/SKILL.md": "723675eb3a5c853026c1440966d3044b069d89acde20dd7a37f5ca2ab4f2959a",
    ".agents/skills/godot-headless-verification/SKILL.md": "2f466fef3f74b6d99050d889d290cc28b7eb8f00251ad037fa0b3857be2827b4",
    ".agents/skills/godot-human-involvement/SKILL.md": "d5f837b7b2326111bb066b0487f34d559bf473d239189ce2cdb289529963522b",
    ".agents/skills/godot-increment-size/SKILL.md": "ab06cc5654c6c4e2516cc77ea4b3459454a01396545d98ca4167f34110c56181",
    ".agents/skills/godot-lifecycle-and-signals/SKILL.md": "1634aa0a5570892d7baf860e0657c56a9dc68a6bdf01efc4fa5125a6c1045b3a",
    ".agents/skills/godot-multiplayer-authority/SKILL.md": "c455f9384a380572ad45bb219749c744c0e88650282e68af44fb2f40db40fdf7",
    ".agents/skills/godot-node-or-resource/SKILL.md": "5119487eca9046b367e506407c3e61d02f4860716f36310294f3109703a07202",
    ".agents/skills/godot-performance-evidence/SKILL.md": "a2359575bdc352d66baf29275066540c14a66153242c1d3c56090aa1662bec83",
    ".agents/skills/godot-project-decisions/SKILL.md": "2ea52b92cd1ecae83d0b7b0d00ed60aae96fade8a465dcbd6951c4e894ebc406",
    ".agents/skills/godot-resolving-ambiguity/SKILL.md": "5f4fe5ae4db9105eaa27d759e831ac09a05aed885242c646a43d3f4bf3543d77",
    ".agents/skills/godot-retrospective/SKILL.md": "950f7839dc0a945af3a596e14d6c5ebe363ce9e47e0ac432b38ff94b377f2e8a",
    ".agents/skills/godot-scene-files/SKILL.md": "16254366a64e1b73561f7dfa988902781e40ba33d94bf4447d03738c4de3413c",
    ".agents/skills/godot-tooling-friction/SKILL.md": "3bfa455c4bb6d04d99d657ae51f5859465019bb094676fdbacd3d63e3eb6f0d4",
    ".agents/skills/godot-typed-data-boundary/SKILL.md": "4c75bbb0a45c7cdfc856d8e45c9eae1af0326465054d2634e218d0a73e85c03e",
    ".agents/skills/shell-compat/SKILL.md": "c77c09fa42cf444f377455ee8115eeef36b6d4047934f61d4a2f141040b535c0",
    ".gitattributes": "489fb811f97deb0befb181e8eda6330bd155b6e1a3d7cc4b717391c31cf870f0",
    ".github/agents/game-builder.agent.md": "98ebe382ad2818c7e6ee8f986d2e1fa53721dd6b164694ebca66e2644ea740df",
    ".github/agents/kit-builder.agent.md": "9c450bfd8ac1a0191a2c08e584236637e233b907f2f619ada0cb6bc95a90e7ef",
    ".github/agents/retrospective.agent.md": "127290ba785b3afc5a000664fde263425423028d3912fe4e74a58e00fe2b21ed",
    ".github/copilot-instructions.md": "9c1865267c6d6c14e4745559f2b203fc2946431600d19096540fc395e9e0d885",
    ".gitignore": "9d9452442d1f83fba5479f0e1e5cbc2e8eb574b90348652b615a237db89042b8",
    "AGENTS.md": "3ec8a4f2b98af12aca084809c5904e69ca7fad404e2a983aa80ca35eaa8d910b",
    "kit": "a8a560126017bed24a9fb1325696a24f5d62205f36dc972f9c09b48add20917b",
    "kit.cmd": "557d0d43b30b8db4a7438ebe5e5c478a083c90512703a4b1e06b4e15951b0dcd",
}
LEGACY_0_2_0_RETIRED_SHA256 = {
    ".github/workflows/ci.yml": "4266ad70d68f7dbbfd6e969d85ffc13c53ba225f4b9e617770e6a8e44a7cac39",
    "SETUP.md": "e660eea38924256d814046826813a4d828377211e56e1419486c890d4a1efcdf",
    "VERIFY.md": "1c44b77f461a82698ebdbdae4f5c3a58751ec73bb53edd88a0c7930239e1b6aa",
    "arch.py": "bc02809dde2f8dc97478d2dbddebacfd8d1911f8e1f160254bb455a16bb0e53e",
    "bootstrap.py": "9e0b5718860333d2c5f503fc065a7117f827e239d6fdcc5324e3ce4ac17af629",
    "check.py": "30c1875b36619ed4345b07344c5621476e3df9d2b9534b72722b812e53ce369c",
    "dependencies.lock.json": "208b8196ee28d65351dbeed7977deeb3c0f4b535fcd83fa737600a7d14424446",
    "docs/DECISIONS.md": "5265f7a24fcf7eac803b3b024626545fe5563cf32f29843146c62c82792879ce",
    "docs/DESIGN.md": "b15c00c8de8f7b81a8553643181d996db4c322a1a62b766fc42870d024cf8cfa",
    "docs/GATE.md": "2da843abe4506fe707ec9e94bd32727f313419e686be48c2728555112472bcf1",
    "docs/GDSCRIPT.md": "07337530e286facce9800c5f127f728b221d021433ab8621908ae187e06e7a91",
    "docs/RULES.md": "94dbe88f9f361d2bf7210920a882fc007fbea941f81426b5ab79670cadc40a62",
    "docs/SCENES.md": "bf81763573abbac85ce75f6a20790b1d9f1f2d43fcaa6a5b6d5adad8f75f413e",
    "docs/WORKFLOW.md": "1a55860256ca786e212a751843342943e39053834909811c762dbf802ebc8797",
    "gate.rules.json": "1a7c79b2d2541ba4a30c02caf90fb8e757d36155f5a70014c24ea505207722ff",
    "import_profiles.json": "033c6039c64de2f9764558a49040476eca0c912e5f0910a5ecba45d0a892b3a5",
    "kit.py": "b08bbacd0483e692cebd63a33dd3770e6649a3aa88186035672bcc19904854b5",
    "sanitise.py": "2de8023205108131e18c324131a1099f9fc7895b337143d0ce783c98da884d84",
    "tools/authored_scope.py": "3893bf38efcf92ae94c3f3eb1d1ea657162096f9601155c35e47ad2b8d909c98",
    "tools/board.py": "143a0d1f5e6ccf232e19eaf8d92ada47e3fae2f9d7fbc35dfb1a90183ab4665d",
    "tools/board_client.py": "2a5b935e7948202da4a8e9115d7c7e5bca1a3715dd49be41d8d018a91ebeae0a",
    "tools/cockpit.py": "94614a75a358b837f43b92f345c127feaf3aec2686599c65f33b3346c61f6fd6",
    "tools/design.py": "7b2be0b3aa3788996fe10309a6d8fb401b89505cb01cf405c0bc1be7dcd0a993",
    "tools/engine_discovery.py": "75712ab619e3aa35e7e0cc344f6a7426b70730849cea890880c0bd7b201ca170",
    "tools/friction.py": "f00ee0e1e4d3fcdbf08c5b1e0d842f3e5f539a7c7a951057029f71f4de1e5434",
    "tools/gd_signature.py": "557ea89baf5508d238500edc70606cfb4c120658430077f3b08938dad6badafe",
    "tools/gddoc.py": "1db287bd5ead75df3c82ac8de520d1816f161a4bdb4e25e4b3a2fffa45355ad5",
    "tools/gdls.py": "a92dab79734217a5e98193dd545dab0b533852c8c0fe648ef4c28cbcc02fe25f",
    "tools/gen_gdscript_doc.py": "2f3b90f9391e18b8ceab059192b596bcd76b2fa672bdaaae9e959e3e3c10aaa6",
    "tools/md.py": "fa756b421874a6107303983a2813da0f108bb0ddb9812a558ae7060961cc11d2",
    "tools/native_engine.py": "9f9a7a08cf9b0d9be37d1fe50d0499837ef8b0ee908a529a54a9a04e1e4bf9e3",
    "tools/plan_html.py": "a2466d6b05bcf9ea4ea20906bc24052b5807893d46af1d0d8b2f4526a386794c",
    "tools/process_supervisor.py": "c5f88d52dab0ad5e126023977ab1880eddb3b4d08db933cf220e0d9a9b938157",
    "tools/project_context.py": "78cd284d251803bed348aae47bff0b30f09b2a91956b07e83c64982e8729f922",
    "tools/proposal_authority.py": "00ac22df2c9c20dbe74fb4ef5d3f6920e2110e8bb62ce3ee4099780d5fcce91e",
    "tools/providers.py": "cff2d3b84c683158c05bdb1d8bc39054024fc45b04e2b5b4048bf68397539cc2",
    "tools/release.py": "04a5c6317d9031f8a3962c6b8be06b0221ba6c85fb48a8c1b9d9c1e65c1618b3",
    "tools/retro.py": "c7beb0c8278038bff4ef833646dc5c7087f66678a650fee9126557fed3028867",
    "tools/retro_due.py": "bbcbee1190f4412d29007dfd6df85e3598aeaaf9aabfe6f5d4894b6bf62d2b0e",
    "tools/retro_html.py": "2ca8032acccd6be3f681fdbd20d8c3b18f395a941c9aa0004557b66fefe109a5",
    "tools/retro_ledger.py": "10fba62d2a4b0332dda35756b972d76a798bd0f0131cc897aeff84296c928202",
    "tools/retro_queue.py": "d091e73ebd0851fa395249c412db5e95dfb0b28361abf65332ecde4281208623",
    "tools/retro_rank.py": "88911f6146f827edec352b2c717f79cc03289001636fcba9aa4818110f1ddb91",
    "tools/retro_sdk.py": "37cd3e35c6b93f83678aa111a4e2f3d7408fa5ad23ff6a51f9d24ff00bc60f21",
    "tools/run_result.py": "cfe84c1d98418c2bcb337f5dfd8e309cf7a6acb04495909137602871a70ea853",
    "tools/runtime_paths.py": "17fb4874d88c57fb5b75b921488cd01883ee9641392c52d348498660139cf76f",
    "tools/schema.py": "08328ec967f17a7017dc68efdbc9e41f6803b5c0fc9256e6754014d8d28e3aa7",
    "tools/session_digest.py": "c65b1ffcb262eac0e0c411804f3942bb72b25c02228a5febdeecde1dc2523546",
    "tools/session_evidence.py": "ec9683274a60b1fda15eeeb10761122faeb7ff6abfe62ce317984455d75f19ca",
    "tools/strict_verify.py": "1b2fc4206277d75685f28b32c00c77357d168ffec9201c652bcb1dd1e824e075",
    "tools/tests/browser_check.py": "7f54e765325e379868e20e5a071b00ec5c6c794474d626dad12bd977856aba12",
    "tools/tests/dom_harness.js": "084637e58a323996d29555fa63205b78e95175634b4f2ec647924f8529b3db3a",
    "tools/tests/mock_board.py": "e59779dfb8ffa32d181b61251888843d128e97c7e6ffe7954f18c8887c07b35d",
    "tools/tests/page_parts.py": "acc6c9be5387c4de411b44727a381b7bb398ea51f62d0bf9ec4c82031b813897",
    "tools/tests/test_authored_scope.py": "a9b7936544b30facfc55a4721d712656980bd2083474ed3731e8dc78f897d49e",
    "tools/tests/test_board_api.py": "0c29ae870d03726b80f48383d34f8b5de05ba8a5be14f4713f552911323e4abf",
    "tools/tests/test_bootstrap.py": "9b3b75f09c2e0f696fcc939f18f6db5411df763bdce0c9fcc981fa280e2dc0a4",
    "tools/tests/test_cockpit.py": "3e3aa1afe4e7f78239ab18d6b4e6ee45aaf327b1b71359ccee23351c5a26d18d",
    "tools/tests/test_design_conformance.py": "8fe901812d06b6c2fc5832688b83e6a9dff52602504a53fcfd54cb45b4100342",
    "tools/tests/test_design_governance.py": "88b6ec63913640e59d8412e416d932b1a5336e5ace85ddd8e8dc6b30cb888f56",
    "tools/tests/test_engine_boundary.py": "13705f32a6d17474aefe1ba6d1619b05756a30cf0d87c6b1221b9dbb1812ad57",
    "tools/tests/test_engine_discovery.py": "625c219c475eaea7434e500eb95c1e7e44eb8ce2e1f7f1b9187c70ae22c58d27",
    "tools/tests/test_friction.py": "f514c7850906ee67042545082998955adbbd18f0efe49c9d8ac7cc5e49d398fe",
    "tools/tests/test_frontend.py": "b87967386a9007ced5cf78da2263e2511e77fc495a87de2997b99d7883fb12aa",
    "tools/tests/test_gate_receipt.py": "4e0180f83d9f2f705a9838b3157ca0ee6d5e1d37ff580f5c97811643babf6f79",
    "tools/tests/test_gd_signature.py": "bb71e9dbfd3521f4d53d0cb57433f3de4eb11dbdb8c155ab11ac04d8204cf442",
    "tools/tests/test_integration.py": "eac1cd8c455437ef5ab760266a7dd1a2e7e9e9a96c912f5bdf5281a487d04afc",
    "tools/tests/test_kit_cli.py": "5b07a8fe555a2fe841d51fa0cdf9f77f765e4746fd2b38108fb97e7f8541181a",
    "tools/tests/test_layout_consumers.py": "ec40307c057e07b68971864a4988ce7f7d5b9753ea5e6eae7162c19c6e70e473",
    "tools/tests/test_native_engine.py": "82084dd00cb98f0b0e3cd5a863ebe2ab9c356dac4f0bb46ed6ff902bd224970b",
    "tools/tests/test_native_process_containment.py": "13495fbc3eab52074b090a68eb7c1128afb1ae23eda4b700c207a87500dbcf82",
    "tools/tests/test_project_context.py": "d36af78d18354eb348b41f6e73166a7845844892aadb68ed33a4c698a2bf4e40",
    "tools/tests/test_providers.py": "54c8ca2622ea1f2b9a6dd1a4ff18d60222bc089319aecd2539a38af94541effb",
    "tools/tests/test_release.py": "2dfc486fb6ffdf56f83ef39a7b6cf376e5d188634c268a6264dfbb7470c39668",
    "tools/tests/test_retro_due.py": "3e0e3bc3983c16943b9ba18d0f3a5b2d7efb1646116e4e75aaf74ad2cafea6a0",
    "tools/tests/test_retro_ledgers.py": "93496892d4a866b384dad37dc40381290b8aef8d043914a5d3abb7496a480abe",
    "tools/tests/test_retro_queue.py": "1012a65dd59eb5e75f639a5518d10fa033d083c68705094a950117753166fbe3",
    "tools/tests/test_retro_workflow.py": "fbc0567a9e72285fdf07c8d81e56c5ea1ad5c3f42f8f8b1ccc439b061ae14e0d",
    "tools/tests/test_run_result.py": "ba63b8c6898e2b02cc89c27b9e3cd5df8a328a9123cb5677b0312620af84a0f3",
    "tools/tests/test_runtime_paths.py": "7b64802f13458820c6c4d048f0308e0895c1f73c8b6c2d841d0f5e7995b8d6fa",
    "tools/tests/test_session_evidence.py": "d95b474bab2d47374e26797636823f2e4f8877cbfa6e059b3b6102a417684e8a",
    "tools/tests/test_signature_consumers.py": "1151c22d125462877902af6d084800bee7331a8620587aee1cf7a1a1887987b2",
    "tools/tests/test_strict_verify.py": "08ae8716a0f5ac34872c70731293c97fafe2365fd0d44c4025520ab45823cb50",
}
LEGACY_0_2_0_PRESERVED_PATHS = frozenset({
    ".claude/settings.json",
    ".gate.sha256",
    ".gdlintrc",
    "ARCHITECTURE.md",
    "CLAUDE.md",
    "LICENSE",
    "README.md",
    "VERSION",
    "arch.rules.json",
    "docs/retro/README.md",
    "docs/retro/notes/README.md",
    "kit.config.json",
})
MANAGED_LIFECYCLE_SOURCE_FILES = frozenset({
    "tools/brownfield.py",
    "tools/kit_change.py",
    "tools/kit_change_check.py",
    "tools/kit_change_controller.py",
    "tools/kit_change_html.py",
    "tools/managed_launcher.py",
    "tools/tests/test_brownfield.py",
    "tools/tests/test_brownfield_gate.py",
    "tools/tests/test_kit_change.py",
    "tools/tests/test_kit_change_board.py",
    "tools/tests/test_kit_change_check.py",
    "tools/tests/test_kit_change_controller.py",
    "tools/tests/test_kit_change_html.py",
    "tools/tests/test_managed_launcher.py",
})
LEGACY_0_2_0_REQUIRED_KIT_FILES = frozenset(
    (
        set(LEGACY_0_2_0_SURFACE_SHA256)
        | set(LEGACY_0_2_0_RETIRED_SHA256)
        | set(LEGACY_0_2_0_PRESERVED_PATHS)
    )
    - set(LEGAL_FILES)
)
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
    return (
        path in REQUIRED_KIT_FILES
        or path in LEGAL_FILES
        or path in GENERATED_INSTALL_FILES
    )


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
    content = _read_regular_bytes(
        path,
        label=f"allowlisted source {relative}",
        limit=MAX_FILE_BYTES,
    )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ReleaseError(f"allowlisted source is not UTF-8 text: {relative}") from exc
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _read_regular_bytes(path: Path, *, label: str, limit: int) -> bytes:
    """Read one exact regular file once, rejecting redirects, links and races."""
    try:
        path_before = path.lstat()
        if (
            stat.S_ISLNK(path_before.st_mode)
            or _is_reparse_point(path)
            or not stat.S_ISREG(path_before.st_mode)
        ):
            raise ReleaseError(f"{label} is linked or not a regular file: {path}")
        if int(getattr(path_before, "st_nlink", 1)) != 1:
            raise ReleaseError(f"{label} may not be a hard link: {path}")
        if path_before.st_size > limit:
            raise ReleaseError(f"{label} exceeds {limit} bytes: {path}")
        with path.open("rb") as source:
            before = os.fstat(source.fileno())
            content = source.read(limit + 1)
            after = os.fstat(source.fileno())
        path_after = path.lstat()
    except ReleaseError:
        raise
    except OSError as exc:
        raise ReleaseError(f"cannot read {label}: {exc}") from exc
    if len(content) > limit:
        raise ReleaseError(f"{label} exceeds {limit} bytes: {path}")

    path_fields = (
        "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_nlink",
    )
    handle_fields = (
        "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink",
    )
    cross_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
    if (
        tuple(getattr(path_before, key, None) for key in path_fields)
        != tuple(getattr(path_after, key, None) for key in path_fields)
        or tuple(getattr(path_before, key, None) for key in cross_fields)
        != tuple(getattr(before, key, None) for key in cross_fields)
        or tuple(getattr(before, key, None) for key in handle_fields)
        != tuple(getattr(after, key, None) for key in handle_fields)
        or _is_reparse_point(path)
        or len(content) != before.st_size
    ):
        raise ReleaseError(f"{label} changed while reading: {path}")
    return content


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


def _semantic_version(value: str) -> tuple[int, int, int] | None:
    match = SEMVER_RE.fullmatch(value)
    if match is None:
        return None
    return tuple(int(match.group(index)) for index in (1, 2, 3))


def _requires_install_support(version: str) -> bool:
    parsed = _semantic_version(version)
    return parsed is not None and parsed >= INSTALL_SUPPORTED_FROM


def _managed_block(begin: str, body: str | bytes, end: str) -> bytes:
    content = body.encode("utf-8") if isinstance(body, str) else body
    try:
        text = content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError as exc:
        raise ReleaseError("managed install block source is not UTF-8") from exc
    return f"{begin}\n{text.rstrip()}\n{end}\n".encode("utf-8")


def _surface_identifier(prefix: str, path: str) -> str:
    name = PurePosixPath(path).parts[-2] if path.endswith("/SKILL.md") else PurePosixPath(path).stem
    if name.endswith(".agent"):
        name = name[:-6]
    return f"{prefix}-{name}".replace("_", "-")


def _install_owned_files() -> list[dict]:
    files: list[dict] = [
        {
            "id": "marker",
            "path": ".agent-kit.json",
            "source": ".agent-kit.json",
            "mode": "0644",
            "strategy": "replace",
            "legacy_sha256": LEGACY_0_2_0_SURFACE_SHA256[".agent-kit.json"],
        },
        {
            "id": "launcher-unix",
            "path": "kit",
            "source": "install/kit",
            "mode": "0755",
            "strategy": "replace",
            "legacy_sha256": LEGACY_0_2_0_SURFACE_SHA256["kit"],
        },
        {
            "id": "launcher-windows",
            "path": "kit.cmd",
            "source": "install/kit.cmd",
            "mode": "0644",
            "strategy": "replace",
            "legacy_sha256": LEGACY_0_2_0_SURFACE_SHA256["kit.cmd"],
        },
        {
            "id": "managed-launcher",
            "path": ".agent-kit/launcher.py",
            "source": "tools/managed_launcher.py",
            "mode": "0755",
            "strategy": "replace",
            "legacy_sha256": None,
        },
        {
            "id": "continuous-integration",
            "path": ".github/workflows/agent-kit-ci.yml",
            "source": ".github/workflows/ci.yml",
            "mode": "0644",
            "strategy": "replace",
            "legacy_sha256": None,
        },
        {
            "id": "gdscript-style",
            "path": ".gdlintrc",
            "source": ".gdlintrc",
            "mode": "0644",
            "strategy": "create-only",
            "legacy_sha256": None,
        },
        {
            "id": "architecture",
            "path": "ARCHITECTURE.md",
            "source": "ARCHITECTURE.md",
            "mode": "0644",
            "strategy": "create-only",
            "legacy_sha256": None,
        },
        {
            "id": "architecture-rules",
            "path": "arch.rules.json",
            "source": "arch.rules.json",
            "mode": "0644",
            "strategy": "create-only",
            "legacy_sha256": None,
        },
        {
            "id": "project-config",
            "path": "kit.config.json",
            "source": "install/kit.config.default.json",
            "mode": "0644",
            "strategy": "schema-json",
            "legacy_sha256": None,
            "schema_key": "schema",
            "target_schema": INSTALL_CONFIG_SCHEMA,
            "supported_schemas": [INSTALL_CONFIG_SCHEMA],
            "defaults": DEFAULT_KIT_CONFIG,
            "allowed_keys": sorted(DEFAULT_KIT_CONFIG),
        },
    ]
    for path in sorted(REQUIRED_SKILL_FILES):
        files.append({
            "id": _surface_identifier("skill", path),
            "path": path,
            "source": path,
            "mode": "0644",
            "strategy": "replace",
            "legacy_sha256": LEGACY_0_2_0_SURFACE_SHA256[path],
        })
    for path in sorted(REQUIRED_AGENT_FILES):
        files.append({
            "id": _surface_identifier("persona", path),
            "path": path,
            "source": path,
            "mode": "0644",
            "strategy": "replace",
            "legacy_sha256": LEGACY_0_2_0_SURFACE_SHA256[path],
        })
    return sorted(files, key=lambda item: item["id"])


def _install_managed_blocks() -> list[dict]:
    return sorted([
        {
            "id": "agent-policy",
            "path": "AGENTS.md",
            "source": "install/agents.block.md",
            "mode": "0644",
            "legacy_file_sha256": LEGACY_0_2_0_SURFACE_SHA256["AGENTS.md"],
            "begin": INSTALL_BLOCK_BEGIN,
            "end": INSTALL_BLOCK_END,
        },
        {
            "id": "copilot-policy",
            "path": ".github/copilot-instructions.md",
            "source": "install/copilot.block.md",
            "mode": "0644",
            "legacy_file_sha256": LEGACY_0_2_0_SURFACE_SHA256[
                ".github/copilot-instructions.md"
            ],
            "begin": INSTALL_BLOCK_BEGIN,
            "end": INSTALL_BLOCK_END,
        },
        {
            "id": "gitattributes-policy",
            "path": ".gitattributes",
            "source": "install/gitattributes.block",
            "mode": "0644",
            "legacy_file_sha256": LEGACY_0_2_0_SURFACE_SHA256[".gitattributes"],
            "begin": INSTALL_TEXT_BEGIN,
            "end": INSTALL_TEXT_END,
        },
        {
            "id": "gitignore-policy",
            "path": ".gitignore",
            "source": "install/gitignore.block",
            "mode": "0644",
            "legacy_file_sha256": LEGACY_0_2_0_SURFACE_SHA256[".gitignore"],
            "begin": INSTALL_TEXT_BEGIN,
            "end": INSTALL_TEXT_END,
        },
    ], key=lambda item: item["id"])


def _install_manifest_document(version: str) -> dict:
    return {
        "schema": INSTALL_SCHEMA,
        "kind": "agent-kit-install-manifest",
        "kit_version": version,
        "install_schema": INSTALL_SCHEMA,
        "layout_schema": INSTALL_LAYOUT_SCHEMA,
        "config_schema": INSTALL_CONFIG_SCHEMA,
        "supported_legacy_versions": ["0.2.0"],
        "supported_install_schemas": [INSTALL_SCHEMA],
        "core_layout": "versioned-by-archive-sha256",
        "owned_files": _install_owned_files(),
        "managed_blocks": _install_managed_blocks(),
        "legacy_retired_files": [
            {"path": path, "sha256": digest}
            for path, digest in sorted(LEGACY_0_2_0_RETIRED_SHA256.items())
        ],
    }


def _definition_errors(manifest: dict) -> list[str]:
    errors: list[str] = []
    surfaces = [*manifest["owned_files"], *manifest["managed_blocks"]]
    identifiers = [str(item["id"]) for item in surfaces]
    paths = [str(item["path"]) for item in surfaces]
    sources = [str(item["source"]) for item in surfaces]
    if len(identifiers) != len(set(identifiers)):
        errors.append("install surface ids collide")
    if len(paths) != len({path.casefold() for path in paths}):
        errors.append("install surface paths collide by case")
    if len(sources) != len({path.casefold() for path in sources}):
        errors.append("install sources collide by case")
    for label, candidates in (("surface", paths), ("source", sources)):
        parts = [tuple(part.casefold() for part in PurePosixPath(path).parts)
                 for path in candidates]
        for index, left in enumerate(parts):
            for right in parts[index + 1:]:
                shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
                if len(shorter) < len(longer) and longer[:len(shorter)] == shorter:
                    errors.append(f"install {label} paths collide as parent and child")
                    break
            if errors and errors[-1].startswith(f"install {label} paths collide"):
                break
    try:
        for path in [*paths, *sources, *LEGACY_0_2_0_RETIRED_SHA256]:
            _safe_member_path(path)
    except ReleaseError:
        errors.append("install definition contains an unsafe path")
    expected_legacy_surfaces = {
        str(item["path"])
        for item in surfaces
        if item.get("legacy_sha256") is not None
        or item.get("legacy_file_sha256") is not None
    }
    if expected_legacy_surfaces != set(LEGACY_0_2_0_SURFACE_SHA256):
        errors.append("legacy 0.2.0 surface set is incomplete")
    historic_paths = (
        set(LEGACY_0_2_0_REQUIRED_KIT_FILES)
        | {"LICENSE"}
    )
    expected_retired = (
        historic_paths
        - set(LEGACY_0_2_0_SURFACE_SHA256)
        - set(LEGACY_0_2_0_PRESERVED_PATHS)
    )
    if expected_retired != set(LEGACY_0_2_0_RETIRED_SHA256):
        errors.append("legacy 0.2.0 retirement set is incomplete")
    if (
        len(LEGACY_0_2_0_SURFACE_SHA256) != 30
        or len(LEGACY_0_2_0_RETIRED_SHA256) != 83
        or len(LEGACY_0_2_0_PRESERVED_PATHS) != 12
        or len(historic_paths) != 125
    ):
        errors.append("legacy 0.2.0 pinned member counts changed")
    if (
        SHA256_RE.fullmatch(LEGACY_0_2_0_ARCHIVE_SHA256) is None
        or COMMIT_RE.fullmatch(LEGACY_0_2_0_SOURCE_COMMIT) is None
    ):
        errors.append("legacy 0.2.0 release identity is malformed")
    for label, values in (
        ("legacy surface", LEGACY_0_2_0_SURFACE_SHA256),
        ("legacy retirement", LEGACY_0_2_0_RETIRED_SHA256),
    ):
        if any(SHA256_RE.fullmatch(value) is None for value in values.values()):
            errors.append(f"{label} contains a malformed SHA-256")
    protected = {
        "README.md", "VERSION", ".gate.sha256", "kit.config.json",
        "ARCHITECTURE.md", "arch.rules.json", ".gdlintrc",
    } | set(LEGAL_FILES)
    retired = set(LEGACY_0_2_0_RETIRED_SHA256)
    if retired & protected or any(
        path.startswith(("docs/design/", "docs/retro/", "src/", ".kit/"))
        for path in retired
    ):
        errors.append("legacy retirement includes project or protected state")
    return errors


def _member_content(members: dict[str, ArchiveMember], path: str) -> bytes:
    member = members.get(path)
    if member is None:
        raise ReleaseError(f"install support source is missing: {path}")
    return member.content


def _generated_install_release_files(
        version: str, members: dict[str, ArchiveMember]) -> list[ReleaseFile]:
    manifest = _install_manifest_document(version)
    errors = _definition_errors(manifest)
    if errors:
        raise ReleaseError("; ".join(errors))
    marker = _member_content(members, ".agent-kit.json")
    if marker != b'{"kind":"portable-agent-kit-root","schema":1}\n':
        raise ReleaseError(".agent-kit.json is not the canonical managed marker")
    generated = {
        "install/agents.block.md": _managed_block(
            INSTALL_BLOCK_BEGIN, MANAGED_AGENT_BLOCK_BODY, INSTALL_BLOCK_END
        ),
        "install/copilot.block.md": _managed_block(
            INSTALL_BLOCK_BEGIN, MANAGED_COPILOT_BLOCK_BODY, INSTALL_BLOCK_END
        ),
        "install/gitattributes.block": _managed_block(
            INSTALL_TEXT_BEGIN,
            _member_content(members, ".gitattributes"),
            INSTALL_TEXT_END,
        ),
        "install/gitignore.block": _managed_block(
            INSTALL_TEXT_BEGIN,
            _member_content(members, ".gitignore"),
            INSTALL_TEXT_END,
        ),
        "install/kit": MANAGED_UNIX_LAUNCHER,
        "install/kit.cmd": MANAGED_WINDOWS_LAUNCHER,
        "install/kit.config.default.json": _canonical_json(DEFAULT_KIT_CONFIG),
        INSTALL_MANIFEST_PATH: _canonical_json(manifest),
    }
    return [
        ReleaseFile(path, content, 0o644)
        for path, content in sorted(generated.items())
    ]


def _strict_json_object(content: bytes, label: str) -> dict:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict:
        value: dict = {}
        for key, item in pairs:
            if key in value:
                raise ValueError(f"duplicate key {key!r}")
            value[key] = item
        return value

    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=object_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ReleaseError(f"{label} is not unambiguous UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"{label} must contain one JSON object")
    return value


def _validate_install_bundle(
        version: str, members: dict[str, ArchiveMember]) -> dict | None:
    present = set(members) & GENERATED_INSTALL_FILES
    if not present:
        if _requires_install_support(version):
            raise ReleaseError(
                f"release {version} is missing managed install support"
            )
        return None
    if present != GENERATED_INSTALL_FILES:
        missing = sorted(GENERATED_INSTALL_FILES - present)
        extra = sorted(present - GENERATED_INSTALL_FILES)
        raise ReleaseError(
            f"managed install support is incomplete; missing={missing}, extra={extra}"
        )
    expected = {
        item.path: item
        for item in _generated_install_release_files(version, members)
    }
    for path, expected_file in expected.items():
        member = members[path]
        if member.content != expected_file.content:
            raise ReleaseError(f"managed install source is not canonical: {path}")
        if member.mode != expected_file.mode:
            raise ReleaseError(f"managed install source mode is invalid: {path}")
    install_manifest = _strict_json_object(
        members[INSTALL_MANIFEST_PATH].content, INSTALL_MANIFEST_PATH
    )
    if install_manifest != _install_manifest_document(version):
        raise ReleaseError("managed install manifest contract is not exact")
    return install_manifest


def _archive_kind(path: Path) -> str:
    lower = path.name.lower()
    if lower.endswith(".zip"):
        return "zip"
    if lower.endswith(".tar.gz") or lower.endswith(".tgz"):
        return "tar.gz"
    if lower.endswith(".tar"):
        return "tar"
    raise ReleaseError("output must end in .zip, .tar, .tar.gz, or .tgz")


def _zip_payload(members: dict[str, tuple[bytes, int]]) -> bytes:
    """Return the canonical cross-host ZIP representation of exact members."""
    raw = io.BytesIO()
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
    return raw.getvalue()


def _write_zip(path: Path, members: dict[str, tuple[bytes, int]]) -> None:
    with path.open("wb") as raw:
        raw.write(_zip_payload(members))
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
    source_members = {
        item.path: ArchiveMember(item.path, item.content, item.mode)
        for item in files
    }
    files.extend(_generated_install_release_files(version, source_members))
    files.sort(key=lambda item: item.path)
    if sum(len(item.content) for item in files) > MAX_TOTAL_BYTES:
        raise ReleaseError(f"release content exceeds {MAX_TOTAL_BYTES} bytes")
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


def _read_zip(content: bytes) -> tuple[str, dict[str, ArchiveMember]]:
    members: dict[str, ArchiveMember] = {}
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(content), "r") as archive:
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


def _read_tar(content: bytes, *, name: str) -> tuple[str, dict[str, ArchiveMember]]:
    members: dict[str, ArchiveMember] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as archive:
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
    kind = "tar.gz" if name.lower().endswith((".tar.gz", ".tgz")) else "tar"
    return kind, members


def _read_archive_material(path: Path) -> tuple[str, dict[str, ArchiveMember], bytes]:
    content = _read_regular_bytes(path, label="archive", limit=MAX_ARCHIVE_BYTES)
    if zipfile.is_zipfile(io.BytesIO(content)):
        kind, members = _read_zip(content)
        return kind, members, content
    try:
        kind, members = _read_tar(content, name=path.name)
    except ReleaseError as exc:
        if "invalid tar archive" not in str(exc):
            raise
    else:
        return kind, members, content
    raise ReleaseError("file is neither a readable zip nor tar archive")


def _read_archive(path: Path) -> tuple[str, dict[str, ArchiveMember]]:
    kind, members, _content = _read_archive_material(path)
    return kind, members


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
    version = str(manifest["version"])
    has_install_support = INSTALL_MANIFEST_PATH in paths
    required_files = (
        REQUIRED_KIT_FILES | GENERATED_INSTALL_FILES
        if has_install_support or _requires_install_support(version)
        else LEGACY_0_2_0_REQUIRED_KIT_FILES
    )
    if not required_files.issubset(paths):
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
    install_manifest = _validate_install_bundle(manifest["version"], members)
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
        "install_manifest": install_manifest,
    }


def _canonical_release_sha256(members: dict[str, ArchiveMember]) -> str:
    """Hash the one canonical container for an authenticated member set."""
    payload = _zip_payload({
        name: (member.content, member.mode if member.mode is not None else 0o644)
        for name, member in members.items()
    })
    if len(payload) > MAX_ARCHIVE_BYTES:
        raise ReleaseError(f"canonical release ZIP exceeds {MAX_ARCHIVE_BYTES} bytes")
    return hashlib.sha256(payload).hexdigest()


def _verify_member_set(
        kind: str,
        members: dict[str, ArchiveMember],
        container_sha256: str | None,
) -> tuple[dict, dict[str, ArchiveMember]]:
    """Authenticate one already bounded member set."""
    manifest_member = members.get(MANIFEST_PATH)
    if manifest_member is None:
        raise ReleaseError(f"release is missing {MANIFEST_PATH}")
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
        raise ReleaseError(f"release member set differs from manifest; missing={missing}, extra={extra}")
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
    install_manifest = _validate_install_bundle(manifest["version"], members)
    identity_sha256 = _canonical_release_sha256(members)
    # 0.2.0 predates canonical cross-container identity. Keep the exact known
    # archive bound to its historical identity even if a future ZIP writer
    # changes its canonical representation.
    if container_sha256 == LEGACY_0_2_0_ARCHIVE_SHA256:
        identity_sha256 = LEGACY_0_2_0_ARCHIVE_SHA256
    report = {
        "ok": True,
        "format": kind,
        "version": manifest["version"],
        "source": manifest["source"],
        "authority_evidence": manifest["authority_evidence"],
        "files": len(expected),
        "archive_sha256": identity_sha256,
        "container_sha256": container_sha256,
        "install_schema": (
            install_manifest["install_schema"]
            if install_manifest is not None else None
        ),
    }
    return report, members


def _verified_archive(path: Path) -> tuple[dict, dict[str, ArchiveMember]]:
    """Return a verification report and the exact members it authenticated."""
    kind, members, content = _read_archive_material(path)
    container_sha256 = hashlib.sha256(content).hexdigest()
    return _verify_member_set(kind, members, container_sha256)


def _directory_member(path: Path, relative: str) -> ArchiveMember:
    try:
        before = path.lstat()
    except OSError as exc:
        raise ReleaseError(f"cannot inspect release directory member {relative}: {exc}") from exc
    if (
        stat.S_ISLNK(before.st_mode)
        or _is_reparse_point(path)
        or not stat.S_ISREG(before.st_mode)
        or int(getattr(before, "st_nlink", 1)) != 1
    ):
        raise ReleaseError(f"release directory contains a linked or non-regular file: {relative}")
    if before.st_size > MAX_FILE_BYTES:
        raise ReleaseError(f"release directory member exceeds size limit: {relative}")
    try:
        with path.open("rb") as source:
            opened = os.fstat(source.fileno())
            content = source.read(MAX_FILE_BYTES + 1)
            after = os.fstat(source.fileno())
        after_path = path.lstat()
    except OSError as exc:
        raise ReleaseError(f"cannot read release directory member {relative}: {exc}") from exc
    path_identity = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_nlink")
    handle_identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink")
    if (
        tuple(getattr(before, key, None) for key in path_identity)
        != tuple(getattr(after_path, key, None) for key in path_identity)
        or tuple(getattr(before, key, None) for key in handle_identity)
        != tuple(getattr(opened, key, None) for key in handle_identity)
        or tuple(getattr(opened, key, None) for key in handle_identity)
        != tuple(getattr(after, key, None) for key in handle_identity)
        or _is_reparse_point(path)
        or len(content) != before.st_size
    ):
        raise ReleaseError(f"release directory member changed while reading: {relative}")
    mode = (
        0o755 if relative.endswith(".py") or relative == "kit" else 0o644
    ) if os.name == "nt" else stat.S_IMODE(before.st_mode)
    return ArchiveMember(relative, content, mode)


def _read_release_directory(root: Path) -> dict[str, ArchiveMember]:
    absolute = root.absolute()
    if absolute.is_symlink() or _is_reparse_point(absolute) or not absolute.is_dir():
        raise ReleaseError(f"release directory is not an unredirected directory: {absolute}")
    try:
        resolved = absolute.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ReleaseError(f"cannot resolve release directory: {exc}") from exc
    if resolved != absolute:
        raise ReleaseError(f"release directory uses redirected components: {absolute}")
    members: dict[str, ArchiveMember] = {}
    folded_paths: set[str] = set()
    actual_directories: set[str] = set()
    total = 0
    for current, directories, filenames in os.walk(resolved, topdown=True, followlinks=False):
        base = Path(current)
        relative_base = base.relative_to(resolved)
        names = [*directories, *filenames]
        if len(names) != len({name.casefold() for name in names}):
            raise ReleaseError(f"release directory contains a case collision: {relative_base}")
        for name in sorted(directories):
            child = base / name
            relative = child.relative_to(resolved).as_posix()
            _safe_member_path(relative)
            try:
                info = child.lstat()
            except OSError as exc:
                raise ReleaseError(f"cannot inspect release directory {relative}: {exc}") from exc
            if stat.S_ISLNK(info.st_mode) or _is_reparse_point(child) or not stat.S_ISDIR(info.st_mode):
                raise ReleaseError(f"release directory contains a linked directory: {relative}")
            actual_directories.add(relative)
        for name in sorted(filenames):
            relative = (relative_base / name).as_posix()
            relative = _safe_member_path(relative)
            folded = relative.casefold()
            if folded in folded_paths:
                raise ReleaseError(f"release directory contains a case collision: {relative}")
            folded_paths.add(folded)
            if len(members) >= MAX_MEMBERS:
                raise ReleaseError(f"release directory has more than {MAX_MEMBERS} members")
            member = _directory_member(base / name, relative)
            total += len(member.content)
            if total > MAX_TOTAL_BYTES:
                raise ReleaseError("release directory content exceeds size limit")
            members[relative] = member
    expected_directories: set[str] = set()
    for path in members:
        parts = PurePosixPath(path).parts
        expected_directories.update(
            PurePosixPath(*parts[:index]).as_posix()
            for index in range(1, len(parts))
        )
    if actual_directories != expected_directories:
        missing = sorted(expected_directories - actual_directories)
        extra = sorted(actual_directories - expected_directories)
        raise ReleaseError(
            f"release directory set is not exact; missing={missing}, extra={extra}"
        )
    return members


def read_verified_archive(path: Path) -> tuple[dict, dict[str, ArchiveMember]]:
    """Return authenticated archive metadata and members for the installer.

    Installation and upgrade need the same bounded reader as release
    verification.  Keeping one public entry point prevents a second extractor
    from drifting away from the release allowlist, size limits and hash checks.
    Callers receive immutable in-memory member values and must never use a
    general-purpose ``extractall`` operation.
    """
    return _verified_archive(path)


def read_verified_directory(root: Path) -> tuple[dict, dict[str, ArchiveMember]]:
    """Verify an exact extracted release without Git or the original archive."""
    members = _read_release_directory(root)
    return _verify_member_set("directory", members, None)


def materialize_verified_directory_zip(root: Path, output: Path) -> dict:
    """Write the canonical ZIP for an exact extracted release, once."""
    report, members = read_verified_directory(root)
    output = output.absolute()
    try:
        output.resolve(strict=False).relative_to(root.resolve(strict=True))
    except ValueError:
        pass
    else:
        raise ReleaseError("materialized release ZIP must be outside the verified directory")
    if output.suffix.lower() != ".zip":
        raise ReleaseError("materialized release output must end in .zip")
    parent = output.parent.absolute()
    if not parent.is_dir() or parent.is_symlink() or _is_reparse_point(parent):
        raise ReleaseError("materialized release parent must be an unredirected directory")
    try:
        if parent.resolve(strict=True) != parent:
            raise ReleaseError("materialized release parent uses redirected components")
    except OSError as exc:
        raise ReleaseError(f"cannot resolve materialized release parent: {exc}") from exc
    payload = _zip_payload({
        name: (member.content, member.mode if member.mode is not None else 0o644)
        for name, member in members.items()
    })
    if output.exists():
        if output.is_symlink() or _is_reparse_point(output) or not output.is_file():
            raise ReleaseError("materialized release output is not a regular file")
        try:
            size = output.stat().st_size
            existing = output.read_bytes() if size <= MAX_ARCHIVE_BYTES else b""
        except OSError as exc:
            raise ReleaseError(f"cannot read materialized release output: {exc}") from exc
        if size > MAX_ARCHIVE_BYTES or existing != payload:
            raise ReleaseError("materialized release output already exists with different bytes")
        return verify_archive(output)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(payload)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    verified = verify_archive(output)
    if verified["archive_sha256"] != report["archive_sha256"]:
        raise ReleaseError("materialized release identity changed after writing")
    return verified


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
