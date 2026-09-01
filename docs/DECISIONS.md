# Kit decisions

This is the current policy of the distributable kit. It is deliberately a
decision register, not a chronological engineering diary. Historical rationale
remains available in Git history. Game-specific decisions belong in
`project.shape.json` and `docs/design/`, neither of which ships in a kit release.

Each row says what a new project may rely on, why that boundary exists, and where
a maintainer would deliberately change it.

## Product boundary

| Current decision | Why | Change deliberately by |
| --- | --- | --- |
| The repository kit and the game are separate products | The same kit must work for new and existing projects without shipping the starter game's state | Change the release allowlist in `tools/release.py` and its exclusion tests |
| Installed kit code lives as an authenticated release under `.agent-kit/`, while game and project files stay outside it | Installing or upgrading the kit must not spread replaceable kit code through a brownfield project or overwrite human work | Change the install manifest, lifecycle transaction and managed-layout fixtures together |
| Root agent instructions are shared surfaces with one marked kit section; content outside that section remains project-owned | Codex and Copilot need the same rules, but an existing project's instructions must survive installation and upgrade | Change both bridge adapters and their preservation/conflict tests together |
| A kit root is identified by `.agent-kit.json` | Discovery must not infer a project boundary from the working directory or a game file | Change `tools/project_context.py` and every launcher test together |
| `kit.config.json` owns `game_root`, private `runtime_root`, providers and dispatch policy | Project layout and machine-local state must be explicit and portable | Change the schema and migration tests before adding another configuration source |
| Both root-layout and `src/`-layout Godot projects are supported | Adoption must not require moving an existing game | Use `kit setup layout root` or `kit setup layout src`; add a layout only with cross-platform fixtures |
| `src/`, game design, proposals, generated views and retrospective run state never ship | A reusable kit must not disclose or impose the source project's product state | Change the closed release policy and its leak-marker tests |
| Godot 4.7.2 Standard and GDScript are the supported engine boundary | The gate, API lookup and strict-language guidance need one exact testable contract | Update `tools/engine_discovery.py`, `check.py`, `AGENTS.md`, dependency guidance and all authenticated CI archives together |

## Public workflow and setup

