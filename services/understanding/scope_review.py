"""Exact source-edge selection preview. No file I/O, inference or tolerance growth."""
from collections import Counter
import hashlib
import json
import math
from shapely import node
from shapely.geometry import LineString,Polygon,MultiLineString,Point
from shapely.ops import polygonize_full,unary_union
from shapely.strtree import STRtree


def drawing_edges(drawing):
    edges=[];indices=Counter()
    for entity in [*drawing.get('lines',[]),*drawing.get('polylines',[])]:
        if entity.get('boundary_selectable') is False:continue
        handle=entity['handle']
        points=entity.get('points_mm') or [entity['start_mm'],entity['end_mm']]
        raw=entity.get('raw_points_mm',points)
        kind=entity.get('source_type') or ('LINE' if 'start_mm' in entity else 'LWPOLYLINE')
        for index,(start,end) in enumerate(zip(points,points[1:])):
            number=indices[handle];indices[handle]+=1
            edges.append({'edge_id':f'{handle}:{number}','display_handle':handle,
                'source_handle':entity.get('source_handle',handle),'source_type':kind,'layer':entity['layer'],
                'raw_coords_mm':[raw[index],raw[index+1]] if raw is not None and len(raw)==len(points) else None,
                'world_coords_mm':[start,end],'start_mm':start,'end_mm':end,'role':entity['role']})
    return edges


def bounds_for_edges(edges):
    points=[p for edge in edges for p in (edge['start_mm'],edge['end_mm'])]
    return [min(p[0] for p in points),min(p[1] for p in points),max(p[0] for p in points),max(p[1] for p in points)] if points else None


def _parts(geometry):
    return list(geometry.geoms) if hasattr(geometry,'geoms') else ([geometry] if not geometry.is_empty else [])


def _identifier(prefix,value):
    text=json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
    return prefix+hashlib.sha256(text.encode()).hexdigest()[:20]


def _point(value):
    if not isinstance(value,(tuple,list)) or len(value)!=2 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in value):
        raise ValueError('NONFINITE_OR_INVALID_SOURCE_POINT')
    return tuple(0.0 if float(v)==0 else float(v) for v in value)


def _boundary(polygon):
    polygon=polygon.normalize()
    return {'boundary_mm':[list(p) for p in polygon.exterior.coords],
            'holes_mm':[[list(p) for p in r.coords] for r in polygon.interiors]}


