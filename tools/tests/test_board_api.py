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

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import board          # noqa: E402
import retro_queue    # noqa: E402
import retro_rank     # noqa: E402
import session_digest  # noqa: E402

FINDINGS = """# Retrospective 2026-01-01

Header prose.

## Finding: the gate ran twice for one edit

sessions: [S1]
human_turns: [S1:H1]
mechanical: []
recurs: true
severity: none
fix_files: [tools/thing.py]
fix_lines: 12

Body prose for the first finding.

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

SLUG_A = "the-gate-ran-twice-for-one-edit"
SLUG_B = "a-second-differently-named-problem"

FAKE_SESSIONS = [{"log": Path("nonexistent-1.jsonl"), "id": "s1"}]


def _fake_digest(sess: dict, index: int) -> dict:
    return {"index": index, "started": "2026-01-01T00:00:00Z",
            "human": [f"human message {i}" for i in range(1, 10)]}


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

    def enqueue(self, item: dict) -> dict:
        self.queue.append(item)
        if self.active is None and not self._halted:
            return self._start(item)
        return {"run_id": None, "status": "queued", "finding": item["finding"],
                "slug": item["slug"], "queue_position": len(self.queue),
                "queue_total": len(self.queue)}

    def _start(self, item: dict) -> dict:
        run_id = f"run-{len(self.spawned) + 1}"
        item["_run_id"] = run_id
        self.spawned.append({"run_id": run_id, **item})
        self.active = run_id
        board.annotate_accepted(item["slug"], run_id=run_id, state="working")
        return {"run_id": run_id, "status": "running", "finding": item["finding"],
                "slug": item["slug"]}

    def finish(self, ok: bool = True) -> None:
        self.active = None
        if not ok:
            self._halted = True
            return
        nxt = next((i for i in self.queue if not i.get("_run_id")), None)
        if nxt is not None:
            self._start(nxt)

    def queue_view(self) -> list[dict]:
        return [{"finding": i["finding"], "slug": i["slug"], "run_id": i.get("_run_id"),
                 "position": n + 1, "total": len(self.queue)}
                for n, i in enumerate(self.queue)]

    def halted(self) -> bool:
        return self._halted

    def spawn_retro(self) -> dict:
        if self.retro_fails:
            return {"error": self.retro_fails}
        self.retro_started += 1
        return {"run_id": f"retro-{self.retro_started}", "kind": "retro",
                "status": "running"}


class BoardTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.findings = self.dir / "2026-01-01-findings.md"
        self.findings.write_text(FINDINGS, encoding="utf-8")
        self.queue_dir = self.dir / "queue"

        self._saved = {
            "discover": session_digest.discover,
            "digest_one": session_digest.digest_one,
            "persona": retro_rank.session_persona,
            "queue_root": retro_queue.ROOT,
            "queue_dir": retro_queue.QUEUE_DIR,
            "rank_retro": retro_rank.RETRO_DIR,
            "rank_deferred": retro_rank.DEFERRED_FILE,
            "board_retro": board.RETRO_DIR,
            "board_runs": board.RUNS_DIR,
            "board_state": board.STATE_FILE,
            "board_lock": board.LOCK_FILE,
            "board_accepted": board.ACCEPTED_FILE,
            "regen": board._regenerate,
            "rm": board._run_manager,
        }
        session_digest.discover = lambda repo, roots=None: list(FAKE_SESSIONS)
        session_digest.digest_one = _fake_digest
        retro_rank.session_persona = lambda log: "game-builder"
        retro_queue.ROOT = self.dir
        retro_queue.QUEUE_DIR = self.queue_dir
        retro_rank.RETRO_DIR = self.dir
        retro_rank.DEFERRED_FILE = self.dir / "deferred.json"
        board.RETRO_DIR = self.dir
        board.RUNS_DIR = self.dir / "runs"
        board.STATE_FILE = self.dir / "board.state.json"
        board.LOCK_FILE = self.dir / "board.lock"
        board.ACCEPTED_FILE = self.dir / "accepted.json"
        board._regenerate = lambda script: None   # never shell out in a test
        self.rm = FakeRunManager()
        board._run_manager = self.rm

        retro_queue.build(findings_dir=self.dir, queue_dir=self.queue_dir)

    def tearDown(self) -> None:
        session_digest.discover = self._saved["discover"]
        session_digest.digest_one = self._saved["digest_one"]
        retro_rank.session_persona = self._saved["persona"]
        retro_queue.ROOT = self._saved["queue_root"]
        retro_queue.QUEUE_DIR = self._saved["queue_dir"]
        retro_rank.RETRO_DIR = self._saved["rank_retro"]
        retro_rank.DEFERRED_FILE = self._saved["rank_deferred"]
        board.RETRO_DIR = self._saved["board_retro"]
        board.RUNS_DIR = self._saved["board_runs"]
        board.STATE_FILE = self._saved["board_state"]
        board.LOCK_FILE = self._saved["board_lock"]
        board.ACCEPTED_FILE = self._saved["board_accepted"]
        board._regenerate = self._saved["regen"]
        board._run_manager = self._saved["rm"]
        self.tmp.cleanup()

    def accepted(self) -> list[dict]:
        return json.loads(board.ACCEPTED_FILE.read_text(encoding="utf-8"))


class TestApprove(BoardTestCase):
    def test_comment_persists_and_is_dispatched(self) -> None:
        code, payload = board.api_approve(SLUG_A, "Keep the CLI, drop the flag.", by="jf")
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

    def test_comment_survives_a_reload_of_the_decision_file(self) -> None:
        board.api_approve(SLUG_A, "the comment is the amendment")
        reloaded = board.load_accepted()
        self.assertEqual(reloaded[0]["comment"], "the comment is the amendment")

    def test_approving_a_stale_artifact_is_refused(self) -> None:
        self.findings.write_text(FINDINGS + "\nan edit landed under it.\n", encoding="utf-8")
        code, payload = board.api_approve(SLUG_A, "go")
        self.assertEqual(code, 409)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "stale")
        self.assertIn("stale", payload["error"])
        self.assertIn("retro_rank.py", payload["error"])
        # refused means refused: nothing dispatched, no decision recorded
        self.assertEqual(self.rm.spawned, [])
        self.assertFalse(board.ACCEPTED_FILE.exists())

    def test_unknown_slug_is_a_structured_404(self) -> None:
        code, payload = board.api_approve("no-such-finding", "go")
        self.assertEqual(code, 404)
        self.assertEqual(payload["code"], "missing_artifact")
        self.assertEqual(self.rm.spawned, [])

    def test_missing_slug_is_a_structured_400(self) -> None:
        code, payload = board.api_approve("", "go")
        self.assertEqual(code, 400)
        self.assertEqual(payload["code"], "bad_request")

    def test_a_spawn_failure_is_reported_not_swallowed(self) -> None:
        self.rm.enqueue = lambda item: {"error": "could not spawn copilot: not on PATH"}
        code, payload = board.api_approve(SLUG_A, "go")
        self.assertEqual(code, 500)
        self.assertEqual(payload["code"], "spawn_failed")
        self.assertIn("copilot", payload["error"])
        # the decision is still on disk: the human approved it, the machine failed
        self.assertEqual(self.accepted()[0]["comment"], "go")


class TestSequentialQueue(BoardTestCase):
    def test_second_approval_queues_behind_the_first(self) -> None:
        board.api_approve(SLUG_A, "first")
        board.api_approve(SLUG_B, "second")
        self.assertEqual(len(self.rm.spawned), 1, "two workers ran concurrently")
        self.assertEqual(self.rm.spawned[0]["slug"], SLUG_A)

        states = {f["slug"]: f for f in board.finding_states()}
        self.assertEqual(states[SLUG_A]["state"], "working")
        self.assertEqual(states[SLUG_B]["state"], "queued")
        self.assertIn("queued", states[SLUG_B]["status_detail"])

    def test_the_queued_item_starts_when_the_first_finishes(self) -> None:
        board.api_approve(SLUG_A, "first")
        board.api_approve(SLUG_B, "second")
        self.rm.finish(ok=True)
        self.assertEqual([s["slug"] for s in self.rm.spawned], [SLUG_A, SLUG_B])

    def test_a_failed_run_halts_the_queue_visibly(self) -> None:
        board.api_approve(SLUG_A, "first")
        board.api_approve(SLUG_B, "second")
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
                      queue_total=None, slug=None):
                run_id = f"run-{len(started) + 1}"
                started.append({"run_id": run_id, "finding": finding, "slug": slug,
                                "prompt": prompt, "queue_position": queue_position})
                return {"run_id": run_id, "status": "running", "finding": finding}

        self.mgr = Recording()
        board._run_manager = self.mgr

    def item(self, slug: str) -> dict:
        art = retro_queue.load_item(slug, self.queue_dir)
        return {"finding": art["title"], "slug": slug,
                "prompt": retro_queue.render_prompt(art, "c")}

    def test_only_one_runs_at_a_time(self) -> None:
        first = self.mgr.enqueue(self.item(SLUG_A))
        second = self.mgr.enqueue(self.item(SLUG_B))
        self.assertEqual(first["run_id"], "run-1")
        self.assertIsNone(second["run_id"])
        self.assertEqual(second["status"], "queued")
        self.assertEqual(len(self.started), 1)

    def test_the_next_item_starts_after_the_previous_exits(self) -> None:
        self.mgr.enqueue(self.item(SLUG_A))
        self.mgr.enqueue(self.item(SLUG_B))
        self.mgr._active = None            # what _watch does on a clean exit
        self.mgr._advance_queue()
        self.assertEqual([s["slug"] for s in self.started], [SLUG_A, SLUG_B])
        self.assertEqual(self.started[1]["queue_position"], 2)

    def test_a_halted_queue_does_not_start_the_next_item(self) -> None:
        self.mgr.enqueue(self.item(SLUG_A))
        self.mgr.enqueue(self.item(SLUG_B))
        self.mgr._active = None
        self.mgr._halted = True            # what _watch does on a failure
        self.mgr._advance_queue()
        self.assertEqual(len(self.started), 1)
        self.assertTrue(self.mgr.halted())

    def test_a_third_approval_lands_behind_both(self) -> None:
        self.mgr.enqueue(self.item(SLUG_A))
        self.mgr.enqueue(self.item(SLUG_B))
        third = self.mgr.enqueue({"finding": "x", "slug": "x", "prompt": "p"})
        self.assertEqual(third["queue_position"], 3)
        self.assertEqual(len(self.started), 1)


class TestState(BoardTestCase):
    def test_state_shape(self) -> None:
        code, payload = board.api_state()
        self.assertEqual(code, 200)
        for key in ("board", "retro_due", "findings", "runs"):
            self.assertIn(key, payload)
        self.assertEqual({f["slug"] for f in payload["findings"]}, {SLUG_A, SLUG_B})
        for f in payload["findings"]:
            self.assertEqual(f["state"], "awaiting_review")
            self.assertIn("stale", f)
            self.assertIn("status_detail", f)
            self.assertIn("resume_cmd", f)

    def test_retro_due_is_surfaced(self) -> None:
        _code, payload = board.api_state()
        self.assertIn("due", payload["retro_due"])
        self.assertIn("threshold", payload["retro_due"])

    def test_a_silent_worker_is_legible(self) -> None:
        board.api_approve(SLUG_A, "go")
        run_id = self.rm.spawned[0]["run_id"]
        log = self.dir / "worker.log"
        log.write_text("started\n", encoding="utf-8")
        old = os.path.getmtime(log) - 3600
        os.utime(log, (old, old))
        state = board.load_state()
        state["runs"] = [{"run_id": run_id, "kind": "finding", "finding": "x",
                          "slug": SLUG_A, "status": "running", "pid": os.getpid(),
                          "log": "worker.log", "t0": 0,
                          "resume_cmd": "copilot --resume=abc", "exit_code": None}]
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
        self.assertEqual(entry["resume_cmd"], "copilot --resume=abc")

    def test_stale_is_orthogonal_to_state(self) -> None:
        self.findings.write_text(FINDINGS + "\nedit\n", encoding="utf-8")
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

    def test_finding_unknown_slug(self) -> None:
        code, payload = board.api_finding("nope")
        self.assertEqual(code, 404)
        self.assertEqual(payload["code"], "missing_artifact")

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
        log = self.dir / "r.log"
        log.write_text("line one\nline two\n", encoding="utf-8")
        board.save_state({"port": 1, "pid": os.getpid(), "started": "now", "runs": [
            {"run_id": "r1", "kind": "finding", "finding": "x", "slug": SLUG_A,
             "status": "finished", "exit_code": 0, "pid": os.getpid(),
             "log": "r.log", "t0": 0, "resume_cmd": "copilot --resume=abc"},
        ]})
        saved_root = board.ROOT
        board.ROOT = self.dir
        try:
            code, payload = board.api_run("r1")
        finally:
            board.ROOT = saved_root
        self.assertEqual(code, 200)
        self.assertIn("line two", payload["tail"])
        self.assertEqual(payload["resume_cmd"], "copilot --resume=abc")
        self.assertEqual(payload["exit_code"], 0)


class TestLiveness(BoardTestCase):
    """Review finding 5: state JSON existing is not evidence a board is alive."""

    def test_dead_pid_and_dead_port(self) -> None:
        board.save_state({"port": 9, "pid": 999999, "started": "now", "runs": []})
        saved = board._probe
        board._probe = lambda port, timeout=1.0: False
        try:
            health = board.board_health()
        finally:
            board._probe = saved
        self.assertFalse(health["alive"])
        self.assertFalse(health["pid_alive"])
        self.assertFalse(health["port_alive"])
        self.assertIn("999999", health["detail"])

    def test_live_pid_but_dead_port_is_not_alive(self) -> None:
        board.save_state({"port": 9, "pid": os.getpid(), "started": "now", "runs": []})
        saved = board._probe
        board._probe = lambda port, timeout=1.0: False
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
        board._probe = lambda port, timeout=1.0: True
        try:
            health = board.board_health()
        finally:
            board._probe = saved
        self.assertFalse(health["alive"])
        self.assertIn("is gone", health["detail"])

    def test_both_alive(self) -> None:
        board.save_state({"port": 9, "pid": os.getpid(), "started": "now", "runs": []})
        saved = board._probe
        board._probe = lambda port, timeout=1.0: True
        try:
            health = board.board_health()
        finally:
            board._probe = saved
        self.assertTrue(health["alive"])
        self.assertEqual(health["detail"], "")

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
            self.assertFalse(board._probe(httpd.server_address[1]))
        finally:
            httpd.shutdown()


class TestOverHTTP(BoardTestCase):
    """The bytes the server really returns, not what the handler meant to."""

    def setUp(self) -> None:
        super().setUp()
        import http.server
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), board.Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def get(self, path: str) -> tuple[int, dict]:
        return self._call(urllib.request.Request(f"http://127.0.0.1:{self.port}{path}"))

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"})
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

    def test_defer_over_the_wire(self) -> None:
        code, payload = self.post("/api/finding/defer", {"slug": SLUG_B, "reason": "later"})
        self.assertEqual(code, 200)
        self.assertEqual(payload["finding"]["state"], "deferred")

    def test_retro_run_over_the_wire(self) -> None:
        code, payload = self.post("/api/retro/run", {})
        self.assertEqual(code, 200)
        self.assertTrue(payload["run_id"])

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
        self.assertIn("boom", payload["error"])

    def test_a_stale_approval_is_refused_over_the_wire(self) -> None:
        self.findings.write_text(FINDINGS + "\nedited\n", encoding="utf-8")
        code, payload = self.post("/api/finding/approve", {"slug": SLUG_A, "comment": "go"})
        self.assertEqual(code, 409)
        self.assertEqual(payload["code"], "stale")
        self.assertEqual(self.rm.spawned, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