| Current decision | Why | Change deliberately by |
| --- | --- | --- |
| `kit` / `kit.cmd` is the only public command surface | Developers should not need to know which internal language or script implements an action | Add a `kit.py` command and document only the launcher form |
| Automation may bind the public launcher with process-local `KIT_PYTHON`, which accepts only an absolute interpreter file and never falls back | A declared CI Python version is not evidence when a platform launcher can silently select another installed version | Bind the setup action's exact executable, prove doctor and strict report the declared version, and keep the override out of kit configuration |
| The Python 3.10 minimum receives a real control-plane run on the latest 3.10 security release, while full desktop strict verification uses one exact current Python release available on every runner | Python 3.10 security releases no longer publish Windows or macOS installers, so one old cross-host patch would make production proof stale; separating compatibility from production proof preserves both promises | Update the minimum-runtime and strict matrix entries together, retain the launcher-version assertions, and prove both paths in `test_strict_verify.py` |
| `kit doctor --json` is offline and read-only | Diagnosis must be safe to run before consent and must never trigger the engine crash it is diagnosing | Keep execution, downloads and mutations out of the doctor path; prove the boundary in `test_bootstrap.py` and `test_engine_boundary.py` |
| A clearly stale versioned `GODOT_BIN` may fall forward only to an exact-version official-style sibling or project-adjacent binary | A global variable for another project must not make this kit launch a known-wrong engine, while generic executable names still need the engine's own version probe | Keep selection read-only in `tools/engine_discovery.py`; retain the bounded exact-version check in `check.py` |
| Setup side effects are separate explicit commands | Consent to setup is not consent to network, editor, Git, import, formatting, naming or profile mutation | Add a named `kit setup` action with its own opt-in and mutation test |
| Install and upgrade always begin with one offline read-only change plan bound to the exact release, project state and affected-file hashes | A folder overlay cannot distinguish safe replacement from lost human work, and approval of one plan must not authorize another | Change the preview schema, full-digest comparison and stale-plan tests together |
| Install and upgrade are journalled, backed up and reversible; the active release changes last | An interruption or failed check must leave either the exact previous state or an explicitly recoverable new state | Change the transaction state machine only with failure injection at every write boundary |
| Brownfield installation records existing gaps without calling them fixed; new or worsened gaps fail | Requiring immediate cleanup makes installation a rewrite, while ignoring old failures breaks the kit's quality promise | Change the baseline fingerprint format and prove unchanged old gaps, reductions and new failures separately |
| Setup never accepts the gate integrity baseline | A tool must not approve changes to the rules that judge it | Human review ends with `kit integrity accept` |
| Third-party dependencies use exact versions, canonical upstream locations, sizes and SHA-256 hashes | Reproducibility and provenance matter more than opportunistic convenience, and canonical upstream does not mean released by Godot | Review `dependencies.lock.json`, then use the specific opt-in acquisition command |
| gdtoolkit transitives resolve as wheels from canonical PyPI without project imports, inherited pip configuration, environment indexes or caches | The locked direct wheel must not become a side door to an unreviewed repository or stale local artifact | Isolate the Python and pip processes, disable every config file and cache, pass the canonical index explicitly, and test the exact command and environment together |
| Project-private Python tool environments are created at their final path | Virtual-environment entry-point launchers embed their installation path and stop resolving after the environment is renamed | Claim the absent versioned target atomically, build and verify it in place, remove it on a handled failure, and never replace an existing target silently |
| Mermaid remains pinned at 11.17.1 until a newer release solves an identified kit problem | Patch-number drift alone does not justify dependency churn, reacquisition and release requalification | Name the required fix or capability, review the official release, then update the lock and authenticated bundle together |
| `kit setup audit-dependencies` authenticates every direct lock source without publishing it | A checked-in tree alone cannot prove that the acquisition recipe still matches its upstream artifact | Keep the audit on the public launcher and run it in every platform CI job |
| Installed dependency mismatches fail closed and are never silently replaced | Local edits and supply-chain changes require a human decision; absent optional adapters are reported honestly instead of making ordinary setup unusable | Repair or reacquire only after diagnosis names the exact mismatch; strict proof still rejects unsupported skips |
| gdtoolkit is an optional pinned third-party style adapter | Godot typechecking is the correctness boundary; a style tool that can lag new syntax must not be foundational | Normal verification may report `format`/`lint` SKIP; strict/release proof requires the exact adapter and rejects the skip |
| GUT is the selected replaceable third-party project-test adapter | Godot's built-in doctest suite targets engine C++ and the GDScript implementation, requires a custom `tests=yes` build, and is explicitly not for user scripts | Replace the isolated `gut` stage, collection contract and evidence tests together; do not turn project testing into a custom-engine dependency |
| Provider roles default to `manual` | Deterministic planning and evidence capture must not incur hidden spend or external processing | Change the known provider kind in `kit.config.json`; automatic analysis still requires `--confirm-spend` |

## Verification and engine safety

| Current decision | Why | Change deliberately by |
| --- | --- | --- |
| Static stages run before any native-engine stage | Cheap deterministic failures should not launch Godot | Change the ordered stage groups in `check.py` and the engine-boundary tests together |
| `kit verify --static` is a complete non-engine proof, not a synonym for release proof | Automation needs a safe boundary, while engine behavior still requires the engine | Add only stages that are proven never to discover or execute Godot |
| Doctor locates Godot without executing it | Executable discovery is readiness evidence; process startup is a separate risk | Keep version execution at the explicit engine boundary |
| Every non-gate Godot operation authenticates the exact resolved executable before project work | An unversioned `GODOT_BIN` or PATH entry is location evidence, not proof that it is Godot 4.7.2; a filename can also lie | Use one shell-free bounded `--headless --version` probe, bind path/version/file identity, and fail closed before import, doctool or GDLS startup |
| One engine health check guards the native stage group | A crashing binary must not be launched repeatedly by cascading stages | Change only with a regression test proving a native failure cannot cause a second launch |
| The first failed native stage suppresses later native stages | Repeated process crashes add no diagnostic value and can destabilize the host application | Change `check.py` engine-failure state and `test_engine_boundary.py` together |
| Native execution uses fail-closed, bounded process containment | A crashing, interrupted or noisy engine must return bounded evidence rather than block on an Application Error dialog, exhaust memory or leave descendants running | Preserve atomic cross-process launch ownership, streaming tail capture, Windows modal suppression and `KILL_ON_JOB_CLOSE`, POSIX group cleanup, and BaseException cleanup tests; the deliberately retained language server must match PID, token, creation marker and executable identity before stop |
| A POSIX child cannot execute until its independent process-group lifeline is ready | macOS has no Linux parent-death signal, so a fast child could escape if its owner died between spawn and watchdog setup | Start a private release-gate wrapper in the final process group, arm the separate watchdog, then release the wrapper to `exec`; EOF before release exits without running project work |
| An unresolved native crash survives static, fast and targeted checks, appears in the cockpit and is an immediate retrospective consequence | Safe diagnostics must not erase the reason native execution was stopped, and a concurrent newer failure must not be cleared by an older pass | Resolve the exact compare-and-swap warning snapshot only after a successful complete full or strict native verification; unknown or unreadable state refuses recovery; change this with crash-then-static and warning-race evidence |
| Godot exit codes are not trusted on their own | Godot can exit zero with hard errors and nonzero after successful work | Preserve bounded output scanning and expected-error declarations |
| Integrity runs first and fails fast | A modified gate cannot be used as evidence for itself | Review the protected diff, then have a human run `kit integrity accept` |
| `kit verify --strict` is required for a release claim | Unsupported or skipped evidence is not production proof | Change strict policy only with production-shaped regression evidence |
| Unit tests never write the checked-out project's verification pointer | `kit self-test` is required during crash recovery and must not replace real evidence with mocked targeted runs | Isolate the CLI evidence writer in unit fixtures and retain one explicit routing test |

