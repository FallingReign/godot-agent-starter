#!/usr/bin/env python3
"""Immutable evidence manifests for repository-scoped agent sessions.

Session logs can contain tool arguments, command output, file contents and
credentials.  This module deliberately does not persist those raw inputs.  It
records their hashes and provider-relative locators as provenance, then stores
only the reduced evidence produced by :mod:`session_digest`.

The manifest bytes are canonical JSON.  Their SHA-256 is the filename, so the
same selected inputs and reduction always resolve to the same immutable file.
"""
from __future__ import annotations

import hashlib
import json
import ntpath
import os
import posixpath
import re
import stat
import tempfile
from pathlib import Path

SCHEMA = 2
KIND = "agent-session-evidence"
LEGACY_SCHEMA = 1
LEGACY_KIND = "copilot-session-evidence"

_USAGE_METRICS = frozenset({
    "aiu",
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
})
_WARNING_FIELDS = frozenset({
    "code", "source", "count", "max_chars", "lines",
    "before_bytes", "after_bytes", "max_bytes", "max_records",
    "observed_bytes", "observed_records",
})
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}\Z")
_MODEL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+-]{0,127}\Z")
_TIMESTAMP_RE = re.compile(r"[0-9][0-9T:.+Z-]{0,63}\Z")
_GATE_STAGES = frozenset({
    "assets", "arch", "conformance", "design", "format", "grep", "import",
    "integrity", "lint", "prerequisites", "resources", "sanitise", "schema",
    "shape", "skills", "tests", "typecheck", "types",
})
_SECRET_NAME = (
    r"(?:[A-Za-z_][A-Za-z0-9_]*(?:token|password|passwd|secret|api_?key|"
    r"authorization)[A-Za-z0-9_]*|token|password|passwd|secret|api_?key|"
    r"authorization)"
)
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b(?:ghp_|github_pat_|sk-|xox[baprs]-)[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)\b(?P<label>authorization)\b\s*:\s*"
        r"(?:(?:bearer|basic)\s+)?"
        r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
    ),
    re.compile(
        r"(?i)\b(?P<label>bearer|basic)\s+"
        r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[A-Za-z0-9._~+/=-]+)"
    ),
    re.compile(
        r"(?i)(?<![\w-])--(?P<label>(?:access-|refresh-)?token|password|passwd|"
        r"secret|client-secret|api[_-]?key|authorization)"
        r"(?:\s*=\s*|\s+)"
        r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
    ),
    re.compile(
        r"(?i)(?<!\w)(?:(?:\$env:|export\s+)?"
        rf"(?P<label>{_SECRET_NAME})"
        r"\s*[:=]\s*)"
        r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
    ),
    re.compile(
        r"(?i)(?<!\w)setx?\s+"
        rf"(?P<label>{_SECRET_NAME})\s+"
        r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
    ),
)
_MACHINE_PATH_PREFIX = (
    r"(?:[A-Z]:[\\/]|"
    r"\\\\(?:\?\\(?:UNC\\)?|\.\\|[^\\/\s]+[\\/][^\\/\s]+(?:[\\/])?)|"
    r"//(?:\?/(?:UNC/)?|[^/\s]+/[^/\s]+(?:/)?)|"
    r"/(?:Users|home)/)"
)
_QUOTED_MACHINE_PATH_RE = re.compile(
    rf"(?i)(?P<quote>[\"']){_MACHINE_PATH_PREFIX}[^\"'\r\n]*(?P=quote)"
)
_MACHINE_PATH_RE = re.compile(
    rf"(?i)(?<![A-Za-z0-9_]){_MACHINE_PATH_PREFIX}[^\r\n<>\"'`|,;\]\[{{}}()]*"
)


