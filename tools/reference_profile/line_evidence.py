"""Supplement the frozen INSERT-only profile without changing its bytes."""
import hashlib
import json
import math
from pathlib import Path

import ezdxf
from shapely.geometry import LineString
from shapely.ops import polygonize, unary_union

from tools.reference_profile.extract import EXPECTED_SHA256, ROOT, OUT


def main():
    path=next((ROOT/'input/reference/cad').glob('*.dxf'))
    if hashlib.sha256(path.read_bytes()).hexdigest()!=EXPECTED_SHA256:raise ValueError('Source changed')
    doc=ezdxf.readfile(path)
    entities=[e for e in doc.modelspace().query('LINE') if '货架' in e.dxf.layer]
    lines=[LineString([tuple(e.dxf.start)[:2],tuple(e.dxf.end)[:2]]) for e in entities]
    polygons=list(polygonize(unary_union(lines)))
    records=[]
    for poly in polygons:
        if poly.area<1000:continue
        rect=poly.minimum_rotated_rectangle;pts=list(rect.exterior.coords)
        sides=[math.dist(a,b) for a,b in zip(pts,pts[1:])]
        length,depth=max(sides),min(sides)
        handles=[e.dxf.handle for e,line in zip(entities,lines) if line.intersection(poly.boundary).length>1]
        records.append(dict(polygon_id=f'LINE-POLY-{len(records)+1:02}',length_mm=round(length,4),depth_mm=round(depth,4),
                            area_mm2=round(poly.area,4),rectangular_fill_ratio=poly.area/rect.area,
                            footprint_mm=list(poly.exterior.coords)[:-1],source_line_handles=handles,
                            classification='UNRESOLVED_SHELF_OR_FIXTURE_LINEWORK'))
    report=dict(source_sha256=EXPECTED_SHA256,
        scope='Supplemental evidence outside frozen named INSERT modules; never production input',
        complete_design_actual_shelf_count=None,
        complete_design_count_status='UNKNOWN: loose LINE shelf/fixture evidence requires semantic and module-boundary review',
        reason='Reference has additional rotated 1200x400 LINE furniture and 2400x400 merged loops near the offline shelf legend; 68 is only the named shelf INSERT subset, not a verified whole-design count.',
        evidence_texts=[dict(handle=e.dxf.handle,text=e.plain_text(),position_mm=list(e.dxf.insert)[:2]) for e in doc.modelspace().query('TEXT MTEXT') if '线下' in e.plain_text()],
        candidate_polygons=records,
        caveats=['polygonize faces are not individual modules; touching boundaries, slight overlaps, missing partitions and numeric precision can merge or fragment faces.',
                 'Do not use polygon face count, area division or historical expected count as actual shelf count.',
                 'Frozen INSERT profile remains valid only as a named-block subset baseline; mature whole-design quality gate needs review of this supplemental evidence.'])
    raw=(json.dumps(report,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    (OUT/'line_evidence.json').write_bytes(raw)
    (OUT/'LINE_EVIDENCE_SHA256.txt').write_text(hashlib.sha256(raw).hexdigest()+'  line_evidence.json\n',encoding='utf-8')
    print(json.dumps({'candidate_polygon_faces':len(records),'complete_design_actual_shelf_count':None,'sha256':hashlib.sha256(raw).hexdigest()}))


if __name__=='__main__':main()
