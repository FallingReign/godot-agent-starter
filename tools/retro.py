#!/usr/bin/env python3
"""Retrospective evidence pack over immutable repository-scoped capture.

Reconstructs what happened during a slice from reduced session evidence,
unarchived notes, git history, kit artefacts, and gate logs.

The evidence pack is the deliverable. A model reading it is one adapter.
The public workflow is ``kit retro status``, ``kit retro run`` and
``kit retro publish``; this module's flags are private implementation details.

The pack never contains a conclusion. It contains counts, sequences and
verbatim quotes, so a reader can disagree with it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RETRO_DIR = ROOT / "docs" / "retro"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_digest  # noqa: E402
import session_evidence  # noqa: E402
import runtime_paths  # noqa: E402

_RUNTIME = runtime_paths.resolve(ROOT)
EVIDENCE_DIR = _RUNTIME.evidence
PROMPT_DIR = _RUNTIME.retro_sdk
THREAD_FILE = _RUNTIME.retro_thread
RAN_FILE = _RUNTIME.retro_ran


def sh(*args: str, cwd: Path | None = None) -> str:
    try:
        r = subprocess.run(
            args, cwd=str(cwd or ROOT), capture_output=True, text=True, timeout=30
        )
        return r.stdout.strip()
    except Exception:
        return ""


def _short(text: str, limit: int = 200) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


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

def _session_pack(args) -> tuple[list[dict], dict, Path, int]:
    """Capture the exact reduced session evidence used by this retro."""
    roots = [Path(args.sessions)] if args.sessions else None
    found = session_digest.discover(ROOT, roots)
    selected = found[-args.max_sessions:]
    digests = [session_digest.digest_one(session, index)
               for index, session in enumerate(selected, 1)]
    manifest = session_evidence.build_manifest(ROOT, digests)
    snapshot = session_evidence.write_manifest(EVIDENCE_DIR / "sessions", manifest)

    sessions: list[dict] = []
    for source in manifest["sessions"]:
        evidence = source["evidence"]
        sessions.append({
            "tag": f"S{source['ordinal']}",
            "session_id": source["session_id"],
            "started": source["started"],
            "updated": source["updated"],
            "model": evidence["model"],
            "persona": evidence.get("persona", "unattributed"),
            "turns": evidence["turns"],
            "cost": evidence["cost"],
            "stage_failures": evidence["gate_fails"],
            "gate_passes": evidence["gate_passes"],
            "command_loops": [
                {"cmd": command, "count": count}
                for command, count in evidence["loops"].items()
            ],
            "file_rewrites": [
                {"file": path, "count": count}
                for path, count in evidence["rewrites"].items()
            ],
            "human_messages": [
                {"citation": f"S{source['ordinal']}:H{message_index}",
                 "source_citation": message["citation"], "text": message["text"],
                 "hints": []}
                for message_index, message in enumerate(
                    evidence["human_messages"], 1
                )
            ],
            "warnings": source["warnings"],
            "sources": source["sources"],
        })
    return sessions, manifest, snapshot, len(found)


def _note_evidence() -> list[dict]:
    notes: list[dict] = []
    notes_dir = RETRO_DIR / "notes"
    for path in sorted(notes_dir.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        try:
            content = path.read_bytes()
            text = content.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        notes.append({
            "path": path.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
            "text": text,
        })
    return notes


def build_pack(args) -> dict:
    sessions, _manifest, snapshot, sessions_found = _session_pack(args)
    baseline = args.baseline
    if not baseline:
        prop = ROOT / "proposal.json"
        if prop.exists():
            try:
                baseline = (json.loads(prop.read_text(encoding="utf-8")) or {}).get("baseline_sha") or None
            except Exception:
                baseline = None
    return {
        "schema": 2,
        "kind": "retro-evidence-pack",
        "repository": ROOT.name,
        "session_snapshot": {
            "path": snapshot.relative_to(ROOT).as_posix(),
            "sha256": snapshot.stem,
        },
        "sessions_found": sessions_found,
        "sessions": sessions,
        "notes": _note_evidence(),
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
    if not RAN_FILE.exists():
        return False
    try:
        value = json.loads(RAN_FILE.read_text(encoding="utf-8"))
        return key in value.get("slices", []) if isinstance(value, dict) else False
    except (OSError, ValueError):
        return False


def mark_ran(key: str) -> None:
    slices: list[str] = []
    if RAN_FILE.exists():
        try:
            value = json.loads(RAN_FILE.read_text(encoding="utf-8"))
            if isinstance(value, dict) and isinstance(value.get("slices"), list):
                slices = [str(item) for item in value["slices"] if str(item)]
        except (OSError, ValueError):
            slices = []
    if key not in slices:
        slices.append(key)
    _atomic_json_write(RAN_FILE, {"schema": 1, "slices": slices})


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


PROMPT = """Run a retrospective over the immutable evidence pack at {pack}.
It contains reduced, repository-scoped session evidence, unarchived slice
notes, Git facts, and gate facts. It contains no conclusions. Read every note
and every sessions[].human_messages entry. Each message has a citation such as
S2:H4 that is stable within the pack named above.

Find defects in the KIT, not mistakes to blame on one worker. A useful finding
states the underlying decision or workflow failure, a small proposed
experiment, and an observable measure. Repetition strengthens a finding, but a
single work-destroying or false-green event may stand alone when labelled
honestly. Do not promote generic agent advice, game design, or game code.

