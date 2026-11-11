// 2026-11-06 P3：HUD 下一事件两行（event/forecast），长文本自动换行、无 ellipsis、不越界/遮挡。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9670';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261106/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
const LONG_EVENT='t=123456.789s 派车 粤F4K1CZAA 长文本自动换行验证 连续文字不截断';
const LONG_FORECAST='预计出场/缴费：预计 123456s 出场/缴费 粤F4K1CZAA（预计，不含排队/故障）';
(async()=>{
 const browser=await chromium.launch({headless:true,args:(process.env.E2E_GPU
   ?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11']
   :['--enable-unsafe-swiftshader','--use-angle=swiftshader']),
   ...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 try{
  const page=await browser.newPage({viewport:{width:1920,height:1080}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  await page.goto(base+'/sim/');await page.waitForFunction(()=>__SIM__?.hud?.setScenarioProgress,null,{timeout:90000});
  await page.evaluate(({event,forecast})=>{
    __SIM__.hud.setScenarioProgress({name:'P3 长文本',description:'',clock:123456.789,
      totalDuration:999999,totalVehicles:1,completedVehicles:0,enteredVehicles:0,exitedVehicles:0,
      nextEvent:event,nextForecast:forecast,playing:true,finished:false,preloadError:null});
  },{event:LONG_EVENT,forecast:LONG_FORECAST});
  await page.waitForTimeout(300);
  const info=await page.evaluate(()=>{
    const parent=document.getElementById('scn-next');
    const ev=document.getElementById('scn-next-event'),fc=document.getElementById('scn-next-forecast');
    const cs=getComputedStyle(parent),ce=getComputedStyle(ev),cf=getComputedStyle(fc);
    const box=document.getElementById('scenario-progress').getBoundingClientRect();
    const feed=document.querySelector('.feed')?.getBoundingClientRect()||null;
    return {children:parent.children.length,eventText:ev.textContent,forecastText:fc.textContent,
      parentHeight:parent.getBoundingClientRect().height,eventHeight:ev.getBoundingClientRect().height,forecastHeight:fc.getBoundingClientRect().height,
      parentWhiteSpace:cs.whiteSpace,eventWhiteSpace:ce.whiteSpace,forecastWhiteSpace:cf.whiteSpace,
      eventOverflow:ce.textOverflow,forecastOverflow:cf.textOverflow,
      eventScroll:ev.scrollWidth,eventClient:ev.clientWidth,forecastScroll:fc.scrollWidth,forecastClient:fc.clientWidth,
      panel:{x:box.x,right:box.right,width:box.width},feed:feed?{x:feed.x}:null,
      docWidth:document.documentElement.scrollWidth,innerWidth:window.innerWidth};
  });
  assert.equal(info.children,2,'#scn-next must have two lines');
  assert.ok(info.eventText.startsWith('下一事件：'));
  assert.ok(info.forecastText.startsWith('预计出场/缴费：'));
  assert.equal(info.parentWhiteSpace,'normal');assert.equal(info.eventWhiteSpace,'normal');assert.equal(info.forecastWhiteSpace,'normal');
  assert.notEqual(info.eventOverflow,'ellipsis');assert.notEqual(info.forecastOverflow,'ellipsis');
  assert.ok(info.eventScroll<=info.eventClient+1,'event line no horizontal overflow '+JSON.stringify(info));
  assert.ok(info.forecastScroll<=info.forecastClient+1,'forecast line no horizontal overflow');
  assert.ok(info.eventHeight>25&&info.forecastHeight>25,'two lines visible');
  assert.ok(info.parentHeight>=info.eventHeight+info.forecastHeight,'two-line parent height');
  assert.ok(!info.feed||info.panel.right<info.feed.x+1,'HUD panel must not overlap feed');
  await page.screenshot({path:out+'/p3-hud-two-line.png'});
  // mobile
  await page.setViewportSize({width:390,height:844});
  await page.evaluate(({event,forecast})=>{
    __SIM__.hud.setScenarioProgress({name:'P3 长文本',description:'',clock:123456.789,
      totalDuration:999999,totalVehicles:1,completedVehicles:0,enteredVehicles:0,exitedVehicles:0,
      nextEvent:event,nextForecast:forecast,playing:true,finished:false,preloadError:null});
  },{event:LONG_EVENT,forecast:LONG_FORECAST});
  await page.waitForTimeout(300);
  const mobile=await page.evaluate(()=>{
    const ev=document.getElementById('scn-next-event'),fc=document.getElementById('scn-next-forecast');
    return {docWidth:document.documentElement.scrollWidth,innerWidth:window.innerWidth,
      eventScroll:ev.scrollWidth,eventClient:ev.clientWidth,forecastScroll:fc.scrollWidth,forecastClient:fc.clientWidth,
      eventOverflow:getComputedStyle(ev).textOverflow};
  });
  assert.equal(mobile.docWidth,mobile.innerWidth,'mobile no horizontal page overflow');
  assert.ok(mobile.eventScroll<=mobile.eventClient+1&&mobile.forecastScroll<=mobile.forecastClient+1,'mobile lines wrap');
  assert.notEqual(mobile.eventOverflow,'ellipsis');
  await page.screenshot({path:out+'/p3-hud-two-line-mobile.png'});
  assert.deepEqual(errors,[]);
  const artifact={info,mobile,errors};fs.writeFileSync(out+'/p3-hud.json',JSON.stringify(artifact,null,2));
  console.log(JSON.stringify({desktop:info,mobile,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