def node_linework(edges):
    """GEOS exact noding/deduplication, with reversible parent parameters."""
    originals=sorted(edges,key=lambda e:e['edge_id'])
    if len({e['edge_id'] for e in originals})!=len(originals):raise ValueError('DUPLICATE_SOURCE_EDGE_ID')
    lines=[]
    for edge in originals:
        start,end=_point(edge['start_mm']),_point(edge['end_mm'])
        if start==end:raise ValueError('ZERO_LENGTH_SOURCE_EDGE:'+edge['edge_id'])
        line=LineString([start,end])
        if not math.isfinite(line.length) or line.length<=0:raise ValueError('INVALID_SOURCE_LINE_LENGTH:'+edge['edge_id'])
        lines.append(line)
    if not lines:return {'nodes':[],'edges':[],'diagnostics':{'cuts':[],'dangles':[],'invalid':[]}}
    noded=node(MultiLineString(lines));tree=STRtree(lines)
    # GEOS intersection coordinates need not satisfy a second floating-point
    # collinearity predicate against the parent line. Preserve membership from
    # the original pairwise intersections instead of buffering/snap tolerances.
    memberships={}
    for index,line in enumerate(lines):
        for p in line.coords:memberships.setdefault(tuple(p),set()).add(index)
    for index,line in enumerate(lines):
        for raw_index in tree.query(line,predicate='intersects'):
            other=int(raw_index)
            if other<=index:continue
            for part in _parts(line.intersection(lines[other])):
                for p in part.coords:memberships.setdefault(tuple(p),set()).update((index,other))
    segments=sorted({tuple(sorted((tuple(a),tuple(b)))) for part in _parts(noded)
                     for a,b in zip(part.coords,list(part.coords)[1:])})
    derived=[];node_sources={};node_degrees=Counter()
    for start,end in segments:
        parents=[]
        for index in sorted(memberships.get(start,set())&memberships.get(end,set())):
            line=lines[index]
            source=originals[index]
            parents.append({'edge_id':source['edge_id'],'source_handle':source.get('source_handle'),
                'interval':[line.project(Point(start),normalized=True),line.project(Point(end),normalized=True)],
                'kind':source.get('kind') or source.get('role') or 'UNCLASSIFIED'})
        if not parents:raise ValueError('DERIVED_EDGE_SOURCE_UNRESOLVED')
        parents.sort(key=lambda s:s['edge_id'])
        record={'id':_identifier('derived-',[start,end]),'start_mm':list(start),'end_mm':list(end),'sources':parents}
        derived.append(record)
        for p in (start,end):
            node_sources.setdefault(p,set()).update(s['edge_id'] for s in parents);node_degrees[p]+=1
    nodes=[{'id':_identifier('node-',p),'point_mm':list(p),'source_edges':sorted(s),'degree':node_degrees[p]}
           for p,s in sorted(node_sources.items())]
    geometry=[LineString([e['start_mm'],e['end_mm']]) for e in derived]
    _,cuts,dangles,invalid=polygonize_full(geometry)
    def diagnose(value):
        return sorted(e['id'] for e,line in zip(derived,geometry) if value.intersection(line).length>0)
    return {'nodes':nodes,'edges':derived,'diagnostics':{'cuts':diagnose(cuts),'dangles':diagnose(dangles),
        'invalid':[{'wkt':g.wkt,'source_edge_ids':sorted({s['edge_id'] for e,line in zip(derived,geometry)
            if g.intersection(line).length>0 for s in e['sources']})} for g in _parts(invalid)]}}


def _source_ids(polygon,topology):
    return sorted({s['edge_id'] for edge in topology['edges']
        if polygon.boundary.intersection(LineString([edge['start_mm'],edge['end_mm']])).length>0 for s in edge['sources']})


def _usage(polygon,topology):
    result={key:[] for key in ('boundary','interior','exterior','unused')}
    for edge in topology['edges']:
        line=LineString([edge['start_mm'],edge['end_mm']])
        key=('unused' if polygon is None else 'boundary' if polygon.boundary.intersection(line).length>0
             else 'interior' if polygon.covers(line.interpolate(0.5,normalized=True)) else 'exterior')
        result[key].append({'derived_edge_id':edge['id'],'sources':edge['sources']})
    return result


def _open_components(topology):
    """Partition only exact derived-node adjacency; never choose an exterior."""
    incident={};lookup={e['id']:e for e in topology['edges']}
    for edge in topology['edges']:
        for point in (edge['start_mm'],edge['end_mm']):
            incident.setdefault(tuple(point),set()).add(edge['id'])
    unseen=set(lookup);components=[]
    while unseen:
        pending=[min(unseen)];members=set();points=set()
        while pending:
            edge_id=pending.pop()
            if edge_id in members:continue
            members.add(edge_id);unseen.discard(edge_id);edge=lookup[edge_id]
            for value in (edge['start_mm'],edge['end_mm']):
                point=tuple(value);points.add(point)
                pending.extend(incident[point]-members)
        source_ids=sorted({s['edge_id'] for edge_id in members for s in lookup[edge_id]['sources']})
        bounds=bounds_for_edges([lookup[edge_id] for edge_id in members])
        components.append({'id':_identifier('open-component-',sorted(members)),
            'bounds_mm':bounds,'source_edge_ids':source_ids,'derived_edge_ids':sorted(members),
            'dangling_points_mm':[list(p) for p in sorted(points) if len(incident[p])==1]})
    # Coordinate order is a stable UI traversal order, not a main-component or
    # area ranking. Every component and every source remains in the response.
    return sorted(components,key=lambda c:(c['bounds_mm'],c['source_edge_ids'],c['id']))


