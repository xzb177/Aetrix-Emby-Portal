# 批 4：无前端调用管理端接口定性（M20＋M3＋M4）

> 基线：GitHub main `2778c5dffedf269ee94bbb4244ec728f824e83e2`（2026-10-25 晚，含 PR #1300 批 3 与 PR #1302 死路由删除）。
> 本批只定性＋文档，**未删任何接口、未改任何代码、未做任何接线施工、未动生产**。
> 本基线下待删候选为 0 条（原因见第 3 节）；如未来再出现待删候选，删除须用户确认无运维手工调用后另批处理，本批不删。

## 1. 结论先行

| 口径 | 数量 |
|---|---|
| 后端管理端路由（机器抽取，全部 `/api/admin` 前缀） | 366 口 |
| 其中审计口径内（in-scope） | 352 口 |
| 其中审计口径外（见第 2 节，不计入总数） | 14 口 |
| 前端去重调用路径（管理站＋用户站） | 233 条 |
| **无前端调用清单（本表行数）** | **58 条记录** |
| 其中去重方法＋路径 | 58 个 |
| 待接线 | 42 条 |
| 运维专用 | 16 条 |
| 待删候选 | 0 条 |

三类计数与下表行数核对一致：42＋16＋0＝58。

标签只有三类，含义如下：

- **待接线**：是真实能力，只是管理前端还没有页面或动作入口；不删，留待接线立项。
- **运维专用**：没有页面是合理的，属运维手工、应急或批量操作入口；不删，不必强行进页面。
- **待删候选**：仅作候选标记，**本批不删**；删除须用户确认无运维手工调用后另批处理。

六处皆零是待删候选的必要条件，不是充分条件：凡属已知能力（M3 合集、M4 治理、新落地能力、运维动作），即使字符串复核皆零也不贴待删候选，只贴待接线或运维专用。

## 2. 方法与范围口径

### 2.1 后端抽取

- 扫描 `backend/` 全部 `.py` 文件的路由装饰器（`@<router>.get/post/put/patch/delete("...")`），按各模块的 `APIRouter(prefix=…)` 及共享路由器（`admin_router`＝`/api/admin`、`admin_emby_router`＝`/api/admin/emby`）还原完整路径；空路径 `""` 即前缀本身，同样计入。
- 挂载核查：`backend/main.py` 的 `include_router` 均无附加前缀，装饰器还原即为真实路径。

### 2.2 前端抽取

- 管理站：以视图、store、组件实际 `import` 的 `@/api/*` 封装函数为准（不是“封装定义了就算”），再按封装函数还原真实请求路径；同时计入绝对路径直调（如 store 里直接调 `GET /api/admin/auth/me`）。
- 封装还原处理了三类易漏写法：动态导入（store 里 `import('@/api/admin').then(({ adminLogout }) => …)`）、裸常量调用（`get(R)`、`get(S)`，其中 `R='/realms'`、`S='/servers'`、`E='/emby'`）、查询三元拼接（`…/scan${full ? '?full=true' : ''}` 按查询串切分，不产生假路径）。
- 用户站：只计绝对路径 `/api/admin…` 直调，实查为 0 条；用户站封装调用走 `/api/user` 等前缀，不计入管理口。
- 归一化：路径模板参数统一写成 `{param}`（如 `{item_id}`、`{id}` 均归一为 `{param}`）；查询串省略。

### 2.3 对账规则

- 按**路径**求差（不分方法），与审计报告 M20 的方法一致：某路径只要任一方法被前端调用，该路径即不算无前端调用。
- 另有 7 条“同路径、不同方法”的记录（例如批量写口只被前端以单条方式调用），按路径口径不计入本表，留待另行核查，不在本批定性内。
- 字符串对账的局限照实写：动态拼接、别名调用可能藏起真实调用方。凡表内证据写“存疑”的，一律不贴待删候选。

### 2.4 范围外 14 口（不计入总数）

审计口径范围外，在当前 tip 为 14 口：stream-nodes 4 口（列表、注册、注销、一键部署包）、playback-bus 4 口（命令写入、单条命令、命令列表、快照）、mount-health 1 口（挂载体检）、nodes 3 口（本机、库列表、库扫描）、mounts 路径选择 2 口（本地目录、浏览）。按路径计，其中 5 口有前端调用（playback-bus 命令列表与快照、mounts 本地目录与浏览，命令写入与列表同路径），其余 9 口无前端调用；但它们属机间协议或探针口径，不在本批定性范围内，本表不列。

