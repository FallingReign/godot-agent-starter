# Camera

_Resolution: direction_

The view follows you without you thinking about it. Moving in one direction, the
camera trails slightly then catches up. Stopping, it settles. It never jumps,
and it never waits at a boundary for the world to load.

## Behaviour

The camera is not aligned to cell boundaries. It sits wherever the player is,
sub-cell, because [movement is continuous](movement.md) and a camera that snaps
to cells would reintroduce the grid the player was free of.

A small dead zone means tiny movements do not move the view. Beyond it, the
camera follows with a short lag so direction changes read as intent rather than
as camera noise.

What the camera shows at each distance belongs to
[scale and view distance](../world/scale-and-camera.md).

## Tunables

| Tunable | Range | What it changes |
|---|---|---|
| `camera.follow_lag` | 0.0 to 0.3 | Seconds of trail behind the player. Zero is rigid. |
| `camera.deadzone_px` | 0 to 48 | Radius the player moves within before the view moves. |
| `camera.zoom_default` | 1.0 to 3.0 | Starting view distance. |

<!-- BEGIN GENERATED BINDINGS -->

| Tunable | Value | Set in | Adjustable |
|---|---|---|---|
| `camera.deadzone_px` | — | — | **not bound** |
| `camera.follow_lag` | — | — | **not bound** |
| `camera.zoom_default` | — | — | **not bound** |

<!-- END GENERATED BINDINGS -->

## Open

Whether the camera leads in the direction of travel or only follows.

What happens at a world edge, which depends on
[world structure](../world/structure.md).
