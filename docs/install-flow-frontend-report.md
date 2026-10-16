# 报告：安装技能少点几下（前端）

> 2026-10-14 由 DSH 执行，对应交接文件 [handoff-install-flow-frontend.md](handoff-install-flow-frontend.md)。工作区 `~/Developer/skill-pilot-install-frontend`，只改了 `skill_pilot/web/static/app.js` 和 `skill_pilot/web/static/index.html` 两个文件加本报告；没有动 Python、测试和别的文档，没有 `git add`、没有提交，改动保持未暂存。

## 1. 结果一览

| 验收项 | 结果 |
| --- | --- |
| 1. `node --check skill_pilot/web/static/app.js` | 通过 |
| 2. 本报告（改了哪些函数、现在的每一步、1～8 逐条对照） | 见 §2～§4 |
| 桩 DOM 脚本自检（Node `vm` 把 `app.js` 原样跑起来，假接口） | 39/39 通过，见 §5 |
| 全量单测 `unittest discover -q`（回归） | 183 项全部通过（54 秒） |
| `index.html` 标签闭合检查 | 无未闭合、无错配 |
| 浏览器实际点一遍 | 不在我的环境里，交给 Claude 在模拟 home 上验收 |

改动量：`app.js` 2729 → 2797 行，`index.html` 443 → 445 行；两个文件合计 +104 / −34 行。`app.css` 没有改动（新的“重试”按钮复用现有 `.btn-small`；“已经装过”用中性的 `.tag-quiet`，和“已撤销”“已忽略”“内容不同”这些小标记同一档，不引入 `DESIGN.md` 之外的绿/红色）。

## 2. 改了哪些地方

全部在 `skill_pilot/web/static/app.js` 的“安装技能：对话框”一段，加上 `index.html` 里 `#install-dialog` 的几行。

| 位置 | 改法 |
| --- | --- |
| `openInstall()` | 重置读取状态（读过的链接 `installInspectedUrl`、正在读的链接 `installReadingUrl`、读取序号、防抖计时器、重试按钮）；给 agent 复选框加上“正在看的那个默认勾上” |
| `inspectInstall(force)` | 新增。原来的读取逻辑从 `#install-inspect` 点击回调里抽出来，加了“同一个链接不重复读”“同一个链接正在读时不重复发请求”“读取中换链接丢弃旧结果”“出错显示原因 + 重试” |
| `renderInstallSkills(review)` | 新增。原来揉在读取回调里的一行模板拆成函数：默认全勾、已经装过不能勾、同名内容不同默认不勾、全都装过时的状态行、读完把焦点放到“安装”上 |
| `renderInstallApply()` | 新增。“一个技能都没勾”时把“安装”按钮置灰 |
| `installDoneText(skills, agents)` | 新增。拼完成提示的一句话（一个技能写原名，多个写数量；给了谁 / 还没给 agent 用） |
| `$('install-apply')` 点击回调 | 去掉 `confirmDialog`；成功后关窗、`refreshEverything()`、`llm_configured` 为真时补一次 `startLabels('missing')`；完成提示换成 `installDoneText()` |
| `$('install-url')` 的 `input` / `paste` 事件 | 新增。粘贴立刻读；输入停下 600 毫秒且内容像 `https://github.com/…` 时读 |
| `$('install-dialog')` 表单 `submit` | 原来是把点击转给“读取”按钮，现在直接触发读取（回车） |
| `$('install-retry')` 点击、`$('install-skills')` 的 `change` | 新增。重试重新读；勾选变化时更新“安装”按钮 |
| 删掉的东西 | `index.html` 的“读取”按钮和它的点击处理；`openInstall` 里旧的 `installReview = null` 单行重置被完整重置取代 |
| `showIncomingConflicts()` | 一行没动，照旧给安装冲突和收编冲突两处用 |

顺带把这一段的“这个仓库里没找到技能。”改成“这个链接里没找到技能。”（`AGENTS.md` 要求主界面不出现“仓库”），其余用词照旧。

## 3. 对话框现在的每一步

1. 技能页点“安装”（或收编遇到同名时自动打开）：窗口打开，链接框空的，“安装”灰着；下面每个 agent 一个勾选框，**技能页正在看的那个已经勾上**，其余不勾。
2. 往链接框里**粘贴**（立刻读）、**按回车**（立刻读），或者手输完**停下约 0.6 秒**（内容像 `https://github.com/…` 时读）。状态行写“正在读取…”。
3. 读回来：每个技能一行，**默认全勾**；名字一样的里面——
   - 内容一样：显示“已经装过”，勾选框灰掉不能勾；
   - 内容不同：显示“已有同名技能”，默认不勾，留着让用户先看差别。
   状态行写“找到 N 个技能。”；**焦点跳到“安装”按钮上**，接着按一下回车就装。全都装过时状态行写“这些技能都已经装过了”。
