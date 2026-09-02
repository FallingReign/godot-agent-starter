#!/usr/bin/env python3
"""Focused tests for file-bound brownfield verification allowances."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from typing import Iterator
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import brownfield  # noqa: E402


INSTALLATION_ID = "1" * 32
RELEASE_SHA256 = "a" * 64


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _issue(
    path: str = "scripts/player.gd",
    *,
    stage: str = "lint",
    code: str = "gdscript.untyped-variable",
    line: int = 7,
    message: bytes = b"variable needs an explicit type",
) -> dict[str, object]:
    return {
        "stage": stage,
        "code": code,
        "path": path,
        "line": line,
        "message_sha256": _sha(message),
    }


class _Scratch:
    def __enter__(self) -> Path:
        configured = os.environ.get("KIT_TEST_TMPDIR")
        candidates = ([Path(configured)] if configured else []) + [
            TOOLS.parent / ".checklogs" / "tests",
            Path(tempfile.gettempdir()),
            Path("/tmp"),
        ]
        self.root: Path | None = None
        for candidate in candidates:
            probe = candidate / f"brownfield-probe-{uuid.uuid4().hex}"
            try:
                candidate.mkdir(parents=True, exist_ok=True)
                probe.mkdir()
                (probe / "write-check").write_text("ok", encoding="utf-8")
                shutil.rmtree(probe)
                self.root = (candidate / f"brownfield-{uuid.uuid4().hex}").resolve()
                self.root.mkdir()
                return self.root
            except OSError:
                shutil.rmtree(probe, ignore_errors=True)
        raise RuntimeError("no writable brownfield test scratch directory")

    def __exit__(self, *_args: object) -> None:
        if self.root is not None and self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)


def _write(root: Path, relative: str, content: bytes) -> Path:
    path = root.joinpath(*relative.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _built(root: Path, issues: Iterator[dict[str, object]] | list[dict[str, object]]):
    return brownfield.build_baseline(
        root,
        issues,
        installation_id=INSTALLATION_ID,
        release_sha256=RELEASE_SHA256,
    )


def _evaluate(root: Path, baseline: object, issues: list[dict[str, object]]):
    return brownfield.evaluate_baseline(
        root,
        baseline,  # type: ignore[arg-type]
        issues,
        installation_id=INSTALLATION_ID,
        release_sha256=RELEASE_SHA256,
    )


class BrownfieldBaselineTests(unittest.TestCase):
    def test_build_is_canonical_sorted_and_bound_only_to_affected_files(self) -> None:
        with _Scratch() as root:
            player = b"extends Node\nvar health = 10\n"
            enemy = b"extends Node\nvar speed = 2\n"
            _write(root, "scripts/player.gd", player)
            _write(root, "scripts/enemy.gd", enemy)
            issues = [
                _issue("scripts/player.gd", code="z-last"),
                _issue("scripts/enemy.gd", code="a-first", line=2),
            ]

            baseline = _built(root, iter(issues))
            encoded = brownfield.canonical_json(baseline)
            parsed = json.loads(encoded.decode("utf-8"))

            self.assertTrue(encoded.endswith(b"\n"))
            self.assertNotIn(b" ", encoded)
            self.assertEqual(INSTALLATION_ID, parsed["binding"]["installation_id"])
            self.assertEqual(RELEASE_SHA256, parsed["binding"]["release_sha256"])
            self.assertEqual(
                ["scripts/enemy.gd", "scripts/player.gd"],
                [entry["path"] for entry in parsed["issues"]],
            )
            self.assertEqual(_sha(enemy), parsed["issues"][0]["file_sha256"])
            self.assertEqual(_sha(player), parsed["issues"][1]["file_sha256"])
            self.assertNotIn("repository_sha256", encoded.decode("utf-8"))

    def test_unchanged_old_gap_is_visible_and_does_not_fail(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            issue = _issue()
            baseline = _built(root, [issue])

            result = _evaluate(root, baseline, [issue])

            self.assertEqual("pass", result["status"])
            self.assertEqual(
                {"existing": 1, "failing": 0, "resolved": 0}, result["counts"]
            )
            self.assertEqual(issue["code"], result["existing_gaps"][0]["code"])
            self.assertEqual([], result["failing_gaps"])

    def test_unrelated_repository_change_does_not_invalidate_allowance(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            issue = _issue()
            baseline = _built(root, [issue])
            _write(root, "docs/new-direction.md", b"unrelated change\n")

            result = _evaluate(root, baseline, [issue])

            self.assertEqual("pass", result["status"])
            self.assertEqual(1, result["counts"]["existing"])

    def test_fixed_gap_disappears_from_visible_state(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            baseline = _built(root, [_issue()])
            _write(root, "scripts/player.gd", b"fixed\n")

            result = _evaluate(root, baseline, [])

            self.assertEqual("pass", result["status"])
            self.assertEqual(
                {"existing": 0, "failing": 0, "resolved": 1}, result["counts"]
            )
            self.assertEqual([], result["existing_gaps"])
            self.assertNotIn("resolved_gaps", result)

    def test_new_or_changed_gap_fails(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            original = _issue()
            baseline = _built(root, [original])
            changed = _issue(message=b"a different diagnostic")

            result = _evaluate(root, baseline, [changed])

            self.assertEqual("fail", result["status"])
            self.assertEqual(1, result["counts"]["failing"])
            self.assertEqual("new-or-changed-gap", result["failing_gaps"][0]["reason"])
            self.assertEqual(1, result["counts"]["resolved"])

    def test_changing_affected_file_invalidates_identical_gap(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            issue = _issue()
            baseline = _built(root, [issue])
            _write(root, "scripts/player.gd", b"old problem plus a comment\n")

            result = _evaluate(root, baseline, [issue])

            self.assertEqual("fail", result["status"])
            failure = result["failing_gaps"][0]
            self.assertEqual("affected-file-changed", failure["reason"])
            self.assertNotEqual(
                failure["baseline_file_sha256"], failure["current_file_sha256"]
            )

    def test_wrong_installation_or_release_binding_is_rejected(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            baseline = _built(root, [_issue()])

            for installation_id, release_sha256 in (
                ("2" * 32, RELEASE_SHA256),
                (INSTALLATION_ID, "b" * 64),
            ):
                with self.subTest(
                    installation_id=installation_id,
                    release_sha256=release_sha256,
                ):
                    with self.assertRaisesRegex(
                        brownfield.BrownfieldError, "different installation or release"
                    ):
                        brownfield.validate_baseline(
                            baseline,
                            installation_id=installation_id,
                            release_sha256=release_sha256,
                        )

    def test_noncanonical_json_duplicate_keys_and_unknown_fields_are_rejected(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            baseline = _built(root, [_issue()])
            canonical = brownfield.canonical_json(baseline)

            with self.assertRaisesRegex(brownfield.BrownfieldError, "not canonical"):
                brownfield.validate_baseline(
                    json.dumps(baseline, indent=2).encode("utf-8"),
                    installation_id=INSTALLATION_ID,
                    release_sha256=RELEASE_SHA256,
                )
            duplicate = canonical.replace(b'{"binding":', b'{"schema":1,"binding":', 1)
            with self.assertRaisesRegex(brownfield.BrownfieldError, "duplicate JSON key"):
                brownfield.validate_baseline(
                    duplicate,
                    installation_id=INSTALLATION_ID,
                    release_sha256=RELEASE_SHA256,
                )
            unknown = dict(baseline)
            unknown["repository_sha256"] = "b" * 64
            with self.assertRaisesRegex(brownfield.BrownfieldError, "unknown"):
                brownfield.validate_baseline(
                    unknown,
                    installation_id=INSTALLATION_ID,
                    release_sha256=RELEASE_SHA256,
                )

    def test_unknown_schema_and_issue_fields_are_rejected(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            baseline = _built(root, [_issue()])
            wrong_schema = dict(baseline)
            wrong_schema["schema"] = 2
            with self.assertRaisesRegex(brownfield.BrownfieldError, "unsupported"):
                brownfield.validate_baseline(
                    wrong_schema,
                    installation_id=INSTALLATION_ID,
                    release_sha256=RELEASE_SHA256,
                )

            issue = _issue()
            issue["severity"] = "warning"
            with self.assertRaisesRegex(brownfield.BrownfieldError, "unknown severity"):
                _built(root, [issue])

            non_string_field = dict(_issue())
            non_string_field[7] = "not a field name"
            with self.assertRaisesRegex(brownfield.BrownfieldError, "must be strings"):
                _built(root, [non_string_field])

    def test_duplicate_issue_identity_is_rejected(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            issue = _issue()
            with self.assertRaisesRegex(brownfield.BrownfieldError, "duplicate issue"):
                _built(root, [issue, dict(issue)])

    def test_global_missing_directory_and_traversal_paths_are_never_baselined(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            (root / "scripts" / "folder").mkdir()
            invalid = [
                "",
                ".",
                "../player.gd",
                "scripts/../player.gd",
                "scripts\\player.gd",
                "C:/project/player.gd",
                "/project/player.gd",
                "scripts/missing.gd",
                "scripts/folder",
            ]
            for path in invalid:
                with self.subTest(path=path):
                    with self.assertRaises(brownfield.BrownfieldError):
                        _built(root, [_issue(path)])

    def test_unsafe_casing_and_case_collision_are_rejected(self) -> None:
        with _Scratch() as root:
            actual = _write(root, "scripts/Player.gd", b"old problem\n")
            with self.assertRaisesRegex(brownfield.BrownfieldError, "unsafe casing"):
                _built(root, [_issue("scripts/player.gd")])

            first = root / "Thing.gd"
            second = root / "thing.gd"
            with mock.patch.object(
                Path, "iterdir", autospec=True, return_value=iter([first, second])
            ):
                with self.assertRaisesRegex(brownfield.BrownfieldError, "case collision"):
                    _built(root, [_issue("THING.gd")])
            self.assertTrue(actual.is_file())

    def test_symlink_or_reparse_file_is_rejected(self) -> None:
        with _Scratch() as root:
            target = _write(root, "scripts/target.gd", b"old problem\n")
            linked = root / "scripts" / "linked.gd"
            try:
                linked.symlink_to(target)
            except OSError:
                linked.write_bytes(target.read_bytes())
                path_type = type(linked)
                original = path_type.lstat

                def redirected_lstat(
                    path: Path, *args: object, **kwargs: object
                ):
                    info = original(path, *args, **kwargs)
                    if path == linked:
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
                ):
                    with self.assertRaisesRegex(
                        brownfield.BrownfieldError, "redirected"
                    ):
                        _built(root, [_issue("scripts/linked.gd")])
                return
            with self.assertRaisesRegex(brownfield.BrownfieldError, "redirected"):
                _built(root, [_issue("scripts/linked.gd")])

    def test_reparse_attribute_is_rejected_without_host_symlink_support(self) -> None:
        with _Scratch() as root:
            affected = _write(root, "scripts/player.gd", b"old problem\n")
            path_type = type(affected)
            original = path_type.lstat

            def fake_lstat(path: Path, *args: object, **kwargs: object):
                info = original(path, *args, **kwargs)
                if path == affected:
                    redirected = mock.Mock(wraps=info)
                    redirected.st_mode = info.st_mode
                    redirected.st_file_attributes = 0x0400
                    return redirected
                return info

            with mock.patch.object(
                path_type, "lstat", autospec=True, side_effect=fake_lstat
            ):
                with self.assertRaisesRegex(brownfield.BrownfieldError, "redirected"):
                    _built(root, [_issue()])

    def test_conflicting_stored_hashes_for_one_file_are_rejected(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            baseline = _built(
                root,
                [_issue(code="first"), _issue(code="second")],
            )
            baseline["issues"][1]["file_sha256"] = "b" * 64

            with self.assertRaisesRegex(brownfield.BrownfieldError, "conflicting"):
                brownfield.validate_baseline(
                    baseline,
                    installation_id=INSTALLATION_ID,
                    release_sha256=RELEASE_SHA256,
                )

    def test_issue_and_file_limits_fail_closed(self) -> None:
        with _Scratch() as root:
            path = _write(root, "scripts/player.gd", b"12")
            with mock.patch.object(brownfield, "MAX_ISSUES", 1):
                with self.assertRaisesRegex(brownfield.BrownfieldError, "exceeds 1 issues"):
                    _built(
                        root,
                        [_issue(code="first"), _issue(code="second")],
                    )
            with mock.patch.object(brownfield, "MAX_FILE_BYTES", 1):
                with self.assertRaisesRegex(brownfield.BrownfieldError, "exceeds 1 bytes"):
                    _built(root, [_issue()])
            self.assertEqual(b"12", path.read_bytes())

    def test_line_hash_and_binding_shapes_fail_closed(self) -> None:
        with _Scratch() as root:
            _write(root, "scripts/player.gd", b"old problem\n")
            for key, value in (
                ("line", True),
                ("line", -1),
                ("message_sha256", "A" * 64),
                ("message_sha256", "a" * 63),
            ):
                issue = _issue()
                issue[key] = value
                with self.subTest(key=key, value=value):
                    with self.assertRaises(brownfield.BrownfieldError):
                        _built(root, [issue])
            with self.assertRaisesRegex(brownfield.BrownfieldError, "installation_id"):
                brownfield.build_baseline(
                    root,
                    [],
                    installation_id="not-an-installation",
                    release_sha256=RELEASE_SHA256,
                )
            with self.assertRaisesRegex(brownfield.BrownfieldError, "release_sha256"):
                brownfield.build_baseline(
                    root,
                    [],
                    installation_id=INSTALLATION_ID,
                    release_sha256="short",
                )


if __name__ == "__main__":
    unittest.main()
