# 勾选提速与助手通用化后端验收报告

日期：2026-10-13。范围：交接文件任务 A、任务 B 及第 6 节验收。

后端已完成快速分配预览、档案驱动的助手管理、发现建议、重复接入、退出管理、忽略建议和撤销。用户数据的模拟副本有 130 个登记技能；与导出的 main 比对，停用名单、可见名单、扫描、技能表和预览的原有结果一致。页面文件未改，下一轮页面接线仍需按下文使用新字段。

## 1. 交付范围和运行边界

- 工作目录保持 `~/Developer/skill-pilot-generic-agents`，分支保持 `generic-agents`。
- 没有暂存、提交或切换本项目分支；原已暂存的交接文件保持原样。
- 没有改 `skill_pilot/web/static/`，没有重建启动器，没有运行真实 home 的看板或命令行。
- 手测目录由 `scripts/make-fake-home.py` 生成；对照 main 从本工作区导出到系统临时目录运行。比较和计时均设置 `SKILL_PILOT_READ_ONLY=1`。
- 没有访问真实 AI 服务，没有调用 Codex、DSH、飞书更新或技能目录里的脚本。单测和冒烟中的技能仓库事务仅发生在临时目录。
- `registry.json`、用量账本和 `labels.json` 的格式保持原有结构。`agents.json` 仅新增可选 `custom`；旧文件可以直接读取。

主要文件：新增 `skill_pilot/agents.py`、`skill_pilot/profiles/catalog.json` 和两份通用助手测试；替换四份 Python 助手档案；通用化 `scan.py`、`sync.py`、`operations.py`、`store.py`、用量调度、模拟目录脚本及后台路由。更新与清理只调整它们调用的档案、停用信息和分配计划参数。

## 2. 档案格式

`catalog.json` 顶层为 `profiles`、`default_ignored`、`discovery_names`、`classified_roots`。后两项保留已知目录的显示名和只读/忽略分类，原来散落的目录判断集中到数据里。

每个档案字段如下。

| 字段 | 最终含义 |
| --- | --- |
| `id`, `name` | 稳定编号和显示名；现有用量编号不变 |
| `detect` | `{config_dirs: string[], apps: string[], cli: string|null}` |
| `skill_roots`, `link_root` | 相对 home 的读取目录、放置目录；后者必须是前者之一 |
| `nested` | 是否递归发现嵌套技能 |
| `readonly_roots` | `[[相对目录, 分类], ...]`，只看不改 |
| `activation` | `immediate` 或 `new_session` |
| `manual_only` | 支持 `disable-model-invocation`、`agents/openai.yaml` 标记 |
| `disable` | 停用适配器名或 `null`；现有适配器为 `codex_config` |
| `usage` | `{reader: 模块名, roots: 会话目录[]}` 或 `null` |
| `activation_note`, `disable_note`, `usage_note`, `visibility_note` | 保留现有诊断说明 |
| `usage_reason` | 用量读取未接入时给覆盖信息的原因 |

`profiles/__init__.py` 负责加载、合并和校验编号、相对路径、放置目录、嵌套开关、适配器和调用标记。软件自管目录和统一仓库不能成为自定义助手的技能根。

用户自定义档案存入 `agents.json.custom`，示例结构为：

```json
{
  "onboarded": ["codex", "claude", "antigravity", "dsh", "zcode"],
  "ignored": ["kimi"],
  "custom": [{
    "id": "zcode", "name": "Zcode",
    "detect": {"config_dirs": [".zcode"], "apps": [], "cli": null},
    "skill_roots": [".zcode/skills"], "link_root": ".zcode/skills",
    "nested": false, "readonly_roots": [],
    "activation": "new_session", "activation_note": "新会话生效",
    "manual_only": [], "disable": null,
    "disable_note": "没有独立停用设置", "usage": null,
    "usage_note": "统计不到：尚未接入这个助手的用量读取方式。",
    "usage_reason": "统计不到：尚未接入这个助手的用量读取方式。",
    "visibility_note": "按技能目录直属条目盘点；尚未核验用量记录。"
  }]
}
```

内置助手显示顺序固定为 Codex、Claude、Antigravity、DSH；自定义助手按接入顺序跟在后面。重新接入时编号不变，档案保留。

## 3. 放置、停用及退出管理的规则

### 3.1 放置与停用

读取者由已接入档案的 `skill_roots` 推导。非递归助手只认直属条目。分配时从接收助手的 `link_root` 贪心选取覆盖尚未覆盖助手最多的目录；相同覆盖数按档案顺序选取，结果路径排序。若该目录还被未分配助手读取，后者必须具备停用机制，否则在写文件之前报“X 和 Y 共用一个技能目录，没法只给 X”。

有停用机制的助手，只停用自己未被分配、但由某个已分配助手通过共用目录读取的技能。完全无人分配而仍留有条目的技能不会被工具新增停用；用户自行添加的停用项保持原样。

测试中原样保留旧 `assignment_entries` 为参照，四个助手的 16 种组合逐一一致。首次迁移继续保留原有条目、嵌套路径和兼容入口；补缺时优先兼容原有私有目录选择，避免改变首次迁移行为。

### 3.2 发现和接入

建议来自已知未接入档案，或隐藏助手目录下的 `skills` 目录（最多四段路径、直属有有效技能）。内置、插件、缓存、参考资料、备份及忽略目录不作为建议。用户数据的模拟副本包含 `.trae/skills` 和 `.zcode/skills`，各有 28 个直属入口，全部是对现有技能的引用；现在均已在实际副本上识别为建议。发现步骤只检查这些引用中的说明文件，不递归遍历引用目标。

接入输入支持编号，或 `{id?, name, skill_root}`。`skill_root` 可用相对路径、`~/...` 或 home 内绝对路径，最终档案存相对路径。共用技能根的已知档案必须一同选择，已接入的共用读取者视为已经选择。

预览逐技能给出 `linked`（已使用仓库）、`link`（同内容）、`import`（新技能）或 `choose`（内容不同）。不同内容默认保留仓库版本，提供文件对比；选择新助手版本会更新所有助手共用的内容，这一点在预览中写明。无效说明和失效入口原样保留。指向 home 之外的目录或内部链接会拒绝接入。

