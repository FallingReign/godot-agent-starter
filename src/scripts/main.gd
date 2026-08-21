class_name Main
extends Control

## Entry point. Keep this thin: wiring and presentation only.
## Game rules belong in plain RefCounted classes under scripts/logic/.

enum ScreenState {
	MENU,
	WORLD,
}

const _BACKGROUND_COLOR: Color = Color(0.035, 0.055, 0.095, 1.0)
const _GRID_COLOR: Color = Color(0.12, 0.17, 0.25, 1.0)
const _MAP_PATH: String = "res://content/maps/red_grid.json"
const _LOGICAL_VIEWPORT_SIZE: Vector2i = Vector2i(1920, 1080)
const _DEFAULT_WINDOW_SIZE: Vector2i = Vector2i(1920, 1080)
const _PAN_SPEED: float = 480.0
const _DEBUG_STATS_UPDATE_INTERVAL: float = 0.25
const _DEBUG_STATS_PANEL: Rect2 = Rect2(16.0, 16.0, 320.0, 116.0)
const _DEBUG_STATS_PANEL_COLOR: Color = Color(0.02, 0.03, 0.05, 0.9)
const _DEBUG_STATS_TEXT_COLOR: Color = Color(0.88, 0.96, 1.0, 1.0)
const _SCALE_PROBE_PITCHES: Array[float] = [12.0, 15.0, 20.0, 24.0, 30.0, 60.0]
const _SCALE_PROBE_IDS: Array[String] = ["S1", "S2", "S3", "S4", "S5", "S6"]

## @tune cell.pitch
## Distance between cell centres in logical units.
@export var cell_pitch: float = 20.0

## @tune cell.inset
## Gap inside each cell in logical units so the grid remains visibly composed of cells.
@export var cell_inset: float = 1.0

var _screen_state: ScreenState = ScreenState.MENU
var _map: LevelFormat = null
var _shape_catalog: CellShapeCatalog = CellShapeCatalog.new()
var _camera_offset: Vector2 = Vector2.ZERO
var _debug_stats_visible: bool = false
var _debug_stats_timer: float = 0.0
var _debug_stats_text: String = ""
var _scale_probe_index: int = 2

@onready var _menu: CenterContainer = $Menu
@onready var _play_button: Button = $Menu/Panel/Margin/Content/PlayButton
@onready var _quit_button: Button = $Menu/Panel/Margin/Content/QuitButton
@onready var _subtitle: Label = $Menu/Panel/Margin/Content/Subtitle


func _ready() -> void:
	_configure_window()
	_configure_content_scale()
	_play_button.pressed.connect(_on_play_pressed)
	_quit_button.pressed.connect(_on_quit_pressed)
	resized.connect(_on_resized)
	_map = LevelFormat.load_from(_MAP_PATH)
	if _map == null:
		_play_button.disabled = true
		_subtitle.text = "Map unavailable"
	queue_redraw()


func _configure_window() -> void:
	var window: Window = get_window()
	window.size = _DEFAULT_WINDOW_SIZE


func _configure_content_scale() -> void:
	var window: Window = get_window()
	window.content_scale_size = _LOGICAL_VIEWPORT_SIZE
	window.content_scale_mode = Window.CONTENT_SCALE_MODE_CANVAS_ITEMS
	window.content_scale_aspect = Window.CONTENT_SCALE_ASPECT_KEEP


func _on_play_pressed() -> void:
	if _map == null:
		return
	_screen_state = ScreenState.WORLD
	_camera_offset = Vector2.ZERO
	_menu.hide()
	queue_redraw()


func _on_quit_pressed() -> void:
	get_tree().quit()


func _on_resized() -> void:
	queue_redraw()


