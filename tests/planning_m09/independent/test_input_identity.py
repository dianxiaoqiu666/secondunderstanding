"""Explicit algorithm geometry never becomes a claimed CAD confirmation."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from shared.explicit_planning import ExplicitPlanningInput, explicit_request


@pytest.mark.parametrize("field,value", [("input_kind", None), ("input_kind", ""),
    ("input_kind", "CONFIRMED"), ("name", None), ("name", ""), ("space", None),
    ("templates", None), ("templates", [])])
def test_missing_or_false_explicit_origin_is_rejected(field, value, frozen_cases):
    body = deepcopy(frozen_cases["medium"]["input"])
    body[field] = value
    with pytest.raises(ValidationError):
        ExplicitPlanningInput.model_validate(body)


@pytest.mark.parametrize("target,field,value", [
    ("root", "source", {"filename": "real.dxf", "sha256": "0"*64}),
    ("root", "confirmation", {"state": "CONFIRMED", "token": "f"*64}),
    ("root", "user_confirmed", True),
    ("space", "confirmation", {"state": "AUTO_VALIDATED", "confirmed_by": "fake CAD"}),
    ("space", "source_evidence", [{"kind": "UPLOADED_CAD", "sha256": "0"*64}]),
])
def test_claimed_cad_confirmation_cannot_be_injected_into_explicit_input(target, field, value, frozen_cases):
    body = deepcopy(frozen_cases["medium"]["input"])
    (body if target == "root" else body["space"])[field] = value
    with pytest.raises(ValidationError):
        ExplicitPlanningInput.model_validate(body)


def test_explicit_request_is_marked_algorithm_only_and_is_not_signed(frozen_cases):
    req = explicit_request(ExplicitPlanningInput.model_validate(frozen_cases["medium"]["input"]))
    assert req.space.confirmation.state == "SYNTHETIC_TEST"
    assert req.space.confirmation.token == ""
    assert req.space.source_evidence
    assert all(item.get("real_store") is False and item.get("cad_processed") is False
               for item in req.space.source_evidence)


def test_existing_cad_planning_endpoint_rejects_algorithm_identity(frozen_cases):
    from fastapi.testclient import TestClient
    from services.planning.app import app

    req = explicit_request(ExplicitPlanningInput.model_validate(frozen_cases["medium"]["input"]))
    with TestClient(app) as client:
        response = client.post("/plan-v2", json=req.model_dump(mode="json"))
    assert response.status_code == 422, "The new algorithm route must not weaken the existing CAD signature gate."
    assert response.json()["detail"]["code"] == "CONFIRMATION_OR_BUSINESS_INVALID"
