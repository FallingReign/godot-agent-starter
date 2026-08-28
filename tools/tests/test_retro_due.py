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


class RetroDueSignalTest(unittest.TestCase):
    def test_closed_consequence_and_prompt_codes_are_deterministic(self) -> None:
        result = retro_due.signals([
            {"path": "b.md", "text": "consequence: native-crash\n"
                                          "retro_trigger: repeated-correction\n"},
            {"path": "a.md", "text": "- consequence: native_crash\n"
                                          "trigger: wrong-built\n"},
        ])

        self.assertEqual(result["immediate_consequences"], [{
            "code": "native-crash", "notes": ["a.md", "b.md"],
        }])
        self.assertEqual(result["prompt_triggers"], [
            {"code": "wrong-built", "notes": ["a.md"]},
            {"code": "repeated-correction", "notes": ["b.md"]},
        ])
        self.assertEqual(result["warnings"], [])

    def test_prose_does_not_infer_a_consequence_and_unknown_code_warns(self) -> None:
        result = retro_due.signals([{
            "path": "note.md",
            "text": "Godot crashed badly.\nconsequence: maybe-crash\n",
        }])

        self.assertEqual(result["immediate_consequences"], [])
        self.assertEqual(result["prompt_triggers"], [])
        self.assertEqual(result["warnings"], [{
            "code": "unknown_retro_signal", "note": "note.md", "value": "maybe-crash",
        }])

    def test_state_exposes_continuous_progress_and_signal_level(self) -> None:
        classified = {
            "immediate_consequences": [],
            "prompt_triggers": [{"code": "wrong-built", "notes": ["one.md"]}],
            "warnings": [],
        }
        with mock.patch.object(retro_due, "unarchived", return_value=[Path("one.md")]), \
                mock.patch.object(retro_due, "threshold", return_value=4), \
                mock.patch.object(retro_due, "signals", return_value=classified), \
                mock.patch.object(retro_due, "verification_consequence", return_value=None), \
                mock.patch.object(Path, "is_dir", return_value=False):
            state = retro_due.state()

        self.assertEqual(state["remaining"], 3)
        self.assertEqual(state["progress"], 0.25)
        self.assertTrue(state["due"])
        self.assertEqual(state["trigger_level"], "prompt")

    def test_native_verification_crash_is_immediate_without_a_slice_note(self) -> None:
        classified = {
            "immediate_consequences": [],
            "prompt_triggers": [],
            "warnings": [],
        }
        crash = {"code": "native-crash", "notes": ["verification:run-1"]}
        with mock.patch.object(retro_due, "unarchived", return_value=[]), \
                mock.patch.object(retro_due, "threshold", return_value=10), \
                mock.patch.object(retro_due, "signals", return_value=classified), \
                mock.patch.object(
                    retro_due, "verification_consequence", return_value=crash
                ), mock.patch.object(Path, "is_dir", return_value=False):
            state = retro_due.state()

        self.assertTrue(state["due"])
        self.assertEqual("immediate", state["trigger_level"])
        self.assertEqual([crash], state["immediate_consequences"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
