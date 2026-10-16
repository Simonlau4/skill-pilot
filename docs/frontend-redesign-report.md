# 重做看板前端 · 交付报告

> 2026-10-11，DSH，工作区 `~/Developer/skill-pilot-frontend`（分支 `frontend-redesign`）。
> 依据：`docs/handoff-frontend-redesign.md`。改动全部留在工作区，没有提交、没有切分支。

前端从「单个 HTML 内嵌压缩 CSS + 515 行 app.js」改成三个文件：`index.html`（骨架）、`app.css`（样式）、`app.js`（逻辑）。顶部 11 个标签收成 4 个主页面 + 1 个低调的「诊断与备份」。所有写入仍然走确认框、备份和撤销，仍然是本机地址加口令，文案仍然是大白话中文。

---

## 1. 旧页面的问题清单（诊断）

按 `redesign-existing-projects` 的「扫描 → 诊断 → 修复」顺序先读旧代码（`git show HEAD:skill_pilot/web/static/{index.html,app.js}`），逐条列问题，再动手。归类沿用该技能的审计小节。

### 1.1 Typography

| 问题 | 证据 |
| --- | --- |
| 38px 大标题 + 全大写 2px 字距的 eyebrow，是营销落地页写法 | `h1{font-size:38px}`、`.eyebrow{letter-spacing:2px}` + `<div class="eyebrow">SKILL PILOT / 本机盘点</div>` |
| 字号跳级、没有尺度 | 同一页出现 42 / 38 / 23 / 18 / 15 / 13 / 12 七种字号，没有变量 |
| 数字用比例字体 | `.count{font-size:42px}` 与技能字数列都没有 `tabular-nums`，130 行数字对不齐 |
| 只有 400 / 650 两档字重 | `h1{font-weight:650}`，正文没有 500 / 600 的层次 |

### 1.2 Color and Surfaces

| 问题 | 证据 |
| --- | --- |
| 同一个绿色写了两遍 | `--accent:#286248`，但 `.notice` 背景写死 `#eaf0e8` |
| 提醒色是另一套暖橙，不是规范里的琥珀 | `.notice.warn{border-color:#ad6a25;background:#f4ebdd}` |
| 没有成功色和危险色 | 全站只有「正常 / 提醒」两种状态 |
| 首屏四张大卡片是通用仪表盘模板 | `.cards{grid-template-columns:repeat(4,1fr)}` + `.count{font-size:42px}`，占掉首屏一半高度只说了四句话 |
| 卡片是「边框 + 白底 + 大数字」的 generic card look | `.card{border-right/bottom:1px solid var(--line)}` |

### 1.3 Layout

| 问题 | 证据 |
| --- | --- |
| 11 个平级标签，其中 4 个是开发期诊断页 | `版本差异 / 助手与目录 / 其他发现 / 首次迁移`，迁移 2026-10-10 就完成了 |
| 首屏卡片和勾选表卡片完全重复 | `#cards`（来自 `/api/scan`）与 `#assignment-counts`（来自 `/api/table`）说的是同一件事，数字还不同 |
| 生效方式塞进表头 | `<th>${a.name}<small>${a.activation}</small></th>` → 表头里出现「Antigravity 新会话生效」 |
| 「最近使用」一行堆四行 | 最后一列 `management.agents.map(a=>…a.name 最近使用…)`，130 行表格被拉长到约 4 倍 |
| 没有固定表头 / 首列 | `.table-wrap{overflow:auto}` 里 `th` 没有 `position:sticky`，滚到第 80 行不知道在看哪个技能 |
| 提示信息三套写法 | `#updates-progress`、`#usage-progress`、各页 `#*-status` 各写各的 |

### 1.4 Interactivity and States

| 问题 | 证据 |
| --- | --- |
| 按钮没有悬停和按下态 | 旧 CSS 只有 `button[aria-selected=true]` 和 `:focus-visible`，没有 `:hover` / `:active` |
| 没有主次按钮 | 全站只有一种 `.badge` 样式，主操作（应用、检查更新、安装）和次要操作长得一样 |
| 没有按钮忙碌态 | `$('x').disabled=true` 之后按钮文字不变，用户不知道点没点上 |
| 勾选后 3 秒没有任何反馈 | `/api/assignment/preview` 在 130 个技能上要跑约 3 秒（后端要对每个技能算树哈希），旧代码只把勾选框禁用 |
| 用浏览器自带弹窗 | `confirm(` 14 处、`prompt(` 1 处（加依赖）；`window.confirm` 不能排版，清理确认只能挤成一坨 |
| 看板断开和操作失败混在一起 | `manageError()` 同时写 `#manage-error` 和 `#connection` |
| 空状态只有半句话 | `没有符合条件的技能。` |

### 1.5 Content

- 全大写 eyebrow「SKILL PILOT / 本机盘点」。
- `发现 12 个助手，0 组版本差异，0 个用户技能链接失效。` 把开发期指标摆给用户看。
- 成功提示带感叹号、`将给 Claude 加 2 个` 这类句子直接沿用（这些是好句子，保留了）。

### 1.6 Code Quality

| 问题 | 证据 |
| --- | --- |
| HTML 里内嵌 13 行压缩 CSS | 单行最长 700+ 字符，没有换行 |
| 8 处内联 `style="..."` | `#install-dialog`、`#pending-bar`、`#usage-coverage`、`pre` 等 |
| 逻辑全塞在 515 行里，单行上千字符 | 旧 `app.js` 第 42、102、175、206、308、313、319、395 行 |
| 没有分段注释 | 只有靠变量名猜哪段是哪页 |
| id 名和内容脱节 | `#skills` 其实是技能清单的 `<tbody>`，`#search` 是搜索框 |
| 诊断页和用户页用同一套样式 | 「其他发现」「版本差异」和主表一样显眼 |

### 1.7 与 `minimalist-ui` 的差距

| 规范要求 | 旧代码 |
| --- | --- |
| 暖白底 `#F7F6F3` | 用了 `#f6f5f1`（接近但不是规范值） |
| 1px 浅边框 `#E8E6E1` | `#d8ded7`（偏绿、偏深） |
| 次要文字 `#787774` | `#647169`（偏绿） |
| 单一深色主按钮 `#2F3437` | 主按钮就是 `--ink:#232b27`，没有独立的按钮色 |
| 低饱和状态色四组 | 只有两组，且不是规范里那两组 |
| 几乎不用阴影 | 没阴影（这点旧代码是对的） |
| 不用表情符号 | 没用（对的） |
| 圆角克制 | 全站统一 4px，没有「控件 6px / 面板 10px」的层次 |

### 1.8 结论与修复顺序

按该技能的 Fix Priority：字体与字号尺度 → 调色统一 → 悬停/按下/忙碌态 → 布局（去重、收标签、固定表头）→ 换掉 `<dialog>` 与通用卡片 → 补空/错/加载状态 → 最后统一间距与圆角。全部在原生 CSS + 原生 DOM 里做，没有引入框架、构建工具、npm 包、CDN、外部字体或图片。

---

## 2. 新结构说明

### 2.1 文件

