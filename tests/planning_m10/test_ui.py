"""Isolated real-browser component tests; mock HTTP, not planner acceptance."""
from copy import deepcopy
import json
import mimetypes
import os
from pathlib import Path
from urllib.parse import urlparse

import pytest
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[2]
STATIC=ROOT/'services/delivery/static'
OUT=ROOT/'outputs/planning_m10_ui'


def polygon(x,y,width,height):
    return [[x,y],[x+width,y],[x+width,y+height],[x,y+height]]


def fixture_result():
    """Deliberately small presentation fixture, not claimed as a legal design."""
    space=dict(boundary=dict(boundary_mm=polygon(0,0,12000,9000),holes_mm=[]),
               exclusions=[],entrances=[],barriers=[])
    workpoints={role:dict(point_mm=point,provenance='SYNTHETIC_TEST') for role,point in
                [('entrance',[600,1200]),('receiving',[600,1600]),('pick_start',[600,2000]),('packing',[1000,2000])]}
    shelves=[];runs=[];faces=[]
    for run_index,(x,y) in enumerate([(100,3000),(4400,3000),(4400,3450)]):
        modules=[]
        for bay in range(2):
            identifier=f'M{run_index}-{bay}'
            shelf=dict(id=identifier,material_id='TEST-900-450',length_mm=900,depth_mm=450,
                       default_level_count=6,footprint_mm=polygon(x+bay*900,y,900,450))
            shelves.append(shelf);modules.append(shelf)
            top=run_index!=1
            start=[x+bay*900,y+(450 if top else 0)]
            faces.append(dict(id=f'F{run_index}-{bay}',module_id=identifier,run_id=f'R{run_index}',
                assembly_id='WALL' if run_index==0 else 'DOUBLE',side='SINGLE' if run_index==0 else 'A' if run_index==1 else 'B',
                start_mm=start,end_mm=[start[0]+900,start[1]],outward_normal=[0,1 if top else -1],
                standing_point_mm=[start[0]+450,start[1]+(600 if top else -600)]))
        runs.append(dict(run_id=f'R{run_index}',direction_deg=0,start_mm=[x,y],end_mm=[x+1800,y],
            available_length_mm=1900,used_length_mm=1800,remaining_length_mm=100,
            modules=modules,footprint_mm=polygon(x,y,1800,450)))
    assemblies=[dict(id='WALL',kind='WALL_SINGLE',run_ids=['R0'],footprint_mm=polygon(100,3000,1800,450),
        side_depths_mm=[450],total_depth_mm=450,structure_gap_mm=0,bay_pairs=[]),
        dict(id='DOUBLE',kind='BACK_TO_BACK',run_ids=['R1','R2'],footprint_mm=polygon(4400,3000,1800,900),
        side_depths_mm=[450,450],total_depth_mm=900,structure_gap_mm=0,bay_pairs=[['M1-0','M2-0'],['M1-1','M2-1']])]
    rules=dict(aisle_width_mm=1200,boundary_clearance_mm=100,wall_clearance_mm=100,end_aisle_mm=1200)
    demand=dict(items=[dict(id='SKU-A',target_fraction=[.1,.3]),dict(id='SKU-FAR',target_fraction=[.9,.9])],
        picking_tasks=[dict(id='P-ALL',item_ids=['SKU-A','SKU-FAR']),dict(id='P-FAR',item_ids=['SKU-FAR'])],
        replenishment_tasks=[dict(id='R-ALL',item_ids=['SKU-A','SKU-FAR'])],
        minimum_pick_length_mm=5000,minimum_nominal_board_area_m2=10,
        placement_policy='FIXED_SPATIAL_TARGET_NEAREST_UNIQUE_FACE_V1',
        route_policy='WEIGHTED_NEAREST_UNVISITED_THEN_DESTINATION_V1')
    request=dict(input_kind='ALGORITHM_VALIDATION',base=dict(space=deepcopy(space),rules=rules,templates=[]),
        usable_walls=[],entrance_opening_mm=[[0,600],[0,1800]],workpoints=workpoints,demand=demand,
        assembly_assumptions=dict(structure_gap_mm=0,provenance='TEST_ASSUMPTION'))
    evaluation=dict(face_access=[],picking_routes=[],replenishment_routes=[],
        item_placements=[dict(item_id='SKU-A',face_id='F0-0',standing_point_mm=faces[0]['standing_point_mm']),
                         dict(item_id='SKU-FAR',face_id='F2-1',standing_point_mm=faces[-1]['standing_point_mm'])],
        shared_segments=[dict(path_mm=[[600,2000],[600,2400],[2000,2400]],task_ids=['P-ALL','R-ALL'],length_mm=1800)],
        diagnostics=['固定远端任务保留用于比较。'],
        walkable_polygons=[dict(boundary_mm=polygon(600,600,10800,7800),holes_mm=[])])
    for face in faces:
        record=dict(face_id=face['id'],module_id=face['module_id'],assembly_id=face['assembly_id'],side=face['side'],reachable=True)
        for role in ('entrance','pick_start','receiving'):
            record[role]=dict(reachable=True,distance_mm=3250,path_mm=[workpoints[role]['point_mm'],[2000,2400],face['standing_point_mm']])
        evaluation['face_access'].append(record)
    path=[[600,2000],[600,2400],[550,2400],[550,4050],[2000,4050],[2000,4600],[5750,4600],[1000,4600],[1000,2000]]
    for task in demand['picking_tasks']:
        evaluation['picking_routes'].append(dict(**task,visited_item_ids=task['item_ids'],path_mm=path,distance_mm=12000,status='COMPLETE'))
    evaluation['replenishment_routes']=[dict(**demand['replenishment_tasks'][0],visited_item_ids=['SKU-FAR','SKU-A'],
        path_mm=[[600,1600],[2000,1600],[2000,4600],[5750,4600],[550,4600],[550,4050]],distance_mm=15000,status='COMPLETE')]
    layout=dict(space=space,rules=rules,shelves=shelves,runs=runs,assemblies=assemblies,pick_faces=faces,
        workpoints=workpoints,planned_corridors=[dict(id='END',role='ROW_END_ACCESS',boundary_mm=polygon(600,600,10800,1200),holes_mm=[])],
        route_evaluation=evaluation,bom=[dict(material_id='TEST-900-450',length_mm=900,depth_mm=450,default_level_count=6,quantity=6)])
    measurements=dict(module_count=6,run_count=3,wall_single_run_count=1,central_single_run_count=0,
        double_sided_run_group_count=1,double_sided_bay_count=2,effective_pick_length_mm=5400,
        single_sided_pick_length_mm=1800,double_sided_pick_length_mm=3600,nominal_board_area_m2=14.58,net_shelf_area_m2=None,
        reachable_face_count=6,total_face_count=6,picking_mean_mm=12000,picking_worst_mm=12000,
        replenishment_mean_mm=15000,replenishment_worst_mm=15000,entry_to_face_mean_mm=3250,
        geometry_violation_count=0,tail_waste_mm=300,short_run_count=3,max_same_run_gap_mm=0,
        effective_length_mm=5400,average_run_length_mm=1800)
    after=dict(layout=layout,measurements=measurements,status='AVAILABLE',elapsed_ms=20)
    before=deepcopy(after)
    before['measurements'].update(picking_mean_mm=None,picking_worst_mm=None,reachable_face_count=5)
    before['layout']['route_evaluation']['face_access'][-1].update(reachable=False,
        entrance=dict(reachable=False,distance_mm=None,path_mm=[]),
        pick_start=dict(reachable=False,distance_mm=None,path_mm=[]),
        receiving=dict(reachable=False,distance_mm=None,path_mm=[]))
    before['layout']['route_evaluation']['picking_routes'][0].update(status='UNREACHABLE',distance_mm=None,path_mm=[],visited_item_ids=['SKU-A'])
    result=dict(operation_mode=True,operations_input=request,input_sha256='fixture-complete-input-sha',before=before,after=after,
        selection=dict(policy='SYNTHETIC_FIXTURE',reason='固定需求保留，新增可达证据。',candidate_count=2,kept_baseline=False,
            frontier=[dict(candidate_id='missing-path',eligible=False,dominated=False,metrics=dict(effective_pick_length_mm=None,picking_mean_mm=None),reason='不可达'),
                      dict(candidate_id='selected',eligible=True,dominated=False,metrics=measurements,reason='固定需求达到且全部取货面可达')],
            ablations=[dict(stage='取货面接入',reason='组件测试阶段记录，不是算法验收')]),reference=dict(status='UNAVAILABLE',reason='组件测试不加载 CAD'))
    return result


