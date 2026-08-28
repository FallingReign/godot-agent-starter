#!/usr/bin/env python3
"""Focused contract tests for the bounded plan cockpit."""
from __future__ import annotations

import copy
import json
import os
import shutil
import stat
import sys
import unittest
import uuid
from datetime import date
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS))

import cockpit  # noqa: E402
import design  # noqa: E402
import retro_due  # noqa: E402


def _remove_readonly(function, path: str, _error) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _design_text(
    *,
    title: str = "Player response",
    authority: str = "agent-provisional",
    authored_by: str = "agent",
    confidence: str = "very-high",
    resolution: str = "settled",
) -> str:
    return f"""# {title}

_Resolution: {resolution}_
_Authority: {authority}_
_Authored by: {authored_by}_
_Confidence: {confidence}_

## Quick read

- Player does: Chooses one legible action.
- Player experiences: An immediate, understandable response.
- Successful outcome: The result and its consequence remain clear.

## Why this inference

The requested interaction depends on visible acknowledgement and context.

## Assumptions

The interaction is local and can be vetoed before content migration.

## Veto and go/no-go

- Veto scope: The interaction and its feedback can be removed together.
- Next go/no-go: Approval is required before persistence changes.

## Intent

Every accepted action acknowledges immediately and preserves enough context.

## Constraints it imposes

The player must be able to connect the response to the action they chose.
"""


def _passing_gate_payload(**values) -> dict:
    nonce = "1" * 32
    repository_digest = "2" * 64
    return {
        "ok": True,
        "status": "passed",
        "exit_code": 0,
        "verification_nonce": nonce,
        "native_warning_start": {"state": "clean", "identity": None},
        "repository_start": {"available": True, "digest": repository_digest},
        "repository_end": {"available": True, "digest": repository_digest},
        "repository_stable": True,
        "gate_summary": {
            "schema": 2,
            "run_id": nonce,
            "failed": False,
            "repository_sha256": repository_digest,
            "auth_sha256": "3" * 64,
            "results": [],
            "diagnostics": {},
        },
        **values,
    }


