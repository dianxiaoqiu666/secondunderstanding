"""Obstacle-side alignment must improve structure without trading away length."""
from copy import deepcopy

from shared.explicit_planning import ExplicitPlanningInput, explicit_request
from shared.planning_v2 import LayoutV2
from services.planning import runs as baseline
from services.planning import search
from services.planning.measurements import measure_layout
from tests.planning_m09.independent.helpers import verify_instances


def centered_column(req):
    _, scope, templates = baseline._request(req)
    runs = baseline._candidate(req, scope, templates, 0, 400)
    shelves = [s for r in runs for s in r.modules]
    return LayoutV2(source=req.source, space=req.space, rules=req.rules, runs=runs,
                    shelves=shelves, bom=baseline._bom(shelves, templates),
                    metrics=baseline._metrics(scope, runs), validation={})


def test_column_aligns_outer_endpoints_without_losing_any_previous_metric(frozen_cases):
    req = explicit_request(ExplicitPlanningInput.model_validate(frozen_cases['column']['input']))
    before = centered_column(req)
    old = measure_layout(req, before)
    assert old['alignment_offset_mm'] == 40
    result, selection = search.optimize_layout(req)
    measured = measure_layout(req, result)
    assert measured['alignment_offset_mm'] == 0
    assert {k: v for k, v in measured.items() if k != 'alignment_offset_mm'} == {
        k: v for k, v in old.items() if k != 'alignment_offset_mm'}
    assert measured['effective_length_mm'] == 116400
    assert result.bom == before.bom
    verify_instances(req, result)
    assert selection['chosen']['kind'] == 'ADJACENT_ENDPOINT_ALIGNMENT'
    for original, aligned in zip(before.runs, result.runs):
        assert original.start_mm[1] == aligned.start_mm[1]
        assert original.available_length_mm == aligned.available_length_mm
        assert [s.material_id for s in original.modules] == [s.material_id for s in aligned.modules]


def test_rotated_obstacle_space_uses_its_own_neighbor_endpoints(frozen_cases):
    value = deepcopy(frozen_cases['column']['input'])
    for zone in [value['space']['boundary'], *value['space']['exclusions']]:
        zone['boundary_mm'] = [[-y, x] for x, y in zone['boundary_mm']]
    req = explicit_request(ExplicitPlanningInput.model_validate(value))
    result, selection = search.optimize_layout(req)
    measured = measure_layout(req, result)
    assert measured['effective_length_mm'] == 116400
    assert measured['alignment_offset_mm'] == 0
    assert all(r.direction_deg == 90 for r in result.runs)
    assert selection['chosen']['kind'] == 'ADJACENT_ENDPOINT_ALIGNMENT'
    verify_instances(req, result)


def test_unsafe_alignment_is_rejected_and_keeps_the_previous_safe_winner(frozen_cases, monkeypatch):
    req = explicit_request(ExplicitPlanningInput.model_validate(frozen_cases['column']['input']))
    previous = centered_column(req)
    original_align = search.align_run_endpoints

    def corrupt(*args):
        runs, moves = original_align(*args)
        if moves:
            runs[0].modules[0].footprint_mm = list(req.space.exclusions[0].boundary_mm)
        return runs, moves

    monkeypatch.setattr(search, 'align_run_endpoints', corrupt)
    result, selection = search.optimize_layout(req)
    assert result.runs == previous.runs and result.bom == previous.bom
    verify_instances(req, result)
    rejected = [e for e in selection['examined']
                if e['kind'] == 'ADJACENT_ENDPOINT_ALIGNMENT' and e['status'] == 'VALIDATION_FAILED']
    assert rejected and all(e['geometry_status'] == 'FAILED' for e in rejected)
