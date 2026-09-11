// Presentation of server-owned geometry and weighted routes. No client-side planning.
import {node, pointsAttr, pathForPolygon} from './svg-primitives.js';

const $ = id => document.getElementById(id);
const sides = ['before', 'after'];
const finite = value => typeof value === 'number' && Number.isFinite(value);
const fmt = (value, digits=1) => finite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits:digits}) : 'NA';
const metres = value => finite(value) ? `${fmt(value / 1000, 2)} m` : 'NA · 无完整可达路线';
const roleNames = {entrance:'店外入口', receiving:'收货', pick_start:'拣货起点', packing:'打包 / 出货'};
const roleSymbols = {entrance:'入', receiving:'收', pick_start:'拣', packing:'包'};
const kindNames = {WALL_SINGLE:'沿墙单面排', CENTRAL_SINGLE:'中央单面排', BACK_TO_BACK:'两套独立单面排背靠背', M09_SINGLE_ASSUMPTION:'M09 单面取货测试假设'};
const sections = ['operations-panel','operations-legend','operations-selection','operations-route-audit','before-route-evidence','after-route-evidence'];
let current = null;

export const isOperations = value => value?.operation_mode === true || value?.mode === 'EMPLOYEE_OPERATIONS';
const items = value => Array.isArray(value) ? value : Object.entries(value || {}).map(([id,record]) => ({id,face_id:id,...record}));
const accessRows = layout => items(layout?.route_evaluation?.face_access);
function moduleCounts(layout) {
  if(!layout)return {};
  const counts={wall_single_module_count:0,central_single_module_count:0,double_sided_module_count:0};
  const byId=new Map((layout.runs || []).map(run=>[run.run_id,run]));
  const modules=[];let pairs=0;
  (layout.assemblies || []).forEach(assembly=>{
    const group=assembly.run_ids.flatMap(id=>byId.get(id)?.modules || []);
    const key=assembly.kind==='BACK_TO_BACK'?'double_sided_module_count':assembly.kind==='WALL_SINGLE'?'wall_single_module_count':'central_single_module_count';
    counts[key]+=group.length;modules.push(...group.map(module=>module.id));pairs+=(assembly.bay_pairs || []).length;
  });
  if(modules.length!==(layout.shelves || []).length || new Set(modules).size!==modules.length || counts.double_sided_module_count!==pairs*2)
    throw new Error('单双面组合、独立模块与物料计数不一致，不能展示有效结果。');
  return counts;
}
const metricsOf = result => ({...(result.layout?.route_evaluation?.summary || {}),...(result.measurements || {}),...moduleCounts(result.layout)});
const layoutOf = side => current.result[side].layout || {};
const baselineName = () => current.result.comparison_baseline==='M10_CORRECTED_INPUT'?'M10 同输入对照':'M09 技术对照';
const validPath = value => Array.isArray(value) && value.length >= 2 && value.every(point => Array.isArray(point) && point.length === 2 && point.every(finite));
const reasonText = value => typeof value === 'string' ? value : value?.message || value?.reason || (value ? JSON.stringify(value) : '未提供');
const routeGood = (route,expectedItems=route?.item_ids) => {
  if(!route || !validPath(route.path_mm) || !finite(route.distance_mm) || /UNREACH|FAIL|INVALID|NOT_|BLOCK|INCOMPLETE/i.test(route.status || ''))return false;
  const required=expectedItems || [],reported=route.item_ids || [],visited=route.visited_item_ids || [];
  return required.length>0 && reported.length===required.length && visited.length===required.length
    && new Set(visited).size===visited.length && required.every(id=>reported.includes(id) && visited.includes(id));
};
const taskKey = (type,id) => JSON.stringify([type,id]);

function element(tag,text,css) {
  const item=document.createElement(tag);
  if(text !== undefined)item.textContent=String(text);
  if(css)item.className=css;
  return item;
}
function row(body,values,attrs={}) {
  const item=element('tr');Object.entries(attrs).forEach(([key,value])=>item.setAttribute(key,value));
  values.forEach(value=>item.append(element('td',value)));body.append(item);return item;
}
function accessValue(record,role,key) {
  const prefix=role==='entrance'?'entry':role;
  return record?.[`${prefix}_${key}`] ?? record?.[role]?.[key];
}
function faceReachable(record) {
  return record?.reachable === true && ['entrance','pick_start','receiving'].every(role=>{
    const distance=accessValue(record,role,'distance_mm'),path=accessValue(record,role,'path_mm');
    const stationary=distance===0 && Array.isArray(path) && path.length===1 && path[0].length===2 && path[0].every(finite);
    return record?.[role]?.reachable!==false && finite(distance) && (validPath(path) || stationary);
  });
}
function selectedTask() {
  if(!current?.task)return null;
  const [type,id]=JSON.parse(current.task);
  return {type,id,task:(current.input.demand?.[type] || []).find(value=>value.id===id)};
}
function routeFor(layout,type,id) {
  const key=type==='picking_tasks'?'picking_routes':'replenishment_routes';
  return items(layout?.route_evaluation?.[key]).find(route=>route.id===id);
}

