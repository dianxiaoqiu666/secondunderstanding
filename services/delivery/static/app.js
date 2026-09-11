import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const $ = id => document.getElementById(id);
const debug = window.__L3_VIEWER__ = {ready:false, shelfCount:0, selectedShelfId:null, controls:null, scene:null};
let selectedFile = null;
let viewer = null;
let pollGeneration = 0;
function chosen(file) { selectedFile = file; $('file-label').textContent = file ? file.name : '选择或拖入一份 CAD'; }
$('cad-file').addEventListener('change', e => chosen(e.target.files[0]));
for (const event of ['dragenter','dragover']) $('dropzone').addEventListener(event, e => {e.preventDefault();$('dropzone').classList.add('dragging');});
for (const event of ['dragleave','drop']) $('dropzone').addEventListener(event,e=>{e.preventDefault();$('dropzone').classList.remove('dragging');});
$('dropzone').addEventListener('drop',e=>{if(e.dataTransfer.files.length===1){chosen(e.dataTransfer.files[0]);$('cad-file').files=e.dataTransfer.files;}});

function showError(message) {$('error').hidden=false; $('error').textContent=message; $('start').disabled=false; $('start').textContent='重新开始规划 →';}
function progress(job) {
  $('progress').value=job.progress; $('progress-text').textContent=job.status==='failed'?'处理失败':`${job.progress}%`;
  $('status-message').textContent=job.message;
  const phase=['queued','understanding','planning','completed'].indexOf(job.status);
  document.querySelectorAll('.steps li').forEach((item,index)=>item.classList.toggle('active',phase>=index+1));
  $('phase-label').textContent=job.status==='completed'?'查看与下载方案':job.status==='failed'?'请修正输入后重试':'自动处理图纸';
  document.querySelector('.intro-note b').textContent=job.status==='completed'?'03 / 03':'02 / 03';
}

$('upload-form').addEventListener('submit',async e=>{
  e.preventDefault(); if(!selectedFile) return;
  const generation=++pollGeneration;
  $('error').hidden=true; $('start').disabled=true; $('start').textContent='正在处理…';
  $('result-details').hidden=true; $('selection').hidden=true; $('scene-status').textContent='处理中'; $('scene-status').classList.remove('ready');
  if(viewer) {viewer.clear(); debug.ready=false; debug.shelfCount=0;}
  $('empty-state').hidden=false; $('reset-view').disabled=true; $('top-view').disabled=true;
  try {
    const form=new FormData(); form.append('file',selectedFile);
    const response=await fetch('/api/jobs',{method:'POST',body:form}); const value=await response.json();
    if(!response.ok) throw new Error(value.detail?.message||'CAD 上传失败，请重试。');
    localStorage.setItem('l3-last-job',value.id);
    await poll(value.id,generation);
  } catch(error) {showError(error.message); $('scene-status').textContent='未生成方案';}
});
async function poll(id,generation) {
  while(generation===pollGeneration){
    const response=await fetch(`/api/jobs/${id}`); const job=await response.json();
    if(!response.ok) throw new Error(job.detail?.message||'读取任务失败。');
    progress(job);
    if(job.status==='failed') {showError(`${job.error?.code||'PROCESS_FAILED'}：${job.error?.message||job.message}`);$('scene-status').textContent='未生成方案';return;}
    if(job.status==='completed') {renderResult(job.result,id);$('start').disabled=false;$('start').textContent='开始新的规划 →';return;}
    await new Promise(resolve=>setTimeout(resolve,500));
  }
}
function textRow(parent,tag,text){const element=document.createElement(tag);element.textContent=text;parent.append(element);return element;}
function renderResult(result,id) {
  $('result-details').hidden=false; $('shelf-count').textContent=result.shelves.length;
  $('material-count').textContent=result.bom.length; $('aisle-width').textContent=`${result.rules.aisle_mm} mm`;
  $('bom-body').replaceChildren();
  for(const row of result.bom){const tr=document.createElement('tr');[row.material_id,`${row.length_mm} × ${row.depth_mm}`,`${row.default_level_count} 层`,`${row.quantity} 组`].forEach(value=>textRow(tr,'td',value));$('bom-body').append(tr);}
  $('bom-total').textContent=`${result.shelves.length} 组`;
  $('download-json').href=`/api/jobs/${id}/layout.json`; $('download-xlsx').href=`/api/jobs/${id}/materials.xlsx`;
  $('source-details').replaceChildren();
  for(const [key,value] of Object.entries({'CAD 文件':result.source.filename,'文件 SHA256':result.source.sha256,'商品记录':`${result.business_summary.product_count} 条；不推测缺失的商品尺寸`,'货架显示高度':`${result.rules.display_shelf_height_mm||1800} mm（假定，仅供可视化）`,'墙体显示':`厚 ${result.rules.display_wall_thickness_mm||100} mm / 高 ${result.rules.display_wall_height_mm||2600} mm（假定，仅供可视化）`,'范围孔洞':`${result.spatial.scope.holes_mm.length} 个，按真实边界保留`,'设计边界':'基础过道为项目参数；本结果不是消防、结构或采购认证。'})){textRow($('source-details'),'dt',key);textRow($('source-details'),'dd',value);}
  $('warnings').replaceChildren();for(const warning of [...new Set([...(result.spatial.warnings||[]),...result.warnings])])textRow($('warnings'),'li',warning);
  try {
    if(!viewer)viewer=new StoreViewer($('viewer'));
    viewer.load(result);
    $('empty-state').hidden=true; $('reset-view').disabled=false; $('top-view').disabled=false;
    $('scene-status').textContent='几何检查通过 · 可交互'; $('scene-status').classList.add('ready');
  } catch(error){showError(`3D 显示失败：${error.message}。布局和物料文件仍可下载。`);debug.ready=false;}
}
$('reset-view').onclick=()=>viewer?.reset(); $('top-view').onclick=()=>viewer?.top();

