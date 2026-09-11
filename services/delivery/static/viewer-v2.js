import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
const $=id=>document.getElementById(id);
const debug=window.__L3_VIEWER__={ready:false,shelfCount:0,selectedShelfId:null};
function textRow(parent,tag,text){const n=document.createElement(tag);n.textContent=text;parent.append(n);return n;}

export class StoreViewerV2 {
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
  resize(){const width=Math.max(1,this.container.clientWidth),height=Math.max(1,this.container.clientHeight);this.renderer.setSize(width,height);this.camera.aspect=width/height;this.camera.updateProjectionMatrix();}
  clear(){Object.assign(debug,{ready:false,shelfCount:0,bomCount:0,boardCount:0,result:null});const geometries=new Set(),materials=new Set();this.world.traverse(node=>{if(node.geometry)geometries.add(node.geometry);if(node.material){for(const material of(Array.isArray(node.material)?node.material:[node.material]))materials.add(material);}});geometries.forEach(g=>g.dispose());materials.forEach(m=>m.dispose());this.world.clear();this.pickables=[];this.highlight=null;debug.selectedShelfId=null;}
  shape(poly){const shape=new THREE.Shape();const points=poly.boundary_mm;shape.moveTo((points[0][0]-this.cx)/1000,(points[0][1]-this.cy)/1000);for(const p of points.slice(1))shape.lineTo((p[0]-this.cx)/1000,(p[1]-this.cy)/1000);for(const ring of poly.holes_mm||[]){const path=new THREE.Path();path.moveTo((ring[0][0]-this.cx)/1000,(ring[0][1]-this.cy)/1000);for(const p of ring.slice(1))path.lineTo((p[0]-this.cx)/1000,(p[1]-this.cy)/1000);shape.holes.push(path);}return shape;}
  polygon(poly,color,height=0){const geom=new THREE.ShapeGeometry(this.shape(poly));geom.rotateX(-Math.PI/2);const mesh=new THREE.Mesh(geom,new THREE.MeshStandardMaterial({color,side:THREE.DoubleSide,roughness:.92}));mesh.position.y=height;this.world.add(mesh);return mesh;}
  outline(ring,color,height=.025){const points=ring.map(p=>new THREE.Vector3((p[0]-this.cx)/1000,height,-(p[1]-this.cy)/1000));const line=new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(points),new THREE.LineBasicMaterial({color}));this.world.add(line);return line;}
  load(result){
    this.clear();Object.assign(debug,{taskPurpose:'generate',floorRendered:!!result.space.boundary,legendCount:0,unknownCount:0});this.result=result;const boundary=result.space.boundary?.boundary_mm || [];
    const bounds=result.drawing?.bounds_mm;
    const fit=boundary.length ? boundary : bounds?.length===4 ? [[bounds[0],bounds[1]],[bounds[2],bounds[3]]] : [[0,0],[1000,1000]];
    const xs=fit.map(p=>p[0]),ys=fit.map(p=>p[1]);this.cx=(Math.min(...xs)+Math.max(...xs))/2;this.cy=(Math.min(...ys)+Math.max(...ys))/2;this.width=(Math.max(...xs)-Math.min(...xs))/1000;this.depth=(Math.max(...ys)-Math.min(...ys))/1000;this.extent=Math.max(this.width,this.depth,8);
    if(boundary.length){const floor=this.polygon(result.space.boundary,'#d8e1ce');floor.name='planning-floor';floor.userData.holes=result.space.boundary.holes_mm.length;this.outline(boundary,'#84997c');}
    (result.space.boundary?.holes_mm || []).forEach(ring=>this.outline(ring,'#9d776a',.025));
    for(const zone of result.space.exclusions){const mesh=this.polygon(zone,'#e6b482',.035);mesh.name=`exclusion:${zone.id}`;this.outline(zone.boundary_mm,'#bd8b5b',.045);}
    for(const zone of result.space.entrances){const mesh=this.polygon(zone,'#78cad6',.045);mesh.name=`entrance:${zone.id}`;this.outline(zone.boundary_mm,'#348c9d',.055);}
    for(const run of result.runs){const line=this.outline(run.footprint_mm,'#326f59',.06);line.name=`run:${run.run_id}`;}
    const box=new THREE.BoxGeometry(1,1,1);const wallMaterial=new THREE.MeshStandardMaterial({color:'#9eaf96',transparent:true,opacity:.38,depthWrite:false,roughness:.9});
    const wh=(result.rules.display_wall_height_mm||2600)/1000,wt=(result.rules.display_wall_thickness_mm||100)/1000;
    for(const wall of result.space.barriers){const [a,b]=[wall.start_mm,wall.end_mm],dx=(b[0]-a[0])/1000,dy=(b[1]-a[1])/1000;const mesh=new THREE.Mesh(box,wallMaterial);mesh.name=`wall:${wall.id}`;mesh.scale.set(Math.hypot(dx,dy),wh,wt);mesh.position.set(((a[0]+b[0])/2-this.cx)/1000,wh/2,-((a[1]+b[1])/2-this.cy)/1000);mesh.rotation.y=Math.atan2(dy,dx);this.world.add(mesh);}
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
    Object.assign(debug,{ready:true,shelfCount:this.shelfRoot.children.length,bomCount:result.bom.reduce((n,b)=>n+b.quantity,0),boardCount,holeCount:result.space.boundary?.holes_mm.length || 0,wallCount:result.space.barriers.length,exclusionCount:result.space.exclusions.length,entranceCount:result.space.entrances.length,runCount:result.runs.length,result,shelfRoot:this.shelfRoot});
  }
  loadExisting(observed){
    const display=observed.display_parameters;
    // Adapter supplies display-only boards. Observed null measurements remain
    // present on each object and in the exported result; no floor is invented.
    const render={...observed,runs:[],rules:{display_wall_height_mm:display.wall_height_mm,display_wall_thickness_mm:display.wall_thickness_mm,display_shelf_height_mm:display.shelf_height_mm},
      shelves:observed.shelves.map(s=>({...s,observed_default_level_count:s.default_level_count,existing_observation:true,default_level_count:s.default_level_count ?? display.shelf_level_count}))};
    this.load(render);
    const sourceGroup=new THREE.Group();sourceGroup.name='observed-cad-lines';this.world.add(sourceGroup);
    const addLine=(points,color,dashed=false)=>{
      if(!points || points.length<2)return;
      const vectors=points.map(p=>new THREE.Vector3((p[0]-this.cx)/1000,.02,-(p[1]-this.cy)/1000));
      const material=dashed?new THREE.LineDashedMaterial({color,dashSize:dashed==='unknown'?.07:.3,gapSize:dashed==='unknown'?.17:.1}):new THREE.LineBasicMaterial({color});
      const line=new THREE.Line(new THREE.BufferGeometry().setFromPoints(vectors),material);line.computeLineDistances();sourceGroup.add(line);return line;
    };
    for(const line of observed.drawing.lines || []){const mesh=addLine([line.start_mm,line.end_mm],'#7d8490');if(mesh)mesh.userData.sourceHandle=line.handle;}
    for(const poly of observed.drawing.polylines || []){const pts=[...poly.points_mm];if(poly.closed && pts.length)pts.push(pts[0]);addLine(pts,'#7d8490');}
    for(const [group,color] of [['legend_samples','#8552b1'],['unknown_objects','#b47820']])for(const object of observed[group]){
      if(object.footprint_mm){const line=addLine(object.footprint_mm,color,group==='unknown_objects'?'unknown':'legend');if(line){line.name=`${group}:${object.id}`;line.userData.observedObject=object;}}
    }
    Object.assign(debug,{taskPurpose:'existing_design',result:observed,sourceCoordinatesPreserved:true,
      floorRendered:!!observed.space.boundary,legendCount:observed.legend_samples.length,unknownCount:observed.unknown_objects.length});
  }
  setView(position){const damping=this.controls.enableDamping;this.controls.enableDamping=false;this.controls.update();this.controls.target.set(0,0,0);this.camera.position.copy(position);this.camera.up.set(0,1,0);this.controls.update();this.controls.enableDamping=damping;}
  reset(){const size=this.extent;this.setView(new THREE.Vector3(size*.73,size*1.14,size*.98));}
  top(){this.setView(new THREE.Vector3(0,this.extent*1.65,.001));}
  pick(event){const rect=this.renderer.domElement.getBoundingClientRect();const mouse=new THREE.Vector2((event.clientX-rect.left)/rect.width*2-1,-(event.clientY-rect.top)/rect.height*2+1);const ray=new THREE.Raycaster();ray.setFromCamera(mouse,this.camera);const hit=ray.intersectObjects(this.pickables,false)[0];if(!hit)return;this.select(hit.object.userData.shelves[hit.instanceId]);}
  select(shelf){debug.selectedShelfId=shelf.id;if(this.highlight){this.world.remove(this.highlight);this.highlight.geometry.dispose();this.highlight.material.dispose();}this.highlight=this.outline(shelf.footprint_mm,'#f7a23b',.08);$('selection-3d').replaceChildren();textRow($('selection-3d'),'b',`${shelf.id} · ${shelf.material_id || '未匹配物料'}`);textRow($('selection-3d'),'p',`${shelf.length_mm.toFixed(2)} × ${shelf.depth_mm.toFixed(2)} mm · ${shelf.existing_observation && shelf.observed_default_level_count==null ? '默认层数未知；层板仅作展示' : `默认 ${shelf.default_level_count} 层`}`);if(shelf.existing_observation)textRow($('selection-3d'),'p',`原图位置 · 来源实体 ${shelf.source_handles.join(', ')}；未重新摆放`);textRow($('selection-3d'),'p','高度为显示假设，不作为采购规格');$('selection-3d').hidden=false;}
  projectShelf(id){const shelf=this.result?.shelves.find(s=>s.id===id);if(!shelf)return null;const p=new THREE.Vector3((shelf.x_mm-this.cx)/1000,(this.result.rules.display_shelf_height_mm||1800)/1000,-(shelf.y_mm-this.cy)/1000);p.project(this.camera);const rect=this.renderer.domElement.getBoundingClientRect();return {x:rect.left+(p.x+1)*rect.width/2,y:rect.top+(1-p.y)*rect.height/2,visible:p.z>-1&&p.z<1};}
}
