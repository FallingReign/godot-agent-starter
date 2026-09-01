#!/usr/bin/env python3
"""Render the living plan as one HTML page for a human to read and talk about.

GENERATED, never authored. Five sources, all machine-readable:

    project.shape.json   decisions (settled), direction (heading), questions
    proposal.json        the approved structure for the current slice
    arch.py --json       the real module graph, derived from code
    git                  what changed recently, and since approval
    referenced docs/design/  only the player-experience sections this slice cites

Referenced design is rendered to HTML at generation time and inlined, not fetched.
file:// blocks fetch() in Chrome and Edge, so a plan opened by double-clicking
cannot load a sibling .md at all. Inlining is the only approach that works for
the way this page is actually opened.

The page holds nothing of its own, so overwriting it is free. Anything it shows
is either intent (from a json file a human approved) or reality (from code and
git). It never invents a third thing.

Public generation is `kit plan`. The optional internal `--slice` mode writes a
review snapshot in addition to the living page.

Snapshots exist so a slice that goes wrong can be compared against the plan as
it stood when the slice started, rather than as it stands after the damage.

Diagrams use inert local markup, open questions retain stable decision IDs, and
all paths are relative so the page works directly from disk or through the
capability-protected loopback board without an external wrapper.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import md as markdown  # noqa: E402  (sibling module, not a package)
import board_client  # noqa: E402  (shared generated-page navigation)
import cockpit  # noqa: E402  (shared decision and verification view)
import authored_scope  # noqa: E402  (shared inverse authored-game classifier)
import gd_signature  # noqa: E402  (shared canonical GDScript signatures)
import design as design_contract  # noqa: E402  (canonical design digests)
import proposal_authority  # noqa: E402  (exact current/historical approval)
import schema as artifact_schema  # noqa: E402  (shared artefact contract)
import project_context  # noqa: E402  (shared configured roots)
import release as kit_release  # noqa: E402  (authoritative fixed kit paths)

TOOLS = Path(__file__).resolve().parent
CORE_ROOT = TOOLS.parent
CONTEXT = project_context.load_active_context(CORE_ROOT)
ROOT = CONTEXT.project_root  # compatibility name for project-owned paths
GAME_ROOT = CONTEXT.game_root
SHAPE = ROOT / "project.shape.json"
PROPOSAL = ROOT / "proposal.json"
DOCS = ROOT / "docs"
DESIGN = DOCS / "design"
VENDOR = CORE_ROOT / "tools" / "vendor"
OUT = ROOT / "plan.html"
SNAP_DIR = ROOT / "plan"

DEPTH = {"hands-off": 0, "module": 1, "file": 2, "function": 3}


class ArchitectureGraphError(RuntimeError):
    """The implementation graph could not be proven from current source."""


def load(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def artefact_errors() -> List[Tuple[str, str]]:
    """Return schema failures before an invalid plan can replace a valid one."""
    found: List[Tuple[str, str]] = []
    for path, spec in ((PROPOSAL, "PROPOSAL"), (SHAPE, "SHAPE")):
        errors, _warnings = artifact_schema.validate(path, spec)
        found.extend((path.name, error) for error in errors)
    return found


def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""))


# An SVG mockup is authored by the agent and rendered unescaped in the human's
# browser, so it is an injection surface: <script>, event handlers and
# <foreignObject><iframe> all execute. Allowlist the shape-drawing subset and
# drop everything else rather than trying to blocklist attacks.
SVG_TAGS = {"lineargradient": "linearGradient",
            "radialgradient": "radialGradient",
            "clippath": "clipPath", "textpath": "textPath"}
SVG_TAGS.update({k: k for k in (
    "svg", "g", "defs", "title", "desc", "rect", "circle", "ellipse", "line",
    "polyline", "polygon", "path", "text", "tspan", "use", "symbol",
    "stop", "pattern", "marker",
)})
# SVG attribute names are case-sensitive in the DOM: `viewbox` is silently
# ignored while `viewBox` scales the drawing, so matching is done in lowercase
# and the canonical spelling is written back out.
SVG_ATTRS = {
    "viewbox": "viewBox", "preserveaspectratio": "preserveAspectRatio",
    "gradientunits": "gradientUnits", "patternunits": "patternUnits",
    "gradienttransform": "gradientTransform", "clippathunits": "clipPathUnits",
    "markerwidth": "markerWidth", "markerheight": "markerHeight",
    "refx": "refX", "refy": "refY",
}
SVG_ATTRS.update({k: k for k in (
    "width", "height", "x", "y", "x1", "y1", "x2", "y2", "cx", "cy",
    "r", "rx", "ry", "d", "points", "fill", "stroke", "stroke-width",
    "stroke-dasharray", "stroke-linecap", "stroke-linejoin", "opacity",
    "fill-opacity", "stroke-opacity", "transform", "font-size", "font-family",
    "font-weight", "text-anchor", "dominant-baseline", "id", "class",
    "offset", "stop-color", "stop-opacity", "clip-path", "xmlns",
    "dx", "dy", "fill-rule", "letter-spacing", "vector-effect",
)})


def clean_svg(raw: str) -> str:
    """Strip an agent-authored SVG down to inert shape markup, or return ""."""
    src = raw.strip()
    if not src.lower().startswith("<svg"):
        return ""
    # Comments and CDATA can hide markup from a naive tag scan.
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"<!\[CDATA\[.*?\]\]>", "", src, flags=re.S)
    # Drop disallowed elements including their content, so <script>..</script>
    # does not leave its body behind as text.
    src = re.sub(r"<\s*(script|style|foreignObject|iframe|object|embed|animate"
                 r"|set|handler|image)\b.*?<\s*/\s*\1\s*>", "", src,
                 flags=re.S | re.I)
    src = re.sub(r"<\s*/?\s*(script|style|foreignObject|iframe|object|embed"
                 r"|animate|set|handler|image)\b[^>]*>", "", src, flags=re.I)

    def fix_tag(m: "re.Match[str]") -> str:
        closing, raw_name, body = m.group(1), m.group(2), m.group(3) or ""
        name = SVG_TAGS.get(raw_name.lower(), "")
        if not name:
            return ""
        if closing:
            return f"</{name}>"
        kept = []
        for am in re.finditer(
                r"([A-Za-z_:][-\w:.]*)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)",
                body):
            attr = SVG_ATTRS.get(am.group(1).lower(), "")
            val = am.group(2).strip("\"'")
            if not attr:
                continue
            low = val.lower().replace(" ", "").replace("\t", "")
            if "javascript:" in low or "data:" in low or "url(" in low:
                continue
            kept.append(f'{attr}="{val.replace(chr(34), "&quot;")}"')
        selfclose = "/" if body.rstrip().endswith("/") else ""
        return f"<{name}{(' ' + ' '.join(kept)) if kept else ''}{selfclose}>"

    src = re.sub(r"<\s*(/?)\s*([A-Za-z_:][-\w:.]*)([^>]*)>", fix_tag, src)
    # Anything left that still looks like an event handler is dropped outright.
    if re.search(r"\son[a-z]+\s*=", src, flags=re.I):
        return ""
    return src if src.lower().startswith("<svg") else ""


def rows(data: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
    """List items, minus template examples.

    An item with an "_" key documents the field shape inside the template. It
    must never render as a real proposed module, or a fresh clone shows a plan
    for work nobody proposed.
    """
    items = data.get(key)
    if not isinstance(items, list):
        return []
    return [i for i in items
            if isinstance(i, dict)
            and not any(k.startswith("_") for k in i)]


def question_relation(question: Dict[str, Any], proposal: Dict[str, Any]) -> str:
    """Classify an open question against the current proposal.

    Relation fields are optional so existing project shapes remain readable.
    Their absence is surfaced as legacy/unscoped rather than interpreted as a
    relation to every future slice.
    """
    related_slices = {
        item.strip()
        for item in question.get("related_slices", [])
        if isinstance(item, str) and item.strip()
    } if isinstance(question.get("related_slices"), list) else set()
    related_design_refs = {
        item.strip().replace("\\", "/")
        for item in question.get("related_design_refs", [])
        if isinstance(item, str) and item.strip()
    } if isinstance(question.get("related_design_refs"), list) else set()
    if not related_slices and not related_design_refs:
        return "legacy"
    active_slice = str(proposal.get("slice") or "").strip()
    active_design_refs = {
        str(item.get("section") or "").strip().replace("\\", "/")
        for item in rows(proposal, "design_refs")
        if item.get("section")
    }
    if (
        (active_slice and active_slice in related_slices)
        or bool(active_design_refs.intersection(related_design_refs))
    ):
        return "related"
    return "other"


def git(*args: str) -> Tuple[int, str]:
    if not shutil.which("git"):
        return 1, ""
    try:
        p = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                           text=True, timeout=20)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def is_authored_game_file(rel: str) -> bool:
    """Return whether a ``res://``-relative path is authored game content."""
    return authored_scope.is_authored_game_file(
        rel,
        game_layout=CONTEXT.game_layout,
        is_kit_file=kit_release.is_allowlisted,
    )


def real_files() -> set:
    """Authored game files relative to ``res://``; kit state is excluded."""
    out = set()
    if not GAME_ROOT.is_dir():
        return out
    for directory, names, files in os.walk(GAME_ROOT):
        base = Path(directory)
        relative_base = base.relative_to(GAME_ROOT).as_posix()
        prefix = "" if relative_base == "." else relative_base + "/"
        names[:] = [
            name for name in names
            if is_authored_game_file(prefix + name + "/placeholder")
        ]
        for name in files:
            rel = prefix + name
            if is_authored_game_file(rel):
                out.add(rel)
    return out


def authored_modules(files: set[str]) -> set[str]:
    """Module existence comes from every authored input, not only code edges."""
    try:
        rules = json.loads((ROOT / "arch.rules.json").read_text(encoding="utf-8"))
        depth = max(1, int(rules.get("module_depth", 2)))
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError):
        depth = 2
    modules: set[str] = set()
    for relative in files:
        parent = str(Path(relative).parent).replace("\\", "/")
        if parent in ("", "."):
            modules.add("(root)")
        else:
            modules.add("/".join(parent.split("/")[:depth]))
    return modules


def touched_since(baseline: str) -> set | None:
    """Game files changed since the approval sha, or ``None`` if unknown.

    Without this, every file that predates the slice reads as "unproposed",
    which buries the two or three that genuinely are. Untracked files must be
    included: new work is usually untracked, and omitting it scopes the view to
    nothing while appearing to be complete.
    """
    if not baseline or not shutil.which("git"):
        return None
    pathspec = CONTEXT.git_pathspec
    code, tracked = git("diff", "--name-only", baseline, "--", pathspec)
    if code != 0:
        return None
    untracked_code, untracked = git(
        "ls-files", "--others", "--exclude-standard", "--", pathspec
    )
    if untracked_code != 0:
        return None
    out = set()
    for line in (tracked + "\n" + untracked).splitlines():
        line = line.strip().replace("\\", "/")
        rel = CONTEXT.game_relative(line)
        if rel is None:
            continue
        if is_authored_game_file(rel):
            out.add(rel)
    return out


def files_at_baseline(baseline: str) -> set | None:
    """Authored game files present at the proposal baseline.

    The hands-off cockpit must classify each changed path as new, modified or
    deleted using the same baseline comparison as conformance.  Containment
    alone is not implementation authority because scope boundaries own an
    exact action as well as a path.
    """
    if not baseline or not shutil.which("git"):
        return None
    code, listing = git(
        "ls-tree", "-r", "--name-only", baseline, "--", CONTEXT.git_pathspec
    )
    if code != 0:
        return None
    files: set[str] = set()
    for line in listing.splitlines():
        relative = CONTEXT.game_relative(line.strip().replace("\\", "/"))
        if relative is not None and is_authored_game_file(relative):
            files.add(relative)
    return files


def function_changes_since(
    baseline: str,
    changed_files: set[str] | None,
    baseline_files: set[str] | None,
    present_files: set[str],
) -> Dict[Tuple[str, str], Dict[str, str]] | None:
    """Return exact baseline/current function evidence, including body-only edits.

    File status cannot establish which function changed.  The architecture map
    therefore uses the same baseline/current comparison as conformance instead
    of colouring every function in a modified file or, worse, calling all of
    them unchanged.  ``None`` means the comparison could not be proven.
    """
    if not baseline or changed_files is None or baseline_files is None:
        return None
    changes: Dict[Tuple[str, str], Dict[str, str]] = {}
    for relative in sorted(
        path for path in changed_files if path.lower().endswith(".gd")
    ):
        before: Dict[str, gd_signature.SourceFunction] = {}
        current: Dict[str, gd_signature.SourceFunction] = {}
        if relative in baseline_files:
            repository_path = (
                relative
                if CONTEXT.game_layout == "."
                else f"{CONTEXT.game_layout}/{relative}"
            )
            code, source = git("show", f"{baseline}:{repository_path}")
            if code != 0:
                return None
            try:
                before = gd_signature.parse_source_functions(source)
            except gd_signature.SignatureError:
                return None
        if relative in present_files:
            try:
                source = (GAME_ROOT / relative).read_text(encoding="utf-8")
                current = gd_signature.parse_source_functions(source)
            except (OSError, UnicodeError, gd_signature.SignatureError):
                return None
        for identity in sorted(set(before) | set(current)):
            old = before.get(identity)
            new = current.get(identity)
            action = ""
            if old is None and new is not None:
                action = "new"
            elif old is not None and new is None:
                action = "delete"
            elif (
                old is not None
                and new is not None
                and (
                    old.signature != new.signature
                    or old.body_sha256 != new.body_sha256
                )
            ):
                action = "modify"
            changes[(relative, identity)] = {
                "action": action,
                "signature": (
                    new.signature if new is not None else old.signature if old else ""
                ),
                "baseline_signature": old.signature if old is not None else "",
                "current_signature": new.signature if new is not None else "",
            }
    return changes


