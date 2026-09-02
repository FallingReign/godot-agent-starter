# AGENTS.md

## First action, every session — no exceptions

Resolve `kit` to the repository launcher (`.\kit.cmd` on Windows, `./kit` on
macOS/Linux). Python entry points are internal implementation details.

```text
kit doctor --json
```

Before answering anything about this project, and before any edit. `doctor` is
offline and read-only: it never installs, downloads, imports, formats, edits
user/game files, changes Git state or calls a model.

- `complete: false` → run `kit setup repair` only for safe repo-local
  repairs, then follow `SETUP.md`. Network, editor, Git, import, formatting,
  project-name and import-profile changes each have an explicit opt-in flag;
  never infer consent for one from a setup request.
- `complete: true` → done. Do not re-run or mention it.

**If setup is incomplete, say so before you ask anything.** Acknowledge the person,
name the specific missing prerequisite or decision, estimate the work, and state
which explicit side effect (if any) needs their approval. A cold question as the
opening line reads as an interrogation, and an unexplained setup command hides risk.

Then run `kit verify --static` before any code. Use full `kit verify` when the
native boundary is known safe. After a native crash, timeout or unresolved
engine failure, static verification and `kit self-test` are the only automatic
proofs allowed; ask for explicit human approval before one bounded full retry.
Bind that retry to the exact warning digest shown by doctor/cockpit with
`kit verify --confirm-native-retry <warning_sha256>` (or add `--strict`). Never
reuse, shorten or guess the digest; the private authorization is run-bound.
Nothing is complete until full verification passes and you have pasted the
result. Use `kit verify --strict` for release or production evidence; a missing
dependency is not proof.

## Layout

Use three roots and keep their meanings fixed:

- `PROJECT_ROOT` owns the game project, design, proposal, retrospective state,
  project decisions and private runtime.
- `CORE_ROOT` owns the active kit rules, kit documentation, tools and skills.
- `GAME_ROOT` owns the files Godot loads and the agent may change as game work.

In a managed install, the project-level managed block defines all three roots;
do not replace `PROJECT_ROOT` with the marker inside the active release. In a
flat source checkout, `PROJECT_ROOT` and `CORE_ROOT` are the nearest ancestor
containing `.agent-kit.json`. `GAME_ROOT` is `PROJECT_ROOT` joined to the exact
`game_root` in `kit.config.json`. That file also declares the private
`runtime_root`, providers and dispatch ownership. Resolve those values; do not
infer any root from a game file or the current working directory. Paths in
`arch.rules.json` and `proposal.json` are relative to `GAME_ROOT`.

Read `docs/RULES.md`, `docs/GATE.md`, `docs/WORKFLOW.md`, `docs/DESIGN.md`,
`docs/GDSCRIPT.md`, `docs/SCENES.md`, `docs/LIFECYCLE.md`, tools and skills from
`CORE_ROOT`. Read `docs/design/`, `project.shape.json`, `proposal.json`, project
decision state, and retrospective evidence from `PROJECT_ROOT`.

`.kit/runtime/` is private, machine-local and release-excluded. It may contain
session evidence, prompts and logs. Never cite it as durable product intent,
commit it, or copy it into documentation.

Before writing `proposal.json` or `project.shape.json`, run `kit schema describe
proposal` (or `shape`) and use exactly those keys.
Every key is validated by the `schema` stage. An invented key is rejected with
the correct name, rather than being silently discarded by every reader.
GDScript the engine runs must be inside the configured game root for `res://` to
reach it.

## Project

Godot **4.7.2**, GDScript only. No C#, no GDExtension. Genre-agnostic: nothing assumes
multiplayer, 3D or procedural content. Choices made: `docs/DECISIONS.md`.

**Read `project.shape.json` before designing anything.** Three tenses in one file:
`decisions[]` settled (append-only), `direction[]` what today's work must accommodate,
`questions[]` raised and unanswered. Not a spec to fill in. Onboarding asks three things
only: the name, one or two sentences on what the game is, and `involvement`.

