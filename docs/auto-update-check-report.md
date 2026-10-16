# SP 启动自动检查新版：后台验收

日期：2026-10-16。对应 #37 第一步，依据 [交接说明](handoff-auto-update-check.md)。

## 改动

| 位置 / 函数 | 变化 |
| --- | --- |
| `skill_pilot/web/server.py` · `run()` | 新看板取得启动锁后、允许写入时，调用 `start_program('check', automatic=True)`。后台检查与看板服务并行；接上已运行的看板和 `resume` 路径均跳过。 |
| `Dashboard.__init__()` | 进度补上 `automatic` 默认值，兼容旧会话；构造后台对象本身不启动检查。 |
| `Dashboard.start_program()` | 增加 `automatic=False` 参数，记录本次任务的来源，并传给工作线程。正在运行时仍直接返回当前进度。 |
| `Dashboard.program_work()` | 自动、手动检查继续共用 `self_update.check()` 和 `program_review`。自动检查失败恢复中性提示；手动失败沿用原来的原因和报错状态。 |
| `README.md` | “更新程序”补上启动自动检查说明，并改正原来“只在点击时联网”的描述。 |
| `tests/test_auto_update_check.py` | 新增 10 项回归测试，实际运行看板启动流程和本机接口，检查结果使用测试替身，不联网。 |
| `tests/test_m7.py` | 现有启动测试明确替换 SP 检查，避免完整测试在干净安装中访问公开仓库。 |
| `tests/test_self_update.py` | 现有升级进程测试先等启动检查结束，再发布模拟新版，避免两次检查争用同一任务。 |

`self_update.py`、更新来源、下载校验和安装方式没有改变；没有新增配置或依赖。

## 给第二步页面使用的字段

`GET /api/program/progress` 新增布尔字段 **`automatic`**：

- `true`：当前运行中的任务或保留的结果来自启动自动检查，成功和失败都保留这个标记。
- `false`：初始状态、手动检查或手动升级。自动检查中点手动检查时，返回当前自动任务，仍为 `true`；完成后再点才启动新的手动任务。
- 刷新应用新版时跟随 `program_progress` 保留；旧会话没有该字段时补为 `false`。

自动检查失败后的接口状态为：

```json
{
  "running": false,
  "phase": "idle",
  "message": "还没查到新版信息",
  "error": "",
  "available": false,
  "automatic": true
}
```

有新版继续使用 `phase="available"`、`available=true` 和原有 `target`；已经是最新版使用 `phase="latest"`。检查准备好的内容保存在原有 `program_review`，可继续通过原有升级接口使用。下载目录和内部预览内容不会进入进度接口。

本次交付后台部分；左栏提醒由交接中的第二步接入。

## 交接要求逐条验收

| 要求 | 实现与验证 |
| --- | --- |
| 1. 启动自动查一次 | 触发点只放在 `run()` 的非 `resume` 启动分支。测试覆盖首次自动检查、重复打开页面、两个页面报到、再次启动接回原看板、页面刷新、应用恢复，以及运行中手动检查复用任务。阻塞检查的测试还确认看板在检查结束前已经能响应请求。 |
| 2. 只读预览不查 | 使用现有 `preview_allowed()` 判定；只读启动测试确认检查次数为零，接口返回 `write_allowed=false`。 |
| 3. 自动失败为中性状态 | 工作线程统一捕获检查异常。网络异常、本地改动和版本分叉分别有测试，结果均为上述中性状态。手动检查仍有失败原因，并能再次成功检查。 |
| 4. 测试不联网 | 直接构造 `Dashboard` 不发起检查；新增启动测试替换 `self_update.check`；原有直接调用 `run()` 的测试增加替身；现有真实升级进程测试使用临时本地仓库，只读刷新进程不启动自动检查。 |
| 5. README 补说明 | “更新程序”已注明打开看板时自动检查一次；有新版时，设置里的“SP 自身更新”会直接出现“一键升级”，不用再点“检查更新”；手动检查用于想立刻再查一次时。 |

