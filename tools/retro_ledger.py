#!/usr/bin/env python3
"""Fail-closed, atomic persistence for tracked retrospective decisions.

Accepted and deferred decisions are durable human records. Treating malformed
JSON as an empty list makes the next click overwrite the only copy of those
decisions. Absence is an empty ledger; unreadable, oversized, malformed or
concurrently changed content is a visible blocker.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

MAX_LEDGER_BYTES = 4 * 1024 * 1024


class LedgerError(ValueError):
    """A durable decision ledger cannot be safely read or replaced."""


class LedgerEntries(list[dict]):
    """Decision list carrying the exact bytes from which it was loaded.

    Existing render/rank consumers use ordinary list operations.  Retaining the
    source bytes on that same object gives their later ``save_entries`` call a
    cross-process compare-and-swap guard without a parallel shadow API.
    """

    def __init__(self, entries: list[dict], *, path_key: str,
                 original: bytes | None) -> None:
        super().__init__(entries)
        self.path_key = path_key
        self.original = original


def _is_reparse(info: os.stat_result) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return bool(int(getattr(info, "st_file_attributes", 0)) & marker)


def _stable_bytes(path: Path, label: str) -> bytes | None:
    """Read one unchanged regular file, or ``None`` when it is absent."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LedgerError(f"{label} is unreadable: {exc}") from exc
    if not stat.S_ISREG(before.st_mode) or _is_reparse(before):
        raise LedgerError(f"{label} must be a regular non-link file")
    if before.st_size > MAX_LEDGER_BYTES:
        raise LedgerError(
            f"{label} exceeds the {MAX_LEDGER_BYTES}-byte decision-ledger limit"
        )
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    if hasattr(os, "O_NOFOLLOW"):
        flags |= int(os.O_NOFOLLOW)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise LedgerError(f"{label} is unreadable: {exc}") from exc
    try:
        opened = os.fstat(descriptor)
        if (not stat.S_ISREG(opened.st_mode) or _is_reparse(opened)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)):
            raise LedgerError(f"{label} changed identity while it was opened")
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            content = source.read(MAX_LEDGER_BYTES + 1)
        if len(content) > MAX_LEDGER_BYTES:
            raise LedgerError(
                f"{label} exceeds the {MAX_LEDGER_BYTES}-byte decision-ledger limit"
            )
        after = path.lstat()
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        if not stat.S_ISREG(after.st_mode) or _is_reparse(after) or identity != after_identity:
            raise LedgerError(f"{label} changed during read; no decision was changed")
        return content
    except OSError as exc:
        raise LedgerError(f"{label} is unreadable: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _decode(content: bytes, label: str) -> list[dict]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LedgerError(
            f"{label} is not valid UTF-8 JSON; repair it before continuing"
        ) from exc
    if not isinstance(value, list) or any(not isinstance(entry, dict) for entry in value):
        raise LedgerError(
            f"{label} must be a JSON array of decision objects; repair it before continuing"
        )
    return value


def load_entries(path: Path, *, label: str) -> list[dict]:
    """Load a valid decision list; only an absent file means no decisions."""
    content = _stable_bytes(path, label)
    entries = [] if content is None else _decode(content, label)
    return LedgerEntries(entries, path_key=_path_key(path), original=content)


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.abspath(path))


def _default_lock_dir(path: Path) -> Path:
    """Keep production locks private while allowing isolated fixture ledgers."""
    parent = path.parent
    if parent.name.lower() == "retro" and parent.parent.name.lower() == "docs":
        root = parent.parent.parent
        try:
            import runtime_paths  # noqa: PLC0415

            return runtime_paths.resolve(root).runtime / "retro" / "ledger-locks"
        except (OSError, ValueError):
            pass
    return parent / ".ledger-locks"


