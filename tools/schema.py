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
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

# ---------------------------------------------------------------------------
# Field spec: name -> (type, required, one-line meaning)
# Types: "str", "bool", "list", "dict", "enum:a|b|c", "list:<spec-name>"
# ---------------------------------------------------------------------------

FUNC = {
    "file":      ("str", True,  "path of the file this function lives in"),
    "signature": ("str", True,  "full signature, e.g. func move(delta: float) -> void"),
    "why":       ("str", True,  "why this function is being added or changed"),
    "action":    ("enum:new|modify", True, "new function, or a change to one that exists"),
    "module":    ("str", False, "owning module path, if any"),
}

FILE_ = {
    "path":   ("str", True, "path relative to repo root, e.g. src/scripts/player.gd"),
    "action": ("enum:new|modify", True, "new file, or a change to one that exists"),
    "why":    ("str", True, "why this file is being created or touched in this slice"),
    "module": ("str", False, "owning module path, if any"),
}

MODULE = {
    "path":          ("str", True,  "directory path, e.g. src/scripts/logic"),
    "role":          ("str", True,  "what this module is responsible for"),
    "why":           ("str", True,  "why this module is being created or changed now"),
    "action":        ("enum:new|modify", True, "new module, or a change to one that exists"),
    "may_depend_on": ("list", False, "module paths this one may import from"),
    # Adopted from what an agent invented unprompted, because it is a better
    # field than anything I specified: it states what crosses the type
    # boundary, which is exactly what the boundary rule asks for.
    "boundary_data": ("str", False, "what enters and leaves this module, by type"),
}

CONSIDERED = {
    "path":    ("str", True, "existing file or module that could have served"),
    "why_not": ("str", True, "why extending it was rejected"),
}

DESIGN_REF = {
    "section": ("str", True, "path under docs/design/, e.g. experience/movement.md"),
    "why":     ("str", True, "the end state this work serves, not the feature above it"),
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
                True, "which warning is being accepted"),
    "slice":   ("str", True,  "slice this applies to -- an ack does not carry forward"),
    "why":     ("str", True,  "why building without it is the right call here"),
    "by":      ("str", True,  "who accepted it"),
    "on":      ("str", True,  "ISO date"),
}

PROPOSAL = {
    "slice":               ("str", True,  "slice name, e.g. slice-2-rotation"),
    "status":              ("enum:draft|approved", True, "draft until the human approves"),
    "approved_by":         ("str", False, "who approved it"),
    "approved_on":         ("str", False, "ISO date of approval"),
    "baseline_sha":        ("str", False, "commit the slice starts from"),
    "experience":          ("dict:EXPERIENCE", True, "what the player experiences"),
    "design_refs":         ("list:DESIGN_REF", False, "design sections this work descends from"),
    "mockup":              ("dict:MOCKUP", False, "cheap preview, when cheaper than the real thing"),
    "considered_existing": ("list:CONSIDERED", False, "existing code weighed before creating new"),
    "modules":             ("list:MODULE", False, "modules created or changed"),
    "files":               ("list:FILE_", False, "files created or changed"),
    "functions":           ("list:FUNC", False, "functions created or changed"),
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
    "note":         ("str", False, "the human's own framing, kept verbatim"),
    "supersedes":   ("str", False, "id of the decision this replaces"),
    "alternatives": ("str", False, "what else was weighed"),
}

QUESTION = {
    "id":        ("str", True,  "stable slug"),
    "question":  ("str", True,  "the open question"),
    "blocks":    ("str", True,  "what work this blocks"),
    "raised":    ("str", False, "ISO date raised, so staleness is visible"),
    "raised_by": ("str", False, "who raised it"),
    "options":   ("list", False, "candidate answers, if any are known"),
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
    "MODULE": MODULE, "CONSIDERED": CONSIDERED, "DESIGN_REF": DESIGN_REF,
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
        elif typ == "list":
            if not isinstance(val, list):
                errs.append(f"{where}.{key}: expected a list")
        elif typ == "dict":
            if not isinstance(val, dict):
                errs.append(f"{where}.{key}: expected an object")
        elif typ == "bool":
            if not isinstance(val, bool):
                errs.append(f"{where}.{key}: expected true or false")
        else:  # str
            if not isinstance(val, str):
                errs.append(f"{where}.{key}: expected a string")
                continue
            if required and not val.strip():
                errs.append(f"{where}.{key}: is empty -- {meaning}")
            elif val.strip().lower() in PLACEHOLDERS:
                errs.append(f"{where}.{key}: is a placeholder ({val!r}). "
                            f"Write the real value or remove the entry.")


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

    _check(obj, spec_name, path.name, errs, warns)
    return (errs, warns)


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
        print(f"\n{bad} problem(s). Run 'python tools/schema.py --describe proposal' "
              "for the field list.")
        return 1
    print("artefacts valid")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
