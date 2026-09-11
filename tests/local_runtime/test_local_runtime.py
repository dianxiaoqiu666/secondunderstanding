"""Migration-specific checks for source isolation and launcher ownership."""
import json
from pathlib import Path
import sys

import pytest

from scripts import start as launcher
from shared import confirmation
from shared.explicit_planning import ExplicitPlanningInput, explicit_request


@pytest.mark.parametrize('wrong_root,wrong_time,wrong_command', [(True,False,False),(False,True,False),(False,False,True)])
def test_stop_ignores_foreign_or_stale_owner(tmp_path, monkeypatch, wrong_root, wrong_time, wrong_command):
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    monkeypatch.setattr(launcher, 'STATE', tmp_path / 'processes.json')
    monkeypatch.setattr(launcher, 'STOP', tmp_path / 'stop.request')
    state = {'project_root': str(tmp_path / 'another' if wrong_root else tmp_path),
             'supervisor': 42, 'supervisor_created': 10, 'run_id': 'test-run', 'status': 'ready'}
    launcher.STATE.write_text(json.dumps(state), encoding='utf-8')
    class Process:
        def __init__(self, pid): pass
        def create_time(self): return 11 if wrong_time else 10
        def cwd(self): return str(tmp_path)
        def cmdline(self): return [sys.executable, 'other.py' if wrong_command else str(Path(launcher.__file__).resolve())]
    monkeypatch.setattr(launcher.psutil, 'Process', Process)
    launcher.request_stop()
    assert not launcher.STOP.exists()


def test_stop_request_binds_verified_run(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, 'ROOT', tmp_path)
    monkeypatch.setattr(launcher, 'STATE', tmp_path / 'processes.json')
    monkeypatch.setattr(launcher, 'STOP', tmp_path / 'stop.request')
    launcher.STATE.write_text(json.dumps({'project_root':str(tmp_path),'supervisor':42,
        'supervisor_created':10,'run_id':'test-run','status':'ready'}), encoding='utf-8')
    class Process:
        def __init__(self, pid): pass
        def create_time(self): return 10
        def cwd(self): return str(tmp_path)
        def cmdline(self): return [sys.executable,str(Path(launcher.__file__).resolve())]
    monkeypatch.setattr(launcher.psutil, 'Process', Process)
    launcher.request_stop()
    assert launcher.STOP.read_text(encoding='utf-8') == 'test-run'


def test_confirmation_integrity_with_synthetic_business_and_isolated_key(tmp_path, monkeypatch):
    value = ExplicitPlanningInput(input_kind='ALGORITHM_VALIDATION', name='migration-signature',
        space={'boundary':{'boundary_mm':[(0,0),(8000,0),(8000,6000),(0,6000),(0,0)]}},
        templates=[{'material_id':'TEST-1200','length_mm':1200,'depth_mm':400,'default_level_count':4}])
    req = explicit_request(value)
    monkeypatch.setattr(confirmation, 'KEY_PATH', tmp_path / 'test-only.key')
    with pytest.raises(ValueError, match='VALIDATED_SPACE_REQUIRED'):
        confirmation.verify_request(req)
    assert not confirmation.KEY_PATH.exists()
    req.space.confirmation.state = 'CONFIRMED'
    req.space.confirmation.reviewed_fields = sorted(confirmation.REVIEW_FIELDS)
    confirmation.sign_request(req)
    confirmation.verify_request(req)
    for field, changed, error in [('source','b'*64,'SOURCE_MISMATCH'),('rules',1300,'SIGNATURE_INVALID'),('template',1300,'SIGNATURE_INVALID')]:
        other = req.model_copy(deep=True)
        if field == 'source': other.source.sha256 = changed
        elif field == 'rules': other.rules.aisle_width_mm = changed
        else: other.business.templates[0].length_mm = changed
        with pytest.raises(ValueError, match=error):
            confirmation.verify_request(other)
