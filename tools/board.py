#!/usr/bin/env python3
"""Local loopback board: makes plan.html and retro.html interactive.

Standard library only. `http.server` binds to 127.0.0.1 on an ephemeral port.
Loopback is transport, not authentication: every mutation also requires the
served page's per-process capability plus an exact same-origin request.

Why this exists: the tick boxes plan.html and retro.html render are Unicode
glyphs, not inputs, because a page opened with `file://` cannot capture a
click, persist a decision, or spawn a process -- `fetch()` is blocked
entirely under that protocol. That restriction is already load-bearing
elsewhere in this kit (it is why design docs are inlined into plan.html
rather than fetched), so the fix is not to fight it: serve the same static
pages over loopback HTTP instead, where `fetch()` works, and let the pages
detect which situation they are in by trying it.

The ephemeral board index lives in `.kit/runtime/board/state.json`: the port,
the server's own pid, and a list of dispatched agent runs. Queue artifacts,
prompts and run logs live beside it under the private runtime. None is durable
product intent -- accept/defer decisions live in `docs/retro/accepted.json` and
`docs/retro/deferred.json` (see `tools/retro_html.py`), findings stay in
`docs/retro/*-findings.md`. Losing `board.state.json` loses only "what is the
board doing right now", never a decision or a finding. In particular the
human's approval *comment* is durable: it is stored on the accepted.json
entry, because it is the amended proposal a worker was dispatched with, not a
UI nicety, and it must survive a board restart.

HTTP contract -- **this list is the authoritative one**; `docs/retro/README.md`
describes the user workflow without duplicating this endpoint inventory:

    GET  /api/health              cheap liveness probe, no side effects
    GET  /api/state               the whole view model in one request
    GET  /api/finding/<slug>      artifact + prompt_preview, before approving
    POST /api/finding/approve     {slug, comment} -> record; dispatch if ready
    POST /api/finding/defer       {slug, reason}  -> record, never dispatch
    POST /api/retro/run           start the retrospective model (spends quota)
    GET  /api/runs/<run_id>       status, exit code, silence, log tail

Those seven are the whole surface. `/api/dispatch/prepare`, `/api/dispatch/run`
and `/api/decision` were deleted rather than left as dead paths: a stale page
hitting one gets `{"code": "no_such_endpoint"}` and says so, and there is
exactly one approval path instead of two.

Approval is always a durable decision. With a configured automatic provider it
also attempts an isolated dispatch and may spend quota; with a manual, invalid,
or unavailable provider it remains honestly approved without a run. Automatic
runs stay sequential because two findings can name the same `fix_files`.

Every API response is JSON, including every error: a non-2xx code with a
message worth showing. No HTML traceback and no 200 with a silent failure ever
leaves this server.

    kit serve                                      start or reuse the board and
                                                   print its authenticated URL

Low-level serve and migration switches are internal implementation details;
the public workflow never asks a developer to invoke this file directly.

Every other tool keeps working with the board absent. `kit plan` only renders;
`kit serve` is the explicit lifecycle boundary. A board that is not running
therefore leaves truthful read-only pages rather than turning rendering into a
hidden background-process launch.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import http.server
import json
import os
import re
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RETRO_DIR = ROOT / "docs" / "retro"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_paths   # noqa: E402
import providers       # noqa: E402  (closed adapters; no arbitrary commands)
import retro_rank      # noqa: E402  (sibling module; no top-level import of `board`, so this is not a cycle)
import retro_queue     # noqa: E402  (dispatch prompt artifacts; same non-cycle argument)
import retro_due       # noqa: E402  (deterministic note counter; it never calls a model and must not learn how)
import retro_html      # noqa: E402  (same -- see its main() for the lazy `import board`)
import session_digest  # noqa: E402
import board_client    # noqa: E402  (CSS/JS constants only, zero project imports of its own)
import run_result      # noqa: E402  (structured worker outcome verification)

_RUNTIME = runtime_paths.resolve(ROOT)
RUNS_DIR = _RUNTIME.board_runs
STATE_FILE = _RUNTIME.board_state
LOCK_FILE = _RUNTIME.board_lock
BOARD_LOG = _RUNTIME.board_log

SCHEMA = board_client.PROTOCOL_SCHEMA
BOARD_VERSION = board_client.protocol_version()
MAX_REQUEST_BODY = 64 * 1024

# Finding states reported by /api/state. `stale` is orthogonal to all of them.
STATES = ("awaiting_review", "approved", "queued", "working", "done", "blocked",
          "unverified", "failed", "deferred")
LIVE_RUN_STATES = frozenset(("queued", "reserved", "starting", "running", "verifying"))
TERMINAL_RUN_STATES = frozenset(("completed", "blocked", "unverified", "failed"))
DISPATCH_LEDGER_CHANGES = frozenset(("docs/retro/accepted.json",))

# Silence threshold before an alive-but-quiet run is reported as such rather
# than as plain "running". Three minutes: a kit-builder slice's individual
# tool calls (view, edit, a targeted check.py run) each take seconds, so
# three minutes of nothing written to the log is already an unusual gap
# without being noisy over an ordinary short pause. Configurable because
# "unusual" depends on what the dispatched task actually does.
SILENCE_MINUTES_DEFAULT = 3.0

_state_file_lock = threading.RLock()
_decision_file_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_json_write(path: Path, value) -> None:
    """Replace one JSON file atomically within its own directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass

# --------------------------------------------------------------- state.json

def migrate_state(value: dict) -> tuple[dict, bool]:
    """Normalise legacy run records without inventing evidence.

    Before structured worker results existed, an exit-zero process was stored
    as ``finished`` and the UI translated that directly to ``done``.  There is
    no defensible migration from process exit to implementation, so finding
    runs in that shape become explicitly unverified.  Retrospective-model runs
    are not implementation claims and retain their historic status.
    """
    state = dict(value) if isinstance(value, dict) else {}
    raw_runs = state.get("runs")
    runs: list[dict] = []
    changed = not isinstance(raw_runs, list)
    for raw in raw_runs if isinstance(raw_runs, list) else []:
        if not isinstance(raw, dict):
            changed = True
            continue
        entry = dict(raw)
        if (entry.get("kind", "finding") == "finding"
                and entry.get("status") == "finished"):
            entry["status"] = "unverified"
            entry.setdefault("outcome", "unverified")
            errors = entry.get("result_errors")
            if not isinstance(errors, list) or not errors:
                entry["result_errors"] = [
                    "legacy exit-zero run has no structured worker result"
                ]
            changed = True
        runs.append(entry)
    state.setdefault("port", None)
    state.setdefault("pid", None)
    state.setdefault("started", None)
    state["runs"] = runs
    return state, changed


def load_state() -> dict:
    with _state_file_lock:
        if not STATE_FILE.exists():
            return {"port": None, "pid": None, "started": None, "runs": []}
        try:
            d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
    return migrate_state(d)[0]


def save_state(state: dict) -> None:
    with _state_file_lock:
        _atomic_json_write(STATE_FILE, state)


def mutate_state(change) -> dict:
    """Apply one in-process state transition as a locked atomic replacement."""
    with _state_file_lock:
        state = load_state()
        change(state)
        save_state(state)
        return state


def migrate_state_file() -> int:
    """Persist the truthful legacy migration once; safe to call repeatedly."""
    with _state_file_lock:
        if not STATE_FILE.exists():
            return 0
        try:
            raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        migrated, changed = migrate_state(raw)
        if changed:
            save_state(migrated)
            return 1
    return 0


# ------------------------------------------------------------- liveness

def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        try:
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return f'"{pid}"' in out or f",{pid}," in out or str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _process_group_options() -> dict:
    """Start provider children in their own terminable process group."""
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _worker_environment(workspace: Path) -> dict[str, str]:
    """Build a bounded provider environment with no inherited Git context."""
    environment = dict(os.environ)
    environment.pop("COPILOT_ALLOW_ALL", None)
    for variable in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
    ):
        environment.pop(variable, None)
    environment.update({
        "COPILOT_AUTO_UPDATE": "false",
        "GITHUB_COPILOT_PROMPT_MODE_EXTENSIONS": "false",
        "GITHUB_COPILOT_PROMPT_MODE_REPO_HOOKS": "false",
        "GITHUB_COPILOT_PROMPT_MODE_WORKSPACE_MCP": "false",
        # The provider workspace lives under the source repository's private
        # runtime directory. Its own Git metadata is detached, so stop any
        # incidental Git discovery before it reaches the parent source repo.
        "GIT_CEILING_DIRECTORIES": str(workspace.parent),
        "KIT_HOST_FINALIZES": "1",
    })
    return environment


