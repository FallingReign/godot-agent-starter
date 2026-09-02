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
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypedDict

import kit_change
import process_supervisor
import release


SESSION_SCHEMA = 1
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
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MODES = {"install", "upgrade"}
OPEN_STATES = {"ready", "blocked"}
FINAL_STATES = {
    "complete",
    "adoption_required",
    "restored_failure",
    "restored",
    "failed",
}
STATES = OPEN_STATES | FINAL_STATES | {"applying", "checking"}
ISSUE_KEYS = {"stage", "code", "path", "line", "message_sha256"}


class KitChangeView(TypedDict):
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
    runtime = _runtime(runtime_root)
    target = _root(target_root, "target project")
    if _inside(runtime, target) or _inside(target, runtime):
        raise KitChangeControllerError(
            "runtime-overlap",
            "controller runtime and target project must be separate directories",
        )
    return runtime, target


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


def _archive_payload(path: Path, expected_sha256: str) -> bytes:
    limit = int(getattr(release, "MAX_ARCHIVE_BYTES", 512 * 1024 * 1024))
    content = _stable(path, limit, "release archive")
    if not hmac.compare_digest(_sha256(content), expected_sha256):
        raise KitChangeControllerError(
            "release-changed", "release archive changed after verification"
        )
    return content


def _archive_path(runtime: Path, digest: str) -> Path:
    if SHA256_RE.fullmatch(digest) is None:
        raise KitChangeControllerError("release-invalid", "release identity is malformed")
    return _directory(runtime, RELEASES_ROOT) / f"{digest}.zip"


