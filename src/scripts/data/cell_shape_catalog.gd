class_name CellShapeCatalog
extends RefCounted

const EMPTY_SHAPE: int = 0
const FILLED_SQUARE_SHAPE: int = 1
const TRIANGLE_SHAPE: int = 2

const EMPTY_TOKEN: String = "EM"
const FILLED_SQUARE_TOKEN: String = "SQ"
const TRIANGLE_TOKEN: String = "TR"

var _shape_points: Array[PackedVector2Array] = []


func _init() -> void:
	_shape_points = [
		PackedVector2Array(),
		PackedVector2Array(
			[
				Vector2(-0.5, -0.5),
				Vector2(0.5, -0.5),
				Vector2(0.5, 0.5),
				Vector2(-0.5, 0.5),
			],
		),
		PackedVector2Array(
			[
				Vector2(-0.5, 0.5),
				Vector2(0.5, 0.5),
				Vector2(0.0, -0.5),
			],
		),
	]


static func shape_code_for(token: String) -> int:
	match token.to_upper():
		EMPTY_TOKEN:
			return EMPTY_SHAPE
		FILLED_SQUARE_TOKEN:
			return FILLED_SQUARE_SHAPE
		TRIANGLE_TOKEN:
			return TRIANGLE_SHAPE
		_:
			return -1


func polygon_for(shape: int) -> PackedVector2Array:
	if not is_known_shape(shape):
		return PackedVector2Array()
	return _shape_points[shape]


static func is_known_shape(shape: int) -> bool:
	return shape >= EMPTY_SHAPE and shape <= TRIANGLE_SHAPE
