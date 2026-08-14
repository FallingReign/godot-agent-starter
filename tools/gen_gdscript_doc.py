#!/usr/bin/env python3
"""Generate docs/GDSCRIPT.md from real code in this repo.

Strict-mode GDScript demands specific idioms that a model trained on ordinary
GDScript will not produce: typed loop iterators, typed locals, and explicit
narrowing of Variant values. An authored cookbook would be a second source of
truth that drifts. This derives every example from files that currently pass
the gate, so the document is a projection of working code.

Run:  python tools/gen_gdscript_doc.py --write
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parent.parent
SKIP = {".godot", "addons", "build", "export", ".git", ".checklogs"}

# Constructs strict mode most often rejects. Each pattern matches a line that
# demonstrates the CORRECT form, so examples come from code, never from here.
PATTERNS: List[Tuple[str, str, re.Pattern]] = [
    ("typed for-loop iterator",
     'An untyped iterator fails: "for" iterator variable has an implicitly '
     "inferred static type.",
     re.compile(r"^\s*for\s+\w+\s*:\s*[\w\[\], .]+\s+in\s+")),
    ("typed local declaration",
     'An untyped or inferred local fails: cannot infer the type of a variable '
     "because the value doesn't have a set type.",
     re.compile(r"^\s*var\s+\w+\s*:\s*[\w\[\], .]+\s*=")),
    ("Variant held in a typed local before use",
     "Calling a method or passing a Variant directly fails. Assign it to a "
     "typed local first, or narrow it.",
     re.compile(r"^\s*var\s+\w+\s*:\s*Variant\s*=")),
    ("narrowing a Variant to a number",
     'int() and float() reject Variant: argument 1 should be "int" but is '
     '"Variant". Wrap in str() or assign to a typed local first.',
     re.compile(r"(int|float)\(\s*str\(")),
    ("narrowing a Variant to a Dictionary or Array",
     "An unsafe cast fails. Type the destination explicitly and check the "
     "source type first.",
     re.compile(r"^\s*var\s+\w+\s*:\s*(Dictionary|Array)[\w\[\], ]*\s*=")),
    ("typed function signature",
     "Every parameter needs a type and every function needs a return type, "
     "including -> void.",
     re.compile(r"^\s*(static\s+)?func\s+\w+\(.*:.*\)\s*->\s*\w+")),
    ("typed constant",
     "An inferred const fails the same way an inferred var does.",
     re.compile(r"^\s*const\s+\w+\s*:\s*[\w\[\], .]+\s*=")),
    ("signal connection, Godot 4 form",
     "The Godot 3 string form parses but never fires. Use the Signal object.",
     re.compile(r"\.\w+\.connect\(")),
]


def gd_files() -> List[Path]:
    out: List[Path] = []
    for p in sorted(ROOT.rglob("*.gd")):
        if any(part in SKIP for part in p.relative_to(ROOT).parts):
            continue
        out.append(p)
    return out


def collect() -> Dict[str, List[Tuple[str, str]]]:
    """Map each construct to (location, source line) examples found in the repo."""
    found: Dict[str, List[Tuple[str, str]]] = {name: [] for name, _, _ in PATTERNS}
    for path in gd_files():
        rel = path.relative_to(ROOT).as_posix()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for num, line in enumerate(lines, start=1):
            if line.strip().startswith("#"):
                continue
            for name, _, pat in PATTERNS:
                if pat.search(line):
                    found[name].append((f"{rel}:{num}", line.strip()))
    return found


def render(found: Dict[str, List[Tuple[str, str]]], per: int = 3) -> str:
    out: List[str] = [
        "# GDScript under strict mode",
        "",
        "**Generated** by `tools/gen_gdscript_doc.py` from code in this repo that",
        "currently passes the gate. Do not edit by hand — regenerate instead.",
        "",
        "The `[debug]` warnings block in `project.godot` promotes untyped and unsafe",
        "operations to errors. That rejects several forms an LLM produces by default.",
        "Every example below is a real line from this repo.",
        "",
    ]
    for name, why, _ in PATTERNS:
        examples = found.get(name, [])
        out.append(f"## {name}")
        out.append("")
        out.append(why)
        out.append("")
        if not examples:
            out.append("_No example in this repo yet._")
            out.append("")
            continue
        out.append("```gdscript")
        for loc, src in examples[:per]:
            out.append(f"{src}    # {loc}")
        out.append("```")
        out.append("")
    out.append("## The boundary rule (this is the one that matters)")
    out.append("")
    out.append(TAIL_BOUNDARY.strip())
    out.append("")
    out.append("## Two lint rules that are not about types")
    out.append("")
    out.append(TAIL_LINT.strip())
    out.append("")
    out.append("## The general rule")
    out.append("")
    out.append("Anything reaching you from a `Dictionary`, `Array`, `JSON.parse_string`,")
    out.append("`get()` or `FileAccess` is a `Variant`. Give it a typed home before you")
    out.append("use it. One extra line per value, and the error disappears.")
    out.append("")
    return "\n".join(out)


TAIL_BOUNDARY = """
There is **no safe cast from Variant** in GDScript. `value as Type` trips
`unsafe_cast`, direct assignment trips `unsafe_property_access`, and
`Color(variant)` or `int(variant)` trip `unsafe_call_argument`. So fixing
individual use sites never converges: each fix reveals the next one downstream.
A field report of this loop ran for 242 turns and 19M tokens without finishing.

