#!/usr/bin/env python3
"""Decision-relevant state shared by the plan, CLI, and loopback board.

This module deliberately has no server lifecycle or provider dependency.  It
reads the reviewed repository state, records local verification evidence, and
applies an exact human decision to ``proposal.json`` only after the caller
proves it is acting on the same proposal and design material now on disk.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import runtime_paths
import proposal_authority
import project_context
import managed_launcher
import process_supervisor
try:
    import native_engine
except ImportError:  # compatibility with older kit revisions
    native_engine = None
try:
    import design as design_contract
except ImportError:  # compatibility with kit revisions before canonical design digests
    design_contract = None

SCHEMA = 1
PROPOSAL_NAME = "proposal.json"
SHAPE_NAME = "project.shape.json"
GENERATED_PATHS = frozenset(("plan.html", "retro.html"))
GENERATED_PREFIXES = (".checklogs/", ".git/", ".kit/", "plan/")
ASSET_SUFFIXES = frozenset(
    (".md", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif")
)
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2, "very-high": 3}
ACTOR_LABEL = proposal_authority.COCKPIT_ACTOR_LABEL
AUTHORITY_TRUST = (
    "Receipts bind the exact reviewed page. They are tamper-evident policy "
    "evidence, not person authentication."
)

TOOLS = Path(__file__).resolve().parent
CORE_ROOT = TOOLS.parent
_ACTIVE_INSTALLATION = project_context.resolve_active_installation(CORE_ROOT)
PROJECT_ROOT = _ACTIVE_INSTALLATION.project_root
CHILD_ENVIRONMENT = managed_launcher.bound_environment(_ACTIVE_INSTALLATION)


def _core_for_project(root: Path) -> Path:
    """Keep synthetic flat fixtures working while selecting the active core."""
    canonical = root.resolve(strict=True)
    return CORE_ROOT if canonical == PROJECT_ROOT else canonical

_decision_lock = threading.RLock()


class CockpitError(RuntimeError):
    """A bounded cockpit operation was safely refused."""

    def __init__(self, message: str, *, code: str = "cockpit_error") -> None:
        super().__init__(message)
        self.code = code


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def approval_sha256(proposal: dict[str, Any]) -> str:
    """Canonical identity of the complete approved proposal contract."""
    return proposal_authority.approval_sha256(proposal)


def _design_digest(content: bytes) -> str:
    if design_contract is not None and hasattr(design_contract, "design_sha256"):
        return str(design_contract.design_sha256(content.decode("utf-8")))
    return hashlib.sha256(content).hexdigest()


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, ValueError) as exc:
        raise CockpitError(f"cannot read {path.name}: {exc}", code="source_invalid") from exc
    if not isinstance(value, dict):
        raise CockpitError(f"{path.name} root must be an object", code="source_invalid")
    return value


def _replace_file(temporary: Path, destination: Path) -> None:
    """Bound the short Windows sharing-violation window around atomic replace."""
    for attempt in range(5):
        try:
            os.replace(temporary, destination)
            return
        except PermissionError as exc:
            if (
                os.name != "nt"
                or getattr(exc, "winerror", None) not in (5, 32)
                or attempt == 4
            ):
                raise
            time.sleep(0.02 * (attempt + 1))


def _atomic_json_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        _replace_file(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _atomic_bytes_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        temporary.write_bytes(value)
        _replace_file(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _durable_json_write(path: Path, value: Any) -> None:
    """Atomically replace a small private journal and flush its bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    payload = (json.dumps(value, indent=2) + "\n").encode("utf-8")
    try:
        with temporary.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_file(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


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
def _decision_process_guard(paths: runtime_paths.RuntimePaths):
    """Serialize authority transitions across board/CLI processes."""
    lock = paths.plan_decision_lock
    lock.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    for _attempt in range(20):
        try:
            descriptor = os.open(
                str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY
            )
            try:
                os.write(
                    descriptor,
                    (json.dumps({"pid": os.getpid(), "at": _now_iso()}) + "\n").encode(
                        "utf-8"
                    ),
                )
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            acquired = True
            break
        except FileExistsError:
            try:
                owner = json.loads(lock.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                owner = {}
            if not _pid_alive(owner.get("pid")):
                try:
                    lock.unlink()
                    continue
                except OSError:
                    pass
            time.sleep(0.05)
    if not acquired:
        raise CockpitError(
            "Another cockpit process is recording or recovering a decision. Refresh shortly.",
            code="decision_busy",
        )
    try:
        yield
    finally:
        try:
            lock.unlink(missing_ok=True)
        except OSError:
            pass


@contextmanager
def _verification_ledger_guard(paths: runtime_paths.RuntimePaths):
    """Hold a real cross-process OS lock for ledger, pointer and warning CAS."""
    lock_path = paths.verification_runs.parent / "verification-ledger.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    acquired = False
    try:
        if lock_path.stat().st_size == 0:
            handle.write(b"\0")
            handle.flush()
        for _attempt in range(40):
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError:
                time.sleep(0.05)
        if not acquired:
            raise CockpitError(
                "Another verification is publishing evidence. The existing latest "
                "result was preserved; retry after that run finishes.",
                code="verification_busy",
            )
        yield
    finally:
        if acquired:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        handle.close()


def _transaction_path(root: Path, raw: object) -> Path:
    if not isinstance(raw, str):
        raise CockpitError("decision journal path is invalid", code="recovery_failed")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise CockpitError("decision journal path escapes the repository", code="recovery_failed")
    candidate = (root / relative).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise CockpitError("decision journal path escapes the repository", code="recovery_failed")
    return candidate


def _decode_journal_bytes(value: object) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CockpitError("decision journal bytes are invalid", code="recovery_failed")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise CockpitError("decision journal bytes are invalid", code="recovery_failed") from exc


def _recover_decision_transaction_locked(
    root: Path, paths: runtime_paths.RuntimePaths
) -> None:
    journal_path = paths.plan_decision_transaction
    if not journal_path.is_file():
        return
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CockpitError(
            "A damaged decision journal makes the cockpit read-only until repaired.",
            code="recovery_failed",
        ) from exc
    entries = journal.get("entries") if isinstance(journal, dict) else None
    state = journal.get("state") if isinstance(journal, dict) else None
    if state not in ("prepared", "committed") or not isinstance(entries, list):
        raise CockpitError("decision journal is malformed", code="recovery_failed")
    decoded: list[tuple[Path, bytes | None, bytes]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise CockpitError("decision journal entry is malformed", code="recovery_failed")
        path = _transaction_path(root, entry.get("path"))
        original = _decode_journal_bytes(entry.get("original"))
        replacement = _decode_journal_bytes(entry.get("replacement"))
        if replacement is None:
            raise CockpitError("decision journal replacement is missing", code="recovery_failed")
        decoded.append((path, original, replacement))
    for path, original, replacement in decoded:
        current = path.read_bytes() if path.is_file() else None
        allowed = (replacement, original)
        if current not in allowed:
            raise CockpitError(
                f"{path.relative_to(root).as_posix()} changed during an interrupted decision; "
                "automatic recovery refused to overwrite it.",
                code="recovery_conflict",
            )
    if state == "prepared":
        for path, original, _replacement in reversed(decoded):
            if original is None:
                path.unlink(missing_ok=True)
            else:
                _atomic_bytes_write(path, original)
    else:
        for path, _original, replacement in decoded:
            current = path.read_bytes() if path.is_file() else None
            if current != replacement:
                raise CockpitError(
                    "committed decision journal does not match its projections",
                    code="recovery_conflict",
                )
    journal_path.unlink(missing_ok=True)


def _recover_decision_transaction(root: Path) -> None:
    paths = runtime_paths.resolve(root)
    if not paths.plan_decision_transaction.is_file():
        return
    with _decision_process_guard(paths):
        _recover_decision_transaction_locked(root, paths)


def _transactional_replace(
    root: Path,
    replacements: dict[Path, bytes],
    originals: dict[Path, bytes | None],
    paths: runtime_paths.RuntimePaths,
) -> None:
    """Commit a multi-file authority projection with a recoverable WAL."""
    for path, expected in originals.items():
        current = path.read_bytes() if path.is_file() else None
        if current != expected:
            raise CockpitError(
                f"{path.relative_to(root).as_posix()} changed while the decision was being prepared. "
                "Nothing was overwritten; refresh and review again.",
                code="decision_conflict",
            )
    journal = {
        "schema": 1,
        "transaction_id": uuid.uuid4().hex,
        "state": "prepared",
        "prepared_at": _now_iso(),
        "entries": [
            {
                "path": path.relative_to(root).as_posix(),
                "original": (
                    base64.b64encode(originals[path]).decode("ascii")
                    if originals[path] is not None
                    else None
                ),
                "replacement": base64.b64encode(content).decode("ascii"),
            }
            for path, content in replacements.items()
        ],
    }
    journal_path = paths.plan_decision_transaction
    _durable_json_write(journal_path, journal)
    try:
        for path, content in replacements.items():
            _atomic_bytes_write(path, content)
        for path, content in replacements.items():
            if not path.is_file() or path.read_bytes() != content:
                raise OSError(f"decision projection verification failed for {path}")
        journal["state"] = "committed"
        journal["committed_at"] = _now_iso()
        _durable_json_write(journal_path, journal)
        journal_path.unlink(missing_ok=True)
    except Exception as failure:
        try:
            _recover_decision_transaction_locked(root, paths)
        except CockpitError as recovery:
            raise CockpitError(
                f"Decision failed and recovery is incomplete: {recovery}",
                code="rollback_failed",
            ) from failure
        raise CockpitError(
            "The decision could not be recorded; recovered the exact prior bytes.",
            code="decision_write_failed",
        ) from failure


def _is_generated(relative: str) -> bool:
    normal = relative.replace("\\", "/").lstrip("./")
    return normal in GENERATED_PATHS or normal.startswith(GENERATED_PREFIXES)


def _run_git(root: Path, *arguments: str, timeout: int = 30) -> subprocess.CompletedProcess:
    executable = process_supervisor.resolve_ordinary_executable(
        "git",
        excluded_roots=(root, CORE_ROOT),
    )
    return subprocess.run(
        [executable, "-C", str(root), *arguments],
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def repository_fingerprint(root: Path) -> dict[str, Any]:
    """Fingerprint current authored repository content without hashing Git internals.

    HEAD identifies every clean tracked byte.  A binary Git diff identifies
    index and worktree changes, while untracked authored files are streamed
    separately. Generated cockpit pages and private runtime evidence are
    excluded so recording or rendering evidence cannot make itself stale.
    """
    canonical = root.resolve(strict=True)
    digest = hashlib.sha256()
    try:
        head_proc = _run_git(canonical, "rev-parse", "--verify", "HEAD")
        if head_proc.returncode != 0:
            return {
                "available": False,
                "digest": None,
                "head": None,
                "reason": "repository has no readable HEAD",
            }
        head = head_proc.stdout.decode("utf-8", errors="replace").strip()
        digest.update(b"head\0" + head.encode("ascii", errors="replace") + b"\0")
        excludes = (
            ":(exclude)plan.html",
            ":(exclude)retro.html",
            ":(exclude)plan/**",
            ":(exclude).kit/**",
            ":(exclude).checklogs/**",
        )
        diff = _run_git(
            canonical,
            "diff",
            "--no-ext-diff",
            "--binary",
            "--full-index",
            "HEAD",
            "--",
            ".",
            *excludes,
            timeout=120,
        )
        if diff.returncode != 0:
            return {
                "available": False,
                "digest": None,
                "head": head,
                "reason": "Git could not fingerprint tracked changes",
            }
        digest.update(b"diff\0" + diff.stdout + b"\0")
        others = _run_git(
            canonical, "ls-files", "--others", "--exclude-standard", "-z"
        )
        if others.returncode != 0:
            return {
                "available": False,
                "digest": None,
                "head": head,
                "reason": "Git could not enumerate untracked files",
            }
        untracked: list[str] = []
        for raw in others.stdout.split(b"\0"):
            if not raw:
                continue
            relative = raw.decode("utf-8", errors="surrogateescape").replace("\\", "/")
            if _is_generated(relative):
                continue
            candidate = (canonical / Path(relative)).resolve(strict=False)
            if candidate != canonical and not candidate.is_relative_to(canonical):
                continue
            untracked.append(relative)
        for relative in sorted(untracked):
            candidate = canonical / Path(relative)
            digest.update(b"untracked\0" + relative.encode("utf-8", errors="surrogateescape") + b"\0")
            try:
                with candidate.open("rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
            except OSError as exc:
                digest.update(f"unreadable:{exc.__class__.__name__}".encode("ascii"))
            digest.update(b"\0")
        return {
            "available": True,
            "digest": digest.hexdigest(),
            "head": head,
            "untracked": len(untracked),
        }
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        return {
            "available": False,
            "digest": None,
            "head": None,
            "reason": f"Git fingerprint unavailable: {exc}",
        }


def _verification_scope(payload: dict[str, Any]) -> str:
    if payload.get("strict"):
        return "strict"
    if payload.get("static"):
        return "static"
    if payload.get("stages"):
        return "targeted"
    if payload.get("fast"):
        return "fast"
    return "full"


def _trusted_gate_receipt(payload: dict[str, Any]) -> bool:
    """Accept only a launcher-authenticated receipt for one stable repository.

    The launcher validates ``auth_sha256`` with its private per-run key before
    placing the summary in this payload.  The cockpit deliberately cannot
    recreate that HMAC; it checks the authenticated schema and repository
    binding rather than pretending a public consumer holds the secret.
    """
    nonce = payload.get("verification_nonce")
    summary = payload.get("gate_summary")
    repository_start = payload.get("repository_start")
    repository_digest = (
        repository_start.get("digest")
        if isinstance(repository_start, dict)
        else None
    )
    return bool(
        isinstance(nonce, str)
        and re.fullmatch(r"[0-9a-f]{32}", nonce)
        and isinstance(summary, dict)
        and summary.get("schema") == 2
        and summary.get("run_id") == nonce
        and summary.get("failed") is False
        and isinstance(summary.get("repository_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", summary["repository_sha256"])
        and summary.get("repository_sha256") == repository_digest
        and isinstance(summary.get("auth_sha256"), str)
        and re.fullmatch(r"[0-9a-f]{64}", summary["auth_sha256"])
        and payload.get("repository_stable") is True
    )


def _warning_snapshot_from_payload(payload: dict[str, Any]) -> tuple[str, str | None]:
    value = payload.get("native_warning_start")
    if not isinstance(value, dict):
        return "unknown", None
    state = value.get("state")
    identity = value.get("identity")
    if state == "clean" and identity is None:
        return "clean", None
    if (
        state == "unresolved"
        and isinstance(identity, str)
        and re.fullmatch(r"[0-9a-f]{64}", identity)
    ):
        return "unresolved", identity
    return "unknown", None


def _failure_class(payload: dict[str, Any]) -> str | None:
    summary = payload.get("gate_summary")
    diagnostics = summary.get("diagnostics") if isinstance(summary, dict) else {}
    if not isinstance(diagnostics, dict):
        return None
    crashes = diagnostics.get("native_crashes")
    if isinstance(crashes, list) and any(
        isinstance(item, dict) and item.get("code") == "native-crash"
        for item in crashes
    ):
        return "native-crash"
    timeouts = diagnostics.get("timeouts")
    if isinstance(timeouts, list) and any(
        isinstance(item, dict) and item.get("code") == "native-engine-timeout"
        for item in timeouts
    ):
        return "engine-timeout"
    refusals = diagnostics.get("engine_refusals")
    if isinstance(refusals, list) and any(
        isinstance(item, dict) and item.get("code") == "concurrent-engine-launch"
        for item in refusals
    ):
        return "concurrent-engine-launch"
    starts = diagnostics.get("engine_start_failures")
    if isinstance(starts, list) and starts:
        return "engine-start-failed"
    return None


def record_verification(root: Path, payload: dict[str, Any]) -> dict[str, Any]:
    """Durably publish evidence before resolving an exact native warning.

    The ledger and conservative latest pointer are persisted under an OS lock.
    Only then may a full/strict receipt compare-and-swap the exact warning
    identity.  A successful CAS gets its own immutable resolution event before
    the pointer becomes fresh.  Any persistence failure therefore leaves the
    warning unresolved and the previous/latest evidence conservative.
    """
    paths = runtime_paths.resolve(root, create=True)
    with _verification_ledger_guard(paths):
        start_warning_state, start_warning_identity = _warning_snapshot_from_payload(
            payload
        )
        if native_engine is not None and hasattr(
            native_engine, "snapshot_native_warning"
        ):
            end_warning = native_engine.snapshot_native_warning(root)
            end_warning_state = str(end_warning.state)
            end_warning_identity = end_warning.identity
        else:
            end_warning_state, end_warning_identity = "unknown", None
        native_warning = persisted_native_warning(root)
        if native_engine is not None and hasattr(
            native_engine, "snapshot_native_warning"
        ):
            final_warning = native_engine.snapshot_native_warning(root)
            final_warning_state = str(final_warning.state)
            final_warning_identity = final_warning.identity
        else:
            final_warning_state, final_warning_identity = "unknown", None
        warning_snapshot_stable = bool(
            start_warning_state != "unknown"
            and end_warning_state == start_warning_state == final_warning_state
            and end_warning_identity
            == start_warning_identity
            == final_warning_identity
        )
        fingerprint = repository_fingerprint(root)
        scope = _verification_scope(payload)
        passed = bool(payload.get("ok"))
        trusted_gate = _trusted_gate_receipt(payload)
        run_id = (
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-"
            f"{uuid.uuid4().hex}"
        )
        current_failure = _failure_class(payload)
        previous: dict[str, Any] = {}
        if paths.verification_latest.is_file():
            try:
                candidate = json.loads(
                    paths.verification_latest.read_text(encoding="utf-8")
                )
                if isinstance(candidate, dict):
                    previous = candidate
            except (OSError, UnicodeError, ValueError):
                previous = {}
        unresolved = (
            dict(previous.get("unresolved_failure"))
            if isinstance(previous.get("unresolved_failure"), dict)
            else None
        )
        if native_warning:
            unresolved = {
                "failure_class": native_warning.get("code"),
                "recorded_at": native_warning.get("recorded_at"),
                "operation": native_warning.get("operation"),
                "summary": native_warning.get("summary"),
            }
        if unresolved is None and previous.get("failure_class") in (
            "native-crash",
            "engine-timeout",
            "engine-start-failed",
            "concurrent-engine-launch",
            "native-warning-state-unknown",
        ) and not previous.get("passed"):
            unresolved = {
                "failure_class": previous.get("failure_class"),
                "recorded_at": previous.get("recorded_at"),
                "run_id": previous.get("id"),
            }
        if current_failure and (
            unresolved is None or unresolved.get("failure_class") != "native-crash"
        ):
            unresolved = {
                "failure_class": current_failure,
                "recorded_at": _now_iso(),
                "run_id": run_id,
            }

        cas_identity = (
            str(end_warning_identity)
            if end_warning_state == "unresolved"
            and isinstance(end_warning_identity, str)
            and re.fullmatch(r"[0-9a-f]{64}", end_warning_identity)
            else ""
        )
        resolution_candidate = bool(
            unresolved
            and passed
            and scope in ("full", "strict")
            and current_failure is None
            and trusted_gate
            and warning_snapshot_stable
            and cas_identity
        )
        if not passed:
            evidence_status = "failed"
        elif (
            scope in ("static", "targeted", "fast")
            or not fingerprint.get("available")
            or not trusted_gate
            or (scope in ("full", "strict") and not warning_snapshot_stable)
            or unresolved is not None
        ):
            evidence_status = "insufficient"
        else:
            evidence_status = "fresh"
        record = {
            "schema": SCHEMA,
            "id": run_id,
            "recorded_at": _now_iso(),
            "status": evidence_status,
            "scope": scope,
            "passed": passed,
            "command_status": str(payload.get("status") or ""),
            "exit_code": int(payload.get("exit_code", 0 if passed else 1)),
            "strict": bool(payload.get("strict")),
            "stages": [str(value) for value in payload.get("stages", []) if value],
            "skips": [str(value) for value in payload.get("skips", []) if value],
            "failure_class": current_failure,
            "gate_receipt": "trusted" if trusted_gate else "missing-or-invalid",
            "native_warning_snapshot": (
                "stable" if warning_snapshot_stable else "changed-or-unknown"
            ),
            "repository": fingerprint,
        }
        if resolution_candidate:
            record["resolution_candidate_identity"] = cas_identity
        path = paths.verification_runs / f"{run_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as handle:
            content = (json.dumps(record, indent=2) + "\n").encode("utf-8")
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        pointer = dict(record)
        pointer["record"] = path.relative_to(paths.runtime).as_posix()
        pointer["current_failure_class"] = current_failure
        if unresolved:
            pointer["unresolved_failure"] = unresolved
            pointer["failure_class"] = unresolved.get("failure_class")
        previous_resolution = previous.get("resolved_failure")
        if isinstance(previous_resolution, dict):
            pointer["resolved_failure"] = dict(previous_resolution)

        preserve_complete_pointer = bool(
            passed
            and scope in ("static", "targeted", "fast")
            and current_failure is None
            and unresolved is None
            and trusted_gate
            and warning_snapshot_stable
            and start_warning_state == "clean"
            and fingerprint.get("available")
            and previous.get("schema") == SCHEMA
            and previous.get("status") == "fresh"
            and previous.get("scope") in ("full", "strict")
            and previous.get("passed") is True
            and previous.get("gate_receipt") == "trusted"
            and previous.get("native_warning_snapshot") == "stable"
            and previous.get("failure_class") is None
            and not previous.get("unresolved_failure")
            and isinstance(previous.get("repository"), dict)
            and previous["repository"].get("available")
            and previous["repository"].get("digest")
            == fingerprint.get("digest")
        )

        # This conservative pointer must exist before native state is mutated.
        # A passing limited diagnostic is still retained as an immutable run,
        # but it must not obscure unchanged, fresh full/strict completion proof.
        if preserve_complete_pointer:
            return pointer
        _durable_json_write(paths.verification_latest, pointer)
        if not resolution_candidate:
            return pointer

        resolved = bool(
            native_engine is not None
            and native_engine.resolve_native_warning(
                root,
                verification_scope=scope,
                expected_identity=cas_identity,
            )
        )
        if not resolved:
            return pointer

        resolved_failure = {
            **(unresolved or {}),
            "resolved_at": _now_iso(),
            "resolved_by": scope,
            "resolution_run_id": run_id,
            "warning_identity": cas_identity,
        }
        resolution_path = paths.verification_runs / f"{run_id}-resolution.json"
        resolution_event = {
            "schema": SCHEMA,
            "event": "native-warning-resolved",
            "recorded_at": _now_iso(),
            "verification_run_id": run_id,
            "warning_identity": cas_identity,
            "scope": scope,
        }
        with resolution_path.open("xb") as handle:
            content = (json.dumps(resolution_event, indent=2) + "\n").encode(
                "utf-8"
            )
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        pointer["status"] = "fresh"
        pointer["failure_class"] = current_failure
        pointer.pop("unresolved_failure", None)
        pointer["resolved_failure"] = resolved_failure
        pointer["resolution_record"] = resolution_path.relative_to(
            paths.runtime
        ).as_posix()
        _durable_json_write(paths.verification_latest, pointer)
        return pointer


def persisted_native_warning(root: Path) -> dict[str, Any] | None:
    """Read an unresolved native-crash warning without launching another process.

    ``verification_view`` also determines whether the repository changed after
    the recorded run, which appropriately asks Git for a fingerprint.  Doctor's
    stricter offline/read-only probe contract only needs the persisted safety
    boundary: an unresolved native application crash cannot be cleared by a
    static check or by repository changes.
    """
    snapshot = (
        native_engine.snapshot_native_warning(root)
        if native_engine is not None
        and hasattr(native_engine, "snapshot_native_warning")
        else None
    )
    if snapshot is not None and snapshot.state == "unknown":
        return {
            "code": "native-warning-state-unknown",
            "recorded_at": None,
            "operation": "native safety inspection",
            "summary": (
                "The native-engine warning record could not be authenticated. "
                "The kit will not clear or bypass it until the private warning "
                "state is readable and one bounded full verification succeeds."
            ),
        }
    direct = (
        native_engine.read_native_warning(root)
        if native_engine is not None else None
    )
    if direct:
        failure_class = str(direct.get("failure_class") or "native-engine-failure")
        return {
            "code": failure_class,
            "recorded_at": direct.get("recorded_at"),
            "operation": direct.get("operation"),
            "warning_sha256": snapshot.identity if snapshot is not None else None,
            "summary": (
                f"A Godot {failure_class.replace('-', ' ')} remains unresolved from "
                f"{direct.get('operation') or 'a kit operation'}. Static checks cannot "
                "clear it; one bounded full verification must prove recovery before "
                "another native launch is treated as safe."
            ),
        }
    try:
        paths = runtime_paths.resolve(root)
        record = json.loads(paths.verification_latest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, runtime_paths.RuntimeConfigError):
        return None
    if not isinstance(record, dict):
        return None
    unresolved = (
        record.get("unresolved_failure")
        if isinstance(record.get("unresolved_failure"), dict)
        else None
    )
    supported = {
        "native-crash",
        "engine-timeout",
        "engine-start-failed",
        "concurrent-engine-launch",
        "native-warning-state-unknown",
    }
    if unresolved is None and (
        record.get("failure_class") in supported and not record.get("passed")
    ):
        unresolved = {
            "failure_class": record.get("failure_class"),
            "recorded_at": record.get("recorded_at"),
            "run_id": record.get("id"),
        }
    if not unresolved or unresolved.get("failure_class") not in supported:
        return None
    failure_class = str(unresolved.get("failure_class"))
    summaries = {
        "native-crash": "A Godot native application crash remains unresolved.",
        "engine-timeout": "A bounded Godot engine run timed out and remains unresolved.",
        "engine-start-failed": "Godot could not be started safely and the failure remains unresolved.",
        "concurrent-engine-launch": "A concurrent Godot launch was refused and remains unresolved.",
        "native-warning-state-unknown": "The native-engine warning state could not be authenticated.",
    }
    return {
        "code": failure_class,
        "recorded_at": unresolved.get("recorded_at"),
        "run_id": unresolved.get("run_id"),
        "summary": (
            summaries[failure_class]
            + " Static checks cannot clear it; one bounded full verification "
            "must prove recovery before another native launch is treated as safe."
        ),
    }


def verification_view(root: Path) -> dict[str, Any]:
    """Return fresh/stale/failed/insufficient/not-run for the current repo."""
    native_warning = persisted_native_warning(root)
    try:
        paths = runtime_paths.resolve(root)
    except runtime_paths.RuntimeConfigError:
        return {
            "status": "not-run",
            "summary": "No verification ledger is configured for this fixture.",
            "recorded_at": None,
        }
    if not paths.verification_latest.is_file():
        if native_warning:
            return {
                "status": "failed",
                "summary": native_warning["summary"],
                "recorded_at": native_warning.get("recorded_at"),
                "failure_class": native_warning.get("code"),
                "unresolved_failure": native_warning,
            }
        return {
            "status": "not-run",
            "summary": "No verification has been recorded for this repository.",
            "recorded_at": None,
        }
    try:
        record = json.loads(paths.verification_latest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {
            "status": "insufficient",
            "summary": "The latest verification pointer is unreadable.",
            "recorded_at": None,
        }
    if not isinstance(record, dict):
        return {
            "status": "insufficient",
            "summary": "The latest verification pointer is malformed.",
            "recorded_at": None,
        }
    if native_warning:
        return {
            "status": "failed",
            "summary": native_warning["summary"],
            "recorded_at": native_warning.get("recorded_at"),
            "failure_class": native_warning.get("code"),
            "unresolved_failure": native_warning,
            "latest_check_status": str(record.get("status") or "insufficient"),
        }
    recorded_status = str(record.get("status") or "insufficient")
    unresolved = (
        record.get("unresolved_failure")
        if isinstance(record.get("unresolved_failure"), dict)
        else None
    )
    resolved = (
        record.get("resolved_failure")
        if isinstance(record.get("resolved_failure"), dict)
        else None
    )
    current = repository_fingerprint(root)
    recorded_repo = record.get("repository") if isinstance(record.get("repository"), dict) else {}
    changed = bool(
        current.get("available")
        and recorded_repo.get("available")
        and current.get("digest") != recorded_repo.get("digest")
    )
    if unresolved:
        status = "failed"
        failure_class = unresolved.get("failure_class")
        if unresolved.get("summary"):
            summary = str(unresolved["summary"])
        elif failure_class == "native-crash":
            summary = (
                "A Godot native application crash remains unresolved. Static or targeted "
                "checks cannot clear it; close any crash dialog and complete kit verify "
                "or kit verify --strict to prove recovery."
            )
        elif failure_class == "engine-timeout":
            summary = (
                "A Godot engine timeout remains unresolved until a complete verification succeeds."
            )
        elif failure_class == "engine-start-failed":
            summary = (
                "Godot could not be started safely; a complete bounded verification "
                "must succeed before the native boundary is treated as recovered."
            )
        elif failure_class == "native-warning-state-unknown":
            summary = (
                "The native-engine warning state could not be authenticated and "
                "cannot be cleared or bypassed."
            )
        else:
            summary = (
                "A concurrent Godot launch refusal remains unresolved until a complete verification succeeds."
            )
    elif recorded_status == "failed" or not record.get("passed"):
        status = "failed"
        failure_class = record.get("failure_class")
        if failure_class == "native-crash":
            summary = (
                "Godot ended with a native application crash. Close any crash dialog, "
                "confirm no other Godot verification is running, then run kit verify again."
            )
        elif failure_class == "engine-timeout":
            summary = (
                "Godot exceeded the bounded verification timeout. Confirm the engine is not "
                "waiting on a dialog or another process, then run kit verify again."
            )
        elif failure_class == "concurrent-engine-launch":
            summary = (
                "Verification refused a concurrent Godot launch. Let the existing engine "
                "verification finish, then run kit verify again."
            )
        else:
            summary = "The latest verification failed."
    elif recorded_status == "insufficient" or not current.get("available"):
        status = "insufficient"
        summary = "The latest check did not cover the complete runnable gate."
    elif changed:
        status = "stale"
        summary = "Repository content changed after the latest passing verification."
    else:
        status = "fresh"
        if resolved and resolved.get("failure_class") == "native-crash":
            summary = (
                "The previous Godot native application crash was resolved by a successful "
                f"{resolved.get('resolved_by') or 'complete'} verification matching current content."
            )
        else:
            summary = "The latest complete verification matches current repository content."
    return {
        "status": status,
        "summary": summary,
        "recorded_at": record.get("recorded_at"),
        "scope": record.get("scope"),
        "strict": bool(record.get("strict")),
        "skips": record.get("skips") if isinstance(record.get("skips"), list) else [],
        "record": record.get("record"),
        "repository_changed": changed,
        "failure_class": (
            unresolved.get("failure_class") if unresolved else record.get("failure_class")
        ),
        "unresolved_failure": unresolved,
        "resolved_failure": resolved,
        "latest_check_status": recorded_status,
    }


def _exact_design_path(root: Path, entry: dict[str, Any]) -> Path:
    """Resolve the same narrow design-reference surface enforced by the gate."""
    try:
        _section, candidate = proposal_authority.canonical_design_reference(
            root, entry.get("section")
        )
    except proposal_authority.AuthorityError as exc:
        raise CockpitError(str(exc), code="design_reference_invalid") from exc
    return candidate


def referenced_design_paths(root: Path, proposal: dict[str, Any]) -> list[Path]:
    found: list[Path] = []
    raw_refs = proposal.get("design_refs")
    for entry in raw_refs if isinstance(raw_refs, list) else []:
        if not isinstance(entry, dict):
            continue
        try:
            candidate = _exact_design_path(root, entry)
        except CockpitError:
            continue
        if candidate not in found:
            found.append(candidate)
    return found


def _first_value(mapping: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        value = mapping.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def _reversibility(proposal: dict[str, Any]) -> dict[str, Any]:
    raw = _first_value(
        proposal,
        ("reversibility", "reversible_envelope", "reversible", "veto_envelope"),
    )
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bool):
        return {"safe_to_continue": raw}
    if raw is not None:
        return {"summary": str(raw)}
    return {}


def _go_no_go(proposal: dict[str, Any]) -> dict[str, Any]:
    raw = _first_value(proposal, ("go_no_go", "go_no_go_point", "approval_gate"))
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, bool):
        return {"approval_required": raw}
    if raw is not None:
        return {"summary": str(raw)}
    return {}


def _design_authority(proposal: dict[str, Any]) -> dict[str, Any]:
    raw = _first_value(
        proposal,
        ("design_authority", "design_provenance", "inferred_design", "design_basis"),
    )
    if isinstance(raw, dict):
        return raw
    if raw is not None:
        return {"summary": str(raw)}
    return {
        "status": "not-recorded",
        "summary": "This proposal predates explicit design-authority metadata.",
    }


def _aggregate_authority(metadata: list[dict[str, str]]) -> dict[str, str]:
    """Summarize multiple design headers without overstating their authority."""
    provisional = [
        item for item in metadata if item.get("authority") == "agent-provisional"
    ]
    confidence_source = provisional or metadata
    confidence = min(
        (item["confidence"] for item in confidence_source),
        key=lambda value: CONFIDENCE_ORDER[value],
    )
    return {
        "authority": "agent-provisional" if provisional else "human-confirmed",
        "authored_by": (
            "agent" if any(item.get("authored_by") == "agent" for item in metadata)
            else "human"
        ),
        "confidence": confidence,
    }


def _disclosure_brief(value: object, *, limit: int = 360) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _design_authority_view(root: Path, proposal: dict[str, Any]) -> dict[str, Any]:
    """Prefer current referenced headers to a potentially stale proposal summary."""
    refs = proposal.get("design_refs")
    if not isinstance(refs, list) or not refs or design_contract is None:
        return _design_authority(proposal)
    metadata: list[dict[str, str]] = []
    disclosures: list[dict[str, str]] = []
    try:
        resolved = proposal_authority.validated_design_refs(root, proposal)
        for reference in resolved:
            parsed = reference.get("metadata")
            if not isinstance(parsed, dict):
                raise CockpitError("invalid design metadata")
            authority = str(parsed.get("authority") or "")
            authored_by = str(parsed.get("authored_by") or "")
            confidence = str(parsed.get("confidence") or "")
            if (
                authority not in ("agent-provisional", "human-confirmed")
                or authored_by not in ("agent", "human")
                or confidence not in CONFIDENCE_ORDER
            ):
                raise CockpitError("invalid design metadata")
            metadata.append(
                {
                    "authority": authority,
                    "authored_by": authored_by,
                    "confidence": confidence,
                }
            )
            disclosures.append(
                {
                    "section": str(reference.get("section") or ""),
                    # These two fields are the exact canonical section bodies,
                    # not a summary invented by the cockpit.  A reviewer must
                    # be able to see the authored outcome and veto boundary
                    # before deciding, even when the full record is collapsed.
                    "quick_read": str(parsed.get("quick_read") or "").strip(),
                    "why_inference": _disclosure_brief(parsed.get("why_inference")),
                    "assumptions": _disclosure_brief(parsed.get("assumptions")),
                    "veto_and_go_no_go": str(
                        parsed.get("veto_and_go_no_go") or ""
                    ).strip(),
                }
            )
    except (
        CockpitError,
        proposal_authority.AuthorityError,
        OSError,
        UnicodeError,
        ValueError,
    ) as exc:
        stored = dict(_design_authority(proposal))
        stored["validation"] = "invalid"
        stored["implementation_eligible"] = False
        stored["summary"] = (
            "Referenced design authority is malformed, stale, or no longer resolves "
            f"exactly: {exc}"
        )
        return stored
    aggregate: dict[str, Any] = _aggregate_authority(metadata)
    aggregate["implementation_eligible"] = True
    aggregate["validation"] = "current"
    aggregate["disclosures"] = disclosures
    if _has_current_design_veto(root, proposal):
        aggregate["authority"] = "vetoed"
        aggregate["implementation_eligible"] = False
        aggregate["validation"] = "vetoed"
        aggregate["summary"] = (
            "Human design authority was vetoed for at least one referenced section; "
            "a new exact design-and-plan approval is required."
        )
    return aggregate


def _has_current_design_veto(root: Path, proposal: dict[str, Any]) -> bool:
    """Project explicit design vetoes without confusing request-changes with veto."""
    try:
        shape = _read_object(root / SHAPE_NAME)
        intents = proposal_authority.proposal_design_intents(root, proposal)
    except (CockpitError, proposal_authority.AuthorityError):
        return False
    return bool(proposal_authority.active_design_vetoes(shape, intents))


def proposal_fingerprint(
    root: Path,
    proposal: dict[str, Any] | None = None,
    *,
    project_shape: dict[str, Any] | None = None,
    design_contents: dict[Path, bytes] | None = None,
) -> dict[str, Any]:
    """Digest the exact decision material: proposal, design, baseline, envelope."""
    canonical = root.resolve(strict=True)
    value = _read_object(canonical / PROPOSAL_NAME) if proposal is None else proposal
    shape = _read_object(canonical / SHAPE_NAME) if project_shape is None else project_shape
    content_overrides = design_contents or {}
    documents: list[dict[str, Any]] = []
    for path in referenced_design_paths(canonical, value):
        relative = path.relative_to(canonical).as_posix()
        try:
            content = content_overrides[path] if path in content_overrides else path.read_bytes()
            document = {"path": relative, "sha256": _design_digest(content)}
        except (OSError, KeyError):
            document = {"path": relative, "sha256": None, "missing": True}
        documents.append(document)
    material = {
        "proposal": value,
        "project_shape": shape,
        "design_documents": documents,
        "baseline_sha": str(value.get("baseline_sha") or ""),
        "reversibility": _reversibility(value),
        "go_no_go": _go_no_go(value),
    }
    return {
        "digest": hashlib.sha256(_canonical(material)).hexdigest(),
        "design_documents": documents,
        "baseline_sha": material["baseline_sha"],
        "reversibility": material["reversibility"],
        "go_no_go": material["go_no_go"],
    }


def plan_view(root: Path) -> dict[str, Any]:
    canonical = root.resolve(strict=True)
    _recover_decision_transaction(canonical)
    proposal = _read_object(canonical / PROPOSAL_NAME)
    shape = _read_object(canonical / SHAPE_NAME)
    fingerprint = proposal_fingerprint(canonical, proposal)
    experience = proposal.get("experience") if isinstance(proposal.get("experience"), dict) else {}
    authority = _design_authority_view(canonical, proposal)
    reversibility = fingerprint["reversibility"]
    go_no_go = fingerprint["go_no_go"]
    proposal_status = str(proposal.get("status") or "")
    exact_approval = proposal_authority.exact_approval_state(
        canonical, proposal, shape
    )
    receipt_trust = str(exact_approval.get("receipt_trust") or "")
    receipt_copy = {
        "local-audit-matched": (
            "The matching private local audit receipt is present. This remains "
            "policy evidence, not cryptographic person authentication."
        ),
        "portable-policy": (
            "The private local receipt is absent, as can happen after clone or CI; "
            "the portable event is self-consistent policy evidence only."
        ),
        "invalid": (
            "A present private receipt contradicts the portable authority event; "
            "approval fails closed."
        ),
        "no-exact-authority-event": (
            "No exact authority event is active for this proposal."
        ),
    }.get(receipt_trust, "Receipt provenance is unavailable.")
    recorded_blocker = ""
    if proposal_status == "recorded":
        try:
            proposal_authority.validate_approval_contract(
                canonical, proposal, shape, allowed_statuses=("recorded",)
            )
        except proposal_authority.AuthorityError as exc:
            recorded_blocker = str(exc)
    effective_status = (
        "approved"
        if proposal_status == "approved" and exact_approval["approved"]
        else "approval-stale"
        if proposal_status == "approved"
        else "recorded-stale"
        if proposal_status == "recorded" and recorded_blocker
        else proposal_status
    )
    approval_required = bool(
        effective_status != "approved"
        and (
            proposal_status in ("draft", "approved")
            or bool(recorded_blocker)
            or reversibility.get("state") == "go-no-go"
            or go_no_go.get("approval_required")
            or go_no_go.get("status") in ("go-no-go", "approval-required", "blocked")
        )
    )
    approval_available = False
    approval_blocker = ""
    if approval_required:
        if recorded_blocker:
            approval_blocker = (
                "Recorded autonomous work no longer matches its exact reversible "
                f"contract: {recorded_blocker}"
            )
        elif proposal_status == "approved" and not exact_approval["approved"]:
            approval_blocker = (
                "Stored approval is not exact/current: "
                + "; ".join(exact_approval["reasons"])
                + ". Return the proposal to draft and review it again."
            )
        else:
            approval_available, approval_blocker = _approval_preflight(
                canonical, proposal
            )
    recorded_decision_available = bool(
        effective_status == "recorded"
        and not approval_required
        and str(reversibility.get("state") or "") == "reversible"
    )
    return {
        "fingerprint": fingerprint["digest"],
        "slice": str(proposal.get("slice") or ""),
        "status": effective_status or "not-proposed",
        "stored_status": proposal_status or "not-proposed",
        "exact_approval": exact_approval,
        "quick_read": {
            "outcome": str(experience.get("player_does") or proposal.get("slice") or ""),
            "feel": str(experience.get("feels_like") or ""),
            "not_this": str(experience.get("not_this") or ""),
        },
        "design_authority": authority,
        "reversibility": reversibility or {
            "status": "not-recorded",
            "summary": "This proposal predates an explicit reversible-build envelope.",
        },
        "go_no_go": go_no_go or {
            "status": "approval-required" if approval_required else "clear",
            "summary": str(reversibility.get("next_go_no_go") or "") or (
                "The draft must be approved before implementation."
                if approval_required
                else "No go/no-go point is currently recorded."
            ),
        },
        "approval_required": approval_required,
        "approval_available": approval_available,
        "approval_blocker": approval_blocker,
        "recorded_decision_available": recorded_decision_available,
        "authority_trust": f"{AUTHORITY_TRUST} {receipt_copy}",
        "authority_receipt_trust": receipt_trust,
        "design_documents": fingerprint["design_documents"],
        "baseline_sha": fingerprint["baseline_sha"],
        "verification": verification_view(root),
    }


def _decision_ledger_path(paths: runtime_paths.RuntimePaths) -> Path:
    identifier = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex}"
    return paths.plan_decisions / f"{identifier}.json"


def _require_design_contract() -> None:
    """Refuse approval if the complete authority contract is unavailable."""
    if (
        design_contract is None
        or not hasattr(design_contract, "design_sha256")
        or not hasattr(design_contract, "parse_design")
    ):
        raise CockpitError(
            "This kit cannot validate canonical design authority; update or repair the kit before approval.",
            code="contract_unavailable",
        )
    try:
        import schema
    except (ImportError, AttributeError) as exc:
        raise CockpitError(
            "This kit cannot validate the proposal approval contract; update or repair the kit before approval.",
            code="contract_unavailable",
        ) from exc
    proposal_spec = getattr(schema, "PROPOSAL", {})
    ref_spec = getattr(schema, "DESIGN_REF", {})
    decision_spec = getattr(schema, "DECISION", {})
    if not (
        {"design_authority", "approval_sha256", "reversibility"} <= set(proposal_spec)
        and "sha256" in ref_spec
        and {
            "design_sha256",
            "design_intent_sha256",
            "proposal_sha256",
            "authority_action",
            "cockpit_receipt_id",
            "cockpit_reviewed_fingerprint",
            "cockpit_receipt_sha256",
        }
        <= set(decision_spec)
    ):
        raise CockpitError(
            "This kit's schema predates atomic design-and-plan approval; approval is read-only until migrated.",
            code="contract_unavailable",
        )


def _approval_preflight(root: Path, proposal: dict[str, Any]) -> tuple[bool, str]:
    """Tell the renderer whether approval can be atomic before offering the action."""
    try:
        _require_design_contract()
        if not (root / SHAPE_NAME).is_file():
            raise CockpitError(
                "Approval requires project.shape.json for its durable human decision.",
                code="shape_required",
            )
        shape = _read_object(root / SHAPE_NAME)
        proposal_authority.validate_approval_contract(
            root, proposal, shape, allow_human_reconfirmation=True
        )
        _approval_design_changes(root, proposal)
    except (CockpitError, proposal_authority.AuthorityError) as exc:
        return False, str(exc)
    return True, ""


def _approval_design_changes(
    root: Path, proposal: dict[str, Any]
) -> tuple[dict[Path, bytes], list[dict[str, Any]], dict[str, str]]:
    """Prepare coherent design confirmation without changing the filesystem."""
    refs = proposal.get("design_refs")
    if not isinstance(refs, list) or not refs:
        raise CockpitError(
            "Approval requires at least one referenced design section.",
            code="design_required",
        )
    prepared: dict[Path, bytes] = {}
    metadata: list[dict[str, str]] = []
    rebound: list[dict[str, Any]] = []
    seen: set[Path] = set()
    for entry in refs:
        if not isinstance(entry, dict):
            raise CockpitError("Every design reference must be an object.", code="design_reference_invalid")
        path = _exact_design_path(root, entry)
        if path in seen:
            raise CockpitError(
                f"{path.relative_to(root).as_posix()} is referenced more than once.",
                code="design_reference_invalid",
            )
        seen.add(path)
        try:
            original = path.read_bytes()
            text = original.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise CockpitError(
                f"Cannot confirm {path.relative_to(root).as_posix()}: {exc}",
                code="design_reference_invalid",
            ) from exc
        supplied_digest = str(entry.get("sha256") or "")
        current_digest = _design_digest(original)
        if supplied_digest != current_digest:
            raise CockpitError(
                f"{path.relative_to(root).as_posix()} changed after this proposal was written "
                f"(proposal {supplied_digest[:12] or 'missing'}, current {current_digest[:12]}). "
                "Revise and re-record the proposal; approval never rebinds stale design silently.",
                code="stale_design_reference",
            )
        if design_contract is not None and hasattr(design_contract, "parse_design"):
            try:
                parsed = design_contract.parse_design(path)
            except (OSError, UnicodeError, ValueError) as exc:
                raise CockpitError(
                    f"Cannot validate {path.relative_to(root).as_posix()}: {exc}",
                    code="design_metadata_invalid",
                ) from exc
            errors = parsed.get("metadata_errors") if isinstance(parsed, dict) else []
            eligible = bool(parsed.get("implementation_eligible")) if isinstance(parsed, dict) else False
            if errors or not eligible:
                detail = "; ".join(str(item) for item in errors) or "not implementation-eligible"
                raise CockpitError(
                    f"{path.relative_to(root).as_posix()} cannot be approved: {detail}",
                    code="design_metadata_invalid",
                )
        authority = str(parsed.get("authority") or "")
        authored_by = str(parsed.get("authored_by") or "")
        confidence = str(parsed.get("confidence") or "")
        if authority == "agent-provisional" and authored_by != "agent":
            raise CockpitError(
                f"{path.relative_to(root).as_posix()} is provisional but does not disclose agent authorship.",
                code="design_metadata_invalid",
            )
        if authority == "agent-provisional":
            try:
                updated_text = design_contract.replace_active_metadata(
                    text, "Authority", "agent-provisional", "human-confirmed"
                )
            except (AttributeError, ValueError) as exc:
                raise CockpitError(
                    f"{path.relative_to(root).as_posix()} has malformed authority headers.",
                    code="design_metadata_invalid",
                ) from exc
            updated = updated_text.encode("utf-8")
            prepared[path] = updated
        else:
            updated = original
        metadata.append(
            {
                "authority": "human-confirmed",
                "authored_by": authored_by,
                "confidence": confidence,
            }
        )
        rebound_entry = dict(entry)
        rebound_entry["sha256"] = _design_digest(updated)
        rebound.append(rebound_entry)
    return prepared, rebound, _aggregate_authority(metadata)


def _veto_design_changes(
    root: Path, proposal: dict[str, Any]
) -> tuple[dict[Path, bytes], list[dict[str, Any]], dict[str, str]]:
    """Prepare an honest revocation of agent-authored confirmed design authority."""
    refs = proposal.get("design_refs")
    if not isinstance(refs, list) or not refs:
        raise CockpitError(
            "Design veto requires the exact referenced design sections.",
            code="design_required",
        )
    prepared: dict[Path, bytes] = {}
    rebound: list[dict[str, Any]] = []
    metadata: list[dict[str, str]] = []
    seen: set[Path] = set()
    for entry in refs:
        if not isinstance(entry, dict):
            raise CockpitError(
                "Every design reference must be an object.",
                code="design_reference_invalid",
            )
        path = _exact_design_path(root, entry)
        if path in seen:
            raise CockpitError(
                f"{path.relative_to(root).as_posix()} is referenced more than once.",
                code="design_reference_invalid",
            )
        seen.add(path)
        try:
            original = path.read_bytes()
            text = original.decode("utf-8")
            parsed = design_contract.parse_design(path)
        except (OSError, UnicodeError, ValueError) as exc:
            raise CockpitError(
                f"Cannot validate {path.relative_to(root).as_posix()}: {exc}",
                code="design_metadata_invalid",
            ) from exc
        supplied_digest = str(entry.get("sha256") or "")
        current_digest = _design_digest(original)
        if supplied_digest != current_digest:
            raise CockpitError(
                f"{path.relative_to(root).as_posix()} changed after the reviewed plan loaded; "
                "refresh before recording a veto.",
                code="stale_design_reference",
            )
        errors = parsed.get("metadata_errors") if isinstance(parsed, dict) else []
        if errors or not isinstance(parsed, dict):
            detail = "; ".join(str(item) for item in errors) or "metadata is malformed"
            raise CockpitError(
                f"{path.relative_to(root).as_posix()} cannot be vetoed safely: {detail}",
                code="design_metadata_invalid",
            )
        authority = str(parsed.get("authority") or "")
        authored_by = str(parsed.get("authored_by") or "")
        confidence = str(parsed.get("confidence") or "")
        if (
            authority not in ("agent-provisional", "human-confirmed")
            or authored_by not in ("agent", "human")
            or confidence not in CONFIDENCE_ORDER
        ):
            raise CockpitError(
                f"{path.relative_to(root).as_posix()} has incomplete authority metadata.",
                code="design_metadata_invalid",
            )
        updated = original
        final_authority = authority
        if authority == "human-confirmed" and authored_by == "agent":
            try:
                updated_text = design_contract.replace_active_metadata(
                    text, "Authority", "human-confirmed", "agent-provisional"
                )
            except (AttributeError, ValueError) as exc:
                raise CockpitError(
                    f"{path.relative_to(root).as_posix()} has malformed authority headers.",
                    code="design_metadata_invalid",
                ) from exc
            updated = updated_text.encode("utf-8")
            prepared[path] = updated
            final_authority = "agent-provisional"
        metadata.append(
            {
                "authority": final_authority,
                "authored_by": authored_by,
                "confidence": confidence,
            }
        )
        rebound_entry = dict(entry)
        rebound_entry["sha256"] = _design_digest(updated)
        rebound.append(rebound_entry)
    return prepared, rebound, _aggregate_authority(metadata)


def _replace_with_rollback(
    replacements: dict[Path, bytes], originals: dict[Path, bytes | None]
) -> None:
    written: list[Path] = []
    created_directories: set[Path] = set()
    for path in replacements:
        parent = path.parent
        while not parent.exists():
            created_directories.add(parent)
            if parent == parent.parent:
                break
            parent = parent.parent
    try:
        for path, content in replacements.items():
            written.append(path)
            _atomic_bytes_write(path, content)
    except Exception as failure:
        rollback_errors: list[str] = []
        for path in reversed(written):
            try:
                original = originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_bytes_write(path, original)
            except OSError as exc:
                rollback_errors.append(f"{path}: {exc}")
        for directory in sorted(
            created_directories, key=lambda value: len(value.parts), reverse=True
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        if rollback_errors:
            raise CockpitError(
                "Approval failed and rollback was incomplete: " + "; ".join(rollback_errors),
                code="rollback_failed",
            ) from failure
        raise CockpitError(
            "The decision could not be recorded; every partial write was rolled back.",
            code="decision_write_failed",
        ) from failure


def _append_design_decisions(
    root: Path,
    shape: dict[str, Any],
    proposal: dict[str, Any],
    *,
    receipt_id: str,
    reviewed_fingerprint: str,
) -> None:
    decisions = shape.get("decisions")
    if not isinstance(decisions, list):
        decisions = []
        shape["decisions"] = decisions
    superseded = {
        str(item.get("supersedes") or "")
        for item in decisions
        if isinstance(item, dict)
        and item.get("decided_by") == "human"
        and item.get("authority_action") == "veto"
    }
    existing = {
        (
            str(item.get("docs_at") or ""),
            str(item.get("design_sha256") or ""),
            str(item.get("proposal_sha256") or ""),
        )
        for item in decisions
        if isinstance(item, dict)
        and item.get("decided_by") == "human"
        and item.get("authority_action") == "confirm"
        and str(item.get("id") or "") not in superseded
    }
    identifiers = {
        str(item.get("id") or "") for item in decisions if isinstance(item, dict)
    }
    next_gate = str(
        (_reversibility(proposal).get("next_go_no_go") or "")
    ).strip()
    slice_name = str(proposal.get("slice") or "this proposal").strip()
    proposal_digest = str(proposal.get("approval_sha256") or "")
    reviewed_digest = proposal_authority.reviewed_contract_sha256(root, proposal)
    design_intents = proposal_authority.proposal_design_intents(root, proposal)
    for ref in proposal.get("design_refs", []):
        if not isinstance(ref, dict):
            continue
        path = _exact_design_path(root, ref)
        docs_at = path.relative_to(root).as_posix()
        digest = str(ref.get("sha256") or "")
        if (docs_at, digest, proposal_digest) in existing:
            continue
        because = str(ref.get("why") or "").strip()
        if not because:
            raise CockpitError(
                f"{docs_at} has no recorded reason for this slice.",
                code="design_reference_invalid",
            )
        slug = re.sub(r"[^a-z0-9]+", "-", docs_at.lower()).strip("-")[-48:]
        identifier = f"design-confirmed-{slug}-{digest[:12]}-{proposal_digest[:12]}"
        ordinal = 2
        while identifier in identifiers:
            identifier = (
                f"design-confirmed-{slug}-{digest[:12]}-{proposal_digest[:12]}-{ordinal}"
            )
            ordinal += 1
        intent_digest = design_intents.get(docs_at, "")
        active_vetoes = proposal_authority.active_design_vetoes(
            shape, {docs_at: intent_digest}
        )
        event = {
                "id": identifier,
                "question": f"Confirm the player-experience design in {docs_at}?",
                "answer": f"Confirmed as the design authority for {slice_name}.",
                "because": because,
                "date": date.today().isoformat(),
                "decided_by": "human",
                "revisit_if": next_gate or "The design content or intended player outcome changes.",
                "docs_at": docs_at,
                "design_sha256": digest,
                "design_intent_sha256": intent_digest,
                "proposal_sha256": proposal_digest,
                "authority_action": "confirm",
                "reviewed_contract_sha256": reviewed_digest,
                "authority_scope": "design-and-plan",
                "cockpit_receipt_id": receipt_id,
                "cockpit_reviewed_fingerprint": reviewed_fingerprint,
            }
        if active_vetoes:
            event["supersedes"] = str(active_vetoes[-1].get("id") or "")
        event["cockpit_receipt_sha256"] = (
            proposal_authority.decision_receipt_sha256(event)
        )
        decisions.append(event)
        identifiers.add(identifier)
        existing.add((docs_at, digest, proposal_digest))


def _append_design_vetoes(
    root: Path,
    shape: dict[str, Any],
    proposal: dict[str, Any],
    *,
    action: str,
    reason: str,
    receipt_id: str,
    reviewed_fingerprint: str,
) -> bool:
    """Record rejection of exact intent, including a draft with no prior approval."""
    proposal_digest = str(proposal.get("approval_sha256") or "").strip()
    if not proposal_digest:
        proposal_digest = approval_sha256(proposal)
    reviewed_digest = proposal_authority.reviewed_contract_sha256(root, proposal)
    design_intents = proposal_authority.proposal_design_intents(root, proposal)
    authority_scope = "design-and-plan" if action == "veto" else "plan-only"
    decisions = shape.get("decisions")
    if not isinstance(decisions, list):
        decisions = []
        shape["decisions"] = decisions
    references: set[tuple[str, str, str]] = set()
    for ref in proposal.get("design_refs", []):
        if not isinstance(ref, dict):
            continue
        section = str(ref.get("section") or "").strip().replace("\\", "/")
        if section:
            references.add(
                (
                    section,
                    str(ref.get("sha256") or ""),
                    str(design_intents.get(section) or ""),
                )
            )
    active = [
        item
        for item in proposal_authority.active_decisions(shape)
        if item.get("decided_by") == "human"
        and item.get("authority_action") == "confirm"
        and str(item.get("proposal_sha256") or "") == proposal_digest
        and (
            str(item.get("docs_at") or ""),
            str(item.get("design_sha256") or ""),
        ) in {(section, digest) for section, digest, _intent in references}
    ]
    existing_rejection = proposal_authority.matching_rejection(shape, reviewed_digest)
    active_veto_keys = {
        (
            str(item.get("docs_at") or ""),
            str(item.get("design_intent_sha256") or ""),
        )
        for item in proposal_authority.active_design_vetoes(shape, design_intents)
    }
    if action == "request-changes" and (
        existing_rejection is not None
        and existing_rejection.get("authority_scope") == "plan-only"
    ):
        return False
    identifiers = {
        str(item.get("id") or "") for item in decisions if isinstance(item, dict)
    }
    confirmations = {
        (
            str(item.get("docs_at") or ""),
            str(item.get("design_sha256") or ""),
        ): item
        for item in active
    }
    appended = False
    for docs_at, design_digest, intent_digest in sorted(references):
        if action == "veto" and (docs_at, intent_digest) in active_veto_keys:
            continue
        confirmation = confirmations.get((docs_at, design_digest), {})
        confirmation_id = str(confirmation.get("id") or "")
        identity = hashlib.sha256(
            _canonical(
                {
                    "action": action,
                    "reviewed_contract": reviewed_digest,
                    "section": docs_at,
                    "reason": reason,
                    "date": date.today().isoformat(),
                }
            )
        ).hexdigest()[:12]
        identifier = f"design-vetoed-{identity}"
        ordinal = 2
        while identifier in identifiers:
            identifier = f"design-vetoed-{identity}-{ordinal}"
            ordinal += 1
        event = {
                "id": identifier,
                "question": (
                    "May work continue under the reviewed design and plan for "
                    f"{docs_at}?"
                ),
                "answer": (
                    "The referenced design intent and reviewed plan were rejected."
                    if action == "veto"
                    else (
                        "The reviewed plan was withdrawn; the referenced "
                        "design-authority state is unchanged."
                    )
                ),
                "because": reason,
                "date": date.today().isoformat(),
                "decided_by": "human",
                "revisit_if": (
                    "The referenced design intent changes or a later exact cockpit "
                    "confirmation supersedes this veto."
                    if action == "veto"
                    else (
                        "A substantively revised plan is recorded or explicitly "
                        "approved."
                    )
                ),
                "docs_at": docs_at,
                "design_sha256": design_digest,
                "design_intent_sha256": intent_digest,
                "proposal_sha256": proposal_digest,
                "authority_action": "veto",
                "reviewed_contract_sha256": reviewed_digest,
                "authority_scope": authority_scope,
                "cockpit_receipt_id": receipt_id,
                "cockpit_reviewed_fingerprint": reviewed_fingerprint,
            }
        if confirmation_id:
            event["supersedes"] = confirmation_id
        event["cockpit_receipt_sha256"] = (
            proposal_authority.decision_receipt_sha256(event)
        )
        decisions.append(event)
        identifiers.add(identifier)
        appended = True
    return appended


def record_plan_decision(
    root: Path,
    *,
    action: str,
    fingerprint: str,
    comment: str = "",
) -> dict[str, Any]:
    """Apply one exact human decision without dispatching any provider work."""
    normal_action = action.strip().lower().replace("_", "-")
    if normal_action not in ("approve", "veto", "request-changes"):
        raise CockpitError("decision must be approve, veto, or request-changes", code="invalid_action")
    reason = comment.strip()
    if normal_action != "approve" and not reason:
        raise CockpitError("veto and request-changes require a reason", code="reason_required")
    canonical = root.resolve(strict=True)
    proposal_path = canonical / PROPOSAL_NAME
    # Refuse an invalid approval before creating even private transaction
    # state. The same checks run again under the cross-process lock below so
    # this early, read-only pass does not weaken the compare-and-swap boundary.
    if normal_action == "approve":
        preliminary_proposal = _read_object(proposal_path)
        if str(preliminary_proposal.get("status") or "") == "approved":
            raise CockpitError(
                "This exact plan is already approved. Refresh before taking another decision.",
                code="already_approved",
            )
        preliminary_shape_path = canonical / SHAPE_NAME
        if not preliminary_shape_path.is_file():
            raise CockpitError(
                "Approval requires project.shape.json for the durable human decision.",
                code="shape_required",
            )
        preliminary_shape = _read_object(preliminary_shape_path)
        _require_design_contract()
        _approval_design_changes(canonical, preliminary_proposal)
        try:
            proposal_authority.validate_approval_contract(
                canonical,
                preliminary_proposal,
                preliminary_shape,
                allow_human_reconfirmation=True,
            )
        except proposal_authority.AuthorityError as exc:
            raise CockpitError(
                f"This proposal is not approvable: {exc}",
                code="proposal_contract_invalid",
            ) from exc
    paths = runtime_paths.resolve(canonical, create=True)
    with _decision_lock, _decision_process_guard(paths):
        _recover_decision_transaction_locked(canonical, paths)
        proposal = _read_object(proposal_path)
        proposal_original = proposal_path.read_bytes()
        shape_path = canonical / SHAPE_NAME
        shape = _read_object(shape_path)
        shape_original = shape_path.read_bytes() if shape_path.is_file() else b""
        before = proposal_fingerprint(canonical, proposal)
        if not fingerprint or not secrets_compare(fingerprint, before["digest"]):
            raise CockpitError(
                "The plan or referenced design changed after this page loaded. Refresh and review again.",
                code="stale_plan",
            )
        ledger_path = _decision_ledger_path(paths)
        receipt_id = ledger_path.stem
        recorded_at = _now_iso()
        today = date.today().isoformat()
        design_replacements: dict[Path, bytes] = {}
        shape_changed = False
        if normal_action == "approve":
            if str(proposal.get("status") or "") == "approved":
                raise CockpitError(
                    "This exact plan is already approved. Refresh before taking another decision.",
                    code="already_approved",
                )
            _require_design_contract()
            if not shape_path.is_file():
                raise CockpitError(
                    "Approval requires project.shape.json for the durable human decision.",
                    code="shape_required",
                )
            design_replacements, rebound, authority = _approval_design_changes(
                canonical, proposal
            )
            try:
                proposal_authority.validate_approval_contract(
                    canonical,
                    proposal,
                    shape,
                    allow_human_reconfirmation=True,
                )
            except proposal_authority.AuthorityError as exc:
                raise CockpitError(
                    f"This proposal is not approvable: {exc}",
                    code="proposal_contract_invalid",
                ) from exc
            proposal["design_refs"] = rebound
            proposal["design_authority"] = authority
            proposal["status"] = "approved"
            proposal["approved_by"] = ACTOR_LABEL
            proposal["approved_on"] = today
            proposal["approval_sha256"] = approval_sha256(proposal)
            _append_design_decisions(
                canonical,
                shape,
                proposal,
                receipt_id=receipt_id,
                reviewed_fingerprint=before["digest"],
            )
            shape_changed = True
        else:
            shape_changed = _append_design_vetoes(
                canonical,
                shape,
                proposal,
                action=normal_action,
                reason=reason,
                receipt_id=receipt_id,
                reviewed_fingerprint=before["digest"],
            )
            if normal_action == "veto":
                _require_design_contract()
                design_replacements, rebound, authority = _veto_design_changes(
                    canonical, proposal
                )
                proposal["design_refs"] = rebound
                proposal["design_authority"] = authority
            proposal["status"] = "draft"
            proposal.pop("approved_by", None)
            proposal.pop("approved_on", None)
            proposal.pop("approval_sha256", None)
            revisions = proposal.get("revisions")
            if not isinstance(revisions, list):
                revisions = []
                proposal["revisions"] = revisions
            revisions.append(
                {
                    "on": today,
                    "what": (
                        "Human vetoed the current proposal."
                        if normal_action == "veto"
                        else "Human requested changes to the current proposal."
                    ),
                    "why": reason,
                    "decided_by": "human",
                }
            )
        proposal_bytes = (json.dumps(proposal, indent=2) + "\n").encode("utf-8")
        after = proposal_fingerprint(
            canonical,
            proposal,
            project_shape=shape,
            design_contents=design_replacements,
        )
        record = {
            "schema": SCHEMA,
            "recorded_at": recorded_at,
            "action": normal_action,
            "comment": reason,
            "actor": ACTOR_LABEL,
            "reviewed_fingerprint": before["digest"],
            "result_fingerprint": after["digest"],
            "baseline_sha": before["baseline_sha"],
            "design_documents": before["design_documents"],
            "reversibility": before["reversibility"],
            "go_no_go": before["go_no_go"],
            "authority_trust": AUTHORITY_TRUST,
            "authority_receipts": [
                {
                    "decision_id": str(item.get("id") or ""),
                    "sha256": str(item.get("cockpit_receipt_sha256") or ""),
                }
                for item in shape.get("decisions", [])
                if isinstance(item, dict)
                and item.get("cockpit_receipt_id") == receipt_id
            ],
        }
        ledger_bytes = (json.dumps(record, indent=2) + "\n").encode("utf-8")
        replacements: dict[Path, bytes] = {ledger_path: ledger_bytes}
        replacements.update(design_replacements)
        if shape_changed:
            replacements[shape_path] = (json.dumps(shape, indent=2) + "\n").encode("utf-8")
        replacements[proposal_path] = proposal_bytes
        originals: dict[Path, bytes | None] = {ledger_path: None}
        originals.update({path: path.read_bytes() for path in design_replacements})
        if shape_changed:
            originals[shape_path] = shape_original
        originals[proposal_path] = proposal_original
        _transactional_replace(canonical, replacements, originals, paths)
        try:
            record_reference = ledger_path.relative_to(canonical).as_posix()
        except ValueError:
            record_reference = (
                "<runtime>/" + ledger_path.relative_to(paths.runtime).as_posix()
            )
        return {
            "ok": True,
            "decision": normal_action,
            "status": str(proposal.get("status") or "draft"),
            "fingerprint": after["digest"],
            "record": record_reference,
            "dispatched": False,
        }


def secrets_compare(left: str, right: str) -> bool:
    """Constant-time comparison without importing server-only capability code."""
    import hmac

    return hmac.compare_digest(str(left), str(right))


def regenerate_views(root: Path, *, snapshot: str = "") -> dict[str, Any]:
    """Regenerate plan and retro as one pure operation; never start the board."""
    canonical = root.resolve(strict=True)
    core = _core_for_project(canonical)
    try:
        child_environment = process_supervisor.isolated_python_environment(
            CHILD_ENVIRONMENT if canonical == PROJECT_ROOT else {}
        )
        plan_command = process_supervisor.isolated_python_script_command(
            sys.executable, core / "tools" / "plan_html.py", core
        )
        retro_command = process_supervisor.isolated_python_script_command(
            sys.executable, core / "tools" / "retro_html.py", core
        )
    except (OSError, ValueError) as exc:
        return {
            "ok": False,
            "processes": {
                "isolation": {
                    "returncode": None,
                    "stdout": "",
                    "stderr": str(exc),
                }
            },
        }
    if snapshot:
        plan_command.extend(("--slice", snapshot_label(snapshot)))
    jobs = {
        "plan": plan_command,
        "retro": retro_command,
    }
    processes: dict[str, Any] = {}
    ok = True
    for name, command in jobs.items():
        try:
            result = subprocess.run(
                command,
                cwd=str(canonical),
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
                env=child_environment,
            )
            processes[name] = {
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
            ok = ok and result.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            processes[name] = {"returncode": None, "stdout": "", "stderr": str(exc)}
            ok = False
    ok = ok and (canonical / "plan.html").is_file() and (canonical / "retro.html").is_file()
    return {"ok": ok, "processes": processes}


def snapshot_name(root: Path) -> str:
    proposal = _read_object(root.resolve(strict=True) / PROPOSAL_NAME)
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", str(proposal.get("slice") or "plan")).strip("-")
    digest = proposal_fingerprint(root, proposal)["digest"][:12]
    return snapshot_label(f"{(label or 'plan')[:44]}-{digest}")


def snapshot_label(value: str) -> str:
    """Map a user-facing label to the single safe immutable snapshot filename."""
    return (
        "".join(character if character.isalnum() or character in "-_" else "-"
                for character in str(value))[:60]
        or "plan"
    )


def safe_served_path(root: Path, request_path: str) -> Path | None:
    """Resolve the small read-only file surface exposed by the loopback board."""
    clean = request_path.split("?", 1)[0].split("#", 1)[0]
    if "\\" in clean or "%" in clean or "\x00" in clean:
        return None
    pure = PurePosixPath(clean.lstrip("/"))
    if pure.is_absolute() or ".." in pure.parts:
        return None
    if len(pure.parts) == 2 and pure.parts[0] == "plan" and pure.suffix.lower() == ".html":
        base = (root / "plan").resolve(strict=False)
    elif len(pure.parts) >= 3 and pure.parts[:2] == ("docs", "design") and pure.suffix.lower() in ASSET_SUFFIXES:
        base = (root / "docs" / "design").resolve(strict=False)
    else:
        return None
    candidate = (root / Path(*pure.parts)).resolve(strict=False)
    if candidate == base or not candidate.is_relative_to(base) or not candidate.is_file():
        return None
    return candidate