应用经 `_run` 加锁、复核 `revision` 和 `plan_id`、备份原目录和原文件、修改登记、核对实际名单与内容、记录操作。接入新内容保留所选文件，避免顺手规范化造成未选择的内容变化。选择仓库版本时原助手内容不变；明确选择新助手版本时，同名共用内容按选择更新，其余技能不变。

### 3.3 不再管理与撤销

至少留下一个助手。离开后从 `onboarded` 和技能分配中移除它；独用目录条目退出登记，但原文件和入口保留。自定义档案也保留，因此可重新接入。独用目录保留的入口仍使用仓库内容，后续这份技能的更新会反映到该目录；共用物理目录仍会随留下的助手的分配变化。

共用目录的取舍：仍被其他已分配助手使用的条目保留；不再有接收者的共用条目移除，以保持其余助手的名单和内容不变。预览给出 `removed_shared_entries` 和 `lost_skills`。例如 DSH 离开时，只给 DSH、通过共用目录提供的技能会列入它以后看不到的名单。Codex 离开而 DSH 留下时，保留相应共用入口及已有停用设置，不干扰 DSH。

接入、离开和忽略均能通过 `/api/undo` 撤销。涉及助手管理的记录须先撤销后续操作，再撤销较早的管理变更；原有无关技能之间的独立撤销仍保留。校验的指纹包含技能文件与模式、入口及其目标、登记、档案、配置和基准内容；操作记录、备份和仓库撤销历史会按设计新增，不算需要复原的用户技能状态。

### 3.4 用量读取

用量调度从档案加载读取器；文件名筛选、默认会话编号、逐行预筛及覆盖说明由读取器模块提供。原三份解析器保持原调用与去重逻辑、增量读取和账本编号。`usage: null` 的助手覆盖信息为 `available: false`，技能表显示“统计不到”，分配给它的技能不进入自动清理候选。

识别技能读取路径时，从所有档案和统一仓库建立目录清单；嵌套路径按 `nested` 处理，软件自管目录排除。原 Antigravity 原因句已移入档案，空读取器文件删除。

## 4. 页面交接：最终接口字段

### 4.1 公共约定

- 全部接口沿用本机地址、会话口令和来源校验；POST 仍需同源 `Origin`。
- 预览 POST 不要求 `confirmed`，不改用户文件。
- 新管理写接口必须携带 `confirmed: true`、非空 `revision`、`plan_id`。缺确认返回 400；只读返回 403；过期、目录冲突、格式问题返回 409 `{error}`。
- `revision` 从 `/api/table` 或 `/api/agents` 读取，分配预览不再返回它。
- 成功写入的共同字段为 `{id, message, backup_id}`，`id` 给 `/api/undo` 使用；各接口额外结果见下文。无变化操作可只返回 `message`。
- 字段里的助手一律用稳定 `id`；提示句直接显示，不从显示名反解编号。

### 4.2 `GET /api/table`

顶层：`home, revision, write_allowed, skills, agents, notes, new_skills, groups, labels_pending, llm_configured`。

`agents[]`：`id, name, activation`（保留旧文案）, `activation_text`（马上生效/新会话生效）, `usage_available, usage_note, count, description_chars`。

`skills[]`：`name, title, summary, description, description_chars, source, source_label, assigned_agents, requires, local_modified, last_used, cell_notes`。`last_used` 为 `{agent_id: 日期或提示句}`；`cell_notes` 为 `{agent_id: 提示句}`，无提示的格子不占键。

`notes` 为档案和实际放置规则推导的句子数组，直接显示。当前四助手只有“Codex 和 DSH 共用一个技能目录：只给 DSH 的技能，会在 Codex 里自动停用。”，不会错误提示只给 Codex 也必须给 DSH。

`groups[] = {name, skills: 技能编号[]}`。`new_skills[]` 为扫描技能条目，另加 `relative, assigned_agents`，其调用限制也是 `manual_only_for`。

### 4.3 分配接口

`POST /api/assignment/preview` 输入 `{changes: [{skill, agent, enabled: boolean}]}`。

返回全部字段：`changed, reasons, auto_added, dependency_warnings, path_warnings, message, assignments, after, cell_notes`。

- `assignments = {skill: agent_id[]}`，含完整草稿分配。
- `after = {agent_id: {count, description_chars}}`。
- `auto_added = [{skill, agent, because}]`，`because` 是直接需要该依赖的技能编号。
- `path_warnings = [{file, entries: string[]}]`；`dependency_warnings`、`reasons` 为句子数组。
- 草稿 `cell_notes = {skill: {agent_id: 句子}}`。
- **没有 `revision`**；预览不调用 `table`、`revision`、`new_skills` 或 `normalization.tree`。

`POST /api/assignment/apply` 输入 `{changes, revision, confirmed:true}`，返回共同写结果及 `reasons`。沿用应用时的完整版本校验。

### 4.4 `GET /api/scan`

顶层：`schema_version, generated_at, home, read_only, agents, roots, candidates, discovered_skill_roots, conflicts, source_copies, disabled, errors, count_basis, discovery_note`。

`discovered_skill_roots` 是额外识别出的、仅含引用入口的未知助手目录数组；供建议流程使用，既有 `candidates`（其他发现）保持原有盘点含义。

`disabled = {agent_id: {names, paths, error}}`；**删除 `codex_disabled`**。

根条目：`path, exists, entry_count, valid_count, manifest_count, broken_count, skills, errors, unresolved_links`；管理根另有 `category, readers`，其他发现另有 `default_hidden`。

有效技能条目：`name, directory_name, path, kind, link_target, resolved_path, broken, valid, description, model_invocable, manual_markers, manual_only_for, description_chars, version, digest, name_mismatch, warning`。`manual_only_for` 是助手编号数组；**删除 `codex_implicit_invocation`**。失效条目没有说明元数据、版本和调用标记，仍有 `manual_only_for: []`。`manual_markers` 是扫描证据，页面展示使用 `manual_only_for`。

已知助手保留原诊断字段 `id, name, config_dirs, apps, cli, status, evidence, skill_roots, readonly_roots, visible_skill_count, available_user_skill_count, manual_only_names, description_chars, visible_names, disabled_paths, visibility_note, activation, disable, usage`；新增档案字段 `detect, link_root, nested, manual_only, activation_note, disable_note, usage_note` 及 `activation_text, usage_available`（未接入用量的还带 `usage_reason`）。此接口的 `activation, disable, usage` 继续为诊断文案；可执行规则读取档案，而非这些诊断字段。

