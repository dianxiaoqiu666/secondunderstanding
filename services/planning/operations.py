"""Attributable finite candidates and joint space/operations evaluation."""
from copy import deepcopy
import hashlib
import json
from time import perf_counter

from shared.operations_planning import OperationsInput, operations_digest
from services.planning.engine import PlanningError
from services.planning.explicit import finalize
from services.planning.search import optimize_layout
from services.planning.products import assign_categories
from services.planning.operations_layouts import (adapt_m09, checked_input,
    generate_candidate, residual_single_options, validate_operations_geometry)
from services.planning.operations_routes import corridor_plans, evaluate_routes, validate_route_evidence

EPS=1e-6
POLICY='M11-FRONTAGE-CANDIDATES-FIXED-DEMAND-PARETO-MINIMAX-REGRET-V1'
# Fixed before M10 tuning (scenarios_m10.json). Each objective is explicit and
# unit-normalized by the ideal observed legal value, not by a scoring weight.
OBJECTIVES=(('effective_pick_length_mm',1),('nominal_board_area_m2',1),
    ('picking_mean_mm',-1),('picking_worst_mm',-1),
    ('replenishment_mean_mm',-1),('replenishment_worst_mm',-1))


def measure_operations(value, layout, evaluation):
    measured=deepcopy(layout.metrics);measured.update(deepcopy(evaluation['summary']))
    reachable={f['face_id'] for f in evaluation['face_access'] if f.get('reachable')}
    by_id={s.id:s for s in layout.shelves}
    measured['installed_face_length_mm']=measured['effective_pick_length_mm']
    measured['effective_pick_length_mm']=sum(by_id[f.module_id].length_mm for f in layout.pick_faces if f.id in reachable)
    measured['all_faces_reachable']=evaluation['status']=='PASS'
    measured['task_provenance']='SYNTHETIC_EVALUATION'
    measured['minimum_pick_length_mm']=value.demand.minimum_pick_length_mm
    measured['minimum_nominal_board_area_m2']=value.demand.minimum_nominal_board_area_m2
    measured['full_operations_proven']=evaluation.get('full_operations_proven',False)
    measured['outside_entry_proof']=evaluation.get('outside_entry_proof','MISSING_LEGACY')
    return measured


def _eligibility(value, measured, evaluation):
    reasons=[]
    if evaluation['status']!='PASS':reasons.append('逐面可达、入口或完整任务路线未全部通过')
    if value.door_connection is not None and evaluation.get('full_operations_proven') is not True:
        reasons.append('真实店外入口至店内工作点的完整门口连接未被证明')
    if measured['effective_pick_length_mm']+EPS<value.demand.minimum_pick_length_mm:
        reasons.append('可达取货面长度低于冻结需求下限')
    if measured['nominal_board_area_m2']+EPS<value.demand.minimum_nominal_board_area_m2:
        reasons.append('名义层板面积低于冻结需求下限')
    if any(measured.get(key) is None for key,_ in OBJECTIVES):reasons.append('路线指标缺失，不能按部分任务平均')
    return reasons


def _dominates(a,b):
    # Tail/fragment diagnostics accompany every point, while the six declared
    # objectives define the density/route frontier without hidden weights.
    better=False
    for key,sign in OBJECTIVES:
        delta=(a[key]-b[key])*sign
        if delta < -EPS:return False
        better|=delta>EPS
    return better


def _frontier(records):
    eligible=[row for row in records if row['eligible']]
    for row in eligible:
        witnesses=[other['candidate_id'] for other in eligible if other is not row and _dominates(other['metrics'],row['metrics'])]
        row['dominated']=bool(witnesses);row['dominated_by']=witnesses
    return [row for row in eligible if not row['dominated']]


