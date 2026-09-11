"""Independent synthetic adopted-scope tests; no real Human state is written."""
from copy import deepcopy
import io
import json

import ezdxf
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from shapely.geometry import Polygon

from shared import confirmation
from shared.contracts import Business, Template
from shared.planning_v2 import PlanRequest
from shared.scope_tolerance import normalize_adopted_scope, ScopeFormatError, TOLERANCE
from services.understanding import prepare


RECTANGLE = [(0, 0), (12000, 0), (12000, 9000), (0, 9000)]
HOLE_BRIDGE = [*RECTANGLE, (0, 0), (2000, 2000), (2000, 3000),
               (3000, 3000), (3000, 2000), (2000, 2000), (0, 0)]


def normalize(points, **kwargs):
    return normalize_adopted_scope(points, layer='PLANNING_SCOPE', source_closed=False, **kwargs)


@pytest.mark.parametrize('layer', ['0', 'ROUGH_FRAME', 'DEFPOINTS', 'WALL'])
def test_unknown_or_rough_frame_cannot_enter_tolerance(layer):
    with pytest.raises(ScopeFormatError) as error:
        normalize_adopted_scope(RECTANGLE, layer=layer, source_closed=False)
    assert error.value.code == 'SCOPE_NOT_EXPLICITLY_ADOPTED'


@pytest.mark.parametrize('closed,repeated', [(False, False), (False, True), (True, False)])
def test_closure_is_logical_and_keeps_raw_points(closed, repeated):
    points = [*RECTANGLE, RECTANGLE[0]] if repeated else RECTANGLE[:]
    before = deepcopy(points)
    polygon, evidence = normalize_adopted_scope(points, layer='HUMAN_PLANNING_SCOPE', source_closed=closed)
    assert polygon.equals(Polygon(RECTANGLE))
    assert points == before
    assert evidence['source_closed_flag'] == closed
    assert evidence['raw_repeated_first_vertex'] == repeated
    assert evidence['logical_closure_is_physical_wall'] is False
    assert evidence['source_cad_modified'] is False
    assert evidence['symmetric_difference_area_mm2'] == 0


def test_duplicate_vertices_and_segments_are_audited_without_filling_hole():
    points = [HOLE_BRIDGE[0], *HOLE_BRIDGE]
    before = deepcopy(points)
    polygon, evidence = normalize(points)
    assert polygon.is_valid and len(polygon.interiors) == 1
    assert Polygon(polygon.interiors[0]).equals(Polygon([(2000, 2000), (3000, 2000), (3000, 3000), (2000, 3000)]))
    assert evidence['removed_duplicate_point_count'] >= 1
    assert evidence['duplicate_segment_count'] >= 1
    assert 'make_valid_linework_preserve_collapsed_audit' in evidence['operations']
    assert evidence['collapsed_lines_mm']
    assert points == before


@pytest.mark.parametrize('points', [
    [*RECTANGLE, (0.4, 9000.2)],
    [*RECTANGLE, (0.4, 0.2)],
])
def test_local_snap_is_bounded_and_deterministic(points):
    first, evidence = normalize(points)
    again, repeated = normalize(points)
    assert first.equals(again) and evidence == repeated
    assert first.is_valid
    assert 0 < evidence['maximum_vertex_shift_mm'] <= TOLERANCE.vertex_shift_mm
    assert evidence['boundary_hausdorff_distance_mm'] <= TOLERANCE.vertex_shift_mm
    assert evidence['symmetric_difference_area_mm2'] <= evidence['maximum_area_change_mm2']
    assert 'local_snap_within_mm_tolerance' in evidence['operations']


def test_nearby_vertex_chain_cannot_grow_the_tolerance():
    points = [*RECTANGLE, (0.6, 9000.2), (1.2, 9000.4)]
    with pytest.raises(ScopeFormatError) as error:
        normalize(points)
    assert error.value.code == 'SCOPE_REPAIR_TOO_LARGE'


