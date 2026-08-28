#!/usr/bin/env python3
"""Focused contract tests for design authority and reversible delivery."""
from __future__ import annotations

import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import design  # noqa: E402
import proposal_authority  # noqa: E402
import schema  # noqa: E402


class ProposalAuthoritySchemaTest(unittest.TestCase):
    def setUp(self) -> None:
        parent = ROOT / ".checklogs" / "tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.scratch = parent / f"design-governance-schema-{uuid.uuid4().hex}"
        self.scratch.mkdir()
        self.path = self.scratch / "proposal.json"
        design_path = (
            self.scratch
            / "docs"
            / "design"
            / "interface"
            / "confirming-an-action.md"
        )
        design_path.parent.mkdir(parents=True)
        design_path.write_text("# Confirming an action\n", encoding="utf-8")

    def tearDown(self) -> None:
        expected = (ROOT / ".checklogs" / "tests").resolve()
        resolved = self.scratch.resolve()
        if resolved.parent != expected or not resolved.name.startswith(
            "design-governance-schema-"
        ):
            raise AssertionError(f"refusing to remove unexpected scratch: {resolved}")
        shutil.rmtree(resolved)

    @staticmethod
    def proposal(
        *,
        status: str = "recorded",
        authority: str = "agent-provisional",
        authored_by: str = "agent",
        confidence: str = "very-high",
        reversible: str = "reversible",
    ) -> dict:
        value = {
            "slice": "slice-confirm-action",
            "status": status,
            "baseline_sha": "b" * 40,
            "experience": {
                "player_does": "Accepts a focused action.",
                "feels_like": "Immediate and legible.",
                "not_this": "A modal interruption.",
            },
            "design_refs": [{
                "section": "docs/design/interface/confirming-an-action.md",
                "why": "Every accepted action must be understandable.",
                "sha256": "a" * 64,
            }],
            "design_authority": {
                "authority": authority,
                "authored_by": authored_by,
                "confidence": confidence,
            },
            "reversibility": {
                "state": reversible,
                "veto_scope": "Remove the isolated feedback treatment.",
                "hard_to_undo": "Teaching authored content to depend on it.",
                "next_go_no_go": "Before the feedback becomes a content contract.",
            },
            "files": [{
                "path": "scripts/confirm_feedback.gd",
                "action": "new",
                "why": "Make accepted actions immediately legible.",
            }],
        }
        if status == "approved":
            value["approved_by"] = "Justin Fenech"
            value["approved_on"] = "2026-08-27"
            value["approval_sha256"] = "b" * 64
        return value

    def validate(self, value: dict) -> tuple[list[str], list[str]]:
        self.path.write_text(json.dumps(value), encoding="utf-8")
        return schema.validate(self.path, "PROPOSAL")

    def test_recorded_agent_provisional_requires_exact_very_high(self) -> None:
        errors, warnings = self.validate(self.proposal())
        self.assertEqual([], errors)
        self.assertEqual([], warnings)

        errors, _ = self.validate(self.proposal(confidence="high"))
        self.assertTrue(any("exactly 'very-high'" in error for error in errors), errors)

    def test_recorded_is_reversible_and_never_approval(self) -> None:
        errors, _ = self.validate(self.proposal(reversible="go-no-go"))
        self.assertTrue(any("recorded work must remain reversible" in error for error in errors))

        value = self.proposal()
        value["approved_by"] = "Agent"
        value["approved_on"] = "2026-08-27"
        errors, _ = self.validate(value)
        self.assertTrue(any("valid only when status is approved" in error for error in errors))

    def test_approved_requires_human_confirmed_design_and_human_record(self) -> None:
        errors, _ = self.validate(self.proposal(status="approved"))
        self.assertTrue(any("requires human-confirmed design" in error for error in errors))

        errors, warnings = self.validate(self.proposal(
            status="approved",
            authority="human-confirmed",
            authored_by="agent",
            confidence="high",
        ))
        self.assertEqual([], errors)
        self.assertEqual([], warnings)

    def test_no_design_acknowledgement_is_history_not_authority(self) -> None:
        value = self.proposal()
        value["design_refs"] = []
        value["acknowledged"] = [{
            "warning": "no-design-refs",
            "slice": value["slice"],
            "why": "Legacy record.",
            "by": "Justin Fenech",
            "on": "2026-08-01",
        }]

        errors, warnings = self.validate(value)
        self.assertTrue(any("acknowledgement is not design authority" in error for error in errors))
        self.assertTrue(any("history only" in warning for warning in warnings))

    def test_design_reference_digest_is_canonical_lowercase_sha256(self) -> None:
        value = self.proposal()
        value["design_refs"][0]["sha256"] = "A" * 64
        errors, _ = self.validate(value)
        self.assertTrue(any("canonical lowercase SHA-256" in error for error in errors))

    def test_implementation_baseline_is_an_exact_commit(self) -> None:
        value = self.proposal()
        value["baseline_sha"] = "main"
        errors, _ = self.validate(value)
        self.assertTrue(any("exact lowercase Git commit" in error for error in errors))

        del value["baseline_sha"]
        errors, _ = self.validate(value)
        self.assertTrue(any("exact starting commit" in error for error in errors))


