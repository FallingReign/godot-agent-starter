# AGENTS.md

## First action, every session — no exceptions

```
python bootstrap.py --json
```

Before answering anything about this project, and before any edit.

- `complete: false` → run `python bootstrap.py --fix`, then `SETUP.md` for anything still
  `MANUAL`. Setup is your job, not the user's.
- `complete: true` → done. Do not re-run or mention it.

**If setup is incomplete, say so before you ask anything.** Acknowledge the person, tell
them this is a fresh project needing one-time setup, say roughly how long it takes and
what you will need from them, then ask the first question. A cold question as the opening
line of a session reads as an interrogation, and the person has no idea how many follow.

Then `python check.py` before any code. Nothing is complete until it passes and you have
pasted the result.

## Layout

**Game code lives in `src/`.** That is the Godot project: `godot --path src`. Everything
outside it is the gate — scripts, docs, rule files, skills — and is not loaded by the
engine. Paths in `arch.rules.json` and `proposal.json` are relative to `src/`.

Before writing `proposal.json` or `project.shape.json`, run `python
tools/schema.py --describe proposal` (or `shape`) and use exactly those keys.
Every key is validated by the `schema` stage. An invented key is rejected with
the correct name, rather than being silently discarded by every reader.
GDScript the engine runs must be inside `src/` for `res://` to reach it.

## Project

Godot **4.7.1**, GDScript only. No C#, no GDExtension. Genre-agnostic: nothing assumes
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
it, open questions, direction, recent changes, the real module graph, and every document
in `docs/` and `docs/design/` rendered inline. The gate regenerates it every run. Point the
human at it rather than describing state in prose. It is generated — never edit it, edit
the JSON.

**Two kinds of documentation, and they do not mix.** `docs/` documents the kit: the gate,
the rules, the workflow. `docs/design/` documents the game: what a cell is, how the world
is structured, what a content format guarantees. **How design works — resolution levels, why
it precedes code, where sections come from — is `docs/design/README.md`. Read it before
writing or citing any design section.**

When a `direction[]` entry or a decision is settled enough to be written for a reader, draft
the section, get it approved, and add a `docs_at` pointer on the decision entry. Nothing is
deleted from `decisions[]` — the plan view stops showing it, the ledger keeps it.

**`involvement` sets what you must propose before writing code.** The gate prints it every
run. `hands-off` — build and report. `module` — propose modules, boundaries and the data
crossing them, then wait. `file` — that plus every file you create or modify. `function` —
that plus signatures and how they connect. It never relaxes a gate check.

**Before you write a proposal, read `docs/design/INDEX.md` and ask one question: what end
state does this work serve?** Not the feature above it — the end state. "The world must be
visible" is true and constrains nothing; "content must be composable from a vocabulary
small enough that a player can author it and a peer can validate it without trusting the
sender" tells you what to build. Start at the index and open only the two or three sections
that matter; never load the whole design body. Put the section you land on in `design_refs`
with one line on why. If nothing answers it, that is the signal: enter discovery and write
the section, rather than reasoning up from the feature. Skills:
`godot-design-retrieval`, `godot-design-discovery`.

**When the plan is ready, say what is missing from it before asking for approval.**
If there are no `design_refs`, say so plainly and offer the choice: design the
section together first, or accept building on an inference. Recommend the first.
If the human accepts the second, record it in `acknowledged[]` with their words as
the reason — **never tick that box on their behalf.** An acknowledgement applies to
one slice; it does not carry forward.

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
why in `mockup.not_possible`. Then the structure. Then `"status": "draft"` and
`baseline_sha` from `git rev-parse HEAD`, run `python tools/plan_html.py`, and give the
human a **clickable link** using the absolute file URI, e.g.
`[Open plan.html](file:///C:/path/to/repo/plan.html)` — do not only name the file. Two or
three lines of summary at most. Wait. On approval set `"status": "approved"`, then build.

**Experience before structure is not optional.** Every other artefact here describes
structure; nothing else records intended feel, so without it feel gets inferred from
whatever the renderer happens to do. `conformance` fails on a proposal that has structure
and no `experience`.

`conformance` only diffs an **approved** proposal, so writing the draft early enforces
nothing and risks nothing. Deviating mid-build is expected and legitimate; deviating
*silently* is not — append to `revisions`, set `status` back to `draft`, regenerate, ask
again. Never set `approved` yourself. Skill: `godot-human-involvement`.

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
4. **Never write past the resolution the human has reached.** A design section may exist
   as a question with no answer. It may not be filled with a plausible answer you inferred
   — that reads as intent to the next agent and gets built on. An empty section is honest;
   a fabricated one is not recoverable. If you are inventing the answer, ask instead.
