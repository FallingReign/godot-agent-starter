#!/usr/bin/env python3
"""Score a retrospective findings file on cost and effort, kept separate.

The retrospective agent attributes facts: which sessions, which digest
citations, which mechanical markers, what severity, what the fix touches.
This script turns those facts into two numbers -- never one -- and ranks by
the first. Splitting the two matters because an agent asked to rank its own
findings will reach for "high" and "critical" -- exactly the vocabulary that
let five architectural findings pass unranked while `ls -la` failing under
PowerShell, evidenced three times and fixable in one line, was never surfaced.

    python tools/retro_rank.py                          rank the newest
                                                         docs/retro/*-findings.md
    python tools/retro_rank.py docs/retro/2026-08-16-findings.md
    python tools/retro_rank.py --dry-run FILE            print, do not rewrite

Cost and effort answer different questions and are never collapsed into one
number by division. `cost` answers "what did this cost the human" -- the
ranking question. `effort` answers "how cheap is this to fix" -- the
sequencing question. An earlier version divided cost by a fix-size bucket to
rank by return on effort; that meant a bucket boundary could flip an
ordering, and fix_lines is always a model estimate ("a rough line count"),
so dividing by it implied a precision the estimate does not have. Ranking is
by cost alone. Effort is shown beside it and drives only the `quick_win` flag.

    cost   = (mechanical_capped + human_turns * HUMAN_TURN_WEIGHT)
             * distinct_sessions * SEVERITY_MULT[severity] * staleness_mult
    effort = FIX_EFFORT_BUCKET(fix_lines)                  # 1, 2, 4 or 8

`mechanical_capped = min(mechanical_cost, human_turns * HUMAN_TURN_WEIGHT)`.
Mechanical evidence amplifies a finding that already has human attribution;
capped at 2x so it can never rank a finding above one with more attributed
human turns on mechanical noise alone (see `HUMAN_TURN_WEIGHT` below for the
proof this holds).

A finding with zero attributed human turns is not ranked. It is moved to an
"Observations" section instead, because a mechanical recurrence nobody ever
reacted to is evidence the human does not care, not evidence of a problem --
see AGENTS.md's principle on this. Findings are never deleted, only reordered
and re-sectioned, so nothing here is a second chance to lose a citation.

A citation pointing at a session run under the kit-builder persona is
excluded from human_turns and mechanical scoring (see `session_persona`).
Kit-builder is the persona that *implements* a retro's own findings, so its
task prompts restate the defect in more detail than any organic complaint
ever would -- scoring those turns lets the retrospective re-discover and
rank what it just fixed. Game-builder and unattributed sessions are real
evidence and stay in scope.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path, PurePosixPath

TOOLS = Path(__file__).resolve().parent
CORE_ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
import project_context  # noqa: E402
import runtime_paths  # noqa: E402
import retro_ledger  # noqa: E402
import session_digest  # noqa: E402  (needs sys.path set first)
import session_evidence  # noqa: E402

ROOT = project_context.load_active_context(CORE_ROOT).project_root
RETRO_DIR = ROOT / "docs" / "retro"
DEFERRED_FILE = RETRO_DIR / "deferred.json"
_RUNTIME = runtime_paths.resolve(ROOT)
SESSION_EVIDENCE_DIR = _RUNTIME.session_evidence
BOARD_STATE_FILE = _RUNTIME.board_state
DECISION_LOCK_DIR = _RUNTIME.runtime / "retro" / "ledger-locks"

# --------------------------------------------------------------- tunables
#
# HUMAN_TURN_WEIGHT: cost of one human-attributed turn. With the mechanical
# cap in place (see `mechanical_capped` above), mechanical evidence can never
# contribute more than human_turns * HUMAN_TURN_WEIGHT on its own -- it can at
# most double a finding's cost, never create or lead one. That removes the
# earlier reason for a large weight (beating the single biggest observed LOOP
# count under division, which no longer applies since there is no divisor).
# What is left is picking a plain, readable unit for "one attributed human
# turn." 10 is chosen only for that readability: distinct_sessions and
# severity are small integers/multipliers, so a two-digit per-turn cost keeps
# totals easy to sanity-check by eye against the raw fields.
HUMAN_TURN_WEIGHT = 10.0

# SEVERITY_MULT: a closed set of four, matching the four the persona is
# allowed to write. `none` is the arithmetic baseline. The other three exist
# because a rare, catastrophic failure breaks pure recurrence arithmetic --
# a `git reset --hard` that discarded work happened exactly once in this
# project's history and was its single most expensive event; scoring by
# recurrence alone would put it near zero. Multipliers are spaced roughly
# 5x apart: enough that severity can move a finding several tiers even on a
# single citation, not so much that severity alone always wins regardless of
# cost.
SEVERITY_MULT = {
    "work-destroyed": 25.0,
    "false-green": 12.0,
    "wrong-built": 5.0,
    "none": 1.0,
}

# FIX_EFFORT_BUCKET: how cheap a fix is, kept entirely separate from cost.
# Bucketed rather than the raw fix_lines number for the same reason a raw
# number was wrong as a divisor: fix_lines is always a model estimate ("a
# rough line count"), so treating it as an exact integer implies a precision
# it does not have. Four buckets, spaced roughly 2-4x apart, are enough
# granularity to distinguish "quick win" (<=10) from everything else without
# pretending 24 lines and 31 lines are meaningfully different fixes.
FIX_EFFORT_BUCKET = [
    (10, 1),
    (50, 2),
    (200, 4),
    (float("inf"), 8),
]

# QUICK_WIN_FLOOR: the cost a finding must clear, on top of having the
# cheapest effort bucket (1), to be flagged a quick win. Set above the cost
# of a single, unrepeated, uncorroborated human turn (1 * HUMAN_TURN_WEIGHT =
# 10) precisely so a finding nobody cared much about does not become a quick
# win merely by being cheap: 20 is the cost of the smallest thing that is
# more than a single passing mention -- two turns in one session, or one
# turn corroborated across two sessions (1 * 10 * 2 sessions = 20). Below
# that, cheapness alone does not earn the flag.
QUICK_WIN_FLOOR = 20.0

# A finding deferred once and recurring in later evidence is the strongest
# case available: "you deferred this and it cost you again." Boost it rather
# than have it re-derive the same score from scratch.
DEFERRED_RECURRENCE_BOOST = 2.0

# A finding whose only evidence predates the last commit touching every file
# it names may already be paid for. Discount rather than drop it silently --
# a human should see and confirm that, not have it vanish from the file.
STALE_EVIDENCE_DISCOUNT = 0.25


def fix_effort_bucket(fix_lines: int) -> int:
    """Bucketed effort for a rough fix-size estimate. See FIX_EFFORT_BUCKET."""
    n = max(fix_lines, 1)
    for ceiling, bucket in FIX_EFFORT_BUCKET:
        if n <= ceiling:
            return bucket
    return FIX_EFFORT_BUCKET[-1][1]


def is_quick_win(cost: float, effort: int) -> bool:
    """Cheapest effort bucket and cost clears the floor. See QUICK_WIN_FLOOR."""
    return effort == FIX_EFFORT_BUCKET[0][1] and cost >= QUICK_WIN_FLOOR


CITE_RE = re.compile(r"S(\d+)(?::H(\d+))?")
COUNT_RE = re.compile(r"x(\d+)\b")
FINDING_RE = re.compile(r"^## Finding: (.+)$", re.M)
# The generated "## Observations" section is regenerated fresh by rewrite()
# on every run; it must bound the preceding finding's block the same way the
# next "## Finding:" heading does, or its boilerplate text gets swept into
# that finding's body as prose and then duplicates once per run forever.
SECTION_BOUNDARY_RE = re.compile(r"^## (?:Finding: .+|Observations)\s*$", re.M)
FIELD_RE = re.compile(r"^([a-z_]+):\s*(.*)$", re.M)
MECH_SESSION_RE = re.compile(r"\(S(\d+)\)\s*$")

# The retrospective's own subagent-selection event. Present at, or very near,
# the top of a session log when that session was invoked with an explicit
# persona (`--agent kit-builder`, `--agent retrospective`). A session with
# no such line was run with no persona flag, which AGENTS.md defines as the
# game-builder default -- that default and true no-persona sessions both
# remain in scope; only kit-builder is excluded. workspace.yaml's own `name`
# field was considered and rejected: every session in this repo's history
# carries the same workspace name ("github/cli", the terminal's own repo),
# so it cannot distinguish personas at all.
PERSONA_LINE_RE = re.compile(r'"type"\s*:\s*"subagent\.selected"')
AGENT_NAME_RE = re.compile(r'"agentName"\s*:\s*"([^"]+)"')
KIT_BUILDER_PERSONA = "kit-builder"


# The three diagnosis sections a finding's free-form body now carries:
# problem (cause, not symptom), proposal (the concrete experiment) and
# measure (how to tell it worked). Bold-headed prose, deliberately never
# `key: value` lines -- a stray `key:` line at the top of a body is stripped
# as leftover schema noise by parse_findings' annotation cleanup, and these
# three must survive a rewrite. Because they live in `body`, which this
# module only ever carries through untouched, nothing here or in `score()`
# ever reads them: that is what keeps a diagnosis from being able to raise a
# finding's cost the way a banned "confidence" or "impact" field could.
SECTION_RE = re.compile(r"\*\*(Problem|Proposal|Measure)\*\*\s*(?:[—-]{1,2}|:)?\s*", re.I)
SNAPSHOT_RE = re.compile(r"^session_snapshot:\s*(\S+)\s*$", re.M)


def extract_sections(body: str) -> dict[str, str]:
    """Split a finding's body into problem/proposal/measure prose, best-effort.

    A finding written before this schema existed (or an Observation with only
    a problem statement) simply has empty strings for what it lacks -- the
    caller renders that gracefully rather than inventing an answer.
    """
    matches = list(SECTION_RE.finditer(body))
    out = {"problem": "", "proposal": "", "measure": ""}
    for i, m in enumerate(matches):
        key = m.group(1).lower()
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        out[key] = body[start:end].strip()
    return out


def _all_findings() -> dict[str, dict]:
    """Every finding across every docs/retro/*-findings.md, keyed by normalised title."""
    out: dict[str, dict] = {}
    for path in sorted(RETRO_DIR.glob("*-findings.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        _, findings = parse_findings(text)
        for f in findings:
            out[normalise_title(f["title"])] = f
    return out


def implemented_findings() -> list[dict]:
    """Accepted findings whose dispatched run exited zero and whose fix_files
    were touched by a commit since acceptance -- derived from git and the
    board's own run log, never from the dispatched agent's own report, which
    is the wrong witness to whether it actually changed those files.

    Returns dicts with `title`, `measure` and `since` (the newest touching
    commit date), so a caller can show what to watch for alongside the fact
    that it shipped.
    """
    accepted = retro_ledger.load_entries(
        RETRO_DIR / "accepted.json", label="docs/retro/accepted.json"
    )
    if not accepted:
        return []

    try:
        board_state = (json.loads(BOARD_STATE_FILE.read_text(encoding="utf-8"))
                       if BOARD_STATE_FILE.exists() else {})
    except (OSError, ValueError):
        board_state = {}
    runs = board_state.get("runs", [])

    findings_by_title = _all_findings()
    out: list[dict] = []
    for e in accepted:
        if e.get("resolved_at"):
            continue  # already a confirmed win via absence-from-evidence
        key = normalise_title(e.get("finding", ""))
        f = findings_by_title.get(key)
        if not f or not f.get("fix_files"):
            continue
        finished = any(
            normalise_title(r.get("finding", "")) == key and r.get("status") == "completed"
            for r in runs
        )
        if not finished:
            continue
        accepted_date = e.get("date", "")
        newest = ""
        all_touched = True
        for ff in f["fix_files"]:
            last_commit = sh("git", "log", "-1", "--format=%ad", "--date=short", "--", ff)
            if not last_commit or (accepted_date and last_commit <= accepted_date):
                all_touched = False
                break
            newest = max(newest, last_commit)
        if all_touched:
            out.append({
                "title": f["title"],
                "measure": extract_sections(f["body"]).get("measure", ""),
                "since": newest,
            })
    return out


def sh(*args: str) -> str:
    try:
        r = subprocess.run(args, cwd=str(ROOT), capture_output=True, text=True, timeout=15)
        return r.stdout.strip()
    except Exception:
        return ""


def _list_field(raw: str) -> list[str]:
    """Parse a `[a; b; c]` field. Tolerant of a missing bracket or trailing separator.

    Semicolon-separated, not comma-separated: a mechanical entry is often a
    shell command with its own commas in it (`--only schema,shape,conformance`
    is one LOOP entry, not three), so comma cannot double as the list
    delimiter without corrupting exactly the evidence this file exists to
    carry precisely.
    """
    raw = raw.strip()
    if raw.startswith("["):
        raw = raw[1:]
    if raw.endswith("]"):
        raw = raw[:-1]
    return [p.strip() for p in raw.split(";") if p.strip()]


def _int_field(raw: str, default: int = 1) -> int:
    m = re.search(r"\d+", raw)
    return int(m.group()) if m else default


def _float_field(raw: str) -> float | None:
    try:
        return float(raw.strip())
    except ValueError:
        return None


def _bool_field(raw: str) -> bool:
    return raw.strip().lower() in ("true", "yes", "1")


def _sessions_from_text(*blobs: str) -> set[str]:
    out: set[str] = set()
    for blob in blobs:
        for m in CITE_RE.finditer(blob):
            out.add(f"S{m.group(1)}")
    return out


def normalise_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")


def session_persona(log: Path) -> str:
    """Persona a session ran under, from its own `subagent.selected` event.

    That event is written once, at or near the start of a session's log,
    whenever the session was invoked with an explicit persona. Read only the
    first handful of lines -- session logs run to megabytes, and the event
    this looks for is never buried deep in one. No event found means no
    persona flag was used, which is the game-builder default, not a signal
    this session is unattributed noise.
    """
    try:
        with log.open(encoding="utf-8", errors="replace") as f:
            for _ in range(20):
                line = f.readline()
                if not line:
                    break
                if PERSONA_LINE_RE.search(line):
                    m = AGENT_NAME_RE.search(line)
                    if m:
                        return m.group(1)
    except OSError:
        pass
    return "unattributed"


# ------------------------------------------------------------- parsing

def parse_findings(text: str) -> tuple[str, list[dict]]:
    """Split a findings file into its header prose and a list of finding dicts.

    Each finding dict carries the raw fact fields plus everything else in the
    block (prose, proposed change) untouched in "body".
    """
    matches = list(FINDING_RE.finditer(text))
    boundaries = list(SECTION_BOUNDARY_RE.finditer(text))
    header = text[: matches[0].start()] if matches else text
    findings: list[dict] = []
    snapshot_match = SNAPSHOT_RE.search(header)
    session_snapshot = snapshot_match.group(1) if snapshot_match else ""
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        start = m.end()
        # Bound this finding's block at whichever comes next: another
        # "## Finding:" heading, or a generated "## Observations" heading.
        next_boundaries = [b.start() for b in boundaries if b.start() >= start]
        end = next_boundaries[0] if next_boundaries else len(text)
        block = text[start:end]

        fields: dict[str, str] = {}
        body_start = 0
        for fm in FIELD_RE.finditer(block):
            key = fm.group(1)
            if key not in ("sessions", "human_turns", "mechanical", "recurs",
                           "severity", "fix_files", "fix_lines", "cost", "effort"):
                continue
            fields[key] = fm.group(2)
            body_start = max(body_start, fm.end())
        body = block[body_start:]
        # Transient annotation lines (`# ...`) are rewritten fresh every run,
        # never preserved as body prose -- otherwise a note from one run and
        # a fresh note from the next both survive, and the same annotation
        # accumulates once per run forever. They sit, with no blank line
        # before them, directly after the fields (see render_finding), ending
        # at the first blank line, which is where the human-written prose
        # for this finding begins.
        # A schema change (e.g. `value:` replaced by `cost:`/`effort:`) leaves
        # the old field's rendered line behind too: FIELD_RE still matches it,
        # but its key is no longer in the allow-list above, so body_start
        # never advances past it and it is swept into body as if it were
        # prose. Strip those the same way, as long as they sit in the same
        # leading run with no blank line separating them from real prose.
        body_lines = body.split("\n")
        last_annotation = -1
        annotations: list[str] = []
        for idx, line in enumerate(body_lines):
            stripped = line.strip()
            if not stripped:
                continue  # blank lines can separate annotations from each other
            if stripped.startswith("#"):
                # Captured (not just skipped) so a reader of the parsed
                # finding -- retro_html.py, in particular -- can show *why* a
                # cost was capped or discounted without recomputing it.
                annotations.append(stripped.lstrip("#").strip())
                last_annotation = idx
                continue
            if FIELD_RE.match(stripped):
                last_annotation = idx
                continue
            break  # first real prose line -- stop, whatever blanks came before it
        body = "\n".join(body_lines[last_annotation + 1:]).lstrip("\n")

        findings.append({
            "title": title,
            "sessions": _list_field(fields.get("sessions", "")),
            "human_turns": _list_field(fields.get("human_turns", "")),
            "mechanical": _list_field(fields.get("mechanical", "")),
            "recurs": _bool_field(fields.get("recurs", "")),
            "severity": fields.get("severity", "none").strip() or "none",
            "fix_files": _list_field(fields.get("fix_files", "")),
            "fix_lines": _int_field(fields.get("fix_lines", "1")),
            # cost/effort/notes below are the *last rendered* values, only
            # present once retro_rank.py has scored this file at least once.
            # rewrite() always recomputes fresh rather than trusting these --
            # they exist on the parsed dict purely for a read-only consumer
            # (retro_html.py) that wants to display the persisted numbers
            # without a second scoring pass that could disagree with them.
            "cost": _float_field(fields.get("cost", "")),
            "effort": _int_field(fields["effort"]) if "effort" in fields else None,
            "notes": annotations,
            "body": body,
            "session_snapshot": session_snapshot,
        })
    return header, findings


# ------------------------------------------------------------ validation

def _snapshot_citations(relative: str) -> tuple[dict[str, dict], dict[str, str], list[str]]:
    warnings: list[str] = []
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or any(ord(char) < 32 for char in relative)):
        return {}, {}, [f"unsafe session snapshot path: {relative!r}"]
    lexical = PurePosixPath(relative)
    if (lexical.is_absolute() or not lexical.parts
            or any(part in ("", ".", "..") for part in lexical.parts)
            or ":" in lexical.parts[0]):
        return {}, {}, [f"unsafe session snapshot path: {relative!r}"]
    path = ROOT.joinpath(*lexical.parts)
    try:
        root_resolved = ROOT.resolve(strict=True)
        allowed = SESSION_EVIDENCE_DIR.resolve(strict=False)
        allowed.relative_to(root_resolved)
        path.resolve(strict=False).relative_to(allowed)
        path.relative_to(SESSION_EVIDENCE_DIR)
    except (OSError, RuntimeError, ValueError):
        return {}, {}, [f"unsafe session snapshot path: {relative!r}"]
    cursor = ROOT
    for part in lexical.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            return {}, {}, [f"symlinked session snapshot path: {relative!r}"]
    if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
        return {}, {}, [f"session snapshot is not content-addressed: {relative!r}"]
    try:
        content = path.read_bytes()
        manifest = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return {}, {}, [f"cannot read session snapshot {relative!r}: {exc}"]
    actual = hashlib.sha256(content).hexdigest()
    if path.stem != actual:
        return {}, {}, [f"session snapshot hash mismatch: {relative!r}"]
    if not isinstance(manifest, dict) or session_evidence.canonical_bytes(manifest) != content:
        return {}, {}, [f"session snapshot is not canonical: {relative!r}"]
    current = (manifest.get("schema") == session_evidence.SCHEMA
               and manifest.get("kind") == session_evidence.KIND)
    legacy = (manifest.get("schema") == session_evidence.LEGACY_SCHEMA
              and manifest.get("kind") == session_evidence.LEGACY_KIND)
    if not current and not legacy:
        return {}, {}, [f"unexpected session snapshot kind: {relative!r}"]
    canonical_repository = os.path.normcase(os.path.abspath(ROOT)).replace("\\", "/").rstrip("/")
    repository_matches = False
    if current and isinstance(manifest.get("repository"), dict):
        repository_matches = (
            manifest["repository"].get("scope_id")
            == session_evidence.repository_scope(ROOT)["scope_id"]
        )
    elif legacy:
        repository_matches = manifest.get("repository") == canonical_repository
    if not repository_matches:
        return {}, {}, [f"session snapshot repository provenance mismatch: {relative!r}"]
    sessions = manifest.get("sessions")
    if not isinstance(sessions, list):
        return {}, {}, [f"invalid session records in {relative!r}"]
    digests: dict[str, dict] = {}
    personas: dict[str, str] = {}
    for ordinal, session in enumerate(sessions, 1):
        if (not isinstance(session, dict) or session.get("ordinal") != ordinal
                or not isinstance(session.get("session_id"), str)
                or not session.get("session_id")
                or not isinstance(session.get("evidence"), dict)):
            return {}, {}, [f"invalid session records in {relative!r}"]
        tag = f"S{ordinal}"
        evidence = session["evidence"]
        messages = evidence.get("human_messages")
        if not isinstance(messages, list):
            return {}, {}, [f"invalid human-message records in {relative!r}"]
        human: list[str] = []
        for number, message in enumerate(messages, 1):
            expected = f"{session['session_id']}:H{number}"
            if (not isinstance(message, dict) or message.get("citation") != expected
                    or not isinstance(message.get("text"), str)):
                return {}, {}, [f"invalid human-message records in {relative!r}"]
            human.append(message["text"])
        digests[tag] = {
            "id": session.get("session_id", ""),
            "provider": session.get("provider", "copilot"),
            "started": session.get("started", ""),
            "updated": session.get("updated", ""),
            "model": evidence.get("model", ""),
            "human": human,
            "loops": evidence.get("loops") or {},
            "rewrites": evidence.get("rewrites") or {},
            "gate_fails": evidence.get("gate_fails") or {},
            "warnings": session.get("warnings") or [],
            "sources": session.get("sources") or {},
        }
        personas[tag] = evidence.get("persona") or "unattributed"
    return digests, personas, warnings


def citation_report(findings: list[dict]) -> tuple[dict[str, dict], list[str], dict[str, str]]:
    """Resolve citations against the immutable snapshot bound to the report.

    Legacy reports without a snapshot remain readable but are not reinterpreted
    against today's mutable session discovery order.
    """
    warnings: list[str] = []
    digests: dict[str, dict] = {}
    personas: dict[str, str] = {}
    snapshots = {f.get("session_snapshot", "") for f in findings
                 if f.get("session_snapshot")}
    if len(snapshots) == 1:
        digests, personas, snapshot_warnings = _snapshot_citations(next(iter(snapshots)))
        warnings.extend(snapshot_warnings)
    elif len(snapshots) > 1:
        warnings.append("findings from multiple evidence snapshots must be resolved per file")
    else:
        warnings.append(
            "legacy findings have no immutable session snapshot; citations were not resolved"
        )
    for f in findings:
        for cite in f["human_turns"]:
            m = CITE_RE.fullmatch(cite.strip())
            if not m or m.group(2) is None:
                warnings.append(f"{f['title']!r}: {cite!r} is not a S<n>:H<n> citation")
                continue
            tag = f"S{m.group(1)}"
            hnum = int(m.group(2))
            d = digests.get(tag)
            if d is None:
                continue  # already warned above
            if d.get("error"):
                warnings.append(f"{f['title']!r}: {cite!r} points at an unreadable session log")
            elif hnum < 1 or hnum > len(d.get("human", [])):
                warnings.append(
                    f"{f['title']!r}: {cite!r} has no H{hnum} "
                    f"(session has {len(d.get('human', []))} human message(s))"
                )
    return digests, warnings, personas


def is_stale(finding: dict, digests: dict[str, dict]) -> bool:
    """True if every citation's session predates the last commit to every fix file.

    Best-effort: a fix file with no git history (proposed, not yet created)
    cannot be judged stale, so the finding is not flagged.
    """
    if not finding["fix_files"]:
        return False
    cite_sessions = finding["sessions"] or sorted(
        _sessions_from_text(" ".join(finding["human_turns"]), " ".join(finding["mechanical"]))
    )
    started_dates = []
    for tag in cite_sessions:
        d = digests.get(tag)
        if d and d.get("started"):
            started_dates.append(d["started"][:10])
    if not started_dates:
        return False
    newest_evidence = max(started_dates)
    for f in finding["fix_files"]:
        last_commit = sh("git", "log", "-1", "--format=%ad", "--date=short", "--", f)
        if not last_commit:
            continue  # file has no history here; cannot judge
        if last_commit > newest_evidence:
            return True
    return False


# ------------------------------------------------------------------ score

def _citation_session_tag(cite: str) -> str | None:
    m = CITE_RE.match(cite.strip())
    return f"S{m.group(1)}" if m else None


def _mechanical_session_tag(entry: str) -> str | None:
    m = MECH_SESSION_RE.search(entry)
    return f"S{m.group(1)}" if m else None


def _exclude_kit_builder(items: list[str], personas: dict[str, str], tag_of) -> tuple[list[str], list[str]]:
    """Split a citation list into (kept, excluded) by session persona.

    A citation whose session persona is unknown (not in `personas`, e.g. an
    unresolved S<n>) is kept -- citation_report already warns about that
    separately, and this is not the place to silently drop it too.
    """
    kept, excluded = [], []
    for item in items:
        tag = tag_of(item)
        if tag is not None and personas.get(tag) == KIT_BUILDER_PERSONA:
            excluded.append(item)
        else:
            kept.append(item)
    return kept, excluded


def recency_key(finding: dict, digests: dict[str, dict]) -> str:
    """Most recent session start date the finding cites, for tiebreaking."""
    sessions = finding["sessions"] or sorted(
        _sessions_from_text(" ".join(finding["human_turns"]), " ".join(finding["mechanical"]))
    )
    dates = [digests[s]["started"][:10] for s in sessions if s in digests and digests[s].get("started")]
    return max(dates) if dates else ""


def score(
    finding: dict, digests: dict[str, dict], personas: dict[str, str]
) -> tuple[float, int, list[str]]:
    """Return (cost, effort, notes). Cost ranks; effort never divides into it."""
    notes: list[str] = []

    kept_turns, excluded_turns = _exclude_kit_builder(
        finding["human_turns"], personas, _citation_session_tag
    )
    for cite in excluded_turns:
        notes.append(f"excluded {cite}: kit-builder session, not attributable friction")
    human_turns = len(kept_turns)
    effort = fix_effort_bucket(finding["fix_lines"])
    if human_turns == 0:
        reason = "no human_turns cited"
        if excluded_turns:
            reason += " after excluding kit-builder session(s)"
        return 0.0, effort, notes + [f"{reason} -- not ranked, see Observations"]

    kept_mech, excluded_mech = _exclude_kit_builder(
        finding["mechanical"], personas, _mechanical_session_tag
    )
    for entry in excluded_mech:
        notes.append(f"excluded mechanical entry {entry!r}: kit-builder session")
    mechanical_raw = sum(int(m) for m in COUNT_RE.findall(" ".join(kept_mech)))
    mechanical_cap = human_turns * HUMAN_TURN_WEIGHT
    mechanical_capped = min(mechanical_raw, mechanical_cap)
    if mechanical_raw > mechanical_cap:
        notes.append(
            f"mechanical cost capped {mechanical_raw:.0f} -> {mechanical_capped:.0f} "
            f"(at most 2x {human_turns} human turn(s), never ranks a finding on its own)"
        )

    kept_sessions, _ = _exclude_kit_builder(finding["sessions"], personas, lambda s: s)
    sessions = set(kept_sessions) or _sessions_from_text(" ".join(kept_turns), " ".join(kept_mech))
    distinct_sessions = max(len(sessions), 1)

    severity = finding["severity"] if finding["severity"] in SEVERITY_MULT else "none"
    if finding["severity"] not in SEVERITY_MULT:
        notes.append(f"unknown severity {finding['severity']!r} treated as 'none'")
    mult = SEVERITY_MULT[severity]

    cost = (mechanical_capped + human_turns * HUMAN_TURN_WEIGHT) * distinct_sessions * mult

    if is_stale(finding, digests):
        raw_cost = cost
        cost *= STALE_EVIDENCE_DISCOUNT
        notes.append(
            f"raw cost {raw_cost:.2f}, evidence predates the last change to "
            f"{', '.join(finding['fix_files'])} -- discounted {STALE_EVIDENCE_DISCOUNT}x "
            f"to {cost:.2f}, may already be fixed"
        )
    return cost, effort, notes


# --------------------------------------------------------------- deferred

def load_deferred() -> list[dict]:
    return retro_ledger.load_entries(
        DEFERRED_FILE, label="docs/retro/deferred.json"
    )


def save_deferred(entries: list[dict]) -> None:
    retro_ledger.save_entries(
        DEFERRED_FILE, entries, label="docs/retro/deferred.json",
        lock_dir=DECISION_LOCK_DIR,
    )


def update_deferred(transform) -> list[dict]:
    """Serialize one deferred-ledger read-modify-write across processes."""
    return retro_ledger.update_entries(
        DEFERRED_FILE, transform, label="docs/retro/deferred.json",
        lock_dir=DECISION_LOCK_DIR,
    )


def apply_deferred_votes(findings: list[dict], scored: dict[str, tuple[float, list[str]]]) -> list[str]:
    """Second-round voting: a deferred finding that recurs scores higher.

    A deferred finding absent from this evidence, deferred before today, is a
    confirmed win and gets marked resolved rather than re-litigated forever.
    """
    today = date.today().isoformat()
    present = {normalise_title(f["title"]) for f in findings if len(f["human_turns"]) > 0}
    console_notes: list[str] = []

    def apply(entries: list[dict]) -> list[dict]:
        for entry in entries:
            key = normalise_title(entry.get("finding", ""))
            if key in present and entry.get("date", today) < today:
                title = next(
                    finding["title"] for finding in findings
                    if normalise_title(finding["title"]) == key
                )
                cost, effort, notes = scored[title]
                boosted = cost * DEFERRED_RECURRENCE_BOOST
                scored[title] = (boosted, effort, notes + [
                    f"deferred on {entry['date']} (\"{entry.get('reason', '')}\") "
                    f"and recurred with new evidence -- boosted "
                    f"{DEFERRED_RECURRENCE_BOOST}x"
                ])
                recurred = entry.setdefault("recurred_at", [])
                if today not in recurred:
                    recurred.append(today)
                console_notes.append(
                    f"deferred finding recurred: {entry.get('finding')!r} "
                    f"(deferred {entry['date']})"
                )
            elif (key not in present and not entry.get("resolved_at")
                  and entry.get("date", today) < today):
                entry["resolved_at"] = today
                console_notes.append(
                    f"confirmed win: {entry.get('finding')!r} deferred on "
                    f"{entry['date']} is absent from this evidence -- marked resolved"
                )
        return entries

    update_deferred(apply)
    return console_notes


# ------------------------------------------------------------------ render

def render_finding(title: str, f: dict, cost: float | None, effort: int, quick_win: bool, notes: list[str]) -> str:
    lines = [f"## Finding: {title}", ""]
    lines.append(f"sessions: [{'; '.join(f['sessions'])}]")
    lines.append(f"human_turns: [{'; '.join(f['human_turns'])}]")
    lines.append(f"mechanical: [{'; '.join(f['mechanical'])}]")
    lines.append(f"recurs: {'true' if f['recurs'] else 'false'}")
    lines.append(f"severity: {f['severity']}")
    lines.append(f"fix_files: [{'; '.join(f['fix_files'])}]")
    lines.append(f"fix_lines: {f['fix_lines']}")
    if cost is not None:
        lines.append(f"cost: {cost:.2f}")
        lines.append(f"effort: {effort}")
        if quick_win:
            lines.append("# quick win: cheapest effort bucket, cost clears the floor -- clear this out first")
    for note in notes:
        lines.append(f"# {note}")
    lines.append("")
    lines.append(f["body"].rstrip("\n"))
    return "\n".join(lines) + "\n"



def rewrite(
    path: Path, header: str, findings: list[dict], digests: dict[str, dict], personas: dict[str, str]
) -> tuple[str, dict[str, tuple[float, int, list[str]]], list[str]]:
    scored: dict[str, tuple[float, int, list[str]]] = {}
    for f in findings:
        scored[f["title"]] = score(f, digests, personas)

    deferred_notes = apply_deferred_votes(findings, scored)

    # A finding may cite only human turns; cost 0.0 is otherwise unreachable
    # once human_turns > 0 (HUMAN_TURN_WEIGHT > 0), so it is the reliable
    # signal that no citation survived kit-builder exclusion either.
    ranked = [f for f in findings if scored[f["title"]][0] > 0]
    observed = [f for f in findings if scored[f["title"]][0] <= 0]
    ranked.sort(
        key=lambda f: (scored[f["title"]][0], recency_key(f, digests), len(f["human_turns"])),
        reverse=True,
    )

    out = [header.rstrip("\n"), ""]
    for f in ranked:
        cost, effort, notes = scored[f["title"]]
        out.append(render_finding(f["title"], f, cost, effort, is_quick_win(cost, effort), notes))
    if observed:
        out.append("## Observations")
        out.append("")
        out.append(
            "No human turn is attributed to these. A recurrence nobody ever reacted "
            "to is evidence the human does not care, not evidence of a problem, so "
            "these are not ranked."
        )
        out.append("")
        for f in observed:
            _, effort, notes = scored[f["title"]]
            out.append(render_finding(f["title"], f, None, effort, False, notes))

    return "\n".join(out).rstrip("\n") + "\n", scored, deferred_notes


# --------------------------------------------------------------------- main

def unreviewed_summary() -> tuple[int, float]:
    """(count, highest cost) of ranked findings with no accept/defer decision.

    Reads only what the last `retro_rank.py` run already persisted into the
    findings file's `cost:` fields -- no session digesting, so this is cheap
    enough for the gate to call on every run and for plan_html.py to call on
    every regeneration. The single canonical implementation: plan_html.py's
    banner, check.py's retro_nudge and retro_html.py's own summary line all
    call this rather than each re-deriving "unreviewed" from scratch.
    """
    accepted = retro_ledger.load_entries(
        RETRO_DIR / "accepted.json", label="docs/retro/accepted.json"
    )
    decided = {normalise_title(e.get("finding", "")) for e in accepted}
    decided |= {normalise_title(e.get("finding", "")) for e in load_deferred()}

    count = 0
    highest = 0.0
    for path in sorted(RETRO_DIR.glob("*-findings.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        _, findings = parse_findings(text)
        for f in findings:
            cost = f.get("cost")
            if cost is None or normalise_title(f["title"]) in decided:
                continue
            count += 1
            highest = max(highest, cost)
    return count, highest


def latest_findings_file() -> Path | None:
    files = sorted(RETRO_DIR.glob("*-findings.md"))
    return files[-1] if files else None


def main() -> int:
    ap = argparse.ArgumentParser(description="Score a retrospective findings file on cost and effort.")
    ap.add_argument("file", nargs="?", help="findings file (default: newest docs/retro/*-findings.md)")
    ap.add_argument("--dry-run", action="store_true", help="print the reordered file, do not write it")
    a = ap.parse_args()

    path = Path(a.file) if a.file else latest_findings_file()
    if path is None or not path.is_file():
        print("no findings file found under docs/retro/", file=sys.stderr)
        return 1

    text = path.read_text(encoding="utf-8")
    header, findings = parse_findings(text)
    if not findings:
        print(f"{path}: no '## Finding:' blocks found", file=sys.stderr)
        return 1

    digests, warnings, personas = citation_report(findings)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    if personas:
        counts: dict[str, int] = {}
        for p in personas.values():
            counts[p] = counts.get(p, 0) + 1
        breakdown = ", ".join(f"{p}={n}" for p, n in sorted(counts.items()))
        excluded = sum(1 for p in personas.values() if p == KIT_BUILDER_PERSONA)
        print(f"cited sessions by persona: {breakdown}"
              + (f" ({excluded} excluded from scoring)" if excluded else ""),
              file=sys.stderr)

    rewritten, scored, deferred_notes = rewrite(path, header, findings, digests, personas)
    for note in deferred_notes:
        print(note)

    if a.dry_run:
        print(rewritten)
    else:
        path.write_text(rewritten, encoding="utf-8")
        # Ranking is the moment findings become dispatch-ready, so it is the
        # only trigger for building dispatch prompt artifacts. Imported here
        # rather than at module scope because retro_queue imports this module.
        # Built after the rewrite, never before: the artifact records the
        # sha256 of the file as it now stands, which is what staleness is
        # measured against.
        import retro_queue  # noqa: PLC0415

        built = retro_queue.build()
        print(f"dispatch artifacts: {len(built)} written to "
              f"{retro_queue.QUEUE_DIR.relative_to(ROOT).as_posix()}/")

    ranked_order = sorted(
        (f for f in findings if scored[f["title"]][0] > 0),
        key=lambda f: (scored[f["title"]][0], recency_key(f, digests), len(f["human_turns"])),
        reverse=True,
    )
    print(f"{path.name}: {len(ranked_order)} ranked, {len(findings) - len(ranked_order)} observation(s)")
    for i, f in enumerate(ranked_order, 1):
        cost, effort, _ = scored[f["title"]]
        flag = "  quick win" if is_quick_win(cost, effort) else ""
        print(f"  {i}. cost {cost:8.2f}  effort {effort}{flag}  {f['title']}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except retro_ledger.LedgerError as exc:
        print(f"retro ranking: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
