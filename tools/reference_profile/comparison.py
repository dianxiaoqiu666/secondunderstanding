"""Pure offline comparison of completed outputs. No I/O and no generation.

The caller verifies frozen reference SHA256 values before passing dictionaries.
This function cannot grant human approval or Gate C, regardless of metadata.
"""
from __future__ import annotations

import math
from collections import Counter
from statistics import median

from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

from shared.planning_v2 import LayoutV2
from tools.reference_profile.extract import group_runs, projected

NUMERIC_MM = 0.001
AREA_EPS_MM2 = 0.001


def _distribution(values):
    vals=sorted(round(float(v),6) for v in values)
    return dict(count=len(vals),values_mm=vals,min_mm=min(vals) if vals else None,
                median_mm=median(vals) if vals else None,max_mm=max(vals) if vals else None)


def _measured(module_id,footprint):
    p=Polygon(footprint)
    if not p.is_valid or p.area<=AREA_EPS_MM2:raise ValueError(f'Invalid module footprint: {module_id}')
    pts=list(p.minimum_rotated_rectangle.exterior.coords)
    sides=[(math.dist(a,b),a,b) for a,b in zip(pts,pts[1:])]
    length,a,b=max(sides);depth=min(s[0] for s in sides)
    angle=math.degrees(math.atan2(b[1]-a[1],b[0]-a[0]))%180
    if min(angle,180-angle)<.000001:angle=0.0
    return dict(module_id=module_id,footprint_mm=list(p.exterior.coords)[:-1],
                length_mm=length,depth_mm=depth,direction_deg=angle,
                rectangular_fill_ratio=p.area/p.minimum_rotated_rectangle.area)


def _structure(modules,tolerance,scope=None,*,include_density=True):
    if not modules:raise ValueError('No evidenced modules')
    runs=group_runs(modules,tol=tolerance)
    polygons=[Polygon(m['footprint_mm']) for m in modules]
    union=unary_union(polygons)
    isolated=sum(r['module_count']==1 for r in runs)
    gaps=[n['clear_gap_mm'] for r in runs for n in r['neighbors']
          if n['relation']=='PARALLEL_FREE_GAP' and r['run_id']<n['run_id']]
    contacts=[(r['run_id'],n['run_id']) for r in runs for n in r['neighbors']
              if n['relation']=='BACK_TO_BACK_CONTACT' and r['run_id']<n['run_id']]
    values=dict(module_count=len(modules),reconstructed_single_face_run_count=len(runs),
                mean_modules_per_run=len(modules)/len(runs),isolated_module_count=isolated,
                isolated_module_ratio=isolated/len(modules),isolated_run_ratio=isolated/len(runs),
                short_fragment_runs_under_two_modules=isolated,
                effective_length_mm=sum(m['length_mm'] for m in modules),
                continuous_multi_module_length_mm=sum(r['effective_length_mm'] for r in runs if r['module_count']>1),
                measured_size_counts={f'{length:g}x{depth:g}':count for (length,depth),count in sorted(Counter((round(m['length_mm']),round(m['depth_mm'])) for m in modules).items())},
                direction_module_counts=dict(sorted(Counter(f'{m["direction_deg"]:.6f}' for m in modules).items())),
                parallel_free_gaps=_distribution(gaps),back_to_back_contact_pairs=len(contacts),
                reconstructed_same_run_gaps=_distribution(g for r in runs for g in r['same_run_gaps_mm']),
                area_mm2=union.area)
    if include_density:
        values['density']=dict(confirmed_space_area_mm2=scope.area if scope is not None else None,
                             confirmed_space_density=union.area/scope.area if scope is not None and scope.area>0 else None,
                             shelf_envelope_proxy_area_mm2=union.envelope.area,
                             shelf_envelope_proxy_density=union.area/union.envelope.area,
                             denominator_note='Shelf envelope proxy is not store area; it ignores aisles outside the shelf envelope and may enclose disjoint zones')
    return values,runs


