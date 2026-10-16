# 看板导航与设置页重排 · 第一轮样稿报告

> 2026-10-16，DSH，副本 `.worktrees/dsh-issue-35-nav-settings`（分支 `dsh/issue-35-nav-settings`）。
> 依据 `docs/handoff-nav-settings-frontend.md`（GitHub issue #35）。**第一轮只出样稿**：没有改 `skill_pilot/` 下的任何文件，没有改测试，没有 `git add`、没有提交。样稿数据全部是编的，没有读真实 home，没有开看板。

## 1. 交付物和怎么看

| 文件 | 说明 |
| --- | --- |
| `work/settings-prototype/a.html` | A 版「一次一块」：右边一次只显示目录里选中的那一块 |
| `work/settings-prototype/b.html` | B 版「整页连续」：所有块排下来，点目录滚过去，滚动时目录高亮跟着走 |
| `work/settings-prototype/prototype.css` | 两版共用：页头导航居中、设置页两栏、每块内容的面板。正式做进看板时并进 `app.css` |

`work/` 在忽略清单里，不会进仓库。两个 html 都直接用浏览器打开就能看（`file://` 即可，不用启动看板），样式用相对路径引用仓库里的 `skill_pilot/web/static/app.css`，图标定义整段拷自 `index.html` 的 `<svg class="icon-defs">`，所以看起来就是真看板。

截图和挑版可以按地址末尾直接打开某一块：`a.html#log`（A 选中操作记录）、`b.html#diagnostics`（B 滚到诊断与备份）。可用的名字：`ai`、`agents`、`share`、`log`、`diagnostics`、`version`。

样稿里的虚构数据：3 个正在管理的 agent（Codex 26 个技能 / Claude 17 / Gemini 11）、1 个发现的新 agent（Trae，12 个）、42 个技能（已整理 34 个，还差 8 个）、1 条其他技能文件夹记录、5 条操作记录（含 1 条已撤销）、2 组版本差异、安装日期 2026/10/15。

## 2. 两版共同的部分

**页头**：SP 标志在左；技能、清理、设置三项在整个窗口宽度的正中间，设置为当前页（`aria-current="page"`）；右边没有「可以管理技能」。只读预览的标记以后放导航右边那一列（`a.html` 里留了注释说明位置）。

**设置页左目录**（两项一组，共六项）：

| 组 | 目录项 | 样稿里的内容（照现在 `index.html` 的控件和文字做） |
| --- | --- | --- |
| 常用 | AI 整理 | 原「AI 服务」表单（服务商 / 服务地址 / 模型 / API key / 保存并试一下 / 删除 key / 只发名字和说明的安全说明）+ 原「技能的中文名、说明和标签」（已整理 34 个，还差 8 个 / 整理剩下的 8 个 / 重新整理全部）。用一条细分隔线分成上下两段 |
| 常用 | agent | 正在管理的 3 个 agent（颜色点、技能数、生效方式、和谁共用一个技能文件夹、不再管理…）+ 按文件夹接入（名字 / 技能文件夹 / 接入）+ 发现的新 agent（Trae，接入 / 忽略）+ **其他技能文件夹**（一条记录 + 去掉、输入框 + 记下，说明写「SP 也会到这些文件夹里找技能。不填就不额外找。」） |
| 常用 | 分享配置 | 原「分享技能配置」：两句说明 + 全部在用技能下拉 + 导出配置 / 导入配置 |
| 记录与维护 | 操作记录 | 原操作记录页：右上角刷新 + 5 条时间线（每条带撤销…，最后一条是已撤销的样式） |
| 记录与维护 | 诊断与备份 | 原诊断页四个折叠块：版本差异（2 组）、agent 与目录、其他发现、**整理与还原**（原「迁移与还原」改名），默认都收起，打开后有样例内容 |
| 记录与维护 | 版本 | 原「SP 自身更新」：安全说明一行、已安装日期、状态一句、检查更新按钮 |

