"""Run from project root: python -B -m tests.planning_v2.record_m01."""
import json
from pathlib import Path

from shared.contracts import Business
from services.planning.runs import generate_layout, combine_modules
from tools.benchmarks.spaces import cases, request
from tools.benchmarks.metrics import layout_metrics


def main():
    root=Path.cwd()
    output=root/'outputs/iterations/M01'
    output.mkdir(parents=True,exist_ok=True)
    raw=json.loads((root/'input/production/materials/material_templates.v1.json').read_text(encoding='utf-8-sig'))
    business=Business(products=[],product_count=0,templates=raw['templates'],sources=[])
    before=json.loads((root/'outputs/baselines/SYNTHETIC_BASELINE/metrics.json').read_text(encoding='utf-8-sig'))
    comparison=[]
    for name in ['square','long','wide','medium','large','translated']:
        result=generate_layout(request(name,cases()[name],business))
        (output/f'{name}.json').write_text(result.model_dump_json(indent=2),encoding='utf-8')
        row={'case':name,'before':next(row for row in before if row['case']==name),
             'after':layout_metrics(result),'validation':result.validation}
        comparison.append(row)
    (output/'comparison.json').write_text(json.dumps(comparison,indent=2),encoding='utf-8')
    combo=combine_modules(6300,[t for t in business.templates if t.depth_mm==400])
    (output/'combination_6300.json').write_text(json.dumps(combo,indent=2),encoding='utf-8')
    print(json.dumps([{'case':r['case'],'modules':r['after']['module_count'],'runs':r['after']['run_count'],
                      'average':r['after']['average_modules_per_run'],'length_mm':r['after']['effective_length_mm'],
                      'tail_mm':r['after']['tail_waste_mm']} for r in comparison],indent=2))


if __name__=='__main__': main()