def _lock_file(path: Path, lock_dir: Path | None) -> Path:
    directory = lock_dir or _default_lock_dir(path)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        info = directory.lstat()
    except OSError as exc:
        raise LedgerError(f"decision-ledger lock directory is unavailable: {exc}") from exc
    if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
        raise LedgerError("decision-ledger lock directory must be an unredirected directory")
    digest = hashlib.sha256(_path_key(path).encode("utf-8")).hexdigest()
    return directory / f"{digest}.lock"


@contextmanager
def _exclusive_lock(path: Path, label: str,
                    lock_dir: Path | None) -> Iterator[None]:
    """Hold one OS-released advisory lock across a complete ledger mutation."""
    lock_path = _lock_file(path, lock_dir)
    flags = os.O_RDWR | os.O_CREAT | int(getattr(os, "O_BINARY", 0))
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise LedgerError(f"{label} mutation lock is unavailable: {exc}") from exc
    locked = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
            raise LedgerError(f"{label} mutation lock must be a regular non-link file")
        if info.st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt  # noqa: PLC0415

            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:
            import fcntl  # type: ignore[import-not-found]  # noqa: PLC0415

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
    except OSError as exc:
        os.close(descriptor)
        raise LedgerError(f"{label} mutation lock failed: {exc}") from exc
    try:
        yield
    finally:
        if locked:
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt  # noqa: PLC0415

                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl  # type: ignore[import-not-found]  # noqa: PLC0415

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(descriptor)


def _encoded_entries(entries: list[dict], label: str) -> bytes:
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        raise LedgerError(f"{label} can store only a list of decision objects")
    content = (json.dumps(
        entries, ensure_ascii=False, indent=2, sort_keys=True
    ) + "\n").encode("utf-8")
    if len(content) > MAX_LEDGER_BYTES:
        raise LedgerError(
            f"{label} would exceed the {MAX_LEDGER_BYTES}-byte decision-ledger limit"
        )
    return content


def _replace_file(temporary: Path, destination: Path) -> None:
    """Bound the short Windows sharing-violation window around ledger replace."""
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


def _replace_unlocked(path: Path, entries: list[dict], label: str,
                      original: bytes | None) -> LedgerEntries:
    content = _encoded_entries(entries, label)
    if original is None and not entries:
        return LedgerEntries([], path_key=_path_key(path), original=None)
    if content == original:
        return LedgerEntries(list(entries), path_key=_path_key(path), original=content)

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as destination:
            destination.write(content)
            destination.flush()
            os.fsync(destination.fileno())
        if _stable_bytes(path, label) != original:
            raise LedgerError(f"{label} changed during write; no decision was changed")
        _replace_file(temporary, path)
        if _stable_bytes(path, label) != content:
            raise LedgerError(f"{label} could not be verified after atomic replacement")
    finally:
        temporary.unlink(missing_ok=True)
    return LedgerEntries(list(entries), path_key=_path_key(path), original=content)


def save_entries(path: Path, entries: list[dict], *, label: str,
                 lock_dir: Path | None = None) -> None:
    """Serialize and atomically replace a valid ledger with optional CAS provenance."""
    with _exclusive_lock(path, label, lock_dir):
        original = _stable_bytes(path, label)
        if original is not None:
            _decode(original, label)
        if isinstance(entries, LedgerEntries):
            if entries.path_key != _path_key(path) or entries.original != original:
                raise LedgerError(
                    f"{label} changed after it was read; no decision was changed"
                )
        _replace_unlocked(path, entries, label, original)


def update_entries(path: Path, transform: Callable[[list[dict]], list[dict]], *,
                   label: str, lock_dir: Path | None = None) -> list[dict]:
    """Run one complete read-modify-write while holding the cross-process lock."""
    with _exclusive_lock(path, label, lock_dir):
        original = _stable_bytes(path, label)
        current = [] if original is None else _decode(original, label)
        updated = transform(current)
        if not isinstance(updated, list):
            raise LedgerError(f"{label} update must return a decision list")
        return _replace_unlocked(path, updated, label, original)
