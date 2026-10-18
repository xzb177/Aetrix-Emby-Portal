# Aetrix Docker 部署

## 单机 EM 部署

当前 Compose 默认是单机模式：Aetrix EM 同时提供门户、后台 API 与 Emby 协议面，Redis 作为内部缓存/限流服务。

```bash
cp env.example .env
# 至少填写固定随机 SECRET_KEY；生产不要使用临时密钥
# 可按需修改 AETRIX_PORT、MEDIA_ROOT

bash scripts/deploy.sh
```

`scripts/deploy.sh` 会校验 Compose 配置、构建镜像、启动服务，并等待 `aetrix` 容器的健康检查通过；
它不会拉取远端代码。首次部署前只执行一次上面的 `cp` 并填写 `.env`，之后重复部署直接运行脚本即可。
想先看将要执行的命令，可以使用：

```bash
bash scripts/deploy.sh --dry-run
```

手动等价流程：

```bash
docker compose build --pull
docker compose up -d --remove-orphans
docker compose ps
curl http://127.0.0.1:8000/api/health
```

容器结构：

- `aetrix`：Python 3.12 + 前端多阶段构建 + ffmpeg，默认端口 `8000`；
- `aetrix-redis`：Redis 7，数据通过 Docker volume 持久化；
- `aetrix_data`：SQLite、图片缓存、字幕缓存；
- `redis_data`：Redis AOF 数据。

媒体目录由 `MEDIA_ROOT` 映射到容器 `/media`，默认只读。媒体挂载配置若使用主机路径，必须改成容器内可见的路径。

## 一键更新

项目根目录提供了一键更新脚本，默认会检查依赖与 Git 工作区、尝试备份容器内 SQLite，然后拉取代码、
重新构建镜像并更新 Compose 服务：

```bash
bash scripts/update.sh
```

脚本会在工作区有未提交改动时停止，不会用远端代码覆盖本地现场；容器正在运行且 `/data` 下有 SQLite
数据库时，会使用容器内置的 Python `sqlite3` 模块把备份写到 `/data/aetrix-update-时间.db`。备份失败会
停止更新；找不到数据库时会明确告警并继续代码更新。

常用选项：

```bash
bash scripts/update.sh --dry-run       # 只打印流程，不实际拉取、备份、构建或重启
bash scripts/update.sh --skip-backup   # 明确跳过数据库备份
```

手动等价流程：

```bash
git pull --ff-only
docker compose build --pull
docker compose up -d --remove-orphans
docker compose ps
```

升级前备份：

```bash
docker compose exec aetrix sh -c 'sqlite3 /data/aetrix_unified.db ".backup /data/aetrix-backup.db"'
```

生产环境不要把 `.env`、数据库、媒体文件写入镜像；`.dockerignore` 已排除这些内容。

## 分离 EM / EA

分离部署时建议拆成两套 Compose 项目：

- EM：`ENABLE_EMBY_GATEWAY=false`，只提供面板；
- EA：使用同一个代码镜像，启动命令改为 `python serve_emby.py`，使用共享 PostgreSQL 与完全相同的 `SECRET_KEY`；
- EA 所在机器必须挂载实际媒体目录，并设置 `NODE_KEY` / `REALM`。

跨机器生产部署不要使用 SQLite；改用 PostgreSQL，并让 EM 与 EA 连接同一个数据库。

## 反向代理

当前 Compose 直接暴露 `AETRIX_PORT`。接入 Nginx/Caddy 时只反代到 `127.0.0.1:8000`，外部 HTTPS 终止在反代层；不要把 Redis 端口暴露公网。

### 反代下的限流 IP（`TRUSTED_PROXIES`）

**接了 Nginx / Caddy 就必须配这一项，否则全站用户共用一个限流桶。**

`backend/ratelimit.py` 只在「TCP 直连方是可信代理」时才采信 `X-Real-IP` / `X-Forwarded-For`
（防伪造：这两个头客户端可以随便写，无条件采信等于限流形同虚设）。
默认只信回环 `127.0.0.0/8`。

问题在于 Compose 部署下，请求的路径是「Nginx（宿主机）→ 宿主机端口 → docker-proxy → 容器」，
容器看到的直连方是 **docker 网关**（实测 `172.18.0.1`），不是回环 → 判定为不可信 →
nginx 写好的 `X-Real-IP` 被忽略 → **每个用户的 IP 都变成网关地址**。

登录限流是每分钟 10 次，于是所有人共用这一个桶，任意一个人多试几次就会把全站 429 锁在门外。

先查网段（不要照抄 `172.18`，不同机器不同）：

