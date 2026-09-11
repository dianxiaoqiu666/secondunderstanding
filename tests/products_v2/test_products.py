import copy
import math
import unittest
from unittest.mock import patch

from pydantic import ValidationError
from shapely.geometry import Polygon, shape
from shapely.ops import unary_union

from shared.contracts import Business, Shelf
from shared.planning_v2 import ShelfRun, ShelfLevel, SyntheticProduct
from services.planning.products import assign_categories, allocate_synthetic, validate_synthetic


def business(categories):
    return Business(products=[dict(category=c,source_row=i+2,barcode=f'b{i}') for i,c in enumerate(categories)],
                    product_count=len(categories),templates=[dict(material_id='m',length_mm=1200,depth_mm=400,default_level_count=5)],sources=[])


def runs(rows=2,columns=3):
    result=[]
    for row in range(rows):
        modules=[];y=row*2000
        for i in range(columns):
            x=i*1200
            modules.append(Shelf(id=f's{row}-{i}',material_id='m',length_mm=1200,depth_mm=400,default_level_count=5,
                                 x_mm=x+600,y_mm=y+200,rotation_deg=0,footprint_mm=[(x,y),(x+1200,y),(x+1200,y+400),(x,y+400)]))
        result.append(ShelfRun(run_id=f'r{row}',direction_deg=0,start_mm=(0,y+200),end_mm=(columns*1200,y+200),
                               available_length_mm=columns*1200,used_length_mm=columns*1200,remaining_length_mm=0,depth_mm=400,
                               modules=modules,footprint_mm=[(0,y),(columns*1200,y),(columns*1200,y+400),(0,y+400)]))
    return result


def product(pid='p',**kwargs):
    return SyntheticProduct(**(dict(product_id=pid,category='A',width_mm=100,depth_mm=150,height_mm=200)|kwargs))


def level(sid='s',**kwargs):
    return ShelfLevel(**(dict(shelf_id=sid,level_index=0,width_mm=500,depth_mm=400,clear_height_mm=300)|kwargs))


class CategoryTests(unittest.TestCase):
    def test_full_paths_and_top_level_preserved(self):
        b=business(['食品>饼干','食品>糖果',' 清洁 >纸品','食品>饼干'])
        p=assign_categories(b,runs())
        self.assertEqual({a['category'] for a in p.category_assignments},{'食品','清洁'})
        paths=[x['source_category_path'] for a in p.category_assignments for x in a['source_paths']]
        self.assertIn(' 清洁 >纸品',paths)
        self.assertEqual(sum(a['initial_shelf_quota'] for a in p.category_assignments),6)
        self.assertEqual(p.sku_assignments,[])
        self.assertEqual(len(p.unassigned_products),4)
        self.assertEqual(p.real_sku_status,'UNAVAILABLE-DUE-TO-MISSING-PHYSICAL-DATA')

    def test_zone_is_exact_shelf_union_never_enclosing_aisle(self):
        rs=runs();p=assign_categories(business(['A','A','A']),rs)
        zone=shape(p.category_assignments[0]['zone_geometry_mm'])
        expected=unary_union([Polygon(s.footprint_mm) for r in rs for s in r.modules])
        self.assertTrue(zone.equals(expected))
        self.assertEqual(zone.geom_type,'MultiPolygon')
        self.assertLess(zone.area,zone.envelope.area)

    def test_snake_order_and_shelf_once(self):
        p=assign_categories(business(['A']),runs())
        self.assertEqual(p.category_assignments[0]['shelf_ids'],['s0-0','s0-1','s0-2','s1-2','s1-1','s1-0'])
        self.assertEqual(p,assign_categories(business(['A']),list(reversed(runs()))))

    def test_more_categories_than_shelves_and_missing_category(self):
        p=assign_categories(business(['A','A','B','C',None,'','>invalid']),runs(1,2))
        self.assertEqual({a['category'] for a in p.category_assignments},{'A','B'})
        self.assertEqual({a['reason'] for a in p.unassigned_categories},{'INSUFFICIENT_SHELF_SLOTS','MISSING_OR_INVALID_SOURCE_CATEGORY'})
        self.assertEqual(sum(a['record_count'] for a in p.unassigned_categories),4)

    def test_no_slots_and_duplicate_ids(self):
        p=assign_categories(business(['A']),[])
        self.assertEqual(p.category_assignments,[])
        self.assertEqual(p.unassigned_categories[0]['reason'],'INSUFFICIENT_SHELF_SLOTS')
        rs=runs();rs[1].modules[0].id=rs[0].modules[0].id
        with self.assertRaisesRegex(ValueError,'DUPLICATE_SHELF_ID'):assign_categories(business(['A']),rs)

    def test_category_function_has_no_io_or_input_mutation(self):
        b=business(['A','B']);rs=runs();before=(b.model_dump(),[r.model_dump() for r in rs])
        with patch('builtins.open',side_effect=AssertionError('I/O')):assign_categories(b,rs)
        self.assertEqual(before,(b.model_dump(),[r.model_dump() for r in rs]))