export function describeOperationsInput(input) {
  const rules=input?.base?.rules || {},demand=input?.demand || {};
  return `员工作业合成评测 · 明确可用墙段 ${input?.usable_walls?.length || 0} · 通行净宽 ${fmt(rules.aisle_width_mm,0)} mm · 固定测试商品 ${demand.items?.length || 0} · 拣货任务 ${demand.picking_tasks?.length || 0} · 补货任务 ${demand.replenishment_tasks?.length || 0}`;
}

export function resetOperations() {
  current=null;document.body.classList.remove('operations-mode');
  sections.forEach(id=>$(id).hidden=true);
  $('before-title').textContent='优化前 · 基线';
  $('metrics-title').textContent='连续排与几何指标';
  $('metrics-note').textContent='差值为“优化后 − 优化前”。仅两模块的短排作为诊断项；指标方向不同，不统一以增减表示优劣。';
  $('selection-space-note').textContent='货架深度来自本场景输入允许的模板。有效长度不等于商品容量；当前没有据此推算 SKU 容量。';
  $('bom-basis-note').textContent='数量来自各方案实际模块；规格和默认层数来自场景使用的内部物料配置。';
}

function drawBackground(svg,layout,side,view) {
  const group=node('g',{'data-layer':'operations-background'});
  const walkable=node('g',{'data-operation-layer':'walkable'});
  (layout.route_evaluation?.walkable_polygons || []).forEach((polygon,index)=>{
    const shape=node('path',{d:pathForPolygon(polygon),'fill-rule':'evenodd',class:'operations-walkable','data-testid':'operations-walkable'});
    shape.append(node('title',{},`有效通行区 ${index+1} · 依据当前通行净宽检查`));walkable.append(shape);
  });
  group.append(walkable);
  const corridors=node('g',{'data-operation-layer':'corridors'});
  (layout.planned_corridors || []).forEach(corridor=>{
    const shape=node('path',{d:pathForPolygon(corridor),'fill-rule':'evenodd',class:'operations-corridor','data-testid':'operations-corridor','data-corridor-id':corridor.id});
    shape.append(node('title',{},`预先规划通道 ${corridor.id} · ${corridor.role}`));corridors.append(shape);
  });
  group.append(corridors);
  const widths=node('g',{'data-operation-layer':'widths'});
  (layout.metrics?.aisle_sections || []).forEach(section=>{
    const a=section.start_mm,b=section.end_mm;if(!validPath([a,b]))return;
    const line=node('polyline',{points:pointsAttr([a,b]),class:'operations-aisle-width','data-testid':'operations-aisle-width','data-clear-width-mm':section.clear_width_mm});
    line.append(node('title',{},`${section.between_assemblies.join(' / ')}：相对取货面实际净宽 ${fmt(section.clear_width_mm,0)} mm`));widths.append(line);
    widths.append(node('text',{x:(a[0]+b[0])/2+70,y:-(a[1]+b[1])/2-50,'font-size':view.span/70,class:'operations-aisle-width-label'},`${fmt(section.clear_width_mm,0)}`));
  });
  group.append(widths);
  svg.insertBefore(group,svg.querySelector('[data-layer="modules"]'));
  const decorations=node('g',{'data-layer':'operations-structures'});
  (layout.assemblies || []).forEach(assembly=>{
    const shape=node('polygon',{points:pointsAttr(assembly.footprint_mm),class:`operations-assembly operations-${assembly.kind.toLowerCase()}`,'data-testid':'operations-assembly','data-assembly-id':assembly.id,'data-assembly-kind':assembly.kind});
    shape.append(node('title',{},`${assembly.id} · ${kindNames[assembly.kind] || assembly.kind} · 组合总深 ${fmt(assembly.total_depth_mm,0)} mm · 结构间隙 ${fmt(assembly.structure_gap_mm,0)} mm`));decorations.append(shape);
    const runIds=new Set(assembly.run_ids);
    (layout.runs || []).filter(run=>runIds.has(run.run_id)).flatMap(run=>run.modules).forEach(module=>{
      // Dataset matching avoids constructing a selector from a model-owned ID.
      [...svg.querySelectorAll('[data-module-id]')].filter(shape=>shape.dataset.moduleId===module.id).forEach(shape=>shape.setAttribute('data-assembly-kind',assembly.kind));
    });
  });
  svg.insertBefore(decorations,svg.querySelector('[data-layer="labels"]'));
}

