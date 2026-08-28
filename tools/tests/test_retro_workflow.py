#!/usr/bin/env python3
"""Production-shaped tests for immutable retrospective evidence and reports."""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import retro  # noqa: E402
import retro_rank  # noqa: E402
import retro_sdk  # noqa: E402
import providers  # noqa: E402


@contextmanager
def scratch_directory() -> Iterator[Path]:
    configured = os.environ.get("KIT_TEST_TMPDIR")
    base = Path(configured) if configured else TOOLS.parent / ".checklogs" / "tests"
    path = base / f"retro-workflow-{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class RetroWorkflowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = scratch_directory()
        self.root = self.scratch.__enter__()
        self.retro_dir = self.root / "docs" / "retro"
        self.runtime_dir = self.root / ".kit" / "runtime"
        (self.retro_dir / "notes").mkdir(parents=True)
        self.note = self.retro_dir / "notes" / "slice-one.md"
        self.note.write_text("The human corrected the same workflow twice.\n",
                             encoding="utf-8")
        self.saved = {
            "root": retro.ROOT,
            "retro": retro.RETRO_DIR,
            "evidence": retro.EVIDENCE_DIR,
            "prompt": retro.PROMPT_DIR,
            "thread": retro.THREAD_FILE,
            "ran": retro.RAN_FILE,
            "rank_root": retro_rank.ROOT,
            "rank_evidence": retro_rank.SESSION_EVIDENCE_DIR,
            "discover": retro.session_digest.discover,
            "digest": retro.session_digest.digest_one,
        }
        retro.ROOT = self.root
        retro.RETRO_DIR = self.retro_dir
        retro.EVIDENCE_DIR = self.runtime_dir / "evidence"
        retro.PROMPT_DIR = self.runtime_dir / "retro" / "sdk"
        retro.THREAD_FILE = self.runtime_dir / "retro" / "thread.json"
        retro.RAN_FILE = self.runtime_dir / "retro" / "ran.json"
        retro_rank.ROOT = self.root
        retro_rank.SESSION_EVIDENCE_DIR = self.runtime_dir / "evidence" / "sessions"
        sessions = [{"id": "session-abc", "log": self.root / "events.jsonl"}]
        retro.session_digest.discover = (
            lambda root, roots=None, **_window: list(sessions)
        )
        retro.session_digest.digest_one = lambda session, index: {
            "index": index,
            "id": session["id"],
            "name": "fixture",
            "started": "2026-01-01T00:00:00Z",
            "updated": "2026-01-01T01:00:00Z",
            "turns": 2,
            "provider": "copilot",
            "usage": {
                "source": "provider_reported",
                "metrics": [{"name": "aiu", "value": 0.5}],
                "monetary_cost": None,
            },
            "model": "fixture-model",
            "persona": "game-builder",
            "gate_fails": {"schema": 2},
            "gate_passes": 1,
            "loops": {"kit verify": 4},
            "rewrites": {"plan.py": 3},
            "human": ["Please make the decision visible."],
            "sources": {"events": {"path": "events.jsonl", "sha256": "a" * 64,
                                    "bytes": 100}},
            "warnings": [],
        }

    def tearDown(self) -> None:
        retro.ROOT = self.saved["root"]
        retro.RETRO_DIR = self.saved["retro"]
        retro.EVIDENCE_DIR = self.saved["evidence"]
        retro.PROMPT_DIR = self.saved["prompt"]
        retro.THREAD_FILE = self.saved["thread"]
        retro.RAN_FILE = self.saved["ran"]
        retro_rank.ROOT = self.saved["rank_root"]
        retro_rank.SESSION_EVIDENCE_DIR = self.saved["rank_evidence"]
        retro.session_digest.discover = self.saved["discover"]
        retro.session_digest.digest_one = self.saved["digest"]
        self.scratch.__exit__(None, None, None)

    def args(self) -> SimpleNamespace:
        return SimpleNamespace(
            sessions=None, since=None, limit=None, baseline=None
        )

    def valid_result(self) -> dict:
        return {
            "summary": "One recurring decision failure.",
            "findings": [{
                "title": "Decision state is invisible",
                "sessions": ["S1"],
                "human_turns": ["S1:H1"],
                "mechanical": ["LOOP kit verify x4 (S1)"],
                "recurs": True,
                "severity": "wrong-built",
                "fix_files": ["tools/plan_html.py", "src/forbidden.gd", "../escape"],
                "fix_lines": 22,
                "problem": "The surface presents inventory instead of a decision.",
                "proposal": "Render the pending choice first.",
                "measure": "A reader identifies the next decision without opening details.",
            }],
            "observations": [],
        }

    def report_for(self, pack_path: Path) -> Path:
        report = self.retro_dir / f"2026-01-01-{pack_path.stem[:10]}-findings.md"
        report.write_text("# Retrospective findings\n", encoding="utf-8")
        return report

    def bound_report_for(self, pack_path: Path) -> Path:
        return retro_sdk.write_rankable_report(
            self.retro_dir, "slice", self.valid_result(), "", False,
            pack_path, self.root,
        )

    def published_report_for(self, pack_path: Path) -> Path:
        report = self.bound_report_for(pack_path)
        text = report.read_text(encoding="utf-8")
        header, findings = retro_rank.parse_findings(text)
        digests, warnings, personas = retro_rank.citation_report(findings)
        self.assertEqual(warnings, [])
        ranked, _scores, _notes = retro_rank.rewrite(
            report, header, findings, digests, personas
        )
        report.write_text(ranked, encoding="utf-8", newline="\n")
        return report

    def test_pack_is_content_addressed_and_contains_notes_and_stable_citations(self) -> None:
        first = retro.build_pack(self.args())
        first_path = retro.write_pack(first)
        second = retro.build_pack(self.args())
        second_path = retro.write_pack(second)
        self.assertEqual(first_path, second_path)
        self.assertEqual(first["notes"][0]["text"],
                         self.note.read_bytes().decode("utf-8"))
        self.assertEqual(first["completion_key"], "no-proposal")
        self.assertEqual(first["sessions"][0]["human_messages"][0]["citation"], "S1:H1")
        self.assertEqual(first["sessions"][0]["persona"], "game-builder")
        pointer = json.loads(
            (self.runtime_dir / "evidence" / "latest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(pointer["sha256"], first_path.stem)
        self.assertTrue(first_path.is_relative_to(self.runtime_dir / "evidence" / "packs"))
        self.assertTrue(Path(first["session_snapshot"]["path"]).parts[:3]
                        == (".kit", "runtime", "evidence"))
        self.assertFalse((self.retro_dir / "evidence").exists())
        self.assertFalse((self.retro_dir / "evidence.json").exists())

    def test_default_capture_keeps_every_bounded_matching_session(self) -> None:
        sessions = [
            {"id": f"session-{number}", "log": self.root / f"events-{number}.jsonl"}
            for number in range(1, 6)
        ]

        def discover(_root, _roots=None, *, since=None, limit=None):
            self.assertIsNone(since)
            self.assertIsNone(limit)
            return list(sessions)

        with mock.patch.object(retro.session_digest, "discover", side_effect=discover):
            pack = retro.build_pack(self.args())

        self.assertEqual(len(pack["sessions"]), 5)
        self.assertEqual(pack["sessions_found"], 5)
        self.assertEqual(pack["session_capture"], {
            "complete": True,
            "window": {
                "mode": "complete-bounded",
                "since": None,
                "limit": None,
                "captured_sessions": 5,
            },
            "failures": [],
        })

    def test_explicit_session_window_is_bound_into_snapshot_and_pack(self) -> None:
        args = self.args()
        args.since = "2026-01-01T00:00:00Z"
        args.limit = 2
        selected = [
            {"id": f"window-{number}", "log": self.root / f"window-{number}.jsonl"}
            for number in range(1, 3)
        ]

        def discover(_root, _roots=None, *, since=None, limit=None):
            self.assertEqual(since, args.since)
            self.assertEqual(limit, args.limit)
            return list(selected)

        with mock.patch.object(retro.session_digest, "discover", side_effect=discover):
            pack = retro.build_pack(args)

        snapshot_path = self.root / pack["session_snapshot"]["path"]
        manifest = json.loads(snapshot_path.read_text(encoding="utf-8"))
        self.assertEqual(
            pack["session_capture"]["window"], manifest["capture_window"]
        )
        self.assertEqual(manifest["capture_window"], {
            "mode": "explicit",
            "since": args.since,
            "limit": 2,
            "captured_sessions": 2,
        })

    def test_failed_session_capture_is_visible_and_blocks_handoff_and_publish(self) -> None:
        def failed(_session, index):
            return {
                "index": index,
                "id": "session-abc",
                "provider": "copilot",
                "error": "events_byte_limit_exceeded",
                "sources": {},
                "warnings": [{
                    "code": "events_byte_limit_exceeded",
                    "max_bytes": 8,
                    "observed_bytes": 9,
                }],
            }

        with mock.patch.object(retro.session_digest, "digest_one", side_effect=failed):
            pack = retro.build_pack(self.args())
        pack_path = retro.write_pack(pack)

        self.assertFalse(pack["session_capture"]["complete"])
        self.assertEqual(
            pack["sessions"][0]["error"], "events_byte_limit_exceeded"
        )
        with self.assertRaisesRegex(ValueError, "session evidence is incomplete"):
            retro.emit_print(pack, pack_path)
        report = self.bound_report_for(pack_path)
        with self.assertRaisesRegex(ValueError, "session evidence is incomplete"):
            retro._bound_report_pack(report)

    def test_prompt_and_ran_marker_are_private_runtime_state(self) -> None:
        pack = retro.build_pack(self.args())
        pack_path = retro.write_pack(pack)
        retro.emit_print(pack, pack_path)
        retro.mark_ran("slice@abc", pack_path)
        self.assertTrue((self.runtime_dir / "retro" / "sdk" / "prompt.md").is_file())
        self.assertTrue((self.runtime_dir / "retro" / "ran.json").is_file())
        self.assertTrue(retro.already_ran("slice@abc"))
        marker = json.loads(retro.RAN_FILE.read_text(encoding="utf-8"))
        self.assertEqual(marker["schema"], 2)
        self.assertEqual(marker["completed_snapshots"], [{
            "slice": "slice@abc",
            "evidence_pack": pack_path.relative_to(self.root).as_posix(),
            "sha256": pack_path.stem,
        }])
        self.assertFalse((self.retro_dir / "prompt.md").exists())
        self.assertFalse((self.retro_dir / ".ran").exists())

    def test_completion_cli_routes_without_capturing_a_new_snapshot(self) -> None:
        report = self.retro_dir / "2026-01-01-fixture-findings.md"
        with mock.patch.object(
                retro, "complete_published_retro", return_value=(True, "completed")
        ) as complete, mock.patch.object(
                retro, "build_pack"
        ) as build, mock.patch.object(
                sys,
                "argv",
                ["retro.py", "--complete-published", str(report)],
        ):
            code = retro.main()

        self.assertEqual(0, code)
        complete.assert_called_once_with(report)
        build.assert_not_called()

    def test_valid_analyzer_result_becomes_the_rankable_schema(self) -> None:
        pack = retro.build_pack(self.args())
        pack_path = retro.write_pack(pack)
        result = self.valid_result()
        self.assertEqual(retro_sdk.validate_findings(result, pack), [])
        report = retro_sdk.write_rankable_report(
            self.retro_dir, "slice", result, "", False, pack_path, self.root)
        header, findings = retro_rank.parse_findings(report.read_text(encoding="utf-8"))
        self.assertIn("evidence_pack:", header)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["human_turns"], ["S1:H1"])
        self.assertEqual(findings[0]["fix_files"], ["tools/plan_html.py"])
        self.assertEqual(retro_rank.extract_sections(findings[0]["body"])["measure"],
                         "A reader identifies the next decision without opening details.")
        digests, warnings, personas = retro_rank.citation_report(findings)
        self.assertEqual(warnings, [])
        self.assertEqual(digests["S1"]["human"], ["Please make the decision visible."])
        self.assertEqual(personas["S1"], "game-builder")
        payload = report.read_bytes()
        self.assertNotIn(b"\r", payload)
        self.assertTrue(payload.endswith(b"\n"))
        self.assertFalse(payload.endswith(b"\n\n"))

    def test_failed_atomic_publish_leaves_no_rankable_report_or_temp(self) -> None:
        pack = retro.build_pack(self.args())
        pack_path = retro.write_pack(pack)
        with mock.patch.object(retro_sdk.os, "replace",
                               side_effect=OSError("publish failed")):
            with self.assertRaisesRegex(OSError, "publish failed"):
                retro_sdk.write_rankable_report(
                    self.retro_dir, "slice", self.valid_result(), "", False,
                    pack_path, self.root,
                )
        self.assertEqual(list(self.retro_dir.glob("*-findings.md")), [])
        self.assertEqual(list(self.retro_dir.glob(".*.tmp")), [])

    def test_invalid_output_is_canonical_and_never_rankable(self) -> None:
        pack_path = retro.write_pack(retro.build_pack(self.args()))
        report = retro_sdk.write_rankable_report(
            self.retro_dir, "slice", None, "bad\r\nresponse\r\n\r\n", False,
            pack_path, self.root,
        )
        self.assertTrue(report.name.endswith("-unvalidated.md"))
        self.assertEqual(list(self.retro_dir.glob("*-findings.md")), [])
        payload = report.read_bytes()
        self.assertNotIn(b"\r", payload)
        self.assertTrue(payload.endswith(b"\n"))
        self.assertFalse(payload.endswith(b"\n\n"))
        self.assertEqual(list(self.retro_dir.glob(".*.tmp")), [])

    def test_unknown_citation_cannot_enter_the_findings_queue(self) -> None:
        pack = retro.build_pack(self.args())
        value = {
            "findings": [{
                "title": "unsupported", "sessions": ["S9"],
                "human_turns": ["S9:H1"], "mechanical": [],
                "problem": "p", "proposal": "p", "measure": "m",
            }],
            "observations": [],
        }
        errors = retro_sdk.validate_findings(value, pack)
        self.assertTrue(any("unknown human turn" in error for error in errors))
        self.assertTrue(any("unknown session" in error for error in errors))

    def test_legacy_ranking_never_rebinds_citations_to_live_sessions(self) -> None:
        finding = {
            "title": "legacy", "sessions": ["S1"], "human_turns": ["S1:H1"],
            "mechanical": [], "session_snapshot": "",
        }
        with mock.patch.object(
                retro_rank.session_digest, "discover",
                side_effect=AssertionError("live discovery must not run")):
            digests, warnings, personas = retro_rank.citation_report([finding])
        self.assertEqual(digests, {})
        self.assertEqual(personas, {})
        self.assertIn("no immutable session snapshot", " ".join(warnings))

    def test_notes_archive_only_when_captured_bytes_still_match(self) -> None:
        pack = retro.build_pack(self.args())
        self.note.write_text("changed after capture\n", encoding="utf-8")
        ok, error = retro.archive_notes(pack)
        self.assertFalse(ok)
        self.assertIn("changed", error)
        self.assertTrue(self.note.exists())

        self.note.write_text("The human corrected the same workflow twice.\n",
                             encoding="utf-8")
        ok, error = retro.archive_notes(pack)
        self.assertTrue(ok, error)
        self.assertFalse(self.note.exists())
        self.assertTrue((self.retro_dir / "archive" / "slice-one.md").exists())

    def test_manual_analyzer_never_starts_node(self) -> None:
        pack_path = retro.write_pack(retro.build_pack(self.args()))
        spec = providers.ProviderSpec(role="analyzer", kind="manual")
        with mock.patch.object(retro_sdk.subprocess, "run") as run:
            result = retro_sdk.run_sdk(
                pack_path, retro.PROMPT, self.retro_dir,
                root=self.root,
                work_dir=self.runtime_dir / "retro" / "sdk",
                thread_file=self.runtime_dir / "retro" / "thread.json",
                provider=spec,
            )
        self.assertEqual(result, 2)
        run.assert_not_called()
        self.assertFalse((self.runtime_dir / "retro" / "sdk" / "cfg.json").exists())

    def test_sdk_bridge_imports_the_concrete_export_from_private_runtime(self) -> None:
        pack_path = retro.write_pack(retro.build_pack(self.args()))
        sdk_entry = self.root / "official-copilot" / "sdk" / "index.js"
        sdk_entry.parent.mkdir(parents=True)
        sdk_entry.write_text("export class CopilotClient {}\n", encoding="utf-8")
        work = self.runtime_dir / "retro" / "sdk"
        thread_file = self.runtime_dir / "retro" / "thread.json"
        spec = providers.ProviderSpec(
            role="analyzer", kind="copilot-sdk", model="fixture-model")

        def fake_node(command, **_kwargs):
            config = json.loads(Path(command[2]).read_text(encoding="utf-8"))
            self.assertEqual(config["sdkEntry"], str(sdk_entry))
            self.assertEqual(config["model"], "fixture-model")
            self.assertIn("pathToFileURL(cfg.sdkEntry)",
                          Path(command[1]).read_text(encoding="utf-8"))
            Path(config["out"]).write_text(json.dumps({
                "threadId": "thread-123",
                "resumed": False,
                "text": json.dumps(self.valid_result()),
            }), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with mock.patch.object(retro_sdk.providers, "preflight", return_value=[]), \
                mock.patch.object(retro_sdk.providers, "copilot_sdk_path",
                                  return_value=sdk_entry), \
                mock.patch.object(retro_sdk.subprocess, "run", side_effect=fake_node):
            result = retro_sdk.run_sdk(
                pack_path, retro.PROMPT, self.retro_dir,
                root=self.root, work_dir=work, thread_file=thread_file,
                provider=spec,
            )
        self.assertEqual(result, 0)
        self.assertTrue(thread_file.is_file())
        self.assertEqual(json.loads(thread_file.read_text(encoding="utf-8"))["thread_id"],
                         "thread-123")
        self.assertFalse((self.retro_dir / ".sdk").exists())
        self.assertFalse((self.retro_dir / "thread.json").exists())

    def test_immediate_consequence_warrants_retro_even_after_slice_marker(self) -> None:
        pack = {
            "artefacts": {}, "gate_logs": {}, "sessions": [],
            "notes": [{"path": "slice.md", "text": "consequence: native-crash\n"}],
        }
        with mock.patch.object(retro, "already_ran", return_value=True), \
                mock.patch.object(retro, "slice_key", return_value="slice@abc"):
            warranted, reason = retro.should_trigger(pack)
        self.assertTrue(warranted)
        self.assertIn("immediate consequence: native-crash", reason)

    def test_failed_analysis_does_not_write_completion_marker(self) -> None:
        pack_path = self.runtime_dir / "evidence" / "packs" / ("a" * 64 + ".json")
        with mock.patch.object(sys, "argv", [
                "retro.py", "--sdk", "--force", "--quiet"]), \
                mock.patch.object(
                    retro, "build_pack",
                    return_value={
                        "completion_key": "slice@abc",
                        "session_capture": {"complete": True},
                    }), \
                mock.patch.object(retro, "write_pack", return_value=pack_path), \
                mock.patch.object(retro_sdk, "run_sdk", return_value=1), \
                mock.patch.object(retro, "finalise_retro") as finalise:
            result = retro.main()
        self.assertEqual(result, 1)
        finalise.assert_not_called()
        self.assertFalse(retro.RAN_FILE.exists())

    def test_public_automatic_run_passes_the_frozen_completion_identity(self) -> None:
        pack_path = self.runtime_dir / "evidence" / "packs" / ("b" * 64 + ".json")
        pack = {
            "notes": [], "completion_key": "slice@abc",
            "session_capture": {"complete": True},
        }
        with mock.patch.object(sys, "argv", [
                "retro.py", "--sdk", "--force", "--quiet"]), \
                mock.patch.object(retro, "build_pack", return_value=pack), \
                mock.patch.object(retro, "write_pack", return_value=pack_path), \
                mock.patch.object(retro_sdk, "run_sdk", return_value=0), \
                mock.patch.object(
                    retro, "finalise_retro", return_value=0
                ) as finalise, \
                mock.patch.object(retro, "emit_print") as emit:
            result = retro.main()
        self.assertEqual(result, 0)
        finalise.assert_called_once_with(pack, pack_path, "slice@abc")
        emit.assert_not_called()

    def test_manual_evidence_preparation_never_marks_completion(self) -> None:
        pack_path = self.runtime_dir / "evidence" / "packs" / ("c" * 64 + ".json")
        pack = {
            "notes": [], "completion_key": "slice@abc",
            "session_capture": {"complete": True},
        }
        with mock.patch.object(sys, "argv", [
                "retro.py", "--print", "--force", "--quiet"]), \
                mock.patch.object(retro, "build_pack", return_value=pack), \
                mock.patch.object(retro, "write_pack", return_value=pack_path), \
                mock.patch.object(retro, "emit_print") as emit, \
                mock.patch.object(retro, "finalise_retro") as finalise:
            result = retro.main()
        self.assertEqual(result, 0)
        emit.assert_called_once_with(pack, pack_path)
        finalise.assert_not_called()
        self.assertFalse(retro.RAN_FILE.exists())

    def test_publication_failure_keeps_notes_and_has_no_completion_marker(self) -> None:
        pack = retro.build_pack(self.args())
        pack_path = retro.write_pack(pack)
        self.report_for(pack_path)
        ranked = SimpleNamespace(returncode=0, stdout="", stderr="")
        render_failed = SimpleNamespace(returncode=1, stdout="", stderr="render failed")
        with mock.patch.object(
                retro.subprocess, "run", side_effect=[ranked, render_failed]):
            result = retro.finalise_retro(
                pack, pack_path, pack["completion_key"]
            )
        self.assertEqual(result, 1)
        self.assertTrue(self.note.exists())
        self.assertFalse(retro.RAN_FILE.exists())

    def test_successful_publication_archives_then_marks_exact_snapshot(self) -> None:
        pack = retro.build_pack(self.args())
        pack_path = retro.write_pack(pack)
        self.published_report_for(pack_path)
        success = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(
                retro.subprocess, "run", side_effect=[success, success, success]
        ):
            result = retro.finalise_retro(
                pack, pack_path, pack["completion_key"])
        self.assertEqual(result, 0)
        self.assertFalse(self.note.exists())
        self.assertTrue((self.retro_dir / "archive" / self.note.name).is_file())
        marker = json.loads(retro.RAN_FILE.read_text(encoding="utf-8"))
        self.assertEqual(marker["completed_snapshots"][0]["sha256"], pack_path.stem)

    def test_marker_commit_failure_rolls_archived_notes_back(self) -> None:
        pack = retro.build_pack(self.args())
        pack_path = retro.write_pack(pack)
        self.published_report_for(pack_path)
        success = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(
                retro.subprocess, "run", side_effect=[success, success, success]
        ), \
                mock.patch.object(retro, "_commit_ran", side_effect=OSError("locked")):
            result = retro.finalise_retro(
                pack, pack_path, pack["completion_key"])
        self.assertEqual(result, 1)
        self.assertTrue(self.note.is_file())
        self.assertFalse((self.retro_dir / "archive" / self.note.name).exists())
        self.assertFalse(retro.RAN_FILE.exists())

    def test_manual_publish_closes_exact_bound_snapshot_idempotently(self) -> None:
        pack = retro.build_pack(self.args())
        pack_path = retro.write_pack(pack)
        report = self.published_report_for(pack_path)

        ok, detail = retro.complete_published_retro(report)
        self.assertTrue(ok, detail)
        self.assertFalse(self.note.exists())
        archived = self.retro_dir / "archive" / self.note.name
        self.assertTrue(archived.is_file())
        marker = json.loads(retro.RAN_FILE.read_text(encoding="utf-8"))
        self.assertEqual(marker["completed_snapshots"], [{
            "slice": pack["completion_key"],
            "evidence_pack": pack_path.relative_to(self.root).as_posix(),
            "sha256": pack_path.stem,
        }])

        ok, detail = retro.complete_published_retro(report)
        self.assertTrue(ok, detail)
        self.assertIn("already completed", detail)
        self.assertEqual(archived.read_text(encoding="utf-8"),
                         "The human corrected the same workflow twice.\n")

    def test_manual_publish_refuses_report_snapshot_mismatch_without_mutation(self) -> None:
        pack = retro.build_pack(self.args())
        pack_path = retro.write_pack(pack)
        report = self.published_report_for(pack_path)
        report.write_text(
            report.read_text(encoding="utf-8").replace(
                "session_snapshot: ", "session_snapshot: wrong/"),
            encoding="utf-8",
        )

        ok, detail = retro.complete_published_retro(report)
        self.assertFalse(ok)
        self.assertIn("does not match", detail)
        self.assertTrue(self.note.is_file())
        self.assertFalse(retro.RAN_FILE.exists())

    def test_manual_publish_refuses_unbound_legacy_pack_without_mutation(self) -> None:
        pack = retro.build_pack(self.args())
        pack.pop("completion_key")
        pack_path = retro.write_pack(pack)
        report = self.published_report_for(pack_path)

        ok, detail = retro.complete_published_retro(report)
        self.assertFalse(ok)
        self.assertIn("no bound completion key", detail)
        self.assertTrue(self.note.is_file())
        self.assertFalse(retro.RAN_FILE.exists())

    def test_manual_publish_refuses_unranked_report_without_mutation(self) -> None:
        pack = retro.build_pack(self.args())
        pack_path = retro.write_pack(pack)
        report = self.bound_report_for(pack_path)

        ok, detail = retro.complete_published_retro(report)
        self.assertFalse(ok)
        self.assertIn("not been ranked and published", detail)
        self.assertTrue(self.note.is_file())
        self.assertFalse(retro.RAN_FILE.exists())

    def test_manual_publish_refuses_unknown_snapshot_citation(self) -> None:
        pack = retro.build_pack(self.args())
        pack_path = retro.write_pack(pack)
        report = self.published_report_for(pack_path)
        report.write_text(
            report.read_text(encoding="utf-8").replace("S1:H1", "S1:H9"),
            encoding="utf-8", newline="\n",
        )

        ok, detail = retro.complete_published_retro(report)
        self.assertFalse(ok)
        self.assertIn("unknown human turn", detail)
        self.assertTrue(self.note.is_file())
        self.assertFalse(retro.RAN_FILE.exists())

    def test_manual_publish_rolls_back_if_report_changes_during_completion(self) -> None:
        pack = retro.build_pack(self.args())
        pack_path = retro.write_pack(pack)
        report = self.published_report_for(pack_path)

        with mock.patch.object(
                retro, "_publication_unchanged", side_effect=[True, False]):
            ok, detail = retro.complete_published_retro(report)

        self.assertFalse(ok)
        self.assertIn("changed during completion", detail)
        self.assertTrue(self.note.is_file())
        self.assertFalse((self.retro_dir / "archive" / self.note.name).exists())
        self.assertFalse(retro.RAN_FILE.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