其他发现的助手仍为 `id, name, status, evidence, skill_roots, readonly_roots, visible_skill_count, description_chars, visibility_note`。新助手建议的可靠来源用下面的 `/api/agents`。

### 4.5 `GET /api/agents`

顶层：`revision, managed, suggested, ignored, must_select_together`。

- `managed[] = {id, name, skill_roots, activation_text, usage_available, usage_note, shared_with: agent_id[], can_leave}`。
- `suggested[] = {id, name, skill_roots, skill_count, reason}`。
- `ignored` 为编号数组；`must_select_together` 为共用根的编号分组数组。
- 尚未首次迁移时 `managed` 为空、`revision` 为 `null`；此时先用迁移接口。
- 所有 `skill_roots` 都是相对 home 的目录。选择建议时只需传它的 `id`。

### 4.6 接入接口

`POST /api/agents/join/preview` 输入 `{agents: [编号或{id?,name,skill_root}]}`。

返回：`plan_id, revision, agents, profiles, skills, conflicts, skipped, roots, before, must_select_together, read_only`。

- `profiles` 是将接入的完整档案；`agents` 是对应编号数组。
- `skills[] = {name, entries, assigned_agents, action, default, options, reason, comparison}`。
- `options[] = {id, path, origin, digest}`；`origin` 为 `store` 或 `incoming`。仓库选项编号固定为 `store`；传入版本使用预览给出的编号。
- `conflicts` 为需要选择的同一批技能行；`comparison` 是复用文件对比的全文。
- `skipped[] = {path, reason}`。
- `before = {agent_id: {available, automatic, versions: {skill: 内容摘要[]}}}`。

`POST /api/agents/join/apply` 输入 `{agents, plan_id, revision, confirmed:true, choices?: {skill: option_id}}`。

成功返回共同写结果及 `agents, after`；`after` 结构同 `before`。省略 `choices` 取每行 `default`。应用会在锁内重新生成计划并核对，不依赖后台缓存的预览对象。

### 4.7 不再管理和忽略建议

`POST /api/agents/leave/preview` 输入 `{agent}`，返回 `plan_id, revision, agent, name, remaining, kept_entries, removed_shared_entries, lost_skills, changed, before, message`。内部登记计划不返回页面。

`POST /api/agents/leave/apply` 输入 `{agent, plan_id, revision, confirmed:true}`，成功返回共同写结果及 `agent, lost_skills`。

`POST /api/agents/ignore/preview` 输入 `{agent}`，返回 `agent, revision, message, plan_id`。

`POST /api/agents/ignore/apply` 输入 `{agent, plan_id, revision, confirmed:true}`，成功返回共同写结果及 `agent`。忽略后不再出现在 `suggested`，可撤销；显式重新接入已保存档案会移出忽略名单。

### 4.8 保留的首次迁移接口

`GET /api/migration/plan?agents=id,id`：没有登记表时返回 `plan_id, home, agents, skills, conflicts, before, after, roots, skipped, read_only, write_allowed, agent_catalog, must_select_together`。`agent_catalog` 是完整档案数组，列表和必须同选分组从档案来。

首次迁移的 `skills[]` 保留 `name, options, default, reason, source, source_needs_confirmation, entries, assigned_agents, name_mismatch`；`options[]` 保留 `id, paths, path, version, source_match, match_reason, modified`。

`GET /api/migration/compare?agents=...&plan_id=...&name=...` 返回 `{comparison}`。

`POST /api/migration/apply` 输入 `{plan_id, agents, choices?, sources?, confirmed:true}`。首次成功返回 `backup_id, message, changed_entries, after, normalization`；仍可用备份还原。已有登记表时 `store.preview/apply` 转入可重复接入，建议页面统一使用新的 join 接口，避免混用首次迁移的 `options` 格式。已迁移场景的比较直接读取 join 预览行的 `comparison`。

### 4.9 其余接口（沿用原合同）

下面列全保留接口，便于页面不漏接。共同写结果仍为 `{id,message,backup_id}`；本轮只读写新助手功能，不改变这些功能的产品逻辑。

| 接口 | 输入及最终返回字段 |
| --- | --- |
| `GET /api/status` | `{pid, read_only}` |
| `POST /api/heartbeat?page=...&closing=...` | 返回 `{ok}` |
| `GET /api/log` | 记录数组；每项 `id,time,message,agents,backup_id,skills,undo_of,after_revision,extra_paths,extra_state,undone`；新记录带 `agents_changed`，旧记录可缺省 |
| `POST /api/undo` | 输入 `id,revision,confirmed:true`；返回共同写结果 |
| `GET /api/backups` | 备份数组；`backup_id,home,plan_id,paths,existing,before,protected` |
| `POST /api/restore` | 输入 `backup_id,confirmed:true`；返回 `backup_id,restored,message` |
| `POST /api/dependencies` | 输入 `decisions:[{skill,requires,confirmed}],revision,confirmed:true`；返回共同写结果 |
| `POST /api/install/inspect` | 输入 `url`；返回 `id,temporary,url,skills,conflicts`；技能项为 `name,path,repository_path,digest,description,source` |
| `POST /api/incoming/compare` | 输入 `name` 加 `review_id` 或 `relative`；返回 `{comparison}` |
| `POST /api/install/apply` | 输入 `review_id,skills,agents?,revision,confirmed:true`；返回共同写结果及 `reasons,normalization`，同名时返回 `conflicts,message` |
| `POST /api/absorb` | 输入 `all?,selected?,revision,confirmed:true`；返回共同写结果及 `normalization`，或 `new_skills,message` / `conflicts,message` / `message` |
| `GET /api/usage-progress` | `running,done,total` |
| `GET /api/usage` | `coverage,skills,scanned_bytes,checked_at,ledger,persistent`；覆盖项为 `available,message,files,earliest,latest,errors`；技能项为 `name,last_used,last_agent,last_used_at,sessions_30d,first_seen` |
| `GET /api/cleanup` | `candidates,excluded,withdrawn,coverage,savings,revision,checked_at,write_allowed,ledger,scanned_bytes`；行包含 `name,assigned_agents,description_chars,last_used,last_agent,last_used_at,installed_at,installation_evidence,kept_at,path_warnings,reasons`，已撤下行另有 `previous_agents,withdrawn_at` |
| `POST /api/cleanup/apply` | 输入 `keep?,revision,candidates,confirmed:true`；返回共同写结果及 `withdrawn,kept,path_warnings,dependency_warnings`，或 `message` |
| `POST /api/withdrawn/restore` | 输入 `skills,revision,confirmed:true`；返回共同写结果及 `restored,reasons` |
| `GET /api/settings` / `POST /api/settings/llm` | 后者输入 `provider,base_url,model,api_key`；两者返回 `{llm:{provider,base_url,model,has_key,key_tail}}`，没有完整 key |
| `POST /api/settings/llm/test` | 输入对象；返回 `ok,message` |
| `GET /api/labels/progress` / `POST /api/labels/run` | 后者输入 `mode: missing/all`；返回 `running,done,total,message,error` |
| `POST /api/updates/check` / `GET /api/updates/progress` | 前者输入 `withdrawn?`；返回更新检查对象（无任务时 GET 为 `null`），字段见下段 |
| `POST /api/updates/explain` | 输入 `review_id,name`；返回 `{explanation}` |
| `POST /api/updates/calibrate` | 输入 `review_id,name`；返回文件比较对象，字段见下段 |
| `POST /api/updates/merge` | 输入 `review_id,name`；返回文件比较字段及 `name,explanation,merge_id` |
| `POST /api/updates/apply` | 输入 `review_id,skills,choices?,merge_ids?,confirm_lark_cli?,confirmed:true`；返回共同写结果及 `normalization` |

