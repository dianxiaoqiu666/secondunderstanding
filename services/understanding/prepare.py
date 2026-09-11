"""Explicit CAD facts run automatically; humans supply only missing facts.

Local closure is a source-endpoint option, never automatic wall inference.
Uploaded evidence and approved business inputs remain server-owned.
"""
import io
import hashlib
import hmac
import json
import math
import re
from pathlib import Path
from typing import Literal
from uuid import uuid4

import ezdxf
from ezdxf import disassemble
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import Field, field_validator
from shapely.geometry import Polygon

from shared.contracts import Model, Business, Exclusion, PolygonData, Source, Wall, Point
from shared.scope_tolerance import normalize_adopted_scope, ScopeFormatError
from services.understanding.scope_review import drawing_edges,preview_scope,bounds_for_edges,node_linework
from services.understanding.store_scope import review_store_scope
from services.understanding.door_gaps import find_door_gaps,portal_geometry,is_door_name,POLICY as PORTAL_POLICY
from shared.planning_v2 import Confirmation, PlanningSpace, PlanRequest, PlanningRules
from shared.confirmation import REVIEW_FIELDS, sign_request, verify_request, _key

ROOT=Path(__file__).resolve().parents[2]
RUNTIME=ROOT/'runtime/understanding_v2'
router=APIRouter()
MAX_CAD_BYTES=25*1024*1024
PARSER_POLICY_VERSION='M11-adopted-scope-format-v1'
SCOPE_POLICY_VERSION='M08-store-perimeter-intent-v1'
LOCAL_GAP_MAX_MM=200.0
LOCAL_GAP_MAX_RATIO=0.01
ENTRANCE_LAYERS={'ENTRANCE','ENTRANCES','入口'}


class InputError(ValueError):
    def __init__(self,code,message):
        self.code,self.message=code,message
        super().__init__(message)