**Before inventing a format that persists content** — save files, level or map documents,
item definitions — read `direction[]` for what the document shape must leave room for.
Adding a field later is cheap; restructuring a document that players have authored is not.
Leave the room, implement only today's fields. If `direction[]` is empty, ask what it will
eventually hold. Skill: `godot-content-pipeline`.

**`plan.html` is the living view** of all of this: what is approved, what is built against
it, open questions, direction, recent changes, the real module graph, and the bounded
kit/design documents relevant to the active decision rendered inline. `kit plan` regenerates
it and `retro.html` coherently. Point the
human at it rather than describing state in prose. It is generated — never edit it, edit
the JSON.

**Two kinds of documentation, and they do not mix.** `docs/` documents the kit: the gate,
the rules, the workflow. `docs/design/` documents the game: intended player experience,
outcomes and gameplay rules code must preserve. It never prescribes architecture or other
technical implementation unless that implementation is itself a gameplay rule. **How
design works — resolution, authority, why it precedes code,
where sections come from — is `docs/DESIGN.md`. Read it before writing or citing any
design section.**

Resolution and authority are separate. Every new or changed design section declares
`Resolution`, `Authority`, `Authored by` and `Confidence`. Human-confirmed design can have
been written by either a human or an agent. Agent-written design remains
`agent-provisional` until the human confirms its exact digest. It must show a Quick read,
the inference rationale, assumptions, veto scope and next go/no-go. Nothing silently
inherits authority from prose, chat, age or a previous slice.

When a `direction[]` entry or decision becomes readable design, draft the section. An
explicit cockpit confirmation appends a capability-protected local operator policy event
with `docs_at`, the section's canonical `design_sha256`, the exact `proposal_sha256`, and
`authority_action: confirm`. Its portable receipt is tamper-evident policy/audit evidence,
not proof that a person's identity was authenticated; only a matching private receipt may
be labelled `local-audit-matched`. A veto appends a superseding
`authority_action: veto` event.
Nothing is deleted from `decisions[]` — the plan view may stop showing it, the ledger keeps
the exact approval and withdrawal events.

**`involvement` sets what you must propose before writing code.** The gate prints it every
run. `hands-off` — record the plan with coarse game-root-relative file or directory
`scope[]` boundaries and the exact `new`, `modify` or `delete` action each boundary
owns, then build only inside them while it remains reversibly vetoable. A directory
boundary owns only matching actions; use a more-specific boundary for a different
action. `(root)` means the configured game root and is the least-specific boundary.
`module` — propose modules, boundaries and the data
crossing them, then wait. `file` — that plus every file you create or modify. `function` —
that plus every changed function's typed canonical signature and purpose. Module
dependencies record how components connect. It never relaxes a gate check.

**Before you write a proposal, read `docs/design/INDEX.md` and ask one question: what end
state does this work serve?** Not the feature above it — the end state. "The interface must
respond" is true and constrains nothing; "every accepted action acknowledges immediately
and preserves enough context for the player to understand its result" tells you what to
build. Start at the index and open only the two or three sections
that matter; never load the whole design body. Put the section you land on in `design_refs`
with one line on why and its canonical `sha256`. If nothing answers it, that is the signal:
enter discovery. Do not build without first writing the missing design. Skills:
`godot-design-retrieval`, `godot-design-discovery`.

**No design reference means no implementation authority.** A legacy
`no-design-refs` acknowledgement is history, not consent to guess intent. Work with the
human on the missing design unless you can explain why one inferred design is correct with
exactly `very-high` confidence. In that exceptional case, write it as
`agent-provisional`, disclose that you wrote it, and record the inference, assumptions,
veto scope and next go/no-go. Lower confidence stops for the human.

