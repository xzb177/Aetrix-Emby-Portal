# Aetrix Docker 部署教程（自建版）

本篇讲的是**从源码自建**：在你自己的机器上用 Docker Compose 把 Aetrix 构建并跑起来。
如果你拿到的是**授权发行包**（只有编排文件、没有源码，拉官方镜像部署），请看仓库根目录的
[DEPLOY.md](../DEPLOY.md)，不要混用两边的命令。

整套系统由两部分组成：

- **后端**（`docker-compose.yml`）：API、Emby 协议网关（EA）、后台任务（worker）、
  PostgreSQL、Redis，共 5 个容器；
- **网页前端**（`docker-compose.frontend.yml`）：一个 nginx 容器 `aetrix-web`，
  提供用户端与管理后台页面，并把 `/api/`、`/emby/` 反代到后端。

只起后端也能给第三方 Emby 客户端用，但**网页打不开**——想要完整的网页体验，
两份编排文件要一起用（见第 3 节）。

---

## 1. 准备

- 一台 Linux 主机（本地电脑、NAS、VPS 均可），装好 Docker Engine 与 Docker Compose v2
  （能运行 `docker compose version`，注意是带空格的 v2 写法）；
- Git（拉代码用）；
- 磁盘留足空间：镜像构建、图片缓存、（可选）rclone 播放缓存都要占盘，媒体文件本身不进容器。

Ubuntu / Debian 装 Docker：

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
docker --version && docker compose version
```

## 2. 拿代码、写配置

```bash
git clone https://github.com/xzb177/Aetrix-Emby-Portal.git
cd Aetrix-Emby-Portal
cp env.example .env
chmod 600 .env
```

`.env` 里**只有两项必须改**，其余保持默认就能跑起来：

```bash
# 1) 会话签名密钥（至少 32 字符；泄露 = 别人能伪造登录态）
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')|" .env

# 2) 数据库密码
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')|" .env

# 确认改到了（应输出 2 行 <已设置>，且长度都明显大于 1）
grep -E '^(SECRET_KEY|POSTGRES_PASSWORD)=' .env | sed 's/=.*/=<已设置>/'
awk -F= '/^SECRET_KEY=|^POSTGRES_PASSWORD=/{print $1, "长度:", length($2)}' .env
```

> macOS 的 `sed -i` 要写成 `sed -i '' "s|...|...|"`。写错了也不怕，下面的 `grep` 会当场告诉你。

常改的可选项（不改也能启动）：

| 配置项 | 说明 |
|---|---|
| `MEDIA_ROOT` | 宿主机上的影视目录，默认 `./media`，以**只读**方式挂进容器的 `/media`。之后在管理后台「媒体库管理」里按容器内路径加库；挂载配置里如果写的是宿主机路径，必须换成容器内能看到的路径 |
| `SETUP_BOOTSTRAP_TOKEN` | 首次初始化向导的部署者凭据，**建议设置**（任意一串足够长的随机字符串）。设了之后，建第一个管理员时必须填对它，防止服务刚暴露到公网就被陌生人抢注管理员。生成方法同 `SECRET_KEY` |

不要动的项：

- **`DATABASE_URL`**：模板里默认没有这一行，它由编排文件根据 `POSTGRES_PASSWORD` 自动拼。
  自己手写一行、又和 `POSTGRES_PASSWORD` 不一致，是这套部署最常见的启动失败原因
  （日志报 `password authentication failed for user "aetrix"`）。已经写坏了就把那一行删掉。
- **`PORT` / `HOST`**：容器内部监听用，编排文件不读，改了没有任何效果。
- **`AETRIX_PORT`**：那是**授权版**编排（`docker-compose.prod.yml`）的对外端口；
  自建版的端口写在编排文件的 `ports` 里（见第 3 节），改 `.env` 这一项对自建版无效。
- **`REDIS_ENABLED`**：保持 `true`。改成 `false` 后台任务进程会拒绝启动，服务永远等不到「就绪」。

## 3. 启动

在仓库根目录执行（两份编排文件一起用，才是完整的一套）：

```bash
docker compose -f docker-compose.yml -f docker-compose.frontend.yml build --pull
docker compose -f docker-compose.yml -f docker-compose.frontend.yml up -d
docker compose -f docker-compose.yml -f docker-compose.frontend.yml ps
```

首次构建要拉基础镜像并编译前端，耗时取决于机器与网络。`ps` 里各容器 `STATUS`
陆续变成 `healthy` 即就绪（后端健康检查的宽限期就有 60–90 秒，第一次启动请等 2–3 分钟）。

启动后各入口：

| 入口 | 地址 | 说明 |
|---|---|---|
| 网页（用户端 + 管理后台） | `http://<主机IP>:8081`，后台在 `/admin/` | 经 `aetrix-web`（nginx）访问 |
| 后端 API | `http://<主机IP>:8000` | 只提供接口，不提供网页（直接开根路径看不到页面是正常的） |
| Emby 协议面 | `http://<主机IP>:8000` | 默认 `ENABLE_EMBY_GATEWAY=true` 时，播放器填这个地址，不要加 `/emby`，不要填 `:8081`。`:8081` 的 nginx 只反代 `/emby/` 前缀，根路径是网页。EA 的宿主机映射是 `127.0.0.1:8002`（容器内 8001），只给本机反代用，不要暴露到公网 |