@pytest.fixture(scope='module')
def browser():
    temporary=ROOT/'.tmp/planning_m10_ui';temporary.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,TEMP=str(temporary),TMP=str(temporary),PLAYWRIGHT_BROWSERS_PATH=str(ROOT/'.cache/ms-playwright'))
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True,env=env,args=['--disable-background-networking','--disable-component-update'])
        yield browser
        browser.close()


@pytest.fixture
def ui(browser):
    result=fixture_result()
    scenario=dict(id='op-ui',title='组件场景 · 员工作业',description='明确合成测试',group='main',mode='EMPLOYEE_OPERATIONS',input=result['operations_input'])
    legacy_result=deepcopy(result)
    legacy_result.pop('operation_mode');legacy_result['input']=result['operations_input']['base']
    legacy_scenario=dict(id='legacy-ui',title='组件场景 · M09',description='旧页面兼容',group='holdout',input=legacy_result['input'])
    data=dict(result=result,legacy=legacy_result,scenarios=[legacy_scenario,scenario],errors=[],calls=[])
    context=browser.new_context(viewport=dict(width=1600,height=1100));page=context.new_page()
    page.on('pageerror',lambda error:data['errors'].append(str(error)))
    def route(request):
        url=urlparse(request.request.url);assert url.hostname=='127.0.0.1'
        if url.path.startswith('/api/'):
            data['calls'].append((request.request.method,url.path))
            if url.path.endswith('/generate'):request.fulfill(json=data['legacy'] if '/legacy-ui/' in url.path else data['result'])
            else:request.fulfill(json=dict(input_kind='ALGORITHM_VALIDATION',scenarios=data['scenarios']))
            return
        path=STATIC/('algorithm-lab.html' if url.path=='/algorithm' else url.path.removeprefix('/static/'))
        assert path.resolve().is_relative_to(STATIC.resolve())
        if path.is_file():request.fulfill(body=path.read_bytes(),content_type=mimetypes.guess_type(str(path))[0] or 'application/octet-stream')
        else:request.fulfill(status=404,body='missing')
    context.route('**/*',route);data['page']=page
    yield data
    context.close()
    assert not data['errors'],data['errors']


