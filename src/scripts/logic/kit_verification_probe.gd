class_name KitVerificationProbe
extends RefCounted

var _observations: int = 0


func observe() -> int:
	_observations += 1
	return _observations
