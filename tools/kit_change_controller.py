#!/usr/bin/env python3
"""Crash-safe review session around the managed kit lifecycle.

``kit_change`` owns project writes and rollback. ``release`` owns release
authentication. This module only retains one verified release, binds one
read-only review to it, records state, and runs an injected offline check.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import base64
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypedDict

import kit_change
import kit_change_check
import process_supervisor
import project_context
import release
import brownfield


SESSION_SCHEMA = 2
SESSION_KIND = "agent-kit-change-session"
RESULT_SCHEMA = 1
RESULT_KIND = "agent-kit-change-result"
CONTROLLER_ROOT = "kit-change-controller"
RELEASES_ROOT = f"{CONTROLLER_ROOT}/releases"
SESSIONS_ROOT = f"{CONTROLLER_ROOT}/sessions"
LOCK_PATH = f"{CONTROLLER_ROOT}/controller.lock"
MAX_SESSION_BYTES = 16 * 1024 * 1024
MAX_DETAIL_CHARS = 2048
MAX_ISSUES = 4096
MAX_ATTEMPTS = 1024
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MODES = {"install", "upgrade"}
OPEN_STATES = {"ready", "blocked"}
RECOVERY_STATES = {"recovery_required"}
FINAL_STATES = {
    "complete",
    "adoption_required",
    "restored_failure",
    "restored",
    "failed",
}
STATES = OPEN_STATES | RECOVERY_STATES | FINAL_STATES | {"applying", "checking"}
ISSUE_KEYS = {"stage", "code", "path", "line", "message_sha256"}
BASELINE_SNAPSHOT_KEYS = {"exists", "sha256", "content_base64"}
RECOVERY_DETAIL_MARKER = " Restore is still required: "


class KitChangeView(TypedDict):
    session_id: str
    mode: str
    status: str
    project: dict[str, str]
    current_version: str
    incoming_version: str
    plan_sha256: str
    counts: dict[str, int]
    design: str
    existing_gaps: dict[str, object]
    decisions: list[dict[str, object]]
    files: list[dict[str, str]]
    recovery: str
    blockers: list[str]
    detail: str
    result_sha256: str
    plan_url: str


class ControllerResult(TypedDict):
    ok: bool
    session_id: str
    session_sha256: str
    kit_change: KitChangeView


PostApplyCheck = Callable[[Path, Mapping[str, Any]], Mapping[str, Any]]


class KitChangeControllerError(RuntimeError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def _canonical(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _root(path: Path, label: str) -> Path:
    try:
        return kit_change._canonical_root(path)  # noqa: SLF001 - shared safety primitive
    except kit_change.KitChangeError as exc:
        raise KitChangeControllerError(exc.code, f"{label}: {exc.detail}") from exc


def _runtime(path: Path) -> Path:
    absolute = path.absolute()
    if not absolute.exists():
        missing: list[Path] = []
        cursor = absolute
        while not cursor.exists() and len(missing) < 32:
            missing.append(cursor)
            cursor = cursor.parent
        _root(cursor, "controller runtime parent")
        for directory in reversed(missing):
            try:
                directory.mkdir()
            except FileExistsError:
                pass
            if directory.is_symlink() or kit_change._is_reparse(directory):  # noqa: SLF001
                raise KitChangeControllerError(
                    "runtime-unsafe", "controller runtime was redirected while creating it"
                )
    return _root(absolute, "controller runtime")


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _separate_roots(runtime_root: Path, target_root: Path) -> tuple[Path, Path]:
    target = _root(target_root, "target project")
    runtime_candidate = runtime_root.absolute()
    overlap_allowed = False
    if _inside(runtime_candidate, target):
        try:
            context = project_context.load_configured_context(target)
        except project_context.ProjectContextError:
            context = None
        overlap_allowed = bool(
            context is not None
            and context.install_mode == "managed"
            and runtime_candidate == context.runtime_root.absolute()
        )
    if (_inside(runtime_candidate, target) or _inside(target, runtime_candidate)) and not overlap_allowed:
        raise KitChangeControllerError(
            "runtime-overlap",
            "controller runtime and target project must be separate directories",
        )
    runtime = _runtime(runtime_candidate)
    return runtime, target


def _validate_runtime_target_pair(runtime: Path, target: Path) -> None:
    if not (_inside(runtime, target) or _inside(target, runtime)):
        return
    try:
        context = project_context.load_configured_context(target)
    except project_context.ProjectContextError as exc:
        raise KitChangeControllerError(
            "runtime-overlap", "controller runtime overlaps the target project"
        ) from exc
    if context.install_mode != "managed" or runtime != context.runtime_root:
        raise KitChangeControllerError(
            "runtime-overlap", "controller runtime overlaps the target project"
        )


def _directory(runtime: Path, relative: str) -> Path:
    try:
        return kit_change._ensure_directory(runtime, relative, [])  # noqa: SLF001
    except kit_change.KitChangeError as exc:
        raise KitChangeControllerError(exc.code, exc.detail) from exc


def _target(runtime: Path, relative: str) -> Path:
    try:
        return kit_change._target(runtime, relative)  # noqa: SLF001
    except kit_change.KitChangeError as exc:
        raise KitChangeControllerError(exc.code, exc.detail) from exc


def _atomic(path: Path, content: bytes) -> None:
    if path.suffix == ".json" and len(content) > MAX_SESSION_BYTES:
        raise KitChangeControllerError("session-too-large", "session exceeds its size limit")
    try:
        kit_change._atomic_bytes(path, content, "0644")  # noqa: SLF001
    except OSError as exc:
        raise KitChangeControllerError("runtime-write-failed", str(exc)) from exc


def _stable(path: Path, limit: int, label: str) -> bytes:
    try:
        return kit_change._stable_bytes(path, limit=limit)  # noqa: SLF001
    except kit_change.KitChangeError as exc:
        raise KitChangeControllerError(exc.code, f"{label}: {exc.detail}") from exc


def _archive_payload(path: Path, expected_container_sha256: str) -> bytes:
    limit = int(getattr(release, "MAX_ARCHIVE_BYTES", 512 * 1024 * 1024))
    content = _stable(path, limit, "release archive")
    if (
        SHA256_RE.fullmatch(expected_container_sha256) is None
        or not hmac.compare_digest(_sha256(content), expected_container_sha256)
    ):
        raise KitChangeControllerError(
            "release-changed", "release archive changed after verification"
        )
    return content


def _archive_path(runtime: Path, digest: str) -> Path:
    if SHA256_RE.fullmatch(digest) is None:
        raise KitChangeControllerError("release-invalid", "release identity is malformed")
    return _directory(runtime, RELEASES_ROOT) / f"{digest}.zip"


def _retain_release(runtime: Path, source: Path) -> tuple[dict[str, Any], int, Path]:
    source_is_directory = source.is_dir()
    try:
        if source_is_directory:
            source_report, _source_members = release.read_verified_directory(source)
            digest = str(source_report.get("archive_sha256") or "")
            stored = _archive_path(runtime, digest)
            if not stored.exists():
                release.materialize_verified_directory_zip(source, stored)
        else:
            source_report, _source_members = release.read_verified_archive(source)
            digest = str(source_report.get("archive_sha256") or "")
            source_container = str(source_report.get("container_sha256") or "")
            content = _archive_payload(source, source_container)
            stored = _archive_path(runtime, digest)
            wrote_source = not stored.exists()
            if wrote_source:
                _atomic(stored, content)
        report, members = release.read_verified_archive(stored)
        container = str(report.get("container_sha256") or "")
        _archive_payload(stored, container)
        if (
            report.get("archive_sha256") != digest
            or report.get("version") != source_report.get("version")
            or (
                not source_is_directory
                and wrote_source
                and container != source_container
            )
        ):
            raise KitChangeControllerError(
                "release-changed", "retained release differs from its source"
            )
    except release.ReleaseError as exc:
        raise KitChangeControllerError("release-invalid", str(exc)) from exc
    return dict(report), len(members), stored


def _verify_release(runtime: Path, session: Mapping[str, Any]) -> Path:
    release_state = session.get("release")
    if not isinstance(release_state, Mapping):
        raise KitChangeControllerError("session-invalid", "release state is missing")
    digest = str(release_state.get("archive_sha256") or "")
    container = str(release_state.get("container_sha256") or "")
    stored = _archive_path(runtime, digest)
    expected = stored.relative_to(runtime).as_posix()
    if release_state.get("archive") != expected:
        raise KitChangeControllerError("session-invalid", "release path is not canonical")
    _archive_payload(stored, container)
    try:
        report, members = release.read_verified_archive(stored)
    except release.ReleaseError as exc:
        raise KitChangeControllerError("release-tampered", str(exc)) from exc
    if (
        report.get("archive_sha256") != digest
        or report.get("container_sha256") != container
        or report.get("version") != release_state.get("version")
        or len(members) != release_state.get("member_count")
    ):
        raise KitChangeControllerError("release-tampered", "retained release metadata changed")
    return stored


def _session_id(
    target: Path,
    archive_sha256: str,
    preview_sha256: str,
    mode: str,
    requested_game_root: str | None,
    selected_game_root: object,
    attempt: int,
) -> str:
    return _sha256(_canonical({
        "target": str(target),
        "archive_sha256": archive_sha256,
        "preview_sha256": preview_sha256,
        "mode": mode,
        "requested_game_root": requested_game_root,
        "selected_game_root": selected_game_root,
        "attempt": attempt,
    }))


def _session_path(runtime: Path, session_id: str) -> Path:
    if SHA256_RE.fullmatch(session_id) is None:
        raise KitChangeControllerError("session-id-invalid", "session id must be a full SHA-256")
    return _directory(runtime, SESSIONS_ROOT) / f"{session_id}.json"


def _signed(session: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(session)
    value.pop("session_sha256", None)
    value["session_sha256"] = _sha256(_canonical(value))
    return value


def _save(runtime: Path, session: Mapping[str, Any]) -> dict[str, Any]:
    value = _signed(session)
    _atomic(_session_path(runtime, str(value["session_id"])), _canonical(value))
    return value


def _parse_session(content: bytes) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise KitChangeControllerError("session-invalid", f"duplicate key: {key}")
            value[key] = item
        return value

    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=unique)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KitChangeControllerError("session-invalid", str(exc)) from exc
    if not isinstance(value, dict) or _canonical(value) != content:
        raise KitChangeControllerError("session-invalid", "session is not canonical JSON")
    expected = {
        "schema", "kind", "session_id", "target", "release", "request",
        "preview", "state", "sequence", "transaction_id", "check", "result",
        "result_sha256", "restore", "baseline", "attempt", "session_sha256",
    }
    if set(value) != expected or value["schema"] != SESSION_SCHEMA or value["kind"] != SESSION_KIND:
        raise KitChangeControllerError("session-invalid", "session fields are not exact")
    supplied = str(value["session_sha256"] or "")
    unsigned = dict(value)
    unsigned.pop("session_sha256")
    if SHA256_RE.fullmatch(supplied) is None or not hmac.compare_digest(
        supplied, _sha256(_canonical(unsigned))
    ):
        raise KitChangeControllerError("session-tampered", "session fingerprint changed")
    if (
        value["state"] not in STATES
        or not isinstance(value["sequence"], int)
        or not isinstance(value["attempt"], int)
        or isinstance(value["attempt"], bool)
        or not 0 <= value["attempt"] < MAX_ATTEMPTS
    ):
        raise KitChangeControllerError("session-invalid", "session state is malformed")
    for key in ("target", "release", "request", "preview"):
        if not isinstance(value[key], Mapping):
            raise KitChangeControllerError("session-invalid", f"session {key} is malformed")
    _validate_preview_binding(value)
    plan_sha = str(value["preview"].get("sha256") or "")
    archive_sha = str(value["release"].get("archive_sha256") or "")
    mode = value["request"].get("mode")
    if SHA256_RE.fullmatch(plan_sha) is None or SHA256_RE.fullmatch(archive_sha) is None or mode not in MODES:
        raise KitChangeControllerError("session-invalid", "session identity is malformed")
    target = _root(Path(str(value["target"].get("path") or "")), "target project")
    expected_id = _session_id(
        target, archive_sha, plan_sha, str(mode), value["request"].get("game_root"),
        value["request"].get("selected_game_root"),
        value["attempt"],
    )
    if not hmac.compare_digest(str(value["session_id"]), expected_id):
        raise KitChangeControllerError("session-tampered", "session identity changed")
    result_sha = str(value["result_sha256"] or "")
    if value["result"] is None:
        if result_sha:
            raise KitChangeControllerError("session-invalid", "result fingerprint has no result")
    elif (
        not isinstance(value["result"], Mapping)
        or SHA256_RE.fullmatch(result_sha) is None
        or not hmac.compare_digest(result_sha, _sha256(_canonical(value["result"])))
    ):
        raise KitChangeControllerError("session-tampered", "result fingerprint changed")
    _validate_baseline_state(value["baseline"])
    return value


def _validate_preview_binding(session: Mapping[str, Any]) -> None:
    target = session["target"]
    release_state = session["release"]
    request = session["request"]
    preview = session["preview"]
    if set(target) != {"path"} or not isinstance(target.get("path"), str):
        raise KitChangeControllerError("session-invalid", "target fields are not exact")
    release_keys = {
        "archive",
        "archive_sha256",
        "container_sha256",
        "version",
        "member_count",
    }
    if (
        set(release_state) != release_keys
        or not isinstance(release_state.get("archive"), str)
        or SHA256_RE.fullmatch(str(release_state.get("container_sha256") or "")) is None
        or not isinstance(release_state.get("version"), str)
        or not isinstance(release_state.get("member_count"), int)
        or isinstance(release_state.get("member_count"), bool)
        or int(release_state.get("member_count") or 0) <= 0
    ):
        raise KitChangeControllerError("session-invalid", "release fields are not exact")
    if set(request) != {"mode", "game_root", "selected_game_root"}:
        raise KitChangeControllerError("session-invalid", "request fields are not exact")
    if request.get("mode") not in MODES:
        raise KitChangeControllerError("session-invalid", "request mode is malformed")
    for field in ("game_root", "selected_game_root"):
        if request.get(field) is not None and not isinstance(request.get(field), str):
            raise KitChangeControllerError("session-invalid", f"request {field} is malformed")
    if set(preview) != {"sha256", "raw"} or not isinstance(preview.get("raw"), Mapping):
        raise KitChangeControllerError("session-invalid", "preview fields are not exact")
    raw = preview["raw"]
    if (
        set(raw) != {"schema", "kind", "material", "approval"}
        or raw.get("schema") != kit_change.PREVIEW_SCHEMA
        or raw.get("kind") != "agent-kit-change-preview"
        or not isinstance(raw.get("material"), Mapping)
        or not isinstance(raw.get("approval"), Mapping)
    ):
        raise KitChangeControllerError("session-invalid", "preview contract is malformed")
    material = raw["material"]
    approval = raw["approval"]
    if set(approval) != {"algorithm", "sha256", "approvable"}:
        raise KitChangeControllerError("session-invalid", "preview approval fields are not exact")
    material_sha = _sha256(_canonical(material))
    if (
        approval.get("algorithm") != "sha256-canonical-json-v1"
        or not isinstance(approval.get("approvable"), bool)
        or not hmac.compare_digest(str(approval.get("sha256") or ""), material_sha)
        or not hmac.compare_digest(str(preview.get("sha256") or ""), material_sha)
    ):
        raise KitChangeControllerError("session-tampered", "preview fingerprint is not exact")
    blockers = material.get("blockers")
    if not isinstance(blockers, list) or bool(approval["approvable"]) != (not blockers):
        raise KitChangeControllerError("session-tampered", "preview blockers and approval disagree")
    if material.get("operation") != request.get("mode"):
        raise KitChangeControllerError("session-tampered", "preview operation changed")
    game_state = material.get("game_root")
    if not isinstance(game_state, Mapping) or game_state.get("value") != request.get("selected_game_root"):
        raise KitChangeControllerError("session-tampered", "preview game folder binding changed")
    if request.get("game_root") is not None and request.get("game_root") != game_state.get("value"):
        raise KitChangeControllerError("session-tampered", "requested game folder binding changed")
    target_release = material.get("target_release")
    if (
        not isinstance(target_release, Mapping)
        or target_release.get("archive_sha256") != release_state.get("archive_sha256")
        or target_release.get("kit_version") != release_state.get("version")
    ):
        raise KitChangeControllerError("session-tampered", "preview release binding changed")


def _validate_baseline_snapshot(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or set(value) != BASELINE_SNAPSHOT_KEYS:
        raise KitChangeControllerError("session-invalid", "baseline snapshot fields are not exact")
    exists = value.get("exists")
    digest = value.get("sha256")
    encoded = value.get("content_base64")
    if not isinstance(exists, bool) or not isinstance(digest, str) or not isinstance(encoded, str):
        raise KitChangeControllerError("session-invalid", "baseline snapshot is malformed")
    try:
        content = base64.b64decode(encoded.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise KitChangeControllerError("session-invalid", "baseline snapshot is not canonical base64") from exc
    if len(content) > brownfield.MAX_DOCUMENT_BYTES:
        raise KitChangeControllerError("session-invalid", "baseline snapshot is too large")
    if exists:
        if SHA256_RE.fullmatch(digest) is None or _sha256(content) != digest:
            raise KitChangeControllerError("session-invalid", "baseline snapshot identity is malformed")
    elif digest or content:
        raise KitChangeControllerError("session-invalid", "absent baseline snapshot carries content")
    return {"exists": exists, "sha256": digest, "content_base64": encoded}


def _validate_baseline_state(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != {"prior", "generated"}:
        raise KitChangeControllerError("session-invalid", "baseline state fields are not exact")
    return {
        "prior": _validate_baseline_snapshot(value.get("prior")),
        "generated": _validate_baseline_snapshot(value.get("generated")),
    }


def _preview(session: Mapping[str, Any], archive: Path) -> dict[str, Any]:
    try:
        current = kit_change.preview(
            Path(str(session["target"]["path"])),
            archive,
            game_root=session["request"].get("game_root"),
        )
    except kit_change.KitChangeError as exc:
        raise KitChangeControllerError(exc.code, exc.detail) from exc
    stored = session["preview"].get("raw")
    actual_sha = str(current.get("approval", {}).get("sha256") or "")
    if (
        current.get("material", {}).get("operation") != session["request"].get("mode")
        or not hmac.compare_digest(actual_sha, str(session["preview"].get("sha256") or ""))
        or _canonical(current) != _canonical(stored)
    ):
        raise KitChangeControllerError(
            "preview-changed", "project or release changed after the exact review"
        )
    return current


def _load(
    runtime_root: Path, session_id: str, *, refresh_open: bool
) -> tuple[Path, dict[str, Any], Path]:
    runtime = _root(runtime_root, "controller runtime")
    session = _parse_session(_stable(
        _session_path(runtime, session_id), MAX_SESSION_BYTES, "controller session"
    ))
    if not hmac.compare_digest(str(session["session_id"]), session_id):
        raise KitChangeControllerError(
            "session-tampered", "session content does not match the requested file"
        )
    target = _root(Path(str(session["target"]["path"])), "target project")
    _validate_runtime_target_pair(runtime, target)
    archive = _verify_release(runtime, session)
    if refresh_open and session["state"] in OPEN_STATES:
        _preview(session, archive)
    return runtime, session, archive


def load(runtime_root: Path, session_id: str) -> dict[str, Any]:
    """Authenticate one session, retained release, target, and open preview."""
    return _load(runtime_root, session_id, refresh_open=True)[1]


def _game_roots(target: Path) -> list[str]:
    found: list[str] = []
    visited = 0
    for current, directories, files in os.walk(target, topdown=True, followlinks=False):
        base = Path(current)
        relative = base.relative_to(target)
        directories[:] = [
            name for name in sorted(directories)
            if name.casefold() not in {".agent-kit", ".git", ".godot", ".kit", "node_modules"}
            and not (base / name).is_symlink()
            and not kit_change._is_reparse(base / name)  # noqa: SLF001
            and len(relative.parts) < 8
        ]
        visited += len(directories) + len(files)
        if visited > 20_000:
            return []
        if "project.godot" in files:
            path = base / "project.godot"
            if path.is_file() and not path.is_symlink() and not kit_change._is_reparse(path):  # noqa: SLF001
                found.append("." if relative.as_posix() == "." else relative.as_posix())
    return sorted(set(found))


def _simple_state(session: Mapping[str, Any], plan_url: str = "") -> KitChangeView:
    raw = session["preview"].get("raw")
    if not isinstance(raw, Mapping) or not isinstance(raw.get("material"), Mapping):
        raise KitChangeControllerError("session-invalid", "preview material is missing")
    material = raw["material"]
    changes = [item for item in material.get("changes", []) if isinstance(item, Mapping)]
    blocker_values = [item for item in material.get("blockers", []) if isinstance(item, Mapping)]
    core = material.get("core") if isinstance(material.get("core"), Mapping) else {}
    kit_files = int(session["release"].get("member_count") or 0) if core.get("action") == "create" else 0
    shared_files = 0
    removed_files = 0
    files: list[dict[str, str]] = []
    if core.get("action") == "create":
        files.append({
            "path": str(core.get("path") or ".agent-kit/releases"),
            "action": "Add kit core",
            "reason": "Store the exact verified kit release.",
        })
    for change in changes:
        action = str(change.get("action") or "modify")
        ownership = str(change.get("ownership") or "shared")
        strategy = str(change.get("strategy") or "")
        if action == "delete":
            removed_files += 1
            label, reason = "Remove old kit file", "Retire an exact file owned by the older kit."
        elif ownership == "kit":
            kit_files += 1
            label, reason = (
                "Add kit file" if action == "create" else "Update kit file",
                "Use the verified managed kit version.",
            )
        else:
            shared_files += 1
            label = "Add kit section" if strategy == "managed-block" else "Add shared setting"
            if action != "create":
                label = label.replace("Add", "Update", 1)
            reason = (
                "Connect the kit without replacing project instructions."
                if strategy == "managed-block"
                else "Add missing kit defaults without replacing project settings."
            )
        files.append({"path": str(change.get("path") or "Unnamed file"), "action": label, "reason": reason})
    decisions: list[dict[str, object]] = []
    if any(item.get("code") == "game-root-ambiguous" for item in blocker_values):
        decisions.append({
            "id": "D1",
            "question": "Which folder contains the game?",
            "selected": "",
            "choices": [
                {
                    "value": choice,
                    "label": choice,
                    "description": f"Use {choice} as the game folder.",
                    "recommended": False,
                }
                for choice in _game_roots(Path(str(session["target"]["path"])))
            ],
        })
    state = str(session["state"])
    display = "restored" if state == "restored_failure" else state
    check = session.get("check") if isinstance(session.get("check"), Mapping) else None
    issue_count = int(check.get("existing_issue_count") or 0) if check else 0
    details = {
        "ready": "The exact kit change is ready for review.",
        "blocked": "Resolve the listed blockers, then prepare a new review.",
        "applying": "Applying the exact reviewed kit change.",
        "checking": "Checking the installed kit without starting Godot.",
        "recovery_required": "The kit change is safe, but restoring the previous state still needs to finish.",
        "complete": "The kit change passed its offline checks.",
        "adoption_required": "The kit works. Existing project gaps still need adoption work.",
        "restored_failure": "The kit check failed, so the previous project state was restored.",
        "restored": "The project was restored to its previous state.",
        "failed": "The kit change could not finish safely.",
    }
    current = material.get("current") if isinstance(material.get("current"), Mapping) else {}
    blockers = [
        f"[{item.get('code') or 'blocked'}] "
        f"{(str(item.get('path')) + ': ') if item.get('path') else ''}"
        f"{item.get('detail') or 'The change cannot continue.'}"
        for item in blocker_values
    ]
    return {
        "session_id": str(session["session_id"]),
        "mode": str(session["request"]["mode"]),
        "status": display,
        "project": {
            "name": Path(str(session["target"]["path"])).name,
            "path": str(session["target"]["path"]),
        },
        "current_version": str(current.get("kit_version") or ""),
        "incoming_version": str(session["release"].get("version") or ""),
        "plan_sha256": str(session["preview"]["sha256"]),
        "counts": {
            "kit_files": kit_files,
            "shared_files": shared_files,
            "removed_files": removed_files,
            "game_files": 0,
        },
        "design": "Not part of this kit change",
        "existing_gaps": {
            "count": issue_count,
            "status": "checked" if check is not None else "not_checked",
            "issues": list(check.get("existing_issues", [])) if check else [],
        },
        "decisions": decisions,
        "files": files,
        "recovery": (
            "Previous state restored"
            if state in {"restored", "restored_failure"}
            else "Previous state is saved; recovery still needs to finish"
            if state == "recovery_required"
            else (
                "Previous state is saved and can be restored"
                if session.get("transaction_id") is not None
                else "Apply will save the previous state before changing project files"
            )
        ),
        "blockers": blockers,
        "detail": str(check.get("detail") or details[state]) if check else details[state],
        "result_sha256": str(session.get("result_sha256") or ""),
        "plan_url": plan_url,
    }


def _public(session: Mapping[str, Any], plan_url: str = "") -> ControllerResult:
    return {
        "ok": session["state"] in {"ready", "complete", "adoption_required", "restored"},
        "session_id": str(session["session_id"]),
        "session_sha256": str(session["session_sha256"]),
        "kit_change": _simple_state(session, plan_url),
    }


def status(runtime_root: Path, session_id: str, *, plan_url: str = "") -> ControllerResult:
    """Return the exact state consumed by ``kit_change_html.render``."""
    return _public(load(runtime_root, session_id), plan_url)


def prepare(
    runtime_root: Path,
    target_project: Path,
    release_source: Path,
    mode: str,
    *,
    game_root: str | None = None,
) -> ControllerResult:
    """Persist one target-read-only install or upgrade review."""
    requested_mode = str(mode or "").strip().lower()
    if requested_mode not in MODES:
        raise KitChangeControllerError("mode-invalid", "mode must be install or upgrade")
    runtime, target = _separate_roots(runtime_root, target_project)
    _directory(runtime, CONTROLLER_ROOT)
    try:
        with process_supervisor.exclusive_file_lock(_target(runtime, LOCK_PATH), label="kit change controller"):
            report, member_count, archive = _retain_release(runtime, release_source)
            try:
                raw = kit_change.preview(target, archive, game_root=game_root)
            except kit_change.KitChangeError as exc:
                raise KitChangeControllerError(exc.code, exc.detail) from exc
            material = raw.get("material")
            approval = raw.get("approval")
            if not isinstance(material, Mapping) or not isinstance(approval, Mapping):
                raise KitChangeControllerError("preview-invalid", "lifecycle preview is malformed")
            detected = material.get("operation")
            if detected != requested_mode:
                raise KitChangeControllerError(
                    "mode-mismatch",
                    f"requested {requested_mode}, but this project requires {detected}",
                )
            plan_sha = str(approval.get("sha256") or "")
            if SHA256_RE.fullmatch(plan_sha) is None:
                raise KitChangeControllerError("preview-invalid", "preview has no full fingerprint")
            game_state = material.get("game_root")
            selected = game_state.get("value") if isinstance(game_state, Mapping) else None
            digest = str(report.get("archive_sha256") or "")
            attempt = 0
            while attempt < MAX_ATTEMPTS:
                session_id = _session_id(
                    target,
                    digest,
                    plan_sha,
                    requested_mode,
                    game_root,
                    selected,
                    attempt,
                )
                path = _session_path(runtime, session_id)
                if not path.exists():
                    break
                existing = load(runtime, session_id)
                if existing["state"] not in {"restored", "restored_failure", "failed"}:
                    return _public(existing)
                attempt += 1
            if attempt >= MAX_ATTEMPTS:
                raise KitChangeControllerError(
                    "attempt-limit", "this exact kit change has too many prior attempts"
                )
            session: dict[str, Any] = {
                "schema": SESSION_SCHEMA,
                "kind": SESSION_KIND,
                "session_id": session_id,
                "attempt": attempt,
                "target": {"path": str(target)},
                "release": {
                    "archive": archive.relative_to(runtime).as_posix(),
                    "archive_sha256": digest,
                    "container_sha256": str(report.get("container_sha256") or ""),
                    "version": str(report.get("version") or ""),
                    "member_count": member_count,
                },
                "request": {
                    "mode": requested_mode,
                    "game_root": game_root,
                    "selected_game_root": selected,
                },
                "preview": {"sha256": plan_sha, "raw": raw},
                "state": "blocked" if material.get("blockers") else "ready",
                "sequence": 0,
                "transaction_id": None,
                "check": None,
                "result": None,
                "result_sha256": "",
                "restore": None,
                "baseline": {"prior": None, "generated": None},
                "session_sha256": "",
            }
            return _public(_save(runtime, session))
    except process_supervisor.ExclusiveLockUnavailable as exc:
        raise KitChangeControllerError("controller-busy", str(exc)) from exc


def reprepare(
    runtime_root: Path,
    session_id: str,
    choices: Mapping[str, str],
) -> ControllerResult:
    """Create a new read-only review for one exact advertised D1 choice."""
    _runtime_value, session, archive = _load(runtime_root, session_id, refresh_open=True)
    if session["state"] != "blocked":
        raise KitChangeControllerError("decision-not-available", "this review has no open decision")
    if not isinstance(choices, Mapping) or set(choices) != {"D1"}:
        raise KitChangeControllerError("decision-invalid", "choices must contain only D1")
    selected = choices.get("D1")
    if not isinstance(selected, str) or not selected:
        raise KitChangeControllerError("decision-invalid", "D1 must select one advertised folder")
    view = _simple_state(session)
    decisions = view.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != 1 or decisions[0].get("id") != "D1":
        raise KitChangeControllerError("decision-not-available", "D1 is not advertised by this review")
    advertised = {
        str(item.get("value"))
        for item in decisions[0].get("choices", [])
        if isinstance(item, Mapping)
    }
    if selected not in advertised:
        raise KitChangeControllerError("decision-invalid", "D1 choice was not advertised")
    blockers = session["preview"]["raw"]["material"].get("blockers", [])
    if any(
        not isinstance(item, Mapping) or item.get("code") != "game-root-ambiguous"
        for item in blockers
    ):
        raise KitChangeControllerError(
            "decision-not-available", "this review has another blocker that D1 cannot resolve"
        )
    return prepare(
        runtime_root,
        Path(str(session["target"]["path"])),
        archive,
        str(session["request"]["mode"]),
        game_root=selected,
    )


def _transition(
    runtime: Path, session: Mapping[str, Any], state: str, **changes: object
) -> dict[str, Any]:
    value = dict(session)
    value.update(changes)
    value["state"] = state
    value["sequence"] = int(session["sequence"]) + 1
    return _save(runtime, value)


def _failpoint(_name: str) -> None:
    """Test-only crash boundary around durable cross-module transitions."""


def _baseline_snapshot(target: Path) -> dict[str, object]:
    try:
        content = brownfield.read_baseline_file(target)
    except (OSError, ValueError) as exc:
        raise KitChangeControllerError("baseline-unsafe", str(exc)) from exc
    if content is None:
        return {"exists": False, "sha256": "", "content_base64": ""}
    return {
        "exists": True,
        "sha256": _sha256(content),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def _content_snapshot(content: bytes) -> dict[str, object]:
    if len(content) > brownfield.MAX_DOCUMENT_BYTES:
        raise KitChangeControllerError("check-invalid", "generated baseline is too large")
    return {
        "exists": True,
        "sha256": _sha256(content),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }


def _record_baseline_intent(
    runtime: Path,
    session: Mapping[str, Any],
    content: bytes,
) -> dict[str, Any]:
    intended = _content_snapshot(content)
    baseline = _validate_baseline_state(session.get("baseline"))
    prior = baseline.get("prior")
    if prior is None:
        raise KitChangeControllerError("session-invalid", "prior baseline snapshot is missing")
    generated = baseline.get("generated")
    if isinstance(generated, Mapping):
        if not _same_snapshot(generated, intended):
            raise KitChangeControllerError(
                "baseline-changed",
                "brownfield scan changed after its generated baseline was recorded",
            )
        return dict(session)
    return _transition(
        runtime,
        session,
        "checking",
        baseline={"prior": prior, "generated": intended},
    )


def _snapshot_content(snapshot: Mapping[str, object]) -> bytes | None:
    validated = _validate_baseline_snapshot(snapshot)
    assert validated is not None
    if not validated["exists"]:
        return None
    return base64.b64decode(str(validated["content_base64"]).encode("ascii"), validate=True)


def _same_snapshot(first: Mapping[str, object], second: Mapping[str, object]) -> bool:
    return (
        first.get("exists") == second.get("exists")
        and first.get("sha256") == second.get("sha256")
    )


def _record_generated_baseline(
    runtime: Path,
    session: Mapping[str, Any],
    expected_sha256: str,
) -> dict[str, Any]:
    if SHA256_RE.fullmatch(expected_sha256) is None:
        raise KitChangeControllerError("check-invalid", "generated baseline identity is malformed")
    target = Path(str(session["target"]["path"]))
    generated = _baseline_snapshot(target)
    if not generated["exists"] or generated["sha256"] != expected_sha256:
        raise KitChangeControllerError("baseline-changed", "generated baseline changed before it was recorded")
    baseline = _validate_baseline_state(session.get("baseline"))
    prior_generated = baseline.get("generated")
    if isinstance(prior_generated, Mapping) and not _same_snapshot(prior_generated, generated):
        raise KitChangeControllerError("baseline-changed", "generated baseline no longer matches this session")
    return _transition(
        runtime,
        session,
        "checking",
        baseline={"prior": baseline["prior"], "generated": generated},
    )


def _baseline_restore_precheck(session: Mapping[str, Any]) -> None:
    baseline = _validate_baseline_state(session.get("baseline"))
    generated = baseline.get("generated")
    if generated is None:
        return
    current = _baseline_snapshot(Path(str(session["target"]["path"])))
    prior = baseline.get("prior")
    if (
        not _same_snapshot(current, generated)
        and not (isinstance(prior, Mapping) and _same_snapshot(current, prior))
    ):
        raise KitChangeControllerError(
            "baseline-changed",
            "brownfield baseline changed after the kit check; restore was refused",
        )


def _restore_prior_baseline(session: Mapping[str, Any]) -> None:
    baseline = _validate_baseline_state(session.get("baseline"))
    prior = baseline.get("prior")
    generated = baseline.get("generated")
    if prior is None or generated is None:
        return
    assert isinstance(prior, Mapping) and isinstance(generated, Mapping)
    target = Path(str(session["target"]["path"]))
    current = _baseline_snapshot(target)
    if _same_snapshot(current, prior):
        return
    if not _same_snapshot(current, generated):
        raise KitChangeControllerError(
            "baseline-changed",
            "brownfield baseline has a third version; automatic restore was refused",
        )
    path = kit_change._target(target, brownfield.BASELINE_RELATIVE)  # noqa: SLF001
    content = _snapshot_content(prior)
    try:
        if content is None:
            path.unlink()
        else:
            kit_change._atomic_bytes(path, content, "0644")  # noqa: SLF001
    except (FileNotFoundError, OSError, kit_change.KitChangeError) as exc:
        # Missing is acceptable only for the previously absent baseline.
        if not (content is None and isinstance(exc, FileNotFoundError)):
            detail = exc.detail if isinstance(exc, kit_change.KitChangeError) else str(exc)
            raise KitChangeControllerError("baseline-restore-failed", detail) from exc
    restored = _baseline_snapshot(target)
    if not _same_snapshot(restored, prior):
        raise KitChangeControllerError("baseline-restore-failed", "prior baseline was not restored exactly")


def default_post_apply_check(
    target: Path, session: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Run the real engine-free proof supplied by the installed core."""
    return kit_change_check.run(target, session)


