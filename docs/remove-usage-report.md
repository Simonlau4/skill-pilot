# 拿掉用量统计、清理改成手动挑：后端完成报告

2026-10-13。按 [handoff-remove-usage.md](handoff-remove-usage.md) 完成后端范围。

用量统计已删除；清理现在只撤下用户明确选中的技能，不再按使用时间生成名单。预览会提示依赖和受影响的文件；撤下后技能文件仍保留，操作记录、撤销及恢复原分配继续可用。全量 176 项测试通过，独立模拟看板和命令行验收通过。

## 文件变更

删除的用量包文件，共 6 个：

- `skill_pilot/usage/__init__.py`
- `skill_pilot/usage/common.py`
- `skill_pilot/usage/codex.py`
- `skill_pilot/usage/claude.py`
- `skill_pilot/usage/dsh.py`
- `skill_pilot/usage/dsh_decode.js`

删除的测试：`tests/test_usage.py`、`tests/test_usage_lock.py`。

删除的过时开发脚本：

- `scripts/verify-m5-usage.py`：旧用量数据交叉核对。
- `scripts/serve-fix-usage-lock-fixture.py`、`scripts/verify-fix-usage-lock-ui.cjs`：旧统计进度和数据库占用场景。
- `scripts/serve-m5-fixture.py`、`scripts/m5-playwright.py`：旧用量及自动清理页面的模拟数据和专用浏览器入口。

保留并修改的文件：

| 文件 | 改动 |
| --- | --- |
| `skill_pilot/operations.py` | `table(home=None)` 去掉 `database` 参数、技能的 `last_used` 和 agent 的用量字段。 |
| `skill_pilot/agents.py` | overview 的 managed 条目去掉用量字段。 |
| `skill_pilot/scan.py` | agent 扫描结果去掉用量字段。 |
| `skill_pilot/profiles/catalog.json`、`profiles/__init__.py` | 去掉用量来源、说明、读取器及其目录校验；保留 `manual_only`、技能目录和停用机制。旧自定义档案中的已废弃字段在读取时忽略，不改写原文件。`config_paths()` 只保留停用设置路径。 |
| `skill_pilot/cleanup.py` | 删除安装时间资格推算、60 天规则、自动候选、覆盖说明、省字数和“留下”逻辑；增加已撤下列表、手动预览和应用。`restore()` 的代码与原版完全相同。 |
| `skill_pilot/web/server.py` | 删除统计锁、进度、临时数据库、数据库路径及两个用量接口；接入手动清理预览和应用。 |
| `skill_pilot/cli.py` | 删除 `usage` 和 `--keep`；接受明确的技能名，不带 `--yes` 只预览。 |
| `scripts/make-fake-home.py` | 跳过旧 `usage.sqlite` 及其附属文件；不读取或复制这些文件。 |
| `scripts/serve-m7-fixture.py` | 删除模拟用量和旧清理年龄数据，保留更新、分配等模拟场景。 |
| `scripts/verify-m5-cleanup.py` | 改为手动预览、撤下、已撤下列表、恢复、撤销的命令行验收；结果直接输出。 |
| `scripts/verify-m7-ui.cjs` | 只移除旧自动清理和“留下”验收段，保留其余页面验收。 |
| `tests/test_cleanup.py`、`test_m5_web.py` | 改为手动选择；覆盖警告、确认、应用、撤销、恢复、非法选择、过期状态和失败回滚。 |
| `tests/test_generic_agents.py`、`test_generic_agents_web.py`、`test_store.py` | 核对用量字段已消失、旧自定义档案兼容、模拟复制跳过旧数据库。 |
| `AGENTS.md` | 更新真实数据、解释器说明和结构速览；明确旧数据库只保留。 |

新增 `tests/test_cleanup_cli.py` 和标准库验收脚本 [verify-remove-usage.py](../scripts/verify-remove-usage.py)。`research.py` 没有用量引用，无需修改。

## 实际接口字段

以下字段来自本次接口实测及返回代码。所有请求带 `X-Skill-Pilot-Token` 和 `Origin`。

### `GET /api/cleanup`

直接返回，不使用统计锁。

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `revision` | string | 当前状态版本，用于后续写入核对。 |
| `write_allowed` | boolean | 当前目录是否允许修改。 |
| `withdrawn` | array | 已撤下的技能，按原名排序。 |
| `withdrawn[].name` | string | 技能原名。 |
| `withdrawn[].previous_agents` | string[] | 撤下前的 agent 编号。 |
| `withdrawn[].withdrawn_at` | string 或 null | 撤下时间；旧记录没有时间时为 null。 |

不再返回 `candidates`、`excluded`、`coverage`、`savings`、`checked_at`、`ledger`、`scanned_bytes`。

### `POST /api/cleanup/preview`

请求 `{"skills": ["技能原名"]}`。`skills` 必须是非空字符串列表；重复名称只计一次，返回按原名排序。每项必须已登记、分配给至少一个 agent，且未处于已撤下状态。不符合时返回 HTTP 400 和 `{"error": "中文原因"}`，整批不操作。

