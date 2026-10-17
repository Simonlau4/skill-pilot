# M2 统一仓库与首次迁移回报

状态：完成

## 给用户的总结

统一仓库、迁移核对页、备份和一键还原已完成。
模拟目录完整走过两轮迁移与还原，故障时也能自动恢复。
看板实际改选并执行成功，还原后文件、权限和原链接均与迁移前一致。
真实目录只生成了预览，四个技能根、设置和来源记录的指纹完全没变。
正式迁移仍等你核对并确认，本轮停在这里。

## 真实目录迁移预览

只读扫描共 128 个技能，26 组不同内容。完整数据见 [real-migration-preview.json](../work/real-migration-preview.json)。

来源可信程度：高 80 条、中 34 条、低 14 条；共 48 条需要在核对页确认。Codex 归属表原有的 34 条中、7 条低全部保留，另有 7 个来自其他目录且没有已证实来源的技能。未确认的默认登记为来源不明。

下表的“可调用”包含手动入口，“自动可用”不包含手动入口。

| 助手 | 迁移前可调用 | 预期迁移后可调用 | 迁移前自动可用 | 预期迁移后自动可用 |
| --- | ---: | ---: | ---: | ---: |
| codex | 96 | 96 | 94 | 94 |
| claude | 43 | 43 | 43 | 43 |
| antigravity | 38 | 38 | 38 | 38 |
| dsh | 90 | 90 | 90 | 90 |

每组的默认选择与理由：

| 技能 | 默认保留的内容位置 | 理由 |
| --- | --- | --- |
| agency-router | `~/Documents/Example-Notes/samples/agency-router` | 与已登记的工作台源码一致 |
| lark-approval | `~/.agents/skills/lark-approval` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-base | `~/.agents/skills/lark-base` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-calendar | `~/.agents/skills/lark-calendar` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-contact | `~/.agents/skills/lark-contact` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-doc | `~/.agents/skills/lark-doc` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-drive | `~/.agents/skills/lark-drive` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-event | `~/.agents/skills/lark-event` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-im | `~/.agents/skills/lark-im` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-mail | `~/.agents/skills/lark-mail` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-markdown | `~/.agents/skills/lark-markdown` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-minutes | `~/.agents/skills/lark-minutes` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-okr | `~/.agents/skills/lark-okr` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-shared | `~/.agents/skills/lark-shared` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-sheets | `~/.agents/skills/lark-sheets` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-slides | `~/.agents/skills/lark-slides` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-task | `~/.agents/skills/lark-task` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-vc | `~/.agents/skills/lark-vc` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-vc-agent | `~/.agents/skills/lark-vc-agent` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-whiteboard | `~/.agents/skills/lark-whiteboard` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-wiki | `~/.agents/skills/lark-wiki` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-workflow-meeting-summary | `~/.agents/skills/lark-workflow-meeting-summary` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| lark-workflow-standup-report | `~/.agents/skills/lark-workflow-standup-report` | 与已核实配套的飞书工具版本一致，内容也与当时记录一致 |
| marketing-router | `~/Documents/Example-Notes/samples/marketing-router` | 与已登记的工作台源码一致 |
| simon-neat | `~/Documents/Example-Notes/samples/simon-neat` | 与已登记的工作台源码一致 |
| ui-ux-pro-max | `~/.codex/skills/ui-ux-pro-max` | 未能离线证明与来源记录一致，默认选最近修改的那份 |

飞书默认选择依据 S4 已核实的配套版本以及 M1 当时记录的内容。本轮只读核对当前工具包版本、来源记录和内容；三者相符才使用该证据，没有联网检查或升级。摘要保存在 [lark-paired-snapshot.json](lark-paired-snapshot.json)。

以下目录原样保留：

| 位置 | 原因 |
| --- | --- |
| `~/.agents/skills/doc` | 链接指向的位置已不存在 |
| `~/.codex/skills/.system` | 内置、插件或桌面同步目录，只查看 |
| `~/.codex/plugins/cache` | 内置、插件或桌面同步目录，只查看 |
| `~/.codex/plugins/user` | 内置、插件或桌面同步目录，只查看 |
| `~/.codex/vendor_imports` | 内置、插件或桌面同步目录，只查看 |
| `~/.claude/plugins` | 内置、插件或桌面同步目录，只查看 |
| `~/.claude/skills/synced` | 内置、插件或桌面同步目录，只查看 |
| `~/.gemini/antigravity/builtin/skills` | 内置、插件或桌面同步目录，只查看 |
| `~/.gemini/antigravity-cli/builtin/skills` | 内置、插件或桌面同步目录，只查看 |
| `~/.gemini/config/plugins` | 内置、插件或桌面同步目录，只查看 |
| `~/Library/Application Support/kimi-desktop/daimon-share/daimon/skills` | 未接入的助手，保留原样 |
| `~/.workbuddy/skills-marketplace/skills` | 未接入的助手，保留原样 |
| `~/.cursor/skills-cursor` | 未接入的助手，保留原样 |
| `~/.trae/builtin` | 未接入的助手，保留原样 |

