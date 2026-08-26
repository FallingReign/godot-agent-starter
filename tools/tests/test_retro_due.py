#!/usr/bin/env python3
"""Contract tests for the deterministic retrospective trigger."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

import retro_due  # noqa: E402


class RetroDueConfigTest(unittest.TestCase):
    def test_threshold_comes_from_unified_kit_config(self) -> None:
        with mock.patch.object(
            retro_due.runtime_paths,
            "load_config",
            return_value={"schema": 1, "note_threshold": 7},
        ):
            self.assertEqual(7, retro_due.threshold())

    def test_missing_threshold_uses_documented_default(self) -> None:
        with mock.patch.object(
            retro_due.runtime_paths, "load_config", return_value={"schema": 1}
        ):
            self.assertEqual(retro_due.DEFAULT_THRESHOLD, retro_due.threshold())

    def test_invalid_threshold_is_rejected_instead_of_silently_changed(self) -> None:
        for value in (0, -1, True, "10"):
            with self.subTest(value=value), mock.patch.object(
                retro_due.runtime_paths,
                "load_config",
                return_value={"schema": 1, "note_threshold": value},
            ):
                with self.assertRaises(retro_due.runtime_paths.RuntimeConfigError):
                    retro_due.threshold()


if __name__ == "__main__":
    unittest.main(verbosity=2)
