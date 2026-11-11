// 2026-10-31 设备控制/管理端正常态：设备控制卡常显（入口/出口道闸抬/落/故障/恢复 + 摄像头故障/恢复），
// 待确认仅在真实异常时出现；无旧人工授权大卡片；事件流无裸 JSON；重置 force。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict'),
  { spawnDispatchNoWait } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9655';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261031/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
const DEVICE_IDS=['device-barrier-raise-in','device-barrier-lower-in','device-barrier-fault-in','device-barrier-recover-in',
 'device-barrier-raise-out','device-barrier-lower-out','device-barrier-fault-out','device-barrier-recover-out',
 'device-camera-fault-in','device-camera-recover-in','device-camera-fault-out','device-camera-recover-out'];
(async()=>{
 const browser=await chromium.launch({headless:true,args:(process.env.E2E_GPU
   ?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11','--disable-background-timer-throttling']
   :['--enable-unsafe-swiftshader','--use-angle=swiftshader','--disable-background-timer-throttling']),
   ...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 try{
  const sim=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];
  sim.on('pageerror',e=>errors.push(String(e)));
  await sim.goto(base+'/sim/');await sim.waitForFunction(()=>__SIM__?.simApi?.rt?.sessionId,null,{timeout:90000});
  const login=await sim.request.post(base+'/api/auth/login',{data:{username:'admin',password:'admin123'}});
  assert.equal(login.status(),200);const token=(await login.json()).token,headers={Authorization:'Bearer '+token};
  const admin=await browser.newPage({viewport:{width:1280,height:950}});
  admin.on('pageerror',e=>errors.push('admin:'+String(e)));
  await admin.addInitScript(t=>localStorage.setItem('parking.admin.token',t),token);
  await admin.goto(base+'/admin/#/monitor');
  await admin.waitForSelector('#device-control-card',{timeout:20000});
  await admin.waitForTimeout(1200);
  assert.equal(await admin.locator('#device-control-card').isVisible(),true);
  for(const id of DEVICE_IDS) assert.ok(await admin.locator('#'+id).count()>0,'device button missing '+id);
  assert.equal(await admin.locator('#authorization-card').count(),0);
  assert.equal(await admin.getByRole('button',{name:'人工出场',exact:true}).count(),0);
  assert.equal(await admin.locator('#pending-card').isVisible(),false,'normal pending must be hidden');
  const bodyText=await admin.locator('body').innerText();
  for(const frag of ['本次识别实际使用','识别演示 · 不产生入场/放行','人工授权（入口/出口）']) assert.ok(!bodyText.includes(frag),'stale text '+frag);
  await admin.screenshot({path:out+'/device-control-normal.png',fullPage:true});
  async function snap(){return (await admin.request.get(base+'/api/state/snapshot',{headers})).json();}
  async function waitBarrier(gate,pred,timeout=8000){
    const t0=Date.now();
    while(Date.now()-t0<timeout){const s=await snap();if(pred(s.barriers[gate]))return s;await admin.waitForTimeout(150);}
    throw new Error('barrier wait failed '+gate+': '+JSON.stringify((await snap()).barriers[gate]));
  }
  for(const gate of ['in','out']){
    await admin.locator(`#device-barrier-raise-${gate}`).click();await admin.waitForTimeout(700);
    let s=await snap();assert.equal(s.barriers[gate].passable,true,gate+' raise passable');
    await admin.locator(`#device-barrier-lower-${gate}`).click();
    s=await waitBarrier(gate,b=>String(b.state).toLowerCase()==='closed');
    assert.equal(String(s.barriers[gate].state).toLowerCase(),'closed',gate+' lower closed');
    await admin.locator(`#device-barrier-fault-${gate}`).click();await admin.waitForTimeout(700);
    s=await snap();assert.equal(String(s.barriers[gate].state).toUpperCase(),'FAULT',gate+' fault');
    await admin.locator(`#device-barrier-recover-${gate}`).click();
    s=await waitBarrier(gate,b=>String(b.state).toUpperCase()!=='FAULT');
    assert.notEqual(String(s.barriers[gate].state).toUpperCase(),'FAULT',gate+' recovered');
    await admin.locator(`#device-barrier-lower-${gate}`).click().catch(()=>{});await admin.waitForTimeout(500);
    await admin.locator(`#device-camera-fault-${gate}`).click();await admin.waitForTimeout(700);
    s=await snap();assert.equal(s.cameras[gate].online,false,gate+' camera fault');
    await admin.locator(`#device-camera-recover-${gate}`).click();await admin.waitForTimeout(700);
    s=await snap();assert.equal(s.cameras[gate].online,true,gate+' camera recovered');
  }
  await admin.screenshot({path:out+'/device-control-anomalies.png',fullPage:true});
  const feed=await admin.locator('.feed').innerText().catch(()=> '');
  assert.ok(!feed.includes('{"')&&!feed.includes('"type"'),'event feed raw JSON');
  // 真实异常才出现待确认：入口摄像头故障 + 派车 → pending
  await sim.bringToFront();
  await admin.locator('#device-camera-fault-in').click();
  const car=await spawnDispatchNoWait(sim,{vehicleId:'device-pending-car',plate:'粤F4K1CZ',plateType:'blue',planDurationSec:600});
  await admin.bringToFront();
  await admin.waitForSelector('#pending-card:not([hidden])',{timeout:30000});
  await admin.screenshot({path:out+'/device-control-pending-abnormal.png',fullPage:true});
  const pending=await (await admin.request.get(base+'/api/manual/pending?status=pending',{headers})).json();
  const item=(pending.items||[])[0];assert.ok(item,'pending item');
  const rej=await admin.request.post(base+'/api/manual/reject',{headers,data:{id:item.id,reason:'E2E 设备测试清理'}});
  assert.ok(rej.ok(),await rej.text());
  await admin.locator('#device-camera-recover-in').click();
  await admin.waitForTimeout(1000);
  let resetBody=null;admin.on('request',r=>{if(r.url().includes('/api/demo/reset')&&r.method()==='POST'){try{resetBody=JSON.parse(r.postData()||'{}')}catch(_){}}});
  await admin.locator('#ops-reset').click();
  await admin.getByRole('button',{name:'重置',exact:true}).last().click();
  await admin.waitForFunction(()=>/重置完成/.test(document.getElementById('ops-reset-report')?.textContent||''),null,{timeout:20000});
  assert.equal(resetBody?.force,true);
  await admin.screenshot({path:out+'/device-control-reset.png',fullPage:true});
  assert.deepEqual(errors,[]);
  const artifact={deviceIds:DEVICE_IDS,pending:item,resetForce:resetBody?.force,feedText:feed.slice(0,2000),errors};
  fs.writeFileSync(out+'/device-controls.json',JSON.stringify(artifact,null,2));
  console.log(JSON.stringify({deviceButtons:DEVICE_IDS.length,pendingShown:true,resetForce:resetBody?.force,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
