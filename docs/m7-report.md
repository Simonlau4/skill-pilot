# M7 回报

需求正本：[PLAN.md](../PLAN.md)。执行卡：[handoff-m7.md](handoff-m7.md)。日期：2026-10-11，洛杉矶时间。

## 状态

**完成但有疑虑。** M7 功能、安装产物、124 个单测和模拟看板流程均完成。存在一次明确的执行范围违规，以及默认浏览器直接验收的限制，详见“未决项”。因此不声称本轮完全符合全部边界要求。

## 给用户的大白话总结

- 现在可以双击 `~/Applications/Skill Pilot.app` 打开看板，也已安装助手调用的命令入口。
- 清理、更新、分配和手动修改“需要的技能”都可在看板完成，并从操作记录撤销。
- 你改过的技能默认用上游版本；AI 只在主动点击可选按钮时调用。
- 管理技能已准备好，第一次正常双击会给四个已接入助手安装；本轮真实环境没有安装它。
- 功能验证通过；有一次禁止的脚本执行，需主控对照独立的任务前指纹复核。

## 用户第一次正常双击

启动器使用固定的 `~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11`，本机版本 3.11.15；不依赖终端里的 Python。解释器缺失时会弹出中文提示。启动器补齐工具路径，包括本机 Node，方便 DSH 用量解析。

普通双击会打开默认浏览器，自动将项目中 `skill/skill-pilot/` 的管理技能复制入统一仓库，分配给 Codex、Claude、Antigravity、DSH，先备份、校验并记一条安装记录。当前真实登记表有 129 个技能且没有它，首次正常启动后应增加这一行。Codex 与 DSH 共用 `.agents/skills` 入口，另有 Claude、Antigravity 的入口，符合 PLAN 的共享目录规则。

在“勾选表”找到 `skill-pilot`，可以取消某个助手的勾选后按“应用”。在“操作记录”撤销首次安装后，技能和入口会消失，再打开也不会自动重装。

- **清理**：打开“清理”，等名单生成。默认全部勾选，取消要留下的，按“清理选中的”并确认；文件仍在仓库，操作记录可撤销。
- **检查更新**：打开“检查更新”，按同名按钮，再勾选要装的新版。你改过的默认“用上游的”，可改为“保留我的”；先看脚本提示，再按“更新选中的”确认。合并和解释都是明确标出的可选按钮。
- **给 Claude 勾 grill-me**：打开“勾选表”，搜索 `grill-me`，勾 Claude 列，`grilling` 会一起勾上；按“应用”并确认。
- **修改需要的技能**：每行可以添加仓库中已有技能，或删除现有关系。添加会把所需技能一起分配；删除关系保留已有分配，两种操作都能撤销。

## 代码行数与删除清单

前后使用同一个命令：

```sh
find skill_pilot -name "*.py" -o -name "*.js" -o -name "*.html" | xargs wc -l
```

| 时点 | 总行数 |
| --- | ---: |
| 修改前，首次读取时实测 | 4,281 |
| 修改后 | 4,280 |

净减少 **1 行**，没有超过硬性上限。启动器、技能正文、测试和验收脚本不计入该命令。[最终逐文件计数](../work/m7-line-count-after.txt)。

删除了以下内容：

- 从 SKILL.md 正文匹配技能名、生成依赖建议的函数，以及安装、收编、清单中的建议返回值。
- M5 的折叠依赖提示、提示数量和相关展示逻辑；相应旧测试删除或改为“正文提及不产生依赖”的回归测试。
- 仅供猜测建议使用的“已否定建议”加载逻辑；手动删除已判定关系的记录仍保留，避免下次读取又加回来。
- 更新时强制先选处理方式的空选项与校验；合并改为可选按钮，默认使用上游。
- 迁移页面中逐项确认来源的旧界面，以及“再演练一次”的开发期按钮。来源登记和来源修改命令继续保留。
- `SKILL_PILOT_ALLOW_REAL_HOME` 保护分支及开发期“真实目录未开放”文字；重复 CLI 报错输出合并为一处。

## 产出文件

| 文件 | 用途 |
| --- | --- |
| `~/Applications/Skill Pilot.app/Contents/{Info.plist,MacOS/SkillPilot}` | Finder 启动壳，拉起后台后立即退出，支持再次双击 |
| `~/.local/bin/skill-pilot`、`scripts/build-app.sh` | 固定解释器的 CLI 入口与安装脚本 |
| `skill/skill-pilot/SKILL.md` | 查看、分配、撤下与本次对话授权规则；没有放进真实技能根 |
| `skill_pilot/{store,operations,updates,cli,cleanup}.py` | 默认写入权限、只读拒绝、首次安装、手动关系、脚本风险、默认更新选择 |
| `skill_pilot/web/server.py`、`web/static/{index.html,app.js}` | 首次安装入口、可写看板、手动关系按钮、更新默认选择及风险原文；UI 命令支持 JSON |
| `skill_pilot/usage/__init__.py` | 删除不再成立的开发期注释，沿用只读时的临时账本 |
| `tests/__init__.py`、相关既有测试、`tests/test_m7.py` | 公共 setUp 验证模拟 home 在系统临时目录；新增权限、首次安装、撤销与风险验证 |
| `scripts/serve-m7-fixture.py`、`verify-m7-ui.cjs` | 模拟看板与独立浏览器交互验收 |
| `scripts/verify-m7-launch.py`、`m7-launch-browser.{sh,cjs}` | 最小环境的 LaunchServices 启动、两次启动共用服务、关页退出实测 |
| `work/m7-*.json`、`m7-tests.txt`、`m7-line-count-after.txt`、两张截图 | 结构化证据、测试输出、行数与界面证据 |

