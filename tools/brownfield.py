#!/usr/bin/env python3
"""File-scoped allowances for adopting the kit in an existing project.

An allowance is deliberately narrower than a verification stage.  It binds one
normalized issue to the exact bytes of the file that already had that issue.
Changes elsewhere in the repository do not affect it; changing that file does.
"""
from __future__ import annotations

import hashlib
import json
import re
import stat
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping

SCHEMA = 1
KIND = "agent-kit-brownfield-baseline"
EVALUATION_KIND = "agent-kit-brownfield-evaluation"
SCAN_KIND = "agent-kit-brownfield-scan"
BASELINE_RELATIVE = ".agent-kit/brownfield.json"
CURRENT_RELATIVE = ".agent-kit/current.json"

MAX_ISSUES = 4096
MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PATH_LENGTH = 1024
MAX_STAGE_LENGTH = 80
MAX_CODE_LENGTH = 160
MAX_LINE = 2_147_483_647

_ISSUE_FIELDS = frozenset({"stage", "code", "path", "line", "message_sha256"})
_STORED_ISSUE_FIELDS = _ISSUE_FIELDS | {"file_sha256"}
_BASELINE_FIELDS = frozenset({"schema", "kind", "binding", "issues"})
_BINDING_FIELDS = frozenset({"installation_id", "release_sha256"})
_SCAN_FIELDS = frozenset({"schema", "kind", "complete", "issues", "errors"})
_HEX_32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*\Z")


class BrownfieldError(ValueError):
    """Raised when an allowance would be ambiguous or unsafe."""


