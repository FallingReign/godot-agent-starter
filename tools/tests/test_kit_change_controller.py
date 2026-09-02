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
sys.path.insert(0, str(TOOLS))

import kit_change_controller as controller  # noqa: E402


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
            "archive_sha256": _sha256(self.payload),
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
        self.assertFalse((self.target / ".agent-kit").exists())

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
        self.assertRegex(applied["kit_change"]["result_sha256"], r"^[0-9a-f]{64}$")
        loaded = controller.load(self.runtime, prepared["session_id"])
        self.assertEqual("complete", loaded["state"])
        self.assertEqual("1" * 32, loaded["transaction_id"])

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

        self.assertEqual("complete", result["kit_change"]["status"])
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

    def test_simple_counts_separate_core_shared_and_retired_files(self) -> None:
        result = self._prepare()
        state = result["kit_change"]

        self.assertEqual(
            {"kit_files": 4, "shared_files": 1, "removed_files": 1, "game_files": 0},
            state["counts"],
        )
        self.assertEqual("install", state["mode"])
        self.assertEqual("Not part of this kit change", state["design"])


if __name__ == "__main__":
    unittest.main()