更新检查对象：`id,withdrawn,checked_at,write_allowed,skills,errors,lark,running,done,current,update_count,latest_count,api_calls,total,remaining,repositories`。技能行基础为 `name,source,has_update,local_modified,status`；按来源/检查结果可含 `local_tree_sha,commit,tree_sha,base_tree_sha,baseline_uncertain,normalized_tree_sha,normalization,base_commit,similarity,updated_at,your_changes,blocked_reason,error` 及比较字段。内部 `_...` 和 `package` 不公开。文件比较字段为 `file_count,added_lines,removed_lines,changed_files,script_files,risk_hits,comparison`；`your_changes` 使用同一结构。

## 5. 验收方法与原始输出

固定解释器：`~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11`。新增测试覆盖 16 种分配组合、第五助手两种冲突选择、接入/撤销/退出/重新接入、共用目录与停用、忽略建议、过期与失败恢复、只读与 HTTP 确认、预览不调用耗时函数、既有助手重新接入提示句准确性、空目录助手的接入/退出/撤销，以及全为引用入口的新助手发现与接入。

### 5.1 全量测试

命令：`PYTHONDONTWRITEBYTECODE=1 <解释器> -m unittest discover -q`。

```text
----------------------------------------------------------------------
Ran 181 tests in 47.071s

OK
```

### 5.2 具体助手判断检查

运行交接文件第 3.2 节的原样 `grep -rnE ... skill_pilot --include='*.py'`，仅命中两个允许文件：

```text
skill_pilot/codex_config.py:6:CONFIG_PATH = '.codex/config.toml'
skill_pilot/research.py:18:        auth = Path(pwd.getpwuid(os.getuid()).pw_dir) / '.codex/auth.json'
```

### 5.3 用户数据模拟副本的对照和计时

模拟目录：`/private/var/folders/ms/q91q3xp96t397xghv571lx240000gn/T/skill-pilot-home-309co8jy`。它由更新后的模拟脚本生成，未复制设置文件和私密会话。

main 导出目录：`/private/var/folders/ms/q91q3xp96t397xghv571lx240000gn/T/skill-pilot-main-8qbcetdf`。同一模拟 home、同一解释器，分别只读执行 `work/generic-agents/collect.py`，完整输出保存于 `main.json`、`new.json`。

比较脚本 `work/generic-agents/compare.py` 逐项投影到原有合同：忽略生成时间和新增字段，将旧专用字段映射为新通用字段；提示统计口径的“Codex 停用项”映射为“助手停用项”。助手数组按编号对齐再比较，显示顺序的变化是第 3.8 节明确要求。技能名单、内容摘要、数量、说明字数、原有停用项、来源和标签均未屏蔽。技能表的 `revision` 也一致。预览仅去掉已要求删除的 `revision` 后比较原有字段。

main 的原始计时输出：

```text
{"code": "/private/var/folders/ms/q91q3xp96t397xghv571lx240000gn/T/skill-pilot-main-8qbcetdf", "registered": 130, "agents": ["antigravity", "claude", "codex", "dsh"], "disabled": {"codex": ["context-pack", "nas-knowledge-base"]}, "preview_assign_ms": [2597.038, 2549.005, 2559.001, 2527.775, 2551.969, 2555.577, 2568.042], "max_ms": 2597.038}
```

新代码的原始计时输出及比对结果：

```text
{"code": "~/Developer/skill-pilot-generic-agents", "registered": 130, "agents": ["codex", "claude", "antigravity", "dsh"], "disabled": {"codex": ["context-pack", "nas-knowledge-base"]}, "preview_assign_ms": [18.929, 17.376, 18.152, 18.084, 17.623, 18.098, 18.603], "max_ms": 18.929}
```

```text
disabled: MATCH
visibility: MATCH
scan: MATCH
table: MATCH
preview: MATCH
preview_assign < 100 ms: True
```

同一用户数据副本的发现输出：

```text
{"suggested": [{"id": "trae", "name": "Trae", "skill_roots": [".trae/skills"], "skill_count": 28, "reason": "这个目录直属放着技能文件夹，可能属于一个新助手。"}, {"id": "zcode", "name": "Zcode", "skill_roots": [".zcode/skills"], "skill_count": 28, "reason": "这个目录直属放着技能文件夹，可能属于一个新助手。"}]}
```

### 5.4 后台 HTTP 冒烟原始输出

`work/generic-agents/http-smoke.py` 先用模拟脚本复制临时样本，随后通过 `SKILL_PILOT_HOME=<模拟目录> BROWSER=true ... -m skill_pilot ui --json` 启动独立后台。按请求完成发现、接入假的第五助手、分配 alpha、撤销分配、退出管理；检查 welcome 入口原样保留。未接触用户真实看板。以下为实际收到的完整响应，未列出后台会话口令；复制清单里的一条本机私人技能目录已改成泛指。脚本只结束它自己创建的后台。

