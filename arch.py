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
import html
import json
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

CORE_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(CORE_ROOT / "tools"))
import gd_signature  # noqa: E402
import project_context  # noqa: E402

PROJECT_ROOT = CORE_ROOT
PROJECT_DIR = CORE_ROOT / "src"
RULES_FILE = PROJECT_ROOT / "arch.rules.json"
ARCH_DOC = PROJECT_ROOT / "ARCHITECTURE.md"

BEGIN = "<!-- BEGIN GENERATED GRAPH -->"
END = "<!-- END GENERATED GRAPH -->"

EXCLUDED_DIRS = {
    ".agents", ".checklogs", ".claude", ".git", ".github", ".godot",
    ".godot_doc", ".kit", "__pycache__", "addons", "build", "docs",
    "export", "plan",
}

SOURCE_SUFFIXES = frozenset({".gd", ".tscn", ".tres", ".gdshader"})
MAX_ARCHITECTURE_SCAN_DEPTH = 12
MAX_ARCHITECTURE_SCAN_MEMBERS = 20_000
MAX_ARCHITECTURE_FILE_BYTES = 4 * 1024 * 1024
MAX_ARCHITECTURE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARCHITECTURE_GRAPH_MODULES = 4_096
MAX_ARCHITECTURE_GRAPH_EDGES = 65_536
MAX_ARCHITECTURE_EDGE_SOURCES = 131_072

_PATH_IDENTITY_FIELDS = (
    "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_nlink",
)
_HANDLE_IDENTITY_FIELDS = (
    "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_nlink",
)


class ArchitectureScanError(RuntimeError):
    """The project cannot be safely and completely scanned."""


class ScannedFile:
    """One stable file snapshot captured by the architecture scan."""

    __slots__ = ("content", "path", "relative")

    def __init__(self, path: Path, relative: Path, content: bytes) -> None:
        self.path = path
        self.relative = relative
        self.content = content


class ArchitectureScan:
    """The checked source inventory used by every architecture consumer."""

    __slots__ = ("project_file", "root", "sources")

    def __init__(
        self,
        root: Path,
        sources: Tuple[ScannedFile, ...],
        project_file: Optional[ScannedFile],
    ) -> None:
        self.root = root
        self.sources = sources
        self.project_file = project_file

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
    content = _read_optional_architecture_file(RULES_FILE, "arch.rules.json")
    if content is not None:
        return json.loads(content)
    return {"module_depth": 2, "modules": {}, "forbid_autoload_use_in": []}


def _identity(info: os.stat_result, fields: Tuple[str, ...]) -> Tuple[object, ...]:
    return tuple(getattr(info, field, None) for field in fields)


def _is_reparse(info: os.stat_result) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400))
    return bool(int(getattr(info, "st_file_attributes", 0) or 0) & marker)


def _scan_label(relative: Path) -> str:
    rendered = relative.as_posix()
    return rendered if rendered and rendered != "." else "<game root>"


def _blocked(detail: str) -> ArchitectureScanError:
    return ArchitectureScanError(f"architecture scan blocked: {detail}")


def _add_graph_module(modules: Set[str], module: str) -> None:
    if module in modules:
        return
    if len(modules) >= MAX_ARCHITECTURE_GRAPH_MODULES:
        raise _blocked(
            "architecture graph contains more than "
            f"{MAX_ARCHITECTURE_GRAPH_MODULES} modules"
        )
    modules.add(module)


def _validate_directory(
    info: os.stat_result,
    relative: Path,
) -> None:
    label = _scan_label(relative)
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise _blocked(f"linked directory is not allowed: {label}")
    if not stat.S_ISDIR(info.st_mode):
        raise _blocked(f"expected a directory: {label}")


def _validate_regular_file(info: os.stat_result, relative: Path) -> None:
    label = _scan_label(relative)
    if stat.S_ISLNK(info.st_mode) or _is_reparse(info):
        raise _blocked(f"linked file is not allowed: {label}")
    if not stat.S_ISREG(info.st_mode):
        raise _blocked(f"non-regular file is not allowed: {label}")
    if int(getattr(info, "st_nlink", 1)) != 1:
        raise _blocked(f"hard-linked file is not allowed: {label}")


