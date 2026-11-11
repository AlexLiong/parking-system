// 2026-11-04 N3 管理端 E2E（隔离 seed 数据）：记录详情中文化/自动扣费翻译/历史费率；
// 费率页仅当前+查看历史；报表预览 day/week/month/quarter/year + CSV/PDF 按钮；场景页删字。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict');
const { spawnDispatchNoWait, waitParked, waitGone } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL;
if (!base) throw new Error('PARKING_URL required');
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261104/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
const PLATE='粤A11111';
(async()=>{
 const browser=await chromium.launch({headless:true,args:(process.env.E2E_GPU
   ?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11']
   :['--enable-unsafe-swiftshader','--use-angle=swiftshader']),
   ...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  await page.goto(base+'/admin/');
  const login=await page.request.post(base+'/api/auth/login',{data:{username:'admin',password:'admin123'}});
  assert.equal(login.status(),200,await login.text());const token=(await login.json()).token,headers={Authorization:'Bearer '+token};
  // 创建新的当前费率版本，使 seed 历史记录显示“历史费率（与当前不同）”
  const cur=await (await page.request.get(base+'/api/fee-rules/current',{headers})).json();
  const payload={free_minutes:cur.free_minutes,first_hour_price:cur.first_hour_price,unit_minutes:cur.unit_minutes,
    unit_price:Number(cur.unit_price)+1,daily_cap:cur.daily_cap,ne_discount:cur.ne_discount,round_up:cur.round_up,
    note:'N3 E2E',effective_from:null};
  const put=await page.request.put(base+'/api/fee-rules/current',{headers,data:payload});
  assert.ok(put.ok(),await put.text());

  // 生成一条真实自动扣费出场记录（隔离 seed 库；如遇在场占用则退回 API 支付 auto）
  let autoRecord=null;
  try{
    const sim=await browser.newPage({viewport:{width:1280,height:900}});
    await sim.goto(base+'/sim/');await sim.waitForFunction(()=>__SIM__?.simApi?.rt?.sessionId,null,{timeout:90000});
    await sim.evaluate(async()=>{await __SIM__.controls.setSpeed(10);});
    const car=await spawnDispatchNoWait(sim,{vehicleId:'N3-AUTO',plate:PLATE,plateType:'blue',planDurationSec:600});
    await waitParked(sim,car.id,PLATE,90000);
    await sim.evaluate(id=>__SIM__.scheduler.startExit(id),car.id);
    await waitGone(sim,car.id,90000)
    const rr=await (await sim.request.get(base+'/api/records?plate='+encodeURIComponent(PLATE)+'&page_size=10',{headers})).json();
    const listRec=(rr.items||[])[0]||null;
    autoRecord=listRec?(await (await sim.request.get(base+'/api/records/'+listRec.id,{headers})).json()):null;
    await sim.close();
  }catch(e){errors.push('sim_auto:'+String(e));}
  if(!autoRecord){
    const ae=await page.request.post(base+'/api/manual/authorize-entry',{headers,data:{vehicle_id:'N3-API',plate:PLATE,color:'blue'}});
    assert.ok(ae.ok(),await ae.text());const rec=await ae.json();
    const pay=await page.request.post(base+'/api/pay',{headers,data:{record_id:rec.record_id,method:'auto'}});
    assert.ok(pay.ok(),await pay.text());
    autoRecord=await (await page.request.get(base+'/api/records/'+rec.record_id,{headers})).json();
  }
  assert.equal(autoRecord.payment?.method,'auto','need auto payment record: '+JSON.stringify(autoRecord.payment));
  // 找一条 seed 历史记录（fee_rule_id 为旧版本）
  const all=await (await page.request.get(base+'/api/records?page_size=200',{headers})).json();
  let hist=null;
  for(const it of (all.items||[]).filter(r=>r.status==='out')){
    const d=await page.request.get(base+'/api/records/'+it.id,{headers});
    if(!d.ok()) continue;
    const detail=await d.json();
    if(detail.payment && String(detail.fee_rule_id)===String(cur.id)){hist=detail;break;}
  }
  assert.ok(hist,'historical record');

  await page.addInitScript(t=>localStorage.setItem('parking.admin.token',t),token);
  await page.goto(base+'/admin/');
  await page.evaluate(t=>localStorage.setItem('parking.admin.token',t),token);
  await page.goto(base+'/admin/#/records');
  await page.waitForSelector('input[placeholder*="车牌"]',{timeout:30000});

  // ---- 记录详情：中文、自动扣费、历史费率、无 raw JSON ----
  await page.getByPlaceholder('车牌（模糊）').fill(PLATE);
  await page.getByRole('button',{name:'查询',exact:true}).click();
  await page.getByRole('button',{name:'详情',exact:true}).first().click();
  await page.waitForSelector('.modal',{timeout:20000});
  await page.waitForFunction(()=>{const t=document.querySelector('.modal')?.innerText||'';return t.includes('计费明细')||t.includes('加载失败')},null,{timeout:20000});
  const autoModal=await page.locator('.modal').last().innerText();
  for(const label of ['计费明细','停放时长','计费时长','支付方式','自动扣费','出场时间']) assert.ok(autoModal.includes(label),'record detail missing '+label);
  assert.ok(!/[\{\}"]\s*:|"detail":|"code":/.test(autoModal),'record detail must not show raw JSON: '+autoModal.slice(0,400));
  await page.screenshot({path:out+'/record-detail-auto.png',fullPage:true});
  await page.keyboard.press('Escape');
  // 历史费率 label（旧 fee_rule）
  await page.getByPlaceholder('车牌（模糊）').fill(hist.plate);
  await page.getByRole('button',{name:'查询',exact:true}).click();
  await page.getByRole('button',{name:'详情',exact:true}).first().click();
  await page.waitForSelector('.modal',{timeout:20000});
  await page.waitForFunction(()=>{const t=document.querySelector('.modal')?.innerText||'';return t.includes('计费明细')||t.includes('加载失败')},null,{timeout:20000});
  const histModal=await page.locator('.modal').last().innerText();
  assert.ok(/历史费率（与当前不同）/.test(histModal),'history fee badge missing: '+histModal.slice(0,500));
  assert.ok(/#\d+/.test(histModal),'fee version id missing');
  await page.screenshot({path:out+'/record-detail-history-fee.png',fullPage:true});
  await page.keyboard.press('Escape');

  // ---- 费率页：仅当前+历史只读 ----
  await page.goto(base+'/admin/#/fee-rules');
  await page.getByText('当前生效费率').waitFor({timeout:20000});
  const feeBody=await page.locator('body').innerText();
  for(const stale of ['新增费率版本','以此修订','修订模式','版本快照不可变','阶梯计费 / 每日封顶']) assert.ok(!feeBody.includes(stale),'fee page stale text '+stale);
  assert.ok(await page.getByRole('button',{name:'编辑',exact:true}).count()>0);
  await page.screenshot({path:out+'/fee-current.png',fullPage:true});
  await page.getByRole('button',{name:'查看历史费率',exact:true}).click();
  await page.waitForSelector('.modal',{timeout:20000});
  await page.waitForTimeout(800);
  const histFee=await page.locator('.modal').last().innerText();
  assert.ok(histFee.includes('历史费率')&&histFee.includes('停用时间'),'fee history modal: '+histFee.slice(0,500));
  await page.screenshot({path:out+'/fee-history.png',fullPage:true});
  await page.keyboard.press('Escape');

  // ---- 报表预览 5 周期 + 导出按钮 ----
  await page.goto(base+'/admin/#/stats');
  await page.getByRole('button',{name:'预览报表',exact:true}).click();
  await page.waitForSelector('.modal',{timeout:20000});
  const statsTexts={};
  for(const period of ['day','week','month','quarter','year']){
    const modal=page.locator('.modal').last();
    const selects=modal.locator('select');assert.ok(await selects.count()>=1);
    await selects.first().selectOption(period);
    await modal.getByRole('button',{name:'加载预览',exact:true}).click();
    await page.waitForFunction(()=>{const t=document.querySelector('.modal')?.innerText||'';return t.includes('仅完整出场记录')||t.includes('预览失败')},null,{timeout:20000});
    const t=await modal.innerText();statsTexts[period]=t;
    assert.ok(t.includes('仅完整出场记录'),'report preview '+period+' complete-only note');
    assert.ok(t.includes('入场车次')&&t.includes('出场车次')&&t.includes('营收'),'report summary '+period);
    for(const btn of ['导出 CSV','导出 PDF']) assert.ok(await modal.getByRole('button',{name:btn,exact:true}).count()>0,'export button '+btn);
    if(period==='day') assert.ok(t.includes('车辆明细'),'day vehicle detail');
    if(period==='week') assert.ok(t.includes('每日汇总')||t.includes('车辆明细'),'week details');
    if(period==='month') assert.ok(t.includes('按周汇总')||t.includes('周'),'month weeks');
    if(period==='quarter'||period==='year') assert.ok(t.includes('按月')||t.includes('月'),'month summary');
    await page.screenshot({path:out+'/report-preview-'+period+'.png',fullPage:true});
  }
  assert.deepEqual(errors,[]);

  // ---- 场景页删字 ----
  await page.goto(base+'/admin/#/scenario');
  await page.waitForTimeout(1200);
  const scn=await page.locator('body').innerText();
  for(const stale of ['时间单位：仿真秒','max(t + duration)','所有车辆 duration 合计','预计缴费 = 预计出场']) assert.ok(!scn.includes(stale),'scenario stale text '+stale);

  assert.deepEqual(errors,[]);
  const artifact={autoRecord:{id:autoRecord.id,method:autoRecord.payment.method},hist:{id:hist.id,plate:hist.plate,fee_rule_id:hist.fee_rule_id},
    autoModal:autoModal.slice(0,2000),histFee:histFee.slice(0,1200),stats:Object.fromEntries(Object.entries(statsTexts).map(([k,v])=>[k,v.slice(0,800)])),errors};
  fs.writeFileSync(out+'/admin-n3.json',JSON.stringify(artifact,null,2));
  console.log(JSON.stringify({autoRecord:autoRecord.id,autoMethod:autoRecord.payment.method,historyRecord:hist.id,
    stats:Object.keys(statsTexts),errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
