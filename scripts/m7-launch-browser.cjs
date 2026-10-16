// Controlled browser used only for the launcher acceptance; its profile is disposable.
const fs=require('node:fs');
const {chromium}=require(require('path').join(process.env.HOME,'.npm/_npx/31e32ef8478fbf80/node_modules/playwright'));
(async()=>{
 const root=process.env.M7_BROWSER_RUNTIME;
 if(!root || !root.startsWith('/private/var/folders/')) throw new Error('需要系统临时目录');
 const browser=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
 const page=await browser.newPage({viewport:{width:1280,height:720}});
 await page.goto(process.argv[2]);
 await page.locator('#mode-badge').getByText('只读预览').waitFor();
 const text=await page.locator('#cards').innerText();
 await page.locator('[data-panel="assignments"]').click();
 await page.getByLabel('Claude 使用 grill-me',{exact:true}).waitFor();
 const disabled=await page.getByLabel('Claude 使用 grill-me',{exact:true}).isDisabled();
 fs.writeFileSync(`${root}/${process.pid}.json`,JSON.stringify({url:process.argv[2],text,disabled,ready_at:Date.now()}));
 await page.screenshot({path:`${root}/real-dashboard.png`});
 const timer=setInterval(async()=>{
  if(!fs.existsSync(root)||fs.existsSync(root+'/close')){
   clearInterval(timer);await page.close();await browser.close();
   if(fs.existsSync(root)) fs.writeFileSync(`${root}/${process.pid}.closed`,String(Date.now()));
  }
 },100);
})().catch(error=>{console.error(error);process.exit(1)});
