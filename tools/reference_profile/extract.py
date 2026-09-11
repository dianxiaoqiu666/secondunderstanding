"""Offline reference evidence. Never import this module from production services."""
from __future__ import annotations
import hashlib
import html
import json
import math
import re
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf import bbox, disassemble
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

from shared.planning_v2 import ReferenceDesignProfile

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'outputs/reference_profile'
EXPECTED_SHA256 = 'c19be5b57b39f7088a8ef749125cef206021d93fa0e858da537335489c4655e9'
# Reference extraction precision, not a planning clearance: whole-mm drawing
# dimensions have floating-point residues <1e-7 mm; smallest observed free gap
# is hundreds of mm. One millimetre cannot bridge an aisle.
JOIN_TOL_MM = 1.0
ANGLE_TOL_DEG = 0.01


def spec_from_text(text):
    match = re.fullmatch(r'\s*(\d+(?:\.\d+)?)\s*[×xX*]\s*(\d+(?:\.\d+)?)\s*m\s*', text)
    return tuple(float(v) * 1000 for v in match.groups()) if match else None


def extract_module(entity):
    """Exact closed LINE polygon via ezdxf transforms, never axis-aligned bbox."""
    parts = list(disassemble.recursive_decompose([entity]))
    lines = [LineString([tuple(e.dxf.start)[:2], tuple(e.dxf.end)[:2]])
             for e in parts if e.dxftype() == 'LINE']
    polygons = list(polygonize(unary_union(lines)))
    if not polygons:
        raise ValueError(f'Shelf block has no closed footprint: {entity.dxf.handle}')
    poly = unary_union(polygons)
    if poly.geom_type != 'Polygon' or not poly.is_valid:
        raise ValueError('Ambiguous shelf footprint')
    rect = poly.minimum_rotated_rectangle
    if abs(rect.area - poly.area) > 1:
        raise ValueError('Nonrectangular shelf requires an explicit reference method')
    pts = list(rect.exterior.coords)[:-1]
    edges = [(math.dist(pts[i], pts[(i+1)%4]), pts[i], pts[(i+1)%4]) for i in range(4)]
    length, a, b = max(edges, key=lambda e:e[0])
    depth = min(e[0] for e in edges)
    angle = math.degrees(math.atan2(b[1]-a[1], b[0]-a[0])) % 180
    if abs(angle - 180) < ANGLE_TOL_DEG or abs(angle) < ANGLE_TOL_DEG:
        angle = 0.0
    ext = bbox.extents(parts, fast=True)
    return dict(module_id=entity.dxf.handle, block_name=entity.dxf.name,
                layer=entity.dxf.layer, length_mm=round(length,6), depth_mm=round(depth,6),
                direction_deg=round(angle,6), footprint_mm=[list(p) for p in poly.exterior.coords[:-1]],
                center_mm=list(poly.centroid.coords[0]),
                bbox_mm=[list(ext.extmin)[:2],list(ext.extmax)[:2]],
                default_level_count=None)


