---
name: godot-resolving-ambiguity
description: Use when a request could be satisfied two materially different ways, when you are about to guess at intended behaviour, movement, camera, layout or scope, or when you notice yourself picking the interpretation that is easier to build. Also use before writing a proposal for anything whose feel or shape was not stated.
---

# Resolving ambiguity

A request that can be satisfied two materially different ways is not a request
yet. Building the average of two readings produces something nobody wanted, and
building the easier reading is a guess dressed as a decision.

The cost asymmetry is stark. One question costs a message. A wrong reading costs
a slice, plus the conversation working out why the result feels wrong, plus the
revert.

## The test

Before you write a proposal, try to describe the finished thing in two sentences
to the person who asked for it. If they would say *"no, that is not what I
meant"* at any point, you have found the ambiguity.

Concretely, you are ambiguous if you cannot answer these from what was written:

- What the player is doing, moment to moment
- Whether it is continuous or discrete
- What the camera does while it happens
- What input does what
- What happens at the edge, the end, or the failure case
- How much of it is in scope for this increment

## One question at a time

**Ask the single question that eliminates the most possibilities.** Then wait.
Then ask the next, informed by the answer.

Do not send five questions in one message. The human answers the easy ones,
skips the hard one, and you build on the gap. Do not offer a menu of eight
options either — two or three concrete readings, each described by its
consequence.

Bad: *"How should movement work?"* — unanswerable, no options, invites a shrug.

Bad: *"Should movement be grid-based or free? And what about the camera? And
should there be acceleration? And what happens at map edges? And is there
collision?"* — five questions, one answer.

Good: *"Two readings of 'move around the map'. One: the player steps cell to
cell, arriving centred, like a roguelike. Two: the player moves continuously at
any position, cells only describing what is drawn. These need different
collision and camera work. Which?"*

Then, after the answer: *"Continuous it is. Does the camera follow the player
smoothly, or stay fixed until they near the edge?"*

## When to stop

Stop when you could state the outcome and the human would recognise it. Not when
you have enough to start typing — those are different thresholds, and the second
one arrives much earlier.

Two specific stopping conditions:

- The human says "you decide". Then decide, but **record who decided**. Write the
  decision with `decided_by: agent` and the reason, so it is visible later as a
  choice you made rather than one they made.
- The question is not yet statable. If answering it requires knowing something
  the next increment will reveal, it is fog, not ambiguity. Leave it. Say what
  you are assuming for now and that it will be revisited.

## Where the answers go

An answer that lives only in the chat is lost by the next session.

- A resolved fork with lasting consequence → a `decisions[]` entry in
  `project.shape.json`, with the question as it was asked and one line on what
  forced it.
- Intended feel, movement, camera, controls → the `experience` block of
  `proposal.json`, including `not_this` naming the reading you ruled out. That
  one line prevents the same wrong inference next time.
- Something raised but not yet answerable → a `questions[]` entry, so the
  frontier is visible instead of forgotten.

## Do not

- Do not resolve ambiguity by building both and letting the human choose. That
  is two implementations to review and throw one away.
- Do not treat silence as approval of your reading.
- Do not ask about something the repo already answers. Read
  `project.shape.json`, the existing code and `ARCHITECTURE.md` first — asking
  a question that is already recorded wastes the human's attention and teaches
  them to stop reading your questions.
- Do not bury the question at the end of a long message about something else.
