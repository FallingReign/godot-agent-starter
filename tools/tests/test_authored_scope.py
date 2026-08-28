#!/usr/bin/env python3
"""Regression tests for inverse authored game-file scoping."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import authored_scope  # noqa: E402


class AuthoredScopeTests(unittest.TestCase):
    def test_unknown_content_extensions_and_extensionless_files_are_authored(self) -> None:
        for relative in ("content/world.yaml", "content/table.toml", "content/MAPDATA"):
            self.assertTrue(
                authored_scope.is_authored_game_file(relative, game_layout="src")
            )

    def test_generated_private_and_third_party_surfaces_are_excluded(self) -> None:
        for relative in (
            "art/icon.png.import",
            "scripts/player.gd.uid",
            ".godot/editor/state",
            ".kit/runtime/private.json",
            "addons/gut/plugin.gd",
            "project.godot",
            "export_presets.cfg",
            ".kit-maintainer-fixture",
        ):
            self.assertFalse(
                authored_scope.is_authored_game_file(relative, game_layout="src"),
                relative,
            )

    def test_project_tests_are_authored_implementation_files(self) -> None:
        self.assertTrue(
            authored_scope.is_authored_game_file(
                "tests/unit/test_player.gd", game_layout="src"
            )
        )

    def test_game_owned_tools_are_authored_except_the_exact_kit_fixture(self) -> None:
        for relative in (
            "tools/build_map.gd",
            "tools/dialogue_importer.gd",
            "tools/CONTENT_PIPELINE",
        ):
            self.assertTrue(
                authored_scope.is_authored_game_file(relative, game_layout="src"),
                relative,
            )
        self.assertFalse(
            authored_scope.is_authored_game_file(
                "tools/validate_resources.gd",
                game_layout="src",
            )
        )

    def test_root_layout_still_uses_the_kit_allowlist_for_kit_tools(self) -> None:
        self.assertFalse(
            authored_scope.is_authored_game_file(
                "tools/cockpit.py",
                game_layout=".",
                is_kit_file=lambda value: value == "tools/cockpit.py",
            )
        )
        self.assertTrue(
            authored_scope.is_authored_game_file(
                "tools/build_map.gd",
                game_layout=".",
                is_kit_file=lambda _value: False,
            )
        )

    def test_root_layout_excludes_kit_surfaces_through_the_release_policy(self) -> None:
        self.assertFalse(
            authored_scope.is_authored_game_file(
                "custom-kit-file.txt",
                game_layout=".",
                is_kit_file=lambda value: value == "custom-kit-file.txt",
            )
        )
        self.assertFalse(
            authored_scope.is_authored_game_file("docs/GATE.md", game_layout=".")
        )
        self.assertTrue(
            authored_scope.is_authored_game_file("content/world.dat", game_layout=".")
        )

    def test_unsafe_or_noncanonical_paths_are_rejected(self) -> None:
        for relative in ("../outside", "/absolute", "C:/drive", "a//b", "./a"):
            self.assertFalse(
                authored_scope.is_authored_game_file(relative, game_layout="src"),
                relative,
            )

    def test_directory_normalization_is_exact(self) -> None:
        self.assertEqual(
            authored_scope.normalize_directory("scripts/logic"), "scripts/logic"
        )
        self.assertEqual(authored_scope.normalize_directory("(root)"), "(root)")
        for relative in ("", ".", "../outside", "scripts\\logic", "scripts/logic/"):
            self.assertIsNone(authored_scope.normalize_directory(relative), relative)


if __name__ == "__main__":
    unittest.main()
