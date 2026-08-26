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
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOTES = ROOT / "docs" / "retro" / "notes"
ARCHIVE = ROOT / "docs" / "retro" / "archive"
DEFAULT_THRESHOLD = 10

sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_paths  # noqa: E402


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


def state() -> dict:
    notes = unarchived()
    t = threshold()
    archived = len(list(ARCHIVE.glob("*.md"))) if ARCHIVE.is_dir() else 0
    return {
        "unarchived": len(notes),
        "threshold": t,
        "due": len(notes) >= t,
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
        print(f"retrospective due: {s['unarchived']} unarchived note(s), threshold {s['threshold']}")
        print("  kit retro run")
        print("  or lower note_threshold in kit.config.json")
    else:
        remaining = s["threshold"] - s["unarchived"]
        print(f"{s['unarchived']}/{s['threshold']} slice note(s); {remaining} more before a retrospective is due")
    return 0


if __name__ == "__main__":
    sys.exit(main())
