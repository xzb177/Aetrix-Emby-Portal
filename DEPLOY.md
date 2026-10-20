# Aetrix Portal · 部署指南（授权版）

这份文档给**已经购买授权**的客户。全程不需要源码、不需要会写代码、不需要连数据库。

---

## 〇、销售/交付方：怎么打这个发行包

> 这一节给我们自己人用。客户可以直接跳到第二节。

在仓库根目录执行（版本号自动取自 `VERSION`）：

```bash
VER=$(cat VERSION)
mkdir -p "dist/aetrix-portal-$VER"

# 生产编排在仓库里叫 docker-compose.prod.yml（与自建用的 docker-compose.yml 并存），
# 发行时**必须改名**为 docker-compose.yml —— 客户那边要能直接 `docker compose up`，
# 不该看到两个 compose 文件不知道选哪个。
cp docker-compose.prod.yml "dist/aetrix-portal-$VER/docker-compose.yml"
cp env.example "dist/aetrix-portal-$VER/"
cp DEPLOY.md "dist/aetrix-portal-$VER/"

cd dist && zip -r "aetrix-portal-$VER.zip" "aetrix-portal-$VER"
```

发出去之前请确认包里**只有这 3 个文件**（`unzip -l` 看一眼），不要误带 `.env`。

| 客户填 | 你要做的 |
|---|---|
| Package 保持 Private | 在 **仓库** Settings → Collaborators 里给他 Read（私有包跟随仓库权限） |
| 或 Package 设 Public | Package → Settings → Change visibility |

权限的详细说明与**收权的真实边界**见第四节。

---

## 一、你拿到的包里有什么

发行包一共 3 个文件，解压后长这样：

```
aetrix-portal-<版本号>/        ← 目录名里的版本号以你收到的包为准（如 2.54.0）
├── docker-compose.yml   ← 部署编排（已写好官方镜像地址）
├── env.example          ← 配置模板
└── DEPLOY.md             ← 本文件
```

没有源代码。镜像由我们构建好推到私有仓库，你只负责拉下来跑起来。

> 镜像里**没有**：本仓库的 git 历史、前端 TypeScript/Vue 源码、测试代码、运维脚本、构建脚本、开发文档。
>
> 需要如实说明一点：镜像里**有**后端的 Python 代码 —— 服务本身就是 Python 写的，
> 运行时必须有它，任何容器化部署都避不开（同类商业产品同理）。但前端源码、测试、
> 运维脚本、构建脚本、git 历史都不在镜像里。
>
> 关于运维脚本：我们内部自建部署时会用一个带 `scripts/` 的镜像（这样可以在容器里
> 直接跑「造管理员」这类工具）。给客户的镜像是另一个不带脚本的构建，两者由流水线
> 在推送前校验区分。所以你 `docker exec` 进去不会看到 `/app/scripts` —— 这是设计如此，
> 不是镜像不完整；日常运维不需要它，出问题找我们就行。

---

## 二、三步部署

### 第 1 步：装 Docker

服务器需要 Docker Engine 与 Docker Compose v2（`docker compose` 命令，不是 `docker-compose`）。

**Ubuntu / Debian：**

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER
newgrp docker
docker --version && docker compose version
```

**CentOS / Rocky：** 同一条命令即可（脚本会自动识别发行版）。

装完确认这两条命令都能用。Docker 官方文档另有离线安装包，公司内网机器请找我们要。

---

### 第 2 步：登录镜像仓库

镜像在 GitHub Container Registry（ghcr.io），**私有**，只有获得授权的 GitHub 账号能拉。

#### 2.1 生成一个访问令牌（PAT）

在你的 GitHub 账号里：

1. 右上角头像 → **Settings**
2. 左侧最下面 → **Developer settings**
3. **Personal access tokens** → **Tokens (classic)**
4. 点 **Generate new token (classic)**
5. **Note** 随便填，比如 `aetrix-deploy`
6. **Expiration** 选个日期（建议 1 年，到期后重新生成即可，不影响已部署的服务）
7. **勾选权限**：
   - ✅ `read:packages` ← 只需要这一个
   - 其余全部不要勾
8. 点 **Generate token**
9. **立刻复制那串 token**（页面刷新后就再也看不到了）

#### 2.2 登录

```bash
docker login ghcr.io -u <你的GitHub用户名>
# 提示 Password: 粘贴刚才的 token（粘贴时屏幕不显示字符，属正常）
```

看到 `Login Succeeded` 即可。

> ⚠️ 这里必须用**你自己账号的 PAT**，不能用 GitHub Actions 那种自动令牌 ——
> 那类令牌不允许用来登录 Docker。

---

### 第 3 步：配置并启动

```bash
cd aetrix-portal-<版本号>     # 换成你实际解压出来的目录名

