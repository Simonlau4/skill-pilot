# 交接卡：Skill Pilot M5 用量与清理，外加三项修正

> 2026-10-10 由 Claude（主控）派给 Codex。需求正本 `PLAN.md`（先完整重读 §1 中“清理判定”“清理方式”“留下的技能”三行、§2 用量记录、§4.5、§5、§7 的 M5、§8），验证记录 `docs/spikes.md`（S2 DSH、S3 Antigravity、S8 Claude），上一轮 `docs/m4-report.md`。沿用现有代码。

## 背景

- 真实 home 已在用：统一仓库 `~/.skill-pilot/`，四个技能根是指向 `~/.skill-pilot/store/<name>` 的链接。M4 之后 Claude 经用户同意在真实 home 装了 16 个更新（store 提交 cb01b72、20a7ad9、28ad465）。
- 依赖由 agent 判定，见 `docs/dependency-decisions.json`、`operations.curated_dependencies()`；PLAN §3 已写明“未判定的建议只作折叠提示，不阻塞”。
- 用户偏好：**不要让用户逐条确认**。清理名单默认全选，用户只取消要留的。

## 一、M5 用量账本与清理（PLAN §4.5、§6）

1. **用量解析**（`skill_pilot/usage/`），每个会话每个技能只算一次，只算真实调用或读取，系统提示里的技能列表、写入、编辑不算：
   - Claude：`~/.claude/projects/**/*.jsonl` 中 `"name":"Skill"` 的 `input.skill`、Read 工具读取 `*/SKILL.md`、用户输入的 `/<技能名>` 命令。S8 已确认桌面 app 的会话元数据不需要另算。
   - Codex：`~/.codex/sessions/**/*.jsonl` 与 `~/.codex/archived_sessions/`，`payload.type` 为 `custom_tool_call`/`function_call`/`local_shell_call` 且参数含 `.../<目录>/SKILL.md`。
   - DSH：`~/.dsh/sessions/**/session.v{3,4}.jsonl.zstd`，按 S2 的方法逐 frame 解码（允许调用本机 Node 标准库），`type == "tool/call"` 且 `data.name == "skill"`，另加读取 SKILL.md 的调用；v3/v4 去重。去重可靠就把 DSH 记为“可统计”，否则仍记“统计不到”并写明原因。
   - Antigravity：S3 未完成。允许**一次**真实 `agy -p` 复现（命令见 `Codex记忆/工作流/多Agent协作.md` 第四节，在系统临时目录运行，只让它读一个现有技能回答一句话，不写任何文件），据此定位记录字段并做去重规则。做不成就保持“统计不到”。
   - 路径到技能名的映射：日志里有迁移前的路径（`~/.codex/skills/x`、`~/.agents/skills/x`、`~/.gemini/config/skills/x`、`~/.codex/skills/community/x`、某个本机私人技能目录）和迁移后的路径（同上加 `~/.skill-pilot/store/x`）；目录名与技能名不同的（如 `nuwa-skill` → `huashu-nuwa`）按 registry 的 `entries` 和迁移预览记录映射。
2. **账本**：`~/.skill-pilot/usage.sqlite`，增量扫描（记住每个日志文件的扫描位置），日志被删后历史不丢。真实 home 本轮不建账本（只读）。
3. **入库时间**：迁移时仓库是重建的，registry 和 store 里的时间不代表真实安装时间。按以下顺序取最早的可靠证据：`~/.agents/.skill-lock.json` 的 `installedAt` → 迁移备份 `~/.skill-pilot/backups/20261011T041403221947` 里原目录的文件时间 → 该技能名在各 agent 日志中第一次出现（含系统提示里的技能列表，这里只用来定“什么时候装的”，不算使用）。都拿不到就视为“刚装”，不进名单。
4. **清理候选**（全部满足才进名单）：至少分配给一个 agent；入库满 60 天；所有可统计 agent 在 60 天内都没用过；没有分配给“统计不到”的 agent；不是 60 天内用过的技能的依赖（按 `requires` 传递展开）；最近 60 天内没被用户“留下”。
5. **清理页**（看板）：名单默认全选；每行显示技能名、分配给谁、最后一次使用（哪个 agent、哪天）、描述字数；页头写明各 agent 清理后能省多少描述字数；数据覆盖说明（“Claude 的记录只保留最近 30 天，最早到 X 月 X 日”“Antigravity 统计不到/已可统计”）。用户取消勾选的记一次“留下”（当作今天用过）。按“清理选中的 N 个”：从所有 agent 撤下（对账、移除对应的 Codex 停用条目），store 保留，标“已撤下”；撤下会让写死路径失效的，确认框列出受影响文件（复用 M3）。“已撤下”列表可一键恢复原分配。每次清理一次 git 提交，进操作记录，可撤销。
6. **CLI**：`skill-pilot usage [--json]`（各 agent 覆盖范围和每个技能最后使用）、`skill-pilot cleanup --list [--json]`（只报告）、`skill-pilot cleanup --yes [--keep <技能>...]`、`skill-pilot restore-withdrawn <技能>...`。
7. **真实 home 只读预演**：对真实 home 跑一次用量统计和清理名单（账本放系统临时目录，不写 `~/.skill-pilot/`），结果存 `work/real-cleanup-preview.json`，报告里列表：候选技能、分配给谁、最后使用、描述字数、入库时间及依据；以及每个 agent 清理后能省多少字。

