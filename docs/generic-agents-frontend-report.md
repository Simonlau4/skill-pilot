# 报告：勾选不锁页面 + 页面通用化（前端）

> 2026-10-13/14 由 DSH 执行，对应交接文件 [handoff-generic-agents-frontend.md](handoff-generic-agents-frontend.md) 的任务 A、任务 B 与第 5 节验收。工作区 `~/Developer/skill-pilot-generic-agents`（分支 `generic-agents`），只改了 `skill_pilot/web/static/` 下的三个文件和本报告，没有动 Python，没有做任何 git 写操作。

## 1. 结果一览

| 验收项 | 结果 |
| --- | --- |
| 1. `node --check skill_pilot/web/static/app.js` | 通过 |
| 2. 写死词与“助手/四个”检查（三个文件） | 只剩检查更新页的两个 Codex 按钮文字（见 §5 的 F2，主控已放行） |
| 3. `tests.test_web + tests.test_m5_web + tests.test_generic_agents_web` | 17 项全部通过（第一轮 4.5 秒，第三轮 4.9 秒） |
| 3b. 全量 `unittest discover -q` | 第一轮 184 项、第三轮 185 项，全部通过（第三轮 53 秒） |
| 4. 模拟 home 上逐个接口核对字段 | 见 §4 与文末附录的原始输出 |
| 5. 本报告 | 本文 |
| 浏览器视觉验收 | 不在我的环境里，交给主控（第一轮已由主控验过；F1～F7 见 §7，G1～G3 见 §8） |

另外补了两层自动核对（不引入任何依赖，脚本放在系统临时目录，不进仓库）。最后一遍是**新开一个模拟 home、夹具全部按当时的真后台重新生成**之后跑的：

- **页面逻辑真跑一遍**：用 Node 的 `vm` 造一个最小 DOM + 假网络，把 `app.js` 原样跑起来，效果等同于把页面打开（区别只是没有真实绘制），表与预览数据用 `/api/table`、`/api/assignment/preview` 的真实返回。覆盖 24 项：agent 列表/名字/生效方式来自接口、表头与脚注、新 agent 横幅、勾选立刻改草稿且不锁、连续两次快速勾选只发一次预览、预览失败只撤回最后这一次（其余草稿保留）、自动带上/待应用标记、底部操作栏文案、编号翻名字、诊断页、设置页 agent 一栏、迁移第 1、2 步收起与未迁移时的勾选项。`24/24 通过`。
- **接入新 agent 端到端（真写入真后台）**：接入 Trae（含两处“内容不同要你选”）→ 撤销 → 忽略建议 → 撤销 → 不再管理 DSH，每一步都核对接口返回与页面状态。`17/17 通过`。这一轮在模拟 home 的 `~/.skill-pilot/logs/` 里留下了「接入了 Trae / 撤销：接入了 Trae / 忽略 Zcode 的接入建议。 / 撤销：忽略 Zcode…」等记录（都是模拟 home，不是真实数据）。

## 2. 任务 A：勾选不锁页面（逐条对照）

| 交接要求 | 做法 |
| --- | --- |
| 勾选框只在写入或只读时禁用 | `skillRowHtml()` 里 `const locked = Boolean(writeBusy) \|\| !state.table.write_allowed;`，`previewBusy` 不再参与；`change` 事件里的提前返回也去掉了 |
| 点一下立刻更新草稿并重画，再发预览 | `change` 处理里先改 `state.draft`，同步 `renderSkillTable() / renderAgentSummary() / renderSkillsActionBar()`，然后 `schedulePreview()` |
| 连续快点合并请求（约 100ms），只认最后一次 | 新增 `PREVIEW_DEBOUNCE_MS = 100` 与 `previewTimer`；`schedulePreview()` 重置定时器，`runPreview()` 用 `previewSequence` 判断结果是否过期 |
| 去掉页头“正在核对…”任务条 | 不再调用 `setTask('assign', …)`，`previewBusy` 只用来禁用“应用”按钮 |
| 删掉对预览 `revision` 的比较 | 原代码 `result.revision !== state.table.revision` 那一句已删除（后台预览本来就不返回 `revision`，那句会让每次勾选都失败） |
| 预览失败只撤回最后这一次勾选，并显示原因 | `state.pendingClick` 记下最后一次勾选的技能、agent、方向和请求序号；失败且序号仍是最新时，把这一个格子改回原状，写 `assignmentErrors[技能\|agent] = '这次勾选没改成：' + 原因`，其余草稿保留 |
| 依赖自动带上改用结构化 `auto_added` | 新增 `state.autoAdded`（`技能\|agent` 的集合，来自 `result.auto_added`）；删掉 `REASON_PATTERN`、`reasonText()`、`dependencySource()`；确认框里“因依赖自动加上”由 `autoAddedSkill()` 判断 |
| 格子说明用接口的 `cell_notes` | `cellNote()`：有草稿时读 `state.pending.cell_notes[技能][agent]`，没草稿时读技能行的 `cell_notes[agent]`，原样显示；写死的“Codex 里已自动停用”判断删掉了 |
| 未生效标记 | 草稿与已应用不同的格子加 `cell-pending`（浅色底），下面一行小字“待应用”；依赖自动带上的写“自动带上”（绿色）。组标题行的数字仍按草稿算 |
| 底部操作栏文案 | 主句固定 `改了 N 处，按「应用」后才生效`；副句放预览的 `message` 加 `dependency_warnings`；“应用”只在 `previewBusy` 或 `writeBusy` 或只读时禁用 |
| “应用”之后流程不变 | `applyAssignment()` 仍是 确认框 → `runWrite()`（写入期间锁定是应当的）→ 带撤销的提示 |

