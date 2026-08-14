#!/usr/bin/env python3
"""Retrospective evidence pack.

Reconstructs what happened during a slice from four deterministic sources:
agent session logs, git history, the kit artefacts, and the gate logs.

The evidence pack is the deliverable. A model reading it is one adapter.

    python tools/retro.py --print          write pack + prompt, no model call
    python tools/retro.py --sdk            call copilot via the SDK adapter
    python tools/retro.py --sessions DIR   override session log location

The pack never contains a conclusion. It contains counts, sequences and
verbatim quotes, so a reader can disagree with it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RETRO_DIR = ROOT / "docs" / "retro"

# Where copilot keeps session logs, per platform.
SESSION_DIRS = [
    Path.home() / ".copilot" / "session-state",
    Path(os.environ.get("APPDATA", "/nonexistent")) / "copilot" / "session-state",
]

# Low-recall hint layer only. Tested against a real session: caught 0 of 4
# genuine friction messages, because real friction reads as observation
# ("Built now shows to do not written") rather than accusation. Every human
# message is therefore included verbatim in the pack and the model classifies.
# These tags are a convenience for the terminal summary, never the signal.
FRICTION_HINTS = [
    (r"\b(?:not|isn'?t) (?:what|right|correct|working)", "correction"),
    (r"\bi (?:did ?n[o']?t|never) (?:ask|want|say|expect)", "correction"),
    (r"\bwhy (?:did|are|is|would|has)\b", "questioning"),
    (r"\b(?:missed|forgot|skipped|ignored|lost|broke)\b", "gap"),
    (r"\b(?:i )?(?:expected|thought|assumed)\b", "expectation gap"),
    (r"\b(?:revert|undo|roll ?back)\b", "rework"),
    (r"\b(?:irks|annoying|frustrating|tedious|unclear|confusing)\b", "irritation"),
    (r"\b(?:needs to|should|must)\b", "requirement"),
    (r"\bwe (?:seem(?:ed)? to have )?lost\b", "regression"),
    (r"\bdo we (?:have|follow|currently)\b", "process gap"),
]

# Commands that legitimately repeat (verification). Anything else repeating
# many times inside one session is a candidate loop.
EXPECTED_REPEATS = ("check.py", "git status", "git diff", "git log")


def sh(*args: str, cwd: Path | None = None) -> str:
    try:
        r = subprocess.run(
            args, cwd=str(cwd or ROOT), capture_output=True, text=True, timeout=30
        )
        return r.stdout.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------- session logs

def find_session_logs(override: str | None) -> list[Path]:
    """Locate events.jsonl files, newest first."""
    roots: list[Path] = []
    if override:
        roots.append(Path(override))
    else:
        roots.extend(d for d in SESSION_DIRS if d.is_dir())
    out: list[Path] = []
    for r in roots:
        if not r.exists():
            continue
        if r.is_file():
            out.append(r)
            continue
        out.extend(r.rglob("events.jsonl"))
        out.extend(p for p in r.glob("*.jsonl") if p.name != "events.jsonl")
    out = [p for p in out if p.stat().st_size > 0]
    out.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return out


def read_events(path: Path) -> list[dict]:
    """Tolerant line-delimited JSON reader. Unknown event types survive."""
    events: list[dict] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return events


def _ts(e: dict) -> str:
    return e.get("timestamp") or ""


def _short(text: str, n: int = 200) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "\u2026"


def detect_format(events: list[dict]) -> str:
    """Which agent wrote this log. Unknown formats are skipped, not guessed."""
    for e in events[:40]:
        if e.get("type") in ("session.start", "assistant.turn_start", "tool.execution_start"):
            return "copilot"
        if e.get("type") in ("user", "gemini") or "$set" in e:
            return "gemini"
        if "sessionId" in e and "projectHash" in e:
            return "gemini"
    return "unknown"


def analyse_gemini(path: Path, events: list[dict]) -> dict:
    """Gemini CLI logs: a header line, then message batches under $set/$push."""
    msgs: list[dict] = []
    started = ""
    for e in events:
        if "startTime" in e and "sessionId" in e:
            started = e.get("startTime", "")
        for key in ("$set", "$push"):
            block = e.get(key)
            if isinstance(block, dict):
                got = block.get("messages")
                if isinstance(got, list):
                    msgs.extend(m for m in got if isinstance(m, dict))
        if e.get("type") in ("user", "gemini"):
            msgs.append(e)

    def text_of(m: dict) -> str:
        c = m.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(
                part.get("text", "") for part in c if isinstance(part, dict)
            )
        return ""

    human: list[dict] = []
    first_human = ""
    turns = 0
    for m in msgs:
        kind = m.get("type") or m.get("role") or ""
        body = text_of(m)
        if kind in ("gemini", "assistant", "model"):
            turns += 1
            continue
        if kind != "user" or not body.strip():
            continue
        if body.lstrip().startswith(("<session_context", "<skill-context", "<system")):
            continue
        first_human = first_human or body
        if body == first_human:
            continue
        hints = [lab for pat, lab in FRICTION_HINTS if re.search(pat, body, re.I)]
        human.append(
            {"at": m.get("timestamp", ""), "text": _short(body, 1200), "hints": sorted(set(hints))}
        )

    return {
        "log": str(path),
        "format": "gemini",
        "session_id": next((e.get("sessionId", "") for e in events if "sessionId" in e), ""),
        "model": "gemini",
        "started": started,
        "branch": "",
        "head": "",
        "turns": turns,
        "nano_aiu": 0,
        "events": len(events),
        "gate_runs": [],
        "stage_failures": {},
        "command_loops": [],
        "file_rewrites": [],
        "file_rereads": [],
        "task_prompt": _short(first_human, 800),
        "human_messages": human[:60],
        "tool_failures": [],
        "user_message_count": len(human) + 1,
        "injected_context_messages": 0,
        "skills_invoked": [],
        "note": "gemini format: tool detail not extracted, human messages only",
    }


def analyse_session(path: Path) -> dict | None:
    """Extract friction signals from one session log. No conclusions."""
    events = read_events(path)
    if not events:
        return None

    fmt = detect_format(events)
    if fmt == "gemini":
        return analyse_gemini(path, events)
    if fmt == "unknown":
        return {
            "log": str(path),
            "format": "unknown",
            "events": len(events),
            "note": "unrecognised session log format, skipped",
            "turns": 0,
            "nano_aiu": 0,
            "started": "",
            "model": "",
            "gate_runs": [],
            "stage_failures": {},
            "command_loops": [],
            "file_rewrites": [],
            "file_rereads": [],
            "human_messages": [],
            "task_prompt": "",
            "skills_invoked": [],
            "tool_failures": [],
        }

    start = next((e for e in events if e.get("type") == "session.start"), None)
    sd = (start or {}).get("data", {})

    commands: Counter[str] = Counter()
    cmd_first: dict[str, str] = {}
    tool_fails: list[dict] = []
    user_msgs: list[dict] = []
    skills: list[dict] = []
    gate_runs: list[dict] = []
    stage_fails: Counter[str] = Counter()
    file_writes: Counter[str] = Counter()
    reads: Counter[str] = Counter()
    injected = 0
    first_human = ""
    nano_aiu = 0
    turns = 0
    pending: dict[str, dict] = {}

    for e in events:
        t = e.get("type")
        d = e.get("data")
        if not isinstance(d, dict):
            d = {}

        if t == "assistant.turn_start":
            turns += 1

        elif t == "session.usage_checkpoint":
            nano_aiu = max(nano_aiu, int(d.get("totalNanoAiu") or 0))

        elif t == "user.message":
            content = d.get("content") or ""
            # Skill bodies and system context are injected as user messages.
            # They are not human input and must not be scanned for friction.
            if content.lstrip().startswith(("<skill-context", "<system", "<current_datetime")):
                injected += 1
                continue
            first_human = first_human or content
            tags = [
                label
                for pat, label in FRICTION_HINTS
                if re.search(pat, content, re.I)
            ]
            user_msgs.append(
                {
                    "at": _ts(e),
                    "text": _short(content, 1200),
                    "hints": sorted(set(tags)),
                    "is_task": content == first_human,
                }
            )

        elif t == "skill.invoked":
            skills.append({"at": _ts(e), "name": d.get("name") or "?"})

        elif t == "tool.execution_start":
            cid = d.get("toolCallId")
            args = d.get("arguments")
            if not isinstance(args, dict):
                args = {"command": args} if isinstance(args, str) else {}
            cmd = args.get("command") or args.get("cmd") or ""
            if cmd:
                cmd = _short(str(cmd), 160)
                commands[cmd] += 1
                cmd_first.setdefault(cmd, _ts(e))
            target = args.get("filePath") or args.get("path") or args.get("file") or ""
            if target:
                reads[_short(str(target), 120)] += 1
            if cid:
                pending[cid] = {"cmd": cmd, "at": _ts(e), "tool": d.get("toolName")}
            tool = (d.get("toolName") or "").lower()
            if tool in ("write", "create_file", "edit", "str_replace", "apply_patch"):
                fp = args.get("filePath") or args.get("path") or args.get("file") or ""
                if fp:
                    file_writes[_short(str(fp), 120)] += 1

        elif t == "tool.execution_complete":
            cid = d.get("toolCallId")
            info = pending.pop(cid, {}) if cid else {}
            result = d.get("result")
            if isinstance(result, dict):
                content = result.get("content")
            elif isinstance(result, str):
                content = result
            else:
                content = ""
            if not isinstance(content, str):
                try:
                    content = json.dumps(content)[:4000]
                except Exception:
                    content = ""
            if d.get("success") is False:
                tool_fails.append(
                    {
                        "at": _ts(e),
                        "cmd": info.get("cmd", "?"),
                        "error": _short(content, 300),
                    }
                )
            if "GATE PASSED" in content or "GATE FAILED" in content:
                passed = "GATE PASSED" in content
                failed_stages = re.findall(r"^\s*FAIL\s+(\w+)", content, re.M)
                for s in failed_stages:
                    stage_fails[s] += 1
                gate_runs.append(
                    {"at": _ts(e), "passed": passed, "failed": failed_stages}
                )

    loops = [
        {"cmd": c, "count": n, "first": cmd_first.get(c, "")}
        for c, n in commands.most_common()
        if n >= 4 and not any(x in c for x in EXPECTED_REPEATS)
    ]
    rewrites = [
        {"file": f, "count": n} for f, n in file_writes.most_common() if n >= 4
    ]
    rereads = [
        {"file": f, "count": n} for f, n in reads.most_common() if n >= 6
    ]

    return {
        "log": str(path),
        "format": "copilot",
        "session_id": sd.get("sessionId", ""),
        "model": sd.get("selectedModel", ""),
        "started": sd.get("startTime", ""),
        "branch": (sd.get("context") or {}).get("branch", ""),
        "head": (sd.get("context") or {}).get("headCommit", "")[:8],
        "turns": turns,
        "nano_aiu": nano_aiu,
        "events": len(events),
        "gate_runs": gate_runs,
        "stage_failures": dict(stage_fails.most_common()),
        "command_loops": loops[:10],
        "file_rewrites": rewrites[:10],
        "tool_failures": tool_fails[:15],
        "file_rereads": rereads[:10],
        "task_prompt": _short(first_human, 800),
        # Every human message after the task, verbatim. This is the primary
        # friction channel. Classify these; do not rely on the hints.
        "human_messages": [
            {"at": m["at"], "text": m["text"], "hints": m["hints"]}
            for m in user_msgs
            if not m["is_task"]
        ][:60],
        "user_message_count": len(user_msgs),
        "injected_context_messages": injected,
        "skills_invoked": skills,
    }


# ------------------------------------------------------------------ repo state

def git_evidence(baseline: str | None) -> dict:
    rng = f"{baseline}..HEAD" if baseline else "-20"
    log = sh("git", "log", "--format=%h|%ad|%s", "--date=short", rng)
    commits = []
    for line in log.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append({"sha": parts[0], "date": parts[1], "subject": parts[2]})
    churn: Counter[str] = Counter()
    if baseline:
        for line in sh("git", "log", "--format=%H", f"{baseline}..HEAD").splitlines():
            for f in sh("git", "show", "--name-only", "--format=", line).splitlines():
                if f.strip():
                    churn[f.strip()] += 1
    return {
        "baseline": baseline or "(none)",
        "commits": commits[:25],
        "commit_count": len(commits),
        "churn": [{"file": f, "commits": n} for f, n in churn.most_common(12) if n >= 2],
        "dirty": bool(sh("git", "status", "--porcelain")),
    }


def artefact_evidence() -> dict:
    out: dict = {}
    prop = ROOT / "proposal.json"
    if prop.exists():
        try:
            p = json.loads(prop.read_text(encoding="utf-8"))
            out["proposal"] = {
                "slice": p.get("slice", ""),
                "status": p.get("status", ""),
                "modules": len(p.get("modules") or []),
                "files": len(p.get("files") or []),
                "revisions": len(p.get("revisions") or []),
                "design_refs": [r.get("section") for r in (p.get("design_refs") or [])],
                "has_experience": bool(p.get("experience")),
                "considered_existing": len(p.get("considered_existing") or []),
            }
        except Exception as exc:
            out["proposal"] = {"error": str(exc)}
    shape = ROOT / "project.shape.json"
    if shape.exists():
        try:
            s = json.loads(shape.read_text(encoding="utf-8"))
            qs = [q for q in (s.get("questions") or []) if not str(q.get("id", "")).startswith("_")]
            out["shape"] = {
                "involvement": s.get("involvement", ""),
                "decisions": len([d for d in (s.get("decisions") or []) if not str(d.get("id", "")).startswith("_")]),
                "open_questions": [
                    {"id": q.get("id"), "raised": q.get("raised"), "question": _short(q.get("question", ""), 160)}
                    for q in qs
                ],
            }
        except Exception as exc:
            out["shape"] = {"error": str(exc)}
    design = ROOT / "docs" / "design"
    if design.is_dir():
        secs = [p for p in design.rglob("*.md") if p.name not in ("README.md", "INDEX.md")]
        out["design"] = {"sections": len(secs), "paths": sorted(str(p.relative_to(ROOT)) for p in secs)[:20]}
    return out


def checklog_evidence() -> dict:
    d = ROOT / ".checklogs"
    if not d.is_dir():
        return {}
    out = {}
    for p in sorted(d.glob("*.log")):
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        errs = re.findall(r"^(?:SCRIPT )?ERROR:.*$", text, re.M)[:5]
        out[p.stem] = {
            "bytes": len(text),
            "error_lines": [_short(e, 200) for e in errs],
        }
    return out


# ------------------------------------------------------------------ pack + emit

def build_pack(args) -> dict:
    logs = find_session_logs(args.sessions)
    sessions = []
    for p in logs[: args.max_sessions]:
        a = analyse_session(p)
        if a:
            sessions.append(a)
    baseline = args.baseline
    if not baseline:
        prop = ROOT / "proposal.json"
        if prop.exists():
            try:
                baseline = (json.loads(prop.read_text(encoding="utf-8")) or {}).get("baseline_sha") or None
            except Exception:
                baseline = None
    return {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repo": str(ROOT),
        "sessions_found": len(logs),
        "sessions": sessions,
        "git": git_evidence(baseline),
        "artefacts": artefact_evidence(),
        "gate_logs": checklog_evidence(),
    }


# ------------------------------------------------------------------- triggers

def slice_key() -> str:
    """Identifies the current slice. The lock is per slice."""
    prop = ROOT / "proposal.json"
    if not prop.exists():
        return "no-proposal"
    try:
        p = json.loads(prop.read_text(encoding="utf-8"))
    except Exception:
        return "unreadable"
    return f"{p.get('slice','unknown')}@{p.get('baseline_sha','')[:8]}"


def already_ran(key: str) -> bool:
    lock = RETRO_DIR / ".ran"
    if not lock.exists():
        return False
    try:
        return key in lock.read_text(encoding="utf-8").splitlines()
    except Exception:
        return False


def mark_ran(key: str) -> None:
    lock = RETRO_DIR / ".ran"
    prev = lock.read_text(encoding="utf-8") if lock.exists() else ""
    lock.write_text(prev + key + "\n", encoding="utf-8")


def should_trigger(pack: dict) -> tuple[bool, str]:
    """Decide whether an automatic retro is warranted.

    Deliberately conservative. A retro that fires twice spends twice, and a
    runaway loop is the exact failure this kit has already produced once.
    """
    key = slice_key()
    if already_ran(key):
        return False, f"already ran for {key}"

    prop = (pack.get("artefacts") or {}).get("proposal") or {}
    reasons: list[str] = []

    if prop.get("status") == "approved":
        confs = pack.get("gate_logs") or {}
        if confs:
            reasons.append("slice complete: proposal approved and gate has run")

    for sess in pack.get("sessions") or []:
        worst = max((sess.get("stage_failures") or {}).values(), default=0)
        if worst >= 3:
            stage = max((sess.get("stage_failures") or {}).items(), key=lambda kv: kv[1])[0]
            reasons.append(f"one stage failed {worst}x in a session ({stage})")
        if sess.get("command_loops"):
            top = sess["command_loops"][0]
            reasons.append(f"command repeated {top['count']}x")
        if sess.get("file_rewrites"):
            top = sess["file_rewrites"][0]
            reasons.append(f"file rewritten {top['count']}x")
        hinted = [m for m in (sess.get("human_messages") or []) if m.get("hints")]
        if len(hinted) >= 3:
            reasons.append(f"{len(hinted)} human messages hint at friction")

    if not reasons:
        return False, "no trigger condition met"
    return True, "; ".join(dict.fromkeys(reasons))


PROMPT = """You are running a retrospective on one slice of work in a Godot project
that uses an agent starter kit. The kit's job is to guide an AI agent so a
human intervenes as little as possible.

