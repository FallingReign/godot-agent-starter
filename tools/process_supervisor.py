#!/usr/bin/env python3
"""Stdlib-only containment for kit-owned child processes.

This is the outer process boundary used by command orchestrators.  It is
deliberately independent of Godot and project state:

* captured stdout/stderr are drained continuously into bounded tails;
* Windows children are created suspended, assigned to a
  ``KILL_ON_JOB_CLOSE`` Job Object, then resumed;
* POSIX children receive their own process group and a separate lifeline
  process kills that group if the Python owner disappears unexpectedly;
* timeout and explicit cancellation first request a graceful POSIX interrupt,
  then use a bounded hard fallback and report whether termination was proven.

The lifeline makes nested supervisors composable.  If an outer owner is killed,
its guard kills the direct child group; that closes the next guard's control
pipe, cascading containment to any deeper group.  A child may deliberately
retain a separately authenticated background process, but callers must opt in
to Windows breakaway for that one command.
"""
from __future__ import annotations

import ctypes
import errno
import os
import select
import signal
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, Sequence

DEFAULT_OUTPUT_CAP_BYTES = 2 * 1024 * 1024
OUTPUT_READ_BYTES = 64 * 1024
OUTPUT_HEADER_RESERVE_BYTES = 512
GRACE_SECONDS = 3.0
HARD_KILL_SECONDS = 10.0
WATCHDOG_READY_SECONDS = 5.0

JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_SUSPENDED = 0x00000004
ERROR_ACCESS_DENIED = 5
ERROR_NOT_FOUND = 1168
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
WAIT_OBJECT_0 = 0x00000000
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000

PID_ALIVE = "alive"
PID_DEAD = "dead"
PID_UNKNOWN = "unknown"


def _is_windows() -> bool:
    """Report the host branch without making tests mutate Python's global OS state."""
    return os.name == "nt"


class Cancellation(Protocol):
    def is_set(self) -> bool: ...


class ProcessContainmentError(OSError):
    """The supervisor could not establish or verify the ownership boundary."""


class ExclusiveLockUnavailable(RuntimeError):
    """Another live process owns a named OS-backed file lock."""


@contextmanager
def exclusive_file_lock(path: Path, *, label: str) -> Iterator[None]:
    """Acquire one nonblocking, stale-file-safe cross-process lock.

    The sidecar remains on disk deliberately; ownership lives in the kernel and
    disappears when the holding process exits.  Contention raises
    :class:`ExclusiveLockUnavailable`. Filesystem/open failures remain their
    original :class:`OSError`, and exceptions from the protected body propagate
    after the OS lock is released.
    """
    lock_label = str(label).strip()
    if not lock_label:
        raise ValueError("exclusive lock label is empty")
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b", buffering=0)
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        try:
            if _is_windows():
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except (BlockingIOError, OSError) as exc:
            if isinstance(exc, BlockingIOError) or getattr(exc, "errno", None) in {
                errno.EACCES,
                errno.EAGAIN,
            }:
                raise ExclusiveLockUnavailable(
                    f"another {lock_label} is already running"
                ) from exc
            raise
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if _is_windows():
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass
        handle.close()


@dataclass(frozen=True)
class SupervisedResult:
    """Observable result of one bounded child command."""

    returncode: int | None
    stdout: str | None
    stderr: str | None
    duration_seconds: float
    timed_out: bool = False
    cancelled: bool = False
    termination_verified: bool = True
    launch_error: str = ""


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JobObjectBasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


def _handle_value(value: Any) -> int:
    raw = getattr(value, "value", value)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


