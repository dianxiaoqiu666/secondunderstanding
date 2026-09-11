"""Wall singles and independent back-to-back ShelfRuns on explicit geometry.

The existing DP, module/BOM representation and M09 validation stay intact.
Here the aisle unit is an assembly: only its explicitly paired internal backs
are structural space. All distinct assemblies retain the configured aisle.
"""
from collections import Counter
import math

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from shared.contracts import Shelf
from shared.planning_v2 import ShelfRun
from shared.operations_planning import (OperationsInput, OperationsLayout, PickFace,
    ShelfAssembly, operations_request)
from services.planning.engine import PlanningError
from services.planning.runs import (EPS, _request, _rect, _xy, _bounds, _parts,
    _available_intervals, _safe_region, _bom, _metrics, _geometric_neighbors, combine_modules)


def _require(condition, message):
    if not condition:
        raise PlanningError('OPERATIONS_GEOMETRY_FAILED', message)


def checked_input(value):
    value = OperationsInput.model_validate(value.model_dump(mode='json') if isinstance(value, OperationsInput) else value)
    req, scope, templates = _request(operations_request(value))
    _require(req.rules.end_aisle_mm + EPS >= req.rules.aisle_width_mm,
             '排端规则宽度小于当前通行净宽，不能发布作业方案。')
    opening = LineString(value.entrance_opening_mm)
    _require(LineString(scope.exterior.coords).buffer(EPS).covers(opening), '明确入口不在外边界上。')
    _require(opening.length + EPS >= req.rules.aisle_width_mm, '入口宽度不满足当前通行规则。')
    _require(len({w.id for w in value.usable_walls}) == len(value.usable_walls), '可用墙段 ID 重复。')
    physical = [LineString(scope.exterior.coords), *[LineString([w.start_mm, w.end_mm]) for w in req.space.barriers]]
    for wall in value.usable_walls:
        line = LineString([wall.start_mm, wall.end_mm]); nx, ny = wall.inward_normal
        _require(line.length > EPS and any(p.buffer(EPS).covers(line) for p in physical), '可用墙段缺少明确边界或墙线依据。')
        dx, dy = wall.end_mm[0]-wall.start_mm[0], wall.end_mm[1]-wall.start_mm[1]
        _require((abs(dx) < EPS or abs(dy) < EPS) and (nx, ny) in ((0,1),(0,-1),(1,0),(-1,0))
                 and abs(dx*nx+dy*ny)<EPS, '当前墙排候选仅支持有明确内向的正交墙段。')
        middle=line.interpolate(.5, normalized=True)
        _require(scope.contains(Point(middle.x+nx, middle.y+ny)), '墙段内向不指向规划范围。')
        _require(line.intersection(opening).length <= EPS, '可用墙段不能包含明确门口。')
    return value, req, scope, templates


def _wall_range(wall, direction, depth, setback):
    a, b = (wall.start_mm, wall.end_mm) if direction == 0 else (wall.start_mm[::-1], wall.end_mm[::-1])
    normal = wall.inward_normal if direction == 0 else wall.inward_normal[::-1]
    if abs(a[1]-b[1]) > EPS or not normal[1]:
        return None
    v = a[1]+setback if normal[1] > 0 else a[1]-setback-depth
    return min(a[0],b[0]), max(a[0],b[0]), v, int(normal[1])


