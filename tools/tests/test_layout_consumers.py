#!/usr/bin/env python3
"""Regression tests for kit consumers of the configured game root."""
from __future__ import annotations

import shutil
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

        with mock.patch.object(design, "GAME_ROOT", self.scratch):
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
