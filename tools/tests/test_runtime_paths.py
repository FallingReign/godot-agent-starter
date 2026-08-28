from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Iterator
from unittest import mock

from tools import runtime_paths


@contextlib.contextmanager
def _scratch() -> Iterator[Path]:
    repo = Path(__file__).resolve().parents[2]
    configured = os.environ.get("KIT_TEST_TMPDIR")
    candidates = ([Path(configured)] if configured else []) + [
        repo / ".checklogs" / "tests", Path(tempfile.gettempdir()), Path("/tmp")
    ]
    parent = None
    for candidate in candidates:
        probe = candidate / f"runtime-probe-{uuid.uuid4().hex}"
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
        raise RuntimeError("no writable runtime-path test scratch directory")
    root = parent / f"runtime-{uuid.uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        if root.resolve().parent == parent:
            shutil.rmtree(root, ignore_errors=True)


def _configure(root: Path, runtime: str = ".kit/runtime", schema: int = 1) -> None:
    (root / "kit.config.json").write_text(
        json.dumps({"schema": schema, "runtime_root": runtime}), encoding="utf-8"
    )


class RuntimePathsTests(unittest.TestCase):

    def test_resolves_private_project_local_runtime(self) -> None:
        with _scratch() as root:
            _configure(root)
            paths = runtime_paths.resolve(root)
            self.assertEqual(paths.runtime, (root / ".kit" / "runtime").resolve())
            self.assertFalse(paths.runtime.exists())

    def test_create_only_makes_runtime_directories(self) -> None:
        with _scratch() as root:
            _configure(root, ".kit/private")
            paths = runtime_paths.resolve(root, create=True)
            self.assertTrue(paths.session_evidence.is_dir())
            self.assertTrue(paths.board_runs.is_dir())
            self.assertTrue(paths.verification_runs.is_dir())
            self.assertTrue(paths.plan_decisions.is_dir())
            self.assertEqual(
                paths.verification_latest,
                root / ".kit" / "private" / "verification" / "latest.json",
            )
            self.assertTrue(paths.dispatch_workspaces.is_dir())
            self.assertFalse((root / "docs").exists())

    def test_rejects_absolute_traversal_and_project_root(self) -> None:
        with _scratch() as root:
            for bad in (
                "../elsewhere", str(root.resolve()), ".", ".kit",
                "state/private", "src/runtime", "docs/private",
            ):
                with self.subTest(bad=bad):
                    _configure(root, bad)
                    with self.assertRaises(runtime_paths.RuntimeConfigError):
                        runtime_paths.resolve(root)

    def test_rejects_missing_or_unknown_config(self) -> None:
        with _scratch() as root:
            with self.assertRaises(runtime_paths.RuntimeConfigError):
                runtime_paths.resolve(root)
            _configure(root, schema=99)
            with self.assertRaises(runtime_paths.RuntimeConfigError):
                runtime_paths.resolve(root)

    def test_existing_runtime_components_cannot_redirect_or_be_files(self) -> None:
        with _scratch() as root:
            _configure(root)
            (root / ".kit").write_text("not a directory", encoding="utf-8")
            with self.assertRaisesRegex(
                runtime_paths.RuntimeConfigError, "unredirected directory"
            ):
                runtime_paths.resolve(root)

        with _scratch() as root:
            _configure(root)
            (root / ".kit").mkdir()
            with mock.patch.object(runtime_paths, "_is_reparse", return_value=True):
                with self.assertRaisesRegex(
                    runtime_paths.RuntimeConfigError, "unredirected directory"
                ):
                    runtime_paths.resolve(root)


if __name__ == "__main__":
    unittest.main()
