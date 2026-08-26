---
name: godot-tooling-friction
description: Use when you are about to hand-author content by hand - level layouts, maps, item tables, dialogue, ability definitions, spawn tables - or when you have retried the same mechanical edit several times, or when a human will need to produce many instances of something by hand. Decides whether to build an authoring tool first instead of grinding through it.
---

# Tooling friction

A game developer automates a process once it becomes slow. An agent will not,
because a tedious task and a quick one feel identical from the inside: both are
just turns. So the trigger has to be recognised deliberately.

## The two signals

**You are about to hand-author content.** A map, a level layout, an item table,
a dialogue tree, a set of ability definitions. Anything where the shape is
mechanical and the volume is more than a handful.

**You have retried the same mechanical edit several times.** Rewriting a whole
file to change a few rows, fighting shell quoting to emit a grid, regenerating
something by hand that a loop could produce.

Neither is about the task being unpleasant. Both are about the same thing:
work that a tool would do more reliably than a turn.

## The question that decides it

Not "is this slow". Ask:

> **Will a human do this more than a handful of times, and is the tool for it
> part of the shipped game anyway?**

If both are yes, building a crude version now is not a detour. It is pulling a
required system forward, and you get to use it while building.

Cases where both are usually yes:

| Content | Tool that ships anyway |
|---|---|
| Level or map layout | A level editor, especially with user-generated content |
| Item and loot tables | A data authoring or balancing surface |
| Dialogue and quests | A dialogue editor |
| Ability and status definitions | A designer-facing data surface |
| Spawn and encounter tables | An encounter authoring surface |

If the answer is no — a one-off fixture, a throwaway probe — grind through it
and move on. Building a tool for a single use is the mirror-image mistake.

## Keep the tool thin

The thinnest useful version is almost never an editor.

1. **A generator.** A script taking a compact spec and emitting the verbose
   form. Often ten lines and enough.
2. **A `@tool` script.** Runs in the editor, no separate application.
3. **A headless command.** Reads a terse text format, writes the real one.
4. **An in-game editor.** Only when it ships as a feature.

`godot-increment-size` applies to the tool as much as to the game. A generator
that handles today's format is right; a general-purpose editor built before any
content exists is not.

## Raise it, do not just do it

Building a tool is a scope change, so it goes through the same route as any
other fork: say what you noticed, what the tool would be, roughly what it costs,
and what it replaces. Then wait.

> Authoring this map by hand means emitting 336 tokens through patch edits, and
> I have retried it four times. A generator taking terse row strings would make
> this one command, and content authoring needs a surface anyway because players
> will create maps. Build the generator first, or continue by hand?

Record the answer as a decision — including a decision *not* to build one, with
what would change that. Otherwise the next session re-litigates it.

## What the report can and cannot see

`kit friction` reports churn from committed history. It sees a file
rewritten across many commits. It cannot see six attempts inside one turn,
because nothing is committed between them.

So a clean report is not evidence there was no friction. You are the only
observer of the attempts that left no trace, which is why the signals above are
worth recognising in the moment rather than waiting for a report.
