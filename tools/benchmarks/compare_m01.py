"""Post-generation comparison only. Never called by any production service."""
import hashlib
import json
from pathlib import Path
from shared.planning_v2 import LayoutV2
from tools.benchmarks.metrics import layout_metrics, compare_reference


def main():
    root=Path(__file__).resolve().parents[2]
    folder=root/'outputs/reference_profile'
    raw=(folder/'reference_profile.json').read_bytes()
    expected=(folder/'FROZEN_SHA256.txt').read_text(encoding='utf-8-sig').split()[0]
    if hashlib.sha256(raw).hexdigest()!=expected:
        raise ValueError('Frozen reference digest mismatch')
    reference=json.loads(raw)
    result=[]
    for name in ('square','long','wide','medium','large','translated'):
        layout=LayoutV2.model_validate_json((root/f'outputs/iterations/M01/{name}.json').read_bytes())
        result.append({'case':name,**compare_reference(layout_metrics(layout),reference)})
    target=root/'outputs/iterations/M01/reference-comparison.json'
    target.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps([{'case':row['case'],'structure':row['structural_subset_status'],
                      'quality_gate':row['status']} for row in result]))


if __name__=='__main__':main()
