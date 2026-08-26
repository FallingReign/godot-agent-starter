---
name: godot-human-involvement
description: Use before writing code for any new module, class, system or data structure, to check how much the human wants to approve first. Also use when you are about to create a directory, add a file, add a module, choose how components connect, or propose an architecture - when the conformance stage reports a deviation from the approved structure - and when a human asks for more or less say over how things are built.
---

# Human involvement

`project.shape.json` has an `involvement` field. It sets how much structure the
human approves before it exists. Read it before proposing or building anything;
the gate prints it on every run.

| Value | Before writing code |
| --- | --- |
| `hands-off` | Nothing. Build it, report what you built and the gate result. |
| `module` | Propose the modules, their boundaries, and the data that crosses them. Wait. |
| `file` | As `module`, plus every file you will create or modify. Wait. |
| `function` | As `file`, plus function signatures and how they connect. Wait. |

It is a working preference, not a fact about the game, so it is editable rather
than append-only. A human may raise or lower it at any time, including mid-slice.

## Extend before you create

Before proposing anything new, look at what already exists that could serve.
List each candidate and say in one line why it cannot. A new module beside an
existing one that does nearly the same job is the most common structural
mistake, and it is invisible in a proposal that only describes new things.

Record those candidates in `considered_existing`. If the honest answer is that
nothing existing fits, say so explicitly rather than omitting the section.

## Propose in the file, not in the chat

A structure described in a chat message cannot be reviewed properly and
evaporates when the turn ends. So the proposal is written down **first**, as a
draft, and the human reviews the rendered view rather than a wall of prose.

1. Write `experience` **first**: what the player does, how it should feel, how
   the camera behaves, what the controls do, and in `not_this` the nearest thing
   this is deliberately not. See "Experience before structure" below.
2. Fill `design_refs`: the design section this work is descended from, plus one
   line on why. See "Cite the design, do not restate it" below.
3. If the visual outcome can be approximated with vector shapes or layout
   boxes, write `mockup.svg` as inline SVG. If it cannot, say why in
   `mockup.not_possible`. See "Mock it when you can draw it" below.
4. Fill the structure to the depth the involvement level implies: modules at
   `module`, plus files at `file`, plus signatures at `function`. Set
   `baseline_sha` to `git rev-parse HEAD` and `"status": "draft"`.
5. Run `kit plan`.
6. Tell the human the draft is ready with a **clickable link** using the
   absolute file URI, for example
   `[Open plan.html](file:///C:/path/to/repo/plan.html)`. Do not only name the
   file. Summarise in two or three lines at most. Do not restate the proposal
   in chat; that is what the view is for.
7. Wait. **Do not write code.**
8. On approval, set `"status": "approved"` with `approved_by` and
   `approved_on`, then build.

## Experience before structure

Every other artefact in this repo describes structure. `ARCHITECTURE.md` is a
graph, `arch.rules.json` is boundaries, the proposal is modules and files.
**Nothing else records intended feel**, so when it is absent, feel gets inferred
from whatever the code happens to do.

That inference can produce structurally valid work with the wrong interaction.
Experience makes the intended response reviewable before implementation gives
one interpretation accidental authority.

So `experience` is required whenever the proposal contains structure, at every
involvement level including `hands-off`. `player_does` and `feels_like` are the
minimum; the `conformance` stage fails without them.

`not_this` earns its place. One line naming the nearest wrong reading kills a
whole class of inference: "inline acknowledgement, not a modal confirmation"
is worth more than three lines describing what it is.

## Cite the design, do not restate it

Every slice is either descended from a design choice or is a throwaway prototype
informing one. It does not have to *write* design. It has to point at the design
it serves, so a reader can follow the chain from a file back to why the game
needs it.

`design_refs` is a list of `{section, why}`. The section is a path under
`docs/design/`; the `why` is one line connecting this slice to that end state.

The reference must reach an **end state**, not the feature immediately above.
"The interface must respond" is one step up and rules nothing out. "Every
accepted action acknowledges immediately and preserves enough context to
understand its result" tells you what to build and what to reject.

**If the only answer available is one step up, the design body is thin there.**
That is not a reason to write a shallow rationale — it is the signal to enter
discovery and write the section. See `godot-design-discovery`.

A reference to a file that does not exist fails the gate, and should: it reads as
though the reason was written down when it was not. Either write the section or
drop the reference.

A slice with genuinely no design ancestry is possible — a throwaway prototype
built to answer a design question. Say that in `experience.player_does` rather
than inventing a lineage for it.

## Mock it when you can draw it

Rejecting a picture costs a minute. Rejecting an implementation costs a slice.

If the outcome is vector-approximable, draw it: grids, shapes, layout
arrangement, UI panels, icon silhouettes, spatial relationships. Inline SVG in
`mockup.svg`, no external files, because the view is opened over `file://`.

If it is not drawable, say why in `mockup.not_possible` and move on. Raster art,
material response, physics feel, timing and animation curves cannot honestly be
approximated with shapes. **Never fake a mockup you cannot draw** — a misleading
picture is worse than none, because it gets approved.

Only inert shape markup is rendered. Script tags, event handlers and embedded
content are stripped, so do not put anything interactive in it.

A draft is not a contract. The `conformance` stage only diffs an `approved`
proposal, so writing a draft early enforces nothing and risks nothing. What it
buys is a reviewable artefact instead of a chat message, and a draft that
survives the turn ending.

## Deviation is expected

You will discover mid-build that the proposal was wrong. That is normal and it
is not a failure. What is a failure is deviating silently.

When the code needs to differ: stop, append an entry to `revisions`, update the
affected section, set `status` back to `draft`, regenerate the view, and ask the
human to look again. At `hands-off`, record it and carry on.

Reverting to `draft` is what makes a revision visible. An approved proposal that
quietly changes is the same failure as no proposal at all.

The conformance stage names the two directions: in code but not proposed, and
proposed but not built. Address both in your report rather than letting the
stage repeat them next run.

## What a proposal contains

**Do not guess the field names.** Run `kit schema describe proposal` and use
exactly what it prints. The gate validates every key, and an
invented one fails with the correct name in the error. This matters because it
has already gone wrong: a proposal written with `change`, `why` and
`responsibility` instead of `action`, `purpose` and `role` rendered as a list of
paths with no reasons attached, and the conformance diff skipped every file.

Every layer states **why**, not just what:

- A module: what it is responsible for, why it exists or changes now, what it
  may depend on, and what crosses its boundary by type.
- A file: the path, `new` or `modify`, and why this slice touches it.
- A function: the full signature, `new` or `modify`, and why.

`why` at file and module level is the part most often skipped, and it is the one
a reviewer needs most. "modify interaction_view.gd" is unreviewable. "preserve
focus after a rejected action and show the reason inline" can be agreed or
argued with.

Keep it short enough to read in one screen. A proposal nobody reads is worse
than none, because it manufactures the appearance of approval.

## Do not

- Propose and implement in one turn at `module` or above.
- Ask for approval of a structure that exists only in your chat message. Write
  the draft, render it, point at it.
- Set `status` to `approved` yourself. Only a human approval flips it.
- Treat a green gate as approval. Conformance reports; it does not consent.
- Leave `baseline_sha` empty. Without it, every pre-existing file reads as
  unproposed and the report becomes noise the human learns to skip.
- Edit `plan.html` or `retro.html`. Both are generated.
- Invent a key because the one you want is not in the schema. If a field is
  genuinely missing, say so and propose adding it.
