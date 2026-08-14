#!/usr/bin/env python3
"""Query the engine's own class reference, generated from the local binary.

Why this exists: model training data goes stale, and web docs default to
whatever version the URL says. `godot --doctool` dumps the class reference
for *exactly* the engine binary on this machine, offline. That is the only
source of API truth that cannot drift from what will actually run.

    python tools/gddoc.py --build             # generate/refresh the dump
    python tools/gddoc.py Color               # class summary
    python tools/gddoc.py Color.from_string   # one member
    python tools/gddoc.py --search from_string
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional

ROOT = Path(__file__).resolve().parent.parent
DOC_DIR = ROOT / ".godot_doc" / "classes"


def find_godot() -> Optional[str]:
    env = os.environ.get("GODOT_BIN")
    if env and (shutil.which(env) or Path(env).exists()):
        return env
    for name in ("godot", "godot4", "Godot"):
        found = shutil.which(name)
        if found:
            return found
    return None


def build() -> int:
    godot = find_godot()
    if godot is None:
        print("godot not found. Set GODOT_BIN or run: python bootstrap.py --json")
        return 2
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    # --doctool writes <path>/doc/classes/*.xml for engine classes.
    proc = subprocess.run([godot, "--headless", "--doctool", str(DOC_DIR.parent)],
                          capture_output=True, text=True, timeout=300)
    files = list(DOC_DIR.parent.rglob("*.xml"))
    if not files:
        print("doctool produced no XML.")
        print((proc.stdout + proc.stderr)[-800:])
        return 1
    print(f"built {len(files)} class files under {DOC_DIR.parent.relative_to(ROOT)}")
    return 0


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
        print("no class reference yet. Run: python tools/gddoc.py --build")
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
    print(f"  detail: python tools/gddoc.py {cls}.<member>")
    return 0


def search(term: str) -> int:
    if not xml_files():
        print("no class reference yet. Run: python tools/gddoc.py --build")
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
    ap.add_argument("--search", metavar="TERM", help="search member names across classes")
    args = ap.parse_args()

    if args.build:
        return build()
    if args.search:
        return search(args.search)
    if args.target:
        return show(args.target)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
