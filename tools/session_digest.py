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
  rewrites, turn counts, and provider-reported usage.

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
import stat
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import session_evidence
import project_context

TOOLS = Path(__file__).resolve().parent
CORE_ROOT = TOOLS.parent
ROOT = project_context.resolve_active_installation(CORE_ROOT).project_root

# Copilot keeps one directory per session, each with a workspace.yaml carrying
# the cwd. That file is small and declarative; the session database is an
# undocumented index whose schema can change. Prefer the yaml. Do not add a
# recursive fallback here: this function runs on the board request path.
COPILOT_SESSION_ROOTS = [
    Path.home() / ".copilot" / "session-state",
    Path(os.environ.get("APPDATA", "/nonexistent")) / "copilot" / "session-state",
]
CODEX_SESSION_ROOTS = [
    Path.home() / ".codex" / "sessions",
    Path.home() / ".codex" / "archived_sessions",
]
SESSION_ROOTS = COPILOT_SESSION_ROOTS + CODEX_SESSION_ROOTS

# Commands worth counting as a loop. A tool name is not a command -- "view" x25
# is meaningless, while repeatedly running the public gate or an internal script
# is evidence of friction. Keep historical Python recognition for older sessions.
KIT_CMD_RE = re.compile(
    r"(?<![\w.-])(?:\.{0,2}[\\/])?kit(?:\.cmd)?\s+"
    r"([a-z][a-z0-9_-]*(?:\s+[a-z][a-z0-9_-]*)?)",
    re.IGNORECASE,
)
GATE_FAIL_RE = re.compile(r"^\s*FAIL\s+(\w[\w-]*)", re.M)
GATE_PASS_RE = re.compile(r"GATE PASSED", re.M)
PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File:\s*(.+?)\s*$", re.M)
PUBLIC_KIT_COMMANDS = frozenset({
    "architecture update", "doctor", "friction", "godot-docs build",
    "godot-docs search", "godot-docs show", "integrity accept", "plan",
    "release build", "retro publish", "retro run", "retro status", "sanitize",
    "schema describe", "serve", "setup dependency", "setup format", "setup repair",
    "verify",
})
GATE_STAGES = frozenset({
    "assets", "arch", "conformance", "design", "format", "grep", "import",
    "integrity", "lint", "prerequisites", "resources", "sanitise", "schema",
    "shape", "skills", "tests", "typecheck", "types",
})
MAX_HUMAN_CHARS = 4000
MAX_CODEX_META_BYTES = 1024 * 1024
# Completeness limits are refusals, not selection knobs. A retrospective must
# never appear comprehensive after silently dropping older sessions or events.
MAX_DISCOVERED_SESSIONS = 4096
MAX_WORKSPACE_BYTES = 1024 * 1024
MAX_EVENT_BYTES = 128 * 1024 * 1024
MAX_EVENT_RECORDS = 250_000
CODEX_TOKEN_METRICS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


class SessionIngestionError(ValueError):
    """Complete repository evidence cannot be ingested inside a declared bound."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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


def _absolute_scope_key(value: object) -> str | None:
    """Return a comparison key only for an explicit absolute repository path.

    Provider metadata is untrusted scope evidence.  In particular, passing an
    empty value to :func:`_norm` would resolve it to this process's working
    directory and could admit an unrelated session by accident.  Validate the
    path shape before any normalization so missing, relative, drive-relative
    and home-relative values all fail closed.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("\\\\", "//")):
        if not ntpath.isabs(raw):
            return None
    elif raw.startswith("/"):
        if not posixpath.isabs(raw):
            return None
    else:
        return None
    return _norm(raw)


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


def _copilot_sessions(repo: str | Path, root: Path) -> list[dict]:
    """Find Copilot sessions using only direct workspace metadata."""
    want = _norm(repo)
    found: list[dict] = []
    if not root.is_dir():
        return found
    for wsf in sorted(root.glob("*/workspace.yaml"), key=_norm):
        try:
            txt = wsf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        workspace_scope = _absolute_scope_key(_yaml_get(txt, "cwd"))
        if workspace_scope is None or workspace_scope != want:
            continue
        log = _select_event_log(wsf.parent)
        if log is None:
            continue
        session_id = _yaml_get(txt, "id") or wsf.parent.name
        found.append({
            "id": session_id,
            "name": _yaml_get(txt, "name"),
            "started": _yaml_get(txt, "created_at"),
            "updated": _yaml_get(txt, "updated_at"),
            "workspace": wsf,
            "log": log,
            "kind": "copilot",
            "provider": "copilot",
            "repository": want,
            "locator": f"copilot/session-state/{wsf.parent.name}",
        })
    return found


