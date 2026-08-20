#!/usr/bin/env python3
"""A board that implements the HTTP contract and nothing else.

Worker 2 owns the real `tools/board.py`. This exists so the browser side can
be exercised against *every* state the contract can produce -- including the
ones a healthy board will not produce on demand: a 500, a non-JSON body, a
worker that has been silent for half an hour, a worker that exited non-zero.
Those are exactly the states review finding 4 was about, and they are the ones
that cannot be tested by waiting for reality to supply them.

    python tools/tests/mock_board.py --port 8899 --scenario healthy
    python tools/tests/mock_board.py --port 8899 --scenario broken

Scenarios:
    healthy   health ok; one awaiting, one working+silent, one failed, one done
    broken    /api/health ok but every POST answers 500
    garbage   /api/health ok but every POST answers HTML instead of JSON
    dead      nothing listens (the process exits immediately)

It serves the real generated plan.html and retro.html from the repo root, so
what a browser loads from it is the artefact being shipped, not a fixture.

Its payloads are held to the real board's by `test_integration.py`: a mock
that drifts from `board.py` is a test suite that passes while the product is
broken, so the two are compared key for key rather than by eye.
"""
from __future__ import annotations

import argparse
import http.server
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import retro_queue  # noqa: E402

SCENARIO = "healthy"


def _slugs() -> list[str]:
    return list(retro_queue.load_index().get("items", []))


def state_payload() -> dict:
    slugs = _slugs()
    states = [
        ("awaiting_review", "", "", None),
        ("working", "worker started 34 min ago, silent for 27 min", "Do the smallest version first.", "run-1"),
        ("failed", "worker exited 1 after 4 min", "Only touch tools/.", "run-2"),
        ("done", "finished in 12 min, gate green", "", "run-3"),
        ("queued", "behind 1 running worker", "Watch the naming.", None),
    ]
    findings = []
    for i, slug in enumerate(slugs):
        st, detail, comment, run_id = states[i % len(states)]
        item = retro_queue.load_item(slug) or {}
        findings.append({
            "slug": slug,
            "title": item.get("title", slug),
            "severity": item.get("severity", "none"),
            "state": st,
            "comment": comment,
            "stale": False,
            "run_id": run_id,
            "status_detail": detail,
            "resume_cmd": None,
            "updated_at": "2026-08-19T00:00:00Z",
        })
    runs = [
        {"run_id": "run-1", "kind": "finding", "finding": "silent worker",
         "slug": None, "status": "running", "status_label": "no output 27m",
         "persona": "kit-builder", "elapsed_s": 2040.0,
         "silence_minutes": 27.0, "exit_code": None,
         "resume_cmd": "copilot --resume 0199a-silent", "last_line": "still thinking"},
        {"run_id": "run-2", "kind": "finding", "finding": "failed worker",
         "slug": None, "status": "failed", "status_label": "failed (exit 1)",
         "persona": "kit-builder", "elapsed_s": 240.0,
         "silence_minutes": 0.0, "exit_code": 1,
         "resume_cmd": "copilot --resume 0199a-failed", "last_line": "gate failed"},
        {"run_id": "run-3", "kind": "finding", "finding": "finished worker",
         "slug": None, "status": "finished", "status_label": "finished",
         "persona": "kit-builder", "elapsed_s": 720.0,
         "silence_minutes": 0.0, "exit_code": 0,
         "resume_cmd": "copilot --resume 0199a-done", "last_line": "check.py green"},
    ]
    return {
        "ok": True,
        "schema": 1,
        "board": {"port": 0, "pid": 0, "started": "2026-08-19T00:00:00Z", "schema": 1},
        "retro_due": {"unarchived": 11, "threshold": 10, "due": True,
                      "archived": 3, "notes": ["slice-1.md", "slice-2.md"]},
        "findings": findings,
        "queue_halted": False,
        "silence_minutes": 3.0,
        "runs": runs,
    }


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "mockboard/1.0"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _static(self, name: str) -> None:
        try:
            self._send(200, (ROOT / name).read_bytes(), "text/html; charset=utf-8")
        except OSError:
            self._send(404, b"not found", "text/plain")

    def _error(self, code: int, message: str, kind: str, **extra) -> None:
        """The real board's error envelope: ok/error/code, always JSON."""
        payload = {"ok": False, "error": message, "code": kind}
        payload.update(extra)
        self._json(code, payload)

    def do_GET(self) -> None:  # noqa: N802
        path, _, _query = self.path.partition("?")
        if path in ("/", "/plan.html"):
            self._static("plan.html")
        elif path == "/retro.html":
            self._static("retro.html")
        elif path == "/api/health":
            if SCENARIO == "nohealth":
                self._error(500, "board is shutting down", "server_error")
                return
            self._json(200, {"ok": True, "port": self.server.server_address[1],
                             "pid": 0, "started": "2026-08-19T00:00:00Z", "schema": 1})
        elif path == "/api/state":
            self._json(200, state_payload())
        elif path.startswith("/api/finding/"):
            slug = path.rsplit("/", 1)[-1]
            if SCENARIO == "broken":
                self._error(500, "artifact unreadable", "server_error")
                return
            if SCENARIO == "garbage":
                self._send(200, b"<html>traceback</html>", "text/html")
                return
            item = retro_queue.load_item(slug)
            if item is None:
                self._error(404, f"no dispatch artifact for {slug!r}", "missing_artifact",
                            slug=slug)
                return
            self._json(200, {
                "ok": True,
                "finding": item,
                "stale": retro_queue.is_stale(item),
                "comment": "",
                "prompt_preview": retro_queue.render_prompt(item, ""),
            })
        else:
            self._error(404, f"no such path: {path}", "not_found")

    def do_POST(self) -> None:  # noqa: N802
        if SCENARIO == "broken":
            self._error(500, "dispatch failed: copilot not on PATH", "spawn_failed")
            return
        if SCENARIO == "garbage":
            self._send(200, b"<html><body>Internal Server Error</body></html>", "text/html")
            return
        self._json(200, {"ok": True, "finding": {"state": "queued"}})


def main() -> int:
    global SCENARIO
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--scenario", default="healthy",
                    choices=["healthy", "broken", "garbage", "nohealth", "dead"])
    a = ap.parse_args()
    SCENARIO = a.scenario
    if a.scenario == "dead":
        print("mock board: scenario 'dead', nothing will listen")
        return 0
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"mock board [{a.scenario}] on http://127.0.0.1:{a.port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
