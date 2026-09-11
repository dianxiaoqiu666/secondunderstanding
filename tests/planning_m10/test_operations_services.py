"""Self-contained M10 ASGI, delivery integrity, and existing material contracts.

No reference CAD, runtime state, development cache, or holdout is required.
One module-scoped real planning call uses one direction/depth (six candidates,
including M09); delivery mutations reuse that response, never regenerate it.
"""
from collections import Counter
from copy import deepcopy
from io import BytesIO
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from openpyxl import load_workbook
import pytest

from services.delivery import algorithm_lab
from services.delivery.exports_v2 import make_workbook
from services.planning.app import app as planning_app
from services.planning.products import assign_categories
from shared.contracts import Business
from shared.explicit_planning import explicit_request
from shared.operations_planning import OperationsInput, OperationsLayout, operations_digest, operations_request


def independent_input():
    """Small contract fixture; these floors are not frozen benchmark changes."""
    return OperationsInput.model_validate({
        'base': {
            'input_kind':'ALGORITHM_VALIDATION',
            'name':'M10-independent-service-contract',
            'space':{'boundary':{'boundary_mm':[[0,0],[12000,0],[12000,9000],[0,9000],[0,0]]}},
            'templates':[
                {'material_id':'TEST-1200-400','length_mm':1200,'depth_mm':400,'default_level_count':6},
                {'material_id':'TEST-1800-400','length_mm':1800,'depth_mm':400,'default_level_count':7}],
            'rules':{'orientation_candidates':[0]}},
        'usable_walls':[
            {'id':'bottom','start_mm':[0,0],'end_mm':[12000,0],'inward_normal':[0,1],'provenance':'SYNTHETIC_TEST'},
            {'id':'top','start_mm':[12000,9000],'end_mm':[0,9000],'inward_normal':[0,-1],'provenance':'SYNTHETIC_TEST'}],
        'entrance_opening_mm':[[0,3300],[0,5700]],
        'workpoints':{role:{'point_mm':point} for role,point in {
            'entrance':[600,4500], 'receiving':[600,2100],
            'pick_start':[600,4500], 'packing':[600,6900]}.items()},
        'demand':{
            'items':[{'id':name,'target_fraction':target} for name,target in [
                ('near-low',[.2,.2]),('far-low',[.8,.2]),('near-high',[.2,.8]),('far-high',[.8,.8])]],
            'picking_tasks':[
                {'id':'P-low','item_ids':['near-low','far-low']},
                {'id':'P-high','item_ids':['near-high','far-high']},
                {'id':'P-far-diagonal','item_ids':['near-low','far-high']}],
            'replenishment_tasks':[
                {'id':'R-near','item_ids':['near-low','near-high']},
                {'id':'R-far','item_ids':['far-low','far-high']}],
            'minimum_pick_length_mm':14400,'minimum_nominal_board_area_m2':34.56}})


@pytest.fixture(scope='module')
def operation_response():
    value=independent_input()
    with TestClient(planning_app) as client:
        response=client.post('/plan-operations',json=value.model_dump(mode='json'))
    assert response.status_code==200,response.text
    return value,response.json()


@pytest.fixture
def delivery_transport(monkeypatch):
    app=FastAPI();app.include_router(algorithm_lab.router)
    client=TestClient(app)
    state={'response':None,'calls':[],'catalog':None}
    original_client=httpx.Client
    def transport(request):
        state['calls'].append({'path':request.url.path,'body':json.loads(request.content)})
        return httpx.Response(200,json=deepcopy(state['response']),request=request)
    def make_client(*args,**kwargs):
        kwargs['transport']=httpx.MockTransport(transport)
        return original_client(*args,**kwargs)
    monkeypatch.setattr(algorithm_lab.httpx,'Client',make_client)
    monkeypatch.setattr(algorithm_lab,'combined_catalog',lambda:deepcopy(state['catalog']))
    monkeypatch.setattr(algorithm_lab,'reference_after_generation',lambda layout:{
        'status':'SKIPPED','reason':'Independent API fixture has no CAD reference dependency'})
    yield client,state
    client.close()


def set_operation_transport(transport,value,response):
    client,state=transport
    state['response']=deepcopy(response)
    state['catalog']={'scenarios':[{'id':'independent-op','mode':'EMPLOYEE_OPERATIONS',
        'input':value.model_dump(mode='json'),'input_sha256':operations_digest(value)}]}
    return client,state


