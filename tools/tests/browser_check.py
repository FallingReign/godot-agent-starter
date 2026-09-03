#!/usr/bin/env python3
"""Load the generated pages in a real browser and assert on what it rendered.

The node harness (`dom_harness.js`) executes the page's scripts against a stub
DOM, which is enough to prove the error paths. This proves the other half: that
a real browser, over a real loopback board, reaches the states the harness
claims -- including the two a healthy board cannot be asked to produce, a
worker silent for half an hour and a worker that exited non-zero.

    python tools/tests/browser_check.py

It starts the mock board on a spare port, renders each page with headless Chrome
or Edge, and checks the resulting DOM. Direct Chromium binaries use `--dump-dom`;
the macOS automation build uses an already-installed ChromeDriver (hosted
runners provide the paired build). Skips with exit 0 if no Chromium-based
browser is installed -- a browser is not a dependency of this kit.

States covered: healthy, board-unreachable, file:// read-only, and per-item
working/stalled, failed, done, queued.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "tools"))
import mock_board  # noqa: E402
import page_parts  # noqa: E402
import kit_change_html  # noqa: E402
import plan_html  # noqa: E402
import retro_html  # noqa: E402

MAC_TEST_BROWSER = (
    "/Applications/Google Chrome for Testing.app/Contents/MacOS/"
    "Google Chrome for Testing"
)
PROCESS_SIGTERM = getattr(signal, "SIGTERM", 15)
PROCESS_SIGKILL = getattr(signal, "SIGKILL", 9)

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    # GitHub's hosted macOS images install the automation-specific binary
    # alongside consumer Chrome. Prefer the browser built for automation.
    MAC_TEST_BROWSER,
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

failures: list[str] = []
checks = 0


@dataclass
class RunningBoard:
    server: mock_board.board.BoardHTTPServer
    thread: threading.Thread
    previous_root: Path
    previous_scenario: str
    previous_items: list[dict] | None

    def poll(self) -> int | None:
        return None if self.thread.is_alive() else 0


KIT_CHANGE_SESSION = "a" * 64
KIT_CHANGE_PLAN_SHA = "b" * 64
KIT_CHANGE_RESULT_SHA = "c" * 64
KIT_CHANGE_READY_SESSION = "d" * 64
KIT_CHANGE_READY_PLAN_SHA = "e" * 64
KIT_CHANGE_RECOVERY_SESSION = "f" * 64
KIT_CHANGE_CHECKED_AT = "2026-09-03T12:34:56Z"


def _kit_change_state(status: str, **changes) -> dict:
    state = {
        "session_id": KIT_CHANGE_SESSION,
        "status": status,
        "detail": f"Fixture state: {status}.",
        "plan_sha256": KIT_CHANGE_PLAN_SHA,
        "result_sha256": "",
        "check_evidence": {"state": "not_run", "checked_at": ""},
        "existing_gaps": {"status": "not_checked", "count": 0, "issues": []},
    }
    state.update(changes)
    return state


class KitChangeBrowserHandler(mock_board.Handler):
    """A deterministic lifecycle API behind the real generated page."""

    lifecycle_state: dict = {}
    lifecycle_requests: list[tuple[str, dict]] = []

    @classmethod
    def reset(cls, state: dict) -> None:
        cls.lifecycle_state = json.loads(json.dumps(state))
        cls.lifecycle_requests = []

    def _handle(self, method: str) -> None:
        path, _, query = self.path.partition("?")
        if method == "GET" and path == "/kit-change.html":
            rejected = mock_board.board._validate_host(self)
            if rejected is not None:
                mock_board.board._json(self, rejected[0], rejected[1])
                return
            expected_query = f"session={KIT_CHANGE_READY_SESSION}"
            if query != expected_query:
                mock_board.board._json(
                    self,
                    404,
                    {"ok": False, "code": "not_found", "error": "unknown fixture review"},
                )
                return
            page = kit_change_html.render(_kit_change_ready_preview())
            page = page.replace("</body>", KIT_CHANGE_READY_PROBE + "</body>", 1)
            mock_board.board._serve_html_text(self, page)
            return
        super()._handle(method)

    def _dispatch(self, method: str, path: str, body: dict):
        cls = type(self)
        if method == "GET" and path == "/api/state":
            payload = mock_board.state_payload()
            payload["kit_change"] = json.loads(json.dumps(cls.lifecycle_state))
            return 200, payload
        if method == "POST" and path.startswith("/api/kit-change/"):
            cls.lifecycle_requests.append((path, json.loads(json.dumps(body))))
            if path == "/api/kit-change/apply":
                session_id = cls.lifecycle_state.get("session_id")
                if session_id == KIT_CHANGE_SESSION:
                    expected = {
                        "session_id": KIT_CHANGE_SESSION,
                        "plan_sha256": KIT_CHANGE_PLAN_SHA,
                        "choices": {"D1": "game"},
                    }
                    if body != expected:
                        return mock_board.board._error(
                            409, "The browser did not submit the exact reviewed decision.",
                            "fixture_request_mismatch",
                        )
                    cls.lifecycle_state = _kit_change_state(
                        "ready",
                        session_id=KIT_CHANGE_READY_SESSION,
                        plan_sha256=KIT_CHANGE_READY_PLAN_SHA,
                    )
                    port = self.server.server_address[1]
                    return 200, {
                        "ok": True,
                        "reprepared": True,
                        "review_url": (
                            f"http://127.0.0.1:{port}/kit-change.html"
                            f"?session={KIT_CHANGE_READY_SESSION}"
                        ),
                        "kit_change": json.loads(json.dumps(cls.lifecycle_state)),
                    }
                expected = {
                    "session_id": KIT_CHANGE_READY_SESSION,
                    "plan_sha256": KIT_CHANGE_READY_PLAN_SHA,
                    "choices": {},
                }
                if session_id != KIT_CHANGE_READY_SESSION or body != expected:
                    return mock_board.board._error(
                        409, "The browser did not submit the exact reviewed decision.",
                        "fixture_request_mismatch",
                    )
                cls.lifecycle_state = _kit_change_state(
                    "complete",
                    session_id=KIT_CHANGE_READY_SESSION,
                    plan_sha256=KIT_CHANGE_READY_PLAN_SHA,
                    result_sha256=KIT_CHANGE_RESULT_SHA,
                    check_evidence={
                        "state": "apply_time",
                        "checked_at": KIT_CHANGE_CHECKED_AT,
                    },
                    existing_gaps={"status": "checked", "count": 0, "issues": []},
                )
            elif path == "/api/kit-change/restore":
                expected = {
                    "session_id": KIT_CHANGE_READY_SESSION,
                    "result_sha256": KIT_CHANGE_RESULT_SHA,
                }
                if body != expected:
                    return mock_board.board._error(
                        409, "The browser did not submit the exact applied result.",
                        "fixture_request_mismatch",
                    )
                cls.lifecycle_state = _kit_change_state(
                    "restored",
                    session_id=KIT_CHANGE_READY_SESSION,
                    plan_sha256=KIT_CHANGE_READY_PLAN_SHA,
                )
            elif path == "/api/kit-change/recover":
                if body != {"session_id": KIT_CHANGE_RECOVERY_SESSION}:
                    return mock_board.board._error(
                        409, "The browser did not submit the exact recovery session.",
                        "fixture_request_mismatch",
                    )
                cls.lifecycle_state = _kit_change_state(
                    "restored",
                    session_id=KIT_CHANGE_RECOVERY_SESSION,
                )
            else:
                return mock_board.board._error(
                    404, "Unknown fixture lifecycle action.", "not_found"
                )
            return 200, {
                "ok": True,
                "kit_change": json.loads(json.dumps(cls.lifecycle_state)),
            }
        return super()._dispatch(method, path, body)


def check(scenario: str, name: str, cond: bool, extra: str = "") -> None:
    global checks
    checks += 1
    if cond:
        print(f"  ok   {scenario} | {name}")
    else:
        print(f"  FAIL {scenario} | {name} {extra}")
        failures.append(f"{scenario}: {name} {extra}")


def find_browser() -> str:
    for variable in ("BROWSER_BIN", "CHROME_BIN"):
        configured = os.environ.get(variable, "").strip()
        if not configured:
            continue
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        found = shutil.which(configured)
        if found:
            return found
    for path in BROWSERS:
        if Path(path).exists():
            return path
    return ""


def find_chromedriver() -> str:
    configured = os.environ.get("CHROMEDRIVER_BIN", "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate.resolve())
        found = shutil.which(configured)
        if found:
            return found
    driver_root = os.environ.get("CHROMEWEBDRIVER", "").strip()
    if driver_root:
        name = "chromedriver.exe" if os.name == "nt" else "chromedriver"
        candidate = Path(driver_root).expanduser() / name
        if candidate.is_file():
            return str(candidate.resolve())
    return shutil.which("chromedriver") or ""


def _is_mac_test_browser(browser: str) -> bool:
    candidate = Path(browser).expanduser()
    target = Path(MAC_TEST_BROWSER)
    try:
        return candidate.resolve(strict=True) == target.resolve(strict=True)
    except OSError:
        return str(candidate) == str(target)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _dump_dom_direct(browser: str, url: str, profile: Path) -> str:
    try:
        proc = subprocess.run(
            [browser, "--headless", "--disable-gpu", "--no-sandbox",
             "--no-proxy-server",
             f"--user-data-dir={profile}", "--virtual-time-budget=6000",
             "--timeout=15000",
             "--dump-dom", url],
            capture_output=True, text=True, errors="replace", timeout=45,
        )
    except subprocess.TimeoutExpired:
        print(f"browser dump timed out for {url}", file=sys.stderr)
        return ""
    if proc.returncode != 0 or not proc.stdout.strip():
        detail = (proc.stderr or "browser returned an empty document").strip()[:500]
        print(f"browser dump failed for {url}: {detail}", file=sys.stderr)
    return proc.stdout


def _webdriver_json(
    method: str,
    url: str,
    payload: dict | None = None,
    timeout: float = 15.0,
) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"WebDriver HTTP {exc.code}: {detail}") from exc
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("WebDriver returned a non-object response")
    return parsed


def _webdriver_capabilities(browser: str, profile: Path) -> dict:
    return {
        "capabilities": {
            "alwaysMatch": {
                "browserName": "chrome",
                "goog:chromeOptions": {
                    "binary": browser,
                    "args": [
                        "--headless",
                        "--disable-gpu",
                        "--no-sandbox",
                        "--no-proxy-server",
                        f"--user-data-dir={profile}",
                    ],
                },
            },
        },
    }


def _signal_driver_tree(
    process: subprocess.Popen[str],
    force: bool,
) -> None:
    if os.name == "posix":
        selected = PROCESS_SIGKILL if force else PROCESS_SIGTERM
        try:
            os.killpg(process.pid, selected)
            return
        except ProcessLookupError:
            return
        except OSError:
            pass
    if process.poll() is not None:
        return
    if force:
        process.kill()
    else:
        process.terminate()


def _process_group_exited(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            pass
        time.sleep(0.1)
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass
    return False


def _stop_driver_tree(process: subprocess.Popen[str]) -> None:
    if os.name == "posix":
        _signal_driver_tree(process, force=False)
        leader_reaped = True
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            leader_reaped = False
        if not _process_group_exited(process.pid, timeout=5.0):
            _signal_driver_tree(process, force=True)
            if not _process_group_exited(process.pid, timeout=5.0):
                print(
                    "ChromeDriver process group survived SIGKILL",
                    file=sys.stderr,
                )
        if not leader_reaped:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("ChromeDriver leader was not reaped", file=sys.stderr)
        return
    _signal_driver_tree(process, force=False)
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal_driver_tree(process, force=True)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        print("ChromeDriver process did not stop after termination", file=sys.stderr)


def _dump_dom_with_webdriver(
    driver: str,
    browser: str,
    url: str,
    profile: Path,
) -> str:
    port = free_port()
    session_id = ""
    try:
        process = subprocess.Popen(
            [driver, f"--port={port}", "--allowed-ips=127.0.0.1",
             "--log-level=SEVERE"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        print(f"ChromeDriver could not start: {exc}", file=sys.stderr)
        return ""
    base_url = f"http://127.0.0.1:{port}"
    try:
        if not wait_for(port, timeout=10.0, process=process):
            raise RuntimeError("ChromeDriver did not open its loopback endpoint")
        request_profile = profile / f"webdriver-{uuid.uuid4().hex}"
        request_profile.mkdir(parents=True)
        created = _webdriver_json(
            "POST",
            f"{base_url}/session",
            _webdriver_capabilities(browser, request_profile),
            timeout=30.0,
        )
        value = created.get("value")
        if isinstance(value, dict):
            session_id = str(value.get("sessionId", ""))
        if not session_id:
            session_id = str(created.get("sessionId", ""))
        if not session_id:
            raise RuntimeError("ChromeDriver did not return a session id")
        session_url = f"{base_url}/session/{session_id}"
        _webdriver_json(
            "POST",
            f"{session_url}/timeouts",
            {"implicit": 0, "pageLoad": 15000, "script": 15000},
        )
        _webdriver_json("POST", f"{session_url}/url", {"url": url}, timeout=20.0)
        deadline = time.monotonic() + 20.0
        html = ""
        while time.monotonic() < deadline:
            rendered = _webdriver_json(
                "POST",
                f"{session_url}/execute/sync",
                {
                    "script": "return document.documentElement.outerHTML;",
                    "args": [],
                },
            )
            current = rendered.get("value")
            html = current if isinstance(current, str) else ""
            if ('data-board-ready="true"' in html
                    and 'data-browser-probe-pending="true"' not in html):
                return html
            time.sleep(0.2)
        return html
    except Exception as exc:
        print(f"WebDriver dump failed for {url}: {exc}", file=sys.stderr)
        return ""
    finally:
        if session_id:
            try:
                _webdriver_json(
                    "DELETE",
                    f"{base_url}/session/{session_id}",
                    timeout=5.0,
                )
            except Exception:
                pass
        _stop_driver_tree(process)


def dump_dom(browser: str, url: str, profile: Path) -> str:
    driver = find_chromedriver()
    if _is_mac_test_browser(browser) and driver:
        return _dump_dom_with_webdriver(driver, browser, url, profile)
    return _dump_dom_direct(browser, url, profile)


def body_class(html: str) -> str:
    m = re.search(r"<body[^>]*>", html)
    return m.group(0) if m else ""


def banner(html: str, elid: str) -> str:
    """Inner content of a banner element.

    Deliberately crude: the banners are flat lists of sibling divs, so rather
    than balancing tags, take the window after the opening tag and cut at the
    next section heading. Enough to assert on the words a human would read.
    """
    m = re.search(r'<div id="%s"[^>]*>' % elid, html)
    if not m:
        return ""
    window = html[m.end():m.end() + 2000]
    # Walk to the matching close so an empty banner reads as empty rather than
    # swallowing whatever follows it on the page.
    depth, i = 1, 0
    while i < len(window):
        opened = window.find("<div", i)
        closed = window.find("</div>", i)
        if closed < 0:
            break
        if 0 <= opened < closed:
            depth += 1
            i = opened + 4
            continue
        depth -= 1
        if depth == 0:
            return window[:closed]
        i = closed + 6
    return window


def controls(html: str) -> tuple[int, int]:
    tags = re.findall(r"<(?:button|textarea|input)[^>]*data-board-control[^>]*>", html)
    return len(tags), len([t for t in tags if "disabled" in t])


def element_tag(html: str, element_id: str) -> str:
    """Return one rendered opening tag by id for visibility assertions."""
    match = re.search(
        r'<[^>]+\bid="' + re.escape(element_id) + r'"[^>]*>',
        html,
    )
    return match.group(0) if match else ""


def body_attribute(html: str, name: str) -> str:
    """Read a fixture probe value serialized onto the rendered body."""
    match = re.search(
        r'<body[^>]*\b' + re.escape(name) + r'="([^"]*)"',
        html,
    )
    return match.group(1) if match else ""


def _costs_descend(cards: list[dict[str, str]]) -> bool:
    """Cost descending, with every unranked card after every ranked one."""
    seq = [None if c.get("data-cost", "") == "" else float(c["data-cost"]) for c in cards]
    ranked = [v for v in seq if v is not None]
    if seq != ranked + [None] * (len(seq) - len(ranked)):
        return False
    return all(ranked[i - 1] >= ranked[i] for i in range(1, len(ranked)))


STATE_CLASS_RE = re.compile(r'class="status-slot ([a-z_]+)"')


def _state_order(html: str) -> str:
    return str(STATE_CLASS_RE.findall(page_parts.section(html, "list-approved")))


def _states_ascend(html: str) -> bool:
    """The rendered status pills of the approved list follow the stated rule."""
    seen = STATE_CLASS_RE.findall(page_parts.section(html, "list-approved"))
    ranks = [retro_html.APPROVED_STATE_ORDER.get(s, retro_html.UNKNOWN_STATE_RANK)
             for s in seen]
    return all(ranks[i - 1] <= ranks[i] for i in range(1, len(ranks)))


def wait_for(
    port: int,
    timeout: float = 10.0,
    process: RunningBoard | subprocess.Popen[str] | None = None,
) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if process is not None and process.poll() is not None:
            return False
        try:
            # Readiness is only "the child owns a listening loopback socket".
            # A direct TCP probe cannot be redirected by a host HTTP proxy,
            # which matters on hosted macOS runners. The browser scenarios
            # exercise the actual health response immediately afterwards.
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


def start_board(
    port: int,
    scenario: str,
    fixture_root: Path,
    handler_cls: type[mock_board.Handler] = mock_board.Handler,
) -> RunningBoard:
    fixture_items = json.loads(
        (fixture_root / "fixture-items.json").read_text(encoding="utf-8")
    )
    if not isinstance(fixture_items, list) or not all(
        isinstance(item, dict) for item in fixture_items
    ):
        raise RuntimeError("fixture-items.json must contain an array of objects")
    previous_root = mock_board.board.ROOT
    previous_scenario = mock_board.SCENARIO
    previous_items = mock_board.FIXTURE_ITEMS
    try:
        mock_board.SCENARIO = scenario
        mock_board.FIXTURE_ITEMS = fixture_items
        mock_board.board.ROOT = fixture_root
        server = mock_board.board.BoardHTTPServer(
            ("127.0.0.1", port), handler_cls
        )
    except BaseException:
        mock_board.board.ROOT = previous_root
        mock_board.SCENARIO = previous_scenario
        mock_board.FIXTURE_ITEMS = previous_items
        raise
    thread = threading.Thread(
        target=server.serve_forever,
        name=f"browser-check-board-{scenario}",
        daemon=True,
    )
    running = RunningBoard(
        server,
        thread,
        previous_root,
        previous_scenario,
        previous_items,
    )
    thread.start()
    if not wait_for(port, timeout=5.0, process=running):
        stop_board(running)
        raise RuntimeError(
            f"mock board did not become ready for scenario {scenario}"
        )
    return running


def stop_board(running: RunningBoard) -> None:
    try:
        running.server.shutdown()
        running.server.server_close()
        running.thread.join(timeout=5)
        if running.thread.is_alive():
            raise RuntimeError("mock board thread did not stop")
    finally:
        mock_board.board.ROOT = running.previous_root
        mock_board.SCENARIO = running.previous_scenario
        mock_board.FIXTURE_ITEMS = running.previous_items


def _fixture_items() -> list[dict]:
    titles = (
        "awaiting review fixture",
        "approved fixture",
        "silent worker fixture",
        "blocked worker fixture",
        "unverified worker fixture",
        "failed worker fixture",
        "finished worker fixture",
        "queued worker fixture",
    )
    return [
        {
            "slug": retro_html.retro_queue.slug_for(title),
            "title": title,
            "severity": "none",
            "dispatchable": True,
            "dispatch_blockers": [],
            "evidence_snapshot": "fixture",
            "generated_at": "2026-08-19T00:00:00Z",
            "prompt": f"Implement the reviewed correction for {title}.",
        }
        for title in titles
    ]


def _fixture_findings(items: list[dict]) -> str:
    blocks = ["# Deterministic browser fixture", ""]
    for index, item in enumerate(items):
        blocks.extend([
            f"## Finding: {item['title']}",
            "",
            "sessions: []",
            "human_turns: []",
            "mechanical: []",
            "recurs: false",
            "severity: none",
            f"fix_files: [tools/browser-fixture-{index + 1}.py]",
            f"fix_lines: {index + 1}",
            f"cost: {80 - index * 5}.0",
            "effort: 1",
            "",
            f"**Problem** - Browser state {index + 1} must be readable.",
            "",
            f"**Proposal** - Render browser state {index + 1} on its decision card.",
            "",
            f"**Measure** - Browser state {index + 1} appears in the real rendered DOM.",
            "",
        ])
    return "\n".join(blocks)


def _kit_change_fixture_preview() -> dict:
    return {
        "mode": "install",
        "project": {"name": "Lifecycle fixture", "path": "C:/fixture/game"},
        "current_version": "Not installed",
        "incoming_version": "0.3.0",
        "status": "needs_decision",
        "session_id": KIT_CHANGE_SESSION,
        "plan_sha256": KIT_CHANGE_PLAN_SHA,
        "counts": {
            "kit_files": 12,
            "shared_files": 2,
            "removed_files": 0,
            "game_files": 0,
        },
        "decisions": [{
            "id": "D1",
            "question": "Which folder contains the game?",
            "choices": [{
                "value": "game",
                "label": "game",
                "description": "Use game as the game folder.",
                "recommended": False,
            }],
        }],
        "files": [{
            "path": "kit.cmd",
            "action": "Add",
            "reason": "Expose the managed kit command.",
        }],
    }


def _kit_change_ready_preview() -> dict:
    preview = _kit_change_fixture_preview()
    preview.update({
        "status": "ready",
        "session_id": KIT_CHANGE_READY_SESSION,
        "plan_sha256": KIT_CHANGE_READY_PLAN_SHA,
        "decisions": [],
    })
    return preview


def _kit_change_recovery_preview() -> dict:
    preview = _kit_change_fixture_preview()
    preview.update({
        "status": "recovery_required",
        "session_id": KIT_CHANGE_RECOVERY_SESSION,
        "decisions": [],
    })
    return preview


KIT_CHANGE_DECISION_PROBE = r"""
<script>
(function(){
  var body=document.body;
  body.setAttribute("data-browser-probe-pending","true");
  function finish(error){
    if(error)body.setAttribute("data-kit-change-probe-error",String(error));
    body.removeAttribute("data-browser-probe-pending");
  }
  function waitFor(label,test,next,attempt){
    attempt=attempt||0;
    try{
      if(test()){next();return;}
      if(attempt>=200){finish("Timed out waiting for "+label);return;}
      setTimeout(function(){waitFor(label,test,next,attempt+1);},20);
    }catch(error){finish(String(error));}
  }
  waitFor("the live review",function(){
    return body.getAttribute("data-board-ready")==="true"
      && window.Board && Board.mode()==="live";
  },function(){
    var apply=document.getElementById("kit-change-apply");
    var choice=document.querySelector('[data-decision-id="D1"]');
    sessionStorage.setItem("kit-change-initial-disabled",String(apply.disabled));
    choice.click();
    sessionStorage.setItem("kit-change-decision-enabled",String(!apply.disabled));
    if(apply.disabled){finish("Apply stayed disabled after the decision");return;}
    apply.click();
  });
})();
</script>
"""


KIT_CHANGE_READY_PROBE = r"""
<script>
(function(){
  var body=document.body;
  body.setAttribute("data-browser-probe-pending","true");
  function record(name,value){body.setAttribute(name,String(value));}
  function finish(error){
    if(error)record("data-kit-change-probe-error",error);
    else record("data-kit-change-probe-ran","true");
    body.removeAttribute("data-browser-probe-pending");
  }
  function waitFor(label,test,next,attempt){
    attempt=attempt||0;
    try{
      if(test()){next();return;}
      if(attempt>=200){finish("Timed out waiting for "+label);return;}
      setTimeout(function(){waitFor(label,test,next,attempt+1);},20);
    }catch(error){finish(String(error));}
  }
  waitFor("the second review",function(){
    var apply=document.getElementById("kit-change-apply");
    return body.getAttribute("data-board-ready")==="true"
      && body.getAttribute("data-kit-change-status")==="ready"
      && window.Board && Board.mode()==="live" && apply && !apply.disabled;
  },function(){
    record("data-kit-change-probe-initial-disabled",
      sessionStorage.getItem("kit-change-initial-disabled")||"");
    record("data-kit-change-probe-decision-enabled",
      sessionStorage.getItem("kit-change-decision-enabled")||"");
    record("data-kit-change-probe-second-review",
      location.pathname+location.search);
    document.getElementById("kit-change-apply").click();
    waitFor("Apply completion",function(){
      return body.getAttribute("data-kit-change-status")==="complete";
    },function(){
      var restore=document.getElementById("kit-change-restore");
      record("data-kit-change-probe-apply-status",
        body.getAttribute("data-kit-change-status")||"");
      record("data-kit-change-probe-checked-at",
        document.getElementById("kit-change-checked-at").textContent.trim());
      record("data-kit-change-probe-restore-enabled",
        !restore.hidden && !restore.disabled);
      if(restore.hidden||restore.disabled){
        finish("Restore was not enabled after Apply");return;
      }
      restore.click();
      waitFor("Restore completion",function(){
        return body.getAttribute("data-kit-change-status")==="restored";
      },function(){
        record("data-kit-change-probe-restore-status",
          body.getAttribute("data-kit-change-status")||"");
        record("data-kit-change-probe-review-enabled",
          !document.getElementById("kit-change-review-again").disabled);
        record("data-kit-change-probe-errors",
          document.querySelectorAll(".board-error").length);
        finish("");
      });
    });
  });
})();
</script>
"""


KIT_CHANGE_RECOVERY_PROBE = r"""
<script>
(function(){
  var body=document.body;
  body.setAttribute("data-browser-probe-pending","true");
  function record(name,value){body.setAttribute(name,String(value));}
  function finish(error){
    if(error)record("data-kit-change-recovery-error",error);
    else record("data-kit-change-recovery-ran","true");
    body.removeAttribute("data-browser-probe-pending");
  }
  function waitFor(label,test,next,attempt){
    attempt=attempt||0;
    try{
      if(test()){next();return;}
      if(attempt>=200){finish("Timed out waiting for "+label);return;}
      setTimeout(function(){waitFor(label,test,next,attempt+1);},20);
    }catch(error){finish(String(error));}
  }
  waitFor("the recovery action",function(){
    var recover=document.getElementById("kit-change-recover");
    return body.getAttribute("data-board-ready")==="true"
      && body.getAttribute("data-kit-change-status")==="recovery_required"
      && recover && !recover.hidden && !recover.disabled;
  },function(){
    document.getElementById("kit-change-recover").click();
    waitFor("Recovery completion",function(){
      return body.getAttribute("data-kit-change-status")==="restored";
    },function(){
      record("data-kit-change-recovery-status",
        body.getAttribute("data-kit-change-status")||"");
      record("data-kit-change-recovery-review-enabled",
        !document.getElementById("kit-change-review-again").disabled);
      record("data-kit-change-recovery-errors",
        document.querySelectorAll(".board-error").length);
      finish("");
    });
  });
})();
</script>
"""


@contextlib.contextmanager
def browser_fixture():
    configured = os.environ.get("KIT_TEST_TMPDIR", "").strip()
    base = Path(configured) if configured else ROOT / ".checklogs" / "tests"
    fixture_root = base / f"browser-check-{uuid.uuid4().hex}"
    fixture_root.mkdir(parents=True)
    try:
        items = _fixture_items()
        findings = fixture_root / "fixture-findings.md"
        findings.write_text(_fixture_findings(items), encoding="utf-8")
        by_title = {
            retro_html.retro_rank.normalise_title(item["title"]): item for item in items
        }
        with mock.patch.object(retro_html, "load_accepted", return_value=[]), \
                mock.patch.object(retro_html.retro_rank, "load_deferred", return_value=[]), \
                mock.patch.object(retro_html.retro_queue, "is_stale", return_value=False), \
                mock.patch.object(
                    retro_html.retro_queue,
                    "load_by_title",
                    side_effect=lambda title: by_title.get(
                        retro_html.retro_rank.normalise_title(title)
                    ),
                ), \
                mock.patch.object(
                    retro_html.retro_queue,
                    "render_prompt",
                    side_effect=lambda item, comment: item["prompt"] + (
                        f"\n\n{comment}" if comment else ""
                    ),
                ):
            rendered = retro_html.render([findings])
        (fixture_root / "retro.html").write_text(rendered, encoding="utf-8")
        shape = {
            "name": "Architecture browser fixture",
            "pitch": "A deterministic real-browser architecture-map fixture.",
            "involvement": "function",
        }
        proposal = {
            "slice": "browser-architecture-map",
            "status": "draft",
            "experience": {
                "player_does": "Submits one action.",
                "feels_like": "The result is immediate and understandable.",
                "not_this": "A silent action with an unexplained result.",
            },
            "design_refs": [{
                "section": "docs/design/experience/action-feedback.md",
                "why": "Every accepted action explains its result immediately.",
                "sha256": "a" * 64,
            }],
            "design_authority": {
                "authority": "human-confirmed",
                "authored_by": "Fixture author",
                "confidence": "very-high",
            },
            "reversibility": {
                "state": "reversible",
                "veto_scope": "Remove the new feedback file and restore the old return type.",
                "hard_to_undo": "Other systems begin consuming the new feedback value.",
                "next_go_no_go": "Before a second system consumes FeedbackEvent.",
            },
            "considered_existing": [{
                "path": "scripts/logic/action_service.gd",
                "why_not": "It owns action rules, not the typed result shared with presentation.",
            }],
            "modules": [{
                "path": "scripts/logic",
                "role": "Engine-independent gameplay decisions and typed results.",
                "why": "The action result needs one explicit typed boundary.",
                "action": "modify",
                "may_depend_on": [],
                "boundary_data": "PlayerAction enters; FeedbackEvent leaves.",
            }],
            "files": [{
                "path": "scripts/logic/action_service.gd",
                "action": "modify",
                "why": "Return an explicit result that presentation can understand.",
                "module": "scripts/logic",
            }, {
                "path": "scripts/logic/feedback_event.gd",
                "action": "new",
                "why": "Carry the action result without coupling logic to a scene.",
                "module": "scripts/logic",
            }],
            "functions": [{
                "file": "scripts/logic/action_service.gd",
                "signature": "func submit_action(action: PlayerAction) -> FeedbackEvent",
                "why": "Return the typed feedback value from the existing action boundary.",
                "action": "modify",
                "module": "scripts/logic",
            }, {
                "file": "scripts/logic/feedback_event.gd",
                "signature": "func create(summary: String) -> FeedbackEvent",
                "why": "Construct one complete result at the typed boundary.",
                "action": "new",
                "module": "scripts/logic",
            }],
        }
        tree = {
            "scripts/logic/action_service.gd": {
                "class_name": "ActionService",
                "functions": [{
                    "name": "submit_action",
                    "identity": "submit_action",
                    "class_scope": "",
                    "signature": "func submit_action(action: PlayerAction) -> FeedbackEvent",
                    "private": False,
                }],
            },
            "scripts/logic/unchanged_helper.gd": {
                "class_name": "UnchangedHelper",
                "functions": [{
                    "name": "describe",
                    "identity": "describe",
                    "class_scope": "",
                    "signature": "func describe() -> String",
                    "private": False,
                }],
            },
        }

        def render_architecture_fixture(
            involvement: str,
            fixture_proposal: dict,
            fixture_tree: dict,
            fixture_mods: list[dict],
            fixture_built: set[str] | None = None,
            fixture_actions: dict[str, str] | None = None,
        ) -> str:
            fixture_shape = {**shape, "involvement": involvement}
            return plan_html.render(
                fixture_shape,
                fixture_proposal,
                fixture_mods,
                "",
                fixture_built or set(),
                [],
                [],
                [],
                "",
                tree=fixture_tree,
                changed_actions=fixture_actions or {},
                present_files=set(fixture_tree) | set(fixture_built or set()),
                baseline_files=set(fixture_tree),
                cockpit_state={
                    "status": "draft",
                    "verification": {
                        "status": "passed",
                        "summary": "Static verification passed for the browser fixture.",
                    },
                },
            )

        rendered_plan = render_architecture_fixture(
            "function",
            proposal,
            tree,
            [{"path": "scripts/logic", "depends_on": []}],
            {"scripts/logic/action_service.gd"},
            {"scripts/logic/action_service.gd": "modify"},
        )

        map_marker = '<script>window.__KIT_ARCHITECTURE_MAP__='
        if map_marker not in rendered_plan:
            raise RuntimeError("architecture fixture did not render the map script")
        force_svg_failure = (
            '<script>window.__fixtureSvgFailureCalls=0;'
            'document.createElementNS=function(){'
            'window.__fixtureSvgFailureCalls+=1;'
            'throw new Error("fixture forced SVG construction failure");};</script>'
        )
        probe = """
