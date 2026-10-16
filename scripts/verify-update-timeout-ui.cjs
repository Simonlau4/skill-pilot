// Reuse the repository's isolated Playwright runner with the timeout fixture.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {chromium} = require(require('path').join(process.env.HOME, '.npm/_npx/31e32ef8478fbf80/node_modules/playwright'));
(async () => {
 const runtime=JSON.parse(fs.readFileSync('work/fix-update-check-ui-runtime.json','utf8'));
 const temporary=fs.mkdtempSync(path.join(os.tmpdir(),'skill-pilot-timeout-browser-'));
 const browser=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true,
  env:{...process.env,HOME:temporary,CFFIXED_USER_HOME:temporary,XDG_CONFIG_HOME:path.join(temporary,'.config'),XDG_CACHE_HOME:path.join(temporary,'.cache')}});
 const page=await browser.newPage();
 const result={checks:[],browser_errors:[]};
 page.on('pageerror',error=>result.browser_errors.push(error.message));
 page.on('dialog',dialog=>dialog.accept());
 const check=(ok,label)=>{if(!ok) throw Error(label);result.checks.push(label);console.log(label);};
 const row=page.locator('[data-update-row="sample"]');
 const count=()=>Number(fs.readFileSync(runtime.history_calls,'utf8'));
 try {
  await page.goto(runtime.url);
  await page.getByRole('button',{name:'检查更新',exact:true}).click();
  await page.locator('#updates-check').click();
  await row.waitFor();
  check((await row.innerText()).includes('无法判断你是否改过，默认用上游的。'),'不确定基准显示清楚说明');
  check(!(await row.innerText()).includes('本机没改过'),'没有把未知情况说成没改过');
  check(await row.locator('select').inputValue()==='upstream','默认选择用上游的');
  check(count()===0,'检查完成第一条时没有查询历史');
  await row.locator('select').selectOption('keep');
  await row.getByText('正在找你改过的地方…',{exact:true}).waitFor();
  check(await row.getByRole('button',{name:'更新',exact:true}).isDisabled(),'找改动期间更新按钮暂停');
  await row.locator('select:enabled').waitFor();
  check(count()===1,'选择保留我的才查询历史');
  check((await row.locator('[data-update-merged]').innerText()).includes('my local edit'),'校准结果显示实际改动');
  await page.reload();
  await page.getByRole('button',{name:'检查更新',exact:true}).click();
  await row.waitFor();
  check((await row.innerText()).includes('你改过'),'刷新后读取已经校准的结果');
  check(count()===1,'刷新不会重复查询历史');
  await row.getByRole('button',{name:'更新',exact:true}).click();
  await row.getByText('已更新',{exact:true}).waitFor();
  check(count()===1,'默认更新不再查询历史');
  check(fs.readFileSync(runtime.home+'/.skill-pilot/store/sample/SKILL.md','utf8').includes('new upstream'),'更新写入上游当前内容');
  check(!fs.readFileSync(runtime.home+'/.skill-pilot/store/sample/SKILL.md','utf8').includes('my local edit'),'按默认选择替换本机修改');
  fs.writeFileSync(runtime.release,'continue');
  await page.locator('#updates-status').getByText(/检查完成/).waitFor();
  check(result.browser_errors.length===0,'没有浏览器脚本异常');
  check(count()===1,'其余检查完成后没有增加历史查询');
  fs.writeFileSync('work/fix-update-timeout-ui-acceptance.json',JSON.stringify(result,null,2)+'\n');
 } finally {
  fs.writeFileSync(runtime.release,'continue');
  await browser.close();
  fs.rmSync(temporary,{recursive:true,force:true});
 }
})().catch(error=>{console.error(error);process.exit(1);});
