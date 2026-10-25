# 管理后台权限核查（批 7，M9）

- 核查基线：`main` 顶 `750a27c7d4b2e7a032a5aebcf06eaaed241ca65e`（2026-10-25；开工时为
  `5eef134b`，工作期间主线先后合入批 6（#1319）、重新布局批 2（#1317）、图床镜像、
  SendCommand（#1318），已按新 tip 重排并重验全部行号与计数）。
- 任务来源：审计报告《管理后台后端组织 20261025》M9（角色门禁只在侧边栏，URL 直访靠后端——待核实）。
- 结论先行：**逐口核查发现缺口 5 项（第 6 节），其余 363 口均有鉴权或有意公开。**
  368 口全部定档，不存在“未核查完写成无缺口”的情况。
- 本批只核查＋文档，**未改任何代码**（后端、前端、测试一律不动）；补校验另立批。

## 1. 鉴权机制总述

### 1.1 两条管理员 JWT 依赖链，角色判定集中在一处

- `get_current_admin`（`backend/api/admin_core.py:53`）：Bearer JWT access token →
  `is_staff` → `is_active` → token 新鲜度（jti 吊销＋token_version）→
  `admin_roles.ensure_admin_allowed(request, user)`（`admin_core.py:104`）。
- `require_staff`（`backend/emby_server/portal.py:237`）：经 `_bearer_admin` 调同一
  `get_current_admin`，口径完全一致；另要求 EA 后端可用
 （`ensure_emby_backend_available`）。
- 集中角色门 `backend/admin_roles.py`：`blocked_reason`（`:106`）＋
  `ensure_admin_allowed`（`:130`）。规则：
  - `viewer`（只读审计）：一切写操作拦；
  - 非超管：写方法命中 `SUPER_ONLY_PREFIXES`（`:51`）拦；
  - 路径以后缀 `/rclone-conf` 结尾（`SUPER_ONLY_SUFFIXES`，`:72`）：读写一律只限超管；
  - 其余 GET 读默认放行（“读仍然人人可看”），`rclone-conf` 读是例外
    （2026-10-19 补正：此前只拦写是回归，已恢复成读也拦）。

超管写禁前缀（`admin_roles.py:51` 起，当前 tip 实查）：

| 前缀 | 对应能力 |
|---|---|
| `/api/admin/admins` | 管理员与权限本身 |
| `/api/admin/economy/settings` | 系统／经济设置 |
| `/api/admin/playback/policy` | 播放与客户端策略 |
| `/api/admin/capabilities` | 能力中心：上游 Key |
| `/api/admin/share-guard` | 防共享 |
| `/api/admin/access-guard` | 访问拦截 |
| `/api/admin/api-keys` | API Key／Service Token（写口；GET 读不受影响） |

### 1.2 共用 `admin_router` 没有统一依赖，兜底只在五处路由级依赖

- `admin_router`（`backend/api/admin_core.py:38`）创建时**未传 `dependencies`**：
  `APIRouter(prefix="/api/admin", tags=["管理后台"])`。凡挂它的口，鉴权全靠逐口
  `Depends(get_current_admin)`。实抽结果：除登录口（有意公开）外，挂它的口逐口
  均有该依赖，无一遗漏。
- 路由级已兜底的五处（凡路由级已兜底的口，定档时计入依赖，不误标为缺口）：
  - `admin_emby_router`（`backend/emby_server/portal.py:260`）：
    `dependencies=[Depends(require_staff)]`，覆盖全部 `/api/admin/emby/*`；
  - `library_cover` 路由（`backend/api/library_cover.py:41` 起，路由级 `require_staff`）；
  - `admin_artwork` 路由（`backend/api/admin_artwork.py:23` 起，路由级 `require_staff`）；
  - `library_order` 路由（`backend/api/library_order.py:21` 起，路由级 `require_staff`）；
  - `mount_picker_router`（`backend/emby_server/portal_mount_routes.py:524` 起，
    路由级 `require_staff`，覆盖 `/api/admin/mounts/*` 两口）。
- `backend/main.py` 的全部 `include_router` 均未传 `dependencies`／`prefix` 覆盖
  （grep 实查）；EA 专属 4 口挂在 `emby_api/main.py:529,531`（见第 5 节）。