接口返回的 `auto_added`、`cell_notes`、`message`、`dependency_warnings`、`path_warnings` 都用上了；`reasons` 不再需要，页面不再解析句子。

## 3. 任务 B：页面通用化（逐条对照）

### 3.1 去掉写死

删掉的东西：`AGENT_ORDER`、`AGENT_NAMES`、`agentName()` 里的固定表、`activationOf()`、`MIGRATION_AGENTS`，以及所有按 `'codex'`/`'dsh'` 分支的代码（迁移页的联动勾选、技能表头的 DSH 提示、详情页的“Codex 里停用了”、诊断页的 `codex_disabled`/`codex_implicit_invocation`、清理页的固定顺序、摘要条的固定四格）。

现在名字和顺序的来源只有两处：

- `/api/table` 的 `agents`：页头摘要、技能表表头、技能详情用量表、清理页覆盖说明与“可省字数”，顺序就是接口给的顺序；
- `/api/agents` 的 `managed`/`suggested`：`state.catalog` 查名字，`nameOf(id)` 先查它、再查表里的名字、最后退回编号本身。操作记录的 `agents`、清理页的 `previous_agents`、已经不在表里的 agent 都靠这个兜底。

其余替换：

- 技能页脚注：用 `/api/table` 的 `notes` 句子（现在会显示“Codex 和 DSH 共用一个技能目录……”）；统计不到的 agent 用 `usage_note` 说明；“四个助手的用量都统计不到”改成“各 agent 的用量都统计不到”。
- 技能详情：用量表按 `state.agents` 顺序；`manual_only_for`（编号数组）翻成“{名字} 只允许手动调用”；停用按 `/api/scan` 的 `disabled[agent].names` 翻成“{名字} 已停用”；`entries` 里的 `readers` 也翻成名字。
- 诊断页：`codex_disabled` → `disabled`、`codex_implicit_invocation` → `manual_only_for`、`readers` 编号翻名字。
- 清理页：覆盖说明和可省字数按 `state.agents` 顺序，接口里多出来的 agent 补在后面。
- 页头副标题：“本机各 agent 的技能，都在这里管”。

### 3.2 排版适应 agent 数量

- 页头摘要从 `repeat(4, …)` 改成 `repeat(auto-fit, minmax(190px, 1fr))`，1 到 8 个都自己换行，每格仍是固定两行（第二行预留，勾选前后不跳）；窄屏用 `minmax(170px, 1fr)`。
- agent 列：`.cell-check` 从 96px 收到 84px（列多时不至于太挤）；表格本身的 `#skills-table { min-width: 820px }` 和技能名一列 `position: sticky; left: 0` 都保留，7～8 列时表格超出窗口宽度、由整页横向滚动，技能名一列固定在左边。我没有浏览器，**7～8 列的实际观感要主控在浏览器里过一眼**。

### 3.3 设置页新增「agent」一栏（放在 AI 服务之前）

内容全部来自 `GET /api/agents`：

