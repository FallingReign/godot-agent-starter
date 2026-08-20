#!/usr/bin/env python3
"""Tests for the dispatch prompt artifacts.

    python tools/tests/test_retro_queue.py

The property worth the most here is the last one: building the dispatch queue
must not open a session log. It is proved, not asserted -- `digest_one` and
`discover` are replaced with functions that raise, and the queue still builds.
That is the defect this module exists to fix, and it is the only way to catch
its return.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import board            # noqa: E402
import retro_queue      # noqa: E402
import retro_rank       # noqa: E402
import session_digest   # noqa: E402

REAL_CITATION_REPORT = retro_rank.citation_report

FINDINGS = """# Retrospective 2026-01-01

Header prose.

## Finding: the gate ran twice for one edit

sessions: [S1; S2]
human_turns: [S1:H1; S2:H9]
mechanical: []
recurs: true
severity: none
fix_files: [tools/thing.py]
fix_lines: 12

Body prose for the first finding.

**Proposed change** - do the other thing.

## Finding: a second, differently named problem

sessions: [S1]
human_turns: [S1:H1]
mechanical: []
recurs: false
severity: false-green
fix_files: [check.py]
fix_lines: 3

Body prose for the second finding.
"""

# Two findings citing S1 between them: if citations were resolved per finding,
# S1 would be digested twice. The counter in test_single_citation_pass is what
# turns that into a failing test rather than a merely slow one.
FAKE_SESSIONS = [
    {"log": Path("nonexistent-1.jsonl"), "id": "s1"},
    {"log": Path("nonexistent-2.jsonl"), "id": "s2"},
]


def _fake_digest(sess: dict, index: int) -> dict:
    return {
        "index": index,
        "started": "2026-01-0%dT00:00:00Z" % index,
        "human": [f"human message {i} of S{index}" for i in range(1, 10)],
    }


class QueueTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.findings = self.dir / "2026-01-01-findings.md"
        self.findings.write_text(FINDINGS, encoding="utf-8")
        self.queue = self.dir / "queue"

        self._saved = (session_digest.discover, session_digest.digest_one,
                       retro_rank.session_persona, retro_queue.ROOT)
        session_digest.discover = lambda repo, roots=None: list(FAKE_SESSIONS)
        session_digest.digest_one = _fake_digest
        retro_rank.session_persona = lambda log: "game-builder"
        retro_queue.ROOT = self.dir

    def tearDown(self) -> None:
        (session_digest.discover, session_digest.digest_one,
         retro_rank.session_persona, retro_queue.ROOT) = self._saved
        retro_rank.citation_report = REAL_CITATION_REPORT
        self.tmp.cleanup()

    def build(self) -> list[dict]:
        return retro_queue.build(findings_dir=self.dir, queue_dir=self.queue)


class TestGeneration(QueueTestCase):
    def test_artifacts_and_index_written(self) -> None:
        items = self.build()
        self.assertEqual(len(items), 2)
        index = json.loads((self.queue / "index.json").read_text(encoding="utf-8"))
        self.assertEqual(index["schema"], 1)
        self.assertEqual(index["items"], [i["slug"] for i in items])
        for item in items:
            self.assertTrue((self.queue / f"{item['slug']}.json").is_file())

    def test_keys_match_the_contract(self) -> None:
        item = self.build()[0]
        self.assertEqual(set(item), {
            "schema", "slug", "title", "normalised_title", "severity", "sessions",
            "fix_files", "fix_lines", "human_turns", "body", "prompt",
            "source_file", "source_sha256", "generated_at",
        })
        self.assertEqual(item["normalised_title"],
                         retro_rank.normalise_title(item["title"]))
        self.assertEqual(item["human_turns"][0]["cite"], "S1:H1")
        self.assertEqual(item["human_turns"][0]["quote"], "human message 1 of S1")

    def test_prompt_carries_the_restrictions(self) -> None:
        prompt = self.build()[0]["prompt"]
        self.assertIn("kit-builder", prompt)
        self.assertIn("do not touch src/", prompt)
        self.assertIn("`python check.py`", prompt)
        self.assertIn("## Finding: the gate ran twice for one edit", prompt)
        self.assertIn("Body prose for the first finding.", prompt)

    def test_ordering_follows_the_findings_file(self) -> None:
        items = self.build()
        self.assertEqual(
            [i["title"] for i in items],
            ["the gate ran twice for one edit", "a second, differently named problem"],
        )

    def test_single_citation_pass(self) -> None:
        calls: list[int] = []

        def counting(sess: dict, index: int) -> dict:
            calls.append(index)
            return _fake_digest(sess, index)

        session_digest.digest_one = counting
        self.build()
        # S1 is cited by both findings, S2 by one. One digest each, not three.
        self.assertEqual(sorted(calls), [1, 2])

    def test_deleted_finding_stops_being_dispatchable(self) -> None:
        self.build()
        self.findings.write_text(
            FINDINGS.split("## Finding: a second")[0], encoding="utf-8")
        items = self.build()
        self.assertEqual(len(items), 1)
        self.assertFalse((self.queue / "a-second-differently-named-problem.json").is_file())


class TestLoading(QueueTestCase):
    def test_load_index_and_item(self) -> None:
        built = self.build()
        index = retro_queue.load_index(self.queue)
        self.assertEqual(index["items"], [i["slug"] for i in built])
        loaded = retro_queue.load_item(built[0]["slug"], self.queue)
        self.assertEqual(loaded, built[0])

    def test_missing_item_is_none_not_an_exception(self) -> None:
        self.build()
        self.assertIsNone(retro_queue.load_item("no-such-finding", self.queue))

    def test_load_index_of_an_empty_dir(self) -> None:
        self.assertEqual(retro_queue.load_index(self.dir / "nothing")["items"], [])


class TestStaleness(QueueTestCase):
    def test_fresh_artifact_is_not_stale(self) -> None:
        self.assertFalse(retro_queue.is_stale(self.build()[0]))

    def test_edited_findings_file_makes_it_stale(self) -> None:
        item = self.build()[0]
        self.findings.write_text(FINDINGS + "\nan edit.\n", encoding="utf-8")
        self.assertTrue(retro_queue.is_stale(item))

    def test_deleted_source_is_stale(self) -> None:
        item = self.build()[0]
        self.findings.unlink()
        self.assertTrue(retro_queue.is_stale(item))


class TestRenderPrompt(QueueTestCase):
    def test_no_comment_returns_the_artifact_prompt_unchanged(self) -> None:
        item = self.build()[0]
        self.assertEqual(retro_queue.render_prompt(item, ""), item["prompt"])
        self.assertEqual(retro_queue.render_prompt(item, "   \n "), item["prompt"])

    def test_comment_is_appended_under_a_delimited_heading(self) -> None:
        item = self.build()[0]
        out = retro_queue.render_prompt(item, "Keep the CLI, drop the flag.")
        self.assertTrue(out.startswith(item["prompt"].rstrip("\n")))
        self.assertIn(retro_queue.COMMENT_HEADING, out)
        self.assertTrue(out.rstrip("\n").endswith("Keep the CLI, drop the flag."))

    def test_deterministic(self) -> None:
        item = self.build()[0]
        self.assertEqual(retro_queue.render_prompt(item, "same words"),
                         retro_queue.render_prompt(item, "same words"))


class TestNoSessionLogAccess(QueueTestCase):
    """The point of the whole artifact: dispatch reads the filesystem only."""

    def setUp(self) -> None:
        super().setUp()
        self.build()

        def explode(*args, **kwargs):
            raise AssertionError("a session log was opened during dispatch")

        session_digest.digest_one = explode
        session_digest.discover = explode
        retro_rank.citation_report = explode

        self._saved_board = (board.RETRO_DIR, board.load_accepted)
        board.RETRO_DIR = self.dir
        titles = [json.loads(p.read_text(encoding="utf-8"))["title"]
                  for p in sorted(self.queue.glob("*.json")) if p.name != "index.json"]
        board.load_accepted = lambda: [{"finding": t} for t in titles]
        self._saved_queue_dir = retro_queue.QUEUE_DIR
        retro_queue.QUEUE_DIR = self.queue

    def tearDown(self) -> None:
        board.RETRO_DIR, board.load_accepted = self._saved_board
        retro_queue.QUEUE_DIR = self._saved_queue_dir
        super().tearDown()

    def test_build_dispatch_queue_opens_no_session_log(self) -> None:
        queue = board.build_dispatch_queue()
        self.assertEqual(len(queue), 2)
        for entry in queue:
            self.assertTrue(entry["ok"], entry.get("error"))
            self.assertFalse(entry["stale"])
            self.assertIn("do not touch src/", entry["prompt"])

    def test_missing_artifact_is_reported_not_rebuilt(self) -> None:
        for p in self.queue.glob("*.json"):
            if p.name != "index.json":
                p.unlink()
        queue = board.build_dispatch_queue()
        self.assertTrue(queue)
        for entry in queue:
            self.assertFalse(entry["ok"])
            self.assertTrue(entry["missing"])
            self.assertIn("retro_rank.py", entry["error"])

    def test_stale_artifact_is_reported_not_rebuilt(self) -> None:
        self.findings.write_text(FINDINGS + "\nan edit.\n", encoding="utf-8")
        queue = board.build_dispatch_queue()
        self.assertTrue(queue)
        for entry in queue:
            self.assertFalse(entry["ok"])
            self.assertTrue(entry["stale"])
            self.assertIn("stale", entry["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
