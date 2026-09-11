"""Fixed M09 measurements from validated physical runs, not a count target."""
from itertools import combinations
from shapely.geometry import Polygon
from services.planning.runs import validate_runs


def measure_layout(req, layout):
    checked=validate_runs(req,layout)
    runs=layout.runs
    shapes={r.run_id:Polygon(r.footprint_mm) for r in runs}
    byid={r.run_id:r for r in runs}
    pairs=set()
    alignment=[]
    for run in runs:
        for neighbor in run.neighbors:
            pair=tuple(sorted((run.run_id,neighbor['run_id'])))
            if pair in pairs:continue
            pairs.add(pair)
            other=byid[neighbor['run_id']]
            axis=0 if run.direction_deg==0 else 1
            alignment.append(min(abs(run.start_mm[axis]-other.start_mm[axis]),
                                 abs(run.end_mm[axis]-other.end_mm[axis])))
    gaps=[Polygon(a.footprint_mm).distance(Polygon(b.footprint_mm))
          for run in runs for a,b in zip(run.modules,run.modules[1:])]
    effective=sum(module.length_mm for run in runs for module in run.modules)
    return {'module_count':len(layout.shelves),'run_count':len(runs),
        'continuous_length_mm':effective,'effective_length_mm':effective,
        'average_modules_per_run':len(layout.shelves)/len(runs),
        'average_run_length_mm':effective/len(runs),
        'isolated_run_count':sum(len(r.modules)==1 for r in runs),
        'short_run_count':sum(len(r.modules)==2 for r in runs),
        'tail_waste_mm':sum(r.remaining_length_mm for r in runs),
        'max_same_run_gap_mm':max(gaps,default=0.0),
        'minimum_inter_run_clearance_mm':min((shapes[a.run_id].distance(shapes[b.run_id])
                                             for a,b in combinations(runs,2)),default=None),
        'alignment_offset_mm':sum(alignment)/len(alignment) if alignment else None,
        'geometry_violation_count':checked['geometry_violation_count'],
        'bom_quantity':sum(row.quantity for row in layout.bom),
        'short_run_meaning':'Two-module runs: diagnostic only, not automatically unreasonable',
        'alignment_meaning':'Mean smaller endpoint offset of unique adjacent parallel run pairs (mm)',
        'circulation_scope':checked['circulation_scope']}
