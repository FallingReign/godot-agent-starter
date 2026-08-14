# Loot

_Resolution: direction_

You see something on the ground and you have a rough idea what it is before you
pick it up. A weapon looks like a weapon. When it goes into your inventory it
still looks like the same object, because it is drawn the same way in both
places.

## One object, several presentations

An item has a single visual identity that survives being on the ground, being
carried, being equipped, and being shown in a panel. The blade shape and the
colour accent persist; only the surrounding framing changes.

This is only possible because everything uses
[the cell grammar](../world/cells.md), so an inventory icon is not a redrawing
of the object.

## On the ground

A dropped item is visible without shouting. A compact composition, a small base
marker so it reads as sitting on the ground rather than floating, and slightly
more emphasis when the player is close enough to take it.

Items do not become conventional floating icons. The thing on the ground is the
thing.

## What a player can tell before reading

Category and rough material from shape, condition from wear in the composition.
How quality is communicated belongs to [rarity](rarity.md).

Combat produces most of it, so this connects to
[combat](../combat/combat.md).

## Open

Whether items stack, and what that does to a compact composition.

Whether equipment visibly changes the character.
