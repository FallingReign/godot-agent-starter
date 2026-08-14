---
name: godot-increment-size
description: Use when planning work, breaking a feature into steps, deciding what to build first, or when a task feels large enough to need a plan. Also use when you are about to build a data format, a system or a layer that nothing yet renders or runs, and when deciding whether to finish several related additions in one pass or stop after the first.
---

# Increment size

The unit of work is the thinnest change that **runs end to end and can be looked
at**, and that answers one question you can state precisely right now.

That is stricter than "small" and stricter than "testable". A data format with
unit tests is small and testable and still the wrong increment, because nothing
runs and nothing can be looked at.

## The two tests

**Does it run end to end?** Data on disk, loaded, processed, presented, visible.
If any link in that chain is missing, the increment is a layer, not a slice.

**Can you state its question now?** Not "will it work" but a real question with a
knowable answer. *Does a hand-drawn map read as a place. How many primitives are
needed. Is the world one grid or many.* If phrasing the question requires
knowing the answer to a later increment, it is not the next increment.

## Why layers feel right and are not

Building the data model, then the parser, then the renderer, then the game is the
order the architecture diagram suggests, and it is the wrong build order. Three
of those four produce nothing anyone can evaluate, so three of them accumulate
decisions nobody has checked. By the time something is visible, the mistakes are
load-bearing.

Building thin and vertical inverts that. The first increment is ugly and almost
featureless, and every decision in it has been seen.

## Do not build ahead of the question

A concrete symptom: eleven primitives added at once because the design document
lists eleven. The point of adding them is to find where the returns stop, and
that is invisible when they arrive together. Add one, look, report what it
unlocked and what is still impossible, then stop.

The same applies to format fields, layers, systems and options. If the current
question does not need it, it is not in scope, even when adding it now is
cheaper than adding it later. Cheap-to-add is not the same as free-to-carry:
every unused field is a thing later code must handle and later agents must
understand.

## Finishing pressure

There is a standing pull toward completing a coherent-feeling unit rather than
stopping mid-way. Resist it when the instruction was to stop. A request to add
one thing and report is a request for information, and delivering four things
destroys the information even if all four work.

If stopping feels wrong because the result is incomplete, say so in the report
rather than fixing it by building more.

## When the increment is genuinely too small

Sometimes the thinnest runnable change is trivially small - a constant, a colour,
one field. That is fine and does not need padding. Ship it, look at it, move on.
The cost of an increment is not its size, it is the review it needs, and a
trivial increment needs a trivial review.

## Recording what an increment taught you

An increment that answers a question has produced a decision. If the answer
changes how later code is written, append it to the decision log rather than
leaving it in a chat transcript. See `godot-project-decisions`.
