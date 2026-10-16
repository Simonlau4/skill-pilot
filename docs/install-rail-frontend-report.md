# 交接报告：安装技能改到技能页左栏（前端）

2026-10-16，DSH。对应 GitHub issue #36，按 `docs/handoff-install-rail-frontend.md` 实施。本文件是交接用的开发记录，按 `AGENTS.md` 规定合并前删掉。

## 改了哪些文件

只动了 `skill_pilot/web/static/` 下的三个前端文件，另按交接文件的允许范围更新了 `DESIGN.md` 的两处规则和新图标一行。

| 文件 | 改动 |
| --- | --- |
| `static/index.html` | 左栏 `#agent-rail` 和 `#rail-notes` 之间加安装区（粘贴框 `#install-url` + 概要卡片 `#install-card`）；删掉工具栏的 `#skills-install` 按钮；安装窗口去掉链接输入框和"重试"，只留结果、agent 勾选、同名对比和"关闭 / 安装"；`<defs>` 里加 `sp-close` 图标 |
| `static/app.js` | 重写"安装技能"一段：状态变量、概要计算、左栏卡片渲染、读取、直接安装、事件接线；`WRITE_LOCK_SELECTORS`、`renderBusyAreas`、`pickAgent`、`refreshTable` 各改一行 |
| `static/app.css` | 新增 `.install-rail` / `.install-box` / `.install-actions(-row)`；窄到 1024px 时安装区限宽 340px，不占满整行 |
| `DESIGN.md` | 布局一节写清左栏安装的样子；工具栏一行删掉"安装只写安装"；图标表加 `sp-close` |

`README.md` 没改：交接文件说"技能"那一行提到安装才更新，现在那一行只写加/撤技能、依赖和核对，没有安装字样，改了反而多话。后台（Python、接口）一行没动。

### app.js 里的函数变化

- 新增：`installReadyNames(review)`（能直接装的技能名）、`installSummary(review)`（概要标题和几行说明）、`renderInstallRail()`（按状态画卡片）、`clearInstallRail(clearUrl)`（收卡片）、`installAllForCurrent(button)`（全部装给当前 agent）。
- 改写：`openInstall(review = null)`（原来的无参版本）、`inspectInstall(force)`（结果画到卡片，不再画到窗口）、`renderInstallApply()`（补上"只读时不能点安装"）。
- 状态变量：原来的 `installReview` 拆成 `railReview`（左栏卡片记的结果）和 `dialogReview`（安装窗口这次显示的结果）。两者分开后，收编同名对比时清空窗口结果不会影响左栏卡片上的"全部装给"。原 `installInspectSequence`、`installInspectedUrl`、`installReadingUrl`、`installInspectTimer`、`INSTALL_URL_PATTERN` 沿用。
- 删除：`#skills-install` 的点击、`#install-retry` 的点击、安装窗口表单的 submit（窗口里没有输入框了，回车改由左栏的框处理）。

## 交接文件 1～7 逐条

1. **左栏粘贴框**：`<div id="install-rail">` 放在 `#agent-rail` 下面、`#rail-notes` 上面；一个一行高的输入框，占位文字"粘贴 GitHub 技能链接"，旁边一个 ⓘ 说明"只读取文件，不运行里面的程序。读完在下面显示概要，读取时不挡着页面"。工具栏的安装按钮已删，`WRITE_LOCK_SELECTORS` 里对应的选择器也换成了新元素。
2. **自动读取**：沿用原规则——粘贴立刻读、按回车立刻读、输入停下约 0.6 秒且像完整 GitHub 链接时读。读取时框下面出一张卡片写"正在读取…／页面照常能用，读完这里显示概要。"，页面不弹窗；实测读取进行中切换 agent、搜索过滤、开关技能都能正常做（见自查）。
3. **概要卡片**：和提醒卡片同一套 `.rail-note` 样式。
   - 概要一句：`读到 N 个技能` + 分条说明`X 个可以直接装`、`Y 个已经装过`、`Z 个同名，要先对比`（为 0 的不写）；链接里没有技能时写"这个链接里没找到技能"。
   - 主按钮"全部装给 <当前在看的 agent>"：只装不在同名名单里的技能（已装过和要先对比的都不含）；点了直接 `runWrite` → `/api/install/apply`，不弹确认框，完成提示沿用"装好了 X，给了 Y"和撤销。按钮名字在切换 agent 时跟着变（`pickAgent` 里重画）。一个能直接装的都没有时不显示这个按钮。
   - 次按钮"挑一挑"：`openInstall(railReview)` 打开原窗口，直接把这次读到的结果画进去，不再读一次。没有可勾的技能（全都是已装过、或链接里没技能）时不显示。
   - 右上角关闭（×，`sp-close` 图标，带 `aria-label` 和 `title`）：只收起卡片，粘贴框里的链接留着，再按回车可以重读；装成功才连粘贴框一起清空。
   - 读取失败：卡片写"没读成"加后台给的原因，带"重试"（用当前链接再读一次）和关闭。
