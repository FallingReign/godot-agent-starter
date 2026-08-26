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
| A kit root is identified by `.agent-kit.json` | Discovery must not infer a project boundary from the working directory or a game file | Change `tools/project_context.py` and every launcher test together |
| `kit.config.json` owns `game_root`, private `runtime_root`, providers and dispatch policy | Project layout and machine-local state must be explicit and portable | Change the schema and migration tests before adding another configuration source |
| Both root-layout and `src/`-layout Godot projects are supported | Adoption must not require moving an existing game | Use `kit setup layout root` or `kit setup layout src`; add a layout only with cross-platform fixtures |
| `src/`, game design, proposals, generated views and retrospective run state never ship | A reusable kit must not disclose or impose the source project's product state | Change the closed release policy and its leak-marker tests |
| Godot 4.7.2 Standard and GDScript are the supported engine boundary | The gate, API lookup and strict-language guidance need one exact testable contract | Update `tools/engine_discovery.py`, `check.py`, `AGENTS.md`, dependency guidance and all authenticated CI archives together |

## Public workflow and setup

| Current decision | Why | Change deliberately by |
| --- | --- | --- |
| `kit` / `kit.cmd` is the only public command surface | Developers should not need to know which internal language or script implements an action | Add a `kit.py` command and document only the launcher form |
| `kit doctor --json` is offline and read-only | Diagnosis must be safe to run before consent and must never trigger the engine crash it is diagnosing | Keep execution, downloads and mutations out of the doctor path; prove the boundary in `test_bootstrap.py` and `test_engine_boundary.py` |
| A clearly stale versioned `GODOT_BIN` may fall forward only to an exact-version official-style sibling or project-adjacent binary | A global variable for another project must not make this kit launch a known-wrong engine, while generic executable names still need the engine's own version probe | Keep selection read-only in `tools/engine_discovery.py`; retain the bounded exact-version check in `check.py` |
| Setup side effects are separate explicit commands | Consent to setup is not consent to network, editor, Git, import, formatting, naming or profile mutation | Add a named `kit setup` action with its own opt-in and mutation test |
| Setup never accepts the gate integrity baseline | A tool must not approve changes to the rules that judge it | Human review ends with `kit integrity accept` |
| Third-party dependencies use exact versions, canonical upstream locations, sizes and SHA-256 hashes | Reproducibility and provenance matter more than opportunistic convenience, and canonical upstream does not mean released by Godot | Review `dependencies.lock.json`, then use the specific opt-in acquisition command |
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
| One engine health check guards the native stage group | A crashing binary must not be launched repeatedly by cascading stages | Change only with a regression test proving a native failure cannot cause a second launch |
| The first failed native stage suppresses later native stages | Repeated process crashes add no diagnostic value and can destabilize the host application | Change `check.py` engine-failure state and `test_engine_boundary.py` together |
| Windows native verification suppresses modal OS error UI and owns timeout cleanup | A crashing engine must return evidence to automation rather than block on an Application Error dialog or leave descendants running | Preserve console-binary preference, inherited error mode, single-launch lock and exact owned-tree termination tests |
| Godot exit codes are not trusted on their own | Godot can exit zero with hard errors and nonzero after successful work | Preserve bounded output scanning and expected-error declarations |
| Integrity runs first and fails fast | A modified gate cannot be used as evidence for itself | Review the protected diff, then have a human run `kit integrity accept` |
| `kit verify --strict` is required for a release claim | Unsupported or skipped evidence is not production proof | Change strict policy only with production-shaped regression evidence |

## Planning and decision experience

| Current decision | Why | Change deliberately by |
| --- | --- | --- |
| `plan.html` and `retro.html` are generated views, never authored state | A presentation must not become a second source of truth | Edit the source JSON or Markdown and regenerate with `kit plan` |
| The plan opens with outcome, truthful state and the next decision | The control surface exists to help a human decide, not to expose implementation telemetry | Change the front-door rendering contract and frontend tests together |
| Full records, hashes, diagrams and diagnostics are progressively disclosed | Evidence must remain available without overwhelming the decision | Keep decision controls visible and move detail behind labelled disclosure |
| Draft proposals do not enforce structure; approved proposals do | Early planning must be cheap, while approval must have an auditable meaning | Change proposal schema, conformance and approval workflow together |
| Only the human approves a proposal or acknowledges missing design | An agent cannot create the consent that authorizes its own work | Preserve human-owned status transitions and acknowledgement text |
| The board is a view and command surface over durable source state | Starting a server must not be required to render or recover the project | Keep generation deterministic and server startup explicit via `kit serve` |

## Retrospective evidence

