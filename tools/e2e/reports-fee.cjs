// 2026-11-04 N2 报表/费率 E2E（隔离 seed 数据）：
// preview day/week/month/quarter/year 只统计完整 out 记录；CSV/PDF 公开下载；非法 422。
// fee-rules 历史 effective_to/created_at、PUT current 版本、内容不变不建版本、记录详情 breakdown。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict');
const base = process.env.PARKING_URL;
if (!base) throw new Error('PARKING_URL required');
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261104/E2E/screenshots';
fs.mkdirSync(out, { recursive: true });
const periods = ['day','week','month','quarter','year'];

function dayKey(iso){ return String(iso || '').slice(0,10); }
function inRange(iso,start,end){ if(!iso) return false; const t=Date.parse(iso),a=Date.parse(start),b=Date.parse(end); return Number.isFinite(t)&&t>=a&&t<b; }
function approx(a,b,tol=0.06){ return Math.abs(Number(a)-Number(b))<=tol; }

(async()=>{
  const browser=await chromium.launch({headless:true,args:(process.env.E2E_GPU
    ?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11']
    :['--enable-unsafe-swiftshader','--use-angle=swiftshader']),
    ...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
  try{
    const page=await browser.newPage({viewport:{width:1280,height:900}});
    const errors=[]; page.on('pageerror',e=>errors.push(String(e)));
    await page.goto(base+'/admin/');
    const login=await page.request.post(base+'/api/auth/login',{data:{username:'admin',password:'admin123'}});
    assert.equal(login.status(),200,await login.text());
    const token=(await login.json()).token,headers={Authorization:'Bearer '+token};
    const authReq=(path,opts={})=>page.request.get(base+path,{headers,...opts});
    async function preview(period,anchor=null,limit=null){
      const q=new URLSearchParams({period});if(anchor)q.set('anchor',anchor);if(limit)q.set('limit',String(limit));
      const r=await authReq('/api/reports/preview?'+q);return {status:r.status(),body:await r.json().catch(()=>null),text:r.status()!==200?await r.text():''};
    }
    // 全量记录（seed 60 历史 + 6 在场）；详情补 payment/时长
    const allResp=await authReq('/api/records?page_size=200');assert.equal(allResp.status(),200);
    const all=(await allResp.json()).items||[];
    const details=[];
    for(let i=0;i<all.length;i+=10){
      const batch=await Promise.all(all.slice(i,i+10).map(async it=>{
        const r=await authReq('/api/records/'+it.id);return r.ok()?await r.json():null;
      }));
      details.push(...batch.filter(Boolean));
    }
    const complete=details.filter(r=>r.status==='out'&&r.entry_time&&r.exit_time);
    assert.ok(complete.length>=30,'seed complete records expected, got '+complete.length);
    const openCount=details.filter(r=>r.status==='in').length;
    assert.ok(openCount>=1,'seed inhouse records expected');
    const latest=complete.slice().sort((a,b)=>String(b.exit_time).localeCompare(String(a.exit_time)))[0];
    const anchor=dayKey(latest.exit_time);
    const artifact={anchor,counts:{all:details.length,complete:complete.length,open:openCount},previews:{},files:{},fee:{},errors};
    function expectedFor(start,end){
      const recs=complete.filter(r=>inRange(r.exit_time,start,end));
      const revenue=recs.reduce((s,r)=>s+Number(r.payment?.amount_paid||0),0);
      const avg=recs.length?recs.reduce((s,r)=>s+Number(r.duration_sim_sec!=null?r.duration_sim_sec/60:(r.minutes||0)),0)/recs.length:0;
      const countIn=complete.filter(r=>inRange(r.entry_time,start,end)).length+details.filter(r=>r.status==='in'&&inRange(r.entry_time,start,end)).length;
      return {countOut:recs.length,revenue,avg,countIn};
    }
    // ---- preview 各周期 ----
    const day=await preview('day',anchor,500);assert.equal(day.status,200,day.text);
    const dayBody=day.body;artifact.previews.day=dayBody;
    assert.equal(dayBody.period,'day');assert.equal(dayBody.anchor,anchor);
    assert.equal(dayBody.level,'vehicle_day');
    assert.ok(/\+08:00$/.test(dayBody.start)&&/\+08:00$/.test(dayBody.end),'local +08 range');
    assert.equal(dayBody.by_day.length,1);assert.ok(Array.isArray(dayBody.by_day[0].vehicles));
    const expDay=expectedFor(dayBody.start,dayBody.end);
    assert.equal(dayBody.summary.count_out,expDay.countOut,'day count_out');
    assert.ok(approx(dayBody.summary.revenue,expDay.revenue,0.01),'day revenue '+dayBody.summary.revenue+' vs '+expDay.revenue);
    const dayVehs=dayBody.by_day[0].vehicles||[];
    const vehAvg=dayVehs.length?dayVehs.reduce((s,v)=>s+Number(v.duration_minutes||0),0)/dayVehs.length:0;
    assert.ok(approx(dayBody.summary.avg_minutes,vehAvg,1.0),
      'day avg minutes '+dayBody.summary.avg_minutes+' vs vehicle avg '+vehAvg);
    assert.ok(Number.isFinite(dayBody.summary.peak_occupancy));
    assert.ok(dayBody.by_day[0].vehicles.every(v=>v.plate&&v.entry_time&&v.exit_time&&v.payment_method));
    // 只含完整 out：open 不在 vehicles
    const openPlates=new Set(details.filter(r=>r.status==='in').map(r=>r.plate));
    assert.ok(!dayBody.by_day[0].vehicles.some(v=>openPlates.has(v.plate)&&!complete.some(c=>c.plate===v.plate)),'open-only records excluded');

    const week=await preview('week',anchor);assert.equal(week.status,200,week.text);artifact.previews.week=week.body;
    assert.equal(week.body.level,'day');assert.equal(week.body.by_day.length,7);
    assert.ok(week.body.by_day.every(d=>Array.isArray(d.vehicles)));
    const expWeek=expectedFor(week.body.start,week.body.end);
    assert.equal(week.body.summary.count_out,expWeek.countOut,'week count_out');
    assert.ok(approx(week.body.summary.revenue,expWeek.revenue,0.01),'week revenue');
    const month=await preview('month',anchor);assert.equal(month.status,200,month.text);artifact.previews.month=month.body;
    assert.equal(month.body.level,'week');assert.ok(Array.isArray(month.body.weeks));assert.equal(month.body.by_day,undefined);
    assert.equal(month.body.weeks.reduce((s,w)=>s+Number(w.count_out||0),0),month.body.summary.count_out,'month weeks sum');
    const quarter=await preview('quarter',anchor);assert.equal(quarter.status,200,quarter.text);artifact.previews.quarter=quarter.body;
    assert.equal(quarter.body.level,'month');assert.equal(quarter.body.months.length,3);assert.equal(quarter.body.by_day,undefined);
    assert.equal(quarter.body.months.reduce((s,m)=>s+Number(m.count_out||0),0),quarter.body.summary.count_out,'quarter months sum');
    const year=await preview('year',anchor);assert.equal(year.status,200,year.text);artifact.previews.year=year.body;
    assert.equal(year.body.level,'month');assert.equal(year.body.months.length,12);
    assert.equal(year.body.months.reduce((s,m)=>s+Number(m.count_out||0),0),year.body.summary.count_out,'year months sum');
    // limit 截断明细但汇总不截断
    const limited=await preview('day',anchor,1);assert.equal(limited.status,200);assert.equal(limited.body.truncated,true);
    assert.ok(limited.body.by_day[0].vehicles.length<=1);
    assert.equal(limited.body.summary.count_out,dayBody.summary.count_out,'limit keeps summary');

    // ---- CSV 公开下载 ----
    const csvHeaders={day:'车牌',week:'日期',month:'周起始',quarter:'周起始',year:'月份'};
    for(const period of periods){
      const url=base+'/api/reports/export.csv?period='+period+'&anchor='+encodeURIComponent(anchor);
      const resp=await fetch(url);assert.equal(resp.status,200,period+' csv status');
      assert.match(resp.headers.get('content-type')||'',/text\/csv/i,period+' csv content-type');
      const buf=Buffer.from(await resp.arrayBuffer());assert.ok(buf[0]===0xEF&&buf[1]===0xBB&&buf[2]===0xBF,period+' UTF-8 BOM');
      assert.ok(buf.length>50,period+' csv non-empty');
      const text=buf.toString('utf8').replace(/^\uFEFF/,'');
      assert.ok(text.includes(csvHeaders[period]),period+' CSV 中文表头 '+csvHeaders[period]+': '+text.slice(0,100));
      fs.writeFileSync(out+'/report-'+period+'.csv',buf);
      artifact.files['csv_'+period]={bytes:buf.length,first:text.split(/\r?\n/)[0]};
    }
    // ---- PDF 公开下载 ----
    for(const period of periods){
      const resp=await fetch(base+'/api/reports/export?period='+period+'&anchor='+encodeURIComponent(anchor));
      assert.equal(resp.status,200,period+' pdf status');
      assert.match(resp.headers.get('content-type')||'',/application\/pdf/i,period+' pdf type');
      const buf=Buffer.from(await resp.arrayBuffer());
      assert.equal(buf.subarray(0,4).toString(),'%PDF',period+' pdf magic');
      assert.ok(buf.length>1500,period+' pdf size '+buf.length);
      const pages=(buf.toString('latin1').match(/\/Type\s*\/Page\b/g)||[]).length;
      assert.ok(pages>=1,period+' pdf pages');
      fs.writeFileSync(out+'/report-'+period+'.pdf',buf);
      artifact.files['pdf_'+period]={bytes:buf.length,pages};
    }
    const pdfPages=periods.map(period=>artifact.files['pdf_'+period].pages);
    assert.ok(Math.max(...pdfPages)>=2,'at least one report PDF must paginate; pages='+JSON.stringify(pdfPages));
    // ---- 非法参数 422 ----
    for(const path of ['/api/reports/preview?period=bad','/api/reports/preview?period=day&anchor=2026-99-99',
      '/api/reports/export.csv?period=bad','/api/reports/export?period=day&anchor=bad']){
      const r=path.startsWith('/api/reports/preview')?await authReq(path):await fetch(base+path);
      const status=typeof r.status==='function'?r.status():r.status;
      assert.equal(status,422,path+' expected 422, got '+status);
    }

    // ---- 费率历史 / PUT current ----
    const rulesResp=await authReq('/api/fee-rules');assert.equal(rulesResp.status(),200);
    const beforeRules=await rulesResp.json();artifact.fee.beforeList=beforeRules.items;
    assert.ok(beforeRules.items.length>=1);
    const oldRule=beforeRules.items[0];
    for(const k of ['id','effective_from','effective_to','created_at','unit_price']) assert.ok(k in oldRule,'fee rule field '+k);
    assert.equal(oldRule.effective_to,null,'latest effective_to null');
    const current=await (await authReq('/api/fee-rules/current')).json();
    assert.ok(current.unit_price!=null&&current.free_minutes!=null,'current structure');
    const payload={free_minutes:current.free_minutes,first_hour_price:current.first_hour_price,
      unit_minutes:current.unit_minutes,unit_price:Number(current.unit_price)+1,
      daily_cap:current.daily_cap,ne_discount:current.ne_discount,round_up:current.round_up,
      note:'N2 E2E',effective_from:null};
    const put=await page.request.put(base+'/api/fee-rules/current',{headers,data:payload});
    assert.equal(put.status(),200,await put.text());const newRule=await put.json();
    assert.notEqual(newRule.id,oldRule.id);assert.equal(Number(newRule.unit_price),Number(current.unit_price)+1);
    const afterResp=await authReq('/api/fee-rules');const after=await afterResp.json();artifact.fee.afterList=after.items;
    assert.equal(after.items.length,beforeRules.items.length+1,'new version appended');
    assert.equal(after.items[0].id,newRule.id);assert.equal(after.items[0].effective_to,null);
    const oldAfter=after.items.find(r=>r.id===oldRule.id);assert.ok(oldAfter,'old version retained');
    assert.equal(oldAfter.effective_to,newRule.effective_from,'old effective_to == new effective_from');
    assert.ok(after.items.every(r=>r.created_at!=null),'created_at present');
    // 相同内容不重复建版本
    const same=await page.request.put(base+'/api/fee-rules/current',{headers,data:payload});
    assert.equal(same.status(),200);const afterSame=await (await authReq('/api/fee-rules')).json();
    assert.equal(afterSame.items.length,after.items.length,'same content must not create version');
    const cur2=await (await authReq('/api/fee-rules/current')).json();assert.equal(Number(cur2.unit_price),Number(current.unit_price)+1);
    // 记录详情 fee_rule_id + payment.breakdown，无嵌套费率对象
    const sample=complete.find(r=>r.payment)||complete[0];assert.ok(sample?.fee_rule_id!=null);
    assert.ok(sample.payment?.breakdown&&typeof sample.payment.breakdown==='object');
    assert.ok(sample.fee_rule===undefined,'record detail must not nest fee_rule');
    artifact.fee.old=oldRule;artifact.fee.new=newRule;artifact.fee.record={id:sample.id,fee_rule_id:sample.fee_rule_id,breakdown:sample.payment.breakdown};
    artifact.fee.afterSameLength=afterSame.items.length;

    assert.deepEqual(errors,[]);
    artifact.errors=errors;
    fs.writeFileSync(out+'/reports-fee.json',JSON.stringify(artifact,null,2));
    console.log(JSON.stringify({anchor,complete:complete.length,open:openCount,
      day:{countOut:dayBody.summary.count_out,revenue:dayBody.summary.revenue,vehicles:dayBody.by_day[0].vehicles.length},
      weekDays:week.body.by_day.length,monthWeeks:month.body.weeks.length,quarterMonths:quarter.body.months.length,yearMonths:year.body.months.length,
      csv:'ok (BOM/中文表头/public)',pdf:'ok (public)',fee:{oldId:oldRule.id,newId:newRule.id,history:after.items.length},
      errors}));
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
