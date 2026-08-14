---
name: godot-project-decisions
description: Use when a design or direction choice has just been settled and should be recorded, when you need something about this project's direction that is not written down, when you are about to invent a format that will persist content, or when you raise a question the human cannot answer yet. Covers any choice that constrains later work - technical (entity counts, content pipeline, netcode, platforms) and non-technical alike (art direction, scope, audience, monetisation, moderation, accessibility, team shape, audio). Also use when onboarding a brand new project.
---

# Project decisions

`project.shape.json` holds three tenses. It is not a questionnaire and not a
spec.

| Array | Tense | Rule |
|---|---|---|
| `decisions` | settled | Append-only. Never edited, never deleted. |
| `direction` | known heading | What today's work must accommodate. Removed when it graduates into code. |
| `questions` | open | The frontier. Deleted when answered, becoming a decision. |

`plan.html` renders all three plus the approved structure and the real module
graph. The gate regenerates it, so it is never stale. Point the human at it
rather than describing the state in prose.

## What counts as a decision

Anything that would change how a later agent writes code, chooses a layout,
shapes content or scopes work. There is no category list and no fixed
vocabulary - if it constrains later work, it belongs in the log.

That is deliberately wider than the technical questions. Art direction,
audience, scope, business model, moderation policy, accessibility floor, team
shape, audio approach, release channel, tone: all of these change what the
right answer looks like downstream, and none of them is recoverable from the
code.

The test is not "is this a programming decision". The test is: **would an
agent six weeks from now do something different if it knew this?**

## Onboarding a new project

Greet the person first. Say this is a fresh project that needs a couple of
minutes of one-time setup, and that you need three answers. Then ask:

1. What is it called?
2. In one or two sentences, what is the game?
3. How much say do they want over structure before code is written -
   hands-off, module, file or function?

Stop there.

Do not ask about entity counts, netcode, renderer, platforms or content
pipeline. A new project cannot answer those honestly, and an answer given
under pressure is a guess that every later agent will read as fact. Those
questions belong to the moment real work depends on them.

If the human says it is a jam entry or a throwaway prototype, say the journey
is short enough not to need a map, and move on.

## Before inventing a format that persists content

Save files, level documents, item definitions, anything a player or a designer
will author and that must load again later. **Read `direction` first.**

A format designed for exactly today's requirements is locally correct and
globally expensive: the next feature needs a version bump and a migration of
every file already written. Direction tells you what the document must leave
room for.

Two things this changes, and nothing else:

- **The shape.** If layers are coming, cell data sits inside a named container
  rather than at the document root. If tiles are coming, the document has room
  for a tile table. Adding a field later is cheap; restructuring a document is
  not.
- **Nothing about scope.** You still implement only what today needs. Leaving
  room for layers is not building a layer system.

If direction is empty and you are about to invent a persisted format, that is
the moment to ask: what will this eventually hold? One question, asked once,
at the only point where the answer is cheap to act on.

## Recording direction

Append to `direction` with `id`, `heading`, `accommodate`, optional `not_yet`,
and `added`.

The test: **does knowing this change the shape of what I build today, without
changing what I build today?** If it changes what you build, it is scope creep
and does not belong. If it changes nothing, leave it out.

Remove the entry when it graduates - the thing now exists in code, so the code
is the source of truth. Record where it landed as a decision.

## Graduating into a design document

Direction and decisions are a ledger. They are not a document a person can read
to understand the game. When enough of one is settled to be written for a
reader, write it into `docs/design/` and set `docs_at` on the decision entry.

Two folders, two purposes. `docs/` documents the kit: the gate, the rules, the
workflow. `docs/design/` documents the game: what a cell is, how the world is
structured, what a content format guarantees to load forever.

Only settled material graduates. Anything still being explored stays in
`direction` or `questions`, because a design document is read as fact. There is
no way for a later agent to tell speculation written as documentation apart from
something decided deliberately, and it will build on both equally.

Nothing is deleted from `decisions` when this happens. The plan view stops
showing the item; the ledger keeps the question, the answer, the reason and the
date. The gate fails if `docs_at` points at a file that does not exist, because
the plan would then hide an item on the promise that it was written down.

Do not generate a design document from decision entries. A list of decisions
reads as a changelog, not as an explanation. Draft it, get it approved, and let
it be prose.

## Recording an open question

When you raise something the human cannot answer yet, or answers later, append
to `questions` with `id`, `question`, `blocks`, optional `options`, `raised_by`
and `raised`. It appears in `plan.html` with the options as radio buttons.

A question belongs there when you can **state it precisely** and real work
depends on it. If you cannot state it precisely, it is fog: leave it out
entirely rather than recording a vague placeholder.

When it is answered, **delete it** and append a decision carrying the same
question text. A question sitting beside the decision that answers it is how a
stale frontier is born, and the gate rejects that overlap.

## When you hit a fork you cannot resolve

This applies to every skill and every task, not only the questions listed
somewhere. Whenever you are about to choose between paths that would lead to
materially different code, content or scope, and nothing in the log or the
repo decides it: **raise it, do not pick.** Then record the answer.

That is the whole mechanism. It replaces asking everything up front, and it
does not depend on anyone having anticipated the question.

## When you need an undeclared decision

Read the log. If the answer is not there:

**Can you state the question precisely, right now, because work in front of
you depends on it?** If yes, ask the human. If no, it is fog - do not ask, and
do not record a blank.

Ask one question with two or three concrete options, grounded in what you are
actually doing. "Does anything iterate all items every frame, or only the
equipped ones?" is answerable. "What is your per-frame entity count?" is not.

Never answer your own question. If the human says "you decide", you may decide
- record it with `decided_by: "agent"` and your reasoning in `because`, so the
provenance is visible later.

## Recording something settled in conversation

Most decisions are not answers to a question you asked. They arrive as an
aside while discussing something else: a constraint, a preference, a scope
cut, a direction. When the human settles something in passing and it would
change later work, append it before moving on, with `question` phrased as the
question it answers.

Keep their framing. A remark like "latency is hidden by design rather than
solved in netcode" is worth more than any category it could be filed under, so
record the words and put the shape in `note`.

## Recording a decision

Append an object with `id`, `question`, `answer`, `because`, `decided_by`,
`date`, `revisit_if`. Optional `note`, `supersedes` and `docs_at`.

- `question` is the question as it was actually asked.
- `answer` is what they said. If their answer does not fit the options you
  offered, keep their words and put the nuance in `note`. Do not round to the
  nearest option you happened to list - the nuance is usually the valuable
  part.
- `because` is one line on what forced the decision now. This is what tells a
  later reader whether the decision still applies.
- `revisit_if` is what would invalidate this. Without it every entry reads as
  permanent, including the ones that were the cheapest thing that unblocked a
  slice. `"permanent"` is a valid answer; refusing to consider the question is
  not. Good ones are observable: *playtesting shows cells unreadable at 4K*,
  *map files exceed the P2P payload budget*, *a second content author joins*.

Never write `unknown`, `tbd`, `n/a` or any placeholder as an answer. An
undecided question is an **absent entry**. The gate rejects placeholders for
exactly this reason: an absent decision makes the next agent ask, a fabricated
one makes it trust a guess.

## Changing direction

Do not edit an existing entry. Append a new one with a new `id` and set
`supersedes` to the old id. The old reasoning stays readable, which matters
when the same question comes back.

Before proposing anything that reverses a current decision, read the entry and
say what has changed since. A decision with a stale `because` is the one worth
revisiting.
