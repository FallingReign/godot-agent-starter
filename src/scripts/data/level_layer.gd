class_name LevelLayer
extends RefCounted

## One named, row-oriented layer in a map document.
##
## Version 1 tokens are two characters: E or S followed by one hexadecimal
## palette index. Version 2 tokens are five characters: a two-character shape
## id, foreground palette index, background palette index, and quarter turn.

const EMPTY_SHAPE: int = CellShapeCatalog.EMPTY_SHAPE
const FILLED_SQUARE_SHAPE: int = CellShapeCatalog.FILLED_SQUARE_SHAPE
const TRIANGLE_SHAPE: int = CellShapeCatalog.TRIANGLE_SHAPE

const _HEX_DIGITS: String = "0123456789ABCDEF"
const _V1_TOKEN_WIDTH: int = 2
const _V2_TOKEN_WIDTH: int = 5
const _SHAPE_SHIFT: int = 18
const _ROTATION_SHIFT: int = 16
const _PALETTE_SHIFT: int = 8
const _BYTE_MASK: int = 0xFF
const _ROTATION_MASK: int = 0x03
const _SCHEMA: PackedStringArray = ["name", "rows"]

var name: String = ""
var rows: PackedStringArray = PackedStringArray()
var _cells: PackedInt32Array = PackedInt32Array()
var _map_width: int = 0
var _format_version: int = 1
var _conversion_problems: PackedStringArray = PackedStringArray()


static func from_dictionary(raw: Dictionary, format_version: int) -> LevelLayer:
	var layer: LevelLayer = LevelLayer.new()
	layer._format_version = format_version
	for key: String in raw:
		if not _SCHEMA.has(key):
			layer._conversion_problems.append("layer has unknown key '%s'" % key)

	var raw_name: Variant = raw.get("name", "")
	if typeof(raw_name) == TYPE_STRING:
		layer.name = str(raw_name)
	else:
		layer._conversion_problems.append("layer name must be a string")

	var raw_rows: Variant = raw.get("rows", [])
	if typeof(raw_rows) != TYPE_ARRAY:
		layer._conversion_problems.append("layer rows must be an array")
		return layer

	var row_values: Array = raw_rows
	for row_value: Variant in row_values:
		if typeof(row_value) != TYPE_STRING:
			layer._conversion_problems.append("layer rows must contain strings")
			continue
		layer.rows.append(str(row_value))
	return layer


func validate(
	map_size: Vector2i,
	palette_size: int,
	format_version: int,
) -> PackedStringArray:
	var problems: PackedStringArray = _conversion_problems.duplicate()
	_cells = PackedInt32Array()
	_map_width = map_size.x
	if name.is_empty():
		problems.append("layer name must not be empty")
	if format_version < 1 or format_version > 2:
		problems.append("layer format version %d is not supported" % format_version)
	if rows.size() != map_size.y:
		problems.append("layer has %d rows, size implies %d" % [rows.size(), map_size.y])

	for row_index: int in range(rows.size()):
		var row_text: String = rows[row_index]
		var tokens: PackedStringArray = _expand_row(
			row_text,
			row_index,
			problems,
			format_version,
		)
		if tokens.size() != map_size.x:
			(
				problems
				. append(
					"layer row %d has %d cells, size implies %d" % [row_index, tokens.size(), map_size.x],
				)
			)
		for token: String in tokens:
			var code: int = _parse_token(token, palette_size, format_version)
			if code < 0:
				problems.append("invalid cell token '%s' in row %d" % [token, row_index])
			else:
				_cells.append(code)

	if not problems.is_empty():
		_cells = PackedInt32Array()
		_map_width = 0
	return problems


func cell_code_at(column: int, row: int) -> int:
	var index: int = row * _map_width + column
	if index < 0 or index >= _cells.size():
		return 0
	return _cells[index]


func to_dictionary() -> Dictionary:
	var serialized_rows: Array[String] = []
	for row: String in rows:
		serialized_rows.append(row)
	return {
		"name": name,
		"rows": serialized_rows,
	}


