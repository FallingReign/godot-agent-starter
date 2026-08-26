# Retrospectives

The retrospective loop improves the kit without giving an analyzer permission to
rewrite the system that judges its own work. Capture is deterministic, decisions
are human-owned, and every automatic worker is bounded and verified.

## Public workflow

```text
kit retro status                  # due state; never calls a model
kit retro run                     # prepare evidence with manual default
kit retro run --confirm-spend     # explicit automatic analyzer action
kit retro publish                 # validate findings and refresh the board
kit plan                          # refresh decision-first pages
kit serve                         # authenticated loopback board
```

Both model-backed roles default to `manual` in `kit.config.json`. With that
default, status and evidence capture still work, but automatic analysis and
dispatch remain unavailable. The kit never guesses a provider, installs one,
authenticates one, or spends quota in response to verification, page generation,
startup, a timer, or a missing dependency.

With the manual default, `retro run` freezes and prepares evidence but does not
invent a provider call. A reviewer or manually chosen analyzer writes the resulting
`docs/retro/*-findings.md`; `retro publish` then validates its citations and success
measures, builds immutable dispatch artifacts, and refreshes both decision views.
For an automatic analyzer, `--confirm-spend` is necessary but not sufficient: the
configured analyzer must be a known adapter and pass its read-only preflight.
Worker dispatch is a separate human decision made on the board after the exact
recommendation is visible.

## Evidence contract

Raw chat databases and `events.json`/`events.jsonl` files are discovery inputs,
not finding citations and never dispatch inputs. The capture step selects only
sessions belonging to this repository and reduces them into an immutable
snapshot under `.kit/runtime/evidence/sessions/`. A snapshot records its source
identity and content hash; changing or replacing it makes downstream artifacts
invalid rather than silently changing what a citation means.

Citation IDs are scoped to one named snapshot. `S3:H4` is meaningful only with
that snapshot's identity and hash; it is not a globally stable conversation ID.
Every finding must bind to the snapshot it used, and each cited occurrence must
resolve inside it. Evidence packs and the `latest.json` convenience pointer live
under `.kit/runtime/evidence/`; the pointer is not itself evidence.

The private runtime may contain verbatim human messages, prompts and logs. It is
gitignored, excluded from releases and must not be copied into durable docs.
Findings should quote only the minimum needed to explain a recurring mechanism.

## What each role owns

- The **game builder** writes short factual notes under `docs/retro/notes/` at a
  slice boundary. A note is testimony, not a kit proposal.
- The **retrospective analyzer** reads a frozen evidence pack and writes findings
  under `docs/retro/`. It cannot change code, rules, skills or agent instructions.
- The **human** approves or defers each recommendation and supplies the decision
  comment. With an automatic provider, approval also permits an isolated run;
  otherwise the decision remains approved for a manual implementation handoff.
- The **kit builder** implements an approved finding only within the configured
  kit-owned paths. It cannot touch the game, design, current project plan,
  retrospective records, private runtime or `.gate.sha256`.

This permission split is deliberate. An analyzer that can both decide a rule is
wrong and remove it can manufacture a green result without improving the kit.

## Finding and dispatch contract

A dispatchable finding contains all of the following:

1. the recurring problem and its consequence;
2. two or more resolved occurrence citations, or an explicit hypothesis label;
3. the recommended kit change;
4. a mechanically checkable success measure;
5. the immutable evidence snapshot identity and hash;
6. the source findings-file hash and bounded kit-owned file scope.

Missing measures, unresolved citations, legacy unbound findings, malformed
artifacts and changed source files are visible as blockers. The board refuses
their approval/dispatch; it never rebuilds a prompt behind the reviewer's back.

Queue artifacts live in `.kit/runtime/retro/queue/`. The page, preview and spawn
read the same validated artifact. The sealed prompt file is piped to the worker's
standard input and is never put through a command shell or exposed in its process
arguments. Approving twice with the same idempotency key
returns the original decision/run rather than launching a second worker.
Approvals are processed sequentially so two workers cannot race on a gate file.
A failed worker stops the queue. Manual, unavailable or invalid providers and an
unstable repository do not erase the human decision: the item remains visibly
approved and states why no implementation started.

