# Godot Agent Starter

A reusable, cross-platform control kit for agent-assisted Godot 4.7.2 projects.
It keeps planning, verification, retrospective evidence and maintenance decisions
auditable while leaving the game itself project-owned.

The kit supports GDScript projects whose `project.godot` is either at the
repository root or under `src/`. It does not assume a genre, multiplayer, 2D or
3D, procedural content, or a custom persistence format.

## Safe start

Use `\.\kit.cmd` on Windows or `./kit` on macOS/Linux. The examples use `kit`
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
process starts. Full verification still confirms the engine's own version output.

If readiness is complete:

```text
kit verify
```

If the native engine is unsafe or unavailable:

```text
kit verify --static
kit self-test
```

`verify --static` runs the project gate's non-engine stages. `self-test` runs the
kit control-plane regression suite with native-engine execution explicitly
disabled. Neither is a substitute for full release evidence.

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
| `kit plan` | Regenerate `plan.html` and `retro.html` | Deterministic local file generation |
| `kit serve` | Start or reuse the authenticated loopback board | Local server on `127.0.0.1` only |
| `kit retro status` | Report whether retrospective work is due | Deterministic; no model call |
| `kit retro run` | Prepare evidence with the configured analyzer | Manual by default |
| `kit retro run --confirm-spend` | Invoke an automatic analyzer | Explicit external-provider action |
| `kit retro publish` | Validate findings and refresh decision views | No worker dispatch |
| `kit release build PATH` | Build and verify a sanitized archive | Local archive only; never publishes |

All commands accept `--project PATH` and `--json`. A kit root is the nearest
ancestor containing `.agent-kit.json`; layout and private runtime paths come from
`kit.config.json`, never from the current directory by inference.

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

- `plan.html` opens with the current outcome, what is actually true, the latest
  evidence and the next decision. Approved structure, open questions, direction,
  recent changes, architecture and source documents remain available under
  progressive disclosure.
- `retro.html` opens each finding as problem, recommended change, success measure
  and decision. Evidence, scoring, hashes and worker detail are secondary.

The pages are views, not state. Edit the durable JSON or Markdown source and
regenerate them; never hand-edit generated HTML. They work as local files. Run
`kit serve` only when authenticated decision controls are needed.

The board binds only to `127.0.0.1` and checks the request Host, mutation Origin
and an in-memory capability token. Rendering a page never starts the server.

## Retrospective evidence and dispatch

The public loop is deliberately short:

```text
kit retro status
kit retro run
kit retro publish
kit plan
```

Evidence capture is repository-scoped. Published findings cite immutable
snapshots, while live histories remain replaceable inputs. Injected platform
instructions are filtered from testimony; genuine in-scope human messages,
including an opening task, remain evidence.

Machine-local snapshots, prompts, queues, logs and run state live under the
configured `.kit/runtime/` and never ship. Human decisions remain in the tracked
retrospective ledger.

Analyzer and worker providers default to `manual`. Provider configuration is a
closed typed registry, not a command string. Automatic dispatch is authenticated,
idempotent, sequential and stops at the first failure.

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
`doctor`, `verify --static`, and `self-test` do not launch Godot.

## Private runtime

`.kit/runtime/` may contain sensitive session evidence, sealed prompts, logs,
detached Git metadata and queue state. It is machine-local, untracked and excluded
from releases. Do not cite it as durable product intent, copy it into docs, or
publish it.

## Distributing the kit

```text
kit release build ../godot-agent-kit.zip
kit release inspect ../godot-agent-kit.zip
kit release verify ../godot-agent-kit.zip
```

Release construction uses a closed reviewed allowlist and requires every fixed
kit surface, all canonical agent personas, every shipped skill, `VERSION`, and
non-empty top-level legal metadata (`LICENSE*` or `COPYING*`). It fails if a
required file disappears.

ZIP and TAR output is deterministic: UTF-8/LF text, fixed timestamps, normalized
modes and a canonical manifest containing size and SHA-256 for every member.
Source paths and archive members reject traversal, links and Windows reparse
points. Verification reads without extracting; strict smoke testing extracts only
after verification into new private scratch and starts the public launcher.

The archive contains the kit, not this repository's game or private history.
`src/`, `docs/design/`, `project.shape.json`, `proposal.json`, generated pages,
current retrospective decisions/evidence and `.kit/runtime/` are excluded.
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
| `tools/engine_discovery.py` | Read-only exact-version engine selection shared by diagnosis and verification |
| `.gate.sha256` | Human-controlled gate trust baseline |
| `tools/plan_html.py`, `tools/retro_html.py` | Decision-view generators |
| `tools/session_evidence.py`, `tools/session_digest.py` | Scoped evidence capture |
| `tools/board.py`, `tools/run_result.py`, `tools/providers.py` | Authenticated dispatch control plane |
| `tools/release.py` | Deterministic sanitized release builder/verifier |
| `.agents/skills/` | On-demand agent guidance |
| `.github/agents/`, `.claude/settings.json` | Provider personas and default safety policy |
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
