"""Deterministic employee walking evidence on an explicit clearance graph.

All weights and outputs use millimetres. This finite rectilinear graph is a
conservative path witness, not a claim of globally shortest routes or that an
unconnected candidate proves the physical space impossible. Required-width
centre lines are checked directly, so an exactly 1200 mm passage is retained.
"""
from collections import defaultdict
from dataclasses import dataclass, field
import json
from math import hypot, isfinite
from statistics import mean

import networkx as nx
import numpy as np
import shapely
from shapely.geometry import GeometryCollection, LineString, Point, Polygon
from shapely.ops import unary_union

from shared.operations_planning import OperationsInput, OperationsLayout, PlannedCorridor

EPS = 1e-6
MAX_GRID_POINTS = 60000
ROUTE_POLICY = 'WEIGHTED_NEAREST_UNVISITED_THEN_DESTINATION_V1'
GRAPH_POLICY = 'RECTILINEAR_CLEARANCE_GRAPH_NETWORKX_V1'


def _xy(point):
    return (round(float(point[0]), 6), round(float(point[1]), 6))


def _length(path):
    return sum(hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(path, path[1:]))


def _parts(geometry):
    if geometry.geom_type == 'Polygon':
        return [geometry] if not geometry.is_empty else []
    return [part for sub in getattr(geometry, 'geoms', []) for part in _parts(sub)]


def _polygon_data(polygon):
    return {'boundary_mm': list(polygon.exterior.coords),
            'holes_mm': [list(ring.coords) for ring in polygon.interiors]}


def _geometry(value, layout=None):
    space = value.base.space
    scope = Polygon(space.boundary.boundary_mm, space.boundary.holes_mm)
    if not scope.is_valid or scope.is_empty or scope.area <= EPS:
        raise ValueError('INVALID_SCOPE: explicit scope must be valid; routing never repairs it')
    blockers = [Polygon(zone.boundary_mm, zone.holes_mm) for zone in space.exclusions]
    blockers += [LineString([wall.start_mm, wall.end_mm]) for wall in space.barriers]
    if layout is not None:
        # Including the whole assembly closes a structural gap between its backs.
        blockers += [Polygon(item.footprint_mm) for item in layout.assemblies]
        blockers += [Polygon(item.footprint_mm) for item in layout.shelves]
    if any(not item.is_valid or item.is_empty for item in blockers):
        raise ValueError('INVALID_OBSTACLE: routing never repairs invalid obstacles')
    obstacles = unary_union(blockers).simplify(0, preserve_topology=True) if blockers else GeometryCollection()
    return scope, obstacles


def _clear(shape, scope, obstacles, radius, boundary=None):
    return (scope.buffer(EPS).covers(shape)
            and shape.distance(scope.boundary if boundary is None else boundary) + EPS >= radius
            and (obstacles.is_empty or shape.distance(obstacles) + EPS >= radius))


def _entrance(value, scope, obstacles):
    """Only the explicit opening exempts the outside wall from entry clearance."""
    radius = float(value.base.rules.aisle_width_mm) / 2
    opening = LineString(value.entrance_opening_mm)
    result = {'status': 'FAIL', 'opening_width_mm': opening.length,
              'required_width_mm': radius * 2, 'path_mm': [], 'diagnostics': []}
    if opening.length + EPS < 2 * radius:
        result['diagnostics'].append('ENTRANCE_OPENING_TOO_NARROW')
        return result
    outer = LineString(scope.exterior.coords)
    if opening.length <= EPS or not outer.buffer(EPS).covers(opening):
        result['diagnostics'].append('ENTRANCE_OPENING_NOT_ON_EXPLICIT_OUTER_BOUNDARY')
        return result
    a, b = list(opening.coords)
    middle = opening.interpolate(0.5, normalized=True)
    normal = (-(b[1] - a[1]) / opening.length, (b[0] - a[0]) / opening.length)
    gate = _xy(middle.coords[0])
    # Remove precisely the supplied door segment, never a guessed wall opening.
    closed_boundary = scope.boundary.difference(opening)
    for sign in (1, -1):
        throat = _xy((gate[0] + sign * normal[0] * radius,
                      gate[1] + sign * normal[1] * radius))
        line = LineString([gate, throat])
        if (_clear(line, scope, obstacles, radius, closed_boundary)
                and _clear(Point(throat), scope, obstacles, radius)):
            result.update(status='PASS', gate_point_mm=gate, throat_point_mm=throat,
                          path_mm=[gate, throat], distance_mm=line.length)
            return result
    result['diagnostics'].append('ENTRANCE_REQUIRED_WIDTH_ACCESS_NOT_PROVEN')
    return result


