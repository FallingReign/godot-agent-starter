---
name: godot-node-or-resource
description: Use when deciding how to represent something new in Godot — whether it should be a Node, a Resource, a plain RefCounted class, an autoload singleton, or raw data driven through a server API. Also use when a scene tree is growing large or deep, when wondering whether to add an autoload, when node counts may become a performance problem, or when unsure what belongs in a scene versus in code.
---

# Node, Resource, RefCounted, or server

## The decision

Ask what the thing actually is, in this order. Take the first match.

**Is it pure state or behaviour with no presence in the world?**
A plain `RefCounted` class. Rules, calculations, state machines, parsing,
inventory logic, damage resolution, simulation. This is the default and it
should be the largest part of the codebase.

**Is it data that is authored once and shared by many things?**
A `Resource`. Definitions, stats, configuration shared across instances.
Resources are shared by reference when loaded, so mutating one at runtime
affects every holder — treat them as read-only unless you deliberately
duplicate.

**Does it need to exist in the tree — be positioned, drawn, receive input, or
participate in physics?**
A `Node`. But keep it thin: it reads input, displays state, and delegates
decisions to a `RefCounted` object it owns.

**Are there thousands of them, all doing the same thing?**
Not nodes. Use the relevant server API with handles, and drive them from flat
arrays in bulk. Per-instance node overhead and per-call boundary crossings both
dominate at scale.

## Why the RefCounted default matters

A plain class can be constructed, exercised and asserted on without a scene, a
window, or a running game. Node code cannot be verified that way at all. So the
proportion of your logic that lives outside nodes is the proportion that is
actually testable.

It also survives a later port to a native extension, because flat data and pure
functions map onto native structures mechanically, while node interaction does
not.

## Autoloads

An autoload is a global. It is appropriate for a genuinely process-wide service
with no alternative: scene transitions, an event bus, a save-file service.

It is not appropriate for configuration or tuning values. Config should be
constructed once and passed in, for three reasons: a system reaching out to a
global is hidden coupling; a global read inside a hot loop becomes a boundary
crossing if that loop is later ported to native code; and tests can construct a
different config instead of mutating global state.

In this project, logic and data layers are forbidden from referencing autoloads
at all, and the gate enforces it.

## Scene granularity

Prefer many small scenes composed by instancing over one large scene. Small
scenes diff cleanly, fail in isolation, and can be validated independently. A
large scene is a merge conflict waiting to happen and tells you nothing about
which part broke.

## Checklist

1. Can this be a plain class? If yes, make it one.
2. If it must be a node, what is the plain class underneath it?
3. Would there be thousands of these? If so, stop and reconsider before writing.
4. Am I adding an autoload? Justify it, or inject instead.

## If the fork is the human's to make

The trade-offs above are yours to weigh, but some forks are not technical
choices at all - they turn on scope, audience, art direction or how much the
project intends to carry. If the right answer here depends on something like
that and nothing in `project.shape.json` decides it, raise it rather than
picking, and record the answer per
`.agents/skills/godot-project-decisions/SKILL.md`.
