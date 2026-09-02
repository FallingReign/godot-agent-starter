#!/usr/bin/env python3
"""The seams between the pieces of the dispatch pipeline.

    python tools/tests/test_integration.py

Workers 1-3 built `retro_queue.py`, `board.py` and the browser side in
parallel against a written contract rather than against each other. Their own
suites prove each piece against that contract. This one proves the pieces
against *each other*, which is where a parallel build actually breaks:

* **Mock/real parity.** `tools/tests/mock_board.py` is what every frontend
  test talks to. If it and `board.py` disagree about a key, a `state` value or
  an error `code`, the frontend suite goes green against a shape the product
  never serves. So both servers are started and their bytes compared.

* **The loop, end to end.** One approval, through a real HTTP request, a real
  `RunManager`, a real spawned process, to the prompt file on disk -- asserting
  the dispatched bytes are the bytes the page displayed. The spawned executable
  is an edit-only stub: the host must seal Git metadata, validate and commit its
  bounded change, run the gate and integrate it. No quota is spent, and nothing
  here can reach a model.

Every path is redirected into a temp directory, so no test here writes to
docs/retro/.
"""
from __future__ import annotations

import http.server
import json
import os
import stat
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOLS = HERE.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(HERE))

import board          # noqa: E402
import mock_board     # noqa: E402
import process_supervisor  # noqa: E402
import retro_queue    # noqa: E402
from test_board_api import SLUG_A, SLUG_B, BoardTestCase  # noqa: E402