def _intervals(req, scope, direction, v, depth, corridors, wall=None):
    intervals = _available_intervals(req, scope, direction, v, depth)
    if wall is not None:
        spec = _wall_range(wall,direction,depth,max(req.rules.boundary_clearance_mm,req.rules.wall_clearance_mm))
        if spec is None:
            return []
        a,b,_,_=spec
        intervals = [(max(lo,a),min(hi,b)) for lo,hi in intervals if min(hi,b)>max(lo,a)+EPS]
    u0,_,u1,_=_bounds(scope,direction)
    strip=_rect(u0,v,u1-u0,depth,direction)
    cutters=[]
    for corridor in corridors:
        intersection=strip.intersection(Polygon(corridor.boundary_mm,corridor.holes_mm))
        cutters += [(_bounds(p,direction)[0],_bounds(p,direction)[2]) for p in _parts(intersection)]
    # Planned passages are already full-width traversable reservations. Their
    # space can serve a run end; padding them a second time creates false waste.
    for left,right in sorted(cutters):
        remaining=[]
        for lo,hi in intervals:
            if right <= lo+EPS or left >= hi-EPS:
                remaining.append((lo,hi)); continue
            if left>lo+EPS: remaining.append((lo,min(left,hi)))
            if right<hi-EPS: remaining.append((max(right,lo),hi))
        intervals=remaining
    return intervals


def _make_run(identifier, direction, v, depth, low, high, templates, minimum):
    subset=[t for t in templates if t.depth_mm==depth]
    combination=combine_modules(high-low,subset,minimum)
    if not combination['material_ids']:
        return None
    lookup={t.material_id:t for t in subset}
    cursor=low+combination['remaining_length_mm']/2; start=cursor; modules=[]
    for material_id in combination['material_ids']:
        t=lookup[material_id]; polygon=_rect(cursor,v,t.length_mm,depth,direction)
        modules.append(Shelf(id=f'{identifier}-M{len(modules)+1:04d}',material_id=t.material_id,
            x_mm=polygon.centroid.x,y_mm=polygon.centroid.y,rotation_deg=direction,
            length_mm=t.length_mm,depth_mm=depth,default_level_count=t.default_level_count,
            footprint_mm=list(polygon.exterior.coords)))
        cursor+=t.length_mm
    return ShelfRun(run_id=identifier,direction_deg=direction,start_mm=_xy(start,v+depth/2,direction),
        end_mm=_xy(cursor,v+depth/2,direction),available_length_mm=high-low,depth_mm=depth,
        modules=modules,used_length_mm=cursor-start,remaining_length_mm=combination['remaining_length_mm'],
        footprint_mm=list(_rect(start,v,cursor-start,depth,direction).exterior.coords))


def _faces(run, assembly, sign, width, side='SINGLE'):
    result=[]; direction=run.direction_deg
    for module in run.modules:
        lo,bottom,hi,top=_bounds(Polygon(module.footprint_mm),direction)
        v=top if sign>0 else bottom
        result.append(PickFace(id=f'F-{module.id}',module_id=module.id,run_id=run.run_id,
            assembly_id=assembly.id,side=side,start_mm=_xy(lo,v,direction),end_mm=_xy(hi,v,direction),
            outward_normal=_xy(0,sign,direction),standing_point_mm=_xy((lo+hi)/2,v+sign*width/2,direction)))
    return result


def geometry_metrics(scope, runs, assemblies):
    metrics=_metrics(scope,runs)
    polygons=[Polygon(a.footprint_mm) for a in assemblies]
    gaps=[a.distance(b) for i,a in enumerate(polygons) for b in polygons[i+1:]]
    shelves=[s for r in runs for s in r.modules]
    paired={rid for a in assemblies if a.kind=='BACK_TO_BACK' for rid in a.run_ids}
    single=sum(s.length_mm for r in runs if r.run_id not in paired for s in r.modules)
    double=sum(s.length_mm for r in runs if r.run_id in paired for s in r.modules)
    metrics.update(algorithm='employee_operations_composite_v1',
        wall_single_run_count=sum(a.kind=='WALL_SINGLE' for a in assemblies),
        central_single_run_count=sum(a.kind in ('CENTRAL_SINGLE','M09_SINGLE_ASSUMPTION') for a in assemblies),
        double_sided_run_group_count=sum(a.kind=='BACK_TO_BACK' for a in assemblies),
        double_sided_bay_count=sum(len(a.bay_pairs) for a in assemblies),
        effective_pick_length_mm=single+double,single_sided_pick_length_mm=single,double_sided_pick_length_mm=double,
        nominal_board_area_m2=sum(s.length_mm*s.depth_mm*s.default_level_count/1e6 for s in shelves),
        nominal_board_area_definition='sum(module length * single-side footprint depth * configured default levels); NOT net shelf area or SKU capacity',
        net_shelf_area_m2=None,net_shelf_area_status='UNKNOWN',
        depth_counts=dict(sorted(Counter(s.depth_mm for s in shelves).items())),
        level_counts=dict(sorted(Counter(s.default_level_count for s in shelves).items())),
        minimum_parallel_aisle_mm=min(gaps) if gaps else None,
        short_run_count=sum(len(r.modules)==2 for r in runs),
        structure_basis='TWO_INDEPENDENT_SINGLE_SIDED_MODULES_TEST_ASSUMPTION',
        objective_order=['geometry','all_faces_reachable','fixed_length_and_nominal_area_floors','pareto_space_and_routes'])
    return metrics


