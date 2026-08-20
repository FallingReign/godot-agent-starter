# Dispatch pipeline — implementation plan

> **Status: implemented, 2026-08-19.** Decisions 1-4 and the HTTP contract below
> are live in `tools/retro_queue.py`, `tools/board.py`, `tools/retro_html.py`,
> `tools/board_client.py` and `tools/plan_html.py`. This file is kept as the
> record of *why* the pipeline is shaped this way; it is no longer the thing to
> build from. The durable description of the loop is `docs/retro/README.md`, and
> the authoritative endpoint list is the module docstring of `tools/board.py`.
> Verification: `VERIFY.md` B59-B65.

Companion to `dispatch-pipeline-review.md`. That file records the observed
defects; this one records the decisions taken with the human on 2026-08-19 and
the contract each worker builds against. It is an implementation brief, not a
retrospective finding.

## Decisions taken

| # | Question | Answer |
| --- | --- | --- |
| 1 | Who runs the retrospective model when the note threshold is reached? | The board surfaces a **Run retrospective** control in `plan.html`. One conscious click spends quota. `retro_due.py` stays inert and never calls a model. |
| 2 | Board lifetime | **On-demand start, then stays running.** First thing that needs it starts it; it does not exit with the page generator. |
| 3 | Approval model | Each finding is approved **with a comment** (decisions or tweaks to the proposed solution). `retro.html` shows two lists: findings **to action**, and **approved** items with their last known status. |
| 4 | Approve then run, or approve is run? | **Approve deploys immediately.** There is no second Run click. Writing the comment is the conscious decision; a separate button buys safety already paid for. |

### Consequences of decisions 3 and 4

- A comment is part of the dispatched prompt, so the solution the worker builds
  is the proposal *as amended by the human*.
- The comment is captured at approval time and stored with the decision.
- "Last known status" must be visible per approved item precisely so that an
  item stuck on `working` is recognisable as something to investigate in the
  chat, rather than looking like normal progress.
- Because approval dispatches, the **displayed prompt must be shown before the
  approve control is used**, not after. The human sees exactly what will be sent,
  amends it via the comment, and approving sends it.
- Approval is still one finding per fresh session, still sequential: a second
  approval while one is running is queued behind it rather than run concurrently.
  Two findings can name the same `fix_files`, and concurrent kit-builders editing
  the same gate file is a merge conflict waiting to happen.
- The two-click boundary in `dispatch-pipeline-review.md` finding 7 is therefore
  **deliberately superseded** by decision 4. Its underlying requirement — that
  quota is never spent without a conscious act — is preserved by the comment step.

## The single architectural change

`/api/dispatch/prepare` must never open a session log. Prompts are built **once**,
when findings are ranked, and persisted. Everything downstream reads the artifact.

```text
retro_rank.py (ranking)  ->  docs/retro/queue/<slug>.json   [prompt artifacts]
retro.html               ->  reads artifacts, displays prompt
board /api/dispatch/*    ->  reads artifacts, spawns worker
```

## Artifact contract

One file per finding: `docs/retro/queue/<slug>.json`.

`slug` is derived from `retro_rank.normalise_title(title)` reduced to
`[a-z0-9-]`. Keys:

| Key | Meaning |
| --- | --- |
| `schema` | integer, `1` |
| `slug` | filename stem, as above |
| `title` | finding title, verbatim |
| `normalised_title` | `retro_rank.normalise_title(title)` — the join key against `accepted.json` |
| `severity` | as parsed from the finding |
| `sessions` | list of cited session ids |
| `fix_files` | list of paths the finding proposes changing |
| `fix_lines` | as parsed |
| `human_turns` | list of `{"cite": "S1H4", "quote": "..."}`; `quote` is `""` when the digest window did not contain it |
| `body` | the finding's markdown body (kit gap, proposed change, instructions) |
| `prompt` | the complete dispatch prompt **without** the human's approval comment |
| `source_file` | repo-relative path of the findings markdown it came from |
| `source_sha256` | sha256 of that file's bytes, for staleness detection |
| `generated_at` | ISO-8601 UTC |