**When nobody has asked for anything specific, the next work is derivable.** The index has a
"Ready to work on" list, derived from section resolution and which declared tunables no code
claims yet. Offer candidates; do not silently pick one. Design declares which values are
adjustable and their sensible range; code claims each with `## @tune <id>` above the
declaration. Design never contains a path, and never contains a value the code also
contains.

At `module` and above, **propose in the file, not in the chat.** Order matters: write
`experience` first — what the player does, how it should feel, camera, controls, and in
`not_this` the nearest thing it deliberately is not. Then a `mockup` as inline SVG **if the
outcome is vector-approximable** (grids, shapes, layout, UI arrangement); if it is not, say
why in `mockup.not_possible`. Then `design_authority`, the `reversibility` envelope, and
the structure. Start with `"status": "draft"` and `baseline_sha` from
`git rev-parse HEAD`, run `kit serve start`, and give the human a **clickable link** using
the exact loopback `plan.html` URL printed by that command. A `file://` page is deliberately
read-only and cannot approve anything. Two or three lines of summary at most.

At `hands-off`, declare coarse game-root-relative file or directory `scope[]` with the
exact action each boundary owns, change the status to `recorded`, and continue only while
all authored changes remain inside that scope and reversible. `recorded` is an audit
state, never approval. Agent-provisional design is eligible only at exactly `very-high`
confidence. At `module`, `file` or `function`, wait;
only an explicit human cockpit action may set `approved_by`, `approved_on`,
`approval_sha256` and `status: approved`, and append the exact confirmation event. Never
hand-fill approval fields or infer consent from silence. At any involvement level, stop at
`reversibility.state: go-no-go`; the human must confirm the exact design and proposal
digests before a hard-to-undo commitment.

**Experience before structure is not optional.** Every other artefact here describes
structure; nothing else records intended feel, so without it feel gets inferred from
whatever the renderer happens to do. `conformance` fails on a proposal that has structure
and no `experience`.

`conformance` diffs every authored game input in `recorded` and `approved` proposals. A
draft authorizes no implementation file.
Deviating mid-build is expected and legitimate; deviating *silently* is not — append to
`revisions`, set `status` back to `draft`, regenerate, and either re-record a still
reversible hands-off plan or ask again. Never set `approved` yourself. Skill:
`godot-human-involvement`.

Everything else is recorded **when it is decided**, not predicted. Three rules govern how
work is scoped and captured:

1. **Raise a fork, do not pick it.** Whenever you would choose between paths leading to
   materially different code, content or scope, and nothing in the log or the repo decides
   it, ask — one question, concrete options, grounded in the work in front of you. Then
   record the answer. If you cannot state the question precisely yet, it is not yet a
   question: leave it alone.
1b. **Resolve ambiguity before proposing, one question at a time.** A request that can be
   satisfied two materially different ways is not a request yet. Do not average the
   readings, do not pick the one that is easier to build, and do not bundle five questions
   into one message. Ask the single question that eliminates the most possibilities, wait,
   then ask the next. Stop when you could describe the outcome to the human and they would
   recognise it. Skill: `godot-resolving-ambiguity`.
2. **Build the thinnest thing that runs and can be looked at.** Not the thinnest layer —
   a data format with tests and no way to see it is the wrong increment. One question per
   increment, stated before you start. Asked for one thing? Deliver one thing and stop;
   four working additions destroy the information the one was for. Skill:
   `godot-increment-size`.
3. **Append what gets settled in passing.** Most direction arrives as an aside, not as an
   answer to a question you asked. A constraint, a scope cut, a preference, a reason. If
   it would change later work, record it in the human's own words before moving on.
4. **Never present an inference as human intent.** A section may exist as a question with no
   answer. Ordinarily, if you are inventing the answer, ask. Only an exact `very-high`
   confidence inference may be written as agent-authored, agent-provisional design, with
   the required disclosure and veto boundary. It never becomes human-confirmed by age,
   implementation or silence.
