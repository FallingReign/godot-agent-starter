#!/usr/bin/env python3
"""Gate integration tests for managed existing-project allowances."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(ROOT))

import check as gate  # noqa: E402
from tools import brownfield  # noqa: E402


INSTALLATION_ID = "1" * 32
RELEASE_SHA256 = "a" * 64


class _Scratch:
    def __enter__(self) -> Path:
        configured = os.environ.get("KIT_TEST_TMPDIR")
        candidates = ([Path(configured)] if configured else []) + [
            ROOT / ".checklogs" / "tests",
            Path(tempfile.gettempdir()),
            Path("/tmp"),
        ]
        self.root: Path | None = None
        for candidate in candidates:
            probe = candidate / f"brownfield-gate-probe-{uuid.uuid4().hex}"
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                probe.mkdir()
                (probe / "write-check").write_text("ok", encoding="utf-8")
                shutil.rmtree(probe)
                self.root = (
                    candidate / f"brownfield-gate-{uuid.uuid4().hex}"
                ).resolve()
                self.root.mkdir()
                return self.root
            except OSError:
                shutil.rmtree(probe, ignore_errors=True)
        raise RuntimeError("no writable brownfield gate test directory")

    def __exit__(self, *_args: object) -> None:
        if self.root is not None and self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)


def _write(root: Path, relative: str, content: bytes) -> Path:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _issue(
    *,
    message: str = "variable needs an explicit type",
    path: str = "scripts/player.gd",
    line: int = 7,
) -> dict[str, object]:
    return {
        "stage": "lint",
        "code": "untyped-variable",
        "path": path,
        "line": line,
        "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
    }


def _install_state(root: Path, issues: list[dict[str, object]]) -> bytes:
    current = {
        "schema": 1,
        "kind": "agent-kit-install-state",
        "installation_id": INSTALLATION_ID,
        "active_release": {"archive_sha256": RELEASE_SHA256},
    }
    _write(
        root,
        ".agent-kit/current.json",
        brownfield.canonical_json(current),
    )
    baseline = brownfield.build_baseline(
        root,
        issues,
        installation_id=INSTALLATION_ID,
        release_sha256=RELEASE_SHA256,
    )
    content = brownfield.canonical_json(baseline)
    _write(root, brownfield.BASELINE_RELATIVE, content)
    return content


@contextlib.contextmanager
def _managed_gate(root: Path):
    results = gate.Results()
    context = SimpleNamespace(
        install_mode="managed",
        release_sha256=RELEASE_SHA256,
        runtime_root=root,
    )
    with mock.patch.object(gate, "PROJECT_ROOT", root), mock.patch.object(
        gate, "PROJECT_DIR", root
    ), mock.patch.object(gate, "GAME_LAYOUT", "."), mock.patch.object(
        gate, "CONTEXT", context
    ), mock.patch.object(gate, "RESULTS", results):
        gate._reset_brownfield_state()
        yield results
        gate._reset_brownfield_state()


class BrownfieldGateTests(unittest.TestCase):
    def test_unchanged_old_issue_passes_and_is_shown(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            issue = _issue()
            _install_state(root, [issue])
            output = io.StringIO()

            with _managed_gate(root) as results, contextlib.redirect_stdout(output):
                gate._finish_file_stage("lint", [issue], failed=True)

            self.assertFalse(results.failed)
            self.assertEqual(["PASS  lint"], results.lines)
            self.assertIn("EXISTING", output.getvalue())
            self.assertIn("scripts/player.gd:7", output.getvalue())

    def test_fixed_issue_disappears(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            _install_state(root, [_issue()])
            _write(root, "scripts/player.gd", b"fixed\n")
            output = io.StringIO()

            with _managed_gate(root) as results, contextlib.redirect_stdout(output):
                gate._finish_file_stage("lint", [], failed=False)

            self.assertFalse(results.failed)
            self.assertNotIn("EXISTING", output.getvalue())
            self.assertNotIn("scripts/player.gd", output.getvalue())

    def test_new_or_worsened_issue_fails(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            _install_state(root, [_issue()])
            changed = _issue(message="a different diagnostic")
            output = io.StringIO()

            with _managed_gate(root) as results, contextlib.redirect_stdout(output):
                gate._finish_file_stage("lint", [changed], failed=True)

            self.assertTrue(results.failed)
            self.assertIn("NEW OR CHANGED", output.getvalue())
            self.assertIn("new-or-changed-gap", output.getvalue())

    def test_edited_file_invalidates_identical_issue(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            issue = _issue()
            _install_state(root, [issue])
            _write(root, "scripts/player.gd", b"old problem plus a comment\n")
            output = io.StringIO()

            with _managed_gate(root) as results, contextlib.redirect_stdout(output):
                gate._finish_file_stage("lint", [issue], failed=True)

            self.assertTrue(results.failed)
            self.assertIn("affected-file-changed", output.getvalue())

    def test_global_stage_cannot_use_a_file_allowance(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            issue = _issue()
            _install_state(root, [issue])

            with _managed_gate(root) as results:
                gate._finish_file_stage("schema", [issue], failed=True)

            self.assertTrue(results.failed)
            self.assertEqual(["FAIL  schema"], results.lines)
            self.assertEqual(
                {"format", "lint", "sanitise", "grep", "types"},
                set(gate.BASELINEABLE_STAGES),
            )

    def test_flat_source_keeps_normal_failure_behavior(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            issue = _issue()
            _install_state(root, [issue])
            results = gate.Results()
            context = SimpleNamespace(install_mode="flat", release_sha256=None)
            output = io.StringIO()

            with mock.patch.object(gate, "PROJECT_ROOT", root), mock.patch.object(
                gate, "CONTEXT", context
            ), mock.patch.object(gate, "RESULTS", results), contextlib.redirect_stdout(
                output
            ):
                gate._reset_brownfield_state()
                gate._finish_file_stage("lint", [issue], failed=True)

            self.assertTrue(results.failed)
            self.assertNotIn("EXISTING", output.getvalue())
            self.assertNotIn("baseline", output.getvalue())

    def test_flat_source_keeps_historical_nonblocking_read_error_semantics(self) -> None:
        results = gate.Results()
        context = SimpleNamespace(install_mode="flat", release_sha256=None)
        with mock.patch.object(gate, "CONTEXT", context), mock.patch.object(
            gate, "RESULTS", results
        ):
            gate._finish_file_stage(
                "grep",
                [],
                failed=False,
                scan_errors=["could not read scripts/player.gd"],
            )

        self.assertFalse(results.failed)
        self.assertEqual(["PASS  grep"], results.lines)

    def test_invalid_baseline_is_ignored_and_failure_remains(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            issue = _issue()
            _install_state(root, [issue])
            _write(root, brownfield.BASELINE_RELATIVE, b"{}\n")
            output = io.StringIO()

            with _managed_gate(root) as results, contextlib.redirect_stdout(output):
                gate._finish_file_stage("lint", [issue], failed=True)

            self.assertTrue(results.failed)
            self.assertIn("baseline ignored", output.getvalue())

    def test_capture_is_deterministic_and_does_not_write_the_baseline(self) -> None:
        with _Scratch() as root:
            game = _write(root, "scripts/player.gd", b"old problem\n")
            original_baseline = _install_state(root, [_issue()])
            first = root / "first-scan.json"
            second = root / "second-scan.json"
            extra = _issue(path="scripts/player.gd", line=2, message="second")

            def stage_format() -> None:
                gate._finish_file_stage("format", [], failed=False)

            def stage_lint() -> None:
                gate._finish_file_stage("lint", [extra, _issue()], failed=True)

            def clean(stage: str):
                return lambda: gate._finish_file_stage(stage, [], failed=False)

            patches = (
                mock.patch.object(gate, "stage_format", side_effect=stage_format),
                mock.patch.object(gate, "stage_lint", side_effect=stage_lint),
                mock.patch.object(gate, "stage_sanitise", side_effect=clean("sanitise")),
                mock.patch.object(gate, "stage_grep", side_effect=clean("grep")),
                mock.patch.object(gate, "stage_types", side_effect=clean("types")),
                mock.patch.object(gate, "LOG_DIR", root),
            )
            with _managed_gate(root), contextlib.ExitStack() as stack:
                for patch in patches:
                    stack.enter_context(patch)
                self.assertEqual(0, gate.run_brownfield_scan(str(first)))
                self.assertEqual(0, gate.run_brownfield_scan(str(second)))

            self.assertEqual(first.read_bytes(), second.read_bytes())
            document = json.loads(first.read_text(encoding="utf-8"))
            self.assertTrue(document["complete"])
            self.assertEqual(2, len(document["issues"]))
            self.assertEqual([2, 7], [item["line"] for item in document["issues"]])
            self.assertEqual(b"old problem\n", game.read_bytes())
            self.assertEqual(
                original_baseline,
                (root / brownfield.BASELINE_RELATIVE).read_bytes(),
            )

    def test_incomplete_capture_is_visible_and_cannot_be_a_baseline(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            _install_state(root, [_issue()])
            output = root / "scan.json"

            def broken_format() -> None:
                gate._capture_error("format", "formatter could not start")

            with _managed_gate(root), mock.patch.object(
                gate, "stage_format", side_effect=broken_format
            ), mock.patch.object(
                gate, "stage_lint", side_effect=lambda: None
            ), mock.patch.object(
                gate, "stage_sanitise", side_effect=lambda: None
            ), mock.patch.object(
                gate, "stage_grep", side_effect=lambda: None
            ), mock.patch.object(
                gate, "stage_types", side_effect=lambda: None
            ), mock.patch.object(gate, "LOG_DIR", root):
                self.assertEqual(2, gate.run_brownfield_scan(str(output)))

            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(document["complete"])
            self.assertEqual(["format: formatter could not start"], document["errors"])
            with self.assertRaisesRegex(
                brownfield.BrownfieldError, "unknown complete"
            ):
                brownfield.validate_baseline(
                    output.read_bytes(),
                    installation_id=INSTALLATION_ID,
                    release_sha256=RELEASE_SHA256,
                )

    def test_format_and_lint_collectors_keep_file_rule_and_line(self) -> None:
        with _Scratch() as root:
            source = _write(root, "scripts/player.gd", b"extends Node\n")
            with mock.patch.object(gate, "PROJECT_DIR", root), mock.patch.object(
                gate, "GAME_LAYOUT", "."
            ):
                format_issues, format_errors = gate._format_issues(
                    f"would reformat {source}\n1 file would be reformatted, 0 files would be left unchanged.\n"
                )
                lint_issues, lint_errors = gate._lint_issues(
                    f"{source}:7: Error: Variable needs a type (untyped-variable)\n"
                    "Failure: 1 problem found\n"
                )

            self.assertEqual([], format_errors)
            self.assertEqual("would-reformat", format_issues[0]["code"])
            self.assertEqual([], lint_errors)
            self.assertEqual("untyped-variable", lint_issues[0]["code"])
            self.assertEqual(7, lint_issues[0]["line"])
            self.assertEqual("scripts/player.gd", lint_issues[0]["path"])

    def test_sanitise_collector_keeps_rule_and_line(self) -> None:
        document = {
            "changed": ["scenes/main.tscn"],
            "notes": [
                "scenes/main.tscn:3: removed unique_id (optional, churns on reimport)"
            ],
            "structural_errors": [
                "scenes/main.tscn:8: ext_resource path does not exist: res://gone.gd"
            ],
        }
        with mock.patch.object(gate, "GAME_LAYOUT", "."):
            issues, errors = gate._sanitise_issues(document)

        self.assertEqual([], errors)
        self.assertEqual(
            [("needs-cleanup", 3), ("missing-ext-resource", 8)],
            [(item["code"], item["line"]) for item in issues],
        )

    def test_grep_and_types_capture_keep_rule_and_line(self) -> None:
        with _Scratch() as root:
            source = _write(
                root,
                "scripts/logic/player.gd",
                (
                    b"extends RefCounted\n"
                    b"func load_data(value: Variant) -> Variant:\n"
                    b"    connect(\"pressed\", self, \"_pressed\")\n"
                    b"    return JSON.parse_string(\"{}\")\n"
                ),
            )
            banned = [
                (
                    re.compile(r"connect\(\s*\""),
                    "legacy connect call",
                    "legacy-connect",
                )
            ]
            gate.BROWNFIELD_CAPTURE_ISSUES.clear()
            gate.BROWNFIELD_CAPTURE_ERRORS.clear()
            with mock.patch.object(gate, "PROJECT_DIR", root), mock.patch.object(
                gate, "GAME_LAYOUT", "."
            ), mock.patch.object(gate, "gd_files", return_value=[source]), mock.patch.object(
                gate, "load_banned", return_value=banned
            ), mock.patch.object(
                gate, "GATE_RULES_FILE", root / "gate.rules.json"
            ), mock.patch.object(
                gate,
                "_layer_dirs",
                return_value=(("scripts/logic/",), ("scripts/data/",)),
            ), mock.patch.object(gate, "BROWNFIELD_CAPTURE", True), contextlib.redirect_stdout(
                io.StringIO()
            ):
                gate.stage_grep()
                gate.stage_types()

            captured = gate.BROWNFIELD_CAPTURE_ISSUES
            self.assertIn(
                ("grep", "legacy-connect", 3),
                [(item["stage"], item["code"], item["line"]) for item in captured],
            )
            self.assertIn(
                ("types", "external-parse-call", 4),
                [(item["stage"], item["code"], item["line"]) for item in captured],
            )
            gate.BROWNFIELD_CAPTURE_ISSUES.clear()
            gate.BROWNFIELD_CAPTURE_ERRORS.clear()


if __name__ == "__main__":
    unittest.main()
