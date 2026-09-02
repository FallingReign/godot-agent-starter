# Godot Agent Starter

A reusable, cross-platform control kit for agent-assisted Godot 4.7.2 projects.
It keeps planning, verification, retrospective evidence and maintenance decisions
auditable while leaving the game itself project-owned.

The kit supports Godot 4.7.2 GDScript projects whose `project.godot` is at the
project root or in one unique nested game folder. It does not assume a genre,
multiplayer, 2D or 3D, procedural content, or a custom persistence format.

## Safe start

Use `.\kit.cmd` on Windows or `./kit` on macOS/Linux. The examples use `kit`
as the platform-neutral name.

```text
kit doctor --json
```

`doctor` is offline and read-only. It locates prerequisites but never executes
Godot, installs or downloads a dependency, edits files, imports assets, formats
code, changes Git, calls a model, or starts a server.

Engine selection is exact-version aware without executing the binary. A stale
versioned `GODOT_BIN` may fall forward to the supported console binary beside it
or beside the project; otherwise the known mismatch is reported and no native
process starts. Full verification and each explicit non-gate native operation
then authenticate the selected executable's own version output. Import, API-doc
generation and the language server never treat an unversioned PATH entry as
execution authority.

If readiness is complete and the native boundary is known safe, with no
unresolved native failure:

```text
kit verify
```

If the native engine is unsafe, unavailable, or has an unresolved crash:

```text
kit verify --static
kit self-test
```

`verify --static` runs the project gate's non-engine stages. `self-test` runs the
kit control-plane regression suite with native-engine execution explicitly
disabled. Neither resolves an existing crash warning or substitutes for full
release evidence. Resume full verification only after explicit approval for one
bounded native retry.

## Public commands

| Command | Purpose | Important side effect boundary |
| --- | --- | --- |
| `kit doctor` | Report environment and project readiness | Offline and read-only |
| `kit setup ...` | Perform one selected setup action | Each mutation or download is a separate command |
| `kit integrity accept` | Accept reviewed gate changes | Human-only trust action |
| `kit verify` | Run static stages, then bounded engine stages | Starts Godot only after static success |
| `kit verify --static` | Run every non-engine gate stage | Never discovers or starts Godot |
| `kit verify --strict` | Produce production/release evidence | Rejects unsupported skips |
| `kit self-test` | Test the kit control plane | Native engine is forcibly disabled |
| `kit plan [--snapshot [NAME]]` | Regenerate the living views and optionally retain an immutable review snapshot | Deterministic local file generation; no server |
| `kit serve [start\|status\|open\|stop]` | Manage the capability-protected local review cockpit | Explicit local server lifecycle on `127.0.0.1` only |
| `kit retro status` | Report whether retrospective work is due | Deterministic; no model call |
| `kit retro run [--since ISO] [--limit N]` | Prepare complete bounded evidence or an explicit snapshot-bound window | Manual by default |
| `kit retro run --confirm-spend` | Invoke an automatic analyzer | Explicit external-provider action |
| `kit retro publish` | Validate findings, refresh both views and transactionally close the exact evidence snapshot | No worker dispatch |
| `kit release build PATH` | Build and verify a sanitized archive | Local archive only; never publishes |
| `kit install TARGET [--release PATH]` | Review adding the kit to an existing project | No project-facing change before Apply |
| `kit upgrade TARGET [--release PATH]` | Review replacing only managed kit parts | Authenticates existing ownership before Apply |
| `kit recover SESSION_ID` | Continue or restore an interrupted kit change | Uses the exact durable session and journal |

All commands accept `--project PATH` and `--json`. A kit root is the nearest
ancestor containing `.agent-kit.json`; layout and private runtime paths come from
`kit.config.json`, never from the current directory by inference.

## Install and upgrade

Do not copy the kit repository over an existing project. Build a release, then
use the bounded review:

```text
kit release build PATH/agent-kit-0.3.0.zip
kit install PATH/TO/PROJECT --release PATH/agent-kit-0.3.0.zip
kit upgrade PATH/TO/PROJECT --release PATH/agent-kit-0.3.0.zip
```

The target must already be a real Godot 4.7.2 GDScript project with a regular
`project.godot` at the project root or in one unique nested game folder. An empty
folder is refused because the kit never invents or edits `project.godot`. For a
new game, create and close a blank Godot project first, then install the kit.

The kit running the change must be the exact incoming extracted release, or a
clean source checkout that produces that exact release. A mismatch is refused
before any project or review-session file is written. Run the command from the
incoming extracted release, or from its matching clean source checkout.