每块标题下面都有一句大白话说明这块是干什么的（例如「排查问题、恢复备份用，平时不用管」）。

原来的 id 一个没改：`log-list`、`log-refresh`、`diag-*`、`inventory-*`、`root-list`、`agent-list`、`llm-*`、`labels-*`、`program-*`、`agents-*`、`extra-source-*`、`config-*` 都留着，做进看板时 `app.js` 不用改选择器。

## 3. A 版「一次一块」

- 结构：左目录 + 右边六块都在 DOM 里，除选中的那一块外都带 `hidden`。默认选中 AI 整理。
- 脚本（版本逻辑约 24 行，含注释）：点目录 → 只显示对应块、把 `aria-current` 挪过去、地址末尾换成 `#块名`；点完滚回页面顶部。不重画 DOM，所以焦点一直留在点过的那颗按钮上。
- 没开 JavaScript 时：只有 AI 整理是显示的（其余块在 HTML 里就是 `hidden`），页面不会空。
- 适合：一次专心看一块，长设置页不用滚。代价是看不到「下面还有什么」，要靠左目录认路。

## 4. B 版「整页连续」

- 结构：左目录 + 右边按顺序排下来的六块，两组之间是组标题「常用」「记录与维护」（第一组标题也放了，滚到中间时知道自己在哪一组）。
- 左目录 `position: sticky; top: 16px`，页面滚动时目录不动。
- 脚本（版本逻辑约 52 行，含注释）：点目录 → `scrollIntoView` 滚到那一块（系统开了「减少动态效果」就直接跳，否则平滑滚动）+ 立刻高亮；滚动时按「标题线以上最近的那一块」更新高亮，滚到页面最底部算最后一块；平滑滚动期间 600ms 内不跟着中间那几块跳，避免高亮闪。
- 适合：一页看全，用 Ctrl+F 也能找；代价是页面长（1280px 宽下约 3400px 高）。

**两版区别一句话**：A 是「目录切换内容」，B 是「目录跳转 + 滚动跟随」；两版的页头、左目录、每一块的内容和文字完全一样（除组标题和 `hidden` 外，两个文件正文逐行相同）。

## 5. 排版上的取舍

1. **页头居中**：`.topbar-row` 从 flex 改成 `grid-template-columns: minmax(0,1fr) auto minmax(0,1fr)`。`app.css` 里页头左右留白本来就是对称的（超过 1440px 时两边各留等宽的边），所以中间那一列正好是窗口正中间，不用绝对定位。只读预览标记放第三列，出现时导航也不会被挤歪。
2. **两栏**：左边目录 220px + 32px 间距 + 内容列。1280px 下内容列 980px，1024px 下 724px，表单仍沿用 `max-width: 560px`。
3. **每块是一张白面板**，和技能页左栏、折叠块用的是同一套（白底、1px 边、12px 圆角、轻阴影）。原来的 `.section-title` 是「整页一条分隔线 + 大上下边距」的写法，搬进两栏里会既占地方又跟面板打架，所以设置页不再用它，改成面板里的 `h2`。块内的小节仍用现成的 `.sub-title`。
4. **层级**：组标题 13px 灰 → 块标题 20px → 小节 14px → 正文/说明 13px。没有引入新字号、新颜色、新圆角，全部用 `app.css` 里的变量。
5. **间距收紧**：面板里的折叠块、工具栏、小节标题的外边距比整页排版小一档，免得一块内容被撑得太长。
6. **左目录复用** `.rail-list` + `.rail-item`（选中给一层浅底），`prototype.css` 只把 `display` 从 grid 改成 flex 好让两组分开；组标题用 `role="group"` + `aria-labelledby` 关联。
7. **分享配置**块里原有一句「导出分享给对方，或导入收到的清单，一次安装和分配」和块说明重复，样稿只留了块说明和安全说明那行；说明图标里的长解释照旧保留。这一处可以改回去，等挑版时定。
8. 折叠块、时间线、表格、按钮、开关都沿用现有样式，没有自创控件；样稿没有做手机版，窄于 1024px 只是不挤坏（目录折到内容上面）。

