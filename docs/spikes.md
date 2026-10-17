# M0 技术调查：S1–S8

核验日期：2026-10-10。范围依 [交接卡](handoff-m0-m1.md)，需求正本为 [PLAN.md](../PLAN.md)。本轮没有写入真实 agent 技能目录或配置，没有运行真实目录探针、升级、安装、迁移、分配或清理。

## 结论索引

| 项 | 结论 | 验证程度 |
| --- | --- | --- |
| S1 | Codex 临时 home 的绝对目录链接与 community 嵌套探针通过；Claude 官方文档、DSH 程序包支持外部目录链接；Antigravity 官方文档确认全局目录，但未明确链接行为 | 真实目录探针待授权 |
| S2 | DSH 默认用户根是 `.dsh/skills` 与 `.agents/skills`；旧用量记录可解码，识别出 35 条原始 skill 调用 | 根目录为源码依据，真实会话待探针；用量不能据此立即用于清理 |
| S3 | Antigravity 的旧数据库有实际文件读取步骤，含技能文件路径；新对话复现本轮禁止 | 未达到完整用量验收，仍记“暂时统计不到” |
| S4 | lark-cli 1.0.97 委托动态版本的 skills CLI 写目录和来源记录；separate / suite 会安装并移除不同布局 | 静态核实；没有升级 |
| S5 | Codex 54 个直属目录加 2 个嵌套目录已逐项归属：高 15、中 34、低 7 | 中低置信度后续确认；高也不是上游内容一致证明 |
| S6 | 三个 GitHub 样本的远端目录版本与下载结果一致；两个无更新，一个有更新 | 通过；API 共 10 次，浅克隆 3 次 |
| S7 | 找到 87 个引用文件；保留原入口时迁移不破坏引用，撤下相应入口后引用失效 | 完整清单见附件 |
| S8 | Claude 桌面 40 份会话元数据全部对应现有 projects 逐字稿；未发现额外工具调用逐字稿 | 只用 `.claude/projects`，不重复计数 |

## S1 外部目录链接、递归与可见数量

### 已确认