@dataclass
class _WalkingGraph:
    graph: nx.Graph
    scope: object
    obstacles: object
    radius: float
    grid_point_count: int
    error: str | None = None
    cache: dict = field(default_factory=dict)

    def paths_from(self, point):
        point = _xy(point)
        if point not in self.cache:
            self.cache[point] = (nx.single_source_dijkstra(self.graph, point, weight='length_mm')
                                 if point in self.graph else ({}, {}))
        return self.cache[point]

    def route(self, start, end):
        start, end = _xy(start), _xy(end)
        distances, paths = self.paths_from(start)
        if end not in distances:
            return {'reachable': False, 'distance_mm': None, 'path_mm': []}
        return {'reachable': True, 'distance_mm': float(distances[end]),
                'path_mm': paths[end]}


def _build_graph(value, layout=None, points=()):
    scope, obstacles = _geometry(value, layout)
    radius = float(value.base.rules.aisle_width_mm) / 2
    coordinates = [*shapely.get_coordinates(scope.boundary), *shapely.get_coordinates(obstacles)]
    xs, ys = set(), set()
    for x, y in coordinates:
        # Original vertices and each offset are critical coordinates; circular
        # corner cuts are never assumed traversable by an untested graph edge.
        xs.update(round(float(x) + delta, 6) for delta in (-radius, 0, radius))
        ys.update(round(float(y) + delta, 6) for delta in (-radius, 0, radius))
    for point in points:
        x, y = _xy(point)
        xs.add(x)
        ys.add(y)
    minx, miny, maxx, maxy = scope.bounds
    xs = sorted(x for x in xs if minx - EPS <= x <= maxx + EPS)
    ys = sorted(y for y in ys if miny - EPS <= y <= maxy + EPS)
    count = len(xs) * len(ys)
    result = _WalkingGraph(nx.Graph(), scope, obstacles, radius, count)
    if count > MAX_GRID_POINTS:
        result.error = 'FINITE_GRAPH_LIMIT_REACHED_NO_REACHABILITY_CLAIM'
        return result
    if not xs or not ys:
        return result
    meshx, meshy = np.meshgrid(xs, ys, indexing='ij')
    coordinates = np.column_stack((meshx.ravel(), meshy.ravel()))
    points_geometry = shapely.points(coordinates)
    valid = (shapely.covers(scope, points_geometry)
             & (shapely.distance(points_geometry, scope.boundary) + EPS >= radius))
    if not obstacles.is_empty:
        valid &= shapely.distance(points_geometry, obstacles) + EPS >= radius
    indices = np.arange(count).reshape((len(xs), len(ys)))
    starts = np.concatenate((indices[:-1, :].ravel(), indices[:, :-1].ravel()))
    ends = np.concatenate((indices[1:, :].ravel(), indices[:, 1:].ravel()))
    keep = valid[starts] & valid[ends]
    starts, ends = starts[keep], ends[keep]
    lines = shapely.linestrings(np.stack((coordinates[starts], coordinates[ends]), axis=1))
    valid_lines = (shapely.covers(scope, lines)
                   & (shapely.distance(lines, scope.boundary) + EPS >= radius))
    if not obstacles.is_empty:
        valid_lines &= shapely.distance(lines, obstacles) + EPS >= radius
    result.graph.add_nodes_from(tuple(coordinates[index]) for index in np.flatnonzero(valid))
    for start, end, length in zip(starts[valid_lines], ends[valid_lines], shapely.length(lines[valid_lines])):
        result.graph.add_edge(tuple(coordinates[start]), tuple(coordinates[end]), length_mm=float(length))
    return result


def _planned_from_path(path, width, scope, identifier, role):
    if len(path) < 2 or _length(path) <= EPS:
        return []
    # Square joins on axis-aligned paths reserve the turning footprint. The
    # actual route graph, not this displayed planning polygon, proves clearance.
    polygon = LineString(path).buffer(width / 2, cap_style=3, join_style=2).intersection(scope)
    return [PlannedCorridor(id=f'{identifier}-{index + 1}', role=role, **_polygon_data(part))
            for index, part in enumerate(_parts(polygon)) if part.area > EPS]


