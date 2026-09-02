#!/usr/bin/env python3
"""Focused contract tests for the install/upgrade review renderer."""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import kit_change_html  # noqa: E402


DIGEST = "a" * 64
RESULT_DIGEST = "b" * 64


def preview(**changes: object) -> dict:
    value: dict = {
        "mode": "install",
        "status": "needs_decision",
        "project": {"name": "Brownfield Game", "path": r"C:\Games\Brownfield"},
        "current_version": "",
        "incoming_version": "0.3.0",
        "session_id": "c" * 64,
        "plan_sha256": DIGEST,
        "counts": {
            "kit_files": 81,
            "shared_files": 2,
            "removed_files": 0,
            "game_files": 0,
        },
        "design": "Missing — agents cannot build",
        "existing_gaps": 24,
        "recovery": "Previous state will be saved",
        "decisions": [{
            "id": "D1",
            "question": "How should the kit connect agent instructions?",
            "selected": "",
            "choices": [{
                "value": "keep-and-link",
                "label": "Keep them and add the kit link",
                "description": "The existing instructions stay in place.",
                "recommended": True,
            }, {
                "value": "keep",
                "label": "Keep them unchanged",
                "description": "The kit cannot guide both agents.",
            }],
        }],
        "files": [{
            "path": "AGENTS.md",
            "action": "Add kit section",
            "reason": "Connect the shared kit rules.",
        }],
        "review_url": "http://127.0.0.1:54321/kit-change.html?session=" + "c" * 64,
        "plan_url": "http://127.0.0.1:54322/plan.html",
    }
    value.update(changes)
    return value


