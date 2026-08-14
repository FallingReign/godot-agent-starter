# Terrain

_Resolution: direction_

The ground reads as ground. Walking from grass to sand to stone feels like
crossing a place rather than stepping between tiles, and a river reads as one
continuous thing rather than a row of river-shaped pieces.

## Continuity

Terrain is resolved per cell rather than placed as stamps, because a stamp
boundary is exactly what produces a visible seam. What a cell contains is
derived from what surrounds it, so an edge between two surfaces is computed
rather than authored.

That makes continuity a deterministic function of the terrain field, which also
means it can be tested without looking at it.

Built from [the cell grammar](cells.md), which sets what a resolved cell may
contain.

## Surfaces

A surface says how it behaves as well as how it looks. Walkable, blocked, slow,
harmful. Whether that lives with the surface or as a separate layer is
[still open](structure.md#open).

Elevation reads as elevation. A cliff is not a differently coloured floor: the
edge carries a repeated boundary form so the change in height is visible from
the shape alone, which is what the
[colour-independence pillar](../pillars.md) requires here.

## Variation

Repeated surfaces vary within a controlled range so a large area does not read
as wallpaper, without varying so much that a player stops trusting what a
surface means.

## Open

How much variation is tolerable before a surface stops being recognisable.

Whether transitions need dedicated forms or whether neighbour-derived resolution
is enough on its own.