`install` and `upgrade` print one exact local review link. The review shows what
will be created, changed, preserved, or removed. **Apply** writes a journal and
backup first, changes the active release last, and runs offline kit and project
checks. **Restore** puts exact prior project bytes back. Existing project gaps
remain visible as **Adoption required**; they are not called fixed and do not
authorize a project rewrite.

If work stops between durable steps, run `kit recover FULL_SESSION_ID` from the
same incoming kit. See [Install and upgrade the kit](docs/LIFECYCLE.md) for the
complete short runbook and preservation boundaries.

## Explicit setup

`kit setup repair` is intentionally narrow: it can apply safe repository-local
kit repairs only. Operations with broader effects remain separate:

```text
kit setup dependency gdtoolkit
kit setup dependency gut
kit setup dependency mermaid
kit setup audit-dependencies
kit setup editor
kit setup repository
kit setup import
kit setup format
kit setup layout root
kit setup layout src
kit setup name "My Game"
kit setup profile desktop-3d
```

The dependency operations use exact canonical upstream sources, sizes and SHA-256
hashes from `dependencies.lock.json`. GUT, gdtoolkit and Mermaid are third-party
adapters, not Godot components. `kit setup audit-dependencies` downloads and
authenticates all three direct lock artifacts without installing or replacing
anything. Nothing upgrades itself; local changes and mismatches are surfaced for
review.

Setup never updates `.gate.sha256`. After changing protected kit files, a human
reviews the exact diff and separately runs `kit integrity accept`.

## Planning that supports a decision

`kit plan` produces two generated views:

- `plan.html` opens with a Quick Read of what the player does, experiences and
  should achieve. It shows the exact design authority and digest, whether the
  current work is reversible, the next go/no-go point, verification freshness,
  and the one decision currently needed. Approved structure, open questions,
  direction, recent changes, architecture and source documents remain available
  under progressive disclosure.
- `retro.html` opens each finding as problem, recommended change, success measure
  and decision. Evidence, scoring, hashes and worker detail are secondary.

The pages are views, not state. Edit the durable JSON or Markdown source and
regenerate them; never hand-edit generated HTML. A direct `file://` page is an
explicit read-only view and says whether the cockpit is live, down, or not
applicable. It never pretends its controls are interactive. Use `kit serve` to
start the cockpit, review the printed `http://127.0.0.1:.../plan.html` URL, and
make a decision-bound approval, veto, or change request. The cockpit protects
the local mutation channel and records portable policy evidence; it does not
authenticate a person's identity. Use `kit serve stop`
when that review session is finished. Rendering or opening a local file never
starts the server.

The board binds only to `127.0.0.1` and checks the request Host, mutation Origin
and an in-memory capability token. Rendering a page never starts the server.

## Design authority before implementation

Every implementation proposal cites exact `docs/design/` content and its
canonical SHA-256 digest. Design records player experience, intended outcomes
and gameplay rules; it does not prescribe architecture. If the needed design is
missing, implementation stops for discovery instead of guessing intent.
An absent `proposal.json` is only a clean planning state: conformance compares
the game root to Git and blocks any authored addition, modification or deletion
that has no recorded design-backed envelope.

An agent may write provisional design only when its confidence is exactly
`very-high`. The section must disclose agent authorship, why the inference was
made, its assumptions, an ADHD-friendly Quick Read, the part that can still be
vetoed, and the next go/no-go point. Hands-off work may continue only inside
recorded coarse file or directory boundaries while the proposal is explicitly
reversible. The cockpit binds an operator policy event to the exact design and
proposal digest; silence, an old approval, or approval of a different plan never
counts. Its portable receipt is tamper-evident policy evidence, not proof that a
particular person's identity was authenticated.

Approval depth is literal. Module review binds complete allowed outgoing
dependencies and boundary data; file review includes new, modified and deleted
authored inputs; function review additionally binds canonical typed GDScript
signatures and normalized body changes at the exact Git baseline. Authored scope
is inverse, so custom/extensionless content and project tests remain visible
unless they are a known generated, private, engine-owned or third-party surface.

## Copilot and Codex

Repository-wide governance lives in `AGENTS.md`. GitHub Copilot receives a
provider bridge in `.github/copilot-instructions.md`; Codex reads `AGENTS.md`
directly. Both surfaces point to the same schemas, design authority, reversible
envelope, gate and retrospective rules rather than maintaining separate policy.

Interactive Copilot and Codex development are supported. Repository-scoped
history evidence is collected for both without retaining assistant responses,
tool bodies, reasoning, environment values or raw command output. Copilot AIU
usage and Codex token categories remain separate, and the kit reports monetary
cost as unknown unless a provider supplies an authoritative value.

