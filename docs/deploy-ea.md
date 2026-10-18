# EA · Emby API —— 协议网关部署

**EA 是客户端眼里的"那台 Emby 服务器"**：Infuse / Fileball / Forward+ / Hills / SenPlayer / 官方 App 直接连 EA，媒体流一律 302 直链（播放器自拉），服务器不传字节（转码/中继已于 2026-10-17 删除）。

EA 自己**不定义任何业务策略**——用户、媒体库、套餐与订阅、设备上限、下载开关、限流口径全部由 EM 写入共享数据库，EA 只按要求兑现。所以：

> **EA 不能脱离 EM 单独运行。** 缺 `SECRET_KEY`、或共享库里还没有 EM 建的表时，EA 启动即被拒绝（详见第 3 节）。

面板（注册/登录/套餐/充值/邀请/后台）在 [EM 部署](./deploy-em.md) 那篇。

## 推荐拓扑：四个职责分开

上量之后不要把所有东西塞进一台机器。把职责拆成四份，各自可以独立升级、独立排错：

```
                    ┌── 管理员 / 用户（浏览器）
                    │
panel.example.com ──┤  EM 控制面（注册登录、订阅权限、求片调度、媒体库管理、扫描、审计）
                    ▼
              EM 控制面进程（ENABLE_EMBY_GATEWAY=false）
                    │
        私网 API / WireGuard / Tailscale
                    │
                    ▼
              EA 播放节点（Emby 协议、认证、权限校验、302 直链、播放会话）
                    │
          共享存储（WebDAV / rclone / 115 / AList）
                    ▼
              统一媒体存储（EM 与 EA 读的是同一份）
```

| 职责 | 跑在哪 | 关键配置 |
| --- | --- | --- |
| EM 控制面 | 当前服务器（`serve.py`） | `ENABLE_EMBY_GATEWAY=false`：面板只做控制，不提供协议面、不转码 |
| EA 数据面 | 另一台服务器（`serve_emby.py`） | `NODE_KEY` 认领节点、`EMBY_PUBLIC_URL` 是**客户端唯一该连的地址** |
| 数据库 | 独立一处（推荐托管 PostgreSQL） | 不开放公网，只允许 EM / EA 的私网来源 |
| 媒体存储 | 独立存储服务（WebDAV / rclone RC / 群晖 / 115） | EM 与 EA 用**同一个地址**读写，不依赖任何一方的本机路径 |

公网只开两个域名：`panel.example.com` → EM，`emby.example.com` → EA。
数据库、节点身份接口、挂载体检接口一律走私网，不要直接暴露公网。

### 数据库：单独一处，按角色分账号

- PostgreSQL **不要**与 EM 或 EA 挤在一台机器上；退而求其次时，至少只允许私网访问；
- 集群/托管实例打开自动备份，并启用 WAL 归档或 PITR——媒体库可以重扫，用户与订阅丢了不可重扫；
- 给 EM 与 EA **两个不同的数据库账号**，同一个库。EA 账号只需读写业务表，不能改用户与订阅；
- ⚠️ **当前代码还做不到「EA 完全只读」**：EA 启动时校验共享库里的 EM 表（见第 3 节），扫描结果、播放会话、
  会话记录都由 EA 写入，ORM 也按同一个库读写。所以现在请把 EA 账号限制为「必要读写」，
  「EA 只读」要等后续把节点侧写入收敛成专用表/账号再做。

### 媒体存储：不要让两台机器各自猜路径

最容易被忽略、也最容易出故障的是这一项。**内容必须由服务化的存储提供**，而不是两台机器各自
的本机路径：

- ✅ 推荐：存储服务器提供只读 **WebDAV** 或 **rclone RC**；MoviePilot 继续用它自己的下载目录，
  入库后由统一存储对外提供内容；EM 用它扫描，EA 用同一个地址读取与播放；
- ❌ 不要依赖 `/media/movies`、`/mnt/downloads` 这类**只在某一台机器存在**的路径；
- 下载缓存留在下载机（MoviePilot）本地盘，入完库就可以不再依赖它。

这样才不会出现「**面板扫描正常、播放节点找不到媒体**」：面板把条目库的 `file_path` 写成自己的本机路径，
而 EA 那台机器上根本没有这个文件时，客户端点播放只会 404/502。

