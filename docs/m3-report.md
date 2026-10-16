# M3 回报：分配、依赖、安装与收编

日期：2026-10-10（洛杉矶时间）。需求正本：[PLAN.md](../PLAN.md)；范围：[handoff-m3.md](handoff-m3.md)。

## 状态

**完成但有疑虑**：M3 功能与全部验收通过，真实目录指纹一致。旧单测曾在测试进程内临时设置真实写入许可，虽然没有执行真实写入，仍与本轮“禁止设置”的要求不一致；已修正，详情见“边界记录与未决项”。

## 给用户的大白话总结

- 可以在勾选表里分配技能，先看变化，确认应用后才生效。
- 依赖先核对，分配时自动带上；撤下有影响时会提示依赖和受影响文件。
- 安装、收编和撤销已完成，遇到同名内容会先进入对比。
- 63 项单测和逐项模拟验收通过，真实目录指纹完全一致。
- 旧单测的临时许可开关已改为测试替身，项目没有提交或推送。

## 用户以后怎么用

- 勾选表：先确认或否定依赖建议，再勾选助手，检查数量和描述字数变化，按“应用”。
- 安装：按“安装技能”，粘贴仓库或技能文件夹链接，选择技能和助手；助手默认不勾。
- 收编：顶部出现“发现 N 个新装的技能”时，按“一键收编”，保持原来的可见范围。
- 撤销：进入“操作记录”，按对应记录的“撤销”；重叠操作需先撤销后来的，外部改动需先核对。

本轮看板写操作限模拟目录。最新使用时间显示“尚未统计”，未提前实现用量账本、检查更新或清理。系统入口和启动器也未安装。

## 产出文件

| 文件 | 用途 |
| --- | --- |
| [operations.py](../skill_pilot/operations.py) | 分配草稿、依赖建议与确认、入库、收编、记录、撤销、写前状态核对和失败还原 |
| [codex_config.py](../skill_pilot/codex_config.py) | 保留原有配置字节，记录和维护工具自己的停用项，写后验证 |
| [sync.py](../skill_pilot/sync.py) | M3 按涉及技能的分配范围对账；保留原有 M2 迁移实现 |
| [sources/github.py](../skill_pilot/sources/github.py) 与 [初始化文件](../skill_pilot/sources/__init__.py) | 链接解析、浅克隆、技能识别、来源搜索、完整目录与历史版本比对、查询节流 |
| [cli.py](../skill_pilot/cli.py) | status、list、assign、unassign、absorb、install、log、undo，均支持 --json |
| [server.py](../skill_pilot/web/server.py) | 看板接口，沿用本机访问限制、随机口令和写入确认 |
| [index.html](../skill_pilot/web/static/index.html)、[app.js](../skill_pilot/web/static/app.js) | 勾选表、待应用、数量与字数变化、顶部新装提示、依赖建议、安装弹窗、同名对比、操作记录 |
| [make-fake-home.py](../scripts/make-fake-home.py) | 复制迁移后的统一仓库与登记表，排除备份，改写指向真实目录的绝对链接 |
| [verify-m3.py](../scripts/verify-m3.py) | 可重复的模拟验收，含 Codex 实际名单和三次撤销 |
| [test_operations.py](../tests/test_operations.py)、[test_sources.py](../tests/test_sources.py)、[test_web.py](../tests/test_web.py) | M3 核心行为、来源和接口用例 |
| [test_store.py](../tests/test_store.py) | 将旧的真实许可测试改为读取替身，保留原判断行为的覆盖 |
| [README.md](../README.md)、[.gitignore](../.gitignore)、本报告 | 使用说明、隔离浏览器日志、验收回报 |

## 验证命令与结果

```bash
python3 scripts/make-fake-home.py > work/m3-fake-home.json
python3 -m unittest discover -v > work/m3-tests.txt 2>&1
node --check skill_pilot/web/static/app.js
python3 -m py_compile skill_pilot/*.py skill_pilot/sources/*.py skill_pilot/web/server.py scripts/verify-m3.py
```

最终单测：**63 项全部通过**，见 [m3-tests.txt](../work/m3-tests.txt)。前端与 Python 语法检查通过。未引入运行时第三方依赖或前端构建工具。浏览器验收使用 Playwright，配置与下载缓存单独放在系统临时目录。

主验收命令：

```bash
python3 scripts/verify-m3.py /private/var/folders/ms/q91q3xp96t397xghv571lx240000gn/T/skill-pilot-home-02ny9s6k --public-review work/m3-public-repo.json --output work/m3-rehearsal.json
```

