#!/usr/bin/env python3
"""Reduce agent session logs to a token-dense digest.

A retrospective must read across many sessions. Raw logs are 50 KB to 300 KB
each, almost all of it tool-call noise, so reading them directly is not viable:
four slices would exceed a million tokens.

This tool does the reduction. Measured against real logs it achieves roughly
200:1 -- a 280 KB session becomes about 1.4 KB.

Three rules govern what survives:

  Human messages verbatim up to an explicit safety cap, never summarised. That
  is where the findings are, and paraphrase destroys them. Any cap is carried
  as a truncation warning in the digest and immutable evidence manifest.

  Everything mechanical becomes a count. Gate failures, command loops, file
  rewrites, turn counts, cost.

  Nothing else. No tool arguments, no assistant prose, no file contents.

Each source message retains a session-id citation in the immutable snapshot.
Retrospective packs additionally assign compact ``S3:H4`` ordinals that are
stable only within that named, content-addressed pack. This module is an
internal implementation detail of the public ``kit retro`` workflow.
"""
from __future__ import annotations

import argparse
import json
import ntpath
import os
import posixpath
import re
import sys
from collections import Counter
from pathlib import Path

import session_evidence

ROOT = Path(__file__).resolve().parent.parent

# Copilot keeps one directory per session, each with a workspace.yaml carrying
# the cwd. That file is small and declarative; the session database is an
# undocumented index whose schema can change. Prefer the yaml. Do not add a
# recursive fallback here: this function runs on the board request path.
SESSION_ROOTS = [
    Path.home() / ".copilot" / "session-state",
    Path(os.environ.get("APPDATA", "/nonexistent")) / "copilot" / "session-state",
]

# Commands worth counting as a loop. A tool name is not a command -- "view" x25
# is meaningless, while repeatedly running the public gate or an internal script
# is evidence of friction. Keep historical Python recognition for older sessions.
KIT_CMD_RE = re.compile(
    r"(?<![\w.-])(?:\.{0,2}[\\/])?kit(?:\.cmd)?\s+"
    r"([a-z][a-z0-9_-]*(?:\s+[a-z][a-z0-9_-]*)?)",
    re.IGNORECASE,
)
PYTHON_CMD_RE = re.compile(r"(?:python|py|python3)\s+([^\s\"']+\.py)(?:\s|$)")
GATE_FAIL_RE = re.compile(r"^\s*FAIL\s+(\w[\w-]*)", re.M)
GATE_PASS_RE = re.compile(r"GATE PASSED", re.M)
MAX_HUMAN_CHARS = 4000


def _yaml_get(text: str, key: str) -> str:
    """Minimal single-level yaml scalar read. No dependency."""
    m = re.search(rf"^{re.escape(key)}\s*:\s*(.+?)\s*$", text, re.M)
    if not m:
        return ""
    v = m.group(1).strip()
    if v[:1] in ("'", '"') and v[-1:] == v[:1]:
        v = v[1:-1]
    return v


def _norm(p: str | Path) -> str:
    """Canonical comparison key without conflating case-sensitive repos.

    Session fixtures can carry a foreign-platform cwd, so semantics follow the
    path's syntax rather than the host running this reducer. Windows drive/UNC
    paths compare case-insensitively; POSIX absolute paths preserve case.
    """
    raw = str(p)
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("\\\\", "//")):
        value = ntpath.normcase(ntpath.normpath(raw))
    elif raw.startswith("/"):
        value = posixpath.normpath(raw)
    else:
        value = os.path.normcase(os.path.abspath(raw))
    return value.replace("\\", "/").rstrip("/")


def _select_event_log(directory: Path) -> Path | None:
    """Select the Copilot event stream without walking below the session."""
    for name in ("events.jsonl", "events.json"):
        candidate = directory / name
        if candidate.is_file():
            return candidate
    candidates: list[tuple[int, str, Path]] = []
    for candidate in directory.glob("*.jsonl"):
        try:
            candidates.append((-candidate.stat().st_size, _norm(candidate), candidate))
        except OSError:
            continue
    return min(candidates)[2] if candidates else None


