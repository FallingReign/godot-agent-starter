class_name PerformanceOverlay
extends PanelContainer

@onready var _label: Label = $Margin/Label


func _ready() -> void:
	set_process(false)


func set_diagnostics_visible(should_show: bool) -> void:
	visible = should_show
	set_process(should_show)
	if should_show:
		refresh()


func refresh() -> void:
	var fps: float = Engine.get_frames_per_second()
	var frame_time_ms: float = 1000.0 / fps if fps > 0.0 else 0.0
	var process_time_ms: float = Performance.get_monitor(Performance.TIME_PROCESS) * 1000.0
	var physics_time_ms: float = Performance.get_monitor(Performance.TIME_PHYSICS_PROCESS) * 1000.0
	var draw_calls: int = int(
		Performance.get_monitor(Performance.RENDER_TOTAL_DRAW_CALLS_IN_FRAME),
	)
	var primitives: int = int(
		Performance.get_monitor(Performance.RENDER_TOTAL_PRIMITIVES_IN_FRAME),
	)
	var render_objects: int = int(
		Performance.get_monitor(Performance.RENDER_TOTAL_OBJECTS_IN_FRAME),
	)
	var object_count: int = int(Performance.get_monitor(Performance.OBJECT_COUNT))
	var orphan_nodes: int = int(
		Performance.get_monitor(Performance.OBJECT_ORPHAN_NODE_COUNT),
	)
	var static_memory: float = Performance.get_monitor(Performance.MEMORY_STATIC)
	var maximum_static_memory: float = (
		Performance
		. get_monitor(
			Performance.MEMORY_STATIC_MAX,
		)
	)
	var video_memory: float = Performance.get_monitor(Performance.RENDER_VIDEO_MEM_USED)
	var texture_memory: float = (
		Performance
		. get_monitor(
			Performance.RENDER_TEXTURE_MEM_USED,
		)
	)
	var buffer_memory: float = (
		Performance
		. get_monitor(
			Performance.RENDER_BUFFER_MEM_USED,
		)
	)
	var vsync_mode: int = DisplayServer.window_get_vsync_mode()
	var fps_cap: String = "off" if Engine.max_fps <= 0 else str(Engine.max_fps)
	var lines: PackedStringArray = PackedStringArray()
	lines.append("DEGLYPH DIAGNOSTICS  [F3]")
	lines.append("FPS %8.1f   frame %8.2f ms" % [fps, frame_time_ms])
	lines.append("VSync %-8s max FPS %s" % [_format_vsync(vsync_mode), fps_cap])
	lines.append("CPU process %8.2f ms" % process_time_ms)
	lines.append("CPU physics %8.2f ms" % physics_time_ms)
	lines.append("Render draw %6d   primitives %6d" % [draw_calls, primitives])
	lines.append("Render objects %6d   total objects %6d" % [render_objects, object_count])
	(
		lines
		. append(
			"RAM static %s   max %s" % [_format_memory(static_memory), _format_memory(maximum_static_memory)],
		)
	)
	(
		lines
		. append(
			"Video %s   texture %s" % [_format_memory(video_memory), _format_memory(texture_memory)],
		)
	)
	lines.append("Buffer %s   orphan nodes %d" % [_format_memory(buffer_memory), orphan_nodes])
	_label.text = String("\n").join(lines)


func _process(_delta: float) -> void:
	refresh()


func _format_memory(bytes: float) -> String:
	const KIB: float = 1024.0
	const MIB: float = KIB * 1024.0
	const GIB: float = MIB * 1024.0
	if bytes >= GIB:
		return "%.1f GiB" % (bytes / GIB)
	if bytes >= MIB:
		return "%.1f MiB" % (bytes / MIB)
	if bytes >= KIB:
		return "%.1f KiB" % (bytes / KIB)
	return "%.0f B" % bytes


func _format_vsync(mode: int) -> String:
	match mode:
		DisplayServer.VSYNC_DISABLED:
			return "OFF"
		DisplayServer.VSYNC_ENABLED:
			return "ON"
		DisplayServer.VSYNC_ADAPTIVE:
			return "ADAPTIVE"
		DisplayServer.VSYNC_MAILBOX:
			return "MAILBOX"
		_:
			return "UNKNOWN"
