// 2026-11-06 P2：footer 仅 v1.0.0 / 报表固定列宽对齐/空值 / day-week-month-quarter-year 基准控件 anchor。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict');
const base = process.env.PARKING_URL;
if (!base) throw new Error('PARKING_URL required');
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261106/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
function weekToMonday(v){const m=/^(\d{4})-W(\d{2})$/.exec(String(v||''));if(!m)return '';const year=Number(m[1]),week=Number(m[2]);const jan4=new Date(Date.UTC(year,0,4));const day=jan4.getUTCDay()||7;const mon=new Date(jan4);mon.setUTCDate(jan4.getUTCDate()-(day-1)+(week-1)*7);return mon.toISOString().slice(0,10);}
(async()=>{
 const browser=await chromium.launch({headless:true,args:(process.env.E2E_GPU
   ?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11']
   :['--enable-unsafe-swiftshader','--use-angle=swiftshader']),
   ...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[],statsReq=[],previewReq=[];
  page.on('pageerror',e=>errors.push(String(e)));
  page.on('request',r=>{const u=r.url();if(u.includes('/api/stats?'))statsReq.push(u);if(u.includes('/api/reports/preview?'))previewReq.push(u);});
  await page.goto(base+'/admin/');
  const login=await page.request.post(base+'/api/auth/login',{data:{username:'admin',password:'admin123'}});
  assert.equal(login.status(),200,await login.text());const token=(await login.json()).token;const headers={Authorization:'Bearer '+token};
  await page.addInitScript(t=>localStorage.setItem('parking.admin.token',t),token);
  await page.goto(base+'/admin/');await page.evaluate(t=>localStorage.setItem('parking.admin.token',t),token);
  await page.goto(base+'/admin/#/stats');
  await page.waitForSelector('#main-content .report-anchor',{timeout:30000});
  await page.waitForTimeout(800);
  // footer
  const footer=await page.locator('.foot').filter({hasText:'v1.0.0'}).first().innerText();
  assert.equal(footer.trim(),'v1.0.0','footer must be exactly v1.0.0');
  assert.equal(await page.locator('a[href*="/api/health"]').count(),0,'health link removed');
  const bodyText=await page.locator('body').innerText();
  assert.ok(!bodyText.includes('无构建原生 ES')&&!bodyText.includes('服务健康检查'),'footer stale text');
  await page.screenshot({path:out+'/p2-footer.png',fullPage:true});
  const results={footer,anchors:{},tables:{},preview:[]};
  // ---- 管理端 raise 后 hold（设备控制） ----
  async function snap(){return (await page.request.get(base+'/api/state/snapshot',{headers})).json();}
  async function ack(gate,state){const s=await snap();const cmd=s.barriers[gate]?.last_cmd_id||null;
    await page.request.post(base+'/sim/events/barrier_ack',{data:{gate,state,cmd_id:cmd}});await page.waitForTimeout(300);}
  await page.goto(base+'/admin/#/monitor');
  await page.waitForSelector('#device-control-card',{timeout:20000});
  await page.locator('#device-barrier-raise-in').click();
  // 等 admin API 请求落到后端再回执（Playwright click 不等待 onclick 内 fetch 完成）
  let t0=Date.now(),s=null;
  while(Date.now()-t0<4000){s=await snap();const st=String(s.barriers.in.state).toUpperCase();
    if(st==='OPENING'||st==='KEEP_OPEN')break;await page.waitForTimeout(100);}
  if(String(s.barriers.in.state).toUpperCase()==='OPENING') await ack('in','open');
  t0=Date.now();
  while(Date.now()-t0<8000){s=await snap();if(String(s.barriers.in.state).toUpperCase()==='KEEP_OPEN')break;await page.waitForTimeout(200);}
  assert.equal(String(s.barriers.in.state).toUpperCase(),'KEEP_OPEN','admin raise should hold KEEP_OPEN');
  assert.equal(s.barriers.in.keep_open,true,'admin raise keep_open=true');
  await page.screenshot({path:out+'/p2-admin-raise-hold.png',fullPage:true});
  // 用同一 manual close 端点落杆并回执（P1 已验证 409/close；此处验证 raise 保持可被清除）
  await page.request.post(base+'/api/manual/close-barrier',{headers,data:{gate:'in',reason:'p2 close'}});
  await page.waitForTimeout(500);await ack('in','closed');
  const t1=Date.now();while(Date.now()-t1<8000){s=await snap();if(String(s.barriers.in.state).toLowerCase()==='closed')break;await page.waitForTimeout(200);}
  assert.equal(String(s.barriers.in.state).toLowerCase(),'closed','manual close should close hold');
  assert.equal(s.barriers.in.keep_open,false,'manual close keep_open=false');
  results.adminRaiseHold={state:'KEEP_OPEN',keep_open:true,closedKeepOpen:false};
  await page.goto(base+'/admin/#/stats');await page.waitForSelector('#main-content .report-anchor',{timeout:30000});await page.waitForTimeout(600);

  async function controlAnchor(scope){
    const info=await scope.evaluate((el)=>{
      const wrap=el.querySelector('.report-anchor')||el;
      const label=wrap.querySelector('label')?.textContent||'';
      const inputs=[...wrap.querySelectorAll('input')].map(i=>({type:i.type,value:i.value}));
      const sels=[...wrap.querySelectorAll('select')].map(s=>({value:s.value,options:[...s.options].map(o=>o.value)}));
      return {label,inputs,sels,text:wrap.innerText};
    });
    return info;
  }
  function expectedAnchor(period,info){
    if(period==='day')return info.inputs.find(i=>i.type==='date')?.value||'';
    if(period==='week')return weekToMonday(info.inputs.find(i=>i.type==='week')?.value);
    if(period==='month'){const v=info.inputs.find(i=>i.type==='month')?.value;return v?v+'-01':'';}
    if(period==='quarter'){const y=info.inputs.find(i=>i.type==='number')?.value;const q=info.sels[0]?.value;return (y&&q)?`${y}-${String((Number(q)-1)*3+1).padStart(2,'0')}-01`:'';}
    if(period==='year'){const y=info.inputs.find(i=>i.type==='number')?.value;return y?`${y}-01-01`:'';}
    return '';
  }
  const periodSelect=page.locator('#main-content select').first();
  for(const period of ['day','week','month','quarter','year']){
    await periodSelect.selectOption(period);
    await page.waitForTimeout(200);
    const before=statsReq.length;
    const anchorInfo=await controlAnchor(page.locator('#main-content'));
    results.anchors[period]={control:anchorInfo,expected:expectedAnchor(period,anchorInfo)};
    await page.getByRole('button',{name:'生成',exact:true}).click();
    const deadline=Date.now()+15000;
    while(statsReq.length<=before&&Date.now()<deadline) await page.waitForTimeout(100);
    const last=statsReq.at(-1);assert.ok(last,'stats request for '+period);
    const got=new URL(last).searchParams.get('anchor');
    assert.equal(got,results.anchors[period].expected,'main stats anchor '+period+': '+got+' vs '+results.anchors[period].expected);
    assert.ok(anchorInfo.label.length>0,'anchor label '+period);
    await page.screenshot({path:out+'/p2-anchor-'+period+'.png',fullPage:true});
  }

  // preview each period: controls, anchor request, report table fixed/alignment/bad cells
  for(const period of ['day','week','month','quarter','year']){
    await page.getByRole('button',{name:'预览报表',exact:true}).click();
    const modal=page.locator('.modal').last();
    await modal.waitFor({timeout:20000});
    const psel=modal.locator('select').first();
    await psel.selectOption(period);
    await page.waitForTimeout(200);
    const before=previewReq.length;
    const anchorInfo=await controlAnchor(modal);
    const expected=expectedAnchor(period,anchorInfo);
    await modal.getByRole('button',{name:'加载预览',exact:true}).click();
    await page.waitForFunction(()=>{const t=document.querySelector('.modal')?.innerText||'';return t.includes('仅完整出场记录')||t.includes('预览失败');},null,{timeout:25000});
    const deadline=Date.now()+15000;
    while(previewReq.length<=before&&Date.now()<deadline) await page.waitForTimeout(100);
    const got=previewReq.length>before?new URL(previewReq.at(-1)).searchParams.get('anchor'):null;
    assert.equal(got,expected,'preview anchor '+period+': '+got+' vs '+expected);
    const tables=await modal.locator('table.report-table').evaluateAll((tables)=>tables.map((t)=>{
      const cs=getComputedStyle(t);const th=[...t.querySelectorAll('thead th')];const first=t.querySelector('tbody tr');
      const tds=first?[...first.children]:[];
      const cols=t.querySelectorAll('colgroup col').length;
      const mismatch=th.reduce((n,h,i)=>n+((h.className===(tds[i]?.className||''))?0:1),0);
      const bad=[...t.querySelectorAll('tbody td')].filter(td=>{const s=(td.innerText??'').trim();return s===''||s.includes('undefined')||s.includes('NaN');}).length;
      const datetime=t.querySelectorAll('td .datetime').length;
      const hasBr=[...t.querySelectorAll('td .datetime')].some(el=>el.innerHTML.includes('<br'));
      return {tableLayout:cs.tableLayout,thCount:th.length,colCount:cols,tdCount:tds.length,mismatch,bad,datetime,hasBr};
    }));
    assert.ok(tables.length>0,'report table '+period);
    for(const t of tables){assert.equal(t.tableLayout,'fixed','table-layout '+period);assert.equal(t.colCount,t.thCount,'colgroup count '+period);
      assert.equal(t.mismatch,0,'header/data alignment mismatch '+period+': '+JSON.stringify(t));assert.equal(t.bad,0,'empty/undefined/NaN cells '+period);}
    if(period==='day'||period==='week'){assert.ok(tables.some(t=>t.datetime>0&&t.hasBr),'vehicle datetime two-line '+period);}
    // export buttons
    assert.ok(await modal.getByRole('button',{name:'导出 CSV',exact:true}).count()>0);
    assert.ok(await modal.getByRole('button',{name:'导出 PDF',exact:true}).count()>0);
    results.tables[period]=tables;
    results.preview.push({period,anchor:got,label:anchorInfo.label,tables:tables.length});
    await page.screenshot({path:out+'/p2-report-table-'+period+'.png',fullPage:true});
    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);
  }
  assert.deepEqual(errors,[]);
  fs.writeFileSync(out+'/p2-admin.json',JSON.stringify({...results,errors},null,2));
  console.log(JSON.stringify({footer,anchors:Object.fromEntries(Object.entries(results.anchors).map(([k,v])=>[k,v.expected])),
    preview:results.preview,tables:Object.fromEntries(Object.entries(results.tables).map(([k,v])=>[k,v.length])),errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