def fail(code,message):
    raise InputError(code,message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def _helpers():
    # Lazy import permits both `import app` and `import prepare` even when app
    # includes this router. Shared safe helpers are called only after import.
    from services.understanding import app
    return app


def _helper(name,*args):
    helpers=_helpers()
    try: return getattr(helpers,name)(*args)
    except helpers.InputError as exc: fail(exc.code,exc.message)


def point(value): return _helper('point',value)
def check_plane(entity): return _helper('check_plane',entity)
def polygon_data(polygon): return _helper('polygon_data',polygon)
def hatch_polygons(entity): return _helper('hatch_polygons',entity)
def validate_raw_tags(text): return _helper('validate_raw_tags',text)
def load_business(): return _helper('load_business')


class Resolution(Model):
    issue_id: str
    option_id: str


class ResolveBody(Model):
    prepared_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    resolutions: list[Resolution]
    user_confirmed: Literal[True]

    @field_validator('user_confirmed',mode='before')
    @classmethod
    def explicit_boolean_confirmation(cls,value):
        if value is not True: raise ValueError('user_confirmed must be the JSON boolean true')
        return value


class ConfirmBody(ResolveBody):
    resolutions: list[Resolution] = Field(default_factory=list)
    boundary: PolygonData | None = None
    entrances: list[Exclusion] | None = None
    exclusions: list[Exclusion] = Field(default_factory=list)
    reviewed_fields: list[str] = Field(default_factory=list)
    scope_edge_ids: list[str] = Field(default_factory=list)
    supplemental_edges: list['SupplementalEdge'] = Field(default_factory=list)
    scope_candidate_id: str | None = None
    portal_closure_ids: list[str] | None = None
    perimeter_edge_ids: list[str] | None = None


class SupplementalEdge(Model):
    id: str = Field(min_length=1,max_length=100)
    start_mm: Point
    end_mm: Point
    kind: Literal['HUMAN_BOUNDARY_SUPPLEMENT'] = 'HUMAN_BOUNDARY_SUPPLEMENT'


class PreviewBody(Model):
    prepared_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    resolutions: list[Resolution] = Field(default_factory=list)
    boundary: PolygonData | None = None
    entrances: list[Exclusion] | None = None
    exclusions: list[Exclusion] = Field(default_factory=list)
    scope_edge_ids: list[str] = Field(default_factory=list)
    supplemental_edges: list[SupplementalEdge] = Field(default_factory=list)
    revoked_issue_ids: list[str] = Field(default_factory=list)
    scope_candidate_id: str | None = None
    portal_closure_ids: list[str] | None = None
    perimeter_edge_ids: list[str] | None = None


ConfirmBody.model_rebuild()


def _strict_polygon(data,label):
    for ring in [data.boundary_mm,*data.holes_mm]:
        if len(ring)<4 or ring[0]!=ring[-1]:
            fail('INVALID_CONFIRMED_GEOMETRY',f'{label} 的范围和孔洞必须显式闭合。')
    outer=Polygon(data.boundary_mm)
    if outer.is_empty or not outer.is_valid or outer.area<=0:
        fail('INVALID_CONFIRMED_GEOMETRY',f'{label} 外环无效或自交；不会自动修复。')
    holes=[]
    for ring in data.holes_mm:
        hole=Polygon(ring)
        if (hole.is_empty or not hole.is_valid or hole.area<=0 or not outer.contains(hole)
            or outer.boundary.intersects(hole.boundary) or any(hole.intersects(other) for other in holes)):
            fail('INVALID_CONFIRMED_HOLE',f'{label} 孔洞必须严格位于外环内且互不相交、接触。')
        holes.append(hole)
    polygon=Polygon(data.boundary_mm,data.holes_mm)
    if not polygon.is_valid: fail('INVALID_CONFIRMED_GEOMETRY',f'{label} 拓扑无效。')
    return polygon


def _polyline(entity):
    check_plane(entity)
    if entity.dxftype()=='LWPOLYLINE':
        vertices=list(entity.get_points())
        if any(v[4]!=0 or v[2]!=0 or v[3]!=0 for v in vertices) or entity.dxf.const_width!=0:
            fail('CURVED_OR_WIDE_POLYLINE_UNSUPPORTED','带弧或实体宽度的多段线须先转换为明确二维轮廓。')
        return [point(v[:2]) for v in vertices],bool(entity.closed)
    if not entity.is_2d_polyline:
        fail('NON_PLANAR_GEOMETRY','仅支持二维 POLYLINE。')
    if any(v.dxf.get('bulge',0)!=0 or v.dxf.get('start_width',0)!=0 or v.dxf.get('end_width',0)!=0 for v in entity.vertices):
        fail('CURVED_OR_WIDE_POLYLINE_UNSUPPORTED','POLYLINE 含弧或实体宽度。')
    return [point(v.dxf.location) for v in entity.vertices],bool(entity.is_closed)


def _reference_hashes():
    try: classification=(ROOT/'DATA_CLASSIFICATION.md').read_text(encoding='utf-8-sig')
    except OSError: fail('SOURCE_CLASSIFICATION_MISSING','项目数据分类清单缺失或不可读。')
    return {sha for line in classification.splitlines() if '| REFERENCE_ONLY |' in line
            for sha in re.findall(r'\b[0-9a-f]{64}\b',line)}


def _decompose_insert(entity,doc):
    """Validate block graph, then let ezdxf apply block transforms recursively."""
    def inspect_block(insert,ancestors,door_display=False):
        name=insert.dxf.name
        door_display=door_display or is_door_name(name) or is_door_name(insert.dxf.layer)
        if name in ancestors or len(ancestors)>32:
            fail('INSERT_RECURSION_UNSUPPORTED','块引用循环或嵌套超过32层。')
        block=doc.blocks.get(name)
        if block is None or block.block.is_xref:
            fail('EXTERNAL_BLOCK_UNSUPPORTED','缺失或外部参照块不能完整解释。')
        if insert.has_extension_dict and 'ACAD_FILTER' in insert.get_extension_dict():
            fail('CLIPPED_INSERT_UNSUPPORTED','含 XCLIP 的块不能安全展开。')
        for child in block:
            if child.dxftype()=='INSERT': inspect_block(child,(*ancestors,name),door_display)
            elif door_display and child.dxftype() in {'ARC','CIRCLE','ELLIPSE','SPLINE'}:
                # ezdxf expands door symbols for display/evidence only. Curves
                # never enter exact source edges or the physical wall network.
                check_plane(child)
            elif child.dxftype() not in {'LINE','LWPOLYLINE','POLYLINE','HATCH','TEXT','MTEXT','ATTRIB','ATTDEF'}:
                fail('UNSUPPORTED_BLOCK_ENTITY',f'块内 {child.dxftype()} 不能安全展开，必须先转换为支持的二维几何。')
    inspect_block(entity,())
    values=list(disassemble.recursive_decompose([entity]))
    if not values: fail('EMPTY_INSERT_UNSUPPORTED','块引用没有可读取的二维证据。')
    return values


def _door_support_edges(drawing,wall_ids=()):
    open_scopes={p['handle'] for p in drawing['polylines'] if p['role']=='SCOPE_CANDIDATE' and not p['closed']}
    return [dict(e,role='WALL' if e['edge_id'] in wall_ids else e['role'],portal_support=e['display_handle'] in open_scopes)
            for e in drawing['edges']]


def _door_block_groups(pieces):
    """Group transformed symbol pieces by actual named INSERT ancestry."""
    from ezdxf import bbox
    groups={};members=set()
    for piece in pieces:
        ancestors=[];cursor=getattr(piece,'source_block_reference',None)
        while cursor is not None:
            ancestors.append(cursor);cursor=getattr(cursor,'source_block_reference',None)
        doors=[i for i,insert in enumerate(ancestors)
               if is_door_name(insert.dxf.name) or is_door_name(insert.dxf.layer)]
        if not doors:continue
        index=max(doors);insert=ancestors[index]
        lineage=[]
        for parent in reversed(ancestors[index:]):
            origin=getattr(parent,'origin_of_copy',None) or parent
            lineage.append(str(origin.dxf.handle))
        key='/'.join(lineage)
        groups.setdefault(key,{'name':insert.dxf.name,'pieces':[]})['pieces'].append(piece)
        members.add(id(piece))
    markers=[]
    for handle,group in sorted(groups.items()):
        extent=bbox.extents(group['pieces'])
        if extent.has_data:
            low,high=point(extent.extmin),point(extent.extmax)
            markers.append({'kind':'CAD_DOOR_BLOCK','handle':handle,'name':group['name'],
                            'bounds_mm':[low[0],low[1],high[0],high[1]]})
    return members,markers


def _cad_evidence(data):
    helpers=_helpers()
    SCOPE_LAYERS,WALL_LAYERS,HOLE_LAYERS,EXCLUSION_LAYERS=(helpers.SCOPE_LAYERS,helpers.WALL_LAYERS,
                                                        helpers.HOLE_LAYERS,helpers.EXCLUSION_LAYERS)
    try:
        text=data.decode('utf-8-sig')
        validate_raw_tags(text)
        doc=ezdxf.read(io.StringIO(text,newline=None))
    except InputError: raise
    except Exception as exc:
        fail('CAD_PARSE_FAILED',f'无法读取 DXF；请使用 UTF-8 DXF 2007 或更新格式。({type(exc).__name__})')
    if doc.units!=4: fail('UNITS_NOT_MM','CAD 必须明确设置 INSUNITS=4（毫米）。')
    audit=doc.audit()
    if audit.has_errors: fail('CAD_AUDIT_FAILED','DXF 结构审计失败。')
    fixes=[{'code':int(f.code),'message':f.message} for f in audit.fixes]
    # Annotation-only fixes are disclosed for every source, never selected by
    # file hash. Geometric repairs are always rejected.
    if any(not ((f['code']==202 and 'PLOTSETTINGS' in f['message']) or
                (f['code']==207 and 'DIMSTYLE' in f['message'])) for f in fixes):
        fail('CAD_GEOMETRY_REPAIR_REQUIRED','DXF 需要非注记修复，请在 CAD 软件中修复后重新导出。')
    drawing={'lines':[],'polylines':[],'texts':[],'bounds_mm':[],'door_markers':[]}
    walls=[];exclusions=[];entrances=[];holes=[];scope_candidates=[];open_scopes=[];warnings=[];source_chains=[]
    explicit_scope_sources=[];scope_format_audit=[];scope_format_failures=[]
    unknown=set();all_points=[];scope_count=0
    for original in doc.modelspace():
        kind=original.dxftype()
        if kind=='DIMENSION':
            warnings.append(f'尺寸标注 {original.dxf.handle} 仅为辅助注记，不作为墙或边界。')
            continue
        pieces=_decompose_insert(original,doc) if kind=='INSERT' else [original]
        door_members,door_markers=_door_block_groups(pieces) if kind=='INSERT' else (set(),[])
        drawing['door_markers'].extend(door_markers)
        for index,entity in enumerate(pieces):
            door_block=id(entity) in door_members
            etype=entity.dxftype()
            source=getattr(entity,'source_of_copy',None)
            child_handle=source.dxf.handle if source is not None else entity.dxf.handle
            handle=(f'{original.dxf.handle}/{child_handle or "virtual"}/{index}' if kind=='INSERT' else str(original.dxf.handle))
            layer=entity.dxf.get('layer','0')
            chain=[]
            if kind=='INSERT':
                cursor=entity
                while cursor is not None:
                    origin=getattr(cursor,'origin_of_copy',None)
                    owner_handle=origin.dxf.handle if origin is not None else cursor.dxf.handle
                    if owner_handle: chain.append(owner_handle)
                    if layer=='0': layer=cursor.dxf.get('layer','0')
                    cursor=getattr(cursor,'source_block_reference',None)
                chain=list(reversed(chain))
                if layer=='0': layer=original.dxf.layer
            upper=layer.upper()
            role=('WALL' if upper in WALL_LAYERS else 'SCOPE_CANDIDATE' if upper in SCOPE_LAYERS
                  else 'HOLE' if upper in HOLE_LAYERS else 'EXCLUSION' if upper in EXCLUSION_LAYERS
                  else 'ENTRANCE' if upper in ENTRANCE_LAYERS
                  else 'UNCLASSIFIED')
            if kind=='INSERT':
                source_chains.append({'handle':handle,'source_chain':chain or [original.dxf.handle,child_handle],
                                      'layer':layer,'transform':'ezdxf.disassemble.recursive_decompose'})
            if etype in {'TEXT','MTEXT','ATTRIB','ATTDEF'}:
                position=point(entity.dxf.insert)
                value=entity.plain_text() if etype=='MTEXT' else entity.dxf.text
                drawing['texts'].append({'position_mm':position,'text':value,'handle':handle,'layer':layer})
                all_points.append(position)
                continue
            door_geometry=door_block or (role=='UNCLASSIFIED' and is_door_name(layer))
            if door_geometry and etype in {'LINE','LWPOLYLINE','POLYLINE','ARC','CIRCLE','ELLIPSE','SPLINE'}:
                check_plane(entity)
                if etype=='LINE':
                    start,end=point(entity.dxf.start),point(entity.dxf.end)
                    drawing['lines'].append({'start_mm':start,'end_mm':end,'handle':handle,'layer':layer,
                        'role':'DOOR_SYMBOL','source_type':etype,'source_handle':child_handle or handle,
                        'raw_points_mm':[start,end],'boundary_selectable':False})
                    if not door_block:
                        drawing['door_markers'].append({'kind':'CAD_DOOR_LINE','handle':handle,'start_mm':start,'end_mm':end})
                    all_points.extend([start,end])
                else:
                    from ezdxf.path import make_path
                    coords=[point(p) for p in make_path(entity).flattening(distance=2.0)]
                    drawing['polylines'].append({'points_mm':coords,'closed':False,'handle':handle,'layer':layer,
                        'role':'DOOR_SYMBOL','source_type':etype,'source_handle':child_handle or handle,
                        'raw_points_mm':None,'boundary_selectable':False,'display_approximation_mm':2.0})
                    all_points.extend(coords)
                continue
            if etype=='HATCH':
                for number,polygon in enumerate(hatch_polygons(entity)):
                    zone=Exclusion(id=f'cad-hatch-{handle}-{number}',**polygon_data(polygon))
                    exclusions.append(zone)
                    for ring in [zone.boundary_mm,*zone.holes_mm]:
                        drawing['polylines'].append({'points_mm':ring,'closed':True,'handle':handle,'layer':layer,'role':'EXCLUSION',
                            'source_type':'HATCH','source_handle':child_handle or handle,'raw_points_mm':None if kind=='INSERT' else ring})
                        all_points.extend(ring)
                continue
            if etype=='LINE':
                if tuple(entity.dxf.get('extrusion',(0,0,1)))!=(0,0,1):
                    fail('NON_PLANAR_GEOMETRY','LINE 法向量不是二维正向 Z。')
                start,end=point(entity.dxf.start),point(entity.dxf.end)
                if start==end: fail('DEGENERATE_LINE','CAD 含零长度线段。')
                origin=getattr(entity,'origin_of_copy',None) or entity
                drawing['lines'].append({'start_mm':start,'end_mm':end,'handle':handle,'layer':layer,'role':role,
                    'source_type':etype,'source_handle':origin.dxf.handle or handle,
                    'raw_points_mm':[point(origin.dxf.start),point(origin.dxf.end)]})
                all_points.extend([start,end])
                if role=='WALL': walls.append(Wall(id=f'cad-wall-{handle}',start_mm=start,end_mm=end))
                elif role in {'HOLE','EXCLUSION','ENTRANCE'}:
                    fail('OPEN_CONSTRAINT_UNSUPPORTED','孔洞或禁放区域图层含不能闭合为面的 LINE。')
                elif role!='CONSTRUCTION': unknown.add(layer)
                continue
            if etype in {'LWPOLYLINE','POLYLINE'}:
                coords,closed=_polyline(entity)
                if len(coords)<2: fail('DEGENERATE_POLYLINE','多段线顶点不足。')
                ring=coords+[coords[0]] if closed and coords[-1]!=coords[0] else coords
                origin=getattr(entity,'origin_of_copy',None) or entity
                raw_points,raw_closed=_polyline(origin)
                if raw_closed and raw_points[-1]!=raw_points[0]:raw_points=[*raw_points,raw_points[0]]
                drawing['polylines'].append({'points_mm':ring,'closed':closed,'handle':handle,'layer':layer,'role':role,
                    'source_type':etype,'source_handle':origin.dxf.handle or handle,'raw_points_mm':raw_points})
                all_points.extend(ring)
                if role=='WALL':
                    for number,(start,end) in enumerate(zip(ring,ring[1:])):
                        if start==end: fail('WALL_DEGENERATE','墙多段线含零长度边。')
                        walls.append(Wall(id=f'cad-wall-{handle}-{number}',start_mm=start,end_mm=end))
                elif role in {'SCOPE_CANDIDATE','HOLE','EXCLUSION','ENTRANCE'}:
                    if role=='SCOPE_CANDIDATE':
                        scope_count+=1
                        explicit_scope_sources.append({'handle':handle,'layer':layer,'coords':coords,
                                                       'closed':closed,'source_type':etype})
                        if not closed:open_scopes.append({'handle':handle,'points_mm':coords})
                        continue
                    if not closed:
                        fail('OPEN_CONSTRAINT_UNSUPPORTED','孔洞和禁放区域必须闭合。')
                    try: polygon=_strict_polygon(PolygonData(boundary_mm=ring),f'实体 {handle}')
                    except InputError:
                        raise
                    if role=='HOLE': holes.append(polygon_data(polygon)['boundary_mm'])
                    elif role=='ENTRANCE': entrances.append(Exclusion(id=f'cad-entrance-{handle}',**polygon_data(polygon)))
                    else: exclusions.append(Exclusion(id=f'cad-exclusion-{handle}',**polygon_data(polygon)))
                elif role!='CONSTRUCTION': unknown.add(layer)
                continue
            fail('UNSUPPORTED_SPATIAL_ENTITY',f'实体 {handle} ({etype}) 无法安全显示或解释，请转换为支持的二维几何。')
    if not all_points: fail('CAD_DRAWING_EMPTY','CAD 没有可显示的二维图形。')
    for index,ring in enumerate(holes):
        if any(Polygon(ring).intersects(Polygon(previous)) for previous in holes[:index]):
            fail('CAD_HOLES_INTERSECT','CAD 显式孔洞互相接触或重叠，必须先修复输入。')
    drawing['bounds_mm']=[min(p[0] for p in all_points),min(p[1] for p in all_points),max(p[0] for p in all_points),max(p[1] for p in all_points)]
    if unknown: warnings.append('未分类图层仅作原图证据，请人工确认其空间含义并补标禁放区：'+', '.join(sorted(unknown)))
    # Format tolerance starts only after unique explicit semantic admission.
    # Original display/source edges are retained; unknown frames and linework
    # never enter this helper, and no closing segment becomes a physical wall.
    if scope_count==1:
        source=explicit_scope_sources[0]
        try:
            polygon,audit=normalize_adopted_scope(source['coords'],layer=source['layer'],
                source_closed=source['closed'],source_type=source['source_type'])
        except ScopeFormatError as error:
            scope_format_failures.append({'handle':source['handle'],'code':error.code,'message':error.message})
            warnings.append(f"规划范围 {source['handle']} 保留待复核：{error.message}")
        else:
            scope_candidates.append((source['handle'],polygon))
            scope_format_audit.append({'handle':source['handle'],**audit})
            if audit['operations']:
                warnings.append(f"明确人工范围 {source['handle']} 已作有限格式整理；原CAD和实体墙不变，孔洞与原坐标审计保留。")
    candidate=None
    if scope_count==1 and len(scope_candidates)==1:
        handle,polygon=scope_candidates[0]
        candidate_holes=[list(r.coords) for r in polygon.interiors]+holes
        candidate_boundary=PolygonData(boundary_mm=list(polygon.exterior.coords),holes_mm=candidate_holes)
        try: polygon=_strict_polygon(candidate_boundary,'CAD 候选范围')
        except InputError:
            warnings.append('CAD 范围与显式孔洞关系无效，须人工重新确认范围；原孔洞必须保留。')
        else:
            # Rings encoded by an explicitly adopted self-touching scope are
            # source holes too; confirmation cannot later remove them.
            holes=candidate_holes
            candidate=PlanningSpace(boundary=PolygonData(**polygon_data(polygon)),barriers=walls,exclusions=exclusions,entrances=entrances,
                source_evidence=[{'kind':'EXPLICIT_CAD_SCOPE','handle':handle,
                    'operations':scope_format_audit[0]['operations'],'scope_format':scope_format_audit[0]}],
                unresolved=[],confirmation=Confirmation()).model_dump(mode='json')
    else:
        warnings.append('原图尚未提供唯一可靠闭合 Planning Scope；保留原线，逻辑开口另列依据，具体缺口见必要事实列表。')
    evidence={'parser':'ezdxf','geometry_kernel':'Shapely/GEOS','geometry_repairs':[],
              'scope_format_normalization':scope_format_audit,'scope_format_failures':scope_format_failures,
              'annotation_audit_fixes':fixes,'review_warnings':warnings,'insert_source_chains':source_chains,
              'explicit_holes':holes,'wall_count':len(walls),'fixed_exclusion_count':len(exclusions),
              'barriers':[w.model_dump(mode='json') for w in walls],
              'fixed_exclusions':[z.model_dump(mode='json') for z in exclusions],
              'entrances':[z.model_dump(mode='json') for z in entrances],
              'scope_candidate_count':len(scope_candidates),'scope_entity_count':scope_count,
              'open_scope_candidates':open_scopes,'required_review_fields':[]}
    drawing['edges']=drawing_edges(drawing)
    adopted_handles={row['handle'] for row in scope_format_audit}
    skipped=[e['edge_id'] for e in drawing['edges'] if e['display_handle'] in adopted_handles
             and e['start_mm']==e['end_mm']]
    evidence['duplicate_scope_point_edge_ids']=skipped
    drawing['edges']=[e for e in drawing['edges'] if e['edge_id'] not in skipped]
    automatic,access=portal_geometry(find_door_gaps(_door_support_edges(drawing),drawing['texts'],drawing['door_markers']),[],load_rules().aisle_width_mm)
    evidence['automatic_portal_closures']=automatic
    # The explicit open Planning Scope supplies boundary intent. Only an exact,
    # independently supported door span may close it automatically.
    if candidate is None and scope_count==1 and len(open_scopes)==1:
        coords=open_scopes[0]['points_mm']
        matching=[p for p in automatic if {tuple(p['start_mm']),tuple(p['end_mm'])}=={tuple(coords[0]),tuple(coords[-1])}]
        if len(matching)==1:
            polygon=_strict_polygon(PolygonData(boundary_mm=[*coords,coords[0]],holes_mm=holes),'明确范围的门开口逻辑闭合')
            candidate=PlanningSpace(boundary=PolygonData(**polygon_data(polygon)),barriers=walls,
                exclusions=exclusions,entrances=entrances,source_evidence=[{'kind':'EXPLICIT_SCOPE_WITH_PORTAL',
                    'handle':open_scopes[0]['handle'],'portal':matching[0]}]).model_dump(mode='json')
    evidence['derived_portal_entrances']=access
    return candidate,evidence,drawing,walls,exclusions,holes


def load_rules():
    return PlanningRules()


def _canonical(value):
    return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)


