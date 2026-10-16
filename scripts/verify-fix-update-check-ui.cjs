// Existing repository convention: isolated Playwright acceptance, no package install.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require(require('path').join(process.env.HOME, '.npm/_npx/31e32ef8478fbf80/node_modules/playwright'));
(async () => {
 const runtime=JSON.parse(fs.readFileSync('work/fix-update-check-ui-runtime.json','utf8'));
 const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'skill-pilot-update-browser-'));
 const browser=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,
  env:{...process.env,HOME:temporary,CFFIXED_USER_HOME:temporary,XDG_CONFIG_HOME:path.join(temporary,'.config'),XDG_CACHE_HOME:path.join(temporary,'.cache')}});
 let page=await browser.newPage();
 const errors=[];page.on('pageerror',e=>errors.push(e.message));page.on('dialog',dialog=>dialog.accept());
 const result={checks:[],browser_errors:errors};
 const check=(ok,label)=>{if(!ok) throw Error(label);result.checks.push(label);console.log(label);};
 const row=name=>page.locator(`[data-update-row="${name}"]`);
 try {
  await page.goto(runtime.url);
  await page.getByRole('button',{name:'检查更新',exact:true}).click();
  const started=Date.now();
  await page.locator('#updates-check').click();
  await page.locator('#updates-progress').getByText(/共 3 个技能 \/ 2 个仓库/).waitFor();
  check(Date.now()-started<1000,'点击后立即显示总技能数、仓库数、已查和剩余');
  await row('sample').waitFor();
  check((await page.locator('#updates-progress').innerText()).includes('正在查'),'首条有更新时显示仍在检查的仓库');
  check(await row('sample').getByText('脚本或执行权限有变化，请先看',{exact:true}).isVisible(),'首条立即显示改动和脚本提醒');
  check(await row('sample').getByRole('button',{name:'更新',exact:true}).isEnabled(),'扫描未完成时首条更新按钮可用');
  check(await row('sample').locator('select').inputValue()==='upstream','本机改过的技能默认用上游的');
  await row('sample').locator('select').selectOption('keep');
  await page.waitForFunction(()=>document.querySelector('#updates-other')?.textContent.includes('检查超时'));
  check(await row('sample').locator('select').inputValue()==='keep','可选择保留我的，进度刷新不会重置选择');
  await row('sample').locator('select').selectOption('upstream');
  check(!(await page.locator('#updates-other').innerText()).includes('archived'),'默认不检查已撤下的技能');
  check((await page.locator('#updates-progress').innerText()).includes('剩 1'),'仓库超时计入已查，其他技能继续等待');
  const reviewId=await page.evaluate(async()=>{
   const token=new URLSearchParams(location.search).get('token');
   return (await (await fetch('/api/updates/progress',{headers:{'X-Skill-Pilot-Token':token}})).json()).id;
  });
  await row('sample').getByRole('button',{name:'更新',exact:true}).click();
  await row('sample').getByText('已更新',{exact:true}).waitFor();
  check(fs.readFileSync(runtime.home+'/.skill-pilot/store/sample/SKILL.md','utf8').includes('new upstream'),'逐条更新确实写入模拟技能，保留分配');
  check(await row('sample').getByRole('button',{name:'更新',exact:true}).isDisabled(),'已更新条目保留并禁用重复更新');
  check(await page.locator('#updates-progress').isVisible(),'逐条更新后扫描仍在运行');
  await page.reload();
  await page.locator('#updates-progress').getByText(/剩 1/).waitFor();
  await page.getByRole('button',{name:'检查更新',exact:true}).click();
  await row('sample').getByText('已更新',{exact:true}).waitFor();
  const after=await page.evaluate(async()=>{
   const token=new URLSearchParams(location.search).get('token');
   return (await (await fetch('/api/updates/progress',{headers:{'X-Skill-Pilot-Token':token}})).json()).id;
  });
  check(after===reviewId,'刷新后接回同一后台任务和已更新结果');
  await page.close();page=await browser.newPage();
  page.on('pageerror',e=>errors.push(e.message));page.on('dialog',dialog=>dialog.accept());
  await page.goto(runtime.url);
  await page.locator('#updates-progress').getByText(/剩 1/).waitFor();
  await page.getByRole('button',{name:'检查更新',exact:true}).click();
  await row('sample').getByText('已更新',{exact:true}).waitFor();
  check(true,'关闭页面后重开，正在扫描的进度和已更新条目接上');
  fs.writeFileSync(runtime.release,'release');
  await row('zsecond').waitFor();
  await page.locator('#updates-progress').waitFor({state:'hidden'});
  check((await page.locator('#updates-status').innerText()).includes('已查 3，剩 0'),'完成后显示完整汇总和失败数量');
  check((await row('sample').innerText()).includes('已更新'),'后续扫描不覆盖已更新条目');
  check(await row('zsecond').getByRole('button',{name:'更新',exact:true}).isEnabled(),'后续查出的条目仍可更新，不受此前写入影响');
  check(await page.locator('#updates-all').isEnabled(),'保留更新全部已查出的入口');
  await page.locator('#updates-all').click();
  await row('zsecond').getByText('已更新',{exact:true}).waitFor();
  check(fs.readFileSync(runtime.home+'/.skill-pilot/store/zsecond/SKILL.md','utf8').includes('new'),'更新全部已查出的可完成剩余更新');
  await page.reload();
  await page.getByRole('button',{name:'检查更新',exact:true}).click();
  await row('zsecond').getByText('已更新',{exact:true}).waitFor();
  check((await row('sample').innerText()).includes('已更新'),'任务结束后刷新仍保留更新结果');
  await page.getByRole('button',{name:'已撤下',exact:true}).click();
  await page.locator('#withdrawn-check').click();
  await row('archived').waitFor();
  check((await page.locator('#updates-status').innerText()).includes('共 1 个技能'),'已撤下页可单独检查，只扫描已撤下项');
  await page.setViewportSize({width:375,height:812});
  check(await row('archived').getByRole('button',{name:'更新',exact:true}).isVisible(),'窄屏仍可看到逐条更新按钮');
  check(errors.length===0,'浏览器没有 JavaScript 异常');
  result.status='passed';
 } catch(error) {result.status='failed';result.error=error.message;throw error;}
 finally {
  fs.writeFileSync(runtime.release,'release');
  await page.close();await browser.close();fs.rmSync(temporary,{recursive:true,force:true});
  fs.writeFileSync('work/fix-update-check-ui-acceptance.json',JSON.stringify(result,null,2)+'\n');
 }
})().catch(error=>{console.error(error);process.exitCode=1;});
