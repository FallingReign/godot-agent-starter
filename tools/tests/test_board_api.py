#!/usr/bin/env python3
"""Tests for the board's HTTP contract.

    python tools/tests/test_board_api.py

Two layers, deliberately:

* the API functions, called directly, for the properties that matter --
  approve-with-comment persists *before* it dispatches, a stale artifact is
  refused rather than silently rebuilt, and a second approval queues behind
  the first instead of running concurrently;
* a real `ThreadingHTTPServer` on a loopback port for the wire shape, because
  a handler that returns the right tuple and an HTTP response that returns an
  HTML traceback are not the same thing, and the page on the other end only
  ever sees the second.

Every filesystem path the board touches is redirected into a temp directory
and `RunManager.spawn` is replaced with a fake, so no test here can spend
quota or write to docs/retro/.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import board          # noqa: E402
import retro_queue    # noqa: E402
import retro_rank     # noqa: E402
import session_digest  # noqa: E402
import session_evidence  # noqa: E402

FINDINGS_TEMPLATE = """# Retrospective 2026-01-01

session_snapshot: {snapshot}
Header prose.

## Finding: the gate ran twice for one edit

sessions: [S1]
human_turns: [S1:H1]
mechanical: []
recurs: true
severity: none
fix_files: [tools/thing.py]
fix_lines: 12

**Problem** - The gate ran twice.

**Proposal** - Reuse one ranking pass.

**Measure** - One pass writes both dispatch artifacts.

## Finding: a second, differently named problem

sessions: [S1]
human_turns: [S1:H1]
mechanical: []
recurs: false
severity: false-green
fix_files: [check.py]
fix_lines: 3

**Problem** - Exit zero hid a false success.

**Proposal** - Require structured worker evidence.

