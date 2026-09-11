"""Independent structural metrics; not imported by the production generator."""
from collections import Counter
from shapely.geometry import Polygon
from shapely.ops import unary_union

def layout_metrics(layout):
    runs=layout.runs
    shelves=layout.shelves
    area=Polygon(layout.space.boundary.boundary_mm,layout.space.boundary.holes_mm).area
    gaps=[]
    for run in runs:
        polygons=[Polygon(m.footprint_mm) for m in run.modules]
        gaps.extend(a.distance(b) for a,b in zip(polygons,polygons[1:]))
    footprints=[Polygon(s.footprint_mm) for s in shelves]
    union=unary_union(footprints)
    inter_run=[]
    for i,a in enumerate(runs):
        for b in runs[i+1:]:
            inter_run.append(Polygon(a.footprint_mm).distance(Polygon(b.footprint_mm)))
    return {
        'module_count':len(shelves),'run_count':len(runs),
        'average_modules_per_run':len(shelves)/len(runs),
        'isolated_run_ratio':sum(len(r.modules)==1 for r in runs)/len(runs),
        'isolated_module_ratio':sum(len(r.modules)==1 for r in runs)/len(shelves),
        'short_fragment_run_count':sum(len(r.modules)<2 for r in runs),
        'effective_length_mm':sum(s.length_mm for s in shelves),
        'continuous_multi_module_length_mm':sum(s.length_mm for r in runs if len(r.modules)>1 for s in r.modules),
        'max_same_run_gap_mm':max(gaps,default=0),
        'tail_waste_mm':sum(r.remaining_length_mm for r in runs),
        'footprint_density':sum(Polygon(s.footprint_mm).area for s in shelves)/area,
        'planning_area_mm2':area,
        'shelf_envelope_proxy_area_mm2':union.envelope.area,
        'shelf_envelope_proxy_density':union.area/union.envelope.area,
        'minimum_inter_run_clearance_mm':min(inter_run) if inter_run else None,
        'direction_module_counts':dict(Counter(s.rotation_deg for s in shelves)),
        'direction_counts':dict(Counter(r.direction_deg for r in runs)),
        'template_counts':dict(Counter(s.material_id for s in shelves)),
    }

def compare_reference(metrics,reference):
    """Review output only; thresholds never depend on an automatic candidate's score."""
    ref=reference['metrics']
    checks={
        'no_worse_isolated_module_ratio':metrics['isolated_module_ratio']<=ref['isolated_module_ratio'],
        'mean_modules_at_least_reference':metrics['average_modules_per_run']>=ref['mean_modules_per_run'],
        'same_run_gap_at_most_1mm':metrics['max_same_run_gap_mm']<=1,
        'no_short_fragment_runs':metrics['short_fragment_run_count']==0,
    }
    return {'status':'PENDING_REAL_CONFIRMED_SPACE',
        'structural_subset_status':'PASS' if all(checks.values()) else 'FAIL',
        'structural_checks':checks,
        'automatic':metrics,'reference':ref,
        'scope':'Named shelf INSERT subset only; complete reference totals remain under review',
        'effective_length_ratio_observation':metrics['effective_length_mm']/ref['total_effective_length_mm'],
        'proxy_density_ratio_observation':metrics['shelf_envelope_proxy_density']/ref['shelf_envelope_proxy_density'],
        'limitations':['Reference footprint-envelope density is not a measured store-area density.',
                      'Reference free gaps are 550-690 mm; production rules retain 1200 mm.',
                      'Raw length and proxy density ratios across different spaces are observations, not acceptance thresholds.',
                      'Real store-outside count requires a human-confirmed boundary.',
                      'A numeric comparison is not by itself product acceptance.']}
