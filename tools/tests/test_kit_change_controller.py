#!/usr/bin/env python3
"""Focused contracts for the managed install and upgrade controller."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(TESTS))

import kit_change_controller as controller  # noqa: E402
import test_kit_change as lifecycle_support  # noqa: E402


class SimulatedCrash(BaseException):
    """A process-ending interruption outside ordinary exception recovery."""


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


class KitChangeControllerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="kit-change-controller-")
        self.base = Path(self.temporary.name).resolve()
        self.runtime = self.base / "controller-runtime"
        self.target = self.base / "project"
        self.target.mkdir()
        self.archive = self.base / "release.zip"
        self.payload = b"canonical release payload\n"
        self.archive.write_bytes(self.payload)
        self.release_report = {
            "ok": True,
            "format": "zip",
            "version": "0.3.0",
            "archive_sha256": "a" * 64,
            "container_sha256": _sha256(self.payload),
        }
        self.members = {"one": object(), "two": object(), "three": object()}
        self.operation = "install"
        self.blockers: list[dict[str, object]] = []
        self.preview_epoch = 0
        self.changes: list[dict[str, object]] = [
            {
                "path": "kit.cmd",
                "action": "create",
                "ownership": "kit",
                "strategy": "replace",
            },
            {
                "path": "AGENTS.md",
                "action": "modify",
                "ownership": "shared",
                "strategy": "managed-block",
            },
            {
                "path": "tools/legacy.py",
                "action": "delete",
                "ownership": "kit",
                "strategy": "retire-file",
            },
        ]

        self.patches = [
            mock.patch.object(
                controller.release,
                "read_verified_archive",
                side_effect=lambda _path: (dict(self.release_report), self.members),
            ),
            mock.patch.object(
                controller.release,
                "read_verified_directory",
                side_effect=lambda _path: (dict(self.release_report), self.members),
            ),
            mock.patch.object(
                controller.release,
                "read_verified_controller",
                side_effect=lambda _path: (dict(self.release_report), self.members),
            ),
            mock.patch.object(
                controller.release,
                "materialize_verified_directory_zip",
                side_effect=self._materialize,
            ),
            mock.patch.object(
                controller.release,
                "verify_archive",
                side_effect=lambda _path: dict(self.release_report),
            ),
            mock.patch.object(
                controller.kit_change,
                "preview",
                side_effect=self._preview,
            ),
            mock.patch.object(
                controller.kit_change,
                "apply",
                side_effect=lambda _root, _archive, digest, **_kwargs: {
                    "ok": True,
                    "status": "applied",
                    "transaction_id": "1" * 32,
                    "preview_sha256": digest,
                },
            ),
            mock.patch.object(
                controller.kit_change,
                "rollback",
                side_effect=lambda _root, transaction_id=None: {
                    "ok": True,
                    "status": "rolled_back",
                    "transaction_id": transaction_id,
                    "preview_sha256": self._preview_sha(),
                },
            ),
            mock.patch.object(
                controller.kit_change,
                "resume",
                side_effect=lambda _root: {
                    "ok": True,
                    "status": "applied",
                    "transaction_id": "1" * 32,
                    "preview_sha256": self._preview_sha(),
                },
            ),
        ]
        for patcher in self.patches:
            patcher.start()

    def tearDown(self) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    def _material(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "target_scope_sha256": "e" * 64,
            "current": {
                "mode": "none" if self.operation == "install" else "managed",
                "kit_version": None if self.operation == "install" else "0.2.0",
            },
            "game_root": {"value": None if self.blockers else "src", "source": "test"},
            "target_release": {
                "kit_version": "0.3.0",
                "archive_sha256": self.release_report["archive_sha256"],
            },
            "core": {
                "action": "create",
                "path": f".agent-kit/releases/{self.release_report['archive_sha256']}",
                "archive_sha256": self.release_report["archive_sha256"],
            },
            "changes": list(self.changes),
            "migrations": [],
            "blockers": list(self.blockers),
            "epoch": self.preview_epoch,
        }

    def _preview_sha(self) -> str:
        return _sha256(_canonical(self._material()))

    def _preview(self, _root: Path, _archive: Path, **_kwargs: object) -> dict[str, object]:
        material = self._material()
        return {
            "schema": 1,
            "kind": "agent-kit-change-preview",
            "material": material,
            "approval": {
                "algorithm": "sha256-canonical-json-v1",
                "sha256": _sha256(_canonical(material)),
                "approvable": not self.blockers,
            },
        }

    def _materialize(self, _source: Path, output: Path) -> dict[str, object]:
        output.write_bytes(self.payload)
        return dict(self.release_report)

    def _prepare(self, **kwargs: object) -> controller.ControllerResult:
        return controller.prepare(
            self.runtime,
            self.target,
            self.archive,
            str(kwargs.pop("mode", self.operation)),
            **kwargs,
        )

    @staticmethod
    def _good_check(_target: Path, _session: object) -> dict[str, object]:
        return {
            "kit_ok": True,
            "project_ok": True,
            "existing_issues": [],
            "detail": "Offline kit and project checks passed.",
        }

    def _target_snapshot(self) -> list[tuple[str, str, bytes | None]]:
        result: list[tuple[str, str, bytes | None]] = []
        for path in sorted(self.target.rglob("*")):
            relative = path.relative_to(self.target).as_posix()
            result.append((relative, "directory" if path.is_dir() else "file", None if path.is_dir() else path.read_bytes()))
        return result

    def test_fresh_prepare_is_target_read_only_and_renderer_ready(self) -> None:
        (self.target / "src").mkdir()
        (self.target / "src" / "game.gd").write_text("extends Node\n", encoding="utf-8")
        before = self._target_snapshot()

        result = self._prepare()

        self.assertEqual(before, self._target_snapshot())
        self.assertEqual("ready", result["kit_change"]["status"])
        self.assertEqual("Not part of this kit change", result["kit_change"]["design"])
        self.assertEqual(0, result["kit_change"]["counts"]["game_files"])
        self.assertEqual(
            "Apply will save the previous state before changing project files",
            result["kit_change"]["recovery"],
        )
        self.assertFalse((self.target / ".agent-kit").exists())
        controller.release.read_verified_controller.assert_called_once_with(
            controller.release.ROOT
        )

    def test_list_sessions_returns_full_authenticated_session_details(self) -> None:
        self.assertEqual([], controller.list_sessions(self.runtime))
        self.assertFalse(self.runtime.exists())
        prepared = self._prepare()

        listed = controller.list_sessions(self.runtime)

        self.assertEqual(1, len(listed))
        self.assertEqual(prepared["session_id"], listed[0]["session_id"])
        self.assertEqual("ready", listed[0]["status"])
        self.assertEqual(str(self.target), listed[0]["project"]["path"])
        self.assertEqual(prepared["kit_change"]["plan_sha256"], listed[0]["plan_sha256"])

    def test_list_sessions_keeps_a_full_id_visible_when_its_preview_changed(self) -> None:
        prepared = self._prepare()
        self.preview_epoch += 1

        listed = controller.list_sessions(self.runtime)

        self.assertEqual([{
            "session_id": prepared["session_id"],
            "status": "unavailable",
            "problem": "preview-changed",
        }], listed)

    def test_mismatched_controller_is_refused_before_release_or_session_write(
        self,
    ) -> None:
        controller.release.read_verified_controller.side_effect = lambda _path: (
            {**self.release_report, "archive_sha256": "b" * 64},
            self.members,
        )

        with self.assertRaises(controller.KitChangeControllerError) as raised:
            self._prepare()

        self.assertEqual("controller-release-mismatch", raised.exception.code)
        self.assertIn("newer extracted release", raised.exception.detail)
        self.assertIn("matching clean source checkout", raised.exception.detail)
        controller.kit_change.preview.assert_not_called()
        controller.release.materialize_verified_directory_zip.assert_not_called()
        self.assertFalse(
            (self.runtime / controller.RELEASES_ROOT).exists(),
            "the incoming release was retained before controller authentication",
        )
        self.assertFalse(
            (self.runtime / controller.SESSIONS_ROOT).exists(),
            "session state was written before controller authentication",
        )

    def test_unauthenticated_controller_is_reported_as_a_release_mismatch(
        self,
    ) -> None:
        controller.release.read_verified_controller.side_effect = (
            controller.release.ReleaseError("source repository is dirty")
        )

        with self.assertRaises(controller.KitChangeControllerError) as raised:
            self._prepare()

        self.assertEqual("controller-release-mismatch", raised.exception.code)
        self.assertIn("could not be authenticated", raised.exception.detail)
        self.assertIn("matching clean source checkout", raised.exception.detail)
        controller.kit_change.preview.assert_not_called()
        self.assertFalse((self.runtime / controller.RELEASES_ROOT).exists())
        self.assertFalse((self.runtime / controller.SESSIONS_ROOT).exists())

    def test_mismatched_controller_cannot_materialize_a_release_directory(
        self,
    ) -> None:
        extracted = self.base / "extracted-release"
        extracted.mkdir()
        controller.release.read_verified_controller.side_effect = lambda _path: (
            {**self.release_report, "archive_sha256": "b" * 64},
            self.members,
        )

        with self.assertRaises(controller.KitChangeControllerError) as raised:
            controller.prepare(
                self.runtime,
                self.target,
                extracted,
                self.operation,
            )

        self.assertEqual("controller-release-mismatch", raised.exception.code)
        controller.release.materialize_verified_directory_zip.assert_not_called()
        controller.kit_change.preview.assert_not_called()
        self.assertFalse((self.runtime / controller.RELEASES_ROOT).exists())

    def test_controller_identity_uses_constant_time_comparison(self) -> None:
        comparison = mock.Mock(return_value=False)
        controller.release.read_verified_controller.side_effect = lambda _path: (
            {**self.release_report, "archive_sha256": "b" * 64},
            self.members,
        )

        with mock.patch.object(controller.hmac, "compare_digest", comparison):
            with self.assertRaises(controller.KitChangeControllerError):
                controller._require_controller_release(self.release_report)

        comparison.assert_called_once_with("b" * 64, "a" * 64)

    def test_archive_and_extracted_directory_resolve_to_same_session(self) -> None:
        first = self._prepare()
        extracted = self.base / "extracted-release"
        extracted.mkdir()

        second = controller.prepare(
            self.runtime, self.target, extracted, "install"
        )

        self.assertEqual(first["session_id"], second["session_id"])
        self.assertEqual(first["kit_change"], second["kit_change"])

    def test_requested_operation_must_match_detected_operation(self) -> None:
        with self.assertRaises(controller.KitChangeControllerError) as raised:
            self._prepare(mode="upgrade")
        self.assertEqual("mode-mismatch", raised.exception.code)

    def test_ambiguous_game_root_exposes_every_real_choice(self) -> None:
        for name in ("first", "second"):
            directory = self.target / name
            directory.mkdir()
            (directory / "project.godot").write_text("[application]\n", encoding="utf-8")
        self.blockers = [{
            "code": "game-root-ambiguous",
            "path": None,
            "detail": "multiple Godot projects exist; choose game_root explicitly",
        }]

        result = self._prepare()

        self.assertEqual("blocked", result["kit_change"]["status"])
        choices = result["kit_change"]["decisions"][0]["choices"]
        self.assertEqual(["first", "second"], [item["value"] for item in choices])
        self.assertIn("game-root-ambiguous", result["kit_change"]["blockers"][0])

    def test_tampered_session_is_rejected(self) -> None:
        result = self._prepare()
        path = self.runtime / controller.SESSIONS_ROOT / f"{result['session_id']}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["state"] = "complete"
        path.write_bytes(_canonical(value))

        with self.assertRaises(controller.KitChangeControllerError) as raised:
            controller.load(self.runtime, result["session_id"])
        self.assertEqual("session-tampered", raised.exception.code)

    def test_final_session_rejects_rechecksummed_preview_tampering(self) -> None:
        prepared = self._prepare()
        controller.apply(
            self.runtime,
            prepared["session_id"],
            prepared["kit_change"]["plan_sha256"],
            post_apply_check=self._good_check,
        )
        path = (
            self.runtime
            / controller.SESSIONS_ROOT
            / f"{prepared['session_id']}.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value["preview"]["raw"]["material"]["operation"] = "upgrade"
        value = controller._signed(value)
        path.write_bytes(_canonical(value))

        with self.assertRaises(controller.KitChangeControllerError) as raised:
            controller.load(self.runtime, prepared["session_id"])
        self.assertEqual("session-tampered", raised.exception.code)

    def test_valid_session_copied_under_another_id_is_rejected(self) -> None:
        result = self._prepare()
        original = (
            self.runtime
            / controller.SESSIONS_ROOT
            / f"{result['session_id']}.json"
        )
        copied_id = "f" * 64
        copied = self.runtime / controller.SESSIONS_ROOT / f"{copied_id}.json"
        copied.write_bytes(original.read_bytes())

        with self.assertRaises(controller.KitChangeControllerError) as raised:
            controller.load(self.runtime, copied_id)
        self.assertEqual("session-tampered", raised.exception.code)

    def test_retained_release_member_count_is_rederived(self) -> None:
        result = self._prepare()
        session = controller.load(self.runtime, result["session_id"])
        self.assertEqual(_sha256(self.payload), session["release"]["container_sha256"])
        changed = dict(session)
        changed["release"] = {**session["release"], "member_count": 99}
        controller._save(self.runtime, changed)

        with self.assertRaises(controller.KitChangeControllerError) as raised:
            controller.load(self.runtime, result["session_id"])
        self.assertEqual("release-tampered", raised.exception.code)

    def test_tampered_retained_archive_is_rejected(self) -> None:
        result = self._prepare()
        session = controller.load(self.runtime, result["session_id"])
        retained = self.runtime / str(session["release"]["archive"])
        retained.write_bytes(b"tampered\n")

        with self.assertRaises(controller.KitChangeControllerError) as raised:
            controller.load(self.runtime, result["session_id"])
        self.assertEqual("release-changed", raised.exception.code)

    def test_target_change_invalidates_the_review(self) -> None:
        result = self._prepare()
        self.preview_epoch += 1

        with self.assertRaises(controller.KitChangeControllerError) as raised:
            controller.load(self.runtime, result["session_id"])
        self.assertEqual("preview-changed", raised.exception.code)

    def test_apply_happy_path_has_exact_result_digest(self) -> None:
        prepared = self._prepare()

        applied = controller.apply(
            self.runtime,
            prepared["session_id"],
            prepared["kit_change"]["plan_sha256"],
            post_apply_check=self._good_check,
        )

        self.assertEqual("complete", applied["kit_change"]["status"])
        self.assertEqual(
            "Previous state is saved and can be restored",
            applied["kit_change"]["recovery"],
        )
        self.assertRegex(applied["kit_change"]["result_sha256"], r"^[0-9a-f]{64}$")
        loaded = controller.load(self.runtime, prepared["session_id"])
        self.assertEqual("complete", loaded["state"])
        self.assertEqual("1" * 32, loaded["transaction_id"])
        self.assertEqual(2, loaded["result"]["schema"])
        self.assertRegex(
            loaded["result"]["checked_at"],
            r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$",
        )
        self.assertEqual(
            {
                "state": "apply_time",
                "checked_at": loaded["result"]["checked_at"],
            },
            applied["kit_change"]["check_evidence"],
        )

    def test_old_finished_result_is_readable_with_an_empty_check_time(self) -> None:
        prepared = self._prepare()
        controller.apply(
            self.runtime,
            prepared["session_id"],
            prepared["kit_change"]["plan_sha256"],
            post_apply_check=self._good_check,
        )
        path = (
            self.runtime
            / controller.SESSIONS_ROOT
            / f"{prepared['session_id']}.json"
        )
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy["result"]["schema"] = 1
        legacy["result"].pop("checked_at")
        legacy["result_sha256"] = _sha256(_canonical(legacy["result"]))
        path.write_bytes(_canonical(controller._signed(legacy)))

        result = controller.status(self.runtime, prepared["session_id"])

        self.assertEqual("complete", result["kit_change"]["status"])
        self.assertEqual(
            {"state": "apply_time", "checked_at": ""},
            result["kit_change"]["check_evidence"],
        )

    def test_failed_kit_check_automatically_restores(self) -> None:
        prepared = self._prepare()
        failed = lambda _target, _session: {
            "kit_ok": False,
            "project_ok": False,
            "existing_issues": [],
            "detail": "Installed kit self-test failed.",
        }

        result = controller.apply(
            self.runtime,
            prepared["session_id"],
            prepared["kit_change"]["plan_sha256"],
            post_apply_check=failed,
        )

        self.assertEqual("restored", result["kit_change"]["status"])
        self.assertEqual("restored_failure", controller.load(self.runtime, prepared["session_id"])["state"])
        controller.kit_change.rollback.assert_called_once()

    def test_project_gaps_keep_the_install_as_adoption_required(self) -> None:
        prepared = self._prepare()
        issue = {
            "stage": "lint",
            "code": "legacy-warning",
            "path": "src/old.gd",
            "line": 4,
            "message_sha256": "a" * 64,
        }
        gaps = lambda _target, _session: {
            "kit_ok": True,
            "project_ok": False,
            "existing_issues": [issue],
            "detail": "Kit works; one existing project gap was recorded.",
        }

        result = controller.apply(
            self.runtime,
            prepared["session_id"],
            prepared["kit_change"]["plan_sha256"],
            post_apply_check=gaps,
        )

        self.assertEqual("adoption_required", result["kit_change"]["status"])
        self.assertEqual(1, result["kit_change"]["existing_gaps"]["count"])
        self.assertEqual("adoption_required", controller.load(self.runtime, prepared["session_id"])["state"])
        controller.kit_change.rollback.assert_not_called()

    def test_restore_requires_exact_result_and_is_idempotent(self) -> None:
        prepared = self._prepare()
        applied = controller.apply(
            self.runtime,
            prepared["session_id"],
            prepared["kit_change"]["plan_sha256"],
            post_apply_check=self._good_check,
        )
        result_sha = applied["kit_change"]["result_sha256"]
        with self.assertRaises(controller.KitChangeControllerError) as raised:
            controller.restore(self.runtime, prepared["session_id"], "f" * 64)
        self.assertEqual("result-mismatch", raised.exception.code)

        first = controller.restore(self.runtime, prepared["session_id"], result_sha)
        second = controller.restore(self.runtime, prepared["session_id"], result_sha)

        self.assertEqual("restored", first["kit_change"]["status"])
        self.assertEqual(first, second)
        controller.kit_change.rollback.assert_called_once()

    def test_restored_plan_gets_a_new_attempt_and_can_apply_again(self) -> None:
        first = self._prepare()
        applied = controller.apply(
            self.runtime,
            first["session_id"],
            first["kit_change"]["plan_sha256"],
            post_apply_check=self._good_check,
        )
        controller.restore(
            self.runtime, first["session_id"], applied["kit_change"]["result_sha256"]
        )

        second = self._prepare()
        reapplied = controller.apply(
            self.runtime,
            second["session_id"],
            second["kit_change"]["plan_sha256"],
            post_apply_check=self._good_check,
        )

        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertEqual(
            first["kit_change"]["plan_sha256"], second["kit_change"]["plan_sha256"]
        )
        self.assertEqual("complete", reapplied["kit_change"]["status"])
        self.assertEqual(2, controller.kit_change.apply.call_count)

    def test_transient_restored_failure_can_prepare_a_new_attempt(self) -> None:
        first = self._prepare()
        failed = lambda _target, _session: {
            "kit_ok": False,
            "project_ok": False,
            "existing_issues": [],
            "detail": "temporary self-test failure",
        }
        controller.apply(
            self.runtime,
            first["session_id"],
            first["kit_change"]["plan_sha256"],
            post_apply_check=failed,
        )

        second = self._prepare()
        result = controller.apply(
            self.runtime,
            second["session_id"],
            second["kit_change"]["plan_sha256"],
            post_apply_check=self._good_check,
        )

        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertEqual("complete", result["kit_change"]["status"])

    def test_complete_plan_reuses_its_finished_attempt(self) -> None:
        first = self._prepare()
        controller.apply(
            self.runtime,
            first["session_id"],
            first["kit_change"]["plan_sha256"],
            post_apply_check=self._good_check,
        )

        repeated = self._prepare()

        self.assertEqual(first["session_id"], repeated["session_id"])
        self.assertEqual("complete", repeated["kit_change"]["status"])
        self.assertEqual("apply_time", repeated["kit_change"]["check_evidence"]["state"])

    def test_interrupted_apply_recovers_then_checks(self) -> None:
        prepared = self._prepare()
        controller.kit_change.apply.side_effect = SimulatedCrash()
        with self.assertRaises(SimulatedCrash):
            controller.apply(
                self.runtime,
                prepared["session_id"],
                prepared["kit_change"]["plan_sha256"],
                post_apply_check=self._good_check,
            )
        self.assertEqual("applying", controller.load(self.runtime, prepared["session_id"])["state"])

        recovered = controller.recover(
            self.runtime,
            prepared["session_id"],
            post_apply_check=self._good_check,
        )

        self.assertEqual("complete", recovered["kit_change"]["status"])
        controller.kit_change.resume.assert_called_once_with(self.target)

    def test_crash_after_durable_apply_recovers_exact_applied_transaction(self) -> None:
        prepared = self._prepare()
        with mock.patch.object(
            controller, "_failpoint", side_effect=SimulatedCrash()
        ), self.assertRaises(SimulatedCrash):
            controller.apply(
                self.runtime,
                prepared["session_id"],
                prepared["kit_change"]["plan_sha256"],
                post_apply_check=self._good_check,
            )
        self.assertEqual(
            "applying", controller.load(self.runtime, prepared["session_id"])["state"]
        )
        controller.kit_change.resume.reset_mock()
        with mock.patch.object(
            controller.kit_change,
            "inspect_transaction",
            return_value={
                "ok": True,
                "status": "applied",
                "transaction_id": "1" * 32,
                "preview_sha256": prepared["kit_change"]["plan_sha256"],
                "target_scope_sha256": "e" * 64,
            },
        ) as inspect:
            recovered = controller.recover(
                self.runtime,
                prepared["session_id"],
                post_apply_check=self._good_check,
            )

        self.assertEqual("complete", recovered["kit_change"]["status"])
        self.assertEqual(
            "1" * 32,
            controller.load(self.runtime, prepared["session_id"])["transaction_id"],
        )
        inspect.assert_called_once_with(
            self.target,
            preview_sha256=prepared["kit_change"]["plan_sha256"],
            target_scope_sha256="e" * 64,
        )
        controller.kit_change.resume.assert_not_called()

    def test_repeated_apply_does_not_repeat_lifecycle_write(self) -> None:
        prepared = self._prepare()
        first = controller.apply(
            self.runtime,
            prepared["session_id"],
            prepared["kit_change"]["plan_sha256"],
            post_apply_check=self._good_check,
        )
        second = controller.apply(
            self.runtime,
            prepared["session_id"],
            prepared["kit_change"]["plan_sha256"],
            post_apply_check=self._good_check,
        )

        self.assertEqual(first, second)
        controller.kit_change.apply.assert_called_once()

    def test_controller_never_changes_game_or_design_files(self) -> None:
        (self.target / "src").mkdir()
        (self.target / "src" / "game.gd").write_text("extends Node\n", encoding="utf-8")
        (self.target / "docs" / "design").mkdir(parents=True)
        (self.target / "docs" / "design" / "vision.md").write_text("Player vision\n", encoding="utf-8")
        before = self._target_snapshot()
        prepared = self._prepare()

        controller.apply(
            self.runtime,
            prepared["session_id"],
            prepared["kit_change"]["plan_sha256"],
            post_apply_check=self._good_check,
        )

        self.assertEqual(before, self._target_snapshot())

    def test_only_authenticated_managed_runtime_may_overlap_target(self) -> None:
        runtime = self.target / ".kit" / "runtime"
        context = mock.Mock(install_mode="managed", runtime_root=runtime)
        with mock.patch.object(
            controller.project_context, "load_configured_context", return_value=context
        ):
            selected_runtime, selected_target = controller._separate_roots(
                runtime, self.target
            )
        self.assertEqual(runtime, selected_runtime)
        self.assertEqual(self.target, selected_target)

        unsafe = self.target / "review-runtime"
        with self.assertRaises(controller.KitChangeControllerError) as raised:
            controller._separate_roots(unsafe, self.target)
        self.assertEqual("runtime-overlap", raised.exception.code)
        self.assertFalse(unsafe.exists())

    def test_same_project_preview_writes_only_authenticated_private_runtime(self) -> None:
        runtime = self.target / ".kit" / "runtime"
        project_file = self.target / "project.godot"
        project_file.write_text("[application]\n", encoding="utf-8")
        context = mock.Mock(install_mode="managed", runtime_root=runtime)
        before = project_file.read_bytes()

        with mock.patch.object(
            controller.project_context, "load_configured_context", return_value=context
        ):
            result = controller.prepare(
                runtime, self.target, self.archive, "install"
            )

        self.assertEqual(before, project_file.read_bytes())
        self.assertTrue(
            (
                runtime
                / controller.SESSIONS_ROOT
                / f"{result['session_id']}.json"
            ).is_file()
        )
        changed_outside_runtime = [
            path
            for path in self.target.rglob("*")
            if path.is_file()
            and runtime not in path.parents
            and path != project_file
        ]
        self.assertEqual([], changed_outside_runtime)

    def test_simple_counts_separate_core_shared_and_retired_files(self) -> None:
        result = self._prepare()
        state = result["kit_change"]

        self.assertEqual(
            {"kit_files": 4, "shared_files": 1, "removed_files": 1, "game_files": 0},
            state["counts"],
        )
        self.assertEqual("install", state["mode"])
        self.assertEqual("Not part of this kit change", state["design"])

    def test_d1_reprepare_creates_a_new_read_only_review_and_never_applies(self) -> None:
        for name in ("first", "second"):
            directory = self.target / name
            directory.mkdir()
            (directory / "project.godot").write_text("[application]\n", encoding="utf-8")
        self.blockers = [{
            "code": "game-root-ambiguous",
            "path": None,
            "detail": "multiple Godot projects exist; choose game_root explicitly",
        }]
        old = self._prepare()

        def selected_preview(_root: Path, _archive: Path, **kwargs: object) -> dict[str, object]:
            if kwargs.get("game_root") == "second":
                material = self._material()
                material["blockers"] = []
                material["game_root"] = {"value": "second", "source": "explicit"}
                return {
                    "schema": 1,
                    "kind": "agent-kit-change-preview",
                    "material": material,
                    "approval": {
                        "algorithm": "sha256-canonical-json-v1",
                        "sha256": _sha256(_canonical(material)),
                        "approvable": True,
                    },
                }
            return self._preview(_root, _archive, **kwargs)

        controller.kit_change.preview.side_effect = selected_preview
        before = self._target_snapshot()

        new = controller.reprepare(self.runtime, old["session_id"], {"D1": "second"})

        self.assertNotEqual(old["session_id"], new["session_id"])
        self.assertNotEqual(
            old["kit_change"]["plan_sha256"], new["kit_change"]["plan_sha256"]
        )
        self.assertEqual("ready", new["kit_change"]["status"])
        self.assertEqual(new["session_id"], new["kit_change"]["session_id"])
        self.assertEqual(before, self._target_snapshot())
        controller.kit_change.apply.assert_not_called()

    def test_d1_reprepare_refuses_unadvertised_or_extra_choices(self) -> None:
        directory = self.target / "only"
        directory.mkdir()
        (directory / "project.godot").write_text("[application]\n", encoding="utf-8")
        self.blockers = [{
            "code": "game-root-ambiguous",
            "path": None,
            "detail": "choose game_root explicitly",
        }]
        old = self._prepare()

        for choices in ({"D1": "missing"}, {"D1": "only", "D2": "extra"}):
            with self.subTest(choices=choices), self.assertRaises(
                controller.KitChangeControllerError
            ):
                controller.reprepare(self.runtime, old["session_id"], choices)
        controller.kit_change.apply.assert_not_called()

    def test_failed_check_removes_a_fresh_generated_baseline(self) -> None:
        prepared = self._prepare()
        generated = b"generated baseline\n"

        def rollback(_root: Path, transaction_id: str | None = None) -> dict[str, object]:
            managed = self.target / ".agent-kit"
            self.assertFalse((managed / "brownfield.json").exists())
            managed.rmdir()
            return {
                "ok": True,
                "status": "rolled_back",
                "transaction_id": transaction_id,
                "preview_sha256": self._preview_sha(),
            }

        def failed(_target: Path, _session: object) -> dict[str, object]:
            path = self.target / ".agent-kit" / "brownfield.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(generated)
            return {
                "kit_ok": False,
                "project_ok": False,
                "existing_issues": [],
                "detail": "self-test failed",
                "baseline_sha256": _sha256(generated),
            }

        with mock.patch.object(controller.kit_change, "rollback", side_effect=rollback):
            result = controller.apply(
                self.runtime,
                prepared["session_id"],
                prepared["kit_change"]["plan_sha256"],
                post_apply_check=failed,
            )

        self.assertEqual("restored", result["kit_change"]["status"])
        self.assertFalse((self.target / ".agent-kit").exists())

    def test_failed_check_keeps_a_preexisting_managed_directory(self) -> None:
        managed = self.target / ".agent-kit"
        managed.mkdir()
        keep = managed / "keep.txt"
        keep.write_bytes(b"existing\n")
        prepared = self._prepare()
        generated = b"generated baseline\n"

        def failed(_target: Path, _session: object) -> dict[str, object]:
            (managed / "brownfield.json").write_bytes(generated)
            return {
                "kit_ok": False,
                "project_ok": False,
                "existing_issues": [],
                "detail": "self-test failed",
                "baseline_sha256": _sha256(generated),
            }

        def rollback(_root: Path, transaction_id: str | None = None) -> dict[str, object]:
            self.assertFalse((managed / "brownfield.json").exists())
            self.assertEqual(b"existing\n", keep.read_bytes())
            return {
                "ok": True,
                "status": "rolled_back",
                "transaction_id": transaction_id,
                "preview_sha256": self._preview_sha(),
            }

        with mock.patch.object(controller.kit_change, "rollback", side_effect=rollback):
            result = controller.apply(
                self.runtime,
                prepared["session_id"],
                prepared["kit_change"]["plan_sha256"],
                post_apply_check=failed,
            )

        self.assertEqual("restored", result["kit_change"]["status"])
        self.assertTrue(managed.is_dir())
        self.assertEqual(b"existing\n", keep.read_bytes())

    def test_manual_restore_removes_a_fresh_managed_directory(self) -> None:
        prepared = self._prepare()
        generated = b"generated baseline\n"

        def good(_target: Path, _session: object) -> dict[str, object]:
            path = self.target / ".agent-kit" / "brownfield.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(generated)
            return {
                "kit_ok": True,
                "project_ok": True,
                "existing_issues": [],
                "detail": "passed",
                "baseline_sha256": _sha256(generated),
            }

        def rollback(_root: Path, transaction_id: str | None = None) -> dict[str, object]:
            managed = self.target / ".agent-kit"
            self.assertFalse((managed / "brownfield.json").exists())
            managed.rmdir()
            return {
                "ok": True,
                "status": "rolled_back",
                "transaction_id": transaction_id,
                "preview_sha256": self._preview_sha(),
            }

        applied = controller.apply(
            self.runtime,
            prepared["session_id"],
            prepared["kit_change"]["plan_sha256"],
            post_apply_check=good,
        )
        with mock.patch.object(controller.kit_change, "rollback", side_effect=rollback):
            restored = controller.restore(
                self.runtime,
                prepared["session_id"],
                applied["kit_change"]["result_sha256"],
            )

        self.assertEqual("restored", restored["kit_change"]["status"])
        self.assertFalse((self.target / ".agent-kit").exists())

    def test_default_malformed_static_proof_restores_project_and_baseline(self) -> None:
        prepared = self._prepare()
        generated = b"generated baseline\n"

        def malformed(
            _target: Path, _session: object, **kwargs: object
        ) -> dict[str, object]:
            ready = kwargs["baseline_ready"]
            assert callable(ready)
            ready(generated)
            path = self.target / ".agent-kit" / "brownfield.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(generated)
            return {
                "kit_ok": False,
                "project_ok": False,
                "existing_issues": [],
                "detail": "verify returned no readable receipt",
                "baseline_sha256": _sha256(generated),
            }

        with mock.patch.object(
            controller.kit_change_check, "run", side_effect=malformed
        ) as run:
            result = controller.apply(
                self.runtime,
                prepared["session_id"],
                prepared["kit_change"]["plan_sha256"],
            )

        self.assertEqual("restored", result["kit_change"]["status"])
        self.assertFalse((self.target / ".agent-kit" / "brownfield.json").exists())
        controller.kit_change.rollback.assert_called_once()
        run.assert_called_once()

    def test_crash_after_baseline_intent_recovers_when_scan_input_changed(self) -> None:
        prepared = self._prepare()
        first = b"first generated baseline\n"
        changed = b"changed generated baseline\n"
        calls = 0

        def checking(
            _target: Path, _session: object, **kwargs: object
        ) -> dict[str, object]:
            nonlocal calls
            calls += 1
            ready = kwargs["baseline_ready"]
            assert callable(ready)
            ready(first if calls == 1 else changed)
            raise SimulatedCrash()

        with mock.patch.object(
            controller.kit_change_check, "run", side_effect=checking
        ), self.assertRaises(SimulatedCrash):
            controller.apply(
                self.runtime,
                prepared["session_id"],
                prepared["kit_change"]["plan_sha256"],
            )
        stored = controller.load(self.runtime, prepared["session_id"])
        self.assertEqual("checking", stored["state"])
        self.assertEqual(_sha256(first), stored["baseline"]["generated"]["sha256"])

        with mock.patch.object(
            controller.kit_change_check, "run", side_effect=checking
        ):
            recovered = controller.recover(self.runtime, prepared["session_id"])

        self.assertEqual("restored", recovered["kit_change"]["status"])
        self.assertFalse((self.target / ".agent-kit" / "brownfield.json").exists())

    def test_baseline_swap_after_write_pauses_and_can_recover(self) -> None:
        prepared = self._prepare()
        generated = b"generated baseline\n"
        calls = 0

        def checking(
            _target: Path, _session: object, **kwargs: object
        ) -> dict[str, object]:
            nonlocal calls
            calls += 1
            ready = kwargs["baseline_ready"]
            assert callable(ready)
            ready(generated)
            path = self.target / ".agent-kit" / "brownfield.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            if calls == 1:
                path.write_bytes(generated)
                raise SimulatedCrash()
            return {
                "kit_ok": False,
                "project_ok": False,
                "existing_issues": [],
                "detail": "static proof unavailable",
                "baseline_sha256": _sha256(generated),
            }

        with mock.patch.object(
            controller.kit_change_check, "run", side_effect=checking
        ), self.assertRaises(SimulatedCrash):
            controller.apply(
                self.runtime,
                prepared["session_id"],
                prepared["kit_change"]["plan_sha256"],
            )
        path = self.target / ".agent-kit" / "brownfield.json"
        path.write_bytes(b"third version\n")
        controller.kit_change.rollback.reset_mock()

        with mock.patch.object(
            controller.kit_change_check, "run", side_effect=checking
        ):
            paused = controller.recover(self.runtime, prepared["session_id"])

        self.assertEqual("checking", paused["kit_change"]["status"])
        controller.kit_change.rollback.assert_not_called()
        path.write_bytes(generated)

        with mock.patch.object(
            controller.kit_change_check, "run", side_effect=checking
        ):
            recovered = controller.recover(self.runtime, prepared["session_id"])

        self.assertEqual("restored", recovered["kit_change"]["status"])
        self.assertFalse(path.exists())

    def test_failed_check_restores_exact_prior_baseline_bytes(self) -> None:
        path = self.target / ".agent-kit" / "brownfield.json"
        path.parent.mkdir(parents=True)
        prior = b"prior baseline\n"
        generated = b"generated baseline\n"
        path.write_bytes(prior)
        prepared = self._prepare()

        def failed(_target: Path, _session: object) -> dict[str, object]:
            path.write_bytes(generated)
            return {
                "kit_ok": False,
                "project_ok": False,
                "existing_issues": [],
                "detail": "self-test failed",
                "baseline_sha256": _sha256(generated),
            }

        controller.apply(
            self.runtime,
            prepared["session_id"],
            prepared["kit_change"]["plan_sha256"],
            post_apply_check=failed,
        )

        self.assertEqual(prior, path.read_bytes())

    def test_manual_restore_waits_for_a_third_baseline_version_to_be_resolved(self) -> None:
        prepared = self._prepare()
        generated = b"generated baseline\n"

        def good(_target: Path, _session: object) -> dict[str, object]:
            path = self.target / ".agent-kit" / "brownfield.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(generated)
            return {
                "kit_ok": True,
                "project_ok": True,
                "existing_issues": [],
                "detail": "passed",
                "baseline_sha256": _sha256(generated),
            }

        applied = controller.apply(
            self.runtime,
            prepared["session_id"],
            prepared["kit_change"]["plan_sha256"],
            post_apply_check=good,
        )
        path = self.target / ".agent-kit" / "brownfield.json"
        path.write_bytes(b"third version\n")
        controller.kit_change.rollback.reset_mock()

        paused = controller.restore(
            self.runtime,
            prepared["session_id"],
            applied["kit_change"]["result_sha256"],
        )

        self.assertEqual("recovery_required", paused["kit_change"]["status"])
        self.assertEqual(b"third version\n", path.read_bytes())
        controller.kit_change.rollback.assert_not_called()
        path.write_bytes(generated)

        restored = controller.restore(
            self.runtime,
            prepared["session_id"],
            applied["kit_change"]["result_sha256"],
        )

        self.assertEqual("restored", restored["kit_change"]["status"])
        self.assertFalse(path.exists())
        controller.kit_change.rollback.assert_called_once()

    def test_manual_restore_retries_a_transient_baseline_permission_error(self) -> None:
        path = self.target / ".agent-kit" / "brownfield.json"
        path.parent.mkdir(parents=True)
        prior = b"prior baseline\n"
        generated = b"generated baseline\n"
        path.write_bytes(prior)
        prepared = self._prepare()

        def good(_target: Path, _session: object) -> dict[str, object]:
            path.write_bytes(generated)
            return {
                "kit_ok": True,
                "project_ok": True,
                "existing_issues": [],
                "detail": "passed",
                "baseline_sha256": _sha256(generated),
            }

        applied = controller.apply(
            self.runtime,
            prepared["session_id"],
            prepared["kit_change"]["plan_sha256"],
            post_apply_check=good,
        )
        original_atomic = controller.kit_change._atomic_bytes
        failed = False

        def flaky_atomic(
            target: Path, content: bytes, mode: str | None = None
        ) -> None:
            nonlocal failed
            if Path(target) == path and not failed:
                failed = True
                raise PermissionError("baseline is temporarily locked")
            original_atomic(target, content, mode)

        with mock.patch.object(
            controller.kit_change, "_atomic_bytes", side_effect=flaky_atomic
        ):
            paused = controller.restore(
                self.runtime,
                prepared["session_id"],
                applied["kit_change"]["result_sha256"],
            )
            restored = controller.restore(
                self.runtime,
                prepared["session_id"],
                applied["kit_change"]["result_sha256"],
            )

        self.assertEqual("recovery_required", paused["kit_change"]["status"])
        self.assertEqual("restored", restored["kit_change"]["status"])
        self.assertEqual(prior, path.read_bytes())
        controller.kit_change.rollback.assert_called_once()

    def test_failed_check_retries_an_exact_transaction_rollback_conflict(self) -> None:
        prepared = self._prepare()
        failed_check = lambda _target, _session: {
            "kit_ok": False,
            "project_ok": False,
            "existing_issues": [],
            "detail": "self-test failed",
        }
        rolled_back = {
            "ok": True,
            "status": "rolled_back",
            "transaction_id": "1" * 32,
            "preview_sha256": prepared["kit_change"]["plan_sha256"],
        }
        controller.kit_change.rollback.side_effect = [
            controller.kit_change.KitChangeError(
                "transaction-conflict", "project bytes changed after apply"
            ),
            rolled_back,
        ]

        paused = controller.apply(
            self.runtime,
            prepared["session_id"],
            prepared["kit_change"]["plan_sha256"],
            post_apply_check=failed_check,
        )
        recovered = controller.recover(
            self.runtime,
            prepared["session_id"],
            post_apply_check=failed_check,
        )

        self.assertEqual("recovery_required", paused["kit_change"]["status"])
        self.assertEqual("restored", recovered["kit_change"]["status"])
        self.assertEqual(2, controller.kit_change.rollback.call_count)

    def test_check_crash_after_baseline_write_recovers_deterministically(self) -> None:
        prepared = self._prepare()
        generated = b"generated baseline\n"
        calls = 0

        def checking(_target: Path, _session: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            path = self.target / ".agent-kit" / "brownfield.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(generated)
            if calls == 1:
                raise SimulatedCrash()
            return {
                "kit_ok": True,
                "project_ok": True,
                "existing_issues": [],
                "detail": "passed",
                "baseline_sha256": _sha256(generated),
            }

        with self.assertRaises(SimulatedCrash):
            controller.apply(
                self.runtime,
                prepared["session_id"],
                prepared["kit_change"]["plan_sha256"],
                post_apply_check=checking,
            )

        recovered = controller.recover(
            self.runtime, prepared["session_id"], post_apply_check=checking
        )
        self.assertEqual("complete", recovered["kit_change"]["status"])
        self.assertEqual(2, calls)


class RealControllerRecoveryTest(unittest.TestCase):
    """Prove the controller can finish exact low-level rollback recovery."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="kit-change-recovery-")
        self.base = Path(self.temporary.name).resolve()
        self.runtime = self.base / "controller-runtime"
        self.target = self.base / "project"
        self.target.mkdir()
        self.archive = self.base / "release.zip"
        self.payload = b"verified release fixture\n"
        self.archive.write_bytes(self.payload)
        self.agents = self.target / "AGENTS.md"
        self.agents.write_bytes(b"# Human project rules\n")
        (self.target / "project.godot").write_bytes(b"[application]\n")
        report, self.members = lifecycle_support._release_fixture()
        self.report = dict(report)
        self.report.update(
            {
                "format": "zip",
                "container_sha256": _sha256(self.payload),
            }
        )
        self.readers = [
            mock.patch.object(
                module,
                "read_verified_archive",
                side_effect=lambda _path: (dict(self.report), self.members),
            )
            for module in (controller.release, controller.kit_change.release)
        ]
        self.readers.append(
            mock.patch.object(
                controller.release,
                "read_verified_controller",
                side_effect=lambda _path: (dict(self.report), self.members),
            )
        )
        for reader in self.readers:
            reader.start()

    def tearDown(self) -> None:
        for reader in reversed(self.readers):
            reader.stop()
        self.temporary.cleanup()

    def _prepare(self) -> controller.ControllerResult:
        return controller.prepare(
            self.runtime,
            self.target,
            self.archive,
            "install",
            game_root=".",
        )

    def _snapshot(self) -> list[tuple[str, str, bytes | None]]:
        result: list[tuple[str, str, bytes | None]] = []
        for path in sorted(self.target.rglob("*")):
            relative = path.relative_to(self.target).as_posix()
            if relative == ".kit" or relative.startswith(".kit/"):
                continue
            result.append(
                (
                    relative,
                    "directory" if path.is_dir() else "file",
                    None if path.is_dir() else path.read_bytes(),
                )
            )
        return result

    @staticmethod
    def _unexpected_check(_target: Path, _session: object) -> dict[str, object]:
        raise AssertionError("post-apply checking must not run before recovery")

    def test_apply_rollback_conflict_binds_exact_transaction_and_recovers(self) -> None:
        before = self._snapshot()
        prepared = self._prepare()
        applied_agents = b""

        def interrupt(name: str) -> None:
            nonlocal applied_agents
            if name == "after-entry:AGENTS.md":
                applied_agents = self.agents.read_bytes()
                self.agents.write_bytes(b"third version owned by another writer\n")
                raise RuntimeError("interrupt after shared file apply")

        with mock.patch.object(
            controller.kit_change, "_failpoint", side_effect=interrupt
        ):
            paused = controller.apply(
                self.runtime,
                prepared["session_id"],
                prepared["kit_change"]["plan_sha256"],
                post_apply_check=self._unexpected_check,
            )

        self.assertEqual("recovery_required", paused["kit_change"]["status"])
        self.assertTrue(
            controller.load(self.runtime, prepared["session_id"])["transaction_id"]
        )
        self.assertEqual(
            b"third version owned by another writer\n", self.agents.read_bytes()
        )

        self.agents.write_bytes(applied_agents)
        recovered = controller.recover(
            self.runtime,
            prepared["session_id"],
            post_apply_check=self._unexpected_check,
        )

        self.assertEqual("restored", recovered["kit_change"]["status"])
        self.assertEqual(before, self._snapshot())
        self.assertFalse((self.target / ".agent-kit").exists())

    def test_apply_rollback_permission_error_retries_the_same_transaction(self) -> None:
        before = self._snapshot()
        prepared = self._prepare()
        original_agents = self.agents.read_bytes()
        original_atomic = controller.kit_change._atomic_bytes
        interrupted = False
        restore_failed = False

        def interrupt(name: str) -> None:
            nonlocal interrupted
            if name == "after-entry:AGENTS.md" and not interrupted:
                interrupted = True
                raise RuntimeError("interrupt after shared file apply")

        def flaky_atomic(path: Path, content: bytes, mode: str | None = None) -> None:
            nonlocal restore_failed
            if Path(path) == self.agents and content == original_agents and not restore_failed:
                restore_failed = True
                raise PermissionError("shared file is temporarily locked")
            original_atomic(path, content, mode)

        with (
            mock.patch.object(
                controller.kit_change, "_failpoint", side_effect=interrupt
            ),
            mock.patch.object(
                controller.kit_change, "_atomic_bytes", side_effect=flaky_atomic
            ),
        ):
            paused = controller.apply(
                self.runtime,
                prepared["session_id"],
                prepared["kit_change"]["plan_sha256"],
                post_apply_check=self._unexpected_check,
            )
            recovered = controller.recover(
                self.runtime,
                prepared["session_id"],
                post_apply_check=self._unexpected_check,
            )

        self.assertTrue(restore_failed)
        self.assertEqual("recovery_required", paused["kit_change"]["status"])
        self.assertEqual("restored", recovered["kit_change"]["status"])
        self.assertEqual(before, self._snapshot())
        self.assertFalse((self.target / ".agent-kit").exists())


if __name__ == "__main__":
    unittest.main()
