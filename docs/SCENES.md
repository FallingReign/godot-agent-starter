# Writing scenes and resources

Read this before creating or editing any `.tscn` or `.tres`. `AGENTS.md` carries the
short version; this is the full set with the reasoning.

### Writing scenes

Godot's text formats are Godot's to interpret. Write the minimum and let the engine
fill in the rest.

**Omit these fields entirely.** They are optional, and Godot assigns them correctly
when it next saves the file:

- `uid="uid://..."` on the `[gd_scene]` / `[gd_resource]` header
- `uid="uid://..."` on `[ext_resource]` lines. Use `path=` only; it is authoritative
- `unique_id="..."` on `[node]` lines. Only exists in 4.6+, churns on every reimport
- `load_steps=N` on the header. Deprecated in 4.6, and you would have to count

**Never invent a `uid://` value.** If you did not read it out of a file, do not write
it. `sanitise.py` deletes UIDs that do not match their target, so a fabricated one is
removed rather than trusted, but it should not be written in the first place.

**Structural rules**, all enforced by `sanitise.py`:

- Exactly one root node, and the root has no `parent=` attribute
- Direct children of the root use `parent="."`
- Deeper nodes use a path excluding the root's own name: `parent="Player/Sprite"`
- Every `parent=` must name a node declared earlier in the same file
- No two nodes may share a name under the same parent
- Every `[ext_resource]` `path=` must exist on disk

**Prefer composition over one large file.** A nested scene is a single line:

```
[node name="Enemy" parent="Spawns" instance=ExtResource("2_enemy")]
position = Vector2(120, 40)
```

Small scenes composed by instancing are idiomatic Godot and keep each file small
enough to review as a diff.

**Do not add nodes beneath a non-root node of an instanced sub-scene.** Godot does
not save them (engine issue #90823). Add to the instance's root, or edit the
sub-scene itself.

**Comments in `.tscn` will not survive.** Godot strips comments and default-valued
properties whenever it re-saves. Put explanation in the `.gd` file instead.

### Escape hatch

If a scene is complex enough that hand-writing it is error-prone, build the tree in
GDScript, run it headlessly, and use `PackedScene.pack()` plus `ResourceSaver.save()`.
The engine then writes the file, so the syntax is correct by construction. Two traps:
`pack()` silently skips any node whose `owner` is not the target root, so set owner
**recursively**; and connections are only serialised when passed `CONNECT_PERSIST`.


## Why this works at all

The kit lets an agent write scenes because the result is verifiable, which is the same
criterion applied to everything else. Two gate stages do the work:

`sanitise` is a deterministic text pass with no third-party dependencies. It deletes
only fields it can prove are wrong, and it verifies every `uid://` against the real
source: a `.gd.uid` sidecar for scripts, the header line for scenes and resources. A
mismatch is deleted, because `path=` is authoritative and Godot logs
`invalid UID ... using text path instead` then loads correctly.

`resources` asks the engine. It loads every `.tscn` and `.tres` and instantiates every
scene, so a malformed file surfaces as `Parse Error`, `Failed loading resource`, or
`node count is 0`. Godot is its own parser, so no third-party library is used or
needed. Third-party `.tscn` parsers exist but are Godot 3 era and hard-fail on modern
scenes containing an `AnimationPlayer`.

## Round-trip expectations

The editor will rewrite a scene you authored the first time it saves it. Expect it to
add a header `uid`, add `unique_id` fields on 4.6+, strip your comments, and drop any
property still at its default. That is normal and not a sign anything was wrong.
