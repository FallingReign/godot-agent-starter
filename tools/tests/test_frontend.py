#!/usr/bin/env python3
"""Tests for the generated pages, run against the generated pages.

Two layers, both reading the artefact rather than the generator:

  * byte assertions on retro.html / plan.html -- the properties that must hold
    for any content (no raw fetch, every control declares itself, the prompt is
    inlined so file:// can read it);
  * `tools/tests/dom_harness.js`, which executes the page's own inline scripts
    against a stub DOM and a stub board and asserts what the human sees in each
    of the five states: healthy, board unreachable, file:// read-only, a worker
    silent for half an hour, a worker that exited non-zero.

The second layer needs node. It skips if node is absent rather than failing:
node is not a dependency of this kit and must not become one.

    python -m unittest discover -s tools/tests -v
"""
from __future__ import annotations

import contextlib
import io
import json
import random
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS = ROOT / "tools" / "tests" / "dom_harness.js"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import page_parts  # noqa: E402
import board_client  # noqa: E402
import browser_check  # noqa: E402
import plan_html  # noqa: E402
import retro_html  # noqa: E402


_DEFAULT_FUNCTION_CHANGES = object()


def generated(name: str, build) -> str:
    # Generator entry points use argparse. Discovery's argv belongs to
    # unittest, not to those entry points. Always exercise current source;
    # reading an existing generated page makes byte tests bless stale output.
    arguments = [f"generate-{name}", "--stdout"]
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        with mock.patch.object(sys, "argv", arguments):
            result = build()
    if result not in (None, 0):
        raise AssertionError(f"{name} generator returned {result}")
    return output.getvalue()


def architecture_fixture(involvement: str = "function") -> dict:
    """A complete, deterministic source tree for architecture UI tests."""
    return {
        "shape": {
            "name": "Architecture browser fixture",
            "pitch": "One existing path and one planned addition.",
            "involvement": involvement,
        },
        "proposal": {
            "slice": "show one architecture change",
            "status": "draft",
            "experience": {
                "player_does": "Chooses an action and immediately understands its result."
            },
            "design_refs": [{
                "section": "docs/design/action-feedback.md",
                "why": "Every accepted action must explain its result immediately.",
                "sha256": "d" * 64,
            }],
            "reversibility": {
                "state": "reversible",
                "hard_to_undo": "No hard-to-undo point is inside this fixture.",
                "veto_scope": "Remove the isolated feedback addition.",
            },
            "modules": [{
                "path": "scripts/logic",
                "action": "modify",
                "role": "Keeps gameplay decisions independent of scenes.",
                "why": "The new result belongs beside the action decision.",
                "may_depend_on": [],
                "boundary_data": "Typed action and feedback values.",
            }],
            "files": [{
                "path": "scripts/logic/feedback_event.gd",
                "module": "scripts/logic",
                "action": "new",
                "why": "Give an accepted action one plain-language result value.",
            }],
            "functions": [{
                "file": "scripts/logic/feedback_event.gd",
                "class_scope": "FeedbackEvent",
                "signature": "func describe_feedback() -> String",
                "action": "new",
                "why": "Expose the short result the interface must show.",
            }],
            "considered_existing": [{
                "path": "scripts/logic/action_service.gd",
                "why_not": "It decides actions; making it own display wording would mix responsibilities.",
            }],
        },
        "modules": [{"path": "scripts/logic", "depends_on": []}],
        "tree": {
            "scripts/logic/action_service.gd": {
                "class_name": "ActionService",
                "functions": [{
                    "name": "submit_action",
                    "identity": "ActionService.submit_action",
                    "class_scope": "ActionService",
                    "signature": (
                        "func submit_action(action: PlayerAction) -> FeedbackEvent"
                    ),
                    "private": False,
                }],
            }
        },
        "built": {"scripts/logic/action_service.gd"},
        "present": {"scripts/logic/action_service.gd"},
        "baseline_files": {"scripts/logic/action_service.gd"},
        # An empty mapping is positive evidence that baseline comparison ran
        # and found no function-level changes.  None has the distinct meaning
        # "comparison unavailable" and is covered by the deletion regressions.
        "function_changes": {},
        "cockpit_state": {
            "verification": {
                "status": "passed",
                "summary": "Static checks passed for the deterministic fixture.",
            }
        },
    }


def architecture_fixture_html(involvement: str = "function") -> str:
    fixture = architecture_fixture(involvement)
    return plan_html.render(
        fixture["shape"],
        fixture["proposal"],
        fixture["modules"],
        "",
        fixture["built"],
        [],
        [],
        [],
        "",
        tree=fixture["tree"],
        cockpit_state=fixture["cockpit_state"],
        present_files=fixture["present"],
        baseline_files=fixture["baseline_files"],
        function_changes=fixture["function_changes"],
    )


def unchanged_architecture_fixture_html() -> str:
    """One complete source tree whose Changes focus must be genuinely empty."""
    fixture = architecture_fixture()
    fixture["shape"] = {
        **fixture["shape"],
        "name": "Architecture unchanged fixture",
        "pitch": "A stable source tree with no observed or planned changes.",
    }
    fixture["proposal"] = {
        "slice": "inspect unchanged architecture",
        "status": "draft",
        "experience": fixture["proposal"]["experience"],
        "reversibility": fixture["proposal"]["reversibility"],
    }
    return plan_html.render(
        fixture["shape"],
        fixture["proposal"],
        fixture["modules"],
        "",
        set(),
        [],
        [],
        [],
        "",
        tree=fixture["tree"],
        cockpit_state=fixture["cockpit_state"],
        changed_actions={},
        present_files=fixture["present"],
        baseline_files=fixture["baseline_files"],
        function_changes={},
    )


class FileModeReviewHint(unittest.TestCase):
    def test_only_exact_canonical_loopback_views_are_links(self) -> None:
        for page in ("plan", "retro"):
            url = f"http://127.0.0.1:54321/{page}.html"
            with self.subTest(url=url):
                self.assertEqual(
                    f'<a href="{url}">{url}</a>',
                    board_client._review_hint_html(url),
                )

    def test_every_other_hint_is_escaped_inert_text(self) -> None:
        unsafe = (
            "https://127.0.0.1:54321/plan.html",
            "http://localhost:54321/plan.html",
            "http://127.0.0.1:54321/plan.html?approve=1",
            "http://127.0.0.1:54321/plan.html#decision",
            "http://127.0.0.1:54321/plan/one.html",
            "http://127.0.0.1:0/plan.html",
            "http://127.0.0.1:65536/retro.html",
            "http://127.0.0.1:054321/retro.html",
            'http://127.0.0.1:54321/plan.html"><img src=x onerror=alert(1)>',
        )
        for value in unsafe:
            with self.subTest(value=value):
                rendered = board_client._review_hint_html(value)
                self.assertTrue(rendered.startswith("<code>"), rendered)
                self.assertNotIn("<a ", rendered)
                self.assertNotIn("<img", rendered)
                if "<" in value:
                    self.assertIn("&lt;", rendered)

    def test_untrusted_hint_cannot_close_the_inline_script(self) -> None:
        payload = "</script><script>globalThis.injected=true</script>"
        script = board_client.core_js(payload)
        self.assertNotIn(payload, script)
        self.assertIn("\\u003c/script\\u003e", script)
        self.assertEqual(1, script.count("</script>"))