| Current decision | Why | Change deliberately by |
| --- | --- | --- |
| Session capture is repository-scoped | Conversations from another checkout or project must never support a finding | Change path normalization and repository-matching tests together |
| Evidence snapshots are immutable, content-addressed inputs to findings | A recommendation must remain auditable after live history changes | Publish a new snapshot; never rewrite the snapshot cited by a finding |
| Human messages are evidence; injected instructions and synthetic context are filtered | The retrospective should evaluate the lived workflow, not platform scaffolding | Change evidence classification with fixtures for opening tasks and injected context |
| Source IDs remain stable and pack-local exhibit IDs are explicit | Citations need to survive repacking without pretending global numbering exists | Preserve source IDs in snapshots and render pack-local IDs only within a named pack |
| Deterministic capture and ranking are separate from model analysis | Evidence collection must be repeatable and free of hidden spend | Keep `kit retro status` and manual `kit retro run` model-free |
| Findings lead with problem, recommended change, success measure and decision | Retrospectives should produce an actionable choice, not an activity report | Change `tools/retro_html.py` and frontend decision-contract tests together |
| Transient evidence, queues, prompts and logs live under `.kit/runtime/` | Machine-local sensitive data is not durable product intent | Change `runtime_root` only to another private repository-local path |
| Human decisions remain tracked; execution state does not | Acceptance needs durability, whereas retries and logs create noise and disclosure risk | Preserve the accepted ledger and keep generated dispatch artifacts private |

## Dispatch and automatic workers

| Current decision | Why | Change deliberately by |
| --- | --- | --- |
| Dispatch is authenticated, idempotent, sequential and stops on the first failure | Retrying or overlapping repository mutations must not duplicate or reorder work | Change the queue state machine only with HTTP and production-shaped integration tests |
| The loopback board binds to `127.0.0.1` and validates Host, Origin and an in-memory capability | Localhost is not a trust boundary by itself | Change the HTTP policy and cross-origin rejection tests together |
| Only closed, typed provider adapters are accepted | A configuration file must not become arbitrary command execution | Add an adapter in `tools/providers.py` with exact prerequisite and command-shape tests |
| Approved prompt bytes are sealed and passed on standard input | Prompts must not be shell-interpolated or exposed in process arguments | Preserve byte-parity tests from approval through worker input |
| The Windows Copilot adapter invokes the official package-declared Node entry point | A command-shell shim cannot safely carry untrusted text | Keep prompt text off argv; the current official adapter requires Node.js 24 or newer |
| Automatic workers receive read/edit tools only, with shell, URL, MCP, memory, sub-agent and `.git` writes denied | The worker should be capable only of its approved documentation edit | Expand capabilities only after policy, prompt and integration evidence agree |
| Git metadata is detached before an automatic worker starts | Provider-visible content must not expose credentials, hooks or a writable repository control plane | Restore only the sealed metadata on the trusted host after the provider exits |
| Automatic scope is non-executable kit documentation | Scripts, tests, workflows, control-plane code, game state and rules require interactive maintenance | Change both declared-scope rejection and post-run changed-path enforcement |
| Trusted-host evaluation runs `kit verify --static`; full engine verification remains interactive | Documentation automation must not trigger native engine crashes | Expand automatic scope only with a stronger isolated execution boundary |

## Release and portability

| Current decision | Why | Change deliberately by |
| --- | --- | --- |
| Releases use a closed reviewed allowlist and require every fixed kit surface | Adding or deleting a repository file must not silently change the product | Update `tools/release.py`, leak tests and required-surface tests together |
| Legal metadata and `VERSION` are mandatory | A distributable without licensing or an identity is not release-ready | Add an approved top-level `LICENSE*` or `COPYING*` and one safe version line |
| Archive paths, member types, links, reparse points, sizes, modes and hashes are checked before extraction | A manifest is trustworthy only if the container cannot redirect or exceed its policy | Change archive limits or member policy with malicious-container fixtures |
| Text is UTF-8 with LF endings; timestamps and modes are normalized | The same reviewed source should produce byte-identical archives across hosts | Change the normalization metadata and deterministic ZIP/TAR tests together |
| The archive manifest records source commit and dirty state without absolute local paths | Consumers need provenance without leaking a developer's filesystem | Change manifest schema and canonical encoding together |
| Verification is extraction-free; strict smoke extraction uses new private scratch | Inspection of an untrusted archive must not mutate the filesystem | Keep extraction behind successful verification and an empty explicit workspace |
| CI repeats static, release and launcher evidence on Windows, macOS and Linux | Cross-platform support is a tested property, not an inference from Windows | Change `.github/workflows/ci.yml` and require all platform jobs |
| Building an archive does not publish it | External distribution is a separate human-controlled action | Add publishing only as an explicitly authenticated release workflow |

## Agent ownership

| Current decision | Why | Change deliberately by |
| --- | --- | --- |
| Game builder, retrospective and kit builder are separate personas with separate deny lists | The author of work should not silently rewrite the rules or assessment that judge it | Change `AGENTS.md` and all three persona contracts together |
| Agents never use destructive Git commands | Existing human work and recoverable history outrank convenience | A human may choose a recovery operation outside the agent workflow |
| Engine-generated UIDs and import sidecars are never invented or hand-edited | Fabricated identity corrupts Godot resource references | Generate them with the engine during an explicitly approved editor/import action |
| Game logic defaults to typed `RefCounted` classes with thin node wiring | The core behavior remains headlessly testable and project structure stays legible | Deliberately revise `arch.rules.json`, `docs/RULES.md` and architecture tests |