def classify_modules(modules, texts):
    """Identify a labeled sample panel by repeated dimension-label alignment.

    A dimension label alone is insufficient: require >=2 distinct specifications,
    same-side local label alignment and all matched sample blocks detached from
    every other block. No source coordinates, handles or expected counts used.
    """
    matches = {}
    for text in texts:
        spec = spec_from_text(text['text'])
        if spec is None:
            continue
        tx, ty = text['position_mm']
        candidates = []
        for m in modules:
            p = Polygon(m['footprint_mm']); x0,y0,x1,y1 = p.bounds
            if (abs(m['length_mm']-spec[0]) <= JOIN_TOL_MM
                and abs(m['depth_mm']-spec[1]) <= JOIN_TOL_MM
                and abs(m['direction_deg']) <= ANGLE_TOL_DEG
                and tx < x0 and x0-tx <= 1.5*m['length_mm']
                and y0-JOIN_TOL_MM <= ty <= y1+JOIN_TOL_MM):
                candidates.append((p.distance(Point(tx,ty)),m))
        if candidates:
            _, nearest = min(candidates,key=lambda p:p[0])
            matches[nearest['module_id']] = text
    specs = {(m['length_mm'],m['depth_mm']) for m in modules if m['module_id'] in matches}
    result=[]
    for m in modules:
        p=Polygon(m['footprint_mm'])
        nearest=min((p.distance(Polygon(n['footprint_mm'])) for n in modules if n is not m),default=None)
        matched=matches.get(m['module_id'])
        is_legend=matched is not None and len(specs)>=2 and nearest is not None and nearest>JOIN_TOL_MM
        evidence={'nearest_other_module_edge_distance_mm':nearest}
        if matched:
            evidence['aligned_dimension_label']=matched
        evidence['reason']=('Detached sample with matching left-side dimension label in a multi-specification sample panel'
                            if is_legend else 'Repeated shelf block instance outside the dimension-labeled sample panel; retained including isolated and oblique modules')
        result.append({**m,'classification':'LEGEND' if is_legend else 'ACTUAL','evidence':evidence})
    return result


def projected(poly, angle):
    t=math.radians(angle); u=(math.cos(t),math.sin(t)); n=(-u[1],u[0])
    pts=list(poly.exterior.coords)
    along=[x*u[0]+y*u[1] for x,y in pts]; cross=[x*n[0]+y*n[1] for x,y in pts]
    return min(along),max(along),min(cross),max(cross)


def group_runs(modules, tol=JOIN_TOL_MM):
    """Single-face rows: collinear centers + end-to-end contact. Back-to-back
    rows remain separate and are marked by the neighbor analysis below."""
    parents=list(range(len(modules)))
    def find(i):
        while parents[i]!=i:
            parents[i]=parents[parents[i]];i=parents[i]
        return i
    for i,a in enumerate(modules):
        pa=projected(Polygon(a['footprint_mm']),a['direction_deg'])
        for j,b in enumerate(modules[:i]):
            if abs((a['direction_deg']-b['direction_deg']+90)%180-90)>ANGLE_TOL_DEG:
                continue
            pb=projected(Polygon(b['footprint_mm']),a['direction_deg'])
            centers=abs((pa[2]+pa[3]-pb[2]-pb[3])/2)
            gap=max(pa[0],pb[0])-min(pa[1],pb[1])
            if centers<=tol and -tol<=gap<=tol and abs(a['depth_mm']-b['depth_mm'])<=tol:
                parents[find(i)]=find(j)
    groups={}
    for i,m in enumerate(modules):groups.setdefault(find(i),[]).append(m)
    runs=[]
    for group in sorted(groups.values(),key=lambda g:min(m['module_id'] for m in g)):
        angle=group[0]['direction_deg']
        group.sort(key=lambda m:projected(Polygon(m['footprint_mm']),angle)[0])
        ps=[projected(Polygon(m['footprint_mm']),angle) for m in group]
        union=unary_union([Polygon(m['footprint_mm']) for m in group])
        runs.append(dict(run_id=f'R{len(runs)+1:02}',direction_deg=angle,
                         modules=[m['module_id'] for m in group],module_count=len(group),
                         module_spec_sequence=[dict(length_mm=m['length_mm'],depth_mm=m['depth_mm']) for m in group],
                         same_run_gaps_mm=[round(ps[i+1][0]-ps[i][1],6) for i in range(len(ps)-1)],
                         effective_length_mm=sum(m['length_mm'] for m in group),
                         depth_mm=group[0]['depth_mm'],footprint_mm=list(union.convex_hull.exterior.coords)[:-1],
                         neighbors=[]))
    for i,a in enumerate(runs):
        pa=projected(Polygon(a['footprint_mm']),a['direction_deg'])
        for b in runs[i+1:]:
            if abs((a['direction_deg']-b['direction_deg']+90)%180-90)>ANGLE_TOL_DEG:continue
            pb=projected(Polygon(b['footprint_mm']),a['direction_deg'])
            overlap=min(pa[1],pb[1])-max(pa[0],pb[0])
            if overlap<=tol:continue
            gap=max(pa[2],pb[2])-min(pa[3],pb[3])
            if gap < -tol:continue
            # An adjacent corridor must not cross another parallel shelf run.
            lo=min(pa[3],pb[3]); hi=max(pa[2],pb[2]);blocked=False
            for c in runs:
                if c is a or c is b:continue
                if abs((a['direction_deg']-c['direction_deg']+90)%180-90)>ANGLE_TOL_DEG:continue
                pc=projected(Polygon(c['footprint_mm']),a['direction_deg'])
                if min(pc[3],hi)-max(pc[2],lo)>tol and min(pa[1],pb[1],pc[1])-max(pa[0],pb[0],pc[0])>tol:
                    blocked=True;break
            if blocked:continue
            relation='BACK_TO_BACK_CONTACT' if abs(gap)<=tol else 'PARALLEL_FREE_GAP'
            for left,right in [(a,b),(b,a)]:left['neighbors'].append(dict(run_id=right['run_id'],relation=relation,clear_gap_mm=round(gap,6),longitudinal_overlap_mm=round(overlap,6)))
    return runs


