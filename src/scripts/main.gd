class_name Main
extends Control

## Entry point. Keep this thin: wiring and presentation only.
## Game rules belong in plain RefCounted classes under scripts/logic/.

enum ScreenState {
	MENU,
	WORLD,
}

const _BACKGROUND_COLOR: Color = Color(0.035, 0.055, 0.095, 1.0)
const _CELL_COLOR_A: Color = Color(0.055, 0.085, 0.14, 1.0)
const _CELL_COLOR_B: Color = Color(0.065, 0.1, 0.16, 1.0)
const _GRID_COLOR: Color = Color(0.12, 0.17, 0.25, 1.0)

## @tune cell.pitch_px
## Distance between cell centres in the initial world view.
@export var cell_pitch_px: float = 18.0

## @tune cell.inset_px
## Gap inside each cell so the grid remains visibly composed of cells.
@export var cell_inset_px: float = 1.0

var _screen_state: ScreenState = ScreenState.MENU

@onready var _menu: CenterContainer = $Menu
@onready var _play_button: Button = $Menu/Panel/Margin/Content/PlayButton
@onready var _quit_button: Button = $Menu/Panel/Margin/Content/QuitButton


func _ready() -> void:
	_play_button.pressed.connect(_on_play_pressed)
	_quit_button.pressed.connect(_on_quit_pressed)
	resized.connect(_on_resized)
	queue_redraw()


func _on_play_pressed() -> void:
	_screen_state = ScreenState.WORLD
	_menu.hide()
	queue_redraw()


func _on_quit_pressed() -> void:
	get_tree().quit()


func _on_resized() -> void:
	queue_redraw()


func _draw() -> void:
	draw_rect(Rect2(Vector2.ZERO, size), _BACKGROUND_COLOR)
	var column_count: int = int(size.x / cell_pitch_px) + 1
	var row_count: int = int(size.y / cell_pitch_px) + 1
	for row: int in range(row_count):
		for column: int in range(column_count):
			var cell_position: Vector2 = Vector2(float(column) * cell_pitch_px, float(row) * cell_pitch_px)
			var cell_size: Vector2 = Vector2(cell_pitch_px, cell_pitch_px)
			var cell_inset: Vector2 = Vector2(cell_inset_px, cell_inset_px)
			var fill_color: Color = _CELL_COLOR_A
			if (row + column) % 2 == 1:
				fill_color = _CELL_COLOR_B
			draw_rect(Rect2(cell_position + cell_inset, cell_size - cell_inset * 2.0), fill_color)
			draw_rect(Rect2(cell_position, cell_size), _GRID_COLOR, false, 1.0)
