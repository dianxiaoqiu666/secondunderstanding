"""Versioned, offline LINE evidence supplement. Existing v1.0 is immutable."""
import hashlib
import html
import json
import math
from collections import Counter

import ezdxf
from shapely import STRtree, set_precision
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

from tools.reference_profile.extract import ROOT, OUT, EXPECTED_SHA256, group_runs, projected

GRID_MM = 0.1
SOURCE_SUPPORT_MM = 0.11  # sqrt(2)*0.05mm max endpoint rounding displacement
DIMENSION_TOL_MM = 1.0  # existing drawing overlap truncates some faces by 0.78mm
NEAR_COLLINEAR_MM = 6.0  # explicitly observed 5.3-5.6mm source lateral offsets


def freeze(path,raw):
    if path.exists():
        if path.read_bytes()!=raw:raise ValueError(f'Frozen artifact differs: {path.name}')
    else:path.write_bytes(raw)


def line_faces(entities, grid=GRID_MM):
    """Only node/snap existing edges; never invent divider lines in merged faces."""
    lines=[LineString([tuple(e.dxf.start)[:2],tuple(e.dxf.end)[:2]]) for e in entities]
    tree=STRtree(lines)
    faces=list(polygonize(unary_union([set_precision(line,grid) for line in lines])))
    rows=[]
    for p in sorted(faces,key=lambda p:(round(p.centroid.x,4),round(p.centroid.y,4))):
        if p.area<1000:continue
        rect=p.minimum_rotated_rectangle;pts=list(rect.exterior.coords)
        edges=[(math.dist(a,b),a,b) for a,b in zip(pts,pts[1:])]
        le,a,b=max(edges);de=min(e[0] for e in edges)
        angle=math.degrees(math.atan2(b[1]-a[1],b[0]-a[0]))%180
        indices=[int(i) for i in tree.query(p.envelope.buffer(SOURCE_SUPPORT_MM*2))]
        supported=[i for i in indices if lines[i].buffer(SOURCE_SUPPORT_MM).intersection(p.boundary).length>0.000001]
        support=unary_union([lines[i].buffer(SOURCE_SUPPORT_MM) for i in supported])
        unsupported=p.boundary.difference(support).length
        # Recover orientation from the source, not the rounded rectangle edges.
        direction_candidates=[]
        for i in supported:
            a,b=lines[i].coords
            an=math.degrees(math.atan2(b[1]-a[1],b[0]-a[0]))%180
            if abs((an-angle+90)%180-90)<.02:direction_candidates.append(an)
        if direction_candidates:angle=sorted(direction_candidates)[len(direction_candidates)//2]
        if min(angle,180-angle)<.001:angle=0
        rows.append(dict(module_id=f'LINE-{len(rows)+1:03}',length_mm=round(le,6),depth_mm=round(de,6),direction_deg=round(angle,9),
                         footprint_mm=list(p.exterior.coords)[:-1],center_mm=list(p.centroid.coords[0]),
                         polygon_area_mm2=p.area,rectangular_fill_ratio=p.area/rect.area,
                         source_line_handles=[entities[i].dxf.handle for i in supported],
                         unsupported_perimeter_mm=unsupported,default_level_count=None))
    return rows


def classify_offline_shelves(faces,texts):
    labels=[t for t in texts if t['text'].replace(' ','')=='线下1.2×0.4m']
    if len(labels)!=1:raise ValueError('Missing/ambiguous explicit offline shelf specification legend')
    label=labels[0];tx,ty=label['position_mm'];rows=[]
    for m in faces:
        if abs(m['length_mm']-1200)>DIMENSION_TOL_MM or abs(m['depth_mm']-400)>DIMENSION_TOL_MM:continue
        if m['rectangular_fill_ratio']<.998 or m['unsupported_perimeter_mm']>.001:
            raise ValueError('Nominal shelf candidate requires unsupported geometry or has ambiguous footprint')
        p=Polygon(m['footprint_mm']);x0,y0,x1,y1=p.bounds
        # Prefix-bearing Chinese specification annotation is wider than the
        # dimension-only labels of the INSERT panel (up to three module lengths).
        legend=(abs(m['direction_deg'])<.01 and tx<x0 and x0-tx<3600 and y0-1<=ty<=y1+1)
        rows.append({**m,'classification':'LEGEND' if legend else 'ACTUAL',
                     'nominal_length_mm':1200,'nominal_depth_mm':400,
                     'specification_source':label,
                     'reason':('Matching left-side offline-shelf specification annotation and detached sample' if legend else
                               'Repeated closed LINE face with all perimeter supported by source LINEs, matching explicit offline-shelf legend; no division synthesized')})
    if sum(r['classification']=='LEGEND' for r in rows)!=1:raise ValueError('Ambiguous LINE legend classification')
    return rows


def run_offsets(runs,modules):
    lookup={m['module_id']:m for m in modules}
    for r in runs:
        ps=[projected(Polygon(lookup[mid]['footprint_mm']),r['direction_deg']) for mid in r['modules']]
        r['lateral_steps_mm']=[round(abs((ps[i+1][2]+ps[i+1][3]-ps[i][2]-ps[i][3])/2),6) for i in range(len(ps)-1)]
        r['nominal_effective_length_mm']=1200*r['module_count']
    return runs


def main():
    path=next((ROOT/'input/reference/cad').glob('*.dxf'))
    if hashlib.sha256(path.read_bytes()).hexdigest()!=EXPECTED_SHA256:raise ValueError('Source changed')
    base_raw=(OUT/'reference_profile.json').read_bytes();base=json.loads(base_raw)
    doc=ezdxf.readfile(path);msp=doc.modelspace()
    entities=[e for e in msp.query('LINE') if '货架' in e.dxf.layer]
    texts=[dict(handle=e.dxf.handle,text=e.plain_text(),position_mm=list(e.dxf.insert)[:2]) for e in msp.query('TEXT MTEXT')]
    faces=line_faces(entities);rows=classify_offline_shelves(faces,texts)
    actual=[r for r in rows if r['classification']=='ACTUAL']
    strict=run_offsets(group_runs(actual,tol=1),actual)
    near=run_offsets(group_runs(actual,tol=NEAR_COLLINEAR_MM),actual)
    area=unary_union([Polygon(m['footprint_mm']) for m in actual]).area
    excluded=[m for m in faces if m['module_id'] not in {r['module_id'] for r in rows}]
    report=dict(schema_version='reference-1.1-supplement',source_sha256=EXPECTED_SHA256,
        base_profile_sha256=hashlib.sha256(base_raw).hexdigest(),base_profile_file='reference_profile.json',
        scope='Named INSERT shelf subset plus separately evidenced offline LINE shelf subset; no production input',
        line_classification=rows,line_runs_strict_1mm=strict,line_runs_near_collinear_6mm=near,
        line_metrics=dict(actual_module_count=len(actual),legend_module_count=len(rows)-len(actual),
                          nominal_effective_length_mm=1200*len(actual),measured_effective_length_mm=sum(r['length_mm'] for r in actual),
                          area_mm2=area,strict_run_count=len(strict),strict_mean_modules_per_run=len(actual)/len(strict),
                          strict_isolated_module_count=sum(r['module_count']==1 for r in strict),
                          near_collinear_run_count=len(near),near_collinear_mean_modules_per_run=len(actual)/len(near),
                          near_collinear_isolated_module_count=sum(r['module_count']==1 for r in near),
                          max_lateral_step_mm=max((s for r in near for s in r['lateral_steps_mm']),default=0),
                          max_absolute_end_gap_mm=max((abs(s) for r in near for s in r['same_run_gaps_mm']),default=0),
                          directions=dict(Counter(str(round(m['direction_deg'],6)) for m in actual))),
        combined_verified_subset=dict(actual_module_count=base['metrics']['actual_module_count']+len(actual),
                                      legend_module_count=base['metrics']['legend_module_count']+1,
                                      nominal_effective_length_mm=base['metrics']['total_effective_length_mm']+1200*len(actual),
                                      strict_run_count=base['metrics']['shelf_run_count']+len(strict),
                                      near_collinear_run_count=base['metrics']['shelf_run_count']+len(near)),
        line_bom=[dict(reference_spec_id='REF-LINE-1200x400',length_mm=1200,depth_mm=400,quantity=len(actual),default_level_count=None,total_level_count=None)],
        excluded_non_shelf_specification_faces=excluded,
        raw_source_lines=[dict(handle=e.dxf.handle,start_mm=list(e.dxf.start)[:2],end_mm=list(e.dxf.end)[:2]) for e in entities],
        method=dict(precision_grid_mm=GRID_MM,perimeter_source_support_tolerance_mm=SOURCE_SUPPORT_MM,
                    source_support='Entire face perimeter must be covered by original LINE buffers; preserve all raw LINE endpoints and supporting handles',
                    module_identity='Existing closed face matches textual nominal specification within 1mm and rectangle-fill>=0.998; merged 2400 face never divided without source partition',
                    near_collinear_grouping='Separate diagnostic only: source has repeated 5-6mm lateral shifts. No repaired footprints, removed overlap or claim of production geometric safety.',
                    observed_routes=[{'round':1,'method':'Raw floating-point node/polygonize','result':'23 1200x400 faces including sample plus 4 merged 2400x400 faces; insufficient partition recovery'},
                                     {'round':2,'method':'GEOS 0.1mm precision model then node/polygonize','result':'All accepted module-face boundaries verified against original LINEs; no invented partition'}]),
        completeness=dict(complete_design_shelf_count=None,status='VERIFIED_SUBSETS_WITH_UNRESOLVED_OTHER_FIXTURES',
                          unresolved=['800x250 faces correspond visually to small-parts-box sample; not regular shelf modules. 1800x900 faces include cold equipment and counters; not automatically classified as shelves.',
                                      'Additional fixtures and unclosed linework cannot be proved absent. Verified subset count is not an authoritative whole-store inventory.',
                                      'Default shelf levels remain UNKNOWN; physical store area and confirmed entrance/zone polygons remain UNKNOWN.']))
    raw=(json.dumps(report,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    freeze(OUT/'reference_profile_v1.1.json',raw)
    digest=hashlib.sha256(raw).hexdigest()
    freeze(OUT/'FROZEN_V1.1_SHA256.txt',(digest+'  reference_profile_v1.1.json\n').encode())
    svg=(OUT/'reference.svg').read_text(encoding='utf-8-sig')
    overlay=[]
    for m in rows:
        color='#dc2626' if m['classification']=='LEGEND' else '#087f23'
        pts=' '.join(f'{x:.4f},{-y:.4f}' for x,y in m['footprint_mm'])
        label=f"{m['module_id']} {m['classification']} 1200×400 mm; measured {m['length_mm']:.3f}×{m['depth_mm']:.3f}"
        overlay.append(f'<polygon points="{pts}" fill="{color}" fill-opacity=".35" stroke="{color}" stroke-width="18"><title>{label}</title></polygon>')
        x,y=m['center_mm'];overlay.append(f'<text x="{x}" y="{-y}" font-size="115" fill="#052e16">{m["module_id"]}</text>')
    svg=svg.replace('</svg>','\n'.join(overlay)+'</svg>')
    (OUT/'reference-v1.1.svg').write_text(svg,encoding='utf-8')
    (OUT/'reference-v1.1.html').write_text('<!doctype html><meta charset="utf-8"><title>参考1.1补充</title><style>body{font-family:system-ui}svg{width:100%;height:90vh}pre{white-space:pre-wrap}</style><h1>命名 INSERT 与独立 LINE 货架证据</h1><p>绿色：新增 LINE 实际模块；红色：规格图例。未知设施仍灰色；不表示确认门店域。</p>'+svg+'<pre>'+html.escape(json.dumps(report['line_metrics'],ensure_ascii=False,indent=2))+'</pre>',encoding='utf-8')
    print(json.dumps(dict(sha256=digest,line_metrics=report['line_metrics'],combined=report['combined_verified_subset']),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
