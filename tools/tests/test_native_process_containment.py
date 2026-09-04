#!/usr/bin/env python3
"""Adversarial proofs for native process ownership and containment."""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path, PureWindowsPath
from typing import Iterator
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(ROOT))

import native_engine  # noqa: E402
import process_supervisor  # noqa: E402
import project_context  # noqa: E402


def _scratch_parent() -> Path:
    configured = os.environ.get("KIT_TEST_TMPDIR", "").strip()
    return Path(configured) if configured else ROOT / ".checklogs" / "tests"


@contextlib.contextmanager
def _project() -> Iterator[Path]:
    base = _scratch_parent()
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"native-containment-{uuid.uuid4().hex}"
    path.mkdir()
    try:
        (path / ".agent-kit.json").write_text(
            json.dumps(project_context.marker_document()), encoding="utf-8"
        )
        (path / "kit.config.json").write_text(
            json.dumps(
                {"schema": 1, "game_root": "src", "runtime_root": ".kit/runtime"}
            ),
            encoding="utf-8",
        )
        (path / "src").mkdir()
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _identity(pid: int, marker: str = "created") -> native_engine.ProcessIdentity:
    return native_engine.ProcessIdentity(
        pid=pid,
        platform="test",
        creation_marker=marker,
        executable_sha256=(marker.encode("utf-8").hex() + "0" * 64)[:64],
        executable_name="Godot.exe",
    )


def _process(
    *,
    pid: int = 43210,
    code: int | None = 0,
    output: bytes = b"",
) -> mock.Mock:
    process = mock.Mock()
    process.pid = pid
    process.returncode = code
    process.stdout = io.BytesIO(output)
    process.poll.return_value = code
    process.wait.return_value = code
    return process


def _real_tree_command(pid_file: Path) -> list[str]:
    child = (
        "import os,pathlib,subprocess,sys,time;"
        "grand=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        "pathlib.Path(sys.argv[1]).write_text(f'{os.getpid()} {grand.pid}',encoding='ascii');"
        "print('tree-ready',flush=True);"
        "time.sleep(60)"
    )
    return [sys.executable, "-c", child, str(pid_file)]


