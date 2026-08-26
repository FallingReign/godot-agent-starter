#!/usr/bin/env python3
"""Derive the module dependency graph from source, draw it, and enforce it.

The diagram in ARCHITECTURE.md is GENERATED, never hand-written. `check.py arch`
regenerates it and fails if the committed copy differs, so it cannot drift.
Boundary rules live in arch.rules.json and are enforced from the same graph, so
the picture and the constraint share one source of truth: the code.

Why not use Godot's own API: ResourceLoader.get_dependencies() returns an empty
array for .gd files -- GDScriptParser::get_dependencies() is still a stub. And
gdtoolkit's parser is documented as unstable and has hard-failed on new syntax
after every engine release. So this parses the stable text formats directly,
with no dependencies and no Godot binary.

Public usage:
  kit verify --stage arch    check the graph and boundaries
  kit architecture update    regenerate the diagram in place
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "tools"))
import project_context  # noqa: E402

CONTEXT = project_context.load_configured_context(ROOT)
PROJECT_DIR = CONTEXT.game_root
RULES_FILE = ROOT / "arch.rules.json"
ARCH_DOC = ROOT / "ARCHITECTURE.md"

BEGIN = "<!-- BEGIN GENERATED GRAPH -->"
END = "<!-- END GENERATED GRAPH -->"

EXCLUDED_DIRS = {
    ".agents", ".checklogs", ".claude", ".git", ".github", ".godot",
    ".godot_doc", ".kit", "__pycache__", "addons", "build", "docs",
    "export", "plan",
}

# --- extraction patterns. Deliberately simple and format-stable. -------------
RE_CLASS_NAME = re.compile(r"^\s*class_name\s+([A-Za-z_][A-Za-z0-9_]*)", re.M)
RE_RES_PATH = re.compile(r'(?:preload|load)\s*\(\s*"(res://[^"]+)"')
RE_EXTENDS_PATH = re.compile(r'^\s*extends\s+"(res://[^"]+)"', re.M)
RE_EXT_RESOURCE = re.compile(r'\[ext_resource[^\]]*path\s*=\s*"(res://[^"]+)"')
RE_AUTOLOAD = re.compile(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*"\*?(res://[^"]+)"', re.M)
RE_LINE_COMMENT = re.compile(r"#.*$", re.M)
RE_STRING = re.compile(r'"[^"\n]*"|\'[^\'\n]*\'')


def strip_noise(text: str) -> str:
    """Remove strings then comments, so identifiers inside them are not edges."""
    return RE_LINE_COMMENT.sub("", RE_STRING.sub('""', text))


def load_rules() -> dict:
    if RULES_FILE.is_file():
        return json.loads(RULES_FILE.read_text(encoding="utf-8"))
    return {"module_depth": 2, "modules": {}, "forbid_autoload_use_in": []}


def source_files() -> List[Path]:
    out: List[Path] = []
    for ext in ("*.gd", "*.tscn", "*.tres", "*.gdshader"):
        for path in PROJECT_DIR.rglob(ext):
            rel = path.relative_to(PROJECT_DIR)
            if EXCLUDED_DIRS.intersection(rel.parts):
                continue
            out.append(path)
    return sorted(out)


def module_of(rel: Path, depth: int) -> str:
    parts = rel.parts[:-1]
    if not parts:
        return "(root)"
    return "/".join(parts[:depth])


def res_to_rel(res_path: str) -> Optional[Path]:
    if not res_path.startswith("res://"):
        return None
    return Path(res_path[len("res://"):])


RE_FUNC = re.compile(
    r"^\s*(?:@\w+(?:\([^)]*\))?\s*\n\s*)*"      # decorators on preceding lines
    r"(static\s+)?func\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*(->\s*[^:]+)?:",
    re.MULTILINE)


def file_tree() -> Dict[str, Any]:
    """Every source file under the configured game root, with its functions.

    The module graph answers "what depends on what". It cannot answer "does the
    thing I approved exist yet", because a module is a directory and approval
    happens at file and function level. This walks the actual tree so the plan
    view can colour each node by whether it is implemented, proposed, or
    unproposed.

    Signatures are read from source rather than from ClassDB so this works
    without a Godot binary and without the project loading.
    """
    out: Dict[str, Any] = {}
    for path in sorted(PROJECT_DIR.rglob("*.gd")):
        rel = path.relative_to(PROJECT_DIR)
        if EXCLUDED_DIRS.intersection(rel.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        clean = strip_noise(text)
        funcs = []
        for m in RE_FUNC.finditer(clean):
            is_static = bool(m.group(1))
            name = m.group(2)
            args = " ".join(m.group(3).split())
            ret = (m.group(4) or "").strip()
            sig = f"{'static ' if is_static else ''}func {name}({args})"
            if ret:
                sig += f" {ret}"
            funcs.append({"name": name, "signature": sig,
                          "private": name.startswith("_")})
        cls = RE_CLASS_NAME.search(clean)
        out[str(rel).replace("\\", "/")] = {
            "class_name": cls.group(1) if cls else "",
            "functions": funcs,
        }
    return out


def build_graph(depth: int) -> Tuple[Dict[str, Set[str]], Dict[str, str], List[str], Dict[str, Set[str]]]:
    """Return (edges, class_to_module, autoload_names, autoload_users)."""
    files = source_files()
    class_to_module: Dict[str, str] = {}
    class_to_file: Dict[str, str] = {}
    modules: Set[str] = set()

    for path in files:
        rel = path.relative_to(PROJECT_DIR)
        mod = module_of(rel, depth)
        modules.add(mod)
        if path.suffix == ".gd":
            match = RE_CLASS_NAME.search(path.read_text(encoding="utf-8", errors="replace"))
            if match:
                class_to_module[match.group(1)] = mod
                class_to_file[match.group(1)] = rel.as_posix()

    autoloads: Dict[str, str] = {}
    project_file = PROJECT_DIR / "project.godot"
    if project_file.is_file():
        text = project_file.read_text(encoding="utf-8", errors="replace")
        section = text.split("[autoload]")
        if len(section) > 1:
            body = section[1].split("\n[")[0]
            for name, res in RE_AUTOLOAD.findall(body):
                autoloads[name] = res

    edges: Dict[str, Set[str]] = {m: set() for m in modules}
    autoload_users: Dict[str, Set[str]] = {}

    for path in files:
        rel = path.relative_to(PROJECT_DIR)
        src_mod = module_of(rel, depth)
        raw = path.read_text(encoding="utf-8", errors="replace")

        targets: Set[str] = set()
        for res in RE_RES_PATH.findall(raw) + RE_EXTENDS_PATH.findall(raw) \
                + RE_EXT_RESOURCE.findall(raw):
            target_rel = res_to_rel(res)
            if target_rel is None:
                continue
            if EXCLUDED_DIRS.intersection(target_rel.parts):
                continue
            targets.add(module_of(target_rel, depth))

        if path.suffix == ".gd":
            body = strip_noise(raw)
            own = RE_CLASS_NAME.search(raw)
            own_name = own.group(1) if own else None
            words = set(re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", body))
            for word in words:
                if word == own_name:
                    continue
                if word in class_to_module:
                    targets.add(class_to_module[word])
                if word in autoloads:
                    autoload_users.setdefault(rel.as_posix(), set()).add(word)

        targets.discard(src_mod)
        edges.setdefault(src_mod, set()).update(targets)
        for target in targets:
            edges.setdefault(target, set())

    return edges, class_to_module, sorted(autoloads), autoload_users


def mermaid(edges: Dict[str, Set[str]], rules: dict, fenced: bool = True) -> str:
    """The diagram. Two callers want two different things and conflating them
    silently breaks one of them:

      fenced=True   for ARCHITECTURE.md, where ```mermaid is what makes a
                    markdown renderer treat the block as a diagram
      fenced=False  for --json, consumed by plan_html.py and handed straight to
                    mermaid.js, which cannot parse a markdown fence and fails
                    with a syntax error rather than rendering

    The fenced form was previously the only form, so the HTML plan showed the
    diagram source as literal text.
    """
    descriptions = {k: v.get("description", "") for k, v in rules.get("modules", {}).items()}

    def node_id(name: str) -> str:
        return re.sub(r"[^A-Za-z0-9]", "_", name)

    lines = ["```mermaid", "graph TD"] if fenced else ["graph TD"]
    for mod in sorted(edges):
        desc = descriptions.get(mod, "")
        label = f"{mod}<br/><i>{desc}</i>" if desc else mod
        lines.append(f'    {node_id(mod)}["{label}"]')
    lines.append("")
    for src in sorted(edges):
        for dst in sorted(edges[src]):
            lines.append(f"    {node_id(src)} --> {node_id(dst)}")
    if fenced:
        lines.append("```")
    return "\n".join(lines)


