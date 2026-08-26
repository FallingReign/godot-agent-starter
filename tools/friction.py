#!/usr/bin/env python3
"""Report authoring friction from git history, so it can be fixed with tooling.

Real developers automate a process once it becomes slow. An agent will not,
because it experiences a tedious task and a quick one identically: both are just
turns. The friction that matters is usually a MISSING SYSTEM rather than a slow
task -- six attempts to hand-author a token grid is not "that was tedious", it
is "this project needs a content authoring surface, and one is in scope anyway
because players will need it too".

What this can and cannot see, stated honestly:

  - The gate captures FINAL STATE. `.checklogs/` holds only the latest run per
    stage, has no history and is gitignored, so nothing in it can be compared
    against anything.
  - Git sees committed history. A current file rewritten across many commits is
    real signal. Deleted files are excluded because they cannot justify tooling
    work in the project that exists now. A file rewritten six times inside one
    turn leaves no trace at all.
  - So this under-reports. A clean report does not mean there was no friction;
    it means none reached the commit log.

Reports only. It never blocks: friction is a judgement call about whether a
tool is worth building, and that judgement is the human's.
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import project_context  # noqa: E402

CONTEXT = project_context.load_configured_context(ROOT)

# A file touched this many times since the baseline is being iterated on rather
# than written. Deliberately not tuned: the point is to surface candidates for a
# human to judge, not to be precise.
CHURN_THRESHOLD = 4
REVISION_THRESHOLD = 3


def git(*args: str) -> Tuple[int, str]:
    if not shutil.which("git"):
        return 1, ""
    try:
        r = subprocess.run(["git", "-C", str(ROOT), *args],
                           capture_output=True, text=True, timeout=30)
        return r.returncode, r.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def churn(since: str) -> Counter:
    """How many commits touched each current game file since `since`."""
    rng = f"{since}..HEAD" if since else "HEAD"
    code, out = git("log", "--format=%H", "--name-only", rng)
    if code != 0:
        return Counter()
    counts: Counter = Counter()
    for line in out.splitlines():
        line = line.strip().replace("\\", "/")
        if not line or len(line) == 40 and " " not in line:
            continue
        relative = CONTEXT.game_relative(line)
        if relative is not None and (CONTEXT.game_root / relative).is_file():
            counts[relative] += 1
    return counts


def proposal_revisions() -> int:
    """Revision entries on the current proposal.

    Several revisions is not failure -- building teaches you the plan was
    wrong, which is the loop working. Many revisions on one slice suggests the
    slice was too large to plan in one pass.
    """
    import json
    p = ROOT / "proposal.json"
    if not p.is_file():
        return 0
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    revs = d.get("revisions")
    return len(revs) if isinstance(revs, list) else 0


def baseline() -> str:
    import json
    p = ROOT / "proposal.json"
    if not p.is_file():
        return ""
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(d.get("baseline_sha", "") or "").strip()


def classify(path: str) -> str:
    """What kind of missing tool this churn might imply.

    Content churn is the interesting case, because an authoring surface for
    content is usually part of the shipped game anyway -- so building a crude
    one early pulls a required system forward rather than adding a detour.
    """
    p = path.lower()
    if path.startswith("content/") or p.endswith((".map", ".json", ".tres", ".txt")):
        return ("hand-authored content — a generator or in-game editor is"
                " usually in scope anyway")
    if p.startswith("tests/") or "/tests/" in p:
        return "test churn — fixtures or a builder may be missing"
    if p.endswith(".tscn") or p.endswith(".tres"):
        return "scene or resource churn — consider building it from a script"
    return "code churn — may just be normal iteration"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", default="",
                    help="git ref to measure from (default: proposal baseline_sha)")
    args = ap.parse_args()

    since = args.since or baseline()

    print("== authoring friction ==")
    if not shutil.which("git"):
        print("  git not found; nothing to measure")
        return 0
    if since:
        print(f"  since {since[:8]}")
    else:
        print("  no baseline_sha and no --since; measuring all history")

    findings: List[str] = []

    counts = churn(since)
    hot = [(f, n) for f, n in counts.most_common() if n >= CHURN_THRESHOLD]
    if hot:
        print(f"\n  files rewritten {CHURN_THRESHOLD}+ times:")
        for f, n in hot:
            print(f"    {n:>3}x  {f}")
            print(f"         {classify(f)}")
        findings.append(f"{len(hot)} file(s) with high churn")

    revs = proposal_revisions()
    if revs >= REVISION_THRESHOLD:
        print(f"\n  proposal has {revs} revisions")
        print("         the slice may be larger than one plan can cover;"
              " consider splitting")
        findings.append(f"{revs} proposal revisions")

    if not findings:
        print("\n  nothing above threshold.")
        print("  Note this only sees COMMITTED history. Repeated attempts"
              " inside a single turn")
        print("  leave no trace, so a clean report is not proof there was no"
              " friction.")
        return 0

    print("\n  " + "; ".join(findings))
    print("\n  If a human will do any of this more than a handful of times, ask"
          " whether the tool")
    print("  for it belongs in the game. Level layout, item tables, dialogue"
          " and ability")
    print("  definitions usually need an authoring surface that ships anyway."
          " The thinnest")
    print("  version is a generator or a @tool script, not a full editor.")
    print("  Skill: godot-tooling-friction")
    return 0


if __name__ == "__main__":
    sys.exit(main())
