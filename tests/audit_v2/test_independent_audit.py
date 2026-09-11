"""Independent adversarial checks; no production or reference data is loaded."""
import ast
import hashlib
import itertools
from fractions import Fraction
from pathlib import Path

import pytest
from shapely.geometry import Polygon

from shared.contracts import Business, PolygonData, Source, Template
from shared.planning_v2 import Confirmation, PlanRequest, PlanningRules, PlanningSpace, space_geometry_digest
from services.planning.engine import PlanningError
from services.planning.runs import combine_modules, generate_layout, validate_runs


def make_request(width=18000, height=12000, origin=(0, 0)):
    x, y = origin
    source = Source(filename='independent-synthetic.dxf', sha256=hashlib.sha256(b'independent-synthetic').hexdigest())
    space = PlanningSpace(boundary=PolygonData(boundary_mm=[
        (x, y), (x + width, y), (x + width, y + height), (x, y + height), (x, y)]),
        confirmation=Confirmation(state='SYNTHETIC_TEST', source_sha256=source.sha256,
                                  confirmed_by='independent-test-fixture'))
    space.confirmation.geometry_sha256 = space_geometry_digest(space)
    templates = [Template(material_id=f'audit-{length}-{depth}', length_mm=length,
                          depth_mm=depth, default_level_count=4)
                 for length in (1200, 1500, 1800) for depth in (400, 600)]
    return PlanRequest(source=source, space=space,
                       business=Business(products=[], product_count=0, templates=templates, sources=[]),
                       rules=PlanningRules())


@pytest.mark.parametrize('width,height,origin', [
    (4800, 4800, (0, 0)),
    (5100.25, 7210.75, (-1725.125, 3444.875)),
    (6355, 28300, (100003, -400011)),
    (33700, 6599, (-300077, -712003)),
    (12713.75, 17321.25, (1000000000.125, -1000000000.25)),
    (27000, 27000, (19, -23)),
])
def test_unseen_rectangles_have_safe_physical_instances(width, height, origin):
    request = make_request(width, height, origin)
    result = generate_layout(request)
    assert result.model_dump(mode='json') == generate_layout(request).model_dump(mode='json')
    scope = Polygon(request.space.boundary.boundary_mm)
    rack_polygons = [Polygon(s.footprint_mm) for s in result.shelves]
    for rack in rack_polygons:
        assert scope.covers(rack)
        assert rack.distance(scope.boundary) >= request.rules.boundary_clearance_mm - 1e-6
    for a, b in itertools.combinations(rack_polygons, 2):
        assert a.intersection(b).area < 1e-6
    for run in result.runs:
        for a, b in zip(run.modules, run.modules[1:]):
            interface = Polygon(a.footprint_mm).intersection(Polygon(b.footprint_mm))
            assert interface.length == pytest.approx(run.depth_mm)
        assert sum(s.length_mm for s in run.modules) == pytest.approx(run.used_length_mm)
    for a, b in itertools.combinations(result.runs, 2):
        assert Polygon(a.footprint_mm).distance(Polygon(b.footprint_mm)) >= request.rules.aisle_width_mm - 1e-6
    assert result.metrics['effective_length_mm'] == pytest.approx(sum(s.length_mm for s in result.shelves))
    assert result.metrics['footprint_density'] == pytest.approx(sum(p.area for p in rack_polygons) / scope.area)
    assert sum(row.quantity for row in result.bom) == len(rack_polygons)


@pytest.mark.parametrize('lengths,available,minimum', [
    ((2.5, 3.75, 5), 13.74, 2),
    ((2.5, 3.75, 5), 13.75, 3),
    ((2.5, 3.75, 5), 12.5, 4),
    ((7, 11, 13), 53, 2),
    ((7, 11, 13), 53, 5),
    ((7, 11, 13), 13.99, 2),
    ((3, 3, 9), 18, 3),
    ((1.125, 2.375, 3.625), 8.99, 2),
])
def test_module_dp_against_rational_exhaustive_oracle(lengths, available, minimum):
    templates = [Template(material_id=f't{i}', length_mm=length, depth_mm=400,
                          default_level_count=4) for i, length in enumerate(lengths)]
    capacity = Fraction(str(available))
    exact_lengths = [Fraction(str(length)) for length in lengths]
    feasible = []
    for counts in itertools.product(*(range(int(capacity / length) + 1) for length in exact_lengths)):
        total = sum(length * count for length, count in zip(exact_lengths, counts))
        ids = tuple(t.material_id for t, count in zip(templates, counts) for _ in range(count))
        if len(ids) >= minimum and total <= capacity:
            feasible.append((capacity - total, len(ids), ids))
    actual = combine_modules(available, list(reversed(templates)), min_modules=minimum)
    if not feasible:
        assert actual['material_ids'] == []
    else:
        oracle = min(feasible)
        assert actual['remaining_length_mm'] == pytest.approx(float(oracle[0]))
        assert (actual['module_count'], tuple(actual['material_ids'])) == oracle[1:]


@pytest.mark.parametrize('field,value', [('effective_length_mm', 10**12), ('footprint_density', 0.99)])
def test_validator_rejects_forged_quality_metrics(field, value):
    request = make_request()
    result = generate_layout(request).model_dump(mode='json')
    result['metrics'][field] = value
    with pytest.raises(PlanningError):
        validate_runs(request, result)


def test_validator_rejects_nonexistent_neighbor_and_fake_clearance():
    request = make_request()
    result = generate_layout(request).model_dump(mode='json')
    result['runs'][0]['neighbors'] = [dict(run_id='NONEXISTENT', relation='PARALLEL_ADJACENT', clearance_mm=999999)]
    with pytest.raises(PlanningError):
        validate_runs(request, result)


@pytest.mark.parametrize('mutation', ['module_template', 'wall_rules', 'source', 'material_quantity'])
def test_validator_rejects_structural_tampering(mutation):
    request = make_request()
    result = generate_layout(request).model_dump(mode='json')
    if mutation == 'module_template':
        result['runs'][0]['modules'][0]['material_id'] = 'NOT-IN-CATALOG'
        result['shelves'][0]['material_id'] = 'NOT-IN-CATALOG'
    elif mutation == 'wall_rules':
        result['rules']['wall_clearance_mm'] = 0
    elif mutation == 'source':
        result['source']['sha256'] = '0' * 64
    else:
        result['bom'][0]['quantity'] += 1
    with pytest.raises(PlanningError):
        validate_runs(request, result)


def test_run_module_has_no_direct_io_or_reference_dependency():
    """Static audit of this module only; does not assert imported modules are I/O-free."""
    path = Path(__file__).resolve().parents[2] / 'services/planning/runs.py'
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    forbidden_modules = {'pathlib', 'os', 'io', 'sqlite3', 'requests', 'httpx', 'urllib', 'ezdxf'}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            imports = [node.module or '']
        else:
            imports = []
        for name in imports:
            assert name.split('.')[0] not in forbidden_modules
            assert 'reference' not in name.lower()
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                assert node.func.id not in {'open', 'eval', 'exec', '__import__'}
            if isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {'read_text', 'read_bytes', 'write_text', 'write_bytes', 'open', 'load', 'loads'}
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            value = node.value.replace('\\', '/').lower()
            assert 'input/reference/' not in value
            assert 'outputs/reference_profile/' not in value