Plus `docs/retro/queue/index.json`:

```json
{"schema": 1, "generated_at": "...", "items": ["<slug>", "..."]}
```

`items` is ordered as the findings appear across `sorted(RETRO_DIR.glob("*-findings.md"))`,
which is the order `board.pending_finding_titles()` already establishes.

### Staleness

An artifact whose `source_sha256` no longer matches the current bytes of
`source_file` is **stale**. Stale artifacts are reported as such and are not
silently rebuilt from raw logs at dispatch time.

### Final prompt = artifact + comment

The prompt actually sent to a kit-builder is
`render_prompt(item, comment)`, which appends the human's approval comment to
`item["prompt"]` under a clearly delimited heading. There is exactly one
implementation of this function and both the display path and the spawn path
call it, so the displayed prompt is byte-identical to the dispatched prompt.

## HTTP contract (workers 2 and 3 build to this; it is authoritative)

Worker 2 implements the server side, worker 3 the browser side. Neither may
change the shape below without saying so.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | `{"ok": true, "port", "pid", "started", "schema": 1}`. Cheap, no side effects. The page uses it to tell "board alive" from "stale port". |
| `GET` | `/api/state` | The whole view model, in one request — see below. |
| `GET` | `/api/finding/<slug>` | Artifact plus `prompt_preview` = `retro_queue.render_prompt(item, "")`, so the human can read exactly what will be sent **before** approving. |
| `POST` | `/api/finding/approve` | `{"slug", "comment"}` → records the decision with the comment, then **dispatches immediately** (decision 4). Returns the updated finding entry. |
| `POST` | `/api/finding/defer` | `{"slug", "reason"}` → records a deferral. Never dispatches. |
| `POST` | `/api/retro/run` | Starts the retrospective model run (decision 1). Returns `{"run_id"}` or a structured error. This is the only other path that spends quota. |
| `GET` | `/api/runs/<run_id>` | Status, exit code, silence duration, tail of the log, and `resume_cmd`. |

`/api/state` returns:

```json
{
  "board":     {"port": 0, "pid": 0, "started": "...", "schema": 1},
  "retro_due": {"unarchived": 0, "threshold": 10, "due": false},
  "findings":  [{"slug": "...", "title": "...", "severity": "...",
                 "state": "awaiting_review", "comment": "", "stale": false,
                 "run_id": null, "status_detail": "", "updated_at": "..."}],
  "runs":      [{"run_id": "...", "finding": "...", "status": "running",
                 "silence_minutes": 0.0, "exit_code": null, "resume_cmd": "..."}]
}
```

`state` is one of: `awaiting_review`, `queued`, `working`, `done`, `failed`,
`deferred`. `stale` is orthogonal (the source findings file changed under it).

`status_detail` is what makes a wedged item legible — for a `working` item it
carries how long the worker has been silent, so an item that says `working` for
an hour is visibly *not* progressing and the human knows to open the chat with
`resume_cmd`.

### Endpoints being removed

`/api/dispatch/prepare` and `/api/dispatch/run` are superseded by
`/api/finding/approve`. Delete them rather than leaving them as dead paths a
stale page can still hit.

## Worker split

1. **Prompt artifacts** — `tools/retro_queue.py` + generation hook in
   `retro_rank.py`; `board.build_finding_prompt()` reads artifacts only.
2. **Server consolidation** — board becomes the on-demand-but-persistent entry
   point; health/reconnect; approve-with-comment; run-retrospective control;
   per-item status.
3. **Frontend** — two-list `retro.html`, explicit state machine, defensive
   fetch, `file://` read-only notice, plan.html banner.
4. **Tests + `VERIFY.md`.**

Worker 1 must land before 2 and 3.