<script>
(function(){
  try {
    var root=document.getElementById("architecture-map");
    var search=document.getElementById("arch-search");
    var complete=root.querySelector('[data-arch-view="complete"]');
    var changes=root.querySelector('[data-arch-view="changes"]');
    var renderedCount=function(){
      if(root.getAttribute("data-render-mode")==="graph")return root.querySelectorAll(".arch-node").length;
      return Array.from(root.querySelectorAll("[data-arch-item]")).filter(function(item){return !item.hidden;}).length;
    };
    var completeCount=renderedCount();
    document.body.setAttribute("data-arch-probe-complete-default",String(
      complete.getAttribute("aria-pressed")==="true" && changes.getAttribute("aria-pressed")==="false"));
    changes.click();
    var changesCount=renderedCount();
    document.body.setAttribute("data-arch-probe-changes-pressed",String(
      changes.getAttribute("aria-pressed")==="true" && complete.getAttribute("aria-pressed")==="false"));
    complete.click();
    var restoredCount=renderedCount();
    document.body.setAttribute("data-arch-probe-complete-restored",String(
      complete.getAttribute("aria-pressed")==="true" && changes.getAttribute("aria-pressed")==="false"));
    document.body.setAttribute("data-arch-probe-complete-count",String(completeCount));
    document.body.setAttribute("data-arch-probe-changes-count",String(changesCount));
    document.body.setAttribute("data-arch-probe-restored-count",String(restoredCount));
    if(root.getAttribute("data-render-mode")==="fallback"){
      root.querySelector('[data-arch-depth="file"]').click();
      root.querySelector('[data-arch-depth="module"]').click();
      root.querySelector('[data-arch-depth="function"]').click();
      document.body.setAttribute("data-arch-probe-svg-failure-calls",String(
        window.__fixtureSvgFailureCalls||0));
    }
    var roving=root.querySelectorAll('.arch-node[tabindex="0"]');
    var before=roving.length===1 ? roving[0].getAttribute("data-node-id") : "";
    if(roving.length===1)roving[0].dispatchEvent(new KeyboardEvent("keydown",{
      key:"ArrowRight",bubbles:true,cancelable:true}));
    var moved=root.querySelectorAll('.arch-node[tabindex="0"]');
    var after=moved.length===1 ? moved[0].getAttribute("data-node-id") : "";
    document.body.setAttribute("data-arch-probe-roving-count",String(moved.length));
    document.body.setAttribute("data-arch-probe-arrow-moved",String(
      Boolean(before && after && before!==after)));
    document.body.setAttribute("data-arch-probe-arrow-focused",String(
      moved.length===1 && document.activeElement===moved[0]));
    search.value="feedback_event";
    search.dispatchEvent(new Event("input",{bubbles:true}));
    search.dispatchEvent(new KeyboardEvent("keydown",{key:"Enter",bubbles:true}));
    document.body.setAttribute("data-arch-probe-ran","true");
    document.body.setAttribute("data-arch-probe-mode",root.getAttribute("data-render-mode")||"");
    document.body.setAttribute("data-arch-probe-selected",
      (document.getElementById("arch-selected-title").textContent||"").trim());
    document.body.setAttribute("data-arch-probe-count",
      (document.getElementById("arch-search-count").textContent||"").trim());
  } catch(error) {
    document.body.setAttribute("data-arch-probe-error",String(error));
  }
})();
</script>
"""

        normal_plan = rendered_plan.replace("</body>", probe + "</body>", 1)
        fallback_plan = rendered_plan.replace(
            map_marker,
            force_svg_failure + map_marker,
            1,
        ).replace("</body>", probe + "</body>", 1)
        (fixture_root / "plan.html").write_text(normal_plan, encoding="utf-8")
        (fixture_root / "plan-fallback.html").write_text(
            fallback_plan,
            encoding="utf-8",
        )

        unchanged_tree = {
            "scripts/logic/stable.gd": {
                "class_name": "Stable",
                "functions": [{
                    "name": "read",
                    "identity": "read",
                    "class_scope": "",
                    "signature": "func read() -> String",
                    "private": False,
                }],
            },
        }
        unchanged_proposal = {
            "slice": "browser-no-changes",
            "status": "draft",
            "experience": proposal["experience"],
        }
        no_changes_probe = """