## 6. 我做的验证

- `node --check`：两个 html 里的脚本语法都通过。
- 用本机 Google Chrome 无头模式实际打开两个文件量了一遍（没有装任何依赖，也没有开看板）：

| 检查 | 结果 |
| --- | --- |
| 1024 / 1280 / 1536px 宽有没有横向滚动条 | 两版三档都 `scrollWidth == clientWidth`，没有 |
| 页头导航是不是窗口正中间 | 三档实测导航中心与窗口中心差 **0px** |
| 脚本报错 / 控制台异常 | 两版都是 0 |
| A：点每一项目录 | 每次都只显示对应那一块，`aria-current` 和地址同步（`#ai`…`#version`） |
| A：直接打开 `a.html#log` | 显示操作记录 |
| B：点「操作记录」 | 滚到距顶 16px（`scroll-margin-top` 生效）并高亮操作记录 |
| B：滚到页面最底部 | 高亮「版本」（不会停在「诊断与备份」） |
| B：回到顶部 | 高亮「AI 整理」 |
| 说明图标 ⓘ | 浮层能出现、完整落在窗口内，滚动时自动收起 |

## 7. 做进看板时要改的地方（第二轮）

行号按本轮 main 的版本，第二轮可能漂移，函数名和 id 不变。

### 7.1 `index.html`

- 页头：五个导航项删成三个（技能 / 清理 / 设置），`nav-quiet` 的两个去掉；`#mode-badge` 保留，挪到 `</nav>` 之后（第三列），只读预览时才显示。
- 删掉 `<section id="page-log">`（222–232 行）和 `<section id="page-diagnostics">`（234–329 行），内容整段搬进 `#page-settings`（330–414 行）。
- `#page-settings` 换成样稿的结构：`.settings-split` → `nav.settings-nav`（六项、两组）+ `.settings-main`（六块 `.set-block`，A 版除第一块外加 `hidden`，B 版再加两个组标题）。
- 「迁移与还原」的 `<summary>` 改成「整理与还原」；「额外技能源」改名「其他技能文件夹」、说明改成指定那句、整块挪到 agent 一块最下面（不再是独立块）。
- 原「AI 服务」和「技能的中文名、说明和标签」两块合成一块「AI 整理」，之间用 `.set-divider`。
- 所有现有 id 原样保留，`app.js` 里的选择器不用改。

### 7.2 `app.js`

- `showPage()`（574 行）：页面只剩 `skills` / `cleanup` / `settings`，删掉 `if (page === 'log')`（585）和 `if (page === 'diagnostics')`（586）两个分支。
- 新增选中状态和函数，例如 `state.settingsBlock` + `showSettingsBlock(name)`：A 版切 `hidden` 和高亮，B 版 `scrollIntoView` + 滚动监听（脚本照 `a.html` / `b.html` 里那段搬）。地址建议用 `#settings/<块名>`。
- 走进设置的入口：
  - `$('labels-settings')`（1699 行）现在 `showPage('settings')` → 再加上选中「AI 整理」。
  - `$('agents-migration-link')`（1875 行）现在 `showPage('diagnostics')` → 改成设置页 +「诊断与备份」，最好顺手展开「整理与还原」。
  - `renderRailNotes()`（708 行）里「去接入…」的 `data-goto="settings"` → 设置页 +「agent」；`#page-skills` 的 `[data-goto]` 处理（1217–1220 行）可以支持 `settings/agents` 这种带块名的写法。
