// 2026-10-31 黑名单出场：0 费用、0 普通记录；遗留开放记录转 abnormal 并释放；绕行 blacklist-gate 正常。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict'), crypto = require('crypto');
const { spawnDispatchNoWait, waitParked } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9652';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261031/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
const PLATE='苏B2S39K';
(async()=>{
 const browser=await chromium.launch({headless:true,args:(process.env.E2E_GPU
   ?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11','--disable-background-timer-throttling']
   :['--enable-unsafe-swiftshader','--use-angle=swiftshader','--disable-background-timer-throttling']),
   ...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[],payRequests=[];
  page.on('pageerror',e=>errors.push(String(e)));
  page.on('request',r=>{if(r.url().includes('/api/pay'))payRequests.push(r.url())});
  await page.goto(base+'/sim/');await page.waitForFunction(()=>__SIM__?.simApi?.rt?.sessionId,null,{timeout:90000});
  const login=await page.request.post(base+'/api/auth/login',{data:{username:'admin',password:'admin123'}});
  assert.equal(login.status(),200);const token=(await login.json()).token,headers={Authorization:'Bearer '+token};
  async function post(path,data){const r=await page.request.post(base+path,{headers,data});return {status:r.status(),body:await r.json().catch(()=>null),text:await r.text().catch(()=> '')};}
  async function records(plate,status=''){const r=await page.request.get(base+'/api/records?plate='+encodeURIComponent(plate)+'&page_size=50'+(status?'&status='+status:''),{headers});assert.equal(r.status(),200);return r.json();}
  await page.evaluate(async()=>{await __SIM__.simApi.resetDemo();await __SIM__.controls.setSpeed(10);});
  // 1) 黑名单前正常入场，形成遗留开放记录
  const car=await spawnDispatchNoWait(page,{vehicleId:'blk-exit-car',plate:PLATE,plateType:'blue',planDurationSec:600});
  await waitParked(page,car.id,PLATE,90000);
  const before=await records(PLATE);
  assert.equal((before.items||[])[0].status,'in');
  const rid=before.items[0].id;
  // 2) 加入黑名单后出场识别
  const add=await post('/api/lists',{plate:PLATE,list_type:'black',note:'E2E blacklist exit'});
  assert.ok([200,201].includes(add.status),JSON.stringify(add));
  await page.evaluate(id=>__SIM__.scheduler.startExit(id),car.id);
  // 等待后端 blacklist_exit 清理；3D 不一定获业务放行（不能收费）
  let rec=null;
  for(let i=0;i<60;i++){const r=await records(PLATE);rec=(r.items||[])[0]||null;if(rec&&rec.status!=='in')break;await page.waitForTimeout(500);}
  assert.ok(rec,'blacklist record');
  assert.equal(rec.status,'abnormal','leftover record should be abnormal, got '+rec.status);
  assert.equal(rec.aborted_reason,'blacklist_exit');
  assert.ok(!rec.payment,'blacklist exit must have no PaymentRecord');
  assert.equal(Number(rec.amount_due||0),0);
  const snap=await page.evaluate(async()=>await __SIM__.simApi.rest.snapshot());
  assert.equal(snap.layout.free_total,80,'space must be released');
  assert.ok(rec.exit_photo,'exit evidence photo should be stored');
  await page.screenshot({path:out+'/blacklist-exit-cleared.png'});
  // 3) 清理残留车（黑名单出场无业务授权，车可能停在杆区）后验证绕行闸杆 out open/close；不产生费用/记录
  await page.request.post(base+'/api/demo/reset',{headers,data:{request_id:crypto.randomUUID(),mode:'soft',new_session:true,force:true}});
  await page.waitForTimeout(800);
  const openG=await post('/api/sim/blacklist-gate',{vehicle_id:'blk-exit-car',plate:PLATE,gate:'out',action:'open'});
  assert.equal(openG.status,200,JSON.stringify(openG));
  await page.waitForTimeout(1200);
  const closeG=await post('/api/sim/blacklist-gate',{vehicle_id:'blk-exit-car',plate:PLATE,gate:'out',action:'close'});
  assert.equal(closeG.status,200,JSON.stringify(closeG));
  const after=await records(PLATE);
  assert.ok(!(after.items||[]).some(r=>r.status==='out'),'must not create normal out record');
  assert.equal((after.items||[]).length,1,'only the cleared abnormal audit record');
  assert.equal(payRequests.length,0,'blacklist exit must not call /api/pay');
  const acts=await (await page.request.get(base+'/api/barrier/actions?limit=200',{headers})).json();
  const blk=(acts.items||[]).filter(a=>a.reason==='blacklist_bypass'&&a.gate==='out');
  assert.ok(blk.some(a=>a.cmd==='open')&&blk.some(a=>a.cmd==='close'),'blacklist bypass gate audit');
  await page.screenshot({path:out+'/blacklist-exit-bypass.png'});
  assert.deepEqual(errors,[]);
  const artifact={record:rec,recordsAfter:after.items,free:snap.layout.free_total,
    bypass:blk,payRequests,errors};
  fs.writeFileSync(out+'/blacklist-exit.json',JSON.stringify(artifact,null,2));
  console.log(JSON.stringify({status:rec.status,aborted:rec.aborted_reason,payment:rec.payment,
    free:snap.layout.free_total,bypassOpen:blk.filter(a=>a.cmd==='open').length,payRequests:payRequests.length,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
