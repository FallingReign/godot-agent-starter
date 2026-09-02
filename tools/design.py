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
import hashlib
import json
import re
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
CORE_ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))
import project_context  # noqa: E402

_INSTALLATION = project_context.resolve_active_installation(CORE_ROOT)
ROOT = _INSTALLATION.project_root  # compatibility name for project-owned paths
DESIGN = ROOT / "docs" / "design"

INDEX_NAME = "INDEX.md"
SKIP_FILES = {"README.md", INDEX_NAME}

BIND_BEGIN = "<!-- BEGIN GENERATED BINDINGS -->"
BIND_END = "<!-- END GENERATED BINDINGS -->"

RE_TITLE = re.compile(r"^#\s+(.+?)\s*$", re.M)
RE_RESOLUTION = re.compile(r"^_Resolution:\s*([^_\r\n]+?)\s*_\s*$", re.M | re.I)
RE_AUTHORITY = re.compile(r"^_Authority:\s*([^_\r\n]+?)\s*_\s*$", re.M | re.I)
RE_AUTHORED_BY = re.compile(r"^_Authored by:\s*([^_\r\n]+?)\s*_\s*$", re.M | re.I)
RE_CONFIDENCE = re.compile(r"^_Confidence:\s*([^_\r\n]+?)\s*_\s*$", re.M | re.I)
RE_H2 = re.compile(r"^##\s+(.+?)\s*$", re.M)
RE_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)#\s]+\.md)(?:#[^)]*)?\)")
RE_TUNE_ROW = re.compile(r"^\|\s*`([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)`\s*\|", re.M | re.I)
RE_TUNE_ANNOT = re.compile(r"^\s*##\s*@tune\s+([a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+)\s*$", re.I)
RE_DECL = re.compile(
    r"^\s*(?:@export(?:_[a-z_]+)?(?:\([^)]*\))?\s+)?(const|static\s+var|var)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*[^=]+?)?\s*(?::?=)\s*(.+?)\s*$"
)

RESOLUTIONS = ("question", "direction", "settled")
AUTHORITIES = ("human-confirmed", "agent-provisional")
AUTHORS = ("human", "agent")
CONFIDENCES = ("very-high", "high", "medium", "low")
AGENT_SECTIONS = (
    "Quick read", "Why this inference", "Assumptions", "Veto and go/no-go",
)
EXCLUDED_GAME_DIRS = {
    ".agents", ".checklogs", ".git", ".github", ".godot", ".godot_doc",
    ".kit", "addons", "docs", "tests", "tools",
}


def _configured_game_root() -> Path:
    """Resolve the game only when a design operation actually needs it.

    An extracted release is a valid pre-installation controller even though it
    deliberately contains no game.  Pure design parsing is also used by that
    controller, so importing this module must not require game files.
    """
    context = project_context.load_active_context(CORE_ROOT)
    if context.project_root != ROOT:
        raise project_context.ProjectContextError(
            "active design project changed after startup"
        )
    return context.game_root


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


def active_markdown_text(text: str) -> str:
    """Project only authored Markdown that is active, preserving byte offsets.

    Metadata, required headings and labels inside HTML comments or fenced code
    are examples, not design authority. Replacing inactive characters with
    spaces (while preserving line endings) lets every existing regex retain
    exact spans without allowing those examples to satisfy the contract.
    """
    chars = list(text)

    def blank(start: int, end: int) -> None:
        for index in range(start, end):
            if chars[index] not in ("\r", "\n"):
                chars[index] = " "

    for match in re.finditer(r"<!--.*?(?:-->|\Z)", text, re.S):
        blank(match.start(), match.end())

    offset = 0
    fence_character = ""
    fence_width = 0
    for line in text.splitlines(keepends=True):
        content_length = len(line.rstrip("\r\n"))
        projected = "".join(chars[offset:offset + content_length])
        stripped = projected.lstrip(" \t")
        fence = re.match(r"(`{3,}|~{3,})", stripped)
        if not fence_character:
            if fence is not None:
                marker = fence.group(1)
                fence_character = marker[0]
                fence_width = len(marker)
                blank(offset, offset + len(line))
        else:
            closing = re.match(
                re.escape(fence_character) + "{" + str(fence_width) + r",}\s*$",
                stripped,
            )
            blank(offset, offset + len(line))
            if closing is not None:
                fence_character = ""
                fence_width = 0
        offset += len(line)
    return "".join(chars)