5. **Design is a separate pipeline from delivery.** When the human describes an idea or
   vision, that is a discovery session: it produces design sections and decisions, no code.
   Say you are switching. The other entry point is a proposal you cannot ground in an end
   state, above. Skill: `godot-design-discovery`.

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
python tools/gddoc.py --build          # once, dumps the class reference for THIS engine
python tools/gddoc.py Color.from_string
python tools/gddoc.py --search from_str
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

## Non-negotiables

1. **Never ask the human to run a command you could run yourself.** Only installing Godot
   needs a person.
2. **Never edit a human-owned file** (below).
3. **Never modify the `[debug]` warnings block in `src/project.godot`.** Fix the code.
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
Declare the expected line in `src/tests/expected_errors.json` with a `why`. Anything
undeclared still fails.

## When the gate keeps failing

**A falling error count means you are converging. Keep going.** Errors are grouped with
every `file:line` listed — fix the first one at its reported location, re-run, repeat.
Never re-read a whole file hunting for an error the gate already located. If the count
rises, revert that one change. If it is unchanged after three attempts at the same error,
stop and report the error text plus what you tried — do not restart, reset or delete
work, and never claim "the repo is in a bad state" without naming the error.

## Ownership

**Yours:** everything under `src/`, plus `docs/**` and `*.md`. `*.tscn`/`*.tres` too, but
read `docs/SCENES.md` first. Assets: drop in, then `python check.py --only import`.

**Never touch:** `check.py`, `arch.py`, `sanitise.py`, `*.rules.json` (the gate,
hash-checked) · `.gate.sha256` (human-only) · `src/project.godot` (engine-owned; the gate
reports changes but does not fail) · `*.import`, `*.uid` (machine-generated — commit,
never edit) · `plan.html`, `plan/*.html` (generated — edit the JSON) · `export_presets.cfg`,
`src/addons/**`.

Full table and permanent human-only work: **`docs/RULES.md`**.
## The core architecture rule

**Game logic lives in plain `RefCounted` classes under `scripts/logic/`, with no scene,
node or autoload dependency.** Nodes are a thin layer that reads input and displays
results. Logic you can verify headlessly; node code you cannot verify at all. Push as
much as possible across that line. Config is a typed data object under `scripts/data/`,
constructed once and passed in — no config autoload, and no autoload reference from
`logic/` or `data/`. Gate-enforced. Examples: `src/scripts/logic/health.gd`,
`src/scripts/data/game_config.gd`.

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

```
python check.py                    # all stages
python check.py --only typecheck   # one stage, comma-separated for several
python check.py --fix-format       # fix formatting (never bare `gdformat .`)
```

Stages, remediation and engine quirks: **`docs/GATE.md`**.

## Definition of done

1. `python check.py` passes, output pasted.
2. Only agent-owned files changed.
3. New logic-layer code has GUT tests, named `test_<name>.gd` under `src/tests/unit/` — the
   `tests` stage fails on a test file the runner would silently ignore.
4. `python arch.py --write` re-run if module dependencies changed. At `involvement`
   `module` or above, report which modules and edges differ from what was approved —
   the diff is the human's check, not the picture.
5. **Anything you cannot verify called out explicitly** — anything visual, feel, timing,
   composition, animation graphs, tilesets, shaders by eye, bakes, export presets,
   real-latency networking, device testing
6. **Launch the game when you stop for visual judgement.** If the increment changed
   anything the human has to look at, run it before handing back — do not ask them to
   launch it themselves. The gate proves it loads; only they can say whether it is right,
   and asking them to do the launching adds a step to every review. A passing `resources`
   stage proves a scene loads, not that it looks right. Never claim a game "is running"
   from a headless run.
7. **At a slice boundary, run `python tools/friction.py`.** If a file was rewritten many
   times, or the proposal needed several revisions, say so and ask whether a tool should
   exist. It only sees committed history, so anything you retried inside one turn is
   yours to report — you are the only observer of it. Skill: `godot-tooling-friction`.
8. **When the gate says a retro is warranted, run one.** `python tools/retro.py --print`
   writes an evidence pack and a prompt and calls no model; `--sdk` hands the pack to
   copilot and resumes the thread within the same slice. Read
   `sessions[].human_messages` first — every correction the human made is a candidate
   kit defect. Write findings to `docs/retro/`; never change a rule, skill or gate file
   because of your own retro. Skill: `godot-retrospective`.
