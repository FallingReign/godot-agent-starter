#!/usr/bin/env python3
"""Validate the kit's JSON artefacts against a declared shape.

Why this exists: proposal.json shipped as empty arrays with no item shape
documented anywhere a machine could read. An agent filling it invented
reasonable key names -- change/why/responsibility instead of
action/purpose/role -- and the renderer silently dropped everything it did not
recognise. Five files each had a written justification; the plan showed none of
them, and conformance skipped every file because `action` was absent.

A wrong key must fail at write time, naming the correct one. Anything softer
means the artefact and the reader disagree in silence, and every tool
downstream reasons about a document whose contents were partly discarded.

This module is the single source of truth for artefact keys. The renderer, the
gate and the template all derive from it, so they cannot drift apart.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Field spec: name -> (type, required, one-line meaning)
# Types: "str", "sha256", "gitsha", "bool", "list", "dict", "enum:a|b|c",
# "list:<spec-name>"
# ---------------------------------------------------------------------------

FUNC = {
    "file":      ("str", True,  "path of the file this function lives in"),
    "class_scope": ("str", False,
                    "dot-separated inner-class scope, e.g. Inventory.Entry"),
    "signature": ("str", True,  "full signature, e.g. func move(delta: float) -> void"),
    "why":       ("str", True,  "why this function is being added or changed"),
    "action":    ("enum:new|modify|delete", True,
                  "new function, changed function, or function removed from the file"),
    "module":    ("str", False, "owning module path, if any"),
}

FILE_ = {
    "path":   ("str", True, "path relative to the configured game root, e.g. scripts/player.gd"),
    "action": ("enum:new|modify|delete", True,
               "new file, changed file, or file removed from the game root"),
    "why":    ("str", True, "why this file is being created or touched in this slice"),
    "module": ("str", False, "owning module path, if any"),
}

MODULE = {
    "path":          ("str", True,  "directory under the configured game root, e.g. scripts/logic"),
    "role":          ("str", True,  "what this module is responsible for"),
    "why":           ("str", True,  "why this module is being created or changed now"),
    "action":        ("enum:new|modify|delete", True,
                      "new module, changed module, or module removed from the game root"),
    "may_depend_on": ("strlist", True,
                      "complete module paths this one may import from; [] means none"),
    # Adopted from what an agent invented unprompted, because it is a better
    # field than anything I specified: it states what crosses the type
    # boundary, which is exactly what the boundary rule asks for.
    "boundary_data": ("str", True,
                      "what enters and leaves this module by type, or that none crosses"),
}

CONSIDERED = {
    "path":    ("str", True, "existing file or module that could have served"),
    "why_not": ("str", True, "why extending it was rejected"),
}

SCOPE = {
    "path":   ("str", True,
               "coarse game-root-relative file or directory boundary for hands-off work"),
    "kind":   ("enum:file|directory", True,
               "whether path is one exact file or a directory boundary"),
    "action": ("enum:new|modify|delete", True,
               "new, changed or removed authored input within this boundary"),
    "why":    ("str", True, "why this coarse boundary is needed for the slice"),
}

DESIGN_REF = {
    "section": ("str", True,
                "repository path under docs/design/, e.g. docs/design/experience/movement.md"),
    "why":     ("str", True, "the end state this work serves, not the feature above it"),
    "sha256":  ("sha256", True, "canonical digest of the cited design section"),
}

DESIGN_AUTHORITY = {
    "authority":   ("enum:human-confirmed|agent-provisional", True,
                    "whether a human confirmed the design or it remains agent-provisional"),
    "authored_by": ("enum:human|agent", True, "who wrote the cited design"),
    "confidence":  ("enum:very-high|high|medium|low", True,
                    "confidence in the stated design; provisional implementation requires very-high"),
}

REVERSIBILITY = {
    "state":         ("enum:reversible|go-no-go", True,
                      "whether the current implementation can still be safely vetoed"),
    "veto_scope":    ("str", True, "what can be removed or changed if the design is vetoed"),
    "hard_to_undo":  ("str", True, "the commitment that would become expensive to reverse"),
    "next_go_no_go": ("str", True, "the next point that requires explicit human approval"),
}

REVISION = {
    "on":         ("str", True,  "ISO date"),
    "what":       ("str", True,  "what changed in the plan"),
    "why":        ("str", True,  "what building it revealed"),
    "decided_by": ("enum:human|agent", True, "who made the call"),
}

EXPERIENCE = {
    "player_does": ("str", True,  "what the player does, in their words"),
    "feels_like":  ("str", True,  "how it should feel"),
    "camera":      ("str", False, "camera behaviour"),
    "controls":    ("str", False, "input mapping"),
    "not_this":    ("str", True,  "the wrong reading to rule out"),
}

MOCKUP = {
    "svg":          ("str", False, "inline SVG, shape markup only"),
    "caption":      ("str", False, "what the mockup shows"),
    "not_possible": ("str", False, "why no preview was made"),
}

# An acknowledgement is a human consciously accepting a warning. It is not a
# suppression: the warning still prints, the plan still shows it, and the reason
# the human gave is recorded next to it. This exists because the alternatives are
# both worse -- a blocking check gets satisfied by writing something (we watched
# push_error get deleted and a stale mockup carried forward with a new caption),
# and a warning with no acknowledgement path gets scrolled past until it is noise.
ACK = {
    "warning": ("enum:no-design-refs|shallow-ancestry|unbound-tunables|tier-suspect",
                True, "which legacy warning is being recorded; it never grants design authority"),
    "slice":   ("str", True,  "slice this applies to -- an ack does not carry forward"),
    "why":     ("str", True,  "why the warning was recorded"),
    "by":      ("str", True,  "who accepted it"),
    "on":      ("str", True,  "ISO date"),
}

PROPOSAL = {
    "slice":               ("str", True,  "slice name, e.g. slice-2-rotation"),
    "status":              ("enum:draft|recorded|approved", True,
                            "draft waits; recorded is reversible hands-off work; approved is human approval"),
    "approved_by":         ("str", False, "who approved it"),
    "approved_on":         ("str", False, "ISO date of approval"),
    "approval_sha256":     ("sha256", False,
                            "canonical digest of the exact approved proposal contract"),
    "baseline_sha":        ("gitsha", False, "exact commit the slice starts from"),
    "experience":          ("dict:EXPERIENCE", True, "what the player experiences"),
    "design_refs":         ("list:DESIGN_REF", False,
                            "required with structure: exact design sections this work descends from"),
    "design_authority":    ("dict:DESIGN_AUTHORITY", False,
                            "required with structure: authorship, authority and confidence"),
    "reversibility":       ("dict:REVERSIBILITY", False,
                            "required with structure: the safe veto envelope and next go/no-go"),
    "mockup":              ("dict:MOCKUP", False, "cheap preview, when cheaper than the real thing"),
    "considered_existing": ("list:CONSIDERED", False, "existing code weighed before creating new"),
    "scope":               ("list:SCOPE", False,
                             "coarse reversible boundaries used only at hands-off involvement"),
    "modules":             ("list:MODULE", False, "modules created, changed or deleted"),
    "files":               ("list:FILE_", False, "files created, changed or deleted"),
    "functions":           ("list:FUNC", False, "functions created, changed or deleted"),
    "revisions":           ("list:REVISION", False, "how the plan changed mid-build"),
    "acknowledged":        ("list:ACK", False, "warnings the human consciously accepted"),
}

# Field names below match docs and the shape stage exactly. I first wrote this
# spec with my own preferred names (why/on) and it contradicted both -- which
# would have made the schema itself a third source of truth for artefact keys.
DECISION = {
    "id":           ("str", True,  "stable slug, e.g. world-structure"),
    "question":     ("str", True,  "the question as it was actually asked"),
    "answer":       ("str", True,  "what was decided"),
    "because":      ("str", True,  "one line on what forced this decision now"),
    "date":         ("str", True,  "ISO date"),
    "decided_by":   ("enum:human|agent", True, "who decided"),
    "revisit_if":   ("str", True,  "what would invalidate this, or 'permanent'"),
    "docs_at":      ("str", False, "path under docs/design/ if it graduated"),
    "design_sha256": ("sha256", False,
                      "canonical digest of docs_at when this decision confirmed it"),
    "design_intent_sha256": ("sha256", False,
                              "authority-normalized player-experience intent bound by the event"),
    "proposal_sha256": ("sha256", False,
                        "canonical digest of the exact proposal confirmed with this design"),
    "authority_action": ("enum:confirm|veto", False,
                         "whether this human event grants or supersedes exact plan authority"),
    "reviewed_contract_sha256": ("sha256", False,
                                  "substantive design-and-plan identity independent of lifecycle labels"),
    "authority_scope": ("enum:design-and-plan|plan-only", False,
                         "whether a veto withdraws design authority too, or only the reviewed plan"),
    "cockpit_receipt_id": ("str", False,
                            "durable cross-reference to the local cockpit receipt"),
    "cockpit_reviewed_fingerprint": ("sha256", False,
                                       "exact cockpit page fingerprint the operator acted on"),
    "cockpit_receipt_sha256": ("sha256", False,
                                "tamper-evident digest of this authority event; not a user signature"),
    "note":         ("str", False, "the human's own framing, kept verbatim"),
    "supersedes":   ("str", False, "id of the decision this replaces"),
    "alternatives": ("str", False, "what else was weighed"),
}

QUESTION = {
    "id":                  ("str", True,  "stable slug"),
    "question":            ("str", True,  "the open question"),
    "blocks":              ("str", True,  "what work this blocks"),
    "raised":              ("str", False, "ISO date raised, so staleness is visible"),
    "raised_by":           ("str", False, "who raised it"),
    "options":             ("list", False, "candidate answers, if any are known"),
    "related_slices":      ("strlist", False,
                             "exact proposal slice ids where this question is promoted"),
    "related_design_refs": ("strlist", False,
                             "exact docs/design section paths where this question is promoted"),
}

DIRECTION = {
    "id":          ("str", True,  "stable slug"),
    "heading":     ("str", True,  "the known heading, one line"),
    "accommodate": ("str", True,  "what today's work must leave room for"),
    # A negative constraint is often the more useful half: it stops the
    # accommodation quietly becoming an implementation.
    "not_yet":     ("str", False, "what must NOT be built yet, despite the above"),
    "added":       ("str", False, "ISO date added"),
}

SHAPE = {
    "name":        ("str", True,  "project name"),
    "pitch":       ("str", True,  "one or two sentences on what the game is"),
    "involvement": ("enum:hands-off|module|file|function", True,
                    "how much the human approves before code"),
    "decisions":   ("list:DECISION", False, "settled, append-only"),
    "questions":   ("list:QUESTION", False, "open, deleted when answered"),
    "direction":   ("list:DIRECTION", False, "known constraints on today's work"),
    "docs_at":     ("str", False, "unused, kept for older files"),
}

SPECS: Dict[str, Dict[str, Tuple[str, bool, str]]] = {
    "ACK": ACK, "PROPOSAL": PROPOSAL, "SHAPE": SHAPE, "FUNC": FUNC, "FILE_": FILE_,
    "MODULE": MODULE, "CONSIDERED": CONSIDERED, "SCOPE": SCOPE,
    "DESIGN_REF": DESIGN_REF,
    "DESIGN_AUTHORITY": DESIGN_AUTHORITY, "REVERSIBILITY": REVERSIBILITY,
    "REVISION": REVISION, "EXPERIENCE": EXPERIENCE, "MOCKUP": MOCKUP,
    "DECISION": DECISION, "QUESTION": QUESTION, "DIRECTION": DIRECTION,
}

PLACEHOLDERS = {"tbd", "todo", "unknown", "n/a", "na", "xxx", "?", "-",
                "unknown yet", "not sure", "tba", "pending", "fixme"}


def _near(key: str, valid: List[str]) -> str:
    """Suggest the intended key. An agent that wrote `change` needs to be told
    `action`, not merely that `change` is wrong."""
    k = key.lower().replace("_", "")
    for v in valid:
        if v.lower().replace("_", "") == k:
            return v
    # Substring either way catches change/action? no. Catches why/why_not,
    # considered_existing/existing_considered, purpose/purposes.
    for v in valid:
        a, b = k, v.lower().replace("_", "")
        if a in b or b in a:
            return v
    # Common renamings we have actually seen an agent invent.
    alias = {"change": "action", "purpose": "why", "reason": "why",
             "responsibility": "role", "desc": "why", "description": "why",
             "existingconsidered": "considered_existing", "name": "path",
             "func": "signature", "sig": "signature", "date": "on",
             "author": "decided_by", "revisit": "revisit_if"}
    return alias.get(k, "")


def _check(obj: Any, spec_name: str, where: str,
           errs: List[str], warns: List[str]) -> None:
    spec = SPECS[spec_name]
    if not isinstance(obj, dict):
        errs.append(f"{where}: expected an object, got {type(obj).__name__}")
        return

    for key in obj:
        if key in spec:
            continue
        # Keys starting with _ are notes to the reader, not data. The template
        # uses them to document its own shape, which is the whole reason an
        # agent had to guess at key names in the first place.
        if key.startswith("_"):
            continue
        hint = _near(key, list(spec))
        msg = f"{where}: unknown key '{key}'"
        if hint:
            msg += f" -- did you mean '{hint}'?"
        else:
            msg += f" (valid: {', '.join(sorted(spec))})"
        errs.append(msg)

    for key, (typ, required, meaning) in spec.items():
        if key not in obj:
            if required:
                errs.append(f"{where}: missing required key '{key}' -- {meaning}")
            continue
        val = obj[key]

        if typ.startswith("list:"):
            if not isinstance(val, list):
                errs.append(f"{where}.{key}: expected a list")
                continue
            for i, item in enumerate(val):
                _check(item, typ.split(":", 1)[1], f"{where}.{key}[{i}]", errs, warns)
        elif typ.startswith("dict:"):
            if not isinstance(val, dict):
                errs.append(f"{where}.{key}: expected an object")
                continue
            _check(val, typ.split(":", 1)[1], f"{where}.{key}", errs, warns)
        elif typ.startswith("enum:"):
            allowed = typ.split(":", 1)[1].split("|")
            if not isinstance(val, str) or val not in allowed:
                if required or (isinstance(val, str) and val.strip()):
                    errs.append(f"{where}.{key}: must be one of "
                                f"{', '.join(allowed)} (got {val!r})")
        elif typ == "strlist":
            if not isinstance(val, list):
                errs.append(f"{where}.{key}: expected a list of strings")
                continue
            for i, item in enumerate(val):
                if not isinstance(item, str) or not item.strip():
                    errs.append(
                        f"{where}.{key}[{i}]: expected a non-empty string"
                    )
                elif item.strip().lower() in PLACEHOLDERS:
                    errs.append(
                        f"{where}.{key}[{i}]: is a placeholder ({item!r})"
                    )
        elif typ == "list":
            if not isinstance(val, list):
                errs.append(f"{where}.{key}: expected a list")
        elif typ == "dict":
            if not isinstance(val, dict):
                errs.append(f"{where}.{key}: expected an object")
        elif typ == "bool":
            if not isinstance(val, bool):
                errs.append(f"{where}.{key}: expected true or false")
        elif typ == "sha256":
            if not isinstance(val, str) or not re.fullmatch(r"[0-9a-f]{64}", val):
                errs.append(f"{where}.{key}: expected a canonical lowercase SHA-256 digest")
        elif typ == "gitsha":
            if not isinstance(val, str) or not re.fullmatch(r"[0-9a-f]{40,64}", val):
                errs.append(f"{where}.{key}: expected an exact lowercase Git commit id")
        else:  # str
            if not isinstance(val, str):
                errs.append(f"{where}.{key}: expected a string")
                continue
            if required and not val.strip():
                errs.append(f"{where}.{key}: is empty -- {meaning}")
            elif val.strip().lower() in PLACEHOLDERS:
                errs.append(f"{where}.{key}: is a placeholder ({val!r}). "
                            f"Write the real value or remove the entry.")


def _proposal_semantics(
    obj: Any,
    errs: List[str],
    warns: List[str],
    *,
    root: Path | None = None,
) -> None:
    """Validate authority transitions that cannot be expressed by field types.

    `recorded` deliberately is not a weaker spelling of approval. It exists for
    hands-off work that stays inside a documented reversible envelope. The gate
    separately checks the configured involvement level and the referenced
    design documents; this function keeps the artefact internally honest.
    """
    if not isinstance(obj, dict):
        return

    try:
        from tools import authored_scope, gd_signature, proposal_authority
    except ImportError:  # direct ``python tools/schema.py`` execution
        import authored_scope  # type: ignore[no-redef]
        import gd_signature  # type: ignore[no-redef]
        import proposal_authority  # type: ignore[no-redef]

    status = obj.get("status")
    has_structure = any(
        isinstance(obj.get(key), list) and bool(obj[key])
        for key in ("scope", "modules", "files", "functions")
    )
    refs = obj.get("design_refs")
    authority = obj.get("design_authority")
    reversibility = obj.get("reversibility")

    seen_scope: set[tuple[str, str]] = set()
    for index, item in enumerate(obj.get("scope") or []):
        if not isinstance(item, dict) or any(key.startswith("_") for key in item):
            continue
        raw = item.get("path")
        kind = item.get("kind")
        normalized = (
            authored_scope.normalize_directory(raw)
            if kind == "directory" and isinstance(raw, str)
            else authored_scope.normalize_relative(raw)
            if isinstance(raw, str)
            else None
        )
        if isinstance(raw, str) and normalized != raw:
            errs.append(
                f"proposal.json.scope[{index}].path: expected a canonical "
                "game-root-relative path"
            )
        identity = (str(kind or ""), str(raw or ""))
        if identity in seen_scope:
            errs.append(
                f"proposal.json.scope[{index}]: duplicate {identity[0]} boundary {identity[1]!r}"
            )
        seen_scope.add(identity)

    seen_modules: set[str] = set()
    for index, item in enumerate(obj.get("modules") or []):
        if not isinstance(item, dict) or any(key.startswith("_") for key in item):
            continue
        raw = item.get("path")
        if isinstance(raw, str) and authored_scope.normalize_directory(raw) != raw:
            errs.append(
                f"proposal.json.modules[{index}].path: expected a canonical "
                "game-root-relative module path"
            )
        if isinstance(raw, str) and raw in seen_modules:
            errs.append(f"proposal.json.modules[{index}].path: duplicate module {raw!r}")
        elif isinstance(raw, str):
            seen_modules.add(raw)
        dependencies = item.get("may_depend_on")
        if isinstance(dependencies, list):
            for dep_index, dependency in enumerate(dependencies):
                if (
                    isinstance(dependency, str)
                    and authored_scope.normalize_directory(dependency) != dependency
                ):
                    errs.append(
                        f"proposal.json.modules[{index}].may_depend_on[{dep_index}]: "
                        "expected a canonical module path"
                    )

    seen_files: set[str] = set()
    for index, item in enumerate(obj.get("files") or []):
        if not isinstance(item, dict) or any(key.startswith("_") for key in item):
            continue
        raw = item.get("path")
        if isinstance(raw, str) and authored_scope.normalize_relative(raw) != raw:
            errs.append(
                f"proposal.json.files[{index}].path: expected a canonical "
                "game-root-relative file path"
            )
        if isinstance(raw, str) and raw in seen_files:
            errs.append(f"proposal.json.files[{index}].path: duplicate file {raw!r}")
        elif isinstance(raw, str):
            seen_files.add(raw)
        declared_module = item.get("module")
        if (
            isinstance(declared_module, str)
            and declared_module
            and authored_scope.normalize_directory(declared_module) != declared_module
        ):
            errs.append(
                f"proposal.json.files[{index}].module: expected a canonical module path"
            )

    seen_functions: set[tuple[str, str]] = set()
    for index, item in enumerate(obj.get("functions") or []):
        if not isinstance(item, dict) or any(key.startswith("_") for key in item):
            continue
        raw_file = item.get("file")
        if (
            isinstance(raw_file, str)
            and (
                authored_scope.normalize_relative(raw_file) != raw_file
                or not raw_file.endswith(".gd")
            )
        ):
            errs.append(
                f"proposal.json.functions[{index}].file: expected a canonical .gd path"
            )
        signature = item.get("signature")
        class_scope = item.get("class_scope")
        if class_scope not in (None, "") and (
            not isinstance(class_scope, str)
            or re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*",
                class_scope,
            ) is None
        ):
            errs.append(
                f"proposal.json.functions[{index}].class_scope: expected dot-separated class identifiers"
            )
        if isinstance(signature, str):
            try:
                parsed = gd_signature.parse_proposal_signature(signature)
            except gd_signature.SignatureError as exc:
                errs.append(f"proposal.json.functions[{index}].signature: {exc}")
            else:
                if signature.strip() != parsed.canonical:
                    errs.append(
                        f"proposal.json.functions[{index}].signature: use canonical "
                        f"{parsed.canonical!r}"
                    )
                scoped_name = (
                    f"{class_scope}.{parsed.name}" if class_scope else parsed.name
                )
                identity = (str(raw_file or ""), scoped_name)
                if identity in seen_functions:
                    errs.append(
                        f"proposal.json.functions[{index}]: duplicate function "
                        f"{identity[0]}::{identity[1]}"
                    )
                seen_functions.add(identity)
        declared_module = item.get("module")
        if (
            isinstance(declared_module, str)
            and declared_module
            and authored_scope.normalize_directory(declared_module) != declared_module
        ):
            errs.append(
                f"proposal.json.functions[{index}].module: expected a canonical module path"
            )

    for index, item in enumerate(refs if isinstance(refs, list) else []):
        if not isinstance(item, dict):
            continue
        section = item.get("section")
        if root is None:
            continue
        try:
            proposal_authority.canonical_design_reference(root, section)
        except proposal_authority.AuthorityError as exc:
            errs.append(f"proposal.json.design_refs[{index}].section: {exc}")

    if has_structure or status == "recorded":
        if not isinstance(obj.get("baseline_sha"), str) or not obj["baseline_sha"].strip():
            errs.append(
                "proposal.json.baseline_sha: implementation requires the exact starting commit"
            )
        if not isinstance(refs, list) or not refs:
            errs.append(
                "proposal.json.design_refs: implementation requires at least one "
                "design section; an acknowledgement is not design authority"
            )
        if not isinstance(authority, dict):
            errs.append(
                "proposal.json.design_authority: required for implementation so "
                "authorship and authority cannot be inferred"
            )
        if not isinstance(reversibility, dict):
            errs.append(
                "proposal.json.reversibility: required for implementation so the "
                "veto envelope and next go/no-go are explicit"
            )

    acknowledgements = obj.get("acknowledged")
    if isinstance(acknowledgements, list) and any(
        isinstance(item, dict) and item.get("warning") == "no-design-refs"
        for item in acknowledgements
    ):
        warns.append(
            "proposal.json: legacy no-design-refs acknowledgement retained for "
            "history only; it does not authorize implementation"
        )

    approved_fields = [
        key for key in ("approved_by", "approved_on", "approval_sha256")
        if obj.get(key)
    ]
    if status != "approved" and approved_fields:
        errs.append(
            "proposal.json: approved_by/approved_on are valid only when status is approved"
        )
    if status == "approved":
        for key in ("approved_by", "approved_on", "approval_sha256"):
            if not isinstance(obj.get(key), str) or not obj[key].strip():
                errs.append(f"proposal.json.{key}: required when status is approved")

    authority_value = authority.get("authority") if isinstance(authority, dict) else None
    authored_by = authority.get("authored_by") if isinstance(authority, dict) else None
    confidence = authority.get("confidence") if isinstance(authority, dict) else None

    if authority_value == "agent-provisional" and authored_by != "agent":
        errs.append(
            "proposal.json.design_authority.authored_by: agent-provisional design "
            "must disclose authored_by as agent"
        )
    if status == "recorded" and authority_value == "agent-provisional" \
            and confidence != "very-high":
        errs.append(
            "proposal.json.design_authority.confidence: recorded agent-provisional "
            "work requires exactly 'very-high'"
        )
    if status == "approved" and authority_value != "human-confirmed":
        errs.append(
            "proposal.json.design_authority.authority: approved work requires "
            "human-confirmed design"
        )

    reversibility_state = (
        reversibility.get("state") if isinstance(reversibility, dict) else None
    )
    if status == "recorded" and reversibility_state != "reversible":
        errs.append(
            "proposal.json.reversibility.state: recorded work must remain reversible; "
            "go-no-go work requires explicit approval"
        )


def _shape_semantics(obj: Any, errs: List[str], warns: List[str]) -> None:
    try:
        from tools import proposal_authority
    except ImportError:
        import proposal_authority  # type: ignore[no-redef]
    if not isinstance(obj, dict):
        return
    questions = obj.get("questions")
    if isinstance(questions, list):
        for index, question in enumerate(questions):
            if not isinstance(question, dict):
                continue
            where = f"project.shape.json.questions[{index}]"
            for key in ("related_slices", "related_design_refs"):
                values = question.get(key)
                if not isinstance(values, list):
                    continue
                normalised = [
                    item.strip() for item in values
                    if isinstance(item, str) and item.strip()
                ]
                if len(normalised) != len(set(normalised)):
                    errs.append(f"{where}.{key}: duplicate relations are not allowed")
            design_refs = question.get("related_design_refs")
            if isinstance(design_refs, list):
                for relation_index, relation in enumerate(design_refs):
                    if not isinstance(relation, str) or not relation.strip():
                        continue
                    canonical = relation.strip()
                    parts = canonical.split("/")
                    if (
                        canonical != relation
                        or "\\" in canonical
                        or not canonical.startswith("docs/design/")
                        or not canonical.endswith(".md")
                        or ".." in parts
                    ):
                        errs.append(
                            f"{where}.related_design_refs[{relation_index}]: must be a "
                            "canonical docs/design/*.md path"
                        )
    if not isinstance(obj.get("decisions"), list):
        return
    for index, decision in enumerate(obj["decisions"]):
        if not isinstance(decision, dict):
            continue
        docs_at = str(decision.get("docs_at", "") or "").strip()
        digest = str(decision.get("design_sha256", "") or "").strip()
        proposal_digest = str(decision.get("proposal_sha256", "") or "").strip()
        authority_action = str(decision.get("authority_action", "") or "").strip()
        reviewed_digest = str(
            decision.get("reviewed_contract_sha256", "") or ""
        ).strip()
        authority_scope = str(decision.get("authority_scope", "") or "").strip()
        intent_digest = str(
            decision.get("design_intent_sha256", "") or ""
        ).strip()
        where = f"project.shape.json.decisions[{index}]"
        if digest and not docs_at:
            errs.append(f"{where}.design_sha256: requires docs_at")
        elif docs_at and not digest:
            warns.append(
                f"{where}: legacy docs_at is not bound to an exact design digest; "
                "do not treat it as new approval"
            )
        if proposal_digest and (not docs_at or not digest):
            errs.append(
                f"{where}.proposal_sha256: requires docs_at and design_sha256"
            )
        if authority_action:
            if not proposal_digest:
                errs.append(
                    f"{where}.authority_action: requires proposal_sha256"
                )
            if decision.get("decided_by") != "human":
                errs.append(
                    f"{where}.authority_action: exact authority transitions must be human"
                )
            if not intent_digest:
                errs.append(
                    f"{where}.design_intent_sha256: required with authority_action"
                )
            receipt_error = proposal_authority.cockpit_receipt_error(decision)
            if receipt_error:
                errs.append(f"{where}.cockpit_receipt_sha256: {receipt_error}")
        elif proposal_digest:
            errs.append(
                f"{where}.authority_action: required with proposal_sha256"
            )
        elif digest:
            warns.append(
                f"{where}: legacy design confirmation is not bound to an exact proposal; "
                "re-review it in the cockpit before delivery"
            )
        if reviewed_digest and not authority_action:
            errs.append(
                f"{where}.reviewed_contract_sha256: requires authority_action"
            )
        if authority_scope and not reviewed_digest:
            errs.append(
                f"{where}.authority_scope: requires reviewed_contract_sha256"
            )
        if reviewed_digest and not authority_scope:
            errs.append(
                f"{where}.authority_scope: required with reviewed_contract_sha256"
            )
        if authority_action == "confirm" and authority_scope == "plan-only":
            errs.append(
                f"{where}.authority_scope: confirmation must cover design-and-plan"
            )


def validate(path: Path, spec_name: str) -> Tuple[List[str], List[str]]:
    """Return (errors, warnings) for one artefact."""
    errs: List[str] = []
    warns: List[str] = []
    if not path.exists():
        return ([], [f"{path.name} not found"])
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return ([f"{path.name}: not valid JSON -- {exc}"], [])

    # An untouched template is not an error. It is the state before any work.
    if spec_name == "PROPOSAL" and not str(obj.get("slice", "")).strip():
        return ([], [f"{path.name}: no slice named yet (untouched template)"])
    if spec_name == "SHAPE" and not str(obj.get("name", "")).strip():
        return ([], [f"{path.name}: project not named yet"])

    return validate_object(
        obj,
        spec_name,
        root=path.resolve(strict=False).parent,
        source_name=path.name,
    )


def validate_object(
    obj: Any,
    spec_name: str,
    *,
    root: Path | None = None,
    source_name: str | None = None,
) -> Tuple[List[str], List[str]]:
    """Validate an in-memory artefact using the exact file validator contract."""
    errs: List[str] = []
    warns: List[str] = []
    name = source_name or ("proposal.json" if spec_name == "PROPOSAL" else "project.shape.json")
    _check(obj, spec_name, name, errs, warns)
    if spec_name == "PROPOSAL":
        _proposal_semantics(obj, errs, warns, root=root)
    elif spec_name == "SHAPE":
        _shape_semantics(obj, errs, warns)
    return errs, warns


def describe(spec_name: str, indent: int = 0) -> List[str]:
    """Human-readable field list, so docs are generated from the spec."""
    out: List[str] = []
    pad = " " * indent
    for key, (typ, required, meaning) in SPECS[spec_name].items():
        req = "required" if required else "optional"
        out.append(f"{pad}{key} ({req}) -- {meaning}")
        if typ.startswith(("list:", "dict:")):
            out.extend(describe(typ.split(":", 1)[1], indent + 4))
    return out


def main(argv: List[str]) -> int:
    root = Path(__file__).resolve().parent.parent
    if "--describe" in argv:
        which = argv[argv.index("--describe") + 1] if len(argv) > argv.index("--describe") + 1 else "PROPOSAL"
        print("\n".join(describe(which.upper())))
        return 0

    targets = [(root / "proposal.json", "PROPOSAL"),
               (root / "project.shape.json", "SHAPE")]
    bad = 0
    for path, spec in targets:
        errs, warns = validate(path, spec)
        for w in warns:
            print(f"note: {w}")
        for e in errs:
            print(f"error: {e}")
        bad += len(errs)
    if bad:
        print(f"\n{bad} problem(s). Run 'kit schema describe proposal' "
              "for the field list.")
        return 1
    print("artefacts valid")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
