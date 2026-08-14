---
name: godot-retrospective
description: Use when a slice finishes, when the gate says a retro is warranted, when the human reports friction or says something was tedious or unclear, or when asked why a slice cost more than expected. Covers running the evidence pack, reading it honestly, and turning findings into kit changes rather than blame.
---

# Retrospective

The kit's job is to guide an agent so the human intervenes as little as
possible. A retro measures whether it did, and turns the gaps into changes.

Run it with `python tools/retro.py --print`. That writes a deterministic
evidence pack and a prompt, and calls no model. `--sdk` hands the same pack to
copilot and can resume a thread within the same slice. `--why` reports whether
a retro is warranted without doing anything.

The pack is the deliverable. The model reading it is one adapter, so a retro
works with any agent, or with none.

## Read the human messages first

`sessions[].human_messages` holds every message the human sent after the
opening task, verbatim. It is the primary signal and everything else is
context. Each carries a `hints` array from a crude regex; it was tested
against a real session and missed most genuine friction, so an empty `hints`
array means nothing.

Real friction reads as observation, not complaint:

- "Built now shows to do not written" is a defect report
- "I'm seeing a lot of unproposed functions here" is a defect report
- "do we currently have a process around backlogging work" is a missing
  mechanism
- "I expected to see a player scene" is an expectation the kit never set

None of those contain a complaint word. All of them are kit gaps.

## Every correction is a candidate kit defect

Start from the assumption that the agent behaved reasonably given what it
was told. Then ask what the kit failed to supply, check or prevent.

| Signal in the pack | Read it as |
| --- | --- |
| Human corrects an output | Something rendered wrong or was never required |
| Human asks how something works | A mechanism exists but is not surfaced |
| Human asks whether a process exists | It does not, or it is invisible |
| Same command run many times | A loop, usually a check that cannot pass |
| Same file rewritten many times | Missing tooling, see `godot-tooling-friction` |
| One stage failing repeatedly | Remediation text is not enough to act on |
| A rule satisfied in a strange way | The rule is passable without complying |

That last one is the most important and the hardest to see. A gate that can
only be passed by producing something will get something produced.

## What the pack cannot see

It reads committed history, logged turns and gate logs. It does not see
attempts inside a single turn. Six tries at one command in one turn leave no
artefact, so the most expensive friction can be invisible.

So say what you suspect and cannot evidence, and ask. The human is the only
instrument that watched it happen.

## Findings are proposals, not changes

Write findings to `docs/retro/`. Do not edit gate files, rules or skills as a
result of your own retro. A retro that can act on its own conclusions will
accrete plausible rules that nobody agreed to, and the kit gets worse without
anyone noticing.

For each finding say whether the fix is mechanically checkable. A rule with
nothing enforcing it is worth having, but say so plainly rather than implying
a check exists.

## Propose goals against the end state

A goal is a change to the kit, not a task in the game. Tie it to a finding,
and prefer removing a mechanism over adding one where both would work. The
kit has more rules than any agent can hold at once, so a rule that replaces
two is worth more than a rule that adds a third.
