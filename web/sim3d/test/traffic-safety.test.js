import test from 'node:test';
import assert from 'node:assert/strict';
import * as THREE from 'three';
import fs from 'node:fs';
import {TrafficScheduler} from '../js/sim/TrafficScheduler.js';
import {RoutePlanner} from '../js/sim/RoutePlanner.js';
import {Collider} from '../js/entities/Collider.js';
import {Rest} from '../js/api.js';

const manifest=JSON.parse(fs.readFileSync(new URL('../../../../assets_3d/layout/lot_v2.json',import.meta.url)));
const flush=()=>new Promise(resolve=>setImmediate(resolve));
function setup(seed=42){
 const root=new THREE.Scene();
 const scene={scene:root,sceneCfg:{},gatesCfg:{},gates:{in:{isOpen:false},out:{isOpen:true}},
  assets:{async cloneModel(){return new THREE.Group();}},markers:{setOccupied(){}}};
 const planner=new RoutePlanner(manifest,RoutePlanner.spacesFromManifest(manifest));
 const events=[];
 const scheduler=new TrafficScheduler({scene,planner,manifest,onEvent:e=>events.push(e),options:{seed}});
 return {scheduler,scene,events};
}


test('eight simultaneous requests stay bounded, FIFO and cancel without ghosts',async()=>{
 const {scheduler:s}=setup();
 const promises=Array.from({length:8},(_,i)=>s.spawnEntry({vehicleId:'E'+i,kind:i%2?'green':'blue'}));
 await flush();await flush();
 assert.ok(s.vehicles.size>0 && s.vehicles.size<=3);
 assert.equal(s.vehicles.size+s._spawnRequests.length,8);
 for(const v of s.vehicles.values()) assert.ok(v.pos.z+v.dims.len/2<=s.outerSouthZ);
 const vs=[...s.vehicles.values()];
 for(let i=0;i<vs.length;i++)for(let j=i+1;j<vs.length;j++)assert.equal(s._pairCollide(vs[i],vs[j]),false);
 await assert.rejects(s.spawnEntry({vehicleId:'E7'}),/重复/);
 s.clearAll();await Promise.all(promises);await flush();
 assert.equal(s.vehicles.size,0);assert.equal(s._spawnRequests.length,0);
});


test('same seed gives identical model/image/pose and ids across reset',async()=>{
 async function snapshot(seed){const {scheduler:s}=setup(seed);
  const requests=Array.from({length:3},()=>s.spawnEntry());await flush();await flush();
  const result=[...s.vehicles.values()].map(v=>({id:v.id,model:v.spec.file,image:v.meta.imagePath,pose:v.pos}));
  s.clearAll();await Promise.all(requests);return result;}
 assert.deepEqual(await snapshot(18),await snapshot(18));
 assert.notDeepEqual(await snapshot(18),await snapshot(19));
});


test('unknown vehicle and ambiguous plate never bind another waiting car',async()=>{
 const {scheduler:s}=setup();
 const a=await s.spawnEntry({vehicleId:'A',plate:'京A12345'});
 const b=await s.spawnEntry({vehicleId:'B',plate:'京B12345'});
 a.state=b.state='wait_entry';
 assert.equal(s.onSpaceAssigned({vehicle_id:'unknown',plate:'京A12345',space_no:'A-01'}),null);
 assert.equal(a.meta.spaceNo,null);assert.equal(b.meta.spaceNo,null);
 s.onSpaceAssigned({vehicle_id:'B',plate:'京B12345',space_no:'B-01'});
 assert.equal(b.meta.spaceNo,'B-01');assert.equal(a.meta.spaceNo,null);
 assert.equal(s.onSpaceAssigned({vehicle_id:'B',space_no:'B-02'}),null);
 a.meta.plate=b.meta.plate='京C12345';
 assert.equal(s.onRecognized({plate:'京C12345',need_manual:false}),null);
});