**Measure** - An exit-zero run without evidence is unverified.
"""

SLUG_A = "the-gate-ran-twice-for-one-edit"
SLUG_B = "a-second-differently-named-problem"

FAKE_SESSIONS = [{"log": Path("nonexistent-1.jsonl"), "id": "s1"}]


def _scratch_parent() -> Path:
    configured = os.environ.get("KIT_TEST_TMPDIR", "").strip()
    return Path(configured) if configured else TOOLS.parent / ".checklogs"


def _remove_readonly(function, path: str, _error) -> None:
    """Allow Windows to remove read-only Git object files in scratch repos."""
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _fake_digest(sess: dict, index: int) -> dict:
    return {"index": index, "started": "2026-01-01T00:00:00Z",
            "human": [f"human message {i}" for i in range(1, 10)]}


def _write_snapshot(root: Path) -> str:
    manifest = session_evidence.build_manifest(root, [{
        "id": "session-one",
        "name": "fixture",
        "started": "2026-01-01T00:00:00Z",
        "updated": "2026-01-01T01:00:00Z",
        "human": [f"human message {i}" for i in range(1, 10)],
        "sources": {"events": {"sha256": "a" * 64, "bytes": 100}},
        "persona": "game-builder",
    }])
    path = session_evidence.write_manifest(
        root / ".kit" / "runtime" / "evidence" / "sessions", manifest)
    return path.relative_to(root).as_posix()


class FakeRunManager:
    """Records what would have been spawned, and models the sequential queue.

    The queueing rule is the real one from `RunManager`: an item starts only
    when nothing is in flight. Here `finish()` stands in for the watcher
    thread noticing a process exit.
    """

    def __init__(self) -> None:
        self.spawned: list[dict] = []
        self.queue: list[dict] = []
        self.active: str | None = None
        self._halted = False
        self.retro_started = 0
        self.retro_fails = ""

    def enqueue(self, item: dict, retry_of: str = "") -> dict:
        self.queue.append(item)
        if self.active is None and not self._halted:
            return self._start(item)
        return {"run_id": item.get("_run_id"), "status": "queued",
                "finding": item["finding"],
                "slug": item["slug"], "queue_position": len(self.queue),
                "queue_total": len(self.queue)}

    def _start(self, item: dict) -> dict:
        run_id = item.get("_run_id") or f"run-{len(self.spawned) + 1}"
        item["_run_id"] = run_id
        item["_started"] = True
        self.spawned.append({"run_id": run_id, **item})
        self.active = run_id
        state = board.load_state()
        state.setdefault("runs", []).append({
            "run_id": run_id, "kind": "finding", "finding": item["finding"],
            "slug": item["slug"], "status": "running", "pid": os.getpid(),
            "log": None, "t0": time.time(), "exit_code": None,
        })
        board.save_state(state)
        board.annotate_accepted(item["slug"], run_id=run_id, state="working")
        return {"run_id": run_id, "status": "running", "finding": item["finding"],
                "slug": item["slug"]}

    def finish(self, ok: bool = True) -> None:
        finished = self.active
        state = board.load_state()
        for run in state.get("runs", []):
            if run.get("run_id") == finished:
                run["status"] = "completed" if ok else "failed"
                run["exit_code"] = 0 if ok else 1
                run["duration_s"] = 0.0
        board.save_state(state)
        self.active = None
        if not ok:
            self._halted = True
            return
        nxt = next((i for i in self.queue if not i.get("_started")), None)
        if nxt is not None:
            self._start(nxt)

    def queue_view(self) -> list[dict]:
        return [{"finding": i["finding"], "slug": i["slug"], "run_id": i.get("_run_id"),
                 "status": "running" if i.get("_run_id") == self.active else "queued",
                 "position": n + 1, "total": len(self.queue)}
                for n, i in enumerate(self.queue)]

    def halted(self) -> bool:
        return self._halted

    def owns(self, run_id: str | None) -> bool:
        return bool(run_id and run_id == self.active)

    def spawn_retro(self, _provider_spec) -> dict:
        if self.retro_fails:
            return {"error": self.retro_fails}
        self.retro_started += 1
        return {"run_id": f"retro-{self.retro_started}", "kind": "retro",
                "status": "running"}


class BoardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = _scratch_parent() / f"board-api-test-{uuid.uuid4().hex}"
        self.dir.mkdir(parents=True)
        self.retro = self.dir / "docs" / "retro"
        self.retro.mkdir(parents=True)
        self.runtime = self.dir / ".kit" / "runtime"
        self.session_evidence = self.runtime / "evidence" / "sessions"
        self.snapshot = _write_snapshot(self.dir)
        self.findings_text = FINDINGS_TEMPLATE.format(snapshot=self.snapshot)
        self.findings = self.retro / "2026-01-01-findings.md"
        self.findings.write_text(self.findings_text, encoding="utf-8")
        self.queue_dir = self.runtime / "retro" / "queue"
        shutil.copyfile(TOOLS.parent / "kit.config.json", self.dir / "kit.config.json")

        self._saved = {
            "discover": session_digest.discover,
            "digest_one": session_digest.digest_one,
            "persona": retro_rank.session_persona,
            "queue_root": retro_queue.ROOT,
            "queue_dir": retro_queue.QUEUE_DIR,
            "queue_evidence": retro_queue.SESSION_EVIDENCE_DIR,
            "rank_root": retro_rank.ROOT,
            "rank_retro": retro_rank.RETRO_DIR,
            "rank_deferred": retro_rank.DEFERRED_FILE,
            "rank_evidence": retro_rank.SESSION_EVIDENCE_DIR,
            "rank_board_state": retro_rank.BOARD_STATE_FILE,
            "board_retro": board.RETRO_DIR,
            "board_runs": board.RUNS_DIR,
            "board_state": board.STATE_FILE,
            "board_lock": board.LOCK_FILE,
            "board_log": board.BOARD_LOG,
            "board_accepted": board.ACCEPTED_FILE,
            "board_root": board.ROOT,
            "board_runtime": board._RUNTIME,
            "board_controller_runtime": board._CONTROLLER_RUNTIME,
            "regen": board._regenerate,
            "rm": board._run_manager,
            "provider_selection": board.providers.selection,
            "provider_preflight": board.providers.preflight,
            "dispatch_blockers": board.run_result.dispatch_blockers,
        }
        session_digest.discover = lambda repo, roots=None: list(FAKE_SESSIONS)
        session_digest.digest_one = _fake_digest
        retro_rank.session_persona = lambda log: "game-builder"
        retro_queue.ROOT = self.dir
        retro_queue.QUEUE_DIR = self.queue_dir
        retro_queue.SESSION_EVIDENCE_DIR = self.session_evidence
        retro_rank.ROOT = self.dir
        retro_rank.RETRO_DIR = self.retro
        retro_rank.DEFERRED_FILE = self.retro / "deferred.json"
        retro_rank.SESSION_EVIDENCE_DIR = self.session_evidence
        retro_rank.BOARD_STATE_FILE = self.runtime / "board" / "state.json"
        board.RETRO_DIR = self.retro
        board.RUNS_DIR = self.runtime / "board" / "runs"
        board.STATE_FILE = self.runtime / "board" / "state.json"
        board.LOCK_FILE = self.runtime / "board" / "board.lock"
        board.BOARD_LOG = self.runtime / "board" / "board.log"
        board.ACCEPTED_FILE = self.retro / "accepted.json"
        board.ROOT = self.dir
        board._RUNTIME = board.runtime_paths.RuntimePaths(self.dir, self.runtime)
        board._CONTROLLER_RUNTIME = board._RUNTIME
        (self.dir / "plan.html").write_text(
            "<!doctype html><html><head></head><body><script>ok</script></body></html>",
            encoding="utf-8",
        )
        board._regenerate = lambda script: None   # never shell out in a test
        worker = self.worker_provider = board.providers.ProviderSpec(
            "worker", "copilot-cli", persona="kit-builder"
        )
        analyzer = board.providers.ProviderSpec("analyzer", "copilot-sdk")
        board.providers.selection = lambda _root, role: (
            worker if role == "worker" else analyzer
        )
        board.providers.preflight = lambda _spec: []
        board.run_result.dispatch_blockers = lambda *_args, **_kwargs: []
        self.rm = FakeRunManager()
        board._run_manager = self.rm

        retro_queue.build(findings_dir=self.retro, queue_dir=self.queue_dir)

    def tearDown(self) -> None:
        session_digest.discover = self._saved["discover"]
        session_digest.digest_one = self._saved["digest_one"]
        retro_rank.session_persona = self._saved["persona"]
        retro_queue.ROOT = self._saved["queue_root"]
        retro_queue.QUEUE_DIR = self._saved["queue_dir"]
        retro_queue.SESSION_EVIDENCE_DIR = self._saved["queue_evidence"]
        retro_rank.ROOT = self._saved["rank_root"]
        retro_rank.RETRO_DIR = self._saved["rank_retro"]
        retro_rank.DEFERRED_FILE = self._saved["rank_deferred"]
        retro_rank.SESSION_EVIDENCE_DIR = self._saved["rank_evidence"]
        retro_rank.BOARD_STATE_FILE = self._saved["rank_board_state"]
        board.RETRO_DIR = self._saved["board_retro"]
        board.RUNS_DIR = self._saved["board_runs"]
        board.STATE_FILE = self._saved["board_state"]
        board.LOCK_FILE = self._saved["board_lock"]
        board.BOARD_LOG = self._saved["board_log"]
        board.ACCEPTED_FILE = self._saved["board_accepted"]
        board.ROOT = self._saved["board_root"]
        board._RUNTIME = self._saved["board_runtime"]
        board._CONTROLLER_RUNTIME = self._saved["board_controller_runtime"]
        board._regenerate = self._saved["regen"]
        board._run_manager = self._saved["rm"]
        board.providers.selection = self._saved["provider_selection"]
        board.providers.preflight = self._saved["provider_preflight"]
        board.run_result.dispatch_blockers = self._saved["dispatch_blockers"]
        shutil.rmtree(self.dir, onerror=_remove_readonly)

    def accepted(self) -> list[dict]:
        return json.loads(board.ACCEPTED_FILE.read_text(encoding="utf-8"))

    def review(self, slug: str) -> dict:
        item = retro_queue.load_item(slug, self.queue_dir)
        return retro_queue.review_identity(item) if item is not None else {}

    def approve(self, slug: str, comment: str, by: str = "", request_id: str = "",
                retry_of: str = "") -> tuple[int, dict]:
        return board.api_approve(
            slug, comment, by=by, request_id=request_id, retry_of=retry_of,
            review=self.review(slug),
        )

    def probe_identity(self, port: int, pid: int | None = None,
                       instance_id: str = "expected-instance") -> dict:
        return {
            "port": port,
            "pid": os.getpid() if pid is None else pid,
            "instance_id": instance_id,
            "repository_scope_id": board._repository_scope_id(),
            "schema": board.SCHEMA,
            "version": board.BOARD_VERSION,
        }


class TestApprove(BoardTestCase):
    def test_comment_persists_and_is_dispatched(self) -> None:
        code, payload = self.approve(SLUG_A, "Keep the CLI, drop the flag.", by="jf")
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])

        # durable: the amended proposal survives a board restart
        entry = self.accepted()[0]
        self.assertEqual(entry["slug"], SLUG_A)
        self.assertEqual(entry["comment"], "Keep the CLI, drop the flag.")

        # dispatched: the prompt is render_prompt(item, comment), byte for byte
        self.assertEqual(len(self.rm.spawned), 1)
        item = retro_queue.load_item(SLUG_A, self.queue_dir)
        self.assertEqual(self.rm.spawned[0]["prompt"],
                         retro_queue.render_prompt(item, "Keep the CLI, drop the flag."))
        self.assertIn(retro_queue.COMMENT_HEADING, self.rm.spawned[0]["prompt"])
        self.assertTrue(board.STATE_FILE.is_relative_to(self.runtime))
        self.assertTrue(retro_queue.QUEUE_DIR.is_relative_to(self.runtime))
        self.assertFalse((self.retro / "queue").exists())
        self.assertFalse((self.retro / "board.state.json").exists())
        self.assertFalse((self.retro / "runs").exists())

    def test_comment_survives_a_reload_of_the_decision_file(self) -> None:
        self.approve(SLUG_A, "the comment is the amendment")
        reloaded = board.load_accepted()
        self.assertEqual(reloaded[0]["comment"], "the comment is the amendment")

    def test_repeated_automatic_approval_returns_the_complete_dispatch_contract(self) -> None:
        _code, first = self.approve(SLUG_A, "same amendment")
        code, repeated = self.approve(SLUG_A, "same amendment")

        self.assertEqual(200, code)
        self.assertEqual(first["run"]["run_id"], repeated["run"]["run_id"])
        self.assertTrue(repeated["run"]["idempotent"])
        self.assertTrue(repeated["dispatch"]["started"])
        self.assertTrue(repeated["dispatch"]["idempotent"])
        self.assertEqual(1, len(self.rm.spawned))

    def test_approval_requires_the_exact_artifact_review_identity(self) -> None:
        original_review = self.review(SLUG_A)
        path = self.queue_dir / f"{SLUG_A}.json"
        item = json.loads(path.read_text(encoding="utf-8"))
        item["generated_at"] = "2099-01-01T00:00:00Z"
        item["artifact_sha256"] = retro_queue.artifact_sha256(item)
        path.write_text(json.dumps(item), encoding="utf-8")

        code, payload = board.api_approve(
            SLUG_A, "go", review=original_review
        )

        self.assertEqual(409, code)
        self.assertEqual("stale_review", payload["code"])
        self.assertEqual([], self.rm.spawned)
        self.assertFalse(board.ACCEPTED_FILE.exists())

    def test_orphan_reservation_recovers_same_run_and_prompt(self) -> None:
        def crash_after_decision(*_args, **_kwargs):
            raise RuntimeError("simulated process loss before enqueue")

        self.rm.enqueue = crash_after_decision
        with self.assertRaisesRegex(RuntimeError, "simulated process loss"):
            self.approve(SLUG_A, "exact amendment")
        accepted = self.accepted()[0]
        self.assertEqual("reserved", accepted["state"])
        reserved_run_id = accepted["run_id"]

        replacement = FakeRunManager()
        board._run_manager = replacement
        recovered = board.recover_orphan_reservations()

        self.assertEqual(1, len(recovered))
        self.assertNotIn("error", recovered[0])
        self.assertEqual(reserved_run_id, replacement.spawned[0]["run_id"])
        item = retro_queue.load_item(SLUG_A, self.queue_dir)
        expected_prompt = retro_queue.render_prompt(item, "exact amendment")
        self.assertEqual(expected_prompt, replacement.spawned[0]["prompt"])
        self.assertEqual(
            accepted["dispatch_prompt_sha256"],
            hashlib.sha256(expected_prompt.encode("utf-8")).hexdigest(),
        )

    def test_orphan_recovery_fails_closed_if_artifact_changed(self) -> None:
        self.rm.enqueue = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated process loss")
        )
        with self.assertRaises(RuntimeError):
            self.approve(SLUG_A, "exact amendment")
        path = self.queue_dir / f"{SLUG_A}.json"
        item = json.loads(path.read_text(encoding="utf-8"))
        item["generated_at"] = "2099-01-02T00:00:00Z"
        item["artifact_sha256"] = retro_queue.artifact_sha256(item)
        path.write_text(json.dumps(item), encoding="utf-8")
        replacement = FakeRunManager()
        board._run_manager = replacement

        recovered = board.recover_orphan_reservations()

        self.assertEqual(1, len(recovered))
        self.assertIn("error", recovered[0])
        self.assertEqual([], replacement.spawned)
        self.assertEqual("unverified", self.accepted()[0]["state"])

    def test_approving_a_stale_artifact_is_refused(self) -> None:
        self.findings.write_text(
            self.findings_text + "\nan edit landed under it.\n", encoding="utf-8")
        code, payload = self.approve(SLUG_A, "go")
        self.assertEqual(code, 409)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "stale")
        self.assertIn("stale", payload["error"])
        self.assertIn("kit retro publish", payload["error"])
        # refused means refused: nothing dispatched, no decision recorded
        self.assertEqual(self.rm.spawned, [])
        self.assertFalse(board.ACCEPTED_FILE.exists())

    def test_unknown_slug_is_a_structured_404(self) -> None:
        code, payload = self.approve("no-such-finding", "go")
        self.assertEqual(code, 404)
        self.assertEqual(payload["code"], "missing_artifact")
        self.assertEqual(self.rm.spawned, [])

    def test_missing_slug_is_a_structured_400(self) -> None:
        code, payload = self.approve("", "go")
        self.assertEqual(code, 400)
        self.assertEqual(payload["code"], "bad_request")

    def test_unavailable_provider_preserves_the_decision_without_dispatch(self) -> None:
        board.providers.preflight = lambda _spec: ["adapter unavailable"]

        code, payload = self.approve(SLUG_A, "go")

        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["dispatch"]["started"])
        self.assertIn("adapter unavailable", payload["dispatch"]["blockers"])
        self.assertEqual(self.rm.spawned, [])
        self.assertEqual("approved", self.accepted()[0]["state"])

    def test_unstable_repository_preserves_the_decision_without_dispatch(self) -> None:
        board.run_result.dispatch_blockers = lambda *_args, **_kwargs: [
            "repository has uncommitted changes: tools/thing.py"
        ]

        code, payload = self.approve(SLUG_A, "go")

        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["dispatch"]["started"])
        self.assertIn("repository has uncommitted changes: tools/thing.py",
                      payload["dispatch"]["blockers"])
        self.assertEqual(self.rm.spawned, [])
        self.assertEqual("approved", self.accepted()[0]["state"])

    def test_declared_fix_scope_is_checked_before_dispatch(self) -> None:
        requested: list[str] = []

        def blockers(_root, **kwargs):
            requested.extend(kwargs.get("requested_files") or [])
            return ["automatic dispatch requires an interactive maintainer"]

        board.run_result.dispatch_blockers = blockers

        code, payload = self.approve(SLUG_A, "go")

        self.assertEqual(code, 200)
        self.assertEqual(requested, ["tools/thing.py"])
        self.assertFalse(payload["dispatch"]["started"])
        self.assertEqual(self.rm.spawned, [])
        self.assertEqual("approved", self.accepted()[0]["state"])

    def test_manual_provider_records_approval_without_requesting_a_worker(self) -> None:
        manual = board.providers.ProviderSpec(
            "worker", "manual", persona="kit-builder"
        )
        board.providers.selection = lambda _root, _role: manual
        board.providers.preflight = lambda _spec: ["worker provider is manual"]

        code, payload = self.approve(SLUG_A, "approved for later")

        self.assertEqual(200, code)
        self.assertIsNone(payload["run"])
        self.assertFalse(payload["dispatch"]["started"])
        self.assertEqual([], self.rm.spawned)
        self.assertEqual("approved", self.accepted()[0]["state"])

        repeated_code, repeated = self.approve(SLUG_A, "approved for later")
        self.assertEqual(200, repeated_code)
        self.assertIsNone(repeated["run"])
        self.assertFalse(repeated["dispatch"]["started"])
        self.assertTrue(repeated["dispatch"]["idempotent"])

    def test_a_spawn_failure_is_reported_not_swallowed(self) -> None:
        self.rm.enqueue = lambda item, retry_of="": {
            "error": "could not spawn copilot: not on PATH"
        }
        code, payload = self.approve(SLUG_A, "go")
        self.assertEqual(code, 500)
        self.assertEqual(payload["code"], "spawn_failed")
        self.assertIn("copilot", payload["error"])
        # the decision is still on disk: the human approved it, the machine failed
        self.assertEqual(self.accepted()[0]["comment"], "go")

    def test_legacy_artifact_is_refused_before_decision_or_run(self) -> None:
        path = self.queue_dir / f"{SLUG_A}.json"
        item = json.loads(path.read_text(encoding="utf-8"))
        item["schema"] = 2
        item.pop("dispatchable", None)
        item.pop("dispatch_blockers", None)
        item.pop("evidence_snapshot", None)
        path.write_text(json.dumps(item), encoding="utf-8")

        code, payload = self.approve(SLUG_A, "go")

        self.assertEqual(code, 409)
        self.assertEqual(payload["code"], "not_dispatchable")
        self.assertFalse(payload["dispatchable"])
        self.assertIn("legacy schema", " ".join(payload["dispatch_blockers"]))
        self.assertEqual(self.rm.spawned, [])
        self.assertFalse(board.ACCEPTED_FILE.exists())
        self.assertEqual(board.load_state()["runs"], [])

    def test_rebuilt_legacy_report_is_refused_before_decision_or_run(self) -> None:
        legacy = "\n".join(
            line for line in self.findings_text.splitlines()
            if not line.startswith("session_snapshot:")) + "\n"
        self.findings.write_text(legacy, encoding="utf-8")
        items = retro_queue.build(findings_dir=self.retro, queue_dir=self.queue_dir)
        self.assertFalse(items[0]["dispatchable"])

        code, payload = self.approve(SLUG_A, "go")

        self.assertEqual(code, 409)
        self.assertEqual(payload["code"], "not_dispatchable")
        self.assertIn("legacy report", " ".join(payload["dispatch_blockers"]))
        self.assertEqual(self.rm.spawned, [])
        self.assertFalse(board.ACCEPTED_FILE.exists())
        self.assertEqual(board.load_state()["runs"], [])

    def test_mutated_artifact_prompt_is_refused_before_decision_or_run(self) -> None:
        path = self.queue_dir / f"{SLUG_A}.json"
        item = json.loads(path.read_text(encoding="utf-8"))
        item["prompt"] += "\nUnreviewed extra instruction.\n"
        path.write_text(json.dumps(item), encoding="utf-8")

        code, payload = self.approve(SLUG_A, "go")

        self.assertEqual(code, 409)
        self.assertEqual(payload["code"], "not_dispatchable")
        self.assertIn("prompt digest", " ".join(payload["dispatch_blockers"]))
        self.assertEqual(self.rm.spawned, [])
        self.assertFalse(board.ACCEPTED_FILE.exists())

    def test_tampered_snapshot_refuses_retry_without_rewriting_decision(self) -> None:
        code, first = self.approve(SLUG_A, "first")
        self.assertEqual(code, 200)
        run_id = first["run"]["run_id"]
        state = board.load_state()
        state["runs"][0]["status"] = "failed"
        state["runs"][0]["duration_s"] = 1.0
        board.save_state(state)
        before = board.ACCEPTED_FILE.read_bytes()
        snapshot = self.dir / self.snapshot
        snapshot.write_bytes(snapshot.read_bytes().replace(
            b"human message 1", b"tampered message"))

        code, payload = self.approve(
            SLUG_A, "retry amendment", retry_of=run_id)

        self.assertEqual(code, 409)
        self.assertEqual(payload["code"], "not_dispatchable")
        self.assertIn("content hash", " ".join(payload["dispatch_blockers"]))
        self.assertEqual(board.ACCEPTED_FILE.read_bytes(), before)
        self.assertEqual(len(self.rm.spawned), 1)
        self.assertEqual(len(board.load_state()["runs"]), 1)


class TestSequentialQueue(BoardTestCase):
    def test_second_approval_queues_behind_the_first(self) -> None:
        self.approve(SLUG_A, "first")
        self.approve(SLUG_B, "second")
        self.assertEqual(len(self.rm.spawned), 1, "two workers ran concurrently")
        self.assertEqual(self.rm.spawned[0]["slug"], SLUG_A)

        states = {f["slug"]: f for f in board.finding_states()}
        self.assertEqual(states[SLUG_A]["state"], "working")
        self.assertEqual(states[SLUG_B]["state"], "queued")
        self.assertIn("queued", states[SLUG_B]["status_detail"])

    def test_the_queued_item_starts_when_the_first_finishes(self) -> None:
        self.approve(SLUG_A, "first")
        self.approve(SLUG_B, "second")
        self.rm.finish(ok=True)
        self.assertEqual([s["slug"] for s in self.rm.spawned], [SLUG_A, SLUG_B])

    def test_a_failed_run_halts_the_queue_visibly(self) -> None:
        self.approve(SLUG_A, "first")
        self.approve(SLUG_B, "second")
        self.rm.finish(ok=False)
        self.assertEqual(len(self.rm.spawned), 1)
        states = {f["slug"]: f for f in board.finding_states()}
        self.assertIn("halted", states[SLUG_B]["status_detail"])


class TestRealRunManagerQueue(BoardTestCase):
    """The queueing rule in the shipped class, not in the test double.

    `spawn` is stubbed to the point of launching a process and no further, so
    the ordering, the single-in-flight invariant and the halt-on-failure rule
    are the real ones.
    """

    def setUp(self) -> None:
        super().setUp()
        started = self.started = []

        class Recording(board.RunManager):
            def spawn(self, prompt, finding=None, queue_position=None,
                      queue_total=None, slug=None, run_id=None, decision_id=None,
                      requested_files=None,
                      provider_spec=None):
                started.append({"run_id": run_id, "finding": finding, "slug": slug,
                                "prompt": prompt, "queue_position": queue_position})
                return {"run_id": run_id, "status": "running", "finding": finding}

        self.mgr = Recording()
        board._run_manager = self.mgr

    def item(self, slug: str) -> dict:
        art = retro_queue.load_item(slug, self.queue_dir)
        return {"finding": art["title"], "slug": slug,
                "prompt": retro_queue.render_prompt(art, "c"),
                "_provider_spec": self.worker_provider}

    def wait_started(self, count: int) -> None:
        for _ in range(100):
            if len(self.started) >= count:
                return
            threading.Event().wait(0.01)
        self.fail(f"only {len(self.started)} worker(s) started; expected {count}")

    def test_only_one_runs_at_a_time(self) -> None:
        first = self.mgr.enqueue(self.item(SLUG_A))
        second = self.mgr.enqueue(self.item(SLUG_B))
        self.wait_started(1)
        self.assertTrue(first["run_id"])
        self.assertTrue(second["run_id"])
        self.assertEqual(second["status"], "queued")
        self.assertEqual(len(self.started), 1)

    def test_the_next_item_starts_after_the_previous_exits(self) -> None:
        self.mgr.enqueue(self.item(SLUG_A))
        self.mgr.enqueue(self.item(SLUG_B))
        self.mgr._active = None            # what _watch does on a clean exit
        self.mgr._advance_queue()
        self.wait_started(2)
        self.assertEqual([s["slug"] for s in self.started], [SLUG_A, SLUG_B])
        self.assertEqual(self.started[1]["queue_position"], 2)

    def test_a_halted_queue_does_not_start_the_next_item(self) -> None:
        self.mgr.enqueue(self.item(SLUG_A))
        self.mgr.enqueue(self.item(SLUG_B))
        self.wait_started(1)
        self.mgr._active = None
        self.mgr._halted = True            # what _watch does on a failure
        self.mgr._advance_queue()
        self.assertEqual(len(self.started), 1)
        self.assertTrue(self.mgr.halted())

    def test_a_third_approval_lands_behind_both(self) -> None:
        self.mgr.enqueue(self.item(SLUG_A))
        self.mgr.enqueue(self.item(SLUG_B))
        third = self.mgr.enqueue({
            "finding": "x", "slug": "x", "prompt": "p",
            "_provider_spec": self.worker_provider,
        })
        self.wait_started(1)
        self.assertEqual(third["queue_position"], 3)
        self.assertEqual(len(self.started), 1)


class TestDurableDispatch(BoardTestCase):
    def test_concurrent_identical_approvals_reserve_and_spawn_once(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls: list[str] = []
        calls_lock = threading.Lock()

        class Blocking(board.RunManager):
            def spawn(self, prompt, finding=None, queue_position=None,
                      queue_total=None, slug=None, run_id=None, decision_id=None,
                      requested_files=None,
                      provider_spec=None):
                with calls_lock:
                    calls.append(run_id)
                entered.set()
                release.wait(5)
                return {"run_id": run_id, "status": "running",
                        "finding": finding, "slug": slug}

        board._run_manager = Blocking()
        barrier = threading.Barrier(3)
        results: list[tuple[int, dict]] = []

        def approve() -> None:
            barrier.wait()
            results.append(self.approve(
                SLUG_A, "one exact amendment", request_id="browser-request-1"
            ))

        workers = [threading.Thread(target=approve) for _ in range(2)]
        for worker in workers:
            worker.start()
        barrier.wait()
        self.assertTrue(entered.wait(3), "reserved worker never reached spawn")
        for worker in workers:
            worker.join(3)
        release.set()
        for worker in workers:
            worker.join(3)

        self.assertEqual([code for code, _ in results], [200, 200])
        run_ids = {payload["run"]["run_id"] for _, payload in results}
        self.assertEqual(len(run_ids), 1, results)
        self.assertEqual(len(calls), 1, "duplicate HTTP delivery spawned twice")
        state_runs = board.load_state()["runs"]
        self.assertEqual(len(state_runs), 1)
        self.assertEqual(state_runs[0]["decision_id"], self.accepted()[0]["decision_id"])
        self.assertEqual(len(list(board.RUNS_DIR.glob("*.prompt.md"))), 1)

    def test_different_second_decision_requires_an_explicit_terminal_retry(self) -> None:
        code, _ = self.approve(SLUG_A, "first amendment")
        self.assertEqual(code, 200)
        code, payload = self.approve(SLUG_A, "different amendment")
        self.assertEqual(code, 409)
        self.assertEqual(payload["code"], "decision_conflict")
        self.assertEqual(len(self.rm.spawned), 1)

    def test_queued_prompt_survives_restart_and_is_started_once(self) -> None:
        original = board.RunManager()
        original._halted = True
        item = {"finding": "durable", "slug": "durable", "prompt": "exact bytes\n",
                "decision_id": "decision-durable",
                "_provider_spec": self.worker_provider}
        queued = original.enqueue(item)
        self.assertEqual(queued["status"], "queued")

        started: list[dict] = []
        started_event = threading.Event()

        class Recovered(board.RunManager):
            def spawn(self, prompt, finding=None, queue_position=None,
                      queue_total=None, slug=None, run_id=None, decision_id=None,
                      requested_files=None,
                      provider_spec=None):
                started.append({"prompt": prompt, "run_id": run_id,
                                "decision_id": decision_id})
                started_event.set()
                return {"run_id": run_id, "status": "running"}

        recovered = Recovered()
        recovered.recover()
        self.assertTrue(started_event.wait(3))
        self.assertEqual(started, [{"prompt": "exact bytes\n",
                                    "run_id": queued["run_id"],
                                    "decision_id": "decision-durable"}])
        self.assertEqual(len(board.load_state()["runs"]), 1)

    def test_recovery_does_not_run_past_a_failed_attempt(self) -> None:
        manager = board.RunManager()
        manager._halted = True
        manager.enqueue({"finding": "later", "slug": "later", "prompt": "later",
                         "decision_id": "later-decision",
                         "_provider_spec": self.worker_provider})
        state = board.load_state()
        state["runs"].insert(0, {
            "run_id": "failed-first", "kind": "finding", "status": "failed",
            "slug": "first", "finding": "first", "duration_s": 1.0,
        })
        board.save_state(state)

        recovered = board.RunManager()
        recovered.recover()
        self.assertTrue(recovered.halted())
        self.assertEqual(recovered.queue_view()[0]["status"], "queued")

    def test_legacy_exit_zero_is_unverified_and_duration_never_grows(self) -> None:
        board.save_accepted([{
            "finding": "the gate ran twice for one edit", "slug": SLUG_A,
            "state": "done", "run_id": "legacy-run", "comment": "old",
        }])
        board.save_state({"runs": [{
            "run_id": "legacy-run", "kind": "finding", "slug": SLUG_A,
            "finding": "the gate ran twice for one edit", "status": "finished",
            "exit_code": 0, "t0": 1.0, "log": None,
        }]})
        migrated = board.load_state()["runs"][0]
        self.assertEqual(migrated["status"], "unverified")
        first = board.describe_run(migrated, 3)
        threading.Event().wait(0.02)
        second = board.describe_run(migrated, 3)
        self.assertIsNone(first["elapsed_s"])
        self.assertEqual(first["elapsed_s"], second["elapsed_s"])
        finding = next(f for f in board.finding_states() if f["slug"] == SLUG_A)
        self.assertEqual(finding["state"], "unverified")
        self.assertIn("legacy exit-zero", finding["status_detail"])


class TestState(BoardTestCase):
    def test_state_shape(self) -> None:
        code, payload = board.api_state()
        self.assertEqual(code, 200)
        for key in ("board", "retro_due", "providers", "findings", "runs"):
            self.assertIn(key, payload)
        self.assertIn("analyzer", payload["providers"])
        self.assertIn("worker", payload["providers"])
        self.assertEqual({f["slug"] for f in payload["findings"]}, {SLUG_A, SLUG_B})
        for f in payload["findings"]:
            self.assertEqual(f["state"], "awaiting_review")
            self.assertIn("stale", f)
            self.assertTrue(f["dispatchable"])
            self.assertEqual(f["dispatch_blockers"], [])
            self.assertEqual(f["evidence_snapshot"], self.snapshot)
            self.assertIn("status_detail", f)
            self.assertIn("resume_cmd", f)

    def test_retro_due_is_surfaced(self) -> None:
        _code, payload = board.api_state()
        self.assertIn("due", payload["retro_due"])
        self.assertIn("threshold", payload["retro_due"])

    def test_a_silent_worker_is_legible(self) -> None:
        self.approve(SLUG_A, "go")
        run_id = self.rm.spawned[0]["run_id"]
        board.RUNS_DIR.mkdir(parents=True, exist_ok=True)
        log = board.RUNS_DIR / f"{run_id}.log"
        log.write_text("started\n", encoding="utf-8")
        old = os.path.getmtime(log) - 3600
        os.utime(log, (old, old))
        state = board.load_state()
        state["runs"] = [{"run_id": run_id, "kind": "finding", "finding": "x",
                          "slug": SLUG_A, "status": "running", "pid": os.getpid(),
                          "log": log.relative_to(self.dir).as_posix(), "t0": 0,
                          "resume_cmd": "copilot --agent kit-builder --resume=abc",
                          "exit_code": None}]
        board.save_state(state)
        saved_root = board.ROOT
        board.ROOT = self.dir
        try:
            entry = {f["slug"]: f for f in board.finding_states()}[SLUG_A]
        finally:
            board.ROOT = saved_root
        # an hour of silence must *look* wrong rather than like normal progress
        self.assertEqual(entry["state"], "working")
        self.assertIn("no output for", entry["status_detail"])
        self.assertIn("60m", entry["status_detail"])
        self.assertEqual(
            entry["resume_cmd"], "copilot --agent kit-builder --resume=abc"
        )

    def test_stale_is_orthogonal_to_state(self) -> None:
        self.findings.write_text(self.findings_text + "\nedit\n", encoding="utf-8")
        _code, payload = board.api_state()
        for f in payload["findings"]:
            self.assertTrue(f["stale"])
            self.assertEqual(f["state"], "awaiting_review")


class TestFindingAndDefer(BoardTestCase):
    def test_finding_returns_the_prompt_that_would_be_sent(self) -> None:
        code, payload = board.api_finding(SLUG_A)
        self.assertEqual(code, 200)
        item = retro_queue.load_item(SLUG_A, self.queue_dir)
        self.assertEqual(payload["prompt_preview"], retro_queue.render_prompt(item, ""))
        self.assertFalse(payload["stale"])
        self.assertTrue(payload["dispatchable"])
        self.assertEqual(payload["dispatch_blockers"], [])
        self.assertEqual(payload["evidence_snapshot"], self.snapshot)
        self.assertEqual(payload["review"], retro_queue.review_identity(item))

    def test_browser_control_carries_the_rendered_review_identity(self) -> None:
        item = retro_queue.load_item(SLUG_A, self.queue_dir)
        review = retro_queue.review_identity(item)
        rendered = board.retro_html.render_decision(
            item["title"], item, accepted={}, deferred={}
        )
        for field in (
            "artifact_sha256", "prompt_sha256", "template_sha256", "review_sha256"
        ):
            attribute = field.replace("_", "-")
            self.assertIn(f'data-{attribute}="{review[field]}"', rendered)
        self.assertIn(
            f'data-artifact-schema="{retro_queue.SCHEMA}"', rendered
        )
        self.assertIn("review:review", board.retro_html.DISPATCH_JS)

    def test_finding_get_exposes_tampered_snapshot_as_ineligible(self) -> None:
        snapshot = self.dir / self.snapshot
        snapshot.write_bytes(snapshot.read_bytes().replace(
            b"human message 1", b"tampered message"))
        code, payload = board.api_finding(SLUG_A)
        self.assertEqual(code, 200)
        self.assertFalse(payload["dispatchable"])
        self.assertIn("content hash", " ".join(payload["dispatch_blockers"]))

    def test_finding_unknown_slug(self) -> None:
        code, payload = board.api_finding("nope")
        self.assertEqual(code, 404)
        self.assertEqual(payload["code"], "missing_artifact")

    def test_traversal_and_alternate_slug_spellings_are_rejected(self) -> None:
        for slug in ("../accepted", "..\\accepted", "Two-Words", "two--words"):
            with self.subTest(slug=slug):
                code, payload = board.api_finding(slug)
                self.assertEqual(400, code)
                self.assertEqual("invalid_slug", payload["code"])
                code, payload = board.api_defer(slug, "later")
                self.assertEqual(400, code)
                self.assertEqual("invalid_slug", payload["code"])

    def test_defer_records_and_never_dispatches(self) -> None:
        code, payload = board.api_defer(SLUG_A, "not worth it yet")
        self.assertEqual(code, 200)
        self.assertEqual(payload["finding"]["state"], "deferred")
        self.assertEqual(self.rm.spawned, [])
        deferred = json.loads(retro_rank.DEFERRED_FILE.read_text(encoding="utf-8"))
        self.assertEqual(deferred[0]["reason"], "not worth it yet")

    def test_defer_unknown_slug(self) -> None:
        code, payload = board.api_defer("nope", "x")
        self.assertEqual(code, 404)
        self.assertEqual(payload["code"], "missing_artifact")


class TestRetroRun(BoardTestCase):
    def test_read_boundary_blocker_never_launches_process(self) -> None:
        manager = board.RunManager()
        analyzer = board.providers.ProviderSpec("analyzer", "copilot-sdk")
        blocker = board.providers.COPILOT_READ_BOUNDARY_BLOCKER

        with mock.patch.object(
            board.providers, "preflight", return_value=[blocker]
        ), mock.patch.object(board.subprocess, "Popen") as launch:
            result = manager.spawn_retro(analyzer)

        self.assertEqual(
            f"analyzer provider is unavailable: {blocker}", result["error"]
        )
        launch.assert_not_called()
        self.assertEqual([], board.load_state().get("runs", []))

    def test_run_returns_a_run_id(self) -> None:
        code, payload = board.api_retro_run()
        self.assertEqual(code, 200)
        self.assertTrue(payload["run_id"])
        self.assertEqual(self.rm.retro_started, 1)

    def test_a_second_run_while_one_is_in_flight_is_refused(self) -> None:
        self.rm.retro_fails = "a retrospective run is already in flight (retro-1)"
        code, payload = board.api_retro_run()
        self.assertEqual(code, 409)
        self.assertEqual(payload["code"], "retro_unavailable")

    def test_a_start_failure_is_a_500_with_a_message(self) -> None:
        self.rm.retro_fails = "could not start the retrospective: no python"
        code, payload = board.api_retro_run()
        self.assertEqual(code, 500)
        self.assertIn("could not start", payload["error"])


class TestRunStatus(BoardTestCase):
    def test_unknown_run(self) -> None:
        code, payload = board.api_run("nope")
        self.assertEqual(code, 404)
        self.assertEqual(payload["code"], "missing_run")

    def test_known_run_carries_tail_and_resume(self) -> None:
        run_id = "run-r1"
        board.RUNS_DIR.mkdir(parents=True, exist_ok=True)
        log = board.RUNS_DIR / f"{run_id}.log"
        log.write_text("line one\nline two\n", encoding="utf-8")
        board.save_state({"port": 1, "pid": os.getpid(), "started": "now", "runs": [
            {"run_id": run_id, "kind": "finding", "finding": "x", "slug": SLUG_A,
             "status": "finished", "exit_code": 0, "pid": os.getpid(),
             "log": log.relative_to(self.dir).as_posix(), "t0": 0,
             "resume_cmd": "copilot --agent kit-builder --resume=abc"},
        ]})
        saved_root = board.ROOT
        board.ROOT = self.dir
        try:
            code, payload = board.api_run(run_id)
        finally:
            board.ROOT = saved_root
        self.assertEqual(code, 200)
        self.assertIn("line two", payload["tail"])
        self.assertEqual(
            payload["resume_cmd"], "copilot --agent kit-builder --resume=abc"
        )
        self.assertEqual(payload["exit_code"], 0)

    def test_run_log_is_bounded_and_public_tail_is_redacted(self) -> None:
        run_id = "run-redaction"
        board.RUNS_DIR.mkdir(parents=True, exist_ok=True)
        log = board.RUNS_DIR / f"{run_id}.log"
        log.write_text(
            "api_key=super-secret C:\\Users\\person\\private.txt\n"
            "Authorization: Bearer ultra-secret /home/person/private.txt\n",
            encoding="utf-8",
        )
        board.save_state({"runs": [{
            "run_id": run_id, "kind": "finding", "finding": "x", "slug": SLUG_A,
            "status": "running", "pid": os.getpid(),
            "log": log.relative_to(self.dir).as_posix(),
            "resume_cmd": "copilot --resume=abc; calc.exe",
            "provider": {"kind": "copilot-cli", "secret": "provider-private"},
            "unexpected_private_field": "state-private",
        }]})

        code, payload = board.api_run(run_id)

        self.assertEqual(200, code)
        self.assertNotIn("super-secret", payload["tail"])
        self.assertNotIn("ultra-secret", payload["tail"])
        self.assertNotIn("C:\\Users", payload["tail"])
        self.assertNotIn("/home/person", payload["tail"])
        self.assertIn("<redacted>", payload["tail"])
        self.assertIsNone(payload["resume_cmd"])
        self.assertNotIn("log", payload)
        self.assertNotIn("provider-private", json.dumps(payload))
        self.assertNotIn("state-private", json.dumps(payload))

    def test_resume_hint_requires_the_exact_provider_grammar(self) -> None:
        self.assertEqual(
            board._safe_resume_command(
                "copilot --agent kit-builder --resume=good-session_123"
            ),
            "copilot --agent kit-builder --resume=good-session_123",
        )
        self.assertIsNone(board._safe_resume_command("Remove-Item C:/important"))
        self.assertIsNone(board._safe_resume_command("copilot --resume=legacy"))

    def test_run_log_path_cannot_escape_private_run_root(self) -> None:
        run_id = "run-escape"
        outside = self.dir / f"{run_id}.log"
        outside.write_text("outside-private-text\n", encoding="utf-8")
        board.save_state({"runs": [{
            "run_id": run_id, "kind": "finding", "finding": "x", "slug": SLUG_A,
            "status": "running", "pid": os.getpid(),
            "log": outside.relative_to(self.dir).as_posix(),
        }]})

        code, payload = board.api_run(run_id)

        self.assertEqual(200, code)
        self.assertEqual("", payload["tail"])


class TestCodexPublicBoundary(BoardTestCase):
    def test_manual_provider_is_a_neutral_handoff_not_an_unavailable_adapter(self) -> None:
        spec = board.providers.ProviderSpec("analyzer", "manual")
        board.providers.selection = lambda _root, _role: spec

        view = board._provider_view("analyzer")

        self.assertFalse(view["automatic"])
        self.assertTrue(view["ready"])
        self.assertEqual(view["preflight"], "manual-handoff")
        self.assertEqual(view["blockers"], [])

    def test_codex_run_never_exposes_log_events_paths_or_result_text(self) -> None:
        log = self.runtime / "board" / "runs" / "codex-events.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text('{"hidden":"do not expose"}\n', encoding="utf-8")
        entry = {
            "run_id": "codex-1",
            "kind": "finding",
            "finding": "Safe progress",
            "slug": "safe-progress",
            "status": "blocked",
            "provider": {
                "role": "worker",
                "kind": "codex-cli",
                "model": "gpt-safe",
                "secret": "provider-private",
            },
            "log": log.relative_to(self.dir).as_posix(),
            "resume_cmd": "codex resume private-token",
            "result_summary": "private result detail",
            "result_errors": ["private error detail"],
            "prompt": "private prompt",
        }
        described = board.describe_run(entry, 3)
        self.assertEqual(described["last_line"], "")
        self.assertNotIn("private result", described["status_label"])
        board.save_state({"schema": board.SCHEMA, "runs": [entry]})

        code, payload = board.api_run("codex-1")
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        for forbidden in (
            "log", "tail", "resume_cmd", "prompt", "result_summary", "result_errors"
        ):
            self.assertNotIn(forbidden, payload)
        self.assertNotIn("secret", payload["provider"])
        rendered = json.dumps(board._sanitize_public_payload({"runs": [entry]}))
        for private in (
            "do not expose", "private-token", "private result", "private error",
            "private prompt", "provider-private",
        ):
            self.assertNotIn(private, rendered)

    def test_codex_analyzer_preflight_explains_repository_read_boundary(self) -> None:
        spec = board.providers.ProviderSpec("analyzer", "codex-cli")
        blocker = board.providers.CODEX_READ_BOUNDARY_BLOCKER
        board.providers.selection = lambda _root, _role: spec
        board.providers.preflight = lambda _spec: [blocker]

        view = board._provider_view("analyzer")

        self.assertFalse(view["ready"])
        self.assertEqual(view["preflight"], "blocked")
        self.assertEqual(view["blockers"], [blocker])


class TestLiveness(BoardTestCase):
    """Review finding 5: state JSON existing is not evidence a board is alive."""

    def setUp(self) -> None:
        super().setUp()
        self._real_pid_alive = board._pid_alive
        board._pid_alive = lambda pid: pid == os.getpid()

    def tearDown(self) -> None:
        board._pid_alive = self._real_pid_alive
        super().tearDown()

    def test_dead_pid_and_dead_port(self) -> None:
        board.save_state({"port": 9, "pid": 999999, "started": "now", "runs": []})
        saved = board._probe
        board._probe = lambda port, expected, timeout=1.0, **kwargs: False
        try:
            health = board.board_health()
        finally:
            board._probe = saved
        self.assertFalse(health["alive"])
        self.assertFalse(health["pid_alive"])
        self.assertFalse(health["port_alive"])
        self.assertIn("999999", health["detail"])

    def test_corrupt_lifecycle_identity_fails_closed_without_exception(self) -> None:
        board.save_state({
            "port": "not-a-port", "pid": "not-a-pid", "started": "now", "runs": [],
            "instance_id": [], "repository_scope_id": {},
            "schema": board.SCHEMA, "version": board.BOARD_VERSION,
        })
        health = board.board_health()
        self.assertFalse(health["alive"])
        self.assertFalse(health["pid_alive"])
        self.assertFalse(health["port_alive"])

    def test_live_pid_but_dead_port_is_not_alive(self) -> None:
        board.save_state({"port": 9, "pid": os.getpid(), "started": "now", "runs": []})
        saved = board._probe
        board._probe = lambda port, expected, timeout=1.0, **kwargs: False
        try:
            health = board.board_health()
        finally:
            board._probe = saved
        self.assertFalse(health["alive"])
        self.assertTrue(health["pid_alive"])
        self.assertIn("does not answer", health["detail"])

    def test_live_port_but_dead_pid_is_not_alive(self) -> None:
        board.save_state({"port": 9, "pid": 999999, "started": "now", "runs": []})
        saved = board._probe
        board._probe = lambda port, expected, timeout=1.0, **kwargs: True
        try:
            health = board.board_health()
        finally:
            board._probe = saved
        self.assertFalse(health["alive"])
        self.assertIn("is gone", health["detail"])

    def test_both_alive(self) -> None:
        board.save_state({"port": 9, "pid": os.getpid(), "started": "now", "runs": []})
        saved = board._probe
        board._probe = lambda port, expected, timeout=1.0, **kwargs: True
        try:
            health = board.board_health()
        finally:
            board._probe = saved
        self.assertTrue(health["alive"])
        self.assertEqual(health["detail"], "")

    def test_exact_prior_build_is_reported_as_stale_not_dead(self) -> None:
        board.save_state({
            "port": 9123,
            "pid": os.getpid(),
            "started": "now",
            "instance_id": "old-instance",
            "repository_scope_id": board._repository_scope_id(),
            "schema": board.SCHEMA,
            "version": "old-build",
            "runs": [],
        })

        def probe(_port, _expected, timeout=1.0, *, require_current=True):
            return not require_current

        with mock.patch.object(board, "_probe", side_effect=probe):
            health = board.board_health()

        self.assertFalse(health["alive"])
        self.assertTrue(health["identity_alive"])
        self.assertIn("build is stale", health["detail"])

    def test_start_stops_exact_stale_instance_before_replacement(self) -> None:
        state = {
            "port": 9123,
            "pid": os.getpid(),
            "instance_id": "old-instance",
            "repository_scope_id": board._repository_scope_id(),
            "schema": board.SCHEMA,
            "version": "old-build",
            "runs": [],
        }
        board.save_state(state)
        with mock.patch.object(board, "board_health", return_value={
            "alive": False,
            "identity_alive": True,
            "detail": "stale",
            "url": None,
        }), mock.patch.object(
            board, "_stop_stale_recorded_instance", return_value=False
        ) as stop, mock.patch.object(board.subprocess, "Popen") as launch:
            self.assertIsNone(board.ensure_running())

        stop.assert_called_once()
        stopped_state = stop.call_args.args[0]
        for key, value in state.items():
            self.assertEqual(value, stopped_state[key])
        launch.assert_not_called()

    def test_probe_rejects_a_port_answering_something_else(self) -> None:
        import http.server

        class Other(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: A003
                pass

            def do_GET(self):  # noqa: N802
                body = b'{"ok": true}'   # ok, but no schema: not a board
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            self.assertFalse(board._probe(httpd.server_address[1], self.probe_identity(httpd.server_address[1])))
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_probe_rejects_a_board_from_an_older_kit_revision(self) -> None:
        import http.server

        class Stale(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: A003
                pass

            def do_GET(self):  # noqa: N802
                body = json.dumps({
                    "ok": True, "schema": board.SCHEMA, "version": "stale-version"
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Stale)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            self.assertFalse(board._probe(httpd.server_address[1], self.probe_identity(httpd.server_address[1])))
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_probe_binds_pid_instance_repository_and_port(self) -> None:
        httpd = board.BoardHTTPServer(
            ("127.0.0.1", 0), board.Handler,
            instance_id="instance-one",
            repository_scope_id=board._repository_scope_id(),
        )
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        expected = self.probe_identity(port, instance_id="instance-one")
        try:
            self.assertTrue(board._probe(port, expected))
            for field, wrong in (
                ("pid", os.getpid() + 1),
                ("instance_id", "instance-two"),
                ("repository_scope_id", "0" * 64),
                ("port", port + 1),
                ("schema", board.SCHEMA + 1),
                ("version", "other-version"),
            ):
                with self.subTest(field=field):
                    altered = {**expected, field: wrong}
                    self.assertFalse(board._probe(port, altered))
        finally:
            httpd.shutdown()
            httpd.server_close()

    def test_probe_rejects_a_different_controller_runtime_binding(self) -> None:
        httpd = board.BoardHTTPServer(
            ("127.0.0.1", 0), board.Handler,
            instance_id="controller-binding",
            repository_scope_id=board._repository_scope_id(),
        )
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        port = httpd.server_address[1]
        expected = self.probe_identity(port, instance_id="controller-binding")
        other = self.runtime / "other-controller"
        other.mkdir()
        original = board._CONTROLLER_RUNTIME
        try:
            board._CONTROLLER_RUNTIME = board.runtime_paths.RuntimePaths(
                self.dir, other
            )
            self.assertFalse(board._probe(port, expected))
            self.assertTrue(
                board._probe(port, expected, require_current=False)
            )
        finally:
            board._CONTROLLER_RUNTIME = original
            httpd.shutdown()
            httpd.server_close()

    def test_restart_preserves_the_exact_controller_runtime_environment(self) -> None:
        process = mock.Mock(pid=54321)
        process.poll.return_value = None
        inherited = str(self.runtime.resolve())
        with mock.patch.object(board, "board_health", return_value={
            "alive": False, "detail": "no board", "url": None,
        }), mock.patch.object(board, "_free_port", return_value=48991), \
                mock.patch.object(board, "_probe", return_value=False), \
                mock.patch.object(board.time, "sleep", return_value=None), \
                mock.patch.object(
                    board.subprocess, "Popen", return_value=process
                ) as spawn, mock.patch.object(
                    board, "_terminate_process_tree", return_value=True
                ), mock.patch.dict(
                    board.os.environ,
                    {board.runtime_paths.CONTROLLER_RUNTIME_ENV: inherited},
                    clear=False,
                ):
            board.ensure_running()

        environment = spawn.call_args.kwargs["env"]
        self.assertEqual(
            inherited,
            environment[board.runtime_paths.CONTROLLER_RUNTIME_ENV],
        )
        arguments = spawn.call_args.args[0]
        runtime_index = arguments.index("--controller-runtime-id")
        self.assertEqual(
            board._controller_runtime_id(), arguments[runtime_index + 1]
        )

    def test_failed_start_is_not_persisted_or_reported_as_live(self) -> None:
        process = mock.Mock(pid=54321)
        process.poll.return_value = None
        before = board.load_state()
        with mock.patch.object(board, "board_health", return_value={
            "alive": False, "detail": "no board", "url": None,
        }), mock.patch.object(board, "_free_port", return_value=48991), \
                mock.patch.object(board, "_probe", return_value=False), \
                mock.patch.object(board.time, "sleep", return_value=None), \
                mock.patch.object(board.subprocess, "Popen", return_value=process), \
                mock.patch.object(
                    board, "_terminate_process_tree", return_value=True
                ) as terminate:
            url = board.ensure_running()

        self.assertIsNone(url)
        terminate.assert_called_once_with(process)
        self.assertEqual(before, board.load_state())

    def test_owned_provider_tree_is_terminated_at_its_bound_timeout(self) -> None:
        process = mock.Mock(pid=12345)
        process.wait.side_effect = [subprocess.TimeoutExpired("provider", 60), 143]
        process.poll.return_value = 143
        with mock.patch.object(
                board, "_terminate_process_tree", return_value=True) as terminate:
            exit_code, timed_out = board._wait_bounded(process, 60)

        self.assertTrue(timed_out)
        self.assertEqual(143, exit_code)
        terminate.assert_called_once_with(process)

    def test_windows_tree_kill_failure_is_checked_and_falls_back(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.side_effect = [None, None, 0]
        process.wait.return_value = 0
        failed = subprocess.CompletedProcess(["taskkill"], 1)
        windows_os = mock.Mock()
        windows_os.name = "nt"
        taskkill = r"C:\Windows\System32\taskkill.exe"
        with mock.patch.object(board, "os", windows_os), mock.patch.object(
            board.process_supervisor,
            "windows_system_executable",
            return_value=taskkill,
        ) as resolve, mock.patch.object(
            board.subprocess, "run", return_value=failed
        ) as run:
            terminated = board._terminate_process_tree(process)
        self.assertTrue(terminated)
        resolve.assert_called_once_with("taskkill.exe")
        self.assertEqual(taskkill, run.call_args.args[0][0])
        process.kill.assert_called_once_with()

    def test_posix_tree_kill_escalates_from_term_to_kill(self) -> None:
        process = mock.Mock(pid=12345)
        process.poll.side_effect = [None, -9]
        process.wait.side_effect = [subprocess.TimeoutExpired("provider", 5), -9]
        posix_os = mock.Mock()
        posix_os.name = "posix"
        posix_os.killpg.side_effect = [None, None, None, ProcessLookupError()]
        with mock.patch.object(board, "os", posix_os), mock.patch.object(
                board.signal, "SIGKILL", 9, create=True):
            terminated = board._terminate_process_tree(process)
        self.assertTrue(terminated)
        self.assertEqual(
            [
                mock.call(12345, signal.SIGTERM), mock.call(12345, 0),
                mock.call(12345, 9), mock.call(12345, 0),
            ],
            posix_os.killpg.call_args_list,
        )

    def test_worker_environment_cannot_inherit_broad_or_parent_git_access(self) -> None:
        workspace = self.runtime / "dispatch" / "workspaces" / "run-safe"
        workspace.mkdir(parents=True)
        inherited = {
            "COPILOT_ALLOW_ALL": "true",
            "GIT_DIR": "C:/attacker/repository",
            "GIT_WORK_TREE": "C:/attacker/worktree",
            "GIT_CONFIG_PARAMETERS": "'core.fsmonitor=attacker-command'",
        }
        with mock.patch.dict(board.os.environ, inherited, clear=False):
            environment = board._worker_environment(workspace)

        for variable in inherited:
            self.assertNotIn(variable, environment)
        self.assertEqual("false", environment["COPILOT_AUTO_UPDATE"])
        self.assertEqual(str(workspace.parent), environment["GIT_CEILING_DIRECTORIES"])
        self.assertEqual("1", environment["KIT_HOST_FINALIZES"])


class TestHTTPRejectionDrain(unittest.TestCase):
    def test_bounded_rejected_body_is_consumed_before_socket_close(self) -> None:
        handler = mock.Mock()
        handler.headers.get.return_value = None
        handler.headers.get_all.return_value = ["2"]
        handler.rfile.read.return_value = b"{}"
        handler.close_connection = False

        board._discard_rejected_request_body(handler)

        handler.rfile.read.assert_called_once_with(2)
        self.assertTrue(handler.close_connection)

    def test_unbounded_rejected_body_is_never_consumed(self) -> None:
        handler = mock.Mock()
        handler.headers.get.return_value = None
        handler.headers.get_all.return_value = [str(board.MAX_REQUEST_BODY + 4097)]
        handler.close_connection = False

        board._discard_rejected_request_body(handler)

        handler.rfile.read.assert_not_called()
        self.assertTrue(handler.close_connection)


class TestOverHTTP(BoardTestCase):
    """The bytes the server really returns, not what the handler meant to."""

    def setUp(self) -> None:
        super().setUp()
        self.httpd = board.BoardHTTPServer(("127.0.0.1", 0), board.Handler,
                                          capability_token="test-board-capability")
        self.port = self.httpd.server_address[1]
        self.server_thread = threading.Thread(
            target=self.httpd.serve_forever,
            name=f"board-test-{self.port}",
            daemon=True,
        )
        self.server_thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.server_thread.join(timeout=5)
        self.assertFalse(
            self.server_thread.is_alive(),
            f"board test server on port {self.port} did not stop",
        )
        super().tearDown()

    def get(self, path: str) -> tuple[int, dict]:
        return self._call(urllib.request.Request(f"http://127.0.0.1:{self.port}{path}"))

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        body = dict(body)
        if path == "/api/finding/approve" and "review" not in body:
            body["review"] = self.review(str(body.get("slug") or ""))
        return self.post_raw(path, json.dumps(body).encode("utf-8"), {
            "Content-Type": "application/json",
            "Origin": f"http://127.0.0.1:{self.port}",
            "X-Kit-Board-Token": self.httpd.capability_token,
        })

    def post_raw(self, path: str, body: bytes, headers: dict[str, str]) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="POST",
            data=body, headers=headers)
        return self._call(req)

    def _call(self, req) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8")
            self.assertEqual(e.headers.get("Content-Type"), "application/json",
                             f"error body was not JSON: {raw[:200]}")
            return e.code, json.loads(raw)

    def test_health(self) -> None:
        code, payload = self.get("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["schema"], board.SCHEMA)
        self.assertEqual(payload["version"], board.BOARD_VERSION)
        self.assertEqual(payload["pid"], os.getpid())
        self.assertEqual(payload["port"], self.port)
        self.assertEqual(payload["instance_id"], self.httpd.instance_id)
        self.assertEqual(
            payload["repository_scope_id"], board._repository_scope_id()
        )
        self.assertEqual(
            payload["controller_runtime_id"], board._controller_runtime_id()
        )

    def test_served_html_receives_an_ephemeral_capability_and_security_headers(self) -> None:
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/plan.html", timeout=10) as r:
            body = r.read().decode("utf-8")
            headers = r.headers
        self.assertIn('name="kit-board-token" content="test-board-capability"', body)
        self.assertNotIn("test-board-capability",
                         (board.ROOT / "plan.html").read_text(encoding="utf-8"))
        scripts = re.findall(r"<script\b[^>]*>", body)
        self.assertTrue(scripts)
        self.assertTrue(all(" nonce=" in tag for tag in scripts), scripts)
        self.assertIn("frame-ancestors 'none'", headers.get("Content-Security-Policy", ""))
        self.assertIn("'nonce-", headers.get("Content-Security-Policy", ""))
        self.assertEqual("DENY", headers.get("X-Frame-Options"))
        self.assertEqual("nosniff", headers.get("X-Content-Type-Options"))
        self.assertEqual("same-origin", headers.get("Cross-Origin-Resource-Policy"))
        self.assertEqual("no-store", headers.get("Cache-Control"))
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))

    def test_capability_rotates_with_the_server_process(self) -> None:
        other = board.BoardHTTPServer(("127.0.0.1", 0), board.Handler)
        try:
            self.assertNotEqual(self.httpd.capability_token, other.capability_token)
        finally:
            other.server_close()

    def test_state(self) -> None:
        code, payload = self.get("/api/state")
        self.assertEqual(code, 200)
        self.assertEqual(len(payload["findings"]), 2)

    def test_finding_and_approve_over_the_wire(self) -> None:
        code, payload = self.get(f"/api/finding/{SLUG_A}")
        self.assertEqual(code, 200)
        preview = payload["prompt_preview"]
        code, payload = self.post("/api/finding/approve",
                                   {"slug": SLUG_A, "comment": "amend it like this"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["finding"]["state"], "working")
        sent = self.rm.spawned[0]["prompt"]
        self.assertTrue(sent.startswith(preview.rstrip("\n")))
        self.assertIn("amend it like this", sent)

    def test_wire_approval_without_rendered_review_digest_is_refused(self) -> None:
        code, payload = self.post(
            "/api/finding/approve",
            {"slug": SLUG_A, "comment": "go", "review": {}},
        )
        self.assertEqual(409, code)
        self.assertEqual("stale_review", payload["code"])
        self.assertEqual([], self.rm.spawned)

    def test_defer_over_the_wire(self) -> None:
        code, payload = self.post("/api/finding/defer", {"slug": SLUG_B, "reason": "later"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["finding"]["state"], "deferred")

    def test_retro_run_over_the_wire(self) -> None:
        code, payload = self.post("/api/retro/run", {})
        self.assertEqual(code, 200)
        self.assertTrue(payload["run_id"])

    def test_retro_read_boundary_blocker_over_the_wire_never_dispatches(self) -> None:
        blocker = board.providers.COPILOT_READ_BOUNDARY_BLOCKER

        with mock.patch.object(
            board.providers, "preflight", return_value=[blocker]
        ):
            code, payload = self.post("/api/retro/run", {})

        self.assertEqual(409, code)
        self.assertEqual("provider_unavailable", payload["code"])
        self.assertEqual([blocker], payload["provider_blockers"])
        self.assertEqual(0, self.rm.retro_started)
        self.assertEqual([], self.rm.spawned)

    def test_recorded_plan_veto_requires_capability_and_never_dispatches(self) -> None:
        decision = {
            "ok": True,
            "decision": "veto",
            "status": "draft",
            "fingerprint": "e" * 64,
            "record": "<runtime>/plan-decisions/decision.json",
            "dispatched": False,
        }
        origin = f"http://127.0.0.1:{self.port}"
        with mock.patch.object(
            board.cockpit, "record_plan_decision", return_value=decision
        ) as record, mock.patch.object(
            board, "_regenerate_views", return_value={"ok": True}
        ):
            code, payload = self.post_raw(
                "/api/plan/decision",
                json.dumps(
                    {
                        "action": "veto",
                        "fingerprint": "f" * 64,
                        "comment": "The recorded outcome needs review.",
                    }
                ).encode("utf-8"),
                {"Content-Type": "application/json", "Origin": origin},
            )
            self.assertEqual(403, code)
            self.assertEqual("invalid_capability", payload["code"])
            record.assert_not_called()

            code, payload = self.post(
                "/api/plan/decision",
                {
                    "action": "veto",
                    "fingerprint": "f" * 64,
                    "comment": "The recorded outcome needs review.",
                },
            )

        self.assertEqual(200, code)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["regenerated"])
        self.assertFalse(payload["dispatched"])
        record.assert_called_once_with(
            self.dir,
            action="veto",
            fingerprint="f" * 64,
            comment="The recorded outcome needs review.",
        )
        self.assertEqual([], self.rm.spawned)

    def test_deleted_endpoints_are_gone_and_say_so(self) -> None:
        for path in ("/api/dispatch/prepare", "/api/dispatch/run", "/api/decision"):
            code, payload = self.post(path, {})
            self.assertEqual(code, 404, path)
            self.assertEqual(payload["code"], "no_such_endpoint")
            self.assertFalse(payload["ok"])

    def test_error_paths_are_json_not_html(self) -> None:
        cases = [
            ("POST", "/api/finding/approve", {"slug": "nope", "comment": ""}, 404),
            ("POST", "/api/finding/approve", {"comment": "x"}, 400),
            ("POST", "/api/finding/defer", {"slug": "nope"}, 404),
            ("GET", "/api/finding/nope", None, 404),
            ("GET", "/api/runs/nope", None, 404),
            ("GET", "/api/ping", None, 404),
            ("GET", "/api/nonsense", None, 404),
            ("GET", "/nonsense.html", None, 404),
        ]
        for method, path, body, expected in cases:
            code, payload = (self.get(path) if method == "GET" else self.post(path, body))
            self.assertEqual(code, expected, f"{method} {path}")
            self.assertFalse(payload["ok"], f"{method} {path}")
            self.assertTrue(payload["error"], f"{method} {path}")
            self.assertTrue(payload["code"], f"{method} {path}")

    def test_an_unhandled_exception_is_json_not_a_traceback(self) -> None:
        saved = board.finding_states
        board.finding_states = lambda silence=None: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            code, payload = self.get("/api/state")
        finally:
            board.finding_states = saved
        self.assertEqual(code, 500)
        self.assertEqual(payload["code"], "server_error")
        self.assertEqual("The board could not complete this request.", payload["error"])
        self.assertRegex(payload["error_id"], r"^[0-9a-f]{16}$")
        self.assertNotIn("boom", json.dumps(payload))

    def test_a_stale_approval_is_refused_over_the_wire(self) -> None:
        self.findings.write_text(self.findings_text + "\nedited\n", encoding="utf-8")
        code, payload = self.post("/api/finding/approve", {"slug": SLUG_A, "comment": "go"})
        self.assertEqual(code, 409)
        self.assertEqual(payload["code"], "stale")
        self.assertEqual(self.rm.spawned, [])

    def test_mutations_fail_closed_before_state_changes(self) -> None:
        origin = f"http://127.0.0.1:{self.port}"
        valid = {
            "Content-Type": "application/json",
            "Origin": origin,
            "X-Kit-Board-Token": self.httpd.capability_token,
        }
        cases = [
            ({"Content-Type": "application/json", "Origin": origin}, b"{}", 403,
             "invalid_capability"),
            ({**valid, "X-Kit-Board-Token": "wrong"}, b"{}", 403,
             "invalid_capability"),
            ({"Content-Type": "application/json",
              "X-Kit-Board-Token": self.httpd.capability_token}, b"{}", 403,
             "invalid_origin"),
            ({**valid, "Origin": "null"}, b"{}", 403, "invalid_origin"),
            ({**valid, "Origin": "https://attacker.invalid"}, b"{}", 403,
             "invalid_origin"),
            ({**valid, "Content-Type": "text/plain"}, b"{}", 415,
             "unsupported_media_type"),
            (valid, b"not json", 400, "invalid_json"),
            (valid, b"[]", 400, "invalid_json_root"),
            (valid, b"{" + (b"x" * board.MAX_REQUEST_BODY) + b"}", 413,
             "body_too_large"),
        ]
        for headers, raw, expected_status, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                code, payload = self.post_raw("/api/finding/approve", raw, headers)
                self.assertEqual(expected_status, code, expected_code)
                self.assertEqual(expected_code, payload["code"])
                self.assertEqual([], self.rm.spawned)
                self.assertFalse(board.ACCEPTED_FILE.exists())

    def test_spoofed_host_and_preflight_are_rejected(self) -> None:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/finding/approve",
            method="POST", data=b"{}",
            headers={
                "Host": "attacker.invalid",
                "Content-Type": "application/json",
                "Origin": f"http://127.0.0.1:{self.port}",
                "X-Kit-Board-Token": self.httpd.capability_token,
            },
        )
        code, payload = self._call(req)
        self.assertEqual(421, code)
        self.assertEqual("invalid_host", payload["code"])

        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/finding/approve",
            method="OPTIONS",
            headers={"Origin": "https://attacker.invalid",
                     "Access-Control-Request-Method": "POST"},
        )
        code, payload = self._call(req)
        self.assertEqual(403, code)
        self.assertEqual("cors_forbidden", payload["code"])
        self.assertEqual([], self.rm.spawned)


if __name__ == "__main__":
    unittest.main(verbosity=2)