新增 10 项测试覆盖：启动一次及重复页面 / 启动、已经最新、只读启动、恢复会话、网络失败、本地改动、版本分叉、检查中手动复用、完成后手动失败并重试、单独构造后台对象不检查。

## 源码与 ZIP 安装进程实测

环境：macOS，Python 3.12.14，只用标准库。六个场景各用独立的系统临时目录、新模拟 home 和含空格的项目位置；项目名称还包含引号和特殊字符。模拟 home 仅由合成技能建立，没有复制真实用户的数据或设置。

步骤：

1. 复用 `tests/test_self_update.py` 的 `make_fixture()` / `publish()`：临时本地仓库代替公开远端。源码场景克隆该仓库；ZIP 场景按现有夹具复制不含 `.git` 的程序目录，并建立安装清单。只在这些临时副本里把 `SOURCE` 指向本地仓库。
2. 在临时目录创建合成来源用户，放入一份 `example` 技能；用 `scripts/make-fake-home.py --source-home` 生成新的模拟 home。通过现有 `run_installer()` 安装到这个模拟用户，从未运行 `scripts/build-app.sh`。
3. 有新版场景在安装后发布新的本地版本；已经最新场景保持远端不变；远端不可读场景临时改名远端目录。
4. 在各自含空格的项目目录启动真实进程：

   ```sh
   HOME="$FAKE" SKILL_PILOT_HOME="$FAKE" BROWSER=/usr/bin/true \
     PYTHONDONTWRITEBYTECODE=1 python3 -m skill_pilot ui --json
   ```

5. 从启动输出取得本次地址和口令，带 `X-Skill-Pilot-Token` 读取 `/api/program/progress`，等 `running=false` 后核对下表。启动后没有先调用手动检查接口。
6. 首次检查完成后保持远端不可读，重复读取页面和进度、调用 `/api/runtime/refresh`，确认原结果不变。再仅在临时副本给后台文件追加一行注释，触发实际程序切换，确认地址、进程和检查结果都保留；随后恢复该临时文件。
7. 远端不可读场景再调用 `POST /api/program/check`，确认手动失败为 `phase="error"`、`automatic=false`，保留原因“SP 更新没有完成，请检查网络和项目文件夹权限后重试。”
8. 核对检查前后的程序文件和模拟 home 内容指纹；六个场景均未变化。结束各自的测试看板，等待退出并清理临时安装与下载目录。

实际接口结果如下，六个场景均通过：

| 安装方式 | 远端状态 | `phase` | `available` | `automatic` | `error` | 提示 |
| --- | --- | --- | --- | --- | --- | --- |
| 源码 | 有新版 | `available` | `true` | `true` | 空 | 有新版，可以一键升级 |
| 源码 | 已是最新 | `latest` | `false` | `true` | 空 | SP 已是最新版本 |
| 源码 | 远端不可读 | `idle` | `false` | `true` | 空 | 还没查到新版信息 |
| ZIP | 有新版 | `available` | `true` | `true` | 空 | 有新版，可以一键升级 |
| ZIP | 已是最新 | `latest` | `false` | `true` | 空 | SP 已是最新版本 |
| ZIP | 远端不可读 | `idle` | `false` | `true` | 空 | 还没查到新版信息 |

所有行均为 `running=false`、`write_allowed=true`。两行有新版结果的 `target` 与各自临时远端最新版本一致。六个场景的重复页面和真实应用恢复均保留首次结果。

## 测试与交付边界

验证结果：

- 全量测试：`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -q`，291 项全部通过，用时 125.041 秒。
- 新增自动检查与现有启动相关测试：`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_auto_update_check tests.test_m7 -q`，17 项通过。
- 更新与刷新专项：`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_auto_update_check tests.test_self_update tests.test_runtime -q`，38 项通过。
- `git diff --check` 通过；页面文件无改动。

程序文件行数检查共 9,351 行。

没有对真实 home 运行本工具，没有操作用户正在运行的看板，没有改写真实启动入口。当前副本的修改均未暂存，未提交、未推送；原有未跟踪的交接文件保持原样。
