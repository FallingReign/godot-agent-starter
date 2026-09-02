#!/usr/bin/env python3
"""Focused loopback tests for one exact kit-change review session."""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import sys
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from unittest import mock


TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))
import board  # noqa: E402


SESSION_A = "a" * 64
SESSION_B = "b" * 64
SESSION_NEW = "c" * 64
PLAN_A = "d" * 64
PLAN_B = "e" * 64
PLAN_NEW = "f" * 64
RESULT_A = "1" * 64


def _view(session_id: str, plan: str, status: str = "ready") -> dict:
    adoption = status == "adoption_required"
    return {
        "ok": status in {"ready", "complete", "adoption_required", "restored"},
        "session_id": session_id,
        "session_sha256": "2" * 64,
        "kit_change": {
            "mode": "upgrade",
            "status": status,
            "project": {"name": "Fixture", "path": "C:/Fixture"},
            "current_version": "0.2.0",
            "incoming_version": "0.3.0",
            "plan_sha256": plan,
            "counts": {
                "kit_files": 4,
                "shared_files": 2,
                "removed_files": 1,
                "game_files": 0,
            },
            "design": "Not part of this kit change",
            "existing_gaps": {
                "count": 7 if adoption else 0,
                "status": "checked" if adoption else "not_checked",
            },
            "decisions": [],
            "files": [],
            "recovery": "Previous state is saved and can be restored",
            "blockers": [],
            "detail": (
                "The kit works. Existing project cleanup remains."
                if adoption else "The exact kit change is ready for review."
            ),
            "result_sha256": RESULT_A if status in {"complete", "adoption_required"} else "",
            "plan_url": "",
        },
    }