### 1.3 不走管理员 JWT 的鉴权（C 档口的替代鉴权）

- 节点密钥 `require_panel_key`（`backend/emby_server/mount_health.py:80`）：
  节点签名头或静态节点密钥头（常量时间比较；SECRET_KEY 原文不再被接受）。
- API Key：`api_key_core.require_api_key("read")`（第三方自测用，不走管理员 JWT）。
- 流节点部署包 HMAC 签名头：`node_auth.verify_signed_headers`
  （`backend/emby_server/stream_nodes.py:242` 起；时效 ±5 分钟＋nonce 防重放＋
  绑定 method＋path；静态 panel key 已拒）。
- 首次运行向导 bootstrap（`backend/api/setup.py`）：完成后永久 403、限流、
  设置 `SETUP_BOOTSTRAP_TOKEN` 时须带一致 token。

## 2. 核查方法与口径

- 枚举方法：AST 静态解析 `backend/api/*.py` 与 `backend/emby_server/*.py` 全部
  路由装饰器，解析路由前缀、路由级依赖、装饰器依赖、签名 `Depends`；
  再按第 1 节的集中门规则定档。EM＋EA 合计 **368 个路由定义**。
- 口径对照旧数：旧审计在册 351（定义口径，含已删影子口 4 个重复定义）＋
  范围外 12＝363；本轮 368＝在册 356＋范围外 12。变化四项：
  - 影子口文件 `backend/api/welfare_requests.py` 已删（−4 定义；同路径现仅
    `welfare_admin` 版 single 定义，运行时不再有遮蔽）；
  - 4 条旧死口候选已删（−4）：`POST /api/admin/emby/scrape/enrich/retry-unmatched`、
    `POST /api/admin/emby/scrape/enrich/requeue-inherit-children`、
    `GET /api/admin/emby/scrape/probe-progress`、
    `GET /api/admin/emby/scrape/scheduler/status`（当前 tip 全仓 grep 零命中）；
  - 新增在册 13 口：合集封面 5（`media-library/artwork-overrides` 3＋
    `cover-strategy` 2）、库设序 2（`emby/libraries/order` GET＋PUT）、
    上游聚合／苹果 CMS 同步 2（`upstream-sources/{id}/aggregate`、
    `upstream-sources/{id}/sync-applecms`）、播放路径策略 2
    （`playback/stream-path-policies/import`、`seed-templates`）、
    图床镜像 2（`media-library/cover-mirrors` GET＋POST retry，新 tip 合入，
    均路由级 `require_staff`，B 档）；
  - 范围外 12 口现全部并入全表：流节点 4、播放总线 4、挂载体检 1、节点 3。
- 三档定义：
  - **A 超管校验有**：走管理员 JWT 链，且（写方法命中 `SUPER_ONLY_PREFIXES`）
    或（路径以 `/rclone-conf` 结尾，不分读写）。
  - **B 仅管理员校验**：走管理员 JWT 链（含路由级兜底），不命中超管门。
  - **C 未见管理员校验**：无 `get_current_admin`／`require_staff` 链；**全部 12 口
    均已人工读 handler 与依赖链复核**（见第 5 节），有替代鉴权或有意公开的
    不算缺口，不只凭 grep 判死。
- 枚举盲点与兜底：AST 认不出工厂式 `Depends`（verify 口的
  `require_api_key("read")`）与函数体内调鉴权（流节点口的 `_panel_auth`），
  这两类靠 C 档人工复核兜底；全仓 `Depends(` 首次参数普查确认，除上述一口外，
  管理面再无其他工厂式依赖。
- 计数：**A 16＋B 340＋C 12＝368**，与下表行数相符（A 表 16 行、C 表 12 行、
  B 分组 34 组求和 340）。

## 3. A 档表：超管校验有（16 口，逐口列）

