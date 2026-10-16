---
name: Skill Pilot
description: 本机看板：一份技能库，分给多个 agent
colors:
  shell: "#18202E"
  shell-raised: "#262F42"
  shell-ink: "#F2F4F8"
  shell-ink-muted: "#AEB7C5"
  ground: "#EDF0F4"
  surface: "#FFFFFF"
  surface-soft: "#F2F4F8"
  surface-deep: "#E6EAF0"
  line: "#DCE1E8"
  line-strong: "#AEB7C5"
  ink: "#18202E"
  ink-secondary: "#465163"
  ink-muted: "#5F6A7B"
  pending: "#E3A008"
  pending-bg: "#FFF5D6"
  pending-ink: "#6A4A00"
  danger-ink: "#A8251B"
  danger-bg: "#FDECEA"
  agent-1: "#2F5BD3"
  agent-2: "#C4552B"
  agent-3: "#6D4BC2"
  agent-4: "#0F7C6E"
  agent-5: "#93650A"
  agent-6: "#B0346B"
  agent-7: "#3D7A1F"
  agent-8: "#5B6B85"
typography:
  brand:
    fontFamily: "Avenir Next, Avenir, PingFang SC, -apple-system, Microsoft YaHei, sans-serif"
    fontSize: "20px"
    fontWeight: 800
    lineHeight: 1.3
  title:
    fontFamily: "Avenir Next, Avenir, PingFang SC, -apple-system, Microsoft YaHei, sans-serif"
    fontSize: "26px"
    fontWeight: 700
    lineHeight: 1.3
  section:
    fontFamily: "Avenir Next, Avenir, PingFang SC, -apple-system, Microsoft YaHei, sans-serif"
    fontSize: "20px"
    fontWeight: 700
    lineHeight: 1.3
  body:
    fontFamily: "-apple-system, BlinkMacSystemFont, PingFang SC, Helvetica Neue, Microsoft YaHei, sans-serif"
    fontSize: "14px"
    fontWeight: 400
    lineHeight: 1.65
  note:
    fontFamily: "PingFang SC, -apple-system, sans-serif"
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.5
  meta:
    fontFamily: "PingFang SC, -apple-system, sans-serif"
    fontSize: "12px"
    fontWeight: 400
    lineHeight: 1.5
  code:
    fontFamily: "SF Mono, ui-monospace, Menlo, monospace"
    fontSize: "12px"
rounded:
  sm: "8px"
  md: "12px"
  tray: "12px"
  pill: "999px"
spacing:
  xs: "4px"
  sm: "8px"
  md: "12px"
  lg: "16px"
  xl: "24px"
  xxl: "32px"
components:
  button:
    backgroundColor: "{colors.surface}"
    textColor: "{colors.ink}"
    rounded: "{rounded.sm}"
    height: "34px"
  button-primary:
    backgroundColor: "{colors.shell}"
    textColor: "{colors.shell-ink}"
    rounded: "{rounded.sm}"
  switch-on:
    backgroundColor: "{colors.agent-1}"
    rounded: "{rounded.pill}"
    size: "40px x 24px"
  pending-ring:
    backgroundColor: "{colors.pending}"
  tray:
    backgroundColor: "{colors.surface}"
    rounded: "{rounded.tray}"
---

# Skill Pilot 的界面规则

样式都在 `skill_pilot/web/static/app.css` 的 `:root` 里，这里说清怎么用。产品本身的情况见 [PRODUCT.md](PRODUCT.md)。

## Overview

这是一个“干活”的界面：打开就要知道有什么要处理，改完马上生效、随时能撤销。外观只负责四件事：字、配色、疏密，以及一个特色元素，也就是每个 agent 的“占用 7.2k Token”的占用条。导航、按钮、开关、表格都用大家熟悉的样子，不自创控件。

界面靠“图标加几个字”带路，不靠大段说明：一眼能懂的事用图标和两三个字，要多解释的放进图标旁的提示（见“图标”一节）。

页面的使用场景：白天在 Mac 上，开完几轮 agent 对话后花几分钟集中整理技能，所以只做浅色。SP 管理电脑上的本地技能，按桌面窗口设计和验收。

## Colors