4. 点“安装”→ 按钮变“正在安装…”，**没有第二个确认框**，直接开始装。
5. 装完：窗口自己关掉，列表自己刷新，右上角出现带“撤销”的提示（`装好了 lemo-opuscar，给了 Codex、Claude` / `装好了 3 个技能，给了 Codex` / 没选 agent 时 `…，还没给 agent 用`）。**不用刷新页面**。
6. 如果设置里填了 AI 服务的 key，装完紧接着让后台给新技能补中文名和标签：页头任务条显示已有的进度，正在整理的那批做完就轮到新技能，整理完列表自己出现中文名。
7. 读不成时：状态行写后台给的中文原因，右边出现“重试”小按钮；按回车或重试都能再读一次。
8. 安装时后台说“有同名技能需要先对比”：窗口不关，下面展开两份内容的差别，看完由用户决定。

## 4. 交接 1～8 逐条对照

**1. 粘贴就读取，去掉“读取”按钮。**
`index.html` 里 `#install-inspect` 已删除。自动读的三个入口：`paste` 事件（粘贴后立刻读，`setTimeout(…, 0)` 等浏览器把内容放进输入框）、表单 `submit`（回车，`event.preventDefault()` 后 `inspectInstall(true)`）、`input` 事件（`clearTimeout` + 600 毫秒防抖，`INSTALL_URL_PATTERN = /^https:\/\/github\.com\/\S+\/\S+/` 匹配才读，短于“owner/repo”的片段不读）。同一个链接读过就记在 `installInspectedUrl` 里，再触发直接返回；正在读的链接记在 `installReadingUrl` 里，同一个链接按两次回车也只发一次。读取用递增的 `installInspectSequence` 编号，结果回来时编号不是最新的就丢掉（用 `finally` 收尾，且只有仍是最新的那次才清 `installReadingUrl`，避免慢的那次把新的状态擦掉）。读取时状态行写“正在读取…”，同时清空上一次的技能行、差别区，并把“安装”置灰。出错时 `installReview = null`、状态行写 `error.message`（后台的中文原因）、`#install-retry` 显示出来。

**2. 找到的技能默认全勾。**
`renderInstallSkills()`：有冲突且 `same_content` 为真 → 勾选框带 `disabled`、不带 `checked`，下面一行“已经装过”的小标记加说明图标；有冲突但内容不同 → 不带 `checked`，保留原来的“已有同名技能”提示和说明；其余 → 带 `checked`。`allInstalled = skills.length > 0 && 每个技能都 same_content` 时状态行写“这些技能都已经装过了”；一个技能都没有时写“这个链接里没找到技能。”；其余写“找到 N 个技能。”。读完 `renderInstallApply()` 之后再 `$('install-apply').focus()`（按钮可点时才聚焦，见 §6）。

**3. 给哪些 agent：默认勾上技能页当前在看的那个。**
`openInstall()` 里 `const agent = currentAgent();`，每个 agent 的复选框按 `a.id === agent?.id` 决定加不加 `checked`；`currentAgent()` 本身没动（还是“当前看的 agent，没有就用列表第一个”）。用户可以多勾或取消。`index.html` 里那一行小提示改成“默认给你正在看的 agent；装完也可以在技能页里调”。

**4. 只点一次安装。**
按钮文字改成“安装”。`renderInstallApply()` 让 `#install-apply` 只在 `installReview` 存在且 `[data-install-skill]:checked` 至少有一个时可点（`change` 事件里重算，读完后也重算）。点击处理里原来的 `confirmDialog` 整段删掉，直接 `runWrite({button, busyText: '正在安装…', …})`，请求字段 `review_id / skills / agents / revision / confirmed` 与以前完全一样。

**5. 完成提示带“撤销”。**
`installDoneText(skills, agents)` 按交接给的三种说法拼：一个技能用 `skills[0]` 原名，多个用数量；`agents.map(agentName).join('、')`；没选 agent 时后半句是“，还没给 agent 用”。`success` 里照旧 `undoOperation(written.id, {skills, message: written.message, button: undoButton})`，撤销仍然先弹一次确认框（撤销是“动到分配”的操作，保留确认）。