## Planning and decision experience

| Current decision | Why | Change deliberately by |
| --- | --- | --- |
| `plan.html` and `retro.html` are generated views, never authored state | A presentation must not become a second source of truth | Edit the source JSON or Markdown and regenerate with `kit plan` |
| The plan opens with outcome, truthful state and the next decision | The control surface exists to help a human decide, not to expose implementation telemetry | Change the front-door rendering contract and frontend tests together |
| Full records, hashes, diagrams and diagnostics are progressively disclosed | Evidence must remain available without overwhelming the decision | Keep decision controls visible and move detail behind labelled disclosure |
| Module, file and function review uses one stable, dependency-free architecture constellation; its readable item list appears only when SVG rendering fails | Coloured changes need one clear read path, the layout must not keep moving, and an offline distributable must not make its core decision view depend on another package or CDN | Change `tools/plan_html.py`, the involvement caps, normal and forced-failure DOM tests, and real-browser proof together |
| Draft proposals authorize nothing; recorded reversible hands-off proposals and human-approved proposals enforce structure | Early planning must be cheap, reversible autonomy must remain auditable, and `recorded` must never be mistaken for approval | Change proposal schema, conformance and approval workflow together |
| Hands-off proposals bind coarse file/directory `scope[]` rather than guessing modules | Autonomy still needs a reviewable, deletion-aware limit without pretending the human approved an architecture | Change schema, cockpit, conformance, plan rendering and hands-off instructions together |
| Implementation always cites exact design; agent-authored design stays provisional until a human confirms its digest and exact proposal contract | Silence, a no-design acknowledgement and an approval for an older structure cannot become authority, while a very-high-confidence inference can still gather reversible evidence without blocking hands-off work | Preserve orthogonal resolution/authority/authorship/confidence metadata, canonical `approval_sha256`, and append-only human `confirm`/`veto` events bound to `docs_at`, `design_sha256` and `proposal_sha256`; legacy approvals require cockpit re-review |
| Approval, veto and request-changes are one cross-process compare-and-swap transaction | A crash or second cockpit must not leave design, proposal and ledger claiming different decisions | Preserve the private write-ahead journal, exact original-byte checks, recovery tests and substantive rejection fingerprint |
| Authored scope is inverse and deletion-aware | A custom content extension, extensionless file, project test or deleted input must not become invisible merely because a suffix allowlist did not anticipate it | Exclude only reviewed generated/private/engine/third-party surfaces through `tools/authored_scope.py`; change gate and plan consumers together |
| Function involvement binds canonical typed signatures and normalized body changes from the exact Git baseline | Name-only matching and duplicate parsers can falsely claim that a different function was approved or implemented | Keep `tools/gd_signature.py`, schema, architecture, conformance and plan status on one parser; fail closed when baseline or source cannot be represented |
| The board is a view and command surface over durable source state | Starting a server must not be required to render or recover the project | Keep generation deterministic and server startup explicit via `kit serve` |
| `AGENTS.md` is the canonical provider-neutral contract and `.github/copilot-instructions.md` is a thin Copilot bridge | Copilot and Codex must derive the same design, approval, verification and retrospective behavior without duplicated policy drifting | Change the shared contract and both provider fixtures together; interactive Copilot and Codex remain supported |

