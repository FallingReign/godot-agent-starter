class_name GameConfig
extends RefCounted

## Tuning values as a flat, injected data object. NOT an autoload.
##
## Why not a global singleton:
##   1. A system that reaches out to a global is hidden coupling. Config should
##      flow IN, the way an ECS passes a singleton component to a system.
##   2. Every global lookup inside a hot loop becomes a boundary crossing if that
##      loop is later ported to GDExtension. Flat data copies once and is then
##      read at native speed. See godot-cpp#1063 for what that costs when ignored.
##   3. Tests construct a config with different numbers instead of mutating
##      global state, so they cannot leak into each other.
##
## Shape rules, enforced by review:
##   - Typed primitives only: int, float, bool, String, Vector2/3, Color.
##   - No arrays of objects, no Node or Resource references, no methods with
##     behaviour beyond construction, validation and hashing.
##   - That shape maps 1:1 onto a C++ POD or an ECS singleton component, so the
##     eventual port is mechanical rather than a rewrite.
##
## Delete the example fields. Keep the shape.

var tick_rate: int = 60
var move_speed: float = 6.0
var gravity: float = 24.0
var max_players: int = 4

## @tune movement.max_speed
## Peak horizontal speed in pixels per second for the controllable character.
var movement_max_speed: float = 300.0

## @tune movement.accel_time
## Seconds for the controllable character to reach full speed.
var movement_accel_time: float = 0.08

## @tune movement.stop_time
## Seconds for the controllable character to come to rest after release.
var movement_stop_time: float = 0.08


func _init(overrides: Dictionary = {}) -> void:
	for key: String in overrides:
		if key in self:
			set(key, overrides[key])
	_validate()


func _validate() -> void:
	tick_rate = clampi(tick_rate, 1, 240)
	move_speed = maxf(move_speed, 0.0)
	max_players = clampi(max_players, 1, 64)
	movement_max_speed = clampf(movement_max_speed, 180.0, 420.0)
	movement_accel_time = clampf(movement_accel_time, 0.0, 0.2)
	movement_stop_time = clampf(movement_stop_time, 0.0, 0.15)


## Stable hash of every tuning value.
##
## For networked builds: exchange this at handshake and refuse a mismatch.
## Divergent config is a desync that otherwise shows up much later as an
## inexplicable behavioural difference between peers.
func fingerprint() -> String:
	var parts: PackedStringArray = PackedStringArray()
	for entry: Dictionary in get_property_list():
		var usage: int = entry["usage"]
		if usage & PROPERTY_USAGE_SCRIPT_VARIABLE:
			parts.append("%s=%s" % [entry["name"], get(StringName(str(entry["name"])))])
	return str(parts.size()) + ":" + String("|").join(parts).md5_text()
