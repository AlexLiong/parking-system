// 2026-10-26 入口摄像头/道闸故障：人工录信息 + 人工抬杆 → 车正常进场；无穿杆。
// 旧的单独“人工授权卡”已从管理端删除；这里验证真实降级路径：
// A) 入口摄像头故障 → 相机 pending → 人工录入车牌（API，UI 能力保留）→ 车正常进场；
// B) 入口道闸故障 → 车 wait_entry → 管理端异常卡 #anomaly-raise-in 物理抬杆 → 车进场。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict'), crypto = require('crypto');
const { spawnDispatchNoWait, waitParked } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9636';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261031/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
const PLATE='粤F4K1CZ';

async function watchGate(page, gate){
  await page.evaluate((g)=>{window.__gv=[];window.__gt=setInterval(()=>{
    const gateObj=__SIM__.scene.gates[g];if(!gateObj)return;const center=gateObj.zoneCenter();const plane=center.z;
    for(const v of __SIM__.scheduler.vehicles.values()){
      if(!v?.pos)continue;if(Math.abs(v.pos.x-center.x)>2.6)continue;const half=Number(v.dims?.len||4.6)/2;
      const over=Math.min(v.pos.z+half-plane,plane-(v.pos.z-half));
      if(over>0.25&&!gateObj.isPassable)window.__gv.push({id:v.id,state:v.state,z:v.pos.z,over,gateState:gateObj.state});
    }
  },40);},gate);
}
async function stopWatch(page){return page.evaluate(()=>{clearInterval(window.__gt);return window.__gv||[];});}

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
  const admin=await browser.newPage({viewport:{width:1280,height:900}});
  admin.on('pageerror',e=>errors.push('admin:'+String(e)));
  await admin.addInitScript(t=>localStorage.setItem('parking.admin.token',t),token);
  await admin.goto(base+'/admin/#/monitor');
  const result={camera:{},barrier:{},errors};

  // ---------- A) 入口摄像头故障：相机 pending → 人工录入 ----------
  await sim.bringToFront();
  await sim.evaluate(async()=>{await __SIM__.simApi.resetDemo();await __SIM__.controls.setSpeed(4);});
  await watchGate(sim,'in');
  await admin.bringToFront();
  await admin.locator('#device-camera-fault-in').click();
  await sim.bringToFront();
  const a=await spawnDispatchNoWait(sim,{vehicleId:'entry-cam-car',plate:PLATE,plateType:'blue',planDurationSec:600});
  await sim.waitForFunction(()=>{
    const v=__SIM__.scheduler.vehicles.get('entry-cam-car');
    return !v || ['wait_entry','to_space'].includes(v.state);
  },null,{timeout:90000});
  let pend=null;
  for(let i=0;i<80;i++){
    const sk=await snap();pend=(sk.pending_manual||[]).find(p=>p.gate==='in');
    if(pend)break;await sim.waitForTimeout(500);
  }
  assert.ok(pend,'camera fault should create entry pending');
  await admin.bringToFront();
  await admin.waitForSelector('#device-control-card',{timeout:20000});
  await admin.waitForSelector('#pending-card:not([hidden])',{timeout:20000});
  await admin.screenshot({path:out+'/entry-camera-fault-admin.png',fullPage:true});
  // 人工录信息（API 能力保留；UI 已收敛为异常驱动）
  const registered=await post('/api/manual/register',{plate:PLATE,color:'blue'});
  assert.ok(registered.record_id || registered.space_no || registered.ok,'manual register result');
  await admin.bringToFront();
  await admin.locator('#device-camera-recover-in').click();
  await sim.bringToFront();
  await sim.waitForFunction((id)=>{
    const v=__SIM__.scheduler.vehicles.get(id);
    return !!v && ['to_space','parked','to_exit','wait_exit','leaving'].includes(v.state);
  },a.id,{timeout:90000});
  const camState=await sim.evaluate((id)=>__SIM__.scheduler.vehicles.get(id)?.state||'gone',a.id);
  assert.notEqual(camState,'wait_entry','manual register should let camera-fault car enter');
  const camRecs=await (await sim.request.get(base+'/api/records?plate='+encodeURIComponent(PLATE)+'&page_size=50',{headers})).json();
  assert.ok(Number(camRecs.total)>=1,'manual register should create entry record: '+JSON.stringify(registered));
  const camRecord=camRecs.items[0];
  await sim.screenshot({path:out+'/entry-camera-fault-entered.png'});
  result.camera={pending:pend,registered,camRecord,barrier:(await snap()).barriers.in};

  // ---------- B) 入口道闸故障：车 wait_entry → 异常卡物理抬杆 → 进场 ----------
  await sim.evaluate(async()=>{await __SIM__.simApi.resetDemo();await __SIM__.controls.setSpeed(4);});
  await admin.bringToFront();
  await admin.locator('#device-barrier-fault-in').click();
  await sim.bringToFront();
  const b=await spawnDispatchNoWait(sim,{vehicleId:'entry-gate-car',plate:'甘N35MYY',plateType:'blue',planDurationSec:600});
  await sim.waitForFunction(()=>{
    const v=__SIM__.scheduler.vehicles.get('entry-gate-car');
    return !v || ['wait_entry','to_space'].includes(v.state);
  },null,{timeout:90000});
  const before=await snap();
  assert.equal(before.barriers.in.passable,false,'fault barrier should be impassable');
  await admin.bringToFront();
  await admin.waitForSelector('#device-control-card',{timeout:20000});
  await admin.locator('#device-barrier-raise-in').waitFor();
  await admin.screenshot({path:out+'/entry-barrier-fault-admin.png',fullPage:true});
  await admin.locator('#device-barrier-raise-in').click();
  await admin.waitForTimeout(1200);
  await sim.bringToFront();
  await sim.waitForFunction((id)=>{
    const v=__SIM__.scheduler.vehicles.get(id);
    return !!v && ['to_space','parked','to_exit','wait_exit','leaving'].includes(v.state);
  },b.id,{timeout:90000});
  await admin.bringToFront();
  await admin.locator('#device-barrier-recover-in').click();
  await new Promise(r=>setTimeout(r,800));
  await admin.locator('#device-barrier-lower-in').click();
  const violations=await stopWatch(sim);
  const gateAfter=await snap();
  assert.deepEqual(violations,[],'entry car crossed closed barrier: '+JSON.stringify(violations));
  assert.equal(gateAfter.barriers.in.passable,false);
  await sim.screenshot({path:out+'/entry-barrier-fault-entered.png'});
  result.barrier={before:before.barriers.in,after:gateAfter.barriers.in,violations};
  assert.deepEqual(errors,[]);
  fs.writeFileSync(out+'/entry-fault-manual.json',JSON.stringify(result,null,2));
  console.log(JSON.stringify({camera:result.camera.barrier,barrier:result.barrier.after,violations:violations.length,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
