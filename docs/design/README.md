# How design works

This folder documents **the game**. `docs/` documents the kit. They do not mix.

A design section is written **before** the code it governs, not after. Design that
only records what was already built is a changelog, and nobody reads it twice.

## Where a document goes

One question, and it has one answer:

> **Which domain would be incoherent without this?**

That domain owns it. Every other domain links to it and states only how it differs.

Remove the world and cells are meaningless, so `world/cells.md` owns the cell
grammar. Remove UGC and a stable format has no reason to exist, so `ugc/format.md`
owns the format rules. `ui/` links to `world/cells.md` and says only what panels do
differently.

**Do not create a folder for cross-domain concerns.** `foundations/`, `systems/`,
`shared/`, `common/` — each one creates a second plausible home for every document
that spans domains, and every shared rule spans domains by definition. Cross-domain
reach is expressed as inbound links, not as a location. `tools/design.py` reports
fan-in, so a rule referenced by six domains is visibly shared without needing a
folder of its own.

## Goals versus rules

**A goal** is a property everything should exhibit, satisfied differently in each
domain. Accessible. Readable at small sizes. Recognisable before decoding. Goals
live in `pillars.md`, and each domain records how it satisfies them — a child
inherits its parent's position unless it departs from it, and `not applicable` is a
valid, useful answer.

**A rule** admits one implementation, so no domain has discretion. One primitive per
cell. Rotation is a draw-time transform. Rules have an owning domain and everyone
else links to them. A rule does not get a per-domain section, because the honest
answer everywhere would be "yes, as specified" — which is box-ticking.

## Resolution

Every document declares its current maturity on the first line:

```
_Resolution: settled
```

- `question` — nothing decided. The document holds the shape of the question.
- `direction` — a leaning, with what would settle it.
- `settled` — decided, and code may depend on it.

This is a **maturity marker on a living document**, not a document type. A file
declared `question` today becomes `direction` then `settled` as content firms up.
Same file throughout.

Two things depend on it. Tier gating: never write past the resolution the human has
reached — a section may exist as a question with no answer, but must not be filled
with a plausible inference, because that reads as intent to the next agent and gets
built on. And backlog derivation: `settled` with unbound tunables is ready to build;
`question` with a stated way to settle it is a probe.

## Structure of a section

Open with what the player experiences, in prose, with no heading above it. Then what
the system must do. Reasoning appears only where it constrains implementation.

Three headings are load-bearing and must appear verbatim when used:

- `## Tunables` — values a designer adjusts, with ranges. `tools/design.py` finds
  them by this exact heading and generates the binding table beneath it.
- `## Open` — unresolved questions. This is how they reach the plan view.
- `## What would settle it` — for `question` and `direction` sections.

Everything else is free. Use headings that fit the content.

## Tunables and binding

Design names the value; code claims it:

```gdscript
## @tune movement.max_speed
## Peak horizontal speed. Above 380 it starts to feel floaty.
@export var max_speed: float = 320.0
```

`tools/design.py` generates a table into the section showing the current value, its
`file:line`, whether it is adjustable, and that comment. **Design never contains a
path**, so code moves freely.

A declared tunable bound to a `const` is reported, because declaring something
tunable is a requirement that it be adjustable. A tunable claimed in code and
declared in no section is also reported — that is a knob nobody designed.

## Regenerating

```
python tools/design.py
```

Rewrites `INDEX.md` and every binding table. **Run it after adding, moving or
renaming any document**, or the `design` stage fails with a stale index — which
otherwise silently hides the entire design body from the plan.

`INDEX.md` is generated. Never hand-edit it.

## Open questions and decisions

An open question lives in the document it concerns, under `## Open`. A combat
question goes in `combat/`, which means the file has to exist — that is the point.
Creating the file is what invites a designer to fill it in.

A settled decision goes in `project.shape.json` under `decisions[]`, with a
`docs_at` pointer to the section that explains it for a reader.
