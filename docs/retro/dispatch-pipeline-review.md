# Retrospective dispatch pipeline review

> **Status: closed, 2026-08-19.** Every criterion below is walked in *Outcome*
> at the foot of this file. Criterion 8 was deliberately superseded by decision
> 4 in `dispatch-pipeline-plan.md`. Kept as the record of the defects that
> produced the current design; the durable description of the loop is
> `docs/retro/README.md`.

This is a handoff for a builder model. It records the observed problems in the
retrospective HTML -> board -> kit-builder flow and the intended behavior.
Do not treat this file as a retrospective finding to rank or dispatch: it is
an implementation brief.

## Context

The retrospective pipeline already performs an expensive evidence pass:

1. `tools/retro.py` discovers session logs and builds
   `docs/retro/evidence.json`.
2. `tools/session_digest.py` reduces raw session logs to mechanical counts and
   verbatim human messages.
3. A retrospective model reads that evidence and produces a dated findings
   report under `docs/retro/`.
4. `tools/retro_rank.py` resolves finding citations and adds ranking metadata.
5. `tools/retro_html.py` renders the findings and evidence into `retro.html`.

Once the findings exist, dispatching a kit-builder should be a cheap operation:
load an approved finding and its already-resolved prompt, show it, then spawn
the worker. It should not repeat retrospective analysis.

## Finding 1: queue preparation re-reads raw session logs

### Observed behavior

Clicking `retro.html` -> `1. Generate queue` can take close to a minute or
longer. The prompt eventually appears only after the server has done substantial
work, and the delay grows with the size and number of cited session logs.

### Current code path

The browser calls:

```text
POST /api/dispatch/prepare
  -> board.build_dispatch_queue()
  -> board.build_finding_prompt()
  -> retro_rank.citation_report()
  -> session_digest.digest_one()
```

`citation_report()` discovers the cited sessions again and
`digest_one()` opens and parses the original `events.jsonl` files line by line.
The digest extracts the human messages eventually used in the prompt, but it
also scans all records to calculate turns, cost, command loops, rewrites, and
gate failures.

For multiple findings, `build_finding_prompt()` calls `citation_report()`
independently for each finding. The same large session can therefore be opened
and parsed repeatedly.

### Required change

Do not read or parse raw session logs from `/api/dispatch/prepare`.

The retrospective stage must persist the resolved, dispatch-ready prompt for
each finding, or persist an equivalent compact evidence artifact that contains
everything needed to build the prompt. Queue preparation must consume that
artifact rather than reconstructing citations from raw logs.

The worker must receive the evidence and proposal that were reviewed, not a
new prompt silently rebuilt from potentially changed session logs.

## Finding 2: condensed evidence is not reused by dispatch

### Observed behavior

The repository contains compact evidence in `evidence.json`, and the generated
HTML displays condensed evidence and human quotes. It is reasonable to expect
the dispatch step to reuse that material.

### Current behavior

The dispatch code does not load `docs/retro/evidence.json` or a stored prompt.
The HTML is a presentation layer, not a prompt store. After approval, the
server reopens the findings Markdown, resolves citation identifiers, and
reprocesses raw logs.

### Required change

Introduce one durable source for dispatch prompts. The exact format can be
chosen by the builder, but it must:

- be generated when the retrospective findings are produced or ranked;
- contain the finding title, severity, cited evidence, kit gap, proposed change,
  and the kit-builder instructions;
- be readable by `retro.html` and `board.py`;
- remain stable after human approval;
- be regenerated only when the retrospective evidence/findings are regenerated;
- avoid storing secrets or unnecessary raw session-log content.

A separate file per finding, or a generated queue artifact keyed by normalized
finding title, would both satisfy this.

## Finding 3: queue preparation is synchronous and gives no progress

### Observed behavior

The browser waits with no visible progress while `/api/dispatch/prepare` does
the expensive work. This makes a slow request look broken.

### Current code path

The JavaScript disables the button, performs a `fetch()`, and only updates the
page after the complete JSON response arrives. The HTTP handler performs all
prompt construction before returning that response.

### Required change

After prompt artifacts are persisted, queue preparation should be a fast
filesystem read and return immediately.

If any preparation work remains expensive, the UI must show explicit progress
and the server must expose a preparation status rather than leaving the user
with a disabled button and no explanation. The preferred fix is to remove the
expensive work from this request rather than adding a progress animation around
it.

## Finding 4: failed fetches permanently disable the Generate button

### Observed behavior

The button worked once, then appeared not to function. When the board is
unavailable or a request fails, there is no error shown and the button remains
disabled.

### Current code path

In `tools/retro_html.py`, the generated JavaScript does this:

```javascript
prep.disabled = true;
fetch('/api/dispatch/prepare', {method:'POST'})
  .then(...)
  .then(...);
```