def preview_scope(edges,scope_edge_ids,supplemental_edges,known_holes,scope_candidate_id=None):
    """Preview selected source-network faces without inferring a store boundary."""
    lookup={edge['edge_id']:edge for edge in edges};feedback=[];supplements=[]
    topology={'nodes':[],'edges':[],'diagnostics':{'cuts':[],'dangles':[],'invalid':[]}}
    result={'valid':False,'boundary':None,'feedback':feedback,'bounds_mm':None,
        'selected_scope_edge_ids':sorted(scope_edge_ids),'supplemental_edges':supplements,
        'derived_topology':topology,'diagnostics':topology['diagnostics'],'scope_candidates':[],
        'boundary_source_edge_ids':[],'source_edge_usage':_usage(None,topology)}
    if len(set(scope_edge_ids))!=len(scope_edge_ids):feedback.append({'code':'DUPLICATE_SCOPE_EDGE','message':'同一源边被重复选择。'})
    if len(lookup)!=len(edges):feedback.append({'code':'DUPLICATE_SOURCE_EDGE_ID','message':'源边ID重复，无法唯一追踪。'})
    unknown=sorted(set(scope_edge_ids)-set(lookup))
    if unknown:feedback.append({'code':'UNKNOWN_SCOPE_EDGE','message':'所选边不属于当前源图。','source_edge_ids':unknown})
    selected=[lookup[e] for e in sorted(set(scope_edge_ids)) if e in lookup]
    try:
        # Only supplement endpoint admission uses the full source topology;
        # unselected lines never contribute faces to the selected boundary.
        all_topology=node_linework(edges) if supplemental_edges else None
        endpoints={tuple(n['point_mm']) for n in all_topology['nodes']} if all_topology else set()
        ids=set()
        for supplied in sorted(supplemental_edges,key=lambda e:e.get('id','')):
            ident=supplied.get('id','');start=_point(supplied['start_mm']);end=_point(supplied['end_mm'])
            if not ident or ident in ids:raise ValueError('DUPLICATE_OR_MISSING_SUPPLEMENTAL_ID')
            ids.add(ident)
            if start==end:raise ValueError('ZERO_LENGTH_SUPPLEMENTAL_EDGE')
            if start not in endpoints or end not in endpoints:
                feedback.append({'code':'SUPPLEMENT_NOT_SOURCE_ENDPOINTS','message':'补线必须连接精确源端点或GEOS派生节点；不会吸附跨越真实空隙。','points_mm':[list(start),list(end)]})
                continue
            kind=supplied.get('kind','HUMAN_BOUNDARY_SUPPLEMENT')
            if kind not in {'HUMAN_BOUNDARY_SUPPLEMENT','PORTAL_CLOSURE'}:raise ValueError('INVALID_SUPPLEMENTAL_KIND')
            supplement={'id':ident,'start_mm':list(start),'end_mm':list(end),'kind':kind}
            supplements.append(supplement)
            selected.append({'edge_id':'supplemental:'+ident,'source_handle':None,'start_mm':start,'end_mm':end,'kind':kind})
        topology=node_linework(selected)
    except (ValueError,KeyError,TypeError) as exc:
        feedback.append({'code':'INVALID_SOURCE_LINEWORK','message':str(exc)})
    try:
        for edge in selected:_point(edge['start_mm']);_point(edge['end_mm'])
        bounds=bounds_for_edges(selected)
    except (ValueError,TypeError,KeyError):bounds=None
    result.update(derived_topology=topology,diagnostics=topology['diagnostics'],bounds_mm=bounds,
                  source_edge_usage=_usage(None,topology))
    if feedback:return result
    # Local closed faces do not explain other open source components. Expose
    # both without treating legitimate branches as a global invalid boundary.
    open_components=[c for c in _open_components(topology) if c['dangling_points_mm']]
    result['open_components']=open_components
    result['diagnostics']['open_components']=open_components
    geometries=[LineString([e['start_mm'],e['end_mm']]) for e in topology['edges']]
    faces=sorted(_parts(polygonize_full(geometries)[0]),key=lambda p:p.normalize().wkb_hex)
    if not faces:
        components=_open_components(topology)
        result['open_components']=components
        result['diagnostics']['open_components']=components
        first=components[0] if components else {}
        feedback.append({'code':'SCOPE_NOT_CLOSED','message':'所选源网络尚未形成闭合面；先定位这一局部，其他连通组件仍完整保留，并未判断其内外。',
            'component_id':first.get('id'),'source_edge_ids':first.get('source_edge_ids',[]),
            'bounds_mm':first.get('bounds_mm'),'points_mm':first.get('dangling_points_mm',[])})
        return result
    # polygonize emits nested void interiors as independent faces. Alternating
    # shell containment keeps those voids before unioning neighboring room faces.
    shells=[Polygon(face.exterior.coords) for face in faces]
    retained=[face for i,face in enumerate(faces) if sum(shell.contains(face.representative_point())
        for j,shell in enumerate(shells) if i!=j)%2==0]
    combined=unary_union(retained)
    candidates=[];polygons={}
    for polygon in sorted(_parts(combined),key=lambda p:p.normalize().wkb_hex):
        if not isinstance(polygon,Polygon) or not polygon.is_valid:continue
        holes=[list(r.coords) for r in polygon.interiors]
        okay=True
        for ring in known_holes:
            if len(ring)<4 or ring[0]!=ring[-1]:okay=False;break
            hole=Polygon(ring)
            outer=Polygon(polygon.exterior.coords)
            if not hole.is_valid or hole.area<=0 or not outer.contains(hole) or outer.boundary.intersects(hole.boundary):okay=False;break
            if not any(hole.equals(Polygon(h)) for h in holes):holes.append(ring)
        if not okay:continue
        candidate_poly=Polygon(polygon.exterior.coords,holes).normalize()
        if not candidate_poly.is_valid or candidate_poly.area<=0:continue
        boundary=_boundary(candidate_poly);source_ids=_source_ids(candidate_poly,topology)
        ident=_identifier('scope-',{'boundary':boundary,'selected':sorted(scope_edge_ids),'supplements':supplements})
        candidates.append({'id':ident,'boundary':boundary,'bounds_mm':list(candidate_poly.bounds),
            'area_mm2':candidate_poly.area,'source_edge_ids':source_ids,'boundary_source_edge_ids':source_ids})
        polygons[ident]=candidate_poly
    result['scope_candidates']=sorted(candidates,key=lambda c:c['id'])
    if not candidates:
        feedback.append({'code':'SOURCE_HOLE_OUTSIDE_SELECTION' if known_holes else 'SCOPE_TOPOLOGY_INVALID',
            'message':'所选面与明确源孔洞不兼容，或未形成有效范围；不会删除孔洞。'})
        return result
    if scope_candidate_id is not None and scope_candidate_id not in polygons:
        feedback.append({'code':'SCOPE_CANDIDATE_INVALID','message':'候选ID不属于当前源边及补边，请重新预览。'})
        return result
    if len(candidates)>1 and scope_candidate_id is None:
        feedback.append({'code':'SCOPE_SELECTION_REQUIRED','message':'存在多个独立有效区域，请明确选择规划范围；不会自动取最大面。'})
        return result
    chosen=next(c for c in candidates if c['id']==scope_candidate_id) if scope_candidate_id else candidates[0]
    result.update(valid=True,boundary=chosen['boundary'],bounds_mm=chosen['bounds_mm'],
        scope_candidate_id=chosen['id'],boundary_source_edge_ids=chosen['boundary_source_edge_ids'],
        source_edge_usage=_usage(polygons[chosen['id']],topology))
    return result