# 1) 生成配置文件
cp env.example .env

# 2) 自动生成一把随机密钥写进 .env（生产必须，漏了服务会拒绝启动）
sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')|" .env

# 3) 改掉数据库默认密码（见下方说明，至少改这一个）
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')|" .env

# 4) 确认两行都改到了（下面应输出 2 行 <已设置>，且长度都不是 0）
grep -E '^(SECRET_KEY|POSTGRES_PASSWORD)=' .env | sed 's/=.*/=<已设置>/'
awk -F= '/^SECRET_KEY=|^POSTGRES_PASSWORD=/{print $1, "长度:", length($2)}' .env
# 长度明显大于 1 才算真的换了（模板里 SECRET_KEY 本来是空的、
# POSTGRES_PASSWORD 本来是 aetrix）；若看到长度 0 或 6，说明上面没改到

# 5) 拉镜像并启动
docker compose pull
docker compose up -d
```

> 第 2、3 步的 `sed` 在 macOS 上若报 `invalid command` 或行为异常，用
> `sed -i '' "s|...|...|"`（BSD sed 的 `-i` 要跟参数）。写错了也不会出事：
> 下一步的 `grep` 会直接告诉你有没有改到。
>
> **只改 `POSTGRES_PASSWORD` 就够了**，不要自己动 `DATABASE_URL`
> （`env.example` 里默认没有这一行，它由编排文件根据密码自动拼）。
> 两个一起改、改成不一致的密码，是这套部署最常见的启动失败原因。

首次启动要 1-3 分钟（容器要等数据库、Redis、后端都健康才算就绪）。查看进度：

```bash
docker compose ps          # STATUS 列变成 healthy 就绪了
docker compose logs -f aetrix-api
```

**就绪后浏览器打开** `http://<服务器IP>:8000`
（端口在 `.env` 的 `AETRIX_PORT` 里改，默认 8000。）

> 改端口改 `AETRIX_PORT`。`env.example` 里的 `PORT` / `HOST` 是容器**内部**
> 监听用的，编排文件不读它们 —— 改那里不会有任何效果。

首次进入需要创建管理员账号：打开 `http://<服务器IP>:8000/admin`，第一次打开会被送到
初始化向导，在向导里建第一个管理员（没有默认账号）；向导完成后入口永久关闭。
如果部署时在 `.env` 里设了 `SETUP_BOOTSTRAP_TOKEN`，向导会要求填入它，照填即可。

---

### 必须改的配置

`.env` 里只有两项是**必须**改的，其余保持默认即可跑起来：

| 配置项 | 说明 |
|---|---|
| `SECRET_KEY` | 会话签名密钥。上面第 2 条命令已自动生成。**泄露 = 别人能伪造登录态** |
| `POSTGRES_PASSWORD` | 数据库账号密码。上面第 3 条命令已自动生成 |

其余**全部保持默认即可**，包括这几项容易踩的：

| 配置项 | 为什么不用动 |
|---|---|
| `REDIS_ENABLED` | 保持 `true`。改成 `false` 会让后台扫描进程拒绝启动，服务永远等不到「就绪」 |
| `DATABASE_URL` | 默认不出现这一行，由编排文件根据 `POSTGRES_PASSWORD` 自动拼。手动写容易造成密码不一致 |
| `MEDIA_ROOT` | 不指向真实媒体库也能启动；之后在后台「媒体库管理」里加库更方便 |
| `EMBY_PUBLIC_URL` | 只影响客户端一键导入时的地址提示，不影响启动 |

