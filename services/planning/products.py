"""Pure category zoning and explicitly synthetic shelf-level placement.

Project heuristics only: no source reads, inferred product dimensions, demand
prediction, general optimizer, CAD understanding, or reference-design inputs.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict

from shapely.geometry import Polygon, mapping
from shapely.ops import unary_union

from shared.contracts import Business
from shared.planning_v2 import ProductPlacementPlan, ShelfRun, ShelfLevel, SyntheticProduct

EPS = 1e-6


def _snake_slots(runs):
    ids=[s.id for r in runs for s in r.modules]
    if len(ids)!=len(set(ids)):raise ValueError('DUPLICATE_SHELF_ID')
    if len({r.run_id for r in runs})!=len(runs):raise ValueError('DUPLICATE_RUN_ID')
    groups=defaultdict(list)
    for r in runs:
        if not r.modules:raise ValueError('EMPTY_RUN')
        groups[r.direction_deg].append(r)
    slots=[]
    for direction,rows in sorted(groups.items()):
        u,v=(0,1) if direction==0 else (1,0)
        center=lambda r:Polygon(r.footprint_mm).centroid.coords[0]
        rows.sort(key=lambda r:(center(r)[v],center(r)[u],r.run_id))
        for row_index,r in enumerate(rows):
            modules=sorted(r.modules,key=lambda s:((s.x_mm,s.y_mm)[u],s.id))
            if row_index%2:modules.reverse()
            for s in modules:
                p=Polygon(s.footprint_mm)
                if not p.is_valid or p.area<=0:raise ValueError('INVALID_SHELF_FOOTPRINT')
                slots.append((r.run_id,s))
    return slots


def assign_categories(business:Business,runs:list[ShelfRun])->ProductPlacementPlan:
    """Hamilton quotas based on record counts, with one slot per supported
    category when possible; assign contiguous segments of a spatial snake."""
    if business.product_count!=len(business.products):raise ValueError('PRODUCT_RECORD_COUNT_MISMATCH')
    categories=defaultdict(list);missing=[]
    for i,p in enumerate(business.products):
        raw=p.get('category')
        if not isinstance(raw,str) or not raw.strip() or not raw.split('>')[0].strip():
            missing.append((i,p));continue
        categories[raw.split('>')[0].strip()].append((i,p,raw))
    slots=_snake_slots(runs)
    order=sorted(categories,key=lambda c:(-len(categories[c]),c))
    selected=order[:len(slots)]
    quotas={c:1 for c in selected}
    remaining=len(slots)-len(selected)
    if selected and remaining:
        denominator=sum(len(categories[c]) for c in selected)
        numerators={c:remaining*len(categories[c]) for c in selected}
        for c in selected:quotas[c]+=numerators[c]//denominator
        remainder=remaining-sum(n//denominator for n in numerators.values())
        for c in sorted(selected,key=lambda c:(-(numerators[c]%denominator),c))[:remainder]:quotas[c]+=1
    assignments=[];unassigned=[];offset=0
    for c in order:
        records=categories[c]
        paths=[dict(source_category_path=p,record_count=n) for p,n in sorted(Counter(raw for _,_,raw in records).items())]
        if c not in quotas:
            unassigned.append(dict(category=c,record_count=len(records),source_paths=paths,reason='INSUFFICIENT_SHELF_SLOTS'));continue
        chosen=slots[offset:offset+quotas[c]];offset+=quotas[c]
        by_run=defaultdict(list)
        for rid,s in chosen:by_run[rid].append(s.id)
        footprints=[Polygon(s.footprint_mm) for _,s in chosen]
        zone=unary_union(footprints)
        assignments.append(dict(category=c,record_count=len(records),source_paths=paths,
                                initial_shelf_quota=quotas[c],quota_basis='SOURCE_RECORD_COUNT_NOT_SALES_OR_DEMAND',
                                zone_id=f'category-zone-{len(assignments)+1:03}',
                                zone_geometry_mm=mapping(zone),zone_geometry_basis='EXACT_UNION_OF_ASSIGNED_SHELF_FOOTPRINTS_NO_HULL',
                                run_assignments=[dict(run_id=rid,shelf_ids=sids) for rid,sids in by_run.items()],
                                shelf_ids=[s.id for _,s in chosen],
                                adjacency_policy='CONTIGUOUS_SEGMENT_OF_DIRECTION_GROUPED_SERPENTINE_SHELF_TRAVERSAL',
                                scope='CATEGORY_LABEL_ONLY_NOT_PRECISE_SKU_OR_CAPACITY_PLAN'))
    if missing:unassigned.append(dict(category=None,record_count=len(missing),source_paths=[],reason='MISSING_OR_INVALID_SOURCE_CATEGORY'))
    unassigned_products=[]
    for i,p in enumerate(business.products):
        raw=p.get('category');top=raw.split('>')[0].strip() if isinstance(raw,str) and raw.strip() else None
        unassigned_products.append(dict(source_record_index=i,source_row=p.get('source_row'),barcode=p.get('barcode'),
                                        source_category_path=raw,category=top,
                                        category_status='ASSIGNED' if top in quotas else 'UNASSIGNED',
                                        reason='UNAVAILABLE-DUE-TO-MISSING-PHYSICAL-DATA'))
    return ProductPlacementPlan(category_basis='Top level of source category split on >; exact full paths retained. Minimum-one plus proportional source-record quotas; not sales, facing, stock or capacity optimization.',
                                category_assignments=assignments,unassigned_categories=unassigned,
                                unassigned_products=unassigned_products)


def _synthetic_inputs(products,levels):
    ps=[SyntheticProduct.model_validate(p.model_dump()) for p in products]
    ls=[ShelfLevel.model_validate(l.model_dump()) for l in levels]
    if len({p.product_id for p in ps})!=len(ps):raise ValueError('DUPLICATE_PRODUCT_ID')
    if len({(l.shelf_id,l.level_index) for l in ls})!=len(ls):raise ValueError('DUPLICATE_SHELF_LEVEL')
    if any(not p.product_id.strip() or not p.category.strip() for p in ps):raise ValueError('EMPTY_PRODUCT_ID_OR_CATEGORY')
    if any(not l.shelf_id.strip() for l in ls):raise ValueError('EMPTY_SHELF_ID')
    return ps,sorted(ls,key=lambda l:(l.shelf_id,l.level_index))


def _orientations(p):
    variants=[(False,p.width_mm,p.depth_mm)]
    if p.rotation_policy=='ALLOW_XY_SWAP' and p.width_mm!=p.depth_mm:variants.append((True,p.depth_mm,p.width_mm))
    return variants


def _reason(p,levels,used):
    if not levels:return 'NO_LEVELS'
    same=[l for l in levels if l.category is None or l.category==p.category]
    if not same:return 'CATEGORY_MISMATCH'
    high=[l for l in same if p.height_mm<=l.clear_height_mm+EPS]
    if not high:return 'TOO_HIGH'
    deep=[(l,w) for l in high for _,w,d in _orientations(p) if d<=l.depth_mm+EPS]
    if not deep:return 'TOO_DEEP'
    wide=[(l,w) for l,w in deep if w*p.minimum_facings<=l.width_mm+EPS]
    if not wide:return 'TOO_WIDE_FOR_MINIMUM_FACINGS'
    if not any(used.get((l.shelf_id,l.level_index),0)+w*p.minimum_facings<=l.width_mm+EPS for l,w in wide):return 'CAPACITY_EXHAUSTED'
    return 'AVAILABLE_CAPACITY'


def allocate_synthetic(products:list[SyntheticProduct],levels:list[ShelfLevel])->dict:
    """Best-fit row packing of minimum facings, in deterministic demand order.

    One SKU group goes to one provided level, at its minimum facings. Layer
    height is explicit input, never inferred from real material defaults.
    """
    products,levels=_synthetic_inputs(products,levels)
    used=defaultdict(float);assigned=[];unassigned=[]
    for p in sorted(products,key=lambda p:(-p.demand_weight,p.product_id)):
        choices=[]
        for l in levels:
            if l.category is not None and l.category!=p.category:continue
            if p.height_mm>l.clear_height_mm+EPS:continue
            key=(l.shelf_id,l.level_index)
            for rotated,width,depth in _orientations(p):
                total=width*p.minimum_facings
                if depth<=l.depth_mm+EPS and used[key]+total<=l.width_mm+EPS:
                    choices.append((l.width_mm-used[key]-total,l.shelf_id,l.level_index,rotated,width,depth))
        if not choices:
            unassigned.append(dict(product_id=p.product_id,reason=_reason(p,levels,used)));continue
        _,sid,index,rotated,width,depth=min(choices)
        key=(sid,index);start=used[key];end=start+width*p.minimum_facings
        assigned.append(dict(product_id=p.product_id,category=p.category,shelf_id=sid,level_index=index,
                             facings=p.minimum_facings,rotated_xy=rotated,unit_width_mm=width,unit_depth_mm=depth,
                             height_mm=p.height_mm,x_start_mm=start,x_end_mm=end,y_start_mm=0.0,
                             synthetic=True))
        used[key]=end
    result=dict(schema_version='synthetic-placement-1.0',data_status='SYNTHETIC_ONLY_NOT_REAL_SKU',
                algorithm='Demand-weight descending then product ID; best-fit remaining width; minimum facings; one row and one level per SKU',
                assignments=assigned,unassigned=unassigned,
                level_usage=[dict(shelf_id=l.shelf_id,level_index=l.level_index,used_width_mm=used[(l.shelf_id,l.level_index)],
                                  remaining_width_mm=l.width_mm-used[(l.shelf_id,l.level_index)]) for l in levels])
    result['validation']=validate_synthetic(products,levels,result)
    if result['validation']['status']!='PASS':raise ValueError('SYNTHETIC_RESULT_FAILED_INDEPENDENT_VALIDATION')
    return result


def validate_synthetic(products:list[SyntheticProduct],levels:list[ShelfLevel],result:dict)->dict:
    """Independently check supplied allocations; never call the allocator."""
    products,levels=_synthetic_inputs(products,levels)
    ps={p.product_id:p for p in products};ls={(l.shelf_id,l.level_index):l for l in levels}
    errors=[];seen=[];spans=defaultdict(list);used=defaultdict(float)
    assignments=result.get('assignments',[]);unassigned=result.get('unassigned',[])
    for a in assignments:
        pid=a.get('product_id');seen.append(pid)
        if pid not in ps:errors.append('UNKNOWN_ASSIGNED_PRODUCT');continue
        p=ps[pid];key=(a.get('shelf_id'),a.get('level_index'));l=ls.get(key)
        if l is None:errors.append('UNKNOWN_ASSIGNED_LEVEL');continue
        if l.category is not None and l.category!=p.category:errors.append('CATEGORY_MISMATCH')
        rotated=a.get('rotated_xy');facings=a.get('facings')
        if not isinstance(rotated,bool):errors.append('INVALID_ROTATION_FLAG');continue
        if rotated and p.rotation_policy!='ALLOW_XY_SWAP':errors.append('ROTATION_NOT_ALLOWED')
        if isinstance(facings,bool) or not isinstance(facings,int) or facings<p.minimum_facings:errors.append('MINIMUM_FACINGS_NOT_MET');continue
        width,depth=(p.depth_mm,p.width_mm) if rotated else (p.width_mm,p.depth_mm)
        required={'unit_width_mm':width,'unit_depth_mm':depth,'height_mm':p.height_mm}
        numeric=['x_start_mm','x_end_mm','y_start_mm',*required]
        if any(isinstance(a.get(k),bool) or not isinstance(a.get(k),(int,float)) or not math.isfinite(a[k]) for k in numeric):
            errors.append('NONFINITE_OR_MISSING_PLACEMENT_GEOMETRY');continue
        if any(abs(a[k]-value)>EPS for k,value in required.items()):errors.append('PRODUCT_DIMENSION_MISMATCH')
        start,end=a['x_start_mm'],a['x_end_mm']
        if start<-EPS or end>l.width_mm+EPS or abs((end-start)-width*facings)>EPS:errors.append('WIDTH_OR_FACINGS_GEOMETRY')
        if a['y_start_mm']<-EPS or a['y_start_mm']+depth>l.depth_mm+EPS:errors.append('DEPTH_LIMIT')
        if p.height_mm>l.clear_height_mm+EPS:errors.append('HEIGHT_LIMIT')
        if a.get('category')!=p.category:errors.append('PRODUCT_CATEGORY_MISMATCH')
        if a.get('synthetic') is not True:errors.append('MISSING_SYNTHETIC_MARKER')
        spans[key].append((start,end));used[key]=max(used[key],end)
    for key,intervals in spans.items():
        ordered=sorted(intervals)
        if any(a[1]>b[0]+EPS for a,b in zip(ordered,ordered[1:])):errors.append('OVERLAPPING_FACING_GROUPS')
    for item in unassigned:
        pid=item.get('product_id');seen.append(pid)
        if pid not in ps:errors.append('UNKNOWN_UNASSIGNED_PRODUCT');continue
        expected=_reason(ps[pid],levels,used)
        if expected=='AVAILABLE_CAPACITY':errors.append('UNASSIGNED_WITH_AVAILABLE_CAPACITY')
        elif item.get('reason')!=expected:errors.append('INCORRECT_UNASSIGNED_REASON')
    if len(seen)!=len(set(seen)):errors.append('DUPLICATE_PRODUCT_ALLOCATION')
    if set(seen)!=set(ps):errors.append('PRODUCT_ACCOUNTING_MISMATCH')
    if result.get('data_status')!='SYNTHETIC_ONLY_NOT_REAL_SKU':errors.append('MISSING_SYNTHETIC_DATA_STATUS')
    usage=result.get('level_usage',[])
    usage_keys=[(row.get('shelf_id'),row.get('level_index')) for row in usage]
    if len(usage_keys)!=len(set(usage_keys)) or set(usage_keys)!=set(ls):errors.append('LEVEL_USAGE_ACCOUNTING_MISMATCH')
    for row in usage:
        key=(row.get('shelf_id'),row.get('level_index'))
        if key not in ls:continue
        expected={'used_width_mm':used[key],'remaining_width_mm':ls[key].width_mm-used[key]}
        if any(isinstance(row.get(k),bool) or not isinstance(row.get(k),(int,float)) or not math.isfinite(row[k]) or abs(row[k]-value)>EPS for k,value in expected.items()):
            errors.append('LEVEL_USAGE_GEOMETRY_MISMATCH')
    return dict(status='FAIL' if errors else 'PASS',violations=sorted(set(errors)),assigned_count=len(assignments),
                unassigned_count=len(unassigned),source_product_count=len(products),
                constraint_scope='Explicit synthetic width/depth/height, minimum facings, XY rotation, category, uniqueness and nonoverlapping width intervals')
