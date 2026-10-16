# 交接：勾选提速 + 助手通用化（后端）

> 2026-10-13 由 Claude（主控）派给 Codex。先读本仓库 `AGENTS.md`、`docs/STATUS.md`，再读本文件。用户已在对话里确认本方案。
> 本轮只改后端、脚本和测试。页面（`skill_pilot/web/static/` 三个文件）下一轮交给 DSH，你不要改；本轮接口变化导致旧页面暂时对不上没关系，但要在报告里写清每个接口的最终字段。

## 0. 工作位置与边界

- 你在 git worktree `~/Developer/skill-pilot-generic-agents`（分支 `generic-agents`）里工作，沙箱只允许写这个目录和系统临时目录。用户双击打开的真实看板跑的是 `~/Developer/skill-pilot`（main），不要去碰。
- 工作区里已暂存（staged）的只有本交接文件。**不要 `git add`、`commit`、`reset`、`checkout`、`stash`，不切分支、不改 git 配置**；你的改动留在未暂存状态，主控用 `git diff` 审阅。
- 不对真实 home 运行本工具，不写 `~/.skill-pilot/`，不运行 `scripts/build-app.sh`、`skill-pilot` 命令行、`lark-cli update`、`npx skills`，不执行任何技能目录里的脚本。手测只用 `scripts/make-fake-home.py` 生成的模拟 home（§7）。不调用真实 AI 服务，不调用 Codex 或 DSH。
- 只用标准库。解释器：`~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11`。
- 遵守 `AGENTS.md` 的“做减法”：不加本文件没要求的功能、配置项、抽象层。

## 1. 背景与用户确认的方案

用户反馈两个问题：

1. **勾选卡顿**：技能页每点一个勾选框，页面变灰约 1 秒不能操作，连点几个要等很久；而勾完还要按底部“应用”才生效。原因（主控已实测）：页面每次勾选都调 `/api/assignment/preview`，`operations.preview_assign()` 里调用了整张 `table()`（重算 `revision()` 的全仓库内容摘要和每个技能的 git tree、`new_skills()` 重扫所有技能根、逐个算 `local_modified`），在用户数据的模拟 home 上一次 **2.4 秒**；页面在这期间锁住所有勾选框。
   用户确认：**保留“勾完统一按应用”**，勾选本身不能再等。
2. **写死四个助手**：四个助手的信息散在前后端十几处（按 `'codex'`/`'dsh'`/`'claude'`/`'antigravity'` 分支、写死路径、写死“Kimi/WorkBuddy/Cursor/Trae”），首次迁移后**无法再接入新助手**（`store.apply()` 遇到已有登记表直接拒绝），也没有“不再管理某个助手”。
   用户确认：做成通用技能看板——助手只是一份**档案数据**，程序和页面只读档案；增加助手 = 看板发现新技能目录 → 用户核对名字和目录 → 接入；减少助手 = 不再管理（它目录里的技能原样留着、照常能用，以后可重新接入）。每个助手唯一可能需要单独写代码的是**用量读取**（各家会话记录格式不同），没写的显示“统计不到”、分配给它的技能不进清理名单（同现在的 Antigravity）。
   对原 PLAN 的改动：原定“新助手先让 Codex 调研再接入”，改为**直接按发现的目录接入、其余项取保守默认值**。“让 Codex 查一下”按钮本轮不做。

**硬性保证**：改完后，用户现有四个助手的一切行为与现在逐条一致（放置位置、Codex 停用条目、可见名单、扫描结果、用量统计），用户真实数据不需要任何迁移；`~/.skill-pilot/agents.json` 现有格式 `{"onboarded": [...], "ignored": ["kimi"]}` 照常能读。

## 2. 任务 A：勾选预览提速