def test_local_point_to_segment_snap_is_noded_and_deduplicated():
    points = [*RECTANGLE, (0, 0), (6000, 0.4), (8000, 0), (6000, 0.4), (0, 0)]
    polygon, evidence = normalize(points)
    assert polygon.equals(Polygon(RECTANGLE))
    assert evidence['maximum_vertex_shift_mm'] == pytest.approx(0.4)
    assert 'local_snap_within_mm_tolerance' in evidence['operations']
    assert 'node_intersections_and_duplicate_segments' in evidence['operations']
    segments = evidence['derived_noded_segments_mm']
    assert any([6000.0, 0.0] in line for line in segments)
    assert len({tuple(sorted(tuple(p) for p in line)) for line in segments}) == len(segments)


@pytest.mark.parametrize('points', [
    [(0, 0), (12000, 9000), (0, 9000), (12000, 0)],
    [(0, 0), (4000, 0), (4000, 4000), (0, 4000), (0, 0),
     (6000, 0), (10000, 0), (10000, 4000), (6000, 4000), (6000, 0), (0, 0)],
])
def test_multiple_substantial_regions_never_select_largest(points):
    with pytest.raises(ScopeFormatError) as error:
        normalize(points)
    assert error.value.code == 'SCOPE_MULTIPLE_OR_DEGENERATE_REGIONS'


def test_long_thin_spur_cannot_be_erased_as_small_area_noise():
    points = [(0, 0), (12000, 0), (12000, 9000), (6000, 9000),
              (6000.2, 15000), (6000.4, 9000), (0, 9000)]
    with pytest.raises(ScopeFormatError) as error:
        normalize(points)
    assert error.value.code == 'SCOPE_REPAIR_TOO_LARGE'


def test_even_sub_mm_hole_cannot_be_filled_by_vertex_snap():
    points = [*RECTANGLE, (0, 0), (2000, 2000), (2000, 2000.5),
              (2000.5, 2000.5), (2000.5, 2000), (2000, 2000), (0, 0)]
    with pytest.raises(ScopeFormatError) as error:
        normalize(points)
    assert error.value.code == 'SCOPE_HOLE_CHANGED'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, 'RUNTIME', tmp_path / 'understanding')
    monkeypatch.setattr(confirmation, 'KEY_PATH', tmp_path / 'confirmation.key')
    business = Business(products=[], product_count=0, sources=[], templates=[
        Template(material_id='SYNTHETIC-SCOPE-TEST', length_mm=1200, depth_mm=400, default_level_count=6)])
    monkeypatch.setattr(prepare, 'load_business', lambda: business)
    app = FastAPI()
    app.include_router(prepare.router)
    with TestClient(app) as value:
        yield value


def cad(points=RECTANGLE, *, layer='PLANNING_SCOPE', closed=False, kind='LWPOLYLINE', explicit_hole=False):
    doc = ezdxf.new('R2007')
    doc.units = 4
    model = doc.modelspace()
    if kind == 'LWPOLYLINE':
        scope = model.add_lwpolyline(points, close=closed, dxfattribs={'layer': layer})
    else:
        scope = model.add_polyline2d(points, close=closed, dxfattribs={'layer': layer})
    model.add_line((4500, 2000), (4500, 7000), dxfattribs={'layer': 'WALL'})
    model.add_lwpolyline([(1000, 0), (2200, 0), (2200, 800), (1000, 800)], close=True,
                        dxfattribs={'layer': 'ENTRANCE'})
    if explicit_hole:
        model.add_lwpolyline([(8000, 1000), (9000, 1000), (9000, 2000), (8000, 2000)], close=True,
                            dxfattribs={'layer': 'PLANNING_HOLE'})
    return doc, scope


def encoded(doc):
    stream = io.StringIO()
    doc.write(stream)
    return stream.getvalue().encode('utf-8')


def upload(client, doc):
    response = client.post('/prepare', files={'file': ('SYNTHETIC-M11-SCOPE.dxf', encoded(doc), 'application/dxf')})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize('kind', ['LWPOLYLINE', 'POLYLINE'])
