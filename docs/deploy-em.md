# EM · Emby Manager —— 面板部署

**EM 负责所有「卖会员」相关的事**：用户注册 / 登录 / 套餐订阅 / 续费 / 充值 / 邀请返利 / 卡码核销 / 签到 / 工单 / 公告 / 设备与登录风控 / 管理后台。它是整套系统的**唯一事实来源**——用户、媒体库、策略、开关都由 EM 写入共享数据库，EA（Emby API 网关）只是把这些"推下来"的结果对客户端兑现。

EM 部署到**你自己的域名**（面板域名），客户端不要连这个域名。

> 第一次部署？建议顺序：先按本篇把 EM 跑起来并建好媒体库，再按 [EA 部署](./deploy-ea.md) 把客户端接上。

## 1. 部署形态：先决定单进程还是分离

| 形态 | 进程 | 适用 |
| --- | --- | --- |
| **分离部署（推荐生产）** | EM（`serve.py`）+ EA（`serve_emby.py`） | 面板与协议面分开、各自独立域名/端口、可分别重启与扩容，客户端直连 EA |
| 单进程（兼容旧部署） | 只跑 EM（`serve.py`） | 单机小站，EM 自己一并提供 `/emby/*` 协议面 |

EM 是否内置 Emby 协议面由 `ENABLE_EMBY_GATEWAY` 控制（**默认 `true`**，即保持原有行为）：

```env
ENABLE_EMBY_GATEWAY=true    # 单进程模式：EM 自己提供 /emby/*
ENABLE_EMBY_GATEWAY=false   # 分离模式：协议面交给 EA，EM 只跑面板
```

置为 `false` 后，若有客户端误连 EM，EM 会返回明确的 404 指引（告诉它该去哪个 EA 地址），而不是把页面 HTML 当成响应发出去。

## 2. 环境要求

| 组件 | 版本 | 说明 |
| --- | --- | --- |
| Python | 3.10+（容器镜像为 3.11-slim） | 必需 |
| Node.js | 20.19+ 或 22.12+ | 仅构建前端时需要（见 `user_frontend/package.json` 的 `engines`） |
| PostgreSQL | 13+ | **默认数据库**（v2.42.0 起）。必须能被 EM 连上，连不上启动即退出 |
| SQLite | 内置 | 可选：设 `DATABASE_TYPE=sqlite` 后使用，单机 / 单进程够用 |
| Redis | 5+ | 可选：多实例、或希望重启不丢风控状态时启用 |
| ffmpeg | 可选 | **分离部署下转码在 EA 那台机器上，EM 不需要** |

```bash
# Debian / Ubuntu
sudo apt update && sudo apt install -y python3 python3-venv python3-pip
```

## 3. 拉取代码并安装依赖

```bash
git clone https://github.com/xzb177/Aetrix-Portal.git
cd Aetrix-Portal

python3 -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
```

## 4. 构建两个前端

EM 负责托管门户与管理后台的静态产物。产物路径是**固定约定**：

| 前端 | 构建命令 | 输出 | Vite `base` | EM 挂载到 |
| --- | --- | --- | --- | --- |
| 用户门户 | `npm run build-only` | `user_frontend/dist` | `/` | `/` |
| 管理后台 | `npm run build` | `admin_frontend/dist` | `/admin/` | `/admin` |

```bash
cd user_frontend  && npm ci && npm run build-only && cd ..
cd admin_frontend && npm ci && npm run build      && cd ..
```

想要构建时同时过类型检查，用户端可用 `npm run build:strict`。

> 管理后台的 `base` 必须是 `/admin/`；改错会导致后台白屏（页面里的资源全 404）。EM 启动时若发现 `admin_frontend/dist` 不存在，会在日志里提示「管理后台构建产物不存在，/admin 不可用」。

门户与管理后台的 API 请求都走**相对路径**（`/api/user/*`、`/api/admin`），因此**必须与 EM 同源**：一个域名，Nginx 分流。不要把面板放 A 域、API 放 B 域。

## 5. 配置 `.env`

```bash
cp env.example .env
```

`env.example` 里逐项都有注释。真正需要你决定的是这些：

