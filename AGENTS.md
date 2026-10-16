# Skill Pilot 项目规则

给在本仓库工作的 agent（Codex、Claude 等）。开始前先读 [docs/STATUS.md](docs/STATUS.md)，需要背景时再读 [PLAN.md](PLAN.md)（需求与全部决策史，顶部注明最新生效决策）。`docs/handoff-*.md`、`docs/*-report.md`、`docs/spikes.md` 是开发历史，按需查。

## 最高准则：通用化与可复现

2026-10-15 用户确认：**后续所有修改、优化和修复，都以其他用户从 GitHub 获取项目后，能按公开步骤在自己的电脑上使用同等功能为标准。仅在维护者本机可用，不算完成。** 本节优先于历史计划中的“先让用户自己能用起来”等单机优先取舍；真实数据、权限和发布边界继续适用。

- **按用户环境运行**：用户目录、项目位置、程序入口和依赖位置应从当前环境发现或按公开步骤配置。产品的安装、启动和更新逻辑不得依赖维护者的用户名、个人文件夹、固定安装路径或本机独有的运行时版本。
- **依赖公开可得**：明确受支持的平台、版本要求、安装和更新步骤。需要第三方账号、授权或服务密钥的功能，由用户用自己的账号配置；不依赖维护者的私有文件或个人配置。
- **同等功能不等于复制私人数据**：每位用户管理自己的技能库、分配和设置。维护者的技能内容、账号、密钥及本机数据不作为别人使用功能的前提，也不随项目发布。
- **按影响范围验收**：涉及安装、启动、路径、依赖或更新的改动，必须在系统临时目录中的新模拟 home、不同项目或安装位置下验证对应流程；路径处理需覆盖含空格的情况。不能只用本机已有安装和依赖作为验证依据。界面和业务改动也应适用于不同技能清单及 agent 组合。
- **区分本地完成与公开可用**：代码改好、本机验证通过、公开版本发布是不同状态。只有所需文件已进入公开版本，且公开步骤已验证，才能说其他用户可以获得同等功能；仍遵守仅在用户要求时提交或推送的规则。
- **历史定制视为待修复项**：遇到与本次改动相关的本机固定条件，应按本准则消除并验证；无关问题登记到 [docs/STATUS.md](docs/STATUS.md)，不借机扩大修改范围。本准则不自动扩大平台范围；当前仍面向 macOS 本地桌面。

## 用户与方向

- 用户不是程序员。界面、确认框、报错、给用户的回复一律中文大白话，不出现 symlink、diff、git、TOML、hash 这类词。
- 最初要解决的四个问题：各 agent 分别管技能；更新不用专门开对话；同一技能多份副本导致版本冲突；装了什么不知道、不用的占上下文。新功能先问是否服务于这四个问题之一。
- **做减法**：能删就删，不为假想需求加功能、加配置、加 AI 调用。只有用户在设置里填了 AI 服务的 key，才会自动调用它给技能起中文名和分组；其余 AI 功能仍只作可选按钮。
- 用户不想逐条确认：能由规则或证据决定的不要做成待确认项；批量操作默认全选，让用户只挑例外。

## 真实数据与安全边界

- 真实数据：`~/.skill-pilot/`（`store/` 技能正本 git 仓库、`bases/`、`registry.json`、`agents.json`、`logs/`、`backups/`、`labels.json` 中文名与分组、`settings.json` 含 API key：不复制进模拟 home、不进备份和日志、不在回复里出现），四个技能根 `~/.codex/skills`、`~/.agents/skills`（Codex 与 DSH 共读）、`~/.claude/skills`、`~/.gemini/config/skills`，以及 `~/.codex/config.toml` 的 `[skills] config`。
- 旧版留下的用量数据库只保留，不读取、改写、删除或复制进模拟 home。
- 永远不碰：`~/.claude/skills/synced/`（Claude 桌面 app 自管）、`~/.codex/skills/.system/`、各家插件和内置目录、`~/.agents/.skill-lock.json`（只读）。
- 开发和测试只用系统临时目录里的模拟 home（`scripts/make-fake-home.py`；`tests/__init__.py` 断言目标 home 在临时目录）。对真实 home 运行本工具一律带 `SKILL_PILOT_READ_ONLY=1`。
- 对真实技能库做分配、更新、清理、改来源、撤销，必须是用户在当次对话里明确要求的；优先让用户自己在看板上点。
- 不要结束或干扰用户正在运行的看板进程（`python3.11 -m skill_pilot ui`）。不运行 `lark-cli update`、`npx skills`，不执行任何技能里的脚本。
- 动真实环境前后各取一次指纹（四个技能根的条目与链接目标、`~/.skill-pilot/` 文件、Codex 配置、来源记录），用 `~/.skill-pilot/logs/` 区分用户自己在看板上的操作。