def test_real_prepare_accepts_explicit_unflagged_ring_and_signs_evidence(client, kind):
    doc, source = cad(kind=kind, explicit_hole=True)
    raw = encoded(doc)
    response = client.post('/prepare', files={'file': ('SYNTHETIC-M11-SCOPE.dxf', raw, 'application/dxf')})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['status'] == 'READY' and result['readiness'] == 'AUTO_VALIDATED'
    req = PlanRequest.model_validate(result['plan_request'])
    confirmation.verify_request(req)
    assert len(req.space.boundary.holes_mm) == 1
    assert len(req.space.barriers) == 1
    assert req.space.barriers[0].start_mm == (4500, 2000)
    record = next(e for e in req.space.source_evidence if e['kind'] == 'ADOPTED_SCOPE_FORMAT_NORMALIZATION')
    assert record['records'][0]['source_closed_flag'] is False
    assert record['records'][0]['handle'] == source.dxf.handle
    assert 'logical_ring_closure' in record['records'][0]['operations']
    drawing = next(p for p in result['drawing']['polylines'] if p['handle'] == source.dxf.handle)
    assert drawing['closed'] is False and drawing['points_mm'][0] != drawing['points_mm'][-1]
    assert (prepare.RUNTIME / f"{result['prepared_id']}.dxf").read_bytes() == raw


def test_prepare_preserves_both_implicit_and_explicit_source_holes(client):
    doc, _ = cad([HOLE_BRIDGE[0], *HOLE_BRIDGE], explicit_hole=True)
    result = upload(client, doc)
    assert result['status'] == 'READY'
    req = PlanRequest.model_validate(result['plan_request'])
    assert len(req.space.boundary.holes_mm) == 2
    assert len(result['evidence']['explicit_holes']) == 2
    assert result['evidence']['duplicate_scope_point_edge_ids']
    saved = json.loads((prepare.RUNTIME / f"{result['prepared_id']}.json").read_text(encoding='utf-8-sig'))
    changed = req.model_copy(deep=True)
    changed.space.boundary.holes_mm.pop()
    with pytest.raises(prepare.InputError) as error:
        prepare._validate_fixed(changed, saved)
    assert error.value.code == 'CAD_HOLE_MUST_BE_PRESERVED'


def test_scope_format_audit_cannot_be_removed_or_changed(client):
    doc, _ = cad()
    result = upload(client, doc)
    saved = json.loads((prepare.RUNTIME / f"{result['prepared_id']}.json").read_text(encoding='utf-8-sig'))
    for remove in (True, False):
        req = PlanRequest.model_validate(result['plan_request'])
        if remove:
            req.space.source_evidence = [e for e in req.space.source_evidence if e['kind'] != 'ADOPTED_SCOPE_FORMAT_NORMALIZATION']
        else:
            next(e for e in req.space.source_evidence if e['kind'] == 'ADOPTED_SCOPE_FORMAT_NORMALIZATION')['records'][0]['maximum_vertex_shift_mm'] = 1000
        with pytest.raises(prepare.InputError) as error:
            prepare._validate_fixed(req, saved)
        assert error.value.code == 'SCOPE_FORMAT_EVIDENCE_CHANGED'


@pytest.mark.parametrize('case', ['rough', 'multiple', 'bowtie', 'large_spur'])
def test_prepare_does_not_adopt_rough_or_unsafe_scope(client, case):
    points = RECTANGLE
    if case == 'bowtie':
        points = [(0, 0), (12000, 9000), (0, 9000), (12000, 0)]
    if case == 'large_spur':
        points = [(0, 0), (12000, 0), (12000, 9000), (6000, 9000), (6000.2, 15000), (6000.4, 9000), (0, 9000)]
    doc, _ = cad(points, layer='ROUGH_FRAME' if case == 'rough' else 'PLANNING_SCOPE')
    if case == 'multiple':
        doc.modelspace().add_lwpolyline([(1000, 1000), (2000, 1000), (2000, 2000), (1000, 2000)],
                                      close=True, dxfattribs={'layer': 'HUMAN_PLANNING_SCOPE'})
    result = upload(client, doc)
    assert result['plan_request'] is None
    assert result['readiness'] != 'AUTO_VALIDATED'
    assert not (result.get('candidate_space') or {}).get('boundary')
    if case in ('rough', 'multiple'):
        assert result['evidence']['scope_format_normalization'] == []
    else:
        assert result['evidence']['scope_format_failures']
