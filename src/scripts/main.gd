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

## @tune cell.pitch
## Distance between cell centres in logical units.
@export var cell_pitch: float = 15.0

## @tune cell.inset
## Gap inside each cell in logical units so the grid remains visibly composed of cells.
@export var cell_inset: float = 1.0

var _screen_state: ScreenState = ScreenState.MENU
var _map: LevelFormat = null
var _shape_catalog: CellShapeCatalog = CellShapeCatalog.new()

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
	window.borderless = true
	window.mode = Window.MODE_FULLSCREEN


func _configure_content_scale() -> void:
	var window: Window = get_window()
	window.content_scale_size = _LOGICAL_VIEWPORT_SIZE
	window.content_scale_mode = Window.CONTENT_SCALE_MODE_CANVAS_ITEMS
	window.content_scale_aspect = Window.CONTENT_SCALE_ASPECT_KEEP


func _on_play_pressed() -> void:
	if _map == null:
		return
	_screen_state = ScreenState.WORLD
	_menu.hide()
	queue_redraw()


func _on_quit_pressed() -> void:
	get_tree().quit()


func _on_resized() -> void:
	queue_redraw()


func _draw() -> void:
	draw_rect(Rect2(Vector2.ZERO, size), _BACKGROUND_COLOR)
	if _map == null:
		return
	var layer: LevelLayer = _map.layers[0]
	for row: int in range(_map.size.y):
		for column: int in range(_map.size.x):
			_draw_map_cell(layer, column, row)


func _draw_map_cell(layer: LevelLayer, column: int, row: int) -> void:
	var cell_code: int = layer.cell_code_at(column, row)
	var shape: int = LevelLayer.cell_shape(cell_code)
	var quarter_turns: int = LevelLayer.cell_rotation(cell_code)
	var foreground_index: int = LevelLayer.cell_foreground(cell_code)
	var background_index: int = LevelLayer.cell_background(cell_code)
	var cell_position: Vector2 = Vector2(float(column) * cell_pitch, float(row) * cell_pitch)
	var cell_size: Vector2 = Vector2(cell_pitch, cell_pitch)
	var inset: Vector2 = Vector2(cell_inset, cell_inset)
	var cell_rect: Rect2 = Rect2(cell_position + inset, cell_size - inset * 2.0)
	draw_rect(cell_rect, _map.palette[background_index])
	_draw_shape(cell_rect, shape, _map.palette[foreground_index], quarter_turns)
	draw_rect(Rect2(cell_position, cell_size), _GRID_COLOR, false, 1.0)


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
