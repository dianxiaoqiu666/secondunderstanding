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

from shared.operations_planning import OperationsInput, OperationsLayout, PlannedCorridor, operations_request

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
    space = operations_request(value).space
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


def _internal_workpoints(value):
    points = {role: _xy(getattr(value.workpoints, role).point_mm)
              for role in ('entrance', 'receiving', 'pick_start', 'packing')}
    if value.door_connection is not None:
        points['entrance'] = _xy(value.door_connection.inside_point_mm)
    return points


def _entry_origin(entry):
    return entry.get('outside_point_mm', entry.get('gate_point_mm'))


def _door_configuration_issues(value, scope):
    door = value.door_connection
    if door is None:
        return []
    issues = []
    opening = LineString(value.entrance_opening_mm)
    if opening.length <= EPS or not LineString(scope.exterior.coords).buffer(EPS).covers(opening):
        issues.append('DOOR_OPENING_NOT_ON_LOGICAL_SCOPE_BOUNDARY')
    region = Polygon(door.connection_region.boundary_mm, door.connection_region.holes_mm)
    if (any(ring[0] != ring[-1] for ring in [door.connection_region.boundary_mm, *door.connection_region.holes_mm])
            or not region.is_valid or region.is_empty or region.area <= EPS):
        issues.append('DOOR_CONNECTION_REGION_INVALID')
    if scope.covers(Point(door.outside_point_mm)):
        issues.append('DOOR_OUTSIDE_POINT_IS_NOT_OUTSIDE')
    if not scope.contains(Point(door.inside_point_mm)):
        issues.append('DOOR_INSIDE_POINT_IS_NOT_INSIDE')
    if hypot(value.workpoints.entrance.point_mm[0] - door.outside_point_mm[0],
             value.workpoints.entrance.point_mm[1] - door.outside_point_mm[1]) > EPS:
        issues.append('ENTRANCE_WORKPOINT_MUST_EQUAL_EXPLICIT_OUTSIDE_POINT')
    identifiers = [wall.id for wall in value.physical_perimeter_walls]
    if len(identifiers) != len(set(identifiers)):
        issues.append('DUPLICATE_PHYSICAL_PERIMETER_WALL_ID')
    outer = LineString(scope.exterior.coords)
    if any(not outer.buffer(EPS).covers(LineString([wall.start_mm, wall.end_mm]))
           for wall in value.physical_perimeter_walls):
        issues.append('PHYSICAL_PERIMETER_WALL_NOT_ON_DECLARED_PERIMETER')
    return issues


def validate_door_configuration(value: OperationsInput) -> dict:
    """Input facts only: a closed door remains valid input and fails reachability."""
    scope = Polygon(value.base.space.boundary.boundary_mm, value.base.space.boundary.holes_mm)
    issues = _door_configuration_issues(value, scope) if scope.is_valid else ['INVALID_LOGICAL_SCOPE']
    return {'status': 'FAIL' if issues else 'PASS',
            'code': 'SCENARIO_CONFIGURATION_INVALID' if issues else None,
            'issues': issues, 'outside_proof_required': value.door_connection is not None}


def _door_link_clear_width(value, obstacles):
    door = value.door_connection
    if door is None or not value.physical_perimeter_walls:
        return None
    outside, inside = door.outside_point_mm, door.inside_point_mm
    length = hypot(inside[0] - outside[0], inside[1] - outside[1])
    if length <= EPS:
        return 0.0
    normal = ((inside[0] - outside[0]) / length, (inside[1] - outside[1]) / length)
    tangent = (-normal[1], normal[0])
    region = Polygon(door.connection_region.boundary_mm, door.connection_region.holes_mm)
    coordinates = shapely.get_coordinates(region)
    projections = [point[0] * normal[0] + point[1] * normal[1] for point in coordinates]
    span = max(abs(coordinate) for point in coordinates for coordinate in point) * 4 + length + 1
    # The explicit link's two longitudinal ends are observation endpoints,
    # not transverse walls. All side boundaries and region holes constrain width.
    caps = []
    for distance in (min(projections), max(projections)):
        center = (normal[0] * distance, normal[1] * distance)
        caps.append(LineString([(center[0] + sign * tangent[0] * span,
                                 center[1] + sign * tangent[1] * span) for sign in (-1, 1)]))
    sides = region.boundary.difference(unary_union(caps))
    line = LineString([outside, inside])
    widths = [LineString(value.entrance_opening_mm).length]
    if not obstacles.is_empty:
        widths.append(2 * line.distance(obstacles))
    if not sides.is_empty:
        widths.append(2 * line.distance(sides))
    return float(min(widths))


