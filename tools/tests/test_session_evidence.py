#!/usr/bin/env python3
"""Production-shaped tests for repository-scoped Copilot evidence snapshots."""
from __future__ import annotations

import hashlib
import json
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
            "python tools/check_secret.py": 3,
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
        self.assertIn("python tools/check_secret.py", encoded)
        self.assertNotIn("workspace-secret", encoded)
        self.assertNotIn("command-secret", encoded)
        self.assertNotIn("tool-output-secret", encoded)
        self.assertNotIn("assistant-secret", encoded)
        message = manifest["sessions"][0]["evidence"]["human_messages"][0]
        self.assertEqual(
            message["citation"],
            "12345678-1234-1234-1234-123456789abc:H1",
        )

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