## Retrospective evidence

| Current decision | Why | Change deliberately by |
| --- | --- | --- |
| Session capture is repository-scoped | Conversations from another checkout or project must never support a finding | Change path normalization and repository-matching tests together |
| Evidence snapshots are immutable, content-addressed inputs to findings | A recommendation must remain auditable after live history changes | Publish a new snapshot; never rewrite the snapshot cited by a finding |
| Human messages are evidence; injected instructions and synthetic context are filtered | The retrospective should evaluate the lived workflow, not platform scaffolding | Change evidence classification with fixtures for opening tasks and injected context |
| Source IDs remain stable and pack-local exhibit IDs are explicit | Citations need to survive repacking without pretending global numbering exists | Preserve source IDs in snapshots and render pack-local IDs only within a named pack |
| Deterministic capture and ranking are separate from model analysis | Evidence collection must be repeatable and free of hidden spend | Keep `kit retro status` and manual `kit retro run` model-free |
| Copilot and Codex evidence share a stored-field privacy allowlist but keep provider-native usage units separate | Cross-provider comparison must not retain hidden work or invent a currency conversion; retained human testimony remains sensitive and best-effort redaction is not a secret-scanner guarantee | Preserve repository re-authentication and explicit ingestion limits, omit assistant/tool/reasoning/raw-output bodies, protect private runtime, keep Copilot AIU separate from Codex tokens and report monetary cost as unknown without an authoritative provider value |
| Codex recency follows latest rollout activity, not task creation time | A resumed older task is current evidence and must not be displaced by newer-created but inactive tasks | Use rollout metadata time for selection and active/archive deduplication without retaining additional bodies |
| `kit retro publish` is the single transactional manual completion boundary | A ranked report or refreshed page alone must not archive testimony or claim that the cited snapshot was consumed | Resolve one exact report before ranking; require both views, exact pack/snapshot/citation validation and byte-stable report publication before moving captured notes and exposing the marker; roll moves back on failure |
| Findings lead with problem, recommended change, success measure and decision | Retrospectives should produce an actionable choice, not an activity report | Change `tools/retro_html.py` and frontend decision-contract tests together |
| Transient evidence, queues, prompts and logs live under a configured descendant of `.kit/` | Machine-local sensitive data is not durable product intent, and an arbitrary repository-local folder can overlap game or tracked documentation | Change `runtime_root` only to another proper descendant of `.kit/`; derive dispatch exclusion from that same immutable configuration |
| Human decisions remain tracked; execution state does not | Acceptance needs durability, whereas retries and logs create noise and disclosure risk | Preserve the accepted ledger and keep generated dispatch artifacts private |

## Dispatch and automatic workers

| Current decision | Why | Change deliberately by |
| --- | --- | --- |
| Dispatch is capability-protected, sequential, stop-on-first-failure and replay-safe for one exact approval identity | Re-delivery of the same artifact/amendment/retry target must not duplicate work, while a materially different approval must not inherit an old result; this policy evidence does not authenticate a person's identity | Change decision identity or queue state only with HTTP, crash-recovery and production-shaped integration tests; do not describe the request ID as a general idempotency key |
| The loopback board binds to `127.0.0.1` and validates Host, Origin and an in-memory capability | Localhost is not a trust boundary by itself | Change the HTTP policy and cross-origin rejection tests together |
| Only closed, typed provider adapters are accepted | A configuration file must not become arbitrary command execution | Add an adapter in `tools/providers.py` with exact prerequisite and command-shape tests |
| Approved prompt bytes are sealed and passed on standard input | Prompts must not be shell-interpolated or exposed in process arguments | Preserve byte-parity tests from approval through worker input |
| The Windows Copilot adapter invokes the official package-declared Node entry point | A command-shell shim cannot safely carry untrusted text | Keep prompt text off argv; the current official adapter requires Node.js 24 or newer |
| Automatic Copilot and Codex analysis and dispatch fail closed until repository-scoped host reads are enforceable | Authentication, prompt restrictions and post-run diff checks cannot prove that unrelated host files were unreadable | Keep both interactive workflows and manual evidence handoff available; enable either automatic role only with an OS-isolated adapter and repository-read-boundary tests |
| Automatic workers receive read/edit tools only, with shell, URL, MCP, memory, sub-agent and `.git` writes denied | The worker should be capable only of its approved documentation edit | Expand capabilities only after policy, prompt and integration evidence agree |
| Git metadata is detached before an automatic worker starts | Provider-visible content must not expose credentials, hooks or a writable repository control plane | Restore only the sealed metadata on the trusted host after the provider exits |
| Automatic scope is non-executable kit documentation | Scripts, tests, workflows, control-plane code, game state and rules require interactive maintenance | Change both declared-scope rejection and post-run changed-path enforcement |
| Trusted-host evaluation runs `kit verify --static`; full engine verification remains interactive | Documentation automation must not trigger native engine crashes | Expand automatic scope only with a stronger isolated execution boundary |