**6. 装完不用刷新。**
`work()` 里：后台没有冲突时先 `$('install-dialog').close()`，再 `await refreshEverything()`（照旧），然后 `if (state.table.llm_configured) startLabels('missing')`。`startLabels` 原有逻辑负责进度条、结束时的 `refreshTable(true)` 和中文名出现；它开头有 `if (labelsRunning) return;`，页面已经在轮询时直接返回、由原来的轮询接着显示；后台在跑但本页没在轮询时，它会读 `/api/labels/progress` 发现 `running` 为真，只开始轮询、不再发一次整理请求（这两条都验过，见 §5）。

**7. 保留的东西。**
`showIncomingConflicts()` 和 `#incoming-conflicts` 差别区一行没动。收编遇到同名那条路（`openInstall()` 之后直接 `showIncomingConflicts(result, null)`）没有链接，所以：`openInstall()` 把链接框清空、读过的链接和读取序号一并重置；这次打开不会触发任何读取（没有 `input`/`paste`/`submit` 事件）；技能列表是空的，`renderInstallApply` 不会被调用，但 `openInstall()` 已经把“安装”置灰，所以不会出现“能点安装却没东西可装”。另外 `inspectInstall()` 对空链接是**直接返回、不写状态行**，这样在收编那条路上误按回车也不会把“有同名技能需要先对比……”的说明冲掉。只读模式下 `#skills-install` 仍然整个禁用，对话框打不开，和以前一样。

**8. 用词。**
“读取”“安装”“正在读取…”“正在安装…”“重试”“已经装过”“已有同名技能”“这些技能都已经装过了”“还没给 agent 用”都是中文大白话；不出现仓库、克隆、git；“只读取文件，不运行里面的程序”那一行保留。这一轮只把“这个仓库里没找到技能。”换成“这个链接里没找到技能。”。

## 5. 自己做的验证

1. **`node --check skill_pilot/web/static/app.js`** 通过。
2. **`index.html` 标签闭合**：用 Python 的 `html.parser` 走一遍，无未闭合、无错配。
3. **桩 DOM 脚本自检（39 项，39/39 通过）**：用 Node 的 `vm` 造一个最小 DOM 加假 `fetch`，把 `app.js` 原样载入（等于把页面打开，只是没有真实绘制），然后按真实事件顺序驱动安装对话框。脚本在仓库外的 `/tmp/sp-install-check/check.cjs`（另需把 `static/app.js` 复制成同目录的 `app.js`），不进仓库、随时可删。覆盖：
   - 打开对话框：默认只勾上正在看的 agent（Claude）、链接框空、“安装”灰；
   - 粘贴 `https://github.com/lemomo-ai/lemo-opuscar` 后**只发一次** `/api/install/inspect`，请求体 `{url}` 正确；
   - 普通技能默认勾上、同名内容不同默认不勾、已经装过带 `disabled` 且不勾、显示“已经装过”、状态行“找到 3 个技能。”、读完“安装”可点且**焦点在“安装”上**；
   - 同一个链接再触发（含 600 毫秒防抖到点）不再读；正在读时再按回车不再发；读取中把链接换成另一个、慢的那个结果回来后**不覆盖新的结果**；
   - 读取失败显示后台原文“仓库读取超时或工具暂时不可用，请稍后重试。”并露出“重试”，点“重试”能重读成功；
   - 点“安装”→ 请求体 `skills` 只含勾上的、`agents` 是默认的 Claude；装完对话框关闭、列表刷新（又发了一次 `/api/table`）、提示条写“装好了 lemo-opuscar，给了 Claude”且带“撤销”（点“撤销”会打开撤销确认框）；
   - 一个技能、没勾 agent 时提示“装好了 lemo-opuscar，还没给 agent 用”；多个技能时 `installDoneText` 给“装好了 3 个技能，给了 Claude、Codex”；
   - `llm_configured` 为真时，装完确实发了 `POST /api/labels/run {mode:'missing'}`；
   - 整理已经在后台跑时，`startLabels` 不再发一次，任务条接着显示“正在整理中文名和标签 1/5”；
   - 一个技能都不勾时“安装”不可点；全都装过时状态行“这些技能都已经装过了”且按钮不可点；
   - 安装时后台返回冲突：窗口不关、状态行“有同名技能需要先对比，请看下面的差别。”、差别区展开、`/api/incoming/compare` 被调用；
   - 收编遇到同名那条路：打开对话框加看差别，不发任何 `/api/install/inspect`，“安装”不可点，按回车也不会触发读取。
4. **全量单测** `PYTHONDONTWRITEBYTECODE=1 …/python3.11 -m unittest discover -q`：183 项通过，54 秒（这次只改前端，跑一遍是回归保险）。
5. **静态核对**：全仓库搜不到 `install-inspect`；`app.js` 和 `index.html` 里用到的 `install-url / install-message / install-retry / install-skills / install-agents / install-apply / install-close` 七个 id 两边一一对应。

