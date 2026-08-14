#!/usr/bin/env python3
"""Copilot SDK adapter for the retrospective.

Optional. `retro.py --print` works with no dependency and is the fallback.

This adapter exists for one reason: thread resumption. The SDK persists a
thread, so a retro started on slice 3 can be reopened later and probed
further, warm, instead of starting cold. The CLI's -p mode is single-shot.

Requires node >= 22 and @github/copilot on PATH. Never installs anything.

The thread id lives in docs/retro/thread.json. A retro for a new slice starts
a fresh thread; the same slice resumes. The durable findings live in
docs/retro/*.md -- the thread is warm working context, the repo is memory.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

NODE_MIN = 22
# Written by run_sdk. The bridge script is generated rather than shipped so it
# can never drift from the python side that calls it.
BRIDGE = """
import { CopilotClient } from "@github/copilot";
import fs from "node:fs";

const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
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


def node_version() -> int | None:
    exe = shutil.which("node")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15).stdout
        m = re.search(r"v(\d+)", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def preflight() -> list[str]:
    """Everything that must be true before spending any quota."""
    problems: list[str] = []
    v = node_version()
    if v is None:
        problems.append("node not found on PATH; install node >= 22 or use --print")
    elif v < NODE_MIN:
        problems.append(f"node {v} found, need >= {NODE_MIN}; or use --print")
    if not shutil.which("copilot"):
        problems.append(
            "copilot CLI not on PATH; npm i -g @github/copilot, or use --print"
        )
    return problems


def load_thread(retro_dir: Path, slice_name: str) -> str | None:
    """Resume only within the same slice. A new slice deserves a clean thread."""
    f = retro_dir / "thread.json"
    if not f.exists():
        return None
    try:
        d = json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None
    if d.get("slice") != slice_name:
        return None
    return d.get("thread_id") or None


def save_thread(retro_dir: Path, slice_name: str, thread_id: str) -> None:
    (retro_dir / "thread.json").write_text(
        json.dumps(
            {
                "slice": slice_name,
                "thread_id": thread_id,
                "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        ),
        encoding="utf-8",
    )


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


def write_report(retro_dir: Path, slice_name: str, findings: dict, raw: str, resumed: bool) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    safe = re.sub(r"[^a-z0-9.-]+", "-", (slice_name or "unknown").lower()).strip("-")
    out = retro_dir / f"{stamp}-{safe}.md"

    lines = [
        f"# Retro: {slice_name or 'unknown'}",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')}"
        f"{' (resumed thread)' if resumed else ''}.",
        "",
        "Findings are a model's reading of the evidence pack. Nothing here is",
        "applied automatically. Promote what you agree with; delete the rest.",
        "",
    ]
    if findings:
        if findings.get("summary"):
            lines += ["## Summary", "", findings["summary"], ""]
        fs = findings.get("findings") or []
        if fs:
            lines += ["## Findings", ""]
            for f in fs:
                lines += [
                    f"### {f.get('title','(untitled)')}",
                    "",
                    f"_severity {f.get('severity','?')} \u00b7 confidence {f.get('confidence','?')}_",
                    "",
                    f"**Evidence.** {f.get('evidence','')}",
                    "",
                    f"**Kit gap.** {f.get('kit_gap','')}",
                    "",
                ]
        goals = findings.get("proposed_goals") or []
        if goals:
            lines += ["## Proposed goals", "", "| Goal | Because | Checkable |", "| --- | --- | --- |"]
            for g in goals:
                lines.append(
                    f"| {g.get('goal','')} | {g.get('because','')} | "
                    f"{'yes' if g.get('checkable') else 'no'} |"
                )
            lines.append("")
        if findings.get("friction_not_visible"):
            lines += [
                "## Suspected but not evidenced",
                "",
                findings["friction_not_visible"],
                "",
                "This is the part only you can confirm. The pack sees committed",
                "history and logged turns, never the attempts inside one turn.",
                "",
            ]
    else:
        lines += [
            "## Unvalidated output",
            "",
            "The model did not return parseable JSON. Raw response below.",
            "",
            "```",
            raw.strip()[:8000],
            "```",
            "",
        ]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out


def run_sdk(pack_path: Path, prompt_template: str, retro_dir: Path) -> int:
    problems = preflight()
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

    root = pack_path.parent.parent.parent
    thread_id = load_thread(retro_dir, slice_name)
    prompt = prompt_template.format(pack=pack_path.relative_to(root))
    if thread_id:
        prompt = (
            "This continues an earlier retro on the same slice. The evidence pack "
            "has been regenerated; re-read it. Report only what is new or what you "
            "now read differently.\n\n" + prompt
        )

    work = retro_dir / ".sdk"
    work.mkdir(exist_ok=True)
    bridge = work / "bridge.mjs"
    bridge.write_text(BRIDGE, encoding="utf-8")
    cfg_path = work / "cfg.json"
    out_path = work / "out.json"
    cfg_path.write_text(
        json.dumps(
            {
                "cwd": str(root),
                "threadId": thread_id,
                "prompt": prompt,
                "out": str(out_path),
                "model": os.environ.get("RETRO_MODEL", ""),
            }
        ),
        encoding="utf-8",
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
    if got.get("threadId"):
        save_thread(retro_dir, slice_name, got["threadId"])

    report = write_report(retro_dir, slice_name, findings or {}, text, bool(got.get("resumed")))
    print(f"report          {report.relative_to(root)}")
    if not findings:
        print("note            response was not valid JSON, written unvalidated")
    else:
        n = len(findings.get("findings") or [])
        g = len(findings.get("proposed_goals") or [])
        print(f"findings        {n} finding(s), {g} proposed goal(s)")
    return 0
