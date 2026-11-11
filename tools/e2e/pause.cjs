const {chromium}=require('playwright');
const fs=require('fs');
const assert=require('assert/strict');
const base=process.env.PARKING_URL||'http://127.0.0.1:9632';
const out=process.env.EVIDENCE_DIR||'../_qa_audit/repair/P1-05/evidence';
fs.mkdirSync(out,{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH,
  args:(process.env.E2E_GPU?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11']:['--enable-unsafe-swiftshader','--use-angle=swiftshader'])});
 try {
  const page=await browser.newPage({viewport:{width:1280,height:800}});
  const errors=[];page.on('pageerror',e=>errors.push(String(e)));
  await page.goto(base+'/sim/');
  await page.waitForFunction(()=>__SIM__?.simApi?.authed&&__SIM__?.scheduler&&__SIM__?.rig);
  await page.evaluate(()=>__SIM__.simApi.resetDemo());
  await page.waitForTimeout(500);
  await page.evaluate(()=>__SIM__.scheduler.spawnEntry({vehicleId:'pause-car',imagePath:'datasets/plates/blue_000.png'}));
  await page.evaluate(()=>__SIM__.simApi.manualOpen('in'));
  await page.waitForFunction(()=>{const a=__SIM__.scene.gates.in.armAngleDeg();return a>5&&a<75;},null,{polling:20});
  await page.evaluate(()=>__SIM__.controls.setPaused(true));
  await page.waitForTimeout(350);
  async function sample(){return page.evaluate(()=>({angle:__SIM__.scene.gates.in.armAngleDeg(),
   camera:__SIM__.rig.camera.position.toArray(),clock:__SIM__.dump().simTime,
   scenario:__SIM__.scenario.clock,vehicles:[...__SIM__.scheduler.vehicles.values()].map(v=>({id:v.id,pose:{x:v.pos.x,z:v.pos.z,heading:v.heading()}}))}));}
  const before=await sample();
  await page.waitForTimeout(5000);
  const after=await sample();
  assert.deepEqual(after,before);
  const second=await browser.newPage();await second.goto(base+'/sim/');
  await second.waitForFunction(()=>__SIM__?.controls?.isPaused());
  const snapshot=await page.evaluate(()=>__SIM__.simApi.rest.snapshot());
  assert.equal(snapshot.simulation.paused,true);assert.notEqual(snapshot.barriers.in.state,'FAULT');
  await page.screenshot({path:out+'/paused.png'});
  await page.evaluate(()=>__SIM__.controls.setPaused(false));
  await page.waitForFunction(()=>__SIM__.scene.gates.in.state==='open');
  await second.waitForFunction(()=>!__SIM__.controls.isPaused());
  await page.screenshot({path:out+'/resumed.png'});
  assert.deepEqual(errors,[]);
  fs.writeFileSync(out+'/browser.json',JSON.stringify({before,after,snapshot,errors},null,2));
  console.log(JSON.stringify({frozen5s:true,twoClientSync:true,angle:before.angle,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