def _explicit_entrance(value, scope, obstacles):
    door = value.door_connection
    radius = float(value.base.rules.aisle_width_mm) / 2
    opening = LineString(value.entrance_opening_mm)
    result = {'status': 'FAIL', 'mode': 'EXPLICIT_OUTSIDE_LINK', 'outside_entry_proof': 'FAIL',
              'opening_width_mm': opening.length, 'required_width_mm': radius * 2,
              'path_mm': [], 'diagnostics': [], 'scope_boundary_role': 'LOGICAL_DOMAIN_NOT_ENTITY_WALL',
              'outside_point_mm': _xy(door.outside_point_mm), 'inside_point_mm': _xy(door.inside_point_mm),
              'connection_region': door.connection_region.model_dump(mode='json')}
    configuration = _door_configuration_issues(value, scope)
    if configuration:
        result.update(configuration_status='INVALID', configuration_code='SCENARIO_CONFIGURATION_INVALID')
        result['diagnostics'].extend(configuration)
        return result
    result['configuration_status'] = 'VALID'
    result['actual_clear_width_mm'] = _door_link_clear_width(value, obstacles)
    if opening.length + EPS < radius * 2:
        result['diagnostics'].append('ENTRANCE_OPENING_TOO_NARROW')
        return result
    if not value.physical_perimeter_walls:
        result['diagnostics'].append('EXPLICIT_PHYSICAL_PERIMETER_WALL_EVIDENCE_MISSING')
        return result
    physical_walls = unary_union([LineString([wall.start_mm, wall.end_mm])
                                  for wall in value.physical_perimeter_walls])
    if any(physical_walls.distance(Point(point)) > EPS for point in value.entrance_opening_mm):
        result['diagnostics'].append('EXPLICIT_DOOR_JAMB_EVIDENCE_MISSING')
        return result
    outside, inside = _xy(door.outside_point_mm), _xy(door.inside_point_mm)
    gate = _xy(opening.interpolate(.5, normalized=True).coords[0])
    tangent = ((opening.coords[-1][0] - opening.coords[0][0]) / opening.length,
               (opening.coords[-1][1] - opening.coords[0][1]) / opening.length)
    if any(abs((point[0] - gate[0]) * tangent[0] + (point[1] - gate[1]) * tangent[1]) > EPS
           for point in (outside, inside)):
        result['diagnostics'].append('EXPLICIT_DOOR_LINK_MUST_FOLLOW_OPENING_NORMAL_WITHOUT_SNAPPING')
        return result
    path = [outside, gate, inside]
    line, external, internal = LineString(path), LineString(path[:2]), LineString(path[1:])
    region = Polygon(door.connection_region.boundary_mm, door.connection_region.holes_mm)
    swept = line.buffer(radius, cap_style=2, join_style=2)
    # The local connection authorizes crossing this logical boundary segment;
    # it never removes, cuts, or ignores a supplied physical wall entity.
    closed_domain_boundary = scope.boundary.difference(opening)
    if (not region.buffer(EPS).covers(swept) or not scope.buffer(EPS).covers(internal)
            or external.intersection(scope).length > EPS
            or line.distance(closed_domain_boundary) + EPS < radius
            or not _clear(Point(inside), scope, obstacles, radius)):
        result['diagnostics'].append('EXPLICIT_DOOR_CONNECTION_REGION_OR_DOMAIN_CLEARANCE_FAILED')
        return result
    if not obstacles.is_empty and line.distance(obstacles) + EPS < radius:
        result['diagnostics'].append('EXPLICIT_DOOR_BLOCKED_BY_PHYSICAL_WALL_OR_OBSTACLE')
        return result
    result.update(status='PASS', outside_entry_proof='PASS', gate_point_mm=gate,
                  throat_point_mm=inside, path_mm=path, distance_mm=line.length)
    return result


def _entrance(value, scope, obstacles):
    if value.door_connection is not None:
        return _explicit_entrance(value, scope, obstacles)
    result = _legacy_entrance(value, scope, obstacles)
    result.update(mode='LEGACY_INSIDE_ONLY_TECHNICAL', outside_entry_proof='MISSING_LEGACY',
                  scope_boundary_role='LOGICAL_DOMAIN_NOT_ENTITY_WALL', actual_clear_width_mm=None)
    return result


def _legacy_entrance(value, scope, obstacles):
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
    plans = _corridor_variants(value, direction, depth, compact=False)
    if value.door_connection is not None:
        # A separately named M11 alternative removes only the extra rack
        # setback previously added to a passage's already valid half-width.
        plans.extend(_corridor_variants(value, direction, depth, compact=True))
        # The wall-front variant also reserves a passage in front of wall
        # shelves perpendicular to the central runs, for the mixed-wall branch.
        plans.extend(_corridor_variants(value, direction, depth, front_loop=True))
    return plans


def _corridor_variants(value, direction, depth, compact=False, front_loop=False):
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
    end_offset = 0 if compact else clearance
    wall_setback = max(clearance, float(rules.wall_clearance_mm)) if value.door_connection is not None else clearance
    low_u, high_u = u0 + end_offset + ends / 2, u1 - end_offset - ends / 2
    if front_loop:
        low_u, high_u = u0 + wall_setback + depth + width / 2, u1 - wall_setback - depth - width / 2
    low_v = v0 + wall_setback + depth + width / 2
    high_v = v1 - wall_setback - depth - width / 2
    if low_u >= high_u or low_v >= high_v:
        return []
    corners = [xy((low_u, low_v)), xy((low_u, high_v)),
               xy((high_u, high_v)), xy((high_u, low_v))]
    work = _internal_workpoints(value)
    entry_u = uv(work['entrance'])[0]
    # On a short end, a middle connector remains a distinct test alternative.
    cross_u = entry_u if low_u + ends < entry_u < high_u - ends else (low_u + high_u) / 2
    cross = [xy((cross_u, low_v)), xy((cross_u, high_v))]
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
    identifier = 'WALL_FRONT_LOOP' if front_loop else 'COMPACT_PERIMETER_LOOP' if compact else 'PERIMETER_LOOP'
    result = [{'id': identifier, 'corridors': corridors}]
    cross_route = navigation.route(cross[0], cross[1])
    if cross_route['reachable']:
        extra = _planned_from_path(cross_route['path_mm'], width, scope, 'entry-cross-link', 'CROSS_AISLE')
        result.append({'id': identifier + '_WITH_ENTRY_CROSS_LINK', 'corridors': [*corridors, *extra]})
    return result


