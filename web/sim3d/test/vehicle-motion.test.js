import test from 'node:test';import assert from 'node:assert/strict';import * as THREE from 'three';
import{Vehicle}from'../js/entities/Vehicle.js';import{Collider}from'../js/entities/Collider.js';
import{TrafficScheduler}from'../js/sim/TrafficScheduler.js';
function vehicle(){const v=new Vehicle('test',{}, {family:'sedan'});v.model=new THREE.Group();v.model.add(new THREE.Mesh(new THREE.BoxGeometry(1.8,1.5,4.6)));return v;}
const line=(end,reverse=false)=>({kind:'line',ax:0,az:0,bx:0,bz:end,reverse});
test('line reaches the exact endpoint and stops',()=>{const v=vehicle();v.setLegs([line(-10)]);for(let i=0;i<600&&!v.done;i++)v.update(1/30);assert.equal(v.pos.z,-10);assert.equal(v.speed,0);assert.equal(v.done,true);});
test('zero elapsed time leaves pose and progress unchanged',()=>{const v=vehicle();v.setLegs([line(-10)]);v.update(1);const before=JSON.stringify({pos:v.pos,dist:v.dist,theta:v.theta});v.update(0);assert.equal(JSON.stringify({pos:v.pos,dist:v.dist,theta:v.theta}),before);});
test('acceleration and straight speed stay bounded',()=>{const v=vehicle();v.setLegs([line(-100)]);v.update(.1);assert.ok(v.speed>0&&v.speed<.3);for(let i=0;i<100;i++)v.update(.1);assert.ok(v.speed<=2.5);});
test('reverse motion keeps the nose pointing away from travel',()=>{const v=vehicle();v.setLegs([line(5,true)]);for(let i=0;i<500&&!v.done;i++)v.update(1/30);assert.equal(v.pos.z,5);assert.ok(Math.abs(v.theta)<.001);assert.ok(v.nosePos().z<v.tailPos().z);assert.equal(v.reverseLeg,true);});
test('multiple legs consume elapsed progress without teleporting to a different origin',()=>{const v=vehicle();v.setLegs([line(-1),{kind:'line',ax:0,az:-1,bx:0,bz:-3}]);for(let i=0;i<600&&!v.done;i++)v.update(1/30);assert.equal(v.pos.z,-3);assert.equal(v.dist>=3,true);});
test('rotated OBB distinguishes separated parallel cars from AABB overlap',()=>{const a={x:0,z:0,theta:Math.PI/4,len:4.6,wid:1.8},b={...a,x:1.5,z:-1.5};assert.equal(Collider.boxFromPose(a).intersectsBox(Collider.boxFromPose(b)),true);assert.equal(Collider.poseOverlap(a,b),false);});
function scheduler(){return new TrafficScheduler({scene:{scene:new THREE.Scene(),staticColliders:[]},paths:{},onEvent(){}});}
test('static detection includes the rear corners and medians',()=>{const s=scheduler(),v=vehicle();v.placeAt(0,0,0);s.scene.staticColliders=[{id:'rear',kind:'median',minX:.7,maxX:1.1,minZ:1.8,maxZ:2.5}];assert.equal(s._staticHit(v).id,'rear');});
test('predicted vehicle collision restores the complete movement pose',()=>{const s=scheduler(),v=vehicle(),other=vehicle();other.id='other';v.setLegs([line(-20)]);v.speed=2.5;other.placeAt(0,-4.7,0);const before={...v.pos};s._safeStep(v,.1,[v,other]);assert.deepEqual(v.pos,before);assert.equal(v.dist,0);assert.equal(v.speed,0);});
test('predicted wall collision prevents crossing even if the old pose was clear',()=>{const s=scheduler(),v=vehicle();v.setLegs([line(-20)]);v.speed=2.5;s.scene.staticColliders=[{id:'wall',kind:'wall',minX:-2,maxX:2,minZ:-3,maxZ:-2.4}];assert.equal(s._staticHit(v),null);s._safeStep(v,.1,[v]);assert.equal(v.dist,0);assert.equal(v.pos.z,0);});
test('scheduler subdivides slow frames while consuming all elapsed time',()=>{const s=scheduler();const steps=[];s._updateStep=dt=>steps.push(dt);s.update(1.2);assert.ok(steps.length>=24);assert.ok(steps.every(dt=>dt<=.05));assert.ok(Math.abs(steps.reduce((a,b)=>a+b,0)-1.2)<1e-8);});

test('wheel axle roll follows signed distance and only front wheels steer',()=>{
 const v=new Vehicle('wheels',{}, {family:'sedan'});
 v.wheels=['FL','FR','RL','RR'].map(s=>{const w=new THREE.Object3D();w.name='Wheel_'+s;w.position.z=s[0]==='F'?-1.4:1.4;return w;});
 const arc={kind:'arc',cx:0,cz:0,r:8,a0:0,a1:Math.PI/2};
 v.setLegs([arc]);for(let i=0;i<30;i++)v.update(.05);
 assert.ok(v.wheels.every(w=>w.rotation.x<0));
 assert.ok(v.wheels.slice(0,2).every(w=>w.rotation.y<-.1));
 assert.ok(v.wheels.slice(2).every(w=>w.rotation.y===0));
 const before=v._spin;v.setLegs([{...arc,reverse:true}]);for(let i=0;i<30;i++)v.update(.05);
 assert.ok(v._spin>before);assert.ok(v.wheels.slice(0,2).every(w=>w.rotation.y>.1));
 v.stop();for(let i=0;i<60;i++)v.update(.05);const stopped=v._spin;v.update(.05);assert.equal(v._spin,stopped);
});