class TestCockpitDecision(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TOOLS.parent / ".checklogs" / f"cockpit-test-{uuid.uuid4().hex}"
        self.design_root = self.root / "docs" / "design"
        self.design_root.mkdir(parents=True)
        (self.root / "kit.config.json").write_text(
            json.dumps(
                {
                    "schema": 1,
                    "game_root": "src",
                    "runtime_root": ".kit/runtime",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.design_path = self.design_root / "experience.md"
        self.design_path.write_text(_design_text(), encoding="utf-8")
        self.shape_path = self.root / "project.shape.json"
        self.shape_path.write_text(
            json.dumps(
                {
                    "name": "Fixture",
                    "pitch": "A decision fixture.",
                    "involvement": "module",
                    "decisions": [],
                    "direction": [],
                    "questions": [],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        self.proposal_path = self.root / "proposal.json"
        self._write_proposal()
        self._saved_design_root = design.DESIGN
        design.DESIGN = self.design_root
        self._baseline_patcher = mock.patch.object(
            cockpit.proposal_authority, "baseline_exists", return_value=True
        )
        self._baseline_patcher.start()

    def tearDown(self) -> None:
        self._baseline_patcher.stop()
        design.DESIGN = self._saved_design_root
        shutil.rmtree(self.root, onerror=_remove_readonly)

    def _proposal(self, refs: list[dict] | None = None) -> dict:
        digest = design.design_sha256(self.design_path.read_text(encoding="utf-8"))
        return {
            "slice": "one legible response",
            "status": "draft",
            "baseline_sha": "a" * 40,
            "experience": {
                "player_does": "Chooses one action.",
                "feels_like": "Immediate and legible.",
                "not_this": "An unexplained background mutation.",
            },
            "mockup": {
                "not_possible": "The decision concerns interaction authority, not a vector-approximable layout."
            },
            "design_refs": refs
            if refs is not None
            else [
                {
                    "section": "docs/design/experience.md",
                    "why": "It defines the response the slice must preserve.",
                    "sha256": digest,
                }
            ],
            "design_authority": {
                "authority": "agent-provisional",
                "authored_by": "agent",
                "confidence": "very-high",
            },
            "reversibility": {
                "state": "go-no-go",
                "veto_scope": "The interaction and feedback.",
                "hard_to_undo": "Persisted player content.",
                "next_go_no_go": "Before persistence changes.",
            },
            "modules": [
                {
                    "path": "scripts/logic/response",
                    "role": "Keep acknowledgement legible.",
                    "why": "The reviewed outcome needs an isolated logic boundary.",
                    "action": "new",
                    "may_depend_on": [],
                    "boundary_data": "Typed action intent enters and a typed response leaves.",
                }
            ],
        }

    def _write_proposal(self, refs: list[dict] | None = None) -> None:
        self.proposal_path.write_text(
            json.dumps(self._proposal(refs), indent=2) + "\n", encoding="utf-8"
        )

    def _fingerprint(self) -> str:
        return cockpit.proposal_fingerprint(self.root)["digest"]

    def _approve(self) -> dict:
        return cockpit.record_plan_decision(
            self.root, action="approve", fingerprint=self._fingerprint()
        )

    def _record_reversible_hands_off(self) -> None:
        shape = json.loads(self.shape_path.read_text(encoding="utf-8"))
        shape["involvement"] = "hands-off"
        self.shape_path.write_text(
            json.dumps(shape, indent=2) + "\n", encoding="utf-8"
        )
        proposal = self._proposal()
        proposal["status"] = "recorded"
        proposal["reversibility"]["state"] = "reversible"
        proposal["scope"] = [
            {
                "kind": "directory",
                "path": "scripts/logic",
                "action": "modify",
                "why": "The reversible interaction logic is contained here.",
            }
        ]
        proposal["modules"] = []
        self.proposal_path.write_text(
            json.dumps(proposal, indent=2) + "\n", encoding="utf-8"
        )

    @unittest.skipUnless(os.name == "nt", "Windows sharing violation behavior")
    def test_atomic_replace_retries_only_bounded_windows_sharing_violation(self) -> None:
        sharing = PermissionError("sharing violation")
        sharing.winerror = 5
        with mock.patch.object(
            cockpit.os, "replace", side_effect=[sharing, None]
        ) as replace, mock.patch.object(cockpit.time, "sleep") as sleep:
            cockpit._replace_file(self.root / "temporary", self.root / "destination")

        self.assertEqual(2, replace.call_count)
        sleep.assert_called_once_with(0.02)

    def test_approve_binds_design_plan_shape_and_ledger_once(self) -> None:
        self.assertTrue(cockpit.plan_view(self.root)["approval_available"])
        result = self._approve()

        proposal = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        shape = json.loads(self.shape_path.read_text(encoding="utf-8"))
        confirmed_text = self.design_path.read_text(encoding="utf-8")
        self.assertEqual(proposal["status"], "approved")
        self.assertEqual(proposal["approved_by"], cockpit.ACTOR_LABEL)
        self.assertEqual(proposal["approval_sha256"], cockpit.approval_sha256(proposal))
        self.assertIn("_Authority: human-confirmed_", confirmed_text)
        self.assertEqual(
            proposal["design_refs"][0]["sha256"], design.design_sha256(confirmed_text)
        )
        decision = shape["decisions"][0]
        self.assertEqual(decision["decided_by"], "human")
        self.assertEqual(decision["authority_action"], "confirm")
        self.assertEqual(decision["design_sha256"], proposal["design_refs"][0]["sha256"])
        self.assertRegex(decision["design_intent_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(decision["proposal_sha256"], proposal["approval_sha256"])
        self.assertEqual("", cockpit.proposal_authority.cockpit_receipt_error(decision))
        receipt = json.loads((self.root / result["record"]).read_text(encoding="utf-8"))
        self.assertEqual(
            decision["cockpit_reviewed_fingerprint"], receipt["reviewed_fingerprint"]
        )
        self.assertEqual(
            decision["cockpit_receipt_sha256"],
            receipt["authority_receipts"][0]["sha256"],
        )
        self.assertFalse(result["dispatched"])
        view = cockpit.plan_view(self.root)
        self.assertFalse(view["approval_required"])
        self.assertEqual(view["go_no_go"]["status"], "clear")

        with self.assertRaises(cockpit.CockpitError) as raised:
            cockpit.record_plan_decision(
                self.root,
                action="approve",
                fingerprint=cockpit.proposal_fingerprint(self.root)["digest"],
            )
        self.assertEqual(raised.exception.code, "already_approved")
        self.assertEqual(len(json.loads(self.shape_path.read_text(encoding="utf-8"))["decisions"]), 1)

    def test_relative_fragment_traversal_and_missing_refs_write_nothing(self) -> None:
        bad_paths = (
            "experience.md",
            "docs/design/experience.md#intent",
            "docs/design/../outside.md",
            "docs/design/missing.md",
        )
        for section in bad_paths:
            with self.subTest(section=section):
                self._write_proposal(
                    [
                        {
                            "section": section,
                            "why": "Invalid fixture.",
                            "sha256": "b" * 64,
                        }
                    ]
                )
                originals = {
                    self.proposal_path: self.proposal_path.read_bytes(),
                    self.shape_path: self.shape_path.read_bytes(),
                    self.design_path: self.design_path.read_bytes(),
                }
                preflight = cockpit.plan_view(self.root)
                self.assertFalse(preflight["approval_available"])
                self.assertTrue(preflight["approval_blocker"])
                with self.assertRaises(cockpit.CockpitError) as raised:
                    self._approve()
                self.assertEqual(raised.exception.code, "design_reference_invalid")
                for path, content in originals.items():
                    self.assertEqual(path.read_bytes(), content)
                self.assertFalse((self.root / ".kit").exists())

    def test_present_private_receipt_mismatch_fails_closed(self) -> None:
        result = self._approve()
        receipt_path = self.root / result["record"]
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["action"] = "veto"
        receipt_path.write_text(
            json.dumps(receipt, indent=2) + "\n", encoding="utf-8"
        )

        view = cockpit.plan_view(self.root)

        self.assertEqual("approval-stale", view["status"])
        self.assertEqual("invalid", view["exact_approval"]["receipt_trust"])
        self.assertTrue(
            any("private cockpit receipt mismatch" in reason for reason in view["exact_approval"]["reasons"])
        )

    def test_missing_private_receipt_is_labeled_portable_policy(self) -> None:
        result = self._approve()
        (self.root / result["record"]).unlink()

        view = cockpit.plan_view(self.root)

        self.assertEqual("approved", view["status"])
        self.assertEqual("portable-policy", view["authority_receipt_trust"])
        self.assertIn("self-consistent policy evidence only", view["authority_trust"])

    def test_malformed_or_ineligible_design_writes_nothing(self) -> None:
        for text in (
            _design_text() + "\n_Authority: agent-provisional_\n",
            _design_text(confidence="high"),
        ):
            with self.subTest(text=text[-40:]):
                self.design_path.write_text(text, encoding="utf-8")
                self._write_proposal()
                originals = {
                    self.proposal_path: self.proposal_path.read_bytes(),
                    self.shape_path: self.shape_path.read_bytes(),
                    self.design_path: self.design_path.read_bytes(),
                }
                with self.assertRaises(cockpit.CockpitError) as raised:
                    self._approve()
                self.assertEqual(raised.exception.code, "design_metadata_invalid")
                for path, content in originals.items():
                    self.assertEqual(path.read_bytes(), content)
                self.assertFalse((self.root / ".kit").exists())

    def test_missing_design_contract_renders_read_only_and_refuses_approval(self) -> None:
        original = self.proposal_path.read_bytes()
        with mock.patch.object(cockpit, "design_contract", None):
            view = cockpit.plan_view(self.root)
            self.assertTrue(view["approval_required"])
            self.assertFalse(view["approval_available"])
            self.assertIn("cannot validate", view["approval_blocker"])
            with self.assertRaises(cockpit.CockpitError) as raised:
                self._approve()
        self.assertEqual(raised.exception.code, "contract_unavailable")
        self.assertEqual(self.proposal_path.read_bytes(), original)
        self.assertFalse((self.root / ".kit").exists())

    def test_recorded_plan_blocks_when_bound_design_changes(self) -> None:
        shape = json.loads(self.shape_path.read_text(encoding="utf-8"))
        shape["involvement"] = "hands-off"
        self.shape_path.write_text(json.dumps(shape, indent=2) + "\n", encoding="utf-8")
        proposal = self._proposal()
        proposal["status"] = "recorded"
        proposal["reversibility"]["state"] = "reversible"
        proposal["scope"] = [
            {
                "kind": "directory",
                "path": "scripts/logic",
                "action": "modify",
                "why": "The reversible interaction logic is contained here.",
            }
        ]
        proposal["modules"] = []
        self.proposal_path.write_text(
            json.dumps(proposal, indent=2) + "\n", encoding="utf-8"
        )

        current = cockpit.plan_view(self.root)
        self.assertEqual("recorded", current["status"])
        self.assertFalse(current["approval_required"])

        self.design_path.write_text(
            _design_text() + "\nThe intended response now has a different constraint.\n",
            encoding="utf-8",
        )
        stale = cockpit.plan_view(self.root)

        self.assertEqual("recorded-stale", stale["status"])
        self.assertTrue(stale["approval_required"])
        self.assertFalse(stale["approval_available"])
        self.assertEqual("invalid", stale["design_authority"]["validation"])
        self.assertIn("changed after the proposal", stale["approval_blocker"])

    def test_recorded_plan_blocks_when_project_shape_is_missing(self) -> None:
        proposal = self._proposal()
        proposal["status"] = "recorded"
        proposal["reversibility"]["state"] = "reversible"
        proposal["scope"] = [
            {
                "kind": "directory",
                "path": "scripts/logic",
                "action": "modify",
                "why": "The reversible interaction logic is contained here.",
            }
        ]
        proposal["modules"] = []
        self.proposal_path.write_text(
            json.dumps(proposal, indent=2) + "\n", encoding="utf-8"
        )
        self.shape_path.unlink()

        view = cockpit.plan_view(self.root)

        self.assertEqual("recorded-stale", view["status"])
        self.assertTrue(view["approval_required"])
        self.assertFalse(view["approval_available"])
        self.assertIn("project involvement is unset or invalid", view["approval_blocker"])

    def test_recorded_reversible_plan_accepts_only_reasoned_request_changes(self) -> None:
        self._record_reversible_hands_off()
        view = cockpit.plan_view(self.root)

        self.assertEqual("recorded", view["status"])
        self.assertFalse(view["approval_required"])
        self.assertTrue(view["recorded_decision_available"])
        with self.assertRaises(cockpit.CockpitError) as raised:
            cockpit.record_plan_decision(
                self.root,
                action="request-changes",
                fingerprint=view["fingerprint"],
                comment="",
            )
        self.assertEqual("reason_required", raised.exception.code)
        self.assertEqual("recorded", cockpit.plan_view(self.root)["status"])

        result = cockpit.record_plan_decision(
            self.root,
            action="request-changes",
            fingerprint=view["fingerprint"],
            comment="The reversible boundary needs a narrower plan.",
        )

        proposal = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        decisions = json.loads(self.shape_path.read_text(encoding="utf-8"))["decisions"]
        self.assertEqual("draft", result["status"])
        self.assertEqual("draft", proposal["status"])
        self.assertEqual("agent-provisional", proposal["design_authority"]["authority"])
        self.assertEqual(1, len(decisions))
        self.assertEqual("plan-only", decisions[0]["authority_scope"])
        self.assertEqual("human", decisions[0]["decided_by"])
        self.assertNotIn("approval", decisions[0]["question"].lower())
        self.assertIn("reviewed plan was withdrawn", decisions[0]["answer"].lower())
        self.assertIn("state is unchanged", decisions[0]["answer"].lower())
        self.assertNotIn("confirmed", decisions[0]["answer"].lower())
        self.assertIn("revised plan", decisions[0]["revisit_if"].lower())
        self.assertTrue(decisions[0]["cockpit_receipt_id"])
        self.assertEqual([], list((self.root / ".kit" / "runtime").glob("*.tmp")))
        self.assertEqual(
            1,
            len(
                list(
                    (
                        self.root
                        / ".kit"
                        / "runtime"
                        / "cockpit"
                        / "plan-decisions"
                    ).glob("*.json")
                )
            ),
        )

    def test_recorded_design_veto_blocks_plan_only_rerecord_until_confirmation(self) -> None:
        self._record_reversible_hands_off()
        current = cockpit.plan_view(self.root)

        result = cockpit.record_plan_decision(
            self.root,
            action="veto",
            fingerprint=current["fingerprint"],
            comment="The inferred outcome is not acceptable for the player.",
        )

        vetoed = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        decisions = json.loads(self.shape_path.read_text(encoding="utf-8"))["decisions"]
        self.assertEqual("draft", result["status"])
        self.assertEqual("design-and-plan", decisions[-1]["authority_scope"])
        self.assertNotIn("approval", decisions[-1]["question"].lower())
        self.assertIn("design intent", decisions[-1]["answer"].lower())
        self.assertIn(
            "later exact cockpit confirmation",
            decisions[-1]["revisit_if"].lower(),
        )
        self.assertEqual("vetoed", cockpit.plan_view(self.root)["design_authority"]["authority"])

        # Baseline, reversibility-envelope and other plan-only edits cannot
        # silently erase a veto of the exact player-experience intent.
        vetoed["status"] = "recorded"
        vetoed["baseline_sha"] = "b" * 40
        vetoed["reversibility"]["hard_to_undo"] = "A revised plan-only boundary."
        self.proposal_path.write_text(
            json.dumps(vetoed, indent=2) + "\n", encoding="utf-8"
        )
        blocked = cockpit.plan_view(self.root)
        self.assertEqual("recorded-stale", blocked["status"])
        self.assertTrue(blocked["approval_required"])
        self.assertFalse(blocked["recorded_decision_available"])
        self.assertIn("human rejection", blocked["approval_blocker"].lower())
        self.assertIn("design-and-plan", blocked["approval_blocker"])
        self.assertIn("explicit cockpit confirmation", blocked["approval_blocker"])

        # Only a later exact cockpit confirmation supersedes this unchanged
        # intent veto; the immutable veto event remains in the authority log.
        vetoed["status"] = "draft"
        self.proposal_path.write_text(
            json.dumps(vetoed, indent=2) + "\n", encoding="utf-8"
        )
        self._approve()
        confirmed = cockpit.plan_view(self.root)
        events = json.loads(self.shape_path.read_text(encoding="utf-8"))["decisions"]
        self.assertEqual("approved", confirmed["status"])
        self.assertEqual("human-confirmed", confirmed["design_authority"]["authority"])
        self.assertEqual(1, len([e for e in events if e.get("authority_action") == "veto"]))
        confirmations = [e for e in events if e.get("authority_action") == "confirm"]
        self.assertEqual(1, len(confirmations))
        self.assertEqual(decisions[-1]["id"], confirmations[0]["supersedes"])

    def test_recorded_design_veto_allows_a_substantively_new_design_intent(self) -> None:
        self._record_reversible_hands_off()
        current = cockpit.plan_view(self.root)
        cockpit.record_plan_decision(
            self.root,
            action="veto",
            fingerprint=current["fingerprint"],
            comment="This player outcome is not acceptable.",
        )

        proposal = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        changed_text = self.design_path.read_text(encoding="utf-8").replace(
            "Every accepted action acknowledges immediately and preserves enough context.",
            "Every accepted action previews its consequence before the player commits.",
        )
        self.design_path.write_text(changed_text, encoding="utf-8")
        proposal["design_refs"][0]["sha256"] = design.design_sha256(changed_text)
        proposal["status"] = "recorded"
        self.proposal_path.write_text(
            json.dumps(proposal, indent=2) + "\n", encoding="utf-8"
        )

        changed = cockpit.plan_view(self.root)
        self.assertEqual("recorded", changed["status"])
        self.assertFalse(changed["approval_required"])
        self.assertTrue(changed["recorded_decision_available"])
        self.assertEqual("agent-provisional", changed["design_authority"]["authority"])
        decisions = json.loads(self.shape_path.read_text(encoding="utf-8"))["decisions"]
        self.assertEqual(1, len([e for e in decisions if e.get("authority_action") == "veto"]))

    def test_mixed_designs_use_conservative_authority_aggregate(self) -> None:
        confirmed = self.design_root / "confirmed.md"
        confirmed.write_text(
            _design_text(
                title="Confirmed outcome",
                authority="human-confirmed",
                authored_by="human",
                confidence="low",
            ),
            encoding="utf-8",
        )
        refs = [
            {
                "section": "docs/design/experience.md",
                "why": "Defines the response.",
                "sha256": design.design_sha256(
                    self.design_path.read_text(encoding="utf-8")
                ),
            },
            {
                "section": "docs/design/confirmed.md",
                "why": "Defines the successful outcome.",
                "sha256": design.design_sha256(confirmed.read_text(encoding="utf-8")),
            },
        ]
        self._write_proposal(refs)
        before = cockpit.plan_view(self.root)["design_authority"]
        self.assertEqual(before["authority"], "agent-provisional")
        self.assertEqual(before["authored_by"], "agent")
        self.assertEqual(before["confidence"], "very-high")

        self._approve()
        proposal = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        self.assertEqual(
            proposal["design_authority"],
            {
                "authority": "human-confirmed",
                "authored_by": "agent",
                "confidence": "low",
            },
        )

    def test_provisional_inference_and_assumptions_are_in_the_decision_view(self) -> None:
        authority = cockpit.plan_view(self.root)["design_authority"]

        self.assertEqual("agent-provisional", authority["authority"])
        self.assertEqual(1, len(authority["disclosures"]))
        disclosure = authority["disclosures"][0]
        self.assertEqual("docs/design/experience.md", disclosure["section"])
        self.assertEqual(
            "- Player does: Chooses one legible action.\n"
            "- Player experiences: An immediate, understandable response.\n"
            "- Successful outcome: The result and its consequence remain clear.",
            disclosure["quick_read"],
        )
        self.assertIn("visible acknowledgement", disclosure["why_inference"])
        self.assertIn("local", disclosure["assumptions"])
        self.assertEqual(
            "- Veto scope: The interaction and its feedback can be removed together.\n"
            "- Next go/no-go: Approval is required before persistence changes.",
            disclosure["veto_and_go_no_go"],
        )

    def test_agent_confirmation_never_satisfies_human_approval(self) -> None:
        proposal = self._proposal()
        _prepared, refs, authority = cockpit._approval_design_changes(self.root, proposal)
        anticipated = copy.deepcopy(proposal)
        anticipated["design_refs"] = refs
        anticipated["design_authority"] = authority
        anticipated["status"] = "approved"
        anticipated["approved_by"] = cockpit.ACTOR_LABEL
        anticipated["approved_on"] = date.today().isoformat()
        anticipated["approval_sha256"] = cockpit.approval_sha256(anticipated)
        shape = json.loads(self.shape_path.read_text(encoding="utf-8"))
        ref = refs[0]
        shape["decisions"].append(
            {
                "id": "agent-confirmation-must-not-count",
                "question": "Confirm design?",
                "answer": "Agent claimed confirmation.",
                "because": "Invalid fixture.",
                "date": date.today().isoformat(),
                "decided_by": "agent",
                "revisit_if": "Always.",
                "docs_at": ref["section"],
                "design_sha256": ref["sha256"],
                "proposal_sha256": anticipated["approval_sha256"],
                "authority_action": "confirm",
            }
        )
        self.shape_path.write_text(json.dumps(shape, indent=2) + "\n", encoding="utf-8")

        before = self.shape_path.read_bytes()
        view = cockpit.plan_view(self.root)
        self.assertFalse(view["approval_available"])
        with self.assertRaises(cockpit.CockpitError) as raised:
            self._approve()
        self.assertEqual(raised.exception.code, "proposal_contract_invalid")
        self.assertEqual(self.shape_path.read_bytes(), before)

    def test_request_changes_supersedes_every_active_confirmation(self) -> None:
        self._approve()
        approved = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        approved_digest = approved["approval_sha256"]
        confirmation = json.loads(self.shape_path.read_text(encoding="utf-8"))["decisions"][0]

        result = cockpit.record_plan_decision(
            self.root,
            action="request-changes",
            fingerprint=self._fingerprint(),
            comment="The next boundary needs a clearer veto scope.",
        )

        proposal = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        decisions = json.loads(self.shape_path.read_text(encoding="utf-8"))["decisions"]
        vetoes = [item for item in decisions if item.get("authority_action") == "veto"]
        self.assertEqual(result["status"], "draft")
        self.assertNotIn("approval_sha256", proposal)
        self.assertEqual(len(vetoes), 1)
        self.assertEqual(vetoes[0]["supersedes"], confirmation["id"])
        self.assertEqual(vetoes[0]["proposal_sha256"], approved_digest)
        self.assertEqual(vetoes[0]["decided_by"], "human")
        self.assertIn("_Authority: human-confirmed_", self.design_path.read_text(encoding="utf-8"))
        self.assertEqual(
            "human-confirmed",
            cockpit.plan_view(self.root)["design_authority"]["authority"],
        )

    def test_design_veto_survives_plan_revision_until_explicit_confirmation(self) -> None:
        self._approve()
        first_confirmation = json.loads(
            self.shape_path.read_text(encoding="utf-8")
        )["decisions"][0]

        cockpit.record_plan_decision(
            self.root,
            action="veto",
            fingerprint=self._fingerprint(),
            comment="The inferred player outcome is not acceptable yet.",
        )

        vetoed = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        vetoed_text = self.design_path.read_text(encoding="utf-8")
        self.assertEqual(vetoed["status"], "draft")
        self.assertEqual(vetoed["design_authority"]["authority"], "agent-provisional")
        self.assertIn("_Authority: agent-provisional_", vetoed_text)
        self.assertEqual(
            vetoed["design_refs"][0]["sha256"], design.design_sha256(vetoed_text)
        )
        self.assertEqual(
            cockpit.plan_view(self.root)["design_authority"]["authority"],
            "vetoed",
        )

        # Plan-only edits cannot erase a veto of the referenced player intent.
        vetoed["baseline_sha"] = "b" * 40
        vetoed["reversibility"]["hard_to_undo"] = "A different plan boundary."
        self.proposal_path.write_text(
            json.dumps(vetoed, indent=2) + "\n", encoding="utf-8"
        )
        revised = cockpit.plan_view(self.root)
        self.assertEqual("vetoed", revised["design_authority"]["authority"])
        self.assertTrue(revised["approval_required"])
        self.assertTrue(revised["approval_available"])

        # The operator may explicitly confirm the exact intent again. The new
        # immutable confirmation supersedes the veto rather than erasing it.
        self._approve()
        reapproved = json.loads(self.proposal_path.read_text(encoding="utf-8"))
        decisions = json.loads(self.shape_path.read_text(encoding="utf-8"))["decisions"]
        self.assertEqual(reapproved["status"], "approved")
        self.assertIn("_Authority: human-confirmed_", self.design_path.read_text(encoding="utf-8"))
        confirmations = [
            item for item in decisions if item.get("authority_action") == "confirm"
        ]
        vetoes = [item for item in decisions if item.get("authority_action") == "veto"]
        self.assertEqual(len(confirmations), 2)
        self.assertEqual(len(vetoes), 1)
        self.assertEqual(vetoes[0]["supersedes"], first_confirmation["id"])
        self.assertEqual(confirmations[-1]["supersedes"], vetoes[0]["id"])
        self.assertEqual(
            "human-confirmed",
            cockpit.plan_view(self.root)["design_authority"]["authority"],
        )

    def test_human_authored_design_veto_is_explicit_in_plan_view(self) -> None:
        self.design_path.write_text(
            _design_text(
                authority="human-confirmed",
                authored_by="human",
                confidence="high",
            ),
            encoding="utf-8",
        )
        proposal = self._proposal()
        proposal["design_authority"] = {
            "authority": "human-confirmed",
            "authored_by": "human",
            "confidence": "high",
        }
        self.proposal_path.write_text(
            json.dumps(proposal, indent=2) + "\n", encoding="utf-8"
        )
        self._approve()

        cockpit.record_plan_decision(
            self.root,
            action="veto",
            fingerprint=self._fingerprint(),
            comment="This player outcome is explicitly vetoed.",
        )

        self.assertIn(
            "_Authority: human-confirmed_",
            self.design_path.read_text(encoding="utf-8"),
        )
        authority = cockpit.plan_view(self.root)["design_authority"]
        self.assertEqual(authority["authority"], "vetoed")
        self.assertEqual(authority["validation"], "vetoed")
        self.assertFalse(authority["implementation_eligible"])

    def test_ledger_failure_and_late_failure_roll_back_every_file(self) -> None:
        original_writer = cockpit._atomic_bytes_write
        for fail_at in ("ledger", "proposal"):
            with self.subTest(fail_at=fail_at):
                self.design_path.write_text(_design_text(), encoding="utf-8")
                self._write_proposal()
                self.shape_path.write_text(
                    json.dumps(
                        {
                            "name": "Fixture",
                            "pitch": "A decision fixture.",
                            "involvement": "module",
                            "decisions": [],
                            "direction": [],
                            "questions": [],
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                originals = {
                    self.proposal_path: self.proposal_path.read_bytes(),
                    self.shape_path: self.shape_path.read_bytes(),
                    self.design_path: self.design_path.read_bytes(),
                }
                failed = False

                def injected(path: Path, content: bytes) -> None:
                    nonlocal failed
                    should_fail = (
                        fail_at == "ledger" and "plan-decisions" in path.parts
                    ) or (fail_at == "proposal" and path == self.proposal_path)
                    if should_fail and not failed:
                        failed = True
                        raise OSError("injected write failure")
                    original_writer(path, content)

                with mock.patch.object(cockpit, "_atomic_bytes_write", side_effect=injected):
                    with self.assertRaises(cockpit.CockpitError) as raised:
                        self._approve()
                self.assertEqual(
                    raised.exception.code,
                    "decision_write_failed",
                    str(raised.exception),
                )
                for path, content in originals.items():
                    self.assertEqual(path.read_bytes(), content)
                ledger = self.root / ".kit" / "runtime" / "cockpit" / "plan-decisions"
                self.assertFalse(ledger.exists() and any(ledger.iterdir()))

    def test_native_crash_survives_static_pass_until_full_recovery(self) -> None:
        fingerprint = {
            "available": True,
            "digest": "f" * 64,
            "head": "a" * 40,
            "untracked": 0,
        }
        crash_payload = {
            "ok": False,
            "status": "failed",
            "exit_code": 1,
            "gate_summary": {
                "diagnostics": {
                    "native_crashes": [{"code": "native-crash", "stage": "gut"}]
                }
            },
        }
        with mock.patch.object(cockpit, "repository_fingerprint", return_value=fingerprint):
            cockpit.record_verification(self.root, crash_payload)
            cockpit.record_verification(
                self.root,
                {"ok": True, "status": "passed", "exit_code": 0, "static": True},
            )
            unresolved = cockpit.verification_view(self.root)

        self.assertEqual(unresolved["status"], "failed")
        self.assertEqual(unresolved["failure_class"], "native-crash")
        self.assertEqual(unresolved["latest_check_status"], "insufficient")
        self.assertIn("remains unresolved", unresolved["summary"])
        with mock.patch.object(
            cockpit,
            "repository_fingerprint",
            side_effect=AssertionError("persisted warning invoked Git"),
        ):
            warning = cockpit.persisted_native_warning(self.root)
        self.assertIsNotNone(warning)
        self.assertEqual(warning["code"], "native-crash")
        saved_root = retro_due.ROOT
        retro_due.ROOT = self.root
        try:
            consequence = retro_due.verification_consequence()
        finally:
            retro_due.ROOT = saved_root

        self.assertEqual(consequence["code"], "native-crash")

        with mock.patch.object(cockpit, "repository_fingerprint", return_value=fingerprint):
            cockpit.record_verification(
                self.root, {"ok": True, "status": "passed", "exit_code": 0}
            )
            still_unresolved = cockpit.verification_view(self.root)
        identity = "9" * 64
        unresolved_snapshot = cockpit.native_engine.NativeWarningSnapshot(
            "unresolved", identity
        )
        direct_warning = {
            "code": "native-crash",
            "recorded_at": "2026-08-28T00:00:00Z",
            "operation": "verification",
            "summary": "native crash",
        }
        with mock.patch.object(
            cockpit, "repository_fingerprint", return_value=fingerprint
        ), mock.patch.object(
            cockpit.native_engine,
            "snapshot_native_warning",
            return_value=unresolved_snapshot,
        ), mock.patch.object(
            cockpit, "persisted_native_warning", return_value=direct_warning
        ), mock.patch.object(
            cockpit.native_engine, "resolve_native_warning", return_value=True
        ) as resolve:
            recovered_record = cockpit.record_verification(
                self.root,
                _passing_gate_payload(
                    native_warning_start={
                        "state": "unresolved",
                        "identity": identity,
                    }
                ),
            )
        with mock.patch.object(cockpit, "repository_fingerprint", return_value=fingerprint):
            recovered = cockpit.verification_view(self.root)
        self.assertEqual(still_unresolved["status"], "failed")
        self.assertEqual(
            still_unresolved["latest_check_status"], "insufficient"
        )
        self.assertEqual(recovered["status"], "fresh")
        self.assertEqual(recovered_record["status"], "fresh")
        resolve.assert_called_once_with(
            self.root,
            verification_scope="full",
            expected_identity=identity,
        )
        self.assertIsNone(recovered["failure_class"])
        self.assertEqual(recovered["resolved_failure"]["failure_class"], "native-crash")
        self.assertIn("was resolved", recovered["summary"])
        self.assertIsNone(cockpit.persisted_native_warning(self.root))
        retro_due.ROOT = self.root
        try:
            self.assertIsNone(retro_due.verification_consequence())
        finally:
            retro_due.ROOT = saved_root

    def test_unchanged_static_diagnostic_preserves_fresh_complete_pointer(self) -> None:
        fingerprint = {
            "available": True,
            "digest": "f" * 64,
            "head": "a" * 40,
            "untracked": 0,
        }
        with mock.patch.object(
            cockpit, "repository_fingerprint", return_value=fingerprint
        ):
            complete = cockpit.record_verification(
                self.root, _passing_gate_payload()
            )
            diagnostic = cockpit.record_verification(
                self.root, _passing_gate_payload(static=True)
            )
            view = cockpit.verification_view(self.root)

        paths = cockpit.runtime_paths.resolve(self.root)
        latest = json.loads(paths.verification_latest.read_text(encoding="utf-8"))
        run_records = [
            path for path in paths.verification_runs.glob("*.json")
            if not path.name.endswith("-resolution.json")
        ]
        self.assertEqual(2, len(run_records))
        self.assertEqual("fresh", complete["status"])
        self.assertEqual("insufficient", diagnostic["status"])
        self.assertEqual(complete["id"], latest["id"])
        self.assertEqual(complete["record"], view["record"])
        self.assertEqual("fresh", view["status"])
        self.assertEqual("full", view["scope"])

    def test_changed_static_diagnostic_replaces_stale_complete_pointer(self) -> None:
        original = {
            "available": True,
            "digest": "f" * 64,
            "head": "a" * 40,
            "untracked": 0,
        }
        changed = {
            "available": True,
            "digest": "e" * 64,
            "head": "b" * 40,
            "untracked": 1,
        }
        with mock.patch.object(
            cockpit, "repository_fingerprint", return_value=original
        ):
            complete = cockpit.record_verification(
                self.root, _passing_gate_payload()
            )
        with mock.patch.object(
            cockpit, "repository_fingerprint", return_value=changed
        ):
            diagnostic = cockpit.record_verification(
                self.root, _passing_gate_payload(static=True)
            )

        paths = cockpit.runtime_paths.resolve(self.root)
        latest = json.loads(paths.verification_latest.read_text(encoding="utf-8"))
        self.assertNotEqual(complete["id"], diagnostic["id"])
        self.assertEqual(diagnostic["id"], latest["id"])
        self.assertEqual("insufficient", latest["status"])

    def test_gate_receipt_requires_authenticated_stable_repository_binding(self) -> None:
        payload = _passing_gate_payload()
        self.assertTrue(cockpit._trusted_gate_receipt(payload))

        for mutation in (
            lambda value: value["gate_summary"].update(schema=1),
            lambda value: value["gate_summary"].update(repository_sha256="4" * 64),
            lambda value: value.update(repository_stable=False),
            lambda value: value["gate_summary"].update(auth_sha256="missing"),
        ):
            with self.subTest(mutation=mutation):
                altered = copy.deepcopy(payload)
                mutation(altered)
                self.assertFalse(cockpit._trusted_gate_receipt(altered))

    def test_warning_appearing_after_verify_start_cannot_be_cleared(self) -> None:
        fingerprint = {
            "available": True,
            "digest": "f" * 64,
            "head": "a" * 40,
            "untracked": 0,
        }
        appeared = cockpit.native_engine.NativeWarningSnapshot(
            "unresolved", "c" * 64
        )
        warning = {
            "code": "native-crash",
            "recorded_at": "2026-08-28T00:00:00Z",
            "operation": "verification",
            "summary": "native crash",
        }
        with mock.patch.object(
            cockpit, "repository_fingerprint", return_value=fingerprint
        ), mock.patch.object(
            cockpit.native_engine, "snapshot_native_warning", return_value=appeared
        ), mock.patch.object(
            cockpit, "persisted_native_warning", return_value=warning
        ), mock.patch.object(
            cockpit.native_engine, "resolve_native_warning"
        ) as resolve:
            record = cockpit.record_verification(
                self.root, _passing_gate_payload()
            )

        self.assertEqual("insufficient", record["status"])
        self.assertEqual("native-crash", record["failure_class"])
        self.assertEqual(
            "changed-or-unknown", record["native_warning_snapshot"]
        )
        resolve.assert_not_called()

    def test_verification_pointer_failure_leaves_exact_warning_unresolved(self) -> None:
        fingerprint = {
            "available": True,
            "digest": "f" * 64,
            "head": "a" * 40,
            "untracked": 0,
        }
        identity = "8" * 64
        unresolved = cockpit.native_engine.NativeWarningSnapshot(
            "unresolved", identity
        )
        warning = {
            "code": "native-crash",
            "recorded_at": "2026-08-28T00:00:00Z",
            "operation": "verification",
            "summary": "native crash",
        }
        payload = _passing_gate_payload(
            native_warning_start={"state": "unresolved", "identity": identity}
        )
        with mock.patch.object(
            cockpit, "repository_fingerprint", return_value=fingerprint
        ), mock.patch.object(
            cockpit.native_engine, "snapshot_native_warning", return_value=unresolved
        ), mock.patch.object(
            cockpit, "persisted_native_warning", return_value=warning
        ), mock.patch.object(
            cockpit, "_durable_json_write", side_effect=OSError("disk full")
        ), mock.patch.object(
            cockpit.native_engine, "resolve_native_warning"
        ) as resolve:
            with self.assertRaises(OSError):
                cockpit.record_verification(self.root, payload)

        resolve.assert_not_called()

    def test_concurrent_verification_publish_preserves_latest_pointer(self) -> None:
        paths = cockpit.runtime_paths.resolve(self.root, create=True)
        existing = {"schema": 1, "id": "completed-proof", "status": "fresh"}
        cockpit._durable_json_write(paths.verification_latest, existing)

        with cockpit._verification_ledger_guard(paths), mock.patch.object(
            cockpit.time, "sleep", return_value=None
        ):
            with self.assertRaises(cockpit.CockpitError) as raised:
                cockpit.record_verification(self.root, _passing_gate_payload())

        self.assertEqual("verification_busy", raised.exception.code)
        self.assertEqual(
            existing,
            json.loads(paths.verification_latest.read_text(encoding="utf-8")),
        )

    def test_unreadable_native_warning_state_is_itself_a_warning(self) -> None:
        unknown = cockpit.native_engine.NativeWarningSnapshot("unknown", None)
        with mock.patch.object(
            cockpit.native_engine, "snapshot_native_warning", return_value=unknown
        ):
            warning = cockpit.persisted_native_warning(self.root)

        self.assertIsNotNone(warning)
        self.assertEqual("native-warning-state-unknown", warning["code"])
        self.assertIn("could not be authenticated", warning["summary"])

    def test_unchanged_preexisting_warning_uses_compare_and_swap(self) -> None:
        fingerprint = {
            "available": True,
            "digest": "f" * 64,
            "head": "a" * 40,
            "untracked": 0,
        }
        identity = "d" * 64
        unchanged = cockpit.native_engine.NativeWarningSnapshot(
            "unresolved", identity
        )
        warning = {
            "code": "native-crash",
            "recorded_at": "2026-08-28T00:00:00Z",
            "operation": "verification",
            "summary": "native crash",
        }
        payload = _passing_gate_payload(
            native_warning_start={"state": "unresolved", "identity": identity}
        )
        paths = cockpit.runtime_paths.resolve(self.root, create=True)

        def resolve_after_persistence(*_args, **_kwargs) -> bool:
            self.assertTrue(paths.verification_latest.is_file())
            self.assertTrue(any(paths.verification_runs.glob("*.json")))
            conservative = json.loads(
                paths.verification_latest.read_text(encoding="utf-8")
            )
            self.assertIn("unresolved_failure", conservative)
            return True

        with mock.patch.object(
            cockpit, "repository_fingerprint", return_value=fingerprint
        ), mock.patch.object(
            cockpit.native_engine, "snapshot_native_warning", return_value=unchanged
        ), mock.patch.object(
            cockpit, "persisted_native_warning", return_value=warning
        ), mock.patch.object(
            cockpit.native_engine,
            "resolve_native_warning",
            side_effect=resolve_after_persistence,
        ) as resolve:
            record = cockpit.record_verification(self.root, payload)

        self.assertEqual("fresh", record["status"])
        self.assertIsNone(record.get("unresolved_failure"))
        self.assertEqual("native-crash", record["resolved_failure"]["failure_class"])
        resolve.assert_called_once_with(
            self.root,
            verification_scope="full",
            expected_identity=identity,
        )


if __name__ == "__main__":
    unittest.main()
