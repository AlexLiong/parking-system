// 2026-10-31 异常纠正：入场牌录错（开放记录 plate 错）→ 出场识别正确且无匹配记录 → admin
// 选择开放记录 + 正确车牌 + 入场 sim 时间 → correct-exit 自动按原时长收费/out/开闸；
// 无记录 auto=409、free/void 显式放行（不静默 0 元）。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict'), crypto = require('crypto');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9651';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261031/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
const WRONG_PLATE='粤A11111', RIGHT_PLATE='粤F4K1CZ', OTHER_PLATE='甘N35MYY', THIRD_PLATE='桂RNRYAB';
const WAIT_MIN=16;
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
  async function post(path,data){const r=await sim.request.post(base+path,{headers,data});let body=null;try{body=await r.json()}catch(_){}return {status:r.status(),body};}
  async function records(plate,status='in'){const r=await sim.request.get(base+'/api/records?status='+status+'&plate='+encodeURIComponent(plate)+'&page_size=50',{headers});assert.equal(r.status(),200);return r.json();}
  async function arrived(plate,vid,image){return post('/sim/events/arrived',{vehicle_id:vid,gate:'out',image_path:image,expected_plate:plate,source:'legacy'});}

  await sim.evaluate(async()=>{await __SIM__.simApi.resetDemo();await __SIM__.controls.setSpeed(10);});
  const wrong=await post('/api/manual/authorize-entry',{vehicle_id:'wrong-record-car',plate:WRONG_PLATE,color:'blue'});
  assert.equal(wrong.status,200,JSON.stringify(wrong));const wrongRecordId=wrong.body.record_id||wrong.body.id;
  const openWrong=(await records(WRONG_PLATE)).items[0];assert.ok(openWrong);
  const target=Number(openWrong.entry_sim_sec)+WAIT_MIN*60;
  const t0=Date.now();let simNow=0;
  while(Date.now()-t0<180000){simNow=Number(await sim.evaluate(async()=>Number((await __SIM__.simApi.rest.snapshot()).simulation.sim_time)));if(simNow>=target)break;await sim.waitForTimeout(1000);}
  assert.ok(simNow>=target,'wait chargeable duration failed');
  const arr=await arrived(RIGHT_PLATE,'correct-exit-car','datasets/demo-lpr/blue-000-clean.png');
  assert.equal(arr.status,200,JSON.stringify(arr));
  let pending=null;
  for(let i=0;i<50;i++){const snap=await sim.evaluate(async()=>await __SIM__.simApi.rest.snapshot());pending=(snap.pending_manual||[]).find(p=>p.gate==='out');if(pending)break;await sim.waitForTimeout(300);}
  assert.ok(pending,'exit_without_record pending expected');assert.equal(pending.reason,'exit_without_record');

  const admin=await browser.newPage({viewport:{width:1280,height:950}});
  admin.on('pageerror',e=>errors.push('admin:'+String(e)));
  let correctResp=null;
  admin.on('response',async r=>{if(r.url().includes('/api/manual/correct-exit')){try{correctResp=await r.json()}catch(_){}}});
  await admin.addInitScript(t=>localStorage.setItem('parking.admin.token',t),token);
  await admin.goto(base+'/admin/#/monitor');
  await admin.waitForSelector('#pending-card:not([hidden])',{timeout:20000});
  const input=admin.locator('#pending-card input[data-pending-id]').first();
  await input.fill(RIGHT_PLATE);
  const pendingId=await input.getAttribute('data-pending-id');
  const sel=admin.locator(`#correction-record-${pendingId}`);
  await sel.waitFor({timeout:20000});
  await admin.waitForFunction((id)=>document.querySelector('#correction-record-'+id)?.options?.length>1,pendingId,{timeout:20000});
  const optValues=await sel.locator('option').evaluateAll(opts=>opts.map(o=>o.value));
  assert.ok(optValues.includes(String(wrongRecordId)),'open record option '+wrongRecordId+' in '+JSON.stringify(optValues));
  await sel.selectOption(String(wrongRecordId));
  const plateInput=admin.locator(`#correction-plate-${pendingId}`);
  const unitSel=admin.locator(`#correction-unit-${pendingId}`);
  const entryInput=admin.locator(`#correction-entry-${pendingId}`);
  await unitSel.selectOption('sec');
  await plateInput.fill(RIGHT_PLATE);
  await entryInput.fill(String(openWrong.entry_sim_sec));
  await plateInput.fill(RIGHT_PLATE);
  await admin.screenshot({path:out+'/correct-exit-before.png',fullPage:true});
  await admin.locator(`#correction-auto-${pendingId}`).click();
  await admin.waitForFunction(()=>document.getElementById('pending-card')?.hidden===true,null,{timeout:25000});
  assert.ok(correctResp,'correct-exit response');
  assert.ok(Number(correctResp.settlement?.amount_paid)>0,'correct-exit must charge >0: '+JSON.stringify(correctResp));
  assert.equal(correctResp.settlement?.old_plate,WRONG_PLATE);
  const fixed=await records(RIGHT_PLATE,'out');const fixedListItem=(fixed.items||[]).find(r=>r.id===wrongRecordId)||(fixed.items||[])[0];
  assert.ok(fixedListItem,'corrected record out');const fixedDetail=await sim.request.get(base+'/api/records/'+fixedListItem.id,{headers});
  assert.equal(fixedDetail.status(),200);const fixedRec=await fixedDetail.json();
  assert.equal(fixedRec.status,'out');assert.equal(fixedRec.plate,RIGHT_PLATE);
  assert.equal(fixedRec.payment?.method,'auto');
  await admin.screenshot({path:out+'/correct-exit-after.png',fullPage:true});

  await sim.evaluate(async()=>{await __SIM__.simApi.resetDemo();await __SIM__.controls.setSpeed(10);});
  const a2=await arrived(OTHER_PLATE,'no-record-car','datasets/demo-lpr/blue-001-clean.png');assert.equal(a2.status,200);
  let p2=null;for(let i=0;i<50;i++){const s=await sim.evaluate(async()=>await __SIM__.simApi.rest.snapshot());p2=(s.pending_manual||[]).find(p=>p.gate==='out');if(p2)break;await sim.waitForTimeout(300);}
  assert.ok(p2,'no-record pending');
  const auto=await post('/api/manual/correct-exit',{plate:OTHER_PLATE,action:'auto'});
  assert.equal(auto.status,409,'no-record auto must 409');
  assert.ok(JSON.stringify(auto.body).includes('no_parking_record'),'no-record auto reason: '+JSON.stringify(auto.body));
  const free=await post('/api/manual/correct-exit',{plate:OTHER_PLATE,action:'free',reason:'E2E 无记录免费'});
  assert.equal(free.status,200,JSON.stringify(free));assert.equal(Number(free.body.settlement.amount_paid),0);
  const a3=await arrived(THIRD_PLATE,'void-record-car','datasets/demo-lpr/blue-002-clean.png');assert.equal(a3.status,200);
  const voidR=await post('/api/manual/correct-exit',{plate:THIRD_PLATE,action:'void',reason:'E2E 无记录作废'});
  assert.equal(voidR.status,200,JSON.stringify(voidR));assert.equal(voidR.body.settlement.action,'void');
  assert.deepEqual(errors,[]);
  const artifact={wrongRecordId,rightPlate:RIGHT_PLATE,pending:{id:pending.id,gate:pending.gate,reason:pending.reason},
    correct:correctResp,fixedRec,noRecordAuto:auto,noRecordFree:free,noRecordVoid:voidR,simNow,errors};
  fs.writeFileSync(out+'/correct-exit.json',JSON.stringify(artifact,null,2));
  console.log(JSON.stringify({correctAmount:correctResp.settlement.amount_paid,oldPlate:correctResp.settlement.old_plate,
    correctedRecord:fixedRec.id,noRecordAuto:auto.status,free:free.body.settlement.amount_paid,
    voidAction:voidR.body.settlement.action,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
