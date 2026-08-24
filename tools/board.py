#!/usr/bin/env python3
"""Local loopback board: makes plan.html and retro.html interactive.

Standard library only. `http.server` on 127.0.0.1, an ephemeral port, no auth
beyond loopback -- nothing here is reachable from outside this machine.

Why this exists: the tick boxes plan.html and retro.html render are Unicode
glyphs, not inputs, because a page opened with `file://` cannot capture a
click, persist a decision, or spawn a process -- `fetch()` is blocked
entirely under that protocol. That restriction is already load-bearing
elsewhere in this kit (it is why design docs are inlined into plan.html
rather than fetched), so the fix is not to fight it: serve the same static
pages over loopback HTTP instead, where `fetch()` works, and let the pages
detect which situation they are in by trying it.

State lives in exactly one file, `docs/retro/board.state.json`: the port,
the server's own pid, and a list of dispatched agent runs. It holds nothing
durable -- accept/defer decisions live in `docs/retro/accepted.json` and
`docs/retro/deferred.json` (see `tools/retro_html.py`), findings stay in
`docs/retro/*-findings.md`. Losing `board.state.json` loses only "what is the
board doing right now", never a decision or a finding. In particular the
human's approval *comment* is durable: it is stored on the accepted.json
entry, because it is the amended proposal a worker was dispatched with, not a
UI nicety, and it must survive a board restart.

HTTP contract -- **this list is the authoritative one**; docs point here rather
than restating it (`docs/retro/dispatch-pipeline-plan.md` recorded the decisions
that produced it and is now closed history):

    GET  /api/health              cheap liveness probe, no side effects
    GET  /api/state               the whole view model in one request
    GET  /api/finding/<slug>      artifact + prompt_preview, before approving
    POST /api/finding/approve     {slug, comment} -> record, then dispatch
    POST /api/finding/defer       {slug, reason}  -> record, never dispatch
    POST /api/retro/run           start the retrospective model (spends quota)
    GET  /api/runs/<run_id>       status, exit code, silence, log tail

Those seven are the whole surface. `/api/dispatch/prepare`, `/api/dispatch/run`
and `/api/decision` were deleted rather than left as dead paths: a stale page
hitting one gets `{"code": "no_such_endpoint"}` and says so, and there is
exactly one approval path instead of two.

Approval *is* dispatch (decision 4): writing the comment is the conscious act
that spends quota, so there is no second Run click. Approvals stay sequential
-- two findings can name the same `fix_files` -- so a second approval while one
run is in flight is queued behind it by the one queue `RunManager` owns.

Every API response is JSON, including every error: a non-2xx code with a
message worth showing. No HTML traceback and no 200 with a silent failure ever
leaves this server.

    python tools/board.py --serve --port 51234      run the server (internal;
                                                     tools call ensure_running()
                                                     instead of invoking this)
    python tools/board.py --ensure                  start it if not running,
                                                     print the URL
    python tools/board.py --migrate                 normalise accepted.json
                                                     entries written before
                                                     approval carried a comment

Every other tool keeps working with the board absent. `plan_html.py` and
`retro_html.py` call `ensure_running()` after writing their output and print
whatever it returns (or a note that it could not start), but they do not
depend on it succeeding -- a board that fails to start degrades the workflow
to exactly what it is today, it does not break anything.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import shutil
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
RUNS_DIR = RETRO_DIR / "runs"
STATE_FILE = RETRO_DIR / "board.state.json"
LOCK_FILE = RETRO_DIR / "board.lock"
CONFIG_FILE = ROOT / "retro.config.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import retro_rank      # noqa: E402  (sibling module; no top-level import of `board`, so this is not a cycle)
import retro_queue     # noqa: E402  (dispatch prompt artifacts; same non-cycle argument)
import retro_due       # noqa: E402  (deterministic note counter; it never calls a model and must not learn how)
import retro_html      # noqa: E402  (same -- see its main() for the lazy `import board`)
import session_digest  # noqa: E402
import board_client    # noqa: E402  (CSS/JS constants only, zero project imports of its own)

SCHEMA = 1

# Finding states reported by /api/state. `stale` is orthogonal to all of them.
STATES = ("awaiting_review", "queued", "working", "done", "failed", "deferred")

# Silence threshold before an alive-but-quiet run is reported as such rather
# than as plain "running". Three minutes: a kit-builder slice's individual
# tool calls (view, edit, a targeted check.py run) each take seconds, so
# three minutes of nothing written to the log is already an unusual gap
# without being noisy over an ordinary short pause. Configurable because
# "unusual" depends on what the dispatched task actually does.
SILENCE_MINUTES_DEFAULT = 3.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _copilot_executable() -> str | None:
    """Resolve a runnable Copilot command, including Windows npm shims."""
    names = ("copilot.cmd", "copilot.exe", "copilot") if os.name == "nt" else ("copilot",)
    for name in names:
        executable = shutil.which(name)
        if executable:
            return executable
    return None


# --------------------------------------------------------------- state.json

def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"port": None, "pid": None, "started": None, "runs": []}
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d.setdefault("port", None)
    d.setdefault("pid", None)
    d.setdefault("started", None)
    d.setdefault("runs", [])
    return d


def save_state(state: dict) -> None:
    RETRO_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


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
    return bool(isinstance(payload, dict) and payload.get("ok") and payload.get("schema") == SCHEMA)


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
    """(Re)start the board if the recorded instance is gone, never more than one.

    Guarded by a short-lived lockfile: the check ("is the recorded pid alive
    and does the port answer") and the spawn are not atomic otherwise, and
    `plan_html.py` and `retro_html.py` can both call this back-to-back in the
    same slice. The lock is only ever held for the few hundred milliseconds
    this function itself runs, and a lock older than 10s is assumed to be
    left over from a process that died mid-check and is broken rather than
    honoured, so a crash here can never wedge every future call.
    """
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
        RETRO_DIR.mkdir(parents=True, exist_ok=True)
        server_log = RETRO_DIR / "board.log"
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
        for _ in range(50):
            if _probe(port):
                break
            time.sleep(0.1)
        state["port"] = port
        state["pid"] = proc.pid
        state["started"] = _now_iso()
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
    else. Under decision 4 an acceptance *is* a dispatch, so an old entry has
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
    RETRO_DIR.mkdir(parents=True, exist_ok=True)
    ACCEPTED_FILE.write_text(json.dumps(entries, indent=2) + "\n", encoding="utf-8")


def record_decision(finding: str, action: str, by: str, reason: str,
                    comment: str = "", slug: str = "") -> tuple[bool, str]:
    finding = (finding or "").strip()
    action = (action or "").strip().lower()
    if not finding:
        return False, "finding is required"
    if action not in ("accept", "defer"):
        return False, "action must be accept or defer"
    key = retro_rank.normalise_title(finding)
    entry = {"finding": finding, "date": date.today().isoformat(),
              "by": (by or "").strip(), "reason": (reason or "").strip()}
    if action == "accept":
        # The comment is the amended proposal the worker was dispatched with.
        # It is written here, before the spawn, so a board killed mid-dispatch
        # still leaves a record of what the human actually approved.
        entry["comment"] = (comment or "").strip()
        entry["slug"] = slug or retro_queue.slug_for(finding)
        entry["updated_at"] = _now_iso()
        entries = [e for e in load_accepted() if retro_rank.normalise_title(e.get("finding", "")) != key]
        entries.append(entry)
        save_accepted(entries)
    else:
        entries = [e for e in retro_rank.load_deferred() if retro_rank.normalise_title(e.get("finding", "")) != key]
        entries.append(entry)
        retro_rank.save_deferred(entries)
    _regenerate("retro_html.py")
    return True, ""


def _accepted_entry(slug: str) -> dict | None:
    for e in load_accepted():
        if (e.get("slug") or retro_queue.slug_for(e.get("finding", ""))) == slug:
            return e
    return None


def annotate_accepted(slug: str, **fields) -> None:
    """Attach dispatch bookkeeping (run_id, state) to a stored decision."""
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

# The kit-builder restrictions (kit files only, never src/, finish green) now
# live with the artifact that carries them, so the stored prompt is the whole
# prompt. Re-exported under the old name: it is the documented constant.
DISPATCH_HEADER = retro_queue.DISPATCH_HEADER


def pending_finding_titles() -> list[str]:
    """Titles of accepted, unresolved findings, in the order they appear
    across docs/retro/*-findings.md. Shared by /api/dispatch/prepare and
    anything else that needs "what is waiting to be dispatched" without a
    second reader of accepted.json.
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

    This reads `docs/retro/queue/<slug>.json` and nothing else. It never opens
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
            "error": (f"no dispatch artifact for {slug!r}. "
                      "Run `python tools/retro_rank.py` to build it."),
        }
    stale = retro_queue.is_stale(item)
    return {
        "finding": title,
        "slug": slug,
        "prompt": retro_queue.render_prompt(item, comment),
        "ok": not stale,
        "stale": stale,
        "missing": False,
        "generated_at": item.get("generated_at", ""),
        "source_file": item.get("source_file", ""),
        "error": (f"dispatch artifact for {slug!r} is stale: "
                  f"{item.get('source_file', '')} changed since it was written. "
                  "Re-rank before dispatching.") if stale else "",
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
        self._queue: list[dict] = []   # [{"finding", "slug", "prompt"}, ...]
        self._queue_pos = 0
        self._active: str | None = None    # run_id of the in-flight finding
        self._halted = False               # a failed item stops the queue
        self._retro_run: str | None = None

    def spawn(self, prompt: str, finding: str | None = None,
              queue_position: int | None = None, queue_total: int | None = None,
              slug: str | None = None) -> dict:
        cfg = _config()
        model = cfg.get("dispatch_model") or cfg.get("model") or ""
        # `--allow-all-tools` is REQUIRED for non-interactive mode: without it
        # the child prompts for permission, nothing answers, and every write is
        # denied. An earlier default of ["read","edit","search","execute"] named
        # tools that do not exist -- the real ones are `write` and `shell(...)`
        # -- so a dispatched worker burned a full run and could change nothing.
        # `--allow-all-paths` because the kit-builder edits docs/ and tools/ and
        # runs `python check.py`. The persona's deny list, not the CLI, is what
        # keeps it out of src/.
        allow_all = cfg.get("dispatch_allow_all", True)
        allow_tools = cfg.get("dispatch_allow_tools") or []
        deny_tools = cfg.get("dispatch_deny_tools") or []

        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = RUNS_DIR / f"{run_id}.log"
        prompt_path = RUNS_DIR / f"{run_id}.prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")

        executable = _copilot_executable()
        if executable is None:
            return {"error": "could not spawn copilot: executable not found on PATH"}
        cmd = [executable, "--agent", "kit-builder", "-p", prompt]
        if model:
            cmd += ["--model", model]
        if allow_all:
            cmd += ["--allow-all-tools", "--allow-all-paths"]
        for t in allow_tools:
            cmd += ["--allow-tool", t]
        for t in deny_tools:
            cmd += ["--deny-tool", t]

        pre_existing = {s["id"] for s in session_digest.discover(ROOT)}
        lf = open(log_path, "ab")
        try:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=lf, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL)
        except OSError as exc:
            lf.close()
            return {"error": f"could not spawn copilot: {exc}"}
        finally:
            # The child holds its own duplicated handle. Keeping this one open
            # would pin the log for the life of the board process -- on Windows
            # that is an unopenable, undeletable file for everyone else.
            if not lf.closed:
                lf.close()

        entry = {
            "run_id": run_id,
            "kind": "finding",
            "persona": "kit-builder",
            "finding": finding,
            "slug": slug,
            "queue_position": queue_position,
            "queue_total": queue_total,
            "started": _now_iso(),
            "t0": time.time(),
            "log": str(log_path.relative_to(ROOT)).replace("\\", "/"),
            "prompt_file": str(prompt_path.relative_to(ROOT)).replace("\\", "/"),
            "pid": proc.pid,
            "status": "running",
            "session_id": None,
            "resume_cmd": None,
            "exit_code": None,
        }
        with self._lock:
            self._procs[run_id] = proc
        state = load_state()
        state.setdefault("runs", []).append(entry)
        save_state(state)

        threading.Thread(
            target=self._watch, args=(run_id, proc, pre_existing), daemon=True
        ).start()
        return entry

    def enqueue(self, item: dict) -> dict:
        """Approve-dispatches one finding, behind anything already running.

        Decision 4 made approval the dispatch, so this is what a `POST
        /api/finding/approve` reaches. It appends to the single queue and
        starts the item only if nothing is in flight: two findings can name
        the same `fix_files`, and concurrent kit-builders editing one gate
        file is a merge conflict waiting to happen.
        """
        with self._lock:
            self._queue.append(item)
            total = len(self._queue)
            position = total
            idle = self._active is None
            halted = self._halted
        if idle and not halted:
            entry = self._advance_queue()
            if entry and "error" not in entry:
                return entry
            if entry and "error" in entry:
                return entry
        return {
            "run_id": None,
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
                 "position": n + 1, "total": len(self._queue)}
                for n, i in enumerate(self._queue)
            ]

    def halted(self) -> bool:
        with self._lock:
            return self._halted

    def _advance_queue(self) -> dict:
        with self._lock:
            if self._halted or self._active is not None:
                return {}
            index = next((n for n, i in enumerate(self._queue)
                          if not i.get("_run_id")), None)
            if index is None:
                return {}
            self._queue_pos = index
            item = self._queue[index]
            total = len(self._queue)
        entry = self.spawn(item["prompt"], finding=item["finding"],
                            queue_position=index + 1, queue_total=total,
                            slug=item.get("slug"))
        if "error" not in entry:
            with self._lock:
                item["_run_id"] = entry["run_id"]
                self._active = entry["run_id"]
            if item.get("slug"):
                annotate_accepted(item["slug"], run_id=entry["run_id"], state="working")
        else:
            # A spawn that never started must not wedge every later approval.
            with self._lock:
                item["_run_id"] = f"failed-{time.time():.0f}"
        return entry

    def spawn_retro(self) -> dict:
        """Start the retrospective model run -- decision 1, one conscious click.

        Out of process, because it takes minutes and the request thread must
        not block. `retro_due.py` stays inert: nothing there gained the
        ability to call a model, this did, and only when asked over HTTP.
        The pinned model in retro.config.json is passed through, so quota is
        never spent at an unknown rate.
        """
        with self._lock:
            active = self._retro_run
        if active:
            state = load_state()
            for r in state.get("runs", []):
                if r.get("run_id") == active and r.get("status") == "running":
                    return {"error": f"a retrospective run is already in flight ({active})"}
        cfg = _config()
        model = cfg.get("model") or ""
        RUNS_DIR.mkdir(parents=True, exist_ok=True)
        run_id = "retro-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        log_path = RUNS_DIR / f"{run_id}.log"
        cmd = [sys.executable, str(ROOT / "tools" / "retro.py"), "--sdk", "--force"]
        env = dict(os.environ)
        if model:
            env["RETRO_MODEL"] = model
        lf = open(log_path, "ab")
        try:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=lf, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, env=env)
        except OSError as exc:
            lf.close()
            return {"error": f"could not start the retrospective: {exc}"}
        finally:
            if not lf.closed:
                lf.close()
        entry = {
            "run_id": run_id,
            "kind": "retro",
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
        rc = proc.wait()
        self._update_run(run_id, status="finished" if rc == 0 else "failed", exit_code=rc)
        with self._lock:
            self._procs.pop(run_id, None)
            if self._retro_run == run_id:
                self._retro_run = None
        if rc == 0:
            # Ranking regenerates the dispatch artifacts, so new findings are
            # approvable without a manual step. Without this the human would
            # have to remember a CLI the board exists to replace.
            _regenerate("retro_rank.py")
        _regenerate("retro_html.py")
        _regenerate("plan_html.py")

    def _update_run(self, run_id: str, **fields) -> None:
        state = load_state()
        for r in state.get("runs", []):
            if r.get("run_id") == run_id:
                r.update(fields)
                break
        save_state(state)

    def _watch(self, run_id: str, proc: subprocess.Popen, pre_existing: set[str]) -> None:
        # Resolve the CLI session id the same way session_digest.py already
        # discovers sessions for this repo, rather than re-parsing
        # workspace.yaml a second way: poll until a session directory shows
        # up that was not there before this run started.
        session_id = None
        for _ in range(90):  # ~3 minutes at 2s -- a fresh session directory
                              # appears within seconds of copilot starting up
            found = session_digest.discover(ROOT)
            new_ids = [s["id"] for s in found if s["id"] not in pre_existing]
            if new_ids:
                session_id = new_ids[-1]
                break
            if proc.poll() is not None:
                # one more look in case the session directory landed just
                # as the process exited
                found = session_digest.discover(ROOT)
                new_ids = [s["id"] for s in found if s["id"] not in pre_existing]
                session_id = new_ids[-1] if new_ids else None
                break
            time.sleep(2)
        if session_id:
            self._update_run(
                run_id, session_id=session_id,
                resume_cmd=f"copilot --agent kit-builder --resume={session_id}",
            )

        rc = proc.wait()
        self._update_run(run_id, status="finished" if rc == 0 else "failed", exit_code=rc)
        with self._lock:
            self._procs.pop(run_id, None)
            if self._active == run_id:
                self._active = None
            item = next((i for i in self._queue if i.get("_run_id") == run_id), None)
            if rc != 0 and item is not None:
                # A failed item halts the queue rather than continuing, and is
                # never auto-retried. Recorded so /api/state can say *why*
                # everything behind it is sitting still.
                self._halted = True
        if item is not None and item.get("slug"):
            annotate_accepted(item["slug"], state="done" if rc == 0 else "failed",
                               run_id=run_id)

        # The human should see the result, not a completion notice: findings
        # whose fix_files were touched since acceptance render as implemented
        # (retro_html.py derives that from git, not from anything this
        # process reports about its own run).
        _regenerate("retro_html.py")
        _regenerate("plan_html.py")

        # Queue continuation: the next approval starts only after this process
        # has already exited, so approvals are sequential by construction.
        if rc == 0:
            self._advance_queue()


def describe_run(entry: dict, silence_minutes: float) -> dict:
    """Decorate a stored run record with an honest status label.

    Exactly the four vocabulary forms this was specified with: running, no
    output Nm, finished, failed. Never "stuck" or "needs intervention" -- a
    `-p` invocation is non-interactive, so silence is silence, not a request
    for help the board has no way to see.
    """
    out = dict(entry)
    status = entry.get("status", "running")
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

    if status == "running":
        alive = _pid_alive(entry.get("pid"))
        if not alive:
            # The watcher thread that would have recorded a real exit code
            # belonged to a board process that is no longer running this
            # one. Say exactly that rather than guessing finished or failed.
            status = "finished (exit unknown -- board restarted mid-run)"
        else:
            if silent_for is not None and silent_for > silence_minutes:
                status = f"no output {int(silent_for)}m"
            else:
                status = "running"
    elif status == "failed":
        status = f"failed (exit {entry.get('exit_code')})"

    out["status_label"] = status
    out["silence_minutes"] = round(silent_for, 1) if silent_for is not None else 0.0
    out["elapsed_s"] = max(0.0, time.time() - float(entry.get("t0", time.time())))
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
    return float(_config().get("board_silence_minutes", SILENCE_MINUTES_DEFAULT))


def _error(code: int, message: str, kind: str = "error", **extra) -> tuple[int, dict]:
    payload = {"ok": False, "error": message, "code": kind}
    payload.update(extra)
    return code, payload


def api_health() -> tuple[int, dict]:
    state = load_state()
    return 200, {"ok": True, "port": state.get("port"), "pid": os.getpid(),
                 "started": state.get("started"), "schema": SCHEMA}


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
    unstarted = [q["slug"] for q in _run_manager.queue_view()
                 if q.get("slug") and not q.get("run_id")]
    queued_slugs = {slug: n + 1 for n, slug in enumerate(unstarted)}
    halted = _run_manager.halted()

    out: list[dict] = []
    for slug in retro_queue.load_index().get("items", []):
        item = retro_queue.load_item(slug)
        if item is None:
            continue
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
                    state = stored if stored in STATES else "queued"
                    if acc.get("migrated_from"):
                        # An acceptance from the pre-comment pipeline. It was
                        # never dispatched and carries no amended proposal, so
                        # it is waiting for a real approval, not for a worker.
                        detail = ("accepted by the pre-comment pipeline; approve "
                                  "again with a comment to dispatch it")
                    else:
                        detail = ("recorded before this board started; no live run to report"
                                  if stored in STATES else
                                  "accepted but not dispatched by this board")
                if halted:
                    detail += " -- the queue is halted after a failed run"
            elif run.get("status") == "running":
                state = "working"
                silent = run.get("silence_minutes") or 0.0
                detail = (f"working, no output for {int(silent)}m"
                          if silent > silence else f"working, last output {int(silent)}m ago")
            elif run.get("status") == "failed":
                state = "failed"
                detail = f"worker exited {run.get('exit_code')}"
            else:
                state = "done"
                detail = run.get("status_label", "finished")
        out.append({
            "slug": slug,
            "title": item.get("title", ""),
            "severity": item.get("severity", ""),
            "state": state,
            "comment": (acc or {}).get("comment", ""),
            "stale": retro_queue.is_stale(item),
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
                   "started": state.get("started"), "schema": SCHEMA},
        "retro_due": retro_due.state(),
        "findings": finding_states(silence),
        "queue_halted": _run_manager.halted(),
        "silence_minutes": silence,
        "runs": [
            {"run_id": r.get("run_id"), "kind": r.get("kind", "finding"),
             "finding": r.get("finding"), "slug": r.get("slug"),
             "status": r.get("status"), "status_label": r.get("status_label"),
             "persona": r.get("persona", ""),
             "elapsed_s": r.get("elapsed_s"),
             "silence_minutes": r.get("silence_minutes", 0.0),
             "exit_code": r.get("exit_code"), "resume_cmd": r.get("resume_cmd"),
             "last_line": r.get("last_line", "")}
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
                            "Run `python tools/retro_rank.py` to build it.", "missing_artifact",
                      slug=slug)
    acc = _accepted_entry(slug)
    stale = retro_queue.is_stale(item)
    return 200, {
        "ok": True,
        "finding": item,
        "stale": stale,
        "comment": (acc or {}).get("comment", ""),
        # Exactly the bytes an approval with no comment would dispatch, shown
        # *before* the approve control is used -- approving is the dispatch.
        "prompt_preview": retro_queue.render_prompt(item, ""),
    }


def api_approve(slug: str, comment: str, by: str = "") -> tuple[int, dict]:
    """Record the decision with the comment, then dispatch it (decision 4)."""
    slug = (slug or "").strip()
    if not slug:
        return _error(400, "slug is required", "bad_request")
    item = retro_queue.load_item(slug)
    if item is None:
        return _error(404, f"no dispatch artifact for {slug!r}. "
                            "Run `python tools/retro_rank.py` to build it.", "missing_artifact",
                      slug=slug)
    if retro_queue.is_stale(item):
        # Refused, not silently rebuilt: dispatching a prompt regenerated from
        # a findings file that moved would send something the human did not
        # read, which is the one thing the artifact exists to prevent.
        return _error(409, f"dispatch artifact for {slug!r} is stale: "
                            f"{item.get('source_file', '')} changed since it was written. "
                            "Re-rank with `python tools/retro_rank.py`, re-read the "
                            "finding, and approve again.", "stale", slug=slug)
    title = item.get("title", "")
    ok, err = record_decision(title, "accept", by, comment, comment=comment, slug=slug)
    if not ok:
        return _error(400, err, "bad_request", slug=slug)
    entry = _run_manager.enqueue({"finding": title, "slug": slug,
                                  "prompt": retro_queue.render_prompt(item, comment)})
    if "error" in entry:
        annotate_accepted(slug, state="failed")
        return _error(500, entry["error"], "spawn_failed", slug=slug)
    annotate_accepted(slug, run_id=entry.get("run_id"),
                       state="working" if entry.get("run_id") else "queued")
    finding = next((f for f in finding_states() if f["slug"] == slug), None)
    return 200, {"ok": True, "finding": finding, "run": entry}


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
    entry = _run_manager.spawn_retro()
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
        "silence_minutes": described.get("silence_minutes", 0.0),
        "resume_cmd": entry.get("resume_cmd"),
        "log": entry.get("log"),
        "tail": tail,
    }


# --------------------------------------------------------------- HTTP


def _serve_static(handler: http.server.BaseHTTPRequestHandler, name: str,
                  content_type: str = "text/html; charset=utf-8") -> None:
    path = ROOT / name
    try:
        body = path.read_bytes()
    except OSError:
        handler.send_response(404)
        handler.end_headers()
        return
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(body)))
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
        handler.send_response(404)
        handler.end_headers()
        return
    _serve_static(handler, f"tools/vendor/{name}",
                 content_type="application/javascript; charset=utf-8")


def _json(handler: http.server.BaseHTTPRequestHandler, code: int, payload: dict) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json_body(handler: http.server.BaseHTTPRequestHandler) -> dict:
    try:
        length = int(handler.headers.get("Content-Length", 0))
    except ValueError:
        length = 0
    if not length:
        return {}
    raw = handler.rfile.read(length)
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "board/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003 (stdlib name)
        pass  # goes to board.log via the redirected stdout of the whole process instead

    def _dispatch(self, method: str, path: str, body: dict) -> tuple[int, dict] | None:
        """Route one API call. None means "not an API path"."""
        if method == "GET":
            if path in ("/api/health", "/api/ping"):
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
                                    body.get("by", ""))
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
        body = _read_json_body(self) if method == "POST" else {}
        try:
            result = self._dispatch(method, path, body)
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
            _serve_static(self, "plan.html")
        elif method == "GET" and path == "/retro.html":
            _serve_static(self, "retro.html")
        elif method == "GET" and path.startswith("/tools/vendor/"):
            _serve_vendor(self, path)
        else:
            _json(self, 404, {"ok": False, "code": "not_found",
                               "error": f"no such path: {path}"})

    def do_GET(self) -> None:  # noqa: N802 (stdlib method name)
        self._handle("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._handle("POST")


def serve(port: int) -> int:
    # Self-heal on start: a decision file from the pre-comment pipeline must
    # not render as a queued item nobody is working on (see migrate_accepted).
    migrate_accepted_file()
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
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