想改端口：改编排文件里的 `ports`（后端在 `docker-compose.yml` 的 `aetrix-api`，
网页在 `docker-compose.frontend.yml` 的 `aetrix-web`），改完重新 `up -d`。

### 用一键脚本（只管后端）

```bash
bash scripts/deploy.sh          # 首次部署后端；--dry-run 可先看它要执行什么
```

注意它的边界：`scripts/deploy.sh` / `scripts/update.sh` 只管理**默认编排文件里的后端 5 个容器**，
不包含网页前端；而且它们带的 `--remove-orphans` 会把不在默认编排文件里的容器（比如你用
组合命令起的 `aetrix-web`）摘掉。**脚本路线和组合命令路线二选一，不要混用。**

## 4. 容器与数据都存在哪

| 容器 | 职责 |
|---|---|
| `aetrix-api` | 面板 API（用户、媒体库、计费、后台接口） |
| `aetrix-ea` | Emby 协议网关：客户端认证、浏览、出流 |
| `aetrix-worker` | 扫描、刮削、探测等后台任务 |
| `aetrix-postgres` | PostgreSQL 16，主数据库 |
| `aetrix-redis` | Redis 7，缓存 / 限流 / 队列 |
| `aetrix-web` | nginx：网页静态文件 + `/api/`、`/emby/` 反代（来自前端编排文件） |

数据卷（`docker compose down` 不会删，**`down -v` 会全删，慎用**）：

| 卷 | 存什么 |
|---|---|
| `postgres_data` | 数据库本体——最重要的数据在这里 |
| `aetrix_data` | 海报图片缓存、转码临时目录（**不是数据库**） |
| `redis_data` | Redis AOF 数据 |
| `aetrix_backups` | 数据库备份的持久化卷（与部署解耦，重建容器不丢） |

媒体目录由 `MEDIA_ROOT` 映射到容器 `/media`，默认只读，服务不会改动你的媒体文件。
生产环境不要把 `.env`、数据库、媒体文件打进镜像；`.dockerignore` 已排除这些内容。

## 5. 第一次登录：建管理员

服务就绪后打开 `http://<主机IP>:8081/admin/`，第一次打开会被送到**初始化向导**，
在那里创建第一个管理员账号（新库里不会有任何默认账号，第一个注册的普通用户也不会
自动变成管理员，这是刻意的安全设计）。向导完成后入口永久关闭。

- 如果你在 `.env` 里设了 `SETUP_BOOTSTRAP_TOKEN`，向导会要求填入它，照填即可；
- 向导不可用（比如旗标状态异常）时，自建版还可以用镜像里自带的脚本造管理员：

```bash
docker compose exec aetrix-api python scripts/create_admin.py
# 不带参数 = 创建/升级 admin 账号并打印随机强密码；-u/-p 可指定，详见脚本 --help
```

建好管理员后，用普通用户流程注册/登录门户，再进媒体库管理添加你的第一个媒体库。

## 6. 部署后验证

