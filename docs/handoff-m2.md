# 交接卡：Skill Pilot M2 统一仓库与首次迁移（演练到“待用户确认”为止）

> 2026-10-10 由 Claude（主控）派给 Codex。需求正本 `PLAN.md`（已按 M0/M1 与 S1 真实探针结果修订，先完整重读 §2–§4、§7、§8），验证记录 `docs/spikes.md`（重点看文末“S1 真实目录探针结果”）。沿用 M1 已有代码。

## 目标

实现 PLAN.md §4.1 的统一仓库、首次迁移和一键还原，以及看板上的迁移流程（选 agent → 核对版本 → 确认 → 完成/还原）。迁移和还原要在**模拟 home** 里完整演练通过；对真实 home 只生成迁移预览，**不执行迁移**。真实迁移等用户亲自在核对页确认。

## 交付物

1. 代码：`store.py`（统一仓库、`registry.json`、`bases/`、备份与还原）、`sync.py`（由分配表计算各根应有的链接并对账）、迁移流程、看板的迁移页面和写接口、CLI：
   - `skill-pilot migrate --plan [--json]`：只读，输出迁移预览（接入哪些 agent、冲突组及默认选择、来源归属、每个 agent 迁移前后的可见名单、会被跳过的目录）。
   - `skill-pilot migrate --apply --plan-id <id>`：只接受与当前磁盘状态一致的预览 id（扫描结果变了就拒绝，要求重新预览）。
   - `skill-pilot restore <backup-id>` 和 `skill-pilot backups`。
   - 所有命令支持用环境变量（如 `SKILL_PILOT_HOME`，加上 `HOME`）指向模拟 home。
2. `scripts/make-fake-home.py`：从真实 home 只读复制出一份模拟 home（见下文“模拟 home 规则”）。
3. `tests/`：新增单测覆盖冲突默认选择、对账（含共享根、去重、同目标只列一次）、备份还原、预览 id 失效、模拟 home 越界保护。
4. `docs/m2-report.md`：回报，格式见文末；附**真实 home 的迁移预览**摘要：冲突组数和每组默认选择及理由、中低置信度来源条数、迁移前后各 agent 可见数量、跳过的目录。完整预览 JSON 放 `work/real-migration-preview.json`。

## 已定规则（来自 PLAN 与实测，不要重新讨论）

- 仓库位置 `~/.skill-pilot/store/`（git 仓库）、`bases/`、`registry.json`、`agents.json`、`backups/`；只存在于目标 home。
- 默认选择：内容与来源记录一致的那份 → 否则最近修改的那份；内容相同的直接合并。Example-Notes 中 `skills/{simon-neat,agency-router,marketing-router}` 三份源码作为额外候选参与核对（只读，不移动不删除）。
- 来源：用 `docs/source-attribution.md` 的归属；高置信度直接记录，中低置信度在核对页列出，用户可以改或标“来源不明”（不确认即视为来源不明，不检查更新）。
- 每个 agent 的初始分配 = 迁移前**实际可见**的集合（目录里有、没被停用）。迁移不新增也不删除 `~/.codex/config.toml` 的停用条目，不改 `~/.agents/.skill-lock.json`。
- 共享根 `~/.agents/skills` 的读者只有 Codex 和 DSH；Claude 只读 `~/.claude/skills`，Antigravity 只读 `~/.gemini/config/skills`。Codex 两个根都链接同一目标时只列一次。链接一律是指向 `~/.skill-pilot/store/<name>` 的绝对路径。
- 一律跳过、不迁移不改动：`~/.claude/skills/synced/`（Claude 桌面 app 自己维护）、`~/.codex/skills/.system/`、各家插件与内置目录、未接入的 agent。断链（如 `~/.agents/skills/doc`）原样保留，在看板“失效链接”里列出。
- 嵌套目录（`~/.codex/skills/community/*`）在仓库里摊平为 `store/<name>`，原位置换成链接后 Codex 仍能找到（S1 已证实递归发现）。
- 第一次迁移时默认勾选四个主力 agent；Kimi 忽略，WorkBuddy、Cursor、Trae 不接入。
- 执行顺序：校验预览 id → 备份所有涉及的根（tar，保留链接本身）→ 写仓库和 `bases/` → 逐个把真实目录换成链接 → 提交一次 git → 重新扫描，比对每个 agent 迁移前后的可见名单（版本按核对页选择），不一致就自动整体还原并报错。
- 写接口：沿用 M1 的 127.0.0.1、随机口令、`Host`/`Origin` 校验；所有写操作都要有确认步骤。界面全中文大白话。

