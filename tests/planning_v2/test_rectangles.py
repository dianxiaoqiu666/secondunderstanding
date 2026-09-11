import itertools
import json
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from shared.contracts import Business, Template
from shared.planning_v2 import space_geometry_digest
from services.planning.engine import PlanningError
from services.planning.runs import combine_modules, generate_layout, validate_runs
from tools.benchmarks.spaces import cases, rectangle, request

ROOT = Path(__file__).resolve().parents[2]
RECTANGLES = ['square', 'long', 'wide', 'medium', 'large', 'translated']


@pytest.fixture
def business():
    catalog = json.loads((ROOT/'input/production/materials/material_templates.v1.json').read_text(encoding='utf-8-sig'))
    return Business(products=[], product_count=0, templates=catalog['templates'], sources=[])


@pytest.mark.parametrize('name', RECTANGLES)
def test_rectangle_structure_independent_geometry_and_determinism(name, business):
    req = request(name, cases()[name], business)
    result = generate_layout(req)
    assert result == generate_layout(req)
    assert validate_runs(req,result)['status'] == 'PASS'
    assert result.shelves == [s for r in result.runs for s in r.modules]
    assert all(len(r.modules) >= 2 for r in result.runs)
    assert result.products.real_sku_status == 'UNAVAILABLE-DUE-TO-MISSING-PHYSICAL-DATA'
    scope = Polygon(req.space.boundary.boundary_mm)
    for run in result.runs:
        assert scope.covers(Polygon(run.footprint_mm))
        for a,b in zip(run.modules,run.modules[1:]):
            pa,pb=Polygon(a.footprint_mm),Polygon(b.footprint_mm)
            assert pa.distance(pb) == pytest.approx(0)
            assert pa.intersection(pb).area == pytest.approx(0)
            assert pa.intersection(pb).length == pytest.approx(run.depth_mm)
    for a,b in itertools.combinations(result.runs,2):
        assert Polygon(a.footprint_mm).distance(Polygon(b.footprint_mm)) >= req.rules.aisle_width_mm-1e-6
    assert sum(row.quantity for row in result.bom) == len(result.shelves)
    assert sum(row.total_level_count for row in result.bom) == sum(s.default_level_count for s in result.shelves)


def test_translation_covariance(business):
    origin = generate_layout(request('origin', rectangle('origin',18000,12000),business))
    shifted = generate_layout(request('shifted', rectangle('shifted',18000,12000,(135000,-97000)),business))
    assert len(origin.shelves)==len(shifted.shelves)
    for a,b in zip(origin.shelves,shifted.shelves):
        assert a.material_id == b.material_id
        assert a.rotation_deg == b.rotation_deg
        assert b.x_mm-a.x_mm == pytest.approx(135000)
        assert b.y_mm-a.y_mm == pytest.approx(-97000)
    assert origin.bom == shifted.bom


def test_mixed_6300_exact_minimum_modules(business):
    subset=[t for t in business.templates if t.depth_mm==400]
    result=combine_modules(6300,subset)
    assert result['used_length_mm']==6300
    assert result['remaining_length_mm']==0
    assert result['module_count']==4
    assert len(set(result['material_ids']))>1
    assert result == combine_modules(6300,list(reversed(subset)))


@pytest.mark.parametrize('available', [2400,2500,2999.5,3000,3599.999,4500,6300,7100])
def test_dp_matches_exhaustive_oracle(available,business):
    subset=sorted([t for t in business.templates if t.depth_mm==400],key=lambda t:t.material_id)
    options=[]
    for counts in itertools.product(range(int(available/1200)+1),repeat=len(subset)):
        length=sum(t.length_mm*c for t,c in zip(subset,counts))
        ids=tuple(t.material_id for t,c in zip(subset,counts) for _ in range(c))
        if len(ids)>=2 and length<=available:
            options.append((available-length,len(ids),ids))
    oracle=min(options)
    actual=combine_modules(available,subset)
    assert (actual['remaining_length_mm'],actual['module_count'],tuple(actual['material_ids']))==oracle


def test_data_driven_1250_template_improves_tail(business):
    subset=[t for t in business.templates if t.depth_mm==400]
    before=combine_modules(2500,subset)
    extra=Template(material_id='SHELF-1250-400',length_mm=1250,depth_mm=400,default_level_count=5)
    after=combine_modules(2500,[*subset,extra])
    assert before['remaining_length_mm']==100
    assert after['remaining_length_mm']==0
    assert after['material_ids']==[extra.material_id]*2
    business.templates.append(extra)
    req=request('new-template',rectangle('new-template',4900,4000),business)
    req.rules.orientation_candidates=[0]
    result=generate_layout(req)
    assert any(s.material_id==extra.material_id for s in result.shelves)


