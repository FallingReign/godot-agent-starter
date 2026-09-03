#!/usr/bin/env python3
"""Deterministic sanitiser for Godot text resources.

Removes the small, closed set of fields an agent is not permitted to author,
and verifies every UID against its real source. Pure standard library: Godot's
text formats are Godot's to interpret, so this tool only ever *deletes*
fields it can prove are wrong, never rewrites semantics.

What it removes
  unique_id=...    on [node] lines. Present only in 4.6+, documented as
                   optional and not guaranteed. Regenerates on every reimport
                   of a GLB-inherited scene, producing hundreds of noise lines.
  load_steps=N     on the [gd_scene]/[gd_resource] header. Deprecated in 4.6;
                   an incorrect value only ever affected loading bars.
  uid="uid://..."  ONLY when it does not match the target's real UID.
                   `path=` is authoritative: Godot logs
                   `invalid UID ... using text path instead` and loads fine.

What it never does
  Invent a UID. Reorder or reformat anything. Touch .import or .uid files.
  Touch a line it does not recognise.

Public usage
  kit sanitize                  report only, exit 1 if changes are needed
  kit sanitize --write          apply changes in place
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path, PurePosixPath
from typing import Dict, List, Optional, Sequence, Tuple

CORE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(CORE_ROOT / "tools"))
import project_context  # noqa: E402

CONTEXT = project_context.load_active_context(CORE_ROOT)
PROJECT_ROOT = CONTEXT.project_root
PROJECT_DIR = CONTEXT.game_root
EXCLUDED = {
    ".agents", ".checklogs", ".git", ".github", ".godot", ".godot_doc",
    ".kit", "addons", "build", "docs", "export", "plan", "tools",
}

HEADER_RE = re.compile(r"^\[gd_(scene|resource)\b")
NODE_RE = re.compile(r"^\[node\b")
EXT_RE = re.compile(r"^\[ext_resource\b")
UID_ATTR_RE = re.compile(r'\s+uid="(uid://[^"]*)"')
UNIQUE_ID_RE = re.compile(r'\s+unique_id="[^"]*"')
LOAD_STEPS_RE = re.compile(r"\s+load_steps=\d+")
PATH_ATTR_RE = re.compile(r'\bpath="([^"]*)"')
UID_LITERAL_RE = re.compile(r"uid://[A-Za-z0-9]+")
# A real Godot UID is "uid://" followed by lowercase alphanumerics only. Anything
# else (uppercase, punctuation, non-ASCII) cannot have come from the engine and
# is therefore fabricated. Seen in the field: uid://c6gscx7f7<tamil>r.
WELLFORMED_UID_RE = re.compile(r"^uid://[a-z0-9]+$")
# unique_id written as a standalone property line rather than a node attribute.
UNIQUE_ID_LINE_RE = re.compile(r'^\s*unique_id\s*=')

RESOURCE_SUFFIXES = frozenset({".tscn", ".tres"})
UID_SIDECAR_ENDINGS = (".gd.uid", ".gdshader.uid")
MAX_SANITISE_SCAN_DEPTH = 12
MAX_SANITISE_SCAN_MEMBERS = 20_000
MAX_SANITISE_FILE_BYTES = 4 * 1024 * 1024
MAX_SANITISE_TOTAL_BYTES = 64 * 1024 * 1024

_PATH_IDENTITY_FIELDS = (
    "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_nlink",
)
_HANDLE_IDENTITY_FIELDS = (
    "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink",
)
_WRITE_IDENTITY_FIELDS = ("st_dev", "st_ino", "st_mode", "st_nlink")


class SanitiseScanError(RuntimeError):
    """The project cannot be safely and completely scanned."""


class ScannedFile:
    __slots__ = ("content", "expected", "path", "relative")

    def __init__(
        self,
        path: Path,
        relative: Path,
        content: bytes,
        expected: os.stat_result,
    ) -> None:
        self.path = path
        self.relative = relative
        self.content = content
        self.expected = expected


class SanitiseScan:
    __slots__ = ("members", "resources", "root", "_text")

    def __init__(
        self,
        root: Path,
        resources: Sequence[ScannedFile],
        text_files: Sequence[ScannedFile],
        members: Sequence[str],
    ) -> None:
        self.root = root
        self.resources = tuple(resources)
        self.members = frozenset(
            value.casefold() if os.name == "nt" else value
            for value in members
        )
        self._text = {
            (
                item.relative.as_posix().casefold()
                if os.name == "nt"
                else item.relative.as_posix()
            ): item
            for item in text_files
        }

    def text(self, relative: str) -> Optional[str]:
        key = relative.casefold() if os.name == "nt" else relative
        item = self._text.get(key)
        if item is None:
            return None
        return item.content.decode("utf-8", errors="replace")

    def contains(self, relative: str) -> bool:
        key = relative.casefold() if os.name == "nt" else relative
        return key in self.members


def _identity(info: os.stat_result, fields: Tuple[str, ...]) -> Tuple[object, ...]:
    return tuple(getattr(info, field, None) for field in fields)


def _is_reparse(info: os.stat_result) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return bool(int(getattr(info, "st_file_attributes", 0) or 0) & marker)


def _label(relative: Path) -> str:
    rendered = relative.as_posix()
    return rendered if rendered and rendered != "." else "<game root>"


def _blocked(detail: str) -> SanitiseScanError:
    return SanitiseScanError(f"sanitise scan blocked: {detail}")


def _validate_directory(info: os.stat_result, relative: Path) -> None:
    label = _label(relative)
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise _blocked(f"linked directory is not allowed: {label}")
    if not stat.S_ISDIR(info.st_mode):
        raise _blocked(f"expected a directory: {label}")


def _validate_file(info: os.stat_result, relative: Path) -> None:
    label = _label(relative)
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise _blocked(f"linked file is not allowed: {label}")
    if not stat.S_ISREG(info.st_mode):
        raise _blocked(f"non-regular file is not allowed: {label}")
    if int(getattr(info, "st_nlink", 1)) != 1:
        raise _blocked(f"hard-linked file is not allowed: {label}")


def _read_file(path: Path, relative: Path, expected: os.stat_result) -> bytes:
    label = _label(relative)
    try:
        path_before = path.lstat()
        _validate_file(path_before, relative)
        if _identity(expected, _PATH_IDENTITY_FIELDS) != _identity(
            path_before, _PATH_IDENTITY_FIELDS
        ):
            raise _blocked(f"resource changed during the scan: {label}")
        if path_before.st_size > MAX_SANITISE_FILE_BYTES:
            raise _blocked(
                f"resource exceeds {MAX_SANITISE_FILE_BYTES} bytes: {label}"
            )

        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(path, flags)
        try:
            handle_before = os.fstat(descriptor)
            chunks: List[bytes] = []
            remaining = MAX_SANITISE_FILE_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            handle_after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        path_after = path.lstat()
    except SanitiseScanError:
        raise
    except OSError as exc:
        raise _blocked(f"resource could not be read: {label}: {exc}") from exc

    if len(content) > MAX_SANITISE_FILE_BYTES:
        raise _blocked(f"resource exceeds {MAX_SANITISE_FILE_BYTES} bytes: {label}")
    _validate_file(path_after, relative)
    if (
        _identity(path_before, _PATH_IDENTITY_FIELDS)
        != _identity(path_after, _PATH_IDENTITY_FIELDS)
        or _identity(path_before, _HANDLE_IDENTITY_FIELDS)
        != _identity(handle_before, _HANDLE_IDENTITY_FIELDS)
        or _identity(handle_before, _HANDLE_IDENTITY_FIELDS)
        != _identity(handle_after, _HANDLE_IDENTITY_FIELDS)
        or len(content) != handle_before.st_size
    ):
        raise _blocked(f"resource changed while it was read: {label}")
    return content


def scan_project(project_dir: Optional[Path] = None) -> SanitiseScan:
    """Capture one bounded, link-free resource and UID inventory."""
    selected = PROJECT_DIR if project_dir is None else Path(project_dir)
    root = Path(os.path.abspath(selected))
    try:
        root_info = root.lstat()
        _validate_directory(root_info, Path())
    except SanitiseScanError:
        raise
    except OSError as exc:
        raise _blocked(f"game root could not be inspected: {root}: {exc}") from exc

    resources: List[ScannedFile] = []
    text_files: List[ScannedFile] = []
    members: List[str] = []
    member_count = 0
    total_bytes = 0
    excluded = {name.casefold() for name in EXCLUDED}
    seen_directories: set[Tuple[int, int]] = set()
    root_identity = (int(root_info.st_dev), int(root_info.st_ino))
    if root_identity[1]:
        seen_directories.add(root_identity)
    pending: List[Tuple[Path, Path, os.stat_result]] = [(root, Path(), root_info)]

    while pending:
        directory, relative_dir, expected_dir = pending.pop()
        directory_label = _label(relative_dir)
        try:
            before_dir = directory.lstat()
            _validate_directory(before_dir, relative_dir)
            if _identity(expected_dir, _PATH_IDENTITY_FIELDS) != _identity(
                before_dir, _PATH_IDENTITY_FIELDS
            ):
                raise _blocked(f"directory changed during the scan: {directory_label}")

            with os.scandir(directory) as entries:
                for entry in entries:
                    member_count += 1
                    if member_count > MAX_SANITISE_SCAN_MEMBERS:
                        raise _blocked(
                            "project contains more than "
                            f"{MAX_SANITISE_SCAN_MEMBERS} scan members"
                        )
                    relative = relative_dir / entry.name
                    rendered = _label(relative)
                    if entry.name.casefold() in excluded:
                        continue
                    if len(relative.parts) > MAX_SANITISE_SCAN_DEPTH:
                        raise _blocked(
                            "project nesting exceeds "
                            f"{MAX_SANITISE_SCAN_DEPTH} levels: {rendered}"
                        )

                    suffix = Path(entry.name).suffix.casefold()
                    is_resource = suffix in RESOURCE_SUFFIXES
                    is_uid = entry.name.casefold().endswith(UID_SIDECAR_ENDINGS)
                    included = is_resource or is_uid
                    path = directory / entry.name
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode):
                        raise _blocked(f"symbolic link is not allowed: {rendered}")
                    if _is_reparse(info):
                        if stat.S_ISDIR(info.st_mode):
                            raise _blocked(f"linked directory is not allowed: {rendered}")
                        raise _blocked(f"linked file is not allowed: {rendered}")

                    if stat.S_ISDIR(info.st_mode):
                        _validate_directory(info, relative)
                        identity = (int(info.st_dev), int(info.st_ino))
                        if identity[1] and identity in seen_directories:
                            raise _blocked(
                                f"directory alias or hard link is not allowed: {rendered}"
                            )
                        if identity[1]:
                            seen_directories.add(identity)
                        members.append(relative.as_posix())
                        pending.append((path, relative, info))
                        continue
                    if stat.S_ISREG(info.st_mode):
                        members.append(relative.as_posix())
                    if not included:
                        continue

                    _validate_file(info, relative)
                    if total_bytes + info.st_size > MAX_SANITISE_TOTAL_BYTES:
                        raise _blocked(
                            "resources exceed "
                            f"{MAX_SANITISE_TOTAL_BYTES} total bytes"
                        )
                    content = _read_file(path, relative, info)
                    total_bytes += len(content)
                    if total_bytes > MAX_SANITISE_TOTAL_BYTES:
                        raise _blocked(
                            "resources exceed "
                            f"{MAX_SANITISE_TOTAL_BYTES} total bytes"
                        )
                    scanned = ScannedFile(path, relative, content, info)
                    text_files.append(scanned)
                    if is_resource:
                        resources.append(scanned)

            after_dir = directory.lstat()
            _validate_directory(after_dir, relative_dir)
            if _identity(before_dir, _PATH_IDENTITY_FIELDS) != _identity(
                after_dir, _PATH_IDENTITY_FIELDS
            ):
                raise _blocked(f"directory changed during the scan: {directory_label}")
        except SanitiseScanError:
            raise
        except OSError as exc:
            raise _blocked(
                f"directory could not be scanned: {directory_label}: {exc}"
            ) from exc

    resources.sort(key=lambda item: item.relative.as_posix())
    text_files.sort(key=lambda item: item.relative.as_posix())
    return SanitiseScan(root, resources, text_files, members)


def res_files() -> List[Path]:
    return [item.path for item in scan_project().resources]


def _res_relative(res_path: str) -> Optional[str]:
    if not res_path.startswith("res://"):
        return None
    raw = res_path[len("res://"):]
    candidate = PurePosixPath(raw)
    if (
        not raw
        or "\\" in raw
        or "\x00" in raw
        or candidate.is_absolute()
        or candidate.as_posix() != raw
        or any(part in ("", ".", "..") for part in candidate.parts)
        or (candidate.parts and ":" in candidate.parts[0])
    ):
        return None
    return raw


def real_uid(res_path: str, *, scan: Optional[SanitiseScan] = None) -> Optional[str]:
    """The authoritative UID for a res:// path, or None if it has none.

    Scripts and shaders keep theirs in a .uid sidecar. Scenes and resources
    keep theirs on their own header line. Anything else (imported assets)
    stores it in .godot/, which is not committed, so treat it as unknown and
    leave the attribute alone rather than guess.
    """
    relative = _res_relative(res_path)
    if relative is None:
        return None
    inventory = scan if scan is not None else scan_project()
    target = PurePosixPath(relative)

    if target.suffix in (".gd", ".gdshader"):
        sidecar = target.with_suffix(target.suffix + ".uid").as_posix()
        text = inventory.text(sidecar)
        if text is not None:
            text = text.strip()
            match = UID_LITERAL_RE.search(text)
            return match.group(0) if match else None
        return None

    if target.suffix in RESOURCE_SUFFIXES:
        text = inventory.text(relative)
        if text is None:
            return None
        for line in text.splitlines():
            if HEADER_RE.match(line):
                match = UID_ATTR_RE.search(line)
                return match.group(1) if match else None
            if line.strip():
                break
        return None

    return None


def sanitise_text(
    text: str,
    rel: str,
    *,
    scan: Optional[SanitiseScan] = None,
) -> Tuple[str, List[str]]:
    notes: List[str] = []
    out_lines: List[str] = []
    # Preserve the file's own line ending rather than imposing one.
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(("\n", "\r\n"))

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw

        if HEADER_RE.match(line):
            if LOAD_STEPS_RE.search(line):
                line = LOAD_STEPS_RE.sub("", line)
                notes.append(f"{rel}:{lineno}: removed load_steps (deprecated in 4.6)")
            # A header uid identifies THIS file. Only Godot can assign it, so
            # keep a well-formed one and never fabricate one. A malformed value
            # cannot have come from the engine, so it is removed; Godot assigns
            # a real one the next time it saves the file.
            head_uid = UID_ATTR_RE.search(line)
            if head_uid and not WELLFORMED_UID_RE.match(head_uid.group(1)):
                line = UID_ATTR_RE.sub("", line)
                notes.append(f"{rel}:{lineno}: removed malformed header uid "
                             f"{head_uid.group(1)!r} (not engine-generated)")

        elif NODE_RE.match(line):
            if UNIQUE_ID_RE.search(line):
                line = UNIQUE_ID_RE.sub("", line)
                notes.append(f"{rel}:{lineno}: removed unique_id (optional, churns on reimport)")

        elif EXT_RE.match(line):
            uid_match = UID_ATTR_RE.search(line)
            path_match = PATH_ATTR_RE.search(line)
            if uid_match:
                claimed = uid_match.group(1)
                if not WELLFORMED_UID_RE.match(claimed):
                    line = UID_ATTR_RE.sub("", line)
                    notes.append(f"{rel}:{lineno}: removed malformed uid "
                                 f"{claimed!r} (not engine-generated)")
                elif not path_match:
                    line = UID_ATTR_RE.sub("", line)
                    notes.append(f"{rel}:{lineno}: removed uid with no path= to verify against")
                else:
                    actual = real_uid(path_match.group(1), scan=scan)
                    if actual is None:
                        pass  # unknowable from committed files; leave alone
                    elif actual != claimed:
                        line = UID_ATTR_RE.sub("", line)
                        notes.append(
                            f"{rel}:{lineno}: removed wrong uid {claimed} "
                            f"(real is {actual}); path= is authoritative")

        if UNIQUE_ID_LINE_RE.match(line):
            notes.append(f"{rel}:{lineno}: removed unique_id line "
                         "(optional, churns on reimport)")
            continue

        out_lines.append(line)

    result = newline.join(out_lines)
    if trailing:
        result += newline
    return result, notes


def structural_errors(
    text: str,
    rel: str,
    *,
    scan: Optional[SanitiseScan] = None,
) -> List[str]:
    """Text-only assertions. Cheap, deterministic, catch the common mistakes."""
    errs: List[str] = []
    roots: List[str] = []
    declared: List[str] = []
    seen: Dict[str, int] = {}

    for lineno, line in enumerate(text.splitlines(), 1):
        if not NODE_RE.match(line):
            if EXT_RE.match(line):
                match = PATH_ATTR_RE.search(line)
                if match and match.group(1).startswith("res://"):
                    relative = _res_relative(match.group(1))
                    if relative is None:
                        errs.append(
                            f"{rel}:{lineno}: ext_resource path is not a canonical "
                            f"project path: {match.group(1)}"
                        )
                        continue
                    inventory = scan if scan is not None else scan_project()
                    if not inventory.contains(relative):
                        errs.append(f"{rel}:{lineno}: ext_resource path does not exist:"
                                    f" {match.group(1)}")
            continue

        name_m = re.search(r'name="([^"]*)"', line)
        parent_m = re.search(r'parent="([^"]*)"', line)
        if not name_m:
            errs.append(f"{rel}:{lineno}: [node] with no name=")
            continue
        name = name_m.group(1)

        if parent_m is None:
            roots.append(name)
            declared.append(".")
            continue

        parent = parent_m.group(1)
        if parent not in declared:
            errs.append(f'{rel}:{lineno}: parent="{parent}" is not a declared node')
        full = name if parent == "." else f"{parent}/{name}"
        if full in seen:
            errs.append(f"{rel}:{lineno}: duplicate node path {full}"
                        f" (also line {seen[full]})")
        seen[full] = lineno
        declared.append(full)

    if rel.endswith(".tscn"):
        if not roots:
            errs.append(f"{rel}: no root node (every scene needs exactly one)")
        elif len(roots) > 1:
            errs.append(f"{rel}: {len(roots)} root nodes ({', '.join(roots)});"
                        f" exactly one is allowed")
    return errs


def _write_scanned_file(source: ScannedFile, text: str) -> None:
    """Write only if the scanned path still names the same unlinked file."""
    label = _label(source.relative)
    encoded = text.encode("utf-8")
    descriptor = -1
    try:
        path_before = source.path.lstat()
        _validate_file(path_before, source.relative)
        if _identity(source.expected, _PATH_IDENTITY_FIELDS) != _identity(
            path_before, _PATH_IDENTITY_FIELDS
        ):
            raise _blocked(f"resource changed before it could be written: {label}")

        flags = os.O_WRONLY | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(source.path, flags)
        handle_before = os.fstat(descriptor)
        _validate_file(handle_before, source.relative)
        if _identity(path_before, _HANDLE_IDENTITY_FIELDS) != _identity(
            handle_before, _HANDLE_IDENTITY_FIELDS
        ):
            raise _blocked(f"resource changed before it could be written: {label}")

        os.ftruncate(descriptor, 0)
        offset = 0
        while offset < len(encoded):
            written = os.write(descriptor, encoded[offset:])
            if written <= 0:
                raise OSError("write returned no progress")
            offset += written
        handle_after = os.fstat(descriptor)
        path_after = source.path.lstat()
        _validate_file(path_after, source.relative)
    except SanitiseScanError:
        raise
    except OSError as exc:
        raise _blocked(f"resource could not be written: {label}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if (
        _identity(handle_after, _WRITE_IDENTITY_FIELDS)
        != _identity(path_after, _WRITE_IDENTITY_FIELDS)
        or handle_after.st_size != len(encoded)
        or path_after.st_size != len(encoded)
    ):
        raise _blocked(f"resource changed while it was written: {label}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="apply changes in place")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    try:
        inventory = scan_project()
    except SanitiseScanError as exc:
        message = str(exc)
        if args.json:
            print(json.dumps({
                "scanned": 0,
                "changed": [],
                "notes": [],
                "structural_errors": [],
                "errors": [message],
                "applied": False,
                "ok": False,
            }, indent=2))
        else:
            print(message)
        return 2

    files = inventory.resources
    all_notes: List[str] = []
    all_errs: List[str] = []
    scan_errors: List[str] = []
    changed: List[str] = []

    for source in files:
        rel = source.relative.as_posix()
        original = source.content.decode("utf-8", errors="replace")
        cleaned, notes = sanitise_text(original, rel, scan=inventory)
        all_errs.extend(structural_errors(cleaned, rel, scan=inventory))
        if notes:
            all_notes.extend(notes)
        if cleaned != original:
            changed.append(rel)
            if args.write:
                try:
                    _write_scanned_file(source, cleaned)
                except SanitiseScanError as exc:
                    scan_errors.append(str(exc))

    if args.json:
        print(json.dumps({
            "scanned": len(files),
            "changed": changed,
            "notes": all_notes,
            "structural_errors": all_errs,
            "errors": scan_errors,
            "applied": args.write and not scan_errors,
            "ok": not scan_errors and not all_errs and (args.write or not changed),
        }, indent=2))
        if scan_errors:
            return 2
        return 0 if (not all_errs and (args.write or not changed)) else 1

    print(f"sanitise: scanned {len(files)} resource file(s)")
    for note in all_notes:
        print(f"  {'fixed  ' if args.write else 'needs  '}{note}")
    for err in all_errs:
        print(f"  ERROR  {err}")
    for error in scan_errors:
        print(f"  ERROR  {error}")

    if scan_errors:
        return 2

    if all_errs:
        print(f"\n{len(all_errs)} structural error(s). These are not auto-fixable:"
              " the scene is malformed and must be corrected by hand.")
        return 1
    if changed and not args.write:
        print(f"\n{len(changed)} file(s) need sanitising. Apply with:"
              " kit sanitize --write")
        return 1
    if changed:
        print(f"\nsanitised {len(changed)} file(s)")
    else:
        print("  clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
