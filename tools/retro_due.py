#!/usr/bin/env python3
"""Count unarchived slice notes and report whether a retrospective is due.

Deterministic. Invokes nothing, spends nothing, and never calls a model. The
gate runs this and prints one line; you decide whether to act on it.

That separation is the point. A trigger that fires a model call by itself is a
trigger that spends quota without being asked, and this kit has already produced
one runaway loop.

    python tools/retro_due.py            human-readable
    python tools/retro_due.py --json     machine-readable
    python tools/retro_due.py --quiet     exit 1 if due, no output
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
CORE_ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
import project_context  # noqa: E402
import runtime_paths  # noqa: E402

ROOT = project_context.load_active_context(CORE_ROOT).project_root
NOTES = ROOT / "docs" / "retro" / "notes"
ARCHIVE = ROOT / "docs" / "retro" / "archive"
DEFAULT_THRESHOLD = 10
IMMEDIATE_CONSEQUENCE_CODES = (
    "native-crash",
    "false-green",
    "unauthorized-provider-use",
    "irreversible-without-approval",
    "work-destroyed",
)
PROMPT_TRIGGER_CODES = (
    "wrong-built",
    "repeated-correction",
    "repeated-verification-failure",
)
_DECLARATION_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(consequence|retro[_ -]?trigger|trigger)\s*:\s*"
    r"([a-z][a-z0-9_-]*(?::[a-z][a-z0-9_-]*)?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

def threshold() -> int:
    cfg = runtime_paths.load_config(ROOT)
    value = cfg.get("note_threshold", DEFAULT_THRESHOLD)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise runtime_paths.RuntimeConfigError(
            "kit.config.json note_threshold must be a positive integer"
        )
    return value


def unarchived() -> list[Path]:
    if not NOTES.is_dir():
        return []
    return sorted(p for p in NOTES.glob("*.md") if p.name.lower() != "readme.md")


def signals(notes: list[object]) -> dict:
    """Classify only explicit closed-code declarations from slice testimony."""
    immediate: dict[str, set[str]] = {
        code: set() for code in IMMEDIATE_CONSEQUENCE_CODES}
    prompt: dict[str, set[str]] = {code: set() for code in PROMPT_TRIGGER_CODES}
    warnings: list[dict] = []
    for number, note in enumerate(notes, 1):
        if isinstance(note, Path):
            label = note.name
            try:
                text = note.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                warnings.append({"code": "retro_note_unreadable", "note": label})
                continue
        elif isinstance(note, dict):
            label = str(note.get("path") or note.get("name") or f"note-{number}")
            text = note.get("text")
            if not isinstance(text, str):
                warnings.append({"code": "retro_note_unreadable", "note": label})
                continue
        else:
            continue
        for declaration, raw_code in _DECLARATION_RE.findall(text):
            code = raw_code.lower().replace("_", "-")
            if code.startswith("prompt:"):
                code = code.split(":", 1)[1]
                declaration = "trigger"
            if code in immediate and declaration.lower() == "consequence":
                immediate[code].add(label)
            elif code in prompt:
                prompt[code].add(label)
            else:
                warnings.append({
                    "code": "unknown_retro_signal",
                    "note": label,
                    "value": code[:128],
                })
    return {
        "immediate_consequences": [
            {"code": code, "notes": sorted(immediate[code])}
            for code in IMMEDIATE_CONSEQUENCE_CODES if immediate[code]
        ],
        "prompt_triggers": [
            {"code": code, "notes": sorted(prompt[code])}
            for code in PROMPT_TRIGGER_CODES if prompt[code]
        ],
        "warnings": sorted(
            warnings, key=lambda item: (item["code"], item.get("note", ""),
                                        item.get("value", ""))),
    }


def verification_consequence() -> dict | None:
    """Promote an observed native verifier crash without classifying prose."""
    try:
        paths = runtime_paths.resolve(ROOT)
        payload = json.loads(paths.verification_latest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, runtime_paths.RuntimeConfigError):
        return None
    if not isinstance(payload, dict):
        return None
    diagnostics = payload.get("diagnostics")
    native = (
        diagnostics.get("native_crashes")
        if isinstance(diagnostics, dict) else []
    )
    if payload.get("failure_class") != "native-crash" and not native:
        return None
    run_id = str(payload.get("id") or "latest")
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "-", run_id)[:96] or "latest"
    return {"code": "native-crash", "notes": [f"verification:{safe_id}"]}


def state() -> dict:
    notes = unarchived()
    t = threshold()
    archived = len(list(ARCHIVE.glob("*.md"))) if ARCHIVE.is_dir() else 0
    classified = signals(list(notes))
    runtime_consequence = verification_consequence()
    if runtime_consequence:
        immediate_by_code = {
            str(item.get("code")): set(item.get("notes") or [])
            for item in classified["immediate_consequences"]
            if isinstance(item, dict)
        }
        immediate_by_code.setdefault(runtime_consequence["code"], set()).update(
            runtime_consequence["notes"]
        )
        classified["immediate_consequences"] = [
            {"code": code, "notes": sorted(immediate_by_code[code])}
            for code in IMMEDIATE_CONSEQUENCE_CODES if immediate_by_code.get(code)
        ]
    routine_due = len(notes) >= t
    immediate_due = bool(classified["immediate_consequences"])
    prompt_due = bool(classified["prompt_triggers"])
    level = ("immediate" if immediate_due else "prompt" if prompt_due
             else "routine" if routine_due else "none")
    return {
        "unarchived": len(notes),
        "threshold": t,
        "remaining": max(t - len(notes), 0),
        "progress": round(min(len(notes) / t, 1.0), 4),
        "due": routine_due or immediate_due or prompt_due,
        "trigger_level": level,
        "immediate_consequences": classified["immediate_consequences"],
        "prompt_triggers": classified["prompt_triggers"],
        "warnings": classified["warnings"],
        "archived": archived,
        "notes": [p.name for p in notes],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Is a retrospective due?")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true", help="no output, exit 1 if due")
    a = ap.parse_args()

    try:
        s = state()
    except runtime_paths.RuntimeConfigError as exc:
        print(f"retro status unavailable: {exc}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps(s, indent=2))
        return 0
    if a.quiet:
        return 1 if s["due"] else 0

    if s["due"]:
        print(f"retrospective due ({s['trigger_level']}): "
              f"{s['unarchived']} unarchived note(s), threshold {s['threshold']}")
        for item in s["immediate_consequences"]:
            print(f"  immediate: {item['code']} ({', '.join(item['notes'])})")
        for item in s["prompt_triggers"]:
            print(f"  prompt: {item['code']} ({', '.join(item['notes'])})")
        print("  kit retro run")
        if s["trigger_level"] == "routine":
            print("  or lower note_threshold in kit.config.json")
    else:
        print(f"{s['unarchived']}/{s['threshold']} slice note(s); "
              f"{s['remaining']} more before a retrospective is due")
    return 0


if __name__ == "__main__":
    sys.exit(main())