def generate_candidate(value, direction, depth, corridors=(), pattern='BACK_TO_BACK', policy='STRUCTURE_ONLY'):
    value,req,scope,templates=checked_input(value)
    _require(direction in req.rules.orientation_candidates, '方向未获规则允许。')
    _require(depth in {t.depth_mm for t in templates}, '模板深度不存在。')
    width=req.rules.aisle_width_mm; gap=value.assembly_assumptions.structure_gap_mm
    u0,v0,u1,v1=_bounds(scope,direction)
    setback=max(req.rules.boundary_clearance_mm,req.rules.wall_clearance_mm)
    runs=[];assemblies=[];faces=[]
    def add(low,high,v,kind,sign=1,wall=None):
        aid=f'A{len(assemblies)+1:04d}'
        first=_make_run(f'R{len(runs)+1:04d}',direction,v,depth,low,high,templates,req.rules.min_modules_per_run)
        if first is None:return
        group=[first]; total=depth; pairs=[]
        if kind=='BACK_TO_BACK':
            second=_make_run(f'R{len(runs)+2:04d}',direction,v+depth+gap,depth,low,high,templates,req.rules.min_modules_per_run)
            group.append(second);total=2*depth+gap;pairs=[[a.id,b.id] for a,b in zip(first.modules,second.modules)]
        start=_bounds(Polygon(first.footprint_mm),direction)[0]
        assembly=ShelfAssembly(id=aid,kind=kind,run_ids=[r.run_id for r in group],
            footprint_mm=list(_rect(start,v,first.used_length_mm,total,direction).exterior.coords),
            side_depths_mm=[depth]*len(group),total_depth_mm=total,structure_gap_mm=gap if len(group)==2 else 0,
            wall_id=wall.id if wall else None,bay_pairs=pairs)
        runs.extend(group);assemblies.append(assembly)
        if len(group)==2:
            faces.extend(_faces(first,assembly,-1,width,'A'));faces.extend(_faces(group[1],assembly,1,width,'B'))
        else:faces.extend(_faces(first,assembly,sign,width))
    for wall in value.usable_walls:
        spec=_wall_range(wall,direction,depth,setback)
        if spec is None:continue
        _,_,v,sign=spec
        for low,high in _intervals(req,scope,direction,v,depth,corridors,wall):
            add(low,high,v,'WALL_SINGLE',sign,wall)
    # Reserve fronts of possible wall shelves even where a doorway cuts one.
    # This yields a compact loop without creating a picking aisle behind a wall run.
    bottom=v0+setback+depth+width; top=v1-setback-depth-width
    total=2*depth+gap if pattern=='BACK_TO_BACK' else depth
    count=math.floor((top-bottom+width+EPS)/(total+width))
    if count<=0 and pattern=='BACK_TO_BACK':
        total=depth;pattern='CENTRAL_SINGLE';count=math.floor((top-bottom+width+EPS)/(total+width))
    if count>0:
        start=bottom+(top-bottom-(count*total+(count-1)*width))/2
        for index in range(count):
            v=start+index*(total+width)
            for low,high in _intervals(req,scope,direction,v,total,corridors):
                add(low,high,v,'BACK_TO_BACK' if pattern=='BACK_TO_BACK' else 'CENTRAL_SINGLE',
                    1 if v+total/2 <= (v0+v1)/2 else -1)
    if not runs:
        raise PlanningError('NO_OPERATIONAL_CANDIDATE','当前有限结构候选没有可放置的连续排。')
    neighbors=_geometric_neighbors(runs)
    for run in runs:run.neighbors=neighbors[run.run_id]
    shelves=[s for r in runs for s in r.modules]
    result=OperationsLayout(source=req.source,space=req.space,rules=req.rules,runs=runs,shelves=shelves,
        bom=_bom(shelves,templates),metrics=geometry_metrics(scope,runs,assemblies),validation={},
        assemblies=assemblies,pick_faces=faces,planned_corridors=list(corridors),workpoints=value.workpoints,
        operational_policy=policy)
    result.validation=validate_operations_geometry(value,result)
    return result


