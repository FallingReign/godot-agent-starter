#!/usr/bin/env python3
"""Tests for immutable, evidence-bound retrospective dispatch artifacts."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import board             # noqa: E402
import retro_queue       # noqa: E402
import retro_rank        # noqa: E402
import session_digest    # noqa: E402
import session_evidence  # noqa: E402


def _scratch_parent() -> Path:
    configured = os.environ.get("KIT_TEST_TMPDIR", "").strip()
    return Path(configured) if configured else TOOLS.parent / ".checklogs"


def _digest(session_id: str, label: str, count: int = 9) -> dict:
    return {
        "id": session_id,
        "name": label,
        "started": "2026-01-01T00:00:00Z",
        "updated": "2026-01-01T01:00:00Z",
        "human": [f"{label} human message {number}" for number in range(1, count + 1)],
        "sources": {"events": {"sha256": "a" * 64, "bytes": 100}},
        "persona": "game-builder",
    }


def _write_snapshot(root: Path, *digests: dict) -> str:
    manifest = session_evidence.build_manifest(root, list(digests))
    path = session_evidence.write_manifest(
        root / ".kit" / "runtime" / "evidence" / "sessions", manifest)
    return path.relative_to(root).as_posix()


def _findings(snapshot: str, *, first_cite: str = "S1:H1",
              first_sections: str | None = None,
              first_fix_files: str = "tools/thing.py", first_fix_lines: int = 12) -> str:
    sections = first_sections if first_sections is not None else """**Problem** - The gate ran twice.

**Proposal** - Make the ranking pass reusable.

**Measure** - One ranking pass produces both artifacts."""
    snapshot_line = f"session_snapshot: {snapshot}\n" if snapshot else ""
    return f"""# Retrospective 2026-01-01

{snapshot_line}Header prose.

## Finding: the gate ran twice for one edit

sessions: [S1; S2]
human_turns: [{first_cite}; S2:H9]
mechanical: []
recurs: true
severity: none
fix_files: [{first_fix_files}]
fix_lines: {first_fix_lines}

{sections}

## Finding: a second, differently named problem

sessions: [S1]
human_turns: [S1:H1]
mechanical: []
recurs: false
severity: false-green
fix_files: [check.py]
fix_lines: 3

**Problem** - The old result was a false success.

**Proposal** - Require structured evidence.

