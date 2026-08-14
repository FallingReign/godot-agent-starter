class_name Health
extends RefCounted

## Reference example of the architecture rule in AGENTS.md.
##
## Pure logic: no Node, no scene, no engine singletons. Fully testable headlessly,
## which means an agent can write it and verify it without human involvement.

signal died

var maximum: int
var current: int


func _init(maximum_value: int) -> void:
	maximum = maxi(maximum_value, 1)
	current = maximum


func apply_damage(amount: int) -> void:
	if amount <= 0:
		return
	current = maxi(current - amount, 0)
	if current == 0:
		died.emit()


func heal(amount: int) -> void:
	if amount <= 0:
		return
	current = mini(current + amount, maximum)


func is_alive() -> bool:
	return current > 0


func fraction() -> float:
	return float(current) / float(maximum)
