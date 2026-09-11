"""M01b evidence only; preserves all M01 files."""
import json
from pathlib import Path
from shared.contracts import Business
from services.planning.runs import generate_layout
from tools.benchmarks.spaces import cases, request


def main():
    root=Path.cwd()
    output=root/'outputs/iterations/M01b'
    output.mkdir(parents=True,exist_ok=True)
    catalog=json.loads((root/'input/production/materials/material_templates.v1.json').read_text(encoding='utf-8-sig'))
    business=Business(products=[],product_count=0,templates=catalog['templates'],sources=[])
    records=[]
    for name in ['square','long','wide','medium','large','translated']:
        before=json.loads((root/f'outputs/iterations/M01/{name}.json').read_text(encoding='utf-8-sig'))
        result=generate_layout(request(name,cases()[name],business))
        after=result.model_dump(mode='json')
        record={'case':name,'shelves_unchanged':before['shelves']==after['shelves'],
                'runs_unchanged':before['runs']==after['runs'],'bom_unchanged':before['bom']==after['bom'],
                'geometric_metrics_unchanged':all(before['metrics'][k]==after['metrics'][k]
                    for k in before['metrics'] if k not in ('deterministic_sha256','hash_definition')),
                'validation':result.validation}
        assert all(record[k] for k in ['shelves_unchanged','runs_unchanged','bom_unchanged','geometric_metrics_unchanged'])
        records.append(record)
    (output/'comparison.json').write_text(json.dumps(records,indent=2),encoding='utf-8')
    print(json.dumps(records,indent=2))


if __name__=='__main__':main()