class WindowsJob:
    """An owned Windows Job whose last handle contains assigned descendants."""

    def __init__(self, handle: int, kernel32: Any) -> None:
        self._handle = int(handle)
        self._kernel32 = kernel32

    @property
    def closed(self) -> bool:
        return self._handle == 0

    def assign(self, process: subprocess.Popen[bytes]) -> tuple[bool, int]:
        process_handle = _handle_value(getattr(process, "_handle", None))
        if self.closed or process_handle == 0:
            return False, 6
        if self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            return True, 0
        return False, int(ctypes.get_last_error() or 1)

    def resume(self, process: subprocess.Popen[bytes]) -> tuple[bool, int]:
        invalid_handle = _handle_value(ctypes.c_void_p(-1))
        snapshot = _handle_value(
            self._kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        )
        if snapshot in (0, invalid_handle):
            return False, int(ctypes.get_last_error() or 1)
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            present = bool(
                self._kernel32.Thread32First(snapshot, ctypes.byref(entry))
            )
            while present:
                if int(entry.th32OwnerProcessID) == int(process.pid):
                    thread = _handle_value(
                        self._kernel32.OpenThread(
                            THREAD_SUSPEND_RESUME,
                            False,
                            int(entry.th32ThreadID),
                        )
                    )
                    if thread == 0:
                        return False, int(ctypes.get_last_error() or 1)
                    try:
                        previous = int(self._kernel32.ResumeThread(thread))
                        if previous == 0xFFFFFFFF:
                            return False, int(ctypes.get_last_error() or 1)
                        return True, 0
                    finally:
                        self._kernel32.CloseHandle(thread)
                present = bool(
                    self._kernel32.Thread32Next(snapshot, ctypes.byref(entry))
                )
            return False, int(ctypes.get_last_error() or ERROR_NOT_FOUND)
        finally:
            self._kernel32.CloseHandle(snapshot)

    def terminate(self, exit_code: int = 1) -> bool:
        if self.closed:
            return True
        return bool(
            self._kernel32.TerminateJobObject(
                self._handle, int(exit_code) & 0xFFFFFFFF
            )
        )

    def active_processes(self) -> int | None:
        if self.closed:
            return 0
        value = _JobObjectBasicAccountingInformation()
        returned = wintypes.DWORD(0)
        if not self._kernel32.QueryInformationJobObject(
            self._handle,
            JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(value),
            ctypes.sizeof(value),
            ctypes.byref(returned),
        ):
            return None
        return int(value.ActiveProcesses)

    def close(self) -> None:
        if self.closed:
            return
        handle = self._handle
        self._handle = 0
        self._kernel32.CloseHandle(handle)

    def __del__(self) -> None:
        try:
            self.close()
        except (AttributeError, OSError):
            pass


def _create_windows_job(*, allow_breakaway: bool) -> WindowsJob:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    ]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    ]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    raw = kernel32.CreateJobObjectW(None, None)
    handle = _handle_value(raw)
    if handle == 0:
        raise ProcessContainmentError(
            int(ctypes.get_last_error() or 1), "CreateJobObjectW failed"
        )
    job = WindowsJob(handle, kernel32)
    limits = _JobObjectExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if allow_breakaway:
        limits.BasicLimitInformation.LimitFlags |= JOB_OBJECT_LIMIT_BREAKAWAY_OK
    if not kernel32.SetInformationJobObject(
        handle,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        error = int(ctypes.get_last_error() or 1)
        job.close()
        raise ProcessContainmentError(
            error, "SetInformationJobObject containment failed"
        )
    return job


def _windows_error_mode() -> tuple[Any | None, int | None]:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetErrorMode.argtypes = []
        kernel32.GetErrorMode.restype = wintypes.UINT
        kernel32.SetErrorMode.argtypes = [wintypes.UINT]
        kernel32.SetErrorMode.restype = wintypes.UINT
        current = int(kernel32.GetErrorMode())
        previous = int(kernel32.SetErrorMode(current | 0x0001 | 0x0002))
        return kernel32, previous
    except (AttributeError, OSError, TypeError, ValueError):
        return None, None


def _start_windows(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None,
    capture_output: bool,
    allow_breakaway: bool,
) -> tuple[subprocess.Popen[bytes], WindowsJob]:
    job = _create_windows_job(allow_breakaway=allow_breakaway)
    process: subprocess.Popen[bytes] | None = None
    assigned = False
    kernel32, previous_mode = _windows_error_mode()
    try:
        flags = CREATE_SUSPENDED | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(
            [str(part) for part in command],
            cwd=str(cwd),
            env=dict(environment) if environment is not None else None,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            shell=False,
            creationflags=flags,
        )
        assigned, error = job.assign(process)
        if not assigned:
            _terminate_unassigned_windows(process)
            process = None
            raise ProcessContainmentError(
                error, "AssignProcessToJobObject failed"
            )
        resumed, error = job.resume(process)
        if not resumed:
            raise ProcessContainmentError(error, "ResumeThread failed")
        return process, job
    except BaseException:
        if process is not None:
            try:
                if assigned:
                    job.terminate(1)
                    process.wait(timeout=HARD_KILL_SECONDS)
                else:
                    _terminate_unassigned_windows(process)
            except (OSError, subprocess.SubprocessError):
                pass
        job.close()
        raise
    finally:
        if kernel32 is not None and previous_mode is not None:
            kernel32.SetErrorMode(previous_mode)


def _terminate_unassigned_windows(process: subprocess.Popen[bytes]) -> None:
    try:
        process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=HARD_KILL_SECONDS)
    except (OSError, subprocess.SubprocessError):
        pass
    if process.poll() is None:
        raise ProcessContainmentError(
            ERROR_ACCESS_DENIED,
            "unassigned suspended process termination could not be verified",
        )


