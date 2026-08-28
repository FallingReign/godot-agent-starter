#!/usr/bin/env python3
"""Durable, evidence-bound dispatch-prompt artifacts.

The retrospective pipeline already pays for an expensive evidence pass:
`session_digest` opens every cited `events.jsonl` and reduces it to counts and
verbatim human messages. Dispatch used to pay for that pass *again* -- once per
finding -- because `board.build_finding_prompt()` called
`retro_rank.citation_report()` per finding, so a single large session was
reopened and reparsed as many times as it was cited. That made
`/api/dispatch/prepare` take close to a minute, and it meant the worker's prompt
was rebuilt from session logs that may have changed since the human reviewed it.

This module makes the prompt an artifact instead:

    retro_rank.py (ranking) -> .kit/runtime/retro/queue/<slug>.json + index.json
    retro.html / board.py   -> read the artifact, never a session log

`build()` resolves each report only against the immutable session snapshot named
in that report.  It never discovers live Copilot sessions.  That distinction is
important: two reports can both cite ``S1:H1`` while binding that ordinal to
different captured sessions, and a legacy report must not acquire new meaning
because the live discovery order changed.

    python tools/retro_queue.py            regenerate artifacts, print a summary
    python tools/retro_queue.py --list     show what is on disk, and staleness

The `__main__` exists for regeneration and debugging only. The module is meant
to be imported: this project is moving away from bespoke CLIs and toward the
board calling library functions directly.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent.parent
RETRO_DIR = ROOT / "docs" / "retro"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import runtime_paths  # noqa: E402

_RUNTIME = runtime_paths.resolve(ROOT)
QUEUE_DIR = _RUNTIME.retro_queue
INDEX_FILE = QUEUE_DIR / "index.json"
SESSION_EVIDENCE_DIR = _RUNTIME.session_evidence

import retro_rank  # noqa: E402  (needs sys.path set first)
import session_evidence  # noqa: E402

SCHEMA = 4

_SNAPSHOT_NAME_RE = re.compile(r"^[0-9a-f]{64}\.json$")
_SECTION_ORDER = ("problem", "proposal", "measure")
_ROOT_KIT_FILES = frozenset({
    ".agent-kit.json", ".gate.sha256", ".gdlintrc", ".gitignore",
    "AGENTS.md", "ARCHITECTURE.md", "CLAUDE.md", "README.md", "SETUP.md",
    "VERIFY.md", "LICENSE", "LICENSE.md", "VERSION", "arch.py",
    "arch.rules.json", "bootstrap.py", "check.py", "gate.rules.json",
    "dependencies.lock.json", "import_profiles.json", "kit", "kit.cmd",
    "kit.config.json", "kit.py", "sanitise.py",
})
_BLOCKED_PATH_PARTS = frozenset({
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git",
    ".checklogs", "node_modules",
})

# The restrictions every dispatched kit-builder must carry. They live here, not
# in board.py, because the prompt is now an artifact written at ranking time --
# if the header were applied at dispatch time the stored `prompt` would not be
# the prompt actually sent, which is exactly the property this file exists to
# guarantee. `board.DISPATCH_HEADER` re-exports this name.
DISPATCH_HEADER = (
    "You are the kit-builder, dispatched from the retrospective board to "
    "implement the finding below. Follow .github/agents/kit-builder.agent.md: "
    "name the finding you are implementing and quote the occurrences it cites "
    "before changing anything and edit only the reviewed `fix_files`. This is "
    "host-finalized edit mode: shell, URL, memory, MCP, sub-agent and temporary "
    "directory tools are unavailable. Do not attempt to run Git, Godot, tests, "
    "the kit launcher, or a result-artifact script; do not touch game or design "
    "files, `.git`, or private runtime state. Make the smallest requested file "
    "edits, then "
    "exit with a concise summary. The trusted dispatcher independently checks "
    "the exact scope, runs `kit verify --static`, creates the commit, and integrates it "
    "only if every proof passes. Integrity acceptance remains human-only.\n\n"
)

# Where render_prompt() appends the human's approval comment. A fixed, obvious
# delimiter so the worker can tell the reviewed proposal from the amendment to
# it, and so the appended block is greppable in the private board run ledger.
COMMENT_HEADING = "## Amendment from the human who approved this finding"
COMMENT_PREAMBLE = (
    "The finding above was approved with the following comment. It is part of "
    "the instruction: build the proposal as amended here, and where the two "
    "disagree, this comment wins."
)

_SLUG_RE = re.compile(r"[^a-z0-9-]+")
_CANONICAL_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_SLUG_CHARS = 128


def slug_for(title: str) -> str:
    """Filename stem for a finding title.

    `normalise_title` is the join key against accepted.json and must not
    change; the slug is that key reduced to characters legal in a filename on
    every platform this runs on.
    """
    reduced = _SLUG_RE.sub("", retro_rank.normalise_title(title)).strip("-")
    return re.sub(r"-+", "-", reduced)[:MAX_SLUG_CHARS].rstrip("-")


def canonical_slug(value: object) -> str | None:
    """Return one bounded queue slug, or ``None`` for any alternate spelling."""
    if not isinstance(value, str) or value != value.strip():
        return None
    if not value or len(value) > MAX_SLUG_CHARS:
        return None
    return value if _CANONICAL_SLUG_RE.fullmatch(value) is not None else None


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(value: dict) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ) + "\n").encode("utf-8")


def template_sha256() -> str:
    """Identity of the exact prompt join contract used after human approval."""
    return _sha256(_canonical_json_bytes({
        "artifact_schema": SCHEMA,
        "dispatch_header": DISPATCH_HEADER,
        "comment_heading": COMMENT_HEADING,
        "comment_preamble": COMMENT_PREAMBLE,
    }))


def artifact_sha256(item: dict) -> str:
    """Content identity of the complete queue record, excluding this digest."""
    payload = dict(item)
    payload.pop("artifact_sha256", None)
    return _sha256(_canonical_json_bytes(payload))


def review_identity(item: dict) -> dict[str, object]:
    """Digests a browser must echo to approve exactly this rendered artifact."""
    identity: dict[str, object] = {
        "artifact_schema": item.get("schema"),
        "artifact_sha256": item.get("artifact_sha256", ""),
        "prompt_sha256": item.get("prompt_sha256", ""),
        "template_sha256": item.get("template_sha256", ""),
    }
    identity["review_sha256"] = _sha256(_canonical_json_bytes(identity))
    return identity


def dispatch_prompt_sha256(item: dict, comment: str = "") -> str:
    return _sha256(render_prompt(item, comment).encode("utf-8"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _human_turns(finding: dict, digests: dict[str, dict]) -> list[dict]:
    """Resolve each `S<n>:H<n>` citation to its verbatim quote, once.

    A citation whose quote is not in the digest window resolves to `""` rather
    than being dropped: the worker should see that the evidence was cited and
    could not be shown, not silently receive a shorter list than the human
    reviewed.
    """
    out: list[dict] = []
    for cite in finding["human_turns"]:
        cite = cite.strip()
        quote = ""
        m = retro_rank.CITE_RE.fullmatch(cite)
        if m and m.group(2):
            d = digests.get(f"S{m.group(1)}")
            hnum = int(m.group(2))
            if d and not d.get("error"):
                human = d.get("human", [])
                if 1 <= hnum <= len(human):
                    quote = human[hnum - 1]
        out.append({"cite": cite, "quote": quote})
    return out


def _canonical_path(value: str | Path) -> str:
    """Match the canonical repository spelling used by session_evidence.py."""
    return os.path.normcase(os.path.abspath(value)).replace("\\", "/").rstrip("/")


def _add(blockers: list[str], message: str) -> None:
    if message not in blockers:
        blockers.append(message)


def _snapshot_path(reference: str) -> tuple[Path | None, list[str]]:
    """Resolve one repository-relative snapshot path without following aliases."""
    blockers: list[str] = []
    reference = (reference or "").strip()
    if not reference:
        return None, ["evidence_snapshot: missing (legacy report)"]
    if "\\" in reference or any(ord(ch) < 32 for ch in reference):
        return None, ["evidence_snapshot: path is not a safe repository-relative path"]

    relative = PurePosixPath(reference)
    if (relative.is_absolute() or not relative.parts
            or any(part in ("", ".", "..") for part in relative.parts)
            or ":" in relative.parts[0]):
        return None, ["evidence_snapshot: path is not a safe repository-relative path"]

    allowed = SESSION_EVIDENCE_DIR
    candidate = ROOT.joinpath(*relative.parts)
    try:
        root_resolved = ROOT.resolve(strict=True)
        allowed_resolved = allowed.resolve(strict=False)
        allowed_resolved.relative_to(root_resolved)
        candidate.resolve(strict=False).relative_to(allowed_resolved)
        candidate.relative_to(allowed)
    except (OSError, RuntimeError, ValueError):
        return None, [
            "evidence_snapshot: path must be under the configured session evidence root"
        ]

    cursor = ROOT
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            _add(blockers, "evidence_snapshot: symlinks are not accepted")
            break
    if not _SNAPSHOT_NAME_RE.fullmatch(candidate.name):
        _add(blockers, "evidence_snapshot: filename must be a lowercase SHA-256")
    return candidate, blockers


def _snapshot_digests(reference: str) -> tuple[dict[str, dict], list[str]]:
    """Load and validate a content-addressed session evidence manifest."""
    path, blockers = _snapshot_path(reference)
    if path is None or blockers:
        return {}, blockers
    try:
        content = path.read_bytes()
    except OSError:
        return {}, ["evidence_snapshot: file is missing or unreadable"]
    if hashlib.sha256(content).hexdigest() != path.stem:
        return {}, ["evidence_snapshot: content hash does not match filename"]
    try:
        manifest = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}, ["evidence_snapshot: content is not valid UTF-8 JSON"]
    if not isinstance(manifest, dict):
        return {}, ["evidence_snapshot: JSON root must be an object"]
    canonical = session_evidence.canonical_bytes(manifest)
    if canonical != content:
        return {}, ["evidence_snapshot: JSON is not in canonical manifest form"]
    current = (manifest.get("schema") == session_evidence.SCHEMA
               and manifest.get("kind") == session_evidence.KIND)
    legacy = (manifest.get("schema") == session_evidence.LEGACY_SCHEMA
              and manifest.get("kind") == session_evidence.LEGACY_KIND)
    if not current and not legacy:
        return {}, ["evidence_snapshot: unsupported schema or kind"]
    repository_matches = False
    if current and isinstance(manifest.get("repository"), dict):
        repository_matches = (
            manifest["repository"].get("scope_id")
            == session_evidence.repository_scope(ROOT)["scope_id"]
        )
    elif legacy:
        try:
            repository_matches = (
                _canonical_path(str(manifest.get("repository") or ""))
                == _canonical_path(ROOT)
            )
        except (OSError, ValueError):
            repository_matches = False
    if not repository_matches:
        return {}, ["evidence_snapshot: repository provenance does not match this repository"]

    sessions = manifest.get("sessions")
    if not isinstance(sessions, list):
        return {}, ["evidence_snapshot: sessions must be a list"]

    digests: dict[str, dict] = {}
    ordinals: set[int] = set()
    for session in sessions:
        if not isinstance(session, dict):
            _add(blockers, "evidence_snapshot: every session must be an object")
            continue
        ordinal = session.get("ordinal")
        if (isinstance(ordinal, bool) or not isinstance(ordinal, int)
                or ordinal < 1 or ordinal in ordinals):
            _add(blockers, "evidence_snapshot: session ordinals must be unique positive integers")
            continue
        ordinals.add(ordinal)
        session_id = session.get("session_id")
        evidence = session.get("evidence")
        if not isinstance(session_id, str) or not session_id or not isinstance(evidence, dict):
            _add(blockers, "evidence_snapshot: session identity or evidence is invalid")
            continue
        messages = evidence.get("human_messages")
        if not isinstance(messages, list):
            _add(blockers, "evidence_snapshot: human_messages must be a list")
            continue
        human: list[str] = []
        for number, message in enumerate(messages, 1):
            expected = f"{session_id}:H{number}"
            if (not isinstance(message, dict)
                    or message.get("citation") != expected
                    or not isinstance(message.get("text"), str)):
                _add(blockers, "evidence_snapshot: human message records are invalid")
                break
            human.append(message["text"])
        else:
            digests[f"S{ordinal}"] = {
                "id": session_id,
                "human": human,
                "error": session.get("error"),
            }
    if ordinals != set(range(1, len(sessions) + 1)):
        _add(blockers, "evidence_snapshot: session ordinals must be contiguous from one")
    return ({} if blockers else digests), blockers


def _turn_blockers(human_turns: list[dict]) -> list[str]:
    blockers: list[str] = []
    for turn in human_turns:
        cite = str(turn.get("cite") or "")
        if not turn.get("quote"):
            _add(blockers, f"human_turns: unresolved citation {cite or '(empty)'}")
    return blockers


def _section_blockers(body: object) -> list[str]:
    if not isinstance(body, str):
        return ["sections: body must be text"]
    matches = list(retro_rank.SECTION_RE.finditer(body))
    names = tuple(match.group(1).lower() for match in matches)
    if names != _SECTION_ORDER:
        return ["sections: expected exactly Problem, Proposal, Measure in that order"]
    blockers: list[str] = []
    if body[:matches[0].start()].strip():
        _add(blockers, "sections: content appears before Problem")
    for index, name in enumerate(_SECTION_ORDER):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        if not body[matches[index].end():end].strip():
            _add(blockers, f"sections: {name.title()} is empty")
    return blockers


def _is_kit_file(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    if "\\" in value or any(ord(ch) < 32 for ch in value):
        return False
    path = PurePosixPath(value)
    lower_parts = tuple(part.lower() for part in path.parts)
    if (path.is_absolute() or not path.parts
            or any(part in ("", ".", "..") for part in path.parts)
            or ":" in path.parts[0]
            or any(part in _BLOCKED_PATH_PARTS for part in lower_parts)):
        return False
    if len(path.parts) == 1:
        return value in _ROOT_KIT_FILES or value.endswith(".rules.json")
    if lower_parts[0] == "tools":
        return True
    if lower_parts[:2] in ((".agents", "skills"), (".github", "agents")):
        return True
    if lower_parts[0] == "docs":
        if (len(lower_parts) < 2 or lower_parts[1] == "design"
                or lower_parts[:2] == ("docs", "decisions.md")):
            return False
        if lower_parts[1] == "retro":
            return lower_parts == ("docs", "retro", "readme.md")
        return path.suffix.lower() == ".md"
    return False


def _finding_blockers(finding: dict, human_turns: list[dict],
                      snapshot_blockers: list[str]) -> list[str]:
    blockers = list(snapshot_blockers)
    for blocker in _turn_blockers(human_turns) + _section_blockers(finding.get("body")):
        _add(blockers, blocker)
    fix_files = finding.get("fix_files")
    if not isinstance(fix_files, list) or not fix_files:
        _add(blockers, "fix_files: at least one kit-owned path is required")
    else:
        for value in fix_files:
            if not _is_kit_file(value):
                _add(blockers, f"fix_files: unsafe or non-kit path {value!r}")
    fix_lines = finding.get("fix_lines")
    if isinstance(fix_lines, bool) or not isinstance(fix_lines, int) or fix_lines < 1:
        _add(blockers, "fix_lines: must be a positive integer")
    return blockers


def dispatch_eligibility(item: dict) -> tuple[bool, list[str]]:
    """Revalidate an artifact at GET/approval time, failing legacy data closed."""
    if item.get("schema") != SCHEMA:
        return False, ["artifact: legacy schema has no verified evidence binding"]
    expected_prompt_digest = _sha256(str(item.get("prompt", "")).encode("utf-8"))
    expected_template_digest = template_sha256()
    expected_artifact_digest = artifact_sha256(item)
    if item.get("prompt_sha256") != expected_prompt_digest:
        return False, ["artifact: prompt digest does not match its exact bytes"]
    if item.get("template_sha256") != expected_template_digest:
        return False, ["artifact: prompt template or schema digest is stale"]
    if item.get("artifact_sha256") != expected_artifact_digest:
        return False, ["artifact: canonical content digest does not match"]
    declared = item.get("dispatchable")
    stored = item.get("dispatch_blockers")
    if not isinstance(declared, bool) or not isinstance(stored, list) or any(
            not isinstance(value, str) or not value for value in stored):
        return False, ["artifact: eligibility metadata is missing or malformed"]

    reference = item.get("evidence_snapshot")
    digests, snapshot_blockers = _snapshot_digests(
        reference if isinstance(reference, str) else "")
    cited = item.get("human_turns")
    if not isinstance(cited, list) or any(not isinstance(turn, dict) for turn in cited):
        human_turns: list[dict] = []
        snapshot_blockers = [*snapshot_blockers,
                             "human_turns: artifact records are malformed"]
    else:
        pseudo = {"human_turns": [str(turn.get("cite") or "") for turn in cited]}
        human_turns = _human_turns(pseudo, digests)
        if human_turns != cited:
            _add(snapshot_blockers,
                 "human_turns: artifact quotes do not match the evidence snapshot")

    computed = _finding_blockers(item, human_turns, snapshot_blockers)
    try:
        expected_prompt = _prompt_text(item, human_turns)
    except (KeyError, TypeError, ValueError):
        _add(computed, "artifact: finding fields cannot produce a dispatch prompt")
    else:
        if item.get("prompt") != expected_prompt:
            _add(computed, "artifact: prompt does not match validated finding fields")
    blockers: list[str] = []
    for blocker in stored + computed:
        _add(blockers, blocker)
    if not declared and not blockers:
        _add(blockers, "artifact: marked non-dispatchable")
    return declared and not blockers, blockers


def _prompt_text(finding: dict, human_turns: list[dict]) -> str:
    """The complete dispatch prompt, minus any approval comment.

    Byte-for-byte the shape `board.build_finding_prompt()` used to produce, so
    moving prompt construction to ranking time changed where the prompt is
    built and not what a worker receives.
    """
    lines = [f"## Finding: {finding['title']}", ""]
    lines.append(f"sessions: {'; '.join(finding['sessions']) or 'none'}")
    lines.append(f"severity: {finding['severity']}")
    lines.append(f"fix_files: {'; '.join(finding['fix_files']) or 'none'}")
    lines.append(f"fix_lines: {finding['fix_lines']}")
    lines.append("")
    lines.append("Human turns cited as evidence for this finding:")
    for turn in human_turns:
        quote = turn["quote"]
        lines.append(
            f'  {turn["cite"]}: "{quote}"'
            if quote
            else f"  {turn['cite']}: (text not in digest window)"
        )
    lines.append("")
    lines.append(finding["body"].strip())
    return DISPATCH_HEADER + "\n".join(lines) + "\n"


def build(findings_dir: Path | None = None, queue_dir: Path | None = None) -> list[dict]:
    """(Re)write every dispatch artifact. Returns the items, in index order.

    Each findings report is resolved against its own named immutable snapshot.
    There is deliberately no live-session fallback, including for legacy
    reports: those reports remain readable but their artifacts fail closed.
    """
    findings_dir = findings_dir or RETRO_DIR
    queue_dir = queue_dir or QUEUE_DIR

    sources: list[tuple[Path, bytes, dict, dict[str, dict], list[str]]] = []
    for path in sorted(Path(findings_dir).glob("*-findings.md")):
        try:
            source_bytes = path.read_bytes()
            text = source_bytes.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        except (OSError, UnicodeDecodeError):
            continue
        _header, findings = retro_rank.parse_findings(text)
        reference = findings[0].get("session_snapshot", "") if findings else ""
        digests, snapshot_blockers = _snapshot_digests(reference)
        for f in findings:
            sources.append((path, source_bytes, f, digests, snapshot_blockers))

    generated_at = _now_iso()
    items: list[dict] = []
    seen: set[str] = set()
    for path, source_bytes, finding, digests, snapshot_blockers in sources:
        slug = slug_for(finding["title"])
        if not slug or slug in seen:
            # First occurrence wins, matching the order
            # board.pending_finding_titles() already establishes.
            continue
        seen.add(slug)
        human_turns = _human_turns(finding, digests)
        blockers = _finding_blockers(finding, human_turns, snapshot_blockers)
        item = {
            "schema": SCHEMA,
            "slug": slug,
            "title": finding["title"],
            "normalised_title": retro_rank.normalise_title(finding["title"]),
            "severity": finding["severity"],
            "sessions": list(finding["sessions"]),
            "fix_files": list(finding["fix_files"]),
            "fix_lines": finding["fix_lines"],
            "human_turns": human_turns,
            "body": finding["body"].strip(),
            "prompt": _prompt_text(finding, human_turns),
            "evidence_snapshot": finding.get("session_snapshot", ""),
            "dispatchable": not blockers,
            "dispatch_blockers": blockers,
            "source_file": path.resolve().relative_to(ROOT).as_posix()
            if _under_root(path)
            else path.name,
            "source_sha256": _sha256(source_bytes),
            "generated_at": generated_at,
        }
        item["prompt_sha256"] = _sha256(item["prompt"].encode("utf-8"))
        item["template_sha256"] = template_sha256()
        item["artifact_sha256"] = artifact_sha256(item)
        items.append(item)

    queue_dir.mkdir(parents=True, exist_ok=True)
    keep = {f"{item['slug']}.json" for item in items} | {"index.json"}
    for stale in queue_dir.glob("*.json"):
        # A finding deleted from the markdown must not stay dispatchable.
        if stale.name not in keep:
            stale.unlink()
    for item in items:
        _write_json(queue_dir / f"{item['slug']}.json", item)
    _write_json(queue_dir / "index.json", {
        "schema": SCHEMA,
        "generated_at": generated_at,
        "items": [item["slug"] for item in items],
    })
    return items


def _under_root(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT)
    except ValueError:
        return False
    return True


def _write_json(path: Path, payload: dict) -> None:
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{datetime.now(timezone.utc).timestamp():.6f}.tmp"
    )
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _is_reparse(info: os.stat_result) -> bool:
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
    return bool(getattr(info, "st_file_attributes", 0) & marker)


def _regular_file_bytes(path: Path, *, maximum: int = 16 * 1024 * 1024) -> bytes | None:
    """Read one unchanged regular non-link file without following its final name."""
    try:
        before = path.lstat()
        if (not stat.S_ISREG(before.st_mode) or _is_reparse(before)
                or before.st_size < 0 or before.st_size > maximum):
            return None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        after = os.fstat(descriptor)
        if (not stat.S_ISREG(after.st_mode) or _is_reparse(after)
                or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
                or after.st_size > maximum):
            return None
        with os.fdopen(descriptor, "rb", closefd=True) as source:
            descriptor = -1
            content = source.read(maximum + 1)
        return content if len(content) <= maximum else None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _direct_regular_child_bytes(directory: Path, path: Path) -> bytes | None:
    """Read only a direct regular child of a real queue directory."""
    try:
        directory_info = directory.lstat()
        if not stat.S_ISDIR(directory_info.st_mode) or _is_reparse(directory_info):
            return None
        canonical_directory = directory.resolve(strict=True)
        if path.parent.resolve(strict=True) != canonical_directory:
            return None
    except (OSError, RuntimeError, ValueError):
        return None
    return _regular_file_bytes(path, maximum=4 * 1024 * 1024)


def _bounded_findings_source(value: object) -> Path | None:
    """Resolve one direct regular ``docs/retro/*-findings.md`` source."""
    if (not isinstance(value, str) or not value or value != value.strip()
            or len(value) > 512 or "\\" in value
            or any(ord(character) < 32 for character in value)):
        return None
    relative = PurePosixPath(value)
    if (relative.is_absolute() or any(part in ("", ".", "..") for part in relative.parts)
            or not relative.name.endswith("-findings.md") or ":" in relative.parts[0]):
        return None
    path = ROOT.joinpath(*relative.parts)
    retro_root = ROOT / "docs" / "retro"
    try:
        if path.parent.resolve(strict=True) != retro_root.resolve(strict=True):
            return None
        info = path.lstat()
    except (OSError, RuntimeError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
        return None
    return path


def load_index(queue_dir: Path | None = None) -> dict:
    """The index as written, or an empty one. Pure filesystem read."""
    directory = queue_dir or QUEUE_DIR
    path = directory / "index.json"
    try:
        content = _direct_regular_child_bytes(directory, path)
        data = json.loads(content.decode("utf-8")) if content is not None else None
    except (UnicodeError, ValueError):
        return {"schema": SCHEMA, "generated_at": "", "items": []}
    if not isinstance(data, dict):
        return {"schema": SCHEMA, "generated_at": "", "items": []}
    if data.get("schema") != SCHEMA:
        return {"schema": SCHEMA, "generated_at": "", "items": []}
    items = data.get("items")
    data["items"] = (
        [slug for slug in items if canonical_slug(slug) is not None]
        if isinstance(items, list) else []
    )
    return data


def load_item(slug: str, queue_dir: Path | None = None) -> dict | None:
    """One artifact by slug, or None. Pure filesystem read -- no session logs."""
    canonical = canonical_slug(slug)
    if canonical is None:
        return None
    directory = queue_dir or QUEUE_DIR
    path = directory / f"{canonical}.json"
    try:
        content = _direct_regular_child_bytes(directory, path)
        data = json.loads(content.decode("utf-8")) if content is not None else None
    except (UnicodeError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("slug") != canonical:
        return None
    if _bounded_findings_source(data.get("source_file")) is None:
        return None
    return data


def load_by_title(title: str, queue_dir: Path | None = None) -> dict | None:
    """Artifact for a finding title, joined the way accepted.json joins."""
    return load_item(slug_for(title), queue_dir)


def is_stale(item: dict) -> bool:
    """True if the findings file has changed since this artifact was written.

    A stale artifact is reported as stale and never silently rebuilt from raw
    logs at dispatch time: the worker must receive the prompt the human
    reviewed, or nothing.
    """
    path = _bounded_findings_source(item.get("source_file"))
    if path is None:
        return True
    try:
        content = _regular_file_bytes(path)
        return content is None or _sha256(content) != item.get("source_sha256")
    except (OSError, ValueError):
        return True


def render_prompt(item: dict, comment: str = "") -> str:
    """The exact bytes a kit-builder receives: artifact prompt + approval comment.

    The one implementation of this join. Both the display path and the spawn
    path call it, so what the human sees on the board is byte-identical to what
    is dispatched.
    """
    base = str(item.get("prompt", ""))
    text = (comment or "").strip()
    if not text:
        return base
    return base.rstrip("\n") + "\n\n" + COMMENT_HEADING + "\n\n" + COMMENT_PREAMBLE + "\n\n" + text + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="Build or inspect dispatch prompt artifacts.")
    ap.add_argument("--list", action="store_true", help="show artifacts on disk, do not rebuild")
    a = ap.parse_args()

    if a.list:
        index = load_index()
        print(f"{INDEX_FILE.relative_to(ROOT).as_posix()}: "
              f"{len(index['items'])} item(s), generated {index.get('generated_at') or 'never'}")
        for slug in index["items"]:
            item = load_item(slug)
            if item is None:
                print(f"  {slug}: MISSING")
                continue
            print(f"  {slug}: {'STALE' if is_stale(item) else 'ok'}  {item['title']}")
        return 0

    items = build()
    print(f"{QUEUE_DIR.relative_to(ROOT).as_posix()}: {len(items)} artifact(s)")
    for item in items:
        print(f"  {item['slug']}  ({len(item['prompt'])} chars)  {item['title']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
