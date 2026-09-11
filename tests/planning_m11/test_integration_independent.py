"""Independent contracts for scene migration, baseline honesty and materials.

The M11 independent holdout is never opened or evaluated. Old fixtures are read
only to prove preservation. One small self-contained ASGI result is reused by
the transport mutations; no development output cache is required.
"""
from collections import Counter
from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from openpyxl import load_workbook
import pytest

from services.delivery import algorithm_lab
from services.delivery.exports_v2 import make_workbook
from services.planning.app import app as planning_app
from services.planning.baselines import m10_comparison, m10_selection
from services.planning.engine import PlanningError
from services.planning.products import assign_categories
from shared.contracts import Business
from shared.operations_planning import OperationsInput, OperationsLayout, operations_digest


ROOT = Path(__file__).resolve().parents[2]


def rows(filename):
    return json.loads((ROOT / 'tools/benchmarks' / filename).read_text(encoding='utf-8-sig'))['scenarios']


@pytest.mark.parametrize('identifier', ['medium', 'square', 'long', 'column', 'edge_block', 'entrance', 'concave'])
def test_all_old_geometry_fields_are_preserved_exactly(identifier):
    old = next(row for row in rows('scenarios_m09.json') if row['id'] == identifier)
    current = next(row for row in rows('scenarios_m11.json') if row['id'] == identifier)
    assert current['input']['base'] == old['input']
    assert current['mode'] == 'EMPLOYEE_OPERATIONS'
    assert current['evaluation_role'] == 'UPGRADED_GEOMETRY_REGRESSION'
    assert current['group'] == 'main'


@pytest.mark.parametrize('identifier', [
    'op-short', 'op-long', 'op-corner', 'op-shared', 'op-split',
    'op-square', 'op-column', 'op-holdout', 'op-independent-holdout'])
def test_all_old_tasks_floors_positions_and_rules_are_preserved_exactly(identifier):
    old = next(row for row in rows('scenarios_m10.json') + rows('scenarios_m10_holdout.json')
               if row['id'] == identifier)
    current = next(row for row in rows('scenarios_m11.json') if row['id'] == identifier)
    assert current['input']['base'] == old['input']['base']
    # Includes every remote item, item order, destination fraction, task order,
    # placement/route policy and both minimum-space requirements.
    assert current['input']['demand'] == old['input']['demand']
    assert current['input']['assembly_assumptions'] == old['input']['assembly_assumptions']
    assert current['input']['entrance_opening_mm'] == old['input']['entrance_opening_mm']
    for role in ('receiving', 'pick_start', 'packing'):
        assert current['input']['workpoints'][role] == old['input']['workpoints'][role]
    assert current['group'] == 'main'
    if identifier == 'op-holdout':
        assert current['evaluation_role'] == 'POST_CORRECTION_REGRESSION'
    if identifier == 'op-independent-holdout':
        assert current['evaluation_role'] == 'PREVIOUS_FAILED_HOLDOUT_REGRESSION'


def independent_input():
    """Contract fixture only; it cannot alter frozen benchmark demand floors."""
    return OperationsInput.model_validate({
        'base': {
            'input_kind': 'ALGORITHM_VALIDATION', 'name': 'M11-independent-integration',
            'space': {'boundary': {'boundary_mm': [[0, 0], [12000, 0], [12000, 9000], [0, 9000], [0, 0]]}},
            'templates': [
                {'material_id': 'TEST-1200-400', 'length_mm': 1200, 'depth_mm': 400, 'default_level_count': 6},
                {'material_id': 'TEST-1800-400', 'length_mm': 1800, 'depth_mm': 400, 'default_level_count': 7}],
            'rules': {'orientation_candidates': [0]}},
        'usable_walls': [
            {'id': 'bottom', 'start_mm': [0, 0], 'end_mm': [12000, 0], 'inward_normal': [0, 1], 'provenance': 'SYNTHETIC_TEST'},
            {'id': 'top', 'start_mm': [12000, 9000], 'end_mm': [0, 9000], 'inward_normal': [0, -1], 'provenance': 'SYNTHETIC_TEST'}],
        'entrance_opening_mm': [[0, 3300], [0, 5700]],
        'physical_perimeter_walls': [
            {'id': f'wall-{index}', 'start_mm': a, 'end_mm': b}
            for index, (a, b) in enumerate([
                ([0, 0], [12000, 0]), ([12000, 0], [12000, 9000]), ([12000, 9000], [0, 9000]),
                ([0, 9000], [0, 5700]), ([0, 3300], [0, 0])])],
        'door_connection': {
            'id': 'outside-door', 'inside_point_mm': [600, 4500], 'outside_point_mm': [-600, 4500],
            'connection_region': {'boundary_mm': [[-600, 3300], [600, 3300], [600, 5700], [-600, 5700], [-600, 3300]]},
            'provenance': 'SYNTHETIC_TEST'},
        'workpoints': {role: {'point_mm': point} for role, point in {
            'entrance': [-600, 4500], 'receiving': [600, 2100],
            'pick_start': [600, 4500], 'packing': [600, 6900]}.items()},
        'demand': {
            'items': [{'id': name, 'target_fraction': target} for name, target in [
                ('near-low', [.2, .2]), ('far-low', [.8, .2]), ('near-high', [.2, .8]), ('far-high', [.8, .8])]],
            'picking_tasks': [
                {'id': 'P-low', 'item_ids': ['near-low', 'far-low']},
                {'id': 'P-high', 'item_ids': ['near-high', 'far-high']},
                {'id': 'P-far-diagonal', 'item_ids': ['near-low', 'far-high']}],
            'replenishment_tasks': [
                {'id': 'R-near', 'item_ids': ['near-low', 'near-high']},
                {'id': 'R-far', 'item_ids': ['far-low', 'far-high']}],
            'minimum_pick_length_mm': 14400, 'minimum_nominal_board_area_m2': 34.56}})


