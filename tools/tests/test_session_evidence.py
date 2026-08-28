#!/usr/bin/env python3
"""Production-shaped tests for repository-scoped agent evidence snapshots."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import session_digest    # noqa: E402
import session_evidence  # noqa: E402


def _scratch() -> Path:
    path = TOOLS.parent / f".session-evidence-test-{uuid.uuid4().hex}"
    path.mkdir()
    return path


def _remove_scratch(path: Path) -> None:
    resolved = path.resolve()
    if resolved.parent != TOOLS.parent.resolve():
        raise AssertionError(f"refusing to remove scratch outside repository: {resolved}")
    if not resolved.name.startswith(".session-evidence-test-"):
        raise AssertionError(f"refusing to remove unexpected scratch path: {resolved}")
    shutil.rmtree(resolved)


def _event(event_type: str, **data) -> str:
    return json.dumps({"type": event_type, "data": data}, separators=(",", ":"))


def _write_session(root: Path, session_id: str, cwd: str, events: bytes,
                   *, updated: str = "2026-08-24T01:02:03Z",
                   log_name: str = "events.jsonl") -> dict:
    directory = root / session_id
    directory.mkdir(parents=True)
    workspace = directory / "workspace.yaml"
    workspace.write_text(
        f"id: {session_id}\n"
        f"cwd: '{cwd}'\n"
        "client_name: github/cli\n"
        "api_token: workspace-secret\n"
        "created_at: 2026-08-24T01:00:00Z\n"
        f"updated_at: {updated}\n",
        encoding="utf-8",
    )
    log = directory / log_name
    log.write_bytes(events)
    return {"directory": directory, "workspace": workspace, "log": log}


def _codex_record(record_type: str, payload: dict, timestamp: str) -> str:
    return json.dumps({"timestamp": timestamp, "type": record_type, "payload": payload},
                      separators=(",", ":"))


def _write_codex_session(root: Path, session_id: str, cwd: str,
                         records: list[str], *, archived: bool = False,
                         parent: str = "",
                         timestamp: str = "2026-08-24T01:00:00Z") -> Path:
    directory = root if archived else root / "2026" / "08" / "24"
    directory.mkdir(parents=True, exist_ok=True)
    log = directory / f"rollout-2026-08-24T01-00-00-{session_id}.jsonl"
    meta = _codex_record("session_meta", {
        "id": session_id,
        "timestamp": timestamp,
        "cwd": cwd,
        "parent_thread_id": parent,
        "environment": {"SECRET": "env-secret"},
    }, timestamp)
    log.write_text("\n".join([meta, *records]) + "\n", encoding="utf-8")
    return log


class TestRepositoryScopedDiscovery(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = _scratch()
        self.root = self.scratch / "session-state"
        self.root.mkdir()
        self.repo = Path("C:/Development/Godot/Test Game")

    def tearDown(self) -> None:
        _remove_scratch(self.scratch)

    def test_direct_workspace_scan_is_repo_scoped_nonrecursive_and_deterministic(self) -> None:
        second = _write_session(
            self.root, "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "c:\\development\\godot\\test game\\", b"{}\n")
        first = _write_session(
            self.root, "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "C:/DEVELOPMENT/GODOT/TEST GAME", b"{}\n")
        _write_session(
            self.root, "cccccccc-cccc-cccc-cccc-cccccccccccc",
            "C:/Development/Godot/Another Game", b"{}\n")

        nested = self.root / "container" / "nested-session"
        nested.mkdir(parents=True)
        (nested / "workspace.yaml").write_text(
            f"id: nested\ncwd: '{self.repo}'\n", encoding="utf-8")
        (nested / "events.jsonl").write_text("{}\n", encoding="utf-8")

        # A larger fallback log must not displace Copilot's declared event stream.
        (second["directory"] / "other.jsonl").write_bytes(b"{}\n" * 100)

        with mock.patch.object(Path, "rglob", side_effect=AssertionError(
                "discovery must never recurse")):
            found = session_digest.discover(self.repo, [self.root])

        self.assertEqual([s["id"] for s in found], [
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
        ])
        self.assertEqual(found[0]["workspace"], first["workspace"])
        self.assertEqual(found[1]["log"], second["log"])
        self.assertTrue(all(s["kind"] == "copilot" for s in found))

    def test_fallback_log_selection_is_size_then_name_and_stays_direct(self) -> None:
        directory = self.root / "fallback"
        directory.mkdir()
        (directory / "workspace.yaml").write_text(
            f"id: fallback\ncwd: '{self.repo}'\nupdated_at: 2026-08-24Z\n",
            encoding="utf-8")
        (directory / "z.jsonl").write_bytes(b"a" * 20)
        expected = directory / "a.jsonl"
        expected.write_bytes(b"b" * 20)
        (directory / "nested").mkdir()
        (directory / "nested" / "events.jsonl").write_bytes(b"c" * 1000)

        found = session_digest.discover(self.repo, [self.root])

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["log"], expected)

    def test_posix_repository_identity_remains_case_sensitive(self) -> None:
        wanted = _write_session(
            self.root, "upper", "/srv/projects/Game", b"{}\n"
        )
        _write_session(self.root, "lower", "/srv/projects/game", b"{}\n")

        found = session_digest.discover("/srv/projects/Game", [self.root])

        self.assertEqual(["upper"], [session["id"] for session in found])
        self.assertEqual(wanted["log"], found[0]["log"])

    def test_copilot_scope_rejects_missing_empty_and_relative_cwd(self) -> None:
        actual_repo = TOOLS.parent.resolve()
        _write_session(self.root, "valid", str(actual_repo), b"{}\n")
        _write_session(self.root, "empty", "", b"{}\n")
        _write_session(self.root, "dot", ".", b"{}\n")
        _write_session(self.root, "relative", "projects/game", b"{}\n")
        _write_session(self.root, "drive-relative", "C:projects\\game", b"{}\n")
        missing = self.root / "missing"
        missing.mkdir()
        (missing / "workspace.yaml").write_text(
            "id: missing\nupdated_at: 2026-08-24Z\n", encoding="utf-8")
        (missing / "events.jsonl").write_text("{}\n", encoding="utf-8")

        found = session_digest.discover(actual_repo, [self.root])

        self.assertEqual(["valid"], [session["id"] for session in found])

    def test_session_limit_fails_closed_unless_the_cut_is_explicit(self) -> None:
        _write_session(
            self.root, "older", str(self.repo), b"{}\n",
            updated="2026-08-24T01:00:00Z",
        )
        _write_session(
            self.root, "newer", str(self.repo), b"{}\n",
            updated="2026-08-24T02:00:00Z",
        )

        with mock.patch.object(session_digest, "MAX_DISCOVERED_SESSIONS", 1):
            with self.assertRaisesRegex(
                session_digest.SessionIngestionError,
                "2 matching sessions.*no sessions were silently discarded",
            ):
                session_digest.discover(self.repo, [self.root])
            selected = session_digest.discover(
                self.repo, [self.root], limit=1
            )

        self.assertEqual(["newer"], [session["id"] for session in selected])


class TestCaptureAndPrivacy(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = _scratch()
        self.root = self.scratch / "session-state"
        self.root.mkdir()
        self.repo = Path("C:/Development/Godot/Test Game")

    def tearDown(self) -> None:
        _remove_scratch(self.scratch)

    def _captured_digest(self) -> tuple[dict, dict]:
        lines = [
            _event("user.message", content="The ordering still hides the decision."),
            _event("assistant.message", model="claude-haiku-4.5",
                   content="assistant-secret",
                   usage={"totalAiu": 1.25}),
        ]
        for _ in range(3):
            lines.append(_event(
                "tool.execution", name="shell",
                arguments={
                    "command": "python tools/check_secret.py --token command-secret",
                },
                output="tool-output-secret",
            ))
        for command in ("kit verify --stage shape", r".\kit verify --stage shape"):
            lines.append(_event(
                "tool.execution", name="shell",
                arguments={"command": command},
                output="GATE PASSED",
            ))
        lines.extend([
            "not-json",
            _event("user.message", content="x" * (session_digest.MAX_HUMAN_CHARS + 50)),
        ])
        raw = ("\n".join(lines) + "\n" + '{"type":').encode("utf-8")
        written = _write_session(
            self.root, "12345678-1234-1234-1234-123456789abc",
            str(self.repo), raw)
        session = session_digest.discover(self.repo, [self.root])[0]
        digest = session_digest.digest_one(session, 7)
        return digest, written

    def test_digest_has_hash_provenance_and_explicit_corruption_truncation_warnings(self) -> None:
        digest, written = self._captured_digest()

        self.assertEqual(digest["sources"]["events"]["sha256"],
                         hashlib.sha256(written["log"].read_bytes()).hexdigest())
        self.assertEqual(digest["sources"]["workspace"]["sha256"],
                         hashlib.sha256(written["workspace"].read_bytes()).hexdigest())
        self.assertEqual(digest["loops"], {
            "kit verify": 2,
        })
        self.assertEqual(len(digest["human"][1]), session_digest.MAX_HUMAN_CHARS)
        warning_codes = [warning["code"] for warning in digest["warnings"]]
        self.assertEqual(warning_codes, [
            "invalid_json_record",
            "truncated_json_record",
            "human_message_truncated",
        ])

    def test_manifest_copies_reduced_evidence_not_raw_secrets(self) -> None:
        digest, _written = self._captured_digest()

        manifest = session_evidence.build_manifest(self.repo, [digest])
        encoded = session_evidence.canonical_bytes(manifest).decode("utf-8")

        self.assertIn("The ordering still hides the decision.", encoded)
        self.assertNotIn("python tools/check_secret.py", encoded)
        self.assertNotIn("workspace-secret", encoded)
        self.assertNotIn("command-secret", encoded)
        self.assertNotIn("tool-output-secret", encoded)
        self.assertNotIn("assistant-secret", encoded)
        message = manifest["sessions"][0]["evidence"]["human_messages"][0]
        self.assertEqual(
            message["citation"],
            "12345678-1234-1234-1234-123456789abc:H1",
        )
        self.assertEqual(manifest["schema"], 2)
        self.assertEqual(manifest["kind"], "agent-session-evidence")
        self.assertEqual(manifest["capture_window"], {
            "mode": "complete-bounded",
            "since": None,
            "limit": None,
            "captured_sessions": 1,
        })
        self.assertEqual(len(manifest["repository"]["scope_id"]), 64)
        usage = manifest["sessions"][0]["evidence"]["usage"]
        self.assertEqual(usage["metrics"], [{"name": "aiu", "value": 1.25}])
        self.assertIsNone(usage["monetary_cost"])
        self.assertNotIn("cost", manifest["sessions"][0]["evidence"])

    def test_explicit_capture_window_changes_content_addressed_manifest(self) -> None:
        digest, _written = self._captured_digest()
        complete = session_evidence.build_manifest(self.repo, [digest])
        selected = session_evidence.build_manifest(
            self.repo, [digest], since="2026-08-24T00:00:00Z", limit=1
        )

        self.assertNotEqual(
            session_evidence.canonical_bytes(complete),
            session_evidence.canonical_bytes(selected),
        )
        self.assertEqual(selected["capture_window"], {
            "mode": "explicit",
            "since": "2026-08-24T00:00:00Z",
            "limit": 1,
            "captured_sessions": 1,
        })

    def test_json_array_event_file_is_supported(self) -> None:
        events = json.dumps([
            {"type": "user.message", "data": {"content": "Array-backed event."}},
        ]).encode("utf-8")
        _write_session(
            self.root, "array-session", str(self.repo), events,
            log_name="events.json")

        digest = session_digest.digest_one(
            session_digest.discover(self.repo, [self.root])[0], 1)

        self.assertEqual(digest["human"], ["Array-backed event."])
        self.assertEqual(digest["warnings"], [])

    def test_redaction_covers_auth_cli_env_and_spaced_windows_paths(self) -> None:
        unsafe = "\n".join([
            "Authorization: Bearer opaque-token-value",
            "Authorization: Basic dXNlcjpwYXNzd29yZA==",
            "Bearer standalone-bearer-value",
            "Basic c2VjcmV0OnZhbHVl",
            "--token command-secret",
            '--api-key="quoted command secret"',
            "TOKEN=bare-env-secret",
            "$env:GITHUB_TOKEN = 'powershell env secret'",
            'setx CLIENT_SECRET "setx secret value"',
            "C:\\Users\\Jane Doe\\Project Name\\private file.txt",
            r"\\server\share\User Data\private file.txt",
            r"\\?\C:\Users\Jane Doe\extended private.txt",
            r"\\?\UNC\server\share\Extended Data\private file.txt",
            '"C:\\Users\\Quoted User\\Project Name\\secret.txt"',
        ])

        safe, count = session_evidence.redact_text(unsafe)

        self.assertGreaterEqual(count, 14)
        for leaked in (
            "opaque-token-value", "dXNlcjpwYXNzd29yZA", "standalone-bearer-value",
            "c2VjcmV0OnZhbHVl", "command-secret", "quoted command secret",
            "bare-env-secret", "powershell env secret", "setx secret value",
            "Jane Doe", "Project Name", "User Data", "extended private",
            "Extended Data", "Quoted User", "private file.txt",
        ):
            self.assertNotIn(leaked, safe)
        self.assertNotIn("\\\\server", safe)
        self.assertNotIn("\\\\?\\", safe)
        self.assertIn("authorization=<redacted>", safe.lower())
        self.assertIn("<local-path>", safe)

    def test_copilot_context_blocks_are_removed_without_losing_request(self) -> None:
        events = "\n".join([
            _event("user.message", content=(
                "Keep the genuine prefix.\n"
                "<environment_context><cwd>C:/private/path</cwd></environment_context>\n"
                "Keep the genuine suffix."
            )),
            _event("user.message", content=(
                "<app-context source=\"ambient\">not-human</app-context>\n"
                "## My request:\nRetain this request."
            )),
            _event("user.message", content=(
                "Discard the injected tail.\n"
                "<codex_internal_context source=\"goal\">private-tail"
            )),
            _event("user.message", content=(
                "<environment_context>pure injection</environment_context>"
            )),
        ])
        _write_session(
            self.root, "context-session", str(self.repo),
            (events + "\n").encode("utf-8"),
        )

        digest = session_digest.digest_one(
            session_digest.discover(self.repo, [self.root])[0], 1)

        self.assertEqual(digest["human"], [
            "Keep the genuine prefix. Keep the genuine suffix.",
            "## My request: Retain this request.",
            "Discard the injected tail.",
        ])
        encoded = json.dumps(digest)
        self.assertNotIn("private/path", encoded)
        self.assertNotIn("not-human", encoded)
        self.assertNotIn("private-tail", encoded)
        self.assertNotIn("pure injection", encoded)

    def test_copilot_workspace_is_reauthenticated_after_event_capture(self) -> None:
        written = _write_session(
            self.root, "scope-swap", str(self.repo),
            (_event("user.message", content="must-not-survive") + "\n").encode("utf-8"),
        )
        session = session_digest.discover(self.repo, [self.root])[0]
        original = session_evidence.capture_file

        def capture_then_swap(path: Path, **kwargs):
            captured = original(path, **kwargs)
            if path == written["log"]:
                written["workspace"].write_text(
                    "id: scope-swap\ncwd: 'C:/Development/Godot/Foreign'\n",
                    encoding="utf-8",
                )
            return captured

        with mock.patch.object(
            session_evidence, "capture_file", side_effect=capture_then_swap
        ):
            digest = session_digest.digest_one(session, 1)

        self.assertEqual(digest["error"], "copilot_scope_changed")
        self.assertNotIn("human", digest)
        self.assertIn(
            "copilot_scope_changed",
            [warning["code"] for warning in digest["warnings"]],
        )

    def test_oversized_event_source_fails_before_partial_parse_with_exact_limit(self) -> None:
        written = _write_session(
            self.root, "oversized", str(self.repo),
            (_event("user.message", content="must-not-survive") + "\n").encode("utf-8"),
        )
        session = session_digest.discover(self.repo, [self.root])[0]
        with mock.patch.object(session_digest, "MAX_EVENT_BYTES", 8):
            digest = session_digest.digest_one(session, 1)

        self.assertEqual(digest["error"], "events_byte_limit_exceeded")
        warning = next(
            item for item in digest["warnings"]
            if item["code"] == "events_byte_limit_exceeded"
        )
        self.assertEqual(warning["max_bytes"], 8)
        self.assertEqual(warning["observed_bytes"], written["log"].stat().st_size)
        self.assertNotIn("human", digest)

    def test_event_record_limit_fails_without_a_partial_digest(self) -> None:
        events = "\n".join([
            _event("user.message", content="first"),
            _event("user.message", content="second"),
        ]) + "\n"
        _write_session(
            self.root, "many-records", str(self.repo), events.encode("utf-8")
        )
        session = session_digest.discover(self.repo, [self.root])[0]
        with mock.patch.object(session_digest, "MAX_EVENT_RECORDS", 1):
            digest = session_digest.digest_one(session, 1)

        self.assertEqual(digest["error"], "event_record_limit_exceeded")
        warning = next(
            item for item in digest["warnings"]
            if item["code"] == "event_record_limit_exceeded"
        )
        self.assertEqual(warning["max_records"], 1)
        self.assertEqual(warning["observed_records"], 2)
        self.assertNotIn("human", digest)
        rendered = session_digest.render([digest])
        self.assertIn("FAILED event_record_limit_exceeded", rendered)
        self.assertIn("max_records=1", rendered)


class TestCodexCaptureAndPrivacy(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = _scratch()
        self.active = self.scratch / "sessions"
        self.archive = self.scratch / "archived_sessions"
        self.repo = Path("C:/Development/Godot/Test Game")

    def tearDown(self) -> None:
        _remove_scratch(self.scratch)

    def _records(self) -> list[str]:
        records = [
            _codex_record("response_item", {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "Codex correction."}],
            }, "2026-08-24T01:01:00Z"),
            _codex_record("response_item", {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": "assistant-secret"}],
            }, "2026-08-24T01:01:01Z"),
            _codex_record("event_msg", {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 60, "output_tokens": 40, "total_tokens": 100,
                }}, "rate_limits": {"credits": "credit-secret"},
            }, "2026-08-24T01:01:02Z"),
            _codex_record("turn_context", {
                "model": "gpt-5.6-sol", "reasoning": "hidden-reasoning-secret",
            }, "2026-08-24T01:01:03Z"),
        ]
        command = json.dumps({"cmd": "kit verify --stage schema --token command-secret"})
        for _ in range(2):
            records.append(_codex_record("response_item", {
                "type": "function_call", "name": "exec_command", "arguments": command,
            }, "2026-08-24T01:01:04Z"))
        patch = "*** Update File: C:/private/project/thing.py\ncommand-secret\n"
        for _ in range(3):
            records.append(_codex_record("response_item", {
                "type": "custom_tool_call", "name": "apply_patch", "input": patch,
            }, "2026-08-24T01:01:05Z"))
        records.extend([
            _codex_record("response_item", {
                "type": "function_call_output",
                "output": "FAIL schema\nGATE PASSED\ntool-output-secret",
            }, "2026-08-24T01:01:06Z"),
            _codex_record("event_msg", {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 150, "cached_input_tokens": 25,
                    "output_tokens": 100, "reasoning_output_tokens": 20,
                    "total_tokens": 250,
                }},
            }, "2026-08-24T01:02:00Z"),
            _codex_record("event_msg", {
                "type": "user_message", "message": "not-a-human-turn-secret",
            }, "2026-08-24T01:02:01Z"),
        ])
        return records

    def test_active_and_archived_sessions_use_first_record_repository_scope(self) -> None:
        active_log = _write_codex_session(
            self.active, "codex-active", str(self.repo), self._records())
        archived_log = _write_codex_session(
            self.archive, "codex-archive", str(self.repo), [], archived=True)
        _write_codex_session(
            self.active, "foreign", "C:/Development/Godot/Other", self._records())
        found = session_digest.discover(self.repo, [self.active, self.archive])

        self.assertEqual([item["id"] for item in found],
                         ["codex-active", "codex-archive"])
        self.assertEqual({item["log"] for item in found}, {active_log, archived_log})
        self.assertTrue(all(item["provider"] == "codex" for item in found))

    def test_codex_scope_rejects_missing_empty_and_relative_meta_cwd(self) -> None:
        actual_repo = TOOLS.parent.resolve()
        _write_codex_session(self.active, "valid", str(actual_repo), [])
        _write_codex_session(self.active, "empty", "", [])
        _write_codex_session(self.active, "dot", ".", [])
        _write_codex_session(self.active, "relative", "projects/game", [])
        _write_codex_session(self.active, "drive-relative", "C:projects\\game", [])

        directory = self.active / "2026" / "08" / "24"
        missing = directory / "rollout-2026-08-24T01-00-00-missing.jsonl"
        missing.write_text(_codex_record("session_meta", {
            "id": "missing",
            "timestamp": "2026-08-24T01:00:00Z",
        }, "2026-08-24T01:00:00Z") + "\n", encoding="utf-8")

        found = session_digest.discover(actual_repo, [self.active])

        self.assertEqual(["valid"], [session["id"] for session in found])

    def test_codex_capture_reauthenticates_scope_after_discovery(self) -> None:
        log = _write_codex_session(
            self.active, "replaced", str(self.repo), self._records())
        session = session_digest.discover(self.repo, [self.active])[0]
        log.write_text(_codex_record("response_item", {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": "must-not-survive"}],
        }, "2026-08-24T01:01:00Z") + "\n", encoding="utf-8")

        digest = session_digest.digest_one(session, 1)

        self.assertEqual(digest["error"], "codex_scope_changed")
        self.assertNotIn("human", digest)
        self.assertNotIn("turns", digest)
        self.assertIn("codex_scope_changed",
                      [warning["code"] for warning in digest["warnings"]])

    def test_codex_capture_rejects_changed_session_identity_in_matching_repo(self) -> None:
        log = _write_codex_session(
            self.active, "discovered", str(self.repo), self._records())
        session = session_digest.discover(self.repo, [self.active])[0]
        replacement = [
            _codex_record("session_meta", {
                "id": "different-session",
                "cwd": str(self.repo),
                "timestamp": "2026-08-24T01:00:00Z",
            }, "2026-08-24T01:00:00Z"),
            _codex_record("response_item", {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "must-not-survive"}],
            }, "2026-08-24T01:01:00Z"),
        ]
        log.write_text("\n".join(replacement) + "\n", encoding="utf-8")

        digest = session_digest.digest_one(session, 1)

        self.assertEqual(digest["error"], "codex_scope_changed")
        self.assertNotIn("human", digest)

    def test_codex_capture_rejects_concatenated_second_session_metadata(self) -> None:
        log = _write_codex_session(
            self.active, "expected", str(self.repo), self._records())
        session = session_digest.discover(self.repo, [self.active])[0]
        with log.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(_codex_record("session_meta", {
                "id": "other-session",
                "cwd": str(self.repo),
                "timestamp": "2026-08-24T02:00:00Z",
            }, "2026-08-24T02:00:00Z") + "\n")
            stream.write(_codex_record("response_item", {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": "must-not-survive"}],
            }, "2026-08-24T02:01:00Z") + "\n")

        digest = session_digest.digest_one(session, 1)

        self.assertEqual(digest["error"], "codex_scope_changed")
        self.assertNotIn("human", digest)

    def test_codex_context_blocks_are_removed_without_losing_request(self) -> None:
        records = [
            _codex_record("response_item", {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": (
                    "Codex genuine request.\n"
                    "<codex_internal_context source=\"goal\">hidden goal"
                    "</codex_internal_context>\n"
                    "Codex genuine correction."
                )}],
            }, "2026-08-24T01:01:00Z"),
            _codex_record("response_item", {
                "type": "message", "role": "user",
                "content": [{"type": "input_text", "text": (
                    "<in-app-browser-context>ambient browser"
                    "</in-app-browser-context>\n"
                    "## My request:\nKeep this too."
                )}],
            }, "2026-08-24T01:01:01Z"),
        ]
        _write_codex_session(self.active, "context", str(self.repo), records)

        digest = session_digest.digest_one(
            session_digest.discover(self.repo, [self.active])[0], 1)

        self.assertEqual(digest["human"], [
            "Codex genuine request. Codex genuine correction.",
            "## My request: Keep this too.",
        ])
        self.assertNotIn("hidden goal", json.dumps(digest))
        self.assertNotIn("ambient browser", json.dumps(digest))

    def test_resumed_older_session_is_recent_by_rollout_activity_without_body_parse(
            self) -> None:
        oldest = _write_codex_session(
            self.active, "oldest", str(self.repo), self._records(),
            timestamp="2026-01-01T00:00:00Z")
        middle = _write_codex_session(
            self.active, "middle", str(self.repo), self._records(),
            timestamp="2026-08-26T00:00:00Z")
        resumed = _write_codex_session(
            self.active, "resumed-old-task", str(self.repo), self._records(),
            timestamp="2026-01-02T00:00:00Z")
        base = 1_800_000_000
        os.utime(oldest, ns=(base * 1_000_000_000, base * 1_000_000_000))
        os.utime(middle, ns=((base + 1) * 1_000_000_000,
                             (base + 1) * 1_000_000_000))
        os.utime(resumed, ns=((base + 2) * 1_000_000_000,
                              (base + 2) * 1_000_000_000))

        with mock.patch.object(
                session_digest, "_parse_records",
                side_effect=AssertionError("discovery must not parse rollout bodies")):
            found = session_digest.discover(self.repo, [self.active])

        self.assertEqual([item["id"] for item in found], [
            "oldest", "middle", "resumed-old-task",
        ])
        self.assertEqual([item["id"] for item in found[-2:]], [
            "middle", "resumed-old-task",
        ])

    def test_duplicate_active_and_archived_session_uses_latest_activity(self) -> None:
        archived = _write_codex_session(
            self.archive, "same-session", str(self.repo), [], archived=True,
            timestamp="2026-08-26T00:00:00Z",
        )
        active = _write_codex_session(
            self.active, "same-session", str(self.repo), self._records(),
            timestamp="2026-01-01T00:00:00Z",
        )
        base = 1_800_000_000
        os.utime(archived, ns=(base * 1_000_000_000, base * 1_000_000_000))
        os.utime(active, ns=((base + 1) * 1_000_000_000,
                             (base + 1) * 1_000_000_000))

        found = session_digest.discover(self.repo, [self.active, self.archive])

        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["log"], active)
        self.assertIn("/sessions/", found[0]["locator"])

    def test_cumulative_tokens_are_not_summed_and_private_bodies_are_discarded(self) -> None:
        _write_codex_session(self.active, "codex-active", str(self.repo), self._records())
        digest = session_digest.digest_one(
            session_digest.discover(self.repo, [self.active])[0], 1)
        metrics = {item["name"]: item["value"]
                   for item in digest["usage"]["metrics"]}

        self.assertEqual(metrics["total_tokens"], 250)
        self.assertEqual(metrics["input_tokens"], 150)
        self.assertNotEqual(metrics["total_tokens"], 350)
        self.assertEqual(digest["human"], ["Codex correction."])
        self.assertEqual(digest["loops"], {"kit verify": 2})
        self.assertEqual(digest["rewrites"], {"thing.py": 3})
        self.assertEqual(digest["gate_fails"], {"schema": 1})
        self.assertEqual(digest["gate_passes"], 1)

        manifest = session_evidence.build_manifest(self.repo, [digest])
        encoded = session_evidence.canonical_bytes(manifest).decode("utf-8")
        self.assertIn("Codex correction.", encoded)
        self.assertIn("thing.py", encoded)
        for secret in ("assistant-secret", "tool-output-secret", "command-secret",
                       "env-secret", "credit-secret", "hidden-reasoning-secret",
                       "not-a-human-turn-secret", "C:/private/project"):
            self.assertNotIn(secret, encoded)
        usage = manifest["sessions"][0]["evidence"]["usage"]
        self.assertIsNone(usage["monetary_cost"])
        self.assertFalse(any(item["name"] == "aiu" for item in usage["metrics"]))

    def test_child_session_keeps_linkage_but_omits_inherited_user_messages(self) -> None:
        _write_codex_session(
            self.active, "codex-child", str(self.repo), self._records(), parent="parent-id")
        digest = session_digest.digest_one(
            session_digest.discover(self.repo, [self.active])[0], 1)
        manifest = session_evidence.build_manifest(self.repo, [digest])
        session = manifest["sessions"][0]

        self.assertEqual(session["parent_session_id"], "parent-id")
        self.assertEqual(session["evidence"]["human_messages"], [])
        self.assertIn("child_session_human_messages_omitted",
                      [warning["code"] for warning in session["warnings"]])


class TestContentAddressedPersistence(unittest.TestCase):
    def setUp(self) -> None:
        self.scratch = _scratch()
        self.output = self.scratch / "evidence"
        self.repo = Path("C:/Development/Godot/Test Game")
        self.digest_a = {
            "index": 99,
            "id": "session-a",
            "started": "2026-08-24T01:00:00Z",
            "updated": "2026-08-24T01:01:00Z",
            "turns": 1,
            "human": ["A correction."],
            "sources": {"events": {"path": "source/a/events.jsonl",
                                    "sha256": "a" * 64, "bytes": 10}},
            "warnings": [],
        }
        self.digest_b = {
            "index": 1,
            "id": "session-b",
            "started": "2026-08-24T02:00:00Z",
            "updated": "2026-08-24T02:01:00Z",
            "turns": 2,
            "human": ["Another correction."],
            "sources": {"events": {"path": "source/b/events.jsonl",
                                    "sha256": "b" * 64, "bytes": 20}},
            "warnings": [],
        }

    def tearDown(self) -> None:
        _remove_scratch(self.scratch)

    def test_manifest_order_and_bytes_are_deterministic_and_content_addressed(self) -> None:
        forward = session_evidence.build_manifest(
            self.repo, [self.digest_a, self.digest_b])
        reverse = session_evidence.build_manifest(
            self.repo, [self.digest_b, self.digest_a])

        self.assertEqual(forward, reverse)
        first = session_evidence.write_manifest(self.output, forward)
        original = first.read_bytes()
        second = session_evidence.write_manifest(self.output, reverse)

        self.assertEqual(first, second)
        self.assertEqual(first.stem, hashlib.sha256(original).hexdigest())
        self.assertEqual(list(self.output.glob("*.tmp")), [])
        self.assertEqual([s["ordinal"] for s in forward["sessions"]], [1, 2])
        self.assertEqual([s["session_id"] for s in forward["sessions"]],
                         ["session-a", "session-b"])

        changed = session_evidence.build_manifest(self.repo, [
            {**self.digest_a, "human": ["A changed correction."]}, self.digest_b,
        ])
        changed_path = session_evidence.write_manifest(self.output, changed)
        self.assertNotEqual(first, changed_path)
        self.assertEqual(first.read_bytes(), original)

    def test_atomic_failure_leaves_no_partial_snapshot(self) -> None:
        manifest = session_evidence.build_manifest(self.repo, [self.digest_a])
        with mock.patch.object(session_evidence.os, "replace",
                               side_effect=OSError("simulated replace failure")):
            with self.assertRaisesRegex(OSError, "simulated replace failure"):
                session_evidence.write_manifest(self.output, manifest)

        self.assertEqual(list(self.output.iterdir()), [])

    def test_existing_corrupt_content_address_is_not_overwritten(self) -> None:
        manifest = session_evidence.build_manifest(self.repo, [self.digest_a])
        path = session_evidence.write_manifest(self.output, manifest)
        path.write_bytes(b"corrupt")

        with self.assertRaisesRegex(ValueError, "corrupt snapshot"):
            session_evidence.write_manifest(self.output, manifest)
        self.assertEqual(path.read_bytes(), b"corrupt")


class TestCommandLineSnapshot(unittest.TestCase):
    def test_real_cli_discovers_digests_and_writes_one_snapshot(self) -> None:
        base = _scratch()
        try:
            root = base / "session-state"
            root.mkdir()
            output = base / "snapshots"
            repo = Path("C:/Development/Godot/Test Game")
            _write_session(
                root, "cli-session", str(repo),
                (_event("user.message", content="CLI correction.") + "\n").encode("utf-8"))

            completed = subprocess.run([
                sys.executable,
                str(TOOLS / "session_digest.py"),
                "--repo", str(repo),
                "--sessions", str(root),
                "--snapshot", str(output),
            ], capture_output=True, text=True, timeout=20, check=False)

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn('S1:H1 "CLI correction."', completed.stdout)
            self.assertIn("snapshot:", completed.stderr)
            snapshots = list(output.glob("*.json"))
            self.assertEqual(len(snapshots), 1)
            self.assertEqual(
                snapshots[0].stem,
                hashlib.sha256(snapshots[0].read_bytes()).hexdigest(),
            )
            repeated = subprocess.run([
                sys.executable,
                str(TOOLS / "session_digest.py"),
                "--repo", str(repo),
                "--sessions", str(root),
                "--snapshot", str(output),
            ], capture_output=True, text=True, timeout=20, check=False)
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(completed.stdout, repeated.stdout)
            self.assertEqual(len(list(output.glob("*.json"))), 1)
        finally:
            _remove_scratch(base)


if __name__ == "__main__":
    unittest.main(verbosity=2)
