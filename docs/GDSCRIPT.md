# GDScript under strict mode

**Generated** by `tools/gen_gdscript_doc.py` from code in this repo that
currently passes the gate. Do not edit by hand — regenerate instead.

The `[debug]` warnings block in `src/project.godot` promotes untyped and unsafe
operations to errors. That rejects several forms an LLM produces by default.
Every example below is a real line from this repo.

## typed for-loop iterator

An untyped iterator fails: "for" iterator variable has an implicitly inferred static type.

```gdscript
for key: String in overrides:    # scripts/data/game_config.gd:31
for entry: Dictionary in get_property_list():    # scripts/data/game_config.gd:50
for spawn: Vector2i in spawns:    # scripts/data/level_format.gd:63
```

## typed local declaration

An untyped or inferred local fails: cannot infer the type of a variable because the value doesn't have a set type.

```gdscript
var tick_rate: int = 60    # scripts/data/game_config.gd:24
var move_speed: float = 6.0    # scripts/data/game_config.gd:25
var gravity: float = 24.0    # scripts/data/game_config.gd:26
```

## Variant held in a typed local before use

Calling a method or passing a Variant directly fails. Assign it to a typed local first, or narrow it.

```gdscript
var value: Variant = raw[key]    # scripts/data/level_format.gd:74
var parsed: Variant = JSON.parse_string(text)    # scripts/data/level_format.gd:94
```

## narrowing a Variant to a number

int() and float() reject Variant: argument 1 should be "int" but is "Variant". Wrap in str() or assign to a typed local first.

```gdscript
level = _migrate(level, int(str(raw.get("version", 0))))    # scripts/data/level_format.gd:78
```

## narrowing a Variant to a Dictionary or Array

An unsafe cast fails. Type the destination explicitly and check the source type first.

```gdscript
var spawns: Array[Vector2i] = []    # scripts/data/level_format.gd:40
var dict: Dictionary = parsed    # scripts/data/level_format.gd:97
var raw: Dictionary = _valid_level().to_dictionary()    # tests/unit/test_level_format.gd:43
```

## typed function signature

Every parameter needs a type and every function needs a return type, including -> void.

```gdscript
func _init(overrides: Dictionary = {}) -> void:    # scripts/data/game_config.gd:30
static func from_dictionary(raw: Dictionary) -> LevelFormat:    # scripts/data/level_format.gd:69
static func _migrate(level: LevelFormat, from_version: int) -> LevelFormat:    # scripts/data/level_format.gd:84
```

## typed constant

An inferred const fails the same way an inferred var does.

```gdscript
const VERSION: int = 1    # scripts/data/level_format.gd:24
const SCHEMA: Dictionary = {    # scripts/data/level_format.gd:28
const MAIN_SCENE_PATH: String = "res://scenes/main.tscn"    # tests/smoke_test.gd:8
```

## signal connection, Godot 4 form

The Godot 3 string form parses but never fires. Use the Signal object.

_No example in this repo yet._

## The boundary rule (this is the one that matters)

There is **no safe cast from Variant** in GDScript. `value as Type` trips
`unsafe_cast`, direct assignment trips `unsafe_property_access`, and
`Color(variant)` or `int(variant)` trip `unsafe_call_argument`. So fixing
individual use sites never converges: each fix reveals the next one downstream.
A field report of this loop ran for 242 turns and 19M tokens without finishing.

The fix is structural, not syntactic. Convert external data **once**, at the
boundary, then pass only typed values inward:

- `src/scripts/data/` is the boundary. It parses, validates, and returns typed
  objects. Loose `Dictionary` and `Variant` are correct here.
- `src/scripts/logic/` is the interior. It receives typed objects only. It never
  calls `JSON.parse_string`, `FileAccess.open` or `ConfigFile`, and its
  signatures never contain a bare `Dictionary`, `Array` or `Variant`.

The `types` gate stage enforces this direction. If it fires, do not add casts:
move the conversion to the boundary and change the signature to accept the
typed object.

Two mechanisms are safe inside the boundary. `is` narrowing, because the
analyser tracks the type inside the branch:

```gdscript
static func as_dict(v: Variant) -> Dictionary:
	if v is Dictionary:
		return v
	return {}
```

And `str()`, because it is vararg and accepts Variant. That is why
`int(str(v))` works where `int(v)` does not.

For colours, store hex strings in your data format and use
`Color.from_string(str(v), Color.MAGENTA)`. One call, no per-channel unpacking,
and hex is readable to anyone hand-editing content.

## Two lint rules that are not about types

`class-definitions-order`: within a class, declare in this order — signals,
enums, constants, static variables, variables, `_init`, then other methods.
gdlint fails the file otherwise.

GUT doubles have no annotatable type. `stub()` and `double()` return an untyped
object, and there is no `Stub` class in scope, so a type annotation cannot be
satisfied. Assign with `:=` and let inference handle it, or use
`partial_double()` and hold the result as the real class. Inventing a type name
produces `Could not find type "Stub" in the current scope`, which under `-d`
sends Godot's debugger into a break loop.

## The general rule

Anything reaching you from a `Dictionary`, `Array`, `JSON.parse_string`,
`get()` or `FileAccess` is a `Variant`. Give it a typed home before you
use it. One extra line per value, and the error disappears.
