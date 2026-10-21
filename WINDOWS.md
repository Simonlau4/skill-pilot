# Windows 安装和更新

## 要求

- Windows 10 / 11；Python 3.12 或更新版本，安装时勾选加入 PATH。
- Git for Windows，并确保 `git --version` 可用。
- 在 Windows 设置中开启开发者模式，以允许创建技能目录链接。首次整理前程序会检查此条件；安装程序不会更改系统权限。

## 安装

从本仓库获取完整源码或下载并解压源码 ZIP，在项目文件夹运行：

```powershell
python -X utf8 -B scripts/install.py
```

也可在 PowerShell 运行 `./scripts/install-windows.ps1`。随后在开始菜单搜索 **Skill Pilot** 打开看板。项目文件夹需要保留；移动项目或 Python 后，在新位置重新运行安装命令。

命令行入口位于当前用户目录的 `.local/bin/skill-pilot.cmd`。安装器按当前用户环境创建入口，不要求特定用户名或固定项目位置。程序、技能数据和个人设置分开存放，安装程序本身不会整理现有技能；首次整理仍由用户在看板核对。

## 更新

打开看板时会自动检查官方 main 分支。有新版时，在技能页左栏或 **设置 → 版本** 点 **一键升级 → 刷新生效**。程序、开始菜单和命令行入口一起更新；已有技能、分配和账号设置保留。Windows 刷新保留原地址和会话。

从早期本地适配版升级到本版本时，安装后正常关闭全部看板页面，再从开始菜单重开一次，以切换后台刷新程序。之后的普通升级可以直接刷新生效。

普通源码安装和源码 ZIP 安装均可使用。程序存在本地改动时先保留原版；新版无法载入时不安装；安装失败会自动恢复原程序与启动入口。关闭所有页面后程序自动退出，不安装常驻升级服务。

尚未发布到官方 main 的开发分支不能自动切换到无关的正式版本。应在 PR 合并后使用官方 main，或下载对应的完整正式源码包。

## 边界与排查

Windows 版不自动升级飞书 CLI；这项能力在 Windows 上明确拒绝，按飞书工具的公开步骤单独升级即可。

打开失败可查看项目目录的 `launcher.log`。日志可能包含本次看板的本机地址，不应公开分享。程序更新失败详情保存在当前用户的 `.skill-pilot/logs/program-update.jsonl`。

## 开发验证

```powershell
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
python -m unittest discover -q
node --check skill_pilot/web/static/app.js
```

测试只使用系统临时目录中的模拟用户。Windows 专项覆盖新安装位置、含空格和中文的路径、连续两版升级、失败回退、本地改动保护、无法载入新版时保留原版，以及原地址与会话刷新。macOS 进程机制和 Unix 权限位的测试只在对应平台运行。
