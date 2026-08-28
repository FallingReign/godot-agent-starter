#!/usr/bin/env python3
"""Contract tests for the unified public kit CLI.

The tests deliberately use the checked-out repository as their content fixture
and replace every delegated process. Verification locks use a private test
root, so this suite can run inside strict verification without contending with
the parent verifier. Tests cannot accidentally run a provider, installer, gate,
or server.
"""
from __future__ import annotations

import contextlib
import hashlib
import hmac
import io
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import kit  # noqa: E402


class KitCliTest(unittest.TestCase):
    def setUp(self) -> None:
        lock_root = (
            ROOT / ".checklogs" / "tests" / f"kit-cli-lock-{uuid.uuid4().hex}"
        )
        lock_root.mkdir(parents=True)
        shutil.copyfile(ROOT / ".agent-kit.json", lock_root / ".agent-kit.json")
        shutil.copyfile(ROOT / "kit.config.json", lock_root / "kit.config.json")
        self.addCleanup(shutil.rmtree, lock_root, True)

        real_lock = kit._verification_run_lock

        @contextlib.contextmanager
        def isolated_verification_lock(_project: Path):
            with real_lock(lock_root):
                yield

        lock_patch = mock.patch.object(
            kit, "_verification_run_lock", isolated_verification_lock
        )
        lock_patch.start()
        self.addCleanup(lock_patch.stop)

    @staticmethod
    def completed(
        returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    @staticmethod
    def signed_gate_summary(
        nonce: str,
        auth_key: str,
        repository_sha256: str,
        *,
        failed: bool = False,
        diagnostics: dict | None = None,
    ) -> dict:
        summary = {
            "schema": 2,
            "run_id": nonce,
            "repository_sha256": repository_sha256,
            "failed": failed,
            "results": ["FAIL engine"] if failed else ["PASS synthetic"],
            "diagnostics": diagnostics or {},
        }
        canonical = json.dumps(
            summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        summary["auth_sha256"] = hmac.new(
            bytes.fromhex(auth_key), canonical, hashlib.sha256
        ).hexdigest()
        return summary

    def invoke(
        self,
        *args: str,
        record_verification: bool = False,
        gate_summary_override: dict | None | type(Ellipsis) = Ellipsis,
    ) -> tuple[int, str]:
        output = io.StringIO()
        recording = (
            contextlib.nullcontext()
            if record_verification
            else mock.patch.object(
                kit.cockpit,
                "record_verification",
                return_value={"status": "test-isolated"},
            )
        )
        gate_summary = {
            "schema": 2,
            "run_id": "test-receipt",
            "repository_sha256": "a" * 64,
            "auth_sha256": "b" * 64,
            "failed": False,
            "results": [],
            "diagnostics": {},
        }
        if gate_summary_override is not Ellipsis:
            gate_summary = gate_summary_override  # type: ignore[assignment]
        with recording, mock.patch.object(
            kit, "_read_gate_summary", return_value=gate_summary
        ), contextlib.redirect_stdout(output):
            code = kit.main(list(args))
        return code, output.getvalue()

    def test_verify_result_is_forwarded_to_the_private_evidence_ledger(self) -> None:
        result = {
            "ok": True,
            "command": "verify",
            "status": "passed",
            "strict": False,
            "stages": ["schema"],
            "skips": [],
        }
        recorded = {"status": "fresh", "id": "fixture-verification"}
        fingerprint = {
            "available": True,
            "digest": "c" * 64,
            "head": "fixture",
            "untracked": 0,
        }
        with mock.patch.dict(os.environ, {"KIT_SELF_TEST": ""}), \
                mock.patch.object(
                    kit, "_verify_once", return_value=(
                        kit.EXIT_OK, result, ["verify: passed"]
                    )
                ), mock.patch.object(
                    kit.cockpit, "repository_fingerprint", return_value=fingerprint
                ), \
                mock.patch.object(
                    kit.cockpit, "record_verification", return_value=recorded
                ) as record:
            code, output = self.invoke(
                "verify",
                "--project",
                str(ROOT),
                "--json",
                record_verification=True,
            )

        self.assertEqual(kit.EXIT_OK, code)
        payload = json.loads(output)
        self.assertEqual(recorded, payload["verification_record"])
        record.assert_called_once()
        recorded_payload = record.call_args.args[1]
        self.assertEqual(0, recorded_payload["exit_code"])
        self.assertTrue(recorded_payload["repository_stable"])
        self.assertEqual(fingerprint, recorded_payload["repository_start"])

    def test_passing_verify_fails_if_its_evidence_cannot_be_persisted(self) -> None:
        gate = self.completed(stdout="PASS  integrity\nGATE PASSED\n")
        fingerprint = {
            "available": True,
            "digest": "d" * 64,
            "head": "fixture",
            "untracked": 0,
        }
        with mock.patch.dict(os.environ, {"KIT_SELF_TEST": ""}), mock.patch.object(
                kit, "_run_process", return_value=gate
        ), \
                mock.patch.object(
                    kit.cockpit, "repository_fingerprint", return_value=fingerprint
                ), mock.patch.object(
                    kit.cockpit,
                    "record_verification",
                    side_effect=OSError("locked"),
                ):
            code, output = self.invoke(
                "verify", "--static", "--project", str(ROOT), "--json",
                record_verification=True,
            )

        self.assertEqual(kit.EXIT_FAILED, code)
        payload = json.loads(output)
        self.assertEqual("evidence_persistence_failed", payload["status"])
        self.assertFalse(payload["ok"])

    def test_repository_change_during_verify_invalidates_a_green_gate(self) -> None:
        gate = self.completed(stdout="PASS  integrity\nGATE PASSED\n")
        fingerprints = [
            {"available": True, "digest": "e" * 64, "head": "before"},
            {"available": True, "digest": "f" * 64, "head": "after"},
        ]
        with mock.patch.dict(os.environ, {"KIT_SELF_TEST": ""}), mock.patch.object(
                kit, "_run_process", return_value=gate
        ), \
                mock.patch.object(
                    kit.cockpit,
                    "repository_fingerprint",
                    side_effect=fingerprints,
                ), mock.patch.object(
                    kit.cockpit,
                    "record_verification",
                    return_value={"status": "failed"},
                ):
            code, output = self.invoke(
                "verify", "--static", "--project", str(ROOT), "--json",
                record_verification=True,
            )

        self.assertEqual(kit.EXIT_FAILED, code)
        payload = json.loads(output)
        self.assertEqual("repository_changed_during_verification", payload["status"])
        self.assertFalse(payload["repository_stable"])

    def test_overlap_refusal_never_overwrites_completed_verification_evidence(self) -> None:
        with mock.patch.dict(os.environ, {"KIT_SELF_TEST": ""}), \
                kit._verification_run_lock(ROOT), mock.patch.object(
            kit.cockpit, "record_verification"
        ) as record:
            code, output = self.invoke(
                "verify", "--static", "--project", str(ROOT), "--json",
                record_verification=True,
            )

        self.assertEqual(kit.EXIT_REFUSED, code)
        self.assertEqual("verification_in_progress", json.loads(output)["status"])
        record.assert_not_called()

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
                mock.patch.object(kit, "_gdls_read_only_status", return_value=None), \
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
        with mock.patch.object(kit, "_run_process", return_value=probe) as run, \
                mock.patch.object(kit, "_gdls_read_only_status", return_value=None):
            code, output = self.invoke("--project", str(ROOT), "--json", "doctor")
        self.assertEqual(kit.EXIT_REFUSED, code)
        payload = json.loads(output)
        self.assertEqual("needs_setup", payload["status"])
        self.assertEqual(["godot"], payload["blocking"])
        self.assertEqual(
            [sys.executable, str(bootstrap), "--json"], run.call_args.args[0]
        )

    def test_doctor_surfaces_persisted_native_crash_without_another_process(self) -> None:
        probe = self.completed(stdout=json.dumps({
            "complete": True,
            "needs_human": False,
            "results": [{"name": "check", "state": "OK", "detail": "available"}],
        }))
        warning = {
            "code": "native-crash",
            "recorded_at": "2026-08-27T00:00:00Z",
            "run_id": "verify-1",
            "summary": "A Godot native application crash remains unresolved.",
        }
        with mock.patch.object(kit, "_run_process", return_value=probe) as run, \
                mock.patch.object(kit, "_gdls_read_only_status", return_value=None), \
                mock.patch.object(
                    kit.cockpit, "persisted_native_warning", return_value=warning
                ) as persisted, mock.patch.object(subprocess, "run") as raw_run:
            code, output = self.invoke("doctor", "--project", str(ROOT), "--json")

        self.assertEqual(kit.EXIT_OK, code)
        payload = json.loads(output)
        self.assertEqual([warning], payload["warnings"])
        run.assert_called_once()
        persisted.assert_called_once_with(ROOT.resolve())
        raw_run.assert_not_called()

    def test_doctor_rejects_an_invalid_bootstrap_probe(self) -> None:
        probe = self.completed(stdout="not json\n")
        with mock.patch.object(kit, "_run_process", return_value=probe), \
                mock.patch.object(kit, "_gdls_read_only_status", return_value=None):
            code, output = self.invoke("doctor", "--project", str(ROOT), "--json")
        self.assertEqual(kit.EXIT_FAILED, code)
        self.assertEqual("probe_failed", json.loads(output)["status"])

    def test_doctor_surfaces_read_only_gdls_disappearance_warning(self) -> None:
        probe = self.completed(stdout=json.dumps({
            "complete": True,
            "needs_human": False,
            "results": [{"name": "check", "state": "OK", "detail": "available"}],
        }))
        status = {
            "status": "owner_vanished",
            "pid": 8123,
            "warning": {
                "code": "native-engine-disappeared",
                "summary": "the exactly owned Godot process disappeared unexpectedly",
            },
        }
        with mock.patch.object(kit, "_run_process", return_value=probe), \
                mock.patch.object(
                    kit, "_gdls_read_only_status", return_value=status
                ), mock.patch.object(
                    kit.cockpit, "persisted_native_warning", return_value=None
                ):
            code, output = self.invoke("doctor", "--project", str(ROOT), "--json")

        self.assertEqual(kit.EXIT_OK, code)
        payload = json.loads(output)
        self.assertEqual(status, payload["gdls"])
        self.assertEqual("native-engine-disappeared", payload["warnings"][0]["code"])

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

    def test_setup_import_passes_the_one_public_engine_selection_to_bootstrap(self) -> None:
        bootstrap = ROOT / "bootstrap.py"
        engine_path = ROOT / "Godot_v4.7.2-stable_win64_console.exe"
        selected = kit.engine_discovery.EngineSelection(
            engine_path,
            "project-adjacent",
            selected_version="4.7.2",
        )
        with mock.patch.object(
            kit.engine_discovery, "select_godot", return_value=selected
        ), mock.patch.object(
            kit, "_run_process", return_value=self.completed(stdout="import ready\n")
        ) as run:
            code, _output = self.invoke(
                "setup", "import", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        run.assert_called_once_with(
            [
                sys.executable,
                str(bootstrap),
                "--import-project",
                "--engine-bin",
                str(engine_path),
                "--operation-target",
                "import-cache",
            ],
            cwd=ROOT.resolve(),
            timeout=900,
        )

    def test_native_setup_and_docs_refuse_a_known_wrong_engine_before_delegation(self) -> None:
        selected = kit.engine_discovery.EngineSelection(
            ROOT / "Godot_v4.7.1-stable_win64.exe",
            "environment",
            selected_version="4.7.1",
        )
        for public in (
            ("setup", "import"),
            ("godot-docs", "build"),
            ("gdls", "start"),
        ):
            with self.subTest(public=public), mock.patch.object(
                kit.engine_discovery, "select_godot", return_value=selected
            ), mock.patch.object(kit, "_run_process") as run:
                code, output = self.invoke(
                    *public, "--project", str(ROOT), "--json"
                )
            self.assertEqual(kit.EXIT_REFUSED, code)
            self.assertEqual("engine_version_mismatch", json.loads(output)["status"])
            run.assert_not_called()

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
        run.assert_called_once()
        self.assertEqual([sys.executable, str(check)], run.call_args.args[0])
        self.assertEqual(ROOT.resolve(), run.call_args.kwargs["cwd"])
        self.assertEqual(1800, run.call_args.kwargs["timeout"])
        actual_environment = dict(run.call_args.kwargs["environment"])
        nonce = actual_environment.pop("KIT_VERIFY_NONCE")
        auth_key = actual_environment.pop("KIT_VERIFY_AUTH_KEY")
        repository_sha256 = actual_environment.pop("KIT_VERIFY_REPOSITORY_SHA256")
        self.assertEqual("1", actual_environment.pop("PYTHONDONTWRITEBYTECODE"))
        self.assertRegex(nonce, r"^[0-9a-f]{32}$")
        self.assertRegex(auth_key, r"^[0-9a-f]{64}$")
        self.assertRegex(repository_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(environment, actual_environment)
        captured.assert_not_called()

        strict = self.completed(
            returncode=kit.EXIT_REFUSED,
            stdout=json.dumps({
                "schema": 1,
                "command": "strict-verify",
                "ok": False,
                "exit_code": kit.EXIT_REFUSED,
                "status": "blocked",
                "project": str(ROOT.resolve()),
                "authority_receipt": {
                    "receipt_trust": "no-exact-authority-event",
                    "receipt_reasons": ["fixture"],
                },
                "stages": [
                    {
                        "name": "source-state",
                        "status": "passed",
                        "reason": "clean",
                    },
                    {
                        "name": "doctor",
                        "status": "blocked",
                        "reason": "required dependency unavailable",
                    },
                    {
                        "name": "gate",
                        "status": "not_run",
                        "reason": "doctor blocked",
                    },
                ],
            }),
        )
        with mock.patch.object(kit, "_run_process", return_value=strict) as run, \
                mock.patch.object(
                    kit.engine_discovery, "select_godot", return_value=engine
                ):
            code, output = self.invoke(
                "verify", "--strict", "--project", str(ROOT), "--json",
                gate_summary_override=None,
            )
        self.assertEqual(kit.EXIT_REFUSED, code)
        payload = json.loads(output)
        self.assertEqual("blocked", payload["status"])
        run.assert_called_once()
        self.assertEqual(
            [sys.executable, str(strict_tool), "--json"], run.call_args.args[0]
        )
        self.assertEqual(ROOT.resolve(), run.call_args.kwargs["cwd"])
        self.assertEqual(10800, run.call_args.kwargs["timeout"])
        actual_environment = dict(run.call_args.kwargs["environment"])
        nonce = actual_environment.pop("KIT_VERIFY_NONCE")
        auth_key = actual_environment.pop("KIT_VERIFY_AUTH_KEY")
        repository_sha256 = actual_environment.pop("KIT_VERIFY_REPOSITORY_SHA256")
        self.assertEqual("1", actual_environment.pop("PYTHONDONTWRITEBYTECODE"))
        self.assertRegex(nonce, r"^[0-9a-f]{32}$")
        self.assertRegex(auth_key, r"^[0-9a-f]{64}$")
        self.assertRegex(repository_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(environment, actual_environment)

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
        run.assert_called_once()
        self.assertEqual(
            [sys.executable, str(check), "--static"], run.call_args.args[0]
        )
        actual_environment = run.call_args.kwargs["environment"]
        self.assertEqual(
            {
                "KIT_VERIFY_NONCE",
                "KIT_VERIFY_AUTH_KEY",
                "KIT_VERIFY_REPOSITORY_SHA256",
                "PYTHONDONTWRITEBYTECODE",
            },
            set(actual_environment),
        )
        self.assertRegex(
            actual_environment["KIT_VERIFY_NONCE"], r"^[0-9a-f]{32}$"
        )
        self.assertRegex(
            actual_environment["KIT_VERIFY_AUTH_KEY"], r"^[0-9a-f]{64}$"
        )
        self.assertRegex(
            actual_environment["KIT_VERIFY_REPOSITORY_SHA256"], r"^[0-9a-f]{64}$"
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

    def test_unresolved_native_warning_requires_exact_run_bound_confirmation(self) -> None:
        identity = "a" * 64
        warning = kit.native_engine.NativeWarningSnapshot(
            kit.native_engine.WARNING_UNRESOLVED,
            identity,
        )
        engine_path = ROOT / "Godot_v4.7.2-stable_win64_console.exe"
        engine = kit.engine_discovery.EngineSelection(
            engine_path,
            "project-adjacent",
            selected_version="4.7.2",
        )
        with mock.patch.object(
            kit.native_engine, "snapshot_native_warning", return_value=warning
        ), mock.patch.object(
            kit.engine_discovery, "select_godot", return_value=engine
        ), mock.patch.object(kit.native_engine, "authorize_recovery_retry") as authorize, \
                mock.patch.object(kit, "_run_process") as captured, \
                mock.patch.object(kit, "_run_process_inherited") as inherited:
            code, output = self.invoke(
                "verify", "--project", str(ROOT), "--json"
            )

        self.assertEqual(kit.EXIT_REFUSED, code)
        self.assertEqual(
            "native_retry_confirmation_required",
            json.loads(output)["status"],
        )
        authorize.assert_not_called()
        captured.assert_not_called()
        inherited.assert_not_called()

    def test_exact_native_warning_confirmation_is_private_and_run_bound(self) -> None:
        identity = "b" * 64
        token = "c" * 64
        warning = kit.native_engine.NativeWarningSnapshot(
            kit.native_engine.WARNING_UNRESOLVED,
            identity,
        )
        engine_path = ROOT / "Godot_v4.7.2-stable_win64_console.exe"
        engine = kit.engine_discovery.EngineSelection(
            engine_path,
            "project-adjacent",
            selected_version="4.7.2",
        )
        gate = self.completed(stdout="PASS  smoke\nGATE PASSED\n")
        with mock.patch.object(
            kit.native_engine, "snapshot_native_warning", return_value=warning
        ), mock.patch.object(
            kit.engine_discovery, "select_godot", return_value=engine
        ), mock.patch.object(
            kit.native_engine, "authorize_recovery_retry", return_value=token
        ) as authorize, mock.patch.object(
            kit, "_run_process", return_value=gate
        ) as run:
            code, output = self.invoke(
                "verify",
                "--confirm-native-retry",
                identity,
                "--project",
                str(ROOT),
                "--json",
            )

        self.assertEqual(kit.EXIT_OK, code)
        payload = json.loads(output)
        self.assertNotIn(token, json.dumps(payload))
        environment = run.call_args.kwargs["environment"]
        self.assertEqual(token, environment["KIT_NATIVE_RETRY_TOKEN"])
        nonce = environment["KIT_VERIFY_NONCE"]
        authorize.assert_called_once_with(ROOT.resolve(), identity, nonce)

    def test_static_verification_remains_available_after_a_native_warning(self) -> None:
        warning = kit.native_engine.NativeWarningSnapshot(
            kit.native_engine.WARNING_UNRESOLVED,
            "d" * 64,
        )
        gate = self.completed(stdout="PASS  integrity\nGATE PASSED\n")
        with mock.patch.object(
            kit.native_engine, "snapshot_native_warning", return_value=warning
        ), mock.patch.object(
            kit.native_engine, "authorize_recovery_retry"
        ) as authorize, mock.patch.object(
            kit.engine_discovery,
            "select_godot",
            side_effect=AssertionError("static verification discovered Godot"),
        ), mock.patch.object(kit, "_run_process", return_value=gate):
            code, _output = self.invoke(
                "verify", "--static", "--project", str(ROOT), "--json"
            )

        self.assertEqual(kit.EXIT_OK, code)
        authorize.assert_not_called()

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

    def test_invalid_stage_is_refused_before_native_retry_authorization(self) -> None:
        identity = "e" * 64
        warning = kit.native_engine.NativeWarningSnapshot(
            kit.native_engine.WARNING_UNRESOLVED,
            identity,
        )
        with mock.patch.object(
            kit.native_engine, "snapshot_native_warning", return_value=warning
        ), mock.patch.object(
            kit.native_engine, "authorize_recovery_retry"
        ) as authorize, mock.patch.object(
            kit.engine_discovery,
            "select_godot",
            side_effect=AssertionError("invalid scope discovered Godot"),
        ), mock.patch.object(kit, "_run_process") as captured, mock.patch.object(
            kit, "_run_process_inherited"
        ) as inherited:
            code, output = self.invoke(
                "verify",
                "--stage",
                "bad/stage",
                "--confirm-native-retry",
                identity,
                "--project",
                str(ROOT),
                "--json",
            )

        self.assertEqual(kit.EXIT_REFUSED, code)
        self.assertEqual("invalid_stage", json.loads(output)["status"])
        authorize.assert_not_called()
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
                "KIT_SELF_TEST": "1",
                "KIT_VERIFY_NONCE": "",
                "KIT_VERIFY_AUTH_KEY": "",
                "KIT_VERIFY_REPOSITORY_SHA256": "",
                "KIT_NATIVE_RETRY_TOKEN": "",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        inherited.assert_not_called()

    def test_self_test_marker_prevents_nested_verify_evidence_writes(self) -> None:
        result = {
            "ok": False,
            "command": "verify",
            "status": "failed",
            "strict": False,
            "stages": [],
            "skips": [],
        }
        handler = mock.Mock(return_value=(kit.EXIT_FAILED, result, ["verify: failed"]))
        with mock.patch.dict(kit._HANDLERS, {"verify": handler}), mock.patch.dict(
            os.environ, {"KIT_SELF_TEST": "1"}
        ), mock.patch.object(kit.cockpit, "record_verification") as record:
            code, output = self.invoke(
                "verify",
                "--project",
                str(ROOT),
                "--json",
                record_verification=True,
            )

        self.assertEqual(kit.EXIT_FAILED, code)
        self.assertNotIn("verification_record", json.loads(output))
        record.assert_not_called()

    def test_verify_maps_a_child_failure_to_one(self) -> None:
        gate = self.completed(returncode=2, stderr="godot unavailable\n")
        with mock.patch.object(kit, "_run_process", return_value=gate):
            code, output = self.invoke("verify", "--project", str(ROOT), "--json")
        self.assertEqual(kit.EXIT_FAILED, code)
        self.assertEqual("failed", json.loads(output)["status"])

    def test_gate_summary_is_bound_to_nonce_and_keeps_start_failures(self) -> None:
        nonce = "a" * 32
        auth_key = "b" * 64
        repository_sha256 = "c" * 64
        raw = self.signed_gate_summary(
            nonce,
            auth_key,
            repository_sha256,
            failed=True,
            diagnostics={
                "engine_start_failures": [{
                    "code": "engine-start-failed",
                    "executable": "Godot.exe",
                    "exit_code": 127,
                    "private": "must-not-cross",
                }]
            },
        )
        with mock.patch.object(
            Path, "read_text", return_value=json.dumps(raw)
        ):
            summary = kit._read_gate_summary(
                ROOT, nonce, auth_key, repository_sha256
            )
            mismatched = kit._read_gate_summary(
                ROOT, "b" * 32, auth_key, repository_sha256
            )

        self.assertIsNotNone(summary)
        self.assertEqual(nonce, summary["run_id"])
        self.assertEqual(
            [{
                "code": "engine-start-failed",
                "executable": "Godot.exe",
                "exit_code": 127,
            }],
            summary["diagnostics"]["engine_start_failures"],
        )
        self.assertIsNone(mismatched)

    def test_gate_summary_rejects_numeric_failed_state(self) -> None:
        nonce = "a" * 32
        auth_key = "b" * 64
        repository_sha256 = "c" * 64
        raw = {
            "schema": 2,
            "run_id": nonce,
            "repository_sha256": repository_sha256,
            "failed": 0,
            "results": ["PASS synthetic"],
            "diagnostics": {},
        }
        canonical = json.dumps(
            raw, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        raw["auth_sha256"] = hmac.new(
            bytes.fromhex(auth_key), canonical, hashlib.sha256
        ).hexdigest()
        with mock.patch.object(Path, "read_text", return_value=json.dumps(raw)):
            self.assertIsNone(
                kit._read_gate_summary(
                    ROOT, nonce, auth_key, repository_sha256
                )
            )

    def test_gate_summary_rejects_forged_authentication_or_repository(self) -> None:
        nonce = "a" * 32
        auth_key = "b" * 64
        repository_sha256 = "c" * 64
        raw = self.signed_gate_summary(nonce, auth_key, repository_sha256)
        with mock.patch.object(Path, "read_text", return_value=json.dumps(raw)):
            self.assertIsNone(
                kit._read_gate_summary(
                    ROOT, nonce, "d" * 64, repository_sha256
                )
            )
            self.assertIsNone(
                kit._read_gate_summary(
                    ROOT, nonce, auth_key, "e" * 64
                )
            )

    def test_strict_report_requires_consistent_statuses_and_gate_receipt(self) -> None:
        base = {
            "schema": 1,
            "command": "strict-verify",
            "ok": True,
            "status": "passed",
            "exit_code": 0,
            "project": str(ROOT.resolve()),
            "authority_receipt": {
                "receipt_trust": "no-exact-authority-event",
                "receipt_reasons": ["fixture"],
            },
            "stages": [
                {"name": "source-state", "status": "passed"},
                {"name": "gate", "status": "passed"},
            ],
        }
        receipt = {"schema": 2}
        self.assertTrue(kit._strict_report_is_valid(ROOT, base, 0, receipt))
        self.assertFalse(kit._strict_report_is_valid(ROOT, base, 0, None))
        contradictory = {**base, "status": "blocked", "exit_code": 3, "ok": False}
        self.assertFalse(
            kit._strict_report_is_valid(ROOT, contradictory, 3, receipt)
        )

    def test_strict_report_can_block_before_gate_without_a_receipt(self) -> None:
        report = {
            "schema": 1,
            "command": "strict-verify",
            "ok": False,
            "status": "blocked",
            "exit_code": 3,
            "project": str(ROOT.resolve()),
            "authority_receipt": {
                "receipt_trust": "no-exact-authority-event",
                "receipt_reasons": ["fixture"],
            },
            "stages": [
                {"name": "source-state", "status": "passed"},
                {"name": "doctor", "status": "blocked"},
                {"name": "gate", "status": "not_run"},
            ],
        }
        self.assertTrue(kit._strict_report_is_valid(ROOT, report, 3, None))
        self.assertFalse(kit._strict_report_is_valid(ROOT, report, 3, {}))

    def test_public_verification_lock_refuses_overlap(self) -> None:
        with kit._verification_run_lock(ROOT):
            with self.assertRaises(kit.CliError) as raised:
                with kit._verification_run_lock(ROOT):
                    pass
        self.assertEqual("verification_in_progress", raised.exception.status)

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
        run.assert_called_once()
        self.assertEqual(
            [sys.executable, str(check), "--only", "shape", "--only", "conformance"],
            run.call_args.args[0],
        )
        actual_environment = run.call_args.kwargs["environment"]
        self.assertEqual(
            {
                "KIT_VERIFY_NONCE",
                "KIT_VERIFY_AUTH_KEY",
                "KIT_VERIFY_REPOSITORY_SHA256",
                "PYTHONDONTWRITEBYTECODE",
            },
            set(actual_environment),
        )
        self.assertRegex(
            actual_environment["KIT_VERIFY_NONCE"], r"^[0-9a-f]{32}$"
        )
        self.assertRegex(
            actual_environment["KIT_VERIFY_AUTH_KEY"], r"^[0-9a-f]{64}$"
        )
        self.assertRegex(
            actual_environment["KIT_VERIFY_REPOSITORY_SHA256"], r"^[0-9a-f]{64}$"
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
        engine_path = ROOT / "Godot_v4.7.2-stable_win64_console.exe"
        selected = kit.engine_discovery.EngineSelection(
            engine_path,
            "project-adjacent",
            selected_version="4.7.2",
        )
        cases = (
            (("godot-docs", "build"),
             [sys.executable, str(docs), "--build", "--engine", str(engine_path)], 360),
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
            ) as run, mock.patch.object(
                kit.engine_discovery, "select_godot", return_value=selected
            ):
                code, _output = self.invoke(
                    *public, "--project", str(ROOT), "--json"
                )
            self.assertEqual(kit.EXIT_OK, code)
            run.assert_called_once_with(
                delegated, cwd=ROOT.resolve(), timeout=timeout
            )

    def test_gdls_public_commands_hide_internal_entry_point(self) -> None:
        tool = ROOT / "tools" / "gdls.py"
        engine_path = ROOT / "Godot_v4.7.2-stable_win64_console.exe"
        selected = kit.engine_discovery.EngineSelection(
            engine_path,
            "project-adjacent",
            selected_version="4.7.2",
        )
        cases = (
            (
                ("gdls", "start"),
                [sys.executable, str(tool), "--engine", str(engine_path), "start"],
                120,
            ),
            (("gdls", "status"), [sys.executable, str(tool), "status"], 60),
            (("gdls", "stop"), [sys.executable, str(tool), "stop"], 60),
            (
                ("gdls", "refs", "Mover"),
                [sys.executable, str(tool), "refs", "Mover"],
                60,
            ),
        )
        for public, delegated, timeout in cases:
            with self.subTest(public=public), mock.patch.object(
                kit,
                "_run_process",
                return_value=self.completed(stdout='{"status":"ok"}\n'),
            ) as run, mock.patch.object(
                kit.engine_discovery,
                "select_godot",
                return_value=selected,
            ) as select:
                code, output = self.invoke(
                    *public, "--project", str(ROOT), "--json"
                )
            self.assertEqual(kit.EXIT_OK, code)
            self.assertEqual("ok", json.loads(output)["status"])
            run.assert_called_once_with(
                delegated,
                cwd=ROOT.resolve(),
                timeout=timeout,
                allow_child_breakaway=public[1] == "start",
            )
            if public[1] != "start":
                select.assert_not_called()

    def test_plan_routes_both_views_without_starting_the_board(self) -> None:
        rendered = {"ok": True, "processes": {"plan": {}, "retro": {}}}
        with mock.patch.object(
                kit.cockpit, "regenerate_views", return_value=rendered
        ) as regenerate:
            code, output = self.invoke("plan", "--project", str(ROOT), "--json")
        self.assertEqual(kit.EXIT_OK, code)
        payload = json.loads(output)
        self.assertEqual("regenerated", payload["status"])
        self.assertEqual(str(ROOT / "plan.html"), payload["paths"]["plan"])
        self.assertEqual(str(ROOT / "retro.html"), payload["paths"]["retro"])
        self.assertTrue(payload["review_uri"].startswith("file:///"))
        regenerate.assert_called_once_with(ROOT.resolve(), snapshot="")

    def test_plan_snapshot_uses_one_safe_label_for_render_and_result_path(self) -> None:
        rendered = {"ok": True, "processes": {"plan": {}, "retro": {}}}
        with mock.patch.object(
                kit.cockpit, "regenerate_views", return_value=rendered
        ) as regenerate:
            code, output = self.invoke(
                "plan", "--snapshot", "review/one", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        self.assertIsNone(json.loads(output)["snapshot"])
        regenerate.assert_called_once_with(ROOT.resolve(), snapshot="review-one")

    def test_plan_fails_when_either_view_fails(self) -> None:
        result = {"ok": False, "processes": {
            "plan": {"stdout": "", "stderr": ""},
            "retro": {"stdout": "", "stderr": "bad retro"},
        }}
        with mock.patch.object(kit.cockpit, "regenerate_views", return_value=result):
            code, output = self.invoke(
                "plan", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_FAILED, code)
        self.assertEqual("failed", json.loads(output)["status"])

    def test_serve_routes_to_board_ensure_and_returns_url(self) -> None:
        tool = ROOT / "tools" / "board.py"
        result = self.completed(stdout=(
            "board: recovered a stale receipt before start\n"
            + json.dumps({
                "ok": True,
                "status": "running",
                "url": "http://127.0.0.1:54321/",
                "review_url": "http://127.0.0.1:54321/plan.html",
            })
            + "\n"
        ))
        with mock.patch.object(
                kit, "_plan", return_value=(kit.EXIT_OK, {"status": "regenerated"}, [])
        ) as plan, mock.patch.object(kit, "_run_process", return_value=result) as run:
            code, output = self.invoke("serve", "--project", str(ROOT), "--json")
        self.assertEqual(kit.EXIT_OK, code)
        self.assertEqual("http://127.0.0.1:54321/", json.loads(output)["url"])
        self.assertEqual(
            "http://127.0.0.1:54321/plan.html", json.loads(output)["review_url"]
        )
        plan.assert_called_once()
        run.assert_called_once_with(
            [sys.executable, str(tool), "--ensure", "--json"],
            cwd=ROOT.resolve(),
            timeout=60,
            allow_child_breakaway=True,
        )

    def test_serve_human_output_prints_the_complete_review_url(self) -> None:
        tool = ROOT / "tools" / "board.py"
        review_url = "http://127.0.0.1:54321/plan.html"
        result = self.completed(stdout=json.dumps({
            "ok": True,
            "status": "running",
            "url": "http://127.0.0.1:54321/",
            "review_url": review_url,
        }))
        with mock.patch.object(
                kit, "_plan", return_value=(kit.EXIT_OK, {}, [])
        ), mock.patch.object(kit, "_run_process", return_value=result) as run:
            code, output = self.invoke("serve", "--project", str(ROOT))
        self.assertEqual(kit.EXIT_OK, code)
        self.assertEqual(f"serve: running\n  {review_url}\n", output)
        run.assert_called_once_with(
            [sys.executable, str(tool), "--ensure", "--json"],
            cwd=ROOT.resolve(),
            timeout=60,
            allow_child_breakaway=True,
        )

    def test_serve_status_does_not_regenerate_or_open(self) -> None:
        tool = ROOT / "tools" / "board.py"
        result = self.completed(stdout=json.dumps({
            "ok": True, "status": "stopped", "url": None, "review_url": None,
        }))
        with mock.patch.object(kit, "_plan") as plan, \
                mock.patch.object(kit, "_run_process", return_value=result) as run:
            code, output = self.invoke(
                "serve", "status", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        self.assertEqual("stopped", json.loads(output)["status"])
        plan.assert_not_called()
        run.assert_called_once_with(
            [sys.executable, str(tool), "--status", "--json"],
            cwd=ROOT.resolve(),
            timeout=60,
            allow_child_breakaway=False,
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

    def test_retro_status_human_output_exposes_urgency_codes_and_progress(self) -> None:
        result = self.completed(stdout=json.dumps({
            "unarchived": 3,
            "threshold": 10,
            "remaining": 7,
            "progress": 0.3,
            "due": True,
            "trigger_level": "immediate",
            "immediate_consequences": [{"code": "native-crash", "notes": ["a.md"]}],
            "prompt_triggers": [{"code": "wrong-built", "notes": ["b.md"]}],
            "warnings": [{"code": "retro_note_unreadable", "note": "c.md"}],
            "archived": 2,
            "notes": ["a.md", "b.md", "c.md"],
        }))
        with mock.patch.object(kit, "_run_process", return_value=result):
            code, output = self.invoke("retro", "status", "--project", str(ROOT))

        self.assertEqual(kit.EXIT_OK, code)
        self.assertIn("retro: due", output)
        self.assertIn("progress: 3/10 notes (7 remaining; 30%)", output)
        self.assertIn("urgency: immediate", output)
        self.assertIn("immediate codes: native-crash", output)
        self.assertIn("prompt codes: wrong-built", output)
        self.assertIn("status warnings: retro_note_unreadable", output)

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

    def test_retro_run_forwards_only_an_explicit_session_window(self) -> None:
        tool = ROOT / "tools" / "retro.py"
        result = self.completed(stdout="evidence prepared\n")
        with mock.patch.object(kit, "_run_process", return_value=result) as run:
            code, output = self.invoke(
                "retro", "run",
                "--since", "2026-01-01T00:00:00Z", "--limit", "7",
                "--project", str(ROOT), "--json",
            )

        self.assertEqual(kit.EXIT_OK, code)
        payload = json.loads(output)
        self.assertEqual(payload["capture_window"], {
            "since": "2026-01-01T00:00:00Z",
            "limit": 7,
            "explicit": True,
        })
        run.assert_called_once_with(
            [sys.executable, str(tool), "--print", "--force",
             "--since", "2026-01-01T00:00:00Z", "--limit", "7"],
            cwd=ROOT.resolve(), timeout=3600, allow_provider=False,
        )

    def test_retro_run_rejects_nonpositive_session_limit_before_capture(self) -> None:
        with mock.patch.object(kit, "_run_process") as run:
            code, output = self.invoke(
                "retro", "run", "--limit", "0",
                "--project", str(ROOT), "--json",
            )

        self.assertEqual(kit.EXIT_REFUSED, code)
        self.assertEqual(json.loads(output)["status"], "invalid_capture_window")
        run.assert_not_called()

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

    def test_automatic_retro_reports_known_blocker_before_spend_confirmation(self) -> None:
        provider = kit.providers.ProviderSpec("analyzer", "codex", "small")
        with mock.patch.object(kit.providers, "selection", return_value=provider), \
                mock.patch.object(
                    kit.providers,
                    "preflight",
                    return_value=["Codex repository-read scope cannot be proven"],
                ) as preflight, mock.patch.object(kit, "_run_process") as run:
            code, output = self.invoke(
                "retro", "run", "--project", str(ROOT), "--json"
            )

        self.assertEqual(kit.EXIT_REFUSED, code)
        payload = json.loads(output)
        self.assertEqual("provider_unavailable", payload["status"])
        self.assertIn("repository-read scope", payload["error"])
        preflight.assert_called_once_with(provider)
        run.assert_not_called()

    def test_retro_publish_validates_then_refreshes_both_views(self) -> None:
        rank_tool = ROOT / "tools" / "retro_rank.py"
        completion_tool = ROOT / "tools" / "retro.py"
        report = ROOT / "docs" / "retro" / "2026-08-27-fixture-findings.md"
        plan_payload = {"status": "regenerated"}
        with mock.patch.object(
                kit,
                "_run_process",
                side_effect=[
                    self.completed(stdout="ranked\n"),
                    self.completed(stdout="retrospective snapshot completed\n"),
                ],
        ) as run, mock.patch.object(
                kit, "_plan", return_value=(kit.EXIT_OK, plan_payload, [])
        ) as plan, mock.patch.object(
                kit, "_resolve_retro_report", return_value=report
        ) as resolve:
            code, output = self.invoke(
                "retro", "publish", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_OK, code)
        payload = json.loads(output)
        self.assertEqual("published", payload["status"])
        self.assertEqual(str(report), payload["report"])
        resolve.assert_called_once_with(ROOT.resolve(), None)
        self.assertEqual(
            [
                mock.call(
                    [sys.executable, str(rank_tool), str(report)],
                    cwd=ROOT.resolve(),
                    timeout=180,
                ),
                mock.call(
                    [
                        sys.executable,
                        str(completion_tool),
                        "--complete-published",
                        str(report),
                    ],
                    cwd=ROOT.resolve(),
                    timeout=180,
                ),
            ],
            run.call_args_list,
        )
        plan.assert_called_once()

    def test_retro_publish_does_not_close_when_rendering_fails(self) -> None:
        report = ROOT / "docs" / "retro" / "2026-08-27-fixture-findings.md"
        with mock.patch.object(
                kit, "_resolve_retro_report", return_value=report
        ), mock.patch.object(
                kit, "_run_process", return_value=self.completed(stdout="ranked\n")
        ) as run, mock.patch.object(
                kit, "_plan", return_value=(kit.EXIT_FAILED, {"status": "failed"}, [])
        ):
            code, output = self.invoke(
                "retro", "publish", "--project", str(ROOT), "--json"
            )

        self.assertEqual(kit.EXIT_FAILED, code)
        self.assertEqual("render_failed", json.loads(output)["status"])
        run.assert_called_once()

    def test_retro_publish_reports_transactional_completion_failure(self) -> None:
        report = ROOT / "docs" / "retro" / "2026-08-27-fixture-findings.md"
        with mock.patch.object(
                kit, "_resolve_retro_report", return_value=report
        ), mock.patch.object(
                kit,
                "_run_process",
                side_effect=[
                    self.completed(stdout="ranked\n"),
                    self.completed(
                        returncode=1,
                        stderr="validated findings changed during completion\n",
                    ),
                ],
        ), mock.patch.object(
                kit, "_plan", return_value=(kit.EXIT_OK, {"status": "regenerated"}, [])
        ):
            code, output = self.invoke(
                "retro", "publish", "--project", str(ROOT), "--json"
            )

        self.assertEqual(kit.EXIT_FAILED, code)
        payload = json.loads(output)
        self.assertEqual("completion_failed", payload["status"])
        self.assertIn("validated findings changed", payload["completion"]["stderr"])

    def test_retro_publish_refuses_a_report_outside_docs_retro(self) -> None:
        with mock.patch.object(kit, "_run_process") as run:
            code, output = self.invoke(
                "retro", "publish", str(ROOT / "README.md"),
                "--project", str(ROOT), "--json",
            )
        self.assertEqual(kit.EXIT_REFUSED, code)
        self.assertEqual("report_outside_retro", json.loads(output)["status"])
        run.assert_not_called()

    def test_retro_publish_refuses_when_no_exact_findings_report_exists(self) -> None:
        with mock.patch.object(Path, "glob", return_value=[]), \
                mock.patch.object(kit, "_run_process") as run:
            code, output = self.invoke(
                "retro", "publish", "--project", str(ROOT), "--json"
            )
        self.assertEqual(kit.EXIT_REFUSED, code)
        self.assertEqual("report_unavailable", json.loads(output)["status"])
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
        completed = kit.process_supervisor.SupervisedResult(
            0, "ok\n", "", 0.01
        )
        with mock.patch.object(
            kit.process_supervisor, "run_supervised", return_value=completed
        ) as run:
            actual = kit._run_process(
                [sys.executable, "check.py", "--list"], cwd=ROOT, timeout=30
            )
        self.assertEqual(0, actual.returncode)
        self.assertEqual("ok\n", actual.stdout)
        run.assert_called_once_with(
            [sys.executable, "check.py", "--list"],
            cwd=ROOT,
            timeout=30,
            environment=None,
            capture_output=True,
            allow_child_breakaway=False,
        )

    def test_human_verifier_runner_inherits_live_output_and_never_a_shell(self) -> None:
        completed = kit.process_supervisor.SupervisedResult(0, None, None, 0.01)
        with mock.patch.object(
            kit.process_supervisor, "run_supervised", return_value=completed
        ) as run:
            actual = kit._run_process_inherited(
                [sys.executable, "check.py", "--static"], cwd=ROOT, timeout=30
            )
        self.assertEqual(0, actual.returncode)
        self.assertIsNone(actual.stdout)
        run.assert_called_once_with(
            [sys.executable, "check.py", "--static"],
            cwd=ROOT,
            timeout=30,
            environment=None,
            capture_output=False,
            allow_child_breakaway=False,
        )

    def test_process_runner_fails_closed_when_tree_termination_is_unverified(self) -> None:
        outcome = kit.process_supervisor.SupervisedResult(
            None,
            "partial",
            "",
            30.0,
            timed_out=True,
            termination_verified=False,
        )
        with mock.patch.object(
            kit.process_supervisor, "run_supervised", return_value=outcome
        ), self.assertRaises(kit.CliError) as raised:
            kit._run_process(
                [sys.executable, "check.py"], cwd=ROOT, timeout=30
            )
        self.assertEqual("containment_unverified", raised.exception.status)

    def test_missing_project_is_a_machine_readable_refusal(self) -> None:
        missing = ROOT / ".definitely-not-a-project-for-kit-cli-tests"
        code, output = self.invoke("doctor", "--project", str(missing), "--json")
        self.assertEqual(kit.EXIT_REFUSED, code)
        self.assertEqual("project_unavailable", json.loads(output)["status"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