<script>
(function(){
  try {
    document.body.setAttribute("data-browser-probe-pending","true");
    var root=document.getElementById("architecture-map");
    var completeCount=root.querySelectorAll(".arch-node").length;
    root.querySelector('[data-arch-view="changes"]').click();
    var firstCount=root.querySelectorAll(".arch-node").length;
    window.dispatchEvent(new Event("resize"));
    setTimeout(function(){
      document.body.setAttribute("data-zero-complete-count",String(completeCount));
      document.body.setAttribute("data-zero-changes-count",String(firstCount));
      document.body.setAttribute("data-zero-stable-count",String(root.querySelectorAll(".arch-node").length));
      document.body.setAttribute("data-zero-mode",root.getAttribute("data-render-mode")||"");
      document.body.setAttribute("data-zero-fallback-hidden",String(document.getElementById("arch-fallback").hidden));
      document.body.removeAttribute("data-browser-probe-pending");
    },180);
  } catch(error) {
    document.body.setAttribute("data-zero-error",String(error));
    document.body.removeAttribute("data-browser-probe-pending");
  }
})();
</script>
"""
        no_changes_page = render_architecture_fixture(
            "function",
            unchanged_proposal,
            unchanged_tree,
            [{"path": "scripts/logic", "depends_on": []}],
        ).replace("</body>", no_changes_probe + "</body>", 1)
        (fixture_root / "plan-no-changes.html").write_text(
            no_changes_page,
            encoding="utf-8",
        )

        involvement_probe = """
