# 交接：拿掉用量统计，清理改成手动挑（后端）

2026-10-13，Claude 写给 Codex。先读仓库根目录 `AGENTS.md`（安全边界、解释器、测试命令），需要背景再看 `docs/STATUS.md`。

## 用户的决定

用户不再要用量统计：Antigravity、Zcode 统计不到，其余几家的记录也覆盖不全，页面上大片“统计不到”。决定整个拿掉；清理页改成用户自己手动挑要撤下的技能，不再按“60 天没用”自动出名单。

## 分工

- **你（Codex）**：Python 后端、命令行、`profiles/catalog.json`、测试、开发脚本、`AGENTS.md` 里相关的描述。
- **Claude**：`skill_pilot/web/static/` 下的前端三个文件、`docs/STATUS.md`、`PRODUCT.md`、`DESIGN.md`。**这些文件你不要改**；前端会按下面的接口约定同时改。

## 要做的事

### 1. 删掉用量统计

- 删除 `skill_pilot/usage/` 整个包，以及所有引用：
  - `operations.table()`：技能不再带 `last_used`；`agents[]` 不再带 `usage_available`、`usage_note`；函数签名去掉 `database` 参数（调用方一起改）。
  - `agents.py` 的 overview：去掉 `usage_available`、`usage_note`。
  - `scan.py`：agent 条目去掉 `usage`、`usage_available`。
  - `profiles/catalog.json` 与 `profiles/__init__.py`：去掉档案的 `usage`、`usage_note`、`usage_reason` 字段，去掉对 usage reader 的校验，`config_paths`（或同类函数）里不再加 usage roots；用户自定义 agent 的默认档案也去掉这几项。**`manual_only`（手动调用标记）不是用量，保留。**
  - `web/server.py`：去掉 `usage_lock`、`usage_progress`、`usage_temporary`、`usage_database`，删除 `/api/usage`、`/api/usage-progress` 路由。
  - `cli.py`：删除 `usage` 命令。
  - 其他地方（`research.py` 等）如有引用一并清掉。
- **不要删除、读取或改写用户的 `~/.skill-pilot/usage.sqlite`**。它留在原地，报告里写一句“以后可以手动删掉”。

### 2. 清理改成手动挑

保留“已撤下的技能”和恢复（`cleanup.restore`、`/api/withdrawn/restore` 不变），撤下时记录的字段不变（`withdrawn`、`withdrawn_at`、`withdrawn_agents`、`withdrawn_entries`），撤销（`/api/undo`）照常可用。去掉 60 天规则、“留下”记录（keep）、候选名单、可省字数、覆盖说明，以及只为判断资格存在的代码（如 `installations()`）。

接口约定（前端按这个写，字段名请照抄；如有必须改动，在报告里写清）：

- `GET /api/cleanup` → `{"revision": str, "write_allowed": bool, "withdrawn": [{"name": str, "previous_agents": [agent_id], "withdrawn_at": str|null}]}`。不再有 `candidates`、`excluded`、`coverage`、`savings`、`checked_at`。不需要加锁，直接返回。
- `POST /api/cleanup/preview`，body `{"skills": [name]}` → `{"skills": [name], "message": "从所有 agent 撤下 N 个技能", "dependency_warnings": [str], "path_warnings": [{"file": str, "entries": [str]}], "revision": str}`。
  - `dependency_warnings`：撤下后仍分配着、且 `requires` 里有被撤下技能的技能，每条一句中文，例如 `「多智能体分派」还要用「营销技能路由」，撤下后它可能用不了。`。技能名优先用 `labels.json` 里的中文名（`operations.table()` 现在给的 `title` 就是），没有就用原名。
  - `path_warnings`：沿用 `operations.path_warnings()`，传入这次会消失的入口。
  - 校验：每个名字都在登记表里、至少分配给一个 agent、没有处于已撤下状态；否则 400 并给中文原因。
- `POST /api/cleanup/apply`，body `{"skills": [name], "revision": str, "confirmed": true}` → 和其他写入一样返回操作记录（至少 `id`、`message`）。`message` 用过去时：`撤下了 N 个技能`。`revision` 不对时照现有写入的方式拒绝；有 `dependency_warnings` 或 `path_warnings` 而 `confirmed` 不为 true 时，按现有模式返回需要确认。
- 命令行：`skill-pilot cleanup --list [--json]` 列出已撤下的技能；`skill-pilot cleanup SKILL [SKILL ...] [--yes] [--json]` 手动撤下（不带 `--yes` 只预览）；删除 `--keep`。`restore-withdrawn` 不变。

### 3. 测试与脚本

- 删除 `tests/test_usage.py`、`tests/test_usage_lock.py`；`tests/test_cleanup.py` 改成手动撤下的语义（预览、警告、应用、撤销、恢复、各种非法输入）；其他测试里引用用量字段的一并改掉。
- `scripts/` 里只为用量或旧清理存在的开发脚本（如 `verify-m5-usage.py`、`serve-fix-usage-lock-fixture.py`、`verify-fix-usage-lock-ui.cjs`、`verify-m5-cleanup.py`，以及 import 了被删模块的其他脚本）：已经过时的删掉，还在用的改掉，在报告里列清楚。
- 更新 `AGENTS.md` 里提到 `usage.sqlite`、`usage/`、“60 天清理”的描述（结构速览表、真实数据列表）。

## 验收

1. 全量测试通过：`PYTHONDONTWRITEBYTECODE=1 ~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11 -m unittest discover -q`。
2. `grep -rn "usage" skill_pilot --include='*.py'` 和 `catalog.json` 里不再有用量相关的引用（无关的英文单词除外，报告里说明）。
3. 在 `scripts/make-fake-home.py` 建的模拟 home 上，用脚本把这几个接口各走一遍并记录结果：`/api/table`、`/api/agents`、`GET /api/cleanup`、`/api/cleanup/preview`（含一个有依赖警告的例子）、`/api/cleanup/apply`、`/api/undo` 撤销这次撤下、`/api/withdrawn/restore`。请求需要带 `X-Skill-Pilot-Token` 和 `Origin`（见 `docs/generic-agents-frontend-report.md` 里的做法）。启动看板时设 `BROWSER=/usr/bin/true`，免得打开用户的浏览器；用完结束你自己起的进程。
4. 写报告 `docs/remove-usage-report.md`：删了哪些文件、实际的接口字段（逐字段）、命令行用法、测试结果、代码行数变化、没做或有疑问的地方。

## 边界

- 只在系统临时目录的模拟 home 上开发和验证；不要对真实 home 运行本工具（必须时带 `SKILL_PILOT_READ_ONLY=1`），不要碰用户正在运行的看板进程。
- 不要 `git add`、不要提交；你的改动保持未暂存。
- 只用标准库。