function drawDirections(svg,layout,side,view) {
  const size=view.span/70;
  const defs=svg.querySelector('defs') || node('defs');
  if(!defs.parentNode)svg.prepend(defs);
  const marker=node('marker',{id:`${side}-pick-arrow`,markerWidth:size*.55,markerHeight:size*.55,refX:size*.45,refY:size*.25,orient:'auto',markerUnits:'userSpaceOnUse',viewBox:`0 0 ${size*.5} ${size*.5}`});
  marker.append(node('path',{d:`M0,0 L${size*.5},${size*.25} L0,${size*.5} Z`,fill:'#283c65'}));defs.append(marker);
  const group=node('g',{'data-layer':'operations-faces'});
  (layout.pick_faces || []).forEach(face=>{
    const start=face.start_mm,end=face.end_mm,mid=[(start[0]+end[0])/2,(start[1]+end[1])/2];
    const tip=[mid[0]+face.outward_normal[0]*size*1.25,mid[1]+face.outward_normal[1]*size*1.25];
    const access=accessRows(layout).find(record=>record.face_id===face.id);
    const reachable=faceReachable(access);
    const segment=node('polyline',{points:pointsAttr([start,end]),class:`operations-face ${reachable?'is-reachable':'is-unreachable'}`,'data-testid':'operations-pick-face','data-face-id':face.id,'data-module-id-ref':face.module_id,'data-reachable':reachable});
    segment.append(node('title',{},`${face.module_id} · 取货面 ${face.id} · ${face.side} 侧 · ${reachable?'三个工作起点可达':'缺少完整可达证据'}`));group.append(segment);
    group.append(node('line',{x1:mid[0],y1:-mid[1],x2:tip[0],y2:-tip[1],class:'operations-direction','marker-end':`url(#${side}-pick-arrow)`,'data-testid':'operations-pick-direction'}));
    if(face.standing_point_mm){const point=face.standing_point_mm;group.append(node('circle',{cx:point[0],cy:-point[1],r:size*.16,class:`operations-standing ${reachable?'':'is-unreachable'}`,'data-testid':'operations-standing-point','data-face-id':face.id}));}
  });
  svg.insertBefore(group,svg.querySelector('[data-layer="labels"]'));
}

function drawWorkpoints(svg,layout,side,view) {
  const group=node('g',{'data-layer':'operations-workpoints'}),size=view.span/65;
  const opening=current.input.entrance_opening_mm;
  if(validPath(opening)){
    // This thin logical threshold is not a physical wall. The wall segments
    // are rendered separately from the server's actual collision geometry.
    const line=node('polyline',{points:pointsAttr(opening),class:'operations-opening','data-testid':'operations-entrance-opening','data-is-wall':'false'});
    const width=Math.hypot(opening[1][0]-opening[0][0],opening[1][1]-opening[0][1]);
    line.append(node('title',{},`实际门洞净宽 ${fmt(width,0)} mm · 逻辑通行口，非实体墙`));group.append(line);
    opening.forEach(point=>group.append(node('circle',{cx:point[0],cy:-point[1],r:size*.14,class:'operations-door-jamb','data-testid':'operations-door-jamb'})));
    const middle=opening[0].map((v,i)=>(v+opening[1][i])/2);
    group.append(node('text',{x:middle[0]+size*.7,y:-middle[1]+size*1.3,'font-size':size*.7,class:'operations-door-width'},`净口 ${fmt(width,0)} mm`));
  }
  const connection=layout.route_evaluation?.entrance_connection;
  if(connection?.status==='PASS' && finite(connection.distance_mm)){
    const path=drawPath(group,connection.path_mm,'operations-entry-path',{'data-testid':'operations-entrance-connection'});
    path?.append(node('title',{},`入口净口接入路径 · ${metres(connection.distance_mm)} · 由当前通行规则校验`));
  }
  const grouped=new Map();
  Object.entries(layout.workpoints || current.input.workpoints || {}).forEach(([role,value])=>{
    const point=value.point_mm;if(!Array.isArray(point))return;
    const key=JSON.stringify(point);if(!grouped.has(key))grouped.set(key,{point,roles:[]});grouped.get(key).roles.push(role);
    const mark=node('circle',{cx:point[0],cy:-point[1],r:size*.32,class:`operations-workpoint operations-workpoint-${role}`,'data-testid':'operations-workpoint','data-workpoint-role':role});
    mark.append(node('title',{},`${roleNames[role] || role} · 合成测试位置 [${point.join(', ')}] mm`));group.append(mark);
  });
  grouped.forEach(({point,roles})=>{
    const label=node('text',{x:point[0]+size*.5,y:-point[1]-size*.45,'font-size':size,class:'operations-workpoint-label'});
    label.append(node('tspan',{},roles.map(role=>roleSymbols[role] || role).join(' / ')));group.append(label);
  });
  svg.append(group);
}

function drawPath(parent,path,css,attrs={}) {
  if(!validPath(path))return null;
  const shape=node('polyline',{points:pointsAttr(path),class:css,...attrs});parent.append(shape);return shape;
}