class BrowserReadinessProbe(unittest.TestCase):
    def test_mac_prefers_the_preinstalled_automation_browser(self) -> None:
        testing = (
            "/Applications/Google Chrome for Testing.app/Contents/MacOS/"
            "Google Chrome for Testing"
        )
        consumer = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

        self.assertLess(
            browser_check.BROWSERS.index(testing),
            browser_check.BROWSERS.index(consumer),
        )

    def test_dump_uses_current_headless_mode_and_browser_capture_bound(self) -> None:
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="<html></html>", stderr=""
        )
        with mock.patch.object(
            browser_check.subprocess, "run", return_value=completed
        ) as run:
            browser_check.dump_dom(
                "chrome", "http://127.0.0.1:12345/plan.html", Path("profile")
            )

        command = run.call_args.args[0]
        self.assertIn("--headless", command)
        self.assertNotIn("--headless=new", command)
        self.assertIn("--timeout=15000", command)
        self.assertEqual(45, run.call_args.kwargs["timeout"])

    def test_direct_browser_timeout_returns_a_bounded_failure(self) -> None:
        with mock.patch.object(
            browser_check.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["chrome"], 45),
        ):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(
                    "",
                    browser_check.dump_dom(
                        "chrome",
                        "http://127.0.0.1:12345/plan.html",
                        Path("profile"),
                    ),
                )

    def test_mac_testing_browser_uses_the_selected_existing_driver(self) -> None:
        with mock.patch.object(
            browser_check,
            "find_chromedriver",
            return_value="chromedriver",
        ):
            with mock.patch.object(
                browser_check,
                "_dump_dom_with_webdriver",
                return_value="<html></html>",
            ) as webdriver_dump:
                with mock.patch.object(
                    browser_check.subprocess,
                    "run",
                    side_effect=AssertionError("direct browser CLI was used"),
                ):
                    result = browser_check.dump_dom(
                        browser_check.MAC_TEST_BROWSER,
                        "http://127.0.0.1:12345/plan.html",
                        Path("profile"),
                    )

        self.assertEqual("<html></html>", result)
        webdriver_dump.assert_called_once_with(
            "chromedriver",
            browser_check.MAC_TEST_BROWSER,
            "http://127.0.0.1:12345/plan.html",
            Path("profile"),
        )

    def test_webdriver_uses_a_private_profile_and_reads_the_rendered_dom(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        transient_down = '<html><body class="board-down"></body></html>'
        transient_live = '<html><body class="board-live"></body></html>'
        rendered = (
            '<html><body class="board-live" data-board-ready="true">'
            '<div class="status-slot stalled"></div></body></html>'
        )
        responses = [
            {"value": {"sessionId": "session-1"}},
            {"value": None},
            {"value": None},
            {"value": transient_down},
            {"value": transient_live},
            {"value": rendered},
            {"value": None},
        ]
        with browser_check.browser_fixture() as fixture_root:
            profile = fixture_root / "profile"
            with mock.patch.object(browser_check, "_stop_driver_tree") as stop:
                with mock.patch.object(
                    browser_check.subprocess,
                    "Popen",
                    return_value=process,
                ) as popen:
                    with mock.patch.object(browser_check, "free_port", return_value=9515):
                        with mock.patch.object(browser_check, "wait_for", return_value=True):
                            with mock.patch.object(
                                browser_check,
                                "_webdriver_json",
                                side_effect=responses,
                            ) as request:
                                result = browser_check._dump_dom_with_webdriver(
                                    "chromedriver",
                                    browser_check.MAC_TEST_BROWSER,
                                    "http://127.0.0.1:12345/plan.html",
                                    profile,
                                )

        self.assertEqual(rendered, result)
        self.assertIn("--port=9515", popen.call_args.args[0])
        capabilities = request.call_args_list[0].args[2]
        chrome = capabilities["capabilities"]["alwaysMatch"]["goog:chromeOptions"]
        self.assertEqual(browser_check.MAC_TEST_BROWSER, chrome["binary"])
        self.assertIn("--headless", chrome["args"])
        self.assertIn("--no-proxy-server", chrome["args"])
        profiles = [
            value
            for value in chrome["args"]
            if value.startswith("--user-data-dir=")
        ]
        self.assertEqual(1, len(profiles))
        self.assertIn("webdriver-", profiles[0])
        self.assertEqual("DELETE", request.call_args_list[-1].args[0])
        stop.assert_called_once_with(process)
        self.assertFalse(fixture_root.exists())

    def test_driver_cleanup_kills_a_group_after_its_leader_exits(self) -> None:
        process = mock.Mock(pid=7654)
        process.poll.return_value = 0
        process.wait.return_value = 0
        with mock.patch.object(browser_check.os, "name", "posix"):
            with mock.patch.object(browser_check.os, "killpg", create=True) as killpg:
                with mock.patch.object(
                    browser_check,
                    "_process_group_exited",
                    side_effect=[False, True],
                ) as group_exited:
                    browser_check._stop_driver_tree(process)

        self.assertEqual(
            [
                mock.call(7654, browser_check.PROCESS_SIGTERM),
                mock.call(7654, browser_check.PROCESS_SIGKILL),
            ],
            killpg.call_args_list,
        )
        self.assertEqual(2, group_exited.call_count)

    def test_runner_driver_root_precedes_an_unrelated_path_driver(self) -> None:
        with browser_check.browser_fixture() as fixture_root:
            root = fixture_root / "driver-root"
            root.mkdir()
            name = "chromedriver.exe" if browser_check.os.name == "nt" else "chromedriver"
            paired = root / name
            paired.write_bytes(b"")
            with mock.patch.dict(
                browser_check.os.environ,
                {
                    "CHROMEDRIVER_BIN": "",
                    "CHROMEWEBDRIVER": str(root),
                },
            ):
                with mock.patch.object(
                    browser_check.shutil,
                    "which",
                    return_value="unrelated-driver",
                ):
                    self.assertEqual(
                        str(paired.resolve()),
                        browser_check.find_chromedriver(),
                    )
        self.assertFalse(fixture_root.exists())

    def test_readiness_requires_only_a_direct_loopback_listener(self) -> None:
        with browser_check.socket.socket(
            browser_check.socket.AF_INET,
            browser_check.socket.SOCK_STREAM,
        ) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)

            self.assertTrue(
                browser_check.wait_for(listener.getsockname()[1], timeout=0.5)
            )

    def test_mock_board_needs_no_nested_process(self) -> None:
        previous_root = browser_check.mock_board.board.ROOT
        previous_scenario = browser_check.mock_board.SCENARIO
        previous_items = browser_check.mock_board.FIXTURE_ITEMS
        with browser_check.browser_fixture() as fixture_root:
            port = browser_check.free_port()
            with mock.patch.object(
                browser_check.subprocess,
                "Popen",
                side_effect=AssertionError("browser fixture spawned a child process"),
            ):
                running = browser_check.start_board(port, "healthy", fixture_root)
                try:
                    self.assertTrue(browser_check.wait_for(port, timeout=0.5))
                finally:
                    browser_check.stop_board(running)
        self.assertEqual(previous_root, browser_check.mock_board.board.ROOT)
        self.assertEqual(previous_scenario, browser_check.mock_board.SCENARIO)
        self.assertIs(previous_items, browser_check.mock_board.FIXTURE_ITEMS)


class RetroPageBytes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        # The distributable's honest first-run page can contain no findings.
        # Byte-level action assertions use the same deterministic actionable
        # fixture as the DOM harness instead of depending on repository history.
        cls.html = Harness._retro_fixture().read_text(encoding="utf-8")

    def test_no_bare_fetch_outside_the_shared_client(self) -> None:
        """Every board call goes through Board.request (review finding 4).

        A `fetch(` anywhere other than inside Board.request is a call that has
        not been checked for response.ok, non-JSON, or a dead port -- which is
        precisely the defect that made the page look broken.
        """
        calls = [m.start() for m in re.finditer(r"\bfetch\(", self.html)]
        self.assertEqual(len(calls), 1, "exactly one fetch( -- the one inside Board.request")
        window = self.html[calls[0] - 1200:calls[0]]
        self.assertIn("function request(", window)

    def test_every_control_is_declared_to_the_board(self) -> None:
        self.assertIn("data-board-control", self.html)
        for cls in ("approve-btn", "comment", "defer-reason", "defer-confirm-btn"):
            for m in re.finditer(r'class="[^"]*\b%s\b[^"]*"' % cls, self.html):
                tag_start = self.html.rfind("<", 0, m.start())
                tag = self.html[tag_start:self.html.index(">", m.start())]
                self.assertIn("data-board-control", tag, f"{cls} must be disable-able: {tag}")

    def test_two_lists_exist(self) -> None:
        self.assertIn('id="list-toaction"', self.html)
        self.assertIn('id="list-approved"', self.html)

    def test_prompt_is_readable_before_approving(self) -> None:
        self.assertIn("prompt-block", self.html)
        self.assertIn("read it before approving", self.html)

    def test_approval_copy_distinguishes_decision_from_optional_spend(self) -> None:
        self.assertIn("may spend quota", self.html)
        self.assertIn("does not launch a worker", self.html)
        self.assertIn("committed baseline is unavailable", self.html)

    def test_stalled_state_has_its_own_visual(self) -> None:
        self.assertIn("stall-pulse", self.html)
        self.assertIn(".status-slot.stalled", self.html)

    def test_dead_endpoints_are_gone(self) -> None:
        scripts = "\n".join(re.findall(r"<script[^>]*>(.*?)</script>", self.html, re.S))
        for path in ("/api/dispatch/prepare", "/api/dispatch/run", "/api/decision"):
            self.assertNotIn(path, scripts, f"{path} was removed by the browser contract")

    def test_inlined_prompt_matches_render_prompt(self) -> None:
        """What the page shows with no board is what the board would send."""
        sys.path.insert(0, str(ROOT / "tools"))
        import retro_queue
        for slug in retro_queue.load_index().get("items", []):
            item = retro_queue.load_item(slug)
            if item is None:
                continue
            self.assertEqual(retro_queue.render_prompt(item, ""), item["prompt"])

    def test_tabs_and_wedge_banner_present_above_them(self) -> None:
        """Approved mock: banner sits above the tabs so the alarm survives
        being on another tab."""
        self.assertIn('id="wedge-banner"', self.html)
        self.assertIn('class="tabs"', self.html)
        banner_i = self.html.index('id="wedge-banner"')
        tabs_i = self.html.index('class="tabs"')
        self.assertLess(banner_i, tabs_i)

    def test_only_one_panel_is_visible_at_a_time(self) -> None:
        for kind in ("toaction", "approved", "deferred"):
            i = self.html.index('id="panel-%s"' % kind)
            window = self.html[i:i + 60]
            hidden = "hidden" in window
            self.assertEqual(hidden, kind != "toaction",
                              f"panel-{kind} hidden={hidden}")

    def test_decision_form_shows_one_text_field_until_defer_is_clicked(self) -> None:
        """Comment textarea is always visible; the defer reason input starts
        inside a hidden .deferbox, so exactly one text field shows per card
        until "Defer instead" is clicked."""
        for m in re.finditer(r'<div class="decision-form"[^>]*>', self.html):
            end = self.html.index("thread-slot", m.end())
            chunk = self.html[m.end():end]
            self.assertIn('<div class="deferbox" hidden>', chunk)


class Ordering(unittest.TestCase):
    """The ordering rule: pure, deterministic, and the one the caption states."""

    ROWS = [
        {"title": "a", "cost": 2.5, "state": "", "updated_at": "2026-01-01"},
        {"title": "b", "cost": None, "state": "working", "updated_at": "2026-01-02"},
        {"title": "c", "cost": 160.0, "state": "failed", "updated_at": "2026-01-09"},
        {"title": "d", "cost": 60.0, "state": "queued", "updated_at": "2026-01-03"},
        {"title": "e", "cost": None, "state": "done", "updated_at": "2026-01-08"},
        {"title": "f", "cost": 60.0, "state": "stalled", "updated_at": "2026-01-04"},
        {"title": "g", "cost": 0.0, "state": "", "updated_at": ""},
        {"title": "h", "cost": None, "state": "approved", "updated_at": "2026-01-05"},
    ]

    def test_to_action_is_cost_descending_then_unranked(self) -> None:
        got = [r["title"] for r in retro_html.order_rows("toaction", self.ROWS)]
        self.assertEqual(got, ["c", "d", "f", "a", "g", "b", "e", "h"])

    def test_approved_puts_attention_before_live_queue_and_verified(self) -> None:
        got = [r["title"] for r in retro_html.order_rows("approved", self.ROWS)]
        # failed needs attention; accepted-without-worker follows; then
        # working/stalled (f, b -- newest first), queued, verified, unknown.
        self.assertEqual(got, ["c", "h", "f", "b", "d", "e", "a", "g"])

    def test_deferred_is_most_recent_first(self) -> None:
        got = [r["title"] for r in retro_html.order_rows("deferred", self.ROWS)]
        self.assertEqual(got, ["c", "e", "h", "f", "d", "b", "a", "g"])

    def test_order_does_not_depend_on_input_order(self) -> None:
        """Same set in any order renders the same list.

        This is the property that stops the page changing shape because a
        dict, a JSON file or a glob happened to enumerate differently.
        """
        rng = random.Random(20260819)
        for kind in ("toaction", "approved", "deferred"):
            want = [r["title"] for r in retro_html.order_rows(kind, self.ROWS)]
            for _ in range(50):
                shuffled = self.ROWS[:]
                rng.shuffle(shuffled)
                got = [r["title"] for r in retro_html.order_rows(kind, shuffled)]
                self.assertEqual(got, want, f"{kind} order changed under shuffling")

    def test_repeated_calls_are_identical(self) -> None:
        for kind in ("toaction", "approved", "deferred"):
            first = retro_html.order_rows(kind, self.ROWS)
            self.assertEqual(first, retro_html.order_rows(kind, self.ROWS))

    def test_browser_uses_the_same_state_ranking(self) -> None:
        """The JS mirror of APPROVED_STATE_ORDER is checked, not trusted.

        Two copies of a rule disagree within a revision unless something
        compares them, so the emitted script is read back and its table is
        held to the Python one.
        """
        html = generated("retro.html", lambda: retro_html.main())
        m = re.search(r"var STATE_ORDER = \{([^}]*)\}", html)
        self.assertIsNotNone(m, "the page must emit its own state ranking")
        pairs = dict(re.findall(r"(\w+):(\d+)", m.group(1)))
        self.assertEqual({k: int(v) for k, v in pairs.items()},
                         retro_html.APPROVED_STATE_ORDER)
        self.assertIn("var UNKNOWN_RANK = %d;" % retro_html.UNKNOWN_STATE_RANK, html)