def discover(repo: str | Path, roots: list[Path] | None = None) -> list[dict]:
    """Find Copilot sessions whose direct workspace metadata matches *repo*."""
    want = _norm(repo)
    found: dict[str, dict] = {}
    unique_roots = sorted({_norm(root): root for root in (roots or SESSION_ROOTS)}.values(),
                          key=_norm)
    for root in unique_roots:
        if not root.is_dir():
            continue
        for wsf in sorted(root.glob("*/workspace.yaml"), key=_norm):
            try:
                txt = wsf.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if _norm(_yaml_get(txt, "cwd")) != want:
                continue
            log = _select_event_log(wsf.parent)
            if log is None:
                continue
            session_id = _yaml_get(txt, "id") or wsf.parent.name
            candidate = {
                "id": session_id,
                "name": _yaml_get(txt, "name"),
                "started": _yaml_get(txt, "created_at"),
                "updated": _yaml_get(txt, "updated_at"),
                "workspace": wsf,
                "log": log,
                "kind": "copilot",
            }
            current = found.get(session_id)
            if current is None or _norm(candidate["log"]) < _norm(current["log"]):
                found[session_id] = candidate
    return sorted(found.values(), key=lambda s: (
        s["updated"] or s["started"] or "", s["started"] or "", s["id"],
        _norm(s["log"]),
    ))


def _human_messages(recs: list[dict], warnings: list[dict] | None = None) -> list[str]:
    """Human turns only.

    Skill injections arrive as user messages too, so they are excluded by
    shape. Genuine opening tasks remain evidence: they establish expectations
    that later output can satisfy or violate.
    """
    out: list[str] = []
    truncated = 0
    for r in recs:
        t = r.get("type", "")
        if t not in ("user.message", "user"):
            continue
        d = r.get("data", r)
        txt = d.get("content") or d.get("text") or ""
        if not isinstance(txt, str):
            continue
        s = txt.strip()
        if not s:
            continue
        # Injected skill / context payloads, not the human speaking.
        if s.startswith(("<", "[", "#!")) or "SKILL.md" in s[:200]:
            continue
        if "Workspace Directories" in s[:200]:
            continue
        normalised = " ".join(s.split())
        if len(normalised) > MAX_HUMAN_CHARS:
            normalised = normalised[:MAX_HUMAN_CHARS]
            truncated += 1
        out.append(normalised)
    if truncated and warnings is not None:
        warnings.append({"code": "human_message_truncated", "count": truncated,
                         "max_chars": MAX_HUMAN_CHARS})
    return out


