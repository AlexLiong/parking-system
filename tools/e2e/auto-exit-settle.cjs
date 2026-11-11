// 2026-10-31 正常出场自动扣费：免费车 + 收费车均无需 /api/pay/人工，出闸自动结算并开闸。
// 断言 record out、payment.method=auto（金额含 0）、exit_photo 有值、出口杆真实开/关、无手动 pay 请求。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict');
const { spawnDispatchNoWait, waitParked, waitGone } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9650';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261031/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
const FREE_PLATE='粤F4K1CZ';
const CHARGE_PLATE='甘N35MYY';
const CHARGE_MIN=16;

async function adminToken(page){
  const r=await page.request.post(base+'/api/auth/login',{data:{username:'admin',password:'admin123'}});
  assert.equal(r.status(),200);return (await r.json()).token;
}
async function getRecord(page,headers,plate){
  const r=await page.request.get(base+'/api/records?plate='+encodeURIComponent(plate)+'&page_size=50',{headers});
  assert.equal(r.status(),200,await r.text());const body=await r.json();
  const item=(body.items||[])[0];assert.ok(item,'record missing '+plate);
  const detail=await page.request.get(base+'/api/records/'+item.id,{headers});
  assert.equal(detail.status(),200,await detail.text());return await detail.json();
}
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
  const token=await adminToken(page),headers={Authorization:'Bearer '+token};
  async function runOne(plate,chargeMinutes,vid){
    await page.evaluate(async()=>{await __SIM__.simApi.resetDemo();await __SIM__.controls.setSpeed(10);});
    await spawnDispatchNoWait(page,{vehicleId:vid,plate,plateType:'blue',planDurationSec:600});
    await waitParked(page,vid,plate,90000);
    const recs=await page.request.get(base+'/api/records?status=in&plate='+encodeURIComponent(plate)+'&page_size=50',{headers});
    assert.equal(recs.status(),200,await recs.text());
    const openRec=((await recs.json()).items||[])[0];assert.ok(openRec,'open record '+plate);
    if(chargeMinutes){
      const target=Number(openRec.entry_sim_sec)+chargeMinutes*60;
      const started=Date.now();let now=0;
      while(Date.now()-started<240000){
        now=Number(await page.evaluate(async()=>Number((await __SIM__.simApi.rest.snapshot()).simulation.sim_time)));
        if(now>=target)break;await page.waitForTimeout(1000);
      }
      assert.ok(now>=target,`park ${chargeMinutes} sim min failed ${now}<${target}`);
    }
    await page.evaluate(id=>__SIM__.scheduler.startExit(id),vid);
    await page.waitForFunction(id=>{const v=__SIM__.scheduler.vehicles.get(id);return !v||v.state==='gone';},vid,{timeout:90000});
    const rec=await getRecord(page,headers,plate);
    assert.equal(rec.status,'out');
    assert.ok(rec.exit_photo,'exit_photo must be written');
    assert.equal(rec.payment?.method,'auto','payment.method should be auto: '+JSON.stringify(rec.payment));
    if(chargeMinutes) assert.ok(Number(rec.payment?.amount_paid)>0,'charged car amount must >0');
    else assert.equal(Number(rec.payment?.amount_paid),0,'free car amount should be 0');
    await page.screenshot({path:out+'/auto-exit-'+(chargeMinutes?'charged':'free')+'.png'});
    return rec;
  }
  const free=await runOne(FREE_PLATE,0,'auto-free-car');
  const charged=await runOne(CHARGE_PLATE,CHARGE_MIN,'auto-charge-car');
  assert.equal(payRequests.length,0,'normal auto exit must not call /api/pay');
  const acts=await (await page.request.get(base+'/api/barrier/actions?limit=200',{headers})).json();
  const outOpens=(acts.items||[]).filter(a=>a.gate==='out'&&a.cmd==='open');
  assert.ok(outOpens.length>=1,'exit barrier open action missing');
  assert.deepEqual(errors,[]);
  const artifact={free,charged: {record_id:charged.id,amount:charged.payment.amount_paid,method:charged.payment.method},
    payRequests,outBarrierOpens:outOpens,errors};
  fs.writeFileSync(out+'/auto-exit-settle.json',JSON.stringify(artifact,null,2));
  console.log(JSON.stringify({freeAmount:free.payment.amount_paid,chargedAmount:charged.payment.amount_paid,
    exitPhoto:!!charged.exit_photo,payRequests:payRequests.length,outOpens:outOpens.length,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
