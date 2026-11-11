// 2026-10-26 出口杆异常：已识别/已授权 vs 未授权 + 人工抬杆。
// A) 未授权：出口杆故障，人工抬杆后车仍 wait_exit；付费后才离开。
// B) 已授权：付费后出口杆故障，人工抬杆后已授权车自动离开。
// 全程真实浏览器 + 真实后端；无穿杆（车体跨杆平面时杆必须 passable）。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict'), crypto = require('crypto');
const { spawnDispatchNoWait, waitParked, waitGone } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9640';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261026/E2E/screenshots';
fs.mkdirSync(out, { recursive: true });
const PLATE='粤F4K1CZ';

async function startGateWatcher(page, gate){
  await page.evaluate((g)=>{
    window.__gateViolations=[];window.__gateSeen={};
    window.__gateTimer=setInterval(()=>{
      const gateObj=__SIM__.scene.gates[g];
      if(!gateObj) return;
      window.__gateSeen[gateObj.state]=(window.__gateSeen[gateObj.state]||0)+1;
      const center=gateObj.zoneCenter();
      const plane=center.z;
      for(const v of __SIM__.scheduler.vehicles.values()){
        if(!v?.pos) continue;
        if(Math.abs(v.pos.x-center.x)>2.6) continue;
        const half=Number(v.dims?.len||4.6)/2;
        const over=Math.min(v.pos.z+half-plane, plane-(v.pos.z-half));
        if(over>0.25 && !gateObj.isPassable){
          window.__gateViolations.push({id:v.id,state:v.state,z:v.pos.z,plane,over,
            gateState:gateObj.state,passable:gateObj.isPassable,manual:gateObj.manual,authorization:gateObj.authorization});
        }
      }
    },40);
  },gate);
}
async function stopGateWatcher(page){
  return page.evaluate(()=>{clearInterval(window.__gateTimer);return {violations:window.__gateViolations||[],seen:window.__gateSeen||{}};});
}
(async()=>{
 const browser=await chromium.launch({headless:true,args:(process.env.E2E_GPU
   ?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11','--disable-background-timer-throttling']
   :['--enable-unsafe-swiftshader','--use-angle=swiftshader','--disable-background-timer-throttling']),
   ...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 try{
  const sim=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];sim.on('pageerror',e=>errors.push(String(e)));
  await sim.goto(base+'/sim/');await sim.waitForFunction(()=>__SIM__?.simApi?.rt?.sessionId,null,{timeout:90000});
  const login=await sim.request.post(base+'/api/auth/login',{data:{username:'admin',password:'admin123'}});
  assert.equal(login.status(),200);const token=(await login.json()).token,headers={Authorization:'Bearer '+token};
  async function post(path,data){const r=await sim.request.post(base+path,{headers,data});assert.ok(r.ok(),await r.text());return r.json();}
  async function snap(){return (await sim.request.get(base+'/api/state/snapshot',{headers})).json();}
  async function openRecordId(plate){
    const r=await sim.request.get(base+'/api/records?status=in&plate='+encodeURIComponent(plate)+'&page_size=50',{headers});
    assert.equal(r.status(),200,await r.text());
    return ((await r.json()).items||[])[0]?.id ?? null;
  }
  const admin=await browser.newPage({viewport:{width:1280,height:900}});
  admin.on('pageerror',e=>errors.push('admin:'+String(e)));
  await admin.addInitScript(t=>localStorage.setItem('parking.admin.token',t),token);
  const result={unauthorized:{},authorized:{},errors};
  await admin.goto(base+'/admin/#/monitor');

  // ---------- A) 未授权：故障杆 + 人工抬杆不放行 ----------
  await sim.bringToFront();
  await sim.evaluate(async()=>{await __SIM__.simApi.resetDemo();await __SIM__.controls.setSpeed(4);});
  await startGateWatcher(sim,'out');
  const a=await spawnDispatchNoWait(sim,{vehicleId:'authless-car',plate:PLATE,plateType:'blue',planDurationSec:600});
  await waitParked(sim,a.id,PLATE,90000);
  await sim.evaluate(()=>__SIM__.scheduler.startExit('authless-car'));
  await sim.waitForFunction(()=>__SIM__.scheduler.vehicles.get('authless-car')?.state==='wait_exit',null,{timeout:60000});
  await post('/api/barrier/out/fault',{action:'inject',mode:'stuck_closed'});
  await admin.bringToFront();
  await admin.waitForSelector('#anomaly-card:not([hidden])',{timeout:20000});
  await admin.waitForSelector('#anomaly-raise-out',{timeout:20000});
  await admin.locator('#anomaly-raise-out').click();
  await admin.waitForTimeout(1200);
  await sim.bringToFront();
  await sim.waitForTimeout(2000);
  let s=await snap();
  assert.equal(s.barriers.out.passable,true,'manual lift should be physically passable');
  const still=await sim.evaluate(()=>__SIM__.scheduler.vehicles.get('authless-car')?.state);
  assert.equal(still,'wait_exit','manual lift without business authorization must not release car');
  await sim.screenshot({path:out+'/barrier-auth-unpaid-manual-open.png'});
  result.unauthorized={barrier:s.barriers.out,state:still};
  // 完成计费后才离开
  let rid=await sim.evaluate(()=>__SIM__.scheduler.vehicles.get('authless-car')?.meta?.recordId);
  if(rid==null) rid=await openRecordId(PLATE);
  assert.ok(rid,'authless car needs record id');await sim.evaluate(id=>__SIM__.simApi.pay(id),rid);
  await waitGone(sim,a.id,90000);
  await post('/api/barrier/out/clear-fault');await post('/api/manual/close-barrier',{gate:'out'});
  result.unauthorized.afterPayBarrier=(await snap()).barriers.out;

  // ---------- B) 已授权：付费完成后杆故障，人工抬杆自动离开 ----------
  await sim.evaluate(async()=>{await __SIM__.simApi.resetDemo();await __SIM__.controls.setSpeed(4);});
  const b=await spawnDispatchNoWait(sim,{vehicleId:'authorized-car',plate:'甘N35MYY',plateType:'blue',planDurationSec:600});
  await waitParked(sim,b.id,'甘N35MYY',90000);
  await sim.evaluate(()=>__SIM__.scheduler.startExit('authorized-car'));
  await sim.waitForFunction(()=>__SIM__.scheduler.vehicles.get('authorized-car')?.state==='wait_exit',null,{timeout:60000});
  let brid=await sim.evaluate(()=>__SIM__.scheduler.vehicles.get('authorized-car')?.meta?.recordId);
  if(brid==null) brid=await openRecordId('甘N35MYY');
  assert.ok(brid,'authorized car needs record id');
  await sim.evaluate(id=>__SIM__.simApi.pay(id),brid);
  await sim.waitForFunction(()=>__SIM__.scheduler.vehicles.get('authorized-car')?.meta?.exitAuthorized===true || __SIM__.scheduler.vehicles.get('authorized-car')?.state==='leaving',null,{timeout:30000});
  await post('/api/barrier/out/fault',{action:'inject',mode:'stuck_closed'});
  await admin.bringToFront();
  await admin.waitForSelector('#anomaly-card:not([hidden])',{timeout:20000});
  await admin.locator('#anomaly-raise-out').click();
  await admin.waitForTimeout(1200);
  await sim.bringToFront();
  await waitGone(sim,b.id,90000);
  const watcher=await stopGateWatcher(sim);
  assert.deepEqual(watcher.violations,[],'vehicle crossed closed barrier: '+JSON.stringify(watcher.violations));
  const after=await snap();
  assert.equal(String(after.barriers.out.state).toLowerCase(),'closed');
  await sim.screenshot({path:out+'/barrier-auth-authorized-left.png'});
  result.authorized={stateAfter:'gone',barrier:after.barriers.out,watcher};
  await post('/api/barrier/out/clear-fault').catch(()=>{});
  assert.deepEqual(errors,[]);
  fs.writeFileSync(out+'/barrier-authorization.json',JSON.stringify(result,null,2));
  console.log(JSON.stringify({unauthorized:result.unauthorized,authorized:{barrier:after.barriers.out,violations:watcher.violations.length},errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