def redact_text(value: object) -> tuple[str, int]:
    """Remove high-confidence credentials and machine-local absolute paths."""
    text = str(value or "")
    count = 0
    for pattern in _SECRET_PATTERNS:
        def secret_replacement(match: re.Match[str]) -> str:
            nonlocal count
            count += 1
            label = match.groupdict().get("label") or "credential"
            return f"{label}=<redacted>"
        text = pattern.sub(secret_replacement, text)

    def path_replacement(_match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return "<local-path>"

    text = _QUOTED_MACHINE_PATH_RE.sub(path_replacement, text)
    return _MACHINE_PATH_RE.sub(path_replacement, text), count


def _canonical_path(path: Path) -> str:
    raw = str(path)
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith(("\\\\", "//")):
        value = ntpath.normcase(ntpath.normpath(raw))
    elif raw.startswith("/"):
        value = posixpath.normpath(raw)
    else:
        value = os.path.normcase(os.path.abspath(raw))
    return value.replace("\\", "/").rstrip("/")


def repository_scope(path: Path) -> dict[str, str]:
    """Return a stable repository identity without exposing its absolute path."""
    canonical = _canonical_path(path)
    return {
        "scope_id": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "name": Path(canonical).name,
    }


class CaptureLimitError(ValueError):
    """A source exceeds a declared pre-parse byte boundary."""

    def __init__(self, maximum: int, observed: int) -> None:
        super().__init__(f"source is {observed} bytes; limit is {maximum}")
        self.maximum = maximum
        self.observed = observed


def _is_reparse(info: os.stat_result) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return bool(int(getattr(info, "st_file_attributes", 0)) & marker)


def capture_file(path: Path, *, locator: str = "source",
                 maximum_bytes: int | None = None) -> tuple[bytes, dict, list[dict]]:
    """Read one stable regular source and return bytes plus safe provenance.

    The name is authenticated immediately before open and after read.  A size
    limit is checked before allocating and again while reading, so a growing
    event stream cannot turn a retrospective into unbounded ingestion.
    """
    try:
        named_before = path.lstat()
    except OSError:
        raise
    if not stat.S_ISREG(named_before.st_mode) or _is_reparse(named_before):
        raise OSError("source must be a regular non-link file")
    if maximum_bytes is not None and named_before.st_size > maximum_bytes:
        raise CaptureLimitError(maximum_bytes, named_before.st_size)

    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    if hasattr(os, "O_NOFOLLOW"):
        flags |= int(os.O_NOFOLLOW)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode) or _is_reparse(before)
                or (named_before.st_dev, named_before.st_ino)
                != (before.st_dev, before.st_ino)):
            raise OSError("source identity changed while opening")
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            content = source.read(
                maximum_bytes + 1 if maximum_bytes is not None else -1
            )
            after = os.fstat(source.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if maximum_bytes is not None and len(content) > maximum_bytes:
        raise CaptureLimitError(maximum_bytes, max(after.st_size, len(content)))
    named_after = path.lstat()
    if (not stat.S_ISREG(named_after.st_mode) or _is_reparse(named_after)
            or (before.st_dev, before.st_ino) != (named_after.st_dev, named_after.st_ino)):
        raise OSError("source identity changed during capture")

    provenance = {
        "locator": str(locator).replace("\\", "/")[:512],
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
    }
    warnings: list[dict] = []
    if (before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns
            or after.st_size != named_after.st_size
            or after.st_mtime_ns != named_after.st_mtime_ns):
        warnings.append({
            "code": "source_changed_during_capture",
            "before_bytes": before.st_size,
            "after_bytes": after.st_size,
        })
    return content, provenance, warnings


def _bounded_label(value: object, fallback: str = "") -> str:
    if not isinstance(value, str):
        return fallback
    text = " ".join(value.split())
    if not text or len(text) > 256 or any(ord(char) < 32 for char in text):
        return fallback
    return text


def _identifier(value: object, fallback: str = "") -> str:
    if not isinstance(value, str) or not value.strip():
        return fallback
    text = value.strip()
    if _IDENTIFIER_RE.fullmatch(text):
        return text
    return "redacted-" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _timestamp(value: object) -> str:
    return value if isinstance(value, str) and _TIMESTAMP_RE.fullmatch(value) else ""


def _nonnegative_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _counts(value: object, kind: str) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    out: dict[str, int] = {}
    for raw_key in sorted(value, key=lambda item: str(item)):
        count = value[raw_key]
        if (not isinstance(raw_key, str) or not isinstance(count, int)
                or isinstance(count, bool) or count < 1):
            continue
        key = " ".join(raw_key.split())
        accepted = False
        if kind == "gate":
            accepted = key in _GATE_STAGES
        elif kind == "loop":
            accepted = key.startswith("kit ") and len(key) <= 128
        elif kind == "rewrite":
            accepted = (len(key) <= 256 and "/" not in key and "\\" not in key
                        and key not in (".", ".."))
        if accepted:
            out[key] = count
        if len(out) >= 32:
            break
    return out


def _locator(value: object, fallback: str) -> str:
    if not isinstance(value, str):
        return fallback
    text = value.strip().replace("\\", "/")
    parts = text.split("/")
    if (not text or len(text) > 512 or text.startswith("/")
            or (parts and ":" in parts[0]) or any(part in ("", ".", "..") for part in parts)):
        return fallback
    return text


def _usage(digest: dict) -> dict:
    raw = digest.get("usage")
    metrics: dict[str, float | int] = {}
    if isinstance(raw, dict):
        values = raw.get("metrics")
        if isinstance(values, list):
            for item in values:
                if not isinstance(item, dict) or item.get("name") not in _USAGE_METRICS:
                    continue
                value = item.get("value")
                if (isinstance(value, (int, float)) and not isinstance(value, bool)
                        and value >= 0):
                    metrics[str(item["name"])] = value
    # Read compatibility for in-memory Copilot v1 digests. The v2 manifest no
    # longer calls AIU "cost" and never turns it into currency.
    if "aiu" not in metrics:
        legacy_aiu = digest.get("cost")
        if (isinstance(legacy_aiu, (int, float)) and not isinstance(legacy_aiu, bool)
                and legacy_aiu >= 0):
            metrics["aiu"] = legacy_aiu
    return {
        "source": "provider_reported",
        "metrics": [
            {"name": name, "value": metrics[name]}
            for name in sorted(metrics)
        ],
        "monetary_cost": None,
    }


def _sources(digest: dict, provider: str, session_id: str) -> dict:
    out: dict[str, dict] = {}
    raw = digest.get("sources")
    if not isinstance(raw, dict):
        return out
    for source_name in sorted(raw, key=lambda item: str(item)):
        source = raw[source_name]
        if not isinstance(source_name, str) or not isinstance(source, dict):
            continue
        sha256 = source.get("sha256")
        byte_count = source.get("bytes")
        if (not isinstance(sha256, str) or len(sha256) != 64
                or not isinstance(byte_count, int) or isinstance(byte_count, bool)
                or byte_count < 0):
            continue
        fallback = f"{provider}/{session_id}/{source_name}"
        locator = _locator(source.get("locator"), fallback)
        out[source_name] = {
            "locator": locator,
            "sha256": sha256,
            "bytes": byte_count,
        }
    return out


def _warnings(digest: dict) -> list[dict]:
    warnings: list[dict] = []
    for warning in digest.get("warnings") or []:
        if not isinstance(warning, dict) or not isinstance(warning.get("code"), str):
            continue
        clean = {key: warning[key] for key in sorted(_WARNING_FIELDS & set(warning))}
        clean["code"] = _identifier(clean["code"], "unknown-warning")
        if "source" in clean:
            clean["source"] = _identifier(clean["source"], "unknown")
        for key in (
            "count", "max_chars", "before_bytes", "after_bytes", "max_bytes",
            "max_records", "observed_bytes", "observed_records",
        ):
            if key in clean:
                clean[key] = _nonnegative_int(clean[key])
        if "lines" in clean:
            lines = clean["lines"] if isinstance(clean["lines"], list) else []
            clean["lines"] = [_nonnegative_int(line) for line in lines[:20]
                              if _nonnegative_int(line) > 0]
        warnings.append(clean)
    return sorted(warnings, key=lambda value: json.dumps(value, sort_keys=True))


def _capture_window(since: str | None, limit: int | None,
                    captured_sessions: int) -> dict:
    """Return the bounded operator-selected window stored in snapshot bytes."""
    if since is not None:
        since = _timestamp(since)
        if not since:
            raise ValueError("session capture --since must be a bounded ISO timestamp")
    if limit is not None and (
            not isinstance(limit, int) or isinstance(limit, bool) or limit < 1):
        raise ValueError("session capture --limit must be a positive integer")
    return {
        "mode": "explicit" if since is not None or limit is not None else "complete-bounded",
        "since": since,
        "limit": limit,
        "captured_sessions": captured_sessions,
    }


def build_manifest(repository: Path, digests: list[dict], *,
                   since: str | None = None, limit: int | None = None) -> dict:
    """Build a deterministic, privacy-minimised manifest from session digests."""
    ordered = sorted(
        digests,
        key=lambda d: (
            d.get("updated") or d.get("started") or "",
            d.get("provider") or d.get("kind") or "copilot",
            d.get("id") or "",
            ((d.get("sources") or {}).get("events") or {}).get("sha256", ""),
        ),
    )
    sessions: list[dict] = []
    for ordinal, digest in enumerate(ordered, 1):
        session_id = _identifier(digest.get("id"), f"session-{ordinal}")
        raw_provider = digest.get("provider") or digest.get("kind") or "copilot"
        provider = raw_provider if raw_provider in ("copilot", "codex") else "unknown"
        human = [message for message in (digest.get("human") or [])
                 if isinstance(message, str) and message.strip()]
        redacted_human: list[str] = []
        defense_redactions = 0
        for message in human:
            safe, count = redact_text(message)
            redacted_human.append(safe)
            defense_redactions += count
        human = redacted_human
        messages = [
            {
                "citation": f"{session_id}:H{message_index}",
                "text": message,
            }
            for message_index, message in enumerate(human, 1)
        ]
        for message in messages:
            message["text"] = " ".join(message["text"].split())[:4000]
        model = digest.get("model")
        if not isinstance(model, str) or _MODEL_RE.fullmatch(model) is None:
            model = ""
        turns = digest.get("turns", 0)
        if not isinstance(turns, int) or isinstance(turns, bool) or turns < 0:
            turns = 0
        entry_warnings = _warnings(digest)
        if defense_redactions:
            entry_warnings.append({
                "code": "sensitive_text_redacted",
                "count": defense_redactions,
            })
        entry = {
            "ordinal": ordinal,
            "provider": provider,
            "session_id": session_id,
            "parent_session_id": _identifier(digest.get("parent_session_id")),
            "started": _timestamp(digest.get("started")),
            "updated": _timestamp(digest.get("updated")),
            "sources": _sources(digest, provider, session_id),
            "warnings": entry_warnings,
            "evidence": {
                "turns": turns,
                "usage": _usage(digest),
                "model": model,
                "persona": _identifier(digest.get("persona"), "unattributed"),
                "gate_fails": _counts(digest.get("gate_fails"), "gate"),
                "gate_passes": _nonnegative_int(digest.get("gate_passes", 0)),
                "loops": _counts(digest.get("loops"), "loop"),
                "rewrites": _counts(digest.get("rewrites"), "rewrite"),
                "human_messages": messages,
            },
        }
        if digest.get("error"):
            entry["error"] = digest["error"]
        sessions.append(entry)

    return {
        "schema": SCHEMA,
        "kind": KIND,
        "repository": repository_scope(repository),
        "capture_window": _capture_window(since, limit, len(sessions)),
        "sessions": sessions,
    }


def canonical_bytes(manifest: dict) -> bytes:
    """Return the one byte representation used for hashing and persistence."""
    return (json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ) + "\n").encode("utf-8")


def write_manifest(output_dir: Path, manifest: dict) -> Path:
    """Atomically persist a content-addressed manifest and return its path."""
    content = canonical_bytes(manifest)
    content_hash = hashlib.sha256(content).hexdigest()
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{content_hash}.json"

    if target.exists():
        if target.read_bytes() != content:
            raise ValueError(f"content-address collision or corrupt snapshot: {target}")
        return target

    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{content_hash}.", suffix=".tmp", dir=output_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()

    if target.read_bytes() != content:
        raise OSError(f"snapshot verification failed after atomic write: {target}")
    return target