function renderRoutes() {
  const selected=selectedTask();if(!selected)return;
  const isPick=selected.type==='picking_tasks';
  $('operations-task-identity').textContent=`${isPick?'拣货':'补货'} ${selected.id} · 固定商品：${(selected.task?.item_ids || []).join('、')} · ${isPick?'拣货起点 → 任务商品取货面 → 打包 / 出货':'收货点 → 任务补货面 → 收货点'} · 合成评测`;
  sides.forEach(side=>{
    const layout=layoutOf(side),svg=$(`${side}-canvas`),route=routeFor(layout,selected.type,selected.id);
    svg.querySelector('[data-layer="operations-routes"]')?.remove();
    const group=node('g',{'data-layer':'operations-routes'});
    const shared=node('g',{'data-operation-layer':'shared'});
    const relatedShared=(layout.route_evaluation?.shared_segments || []).filter(segment=>
      (segment.task_ids || []).some(id=>id===selected.id || id===`${isPick?'picking':'replenishment'}:${selected.id}`));
    const sharedLength=relatedShared.every(segment=>finite(segment.length_mm))?relatedShared.reduce((sum,segment)=>sum+segment.length_mm,0):null;
    relatedShared.forEach((segment,index)=>{
      const shape=drawPath(shared,segment.path_mm,'operations-shared',{'data-testid':'operations-shared-segment'});
      shape?.append(node('title',{},`共享路段 ${index+1} · ${(segment.task_ids || []).join('、')} · 可能占道，不代表必然拥堵`));
    });
    group.append(shared);
    const evidence=$(`${side}-route-evidence`);evidence.replaceChildren();
    const good=routeGood(route,selected.task?.item_ids);
    const heading=element('strong',`${isPick?'拣货':'补货'} ${selected.id} · ${good?metres(route.distance_mm):'NA · 任务没有完整可走路线'}`);
    heading.className=good?'operations-route-ok':'operations-route-failed';evidence.append(heading);
    if(good){
      drawPath(group,route.path_mm,`operations-task-route ${isPick?'is-picking':'is-replenishment'}`,{'data-testid':'operations-task-path','data-task-id':selected.id,'data-route-kind':selected.type,'data-distance-mm':route.distance_mm});
      evidence.append(element('p',`实际访问顺序：${(route.visited_item_ids || []).join(' → ') || '未提供'}。${isPick?'含到打包点的末段。':'含返回收货点的末段。'}距离取自加权路径长度。本任务关联共享线段合计 ${finite(sharedLength)?metres(sharedLength):'未提供长度'}（可能占道）。`));
      const placements=items(layout.route_evaluation?.item_placements);
      (route.visited_item_ids || []).forEach((id,index)=>{
        const placement=placements.find(item=>item.item_id===id),point=placement?.standing_point_mm;
        if(!point)return;
        group.append(node('circle',{cx:point[0],cy:-point[1],r:current.view.span/110,class:'operations-stop','data-testid':'operations-route-stop'}));
        group.append(node('text',{x:point[0],y:-point[1]+current.view.span/250,'font-size':current.view.span/95,'text-anchor':'middle',class:'operations-stop-label'},String(index+1)));
      });
    } else {
      evidence.append(element('p',`状态：${route?.status || '未提供路线'}。${route?.reason?reasonText(route.reason):'路径缺失或未完整到达固定任务中的取货面；本页不以直线或局部路线代替。'}`));
    }
    svg.insertBefore(group,svg.querySelector('[data-layer="operations-workpoints"]'));
  });
  updateLayerVisibility();
}

function renderEntryRoutes() {
  if(!current)return;
  const itemId=$('operations-entry-item-select')?.value;
  const texts=[];
  sides.forEach(side=>{
    const layout=layoutOf(side),svg=$(`${side}-canvas`);
    svg.querySelector('[data-layer="operations-outside-route"]')?.remove();
    const placement=items(layout.route_evaluation?.item_placements).find(item=>item.item_id===itemId);
    const access=accessRows(layout).find(face=>face.face_id===placement?.face_id);
    const path=accessValue(access,'entrance','path_mm'),distance=accessValue(access,'entrance','distance_mm');
    const proven=layout.route_evaluation?.outside_entry_proof==='PASS' || layout.route_evaluation?.full_operations_proven===true;
    const good=proven && validPath(path) && finite(distance);
    const group=node('g',{'data-layer':'operations-outside-route'});
    if(good)drawPath(group,path,'operations-outside-route',{'data-testid':'operations-outside-route','data-item-id':itemId,'data-face-id':placement.face_id,'data-distance-mm':distance});
    svg.insertBefore(group,svg.querySelector('[data-layer="operations-workpoints"]'));
    texts.push(`${side==='before'?baselineName():'当前版'}：${good?metres(distance):'NA · 无完整店外入门证明'}`);
  });
  $('operations-entry-evidence').textContent=`固定商品 ${itemId || '未提供'} · 店外入口 → 实际门洞 → 该商品取货面站位。${texts.join('；')}。`;
}

function updateLayerVisibility() {
  if(!current)return;
  for(const name of ['walkable','corridors','shared','widths']){
    const visible=$(`operations-show-${name}`).checked;
    document.querySelectorAll(`[data-operation-layer="${name}"]`).forEach(layer=>layer.style.display=visible?'':'none');
  }
}

