#!/usr/bin/env python3
"""Load the generated pages in a real browser and assert on what it rendered.

The node harness (`dom_harness.js`) executes the page's scripts against a stub
DOM, which is enough to prove the error paths. This proves the other half: that
a real browser, over a real loopback board, reaches the states the harness
claims -- including the two a healthy board cannot be asked to produce, a
worker silent for half an hour and a worker that exited non-zero.

    python tools/tests/browser_check.py

It starts `mock_board.py` on a spare port, renders each page with headless
Chrome or Edge (`--dump-dom` runs the page's JavaScript first), and checks the
resulting DOM. Skips with exit 0 if no Chromium-based browser is installed --
a browser is not a dependency of this kit.

States covered: healthy, board-unreachable, file:// read-only, and per-item
working/stalled, failed, done, queued.
"""
from __future__ import annotations

import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
MOCK = ROOT / "tools" / "tests" / "mock_board.py"
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "tools"))
import page_parts  # noqa: E402
import retro_html  # noqa: E402

BROWSERS = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
]

failures: list[str] = []
checks = 0


def check(scenario: str, name: str, cond: bool, extra: str = "") -> None:
    global checks
    checks += 1
    if cond:
        print(f"  ok   {scenario} | {name}")
    else:
        print(f"  FAIL {scenario} | {name} {extra}")
        failures.append(f"{scenario}: {name} {extra}")


def find_browser() -> str:
    for path in BROWSERS:
        if Path(path).exists():
            return path
    return ""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def dump_dom(browser: str, url: str, profile: Path) -> str:
    proc = subprocess.run(
        [browser, "--headless=new", "--disable-gpu", "--no-sandbox",
         f"--user-data-dir={profile}", "--virtual-time-budget=6000",
         "--dump-dom", url],
        capture_output=True, text=True, errors="replace", timeout=180,
    )
    return proc.stdout


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


def wait_for(port: int, timeout: float = 10.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1):
                return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    return False


def start_board(port: int, scenario: str) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, str(MOCK), "--port", str(port), "--scenario", scenario],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    wait_for(port)
    return proc


def main() -> int:
    browser = find_browser()
    if not browser:
        print("no Chromium-based browser found; browser check skipped")
        return 0
    for page in ("retro.html", "plan.html"):
        if not (ROOT / page).exists():
            print(f"{page} not generated; run tools/{page.split('.')[0]}_html.py first")
            return 1

    profile = ROOT / ".checklogs" / "browser-profile"
    profile.mkdir(parents=True, exist_ok=True)
    port = free_port()

    # ---------------------------------------------------------- healthy
    proc = start_board(port, "healthy")
    try:
        html = dump_dom(browser, f"http://127.0.0.1:{port}/retro.html", profile)
        s = "live/healthy"
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
        check(s, "the approved list runs live work first, then queued, done, failed",
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
        check(s, "body is board-live", "board-live" in body_class(plan))
        rb = banner(plan, "retro-banner")
        check(s, "the plan says a retrospective is due", "retrospective is due" in rb, rb[:80])
        check(s, "the plan says findings are awaiting a decision",
              "awaiting your decision" in rb)
        check(s, "the plan links into the retro board", 'href="/retro.html"' in rb)
    finally:
        proc.terminate()

    # ------------------------------------------------- board unreachable
    proc = start_board(port, "nohealth")
    try:
        html = dump_dom(browser, f"http://127.0.0.1:{port}/retro.html", profile)
        s = "live/board-down"
        b = banner(html, "board-banner")
        check(s, "body is board-down", "board-down" in body_class(html), body_class(html))
        check(s, "the page says the board is not reachable", "not reachable" in b, b[:80])
        check(s, "the page names the URL it tried", f"127.0.0.1:{port}" in b)
        check(s, "the page says how to bring the board back", "board.py --ensure" in b)
        check(s, "a retry control is offered", "board-retry" in html)
        total, disabled = controls(html)
        check(s, "every control is visibly disabled", total > 0 and disabled == total,
              f"{disabled}/{total}")

        plan = dump_dom(browser, f"http://127.0.0.1:{port}/plan.html", profile)
        check("live/board-down", "the plan banner degrades to nothing",
              banner(plan, "retro-banner").strip() == "")
    finally:
        proc.terminate()

    # ------------------------------------------------------ file:// mode
    for page in ("retro.html", "plan.html"):
        url = (ROOT / page).as_uri()
        html = dump_dom(browser, url, profile)
        s = f"file/{page}"
        check(s, "body is board-file", "board-file" in body_class(html), body_class(html))
        if page == "retro.html":
            b = banner(html, "board-banner")
            check(s, "the page says it is read-only because it is a file",
                  "Read-only" in b, b[:80])
            check(s, "it says approval and dispatch need the loopback board",
                  "loopback board" in b)
            total, disabled = controls(html)
            check(s, "controls are disabled, not clickable-but-inert",
                  total > 0 and disabled == total, f"{disabled}/{total}")
            check(s, "the report and its prompts are still readable",
                  html.count("<pre>") > 0 and "prompt-block" in html)
        else:
            check(s, "the plan banner is silent", banner(html, "retro-banner").strip() == "")

    print(f"\n{checks - len(failures)}/{checks} browser checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
