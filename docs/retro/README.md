# Retrospective

Two artefacts live here, produced by different agents at different cadences.

## `notes/`

Written by the **game builder** at the end of each slice. Testimony, not
assessment: what was proposed, what changed, what the human corrected, what the
agent got wrong, where it hit friction.

No model call. Cheap enough to be unconditional.

A note draws no conclusions and proposes no kit changes. The builder cannot see
across slices, so anything it concludes from one slice is a guess.

## `<date>-findings.md`

Written by the **retrospective agent** when notes accumulate to the threshold in
`retro.config.json`. It reads every unarchived note, every session digest, and
git history, and looks only for what a single slice cannot show:

- a correction the human made more than once
- a rule worked around rather than followed, more than once
- a question raised and never closed
- documentation, a skill or a comment contradicting the code it describes
- a task retried mechanically, which usually means missing tooling
- a standing preference stated in chat that never became a rule

Each finding carries occurrence citations, a proposed change, a confidence, and a
cost. One occurrence is a hypothesis and must be labelled as one.

Notes consumed by a retrospective move to `archive/`, so the same evidence never
produces the same finding twice.

## The permission split

The retrospective **proposes**. You **promote**. The kit builder **implements**.

The retrospective has no write access outside this directory. An agent that can
conclude a rule is wrong and then delete it will make the kit worse in ways
nobody notices, which is the same reason `.gate.sha256` exists.

## The loop

Promotion is not a CLI ritual. It is one page and one click per finding:

```text
notes accumulate
  -> the gate says a retro is warranted (it never calls a model itself)
  -> plan.html shows a "Run retrospective" banner        [one click spends quota]
  -> the retrospective agent writes <date>-findings.md
  -> retro_rank.py ranks it and writes docs/retro/queue/<slug>.json  [prompt artifacts]
  -> retro.html lists findings "to action", "approved" and "deferred"
  -> you expand "The exact prompt that will be sent" and read it
  -> you write a comment and press Approve                [one click spends quota]
  -> a kit-builder is dispatched with prompt + your comment, immediately
  -> retro.html shows its status until it is done
```

Two clicks in the whole loop spend quota, and each is preceded by the thing it
is a decision about: the banner says a retrospective is due, the approve control
sits under the prompt it will send.

**The board starts on demand and stays up.** `plan_html.py` and `retro_html.py`
call `board.ensure_running()` after writing their output; the board does not
exit with them. It is not a service registry — a recorded pid and port are
re-probed, and a dead one is replaced rather than trusted. Every tool keeps
working with the board absent: the pages stay readable under `file://` and say
so, with the controls deliberately disabled rather than clickable and inert.

**Approving is dispatching.** There is no second Run click. The comment is the
conscious act, and it is the amended proposal — it is appended to the stored
prompt, so the worker builds the proposal *as you amended it*. It is written to
`accepted.json` before the spawn, so a board killed mid-dispatch still records
what you approved.

**Approvals are sequential.** A second approval while a worker is running is
queued behind it. Two findings can name the same `fix_files`, and concurrent
kit-builders editing one gate file is a merge conflict waiting to happen. A
worker that exits non-zero halts the queue rather than continuing.

**A settled item must never read as an unsettled one.** `retro.html` is three
separate blocks, not one flow with headings: **To action**, **Approved — already
sent**, and **Deferred**, which is collapsed because it is settled rather than
work. A finding with a recorded decision is never emitted into the to-action
list, whatever the board reports about it. Within each block the order is fixed
and printed as a caption on the page:

| List | Order |
| --- | --- |
| To action | cost descending; findings that could not be ranked last |
| Approved | `working`/`stalled` first, then `queued`, then `done`, then `failed`; most recently updated first within each |
| Deferred | most recently deferred first |

Live work sorts to the top of **Approved** because that is the only part of it
that can still need you. The rule is `retro_html.order_rows` and a mirror of it
in the page's own script; both are pure functions of the row data, so the same
findings always render in the same order however the board or the filesystem
happened to enumerate them. Verification: `VERIFY.md` B67.

**A stuck worker must look stuck.** Each approved item carries its last known
status, and a `working` item that has produced no output for longer than
`board_silence_minutes` renders as *stalled*, with the `copilot --resume`
command to open the chat and look. `working` for an hour must never read as
normal progress.

### Prompt artifacts

`docs/retro/queue/<slug>.json` is the one durable source for dispatch prompts,
written by `retro_rank.py` when findings are ranked. Everything downstream —
the page, the preview, the spawn — reads that artifact and nothing else. No
dispatch path opens a raw `events.jsonl`.

An artifact records the sha256 of the findings file it came from. If that file
changes underneath it the artifact is **stale**, and approval is refused with
`409 {"code": "stale"}` rather than silently rebuilt: a prompt regenerated from
a file that moved is not the prompt you read.

The seven-endpoint HTTP contract is documented once, in the module docstring of
`tools/board.py`. It is not restated here or in `VERIFY.md`.

## Running it

```
python tools/retro_due.py                        is a retrospective due?
python tools/session_digest.py --list            which sessions exist for this repo
python tools/session_digest.py                   reduced evidence, ~200:1
copilot --agent retrospective -p "Run a retrospective over the unarchived notes."
```

The digest tool exists because a session log is 50 KB to 2 MB of mostly tool-call
noise. Never hand a raw `events.jsonl` to an agent.

The board equivalents, when you want them without a page:

```
python tools/board.py --ensure                   start it if needed, print the URL
python tools/retro_queue.py --list               are the prompt artifacts current?
python tools/board.py --migrate                  normalise a pre-comment accepted.json
```

## Files here

| File | What it is |
| --- | --- |
| `notes/`, `archive/` | slice notes, and the ones a retrospective has consumed |
| `<date>-findings.md` | what the retrospective proposed |
| `queue/<slug>.json` | the dispatch-ready prompt artifact for one finding |
| `accepted.json` | approvals, each with the comment it was dispatched with |
| `deferred.json` | deferrals, each with its reason |
| `runs/` | one prompt and one log per dispatched worker |
| `board.state.json` | what the board is doing *right now*; losing it loses nothing durable |
| `dispatch-pipeline-*.md` | closed implementation briefs, kept as history |