class _BoundedCollector:
    def __init__(self, stream: Any, cap_bytes: int) -> None:
        self._stream = stream
        self._cap = max(1024, int(cap_bytes))
        self._buffer = bytearray()
        self._dropped = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._read,
            name="kit-supervised-output",
            daemon=True,
        )
        self._thread.start()

    def _append(self, chunk: bytes) -> None:
        limit = max(1, self._cap - OUTPUT_HEADER_RESERVE_BYTES)
        if len(chunk) > limit:
            self._dropped += len(chunk) - limit
            chunk = chunk[-limit:]
        self._buffer.extend(chunk)
        overflow = len(self._buffer) - limit
        if overflow > 0:
            del self._buffer[:overflow]
            self._dropped += overflow

    def _read(self) -> None:
        if self._stream is None:
            return
        try:
            while True:
                chunk = self._stream.read(OUTPUT_READ_BYTES)
                if not chunk:
                    return
                raw = chunk if isinstance(chunk, bytes) else bytes(chunk)
                with self._lock:
                    self._append(raw)
        except (OSError, TypeError, ValueError):
            return

    def finish(self, timeout: float = HARD_KILL_SECONDS) -> str:
        self._thread.join(timeout=max(0.0, timeout))
        if self._thread.is_alive() and self._stream is not None:
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass
            self._thread.join(timeout=0.25)
        if self._stream is not None:
            try:
                self._stream.close()
            except (OSError, ValueError):
                pass
        with self._lock:
            raw = bytes(self._buffer)
            dropped = self._dropped
        text = raw.decode("utf-8", errors="replace")
        if dropped:
            return (
                f"[supervised output truncated: {dropped} bytes omitted; "
                "showing bounded tail]\n" + text
            )
        return text


def _linux_parent_death_signal(expected_parent: int | None = None) -> None:
    """Close the POSIX spawn-to-lifeline gap on Linux when available."""
    if not sys.platform.startswith("linux"):
        return
    parent = int(expected_parent if expected_parent is not None else os.getppid())
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        prctl = libc.prctl
        prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        prctl.restype = ctypes.c_int
        if prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
            os._exit(127)
        if os.getppid() != parent:
            os._exit(127)
    except (AttributeError, OSError, TypeError, ValueError):
        os._exit(127)


