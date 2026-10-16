# 技能中文名与 AI 设置验收报告

2026-10-12，工作区 `~/Developer/skill-pilot-llm-labels`。

## 完成的改动

- 新增“设置”页，包含六个服务选项、可修改的地址和模型、密码输入框、保存并测试、确认后删除 key。保存连接成功后开始给缺少中文名的技能起名；已有设置时，每次打开页面自动补齐一次缺项。
- 中文名、说明与分组改为读用户目录的 `labels.json`，删除仓库里的 `docs/skill-labels.json`。没有有效分组时，原名和原说明直接平铺，并提供设置入口；原有分组、默认收起、排序和其他页面的中文名显示沿用已有实现。
- 增加后台整理、按技能计数的进度、失败重试、只补缺项和重新整理全部。整理独立运行，技能分配照常操作；完成后刷新列表时保留未应用的勾选。
- 更新 `AGENTS.md` 的自动调用规则、私人数据边界和模块列表；更新 `docs/STATUS.md` 的实现、代码量和验证结果。

## 关键设计

`labels.py` 使用标准库和 OpenAI 兼容接口，每批最多 25 个技能，按名字顺序处理。每个技能只发送名字与最多 500 字的说明，后续批次带上已出现的组名。提示词要求资料中的指令不可执行，只返回指定 JSON；解析兼容代码围栏，忽略本批以外的技能和重复记录，空项留作待整理，字段裁到约定上限。显示内容经现有 `escape()` 或浏览器文本属性安全写入，不作为页面代码执行。

设置和整理结果都先写同目录临时文件再替换，并经过目录保护。设置临时文件使用 `os.open(..., O_WRONLY|O_CREAT|O_EXCL, 0o600)`，已有设置文件即使权限被放宽，再保存也恢复 0600。接口只提供是否保存和末四位；服务错误不读取、透传服务商错误正文，改用约定的中文提示，底层异常也不会带出请求头或 key。拒绝服务重定向，避免把请求凭据送到另一个地址。

`missing` 保留已有组和组内顺序，新组接在后面；每批成功后立即保存，所以后续失败不会丢掉前面的结果。`all` 先保存原文件为 `labels.previous.json`，首批不给已有组名，结果按组内技能数降序。保存前重新读取登记表，去掉已不存在的技能。中文名和设置不参与操作记录、撤销或 `revision()`，用量与技能操作备份原有固定清单也不包括它们。

模拟目录复制明确跳过 `settings.json`，包括通过其他目录链接发现的设置文件依赖。只读模式禁止保存、测试请求和启动整理，前端也不会自动调用。

## 验证

新增 12 项单测，原有两个中文名用例改为读取模拟目录数据。覆盖设置权限与回读、空值保留与清除、只读及越界拒绝、备份与撤销隔离、正常和围栏回复、字段截断与发送范围、缺项与旧组复用、未知与重复记录、全部整理备份与排序、坏文件容错、中文错误与超时、重定向拒绝、模拟目录排除设置，以及整理期间实际完成技能分配。

全量测试使用指定解释器和临时模拟目录：

```sh
PYTHONDONTWRITEBYTECODE=1 ~/.local/share/uv/python/cpython-3.11-macos-aarch64-none/bin/python3.11 -m unittest discover -q
```

原始输出：

```text
----------------------------------------------------------------------
Ran 163 tests in 41.893s

OK
```

页面脚本检查：

```sh
node --check skill_pilot/web/static/app.js
```

原始输出为空，退出码 0。改动空白检查也通过。

冒烟用临时生成的 26 个样本技能作为 `--source-home`，先通过 `scripts/make-fake-home.py` 生成模拟目录，再在该目录启动 `SKILL_PILOT_HOME=<模拟目录> BROWSER=true PYTHONDONTWRITEBYTECODE=1 <指定解释器> -m skill_pilot ui --json`。启动时原有自动安装流程增加一个管理技能，因此整理总数为 27。假服务只监听 `127.0.0.1`。通过带本次页面口令与同源校验的 HTTP 请求，完成保存、测试、补齐、删除一条中文名后仅补此项、全部整理与备份核对；整理时持续发送页面心跳。测试后只结束本次创建的模拟看板与假服务，临时技能目录自动清除。

原始输出（地址端口由系统分配，末四位为虚构测试凭据）：

```text
make-fake-home: {"links_within_home": true, "settings_copied": false}
save: {"llm": {"provider": "other", "base_url": "http://127.0.0.1:52923/v1", "model": "local-fixture", "has_key": true, "key_tail": "1234"}}
settings_permission: 0o600
test: {"ok": true, "message": "连接成功，可以给技能起中文名了。"}
missing: {"running": false, "done": 27, "total": 27, "message": "已给 27 个技能起好中文名。", "error": ""}
table: {"skills": 27, "titles": 27, "groups": [{"name": "整理", "skills": ["demo-00", "demo-01", "demo-02", "demo-03", "demo-04", "demo-05", "demo-06", "demo-07", "demo-08", "demo-09", "demo-10", "demo-11", "demo-12", "demo-13", "demo-14", "demo-15", "demo-16", "demo-17", "demo-18", "demo-19", "demo-20", "demo-21", "demo-22", "demo-23", "demo-24", "demo-25", "skill-pilot"]}]}
missing_one: {"progress": {"running": false, "done": 1, "total": 1, "message": "已给 1 个技能起好中文名。", "error": ""}, "sent_names": ["demo-07"]}
all: {"running": false, "done": 27, "total": 27, "message": "已给 27 个技能起好中文名。", "error": ""}
previous_backup_exact: true
key_absent_from_all_checked_api_responses: true
ai_calls: 6
SMOKE OK
```

前端逻辑检查用 Node 自带执行环境与轻量页面对象运行现有 `app.js`，验证无分组平铺、文本安全、未应用勾选保留、每次打开仅自动启动一次、只读时不调用，以及六个服务默认值。它不替代浏览器视觉验收。

原始输出：

```text
UI: no key shows flat rows, original description, settings hint; naming disabled
UI: names, summaries and group text escaped; background refresh preserves pending selections
UI: automatic missing run once per opening; read-only mode does not call AI
UI: all six providers fill the required defaults
UI LOGIC OK
```

代码量为 `skill_pilot/` 的 Python、JavaScript、HTML 共 6,508 行，样式 846 行。以交接时已有基础为基准，本次功能代码净增加 360 行（含模拟目录脚本、空行及注释，不含测试），略高于 350 行左右的目标；保留了密钥重定向保护、坏文件验证和未应用勾选保护。

## 边界与后续

本次所有运行与写入仅限此工作区和系统临时目录；不读取或修改真实技能数据，不运行真实看板、不重建启动器、不调用真实 AI 服务，不新增依赖，不暂存、不提交。交接时已有基础的暂存清单与统计保持原样。

本交接要求的测试、脚本检查、HTTP 冒烟、代码量核对和报告均完成。浏览器实际页面验收由主控继续完成；真实用户中文名数据的迁移也留给主控在验收后执行。各服务的实际连通性按边界未测试。
