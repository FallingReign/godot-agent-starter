extends GutTest

## Requires GUT, installed by bootstrap.py --fix.


func test_starts_at_maximum() -> void:
	var health: Health = Health.new(10)
	assert_eq(health.current, 10, "should start full")
	assert_true(health.is_alive(), "should start alive")


func test_damage_reduces_current() -> void:
	var health: Health = Health.new(10)
	health.apply_damage(3)
	assert_eq(health.current, 7, "damage should subtract")


func test_damage_clamps_at_zero() -> void:
	var health: Health = Health.new(10)
	health.apply_damage(999)
	assert_eq(health.current, 0, "should not go negative")
	assert_false(health.is_alive(), "should be dead")


func test_heal_clamps_at_maximum() -> void:
	var health: Health = Health.new(10)
	health.apply_damage(5)
	health.heal(999)
	assert_eq(health.current, 10, "should not exceed maximum")


func test_non_positive_amounts_are_ignored() -> void:
	var health: Health = Health.new(10)
	health.apply_damage(0)
	health.apply_damage(-5)
	health.heal(-5)
	assert_eq(health.current, 10, "non-positive amounts are no-ops")