- `preview_assign()` 不再调用 `table()`、`revision()`、`new_skills()`、`normalization.tree()`。只需要：登记表、依赖（`curated_dependencies` + 登记表 `requires`）、每个技能的说明字数（读 store 里的 `SKILL.md` 元数据，或其他便宜办法）、`path_warnings`。
- 返回里去掉 `revision`（应用时 `apply_assign` 照旧校验版本号，过期就报错）。
- 依赖自动带上的原因，除了现有 `reasons` 文本，再给结构化的 `auto_added: [{"skill", "agent", "because"}]`（现在页面用正则从带助手**显示名**的句子里反解，很脆弱）。
- 返回 `cell_notes`（见 §3.8），描述草稿状态下每个技能、每个助手格子要显示的说明。
- 目标：在用户数据的模拟 home 上 **< 100 毫秒**（§7 有计时脚本）。

## 3. 任务 B：助手通用化

### 3.1 档案格式

把 `skill_pilot/profiles/{codex,claude,antigravity,dsh}.py` 换成一个数据文件（建议 `skill_pilot/profiles/catalog.json`，内置已知助手），`profiles/__init__.py` 负责读取、合并用户自定义档案、校验。每份档案只保留确实被程序用到的字段，建议：

| 字段 | 含义 | 现有四个助手的取值 |
| --- | --- | --- |
| `id`、`name` | 稳定编号（用量账本、登记表都用它，**不能改**）、显示名 | 照旧 |
| `detect` | 判定“本机装了”的证据：配置目录、应用名、命令名 | 照搬现有 `config_dirs`/`apps`/`cli` |
| `skill_roots` | 它读取的用户技能目录（相对 home） | 照旧 |
| `link_root` | 只需要给它一个人放链接时，放在哪个目录（必须是 `skill_roots` 之一） | codex `.codex/skills`，dsh `.agents/skills`，claude `.claude/skills`，antigravity `.gemini/config/skills` |
| `nested` | 是否会发现目录里嵌套的技能 | 只有 dsh 为 false |
| `readonly_roots` | 内置、插件等只看不改的目录及类别 | 照旧 |
| `activation` | `immediate`（马上生效）或 `new_session`（新会话生效） | claude、dsh 马上；codex、antigravity 新会话 |
| `manual_only` | 哪些标记会让技能“只允许手动调用” | codex：`disable-model-invocation` 和 `agents/openai.yaml` 的 policy；dsh：`disable-model-invocation`；其余空 |
| `disable` | 停用机制（适配器名），没有为 null | codex `codex_config`，其余 null |
| `usage` | 用量读取器与会话目录，没有为 null，另给一句“统计不到”的原因 | 照搬 `usage/__init__.py` 的 `SOURCES` 和各自说明句 |
| 说明文字 | 现有 `visibility_note`、`usage`、`disable` 等给诊断页看的句子 | 照搬 |

字段名、是否合并可以按你判断调整，但**不要为假想需求加字段**。

用户自定义档案（接入目录里没有的助手时生成）存进 `~/.skill-pilot/agents.json`（比如新增 `custom` 列表）。保守默认值：`link_root` = 唯一的技能目录，`nested` false，`readonly_roots` 空，`activation` `new_session`，`manual_only` 空，`disable` null，`usage` null。

### 3.2 去掉按助手名分支

除下面列的例外，`skill_pilot/` 里不再出现具体助手的编号或其目录。主控验收时会跑：

```sh
grep -rnE "'(codex|claude|dsh|antigravity)'|\"(codex|claude|dsh|antigravity)\"|\.codex/|\.claude/|\.dsh/|\.gemini/" skill_pilot --include='*.py'
```

允许出现的地方：`skill_pilot/profiles/`（档案读取）、`skill_pilot/usage/` 下各读取器模块本身、`codex_config.py`（Codex 停用适配器本身，路径改为从档案拿也可以）、`research.py`（把 Codex 当工具调用，与被管理的助手无关）。`.agents/.skill-lock.json`（`npx skills` 的来源记录，不是助手）和用户自己的 `Developer/vendor/stop-that-shit-local` 路径不在本轮范围，原样保留。

主控已知的写死点（不保证完整，请自己再查一遍）：

