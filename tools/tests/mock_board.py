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
    healthy   health ok; awaiting, approved-only, active, failed and done states
    broken    /api/health ok but every POST answers 500
    garbage   /api/health ok but every POST answers HTML instead of JSON
    dead      nothing listens (the process exits immediately)

It serves plan.html and retro.html generated from current source into an
isolated fixture root, so the real browser cannot accidentally validate stale
ignored output against the current board protocol.

Its payloads are held to the real board's by `test_integration.py`: a mock
that drifts from `board.py` is a test suite that passes while the product is
broken, so the two are compared key for key rather than by eye.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import board        # noqa: E402
import retro_queue  # noqa: E402

SCENARIO = "healthy"
FIXTURE_ITEMS: list[dict] | None = None


def _items() -> list[dict]:
    if FIXTURE_ITEMS is not None:
        return [dict(item) for item in FIXTURE_ITEMS]
    return [retro_queue.load_item(slug) or {"slug": slug, "title": slug}
            for slug in retro_queue.load_index().get("items", [])]


def _slugs() -> list[str]:
    return [str(item.get("slug") or "") for item in _items() if item.get("slug")]


def _load_item(slug: str) -> dict | None:
    if FIXTURE_ITEMS is not None:
        return next((dict(item) for item in FIXTURE_ITEMS
                     if item.get("slug") == slug), None)
    return retro_queue.load_item(slug)


def state_payload() -> dict:
    items = _items()
    states = [
        ("awaiting_review", "", "", None),
        ("approved", "decision recorded; automatic worker unavailable", "Keep the public flow small.", None),
        ("working", "worker started 34 min ago, silent for 27 min", "Do the smallest version first.", "run-1"),
        ("blocked", "worker reported that a required provider is unavailable", "", None),
        ("unverified", "worker exited without a structured result", "", None),
        ("failed", "worker exited 1 after 4 min", "Only touch tools/.", "run-2"),
        ("done", "finished in 12 min, gate green", "", "run-3"),
        ("queued", "behind 1 running worker", "Watch the naming.", None),
    ]
    findings = []
    for i, item in enumerate(items):
        slug = str(item.get("slug") or "")
        if not slug:
            continue
        st, detail, comment, run_id = states[i % len(states)]
        findings.append({
            "slug": slug,
            "title": item.get("title", slug),
            "severity": item.get("severity", "none"),
            "state": st,
            "comment": comment,
            "stale": False,
            "dispatchable": bool(item.get("dispatchable", False)),
            "dispatch_blockers": list(item.get("dispatch_blockers", [])),
            "evidence_snapshot": item.get("evidence_snapshot", ""),
            "run_id": run_id,
            "status_detail": detail,
            "resume_cmd": None,
            "updated_at": "2026-08-19T00:00:00Z",
        })
    runs = [
        {"run_id": "run-1", "kind": "finding", "finding": "silent worker",
         "slug": None, "status": "running", "status_label": "no output 27m",
         "provider": {"role": "worker", "kind": "copilot-cli", "model": "",
                      "persona": "kit-builder", "automatic": True},
         "model": "",
         "persona": "kit-builder", "elapsed_s": 2040.0,
         "silence_minutes": 27.0, "exit_code": None,
         "resume_cmd": "copilot --resume 0199a-silent", "last_line": "still thinking",
         "outcome": None, "result_summary": "", "result_errors": [],
         "changed_files": [], "verification": None, "integration": None,
         "workspace": None, "timeout_seconds": 1800, "timed_out": False},
        {"run_id": "run-2", "kind": "finding", "finding": "failed worker",
         "slug": None, "status": "failed", "status_label": "failed (exit 1)",
         "provider": {"role": "worker", "kind": "copilot-cli", "model": "",
                      "persona": "kit-builder", "automatic": True},
         "model": "",
         "persona": "kit-builder", "elapsed_s": 240.0,
         "silence_minutes": 0.0, "exit_code": 1,
         "resume_cmd": "copilot --resume 0199a-failed", "last_line": "gate failed",
         "outcome": "failed", "result_summary": "fixture failure",
         "result_errors": ["fixture failure"], "changed_files": [],
         "verification": None, "integration": None, "workspace": None,
         "timeout_seconds": 1800, "timed_out": False},
        {"run_id": "run-3", "kind": "finding", "finding": "finished worker",
         "slug": None, "status": "completed",
         "status_label": "implemented and independently verified",
         "provider": {"role": "worker", "kind": "copilot-cli", "model": "",
                      "persona": "kit-builder", "automatic": True},
         "model": "",
         "persona": "kit-builder", "elapsed_s": 720.0,
         "silence_minutes": 0.0, "exit_code": 0,
         "resume_cmd": "copilot --resume 0199a-done", "last_line": "check.py green",
         "outcome": "implemented", "result_summary": "fixture implementation",
         "result_errors": [], "changed_files": ["tools/example.py"],
         "verification": {"passed": True, "exit_code": 0, "tail": "GATE PASSED"},
         "integration": {"integrated": True}, "workspace": ".kit/runtime/dispatch/workspaces/run-3",
        "timeout_seconds": 1800, "timed_out": False},
    ]
    public_run_fields = (
        "run_id", "kind", "finding", "slug", "status", "status_label",
        "provider", "model", "persona", "elapsed_s", "silence_minutes",
        "exit_code", "resume_cmd", "outcome", "timeout_seconds", "timed_out",
    )
    runs = [
        {key: run.get(key) for key in public_run_fields}
        for run in runs
    ]
    try:
        plan = board.cockpit.plan_view(board.ROOT)
    except Exception:
        plan = {"status": "unavailable", "fingerprint": None,
                "verification": {"status": "not-run"}}
    retro_state = board.retro_due.state()
    retro_state.update({
        "unarchived": 11,
        "threshold": 10,
        "due": True,
        "archived": 3,
        "notes": ["slice-1.md", "slice-2.md"],
        "trigger_level": "routine",
        "immediate_consequences": [],
        "prompt_triggers": [],
        "warnings": [],
    })
    return {
        "ok": True,
        "schema": board.SCHEMA,
        "board": {"port": 0, "pid": 0, "started": "2026-08-19T00:00:00Z",
                  "schema": board.SCHEMA, "version": board.BOARD_VERSION},
        "retro_due": retro_state,
        "providers": {
            "analyzer": board._provider_view("analyzer"),
            "worker": board._provider_view("worker"),
        },
        "plan": plan,
        "verification": plan.get("verification", {}),
        "findings": findings,
        "queue_halted": False,
        "silence_minutes": 3.0,
        "runs": runs,
    }