def _serve(handler_cls) -> tuple[board.BoardHTTPServer, int]:
    httpd = board.BoardHTTPServer(("127.0.0.1", 0), handler_cls)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _fetch(port: int, path: str) -> tuple[int, dict]:
    """Read the bytes actually served, and insist they are JSON."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            raw = r.read()
            code = r.status
            ctype = r.headers.get("Content-Type", "")
    except urllib.error.HTTPError as e:
        raw, code, ctype = e.read(), e.code, e.headers.get("Content-Type", "")
    assert "json" in ctype, f"{path} served {ctype!r}: {raw[:200]!r}"
    return code, json.loads(raw.decode("utf-8"))


# A stored run record shaped exactly as RunManager.spawn writes one, so the
# real /api/state has a run to project and its key set can be compared.
STORED_RUN = {
    "run_id": "20260819T000000Z", "kind": "finding", "persona": "kit-builder",
    "finding": "a finding", "slug": SLUG_A, "queue_position": 1, "queue_total": 1,
    "started": "2026-08-19T00:00:00+00:00", "t0": time.time(),
    "log": None, "prompt_file": None, "pid": os.getpid(), "status": "running",
    "session_id": None, "resume_cmd": None, "exit_code": None,
}


class TestMockRealParity(BoardTestCase):
    """The mock and the real board must answer with the same shape."""

    def setUp(self) -> None:
        super().setUp()
        board.save_state({"port": 0, "pid": os.getpid(), "started": "now",
                          "runs": [dict(STORED_RUN)]})
        self.real, self.real_port = _serve(board.Handler)
        self.mock, self.mock_port = _serve(mock_board.Handler)
        mock_board.SCENARIO = "healthy"

    def tearDown(self) -> None:
        for srv in (self.real, self.mock):
            srv.shutdown()
            srv.server_close()
        super().tearDown()

    def both(self, path: str) -> tuple[dict, dict]:
        _, real = _fetch(self.real_port, path)
        _, mock = _fetch(self.mock_port, path)
        return real, mock

    def assertSameKeys(self, real: dict, mock: dict, where: str) -> None:
        missing = sorted(set(real) - set(mock))
        extra = sorted(set(mock) - set(real))
        self.assertEqual((missing, extra), ([], []),
                         f"{where}: mock is missing {missing} and invents {extra}")

    def test_health_agrees(self) -> None:
        real, mock = self.both("/api/health")
        self.assertSameKeys(real, mock, "/api/health")
        self.assertEqual(real["schema"], mock["schema"])
        self.assertEqual(real["version"], mock["version"])

    def test_state_top_level_agrees(self) -> None:
        real, mock = self.both("/api/state")
        self.assertSameKeys(real, mock, "/api/state")
        self.assertSameKeys(real["board"], mock["board"], "/api/state board")
        self.assertSameKeys(real["retro_due"], mock["retro_due"], "/api/state retro_due")

    def test_state_findings_agree(self) -> None:
        real, mock = self.both("/api/state")
        self.assertTrue(real["findings"] and mock["findings"], "no findings to compare")
        self.assertSameKeys(real["findings"][0], mock["findings"][0],
                            "/api/state findings[]")

    def test_state_runs_agree(self) -> None:
        real, mock = self.both("/api/state")
        self.assertTrue(real["runs"] and mock["runs"], "no runs to compare")
        self.assertSameKeys(real["runs"][0], mock["runs"][0], "/api/state runs[]")

    def test_mock_uses_only_the_real_state_vocabulary(self) -> None:
        _, mock = _fetch(self.mock_port, "/api/state")
        for f in mock["findings"]:
            self.assertIn(f["state"], board.STATES, f"{f['slug']}: not a board state")

    def test_finding_detail_agrees(self) -> None:
        real, mock = self.both(f"/api/finding/{SLUG_A}")
        self.assertSameKeys(real, mock, "/api/finding/<slug>")
        self.assertEqual(real["prompt_preview"], mock["prompt_preview"],
                         "the mock previews a different prompt than the board")
        self.assertSameKeys(real["finding"], mock["finding"], "artifact")

    def test_error_envelopes_agree(self) -> None:
        for path in (f"/api/finding/{'nosuchslug'}", "/nonsense.html"):
            rcode, real = _fetch(self.real_port, path)
            mcode, mock = _fetch(self.mock_port, path)
            self.assertEqual(rcode, mcode, path)
            self.assertSameKeys(real, mock, f"error body for {path}")
            self.assertEqual(real["code"], mock["code"],
                             f"{path}: error code vocabulary differs")
            self.assertFalse(real["ok"])
            self.assertFalse(mock["ok"])


def _git(directory: Path, *args: str) -> str:
    executable = process_supervisor.resolve_ordinary_executable(
        "git", excluded_roots=(directory, TOOLS.parent)
    )
    result = subprocess.run([executable, "-C", str(directory), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def _stub_worker(directory: Path) -> Path:
    """An executable that behaves like a worker and costs nothing.

    It sleeps long enough for the board to observe a `working` item and a
    second approval queued behind it, then exits 0. This is what keeps the
    end-to-end test off a paid model while still exercising Popen, the
    watcher thread, the queue and the state file.
    """
    seconds = 3
    worker = directory / "stub_worker.py"
    worker.write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        f"time.sleep({seconds})\n"
        "mode = sys.argv[1]\n"
        "if mode == 'missing':\n"
        "    raise SystemExit(0)\n"
        "run_id = os.environ['KIT_RUN_ID']\n"
        "prompt = sys.stdin.read()\n"
        "name = 'check.py' if 'fix_files: check.py' in prompt else 'tools/thing.py'\n"
        "Path(name).parent.mkdir(parents=True, exist_ok=True)\n"
        "existing = Path(name).read_text(encoding='utf-8') if Path(name).exists() else ''\n"
        "Path(name).write_text(existing + '# host-finalized ' + run_id + '\\n', "
        "encoding='utf-8')\n",
        encoding="utf-8",
    )
    return worker


def _stub_command(worker: Path, mode: str) -> list[str]:
    """Return an argv-only command that never passes through a command shell.

    The provider command builder has its own exact-argv security tests. This
    seam test needs a stable child process on every platform, especially on
    Windows where a ``.cmd`` wrapper can reinterpret a multiline prompt before
    the stub sees it.
    """
    return [sys.executable, str(worker), mode]


class TestEndToEnd(BoardTestCase):
    """Approve over HTTP; a real process runs; the prompt on disk is the
    prompt the page showed."""

    def setUp(self) -> None:
        super().setUp()
        self._root = board.ROOT
        self._launcher = board.providers._copilot_launcher
        self._worker_command = board.providers.worker_command
        board.ROOT = self.dir                    # runs/ live under the temp repo
        (self.dir / "check.py").write_text("print('GATE PASSED')\n", encoding="utf-8")
        (self.dir / ".gitignore").write_text(".kit/\n", encoding="utf-8")
        (self.dir / "kit.config.json").write_text(json.dumps({
            "schema": 1,
            "runtime_root": ".kit/runtime",
            "dispatch_policy": {
                "owned": ["tools", "check.py"],
                "forbidden": [".kit", "docs/retro", "src"],
            },
        }), encoding="utf-8")
        _git(self.dir, "init")
        _git(self.dir, "config", "user.email", "board-test@example.invalid")
        _git(self.dir, "config", "user.name", "Board Integration")
        _git(self.dir, "add", ".")
        _git(self.dir, "commit", "-m", "fixture baseline")
        self.stub_dir = self.runtime / "test-stubs"
        self.stub_dir.mkdir(parents=True, exist_ok=True)
        self.stub = _stub_worker(self.stub_dir)
        board.providers._copilot_launcher = lambda: [sys.executable]
        board.providers.worker_command = (
            lambda _spec: _stub_command(self.stub, "implemented")
        )
        board._run_manager = board.RunManager()  # the real one, not the fake
        self.httpd, self.port = _serve(board.Handler)

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._drain()
        board.ROOT = self._root
        board.providers._copilot_launcher = self._launcher
        board.providers.worker_command = self._worker_command
        super().tearDown()

    def _drain(self, timeout: float = 40.0) -> None:
        """Let every stub worker exit before the temp repo is deleted.

        A running child holds its log open, and on Windows that makes the
        directory undeletable -- a test that leaves a worker running fails in
        teardown rather than in the assertion that matters.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            runs = board.load_state().get("runs", [])
            if not any(r.get("status") in ("starting", "running", "verifying")
                       for r in runs):
                time.sleep(0.3)   # let the watcher thread close its handles
                return
            time.sleep(0.25)

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", method="POST",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Origin": f"http://127.0.0.1:{self.port}",
                     "X-Kit-Board-Token": self.httpd.capability_token})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def approval_body(self, slug: str, comment: str, *, by: str = "") -> dict:
        code, detail = _fetch(self.port, f"/api/finding/{slug}")
        self.assertEqual(code, 200, detail)
        return {
            "slug": slug,
            "comment": comment,
            "by": by,
            "review": detail["review"],
        }

    def state_of(self, slug: str) -> dict:
        _, payload = _fetch(self.port, "/api/state")
        return next(f for f in payload["findings"] if f["slug"] == slug)

    def wait_for(self, slug: str, states: tuple[str, ...], timeout: float = 40.0) -> dict:
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            last = self.state_of(slug)
            if last["state"] in states:
                return last
            time.sleep(0.25)
        self.fail(f"{slug} never reached {states}; last was {last.get('state')!r}")

    def wait_run_started(self, run_id: str, timeout: float = 10.0) -> dict:
        deadline = time.time() + timeout
        run: dict = {}
        while time.time() < deadline:
            run = next(r for r in board.load_state()["runs"] if r["run_id"] == run_id)
            if run.get("status") == "running" and run.get("pid"):
                return run
            time.sleep(0.05)
        self.fail(
            f"{run_id} did not start; status={run.get('status')!r}; "
            f"errors={run.get('result_errors')!r}"
        )

    def test_approve_dispatches_the_prompt_the_page_displayed(self) -> None:
        comment = "Do the smallest version, and keep the CLI."

        # what the page shows before approving
        _, detail = _fetch(self.port, f"/api/finding/{SLUG_A}")
        preview = detail["prompt_preview"]

        code, payload = self.post(
            "/api/finding/approve",
            self.approval_body(SLUG_A, comment, by="jf"),
        )
        self.assertEqual(code, 200, payload)
        run_id = payload["run"]["run_id"]
        self.assertTrue(run_id, "approve returned no run")

        # (a) the comment persisted, before anything was spawned
        entry = next(e for e in self.accepted() if e["slug"] == SLUG_A)
        self.assertEqual(entry["comment"], comment)

        # (b) a worker really was spawned -- a live pid, not a bookkeeping entry
        run = self.wait_run_started(run_id)
        self.assertTrue(board._pid_alive(run["pid"]), "no live worker process")

        # (c) the dispatched bytes are the displayed bytes plus the comment
        prompt_file = board.RUNS_DIR / f"{run_id}.prompt.md"
        written = prompt_file.read_text(encoding="utf-8")
        item = retro_queue.load_item(SLUG_A, self.queue_dir)
        self.assertEqual(written, retro_queue.render_prompt(item, comment))
        self.assertTrue(written.startswith(preview.rstrip("\n")),
                        "the prompt sent is not the prompt previewed")
        self.assertIn(comment, written)

        # (d) the finding moves working -> done, and a second approval queues
        self.assertEqual(self.state_of(SLUG_A)["state"], "working")
        code, _ = self.post(
            "/api/finding/approve", self.approval_body(SLUG_B, "second")
        )
        self.assertEqual(code, 200)
        self.assertEqual(self.state_of(SLUG_B)["state"], "queued",
                         "a second approval ran concurrently instead of queueing")

        done = self.wait_for(SLUG_A, ("done", "blocked", "unverified", "failed"))
        self.assertEqual(done["state"], "done", done["status_detail"])
        self.assertEqual(
            next(r for r in board.load_state()["runs"] if r["run_id"] == run_id)["exit_code"], 0)

        # the queued item is picked up only after the first exits
        second = self.wait_for(SLUG_B, ("working", "done"))
        self.assertIn(second["state"], ("working", "done"))

    def test_a_no_comment_approval_dispatches_the_preview_byte_for_byte(self) -> None:
        _, detail = _fetch(self.port, f"/api/finding/{SLUG_A}")
        preview = detail["prompt_preview"]
        code, payload = self.post(
            "/api/finding/approve", self.approval_body(SLUG_A, "")
        )
        self.assertEqual(code, 200, payload)
        written = (board.RUNS_DIR / f"{payload['run']['run_id']}.prompt.md").read_text(
            encoding="utf-8")
        self.assertEqual(written, preview)
        self.wait_for(SLUG_A, ("done", "blocked", "unverified", "failed"))

    def test_exit_zero_without_edits_is_blocked_and_halts_queue(self) -> None:
        board.providers.worker_command = (
            lambda _spec: _stub_command(self.stub, "missing")
        )
        code, first = self.post(
            "/api/finding/approve", self.approval_body(SLUG_A, "plain exit")
        )
        self.assertEqual(code, 200, first)
        code, second = self.post(
            "/api/finding/approve", self.approval_body(SLUG_B, "must wait")
        )
        self.assertEqual(code, 200, second)
        outcome = self.wait_for(SLUG_A, ("blocked", "unverified", "failed"))
        self.assertEqual(outcome["state"], "blocked", outcome["status_detail"])
        self.assertIn("without proposed file changes", outcome["status_detail"],
                      board.load_state())
        queued = self.state_of(SLUG_B)
        self.assertEqual(queued["state"], "queued")
        _code, state = _fetch(self.port, "/api/state")
        self.assertTrue(state["queue_halted"])