def _check_actual(layout,modules,reconstructed):
    violations=[]
    scope=Polygon(layout.space.boundary.boundary_mm,layout.space.boundary.holes_mm)
    if not scope.is_valid or scope.area<=0:raise ValueError('Invalid PlanningSpace polygon')
    lookup={m['module_id']:m for m in modules}
    if len(lookup)!=len(modules):violations.append('DUPLICATE_SHELF_ID')
    polys=[Polygon(m['footprint_mm']) for m in modules]
    outside=sum(p.difference(scope).area>AREA_EPS_MM2 for p in polys)
    if outside:violations.append('SHELF_OUTSIDE_CONFIRMED_GEOMETRY_OR_IN_HOLE')
    if any(p.distance(scope.boundary)+NUMERIC_MM<layout.rules.boundary_clearance_mm for p in polys):
        violations.append('BOUNDARY_OR_HOLE_CLEARANCE')
    obstacles=[Polygon(e.boundary_mm,e.holes_mm) for e in layout.space.exclusions+layout.space.entrances]
    if any(p.intersection(o).area>AREA_EPS_MM2 for p in polys for o in obstacles):violations.append('EXCLUSION_OR_ENTRANCE_INTRUSION')
    walls=[LineString([w.start_mm,w.end_mm]) for w in layout.space.barriers]
    if any(p.distance(w)+NUMERIC_MM<layout.rules.wall_clearance_mm or p.intersection(w).length>NUMERIC_MM for p in polys for w in walls):
        violations.append('WALL_INTRUSION_OR_CLEARANCE')
    overlaps=sum(p.intersection(q).area>AREA_EPS_MM2 for i,p in enumerate(polys) for q in polys[:i])
    if overlaps:violations.append('ILLEGAL_MODULE_OVERLAP')
    for m,shelf in zip(modules,layout.shelves):
        angle_diff=abs((m['direction_deg']-shelf.rotation_deg+90)%180-90)
        if (abs(m['length_mm']-shelf.length_mm)>NUMERIC_MM or abs(m['depth_mm']-shelf.depth_mm)>NUMERIC_MM
            or angle_diff>.001 or m['rectangular_fill_ratio']<.999999):violations.append('SHELF_SPEC_FOOTPRINT_MISMATCH')
    flat=[s for r in layout.runs for s in r.modules]
    expected={s.id:s.model_dump(mode='json') for s in layout.shelves}
    if len(flat)!=len(layout.shelves) or Counter(s.id for s in flat)!=Counter(s.id for s in layout.shelves):
        violations.append('DECLARED_RUN_MEMBERSHIP_MISMATCH')
    if any(expected.get(s.id)!=s.model_dump(mode='json') for s in flat):violations.append('NESTED_MODULE_GEOMETRY_MISMATCH')
    declared_gaps=[];declared_lateral=[]
    for run in layout.runs:
        if any(s.id not in lookup for s in run.modules):continue
        ps=sorted(projected(Polygon(lookup[s.id]['footprint_mm']),run.direction_deg) for s in run.modules)
        for a,b in zip(ps,ps[1:]):
            declared_gaps.append(b[0]-a[1]);declared_lateral.append(abs((a[2]+a[3]-b[2]-b[3])/2))
        if len(run.modules)<layout.rules.min_modules_per_run:violations.append('SHORT_DECLARED_RUN')
    if any(abs(g)>NUMERIC_MM for g in declared_gaps) or any(g>NUMERIC_MM for g in declared_lateral):
        violations.append('DECLARED_RUN_NOT_CONTINUOUS_AND_COLLINEAR')
    if any(r['module_count']<layout.rules.min_modules_per_run for r in reconstructed):violations.append('RECONSTRUCTED_SHORT_FRAGMENT_RUN')
    aisle_gaps=[]
    for i,a in enumerate(reconstructed):
        pa=Polygon(a['footprint_mm'])
        for b in reconstructed[i+1:]:
            pb=Polygon(b['footprint_mm'])
            # Unlike reference descriptive neighbors, production safety also
            # checks opposing/end-to-end and orthogonal independent runs.
            if pa.distance(pb)+NUMERIC_MM<layout.rules.aisle_width_mm:
                violations.append('INTER_RUN_CLEARANCE')
        for n in a['neighbors']:
            if a['run_id']<n['run_id']:aisle_gaps.append(n['clear_gap_mm'])
    return dict(violation_codes=sorted(set(violations)),outside_or_hole_module_count=outside,
                overlapping_module_pair_count=overlaps,declared_same_run_gaps=_distribution(declared_gaps),
                declared_same_run_lateral_offsets=_distribution(declared_lateral),
                inter_run_clearances=_distribution(aisle_gaps),
                end_aisle_status='NOT_CHECKED_USE_FULL_PRODUCTION_VALIDATOR',
                authenticated_confirmation_status='NOT_VERIFIED_BY_THIS_PURE_COMPARATOR',
                note='Independent checks of supplied output geometry; not authenticated human confirmation and not a replacement for the full production safety validator')