def _check_result(value: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"kit_ok", "project_ok", "existing_issues", "detail"}
    if set(value) not in (allowed, allowed | {"baseline_sha256"}):
        raise KitChangeControllerError("check-invalid", "check fields are not exact")
    if not isinstance(value["kit_ok"], bool) or not isinstance(value["project_ok"], bool):
        raise KitChangeControllerError("check-invalid", "check outcomes must be true or false")
    detail = value["detail"]
    if not isinstance(detail, str) or len(detail) > MAX_DETAIL_CHARS or "\n" in detail:
        raise KitChangeControllerError("check-invalid", "check detail is not bounded text")
    raw_issues = value["existing_issues"]
    if isinstance(raw_issues, int) and not isinstance(raw_issues, bool):
        if not 0 <= raw_issues <= MAX_ISSUES:
            raise KitChangeControllerError("check-invalid", "existing issue count is out of range")
        issues: list[dict[str, object]] = []
        count = raw_issues
    elif isinstance(raw_issues, Sequence) and not isinstance(raw_issues, (str, bytes)):
        if len(raw_issues) > MAX_ISSUES:
            raise KitChangeControllerError("check-invalid", "existing issue list is too large")
        issues = []
        for item in raw_issues:
            if not isinstance(item, Mapping) or set(item) != ISSUE_KEYS:
                raise KitChangeControllerError("check-invalid", "existing issue fields are not exact")
            issue = dict(item)
            if (
                not all(isinstance(issue[key], str) for key in ("stage", "code", "path", "message_sha256"))
                or not isinstance(issue["line"], int)
                or not SHA256_RE.fullmatch(str(issue["message_sha256"]))
            ):
                raise KitChangeControllerError("check-invalid", "existing issue is malformed")
            issues.append(issue)
        issues.sort(key=lambda item: (
            str(item["stage"]), str(item["path"]), int(item["line"]), str(item["code"])
        ))
        count = len(issues)
    else:
        raise KitChangeControllerError("check-invalid", "existing_issues is malformed")
    baseline_sha256 = str(value.get("baseline_sha256") or "")
    if baseline_sha256 and SHA256_RE.fullmatch(baseline_sha256) is None:
        raise KitChangeControllerError("check-invalid", "baseline identity is malformed")
    return {
        "kit_ok": value["kit_ok"],
        "project_ok": value["project_ok"],
        "existing_issues": issues,
        "existing_issue_count": count,
        "detail": detail,
        "baseline_sha256": baseline_sha256,
    }


