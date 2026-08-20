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

import json
import random
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
HARNESS = ROOT / "tools" / "tests" / "dom_harness.js"
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import page_parts  # noqa: E402
import plan_html  # noqa: E402
import retro_html  # noqa: E402


def generated(name: str, build) -> str:
    path = ROOT / name
    if not path.exists():
        build()
    return path.read_text(encoding="utf-8")


class RetroPageBytes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = generated("retro.html", lambda: retro_html.main())

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

    def test_approving_says_it_spends_quota(self) -> None:
        self.assertIn("spends quota", self.html)
        self.assertIn("no second confirmation", self.html)

    def test_stalled_state_has_its_own_visual(self) -> None:
        self.assertIn("stall-pulse", self.html)
        self.assertIn(".status-slot.stalled", self.html)

    def test_dead_endpoints_are_gone(self) -> None:
        for path in ("/api/dispatch/prepare", "/api/dispatch/run", "/api/decision"):
            self.assertNotIn(path, self.html, f"{path} was removed by the contract")

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
    ]

    def test_to_action_is_cost_descending_then_unranked(self) -> None:
        got = [r["title"] for r in retro_html.order_rows("toaction", self.ROWS)]
        self.assertEqual(got, ["c", "d", "f", "a", "g", "b", "e"])

    def test_approved_puts_live_work_first_then_queued_done_failed(self) -> None:
        got = [r["title"] for r in retro_html.order_rows("approved", self.ROWS)]
        # working/stalled (b, f -- newest first), queued (d), done (e),
        # failed (c), then anything with no state at all (a, g).
        self.assertEqual(got, ["f", "b", "d", "e", "c", "a", "g"])

    def test_deferred_is_most_recent_first(self) -> None:
        got = [r["title"] for r in retro_html.order_rows("deferred", self.ROWS)]
        self.assertEqual(got, ["c", "e", "f", "d", "b", "a", "g"])

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
        cls.html = generated("retro.html", lambda: retro_html.main())

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
        unranked = [c for c in page_parts.cards(self.html, "list-toaction")
                    if c.get("data-cost", "") == ""]
        if not unranked:
            self.skipTest("no unranked finding in the current evidence")
        self.assertIn("unranked-note", self.html)
        self.assertIn("carrying a number it has not earned", self.html)

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

    def test_detail_is_collapsed_so_the_controls_are_reachable(self) -> None:
        """Full detail is kept, just not between the human and the decision."""
        self.assertIn('<details class="detail-block">', self.html)
        for card in page_parts.cards(self.html, "list-toaction"):
            slug = card.get("data-slug", "")
            if not slug:
                continue
            start = self.html.index('data-slug="%s"' % slug)
            end = self.html.index("prompt-block", start)
            self.assertLess(self.html.count("<p>", start, end), 40)


class PlanPageBytes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = generated("plan.html", lambda: plan_html.main())

    def test_banner_element_exists_and_starts_empty(self) -> None:
        self.assertIn('<div id="retro-banner"></div>', self.html)

    def test_banner_has_no_static_text(self) -> None:
        """It must degrade to nothing, so it may not ship any prose of its own."""
        idx = self.html.index('<div id="retro-banner"></div>')
        self.assertNotIn("retrospective is due", self.html[idx:idx + 400])

    def test_plan_has_no_alarming_board_banner(self) -> None:
        """plan.html carries no #board-banner: a dead board must not make the
        plan look broken. It is retro.html that has something to say about it."""
        self.assertNotIn('id="board-banner"', self.html)


class Harness(unittest.TestCase):
    """Run the page's own JavaScript. Skips without node -- not a dependency."""

    def _run(self, page: str) -> dict:
        node = shutil.which("node")
        if not node:
            self.skipTest("node not on PATH; DOM harness skipped")
        if not (ROOT / page).exists():
            self.skipTest(f"{page} not generated")
        proc = subprocess.run([node, str(HARNESS), page], cwd=ROOT,
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


if __name__ == "__main__":
    unittest.main()