- **正在管理的 agent**：一行一个，显示名字、技能目录（`pathLabel` 把 home 换成 `~`）、生效方式（`activation_text`）、用量情况（能统计 / `usage_note`）、和谁共用目录（`shared_with` 翻名字）。右侧“不再管理”；`can_leave` 为假时按钮换成“至少要留一个”的说明。
- **不再管理**：`POST /api/agents/leave/preview` → 确认框写 `message`，`lost_skills` 非空时按“{名字} 以后看不到这几个技能”列出，说明“它目录里的技能原样留着、照常能用，以后可以重新接入” → `runWrite()` 调 `leave/apply`（带 `agent, plan_id, revision, confirmed`）→ 带撤销的提示 → `refreshEverything()`。
- **发现的新 agent**：显示名字、技能目录、技能数、`reason`。按钮“接入”和“忽略”。
  - 忽略：不弹确认框，先 `ignore/preview` 再 `ignore/apply`，提示条里给撤销。
  - 接入：`POST /api/agents/join/preview`，`agents` 传 `must_select_together` 里和它同组、还没被管理的编号（`joinIds()`）→ 打开新的 `<dialog id="agents-join-dialog">`（样式参照安装技能那个）：
    - 顶部一句写清会接入哪几个 agent 和各自的技能目录；
    - 按 `action` 分类计数：已经在用统一仓库（`linked`）/ 内容一致改成统一管理（`link`）/ 收进统一仓库（`import`）/ 内容不同要你选（`choose`）/ 原样留着（`skipped`）；
    - `choose` 的每一行是一个可展开的小块：单选“用统一仓库里的（默认）”和“用这个 agent 里的”，旁边“看看差别”展开 `comparison`；勾后者时提示“选了这一份，会更新所有 agent 用的这份技能。”；
    - 按“接入”→ `runWrite()` 调 `join/apply`（带 `agents, plan_id, revision, choices, confirmed`）→ 带撤销的提示 → 刷新全部数据。
- 还没首次迁移（`managed` 为空）时，这一栏只写“还没完成首次迁移……”，并给一个跳到诊断页“迁移与还原”的链接。

### 3.4 其他入口

- 技能页顶部：`suggested` 非空时显示蓝色横幅“发现新 agent：Trae、Zcode”，按钮“去看看”切到设置页。
- 诊断页“迁移与还原”：已经完成首次迁移时，第 1、2 步收起，只留一句“已经完成首次迁移；以后接入新 agent，请到设置里的 agent 一栏”和“已有备份”；还没迁移时，第 1 步的勾选项从 `/api/agents` 的 `suggested` 来（取不到时退回迁移计划的 `agent_catalog`），`must_select_together` 里同组的勾一个就一起勾上，说明文字由分组生成。
- `refreshEverything()` 现在也刷 `/api/agents`（这一项失败只弹提示，不挡其他刷新）；`WRITE_LOCK_SELECTORS` 加了 `#agents-managed button`、`#agents-suggested button`、`#agents-join-dialog input`、`#agents-join-dialog button`，`renderBusyAreas()` 不需要重画这些区（它们不在里面）。
- 检查更新页里两个写死的按钮文字（“让 Codex 解释”“交给 Codex 合并”）改成“让 agent 解释”“交给 agent 合并”。这是这次唯一一处碰到检查更新页文字的地方，功能没动；不改的话验收第 2 条会一直命中。

### 3.5 用词替换

`index.html`、`app.js`、`app.css` 里所有用户可见的“助手”都改成了 “agent”（含“疑似新助手”“没分配给任何助手”“每个助手各自的情况”等提示、确认框、空状态、脚注、注释）。中文和 agent 之间留一个半角空格。

## 4. 接口核对（模拟 home）

按交接 §6 用 `scripts/make-fake-home.py` 建模拟 home，后台用 `SKILL_PILOT_HOME=<模拟 home> python3.11 -m skill_pilot ui --json` 起，请求带 `X-Skill-Pilot-Token` 和 `Origin`。这一轮的模拟 home 里：4 个已管理 agent（codex / claude / antigravity / dsh）、2 个建议接入（trae / zcode）、`must_select_together = [['codex','dsh']]`；为了让“内容不同要你选”这条真的出现，把 trae 目录里的 `lark-approval`、`lark-base` 换成了两份额外内容（只动模拟 home）。

核对结论：**页面依赖的每个字段都在，名字对得上**。原始输出见文末附录。逐项：

