import json
import itertools
from pathlib import Path
import pytest
from shapely.geometry import Polygon, LineString
from shared.contracts import Business, Wall
from shared.planning_v2 import space_geometry_digest
from services.planning.runs import generate_layout, validate_runs
from services.planning.engine import PlanningError
from tools.benchmarks.spaces import cases,request

ROOT=Path(__file__).resolve().parents[2]

def make_request(name):
    catalog=json.loads((ROOT/'input/production/materials/material_templates.v1.json').read_text(encoding='utf-8-sig'))
    business=Business(products=[],product_count=0,templates=catalog['templates'],sources=[])
    return request(name,cases()[name],business)

def assert_independent_safety(req,result):
    scope=Polygon(req.space.boundary.boundary_mm,req.space.boundary.holes_mm)
    walls=[LineString([w.start_mm,w.end_mm]) for w in req.space.barriers]
    zones=[Polygon(z.boundary_mm,z.holes_mm) for z in req.space.exclusions+req.space.entrances]
    for run in result.runs:
        assert len(run.modules)>=req.rules.min_modules_per_run
        polygon=Polygon(run.footprint_mm)
        assert scope.covers(polygon)
        assert polygon.distance(scope.boundary)>=req.rules.boundary_clearance_mm-1e-6
        assert all(not polygon.intersects(wall) and polygon.distance(wall)>=req.rules.wall_clearance_mm-1e-6 for wall in walls)
        assert all(not polygon.intersects(zone) for zone in zones)
        for a,b in zip(run.modules,run.modules[1:]):
            assert Polygon(a.footprint_mm).distance(Polygon(b.footprint_mm))==pytest.approx(0)
    for a,b in itertools.combinations(result.runs,2):
        assert Polygon(a.footprint_mm).distance(Polygon(b.footprint_mm))>=req.rules.aisle_width_mm-1e-6
    assert result==generate_layout(req)
    assert validate_runs(req,result)['status']=='PASS'

def record_stage(name,req,result):
    destination=ROOT/'outputs/iterations/M02'
    destination.mkdir(parents=True,exist_ok=True)
    (destination/f'{name}.json').write_text(result.model_dump_json(indent=2),encoding='utf-8')
    old=json.loads((ROOT/'outputs/baselines/SYNTHETIC_BASELINE/metrics.json').read_text(encoding='utf-8-sig'))
    report={'case':name,'before':next(row for row in old if row['case']==name),
            'after':result.metrics,'validation':result.validation}
    (destination/f'{name}_comparison.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps({'case':name,'module_count':len(result.shelves),'run_count':len(result.runs),
        'effective_length_mm':result.metrics['effective_length_mm'],'isolated_ratio':result.metrics['isolated_run_ratio']}))

def test_column():
    req=make_request('column')
    result=generate_layout(req)
    assert_independent_safety(req,result)
    record_stage('column',req,result)

def test_wall():
    req=make_request('wall')
    result=generate_layout(req)
    assert_independent_safety(req,result)
    record_stage('wall',req,result)

def test_diagonal_wall_and_nondefault_clearance():
    req=make_request('wall')
    req.space.barriers=[Wall(id='diagonal',start_mm=(5000,3000),end_mm=(12000,8500))]
    req.space.confirmation.geometry_sha256=space_geometry_digest(req.space)
    req.rules.boundary_clearance_mm=300
    req.rules.wall_clearance_mm=50
    result=generate_layout(req)
    assert_independent_safety(req,result)
    # Physical terminal rectangles are clear of the wall, not just a scalar
    # claim in the metadata. They may touch the exterior at their far edge.
    wall=LineString([req.space.barriers[0].start_mm,req.space.barriers[0].end_mm])
    for run in result.runs:
        u,v=run.start_mm if run.direction_deg==0 else run.start_mm[::-1]
        length=run.used_length_mm
        for left,right in [(u-req.rules.end_aisle_mm,u),(u+length,u+length+req.rules.end_aisle_mm)]:
            points=[(left,v-run.depth_mm/2),(right,v-run.depth_mm/2),(right,v+run.depth_mm/2),(left,v+run.depth_mm/2)]
            if run.direction_deg==90: points=[(y,x) for x,y in points]
            corridor=Polygon(points)
            assert not corridor.buffer(-1e-6).intersects(wall)