class TestAcceptedMigration(BoardTestCase):
    """A decision from the pre-comment pipeline must not look like a queue."""

    LEGACY = [{"finding": "the gate ran twice for one edit", "date": "2026-08-18",
               "by": "justin", "reason": "worth doing"}]

    def test_legacy_entry_is_not_reported_as_queued(self) -> None:
        board.save_accepted(self.LEGACY)
        f = next(f for f in board.finding_states() if f["slug"] == SLUG_A)
        self.assertEqual(f["state"], "awaiting_review")
        self.assertEqual(f["comment"], "")
        self.assertIn("approve again", f["status_detail"])

    def test_migration_rewrites_the_file_once_and_is_idempotent(self) -> None:
        board.save_accepted(self.LEGACY)
        self.assertEqual(board.migrate_accepted_file(), 1)
        entry = self.accepted()[0]
        self.assertEqual(entry["slug"], SLUG_A)
        self.assertEqual(entry["comment"], "")
        self.assertEqual(entry["state"], "awaiting_review")
        self.assertEqual(entry["reason"], "worth doing")   # the record is kept
        self.assertEqual(board.migrate_accepted_file(), 0)

    def test_migration_leaves_a_real_decision_alone(self) -> None:
        code, payload = self.approve(SLUG_A, "a real amendment")
        self.assertEqual(code, 200, payload)
        before = self.accepted()
        self.assertEqual(board.migrate_accepted_file(), 0)
        self.assertEqual(self.accepted(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
