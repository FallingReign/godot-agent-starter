#!/usr/bin/env python3
"""Render the living plan as one HTML page for a human to read and talk about.

GENERATED, never authored. Five sources, all machine-readable:

    project.shape.json   decisions (settled), direction (heading), questions
    proposal.json        the approved structure for the current slice
    arch.py --json       the real module graph, derived from code
    git                  what changed recently, and since approval
    docs/                how the kit works: gate, rules, workflow
    docs/design/         what the game is: settled design that code depends on

Documentation is rendered to HTML at generation time and inlined, not fetched.
file:// blocks fetch() in Chrome and Edge, so a plan opened by double-clicking
cannot load a sibling .md at all. Inlining is the only approach that works for
the way this page is actually opened.

The page holds nothing of its own, so overwriting it is free. Anything it shows
is either intent (from a json file a human approved) or reality (from code and
git). It never invents a third thing.

    python tools/plan_html.py                # write plan.html
    python tools/plan_html.py --slice slice-1  # also write plan/slice-1.html
    python tools/plan_html.py --stdout

Snapshots exist so a slice that goes wrong can be compared against the plan as
it stood when the slice started, rather than as it stands after the damage.

Conventions kept so this page works inside Lavish (npx -y lavish-axi), which
wraps a local HTML file in an iframe and adds an annotation and chat layer:
  - diagrams go in <div class="mermaid"> so they become editable whiteboards
  - questions use native <input type=radio> so they are interactive as-is
  - all paths are relative
The page renders identically in a plain browser; Lavish is optional.
"""
from __future__ import annotations

import argparse
import html
import json
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

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SHAPE = ROOT / "project.shape.json"
PROPOSAL = ROOT / "proposal.json"
DOCS = ROOT / "docs"
DESIGN = DOCS / "design"
VENDOR = ROOT / "tools" / "vendor"
OUT = ROOT / "plan.html"
SNAP_DIR = ROOT / "plan"

DEPTH = {"hands-off": 0, "module": 1, "file": 2, "function": 3}


def load(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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


def git(*args: str) -> Tuple[int, str]:
    if not shutil.which("git"):
        return 1, ""
    try:
        p = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                           text=True, timeout=20)
        return p.returncode, p.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


SKIP_PREFIX = ("tests/", "addons/", "tools/", ".godot/")
SKIP_SUFFIX = (".import", ".uid")


def is_authored_game_file(rel: str) -> bool:
    """Return whether a relative src path is authored project content."""
    if rel == "project.godot" or rel.startswith(SKIP_PREFIX):
        return False
    return not rel.endswith(SKIP_SUFFIX)


def real_files() -> set:
    """Authored game files, relative to src/. Tests and addons are not structure."""
    out = set()
    if not SRC.is_dir():
        return out
    for f in SRC.rglob("*"):
        if not f.is_file():
            continue
        rel = str(f.relative_to(SRC)).replace("\\", "/")
        if not is_authored_game_file(rel):
            continue
        out.add(rel)
    return out


def touched_since(baseline: str) -> set | None:
    """Files under src/ changed since the approval sha, or None if unknown.

    Without this, every file that predates the slice reads as "unproposed",
    which buries the two or three that genuinely are. Untracked files must be
    included: new work is usually untracked, and omitting it scopes the view to
    nothing while appearing to be complete.
    """
    if not baseline or not shutil.which("git"):
        return None
    code, tracked = git("diff", "--name-only", baseline, "--", "src")
    if code != 0:
        return None
    _, untracked = git("ls-files", "--others", "--exclude-standard", "--", "src")
    out = set()
    for line in (tracked + "\n" + untracked).splitlines():
        line = line.strip().replace("\\", "/")
        if not line.startswith("src/"):
            continue
        rel = line[len("src/"):]
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
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return [], "", {}
    if not isinstance(data, dict):
        return [], "", {}
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
              targets: Optional[Dict[str, str]] = None) -> List[Dict[str, str]]:
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
        rel = f.relative_to(folder).as_posix()
        source = f"{prefix}{rel}"
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
    """Point local Markdown links at their inlined document panes."""
    pattern = re.compile(r"(\]\()([^\s)]+)")
    base = source.rsplit("/", 1)[0]

    def replace(match: "re.Match[str]") -> str:
        url = match.group(2)
        if not url.lower().endswith(".md") or url.startswith(("#", "/")):
            return match.group(0)
        target = posixpath.normpath(posixpath.join(base, url))
        anchor = targets.get(target)
        return f"{match.group(1)}{anchor}" if anchor else match.group(0)

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
    var findings = (state && state.findings) || [];
    var waiting = findings.filter(function(f){
      return (f.state || 'awaiting_review') === 'awaiting_review';
    }).length;

    if(due.due){
      parts.push('<div class="rb due"><b>A retrospective is due.</b> '
        + B.esc(due.unarchived) + ' unarchived slice note'
        + (Number(due.unarchived) === 1 ? '' : 's')
        + ' against a threshold of ' + B.esc(due.threshold) + '. '
        + 'Running one spends quota.'
        + '<a href="/retro.html">Open the retrospective board</a></div>');
    }
    if(waiting){
      parts.push('<div class="rb action"><b>' + B.esc(waiting) + ' finding'
        + (waiting === 1 ? '' : 's') + ' awaiting your decision.</b> '
        + 'Approving one dispatches a kit-builder immediately.'
        + '<a href="/retro.html">Review and action</a></div>');
    }
    host.innerHTML = parts.join('');
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
             "modified": "modified", "new": "new", "existing": "existing"}[state]
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
        except Exception:
            continue
        if str(obj.get("status", "")).strip() != "approved":
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
    if path in built:
        return {"new": "new", "modify": "modified"}.get(act, "built")
    return "missing"