## 3. 与旧清单的增减，以及本窗口内他批的删除

### 3.1 同脚本对比（可复算的口径）

- 上一基线 `6d17c6be`（本批开工时）同脚本重算：65 条记录。
- 当前基线 `2778c5df`：58 条记录。变化为 **-8 口删除、+1 口新增**，净 -7 口：
  - 删除 8 口（见 3.2 节，皆由他批删除，不是本批所删）。
  - 新增 1 口：`POST /api/admin/playback/stream-path-policies/seed-templates`（PR #1294 种子角色模板播种口）。
  - 其余 57 口与上一基线完全重合，无一口因前端接线而退出清单。

### 3.2 本窗口内已由他批删除的 8 条（不计入表内）

本批开工后、定稿前，main 前进了两批合并，直接改变了清单构成，照实记录：

| 口 | 删除方 | 说明 |
|---|---|---|
| POST /api/admin/emby/scrape/enrich/retry-unmatched | PR #1302（死路由删除） | 任务书中“先前死代码审计的 4 条路由候选”之一；已删除，本批不再标候选。 |
| POST /api/admin/emby/scrape/enrich/requeue-inherit-children | PR #1302（死路由删除） | 同上。 |
| GET /api/admin/emby/scrape/probe-progress | PR #1302（死路由删除） | 同上。 |
| GET /api/admin/emby/scrape/scheduler/status | PR #1302（死路由删除） | 同上。 |
| GET /api/admin/welfare/requests（影子版） | PR #1300（批 3 影子口下线） | 审计 M2 的影子口；批 3 已删，现行 welfare_admin 版 4 口仍在清单内（见 6.1 节）。 |
| POST /api/admin/welfare/requests/{param}/approve（影子版） | PR #1300（批 3 影子口下线） | 同上。 |
| POST /api/admin/welfare/requests/{param}/reject（影子版） | PR #1300（批 3 影子口下线） | 同上。 |
| POST /api/admin/welfare/requests/{param}/done（影子版） | PR #1300（批 3 影子口下线） | 同上。 |

因此：任务书中“4 条路由候选只标待删候选、绝不删”的要求，在当前基线下已无标的对象——它们已被 #1302 删除，本批不重复主张、不复活、不再列入表内。当前基线下没有符合“六处皆零＋无已知能力归属”的待删候选，故待删候选为 0 条；本批不把其他口升格为待删候选。

### 3.3 与审计报告 61 口的关系

审计报告旧清单（旧 tip `166a9073`）为 61 口（去重口径）。本轮同一脚本在旧快照上复跑为 58 个去重口，差额源于抽取口径（本脚本额外识别了动态导入、裸常量调用、查询三元拼接三类调用形式），不解读为“已下线”。当前清单与审计清单的关系以 3.1 节的同脚本对比为准；审计清单中点名的 8 口（4 条死口＋4 条影子口）已确认由 #1302/#1300 删除，其余审计口仍在清单内。

## 4. M3：合集域定性（只定性，不删不施工）

合集域 15 条记录（12 条合集＋3 条模板，`backend/api/media_sets_api.py`），总闸 `COLLECTIONS_ENABLED` 默认关、管理前端无合集页，全部定性为**待接线**，见第 6.1 节第 17–31 行。接线（页面＋开闸）另立项，本批不动。

## 5. M4：可见性治理定性（只定性，不删不施工）

可见性治理 6 条记录（`backend/api/admin_media_governance.py`），管理前端无治理页，全部定性为**待接线**，见第 6.1 节第 6–11 行。治理任务自动入队等后续立项时一并给页面，本批不动。

## 6. 逐口表

表内路径已归一化为 `{param}`；模块行号为装饰器所在行；证据均为一句话。

### 6.1 待接线（42 条）

