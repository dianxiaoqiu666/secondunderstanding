"""Single local entry point: validated CAD facts to automatic design and delivery."""
from collections import Counter
from contextlib import asynccontextmanager, contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import uuid
from urllib.parse import urlparse

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field, StrictBool
from shared.contracts import Model, PolygonData, Exclusion
from shared.planning_v2 import LayoutV2, PlanRequest
from services.delivery.exports_v2 import make_workbook
from services.delivery.exports_existing import make_existing_workbook
from shared.existing_design import validate_existing, verify_existing_digest
from typing import Literal

ROOT=Path(__file__).resolve().parents[2]
STATIC=Path(__file__).parent/'static'
RUNTIME=ROOT/'runtime/workbench'
DB=RUNTIME/'jobs.sqlite3'
MAX_UPLOAD=25*1024*1024
PREPARATION_CONTRACT='M08.1'
UNDERSTANDING=os.environ.get('L3_UNDERSTANDING_URL','http://127.0.0.1:8101')
PLANNING=os.environ.get('L3_PLANNING_URL','http://127.0.0.1:8102')


def local_url(base,path):
    if urlparse(base).hostname not in {'127.0.0.1','localhost','::1'}:
        raise ValueError('微服务地址必须是本机回环地址。')
    return base.rstrip('/')+path


@contextmanager
def connect():
    db=sqlite3.connect(DB,timeout=30)
    db.row_factory=sqlite3.Row
    try:
        with db:yield db
    finally:db.close()


def initialize():
    RUNTIME.mkdir(parents=True,exist_ok=True)
    with connect() as db:
        db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY,status TEXT,progress INTEGER,message TEXT,error TEXT,prepared TEXT,result TEXT,filename TEXT)')
        if 'task_purpose' not in {row[1] for row in db.execute('PRAGMA table_info(jobs)')}:
            db.execute("ALTER TABLE jobs ADD COLUMN task_purpose TEXT NOT NULL DEFAULT 'generate'")
        db.execute("UPDATE jobs SET status='failed',message='处理被服务重启中断，请重新上传。' WHERE status IN ('queued','preparing','planning')")


def job(job_id):
    if not re.fullmatch('[0-9a-f]{32}',job_id):raise HTTPException(404,detail={'code':'JOB_NOT_FOUND','message':'未找到此任务。'})
    with connect() as db:row=db.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
    if row is None:raise HTTPException(404,detail={'code':'JOB_NOT_FOUND','message':'未找到此任务。'})
    result=dict(row)
    for key in ('error','prepared','result'):result[key]=json.loads(result[key]) if result[key] else None
    return result


def update(job_id,status,progress,message,**values):
    if set(values)-{'error','prepared','result'}:raise ValueError('Invalid job fields')
    fields=['status=?','progress=?','message=?']+[f'{key}=?' for key in values]
    args=[status,progress,message]+[json.dumps(v,ensure_ascii=False,allow_nan=False) if v is not None else None for v in values.values()]+[job_id]
    with connect() as db:db.execute(f'UPDATE jobs SET {",".join(fields)} WHERE id=?',args)


def response_json(response):
    try:data=response.json()
    except ValueError:raise ValueError('微服务没有返回有效JSON。')
    if response.is_error:
        detail=data.get('detail',data)
        if isinstance(detail,dict):raise ValueError(f"{detail.get('code','SERVICE_ERROR')}: {detail.get('message',detail)}")
        raise ValueError(str(detail))
    return data


def prepare_job(job_id):
    item=job(job_id)
    if item.get('task_purpose')=='existing_design':
        return prepare_existing_job(job_id,item)
    try:
        update(job_id,'preparing',20,'正在读取CAD和内部商品、物料资料…')
        with httpx.Client(timeout=120,trust_env=False) as client:
            with (RUNTIME/job_id/'source.dxf').open('rb') as source:
                prepared=response_json(client.post(local_url(UNDERSTANDING,'/prepare'),files={'file':(item['filename'],source,'application/dxf')}))
        if prepared.get('source',{}).get('sha256')!=hashlib.sha256((RUNTIME/job_id/'source.dxf').read_bytes()).hexdigest():
            raise ValueError('理解结果与当前上传文件不一致。')
        ready=prepared.pop('plan_request',None)
        prepared['delivery_preparation_contract']=PREPARATION_CONTRACT
        update(job_id,'preparing',40,'已读取CAD和内部业务数据。',prepared=prepared,error=None)
        if prepared.get('status')=='READY':
            if ready is None:raise ValueError('理解服务标记可规划但没有标准空间包。')
            run_plan(job_id,PlanRequest.model_validate(ready))
        else:
            update(job_id,'awaiting_confirmation',40,'仅需补充图上列出的缺失事实，提交后自动完成设计。',error=None)
    except Exception as error:
        update(job_id,'failed',0,'CAD准备失败。',error={'code':'PREPARATION_FAILED','message':str(error)})