```text
make-fake-home: {
  "home": "/private/var/folders/ms/q91q3xp96t397xghv571lx240000gn/T/skill-pilot-generic-http-7kyy6v39/home",
  "copied_roots": [
    ".skill-pilot",
    ".agents/skills",
    ".claude/skills",
    ".codex/skills",
    ".dsh/skills",
    ".gemini/config/skills",
    "某个本机私人技能目录",
    "Documents/Example-Notes/samples/simon-neat",
    "Documents/Example-Notes/samples/agency-router",
    "Documents/Example-Notes/samples/marketing-router",
    ".agents/.skill-lock.json"
  ],
  "link_dependencies": [],
  "links_within_home": true,
  "config_contains_only_skills": true
}
{"method": "GET", "path": "/api/agents", "status": 200, "response": {"revision": "6996468373f35bfe48581fde7932b6d16904c575111f6f4557c56807f4edfb2e", "managed": [{"id": "codex", "name": "Codex", "skill_roots": [".agents/skills", ".codex/skills"], "activation_text": "新会话生效", "usage_available": true, "usage_note": "会话文件中的实际工具调用", "shared_with": ["dsh"], "can_leave": true}, {"id": "claude", "name": "Claude", "skill_roots": [".claude/skills"], "activation_text": "马上生效", "usage_available": true, "usage_note": "会话文件中的技能调用或读取", "shared_with": [], "can_leave": true}, {"id": "antigravity", "name": "Antigravity", "skill_roots": [".gemini/config/skills"], "activation_text": "新会话生效", "usage_available": false, "usage_note": "统计不到；隔离复现未得到可靠读取步骤", "shared_with": [], "can_leave": true}, {"id": "dsh", "name": "DSH", "skill_roots": [".dsh/skills", ".agents/skills"], "activation_text": "马上生效", "usage_available": true, "usage_note": "会话格式已验证，v3/v4 按会话去重；实际覆盖见用量页", "shared_with": ["codex"], "can_leave": true}], "suggested": [{"id": "demo", "name": "Demo", "skill_roots": [".demo/skills"], "skill_count": 1, "reason": "这个目录直属放着技能文件夹，可能属于一个新助手。"}], "ignored": ["kimi"], "must_select_together": [["codex", "dsh"]]}}
{"method": "POST", "path": "/api/agents/join/preview", "status": 200, "response": {"plan_id": "eb01fe65df1d5b705f52239e851cb4a13f826f81c2782e4085cd4e1ec02be35e", "revision": "6996468373f35bfe48581fde7932b6d16904c575111f6f4557c56807f4edfb2e", "agents": ["demo"], "profiles": [{"id": "demo", "name": "Demo", "detect": {"config_dirs": [".demo"], "apps": [], "cli": null}, "skill_roots": [".demo/skills"], "link_root": ".demo/skills", "nested": false, "readonly_roots": [], "activation": "new_session", "activation_note": "新会话生效", "manual_only": [], "disable": null, "disable_note": "没有独立停用设置", "usage": null, "usage_note": "统计不到：尚未接入这个助手的用量读取方式。", "usage_reason": "统计不到：尚未接入这个助手的用量读取方式。", "visibility_note": "按技能目录直属条目盘点；尚未核验用量记录。"}], "skills": [{"name": "welcome", "entries": [".demo/skills/welcome"], "assigned_agents": ["demo"], "action": "import", "default": "96eacf82f291dde1692259a475643d584a2a2b405be76cd02ae40592c4fd6775", "options": [{"id": "96eacf82f291dde1692259a475643d584a2a2b405be76cd02ae40592c4fd6775", "path": "/private/var/folders/ms/q91q3xp96t397xghv571lx240000gn/T/skill-pilot-generic-http-7kyy6v39/home/.demo/skills/welcome", "origin": "incoming", "digest": "26c25a6a239acae88b280a86d87527bfbf403d348093ab0e95eb5f6f11500591"}], "reason": "仓库里还没有，收进仓库。", "comparison": ""}], "conflicts": [], "skipped": [], "roots": [".demo/skills"], "before": {"codex": {"available": ["alpha", "beta", "skill-pilot"], "automatic": ["alpha", "beta", "skill-pilot"], "versions": {"alpha": ["1ee0235bc8cf5dbd57a30b14d0fdb320a66090f372683c38d5b76d6803d85827"], "beta": ["ce297b1b3c4336dd8c42e8c2eb66e62e1bede82e3c0dd3324ff0be3ec14a524d"], "skill-pilot": ["a7893072f72ca0712250cb09fe12240b7a3545198643df16cfa453e147405461"]}}, "claude": {"available": ["skill-pilot"], "automatic": ["skill-pilot"], "versions": {"skill-pilot": ["a7893072f72ca0712250cb09fe12240b7a3545198643df16cfa453e147405461"]}}, "antigravity": {"available": ["skill-pilot"], "automatic": ["skill-pilot"], "versions": {"skill-pilot": ["a7893072f72ca0712250cb09fe12240b7a3545198643df16cfa453e147405461"]}}, "dsh": {"available": ["skill-pilot"], "automatic": ["skill-pilot"], "versions": {"skill-pilot": ["a7893072f72ca0712250cb09fe12240b7a3545198643df16cfa453e147405461"]}}, "demo": {"available": ["welcome"], "automatic": ["welcome"], "versions": {"welcome": ["26c25a6a239acae88b280a86d87527bfbf403d348093ab0e95eb5f6f11500591"]}}}, "must_select_together": [["codex", "dsh"]], "read_only": true}}
{"method": "POST", "path": "/api/agents/join/apply", "status": 200, "response": {"agents": ["demo"], "after": {"codex": {"available": ["alpha", "beta", "skill-pilot"], "automatic": ["alpha", "beta", "skill-pilot"], "versions": {"alpha": ["1ee0235bc8cf5dbd57a30b14d0fdb320a66090f372683c38d5b76d6803d85827"], "beta": ["ce297b1b3c4336dd8c42e8c2eb66e62e1bede82e3c0dd3324ff0be3ec14a524d"], "skill-pilot": ["a7893072f72ca0712250cb09fe12240b7a3545198643df16cfa453e147405461"]}}, "claude": {"available": ["skill-pilot"], "automatic": ["skill-pilot"], "versions": {"skill-pilot": ["a7893072f72ca0712250cb09fe12240b7a3545198643df16cfa453e147405461"]}}, "antigravity": {"available": ["skill-pilot"], "automatic": ["skill-pilot"], "versions": {"skill-pilot": ["a7893072f72ca0712250cb09fe12240b7a3545198643df16cfa453e147405461"]}}, "dsh": {"available": ["skill-pilot"], "automatic": ["skill-pilot"], "versions": {"skill-pilot": ["a7893072f72ca0712250cb09fe12240b7a3545198643df16cfa453e147405461"]}}, "demo": {"available": ["welcome"], "automatic": ["welcome"], "versions": {"welcome": ["26c25a6a239acae88b280a86d87527bfbf403d348093ab0e95eb5f6f11500591"]}}}, "id": "33f080140ee61a9a0ec22178623a2bf690bb880b", "message": "接入了 Demo", "backup_id": "20261013T112157214883"}}
{"method": "POST", "path": "/api/assignment/preview", "status": 200, "response": {"changed": ["alpha"], "reasons": [], "auto_added": [], "dependency_warnings": [], "path_warnings": [], "message": "将给 Demo 加 1 个", "assignments": {"alpha": ["codex", "demo"], "beta": ["codex"], "skill-pilot": ["antigravity", "claude", "codex", "dsh"], "welcome": ["demo"]}, "cell_notes": {"alpha": {}, "beta": {}, "skill-pilot": {}, "welcome": {}}, "after": {"codex": {"count": 3, "description_chars": 64}, "claude": {"count": 1, "description_chars": 56}, "antigravity": {"count": 1, "description_chars": 56}, "dsh": {"count": 1, "description_chars": 56}, "demo": {"count": 2, "description_chars": 11}}}}
{"method": "POST", "path": "/api/assignment/apply", "status": 200, "response": {"reasons": [], "id": "fbe971d4cbe1191c0e0054c74981872a753b24a9", "message": "将给 Demo 加 1 个", "backup_id": "20261013T112157332556"}}
{"method": "POST", "path": "/api/undo", "status": 200, "response": {"id": "683873d7cfb612005463ff7d2ca72bd4bf31e285", "message": "撤销：将给 Demo 加 1 个", "backup_id": "20261013T112157441510"}}
{"method": "POST", "path": "/api/agents/leave/preview", "status": 200, "response": {"plan_id": "7665c038768d542489e75f8008a77a25a400b7e382022ac6e40e303629fc2f9d", "revision": "14ec75b0adc6573f592ccba3be5dcb9383d05d4d771e113329e9f3a490dcd241", "agent": "demo", "name": "Demo", "remaining": ["codex", "claude", "antigravity", "dsh"], "kept_entries": [".demo/skills/welcome"], "removed_shared_entries": [], "lost_skills": [], "changed": ["welcome"], "before": {"codex": {"available": ["alpha", "beta", "skill-pilot"], "automatic": ["alpha", "beta", "skill-pilot"], "versions": {"alpha": ["1ee0235bc8cf5dbd57a30b14d0fdb320a66090f372683c38d5b76d6803d85827"], "beta": ["ce297b1b3c4336dd8c42e8c2eb66e62e1bede82e3c0dd3324ff0be3ec14a524d"], "skill-pilot": ["a7893072f72ca0712250cb09fe12240b7a3545198643df16cfa453e147405461"]}}, "claude": {"available": ["skill-pilot"], "automatic": ["skill-pilot"], "versions": {"skill-pilot": ["a7893072f72ca0712250cb09fe12240b7a3545198643df16cfa453e147405461"]}}, "antigravity": {"available": ["skill-pilot"], "automatic": ["skill-pilot"], "versions": {"skill-pilot": ["a7893072f72ca0712250cb09fe12240b7a3545198643df16cfa453e147405461"]}}, "dsh": {"available": ["skill-pilot"], "automatic": ["skill-pilot"], "versions": {"skill-pilot": ["a7893072f72ca0712250cb09fe12240b7a3545198643df16cfa453e147405461"]}}, "demo": {"available": ["welcome"], "automatic": ["welcome"], "versions": {"welcome": ["26c25a6a239acae88b280a86d87527bfbf403d348093ab0e95eb5f6f11500591"]}}}, "message": "不再管理 Demo；独用目录里的技能原样保留。"}}
{"method": "POST", "path": "/api/agents/leave/apply", "status": 200, "response": {"agent": "demo", "lost_skills": [], "id": "01d92fef4e0edb9f3b31af91a2f95d1563f23e52", "message": "不再管理 Demo；独用目录里的技能原样保留。", "backup_id": "20261013T112157588796"}}
{"method": "GET", "path": "/api/agents", "status": 200, "response": {"revision": "9892eea93dd1c7308a76483b8e3738bdebce9ca9975b94267e52f79191c1b297", "managed": [{"id": "codex", "name": "Codex", "skill_roots": [".agents/skills", ".codex/skills"], "activation_text": "新会话生效", "usage_available": true, "usage_note": "会话文件中的实际工具调用", "shared_with": ["dsh"], "can_leave": true}, {"id": "claude", "name": "Claude", "skill_roots": [".claude/skills"], "activation_text": "马上生效", "usage_available": true, "usage_note": "会话文件中的技能调用或读取", "shared_with": [], "can_leave": true}, {"id": "antigravity", "name": "Antigravity", "skill_roots": [".gemini/config/skills"], "activation_text": "新会话生效", "usage_available": false, "usage_note": "统计不到；隔离复现未得到可靠读取步骤", "shared_with": [], "can_leave": true}, {"id": "dsh", "name": "DSH", "skill_roots": [".dsh/skills", ".agents/skills"], "activation_text": "马上生效", "usage_available": true, "usage_note": "会话格式已验证，v3/v4 按会话去重；实际覆盖见用量页", "shared_with": ["codex"], "can_leave": true}], "suggested": [{"id": "demo", "name": "Demo", "skill_roots": [".demo/skills"], "skill_count": 1, "reason": "已知助手的技能目录已存在。"}], "ignored": ["kimi"], "must_select_together": [["codex", "dsh"]]}}
HTTP smoke: PASS; private skill link retained; assignment undo restored; managed roster restored.
temporary dashboard stopped; home=/private/var/folders/ms/q91q3xp96t397xghv571lx240000gn/T/skill-pilot-generic-http-7kyy6v39/home
```