Read the evidence pack at {pack}. It is deterministic output: counts,
sequences and verbatim human quotes. It contains no conclusions.

The single most important field is sessions[].human_messages. Every message
the human sent after the opening task is there verbatim. Read all of them.
Each carries a "hints" array from a crude regex; it is low-recall and tested
to miss most real friction, so treat an empty hints array as meaningless.
Real friction reads as observation, not complaint: "Built now shows to do
not written" is a kit defect report.

You may read files in this repo to check anything in the pack. You may not
write to any file.

Your job is to find where the KIT failed, not where the agent failed. Every
human correction is a candidate kit defect: something the kit should have
supplied, checked or prevented. A command run many times is a candidate loop.
A file rewritten many times is candidate missing tooling.

Answer as JSON only, matching this shape exactly:

{{
  "slice": "<slice name from the pack, or unknown>",
  "summary": "<two sentences on how the slice went>",
  "findings": [
    {{
      "title": "<short>",
      "evidence": "<what in the pack supports this, quote it>",
      "kit_gap": "<what the kit should have done, or 'none - agent error'>",
      "severity": "high|medium|low",
      "confidence": "high|medium|low"
    }}
  ],
  "proposed_goals": [
    {{
      "goal": "<what to change in the kit>",
      "because": "<which finding it addresses>",
      "checkable": true|false
    }}
  ],
  "friction_not_visible": "<what you suspect happened but the pack cannot show>"
}}

