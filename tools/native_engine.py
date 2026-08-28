#!/usr/bin/env python3
"""One crash-safe process boundary for every kit-owned Godot launch.

Callers must pass the already selected engine executable.  This module never
searches PATH, reads GODOT_BIN, installs anything, or decides which Godot
version is acceptable.  It owns only process safety:

* one repository-private cross-process launch lock;
* Windows crash-dialog and console-window suppression;
* a KILL_ON_JOB_CLOSE Job Object for every bounded Windows run, with a POSIX
  process group elsewhere;
* structured native-crash, timeout, start-failure and concurrency results; and
* a private unresolved-warning record which only an explicit full or strict
  verification integration may clear.

The gate, setup import, local documentation builder and advisory language
server all use this same boundary. The intentionally retained language server
uses an exact PID, token, creation marker and executable identity instead of a
short-launcher Job handle; every bounded operation uses the Job. Ordinary
non-zero Godot exit codes are not classified as native crashes because Godot
also uses them for successful operations; callers must additionally inspect
output and operation-specific proof.
"""
from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
import uuid
import ctypes
import hashlib
import threading
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

if __package__:
    from . import project_context, process_supervisor
else:
    import project_context
    import process_supervisor

LOCK_NAME = "godot-process.lock"
WARNING_NAME = "native-warning.json"
RETRY_AUTHORIZATION_NAME = "native-retry.json"
OUTPUT_CAP_BYTES = 2 * 1024 * 1024
REFUSED_EXIT = 125
TIMEOUT_EXIT = 124
START_FAILED_EXIT = 127
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_SUSPENDED = 0x00000004
ERROR_ACCESS_DENIED = 5
ERROR_NOT_FOUND = 1168
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102
WAIT_OBJECT_0 = 0x00000000
WAIT_FAILED = 0xFFFFFFFF
PROCESS_TERMINATE = 0x0001
GUARD_TIMEOUT_SECONDS = 10.0
OUTPUT_READ_BYTES = 64 * 1024
OUTPUT_HEADER_RESERVE_BYTES = 512

BACKGROUND_OWNED_LIVE = "owned-live"
BACKGROUND_OWNER_VANISHED = "owner-vanished"
BACKGROUND_OWNERSHIP_MISMATCH = "ownership-mismatch"
WARNING_CLEAN = "clean"
WARNING_UNRESOLVED = "unresolved"
WARNING_UNKNOWN = "unknown"
LOCK_CLEAN = "clean"
LOCK_ACTIVE = "active"
LOCK_ABANDONED = "abandoned"
LOCK_UNREADABLE = "unreadable"
LOCK_UNKNOWN = "unknown"
PID_ALIVE = process_supervisor.PID_ALIVE
PID_DEAD = process_supervisor.PID_DEAD
PID_UNKNOWN = process_supervisor.PID_UNKNOWN

_PERSISTED_FAILURES = {
    "native-crash",
    "engine-timeout",
    "engine-start-failed",
    "concurrent-engine-launch",
}
_FAILURE_PRIORITY = {
    "concurrent-engine-launch": 1,
    "engine-start-failed": 2,
    "engine-timeout": 3,
    "native-crash": 4,
}

