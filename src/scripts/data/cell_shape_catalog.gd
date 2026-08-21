class_name CellShapeCatalog
extends RefCounted

const EMPTY_SHAPE: int = 0
const FILLED_SQUARE_SHAPE: int = 1
const TRIANGLE_SHAPE: int = 2
const CIRCLE_SHAPE: int = 3
const DIAGONAL_SHAPE: int = 4
const CORNER_SHAPE: int = 5

const EMPTY_TOKEN: String = "EM"
const FILLED_SQUARE_TOKEN: String = "SQ"
const TRIANGLE_TOKEN: String = "TR"
const CIRCLE_TOKEN: String = "CI"
const DIAGONAL_TOKEN: String = "DG"
const CORNER_TOKEN: String = "CO"

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
		PackedVector2Array(
			[
				Vector2(0.0, -0.5),
				Vector2(0.25, -0.433),
				Vector2(0.433, -0.25),
				Vector2(0.5, 0.0),
				Vector2(0.433, 0.25),
				Vector2(0.25, 0.433),
				Vector2(0.0, 0.5),
				Vector2(-0.25, 0.433),
				Vector2(-0.433, 0.25),
				Vector2(-0.5, 0.0),
				Vector2(-0.433, -0.25),
				Vector2(-0.25, -0.433),
			],
		),
		PackedVector2Array(
			[
				Vector2(-0.5, -0.35),
				Vector2(-0.35, -0.5),
				Vector2(0.5, 0.35),
				Vector2(0.35, 0.5),
			],
		),
		PackedVector2Array(
			[
				Vector2(-0.5, -0.5),
				Vector2(0.5, -0.5),
				Vector2(0.5, -0.25),
				Vector2(-0.25, -0.25),
				Vector2(-0.25, 0.5),
				Vector2(-0.5, 0.5),
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
		CIRCLE_TOKEN:
			return CIRCLE_SHAPE
		DIAGONAL_TOKEN:
			return DIAGONAL_SHAPE
		CORNER_TOKEN:
			return CORNER_SHAPE
		_:
			return -1


func polygon_for(shape: int) -> PackedVector2Array:
	if not is_known_shape(shape):
		return PackedVector2Array()
	return _shape_points[shape]


static func is_known_shape(shape: int) -> bool:
	return shape >= EMPTY_SHAPE and shape <= CORNER_SHAPE