> 后台的「媒体库」页现在会逐条给出**播放可达性**判定（ok / 待确认 / 读不到），
> 以及「用户端地址」一致性检查，配套接口 `GET /api/admin/emby/reachability`：
> 分离部署的每次改动都应该先看这一页，而不是等客户端来报错。

### 网络：内部全部走私网

```
EM ── WireGuard / Tailscale ── EA
 │                              │
 └──── 私网 ─── PostgreSQL ─────┘
                                │
                          WebDAV / rclone
```

- EM → EA 的管理调用（转发扫描、挂载体检）走私网地址；面板里填的 EA 地址可以是私网域名；
- 节点身份（`NODE_KEY`）与体检接口都靠共享的 `SECRET_KEY` 鉴权，但**不要**把 EA 的管理端口直接暴露公网；
- 公网只留上面两个域名（TLS 证书按域名签）。

### 下一步优化（同样属于职责分开）

- EA 的数据库账号只给必要权限（见上面的提醒）；
- EM → EA 的调用全部走私网，公网只用于客户端；
- EA 只通过节点 API 获取必要配置（现在仍然读同一份库，是过渡期做法）；
- 转码目录放 EA 本地 SSD（`EMBY_TRANSCODE_DIR`），不要放网络盘：HLS 切片是高频小块写；
- 给 EA 做独立监控与限流（ffmpeg 并发、磁盘水位、出口带宽）。

分阶段落地：**现在**就是上面这张拓扑（EM 控制面 + EA 数据面 + 独立 PostgreSQL + 共享 WebDAV/rclone
+ 内网 WireGuard/Tailscale）；**下一步**把 EA 的库权限与调用链路收窄；**长期**才是控制面 / 数据面集群 /
消息队列与配置同步那一套。不推荐现在为了两台机器就去做「EM 把全部业务数据同步给 EA」的双写方案：
那需要同步协议、断网重试、版本冲突、策略变更延迟与事件队列，改动大而且会把现有的稳定链路拆开。

## 1. 为什么 EA 必须独占一个地址

Emby 客户端把「服务器地址」当成服务器根来用，因此 `emby_router` 同时声明了**两套路径**：

- `/emby/*`（95 条，如 `/emby/Users/AuthenticateByName`）
- **裸根路径**（87 条，如 `/System/Info`、`/Users/AuthenticateByName`、`/Items/Counts`）

既然裸根路径要占根，EA 就不能和 EM 的面板（门户 SPA 兜底在 `/`）共用一个根。所以：

| 角色 | 对外地址 | 谁连它 |
| --- | --- | --- |
| EM 面板 | `https://panel.example.com` | 浏览器（用户 / 管理员） |
| EA 网关 | `https://emby.example.com`（或同机 `:8001`） | **播放器客户端**，以及经 EM 反代的网页播放器 |

网页播放器走 EM 域名的同源 `/emby/*`，由 Nginx 反代到 EA（见 [EM 部署第 7 节](./deploy-em.md)），所以浏览器侧不需要跨域配置。

## 2. 环境要求

| 组件 | 说明 |
| --- | --- |
| Python | 3.10+，与 EM **同一份代码与依赖**（`pip install -r backend/requirements.txt`） |
| **ffmpeg** | **EA 这边必需**（转码发生在 EA）；直连播放不需要 |
| 磁盘 | `EMBY_TRANSCODE_DIR` 需要放得下 HLS 切片；并发多时要留余量 |
| 媒体目录 | 必须是 **EA 这台机器能看到**的路径（读文件、探测轨道、转码都在 EA 做） |
| 网络 | 能访问与 EM 相同的数据库；若跨机部署，用 PostgreSQL（见第 3 节） |

## 3. 前置：与 EM 配对（EA 的硬依赖）

EA 与 EM 必须共用**同一个数据库**与**同一个 `SECRET_KEY`**（客户端 token 由 EM 签发、由 EA 校验）。建议直接用同一份 `.env`：

| 变量 | 要求 |
| --- | --- |
| `SECRET_KEY` | **必须与 EM 完全相同**，否则客户端 token 全部校验失败 |
| `DATABASE_URL` / `DATABASE_TYPE` | **必须与 EM 指向同一个库** |
| `EMBY_PUBLIC_URL` | EA 的对外地址（客户端账号卡/一键导入用它） |
| `EMBY_SERVER_NAME` / `EMBY_SERVER_ID` | 与 EM 保持一致（客户端按 ID 识别服务器） |
| `EMBY_API_PORT` | EA 监听端口，默认 `8001` |
| `EM_PANEL_URL` | 可选，EA 启动时探测 EM 是否可达（仅告警） |
| `EMBY_TRANSCODE_DIR` / `EMBY_FFMPEG_PATH` | EA 这边的转码目录与 ffmpeg 路径 |
| `CORS_ORIGINS` | 与 EM 同口径 |