class ReviewRenderer(unittest.TestCase):
    def render(self, **changes: object) -> str:
        return kit_change_html.render(preview(**changes))

    def test_plain_read_path_and_exact_progress_order(self) -> None:
        page = self.render()
        self.assertIn("Add kit to this project", page)
        positions = [page.index(f'data-step="{step}"') for step in (
            "scan", "review", "apply", "check"
        )]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(1, page.count('aria-current="step"'))
        self.assertIn('data-step="review" aria-current="step"', page)
        self.assertLess(page.index("Quick read"), page.index("Needs your decision"))
        self.assertLess(page.index("Needs your decision"), page.index("What will change"))
        self.assertLess(page.index("What will change"), page.index("Check result"))

    def test_install_is_the_only_first_use_term(self) -> None:
        page = self.render()
        self.assertIn("Not installed → 0.3.0", page)
        self.assertIn(">Add kit</button>", page)

    def test_upgrade_has_the_matching_title_and_action(self) -> None:
        page = self.render(
            mode="upgrade",
            status="ready",
            current_version="0.2.0",
            decisions=[],
            result_sha256=RESULT_DIGEST,
        )
        self.assertIn("Upgrade this kit", page)
        self.assertIn("0.2.0 → 0.3.0", page)
        self.assertIn(">Upgrade kit</button>", page)

    def test_adoption_is_not_presented_as_full_completion(self) -> None:
        page = self.render(
            status="adoption_required",
            decisions=[],
            existing_gaps={"count": 6, "status": "checked"},
            result_sha256=RESULT_DIGEST,
        )
        self.assertIn("Kit works; project cleanup remains", page)
        self.assertIn('<dt>Existing problems</dt><dd id="kit-change-gap-count">6</dd>', page)
        self.assertIn(">Cleanup remains</dd>", page)
        self.assertIn("Restore previous state", page)

    def test_game_folder_decision_is_actionable_without_hiding_other_blockers(self) -> None:
        game_root = self.render(
            status="blocked",
            blockers=["[game-root-ambiguous] Choose the game folder."],
        )
        self.assertIn('data-kit-change-status="needs_decision"', game_root)
        self.assertNotIn("Cannot continue", game_root)

        another_blocker = self.render(
            status="blocked",
            blockers=[
                "[game-root-ambiguous] Choose the game folder.",
                "[unsafe-path] A managed path is redirected.",
            ],
        )
        self.assertIn('data-kit-change-status="blocked"', another_blocker)
        self.assertIn("Cannot continue", another_blocker)

    def test_quick_read_uses_plain_exact_labels(self) -> None:
        page = self.render()
        for label in (
            "Project", "Kit version", "Game files", "Recovery", "Design",
            "Existing problems",
        ):
            self.assertIn(f"<dt>{label}</dt>", page)
        self.assertIn('<dd class="safe">No changes</dd>', page)
        self.assertIn("Missing — agents cannot build", page)
        self.assertIn("This change records 24 existing problems", page)
        self.assertIn("It does not hide them or mark them as fixed.", page)

    def test_decisions_use_native_keyboard_order_and_explain_choices(self) -> None:
        page = self.render()
        decision = page.index('<fieldset class="decision"')
        first = page.index('value="keep-and-link"', decision)
        second = page.index('value="keep"', first)
        action = page.index('id="kit-change-apply"')
        self.assertLess(decision, first)
        self.assertLess(first, second)
        self.assertLess(second, action)
        self.assertIn("How should the kit connect agent instructions?", page)
        self.assertIn("The existing instructions stay in place.", page)
        self.assertIn("Recommended", page)
        self.assertIn("data-board-control disabled", page)
        self.assertNotIn("tabindex", page)

    def test_invalid_selected_choice_remains_an_open_decision(self) -> None:
        value = preview()["decisions"][0]
        value = dict(value, selected="not-a-real-choice")
        page = self.render(decisions=[value])
        self.assertIn(">1 decision needed</p>", page)
        self.assertNotIn(' checked>', page)

    def test_file_details_are_collapsed_and_there_is_no_graph(self) -> None:
        page = self.render()
        tag = re.search(r'<details class="file-details"[^>]*>', page)
        self.assertIsNotNone(tag)
        self.assertNotIn(" open", tag.group(0))
        self.assertIn("Show file details", page)
        self.assertIn("AGENTS.md", page)
        self.assertNotIn("architecture-map", page)
        self.assertNotIn("<svg", page)

    def test_exact_digest_is_visible_and_bound_to_apply(self) -> None:
        page = self.render()
        self.assertGreaterEqual(page.count(DIGEST), 2)
        self.assertIn('id="kit-change-plan-sha"', page)
        self.assertIn("plan_sha256:STATIC.plan_sha256", page)
        self.assertIn('Board.request("/api/kit-change/apply"', page)
        self.assertIn('Board.request("/api/kit-change/restore"', page)

    def test_invalid_digest_and_game_file_changes_fail_closed(self) -> None:
        invalid = self.render(plan_sha256="short", status="ready")
        self.assertIn('data-kit-change-status="blocked"', invalid)
        self.assertIn("The exact change fingerprint is missing.", invalid)
        changed = self.render(
            status="ready",
            decisions=[],
            counts={"kit_files": 1, "shared_files": 0, "removed_files": 0,
                    "game_files": 1},
        )
        self.assertIn('data-kit-change-status="blocked"', changed)
        self.assertIn("This kit change includes game files.", changed)

    def test_file_and_no_javascript_guidance_are_complete(self) -> None:
        page = self.render()
        self.assertIn("Review only — the cockpit is not connected", page)
        review_url = "http://127.0.0.1:54321/kit-change.html?session=" + "c" * 64
        self.assertIn(f'<a href="{review_url}">{review_url}</a>', page)
        self.assertIn("Review only — JavaScript is unavailable", page)
        self.assertIn("A decision is still needed. Tell your agent your choice first.", page)
        self.assertIn("ask your agent to reopen this kit review", page)
        self.assertIn("Retry now", page)

    def test_no_javascript_ready_review_routes_approval_to_the_agent(self) -> None:
        page = self.render(status="ready", decisions=[])
        self.assertIn(
            "Tell Codex or Copilot that you approve this exact kit change.", page
        )
        self.assertIn("without asking you to run a command", page)

    def test_board_hooks_are_shared_and_controls_start_safe(self) -> None:
        page = self.render()
        self.assertIn("window.Board", page)
        self.assertIn("Board.onState", page)
        self.assertIn("Board.guard", page)
        self.assertIn('id="kit-change-controls" class="interactive-actions section" disabled', page)
        self.assertIn("state.kit_change", page)
        fetch_calls = list(re.finditer(r"\bfetch\(", page))
        self.assertEqual(1, len(fetch_calls), "only the shared Board client may fetch")
        self.assertIn("unresolvedDecisionCount()", page)
        self.assertIn("statusLabel.textContent = unresolved ?", page)
        self.assertIn('status === "complete" || status === "adoption_required"', page)
        self.assertIn("safePlanUrl(nextPlanUrl)", page)
        self.assertIn("session_id:sessionId", page)
        self.assertIn('String(next.session_id || "") === sessionId', page)
        self.assertIn("gapCount.textContent", page)

    def test_all_statuses_have_one_current_step_and_plain_label(self) -> None:
        fixtures = {
            "scanning": "Scanning",
            "ready": "Ready",
            "needs_decision": "1 decision needed",
            "blocked": "Blocked",
            "applying": "Applying",
            "checking": "Checking",
            "complete": "Complete",
            "adoption_required": "Kit works; project cleanup remains",
            "restored": "Previous state restored",
            "failed": "Could not finish",
        }
        for status, label in fixtures.items():
            with self.subTest(status=status):
                page = self.render(status=status)
                self.assertIn(f">{label}</p>", page)
                self.assertEqual(1, page.count('aria-current="step"'))

    def test_untrusted_values_are_escaped_and_not_executable(self) -> None:
        page = self.render(
            project={"name": '<img src=x onerror="bad()">', "path": "</code><script>bad()</script>"},
            decisions=[{
                "id": 'D1"><script>bad()</script>',
                "question": "<b>unsafe</b>",
                "choices": [{"value": '"><script>bad()</script>', "label": "<bad>"}],
            }],
            files=[{"path": "</code><script>bad()</script>", "action": "<add>"}],
        )
        self.assertNotIn("<img src=x", page)
        self.assertNotIn("<script>bad()</script>", page)
        self.assertIn("&lt;img src=x onerror=&quot;bad()&quot;&gt;", page)
        self.assertIn("&lt;b&gt;unsafe&lt;/b&gt;", page)

    def test_unsafe_review_and_plan_urls_never_become_links(self) -> None:
        page = self.render(
            review_url="https://example.com/kit-change.html",
            plan_url="javascript:bad()",
        )
        self.assertNotIn('href="https://example.com', page)
        self.assertNotIn("javascript:bad()", page)


if __name__ == "__main__":
    unittest.main()
