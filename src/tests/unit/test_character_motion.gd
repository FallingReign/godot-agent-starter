extends GutTest


func _configured_motion() -> CharacterMotion:
	var config: GameConfig = (
		GameConfig
		. new(
			{
				"movement_max_speed": 200.0,
				"movement_accel_time": 0.1,
				"movement_stop_time": 0.1,
			},
		)
	)
	return CharacterMotion.new(Vector2(50.0, 50.0), Vector2(1000.0, 1000.0), config)


func test_accelerates_and_stops() -> void:
	var motion: CharacterMotion = _configured_motion()
	motion.step(Vector2.RIGHT, 0.05)
	assert_gt(motion.velocity.x, 0.0, "movement should ramp up")
	assert_lt(motion.velocity.x, 200.0, "movement should not reach full speed immediately")
	motion.step(Vector2.ZERO, 0.1)
	assert_almost_eq(motion.velocity.length(), 0.0, 0.001, "release should settle quickly")


func test_diagonal_input_is_normalized() -> void:
	var motion: CharacterMotion = _configured_motion()
	motion.step(Vector2.ONE, 0.1)
	assert_almost_eq(motion.velocity.length(), 200.0, 0.001, "diagonal speed should be normalized")


func test_position_is_clamped_to_world() -> void:
	var motion: CharacterMotion = _configured_motion()
	motion.step(Vector2(-1.0, -1.0), 1.0)
	assert_eq(motion.position, Vector2.ZERO, "movement should stop at the minimum world bounds")
	motion.step(Vector2.ONE, 10.0)
	assert_eq(motion.position, Vector2(1000.0, 1000.0), "movement should stop at the maximum world bounds")
