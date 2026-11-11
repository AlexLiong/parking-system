/** Audit actual safeTurns plans, including plaza segments, without excluding zones. */
import fs from 'node:fs';
import assert from 'node:assert/strict';
import {RoutePlanner} from '../web/sim3d/js/sim/RoutePlanner.js';
const raw=JSON.parse(fs.readFileSync(new URL('../../assets_3d/layout/lot_v2.json',import.meta.url)));
const spaces=RoutePlanner.spacesFromManifest(raw),planner=new RoutePlanner(raw,spaces),unique=new Map();
for(const space of spaces)for(const mode of ['Entry','Exit'])for(const leg of planner['plan'+mode](space.space_no,{safeTurns:true,access:false}).legs){
 if(leg.kind!=='line'){
  const point=t=>{const u=1-t;return {x:u*u*u*leg.p0.x+3*u*u*t*leg.p1.x+3*u*t*t*leg.p2.x+t*t*t*leg.p3.x,z:u*u*u*leg.p0.z+3*u*u*t*leg.p1.z+3*u*t*t*leg.p2.z+t*t*t*leg.p3.z};};
  for(let i=1;i<=64;i++){const a=point((i-1)/64),b=point(i/64),line={kind:'line',ax:a.x,az:a.z,bx:b.x,bz:b.z,space:space.space_no,mode};unique.set([line.ax,line.az,line.bx,line.bz].join(','),line);}continue;
 }const key=[leg.ax,leg.az,leg.bx,leg.bz].join(',');if(!unique.has(key))unique.set(key,{...leg,space:space.space_no,mode});
}
const lines=[...unique.values()],opposing=[];
const inPlaza=(x,z)=>Math.abs(x)<=6.000001&&[raw.aisles.n_y,raw.aisles.s_y].some(y=>Math.abs(z+y)<=3.250001);
let minOpposingLaneCenters=Infinity,outsideCrossings=0,originPaths=0;
for(let i=0;i<lines.length;i++)for(let j=i+1;j<lines.length;j++){
 const a=lines[i],b=lines[j],dx=a.bx-a.ax,dz=a.bz-a.az,ex=b.bx-b.ax,ez=b.bz-b.az,n=Math.hypot(dx,dz);
 const den=dx*ez-dz*ex;
 if(Math.abs(den)>1e-6){const qx=b.ax-a.ax,qz=b.az-a.az,t=(qx*ez-qz*ex)/den,u=(qx*dz-qz*dx)/den;if(t>=0&&t<=1&&u>=0&&u<=1&&!inPlaza(a.ax+t*dx,a.az+t*dz))outsideCrossings++;}
 if(dx*ex+dz*ez<0&&Math.abs(den)<1e-6){
  if(Math.abs(dz)<1e-6){const lo=Math.max(Math.min(a.ax,a.bx),Math.min(b.ax,b.bx)),hi=Math.min(Math.max(a.ax,a.bx),Math.max(b.ax,b.bx));if(hi-lo>1e-6&&(lo<-6.000001||hi>6.000001))minOpposingLaneCenters=Math.min(minOpposingLaneCenters,Math.abs(a.az-b.az));}
  else if(Math.abs(dx)<1e-6){const lo=Math.max(Math.min(a.az,a.bz),Math.min(b.az,b.bz)),hi=Math.min(Math.max(a.az,a.bz),Math.max(b.az,b.bz));if(hi-lo>1e-6){const points=[lo,hi,...[raw.aisles.n_y,raw.aisles.s_y].flatMap(y=>[-y-3.251,-y+3.251]).filter(z=>z>lo&&z<hi)].sort((a,b)=>a-b);if(points.slice(1).some((z,k)=>!inPlaza(a.ax,(z+points[k])/2)||!inPlaza(b.ax,(z+points[k])/2)))minOpposingLaneCenters=Math.min(minOpposingLaneCenters,Math.abs(a.ax-b.ax));}}
 }
 if(n<1e-6||dx*ex+dz*ez>=0||Math.abs(dx*ez-dz*ex)>1e-6*n||Math.abs((b.ax-a.ax)*dz-(b.az-a.az)*dx)>1e-6*n)continue;
 const p=((b.ax-a.ax)*dx+(b.az-a.az)*dz)/n,q=((b.bx-a.ax)*dx+(b.bz-a.az)*dz)/n;
 const overlap=Math.min(n,Math.max(p,q))-Math.max(0,Math.min(p,q));if(overlap>1e-6)opposing.push({a,b,overlap});
}
for(const l of lines){const dx=l.bx-l.ax,dz=l.bz-l.az,t=-(l.ax*dx+l.az*dz)/(dx*dx+dz*dz);if(t>=0&&t<=1&&Math.hypot(l.ax+t*dx,l.az+t*dz)<1e-6)originPaths++;}
const result={runtime:true,safeTurns:true,plans:spaces.length*2,uniqueSegments:lines.length,excludedZones:[],opposingSharedPairs:opposing.length,minOpposingLaneCenters,vanLaneGap:minOpposingLaneCenters-2,outsideCrossings,originPaths,opposing};
const out=process.env.EVIDENCE_DIR||'../_qa_audit/repair/C-10/evidence';fs.mkdirSync(out,{recursive:true});fs.writeFileSync(out+'/runtime-lanes.json',JSON.stringify(result,null,2));console.log(JSON.stringify({...result,opposing:opposing.slice(0,3)}));assert.equal(opposing.length,0);
assert.ok(result.vanLaneGap>=1.25-1e-6);assert.equal(outsideCrossings,0);assert.equal(originPaths,0);
