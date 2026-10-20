# 团队分享站：部署与管理

分享站只保存同事上传的技能名单、来源和分配关系，不保存技能文件，也不读取任何人的本机技能库或 Skill Pilot 设置。上传、浏览和导入由各自电脑的看板完成，见下文“同事怎么用”。这一部分提供分享站程序、成员管理和接口，没有网页界面。

分享站有两种加入方式：

- **开放加入**：启动时加 `--open`，适合可信局域网里的小团队。启动后把地址告诉同事，同事填自己的名字即可加入，不用管理员发口令。局域网里知道地址的人都能加入并看到全部分享，所以只在可信网络开放。
- **只限成员（默认）**：启动时不加 `--open`，管理员为每位同事生成口令并单独交付。

两种方式都使用成员口令认出每个人；开放加入时由看板自动领取并保存在自己电脑上，不用自己填写。每人一份分享，再次上传会覆盖自己的旧分享。管理员在运行分享站的电脑上用命令加、删成员；持有成员口令的人可以查看全体分享、上传或撤回自己的分享。

## 准备

- 一台一直开着的局域网电脑或 NAS。直接运行支持 Linux、macOS，需要 Python 3.11.4 或更高版本，只用标准库；容器方式使用官方 Python 镜像。
- 获取包含 `hub` 命令的 Skill Pilot 版本并解压。分享站无需安装桌面启动器，也不需要第三方账号或密钥。
- 准备独立的数据文件夹，运行账号必须能够读写它。程序文件夹和数据文件夹分开保存，路径可以含空格。每个数据文件夹只运行一份分享站；请使用本机磁盘上的文件夹，避免跨机器共享同一份数据。
- 电脑或 NAS 的局域网地址最好固定。默认监听所有网卡的 `8765` 端口；只放行可信局域网，关闭路由器端口转发，不向外网开放。端口占用时换一个端口，防火墙和访问地址也要一起改。

下面用 `/srv/Skill Pilot` 表示程序文件夹，`/srv/Team Hub Data` 表示数据文件夹。这两个都是示例位置，请换成自己机器上的位置，并保留引号。

**第一期在局域网内明文传输口令和名单。** 同一网络内能监听流量的人可能读到它们，口令持有者也能以该成员身份操作。只在可信网络使用；需要加密时，放在 NAS 或系统已有的加密访问入口后面，并禁止绕过该入口直连分享站。分享站自身不申请或管理证书。

## 方式 1：直接运行

先确认 Python 版本，再进入程序文件夹：

```bash
python3 --version
cd "/srv/Skill Pilot"
```

下面的启动示例采用开放加入，不用提前添加成员。如果要只限成员，先为同事添加成员（显示名可以重复，成员编号各不相同；去掉首尾空白后为 1–40 个字，不能换行或含控制字符）：

```bash
python3 -m skill_pilot hub add-member "示例成员一" --data "/srv/Team Hub Data"
python3 -m skill_pilot hub add-member "示例成员二" --data "/srv/Team Hub Data"
python3 -m skill_pilot hub members --data "/srv/Team Hub Data"
```

添加成功后显示成员编号和口令，**口令只显示这一次**，不会写入数据文件。请通过各自的私信或公司认可的密码交付方式，把分享站地址（例如 `http://<局域网地址>:8765`）和对应口令单独交给同事，不发到公开群或项目文件里。成员编号用于日后删除，查看成员命令不会显示口令。

在程序文件夹中，用下面这条固定命令启动；手动重启时仍用同一条命令：

```bash
/usr/bin/env python3 -m skill_pilot hub serve --data "/srv/Team Hub Data" --host 0.0.0.0 --port 8765 --open
```

看到“分享站已启动”即可使用，提示里会写明是否开放加入。只限成员时去掉命令末尾的 `--open`。终端需要保持打开；临时试用结束按 Ctrl+C 停止。只想在该机器上检查时可用 `--host 127.0.0.1`，同事将不能直接连接。省略 `--host` 和 `--port` 时分别采用 `0.0.0.0`、`8765`。所有命令都必须明确填写 `--data`，不会默认使用主目录。

