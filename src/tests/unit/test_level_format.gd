extends GutTest


func _valid_level() -> LevelFormat:
	var level: LevelFormat = LevelFormat.new()
	level.name = "test"
	level.size = Vector2i(2, 2)
	level.tiles = PackedByteArray([0, 1, 1, 0])
	level.spawns = [Vector2i(0, 0)]
	return level


func test_valid_level_reports_no_problems() -> void:
	assert_eq(_valid_level().validate().size(), 0, "valid level should pass")


func test_tile_count_must_match_size() -> void:
	var level: LevelFormat = _valid_level()
	level.tiles = PackedByteArray([0, 1])
	assert_gt(level.validate().size(), 0, "mismatched tile count must fail")


func test_spawn_outside_bounds_fails() -> void:
	var level: LevelFormat = _valid_level()
	level.spawns = [Vector2i(99, 99)]
	assert_gt(level.validate().size(), 0, "out-of-bounds spawn must fail")


func test_future_version_is_rejected() -> void:
	var level: LevelFormat = _valid_level()
	level.version = LevelFormat.VERSION + 1
	assert_gt(level.validate().size(), 0, "newer format must not silently load")


func test_round_trip_preserves_data() -> void:
	var original: LevelFormat = _valid_level()
	var restored: LevelFormat = LevelFormat.from_dictionary(original.to_dictionary())
	assert_eq(restored.size, original.size, "size survives round trip")
	assert_eq(restored.tiles, original.tiles, "tiles survive round trip")


func test_unknown_keys_are_dropped() -> void:
	var raw: Dictionary = _valid_level().to_dictionary()
	raw["script"] = "res://evil.gd"
	var restored: LevelFormat = LevelFormat.from_dictionary(raw)
	assert_eq(restored.validate().size(), 0, "unknown keys must not break load")
	assert_false("script" in LevelFormat.SCHEMA, "schema must not accept script paths")