def _terminate_process_tree(proc: subprocess.Popen) -> None:
    """Stop only a provider process tree that this board instance created."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, timeout=15, check=False,
            )
        else:
            os.killpg(proc.pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.terminate()
        except OSError:
            pass


def _wait_bounded(proc: subprocess.Popen, timeout_seconds: float) -> tuple[int, bool]:
    """Wait up to the configured quota window, then stop the owned tree."""
    try:
        return int(proc.wait(timeout=max(0.1, timeout_seconds))), False
    except subprocess.TimeoutExpired:
        _terminate_process_tree(proc)
        try:
            return int(proc.wait(timeout=15)), True
        except (subprocess.TimeoutExpired, OSError):
            return -9, True


def _probe(port: int, timeout: float = 1.0) -> bool:
    """True only if something on this port answers /api/health as a board.

    A recorded port is not evidence: the port may have been reused by an
    unrelated process, and the old pid may have been recycled. The probe
    therefore checks the payload, not merely that a socket accepted.
    """
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=timeout) as r:
            if r.status != 200:
                return False
            payload = json.loads(r.read().decode("utf-8"))
    except Exception:
        return False
    return bool(
        isinstance(payload, dict)
        and payload.get("ok")
        and payload.get("schema") == SCHEMA
        and payload.get("version") == BOARD_VERSION
    )


def board_health(state: dict | None = None) -> dict:
    """Is the recorded board usable? pid alive **and** port answering.

    `board.state.json` existing proves only that a board once ran. Finding 5
    of the review is exactly this: a page kept posting to a recorded port with
    no listener behind it. Both halves are checked, and which half failed is
    reported, so a dead board is replaced rather than trusted.
    """
    state = load_state() if state is None else state
    port, pid = state.get("port"), state.get("pid")
    pid_ok = _pid_alive(pid)
    port_ok = bool(port) and _probe(int(port))
    if not port:
        detail = "no port recorded"
    elif not pid_ok and not port_ok:
        detail = f"pid {pid} is gone and port {port} does not answer"
    elif not pid_ok:
        detail = f"port {port} answers but recorded pid {pid} is gone"
    elif not port_ok:
        detail = f"pid {pid} is alive but port {port} does not answer"
    else:
        detail = ""
    return {
        "alive": bool(pid_ok and port_ok),
        "pid_alive": pid_ok,
        "port_alive": port_ok,
        "port": port,
        "pid": pid,
        "started": state.get("started"),
        "url": f"http://127.0.0.1:{port}/" if port else None,
        "detail": detail,
    }


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------- lifecycle

def ensure_running() -> str | None:
    """Start the explicitly requested board, replacing stale recorded state.

    Guarded by a short-lived lockfile: the check ("is the recorded pid alive
    and does the port answer") and the spawn are not atomic otherwise. Page
    generation never calls this function; `kit serve` is the explicit lifecycle
    boundary. The lock is only held while that request probes and starts, and a
    lock older than 10s is treated as residue from an interrupted start.
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    got_lock = False
    for _ in range(50):
        try:
            fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            got_lock = True
            break
        except FileExistsError:
            try:
                if time.time() - LOCK_FILE.stat().st_mtime > 10:
                    LOCK_FILE.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            time.sleep(0.1)
    if not got_lock:
        return None
    try:
        state = load_state()
        health = board_health(state)
        if health["alive"]:
            return health["url"]
        # Recorded but not usable: replaced, never trusted. Which half failed
        # is left in the log so a wedged board is diagnosable after the fact.
        if health["detail"]:
            print(f"{_now_iso()} board: restarting -- {health['detail']}", flush=True)

        port = _free_port()
        BOARD_LOG.parent.mkdir(parents=True, exist_ok=True)
        server_log = BOARD_LOG
        popen_kwargs: dict = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
            )
        else:
            popen_kwargs["start_new_session"] = True
        with open(server_log, "ab") as lf:
            proc = subprocess.Popen(
                [sys.executable, str(Path(__file__).resolve()), "--serve", "--port", str(port)],
                cwd=str(ROOT), stdout=lf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, **popen_kwargs,
            )
        ready = False
        for _ in range(50):
            if _probe(port):
                ready = True
                break
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        if not ready:
            _terminate_process_tree(proc)
            try:
                proc.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return None
        state["port"] = port
        state["pid"] = proc.pid
        state["started"] = _now_iso()
        state["version"] = BOARD_VERSION
        state.setdefault("runs", [])
        save_state(state)
        return f"http://127.0.0.1:{port}/"
    except OSError:
        return None
    finally:
        if got_lock:
            try:
                LOCK_FILE.unlink()
            except OSError:
                pass


# --------------------------------------------------------------- decisions
#
# Same two files retro_html.py already established. Duplicated here (not
# imported from retro_html, even though it is already imported above for the
# lazy-cycle reason in its own header) only for accepted.json, because these
# are six-line wrappers around one json file each and keeping them next to
# the API handler that calls them is clearer than a cross-module hop for
# something this small; the *shape* of the file -- what a decision record
# contains -- is defined once, in retro_html.py's docstring, and both places
# agree with it.

ACCEPTED_FILE = RETRO_DIR / "accepted.json"


def migrate_accepted(entries: list[dict]) -> list[dict]:
    """Give decisions written before decision 3/4 a state the page can show.

    The pre-comment pipeline wrote `{finding, date, by, reason}` and nothing
    else. The old pipeline treated acceptance as dispatch, so an old entry has
    no `slug`, no `comment` and no run: `finding_states` fell back to `queued`,
    and a human opening the page saw a phantom queued item that no worker was
    ever going to pick up.

    A legacy entry is therefore normalised to `awaiting_review` with an empty
    comment -- honest, because the amended proposal that decision 3 requires
    was never written for it, and it must be approved again to dispatch. The
    original `reason` and `by` are kept: the record of the old decision is not
    discarded, only its implied dispatch state is corrected.

    Pure and idempotent; `migrate_accepted_file` is the writing half.
    """
    out: list[dict] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        if "slug" in e:            # written by this pipeline; leave it alone
            out.append(e)
            continue
        e = dict(e)
        e["slug"] = retro_queue.slug_for(e.get("finding", ""))
        e.setdefault("comment", "")
        e.setdefault("state", "awaiting_review")
        e.setdefault("updated_at", e.get("date", ""))
        e.setdefault("migrated_from", "pre-comment pipeline")
        out.append(e)
    return out