- 渲染守卫要跟着改：
  - `renderLog()`（2945 行）现在 `if (state.page !== 'log') return;`，`renderDiagnostics()`（2989 行）现在 `if (state.page !== 'diagnostics' || !state.inventory) return;` —— 都改成按「设置页 + 当前块」判断。
  - `renderTable()` 里（326 行）已经在调 `renderLog()`，`refreshEverything()`（1817 行）也在刷新日志和诊断；进块时要补一次（`log` 块调 `refreshLog()`，`diagnostics` 块调 `renderDiagnostics()` / `refreshInventory()`），否则先开设置页再切块可能看到旧内容。
  - `showPage('settings')` 现在固定调 `renderConfigExport / renderAgents / refreshSettings / refreshProgramUpdate`（587 行），第二轮按块懒加载更省，也避免每次切块都请求一遍。
- 地址恢复：`start()`（3296、3324 行）读 `location.hash`，旧的 `#log`、`#diagnostics` 要映射到 `settings/log`、`settings/diagnostics`，别让用户收藏的地址失效。`rememberPlace()`（569 行）跟着写块名。
- `#mode-badge`（1309 行）：现在是 `可以管理技能` / `只读预览` 二选一，改成「可以管理技能」时隐藏、只读时显示「只读预览」。
- 不用改：导航点击（593 行）、`renderActionBar()`（598 行，设置页不显示底部托盘）、`fitFilterRail()`（技能页专用）、各个 `diag-*` / `log-*` 的监听。

### 7.3 `app.css`

- 把 `prototype.css` 的内容并进来：页头 grid 居中、`.settings-split` / `.settings-nav` / `.set-nav-*`、`.settings-main` / `.set-block` / `.set-head` / `.set-lead` / `.set-divider`、组标题、1023px 以下的目录折行。
- 可以顺手删掉：`.section-title` 两条规则（仓库里只有设置页用过它）、`.nav-quiet`（只剩三个导航项，不再有安静项）。
- 前端改动后按 `AGENTS.md` 跑全量测试和 `node --check skill_pilot/web/static/app.js`。

## 8. 请挑版时一起定的事

1. 选 A 还是 B（B 版页面约 3400px 高，A 版一次只看一块）。
2. B 版第一组标题「常用」留不留（样稿两处组标题都放了）。
3. 「分享配置」块里那句重复的说明删掉是否可以。
4. B 版用键盘点目录后，焦点仍留在目录按钮上（样稿就是这样）；要不要让焦点跟到内容标题上。
5. 只读预览标记沿用现在的 `#mode-badge` 样式，放导航右边。

## 9. 边界自查

- `skill_pilot/` 下没有任何改动，测试文件没有改动，没有 `git add`、没有提交（`git status` 只有本报告和交接文件是未跟踪的新文件）。
- 样稿没有读真实 home、没有运行任何对真实技能库的命令、没有碰正在运行的看板；验证用的浏览器是无头模式另开的窗口，与用户的看板无关。
- 没有引入框架、构建工具或新依赖，脚本都是原生 JS：说明图标浮层那段和 `app.js` 的写法一样，两个文件各约 55 行；A 版切换约 24 行，B 版滚动定位约 52 行（都含注释）。

---

# 第二轮：把 B 版做进看板

> 2026-10-16，DSH，同一个副本（分支 `dsh/issue-35-nav-settings`）。用户选定 B 版「整页连续」；交接的「第二轮」一节和上面第 7 节是这次的落地清单。
> 只改了 `skill_pilot/web/static/` 下的三个前端文件，加上 `README.md`、`DESIGN.md`；**Python、后台接口和测试文件一个没动**，没有 `git add`、没有提交。

## 1. 改了哪些区块和函数