def _binding(source,business,rules):
    return digest(_canonical({'source_sha256':source.sha256,'business':business.model_dump(mode='json'),
                             'rules':rules.model_dump(mode='json'),'policy':PARSER_POLICY_VERSION}).encode('utf-8'))


def _issues_and_choices(candidate,evidence,drawing):
    issues=[];choices={}
    if candidate is None:
        local=None
        opens=evidence['open_scope_candidates']
        if evidence['scope_entity_count']==1 and len(opens)==1:
            item=opens[0];coords=item['points_mm'];gap=math.dist(coords[-1],coords[0])
            total=sum(math.dist(a,b) for a,b in zip(coords,coords[1:]))
            if total>0 and gap<=LOCAL_GAP_MAX_MM and gap<=LOCAL_GAP_MAX_RATIO*total:
                try:
                    polygon=_strict_polygon(PolygonData(boundary_mm=[*coords,coords[0]] if coords[-1]!=coords[0] else coords,
                                                       holes_mm=evidence['explicit_holes']),'源范围局部端点候选')
                except InputError: pass
                else:
                    issue_id='scope-gap-'+item['handle']
                    local={'id':issue_id,'code':'LOCAL_SCOPE_GAP','field':'boundary','requires_geometry':False,
                        'message':'明确 Planning Scope 只有一个短端点缺口，请选择是否连接原图显示的这两个端点。',
                        'options':[{'id':'close-source-endpoints','label':'连接这两个源端点','source_handle':item['handle'],
                                    'start_mm':coords[-1],'end_mm':coords[0],'endpoints_mm':[coords[-1],coords[0]],'gap_mm':gap}]}
                    choices[issue_id]={'close-source-endpoints':{'boundary':polygon_data(polygon),'field':'boundary'}}
        issues.append(local or {'id':'boundary-missing','code':'BOUNDARY_REQUIRED','field':'boundary','requires_geometry':True,
            'message':'缺少唯一可靠的实际门店边界；现有源框没有可安全提示的局部补线候选。请提供真实范围。','options':[]})
    if not evidence['entrances']:
        issues.append({'id':'entrance-missing','code':'ENTRANCE_REQUIRED','field':'entrances','requires_geometry':True,
            'message':'图中没有明确入口区域，只需补充入口位置；已有有效范围不需要重画。','options':[]})
    groups={}
    for entity in [*drawing['lines'],*drawing['polylines']]:
        if entity['role']=='UNCLASSIFIED': groups.setdefault((entity['layer'],entity['handle']),[]).append(entity)
    for (layer,entity_handle),entities in sorted(groups.items()):
        issue_id='unclassified-'+digest((layer+'|'+entity_handle).encode('utf-8'))[:12]
        walls=[];zones=[];all_closed=True
        for entity in entities:
            points=entity.get('points_mm',[entity.get('start_mm'),entity.get('end_mm')])
            for index,(start,end) in enumerate(zip(points,points[1:])):
                if start!=end: walls.append(Wall(id=f"classified-wall-{entity['handle']}-{index}",start_mm=start,end_mm=end).model_dump(mode='json'))
            if not entity.get('closed',False): all_closed=False;continue
            try: polygon=_strict_polygon(PolygonData(boundary_mm=points),'未分类源闭合图元')
            except InputError: all_closed=False
            else: zones.append(Exclusion(id=f"classified-zone-{entity['handle']}",**polygon_data(polygon)).model_dump(mode='json'))
        available={'as-construction':{'field':'exclusions','barriers':[],'exclusions':[]}}
        options=[{'id':'as-construction','label':'这些源图元是辅助线，不是实体障碍'}]
        if walls:
            available['as-walls']={'field':'barriers','barriers':walls,'exclusions':[]}
            options.append({'id':'as-walls','label':'这些源线段是墙或固定屏障'})
        if all_closed and zones:
            available['as-exclusions']={'field':'exclusions','barriers':[],'exclusions':zones}
            options.append({'id':'as-exclusions','label':'这些源闭合区域是禁放区'})
        handles=[e['handle'] for e in entities]
        for option in options: option['source_handles']=handles
        issues.append({'id':issue_id,'code':'UNCLASSIFIED_SPATIAL_EVIDENCE','field':'exclusions','requires_geometry':False,
                       'message':f'源实体 {entity_handle}（图层 {layer}）的含义尚未明确，请单独选择。','layer':layer,
                       'source_handles':handles,'options':options})
        choices[issue_id]=available
    evidence['required_review_fields']=sorted({i['field'] for i in issues if i['requires_geometry']})
    for issue in issues:
        handles=issue.get('source_handles',[])
        if not handles and issue['code']=='LOCAL_SCOPE_GAP':
            handles=[o['source_handle'] for o in issue['options']]
        edges=[e for e in drawing['edges'] if e['display_handle'] in handles]
        if issue['field']=='boundary':
            action='选择原实体或已有边链，并预览；只补必要事实。'
            condition='先确定有来源依据的门店外围，再处理局部开口；最终区域有效并保留源孔洞。内部墙分支不必参与外围。'
        elif issue['code']=='UNCLASSIFIED_SPATIAL_EVIDENCE':
            action='查看高亮原图元，选择与实际情况相符的用途；预览通过后点击“应用并检查”。'
            condition='本图元的用途明确且源几何通过校验；其他未完成问题可继续分别处理。'
        else:
            action='点击“标记入口”，在图上标出真实入口区域；结束绘制后预览检查。'
            condition='入口是有效区域，并与实际门店范围有正面积交集。'
        issue.update(source_handles=handles,source_edge_ids=[e['edge_id'] for e in edges],
            bounds_mm=bounds_for_edges(edges) or drawing['bounds_mm'],
            action=action,pass_condition=condition)
    return issues,choices


