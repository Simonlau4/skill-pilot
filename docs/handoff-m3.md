# 交接卡：Skill Pilot M3 勾选表、依赖、安装与收编

> 2026-10-10 由 Claude（主控）派给 Codex。需求正本 `PLAN.md`（先完整重读 §1、§3、§4.2、§4.3、§5、§7、§8），验证记录 `docs/spikes.md`，上一轮结果 `docs/m2-report.md`（含文末“真实迁移结果”）。沿用现有代码。

## 背景变化（重要）

真实迁移已于 2026-10-10 21:14 完成：真实 home 现在有 `~/.skill-pilot/{store,bases,registry.json,agents.json,backups}`，四个技能根基本都是指向 `~/.skill-pilot/store/<name>` 的绝对链接。M2 的 `scripts/make-fake-home.py` 是按迁移前的布局写的，本轮要先改成能复制**迁移后**的布局：一并复制 `~/.skill-pilot/`（不含 `backups/`），并把所有指向真实 `~/.skill-pilot/` 的绝对链接改写到模拟 home 里，复制后的越界断言保持不变。

M2 之后 Claude 改过的代码（保留，不要回退）：`store.preview` 不再优先选 Example-Notes 源码；`store.write_allowed`/`guard` 要求真实 home 显式设置 `SKILL_PILOT_ALLOW_REAL_HOME=1`；`docs/source-attribution.md` 已按内容比对补全来源，“自制”表示不检查更新。

## 目标

实现 PLAN §4.2 勾选表、§4.3 安装与收编、依赖、§5 的“操作记录”页，以及相应 CLI。全部在模拟 home 中开发和验收；真实 home 只读。

## 交付物

1. **勾选表**（§4.2）：行是技能，列是已接入 agent；改动先进入“待应用”，底部汇总“将给 Claude 加 2 个、从 Antigravity 撤下 1 个”，页头显示各 agent 技能数、描述字数及“应用后变为多少”；按 agent 分别提示生效方式（Claude、DSH 动态刷新；Codex、Antigravity 新会话生效）。“应用”= 对账 + 一次 git 提交。
2. **对账规则**（§3 核心模型）：共享根 `~/.agents/skills` 的读者只有 Codex 和 DSH；只给 DSH 的技能放共享根，并在 `~/.codex/config.toml` 的 `[skills] config` 内联数组里加 `{ name = "<技能名>", enabled = false }`；只给 Codex 的放 `~/.codex/skills`；两者都要的放共享根，`~/.codex/skills` 里不重复放。Codex 配置只增删 registry 里记录为 Skill Pilot 自己添加的条目，原有条目一律不动；写前备份、写后用 `tomllib` 解析校验，失败就回滚。
3. **依赖**：registry 的 `requires`。入库或首次打开勾选表时，从各 SKILL.md 正文匹配已知技能名，起草依赖建议，在看板上用大白话列出（“grill-me 需要 grilling 才能用”），用户点“确认”或“不是依赖”。已确认的依赖在分配时自动带上并说明原因；撤下一个仍被其他已分配技能依赖的技能时，提示并要求确认，不静默撤下。至少要识别 grill-me → grilling，以及 ascentiz-blog-writer → natural-text-writing、unslop、stss、ascentiz-blog-editor。
4. **撤下前的路径提醒**：撤下某个技能会让 `docs/path-references.md`（S7）里的写死路径失效时，确认框里列出受影响的文件。
5. **安装**（§4.3）：粘贴 GitHub 链接（仓库或 `tree/<ref>/<path>`）→ 浅克隆到临时目录 → 列出找到的技能供勾选 → 入库，记录来源（仓库、路径、commit、tree sha）并写入 `bases/` → 同一弹窗勾选给哪些 agent（默认都不勾）。同名技能已存在时转入对比，不覆盖。**不执行技能里的任何脚本。**
6. **收编**（§4.3）：扫描时，已接入 agent 根目录里出现的真实目录（不是指向 store 的链接）算“新装的技能”，看板顶部提示“发现 N 个新装的技能”。一键收编：移进 store，原位置换成链接，保持原来的可见范围。来源按 PLAN §1“来源确认”和 §4.4 的新规则自动处理：有 `~/.agents/.skill-lock.json` 记录就继承；否则用 GitHub 代码搜索加内容比对（含历史版本，方法参考 `work/source-verify-20261010.json` 的做法），比对上就登记，比对不上就记为“自制”，**不让用户确认**。仓库里已有同名但内容不同的，转入对比页。要跳过的目录与 M2 一致（`synced/`、`.system`、插件、内置、空目录、断链）。
7. **操作记录页**（§5）：每次写入记一行大白话（时间、做了什么、涉及哪些 agent），每行有“撤销”：git revert 后重新对账；对账涉及的 Codex 配置条目也要一起恢复。
8. **CLI**（§4.7）：`skill-pilot status`（大白话汇总）、`list --agent <id>`、`assign <skill> --agent <id>`、`unassign <skill> --agent <id>`、`absorb [--all]`、`install <github-url> [--skill <name>] [--agent <id>]`、`log`、`undo <id>`，全部支持 `--json`。assign/unassign 可直接执行；absorb、install 以及会撤下依赖的 unassign 需要 `--yes`。
9. `tests/` 增补对应单测；`docs/m3-report.md` 回报。

