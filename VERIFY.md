# Release acceptance

This document defines the evidence required to call the kit reusable and
release-ready. It validates the kit, not the starter game or its design.

Use the public launcher throughout: `\.\kit.cmd` on Windows or `./kit` on
macOS/Linux. Commands below use the platform-neutral name `kit`.

## Safety boundaries

- `kit doctor` may resolve a safe exact-version path but must never execute it;
  `kit verify --static` and `kit self-test` must not discover or launch Godot.
- No setup, dependency acquisition, editor change, Git initialization, import,
  format, provider call or integrity acceptance is inferred from another action.
- Full and strict verification may start Godot. Run them only after the native
  engine has been explicitly accepted as safe in the current environment.
- `kit self-test` sets a no-engine environment guard. If any test reaches the
  native boundary, the gate fails before discovery or process creation.
- Automatic provider tests use fakes or typed command inspection. They must not
  spend quota, access a network, or mutate the current checkout.
- Release construction writes only the requested archive and private scratch.
  It never publishes.

## Local acceptance sequence

### 1. Readiness

```text
kit doctor --json
```

Accept only a structured result. `complete: true` means required local
prerequisites match their declared contract. `complete: false` is not permission
to repair anything: each mismatch must name its specific remedy or human decision.

Record the repository status before and after. Doctor must not create
`.kit/runtime/`, start Godot, call a provider, or change any file.

### 2. Kit control-plane regression suite

```text
kit self-test
```

Expected result:

```text
self-test: passed
  native engine: disabled
```

This suite covers public routing, setup mutation boundaries, static/native stage
separation, planning and retrospective rendering, evidence scope, dispatch HTTP
security, exact prompt transfer, isolated worker evaluation, deterministic release
archives and strict-verification orchestration.

### 3. Static project and kit gate

```text
kit verify --static
```

Integrity runs first. If it fails, stop: the protected changes need human review.
The command must finish without a Godot discovery or launch line. Static success
does not prove engine behavior.

### 4. Generated decision views

```text
kit plan
```

Open `plan.html` and `retro.html` directly. Confirm:

- the plan begins with the current outcome, truthful state, latest evidence and
  the next decision;
- the retrospective begins with problem, recommended change, success measure and
  a clear decision;
- detailed records, hashes, prompts, diagrams and diagnostics are available but
  collapsed by default;
- file mode is read-only and explains how to start the board;
- no server or provider starts merely because the pages were generated or opened.

For live decision controls:

```text
kit serve
```

Confirm the printed URL is loopback, both pages load, mutation controls work only
through the authenticated served page, and cross-origin or tokenless requests are
rejected. Stop the server after inspection.

### 5. Retrospective workflow

```text
kit retro status
kit retro run
kit retro publish
```

With the default manual provider, these actions must remain deterministic and
must not call a model. An automatic analyzer must be refused unless the explicit
`--confirm-spend` flag is present.

Evidence acceptance requires:

- repository-scoped source sessions;
- POSIX paths compared case-sensitively and Windows paths compared according to
  Windows semantics;
- injected platform instructions filtered while genuine opening tasks remain;
- immutable snapshot hashes and stable source IDs;
- findings citing the exact snapshot used;
- transient packs, queues, prompts and logs under `.kit/runtime/` only;
- tracked human decisions kept separate from retry/execution state.

Dispatch acceptance requires:

- exact byte parity between the displayed approved prompt, sealed prompt and
  worker standard input;
- no prompt content in shell text or process arguments;
- loopback Host, Origin, content-type, body-size and capability checks;
- protected declared scope rejected before clone, provider startup or spend;
- one isolated job at a time, idempotent retries and stop-on-first-failure;
- detached Git metadata during provider access;
- changed-path and commit verification on the trusted host;
- exit zero with no relevant edit treated as failure;
- `kit verify --static` recorded before integration;
- no automatic engine launch.

### 6. Release archive

Release construction is intentionally blocked until an approved top-level
`LICENSE*` or `COPYING*` exists and `VERSION` contains one safe version line.
Once those human-owned decisions are present:

```text
kit release build ../godot-agent-kit.zip
kit release inspect ../godot-agent-kit.zip
kit release verify ../godot-agent-kit.zip
```

Build the same source twice to different output paths. The archives and reported
SHA-256 values must be byte-identical.

Inspect the member list. It must contain every fixed kit file, all canonical
personas and every shipped skill, plus the canonical manifest and approved legal
metadata. It must not contain:

- `src/` or any game file;
- `docs/design/`, `project.shape.json` or `proposal.json`;
- generated plan/retro HTML;
- current retrospective findings, decisions, evidence or queues;
- `.kit/runtime/`;
- secrets, environment files, caches or unreviewed tools;
- local absolute paths.

Verification must reject malformed or tampered archives without extracting:

