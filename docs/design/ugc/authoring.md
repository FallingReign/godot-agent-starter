# Authoring

_Resolution: direction_

You are standing in the world, you open an editor, and you place things. Then
you close it and you are playing again. It is the same editor a developer used
to build the shipped world.

## One editor

The tool a player uses is the tool the game was made with. Not a reduced
version: the same one. A separate developer-only editor means two tools, two
formats and a permanent asymmetry between authored and player-made places.

That means the editor works against the running game rather than against the
engine's own editor, and it reads and writes
[the content format](format.md) directly.

It is also the answer to a practical problem: hand-writing a grid of cells is
miserable for a person and error-prone for an agent, and a crude editor is
cheaper than doing it by hand repeatedly.

## What authoring produces

Placements of stamps on layers, within [the grammar](../world/cells.md).
Nothing an author does escapes what the grammar allows, which is what keeps
player-made places consistent with authored ones.

Why this exists at all is [player-made content](ugc.md).

## Open

Whether the editor is available during a session or only outside one.

Whether authors can define new stamps or only place existing ones.