```bash
curl http://127.0.0.1:8000/api/health                 # 后端健康，应返回 200 且状态 healthy
curl -I http://127.0.0.1:8081/                        # 网页入口
curl http://127.0.0.1:8081/emby/System/Info/Public    # Emby 协议面（经 nginx 转发）
docker compose -f docker-compose.yml -f docker-compose.frontend.yml ps   # 全部 healthy
```

## 7. 更新到新版本

更新前**先备份数据库**（第 8 节）。然后：

```bash
git pull --ff-only
docker compose -f docker-compose.yml -f docker-compose.frontend.yml build --pull
docker compose -f docker-compose.yml -f docker-compose.frontend.yml up -d
```

数据（数据库、图片缓存、配置）都在数据卷和 `.env` 里，更新不会丢。

关于 `scripts/update.sh`（只走脚本路线的人用）：它做的事是
检查工作区（有未提交改动会停下，不会覆盖现场）→ 尝试备份 → `git pull --ff-only`
→ 重新 `build` → `up -d`，常用选项 `--dry-run`（只打印流程）、`--skip-backup`（明确跳过备份）。
**但要清楚它的备份边界**：脚本只会备份 `/data` 下的 **SQLite** 文件；而现在的默认部署是
PostgreSQL，`/data` 里没有 SQLite，脚本会打印一条「没有找到 SQLite 数据库，跳过备份」的
告警然后继续更新——也就是说 **PostgreSQL 部署用它更新等于没有备份**，务必先按第 8 节
手动备份，再跑更新。

回滚：`git checkout <旧版本的提交或标签>` 后重新执行上面的 `build` + `up -d` 即可；
若新版本已经写过数据库结构，先用第 8 节的备份恢复，再回滚代码。

## 8. 备份与恢复

PostgreSQL 备份用仓库自带脚本（在 postgres 容器内跑 `pg_dump`，带完整性校验，
默认保存在仓库 `backups/` 目录、保留 14 天）：

```bash
bash scripts/backup_db.sh
# 可用环境变量覆盖：BACKUP_DIR=… RETENTION_DAYS=… PG_CONTAINER=…
```

也可以手工来一份：

```bash
docker compose exec postgres pg_dump -U aetrix aetrix > backup-$(date +%F).sql
```

恢复与更多运维细节（定时备份、恢复演练、旧 SQLite 部署的备份方式）见
[运维 · 备份 · 排错](./operations.md)。

> 后台「数据库定时备份」在 PostgreSQL 上会调用容器内的 `pg_dump`（custom 格式，见[运维手册](./operations.md)）。它不能代替更新前的 `scripts/backup_db.sh`。第 7 节已经说明：`update.sh` 不备份 PostgreSQL。

## 9. 反向代理与 HTTPS

对外提供服务时，把 Nginx / Caddy 指到网页入口（宿主机 `8081`，即 `aetrix-web`），
HTTPS 在反代层终止；只调接口也可指到 `8000`。不要把 PostgreSQL、Redis、EA 的
`8002` 端口暴露到公网。

### 反代下必须配 `TRUSTED_PROXIES`，否则全站共用一个限流桶

`backend/ratelimit.py` 只在「TCP 直连方是可信代理」时才采信 `X-Real-IP` /
`X-Forwarded-For`（这两个头客户端可以随便伪造，无条件采信等于限流形同虚设）。
默认只信回环 `127.0.0.0/8`。

问题在于容器部署下，请求的路径是「Nginx（宿主机）→ 宿主机端口 → docker-proxy → 容器」，
容器看到的直连方是 **docker 网关**（例如 `172.18.0.1`），不是回环 → 判定为不可信 →
反代写好的真实 IP 被忽略 → **每个用户的 IP 都变成网关地址**。而登录限流是按 IP 计的，
于是所有人共用一个桶，任意一个人多试几次就能把全站锁在门外（429）。

先查你机器上实际的网段（不要照抄示例里的 `172.18`，每台机器可能不同）：

```bash
docker network inspect aetrix-portal_default --format '{{range .IPAM.Config}}subnet={{.Subnet}} gw={{.Gateway}}{{end}}'
# 网络名以 docker network ls 看到的本项目网络为准（编排项目名 aetrix-portal + _default）
```

再写进 `.env` 并重启后端：

```bash
TRUSTED_PROXIES=172.18.0.0/16
```

