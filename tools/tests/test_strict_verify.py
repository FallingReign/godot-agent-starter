#!/usr/bin/env python3
"""Synthetic orchestration tests for strict verification.

No test in this module starts Godot, a browser, the real gate, or a network
operation.  The fake runner makes ordering and fail-closed interpretation
observable without creating the native crash popups this verifier is intended
to keep serialized.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import unittest
import uuid
from pathlib import Path
from unittest import mock

from tools import strict_verify

REPOSITORY = Path(__file__).resolve().parents[2]


def _doctor(*, complete: bool = True) -> str:
    state = "OK" if complete else "MISSING"
    return json.dumps(
        {
            "ok": complete,
            "exit_code": 0 if complete else 3,
            "status": "ready" if complete else "setup_incomplete",
            "blocking": [] if complete else ["gdtoolkit"],
            "bootstrap": {
                "complete": complete,
                "results": [
                    {
                        "name": "python",
                        "state": "OK",
                        "advisory": False,
                    },
                    {
                        "name": "gdtoolkit",
                        "state": state,
                        "advisory": False,
                    },
                    {
                        "name": "editor-settings",
                        "state": "MISSING",
                        "advisory": True,
                    },
                ],
            },
        }
    )


class FakeRunner:
    def __init__(
        self,
        *,
        source: strict_verify.ProcessOutcome | None = None,
        doctor: strict_verify.ProcessOutcome | None = None,
        gate: strict_verify.ProcessOutcome | None = None,
        unit: strict_verify.ProcessOutcome | None = None,
        browser: strict_verify.ProcessOutcome | None = None,
        archive_one: bytes = b"deterministic-release",
        archive_two: bytes | None = None,
    ) -> None:
        self.source = source or strict_verify.ProcessOutcome(0, "")
        self.doctor = doctor or strict_verify.ProcessOutcome(0, _doctor())
        self.gate = gate or strict_verify.ProcessOutcome(
            0, "PASS  integrity\nPASS  typecheck\nGATE PASSED\n"
        )
        self.unit = unit or strict_verify.ProcessOutcome(
            0, "", "test_one ... ok\n\nRan 1 test in 0.001s\n\nOK\n"
        )
        self.browser = browser or strict_verify.ProcessOutcome(
            0, "\n4/4 browser checks passed\n"
        )
        self.archive_one = archive_one
        self.archive_two = archive_one if archive_two is None else archive_two
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.release_builds = 0

    def __call__(
        self,
        command: strict_verify.Sequence[str],
        _cwd: Path,
        _timeout: int,
        environment: strict_verify.Mapping[str, str],
    ) -> strict_verify.ProcessOutcome:
        argv = [str(part) for part in command]
        self.commands.append(argv)
        self.environments.append(dict(environment))
        if "KIT_STRICT_VERIFY" not in environment:
            raise AssertionError("strict environment marker was not supplied")
        if Path(argv[0]).name.lower() in ("git", "git.exe"):
            return self.source
        script_name = Path(argv[1]).name
        if script_name == "kit.py":
            return self.doctor
        if script_name == "check.py":
            return self.gate
        if "unittest" in argv:
            return self.unit
        if script_name == "browser_check.py":
            return self.browser
        if script_name == "release.py":
            operation = argv[2]
            archive = Path(argv[3])
            if operation == "build":
                content = self.archive_one if self.release_builds == 0 else self.archive_two
                self.release_builds += 1
                archive.write_bytes(content)
            digest = strict_verify.hashlib.sha256(
                archive.read_bytes() if archive.is_file() else b""
            ).hexdigest()
            return strict_verify.ProcessOutcome(
                0, json.dumps({"ok": True, "archive_sha256": digest})
            )
        raise AssertionError(f"unexpected command: {argv}")


class StrictFixture(unittest.TestCase):
    def setUp(self) -> None:
        scratch = REPOSITORY / ".checklogs"
        scratch.mkdir(exist_ok=True)
        self.root = scratch / f"strict-verify-test-{uuid.uuid4().hex}"
        self.root.mkdir()
        (self.root / "tools" / "tests").mkdir(parents=True)
        (self.root / ".agent-kit.json").write_text(
            '{"kind":"portable-agent-kit-root","schema":1}\n', encoding="utf-8"
        )
        (self.root / "kit.config.json").write_text(
            '{"schema":1,"game_root":"src","runtime_root":".kit/runtime"}\n',
            encoding="utf-8",
        )
        for relative in (
            "kit.py",
            "check.py",
            "tools/tests/browser_check.py",
            "tools/release.py",
        ):
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# synthetic fixture\n", encoding="utf-8")
        (self.root / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        (self.root / "LICENSE").write_text(
            "Approved synthetic license.\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        resolved = self.root.resolve()
        scratch = (REPOSITORY / ".checklogs").resolve()
        if (resolved.parent != scratch
                or not resolved.name.startswith("strict-verify-test-")):
            raise AssertionError(f"refusing to remove unexpected scratch path: {resolved}")
        shutil.rmtree(resolved)

    def run_with(self, runner: FakeRunner) -> tuple[dict, int]:
        return strict_verify.run_strict(self.root, runner=runner)

    def enable_maintainer_fixture(self, content: str | None = None) -> None:
        marker = self.root / strict_verify.MAINTAINER_FIXTURE_PATH
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            strict_verify.MAINTAINER_FIXTURE_CONTENT if content is None else content,
            encoding="utf-8",
        )


class StrictSuccessTests(StrictFixture):
    def test_runs_all_proofs_sequentially_and_retains_complete_logs(self) -> None:
        runner = FakeRunner()
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_OK)
        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(
            [stage["name"] for stage in report["stages"]],
            [
                "source-state",
                "doctor",
                "gate",
                "unit-tests",
                "browser-check",
                "release-build-1",
                "release-build-2",
                "release-verify-1",
                "release-verify-2",
                "release-smoke",
                "release-reproducibility",
            ],
        )
        self.assertTrue(all(stage["status"] == "passed" for stage in report["stages"]))
        self.assertEqual(
            "no-exact-authority-event",
            report["authority_receipt"]["receipt_trust"],
        )
        self.assertNotIn("authenticated", json.dumps(report["authority_receipt"]).lower())
        self.assertEqual(len(runner.commands), 10)
        self.assertEqual("git", runner.commands[0][0])
        self.assertIn("doctor", runner.commands[1])
        self.assertEqual(runner.commands[2][1], str(self.root / "check.py"))
        self.assertIn("unittest", runner.commands[3])
        self.assertTrue(runner.commands[4][1].endswith("browser_check.py"))

        report_path = self.root / report["report_path"]
        retained = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(retained["run_id"], report["run_id"])
        for stage in report["stages"]:
            if "stdout_log" in stage:
                self.assertTrue((self.root / stage["stdout_log"]).is_file())
                self.assertTrue((self.root / stage["stderr_log"]).is_file())
        self.assertFalse(list(report_path.parent.glob(".*.tmp")))

    def test_exact_maintainer_fixture_allows_only_project_state_skips(self) -> None:
        self.enable_maintainer_fixture()
        runner = FakeRunner(
            gate=strict_verify.ProcessOutcome(
                0,
                "PASS  integrity\n"
                "SKIP  shape (absent)\n"
                "SKIP  design\n"
                "SKIP  conformance\n"
                "GATE PASSED\n",
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_OK)
        gate = next(item for item in report["stages"] if item["name"] == "gate")
        self.assertEqual(gate["status"], "passed")
        self.assertIn("canonical maintainer", gate["reason"])

    def test_crlf_maintainer_skips_are_classified(self) -> None:
        self.enable_maintainer_fixture()
        runner = FakeRunner(
            gate=strict_verify.ProcessOutcome(
                0,
                "PASS  integrity\r\n"
                "SKIP  shape (absent)\r\n"
                "SKIP  design\r\n"
                "SKIP  conformance\r\n"
                "GATE PASSED\r\n",
            )
        )

        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_OK)
        gate = next(item for item in report["stages"] if item["name"] == "gate")
        self.assertEqual(gate["status"], "passed")
        self.assertIn("SKIP design", gate["reason"])

    def test_only_the_authoritative_gate_receives_the_public_nonce(self) -> None:
        runner = FakeRunner()
        with mock.patch.dict(
            strict_verify.os.environ,
            {
                "KIT_VERIFY_NONCE": "a" * 32,
                "KIT_VERIFY_AUTH_KEY": "b" * 64,
                "KIT_VERIFY_REPOSITORY_SHA256": "c" * 64,
                "KIT_NATIVE_RETRY_TOKEN": "d" * 64,
            },
        ):
            _report, code = self.run_with(runner)

        self.assertEqual(strict_verify.EXIT_OK, code)
        receipt_keys = set(strict_verify.GATE_RECEIPT_ENV)
        self.assertFalse(receipt_keys.intersection(runner.environments[0]))
        self.assertFalse(receipt_keys.intersection(runner.environments[1]))
        self.assertEqual("a" * 32, runner.environments[2]["KIT_VERIFY_NONCE"])
        self.assertEqual("b" * 64, runner.environments[2]["KIT_VERIFY_AUTH_KEY"])
        self.assertEqual(
            "c" * 64,
            runner.environments[2]["KIT_VERIFY_REPOSITORY_SHA256"],
        )
        self.assertEqual(
            "d" * 64,
            runner.environments[2]["KIT_NATIVE_RETRY_TOKEN"],
        )
        for environment in runner.environments[3:]:
            self.assertFalse(receipt_keys.intersection(environment))


class StrictFailureTests(StrictFixture):
    def test_gate_skip_is_a_failure_even_when_gate_exits_zero(self) -> None:
        runner = FakeRunner(
            gate=strict_verify.ProcessOutcome(
                0, "PASS  integrity\nSKIP  format\nGATE PASSED\n"
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        stage = next(item for item in report["stages"] if item["name"] == "gate")
        self.assertEqual(stage["status"], "failed")
        self.assertIn("SKIP", stage["reason"])

    def test_crlf_gate_skip_is_a_failure_even_when_gate_exits_zero(self) -> None:
        runner = FakeRunner(
            gate=strict_verify.ProcessOutcome(
                0, "PASS  integrity\r\nSKIP  format\r\nGATE PASSED\r\n"
            )
        )

        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        stage = next(item for item in report["stages"] if item["name"] == "gate")
        self.assertEqual(stage["status"], "failed")
        self.assertIn("SKIP format", stage["reason"])

    def test_maintainer_fixture_does_not_allow_any_other_skip(self) -> None:
        self.enable_maintainer_fixture()
        runner = FakeRunner(
            gate=strict_verify.ProcessOutcome(
                0,
                "SKIP  shape (absent)\n"
                "SKIP  design\n"
                "SKIP  conformance\n"
                "SKIP  format\n"
                "GATE PASSED\n",
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        gate = next(item for item in report["stages"] if item["name"] == "gate")
        self.assertEqual(gate["status"], "failed")
        self.assertIn("SKIP format", gate["reason"])

    def test_wrong_maintainer_marker_content_fails_closed(self) -> None:
        self.enable_maintainer_fixture("not-the-contract\n")
        runner = FakeRunner(
            gate=strict_verify.ProcessOutcome(
                0, "SKIP  shape (absent)\nGATE PASSED\n"
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        gate = next(item for item in report["stages"] if item["name"] == "gate")
        self.assertEqual(gate["status"], "failed")

    def test_unit_test_skip_is_a_failure(self) -> None:
        runner = FakeRunner(
            unit=strict_verify.ProcessOutcome(
                0,
                "",
                "test_optional ... skipped 'missing tool'\n\n"
                "Ran 1 test in 0.001s\n\nOK (skipped=1)\n",
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        stage = next(item for item in report["stages"] if item["name"] == "unit-tests")
        self.assertEqual(stage["status"], "failed")
        self.assertIn("skipped", stage["reason"])

    def test_browser_skip_is_an_explicit_blocker(self) -> None:
        runner = FakeRunner(
            browser=strict_verify.ProcessOutcome(
                0, "no Chromium-based browser found; browser check skipped\n"
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_BLOCKED)
        stage = next(item for item in report["stages"] if item["name"] == "browser-check")
        self.assertEqual(stage["status"], "blocked")

    def test_timeout_fails_and_refuses_to_start_later_processes(self) -> None:
        runner = FakeRunner(
            gate=strict_verify.ProcessOutcome(
                None,
                "partial gate output\n",
                "partial gate error\n",
                1800.0,
                timed_out=True,
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        self.assertEqual(len(runner.commands), 3)
        self.assertEqual(
            [item["status"] for item in report["stages"][:5]],
            ["passed", "passed", "failed", "not_run", "not_run"],
        )
        gate = report["stages"][2]
        self.assertEqual(
            (self.root / gate["stdout_log"]).read_text(encoding="utf-8"),
            "partial gate output\n",
        )

    def test_malformed_doctor_output_fails_closed(self) -> None:
        runner = FakeRunner(doctor=strict_verify.ProcessOutcome(0, "ready maybe\n"))
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        self.assertEqual(report["stages"][1]["status"], "failed")
        self.assertIn("not one JSON", report["stages"][1]["reason"])
        self.assertEqual(len(runner.commands), 2)

    def test_release_builds_must_be_byte_identical(self) -> None:
        runner = FakeRunner(archive_one=b"first", archive_two=b"second")
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        stage = report["stages"][-1]
        self.assertEqual(stage["name"], "release-reproducibility")
        self.assertEqual(stage["status"], "failed")


class StrictBlockedTests(StrictFixture):
    def test_missing_legal_metadata_is_blocked_not_skipped(self) -> None:
        (self.root / "LICENSE").unlink()
        runner = FakeRunner()
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_BLOCKED)
        self.assertEqual(len(runner.commands), 5)
        stage = report["stages"][-1]
        self.assertEqual(stage["name"], "release-metadata")
        self.assertEqual(stage["status"], "blocked")
        self.assertIn("LICENSE", stage["reason"])

    def test_incomplete_doctor_stops_before_any_mutating_capability(self) -> None:
        runner = FakeRunner(
            doctor=strict_verify.ProcessOutcome(1, _doctor(complete=False))
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_BLOCKED)
        self.assertEqual(len(runner.commands), 2)
        self.assertEqual(report["stages"][1]["status"], "blocked")
        self.assertTrue(all(
            stage["status"] == "not_run" for stage in report["stages"][2:]
        ))

    def test_dirty_source_state_stops_before_any_project_command(self) -> None:
        runner = FakeRunner(
            source=strict_verify.ProcessOutcome(0, " M tools/strict_verify.py\n")
        )
        report, code = self.run_with(runner)

        self.assertEqual(strict_verify.EXIT_BLOCKED, code)
        self.assertEqual(1, len(runner.commands))
        self.assertEqual("source-state", report["stages"][0]["name"])
        self.assertEqual("blocked", report["stages"][0]["status"])
        self.assertTrue(all(
            stage["status"] == "not_run" for stage in report["stages"][1:]
        ))


class StrictCliTests(unittest.TestCase):
    def test_json_cli_preserves_documented_exit_code(self) -> None:
        payload = {
            "schema": 1,
            "command": "strict-verify",
            "ok": False,
            "status": "blocked",
            "exit_code": strict_verify.EXIT_BLOCKED,
        }
        output = io.StringIO()
        with mock.patch.object(
            strict_verify, "run_strict", return_value=(payload, strict_verify.EXIT_BLOCKED)
        ), contextlib.redirect_stdout(output):
            code = strict_verify.main(["--json"])

        self.assertEqual(code, strict_verify.EXIT_BLOCKED)
        self.assertEqual(json.loads(output.getvalue()), payload)

    def test_default_runner_uses_shared_bounded_process_containment(self) -> None:
        supervised = strict_verify.process_supervisor.SupervisedResult(
            0, "ok\n", "", 0.25
        )
        with mock.patch.object(
            strict_verify.process_supervisor,
            "run_supervised",
            return_value=supervised,
        ) as run:
            outcome = strict_verify._default_runner(
                ["git", "status"],
                REPOSITORY,
                30,
                {"NO_COLOR": "1"},
            )

        self.assertEqual(0, outcome.returncode)
        self.assertEqual("ok\n", outcome.stdout)
        run.assert_called_once_with(
            ["git", "status"],
            cwd=REPOSITORY,
            timeout=30,
            environment={"NO_COLOR": "1"},
            capture_output=True,
            allow_child_breakaway=False,
        )

    def test_default_runner_surfaces_unverified_termination_as_launch_failure(self) -> None:
        supervised = strict_verify.process_supervisor.SupervisedResult(
            None,
            "partial",
            "",
            30.0,
            timed_out=True,
            termination_verified=False,
        )
        with mock.patch.object(
            strict_verify.process_supervisor,
            "run_supervised",
            return_value=supervised,
        ):
            outcome = strict_verify._default_runner(
                ["git", "status"], REPOSITORY, 30, {}
            )

        self.assertFalse(outcome.timed_out)
        self.assertIn("termination could not be verified", outcome.launch_error)


class StrictCiContractTests(unittest.TestCase):
    def test_ci_uses_only_pinned_official_actions_and_authenticated_builds(self) -> None:
        root = Path(__file__).resolve().parents[2]
        workflow = (root / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        uses = re.findall(r"(?m)^\s*uses:\s*([^\s#]+)", workflow)
        self.assertEqual(
            uses,
            [
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
                "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020",
            ],
        )
        self.assertTrue(all(re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", item)
                            for item in uses))
        self.assertIn("kit setup dependency gdtoolkit", workflow)
        self.assertIn("kit setup audit-dependencies", workflow)
        self.assertIn("node-version: 24.19.0", workflow)
        self.assertIn("python-version: 3.10.21", workflow)
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("github.com/godotengine/godot-builds/releases/download", workflow)
        self.assertIn("SHA512-SUMS.txt", workflow)
        self.assertIn("hashlib.sha512", workflow)
        self.assertIn("kit self-test", workflow)
        self.assertIn("kit verify --strict --json", workflow)
        self.assertLess(
            workflow.index("kit self-test"),
            workflow.index("Download and authenticate the official Godot build"),
        )
        self.assertNotIn("pip install", workflow)
        self.assertNotIn("python tools/strict_verify.py", workflow)
        self.assertNotIn("upload-artifact", workflow)

    def test_ci_covers_all_three_desktop_operating_systems(self) -> None:
        root = Path(__file__).resolve().parents[2]
        workflow = (root / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        for runner in ("ubuntu-24.04", "windows-2025", "macos-15"):
            self.assertIn(f"os: {runner}", workflow)
        self.assertIn("Godot_v4.7.2-stable_linux.x86_64.zip", workflow)
        self.assertIn("Godot_v4.7.2-stable_win64.exe.zip", workflow)
        self.assertIn("Godot_v4.7.2-stable_macos.universal.zip", workflow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
