// 2026-10-26 sim 控件：重置 force=true 清场、出口摄像头故障/恢复按钮、下一事件合并（出场/缴费）。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9642';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261026/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true,args:(process.env.E2E_GPU
   ?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11','--disable-background-timer-throttling']
   :['--enable-unsafe-swiftshader','--use-angle=swiftshader','--disable-background-timer-throttling']),
   ...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];page.on('pageerror',e=>errors.push(String(e)));
  let resetCalls=[];
  page.on('request',r=>{if(r.url().includes('/api/demo/reset')&&r.method()==='POST'){try{resetCalls.push(JSON.parse(r.postData()||'{}'))}catch(_){}}});
  await page.goto(base+'/sim/');
  await page.waitForFunction(()=>__SIM__?.simApi?.rt?.sessionId && __SIM__?.scenario?.ready,null,{timeout:90000});
  // 默认剧本下一事件：合并“下一事件 + 预计出场/缴费”，不能有独立 #scn-forecast
  assert.equal(await page.locator('#scn-forecast').count(),0,'#scn-forecast must be removed');
  const nextLoaded=await page.locator('#scn-next').innerText();
  assert.match(nextLoaded,/下一事件/,'next event line required: '+nextLoaded);
  await page.evaluate(()=>__SIM__.controls.setSpeed(10));
  await page.locator('#btn-play').click();
  await page.waitForFunction(()=>__SIM__.scenario.clock>0||__SIM__.scheduler.vehicles.size>0,null,{timeout:20000});
  await page.waitForTimeout(1200);
  const nextPlaying=await page.locator('#scn-next').innerText();
  assert.match(nextPlaying,/下一事件/);
  assert.match(nextPlaying,/预计出场|缴费|预计/,'playing next event line should include forecast: '+nextPlaying);
  const nextText=nextPlaying;
  await page.screenshot({path:out+'/sim-controls-scenario-next.png'});

  // 出口摄像头故障/恢复按钮
  await page.locator('#btn-cam-fault-out').click();
  await page.waitForFunction(()=>document.body.innerText.includes('出口摄像头故障')||document.body.innerText.includes('需人工'),null,{timeout:10000});
  const faultText=await page.locator('body').innerText();
  assert.match(faultText,/出口摄像头/,'camera fault HUD text');
  await page.screenshot({path:out+'/sim-controls-camera-out-fault.png'});
  await page.locator('#btn-cam-recover-out').click();
  await page.waitForFunction(()=>document.body.innerText.includes('出口摄像头已恢复')||document.body.innerText.includes('恢复'),null,{timeout:10000});
  const cam=await (await page.request.get(base+'/api/camera/out/status')).json().catch(()=>null);
  await page.screenshot({path:out+'/sim-controls-camera-out-recovered.png'});

  // 重置：请求 force=true，清场/时钟/闸杆
  const before=await page.evaluate(async()=>({snap:await __SIM__.simApi.rest.snapshot(),clock:__SIM__.scenario.clock,
    vehicles:__SIM__.scheduler.vehicles.size,gates:__SIM__.dump().barriers}));
  assert.ok(before.vehicles>0 || before.clock>0,'scenario should be running before reset');
  await page.locator('#btn-reset').click();
  await page.waitForFunction(()=>__SIM__.scheduler.vehicles.size===0 && __SIM__.scenario.clock===0,null,{timeout:30000});
  await page.waitForTimeout(600);
  const after=await page.evaluate(async()=>({snap:await __SIM__.simApi.rest.snapshot(),clock:__SIM__.scenario.clock,
    vehicles:__SIM__.scheduler.vehicles.size,gates:__SIM__.dump().barriers}));
  assert.equal(after.vehicles,0);assert.equal(after.clock,0);
  assert.equal(after.gates.in.state,'closed');assert.equal(after.gates.out.state,'closed');
  assert.ok(resetCalls.some(b=>b.force===true),'sim reset must send force=true: '+JSON.stringify(resetCalls));
  await page.screenshot({path:out+'/sim-controls-reset.png'});
  assert.deepEqual(errors,[]);
  const artifact={nextText,nextPlaying,faultText: faultText.slice(-1200),before,after,resetCalls,errors};
  fs.writeFileSync(out+'/sim-controls.json',JSON.stringify(artifact,null,2));
  console.log(JSON.stringify({nextText,nextPlaying,resetForce:resetCalls.map(b=>b.force),afterGates:after.gates,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
