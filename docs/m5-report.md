# M5 回报：用量、清理与交接修正

需求正本：[PLAN.md](../PLAN.md)；执行范围：[handoff-m5.md](handoff-m5.md)。完成日期：2026-10-11，洛杉矶时间。下表日期沿用记录中的 UTC 日期，完整时间见机器可读证据。

## 状态

**完成**。交接要求的功能、单测、模拟环境验收、真实环境只读预演和独立复算均已完成。Antigravity 按交接允许的降级方式保留“统计不到”。

## 给用户的大白话总结

- 可以查看技能用量，清理名单默认全选，取消勾选要留的即可。
- 清理只从助手撤下，文件留在仓库；可以恢复原分配，也可以撤销。
- 真实预演有 30 个候选，Codex 可省 3,181 字描述，DSH 可省 4,566 字；本轮没有实际清理。
- DSH 已可统计；Antigravity 仍统计不到，分配给它的技能不进清理名单。
- 真实仓库、技能入口和配置指纹全部不变；没有提交或推送项目代码，未进入 M6。

## 真实环境清理预演

完整结果：[real-cleanup-preview.json](../work/real-cleanup-preview.json)。共 129 个技能，30 个候选，其余技能的排除理由也在 JSON 中逐项保留。

| 候选技能 | 分配给谁 | 最后使用（助手 / 日期） | 描述字数 | 入库证据日期 | 时间依据 |
| --- | --- | --- | ---: | --- | --- |
| academic-cv-builder | DSH | 无记录 | 73 | 2026-08-07 | 安装记录 |
| application-form-filler | DSH | 无记录 | 127 | 2026-08-07 | 安装记录 |
| brandkit | Codex、DSH | Codex 2026-08-05 | 464 | 2026-06-12 | 安装记录 |
| career-changer-translator | DSH | 无记录 | 75 | 2026-08-07 | 安装记录 |
| cover-letter-generator | DSH | 无记录 | 77 | 2026-08-07 | 安装记录 |
| creative-portfolio-resume | DSH | 无记录 | 63 | 2026-08-07 | 安装记录 |
| design-taste-frontend-v1 | Codex、DSH | 无记录 | 254 | 2026-06-12 | 安装记录 |
| executive-resume-writer | DSH | 无记录 | 68 | 2026-08-07 | 安装记录 |
| gpt-taste | Codex、DSH | Codex 2026-06-29 | 312 | 2026-06-12 | 安装记录 |
| image-to-code | Codex、DSH | 无记录 | 555 | 2026-06-12 | 安装记录 |
| imagegen-frontend-mobile | Codex、DSH | 无记录 | 638 | 2026-06-12 | 安装记录 |
| industrial-brutalist-ui | Codex、DSH | 无记录 | 286 | 2026-06-12 | 安装记录 |
| interview-prep-generator | DSH | 无记录 | 73 | 2026-08-07 | 安装记录 |
| job-description-analyzer | DSH | 无记录 | 92 | 2026-08-07 | 安装记录 |
| linkedin-profile-optimizer | DSH | 无记录 | 81 | 2026-08-07 | 安装记录 |
| minimalist-ui | Codex、DSH | 无记录 | 145 | 2026-06-12 | 安装记录 |
| new400-cleaning-prompt | Codex | Codex 2026-06-26 | 270 | 2026-06-11 | 原目录文件时间（迁移备份） |
| offer-comparison-analyzer | DSH | 无记录 | 73 | 2026-08-07 | 安装记录 |
| portfolio-case-study-writer | DSH | 无记录 | 61 | 2026-08-07 | 安装记录 |
| reference-list-builder | DSH | 无记录 | 71 | 2026-08-07 | 安装记录 |
| resume-ats-optimizer | DSH | Codex 2026-08-08 | 99 | 2026-08-07 | 安装记录 |
| resume-bullet-writer | DSH | Codex 2026-08-08 | 89 | 2026-08-07 | 安装记录 |
| resume-formatter | DSH | 无记录 | 65 | 2026-08-07 | 安装记录 |
| resume-quantifier | DSH | Codex 2026-08-08 | 82 | 2026-08-07 | 安装记录 |
| resume-section-builder | DSH | 无记录 | 83 | 2026-08-07 | 安装记录 |
| resume-tailor | DSH | 无记录 | 73 | 2026-08-07 | 安装记录 |
| resume-version-manager | DSH | 无记录 | 81 | 2026-08-07 | 安装记录 |
| salary-negotiation-prep | DSH | 无记录 | 83 | 2026-08-07 | 安装记录 |
| stitch-design-taste | Codex、DSH | 无记录 | 257 | 2026-06-12 | 安装记录 |
| tech-resume-optimizer | DSH | 无记录 | 66 | 2026-08-07 | 安装记录 |