| 变量 | 必需 | 说明 |
| --- | --- | --- |
| `SECRET_KEY` | **生产必填** | JWT 签名密钥。不设置会用临时随机密钥，**每次重启所有人登录态失效**；分离部署时 EA 必须用**同一个值**。生成：`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`
| `PORT` / `HOST` | 建议 | 面板端口，默认 `8000` / `0.0.0.0` |
| `EMBY_PUBLIC_URL` | **必填** | 用户账号卡与「一键导入」展示的地址。分离部署时填 **EA 的地址**。留空时会按**管理员当前访问面板的地址**下发（面板与接口同源，测试机不配也能直接用）；上正式域名 / 反代后请固定下来 |
| `EMBY_API_PUBLIC_URL` | 分离模式建议 | 与 `EMBY_PUBLIC_URL` 同为客户端地址，用于 EM 的"请去连 EA"指引 |
| `EM_PANEL_URL` | 可选 | EM 自己的面板地址；EA 启动时探测它是否可达 |
| `CORS_ORIGINS` | **生产必填** | 逗号分隔的具体域名。留空等于允许所有源 |
| `EMBY_ALLOW_LEGACY_TOKENS` | **已移除** | 旧版「纯数字即 user_id」的兼容 token 已彻底删除，设置该变量不再有任何效果 |
| `DATABASE_TYPE` / `DATABASE_URL` | **生产必填** | v2.42.0 起**默认 PostgreSQL**：`DATABASE_TYPE` 的代码默认值就是 `postgresql`，而 `env.example` 里给的是 Compose 内部的服务名 `postgres`。**裸机部署必须把它改成自己那台能连上的 PG**，否则 `python serve.py` 直接以 `psycopg2.OperationalError: connection to server at "localhost", port 5432 failed` 退出。想继续用 SQLite：`DATABASE_TYPE=sqlite` + `DATABASE_URL=sqlite:///./aetrix_unified.db` |
| `REDIS_ENABLED` / `REDIS_URL` | 可选 | 未启用时用内存缓存 |
| `TMDB_API_KEY` | 可选 | 中文元数据刮削（海报/简介/评分）；不填只做本地 ffprobe 探测 |
| `ENABLE_EMBY_GATEWAY` | 分离模式必填 | 详见第 1 节 |

> 支付网关、签到奖励、邀请返利等经济项**可以留空**，之后在管理后台「系统设置 → 经济设置」在线配置，后台值优先。

## 6. 在管理后台选择 Emby 服务

登录管理后台后打开「**多服运营 → 服务器 · Emby 总览**」（这是 Emby 相关功能的唯一入口页，
旧页「Emby 服务入口」已下线，访问 `/admin/emby-servers` 会自动跳到这里），点右上角「添加服务器」，
在类型里有两类完全不同的选择：

- **后端服（EA）**：填 `serve_emby.py` 部署后的公网地址。EA 必须和 EM 使用同一个 `DATABASE_URL` 与 `SECRET_KEY`，先在 EA 服务器启动成功，再点击「测试连接」（或直接保存，保存即体检）和「设为当前」。测试不通过时，媒体库、扫描、挂载、会话和播放相关的自建功能会被明确拦截，不会出现“页面能点但实际不能播”的假状态。
- **已有 Emby 服**：填已经运行的 Emby 地址，可选填管理员 API Key 做连通性测试。这个模式只保存连接信息，不会把本项目的会员、用户、媒体库扫描和自建网关强行写进第三方 Emby；本项目的自建媒体库功能会停用。

EA / Emby **一个服一个**：添加时选好归属服，多服部署时每个服各自填自己的地址（顶栏「当前服」决定其他页面的作用域）。
同一时间只有一台是「当前使用」，它决定网关闸门、客户端指引与用户端账号卡看到的地址。

保存 / 设为当前（EA）时，面板会顺手向 EA 拉一次**EA 视角的挂载体检**：`local` / `strm` 的路径、rclone 的 RC 地址这类配置是「跟着服务器走」的，后台（EM）测试通过不代表那台 EA 能播。结论直接显示在同一页的「挂载体检」列（哪些挂载不可达、共几条），逐条明细在「存储挂载」页的「EM / EA 可达」列（详见 `docs/operations.md`）。

页头的「**一键体检**」会真去问每台已启用的 EA「你是谁、属于哪个服、负责哪些库」（8 秒超时），
并重拉当前出流 EA 的挂载体检；两边不一致（EA 的 `REALM` 与面板登记的归属服不同、`NODE_KEY` 对不上）
会直接列成告警，不用自己比对。单服务单进程部署不需要 EA，保持默认的「面板自己出流」即可。

如果你只是把 EM 和 EA 放在同一台机器上，也仍然可以选择第一项，地址填写 EA 的端口（默认 `http://127.0.0.1:8001`，公网使用域名）。如果不填分离地址，则继续使用默认的**单进程模式**，由 EM 自己提供自建 Emby，不需要额外部署 EA。

## 7. 启动 EM

