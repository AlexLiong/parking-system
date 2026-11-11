// 2026-10-26 适配：管理端正常态不再有“人工出场”按钮；人工结算走 API + 记录页校验。
// 覆盖 free / void / max_duration 三种人工出场，并核对记录页与账单。
const {chromium}=require('playwright'),fs=require('fs'),assert=require('assert/strict'),crypto=require('crypto');
const base=process.env.PARKING_URL||'http://127.0.0.1:9632';
const out=process.env.E2E_SCREENSHOT_DIR||'../_qa_audit/repair_tasks_20261026/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH,args:(process.env.E2E_GPU?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11']:['--enable-unsafe-swiftshader','--use-angle=swiftshader'])});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];page.on('pageerror',e=>errors.push(String(e)));
  const login=await page.request.post(base+'/api/auth/login',{data:{username:'admin',password:'admin123'}});assert.equal(login.status(),200);
  const token=(await login.json()).token,headers={Authorization:'Bearer '+token};
  async function post(path,data){const r=await page.request.post(base+path,{headers,data});assert.ok(r.ok(),await r.text());return r.json();}
  const free=await post('/api/manual/exit',{plate:'京B10203',reason:'人工核实免费',action:'free',request_id:crypto.randomUUID()});
  const abnormal=await post('/api/manual/exit',{plate:'京B10204',reason:'原始凭证缺失，异常作废审核',action:'void',request_id:crypto.randomUUID()});
  assert.equal(free.amount_paid,0);assert.equal(abnormal.status,'abnormal');
  const charged=await post('/api/manual/exit',{plate:'京B10205',reason:'按配置最长时长结算',action:'max_duration',request_id:crypto.randomUUID()});
  assert.ok(charged.amount_paid>0);
  const record=await (await page.request.get(base+`/api/records/${charged.record_id}`,{headers})).json();
  assert.equal(record.payment.amount_paid,charged.amount_paid);assert.ok(record.temporary);
  await page.addInitScript(t=>localStorage.setItem('parking.admin.token',t),token);await page.goto(base+'/admin/#/records');
  await page.getByPlaceholder('车牌（模糊）').fill('京B10205');
  await page.getByRole('button',{name:'查询',exact:true}).click();
  await page.getByRole('cell',{name:'京B10205',exact:true}).first().waitFor();
  await page.screenshot({path:out+'/business-records.png',fullPage:true});
  // 管理端正常态：人工出场按钮已删除，不出现大段人工区
  await page.goto(base+'/admin/#/monitor');
  await page.waitForTimeout(2500);
  assert.equal(await page.getByRole('button',{name:'人工出场',exact:true}).count(),0,
    'normal admin should not show legacy manual exit button');
  assert.equal(await page.locator('#authorization-card').count(),0,
    'normal admin should not show legacy authorization card');
  assert.deepEqual(errors,[]);
  fs.writeFileSync(out+'/business.json',JSON.stringify({free,abnormal,charged,record,errors},null,2));
  console.log(JSON.stringify({charged:charged.amount_paid,free:free.amount_paid,void:abnormal.status,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