<script>
(function(){
  var root=document.getElementById("architecture-map");
  document.body.setAttribute("data-involvement-mode",root.getAttribute("data-render-mode")||"");
  document.body.setAttribute("data-involvement-max",root.getAttribute("data-max-depth")||"");
  document.body.setAttribute("data-involvement-eyebrow",
    (root.querySelector(".arch-intro .arch-eyebrow").textContent||"").trim());
  var involvementCopy=(root.querySelector(".arch-intro-copy").textContent||"").trim();
  document.body.setAttribute("data-involvement-copy",involvementCopy
    .split(String.fromCharCode(8594)).join("to"));
  document.body.setAttribute("data-involvement-placeholder",
    document.getElementById("arch-search").getAttribute("placeholder")||"");
  document.body.setAttribute("data-involvement-heading",
    (root.querySelector(".arch-map-heading h3").textContent||"").trim());
  document.body.setAttribute("data-involvement-depths",Array.from(
    root.querySelectorAll("[data-arch-depth]:not([disabled])")).map(function(button){
      return button.getAttribute("data-arch-depth");
    }).join("|"));
  document.body.setAttribute("data-involvement-node-ids",Array.from(
    root.querySelectorAll(".arch-node")).map(function(node){
      return node.getAttribute("data-node-id");
    }).join("|"));
})();
</script>
"""
        for involvement in ("module", "file", "function"):
            involvement_page = render_architecture_fixture(
                involvement,
                proposal,
                tree,
                [{"path": "scripts/logic", "depends_on": []}],
                {"scripts/logic/action_service.gd"},
                {"scripts/logic/action_service.gd": "modify"},
            ).replace("</body>", involvement_probe + "</body>", 1)
            (fixture_root / f"plan-{involvement}.html").write_text(
                involvement_page,
                encoding="utf-8",
            )

        large_tree = {}
        for index in range(180):
            stem = f"item_{index:03d}"
            large_tree[f"scripts/logic/generated/{stem}.gd"] = {
                "class_name": f"Generated{index:03d}",
                "functions": [{
                    "name": "first",
                    "identity": "first",
                    "class_scope": "",
                    "signature": "func first() -> int",
                    "private": False,
                }, {
                    "name": "second",
                    "identity": "second",
                    "class_scope": "",
                    "signature": "func second() -> int",
                    "private": False,
                }],
            }
        async_graph_probe = """