Example-Notes 三份源码仅参与版本核对，不移动或删除；Codex 停用设置和现有安装来源记录只读取。`nuwa-skill` 的实际名称为 `huashu-nuwa`，统一仓库使用实际名称，原文件夹入口保留；核对页会列出这个差异。

## 产出文件

- [store.py](../skill_pilot/store.py)：预览、来源与版本默认选择、仓库、基准副本、备份、还原及越界保护。
- [sync.py](../skill_pilot/sync.py)：由初始分配计算目录入口，保留原路径，支持共享根及重复对账。
- [skill-pilot](../skill-pilot) 与 [cli.py](../skill_pilot/cli.py)：本项目内的命令行入口，未安装到系统目录。
- [make-fake-home.py](../scripts/make-fake-home.py)：只读复制真实技能与必要的链接目标，设置文件仅保留技能段。
- [verify-m2.py](../scripts/verify-m2.py)：两轮完整演练、Codex 实际名单、重复对账和故障自动还原验收。
- [server.py](../skill_pilot/web/server.py)、[index.html](../skill_pilot/web/static/index.html)、[app.js](../skill_pilot/web/static/app.js)：中文迁移流程、完整差异查看、来源核对、确认与还原接口。
- [scan.py](../skill_pilot/scan.py)、Claude/DSH 档案：跳过桌面同步目录，独立展示只读内容，沿用 S1 已验证的发现范围。
- [test_store.py](../tests/test_store.py)、[test_web.py](../tests/test_web.py)：迁移与接口验收用例。
- [README.md](../README.md)、本报告及 [lark-paired-snapshot.json](lark-paired-snapshot.json)。

## 验证证据

测试环境为 Python 3.12.14，仅使用标准库；前端没有增加构建工具或第三方依赖。

```bash
python3 -m unittest discover -v
node --check skill_pilot/web/static/app.js
./skill-pilot migrate --plan --json > work/real-migration-preview.json
python3 scripts/make-fake-home.py > work/m2-fake-home.json
python3 scripts/verify-m2.py /private/var/folders/ms/q91q3xp96t397xghv571lx240000gn/T/skill-pilot-home-kq_2ds6i --output work/m2-rehearsal.json
```

结果：36 项单测全部通过；前端语法检查通过；真实预览只读成功。日志见 [m2-tests.txt](../work/m2-tests.txt)。测试覆盖来源优先与修改时间回退、相同内容合并、共享根、私有根、同名同目标只列一次、手动及停用入口、嵌套技能、备份还原、预览失效、模拟复制、目录越界和备份路径越界。目录版本计算另与系统版本工具的实际结果交叉核对。

两轮完整演练的命令行步骤：

```bash
HOME=<模拟目录> SKILL_PILOT_HOME=<模拟目录> python3 -m skill_pilot migrate --plan --json
HOME=<模拟目录> SKILL_PILOT_HOME=<模拟目录> python3 -m skill_pilot migrate --apply --plan-id <预览编号> --json
HOME=<模拟目录> SKILL_PILOT_HOME=<模拟目录> python3 -m skill_pilot backups --json
HOME=<模拟目录> SKILL_PILOT_HOME=<模拟目录> python3 -m skill_pilot restore <备份编号> --json
```

| 演练 | 备份编号 | 替换入口 | 还原后的指纹 | 重复对账 |
| --- | --- | ---: | --- | --- |
| 第 1 轮 | `20261010T171216357789` | 227 | 与迁移前完全一致 | 没有再次改动 |
| 第 2 轮 | `20261010T171224600030` | 227 | 与迁移前完全一致 | 没有再次改动 |

两轮收进仓库的内容完全相同；每次仓库只有一次初始记录、没有远程地址。技能文件的修改时间用于“最新”选择，目录重建时间不会影响选择。还原保留文件权限与链接文本，重建完子文件后再恢复目录属性。

