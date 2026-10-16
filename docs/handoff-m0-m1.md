# 交接卡：Skill Pilot M0 验证 + M1 只读盘点

> 2026-10-10 由 Claude（主控）派给 Codex。需求正本是同目录上一级的 `PLAN.md`，先完整读一遍；本卡只限定这一轮的范围。

## 目标

完成 PLAN.md §7 的 **M0（除 S1 探针外）** 和 **M1**：查清还没验证的事实，并做出一个只读的盘点工具和看板。这一轮**不改动任何 agent 的技能目录和配置**。

## 交付物

1. `~/Developer/skill-pilot/docs/spikes.md`：S2–S8 的结论和脱敏证据；S1 按下文“S1 的处理”写。
2. M1 代码，按 PLAN.md §3 的目录结构（只需要 `profiles/`、`scan.py`、`web/` 的只读部分、`cli.py`、`tests/`）：
   - `python3 -m skill_pilot scan --json`：输出已识别的 agent、各根目录的技能清单（名称、真实目录还是链接、链接目标、断链）、疑似目录的分类、同名不同内容的冲突组、每个 agent 的可见技能数和描述字数（Codex 要扣除 `config.toml` 中已停用的条目）。
   - `python3 -m skill_pilot ui`：按 PLAN.md §3 的方式启动只读看板（127.0.0.1、随机端口、随机口令、心跳退出、单实例），展示上面这些信息，中文大白话。
   - `tests/`：疑似目录分类、冲突分组、Codex 停用条目解析的单测，fixtures 用 PLAN.md §2 的真实路径形态，放在临时目录里构造。
3. `~/Developer/skill-pilot/docs/m0-m1-report.md`：一页回报，格式见文末。

## 可读 / 可写范围

- **可写**：只有 `~/Developer/skill-pilot/`（可以 `git init`，**不要 commit，不要 push**），以及系统临时目录。
- **只读**：`~/.codex/`、`~/.agents/`、`~/.claude/`、`~/.gemini/`、`~/Library/Application Support/` 下各 agent 的数据、`/Applications/*.app`、`~/Documents/Example-Notes/notes/`、Example-Vault。
- **禁止**：在任何 agent 的技能目录里建、删、改文件或链接；修改 `~/.codex/config.toml`、`~/.agents/.skill-lock.json`；创建 `~/.skill-pilot/`；安装到 `~/.local/bin` 或 `~/Applications`；运行 `lark-cli update`（`--check --json` 可以）；运行 `npx skills` 的任何写操作。
- 网络：只允许只读请求（`gh api` 的 GET、`git ls-remote`、浅克隆到临时目录、查官方文档）。

## S1 的处理

S1 需要往四个 agent 的真实技能目录里放测试链接，用户这一轮**没有授权**。只做不碰真实目录的部分：

- 阅读各 agent 的官方文档或程序包，判断是否跟随指向目录外的链接，并给出依据；
- Codex：如果能用 `HOME` / `CODEX_HOME` 指向临时目录、在里面放探针，再用 `codex debug prompt-input` 验证，就做；做不到就算了。Codex CLI 路径：`/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`，前台运行加 `< /dev/null`；
- 不运行 `claude -p`、`agy -p`、`dsh headless`；
- 在 spikes.md 里写明：哪些已经确认、哪些需要用户同意后在真实目录里做探针，以及具体要做的步骤（可以直接照着执行的命令）。

S2 中“DSH 能看到哪些目录”这一项依赖 S1，同样处理；S2 中“用量记录”的部分照常只读调查。

## 规则与技能

- 遵守 `~/.codex/AGENTS.md`，以及 `~/Documents/Example-Notes/sample-rules.md` 的落盘规则。
- 用户不是程序员：看板和 report 写大白话；代码、spikes.md 写技术细节。
- PLAN.md §1 的结论不要重新讨论。

## 已知事实

见 PLAN.md §2（编写时核实于 2026-10-10）。另外：`~/.agents/skills` 有 91 个入口，其中 22 个是指向 `~/.codex/skills` 的链接，`doc` 是断链。

## 验收方式

- M0：spikes.md 里 S2–S8 都有结论和证据；S1 有“已确认 / 待用户授权”的清单和待执行命令。
- M1：`scan --json` 的计数与 PLAN.md §2 一致，有出入的写明原因；单测全部通过（报告里贴出运行命令和结果）；`ui` 实测能打开，关掉页面 30 秒内进程退出，重复启动不出现第二个进程。
- 主控会在你做完后，对比开工前后四个技能根目录、`~/.codex/config.toml`、`~/.agents/.skill-lock.json` 的指纹，任何改动都算越界。

## 停止条件

- 任何一步需要写可写范围之外的地方，就停下这一步，在 report 里写明原因和需要用户做的决定，其余工作继续。
- 发现 PLAN.md 有会改变结果的错误或遗漏，写进 report 的“未决项”，不要自己改需求。

## 回报格式（写进 `docs/m0-m1-report.md`，最后一条消息只输出这个文件的路径和一句状态）

- 状态：完成 / 完成但有疑虑 / 缺信息 / 受阻
- 给用户的大白话总结（5 行以内）
- 产出文件清单
- 证据：测试命令与结果、计数对照
- 未决项：需要用户决定或授权的事，每条一句话
- 对 PLAN.md 的修改建议（如有）
