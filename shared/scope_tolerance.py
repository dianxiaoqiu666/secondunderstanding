"""Bounded format normalization of an explicitly adopted human scope only.

The input is a logical planning ring, never a physical wall or a rough frame.
No source file is written, no region is selected by size, and no hole is filled.
"""
from collections import Counter
from dataclasses import asdict, dataclass
import math

from shapely import make_valid, node
from shapely.geometry import LineString, MultiLineString, Point, Polygon
from shapely.ops import unary_union
from shapely.strtree import STRtree


POLICY = 'M11-adopted-human-scope-format-v1'
ADOPTED_SCOPE_LAYERS = frozenset({'HUMAN_PLANNING_SCOPE', 'PLANNING_SCOPE'})
EPS = 1e-7


@dataclass(frozen=True)
class ScopeTolerance:
    vertex_shift_mm: float = 1.0
    maximum_area_change_ratio: float = 0.001


TOLERANCE = ScopeTolerance()


class ScopeFormatError(ValueError):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


def _fail(code, message):
    raise ScopeFormatError(code, message)


def _point(value):
    if (len(value) != 2 or any(isinstance(v, bool) or not isinstance(v, (int, float))
                             or not math.isfinite(v) for v in value)):
        _fail('INVALID_SCOPE_COORDINATE', '人工范围只接受有限二维毫米坐标。')
    return tuple(float(v) for v in value)


def _parts(geometry):
    if geometry.is_empty:
        return []
    if hasattr(geometry, 'geoms'):
        return [part for child in geometry.geoms for part in _parts(child)]
    return [geometry]


def _single_polygon(geometry):
    parts = _parts(geometry)
    polygons = [part for part in parts if part.geom_type == 'Polygon' and part.area > 0]
    if len(polygons) != 1:
        _fail('SCOPE_MULTIPLE_OR_DEGENERATE_REGIONS',
              '人工范围不能解释为唯一有效区域；保留全部分区，不取最大块。')
    return polygons[0], [part for part in parts if part.geom_type != 'Polygon']


def _reference_geometry(geometry):
    parts = _parts(geometry)
    polygons = [part for part in parts if part.geom_type == 'Polygon' and part.area > 0]
    substantial = [part for part in polygons if part.area > TOLERANCE.vertex_shift_mm ** 2]
    if len(substantial) != 1:
        _fail('SCOPE_MULTIPLE_OR_DEGENERATE_REGIONS',
              '人工范围含多个实质区域或已退化；保留全部分区，不取最大块。')
    # Keep every area in the comparison reference. The result is reconstructed
    # from the entire original ring, never chosen from this polygon list.
    return (unary_union(polygons), [part for part in parts if part.geom_type != 'Polygon'],
            [Polygon(ring) for polygon in polygons for ring in polygon.interiors], len(polygons))


def _segments(coords):
    return [LineString([a, b]) for a, b in zip(coords, coords[1:]) if a != b]


def _deduplicate_and_snap(coords):
    """Move only to a nearby source vertex or non-adjacent source segment.

    Every displacement is checked against its own original point, preventing
    iterative tolerance growth. Intersections are subsequently noded by GEOS.
    """
    original = coords[:-1] if coords[0] == coords[-1] else coords[:]
    points = [Point(p) for p in original]
    tree = STRtree(points)
    anchored = []
    for index, point in enumerate(points):
        near = sorted(int(j) for j in tree.query(point.buffer(TOLERANCE.vertex_shift_mm)))
        choices = [j for j in near if j < index and anchored[j] == original[j]
                   and math.dist(original[index], original[j]) <= TOLERANCE.vertex_shift_mm]
        anchored.append(original[min(choices)] if choices else original[index])
    source_ring = [*original, original[0]]
    source_lines = _segments(source_ring)
    line_tree = STRtree(source_lines)
    snapped = []
    fixed_anchors = {current for raw, current in zip(original, anchored) if raw != current}
    for raw, current in zip(original, anchored):
        if current in fixed_anchors:
            snapped.append(current)
            continue
        proposals = []
        for raw_index in line_tree.query(Point(current).buffer(TOLERANCE.vertex_shift_mm)):
            line = source_lines[int(raw_index)]
            if raw in (tuple(line.coords[0]), tuple(line.coords[-1])):
                continue
            projected = tuple(line.interpolate(line.project(Point(current))).coords[0])
            distance = math.dist(current, projected)
            if (EPS < distance <= TOLERANCE.vertex_shift_mm
                    and math.dist(raw, projected) <= TOLERANCE.vertex_shift_mm):
                proposals.append((distance, projected))
        snapped.append(min(proposals)[1] if proposals else current)
    clean = []
    for point in snapped:
        if not clean or point != clean[-1]:
            clean.append(point)
    if len(clean) > 1 and clean[-1] == clean[0]:
        clean.pop()
    if len(set(clean)) < 3:
        _fail('SCOPE_DEGENERATE_AFTER_NORMALIZATION', '有限格式整理后不足三个不同顶点。')
    return [*clean, clean[0]], max(math.dist(a, b) for a, b in zip(original, snapped)), len(original) - len(clean)


def _duplicated_linework(lines):
    tree = STRtree(lines)
    overlaps = []
    for index, line in enumerate(lines):
        for raw_index in tree.query(line, predicate='intersects'):
            other = int(raw_index)
            if other > index:
                intersection = line.intersection(lines[other])
                overlaps.extend(part for part in _parts(intersection) if part.geom_type == 'LineString' and part.length > 0)
    return unary_union(overlaps)


