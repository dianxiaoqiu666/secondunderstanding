"""Wall singles and independent back-to-back ShelfRuns on explicit geometry.

The existing DP, module/BOM representation and M09 validation stay intact.
Here the aisle unit is an assembly: only its explicitly paired internal backs
are structural space. All distinct assemblies retain the configured aisle.
"""
from collections import Counter
import math
from statistics import median

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
    from services.planning.operations_routes import validate_door_configuration
    door=validate_door_configuration(value)
    if door['status']!='PASS':
        raise PlanningError('SCENARIO_CONFIGURATION_INVALID','；'.join(door['issues']))
    req, scope, templates = _request(operations_request(value))
    _require(req.rules.end_aisle_mm + EPS >= req.rules.aisle_width_mm,
             '排端规则宽度小于当前通行净宽，不能发布作业方案。')
    opening = LineString(value.entrance_opening_mm)
    _require(LineString(scope.exterior.coords).buffer(EPS).covers(opening), '明确入口不在外边界上。')
    _require(opening.length + EPS >= req.rules.aisle_width_mm, '入口宽度不满足当前通行规则。')
    _require(len({w.id for w in value.usable_walls}) == len(value.usable_walls), '可用墙段 ID 重复。')
    physical = [LineString([w.start_mm, w.end_mm]) for w in req.space.barriers]
    if value.door_connection is None and not value.physical_perimeter_walls:
        # Legacy M10 inputs are retained as technical fixtures. With explicit
        # physical walls the logical planning scope is never wall evidence.
        physical.append(LineString(scope.exterior.coords))
    physical_lines=unary_union(physical)
    for wall in value.usable_walls:
        line = LineString([wall.start_mm, wall.end_mm]); nx, ny = wall.inward_normal
        _require(line.length > EPS and physical_lines.buffer(EPS).covers(line), '可用墙段缺少明确实体墙线依据。')
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


def _frontage_intervals(req, scope, direction, v, depth, signs):
    """Full-width standing centrelines, before module packing (not shelf centres).

    Only physical barriers/exclusions are solids for walking. Entrance reserve
    areas remain rack-free via _available_intervals but may carry access paths.
    No second end-aisle padding is applied to this access constraint.
    """
    radius=req.rules.aisle_width_mm/2
    walkable=scope.buffer(-radius,join_style=2)
    solids=[LineString([w.start_mm,w.end_mm]) for w in req.space.barriers]
    solids += [Polygon(z.boundary_mm,z.holes_mm) for z in req.space.exclusions]
    if solids:
        walkable=walkable.difference(unary_union(solids).buffer(radius,quad_segs=32))
    u0,_,u1,_=_bounds(scope,direction)
    intervals=[(u0,u1)]
    def segments(geometry):
        if geometry.geom_type=='LineString':
            return [geometry] if geometry.length>EPS else []
        return [p for item in getattr(geometry,'geoms',[]) for p in segments(item)]
    for sign in signs:
        front=v+depth+radius if sign>0 else v-radius
        line=LineString([_xy(u0,front,direction),_xy(u1,front,direction)])
        available=[(_bounds(part,direction)[0],_bounds(part,direction)[2])
                   for part in segments(line.intersection(walkable))]
        intervals=[(max(a,c),min(b,d)) for a,b in intervals for c,d in available
                   if min(b,d)>max(a,c)+EPS]
    return intervals


def _intervals(req, scope, direction, v, depth, corridors, wall=None, face_signs=(), occupied=(), occupied_ends=()):
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
    for polygon in occupied:
        # Other independently placed wall runs keep the same aisle rule. A
        # candidate's own end extension and the prior run's reserved ends are
        # separate geometric obligations, never a second generic aisle pad.
        lo,bottom,hi,top=_bounds(polygon,direction)
        clearance=unary_union([polygon.buffer(req.rules.aisle_width_mm,join_style=2),
            _rect(lo-req.rules.end_aisle_mm,bottom,hi-lo+2*req.rules.end_aisle_mm,top-bottom,direction)])
        cutters += [(_bounds(p,direction)[0],_bounds(p,direction)[2])
                    for p in _parts(strip.intersection(clearance))]
    for polygon in occupied_ends:
        cutters += [(_bounds(p,direction)[0],_bounds(p,direction)[2])
                    for p in _parts(strip.intersection(polygon))]
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
    if face_signs:
        frontage=_frontage_intervals(req,scope,direction,v,depth,face_signs)
        intervals=[(max(a,c),min(b,d)) for a,b in intervals for c,d in frontage
                   if min(b,d)>max(a,c)+EPS]
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


