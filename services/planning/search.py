"""Choose centered runs and bounded endpoint alignments for explicit spaces.

The production CAD path still uses generate_layout. This additive entry reuses
its interval geometry, exact module composition and independent final checks.
It does not infer space, consult references or claim a global optimum.
"""
from collections import Counter
import hashlib
import json

from shared.planning_v2 import LayoutV2
from services.planning.engine import PlanningError
from services.planning.alignment import align_run_endpoints
from services.planning.measurements import measure_layout
from services.planning.runs import EPS, _bom, _candidate, _metrics, _request, generate_layout, validate_runs


POLICY = 'M09-adjacent-endpoint-alignment-v1'
OBJECTIVE = [
    'zero_geometry_violations', 'effective_length_at_least_old_baseline',
    'maximum_effective_length', 'minimum_two_module_run_count',
    'minimum_tail_waste', 'maximum_average_run_length',
    'maximum_average_modules_per_run', 'minimum_adjacent_endpoint_offset',
    'keep_baseline_on_equal_quality', 'keep_centered_on_equal_quality',
    'direction_depth_ascending',
]


def _geometry_key(layout):
    # Quality/hash metadata is not geometry; a centered candidate equal to the
    # old winner must not displace that already validated result.
    return json.dumps([run.model_dump(mode='json') for run in layout.runs],
                      sort_keys=True, separators=(',', ':'), allow_nan=False)


def _quality(measured):
    return (-measured['effective_length_mm'], measured['short_run_count'],
            measured['tail_waste_mm'], -measured['average_run_length_mm'],
            -measured['average_modules_per_run'],
            measured['alignment_offset_mm'] if measured['alignment_offset_mm'] is not None else 0.0)


def _seal(layout, candidate_count):
    layout.metrics['candidate_count'] = candidate_count
    canonical = json.dumps(layout.model_dump(mode='json'), sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False, allow_nan=False)
    layout.metrics['deterministic_sha256'] = hashlib.sha256(canonical.encode('utf-8')).hexdigest()
    layout.metrics['hash_definition'] = 'Canonical JSON before deterministic_sha256 and hash_definition are added'
    return layout


