# Slice 12 testimony: performance diagnostics

## What was proposed

The slice proposed an optional F3 screen-fixed diagnostics panel showing FPS,
frame time, process and physics timings, render workload, object counts, RAM,
and renderer memory. It deliberately did not optimize or rewrite the renderer
before collecting measurements.

## What changed

The main presentation node now toggles the panel with F3 during PLAY. It reads
Godot's runtime monitors for FPS, frame/process/physics timings, draw calls,
primitives, render objects, total objects, orphan nodes, static memory, video
memory, texture memory, and buffer memory, formatting byte values for scanning.
The plan was revised to enumerate the earlier movement/map files still present
since the proposal baseline, so conformance no longer reports those modules as
unplanned.

After the first overlay pass, the human reported 4 FPS while stationary. The
map renderer was then changed to avoid queueing redraws when the player and
camera are unchanged, merge contiguous background cells, draw only non-empty
shapes in the normal view, and update diagnostics from a separate PanelContainer
so metric refreshes do not invalidate the map canvas.

## Human feedback

The human reported that the plan omitted file and function detail and showed
unplanned work again:

> your plan is missing a bunch of details on files and functions and showing
> unplanned again. I am really tired of wasting tokens redoing plans so just go
> ahead an make the change.

They also required hosted plan links instead of the static file:

> make sure you update the plan to show what you modified after and stop linking
> me to the statc version of the plan, link to the hosted server one.

## Where it hit friction

The initial proposal baseline was the last committed slice boundary while the
previous map and movement work remained uncommitted. The first conformance run
therefore surfaced valid existing source as unplanned until the proposal
inventory was expanded.

The first diagnostics implementation coupled live metric text to Main's
expensive map draw pass, so the tool itself preserved the stationary redraw
cost it was meant to measure until the panel was separated.
