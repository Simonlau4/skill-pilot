# 交接：安装技能提速（后端）

2026-10-14，Claude 写给 Codex。先读仓库根目录 `AGENTS.md`（安全边界、解释器、测试命令），需要背景再看 `docs/STATUS.md`。

## 用户的问题

用户原话：粘贴一个链接进去，页面要读取半天；然后给技能打钩、点安装，弹出的窗口上还要再点一次安装；装完还要刷新一次，刷新之后又要调用 AI 去整理中文名和标签。“安装一个技能搞得太麻烦了。”

Claude 查到的原因：

1. **读取慢**：`github.inspect()` 把整个仓库下载下来再找技能。用户这次的 `https://github.com/lemomo-ai/lemo-opuscar` 约 120 MB，实测读取 71.6 秒，里面只有一个技能 `plugin/skills/lemo-opuscar`（两个文件）。Claude 用 git 手动试过：只下文件清单（`--filter=blob:none --no-checkout`），找出 `SKILL.md` 所在文件夹，再只取这些文件夹（sparse checkout），总共约 5 秒，取下来 588 KB。
2. **中文名和标签很慢**：用户真实看板正在给 131 个技能补中文说明和标签（今天新加的功能，旧数据都缺），第一批 10 个跑了 4 分钟以上进度仍是 0/131。每批 10 个、每个说明最多 1500 字全文翻译，一次请求要输出上万字，进度只有每批结束才动。新装的技能还得排在这 131 个后面。
3. 两次点安装、装完要刷新：前端问题，DSH 同时在改（见 `docs/handoff-install-flow-frontend.md`），不归你。

## 分工

- **你（Codex）**：`skill_pilot/sources/github.py`、`skill_pilot/labels.py`、对应测试。
- **DSH**：`skill_pilot/web/static/` 下的前端文件。**这些文件你不要改。** 接口字段不变，前端不依赖你的改动。
- **Claude**：审查、合并、`docs/STATUS.md`。
- 不要改 `skill_pilot/sources/update_github.py`（检查更新的下载逻辑），也不要改 `/api/install/*` 的请求和返回字段。

## 要做的事

### 1. 读取仓库只取技能所在的文件夹（`github.inspect`）

- 克隆改为 `clone --depth 1 --no-local --filter=blob:none --no-checkout`（有分支或标签时照旧加 `--branch`）；指定提交号的情况照旧 `fetch --depth 1 origin <提交>`，之后用这个提交。
- 用 `ls-tree -r -z --name-only <提交> -- <子路径或 .>` 列出文件；一个都没有就照旧报“这个仓库里没有找到指定的文件夹。”
- 取所有文件名恰好是 `SKILL.md` 的父文件夹。没有任何 `SKILL.md`：照旧返回空的 `skills` 列表（前端会显示“这个仓库里没找到技能”），不要报错。
- 有技能在仓库根目录（父文件夹是 `.`）时不做 sparse，照旧整个检出；否则 `sparse-checkout set`（cone 模式）只要这些文件夹。文件夹名可能以 `-` 开头或含空格，传参要安全（比如 `--stdin`，需要的话给 `git()` 加一个传入标准输入的参数）。
- 再 `checkout -q --detach <提交>`。之后 `scan_root`、`safe_skill`、名字校验、同名校验、`content_digest`、`tree_sha`、`commit`、返回结构全部照旧。仓库内容仍然只读取、不执行；`git()` 里关掉钩子等现有保护不变。
- 测试（`file://` 仓库）：
  - 技能文件夹之外放一个无关的大文件，读取后检出目录里没有它，而返回的 `digest`、`tree_sha`、`commit`、`repository_path`、`description` 与改前一致；
  - tree 子路径链接、技能在仓库根目录、指定提交号、没有技能的仓库、一个仓库多个技能，各一个；
  - 现有 `tests/test_operations.py`、`tests/test_m5_fixes.py`、`tests/test_update_check.py` 里用到 `github.inspect` 的照样通过。
- 实测：在系统临时目录里对 `https://github.com/lemomo-ai/lemo-opuscar` 跑一次 `github.inspect` 记下秒数（之前 71.6 秒），跑完删掉临时目录。只读取，不安装，不碰用户的 home。

### 2. 整理中文名：新装的先做，每批小一点（`labels.run`）

- 分批：每批最多 5 个技能，且这一批的说明合计不超过 3000 字（单个说明超过 3000 字的自成一批；单个说明仍按现在的 1500 字截断）。这样每次请求输出少，进度条一两分钟就动一次。
- 顺序：一个标签都没有的技能（`have` 里没有它）排在最前，其次才是只缺中文说明或标签的。
- 运行中插队：每批开始前重新读一次登记表，把“登记表里有、`have` 里没有、本轮名单里也没有”的技能插到待做的最前面，`progress['total']` 相应加上。这样用户在补齐 131 个的过程中装了新技能，最多等当前这一批做完就轮到它。
- 其余不变：已有中文名和分组的照旧只补说明和标签；每批做完就保存；被撤掉或删掉的技能照旧从结果里去掉；出错信息只露出 `ServiceError` 的文字。
- 测试（用 `tests/test_labels.py` 里的假 AI 服务）：分批规则（个数和字数两个上限）；没标签的排在缺说明的前面；运行中途往登记表加一个技能，它出现在下一批、`total` 变大；现有标签测试照样通过。

## 验收

1. 全量测试通过：`PYTHONDONTWRITEBYTECODE=1 ~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11 -m unittest discover -q`。
2. 写报告 `docs/install-flow-report.md`：改了什么、lemo-opuscar 前后读取秒数、新增测试、代码行数变化、没做或有疑问的地方。

## 边界

- 只在系统临时目录的模拟 home 上开发和验证；不要对真实 home 运行本工具（必须时带 `SKILL_PILOT_READ_ONLY=1`），不要碰用户正在运行的看板进程，不要读 `~/.skill-pilot/settings.json`。
- 不要 `git add`、不要提交；改动保持未暂存。
- 只用标准库。能少写就少写，不加配置项。