def _face_issues(value, layout, face):
    issues = []
    shelves = {item.id: item for item in layout.shelves}
    assemblies = {item.id: item for item in layout.assemblies}
    module = shelves.get(face.module_id)
    assembly = assemblies.get(face.assembly_id)
    line = LineString([face.start_mm, face.end_mm])
    radius = float(value.base.rules.aisle_width_mm) / 2
    nx_, ny_ = face.outward_normal
    midpoint = line.interpolate(0.5, normalized=True)
    expected = (midpoint.x + nx_ * radius, midpoint.y + ny_ * radius)
    if module is None or assembly is None:
        return ['FACE_MODULE_OR_ASSEMBLY_MISSING']
    matching_run = next((run for run in layout.runs if run.run_id == face.run_id), None)
    if (matching_run is None or face.run_id not in assembly.run_ids
            or module.id not in {item.id for item in matching_run.modules}):
        issues.append('FACE_RUN_OR_ASSEMBLY_MEMBERSHIP_INVALID')
    if (line.length <= EPS or abs(hypot(nx_, ny_) - 1) > EPS
            or abs((face.end_mm[0] - face.start_mm[0]) * nx_
                   + (face.end_mm[1] - face.start_mm[1]) * ny_) > EPS):
        issues.append('FACE_DIRECTION_INVALID')
    if hypot(expected[0] - face.standing_point_mm[0], expected[1] - face.standing_point_mm[1]) > EPS:
        issues.append('FACE_STANDING_POINT_NOT_AT_REQUIRED_HALF_AISLE')
    module_shape, assembly_shape = Polygon(module.footprint_mm), Polygon(assembly.footprint_mm)
    if (not module_shape.boundary.buffer(EPS).covers(line)
            or not assembly_shape.boundary.buffer(EPS).covers(line)):
        issues.append('FACE_NOT_ON_MODULE_AND_ASSEMBLY_EXTERIOR')
    test_point = Point(midpoint.x + nx_, midpoint.y + ny_)
    if module_shape.contains(test_point) or assembly_shape.contains(test_point):
        issues.append('FACE_NORMAL_POINTS_INTO_SHELF_OR_ASSEMBLY')
    if abs(line.length - module.length_mm) > EPS:
        issues.append('FACE_LENGTH_NOT_MODULE_PICK_LENGTH')
    return issues


def _coverage_issues(layout):
    issues = []
    module_faces = defaultdict(list)
    for face in layout.pick_faces:
        module_faces[face.module_id].append(face)
    if (set(module_faces) != {shelf.id for shelf in layout.shelves}
            or any(len(faces) != 1 for faces in module_faces.values())):
        issues.append({'code': 'COUNTED_MODULE_REQUIRES_ONE_UNIQUE_FACE'})
    if len({face.id for face in layout.pick_faces}) != len(layout.pick_faces):
        issues.append({'code': 'DUPLICATE_PICK_FACE_ID'})
    for assembly in layout.assemblies:
        if assembly.kind == 'BACK_TO_BACK':
            sides = {face.side for face in layout.pick_faces if face.assembly_id == assembly.id}
            if sides != {'A', 'B'}:
                issues.append({'code': 'BACK_TO_BACK_BOTH_SIDES_REQUIRED', 'id': assembly.id})
    return issues


def _placements(value, layout):
    minx, miny, maxx, maxy = Polygon(value.base.space.boundary.boundary_mm).bounds
    remaining = {face.id: face for face in layout.pick_faces}
    records = []
    for item in sorted(value.demand.items, key=lambda item: item.id):
        target = (minx + item.target_fraction[0] * (maxx - minx),
                  miny + item.target_fraction[1] * (maxy - miny))
        if not remaining:
            records.append({'item_id': item.id, 'target_point_mm': target, 'face_id': None,
                            'standing_point_mm': None, 'status': 'UNASSIGNED_NO_UNIQUE_FACE'})
            continue
        # Assignment never consults path distance or drops unreachable faces.
        face = min(remaining.values(), key=lambda candidate: (
            hypot((candidate.start_mm[0] + candidate.end_mm[0]) / 2 - target[0],
                  (candidate.start_mm[1] + candidate.end_mm[1]) / 2 - target[1]), candidate.id))
        del remaining[face.id]
        records.append({'item_id': item.id, 'target_point_mm': target, 'face_id': face.id,
                        'standing_point_mm': face.standing_point_mm, 'status': 'ASSIGNED'})
    return records


def _tasks(tasks, start, end, placements, navigation):
    records = []
    placement_by_id = {item['item_id']: item for item in placements}
    for task in tasks:
        route = {'id': task.id, 'item_ids': list(task.item_ids), 'visited_item_ids': [],
                 'path_mm': [_xy(start)], 'distance_mm': None, 'status': 'FAIL',
                 'leg_distances_mm': [], 'legs': [], 'route_policy': ROUTE_POLICY}
        remaining, current = set(task.item_ids), _xy(start)
        while remaining:
            candidates = []
            for item_id in sorted(remaining):
                target = placement_by_id[item_id]['standing_point_mm']
                if target is not None:
                    leg = navigation.route(current, target)
                    if leg['reachable']:
                        candidates.append((leg['distance_mm'], item_id, leg))
            if not candidates:
                break
            distance, item_id, leg = min(candidates, key=lambda item: (item[0], item[1]))
            route['path_mm'].extend(leg['path_mm'][1:])
            route['leg_distances_mm'].append(distance)
            route['legs'].append({'to_item_id': item_id, **leg})
            route['visited_item_ids'].append(item_id)
            remaining.remove(item_id)
            current = _xy(placement_by_id[item_id]['standing_point_mm'])
        final_leg = navigation.route(current, end)
        if not remaining and final_leg['reachable']:
            route['path_mm'].extend(final_leg['path_mm'][1:])
            route['leg_distances_mm'].append(final_leg['distance_mm'])
            route['legs'].append({'to_item_id': None, **final_leg})
            route.update(status='PASS', distance_mm=sum(route['leg_distances_mm']))
        else:
            route['diagnostic'] = 'FIXED_TASK_INCOMPLETE_NO_COMPARABLE_DISTANCE'
            route['unvisited_item_ids'] = sorted(remaining)
        records.append(route)
    return records