配置生效后应同时成立：经反代访问的用户拿到各自的真实 IP（限流各算各的）；
直连并伪造 `X-Real-IP` 的请求被忽略（防伪生效）。容器直连（直连方是 `127.0.0.1`）
时不需要配这项，回环默认可信。

同一份名单还决定「没配 `EMBY_PUBLIC_URL` 时」播放地址肯不肯采信 `X-Forwarded-Proto`，见下一节。

### 客户端对外地址

播放器拿到的绝对地址，和用户端账号卡上「该把服务器填成哪」的地址，是两条解析链。
两边都要指到客户端真正连得上的 https 入口，否则会出现「账号卡是域名、播放地址却是 http」
或反过来。

**播放地址**（`backend/emby_server/public_url.py` 的 `client_base_url`）用于
PlaybackInfo 的 `DirectStreamUrl` / `DirectPlayUrl` / `TranscodingUrl`、字幕
`DeliveryUrl`、本服务自己的图片绝对地址、HLS 切片，以及 `System/Info` 的
`LocalAddress` / `WanAddress`。顺序是：

1. `EMBY_PUBLIC_URL` 已设置，并且字面以 `http://` 或 `https://` 开头：用它。不看 Host，不看转发头。只填根地址，不要带路径或查询串；代码不剥路径，`https://media.example.com/emby` 会原样留在基址里，后面再拼 `/emby/Videos/...`。尾斜杠会去掉；默认端口 `:443` / `:80` 会去掉。
2. 否则，仅当这次 TCP 直连方允许采信转发头时，用 `X-Forwarded-Proto` 与 `X-Forwarded-Host` 的第一段（协议只接受 `http` / `https`）。允许的直连方是：`TRUSTED_PROXIES`（默认含回环 `127.0.0.0/8` 与 `::1`）、`FORWARDED_ALLOW_IPS` 里的精确 IP 或 CIDR，以及「可信代理已经把真实 IP 写回请求」的那次请求（`CloudflareIPMiddleware` 在确认对端可信并还原出地址后留下标记；还原之后 `scope["client"]` 已是用户 IP，不能再拿它判断这次头可不可信）。`*` 不在这份名单里，见下面「反代后面」。
3. 否则用请求自己的地址（`request.base_url`）。

内置的 Cloudflare 回源网段参与「真实 IP 可不可信」（`_peer_trusted`），不因此让播放地址采信 `X-Forwarded-Proto`。`流媒体加速` 开着或 `TRUST_CF_IP=true` 时，中间件才会在这些网段上还原真实 IP，还原成功后这次请求的转发头才按上一条第 2 步处理。

拼完基址之后，`DirectStreamUrl` / `TranscodingUrl` 还可能被改写（`rewrite_playback_urls_unified`）：有健康且为公网地址的流节点时，换成**该节点登记的地址**；否则流媒体加速开着时换成 `https://` 加加速域名；都没有则保持上面的基址。Drive 直链播放跳过这次改写。流节点的登记地址不是主站的 `EMBY_PUBLIC_URL`。EA 启动时自动登记用的是 `EMBY_API_PUBLIC_URL`；没设且 `HOST` 为 `0.0.0.0` 或 `::` 时不拼地址。内网、回环地址不会被拿去改写播放 URL。

**账号卡与一键导入**（`resolve_emby_base_url`）是用户该填进播放器的服务器根。顺序是：

1. 该服在「多服运营 → 服管理」里填的「对外地址」（`realm.url`）。
2. 该服在「服务器」页当前入口上的地址（自建 EA 的 `emby_managed_url`，或已有 Emby 的 `emby_external_url`）。
3. 这是非默认服，且上面两项都空：账号卡不下发地址，**不再**回退到环境变量或当前请求。
4. 默认服：`EMBY_PUBLIC_URL`（同样只认 `http://` / `https://`）。
5. 与播放地址第 2 步相同的可信转发头；对端不可信时，用当前请求的地址。
6. 连请求都没有：占位 `http://localhost:8000`，来源标成未配置，面板不拿它当「已经配好」报红。

所以服上的对外地址、服务入口，只压过账号卡和导入链接，不参与 PlaybackInfo。

#### 域名 + https（推荐）