def _aisle_sections(scope, assemblies, faces, physical=()):
    """Measured clear spans between opposing parallel pick fronts only.

    Diagonal corner distances and paired internal backs are deliberately not
    aisles. An intervening assembly or physical obstacle removes its projected
    span; the reported endpoints therefore witness the actual clear width.
    """
    grouped={}
    for face in faces:
        grouped.setdefault((face.assembly_id,face.side),[]).append(face)
    fronts=[]
    for (aid,side),group in sorted(grouped.items()):
        normal=group[0].outward_normal
        direction=0 if normal[0]==0 else 90
        points=[p for f in group for p in (f.start_mm,f.end_mm)]
        uv=[p if direction==0 else p[::-1] for p in points]
        fronts.append(dict(assembly=aid,side=side,direction=direction,normal=normal,
            low=min(p[0] for p in uv),high=max(p[0] for p in uv),v=uv[0][1]))
    shapes={a.id:Polygon(a.footprint_mm) for a in assemblies}
    sections=[]
    for index,first in enumerate(fronts):
        for second in fronts[index+1:]:
            if first['assembly']==second['assembly'] or first['direction']!=second['direction']:
                continue
            if any(abs(a+b)>EPS for a,b in zip(first['normal'],second['normal'])):
                continue
            a,b=sorted((first,second),key=lambda f:f['v'])
            normal=a['normal'][1] if a['direction']==0 else a['normal'][0]
            if normal<=0:continue
            low,high=max(a['low'],b['low']),min(a['high'],b['high'])
            width=b['v']-a['v']
            if high<=low+EPS or width<=EPS:continue
            strip=_rect(low,a['v'],high-low,width,a['direction'])
            blockers=[p for aid,p in shapes.items() if aid not in (a['assembly'],b['assembly'])]
            # A view through a cross-aisle can see a distant opposing wall.
            # Its wall-to-wall distance is not the width of a parallel aisle;
            # require neighboring front rows, not a long cross-row sightline.
            if any(strip.intersection(p).area>EPS for p in blockers):continue
            blockers.extend(physical)
            forbidden=strip.difference(scope)
            if blockers:forbidden=unary_union([forbidden,strip.intersection(unary_union(blockers))])
            spans=[(low,high)]
            for part in _parts(forbidden):
                left,_,right,_=_bounds(part,a['direction'])
                spans=[span for lo,hi in spans for span in ((lo,min(hi,left)),(max(lo,right),hi)) if span[1]>span[0]+EPS]
            for lo,hi in spans:
                middle=(lo+hi)/2
                sections.append(dict(between_assemblies=[a['assembly'],b['assembly']],
                    sides=[a['side'],b['side']],start_mm=_xy(middle,a['v'],a['direction']),
                    end_mm=_xy(middle,b['v'],a['direction']),clear_width_mm=width,
                    overlap_start_mm=_xy(lo,(a['v']+b['v'])/2,a['direction']),
                    overlap_end_mm=_xy(hi,(a['v']+b['v'])/2,a['direction']),
                    overlap_length_mm=hi-lo,role='OPPOSING_PICK_FACE_AISLE',
                    measurement_basis='PARALLEL_OPPOSING_FRONTS_CLEAR_PROJECTED_SPAN'))
    return sections


def geometry_metrics(scope, runs, assemblies, faces=(), physical=()):
    metrics=_metrics(scope,runs)
    polygons=[Polygon(a.footprint_mm) for a in assemblies]
    gaps=[a.distance(b) for i,a in enumerate(polygons) for b in polygons[i+1:]]
    shelves=[s for r in runs for s in r.modules]
    paired={rid for a in assemblies if a.kind=='BACK_TO_BACK' for rid in a.run_ids}
    single=sum(s.length_mm for r in runs if r.run_id not in paired for s in r.modules)
    double=sum(s.length_mm for r in runs if r.run_id in paired for s in r.modules)
    sections=_aisle_sections(scope,assemblies,faces,physical)
    widths=[section['clear_width_mm'] for section in sections]
    module_gaps=[Polygon(a.footprint_mm).distance(Polygon(b.footprint_mm))
                 for run in runs for a,b in zip(run.modules,run.modules[1:])]
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
        minimum_assembly_distance_mm=min(gaps) if gaps else None,
        assembly_distance_definition='minimum distance between distinct assembly solids; may include corners, not an aisle width',
        aisle_sections=sections,
        parallel_pick_aisle_min_mm=min(widths) if widths else None,
        parallel_pick_aisle_median_mm=median(widths) if widths else None,
        parallel_pick_aisle_max_mm=max(widths) if widths else None,
        same_run_module_max_gap_mm=max(module_gaps,default=0),
        internal_back_to_back_gap_mm=sorted({a.structure_gap_mm for a in assemblies if a.kind=='BACK_TO_BACK'}),
        short_run_count=sum(len(r.modules)==2 for r in runs),
        structure_basis='TWO_INDEPENDENT_SINGLE_SIDED_MODULES_TEST_ASSUMPTION',
        objective_order=['geometry','all_faces_reachable','fixed_length_and_nominal_area_floors','pareto_space_and_routes'])
    return metrics