| # | 接口 | 模块：行号 | 标签 | 证据 |
|---|---|---|---|---|
| 1 | GET /api/admin/media/versions/{param} | backend/api/admin.py:2337 | 待接线 | 两站前端路径集合无命中；多版本查看能力暂无版本页。 |
| 2 | POST /api/admin/media/versions/{param}/unmerge | backend/api/admin.py:2370 | 待接线 | 两站前端路径集合无命中；解除单版本合并暂无版本页。 |
| 3 | POST /api/admin/media/versions/{param}/unmerge-all | backend/api/admin.py:2395 | 待接线 | 两站前端路径集合无命中；解除全部版本合并暂无版本页。 |
| 4 | POST /api/admin/media/episode-versions/merge | backend/api/admin.py:2419 | 待接线 | 两站前端路径集合无命中；手动触发单集多版本合并暂无版本页。 |
| 5 | GET /api/admin/economy/subscriptions | backend/api/admin_economy.py:872 | 待接线 | 两站前端路径集合无命中；订阅页走按服清单口，未调全局清单口。 |
| 6 | GET /api/admin/emby/visibility-governance/tasks | backend/api/admin_media_governance.py:53 | 待接线 | 两站前端路径集合无命中；M4 治理任务清单，无治理页。 |
| 7 | POST /api/admin/emby/visibility-governance/tasks/{param}/approve | backend/api/admin_media_governance.py:75 | 待接线 | 两站前端路径集合无命中；M4 治理准入动作，无治理页。 |
| 8 | POST /api/admin/emby/visibility-governance/tasks/{param}/reject | backend/api/admin_media_governance.py:93 | 待接线 | 两站前端路径集合无命中；M4 治理驳回动作，无治理页。 |
| 9 | GET /api/admin/emby/visibility-governance/events | backend/api/admin_media_governance.py:111 | 待接线 | 两站前端路径集合无命中；M4 治理事件清单，无治理页。 |
| 10 | GET /api/admin/emby/library-scan-presets/{param} | backend/api/admin_media_governance.py:143 | 待接线 | 两站前端路径集合无命中；M4 库扫描预设读取，无治理页。 |
| 11 | PUT /api/admin/emby/library-scan-presets/{param} | backend/api/admin_media_governance.py:160 | 待接线 | 两站前端路径集合无命中；M4 库扫描预设写入，无治理页。 |
| 12 | PUT /api/admin/promotion/policy | backend/api/admin_ops.py:1354 | 待接线 | 两站前端路径集合无命中；邀请页只读推广配置，未调策略写入口。 |
| 13 | GET /api/admin/emby/scrape/items/{param} | backend/api/admin_scrape.py:1719 | 待接线 | 两站前端路径集合无命中；元数据源页用预览与搜索口，未调条目详情口。 |
| 14 | POST /api/admin/emby/upstream-sources/{param}/sync-applecms | backend/api/admin_upstream_sources.py:199 | 待接线 | 两站前端路径集合无命中；上游页只做源管理，未调苹果 CMS 同步口。 |
| 15 | POST /api/admin/emby/upstream-sources/{param}/aggregate | backend/api/admin_upstream_sources.py:255 | 待接线 | 两站前端路径集合无命中；上游聚合动作，后端已接线，前端页未接。 |
| 16 | GET /api/admin/economy/coupons/{param}/usages | backend/api/coupons_admin.py:280 | 待接线 | 两站前端路径集合无命中；优惠券页只查全部使用记录，未调按券查询口。 |
| 17 | POST /api/admin/media-sets | backend/api/media_sets_api.py:140 | 待接线 | 两站前端路径集合无命中；M3 合集新建，无合集页，总闸默认关。 |
| 18 | GET /api/admin/media-sets | backend/api/media_sets_api.py:162 | 待接线 | 两站前端路径集合无命中；M3 合集清单，无合集页，总闸默认关。 |
| 19 | GET /api/admin/media-sets/{param} | backend/api/media_sets_api.py:171 | 待接线 | 两站前端路径集合无命中；M3 合集详情，无合集页，总闸默认关。 |
| 20 | PATCH /api/admin/media-sets/{param} | backend/api/media_sets_api.py:188 | 待接线 | 两站前端路径集合无命中；M3 合集修改，无合集页，总闸默认关。 |
| 21 | DELETE /api/admin/media-sets/{param} | backend/api/media_sets_api.py:219 | 待接线 | 两站前端路径集合无命中；M3 合集删除，无合集页，总闸默认关。 |
| 22 | POST /api/admin/media-sets/order | backend/api/media_sets_api.py:233 | 待接线 | 两站前端路径集合无命中；M3 合集设序，无合集页，总闸默认关。 |
| 23 | POST /api/admin/media-sets/{param}/members | backend/api/media_sets_api.py:246 | 待接线 | 两站前端路径集合无命中；M3 合集成员手工挑，无合集页，总闸默认关。 |
| 24 | DELETE /api/admin/media-sets/{param}/members/{param} | backend/api/media_sets_api.py:273 | 待接线 | 两站前端路径集合无命中；M3 合集成员移除，无合集页，总闸默认关。 |
| 25 | POST /api/admin/media-sets/{param}/members/order | backend/api/media_sets_api.py:290 | 待接线 | 两站前端路径集合无命中；M3 合集成员设序，无合集页，总闸默认关。 |
| 26 | POST /api/admin/media-sets/{param}/rules | backend/api/media_sets_api.py:309 | 待接线 | 两站前端路径集合无命中；M3 合集规则新增，无合集页，总闸默认关。 |
| 27 | DELETE /api/admin/media-sets/{param}/rules/{param} | backend/api/media_sets_api.py:334 | 待接线 | 两站前端路径集合无命中；M3 合集规则删除，无合集页，总闸默认关。 |
| 28 | POST /api/admin/media-sets/{param}/refresh | backend/api/media_sets_api.py:351 | 待接线 | 两站前端路径集合无命中；M3 合集刷新，无合集页，总闸默认关。 |
| 29 | POST /api/admin/media-set-templates | backend/api/media_sets_api.py:363 | 待接线 | 两站前端路径集合无命中；M3 合集模板新建，无合集页。 |
| 30 | GET /api/admin/media-set-templates | backend/api/media_sets_api.py:386 | 待接线 | 两站前端路径集合无命中；M3 合集模板清单，无合集页。 |
| 31 | POST /api/admin/media-set-templates/{param}/instantiate | backend/api/media_sets_api.py:394 | 待接线 | 两站前端路径集合无命中；M3 模板实例化生成合集，无合集页。 |
| 32 | POST /api/admin/servers/{param}/ops/scan | backend/api/servers.py:873 | 待接线 | 两站前端路径集合无命中；服务器页只读运维快照，未调运维扫描口（存疑：同前缀多口，按精确路径区分）。 |
| 33 | GET /api/admin/welfare/users | backend/api/welfare_admin.py:32 | 待接线 | 两站前端路径集合无命中；公益用户专表，用户页未调本口。 |
| 34 | GET /api/admin/welfare/requests | backend/api/welfare_admin.py:183 | 待接线 | 两站前端路径集合无命中；求片审核清单口，求片页走 media-seek 口（现行版，非影子版）。 |
| 35 | POST /api/admin/welfare/requests/{param}/approve | backend/api/welfare_admin.py:208 | 待接线 | 两站前端路径集合无命中；求片审核通过口，求片页走 media-seek 口（现行版）。 |
| 36 | POST /api/admin/welfare/requests/{param}/reject | backend/api/welfare_admin.py:226 | 待接线 | 两站前端路径集合无命中；求片审核驳回口，求片页走 media-seek 口（现行版）。 |
| 37 | POST /api/admin/welfare/requests/{param}/done | backend/api/welfare_admin.py:244 | 待接线 | 两站前端路径集合无命中；求片标记入库口，求片页走 media-seek 口（现行版）。 |
| 38 | GET /api/admin/welfare/lottery/blacklist | backend/api/welfare_admin.py:726 | 待接线 | 两站前端路径集合无命中；抽奖黑名单清单，抽奖页无黑名单区。 |
| 39 | POST /api/admin/welfare/lottery/blacklist | backend/api/welfare_admin.py:762 | 待接线 | 两站前端路径集合无命中；抽奖黑名单加入，抽奖页无黑名单区。 |
| 40 | DELETE /api/admin/welfare/lottery/blacklist/{param} | backend/api/welfare_admin.py:791 | 待接线 | 两站前端路径集合无命中；抽奖黑名单移出，抽奖页无黑名单区。 |
| 41 | GET /api/admin/emby/libraries/{param}/scan-live | backend/emby_server/portal.py:2618 | 待接线 | 两站前端路径集合无命中；库扫描实时行，媒体库页走扫描队列口（存疑：同前缀多口，按精确路径区分）。 |
| 42 | GET /api/admin/emby/items | backend/emby_server/portal.py:2634 | 待接线 | 两站前端路径集合无命中；媒体库条目清单口，管理端暂无条目清单页。 |