<script>
(function(){
  document.body.setAttribute("data-browser-probe-pending","true");
  var root=document.getElementById("architecture-map"),attempts=0;
  function finish(){
    var mode=root.getAttribute("data-render-mode")||"";
    if((mode!=="graph" && mode!=="fallback") && attempts<240){
      attempts+=1;setTimeout(finish,25);return;
    }
    document.body.setAttribute("data-large-mode",mode);
    document.body.setAttribute("data-large-nodes",String(root.querySelectorAll(".arch-node").length));
    document.body.setAttribute("data-large-edges",String(root.querySelectorAll(".arch-edge").length));
    document.body.setAttribute("data-large-fallback-hidden",String(document.getElementById("arch-fallback").hidden));
    document.body.setAttribute("data-large-busy",document.getElementById("arch-graph").getAttribute("aria-busy")||"");
    document.body.removeAttribute("data-browser-probe-pending");
  }
  finish();
})();
</script>
"""
        large_page = render_architecture_fixture(
            "function",
            unchanged_proposal,
            large_tree,
            [{"path": "scripts/logic", "depends_on": []}],
        ).replace("</body>", async_graph_probe + "</body>", 1)
        (fixture_root / "plan-large.html").write_text(large_page, encoding="utf-8")

        map_data_end = rendered_plan.find(";</script>", rendered_plan.find(map_marker))
        if map_data_end < 0:
            raise RuntimeError("architecture fixture did not expose its data boundary")
        map_data_end += len(";</script>")
        over_budget_injection = """
<script>
(function(){
  var original=document.createElementNS.bind(document);
  window.__fixtureSvgCalls=0;
  document.createElementNS=function(){
    window.__fixtureSvgCalls+=1;
    return original.apply(document,arguments);
  };
  for(var index=0;index<2001;index+=1){
    window.__KIT_ARCHITECTURE_MAP__.nodes.push({
      id:"fixture-over-budget-"+index,
      kind:"function",
      state:"existing",
      label:"fixture over budget "+index,
      path:"fixture/over-budget/"+index,
      signature:"func fixture_"+index+"() -> void"
    });
  }
})();
</script>
"""
        over_budget_probe = """
