extends GutTest


func test_probe_records_observations() -> void:
	var probe: KitVerificationProbe = KitVerificationProbe.new()
	assert_eq(probe.observe(), 1)
	assert_eq(probe.observe(), 2)