def test_real_asgi_operations_bind_complete_input_and_all_tasks(operation_response):
    value,result=operation_response
    assert result['operation_mode'] is True
    assert result['operations_input']==value.model_dump(mode='json')
    assert result['input_sha256']==operations_digest(value)
    assert result['input_sha256']!=operations_request(value).source.sha256
    assert result['selection']['fixed_demand']==value.demand.model_dump(mode='json')
    layout=OperationsLayout.model_validate(result['after']['layout'])
    assert layout.workpoints==value.workpoints
    assert layout.validation['operations_input_sha256']==operations_digest(value)
    assert layout.validation['status']=='PASS' and layout.design_status=='VALIDATED'
    assert layout.validation['human_acceptance']=='PENDING'
    evaluation=layout.route_evaluation
    assert evaluation['status']=='PASS'
    assert {r['id'] for r in evaluation['picking_routes']}=={t.id for t in value.demand.picking_tasks}
    assert {r['id'] for r in evaluation['replenishment_routes']}=={t.id for t in value.demand.replenishment_tasks}
    for tasks,key in [(value.demand.picking_tasks,'picking_routes'),
                      (value.demand.replenishment_tasks,'replenishment_routes')]:
        routes={row['id']:row for row in evaluation[key]}
        for task in tasks:
            assert set(routes[task.id]['visited_item_ids'])==set(task.item_ids)
            assert routes[task.id]['distance_mm']>0
    assert len(evaluation['face_access'])==len(layout.shelves)==sum(row.quantity for row in layout.bom)


@pytest.mark.parametrize('change',['workpoint','task_order','item_target','structure_gap','space_floor'])
def test_operations_digest_includes_workpoints_demands_and_assumptions(change):
    original=independent_input();changed=original.model_dump(mode='json')
    if change=='workpoint':changed['workpoints']['receiving']['point_mm'][1]+=100
    elif change=='task_order':changed['demand']['picking_tasks'][0]['item_ids'].reverse()
    elif change=='item_target':changed['demand']['items'][0]['target_fraction'][0]+=.01
    elif change=='structure_gap':changed['assembly_assumptions']['structure_gap_mm']=100
    else:changed['demand']['minimum_nominal_board_area_m2']+=1
    altered=OperationsInput.model_validate(changed)
    assert operations_request(altered).source.sha256==operations_request(original).source.sha256
    assert operations_digest(altered)!=operations_digest(original)


@pytest.mark.parametrize('change',['unknown_item','dropped_replenishment_far','duplicate_task','changed_route_policy','narrow_opening'])
def test_real_asgi_rejects_incomplete_or_invalid_operational_input(change):
    payload=independent_input().model_dump(mode='json')
    if change=='unknown_item':payload['demand']['picking_tasks'][0]['item_ids'].append('not-in-fixed-demand')
    elif change=='dropped_replenishment_far':payload['demand']['replenishment_tasks'].pop()
    elif change=='duplicate_task':payload['demand']['picking_tasks'][1]['id']=payload['demand']['picking_tasks'][0]['id']
    elif change=='changed_route_policy':payload['demand']['route_policy']='PER_ITEM_ROUND_TRIP'
    else:payload['entrance_opening_mm']=[[0,4000],[0,5100]]
    with TestClient(planning_app) as client:
        response=client.post('/plan-operations',json=payload)
    assert response.status_code==422,response.text


def test_delivery_dispatches_complete_operations_payload(operation_response,delivery_transport):
    value,result=operation_response
    client,state=set_operation_transport(delivery_transport,value,result)
    response=client.post('/api/algorithm/scenarios/independent-op/generate')
    assert response.status_code==200,response.text
    assert state['calls']==[{'path':'/plan-operations','body':value.model_dump(mode='json')}]
    assert response.json()==result


def tamper(result,change):
    if change=='digest':result['input_sha256']='0'*64
    elif change=='input_workpoint':result['operations_input']['workpoints']['packing']['point_mm'][1]+=100
    elif change=='input_task':result['operations_input']['demand']['picking_tasks'][0]['item_ids'].reverse()
    elif change=='layout_workpoint':result['after']['layout']['workpoints']['packing']['point_mm'][1]+=100
    elif change=='bom_quantity':result['after']['layout']['bom'][0]['quantity']+=1
    elif change=='bom_level_total':result['after']['layout']['bom'][0]['total_level_count']+=1
    elif change=='missing_face':result['after']['layout']['pick_faces'].pop()
    elif change=='reported_route_metric':result['after']['measurements']['picking_mean_mm']=0
    elif change=='coordinated_route_metric':
        result['after']['layout']['route_evaluation']['summary']['picking_mean_mm']=0
        result['after']['measurements']['picking_mean_mm']=0
    elif change=='route_distance':result['after']['layout']['route_evaluation']['picking_routes'][0]['distance_mm']=0
    elif change=='dropped_pick_task':result['after']['layout']['route_evaluation']['picking_routes'].pop()
    elif change=='dropped_refill_task':result['after']['layout']['route_evaluation']['replenishment_routes'].pop()
    elif change=='selection_fixed_demand':result['selection']['fixed_demand']['items'][0]['target_fraction'][0]=.99
    elif change=='baseline_coordinated_metric':
        old=result['before']['layout']['route_evaluation']['summary'].get('picking_mean_mm')
        result['before']['layout']['route_evaluation']['summary']['picking_mean_mm']=(old or 0)+100000
        result['before']['measurements']['picking_mean_mm']=(old or 0)+100000
    else:raise AssertionError(change)


