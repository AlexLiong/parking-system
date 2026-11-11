// 2026-11-06 P1：人工抬杆保持 hold 语义 + 报表 PDF day/week 横向分页/月报纵向。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict');
const base = process.env.PARKING_URL;
if (!base) throw new Error('PARKING_URL required');
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261106/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
const KEEP_OPEN_SEC=3, CLOSE_DELAY=1;
(async()=>{
 const browser=await chromium.launch({headless:true,args:(process.env.E2E_GPU
   ?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11']
   :['--enable-unsafe-swiftshader','--use-angle=swiftshader']),
   ...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  await page.goto(base+'/admin/');
  const login=await page.request.post(base+'/api/auth/login',{data:{username:'admin',password:'admin123'}});
  assert.equal(login.status(),200,await login.text());const token=(await login.json()).token,headers={Authorization:'Bearer '+token};
  async function post(path,data){const r=await page.request.post(base+path,{headers,data});let body=null;try{body=await r.json()}catch(_){}return {status:r.status(),body};}
  async function snap(){return (await page.request.get(base+'/api/state/snapshot',{headers})).json();}
  async function waitState(gate,state,timeout=8000){const t0=Date.now();while(Date.now()-t0<timeout){const x=await snap();if(String(x.barriers[gate].state).toUpperCase()===state)return x;await page.waitForTimeout(200);}throw new Error('waitState failed '+gate+' '+state+': '+JSON.stringify((await snap()).barriers[gate]));}
  async function ack(gate,state){
    const s=await snap();const cmd=s.barriers[gate]?.last_cmd_id||null;
    await page.request.post(base+'/sim/events/barrier_ack',{data:{gate,state,cmd_id:cmd}});
    await page.waitForTimeout(300);
  }
  const art={hold:{},pdf:{},errors};
  async function closeGate(gate){const r=await post('/api/manual/close-barrier',{gate,reason:'e2e cleanup'});return r;}
  await closeGate('in').catch(()=>{});
  // ---- 1) manual open holds across keep_open_sec ----
  const opened=await post('/api/manual/open-barrier',{gate:'in',reason:'e2e hold'});
  assert.equal(opened.status,200,JSON.stringify(opened));
  assert.equal(opened.body.hold,true,'open response hold=true');assert.equal(opened.body.keep_open,true);
  await ack('in','open');
  let s=await waitState('in','KEEP_OPEN');
  assert.equal(s.barriers.in.keep_open,true,'snapshot keep_open true');
  await page.waitForTimeout((KEEP_OPEN_SEC+CLOSE_DELAY+3)*1000);
  s=await snap();assert.equal(String(s.barriers.in.state).toUpperCase(),'KEEP_OPEN','manual hold must not auto close');
  assert.equal(s.barriers.in.keep_open,true);
  await page.screenshot({path:out+'/p1-manual-hold.png'});
  const closed=await closeGate('in');assert.equal(closed.status,200,JSON.stringify(closed));
  await ack('in','closed');await page.waitForTimeout(800);s=await snap();
  assert.equal(String(s.barriers.in.state).toLowerCase(),'closed');assert.equal(s.barriers.in.keep_open,false);
  art.hold.manual=opened.body;
  // ---- 2) fault manual open also holds ----
  const fault=await post('/api/barrier/in/fault',{action:'inject',mode:'stuck_closed'});assert.equal(fault.status,200);
  await page.waitForTimeout(300);
  const faultOpen=await post('/api/manual/open-barrier',{gate:'in',reason:'e2e fault hold'});
  assert.equal(faultOpen.status,200,JSON.stringify(faultOpen));assert.equal(faultOpen.body.hold,true);
  await ack('in','open');s=await waitState('in','KEEP_OPEN');
  assert.equal(s.barriers.in.keep_open,true);
  await page.waitForTimeout((KEEP_OPEN_SEC+CLOSE_DELAY+2)*1000);s=await snap();
  assert.equal(String(s.barriers.in.state).toUpperCase(),'KEEP_OPEN','fault manual hold must persist');
  await post('/api/barrier/in/clear-fault');await page.waitForTimeout(400);await closeGate('in');await ack('in','closed');
  await page.waitForTimeout(800);
  art.hold.fault=faultOpen.body;
  // ---- 3) blacklist gate open does not hold ----
  const add=await post('/api/lists',{plate:'苏B2S39K',list_type:'black',note:'P1 hold E2E'});assert.ok([200,201].includes(add.status));
  await closeGate('out').catch(()=>{});
  const blk=await post('/api/sim/blacklist-gate',{vehicle_id:'p1-blk',plate:'苏B2S39K',gate:'out',action:'open'});
  assert.equal(blk.status,200,JSON.stringify(blk));assert.equal(blk.body.hold,false);assert.equal(blk.body.keep_open,false);
  await ack('out','open');await page.waitForTimeout(1200);s=await snap();
  // blacklist open 是 keep_open_sec 自动落杆，不应 hold；等待计时结束后确认
  await page.waitForTimeout((KEEP_OPEN_SEC+CLOSE_DELAY+2)*1000);s=await snap();
  if(String(s.barriers.out.state).toUpperCase()==='CLOSING') await ack('out','closed');
  await page.waitForTimeout(600);s=await snap();
  assert.notEqual(String(s.barriers.out.state).toUpperCase(),'KEEP_OPEN','blacklist open must not hold');
  assert.equal(s.barriers.out.keep_open,false);
  await post('/api/sim/blacklist-gate',{vehicle_id:'p1-blk',plate:'苏B2S39K',gate:'out',action:'close'}).catch(()=>{});
  await ack('out','closed');
  art.hold.blacklist=blk.body;
  // ---- 4) normal business entry open is not held ----
  const entry=await post('/api/manual/authorize-entry',{vehicle_id:'p1-normal',plate:'粤F4K1CZ',color:'blue'});
  assert.equal(entry.status,200,JSON.stringify(entry));
  await ack('in','open');await page.waitForTimeout(1000);s=await snap();
  // 人工授权入场开闸等待车辆通过（permit），不应标记为 manual hold；无车时 KEEP_OPEN 属正常等待
  assert.notEqual(s.barriers.in.authorization,'manual','business open must not be manual hold');
  art.hold.business={state:s.barriers.in.state,keep_open:s.barriers.in.keep_open,authorization:s.barriers.in.authorization};
  const businessClose=await closeGate('in');if(businessClose.status===200) await ack('in','closed');
  await page.waitForTimeout(500);
  // ---- 4b) 杆区有车时 manual hold 落杆 409 ----
  const snap0=await snap();const sessionId=snap0.session?.id||snap0.session_id||'p1-session';
  await page.request.post(base+'/sim/events/zone',{data:{owner_id:'p1',session_id:sessionId,zone_id:'out_barrier',occupied:true,vehicle_id:'p1-zone-car'}});
  const holdOut=await post('/api/manual/open-barrier',{gate:'out',reason:'p1 zone hold'});
  assert.equal(holdOut.status,200,JSON.stringify(holdOut));await ack('out','open');await waitState('out','KEEP_OPEN');
  const closeBusy=await post('/api/manual/close-barrier',{gate:'out',reason:'p1 zone busy'});
  assert.equal(closeBusy.status,409,'close while zone occupied must 409');assert.equal(closeBusy.body?.error||closeBusy.body?.detail?.code,'zone_occupied');
  await page.request.post(base+'/sim/events/zone',{data:{owner_id:'p1',session_id:sessionId,zone_id:'out_barrier',occupied:false,vehicle_id:'p1-zone-car'}});
  const closeOk=await closeGate('out');assert.equal(closeOk.status,200,JSON.stringify(closeOk));await ack('out','closed');
  await page.waitForTimeout(600);
  art.hold.zoneOccupied={closeStatus:closeBusy.status,closeAfterClear:closeOk.status};
  // ---- 4c) CSV 支付方式中文 ----
  const csvResps={};
  for(const period of ['day','week']){
    const r=await fetch(base+'/api/reports/export.csv?period='+period+'&anchor='+encodeURIComponent(await (async()=>{
      const rr=await (await page.request.get(base+'/api/records?page_size=1',{headers})).json();return String((rr.items||[])[0]?.exit_time||'').slice(0,10);
    })()));
    assert.equal(r.status,200);const buf=Buffer.from(await r.arrayBuffer());assert.equal(buf[0],0xEF);
    const text=buf.toString('utf8').replace(/^\uFEFF/,'');fs.writeFileSync(out+'/p1-'+period+'.csv',buf);
    assert.ok(text.includes('支付方式'),'CSV 支付方式 header');
    assert.match(text,/自动扣费|扫码支付|免费|月租|现金/,'CSV payment method must be Chinese: '+text.slice(0,500));
    assert.ok(!/(^|,)(auto|mock_scan|cash|monthly|free)(,|\r?$)/m.test(text),'CSV must not expose raw payment codes');
    csvResps[period]=text.split(/\r?\n/)[0];
  }
  art.csv=csvResps;
  // ---- 5) PDF day/week landscape multipage, month portrait ----
  const recs=await (await page.request.get(base+'/api/records?page_size=200',{headers})).json();
  const complete=(recs.items||[]).filter(r=>r.status==='out'&&r.exit_time);
  assert.ok(complete.length>=100,'seeded complete records >=100, got '+complete.length);
  const anchor=String(complete[0].exit_time).slice(0,10);
  for(const period of ['day','week','month']){
    const resp=await fetch(base+'/api/reports/export?period='+period+'&anchor='+encodeURIComponent(anchor));
    assert.equal(resp.status,200,period+' pdf status');
    assert.match(resp.headers.get('content-type')||'',/application\/pdf/i);
    const buf=Buffer.from(await resp.arrayBuffer());assert.equal(buf.subarray(0,4).toString(),'%PDF');
    fs.writeFileSync(out+'/p1-'+period+'.pdf',buf);
    const text=buf.toString('latin1');
    const pages=(text.match(/\/Type\s*\/Page\b/g)||[]).length;
    const mb=(text.match(/\/MediaBox\s*\[\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*\]/)||[]).slice(1).map(Number);
    art.pdf[period]={bytes:buf.length,pages,mediaBox:mb};
    assert.ok(pages>=1,'pdf pages');
    if(period==='day'||period==='week'){assert.ok(pages>=2,period+' detail PDF should paginate, pages='+pages);
      assert.ok(mb.length===4&&mb[2]>mb[3],period+' detail PDF should be landscape: '+JSON.stringify(mb));}
    if(period==='month'){assert.ok(mb.length===4&&mb[2]<mb[3],'month PDF should be portrait: '+JSON.stringify(mb));}
  }
  await page.screenshot({path:out+'/p1-pdf-downloads.png'});
  assert.deepEqual(errors,[]);
  art.anchor=anchor;art.complete=complete.length;
  fs.writeFileSync(out+'/p1-hold-pdf.json',JSON.stringify(art,null,2));
  console.log(JSON.stringify({hold:art.hold,anchor,complete,pdf:art.pdf,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
