// 2026-11-04 N1：暂停语义（未播放/播放中/finished）、任意跟随保持最后位置、
// duration=入口道闸→出口道闸总时长（新字段，非旧 t+duration）。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict');
const { spawnDispatchNoWait, waitParked } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9660';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261104/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
const SHORT={name:'N1 短剧本',description:'暂停语义',seed:20261104,time_unit:'sim_sec',
  vehicles:[{vid:'N1-P',t:1,plate:'粤F4K1CZ',plate_type:'blue',duration:5}],
  steps:[{t:1,action:'spawn',vid:'N1-P',plate:'粤F4K1CZ',plate_type:'blue'}]};
(async()=>{
 const browser=await chromium.launch({headless:true,args:(process.env.E2E_GPU
   ?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11','--disable-background-timer-throttling']
   :['--enable-unsafe-swiftshader','--use-angle=swiftshader','--disable-background-timer-throttling']),
   ...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  await page.goto(base+'/sim/');await page.waitForFunction(()=>__SIM__?.simApi?.rt?.sessionId,null,{timeout:90000});
  const login=await page.request.post(base+'/api/auth/login',{data:{username:'admin',password:'admin123'}});
  assert.equal(login.status(),200);const token=(await login.json()).token,headers={Authorization:'Bearer '+token};
  async function simTime(){return page.evaluate(()=>Number(__SIM__.dump().simTime));}
  async function scenario(){return page.evaluate(()=>({playing:__SIM__.scenario.playing,clock:__SIM__.scenario.clock,finished:__SIM__.scenario.finished,paused:__SIM__.controls.isPaused()}));}
  const result={};

  // ---------- 1) 未播放：暂停/继续不启动剧本 ----------
  await page.evaluate(()=>__SIM__.controls.setSpeed(10));
  let s=await scenario();assert.equal(s.playing,false);assert.equal(s.clock,0);
  await page.locator('#btn-pause').click();
  await page.waitForFunction(()=>__SIM__.controls.isPaused()===true,null,{timeout:10000});
  const pauseStart=await simTime();await page.waitForTimeout(900);const frozen=await simTime();
  s=await scenario();assert.equal(s.playing,false,'never-played pause must not start scenario');
  assert.ok(Math.abs(frozen-pauseStart)<0.2,'simTime freeze while paused: '+pauseStart+'->'+frozen);
  await page.locator('#btn-pause').click();
  await page.waitForFunction(()=>__SIM__.controls.isPaused()===false,null,{timeout:10000});
  await page.waitForTimeout(700);s=await scenario();
  assert.equal(s.playing,false,'never-played resume must not start scenario');assert.equal(s.clock,0);
  assert.ok(await simTime()>frozen,'simTime advances after resume');
  await page.screenshot({path:out+'/pause-never-played.png'});
  result.neverPlayed={...s};

  // 短暂剧本（便于快速 finished）；PUT 后重载
  const put=await page.request.put(base+'/api/admin/scenario',{headers,data:SHORT});
  assert.ok(put.ok(),await put.text());
  await page.reload();await page.waitForFunction(()=>__SIM__?.scenario?.ready===true,null,{timeout:90000});
  await page.evaluate(()=>__SIM__.controls.setSpeed(10));
  // ---------- 2) 播放中暂停/继续 ----------
  await page.locator('#btn-play').click();
  await page.waitForFunction(()=>__SIM__.scenario.playing===true&&__SIM__.scenario.clock>0,null,{timeout:20000});
  await page.locator('#btn-pause').click();
  await page.waitForFunction(()=>__SIM__.controls.isPaused()===true&&__SIM__.scenario.playing===false,null,{timeout:10000});
  const playingPauseStart=await scenario();await page.waitForTimeout(900);const playingPauseEnd=await scenario();
  assert.equal(playingPauseEnd.clock,playingPauseStart.clock,'scenario clock must freeze during pause');
  await page.locator('#btn-pause').click();
  await page.waitForFunction(()=>__SIM__.controls.isPaused()===false&&__SIM__.scenario.playing===true,null,{timeout:10000});
  const resumedClock=(await scenario()).clock;assert.ok(resumedClock>playingPauseEnd.clock,'resume advances scenario clock');
  await page.screenshot({path:out+'/pause-playing.png'});
  result.playing={pauseAt:playingPauseStart.clock,resumedAt:resumedClock};

  // ---------- 3) finished 后暂停/继续不重播 ----------
  await page.waitForFunction(()=>__SIM__.scenario.finished===true,null,{timeout:60000});
  const finishedBefore=await scenario();assert.equal(finishedBefore.finished,true);
  await page.locator('#btn-pause').click();
  await page.waitForFunction(()=>__SIM__.controls.isPaused()===true,null,{timeout:10000});
  const finFrozen=await scenario();await page.waitForTimeout(700);const finFrozen2=await scenario();
  assert.equal(finFrozen2.clock,finFrozen.clock);
  await page.locator('#btn-pause').click();
  await page.waitForFunction(()=>__SIM__.controls.isPaused()===false,null,{timeout:10000});
  await page.waitForTimeout(700);const afterResume=await scenario();
  assert.equal(afterResume.playing,false,'finished resume must not replay');
  assert.equal(afterResume.finished,true);
  assert.equal(afterResume.clock,finFrozen.clock,'finished clock must stay');
  await page.screenshot({path:out+'/pause-finished.png'});
  result.finished={...afterResume};

  // ---------- 4) 任意跟随：选择 parked/移动目标，离场后保持最后位置 ----------
  await page.evaluate(async()=>{await __SIM__.simApi.resetDemo();await __SIM__.controls.setSpeed(10);});
  const car=await spawnDispatchNoWait(page,{vehicleId:'N1-FOLLOW',plate:'甘N35MYY',plateType:'blue',planDurationSec:600});
  await waitParked(page,car.id,'甘N35MYY',90000);
  await page.waitForFunction(()=>document.querySelectorAll('#follow-vehicle option').length>1,null,{timeout:10000});
  await page.selectOption('#follow-vehicle',{value:car.id});
  await page.waitForTimeout(600);
  let follow=await page.evaluate(()=>({sel:document.getElementById('follow-vehicle').value,target:__SIM__.rig.followTarget?.id||null,mode:__SIM__.rig.mode,hold:!!__SIM__.rig._followHold,pose:[__SIM__.rig.camera?.position?.x,__SIM__.rig.camera?.position?.z]}));
  assert.equal(follow.sel,car.id);assert.equal(follow.target,car.id);assert.equal(follow.mode,'follow');
  await page.screenshot({path:out+'/follow-parked.png'});
  const beforeLeave=await page.evaluate(()=>({x:__SIM__.rig.camera.position.x,z:__SIM__.rig.camera.position.z}));
  await page.evaluate(id=>__SIM__.scheduler.startExit(id),car.id);
  await page.waitForFunction(id=>!__SIM__.scheduler.vehicles.get(id),car.id,{timeout:90000});
  await page.waitForTimeout(900);
  const afterLeave=await page.evaluate(()=>({sel:document.getElementById('follow-vehicle').value,target:__SIM__.rig.followTarget?.id||null,hold:!!__SIM__.rig._followHold,
    hud:document.body.innerText.includes('保持最后位置')||document.body.innerText.includes('已离场'),
    pose:[__SIM__.rig.camera.position.x,__SIM__.rig.camera.position.z]}));
  await page.waitForTimeout(1200);
  const afterHold=await page.evaluate(()=>({hold:!!__SIM__.rig._followHold,
    pose:[__SIM__.rig.camera.position.x,__SIM__.rig.camera.position.z]}));
  assert.equal(afterLeave.sel,car.id,'follow selection retained after target left');
  assert.equal(afterLeave.target,null,'stale followTarget must be cleared');
  assert.equal(afterLeave.hold,true,'camera must hold last position');
  assert.ok(Math.abs(afterHold.pose[0]-afterLeave.pose[0])<0.5&&Math.abs(afterHold.pose[1]-afterLeave.pose[1])<0.5,
    'camera holds last position after target removed: '+JSON.stringify({afterLeave,afterHold}));
  assert.ok(afterLeave.hud,'HUD should say target left/hold');
  await page.screenshot({path:out+'/follow-target-left.png'});
  result.follow={initial:follow,afterLeave};

  // ---------- 5) duration = 入口道闸→出口道闸总时长 ----------
  await page.evaluate(async()=>{await __SIM__.simApi.resetDemo();await __SIM__.controls.setSpeed(10);});
  const durCar=await spawnDispatchNoWait(page,{vehicleId:'N1-DURATION',plate:'粤F4K1CZ',plateType:'blue',planDurationSec:120,autoExitDurationSec:120});
  await waitParked(page,durCar.id,'粤F4K1CZ',90000);
  const meta=await page.evaluate(id=>{const v=__SIM__.scheduler.vehicles.get(id);return v?{...v.meta}:null},durCar.id);
  assert.ok(meta.entryGateAtSim!=null,'entryGateAtSim recorded');
  assert.ok(Math.abs(Number(meta.scheduledExitAt)-(Number(meta.entryGateAtSim)+120))<0.5,
    'scheduledExitAt must be entryGate+duration: '+JSON.stringify(meta));
  assert.ok(Number(meta.entryTravel)>0);
  assert.ok(Math.abs(Number(meta.scheduledExitAt)-(Number(meta.parkedAtSim)+120))>5,
    'new semantics must differ from old parkedAt+duration');
  // 在车辆删除前轮询 meta.gateToGateActual（出口过闸时刻）
  let durationActual=null,lastMeta=meta;
  const durDeadline=Date.now()+120000;
  while(Date.now()<durDeadline){
    const st=await page.evaluate(id=>{const v=__SIM__.scheduler.vehicles.get(id);return v?{state:v.state,meta:{...v.meta}}:null},durCar.id);
    if(st){lastMeta=st.meta;if(Number.isFinite(Number(st.meta.gateToGateActual)))durationActual=Number(st.meta.gateToGateActual);}
    if(!st) break;
    await page.waitForTimeout(100);
  }
  assert.ok(durationActual!=null,'gateToGateActual should be recorded at exit');
  assert.ok(Math.abs(durationActual-120)<=75,'entry→exit actual duration should be explainable near 120s, got '+durationActual);
  result.duration={meta:lastMeta,gateToGateActual:durationActual};
  fs.writeFileSync(out+'/sim-n1.json',JSON.stringify(result,null,2));
  assert.deepEqual(errors,[]);
  await page.screenshot({path:out+'/duration-exit.png'});
  console.log(JSON.stringify({neverPlayed:result.neverPlayed,playing:result.playing,finished:result.finished,
    follow:{sel:afterLeave.sel,hold:afterLeave.hold},duration:{entryGateAtSim:meta.entryGateAtSim,entryTravel:meta.entryTravel,
      scheduledExitAt:meta.scheduledExitAt,autoExitAt:meta.autoExitAt,gateToGateActual:durationActual,errors}}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