def check_rules(edges: Dict[str, Set[str]], autoload_users: Dict[str, Set[str]],
                rules: dict) -> List[str]:
    violations: List[str] = []
    declared = rules.get("modules", {})

    for src in sorted(edges):
        spec = declared.get(src)
        if spec is None:
            continue
        allowed = set(spec.get("may_depend_on", []))
        for dst in sorted(edges[src]):
            if dst not in allowed:
                violations.append(
                    f"{src} -> {dst} is not permitted "
                    f"(arch.rules.json allows: {sorted(allowed) or 'nothing'})"
                )

    forbid = rules.get("forbid_autoload_use_in", [])
    for file_path, names in sorted(autoload_users.items()):
        for prefix in forbid:
            if file_path.startswith(prefix):
                violations.append(
                    f"{file_path} references autoload(s) {sorted(names)}; "
                    f"{prefix} must stay free of globals -- inject the value instead"
                )
    return violations


def splice(doc: str, block: str) -> str:
    generated = f"{BEGIN}\n\n{block}\n\n{END}"
    if BEGIN in doc and END in doc:
        head = doc.split(BEGIN)[0]
        tail = doc.split(END)[1]
        return head + generated + tail
    return doc.rstrip() + "\n\n" + generated + "\n"


def undeclared_modules(edges: Dict[str, Set[str]], rules: dict) -> List[str]:
    """Modules that exist in code but were never declared in arch.rules.json.

    At involvement "module" or "function" the human approves structure before it
    is built, and arch.rules.json is where an approved module gets recorded. So a
    module present in the tree and absent from that file is structure that
    appeared without going through approval.

    Reported, never failed, for two reasons. An undeclared module is legitimate
    at involvement "hands-off". And a module declared in the file but not yet
    written is normal mid-slice. This surfaces the difference between intent and
    reality so the human can see it in one place; it does not adjudicate.
    """
    declared = set((rules.get("modules") or {}).keys())
    return sorted(m for m in edges if m not in declared)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true")
    group.add_argument("--write", action="store_true")
    group.add_argument("--print", dest="do_print", action="store_true")
    group.add_argument("--json", dest="do_json", action="store_true")
    args = parser.parse_args()

    rules = load_rules()
    depth = int(rules.get("module_depth", 2))
    edges, classes, autoloads, autoload_users = build_graph(depth)
    block = mermaid(edges, rules)
    violations = check_rules(edges, autoload_users, rules)

    if args.do_json:
        print(json.dumps({
            "modules": {k: sorted(v) for k, v in edges.items()},
            "classes": classes,
            "autoloads": autoloads,
            "violations": violations,
            "undeclared_modules": undeclared_modules(edges, rules),
            # The rendered diagram WITHOUT a markdown fence: consumers hand
            # this to mermaid.js, which treats ``` as a syntax error.
            "mermaid": mermaid(edges, rules, fenced=False),
            # File and function detail: the module graph cannot show whether an
            # approved file or signature actually exists yet.
            "tree": file_tree(),
        }, indent=2))
        return 1 if violations else 0

    if args.do_print:
        print(block)
        return 1 if violations else 0

    if args.write:
        doc = ARCH_DOC.read_text(encoding="utf-8") if ARCH_DOC.is_file() else "# Architecture\n"
        ARCH_DOC.write_text(splice(doc, block), encoding="utf-8")
        print(f"wrote {ARCH_DOC.name} ({len(edges)} modules)")
        for line in violations:
            print(f"  VIOLATION {line}")
        return 1 if violations else 0

    # --check
    failed = False
    if not ARCH_DOC.is_file():
        print(f"{ARCH_DOC.name} is missing; run: kit architecture update")
        failed = True
    else:
        current = ARCH_DOC.read_text(encoding="utf-8")
        if splice(current, block) != current:
            print(f"{ARCH_DOC.name} diagram is stale; run: kit architecture update")
            failed = True
    for line in violations:
        print(f"boundary violation: {line}")
        failed = True
    extra = undeclared_modules(edges, rules)
    if extra:
        print("undeclared module(s) - present in code, absent from "
              "arch.rules.json:")
        for m in extra:
            print(f"  {m}")
        print("  If structure was approved, declare it (description + "
              "may_depend_on).")
        print("  If it was not, this is structure built without approval.")
    if not failed:
        print(f"{len(edges)} module(s), graph current, no boundary violations")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