| 文件 | 作用 |
| --- | --- |
| `skill_pilot/web/static/index.html` | 页面骨架。不含 `<style>` 块，不含任何 `style="..."` 属性 |
| `skill_pilot/web/static/app.css` | 全部样式。开头 `:root` 用变量定义颜色、间距、圆角、字号、层级、动效时长 |
| `skill_pilot/web/static/app.js` | 全部逻辑。按「通用工具 → 页头与导航 → 技能页 → 技能详情 → 安装技能 → 检查更新页 → 清理页 → 操作记录页 → 诊断与备份页 → 启动」分段，每段一行中文注释 |

### 2.2 页面

- **技能**（默认页）：搜索、5 个筛选（全部 / 没分配给任何助手 / 很久没用 / 你改过 / 有依赖）、`技能 | Codex | Claude | Antigravity | DSH | 最近使用 | 说明字数` 表格、新技能横幅、安装技能。
- **检查更新**：中间一个主按钮，逐条出结果，底部固定操作栏。
- **清理**：上半「建议清理」，下半「已撤下的技能」。
- **操作记录**：时间线 + 每条「撤销」。
- **诊断与备份**（导航最右侧、次要样式）：版本差异、助手与目录、其他发现、迁移与还原，四个折叠块默认收起。

### 2.3 共用组件（`app.js` 第一段）

- `api()`：统一请求，错误对象带 `retry`（后端 409 里给的重试标记）。
- `escape()`：沿用原来的实现，所有插入页面的文本都过一遍。
- `withBusy()`：按钮忙碌态（禁用 + `aria-busy` + 换文案，做完恢复）。
- `confirmDialog()`：全站唯一的确认方式，基于一个 `<dialog>`，带标题、多行明细、脚注和按钮文案。`window.confirm` / `alert` / `prompt` 一处都没有。
- `toast()`：右下角提示条，成功自动消失、出错留着、可带「撤销」。
- `setTask()` / `clearTask()` / `finishTask()`：页头任务条。用量统计和检查更新都走这里，完成后 4 秒自动消失，出错变警告样式并给「重试」。
- `showActionBar()` / `hideActionBar()`：屏幕底部固定操作栏，只在有改动时出现。

### 2.4 视觉取值（全部在 `app.css` 的 `:root` 里）

底色 `#F7F6F3`、面板 `#FFFFFF`、边框 `#E8E6E1`、正文 `#2F3437`、次要文字 `#787774`、主按钮 `#2F3437` 白字、强调绿 `#2F6B4F`；成功 `#EDF3EC`/`#346538`、提醒 `#FBF3DB`/`#956400`、危险 `#FDEBEC`/`#9F2F2D`、信息 `#E1F3FE`/`#1F6C9F`；圆角控件 6px、面板 10px；间距 4/8/12/16/24/32；阴影只给对话框和右侧面板；动效 150–200ms 且只动 `transform` 和 `opacity`。

按要求**没有**做：外部字体、图标库、占位图、背景图片、噪点纹理、玻璃效果、滚动入场动画、错位叠放、衬线大标题、深色模式、多语言。

---

## 3. 旧功能 → 新位置对照表

### 3.1 旧标签页 → 新页面

| 旧标签页 | 新位置 |
| --- | --- |
| 技能清单（`data-panel="inventory"`） | 「技能」页主表 + 每行右侧详情面板；目录视角与「同时看内置和插件」移到「诊断与备份 → 助手与目录」 |
| 版本差异（`conflicts`） | 「诊断与备份 → 版本差异」（默认收起） |
| 助手与目录（`agents`） | 「诊断与备份 → 助手与目录」 |
| 其他发现（`candidates`） | 「诊断与备份 → 其他发现」 |
| 勾选表（`assignments`） | 「技能」页（默认页） |
| 检查更新（`updates`） | 「检查更新」页（同级主页面） |
| 用量（`usage`） | 并入「技能」页的「最近使用」列 + 详情面板的「各助手使用情况」；「更新用量」变成页头任务条 |
| 清理（`cleanup`） | 「清理」页上半部分 |
| 已撤下（`withdrawn`） | 「清理」页下半部分 |
| 操作记录（`operation-log`） | 「操作记录」页 |
| 首次迁移（`migration`） | 「诊断与备份 → 迁移与还原」（入口文案已改） |

### 3.2 旧元素 ID → 新位置

`index.html` 里的 96 个旧 id 全部有落点，逐个列出。

**页头与状态（7）**

| 旧 id | 新位置 |
| --- | --- |
| `mode-badge` | 页头右上角状态标签，同名保留 |
| `connection` | 页头下方的危险色横幅，文案不变 |
| `cards` | 删除；四个助手合并成页头的一条摘要（顺序固定 Codex、Claude、Antigravity、DSH） |
| `basis` | 移到技能表格下方小字 `#skills-note` |
| `global-new-skills` / `global-new-text` / `global-new-open` | 合并成技能页上方的 `#new-banner` + `#new-text` + `#new-toggle` |
| `footer` | 页脚 `#footer-note`，保留 30 秒退出说明与盘点时间 |

**技能清单页（9）**

| 旧 id | 新位置 |
| --- | --- |
| `inventory` | 「技能」页主表 `#skills-rows` |
| `search` | 改名 `#skills-search`，功能不变（名称 + 说明） |
| `root-filter` | 「诊断与备份 → 助手与目录」的目录下拉，同名保留 |
| `include-readonly` | 「诊断与备份 → 助手与目录」的复选框，同名保留 |
| `inventory-count` | 同名保留在诊断页 |
| `skills` | 改名 `#skills-rows`（原名与内容脱节） |
| `conflicts` / `conflict-list` / `conflict-count` | 「诊断与备份 → 版本差异」，同名保留 |

**勾选表页（15）**

| 旧 id | 新位置 |
| --- | --- |
| `assignments` | 「技能」页主表 `#skills-rows` |
| `assignment-head` / `assignment-rows` | 合并进 `#skills-head` / `#skills-rows` |
| `assignment-search` | 合并进 `#skills-search`（一个搜索框同时管名称和说明） |
| `assignment-counts` | 删除（与页头摘要重复）；页头摘要新增「→ 应用后 M 个 / Y 字」 |
| `pending-bar` / `pending-summary` / `pending-reasons` | 屏幕底部固定操作栏 `#action-bar` / `#action-summary` / `#action-note` |
| `assignment-apply` / `assignment-discard` | 操作栏里的「应用」（主按钮）/「取消」 |
| `manage-refresh` | 工具条右侧「重新盘点」 |
| `manage-error` | 删除；错误统一走提示条 + 出错的勾选框下方小字 |
| `install-open` | 工具条右侧「安装技能」（主按钮） |
| `new-skills-notice` / `new-skills-text` / `new-skills-list` | 技能页上方 `#new-banner` / `#new-text` / `#new-list` |
| `absorb-all` | 横幅里的「一键收编」`#new-absorb` |
| `incoming-conflicts` / `incoming-comparisons` | 移到安装对话框下半部分，同名保留 |

**检查更新页（8）**

