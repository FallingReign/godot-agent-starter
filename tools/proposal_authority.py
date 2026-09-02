#!/usr/bin/env python3
"""Pure proposal/design authority contracts shared by every kit consumer.

The schema, verifier and cockpit must agree on three identities:

* a canonical design reference path;
* the exact approved proposal digest; and
* the substantive reviewed contract, which deliberately ignores lifecycle
  labels so a vetoed plan cannot be resumed by changing ``approved`` to
  ``recorded``.

This module performs no writes and starts no provider or engine process.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Callable

try:
    import design
    import process_supervisor
except ImportError:  # imported as tools.proposal_authority
    from tools import design  # type: ignore[no-redef]
    from tools import process_supervisor  # type: ignore[no-redef]


class AuthorityError(ValueError):
    """The proposal cannot be treated as implementation authority."""


COCKPIT_ACTOR_LABEL = "local cockpit operator (human-presence policy)"


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def approval_sha256(proposal: dict[str, Any]) -> str:
    """Return the exact digest stored on an approved proposal."""
    material = copy.deepcopy(proposal)
    material.pop("approval_sha256", None)
    return hashlib.sha256(_canonical(material)).hexdigest()


def canonical_design_reference(root: Path, section: object) -> tuple[str, Path]:
    """Resolve one exact, regular, non-link ``docs/design/*.md`` reference."""
    if not isinstance(section, str) or section != section.strip():
        raise AuthorityError(
            "design section must be an exact docs/design/... Markdown path"
        )
    raw = section
    if (
        not raw.startswith("docs/design/")
        or "\\" in raw
        or "#" in raw
        or "?" in raw
        or "\x00" in raw
    ):
        raise AuthorityError(
            f"{raw or '(empty reference)'} must be an exact docs/design/... Markdown path"
        )
    pure = PurePosixPath(raw)
    if (
        pure.is_absolute()
        or "." in pure.parts
        or ".." in pure.parts
        or len(pure.parts) < 3
        or PurePosixPath(*pure.parts).as_posix() != raw
        or pure.suffix.lower() != ".md"
        or pure.name in ("INDEX.md", "README.md")
    ):
        raise AuthorityError(
            f"{raw} is not a canonical design-section path under docs/design/"
        )
    canonical_root = root.resolve(strict=True)
    design_root = (canonical_root / "docs" / "design").resolve(strict=False)
    lexical = canonical_root.joinpath(*pure.parts)
    try:
        candidate = lexical.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise AuthorityError(f"{raw} is missing or unreadable") from exc
    if (
        candidate == design_root
        or not candidate.is_relative_to(design_root)
        or not candidate.is_file()
        or lexical.is_symlink()
    ):
        raise AuthorityError(f"{raw} is missing, linked, or outside docs/design/")
    # A linked parent can redirect an apparently safe child outside its reviewed
    # tree even when the final path itself is not a symlink.
    cursor = lexical
    while cursor != canonical_root:
        if cursor.is_symlink():
            raise AuthorityError(f"{raw} traverses a symbolic link")
        cursor = cursor.parent
    return raw, candidate


def current_design_digest(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AuthorityError(f"cannot read canonical design {path.name}: {exc}") from exc
    return design.design_sha256(text)


def validated_design_refs(
    root: Path, proposal: dict[str, Any], *, require_current: bool = True
) -> list[dict[str, Any]]:
    """Validate canonical paths, digests and implementation eligibility."""
    refs = proposal.get("design_refs")
    if not isinstance(refs, list) or not refs:
        raise AuthorityError("implementation requires at least one design reference")
    seen: set[str] = set()
    resolved: list[dict[str, Any]] = []
    for index, entry in enumerate(refs):
        if not isinstance(entry, dict):
            raise AuthorityError(f"design_refs[{index}] must be an object")
        section, path = canonical_design_reference(root, entry.get("section"))
        if section in seen:
            raise AuthorityError(f"{section} is referenced more than once")
        seen.add(section)
        supplied = entry.get("sha256")
        if not isinstance(supplied, str) or re.fullmatch(r"[0-9a-f]{64}", supplied) is None:
            raise AuthorityError(f"{section} has no canonical SHA-256 digest")
        actual = current_design_digest(path)
        if require_current and supplied != actual:
            raise AuthorityError(
                f"{section} changed after the proposal was written "
                f"(proposal {supplied[:12]}, current {actual[:12]})"
            )
        try:
            parsed = design.parse_design(path, design_root=root / "docs" / "design")
        except (OSError, UnicodeError, ValueError) as exc:
            raise AuthorityError(f"cannot parse {section}: {exc}") from exc
        errors = parsed.get("metadata_errors") if isinstance(parsed, dict) else []
        if errors:
            raise AuthorityError(f"{section}: {'; '.join(str(v) for v in errors)}")
        if not isinstance(parsed, dict) or not parsed.get("implementation_eligible"):
            raise AuthorityError(f"{section} is not implementation-eligible")
        resolved.append(
            {
                "section": section,
                "path": path,
                "supplied_sha256": supplied,
                "current_sha256": actual,
                "metadata": parsed,
            }
        )
    return resolved


def baseline_exists(root: Path, baseline: object) -> bool:
    """True only when ``baseline`` names an exact commit in this repository."""
    if not isinstance(baseline, str) or re.fullmatch(r"[0-9a-f]{40,64}", baseline) is None:
        return False
    try:
        executable = process_supervisor.resolve_ordinary_executable(
            "git",
            excluded_roots=(root, Path(__file__).resolve().parent.parent),
        )
        result = subprocess.run(
            [executable, "cat-file", "-e", f"{baseline}^{{commit}}"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def validate_approval_contract(
    root: Path,
    proposal: dict[str, Any],
    shape: dict[str, Any],
    *,
    allowed_statuses: tuple[str, ...] = ("draft", "recorded"),
    allow_human_reconfirmation: bool = False,
) -> list[dict[str, Any]]:
    """Validate the complete pre-approval contract without changing authority.

    This is intentionally the same pure entry point used by the cockpit and
    gate. It checks the declared schema, project involvement depth, baseline,
    exact current design ancestry, experience, preview disclosure and
    reversible/go-no-go envelope before an approval action is offered.
    """
    try:
        import schema
    except ImportError:  # imported as tools.proposal_authority
        from tools import schema  # type: ignore[no-redef]

    proposal_errors, _ = schema.validate_object(
        proposal, "PROPOSAL", root=root, source_name="proposal.json"
    )
    shape_errors, _ = schema.validate_object(
        shape, "SHAPE", root=root, source_name="project.shape.json"
    )
    errors = [*proposal_errors, *shape_errors]
    status = str(proposal.get("status") or "")
    if status not in allowed_statuses:
        errors.append(
            f"proposal status must be one of {', '.join(allowed_statuses)} before approval"
        )
    if not baseline_exists(root, proposal.get("baseline_sha")):
        errors.append("baseline_sha does not resolve to a commit in this repository")

    experience = proposal.get("experience")
    if not isinstance(experience, dict) or not all(
        isinstance(experience.get(key), str) and experience[key].strip()
        for key in ("player_does", "feels_like", "not_this")
    ):
        errors.append("experience must state player_does, feels_like and not_this")
    mockup = proposal.get("mockup")
    if not isinstance(mockup, dict) or not (
        isinstance(mockup.get("svg"), str) and mockup["svg"].strip()
        or isinstance(mockup.get("not_possible"), str)
        and mockup["not_possible"].strip()
    ):
        errors.append("mockup must contain inline svg or explain why it is not possible")
    reversibility = proposal.get("reversibility")
    if not isinstance(reversibility, dict) or not all(
        isinstance(reversibility.get(key), str) and reversibility[key].strip()
        for key in ("state", "veto_scope", "hard_to_undo", "next_go_no_go")
    ):
        errors.append("reversibility must state the veto scope and next go/no-go")

    involvement = str(shape.get("involvement") or "")
    scope = proposal.get("scope") if isinstance(proposal.get("scope"), list) else []
    modules = proposal.get("modules") if isinstance(proposal.get("modules"), list) else []
    files = proposal.get("files") if isinstance(proposal.get("files"), list) else []
    functions = proposal.get("functions") if isinstance(proposal.get("functions"), list) else []
    if involvement == "hands-off":
        if not scope:
            errors.append("hands-off work requires at least one coarse scope boundary")
    elif involvement == "module":
        if not modules:
            errors.append("module involvement requires declared modules and boundaries")
    elif involvement == "file":
        if not modules or not files:
            errors.append("file involvement requires declared modules and files")
    elif involvement == "function":
        if not modules or not files or not functions:
            errors.append("function involvement requires modules, files and functions")
    else:
        errors.append("project involvement is unset or invalid")

    try:
        refs = validated_design_refs(root, proposal)
    except AuthorityError as exc:
        errors.append(str(exc))
        refs = []
    try:
        rejection = proposal_authority_rejection(root, proposal, shape)
        if rejection is not None and not allow_human_reconfirmation:
            scope = str(rejection.get("authority_scope") or "design-and-plan")
            errors.append(
                f"human rejection is active ({scope}); an explicit cockpit "
                "confirmation is required before this contract can regain authority"
            )
    except AuthorityError as exc:
        errors.append(str(exc))
    if errors:
        raise AuthorityError("; ".join(dict.fromkeys(errors)))
    return refs


_AUTHORITY_LINE = re.compile(r"(?mi)^_Authority:\s*[^_\r\n]+?_\s*$")


def design_intent_sha256(path: Path, override: bytes | None = None) -> str:
    """Identity of one design section with authority bookkeeping normalized.

    Confirmation can change ``Authority`` from agent-provisional to
    human-confirmed without changing the player experience being reviewed.  A
    veto therefore binds this normalized identity, as well as the exact file
    digest recorded for audit.
    """
    try:
        text = (override.decode("utf-8") if override is not None else path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise AuthorityError(f"cannot read reviewed design intent: {exc}") from exc
    canonical = design.canonical_design_text(text)
    canonical = _AUTHORITY_LINE.sub("_Authority: <reviewed-transition>_", canonical)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def reviewed_contract_sha256(
    root: Path,
    proposal: dict[str, Any],
    *,
    design_contents: dict[Path, bytes] | None = None,
) -> str:
    """Identity of intent independent of approval/status bookkeeping.

    A status flip, approval timestamp, revision note, or provisional-to-confirmed
    authority-line transition cannot make a rejected plan look new. Actual
    player-experience design, structure, baseline, or veto envelope changes do.
    """
    material = copy.deepcopy(proposal)
    for key in ("status", "approved_by", "approved_on", "approval_sha256", "revisions"):
        material.pop(key, None)
    authority = material.get("design_authority")
    if isinstance(authority, dict):
        authority.pop("authority", None)
    overrides = design_contents or {}
    normalized_refs: list[dict[str, Any]] = []
    refs = proposal.get("design_refs")
    for entry in refs if isinstance(refs, list) else []:
        if not isinstance(entry, dict):
            normalized_refs.append({"invalid": True})
            continue
        section, path = canonical_design_reference(root, entry.get("section"))
        normalized = copy.deepcopy(entry)
        normalized.pop("sha256", None)
        normalized["section"] = section
        normalized["design_intent_sha256"] = design_intent_sha256(
            path, overrides.get(path)
        )
        normalized_refs.append(normalized)
    material["design_refs"] = normalized_refs
    return hashlib.sha256(_canonical(material)).hexdigest()


_RECEIPT_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}")


def decision_receipt_sha256(decision: dict[str, Any]) -> str:
    """Digest the durable public half of a local cockpit receipt.

    This is tamper-evident binding, not a signature and not proof of a user's
    legal identity.  Repository write access remains the policy trust boundary.
    """
    material = copy.deepcopy(decision)
    material.pop("cockpit_receipt_sha256", None)
    return hashlib.sha256(_canonical(material)).hexdigest()


def cockpit_receipt_error(decision: dict[str, Any]) -> str:
    """Return why an authority event lacks valid portable cockpit evidence."""
    receipt_id = str(decision.get("cockpit_receipt_id") or "")
    fingerprint = str(decision.get("cockpit_reviewed_fingerprint") or "")
    supplied = str(decision.get("cockpit_receipt_sha256") or "")
    if _RECEIPT_ID.fullmatch(receipt_id) is None:
        return "cockpit receipt id is missing or malformed"
    if re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        return "cockpit reviewed fingerprint is missing or malformed"
    if re.fullmatch(r"[0-9a-f]{64}", supplied) is None:
        return "cockpit receipt digest is missing or malformed"
    if supplied != decision_receipt_sha256(decision):
        return "cockpit receipt digest does not match the authority event"
    return ""


def private_receipt_state(root: Path, decision: dict[str, Any]) -> dict[str, str]:
    """Check a present private receipt, while keeping cloned decisions portable.

    Missing private runtime is expected after clone/CI and is reported as
    ``portable-policy``. A present receipt is never called authentication: it
    is local audit evidence and fails closed when it contradicts the durable
    public event.
    """
    public_error = cockpit_receipt_error(decision)
    if public_error:
        return {"state": "invalid", "reason": public_error}
    try:
        try:
            import runtime_paths
        except ImportError:
            from tools import runtime_paths  # type: ignore[no-redef]
        paths = runtime_paths.resolve(root)
    except (OSError, ValueError, RuntimeError) as exc:
        return {
            "state": "portable-policy",
            "reason": f"private receipt location is unavailable: {exc}",
        }
    receipt_id = str(decision.get("cockpit_receipt_id") or "")
    receipt_path = paths.plan_decisions / f"{receipt_id}.json"
    if not receipt_path.exists():
        return {
            "state": "portable-policy",
            "reason": "private local receipt is absent (expected after clone or CI)",
        }
    if receipt_path.is_symlink() or not receipt_path.is_file():
        return {"state": "invalid", "reason": "private receipt is linked or not a file"}
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        return {"state": "invalid", "reason": f"private receipt is unreadable: {exc}"}
    if not isinstance(receipt, dict):
        return {"state": "invalid", "reason": "private receipt root is not an object"}
    expected_action = (
        "approve"
        if decision.get("authority_action") == "confirm"
        else "request-changes"
        if decision.get("authority_scope") == "plan-only"
        else "veto"
    )
    if receipt.get("action") != expected_action:
        return {"state": "invalid", "reason": "private receipt action disagrees"}
    if receipt.get("actor") != COCKPIT_ACTOR_LABEL:
        return {"state": "invalid", "reason": "private receipt actor label disagrees"}
    if receipt.get("reviewed_fingerprint") != decision.get(
        "cockpit_reviewed_fingerprint"
    ):
        return {
            "state": "invalid",
            "reason": "private receipt reviewed fingerprint disagrees",
        }
    receipts = receipt.get("authority_receipts")
    receipt_entries = receipts if isinstance(receipts, list) else []
    matched = any(
        isinstance(item, dict)
        and item.get("decision_id") == decision.get("id")
        and item.get("sha256") == decision.get("cockpit_receipt_sha256")
        for item in receipt_entries
    )
    if not matched:
        return {
            "state": "invalid",
            "reason": "private receipt has no matching authority-event cross-reference",
        }
    return {
        "state": "local-audit-matched",
        "reason": "private local audit receipt matches the portable event",
    }


def active_decisions(shape: dict[str, Any]) -> list[dict[str, Any]]:
    """Project receipt-bound human authority events not explicitly superseded."""
    raw = shape.get("decisions")
    decisions = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    superseded = {
        str(item.get("supersedes") or "")
        for item in decisions
        if item.get("decided_by") == "human"
        and item.get("authority_action") in ("confirm", "veto")
        and not cockpit_receipt_error(item)
    }
    return [
        item
        for item in decisions
        if item.get("decided_by") == "human"
        and item.get("authority_action") in ("confirm", "veto")
        and not cockpit_receipt_error(item)
        and str(item.get("id") or "") not in superseded
    ]


def active_design_vetoes(
    shape: dict[str, Any], design_intents: dict[str, str]
) -> list[dict[str, Any]]:
    """Return active vetoes for exact section intent, independent of its plan.

    Ledger order is authoritative for a given ``(section, intent)`` pair.  A
    later explicit confirmation clears a veto; plan-only events never do.
    """
    wanted = {
        (section, digest)
        for section, digest in design_intents.items()
        if section and re.fullmatch(r"[0-9a-f]{64}", digest)
    }
    state: dict[tuple[str, str], dict[str, Any] | None] = {
        key: None for key in wanted
    }
    raw = shape.get("decisions")
    events = raw if isinstance(raw, list) else []
    for item in events:
        if (
            not isinstance(item, dict)
            or item.get("decided_by") != "human"
            or item.get("authority_action") not in ("confirm", "veto")
            or cockpit_receipt_error(item)
            or item.get("authority_scope") != "design-and-plan"
        ):
            continue
        key = (
            str(item.get("docs_at") or ""),
            str(item.get("design_intent_sha256") or ""),
        )
        if key not in state:
            continue
        state[key] = item if item.get("authority_action") == "veto" else None
    return [item for item in state.values() if item is not None]


def proposal_design_intents(
    root: Path,
    proposal: dict[str, Any],
    *,
    design_contents: dict[Path, bytes] | None = None,
) -> dict[str, str]:
    """Return canonical section -> normalized current design intent identity."""
    overrides = design_contents or {}
    intents: dict[str, str] = {}
    refs = proposal.get("design_refs")
    for entry in refs if isinstance(refs, list) else []:
        if not isinstance(entry, dict):
            continue
        section, path = canonical_design_reference(root, entry.get("section"))
        intents[section] = design_intent_sha256(path, overrides.get(path))
    return intents


def proposal_authority_rejection(
    root: Path, proposal: dict[str, Any], shape: dict[str, Any]
) -> dict[str, Any] | None:
    """Project plan-only rejection or durable per-design-intent veto."""
    for event in active_decisions(shape):
        receipt = private_receipt_state(root, event)
        if receipt.get("state") == "invalid":
            raise AuthorityError(
                "present private cockpit receipt contradicts its portable event: "
                + str(receipt.get("reason") or "unknown mismatch")
            )
    vetoes = active_design_vetoes(shape, proposal_design_intents(root, proposal))
    if vetoes:
        return vetoes[0]
    reviewed = reviewed_contract_sha256(root, proposal)
    rejection = matching_rejection(shape, reviewed)
    if rejection is not None and rejection.get("authority_scope") == "plan-only":
        return rejection
    return rejection


def matching_rejection(
    shape: dict[str, Any], reviewed_contract_sha: str
) -> dict[str, Any] | None:
    """Return the latest active rejection for one exact reviewed plan."""
    match: dict[str, Any] | None = None
    for item in active_decisions(shape):
        if str(item.get("reviewed_contract_sha256") or "") != reviewed_contract_sha:
            continue
        if item.get("authority_action") == "confirm":
            match = None
        elif item.get("authority_action") == "veto":
            match = item
    return match


def exact_approval_state(
    root: Path, proposal: dict[str, Any], shape: dict[str, Any]
) -> dict[str, Any]:
    """Project whether stored approval is exact, current and still active."""
    reasons: list[str] = []
    try:
        refs = validated_design_refs(root, proposal)
    except AuthorityError as exc:
        refs = []
        reasons.append(str(exc))
    material_state = exact_approval_material(
        proposal,
        shape,
        {ref["section"]: ref["current_sha256"] for ref in refs},
    )
    reasons.extend(material_state["reasons"])
    stored = material_state["stored_sha256"]
    computed = material_state["computed_sha256"]
    receipt_states: list[dict[str, str]] = []
    refs_value = proposal.get("design_refs")
    refs_value = refs_value if isinstance(refs_value, list) else []
    decisions = active_decisions(shape)
    for entry in refs_value:
        if not isinstance(entry, dict):
            continue
        section = str(entry.get("section") or "")
        supplied = str(entry.get("sha256") or "")
        exact_events = [
            item
            for item in decisions
            if item.get("authority_action") == "confirm"
            and str(item.get("docs_at") or "") == section
            and str(item.get("design_sha256") or "") == supplied
            and str(item.get("proposal_sha256") or "") == stored
        ]
        if exact_events:
            receipt_states.append(private_receipt_state(root, exact_events[-1]))
    invalid_receipts = [
        state for state in receipt_states if state.get("state") == "invalid"
    ]
    for state in invalid_receipts:
        reasons.append(f"private cockpit receipt mismatch: {state.get('reason')}")
    if invalid_receipts:
        receipt_trust = "invalid"
    elif receipt_states and all(
        state.get("state") == "local-audit-matched" for state in receipt_states
    ):
        receipt_trust = "local-audit-matched"
    elif receipt_states:
        receipt_trust = "portable-policy"
    else:
        receipt_trust = "no-exact-authority-event"
    try:
        reviewed = reviewed_contract_sha256(root, proposal)
    except AuthorityError as exc:
        reviewed = ""
        reasons.append(str(exc))
    try:
        authority_rejection = proposal_authority_rejection(root, proposal, shape)
    except AuthorityError as exc:
        authority_rejection = None
        reasons.append(str(exc))
    if reviewed and authority_rejection is not None:
        scope = str(authority_rejection.get("authority_scope") or "design-and-plan")
        reasons.append(f"human rejection is active ({scope})")
    return {
        "approved": not reasons,
        "stored_sha256": stored,
        "computed_sha256": computed,
        "reviewed_contract_sha256": reviewed,
        "receipt_trust": receipt_trust,
        "receipt_reasons": [state.get("reason", "") for state in receipt_states],
        "reasons": reasons,
    }


def exact_approval_material(
    proposal: dict[str, Any],
    shape: dict[str, Any],
    design_digests: dict[str, str],
) -> dict[str, Any]:
    """Exact approval projection for current files or one historical Git tree."""
    reasons: list[str] = []
    if proposal.get("status") != "approved":
        reasons.append("proposal status is not approved")
    stored = str(proposal.get("approval_sha256") or "")
    computed = approval_sha256(proposal)
    if not stored or stored != computed:
        reasons.append("proposal approval digest is missing or stale")
    refs = proposal.get("design_refs")
    refs = refs if isinstance(refs, list) else []
    if not refs:
        reasons.append("proposal has no design references")
    decisions = active_decisions(shape)
    seen: set[str] = set()
    for entry in refs:
        if not isinstance(entry, dict):
            reasons.append("proposal has a malformed design reference")
            continue
        section = str(entry.get("section") or "")
        supplied = str(entry.get("sha256") or "")
        if not section or section in seen:
            reasons.append("proposal has an empty or duplicate design reference")
            continue
        seen.add(section)
        if design_digests.get(section) != supplied:
            reasons.append(f"{section} does not match the reviewed historical design")
            continue
        matching = [
            item
            for item in decisions
            if (
            item.get("authority_action") == "confirm"
            and str(item.get("docs_at") or "") == section
            and str(item.get("design_sha256") or "") == supplied
            and str(item.get("proposal_sha256") or "") == stored
            )
        ]
        if not matching:
            raw = shape.get("decisions")
            raw_events = raw if isinstance(raw, list) else []
            candidates = [
                item
                for item in raw_events if isinstance(item, dict)
                and item.get("decided_by") == "human"
                and item.get("authority_action") == "confirm"
                and str(item.get("docs_at") or "") == section
                and str(item.get("design_sha256") or "") == supplied
                and str(item.get("proposal_sha256") or "") == stored
            ]
            if candidates:
                reasons.append(
                    f"{section} exact confirmation has no valid cockpit receipt: "
                    f"{cockpit_receipt_error(candidates[-1]) or 'confirmation was superseded'}"
                )
            else:
                reasons.append(f"{section} has no active exact human confirmation")
    return {
        "approved": not reasons,
        "stored_sha256": stored,
        "computed_sha256": computed,
        "reasons": reasons,
    }
