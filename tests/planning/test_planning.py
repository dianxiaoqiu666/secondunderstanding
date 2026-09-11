"""Safety attacks against the independent planner and its final verification gate."""
import copy
import hashlib
import json
import math

from fastapi.testclient import TestClient
import pytest
from shapely.geometry import Point, Polygon, box

from shared.contracts import LayoutResult, UnderstandingPackage
from services.planning.app import failed_baseline_app as app  # Explicit historical regression only.
from services.planning.engine import PlanningError, _nav_buffer, plan_package, validate_layout


def ring(x1, y1, x2, y2):
    return list(box(x1, y1, x2, y2).exterior.coords)


def package(width=18000, height=15000):
    return UnderstandingPackage.model_validate({
        "source": {"filename": "synthetic-scope.dxf", "sha256": "0" * 64},
        "spatial": {"scope": {"boundary_mm": ring(0, 0, width, height)},
                    "walls": [], "exclusion_zones": []},
        "business": {"products": [{"name": "Unmeasured product 1200x400", "width_mm": None,
                                   "depth_mm": None, "height_mm": None}], "product_count": 1,
                     "templates": [
                         {"material_id": "S-1200-400", "length_mm": 1200, "depth_mm": 400, "default_level_count": 6},
                         {"material_id": "S-900-300", "length_mm": 900, "depth_mm": 300, "default_level_count": 4}],
                     "sources": []}})


