# slice-1-map-backed-cell-screen
_2026-08-14_

## Proposed

2 modules, 4 files, and the typed map parser, layer validator, serializer,
loader, and map-cell drawing functions. The slice was intended to put a
versioned hand-authored map on disk, load it through typed data, and show it in
the running game.

## Changed during the slice

The map format gained a version field, strict unknown-key rejection, palette
parsing, one named row layer, repeated row tokens, and typed cell access.
`LevelLayer` was added under `src/scripts/data/`, and the authored map was
added under `src/content/maps/`.

## Corrections the human made

"only if everything derrives from desing. I did not see that check."

The proposal and gate presentation were revised so the slice cited settled
design sections and the plan showed the relevant design references.

## What I got wrong

The first proposal did not make design derivation visible enough. The new
`class_name` script was also not visible to per-file typechecking until Godot
refreshed its global script-class cache.

## Friction

Typechecking was repeated while the new class cache was stale. The authored
map and parser were also revised while the plan and conformance view were
being reconciled.