- `sync.py`：`desired_links` 共享根与 dsh 特判、`visibility` 的 codex/dsh 特判、`assignment_entries` 整个函数、`reconcile_assignments` 的停用名单。
- `scan.py`：`classify` 里 `.claude/skills/synced`、kimi、`skills-cursor`；`scan_root` 跳过 `synced`；`scan()` 里 dsh 不递归、codex 停用与手动调用、写死的 Kimi/WorkBuddy/Cursor/Trae 列表；`codex_implicit_invocation` 字段名。
- `store.py`：`PROTECTED`；`preview` 的 codex/dsh 必须同选、`.dsh/skills` 不递归、写死的未接入目录列表、`.codex/config.toml` 状态；`apply` 写 `agents.json` 时写死 `ignored: ["kimi"]`、拒绝已有登记表。
- `operations.py`：`revision`、`new_skills` 的 dsh 特判、`table` 里 antigravity“统计不到”和生效方式、`_snapshot` 的配置路径。
- `updates.py` 读 Codex 停用配置；`usage/__init__.py` 的 `PARSERS`/`SOURCES`/dsh/claude 特判；`usage/common.py` 的写死目录列表和 `community/` 特判；`cli.py` 的“默认四个主力”。
- `scripts/make-fake-home.py` 写死的四个目录：改为从档案取。

### 3.3 通用放置规则与停用规则

一个目录的“读者”= 所有已接入助手里 `skill_roots` 包含它的那些（`nested` 为 false 的只认根目录直属的条目，放置本来就只放直属）。

**放置**（替代 `assignment_entries`）：对一个技能和它被分配的助手集合 A，从 A 里各助手的 `link_root` 中，贪心选“覆盖 A 中尚未覆盖助手最多”的目录，直到 A 全覆盖。如果选中的目录还会被 A 以外的助手读到，这些助手必须有停用机制（`disable` 非 null），否则报错（大白话：“X 和 Y 共用一个技能目录，没法只给 X”）。

**停用**（替代 `reconcile_assignments` 末尾的 `disabled` 计算）：对每个有停用机制的助手 X，停用名单 = X 没被分配、但有条目位于“X 读、且有某个**已分配**助手也读”的目录里的技能。用户数据里有 3 个技能没分配给任何人但 `.codex/skills` 里还留着条目（用户自己的停用条目在管它们），按此规则不会被工具加停用，与现在一致。

**必须证明与现在一致**：
- 写一个测试，把现在的 `assignment_entries` 原样抄进测试作参照，四个助手的 16 种分配组合逐一比对新规则的结果。
- 在用户数据的模拟 home 上，用 main 的代码和你的代码分别计算停用名单、`visibility()`、`scan()`、`table()` 的输出，比对一致（新增字段除外）。比对脚本放 `work/` 或临时目录，结果写进报告。

### 3.4 扫描与发现新助手

- `scan()` 按档案盘点已接入和“装了但没接入”的目录里助手。
- 通用发现：`discover()` 找到的、不属于任何已知档案目录、分类不是内置/插件/缓存/参考资料/备份/忽略的技能目录里，形如 `~/.<名字>/skills`（或类似、直属就是技能文件夹）的，列为**建议接入的新助手**：给出建议编号、显示名（目录名去点、首字母大写；`catalog.json` 可以带一个“目录名 → 显示名”的小表，比如 `.workbuddy` → WorkBuddy）、技能目录、技能数。用户这台电脑上现在有 `~/.trae/skills`、`~/.zcode/skills` 两例。其他发现照旧列在“其他发现”。
- 忽略名单（`agents.json` 的 `ignored`，现在是 kimi）里的不再建议。新增“忽略这个建议”的写入。

### 3.5 接入新助手（可重复）

现在 `store.apply()` 遇到已有登记表就拒绝。改成任何时候都能接入一个（或一组）助手：

