#!/usr/bin/env python3
"""Query the engine's own class reference, generated from the local binary.

Why this exists: model training data goes stale, and web docs default to
whatever version the URL says. `godot --doctool` dumps the class reference
for *exactly* the engine binary on this machine, offline. That is the only
source of API truth that cannot drift from what will actually run.

    kit godot-docs build
    kit godot-docs show Color
    kit godot-docs show Color.from_string
    kit godot-docs search from_string
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional

import engine_discovery
import native_engine
import project_context

TOOLS = Path(__file__).resolve().parent
CORE_ROOT = TOOLS.parent
CONTEXT = project_context.load_active_context(CORE_ROOT)
ROOT = CONTEXT.project_root  # compatibility name for project-owned paths
DOC_DIR = ROOT / ".godot_doc" / "classes"
DOC_ROOT = DOC_DIR.parent
_ERROR_RE = re.compile(r"(?im)^\s*(?:SCRIPT ERROR|ERROR|FATAL|CRASH):")


def _promote_fresh_docs(staging: Path) -> None:
    """Replace the generated cache only after a fresh staged dump validates."""
    backup = staging.parent / f"previous-{uuid.uuid4().hex}"
    moved_existing = False
    try:
        if DOC_ROOT.exists():
            os.replace(DOC_ROOT, backup)
            moved_existing = True
        os.replace(staging, DOC_ROOT)
    except OSError:
        if moved_existing and backup.exists() and not DOC_ROOT.exists():
            os.replace(backup, DOC_ROOT)
        raise
    finally:
        if backup.exists() and DOC_ROOT.exists():
            shutil.rmtree(backup, ignore_errors=True)


def build(engine: str | None) -> int:
    if not engine:
        print("engine selection missing; use: kit godot-docs build")
        return 2
    try:
        authenticated = engine_discovery.authenticate_godot(
            ROOT,
            candidate=engine,
            operation="godot-docs-build",
        )
    except engine_discovery.EngineAuthenticationError as exc:
        print(f"godot-docs build refused: {exc}")
        return 2
    # Resolve again so synthetic project fixtures and explicit target projects
    # retain their own private runtime rather than borrowing this process's.
    runtime = project_context.load_configured_context(ROOT).runtime_root
    build_root = runtime / "godot-docs"
    build_root.mkdir(parents=True, exist_ok=True)
    staging = build_root / f"build-{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        # A unique empty destination makes stale pre-existing XML incapable of
        # satisfying this run's proof. Godot may choose a nested doc/classes
        # layout; readers deliberately search the promoted tree recursively.
        try:
            authenticated.assert_unchanged()
        except engine_discovery.EngineAuthenticationError as exc:
            print(f"godot-docs build refused: {exc}")
            return 2
        result = native_engine.run_godot(
            authenticated.path,
            ["--headless", "--doctool", str(staging)],
            root=ROOT,
            cwd=ROOT,
            timeout=300,
        )
        native_engine.persist_native_failure(
            ROOT, result, operation="godot-docs-build"
        )
        files = sorted(staging.rglob("*.xml"))
        if result.failure_class:
            print(f"doctool stopped at the safe native boundary ({result.failure_class}).")
            print(result.output[-800:])
            return 1
        if _ERROR_RE.search(result.output):
            print("doctool reported an engine error; generated output was not promoted.")
            print(result.output[-800:])
            return 1
        if not files:
            print("doctool produced no fresh XML; the previous cache was not accepted.")
            print(result.output[-800:])
            return 1
        try:
            for path in files:
                ET.parse(path)
        except (ET.ParseError, OSError) as exc:
            print(f"doctool produced invalid XML; the previous cache was preserved: {exc}")
            return 1
        count = len(files)
        _promote_fresh_docs(staging)
        print(f"built {count} fresh class files under {DOC_ROOT.relative_to(ROOT)}")
        return 0
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def xml_files() -> List[Path]:
    base = DOC_DIR.parent
    return sorted(base.rglob("*.xml")) if base.exists() else []


def _text(node: Optional[ET.Element], limit: int = 400) -> str:
    if node is None or not node.text:
        return ""
    body = re.sub(r"\s+", " ", node.text).strip()
    return body[:limit] + ("..." if len(body) > limit else "")


def _sig(m: ET.Element) -> str:
    ret = m.find("return")
    rtype = ret.get("type", "void") if ret is not None else "void"
    args = []
    for a in m.findall("param") + m.findall("argument"):
        piece = f"{a.get('name')}: {a.get('type')}"
        if a.get("default"):
            piece += f" = {a.get('default')}"
        args.append(piece)
    return f"{m.get('name')}({', '.join(args)}) -> {rtype}"


def find_class(name: str) -> Optional[Path]:
    for p in xml_files():
        if p.stem.lower() == name.lower():
            return p
    return None


def show(target: str) -> int:
    if not xml_files():
        print("no class reference yet. Run: kit godot-docs build")
        return 2
    cls, _, member = target.partition(".")
    path = find_class(cls)
    if path is None:
        print(f"class not found: {cls}")
        print("Run --search to look for a member name across all classes.")
        return 1
    root = ET.parse(path).getroot()

    if member:
        hits = 0
        for tag in ("method", "constructor", "operator"):
            for m in root.iter(tag):
                if m.get("name", "").lower() == member.lower():
                    print(f"{cls}.{_sig(m)}")
                    desc = _text(m.find("description"))
                    if desc:
                        print(f"  {desc}")
                    hits += 1
        for tag, label in (("member", "property"), ("constant", "constant")):
            for m in root.iter(tag):
                if m.get("name", "").lower() == member.lower():
                    print(f"{cls}.{m.get('name')}: {m.get('type', m.get('value', ''))}"
                          f"  [{label}]")
                    hits += 1
        if hits == 0:
            print(f"no member '{member}' on {cls}")
            return 1
        return 0

    inh = root.get("inherits") or "nothing (built-in type)"
    print(f"{root.get('name')}  (inherits {inh})")
    brief = _text(root.find("brief_description"), 300)
    if brief:
        print(f"  {brief}")
    for label, tag in (("constructors", "constructor"), ("methods", "method")):
        names = sorted({m.get("name", "") for m in root.iter(tag)})
        if names:
            print(f"  {label}: {', '.join(names)}")
    props = sorted({m.get("name", "") for m in root.iter("member")})
    if props:
        print(f"  properties: {', '.join(props)}")
    print(f"  detail: kit godot-docs show {cls}.<member>")
    return 0


def search(term: str) -> int:
    if not xml_files():
        print("no class reference yet. Run: kit godot-docs build")
        return 2
    needle = term.lower()
    found = 0
    for p in xml_files():
        root = ET.parse(p).getroot()
        cls = root.get("name", p.stem)
        for tag in ("method", "constructor", "member", "constant"):
            for m in root.iter(tag):
                if needle in m.get("name", "").lower():
                    kind = _sig(m) if tag in ("method", "constructor") else m.get("name", "")
                    print(f"{cls}.{kind}")
                    found += 1
                    if found >= 60:
                        print("... truncated, narrow the search term")
                        return 0
    if found == 0:
        print(f"no match for '{term}'")
        return 1
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", nargs="?", help="Class or Class.member")
    ap.add_argument("--build", action="store_true", help="generate the class reference")
    ap.add_argument("--engine", help=argparse.SUPPRESS)
    ap.add_argument("--search", metavar="TERM", help="search member names across classes")
    args = ap.parse_args()

    if args.build:
        return build(args.engine)
    if args.search:
        return search(args.search)
    if args.target:
        return show(args.target)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