### 数据库能不能跨机（重要）

| 场景 | 是否可行 |
| --- | --- |
| SQLite，EM 与 EA **在同一台机器**（同一份 `aetrix_unified.db`） | ✅ 可行（WAL 下多进程读写正常） |
| SQLite，EM 与 EA 在**不同机器** | ❌ 不可行——SQLite 是单机文件，无法通过网络安全共享 |
| PostgreSQL，任意部署位置 | ✅ 推荐 |

跨机分离部署请把两边都指向同一个 PostgreSQL。

### 启动时的配对校验

EA 在启动阶段（lifespan）做两件硬校验，任一不满足就**拒绝启动**并给出可执行的提示：

1. `SECRET_KEY` 是否已设置；
2. 共享库里是否存在 EM 初始化后才会有的表（`web_users`、`emby_libraries`、`emby_api_tokens`）。

这样避免出现"服务起得来、但谁都连不上/播不了"的假健康状态。第三件事是**软校验**：若配置了 `EM_PANEL_URL`，EA 会探测 EM 的 `/api/health`，不可达时只打警告——因为 EM 短暂重启不应该掐断正在播放的会话。

## 4. 启动 EA

```bash
cd /opt/Aetrix-Portal
source .venv/bin/activate

python serve_emby.py                      # 默认 0.0.0.0:8001
EMBY_API_PORT=9001 python serve_emby.py   # 指定端口
```

### systemd 常驻

`/etc/systemd/system/aetrix-ea.service`：

```ini
[Unit]
Description=Aetrix Portal EA (Emby API gateway)
After=network.target

[Service]
Type=simple
User=aetrix
WorkingDirectory=/opt/Aetrix-Portal
EnvironmentFile=/opt/Aetrix-Portal/.env
ExecStart=/opt/Aetrix-Portal/.venv/bin/python serve_emby.py
Restart=always
RestartSec=5
# 转码进程组需要能被整体回收
KillMode=control-group
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now aetrix-ea
journalctl -u aetrix-ea -f
```

> **不要**用 `uvicorn --workers N` 起多个 worker：转码子进程管理与内存态会话在同一进程内闭环，多 worker 会重复 fork、会话失效无法回收。

## 5. Nginx + HTTPS（客户端域名）

```nginx
server {
    listen 80;
    server_name emby.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name emby.example.com;

    ssl_certificate     /etc/nginx/ssl/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/privkey.pem;

    add_header X-Content-Type-Options nosniff always;
    # 直连流与切片要能带 Range，且响应头要给客户端读
    add_header Accept-Ranges bytes always;

    # 转码首片可能等十几秒；默认 60s 读超时会提前断流
    proxy_read_timeout 300s;
    proxy_send_timeout 300s;
    proxy_buffering off;          # 边转码边播放，禁止缓冲

    location / {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
    }
}
```

> 证书必须被客户端信任：自签证书在部分播放器上会被直接拒绝，表现为"服务器无法连接"。建议用 Let's Encrypt 等受信证书。

## 6. 客户端接入

1. 客户端「服务器地址」填 `EMBY_PUBLIC_URL` 指向的地址（如 `https://emby.example.com`），**不要带 `/emby` 后缀**——客户端自己会拼协议路径。
2. 用户名密码用**门户账号**：
   - 用户首次打开门户账号卡时会自动生成 `emby_username`；
   - 也可主动设置：`POST /api/user/emby/password`；
   - 门户改密会**自动同步**为 Emby 播放密码，两边永远一致。
3. 若中途改过 `EMBY_SERVER_ID`，客户端会把同一台服务器当成新服务器，需要重新添加。

协议面已覆盖 40+ 端点（节选）：`/Users/AuthenticateByName`、`/Users/{uid}/Items`、`/Items/{id}/PlaybackInfo`（GET/POST）、`/Videos/{id}/stream`（Range 直连）、`/videos/{id}/master.m3u8`（HLS 转码，切片自带 `api_key`）、字幕投递 `/Videos/{id}/{mid}/Subtitles/{i}/Stream.{fmt}`、`/Search/Hints`、`/Items/{id}/Images/{Type}/{Index}`、`/Sessions/Playing/*` 进度上报、`/System/Info`、`/Branding/Configuration` 等。