_SAFE_NATIVE_ENVIRONMENT = frozenset(
    {
        "APPDATA",
        "COMSPEC",
        "DISPLAY",
        "HOME",
        "LANG",
        "LANGUAGE",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "PROGRAMDATA",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WAYLAND_DISPLAY",
        "WINDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)


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


class _FileTime(ctypes.Structure):
    _fields_ = [
        ("dwLowDateTime", wintypes.DWORD),
        ("dwHighDateTime", wintypes.DWORD),
    ]


class NativeContainmentError(OSError):
    """A bounded Windows process could not be safely assigned to its job."""


@dataclass(frozen=True)
class ProcessIdentity:
    """Host-private identity that cannot silently follow a reused PID."""

    pid: int
    platform: str
    creation_marker: str
    executable_sha256: str
    executable_name: str

    def as_json(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "pid": self.pid,
            "platform": self.platform,
            "creation_marker": self.creation_marker,
            "executable_sha256": self.executable_sha256,
            "executable_name": self.executable_name,
        }

    @classmethod
    def from_json(cls, value: object) -> ProcessIdentity | None:
        if not isinstance(value, dict) or value.get("schema") != 1:
            return None
        try:
            identity = cls(
                pid=int(value.get("pid", 0)),
                platform=str(value.get("platform") or ""),
                creation_marker=str(value.get("creation_marker") or ""),
                executable_sha256=str(value.get("executable_sha256") or ""),
                executable_name=str(value.get("executable_name") or ""),
            )
        except (TypeError, ValueError):
            return None
        if (
            identity.pid <= 0
            or not identity.platform
            or not identity.creation_marker
            or len(identity.executable_sha256) != 64
            or not identity.executable_name
        ):
            return None
        try:
            int(identity.executable_sha256, 16)
        except ValueError:
            return None
        return identity


@dataclass(frozen=True)
class BackgroundReceipt:
    """Exact retained-process ownership persisted by the lifecycle helper."""

    pid: int
    lock_token: str
    process_identity: ProcessIdentity


@dataclass(frozen=True)
class BackgroundJournal:
    """Recoverable retained-process handoff state."""

    receipt: BackgroundReceipt
    lifecycle: str
    operation_id: str


@dataclass(frozen=True)
class BackgroundOwnerInspection:
    """Non-mutating retained-owner classification for status and stop paths."""

    state: str
    detail: str


@dataclass(frozen=True)
class NativeWarningSnapshot:
    """Tri-state warning snapshot; unknown is never equivalent to clean."""

    state: str
    identity: str | None


@dataclass(frozen=True)
class EngineLockInspection:
    """Read-only classification of the repository engine launch lock."""

    state: str
    detail: str
    pid: int | None = None


def _handle_value(value: Any) -> int:
    raw = getattr(value, "value", value)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


class WindowsJob:
    """Owned KILL_ON_JOB_CLOSE handle for one bounded Windows process tree."""

    def __init__(self, handle: int, kernel32: Any) -> None:
        self._handle = int(handle)
        self._kernel32 = kernel32

    @property
    def handle(self) -> int:
        return self._handle

    @property
    def closed(self) -> bool:
        return self._handle == 0

    def assign(self, process: subprocess.Popen[bytes]) -> tuple[bool, int]:
        process_handle = _handle_value(getattr(process, "_handle", None))
        if self.closed or process_handle == 0:
            return False, 6  # ERROR_INVALID_HANDLE
        if self._kernel32.AssignProcessToJobObject(self._handle, process_handle):
            return True, 0
        return False, int(ctypes.get_last_error() or 1)

    def resume(self, process: subprocess.Popen[bytes]) -> tuple[bool, int]:
        """Resume the sole primary thread after safe job assignment.

        ``subprocess.Popen`` closes the primary thread handle returned by
        ``CreateProcess``. The documented Tool Help APIs recover that thread;
        because the process was created suspended, it cannot have created any
        other thread or descendant before this point.
        """
        invalid_handle = _handle_value(ctypes.c_void_p(-1))
        snapshot = _handle_value(
            self._kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        )
        if snapshot == 0 or snapshot == invalid_handle:
            return False, int(ctypes.get_last_error() or 1)
        try:
            entry = _ThreadEntry32()
            entry.dwSize = ctypes.sizeof(entry)
            has_entry = bool(self._kernel32.Thread32First(snapshot, ctypes.byref(entry)))
            while has_entry:
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
                        previous_count = int(self._kernel32.ResumeThread(thread))
                        if previous_count == 0xFFFFFFFF:
                            return False, int(ctypes.get_last_error() or 1)
                        return True, 0
                    finally:
                        self._kernel32.CloseHandle(thread)
                has_entry = bool(
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
        # Dropping an owned bounded run is a failure boundary, not permission
        # to orphan its descendants. OS process teardown provides the same
        # last-handle guarantee if Python cannot run this finalizer.
        try:
            self.close()
        except (AttributeError, OSError):
            pass


def _create_windows_job() -> WindowsJob:
    """Create a fail-closed job whose last handle owns every descendant."""
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

    raw_handle = kernel32.CreateJobObjectW(None, None)
    handle = _handle_value(raw_handle)
    if handle == 0:
        raise NativeContainmentError(
            int(ctypes.get_last_error() or 1), "CreateJobObjectW failed"
        )
    job = WindowsJob(handle, kernel32)
    limits = _JobObjectExtendedLimitInformation()
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        handle,
        JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    ):
        error = int(ctypes.get_last_error() or 1)
        job.close()
        raise NativeContainmentError(
            error, "SetInformationJobObject(KILL_ON_JOB_CLOSE) failed"
        )
    return job


@dataclass(frozen=True)
class NativeResult:
    """Sanitised outcome of one bounded native-engine process."""

    exit_code: int
    output: str
    executable: str
    started: bool
    failure_class: str | None = None
    failure_code: str | None = None
    timeout_seconds: int | None = None
    windows_status: str | None = None

    @property
    def native_failure(self) -> bool:
        return self.failure_class in _PERSISTED_FAILURES

    def diagnostic(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "code": self.failure_code,
            "executable": self.executable,
            "exit_code": self.exit_code,
            "failure_class": self.failure_class,
            "started": self.started,
        }
        if self.timeout_seconds is not None:
            value["timeout_seconds"] = self.timeout_seconds
        if self.windows_status is not None:
            value["windows_status"] = self.windows_status
        return value


@dataclass(frozen=True)
class NativeStart:
    """A started process plus the lock token that proves kit ownership."""

    process: subprocess.Popen[bytes] | None
    lock_token: str | None
    failure: NativeResult | None
    executable: str
    windows_job: WindowsJob | None = None
    process_identity: ProcessIdentity | None = None
    posix_guard: process_supervisor.PosixGroupGuard | None = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _context(root: Path) -> project_context.ProjectContext:
    return project_context.load_configured_context(root)


def engine_lock_path(root: Path) -> Path:
    return _context(root).runtime_root / LOCK_NAME


def native_warning_path(root: Path) -> Path:
    return _context(root).runtime_root / WARNING_NAME


def native_retry_authorization_path(root: Path) -> Path:
    return _context(root).runtime_root / RETRY_AUTHORIZATION_NAME


def _atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _exclusive_path_guard(path: Path) -> Iterator[None]:
    """Serialize a path's read/replace sequence across processes.

    A persistent sidecar is intentional. Deleting a lock file while another
    process still holds its handle creates a second inode and defeats mutual
    exclusion. Every participant opens the same byte and the operating system
    releases ownership automatically if the Python process disappears.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    guard_path = path.with_name(f".{path.name}.guard")
    handle = guard_path.open("a+b", buffering=0)
    locked = False
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + GUARD_TIMEOUT_SECONDS
        while not locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out acquiring {path.name} guard")
                time.sleep(0.01)
        yield
    finally:
        if locked:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (OSError, ValueError):
                pass
        handle.close()


def _executable_digest(value: str, *, windows: bool) -> str:
    normalised = os.path.realpath(value)
    if windows:
        normalised = normalised.casefold().replace("/", "\\")
    return hashlib.sha256(
        normalised.encode("utf-8", errors="surrogatepass")
    ).hexdigest()


def _capture_windows_process_identity(pid: int) -> ProcessIdentity | None:
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = _handle_value(
            kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        )
        if handle == 0:
            return None
        try:
            return _windows_identity_from_handle(kernel32, handle, pid)
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _windows_identity_from_handle(
    kernel32: Any,
    handle: int,
    pid: int,
) -> ProcessIdentity | None:
    created = _FileTime()
    exited = _FileTime()
    kernel = _FileTime()
    user = _FileTime()
    if not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(created),
        ctypes.byref(exited),
        ctypes.byref(kernel),
        ctypes.byref(user),
    ):
        return None
    capacity = wintypes.DWORD(32768)
    buffer = ctypes.create_unicode_buffer(int(capacity.value))
    if not kernel32.QueryFullProcessImageNameW(
        handle, 0, buffer, ctypes.byref(capacity)
    ):
        return None
    executable = buffer.value
    marker = (int(created.dwHighDateTime) << 32) | int(created.dwLowDateTime)
    return ProcessIdentity(
        pid=pid,
        platform="windows",
        creation_marker=str(marker),
        executable_sha256=_executable_digest(executable, windows=True),
        executable_name=Path(executable).name,
    )


def _capture_proc_process_identity(pid: int) -> ProcessIdentity | None:
    proc = Path("/proc") / str(pid)
    try:
        stat_value = (proc / "stat").read_text(encoding="utf-8")
        close = stat_value.rfind(")")
        fields = stat_value[close + 2:].split()
        start_ticks = fields[19]
        executable = os.readlink(proc / "exe")
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text(
                encoding="ascii"
            ).strip()
        except (OSError, UnicodeError):
            boot_id = "boot-unknown"
    except (IndexError, OSError, UnicodeError, ValueError):
        return None
    return ProcessIdentity(
        pid=pid,
        platform="procfs",
        creation_marker=f"{boot_id}:{start_ticks}",
        executable_sha256=_executable_digest(executable, windows=False),
        executable_name=Path(executable).name,
    )


def _capture_ps_process_identity(pid: int) -> ProcessIdentity | None:
    try:
        created = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
            shell=False,
        )
        executable = subprocess.run(
            ["ps", "-p", str(pid), "-o", "comm="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    created_text = created.stdout.decode("utf-8", errors="replace").strip()
    executable_text = executable.stdout.decode("utf-8", errors="replace").strip()
    if created.returncode != 0 or executable.returncode != 0:
        return None
    if not created_text or not executable_text:
        return None
    return ProcessIdentity(
        pid=pid,
        platform="posix-ps",
        creation_marker=created_text,
        executable_sha256=_executable_digest(executable_text, windows=False),
        executable_name=Path(executable_text).name,
    )


def capture_process_identity(pid: int) -> ProcessIdentity | None:
    """Capture creation and executable identity for one currently live PID."""
    if pid <= 0:
        return None
    if os.name == "nt":
        return _capture_windows_process_identity(pid)
    identity = _capture_proc_process_identity(pid)
    return identity if identity is not None else _capture_ps_process_identity(pid)


def _capture_expected_process_identity(
    pid: int,
    executable: str | Path,
    *,
    timeout: float = 3.0,
) -> ProcessIdentity | None:
    """Wait through the Linux parent-death wrapper until the engine is exec'd."""
    if os.name == "nt" or not sys.platform.startswith("linux"):
        return capture_process_identity(pid)
    expected_digest = _executable_digest(str(executable), windows=os.name == "nt")
    expected_name = Path(str(executable)).name
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        observed = capture_process_identity(pid)
        if (
            observed is not None
            and observed.executable_sha256 == expected_digest
            and (
                os.name != "nt"
                or observed.executable_name.casefold() == expected_name.casefold()
            )
        ):
            return observed
        if _pid_liveness(pid) == PID_DEAD or time.monotonic() >= deadline:
            return None
        time.sleep(0.01)


def _same_process_identity(
    first: ProcessIdentity | None,
    second: ProcessIdentity | None,
) -> bool:
    return first is not None and second is not None and first == second


def write_background_receipt(
    path: Path,
    *,
    pid: int,
    token: str,
    process_identity: ProcessIdentity,
) -> None:
    """Atomically retain exact ownership needed by a later explicit stop."""
    if process_identity.pid != int(pid):
        raise ValueError("background receipt identity does not match its PID")
    _atomic_json_write(
        path,
        {
            "schema": 2,
            "pid": int(pid),
            "lock_token": str(token),
            "process_identity": process_identity.as_json(),
        },
    )


def read_background_receipt(path: Path) -> BackgroundReceipt | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("schema") not in {2, 3}:
            return None
        pid = int(state.get("pid", 0))
        token = str(state.get("lock_token") or "")
        identity = ProcessIdentity.from_json(state.get("process_identity"))
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    if pid <= 0 or not token or identity is None or identity.pid != pid:
        return None
    return BackgroundReceipt(pid, token, identity)


def write_background_journal(
    path: Path,
    *,
    receipt: BackgroundReceipt,
    operation_id: str,
    lifecycle: str = "starting",
) -> None:
    if lifecycle not in {"starting", "ready"}:
        raise ValueError("background lifecycle must be starting or ready")
    operation = str(operation_id).strip()
    if not operation or len(operation) > 128:
        raise ValueError("background operation identity is invalid")
    with _exclusive_path_guard(path):
        if path.exists():
            raise FileExistsError("background journal already exists")
        _atomic_json_write(
            path,
            {
                "schema": 3,
                "pid": receipt.pid,
                "lock_token": receipt.lock_token,
                "process_identity": receipt.process_identity.as_json(),
                "lifecycle": lifecycle,
                "operation_id": operation,
                "recorded_at": _now_iso(),
            },
        )


def read_background_journal(path: Path) -> BackgroundJournal | None:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("schema") != 3:
            return None
        lifecycle = str(state.get("lifecycle") or "")
        operation_id = str(state.get("operation_id") or "")
    except (OSError, UnicodeError, ValueError, TypeError):
        return None
    receipt = read_background_receipt(path)
    if (
        receipt is None
        or lifecycle not in {"starting", "ready"}
        or not operation_id
        or len(operation_id) > 128
    ):
        return None
    return BackgroundJournal(receipt, lifecycle, operation_id)


def mark_background_ready(
    path: Path,
    *,
    expected: BackgroundJournal,
) -> bool:
    """CAS the exact starting handoff to ready without following replacement."""
    try:
        with _exclusive_path_guard(path):
            current = read_background_journal(path)
            if current != expected or current.lifecycle != "starting":
                return False
            try:
                raw_state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                return False
            if not isinstance(raw_state, dict):
                return False
            ready_at = _now_iso()
            _atomic_json_write(
                path,
                {
                    "schema": 3,
                    "pid": current.receipt.pid,
                    "lock_token": current.receipt.lock_token,
                    "process_identity": current.receipt.process_identity.as_json(),
                    "lifecycle": "ready",
                    "operation_id": current.operation_id,
                    "recorded_at": str(raw_state.get("recorded_at") or ready_at),
                    "ready_at": ready_at,
                },
            )
            return True
    except (OSError, TimeoutError):
        return False


def remove_background_journal(
    path: Path,
    *,
    expected_receipt: BackgroundReceipt,
) -> bool:
    """Remove only the lifecycle state which still names the exact owner."""
    try:
        with _exclusive_path_guard(path):
            current = read_background_receipt(path)
            if current != expected_receipt:
                return False
            path.unlink()
            return True
    except (OSError, TimeoutError):
        return False


def _pid_liveness(pid: int) -> str:
    return process_supervisor.pid_liveness(pid)


def _pid_is_alive(pid: int) -> bool:
    """Compatibility predicate; unknown is conservatively treated as live."""
    return _pid_liveness(pid) != PID_DEAD


def _inspect_lock_path(path: Path) -> EngineLockInspection:
    if not path.exists():
        return EngineLockInspection(LOCK_CLEAN, "no engine process lock exists")
    holder = _read_lock_path(path)
    if holder is None:
        return EngineLockInspection(
            LOCK_UNREADABLE,
            "the engine process lock is unreadable",
        )
    try:
        pid = int(holder.get("pid", 0))
    except (TypeError, ValueError):
        return EngineLockInspection(
            LOCK_UNREADABLE,
            "the engine process lock has no valid owner PID",
        )
    expected = ProcessIdentity.from_json(holder.get("process_identity"))
    if pid <= 0 or expected is None or expected.pid != pid:
        return EngineLockInspection(
            LOCK_UNREADABLE,
            "the engine process lock has no authentic owner identity",
            pid if pid > 0 else None,
        )
    liveness = _pid_liveness(pid)
    if liveness == PID_DEAD:
        return EngineLockInspection(
            LOCK_ABANDONED,
            "the exact engine lock owner is no longer running",
            pid,
        )
    if liveness == PID_UNKNOWN:
        return EngineLockInspection(
            LOCK_UNKNOWN,
            "the engine lock owner liveness could not be established",
            pid,
        )
    observed = capture_process_identity(pid)
    if observed is None:
        return EngineLockInspection(
            LOCK_UNKNOWN,
            "the live engine lock owner identity could not be authenticated",
            pid,
        )
    if not _same_process_identity(expected, observed):
        return EngineLockInspection(
            LOCK_ABANDONED,
            "the engine lock PID now belongs to a different process",
            pid,
        )
    return EngineLockInspection(
        LOCK_ACTIVE,
        "the exact engine lock owner is active",
        pid,
    )


def inspect_engine_lock(root: Path) -> EngineLockInspection:
    """Classify the process lock without creating, deleting or repairing it."""
    try:
        return _inspect_lock_path(engine_lock_path(root))
    except (OSError, project_context.ProjectContextError):
        return EngineLockInspection(
            LOCK_UNKNOWN,
            "the engine process lock could not be inspected",
        )


def acquire_engine_lock(
    root: Path,
    *,
    operation: str = "native-engine-launch",
    verification_run_id: str | None = None,
) -> tuple[str | None, str]:
    """Acquire the engine lock, failing closed on abandoned ownership.

    An abandoned authentic lock is first made durable as an unresolved native
    warning and only then removed. The triggering command is refused, so stale
    recovery can never become an invisible automatic engine retry.
    """
    path = engine_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    token = f"{os.getpid()}-{time.time_ns()}-{uuid.uuid4().hex}"
    identity = capture_process_identity(os.getpid())
    if identity is None:
        return None, "cannot authenticate the current process for the Godot lock"
    try:
        with _exclusive_path_guard(path):
            if path.exists():
                inspection = _inspect_lock_path(path)
                if inspection.state == LOCK_ACTIVE:
                    return None, (
                        "another Godot launch is active "
                        f"(pid {inspection.pid or 'unknown'})"
                    )
                if inspection.state in {LOCK_UNREADABLE, LOCK_UNKNOWN}:
                    return None, inspection.detail
                if inspection.state == LOCK_ABANDONED:
                    holder = _read_lock_path(path) or {}
                    identity = ProcessIdentity.from_json(
                        holder.get("process_identity")
                    )
                    failure = NativeResult(
                        exit_code=1,
                        output="an authenticated engine process lock was abandoned",
                        executable=(
                            identity.executable_name
                            if identity is not None
                            else "Godot"
                        ),
                        started=True,
                        failure_class="native-crash",
                        failure_code="native-engine-lock-abandoned",
                    )
                    persisted = persist_native_failure(
                        root,
                        failure,
                        operation=f"{operation}-abandoned-lock-recovery",
                        verification_run_id=(
                            verification_run_id
                            or os.environ.get("KIT_VERIFY_NONCE", "").strip()
                            or None
                        ),
                    )
                    if persisted is None:
                        return None, (
                            "abandoned Godot ownership could not be recorded; "
                            "the lock was preserved"
                        )
                    path.unlink()
                    return None, (
                        "an abandoned Godot lock was recorded and recovered; "
                        "the triggering launch was refused"
                    )
            descriptor = os.open(
                str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600
            )
            try:
                with os.fdopen(
                    descriptor, "w", encoding="utf-8", newline="\n"
                ) as handle:
                    json.dump(
                        {
                            "schema": 2,
                            "pid": os.getpid(),
                            "token": token,
                            "process_identity": identity.as_json(),
                        },
                        handle,
                        sort_keys=True,
                    )
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                # This process created the path while holding the sidecar
                # guard, so no other owner can have replaced it yet.
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            return token, ""
    except (OSError, TimeoutError) as exc:
        return None, f"cannot acquire the Godot process lock: {exc}"


def _read_lock_path(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _lock_holder_is_live(holder: Mapping[str, Any]) -> bool:
    try:
        pid = int(holder.get("pid", 0))
    except (TypeError, ValueError):
        return True
    if not _pid_is_alive(pid):
        return False
    expected = ProcessIdentity.from_json(holder.get("process_identity"))
    if expected is None:
        # A live legacy/unreadable holder is not safe to recover automatically.
        return True
    observed = capture_process_identity(pid)
    if observed is None:
        # Access denied or a transient query failure must not steal a live lock.
        return True
    return _same_process_identity(expected, observed)


def _lock_holder(root: Path) -> dict[str, Any] | None:
    return _read_lock_path(engine_lock_path(root))


def _transfer_engine_lock(
    root: Path,
    token: str,
    pid: int,
    process_identity: ProcessIdentity | None = None,
) -> bool:
    """Make a long-lived native child, rather than its launcher, the holder."""
    path = engine_lock_path(root)
    identity = process_identity or capture_process_identity(pid)
    if identity is None or identity.pid != int(pid):
        return False
    try:
        with _exclusive_path_guard(path):
            holder = _read_lock_path(path)
            if holder is None or holder.get("token") != token:
                return False
            _atomic_json_write(
                path,
                {
                    "schema": 2,
                    "pid": int(pid),
                    "token": token,
                    "process_identity": identity.as_json(),
                },
            )
            return True
    except (OSError, TimeoutError):
        return False


def release_engine_lock(root: Path, token: str) -> None:
    path = engine_lock_path(root)
    try:
        with _exclusive_path_guard(path):
            holder = _read_lock_path(path)
            if holder is None or holder.get("token") != token:
                return
            path.unlink()
    except (OSError, TimeoutError):
        pass


def _cap_output(raw: bytes | str) -> str:
    value = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= OUTPUT_CAP_BYTES:
        return value
    tail = encoded[-OUTPUT_CAP_BYTES:].decode("utf-8", errors="replace")
    dropped = len(encoded) - OUTPUT_CAP_BYTES
    return (
        f"[native output truncated: {dropped} bytes omitted; "
        f"showing the final {OUTPUT_CAP_BYTES} bytes]\n{tail}"
    )


class _BoundedOutputCollector:
    """Drain a process pipe continuously while retaining only its bounded tail."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._buffer = bytearray()
        self._dropped = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._read,
            name="kit-native-output",
            daemon=True,
        )
        self._thread.start()

    @property
    def retained_bytes(self) -> int:
        with self._lock:
            return len(self._buffer)

    def _append(self, chunk: bytes) -> None:
        limit = max(1, OUTPUT_CAP_BYTES - OUTPUT_HEADER_RESERVE_BYTES)
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
                value = chunk if isinstance(chunk, bytes) else bytes(chunk)
                with self._lock:
                    self._append(value)
        except (OSError, TypeError, ValueError):
            return

    def finish(self, *, timeout: float = 10.0) -> bytes:
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
            tail = bytes(self._buffer)
            dropped = self._dropped
        if dropped <= 0:
            return tail
        header = (
            f"[native output truncated during capture: {dropped} bytes omitted; "
            "showing bounded tail]\n"
        ).encode("utf-8")
        return header + tail


def classify_crash_exit(code: int, *, system_name: str | None = None) -> tuple[bool, str | None]:
    """Classify an access violation/NTSTATUS or POSIX signal exit."""
    current = system_name or ("Windows" if os.name == "nt" else "POSIX")
    if current == "Windows":
        unsigned = code & 0xFFFFFFFF
        crashed = 0xC0000000 <= unsigned <= 0xCFFFFFFF
        return crashed, f"0x{unsigned:08X}" if crashed else None
    return code < 0, None


def native_child_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the documented minimal environment passed to the native engine.

    Verification nonces, provider credentials and other secret-like host
    variables are intentionally absent.  Only OS execution, user data/temp,
    display and locale variables needed by an official Godot binary survive.
    """
    environment = os.environ if source is None else source
    safe: dict[str, str] = {}
    for raw_key, raw_value in environment.items():
        key = str(raw_key)
        upper = key.upper()
        if upper in _SAFE_NATIVE_ENVIRONMENT or upper.startswith("LC_"):
            safe[key] = str(raw_value)
    return safe


def _start_process(
    command: Sequence[str],
    *,
    cwd: Path,
    capture_output: bool,
    extra_creation_flags: int = 0,
    parent_lifeline: bool = True,
) -> subprocess.Popen[bytes]:
    """Create one isolated child while suppressing Windows native error UI."""
    creation_flags = extra_creation_flags
    start_new_session = os.name != "nt"
    kernel32 = None
    previous_error_mode = None
    if os.name == "nt":
        creation_flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            import ctypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.GetErrorMode.restype = ctypes.c_uint
            kernel32.SetErrorMode.argtypes = [ctypes.c_uint]
            kernel32.SetErrorMode.restype = ctypes.c_uint
            current = kernel32.GetErrorMode()
            previous_error_mode = kernel32.SetErrorMode(current | 0x0001 | 0x0002)
        except (AttributeError, OSError):
            kernel32 = None
            previous_error_mode = None
    child_command = [str(value) for value in command]
    if os.name != "nt" and parent_lifeline:
        child_command = process_supervisor.posix_parent_death_command(child_command)
    try:
        return subprocess.Popen(
            child_command,
            cwd=str(cwd),
            env=native_child_environment(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
            stderr=subprocess.STDOUT if capture_output else subprocess.DEVNULL,
            shell=False,
            creationflags=creation_flags,
            start_new_session=start_new_session,
        )
    finally:
        if kernel32 is not None and previous_error_mode is not None:
            kernel32.SetErrorMode(previous_error_mode)


def _terminate_unassigned_windows_process(process: subprocess.Popen[bytes]) -> None:
    """Contain and prove death before considering a breakaway retry."""
    _terminate_pid_tree(int(process.pid))
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
    if process.poll() is None:
        raise NativeContainmentError(
            ERROR_ACCESS_DENIED,
            "unassigned suspended process termination could not be verified",
        )


def _start_bounded_windows_process(
    command: Sequence[str],
    *,
    cwd: Path,
    capture_output: bool,
) -> tuple[subprocess.Popen[bytes], WindowsJob]:
    """Start and assign one bounded tree, retrying only for parent-job conflict.

    Modern Windows supports nested jobs, so the first assignment normally
    succeeds even when Codex itself is job-contained.  ERROR_ACCESS_DENIED can
    mean the inherited parent/PCA job is incompatible.  In that one case the
    unassigned process tree is terminated first, then creation is retried with
    CREATE_BREAKAWAY_FROM_JOB and immediately assigned to this stricter job.
    Both attempts create the process suspended, eliminating the pre-assignment
    child-spawn race. Any other assignment failure, a failed breakaway retry,
    or a failure to resume after assignment refuses and terminates the run.
    """
    job = _create_windows_job()
    process: subprocess.Popen[bytes] | None = None
    assigned_to_job = False
    try:
        process = _start_process(
            command,
            cwd=cwd,
            capture_output=capture_output,
            extra_creation_flags=CREATE_SUSPENDED,
        )
        assigned, error = job.assign(process)
        if assigned:
            assigned_to_job = True
            resumed, error = job.resume(process)
            if not resumed:
                raise NativeContainmentError(
                    error, "ResumeThread failed after job assignment"
                )
            return process, job
        _terminate_unassigned_windows_process(process)
        process = None
        if error != ERROR_ACCESS_DENIED:
            raise NativeContainmentError(
                error, "AssignProcessToJobObject failed"
            )

        process = _start_process(
            command,
            cwd=cwd,
            capture_output=capture_output,
            extra_creation_flags=CREATE_SUSPENDED | CREATE_BREAKAWAY_FROM_JOB,
        )
        assigned, error = job.assign(process)
        if assigned:
            assigned_to_job = True
            resumed, error = job.resume(process)
            if not resumed:
                raise NativeContainmentError(
                    error, "ResumeThread failed after breakaway job assignment"
                )
            return process, job
        _terminate_unassigned_windows_process(process)
        process = None
        raise NativeContainmentError(
            error, "AssignProcessToJobObject failed after breakaway retry"
        )
    except BaseException:
        if process is not None:
            try:
                if assigned_to_job:
                    terminate_owned_process_tree(process, windows_job=job)
                else:
                    _terminate_unassigned_windows_process(process)
            except (NativeContainmentError, OSError):
                pass
        job.close()
        raise


def _result(
    engine: str | Path,
    *,
    exit_code: int,
    output: str,
    started: bool,
    failure_class: str | None = None,
    failure_code: str | None = None,
    timeout_seconds: int | None = None,
    windows_status: str | None = None,
) -> NativeResult:
    return NativeResult(
        exit_code=exit_code,
        output=_cap_output(output),
        executable=Path(str(engine)).name,
        started=started,
        failure_class=failure_class,
        failure_code=failure_code,
        timeout_seconds=timeout_seconds,
        windows_status=windows_status,
    )


def start_godot(
    engine: str | Path,
    arguments: Sequence[str],
    *,
    root: Path,
    cwd: Path,
    capture_output: bool = True,
    operation: str = "native-engine-launch",
    verification_run_id: str | None = None,
    native_retry_token: str | None = None,
) -> NativeStart:
    """Start a selected Godot binary and retain its repository launch lock.

    Captured launches are bounded operations and use a Windows Job Object.
    ``capture_output=False`` is the deliberate retained-server exception used
    by gdls: its short CLI launcher must not close a KILL_ON_JOB_CLOSE handle
    and immediately terminate the server it was asked to retain.  That path is
    instead guarded by an exact PID, token, creation and executable identity
    receipt plus an abandoned-owner warning.
    """
    executable = Path(str(engine)).name
    if os.environ.get("KIT_ENGINE_DISABLED") == "1":
        failure = _result(
            engine,
            exit_code=REFUSED_EXIT,
            output="NATIVE ENGINE DISABLED BY CALLER\n",
            started=False,
            failure_class="engine-disabled",
            failure_code="native-engine-disabled",
        )
        return NativeStart(None, None, failure, executable)
    effective_run_id = (
        str(verification_run_id or "").strip()
        or os.environ.get("KIT_VERIFY_NONCE", "").strip()
        or None
    )
    effective_operation = (
        "verification"
        if operation == "native-engine-launch" and effective_run_id
        else operation
    )
    retry_allowed, retry_problem = _retry_authorized_for_launch(
        root,
        operation=effective_operation,
        verification_run_id=effective_run_id,
        retry_token=native_retry_token,
    )
    if not retry_allowed:
        failure = _result(
            engine,
            exit_code=REFUSED_EXIT,
            output=f"ENGINE LAUNCH REFUSED: {retry_problem}.\n",
            started=False,
            failure_class="native-retry-required",
            failure_code="native-retry-authorization-required",
        )
        return NativeStart(None, None, failure, executable)
    token, problem = acquire_engine_lock(
        root,
        operation=effective_operation,
        verification_run_id=effective_run_id,
    )
    if token is None:
        failure = _result(
            engine,
            exit_code=REFUSED_EXIT,
            output=(
                f"ENGINE LAUNCH REFUSED: {problem}. Let the existing launch finish; "
                "do not retry in parallel.\n"
            ),
            started=False,
            failure_class="concurrent-engine-launch",
            failure_code="concurrent-engine-launch",
        )
        return NativeStart(None, None, failure, executable)
    windows_job: WindowsJob | None = None
    posix_guard: process_supervisor.PosixGroupGuard | None = None
    process: subprocess.Popen[bytes] | None = None
    command = [str(engine), *[str(value) for value in arguments]]
    try:
        if os.name == "nt" and capture_output:
            process, windows_job = _start_bounded_windows_process(
                command,
                cwd=cwd,
                capture_output=capture_output,
            )
        else:
            process = _start_process(
                command,
                cwd=cwd,
                capture_output=capture_output,
                extra_creation_flags=(
                    CREATE_BREAKAWAY_FROM_JOB
                    if os.name == "nt" and not capture_output
                    else 0
                ),
                parent_lifeline=capture_output,
            )
            if os.name != "nt" and capture_output:
                posix_guard = process_supervisor.PosixGroupGuard.start(
                    int(process.pid)
                )
    except OSError as exc:
        if process is not None:
            try:
                _terminate_started_process(
                    process,
                    windows_job=windows_job,
                    posix_guard=posix_guard,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        release_engine_lock(root, token)
        failure = _result(
            engine,
            exit_code=START_FAILED_EXIT,
            output=f"NATIVE ENGINE START FAILED: {type(exc).__name__}\n",
            started=False,
            failure_class="engine-start-failed",
            failure_code="native-engine-start-failed",
        )
        return NativeStart(None, None, failure, executable)
    except BaseException:
        if process is not None:
            try:
                _terminate_started_process(
                    process,
                    windows_job=windows_job,
                    posix_guard=posix_guard,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        release_engine_lock(root, token)
        raise
    assert process is not None
    try:
        process_identity = _capture_expected_process_identity(
            int(process.pid), engine
        )
        transferred = process_identity is not None and _transfer_engine_lock(
            root,
            token,
            int(process.pid),
            process_identity,
        )
    except BaseException:
        _terminate_started_process(
            process,
            windows_job=windows_job,
            posix_guard=posix_guard,
        )
        release_engine_lock(root, token)
        raise
    if not transferred:
        _terminate_started_process(
            process,
            windows_job=windows_job,
            posix_guard=posix_guard,
        )
        release_engine_lock(root, token)
        failure = _result(
            engine,
            exit_code=START_FAILED_EXIT,
            output="NATIVE ENGINE START FAILED: launch ownership could not be recorded\n",
            started=True,
            failure_class="engine-start-failed",
            failure_code="native-engine-start-failed",
        )
        return NativeStart(None, None, failure, executable)
    return NativeStart(
        process,
        token,
        None,
        executable,
        windows_job,
        process_identity,
        posix_guard,
    )


def _terminate_pid_tree(pid: int) -> None:
    if pid <= 0:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill.exe", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
                shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ValueError):
            pass


def terminate_owned_process_tree(
    process: subprocess.Popen[bytes],
    *,
    windows_job: WindowsJob | None = None,
    posix_guard: process_supervisor.PosixGroupGuard | None = None,
) -> bool:
    """Terminate the owned tree and return true only when death is verified."""
    if windows_job is not None:
        # TerminateJobObject reaches assigned descendants even when the root
        # process has already exited. Closing the KILL_ON_JOB_CLOSE handle is
        # the final fail-safe and is deliberately idempotent.
        requested = windows_job.terminate(TIMEOUT_EXIT)
        observed_active = windows_job.active_processes()
        active = observed_active if isinstance(observed_active, int) else None
        deadline = time.monotonic() + 10.0
        while active not in (0, None) and time.monotonic() < deadline:
            time.sleep(0.02)
            observed_active = windows_job.active_processes()
            active = observed_active if isinstance(observed_active, int) else None
        termination_verified = bool(requested and active == 0)
        windows_job.close()
    elif posix_guard is not None:
        termination_verified = posix_guard.kill()
    elif os.name == "nt":
        _terminate_pid_tree(int(process.pid))
        termination_verified = process.poll() is not None
    else:
        # A POSIX session can retain descendants after its leader exits. PGID
        # remains the original PID, so always signal the owned group before
        # considering collection complete.
        _terminate_pid_tree(int(process.pid))
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                os.killpg(int(process.pid), 0)
            except ProcessLookupError:
                break
            except (OSError, ValueError):
                break
            time.sleep(0.02)
        try:
            os.killpg(int(process.pid), 0)
            termination_verified = False
        except ProcessLookupError:
            termination_verified = True
        except (OSError, ValueError):
            termination_verified = False
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass
    try:
        process.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
    if posix_guard is not None:
        termination_verified = bool(
            termination_verified
            and process_supervisor.posix_group_terminated(int(process.pid))
        )
    return bool(process.poll() is not None and termination_verified)


def _terminate_started_process(
    process: subprocess.Popen[bytes],
    *,
    windows_job: WindowsJob | None,
    posix_guard: process_supervisor.PosixGroupGuard | None,
) -> bool:
    if posix_guard is None:
        return terminate_owned_process_tree(process, windows_job=windows_job)
    return terminate_owned_process_tree(
        process,
        windows_job=windows_job,
        posix_guard=posix_guard,
    )


def inspect_background_owner(
    root: Path,
    receipt: BackgroundReceipt,
) -> BackgroundOwnerInspection:
    """Classify retained ownership without killing or recovering anything."""
    holder = _lock_holder(root)
    try:
        holder_pid = int(holder.get("pid", 0)) if holder is not None else 0
    except (TypeError, ValueError):
        holder_pid = 0
    if (
        holder is None
        or holder.get("token") != receipt.lock_token
        or holder_pid != receipt.pid
    ):
        return BackgroundOwnerInspection(
            BACKGROUND_OWNERSHIP_MISMATCH,
            "retained receipt does not match the repository process lock",
        )
    lock_identity = ProcessIdentity.from_json(holder.get("process_identity"))
    if not _same_process_identity(lock_identity, receipt.process_identity):
        return BackgroundOwnerInspection(
            BACKGROUND_OWNERSHIP_MISMATCH,
            "retained receipt identity does not match the process lock",
        )
    if not _pid_is_alive(receipt.pid):
        return BackgroundOwnerInspection(
            BACKGROUND_OWNER_VANISHED,
            "the exact retained process is no longer running",
        )
    observed = capture_process_identity(receipt.pid)
    if observed is None:
        return BackgroundOwnerInspection(
            BACKGROUND_OWNERSHIP_MISMATCH,
            "the live process identity could not be authenticated",
        )
    if not _same_process_identity(receipt.process_identity, observed):
        return BackgroundOwnerInspection(
            BACKGROUND_OWNERSHIP_MISMATCH,
            "the PID now belongs to a different process",
        )
    return BackgroundOwnerInspection(
        BACKGROUND_OWNED_LIVE,
        "the receipt, lock and live process identity match",
    )


def _terminate_exact_windows_process(receipt: BackgroundReceipt) -> bool:
    """Terminate a handle-authenticated process, never a subsequently reused PID."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
            ctypes.POINTER(_FileTime),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = _handle_value(
            kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | SYNCHRONIZE,
                False,
                receipt.pid,
            )
        )
        if handle == 0:
            return _pid_liveness(receipt.pid) == PID_DEAD
        try:
            observed = _windows_identity_from_handle(
                kernel32, handle, receipt.pid
            )
            if not _same_process_identity(receipt.process_identity, observed):
                return False
            if not kernel32.TerminateProcess(handle, TIMEOUT_EXIT):
                return False
            return int(
                kernel32.WaitForSingleObject(
                    handle, int(process_supervisor.HARD_KILL_SECONDS * 1000)
                )
            ) == WAIT_OBJECT_0
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def _terminate_exact_posix_process(receipt: BackgroundReceipt) -> bool:
    """Use a Linux pidfd when available; otherwise refuse the PID race."""
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False
    try:
        descriptor = os.pidfd_open(receipt.pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, ValueError):
        return False
    try:
        observed = capture_process_identity(receipt.pid)
        if not _same_process_identity(receipt.process_identity, observed):
            return False
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        readable, _, _ = select.select([descriptor], [], [], 3.0)
        if not readable:
            signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            readable, _, _ = select.select([descriptor], [], [], 10.0)
        return bool(readable)
    except (OSError, ValueError):
        return False
    finally:
        os.close(descriptor)


def terminate_exact_background_owner(receipt: BackgroundReceipt) -> bool:
    """Terminate only an identity-bound retained owner, never a bare PID."""
    if os.name == "nt":
        return _terminate_exact_windows_process(receipt)
    return _terminate_exact_posix_process(receipt)


def stop_owned_background(root: Path, receipt: BackgroundReceipt) -> bool:
    """Stop a retained native server only when exact live ownership matches."""
    inspection = inspect_background_owner(root, receipt)
    if inspection.state != BACKGROUND_OWNED_LIVE:
        return False
    if not terminate_exact_background_owner(receipt):
        return False
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        observed = capture_process_identity(receipt.pid)
        if observed is None and not _pid_is_alive(receipt.pid):
            break
        if observed is not None and not _same_process_identity(
            receipt.process_identity, observed
        ):
            break
        time.sleep(0.02)
    else:
        return False
    release_engine_lock(root, receipt.lock_token)
    holder = _lock_holder(root)
    return holder is None or holder.get("token") != receipt.lock_token


def reconcile_abandoned_background(
    root: Path,
    receipt: BackgroundReceipt,
    *,
    executable: str,
    operation: str = "retained-engine-unexpected-exit",
    verification_run_id: str | None = None,
) -> NativeResult | None:
    """Turn an unexpectedly vanished retained server into durable evidence.

    A successful background start deliberately outlives its Python launcher.
    If its exact lock still exists but that holder is no longer alive, the
    process ended without the owned stop path.  Treat that conservatively as a
    native failure before recovering the stale lock; this is how a crash which
    happens after startup remains visible on the next command.
    """
    inspection = inspect_background_owner(root, receipt)
    if inspection.state != BACKGROUND_OWNER_VANISHED:
        return None
    failure = NativeResult(
        exit_code=1,
        output="retained Godot process ended without the owned stop path",
        executable=Path(executable).name,
        started=True,
        failure_class="native-crash",
        failure_code="native-engine-disappeared",
    )
    if persist_native_failure(
        root,
        failure,
        operation=operation,
        verification_run_id=verification_run_id,
    ) is None:
        return None
    release_engine_lock(root, receipt.lock_token)
    holder = _lock_holder(root)
    if holder is not None and holder.get("token") == receipt.lock_token:
        return None
    return failure


def run_godot(
    engine: str | Path,
    arguments: Sequence[str],
    *,
    root: Path,
    cwd: Path,
    timeout: int,
    operation: str = "native-engine-launch",
    verification_run_id: str | None = None,
    native_retry_token: str | None = None,
) -> NativeResult:
    """Run one bounded command and release ownership only after containment."""
    started = start_godot(
        engine,
        arguments,
        root=root,
        cwd=cwd,
        capture_output=True,
        operation=operation,
        verification_run_id=verification_run_id,
        native_retry_token=native_retry_token,
    )
    if started.failure is not None:
        return started.failure
    process = started.process
    token = started.lock_token
    windows_job = started.windows_job
    posix_guard = started.posix_guard
    assert process is not None and token is not None
    collector: _BoundedOutputCollector | None = None
    tree_contained = False
    release_allowed = True
    try:
        try:
            collector = _BoundedOutputCollector(process.stdout)
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            termination_verified = _terminate_started_process(
                process,
                windows_job=windows_job,
                posix_guard=posix_guard,
            )
            tree_contained = True
            release_allowed = termination_verified
            raw = collector.finish() if collector is not None else b""
            output = _cap_output(raw or b"")
            if termination_verified:
                output += (
                    f"\nTIMEOUT after {timeout}s; owned process tree terminated\n"
                )
            else:
                output += (
                    f"\nTIMEOUT after {timeout}s; termination could not be "
                    "verified and the launch lock was retained\n"
                )
            return _result(
                engine,
                exit_code=TIMEOUT_EXIT,
                output=output,
                started=True,
                failure_class="engine-timeout",
                failure_code="native-engine-timeout",
                timeout_seconds=timeout,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            release_allowed = _terminate_started_process(
                process,
                windows_job=windows_job,
                posix_guard=posix_guard,
            )
            tree_contained = True
            if collector is not None:
                collector.finish()
            return _result(
                engine,
                exit_code=START_FAILED_EXIT,
                output=f"NATIVE ENGINE COMMUNICATION FAILED: {type(exc).__name__}\n",
                started=True,
                failure_class="engine-start-failed",
                failure_code="native-engine-start-failed",
            )
        code = int(process.returncode if process.returncode is not None else 1)
        # Even a successful POSIX group leader may leave children behind, and
        # a Windows Job handle owns descendants after its root has exited.
        release_allowed = _terminate_started_process(
            process,
            windows_job=windows_job,
            posix_guard=posix_guard,
        )
        tree_contained = True
        raw = collector.finish() if collector is not None else b""
        output = _cap_output(raw or b"")
        crashed, windows_status = classify_crash_exit(code)
        if crashed:
            status = f", {windows_status}" if windows_status else ""
            output += (
                "\nNATIVE ENGINE CRASH: process exited before the operation "
                f"completed (exit {code}{status}).\n"
            )
            return _result(
                engine,
                exit_code=code,
                output=output,
                started=True,
                failure_class="native-crash",
                failure_code="native-crash",
                windows_status=windows_status,
            )
        return _result(
            engine,
            exit_code=code,
            output=output,
            started=True,
        )
    except BaseException:
        if not tree_contained:
            release_allowed = False
            try:
                release_allowed = _terminate_started_process(
                    process,
                    windows_job=windows_job,
                    posix_guard=posix_guard,
                )
                tree_contained = True
            except (OSError, subprocess.SubprocessError):
                pass
        if collector is not None:
            collector.finish(timeout=1.0)
        raise
    finally:
        if not tree_contained:
            release_allowed = False
            try:
                release_allowed = _terminate_started_process(
                    process,
                    windows_job=windows_job,
                    posix_guard=posix_guard,
                )
                tree_contained = True
            except (OSError, subprocess.SubprocessError):
                pass
        if windows_job is not None and not tree_contained:
            # On a normal root exit, closing the job still removes any native
            # descendants the engine left behind. On launcher death Windows
            # closes this handle and enforces the same limit in the kernel.
            windows_job.close()
        if release_allowed:
            release_engine_lock(root, token)


def persist_native_failure(
    root: Path,
    result: NativeResult,
    *,
    operation: str,
    verification_run_id: str | None = None,
) -> dict[str, Any] | None:
    """Persist a privacy-safe unresolved warning; never clear one on success."""
    if result.failure_class not in _PERSISTED_FAILURES:
        return None
    path = native_warning_path(root)
    run_id = str(verification_run_id or "").strip()
    if len(run_id) > 128:
        raise ValueError("verification run identity is too long")
    with _exclusive_path_guard(path):
        previous: dict[str, Any] = {}
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
            if (
                isinstance(candidate, dict)
                and candidate.get("status") == "unresolved"
            ):
                previous = candidate
        except (OSError, UnicodeError, ValueError):
            previous = {}
        observed_at = _now_iso()
        current_priority = _FAILURE_PRIORITY.get(str(result.failure_class), 0)
        previous_priority = _FAILURE_PRIORITY.get(
            str(previous.get("failure_class")), 0
        )
        if previous and previous_priority > current_priority:
            warning = dict(previous)
            warning["last_observed_at"] = observed_at
            warning["last_failure_class"] = result.failure_class
            warning["last_operation"] = operation
            if run_id:
                warning["last_verification_run_id"] = run_id
        else:
            warning = {
                "schema": 1,
                "status": "unresolved",
                "failure_class": result.failure_class,
                "code": result.failure_code,
                "recorded_at": observed_at,
                "last_observed_at": observed_at,
                "operation": operation,
                "executable": result.executable,
                "exit_code": result.exit_code,
            }
            if run_id:
                warning["verification_run_id"] = run_id
            if result.timeout_seconds is not None:
                warning["timeout_seconds"] = result.timeout_seconds
            if result.windows_status is not None:
                warning["windows_status"] = result.windows_status
            if previous:
                warning["supersedes_failure_class"] = previous.get(
                    "failure_class"
                )
        _atomic_json_write(path, warning)
        return warning


def read_native_warning(root: Path) -> dict[str, Any] | None:
    """Read the structured private warning without changing repository state."""
    try:
        value = json.loads(native_warning_path(root).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, project_context.ProjectContextError):
        return None
    if not isinstance(value, dict) or value.get("status") != "unresolved":
        return None
    return value


def snapshot_native_warning(root: Path) -> NativeWarningSnapshot:
    """Prove clean/unresolved state or report that no safe snapshot was possible."""
    try:
        path = native_warning_path(root)
        if not path.exists():
            return NativeWarningSnapshot(WARNING_CLEAN, None)
        # Writers publish with os.replace, so a direct read sees complete old
        # or new bytes without creating a guard file. A replacement/removal
        # race becomes UNKNOWN and therefore can never masquerade as clean.
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            return NativeWarningSnapshot(WARNING_UNKNOWN, None)
        if value.get("status") == "unresolved":
            return NativeWarningSnapshot(
                WARNING_UNRESOLVED,
                hashlib.sha256(raw).hexdigest(),
            )
        if value.get("status") == "resolved":
            return NativeWarningSnapshot(WARNING_CLEAN, None)
        return NativeWarningSnapshot(WARNING_UNKNOWN, None)
    except (
        OSError,
        UnicodeError,
        ValueError,
        TimeoutError,
        project_context.ProjectContextError,
    ):
        return NativeWarningSnapshot(WARNING_UNKNOWN, None)


def native_warning_identity(root: Path) -> str | None:
    """Compatibility view of the current unresolved warning digest."""
    snapshot = snapshot_native_warning(root)
    return snapshot.identity if snapshot.state == WARNING_UNRESOLVED else None


def _validated_hex_identity(value: str, *, label: str) -> str:
    candidate = str(value).strip()
    if len(candidate) != 64:
        raise ValueError(f"{label} must be a SHA-256 digest")
    try:
        int(candidate, 16)
    except ValueError as exc:
        raise ValueError(f"{label} must be a SHA-256 digest") from exc
    return candidate.lower()


def _validated_run_identity(value: str) -> str:
    candidate = str(value).strip()
    if not candidate or len(candidate) > 128:
        raise ValueError("verification run identity is invalid")
    return candidate


def _unresolved_warning_identity_locked(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get("status") != "unresolved":
        return None
    return hashlib.sha256(raw).hexdigest()


def authorize_recovery_retry(
    root: Path,
    expected_identity: str,
    run_id: str,
) -> str:
    """Issue a private run-bound retry token for one exact unresolved warning.

    This mutating API is intended only for a public command carrying the
    human-provided warning digest. The raw token is returned once and only its
    digest is persisted.
    """
    warning_identity = _validated_hex_identity(
        expected_identity, label="expected native warning identity"
    )
    verification_run = _validated_run_identity(run_id)
    warning_path = native_warning_path(root)
    authorization_path = native_retry_authorization_path(root)
    token = uuid.uuid4().hex + uuid.uuid4().hex
    issuer = capture_process_identity(os.getpid())
    if issuer is None:
        raise RuntimeError("native retry issuer identity could not be authenticated")
    with _exclusive_path_guard(warning_path):
        current = _unresolved_warning_identity_locked(warning_path)
        if current != warning_identity:
            raise ValueError("native retry digest does not match the unresolved warning")
        with _exclusive_path_guard(authorization_path):
            _atomic_json_write(
                authorization_path,
                {
                    "schema": 1,
                    "status": "authorized",
                    "warning_identity": warning_identity,
                    "verification_run_id": verification_run,
                    "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
                    "issued_at": _now_iso(),
                    "issuer": issuer.as_json(),
                },
            )
    return token


def consume_recovery_retry(
    root: Path,
    *,
    token: str,
    run_id: str,
    operation: str,
) -> bool:
    """Consume or reuse the exact grant inside its one authenticated run.

    The first native launch atomically changes ``authorized`` to ``consumed``
    and binds it to this Python process identity. Subsequent engine stages in
    that same verifier process may reuse the consumed grant; no other process,
    run or warning can.
    """
    try:
        verification_run = _validated_run_identity(run_id)
        token_identity = _validated_hex_identity(
            hashlib.sha256(str(token).encode("utf-8")).hexdigest(),
            label="native retry token identity",
        )
    except ValueError:
        return False
    if not str(operation).strip() or len(str(operation)) > 128:
        return False
    consumer = capture_process_identity(os.getpid())
    if consumer is None:
        return False
    warning_path = native_warning_path(root)
    authorization_path = native_retry_authorization_path(root)
    try:
        with _exclusive_path_guard(warning_path):
            warning_identity = _unresolved_warning_identity_locked(warning_path)
            if warning_identity is None:
                return False
            with _exclusive_path_guard(authorization_path):
                try:
                    state = json.loads(
                        authorization_path.read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeError, ValueError):
                    return False
                if not isinstance(state, dict) or state.get("schema") != 1:
                    return False
                if (
                    state.get("warning_identity") != warning_identity
                    or state.get("verification_run_id") != verification_run
                    or state.get("token_sha256") != token_identity
                ):
                    return False
                status = state.get("status")
                if status == "consumed":
                    previous = ProcessIdentity.from_json(state.get("consumer"))
                    return _same_process_identity(previous, consumer)
                if status != "authorized":
                    return False
                consumed = dict(state)
                consumed["status"] = "consumed"
                consumed["consumed_at"] = _now_iso()
                consumed["consumer"] = consumer.as_json()
                consumed["first_operation"] = str(operation).strip()
                _atomic_json_write(authorization_path, consumed)
                return True
    except (OSError, TimeoutError, project_context.ProjectContextError):
        return False


def _retry_authorized_for_launch(
    root: Path,
    *,
    operation: str,
    verification_run_id: str | None,
    retry_token: str | None,
) -> tuple[bool, str]:
    snapshot = snapshot_native_warning(root)
    if snapshot.state == WARNING_CLEAN:
        return True, ""
    if snapshot.state == WARNING_UNKNOWN:
        return False, "native warning state is unreadable; engine launch refused"
    run_id = str(
        verification_run_id
        or os.environ.get("KIT_VERIFY_NONCE", "")
    ).strip()
    token = str(
        retry_token
        or os.environ.get("KIT_NATIVE_RETRY_TOKEN", "")
    ).strip()
    if not run_id or not token:
        return False, (
            "an unresolved native warning requires an exact run-bound retry authorization"
        )
    if not consume_recovery_retry(
        root,
        token=token,
        run_id=run_id,
        operation=operation,
    ):
        return False, (
            "native retry authorization did not match this warning, run and process"
        )
    return True, ""


def resolve_native_warning(
    root: Path,
    *,
    verification_scope: str,
    expected_identity: str,
) -> bool:
    """Mark recovery only after the caller proves a full or strict gate pass."""
    if verification_scope not in {"full", "strict"}:
        raise ValueError("only a passing full or strict verification may clear native safety")
    if not isinstance(expected_identity, str) or len(expected_identity) != 64:
        raise ValueError("expected native warning identity must be a SHA-256 digest")
    try:
        int(expected_identity, 16)
    except ValueError as exc:
        raise ValueError(
            "expected native warning identity must be a SHA-256 digest"
        ) from exc
    path = native_warning_path(root)
    try:
        with _exclusive_path_guard(path):
            try:
                raw = path.read_bytes()
                warning = json.loads(raw.decode("utf-8"))
            except (OSError, UnicodeError, ValueError):
                warning = None
                raw = b""
            if not isinstance(warning, dict) or warning.get("status") != "unresolved":
                return False
            current_identity = hashlib.sha256(raw).hexdigest()
            if expected_identity != current_identity:
                return False
            resolved = dict(warning)
            resolved["status"] = "resolved"
            resolved["resolved_at"] = _now_iso()
            resolved["resolved_by"] = verification_scope
            resolved["resolved_warning_identity"] = current_identity
            _atomic_json_write(path, resolved)
            authorization_path = native_retry_authorization_path(root)
            try:
                with _exclusive_path_guard(authorization_path):
                    try:
                        authorization = json.loads(
                            authorization_path.read_text(encoding="utf-8")
                        )
                    except (OSError, UnicodeError, ValueError):
                        authorization = None
                    if (
                        isinstance(authorization, dict)
                        and authorization.get("warning_identity") == current_identity
                    ):
                        authorization_path.unlink(missing_ok=True)
            except (OSError, TimeoutError):
                pass
            return True
    except TimeoutError:
        return False