func _process(delta: float) -> void:
	if _debug_stats_visible:
		_debug_stats_timer -= delta
		if _debug_stats_timer <= 0.0:
			_debug_stats_timer = _DEBUG_STATS_UPDATE_INTERVAL
			var fps: float = Engine.get_frames_per_second()
			var frame_ms: float = 1000.0 / maxf(fps, 0.001)
			var draw_calls: int = int(Performance.get_monitor(Performance.RENDER_TOTAL_DRAW_CALLS_IN_FRAME))
			var primitives: int = int(Performance.get_monitor(Performance.RENDER_TOTAL_PRIMITIVES_IN_FRAME))
			var video_memory_bytes: float = Performance.get_monitor(Performance.RENDER_VIDEO_MEM_USED)
			var video_memory: String = "n/a"
			if video_memory_bytes > 0.0:
				video_memory = "%.1f MB" % (video_memory_bytes / 1048576.0)
			_debug_stats_text = (
				("Scale: %s (%.0f units)\nFPS: %d (%.2f ms)\nDraw calls: %d\n" + "Primitives: %d\nGPU memory: %s")
				% [
					_SCALE_PROBE_IDS[_scale_probe_index],
					cell_pitch,
					int(fps),
					frame_ms,
					draw_calls,
					primitives,
					video_memory,
				]
			)
			queue_redraw()
	if _screen_state != ScreenState.WORLD or _map == null:
		return
	var direction: Vector2 = _pan_direction()
	if direction == Vector2.ZERO:
		return
	var world_size: Vector2 = Vector2(
		float(_map.size.x) * cell_pitch,
		float(_map.size.y) * cell_pitch,
	)
	var max_offset: Vector2 = Vector2(
		maxf(world_size.x - float(_LOGICAL_VIEWPORT_SIZE.x), 0.0),
		maxf(world_size.y - float(_LOGICAL_VIEWPORT_SIZE.y), 0.0),
	)
	_camera_offset = (
		(_camera_offset + direction * _PAN_SPEED * delta)
		. clamp(
			Vector2.ZERO,
			max_offset,
		)
	)
	queue_redraw()


func _unhandled_input(event: InputEvent) -> void:
	if _screen_state != ScreenState.WORLD:
		return
	if event is InputEventKey:
		var key_event: InputEventKey = event
		if key_event.pressed and not key_event.echo and key_event.keycode == KEY_F3:
			_debug_stats_visible = not _debug_stats_visible
			_debug_stats_timer = 0.0
			queue_redraw()
			return
		if key_event.pressed and not key_event.echo and key_event.keycode == KEY_F4:
			_scale_probe_index = (_scale_probe_index + 1) % _SCALE_PROBE_PITCHES.size()
			cell_pitch = _SCALE_PROBE_PITCHES[_scale_probe_index]
			var world_size: Vector2 = Vector2(
				float(_map.size.x) * cell_pitch,
				float(_map.size.y) * cell_pitch,
			)
			var max_offset: Vector2 = Vector2(
				maxf(world_size.x - float(_LOGICAL_VIEWPORT_SIZE.x), 0.0),
				maxf(world_size.y - float(_LOGICAL_VIEWPORT_SIZE.y), 0.0),
			)
			_camera_offset = _camera_offset.clamp(Vector2.ZERO, max_offset)
			_debug_stats_timer = 0.0
			queue_redraw()
			return
		if key_event.pressed and not key_event.echo and key_event.keycode == KEY_HOME:
			_camera_offset = Vector2.ZERO
			queue_redraw()


func _pan_direction() -> Vector2:
	var direction: Vector2 = Vector2.ZERO
	if Input.is_key_pressed(KEY_LEFT):
		direction.x -= 1.0
	if Input.is_key_pressed(KEY_RIGHT):
		direction.x += 1.0
	if Input.is_key_pressed(KEY_UP):
		direction.y -= 1.0
	if Input.is_key_pressed(KEY_DOWN):
		direction.y += 1.0
	return direction.normalized()


