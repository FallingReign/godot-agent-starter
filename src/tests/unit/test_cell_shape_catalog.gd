extends GutTest


func test_shape_ids_resolve_to_catalogued_geometry() -> void:
	var catalog: CellShapeCatalog = CellShapeCatalog.new()
	var square_shape: int = CellShapeCatalog.shape_code_for("SQ")
	var triangle_shape: int = CellShapeCatalog.shape_code_for("TR")
	var circle_shape: int = CellShapeCatalog.shape_code_for("CI")
	var right_triangle_shape: int = CellShapeCatalog.shape_code_for("RT")
	var half_cell_shape: int = CellShapeCatalog.shape_code_for("HC")
	var corner_shape: int = CellShapeCatalog.shape_code_for("CO")
	assert_eq(square_shape, CellShapeCatalog.FILLED_SQUARE_SHAPE)
	assert_eq(triangle_shape, CellShapeCatalog.TRIANGLE_SHAPE)
	assert_eq(circle_shape, CellShapeCatalog.CIRCLE_SHAPE)
	assert_eq(right_triangle_shape, CellShapeCatalog.RIGHT_TRIANGLE_SHAPE)
	assert_eq(half_cell_shape, CellShapeCatalog.HALF_CELL_SHAPE)
	assert_eq(corner_shape, CellShapeCatalog.CORNER_SHAPE)
	assert_eq(catalog.polygon_for(square_shape).size(), 4)
	assert_eq(catalog.polygon_for(triangle_shape).size(), 3)
	assert_eq(catalog.polygon_for(circle_shape).size(), 12)
	assert_eq(catalog.polygon_for(right_triangle_shape).size(), 3)
	assert_eq(catalog.polygon_for(half_cell_shape).size(), 4)
	assert_eq(catalog.polygon_for(corner_shape).size(), 6)


func test_unknown_shape_id_is_rejected() -> void:
	var unknown_shape: int = CellShapeCatalog.shape_code_for("ZZ")
	assert_eq(unknown_shape, -1)
	assert_false(CellShapeCatalog.is_known_shape(unknown_shape))