class QuestionRelationSchemaTest(unittest.TestCase):
    @staticmethod
    def shape(question: dict) -> dict:
        return {
            "name": "Question relation fixture",
            "pitch": "A fixture for scoped cockpit questions.",
            "involvement": "hands-off",
            "questions": [question],
            "decisions": [],
        }

    def test_optional_exact_relations_are_backward_compatible(self) -> None:
        legacy = {
            "id": "legacy-question",
            "question": "What should happen?",
            "blocks": "The old slice.",
        }
        errors, warnings = schema.validate_object(self.shape(legacy), "SHAPE")
        self.assertEqual([], errors)
        self.assertEqual([], warnings)

        related = dict(legacy)
        related["related_slices"] = ["slice-confirm-action"]
        related["related_design_refs"] = [
            "docs/design/interface/confirming-an-action.md"
        ]
        errors, warnings = schema.validate_object(self.shape(related), "SHAPE")
        self.assertEqual([], errors)
        self.assertEqual([], warnings)

    def test_relations_reject_duplicates_and_noncanonical_design_paths(self) -> None:
        question = {
            "id": "bad-relation",
            "question": "What should happen?",
            "blocks": "The active slice.",
            "related_slices": ["slice-a", "slice-a"],
            "related_design_refs": ["docs\\design\\interface.md"],
        }
        errors, _warnings = schema.validate_object(self.shape(question), "SHAPE")

        self.assertTrue(any("duplicate relations" in error for error in errors), errors)
        self.assertTrue(
            any("canonical docs/design/*.md path" in error for error in errors),
            errors,
        )