| 接口 | 页面用的字段 | 核对结果 |
| --- | --- | --- |
| `GET /api/table` | `home, revision, write_allowed, skills, agents, notes, new_skills, groups` | 都在。`agents[]` 的 `id/name/activation_text/usage_available/usage_note/count/description_chars` 都在；技能行的 `assigned_agents/requires/cell_notes/last_used/title/summary/description_chars/local_modified` 都在；`notes` 是一句共用目录提醒，页面直接显示 |
| `GET /api/agents` | `managed/suggested/ignored/must_select_together/revision` | 都在。`managed[]` 带 `skill_roots/activation_text/usage_available/usage_note/shared_with/can_leave`；`suggested[]` 带 `skill_roots/skill_count/reason` |
| `POST /api/assignment/preview` | `changed, reasons, auto_added, dependency_warnings, path_warnings, message, assignments, after, cell_notes` | 都在；**没有 `revision`**（已按此删掉比较）；`auto_added = [{skill, agent, because}]` 结构如文档 |
| `POST /api/agents/join/preview` | `plan_id, revision, agents, profiles, skills, conflicts, skipped, roots, before, must_select_together, read_only` | 都在。`skills[]` 有 `action/default/options/reason/comparison/entries/assigned_agents`；`options[]` 是 `id/path/origin/digest`，仓库选项 `id = "store"`，`choose` 行有两条 |
| `POST /api/agents/leave/preview` | `plan_id, revision, agent, name, remaining, lost_skills, changed, message` | 都在；`_registry` 这样的内部字段没有出现 |
| `POST /api/agents/ignore/preview` | `agent, revision, message, plan_id` | 都在 |
| `GET /api/scan` | `disabled, roots[].readers/skills[].manual_only_for` | 都在；**没有 `codex_disabled`**、条目里也**没有 `codex_implicit_invocation`** |

写入类的接口（`join/apply`、`leave/apply`、`ignore/apply`、`undo`）在这次核对里真的在模拟 home 上跑通了：每次都返回带 `id` 的写入结果，用这个 `id` 撤销后状态回到原样（详见 §1 的端到端脚本）。

## 5. 与后台对接时发现、需要主控转给 Codex 的地方

1. **`/api/agents` 比 `/api/table` 慢很多**：`table`、`scan` 约 10 毫秒，`agents` 在模拟 home 上要 0.6～2.7 秒（`scan()` + `suggestions()` 每次重算）。页面第二轮已按 F5 改成不阻塞（见 §7），但接口本身仍然慢，如果以后要频繁刷新设置页，这里值得缓存。**不是必须改**，记下来备查。
2. **`ignored` 换成对象数组**这一轮后台已经落地，页面两种形状都兼容（见 §7 的 F4）。
3. **已忽略的 agent 再点忽略**会返回 409 `这个 agent 已不在建议里，请刷新。`——页面按普通错误提示处理，符合预期。
4. **后台文字里的“助手”**：第一轮核对时后台给的句子还带着“助手”（附录里就是那次的原样输出，例如 `notes` 的“Codex 和 DSH 共用一个技能目录：……”）；页面对这类句子一直只是原样显示、不从中反解任何东西。到第二轮时后台只剩 `labels.py` 里给 AI 服务的那句提示词还带“助手”，界面上已经见不到。
5. 我这边跑测试期间没有因为这类文字失败：`tests.test_web`、`tests.test_m5_web`、`tests.test_generic_agents_web` 和全量测试都是通过的（第二轮 185 项）。

## 6. 没做 / 做不到 / 需要主控补的

- **浏览器视觉验收**：我没有浏览器。7～8 列时的横向滚动与技能名固定列、1 个 agent 时的摘要条、接入核对框在窄屏下的观感，请主控按交接第 5 节在浏览器里逐项看。
- **1 个 / 2 个 / 7 个 / 8 个 agent 的排版**：我只做到了“样式与数据都不依赖具体数量”，没有在浏览器里真的摆出 8 个 agent。模拟 home 里是 4 + 2 个建议。
- 没有做交接 §4 明确不做的三件事（不改 Python、不做手动填目录、不做“让 Codex 查一下”按钮）。
- 检查更新、清理、操作记录三页的功能没动，只换了其中写死的 agent 和用词。
- 我的核对脚本都在系统临时目录（`/tmp/spfront/`），没有进仓库，也没有引入任何依赖或构建工具。
- 全程没有对真实 home 运行本工具，没有读写 `~/.skill-pilot/`，没有运行 `scripts/build-app.sh` 和 `skill-pilot` 命令行；没有 `git add/commit/reset/checkout/stash`。

## 7. 第二轮修改（主控浏览器验收后的 F1～F7）

> 主控在工作区里把上一轮成果暂存成基础后派来的七处修改。同样只改 `skill_pilot/web/static/` 三个文件和本报告，没有 git 写操作，没有对真实 home 运行。

### F1 勾选丢失（点第一个勾后 100～130 毫秒再点第二个）

**原因**：`change` 事件没有让在途的预览请求作废，`runPreview()` 成功时无条件 `state.draft = result.assignments`，旧请求的返回会盖掉期间的新勾选。

**改法**：新增 `invalidatePreview()`（`++previewSequence`），每次勾选立刻调用，再记 `state.pendingClick`（序号跟着新值走）；`runPreview()` 不再自己加序号，而是**认领**当前序号（`const sequence = previewSequence`）。这样任何在途请求返回时序号已经变了，直接作废，`finally` 里也不会去动忙碌态。

