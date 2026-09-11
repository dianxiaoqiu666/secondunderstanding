"""M02 acceptance probes: independent physical predicates, synthetic inputs only."""
import itertools

import pytest
from pydantic import ValidationError
from shapely.geometry import LineString, Polygon, box

from shared.contracts import Exclusion, PolygonData, Wall
from shared.planning_v2 import PlanRequest, space_geometry_digest
from services.planning.engine import PlanningError
from services.planning.runs import generate_layout, validate_runs
from tests.audit_v2.test_independent_audit import make_request


TOL = 1e-5


def obstacle_request(kind, origin=(0, 0)):
    req = make_request(22000, 16000, origin)
    x, y = origin

    def ring(left, bottom, right, top):
        return [(x+left,y+bottom), (x+right,y+bottom), (x+right,y+top),
                (x+left,y+top), (x+left,y+bottom)]

    if kind in {'column', 'combined'}:
        req.space.exclusions = [Exclusion(id='column', boundary_mm=ring(6500,5000,7800,6600))]
    if kind == 'wall':
        req.space.barriers = [Wall(id='partition', start_mm=(x+9500,y+2500), end_mm=(x+9500,y+13500))]
    if kind == 'diagonal_wall':
        req.space.barriers = [Wall(id='slanted-partition', start_mm=(x+8000,y+4500), end_mm=(x+12000,y+11000))]
    if kind in {'hole', 'combined'}:
        req.space.boundary.holes_mm = [ring(10000,7500,11900,9900)]
    if kind in {'entrance', 'combined'}:
        req.space.entrances = [Exclusion(id='entry-clearance', boundary_mm=ring(0,7500,2800,10500))]
    if kind == 'concave':
        req.space.boundary = PolygonData(boundary_mm=[
            (x,y), (x+22000,y), (x+22000,y+6000), (x+13000,y+6000),
            (x+13000,y+16000), (x,y+16000), (x,y)])
    req.space.confirmation.geometry_sha256 = space_geometry_digest(req.space)
    return req


def end_corridors(run, length):
    x0,y0,x1,y1 = Polygon(run.footprint_mm).bounds
    if run.direction_deg == 0:
        return [box(x0-length,y0,x0,y1), box(x1,y0,x1+length,y1)]
    return [box(x0,y0-length,x1,y0), box(x0,y1,x1,y1+length)]


def check_physical_safety(req, result):
    scope = Polygon(req.space.boundary.boundary_mm, req.space.boundary.holes_mm)
    walls = [LineString([w.start_mm,w.end_mm]) for w in req.space.barriers]
    zones = [Polygon(z.boundary_mm,z.holes_mm) for z in [*req.space.exclusions,*req.space.entrances]]
    solid_zones = [Polygon(z.boundary_mm,z.holes_mm) for z in req.space.exclusions]
    rack_polygons = [Polygon(s.footprint_mm) for s in result.shelves]
    assert len(result.runs) >= 1
    assert all(len(run.modules) >= req.rules.min_modules_per_run for run in result.runs)
    for polygon in rack_polygons:
        assert scope.covers(polygon)
        assert polygon.distance(scope.boundary) + TOL >= req.rules.boundary_clearance_mm
        assert all(not polygon.intersects(zone) for zone in zones)
        assert all(not polygon.intersects(wall) for wall in walls)
        assert all(polygon.distance(wall) + TOL >= req.rules.wall_clearance_mm for wall in walls)
    for a,b in itertools.combinations(rack_polygons,2):
        assert a.intersection(b).area <= TOL
    for a,b in itertools.combinations(result.runs,2):
        assert Polygon(a.footprint_mm).distance(Polygon(b.footprint_mm)) + TOL >= req.rules.aisle_width_mm
    for run in result.runs:
        for a,b in zip(run.modules,run.modules[1:]):
            interface = Polygon(a.footprint_mm).intersection(Polygon(b.footprint_mm))
            assert interface.length == pytest.approx(run.depth_mm)
        for corridor in end_corridors(run,req.rules.end_aisle_mm):
            # A wall may terminate the far end of a corridor, but cannot cross
            # its interior. Entrance clear zones can be used for circulation.
            interior = corridor.buffer(-TOL,join_style=2)
            assert scope.covers(interior), f'{run.run_id}: end corridor outside scope or inside hole'
            assert all(not interior.intersects(wall) for wall in walls), f'{run.run_id}: wall blocks end corridor'
            assert all(interior.intersection(zone).area <= TOL for zone in solid_zones), f'{run.run_id}: obstacle blocks end corridor'
            assert all(interior.intersection(Polygon(other.footprint_mm)).area <= TOL
                       for other in result.runs if other.run_id != run.run_id)
    assert sum(row.quantity for row in result.bom) == len(result.shelves)