## Release and portability

| Current decision | Why | Change deliberately by |
| --- | --- | --- |
| Releases use a closed reviewed allowlist and require every fixed kit surface | Adding or deleting a repository file must not silently change the product | Update `tools/release.py`, leak tests and required-surface tests together |
| Release `ARCHITECTURE.md` is a canonical empty template, not the source project's generated graph | Architecture remains a required workflow surface without disclosing active-game module names or edges | Keep exact membership, substitute the fixed template during collection, and verify its exact bytes before smoke or distribution |
| Legal metadata and `VERSION` are mandatory | A distributable without licensing or an identity is not release-ready | Add an approved top-level `LICENSE*` or `COPYING*` and one safe version line |
| Archive paths, member types, links, reparse points, sizes, modes and hashes are checked before extraction | A manifest is trustworthy only if the container cannot redirect or exceed its policy | Change archive limits or member policy with malicious-container fixtures |
| Text is UTF-8 with LF endings; timestamps and modes are normalized; ZIP and gzip use canonical stored blocks | The same reviewed source should produce byte-identical archives across hosts without depending on a host compressor version | Change the normalization metadata, stored-block encoders and deterministic ZIP/TAR tests together |
| Release architecture is a canonical empty graph plus a fixed genre-neutral rule template | Generated graphs and mutable module rules both describe the source project and must not leak into a distributable | Change both canonical architecture constants and their exact-byte/leakage verification tests together |
| Production release build and verification require clean source provenance; inspection may still expose an older archive's dirty marker | Consumers need reproducible commit-bound provenance without leaking a developer's filesystem or presenting uncommitted bytes as a release | Change clean-source enforcement, manifest schema and canonical encoding together |
| Release manifests label receipt trust as portable policy, never person authentication | Project proposal/shape state and private local receipts are intentionally excluded, so an archive can prove its reviewable policy but cannot carry a human identity claim | Require `receipt_trust: portable-policy` and `portable-policy-audit`; evaluate any present source-project receipt before exclusion, fail on mismatch, and record no project state as not applicable |
| Verification is extraction-free; strict smoke extraction uses new private scratch | Inspection of an untrusted archive must not mutate the filesystem | Keep extraction behind successful verification and an empty explicit workspace |
| CI repeats static, release and launcher evidence on Windows, macOS and Linux | Cross-platform support is a tested property, not an inference from Windows | Change `.github/workflows/ci.yml` and require all platform jobs |
| Building an archive does not publish it | External distribution is a separate human-controlled action | Add publishing only as an explicitly authorized release workflow |

## Agent ownership

| Current decision | Why | Change deliberately by |
| --- | --- | --- |
| Game builder, retrospective and kit builder are separate personas with separate deny lists | The author of work should not silently rewrite the rules or assessment that judge it | Change `AGENTS.md` and all three persona contracts together |
| Agents never use destructive Git commands | Existing human work and recoverable history outrank convenience | A human may choose a recovery operation outside the agent workflow |
| Engine-generated UIDs and import sidecars are never invented or hand-edited | Fabricated identity corrupts Godot resource references | Generate them with the engine during an explicitly approved editor/import action |
| Game logic defaults to typed `RefCounted` classes with thin node wiring | The core behavior remains headlessly testable and project structure stays legible | Deliberately revise `arch.rules.json`, `docs/RULES.md` and architecture tests |