def _shared_segments(picking, replenishment):
    usage = defaultdict(lambda: {'picking': set(), 'replenishment': set()})
    for kind, routes in [('picking', picking), ('replenishment', replenishment)]:
        for route in routes:
            if route['status'] != 'PASS':
                continue
            for a, b in zip(route['path_mm'], route['path_mm'][1:]):
                a, b = _xy(a), _xy(b)
                if a != b:
                    usage[tuple(sorted((a, b)))][kind].add(route['id'])
    return [{'path_mm': list(edge), 'length_mm': _length(edge),
             'task_ids': [f'{kind}:{identifier}' for kind in ('picking', 'replenishment')
                          for identifier in sorted(tasks[kind])],
             'interpretation': 'SHARED_WALKING_SEGMENT_POSSIBLE_BLOCKING_NOT_PROVEN_CONGESTION'}
            for edge, tasks in sorted(usage.items()) if tasks['picking'] and tasks['replenishment']]


def _walkable_polygons(navigation, entrance):
    if entrance not in navigation.graph:
        return []
    component = nx.node_connected_component(navigation.graph, entrance)
    lines = [LineString([a, b]) for a, b in navigation.graph.edges if a in component and b in component]
    if not lines:
        return []
    # Display is the swept footprint of proven graph edges. Route clearance is
    # checked against exact distances, independent of polygon-buffer rendering.
    swept = unary_union(lines).buffer(navigation.radius, quad_segs=8).intersection(navigation.scope)
    if not navigation.obstacles.is_empty:
        swept = swept.difference(navigation.obstacles)
    return [_polygon_data(polygon.simplify(0.01, preserve_topology=True)) for polygon in _parts(swept)]


def _minimum_route_clear_width(value, layout, evaluation):
    _, obstacles = _geometry(value, layout)
    paths = [record.get('path_mm', []) for key in ('picking_routes', 'replenishment_routes')
             for record in evaluation.get(key, [])]
    paths += [record.get(role, {}).get('path_mm', []) for record in evaluation.get('face_access', [])
              for role in ('entrance', 'pick_start', 'receiving')]
    paths.append(evaluation.get('entrance_connection', {}).get('path_mm', []))
    segments = {tuple(sorted((_xy(a), _xy(b)))) for path in paths for a, b in zip(path, path[1:])
                if _xy(a) != _xy(b)}
    widths = []
    if segments and not obstacles.is_empty:
        lines = shapely.linestrings(np.asarray(sorted(segments), dtype=float))
        widths.append(float(2 * np.min(shapely.distance(lines, obstacles))))
    door_width = _door_link_clear_width(value, obstacles)
    if door_width is not None:
        widths.append(door_width)
    return min(widths) if widths else None


