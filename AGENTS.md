# Skill Pilot 项目规则

给在本仓库工作的所有 agent。开始前先读 [docs/STATUS.md](docs/STATUS.md)，需要背景时再读 [PLAN.md](PLAN.md)（需求与全部决策史，顶部注明最新生效决策）。`docs/*-report.md` 不是交接说明时算开发历史，`docs/spikes.md` 按需查。

## 最高准则：通用化与可复现

2026-10-15 用户确认：**后续所有修改、优化和修复，都以其他用户从 GitHub 获取项目后，能按公开步骤在自己的电脑上使用同等功能为标准。仅在维护者本机可用，不算完成。** 本节优先于历史计划中的“先让用户自己能用起来”等单机优先取舍；真实数据、权限和发布边界继续适用。

- **按用户环境运行**：用户目录、项目位置、程序入口和依赖位置应从当前环境发现或按公开步骤配置，不依赖维护者的用户名、个人文件夹、固定安装路径或本机独有的运行时版本。
- **依赖公开可得**：明确受支持的平台、版本要求、安装和更新步骤。需要第三方账号或密钥的功能，由用户用自己的账号配置。
- **同等功能不等于复制私人数据**：维护者的技能内容、账号、密钥及本机数据不作为别人使用功能的前提，也不随项目发布。
- **按影响范围验收**：涉及安装、启动、路径、依赖或更新的改动，必须在系统临时目录中的新模拟 home、不同项目或安装位置下验证；路径处理需覆盖含空格的情况。
- **区分本地完成与公开可用**：只有所需文件已进入公开版本，且公开步骤已验证，才能说其他用户可以获得同等功能。
- **历史定制视为待修复项**：与本次改动相关的本机固定条件按本准则消除；无关问题登记到 [docs/STATUS.md](docs/STATUS.md)，不借机扩大修改范围。当前仍面向 macOS 本地桌面。

## 用户与方向

- 用户不是程序员。界面、确认框、报错、给用户的回复一律中文大白话，不出现 symlink、diff、git、TOML、hash 这类词。
- 新功能先问是否服务于最初四个问题之一：各 agent 分别管技能；更新不用专门开对话；同一技能多份副本导致版本冲突；装了什么不知道、不用的占上下文。
- **做减法**：不为假想需求加功能、加配置、加 AI 调用。只有用户在设置里填了 AI 服务的 key，才会自动调用它给技能起中文名和分组；其余 AI 功能只做可选按钮。
- 能由规则或证据决定的不要做成待确认项；批量操作默认全选，让用户只挑例外。

## 真实数据与安全边界

- 真实数据：`~/.skill-pilot/`（`store/`、`bases/`、`registry.json`、`agents.json`、`logs/`、`backups/`、`labels.json`、含 API key 的 `settings.json`：不复制进模拟 home、不进备份和日志、不在回复、issue、PR 里出现），各 agent 的技能根目录（见 `skill_pilot/profiles/catalog.json`），以及 `~/.codex/config.toml` 的 `[skills] config`。
- 旧版留下的用量数据库只保留，不读取、改写、删除或复制进模拟 home。
- 永远不碰：`~/.claude/skills/synced/`、`~/.codex/skills/.system/`、各家插件和内置目录、`~/.agents/.skill-lock.json`（只读）。
- 开发和测试只用系统临时目录里的模拟 home（`scripts/make-fake-home.py`；`tests/__init__.py` 断言目标 home 在临时目录）。对真实 home 运行本工具一律带 `SKILL_PILOT_READ_ONLY=1`。
- 对真实技能库做分配、更新、清理、改来源、撤销，必须是用户在当次对话里明确要求的；优先让用户自己在看板上点。
- 不要结束或干扰用户正在运行的看板进程。不运行 `lark-cli update`、`npx skills`，不执行任何技能里的脚本。
- 动真实环境前后各取一次指纹（`skill_pilot/profiles/catalog.json` 所列技能根的条目与链接目标、`~/.skill-pilot/` 文件、Codex 配置、来源记录），用 `~/.skill-pilot/logs/` 区分用户自己在看板上的操作。

## 运行与测试

- 使用 Python 3.11.4 以上，只用标准库，不把某台电脑上的解释器位置写死。
- 全量测试：`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -q`。前端改动另跑 `node --check skill_pilot/web/static/app.js`。
- 启动器与命令行入口：`sh scripts/build-app.sh`（不要在副本里运行，会把用户的启动器改成指向副本）。

## 结构速览