def _wall_sections(value, runs, assemblies):
    walls={wall.id:wall for wall in value.usable_walls};by_run={run.run_id:run for run in runs}
    result=[]
    for assembly in assemblies:
        if assembly.kind!='WALL_SINGLE':continue
        wall=walls[assembly.wall_id];run=by_run[assembly.run_ids[0]]
        line=LineString([wall.start_mm,wall.end_mm])
        result.append(dict(assembly_id=assembly.id,wall_id=wall.id,run_id=run.run_id,
            source_segment_start_mm=wall.start_mm,source_segment_end_mm=wall.end_mm,
            source_provenance=wall.provenance,module_count=len(run.modules),
            used_length_mm=run.used_length_mm,available_length_mm=run.available_length_mm,
            tail_waste_mm=run.remaining_length_mm,
            back_setback_mm=Polygon(run.footprint_mm).distance(line),
            back_space_role='REQUIRED_WALL_CLEARANCE_NO_PICK_AISLE',
            is_short_run=len(run.modules)==2,
            reason='沿明确可用墙段朝内连续排；所用源墙段不含明确门口，障碍、规划通道、排端与组合净距继续裁切。分段数量不等同于仅两模块的碎排。'))
    return result


def _measurement_solids(req):
    # Thin physical wall lines also interrupt an aisle span. A tiny buffer is
    # only a polygonization aid for measurement, not a changed clearance rule.
    return [Polygon(zone.boundary_mm,zone.holes_mm) for zone in req.space.exclusions]+[
        LineString([wall.start_mm,wall.end_mm]).buffer(EPS,cap_style=2) for wall in req.space.barriers]


def _central_rows(req, scope, direction, depth, gap, pattern, residual_single_at=None):
    width=req.rules.aisle_width_mm
    _,v0,_,v1=_bounds(scope,direction)
    setback=max(req.rules.boundary_clearance_mm,req.rules.wall_clearance_mm)
    bottom=v0+setback+depth+width;top=v1-setback-depth-width
    total=2*depth+gap if pattern=='BACK_TO_BACK' else depth
    count=math.floor((top-bottom+width+EPS)/(total+width))
    if count<=0 and pattern=='BACK_TO_BACK' and residual_single_at is None:
        total=depth;pattern='CENTRAL_SINGLE';count=math.floor((top-bottom+width+EPS)/(total+width))
    if count<=0:return []
    if residual_single_at is None:
        kinds=[(total,'BACK_TO_BACK' if pattern=='BACK_TO_BACK' else 'CENTRAL_SINGLE')]*count
    else:
        _require(residual_single_at in ('LOW','HIGH') and pattern=='BACK_TO_BACK','余量单面机制参数无效。')
        remaining=top-bottom-(count*total+(count-1)*width)
        if remaining+EPS<depth+width:return []
        kinds=[(total,'BACK_TO_BACK')]*count
        kinds.insert(0 if residual_single_at=='LOW' else len(kinds),(depth,'CENTRAL_SINGLE'))
    used=sum(size for size,_ in kinds)+(len(kinds)-1)*width
    cursor=bottom+(top-bottom-used)/2;rows=[]
    for size,kind in kinds:
        rows.append((cursor,size,kind,1 if cursor+size/2<=(v0+v1)/2 else -1))
        cursor+=size+width
    return rows


def residual_single_options(value, direction, depth):
    """At most two mirrored choices; no enumeration unless geometric slack fits."""
    value,req,scope,_=checked_input(value)
    rows=_central_rows(req,scope,direction,depth,value.assembly_assumptions.structure_gap_mm,'BACK_TO_BACK','LOW')
    return ('LOW','HIGH') if rows else ()