def _wait_for_pid_file(path: Path, timeout: float = 5.0) -> tuple[int, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            values = path.read_text(encoding="ascii").split()
            if len(values) == 2:
                return int(values[0]), int(values[1])
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    raise AssertionError("helper process tree did not publish its PIDs")


class OuterProcessSupervisor(unittest.TestCase):
    def _assert_dead(self, *pids: int) -> None:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if all(
                process_supervisor.pid_liveness(pid)
                == process_supervisor.PID_DEAD
                for pid in pids
            ):
                return
            time.sleep(0.02)
        states = {
            pid: process_supervisor.pid_liveness(pid) for pid in pids
        }
        self.fail(f"supervised helper process survived: {states}")

    def test_real_timeout_contains_child_and_grandchild(self) -> None:
        with _project() as root:
            pid_file = root / "timeout-tree.txt"
            result = process_supervisor.run_supervised(
                _real_tree_command(pid_file),
                cwd=root,
                # Prove containment, not sub-second interpreter startup. Under
                # a full Windows suite the child and its grandchild can need
                # more than 500 ms before they publish their identities.
                timeout=5.0,
            )
            child_pid, grandchild_pid = _wait_for_pid_file(pid_file)
        self.assertTrue(result.timed_out)
        self.assertTrue(result.termination_verified, result)
        self.assertIn("tree-ready", result.stdout or "")
        self._assert_dead(child_pid, grandchild_pid)

    def test_real_cancellation_contains_child_and_grandchild(self) -> None:
        with _project() as root:
            pid_file = root / "cancel-tree.txt"
            cancellation = threading.Event()

            def cancel_when_ready() -> None:
                _wait_for_pid_file(pid_file)
                cancellation.set()

            trigger = threading.Thread(target=cancel_when_ready, daemon=True)
            trigger.start()
            result = process_supervisor.run_supervised(
                _real_tree_command(pid_file),
                cwd=root,
                timeout=10,
                cancellation=cancellation,
            )
            trigger.join(timeout=5)
            child_pid, grandchild_pid = _wait_for_pid_file(pid_file)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.timed_out)
        self.assertTrue(result.termination_verified, result)
        self._assert_dead(child_pid, grandchild_pid)

    def test_owner_death_lifeline_contains_child_and_grandchild(self) -> None:
        with _project() as root:
            pid_file = root / "owner-death-tree.txt"
            tree_command = _real_tree_command(pid_file)
            owner_code = (
                "from pathlib import Path;"
                "from tools import process_supervisor as p;"
                f"p.run_supervised({tree_command!r},cwd=Path({str(root)!r}),timeout=60)"
            )
            owner = subprocess.Popen(
                [sys.executable, "-c", owner_code],
                cwd=str(ROOT),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
            )
            try:
                child_pid, grandchild_pid = _wait_for_pid_file(pid_file)
                owner.kill()
                owner.wait(timeout=10)
                self._assert_dead(child_pid, grandchild_pid)
            finally:
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=10)

    def test_outer_capture_is_bounded_per_stream(self) -> None:
        command = [
            sys.executable,
            "-c",
            "import sys;print('A'*200000);print('B'*200000,file=sys.stderr)",
        ]
        result = process_supervisor.run_supervised(
            command,
            cwd=ROOT,
            timeout=10,
            output_cap_bytes=8192,
        )
        self.assertEqual(0, result.returncode)
        self.assertLessEqual(len((result.stdout or "").encode()), 8192)
        self.assertLessEqual(len((result.stderr or "").encode()), 8192)
        self.assertIn("truncated", result.stdout or "")
        self.assertIn("truncated", result.stderr or "")

    def test_exclusive_file_lock_ignores_stale_sidecar_but_rejects_live_owner(self) -> None:
        with _project() as root:
            lock = root / ".kit" / "runtime" / "strict.lock"
            lock.parent.mkdir(parents=True)
            lock.write_bytes(b"stale-metadata")
            child = (
                "import pathlib,sys;"
                "from tools import process_supervisor as p;"
                "path=pathlib.Path(sys.argv[1]);"
                "ctx=p.exclusive_file_lock(path,label='strict verification');"
                "\ntry:\n ctx.__enter__()\n"
                "except p.ExclusiveLockUnavailable:\n print('busy')\n sys.exit(7)\n"
                "else:\n print('acquired')\n ctx.__exit__(None,None,None)\n"
            )
            with process_supervisor.exclusive_file_lock(
                lock, label="strict verification"
            ):
                blocked = process_supervisor.run_supervised(
                    [sys.executable, "-c", child, str(lock)],
                    cwd=ROOT,
                    timeout=10,
                )
            released = process_supervisor.run_supervised(
                [sys.executable, "-c", child, str(lock)],
                cwd=ROOT,
                timeout=10,
            )
        self.assertEqual(7, blocked.returncode)
        self.assertIn("busy", blocked.stdout or "")
        self.assertEqual(0, released.returncode)
        self.assertIn("acquired", released.stdout or "")