| 旧 id | 新位置 |
| --- | --- |
| `updates` | 「检查更新」页 |
| `updates-check` | 页中间的主按钮，同名保留 |
| `updates-status` | 页面摘要 `#updates-summary` |
| `updates-progress` | 合并进页头任务条 `#task-bar` |
| `updates-error` | 删除；错误走提示条和任务条警告样式 |
| `updates-list` | 结果卡片列表，同名保留（一行一张卡，不再是 `<article class="version">`） |
| `updates-other` | 「其他技能」折叠块 `#updates-other-box` + `#updates-other` |
| `updates-apply` / `updates-all` | 底部固定操作栏的「更新选中的 N 个」（主）/「全部更新」（次） |

**用量页（6）**

| 旧 id | 新位置 |
| --- | --- |
| `usage` | 删除；数据并入「技能」页 |
| `usage-refresh` | 删除；进页面自动在后台统计，页头任务条显示进度 |
| `usage-status` | 页头任务条 |
| `usage-progress` | 合并进页头任务条 |
| `usage-coverage` | 技能表格下方小字 `#skills-note`（覆盖说明）+ 详情面板各助手一行 |
| `usage-rows` | 删除；「最近使用」列 + 详情面板「各助手使用情况」（含近 30 天次数） |

**清理页与已撤下（11）**

| 旧 id | 新位置 |
| --- | --- |
| `cleanup` / `withdrawn` | 合并成「清理」页的上下两部分 |
| `cleanup-refresh` | 删除；进页面自动生成，失败给「重试」 |
| `cleanup-status` | 同名保留 |
| `cleanup-error` / `withdrawn-error` | 删除；错误走提示条 |
| `cleanup-coverage` | `#cleanup-status` 的悬停说明（`title`） |
| `cleanup-savings` | 同名保留，改成一行小卡片（`可省 N 字说明`） |
| `cleanup-rows` | 同名保留 |
| `cleanup-apply` | 底部固定操作栏的「清理选中的 N 个」（主按钮） |
| `withdrawn-refresh` | 同名保留 |
| `withdrawn-check` | 同名保留（点了会跳到「检查更新」页并开始检查已撤下的技能） |
| `withdrawn-list` | 同名保留，每条一个「恢复原分配」 |

**操作记录页（4）**

| 旧 id | 新位置 |
| --- | --- |
| `operation-log` | 「操作记录」页 |
| `operation-list` | `#log-list`（`<ol class="timeline">`），同名保留 |
| `log-refresh` | 同名保留 |
| （撤销按钮） | 保留，改走确认框而不是 `window.confirm` |

**首次迁移（16）**

| 旧 id | 新位置 |
| --- | --- |
| `migration` | 「诊断与备份 → 迁移与还原」`#diag-migration-box` |
| `migration-mode` / `migration-error` | 同名保留 |
| `migration-select` / `migration-agents` / `migration-preview` | 同名保留（第 1 步：选择要接入的助手） |
| `migration-review` / `migration-summary` / `migration-counts` / `migration-versions` / `migration-names` / `migration-skipped` | 同名保留（第 2 步：核对保留哪份内容，含「查看完整差异」） |
| `migration-back` / `migration-apply` | 同名保留 |
| `migration-complete` / `migration-result` / `migration-result-detail` / `migration-restore` / `migration-backups` / `migration-backup-list` | 同名保留（**一键还原和备份列表照旧可用**，浏览器实测通过） |

**对话框（8）**

| 旧元素 | 新位置 |
| --- | --- |
| `install-dialog` | 同名保留，改成 `<dialog>` + `<form method="dialog">`，样式全站统一 |
| `install-url` / `install-inspect` / `install-message` / `install-skills` / `install-agents` / `install-apply` / `install-close` | 全部同名保留，功能不变 |
| `window.confirm`（14 处） | 统一换成 `#confirm-dialog` |
| `window.prompt`（1 处，加依赖） | 换成详情面板里的下拉选择 + 「添加需要的技能」按钮 |

---

## 4. 改动的文件与行数

```
$ wc -l skill_pilot/web/static/index.html skill_pilot/web/static/app.css \
        skill_pilot/web/static/app.js skill_pilot/web/server.py
     283 skill_pilot/web/static/index.html
     727 skill_pilot/web/static/app.css
    1659 skill_pilot/web/static/app.js
     345 skill_pilot/web/server.py
    3014 total

$ git diff --numstat
4       3       skill_pilot/web/server.py
1600    456     skill_pilot/web/static/app.js
267     76      skill_pilot/web/static/index.html
（app.css 是新增文件，不在 diff --numstat 里）
```

改动前：`index.html` 92 行（含 13 行压缩 CSS）、`app.js` 515 行、无 `app.css`。

后端只改了一处（§0 唯一允许的改动）：`server.py` 的静态文件分支从 `("/", "/app.js")` 扩成 `("/", "/app.js", "/app.css")`，同样做 `__SESSION_TOKEN__` 替换、同样要口令，按后缀给 `text/html` / `text/javascript` / `text/css`。接口、请求体、`confirmed`、`revision`、口令校验、CSP 一律没动。

---

## 5. §6 验证结果

### 5.1 全量测试

```
$ PYTHONDONTWRITEBYTECODE=1 ~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11 -m unittest discover -q
----------------------------------------------------------------------
Ran 149 tests in 48.346s

OK
```

（改了 `server.py` 之后又跑了一次，仍是 149 项全过。）

### 5.2 前端语法

```
$ ~/.nvm/versions/node/v24.21.0/bin/node --check skill_pilot/web/static/app.js
（无输出即通过）
```

### 5.3 静态检查

```
$ grep -n "confirm(\|alert(\|prompt(" skill_pilot/web/static/app.js
无匹配

$ grep -n "http://\|https://" skill_pilot/web/static/index.html skill_pilot/web/static/app.css skill_pilot/web/static/app.js
无匹配

$ grep -n "<style\|style=" skill_pilot/web/static/index.html
无匹配
```

另外用 Node 做了一次三个文件之间的交叉自查（`index.html` 的 id ↔ `app.js` 的 `$()` ↔ `app.css` 的类名）：

```
$ node -e '…（脚本见下方说明）…'
index.html id 数量: 95 | 重复: 无
app.js 里 $() 引用的 id 数量: 83 | 缺失: 无
有 class 但 app.css 里没有对应样式的: 无
```

说明：`#dep-select` 是详情面板打开时动态生成的，所以脚本把它列进白名单；`app.js` 里那处访问也用了可选链（`$('dep-select')?.value`），即使面板不在也不会报错。

### 5.4 冒烟（模拟 home）

```sh
$ PY=~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11
$ FAKE=$($PY -c "import tempfile,pathlib;print(pathlib.Path(tempfile.mkdtemp(prefix='sp-dsh-final-')).resolve())")
$ PYTHONDONTWRITEBYTECODE=1 $PY scripts/make-fake-home.py "$FAKE"
$ SKILL_PILOT_HOME="$FAKE" BROWSER=true PYTHONDONTWRITEBYTECODE=1 $PY -m skill_pilot ui --json   # 后台
{"message": "看板已打开；…", "pid": 32221, "url": "http://127.0.0.1:56909/?token=…"}
```

