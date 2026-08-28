#!/usr/bin/env python3
"""Regression tests for fail-closed retrospective decision ledgers."""
from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

import board  # noqa: E402
import retro_html  # noqa: E402
import retro_ledger  # noqa: E402
import retro_rank  # noqa: E402


def _process_append(path_text: str, lock_dir_text: str, finding: str,
                    inside, release) -> None:
    path = Path(path_text)
    lock_dir = Path(lock_dir_text)

    def append(entries: list[dict]) -> list[dict]:
        inside.set()
        if release is not None:
            if not release.wait(10):
                raise RuntimeError("test process timed out waiting for release")
        entries.append({"finding": finding})
        return entries

    retro_ledger.update_entries(
        path, append, label="fixture decisions", lock_dir=lock_dir
    )


class DecisionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        parent = ROOT / ".checklogs" / "tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.scratch = parent / f"retro-ledger-{uuid.uuid4().hex}"
        self.scratch.mkdir()
        self.accepted = self.scratch / "accepted.json"
        self.deferred = self.scratch / "deferred.json"
        self.locks = self.scratch / "locks"
        self.patches = [
            mock.patch.object(board, "ACCEPTED_FILE", self.accepted),
            mock.patch.object(board, "DECISION_LOCK_DIR", self.locks),
            mock.patch.object(retro_html, "ACCEPTED_FILE", self.accepted),
            mock.patch.object(retro_rank, "DEFERRED_FILE", self.deferred),
            mock.patch.object(retro_rank, "DECISION_LOCK_DIR", self.locks),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        resolved = self.scratch.resolve()
        expected = (ROOT / ".checklogs" / "tests").resolve()
        if resolved.parent != expected or not resolved.name.startswith("retro-ledger-"):
            raise AssertionError(f"refusing to remove unexpected scratch: {resolved}")
        shutil.rmtree(resolved)

    def test_absent_ledgers_are_empty_and_valid_writes_are_atomic(self) -> None:
        self.assertEqual(board.load_accepted(), [])
        self.assertEqual(retro_rank.load_deferred(), [])

        board.save_accepted([{"finding": "accepted"}])
        retro_rank.save_deferred([{"finding": "deferred"}])

        loaded = board.load_accepted()
        self.assertEqual(loaded[0]["finding"], "accepted")
        self.assertEqual(loaded[0]["state"], "awaiting_review")
        self.assertEqual(retro_html.load_accepted(), [{"finding": "accepted"}])
        self.assertEqual(retro_rank.load_deferred(), [{"finding": "deferred"}])
        self.assertEqual(list(self.scratch.glob("*.tmp")), [])

    def test_corrupt_accepted_ledger_blocks_every_reader_and_never_overwrites(self) -> None:
        corrupt = b"{not-json\n"
        self.accepted.write_bytes(corrupt)

        for operation in (
            board.load_accepted,
            retro_html.load_accepted,
            board.migrate_accepted_file,
            lambda: board.save_accepted([{"finding": "replacement"}]),
            lambda: retro_html.save_accepted([{"finding": "replacement"}]),
        ):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    retro_ledger.LedgerError,
                    "accepted.json.*repair it before continuing",
                ):
                    operation()
                self.assertEqual(self.accepted.read_bytes(), corrupt)

    def test_corrupt_deferred_ledger_blocks_read_and_write_without_data_loss(self) -> None:
        corrupt = json.dumps({"finding": "not-an-array"}).encode("utf-8")
        self.deferred.write_bytes(corrupt)

        with self.assertRaisesRegex(retro_ledger.LedgerError, "JSON array"):
            retro_rank.load_deferred()
        with self.assertRaisesRegex(retro_ledger.LedgerError, "JSON array"):
            retro_rank.save_deferred([{"finding": "replacement"}])
        self.assertEqual(self.deferred.read_bytes(), corrupt)

    def test_failed_atomic_replace_preserves_previous_valid_ledger(self) -> None:
        board.save_accepted([{"finding": "original"}])
        before = self.accepted.read_bytes()

        with mock.patch.object(
            retro_ledger.os, "replace", side_effect=OSError("simulated replace failure")
        ):
            with self.assertRaisesRegex(OSError, "simulated replace failure"):
                board.save_accepted([{"finding": "new"}])

        self.assertEqual(self.accepted.read_bytes(), before)
        self.assertEqual(list(self.scratch.glob(".*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "Windows sharing violation behavior")
    def test_atomic_replace_retries_only_bounded_windows_sharing_violation(self) -> None:
        sharing = PermissionError("sharing violation")
        sharing.winerror = 5
        with mock.patch.object(
            retro_ledger.os, "replace", side_effect=[sharing, None]
        ) as replace, mock.patch.object(retro_ledger.time, "sleep") as sleep:
            retro_ledger._replace_file(
                self.scratch / "temporary", self.scratch / "destination"
            )

        self.assertEqual(2, replace.call_count)
        sleep.assert_called_once_with(0.02)

    def test_stale_loaded_list_is_rejected_instead_of_overwriting_new_decision(self) -> None:
        board.save_accepted([{"finding": "original"}])
        stale = retro_html.load_accepted()
        board.save_accepted([
            {"finding": "original"},
            {"finding": "concurrent decision"},
        ])
        stale.append({"finding": "stale renderer update"})

        with self.assertRaisesRegex(
                retro_ledger.LedgerError, "changed after it was read"):
            retro_html.save_accepted(stale)

        findings = [entry["finding"] for entry in board.load_accepted()]
        self.assertEqual(findings, ["original", "concurrent decision"])

    def test_cross_process_updates_preserve_two_distinct_decisions(self) -> None:
        context = multiprocessing.get_context("spawn")
        first_inside = context.Event()
        second_inside = context.Event()
        release_first = context.Event()
        first = context.Process(
            target=_process_append,
            args=(str(self.deferred), str(self.locks), "first",
                  first_inside, release_first),
        )
        second = context.Process(
            target=_process_append,
            args=(str(self.deferred), str(self.locks), "second",
                  second_inside, None),
        )
        first.start()
        self.assertTrue(first_inside.wait(5), "first process never acquired ledger lock")
        second.start()
        self.assertFalse(
            second_inside.wait(0.5),
            "second process entered its mutation while the first held the lock",
        )
        release_first.set()
        first.join(10)
        second.join(10)
        if first.is_alive():
            first.terminate()
        if second.is_alive():
            second.terminate()

        self.assertEqual(first.exitcode, 0)
        self.assertEqual(second.exitcode, 0)
        entries = retro_ledger.load_entries(
            self.deferred, label="fixture decisions"
        )
        self.assertEqual(
            {entry["finding"] for entry in entries}, {"first", "second"}
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