class AtomicProcessLock(unittest.TestCase):
    def test_interrupt_during_claim_removes_partial_lock(self) -> None:
        with _project() as root, mock.patch.object(
            native_engine, "capture_process_identity", return_value=_identity(
                os.getpid(), "launcher"
            )
        ), mock.patch.object(
            native_engine.json, "dump", side_effect=KeyboardInterrupt()
        ), self.assertRaises(KeyboardInterrupt):
            native_engine.acquire_engine_lock(root)
            self.assertFalse(native_engine.engine_lock_path(root).exists())

    def test_two_stale_lock_contenders_cannot_both_claim(self) -> None:
        with _project() as root:
            path = native_engine.engine_lock_path(root)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema": 2,
                        "pid": 999999999,
                        "token": "stale",
                        "process_identity": _identity(
                            999999999, "stale"
                        ).as_json(),
                    }
                ),
                encoding="utf-8",
            )
            barrier = threading.Barrier(3)
            results: list[tuple[str | None, str]] = []

            def contend() -> None:
                barrier.wait()
                results.append(native_engine.acquire_engine_lock(root))

            current = _identity(os.getpid(), "launcher")
            with mock.patch.object(
                native_engine,
                "capture_process_identity",
                side_effect=lambda pid: current if pid == os.getpid() else None,
            ), mock.patch.object(
                native_engine,
                "_pid_is_alive",
                side_effect=lambda pid: pid == os.getpid(),
            ):
                threads = [threading.Thread(target=contend) for _ in range(2)]
                for thread in threads:
                    thread.start()
                barrier.wait()
                for thread in threads:
                    thread.join(timeout=5)

            winners = [token for token, _problem in results if token is not None]
            losers = [problem for token, problem in results if token is None]
            self.assertEqual(1, len(winners), results)
            self.assertEqual(1, len(losers), results)
            self.assertIn("abandoned Godot lock", losers[0])
            warning = native_engine.read_native_warning(root)
            self.assertIsNotNone(warning)
            self.assertEqual(
                "native-engine-lock-abandoned", warning["code"]
            )
            native_engine.release_engine_lock(root, str(winners[0]))

    def test_abandoned_lock_is_read_only_until_warning_is_durable(self) -> None:
        with _project() as root:
            path = native_engine.engine_lock_path(root)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "schema": 2,
                        "pid": 999999999,
                        "token": "abandoned",
                        "process_identity": _identity(
                            999999999, "abandoned"
                        ).as_json(),
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                native_engine, "_pid_liveness", return_value=native_engine.PID_DEAD
            ):
                inspection = native_engine.inspect_engine_lock(root)
            self.assertEqual(native_engine.LOCK_ABANDONED, inspection.state)
            self.assertTrue(path.exists())
            self.assertFalse(native_engine.native_warning_path(root).exists())

            original = native_engine.persist_native_failure

            def persist_before_recovery(*args: object, **kwargs: object) -> object:
                self.assertTrue(path.exists())
                return original(*args, **kwargs)

            with mock.patch.object(
                native_engine, "_pid_liveness", return_value=native_engine.PID_DEAD
            ), mock.patch.object(
                native_engine,
                "persist_native_failure",
                side_effect=persist_before_recovery,
            ):
                token, problem = native_engine.acquire_engine_lock(
                    root,
                    operation="verification",
                    verification_run_id="verify-abandoned",
                )
            self.assertIsNone(token)
            self.assertIn("triggering launch was refused", problem)
            self.assertFalse(path.exists())
            warning = native_engine.read_native_warning(root)
            self.assertEqual("verification-abandoned-lock-recovery", warning["operation"])
            self.assertEqual("verify-abandoned", warning["verification_run_id"])

    def test_unknown_owner_liveness_never_recovers_or_overwrites_lock(self) -> None:
        with _project() as root:
            path = native_engine.engine_lock_path(root)
            path.parent.mkdir(parents=True)
            content = json.dumps(
                {
                    "schema": 2,
                    "pid": 7777,
                    "token": "unknown",
                    "process_identity": _identity(7777, "unknown").as_json(),
                }
            )
            path.write_text(content, encoding="utf-8")
            with mock.patch.object(
                native_engine, "_pid_liveness", return_value=native_engine.PID_UNKNOWN
            ):
                token, problem = native_engine.acquire_engine_lock(root)
            self.assertIsNone(token)
            self.assertIn("could not be established", problem)
            self.assertEqual(content, path.read_text(encoding="utf-8"))


class ProcessLivenessTriState(unittest.TestCase):
    def test_windows_wait_failure_is_unknown_not_dead(self) -> None:
        kernel = mock.Mock()
        kernel.OpenProcess.return_value = 123
        kernel.WaitForSingleObject.return_value = process_supervisor.WAIT_FAILED
        with mock.patch.object(
                process_supervisor, "_is_windows", return_value=True
        ), \
                mock.patch.object(
                    process_supervisor.ctypes,
                    "WinDLL",
                    return_value=kernel,
                    create=True,
                ):
            state = process_supervisor.pid_liveness(4242)
        self.assertEqual(process_supervisor.PID_UNKNOWN, state)
        kernel.OpenProcess.assert_called_once_with(
            process_supervisor.SYNCHRONIZE, False, 4242
        )
        kernel.WaitForSingleObject.assert_called_once_with(123, 0)
        kernel.CloseHandle.assert_called_once_with(123)

    def test_windows_access_denied_is_unknown_not_assumed_alive_or_dead(self) -> None:
        kernel = mock.Mock()
        kernel.OpenProcess.return_value = 0
        with mock.patch.object(
                process_supervisor, "_is_windows", return_value=True
        ), \
                mock.patch.object(
                    process_supervisor.ctypes,
                    "WinDLL",
                    return_value=kernel,
                    create=True,
                ), mock.patch.object(
                    process_supervisor.ctypes,
                    "get_last_error",
                    return_value=process_supervisor.ERROR_ACCESS_DENIED,
                    create=True,
                ):
            state = process_supervisor.pid_liveness(4242)
        self.assertEqual(process_supervisor.PID_UNKNOWN, state)