### 5.5 文件范围核对

页面目录没有变动；当前已暂存文件仍仅为 `docs/handoff-generic-agents.md`；分支仍为 `generic-agents`。代码检查没有空白错误。规定的代码量检查结果为 7,174 行（含未改的页面和读取器脚本，不含档案 JSON、测试和报告）。

## 6. 保留的流程与未做部分

- 首次迁移保留 `store.preview/apply`，以保留已有来源证明、选版及规范化行为，已去掉具体助手分支；后续接入使用 `_run`。首次迁移继续用原来的备份还原方式，后续管理用操作记录撤销。
- 页面接线、视觉验收和真实安装没有做，属于下一轮范围。旧页面里的助手列、停用字段以及预览 `revision` 依赖需按第 4 节更新后才是完整用户体验。
- 没有给 Trae、Zcode、Cursor 等新增用量读取器，也没有添加调研按钮。没有优化应用耗时。
- 本轮验收使用模拟目录；真实助手新会话里的运行效果没有实测。它们的原四助手规则由 16 组合测试、现有全量测试和 130 技能模拟数据对照证明兼容。
- 对照与冒烟脚本、完整 JSON、原始日志保留于本工作区的 `work/generic-agents/`（已被项目忽略），没有放入待暂存文件。

## 7. 主控验收修复：接入时消除绕道链接