def posix_parent_death_command(command: Sequence[str]) -> list[str]:
    """Wrap an exec so Linux can arm PDEATHSIG without unsafe ``preexec_fn``.

    The wrapper immediately replaces itself with the requested child, keeping
    the same PID and process group. Other POSIX systems use the command
    directly and rely on the independently spawned lifeline.
    """
    values = [str(part) for part in command]
    if not sys.platform.startswith("linux"):
        return values
    return [
        sys.executable,
        str(Path(__file__).resolve()),
        "--exec-with-parent-death",
        str(os.getpid()),
        *values,
    ]


def _group_liveness(pgid: int) -> str:
    if pgid <= 0:
        return PID_DEAD
    try:
        os.killpg(pgid, 0)
        return PID_ALIVE
    except ProcessLookupError:
        return PID_DEAD
    except PermissionError:
        return PID_UNKNOWN
    except (OSError, ValueError):
        return PID_UNKNOWN


def _signal_group(pgid: int, value: signal.Signals) -> bool:
    try:
        os.killpg(pgid, value)
        return True
    except ProcessLookupError:
        return True
    except (OSError, ValueError):
        return False


def _wait_group_dead(pgid: int, timeout: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if _group_liveness(pgid) == PID_DEAD:
            return True
        time.sleep(0.02)
    return _group_liveness(pgid) == PID_DEAD


def _watch_posix_group(pgid: int, control_fd: int, ready_fd: int) -> int:
    try:
        os.write(ready_fd, b"R")
    except OSError:
        return 2
    finally:
        try:
            os.close(ready_fd)
        except OSError:
            pass
    try:
        command = os.read(control_fd, 1)
    except OSError:
        command = b""
    finally:
        try:
            os.close(control_fd)
        except OSError:
            pass
    if command == b"D":
        return 0
    _signal_group(pgid, signal.SIGKILL)
    return 0


class PosixGroupGuard:
    """Independent lifeline for a process group owned by the current process."""

    def __init__(self, pgid: int, process: subprocess.Popen[bytes], control_fd: int) -> None:
        self.pgid = int(pgid)
        self._process = process
        self._control_fd = int(control_fd)
        self._closed = False

    @classmethod
    def start(cls, pgid: int) -> PosixGroupGuard:
        control_read, control_write = os.pipe()
        ready_read, ready_write = os.pipe()
        for descriptor in (control_read, ready_write):
            os.set_inheritable(descriptor, True)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--watch-posix-group",
            str(int(pgid)),
            str(control_read),
            str(ready_write),
        ]
        watcher: subprocess.Popen[bytes] | None = None
        try:
            watcher = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                start_new_session=True,
                close_fds=True,
                pass_fds=(control_read, ready_write),
            )
        finally:
            os.close(control_read)
            os.close(ready_write)
        try:
            readable, _, _ = select.select([ready_read], [], [], WATCHDOG_READY_SECONDS)
            ready = os.read(ready_read, 1) if readable else b""
        finally:
            os.close(ready_read)
        if watcher is None or ready != b"R" or watcher.poll() is not None:
            try:
                os.close(control_write)
            except OSError:
                pass
            if watcher is not None:
                try:
                    watcher.kill()
                    watcher.wait(timeout=HARD_KILL_SECONDS)
                except (OSError, subprocess.SubprocessError):
                    pass
            raise ProcessContainmentError(
                1, "POSIX parent-lifeline process did not become ready"
            )
        return cls(pgid, watcher, control_write)

    def _finish(self, command: bytes) -> bool:
        if self._closed:
            return self._process.poll() is not None
        self._closed = True
        try:
            os.write(self._control_fd, command)
        except OSError:
            pass
        finally:
            try:
                os.close(self._control_fd)
            except OSError:
                pass
        try:
            self._process.wait(timeout=HARD_KILL_SECONDS)
        except (OSError, subprocess.SubprocessError):
            try:
                self._process.kill()
                self._process.wait(timeout=HARD_KILL_SECONDS)
            except (OSError, subprocess.SubprocessError):
                pass
        return self._process.poll() is not None

    def disarm(self) -> bool:
        return self._finish(b"D")

    def kill(self) -> bool:
        watcher_finished = self._finish(b"")
        if not watcher_finished:
            _signal_group(self.pgid, signal.SIGKILL)
        return watcher_finished

    def __del__(self) -> None:
        if getattr(self, "_closed", True):
            return
        try:
            os.close(self._control_fd)
        except (AttributeError, OSError):
            pass