<script>
(function(){
  var root=document.getElementById("architecture-map");
  document.body.setAttribute("data-budget-mode",root.getAttribute("data-render-mode")||"");
  document.body.setAttribute("data-budget-svg-calls",String(window.__fixtureSvgCalls));
  document.body.setAttribute("data-budget-svg-children",String(
    document.getElementById("arch-graph").childElementCount));
  document.body.setAttribute("data-budget-fallback-hidden",String(
    document.getElementById("arch-fallback").hidden));
  document.body.setAttribute("data-budget-fallback-open",String(
    document.getElementById("arch-fallback").open));
  document.body.setAttribute("data-budget-settled-hidden",String(
    document.getElementById("arch-settled").hidden));
  document.body.setAttribute("data-budget-recoverable",
    root.getAttribute("data-recoverable-fallback")||"");
  var files=root.querySelector('[data-arch-depth="file"]');
  var modules=root.querySelector('[data-arch-depth="module"]');
  var functions=root.querySelector('[data-arch-depth="function"]');
  var changes=root.querySelector('[data-arch-view="changes"]');
  files.click();
  document.body.setAttribute("data-budget-files-recovered",String(
    root.getAttribute("data-render-mode")==="graph"));
  functions.click();
  document.body.setAttribute("data-budget-functions-refallback",String(
    root.getAttribute("data-render-mode")==="fallback"));
  modules.click();
  document.body.setAttribute("data-budget-modules-recovered",String(
    root.getAttribute("data-render-mode")==="graph"));
  functions.click();
  changes.click();
  document.body.setAttribute("data-budget-changes-recovered",String(
    root.getAttribute("data-render-mode")==="graph"));
  document.body.setAttribute("data-budget-final-fallback-hidden",String(
    document.getElementById("arch-fallback").hidden));
  document.body.setAttribute("data-budget-final-settled-hidden",String(
    document.getElementById("arch-settled").hidden));
  document.body.setAttribute("data-budget-final-svg-calls",String(
    window.__fixtureSvgCalls));
})();
</script>
"""
        over_budget_page = (
            rendered_plan[:map_data_end]
            + over_budget_injection
            + rendered_plan[map_data_end:]
        ).replace("</body>", over_budget_probe + "</body>", 1)
        (fixture_root / "plan-over-budget.html").write_text(
            over_budget_page,
            encoding="utf-8",
        )

        hide_map = (
            '<script>document.getElementById("architecture-map").style.display="none";'
            '</script>'
        )
        hidden_probe = """