日期：2026-10-13。本次增量只修改 `skill_pilot/agents.py`、`tests/test_generic_agents.py` 和本报告；核对脚本及输出放在已忽略的 `work/generic-agents/`。

### 7.1 问题与修复

原接入逻辑使用 `path.resolve() == target` 判断是否跳过入口替换。这会把 `.zcode/skills/x -> ../../.agents/skills/x -> .skill-pilot/store/x` 也当成已完成的入口。后续撤下其他助手时，共用入口先被移除，新助手入口随即失效，分配校验报“技能入口出现了外部改动，请先收编或刷新后再应用。”；退出管理后保留的入口也有同样的问题。

现在与首次迁移的 `sync.reconcile()` 一致，只有链接原文恰好等于仓库绝对路径时才跳过替换；其余最终使用仓库的入口改写为直接指向 `<模拟 home>/.skill-pilot/store/<技能名>`。预览仍返回 `action: linked`、`default: store`，不增加冲突选择；需要改写时，`reason` 为“已经使用仓库里的版本，接入时会改为直接指向统一仓库并登记分配。”。已经采用仓库绝对直连的条目保留原有说明和写入行为。接口字段不变。

接入继续使用原事务、整目录备份和写后名单/内容校验；撤销使用原备份恢复相对链接原文，没有修改分配、退出管理、撤销或首次迁移代码。

### 7.2 新增回归测试与全量结果

三个新增测试均在系统临时目录中构造 `.zcode/skills/x -> ../../.agents/skills/x`，中间入口直接指向统一仓库；`x` 最初分配给现有四个助手。

- `test_indirect_link_can_be_assigned_only_to_new_assistant`：接入后一次从现有四个助手撤下 `x`，仅留给 Zcode；操作成功、共用入口移除、Zcode 仍可读取原内容，登记与直连目标正确。
- `test_indirect_link_survives_departure_and_other_assignments_removed`：Zcode 退出管理后，再从现有四个助手撤下 `x`；登记不再包含 Zcode 的入口，共用入口移除，留下的直连仍有效且内容不变。
- `test_indirect_link_preview_and_exact_enrollment_undo`：预览不写文件，仍算 `linked` 并说明直连；接入后原四助手可见结果不变，撤销后链接原文精确恢复为 `../../.agents/skills/x`，整体指纹与接入前一致。

修复前运行这三个用例，结果为 `FAILED (failures=2, errors=1)`；第一个用例复现主控的同一条分配错误。修复后的定向结果：

```text
----------------------------------------------------------------------
Ran 3 tests in 1.502s

OK
```

全量命令：`PYTHONDONTWRITEBYTECODE=1 ~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11 -m unittest discover -q`。原始输出保存于 `work/generic-agents/indirect-links-tests.txt`：

```text
----------------------------------------------------------------------
Ran 184 tests in 52.940s

OK
```

### 7.3 原四助手结果对照

沿用第 5.3 节的 130 技能模拟副本，修复前后分别以 `SKILL_PILOT_READ_ONLY=1` 运行原 `collect.py`，再用原 `compare.py` 比较。输出保存于 `indirect-links-before.json`、`indirect-links-after.json`、`indirect-links-comparison.txt`；未改写该模拟副本。原四助手的停用名单、可见名单及内容、扫描、技能表与分配预览全部一致；16 种分配组合测试也在本次全量测试中通过。

```text
disabled: MATCH
visibility: MATCH
scan: MATCH
table: MATCH
preview: MATCH
preview_assign < 100 ms: True
```

本次修复后七次分配预览耗时为 `[22.398, 22.114, 21.159, 20.109, 20.412, 20.894, 19.635]` 毫秒，最大 `22.398` 毫秒。

### 7.4 28 个绕道入口的模拟副本核对

使用 `scripts/make-fake-home.py` 从第 5.3 节已有的模拟 home 再复制一份到系统临时目录，没有从真实 home 运行或写入。核对脚本为 `work/generic-agents/indirect-links-smoke.py`，原始输出保存于同目录的 `indirect-links-smoke.txt`。

副本中 Trae 的 28 个入口原文全部为 `../../.agents/skills/<名字>`。两轮接入都预览为 28 个 `linked`，并在接入后逐项核对绝对直连目标；原四助手的可见名单、自动调用名单与内容完全不变。第一轮将 `lark-approval` 只留给 Trae，第二轮先退出 Trae 管理、再从原四助手撤下该技能，两轮都成功且技能内容可读。按顺序撤销后，28 个入口的原文和接入前指纹全部恢复；作为复制来源的旧模拟 home 指纹也保持不变。

