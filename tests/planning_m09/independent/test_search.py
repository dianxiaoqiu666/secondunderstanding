"""Independent checks on the six fixed cases and one untouched holdout."""
from copy import deepcopy
import json

import pytest
from shapely.geometry import Polygon

from shared.explicit_planning import ExplicitPlanningInput, explicit_request
from shared.planning_v2 import LayoutV2
from services.planning import runs as baseline
from services.planning.measurements import measure_layout
from tests.planning_m09.independent.conftest import ALL_CASES
from tests.planning_m09.independent.helpers import effective_length, entrance_average_trap, verify_instances


@pytest.mark.parametrize("name", ALL_CASES)
def test_fixed_inputs_have_complete_safe_instances_and_do_not_lose_baseline_length(name, frozen_cases, optimized_case):
    req, result, selection = optimized_case(name)
    verify_instances(req, result)
    assert baseline.validate_runs(req, result)["status"] == "PASS"
    before = LayoutV2.model_validate(frozen_cases[name]["before"]["layout"])
    assert effective_length(result) >= effective_length(before) - 1e-6
    measured = measure_layout(req, result)
    assert measured["effective_length_mm"] == pytest.approx(effective_length(result))
    assert measured["continuous_length_mm"] == measured["effective_length_mm"]
    assert measured["short_run_count"] == sum(len(run.modules) == 2 for run in result.runs)
    assert measured["bom_quantity"] == len(result.shelves)
    assert measured["geometry_violation_count"] == 0
    assert selection, "The selection must disclose its comparison evidence."
    assert selection["selected_metrics"] == measured
    eligible = [entry for entry in selection["examined"] if entry["status"] == "ELIGIBLE"]
    assert len(eligible) == selection["candidate_count"]
    assert effective_length(result) == pytest.approx(max(entry["effective_length_mm"] for entry in eligible))
    assert selection["baseline_effective_length_mm"] == pytest.approx(effective_length(before))
    for entry in selection["examined"]:
        if entry["status"] == "BELOW_BASELINE_EFFECTIVE_LENGTH":
            assert entry["geometry_status"] == "NOT_EVALUATED", "Pruned candidates must not be labelled geometrically verified."
    if selection["kept_baseline"]:
        assert result.model_dump(mode="json") == before.model_dump(mode="json")


@pytest.mark.parametrize("name", ALL_CASES)
def test_original_generator_still_reproduces_the_frozen_before_layout(name, frozen_cases):
    req = explicit_request(ExplicitPlanningInput.model_validate(frozen_cases[name]["input"]))
    assert baseline.generate_layout(req).model_dump(mode="json") == frozen_cases[name]["before"]["layout"]


def test_column_average_modules_is_not_a_reason_to_discard_existing_safe_length(frozen_cases, optimized_case):
    req, result, _ = optimized_case("column")
    _, scope, templates = baseline._request(req)
    lengths = []
    for direction in req.rules.orientation_candidates:
        for depth in sorted({t.depth_mm for t in templates}):
            runs = baseline._candidate(req, scope, templates, direction, depth)
            if not runs:
                continue
            shelves = [module for run in runs for module in run.modules]
            alternative = LayoutV2(source=req.source, space=req.space, rules=req.rules,
                runs=runs, shelves=shelves, bom=baseline._bom(shelves, templates),
                metrics=baseline._metrics(scope, runs), validation={})
            verify_instances(req, alternative)
            assert baseline.validate_runs(req, alternative)["status"] == "PASS"
            lengths.append(effective_length(alternative))
    old = LayoutV2.model_validate(frozen_cases["column"]["before"]["layout"])
    assert max(lengths) > effective_length(old), "The frozen case must retain the actual old-ranking counterexample."
    assert effective_length(result) >= max(lengths) - 1e-6


def test_entrance_average_improves_when_a_safe_row_is_dropped_but_search_must_not_accept_the_loss(frozen_cases, optimized_case):
    req, result, _ = optimized_case("entrance")
    old = LayoutV2.model_validate(frozen_cases["entrance"]["before"]["layout"])
    trap = entrance_average_trap(req, frozen_cases["entrance"]["before"]["layout"])
    assert effective_length(trap) < effective_length(old)
    assert len(trap.shelves) / len(trap.runs) > len(old.shelves) / len(old.runs)
    assert all(len(run.modules) >= req.rules.min_modules_per_run for run in [*old.runs, *trap.runs])
    assert effective_length(result) >= effective_length(old) - 1e-6


@pytest.mark.parametrize("name", ["medium", "column", "entrance"])
def test_repeated_input_is_exactly_deterministic_and_does_not_change_facts(name, optimized_case):
    from services.planning.search import optimize_layout
    req, original, selection = optimized_case(name)
    before = req.model_dump(mode="json")
    again, repeated_selection = optimize_layout(req)
    assert again.model_dump(mode="json") == original.model_dump(mode="json")
    assert repeated_selection == selection
    assert req.model_dump(mode="json") == before


@pytest.mark.parametrize("name", ["medium", "column", "entrance"])
def test_template_order_changes_no_physical_choice(name, frozen_cases, optimized_case):
    from services.planning.search import optimize_layout
    _, before, _ = optimized_case(name)
    value = deepcopy(frozen_cases[name]["input"])
    value["templates"].reverse()
    req = explicit_request(ExplicitPlanningInput.model_validate(value))
    after, _ = optimize_layout(req)
    assert after.shelves == before.shelves and after.runs == before.runs and after.bom == before.bom
    verify_instances(req, after)


