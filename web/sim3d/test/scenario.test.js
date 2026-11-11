import test from 'node:test';import assert from 'node:assert/strict';
import {ScenarioPlayer} from '../js/sim/ScenarioPlayer.js';import {Rest} from '../js/api.js';
const flush=()=>new Promise(r=>setImmediate(r));
function setup(){const events=[],calls=[],vehicles=new Map(),scheduler={vehicles,async spawnEntry(s){calls.push(s);const v={id:s.vehicleId,vid:s.vid,state:'approach',meta:{plate:s.plate},setPlate(){}};vehicles.set(v.id,v);return v;},startExit(id){const v=vehicles.get(id);if(v?.state!=='parked')return null;v.state='to_exit';return v;},pendingExitVehicle(){return [...vehicles.values()].find(v=>v.state==='wait_exit');}};return {scheduler,calls,player:new ScenarioPlayer({scheduler,onEvent:e=>events.push(e)}),events};}
test('same input replay emits the same vehicle identities and ordered actions',async()=>{
 const run=async()=>{const c=setup();c.player.load({steps:[{t:1,action:'spawn',plate:'京A12345'},{t:1,action:'spawn',vid:'fixed',kind:'green'}]});c.player.play();c.player.update(1);await flush();return c.calls;};assert.deepEqual(await run(),await run());
});
test('exit waits for the selected vehicle to park and pay retries failed HTTP',async()=>{
 const c=setup();let pays=0;Rest.pay=async id=>{assert.equal(id,9);if(++pays===1)throw Error('temporary outage');return {ok:true};};
 c.player.load({steps:[{t:0,action:'spawn',vid:'car'},{t:0,action:'exit',vid:'car'},{t:0,action:'pay',vid:'car'}]});c.player.play();c.player.update(.1);await flush();assert.equal(c.scheduler.vehicles.get('car').state,'approach');
 const v=c.scheduler.vehicles.get('car');v.state='parked';c.player.update(2);await flush();assert.equal(v.state,'to_exit');
 v.state='wait_exit';v.meta.recordId=9;c.player.update(2);await flush();assert.equal(pays,1);assert.ok(c.player._retries.length);
 c.player.update(2);await flush();c.player.update(.1);await flush();assert.equal(pays,2);assert.ok(c.player.finished);assert.deepEqual(c.player.failures,[]);
});
test('reset ignores delayed spawn completions and explicit failed actions time out',async()=>{
 const c=setup();let resolve;c.scheduler.spawnEntry=()=>new Promise(r=>resolve=r);c.player.load({steps:[{action:'spawn',vid:'late',t:0}]});c.player.play();c.player.update(1);c.player.reset();resolve(null);await flush();assert.equal(c.player.pendingActions,0);assert.equal(c.events.filter(e=>e.msg?.startsWith('派车 ')).length,0);
 c.player.load({steps:[{t:0,action:'exit',vid:'missing',timeout:1}]});c.player.play();c.player.update(.1);await flush();c.player.update(2);await flush();assert.deepEqual(c.player.failures,['出场 missing超时']);
});

test('reset cancels retry actions still waiting in the microtask queue',async()=>{
 const c=setup();let fired=0;c.player.play();c.player._retry(()=>{fired++;return true;},5,'cancel');c.player.update(.1);c.player.reset();await flush();assert.equal(fired,0);
});