### 6.2 运维专用（16 条）

| # | 接口 | 模块：行号 | 标签 | 证据 |
|---|---|---|---|---|
| 1 | GET /api/admin/api-keys/verify | backend/api/admin_apikey.py:179 | 运维专用 | 两站前端路径集合无命中；第三方手工连通性测试口，ApiKeys 页文案提及但未调用（审计 M10）。 |
| 2 | DELETE /api/admin/emby-tokens/{param} | backend/api/admin_ops.py:603 | 运维专用 | 两站前端路径集合无命中；单令牌吊销，属运维应急手工操作。 |
| 3 | POST /api/admin/emby/scrape/probe/reset | backend/api/admin_scrape.py:1291 | 运维专用 | 两站前端路径集合无命中；探测队列重置，任务页只有暂停与恢复，重置属运维应急。 |
| 4 | GET /api/admin/emby/images/thumbs/status | backend/emby_server/portal.py:2111 | 运维专用 | 两站前端路径集合无命中；缩略图补生成进度，属运维维护口。 |
| 5 | POST /api/admin/emby/images/thumbs/backfill | backend/emby_server/portal.py:2120 | 运维专用 | 两站前端路径集合无命中；手动触发缩略图补生成，属运维维护口。 |
| 6 | DELETE /api/admin/emby/items/{param} | backend/emby_server/portal.py:2653 | 运维专用 | 两站前端路径集合无命中；单条目删除，属运维应急清理。 |
| 7 | DELETE /api/admin/emby/sessions/{param} | backend/emby_server/portal.py:2706 | 运维专用 | 两站前端路径集合无命中；单会话踢出，属运维应急。 |
| 8 | GET /api/admin/emby/servers | backend/api/emby_servers.py:178 | 运维专用 | 两站前端路径集合无命中；旧 Emby 入口配置口，服务器页已走 /servers 新口（存疑：旧口可能被运维手工调用，不贴待删）。 |
| 9 | PUT /api/admin/emby/servers | backend/api/emby_servers.py:216 | 运维专用 | 两站前端路径集合无命中；旧 Emby 入口保存口，服务器页已走 /servers 新口（存疑，同上）。 |
| 10 | POST /api/admin/emby/servers/mounts/refresh | backend/api/emby_servers.py:282 | 运维专用 | 两站前端路径集合无命中；旧挂载体检刷新口，服务器页走新体检口（存疑，同上）。 |
| 11 | POST /api/admin/emby/servers/test | backend/api/emby_servers.py:307 | 运维专用 | 两站前端路径集合无命中；旧 Emby 入口测试口，服务器页走新测试口（存疑，同上）。 |
| 12 | POST /api/admin/playback/stream-path-policies/import | backend/api/playback_admin.py:108 | 运维专用 | 两站前端路径集合无命中；播放路径策略批量导入，属运维手工导入口。 |
| 13 | POST /api/admin/playback/stream-path-policies/seed-templates | backend/api/playback_admin.py:128 | 运维专用 | 两站前端路径集合无命中；本基线新增口，播放路径策略模板一次性播种，属运维初始化手工触发。 |
| 14 | GET /api/admin/emby/mounts/rclone/conf | backend/emby_server/portal_mount_routes.py:304 | 运维专用 | 两站前端路径集合无命中；旧挂载 rclone 配置读取口，服务器页走新配置口（存疑：路径相近，须核实后才可动）。 |
| 15 | POST /api/admin/emby/mounts/rclone/conf | backend/emby_server/portal_mount_routes.py:324 | 运维专用 | 两站前端路径集合无命中；旧挂载 rclone 配置保存口，服务器页走新配置口（存疑，同上）。 |
| 16 | POST /api/admin/emby/mounts/test | backend/emby_server/portal_mount_routes.py:480 | 运维专用 | 两站前端路径集合无命中；旧挂载测试口，服务器页走新挂载测试口（存疑，同上）。 |

### 6.3 待删候选（0 条）

本基线下无待删候选。原候选 8 条已由 PR #1302（4 条死口）与 PR #1300（4 条影子口）在本批工作窗口内删除，见第 3.2 节；本批不另立待删候选、不删任何接口。

## 7. 声明与后续

- **本批未删任何接口**：本文档是批 4 的唯一交付物；后端、前端、测试、scripts、docs（除本文档外）、CI 均未改动。
- 待接线 42 条的接线（合集页、治理页、版本页等）各另立项；运维专用 16 条不强制进页面。
- 未知项照实写：字符串对账不能证实运行时调用；生产访问日志窗口短，运维手工调用无法证实；同路径不同方法的 7 条记录未在本批定性，须另行核查。
- 回滚：revert 本 PR 即可，文档删除不影响任何接口行为。