There is no `.catch(...)` or `finally` path. A network error, non-JSON response,
server exception, or stale board URL leaves `prep.disabled` set to `true`.

The same defensive error handling should be applied to the Run queue and
Accept/Defer requests.

### Required change

Every board request must:

- check `response.ok`;
- handle invalid JSON;
- display a useful error in the page;
- restore the button's enabled state;
- leave the page usable for retrying or refreshing.

Do not silently swallow a board failure.

## Finding 5: generated pages can retain a dead board URL

### Observed behavior

`docs/retro/board.state.json` can contain a port and PID for a board process
that no longer exists. In that state, a previously opened page posts to a dead
loopback port.

The page currently observed this situation as a failed request to the recorded
port. There was no listener, even though `board.state.json` still contained the
old port and PID.

### Current design

`board.ensure_running()` is called while generating `plan.html` or `retro.html`.
A stale page does not call `ensure_running()` before making API requests. The
board state file is not a durable service registry; it is only a snapshot of
the last board process.

### Required change

Make board availability explicit and recoverable:

- expose a health/reconnect path that can start or confirm the board;
- or make page generation the documented required startup step and make the
  page clearly report that the board is unavailable;
- never leave the user with a permanently disabled control and no explanation.

The board should verify both recorded PID liveness and port responsiveness. A
dead process must not be treated as a usable board just because state JSON
exists.

## Finding 6: `file://` and HTTP behavior are different but not obvious enough

### Observed behavior

Opening `retro.html` by double-click displays the report, but board controls do
not work because browser `fetch()` is blocked under `file://`.

### Required change

Keep the read-only `file://` fallback, but make the mode clear in the rendered
page. For example:

- show a read-only notice when board detection fails;
- explain that the loopback URL is required for approval and dispatch;
- provide the board URL when it is known;
- keep all controls hidden or disabled intentionally rather than appearing
  clickable but inert.

## Finding 7: approval, prompt generation, and dispatch need clearer state

The workflow has two deliberate dispatch clicks:

```text
Accept finding
  -> Generate queue (prepare and review prompts; no worker starts)
  -> Run queue (spend quota and start workers)
```

That safety boundary is good and should remain. The UI should make each state
visible:

- awaiting review;
- accepted and awaiting queue generation;
- queue generated and editable;
- queue running;
- queue finished;
- queue failed and retryable.

The queue should identify which stored prompt artifact it came from and when it
was generated. If the underlying finding changes, the old queue should be
invalidated or clearly marked stale instead of silently rebuilding different
content.

## Finding 8: worker dispatch itself is conceptually correct but should consume
the stored prompt

The existing `RunManager` behavior is broadly appropriate:

- one finding per fresh kit-builder session;
- sequential queue execution;
- a failed item halts the queue;
- prompt and process logs are saved under `docs/retro/runs/`;
- the board watches for the new session ID and process exit;
- successful completion regenerates `retro.html` and `plan.html`.

Keep those properties, but change the input to `spawn()` so it receives the
stored, already-reviewed prompt. `spawn()` should not need to resolve citations
or inspect session logs.

## Suggested end-state flow

```text
retro.py
  -> compact evidence.json
  -> retrospective model
  -> findings Markdown
  -> ranked findings + dispatch prompt artifacts
  -> retro.html

human clicks Accept
  -> accepted.json

human clicks Generate queue
  -> read accepted findings
  -> read stored prompt artifacts
  -> display prompts immediately

human clicks Run queue
  -> spawn kit-builder with exact displayed prompt
  -> monitor process and log
  -> run next item only after success
  -> regenerate plan.html and retro.html
```

## Acceptance criteria

1. Generate queue does not open or parse any raw `events.jsonl` files.
2. Queue generation is fast for one or many accepted findings.
3. The displayed prompt is the exact prompt sent to the kit-builder.
4. Prompt artifacts are created before dispatch and survive page reloads.
5. A failed board request shows an error and re-enables the relevant button.
6. A stale/dead board is detected and reported clearly.
7. `file://` remains read-only and explains how to use the loopback board.
8. The two-click safety boundary remains: Generate never spawns Copilot;
   Run queue is the first operation that spends quota.
9. Queue execution remains sequential and stops after a failed worker.
10. The kit-builder still receives the restrictions to modify kit files only,
    avoid `src/`, and finish with a green `python check.py`.
11. Add targeted tests for prompt artifact generation/loading, failed API
    responses, stale board detection, and queue dispatch without session-log
    access.
12. Run the repository gate and verify both generated pages through the board.

## Relevant files