def build_profile(path):
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if digest!=EXPECTED_SHA256:raise ValueError('Reference source SHA256 differs from DATA_CLASSIFICATION.md')
    doc=ezdxf.readfile(path)
    if doc.units!=4:raise ValueError('Reference units must explicitly be millimetres')
    msp=doc.modelspace()
    texts=[dict(handle=e.dxf.handle,text=e.plain_text(),position_mm=list(e.dxf.insert)[:2],layer=e.dxf.layer) for e in msp.query('TEXT MTEXT')]
    modules=[extract_module(e) for e in msp.query('INSERT') if '货架' in e.dxf.name and '货架' in e.dxf.layer]
    classification=classify_modules(modules,texts);actual=[m for m in classification if m['classification']=='ACTUAL']
    runs=group_runs(actual)
    union=unary_union([Polygon(m['footprint_mm']) for m in actual]);envelope=union.envelope
    gaps=[n['clear_gap_mm'] for r in runs for n in r['neighbors'] if n['relation']=='PARALLEL_FREE_GAP' and r['run_id']<n['run_id']]
    specs=Counter((m['length_mm'],m['depth_mm']) for m in actual)
    directions=Counter(str(m['direction_deg']) for m in actual)
    relations=[]
    for t in texts:
        if t['text'].isdigit() or spec_from_text(t['text']):continue
        if not any(s in t['text'] for s in ['门','冷库','厕所','水果','收银','打包','冰箱','冰柜']):continue
        p=Point(t['position_mm']);near=sorted([(p.distance(Polygon(r['footprint_mm'])),r['run_id']) for r in runs])[:3]
        relations.append({**t,'semantics':'TEXT_ANCHOR_ONLY_NOT_CONFIRMED_FUNCTION_ZONE','nearest_runs':[dict(run_id=r,distance_mm=round(d,6)) for d,r in near]})
    metrics=dict(actual_module_count=len(actual),legend_module_count=len(classification)-len(actual),
                 shelf_run_count=len(runs),run_definition='Single face collinear end-to-end chain; back-to-back faces are separate runs',
                 mean_modules_per_run=len(actual)/len(runs),isolated_single_module_runs=sum(r['module_count']==1 for r in runs),
                 isolated_module_ratio=sum(r['module_count']==1 for r in runs)/len(actual),
                 isolated_run_ratio=sum(r['module_count']==1 for r in runs)/len(runs),
                 short_fragment_runs_under_two_modules=sum(r['module_count']<2 for r in runs),
                 total_effective_length_mm=sum(m['length_mm'] for m in actual),
                 continuous_multi_module_length_mm=sum(r['effective_length_mm'] for r in runs if r['module_count']>1),
                 shelf_union_area_mm2=round(union.area,6),store_area_mm2=None,store_footprint_density=None,
                 density_denominator_status='UNKNOWN: no confirmed store boundary in this reference; no PRIMARY scope used',
                 shelf_envelope_proxy_area_mm2=round(envelope.area,6),shelf_envelope_proxy_density=union.area/envelope.area,
                 direction_module_counts=dict(sorted(directions.items())),parallel_free_gaps_mm=sorted(gaps),
                 parallel_gap_warning='Measured reference free gaps only; no legal or production-aisle compliance implied',
                 same_run_gap_max_mm=max((abs(g) for r in runs for g in r['same_run_gaps_mm']),default=0))
    profile=ReferenceDesignProfile(source={'filename':path.name,'sha256':digest},classification=classification,runs=runs,metrics=metrics,
        functional_relations=relations,bom=[dict(reference_spec_id=f'REF-{l:g}x{d:g}',length_mm=l,depth_mm=d,default_level_count=None,quantity=q,total_level_count=None) for (l,d),q in sorted(specs.items())],
        method={'cad_parser':f'ezdxf {ezdxf.__version__}', 'footprint':'recursive_decompose INSERT transformations + Shapely polygonize closed block LINE edges; bbox is audit-only',
                'classification':'Dimension labels aligned left of detached rectangular samples in a multi-specification panel; remaining shelf blocks retained as actual including isolated and diagonal instances',
                'join_tolerance_mm':JOIN_TOL_MM,'angle_tolerance_deg':ANGLE_TOL_DEG,
                'tolerance_evidence':'1mm drawing precision tolerance above <1e-7mm floating residue, below all observed noncontact gaps; tested with translated/rotated fixtures',
                'default_layers':'UNKNOWN; numeric annotations preserved separately, not interpreted as approved layer counts',
                'numeric_annotations':[t for t in texts if t['text'].isdigit()],
                'production_use':'FORBIDDEN; this profile is an offline validation artifact only'},
        unresolved=['No authoritative confirmed store floor polygon; physical store density UNKNOWN. Shelf-envelope proxy must be labeled when compared.',
                    'Bare numeric text (3/4/5/7 etc.) has no independently confirmed layer-count semantics; reference defaults remain null.',
                    'Functional text establishes nearest geometric relationships only, not confirmed zone polygons or category policy.',
                    'Shelf count covers named shelf INSERT modules. Loose LINE fixtures, cold equipment and furniture are displayed as context, not promoted to standardized shelf modules.'])
    return profile,doc