def _start_posix(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None,
    capture_output: bool,
) -> tuple[subprocess.Popen[bytes], PosixGroupGuard]:
    process = subprocess.Popen(
        posix_parent_death_command(command),
        cwd=str(cwd),
        env=dict(environment) if environment is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        shell=False,
        start_new_session=True,
    )
    try:
        guard = PosixGroupGuard.start(int(process.pid))
    except BaseException:
        _signal_group(int(process.pid), signal.SIGKILL)
        try:
            process.wait(timeout=HARD_KILL_SECONDS)
        except (OSError, subprocess.SubprocessError):
            pass
        raise
    return process, guard


def _terminate_windows(
    process: subprocess.Popen[bytes], job: WindowsJob
) -> bool:
    requested = job.terminate(1)
    try:
        process.wait(timeout=HARD_KILL_SECONDS)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
            process.wait(timeout=HARD_KILL_SECONDS)
        except (OSError, subprocess.SubprocessError):
            pass
    deadline = time.monotonic() + HARD_KILL_SECONDS
    active = job.active_processes()
    while active not in (0, None) and time.monotonic() < deadline:
        time.sleep(0.02)
        active = job.active_processes()
    verified = bool(
        requested and process.poll() is not None and active == 0
    )
    job.close()
    return verified


def _terminate_posix(
    process: subprocess.Popen[bytes], guard: PosixGroupGuard, *, graceful: bool
) -> bool:
    pgid = int(process.pid)
    if graceful:
        _signal_group(pgid, signal.SIGINT)
        try:
            process.wait(timeout=GRACE_SECONDS)
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is not None and _wait_group_dead(pgid, 0.25):
            return guard.disarm()
    triggered = guard.kill()
    try:
        process.wait(timeout=HARD_KILL_SECONDS)
    except (OSError, subprocess.SubprocessError):
        pass
    if not _wait_group_dead(pgid, 0.25):
        _signal_group(pgid, signal.SIGKILL)
    return bool(
        triggered
        and process.poll() is not None
        and _wait_group_dead(pgid, HARD_KILL_SECONDS)
    )


def posix_group_terminated(pgid: int, *, timeout: float = HARD_KILL_SECONDS) -> bool:
    """Public verification helper for a group previously signalled by its guard."""
    return _wait_group_dead(int(pgid), timeout)


