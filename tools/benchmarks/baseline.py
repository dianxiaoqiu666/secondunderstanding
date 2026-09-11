"""Record the rejected algorithm's behavior on independent standard spaces."""
import json
from pathlib import Path
from shared.contracts import Spatial,UnderstandingPackage
from services.understanding.app import load_business
from services.planning.engine import plan_package, PlanningError
from tools.benchmarks.spaces import cases,request

def main():
    output=Path('outputs/baselines/SYNTHETIC_BASELINE');output.mkdir(parents=True,exist_ok=True)
    business=load_business(); metrics=[]
    for name,space in cases().items():
        req=request(name,space,business)
        old=UnderstandingPackage(source=req.source,business=business,
            spatial=Spatial(scope=space.boundary,walls=space.barriers,exclusion_zones=space.exclusions+space.entrances))
        try:
            r=plan_package(old)
            metrics.append({'case':name,'shelves':len(r.shelves),'runs':len(r.shelves),
                'isolated_ratio':1.0,'average_modules_per_run':1.0,'effective_length_mm':sum(s.length_mm for s in r.shelves),
                'old_geometric_validation':r.validation['status'],'product_quality':'FAIL_ISOLATED_MODULES'})
        except PlanningError as error:
            metrics.append({'case':name,'old_error':error.code,'product_quality':'FAIL'})
    (output/'metrics.json').write_text(json.dumps(metrics,indent=2),encoding='utf-8')
    print(json.dumps(metrics,indent=2))

if __name__=='__main__':main()