地址和口令从临时目录里 `skill-pilot-501-f468527e845e1677.lock` 读出，`pid` 与启动的进程一致（32221）。

```
=== 请求结果（带口令）===
/            -> 200 text/html; charset=utf-8 12254B
/app.css     -> 200 text/css; charset=utf-8 21975B
/app.js      -> 200 text/javascript; charset=utf-8 75188B
/api/scan    -> 200 application/json; charset=utf-8 272036B
/api/table   -> 200 application/json; charset=utf-8 146319B

=== 不带口令（应 403）===
/            -> 403
/app.css     -> 403
/app.js      -> 403
```

`index.html` 里的两处 `__SESSION_TOKEN__` 都被替换成了本次口令。做完已关掉这个进程（`kill 32221`），并确认用户在跑的真实看板（pid 10139，另一把锁）没被碰过。

> 说明：§6.4 的写法用 `tempfile.mkdtemp()` 得到的 macOS 路径带 `/var/folders/...`，而 `store.guard()` 里 `Path.absolute()` 不解析 `/var` → `/private/var` 这一层软链接，于是 `/api/status` 会报 `read_only: true`（`/api/table` 的 `write_allowed` 仍是 true）。这是后端原有的小不一致，不是前端造成的；把 `$FAKE` 换成 `pathlib.Path(...).resolve()` 之后的路径，`/api/status` 就返回 `read_only: false`。详见 §7.3。

### 5.5 浏览器逐页实测（交接文件说可以不做，我做了）

§6.5 说「你看不到页面效果，这没关系」。为了不把结构性问题留给主控，我用本机 Chrome 的无头模式 + 远程调试协议把每一页都真跑了一遍（模拟 home，没有碰真实环境，也没有用 `npx`）。驱动脚本在 `/tmp/sp-driver*.js`，截图在 `/tmp/sp-shots*/`。

结果是 **0 条控制台异常、0 条 console.error**，并且逐项验证：

| 场景 | 结果 |
| --- | --- |
| 技能页渲染 | 130 行；表头 `技能 / Codex / Claude / Antigravity / DSH / 最近使用 / 说明字数` |
| 页头摘要 | `Codex 89 个技能 · 说明 27,448 字 新会话生效 …`，勾选后出现绿色 `→ 应用后 90 个 / 27,521 字` |
| 勾选 → 预览 | 任务条出现「正在核对这次勾选带来的改动…」；操作栏变「将给 Codex 加 1 个」 |
| 应用 → 确认框 | 标题「应用这些分配改动」，正文「将给 Codex 加 1 个 / 完成后可以在操作记录里撤销」 |
| 应用失败 | 后端的「Codex 的实际技能名单与勾选结果不一致」原样显示在提示条里，待应用改动保留，可重试或取消 |
| 筛选 | 有依赖 37 行、你改过 1 行、搜索 `resume` 13 行 |
| 详情面板 | 打开右侧面板，三个分节（各助手使用情况 / 各助手里的入口 / 需要的技能），依赖增删下拉可用 |
| 真·检查更新 | 97 个技能，任务条逐条走「正在检查更新（已查 40/97 个技能），正在查 …」，结束后「有更新 1 个，已是最新 62 个，没查成 0 个」，结果卡片 1 张，其他技能折叠列出 96 条 |
| 更新卡片 | 勾选框默认选中、「本机没改过 / 你改过」标签、来源、上游日期、文件与增删行数、琥珀色脚本地块（含命中原文）、`看完整对比 / 让 Codex 解释 / 交给 Codex 合并` 文字链接、二选一「用上游的 / 保留我的」、`只更新这一个` |
| 清理页 | 默认全选，取消勾选后操作栏变「将从所有助手撤下 1 个技能」；确认框列出明细；「已撤下」每条一个「恢复原分配」 |
| 诊断页 | 14 个目录、204 个目录入口（勾「同时看内置和插件」后 218 个）、4 个助手折叠、目录下拉 15 项 |
| 迁移与还原 | 真实计划「共 100 个技能，0 组内容需要核对」；备份列表列出 2 份并给出「还原这份备份」+ 确认框；合成带冲突的计划也能正确渲染单选、名称不一致、跳过目录、差异入口 |
| 断开提示 | 杀掉看板进程后 5 秒内页头出现红条「看板已关闭或连接中断。重新双击 Skill Pilot 即可继续。」 |
| 空状态 | 搜索无结果时「没有找到这个技能，换个词试试。」；清理为空时「没有需要清理的技能。60 天内都用过。」；更新未检查时「还没有检查过。按下上面的按钮开始，检查不会改动任何文件。」 |
| 点按区域 | 勾选框点击区 32×32（`.check-hit`），按钮 `min-height: 32px` |
| 键盘 | `:focus-visible` 绿色描边；`<dialog>` 打开后 Esc 关闭；安装框里回车不会误关 |
| 窄屏 | 640px 时表格横向可滚（699 > 606），首列 `position: sticky` 固定在左边，表头也固定 |

截图人工看过：技能页、检查更新页（真实结果）、清理页、操作记录页、诊断页、详情面板、安装对话框、确认框、断线条、窄屏。

---

## 6. 没做完 / 拿不准的地方

1. **变更摘要的日期字段是「上游最后一次提交时间」**，来自后端的 `updated_at`。原来那行写作「上游更新：日期」，如果官方仓库这次提交没动这个技能文件夹，日期可能看起来「很旧」——这是后端口径，前端照搬。
2. **勾选预览要等约 3 秒**（后端对 130 个技能逐个算树哈希）。我加了页头任务条提示，但没法在前端缩短等待。如果以后要更快，需要后端配合（见 §7）。
3. **「很久没用」筛选**用 60 天阈值，和清理页一致；后端清理名单还看「入库未满 60 天」「分配给统计不到的助手」这些条件，所以筛选出来的行数会比清理候选多。这是有意的：筛选是给用户找技能用的，不是清理名单。
4. **统计不到用量时的措辞**：四个助手都统计不到时只说一句「四个助手的用量都统计不到，多半是还没有使用记录。」，不再重复四遍。
5. **页头摘要有两套数字来源**：摘要条用 `/api/table` 的「已分配」口径（因为它要和「应用后」对比），悬停提示里给 `/api/scan` 的「实际可用」口径。旧版把这两套数字分别放在首页大卡片和勾选表卡片里，看起来像互相矛盾；现在只留一处，另一处放进悬停。
6. **`#inventory-search`**：旧「技能清单」页只有一个搜索框，现在主表一个、诊断页一个，各自独立。
7. **没做深色模式和多语言**（交接文件明确不做）。
8. **动效只做了悬停 / 按下 / 面板滑入**，没有加入场动画（交接文件明确不做）。

---

## 7. 需要后端配合的地方

按 §0「如果不改后端就做不到，先不做，写进报告」，以下都没做：