def _residual_space_use(req, scope, direction, depth, gap, side):
    width=req.rules.aisle_width_mm;setback=max(req.rules.boundary_clearance_mm,req.rules.wall_clearance_mm)
    _,low,_,high=_bounds(scope,direction)
    available=high-low-2*(setback+depth+width)
    pair_depth=2*depth+gap
    count=math.floor((available+width+EPS)/(pair_depth+width))
    remaining=available-(count*pair_depth+(count-1)*width)
    return dict(main_direction_deg=direction,position=side,original_double_row_count=count,
        original_residual_width_mm=remaining,single_depth_mm=depth,additional_aisle_mm=width,
        single_plus_aisle_required_mm=depth+width,
        additional_double_plus_aisle_required_mm=pair_depth+width,
        residual_after_single_mm=remaining-depth-width,
        explanation=f'原齐次双面排余宽 {remaining:g} mm；单面 {depth:g} + 新增净通道 {width:g} = {depth+width:g} mm 可放入，'
            f'额外完整双面需 {pair_depth+width:g} mm，超过余宽。最终余 {remaining-depth-width:g} mm 平分至两侧；'
            '单面背侧通道必须由所列对面墙排或双面取货面服务，不作为无人使用的背部过道。')


def _residual_back_services(req, scope, runs, assemblies, faces):
    """A new central single's back aisle must serve a wall or paired pick front."""
    by_run={run.run_id:run for run in runs};shapes={a.id:Polygon(a.footprint_mm) for a in assemblies}
    face_groups={}
    for face in faces:face_groups.setdefault((face.assembly_id,face.side),[]).append(face)
    physical=_measurement_solids(req);result=[]
    for assembly in assemblies:
        if assembly.kind!='CENTRAL_SINGLE':continue
        run=by_run[assembly.run_ids[0]];direction=run.direction_deg
        lo,bottom,hi,top=_bounds(shapes[assembly.id],direction)
        own=face_groups[(assembly.id,'SINGLE')][0]
        sign=own.outward_normal[1] if direction==0 else own.outward_normal[0]
        back=bottom if sign>0 else top;coverage=[];witnesses=[]
        for other in assemblies:
            if other.kind not in ('WALL_SINGLE','BACK_TO_BACK'):continue
            for (aid,side),group in face_groups.items():
                if aid!=other.id or group[0].outward_normal!=own.outward_normal:continue
                coords=[p if direction==0 else p[::-1] for face in group for p in (face.start_mm,face.end_mm)]
                front=coords[0][1]
                if (back-front)*sign<=EPS:continue
                left=max(lo,min(p[0] for p in coords));right=min(hi,max(p[0] for p in coords))
                if right<=left+EPS:continue
                strip=_rect(left,min(back,front),right-left,abs(back-front),direction)
                if any(strip.intersection(shape).area>EPS for aid,shape in shapes.items()
                       if aid not in (assembly.id,other.id)):continue
                forbidden=strip.difference(scope)
                if physical:forbidden=unary_union([forbidden,strip.intersection(unary_union(physical))])
                spans=[(left,right)]
                for part in _parts(forbidden):
                    a,_,b,_=_bounds(part,direction)
                    spans=[span for low,high in spans for span in ((low,min(high,a)),(max(low,b),high))
                           if span[1]>span[0]+EPS]
                for low,high in spans:
                    coverage.append((low,high))
                    witnesses.append(dict(served_assembly_id=other.id,served_side=side,
                        served_face_ids=[face.id for face in group],clear_width_mm=abs(back-front),
                        start_mm=_xy(low,back,direction),end_mm=_xy(high,back,direction)))
        covered=[]
        for low,high in sorted(coverage):
            if covered and low<=covered[-1][1]+EPS:covered[-1]=(covered[-1][0],max(high,covered[-1][1]))
            else:covered.append((low,high))
        length=sum(high-low for low,high in covered)
        result.append(dict(assembly_id=assembly.id,required_back_length_mm=hi-lo,served_back_length_mm=length,
            full_back_aisle_has_pick_service=length+EPS>=hi-lo,witnesses=witnesses,
            role='AISLE_SERVES_WALL_OR_DOUBLE_PICK_FRONTS_BEHIND_EXTRA_SINGLE'))
    return result