@pytest.mark.parametrize('kind', ['column','wall','diagonal_wall','hole','entrance','concave','combined'])
def test_independent_obstacle_shapes_are_safe_and_deterministic(kind):
    req = obstacle_request(kind)
    result = generate_layout(req)
    check_physical_safety(req,result)
    assert result.model_dump(mode='json') == generate_layout(req).model_dump(mode='json')
    assert validate_runs(req,result)['status'] == 'PASS'


@pytest.mark.parametrize('kind', ['diagonal_wall','hole','combined'])
def test_obstacles_at_large_translated_coordinates(kind):
    req = obstacle_request(kind,(100000123.5,-99999222.25))
    result = generate_layout(req)
    check_physical_safety(req,result)
    base = generate_layout(obstacle_request(kind))
    assert result.bom == base.bom
    assert len(result.runs) == len(base.runs)
    for a,b in zip(base.shelves,result.shelves):
        assert a.material_id == b.material_id
        assert b.x_mm-a.x_mm == pytest.approx(100000123.5,abs=1e-5)
        assert b.y_mm-a.y_mm == pytest.approx(-99999222.25,abs=1e-5)


def test_wall_end_corridor_with_distinct_boundary_and_wall_clearance():
    req = obstacle_request('wall')
    req.rules.boundary_clearance_mm = 300
    req.rules.wall_clearance_mm = 50
    req.rules.orientation_candidates = [0]
    check_physical_safety(req,generate_layout(req))


def test_middle_short_fragment_is_discarded_without_losing_valid_sides():
    req = make_request(22000,10000)
    req.rules.orientation_candidates = [0]
    req.space.exclusions = [
        Exclusion(id='left-divider',boundary_mm=[(8000,0),(8500,0),(8500,10000),(8000,10000),(8000,0)]),
        Exclusion(id='right-divider',boundary_mm=[(11000,0),(11500,0),(11500,10000),(11000,10000),(11000,0)]),
    ]
    req.space.confirmation.geometry_sha256 = space_geometry_digest(req.space)
    result = generate_layout(req)
    check_physical_safety(req,result)
    assert any(s.x_mm < 8000 for s in result.shelves)
    assert any(s.x_mm > 11500 for s in result.shelves)
    assert not any(8500 < s.x_mm < 11000 for s in result.shelves)


@pytest.mark.parametrize('field', ['aisle_width_mm','boundary_clearance_mm','wall_clearance_mm','end_aisle_mm'])
@pytest.mark.parametrize('as_dict', [True,False])
def test_negative_rules_rejected_at_algorithm_boundary(field,as_dict):
    req = obstacle_request('column')
    if as_dict:
        req = req.model_dump(mode='json')
        req['rules'][field] = -10
    else:
        setattr(req.rules,field,-10)
    with pytest.raises((PlanningError,ValidationError)):
        generate_layout(req)


@pytest.mark.parametrize('corruption', ['empty_boundary','empty_hole','self_crossing_hole','hole_outside',
                                      'empty_zone_hole','self_crossing_zone','zero_wall'])
def test_invalid_obstacle_geometry_is_rejected_explicitly(corruption):
    req = obstacle_request('column')
    if corruption == 'empty_boundary':
        req.space.boundary.boundary_mm = []
    elif corruption == 'empty_hole':
        req.space.boundary.holes_mm = [[]]
    elif corruption == 'self_crossing_hole':
        req.space.boundary.holes_mm = [[(3000,3000),(6000,6000),(3000,6000),(6000,3000),(3000,3000)]]
    elif corruption == 'hole_outside':
        req.space.boundary.holes_mm = [[(23000,3000),(26000,3000),(26000,6000),(23000,6000),(23000,3000)]]
    elif corruption == 'empty_zone_hole':
        req.space.exclusions[0].holes_mm = [[]]
    elif corruption == 'self_crossing_zone':
        req.space.exclusions[0].boundary_mm = [(3000,3000),(6000,6000),(3000,6000),(6000,3000),(3000,3000)]
    else:
        req.space.barriers = [Wall(id='degenerate-wall',start_mm=(9000,9000),end_mm=(9000,9000))]
    req.space.confirmation.geometry_sha256 = space_geometry_digest(req.space)
    with pytest.raises((PlanningError,ValidationError)):
        generate_layout(req)
