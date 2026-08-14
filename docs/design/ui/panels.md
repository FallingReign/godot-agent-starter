# Panels

_Resolution: direction_

A panel feels like a piece of the world folded into a frame. It has an edge you
can see, room to breathe, and the thing you selected is unmistakable.

## Construction

A panel is many stamps rather than one large one, so its size is not constrained
by any stamp dimension. Borders and corners are repeated forms, which is why
they read as a frame at any size.

Selection is a structured outline plus a position shift, never a colour change
alone, matching the treatment [the menu](../frontend/menu.md) uses.

Cells inside a panel come from [the cell grammar](../world/cells.md) at a
possibly different pitch to the world, since a panel is read at a fixed distance
rather than a variable one.

## Tunables

| Tunable | Range | What it changes |
|---|---|---|
| `panel.min_cell_size_px` | 8 to 20 | Smallest cell a panel may draw. Below this, shapes stop being distinguishable. |

<!-- BEGIN GENERATED BINDINGS -->

| Tunable | Value | Set in | Adjustable |
|---|---|---|---|
| `panel.min_cell_size_px` | — | — | **not bound** |

<!-- END GENERATED BINDINGS -->

## Open

Whether panels can overlap the world or always occupy their own region.
