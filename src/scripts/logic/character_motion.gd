class_name CharacterMotion
extends RefCounted

var position: Vector2
var velocity: Vector2

var _world_size: Vector2
var _config: GameConfig


func _init(start_position: Vector2, world_size: Vector2, config: GameConfig) -> void:
	position = start_position
	velocity = Vector2.ZERO
	_world_size = Vector2(maxf(world_size.x, 0.0), maxf(world_size.y, 0.0))
	_config = config
	_clamp_position()


func step(input_direction: Vector2, delta: float) -> void:
	if delta <= 0.0:
		return
	var direction: Vector2 = input_direction
	if direction.length_squared() > 1.0:
		direction = direction.normalized()
	var target_velocity: Vector2 = direction * _config.movement_max_speed
	var time_to_target: float = (
		_config.movement_accel_time if direction.length_squared() > 0.0 else _config.movement_stop_time
	)
	var velocity_change: float = _config.movement_max_speed
	if time_to_target > 0.0:
		velocity_change = _config.movement_max_speed * delta / time_to_target
	velocity = velocity.move_toward(target_velocity, velocity_change)
	position += velocity * delta
	_clamp_position()


func _clamp_position() -> void:
	position.x = clampf(position.x, 0.0, _world_size.x)
	position.y = clampf(position.y, 0.0, _world_size.y)