5. **Design is a separate pipeline from delivery.** When the human describes an idea or
   vision, switch explicitly to discovery and write design, not code. Delivery may resume
   only after that design is human-confirmed, or while an exact `very-high`
   agent-provisional design remains inside a recorded reversible envelope. Skill:
   `godot-design-discovery`.

A decision is anything that would make a later agent act differently — art direction,
audience, scope, monetisation, moderation, accessibility, team shape, as much as entity
counts or netcode. No category list. Never write `unknown` or a placeholder as an answer:
an undecided question is an absent entry, a fabricated one makes every later agent trust
a guess. Skill: `godot-project-decisions`.

It informs recommendations. It never relaxes a gate check.

## Knowledge: retrieve, do not recall

**Your Godot training data is out of date.** The 3.x to 4.x transition renamed or
removed much of the API and 4.x keeps changing it. Do not write an engine API call
from memory — look it up first:

```
kit godot-docs build                   # once, dumps the reference for THIS engine
kit godot-docs show Color.from_string
kit godot-docs search from_str
```

That reference is generated by the installed engine binary, so it cannot be stale.
Prefer it over web docs, which serve whichever version the URL names. For
conceptual material with no API equivalent, use the official online documentation
and confirm the page matches this project's engine version.

**On-demand skills live in `.agents/skills/`.** Load one when you are unsure —
that is what they are for. Do not guess in a domain a skill covers.

| Skill | Load it when |
| --- | --- |
| `godot-resolving-ambiguity` | a request could mean two different things, or you are about to guess at feel, movement, camera or scope |
| `godot-project-decisions` | a design or direction choice just got settled, or you need one that is not written down |
| `godot-design-discovery` | the human describes an idea or vision, or you cannot say *why* this work exists |
| `godot-design-sections` | writing or updating anything in `docs/design/`, or citing the design a slice descends from |
| `godot-design-retrieval` | finding the relevant design, deciding what to work on next, or binding a tunable to code |
| `godot-human-involvement` | before writing any new module, class or system — check what must be proposed first |
| `godot-increment-size` | planning work, breaking down a feature, or deciding what to build first |
| `godot-tooling-friction` | about to hand-author content, or you have retried the same mechanical edit several times |
| `godot-retrospective` | a slice finished, the gate says a retro is warranted, or the human reports friction |
| `godot-api-lookup` | unsure a class, method or signal exists, or what it takes |
| `godot-typed-data-boundary` | parsing JSON / save files / content, or any `Variant` error |
| `godot-node-or-resource` | deciding node vs resource vs plain class vs autoload |
| `godot-lifecycle-and-signals` | `_ready`, `_process`, signals, `await`, node refs |
| `godot-scene-files` | before writing or editing any `.tscn` / `.tres` |
| `godot-content-pipeline` | adding assets, or designing a level / save / UGC format |
| `godot-headless-verification` | writing tests, or interpreting a headless run |
| `godot-data-layout` | storing many things (items, entities, modifiers), or ECS / DOD / pooling proposed |
| `godot-performance-evidence` | before optimising, or if C++ / ECS is proposed |
| `godot-multiplayer-authority` | any networked code (delete if single-player) |

Failing gate stages name the relevant skill. Load it rather than guessing again.

## Which agent are you

This repo has three agents. `AGENTS.md` is the shared layer every one of them
reads. Personas live in `.github/agents/` and stack on top of it.

| Agent | Invoked | Owns | Must not touch |
|---|---|---|---|
| `game-builder` | you, per slice | configured game root, `docs/design/`, `proposal.json` | gate files, rules, skills |
| `retrospective` | when slice notes reach the threshold | `docs/retro/` only | everything else |
| `kit-builder` | you, after promoting a finding | gate files, rules, skills, docs | `src/` |

If you were invoked without a persona, you are the game builder.

The permission split is the point. The builder is denied the rules it is judged
against; the retrospective's entire output is proposals about those rules; the kit
builder is the only one allowed to change them. That is three deny lists, not one
agent with three moods.

