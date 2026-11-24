"""Turn a set of unit supply cells into a boundary polygon with holes.

Coordinate convention
---------------------
Every cell is a unit square.  The top-left cell's top-left corner is the
origin ``(0, 0)``, ``x`` grows rightwards and ``y`` grows upwards.  Cell
``(row, col)`` therefore spans ``x in [col, col + 1]`` and
``y in [-row - 1, -row]``.

Geometry
--------
A catchment is a set of unit cells.  This module:

1. splits the cells into 4-connected components, so two areas that only
   touch at a corner become separate polygons;
2. walks every component's directed boundary edges (the cell is kept on the
   left of each edge), pairing incoming and outgoing edges with the smallest
   counter-clockwise turn, which yields non-crossing rings;
3. at a *pinch vertex* (a corner visited more than twice, e.g. two lobes of
   one 4-connected component meeting at a point), closes the current walk
   there and starts another, so every ring stays simple;
4. classifies rings by signed area (counter-clockwise shell, clockwise hole),
   assigns each hole to its containing shell and emits one ``MULTIPOLYGON``
   WKT string.

The output covers exactly the supplied cells: the area of the emitted
polygon (shells minus holes) always equals the number of cells.
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Sequence

from .grid import Cell

#: Direction code -> unit vector (x, y), counter-clockwise order starting east.
_EDGE_VECTORS: dict[str, tuple[int, int]] = {
    "E": (1, 0),
    "N": (0, 1),
    "W": (-1, 0),
    "S": (0, -1),
}
#: Counter-clockwise angle of each edge direction, in degrees.
_EDGE_ANGLE = {"E": 0, "N": 90, "W": 180, "S": 270}

Point = tuple[int, int]


def cell_corners(row: int, col: int) -> tuple[Point, Point, Point, Point]:
    """Return the four corners of cell ``(row, col)``: SW, SE, NE, NW."""
    x0, x1 = col, col + 1
    y0, y1 = -row - 1, -row
    return (x0, y0), (x1, y0), (x1, y1), (x0, y1)


def cell_bounds(row: int, col: int) -> tuple[int, int, int, int]:
    """Return ``(min_x, min_y, max_x, max_y)`` of cell ``(row, col)``."""
    return col, -row - 1, col + 1, -row


def _four_neighbours(cell: Cell) -> list[Cell]:
    row, col = cell
    return [(row - 1, col), (row + 1, col), (row, col - 1), (row, col + 1)]


def four_connected_components(cells: Iterable[Cell]) -> list[set[Cell]]:
    """Split cells into edge-connected (4-neighbour) components."""
    remaining = set(cells)
    components: list[set[Cell]] = []
    while remaining:
        seed = next(iter(remaining))
        remaining.remove(seed)
        component = {seed}
        queue: deque[Cell] = deque([seed])
        while queue:
            cell = queue.popleft()
            for neighbour in _four_neighbours(cell):
                if neighbour in remaining:
                    remaining.remove(neighbour)
                    component.add(neighbour)
                    queue.append(neighbour)
        components.append(component)
    return components


def _boundary_edges(component: set[Cell]) -> list[tuple[Point, Point]]:
    """Directed boundary edges of one component, cell interior on the left."""
    edges: list[tuple[Point, Point]] = []
    for row, col in component:
        sw, se, ne, nw = cell_corners(row, col)
        # Bottom edge: west -> east (the cell is above the edge, i.e. left
        # when facing east).
        if (row + 1, col) not in component:
            edges.append((sw, se))
        # Right edge: south -> north.
        if (row, col + 1) not in component:
            edges.append((se, ne))
        # Top edge: east -> west.
        if (row - 1, col) not in component:
            edges.append((ne, nw))
        # Left edge: north -> south.
        if (row, col - 1) not in component:
            edges.append((nw, sw))
    return edges


def _ring_signed_area(ring: Sequence[Point]) -> int:
    """Twice the signed area; positive for counter-clockwise rings."""
    total = 0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        total += x1 * y2 - x2 * y1
    return total


def _trace_rings(edges: Sequence[tuple[Point, Point]]) -> list[list[Point]]:
    """Trace directed boundary edges into simple rings.

    The boundary is a balanced directed graph: every vertex has the same
    number of incoming and outgoing edges.  We first build an explicit
    *successor table* mapping each directed edge to the edge that follows it
    at its end vertex -- the first outgoing edge encountered counter-
    clockwise from the reverse of the incoming direction, which is the
    standard left-hand boundary pairing (interior stays on the left).
    Walking this permutation partitions the edges into rings.  At a pinch
    vertex several pairs coexist, so each ring stays a simple loop that
    merely meets the others at the shared point.
    """
    edge_set = list(edges)
    outgoing_at: dict[Point, list[str]] = {}
    for start, end in edge_set:
        outgoing_at.setdefault(start, []).append(_edge_direction(start, end))
    available: dict[Point, list[str]] = {
        vertex: list(codes) for vertex, codes in outgoing_at.items()
    }

    # successor[edge] = (vertex, outgoing code)
    successor: dict[tuple[Point, Point], tuple[Point, str]] = {}
    for start, end in edge_set:
        incoming_code = _edge_direction(start, end)
        reverse_angle = (_EDGE_ANGLE[incoming_code] + 180) % 360
        candidates = available[end]
        if not candidates:  # pragma: no cover - balanced graph invariant
            raise RuntimeError(f"no outgoing edge at pinch vertex {end}")
        # First outgoing edge counter-clockwise from the reversed incoming edge.
        best_index = min(
            range(len(candidates)),
            key=lambda i: (_EDGE_ANGLE[candidates[i]] - reverse_angle) % 360,
        )
        next_code = candidates.pop(best_index)
        successor[(start, end)] = (end, next_code)

    unused_edges = set(edge_set)
    rings: list[list[Point]] = []
    while unused_edges:
        start_edge = min(unused_edges)
        unused_edges.discard(start_edge)
        ring = [start_edge[0], start_edge[1]]
        current_edge = start_edge
        while True:
            vertex, code = successor[current_edge]
            vector = _EDGE_VECTORS[code]
            next_edge = (vertex, (vertex[0] + vector[0], vertex[1] + vector[1]))
            if next_edge == start_edge:
                break
            if next_edge not in unused_edges:  # pragma: no cover - permutation invariant
                raise RuntimeError(f"boundary edge {next_edge} consumed twice")
            unused_edges.discard(next_edge)
            ring.append(next_edge[1])
            current_edge = next_edge
        # ``ring`` already ends back on its starting vertex; drop the duplicate
        # so callers see an open ring and _ring_wkt closes it exactly once.
        if ring[-1] == ring[0]:
            ring.pop()
        rings.append(ring)
    return rings


def _edge_direction(start: Point, end: Point) -> str:
    dx, dy = end[0] - start[0], end[1] - start[1]
    for code, vector in _EDGE_VECTORS.items():
        if vector == (dx, dy):
            return code
    raise ValueError(f"edge {start} -> {end} is not a unit grid edge")  # pragma: no cover


def _point_in_ring(point: Point, ring: Sequence[Point]) -> bool:
    """Ray-casting containment test (rings are simple integer polygons)."""
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y):
            crossing_x = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if crossing_x > x:
                inside = not inside
    return inside


def _ring_wkt(ring: Sequence[Point]) -> str:
    # WKT rings repeat the starting point at the end.
    closed = list(ring) + [ring[0]]
    body = ", ".join(f"{x} {y}" for x, y in closed)
    return f"({body})"


def component_to_polygons(component: set[Cell]) -> list[tuple[list[Point], list[list[Point]]]]:
    """Trace one 4-connected component into one or more ``(shell, holes)`` pairs.

    A normal component produces a single pair; a component pinched at a
    diagonal corner produces one pair per lobe, each a simple polygon.
    """
    rings = _trace_rings(_boundary_edges(component))
    shells: list[list[Point]] = []
    holes: list[list[Point]] = []
    for ring in rings:
        if _ring_signed_area(ring) > 0:
            shells.append(ring)
        else:
            holes.append(ring)

    polygons: list[tuple[list[Point], list[list[Point]]]] = []
    for shell in shells:
        owned = [hole for hole in holes if _point_in_ring(hole[0], shell)]
        polygons.append((shell, owned))
    if not polygons:  # pragma: no cover - every component has an exterior ring
        raise RuntimeError("component traced to no exterior ring")
    return polygons


def catchment_wkt(cells: Iterable[Cell]) -> str | None:
    """Build the ``MULTIPOLYGON`` WKT covering exactly ``cells``.

    Returns ``None`` for an empty cell set (the outfall then has no supply
    and must not appear in the export).
    """
    cell_set = set(cells)
    if not cell_set:
        return None
    polygons: list[str] = []
    for component in four_connected_components(cell_set):
        for shell, holes in component_to_polygons(component):
            rings = [_ring_wkt(shell), *(_ring_wkt(hole) for hole in holes)]
            polygons.append("(" + ", ".join(rings) + ")")
    return "MULTIPOLYGON (" + ", ".join(polygons) + ")"


def catchment_polygons(
    cells: Iterable[Cell],
) -> list[tuple[list[Point], list[list[Point]]]]:
    """Structured form of :func:`catchment_wkt`, handier for tests."""
    polygons: list[tuple[list[Point], list[list[Point]]]] = []
    for component in four_connected_components(set(cells)):
        polygons.extend(component_to_polygons(component))
    return polygons