class KitChangeBoard(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = TOOLS.parent / ".checklogs" / f"kit-change-board-{uuid.uuid4().hex}"
        self.runtime = self.scratch / ".kit" / "runtime"
        self.runtime.mkdir(parents=True)
        self.saved = {
            "state": board.STATE_FILE,
            "runtime": board._RUNTIME,
            "finding_states": board.finding_states,
            "retro_due": board.retro_due.state,
            "plan_view": board.cockpit.plan_view,
        }
        board.STATE_FILE = self.runtime / "board" / "state.json"
        board._RUNTIME = board.runtime_paths.RuntimePaths(self.scratch, self.runtime)
        board.finding_states = lambda silence=None: []
        board.retro_due.state = lambda: {"due": False}
        board.cockpit.plan_view = lambda _root: {"status": "unavailable", "verification": {}}

        self.states = {
            SESSION_A: _view(SESSION_A, PLAN_A),
            SESSION_B: _view(SESSION_B, PLAN_B),
            SESSION_NEW: _view(SESSION_NEW, PLAN_NEW),
        }

        def status(runtime: Path, session_id: str, *, plan_url: str = "") -> dict:
            self.assertEqual(self.runtime, runtime)
            if session_id not in self.states:
                raise board.kit_change_controller.KitChangeControllerError(
                    "file-unreadable", "private path is deliberately not public"
                )
            value = json.loads(json.dumps(self.states[session_id]))
            value["kit_change"]["plan_url"] = plan_url
            return value

        def apply(runtime: Path, session_id: str, digest: str) -> dict:
            self.assertEqual(self.runtime, runtime)
            if digest != self.states[session_id]["kit_change"]["plan_sha256"]:
                raise board.kit_change_controller.KitChangeControllerError(
                    "approval-mismatch", "stale"
                )
            self.states[session_id] = _view(session_id, digest, "adoption_required")
            return self.states[session_id]

        def restore(runtime: Path, session_id: str, digest: str) -> dict:
            self.assertEqual(self.runtime, runtime)
            if digest != RESULT_A:
                raise board.kit_change_controller.KitChangeControllerError(
                    "result-mismatch", "stale"
                )
            plan = self.states[session_id]["kit_change"]["plan_sha256"]
            self.states[session_id] = _view(session_id, plan, "restored")
            return self.states[session_id]

        def recover(runtime: Path, session_id: str) -> dict:
            self.assertEqual(self.runtime, runtime)
            plan = self.states[session_id]["kit_change"]["plan_sha256"]
            self.states[session_id] = _view(session_id, plan, "restored")
            return self.states[session_id]

        def reprepare(runtime: Path, session_id: str, choices: dict) -> dict:
            self.assertEqual(self.runtime, runtime)
            self.assertEqual(SESSION_A, session_id)
            self.assertEqual({"D1": "game"}, choices)
            return self.states[SESSION_NEW]

        self.status = mock.patch.object(
            board.kit_change_controller, "status", side_effect=status
        ).start()
        self.apply = mock.patch.object(
            board.kit_change_controller, "apply", side_effect=apply
        ).start()
        self.restore = mock.patch.object(
            board.kit_change_controller, "restore", side_effect=restore
        ).start()
        self.recover = mock.patch.object(
            board.kit_change_controller, "recover", side_effect=recover
        ).start()
        self.reprepare = mock.patch.object(
            board.kit_change_controller, "reprepare", side_effect=reprepare, create=True
        ).start()

        self.httpd = board.BoardHTTPServer(
            ("127.0.0.1", 0), board.Handler, capability_token="kit-change-test-token"
        )
        self.port = int(self.httpd.server_address[1])
        state = board.load_state()
        state.update({"port": self.port, "pid": 123, "runs": []})
        board.save_state(state)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)
        mock.patch.stopall()
        board.STATE_FILE = self.saved["state"]
        board._RUNTIME = self.saved["runtime"]
        board.finding_states = self.saved["finding_states"]
        board.retro_due.state = self.saved["retro_due"]
        board.cockpit.plan_view = self.saved["plan_view"]
        shutil.rmtree(self.scratch)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def get_json(self, path: str) -> tuple[int, dict]:
        request = urllib.request.Request(self.base + path.lstrip("/"))
        return self._json_response(request)

    def get_text(self, path: str) -> tuple[int, str]:
        with urllib.request.urlopen(self.base + path.lstrip("/"), timeout=10) as response:
            return response.status, response.read().decode("utf-8")

    def post(self, path: str, body: dict, *, token: str = "kit-change-test-token",
             origin: str | None = None) -> tuple[int, dict]:
        content = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Origin": self.base.rstrip("/") if origin is None else origin,
            "X-Kit-Board-Token": token,
        }
        request = urllib.request.Request(
            self.base + path.lstrip("/"), data=content, method="POST", headers=headers
        )
        return self._json_response(request)

    def _json_response(self, request: urllib.request.Request) -> tuple[int, dict]:
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def activate(self, session_id: str = SESSION_A) -> None:
        board.register_kit_change_session(session_id, base_url=self.base)

    def test_state_migration_and_registration_store_only_the_session_id(self) -> None:
        migrated, changed = board.migrate_state({"runs": []})
        self.assertTrue(changed)
        self.assertIsNone(migrated["kit_change_session"])

        self.activate()

        stored = json.loads(board.STATE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(SESSION_A, stored["kit_change_session"])
        self.assertNotIn("target", json.dumps(stored))
        self.assertNotIn("release", json.dumps(stored))
        self.status.assert_called()

    def test_ensure_registration_prints_the_exact_review_url(self) -> None:
        output = io.StringIO()
        argv = [
            "board.py", "--ensure", "--kit-change-session", SESSION_A, "--json"
        ]
        with mock.patch.object(sys, "argv", argv), mock.patch.object(
            board, "ensure_running", return_value=self.base
        ), contextlib.redirect_stdout(output):
            code = board.main()

        payload = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertTrue(payload["ok"])
        self.assertEqual(SESSION_A, payload["session_id"])
        self.assertEqual(
            self.base + f"kit-change.html?session={SESSION_A}", payload["review_url"]
        )

    def test_registration_refuses_a_second_lifecycle_action(self) -> None:
        stderr = io.StringIO()
        argv = [
            "board.py", "--ensure", "--status", "--kit-change-session", SESSION_A
        ]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as raised:
                board.main()
        self.assertEqual(2, raised.exception.code)
        self.assertIn("only lifecycle action", stderr.getvalue())
        self.status.assert_not_called()

    def test_direct_url_active_fallback_and_two_session_isolation(self) -> None:
        self.activate(SESSION_A)
        code, active_page = self.get_text("kit-change.html")
        self.assertEqual(200, code)
        self.assertIn(f'"session_id":"{SESSION_A}"', active_page)

        code, other_page = self.get_text(f"kit-change.html?session={SESSION_B}")
        self.assertEqual(200, code)
        self.assertIn(f'"session_id":"{SESSION_B}"', other_page)
        self.assertIn('String(next.session_id || "") === sessionId', other_page)

        code, state = self.get_json("api/state")
        self.assertEqual(200, code)
        self.assertEqual(SESSION_A, state["kit_change"]["session_id"])

    def test_malformed_unknown_and_unregistered_reviews_are_clear(self) -> None:
        code, payload = self.get_json("kit-change.html")
        self.assertEqual(404, code)
        self.assertEqual("kit_change_not_registered", payload["code"])
        code, payload = self.get_json("kit-change.html?session=bad")
        self.assertEqual(400, code)
        self.assertEqual("invalid_session", payload["code"])
        code, payload = self.get_json(f"kit-change.html?session={'9' * 64}")
        self.assertEqual(404, code)
        self.assertEqual("unknown_session", payload["code"])
        self.assertNotIn("private path", json.dumps(payload))

    def test_exact_ready_apply_reports_adoption_and_exact_problem_count(self) -> None:
        self.activate()
        code, payload = self.post("api/kit-change/apply", {
            "session_id": SESSION_A,
            "plan_sha256": PLAN_A,
            "choices": {},
        })
        self.assertEqual(200, code)
        self.assertEqual("adoption_required", payload["kit_change"]["status"])
        self.assertEqual(7, payload["kit_change"]["existing_gaps"]["count"])
        self.assertEqual(
            "The kit works. 7 existing project problems remain.",
            payload["kit_change"]["detail"],
        )
        self.apply.assert_called_once_with(self.runtime, SESSION_A, PLAN_A)

        code, page = self.get_text(f"kit-change.html?session={SESSION_A}")
        self.assertEqual(200, code)
        self.assertIn("Kit works; project cleanup remains", page)
        self.assertIn(">7</dd>", page)
        self.assertIn("Existing problems", page)
        self.assertIn("Restore previous state", page)
        self.assertIn("Cleanup remains", page)

    def test_d1_creates_a_new_review_and_never_applies(self) -> None:
        blocked = _view(SESSION_A, PLAN_A, "ready")
        blocked["kit_change"].update({
            "status": "blocked",
            "decisions": [{
                "id": "D1",
                "question": "Which folder contains the game?",
                "selected": "",
                "choices": [{"value": "game", "label": "game"}],
            }],
            "blockers": ["[game-root-ambiguous] Choose the game folder."],
        })
        self.states[SESSION_A] = blocked
        self.activate()
        state_code, active = self.get_json("api/state")
        self.assertEqual(200, state_code)
        self.assertEqual("needs_decision", active["kit_change"]["status"])

        code, payload = self.post("api/kit-change/apply", {
            "session_id": SESSION_A,
            "plan_sha256": PLAN_A,
            "choices": {"D1": "game"},
        })

        self.assertEqual(200, code)
        self.assertTrue(payload["reprepared"])
        self.assertEqual(
            self.base + f"kit-change.html?session={SESSION_NEW}", payload["review_url"]
        )
        self.reprepare.assert_called_once_with(self.runtime, SESSION_A, {"D1": "game"})
        self.apply.assert_not_called()
        self.assertEqual(SESSION_NEW, board.load_state()["kit_change_session"])

    def test_restore_requires_the_exact_session_result_pair(self) -> None:
        self.states[SESSION_A] = _view(SESSION_A, PLAN_A, "adoption_required")
        self.activate()
        code, stale = self.post("api/kit-change/restore", {
            "session_id": SESSION_A,
            "result_sha256": "3" * 64,
        })
        self.assertEqual(409, code)
        self.assertEqual("stale_review", stale["code"])

        code, restored = self.post("api/kit-change/restore", {
            "session_id": SESSION_A,
            "result_sha256": RESULT_A,
        })
        self.assertEqual(200, code)
        self.assertEqual("restored", restored["kit_change"]["status"])

    def test_retry_recovery_uses_the_same_session_and_no_result_digest(self) -> None:
        self.states[SESSION_A] = _view(SESSION_A, PLAN_A, "recovery_required")
        self.states[SESSION_A]["kit_change"].update({
            "detail": "Restore is still required: file is busy.",
            "recovery": "Previous state is saved; recovery still needs to finish",
            "result_sha256": "",
        })
        self.activate()

        code, page = self.get_text(f"kit-change.html?session={SESSION_A}")
        self.assertEqual(200, code)
        self.assertIn("Recovery needed", page)
        self.assertIn("Retry recovery", page)
        code, recovered = self.post("api/kit-change/recover", {
            "session_id": SESSION_A,
        })

        self.assertEqual(200, code)
        self.assertEqual("restored", recovered["kit_change"]["status"])
        self.recover.assert_called_once_with(self.runtime, SESSION_A)
        self.restore.assert_not_called()

    def test_retry_manual_restore_keeps_the_exact_result_digest(self) -> None:
        self.states[SESSION_A] = _view(SESSION_A, PLAN_A, "recovery_required")
        self.states[SESSION_A]["kit_change"]["result_sha256"] = RESULT_A
        self.activate()

        code, page = self.get_text(f"kit-change.html?session={SESSION_A}")
        self.assertEqual(200, code)
        self.assertIn("Retry restore", page)
        code, wrong_action = self.post("api/kit-change/recover", {
            "session_id": SESSION_A,
        })
        self.assertEqual(409, code)
        self.assertEqual("restore_retry_required", wrong_action["code"])
        self.recover.assert_not_called()

        code, restored = self.post("api/kit-change/restore", {
            "session_id": SESSION_A,
            "result_sha256": RESULT_A,
        })
        self.assertEqual(200, code)
        self.assertEqual("restored", restored["kit_change"]["status"])
        self.restore.assert_called_once_with(self.runtime, SESSION_A, RESULT_A)

    def test_retry_recovery_requires_exact_body_capability_and_origin(self) -> None:
        self.states[SESSION_A] = _view(SESSION_A, PLAN_A, "recovery_required")
        self.activate()
        code, payload = self.post("api/kit-change/recover", {
            "session_id": SESSION_A,
            "result_sha256": "",
        })
        self.assertEqual(400, code)
        self.assertEqual("invalid_request", payload["code"])
        code, payload = self.post(
            "api/kit-change/recover", {"session_id": SESSION_A}, token="wrong"
        )
        self.assertEqual(403, code)
        self.assertEqual("invalid_capability", payload["code"])
        code, payload = self.post(
            "api/kit-change/recover",
            {"session_id": SESSION_A},
            origin="https://attacker.invalid",
        )
        self.assertEqual(403, code)
        self.assertEqual("invalid_origin", payload["code"])
        self.recover.assert_not_called()

    def test_exact_body_capability_origin_and_stale_digest_fail_closed(self) -> None:
        self.activate()
        good = {"session_id": SESSION_A, "plan_sha256": PLAN_A, "choices": {}}
        code, payload = self.post("api/kit-change/apply", {
            "session_id": SESSION_A, "plan_sha256": PLAN_A
        })
        self.assertEqual(400, code)
        self.assertEqual("invalid_request", payload["code"])
        code, payload = self.post("api/kit-change/apply", good, token="wrong")
        self.assertEqual(403, code)
        self.assertEqual("invalid_capability", payload["code"])
        code, payload = self.post(
            "api/kit-change/apply", good, origin="https://attacker.invalid"
        )
        self.assertEqual(403, code)
        self.assertEqual("invalid_origin", payload["code"])
        code, payload = self.post("api/kit-change/apply", {
            **good, "plan_sha256": "4" * 64
        })
        self.assertEqual(409, code)
        self.assertEqual("stale_review", payload["code"])
        self.apply.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