const metricRows = [
  ['wall_single_module_count','沿墙单面独立模块数',0],['central_single_module_count','中央单面独立模块数',0],['double_sided_module_count','背靠背两侧独立模块数',0],
  ['module_count','独立货架模块数',0],['run_count','单侧整排数',0],['wall_single_run_count','沿墙单面排数',0],['central_single_run_count','中央单面排数',0],
  ['double_sided_run_group_count','中央背靠背整排组数',0],['double_sided_bay_count','双面 bay 配对数（2 个独立模块 / 对）',0],
  ['effective_pick_length_mm','有效取货面总长（m）',2,1000],['single_sided_pick_length_mm','单面有效取货面长（m）',2,1000],['double_sided_pick_length_mm','双面两侧有效取货面长（m）',2,1000],
  ['nominal_board_area_m2','名义层板面积（m²，长 × 模板深 × 默认层数）',2],['net_shelf_area_m2','已核实净层板面积（m²）',2],
  ['reachable_face_count','全部起点可达的模块取货面数',0],['total_face_count','模块取货面总数（双面两侧分别计）',0],
  ['minimum_route_clear_width_mm','实际路线最窄净宽（mm）',1],
  ['parallel_pick_aisle_min_mm','相向取货面通道最小净宽（mm）',1],
  ['parallel_pick_aisle_median_mm','相向取货面通道净宽中位数（mm）',1],
  ['parallel_pick_aisle_max_mm','相向取货面通道最大净宽（mm）',1],
  ['same_run_module_max_gap_mm','同排模块最大间隙（mm）',3],
  ['entry_to_face_mean_mm','入口至各取货面平均路程（m）',2,1000],['picking_mean_mm','固定拣货任务平均路程（m）',2,1000],['picking_worst_mm','较差拣货任务路程（m）',2,1000],
  ['replenishment_mean_mm','固定补货任务平均路程（m）',2,1000],['replenishment_worst_mm','较差补货任务路程（m）',2,1000],
  ['geometry_violation_count','几何违规项数',0],['tail_waste_mm','排尾剩余总长（m）',2,1000],['short_run_count','短排数（诊断项）',0],
];

function renderMetrics() {
  const body=$('metrics-body');body.replaceChildren();
  const before=metricsOf(current.result.before),after=metricsOf(current.result.after);
  metricRows.forEach(([key,label,digits,divisor=1])=>{
    const values=[before[key],after[key]],display=value=>finite(value)?fmt(value/divisor,digits):key==='net_shelf_area_m2'?'UNKNOWN · 未核实净尺寸':'NA · 未通过或缺失';
    const delta=values.every(finite)?`${after[key]>before[key]?'+':''}${fmt((after[key]-before[key])/divisor,digits)}`:'不适用';
    const record=row(body,[label,...values.map(display),delta],{'data-metric':key});
    const heading=document.createElement('th');heading.scope='row';heading.textContent=label;record.replaceChild(heading,record.firstChild);
  });
  const depths=layout=>{
    const groups=new Map();
    (layout?.shelves || []).forEach(shelf=>{const key=`${fmt(shelf.depth_mm,0)} mm / ${fmt(shelf.default_level_count,0)} 层`;groups.set(key,(groups.get(key)||0)+1);});
    return [...groups].map(([key,count])=>`${key} × ${count}`).join('；') || '未提供';
  };
  row(body,['单侧模板深度 / 默认层数 / 模块数',depths(current.result.before.layout),depths(current.result.after.layout),'按同一模板口径对照'],{'data-metric':'depth_counts'});
  const structures=layout=>{
    const groups=new Map();
    (layout?.assemblies || []).forEach(assembly=>{
      const label=assembly.kind==='BACK_TO_BACK'
        ? `${assembly.side_depths_mm.map(value=>fmt(value,0)).join(' + ')} + 间隙 ${fmt(assembly.structure_gap_mm,0)} = 总深 ${fmt(assembly.total_depth_mm,0)} mm`
        : `${kindNames[assembly.kind] || assembly.kind} · 总深 ${fmt(assembly.total_depth_mm,0)} mm`;
      groups.set(label,(groups.get(label)||0)+1);
    });
    return [...groups].map(([key,count])=>`${key} × ${count}`).join('；') || '未提供';
  };
  row(body,['单侧深度、结构间隙与组合总深',structures(current.result.before.layout),structures(current.result.after.layout),'均为明示测试结构假设'],{'data-metric':'assembly_depth_counts'});
  const showChange=(key,label,divisor=1,unit='')=>{
    if(!finite(before[key]) || !finite(after[key]))return `${label}：${finite(before[key])?fmt(before[key]/divisor,2)+unit:'NA'} → ${finite(after[key])?fmt(after[key]/divisor,2)+unit:'NA'}（不计算改善率）`;
    return `${label} ${fmt(before[key]/divisor,2)} → ${fmt(after[key]/divisor,2)}${unit}`;
  };
  $('selection-tradeoffs').textContent=`同一固定任务需求下：${[showChange('effective_pick_length_mm','有效取货面',1000,' m'),showChange('nominal_board_area_m2','名义层板面积',1,' m²'),showChange('picking_mean_mm','拣货平均',1000,' m'),showChange('replenishment_mean_mm','补货平均',1000,' m')].join('；')}。所选货架深度与层数见下表；路程缺失不按 0 计算。`;
}