@pytest.mark.parametrize('change',[
    'digest','input_workpoint','input_task','layout_workpoint','bom_quantity','bom_level_total',
    'missing_face','reported_route_metric','coordinated_route_metric','route_distance',
    'dropped_pick_task','dropped_refill_task','selection_fixed_demand','baseline_coordinated_metric'])
def test_delivery_rejects_tampered_geometry_tasks_materials_or_route_comparison(
        operation_response,delivery_transport,change):
    value,result=operation_response
    altered=deepcopy(result);tamper(altered,change)
    client,state=set_operation_transport(delivery_transport,value,altered)
    response=client.post('/api/algorithm/scenarios/independent-op/generate')
    assert response.status_code==422,(change,response.text[:1000])
    assert response.json()['detail']['code']=='OPERATIONS_DELIVERY_VALIDATION_FAILED'
    assert state['calls'][0]['body']==value.model_dump(mode='json')


def test_existing_m09_asgi_and_delivery_branch_remain_usable(delivery_transport):
    value=independent_input().base
    with TestClient(planning_app) as planning:
        raw=planning.post('/plan-explicit',json=value.model_dump(mode='json'))
    assert raw.status_code==200,raw.text
    result=raw.json()
    assert result['input_sha256']==explicit_request(value).source.sha256
    assert not result.get('operation_mode',False)
    client,state=delivery_transport
    state['response']=result
    state['catalog']={'scenarios':[{'id':'independent-m09','input':value.model_dump(mode='json')}]}
    response=client.post('/api/algorithm/scenarios/independent-m09/generate')
    assert response.status_code==200,response.text
    assert state['calls']==[{'path':'/plan-explicit','body':value.model_dump(mode='json')}]
    assert response.json()['after']['layout']['bom']==result['after']['layout']['bom']


def test_existing_category_algorithm_preserves_new_layout_instances_and_bom(operation_response):
    value,response=operation_response
    layout=OperationsLayout.model_validate(response['after']['layout'])
    before=layout.model_dump(mode='json')
    business=Business(products=[{'category':'食品>零食'},{'category':'食品>饮品'},
                                {'category':'日用>清洁'}],product_count=3,
                      templates=value.base.templates,sources=[])
    assigned=assign_categories(business,layout.runs)
    assert layout.model_dump(mode='json')==before
    shelves={s.id for s in layout.shelves}
    assignment_ids=[sid for category in assigned.category_assignments for sid in category['shelf_ids']]
    assert len(assignment_ids)==len(set(assignment_ids))==len(shelves)
    assert set(assignment_ids)==shelves
    assert {row['category'] for row in assigned.category_assignments}=={'食品','日用'}
    assert assigned.sku_assignments==[]
    assert assigned.real_sku_status=='UNAVAILABLE-DUE-TO-MISSING-PHYSICAL-DATA'
    assert Counter(s.material_id for s in layout.shelves)=={row.material_id:row.quantity for row in layout.bom}


def test_existing_workbook_accepts_operations_json_and_counts_modules_not_assemblies(operation_response):
    _,response=operation_response
    raw=deepcopy(response['after']['layout'])
    before=deepcopy(raw)
    workbook=load_workbook(BytesIO(make_workbook(raw)),data_only=True)
    try:
        rows=list(workbook['设计级货架清单'].values)
        assert rows[-1][4]==len(raw['shelves'])
        assert rows[-1][5]==sum(row['total_level_count'] for row in raw['bom'])
        assert {row[0]:tuple(row[1:6]) for row in rows[1:-1]}=={
            row['material_id']:(row['length_mm'],row['depth_mm'],row['default_level_count'],row['quantity'],row['total_level_count'])
            for row in raw['bom']}
        instances=list(workbook['货架实例'].values)[1:]
        assert {row[0] for row in instances}=={s['id'] for s in raw['shelves']}
        assert len(instances)==len(raw['shelves'])
        assert any(a['kind']=='BACK_TO_BACK' for a in raw['assemblies'])
        assert len(raw['shelves'])>len(raw['assemblies'])
        assert all(cell.data_type!='f' for sheet in workbook for row in sheet for cell in row)
    finally:workbook.close()
    assert raw==before