```bash
python serve.py              # 默认 0.0.0.0:8000
PORT=9000 python serve.py    # 指定端口
```

### systemd 常驻

`/etc/systemd/system/aetrix-em.service`：

```ini
[Unit]
Description=Aetrix Portal EM (panel)
After=network.target

[Service]
Type=simple
User=aetrix
WorkingDirectory=/opt/Aetrix-Portal
EnvironmentFile=/opt/Aetrix-Portal/.env
ExecStart=/opt/Aetrix-Portal/.venv/bin/python serve.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now aetrix-em
journalctl -u aetrix-em -f
```

> 单进程模式（EM 自带协议面）下建议保持单进程，以免风控计数分散。

## 7. Nginx + HTTPS（按域名授权）

EM 建议放在**面板域名**上，例如 `panel.example.com`。下面是站点级最小配置（仓库里那份面向旧拆分栈的 `nginx.conf` 已随旧栈一起删除，请按下面的模板写自己的）：

```nginx
server {
    listen 80;
    server_name panel.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name panel.example.com;

    ssl_certificate     /etc/nginx/ssl/fullchain.pem;
    ssl_certificate_key /etc/nginx/ssl/privkey.pem;

    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;

    client_max_body_size 20m;

    # 分离部署：网页播放器走同源 /emby/*，反代给 EA，避免跨域
    location /emby/ {
        proxy_pass http://127.0.0.1:8001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 300s;
        proxy_buffering off;          # 直链 302 无体，播放器自拉
    }

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";   # WebSocket 通知（站内消息/工单推送）
    }
}
```

单进程模式（`ENABLE_EMBY_GATEWAY=true`）不需要 `/emby/` 那段，全部转给 `127.0.0.1:8000` 即可。

> ⚠️ 仓库里曾经提交过一份真实域名（`login.laodaemby.xyz`）的证书与 **EC 私钥**（`nginx/ssl/`），已随旧栈删除。如果你用过它，**视为私钥已泄露：立即吊销并重签**。

## 8. 首次登录与管理

1. **先造管理员**：新库里没有任何账号，而且**第一个注册的用户不会被自动提升为管理员**（有意的安全设计）。用仓库自带脚本一步到位：

   ```bash
   python3 scripts/create_admin.py                          # 创建/升级 admin，随机强密码并打印
   python3 scripts/create_admin.py -u boss -p '你的强密码'    # 指定账号与密码
   python3 scripts/create_admin.py -u boss --no-password      # 只把已有账号升级为管理员，不改密码
   ```

   它写的是当前 `DATABASE_URL` 指向的那个库（`web_users.is_staff = True`），并补齐 Emby 客户端凭据（门户密码即播放密码）；幂等，可重复执行。
   不带 `-p` 时，账号已存在会**重置为新的随机密码**并打印（忘了密码时正好用它），只升级权限请加 `--no-password`。

   > 旧拆分栈用的 `scripts/reset_admin_password.py` 已随旧栈删除；在 EM / EA 这套架构下请一律使用上面的 `scripts/create_admin.py`。
2. 门户：`https://panel.example.com/`
3. 管理后台：`https://panel.example.com/admin/`（同一套账号，非 `is_staff` 会被拒）

> 后台**不会**把第一个注册用户自动变成管理员，这是有意的安全设计。

**免登（单点登录）**：门户与后台同源、共用同一套 JWT，管理员在门户登录后，后台会用该会话静默验证一次并直接进入，**不需要再输一遍账号密码**（门户「个人中心 → 安全设置」里有「管理后台」入口）。门户 token 过期时会自动用 refresh token 换新；门户未登录或账号不是管理员时，后台才显示登录页并说明原因。

## 9. 媒体库创建与扫描

媒体库属于 EM 的管理面（写入的就是 EA 要服务的那些数据）：

```bash
TOKEN=<管理员 access_token>

curl -X POST https://panel.example.com/api/admin/emby/libraries \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"name":"电影","collection_type":"movies","paths":["/data/movies"]}'

curl -X POST https://panel.example.com/api/admin/emby/libraries/1/scan \
  -H "Authorization: Bearer $TOKEN"
```

扫描会递归识别「电影 / 剧集 / 季 / 集」（支持 `S01E02`、`1x02`、`第N集`、`EP05`，以及只有季号的 `S09` / `Season 9` / `第九季`），ffprobe 提取编码、分辨率、音轨与字幕轨，并检测外挂字幕。

> 分离部署时注意：媒体文件的路径必须是 **EA 那台机器/容器能看到**的路径（EA 才负责读文件与转码）。

### 刮削策略