| 文件 | 改动 |
| --- | --- |
| `skill_pilot/web/static/index.html` | 页头五项收成三项、`#mode-badge` 改成只在只读预览时显示；删掉 `#page-log`、`#page-diagnostics` 两个页面，内容搬进设置页；技能页加「首次整理」块（`#first-run`）；设置页按 B 版重排；删掉没人再用的 `sp-diagnostics` 图标定义 |
| `skill_pilot/web/static/app.js` | 页面只剩技能 / 清理 / 设置；新增设置页目录、滚动定位与高亮；操作记录、诊断的渲染守卫改到设置页；首次整理的显示与三个步骤；改名和文案；旧地址映射；完成提示带「撤销」 |
| `skill_pilot/web/static/app.css` | 页头改栅格让导航居中；`prototype.css` 并进来（设置两栏、目录、面板、组标题、首次整理面板）；诊断表格在窄栏里的列宽；删掉没人用的 `.section-title`、`.nav-quiet` |
| `README.md` | 「看板里的主要操作」表按新结构重写；「设置 → SP 自身更新」→「设置 → 版本」；「额外技能源记在…」→「其他技能文件夹记在…」 |
| `DESIGN.md` | 页头（三项居中、只读标记）、设置页两栏与滚动高亮、首次整理、导航图标那三段 |

`app.js` 动到的函数：`showPage`、`rememberPlace`（地址记 `settings/<块名>`）、`renderLog`、`renderDiagnostics` 的守卫，`renderRailNotes`、`labels-settings`、`agents-migration-link` 三个入口，`migrationError` / `migrationStep` / `renderMigrationStatus` / `migrationChoices` / `renderMigrationReview` / `finishMigration` / `restoreBackup` / `refreshMigrationStatus`、`start`。新增：`gotoPlace`、`SETTINGS_BLOCKS`、`highlightSettingsBlock`、`currentSettingsBlock`、`trackSettingsBlock`、`showSettingsBlock`、`renderSettings`、`renderFirstRun`、`placeFromHash`。

## 2. 交接第 1～7 条逐条

1. **页头和导航**：`index.html` 里只剩技能、清理、设置三项；`app.css` 的 `.topbar-row` 从 flex 改成 `grid-template-columns: minmax(0,1fr) auto minmax(0,1fr)`——页头左右留白本来就对称，所以中间那一列正好是窗口正中间。`#mode-badge` 只在只读预览时显示「只读预览」，能改的时候不显示（`refreshTable` 里改成 `hidden = table.write_allowed`），它在第三列，出现时导航不会被挤歪。
2. **设置页按 B 版**：`#page-settings` 改成「左边 `nav#settings-nav`（两组六项）+ 右边 `.settings-main`（六块 + 两个组标题）」；`renderSettings()` 一次把六块都刷成最新的（AI 表单、agent、分享配置、操作记录、诊断、版本），诊断这类只读数据的折叠块仍可以展开再读（`已有备份` 一直是点按钮才读）。点目录 `scrollIntoView` 滚过去，滚动时按「标题线以上最近的一块」更新高亮，滚到页面底部算最后一块；点目录后等滚动停稳（200ms 没有新的滚动事件）再定一次高亮，中途不跟着中间几块跳。地址记 `#settings/<块名>`，刷新回到那一块；旧地址 `#log`、`#diagnostics` 在 `placeFromHash()` 里映射成 `settings/log`、`settings/diagnostics`（打开后地址栏也换成新写法）。
3. **原来跳到这两页的入口**：「去接入…」改成 `data-goto="settings/agents"`，`gotoPlace()` 认「页面/块名」两段写法；技能页的「去设置」去 `settings/ai`；设置页 agent 一栏里「还没完成首次迁移 / 去迁移与还原」改成「还没有接手管理任何 agent / 去技能页做首次整理」，点了回技能页。
4. **改名**：「额外技能源」→「其他技能文件夹」，说明改成「SP 也会到这些文件夹里找技能。不填就不额外找。」，整块挪到 agent 一块最下面；「SP 自身更新」块叫「版本」；「迁移与还原」叫「整理与还原」，这一流程里的按钮和提示统一成整理（开始整理 / 确认整理 / 整理好了 / 已还原到整理前 / 正在整理…），页面里的旧说法都换了；命令行的 `extra-sources` 没动。
5. **首次整理引导**：见下一节。
6. **说明文档**：`README.md` 的表里，操作记录、诊断与备份、版本、其他技能文件夹都按新位置写；「更新程序」「怎么用」「数据放在哪」三处的旧名字改掉。`DESIGN.md` 补了页头三项居中、设置页两栏和滚动高亮、首次整理，图标那段说明导航只剩三个（记录和诊断的图标不再用，`sp-diagnostics` 也从 `index.html` 删了）。
7. **样式合并**：`prototype.css` 的内容原样并进 `app.css`（少了两版差异的部分）；`.section-title`、`.nav-quiet` 已经没人引用，删掉；样稿文件仍留在 `work/settings-prototype/`，没有进 `skill_pilot/`。

