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
import stat
import subprocess
import sys
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
RETRO_DIR = ROOT / "docs" / "retro"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import session_digest  # noqa: E402
import session_evidence  # noqa: E402
import runtime_paths  # noqa: E402
import retro_due  # noqa: E402

_RUNTIME = runtime_paths.resolve(ROOT)
EVIDENCE_DIR = _RUNTIME.evidence
PROMPT_DIR = _RUNTIME.retro_sdk
THREAD_FILE = _RUNTIME.retro_thread
RAN_FILE = _RUNTIME.retro_ran
COMPLETION_LOCK = _RUNTIME.retro_completion_lock
_EVIDENCE_PACK_LINE_RE = re.compile(r"^evidence_pack:\s*(\S+)\s*$", re.M)
_SESSION_SNAPSHOT_LINE_RE = re.compile(r"^session_snapshot:\s*(\S+)\s*$", re.M)


def _pid_alive(pid: object) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


@contextmanager
def _completion_guard():
    """Serialize note archive and completion-marker publication across processes."""
    COMPLETION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    for _attempt in range(40):
        try:
            descriptor = os.open(
                str(COMPLETION_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
            try:
                os.write(descriptor, (json.dumps({
                    "pid": os.getpid(), "at": datetime.now(timezone.utc).isoformat()
                }) + "\n").encode("utf-8"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            acquired = True
            break
        except FileExistsError:
            try:
                owner = json.loads(COMPLETION_LOCK.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                owner = {}
            if not _pid_alive(owner.get("pid")):
                try:
                    COMPLETION_LOCK.unlink()
                    continue
                except OSError:
                    pass
            time.sleep(0.05)
    if not acquired:
        raise ValueError("another retrospective completion is in progress")
    try:
        yield
    finally:
        COMPLETION_LOCK.unlink(missing_ok=True)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


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
        safe_errors = [session_evidence.redact_text(e)[0] for e in errs]
        out[p.stem] = {
            "bytes": len(text),
            "error_lines": [_short(e, 200) for e in safe_errors],
        }
    return out


# ------------------------------------------------------------------ pack + emit

def _session_pack(args) -> tuple[list[dict], dict, Path, int]:
    """Capture the exact reduced session evidence used by this retro."""
    roots = [Path(args.sessions)] if args.sessions else None
    since = getattr(args, "since", None) or None
    limit = getattr(args, "limit", None)
    found = session_digest.discover(ROOT, roots, since=since, limit=limit)
    digests = [session_digest.digest_one(session, index)
               for index, session in enumerate(found, 1)]
    try:
        manifest = session_evidence.build_manifest(
            ROOT, digests, since=since, limit=limit
        )
    except ValueError as exc:
        raise session_digest.SessionIngestionError(
            "invalid_capture_window", str(exc)
        ) from exc
    snapshot = session_evidence.write_manifest(EVIDENCE_DIR / "sessions", manifest)

    sessions: list[dict] = []
    for source in manifest["sessions"]:
        evidence = source["evidence"]
        packed = {
            "tag": f"S{source['ordinal']}",
            "provider": source.get("provider", "copilot"),
            "session_id": source["session_id"],
            "parent_session_id": source.get("parent_session_id", ""),
            "started": source["started"],
            "updated": source["updated"],
            "model": evidence["model"],
            "persona": evidence.get("persona", "unattributed"),
            "turns": evidence["turns"],
            "usage": evidence.get("usage") or {
                "source": "provider_reported", "metrics": [],
                "monetary_cost": None,
            },
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
        }
        if source.get("error"):
            packed["error"] = source["error"]
        sessions.append(packed)
    return sessions, manifest, snapshot, len(found)


def _session_capture_state(manifest: dict) -> dict:
    failures = [
        {
            "tag": f"S{source.get('ordinal', index)}",
            "provider": source.get("provider", "unknown"),
            "session_id": source.get("session_id", ""),
            "error": source.get("error", ""),
        }
        for index, source in enumerate(manifest.get("sessions") or [], 1)
        if isinstance(source, dict) and source.get("error")
    ]
    return {
        "complete": not failures,
        "window": manifest.get("capture_window"),
        "failures": failures,
    }


def _require_complete_session_capture(pack: dict, manifest: dict | None = None) -> None:
    """Refuse analysis or publication when any selected session failed capture."""
    state = pack.get("session_capture")
    if not isinstance(state, dict) or state.get("complete") is not True:
        failures = state.get("failures") if isinstance(state, dict) else []
        reasons = [
            f"{item.get('tag', '?')}={item.get('error', 'capture_failed')}"
            for item in (failures if isinstance(failures, list) else [])
            if isinstance(item, dict)
        ]
        detail = "; ".join(reasons) or "capture completeness is not proven"
        raise ValueError(f"retrospective session evidence is incomplete: {detail}")
    if manifest is None:
        return
    expected = _session_capture_state(manifest)
    if state != expected:
        raise ValueError(
            "retrospective evidence pack completeness does not match its session snapshot"
        )


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
    completion_key = slice_key()
    sessions, manifest, snapshot, sessions_found = _session_pack(args)
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
        "completion_key": completion_key,
        "session_snapshot": {
            "path": snapshot.relative_to(ROOT).as_posix(),
            "sha256": snapshot.stem,
        },
        "session_capture": _session_capture_state(manifest),
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


def _completion_record(key: str, pack_path: Path) -> dict[str, str]:
    """Return the exact private pack identity exposed by a completion marker."""
    resolved_pack = pack_path.resolve(strict=True)
    packs_root = (EVIDENCE_DIR / "packs").resolve(strict=True)
    resolved_pack.relative_to(packs_root)
    if resolved_pack.parent != packs_root:
        raise ValueError("completed retrospective pack is outside the pack store")
    digest = hashlib.sha256(resolved_pack.read_bytes()).hexdigest()
    if (not re.fullmatch(r"[0-9a-f]{64}\.json", resolved_pack.name)
            or digest != resolved_pack.stem):
        raise ValueError("completed retrospective pack is not content-addressed")
    relative = resolved_pack.relative_to(ROOT.resolve(strict=True)).as_posix()
    return {
        "slice": key,
        "evidence_pack": relative,
        "sha256": digest,
    }


def _completion_is_visible(key: str, pack_path: Path) -> bool:
    """Whether the exact slice/pack pair already has a durable marker."""
    if not RAN_FILE.exists():
        return False
    try:
        value = json.loads(RAN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"retrospective completion marker is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("retrospective completion marker must be an object")
    completed = value.get("completed_snapshots")
    if completed is None:
        return False
    if not isinstance(completed, list):
        raise ValueError("retrospective completion snapshots must be a list")
    return _completion_record(key, pack_path) in completed


def _completion_payload(key: str, pack_path: Path) -> dict:
    """Bind a completed slice to the exact immutable pack that was published."""
    slices: list[str] = []
    completed: list[dict[str, str]] = []
    if RAN_FILE.exists():
        try:
            value = json.loads(RAN_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"retrospective completion marker is unreadable: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError("retrospective completion marker must be an object")
        existing_slices = value.get("slices", [])
        existing_completed = value.get("completed_snapshots", [])
        if not isinstance(existing_slices, list):
            raise ValueError("retrospective completion slices must be a list")
        if not isinstance(existing_completed, list):
            raise ValueError("retrospective completion snapshots must be a list")
        slices = [str(item) for item in existing_slices if str(item)]
        if existing_completed:
            try:
                completed = [
                    item for item in existing_completed
                    if isinstance(item, dict)
                    and isinstance(item.get("slice"), str)
                    and isinstance(item.get("evidence_pack"), str)
                    and isinstance(item.get("sha256"), str)
                ]
            except AttributeError as exc:
                raise ValueError(
                    "retrospective completion snapshots contain an invalid record"
                ) from exc
            if len(completed) != len(existing_completed):
                raise ValueError(
                    "retrospective completion snapshots contain an invalid record"
                )

    record = _completion_record(key, pack_path)
    if key not in slices:
        slices.append(key)
    if record not in completed:
        completed.append(record)
    return {
        "schema": 2,
        "slices": slices,
        "completed_snapshots": completed,
    }


def _stage_ran(key: str, pack_path: Path) -> Path:
    """Durably stage a completion record without making it visible yet."""
    value = _completion_payload(key, pack_path)
    RAN_FILE.parent.mkdir(parents=True, exist_ok=True)
    staged = RAN_FILE.with_name(
        f".{RAN_FILE.name}.{uuid.uuid4().hex}.pending"
    )
    try:
        with staged.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        staged.unlink(missing_ok=True)
        raise
    return staged


def _commit_ran(staged: Path) -> None:
    os.replace(staged, RAN_FILE)


def mark_ran(key: str, pack_path: Path) -> None:
    with _completion_guard():
        staged = _stage_ran(key, pack_path)
        try:
            _commit_ran(staged)
        finally:
            staged.unlink(missing_ok=True)


def should_trigger(pack: dict) -> tuple[bool, str]:
    """Decide whether an automatic retro is warranted.

    Deliberately conservative. A retro that fires twice spends twice, and a
    runaway loop is the exact failure this kit has already produced once.
    """
    reasons: list[str] = []
    note_signals = retro_due.signals(pack.get("notes") or [])
    if note_signals["immediate_consequences"]:
        codes = ", ".join(item["code"] for item in note_signals[
            "immediate_consequences"])
        reasons.append(f"immediate consequence: {codes}")
    if note_signals["prompt_triggers"]:
        codes = ", ".join(item["code"] for item in note_signals["prompt_triggers"])
        reasons.append(f"prompt trigger: {codes}")
    # Explicit new testimony is never suppressed by an earlier completion
    # marker for the same proposal slice.
    if reasons:
        return True, "; ".join(reasons)

    key = slice_key()
    if already_ran(key):
        return False, f"already ran for {key}"

    prop = (pack.get("artefacts") or {}).get("proposal") or {}

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
      "mechanical": ["LOOP kit verify x4 (S1)"],
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
    _require_complete_session_capture(pack)
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    prompt_path = PROMPT_DIR / "prompt.md"
    _atomic_text_write(
        prompt_path,
        PROMPT.format(pack=pack_path.relative_to(ROOT).as_posix()),
    )
    print(f"evidence pack   {pack_path.relative_to(ROOT).as_posix()}")
    print(f"prompt          {prompt_path.relative_to(ROOT).as_posix()}")
    quote_count = sum(
        len(session.get("human_messages") or [])
        for session in (pack.get("sessions") or [])
        if isinstance(session, dict)
    )
    print(f"human quotes    {quote_count} redacted excerpt(s); review the private pack before sharing")
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


def _single_header_value(pattern: re.Pattern[str], text: str, label: str) -> str:
    matches = pattern.findall(text)
    if len(matches) != 1:
        raise ValueError(f"findings report must contain exactly one {label} line")
    return matches[0]


def _content_addressed_json(relative: str, allowed_dir: Path) -> tuple[dict, Path]:
    """Read one canonical hash-named JSON file from an exact private store."""
    if (not relative or "\\" in relative
            or any(ord(char) < 32 for char in relative)):
        raise ValueError(f"unsafe evidence path: {relative!r}")
    lexical = PurePosixPath(relative)
    if (lexical.is_absolute() or not lexical.parts
            or any(part in ("", ".", "..") for part in lexical.parts)
            or ":" in lexical.parts[0]):
        raise ValueError(f"unsafe evidence path: {relative!r}")

    candidate = ROOT.joinpath(*lexical.parts)
    try:
        root_resolved = ROOT.resolve(strict=True)
        allowed_resolved = allowed_dir.resolve(strict=True)
        allowed_resolved.relative_to(root_resolved)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(allowed_resolved)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(f"evidence path is outside its private store: {relative!r}") from exc
    if resolved.parent != allowed_resolved:
        raise ValueError(f"evidence path is outside its private store: {relative!r}")

    cursor = ROOT
    for part in lexical.parts:
        cursor /= part
        if cursor.is_symlink():
            raise ValueError(f"symlinked evidence path: {relative!r}")
    if not re.fullmatch(r"[0-9a-f]{64}\.json", resolved.name):
        raise ValueError(f"evidence is not content-addressed: {relative!r}")
    try:
        content = resolved.read_bytes()
        value = json.loads(content.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"cannot read evidence {relative!r}: {exc}") from exc
    if hashlib.sha256(content).hexdigest() != resolved.stem:
        raise ValueError(f"evidence hash mismatch: {relative!r}")
    if not isinstance(value, dict) or session_evidence.canonical_bytes(value) != content:
        raise ValueError(f"evidence is not canonical: {relative!r}")
    return value, resolved


def _snapshot_citation_index(manifest: dict) -> tuple[set[str], set[str]]:
    """Validate a snapshot's citation records and return its public indexes."""
    sessions = manifest.get("sessions")
    if not isinstance(sessions, list):
        raise ValueError("retrospective session snapshot has invalid sessions")
    tags: set[str] = set()
    citations: set[str] = set()
    for ordinal, session in enumerate(sessions, 1):
        if (not isinstance(session, dict) or session.get("ordinal") != ordinal
                or not isinstance(session.get("session_id"), str)
                or not session.get("session_id")
                or not isinstance(session.get("evidence"), dict)):
            raise ValueError("retrospective session snapshot has invalid session records")
        tag = f"S{ordinal}"
        tags.add(tag)
        messages = session["evidence"].get("human_messages")
        if not isinstance(messages, list):
            raise ValueError("retrospective session snapshot has invalid human messages")
        for number, message in enumerate(messages, 1):
            source_citation = f"{session['session_id']}:H{number}"
            if (not isinstance(message, dict)
                    or message.get("citation") != source_citation
                    or not isinstance(message.get("text"), str)):
                raise ValueError(
                    "retrospective session snapshot has invalid human messages"
                )
            citations.add(f"{tag}:H{number}")
    return tags, citations


def _pack_matches_snapshot(pack: dict, manifest: dict) -> None:
    """Prove the analyzer pack did not rebind the snapshot's human evidence."""
    _require_complete_session_capture(pack, manifest)
    pack_sessions = pack.get("sessions")
    snapshot_sessions = manifest.get("sessions")
    if (not isinstance(pack_sessions, list)
            or not isinstance(snapshot_sessions, list)
            or len(pack_sessions) != len(snapshot_sessions)):
        raise ValueError("retrospective evidence pack sessions do not match its snapshot")
    for ordinal, (packed, source) in enumerate(
            zip(pack_sessions, snapshot_sessions), 1):
        if (not isinstance(packed, dict) or not isinstance(source, dict)
                or packed.get("tag") != f"S{ordinal}"
                or packed.get("session_id") != source.get("session_id")
                or packed.get("error") != source.get("error")):
            raise ValueError(
                "retrospective evidence pack sessions do not match its snapshot"
            )
        packed_messages = packed.get("human_messages")
        evidence = source.get("evidence")
        source_messages = evidence.get("human_messages") if isinstance(evidence, dict) else None
        if (not isinstance(packed_messages, list)
                or not isinstance(source_messages, list)
                or len(packed_messages) != len(source_messages)):
            raise ValueError(
                "retrospective evidence pack human messages do not match its snapshot"
            )
        for number, (message, source_message) in enumerate(
                zip(packed_messages, source_messages), 1):
            if (not isinstance(message, dict) or not isinstance(source_message, dict)
                    or message.get("citation") != f"S{ordinal}:H{number}"
                    or message.get("source_citation") != source_message.get("citation")
                    or message.get("text") != source_message.get("text")):
                raise ValueError(
                    "retrospective evidence pack human messages do not match its snapshot"
                )


def _validate_completed_findings(text: str, manifest: dict) -> None:
    """Require a ranked, decision-ready report bound to real snapshot turns."""
    # Imported lazily because retro_rank imports the evidence modules too.
    import retro_rank  # noqa: PLC0415

    _header, findings = retro_rank.parse_findings(text)
    if not findings:
        raise ValueError("findings report has no completed finding blocks")
    if len(findings) > 50:
        raise ValueError("findings report exceeds the 50 item limit")
    valid_sessions, valid_turns = _snapshot_citation_index(manifest)
    titles: set[str] = set()
    for index, finding in enumerate(findings, 1):
        title = str(finding.get("title") or "").strip()
        prefix = f"finding {index}"
        normalised_title = retro_rank.normalise_title(title)
        if not normalised_title:
            raise ValueError(f"{prefix} has no title")
        if normalised_title in titles:
            raise ValueError(f"{prefix} duplicates an earlier title")
        titles.add(normalised_title)

        sections = retro_rank.extract_sections(str(finding.get("body") or ""))
        missing = [name for name in ("problem", "proposal", "measure")
                   if not sections.get(name)]
        if missing:
            raise ValueError(f"{prefix} is missing {', '.join(missing)}")
        if finding.get("cost") is None or finding.get("effort") is None:
            raise ValueError(f"{prefix} has not been ranked and published")

        turns = finding.get("human_turns")
        sessions = finding.get("sessions")
        if not isinstance(turns, list) or not turns:
            raise ValueError(f"{prefix} has no human-turn evidence")
        if not isinstance(sessions, list) or not sessions:
            raise ValueError(f"{prefix} has no session evidence")
        cited_sessions: set[str] = set()
        for citation in turns:
            if not isinstance(citation, str) or re.fullmatch(r"S\d+:H\d+", citation) is None:
                raise ValueError(f"{prefix} has malformed human-turn citation {citation!r}")
            if citation not in valid_turns:
                raise ValueError(f"{prefix} cites unknown human turn {citation!r}")
            cited_sessions.add(citation.split(":", 1)[0])
        named_sessions = {str(session) for session in sessions}
        unknown_sessions = named_sessions - valid_sessions
        if unknown_sessions:
            raise ValueError(
                f"{prefix} names unknown sessions {sorted(unknown_sessions)}"
            )
        if named_sessions != cited_sessions:
            raise ValueError(f"{prefix} sessions do not exactly match its human turns")


def _bound_report_pack(report_path: Path) -> tuple[dict, Path, Path, bytes]:
    """Validate a completed report's exact pack and session-snapshot binding."""
    candidate = report_path if report_path.is_absolute() else ROOT / report_path
    try:
        report = candidate.resolve(strict=True)
        report.relative_to(RETRO_DIR.resolve(strict=True))
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("retrospective findings must be under docs/retro") from exc
    if not report.is_file() or not report.name.endswith("-findings.md"):
        raise ValueError("retrospective report name must end with -findings.md")
    try:
        report_bytes = report.read_bytes()
        text = report_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"cannot read retrospective findings: {exc}") from exc

    pack_relative = _single_header_value(
        _EVIDENCE_PACK_LINE_RE, text, "evidence_pack")
    snapshot_relative = _single_header_value(
        _SESSION_SNAPSHOT_LINE_RE, text, "session_snapshot")
    pack, pack_path = _content_addressed_json(
        pack_relative, EVIDENCE_DIR / "packs")
    if (pack.get("schema") != 2
            or pack.get("kind") != "retro-evidence-pack"):
        raise ValueError("findings report names an unexpected evidence pack kind")
    if pack.get("repository") != ROOT.name:
        raise ValueError("retrospective evidence repository provenance mismatch")

    completion_key = pack.get("completion_key")
    if (not isinstance(completion_key, str) or not completion_key.strip()
            or len(completion_key) > 512
            or any(ord(char) < 32 for char in completion_key)):
        raise ValueError("retrospective evidence pack has no bound completion key")
    snapshot = pack.get("session_snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("retrospective evidence pack has no session snapshot")
    if snapshot.get("path") != snapshot_relative:
        raise ValueError("findings session snapshot does not match its evidence pack")
    manifest, snapshot_path = _content_addressed_json(
        snapshot_relative, EVIDENCE_DIR / "sessions")
    if snapshot.get("sha256") != snapshot_path.stem:
        raise ValueError("retrospective session snapshot identity mismatch")

    current = (manifest.get("schema") == session_evidence.SCHEMA
               and manifest.get("kind") == session_evidence.KIND)
    legacy = (manifest.get("schema") == session_evidence.LEGACY_SCHEMA
              and manifest.get("kind") == session_evidence.LEGACY_KIND)
    if not current and not legacy:
        raise ValueError("retrospective session snapshot kind is unsupported")
    if current:
        repository = manifest.get("repository")
        matches_repository = (
            isinstance(repository, dict)
            and repository.get("scope_id")
            == session_evidence.repository_scope(ROOT)["scope_id"]
        )
    else:
        canonical = os.path.normcase(os.path.abspath(ROOT)).replace(
            "\\", "/").rstrip("/")
        matches_repository = manifest.get("repository") == canonical
    if not matches_repository:
        raise ValueError("retrospective session snapshot repository provenance mismatch")
    _pack_matches_snapshot(pack, manifest)
    _validate_completed_findings(text, manifest)
    return pack, pack_path, report, report_bytes


def _regular_identity(path: Path) -> tuple[int, int, int, int] | None:
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if _is_link_or_reparse(path) or not stat.S_ISREG(info.st_mode):
        return None
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _captured_note_moves(
        pack: dict) -> tuple[
            list[tuple[Path, Path, bytes, bool, tuple[int, int, int, int]]], str
        ]:
    """Validate every captured note before mutating any of them."""
    archive = RETRO_DIR / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    notes_root = (RETRO_DIR / "notes").resolve(strict=True)
    archive_root = archive.resolve(strict=True)
    if _is_link_or_reparse(notes_root) or _is_link_or_reparse(archive_root):
        return [], "retrospective notes or archive root is linked/reparse content"
    moves: list[tuple[Path, Path, bytes, bool, tuple[int, int, int, int]]] = []
    for note in pack.get("notes") or []:
        if not isinstance(note, dict):
            return [], "invalid captured note record"
        source = ROOT / str(note.get("path") or "")
        try:
            resolved_source = source.resolve(strict=True)
            resolved_source.relative_to(notes_root)
        except (OSError, RuntimeError, ValueError):
            return [], f"unsafe note path in evidence: {source}"
        if resolved_source.parent != notes_root or source.parent.resolve() != notes_root:
            return [], f"captured note is not a direct notes child: {source.name}"
        identity = _regular_identity(source)
        if identity is None:
            return [], f"captured note is linked, reparsed or not regular: {source.name}"
        if not source.is_file():
            return [], f"captured note disappeared before archive: {source.name}"
        content = source.read_bytes()
        if hashlib.sha256(content).hexdigest() != note.get("sha256"):
            return [], f"captured note changed before archive: {source.name}"
        target = archive / source.name
        if target.exists():
            if target.parent.resolve() != archive_root or _regular_identity(target) is None:
                return [], f"archive target is linked, reparsed or not regular: {source.name}"
            if target.read_bytes() != content:
                return [], f"archive already contains different {source.name}"
        elif target.resolve(strict=False).parent != archive_root:
            return [], f"unsafe archive target: {target}"
        moves.append((source, target, content, target.exists(), identity))
    return moves, ""


def _restore_note_moves(
        moves: list[tuple[
            Path, Path, bytes, bool, tuple[int, int, int, int]
        ]]) -> str:
    errors: list[str] = []
    for source, target, content, target_preexisted, _identity in reversed(moves):
        try:
            if source.exists():
                if source.read_bytes() != content:
                    errors.append(f"{source.name} changed during rollback")
                continue
            if target_preexisted:
                source.write_bytes(content)
            elif target.exists() and target.read_bytes() == content:
                os.replace(target, source)
            else:
                errors.append(f"{source.name} could not be restored")
        except OSError as exc:
            errors.append(f"{source.name}: {exc}")
    return "; ".join(errors)


def _apply_note_moves(
        moves: list[tuple[
            Path, Path, bytes, bool, tuple[int, int, int, int]
        ]]) -> tuple[bool, str]:
    applied: list[tuple[
        Path, Path, bytes, bool, tuple[int, int, int, int]
    ]] = []
    try:
        for source, target, content, target_preexisted, identity in moves:
            if _regular_identity(source) != identity or source.read_bytes() != content:
                raise OSError(f"captured note changed during archive: {source.name}")
            if target_preexisted:
                source.unlink()
            else:
                os.replace(source, target)
            applied.append((source, target, content, target_preexisted, identity))
            if _regular_identity(target) is None or target.read_bytes() != content:
                raise OSError(f"archived note verification failed: {target.name}")
    except OSError as exc:
        rollback = _restore_note_moves(applied)
        detail = f"could not archive captured notes: {exc}"
        if rollback:
            detail += f"; rollback incomplete: {rollback}"
        return False, detail
    return True, ""


def archive_notes(pack: dict) -> tuple[bool, str]:
    """Archive all and only the exact note bytes captured by this retro."""
    moves, error = _captured_note_moves(pack)
    if error:
        return False, error
    return _apply_note_moves(moves)


def _captured_notes_are_archived(pack: dict) -> tuple[bool, str]:
    """Validate idempotent completion against the exact archived note bytes."""
    archive = RETRO_DIR / "archive"
    notes_root = (RETRO_DIR / "notes").resolve(strict=False)
    for note in pack.get("notes") or []:
        if not isinstance(note, dict):
            return False, "invalid captured note record"
        source = ROOT / str(note.get("path") or "")
        try:
            resolved_source = source.resolve(strict=False)
            resolved_source.relative_to(notes_root)
        except (OSError, RuntimeError, ValueError):
            return False, f"unsafe note path in evidence: {source}"
        if resolved_source.parent != notes_root or _is_link_or_reparse(source):
            return False, f"unsafe linked or nested note path in evidence: {source}"
        target = archive / source.name
        if _regular_identity(target) is None:
            return False, f"completed retrospective is missing archived {source.name}"
        try:
            content = target.read_bytes()
        except OSError as exc:
            return False, f"cannot read archived {source.name}: {exc}"
        if hashlib.sha256(content).hexdigest() != note.get("sha256"):
            return False, f"completed retrospective archive changed: {source.name}"
    return True, ""


def _publication_unchanged(publication: tuple[Path, bytes]) -> bool:
    """Whether the exact validated findings bytes are still published."""
    report_path, expected = publication
    try:
        return (not report_path.is_symlink()
                and report_path.is_file()
                and report_path.read_bytes() == expected)
    except OSError:
        return False


def _complete_retro_locked(
        pack: dict, pack_path: Path, completion_key: str, *,
        publication: tuple[Path, bytes] | None = None) -> tuple[bool, str]:
    """Archive one captured note set and atomically expose its exact marker."""
    if pack.get("completion_key") != completion_key:
        return False, "retrospective completion key is not bound to its evidence pack"
    try:
        content = pack_path.read_bytes()
        if session_evidence.canonical_bytes(pack) != content:
            return False, "retrospective evidence pack does not match captured bytes"
        if publication is not None and not _publication_unchanged(publication):
            return False, "validated retrospective findings changed before completion"
        if _completion_is_visible(completion_key, pack_path):
            archived, error = _captured_notes_are_archived(pack)
            if not archived:
                return False, error
            if publication is not None and not _publication_unchanged(publication):
                return False, "validated retrospective findings changed before completion"
            return True, "retrospective snapshot was already completed"
        moves, error = _captured_note_moves(pack)
        if error:
            return False, error
        staged = _stage_ran(completion_key, pack_path)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return False, f"retrospective completion marker could not be staged: {exc}"

    ok, error = _apply_note_moves(moves)
    if not ok:
        staged.unlink(missing_ok=True)
        return False, error
    if publication is not None and not _publication_unchanged(publication):
        rollback = _restore_note_moves(moves)
        staged.unlink(missing_ok=True)
        detail = "validated retrospective findings changed during completion"
        if rollback:
            detail += f"; note rollback incomplete: {rollback}"
        return False, detail
    try:
        _commit_ran(staged)
    except OSError as exc:
        rollback = _restore_note_moves(moves)
        detail = f"retrospective completion marker could not be published: {exc}"
        if rollback:
            detail += f"; note rollback incomplete: {rollback}"
        return False, detail
    finally:
        staged.unlink(missing_ok=True)
    return True, "retrospective snapshot completed"


def complete_retro(
        pack: dict, pack_path: Path, completion_key: str, *,
        publication: tuple[Path, bytes] | None = None) -> tuple[bool, str]:
    """Serialize and complete one exact retrospective transaction."""
    try:
        with _completion_guard():
            return _complete_retro_locked(
                pack,
                pack_path,
                completion_key,
                publication=publication,
            )
    except (OSError, ValueError) as exc:
        return False, f"retrospective completion lock unavailable: {exc}"


def complete_published_retro(report_path: Path) -> tuple[bool, str]:
    """Close an already validated and published manual findings report.

    The public orchestrator calls this only after ranking and both decision
    views succeed. This function performs no analysis or publication itself;
    it binds the report back to its immutable pack, archives exactly that
    pack's notes, and then exposes the completion marker.
    """
    try:
        pack, pack_path, published_report, published_bytes = _bound_report_pack(
            report_path
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return False, f"retrospective findings are not bound to evidence: {exc}"
    return complete_retro(
        pack,
        pack_path,
        pack["completion_key"],
        publication=(published_report, published_bytes),
    )


def finalise_retro(pack: dict, pack_path: Path, completion_key: str) -> int:
    """Publish, archive the captured notes, then expose one completion marker."""
    if pack.get("completion_key") != completion_key:
        print("retrospective completion identity changed after capture")
        return 1
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
    for label, command in (
        ("plan", [sys.executable, str(ROOT / "tools" / "plan_html.py")]),
        (
            "retrospective",
            [sys.executable, str(ROOT / "tools" / "retro_html.py"), "--no-board"],
        ),
    ):
        rendered = subprocess.run(
            command,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        if rendered.returncode != 0:
            print(f"{label} publication view failed")
            print((rendered.stderr or rendered.stdout)[-1500:])
            return 1

    ok, error = complete_published_retro(report)
    if not ok:
        print(f"retrospective was not completed: {error}")
        return 1
    return 0


def summarise(pack: dict) -> None:
    window = (pack.get("session_capture") or {}).get("window") or {}
    mode = window.get("mode") or "unknown-window"
    print(f"sessions        {len(pack['sessions'])} captured ({mode})")
    for s in pack["sessions"]:
        metrics = {
            item.get("name"): item.get("value")
            for item in ((s.get("usage") or {}).get("metrics") or [])
            if isinstance(item, dict)
        }
        usage = ""
        if metrics.get("aiu"):
            usage = f"  {float(metrics['aiu']):.2f} AIU"
        elif metrics.get("total_tokens"):
            usage = f"  {int(metrics['total_tokens'])} tokens"
        label = s.get("model") or "unknown model"
        provider = s.get("provider") or "unknown"
        print(f"  {s['started'][:16] or '?':16s}  {s['turns']:3d} turns"
              f"{usage}  {provider} / {label}")
        if s.get("error"):
            print(f"      CAPTURE FAILED: {s['error']}")
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
    ap.add_argument("--since", help="explicit ISO lower bound for session activity")
    ap.add_argument("--limit", type=int,
                    help="explicitly select the most recent N matching sessions")
    ap.add_argument("--if-warranted", action="store_true",
                    help="exit without acting unless a trigger fires (for hooks)")
    ap.add_argument("--force", action="store_true",
                    help="ignore the per-slice lock")
    ap.add_argument("--why", action="store_true",
                    help="report whether a retro is warranted and exit")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument(
        "--complete-published",
        metavar="REPORT",
        help=(
            "close the exact already-ranked and published findings snapshot; "
            "does not call a model"
        ),
    )
    args = ap.parse_args()

    if args.complete_published:
        conflicting = any(
            (
                args.do_print,
                args.sdk,
                bool(args.sessions),
                bool(args.baseline),
                bool(args.since),
                args.limit is not None,
                args.if_warranted,
                args.force,
                args.why,
            )
        )
        if conflicting:
            ap.error("--complete-published cannot be combined with evidence capture options")
        ok, detail = complete_published_retro(Path(args.complete_published))
        print(detail)
        return 0 if ok else 1

    RETRO_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        pack = build_pack(args)
    except session_digest.SessionIngestionError as exc:
        print(f"retrospective session evidence is incomplete: {exc}", file=sys.stderr)
        return 2
    pack_path = write_pack(pack)
    completion_key = pack["completion_key"]

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
    if not args.quiet:
        summarise(pack)
        print()

    try:
        _require_complete_session_capture(pack)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        print("no analyzer prompt was prepared and this pack cannot be published",
              file=sys.stderr)
        return 2

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
        return finalise_retro(pack, pack_path, completion_key)
    emit_print(pack, pack_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