## 可读 / 可写范围

- **可写**：`~/Developer/skill-pilot/`（不要 commit、push）、系统临时目录（模拟 home 放这里）。
- **只读**：真实 home 的一切，包括 `~/.skill-pilot/`、四个技能根、`~/.codex/config.toml`、`~/.agents/.skill-lock.json`。
- **禁止**：设置 `SKILL_PILOT_ALLOW_REAL_HOME=1`；在真实 home 执行任何写命令或按看板写按钮；安装到 `~/.local/bin` 或 `~/Applications`；运行 `lark-cli update`、`npx skills`。
- 网络：允许只读请求（`gh api` GET、`git ls-remote`、浅克隆到临时目录）。GitHub 代码搜索限 10 次/分钟，注意节流。

## 验收方式（全部在模拟 home）

- 给 Claude 勾上 grill-me，grilling 自动一起分配；扫描显示 Claude 可见名单多出这两个，其他 agent 不变。
- 只给 DSH 的技能：Codex 用 `HOME`/`CODEX_HOME` 指向模拟 home 跑 `codex debug prompt-input` 看不到它，DSH 根里有链接；Codex 配置原有条目逐字保留，只多出 Skill Pilot 记录的那一条。撤销后该条目消失、配置与之前逐字节一致。
- 撤下一个被依赖的技能会被拦下提示；撤下涉及写死路径的技能会列出受影响文件。
- 安装：用一个本地 git 仓库（`file://`）覆盖正常安装和同名冲突；再用一个真实公开仓库（如 `mattpocock/skills` 的 `skills/productivity/grill-me`）只读演示一次克隆和识别，装进模拟 home。
- 收编：在模拟 home 的 `~/.codex/skills` 里放一个新真实目录（模拟 Codex 自己装的技能），看板提示后一键收编；分别覆盖“lock 有记录”“GitHub 比对上”“比对不上记为自制”“同名不同内容转对比”四种情况（GitHub 部分可以用测试替身，另做一次真实只读比对）。
- 操作记录里任选三次写入逐一撤销，模拟 home 指纹回到对应状态。
- 所有单测通过，在报告里贴出命令和结果。主控会在你做完后对比真实 home 的指纹，任何改动都算越界。

## 停止条件

- 需要写可写范围之外的地方，就停下这一步，写进报告，其他继续。
- 发现 PLAN 有会改变结果的错误或遗漏，写进报告的“未决项”，不要自己改需求。
- 不要开始 M4（检查更新）及之后的里程碑。

## 回报格式（写进 `docs/m3-report.md`，最后一条消息只输出这个文件的路径和一句状态）

- 状态：完成 / 完成但有疑虑 / 缺信息 / 受阻
- 给用户的大白话总结（5 行以内）
- 用户以后怎么用：勾选表、安装、收编、撤销各一句话
- 产出文件清单
- 证据：测试命令与结果、各验收项的记录、指纹对比
- 依赖建议清单（识别到的全部依赖关系，供主控核对）
- 未决项、对 PLAN.md 的修改建议（如有）