| 返回字段 | 类型 | 含义 |
| --- | --- | --- |
| `skills` | string[] | 本次实际选中的技能。 |
| `message` | string | `从所有 agent 撤下 N 个技能`。 |
| `dependency_warnings` | string[] | 仍分配着、直接需要被撤下技能的其他技能；本次也撤下的技能不列入。 |
| `path_warnings` | array | 本次会消失的入口被其他文件引用的情况，沿用 `operations.path_warnings()`。 |
| `path_warnings[].file` | string | 受影响的文件。 |
| `path_warnings[].entries` | string[] | 该文件引用的入口。 |
| `revision` | string | 这次预览的状态版本。 |

依赖警告优先采用 `labels.json` 的中文名，未设置时使用原名。实测例子：

> 「多智能体分派」还要用「营销技能路由」，撤下后它可能用不了。

### `POST /api/cleanup/apply`

| 请求字段 | 类型 | 要求 |
| --- | --- | --- |
| `skills` | string[] | 与预览相同的选择校验。 |
| `revision` | string | 必填非空，必须与当前状态一致。缺失、类型不对或已过期返回 HTTP 409。 |
| `confirmed` | boolean | 必须严格为 `true` 才写入；字符串或数字不能代替。 |

有依赖或文件警告、且未确认时，HTTP 200 返回 `needs_confirmation: true` 和预览的全部字段，文件不改动。没有警告且未确认时，沿用其他写入的 HTTP 400 确认提示。只读环境的已确认写入返回 HTTP 403。非法技能选择返回 HTTP 400。

成功时返回：

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | string | 本次操作编号，用于撤销。 |
| `message` | string | `撤下了 N 个技能`。 |
| `backup_id` | string | 操作前备份编号。 |
| `withdrawn` | string[] | 本次撤下的技能原名。 |
| `dependency_warnings` | string[] | 本次依赖警告。 |
| `path_warnings` | array | 本次文件引用警告，内部字段同预览。 |

登记字段继续使用 `withdrawn`、`withdrawn_at`、`withdrawn_agents`、`withdrawn_entries`。技能从全部 agent 撤下，仓库文件保留。旧记录中的“留下”时间不再参与任何判断，也不新增这类记录。

### `GET /api/table`

| 顶层字段 | 类型 | 含义 |
| --- | --- | --- |
| `home` | string | 当前目标主目录。 |
| `revision` | string | 当前状态版本。 |
| `write_allowed` | boolean | 是否允许修改。 |
| `skills` | array | 已登记技能。 |
| `agents` | array | 已接入 agent。 |
| `notes` | string[] | 共用目录等说明。 |
| `new_skills` | array | 新装技能扫描结果，保留原接口字段。 |
| `groups` | array | 显示分组；每项 `name: string`、`skills: string[]`。 |
| `labels_pending` | integer | 还没有中文名的技能数量。 |
| `llm_configured` | boolean | 是否配置了 AI 服务。 |

| `skills[]` 字段 | 类型 | 含义 |
| --- | --- | --- |
| `name` | string | 技能原名。 |
| `title` | string | 中文名；未设置时为空。 |
| `summary` | string | 中文说明；未设置时为空。 |
| `description` | string | 原技能描述。 |
| `description_chars` | integer | 描述字数，继续用于展示开场阅读量。 |
| `source` | object | 已登记来源，结构沿用原接口。 |
| `source_label` | string | 来源显示名称。 |
| `assigned_agents` | string[] | 已分配的 agent 编号。 |
| `requires` | string[] | 已确认的依赖技能原名。 |
| `local_modified` | boolean | 本机内容是否与原始版本不同。 |
| `cell_notes` | object | 按 agent 编号保存的停用说明。 |

| `agents[]` 字段 | 类型 | 含义 |
| --- | --- | --- |
| `id` | string | agent 编号。 |
| `name` | string | 显示名称。 |
| `activation` | string | 生效方式说明。 |
| `activation_text` | string | 简短生效说明。 |
| `count` | integer | 当前技能数。 |
| `description_chars` | integer | 当前已分配技能描述字数。 |

技能不再返回 `last_used`，agent 不再返回 `usage_available`、`usage_note`。

### `GET /api/agents`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| `revision` | string 或 null | 当前状态版本；尚未迁移时为 null。 |
| `managed` | array | 已接入的 agent。 |
| `suggested` | array | 建议接入的 agent，沿用原条目字段。 |
| `ignored` | array | 已忽略的 agent，沿用原条目字段。 |
| `must_select_together` | string[][] | 必须一起接入的共用目录组合。 |
| `managed[].id` | string | agent 编号。 |
| `managed[].name` | string | 显示名称。 |
| `managed[].skill_roots` | string[] | 技能目录。 |
| `managed[].activation_text` | string | 生效说明。 |
| `managed[].shared_with` | string[] | 共用技能目录的其他 agent。 |
| `managed[].can_leave` | boolean | 是否可以不再管理该 agent。 |