| 助手 | 全部候选撤下后能省的描述字数 |
| --- | ---: |
| Codex | 3,181 |
| DSH | 4,566 |
| Claude | 0 |
| Antigravity | 0 |

字数按字符计算。共用技能会分别计入两个助手的收益。没有可靠安装时间证据的技能按刚安装处理；既有技能优先采用安装记录，再用首次迁移备份中的原文件时间，最后才用日志首次出现。迁移重建后的 registry/store 时间不作为旧技能安装时间。

## 各助手用量覆盖

| 助手 | 状态 | 日志文件数 | 最早记录 | 最近记录 | 说明 |
| --- | --- | ---: | --- | --- | --- |
| Antigravity | 统计不到 | 0 | — | — | 一次隔离复现未得到可核验读取，不再重试 |
| Claude | 可统计 | 47 | 2026-09-28 | 2026-10-11 | 默认仅保留 30 天，本机现有记录从 9 月 28 日开始 |
| Codex | 可统计 | 3057 | 2026-04-20 | 2026-10-11 | 含当前与归档会话 |
| DSH | 可统计 | 78 | 2026-09-11 | 2026-10-11 | 10 组双版本会话编号一致；每会话每技能一次 |

真实用量：[real-usage.json](../work/real-usage.json)。真实账本仅写系统临时目录，未创建或改动真实 `~/.skill-pilot/usage.sqlite`。模拟 home 使用正式的账本位置。日志被删除后已记入的历史保留；新增内容从扫描位置继续读取，文件替换或截断会重新扫描并按会话去重。格式错误、压缩片段未写完或解码失败会使对应助手退出清理判定。

Antigravity 只启动了一次 `agy -p`，在系统临时目录设置隔离主目录，用 macOS 沙盒拒绝临时目录外的全部文件写入。只要求读取现有 grilling 技能并回答一句话。返回码为 1，未取得读取回执；只出现很小的隐式会话元数据，没有可验证的调用步骤，因此没有推定它已可统计。临时认证副本已清除。证据：[m5-antigravity-probe.json](../work/m5-antigravity-probe.json)。

## 用户以后怎么用

- **看用量**：看板打开“用量”，或运行 `./skill-pilot usage --json`。
- **清理**：先运行 `./skill-pilot cleanup --list --json`，明确同意后运行 `./skill-pilot cleanup --yes --json`，也可在看板确认执行。
- **留下**：看板取消勾选要留的技能，或加 `--keep 技能甲 技能乙`；确认这一批后，它们会记为今天留下，重新计时 60 天。
- **恢复**：在“已撤下”按“恢复原分配”，或运行 `./skill-pilot restore-withdrawn 技能名 --json`；操作记录中的“撤销”可恢复对应的先前状态。

写入前重新核对目录和最新用量；名单变化要求刷新。清理确认框列出会失效的写死路径文件，复用 M3 的路径证据。清理移除工具维护的对应停用条目，保留原有用户设置；每批一次仓库记录，并可撤销。来源补登记使用 `set-source`，必须带 `--yes`。

## 产出文件