没有对真实 home 跑过本工具，没有碰用户正在运行的看板进程，没有改任何 Python 文件，没有 `git add`/提交。

## 6. 没做或有疑问的地方

- **没有真实浏览器截图/点击**。交接里写明由 Claude 在模拟 home 上点一遍；我这里只有桩 DOM 脚本，它能验事件、请求和文案，验不了真实排版、颜色和焦点框的视觉效果。折行、`.row-wrap` 里“状态行 + 重试”两个元素的间距建议 Claude 顺手看一眼。
- **回车时浏览器的自带校验**：链接框还是 `type="url"`，里面不是合法网址时按回车会先弹浏览器自己的提示（中文“请输入网址”），我们的读取不执行；粘贴和停 0.6 秒这两条不受影响。这是改前就有的行为，我没有动它。
- **自动读取只认 `https://github.com/owner/repo…`**。`http://`、`github.com/owner/repo`（没有 https）这类手输内容不会自动读，按回车或点“重试”仍会读一次并把后台的中文原因显示出来——按交接里“内容像完整的 `https://github.com/...` 链接时自动读取”写的。
- **“读完焦点放到安装按钮”有一处例外**：全都已经装过时按钮是灰的，聚焦不了，这时焦点留在链接框（按回车也不会重复读）。要严格照字面做的话，可以把焦点改到“关闭”，我没加。
- **链接框为空时按回车什么都不做**（不改状态行、不提示），这是为了不破坏收编那条“打开看差别”的路，见 §4 第 7 条。如果更希望给一句“请先粘贴技能链接。”，得先区分这两种打开方式。
- **后台报错里还有“仓库”字样**（例如“请粘贴 GitHub 仓库链接或技能文件夹链接。”“这个仓库里没有找到指定的文件夹。”）。这些字符串来自 Python（`skill_pilot/sources/github.py`），前端照原样显示，按分工我没有改；要不要一起改成“链接”由主控定。
- **读取快慢不归前端**：本工作区里 `github.py` 还是整仓下载的旧版（Codex 那一半没在这里），所以现在实际点下来读取仍然慢；前端只做到“不用再点读取、同一个链接不重复读、读取中换链接不白等”。
- 桩 DOM 自检脚本在 `/tmp/sp-install-check/`，属临时目录，不会进仓库；要复跑就 `node /tmp/sp-install-check/check.cjs`（先把 `static/app.js` 复制成 `/tmp/sp-install-check/app.js`）。

## 7. 第二轮（Claude 浏览器验收后的两处修补）

Claude 在模拟 home 上用浏览器验收了第一轮：粘贴自动读取、默认全勾、默认勾当前 agent、只点一次安装、完成提示和撤销、已经装过、装完自动整理都对；提出两处要改。这一轮只改 `skill_pilot/web/static/app.js`，`index.html`、`app.css` 没动，没有 `git add`、没有提交。

1. **装完中文名迟迟不出现**。原来 `pollLabels()` 只在整轮全部结束时才 `refreshTable(true)`，用户真实环境有 131 个在补，新技能要等很久才看到中文名。现在：
   - 新增 `let labelsDone = 0`（上一次轮询看到的进度）；
   - `startLabels()` 真正开始新一轮时把它清 0（放在开头的 `if (labelsRunning) return;` 之后，所以整理进行中由别处再调 `startLabels` 不会白刷一次）；
   - `pollLabels()` 里 `progress.running` 为真时，`progress.done > labelsDone` 才 `await refreshTable(true)` 并更新 `labelsDone`——**只在 done 变了的那一次刷，done 没变不刷**；整轮结束时照旧 `refreshTable(true)`。
2. **读取失败后同一个链接不能再读**。`inspectInstall()` 的 `catch` 里原来没清 `installInspectedUrl`，失败后只能点“重试”。现在失败时把它清掉，再粘贴同一个链接（或停下 0.6 秒）会重新读。

验证：`node --check skill_pilot/web/static/app.js` 通过。桩 DOM 自检从 39 项加到 **45 项，45/45 通过**，新增的 6 项是：新一轮开始时进度清零；`done` 变大时只刷一次（表请求 6 → 7）；`done` 没变时不再刷；整轮结束时照旧刷；读取失败后能看到原因和“重试”；失败后再粘贴同一个链接确实又发了一次读取。全量单测 183 项通过（这次仍只改前端，跑一遍是回归保险）。这一轮结束时 `app.js` 2806 行（第一轮 2797），两个改动文件累计 +113 / −34 行；§1 的行数和自检项数是第一轮的记录。