class SyntheticTests(unittest.TestCase):
    def test_minimum_facings_and_demand_priority(self):
        ps=[product('low',demand_weight=1,minimum_facings=3),product('high',demand_weight=2,minimum_facings=3)]
        r=allocate_synthetic(ps,[level()])
        self.assertEqual([a['product_id'] for a in r['assignments']],['high'])
        self.assertEqual(r['assignments'][0]['facings'],3)
        self.assertEqual(r['unassigned'],[{'product_id':'low','reason':'CAPACITY_EXHAUSTED'}])
        self.assertEqual(r['validation']['status'],'PASS')

    def test_explicit_reason_matrix(self):
        cases=[(product(height_mm=301),[level()],'TOO_HIGH'),
               (product(depth_mm=401),[level()],'TOO_DEEP'),
               (product(width_mm=501),[level()],'TOO_WIDE_FOR_MINIMUM_FACINGS'),
               (product(),[level(category='B')],'CATEGORY_MISMATCH'),
               (product(),[],'NO_LEVELS')]
        for p,ls,reason in cases:
            with self.subTest(reason=reason):
                r=allocate_synthetic([p],ls)
                self.assertEqual(r['unassigned'][0]['reason'],reason)

    def test_xy_rotation_only_when_authorized(self):
        fixed=product(width_mm=350,depth_mm=100)
        rot=product(width_mm=350,depth_mm=100,rotation_policy='ALLOW_XY_SWAP')
        l=level(width_mm=200,depth_mm=400)
        self.assertEqual(allocate_synthetic([fixed],[l])['unassigned'][0]['reason'],'TOO_WIDE_FOR_MINIMUM_FACINGS')
        a=allocate_synthetic([rot],[l])['assignments'][0]
        self.assertTrue(a['rotated_xy']);self.assertEqual(a['unit_width_mm'],100);self.assertEqual(a['unit_depth_mm'],350)

    def test_level_height_never_inferred(self):
        ps=[product(height_mm=350)]
        self.assertEqual(allocate_synthetic(ps,[level(clear_height_mm=300)])['assignments'],[])
        self.assertEqual(len(allocate_synthetic(ps,[level(clear_height_mm=400)])['assignments']),1)

    def test_duplicate_input_rejection(self):
        with self.assertRaisesRegex(ValueError,'DUPLICATE_PRODUCT_ID'):allocate_synthetic([product(),product()],[level()])
        with self.assertRaisesRegex(ValueError,'DUPLICATE_SHELF_LEVEL'):allocate_synthetic([product()],[level(),level()])

    def test_zero_negative_nonfinite_rejected(self):
        for field in ['width_mm','depth_mm','height_mm']:
            for value in [0,-1,float('nan'),float('inf')]:
                with self.subTest(field=field,value=value):
                    with self.assertRaises(ValidationError):product(**{field:value})
        for value in [0,-1]:
            with self.assertRaises(ValidationError):product(minimum_facings=value)
        p=product();p.width_mm=float('nan')
        with self.assertRaises(ValidationError):allocate_synthetic([p],[level()])

    def test_independent_validator_catches_tampering(self):
        ps=[product('a'),product('b')];ls=[level()];r=allocate_synthetic(ps,ls)
        bad=copy.deepcopy(r);bad['assignments'][1]['x_start_mm']=0;bad['assignments'][1]['x_end_mm']=100
        self.assertIn('OVERLAPPING_FACING_GROUPS',validate_synthetic(ps,ls,bad)['violations'])
        bad=copy.deepcopy(r);bad['assignments'].append(bad['assignments'][0])
        self.assertIn('DUPLICATE_PRODUCT_ALLOCATION',validate_synthetic(ps,ls,bad)['violations'])
        bad=copy.deepcopy(r);bad['assignments'][0]['unit_depth_mm']=1
        self.assertIn('PRODUCT_DIMENSION_MISMATCH',validate_synthetic(ps,ls,bad)['violations'])
        bad=copy.deepcopy(r);bad['assignments'][0]['facings']=0
        self.assertIn('MINIMUM_FACINGS_NOT_MET',validate_synthetic(ps,ls,bad)['violations'])

    def test_deterministic_and_pure(self):
        ps=[product('b'),product('a',rotation_policy='ALLOW_XY_SWAP')];ls=[level('b'),level('a')]
        before=([p.model_dump() for p in ps],[l.model_dump() for l in ls])
        with patch('builtins.open',side_effect=AssertionError('I/O')):r=allocate_synthetic(ps,ls)
        self.assertEqual(r,allocate_synthetic(list(reversed(ps)),list(reversed(ls))))
        self.assertEqual(before,([p.model_dump() for p in ps],[l.model_dump() for l in ls]))


if __name__=='__main__':unittest.main()