| 方法 | 路径 | 文件：行号 | 门控依据 |
|---|---|---|---|
| PUT | `/api/admin/access-guard/policy` | `backend/api/admin_ops.py:850` | 写命中 access-guard 前缀 |
| POST | `/api/admin/access-guard/preview` | `backend/api/admin_ops.py:875` | 写方法命中前缀（本身无副作用，属从严，见第 7 节） |
| POST | `/api/admin/admins` | `backend/api/admins_admin.py:138` | 写命中 admins 前缀 |
| PATCH | `/api/admin/admins/{user_id}` | `backend/api/admins_admin.py:192` | 写命中 admins 前缀 |
| DELETE | `/api/admin/admins/{user_id}` | `backend/api/admins_admin.py:245` | 写命中 admins 前缀 |
| POST | `/api/admin/api-keys` | `backend/api/admin_apikey.py:88` | 写命中 api-keys 前缀 |
| PUT | `/api/admin/api-keys/{key_id}` | `backend/api/admin_apikey.py:127` | 写命中 api-keys 前缀 |
| DELETE | `/api/admin/api-keys/{key_id}` | `backend/api/admin_apikey.py:165` | 写命中 api-keys 前缀 |
| PUT | `/api/admin/capabilities/{slug}` | `backend/api/capabilities_admin.py:76` | 写命中 capabilities 前缀 |
| POST | `/api/admin/capabilities/{slug}/test` | `backend/api/capabilities_admin.py:96` | 写命中 capabilities 前缀 |
| PUT | `/api/admin/economy/settings` | `backend/api/admin_economy.py:339` | 写命中 economy/settings 前缀 |
| PUT | `/api/admin/playback/policy` | `backend/api/playback_admin.py:89` | 写命中 playback/policy 前缀 |
| PUT | `/api/admin/share-guard/policy` | `backend/api/admin_ops.py:787` | 写命中 share-guard 前缀 |
| POST | `/api/admin/share-guard/purge` | `backend/api/admin_ops.py:811` | 写命中 share-guard 前缀 |
| GET | `/api/admin/servers/{server_id}/rclone-conf` | `backend/api/servers.py:756` | 后缀 rclone-conf，读也拦 |
| POST | `/api/admin/servers/{server_id}/rclone-conf` | `backend/api/servers.py:780` | 后缀 rclone-conf |

## 4. B 档表：仅管理员校验（340 口，分组列）

| 文件 | 口数 | 依赖链 | 备注 |
|---|---|---|---|
| `backend/api/admin_scrape.py` | 54 | 路由级 `require_staff` | 含刮削上游密钥写口（见第 7 节待定性） |
| `backend/api/admin.py` | 50 | 逐口 `get_current_admin` | 含 PUT users/{id} 字段级超管护栏（`:389` 非超管改他人 staff 标志受限；`:431-437` 动超管号受限） |
| `backend/emby_server/portal.py` | 34 | 路由级 `require_staff` | |
| `backend/api/admin_ops.py` | 24 | 逐口 `get_current_admin` | 含 login-logs purge（缺口，见第 6 节）；另 4 口在 A 档 |
| `backend/api/welfare_admin.py` | 18 | 逐口 `get_current_admin` | 影子口已 single 定义 |
| `backend/api/admin_economy.py` | 17 | 逐口 `get_current_admin` | 含 hotlink／dedup 写口（缺口）；另 1 口在 A 档 |
| `backend/api/media_sets_api.py` | 15 | 逐口 `get_current_admin` | 另有 `COLLECTIONS_ENABLED` 总闸（关时惰性 404）——是功能闸不是鉴权，定档仍按依赖链计 B |
| `backend/api/servers.py` | 13 | 逐口 `get_current_admin` | 另 2 口在 A 档 |
| `backend/emby_server/portal_mount_routes.py` | 12 | 路由级 `require_staff`（10 口）＋mount_picker 路由级（2 口） | 含 rclone/conf POST（缺口） |
| `backend/api/admin_upstream_sources.py` | 9 | 路由级 `require_staff` | 含新增 aggregate／sync-applecms |
| `backend/api/coupons_admin.py` | 8 | 逐口 `get_current_admin` | |
| `backend/api/realms.py` | 8 | 逐口 `get_current_admin` | |
| `backend/api/admin_alerts.py` | 7 | 逐口 `get_current_admin` | |
| `backend/api/orders_admin.py` | 6 | 逐口 `get_current_admin` | |
| `backend/api/admin_media_governance.py` | 6 | 路由级 `require_staff` | |
| `backend/api/admin_identify_review.py` | 5 | 路由级 `require_staff` | |
| `backend/api/admin_artwork.py` | 7 | 路由级 `require_staff` | 含新 tip 新增图床镜像 2 口（cover-mirrors GET＋POST retry） |
| `backend/api/playback_admin.py` | 5 | 逐口 `get_current_admin` | 含 stream-path-policies 2 口（待定性）；policy PUT 在 A 档 |
| `backend/api/admin_readonly.py` | 5 | 逐口 `get_current_admin` | 全 GET |
| `backend/api/emby_servers.py` | 4 | 逐口 `get_current_admin` | |
| `backend/api/panel.py` | 4 | 逐口 `get_current_admin` | 面板 admin_router 4 口 |
| `backend/emby_server/playback_bus_admin.py` | 4 | 逐口 `get_current_admin` | |
| `backend/api/admin_services.py` | 4 | 逐口 `get_current_admin` | |
| `backend/api/admin_auth.py` | 3 | 逐口 `get_current_admin` | me、改密、登出；登录口在 C 档 |
| `backend/api/admin_diagnosis.py` | 3 | 逐口 `get_current_admin` | |
| `backend/api/reminders_admin.py` | 3 | 逐口 `get_current_admin` | |
| `backend/api/library_cover.py` | 3 | 路由级 `require_staff` | |
| `backend/api/capabilities_admin.py` | 2 | 逐口 `get_current_admin` | 2 口均为 GET；写口在 A 档 |
| `backend/api/library_order.py` | 2 | 路由级 `require_staff` | |
| `backend/api/admins_admin.py` | 1 | 逐口 `get_current_admin` | GET 清单；写口在 A 档 |
| `backend/api/admin_apikey.py` | 1 | 逐口 `get_current_admin` | GET 清单；写口在 A 档，verify 在 C 档 |
| `backend/api/admin_dashboard.py` | 1 | 逐口 `get_current_admin` | |
| `backend/api/admin_tasks.py` | 1 | 逐口 `get_current_admin` | |
| `backend/api/admin_user_grants.py` | 1 | 逐口 `get_current_admin` | |
| 合计 | **340** | | |

