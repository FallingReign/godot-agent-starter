# Release acceptance

This document defines the evidence required to call the kit reusable and
release-ready. It validates the kit, not the starter game or its design.

Use the public launcher throughout: `.\kit.cmd` on Windows or `./kit` on
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
kit plan --snapshot
```

Open `plan.html` and `retro.html` directly. Confirm:

- the plan begins with the player-facing Quick Read, exact design authority,
  reversible envelope, next go/no-go point, truthful implementation state,
  verification freshness and the one current decision;
- the retrospective begins with problem, recommended change, success measure and
  a clear decision;
- detailed records, hashes, prompts, diagrams and diagnostics are available but
  collapsed by default;
- the retained snapshot is immutable and bound to the reviewed proposal/design
  fingerprint;
- file mode is read-only, labels the cockpit live/down/file state truthfully and
  explains the exact `kit serve` lifecycle;
- no server or provider starts merely because the pages were generated or opened.

For live decision controls:

```text
kit serve
kit serve status
kit serve open
kit serve stop
```

Confirm the printed review URL is the real loopback `plan.html` page, both pages
load, start/reuse does not implicitly open a browser, `open` is the only browser
side effect, and mutation controls work only through the capability-protected
served page. Stale plan fingerprints, malformed design, tokenless/cross-origin requests
and unsupported legacy approval contracts must be rejected with zero partial
source or ledger writes. Confirm stop refuses while an owned run is active and
otherwise terminates the recorded cockpit cleanly.

Approval acceptance requires:

- exact `docs/design/...` references and canonical design digests;
- complete design metadata and implementation eligibility;
- agent-authored design only at `very-high` confidence, with Quick Read,
  inference, assumptions, veto scope and next go/no-go content;
- recorded hands-off work only inside explicit coarse file/directory `scope[]`
  boundaries and an explicitly reversible envelope;
- an operator confirmation event bound to the complete current proposal digest,
  with portable-policy evidence never presented as person authentication;
- veto superseding the exact confirmation, with a later approval requiring a
  new current digest;
- Git baseline and all authored changed-file scope proven fail-closed;
- absent `proposal.json` accepted only when Git proves zero authored game changes;
- inverse authored scope including custom/extensionless content, tests and
  deletions;
- complete proposed module dependency boundaries compared with the real graph;
- function-level canonical typed signatures and normalized bodies compared
  with the exact baseline source;
- any unproposed module, file, function or action treated as a blocking
  deviation.
- a static run may report declared work as unbuilt, while full and strict
  verification must reject that state as incomplete.

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

- repository-scoped Copilot and Codex sessions, including active and archived
  Codex rollouts whose scope comes only from their first trusted session cwd;
- resumed Codex tasks ordered by latest rollout activity, with duplicate
  active/archive IDs selecting the newest activity without retaining bodies;
- POSIX paths compared case-sensitively and Windows paths compared according to
  Windows semantics;
- injected platform instructions filtered while genuine opening tasks remain;
- no assistant/tool bodies, hidden reasoning, environment values, raw command
  output or absolute source paths retained in evidence;
- Copilot AIU and Codex token metrics kept separate, with no inferred monetary
  cost;
- parent/child Codex linkage retained without duplicating inherited human
  messages;
- immutable snapshot hashes and stable source IDs;
- findings citing the exact snapshot used;
- manual publication closing only that exact content-addressed snapshot after
  ranking and both views succeed, with note rollback on report drift or marker
  failure;
- transient packs, queues, prompts and logs under `.kit/runtime/` only;
- tracked human decisions kept separate from retry/execution state.

Dispatch acceptance requires:

- exact byte parity between the displayed approved prompt, sealed prompt and
  worker standard input;
- no prompt content in shell text or process arguments;
- loopback Host, Origin, content-type, body-size and capability checks;
- protected declared scope rejected before clone, provider startup or spend;
- one isolated job at a time, exact-approval replay safety and stop-on-first-failure;
- detached Git metadata during provider access;
- changed-path and commit verification on the trusted host;
- exit zero with no relevant edit treated as failure;
- `kit verify --static` recorded before integration;
- no automatic engine launch.

Automatic Copilot and Codex analysis/dispatch must fail closed while their
configured hosts cannot prove repository-only reads at the OS boundary. This
refusal is an accepted security outcome; both interactive workflows and manual
evidence handoff remain usable.

### 6. Release archive

Release construction requires the approved top-level `LICENSE` and the single
safe version identity in `VERSION`. Both are present in the canonical kit and
must remain part of every verified archive. The source repository must be clean;
uncommitted or untracked bytes are not production provenance:

```text
kit release build ../godot-agent-kit.zip
kit release inspect ../godot-agent-kit.zip
kit release verify ../godot-agent-kit.zip
```

Build the same source twice to different output paths. The archives and reported
SHA-256 values must be byte-identical.

The canonical manifest must report `receipt_trust: portable-policy`, identity
model `portable-policy-audit`, and the source project's pre-exclusion receipt
state. Verification and smoke must reject any other archive trust or identity
label. This proves portable policy consistency, not who operated the cockpit.
A present contradictory local receipt blocks build; no proposal/shape state is
reported as `not-applicable-no-project-state`.

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

`ARCHITECTURE.md` must equal the canonical empty release template even when the
source checkout's generated graph contains project-only modules and edges.
Verification must reject a manifest that reauthorizes different architecture
bytes. If the destination has no architecture document, Preview must render its
real graph from the authenticated template and rules, bind those exact bytes in
the review digest, and Apply must write those bytes. Existing destination
documents must remain unchanged.

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

On Windows, a simulated or real native access violation must be classified as
`native-crash`, modal Application Error UI must be suppressed, and only the
owned process tree may be terminated. The private verification ledger must keep
that warning active after a passing static check; only a later successful full
or strict native run resolves it. The cockpit and `kit retro status` must expose
the unresolved consequence without relaunching Godot.

Strict success requires every stage supported and passed, retained evidence under
the configured private runtime, release-archive smoke validation, and no unsupported
skip converted into a false green.

A source checkout proves release determinism by building twice from clean Git
provenance. A managed install has no source Git history, so it proves the same
active release by materializing its already verified core twice. Both paths then
verify both archives, smoke-test one, and require byte-identical output.

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
| Lifecycle project format | Preview tests safely read and fingerprint the selected exact-case `project.godot`; accept an empty, comment-only or section-first header, or one single-line integer assignment `config_version=5`; and block older, newer, malformed, duplicate, ambiguous, redirected, unreadable or changing files |
| Setup safety | Mutation-boundary fixtures preserve existing HEAD, staged, working and untracked state |
| Engine selection and containment | Resolver and engine-boundary tests cover stale variables, exact-version console preference, no-launch diagnosis, owned timeouts and failure-cascade suppression |
| Verification evidence | Cockpit tests cover immutable runs and unresolved-native recovery; CLI unit tests replace the writer so `kit self-test` cannot overwrite the project's real latest-verification pointer |
| Dependency provenance | The public source-audit command downloads and authenticates every canonical direct lock artifact without installing it; a stable tree-digest fixture guards the GUT lock contract |
| Planning UX | Cockpit, frontend byte and DOM tests assert exact approvals, atomic veto/rollback, decision-first ordering, truthful live/file/down states, canonical function status and immutable snapshots |
| Evidence integrity | Session-evidence tests cover Copilot/Codex discovery, before/after repository re-authentication, explicit session/byte/record refusal limits, privacy-minimised field allowlists, provider-specific usage, parent linkage, stable IDs, hashes and repository/path semantics |
| Retrospective lifecycle | Workflow tests cover consequence urgency, unresolved crash persistence, manual defaults, spend confirmation, immutable findings inputs and publication |
| Board security | HTTP tests cover bind address, Host, Origin, capability, content type, body bounds and response shape |
| Provider command safety | Adapter tests prove a closed registry, safe identifiers, official package entry point and prompt-free argv |
| Worker isolation | Run-result tests cover detached Git metadata, scope, hooks/config neutrality, no-op rejection and integration |
| Dispatch seams | Production-shaped integration tests prove prompt parity, sequential operation and halt-on-failure |
| Releases | ZIP/TAR tests cover determinism, clean provenance, explicit portable-policy receipt trust, pre-exclusion receipt mismatch refusal, allowlist completeness, leak exclusion, link/reparse rejection and smoke launch |
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
   official actions, binds the public launcher to that exact interpreter and
   proves both doctor and the retained strict report used it;
3. runs `kit self-test` before any engine download;
4. authenticates every canonical third-party lock source without publishing it;
5. acquires the optional locked GDScript style adapter through the public launcher;
6. downloads the official Godot archive and authenticates it against the
   release's official SHA-512 list before safe extraction;
7. explicitly runs `kit setup import` through the platform launcher to build
   the release-excluded cache a fresh checkout cannot contain;
8. runs `kit verify --strict --json` through the platform launcher;
9. prints retained JSON/log evidence on every outcome.

All three jobs are required evidence for a cross-platform release claim. A local
Windows pass alone is not one.

## Completion checklist

The kit is release-ready only when all answers are yes:

- [ ] `kit doctor --json` reports complete without mutation.
- [ ] `kit self-test` passes with native engine disabled.
- [ ] `kit verify --static` passes after a human-approved integrity baseline.
- [ ] `plan.html` and `retro.html` satisfy the decision-first visual review.
- [ ] Exact design/proposal approval, veto, stale-page and rollback fixtures pass.
- [ ] Copilot and Codex evidence fixtures pass with privacy and cost semantics intact.
- [ ] Retrospective evidence and dispatch seam tests pass.
- [ ] Approved legal metadata and version identity exist.
- [ ] Two release builds are byte-identical and independently verify.
- [ ] The archive contains the complete kit and none of the excluded project/private state.
- [ ] An install into a real existing Godot project keeps the kit active without changing game, design or Git files: **Complete** when every check is ready, or **Adoption required** with every unavailable or unchanged check named.
- [ ] A managed A-to-B upgrade activates the exact B release and preserves project-owned files.
- [ ] An exact verified 0.2.0 install migrates into the managed layout without treating modified legacy files as kit-owned.
- [ ] Existing file-scoped project problems produce **Adoption required**; unchanged problems stay visible, while new problems and unresolved problems in changed files fail.
- [ ] **Restore** recreates the exact prior bytes and restores prior file absence.
- [ ] An empty folder or a selected folder without a regular exact-case `project.godot` is refused before project-facing writes; its exact bytes and supported Godot 4 format declaration are bound into Preview without claiming full parsing or the exact engine patch.
- [ ] A missing architecture document is generated from the destination graph and bound into the exact review; an existing document is preserved.
- [ ] Missing test-runner or trusted style-tool checks are reported as adoption work, never invented or hidden, and strict verification rejects their skips.
- [ ] A bounded full verification succeeds without repeated native launches.
- [ ] `kit verify --strict` passes.
- [ ] Windows, macOS and Linux CI jobs pass.
- [ ] Any visual, provider, legal or external-publishing limitation is explicitly reported.

Until every applicable item has direct evidence, report the kit as incomplete or
blocked on the named human decision. Do not convert missing proof into a release
claim.
