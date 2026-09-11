"""Algorithm-validation page on the existing delivery service, without CAD jobs."""
import hashlib
import json
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from shared.explicit_planning import ExplicitPlanningInput, explicit_request
from shared.planning_v2 import LayoutV2
from shared.operations_planning import OperationsInput, OperationsLayout, operations_digest

ROOT=Path(__file__).resolve().parents[2]
SCENARIOS=ROOT/'tools/benchmarks/scenarios_m09.json'
OPERATIONS_SCENARIOS=ROOT/'tools/benchmarks/scenarios_m10.json'
INDEPENDENT_HOLDOUT=ROOT/'tools/benchmarks/scenarios_m10_holdout.json'
CURRENT_OPERATIONS_SCENARIOS=ROOT/'tools/benchmarks/scenarios_m11.json'
CURRENT_INDEPENDENT_HOLDOUT=ROOT/'tools/benchmarks/scenarios_m11_holdout.json'
router=APIRouter()


def scenario_catalog():
    value=json.loads(SCENARIOS.read_text(encoding='utf-8-sig'))
    for row in value['scenarios']:
        ExplicitPlanningInput.model_validate(row['input'])
    return value


def combined_catalog():
    # Every displayed sample now has explicit operations input. The immutable
    # M09 fixtures remain available through the geometry-only technical route.
    value={'input_kind':'ALGORITHM_VALIDATION','scenarios':[]}
    operational=json.loads(CURRENT_OPERATIONS_SCENARIOS.read_text(encoding='utf-8-sig'))
    operational['scenarios']+=json.loads(CURRENT_INDEPENDENT_HOLDOUT.read_text(encoding='utf-8-sig'))['scenarios']
    for row in operational['scenarios']:
        payload=OperationsInput.model_validate(row['input'])
        if row.get('mode')!='EMPLOYEE_OPERATIONS' or row.get('input_sha256')!=operations_digest(payload):
            raise ValueError('固定作业场景摘要或模式不一致。')
        if payload.door_connection is None or not payload.physical_perimeter_walls:
            raise ValueError('SCENARIO_CONFIGURATION_INVALID: 当前作业样板缺少实体墙或店外门口连接。')
        if row['group']=='holdout':
            row['evaluation_role']='INDEPENDENT_HOLDOUT'
    value['scenarios']+=operational['scenarios']
    value['operations_schema_version']=operational['schema_version']
    return value


def reference_after_generation(layout):
    try:
        from tools.reference_profile.comparison import compare_layout
        folder=ROOT/'outputs/reference_profile';profiles=[]
        for name,manifest in [('reference_profile.json','FROZEN_SHA256.txt'),
                              ('reference_profile_v1.1.json','FROZEN_V1.1_SHA256.txt')]:
            raw=(folder/name).read_bytes()
            expected=(folder/manifest).read_text(encoding='utf-8-sig').split()[0]
            if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('参考画像摘要不匹配')
            profiles.append(json.loads(raw))
        comparison=compare_layout(LayoutV2.model_validate(layout),*profiles,include_density=False)
        return {'status':'AVAILABLE','comparison':comparison,
                'note':'生成后独立对照；仅比较可核实排结构。参考门店面积未知，不设密度或数量通过门限。'}
    except (OSError,ValueError,KeyError,TypeError) as error:
        return {'status':'SKIPPED','reason':f'该项参考对照暂不可用，已生成的算法方案保留：{error}'}


@router.get('/algorithm')
def algorithm_page():
    return FileResponse(Path(__file__).parent/'static/algorithm-lab.html',headers={'Cache-Control':'no-store'})


@router.get('/api/algorithm/scenarios')
def list_scenarios():
    try:
        return combined_catalog()
    except (OSError,ValueError,KeyError,TypeError) as error:
        raise HTTPException(422,detail={'code':'SCENARIO_CONFIGURATION_INVALID',
            'message':f'本地固定作业场景配置错误：{error}；此入口无需 CAD 或人工范围确认。'}) from error


@router.get('/api/algorithm/geometry/scenarios')
def list_geometry_scenarios():
    return scenario_catalog()


@router.post('/api/algorithm/scenarios/{scenario_id}/generate')
def generate_scenario(scenario_id: str):
    # Existing transport/provenance validation is reused; no understanding
    # service, uploaded CAD, confirmation cache or signing key is involved.
    from services.delivery.workbench import local_url, PLANNING, check_delivery, response_json
    record=next((s for s in list_scenarios()['scenarios'] if s['id']==scenario_id),None)
    if record is None:raise HTTPException(404,detail={'code':'SCENARIO_NOT_FOUND','message':'未找到此固定算法场景。'})
    if record.get('mode')=='EMPLOYEE_OPERATIONS':
        return generate_operations_scenario(record)
    return generate_geometry_record(record)


@router.post('/api/algorithm/geometry/scenarios/{scenario_id}/generate')
def generate_geometry_scenario(scenario_id: str):
    record=next((s for s in scenario_catalog()['scenarios'] if s['id']==scenario_id),None)
    if record is None:raise HTTPException(404,detail={'code':'SCENARIO_NOT_FOUND','message':'未找到技术几何场景。'})
    return generate_geometry_record(record)


