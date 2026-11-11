const {chromium}=require('playwright'),fs=require('fs'),assert=require('assert/strict');
const base=process.env.PARKING_URL||'http://127.0.0.1:9633',out='../_qa_audit/repair/P1-16/evidence';fs.mkdirSync(out,{recursive:true});
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH,args:(process.env.E2E_GPU?['--enable-gpu','--ignore-gpu-blocklist','--use-angle=d3d11']:['--enable-unsafe-swiftshader','--use-angle=swiftshader'])});try{
const page=await browser.newPage({viewport:{width:1280,height:800}}),errors=[];page.on('pageerror',e=>errors.push(String(e)));
const auth=await page.request.post(base+'/api/auth/login',{data:{username:'admin',password:'admin123'}});assert.equal(auth.status(),200);const token=(await auth.json()).token,headers={Authorization:'Bearer '+token};
async function post(url,data){const r=await page.request.post(base+url,{headers,data});assert.ok(r.ok(),await r.text());return r.json();}
await page.goto(base+'/admin/');await page.evaluate(t=>localStorage.setItem('parking.admin.token',t),token);await page.goto(base+'/admin/#/incidents');
await page.getByRole('heading',{name:'异常事件',exact:false}).waitFor();
await post('/api/camera/in/inject',{reason:'P1-16实时刷新验收'});
await page.getByRole('cell').filter({hasText:'P1-16实时刷新验收'}).first().waitFor({timeout:12000});await page.screenshot({path:out+'/incident-live.png',fullPage:true});
const r=await page.request.get(base+'/api/incidents?status=open&limit=200',{headers}),items=(await r.json()).items;
const incident=items.find(i=>JSON.stringify(i.detail).includes('P1-16实时刷新验收'));assert.ok(incident);
await post('/api/incidents/'+incident.id+'/resolve',{resolution:'另一个管理端处理'});
await page.getByRole('cell').filter({hasText:'P1-16实时刷新验收'}).waitFor({state:'hidden',timeout:12000});await page.screenshot({path:out+'/incident-resolved-live.png',fullPage:true});
await post('/api/camera/in/recover',{});await page.goto(base+'/admin/#/monitor');await page.getByText('实时在线',{exact:true}).waitFor();
await post('/api/camera/in/inject',{reason:'P1-16原因透传'});await post('/sim/events/arrived',{vehicle_id:'display-fault',gate:'in',image_path:'datasets/plates/blue/blue_00000.jpg'});
await page.locator('.feed code').filter({hasText:'camera_fault'}).first().waitFor({timeout:10000});
await page.screenshot({path:out+'/reason-code.png',fullPage:true});await post('/api/camera/in/recover',{});
assert.deepEqual(errors,[]);fs.writeFileSync(out+'/browser.json',JSON.stringify({incidentId:incident.id,autoRefresh:true,externalResolveRefresh:true,reason:'camera_fault',errors},null,2));console.log('admin display passed');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
