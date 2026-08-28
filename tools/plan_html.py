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

ROOT = Path(__file__).resolve().parent.parent
CONTEXT = project_context.load_configured_context(ROOT)
GAME_ROOT = CONTEXT.game_root
SHAPE = ROOT / "project.shape.json"
PROPOSAL = ROOT / "proposal.json"
DOCS = ROOT / "docs"
DESIGN = DOCS / "design"
VENDOR = ROOT / "tools" / "vendor"
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
    _, untracked = git(
        "ls-files", "--others", "--exclude-standard", "--", pathspec
    )
    out = set()
    for line in (tracked + "\n" + untracked).splitlines():
        line = line.strip().replace("\\", "/")
        rel = CONTEXT.game_relative(line)
        if rel is None:
            continue
        if is_authored_game_file(rel):
            out.add(rel)
    return out


def module_graph() -> Tuple[List[Dict[str, Any]], str, Dict[str, Any]]:
    """Real modules and a mermaid diagram, from arch.py. Never hand-derived:
    arch.py already owns the definition of a module and the gate already fails
    when its output is stale."""
    script = ROOT / "arch.py"
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
            if path.startswith(("tests", "addons", "tools")):
                continue
            mods.append({"path": path,
                         "depends_on": deps if isinstance(deps, list) else []})
    tree = data.get("tree")
    return mods, str(data.get("mermaid", "") or ""), (tree if isinstance(tree, dict) else {})


def recent(limit: int = 12) -> List[Dict[str, str]]:
    """Recent commits with the files they touched, so a human can jump from a
    plan item to the code that implements it."""
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
@media(max-width:760px){.front-grid{grid-template-columns:1fr}}
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
           cockpit_state: Dict[str, Any] | None = None) -> str:
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
        def inside_scope(path: str) -> bool:
            for boundary in scope_rows:
                raw = str(boundary.get("path") or "")
                if boundary.get("kind") == "file" and path == raw:
                    return True
                if boundary.get("kind") == "directory" and (
                    path == raw or path.startswith(raw.rstrip("/") + "/")
                ):
                    return True
            return False
        extra_paths = sorted(path for path in changed_paths if not inside_scope(path))
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
    decision_count = (1 if approval_required else 0) + len(front_questions)
    decision_count += 1 if needs_design_decision else 0
    decision_count += 1 if status in ("approved", "recorded", "recorded-stale") and extra_paths else 0
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
          'usable Git baseline.</div>')
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

    a('<p class="legend">'
      + state_tag("built") + " as approved &nbsp; "
      + state_tag("missing") + " approved, not written &nbsp; "
      + state_tag("new") + " new file &nbsp; "
      + state_tag("modified") + " changed &nbsp; "
      + state_tag("deleted") + " completed deletion &nbsp; "
      + state_tag("existing") + " approved earlier &nbsp; "
      + state_tag("extra") + " exists without approval</p>")

    hier = hierarchy(prop, tree or {}, built, depth, history)
    considered = rows(prop, "considered_existing")
    if considered:
        # Above the diagram: extend-before-create is the decision a reviewer
        # most needs to check, and below the hierarchy it was easy to miss.
        a("<h2>Existing code considered first</h2>")
        for c in considered:
            a(f'<div class="card"><div class="t mono">{esc(c.get("path"))}</div>'
              f'<div class="m">{esc(c.get("why_not"))}</div></div>')

    if hier:
        a('<p class="legend">Every folder, file'
          + (" and function" if depth >= 3 else "")
          + " in this slice, from <code>res://</code> down."
            " Green is built as approved, dashed amber is approved but not"
            " written, red exists without approval.</p>")
        a(f'<div class="mermaid">{esc(hier)}</div>')

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

    # ---- real architecture, derived
    if mermaid:
        a("<h2>Actual architecture</h2>")
        a('<p class="legend">Derived from code by arch.py. The gate fails when'
          " this is stale, so it cannot drift from reality.</p>")
        a(f'<div class="mermaid">{esc(mermaid)}</div>')

    # ---- recent work, with links into the tree
    if commits:
        a("<h2>Recent changes</h2>")
        for c in commits[:8]:
            files = c.get("files") or []
            links = " ".join(
                f'<a href="{esc(f)}"><code>{esc(f)}</code></a>'
                for f in files[:4])
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
                a(f'<p class="m">{esc(" \u00b7 ".join(warn))}</p>')
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
        for module in sorted(authored_modules(present) | set(dependency_by_module))
    ]
    scoped = touched_since(str(prop.get("baseline_sha", "") or "").strip())
    built = present if scoped is None else present & scoped
    deleted = set() if scoped is None else scoped - present
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
        return render(shape, prop, mods, mermaid, built, recent(),
                      kit_docs, design_docs, snapshot_label, mermaid_src(depth),
                      tree, history, doc_targets, scoped, deleted, cockpit_state)

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