B 档补充说明（不是缺口）：

- 超管前缀下的 GET 读全部在 B 档（admins 清单、api-keys 清单、capabilities
  2 口、playback/policy、share-guard、access-guard、economy/settings 的 GET），
  按 `admin_roles` 设计“读仍然人人可看”，不算缺口；viewer 读全部、写全拦。
- 除登录口外，`admin_router` 挂载口逐口均有 `get_current_admin`，
  管理员 JWT 链无一整口遗漏（A＋B 合计 356 口）。

## 5. C 档表：未见管理员校验（12 口，逐口人工复核）

| 方法 | 路径 | 文件：行号 | 人工复核结论（替代鉴权／有意公开） |
|---|---|---|---|
| POST | `/api/admin/auth/login` | `backend/api/admin_auth.py:49` | 有意公开（登录本身）；限流 8 次／60 秒＋人机验证＋仅 `is_staff` 可登＋登录日志。非缺口 |
| GET | `/api/admin/setup/status` | `backend/api/setup.py:165` | 有意公开；仅返回 `setup_completed` 布尔值，无敏感数据。非缺口 |
| POST | `/api/admin/setup` | `backend/api/setup.py:179` | bootstrap；完成后永久 403、限流、设 `SETUP_BOOTSTRAP_TOKEN` 时须带一致 token。非缺口 |
| GET | `/api/admin/api-keys/verify` | `backend/api/admin_apikey.py:180` | API Key 鉴权：`Depends(api_key_core.require_api_key("read"))`，第三方自测用，不走管理员 JWT；AST 认不出工厂式 Depends，人工复核补获。非缺口 |
| GET | `/api/admin/mounts/health` | `backend/emby_server/mount_health.py:244` | EA 挂载；装饰器 `require_panel_key`。非缺口 |
| GET | `/api/admin/nodes/me` | `backend/emby_server/nodes.py:358` | EA；装饰器 `require_panel_key`。非缺口 |
| GET | `/api/admin/nodes/libraries` | `backend/emby_server/nodes.py:378` | EA；装饰器 `require_panel_key`。非缺口 |
| POST | `/api/admin/nodes/libraries/{library_id}/scan` | `backend/emby_server/nodes.py:384` | EA；装饰器 `require_panel_key`。非缺口 |
| GET | `/api/admin/stream-nodes` | `backend/emby_server/stream_nodes.py:213` | 体内 `_panel_auth`（即 `require_panel_key`）。非缺口 |
| POST | `/api/admin/stream-nodes/register` | `backend/emby_server/stream_nodes.py:221` | 体内 `_panel_auth`。非缺口 |
| DELETE | `/api/admin/stream-nodes` | `backend/emby_server/stream_nodes.py:236` | 体内 `_panel_auth`。非缺口 |
| GET | `/api/admin/stream-nodes/bundle` | `backend/emby_server/stream_nodes.py:242` | 只认 HMAC 签名头（`verify_signed_headers`），静态 panel key 已拒；下发物含 SECRET_KEY／数据库地址／rclone.conf／SA 文件是设计使然（部署脚本拉取用），本批只核查鉴权有无，不评价下发物范围。非缺口 |