## 7. 权限、限流与设备控制

这些能力由 EM 配置策略、由 EA 在客户端侧**强制执行**：

| 能力 | 行为 |
| --- | --- |
| 付费墙 | 非会员的 `PlaybackInfo`、直连流、HLS 会话、下载全部被拦（403 + 文案），管理员始终放行 |
| 下载门禁 | `allow_download=false` 时，`/Download` 与 `/Items/{id}/File` 等等价路径在**网关层**统一兜底，新增下载路由也不会漏 |
| 每用户设备上限 | `device_limit_per_user`：客户端 `AuthenticateByName` 登录即登记设备（名称/客户端/版本/IP/首末次出现），超限按策略拒绝或自动踢最久未使用 |
| 远程踢设备 / 撤销 token | 管理端封禁设备会**同时吊销令牌**（该设备立即不可用）；移除设备即踢下线 |
| 用户自助 | 用户端「我的设备」可查看与自助清理自己的设备 |
| 认证限流 | Emby 协议认证 10 次/分/IP，另有卡码预检 20 次/分、核销 10 次/分等 |

> 被封禁设备的登录会在设备登记阶段直接被拒，无法通过换客户端绕过。

## 8. 验证

```bash
EA=https://emby.example.com

curl -s $EA/emby/system/info/public     # 服务器名 / 版本
curl -s $EA/System/Info/Public          # 裸根路径也要通（客户端按根地址连接）
curl -s $EA/api/health                  # service=ea 且 paired_with_em=true
```

`/api/health` 会如实上报配对状态；`paired_with_em=false` 并列出 `missing_em_tables` 时，说明 EA 指向的库不是 EM 那套，客户端将无法认证。

EA 不提供交互式 API 文档（`/docs` 已关闭），也不托管任何页面——它只服务协议面。想确认面板是否正常，去 EM 的 `/api/health`。

## 9. 排错

| 现象 | 原因与处理 |
| --- | --- |
| 启动直接退出，日志说 `SECRET_KEY 未设置` | 与 EM 用同一份 `.env`（值必须一致） |
| 启动退出，日志列出 `缺少 EM 的表` | 指向的库不对，或 EM 还没初始化过；先跑 EM（`python serve.py`）并至少建一个媒体库 |
| 客户端提示"服务器无法连接" | 证书不受信任（换受信证书）、端口未放行、或地址带了 `/emby` 后缀 |
| 认证 401 | 用了旧账号/旧密码；确认门户已生成 `emby_username`，且两边 `SECRET_KEY` 一致 |
| 播放 403 | 付费墙生效（非会员）或站点关闭了下载；管理员不受限 |
| 转码 503 | EA 这台机器没装 ffmpeg，或 `EMBY_FFMPEG_PATH` 指错；直连播放不受影响 |
| 首片久等 / 中途断流 | Nginx 默认 60s 读超时所致：`proxy_read_timeout 300s` + `proxy_buffering off` |
| 切片 404 | 客户端请求早于 ffmpeg 写出（EA 会短暂等待）；ffmpeg 已退出时返回 503，看 `journalctl -u aetrix-ea` 里的 ffmpeg 报错（常见：源文件不可读、磁盘满） |
| 网页播放器没声音/黑屏但客户端正常 | 检查 EM 域名的 Nginx 是否配了 `/emby/` 反代到 EA（网页走同源 `/emby/*`） |
| 客户端误连了面板域名 | 分离模式下 EM 会返回明确的 404 指引（含 EA 地址）；按提示把客户端指向 EA |
| **面板扫描正常，客户端点播放 404/502** | 典型是「内容只在面板那台机器上」：库用的是本机目录 / `local` 挂载，而 EA 读不到。看「媒体库」页的**播放可达性**（或 `GET /api/admin/emby/reachability`）：`bad` 说明 EA 体检已证实读不到，`待确认` 说明还没在 EA 上体检过；改用共享 WebDAV/rclone 是最稳定的改法 |
| 用户端账号卡显示的地址不对 | 「媒体库」页播放可达性里的「用户端地址」一栏会直接报出来（面板已关协议面却仍让用户连面板 = `bad`；**真的把 `localhost` 填进配置** = `待确认`）。如果那一栏写的是「按当前访问地址」，说明还没配过任何地址——面板按管理员此刻访问用的地址下发，上正式域名 / 反代后请在「服务器」页或 `EMBY_PUBLIC_URL` 里固定下来 |