在 `.env` 写上客户端真正访问的根。只填根，不要带路径（不要带 `/emby`，也不要带查询串）：

```bash
EMBY_PUBLIC_URL=https://media.example.com
```

改完重启后端：运行中的进程读的是自己的环境，看不到 `.env` 文件里的新值。管理后台「服管理」里把同一个地址填进该服的「对外地址」。默认服若两项都空着，账号卡才会用到这个环境变量；非默认服空着则账号卡没有地址。服务入口若另外填了一台 EA，账号卡用服务入口，播放地址仍用这里的 `EMBY_PUBLIC_URL`。两边填成同一个 https 根，客户端添加服务器和随后的拉流才是同一台入口。

#### 反代后面、不设 `EMBY_PUBLIC_URL`

TLS 在 Nginx 或 Cloudflare 终结时，启动器传给 uvicorn 的信任名单在 `FORWARDED_ALLOW_IPS` 未设置时只有回环 `127.0.0.1` 与 `::1`。容器里看到的直连方是 Docker 网关或旁边的容器，不在默认名单里，PlaybackInfo 就按 `request.base_url` 拼出 `http://…`。80 端口若只做 301 到 https，不跟随 301 的客户端（例如 SenPlayer）的 `/stream` 到不了本服务。

把反代所在网段写进 `TRUSTED_PROXIES` 或 `FORWARDED_ALLOW_IPS`，并让那一跳带上 `X-Forwarded-Proto: https` 与对外 Host。网段先查再填，不要假设每台机器都是下面的示例：

```bash
docker network inspect aetrix-portal_default --format '{{range .IPAM.Config}}subnet={{.Subnet}} gw={{.Gateway}}{{end}}'
```

```bash
TRUSTED_PROXIES=172.18.0.0/16
```

`172.18.0.0/16` 同时盖住网关（常见 `172.18.0.1`）和同一座桥上的其它容器。只把 `172.18.0.1` 写进 `FORWARDED_ALLOW_IPS` 时，只覆盖「经宿主机端口、docker-proxy 进来」的那一跳；`aetrix-web` 在 Docker 网络里直连 `aetrix-api` 时，对端是网页容器自己的地址，不在这一条里。

本机 Nginx 反代到 `127.0.0.1` 时，回环默认就在 `TRUSTED_PROXIES` 里，不必再加网段。仓库自带的 `docker/nginx.conf` 只 `listen 80`，并且 `proxy_set_header X-Forwarded-Proto $scheme`：网页容器自己看到的是 http，它转发给 API 的协议头也是 http。前面的 Cloudflare 或宿主机 Nginx 即使已经终结了 TLS，这一跳也会把协议盖成 http。这种拓扑下，信任 `172.18.0.0/16` 得到的播放地址仍是 `http://` 加当时的 Host。要 https 播放地址，设置上一节的 `EMBY_PUBLIC_URL`。

不要写 `FORWARDED_ALLOW_IPS=*`。`*` 会让任意一个直连方用 `X-Forwarded-For` 伪造客户端 IP：限流按这个 IP 分桶，按客户端 IP 做的访问判断也会跟着被绕过；公开地址的 scheme / Host 同样能被改掉。现在 `*` 在域名守卫、公开 URL 和启动器里都被丢掉，每个进程只记一次 WARNING。`serve.py`、`serve_emby.py`、`backend/main.py`、`emby_api/main.py` 都把去掉 `*` 之后的名单显式传给 uvicorn，不让 uvicorn 自己读到环境变量里的 `*`。这一项只剩 `*`、或根本没写时，uvicorn 只信回环 `127.0.0.1,::1`。同一行里的精确 IP 和 CIDR 仍然有效。`TRUSTED_PROXIES`（默认回环）和内置 Cloudflare 网段不靠这个 `*`，写 `*` 也不会取消它们。写错的网段会被忽略，不能用 `*` 代替网段。