def _assemble(data,filename,prepared_id,business,rules):
    source=Source(filename=filename,sha256=digest(data))
    candidate,evidence,drawing,walls,exclusions,holes=_cad_evidence(data)
    issues,choices=_issues_and_choices(candidate,evidence,drawing)
    if candidate is not None:
        polygon=_strict_polygon(PolygonData.model_validate(candidate['boundary']),'CAD 明确范围')
        if any(polygon.intersection(Polygon(z['boundary_mm'],z['holes_mm'])).area<=0 for z in evidence['entrances']):
            fail('CAD_ENTRANCE_OUTSIDE_SCOPE','CAD 明确入口与明确范围没有正面积交集，请修复源事实。')
        candidate['unresolved']=[i['code'] for i in issues]
    envelope={'prepared_id':prepared_id,'source':source.model_dump(mode='json'),'candidate_space':candidate,
        'business_summary':{'product_count':business.product_count,'template_count':len(business.templates),
                           'real_sku_physical_placement':'UNAVAILABLE-DUE-TO-MISSING-PHYSICAL-DATA'},
        'evidence':evidence,'drawing':drawing,'status':'REQUIRES_INPUT','readiness':'REQUIRES_INPUT',
        'issues':issues,'plan_request':None}
    return {'envelope':envelope,'business':business.model_dump(mode='json'),'rules':rules.model_dump(mode='json'),
            'source_candidate_space':json.loads(json.dumps(candidate)),
            'barriers':[w.model_dump(mode='json') for w in walls],'fixed_exclusions':[z.model_dump(mode='json') for z in exclusions],
            'explicit_holes':holes,'source_entrances':evidence['entrances'],'choices':choices,
            'binding':_binding(source,business,rules),'policy':PARSER_POLICY_VERSION}


def _portal_data(saved,resolutions=(),portal_closure_ids=None):
    records={r['issue_id']:r['option_id'] for r in saved.get('partial_resolutions',[])}
    records.update({r.issue_id:r.option_id for r in resolutions})
    wall_ids={edge for issue in saved['envelope']['issues'] if records.get(issue['id'])=='as-walls'
              for edge in issue.get('source_edge_ids',[])}
    drawing=saved['envelope']['drawing']
    edges=_door_support_edges(drawing,wall_ids)
    candidates=find_door_gaps(edges,drawing['texts'],drawing.get('door_markers',[]))
    ids=saved.get('portal_closure_ids',[]) if portal_closure_ids is None else portal_closure_ids
    try: logical,entrances=portal_geometry(candidates,ids,saved['rules']['aisle_width_mm'])
    except ValueError as exc:fail('PORTAL_SELECTION_INVALID',str(exc))
    return candidates,logical,entrances,list(ids)


def _refresh_portal_facts(saved,resolutions=(),portal_closure_ids=None):
    """Rebuild dependent scope/entry facts; immutable CAD constraints stay raw."""
    door_candidates,logical,entries,_=_portal_data(saved,resolutions,portal_closure_ids)
    envelope=saved['envelope'];evidence=envelope['evidence'];drawing=envelope['drawing']
    candidate=json.loads(json.dumps(saved.get('source_candidate_space')))
    if candidate and any(e.get('kind')=='EXPLICIT_SCOPE_WITH_PORTAL' for e in candidate.get('source_evidence',[])):
        candidate=None
    opens=evidence['open_scope_candidates']
    if candidate is None and evidence['scope_entity_count']==1 and len(opens)==1:
        coords=opens[0]['points_mm']
        matches=[p for p in logical if {tuple(p['start_mm']),tuple(p['end_mm'])}=={tuple(coords[0]),tuple(coords[-1])}]
        if len(matches)==1:
            polygon=_strict_polygon(PolygonData(boundary_mm=[*coords,coords[0]],holes_mm=saved['explicit_holes']),
                                    '明确范围的当前门开口逻辑闭合')
            candidate=PlanningSpace(boundary=PolygonData(**polygon_data(polygon)),
                barriers=[Wall.model_validate(w) for w in saved['barriers']],
                exclusions=[Exclusion.model_validate(z) for z in saved['fixed_exclusions']],
                source_evidence=[{'kind':'EXPLICIT_SCOPE_WITH_PORTAL','handle':opens[0]['handle'],'portal':matches[0]}]).model_dump(mode='json')
    current_entries=list({e['id']:e for e in [*saved['source_entrances'],*entries]}.values())
    if candidate is not None:candidate['entrances']=current_entries
    evidence.update(entrances=current_entries,derived_portal_entrances=entries,automatic_portal_closures=[p for p in logical if p['automatic']])
    issues,choices=_issues_and_choices(candidate,evidence,drawing)
    for issue in issues:
        if issue['code']=='ENTRANCE_REQUIRED' and door_candidates:
            issue['message']='原图已有门证据，但对应不止一种开口解释；只需明确这一局部门口。'
            issue['action']='先点击“定位门候选”查看编号与端点；对符合现场的候选选择“这是门”，预览后点击“应用并检查”。无需重新画外围。'
            issue['pass_condition']='只选与实际相符、无竞争冲突的当前候选；逻辑闭合不成为墙。局部事实可先保存，范围齐全后自动继续设计。'
            issue['source_edge_ids']=sorted({edge for c in door_candidates for edge in c['source_edge_ids']})
            issue['source_handles']=sorted({e['display_handle'] for e in drawing['edges'] if e['edge_id'] in issue['source_edge_ids']})
            issue['bounds_mm']=bounds_for_edges([e for e in drawing['edges'] if e['edge_id'] in issue['source_edge_ids']])
    envelope.update(candidate_space=candidate,issues=issues)
    saved['choices']=choices
    return saved


def _scope_supplements(supplements,logical):
    return [*supplements,*[{k:edge[k] for k in ('id','start_mm','end_mm','kind')} for edge in logical]]


def _store_review(saved,resolutions,scope_edge_ids=(),supplemental_edges=(),scope_candidate_id=None,
                  perimeter_edge_ids=(),logical=()):
    drawing=saved['envelope']['drawing']
    records={r['issue_id']:r['option_id'] for r in saved.get('partial_resolutions',[])}
    records.update({r.issue_id:r.option_id for r in resolutions})
    wall_ids={e['edge_id'] for e in drawing['edges'] if e['role'] in {'WALL','SCOPE_CANDIDATE'}}
    exclusions=list(saved['fixed_exclusions'])
    for issue in saved['envelope']['issues']:
        option=records.get(issue['id'])
        if option=='as-walls':wall_ids.update(issue.get('source_edge_ids',[]))
        if option=='as-exclusions':exclusions.extend(saved['choices'][issue['id']][option].get('exclusions',[]))
    return review_store_scope(drawing['edges'],list(scope_edge_ids),
        _scope_supplements(list(supplemental_edges),logical),saved['explicit_holes'],scope_candidate_id,
        fixed_exclusions=exclusions,perimeter_edge_ids=list(perimeter_edge_ids or []),analysis_edge_ids=sorted(wall_ids))


def _selections(saved,resolutions,allow_geometry=False,scope_edge_ids=(),supplemental_edges=(),partial=False,
                scope_candidate_id=None,portal_closure_ids=None,perimeter_edge_ids=()):
    supplied={r.issue_id:r.option_id for r in resolutions}
    if len(supplied)!=len(resolutions): fail('DUPLICATE_RESOLUTION','同一事实不能重复选择。')
    supplied={**{r['issue_id']:r['option_id'] for r in saved.get('partial_resolutions',[])},**supplied}
    issues=saved['envelope']['issues']
    candidates,logical,entries,portal_ids=_portal_data(saved,resolutions,portal_closure_ids)
    scoped=set(scope_edge_ids)
    topology=None
    has_scope_work=bool(scoped or supplemental_edges or perimeter_edge_ids or scope_candidate_id)
    if has_scope_work:
        topology=_store_review(saved,resolutions,scope_edge_ids,supplemental_edges,scope_candidate_id,
                               perimeter_edge_ids,logical)
        if not topology['valid']:fail('INVALID_SOURCE_SCOPE',topology['feedback'][0]['message'])
        if not topology['scope_ready'] and not partial:
            fail('PERIMETER_UNFINISHED','所选外围尚未完成；开放工作线组可以保存，但不能作为最终规划区域。')
        scoped=set(topology['boundary_source_edge_ids'])
    scope_issues={i['id'] for i in issues if i['code']=='UNCLASSIFIED_SPATIAL_EVIDENCE'
                  and i.get('source_edge_ids') and set(i['source_edge_ids']).issubset(scoped)}
    if scoped or supplemental_edges:
        scope_issues.update(i['id'] for i in issues if i['field']=='boundary' and not i['requires_geometry'])
    expected={i['id'] for i in issues if not i['requires_geometry']}
    if set(supplied)-expected or (not partial and not (expected-scope_issues).issubset(supplied)):
        fail('RESOLUTION_INCOMPLETE','必须且只能选择当前每个未用作范围的候选事实的一个有效选项。')
    if not allow_geometry and any(i['requires_geometry'] for i in issues):
        fail('GEOMETRY_FACT_REQUIRED','仍缺实际范围或入口，请集中补充这些必要事实。')
    result={'boundary':None,'barriers':[],'exclusions':[],'fields':[],'records':[],
            'scope_edge_ids':list(scope_edge_ids),'supplemental_edges':list(supplemental_edges),'scope_issue_ids':sorted(scope_issues),
            'scope_candidate_id':scope_candidate_id,'portal_closure_ids':portal_ids,'portal_closures':logical,
            'perimeter_edge_ids':list(perimeter_edge_ids or []),
            'door_gap_candidates':candidates,'entrances':entries,'topology':topology}
    if topology and topology['scope_ready']:
        result['boundary']=topology['boundary'];result['fields'].append('boundary')
    if entries and portal_ids and not saved['source_entrances']:result['fields'].append('entrances')
    for issue in issues:
        if issue['requires_geometry'] or issue['id'] not in supplied: continue
        option=supplied[issue['id']]
        choice=saved['choices'][issue['id']].get(option)
        if choice is None: fail('RESOLUTION_OPTION_INVALID','选项不是服务端当前验证的源候选。')
        if 'boundary' in choice: result['boundary']=choice['boundary']
        selected_entity_edges=scoped.intersection(issue.get('source_edge_ids',[]))
        if selected_entity_edges and choice.get('exclusions'):
            fail('PARTIAL_SCOPE_EXCLUSION_CONFLICT','同一闭合实体部分用于范围时，不能再把整个实体声明为禁放区。')
        result['barriers'].extend(choice.get('barriers',[]))
        result['exclusions'].extend(choice.get('exclusions',[]))
        result['fields'].append(choice['field'])
        result['records'].append({'issue_id':issue['id'],'option_id':option})
    return result