1. **站点图标**：浏览器会请求 `/favicon.ico`，现在返回 403，控制台里会留一条 `Failed to load resource: 403`。加图标要么新增静态文件、要么加路由，都超出「只允许动 `/app.css`」的范围。不影响使用，但控制台不干净。**建议**：以后允许加一个 16×16 的图标文件并让它和 `/app.css` 一样提供（同样要口令），或者由主控决定不做。
2. **`/api/status` 与 `/api/table` 的只读口径不一致**（§5.4 备注）：`store.guard()` 里 `path.absolute()` 不解析 `/var` → `/private/var`，而 `home` 已经 `resolve()` 过，于是 `preview_allowed()` 会误判成只读。只有在 `SKILL_PILOT_HOME` 指向 macOS 临时目录的未解析路径时才会出现，真实主目录不受影响。**建议**：把 `guard()` 里那处 `Path(path).absolute()` 改成不改变语义的写法（先 `resolve()` 父目录再拼名字，或两边都 `absolute()`），属于后端口径统一，前端无法绕过。
3. **勾选预览的耗时**：`/api/assignment/preview` 每次都对全部技能重算 revision（约 3 秒 / 130 个技能）。如果希望勾选后立刻有反馈，可以让后端接受「只算这次改动涉及的技能」或者在 `/api/table` 里带一个短时效的 revision，前端就能做乐观更新。现在前端只能如实显示「正在核对…」。
4. **更新检查单条结果的增量推送**：现在是前端每 1.2 秒轮询 `/api/updates/progress` 并整体重绘结果列表。技能多的时候每次重绘 100+ 张卡片。如果以后结果规模继续变大，可以只返回增量（新增的行），前端就不用整体重绘。现在 130 个技能的规模完全够用。
5. **更新卡片里「上游更新」的日期**（见 §6.1）：如果希望显示「这个技能上次变化是什么时候」而不是「仓库最后一次提交」，需要后端换字段。

以上五条都不影响这次交付的功能完整性。

---

# 第二轮修改

> 2026-10-11，主控在浏览器里按 1440×900 和 1024×768 逐页看过之后提出的 6 项 + 3 件顺手的小事。
> 仍然不提交、不改后端接口、不动主控正在用的看板进程（端口 59604 / pid 10139 一直在跑，没有碰过）。

## 1. 顶部区域太高

**改法**

- `.topbar` 去掉 `position: sticky`，整页正常滚动；页头不再是固定层。
- `.table-wrap` 去掉 `overflow: auto` 和 `max-height`，表格不再有自己的滚动框。表头行（`thead th`）用 `position: sticky; top: 0` 贴住视口顶部，并加了一条 1px 下边线，滚动时和表格内容分得清。
- 窄屏时改由整页横向滚动：`#skills-table` 给 `min-width: 820px`，窗口更窄时列不会被压扁；第一列仍然是 `position: sticky; left: 0`，贴在视口左边。
- 页头一行放完：`.topbar-row` 从 flex 换成 `grid-template-columns: auto minmax(0,1fr) auto`，标题 + 副标题合成一个 baseline 对齐的 `.brand`，导航靠右，模式标记在最右，全程不换行。
- 四助手摘要改 `grid-template-columns: repeat(4, minmax(0,1fr))`，≥1024px 固定一行；<1024px 变 2×2。
- 断开提示 `#connection` 改成固定在视口顶部，避免页头不吸顶之后用户看不到它。

**实测（模拟 home，130 个技能）**

| | 改前 | 改后 |
| --- | --- | --- |
| 页头高度（1024×768） | 3 行 | 109px（1 行标题 + 1 行摘要） |
| 表格第一行距页面顶部 | 391px | 273px |
| 1024×768 一屏能看到的技能行 | 6 行 | 8 行 |
| 1440×900 一屏能看到的技能行 | — | 10 行 |

```
$ 滚动到 scrollY=1400 后
表头 th 距视口顶部: 237 → 0        （贴住了）
第一列 position: "sticky"          （仍在视口左边）
```

640px 宽时整页可横向滚动（`scrollWidth > clientWidth`），横滚后第一列 `left = 0`，表头依旧贴顶。

## 2. 四助手摘要换行跳动

**改法**：摘要条改成固定 4 列网格（≥1024px 一行，<1024px 2×2），每个格子固定两行——第一行「名称 + N 个技能 · 说明 X 字」，第二行 `min-height: 18px` 固定预留：平时写生效方式（新会话生效 / 马上生效），有没应用的改动时换成绿色的「应用后 47 个 / 12,309 字」。两种状态高度完全一样。

原来那个 `→ 应用后 …` 是追加在行尾的，所以一勾选就把 DSH 挤到第二行；现在它只换第二行的内容，不动布局。

**实测**

```
1440×900  摘要条高度 42 → 42（勾选前后一致）；四个 chip 的 top 都是 58，即一行
1024×768  四个 chip 的 top 都是 58，即一行
1024×768  四个「… 个技能 · … 字」都没有被截断（scrollWidth ≤ clientWidth）
```

1024px 下每格约 230px，「Antigravity」这个名字最长，加上数字会差十几个像素。所以在 <1200px 时用一个 `.agent-word` 把「说明」两个字隐掉，变成「Antigravity 39 个技能 · 10,185 字」——数字和「字」都留着，读起来不含糊；≥1200px 恢复完整的「… · 说明 10,185 字」。属性提示（`title`）里始终有完整信息和生效方式。

## 3. 确认框列出明细

**改法**：`confirmDialog()` 增加 `groups` 参数，格式是 `[{label, items:[{name, note}]}]`，按组列出技能名；每组超过 8 个时折叠成「展开其余 N 个」的文字按钮，点开显示全部。所有写操作的确认框都改用它：

| 操作 | 确认框里现在列什么 |
| --- | --- |
| 应用分配 | 按助手分组：「给 Claude 加上 2 个」「从 Antigravity 撤下 1 个」，逐条列技能名；依赖自动带上的标注「（因依赖自动加上）」 |
| 收编新技能 | 「收进仓库 N 个」+ 全部技能名 |
| 安装技能 | 「安装 N 个技能」+ 技能名，另起一行写分配给谁 |
| 更新技能 | 「更新 N 个技能」+ 技能名；选了「保留我的」或「用已核对的合并结果」的标注在后面 |
| 清理 | 「要撤下的技能」+ 技能名；留下来的个数写在说明行 |
| 恢复已撤下 | 「要恢复的技能」+ 技能名，并写出恢复给哪些助手 |
| 撤销 | 「这次操作动过 N 个技能」+ 技能名，并带上原来那句操作说明 |
| 加减依赖 | 「这次会动到」+ 两个技能名 |

顺带把「撤销」也统一了：以前从提示条上点「撤销」不弹确认框，现在也弹，用户取消时提示条留着不消失。

**实测**

```
分配确认框: 分组标题 ["给 Codex 加上 1 个"]，条目 ["academic-cv-builder"]
20 个技能:  默认显示 8 条 + 「展开其余 12 个」；点开后 20 条，按钮消失
依赖标注:   "skill-0（因依赖自动加上）"
撤销确认框: 标题「撤销这次操作」，条目 ["huashu-nuwa"]
```

## 4. 详情面板「各助手里的入口」

**改法**：从三列表格改成上下排列的列表，每条两行——第一行路径（等宽字体、`word-break: break-all`，可以在任意位置断行），第二行状态（是否链接到统一仓库、哪些助手读这个目录、是否只允许手动调用、名称是否不一致、链接是否失效）。