Codex 实际名单检查使用下面的方式，仅运行本机调试输出，不启动模型会话。四个助手迁移后的自动名单和可调用名单均与迁移前一致；Codex 提示中的 94 个用户技能名称也逐项一致，版本和内容按核对页选择。Claude、Antigravity、DSH 均未启动会话。

```bash
HOME=<模拟目录> CODEX_HOME=<模拟目录>/.codex XDG_CONFIG_HOME=<模拟目录>/.config XDG_CACHE_HOME=<模拟目录>/.cache XDG_STATE_HOME=<模拟目录>/.state /Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex debug prompt-input probe < /dev/null
```

人为故障放在技能入口已经替换、后续写入尚未完成时：注入写入权限失败，返回“迁移没能完成，已经自动恢复原样。”；所有涉及目录的指纹恢复一致，半成品仓库与登记表被撤回。完整证据见 [m2-rehearsal.json](../work/m2-rehearsal.json)。

看板在 Chrome 原生界面实际完成：选择四个助手 → 将 agency-router 从默认工作台源码改选为原 Codex 副本 → 确认迁移 → 看到完成页 → 一键还原并确认 → 看到“已还原到迁移前”。实际内容与改选一致，备份为 `20261010T170918666953`；还原后指纹完全一致。证据见 [m2-ui-acceptance.json](../work/m2-ui-acceptance.json)。测试标签页已关闭，本机服务已退出。

模拟复制额外只读带入一份技能内部链接所需的 `Example-Notes/notes/sample-cli-map.md`；同时记录工具包版本号用于离线来源核对，没有复制认证配置、会话记录或其他软件设置。复制后所有链接都留在模拟目录内。模拟目录与备份保留在系统临时目录供主控复核，当前位置见 [m2-fake-home.json](../work/m2-fake-home.json)。

真实目录前后对照：`unchanged=true`；`~/.skill-pilot/` 仍不存在。覆盖四个技能根、Codex 设置和安装来源记录，完整对照见 [m2-real-fingerprints.json](../work/m2-real-fingerprints.json)。本项目没有提交或推送，PLAN.md 和 spikes.md 未改动。

## 未决项

- 真实迁移仍需用户亲自在核对页确认；本轮真实写入被保护关闭，正式执行前由主控按新授权开放。
- 48 条中低可信来源需用户确认或保持来源不明，完整名单已在预览和核对页列出。

## 对 PLAN.md 的建议

- 迁移阶段让 Codex 与 DSH 一起选择，保留共享目录与已有停用设置；更细的独立分配交由 M3。
- 写明“最近修改”按技能文件的时间比较，避免备份还原重建目录影响默认选择。

以上仅为建议，没有修改需求正本；未开始 M3 或后续里程碑。

## 真实迁移结果（2026-10-10 21:14，用户在核对页确认，Claude 验收）

- 统一仓库 `~/.skill-pilot/store/` 收入 129 个技能，一次提交 `18506c8 首次收进统一仓库`；工具备份 `~/.skill-pilot/backups/20261011T041403221947`；另有主控独立备份 `~/.local/backups/skill-pilot-premigration-20261010/skill-roots-2112.tar`。
- 四个根：`~/.codex/skills` 54 个入口指向仓库（`community/` 内 2 个嵌套入口同样指向仓库；`.system` 与空目录 `codex-primary-runtime` 原样保留）；`~/.agents/skills` 91 个指向仓库，断链 `doc` 原样保留；`~/.claude/skills` 43 个指向仓库，`synced/` 原样保留；`~/.gemini/config/skills` 38 个指向仓库。
- `~/.codex/config.toml`、`~/.agents/.skill-lock.json` 与迁移前备份逐字节一致。
- 可见名单：用迁移前备份重建的模拟 home 扫描，对比迁移后真实扫描，Codex 95、DSH 91 完全一致；Claude 43、Antigravity 38 一致（重建时 stss 指向某个本机私人技能目录的链接不在备份范围，属重建假象）。
- 新会话实测：Codex `debug prompt-input` 列出 lark-base、simon-neat、reverse-engineer-anything、agency-router；Antigravity 报告 lark-base 为 1.2.23（迁移前其副本为 1.2.0）、能看到 stss；DSH 能看到 reverse-engineer-anything、lark-base、simon-neat；Claude 能看到 natural-text-writing、stss、simon-neat。
- 迁移前 Claude 修正：Example-Notes 源码不再优先（agency-router、marketing-router 默认改选 9/16 的较新安装副本）；真实目录写入需 `SKILL_PILOT_ALLOW_REAL_HOME=1`；48 个来源按用户要求自动比对登记（见 `docs/source-attribution.md`）。