def test_repeatable_hash_and_configured_bom():
    p = package()
    first, second = plan_package(p), plan_package(p)
    assert first.model_dump_json() == second.model_dump_json()
    assert first.validation["status"] == "PASS"
    assert sum(row.quantity for row in first.bom) == len(first.shelves)
    assert first.business_summary["physical_dimensions_known"] == 0
    document = first.model_dump(mode="json")
    digest = document["planning"].pop("deterministic_sha256")
    document["planning"].pop("hash_definition")
    assert digest == hashlib.sha256(json.dumps(document, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
    assert first.planning["candidate_count"] == 16


def test_hole_wall_exclusion_and_conservative_clearance():
    document = package(24000, 18000).model_dump()
    document["spatial"]["scope"]["holes_mm"] = [ring(8000, 7000, 11000, 10000)]
    document["spatial"]["walls"] = [{"id": "W1", "start_mm": [15000, 0], "end_mm": [15000, 7000]}]
    document["spatial"]["exclusion_zones"] = [{"id": "E1", "boundary_mm": ring(18000, 10000, 22000, 14000)}]
    p = UnderstandingPackage.model_validate(document)
    result = plan_package(p)
    scope = Polygon(p.spatial.scope.boundary_mm, p.spatial.scope.holes_mm)
    assert len(result.shelves) > 5
    assert result.validation["minimum_clearance_mm"] >= 1200
    for s in result.shelves:
        f = Polygon(s.footprint_mm)
        assert scope.covers(f)
        assert f.distance(Polygon(ring(8000, 7000, 11000, 10000))) >= 1200
    assert result.spatial == p.spatial
    assert result.validation["navigation"]["after_selected_component_count"] == 1


def test_disconnected_navigation_uses_one_component_without_inventing_entrance():
    p = package().model_dump()
    p["spatial"]["walls"] = [{"id": "divider", "start_mm": [11000, 0], "end_mm": [11000, 15000]}]
    result = plan_package(p)
    assert result.validation["navigation"]["base_component_count"] == 2
    assert all(s.x_mm < 11000 for s in result.shelves)
    assert result.validation["navigation"]["entrance_reachability"] == "UNKNOWN_NO_ENTRANCE_CONTRACT"


@pytest.mark.parametrize("mutation", ["outside", "overlap", "aisle", "rotation", "dimensions", "levels", "bom", "identity", "fake_footprint"])
def test_final_validation_rejects_tampered_results(mutation):
    p = package()
    result = plan_package(p)
    s = result.shelves[0]
    if mutation in ("outside", "overlap", "aisle"):
        if mutation == "outside":
            x, y = -10, s.y_mm
        elif mutation == "overlap":
            x, y = result.shelves[1].x_mm, result.shelves[1].y_mm
        else:
            x, y = result.shelves[1].x_mm - 1, result.shelves[1].y_mm - 100
        dx, dy = x - s.x_mm, y - s.y_mm
        s.x_mm, s.y_mm = x, y
        s.footprint_mm = [(px + dx, py + dy) for px, py in s.footprint_mm]
    elif mutation == "rotation":
        s.rotation_deg = 90 if s.rotation_deg == 0 else 0
    elif mutation == "dimensions":
        s.length_mm += 1
    elif mutation == "levels":
        s.default_level_count += 1
    elif mutation == "bom":
        result.bom[0].quantity += 1
    elif mutation == "identity":
        s.id = result.shelves[1].id
    else:
        s.footprint_mm = ring(s.x_mm - 1, s.y_mm - 1, s.x_mm + 1, s.y_mm + 1)
    with pytest.raises(PlanningError, match=".") as error:
        validate_layout(p, result)
    assert error.value.code == "VALIDATION_FAILED"


@pytest.mark.parametrize("case", ["unclosed", "bowtie", "outside_hole", "zero_wall", "duplicate_template", "bad_product_count"])
def test_invalid_standard_input_fails_closed(case):
    p = package().model_dump()
    if case == "unclosed":
        p["spatial"]["scope"]["boundary_mm"][-1] = [123, 456]
    elif case == "bowtie":
        p["spatial"]["scope"]["boundary_mm"] = [(0, 0), (20000, 20000), (20000, 0), (0, 20000), (0, 0)]
    elif case == "outside_hole":
        p["spatial"]["scope"]["holes_mm"] = [ring(40000, 40000, 50000, 50000)]
    elif case == "zero_wall":
        p["spatial"]["walls"] = [{"id": "bad", "start_mm": [1, 1], "end_mm": [1, 1]}]
    elif case == "duplicate_template":
        p["business"]["templates"].append(copy.deepcopy(p["business"]["templates"][0]))
    else:
        p["business"]["product_count"] = 2
    with pytest.raises(PlanningError) as error:
        plan_package(p)
    assert error.value.code in ("INVALID_SPATIAL", "INVALID_BUSINESS")


def test_too_small_scope_and_full_exclusion_fail():
    with pytest.raises(PlanningError) as error:
        plan_package(package(2000, 2000))
    assert error.value.code == "NO_SAFE_LAYOUT"
    p = package().model_dump()
    p["spatial"]["exclusion_zones"] = [{"id": "full", "boundary_mm": ring(0, 0, 18000, 15000)}]
    with pytest.raises(PlanningError) as error:
        plan_package(p)
    assert error.value.code == "NO_SAFE_LAYOUT"


def test_api_contract_health_success_and_failure():
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok", "service": "planning"}
        success = client.post("/plan", json=package().model_dump(mode="json"))
        assert success.status_code == 200
        LayoutResult.model_validate(success.json())
        small = client.post("/plan", json=package(1000, 1000).model_dump(mode="json"))
        assert small.status_code == 422
        assert small.json()["detail"]["code"] == "NO_SAFE_LAYOUT"
        missing = package().model_dump(mode="json")
        del missing["spatial"]["scope"]
        response = client.post("/plan", json=missing)
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "INVALID_STANDARD_PACKAGE"


def test_navigation_arc_chords_never_underestimate_true_clearance():
    buffered = _nav_buffer(Point(0, 0))
    # Mid-chord directions are where an ordinary GEOS radius-600 buffer
    # underestimates a round clearance envelope the most.
    for i in range(4096):
        angle = i * 2 * math.pi / 4096
        sample = Point(600 * math.cos(angle), 600 * math.sin(angle))
        assert buffered.distance(sample) < 1e-8
