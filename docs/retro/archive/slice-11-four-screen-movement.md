# Slice 11 testimony: four-screen movement

## What was proposed

The slice proposed expanding the authored map to a contiguous 2×2 field and
letting a circle move continuously through it while a following camera stayed
inside the world rectangle. Arrow keys and WASD were included as controls, with
the existing PLAY, F2, and Escape flow retained.

## What changed

The map is now 256×144 cells, with distinct authored areas in each quadrant.
`CharacterMotion` is a pure `RefCounted` class with injected movement tuning,
acceleration, stopping, diagonal normalization, and world clamping. The main
node reads keyboard state, translates the map under a clamped camera, and draws
the circle marker. GUT coverage now includes motion boundaries and the enlarged
map dimensions.

## Human feedback

The human resolved the layout fork as:

> Contiguous 2×2 world with a following camera

No other corrections were made during this slice.

## Where it hit friction

The authored map was expanded with a one-off content transformation because
the current persisted format stores rows explicitly. No reusable authoring tool
was added for this single probe.