class Handler(board.Handler):
    server_version = "mockboard/2.0"

    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def _send_raw(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        board._security_headers(self)
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method: str, path: str, body: dict) -> tuple[int, dict] | None:
        if method == "GET" and path == "/api/health":
            if SCENARIO == "nohealth":
                return board._error(500, "board is shutting down", "server_error")
            return 200, {"ok": True, "port": self.server.server_address[1],
                         "pid": 0, "started": "2026-08-19T00:00:00Z",
                         "instance_id": self.server.instance_id,
                         "repository_scope_id": self.server.repository_scope_id,
                         "schema": board.SCHEMA, "version": board.BOARD_VERSION}
        if method == "GET" and path == "/api/state":
            return 200, state_payload()
        if method == "GET" and path.startswith("/api/finding/"):
            slug = path.rsplit("/", 1)[-1]
            if SCENARIO == "broken":
                return board._error(500, "artifact unreadable", "server_error")
            item = _load_item(slug)
            if item is None:
                return board._error(404, f"no dispatch artifact for {slug!r}",
                                    "missing_artifact", slug=slug)
            return 200, {
                "ok": True,
                "finding": item,
                "stale": False if FIXTURE_ITEMS is not None else retro_queue.is_stale(item),
                "dispatchable": bool(item.get("dispatchable", False)),
                "dispatch_blockers": list(item.get("dispatch_blockers", [])),
                "evidence_snapshot": item.get("evidence_snapshot", ""),
                "comment": "",
                "prompt_preview": retro_queue.render_prompt(item, ""),
                "review": retro_queue.review_identity(item),
            }
        if method == "POST":
            if SCENARIO == "broken":
                return board._error(500, "dispatch failed: copilot not on PATH",
                                    "spawn_failed")
            return 200, {"ok": True, "finding": {"state": "queued"}}
        return super()._dispatch(method, path, body)

    def _handle(self, method: str) -> None:
        if method != "POST" or SCENARIO != "garbage":
            super()._handle(method)
            return
        rejected = board._validate_host(self) or board._validate_mutation(self)
        if rejected is not None:
            board._json(self, rejected[0], rejected[1])
            return
        _body, rejected = board._read_json_body(self)
        if rejected is not None:
            board._json(self, rejected[0], rejected[1])
            return
        self._send_raw(200, b"<html><body>Internal Server Error</body></html>",
                       "text/html")


def main() -> int:
    global FIXTURE_ITEMS, SCENARIO
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--scenario", default="healthy",
                    choices=["healthy", "broken", "garbage", "nohealth", "dead"])
    ap.add_argument(
        "--fixture-root",
        help="serve deterministic generated pages and fixture-items.json from this directory",
    )
    a = ap.parse_args()
    SCENARIO = a.scenario
    if a.fixture_root:
        fixture_root = Path(a.fixture_root).resolve(strict=True)
        if not fixture_root.is_dir():
            ap.error("--fixture-root must be a directory")
        for page in ("plan.html", "retro.html"):
            if not (fixture_root / page).is_file():
                ap.error(f"--fixture-root is missing {page}")
        try:
            fixture_items = json.loads(
                (fixture_root / "fixture-items.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            ap.error(f"fixture-items.json is unreadable: {exc}")
        if not isinstance(fixture_items, list) or not all(
                isinstance(item, dict) for item in fixture_items):
            ap.error("fixture-items.json must contain an array of objects")
        FIXTURE_ITEMS = fixture_items
        # board._serve_html is the production HTML/token path. Redirect only
        # its static root; the handler, headers and API contract stay real.
        board.ROOT = fixture_root
    if a.scenario == "dead":
        print("mock board: scenario 'dead', nothing will listen")
        return 0
    httpd = board.BoardHTTPServer(("127.0.0.1", a.port), Handler)
    print(f"mock board [{a.scenario}] on http://127.0.0.1:{a.port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