## 验证结果

```text
~/.local/bin/python3.11 -m unittest discover -q
Ran 124 tests in 36.757s — OK

node --check skill_pilot/web/static/app.js
node --check skill_pilot/usage/dsh_decode.js
sh -n scripts/build-app.sh
plutil -lint ~/Applications/Skill\ Pilot.app/Contents/Info.plist
均通过
```

[单测输出](../work/m7-tests.txt)。技能正文的名称、说明和目录名已检查；未通过执行管理技能内脚本验证它。

| 验收项 | 结果与证据 |
| --- | --- |
| 首次自动入库 | 模拟 home 安装成功，四个助手可见；备份存在；重复启动不重复安装；撤销后文件和入口消失、指纹恢复，再开不重装 |
| 默认权限与只读保护 | 默认真实主目录及系统临时目录可写，其他位置仍拒绝；真实主目录权限分支只验证判定，不实际写入。只读模式拒绝 11 个后端写入口和全部 10 个网页写路由，模拟 home 指纹不变 |
| 写入保障 | 确认框、写前备份、写后可见性校验、失败还原、记录与撤销、路径越界保护继续保留；已有失败恢复与配置保护测试通过 |
| 清理 | 默认全选，取消 sample，清理其余，保留仓库文件，留下时间落盘；撤销恢复入口 |
| 更新 | 本机已修改的 sample 默认选上游，更新后采用新版；撤销恢复本机修改；保留本机和可选合并的既有测试继续通过 |
| 脚本风险 | 新增/改动的可执行文件，以及联网、删除、执行和安装关键词，列出文件和原文；普通说明文件不当作脚本报警 |
| Claude 分配 | 勾 grill-me 时 grilling 跟上，两者分配落盘；手动添加、删除另一条关系、撤销均验证；静态清单中的关系被手动删除后不自动加回 |
| 不调用 AI 的默认流程 | 模拟整套流程中 AI 入口被替换为“调用即失败”；全部通过，没有执行 sample 中的脚本 |
| Finder 环境与单实例 | `env -i` 经 `open -a` 启动，显式只读；两次启动打开两张页面，共用一个后台进程；真实数据可见、写控件禁用 |
| 关页退出 | 独立临时 Chrome 配置中的两张真实只读页面关闭后，后台 **23.588 秒**退出；默认 Chrome 的直接关闭限制见下文 |

[模拟交互证据](../work/m7-ui-acceptance.json)包含 16 项检查，无 JavaScript 异常。[启动与退出证据](../work/m7-launch-acceptance.json)。

![更新默认选择与风险原文](../work/m7-updates-ui.png)

[真实只读看板截图](../work/m7-real-dashboard.png)。两张截图均为 1280×720、低于 1 MiB，检查后没有放入任何技能目录。

### 真实目录指纹

[基线](../work/m7-boundary-before.json)与[最终比对](../work/m7-boundary-comparison.json)：以下 **8 项在本轮首份基线之后全部不变**，包含内容、权限与链接文字，不跟随链接修改源文件。

- `.codex/skills/`
- `.agents/skills/`
- `.claude/skills/`
- `.gemini/config/skills/`
- `.dsh/skills/`
- `.skill-pilot/`
- `.codex/config.toml`
- `.agents/.skill-lock.json`

所有对真实 home 运行 Skill Pilot 的开发验收均设置 `SKILL_PILOT_READ_ONLY=1`；没有真实分配、更新、清理、收编、改来源、撤销或管理技能自动入库。没有运行 `lark-cli update` 或 `npx skills`，没有对项目 commit/push，没有开始 M6/M8。本轮后台服务、模拟 home 和独立测试浏览器已结束并清理。

## 未决项

1. **明确的执行范围违规**：在读取 UI/UX 指引时，执行过一次下面的技能检索脚本。交接卡禁止“执行技能里的脚本”，这一步不应执行。此后没有再调用技能脚本，也没有请求持久化设计文件。

   ```text
   ~/.local/bin/python3.11 ~/.codex/skills/ui-ux-pro-max/scripts/search.py "confirmation destructive action" --domain ux -n 2
   ```

   首份指纹在这次调用之后、00:18:47（洛杉矶时间）采集，所以不能把上述 8 项一致当作整个任务开始前后不变的证明。当前环境禁用 Python 字节码写入，该技能脚本目录未发现缓存文件，但这不能替代任务前证据。请主控用自己的原始指纹核对；本轮没有擅自删除真实目录文件来消除痕迹。[违规及验证限制记录](../work/m7-scope-deviation.json)。

2. **默认浏览器的直接验收限制**：第一次普通浏览器路径下的只读 `open -a` 启动成功，服务持续收到页面心跳；自动审批拒绝了整窗读取，因为当前 Chrome 窗口包含与任务无关的私密认证信息。指定标签的浏览器连接也不可用，内置浏览器不可用。后续使用全新临时配置的 Chrome，仅访问测试看板，完成页面数据、双启动及 30 秒内退出验证。默认 Chrome 中第一次打开的只读测试页没有由自动化关闭，服务已停止；用户可关闭那张旧测试页。没有请求读取认证信息或扩大授权。

PLAN 没有需要本轮修改的需求遗漏；没有修改 PLAN 或进入后续里程碑。