def generate_geometry_record(record):
    from services.delivery.workbench import local_url, PLANNING, check_delivery, response_json
    payload=ExplicitPlanningInput.model_validate(record['input']);req=explicit_request(payload)
    try:
        with httpx.Client(timeout=120,trust_env=False) as client:
            response=response_json(client.post(local_url(PLANNING,'/plan-explicit'),json=payload.model_dump(mode='json')))
        if response.get('input_sha256')!=req.source.sha256 or response.get('input')!=payload.model_dump(mode='json'):
            raise ValueError('返回结果与所选明确空间参数不匹配。')
        for phase in ('before','after'):
            if phase=='before' and response[phase].get('status')=='NOT_FOUND' and response[phase].get('layout') is None:
                continue
            response[phase]['layout']=check_delivery(response[phase]['layout'],req)
        response['reference']=reference_after_generation(response['after']['layout'])
        return response
    except (httpx.HTTPError,ValueError,KeyError,TypeError) as error:
        raise HTTPException(422,detail={'code':'ALGORITHM_VALIDATION_FAILED','message':str(error)}) from error


def generate_operations_scenario(record):
    from services.delivery.workbench import local_url, PLANNING, response_json
    from services.planning.operations_layouts import validate_operations_geometry
    from services.planning.operations_routes import evaluate_routes, validate_route_evidence
    from services.planning.operations import POLICY, measure_operations
    from services.planning.engine import PlanningError
    payload=OperationsInput.model_validate(record['input'])
    canonical=lambda obj:json.dumps(obj,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
    try:
        # The fixed obstacle suite evaluates all route-feasible alternatives.
        # Keep a bounded local computation window; the page shows elapsed time.
        with httpx.Client(timeout=300,trust_env=False) as client:
            response=response_json(client.post(local_url(PLANNING,'/plan-operations'),json=payload.model_dump(mode='json')))
        if (response.get('operation_mode') is not True or response.get('input_sha256')!=operations_digest(payload)
                or response.get('operations_input')!=payload.model_dump(mode='json')):
            raise ValueError('作业结果与全部场地、工作点、结构假设或固定任务输入不匹配。')
        selection=response['selection']
        if selection.get('fixed_demand')!=payload.demand.model_dump(mode='json') or selection.get('policy')!=POLICY:
            raise ValueError('选优证据的固定任务或策略与输入不一致。')
        identifiers=[row['candidate_id'] for row in selection['frontier']]
        if len(identifiers)!=len(set(identifiers)):
            raise ValueError('候选标识重复，选优证据无法唯一对应实际布局。')
        for phase in ('before','after'):
            layout=OperationsLayout.model_validate(response[phase]['layout'])
            validate_operations_geometry(payload,layout)
            if phase=='before' and canonical(evaluate_routes(payload,layout))!=canonical(layout.route_evaluation):
                raise ValueError('M09 对照路线或可见指标与同输入重算不一致。')
            if phase=='after' and validate_route_evidence(payload,layout,layout.route_evaluation).get('status')!='PASS':
                raise ValueError('最终作业路线未通过交付端独立复验。')
            measured=measure_operations(payload,layout,layout.route_evaluation)
            if canonical(measured)!=canonical(response[phase]['measurements']):
                raise ValueError('页面空间或路线指标与实际输出不一致。')
            if phase=='after' and (not measured['all_faces_reachable']
                    or measured['effective_pick_length_mm']+1e-6<payload.demand.minimum_pick_length_mm
                    or measured['nominal_board_area_m2']+1e-6<payload.demand.minimum_nominal_board_area_m2):
                raise ValueError('最终结果未满足逐面可达或冻结的空间需求。')
        chosen=next((row for row in selection['frontier'] if row['candidate_id']==selection['chosen_candidate_id']),None)
        comparable={key:value for key,value in response['after']['measurements'].items()
                    if key not in ('deterministic_sha256','hash_definition')}
        if (not chosen or not chosen['eligible'] or chosen['dominated']
                or canonical(chosen['metrics'])!=canonical(comparable)
                or selection['kept_baseline']!=(chosen['candidate_id']=='M09')):
            raise ValueError('选中候选的状态或指标与最终布局不一致。')
        if payload.door_connection is not None:
            from services.planning.baselines.m10_comparison import compare_m10
            from services.planning.baselines.m10_layouts import validate_operations_geometry as validate_m10_geometry
            old=compare_m10(payload)
            if old['input_sha256']!=operations_digest(payload):
                raise ValueError('M10 对照输入不一致。')
            if old['layout'] is None and (old.get('status')!='NOT_FOUND'
                    or old.get('measurements') is not None or not old.get('error')):
                raise ValueError('M10 未找到方案时必须保留失败原因，且布局、指标均为空。')
            if old['layout'] is not None:
                if old.get('status')!='AVAILABLE':
                    raise ValueError('M10 对照布局与状态不一致。')
                legacy=OperationsLayout.model_validate(old['layout'])
                validate_m10_geometry(payload,legacy)
                if canonical(evaluate_routes(payload,legacy))!=canonical(legacy.route_evaluation):
                    raise ValueError('M10 对照必须使用同一修正门口、任务和路由度量。')
                if canonical(measure_operations(payload,legacy,legacy.route_evaluation))!=canonical(old['measurements']):
                    raise ValueError('M10 对照指标不一致。')
            response['technical_reference']={'version':'M09','result':response['before']}
            response['before']=old
            response['comparison_baseline']='M10_CORRECTED_INPUT'
            response['input_migration_note']=record.get('input_change_note','新增真实门外入口条件；收益只在同输入算法间比较。')
        return response
    except (httpx.HTTPError,ValueError,KeyError,TypeError,PlanningError) as error:
        raise HTTPException(422,detail={'code':'OPERATIONS_DELIVERY_VALIDATION_FAILED',
            'message':getattr(error,'message',str(error))}) from error
