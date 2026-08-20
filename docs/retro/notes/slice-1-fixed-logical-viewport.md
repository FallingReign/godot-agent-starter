# slice-1-fixed-logical-viewport
_2026-08-14_

## Proposed

2 modules, 3 files, and runtime setup for a fixed 1920x1080 logical viewport,
20-unit cells, uniform scaling, and letterboxing on non-16:9 windows.

## Changed during the slice

The runtime window and content-scale configuration moved into `scripts/main.gd`.
The existing map remained independent of the logical viewport and continued to
be clipped rather than resized.

## Corrections the human made

"the plan was deleted as I updated the documents, so you won't have anything
previously to evolve from."

The plan was rebuilt from the current proposal, and previously delivered files
and functions were carried forward into the proposal so they remained visible
while the viewport correction was described.

## What I got wrong

The proposal did not preserve enough prior approval context when the working
proposal was replaced. Authored JSON content was also initially classified
differently from authored GDScript in the plan view.

## Friction

Plan reconciliation involved repeated regeneration and review of file status.
The viewport itself was launched for visual judgement, while the automated gate
only established that the scene loaded and booted.