def generate(ui,scenario='op-ui'):
    page=ui['page']
    if page.url=='about:blank':page.goto('http://127.0.0.1:8998/algorithm')
    expect(page.locator('#scenario-select')).to_be_enabled()
    page.locator('#scenario-select').select_option(scenario)
    page.get_by_role('button',name='生成并比较',exact=True).click()
    expect(page.locator('#status-label')).to_have_text('比较已完成')
    return page


def test_operations_render_actual_geometry_both_faces_and_no_fabricated_baseline_route(ui):
    page=generate(ui)
    expect(page.locator('#before-title')).to_contain_text('业务未通过')
    expect(page.locator('#before-unavailable')).to_be_hidden()
    for side in ('before','after'):
        svg=page.locator(f'#{side}-canvas')
        expect(svg.get_by_test_id('lab-module')).to_have_count(6)
        expect(svg.get_by_test_id('operations-assembly')).to_have_count(2)
        expect(svg.get_by_test_id('operations-pick-face')).to_have_count(6)
        expect(svg.get_by_test_id('operations-pick-direction')).to_have_count(6)
        expect(svg.get_by_test_id('operations-workpoint')).to_have_count(4)
        expect(svg.get_by_test_id('operations-entrance-opening')).to_have_count(1)
    expect(page.locator('#before-canvas').get_by_test_id('operations-task-path')).to_have_count(0)
    expect(page.locator('#before-route-evidence')).to_contain_text('NA')
    route=page.locator('#after-canvas').get_by_test_id('operations-task-path')
    assert route.get_attribute('points')==' '.join(f'{x},{-y}' for x,y in ui['result']['after']['layout']['route_evaluation']['picking_routes'][0]['path_mm'])
    assert page.locator('#before-canvas').get_attribute('viewBox')==page.locator('#after-canvas').get_attribute('viewBox')
    expect(page.locator('#operations-access-body tr')).to_have_count(12)
    expect(page.locator('[data-metric="net_shelf_area_m2"]')).to_contain_text('UNKNOWN')
    expect(page.locator('#operations-frontier-body [data-candidate-id="missing-path"] td').nth(2)).to_have_text('NA')
    expect(page.locator('#operations-panel')).to_contain_text('人工产品验收 PENDING')
    OUT.mkdir(parents=True,exist_ok=True)
    page.locator('.lab-comparison').screenshot(path=str(OUT/'component-paired.png'))
    page.screenshot(path=str(OUT/'component-page.png'),full_page=True)


