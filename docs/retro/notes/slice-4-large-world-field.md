# Slice 4: large world field

## What was proposed

The world-structure options were laid out before implementation: one large
field, a graph of screen-sized maps, on-demand field chunks, and a hybrid.
The human chose one large world coordinate space, with streaming deferred until
later so the whole world is not loaded in one shot.

The implementation proposal covered a two-screen-wide authored map, a fixed
logical viewport, smooth bounded arrow-key panning, a Home reset, and no player
avatar.

## What changed

The world direction was recorded in `project.shape.json` and
`docs/design/world/structure.md`. The existing authored map was expanded to
192 by 54 cells, and the presentation layer now pans across it while retaining
the existing cell renderer and map format.

The authored-map test now checks the larger dimensions and a landmark beyond
the initial view. The gate passed after the proposal was approved.

## What the human corrected

The first proposal used `src/...` prefixes for module paths. The human noticed
that the plan rendered the existing `scripts/data` module and its catalog as
unplanned. The proposal was corrected to use the architecture's `src`-relative
module and file paths, and the existing data dependency was explicitly
accounted for.

The human also corrected the review handoff: the live hosted plan should be the
main link so retrospective navigation works, with the local file URI retained
only as a backup.

## Where it hit friction

The proposal required several regeneration and gate passes while the module
and file path conventions were corrected. The larger map was extended in the
existing compact row format rather than introducing a generator or editor in
this probe.

During review, the human reported very low performance while panning between
screens. The renderer was submitting every cell in the full two-screen field
and one grid rectangle per cell on every pan frame; the implementation was
updated to draw only visible cells and submit the visible grid as one multiline
draw.

The human then asked for an in-game performance readout. F3 now toggles a
top-corner overlay showing FPS, frame time, draw calls, primitives, and GPU
video memory; live GPU utilization is not exposed by the engine monitor API.

The human reported that asking the agent to launch the game is cumbersome and
spends tokens, and asked whether the hosted plan could become the control hub
with a local game-launch action. They also reported that the generated HTML
does not make it clear how to reopen the agent session that did the work, and
asked whether a resume action or an SDK-backed conversation surface could
remove that friction.

When the scale probe was discussed, the human clarified that F3 is already in
use and should be reused to cycle live pitches rather than introducing another
debug key. The final fullscreen pitch remains undecided until that comparison
has been seen in the running game.

The human asked for at least five probe sizes that divide cleanly across
mainstream resolutions and delegated the exact set. The chosen comparison set
is 12, 15, 20, 24, 30, and 60 logical units per cell, retaining 20 as the
current reference while keeping common 16:9 scale factors integral.

The human clarified that F3 should not be repurposed for this temporary
experiment. F4 was selected as a separate reversible key, leaving F3 as the
performance-overlay toggle.

During review of the next experiment proposal, the human reported that the
plan had become hard to understand, with misleading Mermaid status diagrams
and too much status-oriented information. They asked for clearer free-form
experiment and post-choice reimplementation text, a logical rollback commit
for the throwaway work, and a visible scale ID so preferences can be reported
without ambiguity.
