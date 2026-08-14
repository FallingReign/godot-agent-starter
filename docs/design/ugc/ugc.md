# Player-made content

_Resolution: direction_

Someone builds a dungeon and you play it. It does not look like a mod or a
lesser version of the real thing, because it was built from exactly what the
game is built from.

## Why this is a constraint and not a feature

If players author content, the way content is represented has to be right from
the first commit. Retrofitting authorability means changing the format after
content exists, and content that exists has to keep loading.

That is why [the format](format.md) is decided early and conservatively, and why
[the cell grammar](../world/cells.md) is small enough that a player can work
within it.

## What a player makes

Places, primarily. Dungeons, rooms, arrangements of terrain and objects. Whether
they can make items, enemies or behaviour is open and deliberately not being
answered yet.

Content moves between players directly because there is
[no server](../multiplayer/multiplayer.md), which puts a hard ceiling on how
large a shareable place can be.

How a player builds is [authoring](authoring.md). What stops hostile content is
[moderation](moderation.md).

## Open

Whether authored content can include behaviour, or only arrangement.

Whether a player-made place can be part of the persistent world or only entered
deliberately.