不确定的项就别动 —— 每个变量上方都有注释说明「改了会发生什么」。

---

## 三、升级到新版本

我们每次发新版会在仓库里更新 `VERSION`，镜像 tag 跟着版本号走（如 `2.54.0`）。

**推荐：锁定版本号**，这样出问题能精确回滚。在 `.env` 里加一行：

```
AETRIX_IMAGE_TAG=<新版本号>
```

然后升级：

```bash
docker compose pull      # 拉指定版本
docker compose up -d    # 原地替换容器
```

数据（数据库、媒体库、配置）都在 volume 里，升级不会丢。

**回滚**就是把 `AETRIX_IMAGE_TAG` 改成旧版本号，再 `pull && up -d` 即可。

> 不写 `AETRIX_IMAGE_TAG` 时默认跟随 `latest`（永远是最新的稳定版）。图省事可以不管，
> 但出问题时就没法一句话回滚了。

### 存储来源与 rclone 配置（现行口径）

后台「存储来源」的类型**由路径前缀决定**，没有类型下拉，只有三种：

| 路径写法 | 来源 |
| --- | --- |
| `/media/...`（任意绝对路径） | 本地硬盘 |
| `115:/...` | 115 网盘 |
| `rclone:...` | rclone 任意后端 |

使用 rclone 时请注意：

1. **rclone 配置由你自己粘贴。** 到后台「服务器与线路」页，编辑对应
   的 EA，在弹窗里的 **rclone 配置** 区把你的 `rclone.conf`（标准 INI 文本）粘
   进去保存；**每台 EA 一份**（落盘为 `rclone-<服务器ID>.conf`）。面板只负责
   落盘并给 rclone 命令加 `--config`，不代管任何凭据、也不回显原文。粘好之前，
   rclone 挂载会连不上（面板会明确报「读不到 rclone.conf」，不会静默失败）。
2. **老数据不动。** 已有的挂载、媒体库、已入库条目都保留；不再支持的
   类型（Google Drive 原生挂载等）不能被新建。库里三张没人读写的遗留表
   （`rclone_remotes` / `service_account_files` / `emby_mount_file_ids`）
   **不自动删**，占点空间而已，**不清也完全不影响功能**。想清理时：

   ```bash
   # 发行镜像里不含 scripts/（客户看不到源码），所以走 SQL：
   docker compose exec postgres psql -U aetrix -d aetrix -c \
     'DROP TABLE IF EXISTS rclone_remotes, service_account_files, emby_mount_file_ids;'

   # 自建部署（镜像带 scripts/，或直接在仓库里跑）可以用带预览的脚本：
   venv/bin/python scripts/drop_removed_mount_tables.py          # 先看会删什么
   venv/bin/python scripts/drop_removed_mount_tables.py --yes    # 确认后真删
   ```

---

## 四、授权权限：怎么给、怎么收

镜像的可见性和拉取权限，都在 **GitHub 的 Package 设置**里操作。

进入方式：
仓库页面 → 右侧栏 **Packages** → 点 `aetrix-api` / `aetrix-web` → 右上角 **Settings**。

### 方案 A：设为 Private + 加仓库协作者（推荐，权限可控）

**给权限：**

1. 回到**仓库**页面（不是 Package 页）→ **Settings** → **Collaborators** → **Add people**
2. 填客户/销售的 GitHub 用户名，确认，给 **Read** 权限即可
3. 对方不需要再配 PAT 权限设置，直接用第 2 步那个 token 登录就能拉到

> 原理：GitHub Packages 里，一个私有包默认跟随**关联仓库**的访问权限。
> 所以能读这个仓库的人，就能拉这个仓库关联的私有包。

**收权限：**

同一个页面（Settings → Collaborators）把人 **Remove** 掉即可。

### 方案 B：设为 Public（最省事，但无权限控制）

Package → Settings → **Change visibility** → 选 **Public**。

改完之后**任何人**都能 `docker pull`，不需要登录。适合「反正要卖出去、藏不住」的阶段，
省掉给每个客户配 token 的麻烦。

### ⚠️ 关于「收回」的真实边界（请务必知悉）

**移除协作者只影响「之后再拉」。已经拉到客户机器上的镜像不会被远程删除。**