def _validate_fixed(req,saved):
    if saved['envelope']['evidence'].get('partial_reuse')=='REJECTED_INVALID_PARTIAL':
        fail('PARTIAL_FACTS_INVALID','局部事实存档损坏，不能复用完整确认缓存。')
    partial_revision=next((e.get('digest') for e in req.space.source_evidence if e.get('kind')=='PARTIAL_FACTS_VERSION'),None)
    if partial_revision!=saved.get('partial_revision'):
        fail('PARTIAL_FACTS_CHANGED','已保存的源事实已更新或撤销，旧完整结果不能复用。')
    polygon=_strict_polygon(req.space.boundary,'标准规划范围')
    expected_format=saved['envelope']['evidence'].get('scope_format_normalization',[])
    actual_format=next((e.get('records') for e in req.space.source_evidence
                        if e.get('kind')=='ADOPTED_SCOPE_FORMAT_NORMALIZATION'),[])
    if actual_format!=expected_format:
        fail('SCOPE_FORMAT_EVIDENCE_CHANGED','明确人工范围的格式整理依据缺失或被改写。')
    proposed=[Polygon(hole) for hole in req.space.boundary.holes_mm]
    if any(not any(Polygon(hole).equals(p) for p in proposed) for hole in saved['explicit_holes']):
        fail('CAD_HOLE_MUST_BE_PRESERVED','CAD 明确孔洞不能删除或改形。')
    actual_walls={w.id:w.model_dump(mode='json') for w in req.space.barriers}
    actual_zones={z.id:z.model_dump(mode='json') for z in req.space.exclusions}
    if len(actual_walls)!=len(req.space.barriers) or len(actual_zones)!=len(req.space.exclusions):
        fail('DUPLICATE_FIXED_ID','固定约束 ID 重复。')
    if any(actual_walls.get(w['id'])!=w for w in saved['barriers']): fail('FIXED_WALL_CHANGED','源墙不能删除或替换。')
    if any(actual_zones.get(z['id'])!=z for z in saved['fixed_exclusions']): fail('FIXED_EXCLUSION_CHANGED','源禁放区不能删除或替换。')
    for zone in [*req.space.exclusions,*req.space.entrances]: _strict_polygon(zone,'区域')
    actual_entries={z.id:z.model_dump(mode='json') for z in req.space.entrances}
    if len(actual_entries)!=len(req.space.entrances): fail('DUPLICATE_ZONE_ID','入口 ID 重复。')
    if any(actual_entries.get(z['id'])!=z for z in saved['source_entrances']): fail('FIXED_ENTRANCE_CHANGED','明确源入口不能删除或替换。')
    if not req.space.entrances: fail('ENTRANCE_REQUIRED','仍缺少明确入口区域。')
    if any(polygon.intersection(Polygon(z.boundary_mm,z.holes_mm)).area<=0 for z in req.space.entrances):
        fail('ENTRANCE_OUTSIDE_SCOPE','入口必须与实际规划范围有正面积交集。')
    records=next((item for item in req.space.source_evidence if item.get('kind')=='NECESSARY_FACTS_RESOLVED'),None)
    selected=_selections(saved,[Resolution.model_validate(r) for r in (records or {}).get('resolutions',[])],allow_geometry=True,
        scope_edge_ids=(records or {}).get('scope_edge_ids',[]),supplemental_edges=(records or {}).get('supplemental_edges',[]),
        scope_candidate_id=(records or {}).get('scope_candidate_id'),portal_closure_ids=(records or {}).get('portal_closure_ids'),
        perimeter_edge_ids=(records or {}).get('perimeter_edge_ids',[]))
    if any(actual_entries.get(z['id'])!=z for z in selected['entrances']):
        fail('PORTAL_ACCESS_CHANGED','门开口的规划通道保护带与当前候选或规则不一致。')
    valid_entry_ids={z['id'] for z in selected['entrances']+saved['source_entrances']}
    if any(z.startswith('portal-access-') and z not in valid_entry_ids for z in actual_entries):
        fail('STALE_PORTAL_ACCESS','旧门候选已失效，不能继续使用其派生入口。')
    portal_record=next((e for e in req.space.source_evidence if e.get('kind')=='PLANNING_PORTALS'),None)
    if portal_record and (portal_record.get('version')!=PORTAL_POLICY or portal_record.get('closures')!=selected['portal_closures']):
        fail('PORTAL_EVIDENCE_CHANGED','门候选依据或解析政策已变化，旧结果不能复用。')
    fields=set(req.space.confirmation.reviewed_fields)
    required={i['field'] for i in saved['envelope']['issues'] if i['requires_geometry']}
    if not required.issubset(fields): fail('SPATIAL_REVIEW_INCOMPLETE','缓存或结果缺少必要事实审核。')
    if any(actual_walls.get(w['id'])!=w for w in selected['barriers']): fail('CLASSIFIED_WALL_CHANGED','已选择的源墙几何不一致。')
    if any(actual_zones.get(z['id'])!=z for z in selected['exclusions']): fail('CLASSIFIED_EXCLUSION_CHANGED','已选择的源禁放区几何不一致。')
    explicit=saved['envelope']['candidate_space']
    expected_boundary=selected['boundary'] or (explicit['boundary'] if explicit else None)
    manual_fields=set((records or {}).get('manual_fields',[]))
    if 'boundary' in manual_fields and any((records or {}).get(k) for k in
            ('scope_edge_ids','supplemental_edges','perimeter_edge_ids','scope_candidate_id')):
        fail('CONFLICTING_SCOPE_INPUT','原图外围选择与手绘边界不能同时作为范围依据。')
    if expected_boundary and 'boundary' not in manual_fields and not polygon.equals(_strict_polygon(PolygonData.model_validate(expected_boundary),'源候选范围')):
        fail('SOURCE_SCOPE_CHANGED','源范围或选定局部端点候选被替换。')
    if req.space.confirmation.state=='AUTO_VALIDATED' and saved['envelope']['issues']:
        fail('UNRESOLVED_AUTO_INPUT','未决输入不能标记为自动有效。')


def _make_request(saved,boundary,entrances,exclusions,selected,reviewed_fields,state,manual_fields=(),sign=True):
    polygon=_strict_polygon(boundary,'标准范围')
    fixed=[Exclusion.model_validate(z) for z in saved['fixed_exclusions']+selected['exclusions']]
    extras=[Exclusion(id='human-'+z.id,**polygon_data(_strict_polygon(z,'补充禁放区'))) for z in exclusions]
    entries=[Exclusion.model_validate(z) for z in saved['source_entrances']]
    for entry in [*entrances,*[Exclusion.model_validate(e) for e in selected.get('entrances',[])]]:
        normalized=Exclusion(id=entry.id,**polygon_data(_strict_polygon(entry,'补充入口')))
        same=next((e for e in entries if e.id==entry.id),None)
        if same is not None and same!=normalized: fail('FIXED_ENTRANCE_CHANGED','不能改写源入口。')
        if same is None: entries.append(normalized)
    source=Source.model_validate(saved['envelope']['source'])
    fields=sorted(set(reviewed_fields)|set(selected['fields']))
    evidence=[{'kind':'UPLOADED_CAD','sha256':source.sha256,'geometry_repairs':[]},
              {'kind':'PARSER_POLICY','version':PARSER_POLICY_VERSION,'binding':saved['binding']},
              {'kind':'SCOPE_TOPOLOGY_POLICY','version':SCOPE_POLICY_VERSION},
              {'kind':'PLANNING_PORTALS','version':PORTAL_POLICY,'closures':selected.get('portal_closures',[]),
               'clearance_mm':saved['rules']['aisle_width_mm'],'clearance_is_design_parameter':True}]
    scope_format=saved['envelope']['evidence'].get('scope_format_normalization',[])
    if scope_format:
        evidence.append({'kind':'ADOPTED_SCOPE_FORMAT_NORMALIZATION','records':scope_format,
                         'applies_to':'source_explicit_scope_candidate','source_cad_modified':False})
    if selected.get('topology'):
        evidence.append({'kind':'DERIVED_BOUNDARY_TOPOLOGY','boundary_source_edge_ids':selected['topology']['boundary_source_edge_ids'],
            'source_edge_usage':selected['topology'].get('source_edge_usage',[]),'scope_candidate_id':selected.get('scope_candidate_id')})
    if 'partial_revision' in saved:
        evidence.append({'kind':'PARTIAL_FACTS_VERSION','digest':saved['partial_revision']})
    if state=='CONFIRMED':
        evidence.append({'kind':'NECESSARY_FACTS_RESOLVED','resolutions':selected['records'],
                         'manual_fields':sorted(manual_fields),'resolved_issue_ids':[i['id'] for i in saved['envelope']['issues']],
                         'scope_edge_ids':selected.get('scope_edge_ids',[]),'supplemental_edges':selected.get('supplemental_edges',[]),
                         'perimeter_edge_ids':selected.get('perimeter_edge_ids',[]),
                         'scope_candidate_id':selected.get('scope_candidate_id'),'portal_closure_ids':selected.get('portal_closure_ids',[])})
    space=PlanningSpace(boundary=PolygonData(**polygon_data(polygon)),
        barriers=[Wall.model_validate(w) for w in saved['barriers']+selected['barriers']],
        exclusions=sorted([*fixed,*extras],key=lambda z:z.id),entrances=sorted(entries,key=lambda z:z.id),
        source_evidence=evidence,unresolved=[],confirmation=Confirmation(state=state,
            confirmed_by='cad-explicit-facts-validator' if state=='AUTO_VALIDATED' else 'local-human-supplement',reviewed_fields=fields))
    req=PlanRequest(source=source,space=space,business=Business.model_validate(saved['business']),rules=PlanningRules.model_validate(saved['rules']))
    _validate_fixed(req,saved)
    return sign_request(req) if sign else req


def _cache_path(saved):
    return RUNTIME/'confirmation_cache'/f"{saved['binding']}.json"