managed 条目不再返回 `usage_available`、`usage_note`。`/api/scan` 的 agent 条目同样没有用量字段。

### 恢复、撤销及已删除接口

- `POST /api/withdrawn/restore` 请求仍为 `skills: string[]`、`revision: string`、`confirmed: true`；成功返回 `id: string`、`message: string`、`backup_id: string`、`restored: string[]`、`reasons: string[]`。
- `POST /api/undo` 请求仍为 `id: string`、`revision: string`、`confirmed: true`；成功返回 `id: string`、`message: string`、`backup_id: string`。
- `GET /api/usage`、`GET /api/usage-progress` 均实测为 HTTP 404。

接口名称和约定字段没有调整。确认提示沿用 `needs_confirmation`，已在实测中核对。

## 命令行

```sh
skill-pilot cleanup --list [--json]                    # 只列已撤下的技能
skill-pilot cleanup SKILL [SKILL ...] [--json]         # 只预览选择和警告
skill-pilot cleanup SKILL [SKILL ...] --yes [--json]   # 确认撤下指定技能
skill-pilot restore-withdrawn SKILL [SKILL ...] [--json]
```

`--list` 不能与技能名或 `--yes` 同用。不指定技能且不带 `--list` 会给出中文提示。预览成功退出码为 0；非法技能选择退出码为 1；命令参数错误退出码为 2。`usage` 命令和 `--keep` 已删除。

## 验证结果

全量测试命令：

```sh
PYTHONDONTWRITEBYTECODE=1 ~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11 -m unittest discover -q
```

实际结果 `Ran 176 tests in 51.029s`、`OK`。覆盖重复名称、整批非法选择、已撤下、未分配、中文依赖提示、依赖循环、同时撤下依赖方、文件引用提示、严格确认、过期状态、只读拒绝、批量撤下、撤销、恢复及失败回滚。旧数据库测试禁止打开模拟旧数据库，并核对其内容和修改时间不变。

独立接口验收命令：

```sh
PYTHONDONTWRITEBYTECODE=1 ~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11 scripts/verify-remove-usage.py --output /tmp/skill-pilot-remove-usage-acceptance.json
```

脚本在系统临时目录创建纯模拟数据源，再调用 `scripts/make-fake-home.py` 建模拟 home；没有从真实 home 复制数据。以 `BROWSER=/usr/bin/true` 启动自己的看板进程，用带两项认证请求头的请求验证，共 19 次请求全部符合预期：

| 接口 / 场景 | 实测结果 |
| --- | --- |
| `/api/table`、`/api/agents` | 200，用量字段不存在。 |
| `GET /api/cleanup` | 200，仅有约定列表字段；撤下和恢复后读回状态正确。 |
| `/api/cleanup/preview` | 200，中文依赖警告符合约定；未登记技能为 400。 |
| `/api/cleanup/apply` | 未确认有警告时 200 / `needs_confirmation`，不改动；过期状态为 409；确认后 200，有操作编号及过去时提示。 |
| `/api/undo` | 200；这次撤下撤销后，状态版本与操作前完全一致。 |
| `/api/withdrawn/restore` | 200；恢复原 agent 分配，已撤下列表清空。 |
| 已删除用量接口 | 两项均为 404。 |

临时看板进程已正常结束，它的临时状态文件和模拟 home 已清理。命令行脚本 `scripts/verify-m5-cleanup.py` 也通过：预览不写入、一次撤下一条操作记录、已撤下名单正确、恢复及两次撤销均精确还原。

其他检查：

- Python 后端和 `catalog.json` 中搜索 `usage`、`last_used`、用量字段、`kept_at` 和旧 60 天规则，零匹配。没有需解释的无关英文残留；测试及开发脚本中的旧名称只用于验证兼容、禁止访问旧文件和已删除接口。
- `node --check scripts/verify-m7-ui.cjs` 通过；改动格式检查通过。
- 按 AGENTS.md 的口径统计 `skill_pilot/` 下 `.py`、`.js`、`.html`：7,709 → 7,134 行，减少 575 行。此口径不含测试、开发脚本、CSS 和档案 JSON。
- 前端静态文件与开始时的内容指纹完全一致；已有暂存内容完全一致；本次没有暂存或提交。

## 留待范围外同步的内容

未修改 `skill_pilot/web/static/`、`docs/STATUS.md`、`PRODUCT.md`、`DESIGN.md`，这些属于交接中的前端和产品说明分工。没有替换启动器、重启真实看板或操作真实技能库。

仓库附带的 `skill/skill-pilot/SKILL.md` 不在本次允许修改的文件类别内，保持原样，但需要后续同步：第 3 行仍描述用量，第 12 行仍列 `usage`，第 14 行仍把 `cleanup --list` 解释为候选，第 22 行仍写旧 `cleanup --yes`、`--keep` 和 60 天规则。新命令行为以本报告为准；该说明文件未同步前，不能把它当作新版本命令说明。

用户的 `~/.skill-pilot/usage.sqlite` 没有被读取、改写或删除，留在原地，以后可以手动删掉。后端授权范围没有未完成项。
