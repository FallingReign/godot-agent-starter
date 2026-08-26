#!/usr/bin/env python3
"""Immutable evidence manifests for repository-scoped Copilot sessions.

Session logs can contain tool arguments, command output, file contents and
credentials.  This module deliberately does not persist those raw inputs.  It
records their hashes and paths as provenance, then stores only the reduced
evidence produced by :mod:`session_digest`.

The manifest bytes are canonical JSON.  Their SHA-256 is the filename, so the
same selected inputs and reduction always resolve to the same immutable file.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path

SCHEMA = 1
KIND = "copilot-session-evidence"


def _canonical_path(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path)).replace("\\", "/").rstrip("/")


def capture_file(path: Path) -> tuple[bytes, dict, list[dict]]:
    """Read *path* once and return bytes, provenance, and mutation warnings."""
    with path.open("rb") as source:
        before = os.fstat(source.fileno())
        content = source.read()
        after = os.fstat(source.fileno())

    provenance = {
        "path": _canonical_path(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "bytes": len(content),
    }
    warnings: list[dict] = []
    if before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
        warnings.append({
            "code": "source_changed_during_capture",
            "before_bytes": before.st_size,
            "after_bytes": after.st_size,
        })
    return content, provenance, warnings


def build_manifest(repository: Path, digests: list[dict]) -> dict:
    """Build a deterministic, privacy-minimised manifest from session digests."""
    ordered = sorted(
        digests,
        key=lambda d: (
            d.get("updated") or d.get("started") or "",
            d.get("id") or "",
            ((d.get("sources") or {}).get("events") or {}).get("sha256", ""),
        ),
    )
    sessions: list[dict] = []
    for ordinal, digest in enumerate(ordered, 1):
        session_id = str(digest.get("id") or "")
        messages = [
            {
                "citation": f"{session_id}:H{message_index}",
                "text": message,
            }
            for message_index, message in enumerate(digest.get("human") or [], 1)
        ]
        entry = {
            "ordinal": ordinal,
            "session_id": session_id,
            "name": digest.get("name") or "",
            "started": digest.get("started") or "",
            "updated": digest.get("updated") or "",
            "sources": digest.get("sources") or {},
            "warnings": digest.get("warnings") or [],
            "evidence": {
                "turns": digest.get("turns", 0),
                "cost": digest.get("cost", 0.0),
                "model": digest.get("model") or "",
                "persona": digest.get("persona") or "unattributed",
                "gate_fails": digest.get("gate_fails") or {},
                "gate_passes": digest.get("gate_passes", 0),
                "loops": digest.get("loops") or {},
                "rewrites": digest.get("rewrites") or {},
                "human_messages": messages,
            },
        }
        if digest.get("error"):
            entry["error"] = digest["error"]
        sessions.append(entry)

    return {
        "schema": SCHEMA,
        "kind": KIND,
        "repository": _canonical_path(repository),
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