def normalize_adopted_scope(vertices, *, layer, source_closed, holes=(), source_type='LWPOLYLINE'):
    """Return one polygon and an auditable, bounded in-memory normalization.

    Admission is semantic: callers must first establish one adopted explicit
    scope. Naming this function must never grant an unknown frame that status.
    """
    if layer.upper() not in ADOPTED_SCOPE_LAYERS:
        _fail('SCOPE_NOT_EXPLICITLY_ADOPTED', '只有明确采用的人工规划范围图层可做格式容错。')
    coords = [_point(p) for p in vertices]
    if len(set(coords)) < 3:
        _fail('INVALID_SCOPE_VERTEX_COUNT', '人工范围至少需要三个不同顶点。')
    hole_rings = [[_point(p) for p in ring] for ring in holes]
    ring = coords if coords[0] == coords[-1] else [*coords, coords[0]]
    raw = Polygon(ring, hole_rings)
    if not math.isfinite(raw.area):
        _fail('INVALID_SCOPE_AREA', '人工范围面积不是有限值。')
    # Even-odd linework interpretation is the reference, including every hole.
    # Multiple substantial regions are rejected before snapping can merge them.
    # Sub-mm slivers remain in the area/boundary comparison, never discarded by
    # selecting the largest polygon.
    reference, reference_collapsed, reference_holes, region_count = _reference_geometry(make_valid(raw))
    clean, displacement, removed = _deduplicate_and_snap(ring)
    clean_input = Polygon(clean, hole_rings)
    result, collapsed = _single_polygon(make_valid(clean_input))
    if result.is_empty or not result.is_valid or result.area <= 0:
        _fail('INVALID_NORMALIZED_SCOPE', '人工范围整理后仍不是有效区域。')
    if len(reference_holes) != len(result.interiors):
        _fail('SCOPE_HOLE_CHANGED', '格式整理会改变孔洞数量，已拒绝；不能填孔。')
    result_holes = [Polygon(r) for r in result.interiors]
    if any(not any(hole.equals(other) for other in result_holes) for hole in reference_holes):
        _fail('SCOPE_HOLE_CHANGED', '格式整理会改变孔洞形状，已拒绝；孔洞必须原样保留。')
    difference = reference.symmetric_difference(result).area
    area_limit = min(reference.area * TOLERANCE.maximum_area_change_ratio,
                     reference.length * TOLERANCE.vertex_shift_mm)
    boundary_shift = reference.boundary.hausdorff_distance(result.boundary)
    if (displacement > TOLERANCE.vertex_shift_mm + EPS
            or boundary_shift > TOLERANCE.vertex_shift_mm + EPS or difference > area_limit + EPS):
        _fail('SCOPE_REPAIR_TOO_LARGE', '修复超过集中设置的小容差或面积变化上限；需要核对人工范围。')
    original_lines = _segments(ring)
    duplicate_lines = _duplicated_linework(original_lines)
    permitted_collapsed = unary_union([duplicate_lines, result.boundary.buffer(TOLERANCE.vertex_shift_mm)])
    if any(part.difference(permitted_collapsed).length > EPS for part in [*reference_collapsed, *collapsed]):
        _fail('SCOPE_COLLAPSED_GEOMETRY_UNRESOLVED', '修复会丢弃不能解释为重复线或局部误差的线段。')
    noded = node(MultiLineString(_segments(clean)))
    raw_counts = Counter(tuple(sorted((tuple(line.coords[0]), tuple(line.coords[-1])))) for line in original_lines)
    duplicate_count = sum(count - 1 for count in raw_counts.values())
    operations = []
    if not source_closed:
        operations.append('logical_ring_closure' if coords[0] != coords[-1] else 'closed_flag_normalization')
    if removed:
        operations.append('remove_duplicate_points')
    if displacement > EPS:
        operations.append('local_snap_within_mm_tolerance')
    if duplicate_count or len(_parts(noded)) != len(_segments(clean)):
        operations.append('node_intersections_and_duplicate_segments')
    if not raw.is_valid or not clean_input.is_valid:
        operations.append('make_valid_linework_preserve_collapsed_audit')
    result = result.normalize()
    audit = {'policy': POLICY, 'layer': layer, 'source_type': source_type,
             'source_closed_flag': bool(source_closed), 'raw_vertices_mm': [list(p) for p in coords],
             'raw_repeated_first_vertex': coords[0] == coords[-1], 'raw_polygon_valid': raw.is_valid,
             'logical_closing_segment_mm': None if coords[0] == coords[-1] else [list(coords[-1]), list(coords[0])],
             'logical_closure_is_physical_wall': False, 'tolerance': asdict(TOLERANCE),
             'operations': operations, 'removed_duplicate_point_count': removed,
             'duplicate_segment_count': duplicate_count, 'maximum_vertex_shift_mm': displacement,
             'boundary_hausdorff_distance_mm': boundary_shift, 'symmetric_difference_area_mm2': difference,
             'maximum_area_change_mm2': area_limit, 'hole_count': len(result.interiors),
             'reference_region_count': region_count, 'substantial_region_count': 1,
             'substantial_region_area_threshold_mm2': TOLERANCE.vertex_shift_mm ** 2,
             'area_mm2': result.area, 'final_valid': result.is_valid,
             'collapsed_lines_mm': [[list(p) for p in part.coords] for part in collapsed if part.geom_type == 'LineString'],
             'derived_noded_segments_mm': [[list(p) for p in part.coords] for part in _parts(noded)],
             'source_cad_modified': False}
    return result, audit