其中 `HOME_PATH` 为以下实际模拟目录：

`/private/var/folders/ms/q91q3xp96t397xghv571lx240000gn/T/skill-pilot-home-02ny9s6k`

输出：“M3 模拟验收通过：依赖、路径提醒、Codex 实际名单、安装、四类收编、三次撤销和 CLI。”完整结果见 [m3-rehearsal.json](../work/m3-rehearsal.json)。

命令行验收在子进程中设置 HOME、SKILL_PILOT_HOME、CODEX_HOME、XDG_CONFIG_HOME、XDG_CACHE_HOME、XDG_STATE_HOME，全部指向模拟目录。实际名单用本机 Codex 的 `debug prompt-input`，标准输入关闭，没有启动模型会话。

## 各项验收

| 项目 | 结果与证据 |
| --- | --- |
| 迁移后复制 | 复制 store、bases、registry、agents 和仓库历史，排除 backups；绝对链接改写到模拟目录，越界断言通过。[复制记录](../work/m3-fake-home.json) |
| 勾选表 | 浏览器先确认 grill-me → grilling；勾上 Claude 后两个一起选中，底部“将给 Claude 加 2 个”，页头 43 → 45、11,882 → 12,085 字；应用前实际仍 43，应用后 45，其他助手名单一致。[草稿证据](../work/m3-ui-pending-evidence.json)、[浏览器验收](../work/m3-ui-dependency-acceptance.json) |
| 应用与记录 | 每批分配在模拟统一仓库留下一个记录；写前备份并复查状态，外部改动会拦截。记录显示时间、动作、涉及助手，撤销实际反向恢复；依赖确认与否定也记录 |
| 共享根 | 只给 DSH 的 m3-dsh-only 在共享根有入口，Codex 私有根没有重复入口；实际 Codex 调试名单中不可见；原配置字节逐一保留。撤销后工具条目消失、配置与受影响目录指纹完全一致 |
| 依赖保护 | 已确认依赖自动带上并显示原因；撤下 Claude 的 grilling 被拦下，不改文件；明确确认后才可执行。拒绝建议后不再重复提出 |
| 固定路径 | 撤下 Codex 的 ascentiz-blog-writer 时，列出 S7 的 4 个受影响文件；详见下表和主验收记录 |
| 本地安装 | file:// 临时仓库完成正常入库，记录仓库、目录、提交编号和目录版本，写入 bases；另一份同名不同内容转入对比；测试安装程序没有运行 |
| 公开仓库安装 | 只读克隆 mattpocock/skills 的 grill-me，识别后装进独立模拟目录，基准副本存在，助手默认全不分配。[克隆识别](../work/m3-public-repo.json)、[独立安装](../work/m3-rehearsal.json) |
| 收编：已有来源记录 | 模拟 Codex 新装目录被发现并收编，继承 lock 记录；来源文件在操作期间保持原样；撤销恢复原真实目录和指纹 |
| 收编：比对成功 | 测试替身覆盖自动登记上游；另对真实 grill-me 完成 GitHub 搜索和历史目录一致性比对，见下面来源证据 |
| 收编：比对不上 | 测试替身返回自制，自动登记，没有来源确认弹窗；撤销恢复原真实目录 |
| 收编：同名不同内容 | 停留在对比，不覆盖；单测和完整演练均核对文件指纹没有改变 |
| 收编看板 | 顶部显示“发现 1 个新装的技能”，一键收编后仍只给 Codex，撤销恢复真实目录。[浏览器收编记录](../work/m3-ui-absorb-acceptance.json) |
| 安装看板 | 同一弹窗选技能和助手，四个助手默认不勾；重复安装转入对比页；撤销恢复指纹。[浏览器安装记录](../work/m3-ui-install-acceptance.json) |
| 连续三次撤销 | 三个分配记录倒序逐一撤销，每一步的技能内容、登记表、基准副本、入口和配置指纹都回到对应状态，见下表 |
| 较早记录撤销 | 单测验证撤销不重叠的早期操作会保留后来的分配；重叠变更提示先撤销后来操作。最新操作后的外部目录也不会被覆盖 |
| CLI | 八个命令均以 --json 验收；status 数量正确，Claude list 43 个；install/absorb 缺 --yes 被拒绝；依赖撤下返回提示且无写入；log、undo 正常 |
| 保护范围 | synced、.system、插件/内置、空目录、断链跳过；越界链接、危险名称、过期草稿、未确认写入和跨站请求被拒绝；写入故障自动恢复 |