The fix is structural, not syntactic. Convert external data **once**, at the
boundary, then pass only typed values inward:

- `scripts/data/` is the boundary. It parses, validates, and returns typed
  objects. Loose `Dictionary` and `Variant` are correct here.
- `scripts/logic/` is the interior. It receives typed objects only. It never
  calls `JSON.parse_string`, `FileAccess.open` or `ConfigFile`, and its
  signatures never contain a bare `Dictionary`, `Array` or `Variant`.

The `types` gate stage enforces this direction. If it fires, do not add casts:
move the conversion to the boundary and change the signature to accept the
typed object.

Two mechanisms are safe inside the boundary. `is` narrowing, because the
analyser tracks the type inside the branch:

```gdscript
static func as_dict(v: Variant) -> Dictionary:
	if v is Dictionary:
		return v
	return {}
```

And `str()`, because it is vararg and accepts Variant. That is why
`int(str(v))` works where `int(v)` does not.

For colours, store hex strings in your data format and use
`Color.from_string(str(v), Color.MAGENTA)`. One call, no per-channel unpacking,
and hex is readable to anyone hand-editing content.
"""

TAIL_LINT = """
`class-definitions-order`: within a class, declare in this order — signals,
enums, constants, static variables, variables, `_init`, then other methods.
gdlint fails the file otherwise.

GUT doubles have no annotatable type. `stub()` and `double()` return an untyped
object, and there is no `Stub` class in scope, so a type annotation cannot be
satisfied. Assign with `:=` and let inference handle it, or use
`partial_double()` and hold the result as the real class. Inventing a type name
produces `Could not find type "Stub" in the current scope`, which under `-d`
sends Godot's debugger into a break loop.
"""

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="write docs/GDSCRIPT.md")
    ap.add_argument("--check", action="store_true", help="fail if the file is stale")
    args = ap.parse_args()

    text = render(collect())
    target = ROOT / "docs" / "GDSCRIPT.md"

    if args.check:
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
        if current.strip() != text.strip():
            print("docs/GDSCRIPT.md is stale. Run: python tools/gen_gdscript_doc.py --write")
            return 1
        print("docs/GDSCRIPT.md current")
        return 0

    if args.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")
        print(f"wrote {target.relative_to(ROOT).as_posix()}")
        return 0

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
