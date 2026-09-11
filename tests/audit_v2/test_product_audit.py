"""Independent category and synthetic-product checks; no reference CAD inputs."""
import ast
import copy
import itertools
from collections import Counter, defaultdict
from pathlib import Path

import pytest
from pydantic import ValidationError
from shapely.geometry import Polygon, shape
from shapely.ops import unary_union

from shared.planning_v2 import ShelfLevel, SyntheticProduct
from services.planning.products import assign_categories, allocate_synthetic, validate_synthetic
from services.planning.runs import generate_layout
from tests.audit_v2.test_independent_audit import make_request


def product(pid, width=100, depth=150, height=200, **kwargs):
    return SyntheticProduct(product_id=pid,category=kwargs.pop('category','food'),
                            width_mm=width,depth_mm=depth,height_mm=height,**kwargs)


def level(sid='S1',index=0,width=900,depth=600,height=350,category=None):
    return ShelfLevel(shelf_id=sid,level_index=index,width_mm=width,
                      depth_mm=depth,clear_height_mm=height,category=category)


def check_synthetic_independently(products,levels,result):
    assert result['data_status'] == 'SYNTHETIC_ONLY_NOT_REAL_SKU'
    ps = {p.product_id:p for p in products}
    ls = {(l.shelf_id,l.level_index):l for l in levels}
    seen = [a['product_id'] for a in result['assignments']] + [a['product_id'] for a in result['unassigned']]
    assert Counter(seen) == Counter(ps.keys())
    spans = defaultdict(list)
    for a in result['assignments']:
        p = ps[a['product_id']]
        key = (a['shelf_id'],a['level_index'])
        l = ls[key]
        assert a['category'] == p.category
        assert l.category is None or l.category == p.category
        assert a['synthetic'] is True
        assert isinstance(a['rotated_xy'],bool)
        assert not a['rotated_xy'] or p.rotation_policy == 'ALLOW_XY_SWAP'
        width,depth = (p.depth_mm,p.width_mm) if a['rotated_xy'] else (p.width_mm,p.depth_mm)
        assert a['facings'] >= p.minimum_facings
        assert a['unit_width_mm'] == width
        assert a['unit_depth_mm'] == depth
        assert a['height_mm'] == p.height_mm
        assert a['x_end_mm']-a['x_start_mm'] == pytest.approx(width*a['facings'])
        assert 0 <= a['x_start_mm'] <= a['x_end_mm'] <= l.width_mm+1e-6
        assert 0 <= a['y_start_mm'] <= a['y_start_mm']+depth <= l.depth_mm+1e-6
        assert p.height_mm <= l.clear_height_mm+1e-6
        spans[key].append((a['x_start_mm'],a['x_end_mm']))
    for intervals in spans.values():
        for a,b in itertools.combinations(intervals,2):
            assert min(a[1],b[1])-max(a[0],b[0]) <= 1e-6
    assert len(result['level_usage']) == len(ls)
    usage_keys = [(row['shelf_id'],row['level_index']) for row in result['level_usage']]
    assert Counter(usage_keys) == Counter(ls.keys())
    for row in result['level_usage']:
        key = (row['shelf_id'],row['level_index'])
        actual = sum(end-start for start,end in spans[key])
        assert row['used_width_mm'] == pytest.approx(actual)
        assert row['remaining_width_mm'] == pytest.approx(ls[key].width_mm-actual)
    assert all(isinstance(row.get('reason'),str) and row['reason'] for row in result['unassigned'])


def test_mixed_explicit_dimensions_capacities_and_input_permutation():
    products = [product('a',113.25,153.5,200,minimum_facings=3,demand_weight=10),
                product('b',500,200,250,rotation_policy='ALLOW_XY_SWAP'),
                product('c',200,150,400),product('d',120,250,200,category='drink'),
                product('e',170,300,220,minimum_facings=2)]
    levels = [level(width=1000),level('S1',1,width=900,category='food'),
              level('S2',0,width=600,category='drink')]
    result = allocate_synthetic(products,levels)
    check_synthetic_independently(products,levels,result)
    assert result == allocate_synthetic(list(reversed(products)),list(reversed(levels)))


