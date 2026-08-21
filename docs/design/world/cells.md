# The cell grammar

_Resolution: settled_

Everything you can see is built the same way. Terrain, a chest, an enemy, a
health bar, a menu border. They are all grids of small cells, and each cell
holds one simple shape drawn in two colours. Nothing in the game is drawn by any
other means, so a sword on the ground and the same sword in a panel are made of
the same material.

This section owns the grammar. Every other domain builds within it and links
here rather than restating it.

## How a cell works

One cell holds exactly one shape from a fixed set, plus a rotation in quarter
turns and two palette indices. There is no partial cell and no free-form
geometry.

Rotation is applied when the cell is drawn. A rotated shape is never stored as a
separate shape, so the set stays small and a shape gains four orientations for
free.

Colour is always an index into a palette rather than a literal value. A palette
swap therefore recolours everything at once, which is what makes the
[accessibility pillar](../pillars.md) achievable rather than aspirational.

## The shape set

The set grows one shape at a time and only when something cannot be said
without it. Each addition is drawn, looked at, and kept or discarded on the
evidence.

Starting candidates are the empty cell, a filled square, a triangle, a circle, a
quarter arc, a diagonal, a straight line, and a corner. Whether that is enough
is [an open question](#open).

## Composition

Cells group into stamps. A stamp is any width by height block, so a grass cell
is one by one and a house might be twelve by nine. Nothing assumes a fixed stamp
size, and a stamp is the unit an author places rather than a unit the renderer
knows about.

Stamps layer. A map declares its layers in order and every placement names one,
which is how a tree sits over ground without either being modified.

## Tunables

| Tunable | Range | What it changes |
|---|---|---|
| `cell.pitch` | 12 to 24 | Distance between cell centres in logical units. Sets how much world fits on screen. |
| `cell.inset` | 0 to 3 | Gap inside a cell in logical units. Zero reads as continuous surface, higher reads as tiling. |
| `cell.palette_size` | 8 to 32 | How many colours a map may reference at once. |

<!-- BEGIN GENERATED BINDINGS -->

| Tunable | Value | Set in | Adjustable |
|---|---|---|---|
| `cell.inset` | `1.0` | `src/scripts/main.gd:31` | yes |
| `cell.palette_size` | — | — | **not bound** |
| `cell.pitch` | `20.0` | `src/scripts/main.gd:27` | yes |

- `cell.inset` — Gap inside each cell in logical units so the grid remains visibly composed of cells.
- `cell.pitch` — Distance between cell centres in logical units.

<!-- END GENERATED BINDINGS -->

## Open

Whether the eight starting shapes are enough for a screen to read as a place
rather than a pattern. The answer is a count, found by adding one at a time.

Whether a cell needs more than one shape layered within it, or whether stamp
layering is sufficient.

## What would settle it

Draw a small town with the current set. If it reads, the set is enough. If a
specific thing cannot be said, that names the next shape.
