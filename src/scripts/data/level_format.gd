class_name LevelFormat
extends RefCounted

## Versioned, data-only level format. OPTIONAL: delete this file entirely if
## you author every level as a .tscn in the editor.
##
## Use this when levels are procedurally generated, or when players get a map
## editor. Owning the format means the same editor code runs in-game and in
## development, and it means levels are diffable text an agent can generate,
## validate and write tests against. None of that is true of .tscn.
##
## Three rules that are painful to retrofit, so they are here from commit one:
##
##   1. VERSIONED. `version` is written on every save and checked on every load.
##      User-generated content must keep loading after the format changes, so
##      migration is a first-class path, not an afterthought.
##   2. DATA ONLY. No script paths, no expressions, no callables. Content from
##      other players is untrusted input. A format that can carry executable
##      content is a security hole you cannot patch later.
##   3. VALIDATED IN ONE PLACE. `validate()` is called by the loader, by the
##      editor before save, and by the gate. A malformed level fails in CI
##      rather than at runtime on a player's machine.

const VERSION: int = 1

## Every key a level may contain, with the type it must be. Anything else is
## rejected at load; unknown keys are how injection attempts arrive.
const SCHEMA: Dictionary = {
	"version": TYPE_INT,
	"name": TYPE_STRING,
	"size": TYPE_VECTOR2I,
	"tiles": TYPE_PACKED_BYTE_ARRAY,
	"spawns": TYPE_ARRAY,
}

var version: int = VERSION
var name: String = ""
var size: Vector2i = Vector2i.ZERO
var tiles: PackedByteArray = PackedByteArray()
var spawns: Array[Vector2i] = []


func to_dictionary() -> Dictionary:
	return {
		"version": VERSION,
		"name": name,
		"size": size,
		"tiles": tiles,
		"spawns": spawns,
	}


## Returns an empty array when valid, otherwise a list of human-readable problems.
func validate() -> PackedStringArray:
	var problems: PackedStringArray = PackedStringArray()
	if version > VERSION:
		problems.append("level version %d is newer than supported %d" % [version, VERSION])
	if size.x <= 0 or size.y <= 0:
		problems.append("size must be positive, got %s" % size)
	var expected: int = size.x * size.y
	if tiles.size() != expected:
		problems.append("tiles has %d entries, size implies %d" % [tiles.size(), expected])
	for spawn: Vector2i in spawns:
		if spawn.x < 0 or spawn.y < 0 or spawn.x >= size.x or spawn.y >= size.y:
			problems.append("spawn %s outside bounds %s" % [spawn, size])
	return problems


static func from_dictionary(raw: Dictionary) -> LevelFormat:
	var level: LevelFormat = LevelFormat.new()
	for key: String in SCHEMA:
		if not raw.has(key):
			continue
		var value: Variant = raw[key]
		if typeof(value) != SCHEMA[key]:
			continue
		level.set(key, value)
	level = _migrate(level, int(str(raw.get("version", 0))))
	return level


## Add a branch per format bump. Never delete an old branch: content created
## with version 1 must still load in version 9.
static func _migrate(level: LevelFormat, from_version: int) -> LevelFormat:
	if from_version < 1:
		level.version = 1
	return level


static func load_from(path: String) -> LevelFormat:
	if not FileAccess.file_exists(path):
		return null
	var text: String = FileAccess.get_file_as_string(path)
	var parsed: Variant = JSON.parse_string(text)
	if typeof(parsed) != TYPE_DICTIONARY:
		return null
	var dict: Dictionary = parsed
	return from_dictionary(dict)


func save_to(path: String) -> Error:
	var problems: PackedStringArray = validate()
	if not problems.is_empty():
		push_error("refusing to save invalid level: %s" % String(", ").join(problems))
		return ERR_INVALID_DATA
	var file: FileAccess = FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		return FileAccess.get_open_error()
	file.store_string(JSON.stringify(to_dictionary(), "\t"))
	return OK