def generate_candidate(value, direction, depth, corridors=(), pattern='BACK_TO_BACK', policy='STRUCTURE_ONLY', *, frontage_aware=False, all_wall_directions=False, residual_single_at=None):
    value,req,scope,templates=checked_input(value)
    _require(direction in req.rules.orientation_candidates, '方向未获规则允许。')
    _require(depth in {t.depth_mm for t in templates}, '模板深度不存在。')
    width=req.rules.aisle_width_mm; gap=value.assembly_assumptions.structure_gap_mm
    u0,v0,u1,v1=_bounds(scope,direction)
    setback=max(req.rules.boundary_clearance_mm,req.rules.wall_clearance_mm)
    runs=[];assemblies=[];faces=[];occupied=[];occupied_ends=[]
    def add(low,high,v,kind,sign=1,wall=None,run_direction=None):
        active_direction=direction if run_direction is None else run_direction
        aid=f'A{len(assemblies)+1:04d}'
        first=_make_run(f'R{len(runs)+1:04d}',active_direction,v,depth,low,high,templates,req.rules.min_modules_per_run)
        if first is None:return
        group=[first]; total=depth; pairs=[]
        if kind=='BACK_TO_BACK':
            second=_make_run(f'R{len(runs)+2:04d}',active_direction,v+depth+gap,depth,low,high,templates,req.rules.min_modules_per_run)
            group.append(second);total=2*depth+gap;pairs=[[a.id,b.id] for a,b in zip(first.modules,second.modules)]
        start=_bounds(Polygon(first.footprint_mm),active_direction)[0]
        assembly=ShelfAssembly(id=aid,kind=kind,run_ids=[r.run_id for r in group],
            footprint_mm=list(_rect(start,v,first.used_length_mm,total,active_direction).exterior.coords),
            side_depths_mm=[depth]*len(group),total_depth_mm=total,structure_gap_mm=gap if len(group)==2 else 0,
            wall_id=wall.id if wall else None,bay_pairs=pairs)
        runs.extend(group);assemblies.append(assembly)
        occupied.append(Polygon(assembly.footprint_mm))
        occupied_ends.extend(_rect(u,v,req.rules.end_aisle_mm,total,active_direction)
                             for u in (start-req.rules.end_aisle_mm,start+first.used_length_mm))
        if len(group)==2:
            faces.extend(_faces(first,assembly,-1,width,'A'));faces.extend(_faces(group[1],assembly,1,width,'B'))
        else:faces.extend(_faces(first,assembly,sign,width))
    wall_specs=[(wall,0 if abs(wall.start_mm[1]-wall.end_mm[1])<=EPS else 90) for wall in value.usable_walls]
    if all_wall_directions:
        wall_specs.sort(key=lambda item:(item[1]!=direction,item[0].id))
    for wall,wall_direction in wall_specs:
        if not all_wall_directions:wall_direction=direction
        if wall_direction not in req.rules.orientation_candidates:continue
        spec=_wall_range(wall,wall_direction,depth,setback)
        if spec is None:continue
        _,_,v,sign=spec
        intervals=_intervals(req,scope,wall_direction,v,depth,corridors,wall,(sign,) if frontage_aware else (),
            occupied if all_wall_directions else (),occupied_ends if all_wall_directions else ())
        for low,high in intervals:
            add(low,high,v,'WALL_SINGLE',sign,wall,wall_direction)
    # Reserve fronts of possible wall shelves even where a doorway cuts one.
    # This yields a compact loop without creating a picking aisle behind a wall run.
    rows=_central_rows(req,scope,direction,depth,gap,pattern,residual_single_at)
    _require(residual_single_at is None or rows,'现有横向余量不足以容纳一排单面及原规则通道。')
    for v,total,kind,sign in rows:
        signs=(-1,1) if kind=='BACK_TO_BACK' else (sign,)
        intervals=_intervals(req,scope,direction,v,total,corridors,face_signs=signs if frontage_aware else (),
            occupied=occupied if all_wall_directions else (),occupied_ends=occupied_ends if all_wall_directions else ())
        for low,high in intervals:add(low,high,v,kind,sign)
    if not runs:
        raise PlanningError('NO_OPERATIONAL_CANDIDATE','当前有限结构候选没有可放置的连续排。')
    neighbors=_geometric_neighbors(runs)
    for run in runs:run.neighbors=neighbors[run.run_id]
    shelves=[s for r in runs for s in r.modules]
    result=OperationsLayout(source=req.source,space=req.space,rules=req.rules,runs=runs,shelves=shelves,
        bom=_bom(shelves,templates),metrics=geometry_metrics(scope,runs,assemblies,faces,_measurement_solids(req)),validation={},
        assemblies=assemblies,pick_faces=faces,planned_corridors=list(corridors),workpoints=value.workpoints,
        operational_policy=policy)
    result.metrics['frontage_interval_policy']='FULL_WIDTH_FACE_CENTRELINES_V1' if frontage_aware else 'M10_PHYSICAL_STRIP_ONLY'
    result.metrics['generation_spec']=dict(main_direction_deg=direction,side_depth_mm=depth,
        all_wall_directions=all_wall_directions)
    if residual_single_at is not None:
        _require(any(a.kind=='CENTRAL_SINGLE' for a in assemblies),'余量单面排被实际障碍或通道完全裁掉。')
        result.metrics['generation_spec']['residual_single_at']=residual_single_at
        result.metrics['residual_single_back_service']=_residual_back_services(req,scope,runs,assemblies,faces)
        result.metrics['residual_space_use']=_residual_space_use(req,scope,direction,depth,gap,residual_single_at)
    result.metrics['wall_run_sections']=_wall_sections(value,runs,assemblies)
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
    result.metrics=geometry_metrics(scope,result.runs,assemblies,faces,_measurement_solids(req))
    result.metrics['wall_run_sections']=[]
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
    generation=result.metrics.get('generation_spec')
    mixed=bool(generation and generation.get('all_wall_directions'))
    if generation is not None:
        _require(set(generation) in ({'main_direction_deg','side_depth_mm','all_wall_directions'},
                                    {'main_direction_deg','side_depth_mm','all_wall_directions','residual_single_at'})
            and generation['main_direction_deg'] in req.rules.orientation_candidates
            and generation['side_depth_mm'] in {t.depth_mm for t in templates}
            and isinstance(generation['all_wall_directions'],bool),'生成主轴、深度或混合墙排机制不合法。')
        _require(all(r.depth_mm==generation['side_depth_mm'] for r in result.runs),'生成主轴模板深度与实体不一致。')
        _require(all(run_by[rid].direction_deg==generation['main_direction_deg']
            for a in result.assemblies if a.kind!='WALL_SINGLE' for rid in a.run_ids),'中央排与明确生成主轴不一致。')
        if 'residual_single_at' in generation:
            _require(generation['residual_single_at'] in ('LOW','HIGH') and mixed,'余量单面仅适用于明确墙前环路的混合墙排。')
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
        frontage_policy=result.metrics.get('frontage_interval_policy','M10_PHYSICAL_STRIP_ONLY')
        _require(frontage_policy in ('FULL_WIDTH_FACE_CENTRELINES_V1','M10_PHYSICAL_STRIP_ONLY'),'未知取货面区段机制。')
        signs=(-1,1) if is_pair else (sign,)
        intervals=_intervals(req,scope,direction,bottom,total,result.planned_corridors,wall,
                             signs if frontage_policy=='FULL_WIDTH_FACE_CENTRELINES_V1' else (),
                             assembly_polygons if mixed else (),end_corridors if mixed else ())
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
    expected=geometry_metrics(scope,result.runs,result.assemblies,result.pick_faces,_measurement_solids(req))
    expected['wall_run_sections']=_wall_sections(value,result.runs,result.assemblies)
    if generation and 'residual_single_at' in generation:
        back_service=_residual_back_services(req,scope,result.runs,result.assemblies,result.pick_faces)
        _require(back_service and all(row['full_back_aisle_has_pick_service'] for row in back_service),
            '新增单面的背侧过道没有完整的对面墙排或双面取货面服务依据。')
        expected['residual_single_back_service']=back_service
        expected['residual_space_use']=_residual_space_use(req,scope,generation['main_direction_deg'],
            generation['side_depth_mm'],value.assembly_assumptions.structure_gap_mm,generation['residual_single_at'])
    for key,v in expected.items():
        # JSON object keys roundtrip as strings (depth and orientation counters).
        import json
        _require(json.dumps(result.metrics.get(key),sort_keys=True)==json.dumps(v,sort_keys=True),f'几何指标 {key} 与重算不一致。')
    return dict(status='PASS',geometry_violation_count=0,same_run_continuity='PASS',
        boundary_clearance='PASS',obstacle_clearance='PASS',assembly_aisles='PASS',end_aisles='PASS',
        material_instance_identity='PASS',pick_face_identity='PASS',metric_identity='PASS',
        internal_back_space='STRUCTURAL_SOLID_NO_PASSAGE',shelf_count=len(flattened),
        bom_quantity=sum(b.quantity for b in result.bom),human_acceptance='PENDING')
