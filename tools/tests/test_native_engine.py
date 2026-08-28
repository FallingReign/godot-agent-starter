#!/usr/bin/env python3
"""Focused, fully mocked proofs for the shared Godot process boundary."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import types
import unittest
import uuid
from pathlib import Path
from typing import Iterator
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT))

import bootstrap  # noqa: E402
import engine_discovery  # noqa: E402
import gddoc  # noqa: E402
import gdls  # noqa: E402
import native_engine  # noqa: E402
import project_context  # noqa: E402


@contextlib.contextmanager
def _project() -> Iterator[Path]:
    base = ROOT / ".checklogs" / "tests"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"native-engine-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        (path / ".agent-kit.json").write_text(
            json.dumps(project_context.marker_document()), encoding="utf-8"
        )
        (path / "kit.config.json").write_text(
            json.dumps({
                "schema": 1,
                "game_root": "src",
                "runtime_root": ".kit/runtime",
            }),
            encoding="utf-8",
        )
        (path / "src").mkdir()
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _process(*, pid: int = 43210, code: int | None = 0,
             output: bytes = b"") -> mock.Mock:
    process = mock.Mock()
    process.pid = pid
    process.returncode = code
    process.communicate.return_value = (output, None)
    process.poll.return_value = code
    return process


def _windows_job(*assignments: tuple[bool, int]) -> mock.Mock:
    job = mock.Mock()
    values = assignments or ((True, 0),)
    job.assign.side_effect = values
    job.resume.return_value = (True, 0)
    job.active_processes.return_value = 0
    return job


class SharedNativeBoundary(unittest.TestCase):
    def setUp(self) -> None:
        # The public self-test deliberately exports the no-engine boundary.
        # These tests replace process creation with mocks, so explicitly enable
        # the unit under test without permitting a native executable to start.
        environment = mock.patch.dict(os.environ, {"KIT_ENGINE_DISABLED": "0"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_package_import_works_from_public_gate_context(self) -> None:
        script = (
            "import sys; "
            f"sys.path.insert(0, {str(ROOT)!r}); "
            "from tools import native_engine; "
            "print(native_engine.LOCK_NAME)"
        )
        completed = subprocess.run(
            [sys.executable, "-I", "-c", script],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout)
        self.assertEqual(native_engine.LOCK_NAME, completed.stdout.strip())

    def test_only_one_live_process_can_hold_the_repository_lock(self) -> None:
        with _project() as root:
            first, problem = native_engine.acquire_engine_lock(root)
            self.assertIsNotNone(first, problem)
            second, problem = native_engine.acquire_engine_lock(root)
            self.assertIsNone(second)
            self.assertIn("another Godot launch", problem)
            native_engine.release_engine_lock(root, "not-owner")
            self.assertTrue(native_engine.engine_lock_path(root).is_file())
            native_engine.release_engine_lock(root, str(first))
            self.assertFalse(native_engine.engine_lock_path(root).exists())

    def test_concurrent_run_is_structurally_refused_without_starting(self) -> None:
        with _project() as root:
            first, problem = native_engine.acquire_engine_lock(root)
            self.assertIsNotNone(first, problem)
            try:
                with mock.patch.object(native_engine, "_start_process") as start:
                    result = native_engine.run_godot(
                        "Godot.exe", ["--headless"], root=root, cwd=root, timeout=3
                    )
                self.assertEqual("concurrent-engine-launch", result.failure_class)
                self.assertEqual(native_engine.REFUSED_EXIT, result.exit_code)
                start.assert_not_called()
            finally:
                native_engine.release_engine_lock(root, str(first))

    def test_windows_start_suppresses_console_and_native_error_dialogs(self) -> None:
        process = _process()
        kernel = mock.Mock()
        kernel.GetErrorMode.return_value = 0x0100
        kernel.SetErrorMode.side_effect = (0x0100, 0x0103)
        with mock.patch.object(native_engine.os, "name", "nt"), \
                mock.patch.object(
                    native_engine.subprocess, "CREATE_NO_WINDOW", 0x08000000,
                    create=True,
                ), mock.patch("ctypes.WinDLL", return_value=kernel, create=True), \
                mock.patch.object(
                    native_engine.subprocess, "Popen", return_value=process
                ) as popen:
            returned = native_engine._start_process(
                ["Godot.exe", "--headless"], cwd=ROOT, capture_output=True
            )
        self.assertIs(process, returned)
        self.assertEqual(0x08000000, popen.call_args.kwargs["creationflags"])
        self.assertFalse(popen.call_args.kwargs["start_new_session"])
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(
            [mock.call(0x0103), mock.call(0x0100)], kernel.SetErrorMode.call_args_list
        )

    def test_windows_job_sets_kill_on_close_and_keeps_handle_until_close(self) -> None:
        kernel = mock.Mock()
        kernel.CreateJobObjectW.return_value = 2468
        kernel.SetInformationJobObject.return_value = 1
        with mock.patch("ctypes.WinDLL", return_value=kernel, create=True):
            job = native_engine._create_windows_job()

        self.assertEqual(2468, job.handle)
        kernel.CloseHandle.assert_not_called()
        args = kernel.SetInformationJobObject.call_args.args
        self.assertEqual(2468, args[0])
        self.assertEqual(native_engine.JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, args[1])
        limits = args[2]._obj
        self.assertEqual(
            native_engine.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            limits.BasicLimitInformation.LimitFlags,
        )
        self.assertEqual(native_engine.ctypes.sizeof(limits), args[3])

        job.close()
        job.close()
        self.assertTrue(job.closed)
        kernel.CloseHandle.assert_called_once_with(2468)

    def test_suspended_primary_thread_is_resumed_without_closing_job(self) -> None:
        process = _process(pid=1002, code=None)
        kernel = mock.Mock()
        kernel.CreateToolhelp32Snapshot.return_value = 9753
        kernel.OpenThread.return_value = 8642
        kernel.ResumeThread.return_value = 1

        def populate_thread(_snapshot: int, pointer: object) -> int:
            pointer._obj.th32OwnerProcessID = process.pid
            pointer._obj.th32ThreadID = 2468
            return 1

        kernel.Thread32First.side_effect = populate_thread
        job = native_engine.WindowsJob(1357, kernel)
        resumed, error = job.resume(process)

        self.assertTrue(resumed)
        self.assertEqual(0, error)
        kernel.OpenThread.assert_called_once_with(
            native_engine.THREAD_SUSPEND_RESUME, False, 2468
        )
        kernel.ResumeThread.assert_called_once_with(8642)
        self.assertEqual(
            [mock.call(8642), mock.call(9753)], kernel.CloseHandle.call_args_list
        )
        self.assertFalse(job.closed)

    def test_assignment_conflict_terminates_then_retries_with_breakaway(self) -> None:
        first = _process(pid=1001)
        second = _process(pid=1002)
        job = _windows_job(
            (False, native_engine.ERROR_ACCESS_DENIED),
            (True, 0),
        )
        with mock.patch.object(
            native_engine, "_create_windows_job", return_value=job
        ), mock.patch.object(
            native_engine, "_start_process", side_effect=(first, second)
        ) as start, mock.patch.object(
            native_engine, "_terminate_unassigned_windows_process"
        ) as terminate:
            process, returned_job = native_engine._start_bounded_windows_process(
                ["Godot.exe", "--headless"], cwd=ROOT, capture_output=True
            )

        self.assertIs(second, process)
        self.assertIs(job, returned_job)
        terminate.assert_called_once_with(first)
        self.assertEqual(
            native_engine.CREATE_SUSPENDED,
            start.call_args_list[0].kwargs["extra_creation_flags"],
        )
        self.assertEqual(
            native_engine.CREATE_SUSPENDED | native_engine.CREATE_BREAKAWAY_FROM_JOB,
            start.call_args_list[1].kwargs["extra_creation_flags"],
        )
        job.resume.assert_called_once_with(second)
        job.close.assert_not_called()

    def test_failed_job_assignment_kills_each_attempt_and_refuses_run(self) -> None:
        first = _process(pid=1001)
        second = _process(pid=1002)
        job = _windows_job(
            (False, native_engine.ERROR_ACCESS_DENIED),
            (False, native_engine.ERROR_ACCESS_DENIED),
        )
        with mock.patch.object(
            native_engine, "_create_windows_job", return_value=job
        ), mock.patch.object(
            native_engine, "_start_process", side_effect=(first, second)
        ), mock.patch.object(
            native_engine, "_terminate_unassigned_windows_process"
        ) as terminate, self.assertRaises(native_engine.NativeContainmentError):
            native_engine._start_bounded_windows_process(
                ["Godot.exe", "--headless"], cwd=ROOT, capture_output=True
            )

        self.assertEqual([mock.call(first), mock.call(second)], terminate.call_args_list)
        job.resume.assert_not_called()
        job.close.assert_called_once_with()

    def test_resume_failure_terminates_the_assigned_job_and_refuses_run(self) -> None:
        process = _process(pid=1001, code=None)
        job = _windows_job((True, 0))
        job.resume.return_value = (False, native_engine.ERROR_ACCESS_DENIED)
        with mock.patch.object(
            native_engine, "_create_windows_job", return_value=job
        ), mock.patch.object(
            native_engine, "_start_process", return_value=process
        ), mock.patch.object(
            native_engine, "terminate_owned_process_tree"
        ) as terminate, self.assertRaises(native_engine.NativeContainmentError):
            native_engine._start_bounded_windows_process(
                ["Godot.exe", "--headless"], cwd=ROOT, capture_output=True
            )

        terminate.assert_called_once_with(process, windows_job=job)
        job.close.assert_called_once_with()

    def test_public_bounded_start_surfaces_containment_failure_without_fallback(self) -> None:
        with _project() as root, mock.patch.object(
            native_engine.os, "name", "nt"
        ), mock.patch.object(
            native_engine,
            "_start_bounded_windows_process",
            side_effect=native_engine.NativeContainmentError(
                native_engine.ERROR_ACCESS_DENIED,
                "assignment refused",
            ),
        ), mock.patch.object(native_engine, "_start_process") as unsafe_fallback:
            lock_path = native_engine.engine_lock_path(root)
            started = native_engine.start_godot(
                "Godot.exe",
                ["--headless"],
                root=root,
                cwd=root,
                capture_output=True,
            )

        self.assertIsNone(started.process)
        self.assertEqual("engine-start-failed", started.failure.failure_class)
        self.assertIn("NativeContainmentError", started.failure.output)
        unsafe_fallback.assert_not_called()
        self.assertFalse(lock_path.exists())

    def test_owned_termination_uses_job_even_after_root_process_exits(self) -> None:
        process = _process(code=0)
        job = _windows_job()
        with mock.patch.object(native_engine, "_terminate_pid_tree") as taskkill:
            native_engine.terminate_owned_process_tree(
                process, windows_job=job
            )
        job.terminate.assert_called_once_with(native_engine.TIMEOUT_EXIT)
        job.close.assert_called_once_with()
        taskkill.assert_not_called()

    def test_normal_bounded_run_holds_job_until_output_is_collected(self) -> None:
        with _project() as root:
            process = _process(output=b"done")
            process.stdout = io.BytesIO(b"done")
            job = _windows_job()
            identity = native_engine.ProcessIdentity(
                process.pid, "test", "normal-run", "b" * 64, "Godot.exe"
            )

            def wait(*, timeout: int) -> int:
                self.assertIn(timeout, (3, 10))
                if timeout == 3:
                    job.close.assert_not_called()
                return 0

            process.wait.side_effect = wait
            with mock.patch.object(
                native_engine.os, "name", "nt"
            ), mock.patch.object(
                native_engine, "_create_windows_job", return_value=job
            ), mock.patch.object(
                native_engine, "_start_process", return_value=process
            ), mock.patch.object(
                native_engine, "capture_process_identity", return_value=identity
            ):
                result = native_engine.run_godot(
                    "Godot.exe", ["--headless"], root=root, cwd=root, timeout=3
                )
        self.assertEqual("done", result.output)
        job.close.assert_called_once_with()

    def test_retained_background_start_is_an_explicit_non_job_exception(self) -> None:
        with _project() as root:
            process = _process(pid=7654, code=None)
            identity = native_engine.ProcessIdentity(
                process.pid, "test", "retained", "c" * 64, "Godot.exe"
            )
            with mock.patch.object(native_engine.os, "name", "nt"), \
                    mock.patch.object(
                        native_engine, "_create_windows_job"
                    ) as create_job, mock.patch.object(
                        native_engine, "_start_process", return_value=process
                    ), mock.patch.object(
                        native_engine,
                        "capture_process_identity",
                        return_value=identity,
                    ):
                started = native_engine.start_godot(
                    "Godot.exe",
                    ["--headless", "--editor"],
                    root=root,
                    cwd=root,
                    capture_output=False,
                )
            self.assertIs(process, started.process)
            self.assertIsNone(started.windows_job)
            create_job.assert_not_called()
            native_engine.release_engine_lock(root, str(started.lock_token))


class BootstrapImportBoundary(unittest.TestCase):
    def setUp(self) -> None:
        environment = mock.patch.dict(os.environ, {"KIT_ENGINE_DISABLED": "0"})
        environment.start()
        self.addCleanup(environment.stop)

    def test_import_uses_shared_runner_and_persists_native_failure(self) -> None:
        with _project() as root:
            engine = root / "Godot.exe"
            authenticated = types.SimpleNamespace(
                path=engine,
                version="4.7.2",
                assert_unchanged=mock.Mock(),
            )
            failure = native_engine.NativeResult(
                exit_code=-1073741819,
                output="NATIVE ENGINE CRASH",
                executable="Godot.exe",
                started=True,
                failure_class="native-crash",
                failure_code="native-crash",
                windows_status="0xC0000005",
            )
            with mock.patch.object(bootstrap, "ROOT", root), \
                    mock.patch.object(bootstrap, "SRC", root / "src"), \
                    mock.patch.object(bootstrap, "QUIET", True), \
                    mock.patch.object(
                        bootstrap.engine_discovery,
                        "authenticate_godot",
                        return_value=authenticated,
                    ) as authenticate, \
                    mock.patch.object(
                        bootstrap.native_engine, "run_godot", return_value=failure
                    ) as launch, mock.patch.object(
                        bootstrap.native_engine, "persist_native_failure"
                    ) as persist:
                result = bootstrap.do_import(str(engine))
            self.assertIs(result, failure)
            authenticate.assert_called_once_with(
                root,
                candidate=str(engine),
                operation="setup-import",
            )
            authenticated.assert_unchanged.assert_called_once_with()
            launch.assert_called_once_with(
                engine,
                ["--headless", "--path", str(root / "src"), "--import", "--quit"],
                root=root,
                cwd=root,
                timeout=600,
            )
            persist.assert_called_once_with(root, failure, operation="setup-import")

    def test_import_never_follows_a_failed_authentication_with_project_work(self) -> None:
        with _project() as root:
            refusal = engine_discovery.EngineAuthenticationError(
                "selected executable did not report Godot 4.7.2",
                status="engine_version_unknown",
            )
            with mock.patch.object(bootstrap, "ROOT", root), \
                    mock.patch.object(bootstrap, "SRC", root / "src"), \
                    mock.patch.object(bootstrap, "QUIET", True), \
                    mock.patch.object(
                        bootstrap.engine_discovery,
                        "authenticate_godot",
                        side_effect=refusal,
                    ) as authenticate, mock.patch.object(
                        bootstrap.native_engine, "run_godot"
                    ) as launch:
                result = bootstrap.do_import(sys.executable)

            self.assertEqual("engine-authentication-failed", result.failure_class)
            self.assertEqual("engine_version_unknown", result.failure_code)
            authenticate.assert_called_once()
            launch.assert_not_called()

    def test_native_failure_cannot_be_hidden_by_a_partially_written_cache(self) -> None:
        with _project() as root:
            cache = root / "src" / ".godot" / "global_script_class_cache.cfg"
            cache.parent.mkdir(parents=True)
            cache.write_text("partial\n", encoding="utf-8")
            failure = native_engine.NativeResult(
                exit_code=native_engine.TIMEOUT_EXIT,
                output="TIMEOUT",
                executable="Godot.exe",
                started=True,
                failure_class="engine-timeout",
                failure_code="native-engine-timeout",
                timeout_seconds=600,
            )
            records: list[dict[str, object]] = []
            with mock.patch.object(bootstrap, "SRC", root / "src"), \
                    mock.patch.object(bootstrap, "results", records):
                bootstrap.check_import("Godot.exe", attempted=failure)
            self.assertEqual(bootstrap.MISSING, records[0]["state"])
            self.assertIn("engine-timeout", str(records[0]["detail"]))

    def test_timeout_terminates_only_the_owned_tree_and_releases_lock(self) -> None:
        with _project() as root:
            process = _process(code=0)
            process.stdout = io.BytesIO(b"partial")
            job = _windows_job()
            process.wait.side_effect = subprocess.TimeoutExpired(
                ["Godot.exe"], 3, output=b"partial"
            )
            identity = native_engine.ProcessIdentity(
                process.pid, "test", "timeout", "d" * 64, "Godot.exe"
            )

            def terminate_owned(
                _process: mock.Mock,
                *,
                windows_job: mock.Mock,
            ) -> bool:
                windows_job.close()
                return True

            with mock.patch.object(
                native_engine.os, "name", "nt"
            ), mock.patch.object(
                native_engine, "_start_process", return_value=process
            ), mock.patch.object(
                native_engine, "_create_windows_job", return_value=job
            ), mock.patch.object(
                native_engine, "capture_process_identity", return_value=identity
            ), mock.patch.object(
                native_engine,
                "terminate_owned_process_tree",
                side_effect=terminate_owned,
            ) as terminate:
                result = native_engine.run_godot(
                    "Godot.exe", ["--headless"], root=root, cwd=root, timeout=3
                )
            self.assertEqual("engine-timeout", result.failure_class)
            self.assertEqual("native-engine-timeout", result.failure_code)
            self.assertIn("owned process tree terminated", result.output)
            terminate.assert_called_once_with(process, windows_job=job)
            job.close.assert_called_once_with()
            self.assertFalse(native_engine.engine_lock_path(root).exists())

    def test_access_violation_and_posix_signal_are_native_crashes(self) -> None:
        crashed, status = native_engine.classify_crash_exit(
            -1073741819, system_name="Windows"
        )
        self.assertTrue(crashed)
        self.assertEqual("0xC0000005", status)
        self.assertEqual(
            (True, None), native_engine.classify_crash_exit(-11, system_name="POSIX")
        )
        self.assertEqual(
            (False, None), native_engine.classify_crash_exit(1, system_name="Windows")
        )

    def test_start_oserror_is_structured_and_never_raises(self) -> None:
        job = _windows_job()
        with _project() as root, mock.patch.object(
            native_engine.os, "name", "nt"
        ), mock.patch.object(
            native_engine, "_create_windows_job", return_value=job
        ), mock.patch.object(
            native_engine, "_start_process", side_effect=PermissionError("denied")
        ):
            result = native_engine.run_godot(
                "Godot.exe", ["--headless"], root=root, cwd=root, timeout=3
            )
        self.assertEqual("engine-start-failed", result.failure_class)
        self.assertEqual(native_engine.START_FAILED_EXIT, result.exit_code)
        self.assertIn("PermissionError", result.output)
        self.assertNotIn("denied", result.output)
        job.close.assert_called_once_with()

    def test_native_warning_survives_everything_except_full_or_strict_recovery(self) -> None:
        with _project() as root:
            result = native_engine.NativeResult(
                exit_code=-1073741819,
                output="private process output must not be persisted",
                executable="Godot.exe",
                started=True,
                failure_class="native-crash",
                failure_code="native-crash",
                windows_status="0xC0000005",
            )
            warning = native_engine.persist_native_failure(
                root, result, operation="setup-import"
            )
            self.assertEqual("native-crash", warning["failure_class"])
            persisted = native_engine.native_warning_path(root).read_text(encoding="utf-8")
            self.assertNotIn("private process output", persisted)
            with self.assertRaisesRegex(ValueError, "full or strict"):
                native_engine.resolve_native_warning(
                    root,
                    verification_scope="static",
                    expected_identity=str(
                        native_engine.native_warning_identity(root)
                    ),
                )
            self.assertIsNotNone(native_engine.read_native_warning(root))
            identity = native_engine.native_warning_identity(root)
            self.assertIsNotNone(identity)
            self.assertTrue(
                native_engine.resolve_native_warning(
                    root,
                    verification_scope="full",
                    expected_identity=str(identity),
                )
            )
            self.assertIsNone(native_engine.read_native_warning(root))

    def test_background_stop_requires_the_exact_pid_and_lock_token(self) -> None:
        with _project() as root:
            token, problem = native_engine.acquire_engine_lock(root)
            self.assertIsNotNone(token, problem)
            identity = native_engine.ProcessIdentity(
                9321, "test", "background", "e" * 64, "Godot.exe"
            )
            self.assertTrue(
                native_engine._transfer_engine_lock(
                    root, str(token), 9321, identity
                )
            )
            wrong = native_engine.BackgroundReceipt(9321, "wrong-token", identity)
            exact = native_engine.BackgroundReceipt(9321, str(token), identity)
            with mock.patch.object(
                native_engine,
                "inspect_background_owner",
                side_effect=(
                    native_engine.BackgroundOwnerInspection(
                        native_engine.BACKGROUND_OWNERSHIP_MISMATCH,
                        "wrong token",
                    ),
                    native_engine.BackgroundOwnerInspection(
                        native_engine.BACKGROUND_OWNED_LIVE,
                        "exact owner",
                    ),
                ),
            ), mock.patch.object(
                native_engine, "_pid_is_alive", return_value=False
            ), mock.patch.object(
                native_engine,
                "capture_process_identity",
                side_effect=(identity, None),
            ), mock.patch.object(
                native_engine, "release_engine_lock"
            ) as release, mock.patch.object(
                native_engine, "_lock_holder", return_value=None
            ), mock.patch.object(
                native_engine, "terminate_exact_background_owner", return_value=True
            ) as terminate:
                self.assertFalse(
                    native_engine.stop_owned_background(root, wrong)
                )
                terminate.assert_not_called()
                self.assertTrue(
                    native_engine.stop_owned_background(root, exact)
                )
                terminate.assert_called_once_with(exact)
                release.assert_called_once_with(root, str(token))


class FreshDocumentationProof(unittest.TestCase):
    def _result(self, **changes: object) -> native_engine.NativeResult:
        values: dict[str, object] = {
            "exit_code": 0,
            "output": "",
            "executable": "Godot.exe",
            "started": True,
        }
        values.update(changes)
        return native_engine.NativeResult(**values)

    @staticmethod
    def _authenticated(root: Path) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            path=root / "Godot.exe",
            version="4.7.2",
            assert_unchanged=mock.Mock(),
        )

    def test_preexisting_xml_cannot_make_an_empty_new_dump_pass(self) -> None:
        with _project() as root:
            authenticated = self._authenticated(root)
            docs = root / ".godot_doc"
            old = docs / "classes" / "Old.xml"
            old.parent.mkdir(parents=True)
            old.write_text('<class name="Old"/>\n', encoding="utf-8")
            with mock.patch.object(gddoc, "ROOT", root), \
                    mock.patch.object(gddoc, "DOC_ROOT", docs), \
                    mock.patch.object(gddoc, "DOC_DIR", docs / "classes"), \
                    mock.patch.object(
                        gddoc.engine_discovery,
                        "authenticate_godot",
                        return_value=authenticated,
                    ), \
                    mock.patch.object(
                        gddoc.native_engine, "run_godot", return_value=self._result()
                    ), mock.patch.object(gddoc.native_engine, "persist_native_failure"):
                code = gddoc.build("C:/Godot/Godot.exe")
            self.assertEqual(1, code)
            self.assertTrue(old.is_file())

    def test_only_fresh_valid_staged_xml_is_promoted(self) -> None:
        with _project() as root:
            authenticated = self._authenticated(root)
            docs = root / ".godot_doc"
            old = docs / "classes" / "Old.xml"
            old.parent.mkdir(parents=True)
            old.write_text('<class name="Old"/>\n', encoding="utf-8")

            def fresh_dump(
                _engine: str,
                arguments: list[str],
                **_kwargs: object,
            ) -> native_engine.NativeResult:
                staging = Path(arguments[-1])
                generated = staging / "doc" / "classes" / "Node.xml"
                generated.parent.mkdir(parents=True)
                generated.write_text('<class name="Node"/>\n', encoding="utf-8")
                return self._result(exit_code=1)  # Godot's noisy 1 is not proof of failure.

            with mock.patch.object(gddoc, "ROOT", root), \
                    mock.patch.object(gddoc, "DOC_ROOT", docs), \
                    mock.patch.object(gddoc, "DOC_DIR", docs / "classes"), \
                    mock.patch.object(
                        gddoc.engine_discovery,
                        "authenticate_godot",
                        return_value=authenticated,
                    ), \
                    mock.patch.object(
                        gddoc.native_engine, "run_godot", side_effect=fresh_dump
                    ), mock.patch.object(gddoc.native_engine, "persist_native_failure"):
                code = gddoc.build("C:/Godot/Godot.exe")
            self.assertEqual(0, code)
            self.assertFalse(old.exists())
            self.assertTrue((docs / "doc" / "classes" / "Node.xml").is_file())

    def test_native_docs_failure_is_persisted_and_never_promotes_output(self) -> None:
        with _project() as root:
            authenticated = self._authenticated(root)
            docs = root / ".godot_doc"
            old = docs / "classes" / "Old.xml"
            old.parent.mkdir(parents=True)
            old.write_text('<class name="Old"/>\n', encoding="utf-8")
            failure = self._result(
                exit_code=-1073741819,
                output="NATIVE ENGINE CRASH",
                failure_class="native-crash",
                failure_code="native-crash",
                windows_status="0xC0000005",
            )

            def failed_dump(
                _engine: str,
                arguments: list[str],
                **_kwargs: object,
            ) -> native_engine.NativeResult:
                staging = Path(arguments[-1])
                generated = staging / "classes" / "Partial.xml"
                generated.parent.mkdir(parents=True)
                generated.write_text('<class name="Partial"/>\n', encoding="utf-8")
                return failure

            with mock.patch.object(gddoc, "ROOT", root), \
                    mock.patch.object(gddoc, "DOC_ROOT", docs), \
                    mock.patch.object(gddoc, "DOC_DIR", docs / "classes"), \
                    mock.patch.object(
                        gddoc.engine_discovery,
                        "authenticate_godot",
                        return_value=authenticated,
                    ), \
                    mock.patch.object(
                        gddoc.native_engine, "run_godot", side_effect=failed_dump
                    ), mock.patch.object(
                        gddoc.native_engine, "persist_native_failure"
                    ) as persist:
                code = gddoc.build("C:/Godot/Godot.exe")
            self.assertEqual(1, code)
            persist.assert_called_once_with(
                root, failure, operation="godot-docs-build"
            )
            self.assertTrue(old.is_file())
            self.assertFalse((docs / "classes" / "Partial.xml").exists())

    def test_docs_never_follow_failed_authentication_with_doctool(self) -> None:
        with _project() as root:
            docs = root / ".godot_doc"
            refusal = engine_discovery.EngineAuthenticationError(
                "selected executable is not Godot 4.7.2",
                status="engine_version_mismatch",
            )
            with mock.patch.object(gddoc, "ROOT", root), \
                    mock.patch.object(gddoc, "DOC_ROOT", docs), \
                    mock.patch.object(gddoc, "DOC_DIR", docs / "classes"), \
                    mock.patch.object(
                        gddoc.engine_discovery,
                        "authenticate_godot",
                        side_effect=refusal,
                    ), mock.patch.object(
                        gddoc.native_engine, "run_godot"
                    ) as launch:
                code = gddoc.build(sys.executable)

            self.assertEqual(2, code)
            launch.assert_not_called()
            self.assertFalse(docs.exists())


class LanguageServerBoundary(unittest.TestCase):
    @staticmethod
    def _identity(pid: int = 8123) -> native_engine.ProcessIdentity:
        return native_engine.ProcessIdentity(
            pid=pid,
            platform="test",
            creation_marker="created-once",
            executable_sha256="a" * 64,
            executable_name="Godot.exe",
        )

    @staticmethod
    def _authenticated(root: Path) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            path=root / "Godot.exe",
            version="4.7.2",
            assert_unchanged=mock.Mock(),
        )

    def _write_receipt(
        self,
        state: Path,
        *,
        pid: int = 8123,
        token: str = "owned-token",
    ) -> native_engine.BackgroundReceipt:
        identity = self._identity(pid)
        native_engine.write_background_receipt(
            state,
            pid=pid,
            token=token,
            process_identity=identity,
        )
        return native_engine.BackgroundReceipt(pid, token, identity)

    def test_start_uses_shared_boundary_and_the_configured_game_root(self) -> None:
        with _project() as root:
            process = _process(pid=8123, code=None)
            identity = self._identity()
            started = native_engine.NativeStart(
                process=process,
                lock_token="owned-token",
                failure=None,
                executable="Godot.exe",
                process_identity=identity,
            )
            authenticated = self._authenticated(root)
            state = root / ".kit" / "runtime" / "gdls.json"
            with mock.patch.object(gdls, "ROOT", root), \
                    mock.patch.object(gdls, "GAME_ROOT", root / "src"), \
                    mock.patch.object(gdls, "PID_FILE", state), \
                    mock.patch.object(
                        gdls, "find_godot", return_value=authenticated
                    ), \
                    mock.patch.object(gdls, "port_open", side_effect=(False, True)), \
                    mock.patch.object(gdls, "listener_owned_by", return_value=True), \
                    mock.patch.object(gdls, "_probe_lsp_ready", return_value=True), \
                    mock.patch.object(
                        gdls.native_engine, "start_godot", return_value=started
                    ) as launch, mock.patch.object(
                        gdls.native_engine,
                        "inspect_background_owner",
                        return_value=native_engine.BackgroundOwnerInspection(
                            native_engine.BACKGROUND_OWNED_LIVE,
                            "exact owner",
                        ),
                    ):
                code = gdls.cmd_start(mock.Mock(engine=None))
            self.assertEqual(0, code)
            authenticated.assert_unchanged.assert_called_once_with()
            launch.assert_called_once_with(
                root / "Godot.exe",
                ["--path", str(root / "src"), "--editor", "--headless", "--lsp-port=6105"],
                root=root,
                cwd=root,
                capture_output=False,
                operation="gdls-start",
            )
            saved = json.loads(state.read_text(encoding="utf-8"))
            self.assertEqual(3, saved["schema"])
            self.assertEqual("ready", saved["lifecycle"])
            self.assertEqual(8123, saved["pid"])
            self.assertEqual("owned-token", saved["lock_token"])
            self.assertEqual(identity.as_json(), saved["process_identity"])

    def test_open_port_without_an_exact_owner_is_never_accepted(self) -> None:
        with _project() as root:
            with mock.patch.object(gdls, "ROOT", root), \
                    mock.patch.object(
                        gdls, "PID_FILE", root / ".kit/runtime/gdls.json"
                    ), mock.patch.object(gdls, "port_open", return_value=True), \
                    mock.patch.object(gdls, "find_godot") as authenticate, \
                    mock.patch.object(gdls.native_engine, "start_godot") as launch:
                code = gdls.cmd_start(mock.Mock(engine=None))

            self.assertEqual(1, code)
            authenticate.assert_not_called()
            launch.assert_not_called()

    def test_startup_journal_is_read_only_and_never_reported_as_ready(self) -> None:
        with _project() as root:
            state = root / ".kit" / "runtime" / "gdls.json"
            receipt = native_engine.BackgroundReceipt(
                8123, "token", self._identity()
            )
            native_engine.write_background_journal(
                state,
                receipt=receipt,
                operation_id="gdls-start-test",
            )
            before = state.read_bytes()
            with mock.patch.object(gdls, "ROOT", root), \
                    mock.patch.object(gdls, "PID_FILE", state), \
                    mock.patch.object(gdls, "port_open", return_value=True), \
                    mock.patch.object(
                        gdls.native_engine,
                        "inspect_background_owner",
                        return_value=native_engine.BackgroundOwnerInspection(
                            native_engine.BACKGROUND_OWNED_LIVE,
                            "exact owner",
                        ),
                    ):
                status = gdls.inspect_status()
            self.assertEqual("launch_incomplete", status["status"])
            self.assertTrue(status["owned"])
            self.assertEqual(before, state.read_bytes())

    def test_ready_probe_refuses_a_listener_owned_by_another_pid(self) -> None:
        with mock.patch.object(
            gdls, "listener_owned_by", return_value=False
        ), mock.patch.object(gdls, "Client") as client:
            self.assertFalse(gdls._probe_lsp_ready(8123))
        client.assert_not_called()

    def test_ready_probe_requires_a_real_lsp_initialize(self) -> None:
        client = mock.Mock()
        client.initialise.return_value=False
        with mock.patch.object(
            gdls, "listener_owned_by", return_value=None
        ), mock.patch.object(gdls, "Client", return_value=client):
            self.assertFalse(gdls._probe_lsp_ready(8123))
        client.initialise.assert_called_once_with(timeout=2.0)
        client.disconnect.assert_called_once_with()

    def test_keyboard_interrupt_during_handoff_cleans_the_exact_launch(self) -> None:
        with _project() as root:
            process = _process(pid=8123, code=None)
            identity = self._identity()
            started = native_engine.NativeStart(
                process,
                "owned-token",
                None,
                "Godot.exe",
                process_identity=identity,
            )
            authenticated = self._authenticated(root)
            state = root / ".kit" / "runtime" / "gdls.json"
            with mock.patch.object(gdls, "ROOT", root), \
                    mock.patch.object(gdls, "GAME_ROOT", root / "src"), \
                    mock.patch.object(gdls, "PID_FILE", state), \
                    mock.patch.object(gdls, "find_godot", return_value=authenticated), \
                    mock.patch.object(gdls, "port_open", return_value=False), \
                    mock.patch.object(
                        gdls.native_engine, "start_godot", return_value=started
                    ), mock.patch.object(
                        gdls.native_engine,
                        "write_background_journal",
                        side_effect=KeyboardInterrupt(),
                    ), mock.patch.object(
                        gdls, "_cleanup_started_process", return_value=True
                    ) as cleanup, self.assertRaises(KeyboardInterrupt):
                gdls.cmd_start(mock.Mock(engine=None))
            cleanup.assert_called_once_with(
                process,
                "owned-token",
                native_engine.BackgroundReceipt(
                    8123, "owned-token", identity
                ),
            )

    def test_port_opening_after_launch_still_requires_exact_process_identity(self) -> None:
        with _project() as root:
            process = _process(pid=8123, code=None)
            identity = self._identity()
            started = native_engine.NativeStart(
                process,
                "owned-token",
                None,
                "Godot.exe",
                process_identity=identity,
            )
            authenticated = self._authenticated(root)
            state = root / ".kit" / "runtime" / "gdls.json"
            with mock.patch.object(gdls, "ROOT", root), \
                    mock.patch.object(gdls, "GAME_ROOT", root / "src"), \
                    mock.patch.object(gdls, "PID_FILE", state), \
                    mock.patch.object(gdls, "find_godot", return_value=authenticated), \
                    mock.patch.object(gdls, "port_open", side_effect=(False, True)), \
                    mock.patch.object(gdls, "listener_owned_by", return_value=True), \
                    mock.patch.object(
                        gdls.native_engine, "start_godot", return_value=started
                    ), mock.patch.object(
                        gdls.native_engine,
                        "inspect_background_owner",
                        return_value=native_engine.BackgroundOwnerInspection(
                            native_engine.BACKGROUND_OWNERSHIP_MISMATCH,
                            "the PID now belongs to a different process",
                        ),
                    ), mock.patch.object(
                        gdls.native_engine,
                        "stop_owned_background",
                        return_value=True,
                    ) as terminate, mock.patch.object(
                        gdls.native_engine, "persist_native_failure"
                    ) as persist:
                code = gdls.cmd_start(mock.Mock(engine=None))

            self.assertEqual(1, code)
            terminate.assert_called_once_with(root, mock.ANY)
            persist.assert_called_once()
            self.assertFalse(state.exists())

    def test_failed_engine_authentication_never_starts_the_server(self) -> None:
        with _project() as root:
            refusal = engine_discovery.EngineAuthenticationError(
                "unversioned candidate did not report Godot 4.7.2",
                status="engine_version_unknown",
            )
            with mock.patch.object(gdls, "ROOT", root), \
                    mock.patch.object(
                        gdls, "PID_FILE", root / ".kit/runtime/gdls.json"
                    ), mock.patch.object(gdls, "port_open", return_value=False), \
                    mock.patch.object(gdls, "find_godot", side_effect=refusal), \
                    mock.patch.object(gdls.native_engine, "start_godot") as launch:
                code = gdls.cmd_start(mock.Mock(engine=sys.executable))

            self.assertEqual(2, code)
            launch.assert_not_called()

    def test_start_failure_is_persisted_without_retrying(self) -> None:
        with _project() as root:
            authenticated = self._authenticated(root)
            failure = native_engine.NativeResult(
                exit_code=native_engine.REFUSED_EXIT,
                output="ENGINE LAUNCH REFUSED",
                executable="Godot.exe",
                started=False,
                failure_class="concurrent-engine-launch",
                failure_code="concurrent-engine-launch",
            )
            started = native_engine.NativeStart(None, None, failure, "Godot.exe")
            with mock.patch.object(gdls, "ROOT", root), \
                    mock.patch.object(gdls, "PID_FILE", root / ".kit/runtime/gdls.json"), \
                    mock.patch.object(
                        gdls, "find_godot", return_value=authenticated
                    ), \
                    mock.patch.object(gdls, "port_open", return_value=False), \
                    mock.patch.object(
                        gdls.native_engine, "start_godot", return_value=started
                    ), mock.patch.object(
                        gdls.native_engine, "persist_native_failure"
                    ) as persist:
                code = gdls.cmd_start(mock.Mock(engine=None))
            self.assertEqual(1, code)
            persist.assert_called_once_with(root, failure, operation="gdls-start")

    def test_stop_refuses_a_pid_without_matching_lock_ownership(self) -> None:
        with _project() as root:
            state = root / ".kit" / "runtime" / "gdls.json"
            receipt = self._write_receipt(state, token="token")
            with mock.patch.object(gdls, "ROOT", root), \
                    mock.patch.object(gdls, "PID_FILE", state), \
                    mock.patch.object(
                        gdls.native_engine,
                        "reconcile_abandoned_background",
                        return_value=None,
                    ), mock.patch.object(
                        gdls.native_engine,
                        "inspect_background_owner",
                        return_value=native_engine.BackgroundOwnerInspection(
                            native_engine.BACKGROUND_OWNERSHIP_MISMATCH,
                            "PID reused",
                        ),
                    ), \
                    mock.patch.object(
                        gdls.native_engine, "stop_owned_background", return_value=False
                    ) as stop:
                code = gdls.cmd_stop(mock.Mock())
            self.assertEqual(1, code)
            self.assertTrue(state.is_file())
            stop.assert_not_called()

    def test_read_only_status_surfaces_vanished_owner_without_mutating_state(self) -> None:
        with _project() as root:
            state = root / ".kit" / "runtime" / "gdls.json"
            receipt = self._write_receipt(state)
            with mock.patch.object(gdls, "ROOT", root), \
                    mock.patch.object(gdls, "PID_FILE", state), \
                    mock.patch.object(gdls, "port_open", return_value=False), \
                    mock.patch.object(
                        gdls.native_engine,
                        "inspect_background_owner",
                        return_value=native_engine.BackgroundOwnerInspection(
                            native_engine.BACKGROUND_OWNER_VANISHED,
                            "gone",
                        ),
                    ) as inspect, mock.patch.object(
                        gdls.native_engine, "persist_native_failure"
                    ) as persist, mock.patch.object(
                        gdls.native_engine, "reconcile_abandoned_background"
                    ) as reconcile:
                status = gdls.inspect_status()

            self.assertEqual("owner_vanished", status["status"])
            self.assertEqual("native-engine-disappeared", status["warning"]["code"])
            inspect.assert_called_once_with(root, receipt)
            persist.assert_not_called()
            reconcile.assert_not_called()
            self.assertTrue(state.is_file())

    def test_next_command_persists_a_background_engine_that_vanished(self) -> None:
        with _project() as root:
            state = root / ".kit" / "runtime" / "gdls.json"
            receipt = self._write_receipt(state, token="token")
            failure = native_engine.NativeResult(
                exit_code=1,
                output="retained engine vanished",
                executable="Godot",
                started=True,
                failure_class="native-crash",
                failure_code="native-engine-disappeared",
            )
            with mock.patch.object(gdls, "ROOT", root), \
                    mock.patch.object(gdls, "PID_FILE", state), \
                    mock.patch.object(
                        gdls.native_engine,
                        "reconcile_abandoned_background",
                        return_value=failure,
                    ) as reconcile, mock.patch.object(
                        gdls.native_engine, "persist_native_failure"
                    ) as persist:
                observed = gdls._record_abandoned_owner()
            self.assertTrue(observed)
            reconcile.assert_called_once_with(
                root,
                receipt,
                executable="Godot.exe",
                operation="gdls-unexpected-exit",
            )
            persist.assert_not_called()
            self.assertFalse(state.exists())


if __name__ == "__main__":
    unittest.main()
