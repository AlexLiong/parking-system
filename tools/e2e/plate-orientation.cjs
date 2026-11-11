// 2026-10-31 车牌方向：正常入口前牌/出口后牌；自动出场照片 OCR exact，图片文字正向（非镜像/倒置）。
const { chromium } = require('playwright');
const fs = require('fs'), assert = require('assert/strict');
const { spawnDispatchNoWait, waitParked } = require('./e2e_support.cjs');
const base = process.env.PARKING_URL || 'http://127.0.0.1:9654';
const out = process.env.E2E_SCREENSHOT_DIR || '../_qa_audit/repair_tasks_20261031/E2E/screenshots';
fs.mkdirSync(out,{recursive:true});
const PLATE='粤F4K1CZ';
(async()=>{
 const browser=await chromium.launch({headless:true,args:(process.env.E2E_GPU
   ?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11','--disable-background-timer-throttling']
   :['--enable-unsafe-swiftshader','--use-angle=swiftshader','--disable-background-timer-throttling']),
   ...(process.env.CHROMIUM_PATH?{executablePath:process.env.CHROMIUM_PATH}:{})});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];
  page.on('pageerror',e=>errors.push(String(e)));
  await page.goto(base+'/sim/');await page.waitForFunction(()=>__SIM__?.simApi?.rt?.sessionId,null,{timeout:90000});
  const login=await page.request.post(base+'/api/auth/login',{data:{username:'admin',password:'admin123'}});
  assert.equal(login.status(),200);const token=(await login.json()).token,headers={Authorization:'Bearer '+token};
  await page.evaluate(async()=>{await __SIM__.simApi.resetDemo();await __SIM__.controls.setSpeed(10);});
  const car=await spawnDispatchNoWait(page,{vehicleId:'plate-orient-car',plate:PLATE,plateType:'blue',planDurationSec:600});
  await waitParked(page,car.id,PLATE,90000);
  await page.evaluate(id=>__SIM__.scheduler.startExit(id),car.id);
  await page.waitForFunction(id=>!__SIM__.scheduler.vehicles.get(id),car.id,{timeout:90000});
  const recs=await (await page.request.get(base+'/api/records?plate='+encodeURIComponent(PLATE)+'&page_size=20',{headers})).json();
  const rec=(recs.items||[])[0];assert.ok(rec,'record');assert.equal(rec.status,'out');
  assert.ok(rec.exit_photo,'exit_photo must exist');
  // 服务器端真实 OCR 识别出场照片：exact + need_manual=false，镜像/倒置牌面会失败
  const recogResp=await page.request.post(base+'/api/recognize?gate=out&image_path='+encodeURIComponent(rec.exit_photo));
  assert.equal(recogResp.status(),200,await recogResp.text());
  const recog=await recogResp.json();
  const recognized=(recog.plate||recog.candidates?.[0]||'').replace(/[·.\-\s]/g,'').toUpperCase();
  const expected=PLATE.replace(/[·.\-\s]/g,'').toUpperCase();
  assert.equal(recognized,expected,'exit photo OCR must read correctly (not flipped): '+JSON.stringify(recog));
  assert.equal(!!recog.need_manual,false);
  assert.equal(recog.success!==false,true);
  // 真正打开出场照片并截图（确认正向显示）
  const bytes=fs.readFileSync(rec.exit_photo);fs.writeFileSync(out+'/exit-photo.png',bytes);
  const b64=bytes.toString('base64');
  const ext=/\.(jpg|jpeg)$/i.test(rec.exit_photo)?'image/jpeg':'image/png';
  await page.setContent('<html><body style="background:#111"><img id="p" src="data:'+ext+';base64,'+b64+'" style="max-width:95vw;max-height:95vh;display:block;margin:auto"></body></html>');
  await page.screenshot({path:out+'/exit-photo-upright.png'});
  assert.deepEqual(errors,[]);
  const artifact={record:rec,recog,exitPhoto:rec.exit_photo,errors};
  fs.writeFileSync(out+'/plate-orientation.json',JSON.stringify(artifact,null,2));
  console.log(JSON.stringify({plate:PLATE,exitPhoto:rec.exit_photo,recognized:recog.plate,
    confidence:recog.overall_conf??recog.conf,needManual:recog.need_manual,errors}));
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