Automatic Copilot and Codex analysis or worker dispatch both fail closed for
now: neither available host can prove that unrelated files are unreadable at the
OS boundary. Interactive use and the manual evidence handoff remain supported.
Authentication and spend confirmation are still required, but neither is a
substitute for isolation; the kit will not claim a boundary it cannot enforce.

## Retrospective evidence and dispatch

The public loop is deliberately short:

```text
kit retro status
kit retro run
kit retro publish
```

With no window flags, `retro run` captures every matching repository session up
to the declared hard session, byte and record limits. `--since` and `--limit`
are explicit operator cuts and are stored in the immutable session snapshot;
there is no implicit newest-three window.

Evidence capture is repository-scoped. Published findings cite immutable
snapshots, while live histories remain replaceable inputs. Injected platform
instructions are filtered from testimony; genuine in-scope human messages,
including an opening task, remain evidence. Matching session count, event bytes
and parsed records have explicit completeness limits. Exceeding one blocks that
capture with the observed and allowed values; it never silently presents a
newest-only subset as the complete history.
If any selected Copilot or Codex source cannot be re-authenticated or completely
captured, the private pack names the failed session and reason, but no analyzer
handoff is prepared and `retro publish` refuses findings bound to that pack.

The stored field set is privacy-minimised, not a secret-scanner guarantee.
Assistant/tool/reasoning/raw-output bodies are omitted, and high-confidence
credentials and machine-local paths in retained human testimony are redacted on
a best-effort basis. Human messages can still contain sensitive material, so
`.kit/runtime/` must be protected accordingly and never published.

`retro publish` is the single manual publication boundary: it ranks one resolved
report, refreshes both decision views, then archives exactly the captured note
bytes and exposes the completion marker. Any validation, rendering, report-drift
or marker failure leaves no false completion and restores moved notes.

Retrospective urgency is consequence-based, not merely note-count based. A
native crash, false green, unauthorized provider use, irreversible unapproved
change or destroyed work becomes an immediate warning. Repeated correction,
wrong output and repeated verification failure become prompt-level warnings.
Exact trigger codes come from slice testimony or the verification ledger, so a
static follow-up cannot silently erase an unresolved native crash.

Machine-local snapshots, prompts, queues, logs and run state live below the
configured `.kit/` private container (normally `.kit/runtime/`) and never ship.
Human decisions remain in the tracked
retrospective ledger.

Analyzer and worker providers default to `manual`. Provider configuration is a
closed typed registry, not a command string. Automatic dispatch is
capability-protected, sequential and stops at the first failure. Replay of the
same exact reviewed artifact, amendment and retry target returns the original
durable decision/run;
a different approval identity is a conflict or an explicit retry, not a blanket
exactly-once guarantee.

Automatic workers are limited to an approved non-executable documentation scope.
The exact approved prompt is sealed and sent over standard input, never a shell
or process argument. Git metadata is detached before provider access. A trusted
host restores it, checks the changed paths, runs `kit verify --static`, and
integrates only verified work. Scripts, tests, workflows, rules, control-plane
files, game state and design require an interactive maintainer.

## Engine crash containment

The verification order is intentional:

1. Integrity runs first and fails fast.
2. Every static stage runs before native-engine discovery.
3. One engine health check guards the native stage group.
4. A failed health check or native stage suppresses every later native stage.

This prevents one native fault from generating a cascade of crash dialogs.
Windows verification prefers the console binary, suppresses modal Application
Error UI, owns a single process tree and terminates that tree on timeout. Native
access violations, bounded timeouts and refused concurrent launches are written
to the private verification ledger and surfaced in the cockpit and
retrospective status. An unresolved native crash remains visible across static
checks until a later complete native verification succeeds. `doctor`,
`verify --static`, and `self-test` do not launch Godot.

## Private runtime

`.kit/runtime/` may contain sensitive session evidence, sealed prompts, logs,
detached Git metadata and queue state. It is machine-local, untracked and excluded
from releases. Do not cite it as durable product intent, copy it into docs, or
publish it. A configured `runtime_root` must remain a proper descendant of
`.kit/`; repository-local paths elsewhere are rejected rather than treated as
private. Existing path components must be ordinary unredirected directories;
links, junctions and a `.kit` file fail closed.

## Distributing the kit

```text
kit release build ../godot-agent-kit.zip
kit release inspect ../godot-agent-kit.zip
kit release verify ../godot-agent-kit.zip
```