The board binds to loopback, uses an unguessable session token, checks request
origin and content type, and serves a restrictive content-security policy. Its
state, lock, logs, prompts and worker runs live under `.kit/runtime/board/`. A
recorded process is always re-probed; a stale PID, port or server-code fingerprint
is not trusted. Rendering never starts a server: `kit serve` is the explicit
background-process boundary.

Worker completion is a claim, not proof. The result verifier checks the exact Git
change set against the ownership/forbidden policy stored in the immutable
pre-dispatch commit, confirms the expected commit relationship and records
verification output. A worker cannot widen the policy used to judge its own run.
A run that touches the game, design, current retro records, runtime, an unowned
path or `.gate.sha256` is not reported as successfully implemented. The policy
limits automatic work to non-executable kit documentation: agent instructions,
dependencies, workflows, every script and test, and the evidence, queue,
provider, board and result-verification control plane require an interactive
maintainer. The declared `fix_files`
scope is checked before quota is spent. Approval is still recorded, but the board
shows `approved` with the blocker and requires an interactive kit maintainer.
Integrity acceptance remains human-only.

An automatic worker starts in a fully copied local clone of the reserved commit,
with its origin removed. Before launch, the host moves the clone's Git metadata
outside the provider-visible directory and prevents Git discovery from reaching
the parent source repository. Its non-interactive Copilot session is restricted
to file discovery, reading and editing inside that metadata-free clone. Shell,
URL, memory, MCP, sub-agent, `.git` write and system-temporary-directory tools are
denied. If the provider recreates the reserved `.git` path, finalization fails
without running Git in that workspace. On Windows, the official npm package's
declared Node entry point is used instead of its command-shell shim. Otherwise
the trusted host restores only the sealed metadata after exit, rechecks the exact
`fix_files` and baseline policy, runs `kit verify --static` for this deliberately
non-executable scope, creates the commit with hooks disabled, and only then
integrates. Full engine verification remains an interactive release boundary.
The bound provider record includes a 1-240 minute timeout (30 by
default). The board stops the process tree it owns when that window expires. A
restarted board never kills an unproven PID. Verified work is fast-forwarded only
if the project still has the same baseline and no unrelated changes; otherwise it
remains preserved and reported as verified-in-isolation but blocked from
integration.

## Decision-first board

`retro.html` leads with the problem, recommended change, success measure and
current decision. Scores, raw evidence, hashes and the exact prompt are available
under disclosure controls for audit, but do not crowd the decision surface.

Findings appear in three distinct groups:

| Group | Meaning | Stable order |
| --- | --- | --- |
| To action | Valid, undecided work | decision priority; unranked last |
| Activity | Accepted decisions and implementation evidence | attention, approved-only, live/stalled, queued, verified; newest first |
| Deferred | Settled for now | newest decision first |

A settled item never returns to **To action** because transient board state was
lost. A silent worker becomes visibly stalled and shows an adapter-specific
resume hint only when the configured adapter supports one.

## Durable and private files

| Location | Purpose |
| --- | --- |
| `docs/retro/notes/` | factual slice testimony |
| `docs/retro/archive/` | notes already consumed by a completed retrospective |
| `docs/retro/*-findings.md` | human-readable analyzer recommendations |
| `docs/retro/accepted.json` | durable human approvals and comments |
| `docs/retro/deferred.json` | durable human deferrals and reasons |
| `.kit/runtime/evidence/` | private immutable session snapshots and evidence packs |
| `.kit/runtime/retro/queue/` | validated dispatch artifacts |
| `.kit/runtime/board/` | process state, locks, prompts, logs and run results |

Generated `retro.html` is a view, never a source of truth. Private runtime state
is recoverable and release-excluded; human decisions are durable but are also
excluded from a distributable kit so a new project starts with no inherited
history.