@pytest.mark.parametrize('case,expected', [
    ('no_levels','NO_LEVELS'),('category','CATEGORY_MISMATCH'),('height','TOO_HIGH'),
    ('depth','TOO_DEEP'),('width','TOO_WIDE_FOR_MINIMUM_FACINGS'),
    ('facings','TOO_WIDE_FOR_MINIMUM_FACINGS'),('occupied','CAPACITY_EXHAUSTED'),
])
def test_unassigned_reasons_match_independent_single_constraint_cases(case,expected):
    p = product('target')
    levels = [level()]
    products = [p]
    if case == 'no_levels': levels = []
    elif case == 'category': levels[0].category = 'other'
    elif case == 'height': p.height_mm = 351
    elif case == 'depth': p.depth_mm = 601
    elif case == 'width': p.width_mm = 901
    elif case == 'facings': p.minimum_facings = 10
    else: products.insert(0,product('occupier',900,100,100,demand_weight=100))
    result = allocate_synthetic(products,levels)
    check_synthetic_independently(products,levels,result)
    assert next(row for row in result['unassigned'] if row['product_id']=='target')['reason'] == expected


@pytest.mark.parametrize('policy,assigned', [('FIXED',False),('ALLOW_XY_SWAP',True)])
def test_rotation_permission_changes_physical_feasibility(policy,assigned):
    products = [product('rotate',500,200,250,rotation_policy=policy)]
    levels = [level(width=400,depth=550,height=300)]
    result = allocate_synthetic(products,levels)
    check_synthetic_independently(products,levels,result)
    assert bool(result['assignments']) is assigned
    if assigned: assert result['assignments'][0]['rotated_xy'] is True


@pytest.mark.parametrize('mutation', ['overlap','duplicate','missing','false_reason','height','fake_usage'])
def test_synthetic_validator_rejects_corrupt_deliverable(mutation):
    products = [product('a'),product('b')]
    levels = [level()]
    result = copy.deepcopy(allocate_synthetic(products,levels))
    if mutation == 'overlap':
        result['assignments'][1]['x_start_mm'] = result['assignments'][0]['x_start_mm']
        result['assignments'][1]['x_end_mm'] = result['assignments'][0]['x_end_mm']
    elif mutation == 'duplicate': result['assignments'].append(copy.deepcopy(result['assignments'][0]))
    elif mutation == 'missing': result['assignments'].pop()
    elif mutation == 'false_reason':
        dropped = result['assignments'].pop()
        result['unassigned'].append({'product_id':dropped['product_id'],'reason':'TOO_HIGH'})
    elif mutation == 'height': result['assignments'][0]['height_mm'] = 1
    else:
        result['level_usage'][0]['used_width_mm'] = 0
        result['level_usage'][0]['remaining_width_mm'] = 900
    assert validate_synthetic(products,levels,result)['status'] == 'FAIL'


@pytest.mark.parametrize('bad', ['duplicate_product','duplicate_level','negative_width','negative_level','empty_id'])
def test_synthetic_inputs_fail_explicitly(bad):
    products = [product('a')]
    levels = [level()]
    if bad == 'duplicate_product': products.append(product('a'))
    elif bad == 'duplicate_level': levels.append(level())
    elif bad == 'negative_width': products[0].width_mm = -10
    elif bad == 'negative_level': levels[0].clear_height_mm = -10
    else: products[0].product_id = ''
    with pytest.raises((ValueError,ValidationError)):
        allocate_synthetic(products,levels)