def adapt_m09(value, baseline):
    """Retain M09 coordinates; disclose its missing facing semantics as a test assumption."""
    req=operations_request(value);scope=Polygon(req.space.boundary.boundary_mm,req.space.boundary.holes_mm)
    assemblies=[];faces=[]
    for run in baseline.runs:
        _,v0,_,v1=_bounds(scope,run.direction_deg)
        _,b0,_,b1=_bounds(Polygon(run.footprint_mm),run.direction_deg)
        assembly=ShelfAssembly(id=f'M09-{run.run_id}',kind='M09_SINGLE_ASSUMPTION',run_ids=[run.run_id],
            footprint_mm=run.footprint_mm,side_depths_mm=[run.depth_mm],total_depth_mm=run.depth_mm,
            structure_gap_mm=0,material_basis='M09_TEST_FACE_ASSUMPTION')
        assemblies.append(assembly)
        faces.extend(_faces(run,assembly,1 if (b0+b1)/2<=(v0+v1)/2 else -1,req.rules.aisle_width_mm))
    result=OperationsLayout(**baseline.model_dump(),assemblies=assemblies,pick_faces=faces,
        workpoints=value.workpoints,operational_policy='M09_PRESERVED_WITH_FIXED_CENTRE_FACING_TEST_ASSUMPTION')
    result.metrics=geometry_metrics(scope,result.runs,assemblies)
    return result


