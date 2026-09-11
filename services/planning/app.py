"""Independent HTTP service: standard JSON package in, validated layout out."""
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from shared.contracts import LayoutResult, UnderstandingPackage
from services.planning.engine import PlanningError, plan_package

app = FastAPI(title="L3 Planning", version="1.0.0")


@app.exception_handler(RequestValidationError)
def invalid_package(request, error):
    return JSONResponse(status_code=422, content={"detail": {
        "code": "INVALID_STANDARD_PACKAGE",
        "message": "标准输入包格式不符合 v1 契约；必须提供有效的 Planning Scope、毫米空间数据和物料配置。",
    }})


@app.get("/health")
def health():
    return {"status": "ok", "service": "planning"}


@app.post("/plan", response_model=LayoutResult)
def plan(package: UnderstandingPackage):
    try:
        return plan_package(package)
    except PlanningError as error:
        raise HTTPException(status_code=422, detail={"code": error.code, "message": error.message}) from error


# The rejected HTTP application remains available only to explicit baseline
# regression tests. The normal uvicorn entry point below exposes v2 only.
failed_baseline_app = app
app = FastAPI(title='L3 Continuous-run Planning', version='2.0')
app.add_api_route('/health', health, methods=['GET'])

from shared.planning_v2 import PlanRequest, LayoutV2
from shared.confirmation import verify_request
from services.planning.runs import generate_layout, validate_runs
from services.planning.products import assign_categories
from shared.explicit_planning import ExplicitPlanningInput
from shared.operations_planning import OperationsInput
from shared.existing_design import validate_existing
import hashlib
import json


@app.post('/plan-explicit')
def plan_explicit(package: ExplicitPlanningInput):
    """A real explicit-data entry, labelled algorithm validation, never CAD READY."""
    from services.planning.explicit import compare_explicit
    try:
        return compare_explicit(package)
    except PlanningError as error:
        message=error.message
        if error.code=='NO_SAFE_LAYOUT':
            message='当前候选搜索未找到满足模板与净距的连续排；这不是全局无解证明，请核对空间、模板和规则。'
        raise HTTPException(422,detail={'code':error.code,'message':message}) from error


@app.post('/existing-design/check')
def check_existing_design(package: dict):
    """Check observed source objects only. Never run the layout generator."""
    try:
        return validate_existing(package)
    except (ValueError,KeyError,TypeError) as error:
        raise HTTPException(422,detail={'code':'EXISTING_DATA_INVALID','message':str(error)}) from error


@app.post('/plan-operations')
def plan_operations(package: OperationsInput):
    """Synthetic employee workflows, including explicit entry and both pick sides."""
    from services.planning.operations import compare_operations
    try:
        return compare_operations(package)
    except PlanningError as error:
        raise HTTPException(422,detail={'code':error.code,'message':error.message}) from error
    except (ValueError,KeyError,TypeError) as error:
        raise HTTPException(422,detail={'code':'OPERATIONS_INPUT_OR_EVIDENCE_INVALID','message':str(error)}) from error


@app.post('/plan',include_in_schema=False)
def retired_plan():
    raise HTTPException(410, detail={'code':'FAILED_BASELINE_RETIRED','message':'旧孤立架接口已停用，请使用标准有效空间与 /plan-v2。'})


@app.post('/plan-v2', response_model=LayoutV2)
def plan_v2(package: PlanRequest):
    try:
        verify_request(package)
        result=generate_layout(package)
        result.products=assign_categories(package.business,result.runs)
        result.validation=validate_runs(package,result)
        # The independent geometric check is the release gate. Human input is
        # required only for missing source facts, never for routine stage approval.
        result.design_status='VALIDATED'
        result.metrics.pop('deterministic_sha256',None)
        result.metrics.pop('hash_definition',None)
        canonical=json.dumps(result.model_dump(mode='json'),sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
        result.metrics['deterministic_sha256']=hashlib.sha256(canonical.encode('utf-8')).hexdigest()
        result.metrics['hash_definition']='Canonical final JSON excluding deterministic_sha256 and hash_definition'
        return result
    except PlanningError as error:
        raise HTTPException(422, detail={'code':error.code,'message':error.message}) from error
    except ValueError as error:
        raise HTTPException(422, detail={'code':'CONFIRMATION_OR_BUSINESS_INVALID','message':str(error)}) from error