You may read repository files to validate a claim. You may not write files.
Return JSON only with this exact shape:

{{
  "slice": "<slice name or unknown>",
  "summary": "<at most two decision-relevant sentences>",
  "findings": [
    {{
      "title": "<short causal title>",
      "sessions": ["S1", "S2"],
      "human_turns": ["S1:H2", "S2:H4"],
      "mechanical": ["LOOP python check.py x4 (S1)"],
      "recurs": true,
      "severity": "work-destroyed|false-green|wrong-built|none",
      "fix_files": ["tools/example.py"],
      "fix_lines": 20,
      "problem": "<cause, not symptom>",
      "proposal": "<small concrete kit change to test>",
      "measure": "<observable evidence that would show improvement>"
    }}
  ],
  "observations": ["<suspected issue that lacks enough evidence to promote>"]
}}

Every human_turn citation must exist in the pack and support the finding. Every
session named must contribute cited evidence. Do not invent a file or exact
line estimate when repository inspection cannot support it; use an empty
fix_files list and a conservative fix_lines estimate. If there is no supported
kit finding, return an empty findings array. Do not include prose outside the
JSON object."""


def emit_print(pack: dict, pack_path: Path) -> None:
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    prompt_path = PROMPT_DIR / "prompt.md"
    _atomic_text_write(
        prompt_path,
        PROMPT.format(pack=pack_path.relative_to(ROOT).as_posix()),
    )
    print(f"evidence pack   {pack_path.relative_to(ROOT).as_posix()}")
    print(f"prompt          {prompt_path.relative_to(ROOT).as_posix()}")
    print()
    print("Hand both files to the analyzer you choose. When its findings report is")
    print("under docs/retro/, validate and publish it with: kit retro publish")
    print("For the configured automatic analyzer, use: kit retro run --confirm-spend")


def write_pack(pack: dict) -> Path:
    """Write an immutable pack plus a small mutable pointer for diagnostics."""
    pack_path = session_evidence.write_manifest(EVIDENCE_DIR / "packs", pack)
    pointer = EVIDENCE_DIR / "latest.json"
    value = {
        "schema": 1,
        "latest": pack_path.relative_to(ROOT).as_posix(),
        "sha256": pack_path.stem,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _atomic_json_write(pointer, value)
    return pack_path


def _atomic_text_write(path: Path, text: str) -> None:
    """Publish one complete private text artifact in its final directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json_write(path: Path, value: object) -> None:
    _atomic_text_write(path, json.dumps(value, indent=2) + "\n")


def archive_notes(pack: dict) -> tuple[bool, str]:
    """Archive exactly the note bytes captured by this successful retro."""
    archive = RETRO_DIR / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    for note in pack.get("notes") or []:
        source = ROOT / str(note.get("path") or "")
        try:
            source.resolve().relative_to((RETRO_DIR / "notes").resolve())
        except ValueError:
            return False, f"unsafe note path in evidence: {source}"
        if not source.is_file():
            return False, f"captured note disappeared before archive: {source.name}"
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != note.get("sha256"):
            return False, f"captured note changed before archive: {source.name}"
        target = archive / source.name
        if target.exists():
            if target.read_bytes() != content:
                return False, f"archive already contains different {source.name}"
            source.unlink()
        else:
            os.replace(source, target)
    return True, ""


def finalise_retro(pack: dict, pack_path: Path) -> int:
    """Rank the generated report, then consume only its captured notes."""
    reports = sorted(RETRO_DIR.glob(f"*-{pack_path.stem[:10]}-findings.md"))
    if not reports:
        print("retro analyzer produced no validated findings report")
        return 1
    report = reports[-1]
    ranked = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "retro_rank.py"), str(report)],
        cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )
    if ranked.returncode != 0:
        print("ranking failed; notes were not archived")
        print((ranked.stderr or ranked.stdout)[-1500:])
        return 1
    ok, error = archive_notes(pack)
    if not ok:
        print(f"notes were not archived: {error}")
        return 1
    subprocess.run(
        [sys.executable, str(ROOT / "tools" / "retro_html.py"), "--no-board"],
        cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )
    return 0


def summarise(pack: dict) -> None:
    print(f"sessions        {len(pack['sessions'])} analysed of {pack['sessions_found']} found")
    for s in pack["sessions"]:
        aiu = float(s.get("cost") or 0.0)
        label = s.get("model") or "unknown model"
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
            print(f"      human messages: {len(s['human_messages'])}"
                  f" ({len(hinted)} hint at friction)")
        if s.get("warnings"):
            print(f"      capture warnings: {len(s['warnings'])}")
    print(f"notes           {len(pack.get('notes') or [])} unarchived")
    print(f"snapshot        {pack['session_snapshot']['path']}")
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
                    help="call the explicitly configured analyzer provider")
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
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    pack = build_pack(args)
    pack_path = write_pack(pack)

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
        result = run_sdk(
            pack_path,
            PROMPT,
            RETRO_DIR,
            root=ROOT,
            work_dir=PROMPT_DIR,
            thread_file=THREAD_FILE,
        )
        if result != 0:
            return result
        return finalise_retro(pack, pack_path)
    emit_print(pack, pack_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
