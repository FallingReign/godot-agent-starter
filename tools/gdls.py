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

    python tools/gdls.py start
    python tools/gdls.py diagnose scripts/logic/foo.gd
    python tools/gdls.py refs MyClass
    python tools/gdls.py symbols scripts/logic/foo.gd
    python tools/gdls.py stop

Staleness is impossible here: file text is pushed from disk on every request,
so the server never serves a cached buffer.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent
PORT = 6105
PID_FILE = ROOT / ".checklogs" / "gdls.pid"
READY_TIMEOUT = 60.0


def find_godot() -> Optional[str]:
    env = os.environ.get("GODOT_BIN")
    if env and (Path(env).is_file() or shutil.which(env)):
        return env
    for name in ("godot", "godot4", "Godot"):
        hit = shutil.which(name)
        if hit:
            return hit
    return None


def port_open(port: int = PORT) -> bool:
    with socket.socket() as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Client:
    """Minimal LSP client over TCP. Enough for diagnostics and navigation."""

    def __init__(self, port: int = PORT) -> None:
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=20)
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

    def initialise(self) -> None:
        self.request("initialize", {
            "processId": os.getpid(),
            "rootUri": ROOT.as_uri(),
            "capabilities": {},
        })
        self.notify("initialized", {})

    def open_fresh(self, rel: str) -> str:
        """Push current disk text so the server cannot serve a stale buffer."""
        path = (ROOT / rel).resolve()
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


def cmd_start(args: argparse.Namespace) -> int:
    if port_open():
        print(json.dumps({"status": "already_running", "port": PORT}))
        return 0
    godot = find_godot()
    if godot is None:
        print(json.dumps({"status": "error",
                          "detail": "godot not found; set GODOT_BIN"}))
        return 2
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        [godot, "--path", str(ROOT), "--editor", "--headless",
         f"--lsp-port={PORT}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    deadline = time.time() + READY_TIMEOUT
    while time.time() < deadline:
        if port_open():
            print(json.dumps({"status": "started", "pid": proc.pid,
                              "port": PORT}))
            return 0
        if proc.poll() is not None:
            print(json.dumps({"status": "error", "detail":
                              f"godot exited {proc.returncode}"}))
            return 1
        time.sleep(0.5)
    print(json.dumps({"status": "error",
                      "detail": f"no response on {PORT} after {READY_TIMEOUT}s"}))
    return 1


def cmd_stop(args: argparse.Namespace) -> int:
    if not PID_FILE.is_file():
        print(json.dumps({"status": "not_running"}))
        return 0
    pid = int(PID_FILE.read_text(encoding="utf-8").strip() or 0)
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           capture_output=True, check=False)
        else:
            os.kill(pid, 15)
    except (ProcessLookupError, ValueError, OSError):
        pass
    PID_FILE.unlink(missing_ok=True)
    print(json.dumps({"status": "stopped", "pid": pid}))
    return 0


def _connect() -> Optional[Client]:
    if not port_open():
        print(json.dumps({"status": "error", "detail":
                          "not running; python tools/gdls.py start"}))
        return None
    c = Client()
    c.diagnostics = []
    c.initialise()
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
    for path in sorted(ROOT.rglob("*.gd")):
        parts = path.relative_to(ROOT).parts
        if any(p in {".godot", "addons", "build", ".git"} for p in parts):
            continue
        for num, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if symbol in line:
                hits.append({"file": path.relative_to(ROOT).as_posix(),
                             "line": num, "text": line.strip()[:160]})
    return hits


def cmd_refs(args: argparse.Namespace) -> int:
    """Symbol search. Works without a server: falls back to a text scan."""
    result = None
    if port_open():
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
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("start").set_defaults(fn=cmd_start)
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
