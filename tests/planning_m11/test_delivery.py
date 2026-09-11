"""Unified sample transport and honest same-input comparison evidence."""
from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

from services.delivery import algorithm_lab, workbench
from shared.operations_planning import OperationsInput, operations_digest

ROOT=Path(__file__).resolve().parents[2]


def test_all_previous_visible_samples_now_have_explicit_outside_door_and_one_mode():
    old=[]
    for filename in ('scenarios_m09.json','scenarios_m10.json','scenarios_m10_holdout.json'):
        old += json.loads((ROOT/'tools/benchmarks'/filename).read_text(encoding='utf-8-sig'))['scenarios']
    current=algorithm_lab.combined_catalog()['scenarios']
    assert {r['id'] for r in old} <= {r['id'] for r in current}
    for row in current:
        value=OperationsInput.model_validate(row['input'])
        assert row['mode']=='EMPLOYEE_OPERATIONS'
        assert operations_digest(value)==row['input_sha256']
        scope=Polygon(value.base.space.boundary.boundary_mm,value.base.space.boundary.holes_mm)
        assert not scope.covers(Point(value.workpoints.entrance.point_mm))
        assert value.workpoints.entrance.point_mm==value.door_connection.outside_point_mm
        wall=unary_union([LineString([w.start_mm,w.end_mm]) for w in value.physical_perimeter_walls])
        opening=LineString(value.entrance_opening_mm)
        assert wall.intersection(opening).length==0
        assert wall.union(opening).symmetric_difference(LineString(scope.exterior.coords)).length < 1e-6
        assert value.base.rules.aisle_width_mm==1200
        assert len(value.demand.items)==24
        assert len(value.demand.picking_tasks)==7 and len(value.demand.replenishment_tasks)==4
    assert next(r for r in current if r['id']=='op-independent-holdout')['evaluation_role']=='PREVIOUS_FAILED_HOLDOUT_REGRESSION'


def test_invalid_fixed_scene_is_configuration_error_not_cad_readiness(monkeypatch,tmp_path):
    monkeypatch.setattr(workbench,'RUNTIME',tmp_path)
    monkeypatch.setattr(workbench,'DB',tmp_path/'jobs.sqlite3')
    def broken():raise ValueError('TEST_DOOR_MISSING')
    monkeypatch.setattr(algorithm_lab,'combined_catalog',broken)
    with TestClient(workbench.app) as client:
        response=client.get('/api/algorithm/scenarios')
    assert response.status_code==422
    assert response.json()['detail']['code']=='SCENARIO_CONFIGURATION_INVALID'
    assert 'TEST_DOOR_MISSING' in response.json()['detail']['message']


@pytest.fixture(scope='module')
def comparison():
    from services.planning.operations import compare_operations
    from services.planning.baselines.m10_comparison import compare_m10
    row=next(r for r in algorithm_lab.combined_catalog()['scenarios'] if r['id']=='op-short')
    value=OperationsInput.model_validate(row['input'])
    return row,compare_operations(value),compare_m10(value)


@pytest.mark.parametrize('corruption',[None,'outside-route','m10-input'])
def test_one_planning_transport_and_same_input_m10_proof(comparison,corruption,monkeypatch,tmp_path):
    from services.planning.baselines import m10_comparison
    from shared import confirmation
    row,result,before=deepcopy(comparison)
    def forbidden(*args,**kwargs):raise AssertionError('Standard scenes must not consult CAD or signing keys')
    monkeypatch.setattr(confirmation,'_key',forbidden)
    monkeypatch.setattr(workbench,'RUNTIME',tmp_path)
    monkeypatch.setattr(workbench,'DB',tmp_path/'jobs.sqlite3')
    monkeypatch.setattr(algorithm_lab,'combined_catalog',lambda:{'scenarios':[row]})
    if corruption=='outside-route':
        result['after']['layout']['route_evaluation']['face_access'][0]['entrance']['path_mm'][0]=[600,6000]
    if corruption=='m10-input':before['input_sha256']='0'*64
    monkeypatch.setattr(m10_comparison,'compare_m10',lambda value:deepcopy(before))
    calls=[]
    def transport(request):
        calls.append(str(request.url))
        assert request.url.port==8102 and request.url.path=='/plan-operations'
        assert json.loads(request.content)==row['input']
        return httpx.Response(200,json=result)
    real=httpx.Client
    monkeypatch.setattr(algorithm_lab.httpx,'Client',lambda **kwargs:real(transport=httpx.MockTransport(transport),**kwargs))
    with TestClient(workbench.app) as client:response=client.post('/api/algorithm/scenarios/op-short/generate')
    assert len(calls)==1
    if corruption:
        assert response.status_code==422,response.text
    else:
        assert response.status_code==200,response.text
        data=response.json()
        assert data['comparison_baseline']=='M10_CORRECTED_INPUT'
        assert data['before']['input_sha256']==data['input_sha256']
        assert data['before']['layout']['workpoints']==data['after']['layout']['workpoints']==row['input']['workpoints']
        assert data['after']['layout']['route_evaluation']['full_operations_proven'] is True
        assert data['technical_reference']['version']=='M09'
