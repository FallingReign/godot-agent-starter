class_name Main
extends Control

## Entry point. Keep this thin: wiring and presentation only.
## Game rules belong in plain RefCounted classes under scripts/logic/.

enum ScreenState {
	MENU,
	WORLD,
}
const _BACKGROUND_COLOR: Color = Color(0.035, 0.055, 0.095, 1.0)
const _MAP_PATH: String = "res://content/maps/red_grid.json"
const _LOGICAL_VIEWPORT_SIZE: Vector2i = Vector2i(1920, 1080)
const _DEFAULT_WINDOW_SIZE: Vector2i = Vector2i(1920, 1080)
const _VIEW_CENTER: Vector2 = Vector2(960.0, 540.0)
const _PLAYER_START_POSITION: Vector2 = Vector2(960.0, 540.0)
const _PLAYER_RADIUS: float = 10.0
const _PLAYER_COLOR: Color = Color(0.96, 0.97, 1.0, 1.0)
const _PLAYER_OUTLINE_COLOR: Color = Color(0.95, 0.82, 0.42, 1.0)

## @tune cell.pitch
## Distance between cell centres in logical units.
@export var cell_pitch: float = 15.0

## @tune cell.inset
## Gap inside each cell in logical units so the grid remains visibly composed of cells.
@export var cell_inset: float = 1.0

var _screen_state: ScreenState = ScreenState.MENU
var _grid_visible: bool = false
var _debug_visible: bool = false
var _map: LevelFormat = null
var _player_motion: CharacterMotion = null
var _camera_position: Vector2 = Vector2.ZERO
var _config: GameConfig = GameConfig.new()

@onready var _menu: CenterContainer = $Menu
@onready var _play_button: Button = $Menu/Panel/Margin/Content/PlayButton
@onready var _quit_button: Button = $Menu/Panel/Margin/Content/QuitButton
@onready var _subtitle: Label = $Menu/Panel/Margin/Content/Subtitle
@onready var _world_map_viewport: SubViewport = $WorldMapViewport
@onready var _world_map: WorldMapRenderer = $WorldMapViewport/WorldMap
@onready var _world_map_texture: Sprite2D = $WorldMapTexture
@onready var _debug_panel: PerformanceOverlay = $DebugPanel


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
	else:
		var world_size: Vector2 = Vector2(
			float(_map.size.x) * cell_pitch,
			float(_map.size.y) * cell_pitch,
		)
		_player_motion = CharacterMotion.new(_PLAYER_START_POSITION, world_size, _config)
		_camera_position = _camera_target_for(_player_motion.position)
		_world_map_viewport.size = Vector2i(int(world_size.x), int(world_size.y))
		_world_map_texture.texture = _world_map_viewport.get_texture()
		_world_map_texture.texture_filter = CanvasItem.TEXTURE_FILTER_NEAREST
		_world_map.configure(_map, cell_pitch, cell_inset)
		_update_world_map_position()
		_refresh_world_map_viewport()
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
	_set_menu_visible(false)
	queue_redraw()


func _on_quit_pressed() -> void:
	get_tree().quit()


func _physics_process(delta: float) -> void:
	if _screen_state != ScreenState.WORLD or _player_motion == null:
		return
	var previous_position: Vector2 = _player_motion.position
	var previous_camera_position: Vector2 = _camera_position
	_player_motion.step(_movement_input(), delta)
	_camera_position = _camera_target_for(_player_motion.position)
	if _player_motion.position != previous_position or _camera_position != previous_camera_position:
		if _camera_position != previous_camera_position:
			_update_world_map_position()
		queue_redraw()


func _movement_input() -> Vector2:
	var direction: Vector2 = Vector2.ZERO
	if Input.is_key_pressed(KEY_A) or Input.is_key_pressed(KEY_LEFT):
		direction.x -= 1.0
	if Input.is_key_pressed(KEY_D) or Input.is_key_pressed(KEY_RIGHT):
		direction.x += 1.0
	if Input.is_key_pressed(KEY_W) or Input.is_key_pressed(KEY_UP):
		direction.y -= 1.0
	if Input.is_key_pressed(KEY_S) or Input.is_key_pressed(KEY_DOWN):
		direction.y += 1.0
	return direction


func _camera_target_for(target_position: Vector2) -> Vector2:
	if _map == null:
		return target_position
	var world_size: Vector2 = Vector2(
		float(_map.size.x) * cell_pitch,
		float(_map.size.y) * cell_pitch,
	)
	var target: Vector2 = target_position
	var maximum_x: float = maxf(_VIEW_CENTER.x, world_size.x - _VIEW_CENTER.x)
	var maximum_y: float = maxf(_VIEW_CENTER.y, world_size.y - _VIEW_CENTER.y)
	target.x = clampf(target.x, _VIEW_CENTER.x, maximum_x)
	target.y = clampf(target.y, _VIEW_CENTER.y, maximum_y)
	return target


func _update_world_map_position() -> void:
	_world_map_texture.position = _VIEW_CENTER - _camera_position


func _refresh_world_map_viewport() -> void:
	_world_map_viewport.render_target_update_mode = SubViewport.UPDATE_ONCE


func _unhandled_input(event: InputEvent) -> void:
	if not event is InputEventKey:
		return
	var key_event: InputEventKey = event
	if not key_event.pressed or key_event.echo:
		return
	if key_event.keycode == KEY_F2 and _screen_state == ScreenState.WORLD:
		_grid_visible = not _grid_visible
		_world_map.set_grid_visible(_grid_visible)
		_refresh_world_map_viewport()
		get_viewport().set_input_as_handled()
	elif key_event.keycode == KEY_F3 and _screen_state == ScreenState.WORLD:
		_debug_visible = not _debug_visible
		_debug_panel.set_diagnostics_visible(_debug_visible)
		get_viewport().set_input_as_handled()
	elif key_event.keycode == KEY_ESCAPE and _screen_state == ScreenState.WORLD:
		_set_menu_visible(true)
		get_viewport().set_input_as_handled()


func _set_menu_visible(show_menu: bool) -> void:
	_screen_state = ScreenState.MENU if show_menu else ScreenState.WORLD
	if show_menu:
		_menu.show()
		_world_map_texture.hide()
		_debug_panel.set_diagnostics_visible(false)
	else:
		_menu.hide()
		_world_map_texture.visible = _map != null
		_debug_panel.set_diagnostics_visible(_debug_visible)


func _on_resized() -> void:
	queue_redraw()


func _draw() -> void:
	if _screen_state == ScreenState.MENU:
		draw_rect(Rect2(Vector2.ZERO, size), _BACKGROUND_COLOR)
	if _screen_state == ScreenState.WORLD and _player_motion != null:
		_draw_player()


func _draw_player() -> void:
	var player_position: Vector2 = _player_motion.position - _camera_position + _VIEW_CENTER
	draw_circle(player_position, _PLAYER_RADIUS, _PLAYER_COLOR)
	draw_arc(player_position, _PLAYER_RADIUS, 0.0, TAU, 24, _PLAYER_OUTLINE_COLOR, 2.0)
