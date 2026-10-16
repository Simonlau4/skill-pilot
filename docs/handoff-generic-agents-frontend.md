# 交接：勾选不锁页面 + 页面通用化（前端）

> 2026-10-13 由 Claude（主控）派给 DSH。先读本仓库 `AGENTS.md`、`docs/STATUS.md`，再读本文件；后台接口以 `docs/generic-agents-report.md` 第 4 节为准（上一轮 Codex 已做完后台）。用户已在对话里确认本方案。

## 0. 工作位置与边界

- 你在 git worktree `~/Developer/skill-pilot-generic-agents`（分支 `generic-agents`）里工作，沙箱只允许写这个目录和系统临时目录。用户双击打开的真实看板跑的是 `~/Developer/skill-pilot`（main），不要去碰。
- 工作区里**已暂存（staged）的改动是上一轮后台的成果**，在它上面继续。**不要 `git add`、`commit`、`reset`、`checkout`、`stash`，不切分支、不改 git 配置**；你的改动留在未暂存状态，主控审阅。
- **只改 `skill_pilot/web/static/` 下的 `index.html`、`app.js`、`app.css`**，外加你写的报告。后台（Python）有问题或缺字段，不要自己改，写进报告，主控转给 Codex。
- 不对真实 home 运行本工具，不读写 `~/.skill-pilot/`，不运行 `scripts/build-app.sh`、`skill-pilot` 命令行。需要后台数据时用 `scripts/make-fake-home.py` 生成的模拟 home（§6）。
- 不引入框架、构建工具和第三方包，保持三个文件的结构和现有写法（分段注释、`$()`、`escape()`、`api()`、`confirmDialog()`、`runWrite()`、`toast()`）。
- 页面上给用户看的文字一律中文大白话，不出现 symlink、diff、git、TOML、hash、revision 这类词。
- 你没有浏览器，视觉验收由主控在浏览器里做；你负责逻辑正确、`node --check` 通过、接口对得上。

## 1. 背景与用户确认的方案

1. **勾选卡顿**：现在每点一个勾选框，页面调 `/api/assignment/preview`，等待期间（原来 2.5 秒）锁住所有勾选框并在页头显示“正在核对…”。后台已经提速到约 0.02 秒。用户确认：**保留“勾完统一按应用”**，但勾选本身任何时候都不能锁、不能等；改了还没生效的格子要一眼看得出来。
2. **写死四个 agent**：页面里写死了四个 agent 的编号、名字、顺序、生效方式、“Codex 和 DSH 共用目录”等说明、迁移页的四个选项、页头固定四格排版。用户要求做成通用看板：agent 的一切信息都从接口来，页面不认识任何具体 agent；agent 数量 1～8 个都要排得好；能在页面上接入新 agent、不再管理某个 agent。
3. **用词**：页面上所有“助手”统一改成 “agent”（用户确认）。中文和 agent 之间留一个半角空格，如“没分配给任何 agent”“发现新 agent”。

## 2. 任务 A：勾选不锁页面

- 勾选框只在“正在写入”（`writeBusy`）或只读模式时禁用；去掉 `previewBusy` 对勾选框的禁用和 `change` 事件里的提前返回。
- 点一下：立刻更新草稿、重画表格，再发预览请求。连续快速点时合并请求（短暂防抖，约 100 毫秒），只认最后一次请求的结果（沿用 `previewSequence`）。去掉页头“正在核对这次勾选带来的改动…”任务条。
- 预览返回里已经**没有 `revision`**，删掉对它的比较（应用时后台照旧校验）。
- 预览失败：只撤回最后这一次勾选，在那一格显示原因（沿用 `assignmentErrors` 的样子），其余草稿保留。
- 依赖自动带上：改用返回里的结构化 `auto_added: [{skill, agent, because}]`，删掉 `REASON_PATTERN`、`reasonText`、`dependencySource` 这类从句子里反解 agent 名字的代码。格子说明“因为 X 需要它”里的 X 用中文名（`titleOf`）。
- 格子说明：有草稿时用预览返回的 `cell_notes[技能][agent]`，没草稿时用 `/api/table` 技能行的 `cell_notes[agent]`，原样显示。删掉写死的“Codex 里已自动停用”判断。
- **未生效标记**：草稿和已应用不同的格子要有明显但安静的标记（比如浅色底 + 小字“待应用”；依赖自动带上的写“自动带上”）。组标题行的数字照旧按草稿算。
- 底部操作栏：主句写“改了 N 处，按「应用」后才生效”，副句放预览的 `message` 和依赖说明。“应用”只在有预览请求未返回或正在写入时禁用（预览很快，用户基本感觉不到）。
- “应用”之后的流程不变：确认框 → `runWrite()`（写入时锁定是合理的）→ 带撤销的提示。