@pytest.mark.parametrize("name", ["medium", "column", "entrance"])
def test_geometry_translation_preserves_physical_design(name, frozen_cases, optimized_case):
    from services.planning.search import optimize_layout
    _, before, _ = optimized_case(name)
    value = deepcopy(frozen_cases[name]["input"])
    dx, dy = 135000, -97000
    space = value["space"]
    for region in [space["boundary"], *space["exclusions"], *space["reserved_passages"]]:
        region["boundary_mm"] = [[x+dx, y+dy] for x, y in region["boundary_mm"]]
        region["holes_mm"] = [[[x+dx, y+dy] for x, y in ring] for ring in region["holes_mm"]]
    for wall in space["barriers"]:
        for key in ("start_mm", "end_mm"):
            wall[key] = [wall[key][0]+dx, wall[key][1]+dy]
    req = explicit_request(ExplicitPlanningInput.model_validate(value))
    after, _ = optimize_layout(req)
    assert len(after.runs) == len(before.runs) and after.bom == before.bom
    for original, shifted in zip(before.shelves, after.shelves):
        assert (shifted.id, shifted.material_id, shifted.rotation_deg) == (original.id, original.material_id, original.rotation_deg)
        assert shifted.x_mm-original.x_mm == pytest.approx(dx, abs=1e-5)
        assert shifted.y_mm-original.y_mm == pytest.approx(dy, abs=1e-5)
    verify_instances(req, after)


def test_search_performs_no_file_reads_after_receiving_explicit_standard_data(monkeypatch, frozen_cases):
    import builtins
    from pathlib import Path
    from services.planning.search import optimize_layout

    req = explicit_request(ExplicitPlanningInput.model_validate(frozen_cases["medium"]["input"]))
    def forbidden(*args, **kwargs):
        raise AssertionError("The pure optimizer must not fetch CAD, cached layouts or reference coordinates.")
    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(Path, "read_text", forbidden)
        patch.setattr(Path, "read_bytes", forbidden)
        result, selection = optimize_layout(req)
    verify_instances(req, result)
    json.dumps(selection, allow_nan=False)


@pytest.mark.parametrize("corruption", ["footprint", "count", "levels"])
def test_selected_layout_cannot_hide_a_corrupted_footprint_or_material_list(corruption, optimized_case):
    from services.planning.engine import PlanningError
    req, result, _ = optimized_case("column")
    if corruption == "footprint":
        result.shelves[0].footprint_mm = list(Polygon(req.space.exclusions[0].boundary_mm).exterior.coords)
    elif corruption == "count":
        result.bom[0].quantity += 1
    else:
        result.bom[0].default_level_count += 1
    with pytest.raises(PlanningError):
        measure_layout(req, result)


def test_final_validation_failure_falls_back_to_a_rechecked_safe_candidate(monkeypatch, frozen_cases):
    from services.planning import search
    from services.planning.engine import PlanningError

    req = explicit_request(ExplicitPlanningInput.model_validate(frozen_cases["column"]["input"]))
    old = LayoutV2.model_validate(frozen_cases["column"]["before"]["layout"])
    actual_check = search.validate_runs
    attempted = []

    def reject_first(request, layout):
        attempted.append([run.model_dump(mode="json") for run in layout.runs])
        if len(attempted) == 1:
            raise PlanningError("VALIDATION_FAILED", "Independent test: reject the first final candidate.")
        return actual_check(request, layout)

    with monkeypatch.context() as patch:
        patch.setattr(search, "validate_runs", reject_first)
        result, selection = search.optimize_layout(req)

    assert len(attempted) == 2 and attempted[0] != attempted[1]
    assert attempted[-1] == [run.model_dump(mode="json") for run in result.runs]
    verify_instances(req, result)
    assert result.validation["status"] == "PASS"
    assert effective_length(result) >= effective_length(old)
    assert selection["selected_metrics"] == measure_layout(req, result)
    assert selection["chosen"]["status"] == "ELIGIBLE"
    assert selection["chosen"]["geometry_status"] == "PASS"
    failed = [entry for entry in selection["examined"] if entry["status"] == "FINAL_VALIDATION_FAILED"]
    eligible = [entry for entry in selection["examined"] if entry["status"] == "ELIGIBLE"]
    assert len(failed) == selection["rejected_by_reason"]["FINAL_VALIDATION_FAILED"] == 1
    assert all(entry["geometry_status"] == "FAILED" for entry in failed)
    assert len(eligible) == selection["candidate_count"] == selection["geometrically_valid_count"]
    assert selection["rejected_count"] + selection["candidate_count"] == selection["examined_count"]


def test_no_layout_is_returned_when_every_final_validation_fails(monkeypatch, frozen_cases):
    from services.planning import search
    from services.planning.engine import PlanningError

    req = explicit_request(ExplicitPlanningInput.model_validate(frozen_cases["column"]["input"]))
    original = req.model_dump(mode="json")
    attempts = []

    def reject_all(request, layout):
        attempts.append(layout)
        raise PlanningError("VALIDATION_FAILED", "Independent test: reject every final candidate.")

    with monkeypatch.context() as patch:
        patch.setattr(search, "validate_runs", reject_all)
        with pytest.raises(PlanningError) as failure:
            search.optimize_layout(req)
    assert attempts
    assert failure.value.code == "NO_SAFE_LAYOUT"
    assert req.model_dump(mode="json") == original