## 模拟 home 规则（安全关键）

- 复制：四个技能根、`~/.claude/skills` 链接指向的目标、stss 指向的 `~/Developer/vendor/stop-that-shit-local/skills/stss`、Example-Notes 三份源码、`~/.agents/.skill-lock.json`、`~/.codex/config.toml` 中只保留 `[skills]` 段（不复制认证和其他配置）。
- 复制后，把所有指向真实 home 的绝对链接改写成模拟 home 里对应的位置；相对链接保持不变。
- 建好后断言：模拟 home 内任何链接解析后都不能落在模拟 home 之外，否则中止。
- 代码层面加保护：任何写操作在执行前对目标做 `realpath`，落在当前目标 home 之外就拒绝。单测要覆盖这一条。
- 验证可见性时可以按 S1 的做法，用 `HOME`、`CODEX_HOME` 指向模拟 home 跑 `codex debug prompt-input`。Claude、Antigravity、DSH 的会话**不要**在本轮运行。

## 可读 / 可写范围

- **可写**：`~/Developer/skill-pilot/`（不要 commit、push）、系统临时目录（模拟 home 放这里）。
- **只读**：真实 home 的一切，包括四个技能根、`~/.codex/config.toml`、`~/.agents/.skill-lock.json`、`~/.claude/skills/synced/`、Example-Notes。
- **禁止**：在真实 home 运行 `migrate --apply`、`restore`，或在真实 home 创建 `~/.skill-pilot/`；在看板里对真实 home 按下任何写操作按钮；安装到 `~/.local/bin` 或 `~/Applications`；运行 `lark-cli update`、`npx skills`。
- 网络：本轮不需要。

## 验收方式

- 模拟 home 中完整走两遍：迁移 → 校验 → 还原 → 指纹与迁移前完全一致 → 再迁移一次结果相同（幂等）。
- 迁移后每个 agent 的可见名单与迁移前一致；Codex 在模拟 home 的 `debug prompt-input` 技能列表与迁移前一致（版本差异只来自核对页选择）。
- 人为制造一次失败（例如中途让一个目标不可写），确认能自动整体还原并给出大白话报错。
- 看板里的迁移流程在模拟 home 上实际点一遍：选 agent、在核对页改一个默认选择、确认迁移、看到完成页、按“一键还原”。
- 所有单测通过，在报告里贴出命令和结果。
- 主控会在你做完后对比真实 home 的指纹，任何改动都算越界。

## 停止条件

- 需要写可写范围之外的地方，就停下这一步，写进报告，其他继续。
- 发现 PLAN 有会改变结果的错误或遗漏，写进报告的“未决项”，不要自己改需求。
- 不要开始 M3 及之后的里程碑。

## 回报格式（写进 `docs/m2-report.md`，最后一条消息只输出这个文件的路径和一句状态）

- 状态：完成 / 完成但有疑虑 / 缺信息 / 受阻
- 给用户的大白话总结（5 行以内）
- 真实 home 迁移预览摘要（见“交付物”第 4 条）
- 产出文件清单
- 证据：测试命令与结果、演练记录、指纹对比
- 未决项：需要用户决定或授权的事，每条一句话
- 对 PLAN.md 的修改建议（如有）