## 运行与测试

- 开发验收使用 Python 3.11.4 以上（系统自带的旧版本可能没有 `tomllib`）。不得把某一台电脑上的解释器位置写死；其他用户按这个版本要求选择可用解释器。只用标准库。
- 全量测试：`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -q`（约 40 秒）。前端改动后另跑 `node --check skill_pilot/web/static/app.js`。
- 启动器与命令行入口：`sh scripts/build-app.sh` 生成 `~/Applications/Skill Pilot.app` 和 `~/.local/bin/skill-pilot`。
- 代码量检查：`find skill_pilot -name "*.py" -o -name "*.js" -o -name "*.html" | xargs wc -l`。

## 结构速览

| 位置 | 作用 |
| --- | --- |
| `skill_pilot/profiles/` | agent 档案：技能根、共享关系、停用方式、手动调用标记 |
| `scan.py`、`store.py`、`sync.py` | 盘点、统一仓库与迁移备份、按分配表对账链接 |
| `operations.py`、`codex_config.py` | 分配、依赖、安装、收编、操作记录与撤销；Codex 停用条目只增删工具自己加的 |
| `updates.py`、`sources/`、`normalization.py` | 检查更新（后台任务、逐条结果）、GitHub/飞书来源、嵌套 SKILL.md 改名 |
| `labels.py` | 本机 AI 服务设置、中文名与分组、分批整理与安全保存 |
| `cleanup.py` | 手动挑选技能撤下、已撤下名单与恢复 |
| `web/` | 本机看板（`127.0.0.1` 随机端口加口令；`static/` 为前端） |
| `skill/skill-pilot/` | 装给各 agent 的 `skill-pilot` 技能 |
| `docs/dependency-decisions.json` | 技能依赖判定表（依赖只来自这里和看板手动加减） |
| `docs/source-attribution.md` | 来源归属表（“自制”不检查更新） |

## Git 与 PR 流程

本仓库连接 GitHub 公开仓库 [Simonlau4/skill-pilot](https://github.com/Simonlau4/skill-pilot)。2026-10-16 用户确认：**所有改动都走 PR，任何 agent 不直接推送 main。**

- **一个 issue 一个分支**：本机的 Codex、DSH、Claude 和云端 agent 都在自己的分支上工作，分支名为 `负责人/issue-编号-简述`，如 `cloud/issue-4-favicon`、`codex/issue-8-local-update`。没有对应 issue 的改动先开 issue。
- **先认领再动手**：以 issue 上的 `owner:` 标签为准。同一块文件区域（如 `web/server.py`、`web/static/`、`updates.py`）同一时间只交给一个 agent；要改别人正在做的区域，先在 issue 里说明并等对方合并。issue 里写明先后顺序的，按顺序做。
- **开 PR 前先跟上 main**：基于最新的 main 变基，跑全量测试（前端改动另跑页面脚本检查），再开 PR。PR 说明用中文大白话写改了什么、怎么验收，并写 `Closes #编号`。
- **审查与合并**：PR 由“SP 测试与审查”审查；只有用户本人批准后才能合并。agent 不自行合并，也不改写已推送的历史。
- **不泄露本机信息**：提交使用 GitHub 隐私邮箱（`用户名@users.noreply.github.com` 形式），不用本机用户名或电脑名；代码、文档、PR 说明里不写本机绝对路径、个人目录或用户名。本机技能库、设置和密钥不上传。
- 推送自己的分支和开 PR 可按 issue 进行；合并仍须用户批准。只暂存本次相关文件。