def run_supervised(
    command: Sequence[str],
    *,
    cwd: Path,
    timeout: float,
    environment: Mapping[str, str] | None = None,
    capture_output: bool = True,
    output_cap_bytes: int = DEFAULT_OUTPUT_CAP_BYTES,
    cancellation: Cancellation | None = None,
    allow_child_breakaway: bool = False,
) -> SupervisedResult:
    """Run a child with bounded output and verified process-tree containment.

    ``allow_child_breakaway`` is Windows-only and should be true solely for a
    command which intentionally creates its own authenticated retained process.
    It does not exempt the direct child from this supervisor's Job.
    """
    if not command:
        raise ValueError("supervised command is empty")
    if timeout <= 0:
        raise ValueError("supervised timeout must be positive")
    started_at = time.monotonic()
    process: subprocess.Popen[bytes] | None = None
    windows_job: WindowsJob | None = None
    posix_guard: PosixGroupGuard | None = None
    stdout: _BoundedCollector | None = None
    stderr: _BoundedCollector | None = None
    timed_out = False
    cancelled = False
    termination_verified = False
    try:
        try:
            if _is_windows():
                process, windows_job = _start_windows(
                    command,
                    cwd=cwd,
                    environment=environment,
                    capture_output=capture_output,
                    allow_breakaway=allow_child_breakaway,
                )
            else:
                process, posix_guard = _start_posix(
                    command,
                    cwd=cwd,
                    environment=environment,
                    capture_output=capture_output,
                )
        except OSError as exc:
            return SupervisedResult(
                None,
                "" if capture_output else None,
                "" if capture_output else None,
                time.monotonic() - started_at,
                termination_verified=True,
                launch_error=f"{type(exc).__name__}: {exc}",
            )
        if capture_output:
            stdout = _BoundedCollector(process.stdout, output_cap_bytes)
            stderr = _BoundedCollector(process.stderr, output_cap_bytes)
        deadline = started_at + timeout
        while process.poll() is None:
            if cancellation is not None and cancellation.is_set():
                cancelled = True
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            try:
                process.wait(timeout=min(0.1, remaining))
            except subprocess.TimeoutExpired:
                continue
        if _is_windows():
            assert windows_job is not None
            termination_verified = _terminate_windows(process, windows_job)
            windows_job = None
        else:
            assert posix_guard is not None
            termination_verified = _terminate_posix(
                process,
                posix_guard,
                graceful=timed_out or cancelled,
            )
            posix_guard = None
        return SupervisedResult(
            int(process.returncode) if process.returncode is not None else None,
            stdout.finish() if stdout is not None else None,
            stderr.finish() if stderr is not None else None,
            time.monotonic() - started_at,
            timed_out=timed_out,
            cancelled=cancelled,
            termination_verified=termination_verified,
        )
    except BaseException:
        if process is not None:
            try:
                if windows_job is not None:
                    _terminate_windows(process, windows_job)
                    windows_job = None
                elif posix_guard is not None:
                    _terminate_posix(process, posix_guard, graceful=True)
                    posix_guard = None
            except (OSError, subprocess.SubprocessError):
                pass
        if stdout is not None:
            stdout.finish(timeout=1.0)
        if stderr is not None:
            stderr.finish(timeout=1.0)
        raise
    finally:
        if windows_job is not None:
            windows_job.close()
        if posix_guard is not None:
            posix_guard.kill()


def pid_liveness(pid: int) -> str:
    """Return alive/dead/unknown without turning an OS query failure into death."""
    if pid <= 0:
        return PID_DEAD
    if pid == os.getpid():
        return PID_ALIVE
    if _is_windows():
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel32.WaitForSingleObject.restype = wintypes.DWORD
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            handle = _handle_value(
                kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            )
            if handle == 0:
                return (
                    PID_UNKNOWN
                    if int(ctypes.get_last_error() or 0) == ERROR_ACCESS_DENIED
                    else PID_DEAD
                )
            try:
                observed = int(kernel32.WaitForSingleObject(handle, 0))
            finally:
                kernel32.CloseHandle(handle)
            if observed == WAIT_TIMEOUT:
                return PID_ALIVE
            if observed == WAIT_OBJECT_0:
                return PID_DEAD
            return PID_UNKNOWN
        except (AttributeError, OSError, TypeError, ValueError):
            return PID_UNKNOWN
    try:
        os.kill(pid, 0)
        return PID_ALIVE
    except ProcessLookupError:
        return PID_DEAD
    except PermissionError:
        return PID_UNKNOWN
    except (OSError, ValueError):
        return PID_UNKNOWN


def _main(argv: Sequence[str]) -> int:
    if len(argv) == 4 and argv[0] == "--watch-posix-group":
        return _watch_posix_group(int(argv[1]), int(argv[2]), int(argv[3]))
    if len(argv) >= 3 and argv[0] == "--exec-with-parent-death":
        try:
            expected_parent = int(argv[1])
        except ValueError:
            return 127
        _linux_parent_death_signal(expected_parent)
        try:
            os.execvpe(argv[2], list(argv[2:]), os.environ)
        except OSError:
            return 127
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
