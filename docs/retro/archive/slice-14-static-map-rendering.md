# Slice 14 testimony: static map rendering

## What was proposed

The slice proposed moving static map drawing out of the movement redraw path so
camera-following movement would update a presentation transform rather than
rebuild the map.

## What changed

The map now renders into a one-shot SubViewport texture. A Sprite2D pans that
texture as the camera follows the circle, while Main redraws only its fixed
background and dynamic player marker. F2 grid changes request one new viewport
render instead of adding per-frame map work.

The first direct retained-CanvasItem implementation was measured at 37–60 FPS
even while stationary because the full 3,840×2,160 world was still rasterized
each frame. After revising to the viewport texture, controlled held movement
measured 390–393 FPS uncapped in both normal and F2 grid views after the
one-time texture warm-up.

## Human feedback

The human reported intermittent movement hitches:

> hmmm occasionally walking around i see a framerate drop down to 11 or so FPS.
> not sure why it has hitches like that

The human approved the static-map rendering draft and then approved the revised
one-shot SubViewport texture approach after the direct CanvasItem sample exposed
the remaining full-world rasterization cost.

## Where it hit friction

The first implementation reduced script-side redraw work but did not reduce the
GPU's full-world rasterization work, so it had to be replaced before the final
runtime comparison.
