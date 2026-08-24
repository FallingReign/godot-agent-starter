# Slice 10 testimony: shape vocabulary batch

## What was proposed

The slice proposed a visual probe for circle, right-angle triangle, straight-edged
half-cell, and corner primitives. The existing isosceles triangle remained in
the vocabulary, and the half-cell was clarified as a rectangle filling half the
cell with quarter-turn rotations.

## What changed

The catalog gained `CI`, `RT`, `HC`, and `CO` tokens and normalized polygon
geometry. The authored map now contains a four-row probe with four rotations of
each shape, while the original landmarks remain in their prior positions. Tests
cover the new catalog entries and authored tokens.

## Human feedback

The human reported that the proposal mockup was not useful:

> Mockups have been fairly useless. [image: copilot-image-db4a04.png] as you
> can see.

They also reported that the plan link was still pointing to the static file
instead of the hosted variant:

> I also notice you are still linking me to a plan that is the satic file
> instead of the hosted variant.

They approved development and asked whether rasterized images would improve
performance:

> Woudl this be more performant if I make these as rasterised images?