`~` 缩写同时匹配两种写法：`pathLabel()` 现在从 `/api/table` 和 `/api/scan` 各取一个 home，并给每个 home 补一个 macOS 的另一种写法（`/var/…` ↔ `/private/var/…`、`/tmp/…` ↔ `/private/tmp/…`），再按最长的先匹配。模拟 home 里两个接口给的 home 写法不同（一个 `/var/folders/…`，一个 `/private/var/folders/…`），现在两种都能缩成 `~`。

**实测**

```
入口列表: ["~/.agents/skills/agent-reach  链接到统一仓库 · codex / dsh",
           "~/.claude/skills/agent-reach  链接到统一仓库 · claude",
           "~/.gemini/config/skills/agent-reach  链接到统一仓库 · antigravity"]
路径全部以 ~ 开头: true      路径 word-break: "break-all"
```

## 5. 模式标记只有一个来源

**改法**：`refreshMigrationStatus()` 不再写 `#mode-badge`，只写 `state.writeAllowed` 和迁移/还原那几个按钮的可用状态。标记唯一来源是 `refreshTable()` 里的 `table.write_allowed`。

**实测**（模拟 home 里正好是两种口径打架的场景）

```
/api/status 说 read_only = false   （报告 §7.2 里那条 /var 口径问题）
/api/table  说 write_allowed = true
右上角标记: "可以管理技能"，与 /api/table 一致
```

## 6. 检查更新页

**汇总句**：末尾那个「已是最新」是飞书工具的状态被直接拼在句子后面造成的，现在改成「飞书工具：已是最新。」。数字改成从结果行现算，并用减法兜住剩下的，保证能加总：

```
检查完成：共 96 个技能，有更新 2 个，已是最新 61 个，没查成 0 个，另有 33 个不检查（自制或没有来源）。 飞书工具：已是最新。
2 + 61 + 0 + 33 = 96 ✓
```

如果这次已经更新过一些，会多出「这次已更新 N 个」；有「这一版已看过」的会单独列出——两者都参与加总。

**日期**：`updated_at` 从原始格式换成大白话，同年只写月日，跨年才带年份。

```
before: 上游更新：2026-10-12T10:50:11+08:00
after:  上游更新：10 月 11 日
```

> 说明：这里按用户本机时区换算。上游提交时间是 +08:00 的 10 月 12 日 10:50，换算到本机（PDT）是 10 月 11 日，所以显示「10 月 11 日」。和页面上其他时间（盘点时间、操作记录）口径一致。

**「交给 Codex 合并」**：恢复旧版条件，只在 `local_modified !== false` 时出现；「让 Codex 解释」一直有。

```
reverse-engineer-anything（本机没改过）: 看完整对比 / 让 Codex 解释          ← 没有合并
grill-me（你改过）:                     看完整对比 / 让 Codex 解释 / 交给 Codex 合并
```

## 7. 顺手的小事

- 「Antigravity暂时统计不到用量」→「Antigravity 暂时统计不到用量」（中英文之间加空格）。
- 表头那个星号：我把渲染后的 DOM 整个遍历了一遍，**任何表头里都没有 `*`**（`[...document.querySelectorAll('th')].map(t=>t.textContent).filter(t=>t.includes('*'))` 返回 `[]`）。页面上唯一出现 `*` 的地方是几个技能说明正文里的 Markdown 记号（例如 agent-reach 的 `MUST USE…`），在「技能」列的描述文字里，不在表头。按主控给的第二个选项「换成看得见的说明」处理：DSH 表头加了悬停说明「DSH 和 Codex 共用一个技能目录」，表格下方的小字第一句也改成看得见的「Codex 和 DSH 共用一个技能目录：只给 DSH 的技能，会在 Codex 里自动停用。」——这条规则以前只在勾选之后以小字出现在格子里，现在打开页面就能看到。

## 8. 第二轮验证

照交接文件 §6 重跑了一遍，全部在改动之后。

### 8.1 全量测试

```
$ PYTHONDONTWRITEBYTECODE=1 ~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11 -m unittest discover -q
----------------------------------------------------------------------
Ran 149 tests in 33.316s

OK
```

### 8.2 前端语法

```
$ ~/.nvm/versions/node/v24.21.0/bin/node --check skill_pilot/web/static/app.js
（无输出即通过）
```

### 8.3 静态检查

```
$ grep -n "confirm(\|alert(\|prompt(" skill_pilot/web/static/app.js
无匹配

$ grep -n "http://\|https://" skill_pilot/web/static/index.html skill_pilot/web/static/app.css skill_pilot/web/static/app.js
无匹配

$ grep -n "<style\|style=" skill_pilot/web/static/index.html
无匹配

$ node -e '…三个文件交叉检查…'
index.html id: 95 | 重复: 无
$() 引用的 id: 83 | 缺失: 无
没有样式的 class: 无
```

### 8.4 冒烟（另起的模拟 home，不是主控那个）

```sh
$ FAKE=$($PY -c "import tempfile,pathlib;print(pathlib.Path(tempfile.mkdtemp(prefix='sp-r2-smoke-')).resolve())")
$ PYTHONDONTWRITEBYTECODE=1 $PY scripts/make-fake-home.py "$FAKE"
$ SKILL_PILOT_HOME="$FAKE" BROWSER=true PYTHONDONTWRITEBYTECODE=1 $PY -m skill_pilot ui --json   # 后台
{"pid": 50114, "url": "http://127.0.0.1:51426/?token=…"}

=== 带口令 ===
/            -> 200 text/html; charset=utf-8 12238B
/app.css     -> 200 text/css; charset=utf-8 24634B
/app.js      -> 200 text/javascript; charset=utf-8 81900B
/api/scan    -> 200 application/json; charset=utf-8 271349B
/api/table   -> 200 application/json; charset=utf-8 146318B

=== 不带口令 ===
/ -> 403   /app.css -> 403   /app.js -> 403
```

跑完已 `kill 50114` 关掉。主控在用的那个看板（`http://127.0.0.1:64993/`，pid 10139）自始至终没有被碰过，第二轮结束前又确认了一次它还在。

### 8.5 浏览器逐页复检

还是用无头 Chrome + 远程调试协议跑了全部页面，**0 条控制台异常、0 条 console.error**。

| 项目 | 结果 |
| --- | --- |
| 1024×768 顶部高度 | 页头 109px、表格首行距顶 273px、一屏 8 行 |
| 1440×900 顶部高度 | 同上，一屏 10 行 |
| 表头吸顶 | 未滚 237 → 滚 1400 后 0，第一列仍 `sticky` |
| 640px 窄屏 | 整页横向可滚，第一列 `left=0`，表头仍贴顶 |
| 摘要条 | 1440/1024 都是 4 格一行，勾选前后高度 42→42，行数 1→1，四个格子都没被截断 |
| 分配确认框 | 按助手分组 + 技能名 + 依赖标注 |
| 20 个技能的折叠 | 默认 8 条，按钮「展开其余 12 个」，展开后 20 条 |
| 详情入口列表 | 三行，全部 `~` 缩写，`word-break: break-all` |
| 模式标记 | 与 `/api/table` 一致，`/api/status` 说只读也不影响 |
| 更新汇总句 | 数字能加总，飞书状态单独成句 |
| 更新日期 | 「10 月 11 日」这种大白话 |
| 合并按钮 | 只在「你改过」的技能上出现 |
| 撤销确认框 | 标题 + 技能名都在 |