def optimize_layout(req):
    """Return (validated layout, deterministic selection evidence).

    A safe old winner remains eligible and sets a non-decreasing effective
    length floor. Failed alternatives are recorded individually. No result
    means this bounded search found none, not that the space is globally infeasible.
    """
    req, scope, templates = _request(req)
    baseline = None
    baseline_error = None
    try:
        baseline = generate_layout(req)
    except PlanningError as error:
        if error.code not in ('NO_SAFE_LAYOUT', 'VALIDATION_FAILED'):
            raise
        baseline_error = {'code': error.code, 'message': error.message}

    eligible = []
    examined = []
    rejected = Counter()
    seen = set()
    baseline_length = 0.0
    if baseline is not None:
        measured = measure_layout(req, baseline)
        baseline_length = measured['effective_length_mm']
        entry = {'kind': 'OLD_SELECTED_BASELINE', 'direction': baseline.runs[0].direction_deg,
                 'depth_mm': baseline.runs[0].depth_mm, 'status': 'ELIGIBLE', 'geometry_status': 'PASS',
                 'effective_length_mm': baseline_length}
        eligible.append((_quality(measured) + (0, 0, 0, 0), baseline, measured, entry))
        seen.add(_geometry_key(baseline))
        examined.append(entry)

    valid_count = int(baseline is not None)
    for direction in sorted(set(req.rules.orientation_candidates)):
        for depth in sorted({template.depth_mm for template in templates}):
            centered = None
            for variant, kind in enumerate(('CENTERED_BASELINE', 'ADJACENT_ENDPOINT_ALIGNMENT')):
                entry = {'kind': kind, 'direction': direction, 'depth_mm': depth}
                try:
                    if variant == 0:
                        centered = _candidate(req, scope, templates, direction, depth)
                        runs = centered
                    else:
                        if not centered:
                            continue
                        runs, moves = align_run_endpoints(req, scope, centered)
                        if not moves:
                            continue
                        entry['alignment_moves'] = moves
                    if not runs:
                        entry.update(status='EMPTY_GRID', geometry_status='NOT_EVALUATED')
                        rejected[entry['status']] += 1
                        examined.append(entry)
                        continue
                    shelves = [shelf for run in runs for shelf in run.modules]
                    layout = LayoutV2(source=req.source, space=req.space, rules=req.rules, runs=runs,
                                      shelves=shelves, bom=_bom(shelves, templates), metrics=_metrics(scope, runs),
                                      validation={})
                    geometry_key = _geometry_key(layout)
                    if geometry_key in seen:
                        entry.update(status='DUPLICATE_LAYOUT', geometry_status='NOT_RECHECKED')
                        rejected[entry['status']] += 1
                        examined.append(entry)
                        continue
                    # Length pruning does not establish geometric validity.
                    if layout.metrics['effective_length_mm'] + EPS < baseline_length:
                        entry.update(status='BELOW_BASELINE_EFFECTIVE_LENGTH', geometry_status='NOT_EVALUATED',
                                     effective_length_mm=layout.metrics['effective_length_mm'],
                                     reason='有效货架长度少于保留的已验证基线，不能成为最终方案。')
                        rejected[entry['status']] += 1
                        examined.append(entry)
                        continue
                    measured = measure_layout(req, layout)  # Includes all validate_runs checks.
                    seen.add(geometry_key)
                    valid_count += 1
                    entry.update(status='ELIGIBLE', geometry_status='PASS',
                                 effective_length_mm=measured['effective_length_mm'],
                                 short_run_count=measured['short_run_count'],
                                 tail_waste_mm=measured['tail_waste_mm'],
                                 alignment_offset_mm=measured['alignment_offset_mm'])
                    score = _quality(measured) + (1, variant, direction, depth)
                    eligible.append((score, layout, measured, entry))
                except PlanningError as error:
                    entry.update(status=error.code, geometry_status='FAILED', reason=error.message)
                    rejected[error.code] += 1
                examined.append(entry)

    if not eligible:
        raise PlanningError('NO_SAFE_LAYOUT',
                            '当前有界连续排搜索未找到安全方案；这不证明空间或模板全局无解。')
    final_failed = 0
    for _, result, measured, chosen in sorted(eligible, key=lambda candidate: candidate[0]):
        try:
            result.validation = validate_runs(req, result)
        except PlanningError as error:
            chosen.update(status='FINAL_VALIDATION_FAILED', geometry_status='FAILED', reason=error.message)
            rejected['FINAL_VALIDATION_FAILED'] += 1
            final_failed += 1
            continue
        break
    else:
        raise PlanningError('NO_SAFE_LAYOUT', '有限候选均未通过最终校验；这不证明空间或模板全局无解。')
    kept_baseline = result is baseline
    if not kept_baseline:
        result = _seal(result, len(eligible) - final_failed)
    reason = ('已检查的有限候选中，原方案指标最好或并列，保留原方案。' if kept_baseline else
              '在满足全部原硬约束且有效货架长度不少于旧方案的候选中，按固定指标顺序选择。')
    if chosen['kind'] == 'ADJACENT_ENDPOINT_ALIGNMENT':
        reason += f" 其中 {len(chosen['alignment_moves'])} 排沿各自可用区段对齐相邻长排的端点，保留原模块组合。"
    selection = {
        'policy': POLICY, 'objective_order': OBJECTIVE, 'kept_baseline': kept_baseline,
        'reason': reason,
        'candidate_count': len(eligible) - final_failed, 'examined_count': len(examined),
        'geometrically_valid_count': valid_count - final_failed, 'rejected_count': sum(rejected.values()),
        'rejected_by_reason': dict(sorted(rejected.items())),
        'baseline_available': baseline is not None, 'baseline_error': baseline_error,
        'baseline_effective_length_mm': baseline_length if baseline is not None else None,
        'chosen': chosen, 'selected_metrics': measured,
        'examined': examined,
        'search_limit': 'Centered grids by permitted direction/depth, plus within-interval alignment to adjacent longer-run endpoints; no cross-axis shifts, global optimum or infeasibility proof',
        'reference_coordinates_used': False,
    }
    return result, selection
