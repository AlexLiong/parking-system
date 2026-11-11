// 2026-10-31 闸屏适配自动结算：入口识别屏 → 出口 fee_quote 屏 → payment_completed 自动结算/paid 屏；
// 不再手动 /api/pay。真实当前牌 dispatch，出口自动扣费无需剧本 pay。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict');
const { spawnDispatchNoWait, waitParked } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9634';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261031/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
const PLATE='粤F4K1CZ';
(async()=>{
 const browser=await chromium.launch({headless:true,args:(process.env.E2E_GPU
   ?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11','--disable-background-timer-throttling']
   :['--enable-unsafe-swiftshader','--use-angle=swiftshader','--disable-background-timer-throttling']),
   ...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:800}}),errors=[],payRequests=[];
  page.on('pageerror',e=>errors.push(String(e)));
  page.on('request',r=>{if(r.url().includes('/api/pay'))payRequests.push(r.url())});
  const stages=[];
  async function capture(name,gate){
    const lines=await page.evaluate(g=>__SIM__.scene.gates[g].screenLines,gate);
    stages.push({name,lines});await page.screenshot({path:out+'/displays-'+name+'.png'});
    const data=await page.evaluate(g=>__SIM__.scene.gates[g].screen.material.map.image.toDataURL('image/png'),gate);
    fs.writeFileSync(out+'/displays-'+name+'-texture.png',Buffer.from(data.split(',')[1],'base64'));
  }
  await page.goto(base+'/sim/');await page.waitForFunction(()=>__SIM__?.simApi?.rt?.sessionId,null,{timeout:90000});
  await page.evaluate(async()=>{await __SIM__.simApi.resetDemo();await __SIM__.controls.setPaused(false);await __SIM__.controls.setSpeed(10);});
  const info=await spawnDispatchNoWait(page,{vehicleId:'display-car',plate:PLATE,plateType:'blue',planDurationSec:600});
  let entry=false,quote=false,paid=false,textureCheck=null;
  // 入场识别屏在入库后可能很快被出口流程覆盖：先等并抓取入口屏，再等 parked
  await page.waitForFunction(()=>__SIM__?.scene?.gates?.in?.screenLines?.join('|').includes('请通行'),null,{timeout:60000});
  await capture('entry-recognized','in');entry=true;
  await waitParked(page,info.id,PLATE,90000);
  await page.evaluate(()=>__SIM__.scheduler.startExit('display-car'));
  const started=Date.now();
  while(Date.now()-started<120000){
    const state=await page.evaluate(async()=>({
      car:(()=>{const v=__SIM__.scheduler.vehicles.get('display-car');return v?{state:v.state,meta:v.meta}:null})(),
      inLines:__SIM__.scene.gates.in.screenLines,outLines:__SIM__.scene.gates.out.screenLines,
      snap:await __SIM__.simApi.rest.snapshot(),
    }));
    const outText=(state.outLines||[]).join('|');
    if(!entry && (state.inLines||[]).join('|').includes('请通行')){await capture('entry-recognized','in');entry=true;}
    if(!quote && /应缴|缴费|¥/.test(outText)){
      await capture('exit-quote','out');quote=true;
      textureCheck=await page.evaluate(async()=>{const g=__SIM__.scene.gates.out,lines=g.screenLines.slice();const before=__SIM__.scene.renderer.info.memory.textures,material=g.screen.material,texture=material.map;
        for(let i=0;i<100;i++)g.setScreen(['测试刷新 '+i,'¥'+i]);g.setScreen(lines);await new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)));
        return {before,after:__SIM__.scene.renderer.info.memory.textures,sameMaterial:material===g.screen.material,sameTexture:texture===g.screen.material.map};});
    }
    if(!paid && (state.car?.state==='leaving' || !state.car || /已缴费|已支付|一路顺风|缴费完成/.test(outText))){
      await capture('exit-paid','out');paid=true;
    }
    if(!state.car) break;
    await page.waitForTimeout(100);
  }
  await page.waitForFunction(()=>!__SIM__.scheduler.vehicles.has('display-car'),null,{timeout:30000});
  const login=await page.request.post(base+'/api/auth/login',{data:{username:'admin',password:'admin123'}});
  const token=(await login.json()).token,headers={Authorization:'Bearer '+token};
  const recs=await (await page.request.get(base+'/api/records?plate='+encodeURIComponent(PLATE)+'&page_size=20',{headers})).json();
  const listRec=(recs.items||[])[0];assert.ok(listRec,'record');
  const det=await page.request.get(base+'/api/records/'+listRec.id,{headers});assert.equal(det.status(),200);
  const rec=await det.json();assert.equal(rec.status,'out');assert.ok(rec.exit_photo);
  assert.equal(rec.payment?.method,'auto');
  assert.ok(entry&&quote&&paid,JSON.stringify({entry,quote,paid,stages}));
  assert.ok(textureCheck,'exit quote must be captured');
  assert.equal(textureCheck.before,textureCheck.after);assert.ok(textureCheck.sameMaterial&&textureCheck.sameTexture);
  assert.equal(payRequests.length,0,'auto settlement must not call /api/pay');
  assert.deepEqual(errors,[]);
  const final=await page.evaluate(async()=>({free:(await __SIM__.simApi.rest.snapshot()).layout.free_total,vehicles:__SIM__.scheduler.vehicles.size}));
  assert.equal(final.free,80);assert.equal(final.vehicles,0);
  fs.writeFileSync(out+'/displays.json',JSON.stringify({stages,textureCheck,record:rec,payRequests,errors},null,2));
  console.log(JSON.stringify({entry,quote,paid,texture:true,amount:rec.payment.amount_paid,exitPhoto:!!rec.exit_photo,payRequests:payRequests.length,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
