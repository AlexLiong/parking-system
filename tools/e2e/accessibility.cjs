const {chromium}=require('playwright'),fs=require('fs'),assert=require('assert/strict');
const base=process.env.PARKING_URL||'http://127.0.0.1:9639',out='../_qa_audit/repair/P3-11/evidence';fs.mkdirSync(out,{recursive:true});
(async()=>{const b=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH});try{
 const p=await b.newPage({viewport:{width:390,height:844}}),errors=[],checks=[];p.setDefaultTimeout(15000);p.on('pageerror',e=>errors.push(String(e)));
 await p.goto(base+'/admin/');await p.getByLabel('用户名',{exact:true}).fill('admin');await p.getByLabel('密码',{exact:true}).fill('bad-password');await p.getByLabel('密码',{exact:true}).press('Enter');await p.getByRole('alert').filter({hasText:/密码|错误|失败/}).waitFor();await p.screenshot({path:out+'/login-error.png'});
 await p.getByLabel('密码',{exact:true}).fill('admin123');await p.getByLabel('密码',{exact:true}).press('Enter');await p.getByRole('combobox',{name:'页面导航',exact:true}).waitFor();
 for(const width of [390,768]){
  await p.setViewportSize({width,height:844});
  for(const route of ['monitor','records','recognize','find-car','lists']){
   await p.goto(base+'/admin/#/'+route);await p.locator('main h2').first().waitFor();await p.waitForTimeout(300);
   const data=await p.evaluate(()=>({width:innerWidth,body:document.documentElement.scrollWidth,missing:[...document.querySelectorAll('main input,main select,main textarea')].filter(n=>n.type!=='hidden'&&n.getClientRects().length&&!n.labels?.length&&!n.getAttribute('aria-label')).map(n=>n.outerHTML)}));
   assert.ok(data.body<=data.width+1,`${route}/${width} overflow ${JSON.stringify(data)}`);assert.deepEqual(data.missing,[],route);checks.push({route,...data});await p.screenshot({path:`${out}/${route}-${width}.png`});
  }
 }
 await p.setViewportSize({width:390,height:844});await p.getByRole('combobox',{name:'页面导航',exact:true}).selectOption('/find-car');await p.waitForFunction(()=>location.hash.includes('find-car'));await p.getByLabel('完整车牌').waitFor({timeout:30000});await p.getByLabel('完整车牌').fill('京A00000');await p.getByLabel('完整车牌').press('Enter');await p.getByRole('alert').filter({hasText:'未找到'}).waitFor();
 await p.getByRole('combobox',{name:'页面导航',exact:true}).selectOption('/lists');const open=p.getByRole('button',{name:/新增/}).first();await open.focus();await p.keyboard.press('Enter');const dialog=p.getByRole('dialog');await dialog.waitFor();assert.equal(await dialog.getAttribute('aria-modal'),'true');await p.getByLabel('车牌',{exact:true}).waitFor();assert.equal(await p.locator('#app').evaluate(n=>n.inert),true);
 const controls=dialog.locator('button,input,select,textarea');const last=controls.last();await last.focus();await p.keyboard.press('Tab');assert.equal(await dialog.locator('button').first().evaluate(n=>n===document.activeElement),true);await p.keyboard.press('Shift+Tab');assert.equal(await last.evaluate(n=>n===document.activeElement),true);
 await p.screenshot({path:out+'/keyboard-dialog.png'});await p.keyboard.press('Escape');await dialog.waitFor({state:'detached'});assert.equal(await open.evaluate(n=>n===document.activeElement),true);assert.equal(await p.locator('#app').evaluate(n=>n.inert),false);
 await p.getByLabel('导航语言').selectOption('en');await p.getByRole('combobox',{name:'Page navigation',exact:true}).waitFor();assert.equal(await p.locator('html').getAttribute('lang'),'en');await p.getByRole('combobox',{name:'Page navigation',exact:true}).selectOption('/find-car');await p.waitForFunction(()=>location.hash.includes('find-car'));await p.getByLabel('Full plate').waitFor({timeout:30000});await p.screenshot({path:out+'/english-navigation.png'});
 const colors=await p.evaluate(()=>{const s=getComputedStyle(document.documentElement);return Object.fromEntries(['--ink-3','--ok','--warn','--danger'].map(k=>[k,s.getPropertyValue(k).trim()]));});
 const contrast=c=>{const a=c.match(/\w\w/g).map(x=>parseInt(x,16)/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4);return 1.05/(a[0]*.2126+a[1]*.7152+a[2]*.0722+.05);};
 const ratios=Object.fromEntries(Object.entries(colors).map(([k,v])=>[k,contrast(v.slice(1))]));for(const r of Object.values(ratios))assert.ok(r>=4.5);assert.deepEqual(errors,[]);fs.writeFileSync(out+'/browser.json',JSON.stringify({checks,keyboardDialog:true,focusRestored:true,errorState:true,mobileNavigation:true,englishNavigation:true,ratios,errors},null,2));console.log('accessibility passed');
 }finally{await b.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