- 输入：一个建议的编号、目录里已知但没接入的助手编号，或用户填的显示名 + 技能目录（目录必须在 home 内，不能是统一仓库、只读目录、`~/.claude/skills/synced`、`~/.codex/skills/.system`，不能已经被其他已接入助手管理——除非就是共用目录）。
- **共用目录**：和某个已接入或同批接入的助手共用目录的，按现有原则“共用目录的助手一起接入”（通用化现在的 codex/dsh 必须同选）。
- 预览（只读）：列出新助手目录里每个技能会怎样：已经链接到统一仓库（直接算分配给它）、内容和仓库一致（换成链接）、仓库里没有（收进仓库）、仓库里有但内容不同（**让用户选保留哪份，默认仓库里那份**；选新助手那份等于更新所有助手用的这份，预览里要用大白话写清）、无效或断链（原样留着）。给出 `plan_id` 防过期，提供不同内容的对比（复用 `compare_files`）。
- 应用：走 `operations._run` 那一套（锁、备份、登记、写后校验、操作记录、可撤销）。不同内容时被替换掉的原文件放进本次备份，不丢用户文件（同 `absorb`）。写 `agents.json`（自定义档案一并存）。
- 写后校验：其他已接入助手能看到的技能名单与内容完全不变；新助手能看到的名单与接入前相同，内容按用户的选择。
- 首次迁移（还没有统一仓库时）最好就是“接入所选助手”的同一套流程；如果统一成本太大，可以保留 `store.preview/apply` 作首次迁移，但要去掉其中的写死，并在报告里说明。

### 3.6 不再管理

- 至少保留一个已接入助手。
- 效果：从 `agents.json` 的 `onboarded` 去掉（自定义档案保留，方便重新接入）；从所有技能的 `assigned_agents` 去掉它；登记表里只属于它的目录的条目移出登记表，**链接文件原样留在它目录里**（技能照常能用）。
- 硬性：其余助手能看到的技能名单和内容完全不变。共用目录导致离开的助手某些技能没法保留的（比如只给 DSH、放在共用目录、靠 Codex 停用条目隐藏的技能），选最简单且满足硬性条件的处理，在预览里列出“X 以后看不到这几个”，在报告里说明你的选择。
- 有预览、可撤销、进操作记录；撤销后与之前指纹一致。不再管理后能再接入回来。

### 3.7 用量读取插件化

- `usage/__init__.py` 里不出现助手编号：读哪个助手、去哪些目录找什么文件、会话编号怎么取、要不要逐行预筛，都由档案的 `usage` 指向的读取器模块提供。现有 claude、codex、dsh 三个读取器保持行为不变，现有用量测试全部通过，账本里的 `agent` 值不变（不需要重扫）。
- `usage: null` 的助手：覆盖信息里 `available: false`，原因用档案里的句子；`table()` 的“最近使用”显示“统计不到”。
- `antigravity.py` 只剩一句原因的话，移进档案后删掉。
- `usage/common.py` 识别 `SKILL.md` 读取路径时的技能根，从档案取（全部档案的 `skill_roots` + 统一仓库）；`community/` 那个嵌套特判改成按 `nested` 通用处理，结果不变。

### 3.8 给页面的接口（下一轮 DSH 用，字段名可调，报告里写清最终版）

- `GET /api/table`：`agents` 按显示顺序（内置目录顺序 codex、claude、antigravity、dsh，之后按接入先后），每个带 `id`、`name`、`count`、`description_chars`、`activation_text`（“马上生效”/“新会话生效”）、`usage_available`、`usage_note`。另给 `notes`：根据档案生成的提示句（如“Codex 和 DSH 共用一个技能目录：只给 DSH 的技能，会在 Codex 里自动停用。”），页面原样显示。技能行加 `cell_notes: {agent_id: 文本}`（如“Codex 里已自动停用”），页面原样显示。
- `POST /api/assignment/preview`：见 §2（`assignments`、`after`、`message`、`reasons`、`auto_added`、`dependency_warnings`、`path_warnings`、草稿状态的 `cell_notes`；不再有 `revision`）。
- `GET /api/scan`：每个技能条目的“只允许手动调用”改成通用表达（如 `manual_only_for: [agent_id]`），去掉 `codex_implicit_invocation` 这种带名字的字段；`codex_disabled` 改为通用的“各助手停用名单”（如 `disabled: {agent_id: {...}}`）。
- 新增 `GET /api/agents`：`managed`（已接入：编号、名字、技能目录、生效方式、用量说明、和谁共用目录、能否“不再管理”）、`suggested`（建议接入：编号、名字、技能目录、技能数、为什么认为它是助手）、`ignored`。
- 新增接入、不再管理、忽略建议的预览与写入接口，写法沿用现有（读路由不要求确认；写路由要求 `confirmed: true`、`revision`，只读模式拒绝，成功返回操作记录编号供撤销）。
- 现有迁移接口如保留，助手列表从档案来，“哪些助手必须一起选”由接口给出。

