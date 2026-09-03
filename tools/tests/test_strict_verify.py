#!/usr/bin/env python3
"""Synthetic orchestration tests for strict verification.

No test in this module starts Godot, a browser, the real gate, or a network
operation.  The fake runner makes ordering and fail-closed interpretation
observable without creating the native crash popups this verifier is intended
to keep serialized.
"""
from __future__ import annotations

import ast
import contextlib
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
from unittest import mock

from tools import release, strict_verify

REPOSITORY = Path(__file__).resolve().parents[2]


def _unittest_skip_lines(tree: ast.AST) -> list[int]:
    """Return every runtime or decorated unittest skip call in one test AST."""
    skip_names = {"skip", "skipIf", "skipUnless", "skipTest", "SkipTest"}
    lines: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if isinstance(function, ast.Name) and function.id in skip_names:
            lines.add(node.lineno)
        elif isinstance(function, ast.Attribute) and (
            function.attr == "skipTest"
            or (
                isinstance(function.value, ast.Name)
                and function.value.id == "unittest"
                and function.attr in skip_names
            )
        ):
            lines.add(node.lineno)
    return sorted(lines)


def _scratch_parent() -> Path:
    configured = os.environ.get("KIT_TEST_TMPDIR", "").strip()
    return Path(configured) if configured else REPOSITORY / ".checklogs"


def _doctor(*, complete: bool = True) -> str:
    state = "OK" if complete else "MISSING"
    return json.dumps(
        {
            "ok": complete,
            "exit_code": 0 if complete else 3,
            "status": "ready" if complete else "setup_incomplete",
            "blocking": [] if complete else ["gdtoolkit"],
            "bootstrap": {
                "complete": complete,
                "results": [
                    {
                        "name": "python",
                        "state": "OK",
                        "advisory": False,
                    },
                    {
                        "name": "gdtoolkit",
                        "state": state,
                        "advisory": False,
                    },
                    {
                        "name": "editor-settings",
                        "state": "MISSING",
                        "advisory": True,
                    },
                ],
            },
        }
    )


class FakeRunner:
    def __init__(
        self,
        *,
        source: strict_verify.ProcessOutcome | None = None,
        doctor: strict_verify.ProcessOutcome | None = None,
        gate: strict_verify.ProcessOutcome | None = None,
        unit: strict_verify.ProcessOutcome | None = None,
        browser: strict_verify.ProcessOutcome | None = None,
        archive_one: bytes = b"deterministic-release",
        archive_two: bytes | None = None,
    ) -> None:
        self.source = source or strict_verify.ProcessOutcome(0, "")
        self.doctor = doctor or strict_verify.ProcessOutcome(0, _doctor())
        self.gate = gate or strict_verify.ProcessOutcome(
            0, "PASS  integrity\nPASS  typecheck\nGATE PASSED\n"
        )
        self.unit = unit or strict_verify.ProcessOutcome(
            0, "", "test_one ... ok\n\nRan 1 test in 0.001s\n\nOK\n"
        )
        self.browser = browser or strict_verify.ProcessOutcome(
            0, "\n4/4 browser checks passed\n"
        )
        self.archive_one = archive_one
        self.archive_two = archive_one if archive_two is None else archive_two
        self.commands: list[list[str]] = []
        self.environments: list[dict[str, str]] = []
        self.release_builds = 0
        self.release_operations: list[str] = []

    def __call__(
        self,
        command: strict_verify.Sequence[str],
        _cwd: Path,
        _timeout: int,
        environment: strict_verify.Mapping[str, str],
    ) -> strict_verify.ProcessOutcome:
        argv = [str(part) for part in command]
        self.commands.append(argv)
        self.environments.append(dict(environment))
        if "KIT_STRICT_VERIFY" not in environment:
            raise AssertionError("strict environment marker was not supplied")
        if Path(argv[0]).name.lower() in ("git", "git.exe"):
            return self.source
        self.assert_isolated(argv)
        mode = argv[6]
        script_name = Path(argv[7]).name
        child_arguments = argv[11:]
        if mode == "module" and child_arguments[0] == "unittest":
            return self.unit
        if script_name == "kit.py":
            return self.doctor
        if script_name == "check.py":
            return self.gate
        if script_name == "browser_check.py":
            return self.browser
        if script_name == "release.py":
            operation = child_arguments[0]
            self.release_operations.append(operation)
            archive = Path(child_arguments[1])
            if operation in ("build", "materialize"):
                content = self.archive_one if self.release_builds == 0 else self.archive_two
                self.release_builds += 1
                archive.write_bytes(content)
            digest = strict_verify.hashlib.sha256(
                archive.read_bytes() if archive.is_file() else b""
            ).hexdigest()
            return strict_verify.ProcessOutcome(
                0, json.dumps({"ok": True, "archive_sha256": digest})
            )
        raise AssertionError(f"unexpected command: {argv}")

    @staticmethod
    def assert_isolated(argv: list[str]) -> None:
        if argv[1:5] != ["-B", "-I", "-S", "-c"]:
            raise AssertionError(f"Python child is not isolated: {argv}")
        if len(argv) < 11 or argv[6] not in ("script", "module"):
            raise AssertionError(f"isolated child receipt is malformed: {argv}")


