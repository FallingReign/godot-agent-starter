---
name: godot-design-sections
description: Use when writing or updating a document in docs/design/, when creating a new design folder, when deciding which domain a design document belongs to, when you need to know what a design section should contain, or when a slice needs to cite the design it descends from. Covers the placement rule, section shape, the goal-versus-rule distinction, and how generated material is embedded without becoming a second source of truth.
---

# Design sections

`docs/design/` holds what the game is meant to be. Not what the code does -
that is derivable from the code. Design states intended experience, and it is
written before the code that delivers it.

## Placement: the one question

**Which domain would be incoherent without this? That is where it lives. Every
other domain links to it and states only how it differs.**

Ask it once and you get one answer. Remove dialogue and branching choices are
meaningless, so branching rules live with dialogue. Remove audio and a dynamic
mix has no reason to exist, so its rules live with audio.

**Do not create a folder for cross-domain concerns.** `foundations/`, `systems/`,
`shared/`, `common/` all create a second plausible home for every document that
spans domains - and every shared rule spans domains by definition. This repo
tried `foundations/` and `systems/` and both produced ambiguity for most
documents. Cross-domain-ness is expressed as **links**, never as a location.

## Goals and rules are different things

A **goal** admits many implementations. Accessibility is satisfied differently by
dialogue, by input, and by audio. Each domain decides how, so each domain
records its own position. Goals live in `pillars.md` and manifest as a section
wherever a domain makes a decision about one.

A **rule** admits one implementation. A saved action binding has one canonical
identifier; display labels are derived at presentation time. No domain gets
discretion, so there is nothing per-domain to record. A rule has an owning domain
and everything else links to it.

Requiring a section for a rule produces box-ticking, because the honest answer in
most files is "yes, as specified". Requiring one for a goal is useful, because the
interesting content is how this domain honoured it.

Two mechanisms make an absent goal section honest rather than a gap:

- A child inherits its parent's position unless it departs from it. `audio/mix.md`
  says nothing about accessibility unless it differs from `audio/audio.md`.
- "Not applicable to this domain, because ..." is a complete and useful answer.

## Domain roots and children

A domain starts as one file and grows a folder when it has a second document.
`audio.md` becomes `audio/audio.md` plus `audio/dynamic-mix.md` when mixing needs
its own page.

The root states **what is true of the whole domain**: what it is, how it feels,
the goal positions its children inherit. Children state what is true only of
themselves.

A root is readable alone and is not a table of contents. It does not summarise
its children - a viewer may render children inline beneath it, and a summary plus
the detail on one screen is the same fact twice.

## Shape of a section

```markdown
# Confirming an action

_Resolution: settled_

You activate a focused action and receive immediate visual and audible
acknowledgement. Confirmation feels crisp without becoming distracting.

## Behaviour

The acknowledgement begins in the same frame as the accepted action. Rejected
actions use a distinct response and leave focus where it was.

## Tunables

| Tunable | What it controls | Range that still feels right |
|---|---|---|
| `interface.confirm_feedback_seconds` | Duration of confirmation feedback | 0.08 to 0.18 seconds |

## Open

Whether controller vibration is part of the default response.

## What would settle it

A controller check on one supported device, with vibration on and off.
```

The opening paragraph carries no heading and describes the player's experience.
It becomes the section's summary line in the index, so it must stand alone.

Headings after that are whatever the section needs - `Behaviour`,
`Construction rules`, `How the set grows`. There is no template. Reasoning
appears only where it constrains implementation, and then as a specific heading
such as `Why continuous`, never as a generic `Why this way`.

Four headings are load-bearing and must match exactly:

- `## Tunables` - the tunable table. `tools/design.py` locates it by this heading.
- `## Open` - unresolved questions. This is how they reach the plan view.
- `## What would settle it` - for a section at question resolution.
- `_Resolution: <level>_` - one of `settled`, `direction`, `question`.

The resolution line is the tier gate. A section may exist as a question with no
answer; it may not be filled with a plausible inference, because that reads as
intent to the next agent and gets built on.

## Tunables

Design names the value and the range that still feels right. It never names a
file, a class or a line. Code claims each one:

```gdscript
## @tune interface.confirm_feedback_seconds
## Duration of confirmation feedback.
@export var confirm_feedback_seconds: float = 0.12
```

`tools/design.py` generates a table into the section showing the current value,
where it is set, and that comment. See `godot-design-retrieval` for the three
rules that govern binding.

Never write a value in design. The range belongs in design; the value belongs in
code and is generated in. A number in both places means one is wrong and nobody
knows which.

## Generated material

Anything derivable from code is embedded by reference, never copied.

> The current module graph is in `ARCHITECTURE.md`, generated from code.

That has already gone wrong in this repo's history: a hand-written stage count in
`README.md` said thirteen when there were nineteen.

## Design or decision

Both, usually.

A **decision** is a point event: this question, this answer, this date, this
person, and what would invalidate it. Append-only, never edited.

A **design section** is the current state of intent. Rewritten freely as
understanding improves.

A settled question produces both: a decision entry recording the moment, and a
section update reflecting the new state, with `docs_at` on the decision pointing
at the section.

Open questions live in the domain they concern, under `## Open`, not in a central
list. An audio question requires `audio/` to exist - which is the point, because
it makes the missing design visible.

## Citing design from a slice

Every slice descends from design or is an explicit throwaway probe to inform it.

A slice cites the section establishing the **end state** it serves, not the
feature immediately above it. "The menu must respond" rules nothing out.
"Every accepted action must acknowledge immediately and preserve enough context
for the player to understand its result" tells you what to build.

If the nearest citable thing is one link up from the work itself, the design body
is too thin there. Enter discovery rather than writing a shallow rationale.

Some slices change nothing experiential. The honest citation is the existing
section being implemented, with a note that intended experience did not change.