def hierarchy(prop: Dict[str, Any], tree: Dict[str, Any], built: set,
              depth: int, history: Dict[str, Any] | None = None) -> str:
    """A mermaid graph from src/ down to functions, coloured by state.

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
    prop_funcs: Dict[str, set] = {}
    for fn_ in list(rows(prop, "functions")) + list(rows(hist, "functions")):
        f = str(fn_.get("file", "")).strip().replace("\\", "/")
        sig = str(fn_.get("signature", "") or "")
        # Match on the function name: a proposed signature is rarely
        # character-identical to what gets written, and comparing whole strings
        # would report every implemented function as missing.
        m = re.search(r"func\s+([A-Za-z_]\w*)", sig)
        if f and m:
            prop_funcs.setdefault(f, set()).add(m.group(1))

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
    states: Dict[str, List[str]] = {"built": [], "missing": [], "extra": []}

    root = nid("src/")
    lines.append(f'    {root}(["src/"])')

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
        st = "built" if (proposed and exists) else ("missing" if proposed
                                                    else "extra")
        n = nid(f)
        lines.append(f'    {n}["{label(Path(f).name)}"]')
        lines.append(f"    {parent} --> {n}")
        states[st].append(n)

        if depth < 3:
            continue
        real = tree.get(f, {})
        real_names = {fn_["name"] for fn_ in real.get("functions", [])
                      if isinstance(fn_, dict) and fn_.get("name")}
        want = prop_funcs.get(f, set())
        for fn_ in real.get("functions", []):
            if not isinstance(fn_, dict):
                continue
            # Engine callbacks are noise in a design view: nobody proposes
            # _ready, and listing them drowns the functions that were.
            if fn_.get("private") and fn_["name"] not in want:
                continue
            fst = "built" if fn_["name"] in want else "extra"
            fnid = nid(f + "::" + fn_["name"])
            lines.append(f'    {fnid}("{label(str(fn_.get("signature", "")))}")')
            lines.append(f"    {nid(f)} --> {fnid}")
            states[fst].append(fnid)
        for missing in sorted(want - real_names):
            fnid = nid(f + "::" + missing)
            lines.append(f'    {fnid}("{label(missing)}()")')
            lines.append(f"    {nid(f)} --> {fnid}")
            states["missing"].append(fnid)

    lines.append("")
    lines.append("    classDef built fill:#12331f,stroke:#2f9e5e,color:#d7f5e3")
    lines.append("    classDef missing fill:#33290f,stroke:#b08b2a,"
                 "color:#f6e6bd,stroke-dasharray:4 3")
    lines.append("    classDef extra fill:#3a1620,stroke:#c0485f,color:#ffdbe3")
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
           doc_targets: Dict[str, str] | None = None) -> str:
    level = str(shape.get("involvement", "") or "").strip()
    depth = DEPTH.get(level, 3)
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
    status = str(prop.get("status", "") or "").strip().lower()
    if status == "draft" and depth > 0:
        a('<div class="banner draft"><b>Draft awaiting your review.</b> '
          "Nothing has been built against this yet. Read it, then tell the "
          "agent to approve it or what to change.</div>")
    elif status == "approved":
        who = prop.get("approved_by") or ""
        when = prop.get("approved_on") or ""
        a('<div class="banner ok"><b>Approved</b>'
          + (f" by {esc(str(who))}" if who else "")
          + (f" on {esc(str(when))}" if when else "")
          + ". Colours below show what has been built against it.</div>")

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

    # ---- no design behind this slice. This is the one warning that must be
    # impossible to scroll past, because it is the difference between work
    # descended from a decision and work descended from an inference. It is
    # NOT a blocking check: a gate satisfiable only by writing something gets
    # something written, and fabricated ancestry that resolves is worse than
    # an honest gap. So it renders as a decision for the human to make, with
    # the recommendation stated plainly, and the acceptance recorded.
    cur_slice = str(prop.get("slice", "") or "").strip()
    acks = {}
    for x in rows(prop, "acknowledged"):
        if str(x.get("slice", "") or "").strip() == cur_slice:
            w = str(x.get("warning", "") or "").strip()
            if w:
                acks[w] = x
    has_struct = any(rows(prop, k) for k in ("modules", "files", "functions"))
    if has_struct and not refs:
        got = acks.get("no-design-refs")
        a("<h2>No design behind this slice</h2>")
        if got:
            a('<div class="banner ok">')
            a("<b>&#9745; Accepted.</b> You chose to build this without design "
              "backing it.")
            w = str(got.get("why", "") or "").strip()
            who = str(got.get("by", "") or "").strip()
            when = str(got.get("on", "") or "").strip()
            if w:
                a(f"<div style='margin-top:6px'>{esc(w)}</div>")
            tail = " &middot; ".join(esc(x) for x in (who, when) if x)
            if tail:
                a(f"<div class='m' style='margin-top:4px'>{tail}</div>")
            a("</div>")
        else:
            a('<div class="banner draft">')
            a("<b>&#9744; Nothing in <code>docs/design/</code> says why this "
              "work exists.</b>")
            a("<div style='margin-top:8px'>Ticking this means the agent builds "
              "on its own inference rather than on your design. The inference "
              "may be reasonable, but it will not be written down, and the next "
              "slice will build on top of it.</div>")
            a("<div style='margin-top:8px'><b>Recommended instead:</b> tell the "
              "agent to enter design mode and work the section out with you "
              "first. It is one short conversation, and everything after it is "
              "descended from a decision you made.</div>")
            a("<div style='margin-top:8px'>To accept anyway, tell the agent to "
              "record it in <code>acknowledged[]</code> with your reason. It "
              "must not tick this for you, and the acceptance applies to this "
              "slice only.</div>")
            a("</div>")

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
        a('<p class="empty">Hands-off: no structure was proposed for approval.'
          " Everything below is a record of what exists.</p>")
    if not prop_mods and not prop_files and depth > 0:
        a('<p class="empty">Nothing approved yet. The agent proposes the'
          " structure, you approve it, and it lands here before any code.</p>")

    a('<p class="legend">'
      + state_tag("built") + " as approved &nbsp; "
      + state_tag("missing") + " approved, not written &nbsp; "
      + state_tag("new") + " new file &nbsp; "
      + state_tag("modified") + " changed &nbsp; "
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
          + " in this slice, from <code>src/</code> down."
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
                    "modified" if fact == "modify" else "missing")
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
        if mod in prop_mods and mod in real_mods:
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
        if q.get("blocks"):
            a(f'<div class="m">blocks: {esc(q.get("blocks"))}</div>')
        opts = q.get("options")
        if isinstance(opts, list) and opts:
            qid = esc(q.get("id") or "q")
            for i, opt in enumerate(opts):
                a(f'<label class="opt"><input type="radio" name="{qid}"'
                  f' value="{i}"> {esc(opt)}</label>')
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
            a('<div class="card"><div class="t">'
              f'{esc(r.get("what_changed"))}</div>')
            if r.get("because"):
                a(f'<div class="m">because: {esc(r.get("because"))}</div>')
            a(f'<div class="m mono">{esc(r.get("date"))} · '
              f'{esc(r.get("approved_by"))}</div></div>')

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

    a(board_client.core_js())
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
          " render as source text. Run: python bootstrap.py --fix -->")
    a("</div></body></html>")
    return "\n".join(p)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stdout", action="store_true", help="print instead of writing")
    ap.add_argument("--slice", default="", metavar="NAME",
                    help="also write plan/NAME.html as a comparable snapshot")
    args = ap.parse_args()

    shape = load(SHAPE)
    prop = load(PROPOSAL)
    history = approved_history()
    mods, mermaid, tree = module_graph()
    built = real_files()
    scoped = touched_since(str(prop.get("baseline_sha", "") or "").strip())
    if scoped is not None:
        built &= scoped
    doc_specs = [(DOCS, "docs/", DESIGN), (DESIGN, "docs/design/", None)]
    doc_targets = documentation_targets(doc_specs)
    kit_docs = read_docs(DOCS, "docs/", skip=DESIGN, targets=doc_targets)
    design_docs = read_docs(DESIGN, "docs/design/", targets=doc_targets)

    def build(depth: int) -> str:
        return render(shape, prop, mods, mermaid, built, recent(),
                      kit_docs, design_docs, args.slice, mermaid_src(depth),
                      tree, history, doc_targets)

    doc = build(0)

    if args.stdout:
        print(doc)
        return 0
    OUT.write_text(doc, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)}")
    if args.slice:
        SNAP_DIR.mkdir(exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "-"
                       for c in args.slice)[:60]
        snap = SNAP_DIR / f"{safe}.html"
        # Rendered again at depth 1: the snapshot lives in plan/, so its
        # reference to the vendored bundle needs one level of ../ that the
        # root page does not.
        snap.write_text(build(1), encoding="utf-8")
        print(f"wrote {snap.relative_to(ROOT)}  (snapshot, kept for comparison)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