class ListsOnThePage(unittest.TestCase):
    """What the served bytes actually say, list by list."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = Harness._retro_fixture().read_text(encoding="utf-8")

    def test_three_lists_each_with_a_heading_of_its_own(self) -> None:
        for kind in ("toaction", "approved", "deferred"):
            self.assertIn('id="list-%s"' % kind, self.html)
            self.assertIn('id="panel-%s"' % kind, self.html)
            self.assertIn('data-count-for="%s"' % kind, self.html)
        self.assertIn("Activity", self.html)

    def test_the_ordering_rule_is_stated_on_the_page(self) -> None:
        for caption in retro_html.ORDER_CAPTIONS.values():
            self.assertIn(caption, self.html)

    def test_deferred_is_collapsed_by_default(self) -> None:
        """Deferred renders behind a tab, hidden until the tab is clicked."""
        i = self.html.index('id="panel-deferred"')
        window = self.html[i:i + 60]
        self.assertIn("hidden", window)

    def test_no_settled_card_is_emitted_into_the_to_action_list(self) -> None:
        """The defect this page exists to prevent: a settled item read as one
        awaiting a decision."""
        for card in page_parts.cards(self.html, "list-toaction"):
            self.assertEqual(card.get("data-decided", ""), "",
                             f"{card.get('data-slug')} is settled but sits in To action")
        for card in page_parts.cards(self.html, "list-approved"):
            self.assertEqual(card.get("data-decided"), "approved")
        for card in page_parts.cards(self.html, "list-deferred"):
            self.assertEqual(card.get("data-decided"), "deferred")

    def test_cards_are_rendered_in_the_ordering_rule_s_order(self) -> None:
        for kind in ("toaction", "approved", "deferred"):
            got = page_parts.cards(self.html, "list-" + kind)
            rows = [{"title": c.get("data-title", ""),
                     "cost": None if c.get("data-cost", "") == "" else float(c["data-cost"]),
                     "state": "",
                     "updated_at": c.get("data-updated", "")} for c in got]
            want = [r["title"] for r in retro_html.order_rows(kind, rows)]
            self.assertEqual([r["title"] for r in rows], want, kind)

    def test_every_card_in_a_list_is_numbered(self) -> None:
        """All or none -- 2 numbered cards out of 5 reads as broken."""
        for kind in ("toaction", "approved", "deferred"):
            got = page_parts.ranks(self.html, "list-" + kind)
            self.assertEqual(got, [f"{i}." for i in range(1, len(got) + 1)], kind)

    def test_an_unranked_finding_says_why_it_has_no_cost(self) -> None:
        finding = {
            "title": "synthetic observation",
            "cost": None,
            "effort": None,
            "notes": ["unranked: no attributable human turns"],
            "body": "**Problem** - Evidence is incomplete.\n\n"
                    "**Proposal** - Capture evidence.\n\n"
                    "**Measure** - The next report has a citation.",
            "sessions": [],
            "human_turns": [],
            "mechanical": [],
            "recurs": False,
            "severity": "none",
            "fix_files": [],
            "fix_lines": 1,
        }
        rendered = retro_html.render_finding(1, finding, {}, {}, {})
        self.assertIn("unranked-note", rendered)
        self.assertIn("Observation only", rendered)
        self.assertIn("no attributable human turns", rendered)

    def test_the_primary_control_is_legible(self) -> None:
        """Filled, in the accent blue -- not amber, which is reserved for the
        quota warning below the button row -- and allowed to wrap rather than
        overflow its border."""
        m = re.search(r"\.approve-btn\{([^}]*)\}", self.html)
        self.assertIsNotNone(m)
        rule = m.group(1)
        self.assertIn("background:var(--acc)", rule)
        self.assertIn("color:#08111d", rule)
        self.assertIn("white-space:normal", rule)
        self.assertIn("box-sizing:border-box", rule)

    def test_decision_content_precedes_collapsed_audit_detail(self) -> None:
        """Problem, recommendation and measure are visible before the action."""
        self.assertIn('<details class="detail-block">', self.html)
        self.assertIn("Evidence and dispatch details", self.html)
        for card in page_parts.cards(self.html, "list-toaction"):
            slug = card.get("data-slug", "")
            if not slug:
                continue
            start = self.html.index('data-slug="%s"' % slug)
            decision = self.html.index('<div class="decision', start)
            detail = self.html.index('<details class="detail-block">', start)
            self.assertIn("Recommended change", self.html[start:decision])
            self.assertIn("Success looks like", self.html[start:decision])
            self.assertLess(decision, detail)

    def test_legacy_findings_expose_the_recommendation(self) -> None:
        sections = retro_html.decision_sections(
            "The workflow requires five scripts.\n\n"
            "**Proposed kit/process change** — expose one command.\n\n"
            "**Mechanically checkable** — the command routes all operations."
        )
        self.assertEqual(sections["problem"], "The workflow requires five scripts.")
        self.assertEqual(sections["proposal"], "expose one command.")
        self.assertEqual(sections["measure"], "the command routes all operations.")


class PlanPageBytes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = generated("plan.html", lambda: plan_html.main())
        fixture = architecture_fixture()
        cls.architecture_model = plan_html.architecture_map_model(
            fixture["proposal"],
            fixture["tree"],
            fixture["built"],
            fixture["modules"],
            cockpit_state=fixture["cockpit_state"],
            present=fixture["present"],
            baseline_files=fixture["baseline_files"],
            involvement=fixture["shape"]["involvement"],
            function_changes=fixture["function_changes"],
        )
        cls.architecture_html = architecture_fixture_html()

    @staticmethod
    def _architecture_test_model(
        proposal: dict,
        *,
        tree: dict | None = None,
        present: set[str] | None = None,
        baseline_files: set[str] | None = None,
        changed_actions: dict[str, str] | None = None,
        modules: list[dict] | None = None,
        cockpit_state: dict | None = None,
        involvement: str = "function",
        function_changes: object = _DEFAULT_FUNCTION_CHANGES,
    ) -> dict:
        current = set() if present is None else set(present)
        baseline = set() if baseline_files is None else set(baseline_files)
        return plan_html.architecture_map_model(
            proposal,
            tree or {},
            current,
            modules or [],
            {} if changed_actions is None else changed_actions,
            cockpit_state or {},
            current,
            baseline,
            involvement,
            (
                {}
                if function_changes is _DEFAULT_FUNCTION_CHANGES
                else function_changes
            ),
        )

    def test_architecture_payload_contains_the_complete_source_tree(self) -> None:
        by_id = {
            str(node.get("id")): node
            for node in self.architecture_model["nodes"]
        }
        expected = {
            "folder:scripts",
            "folder:scripts/logic",
            "file:scripts/logic/action_service.gd",
            "class:scripts/logic/action_service.gd::ActionService",
            "function:scripts/logic/action_service.gd::ActionService.submit_action",
            "file:scripts/logic/feedback_event.gd",
            "class:scripts/logic/feedback_event.gd::FeedbackEvent",
            "function:scripts/logic/feedback_event.gd::FeedbackEvent.describe_feedback",
        }
        self.assertTrue(expected.issubset(by_id), sorted(by_id))
        self.assertEqual(
            "file:scripts/logic/action_service.gd",
            by_id[
                "class:scripts/logic/action_service.gd::ActionService"
            ]["parent"],
        )
        self.assertEqual(
            "class:scripts/logic/action_service.gd::ActionService",
            by_id[
                "function:scripts/logic/action_service.gd::ActionService.submit_action"
            ]["parent"],
        )
        planned = by_id[
            "function:scripts/logic/feedback_event.gd::FeedbackEvent.describe_feedback"
        ]
        self.assertEqual("adding", planned["state"])
        self.assertEqual("func describe_feedback() -> String", planned["signature"])
        self.assertEqual(
            "scripts/logic/action_service.gd",
            planned["options"][0]["name"],
        )
        self.assertIn("window.__KIT_ARCHITECTURE_MAP__", self.architecture_html)

    def test_complete_map_includes_present_authored_non_gdscript_files(self) -> None:
        path = "data/player_actions.json"
        model = self._architecture_test_model(
            {}, present={path}, baseline_files={path}
        )
        node = next(
            item for item in model["nodes"] if item["id"] == f"file:{path}"
        )
        self.assertEqual("file", node["kind"])
        self.assertEqual("existing", node["state"])
        self.assertEqual(f"The authored game file {path}.", node["what"])

    def test_tree_entries_absent_from_present_files_are_excluded(self) -> None:
        ghost = "scripts/logic/ghost.gd"
        model = self._architecture_test_model(
            {
                "status": "draft",
            },
            tree={
                ghost: {
                    "class_name": "Ghost",
                    "functions": [{
                        "name": "haunt",
                        "identity": "Ghost.haunt",
                        "class_scope": "Ghost",
                        "signature": "func haunt() -> void",
                    }],
                }
            },
            present=set(),
            baseline_files=set(),
        )
        ids = {str(item["id"]) for item in model["nodes"]}
        self.assertNotIn(f"file:{ghost}", ids)
        self.assertFalse(any(ghost in item for item in ids), ids)

    def test_unknown_inventory_is_explicit_and_never_uses_built_as_proof(self) -> None:
        path = "scripts/logic/planned.gd"
        model = plan_html.architecture_map_model(
            {
                "status": "draft",
                "files": [{"path": path, "action": "new", "why": "Add it."}],
            },
            {},
            {path},
            [],
            None,
            {"status": "draft", "approval_required": True},
            None,
            None,
            "file",
            None,
        )
        node = next(item for item in model["nodes"] if item["id"] == f"file:{path}")

        self.assertFalse(model["present_known"])
        self.assertFalse(model["comparison_known"])
        self.assertEqual([], model["present_paths"])
        self.assertIn("progress is unknown", node["changing"])
        page = plan_html.architecture_map_html(model, "file")
        self.assertIn("Known and planned", page)
        self.assertIn("full current source inventory is not known", page)
        self.assertNotIn(">Complete</button>", page)

    def test_architecture_inventory_carries_full_paths_and_filters_kit_files(self) -> None:
        game = "scripts/logic/game_service.gd"
        kit_private = ".kit/runtime/private.json"
        kit_helper = "tools/validate_resources.gd"
        model = self._architecture_test_model(
            {
                "files": [
                    {"path": game, "action": "modify", "why": "Change the game."},
                    {"path": kit_private, "action": "modify", "why": "Never show."},
                ]
            },
            present={game, kit_private, kit_helper},
            baseline_files={game, kit_private, kit_helper},
            changed_actions={game: "modify", kit_private: "modify"},
        )
        ids = {str(item["id"]) for item in model["nodes"]}

        self.assertEqual([game], model["present_paths"])
        self.assertEqual([game], model["touched_paths"])
        self.assertEqual([game], model["baseline_paths"])
        self.assertIn(f"file:{game}", ids)
        self.assertFalse(any(".kit" in item for item in ids), ids)
        self.assertFalse(any("validate_resources" in item for item in ids), ids)

    def test_declared_and_observed_action_mismatch_is_a_conflict(self) -> None:
        path = "scripts/logic/conflicted.gd"
        model = self._architecture_test_model(
            {
                "status": "recorded",
                "files": [{
                    "path": path,
                    "action": "new",
                    "why": "Add one isolated result.",
                }],
            },
            present={path},
            baseline_files={path},
            changed_actions={path: "modify"},
        )
        node = next(
            item for item in model["nodes"] if item["id"] == f"file:{path}"
        )
        self.assertEqual("conflict", node["state"])
        self.assertIn("plan says new", node["changing"])
        self.assertIn("Git reports modify", node["changing"])

    def test_hands_off_scope_keeps_plan_git_and_authority_separate(self) -> None:
        path = "scripts/logic/new_service.gd"
        model = self._architecture_test_model(
            {
                "status": "recorded",
                "scope": [
                    {
                        "kind": "directory",
                        "path": "(root)",
                        "action": "new",
                        "why": "New authored files are reversible.",
                    },
                    {
                        "kind": "directory",
                        "path": "scripts/logic",
                        "action": "modify",
                        "why": "Existing logic may only be modified.",
                    },
                ],
            },
            present={path},
            baseline_files=set(),
            changed_actions={path: "new"},
            involvement="hands-off",
            cockpit_state={
                "status": "recorded",
                "approval_required": False,
            },
        )

        node = next(
            item for item in model["nodes"] if item["id"] == f"file:{path}"
        )
        self.assertEqual("modify", node["declared_action"])
        self.assertEqual("new", node["observed_action"])
        self.assertEqual(
            "proposal.scope[directory:scripts/logic]",
            node["authorization_source"],
        )
        self.assertEqual("conflict", node["state"])
        self.assertEqual(
            "does not authorize the observed action",
            node["authorization_status"],
        )

    def test_unplanned_observed_file_never_reads_as_planned(self) -> None:
        path = "scripts/logic/unplanned.gd"
        model = self._architecture_test_model(
            {"status": "approved"},
            present={path},
            baseline_files=set(),
            changed_actions={path: "new"},
            cockpit_state={
                "status": "approved",
                "approval_required": False,
                "design_authority": {"authority": "human-confirmed"},
            },
        )
        node = next(
            item for item in model["nodes"] if item["id"] == f"file:{path}"
        )
        self.assertEqual("unplanned", node["state"])
        self.assertIn("unplanned new", node["changing"])
        self.assertNotIn("planned addition", node["changing"].lower())
        self.assertTrue(node["design"].startswith("No."), node["design"])

    def test_design_alignment_never_says_yes_without_exact_authority(self) -> None:
        path = "scripts/logic/proposed.gd"
        proposal = {
            "status": "draft",
            "design_refs": [{
                "section": "docs/design/outcome.md",
                "why": "The player must understand an accepted action.",
            }],
            "files": [{"path": path, "action": "new", "why": "Show it."}],
        }

        draft = self._architecture_test_model(
            proposal,
            present=set(),
            baseline_files=set(),
            cockpit_state={
                "status": "draft",
                "approval_required": True,
                "design_authority": {"authority": "human-confirmed"},
            },
        )
        provisional = self._architecture_test_model(
            proposal,
            present=set(),
            baseline_files=set(),
            cockpit_state={
                "status": "recorded",
                "approval_required": False,
                "design_authority": {"authority": "agent-provisional"},
            },
        )
        unplanned = self._architecture_test_model(
            {"status": "approved", "design_refs": proposal["design_refs"]},
            present={path},
            baseline_files=set(),
            changed_actions={path: "new"},
            cockpit_state={
                "status": "approved",
                "approval_required": False,
                "design_authority": {"authority": "human-confirmed"},
            },
        )
        for name, model in (
            ("draft", draft),
            ("provisional", provisional),
            ("unplanned", unplanned),
        ):
            with self.subTest(state=name):
                node = next(
                    item for item in model["nodes"] if item["id"] == f"file:{path}"
                )
                self.assertFalse(node["design"].startswith("Yes"), node["design"])

    def test_design_checks_are_independent_and_require_current_digest(self) -> None:
        path = "scripts/logic/proposed.gd"
        section = "docs/design/outcome.md"
        digest = "a" * 64
        proposal = {
            "status": "approved",
            "design_refs": [{
                "section": section,
                "why": "The player understands every accepted action.",
                "sha256": digest,
            }],
            "files": [{"path": path, "action": "new", "why": "Show it."}],
        }
        cockpit_state = {
            "status": "approved",
            "approval_required": False,
            "exact_approval": {"approved": True},
            "design_authority": {
                "authority": "human-confirmed",
                "validation": "current",
                "implementation_eligible": True,
            },
            "design_documents": [{"path": section, "sha256": digest}],
        }

        current = self._architecture_test_model(
            proposal,
            present={path},
            baseline_files=set(),
            changed_actions={path: "new"},
            cockpit_state=cockpit_state,
        )
        node = next(item for item in current["nodes"] if item["id"] == f"file:{path}")
        self.assertTrue(node["design"].startswith("Yes"), node["design"])
        self.assertEqual([section], node["design_evidence"]["references"])
        self.assertEqual(
            "all cited digests match current design",
            node["design_evidence"]["digest"],
        )
        self.assertIn("human-confirmed", node["design_evidence"]["authority"])
        self.assertEqual("approved", node["design_evidence"]["proposal"])

        stale = self._architecture_test_model(
            proposal,
            present={path},
            baseline_files=set(),
            changed_actions={path: "new"},
            cockpit_state={
                **cockpit_state,
                "design_documents": [{"path": section, "sha256": "b" * 64}],
            },
        )
        stale_node = next(
            item for item in stale["nodes"] if item["id"] == f"file:{path}"
        )
        self.assertFalse(stale_node["design"].startswith("Yes"), stale_node["design"])
        self.assertIn("does not match", stale_node["design_evidence"]["digest"])

    def test_proposal_cannot_self_authorize_design_or_implementation(self) -> None:
        path = "scripts/logic/proposed.gd"
        model = self._architecture_test_model(
            {
                "status": "approved",
                "design_authority": {
                    "authority": "human-confirmed",
                    "validation": "current",
                    "implementation_eligible": True,
                },
                "design_refs": [{
                    "section": "docs/design/outcome.md",
                    "sha256": "a" * 64,
                    "why": "The player understands every accepted action.",
                }],
                "files": [{"path": path, "action": "new", "why": "Show it."}],
            },
            present={path},
            baseline_files=set(),
            changed_actions={path: "new"},
            cockpit_state={},
        )
        node = next(item for item in model["nodes"] if item["id"] == f"file:{path}")
        self.assertEqual("not active", node["authorization_status"])
        self.assertIn("not recorded", node["design_evidence"]["authority"])
        self.assertFalse(node["design"].startswith("Yes"), node["design"])

    def test_go_no_go_reversibility_tells_the_agent_to_stop(self) -> None:
        path = "scripts/logic/commitment.gd"
        model = self._architecture_test_model(
            {
                "status": "recorded",
                "reversibility": {
                    "state": "go-no-go",
                    "hard_to_undo": "Authored content will depend on this format.",
                    "veto_scope": "Only the isolated draft can be removed.",
                },
                "files": [{"path": path, "action": "new", "why": "Add it."}],
            },
            present=set(),
            baseline_files=set(),
        )
        node = next(
            item for item in model["nodes"] if item["id"] == f"file:{path}"
        )
        self.assertIn("go/no-go point", node["undo"])
        self.assertIn("Stop for explicit approval", node["undo"])

    def test_top_level_function_without_scope_parents_to_source_class(self) -> None:
        path = "main.gd"
        model = self._architecture_test_model(
            {},
            tree={
                path: {
                    "class_name": "Main",
                    "functions": [{
                        "name": "run",
                        "identity": "run",
                        "signature": "func run() -> void",
                    }],
                }
            },
            present={path},
            baseline_files={path},
        )
        function = next(
            item
            for item in model["nodes"]
            if item["id"] == "function:main.gd::run"
        )
        self.assertEqual("class:main.gd::Main", function["parent"])

    def test_missing_planned_modify_function_says_it_is_not_present(self) -> None:
        path = "scripts/logic/service.gd"
        model = self._architecture_test_model(
            {
                "status": "recorded",
                "files": [{"path": path, "action": "modify", "why": "Refine it."}],
                "functions": [{
                    "file": path,
                    "class_scope": "Service",
                    "signature": "func refresh(value: int) -> void",
                    "action": "modify",
                    "why": "Accept the typed value.",
                }],
            },
            tree={path: {"class_name": "Service", "functions": []}},
            present={path},
            baseline_files={path},
            changed_actions={path: "modify"},
        )
        node = next(
            item
            for item in model["nodes"]
            if item["id"] == f"function:{path}::Service.refresh"
        )
        self.assertEqual("conflict", node["state"])
        self.assertIn("absent", node["changing"])
        self.assertIn("nothing to modify", node["changing"].lower())

    def test_module_involvement_inherits_the_declared_module_state(self) -> None:
        path = "scripts/logic/action_service.gd"
        model = self._architecture_test_model(
            {
                "status": "recorded",
                "modules": [{
                    "path": "scripts/logic",
                    "action": "modify",
                    "role": "Owns action decisions.",
                    "why": "Adjust the module as one boundary.",
                }],
            },
            tree={path: {
                "class_name": "ActionService",
                "functions": [{
                    "name": "run",
                    "identity": "ActionService.run",
                    "class_scope": "ActionService",
                    "signature": "func run() -> void",
                }],
            }},
            present={path},
            baseline_files={path},
            changed_actions={path: "modify"},
            modules=[{"path": "scripts/logic", "depends_on": []}],
            involvement="module",
            function_changes={
                (path, "ActionService.run"): {
                    "action": "modify",
                    "signature": "func run() -> void",
                    "baseline_signature": "func run() -> void",
                    "current_signature": "func run() -> void",
                }
            },
        )
        by_id = {str(item["id"]): item for item in model["nodes"]}
        for node_id in (
            f"file:{path}",
            f"class:{path}::ActionService",
        ):
            with self.subTest(node=node_id):
                self.assertEqual("changing", by_id[node_id]["state"])
                self.assertEqual("modify", by_id[node_id]["declared_action"])
                self.assertEqual("modify", by_id[node_id]["observed_action"])
                self.assertEqual(
                    "proposal.modules[scripts/logic]",
                    by_id[node_id]["authorization_source"],
                )
        function = by_id[f"function:{path}::ActionService.run"]
        self.assertEqual("changing", function["state"])
        self.assertEqual(
            "proposal.modules[scripts/logic]", function["authorization_source"]
        )
        self.assertEqual("changing", by_id["folder:scripts/logic"]["state"])

    def test_module_summary_carries_hidden_descendant_conflict(self) -> None:
        path = "scripts/logic/action_service.gd"
        model = self._architecture_test_model(
            {
                "status": "approved",
                "modules": [{
                    "path": "scripts/logic",
                    "action": "modify",
                    "role": "Owns action decisions.",
                    "why": "Adjust the module boundary.",
                }],
                "files": [{
                    "path": path,
                    "action": "new",
                    "why": "This contradicts the observed modification.",
                }],
            },
            tree={path: {"class_name": "ActionService", "functions": []}},
            present={path},
            baseline_files={path},
            changed_actions={path: "modify"},
            modules=[{"path": "scripts/logic", "depends_on": []}],
            involvement="module",
            cockpit_state={
                "status": "approved",
                "approval_required": False,
                "exact_approval": {"approved": True},
            },
        )
        module = next(
            item
            for item in model["nodes"]
            if item["id"] == "folder:scripts/logic"
        )

        self.assertEqual("conflict", module["state"])
        self.assertIn("inside this module conflicts", module["changing"])
        rendered = plan_html.architecture_map_html(model, "module")
        payload = rendered.split(
            "window.__KIT_ARCHITECTURE_MAP__=", 1
        )[1].split(";</script>", 1)[0]
        visible_model = json.loads(payload)
        self.assertEqual(
            ["folder:scripts/logic"],
            [node["id"] for node in visible_model["nodes"]],
        )
        self.assertEqual("conflict", visible_model["nodes"][0]["state"])

    def test_observed_function_changes_mark_unplanned_new_and_modify(self) -> None:
        path = "scripts/logic/service.gd"
        signature = "func run(value: int) -> void"
        tree = {
            path: {
                "class_name": "Service",
                "functions": [{
                    "name": "run",
                    "identity": "Service.run",
                    "class_scope": "Service",
                    "signature": signature,
                }],
            }
        }
        evidence_by_action = {
            "new": {
                "action": "new",
                "signature": signature,
                "baseline_signature": "",
                "current_signature": signature,
            },
            "modify": {
                "action": "modify",
                "signature": signature,
                "baseline_signature": "func run(value: String) -> void",
                "current_signature": signature,
            },
        }

        for action, evidence in evidence_by_action.items():
            with self.subTest(action=action):
                model = self._architecture_test_model(
                    {"status": "recorded"},
                    tree=tree,
                    present={path},
                    baseline_files=(set() if action == "new" else {path}),
                    changed_actions={path: action},
                    function_changes={(path, "Service.run"): evidence},
                )
                node = next(
                    item
                    for item in model["nodes"]
                    if item["id"] == f"function:{path}::Service.run"
                )
                self.assertEqual("unplanned", node["state"])
                self.assertEqual("none", node["declared_action"])
                self.assertEqual(action, node["observed_action"])
                self.assertEqual("none", node["authorization_source"])
                self.assertIn(f"unplanned {action}", node["changing"])
                self.assertNotIn("No change is proposed", node["changing"])

    def test_unplanned_function_delete_remains_visible_as_a_tombstone(self) -> None:
        path = "scripts/logic/service.gd"
        signature = "func obsolete() -> void"
        model = self._architecture_test_model(
            {"status": "recorded"},
            tree={path: {"class_name": "Service", "functions": []}},
            present={path},
            baseline_files={path},
            changed_actions={path: "modify"},
            function_changes={
                (path, "Service.obsolete"): {
                    "action": "delete",
                    "signature": signature,
                    "baseline_signature": signature,
                    "current_signature": "",
                }
            },
        )
        node = next(
            item
            for item in model["nodes"]
            if item["id"] == f"function:{path}::Service.obsolete::deleted"
        )
        self.assertEqual("unplanned", node["state"])
        self.assertEqual("none", node["declared_action"])
        self.assertEqual("delete", node["observed_action"])
        self.assertIn("unplanned delete", node["changing"])
        self.assertIn("deleted function", node["what"])
        self.assertEqual(f"class:{path}::Service", node["parent"])

    def test_planned_function_delete_requires_proven_baseline_comparison(self) -> None:
        path = "scripts/logic/service.gd"
        signature = "func obsolete() -> void"
        proposal = {
            "status": "recorded",
            "files": [{"path": path, "action": "modify", "why": "Retire it."}],
            "functions": [{
                "file": path,
                "class_scope": "Service",
                "signature": signature,
                "action": "delete",
                "why": "The responsibility no longer exists.",
            }],
        }
        common = {
            "tree": {path: {"class_name": "Service", "functions": []}},
            "present": {path},
            "baseline_files": {path},
            "changed_actions": {path: "modify"},
        }
        proven = self._architecture_test_model(
            proposal,
            **common,
            function_changes={
                (path, "Service.obsolete"): {
                    "action": "delete",
                    "signature": signature,
                    "baseline_signature": signature,
                    "current_signature": "",
                }
            },
        )
        unproven = self._architecture_test_model(
            proposal,
            **common,
            function_changes={},
        )
        unknown = self._architecture_test_model(
            proposal,
            **common,
            function_changes=None,
        )

        node_id = f"function:{path}::Service.obsolete"
        proven_node = next(item for item in proven["nodes"] if item["id"] == node_id)
        unproven_node = next(
            item for item in unproven["nodes"] if item["id"] == node_id
        )
        unknown_node = next(
            item for item in unknown["nodes"] if item["id"] == node_id
        )
        self.assertEqual("removing", proven_node["state"])
        self.assertIn("removal is complete", proven_node["changing"])
        self.assertEqual("conflict", unproven_node["state"])
        self.assertNotIn("complete", unproven_node["changing"].lower())
        self.assertEqual("removing", unknown_node["state"])
        self.assertIn("comparison is unavailable", unknown_node["changing"])
        self.assertIn("not proven", unknown_node["changing"])
        self.assertNotIn("complete", unknown_node["changing"].lower())

    def test_delete_event_without_prior_function_never_counts_as_complete(self) -> None:
        path = "scripts/logic/service.gd"
        signature = "func obsolete() -> void"
        model = self._architecture_test_model(
            {
                "status": "approved",
                "functions": [{
                    "file": path,
                    "class_scope": "Service",
                    "signature": signature,
                    "action": "delete",
                    "why": "Remove it.",
                }],
            },
            tree={path: {"class_name": "Service", "functions": []}},
            present={path},
            baseline_files={path},
            changed_actions={path: "modify"},
            function_changes={
                (path, "Service.obsolete"): {
                    "action": "delete",
                    "signature": signature,
                    "baseline_signature": "",
                    "current_signature": "",
                }
            },
        )
        node = next(
            item
            for item in model["nodes"]
            if item["id"] == f"function:{path}::Service.obsolete"
        )

        self.assertEqual("conflict", node["state"])
        self.assertNotIn("complete", node["changing"].lower())

    def test_planned_function_presence_is_unknown_without_source_inventory(self) -> None:
        path = "scripts/logic/service.gd"
        model = plan_html.architecture_map_model(
            {
                "status": "draft",
                "functions": [{
                    "file": path,
                    "class_scope": "Service",
                    "signature": "func refresh() -> void",
                    "action": "new",
                    "why": "Refresh it.",
                }],
            },
            {},
            {path},
            [],
            None,
            {"status": "draft", "approval_required": True},
            None,
            None,
            "function",
            None,
        )
        node = next(
            item
            for item in model["nodes"]
            if item["id"] == f"function:{path}::Service.refresh"
        )

        self.assertEqual("adding", node["state"])
        self.assertIn("whether the planned function is present is not known", node["changing"])
        self.assertNotIn("implemented", node["changing"].lower())

    def test_exact_function_evidence_can_prove_presence_without_tree_entry(self) -> None:
        path = "scripts/logic/service.gd"
        signature = "func refresh() -> void"
        model = self._architecture_test_model(
            {
                "functions": [{
                    "file": path,
                    "class_scope": "Service",
                    "signature": signature,
                    "action": "new",
                    "why": "Refresh it.",
                }],
            },
            tree={},
            present={path},
            baseline_files=set(),
            changed_actions={path: "new"},
            function_changes={
                (path, "Service.refresh"): {
                    "action": "new",
                    "signature": signature,
                    "baseline_signature": "",
                    "current_signature": signature,
                }
            },
        )
        node = next(
            item
            for item in model["nodes"]
            if item["id"] == f"function:{path}::Service.refresh"
        )

        self.assertEqual("adding", node["state"])
        self.assertIn("exact planned function addition is present", node["changing"])

    def test_file_and_class_dependencies_name_containing_module_evidence(self) -> None:
        path = "scripts/logic/service.gd"
        model = self._architecture_test_model(
            {},
            tree={path: {"class_name": "Service", "functions": []}},
            present={path},
            baseline_files={path},
            modules=[
                {"path": "scripts/logic", "depends_on": []},
                {"path": "scripts/ui", "depends_on": ["scripts/logic"]},
            ],
        )
        by_id = {str(item["id"]): item for item in model["nodes"]}
        for node_id in (f"file:{path}", f"class:{path}::Service"):
            with self.subTest(node=node_id):
                dependencies = by_id[node_id]["dependencies"].lower()
                dependents = by_id[node_id]["dependents"].lower()
                self.assertIn("module-level evidence", dependencies)
                self.assertIn("inside scripts/logic", dependencies)
                self.assertIn("current dependencies: none", dependencies)
                self.assertIn("current dependents", dependents)
                self.assertIn("scripts/ui", dependents)

    def test_observed_and_proposed_dependency_edges_keep_direction_and_source(self) -> None:
        model = self._architecture_test_model(
            {
                "modules": [
                    {
                        "path": "scripts/logic",
                        "action": "modify",
                        "may_depend_on": ["scripts/ui"],
                    },
                    {
                        "path": "scripts/gameplay",
                        "action": "modify",
                        "may_depend_on": ["scripts/logic"],
                    },
                ]
            },
            modules=[
                {"path": "scripts/logic", "depends_on": ["scripts/data"]},
                {"path": "scripts/data", "depends_on": []},
                {"path": "scripts/ui", "depends_on": []},
                {"path": "scripts/presentation", "depends_on": ["scripts/logic"]},
            ],
        )
        logic = next(
            node for node in model["nodes"] if node["id"] == "folder:scripts/logic"
        )
        links = {
            (
                link["source"],
                link["target"],
                link["relation"],
                link["provenance"],
            )
            for link in model["links"]
        }

        self.assertIn("Current dependencies: scripts/data", logic["dependencies"])
        self.assertIn("Proposed allowed dependencies: scripts/ui", logic["dependencies"])
        self.assertIn("Current dependents: scripts/presentation", logic["dependents"])
        self.assertIn("Proposed allowed dependents: scripts/gameplay", logic["dependents"])
        self.assertIn(
            (
                "folder:scripts/logic",
                "folder:scripts/data",
                "depends-on",
                "observed",
            ),
            links,
        )
        self.assertIn(
            (
                "folder:scripts/logic",
                "folder:scripts/ui",
                "may-depend-on",
                "planned",
            ),
            links,
        )

    def test_no_javascript_list_is_server_filtered_by_involvement(self) -> None:
        fixture = architecture_fixture()
        for involvement, allowed in (
            ("module", {"folder"}),
            ("file", {"folder", "file"}),
            ("function", {"folder", "file", "module", "function"}),
        ):
            with self.subTest(involvement=involvement):
                model = plan_html.architecture_map_model(
                    fixture["proposal"],
                    fixture["tree"],
                    fixture["built"],
                    fixture["modules"],
                    {},
                    fixture["cockpit_state"],
                    fixture["present"],
                    fixture["baseline_files"],
                    involvement,
                    fixture["function_changes"],
                )
                page = plan_html.architecture_map_html(model, involvement)
                block = re.search(r"<noscript>([\s\S]*?)</noscript>", page)
                self.assertIsNotNone(block)
                kinds = set(re.findall(r'data-arch-kind="([^"]+)"', block.group(1)))
                self.assertTrue(kinds, block.group(1))
                self.assertTrue(kinds.issubset(allowed), kinds)
                if involvement == "module":
                    self.assertIn('data-arch-item="folder:scripts/logic"', block.group(1))
                    self.assertNotIn('data-arch-item="folder:scripts"', block.group(1))
                if involvement == "file":
                    self.assertIn('data-arch-kind="file"', block.group(1))
                if involvement == "function":
                    self.assertIn('data-arch-kind="module"', block.group(1))
                    self.assertIn('data-arch-kind="function"', block.group(1))

    def test_no_javascript_css_hides_dead_graph_interface(self) -> None:
        block = re.search(r"<noscript>([\s\S]*?)</noscript>", self.architecture_html)
        self.assertIsNotNone(block)
        styles = "".join(re.findall(r"<style>([\s\S]*?)</style>", block.group(1)))
        hidden_selectors: set[str] = set()
        for selectors, declarations in re.findall(
            r"([^{}]+)\{([^{}]+)\}", styles
        ):
            if re.search(r"display\s*:\s*none", declarations):
                hidden_selectors.update(
                    selector.strip() for selector in selectors.split(",")
                )
        for target in (
            ".arch-controls",
            ".arch-graph-shell",
            ".arch-legend",
            ".arch-inspector",
            ".arch-settled",
        ):
            with self.subTest(selector=target):
                self.assertTrue(
                    any(selector.endswith(target) for selector in hidden_selectors),
                    hidden_selectors,
                )

    def test_involvement_copy_promises_only_the_detail_it_can_show(self) -> None:
        def intro(page: str) -> str:
            match = re.search(
                r'<p class="arch-intro-copy">([\s\S]*?)</p>', page
            )
            self.assertIsNotNone(match)
            return match.group(1)

        def search_tag(page: str) -> str:
            match = re.search(r'<input\b[^>]*id="arch-search"[^>]*>', page)
            self.assertIsNotNone(match)
            return match.group(0)

        module = architecture_fixture_html("module")
        file_level = architecture_fixture_html("file")
        function = architecture_fixture_html("function")

        module_intro = intro(module).lower()
        self.assertIn("architecture modules", module_intro)
        self.assertIn("dependencies", module_intro)
        self.assertNotIn("class", module_intro)
        self.assertNotIn("function", module_intro)
        self.assertIn('placeholder="Module"', search_tag(module))
        self.assertIn("<h3>Current and planned modules</h3>", module)

        file_intro = intro(file_level).lower()
        self.assertIn("folder → file", file_intro)
        self.assertNotIn("class", file_intro)
        self.assertNotIn("function", file_intro)
        self.assertIn('placeholder="Folder or file"', search_tag(file_level))
        self.assertIn("<h3>Current and planned files</h3>", file_level)

        function_intro = intro(function).lower()
        self.assertIn("folder → file → class → function", function_intro)
        self.assertIn(
            'placeholder="Folder, file, class or function"',
            search_tag(function),
        )
        self.assertIn("<h3>Current and planned structure</h3>", function)

    def test_settled_badge_starts_hidden_until_graph_finishes(self) -> None:
        badge = re.search(
            r'<span\b[^>]*id="arch-settled"[^>]*>', self.architecture_html
        )
        self.assertIsNotNone(badge)
        self.assertRegex(badge.group(0), r"\bhidden\b")

    def test_architecture_layout_has_explicit_responsive_work_budgets(self) -> None:
        script = plan_html.ARCHITECTURE_MAP_JS
        self.assertIn("var maximumGraphNodes = 2000;", script)
        self.assertIn("var maximumGraphLinks = 12000;", script)

        node_guard = script.index(
            "renderedNodes.length>maximumGraphNodes"
        )
        link_guard = script.index(
            "renderedLinks.length>maximumGraphLinks"
        )
        layout_call = script.index("positions=layout(renderedNodes", node_guard)
        self.assertLess(node_guard, link_guard)
        self.assertLess(link_guard, layout_call)

        layout = re.search(
            r"function layout\([\s\S]*?(?=  function svgElement\()",
            script,
        )
        self.assertIsNotNone(layout)
        layout_source = layout.group(0)
        self.assertIn("queueIndex", layout_source)
        self.assertNotIn("queue.shift(", layout_source)
        self.assertIn("var ringIndex=0,ringStart=0,ring=42;", layout_source)
        self.assertIn(
            "while(childIndex-ringStart>=ringCapacity)", layout_source
        )
        self.assertIn("ring=42+ringIndex*30", layout_source)

    def test_architecture_fallback_starts_hidden_behind_the_graph(self) -> None:
        fallback = re.search(
            r'<details\b[^>]*id="arch-fallback"[^>]*>', self.architecture_html
        )
        shell = re.search(
            r'<div\b[^>]*id="arch-graph-shell"[^>]*>', self.architecture_html
        )
        legend = re.search(
            r'<div\b[^>]*id="arch-legend"[^>]*>', self.architecture_html
        )
        camera = re.search(
            r'<div\b[^>]*id="arch-camera-controls"[^>]*>', self.architecture_html
        )
        self.assertIsNotNone(fallback)
        self.assertIn("hidden", fallback.group(0))
        for visible in (shell, legend, camera):
            self.assertIsNotNone(visible)
            self.assertNotIn("hidden", visible.group(0))

    def test_architecture_focus_defaults_complete_without_false_time_travel(self) -> None:
        self.assertIn(
            'data-arch-view="complete" aria-pressed="true">Current and planned',
            self.architecture_html,
        )
        self.assertIn(
            'data-arch-view="changes" aria-pressed="false">Changes',
            self.architecture_html,
        )
        self.assertNotIn("data-arch-time", self.architecture_html)

    def test_architecture_inspector_uses_plain_decision_language(self) -> None:
        for label in (
            "What is this?",
            "What is changing?",
            "Planned action",
            "Observed by Git",
            "Plan source",
            "Plan authority",
            "Why are we changing it?",
            "Why not extend existing code?",
            "Design reference",
            "Design digest",
            "Design authority",
            "Proposal approval",
            "Does this match the approved design?",
            "What does it use?",
            "What uses it?",
            "What could break?",
            "Can it be safely undone?",
            "How will we check it?",
        ):
            with self.subTest(label=label):
                self.assertIn(label, self.architecture_html)
        self.assertNotIn("Design source", self.architecture_html)

    def test_architecture_detail_is_capped_by_involvement(self) -> None:
        hands_off = architecture_fixture_html("hands-off")
        module = architecture_fixture_html("module")
        file_level = architecture_fixture_html("file")
        function = architecture_fixture_html("function")

        self.assertNotIn('id="architecture-map"', hands_off)
        self.assertIn('data-max-depth="module"', module)
        self.assertIn(
            'data-arch-depth="module" aria-pressed="true">Modules', module
        )
        self.assertIn(
            'data-arch-depth="file" aria-pressed="false" disabled>Files', module
        )
        self.assertIn(
            'data-arch-depth="function" aria-pressed="false" disabled>Functions',
            module,
        )
        self.assertIn('data-max-depth="file"', file_level)
        self.assertIn(
            'data-arch-depth="file" aria-pressed="true">Files', file_level
        )
        self.assertIn(
            'data-arch-depth="function" aria-pressed="false" disabled>Functions',
            file_level,
        )
        self.assertIn('data-max-depth="function"', function)
        self.assertIn(
            'data-arch-depth="function" aria-pressed="true">Functions', function
        )
        self.assertNotIn(
            'data-arch-depth="function" aria-pressed="true" disabled', function
        )

    def test_banner_element_exists_and_starts_empty(self) -> None:
        self.assertIn('<div id="retro-banner"></div>', self.html)

    def test_banner_has_no_static_text(self) -> None:
        """It must degrade to nothing, so it may not ship any prose of its own."""
        idx = self.html.index('<div id="retro-banner"></div>')
        self.assertNotIn("retrospective is due", self.html[idx:idx + 400])

    def test_plan_reuses_the_truthful_cockpit_banner(self) -> None:
        self.assertIn('<div id="board-banner"></div>', self.html)
        self.assertIn("exact review URL", self.html)

    def test_plan_leads_with_decisions_and_collapses_the_record(self) -> None:
        summary = self.html.index('aria-label="Plan summary"')
        decisions = self.html.index("Needs your decision")
        record = self.html.index("Review full plan and project record")
        self.assertLess(summary, decisions)
        self.assertLess(decisions, record)
        architecture = self.architecture_html.index('id="architecture-map"')
        architecture_record = self.architecture_html.index(
            "Review full plan and project record"
        )
        self.assertLess(
            self.architecture_html.index("Needs your decision"), architecture
        )
        self.assertLess(architecture, architecture_record)
        self.assertIn('<details class="record"><summary>', self.html)
        self.assertNotIn('<details class="record" open', self.html)
        self.assertIn('id="plan-live-state"', self.html)
        self.assertIn("if(!B || !sentinel) return", self.html)
        self.assertIn("plan or bound design changed", self.html)

    def test_plan_does_not_offer_unsaved_question_controls(self) -> None:
        self.assertNotIn('type="radio"', self.html)
        page = plan_html.render(
            {"questions": [{"id": "q1", "question": "Choose?", "options": ["A", "B"]}]},
            {}, [], "", set(), [], [], [], "",
        )
        self.assertNotIn('type="radio"', page)
        self.assertIn("This page never pretends a local click was saved", page)

    def test_repository_observation_is_not_presented_as_verification(self) -> None:
        self.assertIn("Repository change scope is context", self.html)
        self.assertIn("Latest verification", self.html)

    def test_real_browser_fixture_renders_current_plan_protocol(self) -> None:
        with mock.patch.object(
            browser_check.shutil,
            "copyfile",
            side_effect=AssertionError("browser fixture copied stale generated output"),
        ):
            with browser_check.browser_fixture() as fixture_root:
                plan = (fixture_root / "plan.html").read_text(encoding="utf-8")

        self.assertIn(json.dumps(board_client.protocol_version()), plan)

    def test_agent_provisional_disclosure_is_visible_before_the_full_record(self) -> None:
        page = plan_html.render(
            {}, {"status": "draft"}, [], "", set(), [], [], [], "",
            cockpit_state={
                "status": "draft",
                "approval_required": True,
                "approval_available": False,
                "authority_trust": (
                    "Receipt binding is tamper-evident, not cryptographic person authentication."
                ),
                "design_authority": {
                    "authority": "agent-provisional",
                    "authored_by": "agent",
                    "confidence": "very-high",
                    "disclosures": [{
                        "section": "docs/design/experience.md",
                        "quick_read": (
                            "- Player does: accepts one focused action.\n"
                            "- Successful outcome: understands its consequence."
                        ),
                        "why_inference": "The requested outcome requires legible acknowledgement.",
                        "assumptions": "The feedback remains local and removable.",
                        "veto_and_go_no_go": (
                            "- Veto scope: remove the isolated feedback.\n"
                            "- Next go/no-go: before content depends on it."
                        ),
                    }],
                },
                "verification": {"status": "not-run"},
            },
        )

        section = page.index("docs/design/experience.md")
        quick_read = page.index("Player does: accepts one focused action")
        inference = page.index("Why this inference")
        assumptions = page.index("Assumptions")
        veto = page.index("Veto and go/no-go")
        record = page.index("Review full plan and project record")
        self.assertLess(section, record)
        self.assertLess(quick_read, record)
        self.assertLess(inference, record)
        self.assertLess(assumptions, record)
        self.assertLess(veto, record)
        self.assertIn("not cryptographic person authentication", page)

    def test_only_explicitly_related_questions_are_promoted(self) -> None:
        questions = [
            {
                "id": "slice-related",
                "question": "Question for this slice?",
                "blocks": "The active interaction.",
                "related_slices": ["active-slice"],
            },
            {
                "id": "design-related",
                "question": "Question for this design?",
                "blocks": "The cited player outcome.",
                "related_design_refs": ["docs/design/active.md"],
            },
            {
                "id": "other-work",
                "question": "Question for other work?",
                "blocks": "Another slice.",
                "related_slices": ["other-slice"],
            },
            {
                "id": "legacy-question",
                "question": "Legacy question without a relation?",
                "blocks": "Older work.",
            },
        ]
        proposal = {
            "slice": "active-slice",
            "status": "recorded",
            "design_refs": [{"section": "docs/design/active.md"}],
        }
        page = plan_html.render(
            {"questions": questions}, proposal, [], "", set(), [], [], [], "",
            changed=set(),
            cockpit_state={
                "status": "recorded",
                "approval_required": False,
                "verification": {"status": "not-run"},
            },
        )
        front, record = page.split("Review full plan and project record", 1)

        self.assertIn("Question for this slice?", front)
        self.assertIn("Question for this design?", front)
        self.assertNotIn("Question for other work?", front)
        self.assertNotIn("Legacy question without a relation?", front)
        self.assertIn("Question for other work?", record)
        self.assertIn("Legacy question without a relation?", record)
        self.assertIn("Legacy/unscoped question", record)
        self.assertIn("2 decisions needed", front)

    def test_approved_boundary_clears_approve_action_but_can_be_reconsidered(self) -> None:
        page = plan_html.render(
            {},
            {"status": "approved", "experience": {}},
            [], "", set(), [], [], [], "",
            changed=set(),
            cockpit_state={
                "status": "approved",
                "approval_required": False,
                "fingerprint": "f" * 64,
                "authority_trust": (
                    "The local receipt is tamper-evident but does not "
                    "cryptographically authenticate a person."
                ),
                "verification": {"status": "not-run"},
            },
        )
        self.assertIn("No decision is waiting", page)
        self.assertNotIn(">Approve design and plan</button>", page)
        self.assertIn("Reconsider this approval", page)
        self.assertIn(">Request changes</button>", page)
        self.assertIn(">Veto design and plan</button>", page)
        self.assertIn("withdraws only this exact plan approval", page)
        self.assertIn("independently of later baseline, envelope or plan edits", page)
        self.assertIn("does not cryptographically authenticate a person", page)

    def test_recorded_reversible_work_exposes_pause_and_veto_controls(self) -> None:
        page = plan_html.render(
            {"name": "Recorded fixture", "involvement": "hands-off"},
            {"status": "recorded", "experience": {}},
            [], "", set(), [], [], [], "",
            changed=set(),
            cockpit_state={
                "status": "recorded",
                "approval_required": False,
                "recorded_decision_available": True,
                "fingerprint": "f" * 64,
                "verification": {"status": "not-run"},
            },
        )

        self.assertIn("No decision is waiting", page)
        self.assertNotIn(">Approve design and plan</button>", page)
        self.assertIn("Pause or veto recorded autonomous work", page)
        self.assertIn('id="plan-recorded-controls"', page)
        self.assertIn(">Request changes</button>", page)
        self.assertIn(">Veto design and plan</button>", page)
        self.assertIn("returning this exact recorded plan to draft", page)
        self.assertIn("design-authority state unchanged", page)
        self.assertIn("survives later baseline, envelope and plan-only edits", page)
        self.assertIn("Both actions require a reason; neither dispatches work", page)
        self.assertIn("document.getElementById('plan-recorded-controls')", page)

        snapshot = plan_html.render(
            {"name": "Recorded fixture", "involvement": "hands-off"},
            {"status": "recorded", "experience": {}},
            [], "", set(), [], [], [], "immutable-slice",
            changed=set(),
            cockpit_state={
                "status": "recorded",
                "approval_required": False,
                "recorded_decision_available": True,
                "fingerprint": "f" * 64,
                "verification": {"status": "not-run"},
            },
        )
        self.assertNotIn('id="plan-recorded-controls"', snapshot)

    def test_plan_only_calls_baseline_diff_paths_unapproved_changes(self) -> None:
        without_baseline = plan_html.render(
            {}, {"status": "approved"}, [], "", {"existing.gd"}, [], [], [], "",
        )
        self.assertNotIn("unapproved change", without_baseline)
        self.assertIn("Change scope is unavailable", without_baseline)
        self.assertIn("Repository change scope could not be verified", without_baseline)
        self.assertNotIn("No decision is waiting", without_baseline)

        with_baseline = plan_html.render(
            {}, {"status": "approved", "baseline_sha": "a" * 40},
            [], "", set(), [], [], [], "",
            changed={"removed.gd"}, deleted={"removed.gd"},
        )
        self.assertIn("1 unapproved change", with_baseline)
        self.assertIn("removed.gd</code> (deleted)", with_baseline)

    def test_recorded_out_of_scope_change_is_a_waiting_decision(self) -> None:
        page = plan_html.render(
            {"involvement": "hands-off"},
            {
                "status": "recorded",
                "baseline_sha": "a" * 40,
                "scope": [
                    {
                        "kind": "directory",
                        "path": "scripts/logic",
                        "action": "modify",
                        "why": "The reversible implementation belongs here.",
                    }
                ],
            },
            [], "", set(), [], [], [], "",
            changed={"tests/unit/test_unplanned.gd"},
            cockpit_state={
                "status": "recorded",
                "approval_required": False,
                "verification": {"status": "not-run"},
            },
        )

        self.assertIn("1 unapproved change", page)
        self.assertIn("differs from the recorded plan", page)
        self.assertNotIn("No decision is waiting", page)

    def test_hands_off_root_scope_honours_exact_action(self) -> None:
        proposal = {
            "status": "recorded",
            "baseline_sha": "a" * 40,
            "scope": [{
                "kind": "directory",
                "path": "(root)",
                "action": "new",
                "why": "The reversible slice may add authored game files.",
            }],
        }
        state = {
            "status": "recorded",
            "approval_required": False,
            "verification": {"status": "not-run"},
        }

        matching = plan_html.render(
            {"involvement": "hands-off"}, proposal,
            [], "", {"scripts/new_logic.gd"}, [], [], [], "",
            changed={"scripts/new_logic.gd"}, cockpit_state=state,
            changed_actions={"scripts/new_logic.gd": "new"},
        )
        wrong_action = plan_html.render(
            {"involvement": "hands-off"}, proposal,
            [], "", {"scripts/new_logic.gd"}, [], [], [], "",
            changed={"scripts/new_logic.gd"}, cockpit_state=state,
            changed_actions={"scripts/new_logic.gd": "modify"},
        )

        self.assertNotIn("unapproved change", matching)
        self.assertIn("1 unapproved change", wrong_action)

    def test_hands_off_scope_uses_the_most_specific_action_owner(self) -> None:
        page = plan_html.render(
            {"involvement": "hands-off"},
            {
                "status": "recorded",
                "baseline_sha": "a" * 40,
                "scope": [
                    {
                        "kind": "directory",
                        "path": "(root)",
                        "action": "new",
                        "why": "New files are reversible anywhere in the game root.",
                    },
                    {
                        "kind": "directory",
                        "path": "scripts/logic",
                        "action": "modify",
                        "why": "This existing module may only be modified.",
                    },
                ],
            },
            [], "", {"scripts/logic/new_file.gd"}, [], [], [], "",
            changed={"scripts/logic/new_file.gd"},
            cockpit_state={
                "status": "recorded",
                "approval_required": False,
                "verification": {"status": "not-run"},
            },
            changed_actions={"scripts/logic/new_file.gd": "new"},
        )

        self.assertIn("1 unapproved change", page)

    def test_recent_change_paths_are_inert_in_the_bounded_cockpit(self) -> None:
        path = "scripts/logic/recent_target.gd"
        page = plan_html.render(
            {}, {}, [], "", set(),
            [{"sha": "abc123", "date": "2026-08-28", "subject": "Change", "files": [path]}],
            [], [], "",
        )

        self.assertIn(f"<code>{path}</code>", page)
        self.assertNotIn(f'href="{path}"', page)
        self.assertIn("bounded cockpit does not serve source files", page)

    def test_recorded_stale_copy_names_the_blocker_without_migration_advice(self) -> None:
        page = plan_html.render(
            {"involvement": "hands-off"}, {"status": "recorded"},
            [], "", set(), [], [], [], "",
            cockpit_state={
                "status": "recorded-stale",
                "approval_required": True,
                "approval_available": False,
                "approval_blocker": "The bound design digest is stale.",
                "verification": {"status": "not-run"},
            },
        )

        self.assertIn("The bound design digest is stale", page)
        self.assertIn("Resolve the named blocker", page)
        self.assertNotIn("until this plan is migrated", page)

    def test_invalid_source_preserves_the_last_valid_plan(self) -> None:
        stderr = io.StringIO()
        with mock.patch.object(
                plan_html, "artefact_errors",
                return_value=[
                    ("proposal.json", "status must be draft or approved")
                ]
        ), mock.patch.object(Path, "write_text") as write_text, \
                mock.patch.object(sys, "argv", ["plan_html.py"]), \
                contextlib.redirect_stderr(stderr):
            code = plan_html.main()

        self.assertEqual(1, code)
        write_text.assert_not_called()
        self.assertIn("existing output was preserved", stderr.getvalue())
        self.assertIn("kit schema describe proposal", stderr.getvalue())


class Harness(unittest.TestCase):
    """Run the page's own JavaScript. Skips without node -- not a dependency."""

    @staticmethod
    def _retro_fixture() -> Path:
        """Render one actionable card independent of repository retro history."""
        def finding(title: str, cost: float) -> dict:
            return {
                "title": title,
                "sessions": ["S1"],
                "human_turns": [],
                "mechanical": [],
                "recurs": True,
                "severity": "none",
                "fix_files": ["tools/example.py"],
                "fix_lines": 8,
                "cost": cost,
                "effort": 1,
                "notes": [],
                "body": (
                    "**Problem** - The action contract needs a fixture.\n\n"
                    "**Proposal** - Render one actionable finding.\n\n"
                    "**Measure** - Every browser state is exercised."
                ),
                "session_snapshot": "",
                "_digests": {},
            }

        findings = [
            finding("Frontend harness finding", 8.0),
            finding("Not rendered on this page", 4.0),
        ]
        rows = [
            {"finding": item,
             "title": retro_html.retro_rank.normalise_title(item["title"]),
             "cost": item["cost"], "state": "", "updated_at": ""}
            for item in findings
        ]
        data = {
            "findings": findings,
            "digests": {},
            "accepted": {},
            "deferred": {},
            "accepted_entries": [],
            "rows": {"toaction": rows, "approved": [], "deferred": []},
        }
        artifacts = {
            retro_html.retro_rank.normalise_title(item["title"]): {
                "slug": retro_html.retro_rank.normalise_title(item["title"]),
                "generated_at": "2026-01-01T00:00:00Z",
                "prompt": (
                    "Implement the approved frontend contract.\n" + "evidence " * 30
                ),
            }
            for item in findings
        }
        with mock.patch.object(retro_html, "collect", return_value=data), \
                mock.patch.object(retro_html, "mark_resolved_if_absent", return_value=False), \
                mock.patch.object(
                    board_client,
                    "last_known_board_url",
                    return_value="http://127.0.0.1:54321/",
                ), \
                mock.patch.object(
                    retro_html.retro_queue, "load_by_title",
                    side_effect=lambda title: artifacts.get(
                        retro_html.retro_rank.normalise_title(title)
                    ),
                ), \
                mock.patch.object(retro_html.retro_queue, "is_stale", return_value=False):
            html = retro_html.render([])
        target = ROOT / ".checklogs" / "tests" / "retro-harness.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding="utf-8")
        return target

    @staticmethod
    def _plan_fixture() -> Path:
        """Render a private plan with a complete architecture map."""
        target = ROOT / ".checklogs" / "tests" / "plan-harness.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(
            board_client,
            "last_known_board_url",
            return_value="http://127.0.0.1:54321/",
        ):
            target.write_text(architecture_fixture_html(), encoding="utf-8")
        return target

    @staticmethod
    def _unchanged_plan_fixture() -> Path:
        """Render a complete architecture whose Changes focus has no nodes."""
        target = ROOT / ".checklogs" / "tests" / "plan-unchanged-harness.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(
            board_client,
            "last_known_board_url",
            return_value="http://127.0.0.1:54321/",
        ):
            target.write_text(
                unchanged_architecture_fixture_html(), encoding="utf-8"
            )
        return target

    @staticmethod
    def _recorded_plan_fixture() -> Path:
        """Render deterministic recorded controls for browser-level action proof."""
        target = ROOT / ".checklogs" / "tests" / "plan-recorded-harness.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(
            board_client,
            "last_known_board_url",
            return_value="http://127.0.0.1:54321/",
        ):
            html = plan_html.render(
                {"name": "Recorded fixture", "involvement": "hands-off"},
                {
                    "slice": "a reversible response",
                    "status": "recorded",
                    "experience": {"player_does": "Chooses one action."},
                },
                [], "", set(), [], [], [], "",
                cockpit_state={
                    "status": "recorded",
                    "approval_required": False,
                    "recorded_decision_available": True,
                    "fingerprint": "f" * 64,
                    "verification": {"status": "not-run"},
                },
            )
        target.write_text(html, encoding="utf-8")
        return target

    def _run(self, page: str) -> dict:
        node = shutil.which("node")
        if not node:
            self.skipTest("node not on PATH; DOM harness skipped")
        if page == "retro.html":
            target = self._retro_fixture()
        elif page == "plan-recorded.html":
            target = self._recorded_plan_fixture()
        elif page == "plan-unchanged.html":
            target = self._unchanged_plan_fixture()
        else:
            target = self._plan_fixture()
        proc = subprocess.run([node, str(HARNESS), str(target)], cwd=ROOT,
                              capture_output=True, text=True)
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            self.fail(f"harness produced no report:\n{proc.stdout}\n{proc.stderr}")
        failures = [r for r in data["results"] if not r["pass"]]
        self.assertFalse(
            failures,
            "\n".join(f"{r['scenario']}: {r['name']} ({r['detail'][:200]})" for r in failures),
        )
        return data

    def test_retro_html_behaves_in_every_state(self) -> None:
        data = self._run("retro.html")
        scenarios = {r["scenario"] for r in data["results"]}
        self.assertEqual(
            scenarios,
            {"healthy", "ordering", "approve-500", "approve-non-json", "prompt-500",
             "board-down", "file-readonly"},
        )

    def test_plan_html_behaves_in_every_state(self) -> None:
        self._run("plan.html")

    def test_unchanged_changes_focus_is_an_explicit_empty_state(self) -> None:
        data = self._run("plan-unchanged.html")
        scenarios = {result["scenario"] for result in data["results"]}
        self.assertIn("architecture-no-changes", scenarios)

    def test_recorded_plan_controls_post_exact_reasoned_actions(self) -> None:
        data = self._run("plan-recorded.html")
        scenarios = {r["scenario"] for r in data["results"]}
        self.assertIn("recorded-plan-decision", scenarios)


if __name__ == "__main__":
    unittest.main()