- absolute, traversing or non-canonical member names;
- duplicate members;
- symlinks and Windows reparse-point sources;
- non-regular TAR/ZIP members;
- oversized archives, files or total expansion;
- files absent from or extra to the manifest;
- invalid mode, line ending, size or SHA-256 metadata;
- missing required kit surfaces, legal data or version identity.

### 7. Native engine and strict release proof

This is the only local acceptance step that intentionally starts Godot. Do not run
it while the engine is known to crash or before the user approves a bounded retry.

```text
kit verify
kit verify --strict
```

The gate performs one engine health check after all static stages. If it fails,
every native stage must be skipped and no second engine process may start. If a
native stage fails, every later native stage must likewise be skipped.

Strict success requires every stage supported and passed, retained evidence under
the configured private runtime, release-archive smoke validation, and no unsupported
skip converted into a false green.

The canonical kit-source checkout has one narrower contract: an exact marker at
`src/.kit-maintainer-fixture` identifies its release-excluded engine fixture. Only
there may strict verification accept `shape (absent)`, `design` and
`conformance` as not applicable, because the canonical source deliberately contains
no game state. The marker is not included in release archives. Extracted kits and
real projects therefore continue to reject every skipped stage.

## What the automated suite proves

| Area | Authoritative regression evidence |
| --- | --- |
| Public launcher and JSON contract | CLI routing tests exercise every public command and refusal path |
| Root and layout portability | Project-context fixtures cover root/src layouts, nested invocation and unsafe paths |
| Setup safety | Mutation-boundary fixtures preserve existing HEAD, staged, working and untracked state |
| Engine selection and containment | Resolver and engine-boundary tests cover stale variables, exact-version console preference, no-launch diagnosis, owned timeouts and failure-cascade suppression |
| Dependency provenance | The public source-audit command downloads and authenticates every canonical direct lock artifact without installing it; a stable tree-digest fixture guards the GUT lock contract |
| Planning UX | Frontend byte and DOM tests assert decision-first ordering, truthful states and read-only file behavior |
| Evidence integrity | Session-evidence tests cover source classification, stable IDs, hashes and repository/path semantics |
| Retrospective lifecycle | Workflow tests cover manual defaults, spend confirmation, immutable findings inputs and publication |
| Board security | HTTP tests cover bind address, Host, Origin, capability, content type, body bounds and response shape |
| Provider command safety | Adapter tests prove a closed registry, safe identifiers, official package entry point and prompt-free argv |
| Worker isolation | Run-result tests cover detached Git metadata, scope, hooks/config neutrality, no-op rejection and integration |
| Dispatch seams | Production-shaped integration tests prove prompt parity, sequential operation and halt-on-failure |
| Releases | ZIP/TAR tests cover determinism, allowlist completeness, leak exclusion, link/reparse rejection and smoke launch |
| Strict orchestration and CI | Strict tests cover fail-closed prerequisites/evidence and the three-platform official-source workflow |

## What local static evidence cannot prove

Call these out rather than inferring them:

- that the current Godot binary is stable;
- that game scenes look, feel or animate correctly;
- editor, device, export or real-latency behavior;
- a live third-party provider's service availability or account permissions;
- remote publishing or consumer installation experience;
- macOS/Linux behavior until their CI jobs complete;
- legal suitability of a license selected by the project owner.

## Cross-platform CI acceptance

`.github/workflows/ci.yml` runs on pinned Windows, macOS and Linux runner labels.
For each platform it:

1. checks out without retained credentials using an official action pinned to an
   immutable commit;
2. provisions the declared Python minimum and current Node line with pinned
   official actions;
3. runs `kit self-test` before any engine download;
4. authenticates every canonical third-party lock source without publishing it;
5. acquires the optional locked GDScript style adapter through the public launcher;
6. downloads the official Godot archive and authenticates it against the
   release's official SHA-512 list before safe extraction;
7. runs `kit verify --strict --json` through the platform launcher;
8. prints retained JSON/log evidence on every outcome.

All three jobs are required evidence for a cross-platform release claim. A local
Windows pass alone is not one.

## Completion checklist

The kit is release-ready only when all answers are yes:

- [ ] `kit doctor --json` reports complete without mutation.
- [ ] `kit self-test` passes with native engine disabled.
- [ ] `kit verify --static` passes after a human-approved integrity baseline.
- [ ] `plan.html` and `retro.html` satisfy the decision-first visual review.
- [ ] Retrospective evidence and dispatch seam tests pass.
- [ ] Approved legal metadata and version identity exist.
- [ ] Two release builds are byte-identical and independently verify.
- [ ] The archive contains the complete kit and none of the excluded project/private state.
- [ ] A bounded full verification succeeds without repeated native launches.
- [ ] `kit verify --strict` passes.
- [ ] Windows, macOS and Linux CI jobs pass.
- [ ] Any visual, provider, legal or external-publishing limitation is explicitly reported.

Until every applicable item has direct evidence, report the kit as incomplete or
blocked on the named human decision. Do not convert missing proof into a release
claim.
