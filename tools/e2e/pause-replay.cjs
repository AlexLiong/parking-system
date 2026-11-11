// 2026-10-31 暂停/重播：短剧本 finished 后暂停仍可用，simTime 冻结/恢复；重播复位。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict'), crypto = require('crypto');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9653';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261031/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
const SHORT={
  name:'E2E 暂停重播（1 车）',description:'暂停/重播验证',seed:20261031,time_unit:'sim_sec',
  vehicles:[{vid:'P1',t:1,plate:'粤F4K1CZ',plate_type:'blue',duration:2}],
  steps:[{t:1,action:'spawn',vid:'P1',plate:'粤F4K1CZ',plate_type:'blue'}],
};
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
  const put=await page.request.put(base+'/api/admin/scenario',{headers,data:SHORT});
  assert.ok(put.ok(),'PUT scenario: '+await put.text());
  await page.reload();await page.waitForFunction(()=>__SIM__?.scenario?.ready===true,null,{timeout:90000});
  await page.evaluate(()=>__SIM__.controls.setSpeed(10));
  await page.locator('#btn-play').click();
  await page.waitForFunction(()=>__SIM__.scenario.finished===true,null,{timeout:60000});
  const pause=page.locator('#btn-pause');
  assert.equal(await pause.isEnabled(),true,'pause must be enabled after finished');
  const before=await page.evaluate(async()=>({sim:Number(__SIM__.dump().simTime),paused:__SIM__.controls.isPaused(),clock:__SIM__.scenario.clock}));
  await pause.click();
  await page.waitForFunction(()=>__SIM__.controls.isPaused()===true,null,{timeout:10000});
  // 等后端仿真也进入 paused（API 异步），再取冻结基线
  await page.waitForFunction(async()=>{const s=await __SIM__.simApi.rest.snapshot();return s.simulation.paused===true;},null,{timeout:10000});
  await page.waitForTimeout(800); // 让最后一个网络 tick 落地
  const pauseStart=await page.evaluate(async()=>({sim:Number(__SIM__.dump().simTime),paused:__SIM__.controls.isPaused(),clock:__SIM__.scenario.clock}));
  await page.waitForTimeout(1200);
  const frozen=await page.evaluate(async()=>({sim:Number(__SIM__.dump().simTime),paused:__SIM__.controls.isPaused(),clock:__SIM__.scenario.clock}));
  assert.ok(Math.abs(frozen.sim-pauseStart.sim)<0.15,'simTime must freeze while paused: '+pauseStart.sim+' -> '+frozen.sim);
  assert.equal(frozen.clock,pauseStart.clock,'scenario clock must freeze');
  await page.screenshot({path:out+'/pause-finished.png'});
  await pause.click();
  await page.waitForFunction(()=>__SIM__.controls.isPaused()===false,null,{timeout:10000});
  await page.waitForTimeout(1000);
  const resumed=await page.evaluate(async()=>({sim:Number(__SIM__.dump().simTime),clock:__SIM__.scenario.clock}));
  assert.ok(resumed.sim>frozen.sim,'simTime must advance after resume');
  // 重播（finished 后按钮文案 ↻ 重播剧本 / 播放；使用 js 调用保证重播）
  const replay=await page.evaluate(()=>{__SIM__.scenario.reset();__SIM__.scenario.play();return true;});
  await page.waitForFunction(()=>__SIM__.scenario.finished===false&&__SIM__.scenario.playing===true,null,{timeout:15000});
  await page.screenshot({path:out+'/pause-replay.png'});
  // restore builtin scenario
  await page.request.post(base+'/api/admin/scenario/reset',{headers,data:{}});
  assert.deepEqual(errors,[]);
  const artifact={before,frozen,resumed,replay,errors};
  fs.writeFileSync(out+'/pause-replay.json',JSON.stringify(artifact,null,2));
  console.log(JSON.stringify({before,frozen,resumed,replay,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