**回归**：新建最小 DOM 回归脚本，按 10 / 60 / 100 / 130 / 200 / 1000 毫秒六种间隔连点两个格子，每个间隔都核对“草稿里两个勾都在、预览结果里两个都在、表格里两个格子都勾着”。预览响应加了 60 毫秒模拟往返，保证“第一次请求还在路上时点第二个格子”这个时序真的发生。
- 修复后：`13/13 通过`。
- 把代码临时还原成旧写法（只回退 `invalidatePreview` 这两处）再跑：**100 和 130 毫秒两档失败**（第二个勾被覆盖掉），其余通过，`9/13`。说明这条回归确实抓得住原 bug。

### F2 恢复检查更新页的两个按钮文字

“让 agent 解释 / 交给 agent 合并”改回“让 Codex 解释 / 交给 Codex 合并”，并在那一行加了注释说明这两件事实际是 Codex 这个工具做的、和被管理的 agent 无关，免得下一轮又被当成写死去替换。§5 的 grep 命中从 0 变成 2 行按钮文字（外加我自己写的那行注释），其余仍然零命中。

### F3 接入核对框的目录显示

新增 `rootsLabel(roots)`：接口给的技能目录是相对 home 的，先拼上 home 再交给 `pathLabel`，所以现在显示成 `Trae（~/.trae/skills）`。设置页两处同样的拼法也换成这个帮手，避免以后再写错。核对脚本直接调用 `renderJoin()` 检查摘要里是 `~/.trae/skills` 且不再出现 `（.trae/skills）`。

### F4 已忽略的 agent 也列在设置页

后台这一轮已经把 `/api/agents` 的 `ignored` 换成对象数组（带 `id/name/skill_roots/skill_count/reason`），我在真后台上核对到了，例如：

```json
[{"id":"zcode","name":"Zcode","skill_roots":[".zcode/skills"],"skill_count":28,"reason":"你之前选了忽略或不再管理。"}]
```

页面做法：

- “发现的新 agent”那一段现在同时画 `suggested` 和 `ignored`；已忽略的多一个“已忽略”小标签，只给“接入”按钮，不再给“忽略”。
- `ignored` 还是编号数组时跳过（`ignoredAgents()` 只保留对象项），不会画出空行也不会报错。
- 顶部“发现新 agent”横幅只看 `suggested`，并且把同时出现在 `ignored` 里的编号过滤掉；没有建议时把横幅文字清空（不再留上一次的句子）。
- 已忽略的 agent 点“接入”走的还是同一个 `join/preview` + `join/apply`（后台 `join` 会把它移出忽略名单）。在真后台上核对：`POST /api/agents/join/preview {agents:['zcode']}` 返回 200、`agents: ['zcode']`、28 个技能、0 条冲突，所以对话框里只需要按“接入”。这一轮的端到端脚本里也加了“页面把已忽略的 Zcode 列在建议下面、标已忽略、只给接入”一项。

### F5 不让慢的 `/api/agents` 拖住写入完成提示

`refreshEverything()` 改成 `await Promise.all([refreshTable(), refreshInventory()])` + `await refreshLog()`，最后**不再 await** `refreshAgents()`（失败仍旧弹提示）。这样写操作的完成提示只等前三个快接口，agent 数据晚到会自己把那一栏重画。核对脚本把 `/api/agents` 人为拖慢 2 秒，断言 `refreshEverything()` 在 1 秒内返回，并随后确认 agent 数据仍然会填上。

### F6 7～8 个 agent 时数字被截断

页头摘要原来 `minmax(190px, 1fr)` + 整行 `white-space: nowrap` + 省略号，7 个 agent 时每格只有约 185px，“39 个技能 · 说明 10,185 字”就被截成“10,18…”。改法三处一起：

- 格子最小宽度 190px → 220px（1440px 窗口下变成每行 5 格，7～8 个自然换到第二行）；
- “名字 + 数字”那一行允许折行（`flex-wrap: wrap`），格子不够宽时数字整段折到下一行，不截断；
- 名字允许收缩（`flex: 0 1 auto; min-width: 0`），实在放不下时先缩名字并加省略号，保证数字和“字”始终完整。

核对方式：静态断言 CSS 规则（`auto-fit` + `220px`、允许折行、数字那里不再有 `text-overflow: ellipsis`），再按“中文 1em、数字 0.55em”的保守字宽估算，在 1440 / 1280 / 1024 / 900 / 800 五种窗口宽度、7 和 8 个 agent、用模拟数据里最长的 Antigravity 一行逐个验证放得下，`78/78 通过`。**真实观感仍要主控在浏览器里过一眼**（尤其是 800px 附近）。