```text
{"home": "/private/var/folders/ms/q91q3xp96t397xghv571lx240000gn/T/skill-pilot-indirect-links-kl132n_s/home", "preview_linked": 28, "direct_links_after_join": 28, "original_four_unchanged": true, "trae_only_assignment": "PASS", "leave_then_withdraw_link_valid": "PASS", "all_original_link_text_restored": true, "undo_fingerprint_matches": true, "source_simulation_unchanged": true}
```

本次未改 `skill_pilot/web/static/`，未暂存或提交项目文件，未运行真实看板、重建启动器或对真实 home 执行本工具。原有工作区改动保留。

## 8. 统一用户用词为 agent

2026-10-13，按用户确认，将 8 个后端 Python 文件中 41 处面向看板或命令行的报错、提示、确认说明和档案说明文字里的“助手”统一替换为 `agent`，与相邻中文之间保留一个半角空格。同步更新 `tests/test_cleanup.py`、`tests/test_generic_agents.py` 的相关断言和“新 agent 版本”样本。已核对 `skill_pilot/profiles/catalog.json`，其中原本没有“助手”，无需修改；`labels.py` 中仅发给 AI 服务的内部提示不属于用户输出，保留原样。

逐项对照已暂存基础，确认本次后端及测试改动只涉及字符串用词和对应空格，接口字段及程序结构保持不变；中文与 `agent` 直接粘连的检查通过。全量测试命令为 `PYTHONDONTWRITEBYTECODE=1 ~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11 -m unittest discover -q`，结果：

```text
----------------------------------------------------------------------
Ran 184 tests in 61.407s

OK
```

本次未改页面目录或 `skill/skill-pilot/SKILL.md`，未对真实 home 运行本工具；本次改动保留在未暂存状态，已暂存基础保持原样。收尾时页面目录另有并行执行者的未暂存改动，已原样保留。空白格式检查通过。

## 9. 主控验收修复：忽略列表与用户提示（B1、B2）

日期：2026-10-13。本次增量修改 `skill_pilot/agents.py`、`skill_pilot/profiles/__init__.py`、`skill_pilot/profiles/catalog.json`、两份通用 agent 测试和本报告。

### 9.1 不再管理、忽略与重新接入

`leave/apply` 除移出 `onboarded`，也把该 agent 的编号加入 `agents.json.ignored`，保留原有顺序并去重。`suggested` 的过滤规则保持不变，所以退出管理后不再弹出新 agent 提示。撤销继续使用原来的整份文件备份，接入名单、忽略名单和技能入口一并恢复；重新接入沿用原逻辑，从 `ignored` 移除该编号。

首次发现就被忽略的新 agent 原本还没有保存档案。现在忽略操作会在同一事务中保存它的保守默认档案到 `agents.json.custom`，便于在忽略列表展示并按编号重新接入；忽略预览仍只读，内部 `_profile` 不出现在 HTTP 返回中，撤销忽略也会恢复原来的档案数据。

`agents.json.ignored` 仍保存编号数组，兼容原有数据。**`GET /api/agents` 返回的 `ignored` 改为对象数组**，元素字段如下；这替代第 4 节中该字段的旧说明，其余接口字段不变。

| 字段 | 内容 |
| --- | --- |
| `id` | 用于 `join/preview` 和 `join/apply` 的 agent 编号 |
| `name` | 已保存的显示名 |
| `skill_roots` | 档案中的技能目录，仍为相对 home 的字符串数组 |
| `skill_count` | 这些目录里有效技能条目的数量，扫描时扣除软件自己管理的目录 |
| `reason` | 固定为“你之前选了忽略或不再管理。” |

只返回未接入、已有档案且至少一个技能目录仍是文件夹的 agent。没有档案、目录已不存在或路径只是普通文件的条目不展示；默认忽略但没有档案和目录的 Kimi 不展示。已有档案且目录为空的 agent 仍可接入，返回 `skill_count: 0`。忽略列表中的编号可直接提交接入预览，无需先撤销忽略或退出管理。

测试中的退出管理返回示例：

```json
{
  "ignored": [{
    "id": "zcode",
    "name": "Zcode",
    "skill_roots": [".zcode/skills"],
    "skill_count": 3,
    "reason": "你之前选了忽略或不再管理。"
  }]
}
```

### 9.2 用户提示

- 不再管理时，没有共用目录技能损失的说明改为“不再管理 X；它文件夹里的技能原样留着，照常能用。”，预览、应用结果和操作记录一致；有技能损失时继续列出具体技能。
- Antigravity 的 `usage_note` 和 `usage_reason` 均改为“统计不到用量：还没找到读取它使用记录的办法。”。
- 自定义档案默认的这两个字段均改为“统计不到用量：还没接入它的使用记录。”。
- `agents.py` 中另外两句带“编号”“档案”的报错改为选择 agent、填写名字和技能文件夹的说法；重接入信息不匹配时仍给出已保存的显示名。检查该文件已无“独用”“档案”“编号”“读取器”。

### 9.3 测试与范围核对

改动前，忽略列表相关的三个新增断言均复现失败：`Ran 3 tests in 0.675s; FAILED (failures=3)`。改动后两份通用 agent 测试通过：`Ran 19 tests in 7.517s; OK`。

新增及扩展断言覆盖退出管理后不在建议列表、忽略对象完整字段、有效技能计数、忽略及退出后按编号重新接入、接入时移出忽略名单、撤销时恢复原忽略名单及完整指纹、无档案/无目录/普通文件过滤、空目录接入，以及供设置页、确认框、操作记录使用的返回字段和用量覆盖信息的新文案。HTTP 用例实际走过退出管理 → 忽略列表 → 重新接入 → 逐次撤销，也保留了确认、版本过期和只读写入保护的原有检查。

全量命令：`PYTHONDONTWRITEBYTECODE=1 ~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11 -m unittest discover -q`。原始输出保存于 `work/generic-agents/b1-b2-tests.txt`：

```text
----------------------------------------------------------------------
Ran 185 tests in 51.651s

OK
```

代码量检查为 7,588 行，包含页面执行者当前的代码。空白格式检查通过。全部写入测试使用系统临时目录里的模拟 home，未对真实 home 运行本工具，也未操作真实看板或启动器。

本执行未编辑 `skill_pilot/web/static/`。该目录在本轮期间出现的额外未暂存变化已原样保留；已暂存基础的指纹与开始时一致。本次改动留在未暂存状态，未对工作区执行 `git add/commit/reset/checkout/stash`。
