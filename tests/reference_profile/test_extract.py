import hashlib
import json
import unittest
from pathlib import Path

import ezdxf
from shapely import affinity
from shapely.geometry import Polygon, box

from shared.planning_v2 import ReferenceDesignProfile
from tools.reference_profile.extract import classify_modules, extract_module, group_runs, spec_from_text


def module(mid,x,y,length=1500,depth=400,angle=0):
    p=affinity.rotate(box(x,y,x+length,y+depth),angle,origin=(x,y))
    return dict(module_id=mid,length_mm=length,depth_mm=depth,direction_deg=angle,
                footprint_mm=list(p.exterior.coords)[:-1],center_mm=list(p.centroid.coords[0]))


class ReferenceExtractionTests(unittest.TestCase):
    def test_label_parser_does_not_treat_function_text_as_spec(self):
        self.assertEqual(spec_from_text('1.5×0.4m'),(1500,400))
        self.assertIsNone(spec_from_text('线下1.2×0.4m'))
        self.assertIsNone(spec_from_text('7'))

    def test_labeled_sample_panel_is_translation_invariant(self):
        for dx,dy in [(0,0),(-9350,17600)]:
            ms=[module('arbitrary-a',dx,dy),module('arbitrary-b',dx,dy+1500,2000),
                module('real-a',dx+6000,dy),module('real-b',dx+7500,dy),
                module('isolated-real',dx+10000,dy+3000)]
            texts=[dict(text='1.5×0.4m',position_mm=[dx-1200,dy+300]),
                   dict(text='2×0.4m',position_mm=[dx-1200,dy+1800])]
            rows=classify_modules(ms,texts)
            self.assertEqual([r['classification'] for r in rows],['LEGEND','LEGEND','ACTUAL','ACTUAL','ACTUAL'])

    def test_single_spec_annotation_is_not_sufficient_for_legend(self):
        rows=classify_modules([module('a',0,0),module('b',6000,0)],
                              [dict(text='1.5×0.4m',position_mm=[-1000,300])])
        self.assertTrue(all(r['classification']=='ACTUAL' for r in rows))

    def test_rotated_insert_uses_true_footprint_not_aabb(self):
        doc=ezdxf.new();blk=doc.blocks.new('fixture')
        pts=[(0,0),(1700,0),(1700,500),(0,500)]
        for a,b in zip(pts,pts[1:]+pts[:1]):blk.add_line(a,b)
        ins=doc.modelspace().add_blockref('fixture',(1234,-987),dxfattribs={'rotation':37})
        m=extract_module(ins);p=Polygon(m['footprint_mm'])
        self.assertAlmostEqual(p.area,850000)
        self.assertAlmostEqual(m['length_mm'],1700)
        self.assertAlmostEqual(m['depth_mm'],500)
        self.assertAlmostEqual(m['direction_deg'],37)
        self.assertGreater(p.envelope.area,p.area*1.5)

    def test_join_tolerance_does_not_bridge_aisle(self):
        runs=group_runs([module('a',0,0),module('b',1500.2,0),module('c',4200.2,0)])
        self.assertEqual(sorted(r['module_count'] for r in runs),[1,2])
        self.assertAlmostEqual(next(r for r in runs if r['module_count']==2)['same_run_gaps_mm'][0],.2)

    def test_rotated_mixed_run(self):
        ms=[module('a',0,0,1500,400),module('b',1500,0,2000,400)]
        for m in ms:
            p=affinity.translate(affinity.rotate(Polygon(m['footprint_mm']),38,origin=(0,0)),2400,7800)
            m.update(footprint_mm=list(p.exterior.coords)[:-1],center_mm=list(p.centroid.coords[0]),direction_deg=38)
        runs=group_runs(ms)
        self.assertEqual(len(runs),1)
        self.assertEqual(runs[0]['effective_length_mm'],3500)
        self.assertAlmostEqual(runs[0]['same_run_gaps_mm'][0],0)

    def test_back_to_back_not_an_aisle_or_same_face(self):
        ms=[module('a',0,0),module('b',1500,0),module('c',0,400),module('d',1500,400),
            module('e',0,2000),module('f',1500,2000)]
        rs=group_runs(ms)
        self.assertEqual(len(rs),3)
        pairs=[n for r in rs for n in r['neighbors'] if r['run_id']<n['run_id']]
        self.assertEqual(len(pairs),2)
        self.assertEqual(sorted(n['clear_gap_mm'] for n in pairs),[0,1200])
        self.assertEqual(sorted(n['relation'] for n in pairs),['BACK_TO_BACK_CONTACT','PARALLEL_FREE_GAP'])

    def test_frozen_contract_and_internal_consistency(self):
        root=Path(__file__).resolve().parents[2]/'outputs/reference_profile'
        raw=(root/'reference_profile.json').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),(root/'FROZEN_SHA256.txt').read_text(encoding='utf-8-sig').split()[0])
        p=ReferenceDesignProfile.model_validate_json(raw)
        actual=[m for m in p.classification if m['classification']=='ACTUAL']
        self.assertEqual(len(actual),sum(r['module_count'] for r in p.runs))
        self.assertEqual(len(actual),sum(r['quantity'] for r in p.bom))
        self.assertEqual({m['module_id'] for m in actual},{mid for r in p.runs for mid in r['modules']})
        self.assertTrue(all(b['default_level_count'] is None for b in p.bom))
        self.assertIsNone(p.metrics['store_area_mm2'])
        self.assertTrue(all(Polygon(m['footprint_mm']).is_valid for m in actual))


if __name__=='__main__':unittest.main()
