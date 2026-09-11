import copy
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from services.delivery import app as delivery
from shared.contracts import UnderstandingPackage


def layout_fixture():
    return {
        'schema_version': '1.0',
        'source': {'filename': '=source.dxf', 'sha256': 'a' * 64},
        'spatial': {'units': 'mm', 'scope': {'boundary_mm': [[0, 0], [10000, 0], [10000, 10000], [0, 0]], 'holes_mm': []}, 'walls': [], 'exclusion_zones': [], 'provenance': {}, 'warnings': []},
        'rules': {'aisle_mm': 1200, 'display_shelf_height_mm': 1800},
        'shelves': [{'id': 'S0001', 'material_id': 'SHELF-1200-400', 'x_mm': 2000, 'y_mm': 2000, 'rotation_deg': 0, 'length_mm': 1200, 'depth_mm': 400, 'default_level_count': 6, 'footprint_mm': [[1400, 1800], [2600, 1800], [2600, 2200], [1400, 2200], [1400, 1800]]}],
        'bom': [{'material_id': 'SHELF-1200-400', 'length_mm': 1200, 'depth_mm': 400, 'default_level_count': 6, 'quantity': 1}],
        'validation': {'status': 'PASS'}, 'planning': {}, 'business_summary': {'product_count': 0, 'physical_dimensions_known': 0}, 'warnings': [],
    }


def package_fixture(result):
    return UnderstandingPackage.model_validate({'source': result['source'], 'spatial': result['spatial'], 'business': {'products': [], 'product_count': 0, 'sources': [], 'templates': [{'material_id': 'SHELF-1200-400', 'length_mm': 1200, 'depth_mm': 400, 'default_level_count': 6}]}})


class DeliveryTests(unittest.TestCase):
    def test_reject_bom_mismatch(self):
        result = layout_fixture()
        result['bom'][0]['quantity'] = 2
        with self.assertRaisesRegex(ValueError, '数量不一致'):
            delivery.validate_delivery(result)

    def test_reject_spec_and_id_mismatch(self):
        result = layout_fixture()
        result['bom'][0]['default_level_count'] = 7
        with self.assertRaisesRegex(ValueError, '规格不一致'):
            delivery.validate_delivery(result)
        result = layout_fixture()
        result['shelves'].append(copy.deepcopy(result['shelves'][0]))
        result['bom'][0]['quantity'] = 2
        with self.assertRaisesRegex(ValueError, '编号重复'):
            delivery.validate_delivery(result)

    def test_reject_unsafe_validation(self):
        result = layout_fixture()
        result['validation']['status'] = 'FAIL'
        with self.assertRaisesRegex(ValueError, '未通过'):
            delivery.validate_delivery(result)

    def test_provenance_and_approved_template(self):
        result = delivery.validate_delivery(layout_fixture())
        package = package_fixture(result)
        delivery.validate_provenance(result, package)
        result['source']['sha256'] = 'b' * 64
        with self.assertRaisesRegex(ValueError, '不一致'):
            delivery.validate_provenance(result, package)
        result = delivery.validate_delivery(layout_fixture())
        result['bom'][0]['length_mm'] = 900
        with self.assertRaisesRegex(ValueError, '未批准'):
            delivery.validate_provenance(result, package)

    def test_workbook_actual_values_and_no_formula_injection(self):
        book = load_workbook(BytesIO(delivery.make_workbook(layout_fixture())))
        sheet = book['设计级货架清单']
        self.assertEqual(sheet['A2'].value, 'SHELF-1200-400')
        self.assertEqual(sheet['D2'].value, 6)
        self.assertEqual(sheet['E2'].value, 1)
        self.assertEqual(sheet['F2'].value, '缺失，需确认')
        self.assertEqual(book['说明与来源']['B6'].data_type, 's')
        self.assertEqual(book['说明与来源']['B6'].value, '=source.dxf')

    def test_http_upload_error_status_and_restart(self):
        root = Path(__file__).resolve().parent / '.tmp'
        root.mkdir(exist_ok=True, parents=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            runtime = Path(directory)
            with patch.object(delivery, 'RUNTIME', runtime), patch.object(delivery, 'DB', runtime / 'jobs.sqlite3'):
                with TestClient(delivery.failed_baseline_app) as client:  # Historical shell regression only.
                    self.assertEqual(client.get('/health').status_code, 200)
                    self.assertEqual(client.get('/').status_code, 200)
                    self.assertEqual(client.post('/api/jobs', files={'file': ('file.dwg', b'x')}).json()['detail']['code'], 'DXF_REQUIRED')
                    self.assertEqual(client.post('/api/jobs', files={'file': ('file.dxf', b'')}).json()['detail']['code'], 'EMPTY_FILE')
                    with patch.object(delivery, 'run_job'):
                        response = client.post('/api/jobs', files={'file': ('file.dxf', b'CAD TEST BYTES')})
                    self.assertEqual(response.status_code, 202)
                    job_id = response.json()['id']
                    self.assertEqual(client.get(f'/api/jobs/{job_id}').json()['status'], 'queued')
                    self.assertEqual(client.get(f'/api/jobs/{job_id}/layout.json').status_code, 409)
                    self.assertEqual(client.get('/api/jobs/not-found').status_code, 404)
                    delivery.initialize()
                    self.assertEqual(client.get(f'/api/jobs/{job_id}').json()['error']['code'], 'PROCESS_INTERRUPTED')


if __name__ == '__main__':
    unittest.main()