## 3. 首次整理引导放在哪

- 先在模拟环境里看了「还没整理过」原来长什么样：左栏是「正在读取各 agent…」加五张「发现新 agent：去接入…」，右边一直停在「正在读取技能列表…」，还有一条红色提示条说技能清单没读出来——因为这时还没有技能表。
- 所以引导放在**技能页主区域、技能列表的位置**：`#first-run` 是 `#page-skills` 里的第一块，显示它的时候把整个 `#skills-split`（左栏 + 列表）收起来，页面就只剩「首次整理」这一张面板。判断条件沿用原来的：`/api/agents` 说一个 agent 都没在管理（`state.agentsView.managed` 为空）。
- 里面的三步是原来「迁移与还原」那套元素和接口，整段搬过来用：`#migration-select`（选 agent → 按钮「开始整理」）、`#migration-review`（核对保留哪份 → 「返回选择 agent」/「确认整理」）、`#migration-complete`（写着「整理好了」和一句「要还原到整理前，去设置 → 诊断与备份 → 整理与还原」）。整理完成后引导自动收起，技能页照常列技能；完成提示条带「撤销」，撤销走的就是同一份备份的还原。
- 「还原到整理前」和「已有备份」留在设置 → 诊断与备份 → 整理与还原里；备份列表、还原按钮、失败提示都在那边（出错提示按当前页写两个框：引导里的和设置里的）。
- 顺带修了一个原来就有的毛病：`migrationChoices()` 原来把「发现的新 agent」（比如 Trae）也列成勾选项，后台 `store.preview()` 只认有盘点档案的 agent，会整批拒绝（「请至少选择一个有接入档案的 agent」）。这轮在前端只列有档案的 agent（用 `/api/scan` 里的 `status: profile_available` 判断），**没有改 Python**；这类新 agent 仍是整理完以后在设置 → agent 里按文件夹接入。

## 4. 自查了什么

- `node --check skill_pilot/web/static/app.js` 通过；`index.html` 用解析器查过标签闭合、没有重复 id、`app.js` 里写死的 id 都在页面里。
- 单测（Python 文件没动，按交接跑相关的）：`tests.test_web`、`tests.test_extra_sources`、`tests.test_m5_web`、`tests.test_generic_agents_web`、`tests.test_sharing_web`，38 项全过。全量测试留给 Claude。
- 无头浏览器（本机 Chrome，另开的窗口）在系统临时目录里**两个模拟 home** 上实测，路径含空格：
  - **从没用过 SP 的**（只有 agent 的技能文件夹，没有 `.skill-pilot`）：技能页显示首次整理、技能列表收起；可选 agent 只有 4 个有档案的（Trae 不列）；点「开始整理」→ 核对页（共 8 个技能、0 组要核对）→ 确认框（开始整理）→ 真的走完整理；完成后引导收起、技能列表出现、提示条「整理好了」带「撤销」；随后设置页 6 块都有内容、agent 一栏 4 个、整理与还原显示「已经整理好了」和还原按钮。
  - **已经整理过的**（`scripts/make-fake-home.py` 建的完整模拟 home）：技能页 5 个 agent、87 行技能；设置页六块、两个组标题、六项目录；操作记录 21 条；诊断 227 个目录入口、15 个目录、5 个 agent 折叠块；其他技能文件夹能记下一条并「去掉」；分享配置下拉列出 5 个 agent。
  - 目录与地址：点「操作记录」滚到距顶 16px 并高亮、地址变 `#settings/log`；直接打开 `#settings/log` 落在同一块；旧地址 `#log` 打开后变成 `#settings/log` 并落在操作记录；滚到页面底部高亮「版本」；手动滚动时高亮跟着走。点目录平滑滚动的全过程每 150ms 采样 14 次，高亮始终停在目标那一块，不闪。
  - 1024 / 1280 / 1536px 宽：导航中心与窗口中心差 0px、页面没有横向滚动条；只读模式下右上角显示「只读预览」，AI 表单和其他技能文件夹的输入禁用。
  - 全程没有脚本报错或控制台异常。
