"""Store identity above GEOS geometry; an open work selection is not a region.

No file I/O, coordinate repair, area ranking, inferred doors or reference inputs.
Polygonized cells describe local topology. Exterior incidence and source-wide
containment qualify a perimeter proposal; explicit intent is still required.
"""
import math
from shapely.geometry import LineString, Polygon, Point
from shapely.ops import polygonize_full, unary_union
from services.understanding.scope_review import (
    preview_scope, _parts, _identifier, _boundary, _source_ids, _usage,
    _open_components, bounds_for_edges,
)


def _polygon(value):
    return Polygon(value['boundary_mm'], value.get('holes_mm', []))


def review_store_scope(edges, scope_edge_ids, supplemental_edges, known_holes,
                       scope_candidate_id=None, *, fixed_exclusions=(),
                       perimeter_edge_ids=(), analysis_edge_ids=None):
    lookup = {e['edge_id']: e for e in edges}
    if analysis_edge_ids is None:
        analysis_edge_ids = [e['edge_id'] for e in edges
                             if e.get('role') not in {'EXCLUSION', 'HOLE', 'CONSTRUCTION'}]
    requested = [*scope_edge_ids, *perimeter_edge_ids]
    ids = sorted(set(analysis_edge_ids) | set(requested))
    # The M07 function is a geometry operation, not authorization of a store.
    geometry = preview_scope(edges, ids, supplemental_edges, [])
    topology = geometry['derived_topology']
    fatal = [f for f in geometry['feedback'] if f['code'] in {
        'DUPLICATE_SCOPE_EDGE', 'DUPLICATE_SOURCE_EDGE_ID', 'UNKNOWN_SCOPE_EDGE',
        'INVALID_SOURCE_LINEWORK', 'SUPPLEMENT_NOT_SOURCE_ENDPOINTS'}]
    if len(set(scope_edge_ids)) != len(scope_edge_ids):
        fatal.append({'code': 'DUPLICATE_SCOPE_EDGE', 'message': '同一源边被重复选择。'})
    if len(set(perimeter_edge_ids)) != len(perimeter_edge_ids):
        fatal.append({'code': 'DUPLICATE_PERIMETER_EDGE', 'message': '外围工作线组中有重复源边。'})
    result = dict(geometry, valid=not fatal, scope_ready=False, boundary=None,
                  scope_candidate_id=None, boundary_source_edge_ids=[],
                  source_edge_usage=_usage(None, topology), feedback=fatal,
                  scope_candidates=[], local_faces=[], perimeter_candidates=[], gap_candidates=[],
                  perimeter_edge_ids=sorted(set(perimeter_edge_ids)), selected_scope_edge_ids=sorted(scope_edge_ids))
    if fatal:
        return result
    segments = topology['edges']
    lines = [LineString([e['start_mm'], e['end_mm']]) for e in segments]
    constraints = [(z['id'], _polygon(z)) for z in fixed_exclusions]
    constraint_union = unary_union([p for _, p in constraints])
    # Supplements only connect exact admitted nodes. A new logical edge cannot
    # pass through an existing physical segment or duplicate it.
    physical = [LineString([lookup[i]['start_mm'], lookup[i]['end_mm']])
                for i in ids if i in lookup and lookup[i].get('role') != 'SCOPE_CANDIDATE']
    for supplement in supplemental_edges:
        line = LineString([supplement['start_mm'], supplement['end_mm']])
        for existing in physical:
            crossing = line.intersection(existing)
            if crossing.length > 0 or any(
                    isinstance(p, Point) and tuple(p.coords[0]) not in {tuple(line.coords[0]), tuple(line.coords[-1])}
                    for p in _parts(crossing)):
                result['valid'] = False
                result['feedback'].append({'code': 'SUPPLEMENT_CROSSES_SOURCE',
                    'message': '这段逻辑补线穿过或重叠已有源线；请撤销该补线，选择当前局部的正确端点。',
                    'points_mm': [supplement['start_mm'], supplement['end_mm']]})
                return result
    faces = sorted(_parts(polygonize_full(lines)[0]), key=lambda p: p.normalize().wkb_hex)
    face_ids = {}
    for face in faces:
        matched = [ident for ident, zone in constraints if zone.covers(face)]
        ident = _identifier('local-', face.normalize().wkb_hex)
        record = {'id': ident, 'boundary': _boundary(face), 'bounds_mm': list(face.bounds),
                  'area_mm2': face.area, 'source_edge_ids': _source_ids(face, topology),
                  'kind': 'FIXED_CONSTRAINT' if matched else 'LOCAL_UNCLASSIFIED',
                  'constraint_ids': matched, 'selectable': False,
                  'reason': ('与已知固定禁放区吻合，不能作为整店范围。' if matched else
                             '这是局部闭合面；闭合本身不能证明它是整店，不会自动采用或当作孔洞。')}
        outside=[s['edge_id'] for edge,line in zip(segments,lines) if not Polygon(face.exterior.coords).covers(line)
                 for s in edge['sources']]
        record['outside_source_edge_ids']=sorted(set(outside))
        if outside and not matched:
            record['reason']+=' 当前另有明确墙线在该面外，尚未完成的外围不能被它替代。'
        result['local_faces'].append(record)
        face_ids[ident] = face
    # A planar cell edge occurring once is exposed; a room partition occurs on
    # two cells. Recover their outer boundary without unioning occupied areas or
    # subtracting every room/column. Explicit holes are reattached unchanged.
    exposed = [line for line in lines if sum(face.boundary.intersection(line).length > 0 for face in faces) == 1]
    shells = sorted(_parts(polygonize_full(exposed)[0]), key=lambda p: p.normalize().wkb_hex)
    qualified = {}
    qualification_feedback=[]
    for shell_face in shells:
        shell = Polygon(shell_face.exterior.coords)
        if constraint_union.covers(shell):
            continue
        source_ids = _source_ids(shell, topology)
        intended = bool(set(perimeter_edge_ids).intersection(source_ids))
        incompatible_holes=[h for h in known_holes if not shell.contains(Polygon(h)) or shell.boundary.intersects(Polygon(h).boundary)]
        if incompatible_holes:
            if intended:
                qualification_feedback.append({'code':'SOURCE_HOLE_OUTSIDE_SELECTION',
                    'message':'所选外围与原图明确孔洞不兼容：该孔洞在范围外或接触外边。请定位核对外围选择，孔洞不会被删除或填平。',
                    'points_mm':incompatible_holes[0], 'bounds_mm':list(Polygon(incompatible_holes[0]).bounds),
                    'action':'定位这个明确孔洞与已选外围，检查哪段外围尚未纳入；保留其他局部工作。',
                    'pass_condition':'原图孔洞严格位于最终外围内，且不接触外边。'})
            continue
        # Exact containment of current relevant source facts, not area, bbox,
        # chain length or an arbitrary coverage percentage. A room inside an
        # incomplete store necessarily leaves source facts outside it.
        outside = [edge['id'] for edge, line in zip(segments, lines) if not shell.covers(line)]
        if outside or any(not shell.covers(zone) for _, zone in constraints):
            if intended and outside:
                first=next(e for e in segments if e['id']==outside[0])
                qualification_feedback.append({'code':'PERIMETER_OUTSIDE_SOURCE_FACTS',
                    'message':'当前闭合局部面没有包容已有明确墙线，不能代表整店；先定位尚未纳入的这部分外围证据。',
                    'source_edge_ids':[s['edge_id'] for s in first['sources']],
                    'points_mm':[first['start_mm'],first['end_mm']]})
            continue
        polygon = Polygon(shell.exterior.coords, known_holes).normalize()
        if not polygon.is_valid or polygon.area <= 0:
            continue
        boundary = _boundary(polygon)
        source_ids = _source_ids(polygon, topology)
        ident = _identifier('perimeter-closed-', {'boundary': boundary, 'sources': source_ids})
        record = {'id': ident, 'status': 'CLOSED', 'boundary': boundary, 'bounds_mm': list(polygon.bounds),
                  'area_mm2': polygon.area, 'source_edge_ids': [i for i in source_ids if i in lookup],
                  'logical_edge_ids': [i for i in source_ids if i not in lookup], 'boundary_source_edge_ids': source_ids,
                  'reason': '该外围包容当前已明确结构线网和约束；请明确这是本次门店外围。',
                  'evidence': ['SOURCE_NETWORK_ENCLOSURE', 'FIXED_CONSTRAINTS_PRESERVED'],
                  'unresolved_endpoints': [],
                  'segments': [e for e, line in zip(segments, lines) if polygon.boundary.intersection(line).length > 0]}
        result['perimeter_candidates'].append(record)
        explicit = bool(source_ids) and all(lookup[i].get('role') == 'SCOPE_CANDIDATE'
                                           for i in source_ids if i in lookup)
        intended = bool(set(perimeter_edge_ids).intersection(source_ids))
        context = any(shell.contains(line) for line in lines) or bool(constraints) or bool(known_holes)
        if explicit or intended or context:
            record['explicit_scope_intent'] = explicit
            result['scope_candidates'].append(record)
            qualified[ident] = polygon
        else:
            record['reason'] = '这是用途尚未明确的闭合线组；没有自动获得整店身份。仅在确属本次外围时选入，程序还会核对全图已知结构。'
    components = _open_components(topology)
    node_map = {tuple(n['point_mm']): n for n in topology['nodes']}
    segment_map = {e['id']: e for e in segments}
    for component in components:
        if not component['dangling_points_mm']:
            continue
        members = [segment_map[i] for i in component['derived_edge_ids']]
        if all(constraint_union.covers(LineString([e['start_mm'], e['end_mm']])) for e in members):
            continue
        endpoints = [{'point_mm': p, 'source_edge_ids': node_map[tuple(p)]['source_edges']}
                     for p in component['dangling_points_mm']]
        result['perimeter_candidates'].append({**component, 'status': 'OPEN', 'segments': members,
            'source_edge_ids': [i for i in component['source_edge_ids'] if i in lookup],
            'logical_edge_ids': [i for i in component['source_edge_ids'] if i not in lookup],
            'unresolved_endpoints': endpoints,
            'reason': '保留的开放墙线组，可选入外围工作集；内部支线也保留，开口未解释前不会生成规划区域。',
            'evidence': ['EXACT_SOURCE_CONNECTIVITY']})
    selected_set = set(perimeter_edge_ids)
    # At most one nearest existing-node proposal per exposed endpoint of a
    # chosen component. This is a local comparison, never a global cycle search
    # or a statement that the gap is a door or should be filled.
    pairs = {}
    for component in components:
        if not selected_set.intersection(component['source_edge_ids']):
            continue
        member_nodes = {tuple(p) for i in component['derived_edge_ids'] for p in
                        (segment_map[i]['start_mm'], segment_map[i]['end_mm'])}
        for value in component['dangling_points_mm']:
            a = tuple(value)
            options = [tuple(n['point_mm']) for n in topology['nodes'] if tuple(n['point_mm']) not in member_nodes]
            # A single open perimeter can have its two tips in one component.
            options += [tuple(p) for p in component['dangling_points_mm'] if tuple(p) != a]
            if not options:
                continue
            b = min(options, key=lambda p: (math.dist(a, p), p))
            pair = tuple(sorted((a, b)))
            ids_here = sorted(set(node_map[a]['source_edges']) | set(node_map[b]['source_edges']))
            pairs[pair] = {'id': _identifier('gap-', pair), 'start_mm': list(pair[0]), 'end_mm': list(pair[1]),
                'source_edge_ids': ids_here, 'gap_mm': math.dist(a, b),
                'reason': '最近已有节点之间仍缺连接依据；先核对原图。只在确属规划边界连接时补这一段；这不是门或实体墙，也不证明入口净空。'}
    result['gap_candidates'] = [pairs[p] for p in sorted(pairs)]
    chosen = None
    if scope_candidate_id:
        if scope_candidate_id not in qualified:
            result['valid'] = False
            result['feedback'].append({'code': 'LOCAL_FACE_NOT_STORE' if scope_candidate_id in face_ids or scope_candidate_id.startswith('scope-') else 'SCOPE_CANDIDATE_INVALID',
                'message': '这个闭合面没有整店范围资格；已知禁放区和未完成外围均保留，请选择外围线组处理局部开口。'})
        else:
            chosen = next(c for c in result['scope_candidates'] if c['id'] == scope_candidate_id)
    elif len(qualified) == 1:
        proposal = result['scope_candidates'][0]
        # Retain a user's pre-closure perimeter intent across local repairs.
        if selected_set.intersection(proposal['boundary_source_edge_ids']) or proposal.get('explicit_scope_intent'):
            chosen = proposal
    if chosen:
        result.update(scope_ready=True, boundary=chosen['boundary'], bounds_mm=chosen['bounds_mm'],
                      scope_candidate_id=chosen['id'], boundary_source_edge_ids=chosen['boundary_source_edge_ids'],
                      source_edge_usage=_usage(qualified[chosen['id']], topology))
    else:
        active = [c for c in result['perimeter_candidates'] if selected_set.intersection(c['source_edge_ids'])]
        endpoints = [p['point_mm'] for c in active for p in c['unresolved_endpoints']]
        if qualification_feedback:
            result['feedback'].extend(qualification_feedback)
            return result
        result['feedback'].append({'code': 'PERIMETER_UNFINISHED' if active else 'PERIMETER_SELECTION_REQUIRED',
            'message': ('已保留所选外围工作线组。请定位尚未解释的端点或开口；这些位置未确认前，不会用其他闭合小区代替门店。' if active else
                        '局部闭合区不等于门店。请选择已有外围线组，或选择有整店结构依据的闭合外围；无需重画整圈。'),
            'source_edge_ids': sorted(selected_set), 'points_mm': endpoints,
            'action': '先定位外围线组并选入工作集，再逐处核对已显示的局部开口；已知门沿门候选处理。',
            'pass_condition': '外围来源与当前门店相符，开口有依据，形成包容当前结构且保留孔洞、禁放约束的有效区域。'})
    return result