固定路径提醒的 4 个文件：

- `~/.claude/skills/ascentiz-blog-writer/SKILL.md`
- `~/.codex/skills/ascentiz-blog-writer/SKILL.md`
- `~/.gemini/config/skills/ascentiz-blog-writer/SKILL.md`
- `~/Documents/Example-Vault/memory/draft/samples/sample-note.md`

公开安装的独立模拟目录：

`/private/var/folders/ms/q91q3xp96t397xghv571lx240000gn/T/skill-pilot-m3-public-home-85jeq67t`

真实来源比对读取 [mattpocock/skills 的 grill-me](https://github.com/mattpocock/skills/tree/main/skills/productivity/grill-me)。安装记录的仓库提交为 `49dd158d1076134a641b33efb035946536778336`；历史比对在 `fcf0071560d32913c9d4f820e0d7ca467c881619` 的整个技能目录完全一致，目录版本编号均为 `3df14e2d3a89459bf300614be9247e3ce74798f8`。两次来源证据独立保存在 [m3-real-source-verification.json](../work/m3-real-source-verification.json) 和主验收记录。

来源实现先做代码搜索，并优先核对已有来源线索，再分页检查该技能的历史版本，只有整个目录一致才登记。大型索引仓库的整仓清单曾被截断，已改为逐层查询目标目录；读取失败会保留新装目录并提示重试。代码搜索跨进程限每分钟 10 次。收编来源的两个分支用测试替身覆盖，真实只读历史比对另行完成。

三次撤销的对应记录：

| 原操作编号 | 撤销编号 | 状态指纹 |
| --- | --- | --- |
| `e090beb7fed5b7bc61c894b0853c51fa176b3040` | `f925894ee3a284d6443ea299337493c50515d9e3` | 完全一致 |
| `24e958279c7db2c351ceb47268c307981998b865` | `4efca17f4dec9360d0e19c25130e15dcf74962be` | 完全一致 |
| `441d681faab9078516297f1a00e2b32ca44a6b74` | `1f171675c8932d997dfce2a5b3b492fe4847dc58` | 完全一致 |

这里“指纹恢复”比较技能正文与附件、文件权限、入口文本、配置字节、registry、agents、bases。新增的备份、操作记录、仓库历史及验证工具自己的缓存保留，因此不把这些新增审计数据算入还原状态。

## 真实目录指纹

最终 `unchanged=true`，见 [m3-real-fingerprints.json](../work/m3-real-fingerprints.json)。前后按文件字节、权限和入口文本比较：

- `~/.skill-pilot/` 全目录，含已有备份与仓库历史。
- `~/.codex/skills`、`~/.agents/skills`、`~/.claude/skills`、`~/.gemini/config/skills`、`~/.dsh/skills`。
- `~/.codex/config.toml`、`~/.agents/.skill-lock.json`。

真实目录没有发现任何改动。项目没有创建提交或推送；`git rev-parse --verify HEAD` 仍没有结果。模拟仓库内按要求生成的操作记录及反向恢复记录均留在系统临时目录。测试浏览器和看板服务已经关闭，临时输入和验收数据保留供主控复核。

## 全部依赖建议

初次读取模拟仓库的 129 个技能，得到 **139 条建议，涉及 49 个技能**。以下每行左边的技能建议需要右边的技能；完整结构化列表见 [m3-dependency-suggestions.json](../work/m3-dependency-suggestions.json)。正文提及可能包括例子或可选工具，仍需用户确认或否定；本轮未写入真实登记表。

| 技能 | 建议需要 |
| --- | --- |
| agency-router | marketing-router |
| ascentiz-blog-editor | ascentiz-blog-writer、natural-text-writing、stss、unslop |
| ascentiz-blog-publishing | ascentiz-blog-editor、lark-cli-first、simon-neat |
| ascentiz-blog-writer | agent-reach、ascentiz-blog-editor、natural-text-writing、stss、unslop |
| ascentiz-facebook-voc-monitor | agency-router、agent-reach、humanizer-zh |
| darwin-skill | playwright |
| grill-me | grilling |
| huashu-design | playwright |
| huashu-nuwa | agent-reach、pdf |
| imagegen-frontend-mobile | image-to-code |
| lark-approval | lark-shared、lark-task |
| lark-apps | lark-drive、lark-shared |
| lark-attendance | lark-shared |
| lark-base | lark-apps、lark-doc、lark-drive、lark-shared、lark-sheets |
| lark-calendar | lark-contact、lark-im、lark-meeting、lark-shared、lark-task |
| lark-cli-first | lark-approval、lark-apps、lark-attendance、lark-base、lark-calendar、lark-contact、lark-doc、lark-drive、lark-event、lark-im、lark-live-meeting-assistant、lark-mail、lark-markdown、lark-minutes、lark-note、lark-okr、lark-shared、lark-sheets、lark-slides、lark-task、lark-vc、lark-vc-agent、lark-whiteboard、lark-wiki、simon-neat |
| lark-contact | lark-calendar、lark-im、lark-openapi-explorer、lark-shared |
| lark-doc | lark-drive、lark-shared、lark-whiteboard |
| lark-drive | lark-base、lark-doc、lark-markdown、lark-shared、lark-sheets、lark-wiki |
| lark-event | lark-im、lark-shared |
| lark-im | lark-shared |
| lark-live-meeting-assistant | lark-cli-first、lark-im、lark-shared、lark-vc-agent |
| lark-mail | lark-shared |
| lark-markdown | lark-drive、lark-shared |
| lark-meeting | lark-drive、lark-shared |
| lark-minutes | lark-meeting |
| lark-note | lark-meeting |
| lark-okr | lark-calendar、lark-openapi-explorer、lark-shared、lark-task |
| lark-openapi-explorer | lark-shared、lark-skill-maker |
| lark-sheets | lark-shared |
| lark-skill-maker | lark-openapi-explorer、lark-shared |
| lark-slides | lark-shared |
| lark-task | lark-meeting、lark-shared |
| lark-vc | lark-meeting |
| lark-vc-agent | lark-meeting |
| lark-whiteboard | lark-base、lark-doc、lark-shared、lark-sheets |
| lark-wiki | lark-base、lark-doc、lark-drive、lark-shared、lark-sheets |
| lark-workflow-meeting-summary | lark-doc、lark-meeting、lark-shared |
| lark-workflow-standup-report | lark-calendar、lark-shared、lark-task |
| marketing-management-15e | marketing-router |
| marketing-router | agency-router、marketing-management-15e |
| new400-cleaning-prompt | seedance-video-generation |
| openstoryline-use | openstoryline-install |
| playwright | pdf |
| resume-tailor | job-description-analyzer |
| simon-weekly-report | beautiful-feishu-whiteboard、lark-cli-first、lark-doc、lark-drive、lark-im、lark-whiteboard、obsidian-cli |
| super-build | ui-ux-pro-max |
| text-polish-router | human-writing、humanizer-zh、natural-text-writing、stss、unslop |
| ui-ux-pro-max | super-build |

## 边界记录与未决项

1. **旧单测的许可开关**：首次沿用完整单测时，`tests/test_store.py::test_real_home_writes_need_explicit_flag` 在单测进程中使用 `patch.dict` 临时把 `SKILL_PILOT_ALLOW_REAL_HOME` 设为 `1`，测试结束后恢复。此分支只调用 `write_allowed` 和 `guard`，没有执行真实写入，真实目录前后指纹完全一致。该动作仍违反了交接卡“禁止设置”的字面要求；执行前没有发现它是本轮的疏漏。现已改为模拟环境读取返回值，**不设置实际进程环境变量**，再跑 63 项完整单测全部通过。应用的真实写入保护和 Claude 的既有修改保留。
2. **writer 的两条依赖与当前正文不一致**：当前统一仓库中的 ascentiz-blog-writer 正文匹配到 natural-text-writing、ascentiz-blog-editor、agent-reach，没有出现 unslop、stss。本轮按 PLAN 和交接卡明确要求，将 unslop、stss 作为依赖建议补入，等待用户确认；没有自行将它们写成真实已确认依赖。建议主控核对这两条是否仍是当前业务依赖，再统一需求文字与技能正文。
3. **来源匹配口径**：本轮采用完整目录完全一致后自动归属，候选代码搜索取前 30 个结果；历史提交按每页 100 个继续分页。相似但不完全一致的内容没有自动归属，记为自制；查询失败则先停止收编，不把失败当作自制。建议在 PLAN 明确这个判定口径，尤其是本地改过且没有 lock 的技能。
4. **数量口径**：本次复制后 Codex 为 97 个可调用技能，其中 95 个自动可用、2 个仅手动调用；Claude 43、Antigravity 38、DSH 91。勾选表使用可调用数量，总览沿用自动可用数量，分别有说明。

PLAN.md、spikes.md 和来源归属表未修改。没有开始 M4 或之后的里程碑，没有安装系统入口，没有执行 lark-cli update、npx skills 或下载技能中的程序。
