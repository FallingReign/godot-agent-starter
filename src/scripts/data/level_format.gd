class_name LevelFormat
extends RefCounted

## Versioned, data-only map format. JSON values are converted here once into
## typed map data before the renderer or a future editor can use them.

const VERSION: int = 2
const SCHEMA: PackedStringArray = ["version", "name", "size", "palette", "layers"]
const _HEX_DIGITS: String = "0123456789ABCDEF"

var version: int = VERSION
var name: String = ""
var size: Vector2i = Vector2i.ZERO
var palette: PackedColorArray = PackedColorArray()
var layers: Array[LevelLayer] = []
var _conversion_problems: PackedStringArray = PackedStringArray()


func to_dictionary() -> Dictionary:
	var palette_values: Array[String] = []
	for color: Color in palette:
		palette_values.append("#" + color.to_html(false))
	var serialized_layers: Array[Dictionary] = []
	for layer: LevelLayer in layers:
		serialized_layers.append(layer.to_dictionary())
	return {
		"version": version,
		"name": name,
		"size":
		{
			"width": size.x,
			"height": size.y,
		},
		"palette": palette_values,
		"layers": serialized_layers,
	}


func validate() -> PackedStringArray:
	var problems: PackedStringArray = _conversion_problems.duplicate()
	if version < 1 or version > VERSION:
		problems.append("level version %d is not supported; expected 1 or %d" % [version, VERSION])
	if name.is_empty():
		problems.append("name must not be empty")
	if size.x <= 0 or size.y <= 0:
		problems.append("size must be positive, got %s" % size)
	if palette.is_empty() or palette.size() > 32:
		problems.append("palette must contain 1 to 32 colours")
	if layers.size() != 1:
		problems.append("version 1 maps must contain exactly one layer")
	for layer: LevelLayer in layers:
		problems.append_array(layer.validate(size, palette.size(), version))
	return problems


static func from_dictionary(raw: Dictionary) -> LevelFormat:
	var level: LevelFormat = LevelFormat.new()
	for key: String in raw:
		if not SCHEMA.has(key):
			level._conversion_problems.append("map has unknown key '%s'" % key)

	var raw_version: Variant = raw.get("version", 0)
	if _is_integer_value(raw_version):
		level.version = int(str(raw_version))
	else:
		level._conversion_problems.append("version must be an integer")

	var raw_name: Variant = raw.get("name", "")
	if typeof(raw_name) == TYPE_STRING:
		level.name = str(raw_name)
	else:
		level._conversion_problems.append("name must be a string")

	var raw_size: Variant = raw.get("size", {})
	if typeof(raw_size) == TYPE_DICTIONARY:
		var size_dictionary: Dictionary = raw_size
		for key: String in size_dictionary:
			if key != "width" and key != "height":
				level._conversion_problems.append("size has unknown key '%s'" % key)
		var raw_width: Variant = size_dictionary.get("width", 0)
		var raw_height: Variant = size_dictionary.get("height", 0)
		if _is_integer_value(raw_width) and _is_integer_value(raw_height):
			level.size = Vector2i(int(str(raw_width)), int(str(raw_height)))
		else:
			level._conversion_problems.append("size width and height must be integers")
	else:
		level._conversion_problems.append("size must be an object")

	var raw_palette: Variant = raw.get("palette", [])
	if typeof(raw_palette) == TYPE_ARRAY:
		var palette_values: Array = raw_palette
		for color_value: Variant in palette_values:
			if typeof(color_value) != TYPE_STRING:
				level._conversion_problems.append("palette entries must be hex strings")
				continue
			var color_text: String = str(color_value)
			if not _is_hex_color(color_text):
				level._conversion_problems.append("invalid palette colour '%s'" % color_text)
				continue
			level.palette.append(Color.from_string(color_text, Color(0.0, 0.0, 0.0, 1.0)))
	else:
		level._conversion_problems.append("palette must be an array")

	var raw_layers: Variant = raw.get("layers", [])
	if typeof(raw_layers) == TYPE_ARRAY:
		var layer_values: Array = raw_layers
		for layer_value: Variant in layer_values:
			if typeof(layer_value) != TYPE_DICTIONARY:
				level._conversion_problems.append("layers must contain objects")
				continue
			var layer_dictionary: Dictionary = layer_value
			level.layers.append(LevelLayer.from_dictionary(layer_dictionary, level.version))
	else:
		level._conversion_problems.append("layers must be an array")
	return level


static func load_from(path: String) -> LevelFormat:
	if not FileAccess.file_exists(path):
		push_error("map file not found: %s" % path)
		return null
	var text: String = FileAccess.get_file_as_string(path)
	var parsed: Variant = JSON.parse_string(text)
	if typeof(parsed) != TYPE_DICTIONARY:
		push_error("map file is not a JSON object: %s" % path)
		return null
	var dict: Dictionary = parsed
	var level: LevelFormat = from_dictionary(dict)
	var problems: PackedStringArray = level.validate()
	if not problems.is_empty():
		push_error("refusing invalid map %s: %s" % [path, String(", ").join(problems)])
		return null
	return level


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


static func _is_hex_color(value: String) -> bool:
	if value.length() != 7 or not value.begins_with("#"):
		return false
	for index: int in range(1, value.length()):
		if _HEX_DIGITS.find(value.substr(index, 1).to_upper()) < 0:
			return false
	return true


static func _is_integer_value(value: Variant) -> bool:
	if typeof(value) == TYPE_INT:
		return true
	if typeof(value) == TYPE_FLOAT:
		var float_value: float = value
		return float_value == float(int(float_value))
	return false