**Measure** - Exit zero without evidence is unverified.
"""


class QueueTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _scratch_parent() / f"retro-queue-test-{uuid.uuid4().hex}"
        self.root.mkdir(parents=True)
        self.retro = self.root / "docs" / "retro"
        self.retro.mkdir(parents=True)
        self.session_evidence = self.root / ".kit" / "runtime" / "evidence" / "sessions"
        self.queue = self.root / ".kit" / "runtime" / "retro" / "queue"
        self._saved = {
            "root": retro_queue.ROOT,
            "queue_dir": retro_queue.QUEUE_DIR,
            "session_evidence": retro_queue.SESSION_EVIDENCE_DIR,
            "rank_root": retro_rank.ROOT,
            "rank_session_evidence": retro_rank.SESSION_EVIDENCE_DIR,
            "discover": session_digest.discover,
            "digest_one": session_digest.digest_one,
            "citation_report": retro_rank.citation_report,
        }
        retro_queue.ROOT = self.root
        retro_queue.QUEUE_DIR = self.queue
        retro_queue.SESSION_EVIDENCE_DIR = self.session_evidence
        retro_rank.ROOT = self.root
        retro_rank.SESSION_EVIDENCE_DIR = self.session_evidence
        self.snapshot = _write_snapshot(
            self.root, _digest("session-one", "S1"), _digest("session-two", "S2"))
        self.findings = self.retro / "2026-01-01-findings.md"
        self.findings.write_text(_findings(self.snapshot), encoding="utf-8")

    def tearDown(self) -> None:
        retro_queue.ROOT = self._saved["root"]
        retro_queue.QUEUE_DIR = self._saved["queue_dir"]
        retro_queue.SESSION_EVIDENCE_DIR = self._saved["session_evidence"]
        retro_rank.ROOT = self._saved["rank_root"]
        retro_rank.SESSION_EVIDENCE_DIR = self._saved["rank_session_evidence"]
        session_digest.discover = self._saved["discover"]
        session_digest.digest_one = self._saved["digest_one"]
        retro_rank.citation_report = self._saved["citation_report"]
        shutil.rmtree(self.root)

    def build(self) -> list[dict]:
        return retro_queue.build(findings_dir=self.retro, queue_dir=self.queue)


class TestGeneration(QueueTestCase):
    def test_artifacts_and_index_written(self) -> None:
        items = self.build()
        self.assertEqual(len(items), 2)
        index = json.loads((self.queue / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["schema"], retro_queue.SCHEMA)
        self.assertEqual(index["items"], [item["slug"] for item in items])
        for item in items:
            self.assertTrue((self.queue / f"{item['slug']}.json").is_file())
        self.assertFalse((self.retro / "queue").exists())

    def test_keys_and_valid_eligibility_match_the_contract(self) -> None:
        item = self.build()[0]
        self.assertEqual(set(item), {
            "schema", "slug", "title", "normalised_title", "severity", "sessions",
            "fix_files", "fix_lines", "human_turns", "body", "prompt",
            "evidence_snapshot", "dispatchable", "dispatch_blockers",
            "source_file", "source_sha256", "generated_at", "prompt_sha256",
            "template_sha256", "artifact_sha256",
        })
        self.assertEqual(item["normalised_title"],
                         retro_rank.normalise_title(item["title"]))
        self.assertEqual(item["human_turns"][0], {
            "cite": "S1:H1", "quote": "S1 human message 1"})
        self.assertEqual(item["evidence_snapshot"], self.snapshot)
        self.assertTrue(item["dispatchable"])
        self.assertEqual(item["dispatch_blockers"], [])
        self.assertEqual(retro_queue.dispatch_eligibility(item), (True, []))
        review = retro_queue.review_identity(item)
        self.assertEqual(review["artifact_schema"], retro_queue.SCHEMA)
        self.assertEqual(review["artifact_sha256"], item["artifact_sha256"])
        self.assertEqual(len(review["review_sha256"]), 64)

    def test_prompt_carries_restrictions_and_strict_finding(self) -> None:
        prompt = self.build()[0]["prompt"]
        self.assertIn("kit-builder", prompt)
        self.assertIn("do not touch game or design files", prompt)
        self.assertIn("`kit verify --static`", prompt)
        self.assertNotIn("python kit.py", prompt)
        self.assertIn("human-only", prompt)
        self.assertIn("## Finding: the gate ran twice for one edit", prompt)
        self.assertIn("**Measure** - One ranking pass", prompt)

    def test_ordering_follows_the_findings_file(self) -> None:
        self.assertEqual(
            [item["title"] for item in self.build()],
            ["the gate ran twice for one edit", "a second, differently named problem"],
        )

    def test_deleted_finding_stops_being_dispatchable(self) -> None:
        self.build()
        self.findings.write_text(
            _findings(self.snapshot).split("## Finding: a second")[0], encoding="utf-8")
        items = self.build()
        self.assertEqual(len(items), 1)
        self.assertFalse((self.queue / "a-second-differently-named-problem.json").is_file())


class TestEvidenceBinding(QueueTestCase):
    def test_build_never_uses_live_discovery_or_citation_fallback(self) -> None:
        def explode(*_args, **_kwargs):
            raise AssertionError("live session discovery was used")

        session_digest.discover = explode
        session_digest.digest_one = explode
        retro_rank.citation_report = explode
        items = self.build()
        self.assertTrue(all(item["dispatchable"] for item in items))

    def test_two_reports_resolve_S1_against_their_own_snapshots(self) -> None:
        self.findings.unlink()
        first_snapshot = _write_snapshot(self.root, _digest("first-id", "first", 1))
        second_snapshot = _write_snapshot(self.root, _digest("second-id", "second", 1))
        report_template = """# Retrospective 2026-01-0{day}

session_snapshot: {snapshot}

## Finding: {title}