4. **一次只处理一个链接**：继续用 `installInspectSequence` 作废旧读取；实测先读一个大仓库、读取途中换成另一个链接，最后卡片显示的是新链接的结果，旧结果没有回来覆盖。没有做排队。
5. **安装窗口保留**：`<dialog id="install-dialog">` 还在，只作"挑一挑"和收编同名对比的详细视图；里面的链接输入框和"重试"删掉了（重试在左栏卡片上），不会出现两个框各读各的。收编遇到同名时 `openInstall()` 再 `showIncomingConflicts(result, null)` 的用法原样保留。
6. **只读预览**：`refreshTable()` 改成调 `renderInstallRail()`，里面按 `state.table.write_allowed` 把粘贴框禁用（原来禁用的是 `#skills-install`），卡片上的按钮同样禁用或不显示。未首次整理时 `#skills-split` 整个隐藏，左栏不出现，未做额外处理。
7. **用词**：界面文案都是中文大白话，说 agent，不出现路径、仓库、哈希这类机制词（卡片只写"链接"是指用户自己粘贴的那个网址）；安全说明"只读取文件，不运行里面的程序"放在粘贴框旁的 ⓘ 里，安装窗口里也还留着这一行。

顺带的小决定：`renderInstallApply()` 多加了"当前能改、也没在写入"两条——以前只读时窗口打不开，现在卡片也能进窗口，补上更稳妥。

## 自查了什么

- `node --check skill_pilot/web/static/app.js` 通过。
- 页面相关测试：`tests.test_web`、`tests.test_nav_static`、`tests.test_cleanup_before_organize`、`tests.test_m5_web`、`tests.test_sharing_web`、`tests.test_generic_agents_web`，共 35 项全过（全量测试留给 Claude）。
- 无头浏览器（Playwright CLI）跑在系统临时目录里的模拟 home，路径带空格，模拟 home 是"已经整理过"的：
  - 读取不挡页面：读一个上千个技能的大仓库（读取持续几十秒），期间切到另一个 agent、在搜索框输入并筛出结果，都正常；卡片停在"正在读取…"。
  - 读到结果：概要、主按钮、挑一挑、关闭都对；主按钮名字随 agent 从"全部装给 Claude"变成"全部装给 Codex"。
  - "挑一挑"不重新读：打开前后 `/api/install/inspect` 请求数没变，窗口里直接列出这次读到的技能，默认勾当前 agent。
  - 勾一半、换 agent 再装：只装了勾的那个技能，给的是勾的 agent，提示带撤销；点撤销后技能从库里移除。
  - "全部装给 X"：只装能直接装的，已装过和同名要对比的都没动；装完卡片收起、粘贴框清空、提示带撤销。两个不同 agent 各组了一次（一个走窗口、一个走主按钮）。
  - 全部已装过：卡片写"2 个已经装过"，只留关闭；链接里没有技能：写"这个链接里没找到技能"，只留关闭。
  - 读取失败与重试：写"没读成"加原因，点"重试"会重新读并再次给出结果。
  - 读取中换链接：新结果覆盖旧结果，没有出现旧链接的概要。
  - 同名要对比：卡片写"1 个可以直接装，1 个同名，要先对比"；主按钮只装了新的那个，库里同名技能的内容一个字节没变；在"挑一挑"里勾上同名技能点安装，窗口给出两份内容的差别，没有覆盖。
  - 一个真实 GitHub 链接（小仓库）：输入停下后自动读取，读完给出概要，控制台没有报错。
  - 只读预览：另起一个只读看板，右上角显示"只读预览"，粘贴框 `disabled`。
  - 宽度 1024 / 1280 / 1536：粘贴框和卡片都放得下，卡片能换行，技能列表没有被挤窄。
  - 浏览器控制台：正常流程 0 条消息；只有故意触发的读取失败会留下一条网络层 409（可预期的失败请求，不是脚本报错）。
- 没对真实技能库做任何写入：所有安装、撤销都在模拟 home 里；没有运行 `scripts/build-app.sh`，没有 `git add`、没有提交。测试用的模拟 home 和临时仓库已删掉。

## 遗留说明

- 交接文件 `docs/handoff-install-rail-frontend.md` 按规则合并前删掉。
- 没有发现必须改后台的地方。
- 界面最终由 Claude 在浏览器里逐项验收（issue #36 的验收标准）。
