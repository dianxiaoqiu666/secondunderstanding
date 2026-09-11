'use strict';
import { node, pointsAttr, pathForPolygon } from './svg-primitives.js';
import { isOperations, describeOperationsInput, resetOperations, renderOperationsResult, appendOperationsDetail } from './operations-lab.js?v=m11-outside-door';

const $ = id => document.getElementById(id);
const state = {scenarios:[], scenario:null, result:null, request:0, busy:false, timer:null};
const sides = ['before','after'];
const finite = value => typeof value === 'number' && Number.isFinite(value);
const fmt = (value,digits=0) => finite(value) ? value.toLocaleString('zh-CN',{maximumFractionDigits:digits}) : '未提供';
const metrics = [
  ['module_count','货架模块数',0],
  ['run_count','连续货架排数',0],
  ['effective_length_mm','有效货架总长（mm）',1],
  ['average_modules_per_run','平均模块 / 排',2],
  ['average_run_length_mm','平均排长（mm）',1],
  ['short_run_count','短排数（仅 2 模块，诊断项）',0],
  ['tail_waste_mm','排尾剩余总长（mm）',1],
  ['max_same_run_gap_mm','同排最大间隙（mm）',3],
  ['minimum_inter_run_clearance_mm','最小排间净距（mm）',1],
  ['alignment_offset_mm','相邻平行排端点错位均值（mm）',1],
  ['geometry_violation_count','几何违规项数',0],
];

function element(tag,text,css) {
  const value=document.createElement(tag);
  if(text!==undefined)value.textContent=String(text);
  if(css)value.className=css;
  return value;
}
function difference(before,after,digits=0) {
  if(!finite(before)||!finite(after))return '不适用';
  const delta=after-before;
  return `${delta>0?'+':''}${fmt(delta,digits)}`;
}
function errorText(value) {
  if(typeof value==='string')return value;
  if(Array.isArray(value))return value.map(errorText).join('；');
  if(value?.detail)return errorText(value.detail);
  if(value?.message)return `${value.message}${value.code?`（${value.code}）`:''}`;
  if(value?.msg)return value.msg;
  return '本地服务没有提供有效结果，请重试。';
}
async function api(url,options={}) {
  const response=await fetch(url,{cache:'no-store',...options,headers:{Accept:'application/json',...options.headers}});
  let value;
  try {value=await response.json();} catch {throw new Error('本地服务未返回 JSON，请检查服务状态后重试。');}
  if(!response.ok)throw new Error(errorText(value));
  return value;
}
function showError(message) {
  $('error-box').hidden=!message;
  $('error-message').textContent=message || '';
  if(message)$('error-box').focus();
}
function clearResult() {
  resetOperations();
  state.result=null;$('result-panel').hidden=true;
  $('before-unavailable').hidden=true;
  sides.forEach(side=>{
    $(`${side}-canvas`).replaceChildren();$(`${side}-canvas`).removeAttribute('aria-hidden');
    $(`${side}-detail`).textContent='选择模块或整排后，这里显示规格、连续性和尾部剩余。';
    $(`${side}-run-select`).replaceChildren(element('option','从图中或列表选择一排'));
    $(`${side}-run-select`).disabled=false;
  });
}
function constraintSummary(space) {
  return `孔洞 ${(space?.boundary?.holes_mm || []).length} · 固定禁放区 ${(space?.exclusions || []).length} · 通行保留带 ${(space?.reserved_passages || space?.entrances || []).length} · 墙段 ${(space?.barriers || []).length}`;
}
function scenarioEvaluationLabel(scenario,detailed=false) {
  if(scenario.evaluation_role==='POST_CORRECTION_REGRESSION')return '发布门槛修正后回归';
  if(scenario.evaluation_role==='PREVIOUS_FAILED_HOLDOUT_REGRESSION')return '原失败留出 · 本轮回归';
  if(scenario.evaluation_role==='UPGRADED_GEOMETRY_REGRESSION')return '原几何样板 · 补齐作业输入';
  if(scenario.group==='holdout')return isOperations(scenario) && detailed?'独立留出验证 · 未参与调参':'留出验证';
  return '主验证';
}
function selectScenario(id,updateUrl=true) {
  clearResult();showError(null);
  state.scenario=state.scenarios.find(item=>item.id===id) || null;
  $('scenario-select').value=state.scenario?.id || '';
  $('generate-button').disabled=!state.scenario || state.busy;
  $('input-details').hidden=!state.scenario;
  if(!state.scenario){
    $('scenario-group').textContent='尚未选择';$('scenario-description').textContent='请从列表选择一个标准测试场景。';
    $('input-summary').textContent='';$('status-label').textContent='请选择场景';$('status-message').textContent='选择后点击“生成并比较”。';return;
  }
  const scenario=state.scenario;
  $('scenario-group').textContent=`${isOperations(scenario)?'员工作业 · ':''}${scenarioEvaluationLabel(scenario)}场景`;
  $('scenario-description').textContent=scenario.description || scenario.title;
  $('input-summary').textContent=isOperations(scenario)?describeOperationsInput(scenario.input):constraintSummary(scenario.input?.space);
  $('input-payload').textContent=JSON.stringify(scenario.input,null,2);
  $('status-label').textContent='场景已就绪';$('status-message').textContent=isOperations(scenario)?'点击“生成并比较”，运行单双面布局、入口接入、逐面可达和固定作业任务评价。':'点击“生成并比较”，自动运行前后布局与几何检查。';
  if(updateUrl){const url=new URL(location.href);url.searchParams.set('scenario',scenario.id);history.replaceState(null,'',url);}
}