| 文件 | 用途 |
| --- | --- |
| `skill_pilot/usage/{__init__,common,claude,codex,dsh,antigravity}.py`、`dsh_decode.js` | 会话调用解析、路径与名称映射、逐帧解码、增量账本和覆盖说明 |
| `skill_pilot/cleanup.py` | 全部清理条件、传递依赖保护、留下、撤下和恢复 |
| `skill_pilot/normalization.py` | 嵌套说明改名、引用改写及不写源文件的标准比较 |
| `skill_pilot/store.py`、`operations.py`、`updates.py` | 迁移、安装、收编、更新统一处理；来源补登记及备份撤销 |
| `skill_pilot/sources/github.py`、`update_github.py` | 标准处理后校准完整上游历史；浅克隆和按需读取历史对象 |
| `skill_pilot/cli.py` | 新 CLI 命令、合并编号自动选择合并、错误时保留预览 |
| `skill_pilot/web/server.py`、`web/static/{index.html,app.js}` | 用量、清理、已撤下页面和确认操作；依赖建议仅作折叠信息 |
| `skill_pilot/profiles/{dsh,antigravity}.py` | 更新已核实的用量状态 |
| `tests/test_usage.py`、`test_cleanup.py`、`test_m5_fixes.py`、`test_m5_web.py` | 用量、条件、恢复、来源、改名、预览修复与网页入口验证 |
| `scripts/verify-m5-{usage,cleanup,upstream}.py` | 独立复算与模拟验收 |
| `scripts/serve-m5-fixture.py`、`m5-playwright.py` | 临时看板与浏览器验收；浏览器和服务已关闭 |
| `docs/source-attribution.md` | interview-skills 根目录来源和完整比对证据 |
| `work/real-usage.json`、`real-cleanup-preview.json` | 真实只读预演 |
| `work/m5-*-acceptance.json`、`m5-independent-usage.json`、`m5-upstream-verification.json` | 结构化验收结果 |
| `work/m5-boundary-before.json`、`m5-boundary-comparison.json` | 真实环境前后指纹 |
| `work/m5-tests.txt`、`m5-cleanup-ui.png` | 测试输出与看板截图 |

## 测试命令与结果

```text
python3 -m unittest discover -q
Ran 118 tests — OK
node --check skill_pilot/web/static/app.js
node --check skill_pilot/usage/dsh_decode.js
均退出 0

python3 scripts/verify-m5-cleanup.py
清理 → 已撤下列表 → 恢复 → 撤销恢复 → 撤销清理：通过；指纹分别恢复；清理只增加一次提交。

python3 scripts/verify-m5-upstream.py
真实 huashu-nuwa 上游更新、模拟 Codex 可见性、真实来源补登记 CLI、撤销：通过。

python3 scripts/verify-m5-usage.py
独立复算与账本的会话去重结果、最后使用时间均完全一致；没有缺项或多项。
```

### 各验收项

