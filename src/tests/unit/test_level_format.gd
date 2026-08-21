extends GutTest


func _valid_raw_map() -> Dictionary:
	return {
		"version": 2,
		"name": "test",
		"size":
		{
			"width": 2,
			"height": 2,
		},
		"palette": ["#090B12", "#E53935", "#2A9D8F", "#F4F1DE"],
		"layers":
		[
			{
				"name": "base",
				"rows":
				[
					"SQ120 EM000",
					"TR231 EM000",
				],
			},
		],
	}


func _valid_level() -> LevelFormat:
	return LevelFormat.from_dictionary(_valid_raw_map())


func test_valid_map_reports_no_problems() -> void:
	assert_eq(_valid_level().validate().size(), 0, "valid map should pass")


func test_two_colours_and_shape_round_trip() -> void:
	var original: LevelFormat = _valid_level()
	var restored: LevelFormat = LevelFormat.from_dictionary(original.to_dictionary())
	assert_eq(restored.validate().size(), 0, "round-tripped map should pass")
	var cell_code: int = restored.layers[0].cell_code_at(0, 0)
	assert_eq(LevelLayer.cell_shape(cell_code), LevelLayer.FILLED_SQUARE_SHAPE)
	assert_eq(LevelLayer.cell_rotation(cell_code), 0)
	assert_eq(LevelLayer.cell_foreground(cell_code), 1)
	assert_eq(LevelLayer.cell_background(cell_code), 2)


func test_all_quarter_turns_round_trip() -> void:
	var raw: Dictionary = _valid_raw_map()
	raw["size"] = {"width": 4, "height": 1}
	raw["layers"] = [
		{
			"name": "rotations",
			"rows": ["TR100 TR101 TR102 TR103"],
		},
	]
	var layer: LevelLayer = LevelFormat.from_dictionary(raw).layers[0]
	assert_eq(layer.validate(Vector2i(4, 1), 2, 2).size(), 0)
	for rotation: int in range(4):
		var cell_code: int = layer.cell_code_at(rotation, 0)
		assert_eq(LevelLayer.cell_shape(cell_code), LevelLayer.TRIANGLE_SHAPE)
		assert_eq(LevelLayer.cell_rotation(cell_code), rotation)


func test_wrong_row_width_fails() -> void:
	var raw: Dictionary = _valid_raw_map()
	raw["layers"] = [
		{
			"name": "base",
			"rows":
			[
				"SQ120 EM000",
				"EM000",
			],
		},
	]
	assert_gt(LevelFormat.from_dictionary(raw).validate().size(), 0, "short row must fail")


func test_unknown_and_invalid_values_are_rejected() -> void:
	var raw: Dictionary = _valid_raw_map()
	raw["script"] = "res://evil.gd"
	raw["palette"] = ["not-a-hex-colour", "#E53935", "#2A9D8F", "#F4F1DE"]
	raw["layers"][0]["rows"][0] = "ZZ120 EM000"
	var level: LevelFormat = LevelFormat.from_dictionary(raw)
	assert_gt(level.validate().size(), 0, "unsafe map fields must fail")


func test_future_version_is_rejected() -> void:
	var raw: Dictionary = _valid_raw_map()
	raw["version"] = LevelFormat.VERSION + 1
	assert_gt(LevelFormat.from_dictionary(raw).validate().size(), 0, "newer map must fail")


func test_v1_map_remains_loadable() -> void:
	var raw: Dictionary = {
		"version": 1,
		"name": "legacy",
		"size":
		{
			"width": 2,
			"height": 1,
		},
		"palette": ["#090B12", "#E53935"],
		"layers":
		[
			{
				"name": "base",
				"rows": ["S1 E0"],
			},
		],
	}
	var level: LevelFormat = LevelFormat.from_dictionary(raw)
	assert_eq(level.validate().size(), 0, "legacy map should remain loadable")
	var cell_code: int = level.layers[0].cell_code_at(0, 0)
	assert_eq(
		cell_code,
		LevelLayer.pack_cell(LevelLayer.FILLED_SQUARE_SHAPE, 0, 1, 0),
		"legacy square should retain its foreground and default background",
	)


func test_authored_map_loads_with_repeated_row_tokens() -> void:
	var level: LevelFormat = LevelFormat.load_from("res://content/maps/red_grid.json")
	assert_not_null(level, "authored map should load")
	assert_eq(level.size, Vector2i(192, 54), "authored map extends beyond the logical view")
	var circle_code: int = level.layers[0].cell_code_at(15, 8)
	var corner_code: int = level.layers[0].cell_code_at(34, 15)
	var roof_code: int = level.layers[0].cell_code_at(35, 14)
	var diagonal_code: int = level.layers[0].cell_code_at(45, 24)
	var distant_roof_code: int = level.layers[0].cell_code_at(131, 14)
	assert_eq(LevelLayer.cell_shape(circle_code), CellShapeCatalog.CIRCLE_SHAPE)
	assert_eq(LevelLayer.cell_shape(corner_code), CellShapeCatalog.CORNER_SHAPE)
	assert_eq(LevelLayer.cell_shape(roof_code), LevelLayer.TRIANGLE_SHAPE)
	assert_eq(LevelLayer.cell_shape(diagonal_code), CellShapeCatalog.DIAGONAL_SHAPE)
	assert_eq(LevelLayer.cell_shape(distant_roof_code), LevelLayer.TRIANGLE_SHAPE)
	assert_eq(LevelLayer.cell_foreground(roof_code), 1)
	assert_eq(LevelLayer.cell_background(roof_code), 0)
	var water_code: int = level.layers[0].cell_code_at(0, 40)
	assert_eq(LevelLayer.cell_background(water_code), 4)