def _lifecycle(value: Mapping[str, Any] | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "status": str(value.get("status") or ""),
        "transaction_id": value.get("transaction_id"),
        "preview_sha256": str(value.get("preview_sha256") or ""),
    }


def _finish(
    runtime: Path,
    session: Mapping[str, Any],
    state: str,
    check: Mapping[str, Any],
    rollback: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "schema": RESULT_SCHEMA,
        "kind": RESULT_KIND,
        "session_id": session["session_id"],
        "plan_sha256": session["preview"]["sha256"],
        "transaction_id": session.get("transaction_id"),
        "status": state,
        "check": dict(check),
        "rollback": _lifecycle(rollback),
    }
    return _transition(
        runtime,
        session,
        state,
        check=dict(check),
        result=result,
        result_sha256=_sha256(_canonical(result)),
        restore=_lifecycle(rollback) if rollback is not None else session.get("restore"),
    )


def _base_recovery_check(session: Mapping[str, Any]) -> dict[str, Any]:
    stored = session.get("check")
    if not isinstance(stored, Mapping):
        raise KitChangeControllerError(
            "session-invalid", "recovery requires the stored offline check"
        )
    required = {"kit_ok", "project_ok", "existing_issues", "detail"}
    candidate = {key: stored.get(key) for key in required}
    if stored.get("baseline_sha256"):
        candidate["baseline_sha256"] = stored.get("baseline_sha256")
    check = _check_result(candidate)
    check["detail"] = str(check["detail"]).split(RECOVERY_DETAIL_MARKER, 1)[0]
    return check