| 位置 | 作用 |
| --- | --- |
| `skill_pilot/profiles/` | agent 档案：技能根、共享关系、停用方式、手动调用标记 |
| `scan.py`、`store.py`、`sync.py` | 盘点、统一仓库与首次整理备份、按分配表对账链接 |
| `operations.py`、`codex_config.py` | 分配、依赖、安装、收编、操作记录与撤销 |
| `updates.py`、`sources/`、`normalization.py` | 检查更新、GitHub/飞书来源、嵌套 SKILL.md 改名 |
| `labels.py` | AI 服务设置、中文名与分组 |
| `cleanup.py` | 手动撤下技能与恢复 |
| `web/` | 本机看板（`static/` 为前端） |
| `skill/skill-pilot/` | 装给各 agent 的 `skill-pilot` 技能 |
| `docs/dependency-decisions.json` | 技能依赖判定表 |
| `docs/source-attribution.md` | 来源归属表（“自制”不检查更新） |

## Git 与 PR 流程

公开仓库 [Simonlau4/skill-pilot](https://github.com/Simonlau4/skill-pilot)。**所有改动都走 PR，任何 agent 不直接推送 main。**

- **一个 issue 一个分支一个 PR**：分支名为 `负责人/issue-编号-简述`，如 `cloud/issue-4-favicon`。没有对应 issue 的改动先开 issue。
- **先认领再动手**：以 issue 上的 `owner:` 标签为准。同一块文件区域同一时间只交给一个 agent；要改别人正在做的区域，先在 issue 里说明并等对方合并。issue 里写明先后顺序的，按顺序做。
- **开 PR 前先跟上 main**：把最新 main 普通合并进分支解决冲突，跑全量测试（前端改动另跑页面脚本检查），再开 PR。PR 说明用中文大白话写改了什么、怎么验收，并写 `Closes #编号`。
- **交接文件不进 PR**：开发方在推送或开 PR 前，自己删掉所有交接用的文件，不管文件名是 `docs/handoff-*.md` 还是 `docs/*-report.md`。凡是回答交接要求、写着“合并前删”、给别的 agent 排下一步的内容，都算交接文件。确实要留的开发报告，只写改了什么、怎么验证的。这条只约束新的 PR，main 上已有的旧文件保留。
- **合并**：“SP 测试与审查”评为“能合”、且没有必须修改项的 PR（含代码 PR），“SP 项目经理”可直接压缩合并，不必请示 Simon；合并记入每日状态（Simon 于 2026-10-16 确认）。仓库用压缩合并。不改写已推送的历史，不强推。
- **不泄露本机信息**：提交使用 GitHub 隐私邮箱；代码、文档、issue、PR 里不写本机绝对路径、个人目录或用户名。本机技能库、设置和密钥不上传。只暂存本次相关文件。

### 项目文件夹与副本（本机 agent）

用户的看板从项目文件夹运行，“设置 → 版本”的一键升级也要求这里停在最新 `main`、没有改动。所以本机 agent 都在自己的副本里干活。

- **项目文件夹只停在 `main`**：不在里面改文件、切分支、提交。唯一例外是 PR 合并后跟上正式版：确认在 `main` 且没有改动，再 `git pull --ff-only`；不满足就停下告诉用户。
- **开副本**：每个 issue 一份副本，从最新正式版开出：`git fetch origin` 后 `git worktree add -b 负责人/issue-编号-简述 .worktrees/负责人-issue-编号-简述 origin/main`。`.claude/worktrees/` 下的副本也可以用，开 PR 前改成规范分支名并跟上 `origin/main`。副本不要放在主目录下的隐藏文件夹里（如 `~/.codex/`）。
- **在副本里看效果**：默认用模拟 home：

  ```sh
  FAKE="$(mktemp -d)/home"
  python3 scripts/make-fake-home.py "$FAKE"
  SKILL_PILOT_HOME="$FAKE" BROWSER=/usr/bin/true python3 -m skill_pilot ui --json
  ```

  对照真实技能库时只能只读打开：`SKILL_PILOT_READ_ONLY=1 TMPDIR="$(mktemp -d)" BROWSER=/usr/bin/true python3 -m skill_pilot ui --json`。单独的临时目录避开用户看板的启动锁，`BROWSER` 防止弹开用户的浏览器。看完结束自己开的看板。
- **清理**：PR 合并或关闭后，删掉自己的副本和本地分支（`git worktree remove`、`git branch -D`；仓库用压缩合并，先用 `gh pr view` 确认状态）。
- 云端 agent 不接触本机文件夹，用 `cloud/` 分支前缀。