def _validated_cached(saved,path):
    cached=PlanRequest.model_validate_json(path.read_text(encoding='utf-8-sig'))
    verify_request(cached)
    if cached.source.sha256!=saved['envelope']['source']['sha256'] or cached.business.model_dump(mode='json')!=saved['business'] or cached.rules.model_dump(mode='json')!=saved['rules']:
        fail('CACHE_BINDING_MISMATCH','缓存来源、业务或规则已变化。')
    if {'kind':'PARSER_POLICY','version':PARSER_POLICY_VERSION,'binding':saved['binding']} not in cached.space.source_evidence:
        fail('CACHE_POLICY_MISMATCH','缓存解析政策不匹配。')
    if {'kind':'SCOPE_TOPOLOGY_POLICY','version':SCOPE_POLICY_VERSION} not in cached.space.source_evidence:
        fail('CACHE_POLICY_MISMATCH','缓存范围拓扑政策不匹配；仍有效的源实体事实保留。')
    _validate_fixed(cached,saved)
    cached.source=Source.model_validate(saved['envelope']['source'])
    return sign_request(cached)


def _ready(saved):
    _refresh_portal_facts(saved)
    envelope=json.loads(json.dumps(saved['envelope']))
    if not envelope['issues'] and not (saved.get('partial_resolutions') or saved.get('portal_closure_ids')):
        selected=_selections(saved,[])
        req=_make_request(saved,PolygonData.model_validate(envelope['candidate_space']['boundary']),[],[],selected,[],'AUTO_VALIDATED')
        envelope.update(status='READY',readiness='AUTO_VALIDATED',plan_request=req.model_dump(mode='json'))
    else:
        path=_cache_path(saved)
        if path.is_file():
            try: cached=_validated_cached(saved,path)
            except (ValueError,OSError,KeyError,TypeError):
                envelope['evidence']['cache_reuse']='REJECTED_INVALID_CACHE'
            else:
                envelope.update(status='READY',readiness='REUSED_CONFIRMATION',issues=[],plan_request=cached.model_dump(mode='json'))
        if envelope['status']!='READY' and saved.get('partial_resolutions'):
            resolved={r['issue_id'] for r in saved['partial_resolutions']}
            envelope['issues']=[i for i in envelope['issues'] if i['id'] not in resolved]
            if not envelope['issues'] and envelope['candidate_space']:
                selected=_selections(saved,[],allow_geometry=True)
                req=_make_request(saved,PolygonData.model_validate(envelope['candidate_space']['boundary']),[],[],selected,[],'CONFIRMED')
                envelope.update(status='READY',readiness='REUSED_PARTIAL_FACTS',plan_request=req.model_dump(mode='json'))
    if saved.get('partial_resolutions'):
        # Publish revalidated human source semantics even while scope facts are
        # missing. Only this deep-copied envelope changes; source constraints in
        # saved remain the immutable baseline used by final validation.
        selected=_selections(saved,[],allow_geometry=True,partial=True)
        barriers=saved['barriers']+selected['barriers']
        exclusions=saved['fixed_exclusions']+selected['exclusions']
        candidate=envelope['candidate_space'] or {'boundary':None,'entrances':saved['source_entrances']}
        candidate.update(barriers=barriers,exclusions=exclusions)
        envelope['candidate_space']=candidate
        envelope['evidence'].update(barriers=barriers,fixed_exclusions=exclusions,
            source_wall_count=len(saved['barriers']),wall_count=len(barriers),
            source_fixed_exclusion_count=len(saved['fixed_exclusions']),fixed_exclusion_count=len(exclusions))
        choices={r['issue_id']:r['option_id'] for r in selected['records']}
        usage={'selected_scope_edge_ids':[],'supplemental_edges':[],
            'classified_as_walls':[],'classified_as_exclusions':[],'classified_as_construction':[],
            'unresolved_edge_ids':sorted({e for i in envelope['issues'] for e in i.get('source_edge_ids',[])})}
        for issue in saved['envelope']['issues']:
            key={'as-walls':'classified_as_walls','as-exclusions':'classified_as_exclusions',
                'as-construction':'classified_as_construction'}.get(choices.get(issue['id']))
            if key:usage[key].extend(issue.get('source_edge_ids',[]))
        envelope['source_usage']=usage
    portals,logical,entries,portal_ids=_portal_data(saved)
    envelope['door_gap_candidates']=portals
    envelope['portal_closures']=logical
    envelope['drawing']['derived_topology']=node_linework(envelope['drawing']['edges'])
    usage=envelope.setdefault('source_usage',{'selected_scope_edge_ids':[],'supplemental_edges':[]})
    usage.update(portal_closure_ids=portal_ids,scope_candidate_id=None)
    if entries:
        space=envelope['candidate_space'] or {'boundary':None,'barriers':envelope['evidence']['barriers'],
                                               'exclusions':envelope['evidence']['fixed_exclusions']}
        space['entrances']=list({e['id']:e for e in [*saved['source_entrances'],*entries]}.values())
        envelope['candidate_space']=space
        envelope['issues']=[i for i in envelope['issues'] if i['code']!='ENTRANCE_REQUIRED']
    work=saved.get('boundary_work',{})
    scope=_store_review(saved,[],work.get('scope_edge_ids',[]),work.get('supplemental_edges',[]),
                         work.get('scope_candidate_id'),work.get('perimeter_edge_ids',[]),logical)
    for key in ('scope_candidates','local_faces','perimeter_candidates','gap_candidates','scope_ready',
                'derived_topology','diagnostics','boundary_source_edge_ids'):
        envelope[key]=scope[key]
    usage.update({key:work.get(key,[] if key!='scope_candidate_id' else None) for key in
                  ('scope_edge_ids','supplemental_edges','perimeter_edge_ids','scope_candidate_id')})
    if envelope['status']!='READY' and scope['scope_ready']:
        space=envelope['candidate_space'] or {'barriers':envelope['evidence']['barriers'],
            'exclusions':envelope['evidence']['fixed_exclusions'],'entrances':entries}
        space['boundary']=scope['boundary'];envelope['candidate_space']=space
        envelope['issues']=[i for i in envelope['issues'] if i['field']!='boundary']
    elif not (envelope.get('candidate_space') or {}).get('boundary'):
        info=next((f for f in reversed(scope['feedback']) if f['code'].startswith('PERIMETER_') or 'HOLE' in f['code']),None)
        if info:
            for issue in envelope['issues']:
                if issue['field']=='boundary' and issue['code']=='BOUNDARY_REQUIRED':
                    issue.update({k:v for k,v in info.items() if k!='code'})
    if (envelope.get('candidate_space') or {}).get('boundary'):envelope['scope_ready']=True
    if envelope['status']!='READY' and not envelope['issues'] and (envelope.get('candidate_space') or {}).get('boundary'):
        selected=_selections(saved,[],allow_geometry=True,**work)
        fields=set(saved['envelope']['evidence']['required_review_fields'])|set(selected['fields'])
        req=_make_request(saved,PolygonData.model_validate(envelope['candidate_space']['boundary']),[],[],selected,fields,'CONFIRMED')
        envelope.update(status='READY',readiness='REUSED_PARTIAL_FACTS',plan_request=req.model_dump(mode='json'))
    return envelope


def prepare_bytes(data,filename):
    filename=Path(filename.replace('\\','/')).name
    if not filename.lower().endswith('.dxf'): fail('CAD_FORMAT_UNSUPPORTED','当前只支持 DXF，请先将 DWG 导出为 DXF。')
    if not data or len(data)>MAX_CAD_BYTES: fail('CAD_SIZE_INVALID','请上传非空且不超过25MB的 DXF。')
    source=Source(filename=filename,sha256=digest(data))
    if source.sha256 in _reference_hashes(): fail('REFERENCE_INPUT_FORBIDDEN','分类清单中的 REFERENCE_ONLY 文件不能作为生产 CAD 上传。')
    business=load_business();rules=load_rules();prepared_id=uuid4().hex
    try: saved=_assemble(data,filename,prepared_id,business,rules)
    except InputError: raise
    except Exception as exc: fail('CAD_GEOMETRY_INVALID',f'CAD 几何损坏或不受支持。({type(exc).__name__})')
    RUNTIME.mkdir(parents=True,exist_ok=True)
    with (RUNTIME/f'{prepared_id}.dxf').open('xb') as stream: stream.write(data)
    with (RUNTIME/f'{prepared_id}.json').open('x',encoding='utf-8') as stream:
        json.dump(saved,stream,ensure_ascii=False,sort_keys=True,allow_nan=False)
    _restore_partial(saved)
    return _ready(saved)


def _load(prepared_id):
    if not re.fullmatch(r'[0-9a-f]{32}',prepared_id): fail('PREPARED_ID_INVALID','prepared_id 无效。')
    path=RUNTIME/f'{prepared_id}.json'
    if not path.is_file(): fail('PREPARED_NOT_FOUND','找不到已准备的 CAD，请重新上传。')
    try:
        saved=json.loads(path.read_text(encoding='utf-8-sig'))
        source_bytes=(RUNTIME/f'{prepared_id}.dxf').read_bytes()
    except (OSError,ValueError): fail('PREPARED_RECORD_DAMAGED','服务端准备记录缺失或损坏，请重新上传。')
    if digest(source_bytes)!=saved['envelope']['source']['sha256']:
        fail('PREPARED_SOURCE_CHANGED','服务端上传证据已改变，请重新上传。')
    if digest(source_bytes) in _reference_hashes(): fail('REFERENCE_INPUT_FORBIDDEN','当前分类清单不允许此源用于生产。')
    current=_assemble(source_bytes,saved['envelope']['source']['filename'],prepared_id,load_business(),load_rules())
    if current['binding']!=saved.get('binding'):
        fail('PREPARED_CONFIGURATION_CHANGED','内部业务、规则或解析政策已变化，请重新上传以使用当前配置。')
    _restore_partial(current)
    _refresh_portal_facts(current)
    return current


def _partial_path(saved):
    return RUNTIME/'partial_facts'/f"{saved['binding']}.json"


def _merge_boundary_work(body,saved):
    work=saved.get('boundary_work',{})
    provided=set(body.model_fields_set)
    for key in ('perimeter_edge_ids','scope_edge_ids','supplemental_edges','scope_candidate_id'):
        if key not in provided and key in work:
            value=work[key]
            if key=='supplemental_edges':value=[SupplementalEdge.model_validate(e) for e in value]
            setattr(body,key,value)
    if body.perimeter_edge_ids is None:body.perimeter_edge_ids=[]