**升级（#1172 之后）**：原来写成 `FORWARDED_ALLOW_IPS=*` 的部署，升级后 uvicorn 只信回环，网关发来的转发头不再被采信。把 `*` 换成网关的精确 IP 或网段，例如 `FORWARDED_ALLOW_IPS=172.18.0.0/16`，或把同一网段写进 `TRUSTED_PROXIES`。网段先用上面的 `docker network inspect` 查，不要假设每台机器都是这个示例。两项都不改、又没有设置 `EMBY_PUBLIC_URL` 时，播放地址回退成 `http://`。限流只在直连方属于 `TRUSTED_PROXIES` 时才读 `X-Real-IP` / `X-Forwarded-For`；网关不在默认回环里，每个用户的 IP 都会变成网关地址，正常用户共用一个限流桶。

#### 局域网 IP 直连

播放器直接连 `http://192.0.2.10:8000` 这类地址、前面没有反代时，什么都不用设。对端是客户端自己，不在可信代理里，伪造的转发头会被忽略，播放地址和账号卡（默认服、服地址与服务入口都空着）都落回这次请求的地址。

#### 校验与缓存

- 启动时若设置了 `EMBY_PUBLIC_URL`，却不是以 `https://` 开头（大小写不敏感），打一条 WARNING，服务照常启动。`http://…` 运行时仍然使用，不会因为这条警告被丢掉。字面不是 `http://` 或 `https://` 开头的值，运行时当成没配。未设置不告警。
- 域名守卫的允许名单分两步：管理后台「流媒体加速」开关打开 **并且** 填了加速域名时，用那份域名；这两项没有同时满足时，才看环境变量 `ENFORCE_DOMAIN`。两边都空则守卫关闭，请求原样放行。名单非空时，启动再检查 `EMBY_PUBLIC_URL` 的主机在不在里面；不在只打 WARNING，地址照样下发。真正的拦截在请求上：Host 不在名单里返回 403，所以客户端要连的域名得在这份名单里。这是域名守卫，不是播放防盗链的 Referer 白名单；Referer 白名单不参与 `EMBY_PUBLIC_URL` 的启动检查，空名单等于不检查 Referer。
- PlaybackInfo 把 MediaSource（含字幕 `DeliveryUrl` 里的基址）缓存 `PLAYBACKINFO_CACHE_TTL` 秒，默认 300，设 `0` 关闭。缓存键是条目、用户、设备 profile 指纹和码率上限，不含对外基址。`DirectStreamUrl` / `DirectPlayUrl` / `TranscodingUrl` 在读缓存之后按**当次**基址重拼，改地址并重启后这三条下次请求就是新基址。字幕 `DeliveryUrl` 留在缓存里的那份仍可能是旧基址，要等这次 TTL 结束。Redis 里的缓存跨重启还在。