### 电脑或 NAS 重启后自动运行

将上面的固定启动命令加入系统自带的开机任务，指定程序文件夹为工作目录、用有权读写数据文件夹的普通账号运行，并设置异常退出后重试。macOS 可使用系统的 launchd；不同 NAS 使用各自的任务管理入口。

常见 Linux 使用 systemd。先运行 `command -v python3`，确认开机任务能够找到同一个合适版本；若系统服务找不到它，将下方 `/usr/bin/env python3` 换成查到的 Python 程序完整位置。创建 `/etc/systemd/system/skill-pilot-team.service`，将 `<运行账号>` 替换为现有的普通系统账号，并按实际位置修改两个文件夹：

```ini
[Unit]
Description=Skill Pilot Team Hub
After=network.target

[Service]
Type=simple
User=<运行账号>
WorkingDirectory=/srv/Skill Pilot
ExecStart=/usr/bin/env python3 -m skill_pilot hub serve --data "/srv/Team Hub Data" --host 0.0.0.0 --port 8765 --open
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

保存后启用并查看运行结果。已手动启动的那一份须先按 Ctrl+C 停止，避免占用同一个端口：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now skill-pilot-team
sudo systemctl status skill-pilot-team
```

`enable` 负责开机运行，`Restart=always` 负责运行中退出后重试。之后可用 `sudo systemctl restart skill-pilot-team` 重启、`sudo systemctl stop skill-pilot-team` 停止。开机任务仍指向相同的数据文件夹，成员、原口令和分享都会保留。请在部署机器重启一次，确认服务自动运行，再让同事连接；不同系统的开机配置需在各自机器验收。

## 方式 2：NAS 容器功能

需要 NAS 或电脑已有可用的容器环境。直接使用官方 `python:3.12-slim`，不制作自己的镜像。先创建数据文件夹，再挂载程序和数据两个文件夹：

```bash
mkdir -p "/srv/Team Hub Data"
docker run -d --name skill-pilot-team \
  --restart unless-stopped \
  -p 8765:8765 \
  -v "/srv/Skill Pilot:/app:ro" \
  -v "/srv/Team Hub Data:/data" \
  -w /app -e PYTHONDONTWRITEBYTECODE=1 \
  python:3.12-slim \
  python3 -m skill_pilot hub serve --data /data --host 0.0.0.0 --port 8765 --open
```

也可在 NAS 的容器管理界面填写同样的镜像、挂载、工作目录、端口和命令。程序挂载只读，数据挂载可读写；数据不能只留在容器内部。若改变对外端口，只改 `-p` 左边的数字，同事也使用新的端口。

**启用容器的“自动重启”，并让容器服务随系统开机运行。** 上面的 `--restart unless-stopped` 会在容器异常退出或容器服务重新启动后恢复运行。手动停止的容器会保持停止，需用 `docker start skill-pilot-team` 重新启用。

成员管理同样在这台机器上进行，使用容器内的数据位置：

```bash
docker exec skill-pilot-team python3 -m skill_pilot hub add-member "示例成员一" --data /data
docker exec skill-pilot-team python3 -m skill_pilot hub members --data /data
docker exec skill-pilot-team python3 -m skill_pilot hub remove-member <成员编号> --data /data
```

通过 `docker logs skill-pilot-team` 查看启动结果。部署后执行 `docker restart skill-pilot-team`，确认原成员口令仍能访问、原分享仍能读取，再重启 NAS 验证开机恢复。只要保留数据挂载文件夹，重启或重新创建容器都保留成员和分享。

## 以后收紧

从启动命令里去掉 `--open`，保留原数据文件夹并重启分享站即可。已经加入的人照常使用，新人需要管理员发口令；删人仍用下面的 `remove-member` 命令。