## 3. 任务 B：页面通用化

### 3.1 去掉写死

- 删掉 `AGENT_ORDER`、`AGENT_NAMES`、`activationOf`、`MIGRATION_AGENTS`，以及所有按 `'codex'`、`'dsh'` 等编号的分支。
- agent 的名字、顺序、生效方式（`activation_text`）、用量能否统计（`usage_available`、`usage_note`）全部来自 `/api/table` 的 `agents`（顺序就是接口给的顺序）。
- 需要把 agent 编号翻成名字、但该 agent 不在当前表里的地方（操作记录的 `agents`、清理页的 `previous_agents` 等），用 `/api/agents` 的 `managed`、`suggested` 和表里的名字建一个查找表，查不到就显示编号本身。
- 技能页脚注：用 `/api/table` 的 `notes` 句子代替写死的“Codex 和 DSH 共用一个技能目录…”；“四个助手的用量都统计不到”改成不带数量的说法。
- 技能详情：用量表按 `state.agents` 顺序；“只允许手动调用”用扫描条目的 `manual_only_for`（agent 编号数组）写成“{名字} 只允许手动调用”；停用用 `/api/scan` 的 `disabled[agent].names` 写成“{名字} 已停用”。
- 诊断页：`codex_disabled` → `disabled`，`codex_implicit_invocation` → `manual_only_for`，目录的 `readers` 编号翻成名字。
- 清理页：覆盖说明、可省字数按 `state.agents` 顺序列出，不再用写死顺序。
- 页头副标题改成“本机各 agent 的技能，都在这里管”。

### 3.2 排版适应 agent 数量

- 页头摘要：去掉“固定 4 列”，改为按数量自动换行的网格（每格最小宽度够放“名字 + N 个技能 · 说明 N 字”两行），1 个到 8 个都整齐；窄屏规则相应调整。
- 技能表：agent 列数随数量变；列多时表格横向滚动，技能名一列保持固定在左侧（已有 sticky 样式，确认在 7～8 列时仍然好用），agent 列宽可以适当收窄。

### 3.3 设置页新增「agent」一栏（放在 AI 服务之前）

数据来自 `GET /api/agents`。

- **正在管理的 agent**：每个一行，显示名字、技能目录（用 `pathLabel`）、生效方式、用量情况（能统计 / `usage_note`）、和谁共用目录（`shared_with`）。右侧“不再管理”按钮（`can_leave` 为假时禁用并说明“至少要留一个”）。
  - 点“不再管理” → `POST /api/agents/leave/preview` → 确认框：写 `message`；`lost_skills` 非空时列出“{名字} 以后看不到这几个技能”；说明“它目录里的技能原样留着、照常能用，以后可以重新接入” → `runWrite()` 调 `leave/apply` → 带撤销的提示 → 刷新全部数据。
- **发现的新 agent**（`suggested`）：每个一行，显示名字、技能目录、技能数、`reason`。按钮“接入”和“忽略”。
  - “忽略”不弹确认框：直接调 `ignore/preview` 再调 `ignore/apply`，提示条里给撤销。
  - “接入” → `POST /api/agents/join/preview`（`agents` 里带上它，以及 `must_select_together` 里和它一组、还没被管理的 agent）→ 打开接入核对框（新建一个 `<dialog>`，样式参照安装技能那个）：
    - 顶部一句话：会接入哪几个 agent、各自的技能目录；
    - 按 `action` 分类计数：已经在用统一仓库（`linked`）、内容一致改成统一管理（`link`）、收进统一仓库（`import`）、内容不同要你选（`choose`）、原样留着（`skipped`）；
    - `choose` 的每一行：单选“用统一仓库里的（默认）/ 用这个 agent 里的”，旁边“看看差别”展开 `comparison`；选后者时提示“会更新所有 agent 用的这份”；
    - 按“接入”→ `runWrite()` 调 `join/apply`（带 `plan_id`、`revision`、`choices`）→ 带撤销的提示 → 刷新全部数据。
