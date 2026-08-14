#!/usr/bin/env python3
"""Derive a retrieval index and code bindings for the design body.

Two jobs, both projections. Nothing here is a source of truth.

  index     docs/design/INDEX.md - one line per section so an agent can find
            the right document without loading the whole design body.

  bindings  A generated block inside each design document listing which
            tunables it declares, where code claims them, and their current
            values. Design names the tunable; code claims it with a
            `## @tune <id>` doc comment. Design never contains a path.

Run with no arguments to regenerate both and report problems.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / "docs" / "design"
SRC = ROOT / "src"

INDEX_NAME = "INDEX.md"
SKIP_FILES = {"README.md", INDEX_NAME}

BIND_BEGIN = "<!-- BEGIN GENERATED BINDINGS -->"
BIND_END = "<!-- END GENERATED BINDINGS -->"

RE_TITLE = re.compile(r"^#\s+(.+?)\s*$", re.M)
RE_RESOLUTION = re.compile(r"^_Resolution:\s*([a-z]+)", re.M | re.I)
RE_H2 = re.compile(r"^##\s+(.+?)\s*$", re.M)
RE_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")
RE_TUNE_ROW = re.compile(r"^\|\s*`([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)`\s*\|", re.M | re.I)
RE_TUNE_ANNOT = re.compile(r"^\s*##\s*@tune\s+([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)\s*$", re.I)
RE_DECL = re.compile(
    r"^\s*(?:@export(?:_[a-z_]+)?(?:\([^)]*\))?\s+)?(const|static\s+var|var)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*[^=]+?)?\s*(?::?=)\s*(.+?)\s*$"
)

RESOLUTIONS = ("question", "direction", "settled")


def design_files() -> list[Path]:
    if not DESIGN.is_dir():
        return []
    out = []
    for p in sorted(DESIGN.rglob("*.md")):
        if p.name in SKIP_FILES:
            continue
        out.append(p)
    return out


def first_sentence(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    if not text:
        return ""
    m = re.search(r"(?<=[.!?])\s", text)
    s = text[: m.start() + 1] if m else text
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "\u2026"
    return s


def section_body(text: str, heading: str) -> str:
    """Text under a given h2, up to the next h2."""
    pat = re.compile(r"^##\s+" + re.escape(heading) + r"\s*$(.*?)(?=^##\s|\Z)", re.M | re.S | re.I)
    m = pat.search(text)
    return m.group(1).strip() if m else ""


def strip_generated(text: str) -> str:
    if BIND_BEGIN in text and BIND_END in text:
        head, rest = text.split(BIND_BEGIN, 1)
        _, tail = rest.split(BIND_END, 1)
        return head + tail
    return text


def parse_design(path: Path) -> dict:
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = strip_generated(raw)
    rel = path.relative_to(DESIGN).as_posix()

    tm = RE_TITLE.search(text)
    title = tm.group(1).strip() if tm else path.stem.replace("-", " ").capitalize()

    rm = RE_RESOLUTION.search(text)
    resolution = rm.group(1).lower() if rm else "unstated"

    intent = section_body(text, "Intent")
    if not intent:
        # fall back to the first paragraph after the title
        after = text[tm.end():] if tm else text
        after = re.sub(r"^_Resolution:[^\n]*\n", "", after.lstrip(), flags=re.M)
        intent = after.strip().split("\n\n")[0] if after.strip() else ""

    headings = [h for h in RE_H2.findall(text)]
    declared = sorted(set(RE_TUNE_ROW.findall(section_body(text, "Tunables"))))

    links = []
    for target in RE_MD_LINK.findall(text):
        resolved = (path.parent / target).resolve()
        try:
            links.append(resolved.relative_to(DESIGN.resolve()).as_posix())
        except ValueError:
            continue

    return {
        "path": rel,
        "title": title,
        "resolution": resolution,
        "summary": first_sentence(intent),
        "headings": headings,
        "declared_tunables": declared,
        "links": sorted(set(links)),
        "has_constraints": bool(section_body(text, "Constraints it imposes")),
    }


def scan_tunables() -> dict[str, dict]:
    """Find `## @tune <id>` claims in GDScript."""
    found: dict[str, dict] = {}
    if not SRC.is_dir():
        return found
    for gd in sorted(SRC.rglob("*.gd")):
        if "addons" in gd.parts:
            continue
        try:
            lines = gd.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        i = 0
        while i < len(lines):
            m = RE_TUNE_ANNOT.match(lines[i])
            if not m:
                i += 1
                continue
            tid = m.group(1)
            note_lines = []
            j = i + 1
            while j < len(lines) and lines[j].lstrip().startswith("##"):
                note_lines.append(lines[j].lstrip()[2:].strip())
                j += 1
            value, kind, symbol = "", "", ""
            if j < len(lines):
                d = RE_DECL.match(lines[j])
                if d:
                    kind = " ".join(d.group(1).split())
                    symbol = d.group(2)
                    value = d.group(3).strip()
                    if "@export" in lines[j]:
                        kind = "@export"
            rel = gd.relative_to(ROOT).as_posix()
            found[tid] = {
                "file": rel,
                "line": j + 1 if j < len(lines) else i + 1,
                "symbol": symbol,
                "value": value,
                "kind": kind,
                "note": " ".join(note_lines).strip(),
            }
            i = j + 1
    return found


def render_bindings(declared: list[str], bound: dict[str, dict]) -> str:
    rows = ["| Tunable | Value | Set in | Adjustable |", "|---|---|---|---|"]
    for tid in declared:
        b = bound.get(tid)
        if not b:
            rows.append(f"| `{tid}` | \u2014 | \u2014 | **not bound** |")
            continue
        adjustable = "yes" if b["kind"] == "@export" else ("no \u2014 `const`" if b["kind"] == "const" else "code only")
        value = f"`{b['value']}`" if b["value"] else "\u2014"
        where = f"`{b['file']}:{b['line']}`"
        rows.append(f"| `{tid}` | {value} | {where} | {adjustable} |")
    notes = [f"- `{t}` \u2014 {bound[t]['note']}" for t in declared if bound.get(t) and bound[t]["note"]]
    out = "\n".join(rows)
    if notes:
        out += "\n\n" + "\n".join(notes)
    return out


def write_bindings(path: Path, declared: list[str], bound: dict[str, dict]) -> bool:
    raw = path.read_text(encoding="utf-8", errors="replace")
    block = f"{BIND_BEGIN}\n\n{render_bindings(declared, bound)}\n\n{BIND_END}"
    if BIND_BEGIN in raw and BIND_END in raw:
        head, rest = raw.split(BIND_BEGIN, 1)
        _, tail = rest.split(BIND_END, 1)
        new = head + block + tail
    else:
        if not declared:
            return False
        tun = re.compile(r"(^##\s+Tunables\s*$.*?)(?=^##\s|\Z)", re.M | re.S | re.I)
        m = tun.search(raw)
        if not m:
            return False
        new = raw[: m.end(1)].rstrip() + "\n\n" + block + "\n\n" + raw[m.end(1):].lstrip()
    if new != raw:
        path.write_text(new, encoding="utf-8", newline="")
        return True
    return False


def ready_to_work(docs: list[dict], bound: dict[str, dict]) -> list[tuple[str, str]]:
    """Derive candidate next work from the design body alone."""
    out = []
    for d in docs:
        unbound = [t for t in d["declared_tunables"] if t not in bound]
        if d["resolution"] == "settled" and unbound:
            if len(unbound) == len(d["declared_tunables"]):
                out.append((d["path"], f"settled, nothing built \u2014 {len(unbound)} tunable(s) unbound"))
            else:
                out.append((d["path"], f"partly built \u2014 {len(unbound)} of {len(d['declared_tunables'])} tunable(s) unbound"))
        elif d["resolution"] == "question":
            settle = "what would settle it" in [h.lower() for h in d["headings"]]
            out.append((d["path"], "open question" + (" \u2014 has a stated way to settle it" if settle else "")))
        elif d["resolution"] == "direction":
            out.append((d["path"], "leaning, not committed \u2014 needs confirming or ruling out"))
    return out


def build_index(docs: list[dict], bound: dict[str, dict], undeclared: list[str]) -> str:
    lines = [
        "# Design index",
        "",
        "Generated by `tools/design.py`. Do not edit.",
        "",
        "One line per section. Read this to find the right document, then open only",
        "that document. Do not load the whole design body into context.",
        "",
    ]

    if not docs:
        lines += [
            "## Sections",
            "",
            "Nothing yet. Sections are created when there is a real question to record,",
            "at whatever resolution they have. See `README.md` in this folder.",
            "",
        ]
    else:
        lines += ["## Sections", "", "| Section | Resolution | What it is about |", "|---|---|---|"]
        for d in docs:
            summary = d["summary"] or "_no intent stated_"
            lines.append(f"| [{d['title']}]({d['path']}) | {d['resolution']} | {summary} |")
        lines.append("")

    ready = ready_to_work(docs, bound)
    lines += ["## Ready to work on", ""]
    if ready:
        lines.append("Derived from resolution and binding state, not a stored backlog.")
        lines.append("")
        for path, why in ready:
            lines.append(f"- `{path}` \u2014 {why}")
    else:
        lines.append("Nothing derivable. Either the body is empty or every settled section is bound.")
    lines.append("")

    all_declared = sorted({t for d in docs for t in d["declared_tunables"]})
    if all_declared or undeclared:
        lines += ["## Tunables", ""]
        if all_declared:
            lines += ["| Tunable | Declared in | Bound |", "|---|---|---|"]
            owner = {t: d["path"] for d in docs for t in d["declared_tunables"]}
            for t in all_declared:
                b = bound.get(t)
                where = f"`{b['file']}:{b['line']}`" if b else "**not bound**"
                lines.append(f"| `{t}` | `{owner[t]}` | {where} |")
            lines.append("")
        if undeclared:
            lines.append("Claimed in code but declared in no section:")
            lines.append("")
            for t in undeclared:
                b = bound[t]
                lines.append(f"- `{t}` at `{b['file']}:{b['line']}` \u2014 a knob nobody designed")
            lines.append("")

    edges = [(d["path"], t) for d in docs for t in d["links"]]
    if edges:
        lines += ["## How sections reference each other", "", "```mermaid", "graph LR"]
        ids: dict[str, str] = {}

        def nid(p: str) -> str:
            if p not in ids:
                ids[p] = "d%d" % len(ids)
            return ids[p]

        titles = {d["path"]: d["title"] for d in docs}
        for src, dst in sorted(set(edges)):
            lines.append(f'    {nid(src)}["{titles.get(src, src)}"] --> {nid(dst)}["{titles.get(dst, dst)}"]')
        lines += ["```", ""]

    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Regenerate the design index and code bindings.")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--check", action="store_true", help="report only, write nothing")
    args = ap.parse_args()

    files = design_files()
    docs = [parse_design(p) for p in files]
    bound = scan_tunables()

    declared_all = {t for d in docs for t in d["declared_tunables"]}
    undeclared = sorted(t for t in bound if t not in declared_all)
    unbound = sorted(t for t in declared_all if t not in bound)
    unstated = [d["path"] for d in docs if d["resolution"] not in RESOLUTIONS]
    const_bound = sorted(t for t in declared_all if bound.get(t, {}).get("kind") == "const")

    wrote = []
    if not args.check:
        for p, d in zip(files, docs):
            if d["declared_tunables"] or BIND_BEGIN in p.read_text(encoding="utf-8", errors="replace"):
                if write_bindings(p, d["declared_tunables"], bound):
                    wrote.append(d["path"])
        DESIGN.mkdir(parents=True, exist_ok=True)
        idx = DESIGN / INDEX_NAME
        new = build_index([parse_design(p) for p in files], bound, undeclared)
        if not idx.exists() or idx.read_text(encoding="utf-8", errors="replace") != new:
            idx.write_text(new, encoding="utf-8", newline="")
            wrote.append(f"docs/design/{INDEX_NAME}")

    if args.json:
        print(json.dumps({
            "sections": docs,
            "bound": bound,
            "unbound": unbound,
            "undeclared": undeclared,
            "unstated_resolution": unstated,
            "const_bound": const_bound,
            "ready": ready_to_work(docs, bound),
            "rewrote": wrote,
        }, indent=2))
        return 0

    print(f"design: {len(docs)} section(s), {len(declared_all)} tunable(s) declared, {len(bound)} claimed in code")
    for w in wrote:
        print(f"  regenerated {w}")
    for t in unbound:
        print(f"  note: `{t}` declared in design, claimed by no code")
    for t in undeclared:
        print(f"  note: `{t}` claimed at {bound[t]['file']}:{bound[t]['line']}, declared in no section")
    for t in const_bound:
        print(f"  note: `{t}` is design-declared as tunable but bound to a const, so it cannot be adjusted")
    for p in unstated:
        print(f"  note: {p} states no resolution line")
    return 0


if __name__ == "__main__":
    sys.exit(main())