def assert_category_evidence(business,runs,result):
    shelves = {s.id:(r.run_id,s) for r in runs for s in r.modules}
    used = []
    for assignment in result.category_assignments:
        ids = assignment['shelf_ids']
        used.extend(ids)
        assert len(set(ids)) == len(ids)
        expected_zone = unary_union([Polygon(shelves[sid][1].footprint_mm) for sid in ids])
        assert shape(assignment['zone_geometry_mm']).equals(expected_zone)
        mapped = [sid for row in assignment['run_assignments'] for sid in row['shelf_ids']]
        assert Counter(mapped) == Counter(ids)
        for row in assignment['run_assignments']:
            assert all(shelves[sid][0] == row['run_id'] for sid in row['shelf_ids'])
    assert len(used) == len(set(used))
    counts = Counter()
    missing = 0
    for product_record in business.products:
        value = product_record.get('category')
        if isinstance(value,str) and value.split('>')[0].strip(): counts[value.split('>')[0].strip()] += 1
        else: missing += 1
    assigned_counts = Counter({a['category']:a['record_count'] for a in result.category_assignments})
    unassigned_counts = Counter({a['category']:a['record_count'] for a in result.unassigned_categories if a['category'] is not None})
    assert assigned_counts+unassigned_counts == counts
    assert sum(a['record_count'] for a in result.unassigned_categories if a['category'] is None) == missing
    assert len(result.unassigned_products) == len(business.products)
    assert sorted(p['source_record_index'] for p in result.unassigned_products) == list(range(len(business.products)))
    assert all(p['reason']=='UNAVAILABLE-DUE-TO-MISSING-PHYSICAL-DATA' for p in result.unassigned_products)
    assert result.real_sku_status == 'UNAVAILABLE-DUE-TO-MISSING-PHYSICAL-DATA'
    assert result.sku_assignments == []


def test_category_zones_are_exact_rack_unions_not_store_boxes_and_no_name_inference():
    req = make_request()
    runs = generate_layout(req).runs
    req.business.products = [dict(category='food > dry',name='100x200x300mm'),
                             dict(category='food > snacks',specification='500x600x700'),
                             dict(category='drink',name='2 litre'),
                             dict(name='water beverage 100mm',width_mm=100,depth_mm=100,height_mm=100),
                             dict(category=None),dict(category=42),dict(category='  ')]
    req.business.product_count = len(req.business.products)
    result = assign_categories(req.business,runs)
    assert_category_evidence(req.business,runs,result)
    assert {a['category'] for a in result.category_assignments} == {'food','drink'}
    assert any(shape(a['zone_geometry_mm']).area < shape(a['zone_geometry_mm']).envelope.area for a in result.category_assignments)
    assert result == assign_categories(req.business,list(reversed(runs)))


def test_authorized_real_product_catalog_preserves_records_and_unknown_physical_data():
    from services.understanding.app import load_business
    business = load_business()
    assert business.product_count == len(business.products) == 7839
    req = make_request()
    result = assign_categories(business,generate_layout(req).runs)
    assert_category_evidence(business,generate_layout(req).runs,result)


def test_products_module_has_no_io_reference_dependency_or_name_dimension_parsing():
    path = Path(__file__).resolve().parents[2]/'services/planning/products.py'
    tree = ast.parse(path.read_text(encoding='utf-8-sig'))
    for node in ast.walk(tree):
        if isinstance(node,ast.Import): imports = [alias.name for alias in node.names]
        elif isinstance(node,ast.ImportFrom): imports = [node.module or '']
        else: imports = []
        assert not any(name.split('.')[0] in {'os','pathlib','io','openpyxl','ezdxf','sqlite3','requests','httpx'} or 'reference' in name for name in imports)
        if isinstance(node,ast.Call):
            if isinstance(node.func,ast.Name): assert node.func.id not in {'open','eval','exec','__import__'}
            if isinstance(node.func,ast.Attribute): assert node.func.attr not in {'read_text','read_bytes','write_text','write_bytes','open','load','loads'}
        if isinstance(node,ast.Constant) and isinstance(node.value,str):
            assert 'input/reference/' not in node.value.replace('\\','/').lower()