def _attempt_rollback(session: Mapping[str, Any]) -> Mapping[str, Any]:
    transaction_id = session.get("transaction_id")
    _baseline_restore_precheck(session)
    _restore_prior_baseline(session)
    if transaction_id is not None:
        return kit_change.rollback(
            Path(str(session["target"]["path"])), str(transaction_id)
        )
    return {
        "status": "rolled_back",
        "transaction_id": None,
        "preview_sha256": session["preview"]["sha256"],
    }


def _restore_or_require(
    runtime: Path,
    session: Mapping[str, Any],
    check: Mapping[str, Any],
    *,
    manual: bool,
) -> dict[str, Any]:
    base = dict(check)
    base["detail"] = str(base.get("detail") or "").split(
        RECOVERY_DETAIL_MARKER, 1
    )[0]
    try:
        rollback = _attempt_rollback(session)
    except (kit_change.KitChangeError, KitChangeControllerError) as exc:
        failed = dict(base)
        failed["detail"] = (
            f"{base['detail']}{RECOVERY_DETAIL_MARKER}{exc.detail}"
        )[:MAX_DETAIL_CHARS]
        return _transition(
            runtime,
            session,
            "recovery_required",
            check=failed,
        )
    if manual:
        return _transition(
            runtime,
            session,
            "restored",
            check=base,
            restore=_lifecycle(rollback),
        )
    return _finish(runtime, session, "restored_failure", base, rollback)


