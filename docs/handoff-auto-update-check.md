# 交接：看板启动时自动检查 SP 新版（后台）

2026-10-16，Claude 写给 Codex。对应 GitHub issue #37 的**第一步（后台）**。先读仓库根目录 `AGENTS.md`，再读本文件。页面上的左栏提醒是第二步，等另一个页面改动合并后由 DSH 做，你这次不改 `skill_pilot/web/static/`。

## 用户的问题

SP 有新版时，现在要去“设置 → SP 自身更新”手动点“检查更新”。其他用户不会想到去点，用户本人也嫌麻烦。希望看板一打开就自动查，有新版再提醒。

## 现在的代码

- `skill_pilot/self_update.py`：`check(home)` 先用 `git ls-remote` 读公开仓库 `main` 的最新版本；和本机相同就直接返回；不同就下载到临时目录、校验，返回 `available`。本机程序有改动、与公开版本分叉时抛出带中文说明的 `ValueError`。
- `skill_pilot/web/server.py`：`Dashboard.__init__` 里的 `program_review`、`program_progress`；检查和升级都在后台线程里跑（`program_progress` 的 `running` / `phase` / `message` / `error` / `available`），页面通过 `GET /api/program/progress` 轮询、`POST /api/program/check` 手动检查。`resume` 不为空表示“刷新应用新版”时在同一次运行里换程序，带着上一份会话状态。
- 页面（不用改）：设置页在 `progress.error` 非空时显示红色报错，`available` 为真时显示“一键升级”。

## 要做的

1. **启动时自动查一次**：看板这次启动后，在后台线程里做一次和手动“检查更新”相同的检查，结果照常写进 `program_progress` / `program_review`。同一次运行里刷新页面、开新页面、刷新应用新版（`resume`）都不再自动查。手动检查照常可用；自动检查还在跑时点手动检查，沿用现在“正在跑就返回当前进度”的处理。
2. **只读预览时不自动查**。
3. **自动检查失败不算报错**：没网、程序文件有本地改动、与公开版本分叉等任何失败，`program_progress` 回到中性状态（不处于运行中、`error` 为空、`available` 为假，`message` 给一句中性的话，如“还没查到新版信息”），并带一个字段标明这是自动检查的结果（字段名你定，写进报告，第二步页面会用它）。这样现在的设置页在自动检查失败后不会出现红色报错。手动检查失败照旧把原因放进 `error`。
4. **测试不联网**：现有测试里有很多处直接构造 `Dashboard`，自动检查不能因此在测试里访问网络。新增测试覆盖：允许写入时启动后自动查一次；只读预览不查；`resume` 不查；自动检查失败为中性状态且有自动检查标记；有新版时 `available` 为真；自动检查后手动检查仍能跑、失败仍显示原因。
5. README 的“更新程序”一节补一句：打开看板时会自动检查一次，有新版会提醒。

## 验收

1. 全量测试 `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -q` 通过（用 Python 3.11.4 以上；不要写死解释器位置）。
2. 在系统临时目录里、项目位置含空格的条件下实测一遍：用临时目录里的仓库模拟“远端有新版”和“远端已是最新”，分别用新的模拟 home 启动看板（`SKILL_PILOT_HOME=... BROWSER=/usr/bin/true python3 -m skill_pilot ui --json`），读 `/api/program/progress` 确认结果；再模拟断网或远端读不到，确认是中性状态。源码安装和 ZIP 安装两种方式都要覆盖（参考 `tests/test_self_update.py` 现有夹具）。看完结束自己开的看板。
3. 报告写在 `docs/auto-update-check-report.md`：改了哪些函数、新增的进度字段、上面 1～5 条逐条怎么做到的、实测步骤和结果。

## 边界

- 不改 `skill_pilot/web/static/` 下的页面文件。
- 不对用户真实 home 运行本工具（需要时只能带 `SKILL_PILOT_READ_ONLY=1`），不碰用户正在运行的看板，不运行 `scripts/build-app.sh`。
- 不要 `git add`、不要提交；改动保持未暂存。
- 只用标准库，不加配置项。能少写就少写。
