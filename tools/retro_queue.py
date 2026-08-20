#!/usr/bin/env python3
"""Durable dispatch-prompt artifacts, built once at ranking time.

The retrospective pipeline already pays for an expensive evidence pass:
`session_digest` opens every cited `events.jsonl` and reduces it to counts and
verbatim human messages. Dispatch used to pay for that pass *again* -- once per
finding -- because `board.build_finding_prompt()` called
`retro_rank.citation_report()` per finding, so a single large session was
reopened and reparsed as many times as it was cited. That made
`/api/dispatch/prepare` take close to a minute, and it meant the worker's prompt
was rebuilt from session logs that may have changed since the human reviewed it.

This module makes the prompt an artifact instead:

    retro_rank.py (ranking) -> docs/retro/queue/<slug>.json + index.json
    retro.html / board.py   -> read the artifact, never a session log

`build()` resolves citations for *every* finding in one `citation_report()`
call, so each cited session is digested exactly once no matter how many findings
cite it. Everything downstream is a filesystem read.

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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RETRO_DIR = ROOT / "docs" / "retro"
QUEUE_DIR = RETRO_DIR / "queue"
INDEX_FILE = QUEUE_DIR / "index.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import retro_rank  # noqa: E402  (needs sys.path set first)

SCHEMA = 1

# The restrictions every dispatched kit-builder must carry. They live here, not
# in board.py, because the prompt is now an artifact written at ranking time --
# if the header were applied at dispatch time the stored `prompt` would not be
# the prompt actually sent, which is exactly the property this file exists to
# guarantee. `board.DISPATCH_HEADER` re-exports this name.
DISPATCH_HEADER = (
    "You are the kit-builder, dispatched from the retrospective board to "
    "implement the finding below. Follow .github/agents/kit-builder.agent.md: "
    "name the finding you are implementing and quote the occurrences it cites "
    "before changing anything, do not touch src/, and finish with a green "
    "`python check.py`. If you changed a hash-checked gate file, re-baseline "
    "with `python bootstrap.py --fix` and say so; `--accept-gate-changes` is "
    "human-only and you must never run it.\n\n"
)

# Where render_prompt() appends the human's approval comment. A fixed, obvious
# delimiter so the worker can tell the reviewed proposal from the amendment to
# it, and so the appended block is greppable in docs/retro/runs/*.prompt.md.
COMMENT_HEADING = "## Amendment from the human who approved this finding"
COMMENT_PREAMBLE = (
    "The finding above was approved with the following comment. It is part of "
    "the instruction: build the proposal as amended here, and where the two "
    "disagree, this comment wins."
)

_SLUG_RE = re.compile(r"[^a-z0-9-]+")


def slug_for(title: str) -> str:
    """Filename stem for a finding title.

    `normalise_title` is the join key against accepted.json and must not
    change; the slug is that key reduced to characters legal in a filename on
    every platform this runs on.
    """
    return _SLUG_RE.sub("", retro_rank.normalise_title(title)).strip("-")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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

    Every finding across every `*-findings.md` is parsed first and citations
    are resolved in a *single* `citation_report()` call, so a session cited by
    four findings is digested once rather than four times. This is the only
    function in the module that touches a session log.
    """
    findings_dir = findings_dir or RETRO_DIR
    queue_dir = queue_dir or (findings_dir / "queue")

    sources: list[tuple[Path, dict]] = []
    for path in sorted(Path(findings_dir).glob("*-findings.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        _header, findings = retro_rank.parse_findings(text)
        for f in findings:
            sources.append((path, f))

    digests, _warnings, _personas = retro_rank.citation_report([f for _p, f in sources])

    generated_at = _now_iso()
    items: list[dict] = []
    seen: set[str] = set()
    for path, finding in sources:
        slug = slug_for(finding["title"])
        if not slug or slug in seen:
            # First occurrence wins, matching the order
            # board.pending_finding_titles() already establishes.
            continue
        seen.add(slug)
        human_turns = _human_turns(finding, digests)
        try:
            source_bytes = path.read_bytes()
        except OSError:
            source_bytes = b""
        items.append({
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
            "source_file": path.resolve().relative_to(ROOT).as_posix()
            if _under_root(path)
            else path.name,
            "source_sha256": _sha256(source_bytes),
            "generated_at": generated_at,
        })

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
    path.write_text(text, encoding="utf-8", newline="\n")


def load_index(queue_dir: Path | None = None) -> dict:
    """The index as written, or an empty one. Pure filesystem read."""
    path = (queue_dir or QUEUE_DIR) / "index.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": SCHEMA, "generated_at": "", "items": []}
    if not isinstance(data, dict):
        return {"schema": SCHEMA, "generated_at": "", "items": []}
    data.setdefault("items", [])
    return data


def load_item(slug: str, queue_dir: Path | None = None) -> dict | None:
    """One artifact by slug, or None. Pure filesystem read -- no session logs."""
    path = (queue_dir or QUEUE_DIR) / f"{slug}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def load_by_title(title: str, queue_dir: Path | None = None) -> dict | None:
    """Artifact for a finding title, joined the way accepted.json joins."""
    return load_item(slug_for(title), queue_dir)


def is_stale(item: dict) -> bool:
    """True if the findings file has changed since this artifact was written.

    A stale artifact is reported as stale and never silently rebuilt from raw
    logs at dispatch time: the worker must receive the prompt the human
    reviewed, or nothing.
    """
    source = item.get("source_file") or ""
    if not source:
        return True
    path = ROOT / source
    try:
        return _sha256(path.read_bytes()) != item.get("source_sha256")
    except OSError:
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
