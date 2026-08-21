# Scale and view distance

_Resolution: settled · updated 2026-08-14_

Standing in a town you can see the shape of a building and the door you are
walking towards. Pulling back, individual cells stop mattering and the road you
are following becomes the thing you read. The world does not become a different
picture when you zoom; it becomes a simpler one.

## Fixed presentation scale

The game preserves the same apparent world scale across common monitor
resolutions. A 4K display should make the same 1920×1080 logical view clearer,
not reveal four times as much map or make cells and interface elements feel
smaller. A 1080p or 720p display should likewise show the same intended game
view without enlarging the world beyond recognition.

The logical viewport and default window are therefore 1920×1080 for the current
presentation direction, with the whole view scaled uniformly to other window
sizes. On displays that are not 16:9, preserve the whole logical view with
letterboxing rather than stretching or cropping it.

At the permanent logical cell pitch, the viewport presents a deliberately
chosen amount of world context rather than imposing a map-size constraint:
authored maps can be larger or smaller, and the viewport shows the portion that
fits within the logical frame.

## How distance simplifies

The same world data is drawn at every distance, with detail removed rather than
replaced. Small features drop out, colours consolidate, and landmarks keep their
silhouette so orientation survives. A regional view is the world with less in
it, not a map of the world.

Simplification uses [the same cell grammar](cells.md) at every distance. There
is no separate map art.

## What must survive

Routes, biome boundaries and landmarks. If a player can navigate at close range
using a road, that road has to still be legible when they pull back to plan.

Where the camera sits and how it moves belongs to
[the camera](../movement/camera.md).

## Settled fullscreen scale

The fullscreen presentation uses the pitch selected through live comparison
against the concept image. It keeps individual cell compositions clear while
preserving enough navigational context to follow routes and landmarks.

## Tunables

| Tunable | Range | What it changes |
|---|---|---|
| `view.cells_wide_target` | 60 to 200 | How many cells fill the width at the default view. |
| `view.zoom_step` | 1.25 to 2.0 | Ratio between adjacent view distances. |

<!-- BEGIN GENERATED BINDINGS -->

| Tunable | Value | Set in | Adjustable |
|---|---|---|---|
| `view.cells_wide_target` | — | — | **not bound** |
| `view.zoom_step` | — | — | **not bound** |

<!-- END GENERATED BINDINGS -->

## Open

How many distinct view distances are worth having.

Whether a globe or whole-world view is a view distance or something else
entirely.