直接运行时先按 Ctrl+C 停止，再用不带 `--open` 的原命令启动。systemd 要从 `ExecStart` 中去掉 `--open`，执行 `sudo systemctl daemon-reload` 后再 `sudo systemctl restart skill-pilot-team`。容器要在停止并移除旧容器后，用相同挂载、端口和不带 `--open` 的命令重新创建；保留数据挂载文件夹，不删除其中的文件。重启已有容器不会改变它原来的启动命令。

## 删成员、备份与更新

直接运行时，在程序文件夹执行：

```bash
python3 -m skill_pilot hub members --data "/srv/Team Hub Data"
python3 -m skill_pilot hub remove-member <成员编号> --data "/srv/Team Hub Data"
```

删除后，这位成员的口令立即失效，分享一并删除；其他成员不受影响。口令遗失时不能找回原口令，请删除该成员后重新添加，交付新口令，并让同事重新上传分享。

数据文件夹包含：

| 文件 | 内容 |
| --- | --- |
| `members.json` | 成员编号、显示名、口令的 SHA-256 摘要、创建时间；没有口令原文 |
| `shares/<成员编号>.json` | 此成员最近一次上传的清单和更新时间 |
| `.lock` | 防止成员管理和同时上传相互覆盖的运行锁；不是账号数据 |

备份时先停止分享站，复制**整个数据文件夹**到受保护的备份位置，再启动。系统服务用 `systemctl stop/start`，容器用 `docker stop/start`，手动运行用 Ctrl+C 和上面的固定命令。备份包含团队名单，应只让管理员访问。恢复时同样先停止，再还原整个文件夹并确认运行账号有读写权限，不要单独拼接不同时间的成员和分享文件。

更新程序时先停止分享站、备份数据，获取包含分享站的新版本并替换程序文件夹内容，保留数据文件夹，之后用原启动命令或开机服务启动。容器方式更新宿主机程序文件夹后重启容器；要更新 Python 镜像时，先拉取官方镜像，再用相同挂载和启动参数重新创建容器。都不需要重新发口令。检查原口令和分享可用后再结束维护。

## 同事怎么用

每位同事用自己电脑上的 Skill Pilot 看板，不用另外安装。

1. **连上分享站**：在看板「设置 → 团队分享」填分享站地址和自己的名字；要口令的分享站，点「管理员给了口令？」填上管理员给的口令，再点「保存并连接」。看到“已连接，你是某某”就好了，页头会多出「团队」。口令只存在自己电脑上，开放加入时看板会自动领取；以后保存或改地址时可以留空，先沿用原来的口令，连接成功不会多出一个自己。
2. **分享自己的技能**：在「团队」页点「分享我的技能…」，选全部在用技能或某个 agent 在用的技能。名单默认全选，取消不想分享的；被别的技能需要的会自动留下。自制或改过的技能，同事只能看到名字和说明，拿不到文件。核对后点「分享」；再分享一次会换成新的名单，「撤回分享」后同事就看不到了。
3. **挑同事的技能**：在「团队」页点某位同事的「看看」，可以搜索、按分组看，每个技能标着在自己电脑上的状态：本机已有、可以装、要另外补齐（原因放在旁边的说明图标里）。默认一个都不勾，勾上想要的，选好“给谁用”，点「导入」先核对，再导入。导入后可以在结果里或操作记录里整批撤销。

分享站连不上时，「团队」页和设置里都会写明原因；自己电脑上的技能不受影响。只读模式下只能看，不能分享或导入。

## 接口与连通检查

只有 `POST /api/join` 不需要口令；没有开放加入时，这条接口返回 `403`。其余接口都带 `Authorization: Bearer <口令>`，没有口令或口令错误返回 `401`。只有完全相同的加入路径和方法可以不带口令，带查询参数也不放行。错误统一返回 `{"error":"中文说明"}`。成功返回 `200`，通过口令检查后，未知地址返回 `404`，不支持的操作返回 `405`。