def _run_check(
    runtime: Path, session: Mapping[str, Any], hook: PostApplyCheck
) -> dict[str, Any]:
    try:
        if hook is default_post_apply_check:
            active_session = dict(session)

            def record_intent(content: bytes) -> Mapping[str, Any]:
                nonlocal active_session
                active_session = _record_baseline_intent(
                    runtime, active_session, content
                )
                return active_session

            raw = kit_change_check.run(
                Path(str(session["target"]["path"])),
                active_session,
                baseline_ready=record_intent,
            )
            session = active_session
        else:
            raw = hook(Path(str(session["target"]["path"])), session)
        if not isinstance(raw, Mapping):
            raise KitChangeControllerError("check-invalid", "check returned no result")
        check = _check_result(raw)
    except Exception as exc:
        detail = exc.detail if isinstance(exc, KitChangeControllerError) else f"checking_failed: {type(exc).__name__}"
        check = {
            "kit_ok": False,
            "project_ok": False,
            "existing_issues": [],
            "existing_issue_count": 0,
            "detail": str(detail)[:MAX_DETAIL_CHARS],
            "baseline_sha256": "",
        }
    baseline_sha256 = str(check.get("baseline_sha256") or "")
    if baseline_sha256:
        try:
            session = _record_generated_baseline(
                runtime, session, baseline_sha256
            )
        except KitChangeControllerError as exc:
            check = {
                "kit_ok": False,
                "project_ok": False,
                "existing_issues": [],
                "existing_issue_count": 0,
                "detail": exc.detail[:MAX_DETAIL_CHARS],
                "baseline_sha256": "",
            }
            if exc.code == "baseline-changed":
                return _transition(runtime, session, "checking", check=check)
            return _finish(runtime, session, "failed", check)
    if check["kit_ok"]:
        return _finish(
            runtime, session, "complete" if check["project_ok"] else "adoption_required", check
        )
    return _restore_or_require(runtime, session, check, manual=False)


