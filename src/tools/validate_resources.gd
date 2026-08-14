extends SceneTree
## Headless resource validator. Godot is its own parser, so the engine is the
## final arbiter of whether a scene or resource is valid.
##
## Loads every .tscn and .tres, then instantiates every scene. Prints one
## RESVAL: line per outcome plus a summary the gate parses.
##
## Run:  godot --headless --path . -s res://tools/validate_resources.gd

const SKIP_DIRS: PackedStringArray = [
	"addons",
	"build",
	"export",
	".godot",
	".git",
	".checklogs",
]


func _init() -> void:
	var paths: PackedStringArray = []
	_collect("res://", paths)
	paths.sort()

	var failures: int = 0
	var checked: int = 0

	for path: String in paths:
		checked += 1
		var res: Resource = ResourceLoader.load(path, "", ResourceLoader.CACHE_MODE_IGNORE)
		if res == null:
			print("RESVAL: FAIL load %s" % path)
			failures += 1
			continue

		if path.ends_with(".tscn"):
			var packed: PackedScene = res as PackedScene
			if packed == null:
				print("RESVAL: FAIL not-a-scene %s" % path)
				failures += 1
				continue
			# Instantiating is the real test: a file can parse and still fail
			# to build a tree, which surfaces as node count is 0.
			var inst: Node = packed.instantiate()
			if inst == null:
				print("RESVAL: FAIL instantiate %s" % path)
				failures += 1
				continue
			print("RESVAL: OK %s (%d node(s))" % [path, _count(inst)])
			inst.free()
		else:
			print("RESVAL: OK %s" % path)

	print("RESVAL: SUMMARY checked=%d failures=%d" % [checked, failures])
	if failures == 0:
		print("RESVAL: PASS")
	else:
		print("RESVAL: FAILED")
	quit(1 if failures > 0 else 0)


func _count(node: Node) -> int:
	var total: int = 1
	for child: Node in node.get_children():
		total += _count(child)
	return total


func _collect(dir_path: String, out: PackedStringArray) -> void:
	var dir: DirAccess = DirAccess.open(dir_path)
	if dir == null:
		return
	dir.list_dir_begin()
	var name: String = dir.get_next()
	while name != "":
		if name.begins_with("."):
			name = dir.get_next()
			continue
		var full: String = dir_path.path_join(name) if dir_path != "res://" else "res://" + name
		if dir.current_is_dir():
			if not SKIP_DIRS.has(name):
				_collect(full, out)
		elif name.ends_with(".tscn") or name.ends_with(".tres"):
			out.append(full)
		name = dir.get_next()
	dir.list_dir_end()