def record(value):
    return {'id': 'independent-integration', 'title': '独立契约测试', 'group': 'main',
            'mode': 'EMPLOYEE_OPERATIONS', 'input': value.model_dump(mode='json'),
            'input_sha256': operations_digest(value)}


def local_client():
    app = FastAPI()
    app.include_router(algorithm_lab.router)
    return TestClient(app, raise_server_exceptions=False)


def install_catalog(monkeypatch, tmp_path, entry):
    main = tmp_path / 'main.json'
    holdout = tmp_path / 'empty-secondary.json'
    main.write_text(json.dumps({'schema_version': 'independent-test', 'scenarios': [entry]}), encoding='utf-8')
    holdout.write_text(json.dumps({'scenarios': []}), encoding='utf-8')
    monkeypatch.setattr(algorithm_lab, 'CURRENT_OPERATIONS_SCENARIOS', main)
    # Explicit empty local file prevents opening the unseen M11 holdout.
    monkeypatch.setattr(algorithm_lab, 'CURRENT_INDEPENDENT_HOLDOUT', holdout)


@pytest.mark.parametrize('missing', ['door_connection', 'physical_perimeter_walls'])
def test_missing_door_facts_are_local_configuration_errors(monkeypatch, tmp_path, missing):
    value = independent_input()
    setattr(value, missing, None if missing == 'door_connection' else [])
    install_catalog(monkeypatch, tmp_path, record(value))
    with local_client() as client:
        response = client.get('/api/algorithm/scenarios')
    assert response.status_code == 422, response.text
    error = response.json()['detail']
    assert error['code'] == 'SCENARIO_CONFIGURATION_INVALID'
    assert '实体墙' in error['message'] or '门口' in error['message']
    assert '无需 CAD 或人工范围确认' in error['message']


def test_missing_catalog_file_is_configuration_error_not_unhandled_500(monkeypatch, tmp_path):
    install_catalog(monkeypatch, tmp_path, record(independent_input()))
    missing = tmp_path / 'missing-scene-catalog.json'
    monkeypatch.setattr(algorithm_lab, 'CURRENT_OPERATIONS_SCENARIOS', missing)
    with local_client() as client:
        response = client.get('/api/algorithm/scenarios')
    assert response.status_code == 422, response.text
    error = response.json()['detail']
    assert error['code'] == 'SCENARIO_CONFIGURATION_INVALID'
    assert missing.name in error['message']


def test_m10_adapter_changes_only_its_private_inside_construction_anchor(monkeypatch):
    value = independent_input()
    original = value.model_dump(mode='json')
    received = []
    def capture(candidate, direction, depth):
        received.append(candidate.model_dump(mode='json'))
        assert (direction, depth) == (0, 400)
        return []
    monkeypatch.setattr(m10_comparison, 'frozen_corridors', capture)
    assert m10_comparison.corridor_plans(value, 0, 400) == []
    expected = deepcopy(original)
    expected['workpoints']['entrance']['point_mm'] = original['door_connection']['inside_point_mm']
    assert received == [expected]
    assert value.model_dump(mode='json') == original


def not_found(value):
    return {'status': 'NOT_FOUND', 'layout': None, 'measurements': None, 'elapsed_ms': None,
            'error': {'code': 'NO_OPERATIONAL_CANDIDATE', 'message': '固定需求下没有可发布候选，未降低需求。'},
            'input_sha256': operations_digest(value), 'algorithm': 'M10_FROZEN_WITH_CURRENT_DOOR_ROUTE_MEASUREMENT'}


def test_frozen_m10_failure_has_no_layout_or_partially_averaged_measurements(monkeypatch):
    value = independent_input()
    original = value.model_dump(mode='json')
    def unavailable(candidate):
        assert candidate.model_dump(mode='json') == original
        raise PlanningError('NO_OPERATIONAL_CANDIDATE', 'fixed failure')
    monkeypatch.setattr(m10_selection, 'compare_operations', unavailable)
    result = m10_comparison.compare_m10(value)
    assert result['status'] == 'NOT_FOUND'
    assert result['layout'] is result['measurements'] is result['elapsed_ms'] is None
    assert result['error'] == {'code': 'NO_OPERATIONAL_CANDIDATE', 'message': 'fixed failure'}
    assert result['input_sha256'] == operations_digest(value)
    assert value.model_dump(mode='json') == original