## 10. 多台 EA 与多个「服」（多机多服）

一个后端服可以**部署到多台服务器**，每台机器各跑一个 EA、同时对外出流：每台机器只能碰到
自己那台上的存储（本机目录、rclone、网盘凭据所在的位置），所以「内容归谁」必须显式声明。

面板侧（EM）的模型是：

- **服（realm）**＝一套可以独立运营的播放服务：自己的媒体库、存储挂载、套餐、订阅、卡码、求片；
- **播放节点**＝「服务器」页里 `kind=ea` 的那几条记录，每条属于一个服；
- 客户端连哪台 EA，就看到哪台的库；**甲服的会员不能在乙服的 EA 上播放**。

### EA 侧要配的两个变量

| 变量 | 作用 | 不配会怎样 |
| --- | --- | --- |
| `NODE_KEY` | 认领面板「服务器」里的一条 EA 记录（该条记录的 `node_key`） | 不做节点过滤：本机能看到所有媒体库，面板也无法把「这台机器专属的库」交给它 |
| `REALM` | 声明本机属于哪个服（填服的 slug 或 id） | 按面板里这台服务器记录的归属服判断；都为空则不做服过滤（单服部署的行为） |

启动时 EA 会打印自己的身份（节点 / 服 / 过滤是否生效），`GET /api/health` 的 `node` 字段也会如实上报：

```bash
curl -s $EA/api/health | python3 -m json.tool | grep -A 8 '"node"'
```

`NODE_KEY` 在面板里找不到对应记录时，EA 会**自动登记**一条（名字形如「EA 节点 <key>」），
你可以在「服务器」页把它改名、指定归属服；也可以在面板里先加好服务器（填 `node_key`）再启动 EA。

### 内容怎么分给某台机器

媒体库的「归属节点」在面板里设置（「媒体库」页 → 归属节点）：

| `node_id` | 含义 |
| --- | --- |
| 空（默认） | **未分配**：所有节点都看得见，由面板（EM）扫描（单节点部署、刚上线还没分完时靠它保持行为不变） |
| 某台节点 | 只有那台节点向客户端展示它、只有它会扫描它（因为只有它碰得到那些文件） |

在面板点「扫描」时，已分配给自己节点的库会被**转发到那台 EA** 执行（走节点签名，密钥为
`NODE_SHARED_SECRET` 或由两端共享的 `SECRET_KEY` 派生；`SECRET_KEY` 本身不再出现在请求里）。所以在「服务器」页配好的地址必须是从 EM 这台机器**能访问到**的 EA 地址。

### 用「服管理」页切换当前服

面板顶部有「当前服」切换器，切开之后订阅、套餐、媒体库、存储挂载、服务器都只看那个服的数据。
「服管理」页可以新建 / 改名 / 停用 / 删除服（删之前必须把数据移交给另一个服，默认服不能删）。
每个服的 Emby 入口（后端服 EA / 已有 Emby，在后台「服务器 · Emby 总览」页里添加）与对外地址都是**一个服一个**的，用户端账号卡会按服列出地址与会员。

### 校验清单

1. 「服管理」页点某服的「节点体检」：应看到每台节点可达、`NODE_KEY` 已认领、`REALM` 与面板一致；
2. 节点自称的服与面板记录不一致时，页面上会直接报警——那是 `REALM` 填错了（典型表现是客户端什么都看不到）；
3. 客户端连 A 服地址只能看到 A 服的库，连 B 服地址只能看到 B 服的库；
4. 「媒体库」页顶部不应出现**读不到**：每个库都是 `ok`；显示 `待确认` 时先点「存储来源」页的
   「拉取 EA 体检」，把没法确认的挂载变成有证据的 `ok` / `读不到`。

## 11. 不想分离？用单进程模式

如果只需要一台机器：把 `ENABLE_EMBY_GATEWAY` 保持 `true`（默认），**不要部署 EA**，EM（`serve.py`）会自己一并提供 `/emby/*` 与裸根协议面。此时客户端地址就是面板地址。

代价是面板与协议面同生共死：一次面板发布重启会中断正在播放的会话，转码也挤在同一进程里。站点上量后建议再切到分离部署。

## 下一步

- 上线检查、备份恢复、更多排错：[运维 · 备份 · 排错](./operations.md)
