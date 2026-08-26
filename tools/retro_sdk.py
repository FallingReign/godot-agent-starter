#!/usr/bin/env python3
"""Copilot SDK adapter for the retrospective.

Optional. `retro.py --print` works with no dependency and is the fallback.

This adapter exists for one reason: thread resumption. The SDK persists a
thread, so a retro started on slice 3 can be reopened later and probed
further, warm, instead of starting cold. The CLI's -p mode is single-shot.

Requires an explicitly configured analyzer provider. Never installs anything.

The thread id and bridge scratch files live under the configured private
runtime root. A retro for a new slice starts a fresh thread; the same slice
resumes. Only validated `*-findings.md` reports are rankable; malformed
analyzer output is preserved separately as `*-unvalidated.md` for diagnosis.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import providers  # noqa: E402
import runtime_paths  # noqa: E402

# Written by run_sdk. The bridge script is generated rather than shipped so it
# can never drift from the python side that calls it.
BRIDGE = """
import fs from "node:fs";
import { pathToFileURL } from "node:url";

const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const sdk = await import(pathToFileURL(cfg.sdkEntry).href);
const { CopilotClient } = sdk;
const client = new CopilotClient({ cwd: cfg.cwd });

let thread;
let resumed = false;
if (cfg.threadId) {
  try {
    thread = await client.resumeThread(cfg.threadId);
    resumed = true;
  } catch (e) {
    thread = await client.createThread();
  }
} else {
  thread = await client.createThread();
}

// Read-only. The retro reports; a human promotes. It must not edit the repo.
const opts = {
  allowTool: ["read", "shell(git log*)", "shell(git show*)", "shell(git diff*)"],
  denyTool: ["write", "edit", "shell"],
};
if (cfg.model) opts.model = cfg.model;