## 9. 第二轮改动的文件与行数

```
$ wc -l skill_pilot/web/static/index.html skill_pilot/web/static/app.css \
        skill_pilot/web/static/app.js skill_pilot/web/server.py
     283 skill_pilot/web/static/index.html
     800 skill_pilot/web/static/app.css
    1792 skill_pilot/web/static/app.js
     345 skill_pilot/web/server.py

$ git status --short
 M skill_pilot/web/server.py
 M skill_pilot/web/static/app.js
 M skill_pilot/web/static/index.html
?? docs/frontend-redesign-report.md
?? docs/handoff-frontend-redesign.md
?? skill_pilot/web/static/app.css
```

第二轮只动了 `index.html`、`app.css`、`app.js` 三个前端文件（外加本报告）。`server.py` 和第一轮结束时一样，`/app.css` 那一处改动没有再加东西。

## 10. 第二轮之后仍然存在的、需要后端配合的地方

第一轮报告 §7 的五条里，第 2 条（`/api/status` 与 `/api/table` 只读口径不一致）这一轮在前端做了规避（模式标记只认 `/api/table`），但**迁移与还原的按钮仍然看 `/api/status`**，所以在那种未解析的临时路径下会出现「徽标说可以管理、迁移说只读」的组合。真实主目录不受影响，仍然建议后端把 `store.guard()` 里 `Path(path).absolute()` 那处口径统一。

其余四条不变：站点图标 403、预览耗时约 3 秒、更新结果整体重绘、上游日期字段的口径。

---

# 第三轮修改

> 2026-10-11，主控复核第二轮 6 项全过之后提出的第三轮：只修「写入过程中的反馈」——
> 也就是用户最初抱怨的「后台在做事时缺少反馈」。仍然不提交、不改后端；
> 主控开着的看板进程全程没碰，每一轮验证都另起一个模拟 home。
> （第二、三轮期间主控的看板换过一次端口：64993/pid 10139 后来关掉了，第三轮开始是 52318/pid 53148；
> 收尾复查时 52318 那个仍在正常响应 `/api/status`。）

## 1. 写在前面：为什么会「点完 10 秒没反应」

第二轮结束时，确认框点「应用」之后是这样的：

```
confirmDialog(...)  →  await api('/api/assignment/apply', ...)  →  refreshEverything()  →  toast(...)
```

中间这段 10 秒里：确认框已经关了，底部操作栏的「应用」还是原样（能再点），勾选框还能改，
页头任务条没有这一条，所以用户看到的是一个「什么都没发生」的页面。

第三轮把这类写入统一收进一个函数 `runWrite()`，六个写入加上顺带一起改的四个写入都走它。

## 2. 共用函数 `runWrite()`

```js
async function runWrite({button, busyText, taskText = busyText, success, onError, work}) {
  const original = button?.textContent ?? null;
  writeBusy = busyText;                       // 全站知道正在写
  if (button) { button.disabled = true; button.textContent = busyText; button.setAttribute('aria-busy', 'true'); }
  setTask('write', {text: taskText});         // 页头任务条显示同一句话
  const unlock = lockControls(WRITE_LOCK_SELECTORS);
  ... await work() ...
  writeBusy = null; unlock(); 恢复按钮文字; renderBusyAreas();
  失败 → setTask 错误样式 + onError + 红色 sticky 提示，返回 null
  成功 → clearTask + success(result) 里的绿色提示（可带撤销）
}
```

配套三件事：

1. **`WRITE_LOCK_SELECTORS`**：一张全站清单（技能页勾选框、底部操作栏按钮、重新盘点、安装技能、
   清理勾选框、已撤下的恢复/刷新/检查更新、更新列表的勾选框/二选一/按钮/检查更新、
   操作记录撤销与刷新、详情面板的依赖按钮和下拉、收编按钮、安装弹窗里的控件、迁移与还原的全部按钮）。
   `lockControls()` 只记录「本来是能点的」那些控件，写完按原样恢复，不会把关着的东西误开。
2. **`writeBusy`**：各区的渲染函数都会看它。万一写入过程中发生了重画（比如用户在搜索框里打字），
   新画出来的勾选框、按钮仍然是禁用的，不会因为重画而「解锁」。
3. **任务条文案 = 按钮文案**：两边用同一句话。写入是用户按下的，所以立刻出现
   （防闪烁的 700 毫秒只留给后台任务，见第 4 节）；没有具体进度时画一条来回走的细条，
   表示还在做。

落地的十个写入：

| 写入 | 按钮 / 任务条文字 | 触发它的按钮 |
| --- | --- | --- |
| `/api/assignment/apply` | 正在应用… | 底部操作栏「应用」 |
| `/api/undo` | 正在撤销… | 操作记录里的「撤销」或提示条上的「撤销」 |
| `/api/updates/apply` | 正在更新… | 底部操作栏「更新选中的 N 个」/「全部更新」/「只更新这一个」 |
| `/api/cleanup/apply` | 正在清理… | 底部操作栏「清理选中的 N 个」 |
| `/api/withdrawn/restore` | 正在恢复… | 已撤下那一行的「恢复原分配」 |
| `/api/restore` | 正在还原… | 「一键还原到迁移前」/ 备份列表里的「还原这份备份」 |
| `/api/absorb`（顺带） | 正在收编… | 新技能横幅的「一键收编」 |
| `/api/install/apply`（顺带） | 正在安装… | 安装弹窗的「安装选中的技能」 |
| `/api/dependencies`（顺带） | 正在处理… | 详情面板的「添加需要的技能」/「删除」 |
| `/api/migration/apply`（顺带） | 正在迁移… | 「确认迁移」 |

失败时：任务条转成红色错误样式，按钮只给「关闭」**不给「重试」**——
写入失败通常是内容或权限的问题，重试前得先看清原因，和只读操作（刷新、检查更新）不一样。

## 3. 带撤销的成功提示停留 ≥ 20 秒，悬停不消失

- 带「撤销」的提示：20 秒（普通提示仍是 6 秒，错误提示照旧常驻）。
- `autoDismiss()` 在 `mouseenter` / `focusin` 时停掉计时，`mouseleave` / `focusout` 时重新计时，
  所以鼠标停在上面（或键盘焦点在里面）就不会消失。

## 4. 任务条完成后不再占一行

- `finishTask()` 删掉：任务做完**直接消失**，不再显示「用量统计完成」那一行。
- 后台任务（用量统计、检查更新）加了 700 毫秒防闪烁：起任务后 700 毫秒内做完的，
  任务条根本不会画出来，首屏就不会闪一下。
- 写入不加防闪烁（用户按下的，要立刻看到）。

实测：冷启动后连续采样 6 秒，任务条一次都没出现过；
统计结束前后页头高度 109px→109px、表格距顶 236px→236px，没有任何位移。