def _joint_key(row, ideals):
    regrets=[]
    for key,sign in OBJECTIVES:
        ideal=ideals[key];actual=row['metrics'][key]
        regrets.append(max(0.0,(ideal-actual if sign>0 else actual-ideal)/max(abs(ideal),EPS)))
    row['normalized_regrets']={key:value for (key,_),value in zip(OBJECTIVES,regrets)}
    row['worst_normalized_regret']=max(regrets)
    m=row['metrics']
    return (max(regrets),sum(regrets),m['short_run_count'],m['tail_waste_mm'],row['candidate_id'])


def _seal(value,layout,evaluation):
    _,req,_,_=checked_input(value)
    layout=layout.model_copy(deep=True)
    if not layout.planned_corridors:
        if (layout.operational_policy!='M09_PRESERVED_WITH_FIXED_CENTRE_FACING_TEST_ASSUMPTION'
                or any(a.kind!='M09_SINGLE_ASSUMPTION' for a in layout.assemblies)):
            raise PlanningError('OPERATIONS_CORRIDOR_FIRST_REQUIRED','仅结构实验没有预先保留作业通道，不能发布。')
        original,_=optimize_layout(req)
        if (layout.runs,layout.shelves,layout.bom)!=(original.runs,original.shelves,original.bom):
            raise PlanningError('OPERATIONS_BASELINE_MISMATCH','保留基线例外必须是同输入的当前 M09 原方案。')
    else:
        generation=layout.metrics.get('generation_spec',{})
        main_direction=generation.get('main_direction_deg',layout.runs[0].direction_deg)
        side_depth=generation.get('side_depth_mm',layout.runs[0].depth_mm)
        plans=corridor_plans(value,main_direction,side_depth)
        if not any(layout.planned_corridors==plan['corridors'] for plan in plans):
            raise PlanningError('OPERATIONS_CORRIDOR_FIRST_REQUIRED','预留通道与该输入的工作点通行候选不一致。')
    layout.products=assign_categories(req.business,layout.runs)
    layout.route_evaluation=evaluation
    layout.validation=validate_operations_geometry(value,layout)
    route=validate_route_evidence(value,layout,evaluation)
    if route.get('status')!='PASS':
        raise PlanningError('OPERATIONS_ROUTE_FAILED','路线证据未通过独立复算。')
    reasons=_eligibility(value,measure_operations(value,layout,evaluation),evaluation)
    if reasons:
        raise PlanningError('OPERATIONS_DEMAND_FAILED','；'.join(reasons))
    layout.validation.update(route_evidence=route,operations_input_sha256=operations_digest(value),human_acceptance='PENDING')
    layout.design_status='VALIDATED'
    layout.metrics.pop('deterministic_sha256',None);layout.metrics.pop('hash_definition',None)
    canonical=json.dumps(layout.model_dump(mode='json'),sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    layout.metrics['deterministic_sha256']=hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    layout.metrics['hash_definition']='Canonical final JSON excluding deterministic_sha256 and hash_definition'
    return layout


def compare_operations(value: OperationsInput, *, include_stage_layouts=False):
    value,req,_,templates=checked_input(value)
    begin=perf_counter()
    baseline,_=optimize_layout(req)
    baseline=finalize(req,baseline)
    before=adapt_m09(value,baseline)
    before.validation=validate_operations_geometry(value,before)
    before_eval=evaluate_routes(value,before)
    before.route_evaluation=before_eval
    before.design_status='2D_REVIEW_REQUIRED'
    before_metrics=measure_operations(value,before,before_eval)
    before_ms=(perf_counter()-begin)*1000
    records=[]; layouts={};evaluations={}; equivalent_candidates=[];route_cache={}
    def examine(identifier,stage,layout):
        if identifier in layouts:
            raise ValueError('Candidate identifiers must preserve distinct template depths')
        # Navigation depends on these complete solid/face/identity records and
        # the fixed input, not on which reserved corridor produced them. Reuse
        # only exact identity, never a rounded geometry or partial-task result.
        route_key=json.dumps(dict(run_membership=[[run.run_id,[item.id for item in run.modules]] for run in layout.runs],
            records=[[item.model_dump(mode='json') for item in group]
                     for group in (layout.shelves,layout.assemblies,layout.pick_faces)]),
            sort_keys=True,separators=(',',':'),ensure_ascii=False)
        cached=route_cache.get(route_key)
        evaluation=(cached[1] if cached else before_eval if identifier=='M09' else evaluate_routes(value,layout))
        if cached is None:route_cache[route_key]=(identifier,evaluation)
        metrics=measure_operations(value,layout,evaluation)
        reasons=_eligibility(value,metrics,evaluation)
        hard_pass=not reasons
        comparison_only=stage=='STRUCTURE_ONLY'
        if comparison_only:
            reasons.append('仅用于结构阶段对照；未预先保留工作点通道，不参与最终选优')
        record=dict(candidate_id=identifier,stage=stage,eligible=not reasons,dominated=False,
            hard_constraints_pass=hard_pass,comparison_only=comparison_only,
            geometry_status='PASS',route_status=evaluation['status'],metrics=metrics,
            generation_mechanism=layout.metrics.get('frontage_interval_policy','M09_FIXED_GEOMETRY'),
            reason='；'.join(reasons) if reasons else '几何、逐面可达、完整任务及冻结空间下限通过')
        if cached is not None:record['route_evidence_reused_from']=cached[0]
        if not any(a.kind=='BACK_TO_BACK' for a in layout.assemblies):
            record['structure_note']=('同输入 M09 固定几何与向场地中央取货的测试假设，未改变原排坐标。'
                if identifier=='M09' else
                '本候选实际为单面：独立的单面比较分支，或当前横向完整双面占地加两侧通道不能容纳一组；可用双面分支及失败原因见候选明细。')
        layouts[identifier]=layout;evaluations[identifier]=evaluation;records.append(record)
    examine('M09','M09_TECHNICAL_BASELINE',before)
    begin=perf_counter()
    for direction in sorted(set(req.rules.orientation_candidates)):
        for depth in sorted({t.depth_mm for t in templates}):
            depth_id=str(int(depth)) if depth.is_integer() else repr(depth)
            prefix=f'O{direction}-D{depth_id}'
            # Mechanism 1: identical DP/templates, now wall fronts and paired backs.
            variants=[('STRUCTURE_ONLY','STRUCTURE_ONLY',[], 'BACK_TO_BACK',False,False,None)]
            residual_options=residual_single_options(value,direction,depth) if value.door_connection is not None else ()
            # Mechanism 2: workpoint/entry corridors are selected BEFORE racks.
            for plan in corridor_plans(value,direction,depth):
                for pattern in ('BACK_TO_BACK','CENTRAL_SINGLE'):
                    suffix=plan['id']+'-'+pattern
                    if plan['id'].startswith('WALL_FRONT_LOOP'):
                        variants.append((suffix+'-MIXED_WALLS-FRONTAGE','CORRIDOR_FIRST',plan['corridors'],pattern,True,True,None))
                        if pattern=='BACK_TO_BACK':
                            for side in residual_options:
                                variants.append((suffix+f'-MIXED_WALLS-RESIDUAL_SINGLE_{side}-FRONTAGE','CORRIDOR_FIRST',
                                    plan['corridors'],pattern,True,True,side))
                    else:
                        variants.append((suffix,'CORRIDOR_FIRST',plan['corridors'],pattern,False,False,None))
                        variants.append((suffix+'-FRONTAGE','CORRIDOR_FIRST',plan['corridors'],pattern,True,False,None))
            for suffix,stage,corridors,pattern,frontage,mixed,residual in variants:
                identifier=f'{prefix}-{suffix}'
                try:
                    layout=generate_candidate(value,direction,depth,corridors,pattern,identifier,
                        frontage_aware=frontage,all_wall_directions=mixed,residual_single_at=residual)
                    original=layouts.get(identifier.removesuffix('-FRONTAGE')) if frontage else None
                    if original is not None and (layout.shelves,layout.pick_faces,layout.planned_corridors)==(
                            original.shelves,original.pick_faces,original.planned_corridors):
                        equivalent_candidates.append(dict(candidate_id=identifier,equivalent_to=identifier.removesuffix('-FRONTAGE'),
                            reason='取货面净宽区段复核产生相同模块、前向和通道；避免重复路线评价'))
                        continue
                    examine(identifier,stage,layout)
                except (PlanningError,ValueError) as error:
                    records.append(dict(candidate_id=identifier,stage=stage,eligible=False,dominated=False,
                        geometry_status='FAIL',route_status='NOT_EVALUATED',metrics=None,
                        reason=getattr(error,'message',str(error))))
    # Structure-only experiments remain visible as an ablation, never as a new
    # published plan. The existing M09 candidate alone retains the old-layout
    # exception, with its geometry and all workflows independently rechecked.
    frontier=_frontier(records)
    if not frontier:
        geometry_count=sum(row.get('geometry_status')=='PASS' for row in records)
        route_count=sum(row.get('route_status')=='PASS' and not row.get('comparison_only') for row in records)
        raise PlanningError('NO_OPERATIONAL_CANDIDATE',f'有限搜索中 {geometry_count} 个几何候选、{route_count} 个发布分支路线通过，'
            '但没有候选同时满足完整作业证明及冻结需求下限；未降低通道或需求，不代表全局无解。')
    ideals={key:(max if sign>0 else min)(r['metrics'][key] for r in frontier) for key,sign in OBJECTIVES}
    chosen=min(frontier,key=lambda r:_joint_key(r,ideals))
    selected=_seal(value,layouts[chosen['candidate_id']],evaluations[chosen['candidate_id']])
    chosen_metrics=measure_operations(value,selected,selected.route_evaluation)
    def stage_summary(stage):
        options=[r for r in records if r['stage']==stage and r.get('metrics')]
        if not options:return dict(stage=stage,status='NO_GEOMETRIC_CANDIDATE')
        # Density-first fixes the pre-joint mechanism's choice, making the
        # final route-aware selection attributable on the same candidate set.
        pick=max(options,key=lambda r:(r['hard_constraints_pass'],r['metrics']['installed_face_length_mm'],
            r['metrics']['nominal_board_area_m2'],-r['metrics']['tail_waste_mm'],r['candidate_id']))
        status='COMPARISON_ONLY' if pick['comparison_only'] else 'PASS' if pick['eligible'] else 'INELIGIBLE'
        return dict(stage=stage,status=status,
            candidate_id=pick['candidate_id'],metrics=pick['metrics'],reason=pick['reason'])
    reason=(f"先满足全部硬约束和冻结需求，剔除被支配方案；在 {len(frontier)} 个非支配候选中，"
        '选择六项空间/路线指标相对理想值的最大损失最小者。并列时依次比较损失合计、碎排、余量和稳定候选 ID。'
        f" 所选最大相对损失 {chosen['worst_normalized_regret']:.4f}；名义面积不是净层板面积或商品容量。")
    double_rows=[row for row in records if row.get('metrics') and row['metrics']['double_sided_run_group_count']>0
                 and not row.get('comparison_only')]
    legal_doubles=[row for row in double_rows if row['eligible']]
    structural_reason=dict(selected_double_groups=chosen_metrics['double_sided_run_group_count'],
        geometric_double_candidate_count=len(double_rows),eligible_double_candidate_count=len(legal_doubles),
        selected_metrics={key:chosen_metrics.get(key) for key in (
            *[key for key,_ in OBJECTIVES],'tail_waste_mm','short_run_count',
            'parallel_pick_aisle_min_mm','parallel_pick_aisle_median_mm','parallel_pick_aisle_max_mm')},
        rejected_double_candidates=[dict(candidate_id=row['candidate_id'],reason=row['reason'])
                                    for row in double_rows if not row['eligible']])
    if 'residual_space_use' in selected.metrics:
        structural_reason['residual_space_use']=selected.metrics['residual_space_use']
        structural_reason['residual_single_back_service']=selected.metrics['residual_single_back_service']
    if not structural_reason['selected_double_groups']:
        if legal_doubles:
            best_double=min(legal_doubles,key=lambda row:_joint_key(row,ideals))
            structural_reason.update(best_eligible_double_candidate_id=best_double['candidate_id'],
                best_eligible_double_metrics=best_double['metrics'],
                best_eligible_double_worst_regret=best_double['worst_normalized_regret'],
                reason=f"存在 {len(legal_doubles)} 个合法双面候选；当前单面方案按同一六指标规则的最大相对损失 "
                    f"{chosen['worst_normalized_regret']:.4f}，最优双面候选为 {best_double['worst_normalized_regret']:.4f}。"
                    '这是空间与固定任务路线的选择结果，不能据此宣称场地放不下双面架。')
        else:
            structural_reason['reason']='已生成的双面候选未同时满足逐面可达、完整任务及冻结空间下限；具体候选和原因全部保留。有限搜索失败不代表全局不能放双面。'
    else:
        structural_reason['reason']='所选方案包含真实背靠背组合；两侧独立模块及每个外向取货面均经过几何和路线复验。'
    ablations=[stage_summary(s) for s in ('M09_TECHNICAL_BASELINE','STRUCTURE_ONLY','CORRIDOR_FIRST')]+[
        dict(stage='JOINT_SELECTION',status='PASS',candidate_id=chosen['candidate_id'],metrics=chosen_metrics,reason=reason)]
    result=dict(operation_mode=True,operations_input=value.model_dump(mode='json'),
        input_sha256=operations_digest(value),
        before=dict(layout=before.model_dump(mode='json'),measurements=before_metrics,elapsed_ms=before_ms,status='AVAILABLE'),
        after=dict(layout=selected.model_dump(mode='json'),measurements=chosen_metrics,
            elapsed_ms=(perf_counter()-begin)*1000,status='AVAILABLE'),
        selection=dict(policy=POLICY,reason=reason,candidate_count=len(records),
            eligible_count=sum(r['eligible'] for r in records),frontier_count=len(frontier),
            kept_baseline=chosen['candidate_id']=='M09',chosen_candidate_id=chosen['candidate_id'],
            objectives=[dict(metric=k,direction='MAX' if s>0 else 'MIN') for k,s in OBJECTIVES],
            ideal_observed_values=ideals,frontier=records,
            equivalent_candidates=equivalent_candidates,
            route_evaluation_count=len(route_cache),
            route_evidence_reuse_count=sum('route_evidence_reused_from' in row for row in records),
            structural_reason=structural_reason,
            mechanism_notes=['保留 M09 与原合法候选；新增逐取货面净宽区段候选。',
                '明确主轴与异向沿墙排共同使用 WALL_FRONT_LOOP 候选；不同组合及双向排端仍保持原净距。',
                '仅当余宽足够时增加 LOW/HIGH 两个中央余量单面分支；其背侧通道必须有对面取货面的完整服务依据。',
                '六项 Pareto 目标、归一化方式、固定任务及需求下限不变。'],
            ablations=ablations,
            fixed_demand=value.demand.model_dump(mode='json'),reference_coordinates_used=False,
            human_acceptance='PENDING',search_limit='Finite orthogonal wall/central candidates and rectilinear clearance graph; no global-optimality or infeasibility claim'),
        reference=dict(status='STRUCTURE_EVIDENCE_ONLY',note='参考画像仅为结构依据；没有回读货架坐标生成方案。真实前向、净层板、采购结构和实际作业效果尚未验证。'))
    if include_stage_layouts:
        # Optional benchmark evidence only. HTTP responses keep the two panels;
        # the offline caller can retain actual 2D evidence for each mechanism.
        result['stage_layouts']=[]
        for stage in ablations:
            if 'candidate_id' not in stage:continue
            identifier=stage['candidate_id']
            layout=layouts[identifier].model_dump(mode='json')
            layout['route_evaluation']=evaluations[identifier]
            result['stage_layouts'].append(dict(stage=stage['stage'],candidate_id=identifier,layout=layout))
    return result
