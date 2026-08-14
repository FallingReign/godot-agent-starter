extends Node

## Headless boot check. Loads the real main scene and asserts it survives a few frames.
## Catches missing assets, broken imports and init crashes, which --check-only misses.
##
## Run: godot --headless --path . res://tests/smoke_test.tscn

const MAIN_SCENE_PATH: String = "res://scenes/main.tscn"
const FRAMES_TO_SURVIVE: int = 10

var _frames: int = 0


func _ready() -> void:
	if not ResourceLoader.exists(MAIN_SCENE_PATH):
		_fail("main scene not found at %s" % MAIN_SCENE_PATH)
		return
	var packed: PackedScene = load(MAIN_SCENE_PATH) as PackedScene
	if packed == null:
		_fail("failed to load %s as PackedScene" % MAIN_SCENE_PATH)
		return
	var instance: Node = packed.instantiate()
	if instance == null:
		_fail("failed to instantiate %s" % MAIN_SCENE_PATH)
		return
	add_child(instance)
	print("SMOKE: instantiated %s" % MAIN_SCENE_PATH)


func _process(_delta: float) -> void:
	_frames += 1
	if _frames < FRAMES_TO_SURVIVE:
		return
	if get_child_count() == 0:
		_fail("main scene vanished before frame %d" % _frames)
		return
	print("SMOKE: PASS after %d frames" % _frames)
	get_tree().quit(0)


func _fail(message: String) -> void:
	printerr("SMOKE: FAIL %s" % message)
	push_error("SMOKE: FAIL %s" % message)
	get_tree().quit(1)