func _draw() -> void:
	draw_rect(Rect2(Vector2.ZERO, size), _BACKGROUND_COLOR)
	if _map == null:
		return
	var layer: LevelLayer = _map.layers[0]
	var first_column: int = maxi(floori(_camera_offset.x / cell_pitch), 0)
	var last_column: int = mini(
		ceili((_camera_offset.x + float(_LOGICAL_VIEWPORT_SIZE.x)) / cell_pitch),
		_map.size.x,
	)
	var first_row: int = maxi(floori(_camera_offset.y / cell_pitch), 0)
	var last_row: int = mini(
		ceili((_camera_offset.y + float(_LOGICAL_VIEWPORT_SIZE.y)) / cell_pitch),
		_map.size.y,
	)
	for row: int in range(first_row, last_row):
		for column: int in range(first_column, last_column):
			_draw_map_cell(layer, column, row)
	var grid_points: PackedVector2Array = PackedVector2Array()
	var top: float = float(first_row) * cell_pitch - _camera_offset.y
	var bottom: float = float(last_row) * cell_pitch - _camera_offset.y
	for column: int in range(first_column, last_column + 1):
		var x: float = float(column) * cell_pitch - _camera_offset.x
		grid_points.append(Vector2(x, top))
		grid_points.append(Vector2(x, bottom))
	var left: float = float(first_column) * cell_pitch - _camera_offset.x
	var right: float = float(last_column) * cell_pitch - _camera_offset.x
	for row: int in range(first_row, last_row + 1):
		var y: float = float(row) * cell_pitch - _camera_offset.y
		grid_points.append(Vector2(left, y))
		grid_points.append(Vector2(right, y))
	draw_multiline(grid_points, _GRID_COLOR, 1.0)
	if _debug_stats_visible:
		draw_rect(_DEBUG_STATS_PANEL, _DEBUG_STATS_PANEL_COLOR)
		draw_multiline_string(
			ThemeDB.fallback_font,
			Vector2(28.0, 40.0),
			_debug_stats_text,
			HORIZONTAL_ALIGNMENT_LEFT,
			-1.0,
			20,
			-1,
			_DEBUG_STATS_TEXT_COLOR,
		)


func _draw_map_cell(layer: LevelLayer, column: int, row: int) -> void:
	var cell_code: int = layer.cell_code_at(column, row)
	var shape: int = LevelLayer.cell_shape(cell_code)
	var quarter_turns: int = LevelLayer.cell_rotation(cell_code)
	var foreground_index: int = LevelLayer.cell_foreground(cell_code)
	var background_index: int = LevelLayer.cell_background(cell_code)
	var cell_position: Vector2 = (
		Vector2(
			float(column) * cell_pitch,
			float(row) * cell_pitch,
		)
		- _camera_offset
	)
	var cell_size: Vector2 = Vector2(cell_pitch, cell_pitch)
	var inset: Vector2 = Vector2(cell_inset, cell_inset)
	var cell_rect: Rect2 = Rect2(cell_position + inset, cell_size - inset * 2.0)
	draw_rect(cell_rect, _map.palette[background_index])
	_draw_shape(cell_rect, shape, _map.palette[foreground_index], quarter_turns)


func _draw_shape(
	cell_rect: Rect2,
	shape: int,
	foreground: Color,
	quarter_turns: int,
) -> void:
	var normalized_points: PackedVector2Array = _shape_catalog.polygon_for(shape)
	if normalized_points.is_empty():
		return
	var center: Vector2 = cell_rect.position + cell_rect.size * 0.5
	var angle: float = float(quarter_turns) * PI * 0.5
	var points: PackedVector2Array = PackedVector2Array()
	for normalized_point: Vector2 in normalized_points:
		var rotated_point: Vector2 = normalized_point.rotated(angle)
		var scaled_point: Vector2 = Vector2(
			rotated_point.x * cell_rect.size.x,
			rotated_point.y * cell_rect.size.y,
		)
		points.append(center + scaled_point)
	draw_colored_polygon(points, foreground)