每个媒体库可单独选择刮削策略（后台媒体库卡片直接选，或 `PUT /api/admin/emby/libraries/{id}` 传 `scrape_policy`）：

| 值 | 含义 |
| --- | --- |
| `missing_only` | 仅缺失时刮削（**默认**）：已有 TMDB 命中就不再发请求，省配额 |
| `3m` / `6m` / `1y` | 到期重刮；缺元数据的条目不受窗口限制，总会补 |
| `all` | 每次扫描全量重刮（仅在通网、配额充足时用） |

多密钥轮询：`TMDB_API_KEYS=key1,key2,key3` 逗号分隔，某个密钥叫到配额上限时自动轮到下一个。

扫描任务使用**固定配置快照**：运行中修改路径/策略不会把本次任务改成「一半旧一半新」；保存后重新触发即用新路径（旧路径不会被继续扫描）。同一媒体库同时只允许一个扫描任务，重复触发返回 `409`。

### 按发行平台生成虚拟媒体库

识别片名/目录里的发行组标签（`NF` / `DSNP` / `ATVP` / `AMZN` / `HMAX` / `HULU` / `PMTP` / `PCOK` / `CR` …）后，可为 Netflix / Disney+ / Apple TV+ / Prime Video / Max / Hulu / Paramount+ / Peacock / Crunchyroll 生成虚拟媒体库：

```bash
curl -X POST https://panel.example.com/api/admin/emby/libraries/virtual \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"enabled": true}'            # 不传 platforms = 按库里实际出现过的标签生成
```

虚拟库没有自己的目录，是**跨库的平台视图**（条目仍归属原媒体库）；只在总开关与单个库都开启时才出现在客户端。关闭方式：

- 整个实例关闭：`ENABLE_VIRTUAL_LIBRARIES=false`（每台 EA 可独立配置）
- 单个库关闭：后台停用该库（或 `PUT /libraries/{id}` 传 `is_enabled: false`）

两种情况下客户端媒体库列表不会出现它，用 guid 直达也返回 `404`。

### 图片修复队列

数据库里有图片记录但取不到图（换盘 / 迁移 / 挂载掉线 / 远程图失效）时，图片接口会返回干净的 `404`（而不是 5xx——客户端会把 5xx 当成鉴权或服务器故障反复重试），并把条目排进修复队列；下一轮扫描会换成 TMDB 远程图。

```bash
curl https://panel.example.com/api/admin/emby/libraries/repair/queue -H "Authorization: Bearer $TOKEN"
curl -X POST https://panel.example.com/api/admin/emby/libraries/repair/run -H "Authorization: Bearer $TOKEN"
```

## 10. 运营配置

进后台「系统设置」按需配置（这些都直接决定 EA 对客户端的态度）：

| 配置 | 作用 |
| --- | --- |
| 付费墙（`subscription_required` + 拦截文案） | 非会员的播放、直连流、HLS、下载一律被拦；管理员始终放行 |
| 下载开关（`allow_download`） | 关闭后客户端下载与 `/Items/{id}/File` 等价路径一并被拦 |
| 设备风控（`device_limit_per_user` / `device_limit_auto_evict`） | 每用户设备上限，超限自动踢最久未使用设备 |
| 登录与安全日志（`login_log_retention_days`） | 登录成功/失败、设备超限、诱饵码触发等事件保留天数 |
| 卡码体系 | 注册码 / 续期码 / 白名单码 / 诱饵码 / 指名码的生成、筛选、停用与回收 |
| 经济设置 | 支付网关（易支付兼容）、签到奖励、邀请返利比例 |

### 10.1 多服运营（一个面板管多个服）

面板可以同时运营多个**服**——一个服 = 一套可以独立运营的播放服务（自己的媒体库、存储挂载、
套餐、订阅、卡码、求片与播放节点）。升级上来的老部署只有「默认服」（`slug=main`），行为不变。

| 位置 | 做什么 |
| --- | --- |
| 顶栏「当前服」 | 切换面板当前操作的服；订阅、套餐、媒体库、存储挂载、服务器都只看它。服务端落库，多管理员口径一致 |
| 「多服运营 → 服管理」 | 新建 / 改名 / 填对外地址 / 停用 / 删除（有数据时必须把数据移交给另一个服，默认服不能删）；每服的运营数据与播放节点一目了然，还可以一键「节点体检」 |
| 「商品与套餐」 | 订阅套餐带归属服：买哪份就开哪个服的会员；积分充值套餐全服共用 |
| 「订阅管理」 | 默认只看当前服，可切「全部服」跨服汇总；会员归属服在列表里直接可见 |
| 「服务器」 | EA / 已有 Emby 一个服一个（新增时选归属服）；MoviePilot / qBittorrent 可声明「全服共用」 |