def _parse_records(content: bytes, warnings: list[dict]) -> list[dict]:
    """Parse captured JSONL bytes and retain bounded, content-free diagnostics."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        text = content.decode("utf-8", errors="replace")
        warnings.append({"code": "invalid_utf8", "count": text.count("\ufffd")})

    stripped = text.strip()
    if stripped.startswith("["):
        try:
            document = json.loads(stripped)
        except ValueError:
            warnings.append({"code": "truncated_json_record", "count": 1,
                             "lines": [1]})
            return []
        if not isinstance(document, list):
            warnings.append({"code": "invalid_json_record", "count": 1,
                             "lines": [1]})
            return []
        records = [record for record in document if isinstance(record, dict)]
        invalid = len(document) - len(records)
        if invalid:
            warnings.append({"code": "invalid_json_record", "count": invalid,
                             "lines": []})
        return records

    recs: list[dict] = []
    invalid_lines: list[int] = []
    truncated_lines: list[int] = []
    lines = text.splitlines()
    final_is_partial = bool(content) and not content.endswith((b"\n", b"\r"))
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except ValueError:
            if final_is_partial and line_number == len(lines):
                truncated_lines.append(line_number)
            else:
                invalid_lines.append(line_number)
            continue
        if isinstance(record, dict):
            recs.append(record)
        else:
            invalid_lines.append(line_number)
    if invalid_lines:
        warnings.append({"code": "invalid_json_record", "count": len(invalid_lines),
                         "lines": invalid_lines[:20]})
    if truncated_lines:
        warnings.append({"code": "truncated_json_record", "count": len(truncated_lines),
                         "lines": truncated_lines[:20]})
    return recs


def digest_one(sess: dict, index: int) -> dict:
    """Reduce one session log to counts plus verbatim human messages."""
    warnings: list[dict] = []
    sources: dict[str, dict] = {}
    workspace = sess.get("workspace")
    if isinstance(workspace, Path):
        try:
            _workspace_content, provenance, source_warnings = session_evidence.capture_file(
                workspace)
            sources["workspace"] = provenance
            warnings.extend({**warning, "source": "workspace"}
                            for warning in source_warnings)
        except OSError:
            warnings.append({"code": "source_unreadable", "source": "workspace"})
    try:
        content, provenance, source_warnings = session_evidence.capture_file(sess["log"])
        sources["events"] = provenance
        warnings.extend({**warning, "source": "events"} for warning in source_warnings)
    except OSError:
        return {"index": index, "id": sess.get("id", ""), "error": "unreadable",
                "sources": sources,
                "warnings": warnings + [{"code": "source_unreadable",
                                           "source": "events"}]}
    recs = _parse_records(content, warnings)

    turns = 0
    cost = 0.0
    model = ""
    persona = "unattributed"
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
        if t == "subagent.selected":
            selected = d.get("agentName") or d.get("agent_name")
            if isinstance(selected, str) and selected.strip():
                persona = selected.strip()
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
            kit_match = KIT_CMD_RE.search(blob)
            if kit_match:
                command = " ".join(kit_match.group(1).lower().split())
                cmds[f"kit {command}"] += 1
            else:
                python_match = PYTHON_CMD_RE.search(blob)
                if python_match:
                    cmds[("python " + python_match.group(1)).strip()] += 1
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
        "updated": sess.get("updated", ""),
        "turns": turns,
        "cost": round(cost, 2),
        "model": model,
        "persona": persona,
        "gate_fails": dict(gate_fails.most_common(6)),
        "gate_passes": gate_passes,
        # Two public-workflow invocations are already a repeated user-facing
        # step (including Windows/Unix launcher spellings normalized above).
        # Internal scripts retain the higher threshold so one diagnostic
        # rerun does not crowd the retrospective with implementation noise.
        "loops": {
            c: n for c, n in cmds.most_common(6)
            if n >= (2 if c.startswith("kit ") else 3)
        },
        "rewrites": {f: n for f, n in writes.most_common(8) if n >= 3},
        "human": _human_messages(recs, warnings),
        "raw_bytes": len(content),
        "sources": sources,
        "warnings": warnings,
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
        for warning in d.get("warnings", []):
            count = warning.get("count", 1)
            lines.append(f"  WARN {warning['code']} x{count}")
        for i, msg in enumerate(d["human"], 1):
            lines.append(f'  S{d["index"]}:H{i} "{msg}"')
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Token-dense digest of agent sessions for this repo.")
    ap.add_argument("--sessions", help="override session-state directory")
    ap.add_argument("--limit", type=int, help="most recent N sessions")
    ap.add_argument("--since", help="ISO date, sessions updated on or after")
    ap.add_argument("--list", action="store_true", help="discovery only, no parsing")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--repo", help="repo path to match (default: this repo)")
    ap.add_argument("--snapshot", metavar="DIR",
                    help="atomically write a content-addressed reduced-evidence manifest")
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
    snapshot_path: Path | None = None
    if a.snapshot:
        manifest = session_evidence.build_manifest(repo, digests)
        snapshot_path = session_evidence.write_manifest(Path(a.snapshot), manifest)
    if a.json:
        print(json.dumps(digests, indent=2))
        if snapshot_path:
            print(f"snapshot: {snapshot_path}", file=sys.stderr)
        return 0

    text = render(digests)
    raw = sum(d.get("raw_bytes", 0) for d in digests)
    print(text, end="")
    if raw:
        print(f"\n# {len(digests)} session(s)  {raw//1024}KB raw -> {len(text)//1024 or 1}KB digest"
              f"  ({raw // max(len(text), 1)}:1)", file=sys.stderr)
    if snapshot_path:
        print(f"snapshot: {snapshot_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
