# GDScript under strict mode

**Generated** by the kit-maintainer documentation tool from reviewed,
project-neutral examples. Do not edit by hand.

The `[debug]` warnings block in `project.godot` promotes untyped and unsafe
operations to errors. That rejects several forms an LLM produces by default.
The examples deliberately contain no source from the configured game.

## typed for-loop iterator

An untyped iterator fails: "for" iterator variable has an implicitly inferred static type.

```gdscript
for label: String in labels:
	print(label)
```

## typed local declaration

An untyped or inferred local fails. Give every local an explicit type.

```gdscript
var elapsed_seconds: float = 0.0
```

## Variant held in a typed local before use

Hold data returned by a loose container as Variant until it has been validated.

```gdscript
var raw_title: Variant = record.get("title", "")
```

## narrowing a Variant to a number

int() and float() reject Variant under strict warnings. Convert through str() after validating the accepted representation.

```gdscript
var count: int = int(str(raw_count))
```

## narrowing a Variant to a Dictionary or Array

Use an `is` branch so the analyser can prove the narrowed type.

```gdscript
var options: Dictionary = {}
if raw_options is Dictionary:
	options = raw_options
```

## typed function signature

Every parameter needs a type and every function needs a return type, including `-> void`.

```gdscript
func retained_strength(value: float) -> float:
	return value
```

## typed constant

An inferred constant fails the same way an inferred variable does.

```gdscript
const DEFAULT_LIMIT: int = 10
```

## signal connection, Godot 4 form

The Godot 3 string form parses but never fires. Connect the Signal object.

```gdscript
confirm_button.pressed.connect(_on_confirm_pressed)
```

## The boundary rule (this is the one that matters)

There is **no general safe cast from Variant** in GDScript. `value as Type` can
trip `unsafe_cast`, direct member access can trip `unsafe_property_access`, and
passing an unchecked Variant to a typed constructor can trip
`unsafe_call_argument`.

The fix is structural, not a trail of casts. Convert external data **once**, at
the boundary, then pass only typed values inward:

- `scripts/data/` is the boundary. It parses, validates, and returns typed
  objects. Loose `Dictionary` and `Variant` are correct here.
- `scripts/logic/` is the interior. It receives typed objects only. It never
  calls `JSON.parse_string`, `FileAccess.open` or `ConfigFile`, and its
  signatures never contain a bare `Dictionary`, `Array` or `Variant`.

The `types` gate stage enforces this direction. If it fires, move conversion to
the boundary and change the interior signature to accept the typed object.

Two mechanisms are safe inside the boundary. `is` narrowing lets the analyser
track the type inside a branch:

```gdscript
static func as_dictionary(value: Variant) -> Dictionary:
	if value is Dictionary:
		return value
	return {}
```

`str()` accepts Variant. After validating that a numeric representation is
allowed, `int(str(value))` provides an explicit conversion path.

For colours stored as text, `Color.from_string(str(value), Color.MAGENTA)` keeps
the external representation readable and provides an explicit fallback.

## Two lint rules that are not about types

`class-definitions-order`: within a class, declare in this order — signals,
enums, constants, static variables, variables, `_init`, then other methods.
gdlint fails the file otherwise.

GUT's dynamic doubles do not provide a stable annotatable type. In a project
that bans inferred declarations, prefer a small typed fake or a partial double
held as the real collaborator type. Do not weaken strict typing just to store a
test double.

## The general rule

Anything reaching you from a `Dictionary`, `Array`, `JSON.parse_string`,
`get()` or `FileAccess` is a `Variant`. Give it a typed home before you
use it. One explicit conversion per value keeps the looseness at the edge.