def test_zero_length_wall_rejected():
    req=make_request('wall')
    req.space.barriers[0].end_mm=req.space.barriers[0].start_mm
    req.space.confirmation.geometry_sha256=space_geometry_digest(req.space)
    with pytest.raises(PlanningError,match='墙线'): generate_layout(req)

def test_hole():
    req=make_request('hole')
    result=generate_layout(req)
    assert_independent_safety(req,result)
    record_stage('hole',req,result)

def test_unclosed_hole_rejected():
    req=make_request('hole')
    req.space.boundary.holes_mm[0]=req.space.boundary.holes_mm[0][:-1]
    req.space.confirmation.geometry_sha256=space_geometry_digest(req.space)
    with pytest.raises(PlanningError,match='显式闭合'): generate_layout(req)

def test_shifted_module_into_hole_rejected():
    req=make_request('hole')
    result=generate_layout(req)
    shelf=result.runs[0].modules[0]
    shelf.x_mm=9000
    shelf.y_mm=6000
    shelf.footprint_mm=[(8400,5800),(9600,5800),(9600,6200),(8400,6200),(8400,5800)]
    with pytest.raises(PlanningError): validate_runs(req,result)

def test_entrance():
    req=make_request('entrance')
    result=generate_layout(req)
    assert_independent_safety(req,result)
    assert result.validation['entrance_nonoccupation']=='PASS'
    assert 'no whole-store reachability' in result.validation['circulation_scope']
    record_stage('entrance',req,result)

def test_entrance_added_over_existing_layout_cannot_reuse_it():
    req=make_request('entrance')
    result=generate_layout(req)
    req.space.entrances[0].boundary_mm=list(result.runs[0].footprint_mm)
    req.space.confirmation.geometry_sha256=space_geometry_digest(req.space)
    # Even if an attacker replaces output space to match the new digest, the
    # physical layout must be rechecked and rejected at the occupied entrance.
    result.space=req.space.model_copy(deep=True)
    with pytest.raises(PlanningError): validate_runs(req,result)

def test_concave():
    req=make_request('concave')
    result=generate_layout(req)
    assert_independent_safety(req,result)
    actual=Polygon(req.space.boundary.boundary_mm)
    assert actual.area < Polygon([(0,0),(18000,0),(18000,12000),(0,12000)]).area
    record_stage('concave',req,result)

def test_obstacle_translation_covariance():
    req=make_request('hole')
    original=generate_layout(req)
    dx,dy=151337,-95331
    req.space.boundary.boundary_mm=[(x+dx,y+dy) for x,y in req.space.boundary.boundary_mm]
    req.space.boundary.holes_mm=[[(x+dx,y+dy) for x,y in h] for h in req.space.boundary.holes_mm]
    req.space.confirmation.geometry_sha256=space_geometry_digest(req.space)
    shifted=generate_layout(req)
    assert len(original.shelves)==len(shifted.shelves)
    for a,b in zip(original.shelves,shifted.shelves):
        assert a.material_id==b.material_id
        assert b.x_mm-a.x_mm==pytest.approx(dx)
        assert b.y_mm-a.y_mm==pytest.approx(dy)

def test_six_rectangle_structures_preserved():
    for name in ['square','long','wide','medium','large','translated']:
        previous=json.loads((ROOT/f'outputs/iterations/M01/{name}.json').read_text(encoding='utf-8-sig'))
        current=generate_layout(make_request(name)).model_dump(mode='json')
        for field in ['shelves','runs','bom']:
            assert current[field]==previous[field]

def test_obstacle_invalid_ring_and_full_block_rejected():
    req=make_request('column')
    req.space.exclusions[0].boundary_mm=[(0,0),(18000,12000),(0,12000),(18000,0),(0,0)]
    req.space.confirmation.geometry_sha256=space_geometry_digest(req.space)
    with pytest.raises(PlanningError,match='拓扑'): generate_layout(req)
    req.space.exclusions[0].boundary_mm=list(req.space.boundary.boundary_mm)
    req.space.confirmation.geometry_sha256=space_geometry_digest(req.space)
    with pytest.raises(PlanningError,match='没有可容纳'): generate_layout(req)
