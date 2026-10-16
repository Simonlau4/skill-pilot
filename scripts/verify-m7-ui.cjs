// A separate browser with an empty temporary profile. Never attach to the user's browser.
const fs = require('node:fs');
const {chromium} = require(require('path').join(process.env.HOME, '.npm/_npx/31e32ef8478fbf80/node_modules/playwright'));
(async () => {
 const browser = await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
 const page = await browser.newPage({viewport:{width:1280,height:720}});
 const errors=[]; page.on('pageerror',error=>errors.push(error.message));
 let promptValue=''; page.on('dialog',dialog=>dialog.accept(dialog.type()==='prompt'?promptValue:undefined));
 const {url,home}=JSON.parse(fs.readFileSync('work/m7-ui-runtime.json','utf8'));
 const result={isolated_profile:true,home,checks:[]};
 function check(condition,label) {if(!condition) throw new Error(label);result.checks.push(label);}
 try {
  await page.goto(url); await page.locator('#mode-badge').getByText('可以管理技能').waitFor();
  const registry=()=>JSON.parse(fs.readFileSync(home+'/.skill-pilot/registry.json','utf8'));
  await page.locator('[data-panel="assignments"]').click();
  await page.getByLabel('Claude 使用 grill-me',{exact:true}).check();
  await page.locator('#pending-reasons').getByText(/grilling/).waitFor();
  check(await page.getByLabel('Claude 使用 grilling',{exact:true}).isChecked(),'勾 grill-me 自动带上 grilling');
  const assignment=page.waitForResponse(r=>r.url().endsWith('/api/assignment/apply'));
  await page.locator('#assignment-apply').click(); await assignment;
  await page.getByLabel('Claude 使用 grill-me',{exact:true}).waitFor();
  check(registry().skills['grill-me'].assigned_agents.includes('claude') && registry().skills.grilling.assigned_agents.includes('claude'),'Claude 分配已落盘');
  promptValue='other';
  await page.locator('[data-add-dependency="grill-me"]').click();
  await page.locator('[data-remove-dependency="other"]').waitFor();
  check(registry().skills['grill-me'].requires.includes('other'),'手动添加需要的技能已落盘');
  await page.locator('[data-dependency-skill="grill-me"][data-remove-dependency="other"]').click();
  await page.locator('[data-dependency-skill="grill-me"][data-remove-dependency="other"]').waitFor({state:'detached'});
  check(!registry().skills['grill-me'].requires.includes('other'),'手动删除需要的技能已落盘');
  await page.locator('[data-panel="updates"]').click(); await page.locator('#updates-check').click();
  const choice=page.locator('[data-update-choice="sample"]'); await choice.waitFor();
  check(await choice.inputValue()==='upstream','本机改过默认用上游的');
  check(await page.locator('#updates-list').innerText().then(t=>t.includes('curl https://example.test') && t.includes('rm -rf temporary')),'风险规则列出文件与原文');
  await choice.scrollIntoViewIfNeeded(); await page.screenshot({path:'work/m7-updates-ui.png'});
  await page.locator('[data-update-skill="sample"]').check(); await page.locator('#updates-apply').click();
  await page.locator('#updates-status').getByText(/核对并更新了/).waitFor();
  check(fs.readFileSync(home+'/.skill-pilot/store/sample/SKILL.md','utf8').includes('上游新版说明'),'默认更新写入上游内容');
  check(!fs.readFileSync(home+'/.skill-pilot/store/sample/SKILL.md','utf8').includes('我修改的说明'),'本机修改已由上游替换');
  await page.locator('[data-panel="operation-log"]').click(); const undoUpdate=page.waitForResponse(r=>r.url().endsWith('/api/undo')); await page.locator('[data-undo]').first().click(); await undoUpdate;
  await page.locator('#operation-list').getByText('已撤销',{exact:true}).first().waitFor();
  check(fs.readFileSync(home+'/.skill-pilot/store/sample/SKILL.md','utf8').includes('我修改的说明'),'撤销更新恢复本机改动');
  check(!errors.length,'浏览器没有 JavaScript 异常');
 } finally {await page.close();await browser.close();fs.writeFileSync('work/m7-ui-acceptance.json',JSON.stringify({...result,errors},null,2)+'\n');}
 console.log(JSON.stringify(result));
})().catch(error=>{console.error(error);process.exit(1)});
