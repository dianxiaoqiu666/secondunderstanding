import pytest
from shared.planning_v2 import ProductPlacementPlan
from shared import confirmation
from services.understanding.app import load_business
from tools.benchmarks.spaces import cases,request

def test_confirmation_cannot_be_faked_or_reused_with_other_source(tmp_path,monkeypatch):
    monkeypatch.setattr(confirmation,'KEY_PATH',tmp_path/'confirmation.key')
    req=request('square',cases()['square'],load_business())
    with pytest.raises(ValueError,match='VALIDATED_SPACE_REQUIRED'):confirmation.verify_request(req)
    req.space.confirmation.state='CONFIRMED'
    req.space.confirmation.reviewed_fields=sorted(confirmation.REVIEW_FIELDS)
    confirmation.sign_request(req);confirmation.verify_request(req)
    bad=req.model_copy(deep=True);bad.source.sha256='b'*64
    with pytest.raises(ValueError,match='SOURCE_MISMATCH'):confirmation.verify_request(bad)
    bad=req.model_copy(deep=True);bad.space.boundary.holes_mm=[[(1,1),(2,1),(2,2),(1,1)]]
    with pytest.raises(ValueError,match='GEOMETRY_CHANGED'):confirmation.verify_request(bad)
    bad=req.model_copy(deep=True);bad.business.templates[0].length_mm=2000
    with pytest.raises(ValueError,match='SIGNATURE_INVALID'):confirmation.verify_request(bad)
    bad=req.model_copy(deep=True);bad.space.confirmation.confirmed_by='changed actor'
    with pytest.raises(ValueError,match='SIGNATURE_INVALID'):confirmation.verify_request(bad)
    bad=req.model_copy(deep=True);bad.rules.aisle_width_mm=600
    with pytest.raises(ValueError,match='SIGNATURE_INVALID'):confirmation.verify_request(bad)
    bad=req.model_copy(deep=True);bad.space.unresolved=['entrance unknown']
    with pytest.raises(ValueError,match='UNRESOLVED'):confirmation.verify_request(bad)

def test_real_unavailable_cannot_contain_simulated_sku():
    with pytest.raises(ValueError,match='Real SKU dimensions'):
        ProductPlacementPlan(sku_assignments=[{'product_id':'pretend'}])

def test_signer_rejects_incomplete_review_without_creating_key(tmp_path,monkeypatch):
    key=tmp_path/'never-created.key'
    monkeypatch.setattr(confirmation,'KEY_PATH',key)
    req=request('square',cases()['square'],load_business())
    with pytest.raises(ValueError,match='VALIDATED_SPACE_REQUIRED'):confirmation.sign_request(req)
    req.space.confirmation.state='CONFIRMED'
    with pytest.raises(ValueError,match='SPATIAL_REVIEW_INCOMPLETE'):confirmation.sign_request(req)
    req.space.confirmation.reviewed_fields=sorted(confirmation.REVIEW_FIELDS)
    req.space.unresolved=['unknown boundary']
    with pytest.raises(ValueError,match='UNRESOLVED'):confirmation.sign_request(req)
    assert not key.exists()
