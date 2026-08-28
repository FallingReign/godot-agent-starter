#!/usr/bin/env python3
"""Contract tests for read-only, exact-version Godot selection."""
from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import types
import unittest
import uuid
from pathlib import Path
from typing import Iterator
from unittest import mock

import sys

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import engine_discovery  # noqa: E402


@contextlib.contextmanager
def _scratch() -> Iterator[Path]:
    configured = os.environ.get("KIT_TEST_TMPDIR")
    candidates = ([Path(configured)] if configured else []) + [
        TOOLS.parent / ".checklogs" / "tests",
        Path(tempfile.gettempdir()),
        Path("/tmp"),
    ]
    parent = None
    for candidate in candidates:
        probe = candidate / f"engine-discovery-probe-{uuid.uuid4().hex}"
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe.mkdir()
            (probe / "write-check").write_text("ok", encoding="utf-8")
            shutil.rmtree(probe)
            parent = candidate.resolve()
            break
        except OSError:
            shutil.rmtree(probe, ignore_errors=True)
    if parent is None:
        raise RuntimeError("no writable engine-discovery test scratch directory")
    directory = parent / f"engine-discovery-{uuid.uuid4().hex}"
    directory.mkdir()
    try:
        yield directory
    finally:
        if directory.parent == parent:
            shutil.rmtree(directory, ignore_errors=True)


class EngineDiscoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = _scratch()
        self.base = self.temporary.__enter__()
        self.root = self.base / "project"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary.__exit__(None, None, None)

    @staticmethod
    def _which(mapping: dict[str, Path]):
        return lambda name: str(mapping[name]) if name in mapping else None

    @staticmethod
    def _file(path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
        return path

    def test_stale_environment_selection_falls_forward_to_exact_sibling(self) -> None:
        stale = self._file(
            self.base / "Godot_v4.7.1-stable_win64.exe"
        )
        exact = self._file(
            self.base / "Godot_v4.7.2-stable_win64_console.exe"
        )

        selected = engine_discovery.select_godot(
            self.root,
            environment={"GODOT_BIN": str(stale)},
            system_name="Windows",
            which=self._which({}),
        )

        self.assertEqual(exact.resolve(), selected.path)
        self.assertEqual("environment-sibling", selected.source)
        self.assertEqual("4.7.1", selected.requested_version)
        self.assertEqual("4.7.2", selected.selected_version)
        self.assertTrue(selected.replaced_stale_request)
        self.assertFalse(selected.known_mismatch)

    def test_console_binary_is_preferred_to_gui_binary(self) -> None:
        gui = self._file(
            self.base / "Godot_v4.7.2-stable_win64.exe"
        )
        console = self._file(
            self.base / "Godot_v4.7.2-stable_win64_console.exe"
        )

        selected = engine_discovery.select_godot(
            self.root,
            environment={},
            system_name="Windows",
            which=self._which({}),
        )

        self.assertEqual(console.resolve(), selected.path)
        self.assertNotEqual(gui.resolve(), selected.path)

    def test_console_on_path_is_preferred_to_adjacent_gui_binary(self) -> None:
        gui = self._file(
            self.base / "Godot_v4.7.2-stable_win64.exe"
        )
        console = self._file(
            self.base / "bin" / "Godot_v4.7.2-stable_win64_console.exe"
        )

        selected = engine_discovery.select_godot(
            self.root,
            environment={},
            system_name="Windows",
            which=self._which({console.name: console}),
        )

        self.assertEqual(console.resolve(), selected.path)
        self.assertNotEqual(gui.resolve(), selected.path)
        self.assertEqual("path-exact", selected.source)

    def test_matching_explicit_binary_remains_authoritative(self) -> None:
        explicit = self._file(
            self.root / "tools" / "Godot_v4.7.2-stable_win64.exe"
        )
        adjacent = self._file(
            self.base / "Godot_v4.7.2-stable_win64_console.exe"
        )

        selected = engine_discovery.select_godot(
            self.root,
            environment={"GODOT_BIN": str(explicit)},
            system_name="Windows",
            which=self._which({}),
        )

        self.assertEqual(explicit.resolve(), selected.path)
        self.assertNotEqual(adjacent.resolve(), selected.path)
        self.assertEqual("environment", selected.source)

    def test_unversioned_explicit_command_is_preserved_for_native_probe(self) -> None:
        generic = self._file(self.base / "bin" / "godot.exe")

        selected = engine_discovery.select_godot(
            self.root,
            environment={"GODOT_BIN": "godot.exe"},
            system_name="Windows",
            which=self._which({"godot.exe": generic}),
        )

        self.assertEqual(generic.resolve(), selected.path)
        self.assertIsNone(selected.selected_version)
        self.assertFalse(selected.known_mismatch)

    def test_known_mismatch_is_returned_when_no_exact_replacement_exists(self) -> None:
        stale = self._file(
            self.root / "Godot_v4.7.1-stable_win64.exe"
        )

        selected = engine_discovery.select_godot(
            self.root,
            environment={"GODOT_BIN": str(stale)},
            system_name="Windows",
            which=self._which({}),
        )

        self.assertEqual(stale.resolve(), selected.path)
        self.assertTrue(selected.known_mismatch)
        self.assertFalse(selected.replaced_stale_request)

    def test_invalid_environment_path_can_recover_to_adjacent_exact_binary(self) -> None:
        missing = self.base / "Godot_v4.7.1-stable_win64.exe"
        exact = self._file(
            self.base / "Godot_v4.7.2-stable_win64_console.exe"
        )

        selected = engine_discovery.select_godot(
            self.root,
            environment={"GODOT_BIN": str(missing)},
            system_name="Windows",
            which=self._which({}),
        )

        self.assertEqual(exact.resolve(), selected.path)
        self.assertTrue(selected.replaced_stale_request)

    def test_exact_version_on_path_precedes_generic_command(self) -> None:
        exact = self._file(
            self.base / "bin" / "Godot_v4.7.2-stable_linux.x86_64"
        )
        generic = self._file(self.base / "bin" / "godot")

        selected = engine_discovery.select_godot(
            self.root,
            environment={},
            system_name="Linux",
            which=self._which({
                "Godot_v4.7.2-stable_linux.x86_64": exact,
                "godot": generic,
            }),
        )

        self.assertEqual(exact.resolve(), selected.path)
        self.assertEqual("path-exact", selected.source)

    def test_missing_engine_is_an_explicit_result(self) -> None:
        selected = engine_discovery.select_godot(
            self.root,
            environment={},
            system_name="Windows",
            which=self._which({}),
        )

        self.assertIsNone(selected.path)
        self.assertEqual("not-found", selected.source)

    def test_unversioned_candidate_requires_engine_reported_exact_version(self) -> None:
        generic = self._file(self.base / "bin" / "godot.exe")
        selection = engine_discovery.EngineSelection(
            generic.resolve(), "path-generic", selected_version=None
        )
        result = types.SimpleNamespace(
            exit_code=0,
            output="4.7.2.stable.official.fixture\n",
            failure_class=None,
        )
        runner = mock.Mock(return_value=result)

        authenticated = engine_discovery.authenticate_godot(
            self.root,
            selection=selection,
            operation="gdls-start",
            runner=runner,
        )

        self.assertEqual(generic.resolve(), authenticated.path)
        self.assertEqual("4.7.2", authenticated.version)
        runner.assert_called_once_with(
            generic.resolve(),
            ["--headless", "--version"],
            root=self.root.resolve(),
            cwd=self.root.resolve(),
            timeout=30,
        )

    def test_unversioned_non_engine_is_rejected_after_only_the_probe(self) -> None:
        candidate = self._file(self.base / "unversioned-engine-candidate")
        selection = engine_discovery.EngineSelection(
            candidate, "environment", selected_version=None
        )
        result = types.SimpleNamespace(
            exit_code=2,
            output="unknown option --headless\n",
            failure_class=None,
        )
        runner = mock.Mock(return_value=result)

        with self.assertRaises(engine_discovery.EngineAuthenticationError) as caught:
            engine_discovery.authenticate_godot(
                self.root,
                selection=selection,
                operation="setup-import",
                runner=runner,
            )

        self.assertEqual("engine_probe_failed", caught.exception.status)
        self.assertEqual(1, runner.call_count)

    def test_known_filename_mismatch_is_rejected_without_a_probe(self) -> None:
        stale = self._file(self.base / "Godot_v4.7.1-stable_win64.exe")
        selection = engine_discovery.EngineSelection(
            stale.resolve(), "environment", selected_version="4.7.1"
        )
        runner = mock.Mock()

        with self.assertRaises(engine_discovery.EngineAuthenticationError) as caught:
            engine_discovery.authenticate_godot(
                self.root,
                selection=selection,
                operation="godot-docs-build",
                runner=runner,
            )

        self.assertEqual("engine_version_mismatch", caught.exception.status)
        runner.assert_not_called()

    def test_ambiguous_or_missing_reported_version_fails_closed(self) -> None:
        generic = self._file(self.base / "bin" / "Godot.exe")
        selection = engine_discovery.EngineSelection(
            generic.resolve(), "path-generic", selected_version=None
        )
        for output in ("Godot Engine\n", "4.7.2 and 4.7.1\n"):
            with self.subTest(output=output):
                result = types.SimpleNamespace(
                    exit_code=0,
                    output=output,
                    failure_class=None,
                )
                with self.assertRaises(
                    engine_discovery.EngineAuthenticationError
                ) as caught:
                    engine_discovery.authenticate_godot(
                        self.root,
                        selection=selection,
                        operation="gdls-start",
                        runner=mock.Mock(return_value=result),
                    )
                self.assertEqual("engine_version_unknown", caught.exception.status)


if __name__ == "__main__":
    unittest.main(verbosity=2)
