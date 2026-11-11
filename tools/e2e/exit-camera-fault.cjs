// 2026-10-26 出口摄像头故障：正常入场 → 停车 33 仿真分钟 → 注入出口摄像头故障
// → 出口待人工确认 → admin 输入车牌 → 正常计费（金额必须 >0）→ record out、出口杆开、车离开。
// speed=10 下 33 仿真分钟≈198s；全程真实浏览器+真实后端。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict'), crypto = require('crypto');
const { spawnDispatchNoWait, waitParked, waitGone } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9638';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261026/E2E/screenshots';
fs.mkdirSync(out, { recursive: true });
const PLATE = '粤F4K1CZ';
const PARK_SIM_SEC = 33 * 60;

(async () => {
  const browser = await chromium.launch({headless:true, args:(process.env.E2E_GPU
    ? ['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11','--disable-background-timer-throttling','--disable-renderer-backgrounding']
    : ['--enable-unsafe-swiftshader','--use-angle=swiftshader','--disable-background-timer-throttling','--disable-renderer-backgrounding']),
    ...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {})});
  try {
    const sim = await browser.newPage({viewport:{width:1280,height:900}});
    const errors=[]; sim.on('pageerror',e=>errors.push(String(e)));
    await sim.goto(base+'/sim/');
    await sim.waitForFunction(()=>__SIM__?.simApi?.rt?.sessionId,null,{timeout:90000});
    await sim.evaluate(async()=>{await __SIM__.simApi.resetDemo();await __SIM__.controls.setPaused(false);await __SIM__.controls.setSpeed(10);});
    const info=await spawnDispatchNoWait(sim,{vehicleId:'cam-fault-car',plate:PLATE,plateType:'blue',planDurationSec:3600});
    await waitParked(sim,info.id,PLATE,90000);
    let openRec=null;
    const simToken=await sim.evaluate(()=>__SIM__.simApi.rt.token());
    for(let i=0;i<60;i++){
      const recs=await (await sim.request.get(base+'/api/records?status=in&plate='+encodeURIComponent(PLATE)+'&page_size=50',
        {headers:{Authorization:'Bearer '+simToken}})).json();
      openRec=(recs.items||[])[0]||null;
      if(openRec&&Number.isFinite(Number(openRec.entry_sim_sec))) break;
      await sim.waitForTimeout(500);
    }
    assert.ok(openRec,'need open parking record');
    const entrySim=Number(openRec.entry_sim_sec);
    assert.ok(Number.isFinite(entrySim));
    const targetSim=entrySim+PARK_SIM_SEC;
    const started=Date.now();
    let nowSim=0;
    while(Date.now()-started<300000){
      nowSim=Number(await sim.evaluate(async()=>Number((await __SIM__.simApi.rest.snapshot()).simulation.sim_time)));
      if(nowSim>=targetSim) break;
      await sim.waitForTimeout(1000);
    }
    assert.ok(nowSim>=targetSim,`parked 33 sim minutes not reached: ${nowSim} < ${targetSim}`);
    const paused=await sim.evaluate(()=>__SIM__.controls.isPaused());
    if(paused) await sim.evaluate(()=>__SIM__.controls.setPaused(false));
    await sim.screenshot({path:out+'/exit-cam-fault-parked-33min.png'});

    // 真实 UI 注入出口摄像头故障，然后让车进入出口流程
    await sim.locator('#btn-cam-fault-out').click();
    await sim.waitForTimeout(800);
    const camState=await (await sim.request.get(base+'/api/camera/out/status')).json().catch(()=>null);
    await sim.evaluate(()=>__SIM__.scheduler.startExit('cam-fault-car'));
    await sim.waitForFunction(()=>{
      const v=__SIM__.scheduler.vehicles.get('cam-fault-car');
      return !v || ['to_exit','wait_exit'].includes(v.state);
    },null,{timeout:60000});
    // 等后端 camera_fault pending
    let pending=null;
    for(let i=0;i<120;i++){
      const snap=await sim.evaluate(async()=>await __SIM__.simApi.rest.snapshot());
      pending=(snap.pending_manual||[]).find(p=>p.gate==='out');
      if(pending) break;
      await sim.waitForTimeout(500);
    }
    assert.ok(pending,'exit camera fault should create out pending');
    assert.equal(pending.reason,'camera_fault');

    // admin 真实 UI 输入车牌并正常计费
    const login=await sim.request.post(base+'/api/auth/login',{data:{username:'admin',password:'admin123'}});
    assert.equal(login.status(),200);
    const token=(await login.json()).token;
    const admin=await browser.newPage({viewport:{width:1280,height:900}});
    admin.on('pageerror',e=>errors.push('admin:'+String(e)));
    let confirmResp=null,quoteResponses=[];
    admin.on('response',async r=>{
      if(r.url().includes('/api/manual/confirm')&&r.request().method()==='POST'){try{confirmResp=await r.json()}catch(_){}}
      if(r.url().includes('/api/quote')){try{quoteResponses.push({status:r.status(),url:r.url(),body:(await r.text()).slice(0,500)})}catch(_){}}
    });
    await admin.addInitScript(t=>localStorage.setItem('parking.admin.token',t),token);
    await admin.goto(base+'/admin/#/monitor');
    await admin.waitForSelector('#pending-card:not([hidden])',{timeout:20000});
    await admin.waitForTimeout(500);
    const input=admin.locator('#pending-card input[data-pending-id]').first();
    await input.fill(PLATE);
    // 初始 feeBox 是“正常费用：—”，必须等实际 quote 数字出现（不能把占位符当成功）
    await admin.waitForFunction(()=>{
      const t=document.querySelector('#pending-card .pending-fee')?.textContent||'';
      return t.includes('正常费用') && /[0-9]/.test(t);
    },null,{timeout:25000});
    const feeText=await admin.locator('#pending-card .pending-fee').first().innerText();
    let directQuote=null;
    if(feeText.includes('—')){
      const qr=await admin.request.get(base+'/api/quote?plate='+encodeURIComponent(PLATE),{headers:{Authorization:'Bearer '+token}});
      directQuote={status:qr.status(),body:(await qr.text()).slice(0,800)};
      console.log('QUOTE_DEBUG '+JSON.stringify({feeText,quoteResponses,directQuote}));
    }
    assert.match(feeText,/正常费用：¥?\s*\d/,'fee must be >0: '+feeText);
    assert.ok(!/正常费用：¥?\s*0(?:\.0+)?(?:\s|·|$)/.test(feeText),'fee must be non-zero: '+feeText);
    await admin.screenshot({path:out+'/exit-cam-fault-pending-fee.png',fullPage:true});
    await admin.getByRole('button',{name:'正常计费',exact:true}).click();
    await admin.waitForFunction(()=>document.getElementById('pending-card')?.hidden===true,null,{timeout:20000});
    await admin.screenshot({path:out+'/exit-cam-fault-charged.png',fullPage:true});
    assert.ok(confirmResp,'manual/confirm response captured');
    assert.ok(Number(confirmResp.settlement?.amount_paid)>0,'settlement amount must be >0: '+JSON.stringify(confirmResp.settlement));
    const record=await (await sim.request.get(base+`/api/records/${confirmResp.settlement.record_id}`,{headers:{Authorization:'Bearer '+token}})).json();
    assert.equal(record.status,'out');
    assert.ok(Number(record.duration_sim_sec)>=PARK_SIM_SEC-30,'duration_sim_sec '+record.duration_sim_sec);
    await sim.bringToFront();
    await waitGone(sim,info.id,90000);
    await sim.waitForFunction(async()=>{const s=await __SIM__.simApi.rest.snapshot();return !s.pending_manual.length;},null,{timeout:30000});
    const final=await sim.evaluate(async()=>({snap:await __SIM__.simApi.rest.snapshot(),barriers:__SIM__.dump().barriers}));
    assert.equal(final.snap.layout.free_total,80);
    assert.equal(final.barriers.out.state,'closed');
    await sim.screenshot({path:out+'/exit-cam-fault-left.png'});
    assert.deepEqual(errors,[]);
    const artifact={entrySim,targetSim,nowSim,feeText,camState,pending,settlement:confirmResp.settlement,record,barriers:final.barriers,errors};
    fs.writeFileSync(out+'/exit-camera-fault.json',JSON.stringify(artifact,null,2));
    console.log(JSON.stringify({plate:PLATE,minutes:record.minutes,durationSim:record.duration_sim_sec,
      amount:confirmResp.settlement.amount_paid,feeText,barrierOut:final.barriers.out.state,errors}));
  } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