def module_graph() -> Tuple[List[Dict[str, Any]], str, Dict[str, Any]]:
    """Real modules and a mermaid diagram, from arch.py. Never hand-derived:
    arch.py already owns the definition of a module and the gate already fails
    when its output is stale."""
    script = CORE_ROOT / "arch.py"
    if not script.is_file():
        return [], "", {}
    try:
        p = subprocess.run([sys.executable, str(script), "--json"],
                           capture_output=True, text=True, timeout=60)
        data = json.loads(p.stdout) if p.stdout.strip() else {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ArchitectureGraphError(
            f"arch.py did not return a readable graph: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ArchitectureGraphError("arch.py returned a non-object graph")
    errors = data.get("errors")
    if isinstance(errors, list) and errors:
        detail = "; ".join(str(error) for error in errors if str(error).strip())
        raise ArchitectureGraphError(detail or "arch.py reported a source error")
    if getattr(p, "returncode", 0) != 0 and not data.get("modules"):
        detail = str(getattr(p, "stderr", "") or "").strip()
        raise ArchitectureGraphError(
            detail or f"arch.py exited {p.returncode} without a graph"
        )
    # arch.py emits modules as {path: [dependencies]}.
    raw = data.get("modules")
    mods: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        for path, deps in sorted(raw.items()):
            mods.append({"path": path,
                         "depends_on": deps if isinstance(deps, list) else []})
    tree = data.get("tree")
    return mods, str(data.get("mermaid", "") or ""), (tree if isinstance(tree, dict) else {})


def recent(limit: int = 12) -> List[Dict[str, str]]:
    """Recent commits with repository-relative paths shown as inert references."""
    code, out = git("log", f"-{limit}", "--pretty=format:%h%x1f%ad%x1f%s",
                    "--date=short", "--name-only")
    if code != 0 or not out.strip():
        return []
    items: List[Dict[str, str]] = []
    cur: Dict[str, Any] | None = None
    for line in out.splitlines():
        if "\x1f" in line:
            if cur:
                items.append(cur)
            sha, date, subject = (line.split("\x1f") + ["", ""])[:3]
            cur = {"sha": sha, "date": date, "subject": subject, "files": []}
        elif line.strip() and cur is not None:
            cur["files"].append(line.strip())
    if cur:
        items.append(cur)
    return items


def read_docs(folder: Path, prefix: str,
              skip: Optional[Path] = None,
              targets: Optional[Dict[str, str]] = None,
              only: Optional[set[str]] = None) -> List[Dict[str, str]]:
    """Documentation, with its body pre-rendered to HTML and inlined.

    Rendered here rather than fetched in the browser because file:// forbids
    fetch(): a plan opened by double-clicking could not load a sibling .md.
    Inlining costs a larger file and buys a page that works when opened the way
    people actually open it.
    """
    out: List[Dict[str, str]] = []
    if not folder.is_dir():
        return out
    for f in sorted(folder.rglob("*.md")):
        # A README explains what the folder is for. Listing it as content makes
        # an empty design folder look populated, which is the opposite of what
        # this section is for. INDEX.md is a generated retrieval aid rendered
        # separately above, so listing it here would duplicate it.
        if f.name.lower() in ("readme.md", "index.md"):
            continue
        # docs/ now globs recursively, so without this the design body would be
        # swept into the kit documentation as well and every section would render
        # twice with a duplicate anchor.
        if skip is not None and skip in f.parents:
            continue
        rel = f.relative_to(folder).as_posix()
        source = f"{prefix}{rel}"
        if only is not None and source not in only:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        title = f.stem.replace("-", " ").replace("_", " ")
        for line in text.splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break
        # A stable anchor so a design reference can link straight to the
        # rendered pane rather than to raw markdown the browser will not format.
        anchor = "doc-" + re.sub(r"[^a-z0-9]+", "-",
                                 f"{prefix}{f.relative_to(folder).as_posix()}".lower()).strip("-")
        if targets:
            text = rewrite_doc_links(text, source, targets)
        out.append({"path": source, "title": title,
                    "anchor": anchor, "body": markdown.render(text),
                    "resolution": resolution_of(text)})
    return out


def documentation_targets(specs: List[Tuple[Path, str, Optional[Path]]]) -> Dict[str, str]:
    """Map source-relative Markdown paths to their rendered page anchors."""
    targets: Dict[str, str] = {}
    for folder, prefix, skip in specs:
        if not folder.is_dir():
            continue
        for f in sorted(folder.rglob("*.md")):
            if f.name.lower() in ("readme.md", "index.md"):
                continue
            if skip is not None and skip in f.parents:
                continue
            rel = f.relative_to(folder).as_posix()
            source = f"{prefix}{rel}"
            anchor = "doc-" + re.sub(
                r"[^a-z0-9]+", "-", source.lower()
            ).strip("-")
            targets[source] = f"#{anchor}"
    return targets


def rewrite_doc_links(text: str, source: str,
                      targets: Dict[str, str]) -> str:
    """Point local docs at inlined panes or the board's safe asset surface."""
    pattern = re.compile(r"(\]\()([^\s)]+)")
    base = source.rsplit("/", 1)[0]

    def replace(match: "re.Match[str]") -> str:
        url = match.group(2)
        if url.startswith(("#", "/")) or re.match(r"^[a-z][a-z0-9+.-]*:", url, re.I):
            return match.group(0)
        clean, suffix = re.match(r"^([^?#]*)(.*)$", url).groups()
        target = posixpath.normpath(posixpath.join(base, clean))
        if not target.startswith("docs/design/"):
            return match.group(0)
        anchor = targets.get(target) if clean.lower().endswith(".md") else None
        destination = anchor or "/" + target + suffix
        return f"{match.group(1)}{destination}"

    return pattern.sub(replace, text)


RE_RESOLUTION = re.compile(r"^_Resolution:\s*([a-z]+)", re.M | re.I)


def resolution_of(text: str) -> str:
    """A section's own declared resolution, so a reader can tell a leaning from
    a commitment without opening it."""
    m = RE_RESOLUTION.search(text)
    return m.group(1).lower() if m else ""


def design_index() -> Dict[str, object]:
    """Read the generated design index, if tools/design.py has produced one.

    Read rather than recomputed: two implementations of the same derivation
    drift, and the index is already regenerated by the gate.
    """
    idx = DESIGN / "INDEX.md"
    if not idx.is_file():
        return {}
    try:
        text = idx.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {}
    ready: List[str] = []
    block = re.search(r"^## Ready to work on\s*$(.*?)(?=^## |\Z)", text,
                      re.M | re.S)
    if block:
        for line in block.group(1).splitlines():
            line = line.strip()
            if line.startswith("- "):
                ready.append(line[2:])
    unbound = len(re.findall(r"\*\*not bound\*\*", text))
    undesigned = len(re.findall(r"a knob nobody designed", text))
    return {"ready": ready, "unbound": unbound, "undesigned": undesigned}


def mermaid_src(depth: int = 0) -> str:
    """Relative path to the vendored mermaid bundle, or "" if absent.

    Referenced with <script src> rather than inlined. Inlining would put ~3 MB
    of minified JS into plan.html and into every snapshot under plan/, which
    makes the snapshots useless for diffing. A relative <script src> loads fine
    over file:// (unlike fetch(), which is blocked), so the page still works
    when opened by double-clicking.

    Vendored rather than pulled from a CDN so it works offline and behind a
    proxy. Absent, diagrams degrade to readable source text.

    depth is how many directories down the page sits: 0 for plan.html at the
    repo root, 1 for plan/<slice>.html.
    """
    for name in ("mermaid.min.js", "mermaid.js"):
        if (VENDOR / name).is_file():
            return "../" * depth + f"tools/vendor/{name}"
    return ""


# ----------------------------------------------------------------- rendering

CSS = """
:root{--bg:#0f1115;--fg:#e6e6e6;--dim:#8b929e;--line:#242832;--card:#161923;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--acc:#58a6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.55 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 80px}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim);
margin:34px 0 12px;padding-bottom:7px;border-bottom:1px solid var(--line)}
.sub{color:var(--dim);font-size:13px;margin:0 0 6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:7px;
padding:13px 15px;margin:0 0 9px}
.card.q{border-left:3px solid var(--warn)}
.card.d{border-left:3px solid var(--acc)}
.card.done{border-left:3px solid var(--ok)}
.t{font-weight:600;margin:0 0 3px}
.m{color:var(--dim);font-size:12.5px;margin:2px 0 0}
code,.mono{font-family:ui-monospace,"Cascadia Code",Consolas,monospace;font-size:12.5px}
.mod{margin:0 0 14px}
.modh{display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
.modp{font-weight:600}
.tag{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;
padding:1.5px 7px;border-radius:9px;border:1px solid var(--line);color:var(--dim)}
.tag.built{color:var(--ok);border-color:#1f4d2b}
.tag.missing{color:var(--warn);border-color:#5a4410}
.tag.deleted{color:#8bd3ff;border-color:#285a73}
.tag.extra{color:var(--bad);border-color:#5c2224}
.tag.new{color:#7fd4ff;border-color:#1d4a63}
.tag.modified{color:#d8b4fe;border-color:#4a2f63}
.tag.existing{color:var(--dim);border-color:#333}
.tag.act{color:var(--dim);border-color:#333;text-transform:none}
.why{color:#b9c2cc;font-size:12px;margin:2px 0 0 4px;line-height:1.45}
.why.warn{color:#8a7a52;font-style:italic}
.why.bd{color:#8fb3c9}
ul.f{list-style:none;margin:7px 0 0;padding:0 0 0 15px;border-left:1px solid var(--line)}
ul.f li{display:flex;align-items:baseline;gap:9px;padding:2.5px 0;flex-wrap:wrap}
.empty{color:var(--dim);font-style:italic}
.legend{color:var(--dim);font-size:12px;margin:0 0 14px}
.banner{border-radius:8px;padding:11px 14px;margin:14px 0;font-size:14px;
  border:1px solid}
.banner.draft{background:#33290f;border-color:#b08b2a;color:#f6e6bd}
.banner.ok{background:#12331f;border-color:#2f9e5e;color:#d7f5e3}
.mermaid{background:var(--card);border:1px solid var(--line);border-radius:7px;
padding:15px;overflow:auto;font-size:12px;color:var(--dim)}
/* Until mermaid.js runs, show the source as readable monospace. It removes
   this class itself once it has drawn the SVG, so an absent renderer degrades
   to legible text instead of a broken box. */
.mermaid:not([data-processed]){white-space:pre;font-family:ui-monospace,monospace}
.mermaid svg{max-width:100%;height:auto}
.card.exp{padding:14px 16px}
.er{display:flex;gap:14px;padding:5px 0;align-items:baseline}
.er+.er{border-top:1px solid rgba(255,255,255,.06)}
.ek{flex:0 0 108px;color:#8b98a9;font-size:12px;text-transform:uppercase;letter-spacing:.04em}
.ev{flex:1;color:#e8edf3}
.ev.no{color:#f0a35e}
.mock{background:#0d1117;border:1px solid rgba(255,255,255,.10);border-radius:8px;padding:18px;text-align:center;margin:10px 0}
.mock svg{max-width:100%;height:auto}
details.doc{background:var(--card);border:1px solid var(--line);border-radius:7px;
margin:0 0 9px;overflow:hidden}
details.doc>summary{cursor:pointer;padding:11px 15px;font-weight:600;
list-style:none;display:flex;align-items:baseline;gap:9px;flex-wrap:wrap}
details.doc>summary::-webkit-details-marker{display:none}
details.doc>summary::before{content:"\25b8";color:var(--dim);font-size:11px}
details.doc[open]>summary::before{content:"\25be"}
details.doc>summary:hover{background:#1b1f2b}
.res{font-size:10px;text-transform:uppercase;letter-spacing:.5px;padding:2px 7px;
  border-radius:9px;font-weight:600;margin-left:2px}
.r-settled{background:#12341f;color:#69d98a;border:1px solid #1d5230}
.r-direction{background:#33270f;color:#e0b25c;border:1px solid #55411a}
.r-question{background:#2a2233;color:#c39ae0;border:1px solid #443055}
.ready{background:var(--card);border:1px solid var(--line);border-left:3px solid #69d98a;
  border-radius:7px;padding:13px 16px;margin:0 0 16px}
.ready .rh{font-weight:600;font-size:13px;margin-bottom:3px}
.ready ul{margin:8px 0 0;padding-left:20px}
.ready li{margin:3px 0;font-size:13px}
.body{padding:2px 20px 18px;border-top:1px solid var(--line)}
.body h1{font-size:19px;margin:18px 0 8px}
.body h2{font-size:15px;text-transform:none;letter-spacing:0;color:var(--fg);
border:0;margin:20px 0 7px;padding:0}
.body h3{font-size:13.5px;margin:16px 0 6px;color:var(--fg)}
.body p{margin:8px 0}
.body ul,.body ol{margin:8px 0;padding-left:22px}
.body li{margin:3px 0}
.body pre{background:#0c0e13;border:1px solid var(--line);border-radius:6px;
padding:11px 13px;overflow:auto;margin:10px 0}
.body pre code{font-size:12px;color:#c8d1dc}
.body table{border-collapse:collapse;margin:12px 0;font-size:13px;display:block;
overflow-x:auto}
.body th,.body td{border:1px solid var(--line);padding:6px 10px;text-align:left;
vertical-align:top}
.body th{background:#1b1f2b;font-size:12px;text-transform:uppercase;
letter-spacing:.05em;color:var(--dim)}
.body blockquote{margin:10px 0;padding:2px 0 2px 14px;
border-left:3px solid var(--line);color:var(--dim)}
.body hr{border:0;border-top:1px solid var(--line);margin:18px 0}
.body code{background:#0c0e13;padding:1px 5px;border-radius:4px}
.body pre code{background:0;padding:0}
.opt{display:block;padding:2.5px 0;color:var(--dim);font-size:13px}
a{color:var(--acc)}
.sig{color:var(--dim)}
.foot{margin-top:44px;padding-top:14px;border-top:1px solid var(--line);
color:var(--dim);font-size:12px}
.front{margin:18px 0 26px}
.front-grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(260px,.65fr);
gap:12px;margin:12px 0}
.front-card{background:var(--card);border:1px solid var(--line);border-radius:9px;
padding:15px 17px}
.front-card h2{border:0;margin:0 0 9px;padding:0;font-size:12px;color:var(--dim)}
.outcome{font-size:17px;font-weight:650;line-height:1.4;margin:0 0 7px}
.front-meta{display:flex;gap:7px;flex-wrap:wrap;margin-top:10px}
.metric{border:1px solid var(--line);border-radius:999px;padding:2px 9px;
font-size:11.5px;color:var(--dim)}
.metric.bad{color:var(--bad);border-color:#5c2224}
.metric.warn{color:var(--warn);border-color:#5a4410}
.metric.ok{color:var(--ok);border-color:#1f4d2b}
.decision-stack{display:grid;gap:8px}
.decision-card{background:#1a1710;border:1px solid #5a4410;border-left:3px solid var(--warn);
border-radius:7px;padding:11px 13px}
.decision-card .ask{font-weight:650;margin-bottom:3px}
.decision-card .answer{color:#c5ced8;font-size:12.5px}
.decision-card ul{margin:6px 0 0;padding-left:19px;color:#c5ced8}
.decision-card code{user-select:all}
.decision-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.decision-actions button{border:1px solid var(--line);border-radius:6px;background:#202735;
color:var(--fg);padding:7px 11px;cursor:pointer;font-weight:600}
.decision-actions button.approve{background:#15351f;border-color:#2f7d48}
.decision-actions button.veto{background:#36191a;border-color:#7d3033}
.decision-comment{width:100%;min-height:64px;margin-top:9px;padding:8px 10px;
border:1px solid var(--line);border-radius:6px;background:#0d1117;color:var(--fg);
font:13px/1.4 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.fact-list{display:grid;gap:5px;font-size:12.5px;color:#c5ced8}
.fact-list b{color:var(--dim);font-size:11px;text-transform:uppercase;
letter-spacing:.05em;margin-right:5px}
.design-disclosure{border-top:1px solid var(--line);margin-top:5px;padding-top:7px}
.design-excerpt{white-space:pre-wrap;margin-top:3px;color:#c5ced8}
.clear{background:#101b14;border-color:#1f4d2b;border-left-color:var(--ok)}
.record{margin-top:22px;border:1px solid var(--line);border-radius:9px;background:#11141b}
.record>summary{cursor:pointer;padding:14px 16px;font-weight:650;list-style:none}
.record>summary::-webkit-details-marker{display:none}
.record>summary::before{content:"▸";color:var(--dim);font-size:11px;margin-right:8px}
.record[open]>summary::before{content:"▾"}
.record>.record-body{padding:0 16px 18px;border-top:1px solid var(--line)}
.architecture-map{--arch-existing:#d9ddd7;--arch-adding:#ff7a35;
--arch-changing:#9b7cff;--arch-removing:#ff5d6c;--arch-unplanned:#ffbd2e;
--arch-conflict:#ff4d5e;
margin:24px 0;border:1px solid var(--line);border-radius:10px;background:#11141b;
overflow:hidden}
.arch-intro{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;
padding:17px 18px;border-bottom:1px solid var(--line)}
.arch-eyebrow{margin:0 0 2px;color:var(--acc);font-size:10.5px;font-weight:700;
letter-spacing:.09em;text-transform:uppercase}
.arch-intro h2{margin:0;padding:0;border:0;color:var(--fg);font-size:18px;
letter-spacing:0;text-transform:none}
.arch-intro-copy{margin:5px 0 0;color:#b9c2cc;font-size:12.5px;max-width:680px}
.arch-settled{flex:0 0 auto;border:1px solid #315f42;border-radius:999px;
padding:3px 9px;color:var(--ok);font-size:11px}
.arch-read-path{display:grid;grid-template-columns:repeat(3,1fr);border-bottom:1px solid var(--line)}
.arch-read-step{display:grid;grid-template-columns:24px 1fr;gap:8px;padding:10px 13px;
color:#aeb7c3;font-size:11.5px;border-right:1px solid var(--line)}
.arch-read-step:last-child{border-right:0}
.arch-step-number{display:grid;place-items:center;width:21px;height:21px;border-radius:50%;
background:#263142;color:#dce8f7;font-weight:700}
.arch-read-step strong{display:block;color:var(--fg);font-size:12px}
.arch-controls{display:grid;grid-template-columns:auto auto minmax(180px,1fr) auto;
gap:14px;padding:12px 14px;border-bottom:1px solid var(--line);align-items:end}
.arch-control{display:grid;gap:5px;min-width:0}
.arch-control-label{color:var(--dim);font-size:10px;font-weight:700;text-transform:uppercase;
letter-spacing:.07em}
.arch-buttons{display:flex;gap:5px;flex-wrap:wrap}
.arch-buttons button,.arch-search,.arch-list-button,.arch-primary{border:1px solid var(--line);
border-radius:6px;background:#181d27;color:var(--fg);font:inherit}
.arch-buttons button{padding:5px 8px;cursor:pointer;font-size:11.5px}
.arch-buttons button[aria-pressed="true"]{background:#26384f;border-color:#4777ac;color:#fff}
.arch-buttons button[disabled]{opacity:.38;cursor:not-allowed}
.arch-search-wrap{display:flex;align-items:center;gap:7px}
.arch-search{width:100%;min-width:0;padding:6px 8px;font-size:12px}
.arch-search-count{color:var(--dim);font-size:11px;min-width:22px}
.arch-workspace{display:grid;grid-template-columns:minmax(0,1fr) minmax(270px,340px)}
.arch-map-panel{min-width:0;border-right:1px solid var(--line)}
.arch-map-heading{display:flex;justify-content:space-between;align-items:baseline;gap:12px;
padding:10px 14px;border-bottom:1px solid var(--line)}
.arch-map-heading h3{margin:0;font-size:12.5px}.arch-map-status{color:var(--dim);font-size:11px}
.arch-graph-shell{position:relative;height:500px;overflow:hidden;background:#0d1016}
.arch-graph{display:block;width:100%;height:100%;touch-action:none;cursor:grab}
.arch-graph:active{cursor:grabbing}.arch-edge{stroke:#747b86;stroke-width:1;opacity:.48}
.arch-edge[data-relation="uses"]{stroke:#5884ad;stroke-dasharray:3 4;opacity:.3}
.arch-edge[data-provenance="planned"]{stroke:#b78cff;stroke-dasharray:2 5}
.arch-edge[data-connected="true"]{opacity:.95;stroke-width:1.8}
.arch-viewport[data-focus="true"] .arch-edge[data-connected="false"]{opacity:.09}
.arch-node{cursor:pointer;outline:none}.arch-node-hit{fill:transparent}
.arch-node-dot{fill:var(--arch-existing);stroke:#0b0d11;stroke-width:1.5}
.arch-node[data-state="adding"] .arch-node-dot{fill:var(--arch-adding)}
.arch-node[data-state="changing"] .arch-node-dot{fill:var(--arch-changing)}
.arch-node[data-state="removing"] .arch-node-dot{fill:#0d1016;stroke:var(--arch-removing);stroke-width:2.5}
.arch-node[data-state="unplanned"] .arch-node-dot{fill:var(--arch-unplanned)}
.arch-node[data-state="conflict"] .arch-node-dot{fill:var(--arch-conflict);stroke:#fff;stroke-width:2}
.arch-node-halo{fill:none;stroke:var(--acc);stroke-width:2;opacity:0}
.arch-node:hover .arch-node-halo,.arch-node:focus-visible .arch-node-halo,
.arch-node[data-selected="true"] .arch-node-halo{opacity:1}
.arch-node[data-match="true"] .arch-node-halo{opacity:1;stroke:#fff;stroke-dasharray:2 2}
.arch-node[data-dimmed="true"]{opacity:.22}
.arch-zoom-hint{position:absolute;right:9px;bottom:7px;margin:0;color:#6f7885;
font-size:10.5px;pointer-events:none}
.arch-legend{display:flex;gap:14px;flex-wrap:wrap;padding:9px 14px;border-top:1px solid var(--line);
color:#aeb7c3;font-size:11px}.arch-key{display:flex;align-items:center;gap:6px}
.arch-key-mark{width:9px;height:9px;border-radius:50%;background:currentColor}
.arch-key.existing{color:var(--arch-existing)}.arch-key.adding{color:var(--arch-adding)}
.arch-key.changing{color:var(--arch-changing)}.arch-key.removing{color:var(--arch-removing)}
.arch-key.removing .arch-key-mark{background:transparent;border:2px solid currentColor}
.arch-key.unplanned{color:var(--arch-unplanned)}
.arch-key.conflict{color:var(--arch-conflict)}
.arch-inspector{min-width:0;background:#141821}.arch-inspector-header{padding:14px 15px;
border-bottom:1px solid var(--line)}.arch-inspector-header h3{margin:2px 0 7px;
font-size:14px;overflow-wrap:anywhere}.arch-tags{display:flex;gap:6px;flex-wrap:wrap}
.arch-tag{border:1px solid var(--line);border-radius:999px;padding:2px 7px;color:var(--dim);
font-size:10.5px}.arch-tag[data-state="adding"]{color:var(--arch-adding)}
.arch-tag[data-state="changing"]{color:var(--arch-changing)}
.arch-tag[data-state="removing"]{color:var(--arch-removing)}
.arch-tag[data-state="unplanned"]{color:var(--arch-unplanned)}
.arch-tag[data-state="conflict"]{color:var(--arch-conflict)}
.arch-inspector-body{max-height:500px;overflow:auto;padding:4px 15px 15px}
.arch-inspector-body dl{margin:0}.arch-inspector-body dl>div{padding:9px 0;
border-bottom:1px solid rgba(255,255,255,.06)}.arch-inspector-body dt{color:var(--dim);
font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.06em}
.arch-inspector-body dd{margin:3px 0 0;color:#d8dee7;font-size:12px;line-height:1.45}
.arch-special{margin:10px 0 0;padding:10px 11px;border:1px solid #3a414d;
border-radius:7px;background:#10141b}.arch-special h4{margin:0 0 5px;font-size:12px}
.arch-option{padding:6px 0;border-top:1px solid var(--line);font-size:11.5px}
.arch-option:first-of-type{border-top:0}.arch-option strong{display:block;color:#e8edf3}
.arch-option span{color:#aeb7c3}.arch-primary{margin-top:11px;padding:6px 9px;cursor:pointer;
font-size:11.5px}.arch-rule-note{display:flex;gap:9px;padding:10px 14px;border-top:1px solid var(--line);
color:var(--dim);font-size:11.5px}.arch-rule-note strong{color:#dce3ec;white-space:nowrap}
.arch-fallback{margin:0;border-top:1px solid var(--line)}
.arch-fallback summary{cursor:pointer;padding:10px 14px;font-weight:650}
.arch-fallback-list{list-style:none;margin:0;padding:0;border-top:1px solid var(--line)}
.arch-fallback-list li[hidden]{display:none}.arch-list-button{display:grid;width:100%;
grid-template-columns:18px minmax(0,1fr) auto;gap:7px;text-align:left;padding:7px 12px;
border:0;border-bottom:1px solid var(--line);border-radius:0;cursor:pointer;font-size:11.5px}
.arch-list-button:hover,.arch-list-button:focus-visible{background:#202633}
.arch-list-state{color:var(--dim);font-size:10.5px}.arch-noscript{padding:10px 14px;color:var(--warn)}
.arch-sr{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;
clip:rect(0,0,0,0);white-space:nowrap;border:0}
@media(max-width:760px){.front-grid{grid-template-columns:1fr}}
@media(max-width:900px){.arch-controls{grid-template-columns:1fr 1fr}.arch-workspace{grid-template-columns:1fr}
.arch-map-panel{border-right:0;border-bottom:1px solid var(--line)}.arch-inspector-body{max-height:none}}
@media(max-width:600px){.arch-intro{display:grid}.arch-settled{justify-self:start}
.arch-read-path{grid-template-columns:1fr}.arch-read-step{border-right:0;border-bottom:1px solid var(--line)}
.arch-read-step:last-child{border-bottom:0}.arch-controls{grid-template-columns:1fr}
.arch-graph-shell{height:390px}.arch-rule-note{display:grid}.arch-rule-note strong{white-space:normal}}
#retro-banner{margin:14px 0}
#retro-banner .rb{padding:10px 14px;border-radius:7px;font-size:13px;
border:1px solid var(--line);background:var(--card);margin-bottom:8px}
#retro-banner .rb b{color:#e8edf3}
#retro-banner .rb.due{border-color:#8a5a1f;background:#221b12}
#retro-banner .rb.action{border-color:#2a5a8f;background:#111a24}
#retro-banner .rb a{margin-left:6px}
"""

# Read from /api/state and nothing else. Every branch that is not "the board
# answered and there is something to say" leaves the element empty, so a plan
# opened from a file, or next to a board that has exited, looks exactly as it
# did before this existed.
RETRO_BANNER_JS = """
<script>
(function(){
  var B = window.Board;
  var host = document.getElementById('retro-banner');
  if(!B || !host) return;
  B.onState(function(state){
    var parts = [];
    var due = (state && state.retro_due) || {};
    var providers = (state && state.providers) || {};
    var analyzer = providers.analyzer || {kind:'manual', automatic:false};
    var findings = (state && state.findings) || [];
    var waiting = findings.filter(function(f){
      return (f.state || 'awaiting_review') === 'awaiting_review';
    }).length;

    var evidenceWarnings = Array.isArray(due.warnings) ? due.warnings : [];
    if(evidenceWarnings.length){
      var shownWarnings = evidenceWarnings.slice(0, 3).map(function(item){
        if(!item || typeof item !== 'object') return 'unclassified warning';
        var code = String(item.code || 'unclassified warning').slice(0, 96);
        var note = String(item.note || '').slice(0, 96);
        var value = String(item.value || '').slice(0, 96);
        return code + (note ? ' (' + note + ')' : '')
          + (value ? ': ' + value : '');
      });
      var remainingWarnings = evidenceWarnings.length - shownWarnings.length;
      parts.push('<div class="rb due"><b>Retrospective evidence warning:</b> '
        + shownWarnings.map(B.esc).join('; ')
        + (remainingWarnings ? '; +' + B.esc(remainingWarnings) + ' more' : '')
        + '. Classification may be incomplete.'
        + '<a href="/retro.html">Inspect the evidence</a></div>');
    }

    if(due.due){
      function triggerCodes(entries){
        return (entries || []).map(function(item){
          return item && item.code ? String(item.code) : '';
        }).filter(function(code){ return !!code; });
      }
      var immediate = triggerCodes(due.immediate_consequences);
      var prompted = triggerCodes(due.prompt_triggers);
      var reason = '';
      if(immediate.length){
        reason = '<b>Immediate retrospective consequence: '
          + immediate.map(B.esc).join(', ') + '.</b> Review this before routine note count.';
      }else if(prompted.length){
        reason = '<b>Retrospective trigger: ' + prompted.map(B.esc).join(', ')
          + '.</b> The declared pattern warrants review now.';
      }else{
        reason = '<b>Routine retrospective is due.</b> ' + B.esc(due.unarchived)
          + ' unarchived slice note' + (Number(due.unarchived) === 1 ? '' : 's')
          + ' reached the threshold of ' + B.esc(due.threshold) + '.';
      }
      var execution = '';
      var blockers = analyzer.blockers || [];
      if(!analyzer.automatic || analyzer.kind === 'manual'){
        execution = ' Deterministic local evidence is available for a manual handoff; '
          + 'opening this cockpit calls no model and spends no provider quota.';
      }else if(analyzer.ready !== false && blockers.length === 0){
        execution = ' The automatic analyzer is ready. Explicitly running it may spend provider quota.';
      }else{
        execution = ' Automatic analysis is blocked: ' + B.esc(blockers.join('; ')
          || 'the configured adapter is unavailable')
          + '. Deterministic local evidence remains available.';
      }
      parts.push('<div class="rb due">' + reason + execution
        + '<a href="/retro.html">Open the retrospective board</a></div>');
    }
    if(waiting){
      parts.push('<div class="rb action"><b>' + B.esc(waiting) + ' finding'
        + (waiting === 1 ? '' : 's') + ' awaiting your decision.</b> '
        + 'Approval is recorded even when no automatic worker is configured.'
        + '<a href="/retro.html">Review and action</a></div>');
    }
    host.innerHTML = parts.join('');
  });
})();
</script>
"""

PLAN_DECISION_JS = """
<script>
(function(){
  var B = window.Board;
  var host = document.getElementById('plan-decision-controls')
    || document.getElementById('plan-reconsider-controls')
    || document.getElementById('plan-recorded-controls');
  var sentinel = document.getElementById('plan-live-state');
  if(!B || !sentinel) return;
  var fingerprint = String(sentinel.getAttribute('data-plan-fingerprint') || '');
  var comment = host ? host.querySelector('.decision-comment') : null;
  var buttons = host ? host.querySelectorAll('[data-plan-action]') : [];
  function decide(button){
    var action = String(button.getAttribute('data-plan-action') || '');
    var reason = comment ? String(comment.value || '').trim() : '';
    if(action !== 'approve' && !reason){
      B.showError('Say what must change before vetoing or requesting changes.');
      if(comment) comment.focus();
      return;
    }
    B.guard(Array.prototype.slice.call(buttons), 'recording\u2026', function(){
      return B.request('/api/plan/decision', {method:'POST', body:{
        action:action, fingerprint:fingerprint, comment:reason
      }}).then(function(result){
        if(!result.ok){ B.reportIfFailed(result); return result; }
        window.location.reload();
        return result;
      });
    });
  }
  if(host){
    for(var i=0; i<buttons.length; i++){
      buttons[i].addEventListener('click', (function(button){
        return function(){ decide(button); };
      })(buttons[i]));
    }
  }
  B.onState(function(state){
    var live = state && state.plan;
    if(!fingerprint || !live || !live.fingerprint || live.fingerprint === fingerprint) return;
    for(var i=0; i<buttons.length; i++) buttons[i].disabled = true;
    B.showError('The plan or bound design changed after this page loaded. Refresh before continuing.');
  });
})();
</script>
"""


def state_tag(state: str) -> str:
    """Labels a reader can act on.

    "unproposed" conflated two different things: code that appeared without
    approval, and code that predates this slice and is fine. Only the first is
    a problem, so they get different words.
    """
    label = {"built": "built", "missing": "to do", "extra": "unproposed",
             "modified": "modified", "new": "new", "existing": "existing",
             "deleted": "deleted"}[state]
    return f'<span class="tag {state}">{label}</span>'


def approved_history() -> Dict[str, Any]:
    """Everything approved in earlier slices, merged.

    Approval accumulates across a project. Reading only the current proposal
    marks every function approved in slices 0-2 as unapproved, so the diagram
    turns red on work you signed off weeks ago -- and a diagram that cries wolf
    gets ignored, which is worse than one that shows nothing.

    Only proposals whose status reached "approved" count. A draft that was never
    signed off is not evidence of approval.
    """
    out: Dict[str, Any] = {"files": [], "modules": [], "functions": []}
    try:
        code, out_txt = git("log", "--format=%H", "--", "proposal.json")
        shas = out_txt.split() if code == 0 else []
    except Exception:
        return out
    seen: set = set()
    for sha in shas:
        try:
            code, blob = git("show", f"{sha}:proposal.json")
            if code != 0:
                continue
            obj = json.loads(blob)
            shape_code, shape_blob = git("show", f"{sha}:project.shape.json")
            if shape_code != 0:
                continue
            shape = json.loads(shape_blob)
        except Exception:
            continue
        design_digests: Dict[str, str] = {}
        refs = obj.get("design_refs") if isinstance(obj, dict) else []
        valid_material = True
        for ref in refs if isinstance(refs, list) else []:
            if not isinstance(ref, dict):
                valid_material = False
                break
            section = str(ref.get("section") or "")
            if (
                not section.startswith("docs/design/")
                or "\\" in section
                or ".." in section.split("/")
                or not section.endswith(".md")
            ):
                valid_material = False
                break
            design_code, design_blob = git("show", f"{sha}:{section}")
            if design_code != 0:
                valid_material = False
                break
            design_digests[section] = design_contract.design_sha256(design_blob)
        if not valid_material:
            continue
        exact = proposal_authority.exact_approval_material(
            obj, shape if isinstance(shape, dict) else {}, design_digests
        )
        if not exact["approved"]:
            continue
        for key in out:
            for item in obj.get(key, []) or []:
                if not isinstance(item, dict):
                    continue
                ident = (key, str(item.get("path") or ""),
                         str(item.get("file") or ""),
                         str(item.get("signature") or ""))
                if ident in seen:
                    continue
                seen.add(ident)
                # Newest commit wins. A delete is a tombstone, not a historic
                # approval that should keep the removed thing green forever.
                if str(item.get("action") or "") != "delete":
                    out[key].append(item)
    return out


def file_state(path: str, prop_files: Dict[str, Any], built: set,
               approved_before: set | None = None) -> str:
    """Which badge a file row gets.

    "built" hid whether the file was created or changed, which is the first
    thing a reviewer wants to know. A declared action drives the badge; falling
    back to built/to do only when none was declared.
    """
    meta = prop_files.get(path)
    if meta is None:
        # Approved in an earlier slice: it exists legitimately and is not part
        # of this one. Calling that "unproposed" is the diagram crying wolf.
        if approved_before and path in approved_before:
            return "existing"
        return "extra"
    act = str(meta.get("action", "") or "")
    if act == "delete" and path not in built:
        return "deleted"
    if path in built:
        return {"new": "new", "modify": "modified"}.get(act, "built")
    return "missing"


def hierarchy(prop: Dict[str, Any], tree: Dict[str, Any], built: set,
              depth: int, history: Dict[str, Any] | None = None) -> str:
    """A mermaid graph from ``res://`` to functions, coloured by state.

    The module graph answers "what depends on what". It cannot answer "does the
    thing I approved exist", because approval happens at file and function level
    while a module is a directory. This walks folders, then files, then function
    signatures, and marks each node:

      built       proposed and present in code
      missing     proposed, not written yet
      extra       present in code, never proposed

    Depth follows involvement: a module-level human is not shown function nodes
    they never approved, and rendering them would bury the level they did.
    """
    prop_files = {str(f.get("path", "")).strip().replace("\\", "/"): f
                  for f in rows(prop, "files") if f.get("path")}
    prop_mods = {str(m.get("path", "")).strip().replace("\\", "/"): m
                 for m in rows(prop, "modules") if m.get("path")}
    # Approval accumulates. Reading only the current proposal marks every
    # function approved in slices 0-2 as "exists without approval", which makes
    # the diagram cry wolf -- worse than showing no state at all.
    hist = history or {"files": [], "modules": [], "functions": []}
    # GDScript has no overloads, so one file/name pair has one current approved
    # signature. Historical proposals are newest-first; the first occurrence
    # wins, then the current proposal overrides it (including deletions).
    prop_funcs: Dict[str, Dict[str, str]] = {}
    seen_history: set[Tuple[str, str]] = set()
    for fn_ in rows(hist, "functions"):
        f = str(fn_.get("file", "")).strip().replace("\\", "/")
        sig = str(fn_.get("signature", "") or "")
        try:
            parsed = gd_signature.parse_proposal_signature(sig)
        except gd_signature.SignatureError:
            # Legacy malformed history is not authority. Current proposal
            # signatures are rejected by schema validation before rendering.
            continue
        class_scope = str(fn_.get("class_scope") or "").strip()
        identity = f"{class_scope}.{parsed.name}" if class_scope else parsed.name
        key = (f, identity)
        if not f or key in seen_history:
            continue
        seen_history.add(key)
        if str(fn_.get("action", "") or "") != "delete":
            prop_funcs.setdefault(f, {})[identity] = parsed.canonical
    for fn_ in rows(prop, "functions"):
        f = str(fn_.get("file", "")).strip().replace("\\", "/")
        sig = str(fn_.get("signature", "") or "")
        try:
            parsed = gd_signature.parse_proposal_signature(sig)
        except gd_signature.SignatureError:
            continue
        if not f:
            continue
        class_scope = str(fn_.get("class_scope") or "").strip()
        identity = f"{class_scope}.{parsed.name}" if class_scope else parsed.name
        if str(fn_.get("action", "") or "") == "delete":
            prop_funcs.setdefault(f, {}).pop(identity, None)
        else:
            prop_funcs.setdefault(f, {})[identity] = parsed.canonical

    hist_files = {str(f.get("path", "")).strip().replace("\\", "/")
                  for f in rows(hist, "files") if f.get("path")}
    hist_mods = {str(m.get("path", "")).strip().replace("\\", "/")
                 for m in rows(hist, "modules") if m.get("path")}
    approved_files = set(prop_files) | hist_files
    approved_mods = set(prop_mods) | hist_mods

    # Files worth showing: proposed, or touched in this slice. Never the whole
    # tree -- a repo-wide dump makes the two new files impossible to find.
    show = set(prop_files) | set(built)
    if not show:
        return ""

    ids: Dict[str, str] = {}

    def nid(key: str) -> str:
        if key not in ids:
            ids[key] = "n%d" % len(ids)
        return ids[key]

    def label(text: str) -> str:
        # Mermaid node text: quotes end the label, <br/> is the only markup it
        # accepts inside one.
        return text.replace('"', "'").replace("\n", " ")

    lines = ["graph LR"]
    states: Dict[str, List[str]] = {
        "built": [], "missing": [], "extra": [], "deleted": []
    }

    root = nid("res://")
    lines.append(f'    {root}(["res://"])')

    # folders, deepest-first ownership so a file appears exactly once
    folders = sorted({str(Path(f).parent).replace("\\", "/")
                      for f in show if "/" in f})
    seen_folder = set()
    for folder in folders:
        parts = folder.split("/")
        for i in range(len(parts)):
            sub = "/".join(parts[: i + 1])
            if sub in seen_folder:
                continue
            seen_folder.add(sub)
            parent = root if i == 0 else nid("/".join(parts[:i]))
            n = nid(sub)
            lines.append(f'    {n}["{label(parts[i])}/"]')
            lines.append(f"    {parent} --> {n}")
            if sub in approved_mods:
                module_meta = prop_mods.get(sub, {})
                if module_meta.get("action") == "delete" and not any(
                    f.startswith(sub + "/") and f in built for f in show
                ):
                    states["deleted"].append(n)
                else:
                    states["built" if any(f.startswith(sub + "/") and f in built
                                          for f in show)
                           else "missing"].append(n)
            elif any(f.startswith(sub + "/") and f not in approved_files
                     for f in show):
                states["extra"].append(n)
            # else: an intermediate path segment carrying approved children.
            # Colouring it "extra" would flag scripts/ as unapproved merely
            # because only scripts/data was named, which is noise.

    for f in sorted(show):
        parent = root
        if "/" in f:
            parent = nid(str(Path(f).parent).replace("\\", "/"))
        proposed = f in approved_files
        exists = f in built
        action = str(prop_files.get(f, {}).get("action") or "")
        st = (
            "deleted" if proposed and action == "delete" and not exists
            else "built" if proposed and exists
            else "missing" if proposed
            else "extra"
        )
        n = nid(f)
        lines.append(f'    {n}["{label(Path(f).name)}"]')
        lines.append(f"    {parent} --> {n}")
        states[st].append(n)

        if depth < 3:
            continue
        real = tree.get(f, {})
        wanted_by_identity = prop_funcs.get(f, {})
        observed_identities: set[str] = set()
        for fn_ in real.get("functions", []):
            if not isinstance(fn_, dict):
                continue
            raw_signature = str(fn_.get("signature", "") or "")
            try:
                real_signature = gd_signature.parse_proposal_signature(
                    raw_signature
                ).canonical
            except gd_signature.SignatureError:
                # Invalid architecture data must never satisfy a proposal. The
                # arch.py producer normally rejects it before the plan runs.
                real_signature = ""
            real_identity = str(fn_.get("identity") or fn_.get("name") or "")
            # Engine callbacks are noise in a design view: nobody proposes
            # _ready, and listing them drowns the functions that were.
            if fn_.get("private") and real_identity not in wanted_by_identity:
                continue
            matches_reviewed_signature = (
                wanted_by_identity.get(real_identity) == real_signature
            )
            if matches_reviewed_signature and real_identity:
                observed_identities.add(real_identity)
            fst = "built" if matches_reviewed_signature else "extra"
            display = (
                f"{real_identity}: {raw_signature}"
                if "." in real_identity else raw_signature
            )
            fnid = nid(f + "::" + (real_identity or real_signature or raw_signature))
            lines.append(f'    {fnid}("{label(display)}")')
            lines.append(f"    {nid(f)} --> {fnid}")
            states[fst].append(fnid)
        for missing_identity in sorted(set(wanted_by_identity) - observed_identities):
            missing = wanted_by_identity[missing_identity]
            fnid = nid(f + "::" + missing_identity)
            display = (
                f"{missing_identity}: {missing}"
                if "." in missing_identity else missing
            )
            lines.append(f'    {fnid}("{label(display)}")')
            lines.append(f"    {nid(f)} --> {fnid}")
            states["missing"].append(fnid)

    lines.append("")
    lines.append("    classDef built fill:#12331f,stroke:#2f9e5e,color:#d7f5e3")
    lines.append("    classDef missing fill:#33290f,stroke:#b08b2a,"
                 "color:#f6e6bd,stroke-dasharray:4 3")
    lines.append("    classDef extra fill:#3a1620,stroke:#c0485f,color:#ffdbe3")
    lines.append("    classDef deleted fill:#102b3a,stroke:#3a9cc4,color:#d7f3ff")
    for st, nodes in states.items():
        if nodes:
            lines.append(f"    class {','.join(sorted(set(nodes)))} {st}")
    return "\n".join(lines)


def architecture_map_model(
    prop: Dict[str, Any],
    tree: Dict[str, Any],
    built: set,
    mods: List[Dict[str, Any]],
    changed_actions: Dict[str, str] | None = None,
    cockpit_state: Dict[str, Any] | None = None,
    present: set[str] | None = None,
    baseline_files: set[str] | None = None,
    involvement: str = "function",
    function_changes: Dict[Tuple[str, str], Dict[str, str]] | None = None,
) -> Dict[str, Any]:
    """Build the complete source map without turning observation into intent."""

    def normalise(value: Any) -> str:
        return str(value or "").strip().replace("\\", "/")

    proposal_files = {
        normalise(item.get("path")): item
        for item in rows(prop, "files")
        if normalise(item.get("path"))
    }
    proposal_modules = {
        normalise(item.get("path")).rstrip("/"): item
        for item in rows(prop, "modules")
        if normalise(item.get("path"))
    }
    observed_actions = {
        normalise(path): str(action or "").strip().lower()
        for path, action in (changed_actions or {}).items()
        if normalise(path)
    }
    comparison_known = changed_actions is not None
    baseline_known = baseline_files is not None
    function_comparison_known = function_changes is not None
    observed_function_changes = function_changes or {}
    baseline_set = {
        normalise(path) for path in (baseline_files or set()) if normalise(path)
    }
    inferred_present = {
        normalise(path) for path in tree if normalise(path)
    } | {
        normalise(path) for path in built if normalise(path)
    }
    current_files = (
        {normalise(path) for path in present if normalise(path)}
        if present is not None
        else inferred_present
    )
    observed_tree = {
        normalise(path): value
        for path, value in tree.items()
        if (
            normalise(path)
            and isinstance(value, dict)
            and (present is None or normalise(path) in current_files)
        )
    }
    all_files = (
        current_files
        | set(proposal_files)
        | set(observed_actions)
    )

    design_reasons = [
        str(item.get("why") or "").strip()
        for item in rows(prop, "design_refs")
        if str(item.get("why") or "").strip()
    ]
    view_state = cockpit_state or {}
    plan_status = str(view_state.get("status") or prop.get("status") or "").strip()
    authority = view_state.get("design_authority")
    if not isinstance(authority, dict):
        authority = prop.get("design_authority")
    if not isinstance(authority, dict):
        authority = {}
    authority_value = str(
        authority.get("authority") or authority.get("status") or ""
    ).strip()
    approval_required = bool(view_state.get("approval_required", plan_status == "draft"))

    reversibility = prop.get("reversibility")
    if not isinstance(reversibility, dict):
        reversibility = {}
    reversibility_state = str(reversibility.get("state") or "").strip()
    hard_to_undo = str(reversibility.get("hard_to_undo") or "").strip()
    veto_scope = str(reversibility.get("veto_scope") or "").strip()

    verification = view_state.get("verification")
    if not isinstance(verification, dict):
        verification = {}
    check_text = str(verification.get("summary") or "").strip()
    if not check_text:
        verification_status = str(verification.get("status") or "not run").strip()
        check_text = (
            f"The cockpit reports verification as {verification_status}. "
            "No item-specific check is recorded in the proposal."
        )

    considered = [
        {
            "name": normalise(item.get("path")),
            "answer": str(item.get("why_not") or "").strip(),
        }
        for item in rows(prop, "considered_existing")
        if normalise(item.get("path"))
    ]
    action_states = {
        "new": "adding",
        "modify": "changing",
        "delete": "removing",
    }

    def under(folder: str, path: str) -> bool:
        if folder == "(root)":
            return "/" not in path
        return path == folder or path.startswith(folder.rstrip("/") + "/")

    def state_for(
        declared: str,
        observed: str,
        before_exists: bool | None = None,
        known: bool | None = None,
    ) -> str:
        evidence_known = comparison_known if known is None else known
        if declared in action_states:
            if observed in action_states and observed != declared:
                return "conflict"
            if evidence_known:
                if declared == "new" and before_exists is True:
                    return "conflict"
                if declared in ("modify", "delete") and before_exists is False:
                    return "conflict"
            return action_states[declared]
        if observed in action_states:
            return "unplanned"
        return "existing"

    def change_text(
        label: str,
        kind: str,
        state: str,
        declared: str,
        observed: str,
        exists: bool,
        known: bool,
    ) -> str:
        if state == "existing":
            if not known:
                return (
                    "No change is proposed here, but Git comparison is unavailable, "
                    "so whether this item changed is unknown."
                )
            return (
                "No change is proposed or observed here; it is shown to keep the "
                "surrounding structure clear."
            )
        if state == "conflict":
            if declared == "new" and not observed and exists:
                return (
                    "The plan says new, but the item already existed at the baseline. "
                    "This is not a new addition."
                )
            if declared == "modify" and not observed and not exists:
                return (
                    "The plan says modify, but the item was absent at the baseline "
                    "and is absent now. There is nothing to modify."
                )
            if declared == "delete" and not observed and not exists:
                return (
                    "The plan says delete, but the item was absent at the baseline "
                    "and is absent now. Git does not show a removal to perform."
                )
            return (
                f"The plan says {declared}, but Git reports {observed}. "
                "This mismatch must be resolved before work continues."
            )
        if state == "unplanned":
            return (
                f"Git reports an unplanned {observed} to this {kind}: {label}. "
                "It is outside the recorded plan."
            )
        if not known:
            return (
                f"The plan says {declared}, but Git comparison is unavailable, "
                "so implementation progress is unknown."
            )
        if declared == "new":
            if observed == "new" and exists:
                return "The planned addition is present in the working tree."
            return "The planned addition has not been observed since the baseline."
        if declared == "modify":
            if observed == "modify" and exists:
                return "The planned change is present in the working tree."
            return "The planned change has not been observed since the baseline."
        if declared == "delete":
            if observed == "delete" and not exists:
                return "The planned removal is complete."
            if not exists:
                return (
                    "The item is absent now, but Git does not show a deletion from "
                    "the baseline. The planned removal is not proven."
                )
            return "The item is planned for removal and is still present."
        return "No implementation claim is available."

    def design_text(state: str) -> str:
        if state in ("unplanned", "conflict"):
            return (
                "No. This item is outside, or conflicts with, the reviewed plan; "
                "design alignment is not established."
            )
        if state == "existing":
            return (
                "This is context outside the active change. The map makes no "
                "design-alignment claim for it."
            )
        if not design_reasons:
            return "No design reference is recorded for this slice."
        reason = " ".join(design_reasons[:3])
        if (
            plan_status == "approved"
            and authority_value == "human-confirmed"
            and not approval_required
        ):
            return "Yes. The approved plan cites this player outcome: " + reason
        if authority_value == "human-confirmed":
            return (
                "The cited design is human-confirmed, but this exact plan is not "
                "currently approved. It serves: " + reason
            )
        if authority_value == "agent-provisional":
            return (
                "Not yet. The cited design is agent-provisional and serves: " + reason
            )
        return (
            "The proposal cites this outcome, but validated design authority is "
            "not available: " + reason
        )

    def risk_text(state: str) -> str:
        if state == "existing":
            return "No change is planned for this item."
        if state in ("unplanned", "conflict"):
            return (
                "The implementation does not match the reviewed boundary and "
                "must be resolved before work continues."
            )
        if hard_to_undo:
            return "The slice-level hard-to-undo point is: " + hard_to_undo
        return "The proposal does not record an item-specific risk."

    def undo_text(state: str) -> str:
        if state == "existing":
            return "Not applicable; this item is outside the planned change."
        if state in ("unplanned", "conflict"):
            return (
                "No. This item is not safely inside the reviewed reversibility "
                "envelope."
            )
        if reversibility_state == "go-no-go":
            return (
                "No. The proposal has reached its go/no-go point. Stop for "
                "explicit approval."
            )
        if reversibility_state == "reversible" and veto_scope:
            return "Yes, within this slice-level veto scope: " + veto_scope
        return "No safe undo boundary is recorded in the proposal."

    observed_dependencies = {
        normalise(item.get("path")).rstrip("/"): {
            normalise(dep).rstrip("/")
            for dep in (item.get("depends_on") or [])
            if normalise(dep)
        }
        for item in mods
        if normalise(item.get("path"))
    }
    planned_dependencies = {
        path: {
            normalise(dep).rstrip("/")
            for dep in (meta.get("may_depend_on") or [])
            if normalise(dep)
        }
        for path, meta in proposal_modules.items()
    }
    module_paths = set(observed_dependencies) | set(proposal_modules)
    folder_paths: set[str] = set(module_paths)
    for path in all_files:
        parts = path.split("/")[:-1]
        for index in range(1, len(parts) + 1):
            folder_paths.add("/".join(parts[:index]))
    if any("/" not in path for path in all_files):
        folder_paths.add("(root)")
    allowed_dependency_targets = folder_paths | module_paths
    for dependencies in list(observed_dependencies.values()) + list(
        planned_dependencies.values()
    ):
        for dependency in dependencies:
            if dependency in allowed_dependency_targets:
                folder_paths.add(dependency)

    def module_existed_at_baseline(folder: str) -> bool | None:
        if not baseline_known:
            return None
        return any(under(folder, path) for path in baseline_set)

    def aggregate_module_action(folder: str) -> str:
        if not comparison_known:
            return ""
        touched = [
            action
            for path, action in observed_actions.items()
            if under(folder, path)
        ]
        if not touched:
            return ""
        before_exists = module_existed_at_baseline(folder)
        after_exists = any(under(folder, path) for path in current_files)
        if before_exists is False and after_exists:
            return "new"
        if before_exists and not after_exists:
            return "delete"
        return "modify"

    incoming_observed: Dict[str, set[str]] = {}
    incoming_planned: Dict[str, set[str]] = {}
    for source, dependencies in observed_dependencies.items():
        for target in dependencies:
            incoming_observed.setdefault(target, set()).add(source)
    for source, dependencies in planned_dependencies.items():
        for target in dependencies:
            incoming_planned.setdefault(target, set()).add(source)

    def dependency_text(module: str, containing: bool = False) -> str:
        if not module:
            return (
                "No owning module is recorded, so reverse dependencies cannot "
                "be attributed."
            )
        current = sorted(incoming_observed.get(module, set()))
        planned = sorted(incoming_planned.get(module, set()))
        parts: List[str] = []
        subject = f"the containing module {module}" if containing else "it"
        if containing:
            parts.append(
                f"Only module-level dependency evidence is available; this item is inside {module}."
            )
        if current:
            parts.append(
                "Current modules that depend on "
                + subject
                + ": "
                + ", ".join(current)
                + "."
            )
        if planned:
            parts.append(
                "Planned modules allowed to depend on "
                + subject
                + ": "
                + ", ".join(planned)
                + "."
            )
        if len(parts) == (1 if containing else 0):
            parts.append(f"No mapped module is recorded as depending on {subject}.")
        return " ".join(parts)

    module_info: Dict[str, Dict[str, str]] = {}
    for folder in module_paths:
        meta = proposal_modules.get(folder)
        declared = str((meta or {}).get("action") or "").strip().lower()
        observed = aggregate_module_action(folder)
        state = state_for(
            declared,
            observed,
            before_exists=module_existed_at_baseline(folder),
        )
        module_info[folder] = {
            "declared": declared,
            "observed": observed,
            "state": state,
        }

    def owning_module(path: str, explicit: str = "") -> str:
        if explicit:
            return explicit
        matches = [module for module in module_paths if under(module, path)]
        return max(matches, key=lambda item: (item.count("/"), len(item))) if matches else ""

    nodes: List[Dict[str, Any]] = []
    node_ids: set[str] = set()

    def add_node(node: Dict[str, Any]) -> None:
        node_id = str(node.get("id") or "")
        if not node_id or node_id in node_ids:
            return
        node_ids.add(node_id)
        nodes.append(node)

    for folder in sorted(folder_paths, key=lambda item: (item.count("/"), item)):
        meta = proposal_modules.get(folder)
        info = module_info.get(
            folder,
            {"declared": "", "observed": "", "state": "existing"},
        )
        declared = info["declared"]
        observed = info["observed"]
        state = info["state"]
        parent_path = folder.rsplit("/", 1)[0] if "/" in folder else ""
        parent = (
            f"folder:{parent_path}"
            if parent_path in folder_paths
            else None
        )
        exists = any(under(folder, path) for path in current_files)
        role = str((meta or {}).get("role") or "").strip()
        why = str((meta or {}).get("why") or "").strip()
        add_node({
            "id": f"folder:{folder}",
            "label": "res://" if folder == "(root)" else folder,
            "path": folder,
            "kind": "folder",
            "architecture_module": folder in module_paths,
            "state": state,
            "action": declared or observed,
            "parent": parent,
            "what": role or f"A source folder in the game architecture: {folder}.",
            "changing": change_text(
                folder,
                "module",
                state,
                declared,
                observed,
                exists,
                comparison_known,
            ),
            "why": why or (
                "No change is proposed for this folder."
                if state == "existing"
                else "No reason is recorded for this module change."
            ),
            "design": design_text(state),
            "depends": dependency_text(folder),
            "risk": risk_text(state),
            "undo": undo_text(state),
            "check": check_text,
            "options": considered if state == "adding" and considered else [],
            "responsibility": "" if state == "removing" else None,
        })

    proposal_functions: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for item in rows(prop, "functions"):
        file_path = normalise(item.get("file"))
        raw_signature = str(item.get("signature") or "")
        try:
            parsed = gd_signature.parse_proposal_signature(raw_signature)
        except gd_signature.SignatureError:
            continue
        class_scope = str(item.get("class_scope") or "").strip()
        identity = f"{class_scope}.{parsed.name}" if class_scope else parsed.name
        proposal_functions[(file_path, identity)] = {
            **item,
            "canonical": parsed.canonical,
            "identity": identity,
            "class_scope": class_scope,
        }

    for file_path in sorted(all_files):
        direct_meta = proposal_files.get(file_path)
        explicit_module = normalise((direct_meta or {}).get("module")).rstrip("/")
        owner = owning_module(file_path, explicit_module)
        inherited_module = (
            proposal_modules.get(owner)
            if involvement == "module" and direct_meta is None
            else None
        )
        meta = direct_meta or inherited_module
        if inherited_module is not None:
            info = module_info.get(
                owner,
                {"declared": "", "observed": "", "state": "existing"},
            )
            declared = info["declared"]
            observed = info["observed"]
            state = info["state"]
            change_known = comparison_known
        else:
            declared = str((meta or {}).get("action") or "").strip().lower()
            observed = observed_actions.get(file_path, "")
            state = state_for(
                declared,
                observed,
                before_exists=(file_path in baseline_set if baseline_known else None),
            )
            change_known = comparison_known
        exists = file_path in current_files
        parent_path = file_path.rsplit("/", 1)[0] if "/" in file_path else ""
        parent = (
            f"folder:{parent_path}"
            if parent_path in folder_paths
            else f"folder:(root)"
            if "(root)" in folder_paths
            else None
        )
        why = str((meta or {}).get("why") or "").strip()
        add_node({
            "id": f"file:{file_path}",
            "label": Path(file_path).name,
            "path": file_path,
            "kind": "file",
            "state": state,
            "action": declared or observed,
            "parent": parent,
            "what": f"The authored game file {file_path}.",
            "changing": change_text(
                file_path,
                "file",
                state,
                declared,
                observed,
                exists,
                change_known,
            ),
            "why": why or (
                "No change is proposed for this file."
                if state == "existing"
                else "This file changed without a recorded plan reason."
            ),
            "design": design_text(state),
            "depends": dependency_text(owner, containing=True),
            "risk": risk_text(state),
            "undo": undo_text(state),
            "check": check_text,
            "options": considered if state == "adding" and considered else [],
            "responsibility": "" if state == "removing" else None,
        })

        source = observed_tree.get(file_path, {})
        source_functions = source.get("functions") or []
        class_names: set[str] = set()
        source_class = str(source.get("class_name") or "").strip()
        if source_class:
            class_names.add(source_class)
        for function in source_functions:
            if isinstance(function, dict):
                scope = str(function.get("class_scope") or "").strip()
                if scope:
                    class_names.add(scope)
        for (function_file, _identity), function in proposal_functions.items():
            if function_file == file_path and function.get("class_scope"):
                class_names.add(str(function["class_scope"]))

        for class_name in sorted(class_names):
            add_node({
                "id": f"class:{file_path}::{class_name}",
                "label": class_name,
                "path": file_path,
                "kind": "module",
                "state": state,
                "action": declared or observed,
                "parent": f"file:{file_path}",
                "what": f"The GDScript class {class_name}, declared in {file_path}.",
                "changing": change_text(
                    file_path,
                    "class",
                    state,
                    declared,
                    observed,
                    exists,
                    change_known,
                ),
                "why": why or (
                    "No change is proposed for this class."
                    if state == "existing"
                    else "The proposal records the enclosing boundary reason."
                ),
                "design": design_text(state),
                "depends": dependency_text(owner, containing=True),
                "risk": risk_text(state),
                "undo": undo_text(state),
                "check": check_text,
                "options": considered if state == "adding" and considered else [],
                "responsibility": "" if state == "removing" else None,
            })

        observed_identities: set[str] = set()
        for function in source_functions:
            if not isinstance(function, dict):
                continue
            identity = str(
                function.get("identity") or function.get("name") or ""
            ).strip()
            raw_signature = str(function.get("signature") or "").strip()
            if not identity or not raw_signature:
                continue
            planned = proposal_functions.get((file_path, identity))
            evidence = observed_function_changes.get((file_path, identity), {})
            actual_action = str(evidence.get("action") or "").strip().lower()
            baseline_signature = str(
                evidence.get("baseline_signature") or ""
            ).strip()
            function_action = str(
                (planned or {}).get("action") or ""
            ).strip().lower()
            canonical = ""
            try:
                canonical = gd_signature.parse_proposal_signature(
                    raw_signature
                ).canonical
            except gd_signature.SignatureError:
                pass
            planned_matches = bool(
                planned
                and canonical
                and canonical == str(planned.get("canonical") or "")
            )
            signature_mismatch = bool(
                planned
                and function_action in ("new", "modify")
                and not planned_matches
            )
            if planned:
                function_state = state_for(
                    function_action,
                    actual_action,
                    before_exists=bool(baseline_signature),
                    known=function_comparison_known,
                )
            elif actual_action in action_states:
                function_state = "unplanned"
            elif not function_comparison_known and observed_actions.get(file_path):
                function_state = "unplanned"
            else:
                function_state = "existing"
            if signature_mismatch:
                function_state = "conflict"
                changing = (
                    "The observed signature does not match the planned signature: "
                    + str(planned.get("canonical") or "")
                )
            elif function_state == "conflict":
                changing = change_text(
                    raw_signature,
                    "function",
                    function_state,
                    function_action,
                    actual_action,
                    True,
                    function_comparison_known,
                )
            elif function_action == "delete":
                changing = "The function is planned for removal and is still present."
            elif planned_matches:
                if actual_action == "new":
                    changing = "The exact planned function addition is present in source."
                elif actual_action == "modify":
                    changing = "The exact planned function change is present in source."
                elif function_comparison_known:
                    changing = (
                        "The exact planned signature is present, but no function body "
                        "or signature change is observed from the baseline."
                    )
                else:
                    changing = (
                        "The exact planned signature is present, but baseline function "
                        "comparison is unavailable."
                    )
            elif function_state == "unplanned" and actual_action:
                changing = (
                    f"Git reports an unplanned {actual_action} to this function. "
                    "It is outside the recorded function-level plan."
                )
            elif function_state == "unplanned":
                changing = (
                    "The containing file changed, but exact function comparison is "
                    "unavailable. This function may or may not have changed."
                )
            else:
                changing = "No change is proposed for this function."
            if planned and not signature_mismatch:
                observed_identities.add(identity)
            class_scope = str(function.get("class_scope") or "").strip()
            parent_class = class_scope or source_class
            parent_id = (
                f"class:{file_path}::{parent_class}"
                if parent_class in class_names
                else f"file:{file_path}"
            )
            function_why = str((planned or {}).get("why") or "").strip()
            observed_node_id = f"function:{file_path}::{identity}"
            if signature_mismatch:
                observed_node_id += "::observed"
            add_node({
                "id": observed_node_id,
                "label": str(function.get("name") or identity),
                "signature": raw_signature,
                "path": file_path,
                "kind": "function",
                "state": function_state,
                "action": function_action or actual_action,
                "parent": parent_id,
                "what": f"The typed function {raw_signature} in {file_path}.",
                "changing": changing,
                "why": (
                    "This observed function does not satisfy the exact function-level plan."
                    if signature_mismatch or function_state == "conflict"
                    else function_why
                    or (
                        "This function changed without a recorded function-level reason."
                        if function_state == "unplanned"
                        else "No change is proposed for this function."
                    )
                ),
                "design": design_text(function_state),
                "depends": (
                    "Function-level callers are not available from the "
                    "architecture index."
                ),
                "risk": risk_text(function_state),
                "undo": undo_text(function_state),
                "check": check_text,
                "options": (
                    considered
                    if function_state == "adding" and considered
                    else []
                ),
                "responsibility": "" if function_state == "removing" else None,
            })

        for (function_file, identity), planned in sorted(
            proposal_functions.items()
        ):
            if function_file != file_path or identity in observed_identities:
                continue
            action = str(planned.get("action") or "").strip().lower()
            evidence = observed_function_changes.get((file_path, identity), {})
            actual_action = str(evidence.get("action") or "").strip().lower()
            baseline_signature = str(
                evidence.get("baseline_signature") or ""
            ).strip()
            state = state_for(
                action,
                actual_action,
                before_exists=(
                    bool(baseline_signature)
                    if function_comparison_known
                    else None
                ),
                known=function_comparison_known,
            )
            class_scope = str(planned.get("class_scope") or "").strip()
            parent_id = (
                f"class:{file_path}::{class_scope}"
                if class_scope in class_names
                else f"file:{file_path}"
            )
            signature = str(
                planned.get("canonical") or planned.get("signature") or ""
            )
            why = str(planned.get("why") or "").strip()
            if action == "delete":
                if actual_action == "delete" and function_comparison_known:
                    changing = "The planned function removal is complete."
                elif state == "conflict":
                    changing = change_text(
                        signature,
                        "function",
                        state,
                        action,
                        actual_action,
                        False,
                        function_comparison_known,
                    )
                else:
                    changing = (
                        "The function is absent, but baseline function comparison is "
                        "unavailable, so the planned removal is not proven."
                    )
            elif action == "new":
                changing = (
                    change_text(
                        signature,
                        "function",
                        state,
                        action,
                        actual_action,
                        False,
                        function_comparison_known,
                    )
                    if state == "conflict"
                    else "The planned function is not present in source yet."
                )
            elif action == "modify":
                changing = (
                    change_text(
                        signature,
                        "function",
                        state,
                        action,
                        actual_action,
                        False,
                        function_comparison_known,
                    )
                    if state == "conflict"
                    else "The planned function signature is not present in source yet."
                )
            else:
                changing = "No implementation claim is available."
            add_node({
                "id": f"function:{file_path}::{identity}",
                "label": identity.rsplit(".", 1)[-1],
                "signature": signature,
                "path": file_path,
                "kind": "function",
                "state": state,
                "action": action,
                "parent": parent_id,
                "what": f"The typed function {signature} in {file_path}.",
                "changing": changing,
                "why": why or "No function-level reason is recorded.",
                "design": design_text(state),
                "depends": (
                    "Function-level callers are not available from the "
                    "architecture index."
                ),
                "risk": risk_text(state),
                "undo": undo_text(state),
                "check": check_text,
                "options": considered if state == "adding" and considered else [],
                "responsibility": "" if state == "removing" else None,
            })

        current_identities = {
            str(function.get("identity") or function.get("name") or "").strip()
            for function in source_functions
            if isinstance(function, dict)
        }
        for (changed_file, identity), evidence in sorted(
            observed_function_changes.items()
        ):
            if (
                changed_file != file_path
                or identity in current_identities
                or (file_path, identity) in proposal_functions
                or str(evidence.get("action") or "") != "delete"
            ):
                continue
            signature = str(
                evidence.get("baseline_signature")
                or evidence.get("signature")
                or ""
            )
            add_node({
                "id": f"function:{file_path}::{identity}::deleted",
                "label": identity.rsplit(".", 1)[-1],
                "signature": signature,
                "path": file_path,
                "kind": "function",
                "state": "unplanned",
                "action": "delete",
                "parent": f"file:{file_path}",
                "what": f"The deleted function {signature} from {file_path}.",
                "changing": (
                    "Git reports an unplanned delete of this function. It is "
                    "outside the recorded function-level plan."
                ),
                "why": "No function-level reason is recorded for this deletion.",
                "design": design_text("unplanned"),
                "depends": (
                    "Function-level callers are not available from the "
                    "architecture index."
                ),
                "risk": risk_text("unplanned"),
                "undo": undo_text("unplanned"),
                "check": check_text,
                "options": [],
                "responsibility": "",
            })

    link_index: Dict[Tuple[str, str], set[str]] = {}
    for provenance, dependency_map in (
        ("observed", observed_dependencies),
        ("planned", planned_dependencies),
    ):
        for module_path, dependencies in dependency_map.items():
            for dependency in dependencies:
                source = f"folder:{module_path}"
                target = f"folder:{dependency}"
                if (
                    source in node_ids
                    and target in node_ids
                    and target != source
                ):
                    link_index.setdefault((source, target), set()).add(provenance)
    links = [
        {
            "source": source,
            "target": target,
            "relation": "uses",
            "provenance": "+".join(sorted(provenance)),
        }
        for (source, target), provenance in sorted(link_index.items())
    ]

    kind_order = {"folder": 0, "file": 1, "module": 2, "function": 3}
    nodes.sort(key=lambda item: (
        str(item.get("path") or ""),
        kind_order.get(str(item.get("kind") or ""), 9),
        str(item.get("id") or ""),
    ))
    return {
        "nodes": nodes,
        "links": links,
        "comparison_known": comparison_known,
        "function_comparison_known": function_comparison_known,
    }
def _script_json(value: Any) -> str:
    """JSON safe inside an executable script element."""
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _architecture_list(nodes: List[Dict[str, Any]], interactive: bool = True) -> str:
    state_names = {
        "existing": "Not affected",
        "adding": "Being added",
        "changing": "Being changed",
        "removing": "Being removed",
        "unplanned": "Not in plan",
        "conflict": "Plan mismatch",
    }
    state_marks = {
        "existing": "•",
        "adding": "+",
        "changing": "Δ",
        "removing": "×",
        "unplanned": "!",
        "conflict": "!",
    }
    items: List[str] = []
    for node in nodes:
        node_id = str(node.get("id") or "")
        label = str(node.get("signature") or node.get("path") or node.get("label") or "")
        state = str(node.get("state") or "existing")
        content = (
            f'<span aria-hidden="true">{esc(state_marks.get(state, "•"))}</span>'
            f'<span>{esc(label)}</span>'
            f'<span class="arch-list-state">{esc(state_names.get(state, state))}</span>'
        )
        if interactive:
            content = (
                f'<button type="button" class="arch-list-button" '
                f'data-arch-node="{esc(node_id)}">{content}</button>'
            )
        items.append(
            f'<li data-arch-item="{esc(node_id)}" '
            f'data-arch-kind="{esc(node.get("kind"))}">{content}</li>'
        )
    return '<ul class="arch-fallback-list">' + "".join(items) + "</ul>"


def _architecture_nodes_for_involvement(
    nodes: List[Dict[str, Any]], involvement: str
) -> List[Dict[str, Any]]:
    """Keep the rendered hierarchy at the depth the human chose to review."""
    if involvement == "module":
        return [
            node
            for node in nodes
            if node.get("kind") == "folder" and node.get("architecture_module")
        ]
    if involvement == "file":
        return [node for node in nodes if node.get("kind") in ("folder", "file")]
    return list(nodes)


def architecture_map_html(model: Dict[str, Any], involvement: str) -> str:
    all_nodes = model.get("nodes") if isinstance(model.get("nodes"), list) else []
    maximum = involvement if involvement in ("module", "file", "function") else "function"
    nodes = _architecture_nodes_for_involvement(all_nodes, maximum)
    if not nodes:
        return ""
    default = maximum
    node_ids = {str(node.get("id") or "") for node in nodes}
    rendered_model = {
        **model,
        "nodes": nodes,
        "links": [
            link
            for link in (model.get("links") or [])
            if isinstance(link, dict)
            and str(link.get("source") or "") in node_ids
            and str(link.get("target") or "") in node_ids
        ],
    }

    def depth_button(value: str, label: str) -> str:
        order = {"module": 1, "file": 2, "function": 3}
        disabled = order[value] > order[maximum]
        return (
            f'<button type="button" data-arch-depth="{value}" '
            f'aria-pressed="{str(value == default).lower()}"'
            + (" disabled" if disabled else "")
            + f'>{label}</button>'
        )

    level_label = {
        "module": "Module-level review",
        "file": "File-level review",
        "function": "Function-level review",
    }.get(maximum, "Architecture review")
    structure_copy = {
        "module": (
            "Start with the coloured changes. The architecture modules and their "
            "dependencies stay still while you inspect them."
        ),
        "file": (
            "Start with the coloured changes. The complete folder → file structure "
            "stays still while you inspect it."
        ),
        "function": (
            "Start with the coloured changes. The complete folder → file → class → "
            "function structure stays still while you inspect it."
        ),
    }[maximum]
    search_placeholder = {
        "module": "Module",
        "file": "Folder or file",
        "function": "Folder, file, class or function",
    }[maximum]
    map_heading = {
        "module": "Architecture modules",
        "file": "Folder and file structure",
        "function": "Complete structure",
    }[maximum]
    fallback = _architecture_list(nodes)
    noscript = _architecture_list(nodes, interactive=False)
    return "".join([
        '<section class="architecture-map" id="architecture-map" '
        f'data-max-depth="{esc(maximum)}" aria-label="Architecture change map">',
        '<div class="arch-intro"><div>',
        f'<p class="arch-eyebrow">{esc(level_label)}</p>',
        '<h2>Architecture change map</h2>',
        f'<p class="arch-intro-copy">{esc(structure_copy)}</p>',
        '</div><span class="arch-settled" id="arch-settled" hidden>'
        'Layout settled — it will not keep moving</span></div>',
        '<div class="arch-read-path" aria-label="How to read this map">',
        '<div class="arch-read-step"><span class="arch-step-number">1</span><span>'
        '<strong>Find the change</strong>Colour shows what is affected.</span></div>',
        '<div class="arch-read-step"><span class="arch-step-number">2</span><span>'
        '<strong>Click one item</strong>Read why it changes and what could break.</span></div>',
        '<div class="arch-read-step"><span class="arch-step-number">3</span><span>'
        '<strong>Check additions</strong>See what existing code was considered first.</span></div>',
        '</div>',
        '<div class="arch-controls" aria-label="Architecture map controls">',
        '<div class="arch-control"><span class="arch-control-label">Detail shown</span>'
        '<div class="arch-buttons" role="group" aria-label="Detail shown">',
        depth_button("module", "Modules"),
        depth_button("file", "Files"),
        depth_button("function", "Functions"),
        '</div></div>',
        '<div class="arch-control"><span class="arch-control-label">Map focus</span>'
        '<div class="arch-buttons" role="group" aria-label="Map focus">'
        '<button type="button" data-arch-view="complete" aria-pressed="true">Complete</button>'
        '<button type="button" data-arch-view="changes" aria-pressed="false">Changes</button>'
        '</div></div>',
        '<label class="arch-control"><span class="arch-control-label">Find an item</span>'
        '<span class="arch-search-wrap"><input class="arch-search" id="arch-search" '
        f'type="search" placeholder="{esc(search_placeholder)}" autocomplete="off">'
        '<span class="arch-search-count" id="arch-search-count"></span></span></label>',
        '<div class="arch-control" id="arch-camera-controls"><span class="arch-control-label">Camera</span>'
        '<div class="arch-buttons"><button type="button" id="arch-fit-changes">Fit changes</button>'
        '<button type="button" id="arch-fit-all">Fit all</button>'
        '<button type="button" id="arch-back" hidden>Back</button></div></div>',
        '</div>',
        '<div class="arch-workspace"><div class="arch-map-panel">',
        f'<div class="arch-map-heading"><h3>{esc(map_heading)}</h3>'
        '<span class="arch-map-status" id="arch-map-status">Preparing map…</span></div>',
        '<div class="arch-graph-shell" id="arch-graph-shell">'
        '<p class="arch-sr">A stable, zoomable source map. Select a coloured node to read '
        'its name, change and rationale.</p>'
        '<svg class="arch-graph" id="arch-graph" role="group" '
        'aria-label="Keyboard-selectable architecture items"></svg>'
        '<p class="arch-zoom-hint">Scroll or pinch to zoom · drag empty space to move</p></div>',
        '<div class="arch-legend" id="arch-legend" aria-label="Change legend">'
        '<span class="arch-key existing"><span class="arch-key-mark"></span>Not affected</span>'
        '<span class="arch-key adding"><span class="arch-key-mark"></span>Being added</span>'
        '<span class="arch-key changing"><span class="arch-key-mark"></span>Being changed</span>'
        '<span class="arch-key removing"><span class="arch-key-mark"></span>Being removed</span>'
        '<span class="arch-key unplanned"><span class="arch-key-mark"></span>Not in plan</span>'
        '<span class="arch-key conflict"><span class="arch-key-mark"></span>Plan mismatch</span></div>',
        '<details class="arch-fallback" id="arch-fallback" hidden>'
        '<summary>Graph unavailable — item list</summary>', fallback, '</details>',
        '<noscript><style>.architecture-map .arch-controls,.architecture-map .arch-read-path,'
        '.architecture-map .arch-map-heading,.architecture-map .arch-graph-shell,'
        '.architecture-map .arch-legend,.architecture-map .arch-fallback,'
        '.architecture-map .arch-inspector,.architecture-map .arch-rule-note,'
        '.architecture-map .arch-settled{display:none!important}'
        '.architecture-map .arch-workspace{display:block!important}</style>'
        '<div class="arch-noscript"><b>Graph unavailable — item list</b>'
        '<p>JavaScript is off, so the readable source list is shown instead.</p>',
        noscript, '</div></noscript>',
        '</div><aside class="arch-inspector" aria-labelledby="arch-selected-title">',
        '<header class="arch-inspector-header"><p class="arch-eyebrow">Selected item</p>'
        '<h3 id="arch-selected-title">Select a coloured item</h3><div class="arch-tags">'
        '<span class="arch-tag" id="arch-kind-tag">Item</span>'
        '<span class="arch-tag" id="arch-state-tag">Not affected</span></div></header>',
        '<div class="arch-inspector-body" id="arch-inspector-body" aria-live="polite"></div>',
        '</aside></div>',
        '<p class="arch-rule-note"><strong>This is a decision aid, not the decision itself.</strong>'
        '<span>The player outcome and approval actions remain above it.</span></p>',
        '</section>',
        '<script>window.__KIT_ARCHITECTURE_MAP__=', _script_json(rendered_model), ';</script>',
        ARCHITECTURE_MAP_JS,
    ])


ARCHITECTURE_MAP_JS = r"""
<script>
(function(){
  "use strict";
  var root = document.getElementById("architecture-map");
  var model = window.__KIT_ARCHITECTURE_MAP__;
  if (!root || !model || !Array.isArray(model.nodes) || !model.nodes.length) return;
  try { delete window.__KIT_ARCHITECTURE_MAP__; } catch (_ignored) {}

  var shell = document.getElementById("arch-graph-shell");
  var graph = document.getElementById("arch-graph");
  var legend = document.getElementById("arch-legend");
  var camera = document.getElementById("arch-camera-controls");
  var fallback = document.getElementById("arch-fallback");
  var status = document.getElementById("arch-map-status");
  var search = document.getElementById("arch-search");
  var searchCount = document.getElementById("arch-search-count");
  var selectedTitle = document.getElementById("arch-selected-title");
  var kindTag = document.getElementById("arch-kind-tag");
  var stateTag = document.getElementById("arch-state-tag");
  var inspector = document.getElementById("arch-inspector-body");
  var back = document.getElementById("arch-back");
  var settled = document.getElementById("arch-settled");
  var stateNames = {existing:"Not affected",adding:"Being added",changing:"Being changed",
                    removing:"Being removed",unplanned:"Not in plan",conflict:"Plan mismatch"};
  var kindNames = {folder:"Folder / module",file:"File",module:"GDScript class",function:"Function"};
  var kindDepth = {folder:0,file:1,module:2,function:3};
  var maxDepth = {module:0,file:1,function:3};
  var activeDepth = root.getAttribute("data-max-depth") || "function";
  var activeView = "complete";
  var graphFailed = false;
  var recoverableFallback = false;
  var focusId = null;
  var selectedId = (model.nodes.find(function(node){return node.state !== "existing";}) || model.nodes[0]).id;
  var renderedNodes = [];
  var renderedLinks = [];
  var positions = new Map();
  var nodeElements = new Map();
  var edgeElements = [];
  var viewport = null;
  var transform = {x:0,y:0,k:1};
  var panning = null;
  var graphFocus = false;
  var zeroSizeSince = 0;
  var zeroSizeTimer = null;
  var renderGeneration = 0;
  var maximumGraphNodes = 2000;
  var maximumGraphLinks = 12000;
  var svgNS = "http://www.w3.org/2000/svg";
  var byId = new Map(model.nodes.map(function(node){ return [node.id,node]; }));

  function escapeHtml(value){
    return String(value || "").replace(/&/g,"&amp;").replace(/</g,"&lt;")
      .replace(/>/g,"&gt;").replace(/\"/g,"&quot;");
  }
  function descendantsOf(id){
    var result = new Set([id]);
    var changed = true;
    while (changed) {
      changed = false;
      model.nodes.forEach(function(node){
        if (node.parent && result.has(node.parent) && !result.has(node.id)) {
          result.add(node.id); changed = true;
        }
      });
    }
    return result;
  }
  function focusSet(){
    if (!focusId) return null;
    var result = descendantsOf(focusId);
    var current = byId.get(focusId);
    while (current && current.parent) {
      result.add(current.parent); current = byId.get(current.parent);
    }
    return result;
  }
  function changedSet(){
    if (activeView !== "changes") return null;
    var result = new Set();
    model.nodes.forEach(function(node){
      if (node.state === "existing") return;
      result.add(node.id);
      var current = node;
      while (current && current.parent) {
        result.add(current.parent); current = byId.get(current.parent);
      }
    });
    return result;
  }
  function visibleNodes(){
    var allowed = focusSet();
    var changed = changedSet();
    return model.nodes.filter(function(node){
      if ((kindDepth[node.kind] || 0) > maxDepth[activeDepth]) return false;
      if (activeDepth === "module" &&
          (node.kind !== "folder" || !node.architecture_module)) return false;
      if (allowed && !allowed.has(node.id)) return false;
      return !changed || changed.has(node.id);
    });
  }
  function nearestParent(node, visibleIds){
    var parent = node.parent;
    while (parent) {
      if (visibleIds.has(parent)) return parent;
      parent = (byId.get(parent) || {}).parent || null;
    }
    return null;
  }
  function sourceLinks(visible){
    var ids = new Set(visible.map(function(node){return node.id;}));
    var links = [];
    visible.forEach(function(node){
      var parent = nearestParent(node, ids);
      if (parent) links.push({source:parent,target:node.id,relation:"contains"});
    });
    (model.links || []).forEach(function(link){
      if (ids.has(link.source) && ids.has(link.target)) links.push(link);
    });
    return links;
  }
  function renderInspector(){
    var visible = visibleNodes();
    var node = byId.get(selectedId);
    if (!node || !visible.some(function(item){return item.id === node.id;})) {
      node = visible.find(function(item){return item.state !== "existing";}) || visible[0];
      if (!node) {
        selectedTitle.textContent = "No items in this view";
        kindTag.textContent = "Item"; stateTag.textContent = "Not affected";
        inspector.innerHTML = '<p class="empty">Choose Complete to restore the full structure.</p>';
        return;
      }
      selectedId = node.id;
    }
    selectedTitle.textContent = node.signature || node.path || node.label;
    kindTag.textContent = kindNames[node.kind] || node.kind;
    stateTag.textContent = stateNames[node.state] || node.state;
    stateTag.setAttribute("data-state", node.state);
    var options = "";
    if (Array.isArray(node.options) && node.options.length) {
      options = '<section class="arch-special"><h4>Why not extend existing code?</h4>'
        + '<p class="m">These are slice-level considerations. The current proposal does not link '
        + 'each one to this exact addition.</p>'
        + node.options.map(function(option){
          return '<div class="arch-option"><strong>' + escapeHtml(option.name) + '</strong><span>'
            + escapeHtml(option.answer) + '</span></div>';
        }).join("") + '</section>';
    }
    var responsibility = "";
    if (node.state === "removing") {
      responsibility = '<section class="arch-special"><h4>Where does this responsibility go?</h4><p>'
        + escapeHtml(node.responsibility || "The proposal does not record a responsibility transfer. Resolve this before approving the removal.")
        + '</p></section>';
    }
    inspector.innerHTML = '<dl>'
      + '<div><dt>What is this?</dt><dd>' + escapeHtml(node.what) + '</dd></div>'
      + '<div><dt>What is changing?</dt><dd>' + escapeHtml(node.changing) + '</dd></div>'
      + '<div><dt>Why are we changing it?</dt><dd>' + escapeHtml(node.why) + '</dd></div>'
      + '</dl>' + options + responsibility + '<dl class="arch-special">'
      + '<div><dt>Does this match the approved design?</dt><dd>' + escapeHtml(node.design) + '</dd></div>'
      + '<div><dt>What depends on it?</dt><dd>' + escapeHtml(node.depends) + '</dd></div>'
      + '<div><dt>What could break?</dt><dd>' + escapeHtml(node.risk) + '</dd></div>'
      + '<div><dt>Can it be safely undone?</dt><dd>' + escapeHtml(node.undo) + '</dd></div>'
      + '<div><dt>How will we check it?</dt><dd>' + escapeHtml(node.check) + '</dd></div>'
      + '</dl>' + (graphFailed ? '' : '<button type="button" class="arch-primary" id="arch-focus">Focus this branch</button>');
    var focus = document.getElementById("arch-focus");
    if (focus) focus.addEventListener("click", guarded(function(){
      focusId = node.id; graphFocus = false; back.hidden = false; renderGraph(true);
    }));
    updateGraphFocus();
  }
  function syncRovingTabIndex(){
    nodeElements.forEach(function(element,id){
      element.setAttribute("tabindex",id===selectedId ? "0" : "-1");
    });
  }
  function selectNode(id, moveCamera){
    if (!byId.has(id)) return;
    selectedId = id; graphFocus = true; renderInspector(); updateGraphFocus();
    if (moveCamera && !graphFailed) fitNodes([id], 2.2);
  }
  function renderFallbackList(){
    var visible = new Set(visibleNodes().map(function(node){return node.id;}));
    var query = search.value.trim().toLowerCase();
    var matches = 0;
    root.querySelectorAll("[data-arch-item]").forEach(function(item){
      var id = item.getAttribute("data-arch-item");
      var node = byId.get(id);
      var match = !query || (node && ((node.path || "") + " " + (node.signature || "")
        + " " + (node.label || "")).toLowerCase().indexOf(query) >= 0);
      item.hidden = !visible.has(id) || !match;
      if (!item.hidden) matches += 1;
    });
    searchCount.textContent = query ? String(matches) : "";
  }
  function showFallback(error,recoverable){
    graphFailed = true;
    recoverableFallback = Boolean(recoverable);
    renderGeneration += 1; clearTimeout(zeroSizeTimer);
    if(settled)settled.hidden=true;
    focusId = null; graphFocus = false; back.hidden = true;
    root.setAttribute("data-render-mode", "fallback");
    root.setAttribute("data-recoverable-fallback",String(recoverableFallback));
    shell.hidden = true; legend.hidden = true; camera.hidden = true;
    fallback.hidden = false; fallback.open = true;
    status.textContent = recoverableFallback
      ? "This view is too large for a responsive graph · readable item list shown"
      : "Graph unavailable · readable item list shown";
    renderFallbackList(); renderInspector();
    if (error && window.console) {
      if(recoverableFallback && console.warn)console.warn("Architecture graph used its responsive list fallback.",error);
      else if(console.error)console.error("Architecture graph could not render.", error);
    }
  }
  function guarded(callback){
    return function(){
      try { return callback.apply(this, arguments); }
      catch (error) { showFallback(error); }
    };
  }
  function radius(node){
    return node.kind === "folder" ? 8 : node.kind === "file" ? 7 : node.kind === "module" ? 6 : 4.5;
  }
  function hash(value){
    var result = 2166136261;
    for (var i=0;i<value.length;i+=1) { result ^= value.charCodeAt(i); result = Math.imul(result,16777619); }
    return result >>> 0;
  }
  function layout(visible,width,height){
    var visibleIds = new Set(visible.map(function(node){return node.id;}));
    function rootOf(node){
      var current = node;
      while (current.parent && visibleIds.has(current.parent)) current = byId.get(current.parent);
      return current.id;
    }
    var roots = Array.from(new Set(visible.map(rootOf))).sort();
    var ratio = Math.max(.6, width / Math.max(1,height));
    var columns = Math.max(1, Math.ceil(Math.sqrt(roots.length * ratio)));
    var rowsCount = Math.max(1, Math.ceil(roots.length / columns));
    var cellWidth = width / columns;
    var cellHeight = height / rowsCount;
    var result = new Map();
    var children = new Map();
    visible.forEach(function(node){
      var parent = nearestParent(node,visibleIds);
      if (!parent) return;
      if (!children.has(parent)) children.set(parent,[]);
      children.get(parent).push(node);
    });
    children.forEach(function(list){list.sort(function(a,b){return a.id.localeCompare(b.id);});});
    roots.forEach(function(rootId,index){
      var column = index % columns;
      var row = Math.floor(index / columns);
      result.set(rootId,{x:(column+.5)*cellWidth,y:(row+.5)*cellHeight});
      var queue = [rootId], queueIndex = 0;
      while (queueIndex < queue.length) {
        var parentId = queue[queueIndex]; queueIndex += 1;
        var parentPos = result.get(parentId);
        var list = children.get(parentId) || [];
        var offset = (hash(parentId)%360)*Math.PI/180;
        var ringIndex=0,ringStart=0,ring=42;
        var ringCapacity=Math.max(6,Math.floor(Math.PI*2*ring/26));
        list.forEach(function(child,childIndex){
          while(childIndex-ringStart>=ringCapacity){
            ringStart+=ringCapacity;ringIndex+=1;ring=42+ringIndex*30;
            ringCapacity=Math.max(6,Math.floor(Math.PI*2*ring/26));
          }
          var slot=childIndex-ringStart;
          var angle=offset+slot*Math.PI*2/ringCapacity;
          result.set(child.id,{x:parentPos.x+Math.cos(angle)*ring,
                               y:parentPos.y+Math.sin(angle)*ring});
          queue.push(child.id);
        });
      }
    });
    var iterations = visible.length > 1000 ? 2 : visible.length > 400 ? 5 : 10;
    var cellSize = 28;
    for (var tick=0;tick<iterations;tick+=1) {
      var buckets = new Map();
      for (var i=0;i<visible.length;i+=1) {
        var nodeA=visible[i], a=result.get(nodeA.id);
        var cellX=Math.floor(a.x/cellSize), cellY=Math.floor(a.y/cellSize);
        for (var offsetX=-1;offsetX<=1;offsetX+=1) {
          for (var offsetY=-1;offsetY<=1;offsetY+=1) {
            var nearby=buckets.get((cellX+offsetX)+":"+(cellY+offsetY)) || [];
            nearby.forEach(function(nodeB){
              var b=result.get(nodeB.id);
              var dx=b.x-a.x,dy=b.y-a.y,distance=Math.sqrt(dx*dx+dy*dy)||.01;
              var minimum=radius(nodeA)+radius(nodeB)+6;
              if(distance>=minimum)return;
              var shift=(minimum-distance)/2,ux=dx/distance,uy=dy/distance;
              a.x-=ux*shift;a.y-=uy*shift;b.x+=ux*shift;b.y+=uy*shift;
            });
          }
        }
        var key=cellX+":"+cellY;
        if(!buckets.has(key))buckets.set(key,[]);
        buckets.get(key).push(nodeA);
      }
    }
    return result;
  }
  function svgElement(tag,attrs){
    if (!document.createElementNS) throw new Error("SVG creation is unavailable");
    var element=document.createElementNS(svgNS,tag);
    Object.keys(attrs || {}).forEach(function(name){element.setAttribute(name,String(attrs[name]));});
    return element;
  }
  function applyTransform(){
    if (viewport) viewport.setAttribute("transform","translate("+transform.x+" "+transform.y+") scale("+transform.k+")");
  }
  function fitNodes(ids,maximum){
    if (!viewport || !ids.length) return;
    var points=ids.map(function(id){return positions.get(id);}).filter(Boolean);
    if (!points.length) return;
    var width=shell.clientWidth || 800, height=shell.clientHeight || 500;
    var minX=Math.min.apply(null,points.map(function(p){return p.x;}))-32;
    var maxX=Math.max.apply(null,points.map(function(p){return p.x;}))+32;
    var minY=Math.min.apply(null,points.map(function(p){return p.y;}))-32;
    var maxY=Math.max.apply(null,points.map(function(p){return p.y;}))+32;
    var scale=Math.min(maximum || 1.5,.88/Math.max((maxX-minX)/width,(maxY-minY)/height));
    if (!isFinite(scale) || scale<=0) scale=1;
    transform={x:width/2-scale*(minX+maxX)/2,y:height/2-scale*(minY+maxY)/2,k:scale};
    applyTransform();
  }
  function changeContextIds(){
    var result=new Set();
    renderedNodes.forEach(function(node){
      if (node.state === "existing") return;
      result.add(node.id); var current=node;
      while (current && current.parent) { result.add(current.parent); current=byId.get(current.parent); }
    });
    return Array.from(result);
  }
  function updateGraphFocus(){
    if (!viewport) return;
    viewport.setAttribute("data-focus",String(graphFocus));
    var related=new Set([selectedId]);
    renderedLinks.forEach(function(link){
      if (link.source===selectedId) related.add(link.target);
      if (link.target===selectedId) related.add(link.source);
    });
    nodeElements.forEach(function(element,id){
      element.setAttribute("data-selected",String(id===selectedId));
      element.setAttribute("data-dimmed",String(graphFocus && !related.has(id)));
    });
    edgeElements.forEach(function(pair){
      pair.element.setAttribute("data-connected",String(graphFocus &&
        (pair.link.source===selectedId || pair.link.target===selectedId)));
    });
    syncRovingTabIndex();
  }
  function updateSearch(){
    var query=search.value.trim().toLowerCase(), matches=[];
    renderedNodes.forEach(function(node){
      if (query && ((node.path||"")+" "+(node.signature||"")+" "+(node.label||""))
          .toLowerCase().indexOf(query)>=0) matches.push(node);
    });
    searchCount.textContent=query ? String(matches.length) : "";
    var matchIds=new Set(matches.map(function(node){return node.id;}));
    nodeElements.forEach(function(element,id){
      element.setAttribute("data-match",String(matchIds.has(id)));
    });
    if (graphFailed) renderFallbackList();
    return matches;
  }
  function hasVisibleLayout(){
    if (document.hidden === true) return false;
    if (root.getClientRects) {
      try { return root.getClientRects().length > 0; }
      catch (_ignored) { return true; }
    }
    return true;
  }
  function scheduleFrame(callback){
    // Animation frames can stop entirely in a background cockpit tab.  A
    // bounded timer task keeps the progressive renderer moving without doing
    // all SVG work in one blocking turn.
    setTimeout(callback,0);
  }
  function drawGraph(fit){
    renderGeneration += 1;
    var generation=renderGeneration;
    if(settled)settled.hidden=true;
    var width=shell.clientWidth || (shell.getBoundingClientRect && shell.getBoundingClientRect().width) || 0;
    var height=shell.clientHeight || (shell.getBoundingClientRect && shell.getBoundingClientRect().height) || 0;
    if (width<=0 || height<=0) {
      status.textContent="Waiting for graph space…";
      if (!hasVisibleLayout()) { zeroSizeSince=0; return "waiting"; }
      if (!zeroSizeSince) zeroSizeSince=Date.now();
      if(Date.now()-zeroSizeSince>2500)throw new Error("Architecture graph never received visible space");
      clearTimeout(zeroSizeTimer);
      zeroSizeTimer=setTimeout(function(){
        if(generation===renderGeneration && !graphFailed)renderGraph(true);
      },100);
      return "waiting";
    }
    zeroSizeSince=0; clearTimeout(zeroSizeTimer);
    renderedNodes=visibleNodes();
    if(renderedNodes.length>maximumGraphNodes){
      var nodeBudgetError=new Error("Architecture map exceeds the responsive node budget");
      nodeBudgetError.recoverable=true;throw nodeBudgetError;
    }
    renderedLinks=sourceLinks(renderedNodes);
    if(renderedLinks.length>maximumGraphLinks){
      var linkBudgetError=new Error("Architecture map exceeds the responsive link budget");
      linkBudgetError.recoverable=true;throw linkBudgetError;
    }
    positions=layout(renderedNodes,width,height); nodeElements=new Map(); edgeElements=[];
    while (graph.firstChild) graph.removeChild(graph.firstChild);
    graph.setAttribute("viewBox","0 0 "+width+" "+height);
    graph.setAttribute("aria-busy","true");
    viewport=svgElement("g",{"class":"arch-viewport","data-focus":"false"});
    var edgeLayer=svgElement("g"), nodeLayer=svgElement("g");
    viewport.appendChild(edgeLayer); viewport.appendChild(nodeLayer); graph.appendChild(viewport);

    function appendEdge(link){
      var source=positions.get(link.source), target=positions.get(link.target);
      if (!source || !target) return;
      var line=svgElement("line",{"class":"arch-edge","data-relation":link.relation,
        "data-provenance":link.provenance||"observed","data-connected":"false",
        x1:source.x,y1:source.y,x2:target.x,y2:target.y});
      edgeLayer.appendChild(line); edgeElements.push({element:line,link:link});
    }
    function appendNode(node){
      var point=positions.get(node.id), group=svgElement("g",{"class":"arch-node",tabindex:"-1",
        role:"button","aria-label":(node.signature||node.path||node.label)+", "+stateNames[node.state],
        "data-node-id":node.id,"data-state":node.state,"data-selected":String(node.id===selectedId),
        "data-match":"false",transform:"translate("+point.x+" "+point.y+")"});
      group.appendChild(svgElement("circle",{"class":"arch-node-hit",r:18}));
      group.appendChild(svgElement("circle",{"class":"arch-node-halo",r:radius(node)+5}));
      group.appendChild(svgElement("circle",{"class":"arch-node-dot",r:radius(node)}));
      var title=svgElement("title"); title.textContent=node.signature||node.path||node.label; group.appendChild(title);
      group.addEventListener("click",guarded(function(event){if(event.stopPropagation)event.stopPropagation();selectNode(node.id,false);}));
      group.addEventListener("keydown",guarded(function(event){
        if (event.key==="Enter" || event.key===" ") {
          if(event.preventDefault)event.preventDefault(); selectNode(node.id,true); return;
        }
        if (["ArrowLeft","ArrowUp","ArrowRight","ArrowDown"].indexOf(event.key)<0) return;
        if(event.preventDefault)event.preventDefault();
        var available=renderedNodes.filter(function(item){return nodeElements.has(item.id);});
        if (!available.length) return;
        var current=available.findIndex(function(item){return item.id===node.id;});
        var direction=(event.key==="ArrowLeft" || event.key==="ArrowUp") ? -1 : 1;
        var next=available[(current+direction+available.length)%available.length];
        selectNode(next.id,true);
        var target=nodeElements.get(next.id); if(target && target.focus)target.focus();
      }));
      nodeLayer.appendChild(group); nodeElements.set(node.id,group);
    }
    function finishDraw(){
      if(generation!==renderGeneration || graphFailed)return;
      if (nodeElements.size!==renderedNodes.length) throw new Error("Architecture nodes were not rendered");
      transform={x:0,y:0,k:1}; applyTransform();
      var changes=renderedNodes.filter(function(node){return node.state!=="existing";}).length;
      status.textContent=renderedNodes.length
        ? renderedNodes.length+" items · "+changes+" changes · settled"
        : "No items in this view · choose Complete";
      renderInspector(); updateSearch(); updateGraphFocus(); syncRovingTabIndex();
      if (fit) {
        var ids=changeContextIds(); fitNodes(ids.length ? ids : renderedNodes.map(function(node){return node.id;}),1.5);
      }
      graph.setAttribute("aria-busy","false");
      shell.hidden=false; legend.hidden=false; camera.hidden=false; fallback.hidden=true; fallback.open=false;
      graphFailed=false;recoverableFallback=false;root.removeAttribute("data-recoverable-fallback");
      if(settled)settled.hidden=false;
      root.setAttribute("data-render-mode","graph");
    }

    var total=renderedLinks.length+renderedNodes.length;
    if(total>500){
      var edgeIndex=0,nodeIndex=0;
      status.textContent="Drawing "+renderedNodes.length+" items…";
      function drawChunk(){
        if(generation!==renderGeneration || graphFailed)return;
        try {
          var budget=160;
          while(edgeIndex<renderedLinks.length && budget>0){appendEdge(renderedLinks[edgeIndex]);edgeIndex+=1;budget-=1;}
          while(nodeIndex<renderedNodes.length && budget>0){appendNode(renderedNodes[nodeIndex]);nodeIndex+=1;budget-=1;}
          if(edgeIndex<renderedLinks.length || nodeIndex<renderedNodes.length)scheduleFrame(drawChunk);
          else finishDraw();
        } catch(error){showFallback(error,Boolean(error && error.recoverable));}
      }
      scheduleFrame(drawChunk);
      return "drawing";
    }
    renderedLinks.forEach(appendEdge); renderedNodes.forEach(appendNode); finishDraw();
    return "done";
  }
  function renderGraph(fit){
    if (graphFailed) { renderFallbackList(); renderInspector(); return; }
    try {
      var result=drawGraph(fit);
      if (result==="waiting") { root.setAttribute("data-render-mode","waiting"); return; }
      if (result==="drawing") {
        shell.hidden=false; legend.hidden=false; camera.hidden=false; fallback.hidden=true;
        root.setAttribute("data-render-mode","drawing");
      }
    } catch (error) { showFallback(error,Boolean(error && error.recoverable)); }
  }
  function retryResponsiveFallback(){
    if(!recoverableFallback)return;
    graphFailed=false;recoverableFallback=false;
    root.removeAttribute("data-recoverable-fallback");
    fallback.hidden=true;fallback.open=false;
    shell.hidden=false;legend.hidden=false;camera.hidden=false;
  }

  try {
  root.querySelectorAll("[data-arch-depth]").forEach(function(button){
    button.addEventListener("click",guarded(function(){
      if (button.disabled) return;
      activeDepth=button.getAttribute("data-arch-depth"); graphFocus=false;
      root.querySelectorAll("[data-arch-depth]").forEach(function(peer){
        peer.setAttribute("aria-pressed",String(peer===button));
      });
      retryResponsiveFallback();
      renderGraph(true);
    }));
  });
  root.querySelectorAll("[data-arch-view]").forEach(function(button){
    button.addEventListener("click",guarded(function(){
      activeView=button.getAttribute("data-arch-view"); graphFocus=false;
      root.querySelectorAll("[data-arch-view]").forEach(function(peer){
        peer.setAttribute("aria-pressed",String(peer===button));
      });
      retryResponsiveFallback();
      renderGraph(true);
    }));
  });
  root.querySelectorAll("[data-arch-node]").forEach(function(button){
    button.addEventListener("click",guarded(function(){selectNode(button.getAttribute("data-arch-node"),false);}));
  });
  document.getElementById("arch-fit-changes").addEventListener("click",guarded(function(){
    var ids=changeContextIds(); fitNodes(ids.length?ids:renderedNodes.map(function(node){return node.id;}),1.5);
  }));
  document.getElementById("arch-fit-all").addEventListener("click",guarded(function(){
    fitNodes(renderedNodes.map(function(node){return node.id;}),1.15);
  }));
  back.addEventListener("click",guarded(function(){focusId=null;graphFocus=false;back.hidden=true;renderGraph(true);}));
  search.addEventListener("input",guarded(updateSearch));
  search.addEventListener("keydown",guarded(function(event){
    if(event.key!=="Enter")return;
    var matches=updateSearch(); if(matches.length)selectNode(matches[0].id,!graphFailed);
  }));
  graph.addEventListener("wheel",guarded(function(event){
    if(event.preventDefault)event.preventDefault();
    var next=Math.max(.35,Math.min(3.2,transform.k*Math.exp(-event.deltaY*.001)));
    transform.k=next;applyTransform();
  }),{passive:false});
  graph.addEventListener("pointerdown",guarded(function(event){panning={x:event.clientX,y:event.clientY,tx:transform.x,ty:transform.y};}));
  graph.addEventListener("pointermove",guarded(function(event){
    if(!panning)return;transform.x=panning.tx+event.clientX-panning.x;transform.y=panning.ty+event.clientY-panning.y;applyTransform();
  }));
  graph.addEventListener("pointerup",guarded(function(){panning=null;}));
  graph.addEventListener("pointercancel",guarded(function(){panning=null;}));

  var resizeTimer=null;
  function scheduleRender(){clearTimeout(resizeTimer);resizeTimer=setTimeout(function(){renderGraph(true);},80);}
  if(window.ResizeObserver)new ResizeObserver(guarded(scheduleRender)).observe(shell);
  if(window.addEventListener)window.addEventListener("resize",guarded(scheduleRender));
  var ancestor=root.parentNode;
  while(ancestor){
    if(ancestor.tagName==="DETAILS")ancestor.addEventListener("toggle",guarded(scheduleRender));
    ancestor=ancestor.parentNode;
  }
  if(document.addEventListener)document.addEventListener("visibilitychange",guarded(scheduleRender));
  renderGraph(true);
  } catch (error) { showFallback(error); }
})();
</script>
"""


def render(shape: Dict[str, Any], prop: Dict[str, Any],
           mods: List[Dict[str, Any]], mermaid: str,
           built: set, commits: List[Dict[str, str]],
           docs: List[Dict[str, str]], design: List[Dict[str, str]],
           snapshot: str, mermaid_path: str = "",
           tree: Dict[str, Any] | None = None,
           history: Dict[str, Any] | None = None,
           doc_targets: Dict[str, str] | None = None,
           changed: set | None = None,
           deleted: set | None = None,
           cockpit_state: Dict[str, Any] | None = None,
           changed_actions: Dict[str, str] | None = None,
           present_files: set[str] | None = None,
           baseline_files: set[str] | None = None,
           function_changes: Dict[Tuple[str, str], Dict[str, str]] | None = None,
           ) -> str:
    level = str(shape.get("involvement", "") or "").strip()
    depth = DEPTH.get(level, 3)
    cockpit_state = cockpit_state if isinstance(cockpit_state, dict) else {}
    name = shape.get("name") or "Untitled"
    pitch = shape.get("pitch") or ""

    p: List[str] = []
    a = p.append
    a("<!doctype html><html lang=en><head><meta charset=utf-8>")
    a('<meta name=viewport content="width=device-width,initial-scale=1">')
    a(f"<title>{esc(name)} — plan</title><style>{CSS}{board_client.NAV_CSS}"
      f"{board_client.CSS}</style></head><body>")
    a('<div class="wrap">')
    a(board_client.navigation_html(1 if snapshot else 0))
    a(f"<h1>{esc(name)}</h1>")
    if pitch:
        a(f'<p class="sub">{esc(pitch)}</p>')
    status = str(
        cockpit_state.get("status") or prop.get("status", "") or ""
    ).strip().lower()
    if status == "draft" and depth > 0:
        a('<div class="banner draft"><b>Draft awaiting your review.</b> '
          "Review the player outcome, design authority, and veto envelope below.</div>")
    elif status == "recorded" and not bool(cockpit_state.get("approval_required")):
        a('<div class="banner ok"><b>Recorded reversible work.</b> '
          "The agent may continue only inside the stated veto envelope and must stop at "
          "the next go/no-go point.</div>")
    elif status == "recorded-stale" or (
            status == "recorded" and bool(cockpit_state.get("approval_required"))):
        a('<div class="banner draft"><b>Autonomous continuation is blocked.</b> '
          + esc(str(cockpit_state.get("approval_blocker") or
                    "The recorded design or reversible envelope is no longer exact."))
          + " Revise and re-record the plan before implementation continues.</div>")
    elif status == "approved":
        who = prop.get("approved_by") or ""
        when = prop.get("approved_on") or ""
        a('<div class="banner ok"><b>Approved</b>'
          + (f" by {esc(str(who))}" if who else "")
          + (f" on {esc(str(when))}" if when else "")
          + ". Colours below show what has been built against it.</div>")
    elif status == "approval-stale":
        reasons = cockpit_state.get("exact_approval", {}).get("reasons", []) \
            if isinstance(cockpit_state.get("exact_approval"), dict) else []
        a('<div class="banner draft"><b>Stored approval is not valid evidence.</b> '
          + esc("; ".join(str(item) for item in reasons) or "The exact authority chain is stale.")
          + " Return the proposal to draft and review it again.</div>")

    bits = [f"involvement <b>{esc(level or 'unset')}</b>"]
    if snapshot:
        bits.append(f"snapshot <b>{esc(snapshot)}</b>")
    a(f'<p class="sub">{" · ".join(bits)}</p>')
    # The handoff that closes the loop. plan.html is the page a human has open
    # mid-slice, so it is the only place a retrospective becoming due, or a
    # finding waiting on a decision, can reach them without being asked for.
    # Empty until /api/state says otherwise: under file:// or a dead board it
    # renders nothing at all rather than an error (it must never make the plan
    # look broken to say something optional).
    a('<div id="retro-banner"></div>')
    a(board_client.shell_html())

    # ---- decision-first front door. The old page put the current decision,
    # the implementation tree, every historical decision and every document
    # at one visual level. Preserve that record below, but first answer the
    # three questions a reviewer actually arrives with: what are we trying to
    # achieve, what needs my decision, and what evidence changed since approval?
    all_questions = rows(shape, "questions")
    front_questions = [
        question for question in all_questions
        if question_relation(question, prop) == "related"
    ]
    front_refs = [r for r in rows(prop, "design_refs") if r.get("section")]
    front_slice = str(prop.get("slice", "") or "").strip()
    front_has_struct = any(rows(prop, key) for key in ("scope", "modules", "files", "functions"))
    needs_design_decision = front_has_struct and not front_refs
    front_prop_files = {
        str(f.get("path", "")).strip().replace("\\", "/")
        for f in rows(prop, "files") if f.get("path")
    }
    front_history = {
        str(f.get("path", "")).strip().replace("\\", "/")
        for f in rows(history or {}, "files") if f.get("path")
    }
    missing_paths = sorted(
        path for path in front_prop_files
        if file_state(path, {path: {}}, built, front_history) == "missing"
    )
    deleted_paths = set(deleted or ())
    changed_paths = set(changed or ())
    if depth == 0:
        scope_rows = rows(prop, "scope")

        def scope_contains(boundary: Dict[str, Any], path: str) -> bool:
            raw = str(boundary.get("path") or "").strip().replace("\\", "/")
            if boundary.get("kind") == "file":
                return path == raw
            if raw == "(root)":
                return True
            return path == raw or path.startswith(raw.rstrip("/") + "/")

        def scope_specificity(boundary: Dict[str, Any]) -> Tuple[int, int]:
            raw = str(boundary.get("path") or "").strip().replace("\\", "/")
            if boundary.get("kind") == "file":
                return (2, len(raw.split("/")))
            if raw == "(root)":
                return (0, 0)
            return (1, len(raw.split("/")))

        def inside_scope(path: str) -> bool:
            matching = [
                boundary for boundary in scope_rows
                if scope_contains(boundary, path)
            ]
            if not matching or changed_actions is None:
                return False
            owner = max(matching, key=scope_specificity)
            declared = str(owner.get("action") or "").strip().lower()
            return declared == str(changed_actions.get(path) or "").lower()

        extra_paths = sorted(
            path for path in changed_paths if not inside_scope(path)
        )
    elif depth == 1:
        declared_modules = {
            str(item.get("path") or "").rstrip("/") for item in rows(prop, "modules")
            if item.get("path")
        }
        extra_paths = sorted(
            path for path in changed_paths
            if not any(path == module or path.startswith(module + "/")
                       for module in declared_modules)
        )
    else:
        extra_paths = sorted(changed_paths - front_prop_files)
    approval_required = bool(cockpit_state.get("approval_required", status == "draft"))
    approval_available = bool(cockpit_state.get("approval_available", True))
    recorded_decision_available = bool(
        cockpit_state.get("recorded_decision_available", False)
    )
    scope_unavailable = (
        status in ("approved", "recorded", "recorded-stale")
        and changed is None
    )
    decision_count = (1 if approval_required else 0) + len(front_questions)
    decision_count += 1 if needs_design_decision else 0
    decision_count += 1 if status in ("approved", "recorded", "recorded-stale") and extra_paths else 0
    decision_count += 1 if scope_unavailable else 0
    exp = prop.get("experience")
    player_does = (str(exp.get("player_does", "") or "").strip()
                   if isinstance(exp, dict) else "")
    feels_like = (str(exp.get("feels_like", "") or "").strip()
                  if isinstance(exp, dict) else "")
    outcome = player_does or front_slice or pitch or "No active outcome has been recorded."

    live_fingerprint = str(cockpit_state.get("fingerprint") or "")
    a(f'<section class="front" id="plan-live-state" '
      f'data-plan-fingerprint="{esc(live_fingerprint)}" aria-label="Plan summary">')
    a('<div class="front-grid">')
    quick = cockpit_state.get("quick_read") if isinstance(cockpit_state.get("quick_read"), dict) else {}
    authority = (cockpit_state.get("design_authority")
                 if isinstance(cockpit_state.get("design_authority"), dict) else {})
    authority_trust = str(cockpit_state.get("authority_trust") or "").strip()
    reversibility = (cockpit_state.get("reversibility")
                     if isinstance(cockpit_state.get("reversibility"), dict) else {})
    go_no_go = (cockpit_state.get("go_no_go")
                if isinstance(cockpit_state.get("go_no_go"), dict) else {})
    verification = (cockpit_state.get("verification")
                    if isinstance(cockpit_state.get("verification"), dict) else {})

    a('<div class="front-card"><h2>Quick read</h2>')
    a(f'<div class="outcome">{esc(outcome)}</div>')
    if feels_like:
        a(f'<div class="m">Intended feel: {esc(feels_like)}</div>')
    if front_slice and player_does:
        a(f'<div class="m mono">slice {esc(front_slice)}</div>')
    not_this = str(quick.get("not_this") or (exp.get("not_this") if isinstance(exp, dict) else "") or "").strip()
    if not_this:
        a(f'<div class="m">Not this: {esc(not_this)}</div>')
    a('<div class="front-meta">')
    status_label = ("approval required" if approval_required else
                    "recorded reversible" if status == "recorded" else
                    "approved" if status == "approved" else
                    status or "not proposed")
    status_cls = "warn" if approval_required else "ok" if status in ("approved", "recorded") else ""
    a(f'<span class="metric {status_cls}">{esc(status_label)}</span>')
    a(f'<span class="metric{(" bad" if decision_count else " ok")}">'
      f'{decision_count} decision{("" if decision_count == 1 else "s")} needed</span>')
    if front_prop_files:
        a(f'<span class="metric{(" warn" if missing_paths else " ok")}">'
          f'{len(front_prop_files) - len(missing_paths)}/{len(front_prop_files)} '
          'planned changes observed</span>')
    if extra_paths:
        a(f'<span class="metric bad">{len(extra_paths)} unapproved change'
          f'{("" if len(extra_paths) == 1 else "s")}</span>')
    a('</div></div>')

    a('<div class="front-card"><h2>Latest verification</h2>')
    verify_status = str(verification.get("status") or "not-run")
    verify_cls = "ok" if verify_status == "fresh" else "bad" if verify_status == "failed" else "warn"
    a(f'<div class="front-meta"><span class="metric {verify_cls}">{esc(verify_status)}</span></div>')
    if verification.get("failure_class"):
        a(f'<div class="m mono">{esc(verification.get("failure_class"))}</div>')
    if verification.get("summary"):
        a(f'<div class="m">{esc(verification.get("summary"))}</div>')
    if verification.get("recorded_at"):
        a(f'<div class="m mono">{esc(verification.get("recorded_at"))}</div>')
    baseline = str(prop.get("baseline_sha", "") or "").strip()
    if baseline:
        a(f'<div class="t">Compared with <code>{esc(baseline[:12])}</code></div>')
    if changed is None:
        a('<div class="empty">Change scope is unavailable because the proposal has no '
          'usable Git baseline or Git could not verify it.</div>')
    else:
        a(f'<div class="m">{len(changed)} authored game file'
          f'{("" if len(changed) == 1 else "s")} changed since baseline.</div>')
        if deleted_paths:
            a(f'<div class="m">{len(deleted_paths)} of those '
              f'{("was" if len(deleted_paths) == 1 else "were")} deleted.</div>')
    a('<div class="m" style="margin-top:8px">Repository change scope is context; the '
      'verification state above is the completion evidence.</div>')
    a('</div></div>')

    a('<div class="front-grid">')
    a('<div class="front-card"><h2>Design authority</h2><div class="fact-list">')
    a(f'<div><b>Authority</b>{esc(authority.get("authority") or authority.get("status") or "not recorded")}</div>')
    if authority.get("authored_by"):
        a(f'<div><b>Authored by</b>{esc(authority.get("authored_by"))}</div>')
    if authority.get("confidence"):
        a(f'<div><b>Confidence</b>{esc(authority.get("confidence"))}</div>')
    if authority.get("summary"):
        a(f'<div>{esc(authority.get("summary"))}</div>')
    if authority_trust:
        a(f'<div><b>Trust boundary</b>{esc(authority_trust)}</div>')
    disclosures = authority.get("disclosures")
    if isinstance(disclosures, list):
        for disclosure in disclosures:
            if not isinstance(disclosure, dict):
                continue
            a('<div class="design-disclosure">')
            a(f'<div><b>Design section</b><code>{esc(disclosure.get("section"))}</code></div>')
            if disclosure.get("quick_read"):
                a(f'<div><b>Quick read</b><div class="design-excerpt">'
                  f'{esc(disclosure.get("quick_read"))}</div></div>')
            if disclosure.get("why_inference"):
                a(f'<div><b>Why this inference</b>{esc(disclosure.get("why_inference"))}</div>')
            if disclosure.get("assumptions"):
                a(f'<div><b>Assumptions</b>{esc(disclosure.get("assumptions"))}</div>')
            if disclosure.get("veto_and_go_no_go"):
                a(f'<div><b>Veto and go/no-go</b><div class="design-excerpt">'
                  f'{esc(disclosure.get("veto_and_go_no_go"))}</div></div>')
            a('</div>')
    a('</div></div>')
    a('<div class="front-card"><h2>Reversibility and go/no-go</h2><div class="fact-list">')
    a(f'<div><b>State</b>{esc(reversibility.get("state") or reversibility.get("status") or "not recorded")}</div>')
    if reversibility.get("veto_scope"):
        a(f'<div><b>Veto scope</b>{esc(reversibility.get("veto_scope"))}</div>')
    if reversibility.get("hard_to_undo"):
        a(f'<div><b>Hard to undo</b>{esc(reversibility.get("hard_to_undo"))}</div>')
    next_gate = reversibility.get("next_go_no_go") or go_no_go.get("summary")
    if next_gate:
        a(f'<div><b>Next go/no-go</b>{esc(next_gate)}</div>')
    a('</div></div></div>')

    a('<h2>Needs your decision</h2>')
    a('<div class="decision-stack">')
    if approval_required:
        a('<div class="decision-card"><div class="ask">Approve the design and plan?</div>'
          '<div class="answer">Approval confirms the referenced player-experience design, '
          'binds its exact intent and reviewed plan, and authorizes this proposal. '
          'A later design-and-plan veto remains attached to each design intent until '
          'another explicit confirmation. It never dispatches provider work.</div>')
        fingerprint = str(cockpit_state.get("fingerprint") or "")
        if not snapshot and fingerprint and approval_available:
            a(f'<div id="plan-decision-controls" data-plan-fingerprint="{esc(fingerprint)}">')
            a('<textarea id="plan-decision-comment" class="decision-comment" '
              'data-board-control placeholder="What should change? Required for veto or request changes."></textarea>')
            a('<div class="decision-actions">'
              '<button type="button" class="approve" data-plan-action="approve" '
              'data-board-control>Approve design and plan</button>'
              '<button type="button" data-plan-action="request-changes" '
              'data-board-control>Request changes</button>'
              '<button type="button" class="veto" data-plan-action="veto" '
              'data-board-control>Veto design and plan</button></div></div>')
        elif snapshot:
            a('<div class="m">Snapshots are immutable review records. Open the living plan to decide.</div>')
        else:
            blocker = str(cockpit_state.get("approval_blocker") or "").strip()
            a('<div class="m warn"><b>This decision cannot be recorded yet.</b> '
              + esc(blocker or "The complete design-authority contract is unavailable.")
              + " Resolve the named blocker, then refresh the living plan."
              + '</div>')
        a('</div>')
    if needs_design_decision:
        a('<div class="decision-card"><div class="ask">Design backing is missing.</div>'
          '<div class="answer">Implementation cannot begin. Record the intended player '
          'experience and outcome in docs/design before approving this proposal.</div></div>')
    for q in front_questions:
        qid = str(q.get("id", "") or "").strip()
        a('<div class="decision-card">')
        a(f'<div class="ask">{esc(q.get("question"))}</div>')
        if q.get("blocks"):
            a(f'<div class="answer">Blocks: {esc(q.get("blocks"))}</div>')
        opts = q.get("options")
        if isinstance(opts, list) and opts:
            a('<ul>')
            for opt in opts:
                a(f'<li>{esc(opt)}</li>')
            a('</ul>')
        if qid:
            a(f'<div class="m">Reply in chat with <code>{esc(qid)}</code> and your choice.</div>')
        a('</div>')
    if status in ("approved", "recorded", "recorded-stale") and extra_paths:
        boundary_label = "approved" if status == "approved" else "recorded"
        a('<div class="decision-card"><div class="ask">The implementation differs from the '
          + boundary_label + ' plan.</div>')
        a('<div class="answer">Revise the plan and re-establish its exact authority before work continues: ')
        a(', '.join(
            f'<code>{esc(path)}</code>{" (deleted)" if path in deleted_paths else ""}'
            for path in extra_paths[:4]
        ))
        if len(extra_paths) > 4:
            a(f' and {len(extra_paths) - 4} more')
        a('</div></div>')
    if scope_unavailable:
        a('<div class="decision-card"><div class="ask">Repository change scope could '
          'not be verified.</div><div class="answer">Do not continue implementation '
          'until the proposal baseline and Git comparison can be read successfully.'
          '</div></div>')
    if decision_count == 0:
        remaining = (f' {len(missing_paths)} approved file'
                     f'{(" remains" if len(missing_paths) == 1 else "s remain")} to be observed.'
                     if missing_paths else "")
        open_record = (
            f' {len(all_questions)} other open question'
            f'{(" remains" if len(all_questions) == 1 else "s remain")} in the full record.'
            if all_questions else ""
        )
        a('<div class="decision-card clear"><div class="ask">No decision is waiting.</div>'
          '<div class="answer">No unresolved product choice related to the active '
          'proposal is recorded.' + esc(open_record) + esc(remaining) + '</div></div>')
    a('</div>')
    if status == "approved" and not snapshot:
        fingerprint = str(cockpit_state.get("fingerprint") or "")
        if fingerprint:
            a('<details class="record"><summary>Reconsider this approval</summary>'
              '<div class="record-body"><p class="legend">Request changes withdraws '
              'only this exact plan approval and preserves confirmed design authority. '
              'Veto design and plan rejects each referenced design intent independently '
              'of later baseline, envelope or plan edits until another explicit cockpit '
              'confirmation. Agent-authored sections return to provisional metadata; '
              'human-authored section metadata remains intact while the veto ledger blocks '
              'implementation. Neither action dispatches work.</p>')
            a(f'<div id="plan-reconsider-controls" data-plan-fingerprint="{esc(fingerprint)}">')
            a('<textarea class="decision-comment" data-board-control '
              'placeholder="Why is this approval changing? Required."></textarea>')
            a('<div class="decision-actions">'
              '<button type="button" data-plan-action="request-changes" '
              'data-board-control>Request changes</button>'
              '<button type="button" class="veto" data-plan-action="veto" '
              'data-board-control>Veto design and plan</button></div></div></div></details>')
    if status == "recorded" and recorded_decision_available and not snapshot:
        fingerprint = str(cockpit_state.get("fingerprint") or "")
        if fingerprint:
            a('<details class="record"><summary>Pause or veto recorded autonomous work</summary>'
              '<div class="record-body"><p class="legend">Request changes pauses '
              'implementation by returning this exact recorded plan to draft while leaving '
              'its current design-authority state unchanged. The revised plan must '
              're-establish a valid recorded reversible contract before work resumes. '
              'Veto design and plan also rejects each referenced exact design intent. That '
              'veto survives later baseline, envelope and plan-only edits until the '
              'player-experience intent changes or a later explicit cockpit confirmation '
              'supersedes it. Both actions require a reason; neither dispatches work.</p>')
            a(f'<div id="plan-recorded-controls" data-plan-fingerprint="{esc(fingerprint)}">')
            a('<textarea class="decision-comment" data-board-control '
              'placeholder="Why should this recorded work pause or be vetoed? Required."></textarea>')
            a('<div class="decision-actions">'
              '<button type="button" data-plan-action="request-changes" '
              'data-board-control>Request changes</button>'
              '<button type="button" class="veto" data-plan-action="veto" '
              'data-board-control>Veto design and plan</button></div></div></div></details>')

    # Architecture is a decision aid, so it belongs after the decision and
    # before the audit record. Hands-off work intentionally keeps only its
    # coarse reversible scope; deeper involvement gets the complete source map
    # with detail capped at the level the human chose to review.
    if depth > 0:
        architecture = architecture_map_model(
            prop,
            tree or {},
            built,
            mods,
            changed_actions,
            cockpit_state,
            present_files,
            baseline_files,
            level,
            function_changes,
        )
        rendered_architecture = architecture_map_html(architecture, level)
        if rendered_architecture:
            a(rendered_architecture)

    a('<details class="record"><summary>Review full plan and project record</summary>'
      '<div class="record-body">')
    a('<p class="legend">Implementation detail, design ancestry, history, architecture and '
      'reference documentation are retained here for audit and deep review.</p>')

    # ---- experience first. Structure chosen before intended feel is a guess,
    # and every other artefact in this repo describes structure. This is the
    # only place intended feel is written down, so it goes above the tree.
    exp = prop.get("experience")
    if isinstance(exp, dict) and any(
            str(exp.get(k, "") or "").strip()
            for k in ("player_does", "feels_like", "camera", "controls",
                      "not_this")):
        a("<h2>What this should feel like</h2>")
        a('<p class="legend">Read this first. If it does not match what you'
          " pictured, stop here — the structure below was chosen to serve it.</p>")
        a('<div class="card exp">')
        for key, lab in (("player_does", "The player"),
                         ("feels_like", "Feels like"),
                         ("camera", "Camera"),
                         ("controls", "Controls")):
            val = str(exp.get(key, "") or "").strip()
            if val:
                a(f'<div class="er"><span class="ek">{esc(lab)}</span>'
                  f'<span class="ev">{esc(val)}</span></div>')
        nope = str(exp.get("not_this", "") or "").strip()
        if nope:
            a(f'<div class="er"><span class="ek">Not this</span>'
              f'<span class="ev no">{esc(nope)}</span></div>')
        a("</div>")

    # ---- why this work exists. Design ancestry sits with the experience
    # contract rather than beside the design body further down, because the
    # question it answers is about THIS slice: what end state is it serving.
    # A reference the reader can click is the difference between a rationale
    # and a claim.
    refs = [r for r in rows(prop, "design_refs") if r.get("section")]

    # ---- no design behind this slice is a stop, not an acknowledgement path.
    # A warning cannot grant player-intent authority and legacy acknowledgements
    # remain historical only.
    has_struct = any(rows(prop, k) for k in ("modules", "files", "functions"))
    if has_struct and not refs:
        a("<h2>No design behind this slice</h2>")
        a('<div class="banner draft"><b>Implementation is blocked.</b> '
          "Write the intended player experience and outcome in "
          "<code>docs/design/</code>, cite it here, and regenerate the plan.</div>")

    if refs:
        a("<h2>Why this work exists</h2>")
        a('<p class="legend">The design this slice is descended from. If the'
          " reason below is one step up from the work itself rather than an end"
          " state, the design body is thin there.</p>")
        for r in refs:
            sec = str(r.get("section", "") or "").strip()
            why = str(r.get("why", "") or "").strip()
            live = (ROOT / sec).is_file()
            anchor = "doc-" + re.sub(r"[^a-z0-9]+", "-", sec.lower()).strip("-")
            a('<div class="card">')
            if live:
                a(f'<div class="t"><a href="#{esc(anchor)}">'
                  f'<code>{esc(sec)}</code></a></div>')
            else:
                a(f'<div class="t no"><code>{esc(sec)}</code> — missing</div>')
            if why:
                a(f'<div class="m">{esc(why)}</div>')
            a("</div>")

    # ---- mockup. Rejecting a picture costs almost nothing; rejecting an
    # implementation costs a slice. Inline SVG only, so it survives file://.
    mock = prop.get("mockup")
    if isinstance(mock, dict):
        svg = str(mock.get("svg", "") or "").strip()
        cap = str(mock.get("caption", "") or "").strip()
        nope = str(mock.get("not_possible", "") or "").strip()
        safe = clean_svg(svg)
        if svg and not safe:
            a("<h2>Mockup</h2>")
            a('<p class="empty">A mockup was supplied but was rejected as unsafe'
              " or unparseable. Only inert SVG shape markup is rendered.</p>")
        elif safe:
            a("<h2>Mockup</h2>")
            a('<p class="legend">Approximation, not the real render. Rejecting'
              " this now is far cheaper than rejecting the implementation.</p>")
            a(f'<div class="mock">{safe}</div>')
            if cap:
                a(f'<p class="sub">{esc(cap)}</p>')
        elif nope:
            a("<h2>Mockup</h2>")
            a(f'<p class="empty">Not drawable: {esc(nope)}</p>')

    # ---- now: the approved structure, with what is actually built
    slice_name = prop.get("slice") or ""
    a("<h2>Building now" + (f" — {esc(slice_name)}" if slice_name else "") + "</h2>")
    prop_mods = {str(m.get("path", "")).strip().replace("\\", "/"): m
                 for m in rows(prop, "modules") if m.get("path")}
    prop_files = {str(f.get("path", "")).strip().replace("\\", "/"): f
                  for f in rows(prop, "files") if f.get("path")}
    hist_paths = {str(f.get("path", "")).strip().replace("\\", "/")
                  for f in rows(history or {}, "files") if f.get("path")}
    real_mods = {str(m.get("path", "")).strip().replace("\\", "/")
                 for m in mods if m.get("path")}

    if depth == 0:
        scope_rows = rows(prop, "scope")
        if scope_rows:
            a('<p class="empty">Hands-off: the agent recorded coarse reversible '
              "boundaries rather than asking you to approve modules or files.</p><ul class=f>")
            for boundary in scope_rows:
                a("<li>" + state_tag("built")
                  + f'<span class="mono">{esc(boundary.get("path"))}</span>'
                  + f'<span class="tag act">{esc(boundary.get("kind"))} · '
                    f'{esc(boundary.get("action"))}</span>'
                  + f'<div class="why">{esc(boundary.get("why"))}</div></li>')
            a("</ul>")
        else:
            a('<p class="empty">Hands-off work has no recorded coarse scope and cannot verify.</p>')
    if not prop_mods and not prop_files and depth > 0:
        a('<p class="empty">Nothing approved yet. The agent proposes the'
          " structure, you approve it, and it lands here before any code.</p>")

    funcs_by_file: Dict[str, List[Dict[str, Any]]] = {}
    for fn in rows(prop, "functions"):
        funcs_by_file.setdefault(
            str(fn.get("file", "")).strip().replace("\\", "/"), []).append(fn)

    def file_block(path: str, meta: Dict[str, Any], state: str) -> None:
        """One file, with why it is being touched and what changes inside it.

        A row reading "to do | proposal.json" with nothing else tells a reviewer
        nothing. The coarser the layer, the more the reason matters, because a
        file-level change with no stated reason is the one you cannot evaluate.
        """
        act = str(meta.get("action", "") or "")
        why = str(meta.get("why", "") or "")
        a("<li>" + state_tag(state)
          + f'<span class="mono">{esc(path)}</span>'
          + (f'<span class="tag act">{esc(act)}</span>' if act else "")
          + (f'<div class="why">{esc(why)}</div>' if why
             else ('<div class="why warn">no reason given</div>'
                   if state not in ("existing",) else ""))
          + "</li>")
        if depth >= 3:
            for fn in funcs_by_file.get(path, []):
                fact = str(fn.get("action", "") or "")
                fwhy = str(fn.get("why", "") or "")
                fst = "new" if fact == "new" else (
                    "modified" if fact == "modify" else "deleted")
                a("<li>" + state_tag(fst)
                  + f'<span class="sig mono">{esc(fn.get("signature"))}</span>'
                  + (f'<div class="why">{esc(fwhy)}</div>' if fwhy
                     else '<div class="why warn">no reason given</div>')
                  + "</li>")

    # Every file the view may show: proposed, or built within this slice.
    candidates = set(prop_files) | built
    all_mods = sorted(set(prop_mods) | real_mods)
    # Each file belongs to its DEEPEST matching module. Prefix matching would
    # list scripts/data/map_parser.gd under scripts AND scripts/data.
    owner: Dict[str, str] = {}
    for f in candidates:
        best = ""
        for mod in all_mods:
            pre = mod.rstrip("/") + "/"
            if f.startswith(pre) and len(mod) > len(best):
                best = mod
        if best:
            owner[f] = best
    shown_files = set()
    for mod in all_mods:
        meta = prop_mods.get(mod, {})
        if mod not in prop_mods and not any(owner.get(f) == mod for f in candidates):
            continue
        action = str(meta.get("action") or "")
        if mod in prop_mods and action == "delete" and mod not in real_mods:
            st = "deleted"
        elif mod in prop_mods and mod in real_mods:
            st = "built"
        elif mod in prop_mods:
            st = "missing"
        else:
            st = "extra"
        a('<div class="mod"><div class="modh">'
          + state_tag(st)
          + f'<span class="modp mono">{esc(mod)}</span>'
          + (f'<span class="tag act">{esc(meta.get("action"))}</span>'
             if meta.get("action") else "")
          + (f'<span class="m">{esc(meta.get("role"))}</span>'
             if meta.get("role") else "")
          + (f'<span class="m">→ {esc(", ".join(meta.get("may_depend_on", [])))}</span>'
             if isinstance(meta.get("may_depend_on"), list) and meta.get("may_depend_on")
             else "")
          + (f'<div class="why">{esc(meta.get("why"))}</div>'
             if meta.get("why") else "")
          + (f'<div class="why bd">crossing: {esc(meta.get("boundary_data"))}</div>'
             if meta.get("boundary_data") else "")
          + "</div>")
        if depth >= 2:
            here = sorted(f for f in candidates if owner.get(f) == mod)
            if here:
                a('<ul class="f">')
                for f in here:
                    shown_files.add(f)
                    file_block(f, prop_files.get(f, {}),
                               file_state(f, prop_files, built, hist_paths))
                a("</ul>")
        a("</div>")

    if depth >= 2:
        loose = sorted(f for f in candidates if f not in shown_files)
        if loose:
            a("<h2>Files outside a declared module</h2><ul class=f>")
            for f in loose:
                file_block(f, prop_files.get(f, {}),
                           file_state(f, prop_files, built, hist_paths))
            a("</ul>")

    # ---- open questions: the frontier
    questions = rows(shape, "questions")
    a("<h2>Open questions</h2>")
    if not questions:
        a('<p class="empty">None. Nothing is blocked on a decision.</p>')
    for q in questions:
        a('<div class="card q">')
        a(f'<div class="t">{esc(q.get("question"))}</div>')
        relation = question_relation(q, prop)
        if relation == "related":
            a('<div class="m"><b>Current proposal:</b> its explicit relation matches '
              'this slice or one of its design sections.</div>')
        elif relation == "legacy":
            a('<div class="m warn"><b>Legacy/unscoped question:</b> retained in the '
              'record, but not promoted into the active cockpit. Add an exact '
              '<code>related_slices</code> or <code>related_design_refs</code> relation '
              'when it applies.</div>')
        else:
            a('<div class="m"><b>Other work:</b> related to another slice or design '
              'section and retained in the full record.</div>')
        if q.get("related_slices"):
            a(f'<div class="m mono">related slices: '
              f'{esc(", ".join(str(item) for item in q.get("related_slices", [])))}</div>')
        if q.get("related_design_refs"):
            a(f'<div class="m mono">related design: '
              f'{esc(", ".join(str(item) for item in q.get("related_design_refs", [])))}</div>')
        if q.get("blocks"):
            a(f'<div class="m">blocks: {esc(q.get("blocks"))}</div>')
        opts = q.get("options")
        if isinstance(opts, list) and opts:
            for opt in opts:
                a(f'<div class="opt">• {esc(opt)}</div>')
            a(f'<div class="m">Reply in chat with <code>{esc(q.get("id") or "q")}</code> '
              'and your choice. This page never pretends a local click was saved.</div>')
        a(f'<div class="m mono">{esc(q.get("id"))} · raised'
          f' {esc(q.get("raised"))} by {esc(q.get("raised_by"))}</div>')
        a("</div>")

    # ---- direction: known heading, not a spec
    direction = rows(shape, "direction")
    a("<h2>Direction</h2>")
    if not direction:
        a('<p class="empty">Nothing recorded. Today\'s work is shaped only by'
          " today's requirements.</p>")
    else:
        a('<p class="legend">What today\'s work must leave room for. Not a'
          " backlog: none of this is built now.</p>")
    for d in direction:
        a('<div class="card d">')
        a(f'<div class="t">{esc(d.get("heading"))}</div>')
        a(f'<div class="m">accommodate: {esc(d.get("accommodate"))}</div>')
        if d.get("not_yet"):
            a(f'<div class="m">not yet: {esc(d.get("not_yet"))}</div>')
        a(f'<div class="m mono">{esc(d.get("id"))}</div>')
        a("</div>")

    # ---- revisions: how the plan moved
    revs = rows(prop, "revisions")
    if revs:
        a("<h2>How the plan changed</h2>")
        for r in revs:
            what = r.get("what") or r.get("what_changed") or ""
            why = r.get("why") or r.get("because") or ""
            on = r.get("on") or r.get("date") or ""
            decided_by = r.get("decided_by") or r.get("approved_by") or ""
            a('<div class="card"><div class="t">'
              f'{esc(what)}</div>')
            if why:
                a(f'<div class="m">because: {esc(why)}</div>')
            a(f'<div class="m mono">{esc(on)} · '
              f'{esc(decided_by)}</div></div>')

    # ---- settled: the ledger, never deleted
    decisions = rows(shape, "decisions")
    superseded = {str(d.get("supersedes", "")).strip() for d in decisions}
    a("<h2>Settled</h2>")
    if not decisions:
        a('<p class="empty">Nothing settled yet.</p>')
    for d in reversed(decisions):
        dead = str(d.get("id", "")) in superseded
        a(f'<div class="card done"{" style=opacity:.5" if dead else ""}>')
        a(f'<div class="t">{esc(d.get("answer"))}</div>')
        a(f'<div class="m">{esc(d.get("question"))}</div>')
        if d.get("note"):
            a(f'<div class="m">{esc(d.get("note"))}</div>')
        # revisit_if is what distinguishes a provisional decision from a
        # permanent one. Hiding it makes every entry look load-bearing.
        rev = str(d.get("revisit_if", "") or "").strip()
        if rev and rev.lower().rstrip(".") != "permanent":
            a(f'<div class="m">revisit if: {esc(rev)}</div>')
        doc = str(d.get("docs_at", "") or "").strip()
        if doc:
            anchor = (doc_targets or {}).get(doc, doc)
            a(f'<div class="m"><a href="{esc(anchor)}"><code>{esc(doc)}</code></a></div>')
        a(f'<div class="m mono">{esc(d.get("id"))} · {esc(d.get("date"))} ·'
          f' {esc(d.get("decided_by"))}'
          + (" · superseded" if dead else "") + "</div></div>")

    # ---- recent work, with honest repository-relative references
    if commits:
        a("<h2>Recent changes</h2>")
        a('<p class="legend">Repository-relative paths are references only; the '
          'bounded cockpit does not serve source files.</p>')
        for c in commits[:8]:
            files = c.get("files") or []
            links = " ".join(f'<code>{esc(f)}</code>' for f in files[:4])
            more = f' <span class="m">+{len(files) - 4} more</span>' if len(files) > 4 else ""
            a(f'<div class="card"><div class="t">{esc(c.get("subject"))}</div>'
              f'<div class="m mono">{esc(c.get("date"))} · {esc(c.get("sha"))}</div>'
              f'<div class="m">{links}{more}</div></div>')

    # ---- graduated documentation: the game, then the kit
    if design:
        a("<h2>Game design</h2>")
        a('<p class="legend">What the game is meant to be, written before the'
          " code that delivers it. A section can be an open question, a leaning"
          " or settled \u2014 each states its own resolution. What it must never"
          " be is filled past what was actually decided.</p>")

        # Derived backlog. Not a stored task list: a settled section whose
        # tunables no code claims is unbuilt, and an open question with a stated
        # way to settle it is work waiting to happen. Both fall out of the
        # design body, so the next slice is evident without anyone writing a
        # ticket.
        idx = design_index()
        ready = idx.get("ready") or []
        if ready:
            a('<div class="ready"><div class="rh">Evident from design</div>')
            a('<p class="legend">Derived from section resolution and binding'
              " state, not a stored backlog. Pick one; the agent turns it into a"
              " proposal.</p>")
            a("<ul>")
            for r in ready:
                a(f"<li>{esc(r)}</li>")
            a("</ul>")
            warn = []
            if idx.get("unbound"):
                warn.append(f"{idx['unbound']} declared tunable(s) no code claims")
            if idx.get("undesigned"):
                warn.append(f"{idx['undesigned']} tunable(s) in code no section declares")
            if warn:
                warning_text = " \u00b7 ".join(warn)
                a(f'<p class="m">{esc(warning_text)}</p>')
            a("</div>")

        for d in design:
            res = str(d.get("resolution") or "")
            badge = (f'<span class="res r-{esc(res)}">{esc(res)}</span>'
                     if res else "")
            a(f'<details class="doc" id="{esc(d.get("anchor", ""))}">'
              f'<summary>{esc(d["title"])}{badge}'
              f'<code class="m">{esc(d["path"])}</code></summary>'
              f'<div class="body">{d["body"]}</div></details>')
    else:
        a("<h2>Game design</h2>")
        a('<p class="empty">Nothing written yet. Design is written BEFORE the'
          " code that delivers it, and a section can exist as an open question"
          " with no answer. Describe an idea, or ask the agent why a piece of"
          " work exists \u2014 either starts a discovery session that writes"
          " into <code>docs/design/</code>.</p>")

    if docs:
        a("<h2>How the kit works</h2>")
        a('<p class="legend">The gate, the rules, the workflow. Documents the'
          " tooling rather than the game.</p>")
        for d in docs:
            a(f'<details class="doc" id="{esc(d.get("anchor", ""))}">'
              f'<summary>{esc(d["title"])}'
              f'<code class="m">{esc(d["path"])}</code></summary>'
              f'<div class="body">{d["body"]}</div></details>')

    a('</div></details></section>')

    a('<p class="foot">Generated by <code>tools/plan_html.py</code>. Do not edit'
      " this file: it is overwritten. Change <code>project.shape.json</code> or"
      " <code>proposal.json</code> and regenerate.</p>")

    # A design reference links to a rendered doc pane, but <details> is closed
    # by default, so the jump would land on a collapsed summary. Open the
    # targeted pane. Plain JS with no dependency, and the page is still fully
    # readable if scripting is off -- the pane just stays shut.
    a("<script>"
      "function openTarget(){var h=location.hash;if(!h)return;"
      "var el=document.querySelector(h);if(!el)return;"
      "if(el.tagName==='DETAILS'){el.open=true;}"
      "el.scrollIntoView();}"
      "window.addEventListener('hashchange',openTarget);"
      "openTarget();"
      "</script>")

    board_url = board_client.last_known_board_url()
    review_path = f"plan/{snapshot}.html" if snapshot else "plan.html"
    a(board_client.core_js(board_url + review_path if board_url else ""))
    a(PLAN_DECISION_JS)
    a(RETRO_BANNER_JS)

    if mermaid_path:
        a(f'<script src="{esc(mermaid_path)}"></script>')
        # startOnLoad=false then an explicit run, because <details> content is
        # in the DOM but unrendered until opened; mermaid handles that fine but
        # only if it is told to run rather than racing the load event.
        a("<script>"
          "try{mermaid.initialize({startOnLoad:false,theme:'dark',"
          "securityLevel:'strict',flowchart:{useMaxWidth:true}});"
          "mermaid.run({querySelector:'.mermaid'});}"
          "catch(e){console.warn('mermaid failed, diagrams stay as text',e);}"
          "</script>")
    else:
        a("<!-- No vendored mermaid at tools/vendor/mermaid.min.js. Diagrams"
          " render as source text. Run: kit setup dependency mermaid -->")
    a("</div></body></html>")
    return "\n".join(p)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    ap.add_argument("--slice", default="", metavar="NAME",
                    help="also write plan/NAME.html as a comparable snapshot")
    args = ap.parse_args()

    invalid = artefact_errors()
    if invalid:
        print("plan: source artefact validation failed; existing output was preserved",
              file=sys.stderr)
        for name, error in invalid:
            print(f"  {name}: {error}", file=sys.stderr)
        affected = {name for name, _error in invalid}
        if "proposal.json" in affected:
            print("  inspect the contract: kit schema describe proposal", file=sys.stderr)
        if "project.shape.json" in affected:
            print("  inspect the contract: kit schema describe shape", file=sys.stderr)
        return 1

    shape = load(SHAPE)
    prop = load(PROPOSAL)
    cockpit_state = cockpit.plan_view(ROOT)
    history = approved_history()
    try:
        mods, mermaid, tree = module_graph()
    except ArchitectureGraphError as exc:
        print("plan: implementation graph could not be verified; existing output "
              "was preserved", file=sys.stderr)
        print(f"  {exc}", file=sys.stderr)
        return 1
    present = real_files()
    dependency_by_module = {
        str(item.get("path") or ""): item for item in mods if item.get("path")
    }
    mods = [
        dependency_by_module.get(module, {"path": module, "depends_on": []})
        for module in sorted(authored_modules(present))
    ]
    baseline = str(prop.get("baseline_sha", "") or "").strip()
    scoped = touched_since(baseline)
    baseline_files = files_at_baseline(baseline)
    built = present if scoped is None else present & scoped
    deleted = set() if scoped is None else scoped - present
    changed_actions = (
        None
        if scoped is None or baseline_files is None
        else {
            path: (
                "delete" if path in deleted
                else "modify" if path in baseline_files
                else "new"
            )
            for path in scoped
        }
    )
    function_files = set(scoped or set())
    for item in rows(prop, "functions"):
        file_path = str(item.get("file") or "").strip().replace("\\", "/")
        if file_path:
            function_files.add(file_path)
    function_evidence = function_changes_since(
        baseline,
        function_files if scoped is not None else None,
        baseline_files,
        present,
    )
    referenced: set[str] = set()
    for ref in rows(prop, "design_refs"):
        section = str(ref.get("section") or "").split("#", 1)[0].strip().replace("\\", "/")
        if section and not section.startswith("docs/design/"):
            section = "docs/design/" + section
        if section.startswith("docs/design/") and ".." not in section.split("/"):
            referenced.add(posixpath.normpath(section))
    doc_specs = [(DESIGN, "docs/design/", None)]
    doc_targets = {
        path: anchor
        for path, anchor in documentation_targets(doc_specs).items()
        if path in referenced
    }
    kit_docs: List[Dict[str, str]] = []
    design_docs = read_docs(
        DESIGN, "docs/design/", targets=doc_targets, only=referenced
    )

    def build(depth: int, snapshot_label: str = "") -> str:
        return render(
            shape, prop, mods, mermaid, built, recent(), kit_docs, design_docs,
            snapshot_label, mermaid_src(depth), tree, history, doc_targets,
            scoped, deleted, cockpit_state, changed_actions, present,
            baseline_files, function_evidence,
        )

    doc = build(0)

    if args.stdout:
        print(doc)
        return 0
    safe = ""
    snap = None
    if args.slice:
        safe = cockpit.snapshot_label(args.slice)
        snap = SNAP_DIR / f"{safe}.html"
        if snap.exists():
            print(f"plan: snapshot already exists and will not be overwritten: "
                  f"{snap.relative_to(ROOT)}", file=sys.stderr)
            return 1
    OUT.write_text(doc, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    if args.slice:
        SNAP_DIR.mkdir(exist_ok=True)
        # Rendered again at depth 1: the snapshot lives in plan/, so its
        # reference to the vendored bundle needs one level of ../ that the
        # root page does not.
        assert snap is not None
        snap.write_text(build(1, safe), encoding="utf-8")
        print(f"wrote {snap.relative_to(ROOT)}  (snapshot, kept for comparison)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