| 操作 | 结果 |
| --- | --- |
| `POST /api/join` | 仅此接口不需要口令；请求体为 `{"name":"显示名"}`。开放时返回 `{"service":"skill-pilot-team","api":1,"member":{"id":"成员编号","name":"显示名"},"token":"口令"}`；未开放返回 `403`，不创建成员 |
| `GET /api/me` | `{"service":"skill-pilot-team","api":1,"member":{"id":"成员编号","name":"显示名"}}` |
| `GET /api/shares` | `{"shares":[{"member":{"id":"成员编号","name":"显示名"},"updated_at":"时间","skill_count":1,"agents":[{"id":"agent编号","name":"名称"}]}]}`；最新上传在前，无分享的成员不列出 |
| `GET /api/shares/<成员编号>` | `{"member":{"id":"成员编号","name":"显示名"},"updated_at":"时间","config":{...}}`；未分享或已撤回返回 `404` |
| `PUT /api/shares/me` | 请求体为导出的配置文件；成功返回 `{"updated_at":"时间","skill_count":1}`，覆盖自己的上一份 |
| `DELETE /api/shares/me` | 撤回自己的分享，返回 `{}`；重复撤回也成功 |

上传须直接发送 JSON 文件并带上 `Content-Length`，最多 **1,048,576 字节（1 MB）**；超限直接返回 `413`，格式不支持返回 `400`。不接受分块上传；不接收技能文件。验证配置后只保存 `format`、`version`、`created_at`、agent 的 `id/name`，以及技能的 `name/assigned_agents/requires/source/local_modified/title/summary/group/description`；可选说明字段有才保留。来源只保留受支持的来源字段，不保留额外账号信息。`instructions` 和其他多余字段会丢掉。

加入请求也须带上 `Content-Length`，最多 **4096 字节**，不接受分块发送。缺少长度返回 `411`，超过上限返回 `413`，格式或名字不合要求返回 `400`。名字去掉首尾空白后为 1–40 个字，不能换行或含控制字符，允许与其他成员同名。

开放的分享站可以先用下面的方式自助加入。返回内容含口令，用只有自己能读的临时文件接收，再读进变量，避免把它打印到终端或公共日志：

```bash
HUB_ADDRESS="http://<局域网地址>:8765"
HUB_JOIN_FILE="$(mktemp)"
curl --fail-with-body --silent --show-error -H 'Content-Type: application/json' \
  --data '{"name":"示例成员"}' --output "$HUB_JOIN_FILE" "$HUB_ADDRESS/api/join"
HUB_TOKEN="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["token"])' "$HUB_JOIN_FILE")"
rm -f "$HUB_JOIN_FILE"
curl --fail-with-body -H "Authorization: Bearer $HUB_TOKEN" "$HUB_ADDRESS/api/me"
unset HUB_TOKEN HUB_JOIN_FILE
```

管理员可以用下面的方式检查接口。在终端交互输入口令，它不会显示在屏幕上；以下示例使用 bash，`HUB_ADDRESS` 换成自己的局域网地址：

```bash
HUB_ADDRESS="http://<局域网地址>:8765"
read -r -s -p '成员口令：' HUB_TOKEN
curl --fail-with-body -H "Authorization: Bearer $HUB_TOKEN" "$HUB_ADDRESS/api/me"
curl --fail-with-body -H "Authorization: Bearer $HUB_TOKEN" "$HUB_ADDRESS/api/shares"
curl --fail-with-body -X PUT -H "Authorization: Bearer $HUB_TOKEN" \
  -H 'Content-Type: application/json' --data-binary @skill-pilot-config.json \
  "$HUB_ADDRESS/api/shares/me"
curl --fail-with-body -H "Authorization: Bearer $HUB_TOKEN" "$HUB_ADDRESS/api/shares/<成员编号>"
curl --fail-with-body -X DELETE -H "Authorization: Bearer $HUB_TOKEN" "$HUB_ADDRESS/api/shares/me"
unset HUB_TOKEN
```

示例中的 `skill-pilot-config.json` 是从自己电脑导出的技能配置。分享站只验证、保存名单，不下载或安装任何技能，不执行配置里的说明。服务日志不记录口令、加入的名字、访问路径或名单内容；请勿把添加成员时显示或自助加入时领到的口令保存到公共日志，也不要在打印命令参数的调试模式中运行上述检查。