### F7 去掉技详表下面的技术说明

技能详情“各 agent 使用情况”表格下面那几行 `${agent.name}：${usage_note}`（会显示“会话文件中的实际工具调用”“v3/v4 按会话去重”这类技术口径）整段删掉；`noUsage` 变量也一起删了。表格里仍然写“统计不到”，技能页脚注也仍然写清是哪几个 agent 统计不到。顺带把脚注里同样会搬技术说明的那一句（`missing[0].usage_note`）也去掉、并把注释写明“技术口径不往这里搬”。核对脚本断言详情和脚注里都不再出现 `usage_note` 的原文。

### 第二轮验收结果

| 项目 | 结果 |
| --- | --- |
| `node --check skill_pilot/web/static/app.js` | 通过 |
| §5 的 grep | 只剩 F2 放行的两行 Codex 按钮文字（+1 行说明注释） |
| `tests.test_web + tests.test_m5_web + tests.test_generic_agents_web` | 17 项通过 |
| 全量 `unittest discover -q` | 185 项通过（含另一个执行者新加的一项） |
| 主逻辑核对（最小 DOM 跑 `app.js`） | 24/24 通过 |
| F1 连点回归 | 13/13 通过（旧写法 9/13，100/130 毫秒两档会失败） |
| F3/F4/F5/F7 细节核对 | 9/9 通过 |
| F6 CSS 与字宽估算 | 78/78 通过 |
| 接入端到端（真后台，含已忽略重新列出的那一项） | 18/18 通过 |

### 第二轮没做到 / 需要主控补的

- F6 我只能做规则核对和字宽估算，**真实渲染请主控在浏览器里看 7～8 个 agent**（重点是 800～1024px 窗口、以及换行后同一行里格子高度不齐的观感）。
- “已忽略的 agent 点接入并真的接回来”只核对到接口预览 200；端到端脚本里没有真的把它接回来（免得又留一堆状态），点“接入”走的是和 Trae 完全相同的那条路。
- 后台这一轮刚把 `ignored` 换成对象数组，页面两种形状都兼容；等 `ignored` 稳定成对象数组后，`ignoredAgents()` 里兼容字符串的那半句可以删掉。

## 8. 第三轮收尾（G1～G3 与注释写法）

> 主控复验 F1～F7 之后派来的三处收尾。边界不变：只改 `skill_pilot/web/static/` 三个文件和本报告，没有 git 写操作，没有对真实 home 运行。

### G1 依赖自动带上的格子写清“因为谁需要它”

原来格子只写“自动带上”，别的什么都不说。现在：

- `state.autoAdded` 从 `Set` 换成 `Map`（`「技能|agent」→ 需要它的那个技能`），来自预览的 `auto_added`；
- 格子里在“自动带上”下面补一行小字“因为 {中文名} 需要它”，中文名走 `titleOf()`，查不到就显示技能编号；
- 底部操作栏的副句在预览 `message` 后面、依赖警告前面补上整句，例如“因为 多智能体分派 需要 营销技能路由，已一起勾上给 Claude”；一个 agent 有多条用“；”连起来，多个 agent 的再合在一起（`autoAddedNote()`）。句子由页面用 `auto_added` + `titleOf()` 拼，不再依赖后台 `reasons` 的措辞。

核对：格子里出现“自动带上”和“因为 {中文名} 需要它”；副句出现整句；中文名缺失时退回技能编号。

### G2 “不再管理”确认框不再重复那句话

后台 `message` 已经写了“它文件夹里的技能原样留着，照常能用。”，页面的 note 又写一遍，等于说两次。note 现在只写“以后可以在设置里重新接入。”。核对方式：直接检查 `leaveAgent()` 组装确认框的那几行（`lines: [plan.message]` + 新的 note，note 里不再出现“原样留着”）。

### G3 “发现的新 agent”和“没在管理的 agent”分开两栏

原来两批都画在“发现的新 agent”下面，Claude 这类已忽略的也顶着这个标题。现在：

- “发现的新 agent”只画 `suggested`（可以接入、可以忽略）；
- 新增小标题“没在管理的 agent”，画 `ignored`（标“已忽略”，只给“接入”）；
- 两栏各自空着就各自收起；点击处理两个容器共用一套（`for (const id of ['agents-suggested', 'agents-ignored'])`），写入锁定的选择器也加上了 `#agents-ignored button`。
- `ignored` 还是编号数组时仍然跳过不画。

核对：只有已忽略项时“发现的新 agent”收起且内容为空、“没在管理的 agent”显示；两者同时存在时各在各的栏里，互不串栏。