分离部署时，播放地址在出协议的进程上算，那是 EA；账号卡在 EM 进程里算（`portal.resolve_emby_base_url` 读到 `EMBY_PUBLIC_URL` 的那一步）。EA 不挂门户路由（`emby_api/main.py` 只挂协议面、搜索、挂载体检和节点身份），所以 EA 环境里的 `EMBY_PUBLIC_URL` 不会出现在账号卡上。两边都要设这个变量：Compose 的同一份 `.env` 会同时进 EM 和 EA；两台机器则各写一份，都只填根地址。EM 上若已经填了「服对外地址」或「服务入口」，账号卡可以不靠 EM 的这个变量；EA 的播放地址仍然要靠 EA 自己的 `EMBY_PUBLIC_URL`（或 EA 前面那一跳可信反代的转发头）。详见 [EA 网关部署 · 客户端接入](./deploy-ea.md#6-客户端接入)。

## 10. rclone 挂载（可选）

自建版的默认编排里**没有** rclone 容器。常见用法是先在宿主机上用 rclone 把网盘挂载好，
再把挂载目录通过 `MEDIA_ROOT` 或编排里预留的 `/mnt/mp`、`/mnt/paul`（只读）给到容器；
rclone 的配置文件目录由 `RCLONE_CONFIG_DIR`（默认 `./rclone`）映射到容器 `/config/rclone`。

授权版编排（`docker-compose.prod.yml`）则自带两个 rclone 服务（用 profile 按需启用）：
`rcd` 做控制面（列目录、查状态），`rclone-serve` 做数据面（取流，带 VFS 缓存）。
两者分工与实测数据如下，自建时要自己加 rclone 服务也可参考这套形态：

| | 端点 | 用途 |
|---|---|---|
| 控制面 | `rclone:5572`（`rcd --rc-serve`） | 列目录、查状态（`/operations/list` 等） |
| 数据面 | `rclone-serve:8080`（`serve http`） | 媒体字节，带 VFS 缓存 |

为什么要分数据面：`rclone rcd --rc-serve` 取流**不经过 VFS 层**（rclone 1.71.1 的
`rcd` 连 `--vfs-cache-mode` 这个参数都没有），每个 Range 请求都是冷读；而 Google Drive
每次 Range 读有约 1.2 秒的固定往返——带宽并不差，慢在请求次数，播放器起播的探测与
续读全是小 Range 请求，每次都吃这段延迟（实测起播三步累计约 4.1 秒）。走
`serve http` + VFS 缓存后，同一文件起播三步实测约 1962 ms（冷）→ 35 ms（热），
32MB 吞吐 12.67 MB/s → 19.43 MB/s。

在管理后台的挂载配置里填**取流地址**，让挂载走缓存数据面：

```json
{"mode":"rc","rc_url":"http://rclone:5572","serve_url":"http://rclone-serve:8080","fs":"MP:"}
```

`serve_url` 留空则沿用旧的 rc-serve 取流（向后兼容）。

> **两种端点的 URL 形态完全不同**，混用就是 404 且极难定位：
>
> | 端点 | 正确形态 | 说明 |
> |---|---|---|
> | rc-serve（5572） | `http://rclone:5572/[MP:]/MoviePilot/x.mkv` | remote 套**方括号**，**保留尾冒号** |
> | serve http（8080） | `http://rclone-serve:8080/MoviePilot/x.mkv` | remote 已挂在**站点根**，路径里**不出现** remote 名 |
>
> 换句话说：serve http 的 URL 既没有方括号、也没有 `MP/` 前缀，多写一个 `/MP/` 实测就是 404
> （而缓存目录照样在长，很容易误判成「缓存生效了」）。

缓存限额必须给死：单个 4K 电影就可能有 17GB，缓存不给上限会撑爆磁盘。授权版编排的默认：

| 参数 | 默认 | 作用 |
|---|---|---|
| `RCLONE_VFS_CACHE_MAX_SIZE` | `2G` | 缓存总上限 |
| `RCLONE_VFS_CACHE_MIN_FREE` | `5G` | 磁盘剩余低于它就停止写入（宁可不缓存也不撑爆盘） |

VFS 缓存落在 rclone 配置目录下的 `vfs/` 子目录（宿主机即 `${RCLONE_CONFIG_DIR}/vfs`），
可以直接 `du -sh` 查看。注意 rclone **没有** `--vfs-cache-dir` 这个参数：VFS 缓存固定放在
全局 `--cache-dir` 下的 `vfs/` 子目录，写错参数容器会直接报 `unknown flag` 起不来（已实测）。

## 11. 分离部署（EM / EA 拆到不同机器）

自建编排里 `aetrix-api`（EM 面板）与 `aetrix-ea`（EA 协议网关）本来就是两个容器；
要进一步拆到不同机器，按 [EA 网关部署](./deploy-ea.md) 与 [EM 面板部署](./deploy-em.md)
两篇操作。跨机部署的硬规矩：

- EM 与 EA 必须连**同一个 PostgreSQL**、用**完全相同**的 `SECRET_KEY`；
- 跨机器不要用 SQLite；
- EA 所在机器必须能读到同一份媒体存储（同样的服务化地址，不要各猜各的本机路径）；
- 数据库、Redis 只走私网，不要暴露公网。

## 12. 安全清单

- `SECRET_KEY` 至少 32 字符，EM / EA 必须一致，且不要与节点密钥等其他密钥复用；
- `.env` 权限 `600`，不要提交进 Git，也不要把完整内容发给任何人（里面是你的全部密钥）；
- Docker 容器不要使用 `--privileged`；
- 数据库密码不要用编排文件里的开发兜底值（那只是为了本地试跑不报错，公网部署等于没设密码）；
- 部署后按第 6 节逐项验证；
- 服务器的 root 密码在部署完成后应立即轮换，改用 SSH key 并关闭密码登录。

> 一键更新脚本不会修改宿主机配置、数据库卷或媒体目录；但如第 7 节所说，
> PostgreSQL 部署在更新前请务必先自行备份。
