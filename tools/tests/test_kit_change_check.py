#!/usr/bin/env python3
"""Offline post-apply proof for managed install and upgrade."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import brownfield  # noqa: E402
import kit_change_check as check  # noqa: E402
import release  # noqa: E402


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class OfflineKitChangeCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="kit-change-check-")
        self.target = Path(self.temporary.name).resolve() / "project"
        self.target.mkdir()
        self.core = self.target / ".agent-kit" / "releases" / ("a" * 64)
        self.core.mkdir(parents=True)
        (self.core / "kit.py").write_text("# kit\n", encoding="utf-8")
        (self.core / "check.py").write_text("# check\n", encoding="utf-8")
        (self.core / "tools").mkdir()
        (self.core / "tools" / "managed_launcher.py").write_text(
            "# managed launcher\n", encoding="utf-8"
        )
        launcher_contents = {
            ".agent-kit/launcher.py": b"# launcher\n",
            "kit": b"#!/bin/sh\n",
            "kit.cmd": b"@echo off\r\n",
        }
        surfaces = []
        for index, (relative, content) in enumerate(launcher_contents.items()):
            path = self.target.joinpath(*relative.split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            surfaces.append({
                "id": f"launcher-{index}",
                "path": relative,
                "strategy": "replace",
                "base_sha256": "0" * 64,
                "applied_sha256": _sha256(content),
            })
        self.current = {
            "installation_id": "1" * 32,
            "active_release": {"archive_sha256": "a" * 64},
            "managed_surfaces": surfaces,
        }
        self.current_path = self.target / ".agent-kit" / "current.json"
        self.current_path.write_bytes(_canonical(self.current))
        self.runtime = self.target / ".kit" / "runtime"
        self.game = self.target / "game" / "player.gd"
        self.game.parent.mkdir(parents=True)
        self.game.write_text("extends Node\n", encoding="utf-8")
        self.issues: list[dict[str, object]] = []
        self.returncodes = {"self-test": 0, "scan": 0, "verify": 0}
        self.self_test_failure: dict[str, object] = {
            "ran": 1,
            "failures": 1,
            "errors": 0,
            "skipped": 0,
            "test_ids": ["tests.test_release.ReleaseTests.test_release_copy"],
            "skip_ids": [],
            "log": {
                "available": True,
                "path": ".kit/runtime/self-test/failures/failure-a.log",
                "bytes": 120,
                "sha256": "f" * 64,
                "truncated": False,
            },
        }
        self.self_test_cli_error: dict[str, object] | None = None
        self.malformed_verify = False
        self.skips: list[str] = []
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.installation = SimpleNamespace(
            mode="managed",
            project_root=self.target,
            core_root=self.core,
            release_sha256="a" * 64,
            current_path=self.current_path,
        )
        self.context = SimpleNamespace(
            install_mode="managed",
            project_root=self.target,
            core_root=self.core,
            runtime_root=self.runtime,
        )
        self.session = {
            "session_id": "b" * 64,
            "release": {"archive_sha256": "a" * 64},
            "request": {"mode": "install"},
            "preview": {"raw": {"material": {"changes": []}}},
            "baseline": {
                "prior": {"exists": False, "sha256": "", "content_base64": ""},
                "generated": None,
            },
        }
        self.patches = [
            mock.patch.object(
                check.managed_launcher,
                "resolve_installation",
                return_value=self.installation,
            ),
            mock.patch.object(
                check.project_context,
                "load_configured_context",
                return_value=self.context,
            ),
            mock.patch.object(
                check.managed_launcher,
                "bound_environment",
                side_effect=lambda _installation, base: dict(base),
            ),
            mock.patch.object(
                check.release,
                "read_verified_directory",
                return_value=({"archive_sha256": "a" * 64}, {}),
            ),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    @staticmethod
    def _process_outcome(returncode: int, stdout: str = "") -> SimpleNamespace:
        return SimpleNamespace(
            returncode=returncode,
            stdout=stdout,
            stderr="",
            termination_verified=True,
            timed_out=False,
            cancelled=False,
            launch_error=None,
        )

    def _runner(self, command: list[str], **kwargs: object) -> SimpleNamespace:
        self.commands.append(list(command))
        environment = kwargs.get("environment")
        if isinstance(environment, dict):
            self.environments.append(dict(environment))
        if Path(command[0]).name.casefold() in {"git", "git.exe"}:
            return self._process_outcome(0)
        if "self-test" in command:
            copied_kit = next(
                Path(part) for part in command if str(part).endswith("kit.py")
            )
            self.assertTrue((copied_kit.parent / "src").is_dir())
            code = self.returncodes["self-test"]
            if self.self_test_cli_error is not None:
                return self._process_outcome(
                    code,
                    json.dumps(self.self_test_cli_error),
                )
            receipt: dict[str, object] = {
                "command": "self-test",
                "ok": code == 0,
                "exit_code": code,
                "status": "passed" if code == 0 else "failed",
                "project": str(self.target),
                "engine": "disabled",
                "process": {"exit_code": code},
                "tests": {
                    "ran": 1,
                    "failures": 0 if code == 0 else 1,
                    "errors": 0,
                    "skipped": 0,
                },
            }
            if code != 0:
                receipt["failure"] = self.self_test_failure
            return self._process_outcome(code, json.dumps(receipt))
        if "__brownfield-scan" in command:
            code = self.returncodes["scan"]
            if code == 0:
                output = Path(command[command.index("__brownfield-scan") + 1])
                output.write_bytes(brownfield.canonical_json(
                    brownfield.build_scan_document(self.issues)
                ))
            return self._process_outcome(code, "brownfield scan\n")
        if "verify" in command:
            code = self.returncodes["verify"]
            if self.malformed_verify:
                return self._process_outcome(code, "{}")
            nonce = "c" * 32
            repository_sha = "d" * 64
            skip_by_stage = {item.split(maxsplit=1)[0]: item for item in self.skips}
            results = [
                f"SKIP  {skip_by_stage[stage]}"
                if stage in skip_by_stage
                else f"PASS  {stage}"
                for stage in check.STATIC_STAGES
            ]
            return self._process_outcome(code, json.dumps({
                "command": "verify",
                "ok": code == 0,
                "exit_code": code,
                "status": "passed" if code == 0 else "failed",
                "project": str(self.target),
                "strict": False,
                "static": True,
                "stages": [],
                "fast": False,
                "engine": None,
                "skips": self.skips,
                "verification_nonce": nonce,
                "process": {"exit_code": code},
                "repository_start": {"available": True, "digest": repository_sha},
                "repository_end": {"available": True, "digest": repository_sha},
                "repository_stable": True,
                "gate_summary": {
                    "schema": 2,
                    "run_id": nonce,
                    "repository_sha256": repository_sha,
                    "auth_sha256": "e" * 64,
                    "failed": code != 0,
                    "results": results,
                    "diagnostics": {
                        "native_crashes": [],
                        "engine_refusals": [],
                        "engine_start_failures": [],
                        "timeouts": [],
                    },
                },
            }))
        raise AssertionError(f"unexpected command: {command}")

    def _set_upgrade_prior(self, issues: list[dict[str, object]]) -> None:
        previous_release = "d" * 64
        prior = brownfield.canonical_json(brownfield.build_baseline(
            self.target,
            issues,
            installation_id=self.current["installation_id"],
            release_sha256=previous_release,
        ))
        baseline_path = self.target / brownfield.BASELINE_RELATIVE
        baseline_path.write_bytes(prior)
        self.session["request"] = {"mode": "upgrade"}
        self.session["preview"] = {
            "raw": {
                "material": {
                    "current": {"active_archive_sha256": previous_release},
                    "changes": [],
                },
            },
        }
        self.session["baseline"] = {
            "prior": {
                "exists": True,
                "sha256": _sha256(prior),
                "content_base64": base64.b64encode(prior).decode("ascii"),
            },
            "generated": None,
        }

    def _stored_baseline(self) -> dict[str, object]:
        content = brownfield.read_baseline_file(self.target)
        self.assertIsNotNone(content)
        return brownfield.validate_baseline(
            content or b"",
            installation_id=self.current["installation_id"],
            release_sha256=self.current["active_release"]["archive_sha256"],
        )

    def test_success_validates_and_writes_empty_baseline_without_touching_game(self) -> None:
        before = self.game.read_bytes()

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertTrue(result["kit_ok"])
        self.assertTrue(result["project_ok"])
        self.assertEqual(before, self.game.read_bytes())
        baseline = brownfield.read_baseline_file(self.target)
        self.assertIsNotNone(baseline)
        self.assertEqual(_sha256(baseline or b""), result["baseline_sha256"])
        joined = " ".join(part for command in self.commands for part in command).lower()
        self.assertNotIn("godot", joined)
        self.assertNotIn("http", joined)
        git_commands = self.commands[:3]
        self.assertTrue(
            all(Path(command[0]).name.casefold() in {"git", "git.exe"}
                for command in git_commands)
        )
        for command in git_commands:
            rendered = " ".join(command)
            self.assertIn("--no-pager", command)
            self.assertIn("core.fsmonitor=false", command)
            self.assertIn("diff.external=", command)
            self.assertIn("diff.trustExitCode=false", command)
            self.assertIn(f"core.hooksPath={os.devnull}", command)
            self.assertIn(f"core.attributesFile={os.devnull}", command)
            self.assertNotIn(str(self.target / "git"), rendered)
        for environment in self.environments[:3]:
            self.assertEqual(os.devnull, environment["GIT_CONFIG_GLOBAL"])
            self.assertEqual("1", environment["GIT_CONFIG_NOSYSTEM"])
            self.assertEqual("0", environment["GIT_OPTIONAL_LOCKS"])
            self.assertEqual("0", environment["GIT_TERMINAL_PROMPT"])
        self.assertIn("kit.py", " ".join(self.commands[3]))
        self.assertTrue(
            all(
                "managed_launcher.py" in " ".join(command)
                for command in self.commands[4:]
            )
        )
        self.assertNotIn("KIT_LIFECYCLE_CHECK", self.environments[3])
        for environment in self.environments[4:]:
            self.assertEqual(str(Path(sys.executable).resolve()), environment["KIT_PYTHON"])
            self.assertEqual("1", environment["KIT_LIFECYCLE_CHECK"])
            for unsafe in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONUSERBASE"):
                self.assertNotIn(unsafe, environment)

    def test_existing_issues_make_adoption_required_not_kit_failure(self) -> None:
        self.issues = [{
            "stage": "lint",
            "code": "legacy-warning",
            "path": "game/player.gd",
            "line": 1,
            "message_sha256": "c" * 64,
        }]

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertTrue(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertEqual(self.issues, result["existing_issues"])

    def test_issue_in_a_kit_changed_file_is_never_baselined(self) -> None:
        self.session["preview"]["raw"]["material"]["changes"] = [
            {
                "path": "ARCHITECTURE.md",
                "before": {"kind": "absent"},
                "after": {"kind": "file", "sha256": "a" * 64},
            }
        ]
        self.issues = [
            {
                "stage": "arch",
                "code": "stale-architecture-graph",
                "path": "ARCHITECTURE.md",
                "line": 0,
                "message_sha256": "b" * 64,
            }
        ]

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertIsNone(brownfield.read_baseline_file(self.target))
        self.assertIn("file changed by the kit", str(result["detail"]))

    def test_skipped_project_checks_make_adoption_required_not_kit_failure(
        self,
    ) -> None:
        self.skips = ["format", "lint", "tests (runner not configured)"]

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertTrue(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertEqual([], result["existing_issues"])
        self.assertIn("these project checks are not ready", str(result["detail"]))
        for skipped in self.skips:
            self.assertIn(skipped, str(result["detail"]))

    def test_malformed_skipped_checks_fail_the_installed_proof(self) -> None:
        self.skips = ["format\nforged"]

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertIn("malformed skipped checks", str(result["detail"]))

    def test_missing_skipped_check_list_fails_the_installed_proof(self) -> None:
        original_runner = self._runner

        def missing_skips(command: list[str], **kwargs: object) -> SimpleNamespace:
            if "verify" in command:
                outcome = original_runner(command, **kwargs)
                receipt = json.loads(outcome.stdout)
                receipt.pop("skips")
                return self._process_outcome(0, json.dumps(receipt))
            return original_runner(command, **kwargs)

        result = check.run(self.target, self.session, runner=missing_skips)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertIn("no skipped-check list", str(result["detail"]))

    def test_self_test_success_requires_the_exact_project_scope_and_counts(self) -> None:
        variants = (
            ("engine", lambda receipt: receipt.__setitem__("engine", "available")),
            ("project", lambda receipt: receipt.__setitem__("project", str(self.target.parent))),
            ("ran", lambda receipt: receipt["tests"].__setitem__("ran", 0)),
            ("skipped", lambda receipt: receipt["tests"].__setitem__("skipped", 1)),
            ("nested exit", lambda receipt: receipt["process"].__setitem__("exit_code", 1)),
        )
        original_runner = self._runner
        for label, mutate in variants:
            with self.subTest(label=label):
                def forged(command: list[str], **kwargs: object) -> SimpleNamespace:
                    outcome = original_runner(command, **kwargs)
                    if "self-test" not in command:
                        return outcome
                    receipt = json.loads(outcome.stdout)
                    mutate(receipt)
                    return self._process_outcome(0, json.dumps(receipt))

                result = check.run(self.target, self.session, runner=forged)
                self.assertFalse(result["kit_ok"])
                self.assertFalse(result["project_ok"])
                self.assertIsNone(brownfield.read_baseline_file(self.target))

    def test_static_success_requires_the_exact_scope_and_gate_evidence(self) -> None:
        base = json.loads(self._runner(["verify"]).stdout)
        variants = (
            ("static", lambda receipt: receipt.__setitem__("static", False)),
            ("strict", lambda receipt: receipt.__setitem__("strict", True)),
            ("stages", lambda receipt: receipt.__setitem__("stages", ["lint"])),
            ("fast", lambda receipt: receipt.__setitem__("fast", True)),
            ("project", lambda receipt: receipt.__setitem__("project", str(self.target.parent))),
            ("nested exit", lambda receipt: receipt["process"].__setitem__("exit_code", 1)),
            ("gate failed", lambda receipt: receipt["gate_summary"].__setitem__("failed", True)),
            (
                "gate identity",
                lambda receipt: receipt["gate_summary"].__setitem__("run_id", "f" * 32),
            ),
            ("empty stages", lambda receipt: receipt["gate_summary"].__setitem__("results", [])),
            (
                "duplicate stage",
                lambda receipt: receipt["gate_summary"]["results"].__setitem__(
                    1, "PASS  integrity"
                ),
            ),
            (
                "skip mismatch",
                lambda receipt: receipt["gate_summary"]["results"].__setitem__(
                    0, "SKIP  integrity"
                ),
            ),
        )
        for label, mutate in variants:
            with self.subTest(label=label):
                receipt = json.loads(json.dumps(base))
                mutate(receipt)
                with self.assertRaises(check.KitChangeCheckError):
                    check._validate_static_success(receipt, self.installation, 0)

    def test_wrong_static_scope_fails_with_the_written_baseline_identity(self) -> None:
        original_runner = self._runner

        def wrong_scope(command: list[str], **kwargs: object) -> SimpleNamespace:
            outcome = original_runner(command, **kwargs)
            if "verify" not in command:
                return outcome
            receipt = json.loads(outcome.stdout)
            receipt["static"] = False
            return self._process_outcome(0, json.dumps(receipt))

        result = check.run(self.target, self.session, runner=wrong_scope)

        self.assertFalse(result["kit_ok"])
        baseline = brownfield.read_baseline_file(self.target)
        self.assertIsNotNone(baseline)
        self.assertEqual(_sha256(baseline or b""), result["baseline_sha256"])

    def test_upgrade_carries_only_an_unchanged_existing_gap(self) -> None:
        issue = {
            "stage": "lint",
            "code": "legacy-warning",
            "path": "game/player.gd",
            "line": 1,
            "message_sha256": "c" * 64,
        }
        self.issues = [issue]
        self._set_upgrade_prior([issue])

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertTrue(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        stored = self._stored_baseline()
        self.assertEqual([issue], [
            {key: item[key] for key in check._PUBLIC_ISSUE_FIELDS}
            for item in stored["issues"]
        ])
        self.assertIn("unchanged existing", str(result["detail"]))

    def test_upgrade_drops_a_resolved_gap(self) -> None:
        old_issue = {
            "stage": "lint",
            "code": "legacy-warning",
            "path": "game/player.gd",
            "line": 1,
            "message_sha256": "c" * 64,
        }
        self._set_upgrade_prior([old_issue])

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertTrue(result["kit_ok"])
        self.assertTrue(result["project_ok"])
        self.assertEqual([], self._stored_baseline()["issues"])
        self.assertIn("1 old gap(s) resolved", str(result["detail"]))

    def test_upgrade_does_not_baseline_a_new_gap(self) -> None:
        new_issue = {
            "stage": "lint",
            "code": "new-warning",
            "path": "game/player.gd",
            "line": 2,
            "message_sha256": "e" * 64,
        }
        self.issues = [new_issue]
        self._set_upgrade_prior([])
        self.returncodes["verify"] = 1

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertEqual([], self._stored_baseline()["issues"])
        self.assertEqual([new_issue], result["existing_issues"])
        self.assertIn("static verification failed", str(result["detail"]))

    def test_managed_upgrade_without_a_prior_baseline_stops(self) -> None:
        self.session["request"] = {"mode": "upgrade"}
        self.session["preview"] = {
            "raw": {"material": {"current": {"mode": "managed"}, "changes": []}},
        }

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertIsNone(brownfield.read_baseline_file(self.target))
        self.assertIn("no prior brownfield baseline", str(result["detail"]))
        self.assertEqual(5, len(self.commands))

    def test_reviewed_legacy_upgrade_can_create_its_first_baseline(self) -> None:
        issue = {
            "stage": "lint",
            "code": "legacy-warning",
            "path": "game/player.gd",
            "line": 1,
            "message_sha256": "c" * 64,
        }
        self.issues = [issue]
        self.session["request"] = {"mode": "upgrade"}
        self.session["preview"] = {
            "raw": {"material": {"current": {"mode": "legacy"}, "changes": []}},
        }

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertTrue(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        stored = self._stored_baseline()
        self.assertEqual([issue], [
            {key: item[key] for key in check._PUBLIC_ISSUE_FIELDS}
            for item in stored["issues"]
        ])

    def test_upgrade_does_not_baseline_an_issue_after_its_file_changed(self) -> None:
        issue = {
            "stage": "lint",
            "code": "legacy-warning",
            "path": "game/player.gd",
            "line": 1,
            "message_sha256": "c" * 64,
        }
        self.issues = [issue]
        self._set_upgrade_prior([issue])
        self.game.write_text("extends Node\n# changed\n", encoding="utf-8")
        self.returncodes["verify"] = 1

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertEqual([], self._stored_baseline()["issues"])
        self.assertIn("static verification failed", str(result["detail"]))

    def test_upgrade_rechecks_a_file_changed_after_its_first_evaluation(self) -> None:
        issue = {
            "stage": "lint",
            "code": "legacy-warning",
            "path": "game/player.gd",
            "line": 1,
            "message_sha256": "c" * 64,
        }
        self.issues = [issue]
        self._set_upgrade_prior([issue])
        self.returncodes["verify"] = 1

        def mutate(name: str) -> None:
            if name == "after-upgrade-evaluation":
                self.game.write_text("extends Node\n# changed in race\n", encoding="utf-8")

        with mock.patch.object(check, "_failpoint", side_effect=mutate):
            result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertEqual([], self._stored_baseline()["issues"])
        self.assertIn("static verification failed", str(result["detail"]))

    def test_self_test_failure_stops_before_scan_and_baseline(self) -> None:
        self.returncodes["self-test"] = 1

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertEqual(4, len(self.commands))
        self.assertIsNone(brownfield.read_baseline_file(self.target))
        self.assertIn("test_release_copy", str(result["detail"]))
        self.assertIn("1 failed", str(result["detail"]))
        self.assertIn("private log", str(result["detail"]))

    def test_self_test_cli_error_surfaces_the_bounded_original_problem(self) -> None:
        self.returncodes["self-test"] = 3
        self.self_test_cli_error = {
            "ok": False,
            "command": "self-test",
            "status": "managed_core_untrusted",
            "error": "self-test cannot authenticate the running kit",
            "project": str(self.target),
            "exit_code": 3,
        }

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertIsNone(brownfield.read_baseline_file(self.target))
        self.assertIn("managed core untrusted", str(result["detail"]))
        self.assertIn("cannot authenticate the running kit", str(result["detail"]))

    def test_self_test_cli_error_rejects_extra_or_unbounded_fields(self) -> None:
        valid = {
            "ok": False,
            "command": "self-test",
            "status": "managed_core_untrusted",
            "error": "authentication failed",
            "project": str(self.target),
            "exit_code": 3,
        }
        for mutation in (
            lambda receipt: receipt.__setitem__("process", {"exit_code": 3}),
            lambda receipt: receipt.__setitem__("status", "Not Valid"),
            lambda receipt: receipt.__setitem__("error", "x" * 2_001),
            lambda receipt: receipt.__setitem__("error", "line one\nline two"),
        ):
            receipt = dict(valid)
            mutation(receipt)
            with self.subTest(receipt=receipt), self.assertRaises(
                check.KitChangeCheckError
            ):
                check._validate_self_test_receipt(receipt, self.installation, 3)

    def test_scan_failure_stops_without_baseline(self) -> None:
        self.returncodes["scan"] = 2

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertEqual(5, len(self.commands))
        self.assertIsNone(brownfield.read_baseline_file(self.target))

    def test_clean_scan_plus_static_failure_is_a_kit_failure(self) -> None:
        self.returncodes["verify"] = 1

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(result["project_ok"])
        self.assertTrue(brownfield.read_baseline_file(self.target))
        self.assertIn("static verification failed", str(result["detail"]))

    def test_malformed_static_proof_is_kit_failure_with_generated_identity(self) -> None:
        self.malformed_verify = True

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        baseline = brownfield.read_baseline_file(self.target)
        self.assertIsNotNone(baseline)
        self.assertEqual(_sha256(baseline or b""), result["baseline_sha256"])

    def test_third_baseline_version_is_never_overwritten(self) -> None:
        baseline_path = self.target / brownfield.BASELINE_RELATIVE
        baseline_path.write_bytes(b"third version\n")
        before = baseline_path.read_bytes()

        result = check.run(self.target, self.session, runner=self._runner)

        self.assertFalse(result["kit_ok"])
        self.assertEqual(before, baseline_path.read_bytes())

    def test_recovery_accepts_the_exact_generated_baseline(self) -> None:
        first = check.run(self.target, self.session, runner=self._runner)
        self.commands.clear()

        second = check.run(self.target, self.session, runner=self._runner)

        self.assertTrue(first["kit_ok"])
        self.assertTrue(second["kit_ok"])
        self.assertEqual(first["baseline_sha256"], second["baseline_sha256"])

    def test_real_children_ignore_target_and_pythonpath_startup_files(self) -> None:
        launcher = self.target / ".agent-kit" / "launcher.py"
        launcher_bytes = b"""#!/usr/bin/env python3