def evaluate_routes(value: OperationsInput, layout: OperationsLayout) -> dict:
    work = _internal_workpoints(value)
    points = list(work.values())
    scope, obstacles = _geometry(value, layout)
    entry = _entrance(value, scope, obstacles)
    if entry['status'] == 'PASS':
        points.append(entry['throat_point_mm'])
    points.extend(face.standing_point_mm for face in layout.pick_faces)
    navigation = _build_graph(value, layout, points)
    if entry['status'] == 'PASS':
        connection = navigation.route(entry['throat_point_mm'], work['entrance'])
        if connection['reachable']:
            entry['path_mm'] += connection['path_mm'][1:]
            entry['distance_mm'] = _length(entry['path_mm'])
        else:
            entry.update(status='FAIL', distance_mm=None)
            entry['diagnostics'].append('ENTRANCE_TO_WORKPOINT_NOT_REACHABLE_ON_GRAPH')
    faces, diagnostics = [], _coverage_issues(layout)
    coverage_ok = not diagnostics
    for face in sorted(layout.pick_faces, key=lambda face: face.id):
        issues = _face_issues(value, layout, face)
        record = {'face_id': face.id, 'module_id': face.module_id, 'assembly_id': face.assembly_id,
                  'side': face.side, 'standing_point_mm': face.standing_point_mm,
                  'geometry_issues': issues}
        for role in ('entrance', 'pick_start', 'receiving'):
            connection = navigation.route(work[role], face.standing_point_mm)
            if role == 'entrance':
                if entry['status'] != 'PASS':
                    connection = {'reachable': False, 'distance_mm': None, 'path_mm': []}
                elif connection['reachable']:
                    connection = {'reachable': True,
                                  'distance_mm': entry['distance_mm'] + connection['distance_mm'],
                                  'path_mm': entry['path_mm'] + connection['path_mm'][1:]}
            record[role] = connection
            prefix = 'entry' if role == 'entrance' else role
            record[f'{prefix}_distance_mm'] = connection['distance_mm']
            record[f'{prefix}_path_mm'] = connection['path_mm']
        record['reachable'] = not issues and all(record[role]['reachable']
                                                for role in ('entrance', 'pick_start', 'receiving'))
        record['status'] = 'PASS' if record['reachable'] else 'FAIL'
        faces.append(record)
        if not record['reachable']:
            diagnostics.append({'code': 'FACE_REACHABILITY_NOT_PROVEN', 'face_id': face.id,
                                'issues': issues,
                                'interpretation': 'Candidate has no valid path witness; not a global impossibility claim'})
    placements = _placements(value, layout)
    picking = _tasks(value.demand.picking_tasks, work['pick_start'], work['packing'], placements, navigation)
    replenishment = _tasks(value.demand.replenishment_tasks, work['receiving'], work['receiving'], placements, navigation)
    all_faces = bool(faces) and all(face['reachable'] for face in faces)
    workpoints_connected = (entry['status'] == 'PASS'
                           and all(navigation.route(work['entrance'], point)['reachable'] for point in work.values()))
    complete = (all_faces and coverage_ok and workpoints_connected and entry['status'] == 'PASS'
                and all(route['status'] == 'PASS' for route in [*picking, *replenishment])
                and all(item['status'] == 'ASSIGNED' for item in placements))
    def task_metric(routes, function):
        return float(function(route['distance_mm'] for route in routes)) if complete and routes else None
    entry_distances = [face['entry_distance_mm'] for face in faces if face['entry_distance_mm'] is not None]
    summary = {'reachable_face_count': sum(face['reachable'] for face in faces),
               'total_face_count': len(faces),
               'picking_mean_mm': task_metric(picking, mean),
               'picking_worst_mm': task_metric(picking, max),
               'replenishment_mean_mm': task_metric(replenishment, mean),
               'replenishment_worst_mm': task_metric(replenishment, max),
               'entry_to_face_mean_mm': mean(entry_distances) if all_faces else None,
               'complete_fixed_tasks': complete}
    farthest = sorted((face for face in faces if face['entry_distance_mm'] is not None),
                      key=lambda face: (-face['entry_distance_mm'], face['face_id']))[:3]
    summary['farthest_faces'] = [{'face_id': face['face_id'], 'distance_mm': face['entry_distance_mm']}
                                 for face in farthest]
    detours = []
    for face in faces:
        if face['pick_start_distance_mm'] is None:
            continue
        straight = hypot(face['standing_point_mm'][0] - work['pick_start'][0],
                         face['standing_point_mm'][1] - work['pick_start'][1])
        extra = face['pick_start_distance_mm'] - straight
        detours.append({'face_id': face['face_id'], 'route_distance_mm': face['pick_start_distance_mm'],
                        'euclidean_lower_bound_mm': straight, 'extra_distance_mm': extra,
                        'detour_ratio': face['pick_start_distance_mm'] / straight if straight > EPS else None})
    summary['largest_detours'] = sorted(detours, key=lambda item: (-item['extra_distance_mm'], item['face_id']))[:3]
    diagnostics += [{'code': 'FAR_FACE', **item} for item in summary['farthest_faces']]
    diagnostics += [{'code': 'DETOUR_VS_EUCLIDEAN_LOWER_BOUND', **item} for item in summary['largest_detours']]
    diagnostics += [{'code': message} for message in entry['diagnostics']]
    if navigation.error:
        diagnostics.append({'code': navigation.error})
    result = {'status': 'PASS' if complete else 'FAIL', 'face_access': faces,
            'item_placements': placements, 'picking_routes': picking,
            'replenishment_routes': replenishment, 'summary': summary,
            'entrance_connection': entry, 'shared_segments': _shared_segments(picking, replenishment),
            'diagnostics': diagnostics, 'walkable_polygons': _walkable_polygons(navigation, work['entrance']),
            'walkable_region_basis': 'REQUIRED_WIDTH_SWEEP_OF_ENTRANCE_CONNECTED_VALID_GRAPH',
            'display_geometry_use': 'VISUALIZATION_OF_PROVEN_CENTRE_LINES_NOT_EXTRA_PATHS',
            'diagnostic_data_status': 'DERIVED_ROUTE_DIAGNOSTICS_NOT_TRAFFIC_PREDICTION',
            'evaluation_basis': 'SYNTHETIC_FIXED_TASKS_NO_SALES_OR_CAPACITY_INFERENCE',
            'route_policy': ROUTE_POLICY, 'graph_policy': GRAPH_POLICY,
            'graph': {'node_count': navigation.graph.number_of_nodes(),
                      'edge_count': navigation.graph.number_of_edges(),
                      'grid_point_count': navigation.grid_point_count,
                      'weight_unit': 'mm', 'edge_weight': 'length_mm',
                      'completeness': 'FINITE_RECTILINEAR_PATH_WITNESS_NOT_GLOBAL_OPTIMUM'},
            'outside_entry_proof': entry['outside_entry_proof'],
            'full_operations_proven': bool(complete and entry['outside_entry_proof'] == 'PASS'),
            'operational_completeness': ('COMPLETE_SYNTHETIC_OUTSIDE_ENTRY_AND_TASKS' if complete and entry['outside_entry_proof'] == 'PASS'
                                        else 'LEGACY_TECHNICAL_ONLY_OUTSIDE_ENTRY_MISSING' if value.door_connection is None
                                        else 'OUTSIDE_ENTRY_OR_TASK_EVIDENCE_INCOMPLETE')}
    result['minimum_route_clear_width_mm'] = _minimum_route_clear_width(value, layout, result) if complete else None
    summary['minimum_route_clear_width_mm'] = result['minimum_route_clear_width_mm']
    return result