const res = await thread.sendMessage(cfg.prompt, opts);
const text = typeof res === "string" ? res : (res?.text ?? res?.content ?? JSON.stringify(res));
fs.writeFileSync(cfg.out, JSON.stringify({
  threadId: thread.id ?? cfg.threadId ?? null,
  resumed,
  text,
}, null, 2));
"""


def load_thread(thread_file: Path, slice_name: str) -> str | None:
    """Resume only within the same slice. A new slice deserves a clean thread."""
    if not thread_file.exists():
        return None
    try:
        d = json.loads(thread_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if d.get("slice") != slice_name:
        return None
    return d.get("thread_id") or None


def save_thread(thread_file: Path, slice_name: str, thread_id: str) -> None:
    value = {
        "schema": 1,
        "slice": slice_name,
        "thread_id": thread_id,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _atomic_private_write(thread_file, json.dumps(value, indent=2) + "\n")


def _atomic_private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as output:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def parse_findings(text: str) -> dict | None:
    """The prompt demands JSON. Accept it fenced or bare, else give up cleanly."""
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidate = m.group(1) if m else None
    if candidate is None:
        i, j = text.find("{"), text.rfind("}")
        candidate = text[i : j + 1] if i != -1 and j > i else None
    if not candidate:
        return None
    try:
        got = json.loads(candidate)
        return got if isinstance(got, dict) else None
    except Exception:
        return None


def validate_findings(value: dict | None, pack: dict) -> list[str]:
    """Fail closed when analyzer output cannot support a dispatch artifact."""
    if not isinstance(value, dict):
        return ["response root is not an object"]
    findings = value.get("findings")
    if not isinstance(findings, list):
        return ["findings must be a list"]
    valid_turns: set[str] = set()
    valid_sessions: set[str] = set()
    for session in pack.get("sessions") or []:
        tag = str(session.get("tag") or "")
        if tag:
            valid_sessions.add(tag)
        for message in session.get("human_messages") or []:
            citation = str(message.get("citation") or "")
            if citation:
                valid_turns.add(citation)
    errors: list[str] = []
    if len(findings) > 50:
        errors.append("findings exceeds the 50 item limit")
    for index, finding in enumerate(findings, 1):
        prefix = f"finding {index}"
        if not isinstance(finding, dict):
            errors.append(f"{prefix} is not an object")
            continue
        for field in ("title", "problem", "proposal", "measure"):
            if not str(finding.get(field) or "").strip():
                errors.append(f"{prefix} requires {field}")
        turns = finding.get("human_turns")
        sessions = finding.get("sessions")
        if not isinstance(turns, list) or not turns:
            errors.append(f"{prefix} requires at least one human_turn citation")
            turns = []
        if not isinstance(sessions, list) or not sessions:
            errors.append(f"{prefix} requires at least one session")
            sessions = []
        for citation in turns:
            if str(citation) not in valid_turns:
                errors.append(f"{prefix} cites unknown human turn {citation!r}")
        cited_sessions = {str(citation).split(":", 1)[0] for citation in turns}
        named_sessions = {str(session) for session in sessions}
        if named_sessions - valid_sessions:
            errors.append(f"{prefix} names unknown session(s): "
                          f"{sorted(named_sessions - valid_sessions)}")
        if named_sessions != cited_sessions:
            errors.append(f"{prefix} sessions must exactly match its human_turns")
        if not isinstance(finding.get("mechanical", []), list):
            errors.append(f"{prefix} mechanical must be a list")
        if not isinstance(finding.get("fix_files", []), list):
            errors.append(f"{prefix} fix_files must be a list")
    if not isinstance(value.get("observations", []), list):
        errors.append("observations must be a list")
    return errors


def _field_list(values) -> str:
    clean = [" ".join(str(value).replace(";", ",").split())
             for value in (values or []) if str(value).strip()]
    return "[" + "; ".join(clean) + "]"


def _safe_fix_files(values) -> list[str]:
    files: list[str] = []
    for value in values or []:
        path = str(value or "").strip().replace("\\", "/")
        parts = Path(path).parts
        if (not path or Path(path).is_absolute() or ".." in parts
                or path == "src" or path.startswith("src/")):
            continue
        files.append(path)
    return sorted(set(files))


def _canonical_report_text(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.rstrip("\n") + "\n"


def _atomic_write_report(path: Path, text: str) -> Path:
    """Durably replace one complete report and verify the published bytes."""
    payload = _canonical_report_text(text).encode("utf-8")
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    )
    replace_attempted = False
    replaced = False
    try:
        with temporary.open("xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        if temporary.read_bytes() != payload:
            raise OSError(f"temporary report verification failed: {temporary.name}")
        replace_attempted = True
        os.replace(temporary, path)
        replaced = True
        if path.read_bytes() != payload:
            raise OSError(f"published report verification failed: {path.name}")
    except Exception:
        # An atomic replace either leaves the complete old target or the complete
        # new target. If the new target may have landed but cannot be verified,
        # remove it so a failed write can never enter the findings queue.
        if replaced or (replace_attempted and not temporary.exists()):
            path.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return path


def write_rankable_report(retro_dir: Path, slice_name: str,
                          findings: dict | None, raw: str, resumed: bool,
                          pack_path: Path, root: Path) -> Path:
    """Render validated analyzer JSON into the sole rankable findings schema."""
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe = re.sub(r"[^a-z0-9.-]+", "-", (slice_name or "unknown").lower()).strip("-")
    suffix = pack_path.stem[:10]
    if findings is None:
        out = retro_dir / f"{stamp}-{safe}-{suffix}-unvalidated.md"
        lines = [
            f"# Unvalidated retrospective response: {slice_name or 'unknown'}", "",
            f"evidence_pack: {pack_path.relative_to(root).as_posix()}", "",
            "The analyzer did not return the required JSON. This file is not a",
            "findings file and cannot enter the approval queue.", "", "```text",
            raw.strip()[:8000], "```", "",
        ]
        return _atomic_write_report(out, "\n".join(lines))

    out = retro_dir / f"{stamp}-{safe}-{suffix}-findings.md"
    try:
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pack = {}
    session_snapshot = ((pack.get("session_snapshot") or {}).get("path") or "")
    lines = [
        f"# Retrospective {stamp}", "", f"slice: {slice_name or 'unknown'}",
        f"evidence_pack: {pack_path.relative_to(root).as_posix()}",
        f"session_snapshot: {session_snapshot}",
        f"analyzer_context: {'resumed' if resumed else 'new'}", "",
        "Findings are an analyzer's reading of immutable evidence. Nothing is",
        "implemented until a human approves its exact dispatch proposal.", "",
    ]
    if findings.get("summary"):
        lines += ["## Summary", "", str(findings["summary"]).strip(), ""]
    severities = {"work-destroyed", "false-green", "wrong-built", "none"}
    for finding in findings.get("findings") or []:
        if not isinstance(finding, dict):
            continue
        title = " ".join(str(finding.get("title") or "untitled").split())
        severity = str(finding.get("severity") or "none")
        if severity not in severities:
            severity = "none"
        try:
            fix_lines = min(max(int(finding.get("fix_lines") or 1), 1), 10000)
        except (TypeError, ValueError):
            fix_lines = 1
        sections: list[str] = []
        for label, key in (("Problem", "problem"), ("Proposal", "proposal"),
                           ("Measure", "measure")):
            value = str(finding.get(key) or "").strip()
            if value:
                sections += [f"**{label}** — {value}", ""]
        lines += [
            f"## Finding: {title}",
            f"sessions: {_field_list(finding.get('sessions'))}",
            f"human_turns: {_field_list(finding.get('human_turns'))}",
            f"mechanical: {_field_list(finding.get('mechanical'))}",
            f"recurs: {'true' if finding.get('recurs') else 'false'}",
            f"severity: {severity}",
            f"fix_files: {_field_list(_safe_fix_files(finding.get('fix_files')))}",
            f"fix_lines: {fix_lines}", "", *sections,
        ]
    observations = [str(value).strip() for value in findings.get("observations") or []
                    if str(value).strip()]
    if observations:
        lines += ["## Observations", ""]
        lines += [f"- {observation}" for observation in observations]
        lines.append("")
    return _atomic_write_report(out, "\n".join(lines))


def run_sdk(
    pack_path: Path,
    prompt_template: str,
    retro_dir: Path,
    *,
    root: Path | None = None,
    work_dir: Path | None = None,
    thread_file: Path | None = None,
    provider: providers.ProviderSpec | None = None,
) -> int:
    """Run one configured analyzer against an immutable pack.

    ``retro_dir`` contains only durable reports. Bridge input/output and the
    resumable thread record are always private runtime artifacts.
    """
    root = (root or retro_dir.parent.parent).resolve()
    try:
        spec = provider or providers.selection(root, "analyzer")
    except (providers.ProviderConfigError, runtime_paths.RuntimeConfigError) as exc:
        print(f"cannot use the SDK adapter: {exc}")
        return 2
    problems = providers.preflight(spec)
    if problems:
        print("cannot use the SDK adapter:")
        for p in problems:
            print(f"  - {p}")
        print("\nthe evidence pack is written; run with --print to use any agent")
        return 2

    try:
        pack = json.loads(pack_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"cannot read evidence pack: {exc}")
        return 2
    slice_name = (
        ((pack.get("artefacts") or {}).get("proposal") or {}).get("slice") or "unknown"
    )

    sdk_entry = providers.copilot_sdk_path()
    if spec.kind != "copilot-sdk" or sdk_entry is None:
        print("cannot use the SDK adapter: configured analyzer is not a ready copilot-sdk provider")
        return 2
    if work_dir is None or thread_file is None:
        runtime = runtime_paths.resolve(root)
        work_dir = work_dir or runtime.retro_sdk
        thread_file = thread_file or runtime.retro_thread
    thread_id = load_thread(thread_file, slice_name)
    prompt = prompt_template.format(pack=pack_path.relative_to(root).as_posix())
    if thread_id:
        prompt = (
            "This continues an earlier retro on the same slice. The evidence pack "
            "has been regenerated; re-read it. Report only what is new or what you "
            "now read differently.\n\n" + prompt
        )

    work_dir.mkdir(parents=True, exist_ok=True)
    bridge = work_dir / "bridge.mjs"
    _atomic_private_write(bridge, BRIDGE)
    cfg_path = work_dir / "cfg.json"
    out_path = work_dir / "out.json"
    out_path.unlink(missing_ok=True)
    _atomic_private_write(
        cfg_path,
        json.dumps({
            "cwd": str(root),
            "threadId": thread_id,
            "prompt": prompt,
            "out": str(out_path),
            "model": spec.model,
            "sdkEntry": str(sdk_entry),
        }) + "\n",
    )

    print(f"slice           {slice_name}")
    print(f"thread          {'resuming ' + thread_id[:8] if thread_id else 'new'}")
    print("model call      one prompt, counts toward your copilot allowance")
    print("tools           read + git log/show/diff only, no writes")
    print()

    try:
        r = subprocess.run(
            ["node", str(bridge), str(cfg_path)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired:
        print("timed out after 900s; the pack is written, try --print")
        return 1

    if r.returncode != 0 or not out_path.exists():
        print("SDK bridge failed:")
        print((r.stderr or r.stdout or "")[-1500:])
        print("\nthe evidence pack is written; run with --print to use any agent")
        return 1

    try:
        got = json.loads(out_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"unreadable bridge output: {exc}")
        return 1

    text = got.get("text") or ""
    findings = parse_findings(text)
    validation_errors = validate_findings(findings, pack)
    if validation_errors:
        findings = None
    if got.get("threadId"):
        save_thread(thread_file, slice_name, got["threadId"])

    try:
        report = write_rankable_report(
            retro_dir, slice_name, findings, text, bool(got.get("resumed")),
            pack_path, root,
        )
    except OSError as exc:
        print(f"report write failed: {exc}")
        return 1
    print(f"report          {report.relative_to(root)}")
    if findings is None:
        print("note            response was not valid findings JSON, written unvalidated")
        for error in validation_errors[:10]:
            print(f"                  - {error}")
    else:
        n = len(findings.get("findings") or [])
        print(f"findings        {n} finding(s)")
    return 1 if findings is None else 0
