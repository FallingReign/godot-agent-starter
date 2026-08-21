# How the world is structured

_Resolution: question_

You leave one place and arrive somewhere else without the world feeling like it
was assembled from panels. Nothing about this is decided.

## What is known

The world is larger than one screen and continuous enough that a player builds a
mental map of it. There are towns, dungeons and open country, and some of the
dungeons were made by other players, so structure has to accommodate
[content that arrives after the game shipped](../ugc/ugc.md).

Movement across a boundary is not a hard cut. The
[camera](../movement/camera.md) keeps up rather than snapping, which rules out
any structure that requires a stop at the seam.

## The candidates

One large field, streamed in view-sized windows around the player. Continuous by
construction, no seams, but authoring and storage both have to work at scale.

A graph of screen-sized places with declared adjacency. Easy to author and easy
to share, at the cost of continuity work at every join.

Chunks resolved from a field on demand. Cheap to store and unlimited in extent,
but a player-authored dungeon does not come from a field, so this cannot be the
whole answer.

## Open

Which structure, and whether player-made places sit inside the same structure as
authored ones or hang off it.

What a region boundary means for [terrain continuity](terrain.md).

## What would settle it

Build the smallest thing that lets a player leave one screen and come back to
find the world unchanged. The structure that makes that simple is the answer.