- 没有已管理的 agent（还没首次迁移）时，这一栏只写一句“还没完成首次迁移”，指向诊断页的迁移。

### 3.4 其他入口

- 技能页顶部：`suggested` 非空时显示一条横幅“发现新 agent：Trae、Zcode”，按钮“去看看”跳到设置页的 agent 一栏。样式参照现有“发现新技能”横幅。
- 诊断页“迁移与还原”：已经完成首次迁移时，第 1、2 步（选 agent、核对版本）收起，只写“已经完成首次迁移；以后接入新 agent 请到设置里的 agent 一栏”，保留“已有备份”。还没迁移时，第 1 步的 agent 勾选项从 `/api/agents` 的 `suggested` 来，`must_select_together` 里同组的勾一个就一起勾上（代替写死的 codex/dsh 联动），说明文字从分组生成。
- `refreshEverything()` 之类的刷新要带上 `/api/agents`；写入锁定的选择器（`WRITE_LOCK_SELECTORS`）加上新按钮和新对话框。

### 3.5 用词替换

`index.html`、`app.js` 里所有用户可见的“助手”改成 “agent”（含“疑似新助手”“分配给谁”等提示、确认框、空状态文字）。后台报错文字里的“助手”由 Codex 另行替换，你不用管。

## 4. 不做

- 不改 Python、不改接口。
- 不做“手动填目录添加 agent”，不做“让 Codex 查一下”按钮。
- 不改检查更新、清理、操作记录页的功能，只换掉其中写死的 agent 和用词。
- 不新增其他视觉改版。

## 5. 验收（全部做完再交）

1. `node --check skill_pilot/web/static/app.js` 通过。
2. 以下检查没有任何命中：
   ```sh
   grep -nE "codex|claude|dsh|antigravity|Codex|Claude|DSH|Antigravity|助手|四个" skill_pilot/web/static/index.html skill_pilot/web/static/app.js skill_pilot/web/static/app.css
   ```
3. 后台测试不受影响：`PYTHONDONTWRITEBYTECODE=1 <解释器> -m unittest tests.test_web tests.test_m5_web tests.test_generic_agents_web -q` 通过（全量测试约 45 秒，可能超过你的命令时限，主控会跑全量）。
4. 用模拟 home 启动后台，按 §6 用请求核对页面依赖的每个接口字段都存在、名字对得上（`/api/table`、`/api/agents`、`/api/assignment/preview`、`/api/agents/join/preview`、`/api/agents/leave/preview`、`/api/scan`），把核对结果写进报告。
5. 写报告 `docs/generic-agents-frontend-report.md`：改了什么（按 §2、§3 逐条）、删掉了哪些写死、新增的交互流程、接口核对的原始输出、没做或做不到的地方、需要后台补的地方。主控会在浏览器里逐项验收（含 2、4、7 个 agent 的排版）。

## 6. 模拟 home

```sh
PY=~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11
FAKE=$(mktemp -d)/home
PYTHONDONTWRITEBYTECODE=1 $PY scripts/make-fake-home.py "$FAKE"
FULL=$(cd "$FAKE" && pwd -P)    # 必须用 /private/var/... 形式，否则会被判成只读
# 后台放到后台运行；--json 会打印 url（地址里带 token 参数，就是口令）；30 秒没有页面心跳会自己退出，需要时定时 POST /api/heartbeat?page=任意
SKILL_PILOT_HOME="$FULL" BROWSER=true PYTHONDONTWRITEBYTECODE=1 $PY -m skill_pilot ui --json
```

请求头带 `X-Skill-Pilot-Token: <口令>`，POST 还要带 `Origin: http://127.0.0.1:<端口>` 和 `Content-Type: application/json`。用户数据的模拟副本里有 Trae、Zcode 两个可接入的新 agent。
