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
        architecture = self.html.index("Actual architecture")
        self.assertLess(summary, decisions)
        self.assertLess(decisions, record)
        self.assertLess(record, architecture)
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
        """Generate a private plan page without relying on ignored root output."""
        target = ROOT / ".checklogs" / "tests" / "plan-harness.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        output = io.StringIO()
        with mock.patch.object(plan_html, "OUT", target), \
                mock.patch.object(
                    board_client,
                    "last_known_board_url",
                    return_value="http://127.0.0.1:54321/",
                ), \
                mock.patch.object(sys, "argv", ["plan_html.py"]), \
                contextlib.redirect_stdout(output):
            result = plan_html.main()
        if result != 0 or not target.is_file():
            raise RuntimeError(
                "could not generate the deterministic plan harness fixture: "
                + output.getvalue().strip()
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

    def test_recorded_plan_controls_post_exact_reasoned_actions(self) -> None:
        data = self._run("plan-recorded.html")
        scenarios = {r["scenario"] for r in data["results"]}
        self.assertIn("recorded-plan-decision", scenarios)


if __name__ == "__main__":
    unittest.main()
