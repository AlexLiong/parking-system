/** Worst-case van swept bodies against all other 79 occupied stalls at 30 Hz. */
import fs from 'node:fs';import assert from 'node:assert/strict';import * as THREE from 'three';
import {RoutePlanner} from '../web/sim3d/js/sim/RoutePlanner.js';
import {Vehicle} from '../web/sim3d/js/entities/Vehicle.js';
import {Collider} from '../web/sim3d/js/entities/Collider.js';
const raw=JSON.parse(fs.readFileSync(new URL('../../assets_3d/layout/lot_v2.json',import.meta.url))),spaces=RoutePlanner.spacesFromManifest(raw),planner=new RoutePlanner(raw,spaces),hits=[];
let frames=0,comparisons=0,minGap=Infinity,closest=null;
const corners=p=>{const c=Math.cos(p.theta),s=Math.sin(p.theta);return [[-1,-1],[1,-1],[1,1],[-1,1]].map(([a,b])=>({x:p.x+a*p.wid/2*c-b*p.len/2*s,z:p.z-a*p.wid/2*s-b*p.len/2*c}));};
const edgeDistance=(p,a,b)=>{const x=b.x-a.x,z=b.z-a.z,t=Math.max(0,Math.min(1,((p.x-a.x)*x+(p.z-a.z)*z)/(x*x+z*z)));return Math.hypot(p.x-a.x-t*x,p.z-a.z-t*z);};
const gap=(a,b)=>{const A=corners(a),B=corners(b);return Math.min(...A.flatMap(p=>B.map((v,i)=>edgeDistance(p,v,B[(i+1)%4]))),...B.flatMap(p=>A.map((v,i)=>edgeDistance(p,v,A[(i+1)%4]))));};
for(const space of spaces)for(const mode of ['Entry','Exit']){
 const v=new Vehicle('audit',{}, {family:'van'});v.model=new THREE.Group();v.setLegs(planner['plan'+mode](space.space_no,{safeTurns:true}).legs);v.cruise=2.5;
 for(let i=0;i<30000&&!v.done;i++){v.update(1/30);frames++;const pose={x:v.pos.x,z:v.pos.z,theta:v.theta,...v.dims};
  for(const other of spaces){if(other.space_no===space.space_no||Math.hypot(v.pos.x-other.x,v.pos.z+other.z)>8)continue;comparisons++;const parked={x:other.x,z:-other.z,theta:other.rotation*Math.PI/180,len:5.2,wid:2};if(Collider.poseOverlap(pose,parked)){hits.push({space:space.space_no,mode,other:other.space_no,pose});break;}const distance=gap(pose,parked);if(distance<minGap){minGap=distance;closest={space:space.space_no,mode,other:other.space_no,pose};}}
 }
 assert.ok(v.done);
}
const result={plans:160,frames,comparisons,van:{length:5.2,width:2},otherStallsOccupied:79,minGap,closest,hits};const out=process.env.EVIDENCE_DIR||'../_qa_audit/repair/C-10-clearance/evidence';fs.mkdirSync(out,{recursive:true});fs.writeFileSync(out+'/parked-neighbors.json',JSON.stringify(result,null,2));console.log(JSON.stringify({...result,hits:hits.slice(0,4)}));assert.equal(hits.length,0);

assert.ok(minGap>=.358,`van occupied-neighbor clearance ${minGap} <0.358m`);
