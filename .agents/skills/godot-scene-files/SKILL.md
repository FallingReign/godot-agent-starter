---
name: godot-scene-files
description: Use before writing, editing or creating a Godot scene or resource file as text — .tscn or .tres. Covers what is safe to hand-author versus machine-generated, UID handling and uid:// references, ext_resource and sub_resource, node parent paths, scene instancing, and why a scene fails to load or shows an invalid UID warning. Also use when a scene must be created but no editor session is available.
---

# Authoring scene and resource files as text

## What is safe and what is not

Scene and resource files are line-oriented text and are legitimately editable.
But parts of them are engine bookkeeping, and inventing those is the main way
agent-authored scenes break.

**Safe to author:** node hierarchy, node types, property values, external
resource references by path, instancing another scene, property overrides on an
instance.

**Never invent:** any unique identifier. Identifiers are assigned by the engine.
If you did not read a value out of a file, do not write it. Reference external
resources by path instead — the path is authoritative, and the engine resolves
it correctly even when an identifier is absent, emitting only a warning.

**Omit rather than guess:** optional bookkeeping fields. Several exist purely for
engine convenience and are regenerated on the next save. Omitting them produces
a valid file and a much cleaner diff than a fabricated value.

**Do not author at all:** anything whose text form is a serialised graph rather
than a structure. Animation state machines, tile terrain adjacency data, visual
shader graphs. That text has no meaningful authoring semantics, so editing it is
guessing with extra steps. Hand those back to a human.

## Structural rules that cause hard load failures

- Exactly one root node.
- The root declares no parent.
- Child parent paths are relative to the root and exclude the root's own name.
- Direct children of the root use the shorthand for "the root".
- Every referenced external path must exist on disk.
- No two children of the same parent may share a name.

Breaking any of these produces a loud failure at load, not a silent one, which
is why authoring scenes is verifiable at all.

## Prefer composition

Many small scenes, instanced and overridden, beat one large file. Each instance
is a single line plus its overrides. Small scenes diff cleanly, validate
independently, and tell you which part broke.

## Verification is mandatory

A scene you wrote is not finished until the engine has loaded and instantiated
it. Run the project's checks; do not eyeball the text and declare it correct.

Two distinct things get checked, and both matter: a mechanical pass that strips
fabricated identifiers and other fields you should not have written, and an
engine-side load that proves the file actually resolves.

A passing check proves the scene **loads and builds a tree**. It says nothing
about whether the result looks right. Layout, spacing, anchoring and composition
need human eyes, and you should say so explicitly rather than implying visual
confirmation you cannot provide.

## When a scene is complex

If a scene is large, repetitive, or generated from data, do not hand-write it.
Build the tree in code, then have the engine serialise it. The engine writing the
bytes makes malformed output structurally impossible.

Two traps if you do that: nodes are only saved if their ownership is set to the
target root, recursively — a child of a child with unset ownership is silently
dropped, producing an apparently empty scene. And injecting nodes beneath a
non-root node of an already-instanced sub-scene does not save at all.

## Moving files

A script carries an identifier sidecar. Move both together in one operation, or
every reference to that script breaks. Never edit the sidecar's contents.

## Checklist

1. Did I invent any identifier? Remove it and use a path.
2. Did I omit the optional bookkeeping fields?
3. One root, no parent on the root, correct child paths?
4. Does every referenced path exist?
5. Have I run the checks, and pasted the result?
6. Have I stated that appearance is unverified?

## Where structure belongs: the scene tree or code

The engine's own guidance prefers the declarative option where both work,
because nothing can go wrong in a tree that has no code path. Two rules that
follow from it:

**Structure known at author time belongs in the scene.** A menu with four fixed
buttons, a HUD with a health bar and a minimap frame. Building those in code
means a human cannot see or adjust them without reading a script, and the layout
values end up as magic numbers.

**Structure determined by data belongs in code.** Inventory slots from a variable
item count, damage numbers, enemies from a spawn table, anything with a
per-frame count in the hundreds. Forcing these into the tree defeats the point.

The test is not "is it UI". It is whether the number and identity of the nodes is
known before the game runs.

## Every authored thing needs a human-inspectable surface

The reason static structure belongs in the scene is not that the scene tree is
special. It is that a human must be able to see and change what they authored.
The tree is simply the cheapest surface for scene-shaped content.

Data-oriented content needs a different surface - a tile editor, a stamp editor,
a table view - and that is fine. What is not acceptable is authored content with
no inspection surface at all: hand-edited JSON with no viewer becomes unreviewable
within weeks. If you introduce an authored format, the editor for it is part of
the work, not a later nicety.

## Composing scenes

Consider the tree in relational rather than spatial terms: are these nodes
dependent on their parent existing? If not, they belong elsewhere. A pickup
positioned near a door is not a child of the door.

A sub-scene must instantiate without knowing anything about its surroundings.
Hard node paths reaching upward or outward, and editor-established connections
to nodes outside the scene, both break the moment it is reused. That is also the
mechanical reason behind the connection question below.

## If the fork is the human's to make

The trade-offs above are yours to weigh, but some forks are not technical
choices at all - they turn on scope, audience, art direction or how much the
project intends to carry. If the right answer here depends on something like
that and nothing in `project.shape.json` decides it, raise it rather than
picking, and record the answer per
`.agents/skills/godot-project-decisions/SKILL.md`.