sessions: [S1]
human_turns: [S1:H1]
mechanical: []
recurs: true
severity: none
fix_files: [tools/{day}.py]
fix_lines: 2

**Problem** - Problem {day}.

**Proposal** - Proposal {day}.

**Measure** - Measure {day}.
"""
        (self.retro / "2026-01-01-findings.md").write_text(
            report_template.format(day=1, snapshot=first_snapshot, title="first report"),
            encoding="utf-8")
        (self.retro / "2026-01-02-findings.md").write_text(
            report_template.format(day=2, snapshot=second_snapshot, title="second report"),
            encoding="utf-8")

        items = {item["title"]: item for item in self.build()}
        self.assertEqual(items["first report"]["human_turns"][0]["quote"],
                         "first human message 1")
        self.assertEqual(items["second report"]["human_turns"][0]["quote"],
                         "second human message 1")
        self.assertEqual(items["first report"]["evidence_snapshot"], first_snapshot)
        self.assertEqual(items["second report"]["evidence_snapshot"], second_snapshot)

    def test_legacy_report_is_readable_but_not_dispatchable(self) -> None:
        self.findings.write_text(_findings(""), encoding="utf-8")
        items = self.build()
        self.assertEqual(len(items), 2)
        self.assertFalse(items[0]["dispatchable"])
        self.assertIn("evidence_snapshot: missing (legacy report)",
                      items[0]["dispatch_blockers"])

    def test_legacy_copilot_v1_snapshot_remains_dispatchable(self) -> None:
        repository = os.path.normcase(os.path.abspath(self.root)).replace("\\", "/").rstrip("/")
        manifest = {
            "schema": 1,
            "kind": "copilot-session-evidence",
            "repository": repository,
            "sessions": [{
                "ordinal": 1,
                "session_id": "legacy-session",
                "name": "legacy",
                "started": "2026-01-01T00:00:00Z",
                "updated": "2026-01-01T01:00:00Z",
                "sources": {},
                "warnings": [],
                "evidence": {
                    "turns": 1, "cost": 1.0, "model": "legacy",
                    "persona": "game-builder", "gate_fails": {},
                    "gate_passes": 0, "loops": {}, "rewrites": {},
                    "human_messages": [{
                        "citation": "legacy-session:H1", "text": "Legacy correction."
                    }],
                },
            }],
        }
        path = session_evidence.write_manifest(self.session_evidence, manifest)
        snapshot = path.relative_to(self.root).as_posix()
        self.findings.write_text(_findings(
            snapshot, first_cite="S1:H1").replace("sessions: [S1; S2]", "sessions: [S1]")
            .replace("; S2:H9", ""), encoding="utf-8")

        items = self.build()
        self.assertTrue(items[0]["dispatchable"], items[0]["dispatch_blockers"])
        self.assertEqual(items[0]["human_turns"][0]["quote"], "Legacy correction.")

    def test_tampered_snapshot_is_rejected_by_build_and_revalidation(self) -> None:
        snapshot_path = self.root / self.snapshot
        snapshot_path.write_bytes(snapshot_path.read_bytes().replace(
            b"S1 human message 1", b"changed human message"))
        item = self.build()[0]
        self.assertFalse(item["dispatchable"])
        self.assertIn("evidence_snapshot: content hash does not match filename",
                      item["dispatch_blockers"])
        self.assertFalse(retro_queue.dispatch_eligibility(item)[0])

    def test_malformed_content_addressed_snapshot_is_rejected(self) -> None:
        malformed = b"{not-json\n"
        name = hashlib.sha256(malformed).hexdigest() + ".json"
        path = self.session_evidence / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(malformed)
        self.findings.write_text(
            _findings(path.relative_to(self.root).as_posix()), encoding="utf-8")
        item = self.build()[0]
        self.assertFalse(item["dispatchable"])
        self.assertIn("evidence_snapshot: content is not valid UTF-8 JSON",
                      item["dispatch_blockers"])

    def test_snapshot_outside_configured_runtime_root_is_rejected(self) -> None:
        legacy = self.root / "docs" / "retro" / "evidence" / "sessions"
        manifest = session_evidence.build_manifest(
            self.root, [_digest("legacy", "legacy", 1)])
        path = session_evidence.write_manifest(legacy, manifest)
        self.findings.write_text(
            _findings(path.relative_to(self.root).as_posix()), encoding="utf-8")
        item = self.build()[0]
        self.assertFalse(item["dispatchable"])
        self.assertIn("configured session evidence root",
                      " ".join(item["dispatch_blockers"]))

    def test_snapshot_repository_provenance_is_rejected(self) -> None:
        manifest = session_evidence.build_manifest(
            self.root / "different-repository", [_digest("foreign", "foreign", 1)])
        path = session_evidence.write_manifest(self.session_evidence, manifest)
        self.findings.write_text(
            _findings(path.relative_to(self.root).as_posix()), encoding="utf-8")
        item = self.build()[0]
        self.assertFalse(item["dispatchable"])
        self.assertIn("repository provenance", " ".join(item["dispatch_blockers"]))


class TestEligibilityRules(QueueTestCase):
    def test_missing_measure_blocks_dispatch(self) -> None:
        sections = """**Problem** - It repeats.