## 5. 第三轮验证（照交接文件 §6）

### 5.1 全量测试 / 语法 / 静态检查

```
$ PYTHONDONTWRITEBYTECODE=1 …/python3.11 -m unittest discover -q
Ran 149 tests in 37.898s
OK

$ node --check skill_pilot/web/static/app.js
（无输出即通过）

$ grep -n "confirm(\|alert(\|prompt(" skill_pilot/web/static/app.js   → 无匹配
$ grep -n "http://\|https://" index.html app.css app.js                → 无匹配
$ grep -n "<style\|style=" index.html                                  → 无匹配
```

### 5.2 冒烟（另起的模拟 home）

```sh
$ FAKE=$($PY -c "import tempfile;print(tempfile.mkdtemp(prefix='sp-r3-smoke-'))")
$ PYTHONDONTWRITEBYTECODE=1 $PY scripts/make-fake-home.py "$FAKE"
$ FULL=$(cd "$FAKE" && pwd -P)          # /private/var/… 完整路径
$ SKILL_PILOT_HOME="$FULL" BROWSER=true … -m skill_pilot ui --json
{"pid": 71093, "url": "http://127.0.0.1:59928/?token=…"}

/            -> 200 text/html; charset=utf-8 12238B
/app.css     -> 200 text/css; charset=utf-8 24883B
/app.js      -> 200 text/javascript; charset=utf-8 88178B
/api/scan    -> 200 application/json; charset=utf-8 271349B
/api/table   -> 200 application/json; charset=utf-8 146318B
不带口令：/ 403、/app.css 403、/app.js 403
```

跑完 `kill 71093` 关掉。主控在用的看板（第三轮是 `http://127.0.0.1:52318/`，pid 53148）全程没动过，
收尾时复查它仍在正常响应（`{"pid": 53148, "read_only": false}`）。

### 5.3 主控要求的那条实测（无头 Chrome + 模拟 home，用 `cd "$FAKE" && pwd -P` 的完整路径启动）

启动出来的 `/api/status` 是 `read_only: false`、`/api/table` 是 `write_allowed: true`，
两边一致（不是只读）。

```
=== 0. 首屏不该出现「用量统计完成」一行 ===
  首屏 6 秒内任务条出现过: []
OK   没有「完成」那一行
  页头高度 109px，表格距顶 236px
OK   统计结束后页头/表格没有位移（109/236 → 109/236）

=== 1. 应用分配的写入反馈 ===
OK   待应用改动出现: "从 Antigravity 撤下 1 个"
OK   确认框已打开
  写入中（0.4 秒时）: {"buttons":["取消(禁用)","正在应用…(禁用)"],
                      "checkboxes":237,"disabledChecked":237,
                      "task":"正在应用…","hasBusyBar":true}
OK   按钮文字是「正在应用…」          ← 1 秒内
OK   操作栏按钮全部禁用
OK   勾选框被禁用（237/237）
  写入中（1.2 秒时）: {"task":"正在应用…","hasBusyBar":true}
OK   任务条显示同一句话
OK   任务条有进行中的细条

=== 2. 完成后的提示停留时间 ===
OK   出现成功提示 / 文字「分配已经应用」/ 带撤销按钮
OK   写入后控件恢复
OK   鼠标悬停 5 秒后仍在（悬停不消失）
OK   提示仍在（等待期间）

=== 3. 从提示撤销 ===
OK   提示上有撤销按钮 → 确认框 → 点「撤销」
  撤销中（0.4 秒时）: {"undoButton":["正在撤销…(禁用)"],"task":"正在撤销…",
                      "skillBoxesDisabled":520}
OK   撤销按钮显示「正在撤销…」
OK   任务条显示「正在撤销…」
OK   原来那条带撤销的提示消失
OK   换成「已经撤销这次操作」
OK   撤销后控件恢复

=== 4. 提示停留 ≥ 20 秒 ===
OK   提示自己消失用了 20.086 秒（要求 ≥ 20）

=== 异常 (0) ===
(无)
```

### 5.4 另外补的几条（同一个模拟 home）

| 场景 | 结果 |
| --- | --- |
| 失败路径（模拟 home 里给 Codex 加 academic-cv-builder 会被后端挡下） | 任务条「Codex 的实际技能名单与勾选结果不一致…」红色样式 + 「关闭」，**没有「重试」**；红色提示同时在；控件全部恢复可点；点「关闭」任务条消失 |
| 已撤下的技能「恢复原分配」 | 任务条与按钮都显示「正在恢复…」；清理页的勾选框同时禁用；完成后提示「已经恢复原来的分配」 |
| 更新页写入（用网络延迟把请求拉长到可观察） | 任务条与触发按钮都显示「正在更新…」；更新列表的勾选框、二选一、「只更新这一个」、「检查更新」全部禁用；失败后恢复 |
| 无写入时的清理页 | 勾选框正常可点（没有过度锁定） |
| 首屏防闪烁 | 前 600 毫秒任务条是空的；结束后也是空的 |

## 6. 第三轮改动的文件与行数

```
$ wc -l skill_pilot/web/static/index.html skill_pilot/web/static/app.css \
        skill_pilot/web/static/app.js skill_pilot/web/server.py
     283 skill_pilot/web/static/index.html
     809 skill_pilot/web/static/app.css
    1949 skill_pilot/web/static/app.js
     345 skill_pilot/web/server.py

$ git status --short
 M skill_pilot/web/server.py
 M skill_pilot/web/static/app.js
 M skill_pilot/web/static/index.html
?? docs/frontend-redesign-report.md
?? docs/handoff-frontend-redesign.md
?? skill_pilot/web/static/app.css
```

第三轮只动 `app.js` 和 `app.css`。`index.html`、`server.py` 与第二轮结束时完全一样，
后端接口一行没改。

## 7. 踩到并修掉的两个坑（留给以后看）

1. **重画会把忙碌按钮换掉**。一开始 `runWrite()` 在开头先 `renderBusyAreas()` 把各区重画一遍，
   结果各区的渲染函数重建了底部操作栏的按钮，刚设成「正在应用…」的那个节点被丢掉，
   页面上还是「应用」。改成：写入开始时**不重画**，禁用交给 `lockControls()`；
   只有中途真的发生重画时，各区才靠 `writeBusy` 自己保持禁用。
2. **更新页在写入前先重画了一次**。`applyUpdates()` 里原本有
   `updateState.busy = true; renderUpdates();`，同样会把点击时拿到的按钮节点换掉。
   改成只置标记不重画，重画在写完以后做。
   （其余九个写入没有这个问题，因为它们在 `runWrite` 之前不重画。）

## 8. 第三轮之后仍然存在的取舍

- **任务条出现/消失时，下面的内容会上下移动约 34px**（它在文档流里）。这是有意的：
  长任务跑几分钟，进度条应该把内容顶下去而不是盖住工具条。主控提的「完成那一行」已经去掉，
  所以一次任务只跳两次（起、止），不再有「完成」那次多余的闪动。
- **失败的任务条会一直留着**，直到用户点「关闭」或者发起下一次写入（下一次写入会顶掉它）。
  红色提示本来也常驻，两者说的是同一句话。