- 顺手按实测调了一处排版：诊断里的技术表格走进 220px 目录旁边的窄栏以后，「文件状态」会被挤成竖排。给后几列按自己的宽度排、前两列按比例分（45% / 35%，最小 240 / 200px），放不下就在面板里横向滚动；1280px 下四列都能看见，1024px 下面板内可以横向滚动。

## 5. 留给下一步

- 全量测试和界面逐项验收由 Claude 做（这轮只跑了相关的 38 项）。
- `docs/STATUS.md` 里还有两处按旧界面写的当前状态文字（「设置 → SP 自身更新」和「额外技能源」），交接只列了 README 和 DESIGN，这两处没动，合并前顺手改一下。
- 后台接口返回的文字按要求没改：`/api/migration/apply` 的说明句仍是「迁移完成，各 agent 的技能名单已核对一致」，它会出现在引导完成步骤的正文里（标题是前端写的「整理好了」，引导随即收起）。要连这一句也统一成整理，得改后台。

---

# 第三轮：Claude 验收后的 7 处修改

> 2026-10-16，DSH，同一个副本（分支 `dsh/issue-35-nav-settings`）。依据交接「第三轮」一节。
> 还是只改前端和文档：`index.html`、`app.js`、`app.css`、`docs/STATUS.md`；**Python、后台接口和测试文件没动**，没有 `git add`、没有提交。

## 1. 还原后报错（第 1 条，最重要）

- 原因：`finishMigration()` 原来调 `refreshEverything()`，它无条件读技能表；还原以后登记表已经没有了，`GET /api/table` 返回 409「请先完成首次迁移，再管理技能分配。」，异常从 `runWrite` 冒出来变成红色提示条，`migrationError` 又在「整理与还原」里写了一句黄色提示，后面的刷新都没跑，所以还停在「已经整理好了」。同样的问题也会出现在「已有备份」里的还原和提示条上的「撤销」（三条路都走 `restoreBackup` → `finishMigration`）。
- 改法：`finishMigration()` 不再调 `refreshEverything()`，改成和 `start()` 一样的顺序——先 `refreshInventory()`、`refreshAgents()`，再用 `renderMigrationStatus()` 判断有没有在管理的 agent：
  - 已经整理好：读技能表（失败时按原来的话给一条提示条）并刷新已撤下名单；
  - 还没整理（就是刚还原）：把 `state.table` 清空，不读技能表、也不读已撤下名单（这两个接口在没有登记表时都会报同一句错），随后 `refreshLog()`、`refreshMigrationStatus()` 照常跑。
- 实测三条还原入口都走了一遍（临时目录里的模拟 home）：整理完 → 还原后**只有**「已经还原到整理前的样子」这一条提示，两个错误框都是空的，不刷新页面技能页就回到「首次整理」引导；再走一次首次整理同样成功（技能数从 6 回到 5，是因为还原后 `skill-pilot` 那个技能也跟着退回整理前的样子，属正常）。