```bash
docker network inspect aetrix-portal_default --format '{{range .IPAM.Config}}subnet={{.Subnet}} gw={{.Gateway}}{{end}}'
# 例：subnet=172.18.0.0/16 gw=172.18.0.1
```

再写进 `.env`：

```bash
TRUSTED_PROXIES=172.18.0.0/16
```

配置生效后应同时成立：

- 真实用户经反代访问 → 拿到**各自**的真实 IP（限流各算各的）；
- 直连并伪造 `X-Real-IP` / `XFF` → 被忽略，用直连 IP（防伪生效）。

部署后可以在容器内验证这两条：

```bash
docker exec aetrix-api python3 -c "
import os; from backend.ratelimit import _is_trusted_proxy
print('TRUSTED_PROXIES =', os.getenv('TRUSTED_PROXIES'))
print('网关可信 =', _is_trusted_proxy('172.18.0.1'))"
```

容器直连（`request.client.host` 是 `127.0.0.1`）时不需要配，回环默认可信。

### 播放延迟与 rclone 取流（VFS 缓存）

`rclone rcd --rc-serve` 取流**不经过 VFS 层**，而 VFS 缓存只挂在 VFS 上。
rclone 1.71.1 的 `rcd` 连 `--vfs-cache-mode` 这个 flag 都没有：

```bash
docker exec aetrix-rclone rcd --help | grep -c vfs-cache-mode   # → 0
```

所以经 rcd 取的每一个 Range 请求都是冷读。而 Google Drive 每次 Range 读有
**约 1.2 秒固定往返**（与大小、位置无关）：

| 请求 | 耗时 | 吞吐 |
|---|---|---|
| 1MB | 1220-1560 ms | 0.78 MB/s |
| 8MB | 1470 ms | 5.44 MB/s |
| 32MB | 1900 ms | 16.84 MB/s |

带宽本身并不差（17 MB/s），慢在**请求次数**——播放器起播时探测、缓冲不足时续读，
全是小 Range 请求，每次都吃这段延迟。实测起播三步累计约 **4.1 秒**。

## 用 serve http 做数据面（推荐）

Compose 已配好 `rclone-serve` 服务，跑 `rclone serve http` 并开启 VFS 缓存，
与 `rcd`（控制面）并行：

| | 端点 | 用途 |
|---|---|---|
| 控制面 | `aetrix-rclone:5572`（`rcd`） | 列目录、查状态（`/operations/list` 等） |
| 数据面 | `aetrix-rclone-serve:8080`（`serve http`） | 媒体字节，**带 VFS 缓存** |

实测同一文件起播三步：

| | 现状（rcd） | serve http + 缓存 |
|---|---|---|
| 起播累计 | 4100 ms | 1962 ms（冷）→ 35 ms（热） |
| 32MB 吞吐 | 12.67 MB/s | 19.43 MB/s |

要让面板的 rclone 挂载走缓存，在挂载配置里填上**取流地址**：

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
> 换句话说：serve http 的 URL **既没有方括号、也没有 `MP/` 前缀**。
> 多写一个 `/MP/` 实测就是 404（而缓存目录照样在长，很容易误判成「缓存生效了」）。

### 缓存限额必须给死

单个 4K 电影就 17GB，缓存不给上限会撑爆磁盘。compose 默认：

| 参数 | 默认 | 作用 |
|---|---|---|
| `RCLONE_VFS_CACHE_MAX_SIZE` | `8G` | 缓存总上限 |
| `RCLONE_VFS_CACHE_MIN_FREE` | `5G` | 剩余低于它就停止写入（宁可不缓存也不撑爆盘） |

缓存落在 `${RCLONE_CONFIG_DIR}/vfs/<remote>`（宿主 `/opt/aetrix-rclone/vfs/MP`），可直接查看：

```bash
du -sh /opt/aetrix-rclone/vfs
```

> 注意：rclone **没有** `--vfs-cache-dir` 这个 flag，VFS 缓存固定放在
> 全局 `--cache-dir` 下的 `vfs/` 子目录。写成 `--vfs-cache-dir` 会让容器直接
> `Fatal error: unknown flag` 起不来（已实测）。

## 安全要求

- `SECRET_KEY` 至少 32 字符，EM/EA 必须一致；
- `.env` 权限设为 `600`；
- Docker 容器不要使用 `--privileged`；
- 缓存目录与数据库使用持久卷；
- 部署后验证 `/api/health`、`/admin/`、`/emby/System/Info/Public`；
- root 密码部署完成后应立即轮换，改用 SSH key 并关闭密码登录。

> 更新脚本不会修改宿主机配置、数据库卷或媒体目录。
