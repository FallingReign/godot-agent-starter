#!/usr/bin/env python3
"""Protected-gate regressions for design authority and reversible autonomy."""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import check as gate  # noqa: E402
from tools import cockpit as cockpit_contract  # noqa: E402
from tools import design as design_contract  # noqa: E402
from tools import proposal_authority  # noqa: E402


def _scratch_parent() -> Path:
    configured = os.environ.get("KIT_TEST_TMPDIR", "").strip()
    return Path(configured) if configured else ROOT / ".checklogs" / "tests"


class DesignConformanceTests(unittest.TestCase):
    def setUp(self) -> None:
        parent = _scratch_parent()
        parent.mkdir(parents=True, exist_ok=True)
        self.scratch = parent / f"design-conformance-{uuid.uuid4().hex}"
        self.scratch.mkdir()
        self.git_available = True
        self.git_fail_command = ""
        self.git_tracked = ""
        self.git_untracked = ""
        self.git_baseline_files = ""
        self.git_baseline_sources: dict[str, str] = {}
        self.module_edges: dict[str, list[str]] = {}
        self.involvement = "hands-off"
        self.game_root = self.scratch / "src"
        self.game_root.mkdir()
        self.design_path = self.scratch / "docs" / "design" / "experience" / "loop.md"
        self.design_path.parent.mkdir(parents=True)
        self.design_path.write_text(
            "# Loop\n\n"
            "_Resolution: settled_\n"
            "_Authority: agent-provisional_\n"
            "_Authored by: agent_\n"
            "_Confidence: very-high_\n\n"
            "## Quick read\n"
            "- **Player does:** The thing.\n"
            "- **Player experiences:** A clear response.\n"
            "- **Successful outcome:** Intent is preserved.\n\n"
            "## Why this inference\nThe existing design says so.\n\n"
            "## Assumptions\n- The action remains reversible.\n\n"
            "## Veto and go/no-go\n"
            "- **Veto scope:** Remove the isolated implementation.\n"
            "- **Next go/no-go:** Before content is authored.\n",
            encoding="utf-8",
        )
        (self.scratch / "project.shape.json").write_text(
            json.dumps(
                {
                    "name": "Fixture",
                    "pitch": "A conformance fixture.",
                    "involvement": "hands-off",
                    "decisions": [],
                    "direction": [],
                    "questions": [],
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        resolved = self.scratch.resolve()
        expected = _scratch_parent().resolve()
        if resolved.parent != expected or not resolved.name.startswith(
            "design-conformance-"
        ):
            raise AssertionError(f"refusing to remove unexpected scratch: {resolved}")
        shutil.rmtree(resolved)

    def proposal(self) -> dict[str, object]:
        digest = design_contract.design_sha256(
            self.design_path.read_text(encoding="utf-8")
        )
        return {
            "slice": "fixture-slice",
            "status": "recorded",
            "baseline_sha": "a" * 40,
            "experience": {
                "player_does": "Acts",
                "feels_like": "Clear",
                "not_this": "Opaque",
            },
            "design_refs": [
                {
                    "section": "docs/design/experience/loop.md",
                    "why": "Defines the intended outcome",
                    "sha256": digest,
                }
            ],
            "design_authority": {
                "authority": "agent-provisional",
                "authored_by": "agent",
                "confidence": "very-high",
            },
            "reversibility": {
                "state": "reversible",
                "veto_scope": "Delete the isolated fixture",
                "hard_to_undo": "Player-authored content would bind the shape",
                "next_go_no_go": "Before persistent content is authored",
            },
            "scope": [
                {
                    "path": "scripts/logic",
                    "kind": "directory",
                    "action": "new",
                    "why": "Bound reversible fixture work to the isolated logic directory",
                }
            ],
            "modules": [
                {
                    "path": "scripts/logic",
                    "role": "Fixture logic",
                    "why": "Exercise conformance",
                    "action": "new",
                    "may_depend_on": [],
                    "boundary_data": "No data crosses this fixture boundary",
                }
            ],
        }

    def approved_proposal(self) -> dict[str, object]:
        text = self.design_path.read_text(encoding="utf-8").replace(
            "_Authority: agent-provisional_",
            "_Authority: human-confirmed_",
        )
        self.design_path.write_text(text, encoding="utf-8")
        proposal = self.proposal()
        proposal["status"] = "approved"
        proposal["approved_by"] = "Fixture human"
        proposal["approved_on"] = "2026-08-27"
        proposal["design_authority"]["authority"] = "human-confirmed"  # type: ignore[index]
        proposal["approval_sha256"] = cockpit_contract.approval_sha256(proposal)
        return proposal

    def write_matching_approval(self, proposal: dict[str, object]) -> None:
        proposal["approval_sha256"] = cockpit_contract.approval_sha256(proposal)
        event = self.authority_event(proposal, "confirm-current-fixture-plan")
        (self.scratch / "project.shape.json").write_text(
            json.dumps({"decisions": [event]}),
            encoding="utf-8",
        )

    def authority_event(
        self,
        proposal: dict[str, object],
        identifier: str,
        *,
        action: str = "confirm",
        supersedes: str = "",
    ) -> dict[str, object]:
        digest = proposal["design_refs"][0]["sha256"]  # type: ignore[index]
        event: dict[str, object] = {
            "id": identifier,
            "question": "Approve this exact fixture plan?",
            "answer": "Approved" if action == "confirm" else "Vetoed",
            "because": "The cockpit review was explicit",
            "date": "2026-08-27",
            "decided_by": "human",
            "revisit_if": "The design or implementation plan changes",
            "docs_at": "docs/design/experience/loop.md",
            "design_sha256": digest,
            "design_intent_sha256": proposal_authority.design_intent_sha256(
                self.design_path
            ),
            "proposal_sha256": proposal["approval_sha256"],
            "authority_action": action,
            "authority_scope": "design-and-plan",
            "reviewed_contract_sha256": proposal_authority.reviewed_contract_sha256(
                self.scratch, proposal  # type: ignore[arg-type]
            ),
            "cockpit_receipt_id": "20260827T120000Z-" + "1" * 32,
            "cockpit_reviewed_fingerprint": proposal_authority.reviewed_contract_sha256(
                self.scratch, proposal  # type: ignore[arg-type]
            ),
        }
        if supersedes:
            event["supersedes"] = supersedes
        event["cockpit_receipt_sha256"] = (
            proposal_authority.decision_receipt_sha256(event)
        )
        return event

    @contextmanager
    def run_gate(
        self,
        proposal: dict[str, object] | None,
        *,
        completion_required: bool = False,
    ) -> Iterator[gate.Results]:
        proposal_path = self.scratch / "proposal.json"
        if proposal is None:
            proposal_path.unlink(missing_ok=True)
        else:
            proposal_path.write_text(json.dumps(proposal), encoding="utf-8")
        results = gate.Results()
        def fake_git(
            command: list[str],
            _timeout: int,
            _log: Path,
            *,
            env: Mapping[str, str] | None = None,
        ) -> tuple[int, str]:
            if any(str(part).endswith("arch.py") for part in command):
                return 0, json.dumps({"modules": self.module_edges})
            self.assertIsNotNone(env)
            assert env is not None
            expected_git_environment = {
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            }
            self.assertEqual(
                expected_git_environment,
                {
                    name: value
                    for name, value in env.items()
                    if name.upper().startswith("GIT_")
                },
            )
            operation = next(
                (name for name in ("rev-parse", "diff", "ls-files", "ls-tree", "show")
                 if name in command),
                "",
            )
            if operation == self.git_fail_command:
                return 1, "fixture Git failure"
            if operation == "rev-parse":
                return 0, "a" * 40 + "\n"
            if operation == "diff":
                return 0, self.git_tracked
            if operation == "ls-files":
                return 0, self.git_untracked
            if operation == "ls-tree":
                return 0, self.git_baseline_files
            if operation == "show":
                repository_path = str(command[-1]).split(":", 1)[-1]
                relative = repository_path.removeprefix("src/")
                if relative not in self.git_baseline_sources:
                    return 1, "fixture baseline source missing"
                return 0, self.git_baseline_sources[relative]
            raise AssertionError(f"unexpected conformance command: {command}")

        with mock.patch.object(gate, "ROOT", self.scratch), mock.patch.object(
            gate, "PROJECT_DIR", self.game_root
        ), mock.patch.object(gate, "GAME_LAYOUT", "src"), mock.patch.object(
            gate, "LOG_DIR", self.scratch / ".checklogs"
        ), mock.patch.object(gate, "RESULTS", results), mock.patch.object(
            gate, "involvement", return_value=self.involvement
        ), mock.patch.object(
            design_contract, "DESIGN", self.scratch / "docs" / "design"
        ), mock.patch.object(
            gate,
            "_ordinary_executable",
            return_value="git" if self.git_available else None,
            side_effect=None if self.git_available else FileNotFoundError("fixture"),
        ), mock.patch.object(
            gate, "run", side_effect=fake_git
        ), mock.patch.object(
            gate, "CONFORMANCE_COMPLETION_REQUIRED", completion_required
        ), mock.patch.object(gate, "skill"), mock.patch.object(gate, "write_plan"):
            gate.stage_conformance()
            yield results

    def assert_failure(self, proposal: dict[str, object], expected: str) -> None:
        with self.run_gate(proposal) as results:
            self.assertTrue(results.failed)
            self.assertTrue(
                any(expected in line for line in results.lines),
                msg=f"{expected!r} not in {results.lines!r}",
            )

    def test_absent_proposal_is_only_a_clean_planning_state(self) -> None:
        with self.run_gate(None) as results:
            self.assertFalse(results.failed)
            self.assertIn("SKIP  conformance", results.lines)

    def test_authored_change_without_proposal_has_no_implementation_authority(self) -> None:
        source = self.game_root / "scripts" / "logic" / "unbound.gd"
        source.parent.mkdir(parents=True)
        source.write_text("extends RefCounted\n", encoding="utf-8")
        self.git_untracked = "src/scripts/logic/unbound.gd\n"

        with self.run_gate(None) as results:
            self.assertTrue(results.failed)
            self.assertIn(
                "FAIL  conformance (implementation without design authority)",
                results.lines,
            )

    def test_authored_deletion_without_proposal_is_not_invisible(self) -> None:
        self.git_tracked = "src/scripts/logic/removed.gd\n"

        with self.run_gate(None) as results:
            self.assertTrue(results.failed)
            self.assertIn(
                "FAIL  conformance (implementation without design authority)",
                results.lines,
            )

    def test_absent_proposal_fails_closed_when_scope_cannot_be_proven(self) -> None:
        source = self.game_root / "scripts" / "logic" / "unbound.gd"
        source.parent.mkdir(parents=True)
        source.write_text("extends RefCounted\n", encoding="utf-8")
        self.git_available = False

        with self.run_gate(None) as results:
            self.assertTrue(results.failed)
            self.assertIn(
                "FAIL  conformance (unbound scope unavailable)", results.lines
            )

    def test_missing_design_cannot_be_acknowledged_away(self) -> None:
        proposal = self.proposal()
        proposal["design_refs"] = []
        proposal["acknowledged"] = [
            {
                "warning": "no-design-refs",
                "slice": "fixture-slice",
                "why": "Legacy approval",
                "by": "Human",
                "on": "2026-08-27",
            }
        ]
        self.assert_failure(proposal, "no design authority")

    def test_function_only_scope_still_requires_design_authority(self) -> None:
        proposal = self.proposal()
        proposal["modules"] = []
        proposal["functions"] = [
            {
                "file": "scripts/logic/action.gd",
                "signature": "func act() -> void",
                "why": "Deliver the designed response",
                "action": "new",
            }
        ]
        proposal["design_refs"] = []
        self.assert_failure(proposal, "no design authority")

    def test_changed_design_invalidates_the_bound_digest(self) -> None:
        proposal = self.proposal()
        self.design_path.write_text("# Changed after review\n", encoding="utf-8")
        self.assert_failure(proposal, "stale design_refs")

    def test_provisional_delivery_requires_exact_very_high_confidence(self) -> None:
        proposal = self.proposal()
        proposal["design_authority"]["confidence"] = "high"  # type: ignore[index]
        self.assert_failure(proposal, "provisional design not eligible")

    def test_proposal_cannot_claim_human_authority_over_provisional_design(self) -> None:
        proposal = self.proposal()
        proposal["design_authority"]["authority"] = "human-confirmed"  # type: ignore[index]
        self.assert_failure(proposal, "design authority mismatch")

    def test_hands_off_stops_at_go_no_go(self) -> None:
        proposal = self.proposal()
        proposal["reversibility"]["state"] = "go-no-go"  # type: ignore[index]
        self.assert_failure(proposal, "go-no-go approval required")

    def test_draft_never_authorizes_delivery(self) -> None:
        proposal = self.proposal()
        proposal["status"] = "draft"
        self.assert_failure(proposal, "decision required")

    def test_approved_work_requires_attributed_human_confirmation(self) -> None:
        proposal = self.proposal()
        proposal["status"] = "approved"
        self.assert_failure(proposal, "approval attribution missing")

    def test_approved_work_requires_an_exact_human_decision_record(self) -> None:
        proposal = self.approved_proposal()
        (self.scratch / "project.shape.json").write_text(
            json.dumps({"decisions": []}), encoding="utf-8"
        )
        self.assert_failure(proposal, "stale plan approval")

    def test_approved_work_accepts_the_matching_human_decision_record(self) -> None:
        proposal = self.approved_proposal()
        event = self.authority_event(proposal, "confirm-loop-design")
        (self.scratch / "project.shape.json").write_text(
            json.dumps({"decisions": [event]}),
            encoding="utf-8",
        )
        with self.run_gate(proposal) as results:
                self.assertFalse(results.failed)
                self.assertIn("PASS  conformance", results.lines)

    def test_changed_plan_invalidates_its_approval_digest(self) -> None:
        proposal = self.approved_proposal()
        proposal["reversibility"]["next_go_no_go"] = "A different boundary"  # type: ignore[index]
        self.assert_failure(proposal, "stale plan approval")

    def test_gate_and_cockpit_share_the_approval_digest_contract(self) -> None:
        proposal = self.approved_proposal()
        self.assertEqual(
            cockpit_contract.approval_sha256(proposal),
            gate.proposal_approval_sha256(proposal),
        )

    def test_superseded_confirmation_cannot_be_reused(self) -> None:
        proposal = self.approved_proposal()
        confirm_id = "confirm-exact-plan"
        confirmation = self.authority_event(proposal, confirm_id)
        veto = self.authority_event(
            proposal,
            "veto-exact-plan",
            action="veto",
            supersedes=confirm_id,
        )
        (self.scratch / "project.shape.json").write_text(
            json.dumps({"decisions": [confirmation, veto]}),
            encoding="utf-8",
        )
        self.assert_failure(proposal, "human rejection active")

    def test_very_high_provisional_work_can_continue_while_reversible(self) -> None:
        proposal = self.proposal()
        with self.run_gate(proposal) as results:
            self.assertFalse(results.failed)
            self.assertIn("PASS  conformance", results.lines)

    def test_recorded_status_cannot_authorize_an_empty_scope(self) -> None:
        proposal = self.proposal()
        proposal["scope"] = []
        self.assert_failure(proposal, "involvement scope incomplete")

    def test_full_completion_proof_rejects_declared_work_that_is_not_observed(self) -> None:
        proposal = self.proposal()
        with self.run_gate(proposal, completion_required=True) as results:
            self.assertTrue(results.failed)
            self.assertIn("FAIL  conformance (implementation incomplete)", results.lines)

    def test_unrecorded_structure_deviation_blocks_completion(self) -> None:
        proposal = self.proposal()
        unplanned = self.game_root / "other" / "unplanned.gd"
        unplanned.parent.mkdir()
        unplanned.write_text("extends RefCounted\n", encoding="utf-8")
        self.git_untracked = "src/other/unplanned.gd\n"
        with mock.patch.object(gate, "arch_depth", return_value=2):
            self.assert_failure(proposal, "unrecorded deviation")

    def test_unproposed_authored_content_document_blocks_completion(self) -> None:
        proposal = self.proposal()
        content = self.game_root / "content" / "map.json"
        content.parent.mkdir()
        content.write_text('{"cells": []}\n', encoding="utf-8")
        self.git_untracked = "src/content/map.json\n"
        with mock.patch.object(gate, "arch_depth", return_value=2):
            self.assert_failure(proposal, "unrecorded deviation")

    def test_missing_git_fails_closed(self) -> None:
        self.git_available = False
        self.assert_failure(self.proposal(), "git unavailable")

    def test_unresolvable_baseline_fails_closed(self) -> None:
        self.git_fail_command = "rev-parse"
        self.assert_failure(self.proposal(), "baseline unavailable")

    def test_failed_untracked_query_fails_closed(self) -> None:
        self.git_fail_command = "ls-files"
        self.assert_failure(self.proposal(), "untracked scope unavailable")

    def test_declared_file_and_module_deletion_is_conformant(self) -> None:
        proposal = self.proposal()
        proposal["scope"][0]["action"] = "delete"  # type: ignore[index]
        proposal["modules"][0]["action"] = "delete"  # type: ignore[index]
        proposal["files"] = [
            {
                "path": "scripts/logic/old.gd",
                "action": "delete",
                "why": "Remove the superseded designed behavior",
                "module": "scripts/logic",
            }
        ]
        self.git_tracked = "src/scripts/logic/old.gd\n"
        self.git_baseline_files = "src/scripts/logic/old.gd\n"
        with self.run_gate(proposal) as results:
            self.assertFalse(results.failed)
            self.assertIn("PASS  conformance", results.lines)

    def test_hands_off_directory_action_cannot_hide_mixed_file_actions(self) -> None:
        proposal = self.proposal()
        proposal["scope"][0]["action"] = "modify"  # type: ignore[index]
        existing = self.game_root / "scripts" / "logic" / "existing.gd"
        added = self.game_root / "scripts" / "logic" / "added.gd"
        existing.parent.mkdir(parents=True)
        existing.write_text("extends RefCounted\n", encoding="utf-8")
        added.write_text("extends RefCounted\n", encoding="utf-8")
        self.git_tracked = "src/scripts/logic/existing.gd\n"
        self.git_untracked = "src/scripts/logic/added.gd\n"
        self.git_baseline_files = "src/scripts/logic/existing.gd\n"

        self.assert_failure(proposal, "unrecorded deviation")

    def test_more_specific_hands_off_boundary_can_own_a_different_action(self) -> None:
        proposal = self.proposal()
        proposal["scope"][0]["action"] = "modify"  # type: ignore[index]
        proposal["modules"][0]["action"] = "modify"  # type: ignore[index]
        proposal["scope"].append(  # type: ignore[union-attr]
            {
                "path": "scripts/logic/added.gd",
                "kind": "file",
                "action": "new",
                "why": "The new file is separately reversible",
            }
        )
        existing = self.game_root / "scripts" / "logic" / "existing.gd"
        added = self.game_root / "scripts" / "logic" / "added.gd"
        existing.parent.mkdir(parents=True)
        existing.write_text("extends RefCounted\n", encoding="utf-8")
        added.write_text("extends RefCounted\n", encoding="utf-8")
        self.git_tracked = "src/scripts/logic/existing.gd\n"
        self.git_untracked = "src/scripts/logic/added.gd\n"
        self.git_baseline_files = "src/scripts/logic/existing.gd\n"

        with self.run_gate(proposal) as results:
            self.assertFalse(results.failed)
            self.assertIn("PASS  conformance", results.lines)

    def test_root_hands_off_boundary_means_the_configured_game_root(self) -> None:
        proposal = self.proposal()
        proposal["scope"] = [
            {
                "path": "(root)",
                "kind": "directory",
                "action": "new",
                "why": "The isolated root file is reversibly removable",
            }
        ]
        root_file = self.game_root / "entry.gd"
        root_file.write_text("extends RefCounted\n", encoding="utf-8")
        self.git_untracked = "src/entry.gd\n"

        with self.run_gate(proposal) as results:
            self.assertFalse(results.failed)
            self.assertIn("PASS  conformance", results.lines)

    def test_deleted_file_cannot_be_disguised_as_modify(self) -> None:
        proposal = self.proposal()
        proposal["modules"][0]["action"] = "delete"  # type: ignore[index]
        proposal["files"] = [
            {
                "path": "scripts/logic/old.gd",
                "action": "modify",
                "why": "Incorrectly describes a deletion",
            }
        ]
        self.git_tracked = "src/scripts/logic/old.gd\n"
        self.git_baseline_files = "src/scripts/logic/old.gd\n"
        self.assert_failure(proposal, "unrecorded deviation")

    def test_unknown_authored_extension_is_not_outside_conformance(self) -> None:
        proposal = self.proposal()
        custom = self.game_root / "content" / "encounter.customdata"
        custom.parent.mkdir()
        custom.write_text("encounter-one\n", encoding="utf-8")
        self.git_untracked = "src/content/encounter.customdata\n"
        with mock.patch.object(gate, "arch_depth", return_value=2):
            self.assert_failure(proposal, "unrecorded deviation")

    def test_project_test_file_is_not_invisible_to_file_involvement(self) -> None:
        proposal = self.proposal()
        test_file = self.game_root / "tests" / "unit" / "test_action.gd"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("extends RefCounted\n", encoding="utf-8")
        self.git_untracked = "src/tests/unit/test_action.gd\n"
        with mock.patch.object(gate, "arch_depth", return_value=2):
            self.assert_failure(proposal, "unrecorded deviation")

    def test_module_dependency_outside_the_proposal_blocks(self) -> None:
        proposal = self.proposal()
        self.module_edges = {"scripts/logic": ["scripts/data"]}
        self.assert_failure(proposal, "unrecorded deviation")

    def test_declared_module_dependency_is_allowed(self) -> None:
        proposal = self.proposal()
        proposal["modules"][0]["may_depend_on"] = ["scripts/data"]  # type: ignore[index]
        self.module_edges = {"scripts/logic": ["scripts/data"]}
        with self.run_gate(proposal) as results:
            self.assertFalse(results.failed)
            self.assertIn("PASS  conformance", results.lines)

    def test_function_involvement_accepts_an_exact_new_function(self) -> None:
        proposal = self.approved_proposal()
        proposal["files"] = [
            {
                "path": "scripts/logic/action.gd",
                "action": "new",
                "why": "Expose the designed action",
                "module": "scripts/logic",
            }
        ]
        proposal["functions"] = [
            {
                "file": "scripts/logic/action.gd",
                "signature": "func act(value: int) -> void",
                "why": "Apply the player's accepted action",
                "action": "new",
                "module": "scripts/logic",
            }
        ]
        self.write_matching_approval(proposal)
        source = self.game_root / "scripts" / "logic" / "action.gd"
        source.parent.mkdir(parents=True)
        source.write_text(
            "extends RefCounted\n\n"
            "func act(value: int) -> void:\n"
            "    pass\n",
            encoding="utf-8",
        )
        self.git_untracked = "src/scripts/logic/action.gd\n"
        self.module_edges = {"scripts/logic": []}
        self.involvement = "function"
        with self.run_gate(proposal) as results:
            self.assertFalse(results.failed)
            self.assertIn("PASS  conformance", results.lines)

    def test_function_involvement_rejects_an_unproposed_new_function(self) -> None:
        proposal = self.approved_proposal()
        proposal["files"] = [
            {
                "path": "scripts/logic/action.gd",
                "action": "new",
                "why": "Expose the designed action",
                "module": "scripts/logic",
            }
        ]
        self.write_matching_approval(proposal)
        source = self.game_root / "scripts" / "logic" / "action.gd"
        source.parent.mkdir(parents=True)
        source.write_text(
            "extends RefCounted\n\n"
            "func act(value: int) -> void:\n"
            "    pass\n",
            encoding="utf-8",
        )
        self.git_untracked = "src/scripts/logic/action.gd\n"
        self.module_edges = {"scripts/logic": []}
        self.involvement = "function"
        self.assert_failure(proposal, "involvement scope incomplete")

    def test_function_involvement_accepts_a_body_only_modification(self) -> None:
        proposal = self.approved_proposal()
        proposal["modules"][0]["action"] = "modify"  # type: ignore[index]
        proposal["files"] = [
            {
                "path": "scripts/logic/action.gd",
                "action": "modify",
                "why": "Refine the designed response",
                "module": "scripts/logic",
            }
        ]
        proposal["functions"] = [
            {
                "file": "scripts/logic/action.gd",
                "signature": "func act(value: int) -> int",
                "why": "Return the newly computed response",
                "action": "modify",
                "module": "scripts/logic",
            }
        ]
        self.write_matching_approval(proposal)
        baseline = (
            "extends RefCounted\n\n"
            "func act(value: int) -> int:\n"
            "    return value\n"
        )
        current = baseline.replace("return value", "return value + 1")
        source = self.game_root / "scripts" / "logic" / "action.gd"
        source.parent.mkdir(parents=True)
        source.write_text(current, encoding="utf-8")
        self.git_tracked = "src/scripts/logic/action.gd\n"
        self.git_baseline_files = "src/scripts/logic/action.gd\n"
        self.git_baseline_sources["scripts/logic/action.gd"] = baseline
        self.module_edges = {"scripts/logic": []}
        self.involvement = "function"
        with self.run_gate(proposal) as results:
            self.assertFalse(results.failed)
            self.assertIn("PASS  conformance", results.lines)

    def test_function_involvement_accepts_a_signature_modification(self) -> None:
        proposal = self.approved_proposal()
        proposal["modules"][0]["action"] = "modify"  # type: ignore[index]
        proposal["files"] = [
            {
                "path": "scripts/logic/action.gd",
                "action": "modify",
                "why": "Add the designed strength input",
                "module": "scripts/logic",
            }
        ]
        proposal["functions"] = [
            {
                "file": "scripts/logic/action.gd",
                "signature": "func act(value: int, strength: int = 1) -> int",
                "why": "Apply the selected action strength",
                "action": "modify",
                "module": "scripts/logic",
            }
        ]
        self.write_matching_approval(proposal)
        baseline = (
            "extends RefCounted\n\n"
            "func act(value: int) -> int:\n"
            "    return value\n"
        )
        current = (
            "extends RefCounted\n\n"
            "func act(value: int, strength: int = 1) -> int:\n"
            "    return value * strength\n"
        )
        source = self.game_root / "scripts" / "logic" / "action.gd"
        source.parent.mkdir(parents=True)
        source.write_text(current, encoding="utf-8")
        self.git_tracked = "src/scripts/logic/action.gd\n"
        self.git_baseline_files = "src/scripts/logic/action.gd\n"
        self.git_baseline_sources["scripts/logic/action.gd"] = baseline
        self.module_edges = {"scripts/logic": []}
        self.involvement = "function"
        with self.run_gate(proposal) as results:
            self.assertFalse(results.failed)
            self.assertIn("PASS  conformance", results.lines)

    def test_function_involvement_accepts_an_exact_deletion(self) -> None:
        proposal = self.approved_proposal()
        proposal["modules"][0]["action"] = "modify"  # type: ignore[index]
        proposal["files"] = [
            {
                "path": "scripts/logic/action.gd",
                "action": "modify",
                "why": "Remove the superseded action only",
                "module": "scripts/logic",
            }
        ]
        proposal["functions"] = [
            {
                "file": "scripts/logic/action.gd",
                "signature": "func old_action(value: int) -> int",
                "why": "Remove behavior the design superseded",
                "action": "delete",
                "module": "scripts/logic",
            }
        ]
        self.write_matching_approval(proposal)
        baseline = (
            "extends RefCounted\n\n"
            "func old_action(value: int) -> int:\n"
            "    return value\n\n"
            "func retained(value: int) -> int:\n"
            "    return value\n"
        )
        current = (
            "extends RefCounted\n\n"
            "func retained(value: int) -> int:\n"
            "    return value\n"
        )
        source = self.game_root / "scripts" / "logic" / "action.gd"
        source.parent.mkdir(parents=True)
        source.write_text(current, encoding="utf-8")
        self.git_tracked = "src/scripts/logic/action.gd\n"
        self.git_baseline_files = "src/scripts/logic/action.gd\n"
        self.git_baseline_sources["scripts/logic/action.gd"] = baseline
        self.module_edges = {"scripts/logic": []}
        self.involvement = "function"
        with self.run_gate(proposal) as results:
            self.assertFalse(results.failed)
            self.assertIn("PASS  conformance", results.lines)

    def test_function_action_cannot_disguise_a_signature_mismatch(self) -> None:
        proposal = self.approved_proposal()
        proposal["modules"][0]["action"] = "modify"  # type: ignore[index]
        proposal["files"] = [
            {
                "path": "scripts/logic/action.gd",
                "action": "modify",
                "why": "Refine the action",
                "module": "scripts/logic",
            }
        ]
        proposal["functions"] = [
            {
                "file": "scripts/logic/action.gd",
                "signature": "func act(value: int) -> int",
                "why": "Incorrectly retains the old signature",
                "action": "modify",
                "module": "scripts/logic",
            }
        ]
        self.write_matching_approval(proposal)
        baseline = (
            "extends RefCounted\n\n"
            "func act(value: int) -> int:\n"
            "    return value\n"
        )
        current = (
            "extends RefCounted\n\n"
            "func act(value: int, strength: int) -> int:\n"
            "    return value * strength\n"
        )
        source = self.game_root / "scripts" / "logic" / "action.gd"
        source.parent.mkdir(parents=True)
        source.write_text(current, encoding="utf-8")
        self.git_tracked = "src/scripts/logic/action.gd\n"
        self.git_baseline_files = "src/scripts/logic/action.gd\n"
        self.git_baseline_sources["scripts/logic/action.gd"] = baseline
        self.module_edges = {"scripts/logic": []}
        self.involvement = "function"
        self.assert_failure(proposal, "unrecorded deviation")

    def test_unbuilt_function_is_informational_before_implementation(self) -> None:
        proposal = self.proposal()
        proposal["functions"] = [
            {
                "file": "scripts/logic/action.gd",
                "signature": "func act(value: int) -> void",
                "why": "Declare the intended entry point",
                "action": "new",
                "module": "scripts/logic",
            }
        ]
        with self.run_gate(proposal) as results:
            self.assertFalse(results.failed)
            self.assertIn("PASS  conformance", results.lines)

    def test_function_baseline_read_failure_blocks_instead_of_omitting_scope(self) -> None:
        proposal = self.approved_proposal()
        proposal["modules"][0]["action"] = "modify"  # type: ignore[index]
        proposal["files"] = [
            {
                "path": "scripts/logic/action.gd",
                "action": "modify",
                "why": "Refine the action",
                "module": "scripts/logic",
            }
        ]
        proposal["functions"] = [
            {
                "file": "scripts/logic/action.gd",
                "signature": "func act(value: int) -> int",
                "why": "Refine the action response",
                "action": "modify",
                "module": "scripts/logic",
            }
        ]
        self.write_matching_approval(proposal)
        source = self.game_root / "scripts" / "logic" / "action.gd"
        source.parent.mkdir(parents=True)
        source.write_text(
            "extends RefCounted\n\n"
            "func act(value: int) -> int:\n"
            "    return value + 1\n",
            encoding="utf-8",
        )
        self.git_tracked = "src/scripts/logic/action.gd\n"
        self.git_baseline_files = "src/scripts/logic/action.gd\n"
        self.module_edges = {"scripts/logic": []}
        self.involvement = "function"
        self.assert_failure(proposal, "function source unavailable")


if __name__ == "__main__":
    unittest.main()