At the end of a slice the game builder writes a note to `docs/retro/notes/`.
Testimony, not assessment: what was proposed, what changed, what the human
corrected, what it got wrong, where it hit friction. No conclusions and no kit
proposals - a single slice cannot show a pattern. If a closed consequence or
prompt trigger in `docs/retro/notes/README.md` was directly observed, include
its exact `consequence:` or `retro_trigger:` declaration. Never classify prose
or invent severity.

## Non-negotiables

1. **Never ask the human to run a command you could run yourself.** Ask for
   authorization before an explicit network, editor, Git or game mutation, then
   perform and verify the authorized action yourself. Installing Godot may still
   need a person.
2. **Never edit a human-owned file** (below).
3. **Never modify the `[debug]` warnings block in the configured game root's
   `project.godot`.** Fix the code.
4. **Never edit the gate to make a failure go away.** If `integrity` fails, stop and
   report. `--accept-gate-changes` is human-only.
5. **Never trust a Godot exit code.** The gate greps output; do the same.
6. **Never invent a `uid://` value.** If you did not read it from a file, do not write it.
7. **Never run a destructive git command.** No `git reset --hard`, no `git checkout --`,
   no `git clean`, no force push. They silently destroy work you cannot recover. To undo
   your own edit, rewrite the file. If you believe a reset is needed, stop and ask.
8. **Never delegate a gate failure to a sub-agent.** The fix usually depends on repo
   context a fresh sub-agent does not have, so it re-derives a wrong answer repeatedly.
   Fix gate failures in this context, loading whichever skill the stage names.

## Tests that expect errors

A test asserting bad input is **rejected** legitimately prints error output, and the gut
stage fails on error text by default. Do not delete the `push_error` to make the gate
green — a parser that fails silently is a worse design than one the gate complained about.
Declare the expected line in `<game_root>/tests/expected_errors.json` with a
`why`. Anything undeclared still fails.

## When the gate keeps failing

**A falling error count means you are converging. Keep going.** Errors are grouped with
every `file:line` listed — fix the first one at its reported location, re-run, repeat.
Never re-read a whole file hunting for an error the gate already located. If the count
rises, revert that one change. If it is unchanged after three attempts at the same error,
stop and report the error text plus what you tried — do not restart, reset or delete
work, and never claim "the repo is in a bad state" without naming the error.

## Ownership

**Yours:** everything under the configured game root, plus `docs/**` and `*.md`,
except the provider bridge `.github/copilot-instructions.md`.
`*.tscn`/`*.tres` too, but
read `docs/SCENES.md` first. Assets: drop in, then `kit verify --stage import`.

**Never touch:** `check.py`, `arch.py`, `sanitise.py`, `*.rules.json` (the gate,
hash-checked) · `.gate.sha256` (human-only) · the game root's `project.godot`
(engine-owned; the gate
reports changes but does not fail) · `*.import`, `*.uid` (machine-generated — commit,
never edit) · `plan.html`, `plan/*.html` (generated — edit the JSON) ·
`export_presets.cfg` · the configured game root's `addons/**` ·
`.github/copilot-instructions.md` (provider governance bridge).

Full table and permanent human-only work: **`docs/RULES.md`**.
## The core architecture rule

**Game logic lives in plain `RefCounted` classes under `scripts/logic/` inside
the configured game root, with no scene,
node or autoload dependency.** Nodes are a thin layer that reads input and displays
results. Logic you can verify headlessly; node code you cannot verify at all. Push as
much as possible across that line. Config is a typed data object under `scripts/data/`,
constructed once and passed in — no config autoload, and no autoload reference from
`logic/` or `data/`. Gate-enforced. Paths under `scripts/logic/` and
`scripts/data/` are always relative to the configured game root.