def _restore_partial(saved):
    path=_partial_path(saved);evidence=saved['envelope']['evidence']
    evidence.update(partial_reuse='NONE',applied_resolutions=[],resolved_fields=[])
    if not path.is_file():return
    try:
        record=json.loads(path.read_text(encoding='utf-8-sig'));payload=record['payload']
        expected=hmac.new(_key(),_canonical(payload).encode('utf-8'),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected,record['signature']):raise ValueError('invalid signature')
        if payload['binding']!=saved['binding'] or payload['policy']!=PARSER_POLICY_VERSION:raise ValueError('binding changed')
        allowed={i['id'] for i in saved['envelope']['issues'] if i['code']=='UNCLASSIFIED_SPATIAL_EVIDENCE'}
        resolutions=[Resolution.model_validate(r) for r in payload['resolutions']]
        if any(r.issue_id not in allowed for r in resolutions):raise ValueError('invalid source issue')
        selected=_selections(saved,resolutions,allow_geometry=True,partial=True)
        portal_ids=payload.get('portal_closure_ids',[])
        portal_changed=bool(portal_ids and payload.get('portal_policy')!=PORTAL_POLICY)
        if portal_changed:portal_ids=[]
        _portal_data(dict(saved,partial_resolutions=selected['records']),portal_closure_ids=portal_ids)
    except (OSError,ValueError,KeyError,TypeError):
        evidence['partial_reuse']='REJECTED_INVALID_PARTIAL';return
    saved['partial_resolutions']=selected['records']
    saved['portal_closure_ids']=portal_ids
    work=payload.get('boundary_work')
    if work:
        try:
            if work['scope_policy']!=SCOPE_POLICY_VERSION:raise ValueError('scope policy changed')
            normalized=PreviewBody.model_validate({'prepared_id':saved['envelope']['prepared_id'],
                **{k:v for k,v in work.items() if k!='scope_policy'}})
            _,logical,_,_=_portal_data(saved)
            checked=_store_review(saved,[],normalized.scope_edge_ids,
                [e.model_dump(mode='json') for e in normalized.supplemental_edges],normalized.scope_candidate_id,
                normalized.perimeter_edge_ids,logical)
            if not checked['valid']:raise ValueError('scope work invalid')
            saved['boundary_work']={k:v for k,v in work.items() if k!='scope_policy'}
            evidence['perimeter_reuse']='REUSED_VALIDATED_BOUNDARY_WORK'
        except (ValueError,KeyError,TypeError):
            evidence['perimeter_reuse']='REJECTED_CHANGED_BOUNDARY_WORK_SOURCE_FACTS_PRESERVED'
    effective=dict(payload,portal_closure_ids=portal_ids,portal_policy=PORTAL_POLICY) if portal_changed else payload
    saved['partial_revision']=digest(_canonical(effective).encode('utf-8'))
    evidence.update(partial_reuse='REUSED_VALIDATED_SOURCE_FACTS',applied_resolutions=selected['records'],resolved_fields=sorted(set(selected['fields'])))
    if portal_changed:evidence['portal_reuse']='REJECTED_CHANGED_PORTAL_POLICY_WALL_FACTS_PRESERVED'


def apply_input(body):
    body=body.model_copy(deep=True) if isinstance(body,PreviewBody) else PreviewBody.model_validate(body)
    preview=preview_input(body)
    if not preview['valid']:fail('PARTIAL_INPUT_INVALID','预览仍含无效几何或选项；不会保存。')
    saved=_preview_saved(body)
    selected=_selections(saved,body.resolutions,allow_geometry=True,partial=True,
        scope_edge_ids=body.scope_edge_ids,supplemental_edges=[e.model_dump(mode='json') for e in body.supplemental_edges],
        scope_candidate_id=body.scope_candidate_id,portal_closure_ids=body.portal_closure_ids,perimeter_edge_ids=body.perimeter_edge_ids)
    allowed={i['id'] for i in saved['envelope']['issues'] if i['code']=='UNCLASSIFIED_SPATIAL_EVIDENCE'}
    records=[r for r in selected['records'] if r['issue_id'] in allowed]
    if records or body.revoked_issue_ids or body.portal_closure_ids is not None or body.perimeter_edge_ids is not None or body.scope_edge_ids or body.supplemental_edges:
        payload={'binding':saved['binding'],'policy':PARSER_POLICY_VERSION,'resolutions':records,
            'portal_closure_ids':selected['portal_closure_ids'],'portal_policy':PORTAL_POLICY,
            'boundary_work':{'scope_policy':SCOPE_POLICY_VERSION,'perimeter_edge_ids':body.perimeter_edge_ids or [],
                'scope_edge_ids':body.scope_edge_ids,'supplemental_edges':[e.model_dump(mode='json') for e in body.supplemental_edges],
                'scope_candidate_id':body.scope_candidate_id}}
        record={'payload':payload,'signature':hmac.new(_key(create=True),_canonical(payload).encode('utf-8'),hashlib.sha256).hexdigest()}
        path=_partial_path(saved);path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
        temporary.write_text(_canonical(record),encoding='utf-8');temporary.replace(path)
    preview['applied_resolutions']=records
    preview['partial_reuse']='SAVED_VALIDATED_SOURCE_FACTS' if records or body.revoked_issue_ids or body.portal_closure_ids is not None else 'NO_SOURCE_SEMANTICS_TO_SAVE'
    return preview


def _preview_saved(body):
    saved=_load(body.prepared_id)
    _merge_boundary_work(body,saved)
    revoked=set(body.revoked_issue_ids)
    existing={r['issue_id'] for r in saved.get('partial_resolutions',[])}
    if len(revoked)!=len(body.revoked_issue_ids) or not revoked.issubset(existing):
        fail('INVALID_FACT_REVOCATION','只能撤销当前已保存的合法源实体事实，且不可重复。')
    if revoked.intersection(r.issue_id for r in body.resolutions):
        fail('CONFLICTING_FACT_REVOCATION','同一次动作不能同时撤销和重设同一事实。')
    saved['partial_resolutions']=[r for r in saved.get('partial_resolutions',[]) if r['issue_id'] not in revoked]
    if body.portal_closure_ids is not None:saved['portal_closure_ids']=body.portal_closure_ids
    return _refresh_portal_facts(saved,body.resolutions,body.portal_closure_ids)


def confirm_prepared(body):
    body=body.model_copy(deep=True) if isinstance(body,ConfirmBody) else ConfirmBody.model_validate(body)
    saved=_load(body.prepared_id)
    _merge_boundary_work(body,saved)
    _refresh_portal_facts(saved,body.resolutions,body.portal_closure_ids)
    fields=set(body.reviewed_fields)
    required={i['field'] for i in saved['envelope']['issues'] if i['requires_geometry']}
    if len(fields)!=len(body.reviewed_fields) or fields-REVIEW_FIELDS or not required.issubset(fields):
        fail('SPATIAL_REVIEW_INCOMPLETE','只需明确解决当前缺失事实，但这些必要项目不能遗漏。')
    selected=_selections(saved,body.resolutions,allow_geometry=True,scope_edge_ids=body.scope_edge_ids,
        supplemental_edges=[e.model_dump(mode='json') for e in body.supplemental_edges],
        scope_candidate_id=body.scope_candidate_id,portal_closure_ids=body.portal_closure_ids,perimeter_edge_ids=body.perimeter_edge_ids)
    if body.boundary is not None and (selected['scope_edge_ids'] or selected['supplemental_edges'] or selected['perimeter_edge_ids'] or body.scope_candidate_id):
        fail('CONFLICTING_SCOPE_INPUT','原边范围选择与手绘边界不能同时提交。')
    explicit=saved['envelope']['candidate_space']
    boundary=body.boundary or (PolygonData.model_validate(selected['boundary']) if selected['boundary'] else None) or (
        PolygonData.model_validate(explicit['boundary']) if explicit else None)
    if boundary is None: fail('BOUNDARY_REQUIRED','仍缺少真实边界几何。')
    manual=[]
    if body.boundary is not None: manual.append('boundary')
    if body.entrances is not None: manual.append('entrances')
    if body.exclusions: manual.append('exclusions')
    if not set(manual).issubset(fields): fail('SPATIAL_REVIEW_INCOMPLETE','主动提交的事实修改必须明确标记对应项目。')
    if not fields and not selected['fields']: fail('NO_NECESSARY_CONFIRMATION','输入已明确，无需生成虚假的人工确认。')
    result=_make_request(saved,boundary,body.entrances or [],body.exclusions,selected,fields,'CONFIRMED',manual)
    _save_confirmation(body.prepared_id,saved,result)
    return result


def _save_confirmation(prepared_id,saved,result):
    text=result.model_dump_json(indent=2)
    (RUNTIME/f'{prepared_id}.confirmed.json').write_text(text,encoding='utf-8')
    path=_cache_path(saved);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(text,encoding='utf-8')


def _located_feedback(item,drawing,field='boundary',points=()):
    result=dict(item);result.setdefault('field',field)
    ids=result.get('source_edge_ids',[])
    edges=[e for e in drawing['edges'] if e['edge_id'] in ids]
    locations=result.get('points_mm') or list(points) or [p for e in edges for p in (e['start_mm'],e['end_mm'])]
    result['points_mm']=locations
    result['bounds_mm']=([min(p[0] for p in locations),min(p[1] for p in locations),
                          max(p[0] for p in locations),max(p[1] for p in locations)] if locations else None)
    result['source_handles']=sorted({e['display_handle'] for e in edges})
    return result


def _feedback_issue(item,issue_id='preview-invalid'):
    field=item.get('field','boundary')
    actions={
        'boundary':('定位提示的外围线组或局部开口，修改本项选择或撤销本次修改。','外围来源明确且局部开口已处理，最终区域有效并保留源孔洞；内部墙分支不必参与外围。'),
        'entrances':('重新局部标记提示的入口区域，或撤销本次入口修改；不需要重画门店范围。','入口自身有效，且与已有规划范围有正面积交集。'),
        'exclusions':('修改提示的禁放区域或撤销本次禁放区修改。','禁放区域自身闭合有效，源固定禁放区完整保留。'),
        'barriers':('检查提示的墙实体选择，或撤销本次墙语义修改。','墙来源和端点有效，源固定墙完整保留。')}
    action,condition=actions.get(field,actions['boundary'])
    action=item.get('action',action);condition=item.get('pass_condition',condition)
    return {'id':issue_id,'code':item['code'],'field':field,'requires_geometry':True,
        'message':item['message'],'options':[],'bounds_mm':item.get('bounds_mm'),
        'points_mm':item.get('points_mm',[]),'source_handles':item.get('source_handles',[]),
        'source_edge_ids':item.get('source_edge_ids',[]),'action':action,'pass_condition':condition}