class StrictFixture(unittest.TestCase):
    def setUp(self) -> None:
        scratch = _scratch_parent()
        scratch.mkdir(exist_ok=True)
        self.root = scratch / f"strict-verify-test-{uuid.uuid4().hex}"
        self.create_project(self.root)

    @staticmethod
    def create_project(root: Path) -> None:
        root.mkdir(parents=True)
        (root / "tools" / "tests").mkdir(parents=True)
        (root / ".agent-kit.json").write_text(
            '{"kind":"portable-agent-kit-root","schema":1}\n', encoding="utf-8"
        )
        (root / "kit.config.json").write_text(
            '{"schema":1,"game_root":"src","runtime_root":".kit/runtime"}\n',
            encoding="utf-8",
        )
        for relative in (
            "kit.py",
            "check.py",
            "tools/tests/browser_check.py",
            "tools/release.py",
        ):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# synthetic fixture\n", encoding="utf-8")
        (root / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        (root / "LICENSE").write_text(
            "Approved synthetic license.\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        resolved = self.root.resolve()
        scratch = _scratch_parent().resolve()
        if (resolved.parent != scratch
                or not resolved.name.startswith("strict-verify-test-")):
            raise AssertionError(f"refusing to remove unexpected scratch path: {resolved}")
        shutil.rmtree(resolved)

    def run_with(self, runner: FakeRunner) -> tuple[dict, int]:
        return strict_verify.run_strict(self.root, runner=runner)

    def enable_maintainer_fixture(self, content: str | None = None) -> None:
        marker = self.root / strict_verify.MAINTAINER_FIXTURE_PATH
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            strict_verify.MAINTAINER_FIXTURE_CONTENT if content is None else content,
            encoding="utf-8",
        )

    def managed_core(self) -> Path:
        core = self.root / "managed-core"
        (core / "tools" / "tests").mkdir(parents=True)
        for relative in (
            "kit.py",
            "check.py",
            "tools/tests/browser_check.py",
            "tools/release.py",
        ):
            path = core / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# synthetic managed core\n", encoding="utf-8")
        (core / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        (core / "LICENSE").write_text(
            "Approved synthetic license.\n", encoding="utf-8"
        )
        return core

    def external_test_workspace(self) -> tuple[Path, mock.Mock]:
        temporary_root = Path(tempfile.gettempdir()).resolve(strict=True)
        workspace = temporary_root / (
            strict_verify.STRICT_WORKSPACE_PREFIX + uuid.uuid4().hex[:8]
        )
        self.addCleanup(shutil.rmtree, workspace, True)

        def create(*, prefix: str, dir: Path) -> str:
            self.assertEqual(strict_verify.STRICT_WORKSPACE_PREFIX, prefix)
            self.assertEqual(temporary_root, Path(dir))
            workspace.mkdir()
            return str(workspace)

        return workspace, mock.Mock(side_effect=create)

    def deep_source_project(self) -> Path:
        prefix = "windows-long-source-"
        padding = max(16, 150 - len(str(self.root)) - len(prefix) - 1)
        project = self.root / (prefix + ("x" * padding))
        self.create_project(project)
        return project


class StrictSuccessTests(StrictFixture):
    def test_runs_all_proofs_sequentially_and_retains_complete_logs(self) -> None:
        runner = FakeRunner()
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_OK)
        self.assertTrue(report["ok"])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(
            [stage["name"] for stage in report["stages"]],
            [
                "source-state",
                "doctor",
                "gate",
                "unit-tests",
                "browser-check",
                "release-build-1",
                "release-build-2",
                "release-verify-1",
                "release-verify-2",
                "release-smoke",
                "release-reproducibility",
            ],
        )
        self.assertTrue(all(stage["status"] == "passed" for stage in report["stages"]))
        self.assertEqual(
            "no-exact-authority-event",
            report["authority_receipt"]["receipt_trust"],
        )
        self.assertNotIn("authenticated", json.dumps(report["authority_receipt"]).lower())
        self.assertEqual(len(runner.commands), 10)
        self.assertTrue(Path(runner.commands[0][0]).is_absolute())
        self.assertEqual(
            "git.exe" if os.name == "nt" else "git",
            Path(runner.commands[0][0]).name,
        )
        self.assertIn("doctor", runner.commands[1])
        self.assertEqual(runner.commands[2][7], str(self.root / "check.py"))
        self.assertIn("unittest", runner.commands[3])
        self.assertTrue(runner.commands[4][7].endswith("browser_check.py"))
        self.assertEqual(
            ["build", "build", "verify", "verify", "smoke"],
            runner.release_operations,
        )

        report_path = self.root / report["report_path"]
        retained = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(retained["run_id"], report["run_id"])
        for stage in report["stages"]:
            if "stdout_log" in stage:
                self.assertTrue((self.root / stage["stdout_log"]).is_file())
                self.assertTrue((self.root / stage["stderr_log"]).is_file())
        self.assertFalse(list(report_path.parent.glob(".*.tmp")))

    def test_managed_core_selects_materialization_without_source_git(
        self,
    ) -> None:
        core = self.managed_core()
        runner = FakeRunner()
        workspace, create_workspace = self.external_test_workspace()

        with mock.patch.object(strict_verify, "ROOT", self.root), mock.patch.object(
            strict_verify, "CORE_ROOT", core
        ), mock.patch.object(
            strict_verify.tempfile, "mkdtemp", create_workspace
        ), mock.patch.object(
            strict_verify.release_contract,
            "read_verified_directory",
            side_effect=[
                ({"archive_sha256": "a" * 64}, {}),
                ({"archive_sha256": "a" * 64}, {}),
                ({"archive_sha256": "a" * 64}, {}),
            ],
        ) as verify_directory:
            report, code = strict_verify.run_strict(self.root, runner=runner)

        self.assertEqual(strict_verify.EXIT_OK, code)
        self.assertEqual(
            ["materialize", "materialize", "verify", "verify", "smoke"],
            runner.release_operations,
        )
        self.assertEqual(
            [
                "release-materialize-1",
                "release-materialize-2",
            ],
            [
                stage["name"]
                for stage in report["stages"]
                if stage["name"].startswith("release-materialize")
            ],
        )
        self.assertNotIn(
            "release-build-1", [stage["name"] for stage in report["stages"]]
        )
        unit_command = runner.commands[3]
        browser_command = runner.commands[4]
        self.assertTrue(
            any(str(workspace / "c") in argument for argument in unit_command)
        )
        self.assertTrue(
            any(str(workspace / "c") in argument for argument in browser_command)
        )
        for environment in runner.environments[3:5]:
            self.assertEqual(str(workspace / "t"), environment["KIT_TEST_TMPDIR"])
            self.assertNotIn(
                strict_verify.managed_launcher.PROJECT_ROOT_ENV,
                environment,
            )
            self.assertNotIn(
                strict_verify.managed_launcher.CORE_ROOT_ENV,
                environment,
            )
            self.assertNotIn("KIT_LIFECYCLE_CHECK", environment)
        self.assertEqual(
            [mock.call(core), mock.call(workspace / "c"), mock.call(core)],
            verify_directory.call_args_list,
        )
        copied_path = verify_directory.call_args_list[1].args[0]
        self.assertEqual("c", copied_path.name)
        self.assertEqual(workspace, copied_path.parent)
        self.assertFalse((core / "src").exists())
        self.assertFalse(workspace.exists())
        for command in runner.commands[5:10]:
            self.assertTrue(
                any(str(workspace / "r") in argument for argument in command)
            )
        logs = self.root / report["logs_directory"]
        self.assertTrue((logs / "unit-tests.stdout.log").is_file())
        self.assertTrue((logs / "report.json").is_file())

    def test_source_scratch_avoids_windows_long_nested_test_paths(self) -> None:
        project = self.deep_source_project()
        runner = FakeRunner()
        workspace, create_workspace = self.external_test_workspace()

        with mock.patch.object(
            strict_verify.tempfile, "mkdtemp", create_workspace
        ):
            report, code = strict_verify.run_strict(project, runner=runner)

        self.assertEqual(strict_verify.EXIT_OK, code)
        test_scratch = Path(runner.environments[3]["KIT_TEST_TMPDIR"])
        self.assertEqual(workspace / "t", test_scratch)
        self.assertEqual(
            ["-v", *release.SHIPPED_TEST_MODULES],
            runner.commands[3][12:],
        )
        self.assertNotIn("discover", runner.commands[3])
        for module in release.SOURCE_ONLY_TEST_MODULES:
            self.assertNotIn(module, runner.commands[3])
        self.assertFalse(test_scratch.is_relative_to(project))
        legacy_scratch = (
            project
            / ".kit"
            / "runtime"
            / "verification"
            / "runs"
            / report["run_id"]
            / "test-scratch"
        )
        nested_suffix = (
            Path("board-api-test-" + ("a" * 32))
            / ".kit"
            / "runtime"
            / "dispatch"
            / "workspaces"
            / ("run-" + ("b" * 32))
        )
        self.assertGreaterEqual(len(str(legacy_scratch / nested_suffix)), 260)
        self.assertLess(len(str(test_scratch / nested_suffix)), 260)
        self.assertFalse(workspace.exists())
        self.assertTrue((project / report["logs_directory"] / "report.json").is_file())

    def test_release_scratch_avoids_windows_long_smoke_paths(self) -> None:
        project = self.deep_source_project()
        runner = FakeRunner()
        workspace, create_workspace = self.external_test_workspace()

        with mock.patch.object(
            strict_verify.tempfile, "mkdtemp", create_workspace
        ):
            report, code = strict_verify.run_strict(project, runner=runner)

        self.assertEqual(strict_verify.EXIT_OK, code)
        smoke_command = next(
            command
            for command in runner.commands
            if len(command) > 13
            and Path(command[7]).name == "release.py"
            and command[11] == "smoke"
        )
        smoke_workspace = Path(smoke_command[13])
        self.assertEqual(workspace / "r" / "smoke", smoke_workspace)
        self.assertFalse(smoke_workspace.is_relative_to(project))
        legacy_smoke = (
            project
            / ".kit"
            / "runtime"
            / "verification"
            / "runs"
            / report["run_id"]
            / "release-workspace"
            / "smoke"
        )
        release_member = Path(
            ".agents/skills/godot-headless-verification/SKILL.md"
        )
        self.assertGreaterEqual(len(str(legacy_smoke / release_member)), 260)
        self.assertLess(len(str(smoke_workspace / release_member)), 260)
        self.assertFalse(workspace.exists())

    def test_managed_test_copy_mismatch_is_refused_and_removed(self) -> None:
        core = self.managed_core()
        workspace, create_workspace = self.external_test_workspace()

        with mock.patch.object(
            strict_verify.tempfile, "mkdtemp", create_workspace
        ), mock.patch.object(
            strict_verify.release_contract,
            "read_verified_directory",
            side_effect=[
                ({"archive_sha256": "a" * 64}, {}),
                ({"archive_sha256": "b" * 64}, {}),
            ],
        ), self.assertRaisesRegex(
            strict_verify.StrictVerifyError,
            "does not match the authenticated active release",
        ):
            with strict_verify._isolated_strict_workspace(  # noqa: SLF001
                core,
                self.root,
            ):
                self.fail("mismatched managed test copy was executed")

        self.assertFalse(workspace.exists())

    def test_short_workspace_refuses_project_overlap_and_removes_exact_empty_root(
        self,
    ) -> None:
        core = self.managed_core()
        workspace = self.root / (strict_verify.STRICT_WORKSPACE_PREFIX + "overlap")

        def create(*, prefix: str, dir: Path) -> str:
            self.assertEqual(strict_verify.STRICT_WORKSPACE_PREFIX, prefix)
            self.assertEqual(self.root, Path(dir))
            workspace.mkdir()
            return str(workspace)

        with mock.patch.object(
            strict_verify.tempfile, "gettempdir", return_value=str(self.root)
        ), mock.patch.object(
            strict_verify.tempfile, "mkdtemp", side_effect=create
        ), self.assertRaisesRegex(
            strict_verify.StrictVerifyError,
            "overlaps the project or active core",
        ):
            with strict_verify._short_strict_workspace(  # noqa: SLF001
                self.root, core
            ):
                self.fail("overlapping strict workspace was used")

        self.assertFalse(workspace.exists())

    def test_short_workspace_refuses_cleanup_after_root_identity_changes(
        self,
    ) -> None:
        core = self.managed_core()
        workspace, create_workspace = self.external_test_workspace()

        try:
            with mock.patch.object(
                strict_verify.tempfile, "mkdtemp", create_workspace
            ), mock.patch.object(
                strict_verify,
                "_directory_identity",
                side_effect=[(1, 1), (2, 2)],
            ), self.assertRaisesRegex(
                strict_verify.StrictVerifyError,
                "changed strict workspace",
            ):
                with strict_verify._short_strict_workspace(  # noqa: SLF001
                    self.root, core
                ) as selected:
                    self.assertEqual(workspace, selected)

            self.assertTrue(workspace.is_dir())
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def test_short_workspace_cleans_an_empty_root_when_resolve_fails(self) -> None:
        core = self.managed_core()
        workspace, create_workspace = self.external_test_workspace()
        real_resolve = Path.resolve

        def resolve(selected: Path, strict: bool = False) -> Path:
            if selected == workspace:
                raise OSError("synthetic canonicalization failure")
            return real_resolve(selected, strict=strict)

        with mock.patch.object(
            strict_verify.tempfile, "mkdtemp", create_workspace
        ), mock.patch.object(
            Path, "resolve", autospec=True, side_effect=resolve
        ), self.assertRaisesRegex(
            strict_verify.StrictVerifyError,
            "short strict workspace is unavailable",
        ):
            with strict_verify._short_strict_workspace(  # noqa: SLF001
                self.root, core
            ):
                self.fail("unresolved strict workspace was used")

        self.assertFalse(os.path.lexists(workspace))

    def test_strict_workspace_cleanup_removes_a_readonly_git_object(self) -> None:
        workspace, _create_workspace = self.external_test_workspace()
        git_object = workspace / ".git" / "objects" / "aa" / "object"
        git_object.parent.mkdir(parents=True)
        git_object.write_bytes(b"git object")
        git_object.chmod(stat.S_IREAD)
        identity = strict_verify._directory_identity(workspace)  # noqa: SLF001

        strict_verify._remove_exact_strict_workspace(  # noqa: SLF001
            workspace, workspace.parent, identity
        )

        self.assertFalse(os.path.lexists(workspace))

    def test_strict_workspace_cleanup_retries_transient_permission_failures(
        self,
    ) -> None:
        workspace, _create_workspace = self.external_test_workspace()
        workspace.mkdir()
        (workspace / "object").write_bytes(b"git object")
        identity = strict_verify._directory_identity(workspace)  # noqa: SLF001
        real_rmtree = shutil.rmtree
        attempts = 0

        def transient(path: Path, *, onerror: object) -> None:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise PermissionError(13, "transient handle", str(path))
            real_rmtree(path, onerror=onerror)

        with mock.patch.object(
            strict_verify.shutil, "rmtree", side_effect=transient
        ), mock.patch.object(strict_verify.time, "sleep") as sleep:
            strict_verify._remove_exact_strict_workspace(  # noqa: SLF001
                workspace, workspace.parent, identity
            )

        self.assertEqual(3, attempts)
        self.assertEqual([mock.call(0.05), mock.call(0.1)], sleep.call_args_list)
        self.assertFalse(os.path.lexists(workspace))

    def test_strict_workspace_cleanup_refuses_redirected_paths(self) -> None:
        workspace, _create_workspace = self.external_test_workspace()
        workspace.mkdir()
        child = workspace / "object"
        child.write_bytes(b"git object")
        outside = workspace.parent / f"outside-{uuid.uuid4().hex}"
        outside.write_bytes(b"human data")
        self.addCleanup(outside.unlink, missing_ok=True)
        identity = strict_verify._directory_identity(workspace)  # noqa: SLF001
        permission = PermissionError(13, "read only")
        captured: dict[str, object] = {}

        def expose_handler(path: Path, *, onerror: object) -> None:
            del path
            captured["handler"] = onerror
            raise OSError("stop after capture")

        with mock.patch.object(
            strict_verify.shutil, "rmtree", side_effect=expose_handler
        ), self.assertRaises(strict_verify.StrictVerifyError):
            strict_verify._remove_exact_strict_workspace(  # noqa: SLF001
                workspace, workspace.parent, identity
            )
        handler = captured["handler"]
        self.assertTrue(callable(handler))

        with mock.patch.object(
            strict_verify.os, "chmod"
        ) as chmod, self.assertRaisesRegex(OSError, "escaped its exact root"):
            handler(  # type: ignore[operator]
                os.unlink,
                str(outside),
                (PermissionError, permission, None),
            )
        chmod.assert_not_called()

        redirected = mock.Mock(
            st_mode=stat.S_IFLNK | stat.S_IREAD,
            st_dev=1,
            st_ino=2,
            st_size=10,
            st_mtime_ns=3,
            st_nlink=1,
            st_file_attributes=0,
        )
        real_lstat = Path.lstat

        def lstat(selected: Path) -> object:
            return redirected if selected == child else real_lstat(selected)

        with mock.patch.object(
            Path, "lstat", autospec=True, side_effect=lstat
        ), mock.patch.object(
            strict_verify.os, "chmod"
        ) as chmod, self.assertRaisesRegex(OSError, "redirected or shared"):
            handler(  # type: ignore[operator]
                os.unlink,
                str(child),
                (PermissionError, permission, None),
            )
        chmod.assert_not_called()

    def test_strict_workspace_cleanup_refuses_exhausted_retries(self) -> None:
        workspace, _create_workspace = self.external_test_workspace()
        workspace.mkdir()
        identity = strict_verify._directory_identity(workspace)  # noqa: SLF001
        failure = PermissionError(13, "persistent handle", str(workspace))

        with mock.patch.object(
            strict_verify.shutil, "rmtree", side_effect=failure
        ) as remove, mock.patch.object(
            strict_verify.time, "sleep"
        ) as sleep, self.assertRaisesRegex(
            strict_verify.StrictVerifyError, "strict workspace cleanup failed"
        ):
            strict_verify._remove_exact_strict_workspace(  # noqa: SLF001
                workspace, workspace.parent, identity
            )

        self.assertEqual(
            len(strict_verify.STRICT_CLEANUP_RETRY_DELAYS) + 1,
            remove.call_count,
        )
        self.assertEqual(
            [
                mock.call(delay)
                for delay in strict_verify.STRICT_CLEANUP_RETRY_DELAYS
            ],
            sleep.call_args_list,
        )

    def test_exact_maintainer_fixture_allows_only_project_state_skips(self) -> None:
        self.enable_maintainer_fixture()
        runner = FakeRunner(
            gate=strict_verify.ProcessOutcome(
                0,
                "PASS  integrity\n"
                "SKIP  shape (absent)\n"
                "SKIP  design\n"
                "SKIP  conformance\n"
                "GATE PASSED\n",
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_OK)
        gate = next(item for item in report["stages"] if item["name"] == "gate")
        self.assertEqual(gate["status"], "passed")
        self.assertIn("canonical maintainer", gate["reason"])

    def test_crlf_maintainer_skips_are_classified(self) -> None:
        self.enable_maintainer_fixture()
        runner = FakeRunner(
            gate=strict_verify.ProcessOutcome(
                0,
                "PASS  integrity\r\n"
                "SKIP  shape (absent)\r\n"
                "SKIP  design\r\n"
                "SKIP  conformance\r\n"
                "GATE PASSED\r\n",
            )
        )

        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_OK)
        gate = next(item for item in report["stages"] if item["name"] == "gate")
        self.assertEqual(gate["status"], "passed")
        self.assertIn("SKIP design", gate["reason"])

    def test_only_the_authoritative_gate_receives_the_public_nonce(self) -> None:
        runner = FakeRunner()
        with mock.patch.dict(
            strict_verify.os.environ,
            {
                "KIT_VERIFY_NONCE": "a" * 32,
                "KIT_VERIFY_AUTH_KEY": "b" * 64,
                "KIT_VERIFY_REPOSITORY_SHA256": "c" * 64,
                "KIT_NATIVE_RETRY_TOKEN": "d" * 64,
            },
        ):
            _report, code = self.run_with(runner)

        self.assertEqual(strict_verify.EXIT_OK, code)
        receipt_keys = set(strict_verify.GATE_RECEIPT_ENV)
        self.assertFalse(receipt_keys.intersection(runner.environments[0]))
        self.assertFalse(receipt_keys.intersection(runner.environments[1]))
        self.assertEqual("a" * 32, runner.environments[2]["KIT_VERIFY_NONCE"])
        self.assertEqual("b" * 64, runner.environments[2]["KIT_VERIFY_AUTH_KEY"])
        self.assertEqual(
            "c" * 64,
            runner.environments[2]["KIT_VERIFY_REPOSITORY_SHA256"],
        )
        self.assertEqual(
            "d" * 64,
            runner.environments[2]["KIT_NATIVE_RETRY_TOKEN"],
        )
        for environment in runner.environments[3:]:
            self.assertFalse(receipt_keys.intersection(environment))


class StrictFailureTests(StrictFixture):
    def test_gate_skip_is_a_failure_even_when_gate_exits_zero(self) -> None:
        runner = FakeRunner(
            gate=strict_verify.ProcessOutcome(
                0, "PASS  integrity\nSKIP  format\nGATE PASSED\n"
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        stage = next(item for item in report["stages"] if item["name"] == "gate")
        self.assertEqual(stage["status"], "failed")
        self.assertIn("SKIP", stage["reason"])

    def test_crlf_gate_skip_is_a_failure_even_when_gate_exits_zero(self) -> None:
        runner = FakeRunner(
            gate=strict_verify.ProcessOutcome(
                0, "PASS  integrity\r\nSKIP  format\r\nGATE PASSED\r\n"
            )
        )

        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        stage = next(item for item in report["stages"] if item["name"] == "gate")
        self.assertEqual(stage["status"], "failed")
        self.assertIn("SKIP format", stage["reason"])

    def test_maintainer_fixture_does_not_allow_any_other_skip(self) -> None:
        self.enable_maintainer_fixture()
        runner = FakeRunner(
            gate=strict_verify.ProcessOutcome(
                0,
                "SKIP  shape (absent)\n"
                "SKIP  design\n"
                "SKIP  conformance\n"
                "SKIP  format\n"
                "GATE PASSED\n",
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        gate = next(item for item in report["stages"] if item["name"] == "gate")
        self.assertEqual(gate["status"], "failed")
        self.assertIn("SKIP format", gate["reason"])

    def test_wrong_maintainer_marker_content_fails_closed(self) -> None:
        self.enable_maintainer_fixture("not-the-contract\n")
        runner = FakeRunner(
            gate=strict_verify.ProcessOutcome(
                0, "SKIP  shape (absent)\nGATE PASSED\n"
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        gate = next(item for item in report["stages"] if item["name"] == "gate")
        self.assertEqual(gate["status"], "failed")

    def test_unit_test_skip_is_a_failure(self) -> None:
        runner = FakeRunner(
            unit=strict_verify.ProcessOutcome(
                0,
                "",
                "test_optional ... skipped 'missing tool'\n\n"
                "Ran 1 test in 0.001s\n\nOK (skipped=1)\n",
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        stage = next(item for item in report["stages"] if item["name"] == "unit-tests")
        self.assertEqual(stage["status"], "failed")
        self.assertIn("skipped", stage["reason"])

    def test_expected_unit_test_failure_is_a_failure(self) -> None:
        runner = FakeRunner(
            unit=strict_verify.ProcessOutcome(
                0,
                "",
                "test_known_gap ... expected failure\n\n"
                "Ran 1 test in 0.001s\n\nOK (expected failures=1)\n",
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        stage = next(item for item in report["stages"] if item["name"] == "unit-tests")
        self.assertEqual(stage["status"], "failed")
        self.assertIn("skipped", stage["reason"])

    def test_browser_skip_is_an_explicit_blocker(self) -> None:
        runner = FakeRunner(
            browser=strict_verify.ProcessOutcome(
                0, "no Chromium-based browser found; browser check skipped\n"
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_BLOCKED)
        stage = next(item for item in report["stages"] if item["name"] == "browser-check")
        self.assertEqual(stage["status"], "blocked")

    def test_timeout_fails_and_refuses_to_start_later_processes(self) -> None:
        runner = FakeRunner(
            gate=strict_verify.ProcessOutcome(
                None,
                "partial gate output\n",
                "partial gate error\n",
                1800.0,
                timed_out=True,
            )
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        self.assertEqual(len(runner.commands), 3)
        self.assertEqual(
            [item["status"] for item in report["stages"][:5]],
            ["passed", "passed", "failed", "not_run", "not_run"],
        )
        gate = report["stages"][2]
        self.assertEqual(
            (self.root / gate["stdout_log"]).read_text(encoding="utf-8"),
            "partial gate output\n",
        )

    def test_malformed_doctor_output_fails_closed(self) -> None:
        runner = FakeRunner(doctor=strict_verify.ProcessOutcome(0, "ready maybe\n"))
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        self.assertEqual(report["stages"][1]["status"], "failed")
        self.assertIn("not one JSON", report["stages"][1]["reason"])
        self.assertEqual(len(runner.commands), 2)

    def test_release_builds_must_be_byte_identical(self) -> None:
        runner = FakeRunner(archive_one=b"first", archive_two=b"second")
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_FAILED)
        stage = report["stages"][-1]
        self.assertEqual(stage["name"], "release-reproducibility")
        self.assertEqual(stage["status"], "failed")


class StrictBlockedTests(StrictFixture):
    def test_unavailable_git_becomes_a_blocked_source_stage(self) -> None:
        commands: list[list[str]] = []

        def runner(command, _cwd, _timeout, _environment):
            commands.append([str(item) for item in command])
            return strict_verify.ProcessOutcome(
                127, "", "trusted Git executable is unavailable\n"
            )

        with mock.patch.object(
            strict_verify.process_supervisor,
            "resolve_ordinary_executable",
            side_effect=ValueError("redirected"),
        ):
            report, code = self.run_with(runner)

        self.assertEqual(strict_verify.EXIT_BLOCKED, code)
        self.assertEqual(1, len(commands))
        self.assertEqual(["-B", "-I", "-S", "-c"], commands[0][1:5])
        self.assertEqual("blocked", report["stages"][0]["status"])
        self.assertTrue(all(
            stage["status"] == "not_run" for stage in report["stages"][1:]
        ))

    def test_missing_legal_metadata_is_blocked_not_skipped(self) -> None:
        (self.root / "LICENSE").unlink()
        runner = FakeRunner()
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_BLOCKED)
        self.assertEqual(len(runner.commands), 5)
        stage = report["stages"][-1]
        self.assertEqual(stage["name"], "release-metadata")
        self.assertEqual(stage["status"], "blocked")
        self.assertIn("LICENSE", stage["reason"])

    def test_incomplete_doctor_stops_before_any_mutating_capability(self) -> None:
        runner = FakeRunner(
            doctor=strict_verify.ProcessOutcome(1, _doctor(complete=False))
        )
        report, code = self.run_with(runner)

        self.assertEqual(code, strict_verify.EXIT_BLOCKED)
        self.assertEqual(len(runner.commands), 2)
        self.assertEqual(report["stages"][1]["status"], "blocked")
        self.assertTrue(all(
            stage["status"] == "not_run" for stage in report["stages"][2:]
        ))

    def test_dirty_source_state_stops_before_any_project_command(self) -> None:
        runner = FakeRunner(
            source=strict_verify.ProcessOutcome(0, " M tools/strict_verify.py\n")
        )
        report, code = self.run_with(runner)

        self.assertEqual(strict_verify.EXIT_BLOCKED, code)
        self.assertEqual(1, len(runner.commands))
        self.assertEqual("source-state", report["stages"][0]["name"])
        self.assertEqual("blocked", report["stages"][0]["status"])
        self.assertTrue(all(
            stage["status"] == "not_run" for stage in report["stages"][1:]
        ))


class StrictCliTests(unittest.TestCase):
    def test_json_cli_preserves_documented_exit_code(self) -> None:
        payload = {
            "schema": 1,
            "command": "strict-verify",
            "ok": False,
            "status": "blocked",
            "exit_code": strict_verify.EXIT_BLOCKED,
        }
        output = io.StringIO()
        with mock.patch.object(
            strict_verify, "run_strict", return_value=(payload, strict_verify.EXIT_BLOCKED)
        ), contextlib.redirect_stdout(output):
            code = strict_verify.main(["--json"])

        self.assertEqual(code, strict_verify.EXIT_BLOCKED)
        self.assertEqual(json.loads(output.getvalue()), payload)

    def test_default_runner_uses_shared_bounded_process_containment(self) -> None:
        supervised = strict_verify.process_supervisor.SupervisedResult(
            0, "ok\n", "", 0.25
        )
        with mock.patch.object(
            strict_verify.process_supervisor,
            "run_supervised",
            return_value=supervised,
        ) as run:
            outcome = strict_verify._default_runner(
                ["git", "status"],
                REPOSITORY,
                30,
                {"NO_COLOR": "1"},
            )

        self.assertEqual(0, outcome.returncode)
        self.assertEqual("ok\n", outcome.stdout)
        run.assert_called_once_with(
            ["git", "status"],
            cwd=REPOSITORY,
            timeout=30,
            environment={"NO_COLOR": "1"},
            capture_output=True,
            allow_child_breakaway=False,
        )

    def test_default_runner_surfaces_unverified_termination_as_launch_failure(self) -> None:
        supervised = strict_verify.process_supervisor.SupervisedResult(
            None,
            "partial",
            "",
            30.0,
            timed_out=True,
            termination_verified=False,
        )
        with mock.patch.object(
            strict_verify.process_supervisor,
            "run_supervised",
            return_value=supervised,
        ):
            outcome = strict_verify._default_runner(
                ["git", "status"], REPOSITORY, 30, {}
            )

        self.assertFalse(outcome.timed_out)
        self.assertIn("termination could not be verified", outcome.launch_error)


class StrictCiContractTests(unittest.TestCase):
    def test_skip_contract_detects_a_runtime_skip_call(self) -> None:
        tree = ast.parse(
            "class Case:\n"
            "    def test_runtime(self):\n"
            "        self.skipTest('missing tool')\n"
        )
        self.assertEqual([3], _unittest_skip_lines(tree))

    def test_shipped_suite_has_no_skips_hidden_by_the_local_platform(self) -> None:
        root = Path(__file__).resolve().parents[2]
        offenders: list[str] = []
        validation_tests = sorted(
            relative
            for relative in release.VALIDATION_FILES
            if relative.startswith("tools/tests/test_") and relative.endswith(".py")
        )
        for relative in validation_tests:
            path = root / relative
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            offenders.extend(
                f"{relative}:{line}" for line in _unittest_skip_lines(tree)
            )
        self.assertEqual(
            [], offenders,
            "strict rejects every unittest skip; shipped tests must assert a safe "
            "fallback when an optional host capability is absent",
        )

    def test_ci_runs_source_only_lifecycle_proofs_outside_public_self_test(self) -> None:
        root = Path(__file__).resolve().parents[2]
        workflow = (root / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        step = "Run source-only lifecycle proofs"

        self.assertIn(step, workflow)
        self.assertIn(
            "matrix.strict && hashFiles('tools/tests/test_lifecycle_e2e.py', "
            "'tools/tests/test_managed_consumer_strict_ci.py', "
            "'tools/tests/test_public_lifecycle_e2e.py') != ''",
            workflow,
        )
        self.assertIn("release.SOURCE_ONLY_TEST_MODULES", workflow)
        self.assertIn("result.skipped", workflow)
        self.assertGreater(
            workflow.index(step),
            workflow.index("Run kit control-plane tests through the Windows launcher"),
        )
        self.assertLess(
            workflow.index(step),
            workflow.index("Authenticate every canonical third-party lock source"),
        )

    def test_ci_uses_only_pinned_official_actions_and_authenticated_builds(self) -> None:
        root = Path(__file__).resolve().parents[2]
        workflow = (root / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        uses = re.findall(r"(?m)^\s*uses:\s*([^\s#]+)", workflow)
        self.assertEqual(
            uses,
            [
                "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                "actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97",
                "actions/setup-node@820762786026740c76f36085b0efc47a31fe5020",
            ],
        )
        self.assertTrue(all(re.fullmatch(r"actions/[a-z-]+@[0-9a-f]{40}", item)
                            for item in uses))
        self.assertIn("kit setup dependency gdtoolkit", workflow)
        self.assertIn("kit setup audit-dependencies", workflow)
        self.assertIn("node-version: 24.19.0", workflow)
        self.assertIn("python: 3.10.21", workflow)
        self.assertEqual(3, workflow.count("python: 3.14.7"))
        self.assertIn("python-version: ${{ matrix.python }}", workflow)
        self.assertIn("KIT_PYTHON={executable}", workflow)
        self.assertIn("Prove the public launcher uses the declared Python", workflow)
        self.assertIn("EXPECT_SETUP_COMPLETE: ${{ matrix.strict }}", workflow)
        self.assertIn(
            'expected_code = {"ready": 0, "needs_setup": 3}.get(status)',
            workflow,
        )
        self.assertIn(
            'expects_complete = os.environ["EXPECT_SETUP_COMPLETE"]', workflow
        )
        self.assertIn('expected = os.environ["EXPECTED_PYTHON"]', workflow)
        self.assertIn("if versions != [expected]", workflow)
        self.assertLess(
            workflow.index("Bind the public launcher to the declared Python"),
            workflow.index("kit self-test"),
        )
        self.assertIn("fetch-depth: 0", workflow)
        self.assertIn("persist-credentials: false", workflow)
        self.assertIn("github.com/godotengine/godot-builds/releases/download", workflow)
        self.assertIn("SHA512-SUMS.txt", workflow)
        self.assertIn("hashlib.sha512", workflow)
        self.assertIn("kit self-test", workflow)
        self.assertEqual(2, workflow.count("setup import"))
        self.assertIn("sh ./kit setup import", workflow)
        self.assertIn(r".\kit.cmd setup import", workflow)
        self.assertIn("kit verify --strict --json", workflow)
        self.assertLess(
            workflow.index("kit self-test"),
            workflow.index("Download and authenticate the official Godot build"),
        )
        self.assertLess(
            workflow.index("Download and authenticate the official Godot build"),
            workflow.index("setup import"),
        )
        self.assertLess(
            workflow.index("setup import"),
            workflow.index("kit verify --strict --json"),
        )
        self.assertNotIn("pip install", workflow)
        self.assertNotIn("python tools/strict_verify.py", workflow)
        self.assertNotIn("upload-artifact", workflow)
        self.assertIn("sys.dont_write_bytecode = True", workflow)
        self.assertIn('sys.path.insert(0, str(pathlib.Path(".").resolve()))', workflow)
        self.assertIn("from tools import runtime_paths", workflow)
        self.assertLess(
            workflow.index("sys.dont_write_bytecode = True"),
            workflow.index("from tools import runtime_paths"),
        )
        self.assertLess(
            workflow.index('sys.path.insert(0, str(pathlib.Path(".").resolve()))'),
            workflow.index("from tools import runtime_paths"),
        )
        self.assertIn(
            'runtime_paths.resolve(pathlib.Path(".")).runtime / "verification"',
            workflow,
        )
        evidence_step = workflow.split(
            "- name: Print retained strict evidence", 1
        )[1]
        self.assertIn("import os", evidence_step)
        self.assertNotIn('pathlib.Path(".kit/runtime/verification")', workflow)
        self.assertIn('(root / "strict-report.json").read_text', workflow)
        self.assertIn("if actual != expected", workflow)
        self.assertIn("if: matrix.strict", workflow)
        self.assertIn("if: ${{ always() && matrix.strict }}", workflow)

    def test_public_launchers_fail_closed_on_an_explicit_python_override(self) -> None:
        root = Path(__file__).resolve().parents[2]
        windows = (root / "kit.cmd").read_text(encoding="utf-8")
        posix = (root / "kit").read_text(encoding="utf-8")

        self.assertLess(
            windows.index("if defined KIT_PYTHON"),
            windows.index('if exist "%SystemRoot%\\py.exe"'),
        )
        self.assertIn(
            '"%KIT_PYTHON%" -B -I -S -c "%KIT_BOOTSTRAP_CODE%"', windows
        )
        self.assertIn("KIT_PYTHON must name one absolute interpreter file", windows)
        self.assertIn("KIT_PYTHON must name one absolute interpreter file", windows)
        self.assertLess(
            posix.index('if [ -n "${KIT_PYTHON:-}" ]'),
            posix.index("for KIT_SYSTEM_PYTHON in"),
        )
        self.assertIn(
            'exec "$KIT_PYTHON" -B -I -S -c "$KIT_BOOTSTRAP_CODE"', posix
        )
        self.assertIn("KIT_PYTHON must name one absolute interpreter file", posix)
        self.assertIn("KIT_PYTHON does not name an executable interpreter file", posix)

    def test_ci_covers_all_three_desktop_operating_systems(self) -> None:
        root = Path(__file__).resolve().parents[2]
        workflow = (root / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        for runner in ("ubuntu-24.04", "windows-2025", "macos-15"):
            self.assertIn(f"os: {runner}", workflow)
        self.assertIn("Godot_v4.7.2-stable_linux.x86_64.zip", workflow)
        self.assertIn("Godot_v4.7.2-stable_win64.exe.zip", workflow)
        self.assertIn("Godot_v4.7.2-stable_macos.universal.zip", workflow)


if __name__ == "__main__":
    unittest.main(verbosity=2)
