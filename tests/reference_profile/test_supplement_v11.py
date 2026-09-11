import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import ezdxf

from tools.reference_profile.supplement_v11 import ROOT, OUT, freeze, line_faces


class LineSupplementTests(unittest.TestCase):
    def test_no_invented_divider_in_merged_face(self):
        doc=ezdxf.new();msp=doc.modelspace()
        pts=[(0,0),(2400,0),(2400,400),(0,400)]
        for a,b in zip(pts,pts[1:]+pts[:1]):msp.add_line(a,b)
        rows=line_faces(list(msp.query('LINE')))
        self.assertEqual(len(rows),1)
        self.assertAlmostEqual(rows[0]['length_mm'],2400)
        self.assertEqual(rows[0]['unsupported_perimeter_mm'],0)

    def test_source_partition_recovers_two_faces(self):
        doc=ezdxf.new();msp=doc.modelspace()
        pts=[(0,0),(2400,0),(2400,400),(0,400)]
        for a,b in zip(pts,pts[1:]+pts[:1]):msp.add_line(a,b)
        divider=msp.add_line((1200,.00000001),(1200,399.99999999))
        rows=line_faces(list(msp.query('LINE')))
        self.assertEqual(len(rows),2)
        self.assertTrue(all(abs(r['length_mm']-1200)<1e-6 for r in rows))
        self.assertTrue(all(divider.dxf.handle in r['source_line_handles'] for r in rows))
        self.assertTrue(all(r['unsupported_perimeter_mm']==0 for r in rows))

    def test_immutable_freeze_preserves_mtime_and_rejects_difference(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'.tmp') as td:
            p=Path(td)/'fixture.json';freeze(p,b'one');stat=p.stat()
            freeze(p,b'one');self.assertEqual(p.stat().st_mtime_ns,stat.st_mtime_ns)
            with self.assertRaises(ValueError):freeze(p,b'two')
            self.assertEqual(p.read_bytes(),b'one')

    def test_frozen_v11_support_and_combined_accounting(self):
        raw=(OUT/'reference_profile_v1.1.json').read_bytes();r=json.loads(raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(),(OUT/'FROZEN_V1.1_SHA256.txt').read_text(encoding='utf-8-sig').split()[0])
        self.assertEqual(r['base_profile_sha256'],hashlib.sha256((OUT/'reference_profile.json').read_bytes()).hexdigest())
        actual=[m for m in r['line_classification'] if m['classification']=='ACTUAL']
        self.assertTrue(all(m['unsupported_perimeter_mm']<=.001 for m in actual))
        self.assertTrue(all(m['source_line_handles'] for m in actual))
        self.assertEqual(len(actual),sum(row['quantity'] for row in r['line_bom']))
        for key in ['line_runs_strict_1mm','line_runs_near_collinear_6mm']:
            self.assertEqual({m['module_id'] for m in actual},{mid for run in r[key] for mid in run['modules']})
            self.assertEqual(len(actual),sum(run['module_count'] for run in r[key]))
        self.assertIsNone(r['completeness']['complete_design_shelf_count'])


if __name__=='__main__':unittest.main()