def _codex_first_record(path: Path) -> dict | None:
    """Read only Codex's first record, which is the repository scope record."""
    try:
        with path.open("rb") as source:
            first = source.readline(MAX_CODEX_META_BYTES + 1)
    except OSError:
        return None
    if not first or len(first) > MAX_CODEX_META_BYTES:
        return None
    try:
        record = json.loads(first.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    return payload if isinstance(payload, dict) else None


def _codex_logs(root: Path) -> list[Path]:
    """Enumerate only the two official Codex rollout layouts, never rglob."""
    candidates = set(root.glob("rollout-*.jsonl"))
    candidates.update(root.glob("*/*/*/rollout-*.jsonl"))
    return sorted((path for path in candidates if path.is_file()), key=_norm)


def _codex_activity(path: Path, fallback: str) -> str:
    """Latest rollout activity from filesystem metadata, without reading a body."""
    try:
        modified = path.stat().st_mtime
    except OSError:
        return fallback
    return datetime.fromtimestamp(modified, timezone.utc).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def _codex_sessions(repo: str | Path, root: Path) -> list[dict]:
    """Find Codex sessions whose first session_meta record names *repo*."""
    want = _norm(repo)
    found: list[dict] = []
    if not root.is_dir():
        return found
    for log in _codex_logs(root):
        payload = _codex_first_record(log)
        if payload is None:
            continue
        session_scope = _absolute_scope_key(payload.get("cwd"))
        if session_scope is None or session_scope != want:
            continue
        session_id = payload.get("id") or payload.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            continue
        timestamp = payload.get("timestamp")
        if not isinstance(timestamp, str):
            timestamp = ""
        parent = payload.get("parent_thread_id")
        if not isinstance(parent, str):
            parent = ""
        archive = "archived" in root.name.lower()
        found.append({
            "id": session_id.strip(),
            "name": "",
            "started": timestamp,
            "updated": _codex_activity(log, timestamp),
            "parent_session_id": parent.strip(),
            "log": log,
            "kind": "codex",
            "provider": "codex",
            "repository": want,
            "locator": f"codex/{'archived_sessions' if archive else 'sessions'}/{log.name}",
        })
    return found


def discover(repo: str | Path, roots: list[Path] | None = None, *,
             since: str | None = None, limit: int | None = None) -> list[dict]:
    """Find repository-scoped sessions with only explicit completeness cuts."""
    found: dict[tuple[str, str], dict] = {}
    unique_roots = sorted({_norm(root): root for root in (roots or SESSION_ROOTS)}.values(),
                          key=_norm)
    for root in unique_roots:
        candidates = _copilot_sessions(repo, root) + _codex_sessions(repo, root)
        for candidate in candidates:
            key = (candidate["provider"], candidate["id"])
            current = found.get(key)
            candidate_activity = candidate["updated"] or candidate["started"] or ""
            current_activity = ((current or {}).get("updated")
                                or (current or {}).get("started") or "")
            if (current is None or candidate_activity > current_activity
                    or (candidate_activity == current_activity
                        and _norm(candidate["log"]) < _norm(current["log"]))):
                found[key] = candidate
    ordered = sorted(found.values(), key=lambda session: (
        session["updated"] or session["started"] or "",
        session["started"] or "",
        session["provider"],
        session["id"],
        _norm(session["log"]),
    ))
    if since:
        ordered = [
            session for session in ordered
            if (session["updated"] or session["started"] or "") >= since
        ]
    if limit is not None:
        if limit < 1:
            raise SessionIngestionError(
                "invalid_session_limit", "--limit must be a positive integer"
            )
        ordered = ordered[-limit:]
    if len(ordered) > MAX_DISCOVERED_SESSIONS:
        raise SessionIngestionError(
            "session_limit_exceeded",
            f"repository has {len(ordered)} matching sessions; complete capture limit "
            f"is {MAX_DISCOVERED_SESSIONS}. Narrow the explicit --since window or "
            "select an explicit --limit; "
            "no sessions were silently discarded",
        )
    return ordered


def _bounded_human(text: str, warnings: list[dict] | None = None) -> str:
    redacted, redaction_count = session_evidence.redact_text(text)
    if redaction_count and warnings is not None:
        warnings.append({"code": "sensitive_text_redacted",
                         "count": redaction_count})
    normalised = " ".join(redacted.strip().split())
    if len(normalised) > MAX_HUMAN_CHARS:
        normalised = normalised[:MAX_HUMAN_CHARS]
        if warnings is not None:
            warnings.append({"code": "human_message_truncated", "count": 1,
                             "max_chars": MAX_HUMAN_CHARS})
    return normalised


_INJECTED_XML_TAG = (
    r"environment_context|app-context|codex_internal_context|"
    r"permissions instructions|skills_instructions|in-app-browser-context"
)
_INJECTED_XML_BLOCK_RE = re.compile(
    rf"<(?P<tag>{_INJECTED_XML_TAG})\b[^>]*>.*?</(?P=tag)\s*>",
    re.IGNORECASE | re.DOTALL,
)
_UNCLOSED_INJECTED_XML_BLOCK_RE = re.compile(
    rf"<(?:{_INJECTED_XML_TAG})\b[^>]*>.*\Z",
    re.IGNORECASE | re.DOTALL,
)


def _strip_injected_context(text: str) -> str:
    """Remove product-injected context blocks while retaining human prose.

    Ambient context and the actual request can share a provider message.  A
    prefix-only classification either persisted the ambient block when it came
    later, or discarded the human request when the block came first.  Remove
    every recognized closed block, and fail closed from an unclosed marker to
    the end of the message.
    """
    cleaned = _INJECTED_XML_BLOCK_RE.sub("\n", text)
    cleaned = _UNCLOSED_INJECTED_XML_BLOCK_RE.sub("\n", cleaned)
    beginning = cleaned.lstrip()[:500].lower()
    standalone_markers = (
        "## memory",
        "# agents.md instructions for ",
    )
    if any(beginning.startswith(marker) for marker in standalone_markers):
        return ""
    return cleaned.strip()


def _copilot_human_messages(
        recs: list[dict], warnings: list[dict] | None = None) -> list[str]:
    """Copilot human turns only.

    Skill injections arrive as user messages too, so they are excluded by
    shape. Genuine opening tasks remain evidence: they establish expectations
    that later output can satisfy or violate.
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
        s = _strip_injected_context(txt)
        if not s:
            continue
        # Injected skill / context payloads, not the human speaking.
        if s.startswith(("<", "#!")) or "SKILL.md" in s[:200]:
            continue
        if "Workspace Directories" in s[:200]:
            continue
        normalised = _bounded_human(s, warnings)
        out.append(normalised)
    return out


def _codex_human_messages(
        recs: list[dict], warnings: list[dict] | None = None) -> list[str]:
    """Codex human turns from the one documented message shape only."""
    out: list[str] = []
    for record in recs:
        if record.get("type") != "response_item":
            continue
        payload = record.get("payload")
        if (not isinstance(payload, dict) or payload.get("type") != "message"
                or payload.get("role") != "user"):
            continue
        content = payload.get("content")
        if not isinstance(content, list):
            continue
        parts: list[str] = []
        for item in content:
            if (isinstance(item, dict) and item.get("type") == "input_text"
                    and isinstance(item.get("text"), str)):
                parts.append(item["text"])
        text = _strip_injected_context("\n".join(parts))
        if text:
            out.append(_bounded_human(text, warnings))
    return out


def _codex_scoped_records(
    recs: list[dict], repository: object, warnings: list[dict]
) -> list[dict]:
    """Retain records only while turn context names the selected repository."""
    wanted = _absolute_scope_key(repository)
    if wanted is None:
        warnings.append({"code": "codex_scope_unavailable", "count": 1})
        return []
    scoped: list[dict] = []
    # The captured stream must authenticate its own scope.  Discovery's first
    # record check is not enough because the file can be replaced between
    # discovery and capture.
    active = False
    omitted = 0
    for record in recs:
        payload = record.get("payload")
        if record.get("type") == "session_meta" and isinstance(payload, dict):
            active = _absolute_scope_key(payload.get("cwd")) == wanted
        elif record.get("type") == "turn_context" and isinstance(payload, dict):
            if "cwd" in payload:
                active = _absolute_scope_key(payload.get("cwd")) == wanted
        if active:
            scoped.append(record)
        else:
            omitted += 1
    if omitted:
        warnings.append({"code": "mixed_repository_records_omitted",
                         "count": omitted})
    return scoped


def _codex_capture_matches_session(recs: list[dict], sess: dict) -> bool:
    """Re-authenticate one captured rollout against its discovered identity.

    Discovery reads only the first metadata record.  The rollout can be
    replaced before the bounded capture, so the captured bytes must repeat the
    same absolute repository scope *and* session id before any later record can
    become evidence.  Merely filtering every record from a replacement into an
    empty digest would hide an incomplete capture as a successful quiet session.
    """
    if not recs:
        return False
    expected_scope = _absolute_scope_key(sess.get("repository"))
    expected_id = sess.get("id")
    if expected_scope is None or not isinstance(expected_id, str):
        return False
    for number, record in enumerate(recs):
        if record.get("type") != "session_meta":
            if number == 0:
                return False
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return False
        captured_scope = _absolute_scope_key(payload.get("cwd"))
        captured_id = payload.get("id") or payload.get("session_id")
        if (captured_scope != expected_scope
                or not isinstance(captured_id, str)
                or captured_id.strip() != expected_id.strip()):
            return False
    return True


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
        if len(document) > MAX_EVENT_RECORDS:
            warnings.append({
                "code": "event_record_limit_exceeded",
                "max_records": MAX_EVENT_RECORDS,
                "observed_records": len(document),
            })
            raise SessionIngestionError(
                "event_record_limit_exceeded",
                f"event source has {len(document)} records; limit is "
                f"{MAX_EVENT_RECORDS}; no partial digest was produced",
            )
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
    observed_records = sum(1 for line in lines if line.strip())
    if observed_records > MAX_EVENT_RECORDS:
        warnings.append({
            "code": "event_record_limit_exceeded",
            "max_records": MAX_EVENT_RECORDS,
            "observed_records": observed_records,
        })
        raise SessionIngestionError(
            "event_record_limit_exceeded",
            f"event source has {observed_records} records; limit is "
            f"{MAX_EVENT_RECORDS}; no partial digest was produced",
        )
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


def _count_public_command(blob: str, commands: Counter) -> None:
    """Retain only normalized public kit commands, never arbitrary argv."""
    match = KIT_CMD_RE.search(blob)
    if match:
        command = " ".join(match.group(1).lower().split())
        if command in PUBLIC_KIT_COMMANDS:
            commands[f"kit {command}"] += 1


def _count_patch_files(blob: str, writes: Counter) -> None:
    for value in PATCH_FILE_RE.findall(blob):
        name = ntpath.basename(value.strip().replace("/", "\\"))
        if name:
            writes[name[:256]] += 1


def _count_gate_output(output: object, failures: Counter) -> tuple[int, Counter]:
    passes = 0
    if not isinstance(output, str):
        return passes, failures
    if "FAIL" in output:
        for stage in GATE_FAIL_RE.findall(output):
            if stage.lower() in GATE_STAGES:
                failures[stage.lower()] += 1
    if GATE_PASS_RE.search(output):
        passes += 1
    return passes, failures


def _codex_usage_candidate(record: dict) -> dict[str, int] | None:
    if record.get("type") != "event_msg":
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    total = info.get("total_token_usage") if isinstance(info, dict) else None
    if not isinstance(total, dict):
        return None
    candidate: dict[str, int] = {}
    for name in CODEX_TOKEN_METRICS:
        value = total.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            candidate[name] = value
    return candidate or None


def _failed_digest(sess: dict, index: int, provider: str, sources: dict,
                   warnings: list[dict], error: str) -> dict:
    """Return one bounded failure record without pretending evidence is complete."""
    return {
        "index": index,
        "id": sess.get("id", ""),
        "error": error,
        "provider": provider,
        "parent_session_id": sess.get("parent_session_id", ""),
        "sources": sources,
        "warnings": warnings,
    }


def _is_reparse(info: os.stat_result) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return bool(int(getattr(info, "st_file_attributes", 0)) & marker)


def _copilot_binding(workspace: Path, event_log: Path) -> tuple[str, str, int, int]:
    """Authenticate the direct provider directory that binds metadata to events."""
    if workspace.parent != event_log.parent:
        raise OSError("workspace metadata and event source are not direct siblings")
    directory = workspace.parent
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
        raise OSError("Copilot session directory must be a regular non-link directory")
    workspace_resolved = workspace.resolve(strict=True)
    event_resolved = event_log.resolve(strict=True)
    if workspace_resolved.parent != directory.resolve(strict=True):
        raise OSError("workspace metadata resolves outside its session directory")
    if event_resolved.parent != directory.resolve(strict=True):
        raise OSError("event source resolves outside its session directory")
    return (str(workspace_resolved), str(event_resolved), info.st_dev, info.st_ino)


def _limit_failure(source: str, exc: session_evidence.CaptureLimitError) -> dict:
    return {
        "code": f"{source}_byte_limit_exceeded",
        "source": source,
        "max_bytes": exc.maximum,
        "observed_bytes": exc.observed,
    }


def digest_one(sess: dict, index: int) -> dict:
    """Reduce one session log to counts plus verbatim human messages."""
    warnings: list[dict] = []
    sources: dict[str, dict] = {}
    provider = str(sess.get("provider") or sess.get("kind") or "copilot")
    locator = str(sess.get("locator") or f"{provider}/{sess.get('id', '')}")
    workspace = sess.get("workspace")
    event_log = sess.get("log")
    if not isinstance(event_log, Path):
        warnings.append({"code": "source_unreadable", "source": "events"})
        return _failed_digest(
            sess, index, provider, sources, warnings, "events_unreadable"
        )
    expected_scope = _absolute_scope_key(sess.get("repository"))
    workspace_content: bytes | None = None
    copilot_binding: tuple[str, str, int, int] | None = None
    if provider == "copilot":
        if not isinstance(workspace, Path) or not isinstance(event_log, Path):
            warnings.append({"code": "copilot_scope_unavailable", "count": 1})
            return _failed_digest(
                sess, index, provider, sources, warnings, "copilot_scope_unavailable"
            )
        try:
            copilot_binding = _copilot_binding(workspace, event_log)
            workspace_content, provenance, source_warnings = (
                session_evidence.capture_file(
                    workspace,
                    locator=f"{locator}/workspace",
                    maximum_bytes=MAX_WORKSPACE_BYTES,
                )
            )
        except session_evidence.CaptureLimitError as exc:
            warnings.append(_limit_failure("workspace", exc))
            return _failed_digest(
                sess, index, provider, sources, warnings,
                "workspace_byte_limit_exceeded",
            )
        except OSError:
            warnings.append({"code": "source_unreadable", "source": "workspace"})
            return _failed_digest(
                sess, index, provider, sources, warnings, "workspace_unreadable"
            )
        sources["workspace"] = provenance
        warnings.extend({**warning, "source": "workspace"}
                        for warning in source_warnings)
        captured_scope = _absolute_scope_key(
            _yaml_get(workspace_content.decode("utf-8", errors="replace"), "cwd")
        )
        if expected_scope is None or captured_scope != expected_scope:
            warnings.append({"code": "copilot_scope_changed", "count": 1})
            return _failed_digest(
                sess, index, provider, sources, warnings, "copilot_scope_changed"
            )
        if source_warnings:
            return _failed_digest(
                sess, index, provider, sources, warnings,
                "workspace_changed_during_capture",
            )
    try:
        content, provenance, source_warnings = session_evidence.capture_file(
            event_log, locator=f"{locator}/events", maximum_bytes=MAX_EVENT_BYTES)
        sources["events"] = provenance
        warnings.extend({**warning, "source": "events"} for warning in source_warnings)
    except session_evidence.CaptureLimitError as exc:
        warnings.append(_limit_failure("events", exc))
        return _failed_digest(
            sess, index, provider, sources, warnings, "events_byte_limit_exceeded"
        )
    except OSError:
        warnings.append({"code": "source_unreadable", "source": "events"})
        return _failed_digest(
            sess, index, provider, sources, warnings, "events_unreadable"
        )
    if source_warnings:
        return _failed_digest(
            sess, index, provider, sources, warnings, "events_changed_during_capture"
        )

    if provider == "copilot":
        assert isinstance(workspace, Path)
        try:
            workspace_after, provenance_after, workspace_warnings = (
                session_evidence.capture_file(
                    workspace,
                    locator=f"{locator}/workspace",
                    maximum_bytes=MAX_WORKSPACE_BYTES,
                )
            )
            binding_after = _copilot_binding(workspace, event_log)
        except session_evidence.CaptureLimitError as exc:
            warnings.append(_limit_failure("workspace", exc))
            return _failed_digest(
                sess, index, provider, sources, warnings,
                "workspace_byte_limit_exceeded",
            )
        except OSError:
            warnings.append({"code": "source_unreadable", "source": "workspace"})
            return _failed_digest(
                sess, index, provider, sources, warnings, "workspace_unreadable"
            )
        captured_after = _absolute_scope_key(
            _yaml_get(workspace_after.decode("utf-8", errors="replace"), "cwd")
        )
        if (workspace_warnings or workspace_after != workspace_content
                or provenance_after != sources.get("workspace")
                or binding_after != copilot_binding
                or captured_after != expected_scope):
            warnings.append({"code": "copilot_scope_changed", "count": 1})
            return _failed_digest(
                sess, index, provider, sources, warnings, "copilot_scope_changed"
            )

    try:
        recs = _parse_records(content, warnings)
    except SessionIngestionError as exc:
        return _failed_digest(
            sess, index, provider, sources, warnings, exc.code
        )
    if provider == "codex":
        if not _codex_capture_matches_session(recs, sess):
            warnings.append({"code": "codex_scope_changed", "count": 1})
            return _failed_digest(
                sess, index, provider, sources, warnings, "codex_scope_changed"
            )
        recs = _codex_scoped_records(
            recs, sess.get("repository"), warnings
        )

    turns = 0
    aiu = 0.0
    codex_usage: dict[str, int] = {}
    codex_usage_score = -1
    model = ""
    persona = "unattributed"
    cmds: Counter = Counter()
    writes: Counter = Counter()
    gate_fails: Counter = Counter()
    gate_passes = 0
    updated = str(sess.get("updated") or "")

    for r in recs:
        t = r.get("type", "")
        timestamp = r.get("timestamp")
        if isinstance(timestamp, str) and timestamp:
            updated = timestamp
        if provider == "codex":
            payload = r.get("payload")
            if not isinstance(payload, dict):
                continue
            if t == "response_item" and payload.get("type") == "message" \
                    and payload.get("role") == "assistant":
                turns += 1
            if t == "turn_context" and isinstance(payload.get("model"), str):
                model = payload["model"]
            usage = _codex_usage_candidate(r)
            if usage is not None:
                score = usage.get("total_tokens", sum(usage.values()))
                if score >= codex_usage_score:
                    codex_usage = usage
                    codex_usage_score = score
            if t == "response_item" and payload.get("type") in (
                    "function_call", "custom_tool_call"):
                raw = payload.get("arguments")
                if not isinstance(raw, str):
                    raw = payload.get("input")
                if isinstance(raw, str):
                    _count_public_command(raw, cmds)
                    _count_patch_files(raw, writes)
            if t == "response_item" and payload.get("type") in (
                    "function_call_output", "custom_tool_call_output"):
                found_passes, gate_fails = _count_gate_output(
                    payload.get("output"), gate_fails)
                gate_passes += found_passes
            continue

        d = r.get("data", r) if isinstance(r.get("data"), dict) else r
        if t in ("assistant.message", "assistant"):
            turns += 1
            model = d.get("model") or model
            usage = d.get("usage") or {}
            if isinstance(usage, dict):
                try:
                    aiu += float(usage.get("totalAiu") or usage.get("aiu") or 0)
                except (TypeError, ValueError):
                    pass
        if t == "subagent.selected":
            selected = d.get("agentName") or d.get("agent_name")
            if isinstance(selected, str) and selected.strip():
                persona = selected.strip()
        args = d.get("arguments")
        if not isinstance(args, dict):
            args = {}
        for key in ("command", "cmd", "shellCommand"):
            value = args.get(key)
            if isinstance(value, str):
                _count_public_command(value, cmds)
                break
        for key in ("path", "filePath", "file_path"):
            value = args.get(key)
            if isinstance(value, str) and t.startswith(("tool", "function")):
                name = (d.get("name") or d.get("tool") or "").lower()
                if any(word in name for word in (
                        "write", "edit", "create", "patch", "replace")):
                    writes[_norm(value).split("/")[-1] or value] += 1
        output = d.get("output") or d.get("result") or d.get("content")
        found_passes, gate_fails = _count_gate_output(output, gate_fails)
        gate_passes += found_passes

    usage_metrics = codex_usage if provider == "codex" else {"aiu": round(aiu, 2)}
    human = (_codex_human_messages(recs, warnings) if provider == "codex"
             else _copilot_human_messages(recs, warnings))
    parent_session_id = str(sess.get("parent_session_id") or "")
    if provider == "codex" and parent_session_id and human:
        warnings.append({"code": "child_session_human_messages_omitted",
                         "count": len(human)})
        human = []

    return {
        "index": index,
        "id": sess["id"],
        "name": sess.get("name", ""),
        "provider": provider,
        "parent_session_id": parent_session_id,
        "started": sess.get("started", ""),
        "updated": updated,
        "turns": turns,
        "usage": {
            "source": "provider_reported",
            "metrics": [
                {"name": name, "value": usage_metrics[name]}
                for name in sorted(usage_metrics)
            ],
            "monetary_cost": None,
        },
        "model": model,
        "persona": persona,
        "gate_fails": dict(gate_fails.most_common(6)),
        "gate_passes": gate_passes,
        # Two public-workflow invocations are already a repeated user-facing
        # step (including Windows/Unix launcher spellings normalized above).
        "loops": {command: count for command, count in cmds.most_common(6)
                  if count >= 2},
        "rewrites": {f: n for f, n in writes.most_common(8) if n >= 3},
        "human": human,
        "raw_bytes": len(content),
        "sources": sources,
        "warnings": warnings,
    }


def render(digests: list[dict]) -> str:
    lines: list[str] = []
    for d in digests:
        if d.get("error"):
            lines.append(f"S{d['index']} FAILED {d['error']}")
            for warning in d.get("warnings", []):
                details = []
                for field in (
                    "source", "max_bytes", "observed_bytes", "max_records",
                    "observed_records", "count",
                ):
                    if field in warning:
                        details.append(f"{field}={warning[field]}")
                suffix = " " + " ".join(details) if details else ""
                lines.append(f"  WARN {warning['code']}{suffix}")
            continue
        head = (f"S{d['index']} {d.get('provider', 'copilot')} "
                f"{d['started'][:16]} {d['turns']}t")
        metrics = {
            item.get("name"): item.get("value")
            for item in ((d.get("usage") or {}).get("metrics") or [])
            if isinstance(item, dict)
        }
        if metrics.get("aiu"):
            head += f" {metrics['aiu']}AIU"
        if metrics.get("total_tokens"):
            head += f" {metrics['total_tokens']}tokens"
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
    try:
        sessions = discover(repo, roots, since=a.since, limit=a.limit)
    except SessionIngestionError as exc:
        print(f"session evidence: {exc}", file=sys.stderr)
        return 2

    if not sessions:
        print(f"no sessions found for {repo}", file=sys.stderr)
        print("checked: " + ", ".join(str(r) for r in (roots or SESSION_ROOTS)), file=sys.stderr)
        return 1

    if a.list:
        for i, s in enumerate(sessions, 1):
            kb = s["log"].stat().st_size // 1024
            print(f"S{i} {s['updated'][:16]} {kb}KB {s['provider']} {s['id'][:8]} {s.get('name','')}")
        return 0

    digests = [digest_one(s, i) for i, s in enumerate(sessions, 1)]
    snapshot_path: Path | None = None
    if a.snapshot:
        manifest = session_evidence.build_manifest(
            repo, digests, since=a.since, limit=a.limit
        )
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