- **用量解析**：Claude 的 Skill、Read、用户斜线命令，Codex 三种工具记录与嵌套执行命令，DSH 的 skill/read/bash；写入、编辑、系统清单不计使用。覆盖会话内去重、新旧路径、目录名不同、归档重复、DSH 多帧与双版本、增量追加、截断、日志删除、未完整写入、持久错误。缺安装证据时普通技能名称首次出现也只用于安装时间。
- **清理条件**：构造数据覆盖未分配、年龄未满 60 天、近期使用、统计不到的助手、传递依赖与循环、近期留下、缺证据，以及 60 天边界。全部取消勾选时也能记下“留下”。
- **完整恢复**：CLI、网页接口和真实浏览器都走过清理、恢复和两次撤销；保留仓库文件、恢复原来的 DSH 分配、移除和恢复工具停用条目，相关内容指纹完全一致。
- **嵌套改名**：安装、收编、迁移、更新和三方比较统一处理。已有同名目标文件时停止，保护文件；处理可重复执行，不会反复产生差异。结果列出改名和引用改写。
- **真实上游**：huashu-nuwa 上游固定到 `fe0374687037c4cc51a65c1e0c145afe2981dc69`。模拟更新后 15 个嵌套 `SKILL.md` 全部变为 `SKILL.example.md`，两份说明的引用同步改写。`HOME` / `CODEX_HOME` 指向模拟 home 运行真实 `codex debug prompt-input`，只看到 huashu-nuwa，看不到任何 `*-perspective` 或 x-mastery-mentor。按上游更新后本机修改提示为否。
- **合并预览**：带 `--review` 的参数错误保留整个预览；正确 `--merge-id` 自动采用合并方式，成功后才清理预览。
- **来源**：[interview-skills 上游根目录](https://github.com/jennifer88huang/interview-skills/tree/8f72957468e1a05225c20aaf7239dce514c181f7) 的全部文件和执行权限与本机一致，目录版本为 `7e3dc45f7927ea71d75e7b27922424a72e7b8cb7`。已检查目录历史，本机匹配当前版本。模拟 home 中真实 `set-source ... --yes` 成功，基准重新校准，撤销精确恢复。
- **依赖提示**：未判定建议仅在折叠区展示，不要求用户逐条确认，不阻塞安装、分配、更新或清理；已判定的 requires 仍参与分配和清理保护。
- **浏览器**：默认全选、取消留下、确认清理、已撤下恢复、操作记录撤销均通过，取消勾选保留焦点。截图已检查，1280×720、107,209 字节。没有 JavaScript 异常；只有未携带口令的页面图标请求被现有访问保护拒绝（403）。

独立复算不导入生产解析器，用另一套命令分词重新读扫描位置以内的原始文件，避免正在写入的会话造成时间错位。只输出助手、技能名、计数和脱敏差异，不保存对话正文。当前近 30 天共 **1385** 个会话×技能记录，Claude **57**、Codex **1280**、DSH **48**；10 组 DSH 双版本会话的编号完全一致。完整结果见 [独立复算证据](../work/m5-independent-usage.json)。

### 真实环境边界

[指纹对比](../work/m5-boundary-comparison.json)全部通过：`.skill-pilot/`、`.codex/skills/`、`.agents/skills/`、`.claude/skills/`、`.gemini/config/skills/`、`.dsh/skills/`、`.codex/config.toml`、`.agents/.skill-lock.json`。指纹包含文件内容、权限和链接文字，未跟随链接去改源文件。

本轮未启用真实 home 写入开关，未安装启动器或 CLI，未执行任何技能脚本，未运行飞书升级或 npx skills，未启动 Claude/DSH 会话。唯一 Antigravity 调用按交接的例外执行并隔离写入。项目目录不做 commit/push；模拟仓库的操作提交用于验证清理与撤销。

## 主控需要在真实 home 执行的命令

这两项真实写入由交接指定交给主控，本轮没有代执行。命令需在主控已有授权的真实目录写入模式下运行；当前只读保护会拒绝直接写入。

1. **interview-skills 来源补登记**（先做，消除旧来源位置导致的检查错误）

```bash
cd ~/Developer/skill-pilot
./skill-pilot set-source interview-skills --repo jennifer88huang/interview-skills --path . --yes --json
```

2. **huashu-nuwa 按用户已同意的上游更新**

```bash
cd ~/Developer/skill-pilot
./skill-pilot update huashu-nuwa --choice upstream --yes --json
```

主控更新后核对真实 store 中 15 个示例已改名、引用已同步、新 Codex 会话没有额外示例技能，并读回 registry 来源。两项都会进入操作记录，可以撤销。真实清理未获本轮执行范围，不在这两条命令中。

## 未决项与 PLAN 建议

- **Antigravity 用量仍统计不到**：本轮允许的一次隔离复现没有取得读取证据，遵照交接降级；已分配给它的技能受保护。没有擅自开展新调查。
- **安装日期文字可统一**：PLAN §4.5 仍写安装记录与目录创建时间中较早者；本轮按更具体的 M5 交接执行安装记录 → 原迁移备份文件时间 → 日志首次出现的证据顺序。建议主控把这一说明统一回 PLAN，避免重建 store 的时间误导后续执行。未修改 PLAN。
- 首次用量扫描需读取约 13 GB 历史，之后增量读取；来源历史较多时浅克隆后的首次校准也会较慢。均没有定时扫描或自动更新。
- Codex 的 debug 探针会在模拟 home 自动补齐配置，因此更新撤销验证在探针之前完成；探针所有运行目录均为模拟 home，真实 Codex 配置指纹一致。
