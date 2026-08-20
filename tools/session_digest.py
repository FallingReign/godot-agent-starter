#!/usr/bin/env python3
"""Reduce agent session logs to a token-dense digest.

A retrospective must read across many sessions. Raw logs are 50 KB to 300 KB
each, almost all of it tool-call noise, so reading them directly is not viable:
four slices would exceed a million tokens.

This tool does the reduction. Measured against real logs it achieves roughly
200:1 -- a 280 KB session becomes about 1.4 KB.

Three rules govern what survives:

  Human messages verbatim, never summarised. That is where the findings are,
  and paraphrase destroys them. A real complaint reads as observation
  ("Built now shows to do not written"), not accusation, so no classifier is
  reliable enough to filter them.

  Everything mechanical becomes a count. Gate failures, command loops, file
  rewrites, turn counts, cost.

  Nothing else. No tool arguments, no assistant prose, no file contents.

Each human message gets a stable id (S3:H4) so a retro can cite it and a later
retro can recognise the same complaint recurring.

    python tools/session_digest.py                  digest all sessions for this repo
    python tools/session_digest.py --limit 10       most recent 10
    python tools/session_digest.py --since 2026-08-01
    python tools/session_digest.py --list           discovery only, no parsing
    python tools/session_digest.py --sessions DIR   override session-state location
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Copilot keeps one directory per session, each with a workspace.yaml carrying
# the cwd. That file is 248 bytes and declarative; the session database is an
# undocumented index whose schema can change. Prefer the yaml.
SESSION_ROOTS = [
    Path.home() / ".copilot" / "session-state",
    Path(os.environ.get("APPDATA", "/nonexistent")) / "copilot" / "session-state",
    Path.home() / ".gemini" / "tmp",
]

# Commands worth counting as a loop. A tool name is not a command -- "view" x25
# is meaningless, "python bootstrap.py --json" x7 is a finding.
CMD_RE = re.compile(r"(?:python|py|python3)\s+([\w./\\-]+\.py[^\"']*)")
GATE_FAIL_RE = re.compile(r"^\s*FAIL\s+(\w[\w-]*)", re.M)
GATE_PASS_RE = re.compile(r"GATE PASSED", re.M)


def _yaml_get(text: str, key: str) -> str:
    """Minimal single-level yaml scalar read. No dependency."""
    m = re.search(rf"^{re.escape(key)}\s*:\s*(.+?)\s*$", text, re.M)
    if not m:
        return ""
    v = m.group(1).strip()
    if v[:1] in ("'", '"') and v[-1:] == v[:1]:
        v = v[1:-1]
    return v


def _norm(p: str) -> str:
    return str(p).replace("\\", "/").rstrip("/").lower()


def discover(repo: Path, roots: list[Path] | None = None) -> list[dict]:
    """Find every session whose workspace matches this repo.

    Copilot: workspace.yaml carries cwd directly.
    Gemini: first line of the log carries projectHash and kind, with workspace
    directories in the payload.
    """
    want = _norm(repo)
    found: list[dict] = []
    for root in roots or SESSION_ROOTS:
        if not root.is_dir():
            continue
        for wsf in root.glob("*/workspace.yaml"):
            try:
                txt = wsf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _norm(_yaml_get(txt, "cwd")) != want:
                continue
            logs = sorted(wsf.parent.glob("*.jsonl"))
            if not logs:
                continue
            found.append({
                "id": _yaml_get(txt, "id") or wsf.parent.name,
                "name": _yaml_get(txt, "name"),
                "started": _yaml_get(txt, "created_at"),
                "updated": _yaml_get(txt, "updated_at"),
                "log": max(logs, key=lambda p: p.stat().st_size),
                "kind": "copilot",
            })
        # Gemini: no workspace.yaml, so match on the first line of each log.
        for log in root.glob("**/*.jsonl"):
            if (log.parent / "workspace.yaml").exists():
                continue
            try:
                with log.open(encoding="utf-8", errors="replace") as f:
                    head = f.readline()
            except OSError:
                continue
            if not head.strip():
                continue
            try:
                rec = json.loads(head)
            except ValueError:
                continue
            dirs = rec.get("directories") or []
            if not any(_norm(d) == want for d in dirs):
                # Fall back to a scan of the first few lines for the workspace
                # banner Gemini writes into its context payload.
                try:
                    with log.open(encoding="utf-8", errors="replace") as f:
                        blob = "".join(next(f, "") for _ in range(6))
                except OSError:
                    continue
                if _norm(repo) not in _norm(blob):
                    continue
            found.append({
                "id": rec.get("sessionId") or log.stem,
                "name": rec.get("kind", ""),
                "started": rec.get("startTime", ""),
                "updated": rec.get("lastUpdated", ""),
                "log": log,
                "kind": "gemini",
            })
    found.sort(key=lambda s: s["updated"] or s["started"] or "")
    return found


def _human_messages(recs: list[dict]) -> list[str]:
    """Human turns only.

    Skill injections and task prompts arrive as user messages too, so they are
    excluded by shape: skill context is bracketed markup, and the first user
    message of a session is the task rather than feedback.
    """
    out: list[str] = []
    for r in recs:
        t = r.get("type", "")
        if t not in ("user.message", "user"):
            continue
        d = r.get("data", r)
        txt = d.get("content") or d.get("text") or ""
        if not isinstance(txt, str):
            continue
        s = txt.strip()
        if not s or len(s) > 4000:
            continue
        # Injected skill / context payloads, not the human speaking.
        if s.startswith(("<", "[", "#!")) or "SKILL.md" in s[:200]:
            continue
        if "Workspace Directories" in s[:200]:
            continue
        out.append(" ".join(s.split()))
    return out


def digest_one(sess: dict, index: int) -> dict:
    """Reduce one session log to counts plus verbatim human messages."""
    recs: list[dict] = []
    try:
        with sess["log"].open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return {"index": index, "error": "unreadable"}

    turns = 0
    cost = 0.0
    model = ""
    cmds: Counter = Counter()
    writes: Counter = Counter()
    gate_fails: Counter = Counter()
    gate_passes = 0

    for r in recs:
        t = r.get("type", "")
        d = r.get("data", r) if isinstance(r.get("data"), dict) else r
        if t in ("assistant.message", "assistant"):
            turns += 1
            model = d.get("model") or model
            u = d.get("usage") or {}
            if isinstance(u, dict):
                try:
                    cost += float(u.get("totalAiu") or u.get("aiu") or 0)
                except (TypeError, ValueError):
                    pass
        args = d.get("arguments")
        if not isinstance(args, dict):
            args = {}
        blob = ""
        for k in ("command", "cmd", "shellCommand"):
            v = args.get(k)
            if isinstance(v, str):
                blob = v
                break
        if blob:
            m = CMD_RE.search(blob)
            if m:
                cmds[("python " + m.group(1)).strip()] += 1
        for k in ("path", "filePath", "file_path"):
            v = args.get(k)
            if isinstance(v, str) and t.startswith(("tool", "function")):
                nm = (d.get("name") or d.get("tool") or "").lower()
                if any(w in nm for w in ("write", "edit", "create", "patch", "replace")):
                    writes[_norm(v).split("/")[-1] or v] += 1
        out = d.get("output") or d.get("result") or d.get("content")
        if isinstance(out, str) and "FAIL" in out:
            for stage in GATE_FAIL_RE.findall(out):
                gate_fails[stage] += 1
        if isinstance(out, str) and GATE_PASS_RE.search(out):
            gate_passes += 1

    return {
        "index": index,
        "id": sess["id"],
        "name": sess.get("name", ""),
        "started": sess.get("started", ""),
        "turns": turns,
        "cost": round(cost, 2),
        "model": model,
        "gate_fails": dict(gate_fails.most_common(6)),
        "gate_passes": gate_passes,
        "loops": {c: n for c, n in cmds.most_common(6) if n >= 3},
        "rewrites": {f: n for f, n in writes.most_common(8) if n >= 3},
        "human": _human_messages(recs),
        "raw_bytes": sess["log"].stat().st_size,
    }


def render(digests: list[dict]) -> str:
    lines: list[str] = []
    for d in digests:
        if d.get("error"):
            lines.append(f"S{d['index']} unreadable")
            continue
        head = f"S{d['index']} {d['started'][:16]} {d['turns']}t"
        if d["cost"]:
            head += f" {d['cost']}AIU"
        if d["model"]:
            head += f" {d['model']}"
        if d.get("name"):
            head += f'  "{d["name"]}"'
        lines.append(head)
        for stage, n in d["gate_fails"].items():
            lines.append(f"  FAIL {stage} x{n}")
        if d["gate_passes"]:
            lines.append(f"  PASS gate x{d['gate_passes']}")
        for cmd, n in d["loops"].items():
            lines.append(f"  LOOP {cmd} x{n}")
        for f, n in d["rewrites"].items():
            lines.append(f"  REWRITE {f} x{n}")
        for i, msg in enumerate(d["human"], 1):
            lines.append(f'  {d["index"]}:H{i} "{msg}"')
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Token-dense digest of agent sessions for this repo.")
    ap.add_argument("--sessions", help="override session-state directory")
    ap.add_argument("--limit", type=int, help="most recent N sessions")
    ap.add_argument("--since", help="ISO date, sessions updated on or after")
    ap.add_argument("--list", action="store_true", help="discovery only, no parsing")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--repo", help="repo path to match (default: this repo)")
    a = ap.parse_args()

    # A --repo value may be a foreign-platform path (a Windows cwd read from
    # workspace.yaml on a posix box), so it must not be resolved.
    repo = Path(a.repo) if a.repo else ROOT.resolve()
    roots = [Path(a.sessions)] if a.sessions else None
    sessions = discover(repo, roots)

    if a.since:
        sessions = [s for s in sessions if (s["updated"] or s["started"] or "") >= a.since]
    if a.limit:
        sessions = sessions[-a.limit:]

    if not sessions:
        print(f"no sessions found for {repo}", file=sys.stderr)
        print("checked: " + ", ".join(str(r) for r in (roots or SESSION_ROOTS)), file=sys.stderr)
        return 1

    if a.list:
        for i, s in enumerate(sessions, 1):
            kb = s["log"].stat().st_size // 1024
            print(f"S{i} {s['updated'][:16]} {kb}KB {s['kind']} {s['id'][:8]} {s.get('name','')}")
        return 0

    digests = [digest_one(s, i) for i, s in enumerate(sessions, 1)]
    if a.json:
        print(json.dumps(digests, indent=2))
        return 0

    text = render(digests)
    raw = sum(d.get("raw_bytes", 0) for d in digests)
    print(text, end="")
    if raw:
        print(f"\n# {len(digests)} session(s)  {raw//1024}KB raw -> {len(text)//1024 or 1}KB digest"
              f"  ({raw // max(len(text), 1)}:1)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
