#!/usr/bin/env python3
"""Contract tests for the unified public kit CLI.

The tests deliberately use the checked-out repository as their fixture and
replace every delegated process. They need no writable temporary directory
and cannot accidentally run a provider, installer, gate, or server.
"""
from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import kit  # noqa: E402


class KitCliTest(unittest.TestCase):
    @staticmethod
    def completed(
        returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def invoke(self, *args: str) -> tuple[int, str]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = kit.main(list(args))
        return code, output.getvalue()

    def test_doctor_is_read_only_offline_and_never_invokes_a_provider(self) -> None:
        bootstrap = ROOT / "bootstrap.py"
        probe = self.completed(stdout=json.dumps({
            "complete": True,
            "needs_human": False,
            "results": [
                {"name": "python", "state": "OK", "detail": "3.13"},
                {"name": "check", "state": "OK", "detail": "available"},
            ],
        }))

        with mock.patch.object(kit, "_run_process", return_value=probe) as run, \
                mock.patch.object(Path, "write_text") as write_text, \
                mock.patch.object(Path, "write_bytes") as write_bytes, \
                mock.patch.object(Path, "mkdir") as mkdir, \
                mock.patch.object(subprocess, "run") as raw_run:
            code, output = self.invoke("doctor", "--project", str(ROOT), "--json")

        self.assertEqual(kit.EXIT_OK, code)
        payload = json.loads(output)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["bootstrap"]["complete"])
        self.assertEqual("OK", payload["check"]["bootstrap_result"]["state"])
        self.assertNotIn("stdout", payload["process"])
        self.assertEqual(0, payload["process"]["exit_code"])
        run.assert_called_once_with(
            [sys.executable, str(bootstrap), "--json"],
            cwd=ROOT.resolve(),
            timeout=300,
        )
        called = run.call_args.args[0]
        self.assertFalse(kit._looks_like_provider_action(called))
        self.assertNotIn("--fix", called)
        write_text.assert_not_called()
        write_bytes.assert_not_called()
        mkdir.assert_not_called()
        raw_run.assert_not_called()

    def test_doctor_reports_missing_required_readiness_from_read_only_bootstrap(self) -> None:
        bootstrap = ROOT / "bootstrap.py"
        probe = self.completed(returncode=1, stdout=json.dumps({
            "complete": False,
            "needs_human": False,
            "results": [{
                "name": "godot",
                "state": "MISSING",
                "detail": "not installed",
                "remedy": "opt in to setup",
            }],
        }))
        with mock.patch.object(kit, "_run_process", return_value=probe) as run:
            code, output = self.invoke("--project", str(ROOT), "--json", "doctor")
        self.assertEqual(kit.EXIT_REFUSED, code)
        payload = json.loads(output)
        self.assertEqual("needs_setup", payload["status"])
        self.assertEqual(["godot"], payload["blocking"])
        self.assertEqual(
            [sys.executable, str(bootstrap), "--json"], run.call_args.args[0]
        )

    def test_doctor_rejects_an_invalid_bootstrap_probe(self) -> None:
        probe = self.completed(stdout="not json\n")
        with mock.patch.object(kit, "_run_process", return_value=probe):
            code, output = self.invoke("doctor", "--project", str(ROOT), "--json")
        self.assertEqual(kit.EXIT_FAILED, code)
        self.assertEqual("probe_failed", json.loads(output)["status"])

    def test_setup_dependency_reports_the_selected_operation_not_global_readiness(self) -> None:
        bootstrap = ROOT / "bootstrap.py"
        with mock.patch.object(
                kit, "_run_process", return_value=self.completed(stdout="gut ready\n")
        ) as run:
            code, output = self.invoke(
                "setup", "dependency", "gut", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        self.assertEqual("completed", json.loads(output)["status"])
        run.assert_called_once_with(
            [sys.executable, str(bootstrap), "--download-dep", "gut",
             "--operation-target", "gut"],
            cwd=ROOT.resolve(), timeout=900,
        )

    def test_setup_gdtoolkit_uses_the_same_explicit_locked_dependency_path(self) -> None:
        bootstrap = ROOT / "bootstrap.py"
        with mock.patch.object(
                kit, "_run_process", return_value=self.completed(stdout="gdtoolkit ready\n")
        ) as run:
            code, _output = self.invoke(
                "setup", "dependency", "gdtoolkit", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        run.assert_called_once_with(
            [sys.executable, str(bootstrap), "--download-dep", "gdtoolkit",
             "--operation-target", "gdtoolkit"],
            cwd=ROOT.resolve(), timeout=900,
        )

    def test_setup_dependency_audit_uses_one_public_bounded_operation(self) -> None:
        bootstrap = ROOT / "bootstrap.py"
        with mock.patch.object(
                kit, "_run_process", return_value=self.completed(stdout="sources verified\n")
        ) as run:
            code, output = self.invoke(
                "setup", "audit-dependencies", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        self.assertEqual("completed", json.loads(output)["status"])
        run.assert_called_once_with(
            [sys.executable, str(bootstrap), "--audit-dependencies",
             "--operation-target", "dependency-audit"],
            cwd=ROOT.resolve(), timeout=900,
        )

    def test_setup_layout_maps_the_public_root_name_to_the_config_value(self) -> None:
        bootstrap = ROOT / "bootstrap.py"
        with mock.patch.object(
                kit, "_run_process", return_value=self.completed(stdout="layout ready\n")
        ) as run:
            code, _output = self.invoke(
                "setup", "layout", "root", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        run.assert_called_once_with(
            [sys.executable, str(bootstrap), "--game-layout", ".",
             "--operation-target", "game-layout"],
            cwd=ROOT.resolve(), timeout=900,
        )

    def test_setup_format_uses_the_gate_owned_formatter(self) -> None:
        check = ROOT / "check.py"
        with mock.patch.object(kit, "_run_process", return_value=self.completed()) as run:
            code, _output = self.invoke(
                "setup", "format", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        run.assert_called_once_with(
            [sys.executable, str(check), "--fix-format"],
            cwd=ROOT.resolve(), timeout=300,
        )

    def test_integrity_accept_is_a_typed_human_review_action(self) -> None:
        check = ROOT / "check.py"
        with mock.patch.object(kit, "_run_process", return_value=self.completed()) as run:
            code, output = self.invoke(
                "integrity", "accept", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        self.assertEqual("accepted", json.loads(output)["status"])
        run.assert_called_once_with(
            [sys.executable, str(check), "--accept-gate-changes"],
            cwd=ROOT.resolve(), timeout=120,
        )

    def test_verify_routes_normal_and_strict_modes_to_their_owners(self) -> None:
        check = ROOT / "check.py"
        strict_tool = ROOT / "tools" / "strict_verify.py"
        engine_path = ROOT / "Godot_v4.7.2-stable_win64_console.exe"
        engine = kit.engine_discovery.EngineSelection(
            engine_path,
            "project-adjacent",
            selected_version="4.7.2",
        )
        environment = {"GODOT_BIN": str(engine_path)}
        gate = self.completed(
            stdout="\x1b[33mSKIP\x1b[0m  format\nPASS  smoke\nGATE PASSED\n"
        )
        with mock.patch.object(
            kit, "_run_process_inherited", return_value=gate
        ) as run, mock.patch.object(kit, "_run_process") as captured, \
                mock.patch.object(
                    kit.engine_discovery, "select_godot", return_value=engine
                ):
            code, _ = self.invoke("verify", "--project", str(ROOT))
        self.assertEqual(kit.EXIT_OK, code)
        run.assert_called_once_with(
            [sys.executable, str(check)], cwd=ROOT.resolve(), timeout=1800,
            environment=environment,
        )
        captured.assert_not_called()

        strict = self.completed(
            returncode=kit.EXIT_REFUSED,
            stdout=json.dumps({
                "exit_code": kit.EXIT_REFUSED,
                "status": "blocked",
                "stages": [{
                    "name": "dependencies", "status": "blocked",
                    "reason": "required dependency unavailable",
                }],
            }),
        )
        with mock.patch.object(kit, "_run_process", return_value=strict) as run, \
                mock.patch.object(
                    kit.engine_discovery, "select_godot", return_value=engine
                ):
            code, output = self.invoke(
                "verify", "--strict", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_REFUSED, code)
        payload = json.loads(output)
        self.assertEqual("blocked", payload["status"])
        run.assert_called_once_with(
            [sys.executable, str(strict_tool), "--json"],
            cwd=ROOT.resolve(), timeout=7200, environment=environment,
        )

    def test_static_verify_is_explicit_json_safe_and_never_routes_to_strict(self) -> None:
        check = ROOT / "check.py"
        gate = self.completed(stdout="PASS  integrity\nGATE PASSED\n")
        with mock.patch.object(kit, "_run_process", return_value=gate) as run, \
                mock.patch.object(kit, "_run_process_inherited") as inherited, \
                mock.patch.object(
                    kit.engine_discovery,
                    "select_godot",
                    side_effect=AssertionError("static verification discovered Godot"),
                ):
            code, output = self.invoke(
                "verify", "--static", "--project", str(ROOT), "--json"
            )

        self.assertEqual(kit.EXIT_OK, code)
        payload = json.loads(output)
        self.assertTrue(payload["static"])
        self.assertFalse(payload["strict"])
        run.assert_called_once_with(
            [sys.executable, str(check), "--static"],
            cwd=ROOT.resolve(), timeout=1800, environment=None,
        )
        inherited.assert_not_called()

    def test_known_engine_filename_mismatch_is_refused_without_launch(self) -> None:
        mismatch = kit.engine_discovery.EngineSelection(
            ROOT / "Godot_v4.7.1-stable_win64.exe",
            "environment",
            requested="Godot_v4.7.1-stable_win64.exe",
            requested_version="4.7.1",
            selected_version="4.7.1",
        )
        with mock.patch.object(
            kit.engine_discovery, "select_godot", return_value=mismatch
        ), mock.patch.object(kit, "_run_process") as captured, \
                mock.patch.object(kit, "_run_process_inherited") as inherited:
            code, output = self.invoke(
                "verify", "--project", str(ROOT), "--json"
            )

        self.assertEqual(kit.EXIT_REFUSED, code)
        self.assertEqual("engine_version_mismatch", json.loads(output)["status"])
        captured.assert_not_called()
        inherited.assert_not_called()

    def test_static_verify_rejects_ambiguous_scope_combinations(self) -> None:
        for conflicting in (("--stage", "integrity"), ("--fast",), ("--strict",)):
            with self.subTest(conflicting=conflicting), mock.patch.object(
                kit, "_run_process"
            ) as captured, mock.patch.object(
                kit, "_run_process_inherited"
            ) as inherited:
                code, output = self.invoke(
                    "verify", "--static", *conflicting,
                    "--project", str(ROOT), "--json",
                )
            self.assertEqual(kit.EXIT_REFUSED, code)
            expected_status = (
                "strict_scope_refused"
                if "--strict" in conflicting else "static_scope_refused"
            )
            self.assertEqual(expected_status, json.loads(output)["status"])
            captured.assert_not_called()
            inherited.assert_not_called()

    def test_self_test_uses_the_public_launcher_and_enforces_no_engine(self) -> None:
        suite = self.completed(stdout="all kit tests passed\n")
        with mock.patch.object(kit, "_run_process", return_value=suite) as run, \
                mock.patch.object(kit, "_run_process_inherited") as inherited:
            code, output = self.invoke(
                "self-test", "--project", str(ROOT), "--json"
            )

        self.assertEqual(kit.EXIT_OK, code)
        payload = json.loads(output)
        self.assertEqual("passed", payload["status"])
        self.assertEqual("disabled", payload["engine"])
        run.assert_called_once_with(
            [
                sys.executable,
                "-m",
                "unittest",
                "discover",
                "-s",
                "tools/tests",
            ],
            cwd=ROOT.resolve(),
            timeout=1800,
            environment={
                "KIT_ENGINE_DISABLED": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        inherited.assert_not_called()

    def test_verify_maps_a_child_failure_to_one(self) -> None:
        gate = self.completed(returncode=2, stderr="godot unavailable\n")
        with mock.patch.object(kit, "_run_process", return_value=gate):
            code, output = self.invoke("verify", "--project", str(ROOT), "--json")
        self.assertEqual(kit.EXIT_FAILED, code)
        self.assertEqual("failed", json.loads(output)["status"])

    def test_verify_stage_is_repeatable_and_validated(self) -> None:
        check = ROOT / "check.py"
        with mock.patch.object(
                kit, "_run_process", return_value=self.completed(stdout="GATE PASSED\n")
        ) as run:
            code, _output = self.invoke(
                "verify", "--stage", "shape", "--stage", "conformance",
                "--project", str(ROOT), "--json",
            )
        self.assertEqual(kit.EXIT_OK, code)
        run.assert_called_once_with(
            [sys.executable, str(check), "--only", "shape", "--only", "conformance"],
            cwd=ROOT.resolve(), timeout=1800, environment=None,
        )

    def test_architecture_update_routes_to_the_internal_generator(self) -> None:
        tool = ROOT / "arch.py"
        with mock.patch.object(kit, "_run_process", return_value=self.completed()) as run:
            code, output = self.invoke(
                "architecture", "update", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        self.assertEqual("completed", json.loads(output)["status"])
        run.assert_called_once_with(
            [sys.executable, str(tool), "--write"],
            cwd=ROOT.resolve(), timeout=300,
        )

    def test_sanitize_is_read_only_unless_write_is_explicit(self) -> None:
        tool = ROOT / "sanitise.py"
        for arguments, delegated in (((), []), (("--write",), ["--write"])):
            with self.subTest(arguments=arguments), mock.patch.object(
                kit, "_run_process", return_value=self.completed()
            ) as run:
                code, _output = self.invoke(
                    "sanitize", *arguments, "--project", str(ROOT), "--json"
                )
            self.assertEqual(kit.EXIT_OK, code)
            run.assert_called_once_with(
                [sys.executable, str(tool), *delegated],
                cwd=ROOT.resolve(), timeout=300,
            )

    def test_schema_describe_exposes_fields_through_the_public_cli(self) -> None:
        tool = ROOT / "tools" / "schema.py"
        result = self.completed(stdout="slice (required) -- slice identity\n")
        with mock.patch.object(kit, "_run_process", return_value=result) as run:
            code, output = self.invoke(
                "schema", "describe", "proposal",
                "--project", str(ROOT), "--json",
            )
        self.assertEqual(kit.EXIT_OK, code)
        self.assertIn("slice (required)", json.loads(output)["description"])
        run.assert_called_once_with(
            [sys.executable, str(tool), "--describe", "proposal"],
            cwd=ROOT.resolve(), timeout=60,
        )

    def test_godot_docs_and_friction_hide_internal_entry_points(self) -> None:
        docs = ROOT / "tools" / "gddoc.py"
        friction = ROOT / "tools" / "friction.py"
        cases = (
            (("godot-docs", "build"), [sys.executable, str(docs), "--build"], 360),
            (("godot-docs", "show", "Color.from_string"),
             [sys.executable, str(docs), "Color.from_string"], 60),
            (("godot-docs", "search", "from_string"),
             [sys.executable, str(docs), "--search", "from_string"], 60),
            (("friction", "--since", "HEAD~2"),
             [sys.executable, str(friction), "--since", "HEAD~2"], 60),
        )
        for public, delegated, timeout in cases:
            with self.subTest(public=public), mock.patch.object(
                kit, "_run_process", return_value=self.completed()
            ) as run:
                code, _output = self.invoke(
                    *public, "--project", str(ROOT), "--json"
                )
            self.assertEqual(kit.EXIT_OK, code)
            run.assert_called_once_with(
                delegated, cwd=ROOT.resolve(), timeout=timeout
            )

    def test_plan_routes_both_views_without_starting_the_board(self) -> None:
        plan_tool = ROOT / "tools" / "plan_html.py"
        retro_tool = ROOT / "tools" / "retro_html.py"
        with mock.patch.object(kit, "_run_process", return_value=self.completed()) as run:
            code, output = self.invoke("plan", "--project", str(ROOT), "--json")
        self.assertEqual(kit.EXIT_OK, code)
        payload = json.loads(output)
        self.assertEqual("regenerated", payload["status"])
        self.assertEqual(str(ROOT / "plan.html"), payload["paths"]["plan"])
        self.assertEqual(str(ROOT / "retro.html"), payload["paths"]["retro"])
        self.assertEqual([
            mock.call(
                [sys.executable, str(plan_tool)],
                cwd=ROOT.resolve(), timeout=180
            ),
            mock.call(
                [sys.executable, str(retro_tool), "--no-board"],
                cwd=ROOT.resolve(),
                timeout=180,
            ),
        ], run.call_args_list)

    def test_plan_fails_when_either_view_fails(self) -> None:
        results = [self.completed(), self.completed(returncode=1, stderr="bad retro")]
        with mock.patch.object(kit, "_run_process", side_effect=results):
            code, output = self.invoke(
                "plan", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_FAILED, code)
        self.assertEqual("failed", json.loads(output)["status"])

    def test_serve_routes_to_board_ensure_and_returns_url(self) -> None:
        tool = ROOT / "tools" / "board.py"
        result = self.completed(stdout="http://127.0.0.1:54321/\n")
        with mock.patch.object(kit, "_run_process", return_value=result) as run:
            code, output = self.invoke("serve", "--project", str(ROOT), "--json")
        self.assertEqual(kit.EXIT_OK, code)
        self.assertEqual("http://127.0.0.1:54321/", json.loads(output)["url"])
        run.assert_called_once_with(
            [sys.executable, str(tool), "--ensure"],
            cwd=ROOT.resolve(),
            timeout=60,
        )

    def test_retro_status_is_deterministic_and_routes_to_due_tool(self) -> None:
        tool = ROOT / "tools" / "retro_due.py"
        result = self.completed(stdout=json.dumps({
            "unarchived": 3,
            "threshold": 10,
            "due": False,
            "archived": 2,
            "notes": [],
        }))
        with mock.patch.object(kit, "_run_process", return_value=result) as run:
            code, output = self.invoke(
                "retro", "status", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        self.assertEqual("not_due", json.loads(output)["status"])
        run.assert_called_once_with(
            [sys.executable, str(tool), "--json"],
            cwd=ROOT.resolve(),
            timeout=30,
        )

    def test_manual_retro_run_prepares_evidence_without_spend_confirmation(self) -> None:
        tool = ROOT / "tools" / "retro.py"
        result = self.completed(stdout="evidence prepared\n")
        with mock.patch.object(kit, "_run_process", return_value=result) as run:
            code, output = self.invoke(
                "retro", "run", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        payload = json.loads(output)
        self.assertEqual("evidence_prepared", payload["status"])
        self.assertEqual("manual", payload["provider"]["kind"])
        run.assert_called_once_with(
            [sys.executable, str(tool), "--print", "--force"],
            cwd=ROOT.resolve(), timeout=3600, allow_provider=False,
        )

    def test_automatic_retro_run_needs_explicit_spend_confirmation(self) -> None:
        tool = ROOT / "tools" / "retro.py"
        provider = kit.providers.ProviderSpec("analyzer", "copilot-sdk", "small")
        with mock.patch.object(kit.providers, "selection", return_value=provider), \
                mock.patch.object(kit.providers, "preflight", return_value=[]), \
                mock.patch.object(kit, "_run_process") as run:
            code, output = self.invoke(
                "retro", "run", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_REFUSED, code)
        payload = json.loads(output)
        self.assertEqual("confirmation_required", payload["status"])
        run.assert_not_called()

        result = self.completed(stdout="findings written\n")
        with mock.patch.object(kit.providers, "selection", return_value=provider), \
                mock.patch.object(kit.providers, "preflight", return_value=[]), \
                mock.patch.object(kit, "_run_process", return_value=result) as run:
            code, output = self.invoke(
                "retro", "run", "--confirm-spend",
                "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        self.assertEqual("completed", json.loads(output)["status"])
        run.assert_called_once_with(
            [sys.executable, str(tool), "--sdk", "--force"],
            cwd=ROOT.resolve(),
            timeout=3600,
            allow_provider=True,
        )

    def test_retro_publish_validates_then_refreshes_both_views(self) -> None:
        tool = ROOT / "tools" / "retro_rank.py"
        plan_payload = {"status": "regenerated"}
        with mock.patch.object(
                kit, "_run_process", return_value=self.completed(stdout="ranked\n")
        ) as run, mock.patch.object(
                kit, "_plan", return_value=(kit.EXIT_OK, plan_payload, [])
        ) as plan:
            code, output = self.invoke(
                "retro", "publish", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        self.assertEqual("published", json.loads(output)["status"])
        run.assert_called_once_with(
            [sys.executable, str(tool)], cwd=ROOT.resolve(), timeout=180
        )
        plan.assert_called_once()

    def test_retro_publish_refuses_a_report_outside_docs_retro(self) -> None:
        with mock.patch.object(kit, "_run_process") as run:
            code, output = self.invoke(
                "retro", "publish", str(ROOT / "README.md"),
                "--project", str(ROOT), "--json",
            )
        self.assertEqual(kit.EXIT_REFUSED, code)
        self.assertEqual("report_outside_retro", json.loads(output)["status"])
        run.assert_not_called()

    def test_release_fails_clearly_when_optional_tool_is_absent(self) -> None:
        missing = ROOT / "tools" / "release-missing.py"
        with mock.patch.object(kit, "_release_path", return_value=missing), \
                mock.patch.object(kit, "_run_process") as run:
            code, output = self.invoke(
                "release", "build", "kit.zip",
                "--project", str(ROOT), "--json",
            )
        self.assertEqual(kit.EXIT_REFUSED, code)
        self.assertEqual("release_unavailable", json.loads(output)["status"])
        run.assert_not_called()

    def test_release_delegates_typed_operation_and_archive(self) -> None:
        tool = ROOT / "tools" / "release.py"
        result = self.completed(stdout="{\"ok\": true}\n")
        for operation in ("build", "inspect", "verify"):
            with self.subTest(operation=operation), \
                    mock.patch.object(kit, "_run_process", return_value=result) as run:
                code, output = self.invoke(
                    "release", operation, "dist/kit.zip",
                    "--project", str(ROOT), "--json",
                )
            self.assertEqual(kit.EXIT_OK, code)
            payload = json.loads(output)
            self.assertEqual("completed", payload["status"])
            self.assertEqual(operation, payload["operation"])
            run.assert_called_once_with(
                [sys.executable, str(tool), operation, "dist/kit.zip"],
                cwd=ROOT.resolve(),
                timeout=900,
            )

    def test_bare_release_prints_typed_choices_and_exits_two(self) -> None:
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors), self.assertRaises(SystemExit) as raised:
            kit.main(["release"])
        self.assertEqual(2, raised.exception.code)
        self.assertIn("{build,inspect,verify}", errors.getvalue())

    def test_provider_guard_refuses_non_retro_provider_processes(self) -> None:
        with mock.patch.object(subprocess, "run") as run:
            for executable in ("copilot", "copilot.exe", "claude.cmd"):
                with self.subTest(executable=executable), \
                        self.assertRaises(kit.CliError) as raised:
                    kit._run_process(
                        [executable, "-p", "hello"], cwd=ROOT, timeout=1
                    )
                self.assertEqual("confirmation_required", raised.exception.status)
        run.assert_not_called()
        self.assertFalse(kit._looks_like_provider_action(
            [sys.executable, "tools/release.py", "build", "copilot.zip"]
        ))

    def test_process_runner_uses_an_argv_list_and_never_a_shell(self) -> None:
        completed = self.completed(stdout="ok\n")
        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            actual = kit._run_process(
                [sys.executable, "check.py", "--list"], cwd=ROOT, timeout=30
            )
        self.assertIs(completed, actual)
        arguments, keywords = run.call_args
        self.assertEqual([sys.executable, "check.py", "--list"], arguments[0])
        self.assertIs(False, keywords["shell"])
        self.assertEqual(str(ROOT), keywords["cwd"])
        self.assertIs(subprocess.DEVNULL, keywords["stdin"])

    def test_human_verifier_runner_inherits_live_output_and_never_a_shell(self) -> None:
        completed = self.completed()
        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            actual = kit._run_process_inherited(
                [sys.executable, "check.py", "--static"], cwd=ROOT, timeout=30
            )
        self.assertIs(completed, actual)
        arguments, keywords = run.call_args
        self.assertEqual(
            [sys.executable, "check.py", "--static"], arguments[0]
        )
        self.assertIs(False, keywords["shell"])
        self.assertEqual(str(ROOT), keywords["cwd"])
        self.assertIs(subprocess.DEVNULL, keywords["stdin"])
        self.assertNotIn("stdout", keywords)
        self.assertNotIn("stderr", keywords)

    def test_missing_project_is_a_machine_readable_refusal(self) -> None:
        missing = ROOT / ".definitely-not-a-project-for-kit-cli-tests"
        code, output = self.invoke("doctor", "--project", str(missing), "--json")
        self.assertEqual(kit.EXIT_REFUSED, code)
        self.assertEqual("project_unavailable", json.loads(output)["status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