@pytest.fixture(scope='module')
def real_result():
    value = independent_input()
    with TestClient(planning_app) as client:
        response = client.post('/plan-operations', json=value.model_dump(mode='json'))
    assert response.status_code == 200, response.text
    return value, response.json()


def test_real_asgi_outside_door_fixture_retains_complete_evidence(real_result):
    value, result = real_result
    assert result['operations_input'] == value.model_dump(mode='json')
    assert result['input_sha256'] == operations_digest(value)
    evaluation = result['after']['layout']['route_evaluation']
    assert evaluation['status'] == 'PASS' and evaluation['full_operations_proven'] is True
    assert evaluation['outside_entry_proof'] == 'PASS'
    assert all(row['entry_path_mm'][0] == [-600.0, 4500.0] for row in evaluation['face_access'])
    for tasks, key in [(value.demand.picking_tasks, 'picking_routes'),
                       (value.demand.replenishment_tasks, 'replenishment_routes')]:
        routes = {row['id']: row for row in evaluation[key]}
        assert set(routes) == {task.id for task in tasks}
        assert all(set(routes[task.id]['visited_item_ids']) == set(task.item_ids) for task in tasks)


@pytest.mark.parametrize('corruption', [None, 'finite-measurements', 'available-status', 'missing-error'])
def test_delivery_m10_null_layout_cannot_publish_fake_baseline_metrics(
        real_result, monkeypatch, corruption):
    value, result = real_result
    before = not_found(value)
    if corruption == 'finite-measurements':
        before['measurements'] = {'picking_mean_mm': 0, 'module_count': 0}
    elif corruption == 'available-status':
        before['status'] = 'AVAILABLE'
    elif corruption == 'missing-error':
        before['error'] = {}
    monkeypatch.setattr(algorithm_lab, 'combined_catalog', lambda: {'scenarios': [record(value)]})
    monkeypatch.setattr(m10_comparison, 'compare_m10', lambda candidate: deepcopy(before))
    original_client = httpx.Client
    calls = []
    def transport(request):
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=deepcopy(result))
    monkeypatch.setattr(algorithm_lab.httpx, 'Client', lambda **kwargs:
        original_client(transport=httpx.MockTransport(transport), **kwargs))
    with local_client() as client:
        response = client.post('/api/algorithm/scenarios/independent-integration/generate')
    assert calls == [('/plan-operations', value.model_dump(mode='json'))]
    if corruption:
        assert response.status_code == 422, response.text
        assert response.json()['detail']['code'] == 'OPERATIONS_DELIVERY_VALIDATION_FAILED'
    else:
        assert response.status_code == 200, response.text
        data = response.json()
        assert data['before'] == before
        assert data['after'] == result['after']
        assert data['technical_reference']['result'] == result['before']


def test_new_outside_door_layout_preserves_module_and_assembly_workbook_counts(real_result):
    value, result = real_result
    layout = OperationsLayout.model_validate(result['after']['layout'])
    original = layout.model_dump(mode='json')
    business = Business(products=[{'category': '食品>零食'}, {'category': '日用>清洁'}],
                        product_count=2, templates=value.base.templates, sources=[])
    products = assign_categories(business, layout.runs)
    assigned = [identifier for row in products.category_assignments for identifier in row['shelf_ids']]
    assert len(assigned) == len(set(assigned)) == len(layout.shelves)
    assert set(assigned) == {shelf.id for shelf in layout.shelves}
    assert products.sku_assignments == []
    assert Counter(s.material_id for s in layout.shelves) == {row.material_id: row.quantity for row in layout.bom}
    workbook = load_workbook(BytesIO(make_workbook(original)), data_only=True)
    try:
        material_rows = list(workbook['设计级货架清单'].values)
        assert material_rows[-1][4] == len(layout.shelves)
        assert material_rows[-1][5] == sum(row.total_level_count for row in layout.bom)
        instances = list(workbook['货架实例'].values)[1:]
        assert {row[0] for row in instances} == {shelf.id for shelf in layout.shelves}
        assert len(instances) == len(layout.shelves)
        by_run = {run.run_id: run for run in layout.runs}
        assemblies = {row[0]: row for row in list(workbook['单双面组合'].values)[1:]}
        assert set(assemblies) == {assembly.id for assembly in layout.assemblies}
        for assembly in layout.assemblies:
            row = assemblies[assembly.id]
            assert row[3] == sum(len(by_run[identifier].modules) for identifier in assembly.run_ids)
            assert row[4] == len(assembly.bay_pairs)
            assert row[6] == assembly.structure_gap_mm and row[7] == assembly.total_depth_mm
        assert sum(row[3] for row in assemblies.values()) == len(layout.shelves)
        notes = dict(workbook['说明与来源'].values)
        assert '共用结构零件' in notes['双面物料口径']
        assert 'UNKNOWN' in notes['空间指标']
    finally:
        workbook.close()
    assert layout.model_dump(mode='json') == original
