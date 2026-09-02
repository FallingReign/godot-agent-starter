#!/usr/bin/env python3
"""Regression tests for kit consumers of the configured game root."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(TOOLS))

import arch  # noqa: E402
import check as gate  # noqa: E402
import design  # noqa: E402
import gen_gdscript_doc  # noqa: E402
import plan_html  # noqa: E402
import project_context  # noqa: E402
import sanitise  # noqa: E402
import managed_launcher  # noqa: E402
import process_supervisor  # noqa: E402


def _canonical_json(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _managed_core(project: Path, relative_files: tuple[str, ...]) -> Path:
    """Create one fully validated managed core from selected source files."""
    marker = {"kind": managed_launcher.MARKER_KIND, "schema": 1}
    (project / managed_launcher.MARKER_NAME).write_bytes(_canonical_json(marker))
    contents: dict[str, bytes] = {
        "INSTALL-MANIFEST.json": b'{"schema":1}\n',
        "LICENSE": b"MIT\n",
        "kit.py": b"#!/usr/bin/env python3\n",
    }
    for relative in relative_files:
        contents[relative] = (ROOT / relative).read_bytes()
    files = []
    for relative, content in sorted(contents.items()):
        mode = 0o755 if relative.endswith(".py") else 0o644
        files.append({
            "path": relative,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "mode": f"{mode:04o}",
        })
    manifest = {
        "schema": managed_launcher.MANIFEST_SCHEMA,
        "version": "0.3.0",
        "source": {"commit": "a" * 40, "dirty": False},
        "authority_evidence": {
            "identity_model": "portable-policy-audit",
            "project_receipt_trust": "not-applicable-no-project-state",
            "receipt_trust": "portable-policy",
        },
        "license_files": ["LICENSE"],
        "normalization": {
            "line_endings": "lf",
            "regular_mode": "0644",
            "executable_mode": "0755",
            "timestamps": "fixed",
        },
        "files": files,
    }
    manifest_content = _canonical_json(manifest)
    archive_members = {
        relative: (
            content,
            0o755 if relative.endswith(".py") else 0o644,
        )
        for relative, content in contents.items()
    }
    archive_members[managed_launcher.MANIFEST_NAME] = (manifest_content, 0o644)
    release_sha = managed_launcher._canonical_archive_sha256(archive_members)
    core = project / ".agent-kit" / "releases" / release_sha
    core.mkdir(parents=True)
    for relative, content in sorted(contents.items()):
        target = core.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        target.chmod(0o755 if relative.endswith(".py") else 0o644)
    (core / managed_launcher.MANIFEST_NAME).write_bytes(manifest_content)
    install_content = contents["INSTALL-MANIFEST.json"]
    active_release = {
        "kit_version": "0.3.0",
        "archive_sha256": release_sha,
        "release_manifest_sha256": hashlib.sha256(manifest_content).hexdigest(),
        "install_manifest_sha256": hashlib.sha256(install_content).hexdigest(),
        "source_commit": "a" * 40,
        "core_path": f".agent-kit/releases/{release_sha}",
    }
    current = {
        "schema": 1,
        "kind": managed_launcher.CURRENT_KIND,
        "installation_id": "1" * 32,
        "install_schema": 1,
        "layout_schema": 1,
        "config_schema": 1,
        "active_release": active_release,
        "previous_release": None,
        "managed_surfaces": [],
        "applied_migrations": [],
    }
    (project / ".agent-kit" / "current.json").write_bytes(_canonical_json(current))
    return core


def _managed_environment(project: Path, core: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment[managed_launcher.PROJECT_ROOT_ENV] = str(project)
    environment[managed_launcher.CORE_ROOT_ENV] = str(core)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["KIT_ENGINE_DISABLED"] = "1"
    return environment


class ConfiguredGameRootConsumers(unittest.TestCase):
    def setUp(self) -> None:
        parent = ROOT / ".checklogs" / "tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.scratch = parent / f"layout-consumers-{uuid.uuid4().hex}"
        self.scratch.mkdir()

    def tearDown(self) -> None:
        resolved = self.scratch.resolve()
        expected = (ROOT / ".checklogs" / "tests").resolve()
        if resolved.parent != expected or not resolved.name.startswith("layout-consumers-"):
            raise AssertionError(f"refusing to remove unexpected scratch: {resolved}")
        shutil.rmtree(resolved)

    def test_architecture_reads_a_root_layout_in_res_space(self) -> None:
        script = self.scratch / "scripts" / "logic" / "root_logic.gd"
        script.parent.mkdir(parents=True)
        script.write_text(
            "class_name RootLogic\nextends RefCounted\n\n\n"
            "func value() -> int:\n\treturn 1\n",
            encoding="utf-8",
        )
        (self.scratch / "project.godot").write_text("[application]\n", encoding="utf-8")

        with mock.patch.object(arch, "PROJECT_DIR", self.scratch):
            files = [path.relative_to(self.scratch).as_posix() for path in arch.source_files()]
            tree = arch.file_tree()

        self.assertEqual(["scripts/logic/root_logic.gd"], files)
        self.assertIn("scripts/logic/root_logic.gd", tree)
        self.assertEqual("RootLogic", tree["scripts/logic/root_logic.gd"]["class_name"])

    def test_sanitiser_reads_resources_from_the_root_layout(self) -> None:
        scene = self.scratch / "scenes" / "root_scene.tscn"
        scene.parent.mkdir()
        scene.write_text(
            '[gd_scene load_steps=2 format=3]\n\n[node name="Root" type="Node"]\n',
            encoding="utf-8",
        )

        with mock.patch.object(sanitise, "PROJECT_DIR", self.scratch):
            files = sanitise.res_files()
            cleaned, notes = sanitise.sanitise_text(
                scene.read_text(encoding="utf-8"), "scenes/root_scene.tscn"
            )

        self.assertEqual([scene], files)
        self.assertNotIn("load_steps", cleaned)
        self.assertTrue(any("removed load_steps" in note for note in notes))

    def test_design_tunables_scan_the_configured_root(self) -> None:
        script = self.scratch / "scripts" / "data" / "root_config.gd"
        script.parent.mkdir(parents=True)
        script.write_text(
            "extends RefCounted\n\n## @tune movement.speed\n"
            "var speed: float = 4.0\n",
            encoding="utf-8",
        )

        with mock.patch.object(
            design, "_configured_game_root", return_value=self.scratch
        ):
            found = design.scan_tunables()

        self.assertIn("movement.speed", found)
        self.assertEqual("speed", found["movement.speed"]["symbol"])

    def test_gdscript_reference_does_not_embed_project_source(self) -> None:
        game_script = self.scratch / "scripts" / "data" / "root_config.gd"
        game_script.parent.mkdir(parents=True)
        game_script.write_text(
            "extends RefCounted\n\nvar speed: float = 4.0\n",
            encoding="utf-8",
        )
        kit_script = self.scratch / "tools" / "internal.gd"
        kit_script.parent.mkdir()
        kit_script.write_text(
            "extends RefCounted\n\nvar leaked: float = 9.0\n",
            encoding="utf-8",
        )

        rendered = gen_gdscript_doc.render(gen_gdscript_doc.collect())

        self.assertIn("var elapsed_seconds: float = 0.0", rendered)
        self.assertNotIn("root_config.gd", rendered)
        self.assertNotIn("tools/internal.gd", rendered)

    def test_plan_filters_kit_files_but_keeps_root_layout_game_files(self) -> None:
        for relative in (
            "scripts/player.gd",
            "content/item.json",
            "README.md",
            "docs/internal.md",
            "tools/internal.py",
            ".kit/runtime/private.txt",
        ):
            path = self.scratch / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture\n", encoding="utf-8")
        context = project_context.ProjectContext(
            kit_root=ROOT,
            project_root=ROOT,
            game_root=ROOT,
            runtime_root=ROOT / ".kit" / "runtime",
            marker_path=ROOT / ".agent-kit.json",
            game_layout=".",
        )

        with mock.patch.object(plan_html, "GAME_ROOT", self.scratch), \
                mock.patch.object(plan_html, "CONTEXT", context):
            files = plan_html.real_files()
            with mock.patch.object(
                plan_html,
                "git",
                side_effect=[
                    (0, "README.md\nscripts/player.gd\n"),
                    (0, "content/new.json\n"),
                ],
            ):
                touched = plan_html.touched_since("a" * 40)
            with mock.patch.object(
                plan_html,
                "git",
                return_value=(0, "README.md\nscripts/player.gd\n"),
            ):
                baseline_files = plan_html.files_at_baseline("a" * 40)
            with mock.patch.object(
                plan_html,
                "git",
                side_effect=[
                    (0, "scripts/player.gd\n"),
                    (1, "fatal: untracked enumeration failed\n"),
                ],
            ):
                failed_untracked = plan_html.touched_since("a" * 40)

        self.assertEqual(
            {"content/item.json", "scripts/player.gd", "tools/internal.py"},
            files,
        )
        self.assertEqual({"content/new.json", "scripts/player.gd"}, touched)
        self.assertEqual({"scripts/player.gd"}, baseline_files)
        self.assertIsNone(failed_untracked)

    def test_conformance_maps_git_paths_for_both_supported_layouts(self) -> None:
        with mock.patch.object(gate, "GAME_LAYOUT", "src"):
            self.assertEqual(
                "scripts/main.gd",
                gate.repository_game_relative("src/scripts/main.gd"),
            )
            self.assertIsNone(gate.repository_game_relative("README.md"))
        with mock.patch.object(gate, "GAME_LAYOUT", "."):
            self.assertEqual(
                "scripts/main.gd",
                gate.repository_game_relative("scripts/main.gd"),
            )
            self.assertEqual(
                "src/scripts/main.gd",
                gate.repository_game_relative("src/scripts/main.gd"),
            )
            self.assertIsNone(gate.repository_game_relative("../outside"))

    def test_managed_gate_uses_release_integrity_and_private_project_runtime(self) -> None:
        project = self.scratch / "managed-gate"
        game = project / "game" / "client"
        game.mkdir(parents=True)
        (game / "project.godot").write_text("[application]\n", encoding="utf-8")
        (project / "kit.config.json").write_text(
            json.dumps({
                "schema": 1,
                "game_root": "game/client",
                "runtime_root": ".kit/runtime",
                "note_threshold": 10,
            }),
            encoding="utf-8",
        )
        core = _managed_core(project, (
            "check.py",
            "dependencies.lock.json",
            "tools/managed_launcher.py",
            "tools/project_context.py",
        ))
        environment = _managed_environment(project, core)

        checked = subprocess.run(
            process_supervisor.isolated_python_script_command(
                sys.executable, core / "check.py", core, "--only", "integrity"
            ),
            cwd=project,
            env=process_supervisor.isolated_python_environment(base=environment),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        accepted = subprocess.run(
            process_supervisor.isolated_python_script_command(
                sys.executable,
                core / "check.py",
                core,
                "--accept-gate-changes",
            ),
            cwd=project,
            env=process_supervisor.isolated_python_environment(base=environment),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        self.assertEqual(0, checked.returncode, checked.stdout + checked.stderr)
        self.assertIn("active managed release manifest verified", checked.stdout)
        self.assertTrue(
            (project / ".kit" / "runtime" / "verification" / "runs"
             / "run-summary.json").is_file()
        )
        self.assertFalse((project / ".gate.sha256").exists())
        self.assertEqual(2, accepted.returncode)
        self.assertIn("managed release integrity is immutable", accepted.stdout)

    def test_managed_architecture_reads_an_arbitrary_safe_game_root(self) -> None:
        project = self.scratch / "managed-architecture"
        game = project / "products" / "gameplay"
        logic = game / "scripts" / "logic"
        logic.mkdir(parents=True)
        (game / "project.godot").write_text("[application]\n", encoding="utf-8")
        (logic / "probe.gd").write_text(
            "class_name ManagedProbe\nextends RefCounted\n",
            encoding="utf-8",
        )
        (project / "kit.config.json").write_text(
            json.dumps({
                "schema": 1,
                "game_root": "products/gameplay",
                "runtime_root": ".kit/runtime",
            }),
            encoding="utf-8",
        )
        (project / "arch.rules.json").write_text(
            json.dumps({"module_depth": 2, "modules": {}}), encoding="utf-8"
        )
        core = _managed_core(project, (
            "arch.py",
            "tools/gd_signature.py",
            "tools/managed_launcher.py",
            "tools/project_context.py",
        ))

        result = subprocess.run(
            process_supervisor.isolated_python_script_command(
                sys.executable, core / "arch.py", core, "--json"
            ),
            cwd=project,
            env=process_supervisor.isolated_python_environment(
                base=_managed_environment(project, core)
            ),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertIn("scripts/logic", payload["modules"])
        self.assertIn("scripts/logic/probe.gd", payload["tree"])

    def test_managed_doctor_reports_an_arbitrary_safe_game_root(self) -> None:
        project = self.scratch / "managed-doctor"
        game = project / "existing" / "godot-project"
        game.mkdir(parents=True)
        (game / "project.godot").write_text(
            '[application]\nconfig/name="Existing Game"\n', encoding="utf-8"
        )
        (project / "kit.config.json").write_text(
            json.dumps({
                "schema": 1,
                "game_root": "existing/godot-project",
                "runtime_root": ".kit/runtime",
            }),
            encoding="utf-8",
        )
        core = _managed_core(project, (
            "bootstrap.py",
            "dependencies.lock.json",
            "tools/engine_discovery.py",
            "tools/managed_launcher.py",
            "tools/native_engine.py",
            "tools/process_supervisor.py",
            "tools/project_context.py",
        ))

        result = subprocess.run(
            process_supervisor.isolated_python_script_command(
                sys.executable, core / "bootstrap.py", core, "--json"
            ),
            cwd=project,
            env=process_supervisor.isolated_python_environment(
                base=_managed_environment(project, core)
            ),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

        self.assertIn(result.returncode, (0, 1), result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        context_result = next(
            item for item in payload["results"] if item["name"] == "kit-context"
        )
        self.assertEqual("OK", context_result["state"])
        self.assertIn("game=existing/godot-project", context_result["detail"])


class ProviderGovernanceBridge(unittest.TestCase):
    def test_copilot_bridge_fail_closes_and_game_builder_cannot_edit_it(self) -> None:
        bridge = (ROOT / ".github" / "copilot-instructions.md").read_text(
            encoding="utf-8"
        )
        persona = (ROOT / ".github" / "agents" / "game-builder.agent.md").read_text(
            encoding="utf-8"
        )
        shared = (ROOT / "AGENTS.md").read_text(encoding="utf-8")

        self.assertIn("@../AGENTS.md", bridge)
        self.assertIn("cannot load `AGENTS.md`, stop", bridge)
        self.assertIn("no implementation may", bridge)
        self.assertIn(".github/copilot-instructions.md", persona)
        self.assertIn(".github/copilot-instructions.md", shared)

    def test_portable_policy_evidence_is_not_described_as_person_authentication(self) -> None:
        documents = {
            path: (ROOT / path).read_text(encoding="utf-8")
            for path in (
                "AGENTS.md",
                "README.md",
                "VERIFY.md",
                "docs/DESIGN.md",
                "tools/plan_html.py",
            )
        }
        combined = "\n".join(documents.values()).lower()

        self.assertNotIn("authenticated review cockpit", combined)
        self.assertNotIn("authenticated served page", combined)
        self.assertIn("not proof that a person's identity was authenticated", combined)
        self.assertIn("not person authentication", combined)
        self.assertIn("local-audit-matched", documents["AGENTS.md"])


if __name__ == "__main__":
    unittest.main()