def render(profile,doc):
    all_lines=[]
    for e in disassemble.recursive_decompose(doc.modelspace()):
        if e.dxftype()=='LINE':all_lines.append([tuple(e.dxf.start)[:2],tuple(e.dxf.end)[:2]])
    coords=[p for line in all_lines for p in line]
    # Annotation anchors can extend left of all drawn geometry (the spec panel).
    coords.extend(tuple(e.dxf.insert)[:2] for e in doc.modelspace().query('TEXT MTEXT'))
    x0=min(p[0] for p in coords)-500;x1=max(p[0] for p in coords)+500
    y0=min(p[1] for p in coords)-500;y1=max(p[1] for p in coords)+500
    def pts(ps):return ' '.join(f'{x:.4f},{-y:.4f}' for x,y in ps)
    svg=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0} {-y1} {x1-x0} {y1-y0}" role="img" aria-label="Frozen reference CAD evidence">',
         f'<rect x="{x0}" y="{-y1}" width="{x1-x0}" height="{y1-y0}" fill="white"/>']
    for line in all_lines:svg.append(f'<polyline points="{pts(line)}" stroke="#a8adb6" stroke-width="14" fill="none"/>')
    palette=['#2563eb','#0f766e','#7c3aed','#b45309','#be185d','#0369a1']
    mapping={mid:(r['run_id'],palette[i%len(palette)]) for i,r in enumerate(profile.runs) for mid in r['modules']}
    for m in profile.classification:
        rid,color=mapping.get(m['module_id'],('LEGEND','#e11d48'))
        title=f"{m['module_id']} {rid} {m['length_mm']:g}×{m['depth_mm']:g}mm {m['direction_deg']:g}°"
        svg.append(f'<polygon points="{pts(m["footprint_mm"])}" fill="{color}" fill-opacity=".35" stroke="{color}" stroke-width="22"><title>{title}</title></polygon>')
        x,y=m['center_mm'];svg.append(f'<text x="{x}" y="{-y}" font-size="135" text-anchor="middle" fill="#101827">{m["module_id"]}</text>')
    for t in doc.modelspace().query('TEXT MTEXT'):
        if t.plain_text().isdigit():continue
        x,y,_=t.dxf.insert;svg.append(f'<text x="{x}" y="{-y}" font-size="190" fill="#111827">{html.escape(t.plain_text())}</text>')
    svg.append('</svg>');s='\n'.join(svg)
    (OUT/'reference.svg').write_text(s,encoding='utf-8')
    body=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>成熟参考冻结画像</title><style>body{{font-family:system-ui;margin:24px;background:#eef2f6}}svg{{width:100%;height:78vh;background:white}}pre{{white-space:pre-wrap}}table{{border-collapse:collapse}}td,th{{padding:8px;border:1px solid #aaa}}</style><h1>成熟参考：独立二维证据</h1><p>蓝/绿/紫等颜色为实际货架排；红色为尺寸图例。悬停查看规格，模块标注为来源句柄。灰线为原CAD上下文，不是确认规划域。</p><p>密度分母：货架外接矩形代理，真实门店面积 UNKNOWN。默认层数 UNKNOWN。下方线段式家具未冒充标准货架块。</p>{s}<h2>结构指标</h2><pre>{html.escape(json.dumps(profile.metrics,ensure_ascii=False,indent=2))}</pre><h2>排分组</h2><table><tr><th>排</th><th>方向</th><th>模块顺序</th><th>同排间隙 mm</th></tr>'''
    for r in profile.runs:body+=f'<tr><td>{r["run_id"]}</td><td>{r["direction_deg"]}</td><td>{" → ".join(r["modules"])}</td><td>{r["same_run_gaps_mm"]}</td></tr>'
    body+='</table></html>'
    (OUT/'reference.html').write_text(body,encoding='utf-8')


def main():
    paths=list((ROOT/'input/reference/cad').glob('*.dxf'))
    if len(paths)!=1:raise ValueError('Expected the one explicitly selected reference DXF')
    profile,doc=build_profile(paths[0]);OUT.mkdir(parents=True,exist_ok=True)
    data=(profile.model_dump_json(indent=2)+'\n').encode('utf-8')
    frozen=OUT/'reference_profile.json'
    if frozen.exists():
        if frozen.read_bytes()!=data:
            raise ValueError('Existing reference freeze differs; preserve it and issue an explicitly reviewed new version')
    else:
        frozen.write_bytes(data)
    digest=hashlib.sha256(data).hexdigest()
    freeze_text=f'{digest}  reference_profile.json\n{profile.source.sha256}  SOURCE_DXF\n'
    freeze_path=OUT/'FROZEN_SHA256.txt'
    if freeze_path.exists():
        if freeze_path.read_text(encoding='utf-8-sig')!=freeze_text:
            raise ValueError('Existing freeze record differs')
    else:
        freeze_path.write_text(freeze_text,encoding='utf-8')
    render(profile,doc)
    print(json.dumps({'profile_sha256':digest,'metrics':profile.metrics,'bom':profile.bom},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
