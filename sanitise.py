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

Usage
  python sanitise.py            report only, exit 1 if changes are needed
  python sanitise.py --write    apply changes in place
  python sanitise.py --json     machine-readable report
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
PROJECT_DIR = ROOT / "src"
EXCLUDED = {".godot", "addons", "build", "export", ".git", ".checklogs"}

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


def res_files() -> List[Path]:
    out: List[Path] = []
    for pattern in ("*.tscn", "*.tres"):
        for path in sorted(PROJECT_DIR.rglob(pattern)):
            if EXCLUDED.intersection(path.relative_to(PROJECT_DIR).parts):
                continue
            out.append(path)
    return out


def real_uid(res_path: str) -> Optional[str]:
    """The authoritative UID for a res:// path, or None if it has none.

    Scripts and shaders keep theirs in a .uid sidecar. Scenes and resources
    keep theirs on their own header line. Anything else (imported assets)
    stores it in .godot/, which is not committed, so treat it as unknown and
    leave the attribute alone rather than guess.
    """
    if not res_path.startswith("res://"):
        return None
    target = PROJECT_DIR / res_path[len("res://"):]

    if target.suffix in (".gd", ".gdshader"):
        sidecar = target.with_suffix(target.suffix + ".uid")
        if sidecar.is_file():
            text = sidecar.read_text(encoding="utf-8", errors="replace").strip()
            match = UID_LITERAL_RE.search(text)
            return match.group(0) if match else None
        return None

    if target.suffix in (".tscn", ".tres") and target.is_file():
        for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
            if HEADER_RE.match(line):
                match = UID_ATTR_RE.search(line)
                return match.group(1) if match else None
            if line.strip():
                break
        return None

    return None


def sanitise_text(text: str, rel: str) -> Tuple[str, List[str]]:
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
                    actual = real_uid(path_match.group(1))
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


def structural_errors(text: str, rel: str) -> List[str]:
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
                    target = PROJECT_DIR / match.group(1)[len("res://"):]
                    if not target.exists():
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write", action="store_true", help="apply changes in place")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args()

    files = res_files()
    all_notes: List[str] = []
    all_errs: List[str] = []
    changed: List[str] = []

    for path in files:
        rel = path.relative_to(PROJECT_DIR).as_posix()
        original = path.read_text(encoding="utf-8", errors="replace")
        cleaned, notes = sanitise_text(original, rel)
        all_errs.extend(structural_errors(cleaned, rel))
        if notes:
            all_notes.extend(notes)
        if cleaned != original:
            changed.append(rel)
            if args.write:
                path.write_text(cleaned, encoding="utf-8", newline="")

    if args.json:
        print(json.dumps({
            "scanned": len(files),
            "changed": changed,
            "notes": all_notes,
            "structural_errors": all_errs,
            "applied": args.write,
            "ok": not all_errs and (args.write or not changed),
        }, indent=2))
        return 0 if (not all_errs and (args.write or not changed)) else 1

    print(f"sanitise: scanned {len(files)} resource file(s)")
    for note in all_notes:
        print(f"  {'fixed  ' if args.write else 'needs  '}{note}")
    for err in all_errs:
        print(f"  ERROR  {err}")

    if all_errs:
        print(f"\n{len(all_errs)} structural error(s). These are not auto-fixable:"
              " the scene is malformed and must be corrected by hand.")
        return 1
    if changed and not args.write:
        print(f"\n{len(changed)} file(s) need sanitising. Apply with:"
              " python sanitise.py --write")
        return 1
    if changed:
        print(f"\nsanitised {len(changed)} file(s)")
    else:
        print("  clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