## 二、嵌套 SKILL.md 自动改名

Codex 会递归发现技能（S1 已证实），技能目录里子文件夹的 `SKILL.md` 会被当成独立技能加载。huashu-nuwa 上游 `examples/` 下有 15 个这样的文件，本机原本把它们改名为 `SKILL.example.md`（比较 `bases/huashu-nuwa` 与 store 里的版本可以看到具体做法，照着通用化）。

- 在安装、收编、更新、首次迁移时，把技能根目录以下任何子文件夹里的 `SKILL.md` 统一改名为 `SKILL.example.md`；若正文里引用了这些文件的路径，一并改写并在结果里列出。
- 这是工具的标准处理，不算“你改过”：判断本机是否改过、三方对比、更新时，都先对上游内容做同样的处理再比较，避免每次更新都被当成冲突。
- 在模拟 home 里用 huashu-nuwa 的真实上游演示一次“按上游更新”成功，更新后 Codex（`HOME`/`CODEX_HOME` 指向模拟 home 跑 `codex debug prompt-input`）看不到任何 `*-perspective` 技能。真实 home 的 huashu-nuwa 更新由主控在你完成后执行，用户已同意按上游更新。

## 三、合并确认的小毛病

M4 的 CLI 在带 `--review` 确认合并时，如果因为参数不对报错（例如漏了 `--choice merge`），会把整个合并预览目录删掉，只能重新生成。修正：带 `--review` 的调用出错时保留预览；带 `--merge-id` 时自动视为 `--choice merge`。补单测。

## 四、来源补登记与 interview-skills

- interview-skills 的 `source_path` 是“根目录（待核对）”。按 `docs/source-attribution.md` 的做法核对真实仓库（线索：Codex 配置里的项目目录名 `https-github-com-jennifer88huang-interview-skills`），比对内容和历史版本后更新 `docs/source-attribution.md`。
- 新增 CLI `skill-pilot set-source <技能> --repo <owner/repo> --path <目录> [--json]`：改 registry 的来源并重新校准基准，进操作记录、可撤销；需要 `--yes`。真实 home 由主控执行，你在模拟 home 验收。

## 五、依赖提示不阻塞

核对看板与 CLI：未判定的依赖建议只作折叠提示，不阻塞任何操作，不在顶部弹出要求处理（PLAN §3 新规则）。

## 可读 / 可写范围

- **可写**：`~/Developer/skill-pilot/`（不要 commit、push）、系统临时目录。
- **只读**：真实 home 的一切，包括 `~/.skill-pilot/`、四个技能根、`~/.codex/config.toml`、`~/.agents/.skill-lock.json`、各 agent 的会话记录。
- **禁止**：设置 `SKILL_PILOT_ALLOW_REAL_HOME`（单测也只用替身）；在真实 home 执行任何写操作或建账本；安装到 `~/.local/bin` 或 `~/Applications`；运行 `lark-cli update`、`npx skills`；执行任何技能里的脚本。除上文允许的一次 `agy -p` 外，不启动 Claude、Antigravity、DSH 会话。
- 读会话记录时只提取技能调用信息，不复制、不输出对话正文；证据里打码。
- 网络：只读请求（`gh api` GET、浅克隆到临时目录）。

## 验收方式

- 用量：各 agent 解析单测（每会话每技能一次；系统提示列表不算；新旧路径与目录名映射；DSH 去重）；另写一个独立的一次性脚本重新统计真实 home 近 30 天用量，与账本结果一致。
- 清理：用构造数据覆盖全部候选条件，包括“留下后重新计时”、依赖保护、统计不到的 agent、入库时间证据缺失；模拟 home 中完整走一遍清理 → 已撤下列表 → 恢复 → 撤销，指纹回到对应状态。
- 嵌套改名、合并预览保留、set-source、依赖提示不阻塞各有单测；huashu-nuwa 模拟更新按上文验收。
- 真实 home 只读预演完成并产出清单。所有单测通过，报告里贴出命令和结果。主控会对比真实 home 指纹，任何改动都算越界。

## 停止条件

- 需要写可写范围之外的地方，就停下这一步，写进报告，其他继续。
- 发现 PLAN 有会改变结果的错误或遗漏，写进“未决项”，不要自己改需求。
- 不要开始 M6（新 agent 调研）及之后的里程碑。

## 回报格式（写进 `docs/m5-report.md`，最后一条消息只输出这个文件的路径和一句状态）

- 状态：完成 / 完成但有疑虑 / 缺信息 / 受阻
- 给用户的大白话总结（5 行以内）
- 真实环境的清理预演清单（表格）与各 agent 能省多少字；各 agent 用量数据的覆盖范围
- 用户以后怎么用：看用量、清理、留下、恢复各一句话
- 产出文件清单
- 证据：测试命令与结果、各验收项记录、指纹对比
- 主控需要在真实 home 执行的命令（huashu-nuwa 更新、interview-skills 来源），逐条给出
- 未决项、对 PLAN.md 的修改建议（如有）