def section_body(text: str, heading: str) -> str:
    """Text under a given h2, up to the next h2."""
    pat = re.compile(r"^##\s+" + re.escape(heading) + r"\s*$(.*?)(?=^##\s|\Z)", re.M | re.S | re.I)
    m = pat.search(text)
    return m.group(1).strip() if m else ""


def canonical_design_text(text: str) -> str:
    """Return the stable design bytes that proposals approve and cite.

    Generated binding tables are a view over code and must not invalidate a
    design approval. Line endings and terminal newlines are normalized so the
    digest is portable across Windows, macOS and Linux; authored content is not
    otherwise rewritten.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if BIND_BEGIN in text and BIND_END in text:
        head, rest = text.split(BIND_BEGIN, 1)
        _, tail = rest.split(BIND_END, 1)
        head = head.rstrip("\n")
        tail = tail.lstrip("\n")
        text = head + ("\n\n" if tail else "\n") + tail
    return text.rstrip("\n") + "\n"


def strip_generated(text: str) -> str:
    """Compatibility name for consumers that need the authored design only."""
    return canonical_design_text(text)


def design_sha256(text: str) -> str:
    return hashlib.sha256(canonical_design_text(text).encode("utf-8")).hexdigest()


def _metadata_value(pattern: re.Pattern[str], text: str) -> str:
    match = pattern.search(text)
    return match.group(1).strip().lower() if match else "unstated"


def _has_label(body: str, label: str) -> bool:
    pattern = (
        r"(?:^|\n)\s*(?:[-*]\s*)?(?:\*\*)?" + re.escape(label)
        + r"(?:\*\*)?\s*:"
    )
    return bool(re.search(pattern, body, re.I))


def parse_design(path: Path, *, design_root: Path | None = None) -> dict:
    raw = path.read_text(encoding="utf-8", errors="replace")
    text = canonical_design_text(raw)
    active = active_markdown_text(text)
    rel = path.relative_to(design_root or DESIGN).as_posix()

    tm = RE_TITLE.search(active)
    title = tm.group(1).strip() if tm else path.stem.replace("-", " ").capitalize()

    resolution = _metadata_value(RE_RESOLUTION, active)
    authority = _metadata_value(RE_AUTHORITY, active)
    authored_by = _metadata_value(RE_AUTHORED_BY, active)
    confidence = _metadata_value(RE_CONFIDENCE, active)

    metadata_errors: list[str] = []
    metadata_warnings: list[str] = []
    metadata_started = bool(re.search(
        r"^_(?:Authority|Authored by|Confidence):", active, re.M | re.I
    ))
    resolution_lines = len(re.findall(r"^_Resolution:", active, re.M | re.I))

    if metadata_started:
        nonempty = [line.strip() for line in active.splitlines() if line.strip()]
        expected_patterns = (
            RE_TITLE,
            RE_RESOLUTION,
            RE_AUTHORITY,
            RE_AUTHORED_BY,
            RE_CONFIDENCE,
        )
        positioned = len(nonempty) >= len(expected_patterns) and all(
            pattern.fullmatch(nonempty[index]) is not None
            for index, pattern in enumerate(expected_patterns)
        )
        if not positioned:
            metadata_errors.append(
                "Design metadata must be one complete block immediately below the single title"
            )
        if len(RE_TITLE.findall(active)) != 1:
            metadata_errors.append("Design title must appear exactly once")

    if resolution_lines > 1:
        metadata_errors.append("Resolution metadata must appear exactly once")
    elif resolution == "unstated" and resolution_lines == 1:
        metadata_errors.append("Resolution metadata is malformed")
    elif resolution == "unstated":
        metadata_warnings.append("legacy section has no Resolution metadata")
    elif resolution not in RESOLUTIONS:
        metadata_errors.append(
            f"Resolution must be one of {', '.join(RESOLUTIONS)} (got {resolution!r})"
        )

    if metadata_started:
        for label, value, allowed, pattern in (
            ("Authority", authority, AUTHORITIES, RE_AUTHORITY),
            ("Authored by", authored_by, AUTHORS, RE_AUTHORED_BY),
            ("Confidence", confidence, CONFIDENCES, RE_CONFIDENCE),
        ):
            line_count = len(re.findall(
                r"^_" + re.escape(label) + r":", active, re.M | re.I
            ))
            valid_count = len(pattern.findall(active))
            if line_count == 0:
                metadata_errors.append(f"{label} is required when design metadata is present")
            elif line_count > 1:
                metadata_errors.append(f"{label} metadata must appear exactly once")
            elif valid_count != 1:
                metadata_errors.append(f"{label} metadata is malformed")
            elif value not in allowed:
                metadata_errors.append(
                    f"{label} must be one of {', '.join(allowed)} (got {value!r})"
                )
    else:
        metadata_warnings.append(
            "legacy section has no Authority, Authored by or Confidence metadata"
        )

    if authority == "agent-provisional" and authored_by != "agent":
        metadata_errors.append("agent-provisional design must disclose Authored by as agent")

    intent = section_body(active, "Intent")
    if not intent:
        # fall back to the first paragraph after the title
        after = active[tm.end():] if tm else active
        after = re.sub(
            r"^_(?:Resolution|Authority|Authored by|Confidence):[^\n]*\n",
            "",
            after.lstrip(),
            flags=re.M | re.I,
        )
        intent = after.strip().split("\n\n")[0] if after.strip() else ""

    headings = [h for h in RE_H2.findall(active)]
    if authored_by == "agent" or authority == "agent-provisional":
        for heading in AGENT_SECTIONS:
            if not section_body(active, heading):
                metadata_errors.append(
                    f"agent-authored design requires a non-empty '## {heading}' section"
                )
        quick_read = section_body(active, "Quick read")
        for label in ("Player does", "Player experiences", "Successful outcome"):
            if quick_read and not _has_label(quick_read, label):
                metadata_errors.append(
                    f"Quick read requires a '{label}:' line"
                )
        veto = section_body(active, "Veto and go/no-go")
        for label in ("Veto scope", "Next go/no-go"):
            if veto and not _has_label(veto, label):
                metadata_errors.append(
                    f"Veto and go/no-go requires a '{label}:' line"
                )

    metadata_complete = (
        resolution in RESOLUTIONS
        and authority in AUTHORITIES
        and authored_by in AUTHORS
        and confidence in CONFIDENCES
        and not metadata_errors
    )
    implementation_eligible = (
        metadata_complete
        and resolution == "settled"
        and (
            authority == "human-confirmed"
            or (
                authority == "agent-provisional"
                and authored_by == "agent"
                and confidence == "very-high"
            )
        )
    )
    if authority == "agent-provisional" and confidence in CONFIDENCES \
            and confidence != "very-high":
        metadata_warnings.append(
            "agent-provisional design is not implementation-eligible below very-high confidence"
        )
    declared = sorted(set(RE_TUNE_ROW.findall(section_body(active, "Tunables"))))

    links = []
    for target in RE_MD_LINK.findall(active):
        resolved = (path.parent / target).resolve()
        try:
            links.append(resolved.relative_to(DESIGN.resolve()).as_posix())
        except ValueError:
            continue

    return {
        "path": rel,
        "title": title,
        "resolution": resolution,
        "authority": authority,
        "authored_by": authored_by,
        "confidence": confidence,
        "sha256": design_sha256(raw),
        "implementation_eligible": implementation_eligible,
        "metadata_errors": metadata_errors,
        "metadata_warnings": metadata_warnings,
        "summary": first_sentence(intent),
        "why_inference": section_body(active, "Why this inference"),
        "assumptions": section_body(active, "Assumptions"),
        "quick_read": section_body(active, "Quick read"),
        "veto_and_go_no_go": section_body(active, "Veto and go/no-go"),
        "headings": headings,
        "declared_tunables": declared,
        "links": sorted(set(links)),
        "has_constraints": bool(section_body(active, "Constraints it imposes")),
    }


def replace_active_metadata(
    text: str, label: str, expected: str, replacement: str
) -> str:
    """Replace one validated active metadata value without touching examples."""
    projection = active_markdown_text(text)
    pattern = re.compile(
        r"(?mi)^_" + re.escape(label) + r":\s*" + re.escape(expected) + r"_\s*$"
    )
    matches = list(pattern.finditer(projection))
    if len(matches) != 1:
        raise ValueError(f"active {label} metadata is missing or duplicated")
    match = matches[0]
    original = text[match.start():match.end()]
    value = re.sub(
        re.escape(expected), replacement, original, count=1, flags=re.I
    )
    return text[:match.start()] + value + text[match.end():]


def scan_tunables() -> dict[str, dict]:
    """Find `## @tune <id>` claims in GDScript."""
    found: dict[str, dict] = {}
    game_root = _configured_game_root()
    if not game_root.is_dir():
        return found
    for gd in sorted(game_root.rglob("*.gd")):
        if EXCLUDED_GAME_DIRS.intersection(gd.relative_to(game_root).parts):
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
        if d["resolution"] == "settled" and d["implementation_eligible"] and unbound:
            authority_note = (
                "agent-provisional; reversible work only; "
                if d["authority"] == "agent-provisional" else ""
            )
            if len(unbound) == len(d["declared_tunables"]):
                out.append((d["path"], f"settled, {authority_note}nothing built \u2014 {len(unbound)} tunable(s) unbound"))
            else:
                out.append((d["path"], f"{authority_note}partly built \u2014 {len(unbound)} of {len(d['declared_tunables'])} tunable(s) unbound"))
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
            "at whatever resolution they have. See [docs/DESIGN.md](../DESIGN.md).",
            "",
        ]
    else:
        lines += [
            "## Sections", "",
            "| Section | Resolution | Authority | What it is about |",
            "|---|---|---|---|",
        ]
        for d in docs:
            summary = d["summary"] or "_no intent stated_"
            authority = d["authority"]
            if authority == "unstated":
                authority = "legacy / unstated"
            lines.append(
                f"| [{d['title']}]({d['path']}) | {d['resolution']} | "
                f"{authority} | {summary} |"
            )
        lines.append("")

    needs_authority = [
        d for d in docs
        if d["resolution"] == "settled" and not d["implementation_eligible"]
    ]
    if needs_authority:
        lines += ["## Needs design authority", ""]
        for d in needs_authority:
            reason = "; ".join(d["metadata_errors"] + d["metadata_warnings"])
            lines.append(f"- `{d['path']}` \u2014 {reason or 'not implementation-eligible'}")
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
    metadata_errors = [
        f"{d['path']}: {message}"
        for d in docs for message in d["metadata_errors"]
    ]
    metadata_warnings = [
        f"{d['path']}: {message}"
        for d in docs for message in d["metadata_warnings"]
    ]
    const_bound = sorted(t for t in declared_all if bound.get(t, {}).get("kind") == "const")

    wrote = []
    if not args.check and not metadata_errors:
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
            "metadata_errors": metadata_errors,
            "metadata_warnings": metadata_warnings,
            "const_bound": const_bound,
            "ready": ready_to_work(docs, bound),
            "rewrote": wrote,
        }, indent=2))
        return 1 if metadata_errors else 0

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
    for message in metadata_warnings:
        print(f"  note: {message}")
    for message in metadata_errors:
        print(f"  error: {message}")
    return 1 if metadata_errors else 0


if __name__ == "__main__":
    sys.exit(main())