| Area | Files |
| --- | --- |
| Evidence collection | `tools/retro.py`, `tools/session_digest.py` |
| Findings and citation ranking | `tools/retro_rank.py`, `tools/retro_sdk.py` |
| Findings HTML and browser actions | `tools/retro_html.py`, `tools/board_client.py` |
| Loopback server and worker queue | `tools/board.py` |
| Plan generation | `tools/plan_html.py` |
| Persistent retrospective state | `docs/retro/evidence.json`, `docs/retro/accepted.json`, `docs/retro/deferred.json`, `docs/retro/board.state.json` |

## Outcome, 2026-08-19

Walked criterion by criterion after the integration pass. Evidence is a command
or a test name, never an assertion that it looks right.

| # | Criterion | Holds | Evidence |
| --- | --- | --- | --- |
| 1 | Queue prep opens no raw `events.jsonl` | yes | `TestNoSessionLogAccess.test_build_dispatch_queue_opens_no_session_log` replaces `digest_one`, `discover` and `citation_report` with functions that raise, then builds the whole queue. The request that used to do it, `/api/dispatch/prepare`, no longer exists. |
| 2 | Queue generation is fast for one or many | yes | `build_dispatch_queue()` is a filesystem read of `docs/retro/queue/*.json`; VERIFY B59 times it at well under a second for five artifacts. |
| 3 | The displayed prompt is the prompt sent | yes | One `retro_queue.render_prompt`; both paths call it. `TestEndToEnd.test_a_no_comment_approval_dispatches_the_preview_byte_for_byte` compares `runs/<id>.prompt.md` to the served `prompt_preview` byte for byte, and the live run below did the same against the real board. |
| 4 | Artifacts exist before dispatch and survive reloads | yes | `docs/retro/queue/<slug>.json` + `index.json`, written by `retro_rank.py`; `python tools/retro_queue.py --list` reports five, all `ok`. |
| 5 | A failed request shows an error and re-enables the button | yes | `Board.request` + `B.guard` are the only fetch path (`RetroPageBytes.test_no_bare_fetch_outside_the_shared_client`); `node tools/tests/dom_harness.js` reports `"failed": 0` for both pages across 500, non-JSON and refused-connection scenarios, and VERIFY B62 gives the falsification procedure. |
| 6 | A stale or dead board is detected and reported | yes | `board_health` requires pid liveness **and** a payload-checking port probe. Observed live: `board: restarting -- pid 18732 is gone and port 53519 does not answer`, then a different port. |
| 7 | `file://` stays read-only and explains the loopback board | yes | `python tools/tests/browser_check.py` → `29/29`, including *body is `board-file`*, *controls are disabled, not clickable-but-inert*, and the prompts still readable. |
| 8 | The two-click boundary (Generate never spawns; Run spends quota) | **superseded, deliberately** | Decision 4: approval *is* dispatch, there is no second click. Do not read this row as done. The requirement underneath it — quota is never spent without a conscious act — is preserved differently: the prompt is displayed *before* the approve control, the human writes a comment that becomes part of it, and pressing Approve is the single conscious act. The page says so in words (`test_approving_says_it_spends_quota` asserts "spends quota" and "no second confirmation" are on it). |
| 9 | Execution is sequential and stops after a failure | yes | One queue in `RunManager`; `TestRealRunManagerQueue.test_only_one_runs_at_a_time` and the `_halted` flag. Live: a second approval read `queued behind 1 run(s)` and its worker started only after the first exited 0. |
| 10 | The kit-builder still gets its restrictions | yes | `retro_queue.DISPATCH_HEADER` is part of the stored prompt, so it cannot be lost between display and dispatch. Corrected in this pass: it told the worker to re-baseline with `--accept-gate-changes`, which `AGENTS.md` reserves to humans. It now says `python bootstrap.py --fix` and that the flag is human-only. |
| 11 | Targeted tests exist | yes | 83 tests under `tools/tests/`: 18 artifact, 40 API, 13 page-bytes, 12 integration, plus the node DOM harness and the browser check. |
| 12 | The gate runs and both pages are verified through the board | yes | `python check.py` green (pasted in the slice report), `browser_check.py` 29/29 against a board serving the real generated pages, and one real approval driven end to end through a live board. |

### The live end-to-end run

Performed once against a real board on a real port, with the **spawn stubbed**: a
`copilot.cmd` that sleeps and exits 0 was placed first on `PATH`, so the whole
loop ran and no quota was spent. Twelve checks passed — `prompt_preview` served
before approval; the comment persisted to `accepted.json`; a worker process with
a live pid; `runs/<id>.prompt.md` equal to `render_prompt(item, comment)` and
starting with the displayed preview; the item `working` immediately, then `done`
with exit code 0; a second approval `queued`, started only after the first
finished. The repository state that run created was restored afterwards, so no
stub run is left looking like an implemented finding. Reproduction: VERIFY B65.

