"""Fixture-owned source read and M03 evidence; production products.py has no I/O."""
import hashlib
import json
from collections import Counter
from pathlib import Path

from services.understanding.app import load_business
from services.planning.runs import generate_layout
from services.planning.products import assign_categories, allocate_synthetic
from shared.planning_v2 import SyntheticProduct, ShelfLevel
from tools.benchmarks.spaces import cases, request


def main():
    root=Path(__file__).resolve().parents[2]
    out=root/'outputs/iterations/M03';out.mkdir(parents=True,exist_ok=True)
    business=load_business(root)
    req=request('category-real-data-synthetic-space',cases()['large'],business)
    layout=generate_layout(req)
    plan=assign_categories(business,layout.runs)
    assert plan==assign_categories(business,list(reversed(layout.runs)))
    paths=Counter(p['category'] for p in business.products)
    top=Counter(p.split('>')[0].strip() for p in paths)
    source_hashes=[dict(path=s.get('path'),sha256=s.get('sha256')) for s in business.sources]
    synthetic=[SyntheticProduct(product_id='priority',category='A',width_mm=150,depth_mm=200,height_mm=250,demand_weight=5,minimum_facings=2),
               SyntheticProduct(product_id='rotate',category='B',width_mm=350,depth_mm=100,height_mm=200,rotation_policy='ALLOW_XY_SWAP'),
               SyntheticProduct(product_id='tall',category='A',width_mm=100,depth_mm=100,height_mm=900),
               SyntheticProduct(product_id='overflow',category='A',width_mm=400,depth_mm=200,height_mm=200),
               SyntheticProduct(product_id='wrong-category',category='C',width_mm=100,depth_mm=100,height_mm=100)]
    levels=[ShelfLevel(shelf_id='synthetic-A',level_index=0,width_mm=500,depth_mm=400,clear_height_mm=300,category='A'),
            ShelfLevel(shelf_id='synthetic-B',level_index=0,width_mm=200,depth_mm=400,clear_height_mm=300,category='B')]
    sim=allocate_synthetic(synthetic,levels)
    assert sim==allocate_synthetic(list(reversed(synthetic)),list(reversed(levels)))
    report=dict(milestone='M03_PRODUCT_ALGORITHMS',space_status='SYNTHETIC_TEST_NOT_REAL_CAD_ACCEPTANCE',
                source_product_records=len(business.products),source_full_category_paths=len(paths),source_top_categories=len(top),
                shelf_slots=len(layout.shelves),assigned_top_categories=len(plan.category_assignments),
                unassigned_category_groups=len(plan.unassigned_categories),
                category_shelf_quotas=[dict(category=a['category'],source_records=a['record_count'],shelves=a['initial_shelf_quota']) for a in plan.category_assignments],
                real_sku_status=plan.real_sku_status,real_sku_assignments=len(plan.sku_assignments),
                missing_physical_record_count=len(plan.unassigned_products),business_source_evidence=source_hashes,
                synthetic_assigned=len(sim['assignments']),synthetic_unassigned=sim['unassigned'],synthetic_validation=sim['validation'],
                deterministic=True)
    (out/'category_plan.json').write_text(plan.model_dump_json(indent=2)+'\n',encoding='utf-8')
    (out/'synthetic_plan.json').write_text(json.dumps(sim,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    raw=(json.dumps(report,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    (out/'metrics.json').write_bytes(raw)
    (out/'SHA256.txt').write_text(hashlib.sha256(raw).hexdigest()+'  metrics.json\n',encoding='utf-8')
    print(json.dumps({k:v for k,v in report.items() if k not in ['category_shelf_quotas','business_source_evidence']},ensure_ascii=False,indent=2))


if __name__=='__main__':main()