def corridor_plans(value: OperationsInput, direction: int, depth: float) -> list[dict]:
    """Reserve a perimeter circulation loop and workpoint access before shelves."""
    if direction not in (0, 90) or depth <= 0:
        raise ValueError('Corridor direction must be 0/90 and side depth must be positive')
    scope, obstacles = _geometry(value)
    entry = _entrance(value, scope, obstacles)
    if entry['status'] != 'PASS':
        return []
    rules = value.base.rules
    clearance, width, ends = (float(rules.boundary_clearance_mm), float(rules.aisle_width_mm),
                              float(rules.end_aisle_mm))
    if ends + EPS < width:
        return []
    def uv(point):
        return point if direction == 0 else (point[1], point[0])
    def xy(point):
        return _xy(point if direction == 0 else (point[1], point[0]))
    minx, miny, maxx, maxy = scope.bounds
    u0, v0 = uv((minx, miny))
    u1, v1 = uv((maxx, maxy))
    low_u, high_u = u0 + clearance + ends / 2, u1 - clearance - ends / 2
    low_v = v0 + clearance + depth + width / 2
    high_v = v1 - clearance - depth - width / 2
    if low_u >= high_u or low_v >= high_v:
        return []
    corners = [xy((low_u, low_v)), xy((low_u, high_v)),
               xy((high_u, high_v)), xy((high_u, low_v))]
    entry_u = uv(value.workpoints.entrance.point_mm)[0]
    # On a short end, a middle connector remains a distinct test alternative.
    cross_u = entry_u if low_u + ends < entry_u < high_u - ends else (low_u + high_u) / 2
    cross = [xy((cross_u, low_v)), xy((cross_u, high_v))]
    work = {key: _xy(getattr(value.workpoints, key).point_mm)
            for key in ('entrance', 'receiving', 'pick_start', 'packing')}
    navigation = _build_graph(value, points=[*corners, *cross, *work.values(), entry['throat_point_mm']])
    if navigation.error or any(point not in navigation.graph for point in work.values()):
        return []
    if entry['throat_point_mm'] not in navigation.graph:
        return []
    # A fixed facility at a preferred loop corner can move the loop in the empty
    # space. The selected anchors and complete detours are then reserved.
    anchors = [corner if corner in navigation.graph else
               min(navigation.graph, key=lambda p: (hypot(p[0] - corner[0], p[1] - corner[1]), p))
               for corner in corners]
    corridors, skeleton = [], set()
    for index, (start, end) in enumerate(zip(anchors, anchors[1:] + anchors[:1])):
        route = navigation.route(start, end)
        if not route['reachable']:
            return []
        skeleton.update(route['path_mm'])
        role = 'END_AISLE' if index in (0, 2) else 'WALL_FRONT_AISLE'
        corridors.extend(_planned_from_path(route['path_mm'], ends if index in (0, 2) else width,
                                            scope, f'loop-{index}', role))
    if not skeleton:
        return []
    distances, paths = nx.multi_source_dijkstra(navigation.graph, sorted(skeleton), weight='length_mm')
    connector_paths = []
    for role, point in sorted(work.items()):
        if point not in distances:
            return []
        path = list(reversed(paths[point]))
        connector_paths.append(path)
        corridors.extend(_planned_from_path(path, width, scope, f'workpoint-{role}', 'WORKPOINT_ACCESS'))
    entry_route = navigation.route(entry['throat_point_mm'], work['entrance'])
    if not entry_route['reachable']:
        return []
    entry_path = entry['path_mm'] + entry_route['path_mm'][1:]
    corridors.extend(_planned_from_path(entry_path, width, scope, 'entrance-opening', 'ENTRANCE_ACCESS'))
    result = [{'id': 'PERIMETER_LOOP', 'corridors': corridors}]
    cross_route = navigation.route(cross[0], cross[1])
    if cross_route['reachable']:
        extra = _planned_from_path(cross_route['path_mm'], width, scope, 'entry-cross-link', 'CROSS_AISLE')
        result.append({'id': 'PERIMETER_LOOP_WITH_ENTRY_CROSS_LINK', 'corridors': [*corridors, *extra]})
    return result


