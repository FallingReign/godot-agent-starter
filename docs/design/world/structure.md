# How the world is structured

_Resolution: direction_

You leave one place and arrive somewhere else without the world feeling like it
was assembled from panels. The current direction is one large world coordinate
space, with streaming added later so the whole world is not loaded at once.

## What is known

The world is larger than one screen and continuous enough that a player builds a
mental map of it. There are towns, dungeons and open country, and some of the
dungeons were made by other players, so structure has to accommodate
[content that arrives after the game shipped](../ugc/ugc.md).

Movement across a boundary is not a hard cut. The
[camera](../movement/camera.md) keeps up rather than snapping, which rules out
any structure that requires a stop at the seam.

## Current direction

The overworld is one large field in one coordinate space. This slice should
prove movement out of the current view and back into it while keeping the
field's contents unchanged. It does not decide the eventual region or chunk
size, the streaming policy, or how authored dungeons and towns attach to the
field.

## The candidates

### One large field with a moving camera

Maps are authored as one coordinate space, larger than the fixed 96 by 54 cell
view. The editor can work on regions of that field, but a place file needs a
large footprint or a region/chunk convention so authors are not forced to
handle one enormous document. The camera crosses any coordinate without a
topological seam; unloaded content must be hidden by streaming rather than by
stopping the player.

Towns and open country fit naturally as areas in the same field. A dungeon can
also be an area in that field, but a player-made dungeon then needs a reserved
region, a separate loaded field, or a portal transition, so "same world" does
not automatically mean "same document."

### A graph of screen-sized places

Maps remain small, authored as one screen each, and a world document declares
which place is next in each direction. Authors must prepare matching edges or
an explicit transition rule for every connection. The seam can be hidden by
overlapping neighbour maps and keeping both loaded while the camera crosses,
but mismatched terrain, lighting, or cell coordinates can still make the join
read as a panel boundary.

Towns and dungeons become named places in the same graph, which makes them easy
to share and replace. Open country needs many connected maps or a different
kind of node, and a player-made dungeon can be a graph branch without changing
the overworld's storage.

### Chunks resolved from a field on demand

Maps are authored as a compact field rule or chunk set, with the runtime
resolving only nearby chunks. This can make the world very large without one
large file, but authoring must expose stable chunk coordinates and deterministic
neighbour context. A seam is mathematically absent when adjacent chunks use the
same boundary rule; otherwise every chunk edge needs authored continuity data.

This suits open country especially well. Towns need authored overrides or
hand-placed chunks, and a player-made dungeon cannot be represented by a field
rule alone: it needs an authored place attached to, or entered from, the
resolved world.

## Open

How the large field is divided for future streaming without changing authored
coordinates.

Whether towns and player-made dungeons sit inside the same field or attach as
separate authored places, and how their entrances preserve the no-hard-cut
boundary rule.

What a region boundary means for [terrain continuity](terrain.md).

## What would settle it

Build the smallest thing that lets a player leave one screen and come back to
find the world unchanged. The structure that makes that simple is the answer.