def validate_operations_geometry(value, result):
    """Reconstruct every instance/assembly/face; no trust in declared counts or metrics."""
    value,req,scope,templates=checked_input(value)
    result=OperationsLayout.model_validate(result.model_dump(mode='json') if isinstance(result,OperationsLayout) else result)
    _require(result.source==req.source and result.space==req.space and result.rules==req.rules
             and result.workpoints==value.workpoints,'输出来源、规则、空间或工作点不一致。')
    lookup={t.material_id:t for t in templates}; run_by={r.run_id:r for r in result.runs}
    _require(len(run_by)==len(result.runs),'排 ID 重复。')
    flattened=[s for r in result.runs for s in r.modules]
    _require(flattened==result.shelves and len({s.id for s in flattened})==len(flattened),'模块重复或平铺实例不一致。')
    _require(len({a.id for a in result.assemblies})==len(result.assemblies),'组合 ID 重复。')
    _require(Counter(rid for a in result.assemblies for rid in a.run_ids)==Counter(run_by.keys()),'每排必须恰归属一个组合。')
    walls=[LineString([w.start_mm,w.end_mm]) for w in req.space.barriers]
    blocked=[Polygon(z.boundary_mm,z.holes_mm) for z in [*req.space.exclusions,*req.space.entrances]]
    physical_zones=[Polygon(z.boundary_mm,z.holes_mm) for z in req.space.exclusions]
    corridor_polygons=[]
    for c in result.planned_corridors:
        p=Polygon(c.boundary_mm,c.holes_mm)
        _require(p.is_valid and p.area>EPS and scope.buffer(EPS).covers(p),'规划通道拓扑或范围无效。')
        _require(all(p.intersection(z).area<=EPS for z in physical_zones),'规划通道占用禁放区。')
        _require(all(not p.buffer(-EPS).intersects(w) for w in walls),'规划通道穿墙。')
        corridor_polygons.append(p)
    for run in result.runs:
        _require(run.direction_deg in req.rules.orientation_candidates and len(run.modules)>=req.rules.min_modules_per_run,'方向或最少模块数违规。')
        u,v=run.start_mm if run.direction_deg==0 else run.start_mm[::-1];cursor=u;parts=[]
        for shelf in run.modules:
            t=lookup.get(shelf.material_id)
            _require(t is not None and (shelf.length_mm,shelf.depth_mm,shelf.default_level_count)==(t.length_mm,t.depth_mm,t.default_level_count),'模板身份不一致。')
            _require(shelf.depth_mm==run.depth_mm and shelf.rotation_deg==run.direction_deg,'同排深度或方向不兼容。')
            p=Polygon(shelf.footprint_mm);expected=_rect(cursor,v-run.depth_mm/2,t.length_mm,t.depth_mm,run.direction_deg)
            _require(shelf.footprint_mm[0]==shelf.footprint_mm[-1] and p.is_valid and p.equals(expected),'模块占地与连续排不一致。')
            _require(abs(shelf.x_mm-p.centroid.x)<EPS and abs(shelf.y_mm-p.centroid.y)<EPS,'模块中心不一致。')
            parts.append(p);cursor+=shelf.length_mm
        _require(Polygon(run.footprint_mm).equals(unary_union(parts)),'整排占地不是模块并集。')
        _require(abs(run.used_length_mm-(cursor-u))<=EPS and abs(run.remaining_length_mm-(run.available_length_mm-run.used_length_mm))<=EPS,'排长或余量不一致。')
        _require(all(abs(a-b)<=EPS for a,b in zip(run.end_mm,_xy(cursor,v,run.direction_deg))),'排终点不一致。')
    assembly_polygons=[];end_corridors=[];expected_faces=[]
    for assembly in result.assemblies:
        group=[run_by[rid] for rid in assembly.run_ids];first=group[0];direction=first.direction_deg
        lo,bottom,hi,top=_bounds(Polygon(first.footprint_mm),direction)
        is_pair=assembly.kind=='BACK_TO_BACK';wall=None
        _require(len(group)==(2 if is_pair else 1),'组合侧数错误。')
        _require(assembly.side_depths_mm==[r.depth_mm for r in group],'单侧深度错误。')
        if is_pair:
            second=group[1]; gap=value.assembly_assumptions.structure_gap_mm
            _require(assembly.material_basis=='INDEPENDENT_MODULE_INSTANCES','未支持的共用结构物料口径。')
            _require(second.direction_deg==direction and second.depth_mm==first.depth_mm,'双侧方向或深度不兼容。')
            target=_rect(lo,top+gap,hi-lo,second.depth_mm,direction)
            _require(Polygon(second.footprint_mm).equals(target),'背靠背必须轴向同长、背部相对。')
            _require(assembly.bay_pairs==[[a.id,b.id] for a,b in zip(first.modules,second.modules)]
                and [s.material_id for s in first.modules]==[s.material_id for s in second.modules],'双面模块配对不完整或不兼容。')
            _require(abs(assembly.structure_gap_mm-gap)<=EPS,'结构间隙与测试输入不一致。')
            expected_faces+=_faces(first,assembly,-1,req.rules.aisle_width_mm,'A')
            expected_faces+=_faces(second,assembly,1,req.rules.aisle_width_mm,'B')
        else:
            _require(assembly.structure_gap_mm==0 and not assembly.bay_pairs,'单面不能申报双面间隙或配对。')
            if assembly.kind=='WALL_SINGLE':
                wall=next((w for w in value.usable_walls if w.id==assembly.wall_id),None)
                _require(wall is not None,'沿墙排缺少明确可用墙段。')
                spec=_wall_range(wall,direction,first.depth_mm,max(req.rules.boundary_clearance_mm,req.rules.wall_clearance_mm))
                _require(spec is not None and abs(bottom-spec[2])<=EPS and lo>=spec[0]-EPS and hi<=spec[1]+EPS,'沿墙排退让或所属墙段不正确。')
                sign=spec[3]
            else:
                _,v0,_,v1=_bounds(scope,direction);sign=1 if (bottom+top)/2<=(v0+v1)/2 else -1
            expected_faces+=_faces(first,assembly,sign,req.rules.aisle_width_mm)
        total=sum(assembly.side_depths_mm)+assembly.structure_gap_mm
        p=Polygon(assembly.footprint_mm)
        _require(abs(assembly.total_depth_mm-total)<=EPS and p.is_valid and p.equals(_rect(lo,bottom,hi-lo,total,direction)), '组合占地必须包含完整结构间隙。')
        intervals=_intervals(req,scope,direction,bottom,total,result.planned_corridors,wall)
        _require(any(lo>=a-EPS and hi<=b+EPS and all(abs(r.available_length_mm-(b-a))<=EPS for r in group) for a,b in intervals),'可用区段或余量与源几何、通道不一致。')
        _require(scope.covers(p) and p.distance(scope.boundary)+EPS>=req.rules.boundary_clearance_mm,'组合越界或边界净距不足。')
        _require(all(not p.intersects(w) and p.distance(w)+EPS>=req.rules.wall_clearance_mm for w in walls),'组合穿墙或净距不足。')
        _require(all(not p.intersects(z) for z in blocked),'组合占用禁放区或保留区。')
        _require(all(p.intersection(c).area<=EPS for c in corridor_polygons),'组合占用预先保留的通道。')
        for u in (lo-req.rules.end_aisle_mm,hi):
            end=_rect(u,bottom,req.rules.end_aisle_mm,total,direction)
            _require(scope.covers(end),'排端通道越界或穿孔洞。')
            _require(all(not end.buffer(-EPS).intersects(o) for o in [*walls,*physical_zones]),'排端通道穿墙或禁放区。')
            end_corridors.append(end)
        for previous in assembly_polygons:
            _require(p.intersection(previous).area<=EPS and p.distance(previous)+EPS>=req.rules.aisle_width_mm,'不同组合重叠或通行净距不足。')
        assembly_polygons.append(p)
    _require(all(c.intersection(p).area<=EPS for c in end_corridors for p in assembly_polygons),'排端连接被货架封闭。')
    _require(result.pick_faces==expected_faces,'每模块正确外向取货面或可站立点不完整。')
    _require(result.bom==_bom(flattened,templates),'物料数量、深度、层数与独立实例不一致。')
    neighbors=_geometric_neighbors(result.runs)
    _require(all(r.neighbors==neighbors[r.run_id] for r in result.runs),'邻排关系不一致。')
    expected=geometry_metrics(scope,result.runs,result.assemblies)
    for key,v in expected.items():
        # JSON object keys roundtrip as strings (depth and orientation counters).
        import json
        _require(json.dumps(result.metrics.get(key),sort_keys=True)==json.dumps(v,sort_keys=True),f'几何指标 {key} 与重算不一致。')
    return dict(status='PASS',geometry_violation_count=0,same_run_continuity='PASS',
        boundary_clearance='PASS',obstacle_clearance='PASS',assembly_aisles='PASS',end_aisles='PASS',
        material_instance_identity='PASS',pick_face_identity='PASS',metric_identity='PASS',
        internal_back_space='STRUCTURAL_SOLID_NO_PASSAGE',shelf_count=len(flattened),
        bom_quantity=sum(b.quantity for b in result.bom),human_acceptance='PENDING')
