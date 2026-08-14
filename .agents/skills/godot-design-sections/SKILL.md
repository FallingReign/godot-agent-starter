---
name: godot-design-sections
description: Use when writing or updating a document in docs/design/, when you need to know what a design section should contain, when deciding whether something belongs in design or in a decision entry, or when a slice needs to cite the design it descends from. Covers section shape, the kinds of sections a game design body accumulates, and how generated material is embedded without becoming a second source of truth.
---

# Design sections

`docs/design/` holds what the game is meant to be. Not what the code does -
that is derivable from the code. Design states intended experience, and it is
written before the code that delivers it.

One concern per file, named for the concern. Sections are created when there is
something real to put in them, never scaffolded in advance.

## Shape of a section

```markdown
# Cell vocabulary

_Resolution: settled · updated 2026-08-08_

## Intent
What the player should see, feel or be able to do. Experience, not mechanism.

## Why this way
The pressures that produced this answer, and what they rule out.

## Constraints it imposes
What later work must respect. This is the part slices cite.

## Tunables
| Tunable | What it controls | Range that still feels right |
|---|---|---|
| `movement.max_speed` | Peak horizontal speed | 260 to 380 |

## Open within this section
Questions this section has not answered yet.

## Revisit if
What would invalidate this.
```

`Intent` and `Why this way` are the minimum. The rest appears when it has
content.

`Tunables` names the values a designer wants to adjust and the range that still
feels right. It never names a file, a class or a line. Code claims each one with
a `## @tune <id>` comment, and `tools/design.py` generates a table into this
section showing the current value, where it is set, and the comment from the
code. See `godot-design-retrieval`.

Do not write a value here. The range belongs in design; the value belongs in code
and is generated in. A number in both places means one is wrong.

The resolution line matters more than it looks: a reader must be able to tell a
settled section from a leaning without reading the whole thing.

## Kinds of sections

A design body grows its own shape. This is not a checklist, and a project will
need sections nothing here anticipated.

**Experience** - the fantasy, what a session feels like, what the player is
doing minute to minute, what emotions are being aimed at.

**World** - setting, scale, how space is structured, how the player perceives
and traverses it, what makes a place memorable.

**Interaction** - controls, camera, movement feel, feedback, what responsiveness
means in this game.

**Systems** - combat, progression, economy, crafting, whatever the game has.
One section each, not one section called "systems".

**Content** - what content exists, who authors it, how much is needed, how it is
validated. If players author content, that is a section on its own.

**Presentation** - art direction, visual language, audio, readability,
accessibility.

**Social** - multiplayer shape, authority, communication, moderation, what
players can do to each other.

**Boundaries** - what this game is deliberately not. Often the most useful
section in the body, and the one most often missing.

Sections cross-reference. A vocabulary section constrained by content authoring
should link to the content section rather than restating it.

## Generated material

Anything derivable from the code is embedded, not retyped. Constants, module
graphs, primitive lists, stage counts.

Embed by reference:

> The current module graph is in `ARCHITECTURE.md`, generated from code.

Not by copying. The moment a design document contains a number the code also
contains, one of them is wrong and nobody knows which. That has already
happened in this repo's history.

## Design or decision

Both, usually.

A **decision** is a point event: this question, this answer, this date, this
person. Append-only, never edited.

A **design section** is the current state of intent. Rewritten freely as
understanding improves.

A settled question produces both: a decision entry recording the moment, and a
section update reflecting the new state, with `docs_at` on the decision pointing
at the section.

## Citing design from a slice

Every slice descends from design or is an explicit throwaway probe to inform it.

A slice cites the section establishing the end state it serves - not the feature
immediately above it. If the nearest citable thing is one link up from the work
itself, the design body is too thin there; enter discovery rather than writing a
shallow rationale.

Some slices change nothing experiential. That is fine, and the honest citation
is the existing section being implemented, with a note that nothing about
intended experience changed.
