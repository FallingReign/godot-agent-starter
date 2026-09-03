#!/usr/bin/env python3
"""Prove non-engine verification cannot accidentally start Godot."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
sys.path.insert(0, str(ROOT))

import bootstrap  # noqa: E402
import check as gate  # noqa: E402
from tools import native_engine  # noqa: E402


def _scratch_parent() -> Path:
    configured = os.environ.get("KIT_TEST_TMPDIR", "").strip()
    return Path(configured) if configured else ROOT / ".checklogs"


class EngineInvocationBoundary(unittest.TestCase):
    def setUp(self) -> None:
        for items in gate.RUN_DIAGNOSTICS.values():
            items.clear()

    def invoke(self, *arguments: str) -> tuple[int, str]:
        output = io.StringIO()
        scratch = _scratch_parent()
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

    def test_engine_timeout_from_shared_boundary_is_recorded_by_the_gate(self) -> None:
        scratch = _scratch_parent()
        scratch.mkdir(exist_ok=True)
        log = scratch / "owned-engine-timeout-test.log"
        result = native_engine.NativeResult(
            exit_code=124,
            output="partial\nTIMEOUT after 1s; owned process tree terminated\n",
            executable="Godot.exe",
            started=True,
            failure_class="engine-timeout",
            failure_code="native-engine-timeout",
            timeout_seconds=1,
        )
        try:
            with mock.patch.object(
                native_engine, "run_godot", return_value=result
            ), mock.patch.object(native_engine, "persist_native_failure") as persist:
                code, output = gate.run(
                    ["Godot.exe", "--headless"], 1, log, native=True
                )

            self.assertEqual(124, code)
            self.assertIn("owned process tree terminated", output)
            self.assertEqual(
                "native-engine-timeout",
                gate.RUN_DIAGNOSTICS["timeouts"][0]["code"],
            )
            persist.assert_called_once()
        finally:
            log.unlink(missing_ok=True)

    def test_native_access_violation_is_visible_and_classified(self) -> None:
        scratch = _scratch_parent()
        scratch.mkdir(exist_ok=True)
        log = scratch / "owned-engine-crash-test.log"
        result = native_engine.NativeResult(
            exit_code=-1073741819,
            output=(
                "engine stopped\nNATIVE ENGINE CRASH: process exited before the "
                "operation completed (exit -1073741819, 0xC0000005).\n"
            ),
            executable="Godot.exe",
            started=True,
            failure_class="native-crash",
            failure_code="native-crash",
            windows_status="0xC0000005",
        )
        try:
            with mock.patch.object(
                native_engine, "run_godot", return_value=result
            ), mock.patch.object(native_engine, "persist_native_failure"):
                code, output = gate.run(
                    ["Godot.exe", "--headless"], 1, log, native=True
                )

            self.assertEqual(-1073741819, code)
            self.assertIn("NATIVE ENGINE CRASH", output)
            self.assertRegex(output, r"0xC0000005")
            self.assertTrue(gate.ERROR_RE.search(output))
            self.assertEqual(
                "native-crash",
                gate.RUN_DIAGNOSTICS["native_crashes"][0]["code"],
            )
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
