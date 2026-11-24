"""Raster-cell-set -> polygon geometry: components, holes, rings and WKT."""

import pytest

from app.grid import Cell
from app.polygon import (
    catchment_polygons,
    catchment_wkt,
    cell_bounds,
    cell_corners,
    four_connected_components,
    _ring_signed_area,
)


def polygon_area(shell, holes):
    return _ring_signed_area(shell) / 2 - sum(
        -_ring_signed_area(hole) / 2 for hole in holes
    )


def test_origin_and_axes_put_top_left_cell_at_origin_with_y_up():
    # Cell (0, 0) spans x in [0, 1], y in [-1, 0]; a lower row hangs further down.
    assert cell_corners(0, 0) == ((0, -1), (1, -1), (1, 0), (0, 0))
    assert cell_bounds(2, 3) == (3, -3, 4, -2)


def test_single_cell_wkt_is_a_unit_square():
    wkt = catchment_wkt([(0, 0)])
    assert wkt == "MULTIPOLYGON (((0 -1, 1 -1, 1 0, 0 0, 0 -1)))"


def test_area_always_equals_the_number_of_cells():
    shapes = [
        [(0, 0)],
        [(0, 0), (0, 1), (1, 0), (1, 1)],
        # 3x3 ring with a hole
        [(r, c) for r in range(3) for c in range(3) if (r, c) != (1, 1)],
        # U shape
        [(r, c) for r in range(3) for c in range(3) if not (r == 1 and c == 2)],
        # checkerboard 4x4: every cell isolated from its edge neighbours
        [(r, c) for r in range(4) for c in range(4) if (r + c) % 2 == 0],
    ]
    for cells in shapes:
        polygons = catchment_polygons(cells)
        assert sum(polygon_area(shell, holes) for shell, holes in polygons) == len(cells)


def test_rings_are_oriented_shell_ccw_and_hole_cw():
    ring_cells = {(r, c) for r in range(3) for c in range(3)} - {(1, 1)}
    shell, holes = catchment_polygons(ring_cells)[0]
    assert _ring_signed_area(shell) > 0
    assert len(holes) == 1
    assert _ring_signed_area(holes[0]) < 0


def test_hole_is_preserved_as_an_inner_ring():
    ring_cells = {(r, c) for r in range(3) for c in range(3)} - {(1, 1)}
    wkt = catchment_wkt(ring_cells)
    # One polygon with two rings (shell + hole), not two polygons.
    assert wkt.startswith("MULTIPOLYGON (((")
    assert wkt.count("), (") == 1
    # The hole is the unit square x in [1, 2], y in [-2, -1], traced clockwise.
    shell, holes = catchment_polygons(ring_cells)[0]
    assert len(holes) == 1
    assert set(holes[0]) == {(1, -2), (1, -1), (2, -1), (2, -2)}
    assert _ring_signed_area(holes[0]) < 0


def test_corner_touching_areas_become_separate_polygons():
    cells = [(0, 0), (1, 1)]
    components = four_connected_components(cells)
    assert sorted(sorted(c) for c in components) == [[(0, 0)], [(1, 1)]]
    polygons = catchment_polygons(cells)
    assert len(polygons) == 2
    wkt = catchment_wkt(cells)
    assert wkt.count("((") == 2  # two polygon members, no merged bowtie


def test_pinch_vertex_is_split_into_simple_rings():
    # 3x3 minus the centre and the bottom-right cell: one 4-connected
    # component whose boundary pinches at a corner into two lobes.
    cells = {(r, c) for r in range(3) for c in range(3)} - {(1, 1), (2, 2)}
    assert len(four_connected_components(cells)) == 1
    polygons = catchment_polygons(cells)
    # Every emitted ring must be simple (no repeated vertex except closure).
    for shell, holes in polygons:
        assert len(shell) == len(set(shell))
        for hole in holes:
            assert len(hole) == len(set(hole))
    # Total covered area still equals the number of cells.
    assert sum(polygon_area(shell, holes) for shell, holes in polygons) == len(cells)


def test_edge_connected_cells_stay_one_polygon():
    cells = [(0, 0), (0, 1)]
    assert len(four_connected_components(cells)) == 1
    shell, holes = catchment_polygons(cells)[0]
    assert holes == []
    assert polygon_area(shell, holes) == 2


def test_wkt_rings_are_closed_and_use_integer_coordinates():
    wkt = catchment_wkt([(r, c) for r in range(2) for c in range(2)])
    body = wkt.removeprefix("MULTIPOLYGON ")
    # Every ring repeats its first point as its last point.
    for ring_text in body.replace("((", "(").replace("))", ")").split("), ("):
        points = [pair.split() for pair in ring_text.strip("()").split(", ")]
        assert points[0] == points[-1]
        for x, y in points:
            int(x), int(y)


def test_empty_set_has_no_polygon():
    assert catchment_wkt([]) is None
    assert catchment_polygons([]) == []


def test_generated_wkt_is_ogc_valid_and_covers_exactly_the_cells():
    """Independent validation with shapely when it is installed (skips otherwise)."""
    pytest.importorskip("shapely")
    from shapely import wkt as shapely_wkt
    from shapely.geometry import box
    from shapely.ops import unary_union

    import random

    random.seed(2026)
    for _ in range(300):
        rows, cols = random.randint(1, 8), random.randint(1, 8)
        cells = {
            (r, c)
            for r in range(rows)
            for c in range(cols)
            if random.random() < 0.55
        }
        if not cells:
            assert catchment_wkt(cells) is None
            continue
        geometry = shapely_wkt.loads(catchment_wkt(cells))
        assert geometry.is_valid
        assert geometry.area == len(cells)
        expected = unary_union([box(*cell_bounds(r, c)) for r, c in cells])
        assert geometry.equals(expected)


@pytest.mark.parametrize(
    "cells",
    [
        # plus shape
        [(1, 0), (0, 1), (1, 1), (2, 1), (1, 2)],
        # spiral-ish 5x5
        [(r, c) for r in range(5) for c in range(5) if (r, c) not in {(1, 1), (2, 2)}],
        # long thin winding strip
        [(0, c) for c in range(4)] + [(r, 3) for r in range(1, 4)] + [(3, c) for c in range(3)],
    ],
)
def test_exact_cover_of_unit_cells(cells: list[Cell]):
    """Union of the emitted polygons equals exactly the input cell squares."""
    expected_squares = {cell_bounds(r, c) for r, c in cells}
    covered: set[tuple[int, int, int, int]] = set()
    for shell, holes in catchment_polygons(cells):
        inside = _squares_inside_shell(shell)
        for hole in holes:
            inside -= _squares_inside_shell(hole)
        covered |= inside
    assert covered == expected_squares


def _squares_inside_shell(shell) -> set[tuple[int, int, int, int]]:
    """Enumerate every unit square whose centre lies inside the shell ring."""
    xs = sorted({x for x, _ in shell})
    ys = sorted({y for _, y in shell})
    found = set()
    for x0, x1 in zip(xs, xs[1:]):
        for y0, y1 in zip(ys, ys[1:]):
            centre = ((x0 + x1) / 2, (y0 + y1) / 2)
            if _point_in_ring(centre, shell):
                found.add((x0, y0, x1, y1))
    return found


def _point_in_ring(point, ring) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y):
            crossing = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if crossing > x:
                inside = not inside
    return inside
