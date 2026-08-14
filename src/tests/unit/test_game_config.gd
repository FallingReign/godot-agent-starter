extends GutTest


func test_defaults_are_valid() -> void:
	var config: GameConfig = GameConfig.new()
	assert_eq(config.tick_rate, 60, "default tick rate")
	assert_true(config.max_players >= 1, "at least one player")


func test_overrides_apply() -> void:
	var config: GameConfig = GameConfig.new({"move_speed": 9.5})
	assert_almost_eq(config.move_speed, 9.5, 0.001, "override should apply")


func test_out_of_range_values_are_clamped() -> void:
	var config: GameConfig = GameConfig.new({"tick_rate": 100000, "max_players": 0})
	assert_eq(config.tick_rate, 240, "tick rate clamps to ceiling")
	assert_eq(config.max_players, 1, "max players clamps to floor")


func test_unknown_keys_are_ignored() -> void:
	var config: GameConfig = GameConfig.new({"not_a_real_field": 1})
	assert_eq(config.tick_rate, 60, "unknown key should not disturb defaults")


func test_fingerprint_detects_divergence() -> void:
	var a: GameConfig = GameConfig.new()
	var b: GameConfig = GameConfig.new()
	var c: GameConfig = GameConfig.new({"move_speed": 7.0})
	assert_eq(a.fingerprint(), b.fingerprint(), "identical config, identical hash")
	assert_ne(a.fingerprint(), c.fingerprint(), "divergent config must differ")
