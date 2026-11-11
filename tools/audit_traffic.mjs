/** Deterministic runtime audit. Body proxies use production Collider dimensions;
 * actual GLB/renderer checks are run separately with tools/e2e. */
import fs from 'node:fs';
import * as THREE from 'three';
import {TrafficScheduler} from '../web/sim3d/js/sim/TrafficScheduler.js';
import {RoutePlanner} from '../web/sim3d/js/sim/RoutePlanner.js';
import {PathNetwork} from '../web/sim3d/js/sim/PathNetwork.js';
import {SceneManifest} from '../web/sim3d/js/scene/SceneManifest.js';
import {Vehicle} from '../web/sim3d/js/entities/Vehicle.js';
import {Collider} from '../web/sim3d/js/entities/Collider.js';
import {rectsOverlap,Z7_MARGIN} from '../web/sim3d/js/sim/ReservationTable.js';
import {Rest} from '../web/sim3d/js/api.js';
import {CAR_MODELS} from '../web/sim3d/js/config.js';

const raw=JSON.parse(fs.readFileSync(new URL('../../assets_3d/layout/lot_v2.json',import.meta.url)));
const manifest=new SceneManifest(raw),spaces=RoutePlanner.spacesFromManifest(raw);
const pose=v=>({x:v.pos.x,z:v.pos.z,theta:v.theta,...v.dims});
const rect=c=>({x:(c.minX+c.maxX)/2,z:(c.minZ+c.maxZ)/2,theta:0,wid:c.maxX-c.minX,len:c.maxZ-c.minZ});
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const hits=[];
let routes=0;
for(const space of spaces)for(const mode of ['Entry','Exit']) {
 const planner=new RoutePlanner(raw,spaces),v=new Vehicle('audit',{}, {family:'van'});v.model=new THREE.Group();
 const plan=planner['plan'+mode](space.space_no,{safeTurns:true});v.setLegs(plan.legs);v.cruise=2.5;
 for(let i=0;i<30000&&!v.done;i++) {
  v.update(1/30);
  for(const c of manifest.colliders()) if(Collider.poseOverlap(pose(v),rect(c))) {hits.push({space:space.space_no,mode,collider:c.id,pose:pose(v)});break;}
 }
 if(!v.done)throw Error('route did not finish '+space.space_no+mode);routes++;
}
const events=[],scene={scene:new THREE.Scene(),manifest:raw,sceneCfg:{},gatesCfg:{},
 staticColliders:manifest.colliders(),markers:{setOccupied(){}},gates:{in:{isOpen:true},out:{isOpen:true}},assets:{
 async cloneModel(file){const family=Object.values(CAR_MODELS).flat().find(m=>m.file===file)?.family||'sedan',d=Collider.dimsOf(family);
  const root=new THREE.Group();root.add(new THREE.Mesh(new THREE.BoxGeometry(d.wid,d.hgt,d.len)));return root;}
 }};
const paths=new PathNetwork({manifest:raw,spaces}),scheduler=new TrafficScheduler({scene,paths,manifest:raw,options:{seed:Number(process.env.SEED||20261020)},onEvent:e=>events.push({...e,t:scheduler?.simTime})});
const targets=process.env.TRAFFIC_TARGETS ? process.env.TRAFFIC_TARGETS.split(',') : ['A-02','B-02','C-02','D-02','A-12','B-12','C-12','D-12','A-03','B-03','C-03','D-03','A-13','B-13','C-13','D-13','A-04','B-04','C-04','D-04'];
const ids=targets.map((_,i)=>'audit-'+i),gone=new Set();
Object.assign(Rest,{zone:async()=>({ok:true}),passed:async()=>({ok:true}),left:async(id)=>{gone.add(id);return {ok:true};},
 arrived:async(id,gate)=>{const i=ids.indexOf(id),plate='京A'+String(10000+i);if(gate==='in'){
  scheduler.onRecognized({vehicle_id:id,plate,color:'blue'});scheduler.onSpaceAssigned({vehicle_id:id,plate,space_no:targets[i]});
 }else{scheduler.onFeeQuote({vehicle_id:id,plate,record_id:i+1,amount:5});scheduler.onPaymentCompleted({vehicle_id:id,plate,record_id:i+1});}return {ok:true};}});