function renderCandidateComparison() {
  const body=$('operations-frontier-body');body.replaceChildren();
  const selection=current.result.selection || {};
  const structure=selection.structural_reason;
  $('operations-structure-reason').textContent=structure
    ? `双面候选：几何成立 ${structure.geometric_double_candidate_count} 个，满足全部发布条件 ${structure.eligible_double_candidate_count} 个。${structure.reason}${structure.best_eligible_double_candidate_id?' 最佳合法双面对照：'+structure.best_eligible_double_candidate_id+'。':''}${structure.residual_space_use?.explanation || ''}`
    : '双面候选是否成立及未采用原因，见完整候选明细。';
  const frontier=selection.frontier || current.result.frontier || [];
  items(frontier).forEach(candidate=>{
    const m=candidate.metrics || candidate.measurements || {};
    const id=candidate.candidate_id || candidate.id,isSelected=id===selection.chosen_candidate_id;
    const displayPair=(mean,worst)=>`${fmt(finite(mean)?mean/1000:null,2)} / ${fmt(finite(worst)?worst/1000:null,2)}`;
    const eligibility=candidate.comparison_only===true?'仅结构对照，不参与发布/支配比较'
      :candidate.eligible===false?'未满足发布条件，不参与支配比较'
      :candidate.eligible===true?`符合发布条件 / ${candidate.dominated===true?'被支配':candidate.dominated===false?'未被支配':'支配比较未提供'}`
      :'发布资格与支配比较未提供';
    row(body,[`${isSelected?'✓ 当前选择\n':''}${id}`,eligibility,fmt(finite(m.effective_pick_length_mm)?m.effective_pick_length_mm/1000:null,2),fmt(m.nominal_board_area_m2,2),displayPair(m.picking_mean_mm,m.picking_worst_mm),displayPair(m.replenishment_mean_mm,m.replenishment_worst_mm),fmt(candidate.worst_normalized_regret,4),`${reasonText(candidate.reason)}${candidate.structure_note?'；'+candidate.structure_note:''}`],{'data-candidate-id':id || '','data-selected':isSelected});
    if(isSelected && candidate.structure_note)$('selection-reason').textContent+=` ${candidate.structure_note}`;
  });
  if(!body.children.length)row(body,['后端未提供候选明细','NA','NA','NA','NA','NA','NA','以最终选择原因作为当前证据']);
  const box=$('operations-ablations');box.replaceChildren();
  items(selection.ablations || current.result.ablations || []).forEach((stage,index)=>{
    const article=element('article',undefined,'operations-stage');
    const stageNames={M09_TECHNICAL_BASELINE:'M09 技术对照',STRUCTURE_ONLY:'沿墙与双面结构',CORRIDOR_FIRST:'先规划通道与工作点接入',JOINT_SELECTION:'空间与路线联合选择'};
    article.append(element('h4',stage.title || stageNames[stage.stage] || stage.stage || stage.name || `阶段 ${index+1}`));
    if(stage.status==='COMPARISON_ONLY' || stage.comparison_only===true)article.append(element('p','仅结构对照，不参与发布/支配比较。','lab-subtle'));
    article.append(element('p',reasonText(stage.reason || stage.note || stage.description || stage.mechanism || stage.status)));
    const m=stage.metrics || stage.measurements || {};
    article.append(element('p',`模块 ${fmt(m.module_count,0)} · 有效取货面 ${metres(m.effective_pick_length_mm)} · 名义层板面积 ${fmt(m.nominal_board_area_m2,2)} m² · 可达面 ${fmt(m.reachable_face_count,0)} / ${fmt(m.total_face_count,0)} · 拣货平均 ${metres(m.picking_mean_mm)} · 补货平均 ${metres(m.replenishment_mean_mm)}`));
    const detail=element('details');detail.append(element('summary','查看阶段完整证据'),element('pre',JSON.stringify(stage,null,2)));article.append(detail);box.append(article);
  });
  if(!box.children.length)box.append(element('p','后端未提供阶段记录。'));
}

function routeCell(layout,type,task) {
  const route=routeFor(layout,type,task.id);
  return `${routeGood(route,task.item_ids)?metres(route.distance_mm):'NA · 未完整可达'}\n状态：${route?.status || 'MISSING'}\n访问顺序：${route?.visited_item_ids?.join(' → ') || '未提供'}${route?.reason?'\n'+reasonText(route.reason):''}`;
}
function renderAudit() {
  const body=$('operations-task-body');body.replaceChildren();
  for(const type of ['picking_tasks','replenishment_tasks']){
    (current.input.demand?.[type] || []).forEach(task=>row(body,[`${type==='picking_tasks'?'拣货':'补货'} ${task.id}\n全部任务商品：${task.item_ids.join('、')}`,...sides.map(side=>routeCell(current.result[side].layout,type,task))],{'data-task-id':task.id,'data-task-kind':type}));
  }
  const accessBody=$('operations-access-body');accessBody.replaceChildren();
  sides.forEach(side=>{
    const layout=layoutOf(side),records=accessRows(layout);
    (layout.pick_faces || []).forEach(face=>{
      const access=records.find(item=>item.face_id===face.id),point=face.standing_point_mm;
      row(accessBody,[side==='before'?baselineName():'当前选择',`${face.module_id}\n${face.id} · ${face.side}`,faceReachable(access)?'三个起点均可达':'不可达 / 证据未完整',...['entrance','pick_start','receiving'].map(role=>{const value=accessValue(access,role,'distance_mm');return finite(value)?fmt(value/1000,2):'NA';}),point?`[${point.map(v=>fmt(v,1)).join(', ')}]`:'NA'],{'data-face-id':face.id,'data-version':side});
    });
  });
  const diagnostics=$('operations-diagnostics');diagnostics.replaceChildren();
  sides.forEach(side=>{
    const evaluation=layoutOf(side).route_evaluation || {},box=element('div');
    box.append(element('h3',side==='before'?`${baselineName()} · 远端与绕行诊断`:'当前选择 · 远端与绕行诊断'));
    const entries=items(evaluation.diagnostics),list=element('ul');
    entries.forEach(value=>{
      let message=reasonText(value);
      if(value?.code==='FAR_FACE')message=`远端取货面 ${value.face_id}：从入口 ${metres(value.distance_mm)}。`;
      else if(value?.code==='DETOUR_VS_EUCLIDEAN_LOWER_BOUND')message=`绕行诊断 ${value.face_id}：实际 ${metres(value.route_distance_mm)}，比直线距离下界多 ${metres(value.extra_distance_mm)}。直线只作诊断下界，不视为可走路径。`;
      list.append(element('li',message));
    });
    const farthest=accessRows(current.result[side].layout).filter(record=>finite(accessValue(record,'pick_start','distance_mm'))).sort((a,b)=>accessValue(b,'pick_start','distance_mm')-accessValue(a,'pick_start','distance_mm')).slice(0,3);
    farthest.forEach(record=>list.append(element('li',`远端取货面 ${record.face_id}（${record.module_id}）：从拣货起点 ${metres(accessValue(record,'pick_start','distance_mm'))}。`)));
    if(!list.children.length)list.append(element('li','没有可用的完整可达诊断。'));
    box.append(list,element('p',`服务标记共享路段 ${evaluation.shared_segments?.length || 0} 段；拣货与补货可共用通道。共享表示可能占道，路线相交不等于必然拥堵。`,'lab-subtle'));diagnostics.append(box);
  });
}