static func pack_cell(
	shape: int,
	rotation: int,
	foreground_index: int,
	background_index: int,
) -> int:
	return (
		(shape << _SHAPE_SHIFT)
		| ((rotation & _ROTATION_MASK) << _ROTATION_SHIFT)
		| ((foreground_index & _BYTE_MASK) << _PALETTE_SHIFT)
		| (background_index & _BYTE_MASK)
	)


static func cell_shape(cell_code: int) -> int:
	return (cell_code >> _SHAPE_SHIFT) & _BYTE_MASK


static func cell_rotation(cell_code: int) -> int:
	return (cell_code >> _ROTATION_SHIFT) & _ROTATION_MASK


static func cell_foreground(cell_code: int) -> int:
	return (cell_code >> _PALETTE_SHIFT) & _BYTE_MASK


static func cell_background(cell_code: int) -> int:
	return cell_code & _BYTE_MASK


func _parse_token(token: String, palette_size: int, format_version: int) -> int:
	if format_version == 1:
		return _parse_v1_token(token, palette_size)
	if token.length() != _V2_TOKEN_WIDTH:
		return -1
	var shape: int = CellShapeCatalog.shape_code_for(token.substr(0, 2))
	if not CellShapeCatalog.is_known_shape(shape):
		return -1
	var foreground_index: int = _parse_palette_digit(token.substr(2, 1), palette_size)
	var background_index: int = _parse_palette_digit(token.substr(3, 1), palette_size)
	var rotation: int = _parse_rotation(token.substr(4, 1))
	if foreground_index < 0 or background_index < 0 or rotation < 0:
		return -1
	return pack_cell(shape, rotation, foreground_index, background_index)


func _parse_v1_token(token: String, palette_size: int) -> int:
	if token.length() != _V1_TOKEN_WIDTH:
		return -1
	var shape: int = EMPTY_SHAPE
	var shape_token: String = token.substr(0, 1)
	if shape_token == "S":
		shape = FILLED_SQUARE_SHAPE
	elif shape_token != "E":
		return -1
	var foreground_index: int = _parse_palette_digit(token.substr(1, 1), palette_size)
	if foreground_index < 0:
		return -1
	return pack_cell(shape, 0, foreground_index, 0)


func _parse_palette_digit(token: String, palette_size: int) -> int:
	if token.length() != 1 or _HEX_DIGITS.find(token.to_upper()) < 0:
		return -1
	var palette_index: int = token.hex_to_int()
	if palette_index < 0 or palette_index >= palette_size:
		return -1
	return palette_index


func _parse_rotation(token: String) -> int:
	if token.length() != 1 or "0123".find(token) < 0:
		return -1
	return token.to_int()


func _expand_row(
	row_text: String,
	row_index: int,
	problems: PackedStringArray,
	format_version: int,
) -> PackedStringArray:
	var tokens: PackedStringArray = PackedStringArray()
	var token_width: int = _V1_TOKEN_WIDTH if format_version == 1 else _V2_TOKEN_WIDTH
	if row_text.find(" ") < 0 and row_text.find("*") < 0:
		if row_text.length() % token_width != 0:
			problems.append("layer row %d has an invalid token length" % row_index)
		for token_start: int in range(0, row_text.length(), token_width):
			tokens.append(row_text.substr(token_start, token_width))
		return tokens

	for segment: String in row_text.split(" ", false):
		var parts: PackedStringArray = segment.split("*", false)
		if parts.size() == 1:
			tokens.append(segment)
			continue
		if parts.size() != 2 or parts[0].length() != _V2_TOKEN_WIDTH:
			problems.append("invalid row segment '%s' in row %d" % [segment, row_index])
			continue
		var count_text: String = parts[1]
		if count_text.is_empty() or not count_text.is_valid_int():
			problems.append("invalid row repeat count in '%s' at row %d" % [segment, row_index])
			continue
		var count: int = int(count_text)
		if count <= 0:
			problems.append("row repeat count must be positive in '%s' at row %d" % [segment, row_index])
			continue
		for repeat_index: int in range(count):
			tokens.append(parts[0])
	return tokens
