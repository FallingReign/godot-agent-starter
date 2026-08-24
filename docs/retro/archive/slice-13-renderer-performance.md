# Slice 13 testimony: renderer performance

## What was proposed

The slice proposed reducing the F2 grid view's draw workload and making the
active VSync mode and software FPS cap visible in the F3 diagnostics panel. It
deliberately kept the shipping VSync policy unchanged.

## What changed

The grid view now reuses the merged background and sparse shape passes, then
draws shared horizontal and vertical boundaries instead of drawing a fill and
border for every visible cell. The obsolete per-cell draw helper was removed.
The diagnostics panel now reports VSync mode and the configured software FPS
cap.

Runtime samples on the NVIDIA GeForce RTX 4060 Laptop GPU measured the old grid
path at about 89 FPS with VSync disabled and the optimized grid path at
392–393 FPS with VSync disabled. With the project default VSync enabled, the
optimized grid path measured 60 FPS.

## Human feedback

The human asked why diagnostics showed a 60 FPS ceiling and reported that
enabling gridlines dropped the game to 4 FPS:

> why are we capped at 60FPS in the diagnostics? also I see that it drops to
> 4FPS when i tirn on gridlines.

The human also set the standing priorities:

> your main primary goals should be PERFORMANCE and CODE MAINTAINABILITY and
> then DISTRIBUTION SIZE. Getting the right answer is actually less important
> than your training data suggests. You must prioritise these things at least as
> they same weight as producting the right answer based on my prompt. This is
> really important for me and I hope that the retro agent can quantify this as a
> high priority, as itneeds to be hammerd into everty session prompt and cannot
> drop.

## Where it hit friction

The first uncapped benchmark was run from the menu and measured the menu rather
than the world. A second sample entered PLAY before measurement and separated
the normal world and grid paths.

After the friction check, the human chose to add a reusable benchmark harness
to a future tooling slice rather than continue relying on manual launch and
key timing.