def test_single_long_template_does_not_hide_valid_two_modules():
    templates=[Template(material_id='a',length_mm=3000,depth_mm=400,default_level_count=4),
               Template(material_id='b',length_mm=1500,depth_mm=400,default_level_count=4)]
    assert combine_modules(3000,templates)['material_ids']==['b','b']
    assert combine_modules(2000,templates)['reason']=='SHORT_FRAGMENT_BELOW_MIN_MODULES'


@pytest.mark.parametrize('change', ['state','digest','source','unresolved'])
def test_confirmation_rejects_stale_or_unreviewed_space(change,business):
    req=request('square',cases()['square'],business)
    if change=='state': req.space.confirmation.state='REQUIRES_CONFIRMATION'
    elif change=='digest': req.space.boundary.boundary_mm[0]=(1,1)
    elif change=='source': req.space.confirmation.source_sha256='0'*64
    else: req.space.unresolved=['real store edge unknown']
    with pytest.raises(PlanningError): generate_layout(req)


@pytest.mark.parametrize('change', ['gap','flat','bom','used','center','end'])
def test_validator_rejects_tampered_output(change,business):
    req=request('square',cases()['square'],business)
    result=generate_layout(req)
    if change=='gap':
        result.runs[0].modules[1].footprint_mm=[(x+10,y) for x,y in result.runs[0].modules[1].footprint_mm]
    elif change=='flat': result.shelves=list(reversed(result.shelves))
    elif change=='bom': result.bom[0].total_level_count+=1
    elif change=='used': result.runs[0].used_length_mm+=1
    elif change=='center': result.runs[0].modules[0].x_mm+=1
    else: result.runs[0].end_mm=(0,0)
    with pytest.raises(PlanningError): validate_runs(req,result)


def test_invalid_geometry_rejected(business):
    req=request('square',cases()['square'],business)
    req.space.boundary.boundary_mm=[(0,0),(10000,10000),(0,10000),(10000,0),(0,0)]
    req.space.confirmation.geometry_sha256=space_geometry_digest(req.space)
    with pytest.raises(PlanningError,match='拓扑无效'): generate_layout(req)


def test_catalog_levels_never_drive_geometry(business):
    req=request('square',cases()['square'],business)
    before=generate_layout(req)
    for t in req.business.templates: t.default_level_count=1
    after=generate_layout(req)
    assert [(s.material_id,s.x_mm,s.y_mm) for s in before.shelves]==[(s.material_id,s.x_mm,s.y_mm) for s in after.shelves]


def test_module_function_has_no_io_or_reference_imports():
    import ast
    tree=ast.parse((ROOT/'services/planning/runs.py').read_text(encoding='utf-8-sig'))
    calls=[n for n in ast.walk(tree) if isinstance(n,ast.Call)]
    assert not any(isinstance(n.func,ast.Name) and n.func.id in {'open','eval','exec','__import__'} for n in calls)
    imports=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
    assert not any('reference' in (name or '') or 'understanding' in (name or '') for name in imports)


def test_empty_runs_and_extreme_resource_requests_fail_closed(business):
    req=request('square',cases()['square'],business)
    result=generate_layout(req)
    result.runs=[]
    result.shelves=[]
    result.bom=[]
    with pytest.raises(PlanningError): validate_runs(req,result)
    subset=[t for t in business.templates if t.depth_mm==400]
    with pytest.raises(PlanningError,match='10000'): combine_modules(12000,subset,10001)
    with pytest.raises(PlanningError,match='200000'): combine_modules(100000000,subset)


@pytest.mark.parametrize('field', ['module_count','run_count','average_modules_per_run','isolated_run_ratio',
    'average_run_length_mm','effective_length_mm','tail_waste_mm','max_same_run_gap_mm',
    'minimum_parallel_aisle_mm','footprint_density','planning_area_mm2','geometry_violation_count',
    'direction_counts','template_counts'])
def test_all_structural_quality_metrics_are_recomputed(field,business):
    req=request('square',cases()['square'],business)
    result=generate_layout(req)
    result.metrics[field]=999999
    with pytest.raises(PlanningError,match='质量指标'): validate_runs(req,result)


@pytest.mark.parametrize('mutation', ['missing','fake_clearance','fake_relation','missing_metric'])
def test_neighbor_and_missing_evidence_rejected(mutation,business):
    req=request('square',cases()['square'],business)
    result=generate_layout(req)
    if mutation=='missing': result.runs[0].neighbors=[]
    elif mutation=='fake_clearance': result.runs[0].neighbors[0]['clearance_mm']=999999
    elif mutation=='fake_relation': result.runs[0].neighbors[0]['relation']='SAME_RUN'
    else: result.metrics.pop('footprint_density')
    with pytest.raises(PlanningError): validate_runs(req,result)


def test_quality_evidence_survives_json_roundtrip(business):
    req=request('square',cases()['square'],business)
    result=generate_layout(req)
    assert validate_runs(req,json.loads(result.model_dump_json()))['quality_metrics_identity']=='PASS'
