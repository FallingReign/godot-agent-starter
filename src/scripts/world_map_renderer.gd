class_name WorldMapRenderer
extends Node2D

const _GRID_COLOR: Color = Color(0.12, 0.17, 0.25, 1.0)

var _map: LevelFormat = null
var _cell_pitch: float = 15.0
var _cell_inset: float = 1.0
var _grid_visible: bool = false
var _shape_catalog: CellShapeCatalog = CellShapeCatalog.new()


func configure(level: LevelFormat, pitch: float, inset: float) -> void:
	_map = level
	_cell_pitch = pitch
	_cell_inset = inset
	queue_redraw()


func set_grid_visible(should_show: bool) -> void:
	if _grid_visible == should_show:
		return
	_grid_visible = should_show
	queue_redraw()


func _draw() -> void:
	if _map == null or _map.layers.is_empty():
		return
	var layer: LevelLayer = _map.layers[0]
	_draw_map_backgrounds(layer, 0, _map.size.x, 0, _map.size.y)
	_draw_map_shapes(layer, 0, _map.size.x, 0, _map.size.y)
	if _grid_visible:
		_draw_grid_lines(0, _map.size.x, 0, _map.size.y)


func _draw_map_backgrounds(
	layer: LevelLayer,
	first_column: int,
	last_column: int,
	first_row: int,
	last_row: int,
) -> void:
	for row: int in range(first_row, last_row):
		if first_column >= last_column:
			continue
		var run_start: int = first_column
		var run_background: int = (
			LevelLayer
			. cell_background(
				layer.cell_code_at(first_column, row),
			)
		)
		for column: int in range(first_column + 1, last_column):
			var background_index: int = (
				LevelLayer
				. cell_background(
					layer.cell_code_at(column, row),
				)
			)
			if background_index == run_background:
				continue
			_draw_background_run(row, run_start, column, run_background)
			run_start = column
			run_background = background_index
		_draw_background_run(row, run_start, last_column, run_background)


func _draw_background_run(
	row: int,
	start_column: int,
	end_column: int,
	background_index: int,
) -> void:
	var cell_rect: Rect2 = _world_cell_rect(start_column, row)
	var run_size: Vector2 = Vector2(
		float(end_column - start_column) * _cell_pitch,
		_cell_pitch,
	)
	draw_rect(Rect2(cell_rect.position, run_size), _map.palette[background_index])


func _draw_map_shapes(
	layer: LevelLayer,
	first_column: int,
	last_column: int,
	first_row: int,
	last_row: int,
) -> void:
	for row: int in range(first_row, last_row):
		for column: int in range(first_column, last_column):
			var cell_code: int = layer.cell_code_at(column, row)
			if LevelLayer.cell_shape(cell_code) == CellShapeCatalog.EMPTY_SHAPE:
				continue
			_draw_map_shape(column, row, cell_code)


func _draw_map_shape(column: int, row: int, cell_code: int) -> void:
	var shape: int = LevelLayer.cell_shape(cell_code)
	var quarter_turns: int = LevelLayer.cell_rotation(cell_code)
	var foreground_index: int = LevelLayer.cell_foreground(cell_code)
	var cell_rect: Rect2 = _world_cell_rect(column, row)
	var inset_amount: float = _cell_inset if _grid_visible else 0.0
	var inset: Vector2 = Vector2(inset_amount, inset_amount)
	var fill_rect: Rect2 = Rect2(cell_rect.position + inset, cell_rect.size - inset * 2.0)
	_draw_shape(fill_rect, shape, _map.palette[foreground_index], quarter_turns)


func _draw_grid_lines(
	first_column: int,
	last_column: int,
	first_row: int,
	last_row: int,
) -> void:
	if first_column >= last_column or first_row >= last_row:
		return
	var top_left: Vector2 = _world_cell_rect(first_column, first_row).position
	var right: float = top_left.x + float(last_column - first_column) * _cell_pitch
	var bottom: float = top_left.y + float(last_row - first_row) * _cell_pitch
	for column: int in range(first_column, last_column + 1):
		var x: float = top_left.x + float(column - first_column) * _cell_pitch
		draw_line(Vector2(x, top_left.y), Vector2(x, bottom), _GRID_COLOR, 1.0)
	for row: int in range(first_row, last_row + 1):
		var y: float = top_left.y + float(row - first_row) * _cell_pitch
		draw_line(Vector2(top_left.x, y), Vector2(right, y), _GRID_COLOR, 1.0)


func _world_cell_rect(column: int, row: int) -> Rect2:
	var world_cell_position: Vector2 = Vector2(
		float(column) * _cell_pitch,
		float(row) * _cell_pitch,
	)
	return Rect2(world_cell_position, Vector2(_cell_pitch, _cell_pitch))


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