const requests=ids.map(vehicleId=>scheduler.spawnEntry({vehicleId,kind:'blue',model:'car_sedan_standard_blue.glb'}));await flush();
const overlaps=[],outside=[],staticHits=[];const parked=new Set(),started=new Set(),windowConflicts=[];let z7WindowSamples=0,simultaneousSamples=0;
for(let frame=0;frame<600*30;frame++){
 scheduler.update(1/30);
 const held=[...scheduler.reservations._zones].flatMap(([zone,list])=>list.map(w=>({...w,zone})));
 z7WindowSamples+=held.filter(w=>w.zone.startsWith('Z7:')).length;
 for(let a=0;a<held.length;a++)for(let b=a+1;b<held.length;b++){
  const A=held[a],B=held[b];if(A.vehicleId===B.vehicleId)continue;
  const same=A.zone===B.zone,z7=A.zone.startsWith('Z7:')&&B.zone.startsWith('Z7:')&&rectsOverlap(scheduler.reservations._zoneRect(A.zone),scheduler.reservations._zoneRect(B.zone),Z7_MARGIN);
  if((same||z7)&&A.t0<B.t1-1e-9&&B.t0<A.t1-1e-9)windowConflicts.push({A,B,t:scheduler.simTime});
 }
 for(const v of scheduler.vehicles.values()) {
  if(v.state==='parked')parked.add(v.id);
  if(v.state==='parked'&&scheduler.simTime>Number(process.env.EXIT_AFTER||240)&&!started.has(v.id)){scheduler.startExit(v.id);started.add(v.id);}
  if(['approach','wait_entry'].includes(v.state)&&v.pos.z+v.dims.len/2>scheduler.outerSouthZ+.001)outside.push({id:v.id,t:scheduler.simTime});
  for(const c of manifest.colliders())if(Collider.poseOverlap(pose(v),rect(c)))staticHits.push({id:v.id,c:c.id,t:scheduler.simTime});
 }
 const vs=[...scheduler.vehicles.values()];
 if(vs.filter(v=>['approach','wait_entry','to_space'].includes(v.state)).length>=3&&vs.filter(v=>['to_exit','wait_exit','leaving'].includes(v.state)).length>=2)simultaneousSamples++;
 for(let a=0;a<vs.length;a++)for(let b=a+1;b<vs.length;b++)if(Collider.poseOverlap(pose(vs[a]),pose(vs[b])))overlaps.push({a:vs[a].id,b:vs[b].id,t:scheduler.simTime});
 if(frame%30===0)await flush();
}
for(const e of events){if(e.type==='parked'&&e.id)parked.add(e.id);}
const result={seed:scheduler.seed,simSeconds:scheduler.simTime,staticRoutes:routes,staticRouteHits:hits,requested:targets.length,parked:parked.size,exitStarted:started.size,left:gone.size,
 overlaps,outside,staticHits,z7WindowSamples,windowConflicts,simultaneousSamples,pending:scheduler._spawnRequests.length,final:scheduler.debugSnapshot(),diagnostic:[...scheduler.vehicles.values()].map(v=>({id:v.id,cruise:v.cruise,nextZone:v.nextZoneId,nextDist:v.nextZoneDist,released:[...v.zoneReleased],entered:[...v.zoneEntered]})),blocks:events.filter(e=>e.type==='motion_blocked').slice(-20),warnings:events.filter(e=>e.type==='warn').slice(-100)};
const out=process.env.EVIDENCE_DIR||'../_qa_audit/repair/T5/evidence';fs.mkdirSync(out,{recursive:true});fs.writeFileSync(out+'/traffic'+(process.env.CASE_NAME?'-'+process.env.CASE_NAME:'')+'.json',JSON.stringify(result,null,2));
console.log(JSON.stringify({staticRoutes:routes,routeHits:hits.length,simultaneousSamples,z7WindowSamples,windowConflicts:windowConflicts.length,requested:targets.length,parked:parked.size,left:gone.size,overlaps:overlaps.length,staticHits:staticHits.length,outside:outside.length,pending:result.pending,final:result.final.vehicles},null,2));
scheduler.clearAll();await Promise.all(requests);
if((process.env.EXIT_AFTER&&!simultaneousSamples)||!z7WindowSamples||windowConflicts.length||hits.length||overlaps.length||staticHits.length||outside.length||parked.size!==targets.length||gone.size!==targets.length)process.exitCode=1;
