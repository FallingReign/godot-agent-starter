---
name: godot-design-discovery
description: Use when you need to understand what the game is actually meant to be before building part of it - when the human describes an idea, vision or feature that is not yet written down, when you cannot answer why a piece of work exists by pointing at an established end state, when a request could be satisfied several very different ways depending on intent, or when the human wants to explore or think out loud rather than ship. Produces design sections and decisions, no code.
---

# Design discovery

A separate pipeline from delivery. Delivery turns a request into working code.
Discovery turns an idea into a written end state that later work can descend
from.

They are different pipelines with different outputs. Do not write code while
discovering, and do not silently start discovery in the middle of a build - say
that is what you are doing. After the design artefact exists, a hands-off agent
may explicitly transition back to delivery only under the provisional-design
rule below.

## When to enter

Three triggers, in order of how often they fire:

**The human describes intent, vision or an idea.** They are thinking out loud,
not filing a request. This is the main source of design, and it is the one the
repo has no other way to capture.

**You cannot answer "why does this exist" from an established end state.** If
the only available answer is one link up from the work itself, the design body
is too thin there. Write the section instead of writing a shallow rationale.

**A request has several very different valid shapes** depending on intent that
is not recorded. Resolve the intent first; see `godot-resolving-ambiguity` for
the questioning technique.

## Reach the end state, not the next step

The purpose is the destination, not a justification for the current task.

Shallow, and useless:

> Why render a grid of tiles? So the world is visible.

True, and it constrains nothing. Any renderer satisfies it.

Deep enough to build against:

> Why build the world from a small vocabulary of primitives rather than
> free pixels? Because players author and share content, so it must be
> composable from parts small enough to author by hand, validate
> automatically and moderate at scale. And because there is no central
> server, map data crosses the wire between peers and must be small and
> verifiable by the receiver without trusting the sender.

That answer names constraints. A vocabulary chosen for authorability is a
different vocabulary from one chosen for wire size, and now you know which
pressure wins.

The test: **does the answer rule anything out?** If not, keep going up.

## Ask one thing at a time

A design tree is walked, not filled in. Ask the narrowest question that
resolves the biggest fork, wait, then use the answer to pick the next question.

Never send five questions in one message. The human answers the easy ones and
the hard one gets lost, which is the one that mattered.

## Write at the resolution you actually have

A design section is created as soon as there is a real question, and it grows
as answers arrive. It does not need to be complete to exist.

| Resolution | What the section holds |
|---|---|
| Question | The question, why it matters, what depends on it. No answer. |
| Direction | A leaning, what it would rule out, what would confirm it. |
| Settled | The answer, the reasoning, what would invalidate it. |

**Never hide an inference inside design as though the human supplied it.** By
default, ask rather than fill a gap. One narrow exception exists: if established
design and repository evidence force one answer with exact `very-high`
confidence, write it as `agent-provisional`, disclose agent authorship, explain
the inference and assumptions, and provide a front-loaded Quick read plus veto
scope and next go/no-go. See `godot-design-sections` for the exact shape.

Below very-high confidence, or where two materially different experiences
remain plausible, ask. An empty question is honest; an undisclosed plausible
answer is not recoverable.

Agent-provisional settled design may authorize hands-off delivery only while
the proposal is `recorded` and every change remains cheap to veto. Stop and ask
for explicit approval before the first difficult-to-reverse commitment. Silence
is never confirmation.

## Where it lands

Design sections live in `docs/design/`. See `godot-design-sections` for what a
section should contain and how to shape one.

A discovery session that settles something also appends a decision entry, with
`docs_at` pointing at the section. See `godot-project-decisions`.

An unresolved question is a design section at question resolution. Do not
record it in two places.

## Ending a session

Say what was settled, what is still open, and what the next question is.

Do not offer to start building. Discovery ends when the human decides it has,
and the natural next step is often another discovery session rather than a
slice.
