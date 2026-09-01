#!/usr/bin/env python3
"""Query Godot's language server for diagnostics and symbol navigation.

WHY THIS EXISTS
Diagnostics from here are the SAME analyser as `check.py --only typecheck`;
there is no quality difference. The reason to run it is navigation:
go-to-definition, find-references and document symbols, which no gate stage can
provide. A session log showed one file re-read 573 times because the agent had
no way to locate a symbol.

STATUS: advisory. Never a gate stage, never blocking.

LIFECYCLE
Owns its own `--editor --headless` instance on port 6105, so it never contends
with a human editor on 6005. A Godot instance holds `.godot/`, which the gate's
own `--import` writes to, so `stop` before running the gate.

    kit gdls start
    kit gdls status
    kit gdls diagnose scripts/logic/foo.gd
    kit gdls refs MyClass
    kit gdls symbols scripts/logic/foo.gd
    kit gdls stop

Staleness is impossible here: file text is pushed from disk on every request,
so the server never serves a cached buffer.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from ctypes import wintypes
from typing import Any, Dict, List, Optional

import engine_discovery
import native_engine
import project_context

TOOLS = Path(__file__).resolve().parent
CORE_ROOT = TOOLS.parent
CONTEXT = project_context.load_active_context(CORE_ROOT)
ROOT = CONTEXT.project_root  # compatibility name for project-owned paths
GAME_ROOT = CONTEXT.game_root
PORT = 6105
PID_FILE = CONTEXT.runtime_root / "gdls.json"
READY_TIMEOUT = 60.0


def find_godot(candidate: str | None = None) -> engine_discovery.AuthenticatedEngine:
    """Select and authenticate the exact engine before language-server startup."""
    if candidate:
        return engine_discovery.authenticate_godot(
            ROOT,
            candidate=candidate,
            operation="gdls-start",
        )
    return engine_discovery.authenticate_godot(
        ROOT,
        selection=engine_discovery.select_godot(ROOT),
        operation="gdls-start",
    )


def _saved_owner() -> native_engine.BackgroundReceipt | None:
    return native_engine.read_background_receipt(PID_FILE)


def _saved_journal() -> native_engine.BackgroundJournal | None:
    return native_engine.read_background_journal(PID_FILE)


def _saved_schema() -> int | None:
    try:
        value = json.loads(PID_FILE.read_text(encoding="utf-8"))
        return int(value.get("schema", 0)) if isinstance(value, dict) else None
    except (OSError, UnicodeError, ValueError, TypeError):
        return None


def _warning(
    code: str,
    summary: str,
    *,
    failure_class: str = "native-safety",
) -> dict[str, str]:
    return {
        "code": code,
        "summary": summary,
        "failure_class": failure_class,
        "operation": "gdls-unexpected-exit",
    }


def inspect_status() -> dict[str, Any]:
    """Inspect the retained server without launching, killing or rewriting state."""
    listening = port_open()
    if not PID_FILE.exists():
        if listening:
            return {
                "status": "foreign_listener",
                "port": PORT,
                "owned": False,
                "warning": _warning(
                    "gdls-foreign-listener",
                    f"port {PORT} is open without a kit ownership receipt",
                ),
            }
        lock = native_engine.inspect_engine_lock(ROOT)
        if lock.state == native_engine.LOCK_CLEAN:
            return {"status": "not_running", "port": PORT, "owned": False}
        return {
            "status": f"engine_lock_{lock.state}",
            "pid": lock.pid,
            "port": PORT,
            "owned": False,
            "warning": _warning(
                f"native-engine-lock-{lock.state}",
                lock.detail,
                failure_class=(
                    "native-crash"
                    if lock.state == native_engine.LOCK_ABANDONED
                    else "native-safety"
                ),
            ),
        }

    receipt = _saved_owner()
    if receipt is None:
        return {
            "status": "ownership_unreadable",
            "port": PORT,
            "owned": False,
            "warning": _warning(
                "gdls-ownership-unreadable",
                "the retained language-server receipt is unreadable; no process is trusted",
            ),
        }
    inspection = native_engine.inspect_background_owner(ROOT, receipt)
    if inspection.state == native_engine.BACKGROUND_OWNER_VANISHED:
        return {
            "status": "owner_vanished",
            "pid": receipt.pid,
            "port": PORT,
            "owned": False,
            "warning": _warning(
                "native-engine-disappeared",
                "the exactly owned Godot language-server process disappeared unexpectedly",
                failure_class="native-crash",
            ),
        }
    if inspection.state != native_engine.BACKGROUND_OWNED_LIVE:
        return {
            "status": "ownership_mismatch",
            "pid": receipt.pid,
            "port": PORT,
            "owned": False,
            "warning": _warning(
                "gdls-ownership-mismatch",
                inspection.detail,
            ),
        }
    journal = _saved_journal()
    if _saved_schema() == 3 and journal is None:
        return {
            "status": "ownership_unreadable",
            "pid": receipt.pid,
            "port": PORT,
            "owned": False,
            "warning": _warning(
                "gdls-journal-unreadable",
                "the language-server lifecycle journal is malformed; no process is trusted",
            ),
        }
    if journal is not None and journal.lifecycle == "starting":
        return {
            "status": "launch_incomplete",
            "pid": receipt.pid,
            "port": PORT,
            "owned": True,
            "warning": _warning(
                "gdls-launch-incomplete",
                "the exact language-server process is still in a recoverable startup handoff",
            ),
        }
    if not listening:
        return {
            "status": "owned_not_listening",
            "pid": receipt.pid,
            "port": PORT,
            "owned": True,
            "warning": _warning(
                "gdls-owned-not-listening",
                "the exactly owned process is alive but its language-server port is closed",
            ),
        }
    return {
        "status": "running",
        "pid": receipt.pid,
        "port": PORT,
        "owned": True,
    }


def _record_abandoned_owner(
    owner: native_engine.BackgroundReceipt | None = None,
) -> bool:
    owner = owner or _saved_owner()
    if owner is None:
        return False
    failure = native_engine.reconcile_abandoned_background(
        ROOT,
        owner,
        executable=owner.process_identity.executable_name,
        operation="gdls-unexpected-exit",
    )
    if failure is None:
        return False
    native_engine.remove_background_journal(
        PID_FILE, expected_receipt=owner
    )
    return True


def cmd_status(_args: argparse.Namespace) -> int:
    status = inspect_status()
    print(json.dumps(status))
    return 0 if status["status"] in {"running", "not_running"} else 1


def _cleanup_started_process(
    process: Any,
    token: str,
    receipt: native_engine.BackgroundReceipt | None,
) -> bool:
    """End only this launch and remove only its still-matching receipt."""
    if receipt is not None:
        stopped = native_engine.stop_owned_background(ROOT, receipt)
    elif os.name == "nt":
        try:
            process.kill()
            process.wait(timeout=10)
            stopped = process.poll() is not None
        except (OSError, subprocess.SubprocessError):
            stopped = False
    else:
        stopped = bool(native_engine.terminate_owned_process_tree(process))
    if not stopped:
        return False
    try:
        process.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    if receipt is None:
        native_engine.release_engine_lock(ROOT, token)
    if receipt is not None:
        native_engine.remove_background_journal(
            PID_FILE, expected_receipt=receipt
        )
    return True


def port_open(port: int = PORT) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


class _TcpOwnerRow(ctypes.Structure):
    _fields_ = [
        ("state", wintypes.DWORD),
        ("local_address", wintypes.DWORD),
        ("local_port", wintypes.DWORD),
        ("remote_address", wintypes.DWORD),
        ("remote_port", wintypes.DWORD),
        ("owner_pid", wintypes.DWORD),
    ]


def _windows_listener_owned_by(pid: int, port: int) -> bool | None:
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)
        iphlpapi.GetExtendedTcpTable.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
            wintypes.ULONG,
            ctypes.c_int,
            wintypes.ULONG,
        ]
        iphlpapi.GetExtendedTcpTable.restype = wintypes.DWORD
        size = wintypes.DWORD(0)
        # AF_INET, TCP_TABLE_OWNER_PID_LISTENER.
        first = int(
            iphlpapi.GetExtendedTcpTable(
                None, ctypes.byref(size), False, 2, 3, 0
            )
        )
        if first not in {0, 122} or int(size.value) < ctypes.sizeof(wintypes.DWORD):
            return None
        buffer = ctypes.create_string_buffer(int(size.value))
        if int(
            iphlpapi.GetExtendedTcpTable(
                buffer, ctypes.byref(size), False, 2, 3, 0
            )
        ) != 0:
            return None
        count = wintypes.DWORD.from_buffer_copy(buffer.raw[:4]).value
        offset = ctypes.sizeof(wintypes.DWORD)
        row_size = ctypes.sizeof(_TcpOwnerRow)
        found_port = False
        for index in range(int(count)):
            start = offset + index * row_size
            end = start + row_size
            if end > len(buffer.raw):
                return None
            row = _TcpOwnerRow.from_buffer_copy(buffer.raw[start:end])
            local_port = socket.ntohs(int(row.local_port) & 0xFFFF)
            if local_port == port:
                found_port = True
                if int(row.owner_pid) == pid:
                    return True
        return False if found_port else None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _linux_listener_owned_by(pid: int, port: int) -> bool | None:
    sockets: set[str] = set()
    try:
        for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
            if not table.is_file():
                continue
            for line in table.read_text(encoding="ascii").splitlines()[1:]:
                fields = line.split()
                if len(fields) < 10 or fields[3] != "0A":
                    continue
                local = fields[1]
                if ":" not in local:
                    continue
                if int(local.rsplit(":", 1)[1], 16) == port:
                    sockets.add(fields[9])
        if not sockets:
            return None
        for entry in (Path("/proc") / str(pid) / "fd").iterdir():
            try:
                target = os.readlink(entry)
            except OSError:
                continue
            if target.startswith("socket:[") and target[8:-1] in sockets:
                return True
        return False
    except (OSError, UnicodeError, ValueError):
        return None


def listener_owned_by(pid: int, port: int = PORT) -> bool | None:
    """Bind a listening socket to the retained owner where the OS exposes it."""
    if os.name == "nt":
        return _windows_listener_owned_by(pid, port)
    if sys.platform.startswith("linux"):
        return _linux_listener_owned_by(pid, port)
    return None


class Client:
    """Minimal LSP client over TCP. Enough for diagnostics and navigation."""

    def __init__(self, port: int = PORT, *, connect_timeout: float = 20.0) -> None:
        self.sock = socket.create_connection(
            ("127.0.0.1", port), timeout=connect_timeout
        )
        self.buf = b""
        self.next_id = 1

    def _send(self, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        head = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        self.sock.sendall(head + body)

    def notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: Dict[str, Any],
                timeout: float = 20.0) -> Optional[Dict[str, Any]]:
        rid = self.next_id
        self.next_id += 1
        self._send({"jsonrpc": "2.0", "id": rid, "method": method,
                    "params": params})
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = self._read(deadline - time.time())
            if msg is None:
                break
            if msg.get("id") == rid:
                return msg
            self._stash(msg)
        return None

    def _stash(self, msg: Dict[str, Any]) -> None:
        if msg.get("method") == "textDocument/publishDiagnostics":
            self.diagnostics.append(msg.get("params", {}))

    diagnostics: List[Dict[str, Any]] = []

    def _read(self, timeout: float) -> Optional[Dict[str, Any]]:
        self.sock.settimeout(max(0.2, timeout))
        while True:
            if b"\r\n\r\n" in self.buf:
                head, rest = self.buf.split(b"\r\n\r\n", 1)
                length = 0
                for line in head.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":")[1].strip())
                if len(rest) >= length:
                    self.buf = rest[length:]
                    try:
                        return json.loads(rest[:length].decode("utf-8"))
                    except json.JSONDecodeError:
                        return None
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                return None
            self.buf += chunk

    def initialise(self, *, timeout: float = 20.0) -> bool:
        response = self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": GAME_ROOT.as_uri(),
            "capabilities": {},
        }, timeout=timeout)
        if not isinstance(response, dict) or not isinstance(response.get("result"), dict):
            return False
        self.notify("initialized", {})
        return True

    def open_fresh(self, rel: str) -> str:
        """Push current disk text so the server cannot serve a stale buffer."""
        path = (GAME_ROOT / rel).resolve()
        uri = path.as_uri()
        text = path.read_text(encoding="utf-8", errors="replace")
        self.notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": "gdscript",
                             "version": int(time.time()), "text": text},
        })
        return uri

    def close(self) -> None:
        try:
            self.notify("exit", {})
        finally:
            self.sock.close()

    def disconnect(self) -> None:
        self.sock.close()


def _probe_lsp_ready(pid: int) -> bool:
    owner = listener_owned_by(pid)
    if owner is False:
        return False
    client: Client | None = None
    try:
        client = Client(connect_timeout=1.0)
        return client.initialise(timeout=2.0)
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if client is not None:
            try:
                client.disconnect()
            except OSError:
                pass


def cmd_start(args: argparse.Namespace) -> int:
    current = inspect_status()
    if current["status"] == "running":
        print(json.dumps({"status": "already_running", "pid": current["pid"],
                          "port": PORT, "owned": True}))
        return 0
    if current["status"] == "owner_vanished":
        if _record_abandoned_owner():
            print(json.dumps({
                "status": "native_warning",
                "detail": current["warning"]["summary"],
                "warning": current["warning"],
            }))
            return 1
    elif current["status"] != "not_running":
        print(json.dumps({
            "status": "error",
            "detail": current.get("warning", {}).get(
                "summary", "language-server ownership could not be authenticated"
            ),
            "warning": current.get("warning"),
        }))
        return 1

    candidate = getattr(args, "engine", None)
    if not isinstance(candidate, str):
        candidate = None
    try:
        authenticated = find_godot(candidate)
        authenticated.assert_unchanged()
    except engine_discovery.EngineAuthenticationError as exc:
        print(json.dumps({
            "status": "error",
            "failure_class": exc.status,
            "detail": str(exc),
        }))
        return 2
    started = native_engine.start_godot(
        authenticated.path,
        ["--path", str(GAME_ROOT), "--editor", "--headless", f"--lsp-port={PORT}"],
        root=ROOT,
        cwd=ROOT,
        capture_output=False,
        operation="gdls-start",
    )
    if started.failure is not None:
        native_engine.persist_native_failure(
            ROOT, started.failure, operation="gdls-start"
        )
        print(json.dumps({
            "status": "error",
            "failure_class": started.failure.failure_class,
            "detail": started.failure.output.strip(),
        }))
        return 1
    proc = started.process
    token = started.lock_token
    process_identity = started.process_identity
    assert proc is not None and token is not None and process_identity is not None
    expected_receipt = native_engine.BackgroundReceipt(
        int(proc.pid),
        token,
        process_identity,
    )
    operation_id = f"gdls-{int(proc.pid)}-{time.time_ns()}"
    expected_journal: native_engine.BackgroundJournal | None = None
    try:
        try:
            native_engine.write_background_journal(
                PID_FILE,
                receipt=expected_receipt,
                operation_id=operation_id,
            )
            expected_journal = _saved_journal()
            if (
                expected_journal is None
                or expected_journal.receipt != expected_receipt
                or expected_journal.lifecycle != "starting"
                or expected_journal.operation_id != operation_id
            ):
                raise OSError("language-server startup journal did not round trip")
        except (OSError, ValueError) as exc:
            cleaned = _cleanup_started_process(proc, token, None)
            failure = native_engine.NativeResult(
                exit_code=native_engine.START_FAILED_EXIT,
                output=(
                    "language-server ownership journal could not be written: "
                    f"{type(exc).__name__}"
                    + ("" if cleaned else "; owned process termination was not confirmed")
                ),
                executable=authenticated.path.name,
                started=True,
                failure_class="engine-start-failed",
                failure_code="native-engine-start-failed",
            )
            native_engine.persist_native_failure(ROOT, failure, operation="gdls-start")
            print(json.dumps({"status": "error", "failure_class": failure.failure_class,
                              "detail": failure.output}))
            return 1

        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                code = int(proc.returncode if proc.returncode is not None else 1)
                crashed, windows_status = native_engine.classify_crash_exit(code)
                failure = native_engine.NativeResult(
                    exit_code=code,
                    output="language-server engine exited during startup",
                    executable=authenticated.path.name,
                    started=True,
                    failure_class="native-crash" if crashed else "engine-start-failed",
                    failure_code="native-crash" if crashed else "native-engine-start-failed",
                    windows_status=windows_status,
                )
                native_engine.persist_native_failure(
                    ROOT, failure, operation="gdls-start"
                )
                _cleanup_started_process(proc, token, expected_receipt)
                print(json.dumps({"status": "error", "detail":
                                  f"godot exited {code}",
                                  "failure_class": failure.failure_class}))
                return 1
            if port_open():
                receipt = _saved_owner()
                inspection = (
                    native_engine.inspect_background_owner(ROOT, receipt)
                    if receipt is not None else None
                )
                owner_matches = bool(
                    receipt == expected_receipt
                    and inspection is not None
                    and inspection.state == native_engine.BACKGROUND_OWNED_LIVE
                )
                listener_owner = listener_owned_by(int(proc.pid))
                if not owner_matches or listener_owner is False:
                    cleaned = _cleanup_started_process(
                        proc, token, expected_receipt
                    )
                    failure = native_engine.NativeResult(
                        exit_code=native_engine.START_FAILED_EXIT,
                        output=(
                            "language-server port opened without matching exact process ownership"
                            + ("" if cleaned else "; owned process termination was not confirmed")
                        ),
                        executable=authenticated.path.name,
                        started=True,
                        failure_class="engine-start-failed",
                        failure_code="native-engine-start-failed",
                    )
                    native_engine.persist_native_failure(
                        ROOT, failure, operation="gdls-start"
                    )
                    print(json.dumps({"status": "error",
                                      "failure_class": failure.failure_class,
                                      "detail": failure.output}))
                    return 1
                if _probe_lsp_ready(int(proc.pid)):
                    assert expected_journal is not None
                    if not native_engine.mark_background_ready(
                        PID_FILE, expected=expected_journal
                    ):
                        cleaned = _cleanup_started_process(
                            proc, token, expected_receipt
                        )
                        failure = native_engine.NativeResult(
                            exit_code=native_engine.START_FAILED_EXIT,
                            output=(
                                "language-server ready handoff could not be committed"
                                + ("" if cleaned else "; owned process termination was not confirmed")
                            ),
                            executable=authenticated.path.name,
                            started=True,
                            failure_class="engine-start-failed",
                            failure_code="native-engine-start-failed",
                        )
                        native_engine.persist_native_failure(
                            ROOT, failure, operation="gdls-start"
                        )
                        print(json.dumps({"status": "error",
                                          "failure_class": failure.failure_class,
                                          "detail": failure.output}))
                        return 1
                    print(json.dumps({"status": "started", "pid": proc.pid,
                                      "port": PORT, "owned": True,
                                      "engine_version": authenticated.version}))
                    return 0
            time.sleep(0.2)
        cleaned = _cleanup_started_process(proc, token, expected_receipt)
        failure = native_engine.NativeResult(
            exit_code=native_engine.TIMEOUT_EXIT,
            output=(
                f"no authenticated language-server response after {READY_TIMEOUT}s; "
                + (
                    "owned process tree terminated"
                    if cleaned else "owned process termination was not confirmed"
                )
            ),
            executable=authenticated.path.name,
            started=True,
            failure_class="engine-timeout",
            failure_code="native-engine-timeout",
            timeout_seconds=int(READY_TIMEOUT),
        )
        native_engine.persist_native_failure(ROOT, failure, operation="gdls-start")
        print(json.dumps({"status": "error",
                          "detail": failure.output,
                          "failure_class": failure.failure_class}))
        return 1
    except BaseException:
        cleaned = _cleanup_started_process(proc, token, expected_receipt)
        if not cleaned:
            failure = native_engine.NativeResult(
                exit_code=native_engine.START_FAILED_EXIT,
                output="language-server startup was interrupted and owned termination was not confirmed",
                executable=authenticated.path.name,
                started=True,
                failure_class="engine-start-failed",
                failure_code="native-engine-start-failed",
            )
            try:
                native_engine.persist_native_failure(
                    ROOT, failure, operation="gdls-start-interrupted"
                )
            except (OSError, ValueError):
                pass
        raise


def cmd_stop(args: argparse.Namespace) -> int:
    if not PID_FILE.is_file():
        status = inspect_status()
        print(json.dumps(status))
        return 0 if status["status"] == "not_running" else 1
    owner = _saved_owner()
    if owner is None:
        print(json.dumps({"status": "error", "detail":
                          "language-server ownership state is unreadable; refusing to kill a process"}))
        return 1
    pid = owner.pid
    if _record_abandoned_owner(owner):
        print(json.dumps({"status": "not_running", "pid": pid,
                          "warning": "the retained Godot process ended unexpectedly"}))
        return 1
    inspection = native_engine.inspect_background_owner(ROOT, owner)
    if (
        inspection.state != native_engine.BACKGROUND_OWNED_LIVE
        or not native_engine.stop_owned_background(ROOT, owner)
    ):
        print(json.dumps({"status": "error", "detail":
                          "language-server ownership does not match the native lock; refusing to kill a process"}))
        return 1
    if not native_engine.remove_background_journal(
        PID_FILE, expected_receipt=owner
    ) and PID_FILE.exists():
        print(json.dumps({"status": "error", "detail":
                          "language-server stopped but its exact lifecycle journal changed; it was preserved"}))
        return 1
    print(json.dumps({"status": "stopped", "pid": pid}))
    return 0


def _connect() -> Optional[Client]:
    status = inspect_status()
    if status["status"] != "running":
        print(json.dumps({
            "status": "error",
            "detail": status.get("warning", {}).get(
                "summary", "not running; use kit gdls start"
            ),
            "server_status": status["status"],
        }))
        return None
    try:
        c = Client()
    except OSError as exc:
        print(json.dumps({
            "status": "error",
            "detail": f"owned language server became unavailable: {type(exc).__name__}",
        }))
        return None
    c.diagnostics = []
    if not c.initialise():
        try:
            c.disconnect()
        except OSError:
            pass
        print(json.dumps({
            "status": "error",
            "detail": "owned language server did not complete LSP initialization",
        }))
        return None
    return c


def cmd_diagnose(args: argparse.Namespace) -> int:
    c = _connect()
    if c is None:
        return 2
    uri = c.open_fresh(args.file)
    deadline = time.time() + 8.0
    while time.time() < deadline:
        msg = c._read(deadline - time.time())
        if msg is None:
            break
        c._stash(msg)
    hits = [d for d in c.diagnostics if d.get("uri") == uri]
    out = []
    for group in hits:
        for d in group.get("diagnostics", []):
            rng = d.get("range", {}).get("start", {})
            out.append({"line": rng.get("line", 0) + 1,
                        "column": rng.get("character", 0) + 1,
                        "severity": d.get("severity"),
                        "message": d.get("message", "")})
    c.close()
    print(json.dumps({"file": args.file, "count": len(out),
                      "diagnostics": out}, indent=2))
    return 0


def cmd_symbols(args: argparse.Namespace) -> int:
    c = _connect()
    if c is None:
        return 2
    uri = c.open_fresh(args.file)
    resp = c.request("textDocument/documentSymbol",
                     {"textDocument": {"uri": uri}})
    c.close()
    print(json.dumps({"file": args.file,
                      "result": (resp or {}).get("result")}, indent=2))
    return 0


def _text_scan(symbol: str) -> List[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    for path in sorted(GAME_ROOT.rglob("*.gd")):
        parts = path.relative_to(GAME_ROOT).parts
        if any(p in {".godot", "addons", "build", ".git"} for p in parts):
            continue
        for num, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if symbol in line:
                hits.append({"file": path.relative_to(GAME_ROOT).as_posix(),
                             "line": num, "text": line.strip()[:160]})
    return hits


def cmd_refs(args: argparse.Namespace) -> int:
    """Symbol search. Works without a server: falls back to a text scan."""
    result = None
    server_status = inspect_status()
    if server_status["status"] == "running":
        c = _connect()
        if c is not None:
            resp = c.request("workspace/symbol", {"query": args.symbol})
            c.close()
            result = (resp or {}).get("result")
    if result:
        print(json.dumps({"symbol": args.symbol, "source": "lsp",
                          "result": result}, indent=2))
        return 0
    hits = _text_scan(args.symbol)
    print(json.dumps({"symbol": args.symbol, "source": "text-scan",
                      "count": len(hits), "result": hits}, indent=2))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--engine", help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start").set_defaults(fn=cmd_start)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    p = sub.add_parser("diagnose")
    p.add_argument("file")
    p.set_defaults(fn=cmd_diagnose)
    p = sub.add_parser("symbols")
    p.add_argument("file")
    p.set_defaults(fn=cmd_symbols)
    p = sub.add_parser("refs")
    p.add_argument("symbol")
    p.set_defaults(fn=cmd_refs)
    args = ap.parse_args()
    return int(args.fn(args))


if __name__ == "__main__":
    raise SystemExit(main())
