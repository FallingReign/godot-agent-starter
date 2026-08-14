# Moving through the world

_Resolution: settled_

You hold a direction and the character goes that way. There is a short ramp up
rather than an instant start, so movement has a little weight without feeling
like you are negotiating with it. Release and it settles quickly.

The world is built from cells but you do not move in cells. You stand halfway
between two of them if you like. The grid is how the world is constructed, not
how you traverse it.

## Behaviour

Movement is continuous on both axes and independent per axis, so a wall stops
you only along the blocked direction. Holding a diagonal against a wall slides
you along it rather than stopping you dead.

Speed does not depend on facing. Diagonal movement is normalised so the corner
is not a shortcut.

Where you can stand is legible before you try it, which is a
[terrain](../world/terrain.md) concern rather than a movement one.

## Not grid locked

There is no snap, no step timing and no queued input. A player who wants to
stand on a cell boundary can. Anything that requires alignment to a cell asks
for it explicitly rather than movement providing it.

## Tunables

| Tunable | Range | What it changes |
|---|---|---|
| `movement.max_speed` | 180 to 420 | Peak speed in pixels per second. Above 380 it starts to feel floaty. |
| `movement.accel_time` | 0.0 to 0.2 | Seconds to reach full speed. Zero is instant and reads as weightless. |
| `movement.stop_time` | 0.0 to 0.15 | Seconds to stop. Longer than 0.1 feels like ice. |
| `movement.wall_slide_friction` | 0.0 to 0.4 | How much speed is lost sliding along a wall. |

<!-- BEGIN GENERATED BINDINGS -->

| Tunable | Value | Set in | Adjustable |
|---|---|---|---|
| `movement.accel_time` | — | — | **not bound** |
| `movement.max_speed` | — | — | **not bound** |
| `movement.stop_time` | — | — | **not bound** |
| `movement.wall_slide_friction` | — | — | **not bound** |

<!-- END GENERATED BINDINGS -->

## Open

Whether a dash or roll exists, and if so whether it is a movement concern or
[a combat one](../combat/combat.md).