### 注释写法

检查更新页那条 `${/* … */ ''}` 形式的注释挪到了模板字符串外面，改成 `updateRowHtml()` 上面的普通 JS 注释。

### 第三轮验收结果

| 项目 | 结果 |
| --- | --- |
| `node --check skill_pilot/web/static/app.js` | 通过 |
| §5 的 grep | 只剩 F2 放行的两行 Codex 按钮文字 + 它们上面那两行普通注释；我自己写的注释里的示例也改成了中性名字 |
| `tests.test_web + tests.test_m5_web + tests.test_generic_agents_web` | 17 项通过 |
| 全量 `unittest discover -q` | 185 项通过 |
| 主逻辑核对 | 24/24 通过 |
| F1 连点回归 | 13/13 通过 |
| 细节核对（F3/F4/F5/F7 + G1/G2/G3） | 12/12 通过 |
| F6 CSS 与字宽估算 | 78/78 通过 |
| 接入端到端（真后台，含“没在管理的 agent”那一栏） | 18/18 通过 |

## 附录：接口核对的原始输出

命令：模拟 home 起后台后，用带 `X-Skill-Pilot-Token` 与 `Origin` 的请求逐个读接口（脚本在系统临时目录）。下面是原样贴出的输出，只把模拟 home 的绝对路径替换成 `<模拟 home>`。

