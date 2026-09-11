"""Standard parameter input to existing whole-run planning; no CAD or signing."""
import hashlib
import json
from time import perf_counter

from shared.explicit_planning import ExplicitPlanningInput, explicit_request
from services.planning.runs import generate_layout, validate_runs
from services.planning.measurements import measure_layout
from services.planning.products import assign_categories
from services.planning.engine import PlanningError


def finalize(req, layout):
    layout=layout.model_copy(deep=True)
    layout.products=assign_categories(req.business,layout.runs)
    layout.validation=validate_runs(req,layout)
    layout.design_status='VALIDATED'
    layout.metrics.pop('deterministic_sha256',None)
    layout.metrics.pop('hash_definition',None)
    canonical=json.dumps(layout.model_dump(mode='json'),sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    layout.metrics['deterministic_sha256']=hashlib.sha256(canonical.encode()).hexdigest()
    layout.metrics['hash_definition']='Canonical final JSON excluding deterministic_sha256 and hash_definition'
    return layout


def compare_explicit(value: ExplicitPlanningInput):
    from services.planning.search import optimize_layout
    req=explicit_request(value)
    start=perf_counter();before=None;before_error=None
    try:
        before=finalize(req,generate_layout(req))
    except PlanningError as error:
        if error.code not in ('NO_SAFE_LAYOUT','VALIDATION_FAILED'):raise
        before_error={'code':error.code,'message':'旧候选搜索没有可发布方案；不代表全局无解。'}
    before_ms=1000*(perf_counter()-start)
    start=perf_counter();after,selection=optimize_layout(req)
    after.metrics['selection_policy']=selection['policy']
    after.metrics['selection_objective_order']=selection['objective_order']
    after=finalize(req,after);after_ms=1000*(perf_counter()-start)
    return {'input':value.model_dump(mode='json'),'input_sha256':req.source.sha256,
        'before':{'layout':before.model_dump(mode='json') if before else None,
                  'measurements':measure_layout(req,before) if before else None,'elapsed_ms':before_ms,
                  'status':'AVAILABLE' if before else 'NOT_FOUND','error':before_error},
        'after':{'layout':after.model_dump(mode='json'),'measurements':measure_layout(req,after),'elapsed_ms':after_ms},
        'selection':selection}