def apply(
    runtime_root: Path,
    session_id: str,
    plan_sha256: str,
    *,
    post_apply_check: PostApplyCheck | None = None,
) -> ControllerResult:
    """Apply one full review fingerprint, then check or restore it."""
    if SHA256_RE.fullmatch(str(plan_sha256 or "")) is None:
        raise KitChangeControllerError("approval-invalid", "apply needs the full review fingerprint")
    runtime = _root(runtime_root, "controller runtime")
    try:
        with process_supervisor.exclusive_file_lock(_target(runtime, LOCK_PATH), label="kit change controller"):
            runtime, session, archive = _load(runtime, session_id, refresh_open=True)
            if not hmac.compare_digest(str(session["preview"]["sha256"]), plan_sha256):
                raise KitChangeControllerError("approval-mismatch", "review fingerprint does not match")
            if session["state"] in FINAL_STATES:
                return _public(session)
            if session["state"] in {"applying", "checking", "recovery_required"}:
                return _public(_recover(runtime, session, post_apply_check or default_post_apply_check))
            if session["state"] == "blocked":
                raise KitChangeControllerError("preview-blocked", "the review still has blockers")
            baseline = _validate_baseline_state(session.get("baseline"))
            if baseline["prior"] is None:
                baseline = {
                    "prior": _baseline_snapshot(Path(str(session["target"]["path"]))),
                    "generated": None,
                }
            session = _transition(runtime, session, "applying", baseline=baseline)
            try:
                changed = kit_change.apply(
                    Path(str(session["target"]["path"])),
                    archive,
                    plan_sha256,
                    game_root=session["request"].get("game_root"),
                )
                _failpoint("after-lifecycle-apply")
            except kit_change.KitChangeError as exc:
                check = {
                    "kit_ok": False,
                    "project_ok": False,
                    "existing_issues": [],
                    "existing_issue_count": 0,
                    "detail": exc.detail[:MAX_DETAIL_CHARS],
                    "baseline_sha256": "",
                }
                state = "restored_failure" if exc.code == "apply-failed" else "failed"
                return _public(_finish(runtime, session, state, check))
            session = _transition(
                runtime, session, "checking", transaction_id=changed.get("transaction_id")
            )
            return _public(_run_check(runtime, session, post_apply_check or default_post_apply_check))
    except process_supervisor.ExclusiveLockUnavailable as exc:
        raise KitChangeControllerError("controller-busy", str(exc)) from exc