复核方法：12 口 handler 与依赖链逐一人工读过（登录、向导、verify、panel_key、
bundle HMAC、`_panel_auth`），不是只凭 grep 定档。**C 档 12 口全部另有鉴权或
有意公开，无“无任何鉴权”的口。**

## 6. 缺口清单（5 项）

| # | 接口 | 文件：行号 | 影响一句 | 违背的口径 |
|---|---|---|---|---|
| 1 | POST `/api/admin/emby/mounts/rclone/conf` | `backend/emby_server/portal_mount_routes.py:325` | 运营角色可整份覆盖 rclone.conf（含全部网盘 token／secret） | `admin_roles`“凭据类仅超管可读写”及 servers 版 rclone-conf 读写双拦（`servers.py:756,780`）；根因是后缀名单只写了 `/rclone-conf`，没覆盖 `/rclone/conf` 这一路径形状 |
| 2 | PUT `/api/admin/settings/registration` | `backend/api/admin.py:669` | 运营角色可在开放／关闭注册之间切换并改注册限流 | Settings 页 superOnly 标记＋“进去也没用”提示（`Layout.vue:87,208,293`）及 `admin_roles`“运营改不了系统设置” |
| 3 | PUT `/api/admin/hotlink-config` | `backend/api/admin_economy.py:434` | 运营角色可开关防盗链总开关、改播放签名 TTL | Settings 页 superOnly 标记（同上） |
| 4 | POST `/api/admin/login-logs/purge` | `backend/api/admin_ops.py:697` | 运营角色可按天清理登录日志，`days=0` 直接清空全部登录审计记录 | Settings 危险操作页 superOnly 标记；审计轨迹可被非超管抹除 |
| 5 | PUT `/api/admin/items-dedup-cache-config` | `backend/api/admin_economy.py:482` | 运营角色可改列表去重缓存开关／TTL／上限 | Settings 页 superOnly 标记与后端放行不一致；敏感度低（仅缓存调优），列入清单备补批一并收敛 |

未列入缺口的反向证据：其余五处 superOnly 页（admins、client-policy、
share-guard、access-guard、api-keys）的写口全部在 A 档；同路径
GET `/api/admin/emby/mounts/rclone/conf`（`portal_mount_routes.py:305`）仅回
remote 名、不回明文，本批不列为缺口（补批可按 servers 口径一并收敛读拦）。

## 7. 口径待业务方定性（不算确认缺口，补批前需一句话）

- 刮削上游密钥类写口（B 档）：TMDB 密钥 PUT／POST add／DELETE／cooldown reset
  （`admin_scrape.py:118,240,271,295`）、多源密钥 POST／DELETE／reset
  （`:709,742,778`）、豆瓣配置 PUT（`:1835`）、备用链路限速 PUT（`:1886`）、
  兜底开关 PUT（`:1928,1956`）。`admin_roles` 称超管含“上游 Key（能力中心）”，
  括号限定在能力中心；刮削页密钥是否同属超管范围，文字有歧义，本批不擅自
  定为缺口。
- 播放路径策略 import／seed（`playback_admin.py:109,129`）：播放策略族新口，
  前端无调用方；超管前缀只写到 `/api/admin/playback/policy`，没覆盖
  `/api/admin/playback/stream-path-policies/*`；是否纳入超管范围需业务方定。
