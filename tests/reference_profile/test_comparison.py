import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from shared.contracts import Shelf
from shared.planning_v2 import LayoutV2
from tools.reference_profile.comparison import compare_layout


ROOT=Path(__file__).resolve().parents[2]


def rectangle(x,y,w,d):return [(x,y),(x+w,y),(x+w,y+d),(x,y+d)]


def fixture(state='SYNTHETIC_TEST',lengths=(1200,1500,1200)):
    shelves=[];runs=[]
    for j,y in enumerate([500,2300]):
        members=[];x=1500
        for i,length in enumerate(lengths):
            s=Shelf(id=f's{j}-{i}',material_id=f'm-{length}',x_mm=x+length/2,y_mm=y+200,
                    rotation_deg=0,length_mm=length,depth_mm=400,default_level_count=5,
                    footprint_mm=rectangle(x,y,length,400))
            members.append(s);shelves.append(s);x+=length
        runs.append(dict(run_id=f'r{j}',direction_deg=0,start_mm=[1500,y+200],end_mm=[x,y+200],
                         available_length_mm=sum(lengths),used_length_mm=sum(lengths),remaining_length_mm=0,depth_mm=400,
                         modules=members,footprint_mm=rectangle(1500,y,sum(lengths),400)))
    return LayoutV2(source={'filename':'synthetic.dxf','sha256':'0'*64},
                    space={'boundary':{'boundary_mm':rectangle(0,0,8000,5000)},'confirmation':{'state':state}},rules={},
                    shelves=shelves,runs=runs,bom=[dict(material_id='m-1200',length_mm=1200,depth_mm=400,default_level_count=5,quantity=4,total_level_count=20),
                                                dict(material_id='m-1500',length_mm=1500,depth_mm=400,default_level_count=5,quantity=2,total_level_count=10)],
                    metrics={},validation={})


class ComparisonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base=json.loads((ROOT/'outputs/reference_profile/reference_profile.json').read_text(encoding='utf-8-sig'))
        cls.supp=json.loads((ROOT/'outputs/reference_profile/reference_profile_v1.1.json').read_text(encoding='utf-8-sig'))

    def compare(self,layout):return compare_layout(layout,self.base,self.supp)

    def test_geometry_recomputed_and_reference_subsets_parallel(self):
        l=fixture();l.metrics={'shelf_count':99999,'isolated_module_ratio':1}
        r=self.compare(l)
        self.assertEqual(r['automatic']['module_count'],6)
        self.assertEqual(r['automatic']['reconstructed_single_face_run_count'],2)
        self.assertEqual(r['automatic']['isolated_module_count'],0)
        self.assertEqual(r['automatic']['distinct_material_count'],2)
        self.assertEqual(r['automatic']['mixed_material_runs'],2)
        self.assertEqual(r['reference']['module_count'],120)
        self.assertEqual(r['reference']['reconstructed_single_face_run_count'],44)
        self.assertEqual(r['reference']['near_collinear_diagnostic_run_count'],36)
        self.assertEqual(r['automatic_structure_status'],'PASS')

    def test_synthetic_never_passes_gate_c(self):
        r=self.compare(fixture())
        self.assertEqual(r['gate_c_status'],'REQUIRES_REVIEW')
        self.assertIsNone(r['automatic']['density']['confirmed_space_area_mm2'])
        self.assertIsNone(r['reference']['density']['confirmed_space_area_mm2'])

    def test_claimed_acceptance_metadata_cannot_approve(self):
        l=fixture('CONFIRMED');l.design_status='ACCEPTED';l.validation={'human_review':'PASS','gate_c':'PASS'}
        l.space.confirmation.note='All real rooms reviewed';l.space.confirmation.confirmed_by='test'
        r=self.compare(l)
        self.assertEqual(r['gate_c_status'],'REQUIRES_REVIEW')
        self.assertEqual(r['product_acceptance_status'],'NOT_ASSESSED')
        self.assertTrue(r['required_external_reviews'])
        self.assertEqual(r['comparability']['total_length_or_count_ratio_gate'],'DISABLED_DIFFERENT_SPACES_AND_RULES')

    def test_nominal_run_cannot_hide_disconnected_module(self):
        l=fixture();s=l.shelves[1]
        s.footprint_mm=rectangle(10000,500,1500,400)
        r=self.compare(l)
        self.assertEqual(r['gate_c_status'],'FAIL')
        self.assertGreater(r['automatic']['isolated_module_count'],0)
        self.assertIn('SHELF_OUTSIDE_CONFIRMED_GEOMETRY_OR_IN_HOLE',r['geometry_checks']['violation_codes'])

    def test_overlap_cannot_be_hidden_in_validation_metadata(self):
        l=fixture();l.shelves[1].footprint_mm=rectangle(2400,500,1500,400);l.validation={'overlap':'PASS'}
        r=self.compare(l)
        self.assertGreater(r['geometry_checks']['overlapping_module_pair_count'],0)
        self.assertEqual(r['automatic_structure_status'],'FAIL')

    def test_call_is_pure_no_io_no_input_mutations(self):
        l=fixture();before=l.model_dump();b=copy.deepcopy(self.base);s=copy.deepcopy(self.supp)
        with patch('builtins.open',side_effect=AssertionError('Unexpected I/O')):
            result=compare_layout(l,b,s)
        self.assertEqual(l.model_dump(),before)
        self.assertEqual(b,self.base);self.assertEqual(s,self.supp)
        self.assertEqual(result,compare_layout(l,b,s))

    def test_single_template_is_contextual_review_not_automatic_failure(self):
        l=fixture(lengths=(1200,1200,1200))
        r=self.compare(l)
        self.assertIn('SINGLE_TEMPLATE_REQUIRES_EXPLANATION_OF_AVAILABLE_LENGTH_AND_REJECTED_ALTERNATIVES',r['review_prompts'])
        self.assertEqual(r['gate_c_status'],'REQUIRES_REVIEW')


if __name__=='__main__':unittest.main()
