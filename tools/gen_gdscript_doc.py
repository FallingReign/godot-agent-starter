#!/usr/bin/env python3
"""Generate docs/GDSCRIPT.md from reviewed, project-neutral examples.

The public reference must be useful in every project and must never become a
copy of whichever game happened to be present when a maintainer built a kit.
Examples therefore live here as a small reviewed corpus rather than being
scraped from the configured game root.

This is a kit-maintainer implementation detail, not a public project command.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Tuple

TOOLS = Path(__file__).resolve().parent
CORE_ROOT = TOOLS.parent

ExampleMap = Dict[str, List[str]]
SECTIONS: List[Tuple[str, str, Tuple[str, ...]]] = [
    (
        "typed for-loop iterator",
        'An untyped iterator fails: "for" iterator variable has an implicitly '
        "inferred static type.",
        ("for label: String in labels:\n\tprint(label)",),
    ),
    (
        "typed local declaration",
        "An untyped or inferred local fails. Give every local an explicit type.",
        ("var elapsed_seconds: float = 0.0",),
    ),
    (
        "Variant held in a typed local before use",
        "Hold data returned by a loose container as Variant until it has been validated.",
        ('var raw_title: Variant = record.get("title", "")',),
    ),
    (
        "narrowing a Variant to a number",
        'int() and float() reject Variant under strict warnings. Convert through str() '
        "after validating the accepted representation.",
        ("var count: int = int(str(raw_count))",),
    ),
    (
        "narrowing a Variant to a Dictionary or Array",
        "Use an `is` branch so the analyser can prove the narrowed type.",
        (
            "var options: Dictionary = {}\n"
            "if raw_options is Dictionary:\n"
            "\toptions = raw_options",
        ),
    ),
    (
        "typed function signature",
        "Every parameter needs a type and every function needs a return type, "
        "including `-> void`.",
        (
            "func retained_strength(value: float) -> float:\n"
            "\treturn value",
        ),
    ),
    (
        "typed constant",
        "An inferred constant fails the same way an inferred variable does.",
        ("const DEFAULT_LIMIT: int = 10",),
    ),
    (
        "signal connection, Godot 4 form",
        "The Godot 3 string form parses but never fires. Connect the Signal object.",
        ("confirm_button.pressed.connect(_on_confirm_pressed)",),
    ),
]


def collect() -> ExampleMap:
    """Return a copy of the reviewed neutral example corpus."""
    return {name: list(examples) for name, _why, examples in SECTIONS}


def render(found: ExampleMap, per: int = 3) -> str:
    out: List[str] = [
        "# GDScript under strict mode",
        "",
        "**Generated** by the kit-maintainer documentation tool from reviewed,",
        "project-neutral examples. Do not edit by hand.",
        "",
        "The `[debug]` warnings block in `project.godot` promotes untyped and unsafe",
        "operations to errors. That rejects several forms an LLM produces by default.",
        "The examples deliberately contain no source from the configured game.",
        "",
    ]
    for name, why, _examples in SECTIONS:
        out.append(f"## {name}")
        out.append("")
        out.append(why)
        out.append("")
        examples = found.get(name, [])
        if not examples:
            out.append("_No reviewed example available._")
            out.append("")
            continue
        for source in examples[:per]:
            out.append("```gdscript")
            out.extend(source.splitlines())
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
    out.append("use it. One explicit conversion per value keeps the looseness at the edge.")
    out.append("")
    return "\n".join(out)


TAIL_BOUNDARY = """
There is **no general safe cast from Variant** in GDScript. `value as Type` can
trip `unsafe_cast`, direct member access can trip `unsafe_property_access`, and
passing an unchecked Variant to a typed constructor can trip
`unsafe_call_argument`.

The fix is structural, not a trail of casts. Convert external data **once**, at
the boundary, then pass only typed values inward:

- `scripts/data/` is the boundary. It parses, validates, and returns typed
  objects. Loose `Dictionary` and `Variant` are correct here.
- `scripts/logic/` is the interior. It receives typed objects only. It never
  calls `JSON.parse_string`, `FileAccess.open` or `ConfigFile`, and its
  signatures never contain a bare `Dictionary`, `Array` or `Variant`.

The `types` gate stage enforces this direction. If it fires, move conversion to
the boundary and change the interior signature to accept the typed object.

Two mechanisms are safe inside the boundary. `is` narrowing lets the analyser
track the type inside a branch:

```gdscript
static func as_dictionary(value: Variant) -> Dictionary:
	if value is Dictionary:
		return value
	return {}
```

`str()` accepts Variant. After validating that a numeric representation is
allowed, `int(str(value))` provides an explicit conversion path.

For colours stored as text, `Color.from_string(str(value), Color.MAGENTA)` keeps
the external representation readable and provides an explicit fallback.
"""

TAIL_LINT = """
`class-definitions-order`: within a class, declare in this order — signals,
enums, constants, static variables, variables, `_init`, then other methods.
gdlint fails the file otherwise.

GUT's dynamic doubles do not provide a stable annotatable type. In a project
that bans inferred declarations, prefer a small typed fake or a partial double
held as the real collaborator type. Do not weaken strict typing just to store a
test double.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write", action="store_true", help="write docs/GDSCRIPT.md")
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    arguments = parser.parse_args()

    text = render(collect())
    target = CORE_ROOT / "docs" / "GDSCRIPT.md"

    if arguments.check:
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
        if current.strip() != text.strip():
            print(
                "docs/GDSCRIPT.md is stale; refresh it through the kit-builder "
                "maintenance workflow"
            )
            return 1
        print("docs/GDSCRIPT.md current")
        return 0

    if arguments.write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="")
        print(f"wrote {target.relative_to(CORE_ROOT).as_posix()}")
        return 0

    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
