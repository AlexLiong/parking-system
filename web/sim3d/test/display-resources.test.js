import test from 'node:test';
import assert from 'node:assert/strict';
import * as THREE from 'three';
import {AssetFactory} from '../js/scene/AssetFactory.js';
import {BarrierGate} from '../js/entities/BarrierGate.js';
import {Vehicle} from '../js/entities/Vehicle.js';
import {ParkingScene} from '../js/scene/ParkingScene.js';
import {GateDisplay} from '../js/ui/GateDisplay.js';

const context = {fillRect(){},strokeRect(){},fillText(){},measureText(text){return {width:text.length*50};},createLinearGradient(){return {addColorStop(){}};}};
globalThis.document = {createElement(){return {getContext(){return context;}};}};
test('gate screen reuses texture/material and only disposes its owned resources',async()=>{
 const factory=new AssetFactory(),shared=new THREE.MeshBasicMaterial();let sharedDisposals=0;shared.addEventListener('dispose',()=>sharedDisposals++);
 factory.cloneModel=async()=>{const root=new THREE.Group(),screen=new THREE.Mesh(new THREE.BoxGeometry(),shared);screen.name='DisplayScreen';root.add(screen);return root;};
 const gate=new BarrierGate('in',factory,()=>{},{gateX:-3.5,gateY:-25.5});await gate.ready;
 ParkingScene.prototype._mergeGateStatics.call({},gate);
 assert.ok(gate.screen.parent, 'dynamic screen must survive static batching');
 const material=gate.screen.material,texture=material.map;let disposed=0;texture.addEventListener('dispose',()=>disposed++);
 for(let i=0;i<100;i++)gate.setScreen(['停车 '+i+' 分钟','应缴 ¥10.00']);
 assert.equal(gate.screen.material,material);assert.equal(gate.screen.material.map,texture);assert.equal(texture.image.width,384);
 gate.dispose();gate.dispose();assert.equal(disposed,1);assert.equal(sharedDisposals,0);
});
test('vehicle plates dispose on replacement/departure without disposing shared GLB material',()=>{
 const factory=new AssetFactory(),shared=new THREE.MeshBasicMaterial(),v=new Vehicle('plate',factory,{family:'sedan'});
 let sharedDisposals=0;shared.addEventListener('dispose',()=>sharedDisposals++);
 v.model=new THREE.Group();v.plateMeshes=[new THREE.Mesh(new THREE.BoxGeometry(),shared),new THREE.Mesh(new THREE.BoxGeometry(),shared)];
 v.setPlate('京A12345');const first=v.plateMeshes[0].material;let disposed=0;first.map.addEventListener('dispose',()=>disposed++);
 v.setPlate('京A12345');assert.equal(v.plateMeshes[0].material,first);
 v.setPlate('京AD12345','green');assert.equal(disposed,1);assert.equal(v.plateMeshes[0].material,v.plateMeshes[1].material);
 v.plateMeshes[0].material.map.addEventListener('dispose',()=>disposed++);v.dispose(new THREE.Scene());v.dispose(new THREE.Scene());
 assert.equal(disposed,2);assert.equal(sharedDisposals,0);
});
test('display projection preserves fee duration, payment and closing order',()=>{
 const lines={},display=new GateDisplay((g,l)=>lines[g]=l);display.reset();
 display.apply({type:'plate_recognized',gate:'in',plate:'京A12345',need_manual:true});assert.ok(lines.in.includes('待人工确认'));
 display.apply({type:'fee_quote',plate:'京A12345',amount:12.5,minutes:61});assert.ok(lines.out.includes('停车 61 分钟'));
 display.apply({type:'payment_completed',plate:'京A12345',amount:12.5});assert.deepEqual(lines.out,['京A12345','停车 61 分钟','已缴 ¥12.50','请通行']);
 display.apply({type:'barrier_state',gate:'out',state:'CLOSED'});assert.deepEqual(lines.out,['出口','请停车识别']);
});
