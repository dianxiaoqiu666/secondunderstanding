"""Deterministic whole-run planning over standard, confirmed geometry.

The only inputs are the versioned request and its material catalog. No I/O is
performed here. GEOS supplies all geometric predicates and polygon operations.
"""
from collections import Counter
from decimal import Decimal
from functools import reduce
import hashlib
import json
import math

from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

from shared.contracts import Shelf, Template
from shared.planning_v2 import BOMRowV2, LayoutV2, PlanRequest, ShelfRun, space_geometry_digest
from services.planning.engine import PlanningError

EPS = 1e-6
MAX_DP_STATES = 200000
MAX_MODULES = 10000


def _fail(code, message):
    raise PlanningError(code, message)


def _catalog(templates):
    items = sorted((Template.model_validate(t) if isinstance(t, dict) else t for t in templates),
                   key=lambda t: (t.material_id, t.length_mm, t.depth_mm))
    if not items or len({t.material_id for t in items}) != len(items):
        _fail('INVALID_BUSINESS', '物料模板为空或 material_id 重复。')
    return items


def combine_modules(available_length, templates, min_modules=2):
    """Exact unbounded, one-dimensional module DP; tail, count, then stable IDs.

    This is project-specific length composition, not a general optimization
    solver. Decimal material lengths determine an exact GCD lattice. Fractional
    tail remains physical free space and is never rounded up into an extra rack.
    """
    if not math.isfinite(available_length) or available_length <= 0 or not isinstance(min_modules,int) or min_modules < 2:
        _fail('INVALID_RUN', '可用长度必须为正数，每排至少两个模块。')
    if min_modules > MAX_MODULES:
        _fail('PLANNING_LIMIT', '每排最小模块数超过 10000。')
    items = _catalog(templates)
    if len({t.depth_mm for t in items}) != 1:
        _fail('INCOMPATIBLE_DEPTH', '同排模块必须使用统一深度。')
    decimals = [Decimal(str(t.length_mm)) for t in items]
    precision = max(max(0, -d.normalize().as_tuple().exponent) for d in decimals)
    if precision > 6:
        _fail('PLANNING_LIMIT', '物料长度精度超过 0.000001 mm。')
    scale = 10 ** precision
    lengths = [int(d * scale) for d in decimals]
    divisor = reduce(math.gcd, lengths)
    steps = [n // divisor for n in lengths]
    capacity = int(Decimal(str(available_length)) * scale // divisor)
    if capacity * (min_modules + 1) > MAX_DP_STATES:
        _fail('PLANNING_LIMIT', '模块组合状态超过 200000，请核对单位或物料精度。')
    # State keeps the fewest modules and lexicographically smallest ID sequence.
    # A separate saturated count bucket preserves valid >=min_modules choices
    # when a single long template competes with two or more short templates.
    states = {(0, 0): ()}
    for used in range(capacity + 1):
        for bucket in range(min_modules + 1):
            seq = states.get((used, bucket))
            if seq is None:
                continue
            for index, length in enumerate(steps):
                target = used + length
                if target > capacity:
                    continue
                candidate = tuple(sorted((*seq, index)))
                key = (target, min(len(candidate), min_modules))
                current = states.get(key)
                if current is None or (len(candidate), candidate) < (len(current), current):
                    states[key] = candidate
    options = [(used, seq) for (used, bucket), seq in states.items() if bucket == min_modules]
    if not options:
        return {'material_ids': [], 'module_count': 0, 'used_length_mm': 0.0,
                'remaining_length_mm': float(available_length), 'counts': {},
                'reason': 'SHORT_FRAGMENT_BELOW_MIN_MODULES', 'objective': ['minimum_tail', 'minimum_module_count', 'material_id_ascending']}
    used, seq = min(options, key=lambda entry: (-entry[0], len(entry[1]), entry[1]))
    physical = float(Decimal(used * divisor) / scale)
    ids = [items[index].material_id for index in seq]
    return {'material_ids': ids, 'module_count': len(ids), 'used_length_mm': physical,
            'remaining_length_mm': max(0.0, float(available_length) - physical),
            'counts': dict(sorted(Counter(ids).items())), 'reason': 'EXACT_DP_OPTIMUM',
            'objective': ['minimum_tail', 'minimum_module_count', 'material_id_ascending']}


def _request(req):
    # Pydantic instances remain mutable; revalidate their serialized fields at
    # every public algorithm boundary, including in-process service callers.
    req = PlanRequest.model_validate(req.model_dump(mode='json') if isinstance(req,PlanRequest) else req)
    confirmation = req.space.confirmation
    if confirmation.state not in ('AUTO_VALIDATED', 'CONFIRMED', 'SYNTHETIC_TEST'):
        _fail('SPACE_REQUIRES_CONFIRMATION', '门店范围尚缺有效输入事实。')
    if confirmation.source_sha256 != req.source.sha256 or confirmation.geometry_sha256 != space_geometry_digest(req.space):
        _fail('SPACE_CONFIRMATION_MISMATCH', '确认的来源或几何摘要不匹配，请重新确认。')
    if not confirmation.confirmed_by:
        _fail('SPACE_REQUIRES_CONFIRMATION', '缺少确认人或合成测试来源。')
    if req.space.unresolved:
        _fail('UNRESOLVED_SPACE', '空间仍有未解决事项，不能开始规划。')
    if req.business.product_count != len(req.business.products):
        _fail('INVALID_BUSINESS', '商品数量与标准商品记录不一致。')
    templates = _catalog(req.business.templates)
    data = req.space.boundary
    if any(len(ring) < 4 or ring[0] != ring[-1] for ring in [data.boundary_mm, *data.holes_mm]):
        _fail('INVALID_SPATIAL', '范围和孔洞必须显式闭合。')
    scope = Polygon(data.boundary_mm, data.holes_mm)
    if scope.is_empty or not scope.is_valid or scope.area <= 0:
        _fail('INVALID_SPATIAL', '规划范围拓扑无效；规划器不会自动修复。')
    for wall in req.space.barriers:
        line=LineString([wall.start_mm,wall.end_mm])
        if line.is_empty or not line.is_valid or line.length<=EPS:
            _fail('INVALID_SPATIAL', '墙线无效或长度为零。')
    for zone in [*req.space.exclusions,*req.space.entrances]:
        if any(len(ring)<4 or ring[0] != ring[-1] for ring in [zone.boundary_mm,*zone.holes_mm]):
            _fail('INVALID_SPATIAL', '障碍或入口范围必须显式闭合。')
        polygon=Polygon(zone.boundary_mm,zone.holes_mm)
        if polygon.is_empty or not polygon.is_valid or polygon.area <= 0:
            _fail('INVALID_SPATIAL', '障碍或入口拓扑无效。')
    if not req.rules.orientation_candidates:
        _fail('INVALID_RULES', '至少需要一个货架排方向。')
    return req, scope, templates


def _xy(u, v, direction):
    return (u, v) if direction == 0 else (v, u)


def _rect(u, v, length, depth, direction):
    x, y = _xy(u, v, direction)
    return box(x, y, x + (length if direction == 0 else depth), y + (depth if direction == 0 else length))


def _bom(shelves, templates):
    counts = Counter(s.material_id for s in shelves)
    return [BOMRowV2(material_id=t.material_id, length_mm=t.length_mm, depth_mm=t.depth_mm,
                    default_level_count=t.default_level_count, quantity=counts[t.material_id],
                    total_level_count=counts[t.material_id] * t.default_level_count)
            for t in templates if counts[t.material_id]]


def _bounds(scope, direction):
    x0, y0, x1, y1 = scope.bounds
    return (x0, y0, x1, y1) if direction == 0 else (y0, x0, y1, x1)


def _obstacles(req):
    walls=[LineString([w.start_mm,w.end_mm]) for w in req.space.barriers]
    zones=[Polygon(z.boundary_mm,z.holes_mm) for z in [*req.space.exclusions,*req.space.entrances]]
    return walls,zones


def _safe_region(req,scope):
    walls,zones=_obstacles(req)
    clearance=req.rules.boundary_clearance_mm
    region=scope.buffer(-clearance,join_style=2) if clearance else scope
    # Uniform minimum setback makes the subsequent end-corridor top-up valid
    # even when wall_clearance and boundary_clearance differ in custom rules.
    blockers=[wall.buffer(max(req.rules.wall_clearance_mm,clearance,EPS),cap_style=3,join_style=2) for wall in walls]
    blockers += [zone.buffer(max(clearance,EPS),join_style=2) for zone in zones]
    if not clearance:
        blockers += [Polygon(hole).buffer(EPS,join_style=2) for hole in req.space.boundary.holes_mm]
    return region.difference(unary_union(blockers)) if blockers else region


def _parts(geometry):
    if geometry.geom_type=='Polygon':
        return [geometry] if geometry.area>EPS else []
    return [p for g in getattr(geometry,'geoms',[]) for p in _parts(g)]


def _available_intervals(req,scope,direction,v,depth,region=None):
    """Project GEOS-forbidden portions of a full-depth run strip onto its axis.

    Each returned interval fits a complete rectangular run. Projection is
    conservative for slanted obstacles; it never treats the scope AABB as space.
    """
    u0,_,u1,_=_bounds(scope,direction)
    strip=_rect(u0,v,u1-u0,depth,direction)
    region=_safe_region(req,scope) if region is None else region
    forbidden=strip.difference(region)
    blocked=sorted((_bounds(part,direction)[0],_bounds(part,direction)[2]) for part in _parts(forbidden))
    intervals=[]
    cursor=u0
    for low,high in blocked:
        if low>cursor+EPS: intervals.append((cursor,low))
        cursor=max(cursor,high)
    if cursor<u1-EPS: intervals.append((cursor,u1))
    # safe_region already includes the boundary setback; top it up to the
    # requested end corridor. Rectangles therefore retain M01 coordinates.
    extension=max(0.0,req.rules.end_aisle_mm-req.rules.boundary_clearance_mm)
    return [(low+extension,high-extension) for low,high in intervals if high-low>2*extension+EPS]


def _candidate(req, scope, templates, direction, depth):
    u0, v0, u1, v1 = _bounds(scope, direction)
    rules = req.rules
    ends = max(rules.end_aisle_mm, rules.boundary_clearance_mm)
    available = u1 - u0 - 2 * ends
    if available <= 0:
        return []
    subset = [t for t in templates if t.depth_mm == depth]
    count = math.floor((v1 - v0 - 2 * rules.boundary_clearance_mm + rules.aisle_width_mm + EPS)
                       / (depth + rules.aisle_width_mm))
    if count <= 0:
        return []
    lookup = {t.material_id: t for t in templates}
    # Center unused cross-axis space and DP tail without world-origin rounding.
    vstart = v0 + (v1 - v0 - (count * depth + (count - 1) * rules.aisle_width_mm)) / 2
    runs = []
    region=_safe_region(req,scope)
    module_count=0
    for row in range(count):
        v = vstart + row * (depth + rules.aisle_width_mm)
        for low,high in _available_intervals(req,scope,direction,v,depth,region):
            available=high-low
            combination=combine_modules(available,subset,rules.min_modules_per_run)
            if not combination['material_ids']: continue
            used=combination['used_length_mm']
            ustart=low+combination['remaining_length_mm']/2
            cursor = ustart
            modules = []
            run_id=f'R{len(runs)+1:04d}'
            for material_id in combination['material_ids']:
                t = lookup[material_id]
                x, y = _xy(cursor + t.length_mm / 2, v + depth / 2, direction)
                modules.append(Shelf(id=f'{run_id}-M{len(modules)+1:04d}', material_id=material_id,
                    x_mm=x, y_mm=y, rotation_deg=direction, length_mm=t.length_mm, depth_mm=depth,
                    default_level_count=t.default_level_count,
                    footprint_mm=list(_rect(cursor, v, t.length_mm, depth, direction).exterior.coords)))
                cursor += t.length_mm
            module_count+=len(modules)
            if module_count>MAX_MODULES: _fail('PLANNING_LIMIT', '方案超过 10000 个货架模块。')
            runs.append(ShelfRun(run_id=run_id, direction_deg=direction,
                start_mm=_xy(ustart, v + depth/2, direction), end_mm=_xy(ustart+used, v+depth/2, direction),
                available_length_mm=available, depth_mm=depth, modules=modules, used_length_mm=used,
                remaining_length_mm=combination['remaining_length_mm'],
                footprint_mm=list(_rect(ustart, v, used, depth, direction).exterior.coords)))
    neighbors = _geometric_neighbors(runs)
    for run in runs:
        run.neighbors = neighbors[run.run_id]
    return runs


def _geometric_neighbors(runs):
    """Nearest overlapping parallel run on each cross-axis side, from geometry."""
    result = {}
    polygons = {run.run_id: Polygon(run.footprint_mm) for run in runs}
    for run in runs:
        polygon = polygons[run.run_id]
        a0, b0, a1, b1 = _bounds(polygon, run.direction_deg)
        center = (b0+b1)/2
        sides = {-1: [], 1: []}
        for other in runs:
            if run.run_id == other.run_id or run.direction_deg != other.direction_deg:
                continue
            other_polygon = polygons[other.run_id]
            oa0, ob0, oa1, ob1 = _bounds(other_polygon, run.direction_deg)
            if min(a1,oa1)-max(a0,oa0) <= EPS:
                continue
            delta = (ob0+ob1)/2-center
            if abs(delta) <= EPS:
                continue
            side = -1 if delta < 0 else 1
            sides[side].append((abs(delta), other.run_id, polygon.distance(other_polygon)))
        result[run.run_id] = []
        for side in (-1,1):
            if sides[side]:
                _, other_id, clearance = min(sides[side])
                result[run.run_id].append({'run_id':other_id,'relation':'PARALLEL_ADJACENT','clearance_mm':clearance})
    return result


def _metrics(scope, runs):
    shelves = [s for run in runs for s in run.modules]
    neighbors = _geometric_neighbors(runs)
    separations = [n['clearance_mm'] for adjacent in neighbors.values() for n in adjacent]
    lengths = [sum(s.length_mm for s in run.modules) for run in runs]
    isolated = sum(len(run.modules) == 1 for run in runs)
    return {'algorithm': 'whole_run_dp_v2_m02', 'module_count': len(shelves), 'run_count': len(runs),
        'average_modules_per_run': len(shelves)/len(runs), 'isolated_run_count': isolated, 'isolated_run_ratio': isolated/len(runs),
        'average_run_length_mm': sum(lengths)/len(runs),
        'effective_length_mm': sum(s.length_mm for s in shelves),
        'tail_waste_mm': sum(r.remaining_length_mm for r in runs),
        'max_same_run_gap_mm': max((Polygon(a.footprint_mm).distance(Polygon(b.footprint_mm))
                                  for r in runs for a,b in zip(r.modules,r.modules[1:])), default=0.0),
        'minimum_parallel_aisle_mm': min(separations) if separations else None,
        'footprint_density': sum(Polygon(s.footprint_mm).area for s in shelves)/scope.area,
        'planning_area_mm2': scope.area, 'geometry_violation_count': 0,
        'direction_counts': dict(sorted(Counter(r.direction_deg for r in runs).items())),
        'template_counts': dict(sorted(Counter(s.material_id for s in shelves).items())),
        'short_fragment_policy': 'Reject any interval unable to fit min_modules_per_run',
        'objective_order': ['zero_geometry_violations', 'zero_isolated_runs', 'maximum_average_modules_per_run',
            'maximum_average_run_length', 'valid_aisles', 'maximum_effective_length', 'minimum_tail_waste',
            'maximum_module_count', 'direction_ascending', 'depth_ascending']}


def validate_runs(req, result):
    """Reconstruct modules, runs, continuity, corridors and BOM independently."""
    req, scope, templates = _request(req)
    result = LayoutV2.model_validate(result) if isinstance(result, dict) else result
    def require(condition, message):
        if not condition:
            _fail('VALIDATION_FAILED', message)
    require(result.source == req.source and result.space == req.space and result.rules == req.rules,
            '输出来源、空间或规则不匹配。')
    require(bool(result.runs) and bool(result.shelves) and bool(result.bom), '方案必须包含货架排、模块和物料清单。')
    flattened = [s for run in result.runs for s in run.modules]
    require(flattened == result.shelves, '平铺货架必须与 runs.modules 完全相等。')
    require(len({s.id for s in flattened}) == len(flattened), '货架实例 ID 重复。')
    require(len({r.run_id for r in result.runs}) == len(result.runs), '货架排 ID 重复。')
    lookup = {t.material_id: t for t in templates}
    footprints = []
    terminal_corridors = []
    gaps = []
    region=_safe_region(req,scope)
    walls,zones=_obstacles(req)
    for run in result.runs:
        require(run.direction_deg in req.rules.orientation_candidates, '货架排方向不在配置内。')
        require(len(run.modules) >= req.rules.min_modules_per_run, '短碎排模块不足。')
        direction = run.direction_deg
        start_u, start_v = run.start_mm if direction == 0 else run.start_mm[::-1]
        cursor = start_u
        module_polygons = []
        for shelf in run.modules:
            t = lookup.get(shelf.material_id)
            require(t is not None and (shelf.length_mm, shelf.depth_mm, shelf.default_level_count) ==
                    (t.length_mm, t.depth_mm, t.default_level_count), '货架规格与物料模板不匹配。')
            require(shelf.rotation_deg == direction and shelf.depth_mm == run.depth_mm, '同排方向或深度不一致。')
            expected = _rect(cursor, start_v-run.depth_mm/2, t.length_mm, t.depth_mm, direction)
            polygon = Polygon(shelf.footprint_mm)
            require(shelf.footprint_mm[0] == shelf.footprint_mm[-1] and polygon.is_valid and polygon.equals(expected),
                    '同排货架不连续，或轮廓与排中心线和规格不一致。')
            require(abs(polygon.centroid.x-shelf.x_mm) <= EPS and abs(polygon.centroid.y-shelf.y_mm) <= EPS,
                    '货架中心与轮廓不一致。')
            module_polygons.append(polygon)
            cursor += shelf.length_mm
        used = sum(s.length_mm for s in run.modules)
        u0, v0, u1, v1 = _bounds(scope, direction)
        ends = max(req.rules.end_aisle_mm, req.rules.boundary_clearance_mm)
        intervals=_available_intervals(req,scope,direction,start_v-run.depth_mm/2,run.depth_mm,region)
        matches=[(low,high) for low,high in intervals if start_u+EPS>=low and cursor<=high+EPS
                 and abs(run.available_length_mm-(high-low))<=EPS]
        require(bool(matches) and abs(run.used_length_mm-used) <= EPS and
                abs(run.remaining_length_mm-(run.available_length_mm-used)) <= EPS, '排长、排尾或障碍切分区间与实际不一致。')
        require(all(abs(a-b) <= EPS for a,b in zip(run.end_mm, _xy(cursor,start_v,direction))), '排终点不一致。')
        polygon = Polygon(run.footprint_mm)
        require(run.footprint_mm[0] == run.footprint_mm[-1] and polygon.is_valid and
                polygon.equals(unary_union(module_polygons)), '整排 footprint 与模块并集不一致。')
        require(scope.covers(polygon) and polygon.distance(scope.boundary)+EPS >= req.rules.boundary_clearance_mm,
                '货架越界或边墙退让不足。')
        require(start_u-u0+EPS >= ends and u1-cursor+EPS >= ends, '货架排端通道不足。')
        require(all(not polygon.intersects(wall) and polygon.distance(wall)+EPS>=req.rules.wall_clearance_mm
                    for wall in walls), '货架碰墙或墙退让不足。')
        require(all(not polygon.intersects(zone) for zone in zones), '货架接触禁放区或入口。')
        require(all(not polygon.intersects(Polygon(hole)) for hole in req.space.boundary.holes_mm), '货架接触孔洞。')
        for a in (start_u-req.rules.end_aisle_mm,cursor):
            corridor=_rect(a,start_v-run.depth_mm/2,req.rules.end_aisle_mm,run.depth_mm,direction)
            require(scope.covers(corridor), '真实排端通道越过范围或孔洞。')
            inner=corridor.buffer(-EPS,join_style=2)
            require(all(not inner.intersects(obstacle) for obstacle in [*walls,*zones]), '真实排端通道穿过墙、禁放区或入口。')
            terminal_corridors.append(corridor)
        for previous in footprints:
            separation = polygon.distance(previous)
            require(polygon.intersection(previous).area <= EPS and separation+EPS >= req.rules.aisle_width_mm,
                    '不同排重叠或净过道不足。')
            gaps.append(separation)
        footprints.append(polygon)
    require(all(corridor.intersection(footprint).area<=EPS for corridor in terminal_corridors for footprint in footprints),
            '真实排端通道与货架相交。')
    require(result.bom == _bom(flattened, templates), '物料数量、规格或总层数与实例不一致。')
    # Never accept user-declared quality evidence. Geometric identity above
    # establishes safe physical instances; now derive their evidence anew.
    expected_neighbors = _geometric_neighbors(result.runs)
    for run in result.runs:
        require(run.neighbors == expected_neighbors[run.run_id], '邻排关系或净距与实际几何不一致。')
    expected_metrics = _metrics(scope,result.runs)
    canonical = lambda value: json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
    for key,value in expected_metrics.items():
        require(key in result.metrics and canonical(result.metrics[key]) == canonical(value),
                f'质量指标 {key} 与实际几何重算结果不一致。')
    return {'status': 'PASS', 'geometry_violation_count': 0, 'store_outside_shelves': 0,
            'same_run_continuity': 'PASS', 'parallel_aisles': 'PASS', 'end_aisles': 'PASS',
            'boundary_clearance': 'PASS', 'template_identity': 'PASS', 'bom_instance_identity': 'PASS',
            'quality_metrics_identity': 'PASS', 'geometric_neighbors_identity': 'PASS',
            'obstacle_clearance': 'PASS', 'entrance_nonoccupation': 'PASS',
            'circulation_scope': 'Local run-end corridors and inter-run gaps only; no whole-store reachability or fire-code certification',
            'minimum_inter_run_clearance_mm': min(gaps) if gaps else None,
            'shelf_count': len(flattened), 'bom_quantity': sum(row.quantity for row in result.bom),
            'total_level_count': sum(row.total_level_count for row in result.bom)}


def generate_layout(req: PlanRequest) -> LayoutV2:
    req, scope, templates = _request(req)
    candidates = []
    for direction in sorted(set(req.rules.orientation_candidates)):
        for depth in sorted({t.depth_mm for t in templates}):
            runs = _candidate(req, scope, templates, direction, depth)
            if not runs:
                continue
            metrics = _metrics(scope, runs)
            shelves = [s for run in runs for s in run.modules]
            result = LayoutV2(source=req.source, space=req.space, rules=req.rules, runs=runs,
                shelves=shelves, bom=_bom(shelves,templates), metrics=metrics, validation={})
            result.validation = validate_runs(req,result)
            score = (-metrics['average_modules_per_run'], -metrics['average_run_length_mm'],
                     -metrics['effective_length_mm'], metrics['tail_waste_mm'], -metrics['module_count'], direction,depth)
            candidates.append((score,result))
    if not candidates:
        _fail('NO_SAFE_LAYOUT', '没有可容纳至少两个连续模块且满足过道规则的货架排。')
    result = min(candidates,key=lambda item:item[0])[1]
    result.metrics['candidate_count'] = len(candidates)
    canonical = json.dumps(result.model_dump(mode='json'),sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    result.metrics['deterministic_sha256'] = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    result.metrics['hash_definition'] = 'Canonical JSON before deterministic_sha256 and hash_definition are added'
    return result