也就是说：如果客户之前已经 `docker compose pull` 过，即使你马上撤销授权，
那台机器上的镜像仍然可以继续跑。要真正收回，客户必须自己执行：

```bash
docker compose down
docker image rm ghcr.io/xzb177/aetrix-api:<版本> ghcr.io/xzb177/aetrix-web:<版本>
```

**所以最稳妥的收权方式**：不要把版本锁在 `latest`，而是每份授权单独发一个**带版本号 tag**
的授权凭据，并记录清楚哪些客户装了哪个版本。真要断供时，先改 tag 让客户拉不到新的，
再要求客户清理本地镜像。

### 建议的日常做法

- 每卖出一份授权，单独发一条**只勾 `read:packages`** 的 PAT 给客户
- 在仓库 Collaborators 里记录客户清单
- 不要把包设为 Public，除非你确实想让任何人都能拉

---

## 五、常见问题

**`docker compose up` 报 `RCLONE_RC_USER` 相关错误？**
只在你要用「rclone 挂载」时才需要。现在不用管，也不影响启动。
（授权版 `docker-compose.prod.yml` 里这两个变量默认空，不会因为它起不来。）

**rclone 挂载连不上了？**
面板不代管 `rclone.conf`——到后台「服务器与线路」页，编辑对应的 EA，
在弹窗的 **rclone 配置** 区把你的配置粘进去保存（没有单独的「rclone.conf」页）。
另见上面「三、升级到新版本」里的存储来源口径。

**`/api/health` 打不开 / 容器一直 restarting？**
`docker compose logs aetrix-api`，最常见是 `.env` 里 `SECRET_KEY` 没填或长度不够 32 位。

**日志里出现 `password authentication failed for user "aetrix"`？**
`.env` 里数据库密码被改成了两处不一致的值。两个地方必须同时匹配：
`.env` 的 `POSTGRES_PASSWORD`，以及（如果你自己写了）`DATABASE_URL` 里的密码。
**最省事的修法：删掉 `.env` 里的 `DATABASE_URL` 那一行**（它本来就不用写），
只保留 `POSTGRES_PASSWORD`，重建容器即可。

```bash
# 删掉可能手改坏的 DATABASE_URL，并重置成一致的密码
sed -i '/^DATABASE_URL=/d' .env
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')|" .env
docker compose up -d --force-recreate
```

**`docker compose ps` 里 aetrix-api 一直显示 `starting`，等很久也不变 healthy？**
先确认 `.env` 里 `REDIS_ENABLED` 是 `true`（compose 部署必须如此）。
改成 `false` 后，后台扫描进程会拒绝启动，健康检查永远等不到它需要的心跳，
看起来就像「卡住了」。把它改回 `true`，再 `docker compose up -d --force-recreate`。

**第一次启动要等多久？**
1-3 分钟（要等数据库初始化、Redis、后端三步健康）。
如果超过 5 分钟还没变 healthy，请看上面的重启与密码两条。

**端口被占用？**
改 `.env` 的 `AETRIX_PORT`（比如 8080），然后 `docker compose up -d`。

**数据存在哪？怎么备份？**
数据库在 volume `postgres_data`，rclone 配置在 volume `rclone_data`
（你要用 rclone 挂载时才需要），媒体库是你自己的目录
（`.env` 的 `MEDIA_ROOT`）。备份用：

```bash
docker compose exec postgres pg_dump -U aetrix aetrix > backup-$(date +%F).sql
```

**怎么升级 Docker / 换服务器？**
装好 Docker 后，把 `.env` 和你的媒体库目录拷过去，重新执行第 3 步即可。

**客户机不能上外网 / 内网环境？**
镜像可以从我们这里导出成 tar 包给你（`docker save`），你再 `docker load` 导入。
需要的话联系我们。

---

## 六、需要帮忙时

提供这三样，能让我们快速定位：

```bash
docker compose ps
docker compose logs --tail=200 aetrix-api
cat VERSION 2>/dev/null; grep -E "AETRIX_IMAGE_TAG" .env
```

**不要**把 `.env` 的完整内容发出来 —— 里面是你的密钥。