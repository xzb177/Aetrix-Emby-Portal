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