test('unpaid car waits while gate is open for another car',async()=>{
 const {scheduler:s}=setup();const v=await s.spawnEntry({vehicleId:'A',plate:'京A12345'});
 v.state='wait_exit';v.meta.spaceNo='A-01';v.meta.recordId=10;
 s._advance(v);assert.equal(v.state,'wait_exit');
 assert.equal(s.onPaymentCompleted({vehicle_id:'unknown',plate:'京A12345',record_id:10}),null);
 assert.equal(s.onPaymentCompleted({vehicle_id:'A',plate:'京A12345',record_id:11}),null);
 s.onPaymentCompleted({vehicle_id:'A',plate:'京A12345',record_id:10});
 s._requestZone=()=>true;s._leadClear=()=>true;s._advance(v);
 assert.equal(v.state,'leaving');
});


test('body occupancy stays true after nose leaves and uses API zone ids',async()=>{
 const {scheduler:s}=setup();const v=await s.spawnEntry({vehicleId:'A'});
 const calls=[],original=Rest.zone;Rest.zone=async(...args)=>calls.push(args);
 try{
  v.placeAt(s.entry.x,s.entry.gateZ-1,0);v.state='to_space';s._syncGateZones([v]);
  assert.equal(calls.at(-1)[0],'in_barrier');assert.equal(calls.at(-1)[1],true);
  assert.ok(v.nosePos().z<s.entry.gateZ-.9 && v.tailPos().z>s.entry.gateZ);
  s._syncGateZones([v]);assert.equal(calls.length,1);await flush();
  v.placeAt(s.entry.x,s.entry.gateZ-v.dims.len/2-1,0);s._syncGateZones([v]);
  assert.equal(calls.at(-1)[0],'in_barrier');assert.equal(calls.at(-1)[1],false);
 }finally{Rest.zone=original;}
});


test('queue and RNG restore without putting queued cars outside the road',async()=>{
 const {scheduler:a}=setup(22);
 const requests=Array.from({length:8},()=>a.spawnEntry());await flush();await flush();
 const saved=a.schedulerSnapshot(),physical=a.runtimeSnapshot().map(v=>({...v,owner_id:'owner'}));
 const {scheduler:b}=setup(99);
 await b.hydrate({session:{id:'session'},vehicle_runtime:physical,scheduler_runtime:[{...saved,owner_id:'owner'}]},'owner');
 assert.deepEqual(b.schedulerSnapshot(),saved);
 assert.equal(b.vehicles.size+b._spawnRequests.length,8);
 a.clearAll();b.clearAll();await Promise.all(requests);
});


test('sensor sends are serialized and failed occupancy is retried',async()=>{
 const {scheduler:s}=setup();const v=await s.spawnEntry({vehicleId:'A'});
 const original=Rest.zone,calls=[];let complete;
 Rest.zone=(...args)=>{calls.push(args);return new Promise((resolve,reject)=>complete={resolve,reject});};
 try{
  v.placeAt(s.entry.x,s.entry.gateZ,0);s._syncGateZones([v]);
  s._syncGateZones([]);assert.equal(calls.length,1);
  complete.reject(new Error('network'));await flush();
  s.simTime=2;s._syncGateZones([v]);assert.equal(calls.length,2);
  complete.resolve({ok:true});await flush();
  s._syncGateZones([]);assert.equal(calls.length,3);assert.equal(calls[2][1],false);
  complete.resolve({ok:true});await flush();
 }finally{Rest.zone=original;s.clearAll();}
});


test('all four production lengths stop with at least 0.30m nose clearance',()=>{
 const {scheduler:s}=setup();
 for(const family of ['sedan','hatch','suv','van']) {
  const dims=Collider.dimsOf(family);
  const entryNose=s._entryStopZ(dims.len)-dims.len/2;
  const exitNose=s._exitStopZ(dims.len)+dims.len/2;
  assert.ok(entryNose-s.entry.gateZ>=.3-1e-9);
  assert.ok(s.exit.gateZ-exitNose>=.3-1e-9);
 }
});