def canonical_json(document: Mapping[str, object]) -> bytes:
    """Return the one portable byte representation used for a baseline."""
    content = (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    if len(content) > MAX_DOCUMENT_BYTES:
        raise BrownfieldError(
            f"brownfield baseline exceeds {MAX_DOCUMENT_BYTES} bytes"
        )
    return content


def normalize_issues(
    issues: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Validate and canonically order public, file-scoped issue records."""
    return _issue_list(issues, stored=False)


def build_scan_document(
    issues: Iterable[Mapping[str, object]],
    *,
    errors: Iterable[str] = (),
) -> dict[str, object]:
    """Build deterministic output for the installer's read-only adoption scan.

    A scan with errors is deliberately incomplete.  A lifecycle controller may
    show that result, but must never turn it into a baseline.
    """
    normalized = normalize_issues(issues)
    normalized_errors: list[str] = []
    for error in errors:
        if not isinstance(error, str) or not error.strip():
            raise BrownfieldError("brownfield scan errors must be non-empty strings")
        rendered = " ".join(error.split())
        if len(rendered) > 1024:
            raise BrownfieldError("brownfield scan error exceeds 1024 characters")
        normalized_errors.append(rendered)
    normalized_errors = sorted(set(normalized_errors))
    document: dict[str, object] = {
        "schema": SCHEMA,
        "kind": SCAN_KIND,
        "complete": not normalized_errors,
        "issues": normalized,
        "errors": normalized_errors,
    }
    _exact_fields(document, _SCAN_FIELDS, "brownfield scan")
    canonical_json(document)
    return document


def read_baseline_file(project_root: str | Path) -> bytes | None:
    """Read the exact managed baseline without following redirected paths."""
    root = _project_root(project_root)
    return _read_safe_file(
        root,
        BASELINE_RELATIVE,
        maximum=MAX_DOCUMENT_BYTES,
        allow_missing=True,
    )


def read_install_binding(project_root: str | Path) -> dict[str, str]:
    """Read the binding already authenticated by the managed launcher.

    This second bounded read prevents a baseline from being evaluated against a
    current pointer that was swapped after process startup.
    """
    root = _project_root(project_root)
    content = _read_safe_file(
        root,
        CURRENT_RELATIVE,
        maximum=MAX_DOCUMENT_BYTES,
        allow_missing=False,
    )
    assert content is not None
    try:
        parsed = json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, BrownfieldError) as exc:
        raise BrownfieldError(f"managed current pointer is invalid: {exc}") from exc
    if not isinstance(parsed, Mapping):
        raise BrownfieldError("managed current pointer must be a JSON object")
    if canonical_json(dict(parsed)) != content:
        raise BrownfieldError("managed current pointer JSON is not canonical")
    active = parsed.get("active_release")
    if not isinstance(active, Mapping):
        raise BrownfieldError("managed current pointer has no active release")
    return _binding(
        str(parsed.get("installation_id") or ""),
        str(active.get("archive_sha256") or ""),
    )


def build_baseline(
    project_root: str | Path,
    issues: Iterable[Mapping[str, object]],
    *,
    installation_id: str,
    release_sha256: str,
) -> dict[str, object]:
    """Capture safe existing issues against their exact affected file bytes.

    Every input issue must have exactly the normalized public issue fields.
    Global, missing, redirected, oversized, or otherwise unsafe file paths are
    rejected rather than converted into a broad allowance.
    """
    root = _project_root(project_root)
    binding = _binding(installation_id, release_sha256)
    normalized = _issue_list(issues, stored=False)
    file_hashes: dict[str, str] = {}
    stored: list[dict[str, object]] = []
    for issue in normalized:
        relative = str(issue["path"])
        if relative not in file_hashes:
            file_hashes[relative] = _hash_safe_file(root, relative)
        stored.append({**issue, "file_sha256": file_hashes[relative]})
    document: dict[str, object] = {
        "schema": SCHEMA,
        "kind": KIND,
        "binding": binding,
        "issues": stored,
    }
    canonical_json(document)
    return document


def validate_baseline(
    baseline: bytes | bytearray | Mapping[str, object],
    *,
    installation_id: str,
    release_sha256: str,
) -> dict[str, object]:
    """Validate persisted content and its exact installation/release binding."""
    document = _baseline_document(baseline)
    _exact_fields(document, _BASELINE_FIELDS, "brownfield baseline")
    if document.get("schema") != SCHEMA or document.get("kind") != KIND:
        raise BrownfieldError("unsupported brownfield baseline schema or kind")
    binding = document.get("binding")
    if not isinstance(binding, Mapping):
        raise BrownfieldError("brownfield baseline binding must be an object")
    _exact_fields(binding, _BINDING_FIELDS, "brownfield baseline binding")
    expected_binding = _binding(installation_id, release_sha256)
    if dict(binding) != expected_binding:
        raise BrownfieldError(
            "brownfield baseline belongs to a different installation or release"
        )
    issues = document.get("issues")
    if not isinstance(issues, list):
        raise BrownfieldError("brownfield baseline issues must be a list")
    normalized = _issue_list(issues, stored=True)
    if issues != normalized:
        raise BrownfieldError("brownfield baseline issues are not canonically ordered")
    validated: dict[str, object] = {
        "schema": SCHEMA,
        "kind": KIND,
        "binding": expected_binding,
        "issues": normalized,
    }
    canonical_json(validated)
    return validated


def evaluate_baseline(
    project_root: str | Path,
    baseline: bytes | bytearray | Mapping[str, object],
    current_issues: Iterable[Mapping[str, object]],
    *,
    installation_id: str,
    release_sha256: str,
) -> dict[str, object]:
    """Compare current issues with safe, file-bound existing allowances.

    The result exposes only issues that still exist.  A former issue that is no
    longer reported contributes to ``resolved`` but is not carried forward as
    visible gap state.
    """
    root = _project_root(project_root)
    validated = validate_baseline(
        baseline,
        installation_id=installation_id,
        release_sha256=release_sha256,
    )
    current = _issue_list(current_issues, stored=False)
    old_by_identity = {
        _identity(issue): issue for issue in validated["issues"]  # type: ignore[index]
    }
    current_identities = {_identity(issue) for issue in current}
    file_hashes: dict[str, str] = {}
    existing: list[dict[str, object]] = []
    failing: list[dict[str, object]] = []
    for issue in current:
        relative = str(issue["path"])
        if relative not in file_hashes:
            file_hashes[relative] = _hash_safe_file(root, relative)
        current_file_sha256 = file_hashes[relative]
        prior = old_by_identity.get(_identity(issue))
        if prior is None:
            failing.append({**issue, "reason": "new-or-changed-gap"})
        elif prior["file_sha256"] != current_file_sha256:
            failing.append({
                **issue,
                "reason": "affected-file-changed",
                "baseline_file_sha256": prior["file_sha256"],
                "current_file_sha256": current_file_sha256,
            })
        else:
            existing.append({**issue, "file_sha256": current_file_sha256})
    resolved = sum(
        1 for identity in old_by_identity if identity not in current_identities
    )
    return {
        "schema": SCHEMA,
        "kind": EVALUATION_KIND,
        "status": "fail" if failing else "pass",
        "counts": {
            "existing": len(existing),
            "failing": len(failing),
            "resolved": resolved,
        },
        "existing_gaps": existing,
        "failing_gaps": failing,
    }


def _baseline_document(
    value: bytes | bytearray | Mapping[str, object],
) -> dict[str, object]:
    if isinstance(value, (bytes, bytearray)):
        content = bytes(value)
        if len(content) > MAX_DOCUMENT_BYTES:
            raise BrownfieldError(
                f"brownfield baseline exceeds {MAX_DOCUMENT_BYTES} bytes"
            )
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BrownfieldError("brownfield baseline is not UTF-8 JSON") from exc
        try:
            parsed = json.loads(text, object_pairs_hook=_unique_object)
        except (json.JSONDecodeError, BrownfieldError) as exc:
            raise BrownfieldError(f"invalid brownfield baseline JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise BrownfieldError("brownfield baseline must be a JSON object")
        if canonical_json(parsed) != content:
            raise BrownfieldError("brownfield baseline JSON is not canonical")
        return parsed
    if not isinstance(value, Mapping):
        raise BrownfieldError("brownfield baseline must be bytes or an object")
    return dict(value)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise BrownfieldError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _binding(installation_id: str, release_sha256: str) -> dict[str, str]:
    if not isinstance(installation_id, str) or not _HEX_32.fullmatch(
        installation_id
    ):
        raise BrownfieldError("installation_id must be 32 lowercase hexadecimal digits")
    if not isinstance(release_sha256, str) or not _HEX_64.fullmatch(release_sha256):
        raise BrownfieldError("release_sha256 must be 64 lowercase hexadecimal digits")
    return {
        "installation_id": installation_id,
        "release_sha256": release_sha256,
    }


def _issue_list(
    issues: Iterable[Mapping[str, object]], *, stored: bool
) -> list[dict[str, object]]:
    fields = _STORED_ISSUE_FIELDS if stored else _ISSUE_FIELDS
    result: list[dict[str, object]] = []
    identities: set[tuple[object, ...]] = set()
    stored_hashes: dict[str, object] = {}
    try:
        iterator = iter(issues)
    except TypeError as exc:
        raise BrownfieldError("issues must be an iterable of objects") from exc
    for issue in iterator:
        if len(result) >= MAX_ISSUES:
            raise BrownfieldError(f"brownfield baseline exceeds {MAX_ISSUES} issues")
        if not isinstance(issue, Mapping):
            raise BrownfieldError("each issue must be an object")
        _exact_fields(issue, fields, "brownfield issue")
        stage = _token(issue.get("stage"), "stage", MAX_STAGE_LENGTH)
        code = _token(issue.get("code"), "code", MAX_CODE_LENGTH)
        path = _relative_path(issue.get("path"))
        line = issue.get("line")
        if isinstance(line, bool) or not isinstance(line, int) or not 0 <= line <= MAX_LINE:
            raise BrownfieldError(f"issue line must be an integer from 0 to {MAX_LINE}")
        message_sha256 = _sha256(issue.get("message_sha256"), "message_sha256")
        normalized: dict[str, object] = {
            "stage": stage,
            "code": code,
            "path": path,
            "line": line,
            "message_sha256": message_sha256,
        }
        if stored:
            normalized["file_sha256"] = _sha256(
                issue.get("file_sha256"), "file_sha256"
            )
            previous_hash = stored_hashes.setdefault(
                path, normalized["file_sha256"]
            )
            if previous_hash != normalized["file_sha256"]:
                raise BrownfieldError(
                    f"baseline has conflicting file hashes for {path!r}"
                )
        identity = _identity(normalized)
        if identity in identities:
            raise BrownfieldError(
                "duplicate issue identity: "
                f"{stage}:{code}:{path}:{line}:{message_sha256}"
            )
        identities.add(identity)
        result.append(normalized)
    result.sort(key=_identity)
    return result


def _identity(issue: Mapping[str, object]) -> tuple[object, ...]:
    return (
        issue["stage"],
        issue["code"],
        issue["path"],
        issue["line"],
        issue["message_sha256"],
    )


def _exact_fields(
    value: Mapping[str, object], expected: frozenset[str], label: str
) -> None:
    if any(not isinstance(key, str) for key in value):
        raise BrownfieldError(f"{label} field names must be strings")
    actual = frozenset(value.keys())
    if actual != expected:
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        detail: list[str] = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if unknown:
            detail.append(f"unknown {', '.join(unknown)}")
        raise BrownfieldError(f"{label} fields are invalid: {'; '.join(detail)}")


def _token(value: object, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or not _TOKEN.fullmatch(value)
    ):
        raise BrownfieldError(
            f"issue {label} must be 1-{maximum} portable token characters"
        )
    return value


def _sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HEX_64.fullmatch(value):
        raise BrownfieldError(f"{label} must be 64 lowercase hexadecimal digits")
    return value


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_LENGTH:
        raise BrownfieldError(
            f"issue path must be 1-{MAX_PATH_LENGTH} characters"
        )
    if (
        "\x00" in value
        or "\\" in value
        or unicodedata.normalize("NFC", value) != value
        or any(ord(character) < 32 for character in value)
    ):
        raise BrownfieldError("issue path must be a canonical repository-relative path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or value == "."
        or not path.parts
        or any(":" in part for part in path.parts)
        or any(part in ("", ".", "..") for part in path.parts)
        or path.as_posix() != value
    ):
        raise BrownfieldError("issue path must be a canonical repository-relative path")
    return value


def _project_root(value: str | Path) -> Path:
    raw = Path(value)
    try:
        info = raw.lstat()
        resolved = raw.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise BrownfieldError(f"project root is not readable: {raw}") from exc
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info) or not stat.S_ISDIR(info.st_mode):
        raise BrownfieldError("project root must be an unredirected directory")
    return resolved


def _is_reparse(info: object) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return bool(int(getattr(info, "st_file_attributes", 0)) & marker)


def _hash_safe_file(root: Path, relative: str) -> str:
    content = _read_safe_file(
        root,
        relative,
        maximum=MAX_FILE_BYTES,
        allow_missing=False,
    )
    assert content is not None
    return hashlib.sha256(content).hexdigest()


def _read_safe_file(
    root: Path,
    relative: str,
    *,
    maximum: int,
    allow_missing: bool,
) -> bytes | None:
    relative = _relative_path(relative)
    cursor = root
    for index, component in enumerate(PurePosixPath(relative).parts):
        try:
            matches = sorted(
                child.name
                for child in cursor.iterdir()
                if child.name.casefold() == component.casefold()
            )
        except OSError as exc:
            raise BrownfieldError(f"cannot inspect issue path {relative}: {exc}") from exc
        if len(matches) != 1 or matches[0] != component:
            if len(matches) > 1:
                detail = "case collision"
            elif matches:
                detail = f"unsafe casing {matches[0]!r}"
            else:
                detail = "missing path"
            if allow_missing and not matches:
                return None
            raise BrownfieldError(f"issue path {relative!r} has {detail}")
        cursor = cursor / component
        try:
            info = cursor.lstat()
        except OSError as exc:
            raise BrownfieldError(f"cannot inspect issue path {relative}: {exc}") from exc
        if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
            raise BrownfieldError(f"issue path {relative!r} uses a redirected path")
        final = index == len(PurePosixPath(relative).parts) - 1
        if not final and not stat.S_ISDIR(info.st_mode):
            raise BrownfieldError(f"issue path {relative!r} has a non-directory parent")
        if final and not stat.S_ISREG(info.st_mode):
            raise BrownfieldError(
                f"issue path {relative!r} is not a regular project file"
            )
    before = cursor.lstat()
    if (
        stat.S_ISLNK(before.st_mode)
        or _is_reparse(before)
        or not stat.S_ISREG(before.st_mode)
    ):
        raise BrownfieldError(f"issue path {relative!r} is no longer a regular file")
    if int(getattr(before, "st_nlink", 1)) != 1:
        raise BrownfieldError(f"issue path {relative!r} may not be a hard link")
    if before.st_size > maximum:
        raise BrownfieldError(
            f"issue file {relative!r} exceeds {maximum} bytes"
        )
    try:
        content = cursor.read_bytes()
        after = cursor.lstat()
    except OSError as exc:
        raise BrownfieldError(f"cannot read issue file {relative!r}: {exc}") from exc
    if (
        after.st_size > maximum
        or stat.S_ISLNK(after.st_mode)
        or _is_reparse(after)
        or not stat.S_ISREG(after.st_mode)
        or int(getattr(after, "st_nlink", 1)) != 1
    ):
        raise BrownfieldError(f"issue file {relative!r} changed while it was read")
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_identity != after_identity or len(content) != after.st_size:
        raise BrownfieldError(f"issue file {relative!r} changed while it was read")
    return content