def _read_scanned_file(
    path: Path,
    relative: Path,
    expected: os.stat_result,
) -> bytes:
    """Read one bounded file and prove its path and handle stayed stable."""
    label = _scan_label(relative)
    try:
        path_before = path.lstat()
        _validate_regular_file(path_before, relative)
        if _identity(expected, _PATH_IDENTITY_FIELDS) != _identity(
            path_before, _PATH_IDENTITY_FIELDS
        ):
            raise _blocked(f"source changed during the scan: {label}")
        if path_before.st_size > MAX_ARCHITECTURE_FILE_BYTES:
            raise _blocked(
                f"source file exceeds {MAX_ARCHITECTURE_FILE_BYTES} bytes: {label}"
            )

        flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
        flags |= int(getattr(os, "O_NOFOLLOW", 0))
        descriptor = os.open(path, flags)
        try:
            handle_before = os.fstat(descriptor)
            chunks: List[bytes] = []
            remaining = MAX_ARCHITECTURE_FILE_BYTES + 1
            while remaining > 0:
                chunk = os.read(descriptor, min(65_536, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            content = b"".join(chunks)
            handle_after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        path_after = path.lstat()
    except ArchitectureScanError:
        raise
    except OSError as exc:
        raise _blocked(f"source could not be read: {label}: {exc}") from exc

    if len(content) > MAX_ARCHITECTURE_FILE_BYTES:
        raise _blocked(
            f"source file exceeds {MAX_ARCHITECTURE_FILE_BYTES} bytes: {label}"
        )
    _validate_regular_file(path_after, relative)
    if (
        _identity(path_before, _PATH_IDENTITY_FIELDS)
        != _identity(path_after, _PATH_IDENTITY_FIELDS)
        or _identity(path_before, _HANDLE_IDENTITY_FIELDS)
        != _identity(handle_before, _HANDLE_IDENTITY_FIELDS)
        or _identity(handle_before, _HANDLE_IDENTITY_FIELDS)
        != _identity(handle_after, _HANDLE_IDENTITY_FIELDS)
        or len(content) != handle_before.st_size
    ):
        raise _blocked(f"source changed while it was read: {label}")
    return content


def _read_optional_architecture_file(path: Path, label: str) -> Optional[str]:
    """Read one exact project-root input without following redirects."""
    relative = Path("project-config") / label
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _blocked(f"project file could not be inspected: {label}: {exc}") from exc
    _validate_regular_file(info, relative)
    content = _read_scanned_file(path, relative, info)
    try:
        return content.decode("utf-8")
    except UnicodeError as exc:
        raise _blocked(f"project file is not valid UTF-8: {label}") from exc


def scan_project(project_dir: Optional[Path] = None) -> ArchitectureScan:
    """Capture one bounded, link-free snapshot of the game root.

    All directory entries are visited at most once. Excluded entries are
    counted but never inspected or entered. Only architecture source files and the root
    project.godot contribute to the byte limits because no other file is read.
    """
    selected = PROJECT_DIR if project_dir is None else Path(project_dir)
    root = Path(os.path.abspath(selected))
    try:
        root_info = root.lstat()
        _validate_directory(root_info, Path())
    except ArchitectureScanError:
        raise
    except OSError as exc:
        raise _blocked(f"game root could not be inspected: {root}: {exc}") from exc

    sources: List[ScannedFile] = []
    project_file: Optional[ScannedFile] = None
    member_count = 0
    total_bytes = 0
    excluded = {name.casefold() for name in EXCLUDED_DIRS}
    seen_directories: Set[Tuple[int, int]] = set()
    root_identity = (int(root_info.st_dev), int(root_info.st_ino))
    if root_identity[1]:
        seen_directories.add(root_identity)
    pending: List[Tuple[Path, Path, os.stat_result]] = [(root, Path(), root_info)]

    while pending:
        directory, relative_dir, expected_dir = pending.pop()
        label = _scan_label(relative_dir)
        try:
            before_dir = directory.lstat()
            _validate_directory(before_dir, relative_dir)
            if _identity(expected_dir, _PATH_IDENTITY_FIELDS) != _identity(
                before_dir, _PATH_IDENTITY_FIELDS
            ):
                raise _blocked(f"directory changed during the scan: {label}")

            with os.scandir(directory) as entries:
                for entry in entries:
                    member_count += 1
                    if member_count > MAX_ARCHITECTURE_SCAN_MEMBERS:
                        raise _blocked(
                            "project contains more than "
                            f"{MAX_ARCHITECTURE_SCAN_MEMBERS} scan members"
                        )
                    relative = relative_dir / entry.name
                    rendered = _scan_label(relative)
                    is_project_file = relative.parts == ("project.godot",)
                    path_suffix = Path(entry.name).suffix.casefold()
                    is_source_file = path_suffix in SOURCE_SUFFIXES
                    is_architecture_file = is_project_file or is_source_file
                    is_excluded = entry.name.casefold() in excluded
                    if is_excluded:
                        continue
                    if len(relative.parts) > MAX_ARCHITECTURE_SCAN_DEPTH:
                        raise _blocked(
                            "project nesting exceeds "
                            f"{MAX_ARCHITECTURE_SCAN_DEPTH} levels: {rendered}"
                        )

                    path = directory / entry.name
                    # DirEntry.stat() reports st_nlink=0 on some Windows
                    # filesystems. Path.lstat() supplies the link count that
                    # lets the scan reject hard-linked files reliably.
                    info = path.lstat()
                    if stat.S_ISLNK(info.st_mode):
                        raise _blocked(f"symbolic link is not allowed: {rendered}")
                    if _is_reparse(info):
                        if stat.S_ISDIR(info.st_mode):
                            raise _blocked(
                                f"linked directory is not allowed: {rendered}"
                            )
                        raise _blocked(f"linked file is not allowed: {rendered}")

                    if stat.S_ISDIR(info.st_mode):
                        _validate_directory(info, relative)
                        directory_identity = (int(info.st_dev), int(info.st_ino))
                        if directory_identity[1] and directory_identity in seen_directories:
                            raise _blocked(
                                f"directory alias or hard link is not allowed: {rendered}"
                            )
                        if directory_identity[1]:
                            seen_directories.add(directory_identity)
                        pending.append((path, relative, info))
                        continue

                    if not is_architecture_file:
                        continue
                    _validate_regular_file(info, relative)
                    if total_bytes + info.st_size > MAX_ARCHITECTURE_TOTAL_BYTES:
                        raise _blocked(
                            "source files exceed "
                            f"{MAX_ARCHITECTURE_TOTAL_BYTES} total bytes"
                        )
                    content = _read_scanned_file(path, relative, info)
                    total_bytes += len(content)
                    if total_bytes > MAX_ARCHITECTURE_TOTAL_BYTES:
                        raise _blocked(
                            "source files exceed "
                            f"{MAX_ARCHITECTURE_TOTAL_BYTES} total bytes"
                        )
                    scanned = ScannedFile(path, relative, content)
                    if is_project_file:
                        project_file = scanned
                    else:
                        sources.append(scanned)

            after_dir = directory.lstat()
            _validate_directory(after_dir, relative_dir)
            if _identity(before_dir, _PATH_IDENTITY_FIELDS) != _identity(
                after_dir, _PATH_IDENTITY_FIELDS
            ):
                raise _blocked(f"directory changed during the scan: {label}")
        except ArchitectureScanError:
            raise
        except OSError as exc:
            raise _blocked(f"directory could not be scanned: {label}: {exc}") from exc

    sources.sort(key=lambda item: item.relative.as_posix())
    return ArchitectureScan(root, tuple(sources), project_file)


def source_files(project_dir: Optional[Path] = None) -> List[Path]:
    return [item.path for item in scan_project(project_dir).sources]


def module_of(rel: Path, depth: int) -> str:
    parts = rel.parts[:-1]
    if not parts:
        return "(root)"
    return "/".join(parts[:depth])


def res_to_rel(res_path: str) -> Optional[Path]:
    if not res_path.startswith("res://"):
        return None
    return Path(res_path[len("res://"):])


def file_tree(
    project_dir: Optional[Path] = None,
    *,
    scan: Optional[ArchitectureScan] = None,
) -> Dict[str, Any]:
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
    inventory = scan if scan is not None else scan_project(project_dir)
    for source in inventory.sources:
        if source.relative.suffix.casefold() != ".gd":
            continue
        try:
            text = source.content.decode("utf-8")
        except UnicodeError as exc:
            raise gd_signature.SignatureError(
                f"{source.relative.as_posix()}: source could not be read: {exc}"
            ) from exc
        clean = strip_noise(text)
        try:
            parsed = gd_signature.parse_source_functions(text)
        except gd_signature.SignatureError as exc:
            raise gd_signature.SignatureError(
                f"{source.relative.as_posix()}: {exc}"
            ) from exc
        funcs = [
            {
                "name": function.name,
                "identity": function.identity,
                "class_scope": ".".join(function.scope),
                "signature": function.signature,
                "private": function.name.startswith("_"),
                "annotations": list(function.annotations),
                "abstract": function.is_abstract,
            }
            for function in parsed.values()
        ]
        cls = RE_CLASS_NAME.search(clean)
        out[source.relative.as_posix()] = {
            "class_name": cls.group(1) if cls else "",
            "functions": funcs,
        }
    return out


def build_graph(
    depth: int,
    project_dir: Optional[Path] = None,
    *,
    scan: Optional[ArchitectureScan] = None,
) -> Tuple[
    Dict[str, Set[str]],
    Dict[str, str],
    List[str],
    Dict[str, Set[str]],
    Dict[Tuple[str, str], Set[str]],
]:
    """Return graph data plus the files responsible for every observed edge."""
    inventory = scan if scan is not None else scan_project(project_dir)
    files = inventory.sources
    texts: Dict[str, str] = {}
    for source in files:
        relative = source.relative.as_posix()
        try:
            texts[relative] = source.content.decode("utf-8")
        except UnicodeError as exc:
            raise _blocked(f"source is not UTF-8: {relative}: {exc}") from exc
    class_to_module: Dict[str, str] = {}
    class_to_file: Dict[str, str] = {}
    modules: Set[str] = set()

    for source in files:
        rel = source.relative
        mod = module_of(rel, depth)
        _add_graph_module(modules, mod)
        if rel.suffix.casefold() == ".gd":
            match = RE_CLASS_NAME.search(texts[rel.as_posix()])
            if match:
                class_to_module[match.group(1)] = mod
                class_to_file[match.group(1)] = rel.as_posix()

    autoloads: Dict[str, str] = {}
    if inventory.project_file is not None:
        try:
            text = inventory.project_file.content.decode("utf-8")
        except UnicodeError as exc:
            raise _blocked(f"source is not UTF-8: project.godot: {exc}") from exc
        section = text.split("[autoload]")
        if len(section) > 1:
            body = section[1].split("\n[")[0]
            for name, res in RE_AUTOLOAD.findall(body):
                autoloads[name] = res

    edges: Dict[str, Set[str]] = {m: set() for m in modules}
    autoload_users: Dict[str, Set[str]] = {}
    edge_sources: Dict[Tuple[str, str], Set[str]] = {}
    edge_count = 0
    edge_source_count = 0

    for source in files:
        rel = source.relative
        src_mod = module_of(rel, depth)
        raw = texts[rel.as_posix()]

        targets: Set[str] = set()
        for res in RE_RES_PATH.findall(raw) + RE_EXTENDS_PATH.findall(raw) \
                + RE_EXT_RESOURCE.findall(raw):
            target_rel = res_to_rel(res)
            if target_rel is None:
                continue
            if EXCLUDED_DIRS.intersection(target_rel.parts):
                continue
            target_module = module_of(target_rel, depth)
            _add_graph_module(modules, target_module)
            targets.add(target_module)

        if rel.suffix.casefold() == ".gd":
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
        source_edges = edges.setdefault(src_mod, set())
        for target in sorted(targets):
            _add_graph_module(modules, target)
            edges.setdefault(target, set())
            if target not in source_edges:
                if edge_count >= MAX_ARCHITECTURE_GRAPH_EDGES:
                    raise _blocked(
                        "architecture graph contains more than "
                        f"{MAX_ARCHITECTURE_GRAPH_EDGES} dependency edges"
                    )
                source_edges.add(target)
                edge_count += 1
            sources = edge_sources.setdefault((src_mod, target), set())
            relative = rel.as_posix()
            if relative not in sources:
                if edge_source_count >= MAX_ARCHITECTURE_EDGE_SOURCES:
                    raise _blocked(
                        "architecture graph contains more than "
                        f"{MAX_ARCHITECTURE_EDGE_SOURCES} edge-source associations"
                    )
                sources.add(relative)
                edge_source_count += 1

    return edges, class_to_module, sorted(autoloads), autoload_users, edge_sources


def violation_issues(
    edges: Dict[str, Set[str]],
    autoload_users: Dict[str, Set[str]],
    edge_sources: Dict[Tuple[str, str], Set[str]],
    rules: dict,
) -> List[dict[str, object]]:
    """Return each boundary violation bound to the source file that causes it."""
    issues: List[dict[str, object]] = []
    declared = rules.get("modules", {})
    for src in sorted(edges):
        spec = declared.get(src)
        if spec is None:
            continue
        allowed = set(spec.get("may_depend_on", []))
        for dst in sorted(edges[src]):
            if dst in allowed:
                continue
            message = (
                f"{src} -> {dst} is not permitted "
                f"(arch.rules.json allows: {sorted(allowed) or 'nothing'})"
            )
            for file_path in sorted(edge_sources.get((src, dst), set())):
                issues.append({
                    "code": "dependency-not-permitted",
                    "path": file_path,
                    "line": 0,
                    "message": message,
                })

    forbid = rules.get("forbid_autoload_use_in", [])
    for file_path, names in sorted(autoload_users.items()):
        for prefix in forbid:
            if file_path.startswith(prefix):
                issues.append({
                    "code": "autoload-not-permitted",
                    "path": file_path,
                    "line": 0,
                    "message": (
                        f"{file_path} references autoload(s) {sorted(names)}; "
                        f"{prefix} must stay free of globals -- inject the value instead"
                    ),
                })
    return issues


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

    modules = set(edges)
    for targets in edges.values():
        modules.update(targets)
    ordered_modules = sorted(modules)
    node_ids = {name: f"m{index}" for index, name in enumerate(ordered_modules)}

    def label_text(value: object) -> str:
        normalized = " ".join(str(value).split())
        return html.escape(normalized, quote=True)

    lines = ["```mermaid", "graph TD"] if fenced else ["graph TD"]
    for mod in ordered_modules:
        desc = descriptions.get(mod, "")
        safe_module = label_text(mod)
        label = (
            f"{safe_module}<br/><i>{label_text(desc)}</i>"
            if desc
            else safe_module
        )
        lines.append(f'    {node_ids[mod]}["{label}"]')
    lines.append("")
    for src in sorted(edges):
        for dst in sorted(edges[src]):
            lines.append(f"    {node_ids[src]} --> {node_ids[dst]}")
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


def render_document(project_dir: Path, rules: dict, document: str) -> str:
    """Render the exact target graph into an architecture document template."""
    depth = int(rules.get("module_depth", 2))
    inventory = scan_project(project_dir)
    edges, _classes, _autoloads, _autoload_users, _edge_sources = build_graph(
        depth,
        scan=inventory,
    )
    return splice(document, mermaid(edges, rules))


def activate_context() -> None:
    """Resolve the active project only when this file is used as a command."""
    global PROJECT_ROOT, PROJECT_DIR, RULES_FILE, ARCH_DOC
    context = project_context.load_active_context(CORE_ROOT)
    PROJECT_ROOT = context.project_root
    PROJECT_DIR = context.game_root
    RULES_FILE = PROJECT_ROOT / "arch.rules.json"
    ARCH_DOC = PROJECT_ROOT / "ARCHITECTURE.md"


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

    try:
        inventory = scan_project()
        tree = file_tree(scan=inventory)
        rules = load_rules()
        depth = int(rules.get("module_depth", 2))
        edges, classes, autoloads, autoload_users, edge_sources = build_graph(
            depth,
            scan=inventory,
        )
    except (ArchitectureScanError, gd_signature.SignatureError) as exc:
        message = f"architecture source error: {exc}"
        if args.do_json:
            print(json.dumps({"errors": [message]}, indent=2))
        else:
            print(message, file=sys.stderr)
        return 1

    block = mermaid(edges, rules)
    violations = check_rules(edges, autoload_users, rules)
    scoped_violations = violation_issues(
        edges,
        autoload_users,
        edge_sources,
        rules,
    )

    if args.do_json:
        print(json.dumps({
            "modules": {k: sorted(v) for k, v in edges.items()},
            "classes": classes,
            "autoloads": autoloads,
            "violations": violations,
            "violation_issues": scoped_violations,
            "undeclared_modules": undeclared_modules(edges, rules),
            # The rendered diagram WITHOUT a markdown fence: consumers hand
            # this to mermaid.js, which treats ``` as a syntax error.
            "mermaid": mermaid(edges, rules, fenced=False),
            # File and function detail: the module graph cannot show whether an
            # approved file or signature actually exists yet.
            "tree": tree,
        }, indent=2))
        return 1 if violations else 0

    if args.do_print:
        print(block)
        return 1 if violations else 0

    if args.write:
        doc = _read_optional_architecture_file(ARCH_DOC, ARCH_DOC.name)
        if doc is None:
            doc = "# Architecture\n"
        ARCH_DOC.write_text(splice(doc, block), encoding="utf-8")
        print(f"wrote {ARCH_DOC.name} ({len(edges)} modules)")
        for line in violations:
            print(f"  VIOLATION {line}")
        return 1 if violations else 0

    # --check
    failed = False
    try:
        current = _read_optional_architecture_file(ARCH_DOC, ARCH_DOC.name)
    except ArchitectureScanError as exc:
        print(f"architecture source error: {exc}", file=sys.stderr)
        return 1
    if current is None:
        print(f"{ARCH_DOC.name} is missing; run: kit architecture update")
        failed = True
    else:
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
    activate_context()
    sys.exit(main())