class StoreViewer {
  constructor(container){
    this.container=container;this.scene=new THREE.Scene();this.scene.background=new THREE.Color('#edf0e8');
    this.camera=new THREE.PerspectiveCamera(42,1,.01,3000);
    this.renderer=new THREE.WebGLRenderer({antialias:true,alpha:false});this.renderer.setPixelRatio(Math.min(devicePixelRatio,2));this.renderer.outputColorSpace=THREE.SRGBColorSpace;
    this.renderer.domElement.id='store-canvas';this.renderer.domElement.setAttribute('aria-label','可交互门店三维货架规划');container.prepend(this.renderer.domElement);
    this.controls=new OrbitControls(this.camera,this.renderer.domElement);this.controls.enableDamping=true;this.controls.maxPolarAngle=Math.PI*.49;this.controls.minDistance=1;this.controls.maxDistance=400;
    this.scene.add(new THREE.HemisphereLight(0xffffff,0x879880,2.8));const light=new THREE.DirectionalLight(0xffffff,3);light.position.set(15,30,12);this.scene.add(light);
    this.world=new THREE.Group();this.scene.add(this.world);this.pickables=[];
    new ResizeObserver(()=>this.resize()).observe(container);this.resize();
    this.renderer.domElement.addEventListener('pointerdown',e=>this.down=[e.clientX,e.clientY]);
    this.renderer.domElement.addEventListener('pointerup',e=>{if(this.down&&Math.hypot(e.clientX-this.down[0],e.clientY-this.down[1])<5)this.pick(e);});
    const tick=()=>{requestAnimationFrame(tick);this.controls.update();this.renderer.render(this.scene,this.camera);};tick();
    Object.assign(debug,{controls:this.controls,scene:this.scene,camera:this.camera,renderer:this.renderer,projectShelf:id=>this.projectShelf(id),cameraSnapshot:()=>({position:this.camera.position.toArray(),target:this.controls.target.toArray(),zoom:this.camera.zoom})});
  }
  resize(){const width=this.container.clientWidth,height=this.container.clientHeight;this.renderer.setSize(width,height);this.camera.aspect=width/height;this.camera.updateProjectionMatrix();}
  clear(){const geometries=new Set(),materials=new Set();this.world.traverse(node=>{if(node.geometry)geometries.add(node.geometry);if(node.material){for(const material of(Array.isArray(node.material)?node.material:[node.material]))materials.add(material);}});geometries.forEach(g=>g.dispose());materials.forEach(m=>m.dispose());this.world.clear();this.pickables=[];this.highlight=null;debug.selectedShelfId=null;}
  shape(poly){const shape=new THREE.Shape();const points=poly.boundary_mm;shape.moveTo((points[0][0]-this.cx)/1000,(points[0][1]-this.cy)/1000);for(const p of points.slice(1))shape.lineTo((p[0]-this.cx)/1000,(p[1]-this.cy)/1000);for(const ring of poly.holes_mm||[]){const path=new THREE.Path();path.moveTo((ring[0][0]-this.cx)/1000,(ring[0][1]-this.cy)/1000);for(const p of ring.slice(1))path.lineTo((p[0]-this.cx)/1000,(p[1]-this.cy)/1000);shape.holes.push(path);}return shape;}
  polygon(poly,color,height=0){const geom=new THREE.ShapeGeometry(this.shape(poly));geom.rotateX(-Math.PI/2);const mesh=new THREE.Mesh(geom,new THREE.MeshStandardMaterial({color,side:THREE.DoubleSide,roughness:.92}));mesh.position.y=height;this.world.add(mesh);return mesh;}
  outline(ring,color,height=.025){const points=ring.map(p=>new THREE.Vector3((p[0]-this.cx)/1000,height,-(p[1]-this.cy)/1000));const line=new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(points),new THREE.LineBasicMaterial({color}));this.world.add(line);return line;}
  load(result){
    this.clear();this.result=result;const boundary=result.spatial.scope.boundary_mm;
    const xs=boundary.map(p=>p[0]),ys=boundary.map(p=>p[1]);this.cx=(Math.min(...xs)+Math.max(...xs))/2;this.cy=(Math.min(...ys)+Math.max(...ys))/2;this.width=(Math.max(...xs)-Math.min(...xs))/1000;this.depth=(Math.max(...ys)-Math.min(...ys))/1000;this.extent=Math.max(this.width,this.depth,8);
    const floor=this.polygon(result.spatial.scope,'#d8e1ce');floor.name='planning-floor';floor.userData.holes=result.spatial.scope.holes_mm.length;this.outline(boundary,'#84997c');
    result.spatial.scope.holes_mm.forEach(ring=>this.outline(ring,'#9d776a',.025));
    for(const zone of result.spatial.exclusion_zones){const mesh=this.polygon(zone,'#e6b482',.035);mesh.name=`exclusion:${zone.id}`;this.outline(zone.boundary_mm,'#bd8b5b',.045);}
    const box=new THREE.BoxGeometry(1,1,1);const wallMaterial=new THREE.MeshStandardMaterial({color:'#9eaf96',transparent:true,opacity:.38,depthWrite:false,roughness:.9});
    const wh=(result.rules.display_wall_height_mm||2600)/1000,wt=(result.rules.display_wall_thickness_mm||100)/1000;
    for(const wall of result.spatial.walls){const [a,b]=[wall.start_mm,wall.end_mm],dx=(b[0]-a[0])/1000,dy=(b[1]-a[1])/1000;const mesh=new THREE.Mesh(box,wallMaterial);mesh.name=`wall:${wall.id}`;mesh.scale.set(Math.hypot(dx,dy),wh,wt);mesh.position.set(((a[0]+b[0])/2-this.cx)/1000,wh/2,-((a[1]+b[1])/2-this.cy)/1000);mesh.rotation.y=Math.atan2(dy,dx);this.world.add(mesh);}
    this.shelfRoot=new THREE.Group();this.shelfRoot.name='shelf-instances';this.world.add(this.shelfRoot);
    const shelfHeight=(result.rules.display_shelf_height_mm||1800)/1000;
    const boardCount=result.shelves.reduce((sum,s)=>sum+s.default_level_count,0);
    const boards=new THREE.InstancedMesh(box,new THREE.MeshStandardMaterial({color:'#558f82',roughness:.72,metalness:.13}),boardCount);
    const posts=new THREE.InstancedMesh(box,new THREE.MeshStandardMaterial({color:'#30564f',roughness:.58,metalness:.28}),result.shelves.length*4);
    boards.name='shelf-levels';posts.name='shelf-posts';boards.userData.shelves=[];posts.userData.shelves=[];
    const dummy=new THREE.Object3D();let boardIndex=0,postIndex=0;
    for(const shelf of result.shelves){
      const root=new THREE.Object3D();root.name=shelf.id;root.userData={kind:'shelf',...shelf};root.position.set((shelf.x_mm-this.cx)/1000,0,-(shelf.y_mm-this.cy)/1000);root.rotation.y=THREE.MathUtils.degToRad(shelf.rotation_deg);root.updateMatrix();this.shelfRoot.add(root);
      const length=shelf.length_mm/1000,depth=shelf.depth_mm/1000;
      const add=(mesh,index,x,y,z,sx,sy,sz)=>{dummy.position.set(x,y,z);dummy.rotation.set(0,0,0);dummy.scale.set(sx,sy,sz);dummy.updateMatrix();mesh.setMatrixAt(index,new THREE.Matrix4().multiplyMatrices(root.matrix,dummy.matrix));mesh.userData.shelves[index]=shelf;};
      for(let level=0;level<shelf.default_level_count;level++){const y=shelf.default_level_count===1?.12:.12+level*(shelfHeight-.12)/(shelf.default_level_count-1);add(boards,boardIndex++,0,y,0,length,.04,depth);}
      for(const x of [-length/2+.02,length/2-.02])for(const z of [-depth/2+.02,depth/2-.02])add(posts,postIndex++,x,shelfHeight/2,z,.035,shelfHeight,.035);
    }
    boards.instanceMatrix.needsUpdate=true;posts.instanceMatrix.needsUpdate=true;this.world.add(boards,posts);this.pickables=[boards,posts];
    this.controls.maxDistance=this.extent*8;this.camera.far=this.extent*20;this.camera.updateProjectionMatrix();this.reset();
    Object.assign(debug,{ready:true,shelfCount:this.shelfRoot.children.length,bomCount:result.bom.reduce((n,b)=>n+b.quantity,0),boardCount,holeCount:result.spatial.scope.holes_mm.length,wallCount:result.spatial.walls.length,exclusionCount:result.spatial.exclusion_zones.length,result,shelfRoot:this.shelfRoot});
  }
  setView(position){const damping=this.controls.enableDamping;this.controls.enableDamping=false;this.controls.update();this.controls.target.set(0,0,0);this.camera.position.copy(position);this.camera.up.set(0,1,0);this.controls.update();this.controls.enableDamping=damping;}
  reset(){const size=this.extent;this.setView(new THREE.Vector3(size*.73,size*1.14,size*.98));}
  top(){this.setView(new THREE.Vector3(0,this.extent*1.65,.001));}
  pick(event){const rect=this.renderer.domElement.getBoundingClientRect();const mouse=new THREE.Vector2((event.clientX-rect.left)/rect.width*2-1,-(event.clientY-rect.top)/rect.height*2+1);const ray=new THREE.Raycaster();ray.setFromCamera(mouse,this.camera);const hit=ray.intersectObjects(this.pickables,false)[0];if(!hit)return;this.select(hit.object.userData.shelves[hit.instanceId]);}
  select(shelf){debug.selectedShelfId=shelf.id;if(this.highlight){this.world.remove(this.highlight);this.highlight.geometry.dispose();this.highlight.material.dispose();}this.highlight=this.outline(shelf.footprint_mm,'#f7a23b',.08);$('selection').replaceChildren();textRow($('selection'),'b',`${shelf.id} · ${shelf.material_id}`);textRow($('selection'),'p',`${shelf.length_mm} × ${shelf.depth_mm} mm · 默认 ${shelf.default_level_count} 层`);textRow($('selection'),'p','高度为显示假设，不作为采购规格');$('selection').hidden=false;}
  projectShelf(id){const shelf=this.result?.shelves.find(s=>s.id===id);if(!shelf)return null;const p=new THREE.Vector3((shelf.x_mm-this.cx)/1000,(this.result.rules.display_shelf_height_mm||1800)/1000,-(shelf.y_mm-this.cy)/1000);p.project(this.camera);const rect=this.renderer.domElement.getBoundingClientRect();return {x:rect.left+(p.x+1)*rect.width/2,y:rect.top+(1-p.y)*rect.height/2,visible:p.z>-1&&p.z<1};}
}

const previous=localStorage.getItem('l3-last-job');
if(previous&&/^[a-f0-9]{32}$/.test(previous)){const generation=++pollGeneration;$('start').disabled=true;poll(previous,generation).catch(error=>showError(error.message));}