- brandkit 的五种品牌色：深墨色 `#18202E`、抬起一层的深墨色 `#262F42`、冷白色 `#F2F4F8`、浅冷灰 `#EDF0F4` 和钢灰 `#AEB7C5`。页头、主按钮和提示条用深墨色，主按钮悬停用第二种深墨色；页面底色用浅冷灰，分组与控件悬停用冷白色，控件边框用钢灰，内容仍放在白色面板上。
- 文字三档：`ink` 正文，`ink-secondary` 说明，`ink-muted` 次要信息；`ink-muted` 只用在 12px 以上，且背景是白色或 `ground`。
- **每个 agent 一种颜色**，按 `/api/table` 给的顺序从 `agent-1` 到 `agent-8` 依次取（`agentColor()`）。页面里凡是代表某个 agent 的地方（色块、占用条、开关打开时）都用它的颜色，别处不用这些颜色做装饰。
- **黄色只表示“改了还没生效”**：开关外圈、托盘、行底色。撤下用 `danger-ink` 字色提醒，不用大片红色。
- 颜色从不单独表达意思：开关有开/关位置，待应用有文字说明。

## Typography

品牌字标和标题用这台 Mac 已有的 Avenir Next，中文标题回退到苹方；正文用系统界面字体，中文仍是苹方。所有字体来自本机。品牌字标 20px / 800，标题 26px / 700，分节 20px / 700；正文 14px，说明 13px，最小 12px，正文行高 1.65。层级靠字号和粗细，不靠颜色。数字一律等宽数字（`tabular-nums`，已在 body 上开启）。技能英文原名用等宽小字跟在中文名后面。

## Layout

- 内容最宽 1440px，左右留 24px（窄屏 16px）。
- 技能页：左栏 220–300px 放 agent 列表、安装粘贴框和提醒，右边放所选 agent 的标题与工具栏；宽度小于 1024px 时左栏变成顶部横排。
- 技能页左栏的安装：一行高的“粘贴 GitHub 技能链接”框，读完在框下面出一张和提醒同款的卡片写概要（读到几个、几个能直接装、几个已经装过、几个要先对比），按钮是“全部装给 <当前在看的 agent>”和“挑一挑”，另有“正在读取…”和读失败带“重试”两种状态；卡片右上角一个关闭。读取时页面照常能用，不弹窗挡住。只读预览时粘贴框不可用；还没首次整理时不出现。
- 技能页还没有整理过时（没有任何正在管理的 agent），主区域显示“首次整理”：选要一起整理的 agent → 核对保留哪份内容 → 完成；整理过以后这一块收起，照常显示技能列表。
- 设置页：左边 220px 是分两组的小目录（常用：AI 整理、agent、分享配置；记录与维护：操作记录、诊断与备份、版本），随页面滚动保持可见；右边按目录顺序把六块排下来，两组之间有组标题，每块是白面板加一句说明。点目录滚到对应那一块，滚动时目录高亮跟着当前看到的块走；地址记当前块（`#settings/<块名>`），刷新回到原处。宽度小于 1024px 时目录折到内容上面。
- 工具栏下面是 180px 的分组、标签目录和技能列表。目录直接放在浅灰底上，以细竖线和列表分开；当前分组有底色和该 agent 颜色的短竖线。目录随页面滚动保持可见，分组和标签过多时各自滚动。
- 标签直接浏览，按技能数量排列，不设标签搜索框；选定后立即筛选技能。仍是一个分组加一个标签，再点当前项取消；没有结果时保留已选项及数量 0，结果标题旁可清除筛选。标签选中时显示该 agent 颜色的实心圆点，数量在右边对齐。
- 标签列表加长，随窗口剩余高度分配空间；下方留白并加细分界线，滚动标签时边界固定可见。分组和标签各自滚动，调整窗口或切换页面后重新适配。
- 目录可收起，原处显示“展开筛选”；结果标题仍显示已选分组和标签。收起、展开、筛选重画后保留键盘焦点和目录滚动位置。
- “展开筛选”和结果标题统一用 14px 字、20px 行高，文字基线对齐；操作用 500 字重，标题用 600 字重，两者之间留间距及一条细分隔线。
- 列表行之间用 1px 细线，分组标题用 `surface-soft` 底色，不用卡片套卡片。
- 技能行里，一句话和标签排在同一行：一句话放不下就省略（鼠标移上去看全文），标签始终完整、可点，让列表更矮、更好找。
- 技能页工具栏在 1280px 宽的窗口里要排成一行：搜索框可以变窄，重新盘点只留图标，有新版时数量并进“全部更新”。窄到 1024px 时允许右边的按钮整体折到第二行。
- 页面底部留出 160px，给浮在底部的托盘让位。