class DesignMetadataTest(unittest.TestCase):
    def setUp(self) -> None:
        parent = ROOT / ".checklogs" / "tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.scratch = parent / f"design-governance-parser-{uuid.uuid4().hex}"
        self.scratch.mkdir()
        self.design_root = self.scratch / "design"
        self.design_root.mkdir()

    def tearDown(self) -> None:
        expected = (ROOT / ".checklogs" / "tests").resolve()
        resolved = self.scratch.resolve()
        if resolved.parent != expected or not resolved.name.startswith(
            "design-governance-parser-"
        ):
            raise AssertionError(f"refusing to remove unexpected scratch: {resolved}")
        shutil.rmtree(resolved)

    def parse(self, text: str) -> dict:
        path = self.design_root / "feedback.md"
        path.write_text(text, encoding="utf-8", newline="")
        with mock.patch.object(design, "DESIGN", self.design_root):
            return design.parse_design(path)

    @staticmethod
    def provisional(confidence: str = "very-high") -> str:
        return f"""# Confirming an action

_Resolution: settled_
_Authority: agent-provisional_
_Authored by: agent_
_Confidence: {confidence}_

Accepted actions acknowledge immediately without stealing focus.

## Quick read

- **Player does:** accepts a focused action.
- **Player experiences:** an immediate, legible acknowledgement.
- **Successful outcome:** understands what happened without losing context.

## Why this inference

The established interface design requires immediate acknowledgement and retained context.

## Assumptions

- The acknowledgement is local to the accepted action.

## Veto and go/no-go

- **Veto scope:** remove or retune the isolated feedback treatment.
- **Next go/no-go:** before authored content depends on this feedback language.
"""

    def test_agent_provisional_disclosure_is_machine_readable(self) -> None:
        parsed = self.parse(self.provisional())
        self.assertEqual("settled", parsed["resolution"])
        self.assertEqual("agent-provisional", parsed["authority"])
        self.assertEqual("agent", parsed["authored_by"])
        self.assertEqual("very-high", parsed["confidence"])
        self.assertTrue(parsed["implementation_eligible"])
        self.assertEqual([], parsed["metadata_errors"])

    def test_lower_provisional_confidence_is_valid_design_but_not_build_authority(self) -> None:
        parsed = self.parse(self.provisional("high"))
        self.assertEqual([], parsed["metadata_errors"])
        self.assertFalse(parsed["implementation_eligible"])
        self.assertTrue(any("below very-high" in item for item in parsed["metadata_warnings"]))

    def test_agent_authored_design_requires_quick_read_and_veto_labels(self) -> None:
        text = self.provisional().replace("- **Successful outcome:**", "- Result:")
        text = text.replace("- **Next go/no-go:**", "- Later:")
        parsed = self.parse(text)
        self.assertTrue(any("Successful outcome:" in item for item in parsed["metadata_errors"]))
        self.assertTrue(any("Next go/no-go:" in item for item in parsed["metadata_errors"]))
        self.assertFalse(parsed["implementation_eligible"])

    def test_partial_or_malformed_new_metadata_is_rejected(self) -> None:
        text = self.provisional().replace(
            "_Authority: agent-provisional_", "_Authority: assumed_"
        )
        parsed = self.parse(text)
        self.assertTrue(any("Authority must be one of" in item for item in parsed["metadata_errors"]))

        text = self.provisional().replace(
            "_Confidence: very-high_", "_Confidence: very-high"
        )
        parsed = self.parse(text)
        self.assertTrue(any("Confidence metadata is malformed" in item for item in parsed["metadata_errors"]))

        text = self.provisional().replace(
            "_Authority: agent-provisional_",
            "_Authority: agent-provisional_\n_Authority: human-confirmed_",
        )
        parsed = self.parse(text)
        self.assertTrue(any("Authority metadata must appear exactly once" in item for item in parsed["metadata_errors"]))

    def test_commented_or_fenced_templates_cannot_supply_design_authority(self) -> None:
        hidden = self.provisional()
        for wrapped in (
            "<!--\n" + hidden + "\n-->\n# Actual design\n\nNo authority is active.\n",
            "```markdown\n" + hidden + "\n```\n# Actual design\n\nNo authority is active.\n",
        ):
            with self.subTest(prefix=wrapped[:4]):
                parsed = self.parse(wrapped)
                self.assertFalse(parsed["implementation_eligible"])
                self.assertEqual("unstated", parsed["authority"])

    def test_metadata_must_be_a_complete_block_immediately_below_title(self) -> None:
        displaced = self.provisional().replace(
            "# Confirming an action\n\n_Resolution: settled_",
            "# Confirming an action\n\nIntroductory prose.\n\n_Resolution: settled_",
        )
        parsed = self.parse(displaced)
        self.assertFalse(parsed["implementation_eligible"])
        self.assertTrue(any("complete block" in item for item in parsed["metadata_errors"]))

    def test_active_metadata_rewrite_preserves_commented_templates(self) -> None:
        text = (
            "<!-- _Authority: agent-provisional_ -->\n"
            + self.provisional()
        )
        updated = design.replace_active_metadata(
            text, "Authority", "agent-provisional", "human-confirmed"
        )
        self.assertIn("<!-- _Authority: agent-provisional_ -->", updated)
        self.assertEqual(1, updated.count("_Authority: human-confirmed_"))

    def test_closed_fenced_example_does_not_hide_later_active_design(self) -> None:
        text = self.provisional().replace(
            "## Quick read",
            "```markdown\n_Authority: human-confirmed_\n```\n\n## Quick read",
        )
        parsed = self.parse(text)
        self.assertTrue(parsed["implementation_eligible"])
        self.assertEqual("agent-provisional", parsed["authority"])

    def test_human_confirmation_does_not_erase_agent_authorship(self) -> None:
        text = self.provisional("high").replace(
            "_Authority: agent-provisional_", "_Authority: human-confirmed_"
        )
        parsed = self.parse(text)
        self.assertEqual("agent", parsed["authored_by"])
        self.assertEqual("human-confirmed", parsed["authority"])
        self.assertTrue(parsed["implementation_eligible"])

    def test_legacy_design_is_readable_but_not_implementation_eligible(self) -> None:
        parsed = self.parse(
            "# Legacy\n\n_Resolution: settled_\n\nThe player receives clear feedback.\n"
        )
        self.assertEqual([], parsed["metadata_errors"])
        self.assertTrue(parsed["metadata_warnings"])
        self.assertFalse(parsed["implementation_eligible"])

    def test_digest_ignores_generated_bindings_and_platform_line_endings(self) -> None:
        authored = self.provisional()
        generated = (
            authored.rstrip()
            + "\r\n\r\n"
            + design.BIND_BEGIN
            + "\r\n\r\n| Tunable | Value |\r\n|---|---|\r\n"
            + design.BIND_END
            + "\r\n"
        )
        self.assertEqual(design.design_sha256(authored), design.design_sha256(generated))
        self.assertNotEqual(
            design.design_sha256(authored),
            design.design_sha256(authored.replace("immediate", "instant", 1)),
        )


class DesignDecisionDigestMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        parent = ROOT / ".checklogs" / "tests"
        parent.mkdir(parents=True, exist_ok=True)
        self.scratch = parent / f"design-governance-shape-{uuid.uuid4().hex}"
        self.scratch.mkdir()
        self.path = self.scratch / "project.shape.json"

    def tearDown(self) -> None:
        resolved = self.scratch.resolve()
        expected = (ROOT / ".checklogs" / "tests").resolve()
        if resolved.parent != expected or not resolved.name.startswith(
            "design-governance-shape-"
        ):
            raise AssertionError(f"refusing to remove unexpected scratch: {resolved}")
        shutil.rmtree(resolved)

    @staticmethod
    def shape() -> dict:
        return {
            "name": "Fixture",
            "pitch": "A fixture proving design governance.",
            "involvement": "hands-off",
            "decisions": [{
                "id": "confirm-feedback",
                "question": "How should accepted actions feel?",
                "answer": "Immediate and legible.",
                "because": "Delivery now depends on it.",
                "date": "2026-08-27",
                "decided_by": "human",
                "revisit_if": "Playtesting shows distraction.",
                "docs_at": "docs/design/interface/confirming-an-action.md",
            }],
        }

    def validate(self, value: dict) -> tuple[list[str], list[str]]:
        self.path.write_text(json.dumps(value), encoding="utf-8")
        return schema.validate(self.path, "SHAPE")

    def test_legacy_docs_pointer_warns_without_inventing_approval(self) -> None:
        errors, warnings = self.validate(self.shape())
        self.assertEqual([], errors)
        self.assertTrue(any("legacy docs_at" in warning for warning in warnings))

    def test_design_digest_requires_the_document_it_binds(self) -> None:
        value = self.shape()
        del value["decisions"][0]["docs_at"]
        value["decisions"][0]["design_sha256"] = "a" * 64
        errors, _ = self.validate(value)
        self.assertTrue(any("requires docs_at" in error for error in errors))

    def test_exact_plan_authority_requires_a_human_action(self) -> None:
        value = self.shape()
        decision = value["decisions"][0]
        decision["design_sha256"] = "a" * 64
        decision["proposal_sha256"] = "b" * 64
        decision["authority_action"] = "confirm"
        decision["design_intent_sha256"] = "c" * 64
        decision["reviewed_contract_sha256"] = "d" * 64
        decision["authority_scope"] = "design-and-plan"
        decision["cockpit_receipt_id"] = "20260827T120000Z-" + "e" * 32
        decision["cockpit_reviewed_fingerprint"] = "f" * 64
        decision["cockpit_receipt_sha256"] = (
            proposal_authority.decision_receipt_sha256(decision)
        )
        errors, warnings = self.validate(value)
        self.assertEqual([], errors)
        self.assertEqual([], warnings)

        decision["decided_by"] = "agent"
        decision["cockpit_receipt_sha256"] = (
            proposal_authority.decision_receipt_sha256(decision)
        )
        errors, _ = self.validate(value)
        self.assertTrue(any("must be human" in error for error in errors))

    def test_hand_filled_authority_without_cockpit_receipt_is_rejected(self) -> None:
        value = self.shape()
        decision = value["decisions"][0]
        decision.update(
            {
                "design_sha256": "a" * 64,
                "design_intent_sha256": "b" * 64,
                "proposal_sha256": "c" * 64,
                "authority_action": "confirm",
                "reviewed_contract_sha256": "d" * 64,
                "authority_scope": "design-and-plan",
            }
        )

        errors, _ = self.validate(value)

        self.assertTrue(any("cockpit receipt" in error for error in errors))

    def test_proposal_digest_without_authority_action_is_rejected(self) -> None:
        value = self.shape()
        decision = value["decisions"][0]
        decision["design_sha256"] = "a" * 64
        decision["proposal_sha256"] = "b" * 64
        errors, _ = self.validate(value)
        self.assertTrue(any("authority_action" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
