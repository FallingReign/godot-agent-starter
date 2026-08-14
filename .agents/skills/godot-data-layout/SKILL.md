---
name: godot-data-layout
description: Use when deciding how to store many things - items, modifiers, entities, particles, tiles - or when someone proposes an ECS, data-oriented design, or object pooling. Also use when a system feels slow and the suspected cause is how data is laid out rather than what the code does.
---

# Choosing a data layout

## First, the question that actually decides it

Not "how many things are there". **How many things does work happen to, per frame?**

Those are different numbers and confusing them causes both mistakes:

- 50,000 items in a database, six equipped, modifiers evaluated on equip.
  Per-frame count is **zero**. Plain typed objects. Columnar layout here buys
  nothing and costs clarity.
- 3,000 projectiles, every one moved and collision-checked every frame.
  Per-frame count is **3,000**. Columnar, pooled, bulk-rendered.

Check `project.shape.json` for a decision on per-frame entity count. If there
is none, ask now - you are about to choose a layout and this is the input.

Ask it concretely, about the thing in front of you: "Does anything iterate all
of these every frame, or only the handful on screen?" Do not ask for a number
in the abstract; nobody knows that on day one, and a guessed number is worse
than an asked question. Record the answer per
`.agents/skills/godot-project-decisions/SKILL.md`.

## The three layouts

**Objects.** One `RefCounted` or `Resource` per thing, fields on the object.
Fast to write, easy to debug, easy to serialise. Costs a pointer chase per
access and an allocation per instance.

Choose when: per-frame count is few, or the thing has a rich distinct
behaviour, or a human authors it individually.

**Columns.** One typed array per field, index is identity.
`PackedFloat32Array` positions, `PackedInt32Array` health. A system loops one
or two arrays and touches nothing else.

Choose when: per-frame count is hundreds or thousands, and the work is a
uniform pass over the same fields. This is also the only layout that maps
directly onto a bulk render upload, so it composes with `MultiMesh`.

**Components with dynamic composition (ECS).** Entities are ids, components
are separately stored, systems query by component set.

Choose when: entity kinds vary combinatorially - hundreds of item types with
arbitrary modifier stacks, where an inheritance tree would be unworkable. The
cost is real machinery: id allocation, storage, queries, a scheduler.

## Do not adopt an ECS to go faster

Columns give you the memory-locality win with none of the machinery. An ECS
gives you *composition flexibility*, which is a modelling benefit, not a speed
one. If the reason offered is performance, columns are the cheaper answer and
you should ask for a measurement first.

If the reason is "we have 400 modifier types that combine arbitrarily", that
is the real case and the machinery earns its place.

## Migration is cheap in one direction

Objects to columns is mechanical if systems are already pure functions that
take data and return data. Columns to objects is also fine. What is expensive
is either of those once presentation reaches into storage directly.

So the thing to get right early is not the layout. It is that a system never
reads a node and a node never mutates simulation state - it reads a snapshot.
Get that boundary right and the layout stays a local decision you can revisit
when a measurement tells you to.

## Heterogeneous stores and the typed facade

Generic storage keyed by type name is inherently loose - a registry mapping
`String` to differently-typed arrays cannot be fully typed in GDScript. That
is legitimate, and it is not the untyped-external-data problem.

Confine it: the loose container lives inside one class, and every method
callers actually use returns a concrete type. Callers never handle the loose
value. The facade's own signature is the one place a suppression comment
belongs.

## Grouping on disk

Group by the system that owns the data, not by the layer it belongs to.
Movement columns next to the movement system. That is feature grouping and
data-oriented grouping agreeing rather than competing.

The only separation that must survive is the parse boundary, because that is
the one a machine can check. Everything else is free to reorganise.

## Before optimising anything

There is no layout decision worth making from intuition. Measure first, and
see `godot-performance-evidence` for what counts as evidence.