```text
============ GET /api/table（页面用到的字段） ============
状态 200
顶层字段: ['agents', 'groups', 'home', 'labels_pending', 'llm_configured', 'new_skills', 'notes', 'revision', 'skills', 'write_allowed']
agents[]: [
 {
  "id": "codex",
  "name": "Codex",
  "activation_text": "新会话生效",
  "usage_available": true,
  "usage_note": "会话文件中的实际工具调用",
  "count": 89,
  "description_chars": 27448
 },
 {
  "id": "claude",
  "name": "Claude",
  "activation_text": "马上生效",
  "usage_available": true,
  "usage_note": "会话文件中的技能调用或读取",
  "count": 46,
  "description_chars": 12236
 },
 {
  "id": "antigravity",
  "name": "Antigravity",
  "activation_text": "新会话生效",
  "usage_available": false,
  "usage_note": "统计不到；隔离复现未得到可靠读取步骤",
  "count": 39,
  "description_chars": 10185
 },
 {
  "id": "dsh",
  "name": "DSH",
  "activation_text": "马上生效",
  "usage_available": true,
  "usage_note": "会话格式已验证，v3/v4 按会话去重；实际覆盖见用量页",
  "count": 60,
  "description_chars": 16825
 }
]
notes: ["Codex 和 DSH 共用一个技能目录：只给 DSH 的技能，会在 Codex 里自动停用。"]
revison 前 12 位: 09b2e8a05e49 | write_allowed: True
技能行字段: ['assigned_agents', 'cell_notes', 'description', 'description_chars', 'last_used', 'local_modified', 'name', 'requires', 'source', 'source_label', 'summary', 'title']
示例技能: {"name": "agency-router", "assigned_agents": ["codex"], "requires": ["marketing-router"], "cell_notes": {}, "last_used": {"codex": "2026-09-30", "claude": "没有使用记录", "antigravity": "统计不到", "dsh": "没有使用记录"}}
“revision”在预览里是否出现（下面单独核对）

============ GET /api/agents（设置页 agent 一栏） ============
状态 200 | 顶层字段: ['ignored', 'managed', 'must_select_together', 'revision', 'suggested']
managed[0]: {
 "id": "codex",
 "name": "Codex",
 "skill_roots": [
  ".agents/skills",
  ".codex/skills"
 ],
 "activation_text": "新会话生效",
 "usage_available": true,
 "usage_note": "会话文件中的实际工具调用",
 "shared_with": [
  "dsh"
 ],
 "can_leave": true
}
suggested[]: [
 {
  "id": "trae",
  "name": "Trae",
  "skill_roots": [
   ".trae/skills"
  ],
  "skill_count": 28,
  "reason": "这个目录直属放着技能文件夹，可能属于一个新 agent。"
 }
]
ignored: ['kimi', 'zcode'] | must_select_together: [['codex', 'dsh']]

============ POST /api/assignment/preview ============
状态 200 | 输入: 给 claude 勾上 agency-router （它需要 ['marketing-router'] ）
返回字段: ['after', 'assignments', 'auto_added', 'cell_notes', 'changed', 'dependency_warnings', 'message', 'path_warnings', 'reasons']
changed: ['agency-router', 'marketing-router'] | reasons: ["因为 agency-router 需要 marketing-router，已一起勾上给 Claude"]
auto_added: [{"skill": "marketing-router", "agent": "claude", "because": "agency-router"}]
message: 将给 Claude 加 2 个
dependency_warnings: [] | path_warnings 条数: 7
after: {"codex": {"count": 89, "description_chars": 27448}, "claude": {"count": 48, "description_chars": 13295}, "antigravity": {"count": 39, "description_chars": 10185}, "dsh": {"count": 60, "description_chars": 16825}}
预览里有 revision 吗： False
assignments[技能] 条数: 130 | cell_notes[技能][agent]: {"context-pack": {"codex": "Codex 里已自动停用"}, "nas-knowledge-base": {"codex": "Codex 里已自动停用"}}

============ GET /api/scan（详情页与诊断页） ============
状态 200 | 顶层字段: ['agents', 'candidates', 'conflicts', 'count_basis', 'disabled', 'discovered_skill_roots', 'discovery_note', 'errors', 'generated_at', 'home', 'read_only', 'roots', 'schema_version', 'source_copies']
disabled: {"codex": {"names": ["academic-cv-builder", "application-form-filler", "build-ios-apps:ios-app-intents", "…"], "paths": 1, "error": null}}
有 codex_disabled 吗： False
根字段: ['broken_count', 'category', 'entry_count', 'errors', 'exists', 'manifest_count', 'path', 'readers', 'skills', 'unresolved_links', 'valid_count']
技能条目字段: ['broken', 'description', 'description_chars', 'digest', 'directory_name', 'kind', 'link_target', 'manual_markers', 'manual_only_for', 'model_invocable', 'name', 'name_mismatch', 'path', 'resolved_path', 'valid', 'version', 'warning']
manual_only_for 示例: [["critical-gate", ["codex"]], ["grill-me", ["codex"]], ["review-agent", ["codex"]]]
根 readers 示例: [[".agents/skills", "user", ["codex", "dsh"]], [".codex/skills", "user", ["codex"]], [".codex/skills/.system", "builtin", ["codex"]]]

============ POST /api/agents/join/preview（接入核对框） ============
状态 200 | 返回字段: ['agents', 'before', 'conflicts', 'must_select_together', 'plan_id', 'profiles', 'read_only', 'revision', 'roots', 'skills', 'skipped']
agents: ['trae'] | roots: ['.trae/skills'] | read_only: True | must_select_together: [['codex', 'dsh']]
skills[] 字段: ['action', 'assigned_agents', 'comparison', 'default', 'entries', 'name', 'options', 'reason']
action 计数: {'choose': 2, 'linked': 26}
conflicts 条数: 2 | skipped: []
choose 行: {"name": "lark-approval", "action": "choose", "default": "store", "reason": "内容不同，默认保留仓库里的版本；选择新 agent 的版本，会更新所有 agent 共用的这份技能。"}
options: [
 {
  "id": "store",
  "origin": "store",
  "path": "<模拟 home>/.skill-pilot/store/lark-approval"
 },
 {
  "id": "7776a6fa9dd4f3a762abbece4f91c8d42761a090fb49d45ac17c0866ad06321d",
  "origin": "incoming",
  "path": "<模拟 home>/.trae/skills/lark-approval"
 }
]
comparison 前 120 字: 默认选择：<模拟 home>/.skill-pilot/store/lark-approval / 另一份：/
before[trae] 字段: ['automatic', 'available', 'versions']

============ POST /api/agents/leave/preview（不再管理确认框） ============
状态 200 | 返回字段: ['agent', 'before', 'changed', 'kept_entries', 'lost_skills', 'message', 'name', 'plan_id', 'remaining', 'removed_shared_entries', 'revision']
name: DSH | remaining: ['codex', 'claude', 'antigravity']
lost_skills: ['context-pack', 'nas-knowledge-base']
message: 不再管理 DSH；它以后看不到这些技能：context-pack、nas-knowledge-base
有 _registry 吗（不该有）: False

============ POST /api/agents/ignore/preview（忽略建议） ============
状态 409 | 返回字段: ['error']
{"error": "这个 agent 已不在建议里，请刷新。"}

============ 复位后重取 ignore/preview ============
先撤销上一次忽略: 200
状态 200 | 返回字段: ['agent', 'message', 'plan_id', 'revision']
{"agent": "zcode", "revision": "fca7ab5b474137d6b94ba7c0abe53da4cbb4303cc2de1350d9ade309db10316c", "message": "忽略 Zcode 的接入建议。", "plan_id": "84f810a6ef8f28898695498de02402cf9c1c8ca2480fb9e2b7f274446398d509"}
```