**The type boundary runs the same direction.** External data is `Variant`, and GDScript
has no safe cast from `Variant`. Convert it **once** in `scripts/data/` into typed
objects, then pass only typed values inward — `scripts/logic/` never parses external
data and never has a bare `Dictionary`, `Array` or `Variant` in a signature.
Gate-enforced. Adding casts at use sites does not converge. Before writing any
data-loading code: skill `godot-typed-data-boundary`, and `docs/GDSCRIPT.md`.

## Style, gate-enforced

Explicit types everywhere — `:=` is banned, write `var x: float = 3.0`. Every signature
needs parameter types and a return type including `-> void`. `gdformat` decides
formatting. Private members prefixed `_`. Declare in gdlint order: signals, enums,
constants, static vars, vars, `_init`, methods.

The one error no tool catches — it parses in Godot 4 and **never fires**:

```gdscript
connect("pressed", self, "_on_pressed")   # wrong, silently dead
button.pressed.connect(_on_pressed)       # correct
```

15 more Godot 3 idioms are banned and grepped: **`docs/RULES.md`**.

## Gate

```text
kit verify                  # public full verification
kit verify --strict         # release/production proof; no unsupported SKIP
kit verify --static         # all non-engine diagnostics; never completion proof
kit verify --stage typecheck # low-level stage diagnostic
kit setup format            # explicit game-format mutation
```

Stages, remediation and engine quirks: **`docs/GATE.md`**.

## Definition of done

1. `kit verify` passes, output pasted. Release claims require
   `kit verify --strict`.
2. Only agent-owned files changed.
3. New logic-layer code has GUT tests, named `test_<name>.gd` under the
   configured game root's `tests/unit/` — the `tests` stage fails on a test file
   the runner would silently ignore.
4. `kit architecture update` re-run if module dependencies changed. At `involvement`
   `module` or above, report which modules and edges differ from what was approved —
   the diff is the human's check, not the picture.
5. **Anything you cannot verify called out explicitly** — anything visual, feel, timing,
   composition, animation graphs, tilesets, shaders by eye, bakes, export presets,
   real-latency networking, device testing
6. **Launch the game when you stop for visual judgement, only when the native boundary is
   safe.** If an unresolved crash, timeout or another engine verification is running, do not
   launch; report the named blocker and obtain approval for one bounded retry. Otherwise,
   if the increment changed anything the human has to look at, run it before handing back
   — do not ask them to launch it themselves. The gate proves it loads; only they can say
   whether it is right. A passing `resources` stage proves a scene loads, not that it looks
   right. Never claim a game "is running" from a headless run.
7. **At a slice boundary, run `kit friction`.** If a file was rewritten many
   times, or the proposal needed several revisions, say so and ask whether a tool should
   exist. It only sees committed history, so anything you retried inside one turn is
   yours to report — you are the only observer of it. Skill: `godot-tooling-friction`.
8. **When the gate says a retro is warranted, inspect `kit retro
   status`.** Capture is deterministic and calls no model. Analyzer and worker
   providers default to `manual`; `kit retro run` prepares evidence
   without a model, and an automatic analyzer additionally requires
   `--confirm-spend`. `kit retro publish` validates a completed findings report,
   builds dispatch artifacts and refreshes the decision views. Findings must cite the immutable,
   repository-scoped snapshot used to produce them. A human reviews the proposed
   change and success measure before approval; dispatch is capability-protected,
   sequential, replay-safe for the exact decision identity and verified against
   the configured ownership policy. It does not authenticate a person's identity.
   Write findings to `docs/retro/`; never change a rule, skill or gate file
   because of your own retro. The whole loop is `docs/retro/README.md`. Skill:
   `godot-retrospective`.
9. **For a distributable, build the kit rather than copying the repository.**
   `kit release build <archive>` uses a closed allowlist and excludes
   the game, current design/proposal/retro state and `.kit/runtime/`. Release
   requires version and legal metadata and verifies the archive it wrote.
