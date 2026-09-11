"""Browser presentation checks; synthetic component responses, not planner proof."""
from copy import deepcopy
import importlib.util
from pathlib import Path

import pytest
from playwright.sync_api import expect

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('m10_ui_components',ROOT/'tests/planning_m10/test_ui.py')
old=importlib.util.module_from_spec(spec)
spec.loader.exec_module(old)
old.OUT=ROOT/'outputs/planning_m11/ui-components'


@pytest.fixture(scope='module')
def browser():
    yield from old.browser.__wrapped__()


@pytest.fixture
def ui(browser):
    yield from old.ui.__wrapped__(browser)


def test_old_component_evidence_and_missing_route_remain_honest(ui):
    old.test_operations_render_actual_geometry_both_faces_and_no_fabricated_baseline_route(ui)


def test_outside_path_and_logical_threshold_are_distinct_from_physical_wall(ui):
    result=ui['result'];value=result['operations_input']
    value['workpoints']['entrance']['point_mm']=[-600,1200]
    value['physical_perimeter_walls']=[dict(id='LOW',start_mm=[0,0],end_mm=[0,600]),
        dict(id='HIGH',start_mm=[0,1800],end_mm=[0,9000])]
    value['door_connection']=dict(id='DOOR',inside_point_mm=[600,1200],outside_point_mm=[-600,1200],
        connection_region=dict(boundary_mm=[[-600,600],[600,600],[600,1800],[-600,1800],[-600,600]],holes_mm=[]),provenance='SYNTHETIC_TEST')
    result['comparison_baseline']='M10_CORRECTED_INPUT'
    for phase in ('before','after'):
        layout=result[phase]['layout']
        layout['workpoints']=deepcopy(value['workpoints'])
        layout['space']['barriers']=deepcopy(value['physical_perimeter_walls'])
        evaluation=layout['route_evaluation'];evaluation['full_operations_proven']=True
        evaluation['outside_entry_proof']='PASS'
        evaluation['face_access'][0]['entrance']=dict(reachable=True,distance_mm=2500,
            path_mm=[[-600,1200],[0,1200],[600,1200],[600,2400],[550,2400],[550,4050]])
    page=old.generate(ui)
    expect(page.locator('#before-title')).to_contain_text('M10')
    for phase in ('before','after'):
        svg=page.locator(f'#{phase}-canvas')
        expect(svg.get_by_test_id('lab-wall')).to_have_count(2)
        expect(svg.get_by_test_id('operations-entrance-opening')).to_have_attribute('data-is-wall','false')
        expect(svg.get_by_test_id('operations-door-jamb')).to_have_count(2)
        path=svg.get_by_test_id('operations-outside-route')
        expect(path).to_have_count(1)
        assert path.get_attribute('points').startswith('-600,-1200 0,-1200 600,-1200')
        width=float(svg.get_by_test_id('operations-entrance-opening').evaluate('(e)=>getComputedStyle(e).strokeWidth').removesuffix('px'))
        assert width<=1
    expect(page.locator('#operations-entry-evidence')).to_contain_text('店外入口')
    page.locator('.lab-comparison').screenshot(path=str(old.OUT/'outside-door-components.png'))


def test_m10_no_legal_candidate_is_na_while_m11_geometry_stays_reviewable(ui):
    ui['result']['comparison_baseline']='M10_CORRECTED_INPUT'
    ui['result']['before']=dict(status='NOT_FOUND',layout=None,measurements=None,elapsed_ms=None,
        error=dict(code='NO_OPERATIONAL_CANDIDATE',message='M10 有限候选没有满足同一输入门槛。'))
    page=old.generate(ui)
    expect(page.locator('#before-unavailable')).to_be_visible()
    expect(page.locator('#before-canvas').get_by_test_id('lab-module')).to_have_count(0)
    expect(page.locator('#after-canvas').get_by_test_id('lab-module')).to_have_count(6)
    expect(page.locator('#operations-entry-evidence')).to_contain_text('NA')
    expect(page.locator('#before-route-evidence')).to_contain_text('NA')


def test_task_switch_uses_exact_returned_path_and_both_face_sides(ui):
    old.test_same_task_switch_and_module_access_evidence_uses_server_path(ui)


def test_incomplete_fixed_task_cannot_gain_fake_path(ui):
    old.test_incomplete_route_never_becomes_a_complete_task_even_with_finite_path(ui)