def compare_layout(layout:LayoutV2,base_profile:dict,supplement:dict,*,include_density=True)->dict:
    """Return measured diagnostics; Gate C can only FAIL or REQUIRE_REVIEW here.

    There is intentionally no ACCEPTED/approval argument. Caller-owned reference
    hash verification and human review are separate gates, never inferred from
    layout.metrics, layout.validation, design_status or confirmation notes.
    """
    measured=[_measured(s.id,s.footprint_mm) for s in layout.shelves]
    confirmed=layout.space.confirmation.state=='CONFIRMED'
    scope=Polygon(layout.space.boundary.boundary_mm,layout.space.boundary.holes_mm)
    auto,auto_runs=_structure(measured,NUMERIC_MM,scope if confirmed else None,include_density=include_density)
    if not confirmed and include_density:
        auto['density']['input_geometry_area_mm2']=scope.area
        auto['density']['input_geometry_density']=auto['area_mm2']/scope.area
    reference_rows=[r for r in base_profile['classification'] if r['classification']=='ACTUAL']
    reference_rows+= [r for r in supplement['line_classification'] if r['classification']=='ACTUAL']
    reference=[_measured(str(i),r['footprint_mm']) for i,r in enumerate(reference_rows)]
    ref,ref_runs=_structure(reference,1.0,include_density=include_density)
    # Rebuild the explicitly separate 6mm LINE diagnostic; do not apply its
    # source-specific lateral tolerance to the automatic output or INSERTs.
    insert_modules=[_measured(str(i),r['footprint_mm']) for i,r in enumerate(base_profile['classification']) if r['classification']=='ACTUAL']
    line_modules=[_measured(str(i),r['footprint_mm']) for i,r in enumerate(supplement['line_classification']) if r['classification']=='ACTUAL']
    ref['near_collinear_diagnostic_run_count']=len(group_runs(insert_modules,tol=1))+len(group_runs(line_modules,tol=6))
    ref['verified_subsets_only']=True
    ref['whole_design_inventory_count']=None
    checks=_check_actual(layout,measured,auto_runs)
    material_counts=Counter(s.material_id for s in layout.shelves)
    auto['material_id_counts']=dict(sorted(material_counts.items()))
    auto['distinct_material_count']=len(material_counts)
    auto['mixed_material_runs']=sum(len({s.material_id for s in r.modules})>1 for r in layout.runs)
    auto['declared_run_count']=len(layout.runs)
    auto['declared_tail_waste_mm']=sum(r.remaining_length_mm for r in layout.runs)
    auto['tail_waste_status']='DECLARED_NOT_INDEPENDENTLY_PROVEN_AVAILABLE_CAPACITY'
    hard_fail=bool(checks['violation_codes'])
    prompts=[]
    if len(material_counts)==1:prompts.append('SINGLE_TEMPLATE_REQUIRES_EXPLANATION_OF_AVAILABLE_LENGTH_AND_REJECTED_ALTERNATIVES')
    if auto['mean_modules_per_run']<ref['mean_modules_per_run']:prompts.append('LOWER_MEAN_MODULES_PER_RUN_REQUIRES_SPACE_AND_RULE_CONTEXT')
    if not layout.space.entrances:prompts.append('ENTRANCE_SEMANTICS_REQUIRE_REVIEW')
    if layout.space.unresolved:prompts.append('PLANNING_SPACE_UNRESOLVED_ITEMS_REQUIRE_REVIEW')
    return dict(schema_version='offline-reference-comparison-1.0',
                input_state=layout.space.confirmation.state,automatic=auto,reference=ref,geometry_checks=checks,
                automatic_structure_status='FAIL' if hard_fail else 'PASS',
                gate_c_status='FAIL' if hard_fail else 'REQUIRES_REVIEW',
                product_acceptance_status='NOT_ASSESSED',
                reference_hash_status='CALLER_MUST_VERIFY_FROZEN_BYTES_OUTSIDE_THIS_FUNCTION',
                comparability=dict(same_confirmed_physical_space='NOT_ESTABLISHED',
                                   physical_store_density_comparison='UNAVAILABLE_REFERENCE_AREA_UNKNOWN',
                                   total_length_or_count_ratio_gate='DISABLED_DIFFERENT_SPACES_AND_RULES',
                                   shelf_envelope_density='DESCRIPTIVE_PROXY_ONLY_NOT_A_PASS_THRESHOLD' if include_density else 'NOT_COMPUTED',
                                   reference_insert_parallel_gaps_mm=list(base_profile['metrics']['parallel_free_gaps_mm']),
                                   production_aisle_width_mm=layout.rules.aisle_width_mm,
                                   reference_aisle_compliance='NOT_ASSUMED; measured reference INSERT gaps differ from production rules'),
                review_prompts=prompts,
                functional_review=dict(status='REQUIRES_EXTERNAL_SPATIAL_AND_2D_REVIEW',
                                       reference_text_relation_count=len(base_profile.get('functional_relations',[])),
                                       reference_semantics='TEXT_ANCHOR_NEIGHBOR_RELATIONS_ONLY_NOT_CONFIRMED_ZONE_POLYGONS',
                                       supplied_entrance_count=len(layout.space.entrances),
                                       supplied_exclusion_count=len(layout.space.exclusions),
                                       unresolved_space_items=list(layout.space.unresolved)),
                required_external_reviews=['Authenticated real PlanningSpace confirmation, including actual scope, entrances and functional exclusions',
                                           'Independent side-by-side 2D review of both reference subsets and automatic result with space/rule differences explained',
                                           'Functional-zone relationships and entrance usability; nearest text anchors are not confirmed zone polygons',
                                           'Human product-quality acceptance; supplied ACCEPTED/validation metadata cannot satisfy this gate'],
                synthetic_gate_policy='SYNTHETIC_TEST never satisfies mature-design Gate C or real-CAD Gate E')