export function appendOperationsDetail(box,side,shelf,run) {
  if(!current)return;
  const layout=current.result[side].layout;
  const assembly=(layout.assemblies || []).find(value=>value.run_ids.includes(run?.run_id));
  if(assembly){
    box.append(element('p',`${assembly.id} · ${kindNames[assembly.kind] || assembly.kind} · 单侧深度 ${assembly.side_depths_mm.map(value=>fmt(value,0)).join(' + ')} mm · 结构间隙 ${fmt(assembly.structure_gap_mm,0)} mm · 组合总深 ${fmt(assembly.total_depth_mm,0)} mm。`,'operations-assembly-detail'));
    box.append(element('p',assembly.kind==='BACK_TO_BACK'?`本组 ${assembly.run_ids.length} 个单侧整排，${assembly.bay_pairs.length} 个双面 bay 配对；BOM 按两侧独立模块计数。内部为货架结构区域，无人员通道。共用结构零部件 UNKNOWN。`:'当前为单面结构；取货方向与可站立位置见图中箭头。'));
    if(assembly.kind==='CENTRAL_SINGLE' && layout.metrics?.residual_space_use){
      box.append(element('p',layout.metrics.residual_space_use.explanation));
      const service=(layout.metrics.residual_single_back_service || []).find(value=>value.assembly_id===assembly.id);
      if(service)box.append(element('p',`背侧通道服务于 ${(service.witnesses || []).map(value=>`${value.served_assembly_id} ${value.served_side} 取货面`).join('、')}；已有服务依据 ${metres(service.served_back_length_mm)} / 单面背长 ${metres(service.required_back_length_mm)}。`));
    }
  }
  const faces=(layout.pick_faces || []).filter(face=>shelf?face.module_id===shelf.id:face.run_id===run?.run_id),records=accessRows(layout);
  box.append(element('p',`本次选中 ${faces.length} 个模块取货面；双面两侧分别核验。`));
  const subset=shelf?faces:faces.slice(0,3);
  subset.forEach(face=>{
    const access=records.find(record=>record.face_id===face.id);
    box.append(element('p',`${face.id} · ${face.side} 侧 · ${faceReachable(access)?'三个起点均可达':'不可达 / 证据未完整'}；入口 ${metres(accessValue(access,'entrance','distance_mm'))}，拣货起点 ${metres(accessValue(access,'pick_start','distance_mm'))}，收货 ${metres(accessValue(access,'receiving','distance_mm'))}。站位 [${face.standing_point_mm.map(v=>fmt(v,1)).join(', ')}] mm。`));
    const path=accessValue(access,'pick_start','path_mm');
    if(validPath(path) && finite(accessValue(access,'pick_start','distance_mm'))){
      const button=element('button','查看拣货起点到此取货面的路径');button.type='button';button.dataset.faceId=face.id;
      button.addEventListener('click',()=>{
        const svg=$(`${side}-canvas`);svg.querySelector('[data-layer="operations-face-path"]')?.remove();
        const group=node('g',{'data-layer':'operations-face-path'});drawPath(group,path,'operations-face-route',{'data-testid':'operations-face-path','data-face-id':face.id});svg.insertBefore(group,svg.querySelector('[data-layer="operations-workpoints"]'));
        const note=element('p',`已叠加 ${face.id} 的可走路径（蓝色虚线）；任务路线继续保留。`,'lab-subtle');button.replaceWith(note);
      });box.append(button);
    }
  });
  if(!shelf && faces.length>3)box.append(element('p','其余取货面见下方“全部模块取货面”列表。','lab-subtle'));
}