Release construction uses a closed reviewed allowlist and requires every fixed
kit surface, all canonical agent personas, every shipped skill, `VERSION`, and
non-empty top-level legal metadata (`LICENSE*` or `COPYING*`). It fails if a
required file disappears or if the source repository is dirty. `release inspect`
can expose the provenance of an older dirty-marked archive for
diagnosis, but production verification rejects it.

ZIP and TAR output is deterministic: UTF-8/LF text, fixed timestamps, normalized
modes and a canonical manifest containing size and SHA-256 for every member.
ZIP entries and gzip streams use canonical stored blocks rather than a host's
compressor heuristics, so zlib/library versions cannot change archive bytes.
The manifest labels archive authority evidence as `receipt_trust:
portable-policy` with the `portable-policy-audit` identity model. That label
means the archive satisfies the reviewable policy carried inside it; it never
claims a person's identity was authenticated. If proposal and shape state are
present in the source checkout, build records their exact pre-exclusion receipt
trust and refuses a contradictory local receipt. Clean kits with no project
state record `not-applicable-no-project-state`.
Source paths and archive members reject traversal, links and Windows reparse
points. Verification reads without extracting; strict smoke testing extracts only
after verification into new private scratch and starts the public launcher.

The archive contains the kit, not this repository's game or private history.
`src/`, `docs/design/`, `project.shape.json`, `proposal.json`, generated pages,
current retrospective decisions/evidence and `.kit/runtime/` are excluded.
The required `ARCHITECTURE.md` member is replaced with a canonical empty graph,
and `arch.rules.json` is replaced with a fixed genre-neutral starting policy.
Running `kit architecture update` in the destination derives the graph from that
project, so source-project module names, descriptions and edges never ship.
Building an archive never publishes it.

CI repeats control-plane, strict engine and release evidence on Windows, macOS
and Linux using official GitHub actions pinned to immutable commits and an
official Godot archive authenticated against its published checksum list.

## Project architecture contract

The default game architecture keeps typed logic in plain `RefCounted` classes
under the configured game root's `scripts/logic/`, typed external-data conversion
under `scripts/data/`, and nodes as thin input/display wiring. Module boundaries
come from `arch.rules.json`; `ARCHITECTURE.md` is generated from real code.

The defaults are deliberately revisable for a project, but changes are explicit:
update the rules, run `kit architecture update`, review the graph, and verify.
Project-specific direction and decisions live in `project.shape.json`; game design
lives in `docs/design/`. Neither becomes kit policy.

## Repository map

| Path | Role |
| --- | --- |
| `.agent-kit.json` | Versioned kit-root marker |
| `kit`, `kit.cmd`, `kit.py` | Public launchers and internal router |
| `kit.config.json` | Layout, private runtime, providers and dispatch ownership |
| `dependencies.lock.json` | Exact canonical-upstream third-party provenance |
| `bootstrap.py` | Readiness detector and explicit setup implementation |
| `check.py` | Ordered static/native verification gate |
| `tools/engine_discovery.py` | Read-only selection plus bounded engine-reported version authentication for explicit native operations |
| `.gate.sha256` | Human-controlled gate trust baseline |
| `tools/cockpit.py`, `tools/plan_html.py`, `tools/retro_html.py` | Durable decision/verification state and generated views |
| `tools/session_evidence.py`, `tools/session_digest.py` | Scoped evidence capture |
| `tools/board.py`, `tools/run_result.py`, `tools/providers.py` | Authenticated dispatch control plane |
| `tools/release.py` | Deterministic sanitized release builder/verifier |
| `.agents/skills/` | On-demand agent guidance |
| `.github/copilot-instructions.md`, `.github/agents/`, `AGENTS.md` | Copilot/Codex bridge, personas and shared governance |
| `.github/workflows/ci.yml` | Cross-platform production evidence |
| `.kit/runtime/` | Private machine-local state; never released |

## Documentation

- [SETUP.md](SETUP.md) — safe readiness and explicit setup actions.
- [docs/WORKFLOW.md](docs/WORKFLOW.md) — normal developer and agent loop.
- [docs/GATE.md](docs/GATE.md) — verification stages and remediation.
- [docs/DESIGN.md](docs/DESIGN.md) — project-design placement, resolution, and binding rules.
- [docs/RULES.md](docs/RULES.md) — ownership and enforced architecture rules.
- [docs/DECISIONS.md](docs/DECISIONS.md) — current kit policy and reversal points.
- [docs/retro/README.md](docs/retro/README.md) — retrospective and dispatch lifecycle.
- [VERIFY.md](VERIFY.md) — release acceptance evidence.
- [AGENTS.md](AGENTS.md) — agent contract.

## License

MIT License. Copyright (c) 2026 Justin Fenech. See [LICENSE](LICENSE).