- access-guard preview POST 在 A 档：本身无副作用（只试跑样本），因写方法命中
  前缀被集中门拦下——属从严而非缺口；是否放宽由补批定。
- 超管前缀下 GET 可读：设计如此（“读仍然人人可看”），不算缺口；若业务方
  要求读也拦，需另立批改集中门（rclone-conf 读拦是既有先例）。

## 8. 前端两项核对结果

- `admin_frontend/src/router/index.ts` 的 `beforeEach`（`:153-181`）：setup 分流
  （`:157-163`）→ `ensureSession`（`:171`）→ `requiresAuth` 与 `isAuthenticated`
  判断（`:173-178`）；**全文无任何角色／superOnly 判断，仍只校验登录与 setup
  状态**，与审计 M9 一致。
- `admin_frontend/src/views/Layout.vue`：`isSuper`（`:85`）仍为
  `auth.admin?.is_super !== false`，字段缺失默认按超管算；superOnly 六页仍为
  client-policy（`:137`）、share-guard（`:139`）、access-guard（`:141`）、
  admins（`:207`）、settings（`:208`）、api-keys（`:218`）；锁标（`:509`）与
  命令面板提示（`:293`，`SUPER_ONLY_HINT` 在 `:87`，文案“需要超级管理员角色：
  可以查看，保存会被服务端拒绝”）**仍只作锁标与提示、不拦截跳转**。
  注意：该提示对第 6 节所列 Settings 页写口不成立（运营保存不会被拒绝）——
  补批修后端时应同步复核提示文案。
- **isSuper 缺省未定事项**：字段缺失默认按超管算是否为有意兜底，**未定，
  需业务方一句话**，本批绝不擅自改缺省值。字段来源：
  GET `/api/admin/auth/me` 经 `permission_payload`（`admin_roles.py:152`）
  恒返回 `is_super`；缺省分支只在 admin 对象无该字段时触发。

## 9. 抽查方法与结果

- 全量枚举：AST 解析 368 个路由定义；三档计数程序求和 16＋340＋12＝368，
  与表行数相符。
- 分层抽查 21 口，依赖行在当前 tip 逐口 grep／sed 核对，全部命中：
  路由锚点 6 口（`admin_core.py:38` router 无 dependencies；
  `portal.py:260` admin_emby_router 路由级 `require_staff`；
  `library_cover.py:41` 起、`admin_artwork.py:23` 起、`library_order.py:21` 起、
  `portal_mount_routes.py:524` 起四处路由级 `require_staff`）；
  逐口依赖 9 口（`admin.py:669`、`admin_economy.py:434`、
  `admin_ops.py:697`、`admins_admin.py:138`、`capabilities_admin.py:76`、
  `playback_admin.py:89`、`playback_admin.py:109`、`servers.py:780`、
  `media_sets_api.py:141` 的 `get_current_admin`）；
  路由级兜底生效 2 口（`admin_upstream_sources.py:101`、
  `admin_scrape.py:118` 的 `require_staff` 来自路由级）；
  特殊鉴权 4 口（`admin_apikey.py:181` 工厂式 API Key 依赖；
  `setup.py:179` 体内 bootstrap 守卫；`stream_nodes.py:213` 体内 `_panel_auth`；
  `stream_nodes.py:242` 体内 HMAC 验签）。
- C 档 12 口全部人工读 handler 定档（见第 5 节）。
- 文档各节均已写实，无空段留空；凡“未见校验”标签均有人工复核记录。
- CI 全量未跑：文档 PR 无代码改动，PR 描述中写清。

## 10. 声明与未做项

- **本批未改任何代码，补校验另立批**；回滚＝revert 本 PR。
- 未做项及原因：
  - 补后端超管校验（第 6 节 5 项）：属行为改动，按任务边界另立补后端批，
    本批不修；
  - isSuper 缺省定性：业务方未定，需一句话，本批不擅自改值；
  - 第 7 节待定性项的裁定：需业务方定超管范围口径，本批只列出、不擅自定；
  - CI 全量：文档 PR 本无须跑代码测试，PR 描述写清即可。