## Elevation & Depth

两档阴影：`--shadow-1`（列表、卡片轻轻浮起）和 `--shadow-2`（托盘、详情面板、提示条、对话框）。不用彩色光晕，也不用硬边投影。

按钮按下用 80ms 的轻微缩放反馈，松开用 120ms，选中底色和标签圆点用 160ms。分组、标签、agent 和“在用 / 可以加的”按钮保留节点，避免重画打断过渡。目录收展和分组区域的高度变化用 260ms，切换筛选结果用 180ms 的淡入与轻微位移；连续操作立即采用最新选择，中断原有动效。系统开启“减少动态效果”时直接切换，不播放这些过渡。

## Shapes

控件圆角 8px，面板和底部托盘 12px，开关和标签用全圆角。不用超过 1px 的彩色左边框做强调。

## Components

- **页头**：高度至少 60px，品牌字标与 SP 标志并列，当前导航入口用冷白色细线标明。导航只留技能、清理、设置三项，排在整个窗口宽度的正中间（不是标志右边剩余空间的中间）；最右边只在只读预览时显示“只读预览”，能改的时候不显示任何标记。
- **开关**：加/撤技能都用开关（`role="switch"`），打开时是该 agent 的颜色；改了没生效时外圈多一圈黄。
- **底部托盘**（`#action-bar`）：所有“改了还没生效”的事都进这里，每处一行，写清给谁、加还是撤下、为什么（自动带上的写原因）。手动改的每处都能“不改这处”。
- **确认**：能撤销的操作直接做、事后给撤销提示；只有撤下、清理、不再管理这类操作才弹一次确认框。确认框第一句写结果，不列文件路径。
- **提示条**：右上角深色，带“撤销”的至少停 20 秒。
- **详情面板**：右侧滑出，“谁在用”每个 agent 一行带开关；路径等技术内容收进最后的“技术细节”。
- **占用条**：写“占用 7.2k Token”，表示每个 agent 每次开新对话要先读的技能说明有多长（按说明长度粗算）。已生效部分按分组分段；会加上的用黄色斜纹，会撤下的用空心斜纹；每 5k Token 一条刻度。大条旁边带一个说明图标，解释这个数是什么。
- **说明图标与提示**：长一点的解释放进说明图标（`.hint`，圆形的 ⓘ，点击区域 24px）。鼠标停一下或用键盘 Tab 到它，旁边浮出深色提示（全站只有一个浮层 `#tip`，用 popover 放到最上层，侧面板和对话框里也不会被切掉）；移开、失去焦点、按 Esc、滚动时收起。说明图标的读屏名字是“说明：”加全文。图标本身不可点，不触发任何操作。
- **小标记**：行里的“你改过”“会带上 N 个别的技能”是图标加两三个字的小标记，鼠标移上去在同一个浮层里看完整意思，详情面板里有同样的信息；它们不能 Tab 到。
- **页头任务条**：后台任务只写“正在检查更新 23/63”这样的一句进度加进度条，不列正在查哪些来源。

## Do's and Don'ts

- 要：说结果，不说机制。“Codex 看不到它”而不是“链接到统一仓库”；做完的事用过去时。
- 要：能用“图标加几个字”说清的，就不写成一段话；多出来的解释放进说明图标的提示里。页面上不留说明性的整段文字。
- 要：下面这些必须读的保持完整文字，不收进提示：确认框、报错和提示条、底部托盘里的改动行、发给 AI 服务什么内容这类安全说明（可以写短，但要在眼前）、每一行的“待应用”状态。
- 要：拿不到的信息不逐行写“不知道”“统计不到”，要么不显示，要么在页面说明里说一次。
- 要：所有操作能用键盘完成，焦点框清楚，关掉面板后焦点回到原来的位置。
- 不要：路径、仓库、链接、哈希这类词出现在主界面。
- 不要：为一件不需要打断的事弹对话框。
- 不要：用 emoji 或字符代替图标；需要图标时用统一线宽的 SVG。
- 不要：只有图标、没有名字也没有提示。只有图标的按钮必须有 `aria-label` 和 `title`；图标旁的字不超过四五个。