class ExecutableResolution(unittest.TestCase):
    def test_ordinary_interpreter_is_accepted(self) -> None:
        candidate = (
            Path(sys.base_prefix) / Path(sys.executable).name
            if os.name == "nt"
            else Path(sys.executable).resolve(strict=True)
        )
        self.assertEqual(
            str(candidate),
            process_supervisor.isolated_python_executable(candidate),
        )

    def test_current_app_exec_alias_uses_exact_base_prefix_interpreter(self) -> None:
        alias = PureWindowsPath(
            r"C:\Users\person\AppData\Local\Microsoft\WindowsApps\python.exe"
        )
        ordinary = PureWindowsPath(
            r"C:\Program Files\WindowsApps\Python\python.exe"
        )
        with mock.patch.object(
            process_supervisor.sys, "executable", str(alias)
        ), mock.patch.object(
            process_supervisor.sys, "base_prefix", str(ordinary.parent)
        ), mock.patch.object(
            process_supervisor, "Path", PureWindowsPath
        ), mock.patch.object(
            process_supervisor,
            "_windows_app_execution_alias",
            return_value=True,
        ), mock.patch.object(
            process_supervisor,
            "validated_executable",
            side_effect=[ValueError("alias"), str(ordinary)],
        ) as validate:
            selected = process_supervisor.isolated_python_executable(alias)
        self.assertEqual(str(ordinary), selected)
        self.assertEqual(
            [
                mock.call(alias, label="isolated Python interpreter"),
                mock.call(ordinary, label="isolated Python interpreter"),
            ],
            validate.call_args_list,
        )

    def test_app_exec_alias_requires_exact_tag_and_shape(self) -> None:
        alias = mock.Mock()
        alias.is_absolute.return_value = True
        info = mock.Mock(
            st_mode=stat.S_IFREG,
            st_size=0,
            st_file_attributes=0x0400,
            st_reparse_tag=0x8000001B,
        )
        alias.lstat.return_value = info
        with mock.patch.object(
            process_supervisor, "_is_windows", return_value=True
        ):
            self.assertTrue(
                process_supervisor._windows_app_execution_alias(alias)
            )
            info.st_reparse_tag = 0xA000000C
            self.assertFalse(
                process_supervisor._windows_app_execution_alias(alias)
            )
            info.st_reparse_tag = 0x8000001B
            info.st_size = 1
            self.assertFalse(
                process_supervisor._windows_app_execution_alias(alias)
            )

    def test_relative_interpreter_is_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute path"):
            process_supervisor.isolated_python_executable("python.exe")

    def test_other_redirected_current_interpreter_is_refused(self) -> None:
        redirected = Path(r"C:\redirected\python.exe")
        with mock.patch.object(
            process_supervisor.sys, "executable", str(redirected)
        ), mock.patch.object(
            process_supervisor,
            "validated_executable",
            side_effect=ValueError("redirected"),
        ), mock.patch.object(
            process_supervisor,
            "_windows_app_execution_alias",
            return_value=False,
        ), self.assertRaisesRegex(ValueError, "redirected"):
            process_supervisor.isolated_python_executable(redirected)

    def test_posix_safe_symlink_resolves_to_exact_target(self) -> None:
        if os.name == "nt":
            return
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            link = directory / "git"
            target = Path(sys.executable).resolve(strict=True)
            link.symlink_to(target)
            selected = process_supervisor.resolve_ordinary_executable(
                "git",
                environment={"PATH": str(directory)},
                excluded_roots=(ROOT,),
            )
        self.assertEqual(str(target), selected)

    def test_posix_exact_current_interpreter_symlink_resolves(self) -> None:
        if os.name == "nt":
            return
        with tempfile.TemporaryDirectory() as temporary:
            alias = Path(temporary) / "python"
            target = Path(sys.executable).resolve(strict=True)
            alias.symlink_to(target)
            with mock.patch.object(
                process_supervisor.sys, "executable", str(alias)
            ):
                selected = process_supervisor.isolated_python_executable(alias)
        self.assertEqual(str(target), selected)

    def test_posix_arbitrary_interpreter_symlink_is_refused(self) -> None:
        if os.name == "nt":
            return
        with tempfile.TemporaryDirectory() as temporary:
            alias = Path(temporary) / "python"
            alias.symlink_to(Path(sys.executable).resolve(strict=True))
            with self.assertRaises(ValueError):
                process_supervisor.isolated_python_executable(alias)

    def test_posix_symlink_into_project_is_refused(self) -> None:
        if os.name == "nt":
            return
        with _project() as root, tempfile.TemporaryDirectory() as temporary:
            target = root / "git"
            target.write_bytes(Path(sys.executable).read_bytes())
            target.chmod(0o755)
            link = Path(temporary) / "git"
            link.symlink_to(target)
            with self.assertRaises(FileNotFoundError):
                process_supervisor.resolve_ordinary_executable(
                    "git",
                    environment={"PATH": temporary},
                    excluded_roots=(root, ROOT),
                )

    def test_windows_target_local_git_node_and_cmd_never_execute(self) -> None:
        if os.name != "nt":
            return
        with _project() as target:
            marker = target / "hijacked.txt"
            for name in ("git.cmd", "node.cmd", "cmd.cmd"):
                (target / name).write_text(
                    f'@echo off\r\n>"{marker}" echo {name}\r\n',
                    encoding="utf-8",
                )
            environment = dict(os.environ)
            environment["PATH"] = str(target) + os.pathsep + environment["PATH"]
            git = process_supervisor.resolve_ordinary_executable(
                "git",
                environment=environment,
                excluded_roots=(target, ROOT),
            )
            with self.assertRaises(FileNotFoundError):
                process_supervisor.resolve_ordinary_executable(
                    "node",
                    environment={"PATH": str(target)},
                    excluded_roots=(target, ROOT),
                )
            try:
                node = process_supervisor.resolve_ordinary_executable(
                    "node",
                    environment=environment,
                    excluded_roots=(target, ROOT),
                )
            except FileNotFoundError:
                node = ""
            cmd = process_supervisor.windows_command_processor()
            commands = [
                [git, "--version"],
                [cmd, "/d", "/c", "exit", "0"],
            ]
            if node:
                commands.append([node, "--version"])
            for command in commands:
                completed = subprocess.run(
                    command,
                    cwd=target,
                    env=environment,
                    capture_output=True,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(0, completed.returncode, command)
            self.assertFalse(marker.exists())


class BoundedProcessLifecycle(unittest.TestCase):
    def test_real_native_boundary_contains_a_grandchild_without_godot(self) -> None:
        # The public self-test exports KIT_ENGINE_DISABLED=1 so no test can
        # accidentally launch Godot.  This proof launches only the current
        # Python interpreter and its helper grandchild; opt the bounded helper
        # into the native boundary explicitly.
        with _project() as root, mock.patch.dict(
            os.environ, {"KIT_ENGINE_DISABLED": "0"}
        ):
            pid_file = root / "native-timeout-tree.txt"
            command = _real_tree_command(pid_file)
            result = native_engine.run_godot(
                command[0],
                command[1:],
                root=root,
                cwd=root,
                timeout=1,
                operation="containment-test",
            )
            child_pid, grandchild_pid = _wait_for_pid_file(pid_file)
        self.assertEqual("engine-timeout", result.failure_class)
        self.assertIn("owned process tree terminated", result.output)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if all(
                process_supervisor.pid_liveness(pid)
                == process_supervisor.PID_DEAD
                for pid in (child_pid, grandchild_pid)
            ):
                break
            time.sleep(0.02)
        self.assertEqual(
            [process_supervisor.PID_DEAD, process_supervisor.PID_DEAD],
            [
                process_supervisor.pid_liveness(child_pid),
                process_supervisor.pid_liveness(grandchild_pid),
            ],
        )

    def test_unassigned_death_must_be_verified_before_breakaway_retry(self) -> None:
        process = _process(code=None)
        process.wait.side_effect = subprocess.TimeoutExpired("Godot.exe", 10)
        process.poll.return_value = None
        job = mock.Mock()
        job.assign.return_value = (False, native_engine.ERROR_ACCESS_DENIED)
        with mock.patch.object(
            native_engine, "_create_windows_job", return_value=job
        ), mock.patch.object(
            native_engine, "_start_process", return_value=process
        ) as start, mock.patch.object(
            native_engine, "_terminate_pid_tree"
        ), self.assertRaisesRegex(
            native_engine.NativeContainmentError,
            "termination could not be verified",
        ):
            native_engine._start_bounded_windows_process(
                ["Godot.exe", "--headless"], cwd=ROOT, capture_output=True
            )
        start.assert_called_once()
        job.close.assert_called_once_with()

    def test_keyboard_interrupt_terminates_tree_and_releases_lock(self) -> None:
        with _project() as root:
            process = _process(code=None)
            process.wait.side_effect = KeyboardInterrupt()
            started = native_engine.NativeStart(
                process=process,
                lock_token="owned",
                failure=None,
                executable="Godot.exe",
            )
            with mock.patch.object(
                native_engine, "start_godot", return_value=started
            ), mock.patch.object(
                native_engine, "terminate_owned_process_tree", return_value=True
            ) as terminate, mock.patch.object(
                native_engine, "release_engine_lock"
            ) as release, self.assertRaises(KeyboardInterrupt):
                native_engine.run_godot(
                    "Godot.exe", ["--headless"], root=root, cwd=root, timeout=3
                )
            terminate.assert_called_once_with(process, windows_job=None)
            release.assert_called_once_with(root, "owned")

    def test_keyboard_interrupt_during_start_releases_untransferred_lock(self) -> None:
        with _project() as root, mock.patch.object(
            native_engine, "acquire_engine_lock", return_value=("owned", "")
        ), mock.patch.object(
            native_engine, "_start_process", side_effect=KeyboardInterrupt()
        ), mock.patch.object(
            native_engine, "release_engine_lock"
        ) as release, mock.patch.dict(
            os.environ, {"KIT_ENGINE_DISABLED": ""}
        ), self.assertRaises(KeyboardInterrupt):
            native_engine.start_godot(
                "Godot", ["--headless"], root=root, cwd=root, capture_output=False
            )
        release.assert_called_once_with(root, "owned")

    def test_posix_group_is_killed_even_after_leader_exits(self) -> None:
        process = _process(pid=2468, code=0)
        with mock.patch.object(native_engine, "_terminate_pid_tree") as terminate:
            self.assertTrue(native_engine.terminate_owned_process_tree(process))
        terminate.assert_called_once_with(2468)
        process.wait.assert_called_once_with(timeout=10)

    def test_normal_run_contains_descendants_after_collecting_bounded_output(self) -> None:
        with _project() as root:
            process = _process(code=0, output=b"complete")
            started = native_engine.NativeStart(
                process=process,
                lock_token="owned",
                failure=None,
                executable="Godot.exe",
            )
            with mock.patch.object(
                native_engine, "start_godot", return_value=started
            ), mock.patch.object(
                native_engine, "terminate_owned_process_tree", return_value=True
            ) as terminate, mock.patch.object(
                native_engine, "release_engine_lock"
            ):
                result = native_engine.run_godot(
                    "Godot.exe", ["--headless"], root=root, cwd=root, timeout=3
                )
        self.assertEqual(0, result.exit_code)
        self.assertEqual("complete", result.output)
        terminate.assert_called_once_with(process, windows_job=None)

    def test_capture_retains_only_a_bounded_tail_while_draining(self) -> None:
        body = b"A" * (native_engine.OUTPUT_CAP_BYTES * 3) + b"FINAL"
        collector = native_engine._BoundedOutputCollector(io.BytesIO(body))
        captured = collector.finish()
        self.assertLessEqual(
            collector.retained_bytes,
            native_engine.OUTPUT_CAP_BYTES
            - native_engine.OUTPUT_HEADER_RESERVE_BYTES,
        )
        self.assertLessEqual(len(captured), native_engine.OUTPUT_CAP_BYTES)
        self.assertIn(b"truncated during capture", captured)
        self.assertTrue(captured.endswith(b"FINAL"))


class RetainedProcessIdentity(unittest.TestCase):
    def _owned_receipt(
        self,
        root: Path,
        *,
        pid: int = 9321,
    ) -> native_engine.BackgroundReceipt:
        token, problem = native_engine.acquire_engine_lock(root)
        self.assertIsNotNone(token, problem)
        identity = _identity(pid, "retained")
        self.assertTrue(
            native_engine._transfer_engine_lock(root, str(token), pid, identity)
        )
        return native_engine.BackgroundReceipt(pid, str(token), identity)

    def test_schema_two_receipt_round_trips_process_identity(self) -> None:
        with _project() as root:
            path = root / ".kit/runtime/gdls.json"
            identity = _identity(1234, "server")
            native_engine.write_background_receipt(
                path,
                pid=1234,
                token="token",
                process_identity=identity,
            )
            self.assertEqual(
                native_engine.BackgroundReceipt(1234, "token", identity),
                native_engine.read_background_receipt(path),
            )
            value = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(2, value["schema"])
            self.assertEqual("server", value["process_identity"]["creation_marker"])

    def test_reused_pid_is_refused_without_termination(self) -> None:
        with _project() as root, mock.patch.object(
            native_engine,
            "capture_process_identity",
            side_effect=lambda pid: _identity(os.getpid(), "launcher"),
        ):
            receipt = self._owned_receipt(root)
            with mock.patch.object(
                native_engine, "_pid_is_alive", return_value=True
            ), mock.patch.object(
                native_engine,
                "capture_process_identity",
                return_value=_identity(receipt.pid, "reused"),
            ), mock.patch.object(native_engine, "_terminate_pid_tree") as terminate:
                inspection = native_engine.inspect_background_owner(root, receipt)
                stopped = native_engine.stop_owned_background(root, receipt)
            self.assertEqual(
                native_engine.BACKGROUND_OWNERSHIP_MISMATCH, inspection.state
            )
            self.assertFalse(stopped)
            terminate.assert_not_called()
            self.assertTrue(native_engine.engine_lock_path(root).is_file())

    def test_stop_releases_lock_only_after_exact_owner_death_is_proven(self) -> None:
        with _project() as root, mock.patch.object(
            native_engine,
            "capture_process_identity",
            side_effect=lambda pid: _identity(os.getpid(), "launcher"),
        ):
            receipt = self._owned_receipt(root)
            alive = [True, False]
            observed = [
                receipt.process_identity,
                receipt.process_identity,
                None,
            ]
            with mock.patch.object(
                native_engine,
                "_pid_is_alive",
                side_effect=lambda _pid: alive.pop(0),
            ), mock.patch.object(
                native_engine,
                "capture_process_identity",
                side_effect=lambda _pid: observed.pop(0),
            ), mock.patch.object(
                native_engine, "terminate_exact_background_owner", return_value=True
            ) as terminate:
                self.assertTrue(native_engine.stop_owned_background(root, receipt))
            terminate.assert_called_once_with(receipt)
            self.assertFalse(native_engine.engine_lock_path(root).exists())

    def test_failed_termination_preserves_ownership_lock(self) -> None:
        with _project() as root, mock.patch.object(
            native_engine,
            "capture_process_identity",
            side_effect=lambda pid: _identity(os.getpid(), "launcher"),
        ):
            receipt = self._owned_receipt(root)
            clock = iter((0.0, 0.0, 11.0))
            with mock.patch.object(
                native_engine, "_pid_is_alive", return_value=True
            ), mock.patch.object(
                native_engine,
                "capture_process_identity",
                return_value=receipt.process_identity,
            ), mock.patch.object(native_engine.time, "monotonic", side_effect=clock), \
                    mock.patch.object(native_engine.time, "sleep"):
                self.assertFalse(native_engine.stop_owned_background(root, receipt))
            self.assertTrue(native_engine.engine_lock_path(root).is_file())


class NativeWarningCompareAndSwap(unittest.TestCase):
    def _failure(self, failure_class: str = "engine-timeout") -> native_engine.NativeResult:
        return native_engine.NativeResult(
            exit_code=native_engine.TIMEOUT_EXIT,
            output="private output",
            executable="Godot.exe",
            started=True,
            failure_class=failure_class,
            failure_code="native-engine-timeout",
            timeout_seconds=3,
        )

    def test_new_warning_cannot_be_cleared_by_an_older_verification_snapshot(self) -> None:
        with _project() as root:
            warning = native_engine.persist_native_failure(
                root,
                self._failure(),
                operation="health",
                verification_run_id="verify-one",
            )
            self.assertEqual("verify-one", warning["verification_run_id"])
            old_identity = native_engine.native_warning_identity(root)
            native_engine.persist_native_failure(
                root,
                self._failure("native-crash"),
                operation="smoke",
                verification_run_id="verify-two",
            )
            self.assertFalse(
                native_engine.resolve_native_warning(
                    root,
                    verification_scope="full",
                    expected_identity=old_identity,
                )
            )
            self.assertIsNotNone(native_engine.read_native_warning(root))

    def test_missing_compare_and_swap_identity_is_rejected(self) -> None:
        with _project() as root:
            self.assertIsNone(native_engine.native_warning_identity(root))
            native_engine.persist_native_failure(
                root, self._failure(), operation="health"
            )
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                native_engine.resolve_native_warning(
                    root,
                    verification_scope="strict",
                    expected_identity=None,
                )
            self.assertIsNotNone(native_engine.read_native_warning(root))

    def test_exact_warning_identity_can_be_resolved(self) -> None:
        with _project() as root:
            native_engine.persist_native_failure(
                root, self._failure(), operation="health"
            )
            identity = native_engine.native_warning_identity(root)
            native_engine.authorize_recovery_retry(
                root, str(identity), "resolution-run"
            )
            self.assertTrue(
                native_engine.native_retry_authorization_path(root).exists()
            )
            self.assertTrue(
                native_engine.resolve_native_warning(
                    root,
                    verification_scope="full",
                    expected_identity=identity,
                )
            )
            self.assertIsNone(native_engine.read_native_warning(root))
            self.assertFalse(
                native_engine.native_retry_authorization_path(root).exists()
            )

    def test_malformed_warning_is_unknown_not_clean(self) -> None:
        with _project() as root:
            path = native_engine.native_warning_path(root)
            path.parent.mkdir(parents=True)
            path.write_text("not-json", encoding="utf-8")
            snapshot = native_engine.snapshot_native_warning(root)
            self.assertEqual(native_engine.WARNING_UNKNOWN, snapshot.state)
            self.assertIsNone(snapshot.identity)

    def test_missing_warning_snapshot_is_strictly_read_only(self) -> None:
        with _project() as root:
            missing = root / "absent-runtime" / "native-warning.json"
            with mock.patch.object(
                native_engine, "native_warning_path", return_value=missing
            ), mock.patch.object(
                Path, "mkdir", side_effect=AssertionError("mkdir is mutation")
            ), mock.patch.object(
                Path, "write_text", side_effect=AssertionError("write is mutation")
            ), mock.patch.object(
                Path, "open", side_effect=AssertionError("open is mutation")
            ):
                snapshot = native_engine.snapshot_native_warning(root)
            self.assertEqual(native_engine.WARNING_CLEAN, snapshot.state)
            self.assertFalse(missing.parent.exists())

    def test_retry_token_is_exact_run_bound_and_consumed_by_one_process(self) -> None:
        with _project() as root:
            native_engine.persist_native_failure(
                root, self._failure(), operation="health"
            )
            identity = native_engine.native_warning_identity(root)
            self.assertIsNotNone(identity)
            token = native_engine.authorize_recovery_retry(
                root, str(identity), "verify-run-one"
            )
            persisted = native_engine.native_retry_authorization_path(
                root
            ).read_text(encoding="utf-8")
            self.assertNotIn(token, persisted)
            self.assertTrue(
                native_engine.consume_recovery_retry(
                    root,
                    token=token,
                    run_id="verify-run-one",
                    operation="verification",
                )
            )
            self.assertTrue(
                native_engine.consume_recovery_retry(
                    root,
                    token=token,
                    run_id="verify-run-one",
                    operation="verification-stage-two",
                )
            )
            self.assertFalse(
                native_engine.consume_recovery_retry(
                    root,
                    token=token,
                    run_id="verify-run-two",
                    operation="verification",
                )
            )
            impostor = _identity(os.getpid(), "different-consumer")
            with mock.patch.object(
                native_engine, "capture_process_identity", return_value=impostor
            ):
                self.assertFalse(
                    native_engine.consume_recovery_retry(
                        root,
                        token=token,
                        run_id="verify-run-one",
                        operation="verification-stage-three",
                    )
                )

    def test_new_warning_invalidates_an_issued_retry_token(self) -> None:
        with _project() as root:
            native_engine.persist_native_failure(
                root, self._failure(), operation="health"
            )
            identity = native_engine.native_warning_identity(root)
            token = native_engine.authorize_recovery_retry(
                root, str(identity), "verify-run"
            )
            native_engine.persist_native_failure(
                root, self._failure("native-crash"), operation="smoke"
            )
            self.assertFalse(
                native_engine.consume_recovery_retry(
                    root,
                    token=token,
                    run_id="verify-run",
                    operation="verification",
                )
            )

    def test_unresolved_warning_refuses_unapproved_native_launch(self) -> None:
        # Exercise the warning authorization boundary itself rather than the
        # public self-test's earlier no-engine guard.  Process creation remains
        # impossible because the refusal occurs before acquire_engine_lock.
        with _project() as root, mock.patch.dict(
            os.environ, {"KIT_ENGINE_DISABLED": "0"}
        ):
            native_engine.persist_native_failure(
                root, self._failure(), operation="health"
            )
            with mock.patch.object(
                native_engine, "acquire_engine_lock"
            ) as acquire:
                started = native_engine.start_godot(
                    "Godot.exe",
                    ["--headless"],
                    root=root,
                    cwd=root,
                )
            self.assertEqual(
                "native-retry-required", started.failure.failure_class
            )
            acquire.assert_not_called()

    def test_native_child_environment_excludes_host_secrets_and_gate_tokens(self) -> None:
        source = {
            "PATH": "safe-path",
            "TEMP": "safe-temp",
            "LANG": "en_AU.UTF-8",
            "LC_MESSAGES": "en_AU.UTF-8",
            "KIT_VERIFY_NONCE": "run-secret",
            "KIT_NATIVE_RETRY_TOKEN": "retry-secret",
            "GITHUB_TOKEN": "provider-secret",
            "AZURE_CLIENT_SECRET": "cloud-secret",
        }
        child = native_engine.native_child_environment(source)
        self.assertEqual("safe-path", child["PATH"])
        self.assertEqual("safe-temp", child["TEMP"])
        self.assertEqual("en_AU.UTF-8", child["LC_MESSAGES"])
        self.assertNotIn("KIT_VERIFY_NONCE", child)
        self.assertNotIn("KIT_NATIVE_RETRY_TOKEN", child)
        self.assertNotIn("GITHUB_TOKEN", child)
        self.assertNotIn("AZURE_CLIENT_SECRET", child)


if __name__ == "__main__":
    unittest.main()