**Proposal** - Reuse the pass."""
        self.findings.write_text(
            _findings(self.snapshot, first_sections=sections), encoding="utf-8")
        item = self.build()[0]
        self.assertFalse(item["dispatchable"])
        self.assertIn("sections: expected exactly Problem, Proposal, Measure in that order",
                      item["dispatch_blockers"])

    def test_unresolved_human_turn_blocks_dispatch(self) -> None:
        self.findings.write_text(
            _findings(self.snapshot, first_cite="S1:H99"), encoding="utf-8")
        item = self.build()[0]
        self.assertFalse(item["dispatchable"])
        self.assertIn("human_turns: unresolved citation S1:H99",
                      item["dispatch_blockers"])

    def test_non_kit_path_and_nonpositive_estimate_block_dispatch(self) -> None:
        self.findings.write_text(_findings(
            self.snapshot, first_fix_files="src/player.gd", first_fix_lines=0),
            encoding="utf-8")
        item = self.build()[0]
        self.assertFalse(item["dispatchable"])
        self.assertIn("fix_files: unsafe or non-kit path 'src/player.gd'",
                      item["dispatch_blockers"])
        self.assertIn("fix_lines: must be a positive integer", item["dispatch_blockers"])

    def test_empty_fix_files_block_dispatch(self) -> None:
        self.findings.write_text(_findings(
            self.snapshot, first_fix_files=""), encoding="utf-8")
        item = self.build()[0]
        self.assertFalse(item["dispatchable"])
        self.assertIn("fix_files: at least one kit-owned path is required",
                      item["dispatch_blockers"])

    def test_case_cannot_bypass_game_state_exclusions(self) -> None:
        self.findings.write_text(_findings(
            self.snapshot,
            first_fix_files="docs/DESIGN/secret.md; docs/DECISIONS.md"),
            encoding="utf-8")
        item = self.build()[0]
        self.assertFalse(item["dispatchable"])
        blockers = " ".join(item["dispatch_blockers"])
        self.assertIn("docs/DESIGN/secret.md", blockers)
        self.assertIn("docs/DECISIONS.md", blockers)


class TestLoadingAndStaleness(QueueTestCase):
    def test_load_index_item_and_missing_item(self) -> None:
        built = self.build()
        self.assertEqual(retro_queue.load_index(self.queue)["items"],
                         [item["slug"] for item in built])
        self.assertEqual(retro_queue.load_item(built[0]["slug"], self.queue), built[0])
        self.assertIsNone(retro_queue.load_item("no-such-finding", self.queue))

    def test_empty_index(self) -> None:
        self.assertEqual(retro_queue.load_index(self.root / "nothing")["items"], [])

    def test_source_change_or_delete_is_stale(self) -> None:
        item = self.build()[0]
        self.assertFalse(retro_queue.is_stale(item))
        self.findings.write_text(_findings(self.snapshot) + "\nedit\n", encoding="utf-8")
        self.assertTrue(retro_queue.is_stale(item))
        self.findings.unlink()
        self.assertTrue(retro_queue.is_stale(item))

    def test_noncanonical_and_traversal_slugs_never_reach_other_json(self) -> None:
        item = self.build()[0]
        outside = self.queue.parent / "outside.json"
        outside.write_text(json.dumps(item), encoding="utf-8")
        for slug in (
            "../outside", "..\\outside", "/outside", "A-Title", "two--hyphens",
            "a" * (retro_queue.MAX_SLUG_CHARS + 1),
        ):
            with self.subTest(slug=slug):
                self.assertIsNone(retro_queue.canonical_slug(slug))
                self.assertIsNone(retro_queue.load_item(slug, self.queue))

    def test_tampered_complete_artifact_digest_fails_closed(self) -> None:
        item = self.build()[0]
        path = self.queue / f"{item['slug']}.json"
        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["prompt"] += "\nunreviewed instruction\n"
        path.write_text(json.dumps(tampered), encoding="utf-8")
        loaded = retro_queue.load_item(item["slug"], self.queue)
        self.assertIsNotNone(loaded)
        dispatchable, blockers = retro_queue.dispatch_eligibility(loaded)
        self.assertFalse(dispatchable)
        self.assertIn("artifact: prompt digest does not match its exact bytes", blockers)

    def test_artifact_symlink_is_not_a_queue_item(self) -> None:
        item = self.build()[0]
        path = self.queue / f"{item['slug']}.json"
        real_lstat = Path.lstat

        def lstat(candidate: Path):
            if candidate == path:
                return mock.Mock(st_mode=stat.S_IFLNK | 0o777)
            return real_lstat(candidate)

        with mock.patch.object(Path, "lstat", autospec=True, side_effect=lstat):
            self.assertIsNone(retro_queue.load_item(item["slug"], self.queue))

    def test_findings_source_must_be_a_direct_regular_retro_file(self) -> None:
        item = self.build()[0]
        path = self.queue / f"{item['slug']}.json"
        outside = self.root / "outside-findings.md"
        outside.write_bytes(self.findings.read_bytes())
        item["source_file"] = outside.relative_to(self.root).as_posix()
        item["source_sha256"] = hashlib.sha256(outside.read_bytes()).hexdigest()
        item["artifact_sha256"] = retro_queue.artifact_sha256(item)
        path.write_text(json.dumps(item), encoding="utf-8")
        self.assertIsNone(retro_queue.load_item(item["slug"], self.queue))


class TestRenderAndBoardRead(QueueTestCase):
    def test_comment_render_is_deterministic(self) -> None:
        item = self.build()[0]
        self.assertEqual(retro_queue.render_prompt(item, ""), item["prompt"])
        first = retro_queue.render_prompt(item, "Keep the CLI, drop the flag.")
        second = retro_queue.render_prompt(item, "Keep the CLI, drop the flag.")
        self.assertEqual(first, second)
        self.assertIn(retro_queue.COMMENT_HEADING, first)
        self.assertTrue(first.rstrip().endswith("Keep the CLI, drop the flag."))

    def test_board_queue_is_filesystem_only_and_exposes_eligibility(self) -> None:
        items = self.build()

        def explode(*_args, **_kwargs):
            raise AssertionError("a session log was opened during dispatch")

        session_digest.digest_one = explode
        session_digest.discover = explode
        retro_rank.citation_report = explode
        saved = (board.RETRO_DIR, board.load_accepted, retro_queue.QUEUE_DIR)
        board.RETRO_DIR = self.retro
        board.load_accepted = lambda: [
            {"finding": item["title"]} for item in items]
        retro_queue.QUEUE_DIR = self.queue
        try:
            queue = board.build_dispatch_queue()
        finally:
            board.RETRO_DIR, board.load_accepted, retro_queue.QUEUE_DIR = saved
        self.assertEqual(len(queue), 2)
        for entry in queue:
            self.assertTrue(entry["ok"], entry.get("error"))
            self.assertTrue(entry["dispatchable"])
            self.assertEqual(entry["dispatch_blockers"], [])
            self.assertEqual(entry["evidence_snapshot"], self.snapshot)


if __name__ == "__main__":
    unittest.main(verbosity=2)