## 图标

2026-10-14 用 brandkit 建立 SP 图标系统。主标志由相扣的 S 路径和 P 回环组成，表示一份技能库分给多个 agent；只使用深墨色和冷白色。页头标志为 34px，导航图标为 18px，沿用现有文字和选中状态。

导航图标共用 24 × 24 网格、1.75 的线宽与圆角：技能用叠层，清理用刷子，设置用滑杆（2026-10-16 起导航只留这三项，操作记录和诊断并进设置，原来的时钟和盾牌图标不再使用）。图标与文字一起显示，读屏只读文字。浏览器小图标使用深墨色方底和冷白色标志。

### 图标加几个字（2026-10-14）

界面靠“图标加几个字”带路：原来的说明句改成一个图标加两三个字，长一点的解释放进图标旁的提示（说明图标 ⓘ，鼠标停一下或键盘 Tab 到它就显示）。写法上：

- 一眼能懂的事：图标加两三个字，如“撤下后文件还留着”前面放文件图标，“只读取文件，不运行里面的程序”前面放锁。
- 要多解释的：字后面跟一个说明图标，完整的话放在提示里。
- 必须读的不收起来：确认框、报错、提示条、托盘里的改动行、发给 AI 服务什么内容。
- 路径、文件夹位置这类技术信息，主行不写，需要时放进提示或“技术细节”。

所有图标都是 `index.html` 顶部 `<defs>` 里的 `<symbol>`：24 × 24 网格、线宽 1.75、圆头圆角、`currentColor`、不填充。图标放在文字旁时写成 `aria-hidden`（`app.js` 里的 `icon(名字)`），只有图标的按钮要有 `aria-label` 和 `title`。每个图标只在真的用到时才加进去，现在除三个导航图标（技能、清理、设置）外还有：

| 图标 | 用在哪 |
| --- | --- |
| `sp-update-up` | 有新版的小箭头、“全部更新”、详情里的“有新版” |
| `sp-info` | 说明图标（长解释的入口）、一般提示 |
| `sp-lock` | 安全说明：只读取文件不运行程序、key 只存在这台电脑上、至少要留一个 agent |
| `sp-undo` | 撤销、恢复、“每次改动都能撤销”、已撤销的标记 |
| `sp-file-kept` | “撤下后文件还留着” |
| `sp-sort` | 清理表“占用 Token”列（占用多的在前） |
| `sp-plus` | 安装、加上、发现新 agent |
| `sp-scan` | 重新盘点（只有图标；和“检查更新”的循环箭头区分开） |
| `sp-link` | 和别的 agent 共用一个技能文件夹、会一起带上别的技能 |
| `sp-sparkle` | 要用 AI 服务的地方：设置 AI 服务、整理中文名和标签 |
| `sp-check-circle` | 都是最新的、都整理好了、已完成、没改过 |
| `sp-warning` | 没查成、还差几个、同名技能要先对比 |
| `sp-edit` | 你改过 |
| `sp-inbox` | 发现新装的技能（一键收编） |
| `sp-refresh` | 刷新列表、刷新记录 |
| `sp-search` | 没找到时的空状态 |
| `sp-close` | 收起左栏的安装概要卡片 |

设计板在 `docs/brand/skill-pilot-brandkit.png`；深浅标志与单独的导航图标在 `skill_pilot/web/static/brand/`，浏览器小图标在 `skill_pilot/web/static/favicon.svg`。页面直接内嵌图形，加载不依赖外部服务。

Mac 应用图标使用同一枚 SP 标志，深墨色圆角方底，四周留透明边缘以匹配系统应用图标。1024 × 1024 原稿在 `assets/SkillPilot.svg`，安装素材在 `assets/SkillPilot.icns`，包含 16、32、128、256、512 点及各自两倍尺寸。`scripts/build-app.sh` 会复制图标并写入应用图标配置，重新生成启动器时仍会保留它。
