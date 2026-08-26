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
        retro.session_digest.discover = lambda root, roots=None: list(sessions)
        retro.session_digest.digest_one = lambda session, index: {
            "index": index,
            "id": session["id"],
            "name": "fixture",
            "started": "2026-01-01T00:00:00Z",
            "updated": "2026-01-01T01:00:00Z",
            "turns": 2,
            "cost": 0.5,
            "model": "fixture-model",
            "persona": "game-builder",
            "gate_fails": {"schema": 2},
            "gate_passes": 1,
            "loops": {"python check.py": 4},
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
        return SimpleNamespace(sessions=None, max_sessions=3, baseline=None)

    def valid_result(self) -> dict:
        return {
            "summary": "One recurring decision failure.",
            "findings": [{
                "title": "Decision state is invisible",
                "sessions": ["S1"],
                "human_turns": ["S1:H1"],
                "mechanical": ["LOOP python check.py x4 (S1)"],
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

    def test_pack_is_content_addressed_and_contains_notes_and_stable_citations(self) -> None:
        first = retro.build_pack(self.args())
        first_path = retro.write_pack(first)
        second = retro.build_pack(self.args())
        second_path = retro.write_pack(second)
        self.assertEqual(first_path, second_path)
        self.assertEqual(first["notes"][0]["text"],
                         self.note.read_bytes().decode("utf-8"))
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

    def test_prompt_and_ran_marker_are_private_runtime_state(self) -> None:
        pack = retro.build_pack(self.args())
        pack_path = retro.write_pack(pack)
        retro.emit_print(pack, pack_path)
        retro.mark_ran("slice@abc")
        self.assertTrue((self.runtime_dir / "retro" / "sdk" / "prompt.md").is_file())
        self.assertTrue((self.runtime_dir / "retro" / "ran.json").is_file())
        self.assertTrue(retro.already_ran("slice@abc"))
        self.assertFalse((self.retro_dir / "prompt.md").exists())
        self.assertFalse((self.retro_dir / ".ran").exists())

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