- **Codex**：本机 `/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex`，0.162.0-alpha。临时 home 中创建两个真实根，其中技能入口链接到临时 home **之外**的 `outside/`；一个入口直接放在 `skills/`，另一个放在 `skills/community/`。子进程覆盖 HOME、CODEX_HOME、XDG_CONFIG_HOME、XDG_CACHE_HOME，cwd 也在临时目录，stdin 为 `/dev/null`。`debug prompt-input` 返回 0，两种技能名都出现两次。未把真实根链接为临时 CODEX_HOME，避免 CLI 对 `.system` 的初始化影响原目录。
- [Codex 官方说明](https://learn.chatgpt.com/docs/build-skills)明确支持技能目录链接。该页面也说明 `agents/openai.yaml` 的 `allow_implicit_invocation: false` 保留显式调用，但不自动触发。
- **Claude Code**：[官方 Skills 文档](https://code.claude.com/docs/en/skills#where-skills-live)的 Symlinked folders 条目说明个人技能目录中的入口可以指向磁盘其他位置，相同目标只加载一次。本机 43 个个人入口全部为相对目录链接，其中 stss 指向某个本机私人技能目录。文档依据与现状不能替代本机新绝对链接探针。
- **DSH**：DeepSeek Harness 0.2.0-rc.2 的 `app.asar` 内，`@deepseek-ai/dsh-skill-filesystem` 同版本。`lib/index.js` 第 767–779 行对 symbolic link 使用 `stat(fullPath)` 判断目标类型；第 714 行通过 `realpath(path)` 读取目标。README.zh.md 也明确描述符号链接目录的发现与预览。源码没有限制目标必须留在根目录内部；仍需真实会话验证运行权限。
- **Antigravity**：[官方 Skills 文档](https://antigravity.google/docs/skills)确认全局位置为 `~/.gemini/config/skills/<skill-folder>/`。现有全局技能目录包含工作台之外的链接；PLAN 已记录 agy 能识别既有链接。本轮没有运行 agy 对话，因此新绝对链接行为仍待验证。

### Codex 额外的可见数量核对

再建一份临时 home，把用户技能逐个链接进去，只复制 `[skills] config` 的停用条目，不复制认证或其他配置。新 CLI 提示中的用户技能名与扫描结果对照：

- 两根共 146 个有效入口，按名称合并为 123 个技能。
- 原配置按 name 停用 92 项、按 path 停用 1 项；其中命中现有用户技能 27 个唯一名称。按 path 的旧 liene-monthly-report 已不存在，没有再扣一个。
- 扣除配置后可用 96 个。`grill-me` 的 frontmatter 禁止模型自动调用，`critical-gate/agents/openai.yaml` 的 policy 禁止隐式调用。
- CLI 自动技能列表为 **94 个**，与最终扫描 `visible_names` 一致；自动技能描述合计 **30,246 字符**。另保留 `available_user_skill_count=96` 与两个 `manual_only_names`，不把手动入口当作不存在。
- 内置 5 个、插件缓存 192 个、用户插件 3 个、候选库 39 个另列，未把磁盘存在视为已启用。

### 待用户授权的真实探针

以下命令仅作为后续执行卡，本轮**未运行**。需要授权：创建 `~/.skill-pilot/store/` 下的无害探针，给四个真实技能根添加临时链接，运行新会话后精确撤除这些链接。不能把临时目录的通过当作正式迁移已经获准。

先创建探针和链接，已有同名路径时停止，绝不覆盖：

```bash
python3 - <<'PY'
from pathlib import Path
home = Path.home()
target = home / '.skill-pilot/store/skill-pilot-probe'
roots = ['.codex/skills', '.agents/skills', '.claude/skills', '.gemini/config/skills']
assert not target.exists() and not target.is_symlink()
for root in roots:
    entry = home / root / 'skill-pilot-probe'
    assert not entry.exists() and not entry.is_symlink(), entry
target.mkdir(parents=True)
(target / 'SKILL.md').write_text('---\nname: skill-pilot-probe\ndescription: Harmless discovery probe; take no actions.\n---\nOnly report whether this skill is visible.\n')
for root in roots:
    (home / root / 'skill-pilot-probe').symlink_to(target, target_is_directory=True)
PY
```

分别在新会话确认名称可见，不要求执行任何技能脚本：

```bash
/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex debug prompt-input "probe" < /dev/null | python3 -c 'import sys; print("skill-pilot-probe visible:", "skill-pilot-probe" in sys.stdin.read())'
claude -p "仅确认技能列表是否有 skill-pilot-probe，不执行任何工具或脚本。"
agy -p --sandbox --dangerously-skip-permissions "仅确认技能列表是否有 skill-pilot-probe，不执行任何工具或脚本。"
"/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh" --profile headless "仅确认技能列表是否有 skill-pilot-probe，不执行任何工具或脚本。"
```

DSH 当前程序包 README 的入口是 `--profile headless`，不是 PLAN 简写的 `dsh headless`。若需要区分 DSH 私有根与共享根，另放两个不同名称的探针，分别只链接进 `.dsh/skills` 和 `.agents/skills`；私有根当前不存在，创建它也须纳入授权。Codex 真实 community 抽检可用同样方式另建 `skill-pilot-nested-probe`；本轮已在临时 home 完成递归测试。

清理仅删除目标匹配的探针链接；探针文件暂留供复核，不执行递归删除：

```bash
python3 - <<'PY'
from pathlib import Path
home = Path.home()
target = home / '.skill-pilot/store/skill-pilot-probe'
for root in ['.codex/skills', '.agents/skills', '.claude/skills', '.gemini/config/skills']:
    entry = home / root / 'skill-pilot-probe'
    if entry.is_symlink() and entry.readlink() == target:
        entry.unlink()
PY
```

## S2 DSH 根目录与用量

静态依据：`/Applications/DeepSeek Harness.app/Contents/Resources/app.asar` 中 `@deepseek-ai/dsh-skill-filesystem/{README.zh.md,lib/index.js}`。默认根按 rank 为项目 `.dsh/skills` 100、项目 `.agents/skills` 200、自定义根 300、用户 `.dsh/skills` 400、用户 `.agents/skills` 500、可配置 bundled 根 600。本轮只管理用户全局根，项目与自定义根不纳入。环境变量 DSH_HOME、DSH_AGENTS_HOME 可改变位置，未启动真实会话去验证实际配置覆盖。

该提供方只发现根直属目录的 SKILL.md 或根下平铺 `.md`，不递归发现 community；支持动态目录刷新。默认 `.dsh/skills` 当前不存在，共享根有 90 个有效技能，所以看板 **90 是默认目录估算**。

用量证据位于 `~/.dsh/sessions/<project>/<session>/session.v{3,4}.jsonl.zstd`，另有 `storages/session_projcache` 的会话摘要。本次只解码前者，76 个文件中 v3 有 62 个、v4 有 14 个；解码 16,894 个压缩 frame，无解码错误。原始行中 `tool/call` 共 8,556 条，其中 `data.name == "skill"` 为 35 条。`data.arguments` 是 JSON 字符串，例如仅含 `name=agent-reach`、`name=simon-neat`，有 `seq` 和毫秒 `time`。另有 read/bash 读取技能文件的线索。

**关键陷阱**：Node 24 的单次 zstd 解码只读第一个 frame，会误判每个文件只有 session header。本轮按 `zstdDecompressSync(bytes, {info:true})` 的 `engine.bytesWritten` 推进，解码完所有 frame 后才切 JSONL。最小复现方式（只读输入，不运行 DSH）：

```javascript
const fs = require('node:fs'), zlib = require('node:zlib');
const bytes = fs.readFileSync(process.argv[2]);
let offset = 0, chunks = [];
while (offset < bytes.length) {
  const part = zlib.zstdDecompressSync(bytes.subarray(offset), {info:true});
  if (!part.engine.bytesWritten) throw new Error('No progress');
  offset += part.engine.bytesWritten;
  chunks.push(part.buffer);
}
const rows = Buffer.concat(chunks).toString('utf8').split('\n').filter(Boolean).map(JSON.parse);
console.log({rows:rows.length,skillCalls:rows.filter(r=>r.type==='tool/call' && r.data?.name==='skill').length});
```

v3/v4 中观察到相同 session 的历史调用重复，不能把 35 当作去重用量，更不能据此清理。未来需按 session id、版本、调用 id / 同会话技能去重；只计真实读取和调用，不计 request/context 的技能清单、write/edit 或发给别人的消息。Python 3.12 标准库不含 zstd 解码，后续需确认允许调用现有 Node 标准库，或选择符合原约束的替代方案。本轮没有新增用量解析模块或账本。

## S3 Antigravity 用量

本机 App 2.22.0。`~/.gemini/antigravity/conversations` 有 29 个 `.db`、10 个 `.pb`；CLI 对应目录另有 6 个 `.db`、3 个 `.pb`。数据库以 SQLite `mode=ro` 打开，无迁移或修复。

表 `steps` 含 idx、step_type、status、step_payload（blob）、step_format；其他表包括 trajectory_meta、gen_metadata、executor_metadata。旧 App 样本 `961007ab-…db` 的 idx=56：step_type=8、status=3、step_format=0，payload 为 13,393 字节，包含 view_file 与插件技能的 SKILL.md 路径；另有 idx=60 对项目中的 ui-ux-pro-max 读取。CLI 的 step_type=132 也含读取线索，但混有思考、内容回放和模型工具片段，不能直接按关键词计次。

结论：旧读取步骤可定位，但没有完成 protobuf 字段级稳定解析和新对话复现；本轮禁止 agy -p。**S3 未达完整用量通过标准**，档案仍为“暂时统计不到”，不允许用该数据触发清理。待后续授权新对话后，记录一次明确读取，对照步骤字段和时间，再制定去重规则。

## S4 lark-cli 更新写入范围

当前 canonical `~/.local/bin/lark-cli` 对应 `@larksuite/cli` 1.0.97。本轮只运行 `update --check --json`：exit=0、already_up_to_date、CLI current/latest=1.0.97、skills current/target=1.0.97、in_sync=true、layout=separate、official=28。真实目录前后指纹未变。

源码依据固定到安装版本：[update.go](https://github.com/larksuite/cli/blob/v1.0.97/cmd/update/update.go)、[updater.go](https://github.com/larksuite/cli/blob/v1.0.97/internal/selfupdate/updater.go)、[sync.go](https://github.com/larksuite/cli/blob/v1.0.97/internal/skillscheck/sync.go)、[layout.go](https://github.com/larksuite/cli/blob/v1.0.97/internal/skillscheck/layout.go)。

- CLI 升级阶段会执行 npm/pnpm 全局安装；技能同步阶段委托 `npx -y skills` 或 pnpm dlx，没有固定 skills 包版本，也没有按 Skill Pilot 接入名单指定 agent。
- **separate**：从官方 regular 索引安装指定 lark-* 全局技能；失败可退回 GitHub 来源。已有 lark-suite 可能被移除。
- **suite**：先在 staging 目录准备 lark-suite，随后全局安装；原 separate 官方技能可能被移除。不是单纯新增一个文件夹。
- skills CLI 写来源记录。当前 registry 的 **skills 1.7.2** 源码与本机缓存 1.7.0 均将记录写到 `~/.agents/.skill-lock.json`；如果 XDG_STATE_HOME 存在，则改到其 `skills/.skill-lock.json`。
- 当前 1.7.2 的 canonical 全局技能目录是 `~/.agents/skills`。自动选择检测到的 agent（加 universal agents），并给非共享的 agent 放对应入口。Claude 为 `~/.claude/skills`；Antigravity 与 CLI 分别为 `~/.gemini/antigravity/skills`、`~/.gemini/antigravity-cli/skills`，**不等于当前实际读取的 `.gemini/config/skills`**。其它已安装 agent 也可能受影响，不只四个接入对象。缓存 1.7.0 的 Codex 路径仍是 `.codex/skills`，说明不能假定动态 CLI 的目录行为恒定。

skills 1.7.2 是只读下载 [官方 npm 发布包](https://registry.npmjs.org/skills/1.7.2)后读取 dist/cli.mjs，未执行包内代码或 npx；包源码项目为 [vercel-labs/skills](https://github.com/vercel-labs/skills)。后续 M4 应把 CLI 升级与技能导出隔离到临时环境，确认实际写出目录后收编；若不能限定写入范围，采用 PLAN 的人工升级提示与全量重扫方案。`.skill-lock.json` 对 Skill Pilot 只读，不代表上游 skills CLI 不会修改它。

## S5 来源归属

逐项表见 [source-attribution.md](source-attribution.md)。54 个直属技能与 2 个 community 技能全部列出，实际名称 `huashu-nuwa` 与安装目录 `nuwa-skill` 的差异已保留。

证据仅来自 SKILL.md 的明确来源、config 的项目目录名、工作台两份专项清单、PLAN 的 source-synced 记录及定向的官方仓库核对。obsidian-cli / obsidian-markdown 入口与当前 kepano/obsidian-skills 上游逐字相同，defuddle 不同。没有把功能相似、作者名或一个正文仓库链接当作整个目录与当前上游一致。

尤其是 unslop：当前 metadata 为 claytonkim/2.3.0，旧候选池里的 MohamedAbdallah-14/unslop 不足以归属此副本，因此仍列低置信度。工作台三份 source-synced 与当前安装副本存在差异，已进入只读冲突组；本轮没有选择覆盖哪份。

## S6 GitHub 目录版本检查

| 技能 / 来源目录 | lock 的目录 tree SHA | 当前目录 tree SHA（API = 下载） | 本机内容对照 | API 次数 |
| --- | --- | --- | --- | --- |
| brandkit / Leonxlnx/taste-skill `skills/brandkit` | d278ff748cafab01e4ae5dd7758fc0b53258621c | d278ff748cafab01e4ae5dd7758fc0b53258621c | 文件逐项一致，无更新 | 3 |
| academic-cv-builder / Paramchoudhary/ResumeSkills `skills/academic-cv-builder` | 86b6f36051bb742e5f029c692060b01b606e2430 | 86b6f36051bb742e5f029c692060b01b606e2430 | 文件逐项一致，无更新 | 3 |
| gzh-design / isjiamu/gzh-design-skill 根目录 | ba1f4175519b481cb3566616c9e5178705067904 | 973c34cd43bcb1566dd02cb9a55ae8d6ed24d02f | 有更新，10 个文件不同 | 4 |

每个样本先查仓库默认分支，再取分支指向的 commit，再查 recursive tree，要求结果未 truncated。浅克隆仅写系统临时目录，检查克隆 HEAD 与查询 commit 一致，随后 `git rev-parse HEAD:<directory>` 或根目录 `HEAD^{tree}` 验证目录版本；实际文件逐项 SHA-256 对照，忽略 .git 与运行缓存。来源分别为 [taste-skill](https://github.com/Leonxlnx/taste-skill)、[ResumeSkills](https://github.com/Paramchoudhary/ResumeSkills)、[gzh-design-skill](https://github.com/isjiamu/gzh-design-skill)。

根目录样本发现 API 别名陷阱：用 commit 调 git/trees 时，返回顶层 sha 不能直接拿来当根 tree SHA。本次该字段为 commit `fa4b78fb…`，与实际 `HEAD^{tree}` 不同；追加 GET git/commits/<commit> 取 `tree.sha` 后与下载一致。**后续更新检测先解析 commit 的 tree，禁止把 commit SHA 当目录 SHA。**三个样本全部最终通过，包含有更新与无更新两种情况。

gzh-design 不同文件为 SKILL.md、assets/preview-template.html、5 个主题说明、scripts/extract_docx.py、scripts/test_validate_gzh_html.py、scripts/validate_gzh_html.py。三个脚本的变化应在后续更新页醒目标出。本轮仅报告，未更新。

## S7 绝对路径引用

清单见 [path-references.md](path-references.md)。扫描技能目录（跟随兼容链接）、Example-Vault、Example-Notes 的 notes/samples/extras，以及 Codex AGENTS.md、Claude CLAUDE.md；排除依赖、版本库和会话 JSONL。仅输出文件路径与被引用技能名，不保存私人正文。

命中 87 个文件；按路径族为 `.codex/skills` 55、`.agents/skills` 21、`.claude/skills` 37，三个集合相互重叠。与 PLAN 粗查 47/16/29 的差异来自跟随链接后覆盖各逻辑入口，以及当前完整目录范围；不是 113 个独立文件。唯一扫描警告是已知 doc 断链。

影响分两步：**迁移且原入口保留**时，旧绝对路径可继续通过入口访问；**撤下对应入口**时，引用该入口的调用、脚本、规则或说明会失效，撤下前按本清单检查。内置 `.system` 只读，不能作为撤下目标；历史记录与示例引用不是当前流程的自动阻断证据，后续提醒应区分实际执行依赖和历史说明。本轮不改路径。

## S8 Claude 桌面额外记录

检查 `~/Library/Application Support/Claude/claude-code-sessions` 与 `local-agent-mode-sessions`：

- claude-code-sessions 有 40 个 local_*.json 会话元数据，包含 cliSessionId、cwd、时间、标题、提示快照和工具表面快照；没有 messages 或工具事件逐字稿。
- 40 个 cliSessionId 全部能在 `~/.claude/projects` 的 45 份 JSONL 中找到同名逐字稿；因此不把桌面元数据再计一次用量。
- local-agent-mode-sessions 没有 JSONL，主要为下载的 skills-plugin、资产和其它会话元数据；没有发现额外 Skill 工具调用流。

结论：本机当前以 `.claude/projects` 为调用证据来源。目录缺少额外逐字稿不代表桌面所有模式都可统计；未来新格式仍需单独核验。本轮没有读取消息正文来做业务归档，没有复制完整会话。

## M1 扫描口径与限制

最终数量、目录完整性、测试与看板验收见 [m0-m1-report.md](m0-m1-report.md) 和 [evidence.json](evidence.json)。扫描支持单行/引号/折叠/保留换行的 scalar frontmatter，使用 Python 标准库；不是通用 YAML 解析器。无法读取的配置或位置会标为未知，不记为零。后台只保留启动时快照，重开看板重新盘点。

发现 26 组同名不同内容：22 组 lark-*，ui-ux-pro-max，以及三份工作台 source-synced 的差异；其中 marketing-router 有三种内容。Kimi 当前全局技能为 72 个而非 PLAN 的 70，按约定保持“已忽略”；WorkBuddy 170、Cursor 20，Trae 按各内置根分别展示。12 个插件**目录**断链另列，不能虚构成 12 个失效技能。

系统限制 12 个应用数据位置的读取，已经在 JSON / 看板中逐项列出；不扩展系统权限。未扫描项目技能、备份、临时目录或 references 子技能。该边界与目录分类单测分别验收。

后续需求修订建议仅写入回报，未改 PLAN：Claude 官方文档与 DSH 程序包均描述动态刷新，不能对所有 agent 保证“正在运行会话完全不受影响”；Antigravity 用量复现、动态 skills CLI 写入范围和 Python 3.12 的 zstd 解码仍需在相应里程碑解决。

## S1 真实目录探针结果（2026-10-10 用户授权，Claude 执行）

步骤：探针放在 `~/.skill-pilot/probe/skill-pilot-probe`（四个根之外的绝对路径），描述里带验证码 `SP-7Q4K`；在 `~/.codex/skills`、`~/.agents/skills`、`~/.claude/skills`、`~/.gemini/config/skills` 各放一个绝对路径链接；新会话要求只回复验证码。

| Agent | 命令 | 结果 |
| --- | --- | --- |
| Codex | `codex debug prompt-input "probe" < /dev/null` | 可见；两个根都链接同一目标时**只列出一次** |
| Claude | `claude -p "<问题>" < /dev/null` | 回复 `SP-7Q4K`；另外，正在运行的 Claude 桌面会话在链接建好后立即在技能列表里出现了探针（动态刷新实证） |
| Antigravity | `agy -p "<问题>" --print-timeout 0 --sandbox --dangerously-skip-permissions < /dev/null` | 回复 `SP-7Q4K` |
| DSH | `dsh --profile headless "<问题>" < /dev/null` | 回复 `SP-7Q4K`（`--profile headless` 可用） |

补充只读核对：Antigravity 新会话看不到只存在于 `~/.agents/skills` 的 resume-tailor、hypit、gzh-design（报告列表 48 条、去重 43 个），**Antigravity 不读共享根**；Claude 当前会话的技能列表也没有这三个，**Claude 不读共享根**。共享根 `~/.agents/skills` 的读者是 Codex 与 DSH。

清理：四个链接已按目标精确删除，探针目录移入废纸篓。清理后四个根与 `~/.codex/config.toml`、`~/.agents/.skill-lock.json` 的指纹与探针前一致，唯一新增是 `~/.claude/skills/synced/`。

**新发现：`~/.claude/skills/synced/<org>_<user>/`** 于 09:53:22 出现在探针期间，`.last-complete-round` 写着 `claude-desktop`，内容为 docs、docx、google-workspace、import-memory、morning、pdf、pptx、skill-creator、xlsx 及 `manifest.json`（4.2MB），对应 Claude 桌面会话里的 `anthropic-skills:*`。由 Claude 桌面 app 自己同步维护，判定为**只读的内置/账号同步目录**：扫描归为内置，迁移、收编、对账一律跳过，不得改为链接或移动。