def _retain_release(runtime: Path, source: Path) -> tuple[dict[str, Any], int, Path]:
    try:
        if source.is_dir():
            report, members = release.read_verified_directory(source)
            digest = str(report.get("archive_sha256") or "")
            stored = _archive_path(runtime, digest)
            materialized = release.materialize_verified_directory_zip(source, stored)
            if (
                materialized.get("archive_sha256") != digest
                or materialized.get("version") != report.get("version")
            ):
                raise KitChangeControllerError(
                    "release-changed", "release changed while it was retained"
                )
        else:
            report, members = release.read_verified_archive(source)
            digest = str(report.get("archive_sha256") or "")
            content = _archive_payload(source, digest)
            stored = _archive_path(runtime, digest)
            if stored.exists():
                if _stable(stored, len(content), "retained release") != content:
                    raise KitChangeControllerError(
                        "release-conflict", "retained release does not match its identity"
                    )
            else:
                _atomic(stored, content)
            verified = release.verify_archive(stored)
            if (
                verified.get("archive_sha256") != digest
                or verified.get("version") != report.get("version")
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
    stored = _archive_path(runtime, digest)
    expected = stored.relative_to(runtime).as_posix()
    if release_state.get("archive") != expected:
        raise KitChangeControllerError("session-invalid", "release path is not canonical")
    _archive_payload(stored, digest)
    try:
        report = release.verify_archive(stored)
    except release.ReleaseError as exc:
        raise KitChangeControllerError("release-tampered", str(exc)) from exc
    if report.get("version") != release_state.get("version"):
        raise KitChangeControllerError("release-tampered", "retained release metadata changed")
    return stored


def _session_id(
    target: Path,
    archive_sha256: str,
    preview_sha256: str,
    mode: str,
    requested_game_root: str | None,
    selected_game_root: object,
) -> str:
    return _sha256(_canonical({
        "target": str(target),
        "archive_sha256": archive_sha256,
        "preview_sha256": preview_sha256,
        "mode": mode,
        "requested_game_root": requested_game_root,
        "selected_game_root": selected_game_root,
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
        "result_sha256", "restore", "session_sha256",
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
    if value["state"] not in STATES or not isinstance(value["sequence"], int):
        raise KitChangeControllerError("session-invalid", "session state is malformed")
    for key in ("target", "release", "request", "preview"):
        if not isinstance(value[key], Mapping):
            raise KitChangeControllerError("session-invalid", f"session {key} is malformed")
    plan_sha = str(value["preview"].get("sha256") or "")
    archive_sha = str(value["release"].get("archive_sha256") or "")
    mode = value["request"].get("mode")
    if SHA256_RE.fullmatch(plan_sha) is None or SHA256_RE.fullmatch(archive_sha) is None or mode not in MODES:
        raise KitChangeControllerError("session-invalid", "session identity is malformed")
    target = _root(Path(str(value["target"].get("path") or "")), "target project")
    expected_id = _session_id(
        target, archive_sha, plan_sha, str(mode), value["request"].get("game_root"),
        value["request"].get("selected_game_root"),
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
    return value


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
    target = _root(Path(str(session["target"]["path"])), "target project")
    if _inside(runtime, target) or _inside(target, runtime):
        raise KitChangeControllerError("runtime-overlap", "controller runtime overlaps the target")
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
    display = "complete" if state == "adoption_required" else "restored" if state == "restored_failure" else state
    check = session.get("check") if isinstance(session.get("check"), Mapping) else None
    issue_count = int(check.get("existing_issue_count") or 0) if check else 0
    details = {
        "ready": "The exact kit change is ready for review.",
        "blocked": "Resolve the listed blockers, then prepare a new review.",
        "applying": "Applying the exact reviewed kit change.",
        "checking": "Checking the installed kit without starting Godot.",
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
        },
        "decisions": decisions,
        "files": files,
        "recovery": (
            "Previous state restored"
            if state in {"restored", "restored_failure"}
            else "Previous state is saved and can be restored"
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
            session_id = _session_id(target, digest, plan_sha, requested_mode, game_root, selected)
            session: dict[str, Any] = {
                "schema": SESSION_SCHEMA,
                "kind": SESSION_KIND,
                "session_id": session_id,
                "target": {"path": str(target)},
                "release": {
                    "archive": archive.relative_to(runtime).as_posix(),
                    "archive_sha256": digest,
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
                "session_sha256": "",
            }
            path = _session_path(runtime, session_id)
            if path.exists():
                return _public(load(runtime, session_id))
            return _public(_save(runtime, session))
    except process_supervisor.ExclusiveLockUnavailable as exc:
        raise KitChangeControllerError("controller-busy", str(exc)) from exc


def _transition(
    runtime: Path, session: Mapping[str, Any], state: str, **changes: object
) -> dict[str, Any]:
    value = dict(session)
    value.update(changes)
    value["state"] = state
    value["sequence"] = int(session["sequence"]) + 1
    return _save(runtime, value)


def default_post_apply_check(
    _target: Path, _session: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Fail closed until the caller connects the installed-core static scan."""
    return {
        "kit_ok": False,
        "project_ok": False,
        "existing_issues": [],
        "detail": "checking_not_configured",
    }


def _check_result(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {"kit_ok", "project_ok", "existing_issues", "detail"}:
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
    return {
        "kit_ok": value["kit_ok"],
        "project_ok": value["project_ok"],
        "existing_issues": issues,
        "existing_issue_count": count,
        "detail": detail,
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


def _run_check(
    runtime: Path, session: Mapping[str, Any], hook: PostApplyCheck
) -> dict[str, Any]:
    try:
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
        }
    if check["kit_ok"]:
        return _finish(
            runtime, session, "complete" if check["project_ok"] else "adoption_required", check
        )
    transaction_id = session.get("transaction_id")
    try:
        rollback = (
            kit_change.rollback(Path(str(session["target"]["path"])), str(transaction_id))
            if transaction_id is not None
            else {
                "status": "rolled_back",
                "transaction_id": None,
                "preview_sha256": session["preview"]["sha256"],
            }
        )
    except kit_change.KitChangeError as exc:
        failed = dict(check)
        failed["detail"] = f"{check['detail']}; restore failed: {exc.detail}"[:MAX_DETAIL_CHARS]
        return _finish(runtime, session, "failed", failed)
    return _finish(runtime, session, "restored_failure", check, rollback)


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
            if session["state"] in {"applying", "checking"}:
                return _public(_recover(runtime, session, post_apply_check or default_post_apply_check))
            if session["state"] == "blocked":
                raise KitChangeControllerError("preview-blocked", "the review still has blockers")
            session = _transition(runtime, session, "applying")
            try:
                changed = kit_change.apply(
                    Path(str(session["target"]["path"])),
                    archive,
                    plan_sha256,
                    game_root=session["request"].get("game_root"),
                )
            except kit_change.KitChangeError as exc:
                check = {
                    "kit_ok": False,
                    "project_ok": False,
                    "existing_issues": [],
                    "existing_issue_count": 0,
                    "detail": exc.detail[:MAX_DETAIL_CHARS],
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
            if session["state"] not in {"complete", "adoption_required"}:
                raise KitChangeControllerError("restore-not-available", "no applied change can be restored")
            transaction_id = session.get("transaction_id")
            try:
                rolled_back = (
                    kit_change.rollback(Path(str(session["target"]["path"])), str(transaction_id))
                    if transaction_id is not None
                    else {
                        "status": "rolled_back",
                        "transaction_id": None,
                        "preview_sha256": session["preview"]["sha256"],
                    }
                )
            except kit_change.KitChangeError as exc:
                raise KitChangeControllerError(exc.code, exc.detail) from exc
            return _public(_transition(runtime, session, "restored", restore=_lifecycle(rolled_back)))
    except process_supervisor.ExclusiveLockUnavailable as exc:
        raise KitChangeControllerError("controller-busy", str(exc)) from exc


def _recover(
    runtime: Path, session: Mapping[str, Any], hook: PostApplyCheck
) -> dict[str, Any]:
    if session["state"] in FINAL_STATES | OPEN_STATES:
        return dict(session)
    if session["state"] == "checking":
        return _run_check(runtime, session, hook)
    try:
        lifecycle = kit_change.resume(Path(str(session["target"]["path"])))
    except kit_change.KitChangeError as exc:
        check = {
            "kit_ok": False,
            "project_ok": False,
            "existing_issues": [],
            "existing_issue_count": 0,
            "detail": exc.detail[:MAX_DETAIL_CHARS],
        }
        return _finish(runtime, session, "failed", check)
    if lifecycle.get("status") == "rolled_back":
        check = {
            "kit_ok": False,
            "project_ok": False,
            "existing_issues": [],
            "existing_issue_count": 0,
            "detail": "Interrupted kit change was restored before checking.",
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