import base64
import json
import sys
from pathlib import Path

base64.b64encode(b\"proof\")
command = sys.argv[1]
if command == \"__brownfield-scan\":
    Path(sys.argv[2]).write_bytes(
        b'{\"complete\":true,\"errors\":[],\"issues\":[],\"kind\":'
        b'\"agent-kit-brownfield-scan\",\"schema\":1}\\n'
    )
    raise SystemExit(0)
if command in {\"self-test\", \"verify\"}:
    project = str(Path(sys.argv[sys.argv.index(\"--project\") + 1]).resolve())
    receipt = {
        \"command\": command,
        \"ok\": True,
        \"exit_code\": 0,
        \"status\": \"passed\",
        \"project\": project,
        \"process\": {\"exit_code\": 0},
    }
    if command == \"self-test\":
        receipt[\"engine\"] = \"disabled\"
        receipt[\"tests\"] = {
            \"ran\": 1,
            \"failures\": 0,
            \"errors\": 0,
            \"skipped\": 0,
        }
    else:
        nonce = \"c\" * 32
        repository_sha = \"d\" * 64
        receipt.update({
            \"strict\": False,
            \"static\": True,
            \"stages\": [],
            \"fast\": False,
            \"engine\": None,
            \"skips\": [],
            \"verification_nonce\": nonce,
            \"repository_start\": {\"available\": True, \"digest\": repository_sha},
            \"repository_end\": {\"available\": True, \"digest\": repository_sha},
            \"repository_stable\": True,
            \"gate_summary\": {
                \"schema\": 2,
                \"run_id\": nonce,
                \"repository_sha256\": repository_sha,
                \"auth_sha256\": \"e\" * 64,
                \"failed\": False,
                \"results\": [
                    \"PASS  \" + stage
                    for stage in (
                        \"integrity\", \"skills\", \"schema\", \"shape\",
                        \"design\", \"conformance\", \"format\", \"lint\",
                        \"sanitise\", \"grep\", \"types\", \"arch\",
                        \"tests\", \"assets\",
                    )
                ],
                \"diagnostics\": {
                    \"native_crashes\": [],
                    \"engine_refusals\": [],
                    \"engine_start_failures\": [],
                    \"timeouts\": [],
                },
            },
        })
    print(json.dumps(receipt))
    raise SystemExit(0)
raise SystemExit(4)
"""
        launcher.write_bytes(launcher_bytes)
        (self.core / "kit.py").write_bytes(launcher_bytes)
        (self.core / "tools" / "managed_launcher.py").write_bytes(launcher_bytes)
        launchers = {
            ".agent-kit/launcher.py": launcher_bytes,
            "kit": release._managed_unix_launcher(launcher_bytes),
            "kit.cmd": release._managed_windows_launcher(launcher_bytes),
        }
        for relative, content in launchers.items():
            path = self.target.joinpath(*relative.split("/"))
            path.write_bytes(content)
            if relative == "kit":
                path.chmod(0o755)
        for surface in self.current["managed_surfaces"]:
            surface["applied_sha256"] = _sha256(launchers[surface["path"]])
        self.current_path.write_bytes(_canonical(self.current))

        poison = self.target.parent / "python-poison"
        poison.mkdir()
        sentinels: list[Path] = []
        for root, label in ((self.target, "target"), (poison, "pythonpath")):
            for module in ("sitecustomize.py", "base64.py"):
                sentinel = self.target.parent / f"{label}-{module}.ran"
                sentinels.append(sentinel)
                (root / module).write_text(
                    "from pathlib import Path\n"
                    f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
                    "raise RuntimeError('unsafe Python startup input executed')\n",
                    encoding="utf-8",
                )

        trusted_git = check.process_supervisor.resolve_ordinary_executable(
            "git",
            excluded_roots=(self.target, self.core),
        )
        with mock.patch.dict(
            os.environ,
            {
                "COMSPEC": str(poison / "hostile-comspec.exe"),
                "PATH": f"{self.target}{os.pathsep}{poison}",
                "PYTHONPATH": f"{self.target}{os.pathsep}{poison}",
                "PYTHONSTARTUP": str(poison / "sitecustomize.py"),
                "PYTHONUSERBASE": str(poison),
            },
            clear=False,
        ), mock.patch.object(
            check.process_supervisor,
            "resolve_ordinary_executable",
            return_value=trusted_git,
        ):
            result = check.run(self.target, self.session)

        self.assertTrue(result["kit_ok"])
        self.assertTrue(result["project_ok"])
        self.assertTrue(all(not sentinel.exists() for sentinel in sentinels))

    def test_tampered_managed_launcher_is_not_executed(self) -> None:
        sentinel = self.target.parent / "tampered-launcher.ran"
        tampered = (
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n"
        ).encode("utf-8")
        (self.target / ".agent-kit" / "launcher.py").write_bytes(tampered)

        result = check.run(self.target, self.session)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(sentinel.exists())
        self.assertIsNone(brownfield.read_baseline_file(self.target))

    def test_tampered_root_command_launcher_is_refused_before_core_runs(self) -> None:
        sentinel = self.target.parent / "core-launcher.ran"
        (self.core / "tools" / "managed_launcher.py").write_text(
            "from pathlib import Path\n"
            f"Path({str(sentinel)!r}).write_text('executed', encoding='utf-8')\n",
            encoding="utf-8",
        )
        (self.target / "kit.cmd").write_bytes(b"@echo off\r\necho tampered\r\n")

        result = check.run(self.target, self.session)

        self.assertFalse(result["kit_ok"])
        self.assertFalse(sentinel.exists())
        self.assertEqual([], self.commands)
        self.assertIsNone(brownfield.read_baseline_file(self.target))


if __name__ == "__main__":
    unittest.main()