def validate_route_evidence(value: OperationsInput, layout: OperationsLayout, evaluation: dict) -> dict:
    """Independently check JSON path witnesses and all fixed demand/face coverage."""
    scope, obstacles = _geometry(value, layout)
    radius = float(value.base.rules.aisle_width_mm) / 2
    issues, checked_segments = [], 0
    entry = _entrance(value, scope, obstacles)
    def require(condition, code, identifier=''):
        if not condition:
            issues.append({'code': code, 'id': identifier})
    def same_json(a, b):
        return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    require(evaluation.get('status') == 'PASS', 'EVALUATION_NOT_PASS_CANNOT_BE_SEALED')
    require(evaluation.get('summary', {}).get('complete_fixed_tasks') is True,
            'FIXED_TASK_COMPLETION_SUMMARY_NOT_TRUE')
    require(evaluation.get('route_policy') == ROUTE_POLICY and evaluation.get('graph_policy') == GRAPH_POLICY,
            'ROUTE_OR_GRAPH_POLICY_MISMATCH')
    def check_path(path, declared, identifier, start=None, end=None, permit_entry=False):
        nonlocal checked_segments
        require(bool(path), 'PATH_MISSING', identifier)
        if not path:
            return
        require(all(len(point) == 2 and all(isfinite(c) for c in point) for point in path),
                'PATH_NONFINITE', identifier)
        if start is not None:
            require(hypot(path[0][0] - start[0], path[0][1] - start[1]) <= EPS,
                    'PATH_START_MISMATCH', identifier)
        if end is not None:
            require(hypot(path[-1][0] - end[0], path[-1][1] - end[1]) <= EPS,
                    'PATH_END_MISMATCH', identifier)
        require(declared is not None and abs(_length(path) - declared) <= EPS,
                'PATH_DISTANCE_NOT_ACTUAL_LENGTH', identifier)
        for index, (a, b) in enumerate(zip(path, path[1:])):
            if _xy(a) == _xy(b):
                continue
            checked_segments += 1
            line = LineString([a, b])
            explicit_prefix = (permit_entry and entry['status'] == 'PASS' and index < len(entry['path_mm']) - 1
                               and _xy(a) == _xy(entry['path_mm'][index]) and _xy(b) == _xy(entry['path_mm'][index + 1]))
            # Only this exact independently revalidated local link can leave
            # the logical scope; no exterior graph nodes or snapped points.
            clear = True if explicit_prefix else _clear(line, scope, obstacles, radius)
            require(clear, 'PATH_CLEARANCE_OR_OBSTACLE_VIOLATION', identifier)
            require(abs(a[0] - b[0]) <= EPS or abs(a[1] - b[1]) <= EPS,
                    'PATH_NOT_RECTILINEAR_GRAPH_EDGE', identifier)
        if len(path) == 1:
            require(_clear(Point(path[0]), scope, obstacles, radius), 'STATIONARY_POINT_CLEARANCE_VIOLATION', identifier)
    face_by_id = {face.id: face for face in layout.pick_faces}
    actual_ids = [face['face_id'] for face in evaluation.get('face_access', [])]
    require(len(actual_ids) == len(set(actual_ids)) and set(actual_ids) == set(face_by_id),
            'FACE_EVIDENCE_COVERAGE_MISMATCH')
    issues.extend(_coverage_issues(layout))
    for face in layout.pick_faces:
        for issue in _face_issues(value, layout, face):
            require(False, issue, face.id)
    if entry['status'] != 'PASS':
        require(False, 'ENTRANCE_OPENING_ACCESS_INVALID')
    connection = evaluation.get('entrance_connection', {})
    require(connection.get('status') == 'PASS', 'ENTRANCE_CONNECTION_NOT_PROVEN')
    if entry['status'] == 'PASS' and connection.get('status') == 'PASS':
        for key in ('opening_width_mm', 'required_width_mm', 'gate_point_mm', 'throat_point_mm'):
            require(same_json(connection.get(key), entry.get(key)), 'ENTRANCE_METADATA_MISMATCH', key)
        check_path(connection.get('path_mm', []), connection.get('distance_mm'), 'entrance-connection',
                   _entry_origin(entry), _internal_workpoints(value)['entrance'], True)
        for key in ('mode', 'outside_entry_proof', 'outside_point_mm', 'inside_point_mm', 'connection_region', 'actual_clear_width_mm'):
            require(same_json(connection.get(key), entry.get(key)), 'DOOR_CONNECTION_METADATA_MISMATCH', key)
    for evidence in evaluation.get('face_access', []):
        face = face_by_id.get(evidence['face_id'])
        if face is None:
            continue
        require(evidence.get('reachable') is True, 'FACE_NOT_REACHABLE_FROM_ALL_WORK_ROLES', face.id)
        for key in ('module_id', 'assembly_id', 'side', 'standing_point_mm'):
            require(same_json(evidence.get(key), getattr(face, key)), 'FACE_EVIDENCE_IDENTITY_MISMATCH', f'{face.id}:{key}')
        require(evidence.get('status') == 'PASS' and evidence.get('geometry_issues') == _face_issues(value, layout, face),
                'FACE_EVIDENCE_STATUS_MISMATCH', face.id)
        for role in ('entrance', 'pick_start', 'receiving'):
            path = evidence.get(role, {})
            prefix = 'entry' if role == 'entrance' else role
            require(evidence.get(f'{prefix}_distance_mm') == path.get('distance_mm')
                    and json.dumps(evidence.get(f'{prefix}_path_mm')) == json.dumps(path.get('path_mm')),
                    'FACE_DISPLAY_EVIDENCE_MISMATCH', f'{face.id}:{role}')
            require(path.get('reachable') is True, 'ROLE_FACE_PATH_NOT_PROVEN', f'{face.id}:{role}')
            start = _entry_origin(entry) if role == 'entrance' else getattr(value.workpoints, role).point_mm
            if path.get('reachable'):
                check_path(path.get('path_mm', []), path.get('distance_mm'), f'{face.id}:{role}',
                           start, face.standing_point_mm, role == 'entrance')
    expected_placements = _placements(value, layout)
    placements = evaluation.get('item_placements', [])
    require(json.dumps(placements, sort_keys=True) == json.dumps(expected_placements, sort_keys=True),
            'FIXED_SPATIAL_PLACEMENT_POLICY_MISMATCH')
    placed = {item['item_id']: item for item in placements}
    require(all(item['status'] == 'ASSIGNED' for item in placements), 'FIXED_DEMAND_HAS_UNASSIGNED_ITEMS')
    work = _internal_workpoints(value)
    navigation_points = list(work.values())
    navigation_points += [face.standing_point_mm for face in layout.pick_faces]
    if entry['status'] == 'PASS':
        navigation_points.append(entry['throat_point_mm'])
    navigation = _build_graph(value, layout, navigation_points)
    graph_evidence = evaluation.get('graph', {})
    for key, expected in [('node_count', navigation.graph.number_of_nodes()),
                          ('edge_count', navigation.graph.number_of_edges()),
                          ('grid_point_count', navigation.grid_point_count),
                          ('weight_unit', 'mm'), ('edge_weight', 'length_mm'),
                          ('completeness', 'FINITE_RECTILINEAR_PATH_WITNESS_NOT_GLOBAL_OPTIMUM')]:
        require(graph_evidence.get(key) == expected, 'GRAPH_EVIDENCE_METADATA_MISMATCH', key)
    connected_workpoints = 0
    for role, point in work.items():
        connected = entry['status'] == 'PASS' and navigation.route(work['entrance'], point)['reachable']
        connected_workpoints += int(connected)
        require(connected, 'WORKPOINT_NOT_CONNECTED_TO_ENTRANCE', role)
    if entry['status'] == 'PASS' and connection.get('status') == 'PASS':
        internal = navigation.route(entry['throat_point_mm'], work['entrance'])
        require(internal['reachable'] and connection.get('distance_mm') is not None
                and abs(connection['distance_mm'] - entry['distance_mm'] - internal['distance_mm']) <= EPS,
                'ENTRANCE_CONNECTION_NOT_WEIGHTED_SHORTEST_PATH')
    for key, tasks, start, end in [
            ('picking_routes', value.demand.picking_tasks, value.workpoints.pick_start.point_mm, value.workpoints.packing.point_mm),
            ('replenishment_routes', value.demand.replenishment_tasks, value.workpoints.receiving.point_mm, value.workpoints.receiving.point_mm)]:
        records = evaluation.get(key, [])
        require([record['id'] for record in records] == [task.id for task in tasks], 'FIXED_TASK_COVERAGE_MISMATCH', key)
        by_id = {task.id: task for task in tasks}
        for record in records:
            task = by_id.get(record['id'])
            if task is None:
                continue
            identifier = f'{key}:{task.id}'
            require(record.get('status') == 'PASS', 'FIXED_TASK_INCOMPLETE', identifier)
            require(record.get('route_policy') == ROUTE_POLICY, 'TASK_ROUTE_POLICY_MISMATCH', identifier)
            visited = record.get('visited_item_ids', [])
            require(record.get('item_ids') == task.item_ids and len(visited) == len(task.item_ids)
                    and set(visited) == set(task.item_ids), 'TASK_ITEM_VISIT_COVERAGE_MISMATCH', identifier)
            if record.get('status') == 'PASS':
                check_path(record.get('path_mm', []), record.get('distance_mm'), identifier, start, end)
                require(abs(sum(record.get('leg_distances_mm', [])) - record['distance_mm']) <= EPS,
                        'TASK_LEG_DISTANCE_SUM_MISMATCH', identifier)
                for item_id in task.item_ids:
                    standing = placed.get(item_id, {}).get('standing_point_mm')
                    require(standing is not None and any(_xy(point) == _xy(standing) for point in record.get('path_mm', [])),
                            'TASK_DID_NOT_VISIT_ASSIGNED_STANDING_POINT', f'{identifier}:{item_id}')
                # Re-evaluate nearest-unvisited choices on actual weighted paths;
                # a relabelled visit order or a changed route policy cannot pass.
                current, remaining = _xy(start), set(task.item_ids)
                expected_visited, expected_lengths = [], []
                while remaining:
                    distances, _ = navigation.paths_from(current)
                    candidates = [(distances[_xy(placed[item_id]['standing_point_mm'])], item_id)
                                  for item_id in sorted(remaining)
                                  if placed.get(item_id, {}).get('standing_point_mm') is not None
                                  and _xy(placed[item_id]['standing_point_mm']) in distances]
                    if not candidates:
                        break
                    distance, item_id = min(candidates)
                    expected_lengths.append(distance)
                    expected_visited.append(item_id)
                    remaining.remove(item_id)
                    current = _xy(placed[item_id]['standing_point_mm'])
                final_leg = navigation.route(current, end)
                if not remaining and final_leg['reachable']:
                    expected_lengths.append(final_leg['distance_mm'])
                require(visited == expected_visited and not remaining and final_leg['reachable'],
                        'TASK_DETERMINISTIC_VISIT_POLICY_MISMATCH', identifier)
                lengths = record.get('leg_distances_mm', [])
                require(len(lengths) == len(expected_lengths)
                        and all(abs(a - b) <= EPS for a, b in zip(lengths, expected_lengths)),
                        'TASK_LEG_NOT_WEIGHTED_SHORTEST_PATH', identifier)
                legs = record.get('legs', [])
                leg_path = list(legs[0].get('path_mm', [])) if legs else []
                for leg in legs[1:]:
                    leg_path.extend(leg.get('path_mm', [])[1:])
                require(json.dumps(leg_path) == json.dumps(record.get('path_mm', []))
                        and [leg.get('to_item_id') for leg in legs] == [*visited, None],
                        'TASK_ROUTE_LEG_WITNESS_MISMATCH', identifier)
                previous = start
                for index, leg in enumerate(legs):
                    target = (placed.get(visited[index], {}).get('standing_point_mm')
                              if index < len(visited) else end)
                    require(leg.get('reachable') is True, 'TASK_LEG_NOT_REACHABLE', identifier)
                    check_path(leg.get('path_mm', []), leg.get('distance_mm'),
                               f'{identifier}:leg-{index}', previous, target)
                    require(index < len(lengths) and leg.get('distance_mm') is not None
                            and abs(leg['distance_mm'] - lengths[index]) <= EPS,
                            'TASK_DECLARED_AND_WITNESSED_LEG_DISTANCE_MISMATCH', identifier)
                    if target is not None:
                        previous = target
    summary = evaluation.get('summary', {})
    require(summary.get('total_face_count') == len(layout.pick_faces)
            and summary.get('reachable_face_count') == sum(face.get('reachable') is True for face in evaluation.get('face_access', [])),
            'REACHABLE_FACE_COUNT_MISMATCH')
    if evaluation.get('status') != 'PASS':
        require(all(summary.get(key) is None for key in ('picking_mean_mm', 'picking_worst_mm',
                'replenishment_mean_mm', 'replenishment_worst_mm')), 'INCOMPLETE_CANDIDATE_MUST_NOT_REPORT_COMPARABLE_TASK_MEAN')
    else:
        for key, prefix in [('picking_routes', 'picking'), ('replenishment_routes', 'replenishment')]:
            distances = [record.get('distance_mm') for record in evaluation.get(key, [])]
            if distances and all(distance is not None for distance in distances):
                for suffix, function in [('mean_mm', mean), ('worst_mm', max)]:
                    recorded = summary.get(f'{prefix}_{suffix}')
                    require(recorded is not None and abs(recorded - function(distances)) <= EPS,
                            'FIXED_TASK_SUMMARY_MISMATCH', f'{prefix}_{suffix}')
        face_records = evaluation.get('face_access', [])
        entrance_distances = [record.get('entrance', {}).get('distance_mm') for record in face_records]
        if entrance_distances and all(distance is not None for distance in entrance_distances):
            recorded = summary.get('entry_to_face_mean_mm')
            require(recorded is not None and abs(recorded - mean(entrance_distances)) <= EPS,
                    'ENTRY_DISTANCE_SUMMARY_MISMATCH')
        else:
            require(False, 'ENTRY_DISTANCE_SUMMARY_INCOMPLETE')
        farthest = sorted((record for record in face_records if record.get('entry_distance_mm') is not None),
                          key=lambda record: (-record['entry_distance_mm'], record['face_id']))[:3]
        expected_far = [{'face_id': record['face_id'], 'distance_mm': record['entry_distance_mm']}
                        for record in farthest]
        expected_detours = []
        for record in face_records:
            if record.get('pick_start_distance_mm') is None or record['face_id'] not in face_by_id:
                continue
            point = face_by_id[record['face_id']].standing_point_mm
            straight = hypot(point[0] - work['pick_start'][0], point[1] - work['pick_start'][1])
            distance = record['pick_start_distance_mm']
            expected_detours.append({'face_id': record['face_id'], 'route_distance_mm': distance,
                                    'euclidean_lower_bound_mm': straight, 'extra_distance_mm': distance - straight,
                                    'detour_ratio': distance / straight if straight > EPS else None})
        expected_detours = sorted(expected_detours, key=lambda item: (-item['extra_distance_mm'], item['face_id']))[:3]
        require(same_json(summary.get('farthest_faces'), expected_far)
                and same_json(summary.get('largest_detours'), expected_detours), 'ROUTE_DIAGNOSTIC_SUMMARY_MISMATCH')
        expected_diagnostics = ([{'code': 'FAR_FACE', **item} for item in expected_far]
                                + [{'code': 'DETOUR_VS_EUCLIDEAN_LOWER_BOUND', **item} for item in expected_detours])
        require(same_json(evaluation.get('diagnostics'), expected_diagnostics), 'ROUTE_DIAGNOSTIC_DATA_MISMATCH')
    require(same_json(evaluation.get('shared_segments'), _shared_segments(
        evaluation.get('picking_routes', []), evaluation.get('replenishment_routes', []))),
        'SHARED_SEGMENT_DATA_MISMATCH')
    require(same_json(evaluation.get('walkable_polygons'), _walkable_polygons(navigation, work['entrance'])),
            'DISPLAY_WALKABLE_REGION_MISMATCH')
    require(connection.get('diagnostics') == entry.get('diagnostics'), 'ENTRANCE_DIAGNOSTIC_MISMATCH')
    require(evaluation.get('outside_entry_proof') == entry['outside_entry_proof'], 'OUTSIDE_ENTRY_PROOF_STATUS_MISMATCH')
    require(evaluation.get('full_operations_proven') is bool(evaluation.get('status') == 'PASS'
            and entry['outside_entry_proof'] == 'PASS'), 'FULL_OPERATIONS_PROOF_STATUS_MISMATCH')
    width = _minimum_route_clear_width(value, layout, evaluation) if evaluation.get('status') == 'PASS' else None
    for claimed in (evaluation.get('minimum_route_clear_width_mm'), summary.get('minimum_route_clear_width_mm')):
        require((width is None and claimed is None) or (width is not None and claimed is not None and abs(width - claimed) <= EPS),
                'ACTUAL_ROUTE_CLEAR_WIDTH_MISMATCH')
    return {'status': 'PASS' if not issues else 'FAIL', 'issues': issues,
            'checked_face_count': len(layout.pick_faces), 'checked_path_segment_count': checked_segments,
            'entrance_connected_workpoint_count': connected_workpoints,
            'outside_entry_proof': entry['outside_entry_proof'],
            'full_operations_proven': not issues and entry['outside_entry_proof'] == 'PASS',
            'minimum_route_clear_width_mm': width,
            'display_and_shared_segment_data': 'RECOMPUTED_FROM_VALIDATED_GRAPH_AND_TASK_PATHS',
            'required_path_width_mm': radius * 2,
            'distance_basis': 'INDEPENDENT_POLYLINE_LENGTH_AND_EXACT_SEGMENT_CLEARANCE'}