def load_accepted() -> list[dict]:
    with _decision_file_lock:
        if not ACCEPTED_FILE.exists():
            return []
        try:
            raw = json.loads(ACCEPTED_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
    # Normalised on every read, not only by the CLI: a board pointed at an
    # un-migrated file must still not show a phantom queued item.
    return migrate_accepted(raw if isinstance(raw, list) else [])


def migrate_accepted_file() -> int:
    """Rewrite accepted.json in place. Returns the number of entries changed."""
    if not ACCEPTED_FILE.exists():
        return 0
    try:
        raw = json.loads(ACCEPTED_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    raw = raw if isinstance(raw, list) else []
    migrated = migrate_accepted(raw)
    if migrated == raw:
        return 0
    save_accepted(migrated)
    return sum(1 for a, b in zip(raw, migrated) if a != b)


def save_accepted(entries: list[dict]) -> None:
    with _decision_file_lock:
        _atomic_json_write(ACCEPTED_FILE, entries)


def record_decision(finding: str, action: str, by: str, reason: str,
                    comment: str = "", slug: str = "",
                    metadata: dict | None = None) -> tuple[bool, str]:
    finding = (finding or "").strip()
    action = (action or "").strip().lower()
    if not finding:
        return False, "finding is required"
    if action not in ("accept", "defer"):
        return False, "action must be accept or defer"
    key = retro_rank.normalise_title(finding)
    entry = {"finding": finding, "date": date.today().isoformat(),
              "by": (by or "").strip(), "reason": (reason or "").strip()}
    entry.update(metadata or {})
    if action == "accept":
        # The comment is the amended proposal the human approved. It is
        # written before any optional spawn, so a board killed mid-dispatch --
        # or a manual provider that never dispatches -- still leaves the exact
        # durable decision.
        entry["comment"] = (comment or "").strip()
        entry["slug"] = slug or retro_queue.slug_for(finding)
        entry["updated_at"] = _now_iso()
        with _decision_file_lock:
            entries = [e for e in load_accepted()
                       if retro_rank.normalise_title(e.get("finding", "")) != key]
            entries.append(entry)
            save_accepted(entries)
    else:
        entries = [e for e in retro_rank.load_deferred() if retro_rank.normalise_title(e.get("finding", "")) != key]
        entries.append(entry)
        retro_rank.save_deferred(entries)
    return True, ""


def _accepted_entry(slug: str) -> dict | None:
    for e in load_accepted():
        if (e.get("slug") or retro_queue.slug_for(e.get("finding", ""))) == slug:
            return e
    return None


def annotate_accepted(slug: str, **fields) -> None:
    """Attach dispatch bookkeeping (run_id, state) to a stored decision."""
    with _decision_file_lock:
        entries = load_accepted()
        for e in entries:
            if (e.get("slug") or retro_queue.slug_for(e.get("finding", ""))) == slug:
                e.update(fields)
                e["updated_at"] = _now_iso()
                save_accepted(entries)
                return


def _regenerate(script: str) -> None:
    """Re-run the exact CLI a human would, so what gets served is what
    the tool actually produces -- never a second, in-process rendering path
    that could drift from it."""
    try:
        subprocess.run(
            [sys.executable, str(ROOT / "tools" / script)],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        pass


# --------------------------------------------------------------- dispatch

# The kit-builder restrictions (approved kit docs only, never game/design state)
# now
# live with the artifact that carries them, so the stored prompt is the whole
# prompt. Re-exported under the old name: it is the documented constant.
DISPATCH_HEADER = retro_queue.DISPATCH_HEADER


def pending_finding_titles() -> list[str]:
    """Titles of accepted, unresolved findings, in the order they appear
    across docs/retro/*-findings.md. Used by the single approval path and any
    view that needs "what is accepted but unresolved" without a second reader
    of accepted.json.
    """
    accepted = load_accepted()
    pending = {
        retro_rank.normalise_title(e["finding"])
        for e in accepted
        if not e.get("resolved_at")
    }
    if not pending:
        return []
    titles: list[str] = []
    for path in sorted(RETRO_DIR.glob("*-findings.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        _, findings = retro_rank.parse_findings(text)
        for f in findings:
            if retro_rank.normalise_title(f["title"]) in pending:
                titles.append(f["title"])
    return titles


def build_finding_item(title: str, comment: str = "") -> dict:
    """The dispatch-ready artifact for one finding, plus its own health.

    Findings are dispatched one at a time, in a fresh session each, rather
    than bundled into a single run: two findings can name the same
    fix_files (a real merge conflict waiting to happen if run concurrently),
    and a single session carries the first implementation's reasoning into
    the second rather than each being shaped by its own proposal.

    This reads the private runtime queue artifact and nothing else. It never opens
    a session log: if the artifact is missing or stale, that is reported as a
    structured fact for the caller to show, because a prompt silently rebuilt
    from logs that have moved since the human approved it is not the prompt
    they approved.
    """
    slug = retro_queue.slug_for(title)
    item = retro_queue.load_item(slug)
    if item is None:
        return {
            "finding": title, "slug": slug, "prompt": "", "ok": False,
            "stale": False, "missing": True,
            "dispatchable": False,
            "dispatch_blockers": ["artifact: dispatch artifact is missing"],
            "evidence_snapshot": "",
            "error": (f"no dispatch artifact for {slug!r}. "
                      "Run `kit retro publish` to build it."),
        }
    stale = retro_queue.is_stale(item)
    dispatchable, blockers = retro_queue.dispatch_eligibility(item)
    if stale:
        error = (f"dispatch artifact for {slug!r} is stale: "
                 f"{item.get('source_file', '')} changed since it was written. "
                 "Re-rank before dispatching.")
    elif not dispatchable:
        error = (f"dispatch artifact for {slug!r} is not eligible: "
                 + "; ".join(blockers))
    else:
        error = ""
    return {
        "finding": title,
        "slug": slug,
        "prompt": retro_queue.render_prompt(item, comment),
        "ok": not stale and dispatchable,
        "stale": stale,
        "missing": False,
        "dispatchable": dispatchable,
        "dispatch_blockers": blockers,
        "evidence_snapshot": item.get("evidence_snapshot", ""),
        "generated_at": item.get("generated_at", ""),
        "source_file": item.get("source_file", ""),
        "error": error,
    }


def build_finding_prompt(title: str, comment: str = "") -> str:
    """The exact prompt for one finding, or "" when it cannot be produced.

    Kept for callers that want only the text; `build_finding_item` carries the
    reason the text is empty.
    """
    return build_finding_item(title, comment)["prompt"]


def build_dispatch_queue() -> list[dict]:
    """One artifact per pending finding, in order -- the queue the board runs.

    A pure filesystem read: zero session logs are opened, so this returns in
    milliseconds however many findings are accepted.
    """
    return [build_finding_item(t) for t in pending_finding_titles()]


class RunManager:
    """Owns every kit-builder process this server instance has spawned.

    Runs are tracked in memory (the `subprocess.Popen` handle a watcher
    thread calls `.wait()` on) and mirrored into `board.state.json` so the
    status API can be served from either. If this server process itself is
    killed mid-run, the in-memory handle and its watcher thread go with it;
    a restarted board can only report the pid-liveness of an orphaned run,
    not its eventual exit code. That is a known limitation, not silently
    papered over -- see `describe_run`.
    """

    def __init__(self) -> None:
        self._procs: dict[str, subprocess.Popen] = {}
        self._lock = threading.Lock()
        # Every item ever enqueued in this server's lifetime, in approval
        # order. `_queue_pos` is the item being run; items before it are
        # finished. There is exactly one of these lists, which is what makes
        # approvals sequential -- a second approval lands behind the first
        # rather than starting a concurrent kit-builder on the same fix_files.
        self._queue: list[dict] = []   # [{"finding", "slug", "prompt", "decision_id"}, ...]
        self._queue_pos = 0
        self._active: str | None = None    # run_id of the in-flight finding
        self._halted = False               # a failed item stops the queue
        self._retro_run: str | None = None
        self._recovered = False

    @staticmethod
    def _new_run_id(prefix: str = "run") -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{prefix}-{stamp}-{secrets.token_hex(4)}"

    def spawn(self, prompt: str, finding: str | None = None,
              queue_position: int | None = None, queue_total: int | None = None,
              slug: str | None = None, run_id: str | None = None,
              decision_id: str | None = None,
              requested_files: object = None,
              provider_spec: providers.ProviderSpec | None = None) -> dict:
        """Start one already-reserved worker.

        Reservation and prompt persistence happen in :meth:`enqueue`; this
        method is deliberately safe to run on a background thread so session
        discovery and process startup never hold an HTTP request open.
        """
        run_id = run_id or self._new_run_id()
        try:
            spec = provider_spec or providers.selection(ROOT, "worker")
            blockers = providers.preflight(spec)
            if blockers:
                return {"error": "worker provider is unavailable: " + "; ".join(blockers),
                        "run_id": run_id}
            cmd = providers.worker_command(spec)
        except (providers.ProviderConfigError, runtime_paths.RuntimeConfigError) as exc:
            return {"error": f"worker provider is invalid: {exc}",
                    "run_id": run_id}

        prompt_path = RUNS_DIR / f"{run_id}.prompt.md"
        try:
            if prompt_path.read_bytes() != prompt.encode("utf-8"):
                return {
                    "error": "reserved prompt bytes changed before worker startup",
                    "run_id": run_id,
                }
        except (OSError, UnicodeError) as exc:
            return {"error": f"reserved prompt is unavailable: {exc}", "run_id": run_id}
        log_path = RUNS_DIR / f"{run_id}.log"
        before_sha = run_result.git_head(ROOT)
        try:
            workspace = run_result.prepare_workspace(
                ROOT, run_id, before_sha, DISPATCH_LEDGER_CHANGES,
                requested_files=requested_files,
            )
            result_path = run_result.workspace_result_path(workspace, run_id)
        except (OSError, run_result.DispatchWorkspaceError,
                runtime_paths.RuntimeConfigError) as exc:
            return {"error": f"isolated worker workspace is unavailable: {exc}",
                    "run_id": run_id}

        discover_fast = getattr(session_digest, "discover_copilot", session_digest.discover)
        pre_existing = {s["id"] for s in discover_fast(workspace)}
        env = _worker_environment(workspace)
        env["KIT_RUN_ID"] = run_id
        workspace_relative = str(workspace.relative_to(ROOT)).replace("\\", "/")
        result_relative = str(result_path.relative_to(ROOT)).replace("\\", "/")
        self._update_run(
            run_id,
            before_sha=before_sha,
            workspace=workspace_relative,
            result_file=result_relative,
            provider=spec.status(),
            persona=spec.persona,
            model=spec.model,
            timeout_seconds=spec.timeout_minutes * 60,
            requested_files=requested_files,
        )
        lf = open(log_path, "ab")
        prompt_input = open(prompt_path, "rb")
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(workspace), stdout=lf, stderr=subprocess.STDOUT,
                stdin=prompt_input, env=env, **_process_group_options(),
            )
        except OSError as exc:
            lf.close()
            prompt_input.close()
            return {"error": f"could not start worker provider: {exc}", "run_id": run_id}
        finally:
            # The child holds its own duplicated handles. Keeping either one
            # open would pin private run files for the life of the board on
            # Windows.
            if not lf.closed:
                lf.close()
            if not prompt_input.closed:
                prompt_input.close()

        started_t = time.time()
        started_at = _now_iso()
        self._update_run(
            run_id, started=started_at, started_at=started_at, t0=started_t,
            before_sha=before_sha, pid=proc.pid, status="running",
            workspace=workspace_relative, result_file=result_relative,
            provider=spec.status(), persona=spec.persona, model=spec.model,
            timeout_seconds=spec.timeout_minutes * 60,
            requested_files=requested_files,
        )
        with self._lock:
            self._procs[run_id] = proc

        threading.Thread(
            target=self._watch,
            args=(run_id, proc, pre_existing, spec, workspace),
            daemon=True,
        ).start()
        return next((r for r in load_state().get("runs", [])
                     if r.get("run_id") == run_id), {
                         "run_id": run_id, "status": "running", "pid": proc.pid,
                         "finding": finding, "slug": slug,
                     })

    def enqueue(self, item: dict, retry_of: str = "") -> dict:
        """Queue one already-approved automatic run behind any active worker.

        The approval handler reaches this only after provider, artifact and
        repository readiness are bound to the durable decision. Two findings
        can name the same ``fix_files``, so concurrent kit-builders remain a
        merge conflict waiting to happen.
        """
        provider_spec = item.get("_provider_spec")
        if not isinstance(provider_spec, providers.ProviderSpec):
            return {"error": "dispatch has no validated worker provider"}
        if provider_spec.role != "worker" or not provider_spec.automatic:
            return {"error": "dispatch worker provider is not automatic"}

        with self._lock:
            decision_id = item.get("decision_id")
            duplicate = next((queued for queued in self._queue
                              if decision_id and queued.get("decision_id") == decision_id), None)
            if duplicate is not None:
                index = self._queue.index(duplicate)
                run_id = duplicate.get("_run_id")
                return {
                    "run_id": run_id,
                    "kind": "finding",
                    "finding": duplicate.get("finding"),
                    "slug": duplicate.get("slug"),
                    "status": duplicate.get("_status", "queued"),
                    "queue_position": index + 1,
                    "queue_total": len(self._queue),
                    "queue_halted": self._halted,
                    "idempotent": True,
                }
            if decision_id:
                persisted = next((r for r in reversed(load_state().get("runs", []))
                                  if r.get("decision_id") == decision_id), None)
                if persisted is not None:
                    return {**persisted, "idempotent": True,
                            "queue_halted": self._halted}

            RUNS_DIR.mkdir(parents=True, exist_ok=True)
            run_id = str(item.get("_run_id") or self._new_run_id())
            if run_result.RUN_ID.fullmatch(run_id) is None:
                return {"error": "dispatch reservation has an invalid run id"}
            prompt_path = RUNS_DIR / f"{run_id}.prompt.md"
            result_path = RUNS_DIR / f"{run_id}.result.json"
            with prompt_path.open("w", encoding="utf-8", newline="\n") as output:
                output.write(item["prompt"])
            item["_run_id"] = run_id
            item["_status"] = "queued"
            item["_retry_of"] = retry_of or None
            if retry_of:
                retried = next((r for r in load_state().get("runs", [])
                                if r.get("run_id") == retry_of), None)
                if retried is None or retried.get("status") not in (
                        "blocked", "unverified", "failed"):
                    return {"error": "retry_of must name a blocked, unverified, or failed run"}
                insert_at = next((n for n, queued in enumerate(self._queue)
                                  if queued.get("_status") == "queued"), len(self._queue))
                self._queue.insert(insert_at, item)
                self._halted = False
            else:
                self._queue.append(item)
            total = len(self._queue)
            position = self._queue.index(item) + 1
            idle = self._active is None
            halted = self._halted
            requested_at = _now_iso()
            entry = {
                "run_id": run_id,
                "kind": "finding",
                "provider": provider_spec.status(),
                "persona": provider_spec.persona,
                "model": provider_spec.model,
                "finding": item.get("finding"),
                "slug": item.get("slug"),
                "decision_id": decision_id,
                "retry_of": retry_of or None,
                "queue_position": position,
                "queue_total": total,
                "requested_at": requested_at,
                "started": None,
                "started_at": None,
                "t0": None,
                "log": str((RUNS_DIR / f"{run_id}.log").relative_to(ROOT)).replace("\\", "/"),
                "prompt_file": str(prompt_path.relative_to(ROOT)).replace("\\", "/"),
                "result_file": str(result_path.relative_to(ROOT)).replace("\\", "/"),
                "workspace": None,
                "requested_files": item.get("fix_files"),
                "before_sha": None,
                "pid": None,
                "status": "queued",
                "session_id": None,
                "resume_cmd": None,
                "exit_code": None,
                "finished_at": None,
                "finished_t": None,
                "duration_s": None,
                "outcome": None,
            }
            mutate_state(lambda state: state.setdefault("runs", []).append(entry))
        if idle and not halted:
            entry = self._advance_queue()
            if entry and "error" not in entry:
                return entry
            if entry and "error" in entry:
                return entry
        return {
            "run_id": run_id,
            "kind": "finding",
            "finding": item.get("finding"),
            "slug": item.get("slug"),
            "status": "queued",
            "queue_position": position,
            "queue_total": total,
            "queue_halted": halted,
        }

    def queue_view(self) -> list[dict]:
        with self._lock:
            return [
                {"finding": i.get("finding"), "slug": i.get("slug"),
                 "run_id": i.get("_run_id"),
                 "status": i.get("_status", "queued"),
                 "position": n + 1, "total": len(self._queue)}
                for n, i in enumerate(self._queue)
            ]

    def halted(self) -> bool:
        with self._lock:
            return self._halted

    def owns(self, run_id: str | None) -> bool:
        """Whether this process has a watcher responsible for the attempt."""
        with self._lock:
            return bool(run_id and (run_id in self._procs or self._active == run_id))

    def recover(self) -> None:
        """Rebuild the in-memory queue from the durable run ledger once.

        A restarted board never re-spawns a starting/running attempt. It
        monitors the recorded PID when possible and then verifies the same
        result artifact. Queued attempts retain their original prompt bytes
        and resume only when there is no unresolved non-success outcome.
        """
        with self._lock:
            if self._recovered:
                return
            self._recovered = True
        runs = [r for r in load_state().get("runs", [])
                if r.get("kind", "finding") == "finding"]
        queued_runs = [r for r in runs if r.get("status") == "queued"]
        live_runs = [r for r in runs if r.get("status") in LIVE_RUN_STATES
                     and r.get("status") != "queued"]

        def item_from(run: dict) -> dict | None:
            relative = str(run.get("prompt_file") or "")
            if not relative:
                return None
            try:
                path = (ROOT / relative).resolve()
                path.relative_to(RUNS_DIR.resolve())
                prompt = path.read_text(encoding="utf-8")
                provider_spec = providers.from_record("worker", run.get("provider"))
            except (OSError, UnicodeError, ValueError, providers.ProviderConfigError):
                return None
            return {
                "finding": run.get("finding"), "slug": run.get("slug"),
                "prompt": prompt, "decision_id": run.get("decision_id"),
                "fix_files": run.get("requested_files"),
                "_run_id": run.get("run_id"), "_status": run.get("status"),
                "_retry_of": run.get("retry_of"),
                "_provider_spec": provider_spec,
            }

        recovered: list[dict] = []
        for run in queued_runs:
            item = item_from(run)
            if item is None:
                self._mark_recovery_unverified(
                    run, "queued run has no readable immutable prompt or bound provider record"
                )
                continue
            recovered.append(item)

        active_item = item_from(live_runs[0]) if live_runs else None
        if live_runs and active_item is None:
            self._mark_recovery_unverified(
                live_runs[0],
                "in-flight run has no readable immutable prompt or bound provider record",
            )
            live_runs = live_runs[1:]
        elif active_item is not None:
            recovered.insert(0, active_item)

        extra_live_runs = live_runs[1:] if active_item is not None else live_runs
        for extra in extra_live_runs:
            self._mark_recovery_unverified(
                extra, "multiple in-flight runs were found during board recovery"
            )

        with self._lock:
            self._queue.extend(recovered)
            if active_item is not None:
                self._active = active_item["_run_id"]

        if active_item is not None:
            threading.Thread(
                target=self._watch_orphan, args=(active_item,), daemon=True
            ).start()
            return

        terminal = next((r for r in reversed(runs)
                         if r.get("status") in TERMINAL_RUN_STATES), None)
        first_queued = recovered[0] if recovered else None
        if (terminal and terminal.get("status") != "completed" and first_queued
                and first_queued.get("_retry_of") != terminal.get("run_id")):
            with self._lock:
                self._halted = True
            return
        if first_queued:
            self._advance_queue()

    def _mark_recovery_unverified(self, run: dict, reason: str) -> None:
        run_id = str(run.get("run_id") or "")
        if not run_id:
            return
        self._update_run(
            run_id, status="unverified", outcome="unverified",
            result_errors=[reason], result_summary=reason,
            finished_at=_now_iso(), duration_s=None,
        )
        if run.get("slug"):
            annotate_accepted(run["slug"], run_id=run_id, state="unverified")

    @staticmethod
    def _verify_and_integrate(run_id: str, stored: dict,
                              *, finalize_edits: bool = False) -> dict:
        """Host-finalize edits, verify them, then fast-forward safely."""
        workspace_value = str(stored.get("workspace") or "")
        result_value = str(stored.get("result_file") or "")
        try:
            workspace = (ROOT / workspace_value).resolve(strict=True)
            workspace.relative_to(_RUNTIME.dispatch_workspaces.resolve(strict=True))
            result_path = (ROOT / result_value).resolve(strict=False)
            result_path.relative_to(workspace)
        except (OSError, RuntimeError, ValueError):
            return {
                "status": "unverified",
                "outcome": "unverified",
                "summary": "worker has no trusted isolated workspace",
                "errors": ["worker workspace or result path is absent or escapes runtime"],
                "changed_files": [],
                "gate": None,
                "integration": None,
            }
        baseline = str(stored.get("before_sha") or "")
        if finalize_edits:
            try:
                run_result.finalize_workspace(
                    result_path,
                    run_id,
                    baseline,
                    workspace,
                    stored.get("requested_files"),
                    f"Implemented approved finding: {stored.get('finding') or run_id}",
                )
            except (OSError, run_result.DispatchWorkspaceError,
                    runtime_paths.RuntimeConfigError) as exc:
                return {
                    "status": "unverified",
                    "outcome": "unverified",
                    "summary": "dispatcher could not finalize provider edits",
                    "errors": [str(exc)],
                    "changed_files": [],
                    "gate": None,
                    "integration": None,
                }
        evaluation = run_result.evaluate(
            result_path,
            run_id,
            baseline,
            workspace,
            requested_files=stored.get("requested_files"),
            trusted_host_result=True,
        )
        evaluation["integration"] = None
        if evaluation.get("status") != "completed":
            return evaluation
        after_sha = run_result.git_head(workspace)
        integration = run_result.integrate_workspace(
            ROOT,
            workspace,
            baseline,
            after_sha,
            DISPATCH_LEDGER_CHANGES,
        )
        evaluation["integration"] = integration
        if not integration.get("integrated"):
            evaluation["status"] = "blocked"
            evaluation["errors"].extend(integration.get("errors", []))
            evaluation["summary"] = (
                evaluation.get("summary", "")
                + "; implementation verified in isolation but was not integrated"
            ).strip("; ")
        return evaluation

    def _watch_orphan(self, item: dict) -> None:
        run_id = item["_run_id"]
        stored = next((r for r in load_state().get("runs", [])
                       if r.get("run_id") == run_id), {})
        timeout_seconds = float(stored.get("timeout_seconds") or 1800)
        deadline = float(stored.get("t0") or time.time()) + timeout_seconds
        timed_out = False
        while _pid_alive(stored.get("pid")):
            if time.time() >= deadline:
                timed_out = True
                break
            time.sleep(1)
        if timed_out:
            evaluation = {
                "status": "failed", "outcome": "failed",
                "summary": "orphaned worker exceeded its configured timeout",
                "errors": [
                    "board restart lost process ownership; the stale PID was not killed automatically"
                ],
                "changed_files": [], "gate": None, "integration": None,
            }
        else:
            evaluation = self._verify_and_integrate(
                run_id, stored, finalize_edits=True
            )
        terminal = evaluation["status"]
        finished_t = time.time()
        self._update_run(
            run_id, status=terminal, exit_code=None, timed_out=timed_out,
            finished_at=_now_iso(),
            finished_t=finished_t,
            duration_s=(round(max(0.0, finished_t - float(stored["t0"])), 3)
                        if stored.get("t0") is not None else None),
            outcome=evaluation.get("outcome"),
            result_summary=evaluation.get("summary", ""),
            result_errors=evaluation.get("errors", []),
            changed_files=evaluation.get("changed_files", []),
            verification=evaluation.get("gate"),
            integration=evaluation.get("integration"),
        )
        with self._lock:
            item["_status"] = terminal
            if self._active == run_id:
                self._active = None
            if terminal != "completed":
                self._halted = True
        if item.get("slug"):
            annotate_accepted(
                item["slug"], run_id=run_id,
                state="done" if terminal == "completed" else terminal,
            )
        if terminal == "completed":
            self._advance_queue()
        _regenerate("retro_html.py")
        _regenerate("plan_html.py")

    def _advance_queue(self) -> dict:
        with self._lock:
            if self._halted or self._active is not None:
                return {}
            index = next((n for n, i in enumerate(self._queue)
                          if i.get("_status") == "queued"), None)
            if index is None:
                return {}
            self._queue_pos = index
            item = self._queue[index]
            total = len(self._queue)
            # Reserve before session discovery or Popen. A retry arriving
            # during those slow operations sees one durable attempt instead
            # of spawning the same finding concurrently.
            run_id = item["_run_id"]
            item["_status"] = "starting"
            self._active = run_id
        self._update_run(run_id, status="starting")
        if item.get("slug"):
            annotate_accepted(item["slug"], run_id=run_id, state="working")
        threading.Thread(
            target=self._start_reserved,
            args=(item, index + 1, total),
            daemon=True,
        ).start()
        return {
            "run_id": run_id,
            "kind": "finding",
            "finding": item.get("finding"),
            "slug": item.get("slug"),
            "status": "starting",
            "queue_position": index + 1,
            "queue_total": total,
        }

    def _start_reserved(self, item: dict, position: int, total: int) -> None:
        run_id = item["_run_id"]
        entry = self.spawn(
            item["prompt"], finding=item["finding"],
            queue_position=position, queue_total=total,
            slug=item.get("slug"), run_id=run_id,
            decision_id=item.get("decision_id"),
            requested_files=item.get("fix_files"),
            provider_spec=item.get("_provider_spec"),
        )
        if "error" not in entry:
            with self._lock:
                item["_status"] = "running"
            return
        finished_t = time.time()
        self._update_run(
            run_id, status="failed", outcome="failed", exit_code=None,
            finished_at=_now_iso(), finished_t=finished_t, duration_s=None,
            result_errors=[entry["error"]], result_summary=entry["error"],
        )
        with self._lock:
            item["_status"] = "failed"
            if self._active == run_id:
                self._active = None
            self._halted = True
        if item.get("slug"):
            annotate_accepted(item["slug"], run_id=run_id, state="failed")

    def spawn_retro(self, provider_spec: providers.ProviderSpec) -> dict:
        """Start the retrospective model run -- decision 1, one conscious click.

        Out of process, because it takes minutes and the request thread must
        not block. `retro_due.py` stays inert: nothing there gained the
        ability to call a model, this did, and only when asked over HTTP.
        The provider and model selected from kit.config.json are bound to the
        run before startup, so quota is never spent at an unknown rate.
        """
        with self._lock:
            active = self._retro_run
        if active:
            state = load_state()
            for r in state.get("runs", []):
                if r.get("run_id") == active and r.get("status") == "running":
                    return {"error": f"a retrospective run is already in flight ({active})"}
        try:
            blockers = providers.preflight(provider_spec)
            if blockers:
                return {"error": "analyzer provider is unavailable: " + "; ".join(blockers)}
            if provider_spec.role != "analyzer" or provider_spec.kind != "copilot-sdk":
                return {"error": "configured analyzer provider cannot run automatically"}
        except providers.ProviderConfigError as exc:
            return {"error": f"analyzer provider is invalid: {exc}"}

        model = provider_spec.model
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        run_id = self._new_run_id("retro")
        log_path = RUNS_DIR / f"{run_id}.log"
        cmd = [sys.executable, str(ROOT / "tools" / "retro.py"), "--sdk", "--force"]
        env = dict(os.environ)
        env.pop("COPILOT_ALLOW_ALL", None)
        env["COPILOT_AUTO_UPDATE"] = "false"
        if model:
            env["RETRO_MODEL"] = model
        lf = open(log_path, "ab")
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(ROOT), stdout=lf, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, env=env, **_process_group_options(),
            )
        except OSError as exc:
            lf.close()
            return {"error": f"could not start the retrospective: {exc}"}
        finally:
            if not lf.closed:
                lf.close()
        entry = {
            "run_id": run_id,
            "kind": "retro",
            "provider": provider_spec.status(),
            "persona": "retrospective",
            "finding": None,
            "slug": None,
            "model": model,
            "started": _now_iso(),
            "t0": time.time(),
            "log": str(log_path.relative_to(ROOT)).replace("\\", "/"),
            "prompt_file": None,
            "pid": proc.pid,
            "status": "running",
            "session_id": None,
            "resume_cmd": None,
            "exit_code": None,
            "timeout_seconds": provider_spec.timeout_minutes * 60,
        }
        with self._lock:
            self._procs[run_id] = proc
            self._retro_run = run_id
        state = load_state()
        state.setdefault("runs", []).append(entry)
        save_state(state)
        threading.Thread(target=self._watch_retro, args=(run_id, proc), daemon=True).start()
        return entry

    def _watch_retro(self, run_id: str, proc: subprocess.Popen) -> None:
        stored = next((r for r in load_state().get("runs", [])
                       if r.get("run_id") == run_id), {})
        timeout_seconds = float(stored.get("timeout_seconds") or 1800)
        rc, timed_out = _wait_bounded(proc, timeout_seconds)
        finished_t = time.time()
        self._update_run(
            run_id, status="finished" if rc == 0 and not timed_out else "failed",
            exit_code=rc, timed_out=timed_out,
            result_summary=(f"analyzer exceeded {int(timeout_seconds // 60)} minute timeout"
                            if timed_out else ""),
            finished_at=_now_iso(), finished_t=finished_t,
            duration_s=(round(max(0.0, finished_t - float(stored["t0"])), 3)
                        if stored.get("t0") is not None else None),
        )
        with self._lock:
            self._procs.pop(run_id, None)
            if self._retro_run == run_id:
                self._retro_run = None
        # retro.py owns evidence -> report -> rank -> board rendering as one
        # transaction. The board only refreshes the plan surface afterwards;
        # it must not replay half the pipeline independently.
        _regenerate("plan_html.py")

    def _update_run(self, run_id: str, **fields) -> None:
        def update(state: dict) -> None:
            for run in state.get("runs", []):
                if run.get("run_id") == run_id:
                    run.update(fields)
                    break

        mutate_state(update)

    def _watch(self, run_id: str, proc: subprocess.Popen, pre_existing: set[str],
               provider_spec: providers.ProviderSpec, workspace: Path) -> None:
        # Resolve the CLI session id the same way session_digest.py already
        # discovers sessions for this repo, rather than re-parsing
        # workspace.yaml a second way: poll until a session directory shows
        # up that was not there before this run started.
        session_id = None
        stored_at_start = next((r for r in load_state().get("runs", [])
                                if r.get("run_id") == run_id), {})
        started_t = float(stored_at_start.get("t0") or time.time())
        timeout_seconds = float(
            stored_at_start.get("timeout_seconds")
            or provider_spec.timeout_minutes * 60
        )
        deadline = started_t + timeout_seconds
        for _ in range(90):  # ~3 minutes at 2s -- a fresh session directory
                              # appears within seconds of copilot starting up
            discover_fast = getattr(session_digest, "discover_copilot", session_digest.discover)
            found = discover_fast(workspace)
            new_ids = [s["id"] for s in found if s["id"] not in pre_existing]
            if new_ids:
                session_id = new_ids[-1]
                break
            if proc.poll() is not None:
                # one more look in case the session directory landed just
                # as the process exited
                found = discover_fast(workspace)
                new_ids = [s["id"] for s in found if s["id"] not in pre_existing]
                session_id = new_ids[-1] if new_ids else None
                break
            if time.time() >= deadline:
                break
            time.sleep(min(2.0, max(0.0, deadline - time.time())))
        if session_id:
            self._update_run(
                run_id, session_id=session_id,
                resume_cmd=providers.resume_command(provider_spec, session_id),
            )

        rc, timed_out = _wait_bounded(proc, max(0.1, deadline - time.time()))
        finished_t = time.time()
        finished_at = _now_iso()
        stored = next((r for r in load_state().get("runs", [])
                       if r.get("run_id") == run_id), {})
        if timed_out:
            evaluation = {
                "status": "failed", "outcome": "failed",
                "summary": f"worker exceeded {int(timeout_seconds // 60)} minute timeout",
                "errors": ["provider process tree was terminated after its configured timeout"],
                "changed_files": [], "gate": None, "integration": None,
            }
            terminal = "failed"
        elif rc == 0:
            self._update_run(run_id, status="verifying", exit_code=rc)
            evaluation = self._verify_and_integrate(
                run_id, stored, finalize_edits=True
            )
            terminal = evaluation["status"]
        else:
            evaluation = {"status": "failed", "outcome": "failed",
                          "summary": f"worker process exited {rc}",
                          "errors": [f"process exit code {rc}"],
                          "changed_files": [], "gate": None}
            terminal = "failed"
        self._update_run(
            run_id, status=terminal, exit_code=rc, timed_out=timed_out,
            finished_at=finished_at,
            finished_t=finished_t,
            duration_s=(round(max(0.0, finished_t - float(stored["t0"])), 3)
                        if stored.get("t0") is not None else None),
            outcome=evaluation.get("outcome"),
            result_summary=evaluation.get("summary", ""),
            result_errors=evaluation.get("errors", []),
            changed_files=evaluation.get("changed_files", []),
            verification=evaluation.get("gate"),
            integration=evaluation.get("integration"),
        )
        with self._lock:
            self._procs.pop(run_id, None)
            if self._active == run_id:
                self._active = None
            item = next((i for i in self._queue if i.get("_run_id") == run_id), None)
            if item is not None:
                item["_status"] = terminal
            if terminal != "completed" and item is not None:
                self._halted = True
        if item is not None and item.get("slug"):
            accepted_state = "done" if terminal == "completed" else terminal
            annotate_accepted(item["slug"], state=accepted_state, run_id=run_id)

        # A verified completion releases the next durable reservation before
        # expensive report regeneration. Report generation must not become a
        # hidden queue delay.
        if terminal == "completed":
            self._advance_queue()

        _regenerate("retro_html.py")
        _regenerate("plan_html.py")


def describe_run(entry: dict, silence_minutes: float) -> dict:
    """Decorate a stored run record with an honest status label.

    Exactly the four vocabulary forms this was specified with: running, no
    output Nm, finished, failed. Never "stuck" or "needs intervention" -- a
    `-p` invocation is non-interactive, so silence is silence, not a request
    for help the board has no way to see.
    """
    out = dict(entry)
    stored_status = str(entry.get("status") or "unverified")
    status = stored_status
    last_line = ""
    silent_for: float | None = None
    log_path = ROOT / entry["log"] if entry.get("log") else None
    if log_path and log_path.exists():
        try:
            lines = [ln for ln in log_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
            last_line = lines[-1] if lines else ""
        except OSError:
            pass
        try:
            silent_for = (time.time() - log_path.stat().st_mtime) / 60.0
        except OSError:
            silent_for = None

    if status == "queued":
        status_label = "queued"
    elif status in ("reserved", "starting"):
        status_label = "starting"
    elif status == "verifying":
        status_label = "verifying repository evidence"
    elif status == "running":
        alive = _pid_alive(entry.get("pid"))
        if not alive:
            manager = globals().get("_run_manager")
            if manager is not None and manager.owns(entry.get("run_id")):
                # The child can exit a few milliseconds before its watcher
                # records and verifies the result. Do not expose that normal
                # handoff window as a terminal unverified outcome.
                status_label = "worker exited; verifying result"
            else:
                # The watcher may have committed its terminal transition
                # between this API snapshot and the liveness probe. Prefer
                # that durable evidence over a stale in-memory copy.
                fresh = next((run for run in load_state().get("runs", [])
                              if run.get("run_id") == entry.get("run_id")), None)
                if fresh is not None and fresh.get("status") != stored_status:
                    return describe_run(fresh, silence_minutes)
                status = "unverified"
                status_label = "unverified -- board restarted before exit was observed"
        else:
            if silent_for is not None and silent_for > silence_minutes:
                status_label = f"no output {int(silent_for)}m"
            else:
                status_label = "running"
    elif status == "completed":
        status_label = "implemented and independently verified"
    elif status == "blocked":
        summary = str(entry.get("result_summary") or "worker reported a blocker")
        status_label = f"blocked -- {summary}"
    elif status == "unverified":
        errors = entry.get("result_errors")
        reason = (str(errors[0]) if isinstance(errors, list) and errors
                  else "completion evidence is missing or invalid")
        status_label = f"unverified -- {reason}"
    elif status == "failed":
        exit_code = entry.get("exit_code")
        if entry.get("timed_out"):
            minutes = int(float(entry.get("timeout_seconds") or 0) // 60)
            status_label = f"failed -- exceeded {minutes or '?'} minute timeout"
        else:
            status_label = (f"failed (exit {exit_code})" if exit_code is not None
                            else "failed before the worker started")
    elif status == "finished" and entry.get("kind") == "retro":
        status_label = "finished"
    else:
        status = "unverified"
        status_label = f"unverified -- unknown stored status {stored_status!r}"

    out["stored_status"] = stored_status
    out["status"] = status
    out["status_label"] = status_label
    out["silence_minutes"] = round(silent_for, 1) if silent_for is not None else 0.0
    if stored_status in LIVE_RUN_STATES and entry.get("t0") is not None:
        out["elapsed_s"] = max(0.0, time.time() - float(entry["t0"]))
    elif entry.get("duration_s") is not None:
        out["elapsed_s"] = max(0.0, float(entry["duration_s"]))
    else:
        out["elapsed_s"] = None
    out["last_line"] = last_line[:200]
    return out


# --------------------------------------------------------------- API model
#
# Each function below returns `(http_status, payload)`. They are the whole API
# surface: the Handler only routes and serialises, so every one of these is
# callable from a test without a socket, and every failure is a code plus a
# message worth showing rather than a traceback.

_run_manager = RunManager()


def _silence_minutes() -> float:
    try:
        value = runtime_paths.load_config(ROOT).get(
            "board_silence_minutes", SILENCE_MINUTES_DEFAULT
        )
        if isinstance(value, bool):
            raise ValueError("boolean is not a duration")
        minutes = float(value)
        if not 0.1 <= minutes <= 1440.0:
            raise ValueError("duration is outside the supported range")
        return minutes
    except (runtime_paths.RuntimeConfigError, TypeError, ValueError):
        return SILENCE_MINUTES_DEFAULT


def _provider_view(role: str) -> dict:
    """Return configuration state without probing executables on every poll."""
    try:
        spec = providers.selection(ROOT, role)
    except (providers.ProviderConfigError, runtime_paths.RuntimeConfigError) as exc:
        return {
            "role": role,
            "kind": "invalid",
            "automatic": False,
            "ready": False,
            "blockers": [str(exc)],
            "preflight": "invalid",
        }
    blockers = providers.preflight(spec) if not spec.automatic else []
    return {
        **spec.status(),
        "ready": False if blockers else None,
        "blockers": blockers,
        "preflight": "blocked" if blockers else "required_on_action",
    }


def _error(code: int, message: str, kind: str = "error", **extra) -> tuple[int, dict]:
    payload = {"ok": False, "error": message, "code": kind}
    payload.update(extra)
    return code, payload


def api_health() -> tuple[int, dict]:
    state = load_state()
    return 200, {"ok": True, "port": state.get("port"), "pid": os.getpid(),
                 "started": state.get("started"), "schema": SCHEMA,
                 "version": BOARD_VERSION}


def _run_index(runs: list[dict]) -> dict[str, dict]:
    return {r["run_id"]: r for r in runs if r.get("run_id")}


def finding_states(silence: float | None = None) -> list[dict]:
    """Every finding with a dispatch artifact, plus the state it is in.

    The point of `status_detail` is legibility of a wedged run: a `working`
    item carries how long its worker has been silent, so an item that has said
    `working` for an hour visibly is not progressing and the human knows to
    open the chat with `resume_cmd` rather than wait longer.
    """
    silence = _silence_minutes() if silence is None else silence
    runs = [describe_run(r, silence) for r in load_state().get("runs", [])]
    by_id = _run_index(runs)
    accepted = {}
    for e in load_accepted():
        accepted[e.get("slug") or retro_queue.slug_for(e.get("finding", ""))] = e
    deferred = {retro_queue.slug_for(e.get("finding", "")): e for e in retro_rank.load_deferred()}
    queued = [q for q in _run_manager.queue_view()
              if q.get("slug") and q.get("status") == "queued"]
    queued_slugs = {q["slug"]: n + 1 for n, q in enumerate(queued)}
    halted = _run_manager.halted()

    out: list[dict] = []
    for slug in retro_queue.load_index().get("items", []):
        item = retro_queue.load_item(slug)
        if item is None:
            continue
        dispatchable, dispatch_blockers = retro_queue.dispatch_eligibility(item)
        acc, dfr = accepted.get(slug), deferred.get(slug)
        run = by_id.get(acc.get("run_id")) if acc and acc.get("run_id") else None
        state = "awaiting_review"
        detail = ""
        if dfr and not acc:
            state = "deferred"
            detail = dfr.get("reason", "")
        elif acc:
            if run is None:
                position = queued_slugs.get(slug)
                if position:
                    state = "queued"
                    detail = (f"queued behind {position} run(s)" if position >= 1
                              else "queued, starting")
                else:
                    stored = acc.get("state")
                    if acc.get("migrated_from"):
                        # An acceptance from the pre-comment pipeline. It was
                        # never dispatched and carries no amended proposal, so
                        # it is waiting for a real approval, not for a worker.
                        detail = ("accepted by the pre-comment pipeline; approve "
                                  "again with a comment to dispatch it")
                        state = "awaiting_review"
                    elif stored in ("blocked", "unverified", "failed"):
                        state = stored
                        detail = f"stored {stored} decision has no linked run evidence"
                    elif stored == "approved":
                        state = "approved"
                        blockers = acc.get("dispatch_blockers")
                        if isinstance(blockers, list) and blockers:
                            detail = "accepted; implementation not dispatched -- " + "; ".join(
                                str(value) for value in blockers
                            )
                        else:
                            detail = "accepted; implementation has not been dispatched"
                    else:
                        state = "unverified"
                        detail = "accepted decision has no linked durable run evidence"
                if halted:
                    detail += " -- the queue is halted after a non-success outcome"
            elif run.get("status") in LIVE_RUN_STATES:
                state = "queued" if run.get("status") == "queued" else "working"
                silent = run.get("silence_minutes") or 0.0
                if state == "queued":
                    detail = "queued behind the active verified-dispatch attempt"
                else:
                    detail = (f"working, no output for {int(silent)}m"
                              if silent > silence else run.get("status_label", "working"))
            elif run.get("status") == "failed":
                state = "failed"
                detail = run.get("status_label", "worker failed")
            elif run.get("status") == "completed":
                state = "done"
                detail = run.get("status_label", "implemented and verified")
            elif run.get("status") in ("blocked", "unverified"):
                state = run["status"]
                detail = run.get("status_label", run["status"])
            else:
                state = "unverified"
                detail = run.get("status_label", "run outcome is not verifiable")
        out.append({
            "slug": slug,
            "title": item.get("title", ""),
            "severity": item.get("severity", ""),
            "state": state,
            "comment": (acc or {}).get("comment", ""),
            "stale": retro_queue.is_stale(item),
            "dispatchable": dispatchable,
            "dispatch_blockers": dispatch_blockers,
            "evidence_snapshot": item.get("evidence_snapshot", ""),
            "run_id": (acc or {}).get("run_id"),
            "status_detail": detail,
            "resume_cmd": (run or {}).get("resume_cmd"),
            "updated_at": (acc or dfr or {}).get("updated_at") or (acc or dfr or {}).get("date", ""),
        })
    return out


def api_state() -> tuple[int, dict]:
    silence = _silence_minutes()
    state = load_state()
    runs = [describe_run(r, silence) for r in state.get("runs", [])]
    return 200, {
        "ok": True,
        "schema": SCHEMA,
        "board": {"port": state.get("port"), "pid": os.getpid(),
                   "started": state.get("started"), "schema": SCHEMA,
                   "version": BOARD_VERSION},
        "retro_due": retro_due.state(),
        "providers": {
            "analyzer": _provider_view("analyzer"),
            "worker": _provider_view("worker"),
        },
        "findings": finding_states(silence),
        "queue_halted": _run_manager.halted(),
        "silence_minutes": silence,
        "runs": [
            {"run_id": r.get("run_id"), "kind": r.get("kind", "finding"),
             "finding": r.get("finding"), "slug": r.get("slug"),
             "status": r.get("status"), "status_label": r.get("status_label"),
             "provider": r.get("provider"),
             "model": r.get("model", ""),
             "persona": r.get("persona", ""),
             "elapsed_s": r.get("elapsed_s"),
             "silence_minutes": r.get("silence_minutes", 0.0),
             "exit_code": r.get("exit_code"), "resume_cmd": r.get("resume_cmd"),
             "last_line": r.get("last_line", ""),
             "outcome": r.get("outcome"),
             "result_summary": r.get("result_summary", ""),
             "result_errors": r.get("result_errors", []),
             "changed_files": r.get("changed_files", []),
             "verification": r.get("verification"),
             "integration": r.get("integration"),
             "workspace": r.get("workspace"),
             "timeout_seconds": r.get("timeout_seconds"),
             "timed_out": bool(r.get("timed_out"))}
            for r in runs
        ],
    }


def api_finding(slug: str) -> tuple[int, dict]:
    slug = (slug or "").strip()
    if not slug:
        return _error(400, "slug is required", "bad_request")
    item = retro_queue.load_item(slug)
    if item is None:
        return _error(404, f"no dispatch artifact for {slug!r}. "
                            "Run `kit retro publish` to build it.", "missing_artifact",
                      slug=slug)
    acc = _accepted_entry(slug)
    stale = retro_queue.is_stale(item)
    dispatchable, blockers = retro_queue.dispatch_eligibility(item)
    return 200, {
        "ok": True,
        "finding": item,
        "stale": stale,
        "dispatchable": dispatchable,
        "dispatch_blockers": blockers,
        "evidence_snapshot": item.get("evidence_snapshot", ""),
        "comment": (acc or {}).get("comment", ""),
        # Exactly the implementation prompt an approval with no comment adopts,
        # whether it is handed off manually or sent to an automatic provider.
        "prompt_preview": retro_queue.render_prompt(item, ""),
    }


def api_approve(slug: str, comment: str, by: str = "", request_id: str = "",
                retry_of: str = "") -> tuple[int, dict]:
    """Record one idempotent decision and dispatch only when safely available."""
    slug = (slug or "").strip()
    if not slug:
        return _error(400, "slug is required", "bad_request")
    item = retro_queue.load_item(slug)
    if item is None:
        return _error(404, f"no dispatch artifact for {slug!r}. "
                            "Run `kit retro publish` to build it.", "missing_artifact",
                      slug=slug)
    if retro_queue.is_stale(item):
        # Refused, not silently rebuilt: dispatching a prompt regenerated from
        # a findings file that moved would send something the human did not
        # read, which is the one thing the artifact exists to prevent.
        return _error(409, f"dispatch artifact for {slug!r} is stale: "
                            f"{item.get('source_file', '')} changed since it was written. "
                            "Refresh it with `kit retro publish`, re-read the "
                            "finding, and approve again.", "stale", slug=slug)
    dispatchable, blockers = retro_queue.dispatch_eligibility(item)
    if not dispatchable:
        return _error(
            409,
            f"dispatch artifact for {slug!r} is not eligible: " + "; ".join(blockers),
            "not_dispatchable",
            slug=slug,
            dispatchable=False,
            dispatch_blockers=blockers,
            evidence_snapshot=item.get("evidence_snapshot", ""),
        )
    title = item.get("title", "")
    request_id = (request_id or "").strip()
    retry_of = (retry_of or "").strip()
    if len(request_id) > 128 or any(ord(ch) < 32 for ch in request_id):
        return _error(400, "request_id must be at most 128 printable characters",
                      "bad_request", slug=slug)
    decision_id = _approval_decision_id(item, comment, retry_of)
    worker_spec: providers.ProviderSpec

    with _decision_file_lock:
        existing = _accepted_entry(slug)
        if existing and not existing.get("migrated_from"):
            if existing.get("decision_id") == decision_id:
                run_id = existing.get("run_id")
                if run_id:
                    run = next((r for r in load_state().get("runs", [])
                                if r.get("run_id") == run_id), None)
                    if run is not None:
                        finding = next((f for f in finding_states()
                                        if f["slug"] == slug), None)
                        return 200, {
                            "ok": True,
                            "finding": finding,
                            "run": {**describe_run(run, _silence_minutes()),
                                    "idempotent": True},
                            "dispatch": {
                                "started": True,
                                "provider": run.get("provider") or existing.get("provider"),
                                "blockers": [],
                                "idempotent": True,
                            },
                        }
                    # The approval and run id are one durable reservation.
                    # A duplicate request can arrive before enqueue has
                    # persisted the queue row, but it must still see the same
                    # reservation rather than claiming no worker was started
                    # or creating a second one.
                    finding = next((f for f in finding_states()
                                    if f["slug"] == slug), None)
                    reserved = {
                        "run_id": run_id,
                        "status": existing.get("state") or "reserved",
                        "finding": title,
                        "slug": slug,
                        "provider": existing.get("provider"),
                        "idempotent": True,
                    }
                    return 200, {
                        "ok": True,
                        "finding": finding,
                        "run": reserved,
                        "dispatch": {
                            "started": existing.get("state") != "failed",
                            "provider": existing.get("provider"),
                            "blockers": list(
                                existing.get("dispatch_blockers") or []
                            ),
                            "idempotent": True,
                        },
                    }
                else:
                    finding = next((f for f in finding_states()
                                    if f["slug"] == slug), None)
                    return 200, {
                        "ok": True,
                        "finding": finding,
                        "run": None,
                        "dispatch": {
                            "started": False,
                            "provider": existing.get("provider"),
                            "blockers": list(existing.get("dispatch_blockers") or []),
                            "idempotent": True,
                        },
                    }
            else:
                previous_run = existing.get("run_id")
                previous = next((r for r in load_state().get("runs", [])
                                 if r.get("run_id") == previous_run), None)
                retry_valid = bool(
                    retry_of and previous_run == retry_of and previous
                    and previous.get("status") in ("blocked", "unverified", "failed")
                )
                amendable = not previous_run and existing.get("state") == "approved"
                if not retry_valid and not amendable:
                    return _error(
                        409,
                        "this finding already has a different durable approval; "
                        "retry only by naming its blocked, unverified, or failed run",
                        "decision_conflict", slug=slug, run_id=previous_run,
                    )
        try:
            worker_spec = providers.selection(ROOT, "worker")
        except (providers.ProviderConfigError, runtime_paths.RuntimeConfigError) as exc:
            worker_spec = providers.ProviderSpec(
                "worker", "manual", persona="kit-builder"
            )
            provider_blockers = [f"worker provider configuration is invalid: {exc}"]
        else:
            provider_blockers = providers.preflight(worker_spec)
        repository_blockers = (
            run_result.dispatch_blockers(
                ROOT, allowed_changes=DISPATCH_LEDGER_CHANGES,
                requested_files=item.get("fix_files"),
            )
            if worker_spec.automatic and not provider_blockers
            else []
        )
        dispatch_blockers = [*provider_blockers, *repository_blockers]
        should_dispatch = worker_spec.automatic and not dispatch_blockers
        reserved_run_id = RunManager._new_run_id() if should_dispatch else None
        ok, err = record_decision(
            title, "accept", by, comment, comment=comment, slug=slug,
            metadata={"decision_id": decision_id,
                      "request_id": request_id or None,
                      "artifact_sha256": item.get("source_sha256", ""),
                      "retry_of": retry_of or None,
                      "provider": worker_spec.status(),
                      "dispatch_blockers": dispatch_blockers,
                      "run_id": reserved_run_id,
                      "state": "reserved" if should_dispatch else "approved"},
        )
        if not ok:
            return _error(400, err, "bad_request", slug=slug)

    if not should_dispatch:
        finding = next((f for f in finding_states() if f["slug"] == slug), None)
        return 200, {
            "ok": True,
            "finding": finding,
            "run": None,
            "dispatch": {
                "started": False,
                "provider": worker_spec.status(),
                "blockers": dispatch_blockers,
            },
        }

    entry = _run_manager.enqueue(
        {"finding": title, "slug": slug,
         "prompt": retro_queue.render_prompt(item, comment),
         "fix_files": item.get("fix_files"),
         "decision_id": decision_id,
         "_run_id": reserved_run_id,
         "_provider_spec": worker_spec},
        retry_of=retry_of,
    )
    if "error" in entry:
        annotate_accepted(slug, state="failed")
        return _error(500, entry["error"], "spawn_failed", slug=slug,
                      run_id=reserved_run_id)
    run_status = entry.get("status")
    if run_status == "completed":
        accepted_state = "done"
    elif run_status in ("blocked", "unverified", "failed"):
        accepted_state = run_status
    elif run_status == "queued":
        accepted_state = "queued"
    else:
        accepted_state = "working"
    annotate_accepted(slug, run_id=entry.get("run_id"), state=accepted_state)
    finding = next((f for f in finding_states() if f["slug"] == slug), None)
    return 200, {
        "ok": True,
        "finding": finding,
        "run": entry,
        "dispatch": {
            "started": True,
            "provider": worker_spec.status(),
            "blockers": [],
        },
    }


def api_defer(slug: str, reason: str, by: str = "") -> tuple[int, dict]:
    slug = (slug or "").strip()
    if not slug:
        return _error(400, "slug is required", "bad_request")
    item = retro_queue.load_item(slug)
    if item is None:
        return _error(404, f"no dispatch artifact for {slug!r}", "missing_artifact", slug=slug)
    ok, err = record_decision(item.get("title", ""), "defer", by, reason)
    if not ok:
        return _error(400, err, "bad_request", slug=slug)
    finding = next((f for f in finding_states() if f["slug"] == slug), None)
    return 200, {"ok": True, "finding": finding}


def api_retro_run() -> tuple[int, dict]:
    try:
        analyzer = providers.selection(ROOT, "analyzer")
    except (providers.ProviderConfigError, runtime_paths.RuntimeConfigError) as exc:
        return _error(
            409,
            f"analyzer provider configuration is invalid: {exc}",
            "provider_invalid",
        )
    blockers = providers.preflight(analyzer)
    if blockers:
        return _error(
            409,
            "analyzer provider is unavailable: " + "; ".join(blockers),
            "provider_unavailable",
            provider=analyzer.status(),
            provider_blockers=blockers,
        )
    entry = _run_manager.spawn_retro(analyzer)
    if "error" in entry:
        return _error(409 if "already in flight" in entry["error"] else 500,
                      entry["error"], "retro_unavailable")
    return 200, {"ok": True, "run_id": entry["run_id"], "run": entry}


def api_run(run_id: str) -> tuple[int, dict]:
    run_id = (run_id or "").strip()
    entry = next((r for r in load_state().get("runs", []) if r.get("run_id") == run_id), None)
    if entry is None:
        return _error(404, f"no run {run_id!r}", "missing_run", run_id=run_id)
    described = describe_run(entry, _silence_minutes())
    tail = ""
    if entry.get("log"):
        try:
            tail = "\n".join((ROOT / entry["log"]).read_text(
                encoding="utf-8", errors="replace").splitlines()[-40:])
        except OSError:
            tail = ""
    return 200, {
        "ok": True,
        "run_id": run_id,
        "kind": entry.get("kind", "finding"),
        "finding": entry.get("finding"),
        "slug": entry.get("slug"),
        "status": described.get("status"),
        "status_label": described.get("status_label"),
        "exit_code": entry.get("exit_code"),
        "timeout_seconds": entry.get("timeout_seconds"),
        "timed_out": bool(entry.get("timed_out")),
        "silence_minutes": described.get("silence_minutes", 0.0),
        "resume_cmd": entry.get("resume_cmd"),
        "log": entry.get("log"),
        "tail": tail,
    }


# --------------------------------------------------------------- HTTP


class BoardHTTPServer(http.server.ThreadingHTTPServer):
    """Loopback server carrying an in-memory mutation capability.

    The token is deliberately absent from board.state.json, URLs, API payloads
    and generated files.  It is injected only into HTML bytes served by this
    process, and rotates whenever the board restarts.
    """

    daemon_threads = True

    def __init__(self, server_address, handler_class,
                 capability_token: str | None = None) -> None:
        super().__init__(server_address, handler_class)
        port = int(self.server_address[1])
        self.capability_token = capability_token or secrets.token_urlsafe(32)
        self.instance_id = secrets.token_urlsafe(12)
        self.allowed_hosts = {f"127.0.0.1:{port}"}
        self.allowed_origins = {f"http://127.0.0.1:{port}"}


def _security_headers(handler: http.server.BaseHTTPRequestHandler,
                      nonce: str | None = None) -> None:
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "DENY")
    if nonce:
        handler.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            f"script-src 'self' 'nonce-{nonce}'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self'; font-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-src 'none'; frame-ancestors 'none'; "
            "form-action 'none'; worker-src 'none'",
        )
    else:
        handler.send_header("Content-Security-Policy",
                            "default-src 'none'; frame-ancestors 'none'")


def _server_value(handler: http.server.BaseHTTPRequestHandler,
                  name: str, default=""):
    return getattr(handler.server, name, default)


def _serve_html(handler: http.server.BaseHTTPRequestHandler, name: str) -> None:
    path = ROOT / name
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        _json(handler, 404, {"ok": False, "code": "not_found",
                             "error": f"generated page is missing: {name}"})
        return
    token = str(_server_value(handler, "capability_token"))
    instance = str(_server_value(handler, "instance_id"))
    nonce = secrets.token_urlsafe(18)
    meta = (
        f'<meta name="kit-board-token" content="{html.escape(token, quote=True)}">'
        f'<meta name="kit-board-instance" content="{html.escape(instance, quote=True)}">'
    )
    if "</head>" in text:
        text = text.replace("</head>", meta + "</head>", 1)
    else:
        text = meta + text
    text = re.sub(r"<script(?=[\s>])(?![^>]*\bnonce=)",
                  f'<script nonce="{nonce}"', text)
    body = text.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    _security_headers(handler, nonce)
    handler.end_headers()
    handler.wfile.write(body)


def _serve_static(handler: http.server.BaseHTTPRequestHandler, name: str,
                  content_type: str = "text/html; charset=utf-8") -> None:
    path = ROOT / name
    try:
        body = path.read_bytes()
    except OSError:
        _json(handler, 404, {"ok": False, "code": "not_found",
                             "error": f"static asset is missing: {name}"})
        return
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
    _security_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def _serve_vendor(handler: http.server.BaseHTTPRequestHandler, path: str) -> None:
    """Serve a file plan.html referenced with a disk-relative <script src>.

    file:// resolves `tools/vendor/mermaid.min.js` against the sibling folder
    with no help from us; the same relative src requested through this server
    has nothing else to answer it, so without this route the bundle 404s and
    mermaid never defines itself -- diagrams silently stay as source text.
    Restricted to `tools/vendor/*.js`: the one asset plan.html actually
    references, not general static file serving.
    """
    name = path[len("/tools/vendor/"):]
    if not re.match(r"^[\w.-]+\.js$", name):
        _json(handler, 404, {"ok": False, "code": "not_found",
                             "error": "no such vendor asset"})
        return
    _serve_static(handler, f"tools/vendor/{name}",
                 content_type="application/javascript; charset=utf-8")


def _json(handler: http.server.BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    _security_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def _validate_host(handler: http.server.BaseHTTPRequestHandler) -> tuple[int, dict] | None:
    hosts = handler.headers.get_all("Host") or []
    allowed = _server_value(handler, "allowed_hosts", set())
    if len(hosts) != 1 or hosts[0].strip().lower() not in allowed:
        return _error(421, "request Host is not this board instance", "invalid_host")
    return None


def _approval_decision_id(item: dict, comment: str, retry_of: str = "") -> str:
    """Content identity for one exact human approval.

    Repeated HTTP delivery of the same decision produces the same id even if
    the browser lost the first response. A retry is a new decision because it
    names the terminal attempt whose evidence the human is choosing to retry.
    """
    payload = {
        "slug": item.get("slug", ""),
        "source_sha256": item.get("source_sha256", ""),
        "comment": (comment or "").strip(),
        "retry_of": (retry_of or "").strip(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_mutation(handler: http.server.BaseHTTPRequestHandler) -> tuple[int, dict] | None:
    if handler.headers.get("Transfer-Encoding"):
        return _error(400, "streamed request bodies are not accepted",
                      "unsupported_transfer_encoding")
    origins = handler.headers.get_all("Origin") or []
    allowed_origins = _server_value(handler, "allowed_origins", set())
    if len(origins) != 1 or origins[0].strip().lower() not in allowed_origins:
        return _error(403, "mutation requires this board's exact origin", "invalid_origin")
    media_type = handler.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        return _error(415, "mutation body must be application/json",
                      "unsupported_media_type")
    tokens = handler.headers.get_all("X-Kit-Board-Token") or []
    expected = str(_server_value(handler, "capability_token"))
    if len(tokens) != 1 or not expected or not secrets.compare_digest(tokens[0], expected):
        return _error(403, "board capability is missing or stale", "invalid_capability")
    return None


def _read_json_body(handler: http.server.BaseHTTPRequestHandler) -> tuple[dict | None,
                                                                           tuple[int, dict] | None]:
    lengths = handler.headers.get_all("Content-Length") or []
    if len(lengths) != 1:
        return None, _error(411, "one Content-Length header is required", "length_required")
    try:
        length = int(lengths[0])
    except (TypeError, ValueError):
        return None, _error(400, "invalid Content-Length", "invalid_length")
    if length < 0:
        return None, _error(400, "invalid Content-Length", "invalid_length")
    if length > MAX_REQUEST_BODY:
        # Drain only a narrowly oversized body so Windows can deliver the
        # structured 413 rather than resetting a socket with unread bytes.
        # Arbitrarily large claims are never allocated or consumed.
        if length <= MAX_REQUEST_BODY + 4096:
            handler.rfile.read(length)
        handler.close_connection = True
        return None, _error(413, f"request body exceeds {MAX_REQUEST_BODY} bytes",
                            "body_too_large")
    raw = handler.rfile.read(length)
    if len(raw) != length:
        return None, _error(400, "request body ended early", "incomplete_body")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, _error(400, "request body is not valid UTF-8 JSON", "invalid_json")
    if not isinstance(value, dict):
        return None, _error(400, "request JSON root must be an object", "invalid_json_root")
    return value, None


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "board/2.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 (stdlib name)
        pass  # goes to board.log via the redirected stdout of the whole process instead

    def _dispatch(self, method: str, path: str, body: dict) -> tuple[int, dict] | None:
        """Route one API call. None means "not an API path"."""
        if method == "GET":
            if path == "/api/health":
                return api_health()
            if path == "/api/state":
                return api_state()
            if path.startswith("/api/finding/"):
                return api_finding(path[len("/api/finding/"):])
            if path.startswith("/api/runs/"):
                return api_run(path[len("/api/runs/"):])
        else:
            if path == "/api/finding/approve":
                return api_approve(body.get("slug", ""), body.get("comment", ""),
                                    body.get("by", ""), body.get("request_id", ""),
                                    body.get("retry_of", ""))
            if path == "/api/finding/defer":
                return api_defer(body.get("slug", ""), body.get("reason", ""),
                                  body.get("by", ""))
            if path == "/api/retro/run":
                return api_retro_run()
        if path.startswith("/api/"):
            return _error(404, f"no such endpoint: {method} {path}", "no_such_endpoint")
        return None

    def _handle(self, method: str) -> None:
        path, _, _query = self.path.partition("?")
        rejected = _validate_host(self)
        if rejected is not None:
            _json(self, rejected[0], rejected[1])
            return
        body = {}
        if method == "POST":
            rejected = _validate_mutation(self)
            if rejected is not None:
                _json(self, rejected[0], rejected[1])
                return
            body, rejected = _read_json_body(self)
            if rejected is not None:
                _json(self, rejected[0], rejected[1])
                return
        try:
            result = self._dispatch(method, path, body or {})
        except Exception as exc:  # noqa: BLE001 -- an unhandled error must still
            # reach the page as JSON. An HTML traceback is unreadable to the
            # fetch() on the other end and shows up as "something went wrong".
            _json(self, 500, {"ok": False, "code": "server_error",
                               "error": f"{type(exc).__name__}: {exc}"})
            return
        if result is not None:
            _json(self, result[0], result[1])
            return
        if method == "GET" and path in ("/", "/plan.html"):
            _serve_html(self, "plan.html")
        elif method == "GET" and path == "/retro.html":
            _serve_html(self, "retro.html")
        elif method == "GET" and path.startswith("/tools/vendor/"):
            _serve_vendor(self, path)
        else:
            _json(self, 404, {"ok": False, "code": "not_found",
                               "error": f"no such path: {path}"})

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")

    def do_OPTIONS(self) -> None:  # noqa: N802
        rejected = _validate_host(self)
        if rejected is not None:
            _json(self, rejected[0], rejected[1])
            return
        code, payload = _error(403, "cross-origin preflight is not permitted",
                               "cors_forbidden")
        _json(self, code, payload)


def serve(port: int) -> int:
    # Self-heal on start: a decision file from the pre-comment pipeline must
    # not render as a queued item nobody is working on (see migrate_accepted).
    migrate_accepted_file()
    migrate_state_file()
    _run_manager.recover()
    httpd = BoardHTTPServer(("127.0.0.1", port), Handler)
    print(f"{_now_iso()} board serving on http://127.0.0.1:{port}/", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--serve", action="store_true", help="run the HTTP server (internal)")
    ap.add_argument("--ensure", action="store_true", help="start if needed, print the URL")
    ap.add_argument("--migrate", action="store_true",
                    help="normalise docs/retro/accepted.json in place and exit")
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    if args.migrate:
        n = migrate_accepted_file()
        print(f"accepted.json: {n} entry(ies) migrated" if n else "accepted.json: nothing to migrate")
        return 0
    if args.serve:
        return serve(args.port or _free_port())
    if args.ensure:
        url = ensure_running()
        print(url or "board: could not start")
        return 0 if url else 1
    print("nothing to do: pass --serve (internal) or --ensure", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
