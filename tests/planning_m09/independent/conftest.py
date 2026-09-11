"""The fixed M09 cases are read-only evidence, including the held-out case."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from shared.explicit_planning import ExplicitPlanningInput, explicit_request
from shared.planning_v2 import LayoutV2


ROOT = Path(__file__).resolve().parents[3]
SCENARIOS = ROOT / "tools/benchmarks/scenarios_m09.json"
BEFORE = ROOT / "outputs/planning_m09/before"
MAIN_CASES = ["medium", "square", "long", "column", "edge_block", "entrance"]
ALL_CASES = [*MAIN_CASES, "concave"]


@pytest.fixture(scope="session")
def frozen_cases():
    document = json.loads(SCENARIOS.read_text(encoding="utf-8-sig"))
    manifest = json.loads((BEFORE / "manifest.json").read_text(encoding="utf-8-sig"))
    assert [item["id"] for item in document["scenarios"]] == ALL_CASES
    assert {item["id"] for item in document["scenarios"] if item["group"] == "holdout"} == {"concave"}
    catalog = ROOT / "input/production/materials/material_templates.v1.json"
    assert hashlib.sha256(catalog.read_bytes()).hexdigest() == document["catalog_sha256"]
    snapshots = {SCENARIOS: SCENARIOS.read_bytes(), catalog: catalog.read_bytes()}
    # The corrected manifest lists evidence files, not a hash of itself.
    assert "manifest.json" not in manifest
    for filename, expected in manifest.items():
        path = BEFORE / filename
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected
        snapshots[path] = raw
    cases = {}
    for item in document["scenarios"]:
        path = BEFORE / (item["id"] + ".json")
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == manifest[path.name]
        record = json.loads(raw.decode("utf-8-sig"))
        assert record["input"] == item["input"]
        req = explicit_request(ExplicitPlanningInput.model_validate(item["input"]))
        assert req.source.sha256 == record["input_sha256"]
        assert record["baseline_head"] == document["baseline_head"]
        assert LayoutV2.model_validate(record["layout"]).source == req.source
        snapshots[path] = raw
        cases[item["id"]] = {**deepcopy(item), "before": record}
    yield cases
    for path, raw in snapshots.items():
        assert path.read_bytes() == raw, f"Independent verification must not rewrite {path.name}."


@pytest.fixture(scope="session")
def optimized_case(frozen_cases):
    from services.planning.search import optimize_layout

    cache = {}

    def obtain(name):
        if name not in cache:
            req = explicit_request(ExplicitPlanningInput.model_validate(frozen_cases[name]["input"]))
            original = req.model_dump(mode="json")
            result, selection = optimize_layout(req)
            assert isinstance(result, LayoutV2) and isinstance(selection, dict)
            assert req.model_dump(mode="json") == original, "Optimization must not edit supplied facts or rules."
            json.dumps(selection, sort_keys=True, allow_nan=False)
            cache[name] = (req, result, selection)
        req, result, selection = cache[name]
        return req.model_copy(deep=True), result.model_copy(deep=True), deepcopy(selection)

    return obtain