一个后端服可以部署到多台机器（每台一个 EA）同时出流：EA 那侧要配 `NODE_KEY` 与 `REALM`，
库的「归属节点」在「媒体库」页指定，详见 [EA 部署 · 多台 EA 与多个服](./deploy-ea.md#10-多台-ea-与多个服多机多服)。
**会员是一个服一个的**：甲服的会员不能连乙服的地址播放，这是预期行为。

### 10.2 把求片接到下游：服务器清单

后台「多服运营 → 服务器」是一份**可增删改的清单**，四类都可以加多台：

| 类型 | 填什么 | 作用 |
| --- | --- | --- |
| 后端服（EA） | 地址（+ 归属服） | 本项目自带的 Emby API。同一个服可加多台（每台一个 `NODE_KEY`），同一时间每服各有一台「当前使用」 |
| 已有 Emby 服 | 地址 + API Key（可选） | 接入你自己那台 Emby / Jellyfin；接入后本项目自建媒体库功能停用 |
| MoviePilot | 地址 + API 密钥与/或用户名密码 | 把求片提交成它的订阅，由它去搜索、下载、整理入库 |
| qBittorrent | 地址 + 用户名密码（+ 保存目录 / 分类） | 下载器；拿到磁力 / 种子链接后直接下载 |

几个要点（也是最容易踩的坑）：

- **MoviePilot 的两套凭据不是一回事**：API 密钥（`API_TOKEN`）只能**查订阅**；想**提交订阅**
  必须填用户名 + 密码（它的 `POST /api/v1/subscribe/` 要求登录用户身份）。只填 API 密钥时
  体检会通过但会明说「还不能提交订阅」，不会静默失败。
- **qBittorrent 自己不会去找片子**。它是下载器，不是索引器：把求片交给它时需要你提供磁力链接
  或 `.torrent` 地址（整段分享文本也能用，会自动从里面挑出磁力链接）。真正的「自动找片下载」
  交给 MoviePilot（它自己带下载器配置）。
- **qB 的 Web UI 默认只监听本机**。要跨机连接请开 HTTPS 并在 qB 里限制来源 IP。
- **EA 被设为「当前使用」时会顺带做一次挂载体检**：同一台机器能不能碰到你配的存储，
  在这一步就能看出来（逐条结论在「存储挂载」页）。
- **删除或停用「当前使用」的 EA / Emby** 会自动收回旧的 Emby 配置：另一类还有激活的服务就
  交给它，都没有则回到「面板自己出流」。

配好之后，求片审核页每条记录就有「交 MoviePilot / 交给 qB」按钮，转交结果（交给谁、成没成、
为什么没成）会显示在列表里，并落库到求片记录。

**已移除的组件**：`telegram_login_bot/` 已从仓库删除。它调用的是 `{WEB_URL}/api/user/auth/telegram-login`，
而 EM 从来没有实现过这个端点（也没有任何前端入口），也就是说这个 Bot 的登录链接必然是 404，
属于「看起来能用、实际跑不通」的残留，连同旧栈一起清掉了。

> 如果你确实要 Telegram 免密登录，需要新做一套带签名校验的链路（Bot 与 EM 共享一个密钥，
> EM 只接受 Bot 签名的登录请求），不能做成「传 telegram_id 就登录」——那等于任何人知道对方 ID
> 就能登进别人账号。需要时告诉开发这边，再单独实现。

## 11. 验证清单

```bash
BASE=https://panel.example.com

curl -s -o /dev/null -w '%{http_code}\n' $BASE/            # 200 门户页面
curl -s -o /dev/null -w '%{http_code}\n' $BASE/admin/      # 200 后台页面
curl -s $BASE/api/health                                    # 面板健康检查
curl -s $BASE/api/health/detailed                           # 数据库 / Redis / WebSocket
```

浏览器再确认：首页有样式、刷新 `/wallet` 等子路由不 404、`/admin/` 能登录进「数据概览」。

## 12. 备份

数据库与 `SECRET_KEY` 一起备（丢了密钥等于所有人重新登录，且 EA 会因配对校验拒绝启动）。SQLite / PostgreSQL 的具体命令、定时任务与恢复步骤见 [运维 · 备份 · 排错](./operations.md)。

## 下一步

- 把客户端接上：部署 [EA · Emby API](./deploy-ea.md)
- 上线检查与排错：[运维 · 备份 · 排错](./operations.md)