## 4. 不做

- 不改页面三个文件。
- 不给 Trae、Zcode、Cursor 等写用量读取器，不在任何真实环境接入它们。
- 不做“让 Codex 查一下”调研按钮。
- 不优化“应用”本身的耗时（约 2.6 秒），不改检查更新、清理、操作记录的功能。
- 不改登记表、用量账本、`labels.json` 的数据格式（`agents.json` 只做向后兼容的新增）。

## 5. 测试

现有全部测试必须通过（测试里写死的 `.codex/skills` 等是在构造模拟目录，属正常）。新增至少：

1. 16 种组合放置与现有函数一致；停用规则覆盖“只给 DSH”、“三个都没分配但留着条目”。
2. 通用第 5 个助手：在模拟 home 放 `~/.zcode/skills/` 下几个技能（其中一个与仓库同名同内容、一个同名不同内容、一个新的），不改任何代码：被列为建议 → 预览 → 接入（不同内容两种选择各测一次）→ 能分配和撤下（链接放在 `.zcode/skills`）→ 撤销接入后指纹一致 → 再接入 → 不再管理（其余助手可见名单不变、链接留在原处）→ 重新接入。
3. 共用目录：两个自定义助手共用一个目录时，必须一起接入；给其中一个、另一个没有停用机制时，分配报大白话错误且文件不变。
4. 忽略建议后不再出现在 `suggested`。
5. `preview_assign` 不调用 `table()`/`revision()`（可以用 mock 断言），结果与原实现一致（`revision` 除外）。

## 6. 验收（全部做完再交）

1. 全量测试：`PYTHONDONTWRITEBYTECODE=1 <解释器> -m unittest discover -q` 全部通过。
2. §3.2 的 grep 只命中允许的文件。
3. 用户数据模拟 home 上：`preview_assign` < 100 毫秒；main 与新代码的停用名单、`visibility()`、`scan()`、`table()` 比对一致（新增字段除外）。
4. 冒烟：在模拟 home 启动看板后台，用 HTTP 请求走一遍 `GET /api/agents` → 接入一个假的第 5 个助手 → 分配一个技能给它 → 撤销 → 不再管理。原始输出写进报告。
5. 写报告 `docs/generic-agents-report.md`：改了什么、档案格式、放置与停用规则、接入与不再管理的行为（含共用目录的取舍）、**全部接口的最终字段**（给 DSH 用）、测试与比对与计时的原始输出、没做或做不到的地方。

## 7. 模拟 home 与计时

```sh
PY=~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11
FAKE=$(mktemp -d)/home
PYTHONDONTWRITEBYTECODE=1 $PY scripts/make-fake-home.py "$FAKE"
FULL=$(cd "$FAKE" && pwd -P)    # 必须用 /private/var/... 形式，否则 store.guard() 会判成只读
# 计时（主控用过的写法）
SKILL_PILOT_READ_ONLY=1 PYTHONDONTWRITEBYTECODE=1 $PY - "$FULL" <<'EOF'
import sys, time
from pathlib import Path
from skill_pilot import operations
home = Path(sys.argv[1]); view = operations.table(home); skill = view['skills'][0]
t = time.time()
operations.preview_assign([{'skill': skill['name'], 'agent': 'claude', 'enabled': 'claude' not in skill['assigned_agents']}], home)
print('preview_assign', round(time.time() - t, 3))
EOF
# 看板后台（放后台；30 秒没有页面心跳会自己退出，需要时定时 POST /api/heartbeat?page=任意）
SKILL_PILOT_HOME="$FULL" BROWSER=true PYTHONDONTWRITEBYTECODE=1 $PY -m skill_pilot ui --json
```

比对 main 的输出时，把 main 的代码导出到临时目录再运行（`git archive main | tar -x -C "$(mktemp -d)"`），用同一个解释器、同一个 `$FULL`、`SKILL_PILOT_READ_ONLY=1` 只读运行；不要在 `~/Developer/skill-pilot` 目录里运行或写任何文件。
