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
import stat
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
        candidates = [Path(configured)] if configured else [
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


def _changed_scan_stat(info: os.stat_result, **changes: object) -> SimpleNamespace:
    fields = set(gate._SCAN_PATH_IDENTITY_FIELDS)
    fields.add("st_file_attributes")
    values = {field: getattr(info, field, None) for field in fields}
    values.update(changes)
    return SimpleNamespace(**values)


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


@contextlib.contextmanager
def _empty_scan(root: Path):
    with _managed_gate(root), contextlib.ExitStack() as stack:
        for name in (
            "stage_format",
            "stage_lint",
            "stage_sanitise",
            "stage_grep",
            "stage_types",
            "stage_arch",
            "stage_tests",
            "stage_assets",
        ):
            stack.enter_context(mock.patch.object(gate, name, side_effect=lambda: None))
        stack.enter_context(mock.patch.object(gate, "LOG_DIR", root / "logs"))
        yield


class BrownfieldGateTests(unittest.TestCase):
    def test_unchanged_old_issue_passes_even_when_tool_exits_zero(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            issue = _issue()
            _install_state(root, [issue])
            output = io.StringIO()

            with _managed_gate(root) as results, contextlib.redirect_stdout(output):
                gate._finish_file_stage("lint", [issue], failed=False)

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
                {
                    "format",
                    "lint",
                    "sanitise",
                    "grep",
                    "types",
                    "arch",
                    "tests",
                    "assets",
                },
                set(gate.BASELINEABLE_STAGES),
            )

    def test_flat_source_issue_fails_even_when_tool_exits_zero(self) -> None:
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
                gate._finish_file_stage("lint", [issue], failed=False)

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
                mock.patch.object(gate, "stage_arch", side_effect=clean("arch")),
                mock.patch.object(gate, "stage_tests", side_effect=clean("tests")),
                mock.patch.object(gate, "stage_assets", side_effect=clean("assets")),
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
            ), mock.patch.object(
                gate, "stage_arch", side_effect=lambda: None
            ), mock.patch.object(
                gate, "stage_tests", side_effect=lambda: None
            ), mock.patch.object(
                gate, "stage_assets", side_effect=lambda: None
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

    def test_scan_refuses_hardlinked_output_without_touching_sentinel(self) -> None:
        with _Scratch() as root:
            sentinel = root.parent / f"brownfield-sentinel-{uuid.uuid4().hex}"
            output = root / "scan.json"
            sentinel.write_bytes(b"outside\n")
            try:
                os.link(sentinel, output)
                with _empty_scan(root):
                    self.assertEqual(2, gate.run_brownfield_scan(str(output)))
                self.assertEqual(b"outside\n", sentinel.read_bytes())
            finally:
                sentinel.unlink(missing_ok=True)

    def test_scan_refuses_symlinked_output_without_touching_sentinel(self) -> None:
        with _Scratch() as root:
            sentinel = root.parent / f"brownfield-sentinel-{uuid.uuid4().hex}"
            output = root / "scan.json"
            sentinel.write_bytes(b"outside\n")
            try:
                try:
                    output.symlink_to(sentinel)
                except OSError:
                    output.write_bytes(b"redirected\n")
                    path_type = type(output)
                    original = path_type.lstat

                    def redirected_lstat(
                        path: Path, *args: object, **kwargs: object
                    ):
                        info = original(path, *args, **kwargs)
                        if path == output:
                            redirected = mock.Mock(wraps=info)
                            redirected.st_mode = info.st_mode
                            redirected.st_file_attributes = 0x0400
                            return redirected
                        return info

                    with mock.patch.object(
                        path_type,
                        "lstat",
                        autospec=True,
                        side_effect=redirected_lstat,
                    ), _empty_scan(root):
                        self.assertEqual(2, gate.run_brownfield_scan(str(output)))
                else:
                    with _empty_scan(root):
                        self.assertEqual(2, gate.run_brownfield_scan(str(output)))
                self.assertEqual(b"outside\n", sentinel.read_bytes())
            finally:
                sentinel.unlink(missing_ok=True)

    def test_scan_ignores_the_old_predictable_temporary_name(self) -> None:
        with _Scratch() as root:
            sentinel = root.parent / f"brownfield-sentinel-{uuid.uuid4().hex}"
            output = root / "scan.json"
            predictable = root / "scan.json.tmp"
            sentinel.write_bytes(b"outside\n")
            try:
                os.link(sentinel, predictable)
                with _empty_scan(root):
                    self.assertEqual(0, gate.run_brownfield_scan(str(output)))
                self.assertEqual(b"outside\n", sentinel.read_bytes())
                self.assertEqual(b"outside\n", predictable.read_bytes())
                self.assertTrue(json.loads(output.read_text(encoding="utf-8"))["complete"])
                self.assertFalse((root / "logs").exists())
            finally:
                sentinel.unlink(missing_ok=True)

    def test_scan_refuses_a_case_colliding_output_name(self) -> None:
        with _Scratch() as root:
            collision = root / "SCAN.JSON"
            output = root / "scan.json"
            collision.write_bytes(b"project-owned\n")

            with _empty_scan(root):
                self.assertEqual(2, gate.run_brownfield_scan(str(output)))

            self.assertEqual(b"project-owned\n", collision.read_bytes())

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

    def test_architecture_capture_keeps_graph_and_source_file_boundaries(self) -> None:
        document = {
            "mermaid": "graph TD\n    scripts --> custom",
            "violations": [
                "scripts -> custom is not permitted "
                "(arch.rules.json allows: nothing)"
            ],
            "violation_issues": [{
                "code": "dependency-not-permitted",
                "path": "scripts/player.gd",
                "line": 0,
                "message": (
                    "scripts -> custom is not permitted "
                    "(arch.rules.json allows: nothing)"
                ),
            }],
        }

        with mock.patch.object(gate, "GAME_LAYOUT", "src"):
            issues, errors = gate._arch_issues(
                document,
                "ARCHITECTURE.md diagram is stale; run: kit architecture update",
            )

        self.assertEqual([], errors)
        self.assertEqual(
            [
                ("stale-architecture-graph", "ARCHITECTURE.md"),
                ("dependency-not-permitted", "src/scripts/player.gd"),
            ],
            [(item["code"], item["path"]) for item in issues],
        )

    def test_architecture_capture_fails_closed_for_an_unscoped_violation(self) -> None:
        document = {
            "mermaid": "graph TD",
            "violations": ["scripts -> custom is not permitted"],
            "violation_issues": [],
        }

        issues, errors = gate._arch_issues(document, "boundary violation")

        self.assertEqual([], issues)
        self.assertIn("not bound to a source file", errors[0])

    def test_tests_and_assets_capture_the_affected_project_files(self) -> None:
        with _Scratch() as root:
            _write(
                root,
                ".gutconfig.json",
                b'{"dirs":["res://tests/unit"],"prefix":"test_",'
                b'"suffix":".gd","include_subdirs":true}\n',
            )
            orphan = _write(root, "tests/missed_spec.gd", b"extends Node\n")
            asset = _write(root, "art/icon.png", b"existing asset\n")
            gate.BROWNFIELD_CAPTURE_ISSUES.clear()
            gate.BROWNFIELD_CAPTURE_ERRORS.clear()

            with mock.patch.object(gate, "PROJECT_DIR", root), mock.patch.object(
                gate, "GAME_LAYOUT", "."
            ), mock.patch.object(
                gate, "gd_files", return_value=[orphan]
            ), mock.patch.object(
                gate, "_layer_dirs", return_value=((), ())
            ), mock.patch.object(
                gate, "BROWNFIELD_CAPTURE", True
            ), contextlib.redirect_stdout(io.StringIO()):
                gate.stage_tests()
                gate.stage_assets()

            captured = gate.BROWNFIELD_CAPTURE_ISSUES
            self.assertIn(
                ("tests", "uncollected-test", "tests/missed_spec.gd"),
                [
                    (item["stage"], item["code"], item["path"])
                    for item in captured
                ],
            )
            self.assertIn(
                ("assets", "missing-import-sidecar", "art/icon.png"),
                [
                    (item["stage"], item["code"], item["path"])
                    for item in captured
                ],
            )
            self.assertEqual(b"existing asset\n", asset.read_bytes())
            gate.BROWNFIELD_CAPTURE_ISSUES.clear()
            gate.BROWNFIELD_CAPTURE_ERRORS.clear()

    def test_missing_test_configuration_is_not_invented_as_an_existing_issue(self) -> None:
        with _Scratch() as root:
            _write(root, "project.godot", b"[application]\n")
            gate.BROWNFIELD_CAPTURE_ISSUES.clear()
            gate.BROWNFIELD_CAPTURE_ERRORS.clear()

            with _managed_gate(root), mock.patch.object(
                gate, "BROWNFIELD_CAPTURE", True
            ), contextlib.redirect_stdout(io.StringIO()):
                gate.stage_tests()

            self.assertEqual([], gate.BROWNFIELD_CAPTURE_ISSUES)
            self.assertEqual([], gate.BROWNFIELD_CAPTURE_ERRORS)
            gate.BROWNFIELD_CAPTURE_ISSUES.clear()
            gate.BROWNFIELD_CAPTURE_ERRORS.clear()

    def test_invalid_test_configuration_is_bound_to_the_existing_file(self) -> None:
        with _Scratch() as root:
            _write(root, ".gutconfig.json", b"{invalid\n")
            gate.BROWNFIELD_CAPTURE_ISSUES.clear()
            gate.BROWNFIELD_CAPTURE_ERRORS.clear()

            with _managed_gate(root), mock.patch.object(
                gate, "BROWNFIELD_CAPTURE", True
            ), contextlib.redirect_stdout(io.StringIO()):
                gate.stage_tests()

            self.assertEqual([], gate.BROWNFIELD_CAPTURE_ERRORS)
            self.assertEqual(
                [("invalid-runner-config", ".gutconfig.json")],
                [
                    (item["code"], item["path"])
                    for item in gate.BROWNFIELD_CAPTURE_ISSUES
                ],
            )
            gate.BROWNFIELD_CAPTURE_ISSUES.clear()
            gate.BROWNFIELD_CAPTURE_ERRORS.clear()

    def test_invalid_test_configuration_fields_are_bound_to_the_existing_file(self) -> None:
        invalid_documents = (
            {"dirs": 1},
            {"dirs": [1]},
            {"dirs": [""]},
            {"dirs": ["res://"]},
            {"dirs": ["/outside"]},
            {"dirs": ["C:/outside"]},
            {"dirs": ["../outside"]},
            {"dirs": ["."]},
            {"dirs": ["tests//unit"]},
            {"dirs": ["tests/"]},
            {"dirs": [r"tests\unit"]},
            {"prefix": []},
            {"prefix": ""},
            {"prefix": "nested/test_"},
            {"suffix": False},
            {"suffix": ""},
            {"suffix": r"nested\_spec.gd"},
            {"include_subdirs": "yes"},
        )
        for document in invalid_documents:
            with self.subTest(document=document), _Scratch() as root:
                _write(
                    root,
                    ".gutconfig.json",
                    json.dumps(document).encode("utf-8"),
                )
                gate.BROWNFIELD_CAPTURE_ISSUES.clear()
                gate.BROWNFIELD_CAPTURE_ERRORS.clear()

                with _managed_gate(root), mock.patch.object(
                    gate, "BROWNFIELD_CAPTURE", True
                ), contextlib.redirect_stdout(io.StringIO()):
                    gate.stage_tests()

                self.assertEqual([], gate.BROWNFIELD_CAPTURE_ERRORS)
                self.assertEqual(
                    [("invalid-runner-config", ".gutconfig.json")],
                    [
                        (item["code"], item["path"])
                        for item in gate.BROWNFIELD_CAPTURE_ISSUES
                    ],
                )
                gate.BROWNFIELD_CAPTURE_ISSUES.clear()
                gate.BROWNFIELD_CAPTURE_ERRORS.clear()

    def test_test_configuration_accepts_res_and_project_relative_directories(self) -> None:
        for directory in ("res://tests/unit", "tests/unit"):
            with self.subTest(directory=directory), _Scratch() as root:
                _write(
                    root,
                    ".gutconfig.json",
                    json.dumps({
                        "dirs": [directory],
                        "prefix": "test_",
                        "suffix": ".gd",
                        "include_subdirs": True,
                    }).encode("utf-8"),
                )
                test_file = _write(root, "tests/unit/test_example.gd", b"extends Node\n")
                gate.BROWNFIELD_CAPTURE_ISSUES.clear()
                gate.BROWNFIELD_CAPTURE_ERRORS.clear()

                with _managed_gate(root), mock.patch.object(
                    gate, "BROWNFIELD_CAPTURE", True
                ), mock.patch.object(
                    gate, "gd_files", return_value=[test_file]
                ), mock.patch.object(
                    gate, "_layer_dirs", return_value=((), ())
                ), contextlib.redirect_stdout(io.StringIO()):
                    gate.stage_tests()

                self.assertEqual([], gate.BROWNFIELD_CAPTURE_ISSUES)
                self.assertEqual([], gate.BROWNFIELD_CAPTURE_ERRORS)

    def test_managed_project_without_test_configuration_reports_an_honest_skip(self) -> None:
        with _Scratch() as root:
            _write(root, "project.godot", b"[application]\n")

            with _managed_gate(root) as results, contextlib.redirect_stdout(
                io.StringIO()
            ):
                gate.stage_tests()

            self.assertFalse(results.failed)
            self.assertEqual(["SKIP  tests (runner not configured)"], results.lines)

    def test_project_inventory_is_reused_without_a_second_walk_or_read(self) -> None:
        with _Scratch() as root:
            source = _write(root, "scripts/player.gd", b"extends Node\n")
            scene = _write(root, "scenes/main.tscn", b"[gd_scene format=3]\n")
            asset = _write(root, "art/icon.png", b"image bytes")
            sidecar = _write(root, "art/icon.png.import", b"[remap]\n")
            _write(root, ".gutconfig.json", b'{"dirs":["tests"]}\n')

            inventory = gate.scan_brownfield_project(root)
            with mock.patch.object(
                gate, "_BROWNFIELD_PROJECT_INVENTORY", inventory
            ), mock.patch.object(
                Path, "rglob", side_effect=AssertionError("second walk")
            ), mock.patch.object(
                Path, "read_text", side_effect=AssertionError("second read")
            ):
                self.assertEqual([source], gate.gd_files())
                self.assertEqual(
                    "extends Node\n",
                    gate._read_project_text(source, errors="replace"),
                )
                self.assertEqual(
                    [scene], gate._project_files_with_suffixes((".tscn",))
                )
                self.assertEqual(
                    [asset], gate._project_files_with_suffixes(gate.ASSET_EXTS)
                )
                self.assertTrue(gate._project_file_exists(sidecar))

    def test_project_inventory_rejects_linked_stage_inputs_before_reading(self) -> None:
        for relative in ("scripts/external.gd", "scenes/external.tscn", "art/external.png"):
            with self.subTest(relative=relative), _Scratch() as root:
                linked = _write(root, relative, b"outside content\n")
                linked_key = os.path.normcase(os.path.abspath(linked))
                real_lstat = Path.lstat

                def simulated_hardlink(path: Path) -> object:
                    info = real_lstat(path)
                    if os.path.normcase(os.path.abspath(path)) == linked_key:
                        return _changed_scan_stat(info, st_nlink=2)
                    return info

                with mock.patch.object(
                    Path, "lstat", simulated_hardlink
                ), mock.patch.object(
                    gate.os,
                    "open",
                    side_effect=AssertionError("linked input was read"),
                ):
                    with self.assertRaisesRegex(
                        gate.BrownfieldScanError,
                        r"hard-linked file is not allowed",
                    ):
                        gate.scan_brownfield_project(root)

    def test_project_inventory_never_opens_a_symbolic_link_target(self) -> None:
        with _Scratch() as root:
            linked = _write(root, "external.gd", b"outside content\n")
            linked_key = os.path.normcase(os.path.abspath(linked))
            real_lstat = Path.lstat

            def simulated_symlink(path: Path) -> object:
                info = real_lstat(path)
                if os.path.normcase(os.path.abspath(path)) == linked_key:
                    return _changed_scan_stat(
                        info,
                        st_mode=stat.S_IFLNK | stat.S_IMODE(info.st_mode),
                    )
                return info

            with mock.patch.object(
                Path, "lstat", simulated_symlink
            ), mock.patch.object(
                gate.os,
                "open",
                side_effect=AssertionError("symbolic link target was opened"),
            ):
                with self.assertRaisesRegex(
                    gate.BrownfieldScanError,
                    r"symbolic link is not allowed: external\.gd",
                ):
                    gate.scan_brownfield_project(root)

    def test_brownfield_scan_does_not_run_or_pass_tools_after_link_blocker(self) -> None:
        with _Scratch() as root:
            linked = _write(root, "scripts/external.gd", b"extends Node\n")
            linked_key = os.path.normcase(os.path.abspath(linked))
            real_lstat = Path.lstat

            def simulated_hardlink(path: Path) -> object:
                info = real_lstat(path)
                if os.path.normcase(os.path.abspath(path)) == linked_key:
                    return _changed_scan_stat(info, st_nlink=2)
                return info

            stage_mocks = []
            with _managed_gate(root), contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(Path, "lstat", simulated_hardlink))
                tool = stack.enter_context(mock.patch.object(gate, "_run_external_tool"))
                for name in (
                    "stage_format", "stage_lint", "stage_sanitise", "stage_grep",
                    "stage_types", "stage_arch", "stage_tests", "stage_assets",
                ):
                    stage_mocks.append(
                        stack.enter_context(mock.patch.object(gate, name))
                    )
                output = root / "scan.json"
                self.assertEqual(2, gate.run_brownfield_scan(str(output)))

            tool.assert_not_called()
            for stage in stage_mocks:
                stage.assert_not_called()
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertFalse(document["complete"])
            self.assertIn("hard-linked file", document["errors"][0])

    def test_project_inventory_member_limit_bounds_the_walk(self) -> None:
        with _Scratch() as root:
            _write(root, "one.txt", b"1")
            _write(root, "two.txt", b"2")
            with mock.patch.object(gate, "MAX_BROWNFIELD_SCAN_MEMBERS", 1):
                with self.assertRaisesRegex(
                    gate.BrownfieldScanError,
                    r"more than 1 scan members",
                ):
                    gate.scan_brownfield_project(root)

    def test_project_inventory_depth_and_total_bytes_are_bounded(self) -> None:
        with _Scratch() as root:
            _write(root, "nested/source.gd", b"1234")
            with mock.patch.object(gate, "MAX_BROWNFIELD_SCAN_DEPTH", 1):
                with self.assertRaisesRegex(
                    gate.BrownfieldScanError,
                    r"nesting exceeds 1 levels",
                ):
                    gate.scan_brownfield_project(root)

        with _Scratch() as root:
            _write(root, "one.gd", b"1234")
            _write(root, "two.gd", b"5678")
            with mock.patch.object(gate, "MAX_BROWNFIELD_TOTAL_BYTES", 7):
                with self.assertRaisesRegex(
                    gate.BrownfieldScanError,
                    r"text files exceed 7 total bytes",
                ):
                    gate.scan_brownfield_project(root)

    def test_project_inventory_rejects_a_reparse_directory_without_entering_it(self) -> None:
        with _Scratch() as root:
            linked = root / "external"
            linked.mkdir()
            _write(linked, "outside.gd", b"extends Node\n")
            linked_key = os.path.normcase(os.path.abspath(linked))
            real_lstat = Path.lstat
            real_scandir = gate.os.scandir
            marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
            entered: list[str] = []

            def simulated_reparse(path: Path) -> object:
                info = real_lstat(path)
                if os.path.normcase(os.path.abspath(path)) == linked_key:
                    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
                    return _changed_scan_stat(
                        info, st_file_attributes=attributes | marker
                    )
                return info

            def tracked_scandir(path: Path) -> object:
                entered.append(os.path.normcase(os.path.abspath(path)))
                return real_scandir(path)

            with mock.patch.object(
                Path, "lstat", simulated_reparse
            ), mock.patch.object(gate.os, "scandir", side_effect=tracked_scandir):
                with self.assertRaisesRegex(
                    gate.BrownfieldScanError,
                    r"linked directory is not allowed: external",
                ):
                    gate.scan_brownfield_project(root)

            self.assertNotIn(linked_key, entered)

    def test_project_inventory_rejects_an_unrelated_reparse_file_consistently(self) -> None:
        with _Scratch() as root:
            linked = _write(root, "notes.bin", b"not a stage input")
            linked_key = os.path.normcase(os.path.abspath(linked))
            real_lstat = Path.lstat
            marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))

            def simulated_reparse(path: Path) -> object:
                info = real_lstat(path)
                if os.path.normcase(os.path.abspath(path)) == linked_key:
                    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
                    return _changed_scan_stat(
                        info, st_file_attributes=attributes | marker
                    )
                return info

            with mock.patch.object(Path, "lstat", simulated_reparse):
                with self.assertRaisesRegex(
                    gate.BrownfieldScanError,
                    r"linked file is not allowed: notes\.bin",
                ):
                    gate.scan_brownfield_project(root)

    def test_lifecycle_style_tools_use_shipped_policy_and_captured_sources(self) -> None:
        observed: dict[str, object] = {}

        def isolated_run(
            command: object,
            _timeout: int,
            _log: Path,
            *,
            cwd: Path,
        ) -> tuple[int, str]:
            observed["cwd"] = str(cwd)
            observed["format"] = (cwd / "gdformatrc").read_text(encoding="utf-8")
            observed["lint"] = (cwd / "gdlintrc").read_text(encoding="utf-8")
            values = list(command)  # type: ignore[arg-type]
            mirror = Path(values[-1])
            observed["input"] = mirror.read_text(encoding="utf-8")
            observed["argument"] = str(mirror)
            return 0, f"{mirror}: ok\n"

        with _Scratch() as root:
            source = _write(root, "scripts/safe.gd", b"extends Node\n")
            inventory = gate.scan_brownfield_project(root)
            source.write_text("changed after inventory\n", encoding="utf-8")
            with mock.patch.object(
                gate, "_lifecycle_tool_boundary", return_value=True
            ), mock.patch.object(
                gate, "_BROWNFIELD_PROJECT_INVENTORY", inventory
            ), mock.patch.object(
                gate, "_run_external_tool", side_effect=isolated_run
            ):
                code, output = gate._run_style_tool(
                    ["gdlint", str(source)], 30, Path("unused")
                )

            self.assertEqual(0, code)
            self.assertIn(str(source), output)
            self.assertNotIn(str(observed["argument"]), output)
            self.assertNotEqual(str(source), observed["argument"])

        shipped_lint_policy = "\n".join(
            line
            for line in (ROOT / ".gdlintrc").read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ) + "\n"
        self.assertEqual("{}\n", observed["format"])
        self.assertEqual(shipped_lint_policy, observed["lint"])
        self.assertEqual("extends Node\n", observed["input"])
        self.assertNotEqual(str(gate.PROJECT_DIR), observed["cwd"])

    def test_lifecycle_style_batches_bound_the_file_argv(self) -> None:
        files = [f"C:/project/file_{index}.gd" for index in range(5)]
        with mock.patch.object(
            gate, "_lifecycle_tool_boundary", return_value=True
        ), mock.patch.object(
            gate, "MAX_STYLE_TOOL_BATCH_FILES", 2
        ), mock.patch.object(
            gate, "_run_style_tool", return_value=(0, "ok")
        ) as run:
            code, _output = gate._run_style_batches(
                ["gdlint"], files, 30, Path("lint.log")
            )

        self.assertEqual(0, code)
        self.assertEqual(3, run.call_count)
        self.assertEqual([3, 3, 2], [len(call.args[0]) for call in run.call_args_list])

    def test_nested_game_root_uses_the_same_captured_layer_rules(self) -> None:
        with _Scratch() as root:
            game_root = root / "game"
            game_root.mkdir()
            _write(
                root,
                "arch.rules.json",
                b'{"type_boundary":{"interior":["code/"],'
                b'"boundary":["io/"]}}\n',
            )
            with mock.patch.object(gate, "ROOT", root), mock.patch.object(
                gate, "PROJECT_DIR", game_root
            ):
                expected = gate._layer_dirs()
                inventory = gate.scan_brownfield_project()
                with mock.patch.object(
                    gate, "_BROWNFIELD_PROJECT_INVENTORY", inventory
                ), mock.patch.object(
                    Path, "read_text", side_effect=AssertionError("live config read")
                ):
                    captured = gate._layer_dirs()

            self.assertEqual((("code/",), ("io/",)), expected)
            self.assertEqual(expected, captured)

    def test_invalid_utf8_test_config_becomes_a_file_scoped_issue(self) -> None:
        with _Scratch() as root:
            _write(root, ".gutconfig.json", b"\xff")
            inventory = gate.scan_brownfield_project(root)
            gate.BROWNFIELD_CAPTURE_ISSUES.clear()
            gate.BROWNFIELD_CAPTURE_ERRORS.clear()
            with mock.patch.object(gate, "PROJECT_DIR", root), mock.patch.object(
                gate, "GAME_LAYOUT", "."
            ), mock.patch.object(
                gate, "_BROWNFIELD_PROJECT_INVENTORY", inventory
            ), mock.patch.object(
                gate, "BROWNFIELD_CAPTURE", True
            ), contextlib.redirect_stdout(io.StringIO()):
                gate.stage_tests()

            self.assertEqual([], gate.BROWNFIELD_CAPTURE_ERRORS)
            self.assertEqual(
                ["invalid-runner-config"],
                [item["code"] for item in gate.BROWNFIELD_CAPTURE_ISSUES],
            )
            gate.BROWNFIELD_CAPTURE_ISSUES.clear()

    def test_brownfield_sanitise_uses_one_json_snapshot(self) -> None:
        payload = json.dumps({
            "scanned": 1,
            "changed": [],
            "notes": [],
            "structural_errors": [],
            "errors": [],
            "applied": False,
            "ok": True,
        })
        gate.BROWNFIELD_CAPTURE_ISSUES.clear()
        gate.BROWNFIELD_CAPTURE_ERRORS.clear()
        with mock.patch.object(
            gate, "BROWNFIELD_CAPTURE", True
        ), mock.patch.object(
            gate, "_run_internal_python", return_value=(0, payload)
        ) as run, contextlib.redirect_stdout(io.StringIO()):
            gate.stage_sanitise()

        run.assert_called_once()
        self.assertEqual(("--json",), run.call_args.args[1])
        self.assertEqual([], gate.BROWNFIELD_CAPTURE_ERRORS)


if __name__ == "__main__":
    unittest.main()