<script>
(function(){
  document.body.setAttribute("data-browser-probe-pending","true");
  var root=document.getElementById("architecture-map"),attempts=0;
  document.body.setAttribute("data-hidden-initial-mode",root.getAttribute("data-render-mode")||"");
  setTimeout(function(){
    root.style.display="";window.dispatchEvent(new Event("resize"));
    function finish(){
      var mode=root.getAttribute("data-render-mode")||"";
      if(mode!=="graph" && mode!=="fallback" && attempts<240){
        attempts+=1;setTimeout(finish,25);return;
      }
      document.body.setAttribute("data-hidden-final-mode",mode);
      document.body.setAttribute("data-hidden-fallback-hidden",String(document.getElementById("arch-fallback").hidden));
      document.body.setAttribute("data-hidden-nodes",String(root.querySelectorAll(".arch-node").length));
      document.body.removeAttribute("data-browser-probe-pending");
    }
    finish();
  },120);
})();
</script>
"""
        hidden_page = rendered_plan.replace(
            map_marker,
            hide_map + map_marker,
            1,
        ).replace("</body>", hidden_probe + "</body>", 1)
        (fixture_root / "plan-hidden.html").write_text(hidden_page, encoding="utf-8")

        kit_change_page = kit_change_html.render(_kit_change_fixture_preview())
        lifecycle_pages = fixture_root / "plan"
        lifecycle_pages.mkdir()
        (lifecycle_pages / "kit-change-browser.html").write_text(
            kit_change_page.replace(
                "</body>", KIT_CHANGE_DECISION_PROBE + "</body>", 1
            ),
            encoding="utf-8",
        )
        recovery_page = kit_change_html.render(_kit_change_recovery_preview())
        (lifecycle_pages / "kit-change-recovery-browser.html").write_text(
            recovery_page.replace(
                "</body>", KIT_CHANGE_RECOVERY_PROBE + "</body>", 1
            ),
            encoding="utf-8",
        )
        (fixture_root / "fixture-items.json").write_text(
            json.dumps(items, indent=2) + "\n", encoding="utf-8"
        )
        yield fixture_root
    finally:
        shutil.rmtree(fixture_root, ignore_errors=True)


def _run_browser_scenarios(browser: str, fixture_root: Path) -> int:

    profile = fixture_root / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    port = free_port()

    # ---------------------------------------------------------- healthy
    proc = start_board(port, "healthy", fixture_root)
    try:
        html = dump_dom(browser, f"http://127.0.0.1:{port}/retro.html", profile)
        s = "live/healthy"
        check(s, "the initial board probe settled", 'data-board-ready="true"' in html)
        check(s, "body is board-live", "board-live" in body_class(html), body_class(html))
        check(s, "no banner is shown when the board is live", banner(html, "board-banner").strip() == "")
        check(s, "a silent worker is rendered as stalled", "status-slot stalled" in html)
        check(s, "the silence is stated in words, not a timestamp",
              re.search(r"silent \d+ min", html) is not None)
        check(s, "the stalled item says to go and look",
              "not progressing on its own" in html)
        check(s, "the resume command is shown", "copilot --resume" in html)
        check(s, "a failed item is rendered", "status-slot failed" in html)
        check(s, "a finished item is rendered", "status-slot done" in html)
        check(s, "a queued item is rendered", "status-slot queued" in html)
        check(s, "the approval comment is shown on approved items",
              "appended" in html and "Do the smallest version first" in html)
        total, disabled = controls(html)
        check(s, "controls are enabled on a live board", total > 0 and disabled == 0,
              f"{disabled}/{total} disabled")

        # ------------------------------------------- ordering and separation
        s = "live/ordering"
        check(s, "the three lists are separately identified by id",
              all(f'id="list-{k}"' in html and f'id="panel-{k}"' in html
                  for k in ("toaction", "approved", "deferred")))
        check(s, "the ordering rule is stated on the page for each list",
              all(cap in html for cap in retro_html.ORDER_CAPTIONS.values()))
        to_action = page_parts.cards(html, "list-toaction")
        approved_cards = page_parts.cards(html, "list-approved")
        check(s, "an approved card is never rendered inside the to-action list",
              all(c.get("data-decided", "") == "" for c in to_action),
              str([c.get("data-slug") for c in to_action
                   if c.get("data-decided")]))
        check(s, "the to-action list is cost-descending with unranked last",
              _costs_descend(to_action),
              str([c.get("data-cost") for c in to_action]))
        check(s, "unranked findings say why they carry no cost",
              all("unranked-note" in html for c in to_action
                  if c.get("data-cost", "") == "") if to_action else True)
        check(s, "the approved list runs attention first, then live, queued and verified",
              _states_ascend(html), _state_order(html))
        for kind in ("toaction", "approved", "deferred"):
            got = page_parts.ranks(html, f"list-{kind}")
            check(s, f"every card in {kind} is numbered by its position",
                  got == [f"{i}." for i in range(1, len(got) + 1)], str(got))
        check(s, "deferred is behind its own tab, not competing for attention",
              'id="panel-deferred" hidden' in html)
        check(s, "settled cards outnumber nothing silently: approved list is populated",
              len(approved_cards) > 0, str(len(approved_cards)))

        plan = dump_dom(browser, f"http://127.0.0.1:{port}/plan.html", profile)
        s = "live/plan-banner"
        check(s, "the initial board probe settled", 'data-board-ready="true"' in plan)
        check(
            s,
            "body is board-live",
            "board-live" in body_class(plan),
            repr(plan[:240]),
        )
        check(s, "the cockpit banner is quiet while live",
              banner(plan, "board-banner").strip() == "")
        rb = banner(plan, "retro-banner")
        check(s, "the plan says a retrospective is due", "retrospective is due" in rb, rb[:80])
        check(s, "the plan says findings are awaiting a decision",
              "awaiting your decision" in rb)
        check(s, "the plan links into the retro board", 'href="/retro.html"' in rb)

        s = "live/architecture-map"
        architecture = element_tag(plan, "architecture-map")
        fallback = element_tag(plan, "arch-fallback")
        graph_only = [
            element_tag(plan, "arch-graph-shell"),
            element_tag(plan, "arch-legend"),
            element_tag(plan, "arch-camera-controls"),
        ]
        check(s, "the architecture map reaches graph mode",
              'data-render-mode="graph"' in architecture, architecture)
        check(s, "the graph renders architecture nodes",
              len(re.findall(r'<g[^>]*class="arch-node"', plan)) > 0)
        check(s, "the fallback list stays hidden during normal rendering",
              bool(fallback) and " hidden" in fallback, fallback)
        check(s, "graph-only surfaces remain visible during normal rendering",
              all(tag and " hidden" not in tag for tag in graph_only), str(graph_only))
        settled = element_tag(plan, "arch-settled")
        check(s, "the settled badge appears only after the graph settles",
              bool(settled) and " hidden" not in settled
              and "Layout settled" in plan,
              settled)
        complete_count = int(body_attribute(plan, "data-arch-probe-complete-count") or 0)
        changes_count = int(body_attribute(plan, "data-arch-probe-changes-count") or 0)
        restored_count = int(body_attribute(plan, "data-arch-probe-restored-count") or 0)
        check(s, "Current and planned is the working default",
              body_attribute(plan, "data-arch-probe-complete-default") == "true"
              and complete_count > 0,
              f"complete={complete_count} body={body_class(plan)}")
        check(s, "Changes filters unaffected items and current context restores them",
              body_attribute(plan, "data-arch-probe-changes-pressed") == "true"
              and body_attribute(plan, "data-arch-probe-complete-restored") == "true"
              and 0 < changes_count < complete_count
              and restored_count == complete_count,
              f"complete={complete_count} changes={changes_count} restored={restored_count}")
        check(s, "exactly one rendered node is in the keyboard tab order",
              body_attribute(plan, "data-arch-probe-roving-count") == "1"
              and len(re.findall(r'<g[^>]*class="arch-node"[^>]*tabindex="0"', plan)) == 1,
              body_class(plan))
        check(s, "an arrow key moves selection and keyboard focus",
              body_attribute(plan, "data-arch-probe-arrow-moved") == "true"
              and body_attribute(plan, "data-arch-probe-arrow-focused") == "true",
              body_class(plan))
        check(s, "search and Enter select the planned feedback file",
              body_attribute(plan, "data-arch-probe-ran") == "true"
              and "feedback_event.gd" in body_attribute(
                  plan, "data-arch-probe-selected"
              )
              and body_attribute(plan, "data-arch-probe-count") != "0",
              body_class(plan))

        fallback_page = dump_dom(
            browser,
            (fixture_root / "plan-fallback.html").as_uri(),
            profile,
        )
        s = "fallback/architecture-map"
        architecture = element_tag(fallback_page, "architecture-map")
        fallback = element_tag(fallback_page, "arch-fallback")
        graph_only = [
            element_tag(fallback_page, "arch-graph-shell"),
            element_tag(fallback_page, "arch-legend"),
            element_tag(fallback_page, "arch-camera-controls"),
        ]
        check(s, "an SVG construction error reaches fallback mode",
              'data-render-mode="fallback"' in architecture, architecture)
        check(s, "the readable fallback list is visible and open",
              bool(fallback) and " hidden" not in fallback and " open" in fallback,
              fallback)
        check(s, "graph-only surfaces are hidden in fallback mode",
              all(tag and " hidden" in tag for tag in graph_only), str(graph_only))
        settled = element_tag(fallback_page, "arch-settled")
        check(s, "the settled badge stays hidden when rendering falls back",
              bool(settled) and " hidden" in settled, settled)
        check(s, "fallback mode is list-only",
              len(re.findall(r'<g[^>]*class="arch-node"', fallback_page)) == 0)
        check(s, "search and Enter still select the planned feedback file",
              body_attribute(fallback_page, "data-arch-probe-ran") == "true"
              and body_attribute(fallback_page, "data-arch-probe-mode") == "fallback"
              and body_attribute(
                  fallback_page, "data-arch-probe-svg-failure-calls"
              ) == "1"
              and "feedback_event.gd" in body_attribute(
                  fallback_page, "data-arch-probe-selected"
              )
              and body_attribute(fallback_page, "data-arch-probe-count") != "0",
              body_class(fallback_page))

        no_changes_page = dump_dom(
            browser,
            (fixture_root / "plan-no-changes.html").as_uri(),
            profile,
        )
        s = "views/no-changes"
        zero_complete = int(body_attribute(
            no_changes_page, "data-zero-complete-count"
        ) or 0)
        zero_changes = int(body_attribute(
            no_changes_page, "data-zero-changes-count"
        ) or -1)
        zero_stable = int(body_attribute(
            no_changes_page, "data-zero-stable-count"
        ) or -1)
        check(s, "Current and planned still shows the unchanged architecture",
              zero_complete > 0, f"complete={zero_complete}")
        check(s, "Changes with no changes stays empty without falling back",
              zero_changes == 0 and zero_stable == 0
              and body_attribute(no_changes_page, "data-zero-mode") == "graph"
              and body_attribute(
                  no_changes_page, "data-zero-fallback-hidden"
              ) == "true",
              f"first={zero_changes} stable={zero_stable} body={body_class(no_changes_page)}")

        module_page = dump_dom(
            browser,
            (fixture_root / "plan-module.html").as_uri(),
            profile,
        )
        module_ids = [item for item in body_attribute(
            module_page, "data-involvement-node-ids"
        ).split("|") if item]
        s = "involvement/module"
        check(s, "module involvement renders architecture modules only",
              body_attribute(module_page, "data-involvement-mode") == "graph"
              and bool(module_ids)
              and all(item.startswith("folder:") for item in module_ids),
              str(module_ids[:12]))
        check(s, "module copy and search match the module cap",
              body_attribute(module_page, "data-involvement-max") == "module"
              and body_attribute(
                  module_page, "data-involvement-eyebrow"
              ) == "Module-level review"
              and body_attribute(
                  module_page, "data-involvement-copy"
              ) == (
                  "Start with the coloured changes. Current and planned architecture "
                  "modules and dependencies stay still while you inspect them."
              )
              and body_attribute(
                  module_page, "data-involvement-placeholder"
              ) == "Module"
              and body_attribute(
                  module_page, "data-involvement-heading"
              ) == "Current and planned modules"
              and body_attribute(
                  module_page, "data-involvement-depths"
              ) == "module",
              body_class(module_page))

        file_page = dump_dom(
            browser,
            (fixture_root / "plan-file.html").as_uri(),
            profile,
        )
        file_ids = [item for item in body_attribute(
            file_page, "data-involvement-node-ids"
        ).split("|") if item]
        s = "involvement/file"
        check(s, "file involvement excludes classes and functions",
              body_attribute(file_page, "data-involvement-mode") == "graph"
              and any(item.startswith("file:") for item in file_ids)
              and not any(item.startswith(("class:", "function:")) for item in file_ids),
              str(file_ids[:12]))
        check(s, "file copy and search match the file cap",
              body_attribute(file_page, "data-involvement-max") == "file"
              and body_attribute(
                  file_page, "data-involvement-eyebrow"
              ) == "File-level review"
              and body_attribute(
                  file_page, "data-involvement-copy"
              ) == (
                  "Start with the coloured changes. Current and planned folder to file "
                  "structure stays still while you inspect it."
              )
              and body_attribute(
                  file_page, "data-involvement-placeholder"
              ) == "Folder or file"
              and body_attribute(
                  file_page, "data-involvement-heading"
              ) == "Current and planned files"
              and body_attribute(
                  file_page, "data-involvement-depths"
              ) == "module|file",
              body_class(file_page))

        function_page = dump_dom(
            browser,
            (fixture_root / "plan-function.html").as_uri(),
            profile,
        )
        function_ids = [item for item in body_attribute(
            function_page, "data-involvement-node-ids"
        ).split("|") if item]
        s = "involvement/function"
        check(s, "function copy and search match the function cap",
              body_attribute(function_page, "data-involvement-mode") == "graph"
              and body_attribute(
                  function_page, "data-involvement-max"
              ) == "function"
              and body_attribute(
                  function_page, "data-involvement-eyebrow"
              ) == "Function-level review"
              and body_attribute(
                  function_page, "data-involvement-copy"
              ) == (
                  "Start with the coloured changes. Current and planned folder to file to "
                  "class to function structure stays still while you inspect it."
              )
              and body_attribute(
                  function_page, "data-involvement-placeholder"
              ) == "Folder, file, class or function"
              and body_attribute(
                  function_page, "data-involvement-heading"
              ) == "Current and planned structure"
              and body_attribute(
                  function_page, "data-involvement-depths"
              ) == "module|file|function"
              and any(item.startswith("function:") for item in function_ids),
              body_class(function_page))

        large_page = dump_dom(
            browser,
            (fixture_root / "plan-large.html").as_uri(),
            profile,
        )
        large_nodes = int(body_attribute(large_page, "data-large-nodes") or 0)
        large_edges = int(body_attribute(large_page, "data-large-edges") or 0)
        s = "rendering/large-map"
        check(s, "the fixture crosses the chunked-render threshold",
              large_nodes + large_edges > 500,
              f"nodes={large_nodes} edges={large_edges}")
        check(s, "chunked rendering completes in graph mode without fallback",
              body_attribute(large_page, "data-large-mode") == "graph"
              and body_attribute(large_page, "data-large-fallback-hidden") == "true"
              and body_attribute(large_page, "data-large-busy") == "false",
              body_class(large_page))

        over_budget_page = dump_dom(
            browser,
            (fixture_root / "plan-over-budget.html").as_uri(),
            profile,
        )
        s = "rendering/over-budget"
        check(s, "an over-budget map falls back before any SVG work",
              body_attribute(over_budget_page, "data-budget-mode") == "fallback"
              and body_attribute(
                  over_budget_page, "data-budget-svg-calls"
              ) == "0"
              and body_attribute(
                  over_budget_page, "data-budget-svg-children"
              ) == "0"
              and body_attribute(
                  over_budget_page, "data-budget-fallback-hidden"
              ) == "false"
              and body_attribute(
                  over_budget_page, "data-budget-fallback-open"
              ) == "true"
              and body_attribute(
                  over_budget_page, "data-budget-settled-hidden"
              ) == "true"
              and body_attribute(
                  over_budget_page, "data-budget-recoverable"
              ) == "true",
              body_class(over_budget_page))
        check(s, "Files and Modules recover a size-only fallback",
              body_attribute(
                  over_budget_page, "data-budget-files-recovered"
              ) == "true"
              and body_attribute(
                  over_budget_page, "data-budget-functions-refallback"
              ) == "true"
              and body_attribute(
                  over_budget_page, "data-budget-modules-recovered"
              ) == "true",
              body_class(over_budget_page))
        check(s, "Changes recovers the size fallback to a settled graph",
              body_attribute(
                  over_budget_page, "data-budget-changes-recovered"
              ) == "true"
              and body_attribute(
                  over_budget_page, "data-budget-final-fallback-hidden"
              ) == "true"
              and body_attribute(
                  over_budget_page, "data-budget-final-settled-hidden"
              ) == "false"
              and int(body_attribute(
                  over_budget_page, "data-budget-final-svg-calls"
              ) or 0) > 0,
              body_class(over_budget_page))

        hidden_page = dump_dom(
            browser,
            (fixture_root / "plan-hidden.html").as_uri(),
            profile,
        )
        s = "rendering/zero-size"
        check(s, "a hidden map waits rather than latching fallback",
              body_attribute(hidden_page, "data-hidden-initial-mode") == "waiting",
              body_class(hidden_page))
        check(s, "the revealed map recovers to graph mode",
              body_attribute(hidden_page, "data-hidden-final-mode") == "graph"
              and body_attribute(hidden_page, "data-hidden-fallback-hidden") == "true"
              and int(body_attribute(hidden_page, "data-hidden-nodes") or 0) > 0,
              body_class(hidden_page))
    finally:
        stop_board(proc)

    # -------------------------- managed lifecycle page, real DOM and requests
    KitChangeBrowserHandler.reset(_kit_change_state("needs_decision"))
    proc = start_board(
        port,
        "healthy",
        fixture_root,
        handler_cls=KitChangeBrowserHandler,
    )
    try:
        html = dump_dom(
            browser,
            f"http://127.0.0.1:{port}/plan/kit-change-browser.html",
            profile,
        )
        s = "live/kit-change-decision-apply-restore"
        check(s, "the two-review lifecycle proof ran to completion",
              body_attribute(html, "data-kit-change-probe-ran") == "true"
              and not body_attribute(html, "data-kit-change-probe-error"),
              body_class(html))
        check(s, "Apply starts disabled and the decision enables it",
              body_attribute(
                  html, "data-kit-change-probe-initial-disabled"
              ) == "true"
              and body_attribute(
                  html, "data-kit-change-probe-decision-enabled"
              ) == "true",
              body_class(html))
        check(s, "the decision opens the exact second review",
              body_attribute(html, "data-kit-change-probe-second-review")
              == (
                  "/kit-change.html?session="
                  + KIT_CHANGE_READY_SESSION
              ),
              body_class(html))
        check(s, "Apply renders checked completion and enables Restore",
              body_attribute(html, "data-kit-change-probe-apply-status") == "complete"
              and body_attribute(
                  html, "data-kit-change-probe-checked-at"
              ) == KIT_CHANGE_CHECKED_AT
              and body_attribute(
                  html, "data-kit-change-probe-restore-enabled"
              ) == "true",
              body_class(html))
        check(s, "Restore renders the restored decision state",
              body_attribute(
                  html, "data-kit-change-probe-restore-status"
              ) == "restored"
              and body_attribute(
                  html, "data-kit-change-probe-review-enabled"
              ) == "true"
              and body_attribute(html, "data-kit-change-probe-errors") == "0",
              body_class(html))
        check(s, "the browser sent the exact Apply and Restore requests",
              KitChangeBrowserHandler.lifecycle_requests == [
                  (
                      "/api/kit-change/apply",
                      {
                          "session_id": KIT_CHANGE_SESSION,
                          "plan_sha256": KIT_CHANGE_PLAN_SHA,
                          "choices": {"D1": "game"},
                      },
                  ),
                  (
                      "/api/kit-change/apply",
                      {
                          "session_id": KIT_CHANGE_READY_SESSION,
                          "plan_sha256": KIT_CHANGE_READY_PLAN_SHA,
                          "choices": {},
                      },
                  ),
                  (
                      "/api/kit-change/restore",
                      {
                          "session_id": KIT_CHANGE_READY_SESSION,
                          "result_sha256": KIT_CHANGE_RESULT_SHA,
                      },
                  ),
              ],
              repr(KitChangeBrowserHandler.lifecycle_requests))

        KitChangeBrowserHandler.reset(_kit_change_state(
            "recovery_required",
            session_id=KIT_CHANGE_RECOVERY_SESSION,
        ))
        recovery = dump_dom(
            browser,
            f"http://127.0.0.1:{port}/plan/kit-change-recovery-browser.html",
            profile,
        )
        s = "live/kit-change-recovery"
        check(s, "the recovery JavaScript proof ran to completion",
              body_attribute(recovery, "data-kit-change-recovery-ran") == "true"
              and not body_attribute(recovery, "data-kit-change-recovery-error"),
              body_class(recovery))
        check(s, "Recovery restores the previous state",
              body_attribute(
                  recovery, "data-kit-change-recovery-status"
              ) == "restored"
              and body_attribute(
                  recovery, "data-kit-change-recovery-review-enabled"
              ) == "true"
              and body_attribute(
                  recovery, "data-kit-change-recovery-errors"
              ) == "0",
              body_class(recovery))
        check(s, "the browser sent only the exact Recovery request",
              KitChangeBrowserHandler.lifecycle_requests == [
                  (
                      "/api/kit-change/recover",
                      {"session_id": KIT_CHANGE_RECOVERY_SESSION},
                  )
              ],
              repr(KitChangeBrowserHandler.lifecycle_requests))
    finally:
        stop_board(proc)

    # ------------------------------------------------- board unreachable
    proc = start_board(port, "nohealth", fixture_root)
    try:
        html = dump_dom(browser, f"http://127.0.0.1:{port}/retro.html", profile)
        s = "live/board-down"
        check(s, "the initial board probe settled", 'data-board-ready="true"' in html)
        b = banner(html, "board-banner")
        check(s, "body is board-down", "board-down" in body_class(html), body_class(html))
        check(s, "the page says the board is not reachable", "not reachable" in b, b[:80])
        check(s, "the page names the URL it tried", f"127.0.0.1:{port}" in b)
        check(s, "the page says how to bring the board back", "kit serve" in b)
        check(s, "a retry control is offered", "board-retry" in html)
        total, disabled = controls(html)
        check(s, "every control is visibly disabled", total > 0 and disabled == total,
              f"{disabled}/{total}")

        plan = dump_dom(browser, f"http://127.0.0.1:{port}/plan.html", profile)
        plan_banner = banner(plan, "board-banner")
        check("live/board-down", "the plan probe settled",
              'data-board-ready="true"' in plan)
        check("live/board-down", "the plan shows the same cockpit outage",
              "not reachable" in plan_banner and "kit serve" in plan_banner,
              plan_banner[:80])
        check("live/board-down", "retro state stays quiet while the cockpit is down",
              banner(plan, "retro-banner").strip() == "")
    finally:
        stop_board(proc)

    # ------------------------------------------------------ file:// mode
    for page in ("retro.html", "plan.html"):
        url = (fixture_root / page).as_uri()
        html = dump_dom(browser, url, profile)
        s = f"file/{page}"
        check(s, "the file-mode probe settled", 'data-board-ready="true"' in html)
        check(s, "body is board-file", "board-file" in body_class(html), body_class(html))
        b = banner(html, "board-banner")
        check(s, "the page says it is read-only because it is a file",
              "Read-only" in b, b[:80])
        check(s, "it says decisions need the loopback cockpit",
              "loopback cockpit" in b)
        if page == "retro.html":
            total, disabled = controls(html)
            check(s, "controls are disabled, not clickable-but-inert",
                  total > 0 and disabled == total, f"{disabled}/{total}")
            check(s, "the report and its prompts are still readable",
                  html.count("<pre>") > 0 and "prompt-block" in html)
        else:
            check(s, "the retro banner is silent", banner(html, "retro-banner").strip() == "")

    print(f"\n{checks - len(failures)}/{checks} browser checks passed")
    return 1 if failures else 0


def main() -> int:
    browser = find_browser()
    if not browser:
        print("no Chromium-based browser found; browser check skipped")
        return 0
    with browser_fixture() as fixture_root:
        return _run_browser_scenarios(browser, fixture_root)


if __name__ == "__main__":
    raise SystemExit(main())