## 2. 引导里重复的一句（第 2 条）

- 引导开头已经写了「整理前会先备份，完成后能还原」，`#migration-mode` 原来又写一遍。现在新增 `renderMigrationMode(writeAllowed)`：能写入时把这一行整个隐藏，只在只读时显示「只读模式下可以预览，不能整理。」；`refreshMigrationStatus()` 和 `renderMigrationReview()` 都走它。
- 只读模式实测：引导里只有那句只读说明；可写时这一行不占位置。

## 3. 第 1 步按钮名字（第 3 条）

- `#migration-preview` 的文案从「开始整理」改成「下一步：核对」；第 2 步的「确认整理」和确认框里的「开始整理」没动。

## 4. 分享配置块重复的说明（第 4 条）

- 删掉「导出分享给对方，或导入收到的清单，一次安装和分配。」这一行；挂在这句后面的 ⓘ 长说明（只包含什么、哪些会标明待补齐、对方同名技能会保留）挪到下面那行安全说明「不含技能正文、账号密钥和本机目录；文件内附使用说明。」后面，内容一字没少。块说明仍是「把技能清单和分配关系交给别人，或者收下别人给的清单。」

## 5. 引导卡片居中（第 5 条）

- `.first-run` 的 `max-width` 从 900px 收到 **760px**，并加 `margin: 0 auto`，在内容区里水平居中。
- 实测 1280px 下卡片左右留白相同（各 260px），只读和可写两种状态都居中。

## 6. 1024px 下诊断工具条被截（第 6 条）

- 原因：`.toolbar` 里搜索框是 `flex: 1`，会把「同时看内置和插件」一路顶到行的最右边（1024px 下正好压线），目录下拉被迫折行，文字看起来像被截掉。
- 改法（只作用于「agent 与目录」这一块）：
  ```css
  #diag-inventory-box .toolbar { row-gap: var(--sp-2); }
  #diag-inventory-box .toolbar .input-search { flex: 0 1 320px; }
  #diag-inventory-box .toolbar .check, #diag-inventory-box .toolbar select { flex: none; max-width: 100%; }
  ```
- 实测 1024px：第一行是搜索框 + 「同时看内置和插件」，目录下拉整件折到第二行，文字完整；1280px 三件仍在一行，搜索框不再一路撑满。

## 7. `docs/STATUS.md`（第 7 条）

- 只改描述当前界面的那一处：开头「当前生效方式」里的「设置 → SP 自身更新」改成「设置 → 版本」。
- 带日期的历史记录（2026-10-15 的「SP 自身更新」条目、2026-10-16 的两条「额外技能源」）按交接要求原样保留。

## 8. 自查

- `node --check skill_pilot/web/static/app.js` 通过；`index.html` 查过标签闭合、没有重复 id。
- 相关单测 38 项（`tests.test_web`、`tests.test_extra_sources`、`tests.test_m5_web`、`tests.test_generic_agents_web`、`tests.test_sharing_web`）全过。
- 无头浏览器在系统临时目录的模拟 home（路径含空格）上：
  - 走完「首次整理 → 还原到整理前 → 再整理一次」整圈，三条还原入口（整理与还原里的按钮、提示条上的「撤销」、「已有备份」列表）各自试过：还原后没有报错、技能页回到引导、再整理成功；
  - 只读模式下引导只显示只读那一行，卡片居中；
  - 设置页深链接 `#settings/share`、`#settings/diagnostics` 落在距顶 16px、`#settings/version` 落在页面底部，高亮都对，滚完不再漂移；
  - 1024 / 1280px 下诊断工具条与表格都排得下，页面没有横向滚动条；
  - 全程没有脚本报错或控制台异常。
- 全量测试和界面复验仍由 Claude 做。