def prepare_existing_job(job_id,item):
    try:
        update(job_id,'preparing',20,'正在读取这份图中的已有结构和对象…')
        path=RUNTIME/job_id/'source.dxf'
        with httpx.Client(timeout=180,trust_env=False) as client:
            with path.open('rb') as source:
                parsed=response_json(client.post(local_url(UNDERSTANDING,'/existing-design'),files={'file':(item['filename'],source,'application/dxf')}))
            if parsed['source']['sha256']!=hashlib.sha256(path.read_bytes()).hexdigest():
                raise ValueError('已有设计与当前上传文件不一致。')
            update(job_id,'preparing',65,'已读取原始位置，正在复核对象分类和观察清单…')
            checked=response_json(client.post(local_url(PLANNING,'/existing-design/check'),json=parsed))
        result=validate_existing(checked)
        if result!=validate_existing(parsed):
            raise ValueError('已有位置在服务间发生变化。')
        make_existing_workbook(result)
        (RUNTIME/job_id/'layout.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        update(job_id,'complete',100,'已有设计已打开；保留原图位置，未自动重新摆放。',result=result,error=None)
    except Exception as error:
        update(job_id,'failed',0,'已有设计读取未完成。',error={'code':'EXISTING_DESIGN_FAILED','message':str(error)})


class Resolution(Model):
    issue_id: str
    option_id: str


class ConfirmBody(Model):
    boundary: PolygonData | None = None
    entrances: list[Exclusion] | None = None
    exclusions: list[Exclusion] | None = None
    reviewed_fields: list[str]=Field(default_factory=list)
    resolutions: list[Resolution]=Field(default_factory=list)
    user_confirmed: StrictBool
    scope_edge_ids: list[str]=Field(default_factory=list)
    supplemental_edges: list[dict]=Field(default_factory=list)
    scope_candidate_id: str | None = None
    portal_closure_ids: list[str] | None = None
    perimeter_edge_ids: list[str] | None = None


class PreviewBody(Model):
    boundary: PolygonData | None = None
    entrances: list[Exclusion] | None = None
    exclusions: list[Exclusion]=Field(default_factory=list)
    resolutions: list[Resolution]=Field(default_factory=list)
    scope_edge_ids: list[str]=Field(default_factory=list)
    supplemental_edges: list[dict]=Field(default_factory=list)
    revoked_issue_ids: list[str]=Field(default_factory=list)
    scope_candidate_id: str | None = None
    portal_closure_ids: list[str] | None = None
    perimeter_edge_ids: list[str] | None = None


class ResolveBody(Model):
    resolutions: list[Resolution]=Field(min_length=1)
    user_confirmed: StrictBool


def check_delivery(value,request):
    result=LayoutV2.model_validate(value)
    if (result.source!=request.source or result.space!=request.space or result.rules!=request.rules):
        raise ValueError('规划结果的来源、确认空间或规则不匹配。')
    if result.design_status!='VALIDATED' or result.validation.get('status')!='PASS':
        raise ValueError('规划结果没有通过自动设计检查。')
    if result.shelves!=[s for r in result.runs for s in r.modules]:raise ValueError('货架排与实例不一致。')
    counts=Counter(s.material_id for s in result.shelves)
    if len({s.id for s in result.shelves})!=len(result.shelves):raise ValueError('货架编号重复。')
    if counts!={b.material_id:b.quantity for b in result.bom} or len(result.bom)!=len(counts):raise ValueError('BOM数量不一致。')
    templates={t.material_id:t for t in request.business.templates}
    for shelf in result.shelves:
        t=templates.get(shelf.material_id)
        if t is None or (t.length_mm,t.depth_mm,t.default_level_count)!=(shelf.length_mm,shelf.depth_mm,shelf.default_level_count):
            raise ValueError('货架实例使用未批准规格。')
    for row in result.bom:
        t=templates.get(row.material_id)
        if t is None or (t.length_mm,t.depth_mm,t.default_level_count)!=(row.length_mm,row.depth_mm,row.default_level_count):raise ValueError('BOM使用未批准规格。')
        if row.total_level_count!=row.quantity*row.default_level_count:raise ValueError('BOM总层数不一致。')
    assigned=[sid for category in result.products.category_assignments for sid in category['shelf_ids']]
    if len(assigned)!=len(set(assigned)) or set(assigned)-{s.id for s in result.shelves}:raise ValueError('品类架位重复或不存在。')
    count=sum(c['record_count'] for c in result.products.category_assignments+result.products.unassigned_categories)
    if count!=request.business.product_count:raise ValueError('品类商品计数不完整。')
    final=result.model_dump(mode='json')
    declared=final['metrics'].pop('deterministic_sha256',None)
    definition=final['metrics'].pop('hash_definition',None)
    canonical=json.dumps(final,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    if declared!=hashlib.sha256(canonical.encode('utf-8')).hexdigest():raise ValueError('最终结果摘要不匹配。')
    final['metrics']['deterministic_sha256']=declared
    final['metrics']['hash_definition']=definition
    return final


def run_plan(job_id,request):
    """Both source-ready and supplemented inputs share the same automatic path."""
    try:
        item=job(job_id)
        if request.source.model_dump(mode='json')!=item['prepared']['source']:
            raise ValueError('标准空间来源与本次上传不匹配。')
        update(job_id,'planning',65,'正在自动组合连续货架排、分配品类并检查安全…',error=None)
        with httpx.Client(timeout=180,trust_env=False) as client:
            value=response_json(client.post(local_url(PLANNING,'/plan-v2'),json=request.model_dump(mode='json')))
        result=check_delivery(value,request)
        # Build the actual workbook now: completion cannot precede export success.
        update(job_id,'planning',90,'几何检查通过，正在生成三维数据和物料文件…')
        make_workbook(result)
        (RUNTIME/job_id/'layout.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        update(job_id,'complete',100,'设计完成，可查看三维并下载布局和物料清单。',result=result,error=None)
    except Exception as error:
        update(job_id,'failed',0,'自动规划或交付检查未通过。',error={'code':'PLANNING_OR_DELIVERY_FAILED','message':str(error)})


def confirm_and_plan(job_id,body,endpoint='/confirm'):
    item=job(job_id)
    try:
        payload={**body,'prepared_id':item['prepared']['prepared_id']}
        # Keep explicit clears intact through the background worker as well.
        with httpx.Client(timeout=180,trust_env=False) as client:
            request=PlanRequest.model_validate(response_json(client.post(local_url(UNDERSTANDING,endpoint),json=payload)))
            if request.source.model_dump(mode='json')!=item['prepared']['source']:raise ValueError('确认结果来源不匹配。')
    except Exception as error:
        update(job_id,'awaiting_confirmation',40,'补充事实未通过验证，请查看具体问题。',error={'code':'INPUT_RESOLUTION_FAILED','message':str(error)})
        return
    run_plan(job_id,request)


@asynccontextmanager
async def lifespan(app):
    initialize()
    yield


app=FastAPI(title='L3 autonomous store design',version='2.1',lifespan=lifespan)
app.mount('/static',StaticFiles(directory=STATIC),name='static')
from services.delivery.algorithm_lab import router as algorithm_router
app.include_router(algorithm_router)


@app.get('/health')
def health():return {'status':'ok','service':'delivery'}


@app.get('/')
def index():return FileResponse(STATIC/'workbench.html')


@app.get('/diagnostics/source-lines')
def source_lines_diagnostic():
    path=ROOT/'outputs/repair_m06/cad_trace/line_trace.html'
    if not path.is_file():raise HTTPException(404,detail='真实源线标注图尚未生成。')
    return FileResponse(path,media_type='text/html')


@app.post('/api/jobs',include_in_schema=False)
def retired():raise HTTPException(410,detail={'code':'FAILED_BASELINE_RETIRED','message':'旧自动粗框3D流程已停用，请使用新版CAD确认入口。'})


@app.post('/api/v2/jobs',status_code=202)
async def upload(background:BackgroundTasks,file:UploadFile=File(...),task_purpose:Literal['generate','existing_design']=Form('generate')):
    filename=Path((file.filename or '').replace('\\','/')).name
    if not filename.lower().endswith('.dxf'):raise HTTPException(422,detail={'code':'DXF_REQUIRED','message':'请上传DXF；DWG需先导出为DXF。'})
    data=await file.read(MAX_UPLOAD+1)
    await file.close()
    if not data:raise HTTPException(422,detail={'code':'EMPTY_FILE','message':'CAD文件为空。'})
    if len(data)>MAX_UPLOAD:raise HTTPException(413,detail={'code':'FILE_TOO_LARGE','message':'CAD不得超过25MB。'})
    job_id=uuid.uuid4().hex
    folder=RUNTIME/job_id
    folder.mkdir()
    (folder/'source.dxf').write_bytes(data)
    with connect() as db:db.execute('INSERT INTO jobs(id,status,progress,message,filename,task_purpose) VALUES(?,?,?,?,?,?)',(job_id,'queued',5,'已接收CAD。',filename,task_purpose))
    background.add_task(prepare_job,job_id)
    return {'id':job_id,'status':'queued'}


@app.get('/api/v2/jobs/{job_id}')
def status(job_id:str,background:BackgroundTasks):
    item=job(job_id)
    # A persisted M05 preparation has no selectable source-edge contract.
    # Re-read its unchanged upload on first visit instead of showing new tools
    # over stale data. Signed applicable facts are restored by Service 1.
    if item['status']=='awaiting_confirmation' and (item.get('prepared') or {}).get('delivery_preparation_contract')!=PREPARATION_CONTRACT:
        with connect() as db:
            changed=db.execute("UPDATE jobs SET status='queued',message='正在更新原图读取结果，保留适用的有效补充…' WHERE id=? AND status='awaiting_confirmation'",(job_id,)).rowcount
        if changed:background.add_task(prepare_job,job_id)
        item=job(job_id)
    return item


@app.post('/api/v2/jobs/{job_id}/preview-input')
def preview_input(job_id:str,body:PreviewBody):
    return local_input_action(job_id,body,'/preview-input')


@app.post('/api/v2/jobs/{job_id}/apply-input')
def apply_input(job_id:str,body:PreviewBody):
    return local_input_action(job_id,body,'/apply-input')


def local_input_action(job_id,body,endpoint):
    item=job(job_id)
    if item['status']!='awaiting_confirmation' or item.get('task_purpose','generate')!='generate':
        raise HTTPException(409,detail={'code':'NOT_AWAITING_INPUT','message':'当前任务没有待处理的规划输入。'})
    # Omitted work fields mean reuse; explicit [] / null mean undo. Preserve
    # that distinction across the service boundary, including candidate clears.
    payload={**body.model_dump(mode='json',exclude_unset=True),'prepared_id':item['prepared']['prepared_id']}
    with httpx.Client(timeout=180,trust_env=False) as client:
        response=client.post(local_url(UNDERSTANDING,endpoint),json=payload)
        if endpoint=='/apply-input' and response.is_success:
            refreshed=response_json(client.get(local_url(UNDERSTANDING,f"/prepared/{item['prepared']['prepared_id']}")))
            if refreshed['source']!=item['prepared']['source']:
                raise HTTPException(409,detail={'code':'SOURCE_CHANGED','message':'局部补充来源不匹配，不能更新页面。'})
            refreshed.pop('plan_request',None);refreshed.pop('confirmed_request',None)
            refreshed['delivery_preparation_contract']=PREPARATION_CONTRACT
            update(job_id,'awaiting_confirmation',40,'已保存本次有效事实；剩余问题完成后自动继续。',prepared=refreshed,error=None)
    return Response(response.content,status_code=response.status_code,media_type='application/json')


@app.post('/api/v2/jobs/{job_id}/confirm',status_code=202)
def confirm(job_id:str,body:ConfirmBody,background:BackgroundTasks):
    job(job_id)
    with connect() as db:
        cursor=db.execute("UPDATE jobs SET status='planning',message='正在核验人工确认…' WHERE id=? AND status='awaiting_confirmation'",(job_id,))
        if cursor.rowcount!=1:raise HTTPException(409,detail={'code':'NOT_AWAITING_CONFIRMATION','message':'任务当前不能重复确认。'})
    background.add_task(confirm_and_plan,job_id,body.model_dump(mode='json',exclude_unset=True))
    return {'id':job_id,'status':'planning'}


@app.post('/api/v2/jobs/{job_id}/resolve',status_code=202)
def resolve(job_id:str,body:ResolveBody,background:BackgroundTasks):
    job(job_id)
    with connect() as db:
        cursor=db.execute("UPDATE jobs SET status='planning',message='正在验证局部补充并自动继续…' WHERE id=? AND status='awaiting_confirmation'",(job_id,))
        if cursor.rowcount!=1:raise HTTPException(409,detail={'code':'NOT_AWAITING_INPUT','message':'任务当前没有待补充输入。'})
    background.add_task(confirm_and_plan,job_id,body.model_dump(mode='json'),'/resolve')
    return {'id':job_id,'status':'planning'}


def review_job(job_id):
    item=job(job_id)
    if item['status']!='complete':raise HTTPException(409,detail={'code':'RESULT_NOT_READY','message':'尚未生成通过检查的设计。'})
    return item


@app.get('/api/v2/jobs/{job_id}/preview.json')
@app.get('/api/v2/jobs/{job_id}/layout.json')
def preview(job_id:str):
    value=review_job(job_id)['result']
    if value.get('task_purpose')=='existing_design':
        try:verify_existing_digest(value)
        except ValueError as error:raise HTTPException(409,detail={'code':'RESULT_DIGEST_INVALID','message':str(error)}) from error
        return Response(json.dumps(value,ensure_ascii=False,indent=2),media_type='application/json',
                        headers={'Content-Disposition':'attachment; filename="existing-design.json"'})
    copied=json.loads(json.dumps(value))
    expected=copied['metrics'].pop('deterministic_sha256',None)
    copied['metrics'].pop('hash_definition',None)
    canonical=json.dumps(copied,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False)
    if hashlib.sha256(canonical.encode('utf-8')).hexdigest()!=expected:
        raise HTTPException(409,detail={'code':'RESULT_DIGEST_INVALID','message':'保存结果的摘要不一致，不能下载。'})
    return Response(json.dumps(value,ensure_ascii=False,indent=2),media_type='application/json',
                    headers={'Content-Disposition':'attachment; filename="store-layout.json"'})


@app.get('/api/v2/jobs/{job_id}/materials.xlsx')
def materials(job_id:str):
    # The JSON endpoint independently checks stored integrity before either export.
    value=json.loads(preview(job_id).body)
    workbook=make_existing_workbook(value) if value.get('task_purpose')=='existing_design' else make_workbook(value)
    return Response(workbook,media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition':'attachment; filename="shelf-materials.xlsx"'})


@app.get('/api/v2/jobs/{job_id}/reference.svg')
def reference_svg(job_id:str):
    review_job(job_id)
    path=ROOT/'outputs/reference_profile/reference-v1.1.svg'
    if not path.is_file():raise HTTPException(503,detail='独立参考图未生成，请运行参考提取工具。')
    return FileResponse(path,media_type='image/svg+xml')


@app.get('/api/v2/jobs/{job_id}/comparison')
def comparison(job_id:str):
    item=review_job(job_id)
    if item.get('task_purpose')=='existing_design':
        raise HTTPException(409,detail='已有设计查看不执行自动方案参考对照。')
    # Read only AFTER planning, never pass reference data to either generator.
    from tools.reference_profile.comparison import compare_layout
    folder=ROOT/'outputs/reference_profile'
    profiles=[]
    for name,manifest in [('reference_profile.json','FROZEN_SHA256.txt'),('reference_profile_v1.1.json','FROZEN_V1.1_SHA256.txt')]:
        raw=(folder/name).read_bytes()
        expected=(folder/manifest).read_text(encoding='utf-8-sig').split()[0]
        if hashlib.sha256(raw).hexdigest()!=expected:raise HTTPException(409,detail='参考冻件摘要不匹配。')
        profiles.append(json.loads(raw))
    return compare_layout(LayoutV2.model_validate(item['result']),*profiles)
