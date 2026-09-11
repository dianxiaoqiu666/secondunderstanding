'use strict';
import { StoreViewerV2 } from './viewer-v2.js';
import { node, pointsAttr, pathForPolygon } from './svg-primitives.js';
(() => {
  const $ = id => document.getElementById(id);
  const svg = $('planning-canvas');
  const reviewFields = ['boundary', 'holes', 'barriers', 'exclusions', 'entrances'];
  const labels = {queued:'已排队',preparing:'正在理解图纸',awaiting_confirmation:'请补充必要事实',planning:'正在自动规划',complete:'规划完成',review_required:'历史二维方案',failed:'处理未通过'};
  let viewer=null;
  const state = {job:null,status:null,prepared:null,result:null,boundary:[],holes:[],fixedHoles:[],entrances:[],exclusions:[],fixedExclusions:[],barriers:[],added:[],draft:[],mode:null,pan:false,view:{x:0,y:0,w:10000,h:7000},fit:null,pointer:null,regionCounter:0,selection:null,poll:null,comparisonJob:null,issues:[],requiredFields:[],resolutions:{},originalIssues:[],activeIssueId:null,scopeEdgeIds:[],perimeterEdgeIds:[],perimeterDirty:false,localFaces:[],perimeterCandidates:[],gapCandidates:[],scopeReady:false,supplementalEdges:[],snap:null,lineStart:null,history:[],dirtyFields:[],previewVersion:0,previewBusy:false,taskPurpose:'generate',feedback:[],appliedResolutions:[],revokedIssueIds:[],scopeCandidateId:null,scopeCandidates:[],portalClosureIds:[],portalClosures:[],doorGapCandidates:[],derivedTopology:{nodes:[],edges:[]},topologyDiagnostics:[],boundarySourceEdgeIds:[],portalSelectionDirty:false};
  // Keep the task choice above the drawing; the local guide is next to it.
  $('task-bar').append($('upload-card'),document.querySelector('.status-card'));
  document.querySelector('.guide-bottom').prepend($('resolve-issues'));
  const guideScroll=document.createElement('div');guideScroll.id='guide-scroll';guideScroll.dataset.testid='guide-scroll';
  for(const child of [...$('editor-panel').children])if(!child.classList.contains('guide-bottom'))guideScroll.append(child);
  $('editor-panel').prepend(guideScroll);
  $('resolve-issues').textContent='应用并检查';$('confirm-space').hidden=true;
  const sourceLayer=node('g',{id:'source-layer'}),snapLayer=node('g',{id:'snap-layer','pointer-events':'none'});
  svg.insertBefore(sourceLayer,$('vertex-layer'));svg.append(snapLayer);
  const topologyLayer=node('g',{id:'topology-layer'});svg.insertBefore(topologyLayer,sourceLayer);
  const focusLayer=node('g',{id:'local-focus-layer','pointer-events':'none'});svg.append(focusLayer);
  const observedLayer=node('g',{id:'observed-layer','pointer-events':'none'});
  svg.insertBefore(observedLayer,$('shelf-layer'));
  const finitePoint = p => Array.isArray(p) && p.length >= 2 && Number.isFinite(p[0]) && Number.isFinite(p[1]);
  const ring = points => {
    const value = (points || []).filter(finitePoint).map(p => [p[0],p[1]]);
    if (value.length > 1 && value[0][0] === value[value.length-1][0] && value[0][1] === value[value.length-1][1]) value.pop();
    return value;
  };
  const closed = points => points.length ? [...points.map(p=>[...p]), [...points[0]]] : [];
  const fmt = (v,d=0) => typeof v === 'number' && Number.isFinite(v) ? v.toLocaleString('zh-CN',{maximumFractionDigits:d}) : '未知';
  function directionDisplay(counts) {
    const grouped=new Map();
    Object.entries(counts || {}).forEach(([angle,count])=>{
      const value=Number(angle), quantity=Number(count);
      if (!Number.isFinite(value) || !Number.isFinite(quantity)) return;
      const rounded=(Math.round(value*10)/10).toFixed(1);
      grouped.set(rounded,(grouped.get(rounded) || 0)+quantity);
    });
    return [...grouped.entries()].sort((a,b)=>Number(a[0])-Number(b[0]))
      .map(([angle,count])=>`${angle}° × ${fmt(count)}`).join('；') || '未知';
  }
  function appendPolygon(parent,points,css,attrs={}) {
    if (points.length >= 3) parent.append(node('polygon',{points:pointsAttr(points),class:css,...attrs}));
  }
  function showError(error) {
    $('error-box').hidden = !error;
    $('error-box').replaceChildren();
    document.title=error?'需要处理 · 门店 CAD 工作台':'L3 · 门店 CAD 工作台';
    if(error){
      const title=document.createElement('strong');title.textContent='这一步未通过，已保留你的其他操作';
      const message=document.createElement('p');message.textContent=String(error);
      const link=document.createElement('a');link.href=state.activeIssueId?'#active-issue-title':'#cad-file';
      link.textContent=state.activeIssueId?'回到当前问题修正':'回到上传图纸';
      link.addEventListener('click',event=>{event.preventDefault();if(state.activeIssueId)focusIssue(state.activeIssueId,true);else $('cad-file').focus();});
      $('error-box').append(title,message,link);$('error-box').tabIndex=-1;$('error-box').focus();
      if(isEditable()){setFeedback([{code:'ACTION_NOT_APPLIED',message:String(error)}],false);}
    }
  }
  function errorText(value) {
    if (typeof value === 'string') return value;
    if (!value) return '请检查输入后重试。';
    if (value.message) return `${value.message}${value.code ? `（${value.code}）` : ''}`;
    if (value.detail) return errorText(value.detail);
    return JSON.stringify(value);
  }
  async function api(url,options) {
    const response = await fetch(url,options);
    const text = await response.text();
    let value;
    try { value = JSON.parse(text); } catch { throw new Error('服务未返回有效结果，请检查本地服务状态。'); }
    if (!response.ok) throw new Error(errorText(value));
    return value;
  }
  function viewApply() {
    svg.setAttribute('viewBox',`${state.view.x} ${state.view.y} ${state.view.w} ${state.view.h}`);
    renderVertices();
    renderSourceSelection();renderTopologyOverlay();
  }
  function fitBounds(bounds,minSpan=1000) {
    if (!Array.isArray(bounds) || bounds.length !== 4 || !bounds.every(Number.isFinite)) return;
    const [x0,y0,x1,y1] = bounds;
    const w = Math.max(minSpan,x1-x0), h = Math.max(minSpan,y1-y0), pad = Math.max(w,h)*.07;
    state.fit = {x:(x0+x1-w)/2-pad,y:-(y0+y1+h)/2-pad,w:w+2*pad,h:h+2*pad};
    state.view = {...state.fit}; viewApply();
  }
  function boundsFromPoints(points) {
    const ps = points.filter(finitePoint);
    if (!ps.length) return null;
    return [Math.min(...ps.map(p=>p[0])),Math.min(...ps.map(p=>p[1])),Math.max(...ps.map(p=>p[0])),Math.max(...ps.map(p=>p[1]))];
  }
  function world(event) {
    const p = svg.createSVGPoint(); p.x = event.clientX; p.y = event.clientY;
    const matrix = svg.getScreenCTM();
    if (!matrix) return {x:0,y:0};
    return p.matrixTransform(matrix.inverse());
  }
  function zoom(factor,anchor) {
    const v = state.view;
    const point = anchor || {x:v.x+v.w/2,y:v.y+v.h/2};
    // This is a camera limit only. A local sub-millimeter gap must stay locally
    // enlarged when the user zooms; CAD geometry and snapping are unchanged.
    const width = Math.max(.001,Math.min(1e8,v.w*factor)), ratio = width/v.w;
    state.view = {x:point.x-(point.x-v.x)*ratio,y:point.y-(point.y-v.y)*ratio,w:width,h:v.h*ratio};
    viewApply();
  }
  function invalidateReview() {
    // Server preview validates the changed fact. There is no full-plan checklist.
    reviewFields.forEach(f=>{$(`review-${f}`).checked=false;});
    updateEditorButtons();
  }
  function isEditable() { return state.status === 'awaiting_confirmation'; }
  function updateEditorButtons() {
    $('finish-drawing').disabled = !state.mode || state.draft.length<3;
    $('undo-point').disabled = !state.mode || !state.draft.length;
    $('cancel-drawing').disabled = !state.mode;
    $('delete-last-region').disabled = !state.added.length || !!state.mode;
    const active=activeIssue(),optionsReady=!!active && (!(active.options || []).length || !!state.resolutions[active.id]);
    $('confirm-space').disabled = !isEditable() || state.previewBusy || !!state.mode || !state.dirtyFields.length;
    $('resolve-issues').disabled=!isEditable() || state.previewBusy || !(state.dirtyFields.length || state.scopeEdgeIds.length || state.perimeterEdgeIds.length || state.perimeterDirty || state.supplementalEdges.length || state.portalClosureIds.length || state.portalSelectionDirty || state.scopeCandidateId || Object.keys(state.resolutions).length);
    $('preview-selection').disabled=!isEditable() || state.previewBusy || !(state.scopeEdgeIds.length || state.perimeterEdgeIds.length);
    $('undo-action').disabled=!isEditable() || state.previewBusy || !state.history.length;
    $('next-issue').disabled=state.issues.length<2;
    $('analyze-known-walls').disabled=!isEditable() || state.previewBusy || !knownWallEdgeIds().length;
    $('selection-count').textContent=`外围工作线组 ${state.perimeterEdgeIds.length} 段；另选原线 ${state.scopeEdgeIds.length} 段；逻辑补线 ${state.supplementalEdges.length} 段；最终采用 ${state.scopeReady?state.boundarySourceEdgeIds.length:0} 段`;
    document.querySelectorAll('input[data-issue-id]').forEach(input=>input.disabled=state.previewBusy);
    document.querySelectorAll('[data-testid="scope-candidate"],[data-testid="perimeter-candidate"],[data-testid="preview-gap-candidate"],[data-testid="adopt-door-candidate"]').forEach(control=>control.disabled=state.previewBusy);
    ['boundary','hole','entrance','exclusion'].forEach(type=>{$(`draw-${type}`).classList.toggle('active',state.mode===type);});
  }
  function selectedRing() {
    const value = $('edit-target').value;
    if (value === 'boundary') return state.boundary;
    const [kind,index] = value.split(':');
    if (kind === 'hole') return state.holes[Number(index)];
    if (kind === 'entrance') return state.entrances[Number(index)]?.boundary_mm;
    if (kind === 'exclusion') return state.exclusions[Number(index)]?.boundary_mm;
    return null;
  }
  function refreshEditTargets() {
    const selected = $('edit-target').value;
    $('edit-target').replaceChildren(new Option('不编辑顶点',''));
    if (state.boundary.length && state.requiredFields.includes('boundary')) $('edit-target').add(new Option('规划范围顶点','boundary'));
    state.holes.forEach((_,i)=>$('edit-target').add(new Option(`新增孔洞 ${i+1} 顶点`,`hole:${i}`)));
    state.entrances.forEach((_,i)=>$('edit-target').add(new Option(`入口 ${i+1} 顶点`,`entrance:${i}`)));
    state.exclusions.forEach((_,i)=>$('edit-target').add(new Option(`新增禁放区 ${i+1} 顶点`,`exclusion:${i}`)));
    if ([...$('edit-target').options].some(o=>o.value===selected)) $('edit-target').value=selected;
    renderVertices();
  }
  function renderVertices() {
    const group = $('vertex-layer'); group.replaceChildren();
    if (!isEditable() || state.mode) return;
    const points = selectedRing();
    if (!points) return;
    const radius = state.view.w / Math.max(400,svg.clientWidth)*5;
    points.forEach(([x,y],i)=>group.append(node('circle',{cx:x,cy:-y,r:radius,class:'vertex','data-vertex':i,'data-testid':`vertex-${i}`})));
  }
  function renderCAD() {
    const group = $('cad-layer'); group.replaceChildren();
    const drawing = state.result?.drawing || state.prepared?.drawing;
    if (!drawing) return;
    (drawing.lines || []).forEach(line=>{
      if (!finitePoint(line.start_mm) || !finitePoint(line.end_mm)) return;
      const role = String(line.role || '').toLowerCase();
      group.append(node('line',{x1:line.start_mm[0],y1:-line.start_mm[1],x2:line.end_mm[0],y2:-line.end_mm[1],class:role.includes('wall') || role.includes('barrier') ? 'cad-wall':'cad-line','data-source-handle':line.handle || ''}));
    });
    (drawing.polylines || []).forEach(poly=>{
      const role = String(poly.role || '').toLowerCase();
      const css = role.includes('hole') ? 'hole' : role.includes('exclusion') || role.includes('hatch') ? 'exclusion' : 'cad-line';
      const shape = node(poly.closed ? 'polygon':'polyline',{points:pointsAttr(ring(poly.points_mm)),class:css,fill:css==='cad-line'?'none':'#e88c9c33','data-source-handle':poly.handle || ''});
      // Source outlines stay unfilled: only the standard exclusion polygon
      // knows which HATCH loops are holes. Filling every loop would erase them.
      shape.style.fill='none';
      group.append(shape);
    });
    const bounds = drawing.bounds_mm;
    const fontSize = Array.isArray(bounds) ? Math.max(80,(bounds[2]-bounds[0])/170) : 120;
    (drawing.texts || []).forEach(text=>{
      if (finitePoint(text.position_mm)) group.append(node('text',{x:text.position_mm[0],y:-text.position_mm[1],'font-size':fontSize,class:'cad-text'},String(text.text || '')));
    });
  }
  function renderSpace() {
    const group = $('space-layer'); group.replaceChildren();
    const resultSpace = state.result?.space;
    const boundary = resultSpace?.boundary || {boundary_mm:state.boundary,holes_mm:[...state.fixedHoles,...state.holes]};
    if ((resultSpace || state.scopeReady) && (boundary.boundary_mm || []).length>=3) group.append(node('path',{d:pathForPolygon(boundary),class:'boundary','fill-rule':'evenodd','data-testid':'adopted-planning-boundary'}));
    (boundary.holes_mm || []).forEach(h=>appendPolygon(group,h,'hole'));
    const exclusions = resultSpace?.exclusions || [...state.fixedExclusions,...state.exclusions];
    exclusions.forEach(e=>group.append(node('path',{d:pathForPolygon(e),class:'exclusion','fill-rule':'evenodd'})));
    const entrances = resultSpace?.entrances || state.entrances;
    entrances.forEach(e=>group.append(node('path',{d:pathForPolygon(e),class:'entrance','fill-rule':'evenodd'})));
    const barriers = resultSpace?.barriers || state.barriers;
    barriers.forEach(w=>{
      if (finitePoint(w.start_mm) && finitePoint(w.end_mm)) group.append(node('line',{x1:w.start_mm[0],y1:-w.start_mm[1],x2:w.end_mm[0],y2:-w.end_mm[1],class:'barrier'}));
    });
    const draft = $('draft-layer');draft.replaceChildren();
    if (state.draft.length) {
      draft.append(node('polyline',{points:pointsAttr(state.draft),class:'draft'}));
      state.draft.forEach(([x,y])=>draft.append(node('circle',{cx:x,cy:-y,r:state.view.w/350,fill:'#277ca4'})));
    }
    if(isEditable()) [activeIssue()].filter(Boolean).forEach(issue=>{
      (issue.options || []).forEach(option=>{
        const endpoints=option.endpoints_mm || (option.start_mm && option.end_mm ? [option.start_mm,option.end_mm] : []);
        if(endpoints.length===2 && endpoints.every(finitePoint)) {
          draft.append(node('line',{x1:endpoints[0][0],y1:-endpoints[0][1],x2:endpoints[1][0],y2:-endpoints[1][1],class:'issue-gap','data-testid':'issue-gap'}));
          endpoints.forEach(([x,y])=>draft.append(node('circle',{cx:x,cy:-y,r:state.view.w/130,class:'issue-endpoint','data-testid':'issue-endpoint'})));
        }
      });
    });
    if(isEditable())state.feedback.flatMap(item=>item?.points_mm || []).filter(finitePoint).forEach(([x,y])=>draft.append(node('circle',{cx:x,cy:-y,r:state.view.w/180,class:'issue-endpoint','data-testid':'feedback-endpoint'})));
    renderVertices();renderSourceSelection();renderTopologyOverlay();updateEditorButtons();
  }
  function sourceEdges(){return state.prepared?.drawing?.edges || [];}
  function snapPoint(event){
    const matrix=svg.getScreenCTM();if(!matrix)return null;
    let closest=null,distance=14;
    const edges=sourceEdges().length?sourceEdges():(state.prepared?.drawing?.lines || []);
    const points=[...edges.flatMap(edge=>[edge.start_mm,edge.end_mm]),...(state.derivedTopology.nodes || []).map(node=>node.point_mm)];
    for(const point of points){
      if(!finitePoint(point))continue;
      const screen=new DOMPoint(point[0],-point[1]).matrixTransform(matrix);
      const d=Math.hypot(event.clientX-screen.x,event.clientY-screen.y);
      if(d<distance){distance=d;closest=[...point];}
    }
    return closest;
  }
  function renderSourceSelection(){
    sourceLayer.replaceChildren();snapLayer.replaceChildren();
    if(!isEditable())return;
    const size=state.view.w/Math.max(400,svg.clientWidth)*5;
    const ids=new Set(state.scopeEdgeIds),perimeterIds=new Set(state.perimeterEdgeIds),adoptedIds=new Set(state.scopeReady?state.boundarySourceEdgeIds:[]),activeIds=new Set(activeIssue()?.source_edge_ids || []);
    for(const edge of sourceEdges()){
      const a=edge.start_mm,b=edge.end_mm;if(!finitePoint(a)||!finitePoint(b))continue;
      const chosen=ids.has(edge.edge_id),perimeter=perimeterIds.has(edge.edge_id),adopted=adoptedIds.has(edge.edge_id),visible=chosen || perimeter || adopted || activeIds.has(edge.edge_id);
      if(visible || state.mode==='source' || !state.mode){
        const segment=node('line',{x1:a[0],y1:-a[1],x2:b[0],y2:-b[1],class:adopted?'source-adopted':perimeter?'source-perimeter':chosen?'source-selected':visible?'source-active':'source-hit','data-edge-id':edge.edge_id,'data-testid':'source-edge','data-source-use':adopted?'BOUNDARY':perimeter?'PERIMETER_WORK':chosen?'SELECTED':'SOURCE',tabindex:state.mode==='source'?0:-1,role:'button','aria-label':`原线 ${edge.edge_id}${adopted?'，最终边界采用':perimeter?'，外围工作线组':chosen?'，已选':''}`,'aria-pressed':String(chosen)});
        segment.style.pointerEvents=state.mode==='source'?'stroke':'none';
        segment.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();toggleSourceEdge(edge.edge_id);}});
        sourceLayer.append(segment);
        if(state.mode==='source' || !state.mode)sourceLayer.append(node('line',{x1:a[0],y1:-a[1],x2:b[0],y2:-b[1],class:'source-hit','data-edge-hit':edge.edge_id,'pointer-events':'stroke','aria-hidden':'true'}));
      }
      if(chosen || perimeter || adopted){
        [a,b].forEach(p=>sourceLayer.append(node('rect',{x:p[0]-size,y:-p[1]-size,width:size*2,height:size*2,class:'source-node','pointer-events':'none'})));
        sourceLayer.append(node('text',{x:(a[0]+b[0])/2,y:-(a[1]+b[1])/2-size*2,'font-size':size*3,class:'source-number','pointer-events':'none'},adopted?'✓':perimeter?'◇':String(state.scopeEdgeIds.indexOf(edge.edge_id)+1)));
      }
    }
    for(const edge of state.supplementalEdges)sourceLayer.append(node('line',{x1:edge.start_mm[0],y1:-edge.start_mm[1],x2:edge.end_mm[0],y2:-edge.end_mm[1],class:'source-supplement','pointer-events':'none','data-testid':'supplemental-edge'}));
    if(state.lineStart)snapLayer.append(node('circle',{cx:state.lineStart[0],cy:-state.lineStart[1],r:size*2,class:'source-node'}));
    if(state.snap){const [x,y]=state.snap;snapLayer.append(node('path',{d:`M${x-size*2},${-y}h${size*4}M${x},${-y-size*2}v${size*4}`,class:'snap-cross','data-testid':'snap-marker'}));snapLayer.append(node('text',{x:x+size*3,y:-y-size*3,'font-size':size*3,class:'source-number'},'吸附可信节点'));}
    $('select-source').setAttribute('aria-pressed',String(state.mode==='source'));
    $('connect-endpoints').setAttribute('aria-pressed',String(state.mode==='connect'));
  }
  function toggleSourceEdge(id){
    if(state.previewBusy || !isEditable())return;
    rememberAction();state.scopeCandidateId=null;const edge=sourceEdges().find(e=>e.edge_id===id);
    const chain=sourceEdges().filter(e=>edge?.source_handle && e.source_handle===edge.source_handle).map(e=>e.edge_id);
    const ids=chain.length?chain:[id],remove=ids.every(value=>state.scopeEdgeIds.includes(value));
    if(remove)state.scopeEdgeIds=state.scopeEdgeIds.filter(value=>!ids.includes(value));
    else state.scopeEdgeIds=[...new Set([...state.scopeEdgeIds,...ids])];
    state.perimeterDirty=true;state.scopeReady=false;state.boundary=[];state.boundarySourceEdgeIds=[];renderSpace();updateEditorButtons();
    // Selection can be assembled before validation; this does not publish geometry.
    setFeedback([{message:`${remove?'已取消':'已选择'}源实体 ${edge?.source_handle || id} 的 ${ids.length} 段边线。继续选择相邻实体，完成后检验；方框与编号表示已选。`}]);
  }
  function renderIssues() {
    $('issue-options').replaceChildren();$('issue-summary-list').replaceChildren();
    if(!state.issues.some(i=>i.id===state.activeIssueId))state.activeIssueId=state.issues[0]?.id || null;
    state.issues.forEach((issue,index)=>{
      const item=document.createElement('li'),link=document.createElement('a');
      link.href='#active-issue-title';link.dataset.issueId=issue.id;link.dataset.testid='issue-summary-link';
      link.textContent=`${index+1}. ${issueTitle(issue)}`;
      link.setAttribute('aria-current',String(issue.id===state.activeIssueId));
      link.addEventListener('click',event=>{event.preventDefault();focusIssue(issue.id,true);});
      item.append(link);$('issue-summary-list').append(item);
      const box=document.createElement('fieldset');box.className='issue-box';
      box.id=`issue-${issue.id}`;box.hidden=issue.id!==state.activeIssueId || !(issue.options || []).length;
      const legend=document.createElement('legend');legend.textContent=issue.message || issue.code;box.append(legend);
      (issue.options || []).forEach(option=>{
        const label=document.createElement('label'),input=document.createElement('input');
        input.type='radio';input.name=`issue-${issue.id}`;input.value=option.id;input.dataset.issueId=issue.id;input.dataset.testid='issue-option';
        input.checked=state.resolutions[issue.id]===option.id;input.disabled=state.previewBusy;
        input.addEventListener('change',()=>{rememberAction();state.resolutions[issue.id]=option.id;updateEditorButtons();previewInput();});
        label.append(input,document.createTextNode(option.label || option.id));box.append(label);
      });
      $('issue-options').append(box);
    });
    const issue=activeIssue(),field=issue?.field;
    $('issue-summary').hidden=!isEditable() || !state.issues.length;
    $('issue-summary-title').textContent=`还有 ${state.issues.length} 项必要信息；正在处理第 ${Math.max(1,state.issues.findIndex(i=>i.id===state.activeIssueId)+1)} 项`;
    $('geometry-editor').hidden=!issue?.requires_geometry || (field==='boundary' && (state.scopeCandidates.length>0 || state.perimeterCandidates.length>0));
    $('source-tools').hidden=field!=='boundary';
    $('resolve-issues').hidden=false;
    $('review-fields').hidden=true;
    reviewFields.forEach(field=>{$(`review-${field}`).closest('label').hidden=true;});
    const toolFields={boundary:'boundary',hole:'holes',entrance:'entrances',exclusion:'exclusions'};
    Object.entries(toolFields).forEach(([tool,toolField])=>{$(`draw-${tool}`).hidden=field!==toolField;});
    const handles=new Set(issue?.source_handles || (issue?.options || []).flatMap(o=>o.source_handle?[o.source_handle]:[]));
    [...$('cad-layer').children].forEach(n=>n.classList.toggle('issue-source',handles.has(n.getAttribute('data-source-handle'))));
    $('issue-position').textContent=issue?`本次 ${state.issues.findIndex(i=>i.id===issue.id)+1} / ${state.issues.length}`:'';
    $('active-issue-title').textContent=issue?issueTitle(issue):'必要信息已齐全';
    $('guide-why').textContent=issue?.message || '系统正在完成验证。';
    $('guide-done').textContent=`已保留 ${state.barriers.length} 条明确墙线、${state.fixedHoles.length} 个固定孔洞、${state.fixedExclusions.length} 个固定禁放区；此前局部补充继续保留。`;
    $('guide-now').textContent=typeof issue?.action==='string'?issue.action:field==='boundary'?'先选择图中已有外围线组，系统保留整组并定位剩余开口；只处理提示的局部事实，无需重新描绘整圈。':field==='entrances'?'在图上标出真实入口区域，结束绘制后系统立即检查。':(issue?.options || []).length?'查看高亮源图元，选择与现场事实一致的含义；选择后自动检查。':'在图上补充当前项目，结束绘制后自动检查。';
    $('guide-success').textContent=typeof issue?.pass_condition==='string'?issue.pass_condition:field==='boundary'?'整店外围依据明确，局部开口得到解释，最终区域闭合有效并保留孔洞和禁放约束；内部墙分支无需全部成为外围。':field==='entrances'?'入口区域有效且与真实门店范围相符。':'选择或几何必须能解释当前源图元，且通过服务端安全检查。';
    renderSourceSelection();renderTopologyOptions();
    updateEditorButtons();
  }
  let portalNumbers=new Map(),locatedPortalId=null,locatedPerimeterId=null,locatedLocalFaceId=null,locatedGapId=null;
  function portalNumber(id){if(!portalNumbers.has(id))portalNumbers.set(id,`D${String(portalNumbers.size+1).padStart(2,'0')}`);return portalNumbers.get(id);}
  function portalEvidence(candidate){
    const entries=Array.isArray(candidate.evidence)?candidate.evidence:[candidate.evidence];
    const facts=entries.filter(Boolean).flatMap(item=>typeof item==='string'?[item]:[item.handle?`图元 ${item.handle}`:'',item.text?`文字「${item.text}」`:'',item.block_name?`块「${item.block_name}」`:''].filter(Boolean));
    return facts.length?[...new Set(facts)].join('；'):'未提供可读门证据，请核对原图。';
  }
  function locatePortal(candidate){locatedPortalId=candidate.id;locateTopology(boundsFromPoints([candidate.start_mm,candidate.end_mm]));renderTopologyOptions();renderTopologyOverlay();}
  function updateTopologyData(value){
    if(Array.isArray(value.source_usage?.classified_as_walls))state.wallSourceIds=[...value.source_usage.classified_as_walls];
    if(Array.isArray(value.source_usage?.perimeter_edge_ids))state.perimeterEdgeIds=[...value.source_usage.perimeter_edge_ids];
    if(Array.isArray(value.local_faces))state.localFaces=value.local_faces;
    if(Array.isArray(value.perimeter_candidates))state.perimeterCandidates=value.perimeter_candidates;
    if(Array.isArray(value.gap_candidates))state.gapCandidates=value.gap_candidates;
    if(Array.isArray(value.scope_candidates))state.scopeCandidates=value.scope_candidates;
    if(Array.isArray(value.door_gap_candidates))state.doorGapCandidates=value.door_gap_candidates;
    if(Array.isArray(value.portal_closures))state.portalClosures=value.portal_closures;
    [...new Set([...state.doorGapCandidates,...state.portalClosures].map(item=>item.id))].sort().forEach(portalNumber);
    const topology=value.derived_topology || value.drawing?.derived_topology;
    if(topology)state.derivedTopology=topology;
    if(value.diagnostics!==undefined)state.topologyDiagnostics=value.diagnostics;
    if(Array.isArray(value.boundary_source_edge_ids))state.boundarySourceEdgeIds=value.boundary_source_edge_ids;
    if(typeof value.scope_ready==='boolean')state.scopeReady=value.scope_ready;
    if(value.scope_ready===false)state.boundary=[];
    if(Object.hasOwn(value,'candidate_space')){
      const boundary=value.candidate_space?.boundary;
      // A rejected or incomplete preview must not leave the previous local face
      // painted as the current planning region, even when another edit failed.
      if(!boundary || value.scope_ready===false){state.boundary=[];state.scopeReady=false;}
      else if(value.scope_ready!==false){state.boundary=ring(boundary.boundary_mm);state.scopeReady=true;}
    }
    if(!state.scopeReady)state.boundarySourceEdgeIds=[];
  }
  function candidateBoundary(candidate){return candidate.boundary?.boundary_mm || (Array.isArray(candidate.boundary)?candidate.boundary:[]);}
  function revealDrawing(block='center'){
    if(isEditable() && window.matchMedia('(min-width:851px)').matches)document.querySelector('.workspace').scrollIntoView({block:'start'});
    else svg.scrollIntoView({block});
  }
  function locateTopology(bounds,minSpan=1000){const fit=state.fit;fitBounds(bounds,minSpan);state.fit=fit;revealDrawing();}
  function chooseScopeCandidate(id){
    if(!isEditable() || state.previewBusy)return;
    const candidate=state.scopeCandidates.find(item=>item.id===id);if(!candidate)return;
    rememberAction();state.scopeCandidateId=id;state.mode=null;state.draft=[];
    locateTopology(candidate.bounds_mm || boundsFromPoints(candidateBoundary(candidate)));
    renderTopologyOptions();renderTopologyOverlay();previewInput();
  }
  function perimeterChosen(candidate){return (candidate.source_edge_ids || []).length>0 && candidate.source_edge_ids.every(id=>state.perimeterEdgeIds.includes(id));}
  function locatePerimeter(candidate,point=null){
    locatedPerimeterId=candidate.id;locatedLocalFaceId=null;locatedGapId=null;
    locateTopology(point?boundsFromPoints([point]):candidate.bounds_mm);
    renderTopologyOptions();renderTopologyOverlay();
  }
  function choosePerimeter(candidate){
    if(!isEditable() || state.previewBusy)return;
    rememberAction();const ids=new Set(candidate.source_edge_ids || []),chosen=perimeterChosen(candidate);
    state.perimeterEdgeIds=chosen?state.perimeterEdgeIds.filter(id=>!ids.has(id)):[...new Set([...state.perimeterEdgeIds,...ids])];
    state.perimeterDirty=true;state.scopeCandidateId=null;state.scopeReady=false;state.boundary=[];state.boundarySourceEdgeIds=[];state.mode=null;state.draft=[];
    locatePerimeter(candidate);renderSpace();previewInput();
  }
  function locateLocalFace(candidate){
    locatedLocalFaceId=candidate.id;locatedPerimeterId=null;locatedGapId=null;
    locateTopology(candidate.bounds_mm || boundsFromPoints(candidateBoundary(candidate)));renderTopologyOptions();renderTopologyOverlay();
  }
  function locateGap(candidate){
    locatedGapId=candidate.id;locatedLocalFaceId=null;locateTopology(boundsFromPoints([candidate.start_mm,candidate.end_mm]),Math.max(1,Math.min(1000,candidate.gap_mm*2 || 1000)));renderTopologyOptions();renderTopologyOverlay();
  }
  function previewGap(candidate){
    if(!isEditable() || state.previewBusy)return;
    const id=`gap-supplement-${candidate.id}`,existing=state.supplementalEdges.some(edge=>edge.id===id);
    rememberAction();state.supplementalEdges=existing?state.supplementalEdges.filter(edge=>edge.id!==id):[...state.supplementalEdges,{id,start_mm:[...candidate.start_mm],end_mm:[...candidate.end_mm],kind:'HUMAN_BOUNDARY_SUPPLEMENT'}];
    state.perimeterDirty=true;state.scopeCandidateId=null;state.scopeReady=false;state.boundary=[];state.mode=null;state.draft=[];
    locateGap(candidate);renderSpace();previewInput();
  }
  function knownWallEdgeIds(){
    const declared=new Set(state.wallSourceIds || state.prepared?.source_usage?.classified_as_walls || []);
    return sourceEdges().filter(edge=>edge.role==='WALL' || declared.has(edge.edge_id)).map(edge=>edge.edge_id);
  }
  function renderTopologyOptions(){
    const field=activeIssue()?.field,visible=isEditable() && ['boundary','entrances'].includes(field);
    const scopeBox=$('scope-candidates'),doorBox=$('door-candidates'),perimeterBox=$('perimeter-candidates'),localBox=$('local-faces'),gapBox=$('gap-candidates');
    for(const box of [scopeBox,doorBox,perimeterBox,localBox,gapBox])box.replaceChildren();
    if(visible && state.perimeterCandidates.length){
      const heading=document.createElement('h3');heading.textContent='先确定门店外围线组';perimeterBox.append(heading);
      const note=document.createElement('p');note.className='muted';note.textContent='开放线组可以先选入工作范围；选择会保留来源并列出局部开口，暂不摆放货架。可选多组，内部相接分支不要求全部成为外围。';perimeterBox.append(note);
      state.perimeterCandidates.forEach((candidate,index)=>{
        const selected=perimeterChosen(candidate),row=document.createElement('div');row.className=`perimeter-choice${locatedPerimeterId===candidate.id?' located':''}`;row.dataset.testid='perimeter-choice';row.dataset.perimeterId=candidate.id;
        const label=document.createElement('label'),input=document.createElement('input');input.type='checkbox';input.value=candidate.id;input.checked=selected;input.disabled=state.previewBusy;input.dataset.testid='perimeter-candidate';input.addEventListener('change',()=>choosePerimeter(candidate));
        label.append(input,document.createTextNode(`外围线组 ${index+1} · ${candidate.status==='CLOSED'?'几何已闭合':'尚有开口'}${selected?' · ◇ 已选工作线组':''}`));row.append(label);
        const reason=document.createElement('p');reason.textContent=candidate.reason || '这是带来源的外围线组；闭合形状本身不能证明门店身份。';row.append(reason);
        const sources=document.createElement('p');sources.className='perimeter-source-ids';sources.dataset.testid='perimeter-source-ids';sources.textContent=`原始源边：${(candidate.source_edge_ids || []).join('、') || '未提供'}`;row.append(sources);
        const logical=document.createElement('p');logical.dataset.testid='perimeter-logical-count';logical.textContent=`独立逻辑闭合段：${Array.isArray(candidate.logical_edge_ids)?candidate.logical_edge_ids.length:'未提供'}（不计为实体墙）`;row.append(logical);
        const locate=document.createElement('button');locate.type='button';locate.textContent='定位整组';locate.dataset.testid='locate-perimeter-candidate';locate.addEventListener('click',()=>locatePerimeter(candidate));row.append(locate);
        if(selected && (candidate.unresolved_endpoints || []).length){
          const help=document.createElement('p');help.className='muted';help.textContent=`开放端点 ${candidate.unresolved_endpoints.length} 个：先核对下方具体开口依据；端点是位置提示，并非每个都必须补线。`;row.append(help);
          candidate.unresolved_endpoints.forEach((endpoint,i)=>{const point=endpoint.point_mm;if(!finitePoint(point))return;const button=document.createElement('button');button.type='button';button.dataset.testid='locate-perimeter-endpoint';button.textContent=`定位端点 ${i+1}`;button.addEventListener('click',()=>locatePerimeter(candidate,point));row.append(button);});
        }
        perimeterBox.append(row);
      });
    }
    if(visible && state.gapCandidates.length){
      const heading=document.createElement('h3');heading.textContent='只处理选定外围的局部连接';gapBox.append(heading);
      const help=document.createElement('p');help.className='notice';help.textContent='以下不是已确认的门。先定位核对这两个原端点，再预览连接；只有确实属于规划边界时才应用。补线不成为实体墙，也不证明入口净空已具备。';gapBox.append(help);
      state.gapCandidates.forEach((candidate,index)=>{
        const row=document.createElement('div');row.className=`gap-choice${locatedGapId===candidate.id?' located':''}`;row.dataset.testid='gap-choice';row.dataset.gapId=candidate.id;
        const title=document.createElement('strong');title.textContent=`连接 ${index+1} · ${fmt(candidate.gap_mm,3)} mm`;row.append(title);
        const reason=document.createElement('p');reason.textContent=candidate.reason || '原线在此未相接，需要解释这个局部连接。';row.append(reason);
        const locate=document.createElement('button');locate.type='button';locate.dataset.testid='locate-gap-candidate';locate.textContent='定位两个原端点';locate.addEventListener('click',()=>locateGap(candidate));row.append(locate);
        const preview=document.createElement('button');preview.type='button';preview.dataset.testid='preview-gap-candidate';const selected=state.supplementalEdges.some(edge=>edge.id===`gap-supplement-${candidate.id}`);preview.textContent=selected?'撤销这段连接':'预览连接这两个原端点';preview.disabled=state.previewBusy;preview.setAttribute('aria-pressed',String(selected));preview.addEventListener('click',()=>previewGap(candidate));row.append(preview);gapBox.append(row);
      });
    }
    if(visible && state.scopeCandidates.length){
      const heading=document.createElement('h3');heading.textContent='具备整店依据的闭合范围';scopeBox.append(heading);
      const note=document.createElement('p');note.className='muted';note.textContent='只列出通过外围资格检查的区域；局部柱体和用途未明的小区域另列，不能在这里当成整店。';scopeBox.append(note);
      state.scopeCandidates.forEach((candidate,index)=>{
        const row=document.createElement('div');row.className='topology-choice';
        const label=document.createElement('label'),input=document.createElement('input');input.type='radio';input.name='scope-candidate';input.value=candidate.id;input.dataset.testid='scope-candidate';input.checked=state.scopeCandidateId===candidate.id;input.disabled=state.previewBusy;
        input.addEventListener('change',()=>chooseScopeCandidate(candidate.id));
        label.append(input,document.createTextNode(`区域 ${index+1} · ${fmt(candidate.area_mm2/1e6,2)} m²`));
        const locate=document.createElement('button');locate.type='button';locate.textContent='定位';locate.dataset.testid='locate-scope-candidate';locate.addEventListener('click',()=>locateTopology(candidate.bounds_mm || boundsFromPoints(candidateBoundary(candidate))));
        row.append(label,locate);scopeBox.append(row);
      });
    }
    if(visible && state.localFaces.length){
      const heading=document.createElement('h3');heading.textContent='局部闭合结构 · 不能作为整店范围';localBox.append(heading);
      state.localFaces.forEach((candidate,index)=>{
        const row=document.createElement('div');row.className=`local-face-choice${locatedLocalFaceId===candidate.id?' located':''}`;row.dataset.testid='local-face-choice';row.dataset.localFaceId=candidate.id;
        const text=document.createElement('p');text.textContent=`L${index+1} · ${candidate.kind==='FIXED_CONSTRAINT'?'已知固定禁放结构':'用途未明的局部闭合区'} · ${fmt(candidate.area_mm2/1e6,2)} m²。${candidate.reason || '闭合仅说明几何形状，不能证明这是门店。'}`;row.append(text);
        const locate=document.createElement('button');locate.type='button';locate.dataset.testid='locate-local-face';locate.textContent=`定位 L${index+1}`;locate.addEventListener('click',()=>locateLocalFace(candidate));row.append(locate);localBox.append(row);
      });
    }
    if(visible && (state.doorGapCandidates.length || state.portalClosures.length)){
      const heading=document.createElement('h3');heading.textContent='局部门与逻辑闭合';doorBox.append(heading);
      const note=document.createElement('p');note.className='muted';note.textContent='仅处理原图已有门候选；逻辑闭合用于范围拓扑，不成为实体墙。缺少完整范围时也可处理。';doorBox.append(note);
      for(const candidate of state.doorGapCandidates){
        const row=document.createElement('div');row.className=`portal-choice${locatedPortalId===candidate.id?' located':''}`;row.dataset.testid='portal-choice';row.dataset.portalId=candidate.id;
        const automatic=candidate.confidence==='HIGH',chosen=state.portalClosureIds.includes(candidate.id),adoptedAutomatic=state.portalClosures.some(portal=>portal.id===candidate.id && portal.automatic===true);
        const title=document.createElement('strong');title.textContent=`${portalNumber(candidate.id)} · `+(automatic?(adoptedAutomatic?'明确门证据 · 后端自动采用':'明确门证据 · 由后端核验采用'):chosen?'门候选 · 本次已选择':'门候选 · 需要明确含义');row.append(title);
        if(finitePoint(candidate.start_mm)&&finitePoint(candidate.end_mm)){
          const dimensions=document.createElement('p');dimensions.className='portal-dimensions';dimensions.textContent=`跨度 ${fmt(Math.hypot(candidate.end_mm[0]-candidate.start_mm[0],candidate.end_mm[1]-candidate.start_mm[1]),1)} mm；A (${candidate.start_mm.join(', ')}) → B (${candidate.end_mm.join(', ')}) mm`;row.append(dimensions);
        }
        const evidence=document.createElement('p');evidence.className='portal-evidence';evidence.textContent=`源证据：${portalEvidence(candidate)}`;row.append(evidence);
        const reason=document.createElement('p');reason.textContent=candidate.reason || '请结合原图门证据核对该局部开口。';row.append(reason);
        const actions=document.createElement('div'),locate=document.createElement('button');locate.type='button';locate.textContent=`定位 ${portalNumber(candidate.id)}`;locate.dataset.testid='locate-door-candidate';locate.dataset.portalId=candidate.id;locate.setAttribute('aria-pressed',String(locatedPortalId===candidate.id));locate.addEventListener('click',()=>locatePortal(candidate));actions.append(locate);
        if(!automatic){const adopt=document.createElement('button');adopt.type='button';adopt.textContent=chosen?'撤销此逻辑闭合':'这是门，采用此逻辑闭合';adopt.dataset.testid='adopt-door-candidate';adopt.dataset.portalId=candidate.id;adopt.disabled=state.previewBusy;adopt.setAttribute('aria-pressed',String(chosen));adopt.addEventListener('click',()=>{if(state.previewBusy)return;rememberAction();state.portalSelectionDirty=true;state.scopeCandidateId=null;state.portalClosureIds=chosen?state.portalClosureIds.filter(id=>id!==candidate.id):[...state.portalClosureIds,candidate.id];renderTopologyOptions();renderTopologyOverlay();previewInput();});actions.append(adopt);}
        row.append(actions);doorBox.append(row);
      }
      const total=document.createElement('p');total.className='muted';total.textContent=`当前逻辑闭合 ${state.portalClosures.length} 段（含自动采用），不计入墙数量。`;doorBox.append(total);
    }
    const diagnostics=Array.isArray(state.topologyDiagnostics)?state.topologyDiagnostics:Object.entries(state.topologyDiagnostics || {}).map(([code,value])=>({code,message:typeof value==='string'?value:`${({cuts:'内部切边',dangles:'悬挂或附着尾段',invalid:'几何诊断'})[code] || code}：${Array.isArray(value)?value.length:'已有'} 项（只作诊断）`}));
    $('topology-diagnostic-list').replaceChildren();for(const item of diagnostics){const p=document.createElement('p');p.textContent=typeof item==='string'?item:item.message || item.code || '';if(p.textContent)$('topology-diagnostic-list').append(p);}
    for(const component of state.topologyDiagnostics?.open_components || []){
      const box=document.createElement('div');box.dataset.testid='open-component';box.dataset.componentId=component.id;
      const sourceIds=new Set(component.source_edge_ids || []),handles=[...new Set(sourceEdges().filter(edge=>sourceIds.has(edge.edge_id)).map(edge=>edge.source_handle || edge.edge_id))];
      const description=document.createElement('p');description.textContent=`未闭合线组：源 ${handles.length?handles.join('、'):[...sourceIds].join('、')}，开放端点 ${(component.dangling_points_mm || []).length} 个。此项只作诊断，尚未认定为门店外围，也未自动补线。`;
      const locate=document.createElement('button');locate.type='button';locate.textContent='定位线组';locate.dataset.testid='locate-open-component';locate.addEventListener('click',()=>locateTopology(component.bounds_mm));box.append(description,locate);$('topology-diagnostic-list').append(box);
    }
    $('topology-diagnostics').hidden=!visible || !diagnostics.length;
    $('topology-options').hidden=!visible || !(scopeBox.children.length || doorBox.children.length || perimeterBox.children.length || localBox.children.length || gapBox.children.length || diagnostics.length);
  }
  function renderTopologyOverlay(){
    topologyLayer.replaceChildren();focusLayer.replaceChildren();if(!isEditable())return;
    const size=state.view.w/Math.max(svg.clientWidth,400)*13;
    state.localFaces.forEach((candidate,index)=>{
      const points=candidateBoundary(candidate);if(points.length<3)return;
      const located=candidate.id===locatedLocalFaceId;
      topologyLayer.append(node('path',{d:pathForPolygon({boundary_mm:points,holes_mm:candidate.boundary?.holes_mm || []}),class:`local-face${located?' located':''}`,'fill-rule':'evenodd','data-testid':'local-face-shape','data-local-face-id':candidate.id,'pointer-events':'none'}));
      if(located)topologyLayer.append(node('text',{x:points[0][0],y:-points[0][1]-size,'font-size':size,class:'source-number','pointer-events':'none'},`L${index+1} · 局部结构，非整店`));
    });
    state.perimeterCandidates.forEach((candidate,index)=>{
      const selected=perimeterChosen(candidate),located=candidate.id===locatedPerimeterId,group=node('g',{'data-testid':'perimeter-candidate-shape','data-perimeter-id':candidate.id,'pointer-events':'none'});
      for(const segment of candidate.segments || []){
        const a=segment.start_mm,b=segment.end_mm;if(!finitePoint(a)||!finitePoint(b))continue;
        group.append(node('line',{x1:a[0],y1:-a[1],x2:b[0],y2:-b[1],class:`perimeter-option${selected?' selected':''}${located?' located':''}`,'data-derived-edge-id':segment.id}));
      }
      if(selected || located)for(const [i,endpoint] of (candidate.unresolved_endpoints || []).entries()){
        const p=endpoint.point_mm;if(!finitePoint(p))continue;
        group.append(node('circle',{cx:p[0],cy:-p[1],r:size*.45,class:'perimeter-endpoint','data-testid':'perimeter-endpoint'}));
        group.append(node('text',{x:p[0]+size*.65,y:-p[1]-size*.65,'font-size':size,class:'source-number'},`线组${index+1} · 端点${i+1}`));
      }
      topologyLayer.append(group);
    });
    for(const [index,candidate] of state.gapCandidates.entries()){
      if(candidate.id!==locatedGapId)continue;
      const a=candidate.start_mm,b=candidate.end_mm;if(!finitePoint(a)||!finitePoint(b))continue;
      // Keep small markers and glyphs in local screen units: sub-millimeter
      // radii at large CAD coordinates otherwise lose browser paint precision.
      // The transform still projects the exact two source points; no offset or
      // snapping is applied to either CAD endpoint.
      const matrix=svg.getScreenCTM(),scale=matrix?Math.hypot(matrix.a,matrix.b):1;
      const end=[(b[0]-a[0])*scale,-(b[1]-a[1])*scale];
      const group=node('g',{'data-testid':'located-gap','data-gap-id':candidate.id,'data-start-mm':JSON.stringify(a),'data-end-mm':JSON.stringify(b),transform:`translate(${a[0]} ${-a[1]}) scale(${1/scale})`,'pointer-events':'none'});
      group.append(node('line',{x1:0,y1:0,x2:end[0],y2:end[1],class:'local-gap-preview'}));
      group.append(node('rect',{x:-5,y:-5,width:10,height:10,class:'portal-focus-endpoint'}));
      group.append(node('circle',{cx:end[0],cy:end[1],r:5,class:'portal-focus-endpoint'}));
      [[0,0],end].forEach((p,i)=>group.append(node('text',{x:p[0]+10,y:p[1]+(i?20:-14),'font-size':13,class:'source-number'},`连接${index+1} ${i?'B ○':'A □'}`)));
      focusLayer.append(group);
    }
    for(const [index,candidate] of state.scopeCandidates.entries()){
      const points=candidateBoundary(candidate),selected=candidate.id===state.scopeCandidateId;
      if(points.length<3)continue;
      const polygon=node('path',{d:pathForPolygon({boundary_mm:points,holes_mm:candidate.boundary?.holes_mm || []}),class:`scope-option${selected?' chosen':''}`,'fill-rule':'evenodd','data-testid':'scope-candidate-shape','data-scope-candidate':candidate.id,'pointer-events':state.mode?'none':'visiblePainted',role:'button',tabindex:state.mode?-1:0,'aria-label':`选择范围候选 ${index+1}`});
      polygon.addEventListener('click',event=>{event.stopPropagation();if(!state.pan)chooseScopeCandidate(candidate.id);});polygon.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();chooseScopeCandidate(candidate.id);}});topologyLayer.append(polygon);
      topologyLayer.append(node('text',{x:points[0][0],y:-points[0][1],'font-size':size,class:'source-number','pointer-events':'none'},`区域 ${index+1}${selected?' ✓':''}`));
    }
    const closures=new Map(state.portalClosures.map(portal=>[portal.id,portal]));
    for(const candidate of state.doorGapCandidates)if(!closures.has(candidate.id))closures.set(candidate.id,{...candidate,pending:!state.portalClosureIds.includes(candidate.id)});
    for(const portal of closures.values()){
      if(!finitePoint(portal.start_mm)||!finitePoint(portal.end_mm))continue;
      topologyLayer.append(node('line',{x1:portal.start_mm[0],y1:-portal.start_mm[1],x2:portal.end_mm[0],y2:-portal.end_mm[1],class:`portal-closure${portal.pending?' pending':''}`,'data-testid':'portal-closure','data-portal-id':portal.id,'pointer-events':'none'}));
      const focused=portal.id===locatedPortalId;
      topologyLayer.append(node('text',{x:(portal.start_mm[0]+portal.end_mm[0])/2,y:-(portal.start_mm[1]+portal.end_mm[1])/2-size,'font-size':size,class:'source-number','pointer-events':'none'},`${portalNumber(portal.id)} ${portal.pending?'◇ 门候选':'◇ 逻辑闭合 · 非墙'}${focused?' ◉ 当前定位':''}`));
      if(focused){
        const a=portal.start_mm,b=portal.end_mm,length=Math.hypot(b[0]-a[0],b[1]-a[1]) || 1,dx=-(b[1]-a[1])/length*size*.3,dy=(b[0]-a[0])/length*size*.3;
        const group=node('g',{'data-testid':'located-portal','data-portal-id':portal.id,'pointer-events':'none'});
        for(const sign of [-1,1])group.append(node('line',{x1:a[0]+sign*dx,y1:-(a[1]+sign*dy),x2:b[0]+sign*dx,y2:-(b[1]+sign*dy),class:'portal-focus-rail'}));
        group.append(node('rect',{x:a[0]-size*.45,y:-a[1]-size*.45,width:size*.9,height:size*.9,class:'portal-focus-endpoint'}));
        group.append(node('circle',{cx:b[0],cy:-b[1],r:size*.45,class:'portal-focus-endpoint'}));
        [a,b].forEach((point,index)=>group.append(node('text',{x:point[0]+size*.7,y:-point[1]+size,'font-size':size,class:'source-number'},`${portalNumber(portal.id)} ${index?'B ○':'A □'}`)));
        topologyLayer.append(group);
      }
    }
  }
  function activeIssue(){return state.issues.find(i=>i.id===state.activeIssueId) || null;}
  function isInputErrorIssue(issue){return String(issue.code || '').startsWith('PREVIEW_') || String(issue.id || '').startsWith('preview-invalid') || issue.id==='validation-failed';}
  function issueTitle(issue){const handles=(issue.source_handles || []).join('、');return handles && issue.field==='exclusions'?`源图元 ${handles} 的用途`:({boundary:'实际门店范围',entrances:'入口位置',exclusions:'源图元与禁放区域',holes:'孔洞',barriers:'墙与阻挡'})[issue.field] || issue.message || '图纸事实';}
  function issueBounds(issue){
    if(Array.isArray(issue?.bounds_mm) && issue.bounds_mm.length===4)return issue.bounds_mm;
    const ids=new Set(issue?.source_edge_ids || []),handles=new Set(issue?.source_handles || []);
    const points=(state.prepared?.drawing?.edges || []).filter(e=>ids.has(e.edge_id)||handles.has(e.source_handle)).flatMap(e=>[e.start_mm,e.end_mm]);
    (issue?.options || []).forEach(o=>points.push(...(o.endpoints_mm || [])));
    return boundsFromPoints(points) || state.prepared?.drawing?.bounds_mm;
  }
  function focusIssue(id,locate=false){
    if(!state.issues.some(i=>i.id===id))return;
    state.issueDrafts=state.issueDrafts || {};
    if(state.activeIssueId!==id){
      if(state.mode && (state.draft.length || state.lineStart))state.issueDrafts[state.activeIssueId]={mode:state.mode,draft:state.draft.map(point=>[...point]),lineStart:state.lineStart?[...state.lineStart]:null};
      const saved=state.issueDrafts[id];state.mode=saved?.mode || null;state.draft=saved?.draft?.map(point=>[...point]) || [];state.lineStart=saved?.lineStart?[...saved.lineStart]:null;state.snap=null;
    }
    state.activeIssueId=id;
    renderIssues();renderSpace();
    if(locate){const fit=state.fit;fitBounds(issueBounds(activeIssue()));state.fit=fit;$('active-issue-title').focus({preventScroll:true});revealDrawing('nearest');}
  }
  function rememberAction(){
    const fields=['boundary','holes','entrances','exclusions','scopeEdgeIds','perimeterEdgeIds','perimeterDirty','scopeReady','boundarySourceEdgeIds','supplementalEdges','resolutions','dirtyFields','issues','activeIssueId','issueDrafts','scopeCandidateId','portalClosureIds','portalSelectionDirty'];
    state.history.push(JSON.parse(JSON.stringify(Object.fromEntries(fields.map(key=>[key,state[key]])))));
    state.history.at(-1).savedFacts=JSON.parse(JSON.stringify({resolutions:state.appliedResolutions,portal_closure_ids:state.appliedPortalClosureIds || [],boundary_work:state.appliedBoundaryWork || boundaryWork({})}));
  }
  function setFeedback(entries,valid=true){
    state.feedback=Array.isArray(entries)?entries:[entries];
    $('issue-feedback').replaceChildren();$('issue-feedback').hidden=!state.feedback.length;
    $('issue-feedback').classList.toggle('invalid',!valid);
    for(const item of state.feedback){const p=document.createElement('p');p.textContent=typeof item==='string'?item:item?.message || item?.code || '';if(p.textContent)$('issue-feedback').append(p);}
  }
  function previewPayload(){
    const payload={resolutions:Object.entries(state.resolutions).map(([issue_id,option_id])=>({issue_id,option_id}))};
    payload.portal_closure_ids=[...state.portalClosureIds];payload.scope_candidate_id=state.scopeCandidateId;
    payload.perimeter_edge_ids=[...state.perimeterEdgeIds];
    if(state.revokedIssueIds.length)payload.revoked_issue_ids=[...state.revokedIssueIds];
    payload.scope_edge_ids=[...state.scopeEdgeIds];
    payload.supplemental_edges=state.supplementalEdges;
    if(state.dirtyFields.includes('boundary') || state.dirtyFields.includes('holes'))payload.boundary={boundary_mm:closed(state.boundary),holes_mm:[...state.fixedHoles,...state.holes].map(closed)};
    if(state.dirtyFields.includes('entrances'))payload.entrances=state.entrances.map(e=>({...e,boundary_mm:closed(e.boundary_mm),holes_mm:(e.holes_mm || []).map(closed)}));
    if(state.dirtyFields.includes('exclusions'))payload.exclusions=state.exclusions.map(e=>({...e,boundary_mm:closed(e.boundary_mm),holes_mm:(e.holes_mm || []).map(closed)}));
    return payload;
  }
  function boundaryWork(usage=null){
    const value=usage || {perimeter_edge_ids:state.perimeterEdgeIds,scope_edge_ids:state.scopeEdgeIds,supplemental_edges:state.supplementalEdges,scope_candidate_id:state.scopeCandidateId};
    return JSON.parse(JSON.stringify({perimeter_edge_ids:value.perimeter_edge_ids || [],scope_edge_ids:value.scope_edge_ids || value.selected_scope_edge_ids || [],supplemental_edges:value.supplemental_edges || [],scope_candidate_id:value.scope_candidate_id || null}));
  }
  async function previewInput(apply=false,undoFacts=null){
    if(!isEditable() || state.previewBusy)return;
    const job=state.job,version=++state.previewVersion;state.previewBusy=true;updateEditorButtons();showError(null);
    setFeedback([{message:'正在检查本次局部操作；其余内容保留。'}]);
    try{
      if(undoFacts){
        const options={method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(undoFacts)};
        const checked=await api(`/api/v2/jobs/${encodeURIComponent(job)}/preview-input`,options);
        if(checked.valid===false)throw new Error((checked.feedback || []).map(item=>item.message || item.code).join('；') || '撤销尚未通过检查，已保存事实未改变。');
        const saved=await api(`/api/v2/jobs/${encodeURIComponent(job)}/apply-input`,options);
        if(job!==state.job)return;
        if(saved.valid===false)throw new Error('撤销未保存，请按当前错误提示修正。');
        state.appliedResolutions=saved.applied_resolutions || undoFacts.resolutions;
        state.appliedPortalClosureIds=[...(saved.source_usage?.portal_closure_ids || undoFacts.portal_closure_ids)];
        state.appliedBoundaryWork=boundaryWork(saved.source_usage || undoFacts);
        state.revokedIssueIds=[];
      }
      const payload=previewPayload();
      let value=await api(`/api/v2/jobs/${encodeURIComponent(job)}/preview-input`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
      if(job!==state.job || version!==state.previewVersion)return;
      updateTopologyData(value);
      if(apply && value.valid!==false){
        const applied=await api(`/api/v2/jobs/${encodeURIComponent(job)}/apply-input`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
        if(job!==state.job)return;
        if(applied.ready!==undefined)value=applied;updateTopologyData(applied);
        if(Array.isArray(applied.applied_resolutions))state.appliedResolutions=applied.applied_resolutions;
        state.appliedPortalClosureIds=[...(applied.source_usage?.portal_closure_ids || payload.portal_closure_ids)];
        state.appliedBoundaryWork=boundaryWork(applied.source_usage || payload);
        if(state.history.length)state.history.at(-1).persistedAction=true;
        state.revokedIssueIds=[];state.portalSelectionDirty=false;state.perimeterDirty=false;
      }
      setFeedback(value.feedback || [{message:value.valid?'本次补充已通过局部检查。':'本次补充尚未通过，请按图上提示修正。'}],value.valid!==false);
      if(value.valid===false){
        const incoming=Array.isArray(value.issues)?value.issues:[];
        const invalid=incoming.filter(isInputErrorIssue);
        const invalidFields=new Set(invalid.map(issue=>issue.field));
        const kept=state.issues.filter(issue=>!isInputErrorIssue(issue) && !(invalidFields.has(issue.field)&&issue.requires_geometry));
        const needed=incoming.filter(issue=>isInputErrorIssue(issue) || !(invalidFields.has(issue.field)&&issue.requires_geometry));
        state.issues=[...new Map([...kept,...needed].map(issue=>[issue.id,issue])).values()];
        const target=invalid[0] || incoming.find(issue=>issue.field===activeIssue()?.field) || incoming[0];
        const message=(value.feedback || []).map(item=>item.message || item.code).join('；') || target?.message || '本次局部操作未通过，请修正当前问题。';
        showError(message);setFeedback(value.feedback || [{message}],false);
        refreshEditTargets();if(target)focusIssue(target.id,true);else{renderIssues();renderSpace();}
        return;
      }
      state.issues=state.issues.filter(issue=>!isInputErrorIssue(issue));
      if(!apply && Array.isArray(value.issues))state.issues=[...new Map([...state.issues,...value.issues].map(issue=>[issue.id,issue])).values()];
      if(apply && Array.isArray(value.issues))state.issues=value.issues;
      if(value.candidate_space?.boundary && !state.dirtyFields.includes('boundary'))state.boundary=ring(value.candidate_space.boundary.boundary_mm);
      if(value.candidate_space?.barriers)state.barriers=value.candidate_space.barriers;
      if(value.candidate_space?.exclusions && !state.dirtyFields.includes('exclusions'))state.fixedExclusions=value.candidate_space.exclusions;
      if(value.candidate_space?.entrances && !state.dirtyFields.includes('entrances'))state.entrances=value.candidate_space.entrances;
      if(value.ready && apply){
        if(!value.confirm_payload)throw new Error('检查结果缺少可提交的标准补充，请重试。');
        await api(`/api/v2/jobs/${encodeURIComponent(job)}/confirm`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...value.confirm_payload,user_confirmed:true})});
        if(job!==state.job)return;state.status='planning';$('editor-panel').hidden=true;$('issue-summary').hidden=true;await poll();
      }else{renderIssues();renderSpace();if(apply)focusIssue(state.activeIssueId || state.issues[0]?.id,true);else if(value.valid!==false)setFeedback([...(value.feedback || []),{message:'这是局部预览，尚未保存。点击“应用并检查”保存明确事实；全部条件通过后自动继续。'}]);}
      if(value.valid===false)showError((value.feedback || []).map(item=>item.message || item.code).join('；') || '本次局部操作未通过，请修正当前问题。');
    }catch(error){if(job===state.job)showError(error.message);}
    finally{
      if(job===state.job){
        // Error summaries above the workspace change page height. Let layout
        // and scroll anchoring settle before exposing the next local action.
        if(isEditable())await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>{
          if(job===state.job && version===state.previewVersion && isEditable())revealDrawing();
          resolve();
        })));
        if(job===state.job){state.previewBusy=false;updateEditorButtons();}
      }
    }
  }
  function beginDrawing(type) {
    if (!isEditable() || state.previewBusy) return;
    if(state.issueDrafts)delete state.issueDrafts[state.activeIssueId];
    state.mode=type;state.draft=[];state.pan=false;
    $('pan-mode').setAttribute('aria-pressed','false');$('edit-target').value='';
    const names={boundary:'规划范围',hole:'孔洞',entrance:'入口',exclusion:'禁放区'};
    $('drawing-hint').textContent=`正在绘制${names[type]}：点击图上空白处添加顶点（至少 3 点），再结束绘制。滚轮仍可缩放；需要移动图纸时启用“平移”。`;
    renderSpace();revealDrawing();
  }
  function finishDrawing() {
    if (!state.mode || state.draft.length<3) return;
    rememberAction();const field={boundary:'boundary',hole:'holes',entrance:'entrances',exclusion:'exclusions'}[state.mode];
    if(!state.dirtyFields.includes(field))state.dirtyFields.push(field);
    const points=state.draft.map(p=>[...p]);
    if (state.mode==='boundary') state.boundary=points;
    else if (state.mode==='hole') {state.holes.push(points);state.added.push({kind:'hole',value:points});}
    else {
      const value={id:`human-${state.mode}-${++state.regionCounter}`,boundary_mm:points,holes_mm:[]};
      const list=state.mode==='entrance'?state.entrances:state.exclusions;
      list.push(value);state.added.push({kind:state.mode,value});
    }
    state.mode=null;state.draft=[];
    if(state.issueDrafts)delete state.issueDrafts[state.activeIssueId];
    $('drawing-hint').textContent='几何已更新。可选择对象拖动顶点，或继续标记孔洞、入口和禁放区；拓扑有效性由服务端检查。';
    invalidateReview();refreshEditTargets();renderSpace();previewInput();
  }
  function initPrepared(prepared) {
    portalNumbers=new Map();locatedPortalId=null;locatedPerimeterId=null;locatedLocalFaceId=null;locatedGapId=null;
    state.appliedPortalClosureIds=[...(prepared.source_usage?.portal_closure_ids || [])];state.wallSourceIds=[...(prepared.source_usage?.classified_as_walls || [])];
    $('source-selection-help').textContent='先从已有外围线组选定工作范围，无需逐节点重画。◇ 虚线表示工作线组，✓ 实线才是最终采用边；局部闭合柱体只可定位。缺口按原端点局部处理，逻辑补线不成为墙。';
    state.prepared=prepared;state.result=null;state.added=[];state.draft=[];state.mode=null;state.issueDrafts={};
    state.regionCounter=0;state.pan=false;
    $('pan-mode').setAttribute('aria-pressed','false');svg.style.cursor='crosshair';
    const space=prepared.candidate_space;
    state.issues=prepared.issues || [];state.resolutions={};state.appliedResolutions=prepared.evidence?.applied_resolutions || [];state.revokedIssueIds=[];
    for(const record of prepared.evidence?.applied_resolutions || [])state.resolutions[record.issue_id]=record.option_id;
    state.originalIssues=state.issues;state.activeIssueId=state.issues[0]?.id || null;const usage=prepared.source_usage || {};state.scopeEdgeIds=[...(usage.scope_edge_ids || usage.selected_scope_edge_ids || [])];state.perimeterEdgeIds=[...(usage.perimeter_edge_ids || [])];state.perimeterDirty=false;state.scopeReady=false;state.localFaces=[];state.perimeterCandidates=[];state.gapCandidates=[];state.supplementalEdges=usage.supplemental_edges || [];state.scopeCandidateId=usage.scope_candidate_id || null;state.appliedBoundaryWork=boundaryWork(usage);state.portalClosureIds=[...(usage.portal_closure_ids || [])];state.portalSelectionDirty=false;state.scopeCandidates=[];state.portalClosures=[];state.doorGapCandidates=[];state.derivedTopology={nodes:[],edges:[]};state.topologyDiagnostics=[];state.boundarySourceEdgeIds=[];updateTopologyData(prepared);state.history=[];state.dirtyFields=[];state.previewBusy=false;state.previewVersion++;state.snap=null;state.lineStart=null;setFeedback([]);
    state.requiredFields=(prepared.evidence?.required_review_fields || state.issues.filter(i=>i.requires_geometry).map(i=>i.field)).filter(f=>reviewFields.includes(f));
    state.boundary=state.scopeReady?ring(space?.boundary?.boundary_mm):[];
    // Explicit CAD PLANNING_HOLE rings are immutable. HATCH internal holes
    // remain inside fixed_exclusions and are never promoted to floor holes.
    state.fixedHoles=(prepared.evidence?.explicit_holes || space?.boundary?.holes_mm || []).map(ring);
    state.holes=[];
    state.entrances=(space?.entrances || []).map(e=>({...e,boundary_mm:ring(e.boundary_mm),holes_mm:(e.holes_mm || []).map(ring)}));
    state.exclusions=[];state.fixedExclusions=prepared.evidence?.fixed_exclusions || space?.exclusions || [];
    state.barriers=prepared.evidence?.barriers || space?.barriers || [];
    $('candidate-note').textContent=state.issues.length ? '只处理下列尚缺事实；原图中相关线段或端点会高亮，提交后系统自动继续。' : prepared.readiness==='REUSED_CONFIRMATION' ? '已核验并复用同份图纸及配置的既有补充，无需重复操作。' : '图纸必要事实已验证，系统自动继续。';
    if(prepared.evidence?.partial_reuse==='REUSED_VALIDATED_SOURCE_FACTS')$('candidate-note').textContent='已复用这份图纸此前有效的局部事实；已保存外围线组和补线按当前源版本恢复，只需处理剩余问题。';
    const summary=prepared.business_summary || {};
    $('business-summary').textContent=`自动加载 ${fmt(summary.product_count)} 条商品记录、${fmt(summary.template_count)} 种货架配置。图纸固定孔洞 ${state.fixedHoles.length} 个，固定墙线 ${state.barriers.length} 条，固定禁放区 ${state.fixedExclusions.length} 个；原图明确对象不能在此删除或改形。`;
    $('source-evidence').textContent=JSON.stringify({source:prepared.source,evidence:prepared.evidence},null,2);
    $('evidence-panel').hidden=false;$('empty-message').hidden=true;
    $('shelf-layer').replaceChildren();$('run-layer').replaceChildren();$('shelf-detail').hidden=true;
    renderCAD();renderIssues();invalidateReview();refreshEditTargets();renderSpace();
    fitBounds(prepared.drawing?.bounds_mm || boundsFromPoints(state.boundary));
    if(isEditable())requestAnimationFrame(()=>revealDrawing());
  }
  function renderResult(result) {
    if(result.task_purpose==='existing_design'){renderExisting(result);return;}
    state.result=result;state.mode=null;state.draft=[];
    observedLayer.replaceChildren();document.querySelectorAll('.observed-legend').forEach(element=>element.hidden=true);
    $('editor-panel').hidden=true;$('result-panel').hidden=false;$('shelf-detail').hidden=true;
    $('issue-summary').hidden=true;$('result-title').textContent='自动规划结果';$('existing-omissions').hidden=true;$('comparison-body').closest('section').hidden=false;document.querySelector('.sku-status').hidden=false;
    $('bom-body').closest('section').querySelector('h2').textContent='设计级货架清单';$('bom-body').closest('section').querySelector('p').textContent='清单来自最终模块实例；默认层数不是商品净高、库存或采购可得性。';
    $('download-json').textContent='下载布局 JSON';$('download-xlsx').textContent='下载物料 XLSX';
    $('viewer-3d-panel').querySelector('p').textContent='左键旋转 · 滚轮缩放 · 右键平移 · 点击货架查看规格。货架显示高 1800 mm；墙显示高 2600 mm、厚 100 mm，仅为显示假设。默认层数按物料配置显示。';
    $('canvas-title').textContent='自动连续货架排 · 二维布局';$('canvas-badge').textContent='规划完成';
    $('delivery-note').textContent=state.prepared?.readiness==='REUSED_CONFIRMATION'
      ? '已核验并复用同份图纸及配置的既有补充，无需重复操作。方案几何与物料一致性检查通过。'
      : '方案几何与物料一致性检查通过。可直接查看三维、二维并下载布局和设计级物料清单。';
    renderSpace();$('run-layer').replaceChildren();$('shelf-layer').replaceChildren();
    (result.runs || []).forEach(r=>appendPolygon($('run-layer'),r.footprint_mm || [],'run'));
    (result.shelves || []).forEach(s=>{
      const element=node('polygon',{points:pointsAttr(s.footprint_mm || []),class:'shelf','data-shelf':s.id,'data-testid':'shelf-module',tabindex:0,role:'button','aria-label':`${s.id} ${s.length_mm}乘${s.depth_mm}毫米货架`});
      element.append(node('title',{},`${s.id} · ${s.length_mm} × ${s.depth_mm} mm · 默认 ${s.default_level_count} 层`));
      element.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();selectShelf(s.id);}});
      $('shelf-layer').append(element);
    });
    const shelfCount=(result.shelves || []).length;
    $('result-metrics').replaceChildren();
    [['货架模块',shelfCount],['连续货架排',(result.runs || []).length],['基础过道 mm',result.rules?.aisle_width_mm],['品类分组',(result.products?.category_assignments || []).length]].forEach(([label,value])=>{
      const box=document.createElement('div');box.textContent=label;const strong=document.createElement('strong');strong.textContent=fmt(value);box.append(strong);$('result-metrics').append(box);
    });
    $('bom-body').replaceChildren();let total=0,levels=0;
    (result.bom || []).forEach(row=>{
      const tr=document.createElement('tr');[row.material_id,`${fmt(row.length_mm)} × ${fmt(row.depth_mm)}`,row.default_level_count,row.quantity,row.total_level_count].forEach(value=>{const td=document.createElement('td');td.textContent=String(value ?? '未知');tr.append(td);});
      total+=Number(row.quantity || 0);levels+=Number(row.total_level_count || 0);$('bom-body').append(tr);
    });
    $('bom-total').textContent=fmt(total);$('level-total').textContent=fmt(levels);
    if (total!==shelfCount) showError('货架实例与物料数量不一致，不能视为有效交付。');
    $('download-preview').href=`/api/v2/jobs/${encodeURIComponent(state.job)}/layout.json`;
    $('download-json').href=`/api/v2/jobs/${encodeURIComponent(state.job)}/layout.json`;
    $('download-xlsx').href=`/api/v2/jobs/${encodeURIComponent(state.job)}/materials.xlsx`;
    $('viewer-3d-panel').hidden=state.status!=='complete';
    if(state.status==='complete') {
      try {
        if(!viewer)viewer=new StoreViewerV2($('viewer-3d'));
        viewer.load(result);
        if(window.__L3_VIEWER__.shelfCount!==total)throw new Error('三维实例与物料数量不一致');
      } catch(error) {showError(`三维显示未成功：${error.message}。布局文件仍可下载。`);}
    }
    fitBounds(boundsFromPoints(result.space?.boundary?.boundary_mm || []));
    loadComparison();
    if ($('reference-panel').open) loadReference();
  }
  function observedGeometry(item){
    const handles=new Set([item.handle,...(item.source_handles || [])].filter(Boolean).map(String));
    const drawing=state.result?.drawing || {};
    const lines=(drawing.lines || []).filter(line=>handles.has(String(line.handle)) || handles.has(String(line.source_handle)));
    const polylines=(drawing.polylines || []).filter(poly=>handles.has(String(poly.handle)) || handles.has(String(poly.source_handle)));
    const texts=(drawing.texts || []).filter(text=>handles.has(String(text.handle)) && finitePoint(text.position_mm));
    const footprint=ring(item.footprint_mm);
    const points=[...lines.flatMap(line=>[line.start_mm,line.end_mm]),...polylines.flatMap(poly=>poly.points_mm || []),...texts.map(text=>text.position_mm),...footprint].filter(finitePoint);
    return {handles,lines,polylines,texts,footprint,points};
  }
  function observedLegend(){
    for(const [parent,id] of [[$('canvas-wrap').parentElement,'observed-legend-2d'],[$('viewer-3d-panel'),'observed-legend-3d']]){
      let legend=$(id);if(!legend){legend=document.createElement('div');legend.id=id;legend.className='observed-legend';legend.setAttribute('aria-label','已有设计对象图例');
        for(const [css,symbol,text] of [['actual','▰','实际货架 · 计入已有货架统计'],['sample','◇','图例样本 · 紫色长虚线 · 不计货架数量'],['unknown','?','未知对象 · 橙色短虚线 · 不计货架数量']]){const item=document.createElement('span'),mark=document.createElement('span');mark.className=`observed-key ${css}`;mark.textContent=symbol;mark.setAttribute('aria-hidden','true');item.append(mark,document.createTextNode(text));legend.append(item);}
        if(id.endsWith('3d'))parent.insertBefore(legend,$('viewer-3d'));else parent.insertBefore(legend,$('shelf-detail'));
      }legend.hidden=false;
    }
  }
  function renderObservedObjects(selected=null){
    observedLayer.replaceChildren();
    if(state.result?.task_purpose!=='existing_design')return;
    for(const [key,css,symbol] of [['legend_samples','observed-sample','◇'],['unknown_objects','observed-unknown','?']]){
      for(const item of state.result[key] || []){
        const data=observedGeometry(item),group=node('g',{class:css+(item===selected?' observed-located':''),'data-testid':item===selected?'located-source-object':'observed-object'});
        if(data.footprint.length>=3)appendPolygon(group,data.footprint,'observed-outline');
        else{
          for(const line of data.lines)if(finitePoint(line.start_mm)&&finitePoint(line.end_mm))group.append(node('line',{x1:line.start_mm[0],y1:-line.start_mm[1],x2:line.end_mm[0],y2:-line.end_mm[1],class:'observed-outline'}));
          for(const poly of data.polylines)group.append(node(poly.closed?'polygon':'polyline',{points:pointsAttr(ring(poly.points_mm)),class:'observed-outline'}));
        }
        if(data.points.length){const point=data.points[0],size=state.view.w/Math.max(svg.clientWidth,400)*15;group.append(node('text',{x:point[0],y:-point[1],'font-size':size,class:'observed-symbol'},symbol));}
        observedLayer.append(group);
      }
    }
  }
  function locateObservedObject(item){
    const data=observedGeometry(item);if(!data.points.length)return;
    const fit=state.fit;fitBounds(boundsFromPoints(data.points));state.fit=fit;
    renderObservedObjects(item);
    [...$('cad-layer').children].forEach(element=>element.classList.toggle('observed-located-source',data.handles.has(element.getAttribute('data-source-handle'))));
    $('shelf-detail').textContent=`已定位原图对象 ${[...data.handles].join('、') || item.id || '未知'}。高亮仅标出已有源线或已有轮廓；对象仍未可靠分类，不计入货架数量。`;$('shelf-detail').hidden=false;
    revealDrawing();svg.setAttribute('tabindex','-1');svg.focus({preventScroll:true});
  }
  function renderExisting(result){
    state.result=result;state.mode=null;state.draft=[];state.boundary=[];state.holes=[];state.fixedHoles=[];state.entrances=[];state.exclusions=[];state.fixedExclusions=[];state.barriers=[];
    $('editor-panel').hidden=true;$('issue-summary').hidden=true;$('evidence-panel').hidden=false;$('empty-message').hidden=true;
    $('result-panel').hidden=false;$('viewer-3d-panel').hidden=false;$('shelf-detail').hidden=true;
    $('result-title').textContent='已有设计（原始位置）';$('canvas-title').textContent='已有设计 · 原始 CAD 位置';$('canvas-badge').textContent='已有设计已读取';
    $('delivery-note').textContent='这是原图已有设计的证据提取与显示，不是算法生成结果，未评估布局几何安全。图例和未知对象独立显示，不计入已有货架统计。';
    document.querySelector('.sku-status').hidden=true;$('comparison-body').closest('section').hidden=true;$('reference-panel').open=false;$('reference-frame').removeAttribute('src');
    $('bom-body').closest('section').querySelector('h2').textContent='已有货架规格统计（CAD 观察）';
    $('bom-body').closest('section').querySelector('p').textContent='仅统计原图中证据充分的实际货架；默认层数未提供时显示未知，图例与未知对象不计采购数量。';
    $('viewer-3d-panel').querySelector('p').textContent='左键旋转 · 滚轮缩放 · 右键平移 · 点击货架查看规格。墙与货架高度仅为显示参数；原图缺失的层数、范围或物体不会被补造。';
    $('existing-omissions').hidden=false;$('existing-omissions').replaceChildren();
    const title=document.createElement('h3');title.textContent='已读取内容与尚缺信息';$('existing-omissions').append(title);
    const observations=document.createElement('p');observations.textContent=`墙线 ${fmt(result.summary?.wall_count)} 条，禁放区域 ${fmt(result.summary?.exclusion_count)} 个，实际货架 ${(result.shelves || []).length} 个。${result.space?.boundary?'原图有可靠范围。':'没有可靠门店范围，因此不显示地面或外壳。'}`;$('existing-omissions').append(observations);
    const omissions=document.createElement('ul');
    for(const value of result.summary?.omissions || []){const li=document.createElement('li');li.textContent=String(value).replace(/^(NO_RELIABLE_PLANNING_SCOPE|NO_IDENTIFIABLE_SHELVES|PARTIAL_EXTRACTION)[：:]\s*/,'');omissions.append(li);}$('existing-omissions').append(omissions);
    const unknown=result.unknown_objects || [];
    if(unknown.length){const details=document.createElement('details'),summary=document.createElement('summary');summary.textContent=`查看 ${unknown.length} 个未知对象的原因`;details.append(summary);const list=document.createElement('ul');for(const item of unknown){const li=document.createElement('li');li.textContent=`源图元 ${item.handle || (item.source_handles || []).join('、') || '未知'}：${/[\u4e00-\u9fff]/.test(item.reason || '')?item.reason:'缺少可靠的对象分类或单个模块证据；已保留源图，不计货架数量。'}`;
      if(observedGeometry(item).points.length){const button=document.createElement('button');button.type='button';button.textContent='定位原图对象';button.className='locate-observed';button.dataset.testid='locate-observed';button.addEventListener('click',()=>locateObservedObject(item));li.append(button);}else{const note=document.createElement('span');note.className='muted';note.textContent='（没有可定位的源坐标）';li.append(note);}list.append(li);}details.append(list);$('existing-omissions').append(details);}
    $('source-evidence').textContent=JSON.stringify({source:result.source,evidence:result.evidence},null,2);$('business-summary').textContent='已有设计依据本次上传 CAD，不调用自动布局或参考方案。';
    renderCAD();renderSpace();$('run-layer').replaceChildren();$('shelf-layer').replaceChildren();
    for(const shelf of result.shelves || []){const element=node('polygon',{points:pointsAttr(shelf.footprint_mm || []),class:'shelf','data-shelf':shelf.id,'data-testid':'shelf-module',tabindex:0,role:'button','aria-label':`原图货架 ${shelf.id}`});element.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();selectShelf(shelf.id);}});$('shelf-layer').append(element);}
    $('result-metrics').replaceChildren();for(const [label,value] of [['实际货架',(result.shelves || []).length],['图例样本',(result.legend_samples || []).length],['未知对象',unknown.length],['原图墙线',result.summary?.wall_count]]){const div=document.createElement('div');div.textContent=label;const strong=document.createElement('strong');strong.textContent=fmt(value);div.append(strong);$('result-metrics').append(div);}
    $('bom-body').replaceChildren();let count=0;for(const row of result.bom || []){const tr=document.createElement('tr');for(const value of [row.material_id || row.id || 'CAD 观察',`${fmt(row.length_mm)} × ${fmt(row.depth_mm)}`,fmt(row.default_level_count),fmt(row.quantity),fmt(row.total_level_count)]){const td=document.createElement('td');td.textContent=value;tr.append(td);}$('bom-body').append(tr);count+=row.quantity || 0;}
    $('bom-total').textContent=fmt(count);$('level-total').textContent='未知';
    $('download-json').textContent='下载已有设计 JSON';$('download-xlsx').textContent='下载已有货架 XLSX';
    for(const id of ['download-json','download-preview'])$(id).href=`/api/v2/jobs/${encodeURIComponent(state.job)}/layout.json`;
    $('download-xlsx').href=`/api/v2/jobs/${encodeURIComponent(state.job)}/materials.xlsx`;
    try{if(!viewer)viewer=new StoreViewerV2($('viewer-3d'));viewer.loadExisting(result);}catch(error){showError(`已有设计三维显示未成功：${error.message}`);}
    fitBounds(result.drawing?.bounds_mm || boundsFromPoints((result.shelves || []).flatMap(s=>s.footprint_mm || [])));
    observedLegend();renderObservedObjects();
  }
  function selectShelf(id) {
    const shelf=state.result?.shelves?.find(s=>s.id===id);if (!shelf) return;
    state.selection=id;
    if(state.result.task_purpose==='existing_design'){$('shelf-detail').textContent=`原图货架 ${id} ｜ 观察规格 ${fmt(shelf.length_mm)} × ${fmt(shelf.depth_mm)} mm ｜ 默认层数：${fmt(shelf.default_level_count)}。保留原始位置；未评估布局几何安全。`;$('shelf-detail').hidden=false;return;}
    [...$('shelf-layer').children].forEach(n=>n.classList.toggle('selected',n.getAttribute('data-shelf')===id));
    const category=(state.result.products?.category_assignments || []).find(a=>(a.shelf_ids || []).includes(id));
    $('shelf-detail').textContent=`货架 ${id} ｜ 物料 ${shelf.material_id} ｜ ${fmt(shelf.length_mm)} × ${fmt(shelf.depth_mm)} mm ｜ 默认 ${shelf.default_level_count} 层 ｜ 品类：${category?.category || '未分配'}。品类标签不代表真实 SKU 已精确上架。`;
    $('shelf-detail').hidden=false;
  }
  async function loadComparison() {
    if(state.result?.task_purpose==='existing_design')return;
    if (!['complete','review_required'].includes(state.status) || state.comparisonJob===state.job) return;
    const job=state.job;state.comparisonJob=job;
    $('comparison-note').textContent='正在读取生成后的独立结构对照…';
    try {
      const value=await api(`/api/v2/jobs/${encodeURIComponent(job)}/comparison`);
      if (job!==state.job) return;
      const data=value.comparison || value;const a=data.automatic || {},r=data.reference || {};
      const distance = d => d?.count ? `${fmt(d.min_mm,1)}–${fmt(d.max_mm,1)} mm（${d.count} 项）` : '无可比观测';
      const entries=[['模块数',fmt(a.module_count),fmt(r.module_count)],['严格连续排数',fmt(a.reconstructed_single_face_run_count),`${fmt(r.reconstructed_single_face_run_count)}；近共线诊断 ${fmt(r.near_collinear_diagnostic_run_count)}`],['平均模块 / 排',fmt(a.mean_modules_per_run,2),fmt(r.mean_modules_per_run,2)],['孤立模块比例',`${fmt((a.isolated_module_ratio ?? NaN)*100,2)}%`,`${fmt((r.isolated_module_ratio ?? NaN)*100,2)}%`],['有效总长（mm）',fmt(a.effective_length_mm),fmt(r.effective_length_mm)],['短碎排',fmt(a.short_fragment_runs_under_two_modules),fmt(r.short_fragment_runs_under_two_modules)],['平行排净距',distance(a.parallel_free_gaps),distance(r.parallel_free_gaps)],['外接矩形代理密度',fmt(a.density?.shelf_envelope_proxy_density,3),fmt(r.density?.shelf_envelope_proxy_density,3)],['确认店面积（m²）',fmt(a.density?.confirmed_space_area_mm2 != null ? a.density.confirmed_space_area_mm2/1e6:NaN,2),'未知'],['模块方向（约 0.1°）',directionDisplay(a.direction_module_counts),directionDisplay(r.direction_module_counts)]];
      $('comparison-body').replaceChildren();entries.forEach(values=>{const tr=document.createElement('tr');if(values[0]==='模块方向（约 0.1°）')tr.className='direction-row';values.forEach(value=>{const td=document.createElement('td');td.textContent=value;tr.append(td);});$('comparison-body').append(tr);});
      $('comparison-note').textContent=`独立参考诊断${data.gate_c_status==='FAIL'?'：存在需说明的结构差异':'：仅描述结构，不是相同门店的数量目标'}。参考真实店面积未知；参考主货架净距约 550–690 mm，当前规则为 ${fmt(data.comparability?.production_aisle_width_mm || state.result?.rules?.aisle_width_mm)} mm。不同空间和规则下，数量、总长和代理密度不能直接比较。本诊断不代替本方案几何检查，也不增加阶段审批。`;
    } catch(error) { if(job===state.job) {state.comparisonJob=null;$('comparison-note').textContent=`独立对照暂不可用：${error.message}`;} }
  }
  function loadReference() {
    if(state.result?.task_purpose==='existing_design')return;
    if (!['complete','review_required'].includes(state.status) || !state.result || !state.job) return;
    const url=`/api/v2/jobs/${encodeURIComponent(state.job)}/reference.svg`;
    if ($('reference-frame').getAttribute('src')!==url) $('reference-frame').setAttribute('src',url);
  }
  function applyJob(job) {
    state.status=job.status;
    if (job.error || job.status==='failed') showError(errorText(job.error || job.message));
    else showError(null);
    $('status-label').textContent=labels[job.status] || '处理中';
    if(job.status==='complete' && job.result?.task_purpose==='existing_design')$('status-label').textContent='已有设计已读取';
    $('status-message').textContent=job.message || '';
    const progress=Number(job.progress);$('job-progress').value=Number.isFinite(progress)?Math.max(0,Math.min(100,progress)):0;
    $('canvas-badge').textContent=labels[job.status] || '处理中';
    const busy=['queued','preparing','planning'].includes(job.status);$('upload-button').disabled=busy;
    $('editor-panel').hidden=job.status!=='awaiting_confirmation';
    if(job.status!=='awaiting_confirmation')$('issue-summary').hidden=true;
    if (!busy && job.prepared && state.prepared?.prepared_id!==job.prepared.prepared_id) initPrepared(job.prepared);
    if (['complete','review_required'].includes(job.status) && job.result && state.result!==job.result) renderResult(job.result);
    updateEditorButtons();
    return busy;
  }
  async function poll() {
    clearTimeout(state.poll);const job=state.job;if (!job) return;
    try {
      const value=await api(`/api/v2/jobs/${encodeURIComponent(job)}`);
      if (job!==state.job) return;
      if (applyJob(value)) state.poll=setTimeout(poll,700);
    } catch(error) {if(job===state.job) {$('upload-button').disabled=false;showError(error.message);}}
  }
  $('upload-form').addEventListener('submit',async event=>{
    event.preventDefault();const file=$('cad-file').files[0];if(!file) return;
    if (!file.name.toLowerCase().endsWith('.dxf')) {showError('请选择 DXF 图纸。');return;}
    clearTimeout(state.poll);showError(null);$('upload-button').disabled=true;
    // Invalidate in-flight reads of the previous job before uploading a new
    // source. A late response must not restore its scene or old issue state.
    state.job=null;
    $('editor-panel').hidden=true;$('result-panel').hidden=true;
    if(viewer)viewer.clear();
    ['cad-layer','space-layer','run-layer','shelf-layer','draft-layer','vertex-layer','source-layer','snap-layer','observed-layer','topology-layer'].forEach(id=>$(id).replaceChildren());
    document.querySelectorAll('.observed-legend').forEach(element=>element.hidden=true);
    $('issue-summary').hidden=true;
    $('shelf-detail').hidden=true;$('empty-message').hidden=false;
    $('empty-message').textContent='正在读取新图纸，请稍候。';
    $('status-label').textContent='正在上传';$('status-message').textContent='正在提交图纸，请稍候。';
    const data=new FormData();data.append('file',file);state.taskPurpose=document.querySelector('input[name="task_purpose"]:checked').value;data.append('task_purpose',state.taskPurpose);
    try {
      const job=await api('/api/v2/jobs',{method:'POST',body:data});
      state.job=job.id;state.prepared=null;state.result=null;state.comparisonJob=null;
      $('result-panel').hidden=true;$('editor-panel').hidden=true;$('reference-frame').removeAttribute('src');
      const url=new URL(location.href);url.searchParams.set('job',job.id);history.replaceState(null,'',url);
      await poll();
    } catch(error) {showError(error.message);$('upload-button').disabled=false;}
  });
  $('confirm-space').addEventListener('click',()=>previewInput(true));
  $('resolve-issues').addEventListener('click',()=>previewInput(true));
  $('analyze-known-walls').addEventListener('click',()=>{if(state.previewBusy)return;const ids=knownWallEdgeIds();if(!ids.length)return;rememberAction();state.scopeEdgeIds=ids;state.perimeterDirty=true;state.scopeCandidateId=null;state.scopeReady=false;state.boundary=[];state.mode=null;state.draft=[];renderSpace();setFeedback([{message:'正在分别显示有来源的外围线组和局部闭合结构；柱体不会因已经闭合而成为门店范围。'}]);previewInput();});
  $('preview-selection').addEventListener('click',()=>{state.mode=null;state.lineStart=null;renderSourceSelection();previewInput();});
  $('locate-issue').addEventListener('click',()=>focusIssue(state.activeIssueId,true));
  $('next-issue').addEventListener('click',()=>{const index=state.issues.findIndex(i=>i.id===state.activeIssueId);focusIssue(state.issues[(index+1)%state.issues.length]?.id,true);});
  $('undo-action').addEventListener('click',()=>{
    if(state.previewBusy || !state.history.length)return;
    const target=state.history.pop(),savedPortals=state.appliedPortalClosureIds || [];
    const priorFacts=target.savedFacts || {resolutions:[],portal_closure_ids:[],boundary_work:boundaryWork({})},persistedAction=target.persistedAction;
    delete target.savedFacts;delete target.persistedAction;
    const changedIssues=new Set([...new Set([...Object.keys(state.resolutions),...Object.keys(target.resolutions)])].filter(id=>state.resolutions[id]!==target.resolutions[id]));
    const changedPortals=new Set([...new Set([...state.portalClosureIds,...target.portalClosureIds])].filter(id=>state.portalClosureIds.includes(id)!==target.portalClosureIds.includes(id)));
    const changedSaved=state.appliedResolutions.filter(record=>changedIssues.has(record.issue_id) && record.option_id!==target.resolutions[record.issue_id]);
    const restoredRecords=persistedAction?priorFacts.resolutions.filter(record=>changedIssues.has(record.issue_id) && !state.appliedResolutions.some(saved=>saved.issue_id===record.issue_id)):[];
    const revoked=changedSaved.filter(record=>!Object.hasOwn(target.resolutions,record.issue_id)).map(record=>record.issue_id);
    const restoredPortals=persistedAction?priorFacts.portal_closure_ids.filter(id=>changedPortals.has(id) && target.portalClosureIds.includes(id) && !savedPortals.includes(id)):[];
    const boundaryChanged=persistedAction && JSON.stringify(state.appliedBoundaryWork || boundaryWork({}))!==JSON.stringify(priorFacts.boundary_work || boundaryWork({}));
    const undoFacts=boundaryChanged || changedSaved.length || restoredRecords.length || restoredPortals.length || savedPortals.some(id=>changedPortals.has(id))?{
      ...(boundaryChanged?priorFacts.boundary_work:state.appliedBoundaryWork),
      resolutions:[...state.appliedResolutions.filter(record=>!revoked.includes(record.issue_id)).map(record=>changedIssues.has(record.issue_id)?{issue_id:record.issue_id,option_id:target.resolutions[record.issue_id]}:record),...restoredRecords],
      portal_closure_ids:[...savedPortals.filter(id=>!changedPortals.has(id) || target.portalClosureIds.includes(id)),...restoredPortals],
      ...(revoked.length?{revoked_issue_ids:revoked}:{})
    }:null;
    Object.assign(state,target);state.revokedIssueIds=[];state.added=[];state.mode=null;state.draft=[];state.lineStart=null;state.snap=null;
    refreshEditTargets();renderIssues();renderSpace();previewInput(false,undoFacts);
  });
  for(const [id,mode] of [['select-source','source'],['connect-endpoints','connect']])$(id).addEventListener('click',()=>{if(state.previewBusy)return;state.mode=state.mode===mode?null:mode;state.pan=false;state.draft=[];state.lineStart=null;$('pan-mode').setAttribute('aria-pressed','false');setFeedback([{message:mode==='source'?'点击原图边线进行选择或取消；完成后检验所选边线。':'依次点选两个已有源端点。出现十字吸附标记后才可补线；补线完成后自动检查；逻辑补线不作为实体墙。'}]);renderSpace();revealDrawing();});
  $('reset-3d').addEventListener('click',()=>viewer?.reset());
  $('top-3d').addEventListener('click',()=>viewer?.top());
  ['boundary','hole','entrance','exclusion'].forEach(type=>$(`draw-${type}`).addEventListener('click',()=>beginDrawing(type)));
  $('finish-drawing').addEventListener('click',finishDrawing);
  $('undo-point').addEventListener('click',()=>{state.draft.pop();renderSpace();});
    $('cancel-drawing').addEventListener('click',()=>{state.draft=[];state.mode=null;state.lineStart=null;state.snap=null;if(state.issueDrafts)delete state.issueDrafts[state.activeIssueId];renderSpace();});
  $('delete-last-region').addEventListener('click',()=>{
    if(state.previewBusy)return;rememberAction();
    const item=state.added.pop();if(!item)return;
    const list=item.kind==='hole'?state.holes:item.kind==='entrance'?state.entrances:state.exclusions;
    const index=list.indexOf(item.value);if(index>=0)list.splice(index,1);
    invalidateReview();refreshEditTargets();renderSpace();previewInput();
  });
  $('edit-target').addEventListener('change',()=>{state.mode=null;state.draft=[];renderSpace();});
  reviewFields.forEach(f=>$(`review-${f}`).addEventListener('change',updateEditorButtons));
  $('pan-mode').addEventListener('click',()=>{state.pan=!state.pan;$('pan-mode').setAttribute('aria-pressed',String(state.pan));svg.style.cursor=state.pan?'grab':'crosshair';});
  $('zoom-in').addEventListener('click',()=>zoom(.8));$('zoom-out').addEventListener('click',()=>zoom(1.25));
  $('reset-view').addEventListener('click',()=>{if(state.fit){state.view={...state.fit};viewApply();}});
  $('reference-panel').addEventListener('toggle',()=>{if($('reference-panel').open)loadReference();});
  svg.addEventListener('wheel',event=>{event.preventDefault();zoom(event.deltaY<0?.85:1/.85,world(event));},{passive:false});
  svg.addEventListener('pointerdown',event=>{
    if(event.button!==0 && event.button!==1)return;
    if(state.previewBusy)return;
    if(event.target.getAttribute?.('data-scope-candidate') && !state.pan && !state.mode)return;
    const edge=event.target.getAttribute?.('data-edge-hit') || event.target.getAttribute?.('data-edge-id');
    if(state.mode==='source' && edge && !state.pan && event.button===0){toggleSourceEdge(edge);event.preventDefault();return;}
    if(!state.mode && edge && !state.pan && event.button===0){const source=sourceEdges().find(e=>e.edge_id===edge);const issue=state.issues.find(i=>(i.source_edge_ids || []).includes(edge) || (i.source_handles || []).includes(source?.source_handle));if(issue){focusIssue(issue.id,false);$('active-issue-title').focus({preventScroll:true});event.preventDefault();return;}}
    const vertex=event.target.getAttribute?.('data-vertex');const shelf=event.target.closest?.('[data-shelf]');
    if(shelf && !state.mode && !state.pan && event.button===0){selectShelf(shelf.getAttribute('data-shelf'));return;}
    const point=world(event);
    if(vertex!==null && isEditable())rememberAction();
    state.pointer={clientX:event.clientX,clientY:event.clientY,world:point,view:{...state.view},vertex:vertex!==null?Number(vertex):null,
      panning:state.pan || event.button===1 || !state.mode,moved:false};
    svg.setPointerCapture(event.pointerId);event.preventDefault();
  });
  svg.addEventListener('pointermove',event=>{
    const p=world(event);$('coordinate-readout').textContent=`坐标：X ${fmt(p.x,1)}，Y ${fmt(-p.y,1)} mm`;
    if(isEditable() && state.mode){state.snap=snapPoint(event);renderSourceSelection();}
    const pointer=state.pointer;if(!pointer)return;
    if(Math.hypot(event.clientX-pointer.clientX,event.clientY-pointer.clientY)>3)pointer.moved=true;
    if(pointer.vertex!==null && isEditable()) {
      const target=selectedRing();if(target && target[pointer.vertex]) {target[pointer.vertex]=snapPoint(event) || [Math.round(p.x*100)/100,Math.round(-p.y*100)/100];invalidateReview();renderSpace();}
    } else if(pointer.panning) {
      const current=world(event);state.view.x+=pointer.world.x-current.x;state.view.y+=pointer.world.y-current.y;viewApply();
    }
  });
  svg.addEventListener('pointerup',event=>{
    const pointer=state.pointer;if(!pointer)return;
    if(state.mode && !pointer.panning && pointer.vertex===null && !pointer.moved && isEditable()) {
      if(state.mode==='connect'){
        const point=snapPoint(event);
        if(!point)setFeedback([{message:'没有吸附到已有端点。请放大局部，靠近源线端点直到出现十字，再点击。'}],false);
        else if(!state.lineStart){state.lineStart=point;setFeedback([{message:'已选择第一个原图端点；请点击另一个已有端点。'}]);}
        else if(point[0]===state.lineStart[0] && point[1]===state.lineStart[1])setFeedback([{message:'请选择另一个不同的源端点。'}],false);
        else{rememberAction();state.scopeCandidateId=null;state.perimeterDirty=true;state.scopeReady=false;state.boundary=[];state.supplementalEdges.push({id:`user-edge-${++state.regionCounter}`,start_mm:[...state.lineStart],end_mm:point,kind:'HUMAN_BOUNDARY_SUPPLEMENT'});state.lineStart=null;state.mode=null;previewInput();}
        renderSourceSelection();
      }else if(state.mode!=='source'){const p=world(event);state.draft.push(snapPoint(event) || [Math.round(p.x*100)/100,Math.round(-p.y*100)/100]);renderSpace();}
    }
    if(pointer.vertex!==null && pointer.moved){const field=({boundary:'boundary',hole:'holes',entrance:'entrances',exclusion:'exclusions'})[$('edit-target').value.split(':')[0]];if(field && !state.dirtyFields.includes(field))state.dirtyFields.push(field);previewInput();}
    state.pointer=null;if(svg.hasPointerCapture(event.pointerId))svg.releasePointerCapture(event.pointerId);
  });
  svg.addEventListener('pointercancel',()=>{state.pointer=null;});
  window.addEventListener('resize',()=>{renderVertices();renderSourceSelection();});
  const initialJob=new URL(location.href).searchParams.get('job');
  if(initialJob){state.job=initialJob;poll();}
})();
