#!/usr/bin/env python3
"""Prove non-engine verification cannot accidentally start Godot."""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
sys.path.insert(0, str(ROOT))

import bootstrap  # noqa: E402
import check as gate  # noqa: E402


class EngineInvocationBoundary(unittest.TestCase):
    def invoke(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        scratch = ROOT / ".checklogs"
        scratch.mkdir(exist_ok=True)
        with mock.patch.object(gate, "RESULTS", gate.Results()), \
                mock.patch.object(gate, "LOG_DIR", scratch), \
                mock.patch.object(gate, "retro_nudge"), \
                mock.patch.object(sys, "argv", ["check.py", *arguments]), \
                contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = gate.main()
        return code, output.getvalue()

    def test_integrity_only_never_discovers_or_launches_godot(self) -> None:
        def pass_integrity() -> None:
            gate.RESULTS.passed("integrity")

        with mock.patch.object(
            gate, "stage_integrity", side_effect=pass_integrity
        ), mock.patch.object(
            gate, "find_godot", side_effect=AssertionError("Godot discovery escaped")
        ), mock.patch.object(
            gate, "run", side_effect=AssertionError("a process was launched")
        ):
            code, output = self.invoke("--only", "integrity")

        self.assertEqual(0, code, output)
        self.assertIn("not discovered or launched", output)

    def test_integrity_failure_stops_before_every_other_stage(self) -> None:
        def fail_integrity() -> None:
            gate.RESULTS.fail("integrity")

        with mock.patch.object(
            gate, "stage_integrity", side_effect=fail_integrity
        ), mock.patch.object(gate, "stage_typecheck") as typecheck, \
                mock.patch.object(gate, "find_godot") as find_godot:
            code, output = self.invoke("--only", "integrity,typecheck")

        self.assertEqual(1, code)
        self.assertIn("SKIP  typecheck (skipped: integrity review required)", output)
        typecheck.assert_not_called()
        find_godot.assert_not_called()

    def test_failed_engine_health_check_cannot_cascade_into_more_launches(self) -> None:
        with mock.patch.dict(os.environ, {"KIT_ENGINE_DISABLED": "0"}), \
                mock.patch.object(gate, "find_godot", return_value="Godot.exe"), \
                mock.patch.object(gate, "run", return_value=(1, "application error")) as run, \
                mock.patch.object(gate, "stage_typecheck") as typecheck, \
                mock.patch.object(gate, "stage_gut") as gut:
            code, output = self.invoke("--only", "typecheck,gut")

        self.assertEqual(1, code)
        run.assert_called_once()
        typecheck.assert_not_called()
        gut.assert_not_called()
        self.assertIn("previous native-engine check failed", output)

    def test_first_failed_engine_stage_stops_every_later_engine_stage(self) -> None:
        def fail_typecheck(_godot: str) -> None:
            gate.RESULTS.fail("typecheck")

        with mock.patch.dict(os.environ, {"KIT_ENGINE_DISABLED": "0"}), \
                mock.patch.object(gate, "find_godot", return_value="Godot.exe"), \
                mock.patch.object(gate, "run", return_value=(0, "4.7.2.stable")), \
                mock.patch.object(
                    gate, "stage_typecheck", side_effect=fail_typecheck
                ) as typecheck, mock.patch.object(gate, "stage_gut") as gut:
            code, output = self.invoke("--only", "typecheck,gut")

        self.assertEqual(1, code)
        typecheck.assert_called_once_with("Godot.exe")
        gut.assert_not_called()
        self.assertIn("gut (skipped: previous native-engine check failed)", output)

    def test_wrong_engine_patch_is_a_failure_and_stops_native_stages(self) -> None:
        with mock.patch.dict(os.environ, {"KIT_ENGINE_DISABLED": "0"}), \
                mock.patch.object(gate, "find_godot", return_value="Godot.exe"), \
                mock.patch.object(gate, "run", return_value=(0, "4.7.1.stable")), \
                mock.patch.object(gate, "stage_typecheck") as typecheck:
            code, output = self.invoke("--only", "typecheck")

        self.assertEqual(1, code)
        typecheck.assert_not_called()
        self.assertIn("unsupported engine version 4.7.1", output)
        self.assertIn("typecheck (skipped: previous native-engine check failed)", output)

    def test_engine_timeout_terminates_the_owned_process_tree(self) -> None:
        scratch = ROOT / ".checklogs"
        scratch.mkdir(exist_ok=True)
        log = scratch / "owned-engine-timeout-test.log"
        process = mock.Mock()
        process.communicate.side_effect = (
            subprocess.TimeoutExpired(["Godot.exe"], 1, output=b"partial"),
            (b"partial", None),
        )
        process.returncode = None
        try:
            with mock.patch.object(
                gate, "_acquire_engine_lock", return_value=("owned-token", "")
            ), mock.patch.object(
                gate, "_release_engine_lock"
            ) as release, mock.patch.object(
                gate, "_start_owned_engine_process", return_value=process
            ), mock.patch.object(
                gate, "_terminate_owned_process_tree"
            ) as terminate:
                code, output = gate.run(["Godot.exe", "--headless"], 1, log)

            self.assertEqual(124, code)
            self.assertIn("owned process tree terminated", output)
            terminate.assert_called_once_with(process)
            release.assert_called_once_with("owned-token")
        finally:
            log.unlink(missing_ok=True)

    def test_caller_can_enforce_a_no_engine_process_boundary(self) -> None:
        with mock.patch.dict(os.environ, {"KIT_ENGINE_DISABLED": "1"}), \
                mock.patch.object(
                    gate, "find_godot", side_effect=AssertionError("Godot discovered")
                ), mock.patch.object(
                    gate, "run", side_effect=AssertionError("a process was launched")
                ):
            code, output = self.invoke("--only", "typecheck")

        self.assertEqual(1, code)
        self.assertIn("native engine disabled by caller", output)
        self.assertIn("not discovered or launched", output)

    def test_bootstrap_diagnosis_locates_but_never_executes_godot(self) -> None:
        records: list[dict] = []
        with mock.patch.object(bootstrap, "results", records), mock.patch.object(
            bootstrap, "find_godot", return_value="C:/Godot/Godot.exe"
        ), mock.patch.object(
            bootstrap, "run", side_effect=AssertionError("diagnosis launched Godot")
        ):
            binary = bootstrap.check_godot()

        self.assertEqual("C:/Godot/Godot.exe", binary)
        self.assertEqual("godot", records[0]["name"])
        self.assertIn("not launched during diagnosis", records[0]["detail"])

    def test_bootstrap_rejects_a_known_wrong_version_without_launching_it(self) -> None:
        records: list[dict] = []
        stale = "C:/Godot/Godot_v4.7.1-stable_win64.exe"
        with mock.patch.dict(os.environ, {"GODOT_BIN": stale}, clear=False), \
                mock.patch.object(bootstrap, "results", records), \
                mock.patch.object(bootstrap, "find_godot", return_value=stale), \
                mock.patch.object(
                    bootstrap,
                    "run",
                    side_effect=AssertionError("diagnosis launched Godot"),
                ):
            binary = bootstrap.check_godot()

        self.assertIsNone(binary)
        self.assertEqual(bootstrap.MANUAL, records[0]["state"])
        self.assertIn("selected 4.7.1", records[0]["detail"])
        self.assertIn("exact 4.7.2 required", records[0]["detail"])

    def test_bootstrap_reports_when_stale_environment_was_safely_replaced(self) -> None:
        records: list[dict] = []
        stale = "C:/Godot/Godot_v4.7.1-stable_win64.exe"
        exact = "C:/Godot/Godot_v4.7.2-stable_win64_console.exe"
        with mock.patch.dict(os.environ, {"GODOT_BIN": stale}, clear=False), \
                mock.patch.object(bootstrap, "results", records), \
                mock.patch.object(bootstrap, "find_godot", return_value=exact), \
                mock.patch.object(
                    bootstrap,
                    "run",
                    side_effect=AssertionError("diagnosis launched Godot"),
                ):
            binary = bootstrap.check_godot()

        self.assertEqual(exact, binary)
        self.assertEqual(bootstrap.OK, records[0]["state"])
        self.assertIn("ignored stale GODOT_BIN (4.7.1)", records[0]["detail"])

    def test_all_native_boundaries_share_the_exact_engine_patch(self) -> None:
        self.assertEqual(
            gate.EXPECTED_VERSION,
            bootstrap.engine_discovery.EXPECTED_GODOT_VERSION,
        )

    def test_schema_failure_routes_remediation_through_public_launcher(self) -> None:
        output = io.StringIO()
        with mock.patch.object(gate, "RESULTS", gate.Results()), mock.patch.object(
            gate, "run", return_value=(1, "error: invalid proposal fixture")
        ), contextlib.redirect_stdout(output):
            gate.stage_schema()

        rendered = output.getvalue()
        self.assertIn("kit schema describe proposal", rendered)
        self.assertNotIn("python tools/schema.py", rendered)


if __name__ == "__main__":
    unittest.main()
