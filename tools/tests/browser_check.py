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
            if 'data-board-ready="true"' in html:
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


def start_board(port: int, scenario: str, fixture_root: Path) -> RunningBoard:
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
            ("127.0.0.1", port), mock_board.Handler
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
        rendered_plan = plan_html.render(
            {}, {}, [], "", set(), [], [], [], "",
            cockpit_state={"verification": {"status": "not-run"}},
        )
        (fixture_root / "plan.html").write_text(rendered_plan, encoding="utf-8")
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