def preview_input(body):
    """Validate a partial fact edit without signing or persisting approval."""
    body=body.model_copy(deep=True) if isinstance(body,PreviewBody) else PreviewBody.model_validate(body)
    saved=_preview_saved(body);envelope=saved['envelope'];issues=envelope['issues']
    supplements=[e.model_dump(mode='json') for e in body.supplemental_edges]
    feedback=[];resolved=set();selected=None;boundary=None;valid=True
    scope_result=None
    portal_candidates,logical,portal_entries,portal_ids=_portal_data(saved,body.resolutions,body.portal_closure_ids)
    scope_result=_store_review(saved,body.resolutions,body.scope_edge_ids,supplements,
        body.scope_candidate_id,body.perimeter_edge_ids,logical)
    explicit_boundary=(envelope.get('candidate_space') or {}).get('boundary')
    feedback.extend(_located_feedback(f,envelope['drawing']) for f in scope_result['feedback']
                    if not explicit_boundary or not f['code'].startswith('PERIMETER_'))
    valid=scope_result['valid']
    active_field='boundary';active_points=body.boundary.boundary_mm if body.boundary is not None else []
    try:
        # Invalid partial boundary selection must still permit independent source
        # classifications and other facts to remain visible in the response.
        selected=_selections(saved,body.resolutions,allow_geometry=True,partial=True,
            scope_edge_ids=body.scope_edge_ids if valid else (),supplemental_edges=supplements if valid else (),
            scope_candidate_id=body.scope_candidate_id if valid else None,portal_closure_ids=body.portal_closure_ids,
            perimeter_edge_ids=body.perimeter_edge_ids if valid else ())
        resolved.update(r['issue_id'] for r in selected['records']);resolved.update(selected['scope_issue_ids'])
        if body.boundary is not None and (body.scope_edge_ids or supplements or body.perimeter_edge_ids or body.scope_candidate_id):
            fail('CONFLICTING_SCOPE_INPUT','原边范围选择与手绘边界不能同时提交。')
        explicit=envelope['candidate_space']
        boundary=body.boundary or (PolygonData.model_validate(selected['boundary']) if selected['boundary'] else None) or (
            PolygonData.model_validate(explicit['boundary']) if explicit else None)
        active_points=boundary.boundary_mm if boundary else []
        polygon=_strict_polygon(boundary,'预览范围') if boundary else None
        if polygon is not None:
            if any(not any(Polygon(h).equals(Polygon(p)) for p in boundary.holes_mm) for h in saved['explicit_holes']):
                fail('CAD_HOLE_MUST_BE_PRESERVED','源孔洞不可删除或改形。')
            resolved.update(i['id'] for i in issues if i['field']=='boundary')
        entries=[Exclusion.model_validate(z) for z in {e['id']:e for e in [*saved['source_entrances'],*portal_entries]}.values()]+(body.entrances or [])
        for entry in entries:
            active_field='entrances';active_points=entry.boundary_mm
            area=_strict_polygon(entry,'入口')
            if polygon is not None and polygon.intersection(area).area<=0:
                fail('ENTRANCE_OUTSIDE_SCOPE','入口必须与范围有正面积交集。')
        for zone in body.exclusions:
            active_field='exclusions';active_points=zone.boundary_mm
            _strict_polygon(zone,'补充禁放区')
        if entries:resolved.update(i['id'] for i in issues if i['code']=='ENTRANCE_REQUIRED')
    except (InputError,ValueError) as exc:
        valid=False;feedback.append(_located_feedback({'code':getattr(exc,'code','INVALID_INPUT'),'message':str(exc)},
            envelope['drawing'],active_field,active_points))
    remaining=[dict(i) for i in issues if i['id'] not in resolved]
    if boundary is None:
        info=next((f for f in reversed(feedback) if f['code'].startswith('PERIMETER_') or 'HOLE' in f['code']),None)
        if info:
            remaining=[_feedback_issue(info,i['id']) if i['field']=='boundary' and i['code']=='BOUNDARY_REQUIRED' else i
                       for i in remaining]
    if not valid:
        remaining.append(_feedback_issue(feedback[0]))
    ready=valid and not remaining and boundary is not None
    payload=None;candidate=None
    if selected is not None and valid:
        candidate={'boundary':boundary.model_dump(mode='json') if boundary is not None else None,'barriers':saved['barriers']+selected['barriers'],
            'exclusions':saved['fixed_exclusions']+selected['exclusions']+[z.model_dump(mode='json') for z in body.exclusions],
            'entrances':[z.model_dump(mode='json') for z in entries]}
    if ready:
        fields=set(selected['fields'])
        fields.update(i['field'] for i in issues if i['requires_geometry'])
        manual=[]
        if body.boundary is not None:manual.append('boundary')
        if body.entrances is not None:manual.append('entrances')
        if body.exclusions:manual.append('exclusions')
        fields.update(manual)
        try:
            _make_request(saved,boundary,body.entrances or [],body.exclusions,selected,fields,
                'CONFIRMED' if fields else 'AUTO_VALIDATED',manual,sign=False)
        except (InputError,ValueError) as exc:
            code=getattr(exc,'code','INVALID_INPUT')
            field='entrances' if 'ENTRANCE' in code or 'PORTAL' in code or code=='DUPLICATE_ZONE_ID' else 'exclusions' if 'EXCLUSION' in code else 'barriers' if 'WALL' in code else 'boundary'
            geometry=(body.entrances or []) if field=='entrances' else body.exclusions if field=='exclusions' else [boundary]
            points=[p for zone in geometry for p in zone.boundary_mm]
            located=_located_feedback({'code':code,'message':str(exc)},envelope['drawing'],field,points)
            valid=ready=False;feedback.append(located)
            remaining.append(_feedback_issue(located,'validation-failed'))
        else:
            payload={'user_confirmed':True,'resolutions':[r.model_dump(mode='json') for r in body.resolutions],
                'reviewed_fields':sorted(fields),'scope_edge_ids':body.scope_edge_ids,'supplemental_edges':supplements,
                'perimeter_edge_ids':body.perimeter_edge_ids,
                'scope_candidate_id':body.scope_candidate_id,'portal_closure_ids':portal_ids}
            if body.boundary is not None:payload['boundary']=body.boundary.model_dump(mode='json')
            if body.entrances is not None:payload['entrances']=[e.model_dump(mode='json') for e in body.entrances]
            if body.exclusions:payload['exclusions']=[e.model_dump(mode='json') for e in body.exclusions]
    choices={r['issue_id']:r['option_id'] for r in (selected or {}).get('records',[])}
    usage={'selected_scope_edge_ids':body.scope_edge_ids,'supplemental_edges':supplements,
        'scope_edge_ids':body.scope_edge_ids,'perimeter_edge_ids':body.perimeter_edge_ids,
        'scope_candidate_id':body.scope_candidate_id,'portal_closure_ids':portal_ids,
        'classified_as_walls':[],'classified_as_exclusions':[],'classified_as_construction':[],
        'unresolved_edge_ids':sorted({e for i in remaining for e in i.get('source_edge_ids',[])})}
    for issue in issues:
        key={'as-walls':'classified_as_walls','as-exclusions':'classified_as_exclusions','as-construction':'classified_as_construction'}.get(choices.get(issue['id']))
        if key:usage[key].extend(issue.get('source_edge_ids',[]))
    return {'valid':valid,'ready':ready,'issues':remaining,'resolved_issue_ids':sorted(resolved),
        'candidate_space':candidate,'feedback':feedback,'source_usage':usage,'confirm_payload':payload,
        'scope_candidates':(scope_result or {}).get('scope_candidates',[]),
        'scope_ready':boundary is not None and valid,
        **{key:scope_result[key] for key in ('local_faces','perimeter_candidates','gap_candidates')},
        'derived_topology':(scope_result or {}).get('derived_topology'),
        'diagnostics':(scope_result or {}).get('diagnostics',[]),
        'boundary_source_edge_ids':(scope_result or {}).get('boundary_source_edge_ids',[]),
        'door_gap_candidates':portal_candidates,'portal_closures':logical}


def resolve_prepared(body):
    body=ResolveBody.model_validate(body.model_dump() if isinstance(body,ResolveBody) else body)
    saved=_load(body.prepared_id)
    selected=_selections(saved,body.resolutions)
    candidate=saved['envelope']['candidate_space']
    boundary=selected['boundary'] or (candidate['boundary'] if candidate else None)
    if not selected['fields'] or boundary is None: fail('NO_NECESSARY_CONFIRMATION','没有可提交的必要局部候选。')
    result=_make_request(saved,PolygonData.model_validate(boundary),[],[],selected,[],'CONFIRMED')
    _save_confirmation(body.prepared_id,saved,result)
    return result


@router.post('/prepare')
def prepare(file:UploadFile=File(...)):
    try: return prepare_bytes(file.file.read(MAX_CAD_BYTES+1),file.filename or 'store.dxf')
    except InputError as exc:
        raise HTTPException(422,detail={'code':exc.code,'message':exc.message}) from exc


@router.post('/confirm',response_model=PlanRequest)
def confirm(body:ConfirmBody):
    try: return confirm_prepared(body)
    except InputError as exc:
        raise HTTPException(422,detail={'code':exc.code,'message':exc.message}) from exc


@router.get('/prepared/{prepared_id}')
def get_prepared(prepared_id:str):
    try:
        saved=_load(prepared_id)
        value=dict(_ready(saved))
        confirmed=RUNTIME/f'{prepared_id}.confirmed.json'
        value['confirmed_request']=None
        if confirmed.is_file():
            try: value['confirmed_request']=_validated_cached(saved,confirmed).model_dump(mode='json')
            except (ValueError,OSError,KeyError,TypeError): value['evidence']['cache_reuse']='REJECTED_INVALID_CACHE'
        return value
    except InputError as exc:
        raise HTTPException(422,detail={'code':exc.code,'message':exc.message}) from exc


@router.post('/resolve',response_model=PlanRequest)
def resolve(body:ResolveBody):
    try: return resolve_prepared(body)
    except InputError as exc:
        raise HTTPException(422,detail={'code':exc.code,'message':exc.message}) from exc


@router.post('/preview-input')
def preview_route(body:PreviewBody):
    try:return preview_input(body)
    except InputError as exc:
        raise HTTPException(422,detail={'code':exc.code,'message':exc.message}) from exc


@router.post('/apply-input')
def apply_route(body:PreviewBody):
    try:return apply_input(body)
    except InputError as exc:
        raise HTTPException(422,detail={'code':exc.code,'message':exc.message}) from exc
