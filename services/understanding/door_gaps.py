"""CAD-supported opening candidates; geometry alone never proves a door.

Association limits below do not snap vertices or repair gaps. No file I/O,
reference coordinates, source mutation, or inference of a store exterior.
"""
from collections import defaultdict
import hashlib
import json
import math
import re

from shapely import node, normalize
from shapely.geometry import LineString, MultiLineString, Point, box

POLICY='M07-cad-supported-portals-v1'
MAX_PORTAL_SPAN_MM=4000.0
TEXT_ASSOCIATION_MM=1500.0
NUMERIC_LOOKUP_MM=1e-7  # Attribute GEOS output to parents; never move a vertex.


def is_door_name(value):
    return bool(re.search(r'(^|[-_\s])(DOOR|ENTRANCE|EXIT)([-_\s]|$)|^(门|入口|出口|出入口)$',str(value).strip(),re.I))


def _id(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()[:20]


def find_door_gaps(edges,texts,door_markers=()):
    """Find supported gaps near explicit door words or CAD door objects."""
    usable=[e for e in edges if (e.get('role')=='WALL' or (e.get('role')=='SCOPE_CANDIDATE' and e.get('portal_support')))
            and e['start_mm']!=e['end_mm']]
    if not usable:return []
    source=[LineString([e['start_mm'],e['end_mm']]) for e in usable]
    network=node(MultiLineString(source))
    incident=defaultdict(list)
    for line in network.geoms:
        a,b=tuple(line.coords[0]),tuple(line.coords[-1])
        incident[a].append(b);incident[b].append(a)
    endpoints=sorted(p for p,neighbors in incident.items() if len(neighbors)==1)
    markers=[dict(t,kind='CAD_DOOR_TEXT') for t in texts if is_door_name(t.get('text',''))]+list(door_markers)
    proposals=[]
    for i,a in enumerate(endpoints):
        for b in endpoints[i+1:]:
            gap=LineString([a,b]);length=gap.length
            if not 0<length<=MAX_PORTAL_SPAN_MM:continue
            intersections=gap.intersection(network)
            if intersections.length>0:continue
            points=list(intersections.geoms) if hasattr(intersections,'geoms') else [intersections]
            if any(p.geom_type=='Point' and not (p.equals(Point(a)) or p.equals(Point(b))) for p in points):continue
            evidence=[]
            for marker in markers:
                kind=marker['kind']
                if kind=='CAD_DOOR_LINE':
                    match=gap.equals(LineString([marker['start_mm'],marker['end_mm']]))
                    detail={'kind':kind,'handle':marker['handle'],'exact_span_match':match}
                elif kind=='CAD_DOOR_BLOCK':
                    bounds=marker['bounds_mm'];region=box(*bounds)
                    match=region.covers(Point(a)) and region.covers(Point(b))
                    detail={'kind':kind,'handle':marker['handle'],'block_name':marker.get('name'),'bounds_mm':bounds}
                else:
                    position=Point(marker['position_mm']);distance=position.distance(gap)
                    match=distance<=TEXT_ASSOCIATION_MM and min(position.distance(Point(a)),position.distance(Point(b)))<=TEXT_ASSOCIATION_MM
                    detail={'kind':kind,'handle':marker['handle'],'text':marker.get('text'),
                            'position_mm':marker['position_mm'],'distance_mm':distance}
                if match:evidence.append(detail)
            if not evidence:continue
            evidence.sort(key=lambda item:json.dumps(item,sort_keys=True,separators=(',',':')))
            direction=((b[0]-a[0])/length,(b[1]-a[1])/length)
            def away(p,toward):
                q=incident[p][0];distance=math.dist(p,q)
                return sum((q[k]-p[k])/distance*toward[k] for k in (0,1))<-.999
            collinear=away(a,direction) and away(b,tuple(-v for v in direction))
            parents=sorted(e['edge_id'] for e,line in zip(usable,source)
                           if min(line.distance(Point(a)),line.distance(Point(b)))<=NUMERIC_LOOKUP_MM)
            proposals.append({'id':'door-gap-'+_id([a,b,sorted(e['handle'] for e in evidence)]),
                'start_mm':list(a),'end_mm':list(b),'span_mm':length,'source_edge_ids':parents,
                'evidence':evidence,'collinear_wall_continuation':collinear,'confidence':'AMBIGUOUS',
                'role':'DOOR_GAP_CANDIDATE','is_barrier':False})
    for candidate in proposals:
        supported={e['handle'] for e in candidate['evidence']}
        competing=[p for p in proposals if p['id']!=candidate['id'] and supported.intersection(e['handle'] for e in p['evidence'])]
        exact_object=any(e.get('exact_span_match') or e['kind']=='CAD_DOOR_BLOCK' for e in candidate['evidence'])
        high=not competing and (candidate['collinear_wall_continuation'] or exact_object)
        candidate['confidence']='HIGH' if high else 'AMBIGUOUS'
        candidate['reason']=('唯一门证据与墙端点几何一致，自动加入规划边界逻辑闭合；不会成为墙。' if high else
            '门证据附近存在竞争开口或墙方向不唯一；请只确认这一局部开口是否为门，不需重画范围。')
        candidate['competing_candidate_ids']=sorted(p['id'] for p in competing)
        candidate['policy']=POLICY
    return sorted(proposals,key=lambda p:p['id'])


def portal_geometry(candidates,selected_ids,clearance_mm):
    lookup={c['id']:c for c in candidates}
    if len(set(selected_ids))!=len(selected_ids) or set(selected_ids)-lookup.keys():
        raise ValueError('门开口选择不属于当前源和已验证几何；请重新选择图上仍有效的局部候选。')
    adopted=[c for c in candidates if c['confidence']=='HIGH' or c['id'] in selected_ids]
    logical=[];areas=[]
    for candidate in adopted:
        identifier=candidate['id'];span=LineString([candidate['start_mm'],candidate['end_mm']])
        for prior in logical:
            if identifier in lookup[prior['candidate_id']]['competing_candidate_ids']:
                raise ValueError('同一门证据对应多个竞争开口，不能同时采用；请保留实际的局部开口。')
        logical.append({'id':identifier,'candidate_id':identifier,'start_mm':candidate['start_mm'],
            'end_mm':candidate['end_mm'],'kind':'PORTAL_CLOSURE','is_barrier':False,
            'automatic':candidate['confidence']=='HIGH','evidence':candidate['evidence'],
            'source_edge_ids':candidate['source_edge_ids']})
        # A design clearance zone; this is not a measured door depth or wall.
        region=normalize(span.buffer(clearance_mm/2,cap_style='flat'))
        areas.append({'id':'portal-access-'+identifier,'boundary_mm':[list(p) for p in region.exterior.coords],'holes_mm':[]})
    return logical,areas