def test_same_task_switch_and_module_access_evidence_uses_server_path(ui):
    page=generate(ui)
    page.locator('#operations-task-select').select_option(json.dumps(['replenishment_tasks','R-ALL'],separators=(',',':')))
    expect(page.locator('#operations-task-identity')).to_contain_text('SKU-A、SKU-FAR')
    for side in ('before','after'):
        expect(page.locator(f'#{side}-canvas [data-testid="operations-task-path"]')).to_have_attribute('data-task-id','R-ALL')
    page.locator('#after-canvas [data-module-id="M2-1"]').click()
    detail=page.locator('#after-detail')
    expect(detail).to_contain_text('两套独立单面排背靠背')
    expect(detail).to_contain_text('组合总深 900 mm')
    expect(detail).to_contain_text('B 侧')
    expect(detail).to_contain_text('三个起点均可达')
    detail.get_by_role('button',name='查看拣货起点到此取货面的路径').click()
    face_path=page.locator('#after-canvas').get_by_test_id('operations-face-path')
    expected=ui['result']['after']['layout']['route_evaluation']['face_access'][-1]['pick_start']['path_mm']
    assert face_path.get_attribute('points')==' '.join(f'{x},{-y}' for x,y in expected)
    page.locator('#operations-show-walkable').uncheck()
    assert page.locator('#after-canvas [data-operation-layer="walkable"]').evaluate('(node)=>getComputedStyle(node).display')=='none'
    page.locator('#operations-task-select').select_option(json.dumps(['picking_tasks','P-ALL'],separators=(',',':')))
    expect(face_path).to_have_count(0)


def test_incomplete_route_never_becomes_a_complete_task_even_with_finite_path(ui):
    route=ui['result']['after']['layout']['route_evaluation']['picking_routes'][0]
    route.update(status='COMPLETE',visited_item_ids=['SKU-A'])
    page=generate(ui)
    expect(page.locator('#after-canvas').get_by_test_id('operations-task-path')).to_have_count(0)
    expect(page.locator('#after-route-evidence')).to_contain_text('NA')
    expect(page.locator('#operations-task-body [data-task-id="P-ALL"]')).to_contain_text('SKU-FAR')


def test_legacy_scenario_keeps_its_metrics_and_hides_operation_state(ui):
    page=generate(ui)
    page.locator('#scenario-select').select_option('legacy-ui')
    expect(page.locator('#result-panel')).to_be_hidden()
    expect(page.locator('#operations-panel')).to_be_hidden()
    page=generate(ui,'legacy-ui')
    expect(page.locator('#operations-panel')).to_be_hidden()
    expect(page.locator('#operations-legend')).to_be_hidden()
    expect(page.locator('#operations-route-audit')).to_be_hidden()
    expect(page.locator('#before-route-evidence')).to_be_hidden()
    expect(page.locator('#metrics-title')).to_have_text('连续排与几何指标')
    expect(page.locator('#metrics-body [data-metric="effective_length_mm"]')).to_have_count(1)
    expect(page.locator('#after-canvas').get_by_test_id('operations-task-path')).to_have_count(0)
    page.locator('#after-canvas').get_by_test_id('lab-module').first.click()
    expect(page.locator('#after-detail')).to_contain_text('TEST-900-450')
    page.locator('#after-detail').get_by_role('button',name='查看整排').click()
    expect(page.locator('#after-detail')).to_contain_text('连续排')


def test_narrow_screen_preserves_comparison_scale_and_task_control(ui):
    page=generate(ui);page.set_viewport_size(dict(width=520,height=900))
    expect(page.locator('#operations-task-select')).to_be_visible()
    assert page.locator('.lab-comparison-scroll').evaluate('(node)=>node.scrollWidth>node.clientWidth')
    assert page.locator('#before-canvas').get_attribute('viewBox')==page.locator('#after-canvas').get_attribute('viewBox')
    OUT.mkdir(parents=True,exist_ok=True)
    page.screenshot(path=str(OUT/'component-narrow.png'),full_page=True)
