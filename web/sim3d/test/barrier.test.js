import test from 'node:test';
import assert from 'node:assert/strict';
import * as THREE from 'three';
import { BarrierGate } from '../js/entities/BarrierGate.js';

async function gate() {
  const acks = [];
  const assets = { async cloneModel() {
    const root = new THREE.Group(), arm = new THREE.Group(); arm.name = 'Arm'; root.add(arm); return root;
  } };
  const gate = new BarrierGate('in', assets, (...ack) => acks.push(ack), { gateX: -3.5, gateY: -25.5 });
  await gate.ready;
  return { gate, acks };
}

test('configured animation, retry and original command ack remain continuous', async () => {
  const {gate:g, acks} = await gate();
  g.command('open','entry','one',1.5);
  const angles = [];
  for (let i=0;i<15;i++) { g.update(.1); angles.push(g.armAngleDeg());
    if(i===4) g.command('open','retry','one',1.5); }
  assert.ok(new Set(angles.map(a=>a.toFixed(3))).size>=5);
  assert.ok(Math.abs(angles.at(-1)-82)<.01);
  assert.equal(acks[0][2],'one'); assert.ok(g.isOpen);
  g.command('close','manual','two',1.5);
  assert.equal(g.isOpen,false); g.update(.5);
  const mid = g.armAngleDeg(); assert.ok(mid>0 && mid<82);
  g.command('open','interrupt','three',1.5); assert.equal(g.armAngleDeg(),mid);
  g.update(1.5); assert.ok(g.isOpen); assert.equal(acks.at(-1)[2],'three');
});

test('stop persists fraction and frozen position can hydrate', async () => {
  const {gate:g,acks}=await gate();
  g.command('open','','open',1.5);g.update(.4);const angle=g.armAngleDeg();
  g.command('stop','','stop');g.update(5);
  assert.equal(g.armAngleDeg(),angle);assert.equal(acks.at(-1)[1],'stopped');
  assert.equal(acks.at(-1)[3],angle/82);
  g.forceState('STOPPED',{hydration:true,arm_position:.43});
  assert.ok(Math.abs(g.armAngleDeg()-82*.43)<1e-6);
});