function commonView(layouts) {
  // Camera bounds only. The page never derives or changes a planning boundary.
  const points=layouts.flatMap(layout=>{
    const space=layout.space;
    return [...(space?.boundary?.boundary_mm || []),...(space?.boundary?.holes_mm || []).flat(),
      ...Object.values(layout.workpoints || {}).map(value=>value.point_mm),
      ...(space?.exclusions || []).flatMap(item=>item.boundary_mm || []),
      ...(space?.reserved_passages || space?.entrances || []).flatMap(item=>item.boundary_mm || []),
      ...(space?.barriers || []).flatMap(item=>[item.start_mm,item.end_mm])];
  }).filter(point=>Array.isArray(point) && point.length>=2 && point.every(finite));
  if(points.length<4)throw new Error('结果没有完整的明确空间边界，无法绘制对比。');
  const xs=points.map(p=>p[0]),ys=points.map(p=>p[1]);
  const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);
  const span=Math.max(x1-x0,y1-y0);
  if(span<=0)throw new Error('结果空间尺寸无效，无法绘制对比。');
  const padding=span*.055;
  return {box:`${x0-padding} ${-y1-padding} ${x1-x0+padding*2} ${y1-y0+padding*2}`,span};
}
function activate(shape,action) {
  shape.addEventListener('click',action);
  shape.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();action();}});
}
function patterns(svg,side,span) {
  const defs=node('defs'),size=span/80;
  for(const [kind,color,background] of [['hole','#ca9839','#fff9ed'],['exclusion','#be566c','#fff1f3'],['passage','#268ca5','#edfafd']]){
    const pattern=node('pattern',{id:`${side}-${kind}-pattern`,patternUnits:'userSpaceOnUse',width:size,height:size});
    pattern.append(node('rect',{width:size,height:size,fill:background}));
    if(kind==='passage')pattern.append(node('circle',{cx:size/2,cy:size/2,r:size*.08,fill:color}));
    else {
      pattern.append(node('path',{d:`M0,${size} L${size},0`,stroke:color,'stroke-width':size*.08}));
      if(kind==='exclusion')pattern.append(node('path',{d:`M0,0 L${size},${size}`,stroke:color,'stroke-width':size*.08}));
    }
    defs.append(pattern);
  }
  svg.append(defs);
}
function renderPlan(side,result,view) {
  const layout=result.layout,space=layout.space,svg=$(`${side}-canvas`);
  svg.replaceChildren();svg.setAttribute('viewBox',view.box);patterns(svg,side,view.span);
  const layers=Object.fromEntries(['space','modules','runs','labels'].map(name=>{
    const group=node('g',{'data-layer':name});svg.append(group);return [name,group];
  }));
  layers.space.append(node('path',{d:pathForPolygon(space.boundary),class:'boundary','fill-rule':'evenodd','data-testid':'lab-boundary'}));
  (space.boundary.holes_mm || []).forEach((points,index)=>{
    const shape=node('polygon',{points:pointsAttr(points),class:'hole',style:`fill:url(#${side}-hole-pattern)`,'data-testid':'lab-hole'});
    shape.append(node('title',{},`孔洞 ${index+1}`));layers.space.append(shape);
  });
  for(const [kind,css,items] of [['exclusion','exclusion',space.exclusions],['passage','entrance',space.entrances]]){
    (items || []).forEach(item=>{
      const shape=node('path',{d:pathForPolygon(item),class:css,'fill-rule':'evenodd',style:`fill:url(#${side}-${kind}-pattern)`,'data-testid':`lab-${kind}`});
      shape.append(node('title',{},`${kind==='passage'?'通行保留带':'固定禁放区'} ${item.id}`));layers.space.append(shape);
    });
  }
  (space.barriers || []).forEach(wall=>{
    const line=node('line',{x1:wall.start_mm[0],y1:-wall.start_mm[1],x2:wall.end_mm[0],y2:-wall.end_mm[1],class:'barrier','data-testid':'lab-wall'});
    line.append(node('title',{},`实体墙 ${wall.id}`));layers.space.append(line);
  });
  (layout.shelves || []).forEach(shelf=>{
    const shape=node('polygon',{points:pointsAttr(shelf.footprint_mm),class:'shelf',role:'button',tabindex:0,'data-testid':'lab-module','data-module-id':shelf.id,
      'aria-label':`${shelf.id}，${shelf.length_mm} × ${shelf.depth_mm} mm，默认 ${shelf.default_level_count} 层`});
    shape.append(node('title',{},`${shelf.id} · ${shelf.material_id} · 默认 ${shelf.default_level_count} 层`));
    activate(shape,()=>selectObject(side,'module',shelf.id));layers.modules.append(shape);
  });
  const select=$(`${side}-run-select`);select.replaceChildren();
  const empty=element('option','从图中或列表选择一排');empty.value='';select.append(empty);
  (layout.runs || []).forEach(run=>{
    const shape=node('polygon',{points:pointsAttr(run.footprint_mm),class:'run',role:'button',tabindex:0,'data-testid':'lab-run','data-run-id':run.run_id,
      'aria-label':`连续排 ${run.run_id}，${run.modules.length} 个模块，剩余 ${run.remaining_length_mm} mm`});
    activate(shape,()=>selectObject(side,'run',run.run_id));layers.runs.append(shape);
    const label=node('text',{x:run.start_mm[0],y:-run.start_mm[1]-view.span/160,'font-size':view.span/65,class:'run-label',role:'button',tabindex:0,
      'data-testid':'lab-run-label','data-run-id':run.run_id,'aria-label':`查看整排 ${run.run_id}`},run.run_id);
    activate(label,()=>selectObject(side,'run',run.run_id));layers.labels.append(label);
    const option=element('option',`${run.run_id} · ${run.modules.length} 模块 · 余 ${fmt(run.remaining_length_mm,1)} mm`);option.value=run.run_id;select.append(option);
  });
  const m=result.measurements || {};
  $(`${side}-summary`).textContent=`${fmt(m.run_count)} 排 · ${fmt(m.module_count)} 模块 · 有效总长 ${fmt(m.effective_length_mm,1)} mm · 几何违规 ${fmt(m.geometry_violation_count)} 项`;
  $(`${side}-runtime`).textContent=`计算 ${fmt(result.elapsed_ms,1)} ms`;
  $(`${side}-constraints`).textContent=constraintSummary(space);
}
function renderUnavailableBaseline(result,view,space) {
  const svg=$('before-canvas');svg.replaceChildren();svg.setAttribute('viewBox',view.box);svg.setAttribute('aria-hidden','true');
  $('before-unavailable').hidden=false;$('before-unavailable-reason').textContent=errorText(result.error);
  $('before-summary').textContent='未找到可发布方案 · 布局、指标与清单不适用';
  $('before-runtime').textContent=`旧搜索 ${fmt(result.elapsed_ms,1)} ms`;
  $('before-constraints').textContent=`明确输入：${constraintSummary(space)}`;
  $('before-run-select').replaceChildren(element('option','本次基线无可查看的货架排'));$('before-run-select').disabled=true;
  $('before-detail').textContent='旧搜索没有提供货架排，无法查看基线规格、连续性或尾部剩余。右侧有效方案可照常选择和查看。';
}
function selectObject(side,kind,id) {
  const result=state.result?.[side];if(!result)return;
  const layout=result.layout;
  const shelf=kind==='module'?layout.shelves.find(item=>item.id===id):null;
  const run=kind==='run'?layout.runs.find(item=>item.run_id===id):layout.runs.find(item=>item.modules.some(module=>module.id===id));
  if(!run && !shelf)return;
  const svg=$(`${side}-canvas`);
  svg.querySelectorAll('[data-module-id]').forEach(shape=>shape.classList.toggle('selected',kind==='module'?shape.dataset.moduleId===id:run?.modules.some(module=>module.id===shape.dataset.moduleId)));
  svg.querySelectorAll('[data-run-id]').forEach(shape=>shape.classList.toggle('selected',shape.dataset.runId===run?.run_id));
  $(`${side}-run-select`).value=run?.run_id || '';
  const box=$(`${side}-detail`);box.replaceChildren();
  box.append(element('h4',`✓ ${kind==='module'?'模块 '+shelf.id:'连续排 '+run.run_id}`));
  if(shelf)box.append(element('p',`${shelf.material_id} · ${fmt(shelf.length_mm)} × ${fmt(shelf.depth_mm)} mm · 默认 ${fmt(shelf.default_level_count)} 层`));
  if(run){
    box.append(element('p',`${run.run_id} · ${fmt(run.direction_deg)}° · ${run.modules.length} 个模块 · 可用 ${fmt(run.available_length_mm,1)} mm · 已用 ${fmt(run.used_length_mm,1)} mm · 尾部剩余 ${fmt(run.remaining_length_mm,1)} mm`));
    if(kind==='run'){
      const groups=new Map();
      run.modules.forEach(module=>{const label=`${module.material_id}（${fmt(module.length_mm)} × ${fmt(module.depth_mm)} mm，默认 ${module.default_level_count} 层）`;groups.set(label,(groups.get(label)||0)+1);});
      box.append(element('p',[...groups].map(([label,count])=>`${label} × ${count}`).join('；')));
    }
    box.append(element('p',`连续性检查（全方案）：同排最大间隙 ${fmt(result.measurements?.max_same_run_gap_mm,3)} mm。当前排的模块与顺序来自后端布局。`,'lab-subtle'));
    if(shelf){const button=element('button',`查看整排 ${run.run_id}`);button.type='button';button.addEventListener('click',()=>selectObject(side,'run',run.run_id));box.append(button);}
  }
  if(isOperations(state.result))appendOperationsDetail(box,side,shelf,run);
}
function renderMetrics(result) {
  const body=$('metrics-body');body.replaceChildren();
  metrics.forEach(([key,label,digits])=>{
    const before=result.before.measurements?.[key],after=result.after.measurements?.[key];
    const row=element('tr');row.dataset.metric=key;
    const heading=element('th',label);heading.scope='row';row.append(heading);
    const display=value=>value===null && key==='minimum_inter_run_clearance_mm'?'无可比较排':fmt(value,digits);
    [result.before.layout?display(before):'不适用',display(after),difference(before,after,digits)].forEach(value=>row.append(element('td',value)));body.append(row);
  });
}
function renderBOM(result) {
  const rows=new Map(),totals={before:result.before.layout?0:null,after:0};
  sides.forEach(side=>(result[side].layout?.bom || []).forEach(item=>{
    const key=JSON.stringify([item.material_id,item.length_mm,item.depth_mm,item.default_level_count]);
    if(!rows.has(key))rows.set(key,{...item,before:result.before.layout?0:null,after:0});
    rows.get(key)[side]+=item.quantity;totals[side]+=item.quantity;
  }));
  sides.forEach(side=>{if(result[side].layout && totals[side]!==result[side].layout.shelves.length)throw new Error(`${side==='before'?'优化前':'优化后'}清单与模块数量不一致，不能作为有效对比结果。`);});
  const quantity=value=>value===null?'不适用':fmt(value);
  const body=$('bom-body');body.replaceChildren();
  [...rows.values()].sort((a,b)=>a.material_id.localeCompare(b.material_id,'zh-CN')).forEach(item=>{
    const row=element('tr');row.dataset.materialId=item.material_id;
    [item.material_id,`${fmt(item.length_mm)} × ${fmt(item.depth_mm)}`,fmt(item.default_level_count),quantity(item.before),quantity(item.after),difference(item.before,item.after)]
      .forEach(value=>row.append(element('td',value)));body.append(row);
  });
  sides.forEach(side=>$(`${side}-bom-total`).textContent=quantity(totals[side]));
  $('bom-total-change').textContent=difference(totals.before,totals.after);
  $('bom-consistency').textContent=result.before.layout
    ? `图中模块与清单一致：优化前 ${fmt(totals.before)} 个，优化后 ${fmt(totals.after)} 个。`
    : `优化后图中模块与清单一致：${fmt(totals.after)} 个。旧搜索无可发布方案，旧数量和数量变化均不适用。`;
}
function directionDisplay(counts) {
  const grouped=new Map();
  Object.entries(counts || {}).forEach(([angle,count])=>{
    const value=Number(angle);if(!Number.isFinite(value)||!finite(count))return;
    const rounded=(Math.round(value*10)/10).toFixed(1);grouped.set(rounded,(grouped.get(rounded)||0)+count);
  });
  return [...grouped].sort((a,b)=>Number(a[0])-Number(b[0])).map(([angle,count])=>`${angle}° × ${fmt(count)}`).join('；') || '未提供';
}
function renderReference(reference) {
  const detail=$('reference-details');detail.replaceChildren();
  if(reference?.status!=='AVAILABLE'){$('reference-status').textContent=`参考结构依据：${reference?.reason || reference?.note || '本次没有可用的参考指标。'}`;return;}
  $('reference-status').textContent=reference.note || '已生成布局后的独立结构对照。参考真实店面积未知，不作密度或数量门限。';
  const ref=reference.comparison?.reference || {};
  const gaps=value=>value?.count?`${fmt(value.min_mm,1)}–${fmt(value.max_mm,1)} mm（${fmt(value.count)} 项观测）`:'无可比观测';
  const verifiedGaps=(reference.comparison?.comparability?.reference_insert_parallel_gaps_mm || []).filter(finite);
  const verifiedGapSummary=verifiedGaps.length?{count:verifiedGaps.length,min_mm:Math.min(...verifiedGaps),max_mm:Math.max(...verifiedGaps)}:null;
  const grouped=finite(ref.module_count)&&finite(ref.isolated_module_count)?ref.module_count-ref.isolated_module_count:null;
  const list=element('ul',undefined,'lab-reference-list');
  for(const [label,value] of [
    ['严格重建连续排平均模块数',fmt(ref.mean_modules_per_run,2)],
    ['属于多模块排的模块数',fmt(grouped)],
    ['多模块连续排总长',`${fmt(ref.continuous_multi_module_length_mm,1)} mm`],
    ['模块方向（约 0.1°）',directionDisplay(ref.direction_module_counts)],
    ['已核验 INSERT 子集的平行排净距',gaps(verifiedGapSummary)],
    ['同排间隙观测',gaps(ref.reconstructed_same_run_gaps)],
  ])list.append(element('li',`${label}：${value}`));
  detail.append(list);
  detail.append(element('p','跨不同方向或分组的宽间隔不当作已确认店内过道；参考净距只是源观测，不替代本次场景的净距规则。','lab-subtle'));
}
function renderTradeoffs(result) {
  const depths=layout=>{
    const values=[...new Set((layout?.shelves || []).map(item=>item.depth_mm).filter(finite))].sort((a,b)=>a-b);
    return values.length?`${values.map(value=>fmt(value,1)).join('、')} mm`:'未提供';
  };
  const fields=[['effective_length_mm','有效总长'],['average_run_length_mm','平均排长'],['tail_waste_mm','排尾余量']];
  const before=result.before.measurements,after=result.after.measurements;
  if(!result.before.layout){
    $('selection-tradeoffs').textContent=`旧搜索未找到可发布结果，本次不计算前后差值。优化后${fields.map(([key,label])=>`${label} ${fmt(after?.[key],1)} mm`).join('，')}。优化后所选货架深度：${depths(result.after.layout)}。`;
    return;
  }
  const changes=fields.map(([key,label])=>{
    if(!finite(before?.[key]) || !finite(after?.[key]))return `${label}差值不适用`;
    const delta=after[key]-before[key];
    return delta===0?`${label}不变（差值 0 mm）`:`${label}${delta>0?'增加':'减少'} ${fmt(Math.abs(delta),1)} mm`;
  });
  const kept=result.selection?.kept_baseline?'候选未达到指标改善条件，最终保留基线。':'';
  $('selection-tradeoffs').textContent=`${kept}本次取舍：${changes.join('，')}。所选货架深度：${depths(result.before.layout)} → ${depths(result.after.layout)}。`;
}
function renderResult(result,scenario) {
  const missingBaseline=result.before?.status==='NOT_FOUND' && result.before.layout===null;
  if(!result.after?.layout || (!result.before?.layout && !missingBaseline))throw new Error('服务没有提供完整的本次方案结果，请重试。');
  const inputSpace=result.operations_input?.base?.space || result.input?.space;
  const view=commonView(inputSpace?[{space:inputSpace,workpoints:result.operations_input?.workpoints}]:sides.map(side=>result[side].layout).filter(Boolean));
  renderBOM(result);state.result=result;
  if(missingBaseline)renderUnavailableBaseline(result.before,view,result.input?.space || result.after.layout.space);
  else renderPlan('before',result.before,view);
  renderPlan('after',result.after,view);renderMetrics(result);renderReference(result.reference);
  $('comparison-title').textContent=`${scenario.title} · 同尺度前后对比`;
  $('result-identity').textContent=`${scenario.group==='holdout'?'留出验证':'主验证'} · ${result.input?.input_kind || 'ALGORITHM_VALIDATION'} · 本次明确输入`;
  const kept=result.selection?.kept_baseline;
  $('after-title').textContent=kept?'优化后 · 保留基线':'优化后 · 最终选择';
  $('selection-title').textContent=kept?'最终选择：保留基线':'最终选择：采用候选方案';
  $('selection-reason').textContent=result.selection?.reason || '服务未提供选择原因。';
  renderTradeoffs(result);
  const policy=result.selection?.policy;
  $('selection-policy').textContent=`评估候选 ${fmt(result.selection?.candidate_count)} 个${policy?` · 策略：${typeof policy==='string'?policy:JSON.stringify(policy)}`:''}`;
  $('input-sha256').textContent=result.input_sha256 || '未提供';
  if(isOperations(result)){
    renderOperationsResult(result,scenario,view);
    $('result-identity').textContent=`${scenarioEvaluationLabel(scenario,true)} · 员工专用门店 · 固定任务合成评测 · 完整输入含工作点与需求`;
  }
  $('result-panel').hidden=false;
}
async function generate() {
  if(!state.scenario || state.busy)return;
  const scenario=state.scenario,request=++state.request,start=performance.now();
  state.busy=true;clearResult();showError(null);
  $('scenario-select').disabled=true;$('generate-button').disabled=true;$('generate-button').textContent='正在生成…';
  $('status-label').textContent='正在运行算法比较';$('generation-progress').hidden=false;
  const progress=()=>{$('status-message').textContent=`正在运行${isOperations(scenario)?'单双面候选、逐面可达和作业路径':'基线、改进候选和几何检查'}，已等待 ${Math.floor((performance.now()-start)/1000)} 秒。完成后自动显示结果。`;};
  progress();state.timer=setInterval(progress,1000);
  try {
    const result=await api(`/api/algorithm/scenarios/${encodeURIComponent(scenario.id)}/generate`,{method:'POST'});
    if(request!==state.request)return;
    renderResult(result,scenario);
    $('status-label').textContent='比较已完成';$('status-message').textContent=isOperations(result)
      ? `${scenario.title}：已显示同一任务下的单双面布局、逐面可达、拣货与补货路线；人工产品验收待复验。`
      : result.before.layout
      ? `${scenario.title}：前后布局、检查指标和物料已显示。`
      : `${scenario.title}：旧搜索未找到可发布方案；已显示右侧有效方案及其检查指标、物料。`;
  } catch(error) {
    if(request!==state.request)return;
    clearResult();$('status-label').textContent='本次生成未通过';$('status-message').textContent='没有发布本次结果，具体原因见下方。';showError(error.message);
  } finally {
    clearInterval(state.timer);state.timer=null;
    if(request===state.request){state.busy=false;$('scenario-select').disabled=false;$('generate-button').disabled=!state.scenario;$('generate-button').textContent='生成并比较';$('generation-progress').hidden=true;}
  }
}
async function initialize() {
  try {
    const response=await api('/api/algorithm/scenarios');
    if(response.input_kind!=='ALGORITHM_VALIDATION' || !Array.isArray(response.scenarios) || !response.scenarios.length)throw new Error('服务没有提供标准算法验证场景。');
    state.scenarios=response.scenarios;
    const select=$('scenario-select');select.replaceChildren();
    const empty=element('option','请选择标准场景');empty.value='';select.append(empty);
    for(const [mode,group,label] of [[true,'main','员工作业 · 主验证与回归场景'],[true,'holdout','员工作业 · 独立留出场景'],[false,'main','历史纯几何 · 不计作业验收'],[false,'holdout','历史纯几何 · 不计作业验收']]){
      const optgroup=element('optgroup');optgroup.label=label;
      state.scenarios.filter(scenario=>scenario.group===group && isOperations(scenario)===mode).forEach(scenario=>{const option=element('option',scenario.title);option.value=scenario.id;optgroup.append(option);});
      if(optgroup.children.length)select.append(optgroup);
    }
    select.disabled=false;
    const requested=new URL(location.href).searchParams.get('scenario');
    selectScenario(requested || (state.scenarios.find(isOperations) || state.scenarios[0]).id,!requested);
    if(requested && !state.scenario)showError(`链接中的场景“${requested}”不存在，请从列表选择现有场景。`);
  } catch(error) {$('status-label').textContent='场景加载未完成';$('status-message').textContent='检查本地服务后刷新页面。';showError(error.message);}
}
$('scenario-select').addEventListener('change',event=>selectScenario(event.target.value));
$('generate-button').addEventListener('click',generate);
sides.forEach(side=>$(`${side}-run-select`).addEventListener('change',event=>{if(event.target.value)selectObject(side,'run',event.target.value);}));
initialize();