export function renderOperationsResult(result,scenario,view) {
  current={result,input:result.operations_input || scenario.input,view,task:null};
  document.body.classList.add('operations-mode');sections.forEach(id=>$(id).hidden=false);
  $('before-title').textContent=result.comparison_baseline==='M10_CORRECTED_INPUT'?'M10 · 同一修正输入':'M09 · 技术对照（业务未通过）';$('after-title').textContent=result.selection?.kept_baseline?'作业联合评价 · 保留方案':'作业联合评价 · 当前选择';
  if(result.comparison_baseline==='M10_CORRECTED_INPUT'){
    $('after-title').textContent=result.selection?.kept_baseline?'M11 联合选择 · 保留 M09 候选':'M11 作业联合布局';
    $('selection-title').textContent=result.selection?.kept_baseline?'最终选择：保留同输入的 M09 候选':'最终选择：采用作业布局候选';
  }
  $('metrics-title').textContent='单双面、名义空间与作业路程';
  $('metrics-note').textContent=`差值为“当前选择 − ${baselineName()}”。路线单位为米；NA 不视作零，不计算未完整任务的改善率。名义层板面积不表示净层板面积或商品容量。${result.input_migration_note || ''}`;
  $('selection-space-note').textContent='名义层板面积 = 模块长度 × 单侧模板深度 × 默认层数；净层板尺寸 UNKNOWN，真实 SKU 容量 UNKNOWN。深度、层数和模块数量必须同时对照。';
  const assumptions=current.input.assembly_assumptions || {};
  $('bom-basis-note').textContent=`组合采用测试假设：两套独立单面货架背靠背，结构间隙 ${fmt(assumptions.structure_gap_mm,0)} mm。BOM 分别计入两侧模块，组数、双面 bay 配对数与单侧整排数见指标表；共用结构采购级零部件 UNKNOWN。`;
  const demand=current.input.demand || {};
  $('operations-demand-summary').textContent=`固定测试商品 ${demand.items?.length || 0} 项，拣货 ${demand.picking_tasks?.length || 0} 组、补货 ${demand.replenishment_tasks?.length || 0} 组；最低有效取货面 ${metres(demand.minimum_pick_length_mm)}，最低名义层板面积 ${fmt(demand.minimum_nominal_board_area_m2,2)} m²。通行净宽 ${fmt(current.input.base?.rules?.aisle_width_mm,0)} mm。`;
  $('operations-route-policy').textContent=`固定任务商品不删减；商品按固定空间目标、最近且唯一取货面的口径分配。多件任务依次访问最近未访问目标，再到目的点；图边权为实际长度，并非逐件往返。商品位置策略：${demand.placement_policy || '未提供'}；路线策略：${demand.route_policy || '未提供'}。`;
  const select=$('operations-task-select');select.replaceChildren();
  for(const [type,label] of [['picking_tasks','拣货任务'],['replenishment_tasks','补货任务']]){
    const group=element('optgroup');group.label=label;
    (demand[type] || []).forEach(task=>{const option=element('option',`${task.id} · ${task.item_ids.join('、')}`);option.value=taskKey(type,task.id);group.append(option);});
    if(group.children.length)select.append(group);
  }
  current.task=select.value || null;
  const entrySelect=$('operations-entry-item-select');
  if(entrySelect){entrySelect.replaceChildren();(demand.items || []).forEach(item=>{const option=element('option',item.id);option.value=item.id;entrySelect.append(option);});}
  sides.forEach(side=>{
    const part=result[side],layout=part.layout,svg=$(`${side}-canvas`),m=metricsOf(part);
    if(!layout)return;
    drawBackground(svg,layout,side,view);drawDirections(svg,layout,side,view);drawWorkpoints(svg,layout,side,view);
    $(`${side}-summary`).textContent=`沿墙 ${fmt(m.wall_single_run_count,0)} 排 · 双面 ${fmt(m.double_sided_run_group_count,0)} 组 · ${fmt(m.module_count,0)} 模块 · 可达面 ${fmt(m.reachable_face_count,0)} / ${fmt(m.total_face_count,0)} · 几何违规 ${fmt(m.geometry_violation_count,0)}`;
    const shared=layout.route_evaluation?.shared_segments || [];
    const endCorridors=(layout.planned_corridors || []).filter(corridor=>corridor.role==='END_AISLE').length;
    $(`${side}-constraints`).textContent+=` · 通行净宽 ${fmt(layout.rules?.aisle_width_mm,0)} mm · 规划通道 ${layout.planned_corridors?.length || 0} 处 / 其中排端 ${endCorridors} 处 · 共享线段仅作诊断`;
  });
  renderMetrics();renderCandidateComparison();renderAudit();renderRoutes();renderEntryRoutes();updateLayerVisibility();
}

$('operations-task-select').addEventListener('change',event=>{
  if(!current)return;current.task=event.target.value;
  document.querySelectorAll('[data-layer="operations-face-path"]').forEach(layer=>layer.remove());renderRoutes();
});
['walkable','corridors','shared','widths'].forEach(name=>$(`operations-show-${name}`).addEventListener('change',updateLayerVisibility));
$('operations-entry-item-select')?.addEventListener('change',renderEntryRoutes);