Rules. Cite evidence for every finding or omit it. If the pack shows nothing
worth reporting, return empty arrays rather than inventing findings. Prefer
'confidence: low' over a confident guess. Say when a proposed goal is not
mechanically checkable."""


def emit_print(pack: dict, pack_path: Path) -> None:
    prompt_path = RETRO_DIR / "prompt.md"
    prompt_path.write_text(PROMPT.format(pack=pack_path.relative_to(ROOT)), encoding="utf-8")
    print(f"evidence pack   {pack_path.relative_to(ROOT)}")
    print(f"prompt          {prompt_path.relative_to(ROOT)}")
    print()
    print("Hand both to any agent, or run:")
    print(f"  copilot -p \"$(cat {prompt_path.relative_to(ROOT)})\" --allow-tool read --deny-tool write")


def summarise(pack: dict) -> None:
    print(f"sessions        {len(pack['sessions'])} analysed of {pack['sessions_found']} found")
    skipped = [s for s in pack["sessions"] if s.get("format") == "unknown"]
    for s in pack["sessions"]:
        if s.get("format") == "unknown":
            continue
        aiu = s["nano_aiu"] / 1e9
        label = s["model"] or s.get("format", "")
        print(f"  {s['started'][:16] or '?':16s}  {s['turns']:3d} turns  {aiu:6.2f} AIU  {label}")
        if s["stage_failures"]:
            print(f"      gate failures: {s['stage_failures']}")
        if s["command_loops"]:
            top = s["command_loops"][0]
            print(f"      repeated x{top['count']}: {top['cmd'][:60]}")
        if s["file_rewrites"]:
            top = s["file_rewrites"][0]
            print(f"      rewritten x{top['count']}: {top['file'][:60]}")
        hinted = [m for m in s["human_messages"] if m["hints"]]
        if s["human_messages"]:
            print(f"      human messages after task: {len(s['human_messages'])}"
                  f" ({len(hinted)} hint at friction)")
    if skipped:
        print(f"  {len(skipped)} log(s) in an unrecognised format, skipped")
    g = pack["git"]
    print(f"git             {g['commit_count']} commits since {g['baseline']}")
    if g["churn"]:
        print(f"      churn: {g['churn'][0]['file']} x{g['churn'][0]['commits']}")
    a = pack["artefacts"]
    if "proposal" in a:
        p = a["proposal"]
        print(f"proposal        {p.get('slice','?')} [{p.get('status','?')}] "
              f"{p.get('modules',0)}m {p.get('files',0)}f {p.get('revisions',0)}rev")
    if "shape" in a and a["shape"].get("open_questions"):
        print(f"open questions  {len(a['shape']['open_questions'])}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a retrospective evidence pack.")
    ap.add_argument("--print", dest="do_print", action="store_true",
                    help="write pack and prompt, no model call (default)")
    ap.add_argument("--sdk", action="store_true",
                    help="call copilot via the SDK adapter (needs node >=22)")
    ap.add_argument("--sessions", help="session log file or directory override")
    ap.add_argument("--baseline", help="git ref to diff from (default proposal baseline_sha)")
    ap.add_argument("--max-sessions", type=int, default=3)
    ap.add_argument("--if-warranted", action="store_true",
                    help="exit without acting unless a trigger fires (for hooks)")
    ap.add_argument("--force", action="store_true",
                    help="ignore the per-slice lock")
    ap.add_argument("--why", action="store_true",
                    help="report whether a retro is warranted and exit")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    RETRO_DIR.mkdir(parents=True, exist_ok=True)
    pack = build_pack(args)
    pack_path = RETRO_DIR / "evidence.json"
    pack_path.write_text(json.dumps(pack, indent=2), encoding="utf-8")

    if args.why or args.if_warranted:
        warranted, reason = should_trigger(pack)
        if args.force:
            warranted, reason = True, "forced"
        if args.why:
            print(("warranted: " if warranted else "not warranted: ") + reason)
            return 0
        if not warranted:
            if not args.quiet:
                print(f"no retro: {reason}")
            return 0
        if not args.quiet:
            print(f"retro warranted: {reason}")
            print()
        mark_ran(slice_key())

    if not args.quiet:
        summarise(pack)
        print()

    if args.sdk:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        try:
            from retro_sdk import run_sdk  # optional, only needed for --sdk
        except Exception as exc:
            print(f"SDK adapter unavailable: {exc}")
            print("the evidence pack is written; use --print instead")
            return 2
        return run_sdk(pack_path, PROMPT, RETRO_DIR)
    emit_print(pack, pack_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