def restore(runtime_root: Path, session_id: str, result_sha256: str) -> ControllerResult:
    """Restore the transaction bound to one full checked-result fingerprint."""
    if SHA256_RE.fullmatch(str(result_sha256 or "")) is None:
        raise KitChangeControllerError("result-invalid", "restore needs the full result fingerprint")
    runtime = _root(runtime_root, "controller runtime")
    try:
        with process_supervisor.exclusive_file_lock(_target(runtime, LOCK_PATH), label="kit change controller"):
            runtime, session, _archive = _load(runtime, session_id, refresh_open=False)
            if not hmac.compare_digest(str(session.get("result_sha256") or ""), result_sha256):
                raise KitChangeControllerError("result-mismatch", "result fingerprint does not match")
            if session["state"] == "restored":
                return _public(session)
            if session["state"] not in {
                "complete",
                "adoption_required",
                "recovery_required",
            }:
                raise KitChangeControllerError("restore-not-available", "no applied change can be restored")
            return _public(
                _restore_or_require(
                    runtime,
                    session,
                    _base_recovery_check(session),
                    manual=True,
                )
            )
    except process_supervisor.ExclusiveLockUnavailable as exc:
        raise KitChangeControllerError("controller-busy", str(exc)) from exc


def _recover(
    runtime: Path, session: Mapping[str, Any], hook: PostApplyCheck
) -> dict[str, Any]:
    if session["state"] == "recovery_required":
        result = session.get("result")
        manual = (
            isinstance(result, Mapping)
            and result.get("status") in {"complete", "adoption_required"}
            and bool(session.get("result_sha256"))
        )
        return _restore_or_require(
            runtime,
            session,
            _base_recovery_check(session),
            manual=manual,
        )
    if session["state"] in FINAL_STATES | OPEN_STATES:
        return dict(session)
    if session["state"] == "checking":
        return _run_check(runtime, session, hook)
    raw = session["preview"].get("raw")
    material = raw.get("material") if isinstance(raw, Mapping) else None
    target_scope = str(material.get("target_scope_sha256") or "") if isinstance(material, Mapping) else ""
    inspected: Mapping[str, Any] | None = None
    try:
        inspected = kit_change.inspect_transaction(
            Path(str(session["target"]["path"])),
            preview_sha256=str(session["preview"]["sha256"]),
            target_scope_sha256=target_scope,
        )
    except kit_change.KitChangeError as exc:
        if exc.code != "transaction-missing":
            check = {
                "kit_ok": False,
                "project_ok": False,
                "existing_issues": [],
                "existing_issue_count": 0,
                "detail": exc.detail[:MAX_DETAIL_CHARS],
                "baseline_sha256": "",
            }
            return _finish(runtime, session, "failed", check)
    if inspected is not None and inspected.get("status") == "applied":
        session = _transition(
            runtime,
            session,
            "checking",
            transaction_id=inspected.get("transaction_id"),
        )
        return _run_check(runtime, session, hook)
    if inspected is not None and inspected.get("status") == "rolled_back":
        check = {
            "kit_ok": False,
            "project_ok": False,
            "existing_issues": [],
            "existing_issue_count": 0,
            "detail": "Interrupted kit change was restored before checking.",
            "baseline_sha256": "",
        }
        value = dict(session)
        value["transaction_id"] = inspected.get("transaction_id")
        return _finish(runtime, value, "restored_failure", check, inspected)
    try:
        lifecycle = kit_change.resume(Path(str(session["target"]["path"])))
    except kit_change.KitChangeError as exc:
        check = {
            "kit_ok": False,
            "project_ok": False,
            "existing_issues": [],
            "existing_issue_count": 0,
            "detail": exc.detail[:MAX_DETAIL_CHARS],
            "baseline_sha256": "",
        }
        return _finish(runtime, session, "failed", check)
    if lifecycle.get("status") == "rolled_back":
        check = {
            "kit_ok": False,
            "project_ok": False,
            "existing_issues": [],
            "existing_issue_count": 0,
            "detail": "Interrupted kit change was restored before checking.",
            "baseline_sha256": "",
        }
        value = dict(session)
        value["transaction_id"] = lifecycle.get("transaction_id")
        return _finish(runtime, value, "restored_failure", check, lifecycle)
    if lifecycle.get("status") != "applied":
        raise KitChangeControllerError("recovery-invalid", "transaction returned an unknown state")
    session = _transition(
        runtime, session, "checking", transaction_id=lifecycle.get("transaction_id")
    )
    return _run_check(runtime, session, hook)


def recover(
    runtime_root: Path,
    session_id: str,
    *,
    post_apply_check: PostApplyCheck | None = None,
) -> ControllerResult:
    """Resume one interrupted transaction, then check or restore it."""
    runtime = _root(runtime_root, "controller runtime")
    try:
        with process_supervisor.exclusive_file_lock(_target(runtime, LOCK_PATH), label="kit change controller"):
            runtime, session, _archive = _load(runtime, session_id, refresh_open=True)
            return _public(_recover(runtime, session, post_apply_check or default_post_apply_check))
    except process_supervisor.ExclusiveLockUnavailable as exc:
        raise KitChangeControllerError("controller-busy", str(exc)) from exc
