# 更新日志 (Changelog)

所有项目重要更改都将记录在此文件中。

## [2.54.0] - 2026-10-14

产品版本号 2.53.0 → 2.54.0（后端 `VERSION` 单一真源；两端 `package.json` / `package-lock.json` 与管理后台页脚 `APP_VERSION` 同步，均由 `scripts/check_version.py` 门禁守着）。

## [2.53.0] - 2026-10-09

### 播放链路与转码改造（PR #413–#416、#420、#422、#423）

#### 单一播放路径（#413）

播放不再让用户在「直连 / 中转」等多条线路之间选择：统一走一条**中转 + CF** 路径，缓存按热度
自动分层（`local_cache`），线路选择相关的状态与口径（`line_health` / `line_stats` / `play_line`）
随之收敛；管理后台「客户端策略」页去掉线路选择项。

#### 用户级转码开关（#414）

`web_users` 新增 `enable_video_transcoding`（Emby 标准字段 `EnableVideoPlaybackTranscoding`），
默认允许；管理员可对指定用户关闭转码、只给直传以省服务器资源。PlaybackInfo 的转码判定加入用户级检查。
（#420 移除了此前一并下发的 `guarantee` 文案字段——它会让前端白屏。）

#### 硬件转码自检（#415）

启动时用一帧真实编码做自检，按 NVENC → QSV → VAAPI 顺序选可用的硬件编码器，都不可用时静默
回退软件转码（`backend/emby_server/hwaccel.py`）。

#### 删除「神医助手秒播助手」（预提取，#416）并接回队列整理

用户反馈该功能从未成功起播，确认无用后删除 `preprobe_sweep` / `_preprobe_loop` / `_spawn_preprobe` /
`_restart_preprobe` 及相关常量。

⚠️ 那次删除把**队列整理（triage）的调度一起删掉了**：`_preprobe_loop` 原本负责「启动先跑一轮、
之后每 `PROBE_PREEXTRACT_INTERVAL_SEC` 跑一轮」，循环删掉之后 `triage()` / `_run_triage_once()`
只剩定义、全仓没有任何调用点——也就是 2.53.0 承诺的「启动时及每 6 小时纠正状态、给最近播放过的
条目提权」在生产上**一次都不会执行**（维护者在 `tests/test_probe_worker_redesign.py` 里用「显式调用
triage」绕过了这个现象）。本版把它接回来：新增 `_triage_loop()` / `_spawn_triage()` /
`_restart_triage()`，由 `start()` 启动、`stop()` 停止，并在 `worker_registry` 注册为 `probe_triage`
（带崩溃自愈重启）；间隔改用语义正确的 `PROBE_TRIAGE_INTERVAL_SEC`（默认 21600 秒），
`env.example` 同步改名，回归测试改为断言 `probe_triage` 已注册、且 `series` 条目无需手动干预
就被标成 `skipped`。

#### 未保留的改动

「播放前预热文件头 10MB」（#417）与「片头缓存 50MB / 24h」（#419）已按用户要求在 #422 / #423 中
整体移除，最终未保留任何片头缓存代码。

### 媒体信息探测 worker 重构（28.5 万条 pending 卡住 / 时不时卡死）
媒体信息探测 worker 重构：28.5 万条 pending 卡住不动、worker 隔一阵整体卡死。
现象：日志里「预提取定时器启动」照常出现，后台显示 28.5 万条待探测，但数字几乎不动，
探测 worker 隔一阵就整体卡住，只有重启才恢复一会儿。

### 根因（均已用种子库 + mock ffprobe 复现）

1. **队列口径对不上，绝大多数 pending 永远抢不到**：扫描器（fast_scanner / scanner background
   模式 / enrich 衔接）和老库补列的默认值把单集、季、剧集骨架、文件名已解析出 codec 的电影
   全标成 `pending`，而旧调度器只抢「movie/series 且 video_codec 为空」。复现库 3800 条 pending
   时 `_claim_batch` 返回 `[]`；预提取扫描只扫「未在队列」的条目，它们已经是 pending →
   每轮入队 0 条。定时器在跑，实际什么都没探。
2. **抢单泄漏**：旧调度器先把一批标 `probing`，再判断线程池背压，背压时直接跳过——这批永远
   停在 probing（复现：30 秒泄漏 600 条），只有重启时恢复。
3. **单条目没有硬上限**：`subprocess.run(timeout)` 只杀直接子进程后无限 `wait()`，FUSE 挂死
   （D 状态）时线程永久卡住；`os.path.isfile` 同理。线程卡满 → 背压 → 第 2 条持续泄漏，
   表现为「时不时卡死」（复现：挂死挂载上 4 个线程全部卡住后，健康挂载 30 秒内 0 条被探测）。
4. **单体模式根本没启动探测 worker**（M11）：只有 `backend/worker.py` 起它。
5. 用户打开详情 / 播放时，条目若已在 pending 队尾，`maybe_enqueue` 直接返回，不插队。

### 新方案

- **按需优先**：详情页 / PlaybackInfo 仍不阻塞（起播用文件名解析的信息）；已在 pending 的条目
  被打开时提到优先级 1000 并立即唤醒调度器；单集也纳入（`PROBE_ITEM_TYPES`，默认 movie,episode）。
- **DB 状态驱动的后台补齐**：新增 `probe_claimed_at`（租约）、`probe_last_error`，索引
  `idx_item_probe_retry (probe_status, probe_next_retry_at)`；PG 用 `FOR UPDATE SKIP LOCKED` 抢单，
  SQLite 用带状态守卫的 UPDATE；只抢有空槽的量，绝不「抢了不处理」；租约过期自动回收；
  指数退避（封顶 6 小时），K 次后 failed 并记录原因。
- **队列整理（triage）**：启动时及每 6 小时按 id 分段纠正状态：季/剧集/无文件/已删/已合并 → 新状态
  `skipped`，已有完整信息 → `done`，`NULL` 且缺信息 → 入队；最近 30 天播放过的提到优先级 800。
- **硬上限**：所有 ffprobe / mediainfo 走 `proc_util.bounded_run`（独立进程组、超时整组 SIGKILL、
  收不回就放弃等待）；单条目解析 ≤ 20s、探测总预算 ≤ 60s；DB 连接不跨探测持有。
- **按挂载限流 + 熔断**：远程挂载每个默认并发 2、本机 4；同一挂载连续 3 次超时熔断 5 分钟
  （翻倍，最长 1 小时），熔断期间不抢该挂载的条目，健康挂载照常推进。
- **可观测 / 可运维**：`GET /api/admin/emby/scrape/probe-progress`（各状态计数、重试中、过期抢单、
  常见错误、近 10 分钟速率、熔断、ETA）；`POST /scrape/probe/reset`（scope=stuck/failed/retrying/all）；
  `POST /scrape/probe/pause`、`/resume`（存 system_configs，跨进程生效）；`/api/health` 详细报告
  新增「有待探测但 worker 没在运行 / 过期抢单 / 熔断中的挂载」告警。
- **自愈**：调度循环任何异常只记日志不退出；`worker_registry` 支持 restart 回调 + 监督线程，
  线程死了 30 秒内自动重启；`snapshot()` 补上 `status` 字段（health 的 crashed 告警此前永不触发）。
- **单体 / worker 同一入口**：`main.py` lifespan（非 api 角色）与 `worker.py` 都调用幂等的
  `probe_worker.start()`；退出时已抢未处理的条目放回 pending。

### 升级须知

- **自动迁移**：启动时补 `emby_items.probe_claimed_at`、`probe_last_error` 两列和
  `idx_item_probe_retry` 索引（PG 上建索引会短暂阻塞写入，28 万行通常几秒）。
- **一次性自动纠正**：首次启动时所有残留 `probing` 放回 `pending`；随后 triage 把不需要探测的
  pending 改成 `skipped` / `done`——**后台「待探测」数字会在启动后几分钟内大幅下降，这是纠正，
  不是数据丢失**。新状态 `skipped` 只表示「这一行没有可探测的文件」。
- **行为变化**：单集默认也会被探测（只想探电影设 `PROBE_ITEM_TYPES=movie`）；单体部署现在会
  启动探测 worker；`PROBE_WORKERS` 默认 2→4、上限 5→16，`PROBE_MIN_INTERVAL_SEC` 默认 1→0.5。
- **新环境变量**（均可选，见 env.example）：`PROBE_ITEM_TYPES`、`PROBE_REMOTE_CONCURRENCY`、
  `PROBE_LOCAL_CONCURRENCY`、`PROBE_ITEM_TIMEOUT_SEC`、`PROBE_RESOLVE_TIMEOUT_SEC`、
  `PROBE_CLAIM_TTL_SEC`、`PROBE_BREAKER_THRESHOLD`、`PROBE_BREAKER_COOLDOWN_SEC`、
  `PROBE_RETRY_MAX_SEC`、`PROBE_TRIAGE_CHUNK`。
- 管理后台前端尚未加探测进度卡片，接口已就绪（见上）。
- **预提取（神医助手「秒播助手」）已在 2.55.0 删除**（PR #416）：用户反馈该功能从未成功起播，`preprobe_sweep` / `_preprobe_loop` / `_spawn_preprobe` / `_restart_preprobe` 及相关常量、`PROBE_PREEXTRACT_*` 环境变量一并移除；上面的「预提取定时器」现象描述指的是删除之前的旧行为。

## [2.52.0] - 2026-10-09
依据性能审查（S1–S7、S9、S12，P2–P4）。接口路径与返回字段不变；数据库结构不变；
除下文「行为变化」所列外，返回内容与升级前逐字一致（有新旧对照测试）。

### 稳定性

- **S1 串流不再占 DB 连接**：FastAPI ≥0.118 的 yield 依赖在响应发完后才清理，Range 流 / HLS 切片 /
  下载在整个播放期间都占着一个连接（`idle in transaction`），约 50 路并发流即耗尽 PG 连接池、之后全站 500。
  现在 `video_stream`（含 `.mkv/.mp4/.{container}` 变体）、`video_hls`、`/Items/{id}/Download`、
  `/Items/{id}/File`、`/Items/{id}/Thumbnails/{index}` 在返回响应前就关闭会话（`async_db.release_db_before_response`）。
- **S2 远程代理连接池**：上游连接上限 24 → 200（`RELAY_MAX_CONNECTIONS`，可配）；超时由 `Timeout(30, read=None)`
  改为连接 10s / 块间读 30s / 池等待 3s。源站卡住不回数据时按读超时结束该流并归还连接（以前永远挂着、
  逐步把池耗尽）；池满立即返回 503 + `Retry-After`，不再白等 30 秒再 502。同步下载代理用同一套超时。
- **S6 转码闲置回收**：闲置改按「客户端最后一次拉播放列表 / 切片」判定（旧口径看输出目录 mtime，
  ffmpeg 一直写到片尾，用户走了也永不闲置，名额被「幽灵会话」占满后所有新转码 503）。满员时闲置超过
  `EMBY_TRANSCODE_IDLE` 的会话让位；闲置超过 `EMBY_TRANSCODE_ABANDON` 无条件停止；常驻回收线程 30 秒一轮收尸。
- **S7 ffmpeg 随父退出**：ffmpeg 起在独立进程组并设置 Linux `PR_SET_PDEATHSIG`（主进程被杀时 ffmpeg 跟着退出）；
  停止时整组 SIGTERM → SIGKILL，并回收僵尸进程。
- **S3 HLS 新建转码移出事件循环**：策略校验、115 取直链（最长 20s）、本地缓存查询、起 ffmpeg 等整段放进线程池
  一次完成；115 慢时不再卡住整个 EA 进程。
- **S4 Redis 熔断降级**：Redis 超时 5s(+重试) → 1s 不重试；连续失败后熔断一段时间不碰 Redis，缓存改用有界内存缓存、
  限流改为进程内计数（以前「Redis 故障即不限流」且每个请求卡满超时），到期自动探测恢复；启动时连不上 Redis
  也会在后台重连，不再永久降级。PlaybackInfo 的缓存读写移出事件循环。
- **S5 run_all 看护**：worker 退出按指数退避重启（封顶 5 分钟，稳定运行后计数清零），**永不拉停 API / EA**
  （以前累计 3 次就整容器退出）；worker 启动时 Redis 不可用会等待重连而不是直接退出。关键进程（api / ea）
  退出仍整容器退出，交给编排重启。
- **S9 `SortBy=DatePlayed` 不再 500**：未带 `IsPlayed` / `IsFavorite` 等筛选时按「最近播放」排序会引用未关联的
  用户数据列（SQLite `no such column` / PG `missing FROM-clause`）。现在按需外连接当前用户的播放数据，
  没有播放记录的条目不论升降序都排在最后；同时支持 `SortBy=PlayCount`（此前被忽略）。
- **S12 片库可见范围 fail-closed（安全）**：读取用户可见媒体库失败时，旧实现按「全部可见」处理——DB 抖动时
  条目详情 / 播放 / 下载的可见性校验会退化成越权。现在读失败一律按不可见处理（列表 / 搜索为空、按条目取 403），
  并记 warning；工作人员（`is_staff`）不受影响。

### 性能

- **P2 `/Items` 去重下推 SQL**：旧实现每页都把全部候选拉回 Python 再去重切片（O(全库)/请求，翻第 1 页与第 28 页一样慢）。
  现在只在 SQL 里用窗口函数圈出「可能重复」的行在 Python 里精确判定，排序 / 分页 / 计数全部在 SQL；结果与旧实现
  逐条一致。重复行多到超过 `ITEMS_DEDUP_MAX_*` 时自动回退旧实现。
- **P3 NextUp 批量化**：旧实现每部已开看的剧一条查询并绑定 `NOT IN(全部已看 id)`；现在按批一条
  `ROW_NUMBER() OVER (PARTITION BY series_id …)` 查询、「未看」改 `NOT EXISTS`，SQL 条数与剧数无关。
- **P4 orjson 直出**：`Users/{id}/Items`、`Items/Resume`、`Items/Latest`、`Items/{id}`、`Shows/{id}/Seasons`、
  `Shows/{id}/Episodes`、`Shows/NextUp` 改用 orjson 序列化（审查实测 100 条电影 11.7ms → 0.26ms），
  不再在事件循环上跑 `jsonable_encoder`；响应字节与升级前一致。

### 行为变化

- `/Items` 排序追加条目 id 作为最终决胜键：**排序键相同的并列行（同名、同入库时间等）之间的先后顺序可能与升级前不同**，
  但从此在分页之间稳定（以前并列行顺序取决于查询计划，翻页时可能重复或漏掉）。
- 远程代理：源站超过 `RELAY_READ_TIMEOUT` 秒不回数据时会结束这条流（播放器会自动重试 Range）；池满返回 503 而非 502。
- 转码：客户端离开超过 `EMBY_TRANSCODE_ABANDON` 秒后 ffmpeg 会被停止。
- 片库可见范围读取失败时普通用户暂时看不到任何条目（以前是全部可见）。
- 配置了 Redis 但 Redis 故障时，限流改由进程内计数继续生效（`REDIS_ENABLED=false` 仍不限流，与以前一致）。

### 升级须知

- 无需迁移；新环境变量均为可选，留空即用默认值（见 `env.example`「稳定性调优」）：

  | 变量 | 默认值 | 说明 |
  |---|---|---|
  | `RELAY_MAX_CONNECTIONS` | 200 | 远程代理上游连接上限（旧版固定 24） |
  | `RELAY_MAX_KEEPALIVE` | 50 | 空闲保活连接上限（旧版 12） |
  | `RELAY_CONNECT_TIMEOUT` | 10 | 连接源站超时（秒） |
  | `RELAY_READ_TIMEOUT` | 30 | 两个数据块之间最长空闲（秒） |
  | `RELAY_POOL_TIMEOUT` | 3 | 连接池满时最多等待（秒），超时 503 |
  | `EMBY_TRANSCODE_IDLE` | 120 | 满员时闲置超过此秒数的转码让位 |
  | `EMBY_TRANSCODE_ABANDON` | 600 | 闲置超过此秒数无条件停止转码（不小于上一项） |
  | `REDIS_SOCKET_TIMEOUT` | 1 | Redis 读写超时（秒，旧版 5 且重试） |
  | `REDIS_CONNECT_TIMEOUT` | 1 | Redis 连接超时（秒） |
  | `REDIS_BREAKER_FAILURES` | 3 | 连续失败多少次熔断 |
  | `REDIS_BREAKER_SECONDS` | 30 | 熔断时长 / 后台重连间隔（秒） |
  | `WORKER_RESTART_BASE` | 5 | worker 首次重启等待（秒），之后翻倍 |
  | `WORKER_RESTART_MAX` | 300 | worker 重启等待上限（秒） |
  | `WORKER_STABLE_SECONDS` | 600 | 稳定运行多久后退避计数清零（秒） |
  | `WORKER_REDIS_WAIT_MAX` | 30 | worker 启动等 Redis 的重试间隔上限（秒） |
  | `ITEMS_DEDUP_MAX_SUSPECTS` | 20000 | `/Items` 去重：可能重复的行超过此数回退旧实现 |
  | `ITEMS_DEDUP_MAX_DROPPED` | 5000 | `/Items` 去重：被去掉的行超过此数回退旧实现 |

- 如果之前靠调大 PG 连接池来扛并发播放，S1 之后可以按 API 请求量重新评估连接池大小。

## [2.51.0] - 2026-10-09
本轮修复 3 个严重 + 5 个高危漏洞。 - 安全修复：3 个严重 + 5 个高危漏洞

### 安全修复

- **S1 管理员提权（严重）**：`admin_role` 为空 / 未知值此前按超管处理，运营把任意账号的 `is_staff` 置真即可造出超管。
  现改为 fail closed：空值 / 未知值一律按最低权限 `viewer`；启动期迁移 `ensure_legacy_admin_roles` 只把最早的管理员显式写成 `super`，
  库里没有任何启用中的超管时才自愈提升最早的管理员。
- **S2 运营越权管理管理员（严重）**：管理员账号的增删改与角色授予收紧为仅超管可操作，运营角色不能再修改 / 停用 / 提权其他管理员。
- **S3 SECRET_KEY 外发（严重）**：EM 此前把 `SECRET_KEY` 放进 `X-Panel-Key` 发往后台可配置的任意服务器地址（且跟随重定向），拿到即可伪造任意 JWT。
  现改为独立的节点密钥 `NODE_SHARED_SECRET`（未设置时由 `SECRET_KEY` 单向派生），EM 发起的请求只带 HMAC 签名头
  `X-Panel-Ts` / `X-Panel-Nonce` / `X-Panel-Sign`（绑定方法与路径、`NODE_AUTH_MAX_SKEW` 内有效、nonce 去重），探测一律不跟随重定向；
  不再接受 `X-Panel-Key: <SECRET_KEY>`。新增 `backend/node_auth.py`。
- **H1 片库可见性**：条目详情、PlaybackInfo、推流、收藏等接口统一按用户可见的媒体库范围校验，不能再凭 guid 访问 / 播放 / 收藏隐藏库里的条目。
  多版本（`AlternateVersions`）、片头片尾标记（`IntroMarkers`）、剧集组（`EpisodeGroups`）、缺失集（`MissingEpisodes`）、
  视频缩略图（`Thumbnails`）同样要求登录并校验可见库；片头标记的增删与剧集组选择是全站共享数据，改为仅可写角色的管理员可改。
  （同时补回被误删的 `IntroMarker` 模型，`IntroMarkers` 接口此前直接 500。）
- **H2 JWT 不进 URL**：网页播放器不再把 JWT 拼进播放 / 图片 URL，改用短期签名播放链接（有效期 `PLAY_SIGN_URL_TTL`，默认 21600 秒），
  链接泄露（日志 / Referer / 分享）不再等于账号泄露。
- **H3 管理接口仅 Bearer**：媒体库 / 挂载 / 115 等管理接口与 `/api/admin/*` 同一口径，只接受 `Authorization: Bearer <access JWT>`；
  不再接受 `?api_key=` 或 Emby 客户端 token（管理员在播放器里登录的长期 token 不能再调删除 / 挂载等管理操作）。
- **H4 可信代理**：`CF-Connecting-IP` / `X-Forwarded-For` / `X-Real-IP` 只在 TCP 直连方是 Cloudflare 回源网段、回环或 `TRUSTED_PROXIES` 时才采信，
  直连源站伪造 `CF-Connecting-IP: 127.0.0.1` 不再能绕过域名守卫 / 限流。新增 `TRUSTED_PROXIES` / `CLOUDFLARE_IP_RANGES` 说明（见 `env.example`）。
- **H5 兑换码每人一次**：注册码 / 兑换码新增核销记录，同一用户对同一张多次码只能兑换一次（兼容升级前 `used_by` 里的记录）。
- **缩略图缓存串图**：库外同名外挂图（`poster.jpg` / `folder.jpg` / `cover.jpg` / `fanart.jpg`……）此前共用同一张缩略图（如 `poster_w320.jpg`），
  首页「本周入库」海报与片名对不上。现对缓存之外的原图按「绝对路径 + mtime + 大小」的 sha1 命名缩略图（`backend/emby_server/image_store.py`）。

### 恢复 2d7d996 误删的功能

2d7d996（安全修复 v2）基于旧底稿重新应用，把 PR #400、#403~#408 的一批
StrmAssistant（神医助手）移植功能整段覆盖掉了。这些删除不是有意的，现按原提交恢复
（2d7d996 的安全语义全部保留：管理员角色 fail closed、节点签名、可见性校验、
JWT 不进 URL、可信代理、兑换码每人一次、缩略图缓存 key）。

- **片头片尾标记**：恢复 `IntroMarker` 模型（`emby_intro_markers` 表）。
  此前 `/Items/{id}/IntroMarkers` 接口访问即 500。
- **演员增强**：恢复 `emby_people.person_tmdb_id` 字段、老库自动加列、
  `(item_id, person_tmdb_id)` 复合索引、刮削落库，以及演员刷新 worker 的启动。
- **TMDB 备选语言 / 原语言海报**：恢复 `language_fallback_chain`（首选语言无命中时
  按 zh-CN → zh-HK → zh-TW → ja-JP → en-US 重试，深度由 `TMDB_FALLBACK_MAX_LANGS` 控制）、
  海报语言偏好（默认 `system`）与原语言海报优先；「重试未匹配项」会清整条语言链的缓存。
- **代理工具**：恢复 `is_valid_proxy_url` / `try_parse_proxy_url`。
- **拼音排序**：恢复 `lru_cache(65536)`；`pypinyin` 恢复锁定 `==0.55.0`。
- **后台 worker**：恢复 Redis 跨进程心跳（`/api/health` 能看到 worker 容器的线程）与
  死线程告警；缩略图 / 字幕扫描 / 多版本合并恢复三轮打磨（guid 防穿越、单视频超时、
  分批提交、目录不可访问不误删字幕、防跨库 / 跨类型合并等）。
- **扫描器**：条目下架时同步清理 `/data/mediainfo` 里的媒体信息 JSON。
- **多版本管理后台接口**：恢复 `/api/admin/media/versions/{id}`、`/unmerge`、`/unmerge-all`。
- **文档**：`docs/新手指南.md` 文件名恢复（此前被改成乱码，README 链接失效）。

### 升级须知

1. **管理员角色**：升级后只有**最早创建的那个管理员**（安装向导 / `scripts/create_admin.py` 建的号）保持超管；
   其他从未显式设置过角色的管理员会变成**只读**，需超管在「管理员」页重新授予角色，或用 `scripts/create_admin.py` 处理。
2. **EM / EA / 所有推流节点必须同时升级**：旧版 EM 发的 `X-Panel-Key: <SECRET_KEY>` 不再被接受，新旧混跑会互相拒绝。
   部署脚本（`deploy-streaming-node.sh`）与运维 curl 的 `X-Panel-Key` 改用 `python -m backend.node_auth` 输出的**节点密钥**，不要再填 `SECRET_KEY`。
   各端时钟需同步（误差超过 `NODE_AUTH_MAX_SKEW`，默认 300 秒，签名会被拒）。
3. **建议轮换 `SECRET_KEY`**：旧版本已把它发往后台配置的服务器地址，视为可能泄露；轮换后所有用户需重新登录，EM / EA 两端同时改。
   若显式设置了 `NODE_SHARED_SECRET`，所有节点同步更新。
4. **Docker 反代需设置 `TRUSTED_PROXIES`**：默认只信 Cloudflare 回源网段与回环；宿主机 Nginx → docker-proxy → 容器的部署，
   容器看到的直连方是 docker 网关，需把网段加入（如 `TRUSTED_PROXIES=172.16.0.0/12`，或按 `docker network inspect` 查到的子网），
   否则所有用户共用一个限流桶。
5. **网页播放链接会过期**：签名播放链接在 `PLAY_SIGN_URL_TTL`（默认 21600 秒 = 6 小时）后失效，长时间挂着的播放页刷新即可重新签发。
6. **可清理旧缩略图**：图片缓存目录为 `EMBY_IMAGE_DIR`（默认 `<EMBY_TRANSCODE_DIR>/images`，即 `/tmp/emby_transcode/images`）。
   旧版按外挂图文件名生成的串图缩略图（如 `poster_w320.jpg`、`folder_w160.jpg`）不会再被引用，可删除后按需重新生成：
   `find "$EMBY_IMAGE_DIR" -maxdepth 1 -name '*_[wh][0-9]*.jpg' ! -regex '.*/[0-9a-f]\{40\}_[wh][0-9].*' -delete`
   （只删非 sha1 命名的缩略图；缩略图本身都是可再生的，误删只会触发重新生成）。

## [2.50.0] - 2026-10-09

只做「不改行为」的清理（依据性能审查第 7 节中标为低风险 / 机械安全的条目），外加一个纯性能修复。
接口、配置项、数据库结构均不变；未删除任何仍被引用的代码。

### 删除

- `backend/emby_server/fast_scanner.py`（731 行）：生产代码零 import，`USE_FAST_SCANNER` 无人读取；
  只测它的 `tests/test_incremental_scan.py` 一并删除。
- ~~`backend/emby_server/refresh_person_worker.py`~~：**保留**。清理时它在 main 上确实无人启动，
  但「恢复 2d7d996 误删的功能」已把 `worker.py` 里的演员刷新 worker 启动接回来，故不删（相关断言一并保留）。
- 28 个全仓零引用的函数 / 类（原清单 29 个；`mediainfo_persist.delete_json` 已被恢复的「下架时清理媒体信息 JSON」使用，保留）（api.py 里的 `_image_urls`、`_probe_duration_on_demand` /
  `_ffprobe_duration_sync`、`_batch_series_source_dirs` 等 8 个，及 reminders / websocket / mounts /
  scan_queue / streaming 等模块里的 21 个），删除前逐个全仓 grep 复核（含字符串 / getattr / 路由注册）。
- `main.py` 里重复两遍的 GZip 注释与被注释掉的死代码（GZip 仍保持禁用）。

### 依赖

- 从 `backend/requirements.txt` 移除未使用的 `alembic`、`aiofiles`、`apscheduler`、`loguru`、
  `pydantic-settings`、`email-validator`。
- `passlib[bcrypt]` 换成显式 `bcrypt>=4.0.0`：代码从未 import passlib，`security.py` 直接用 bcrypt。

### 合并

- `database.py` 的 7 个 `_ensure_*_index` 合并为一张索引表 `_LEGACY_INDEXES` + 一个幂等 helper；
  执行顺序、DDL 与迁移日志沿用「恢复」后的版本（演员索引为 `idx_person_item_tmdb (item_id, person_tmdb_id)`）。

### 性能

- 集 / 季的图片回退链取季改用 `db.get`（命中会话 identity map 时零 SQL）：含集列表每条集原先多
  2 次 SQL，审查实测 `Items?IncludeItemTypes=Episode&Limit=1000` 2015 条 SQL；一页 40 集的回退链
  从 80 条 SQL 降到 0。新增 `tests/test_image_chain_sql.py` 钉住。
- `Shows/{id}/Episodes` 的剧名改从预取结果取，每次请求少一次 IN 查询。

### 升级须知

- 无需任何操作。自定义镜像如果依赖上面移除的 Python 包，需要自行安装。

## [2.49.0] - 2026-10-09

用户端（`user_frontend/`）整体换成「暗房影院」视觉：暖黑底、实色卡片、放映机琥珀**单一强调色**、
衬线标题、整屏胶片颗粒。只改视觉与首页编排，路由 / 接口 / 业务逻辑不变；管理后台与后端未改动。
版本号未在本 PR 内抬升（AGENTS.md 未要求），合并发版时再定号。

### 1. 设计令牌（`styles/aurora.css`，别名层 design-tokens / neo-noir 跟随）

- 底色 `#0c0a09`（bg-soft `#11100e`），去掉 body 上的青 / 紫两团径向光；
- 表面改为**实色**：surface `#171412` / `#1e1a17` / `#26211d`，发丝边框 `rgba(245,235,220,.08 / .14)`，卡片不再有 backdrop-blur；
- 唯一强调色：琥珀 `#e8a84a`（strong `#d4923a`、deep `#a86f24`），琥珀底上的字 `#1a1205`；
  `--au-gradient` / `--au-gradient-warm` 保留名字但变成纯色琥珀，`--au-primary-glow` / `--au-shadow-glow` 取消发光；
- 语义色降饱和：成功 `#8fbf8a`、警告并入琥珀、危险 `#d9776b`、信息 `#9db4c8`；原紫 / 粉装饰色收成中性暖灰；
- 资产 tone（cyan / amber / violet）不再各染一色：数字走正文色，只有图标与 CTA 用琥珀；签到按钮的金币三档并入琥珀家族；
- 文字 `#f3ede4` / .72 / .6 / .42；**正文字号下限 12px → 13px**（令牌与各组件里写死的 0.75rem / 0.6875rem 等一并抬到 13px）；
- 圆角收敛：sm 8 / md 10 / lg 14 / xl 14；阴影克制、无光晕；主按钮实色琥珀，hover 微亮、按下 0.98，不再上浮；
- 入场动效改为「从黑场淡入」（只动透明度，约 0.7s），去掉上浮 / 滑入；
- 新增 `--au-font-serif`：h1–h3、页标题、区块标题、弹窗标题走 Noto Serif SC（`index.html` 以 display=swap 引入 600/700，宋体系兜底）；全站数字 tabular-nums；
- 整屏静态胶片颗粒（`body::after` 内联 SVG feTurbulence，opacity 0.05，overlay 混合，不吃事件、不动画；播放页不叠）；
- 白日模式改为「影院大厅」奶油纸色：底 `#f5f1ea`、表面 `#fffdf9`、墨色文字 `#1c1915`、琥珀压深到 `#b9802f`，三档主题切换照旧。

### 2. 首页重排（`views/HomeView.vue`，数据绑定沿用）

1. **幕布 Hero**：整幅背景图取「追新日历」近 7 天入库里第一张带背景图的条目（没有就是暖黑渐变），底部渐隐到底色；
   左下角琥珀眉题（今日新片 / 本周新片 · 片名）、衬线问候「晚上好，用户名」、状态行（会员剩 N 天 · 签到 / 在播设备）、
   唯一一个琥珀主按钮（未开通 = 立即开通；已开通 / 公益服 = 一键导入播放器）+ 文字链「连接教程」；未开通「三步开始观影」保留；
2. **票根卡**：积分 / 订阅 / 观影数据三张资产卡合成一张电影票——衬线大数字、打孔虚线 + 半圆缺口、右侧票根放续费 / 开通按钮与 ADMIT ONE；骨架与错误态保留；
3. **今日入库**：近 7 天入库的竖版海报横滑（按剧去重，最多 12 张），点击与追新日历一致走 Rex deep link，右上「追新日历 →」；无数据整段不渲染；
4. **正在播放**：带缩略图 + 细琥珀进度条；5. **进行中**：求片 / 工单；6. **帮助中心**：纯文字列表 + 发丝分隔线 + ›；
- 公告 / 兜底条幅改为左侧一根琥珀发丝线，不再铺色。

### 3. 其余页面与组件

- 顶栏：品牌字衬线大写拉开字距（AETRIX），导航改为安静的纯文字、当前页下方一颗琥珀圆点，去掉装饰光斑与毛玻璃；底部导航坞同一语言；
- Toast / Modal / Button / SegmentedControl / LoadingSpinner / EmptyState 等组件去发光、去毛玻璃、换实色；
- 登录页去掉光斑与网格背景；签到、邀请、钱包、个人中心的主卡去掉渐变底与光晕，改为实色卡 + 顶部琥珀发丝线；
  404 页改用主题令牌（原来是 Tailwind 的天蓝 / 灰）；工单状态色收进语义色；详情 / 播放页的硬编码颜色改为令牌。

### 4. 兼容说明

- 站点品牌色：后端 / 管理后台的默认主题色仍是旧青色 `#22d3ee`。用户端把这个值（以及新默认 `#e8a84a`）视为「未自定义」，
  不再注入覆盖，交给主题自己的琥珀；管理员填了其他颜色时仍按品牌色覆盖（发光一律关闭）。

## [2.48.0] - 2026-10-04

上一轮标为「超出本轮范围」的六项，这轮全部落地：写入中的文件不再被探测、监听丢事件能
自己对账、补全队列补上复合索引、探测结果按 path+size+mtime 缓存并合并同文件任务、
封面重生成改为异步且按库合并、条目下架改为**软删除**（可回滚）。

### 1. 写入中的文件不再被探测（避免截断时长落库）

拷贝 / 转场 / 转码过程中扫描到的文件，`ffprobe` 会读出**截断**的时长，而它一旦落库就是
`done`、此后再不重探——比不探更糟。现在探测前比一次指纹（`path|size|mtime_ns` 的 md5，
与扫描时落库的 `MediaItem.file_fingerprint` 同算法）：

- 指纹相同 → 文件自扫描起没动过，直接探（绝大多数情况，零额外开销）；
- 指纹不同**且** mtime 距今不足 `PROBE_STABILITY_SEC`（默认 60s）→ 判定还在写，
  推迟 `PROBE_STABILITY_RETRY_SEC`（默认 60s）后再来，本轮记为 `unstable`；
- 没有扫描时留下的指纹（老数据行）→ **没有证据**就在写，直接探，不会被无限期推迟；
- `os.stat` 失败 → 文件是真的没了，**放行**：稳定性闸子只负责「晚一点探」，不负责改判结果
  （下游的 404 / not-found 分支会立即判失败并计入重试）。

刻意**不用**「连续两次观测 size/mtime 相同」那种做法：那要求每个条目都多等一轮，把 worker
吞吐直接砍半，而上面第 3 条已经用 mtime 新鲜度抓住了真正危险的那一小段窗口。

### 2. 监听丢失能自己对账（不退化成全量扫描）

inotify 会漏事件（watch 数超上限 / 容器重建 / 驱动重启 / 目录被换掉），而这些变更只有靠定期
与数据库对一次差才能发现。现在监听线程每 `FS_RECONCILE_INTERVAL_SEC`（默认 900s，下限 60s）
对一次账：

- **只在有差异时**重建监听：无差异时一条日志都不打（绝大多数时候它就是空转）；
- 有差异时记 info 日志说清楚是「新增库 / 移除库 / 路径变更」，再 `sync_from_db()`；
- 对账口径与 `sync_from_db` **完全一致**（含排除清单、`fs_watch` 开关、纯远程来源的库不监听），
  两边不一致会每轮都判成「有差异」，那就不是补漏而是每 15 分钟空转重建一次；
- **刻意不触发扫描**：inotify 漏掉的事件无法可靠判定“漏了哪些文件”，凭猜测补扫描要么重复
  要么遗漏；补漏靠增量扫描的目录指纹。对账只查库、比内存集合，**不遍历目录、不列文件**。

`watcher_status()` 新增 `reconcile_interval_sec` / `last_reconcile_at` /
`last_reconcile_changed`（`changed` 为 `None` 表示这一轮没对成，界面上能看出异常）。

### 3. 补全队列补上复合索引

探测队列早就有 `idx_item_probe`，补全队列当初只给了 `enrich_status` 单列索引——而
`_claim_batch` 的抢单是 `WHERE enrich_status='pending' AND enrich_next_retry_at<=now` 再按
`enrich_priority` 排序，单列索引帮不上忙，积压一涨就是排序全表。新增
`idx_item_enrich(enrich_status, enrich_next_retry_at, enrich_priority)`，老库启动时按
inspector 幂等补建（`DROP INDEX idx_item_enrich` 即可回滚，无数据影响）。

### 4. 探测缓存 + 同文件任务合并

真正贵的那一步是 ffprobe（起进程 / 向网盘发 Range）。同一份文件被反复排队时，那些成本是
**同一份字节的同一次**：

- **缓存**：键 = `path + size + mtime`（本机文件，文件一变键就变，命中的一定是同一份字节；
  远程挂载拿不到 mtime，退化为 `path + size`）。有界 LRU + TTL
  （`PROBE_CACHE_MAX` 512 / `PROBE_CACHE_TTL_SEC` 900）。**只缓存干净结果**——
  缓存一个 404 等于把这个文件永久判死，文件补回来之后再也探不出时长。
- **合并**：同一批里指向同一个文件（`file_path + size`）的条目只探一次，结果连同内封轨道一起
  镜像给其余条目；抢单顺序（优先级）不变。代表条目「没探成」（被抢走 / 配额熔断）时**不镜像**，
  影子条目退回 `pending`——停在 `probing` 上既不会被下一轮抢到（它只捞 pending），也没人会再动它。

### 5. 封面自动更新改为异步（与扫描解耦、任务合并、失败保旧）

以前扫描尾部**同步**渲染封面：渲染慢就把整轮扫描拖住，转场一批新片还会在同一轮里反复触发。
现在扫描只调 `library_cover.enqueue_cover_regeneration(lib_id)`，后台线程按库合并执行：

- 排队 / 渲染期间同一库再来多少次都只算**一个**任务（转场 200 个文件 → 1 次渲染）；
- 任务之间限速（`COVER_MIN_INTERVAL_SEC`，默认 2s），批量导入时不给渲染器喘息；
- 失败**保留旧封面**：`_write_generated_cover` 只在拿到完整数据后才 `os.replace`，渲染崩了
  旧文件原封不动，只记 `failed` 与原因；一个库失败不影响队列里后面的库；
- `cover_status(lib_id)` 给出 `idle/pending/running/done/failed`；应用与 worker 退出时
  `stop_cover_worker()` 收尾。手动接口仍然是同步调用同一个 `regenerate_cover_for_library`
  ——两条路渲染参数同源，只是自动路径不再占用扫描线程。

### 6. 条目下架改为软删除（可回滚）

以前清理阶段是**硬删**：条目连同外挂的播放进度、收藏、分面一起消失，误删一次就再也回不来。
现在 `emby_items` 新增 `deleted_at`（老库补列后全为 NULL = 行为不变）：

- **标记**：清理阶段只写 `deleted_at`；
- **隐藏**：可见性由一个装在 `sqlalchemy.orm.Session` 上的 `do_orm_execute` 钩子**全局**补上
  `deleted_at IS NULL`——三十多个模块、几百处 `db.query(MediaItem)` 只需漏改一处，被删的条目
  就会换个页面重新冒出来，所以一处兜住，读路径一行都不用改。钩子只作用于 `SELECT` 且只在
  真的选了 `MediaItem` 实体时才加条件（给别的表加会凭空多出一张表的笛卡尔积）；
  复活 / 删库 / 排查用 `include_deleted()` 显式放行；
- **复活**：文件重新出现时扫描器清空 `deleted_at`——复用**原来那一行**（不是新建一条），
  播放进度、收藏、已刮好的元数据一起回来，扫描统计里多一个 `resurrected` 计数。
  指纹秒跳与 `_can_skip_file` 都对「下架过的条目」放行，否则文件回来了却会被秒跳掉；
- **回收**：隐藏超过 `MEDIA_SOFT_DELETE_PURGE_DAYS`（默认 30 天）才物理删除（连从属数据），
  表不会无限涨；
- **删除保护不被绕开**：零结果 / 数量阈值两道闸照旧，而且计数**只算可见行**——否则一个删过
  90 部的库会把「本轮看到剩下 10 部」误判成“来源整体不可用”，把后面真正要清理的场景一起拦住；
- 新增 `idx_item_lib_deleted(library_id, deleted_at)`（老库幂等补建）：可见性条件与几乎所有
  查询的 `library_id = ?` 绑在一起，索引能把已下架的行在索引层就跳过；
- **计数也跟可见性走**：媒体库条目数、`Items/Counts`、看板统计、修复队列条数、剧/季子条目数
  全部改走 `soft_delete.count_visible()`（`Query.count()` 会把语句包成子查询，全局过滤器
  下不到那里去，否则界面上的条目数会比实际能浏览到的多）；
- **回滚**：`.env` 里 `MEDIA_SOFT_DELETE=0` 并重启。此时启动阶段先把已隐藏的行**全部放回可见**
  （否则它们会变成既不显示、也永远删不掉的僵尸行），之后照旧物理删除，与 v2.47.0 一致。

### 验证

pytest **1441 passed**（新增 62 条：`test_soft_delete` 30 / `test_probe_cache_merge` 16 /
`test_cover_auto_regen` +7 / `test_fs_watch` +8 / `test_removal_guard` +1）；12 项
`scripts/check_*.py` 全过；两个前端 type-check + build 全 rc=0。

顺带修掉的：封面 worker 的 `stop_cover_worker()` 少写一个 `global` 导致收尾必抛
`UnboundLocalError`；`FS_RECONCILE_INTERVAL_SEC` / `PROBE_*` 写成非法值会让整个模块导入失败
（现在回落默认）；探测缓存的 `streams` 列表原本是共享引用（调用方改它会改到缓存）；扫描流水
多一个 `resurrected` 计数（后台「新增/更新/下架」列会在发生时补一个 `↺N`）。

## [2.47.0] - 2026-10-04

一轮「存储健康 / 实时监听 / 后台 UI」系统优化，四个主题。

### 1. 实时监听终于会启动（两个 P0）

`start_fs_watcher()` 只写在 `main.py` 的 `if not _is_api_role` 里，而 `run_all.py`
会把 serve.py 子进程改写成 `AETRIX_ROLE=api` —— 那个分支整体跳过；剩下的 worker 进程
清单里又没有 fs_watcher。**结果是生产的 inotify 从来没启动过**，新片只能等定时扫描。
现在 worker.py 也起一次。

同时 `is_watchable()` 增加 FUSE / 网络盘识别（解析 `/proc/self/mountinfo`，最长前缀
匹配挂载点，缓存 60s）。`isdir` + `access` 在 rclone FUSE 上会返回「一切正常」，而真去
watch 会把整个挂载点拖死；现在按文件系统类型直接降级为定时扫描，并给出人话原因。
读不到挂载表时**退化成老行为**——拿不到信息不等于「不能监听」。

### 2. 删除保护：挂载异常不再误删整库

事故形状是「挂载断开 → 底层空目录还在 → 本轮扫描看到 0 个文件 → 清理阶段把整库删了」。
`failed_roots` 挡不住它（目录还在，只是空的）。新增两道闸，都是 O(1) 计数查询、
命中则**一条都不删**：

- **零结果**：本轮看到 0 个文件而库里有条目 → 禁止清理
- **数量阈值**：预计删除数 ≥ `max(20, 库内条目数 × 0.5)` → 禁止清理

拦截原因会进 `stats["removal_guard"]` 并打 error 日志——排障时能看到「本轮没删是因为
被拦了」，而不是误以为「库里本来就空」。

阈值是环境变量（`SCAN_REMOVAL_MAX_RATIO` / `SCAN_REMOVAL_MAX_ABSOLUTE`），
**回滚只需改环境变量，无需改代码、无需跑迁移**。

### 3. 语义统一：排除清单同时管追新与 inotify

以前「已排除追新」的库仍然会因为本机文件变动被触发增量扫描——用户关掉的东西还在后台
跑。现在 `fs_watcher` 复用 `change_watcher.resolve_excluded`（同一函数，含旧包含清单的
一次性迁移），且保存排除清单后立即重建监听，不等重启。

### 4. 路径解析统一到 mounts

`change_watcher` 以前自己写死 `paths.split(",")` 和一份 id 解析器，与扫描/监听/界面的
`mounts.split_library_paths` 不同源。同一串路径在两处会拆出不同结果：**全角逗号分隔
时追新整个漏掉那个目录**（症状：线程在跑、last_check 在更新，却永远发现不了新资源）。

新增 `mounts.parse_id_list()` 作为全仓唯一的 id 字串解析口，`parse_mount_ids` 与追新
的 `_parse_ids` 都转发到它，64 位上限常量也只剩一份定义。

顺带修好的：全角逗号路径、重复路径（以前会重复扫两次）、全角逗号挂载 id、`0` 与超 64
位的脏 id（现在丢弃，对 `.in_()` 查询更安全）。

### 5. 11 项 UI 清单（②③④⑥⑦⑧⑨⑩）

- ② 路径示例改成 `/mnt/电影`
- ③ 新建服务器时可直接粘 rclone.conf，点「创建」时随服务器一起写入
- ④ 「重新刮削」→「刷新元数据」，并写明它不扫描新文件
- ⑥ 单机单节点时隐藏「归属服 / 播放节点」
- ⑦ 封面区写明：样式改完要点底部保存才生效，预览只是预览
- ⑧ 路径变更时**主按钮**变成「保存并扫描」（以前两个并列按钮，主按钮还是「保存」）
- ⑨ 115 账号选择器只在真有 115 来源时显示
- ⑩ Dashboard 存储卡改看**本机目录可读性**（新增 `local_dirs_readable`，真实 scandir，
  并跳过 FUSE 以免拖住这个列表接口），不再看旧的 EA 可达性

### 验证

pytest **1379 passed**（新增 36 条：`test_fs_watcher_fuse` 13 / `test_removal_guard` 8 /
`test_path_parsing_unified` 8 / `test_excluded_unified` 7）；12 项 `scripts/check_*.py` 全过；
两个前端 type-check + build 全 rc=0。

## [2.46.0] - 2026-10-04

### 媒体库设置页挂载去重：只剩「按类型添加路径」一个入口

媒体库设置页之前有**三处**重叠的挂载设置：

1. 媒体路径——每条路径选「存储后端」（本地 / Rclone / 115）并关联挂载 id
2. 「存储挂载」——另外一个下拉多选，勾「这个库用哪些挂载」
3. 高级折叠——「不建挂载，直接写 `rclone:` / `115:` 前缀路径」再加一条

三处说的都是同一件事（内容存在哪），于是同一个挂载要在两个地方各配一次，而第2 处配错了
不会报错，只会让扫描结果和预期对不上。去重后只留第 1 处：

- **删掉「存储挂载」下拉框。** 前端不再下发 `mount_ids` 字段。
- **「按类型添加路径」成为添加路径的唯一入口。** 弹窗里先选类型再浏览目录，默认就是
  服务器本地硬盘；115 / Rclone 收进弹窗的「高级」折叠区。
- 原来的「高级：不建挂载，直接写前缀路径」整块去掉——它就是第3 个添加入口，
  而且这种条目扫描期本来就会记成「不可用来源」（前缀路径在扫描器里没有远程解析分支），
  留着只会让人以为可用。

### 老数据怎么办：后端字段保留，前端不再让它改

`Library.mount_ids` **没有删**，扫描、追新、挂载健康仍然按它取来源。它只是不再能从
媒体库设置页编辑了：

- 前端保存时**根本不下发** `mount_ids`；后端 `LibraryUpdate.mount_ids` 省略 = **不修改**
  （只有显式传列表才会覆盖），所以老库上挂着的挂载不会被静默清空。
- 显式传 `mount_ids` 仍然能写（老前端与外部调用方还在用），传空列表仍然是「解绑」。
- 设置页里对这类库显示一条**只读**提示（还挂着哪几个挂载 + 「不再能改，要解除绑定请去
  服务器页删掉对应挂载」）。不这么写等于骗用户说数据没了——它实际还在生效。
- 列表接口继续下发 `mount_ids`，老前端不受影响。

### 顺手修掉两个「改个名字都存不进去」

- `path_entries=[]` 会被拼成空串再 `split(",")` 成 `[""]`，校验把它当成本机目录判成
  「目录不存在」→ 老库里只剩 `mount_ids`、一条路径都没有的库**改个名字都返回 400**。
  空条目现在跳过。
- 存量前缀路径（`rclone:gdrive/Movies`）同样被 `os.path.isdir` 判成不存在，把整个表单
  卡死。前缀路径现在放过（它通不通交给扫描期判定，扫描期会记成不可用来源并跳过清理，
  比在保存时硬判准）；本机目录写错仍然拒绝。

新增 `tests/test_library_mount_dedup.py`（12 条）钉住这些行为。

## [2.45.0] - 2026-10-04

### 追新改成「排除清单」

以前追新的库是**包含清单**，且「留空 = 全部监听」。于是想关掉 A 库有个反直觉的前置步骤：
必须先去 B 库打开开关、让清单被写出来，再回来把 A 从清单里删掉。清单为空时那个开关干脆
被置灰，提示“先在别的库上打开它、让清单列出来，再回来关本库”。

现在改成**排除清单**（空 = 全部启用库都监听）：

- 媒体库设置里开关直接是「监听 / 排除」，选「排除」就是把这个库加进排除清单，
  一次点击到位，不再需要任何前置步骤，也不再有被置灰的状态；
- 「排除全部库」现在是一个完全合法的配置（以前“清单不能全空”是个硬性拦截）；
- 被排除只影响**追新**，手动扫描、定时扫描、inotify 监听都不受影响（文案里写明）。

### 老配置怎么迁移（这块比 UI 重要）

新的配置键 `chase_new_excluded`；旧的包含清单键 `chase_new_libraries` 只在迁移里读一次：

- 新键存在 → 以新键为准，不看旧键；
- 新键不存在而旧键非空 → 算成「启用库 − 包含清单」写进新键，然后清空旧键；
- 新旧都为空 → 本来就是「全部监听」，两套语义一致，不写任何东西。

这一步不能省：直接忽略旧键，老部署会从「只听 A、B」**静默变成全部监听**（一次扫描风暴）；
直接当排除清单读，则会变成「A、B 被排除、其余反而在听」——把整个清单含义反过来。

另外后端返回的 `libraries` 字段固定为空串：老前端还在读它，给非空值会让它把排除清单
当成包含清单用。两边同时跑也不会打架。

## [2.44.0] - 2026-10-04

### 本地挂载扫描：增量开关 + inotify 实时监听

存储改成宿主机本地挂载（/mnt/...）之后，扫描直接走本地文件系统，这一版把两件事补齐。

**先说清楚现状**：增量扫描的机器其实**早就在跑了**——目录指纹 + 文件指纹
（path|size|mtime_ns）秒跳、删除检测（清理阶段对比库与文件系统）、`SCAN_INCREMENTAL`
全局开关都已有（scanner v2.40.0）。所以这一版**没有重写增量**，而是补上真正缺的：

- **每库「增量扫描」开关**（默认开）：之前 `SCAN_INCREMENTAL` 只能全局关，无法只给某一个库关。
- 卡片上多一个「全量扫描」按钮（带代价提示的二次确认）：平时用不到，排查“改了文件却扫出
  旧数据”这类指纹异常时才用。
- **手动「全量扫描」**：`POST /libraries/{id}/scan?full=true`，无视所有指纹完整处理一遍。
  全量打在**扫描快照**上，只影响这一轮，不会把这个库或别的库改成“以后都全量”；多节点部署下
  全量标记跟着转发到归属节点。
- **inotify 实时监听**（新，默认开）：本机目录有变动就自动入队一轮增量扫描
  （`trigger="fs-event"`），不用等定时扫描。

### 为什么没有按 `mtime > last_scan_at` 做

需求里写的是「只处理 mtime > last_scan_at 的文件」，但那比现有的指纹方案**更不安全**：

- 依赖墙钟：时钟回拨、时区、容器与宿主机时间不同步都会直接造成漏文件；
- 扫描过程中被修改的文件：mtime 可能落在 last_scan_at 之前或之后，边界上必然有缝；
- last_scan_at 写成功但文件没入库时，那一轮的文件会被**永久跳过**。

现有方案不看时间，只看「这个目录/这个文件本身变没变」，不存在这类漏。跳过也永远以
「库里那一行存在且不过期」为前提，指纹写早了、进程崩了，下次扫描会因为查不到那一行而重做。

### 监听的设计取舍

- **只管入队，不管扫描**：回调只把「库 id → 计数」记进字典，真正的入队在独立线程里做，
  遍历/入库仍然交给扫描器（它本来就会按指纹秒跳）。监听线程因此永远不做重活，
  不会抢播放与 API 的资源。
- **1 秒防抖 + 按库合并**：转场一个目录会产生成百上千个事件，逐个处理等于把磁盘读烂；
  1 秒内同一库只入队一次，且同库入队本就幂等。
- **限流**：同一库 60 秒内最多触发一次，防止磁盘抖动把队列灌满。
- **只监听本机路径**：路径从媒体库配置动态读（绝对路径 + local 挂载根），不写死任何路径；
  `mount://` / `115:/` / `rclone:` 这些远程来源**不监听**（拿不到可靠事件源），继续由轮询兜底。
- **降级而不是失败**：路径不可读 / inotify 实例耗尽（`max_user_watches`）时把该库记成
  degraded 并给出人话原因，`GET /libraries/fs-watch/status` 与设置页都能看到，
  此时退回定时扫描——不会少入库，只是慢一点。
- **容器重启自动恢复**：在应用 lifespan 里启动，每次从库里读「当前所有启用库的本地路径」
  重建监听；`watchdog` 装不上就整体退化为不监听，不影响任何其他功能。

### 顺带修掉一个真 bug

`_backfill_orm_columns`（按 ORM 声明补列的兜底）只对账 `database.Base`，而
`backend.emby_server.models` 是自建的另一个 Base——**emby_* 的表从来没被兜底过**。于是每给
`emby_libraries` 加一列，都必须同步手写 `_MISSING_COLUMNS` 条目，漏了就直接报
“表没有该列”。现在两边都以 ORM 为准，忘了写也能自愈。

另外 `LibrarySnapshot` 是 frozen dataclass，而全量扫描的标记原先是直接赋值的——
真点一次「全量扫描」就会抛 `FrozenInstanceError`。已改成 `dataclasses.replace`，
并补了回归测试钉住。

## [2.43.1] - 2026-10-04

### 媒体库封面：新增片入库后自动重新生成

封面的自动生成样式（海报拼贴 / 主视觉 / 胶片带）以前只能手动点「按最新海报重新生成」。
库在持续更新时，这个按钮就得一次次手动点，很容易忘了——封面一直停在几个月前。

- 媒体库设置「媒体库封面」区块新增开关「新片入库后自动更新」，**默认关闭**；
- 打开后，该库每扫完一轮且**确实有新增条目**时，自动用最新入库的海报按当前选中的样式
  重新拼一张封面；
- 走的是和手动按钮**完全相同**的那段代码（`regenerate_cover_for_library`），
  同一套模板与标题参数，不会出现“手动的和自动的不一样”；
- 没选封面样式时开关是空转的（界面会提示），扫描侧也会自己跳过；
- 封面画不出来（库里没海报 / 没装 Pillow）**只记日志**，不影响那一轮扫描的结果落库。

三个提前返回都是刻意的：没开开关不动、没选样式不动、**本轮没新增也不动**——自动更新该
跟“新片”绑定，而不是跟“又扫了一次”绑定，否则每次扫描都白白渲染一次并让客户端缓存失效。

## [2.43.0] - 2026-10-04

### 媒体库路径管理改成「路径 + 存储后端」，不再出现 mount://1/paul_emby/...

媒体库设置里的路径以前是一个多行文本框，要求用户手写 `mount://<挂载ID>/<子目录>`。
那串东西对人没有信息量：`1` 是数据库主键、`paul_emby` 是挂载名，既看不懂也改不动，
配错一次（少个斜杠、ID 填错）还只能等扫描报错才知道。

- **路径列表**：一行一条，写的就是目录本身（`/电影`、`/video/剧集/国产剧`），右边一个
  文件夹图标（就地浏览/更换这个目录）与一个 ✕（删掉这条）。
- **添加路径**：点开先选**存储后端**（本地文件 / Rclone / 115 网盘），远程后端再选具体
  挂载（同一后端可以有多个），然后逐级浏览目录选一个；支持多选批量追加。
- **本机目录也能浏览**：新增 `GET /api/admin/mounts/local-dirs`，与挂载浏览返回同一套
  结构，前端只写一套列表 UI；带地址栏可直接输入绝对路径跳过去。
- **前缀路径的老用法保留**：不建挂载、直接写 `rclone:gdrive/Movies` 这类来源收在
  「高级」折叠块里，扫描器本来就认。

后端：

- `Library` 新增 `storage_backends` 列（与 `paths` 逐条对应，纯展示用）。
  **`paths` 的入库形态一点没变**——扫描、播放、追新仍按 `mount://` 前缀解析，多这一列
  不会让任何来源被扫两次。
- 保存时 `assemble_library_path` 自动把前缀拼回去（裸路径 + 后端 + 挂载 id ⇒
  `mount://3/电影`），读列表时 `library_path_entries` 自动拆开显示。
- **老数据照常回显**：没有 `storage_backends` 的库按「挂载类型 + 路径前缀」现推后端；
  挂载被删又没记过后端的显示「未知来源」而不是猜一个（猜错比空白更误导）。
- 后端与挂载类型对不上（如选了 115 却选了 rclone 挂载）直接 400，不让自相矛盾的来源入库。
- 存储后端下拉项由后端 `storage_backends` 字段下发，前端不自己维护一份。

## [2.42.16] - 2026-10-04

### 远程探测第二跳：moov 在文件尾的 MP4/MOV 改用并行双 Range

上一版（2.42.14）把「ffprobe 跑完却没时长」从失败改成可播放终态，但**探测本身没修**：
存量那 2.6 万条依旧探不出时长。这一版改的是探测怎么读。

- **第一跳不变**：还是只取前 1 MiB。mkv（时长写在 EBML 头部）和头带 moov 的 mp4
  一次就搞定，**90% 的文件速度零影响**。
- **第二跳（新增）**：仅当第一跳没读出时长、且容器是 MP4/MOV 系（moov 可能在文件尾）、
  且文件大于 1 MiB 时触发。**并行**发两个 Range（前 1 MiB + 后 2 MiB，尾部窗口可用
  `PROBE_REMOTE_TAIL_BYTES` 调），从尾段里把 moov 盒子摘出来，拼成「头部 + moov」的
  小文件再喂 ffprobe，并把 mdat 的声明长度改写到恰好停在 moov 之前。
  流量固定在两个窗口，与文件多大无关：8 GB 的片子也只读 3 MiB。
- **旧回退保留**：双 Range 失手（尾窗口里没有完整 moov、分片 mp4、HTTP 错误）仍会退回
  原来的「去掉 Range 让 ffprobe 自己 seek」，行为不倒退。

三个实现细节都是实测出来的，不是推测（用 ffmpeg 7.0.2 在真实 MP4/MOV 上验过）：

1. **头尾直接首尾相接不行**：头部里 mdat 声明了整个媒体的长度，demuxer 跳完它就 EOF，
   实测报 `moov atom not found`。必须改写 mdat 长度指到 moov。
2. **不能用「按真实偏移回填 + 稀疏文件」**：那样 moov 位置天然正确，但文件逻辑大小等于
   原文件。实测 ext4 与 overlayfs **不会真的挖洞**（8 GiB 的空洞实打实占了 8 GiB），
   一次探测就能把磁盘吃穿。改成「头部 + moov」后落盘只有 1 MiB 出头。
3. **容器码率与大小要还原**：时长、分辨率、编码、帧率、声道都写在 moov 里，不受影响；
   但 ffprobe 的 `format.bit_rate` 和 `format.size` 是拿「文件大小 ÷ 时长」现算的，
   合成文件只有 1 MiB 出头。实测 224 MiB 的文件被算成 **19 kb/s**（真值 3134），
   不还原就等于把一堆假码率写进库、显示在客户端「媒体信息」页上。

**存量 2.6 万条**：仍需手动执行
`POST /api/admin/emby/scrape/probe/retry-failed` 拉回队列。上一版捞过的条目会停在
`probed_no_duration` / `degraded`，新逻辑对他们没用，所以该接口新增了可选的
`statuses` 参数（默认不变）：

```
POST /api/admin/emby/scrape/probe/retry-failed?statuses=failed,degraded,probed_no_duration
```

## [2.42.15] - 2026-10-04

### 「存储来源」独立页撒销，存储全部按服务器配

v2.42.13 把 rclone 配置搬进「添加服务器」并开了头，这一版把剩下的收完 —— 包括当初
明确说过「等浏览与体检也搬过去再摘菜单」的那部分。

- **目录浏览**：从那一页的表格状态里抽成独立组件 `MountBrowseDialog`
  （自己管路径 / 条目 / 加载态），挂载行的「浏览」按钮直接用它。
  115 / rclone 挂载点目录仍可把它回写成挂载根（并提醒重新扫描）。
- **挂载体检**：「本机体检」按钮搬进挂载区，逐条跑与「测试连接」相同的探测并落库。
- **编辑挂载**：挂载行新增「编辑」，改名称 / 类型 / 路径；换来源时照旧弹
  「请重新扫描」（复用 `mount_type_changed`）。
- **页面与菜单删除**：`StorageMounts.vue` 与它独占的 `RcloneConfEditor.vue` 一并删除
  （后者已被服务器弹窗里的 rclone 文本框取代）。侧边栏菜单、数据概览的「存储来源」
  卡片、服务器页那个跳转按钮全部改指服务器页。**旧路由 `/mounts` 保留为跳转到
  `/servers`**，别人的书签不至于 404。
- 文档（README / DEPLOY.md / 运维文档）里「去存储来源页粘 rclone.conf」的指引改成
  「去服务器页编辑对应 EA」。

后端接口一个没删 —— 只是管理入口换了地方。

## [2.42.14] - 2026-10-04

### 探测失败处理：只有「文件真的不在了」才判死

线上症状：2.6 万个文件探测失败，报错全是「ffprobe 未返回有效时长」，93% 在
MoviePilot 目录；重试 5 轮后集体标成永久 `failed`，用户端显示「网络错误或者当前媒体库
不存在该项目」——**而文件本身存在且可访问**，只是 ffprobe 读不出时长。

- **ffprobe 跑完但没时长 → ``probed_no_duration``**，不再是失败。播放器不靠时长也能播
  （进度条按字节估算），所以这批条目必须留在可播状态里。记为终态、不计失败次数、
  不排重试——否则它们会反复占满 worker 名额，把真能探到的文件挤到后面。
- **永久失败只认「文件真的不在了」**：``PERMANENT_ERRORS`` 从 7 个码收窄到
  ``not_found`` / ``http_404`` / ``http_410``。原来的 400 / 405 / 415 / 416 在云盘与
  反代后面绝大多数不是「文件坏了」（签名 URL 过期、网关回 415、Range 被中间层改写），
  当成永久失败 = 把还在线播放的条目判死。它们现在走指数退避，真正不支持的格式仍会在
  ``PROBE_MAX_ATTEMPTS`` 后自行转 failed，只是不再第一次就被判死。
- **新状态不会被扫描重新排队**：``needs_probe`` 把 ``probed_no_duration`` 与
  ``degraded`` 同等对待（已探过就不再探，除非文件大小变了）。
- **「重新探测」不再是摆设**：``boost_probe`` 以前只把 ``failed`` 改回 pending，
  而 ``_claim_batch`` 只取 pending——所以对 degraded / 无时长条目调用它等于没插队。
  现在这些终态一律重新入队。
- **存量捞回**：修正分类只阻止新增。生产里已经堆着的那批 ``failed`` 由后台
  ``POST /api/admin/emby/scrape/probe/retry-failed`` 拉回队列（只动 failed，
  不碰已探完的 done / degraded / probed_no_duration）。
- **用户端**：探测不出时长时显示「**时长未知**」而不是「—」。「—」读起来像「这项数据
  不存在」，会让人以为片子坏了；只对可播条目（电影 / 剧集 / 视频）这么写，
  剧集 / 季本来就没时长，不挂这个标签。

## [2.42.13] - 2026-10-04

### rclone 配置并入「添加服务器」（对标 oceancloud 面板）

参考 oceancloud「添加 EA 服务器」弹窗的做法：**配置按服务器统一管一次，媒体库只选前缀**，
不再让每条挂载重复贴一遍凭据。

- **每台 EA 一份 rclone.conf**：落盘到 `data/rclone/rclone-<server_id>.conf`（同目录，
  容器里那处是 bind 挂载，rclone / rclone-serve 容器看到的是同一批文件）。挂载新增
  `storage_mounts.server_id`，由 `RcloneMount` 按服务器取对应那份；没绑服务器的老数据
  回退到共享的 `data/rclone/rclone.conf`（宁可多台共用，也不让老数据直接不可用）。
- **后台「添加 / 编辑服务器」弹窗里直接粘**：多行文本框粘 `rclone config` 生成的 INI 文本，
  保存前先解析校验（非 INI、缺 `type = ...` 当场 400），落盘权限 600、先写临时文件再改名。
- **安全**：接口只返 remote 名列表，**永不回显原文**（里面全是 token）；要改就重新粘一份覆盖
  （本来就是整份替换）。读写**仅超级管理员**——它等同一份凭据文件。
- **115 配置档支持 UA**：每个配置档可选 UA（预置下拉由后端下发，不在前端维护一份），
  解析优先级与 Cookie **完全一致**（显式 > 媒体库绑定 > 默认档 > 服务器级），
  避免 Cookie 用默认档、UA 却用停用档这种串味。
- **挂载建在服务器弹窗里**：按 EA 列出该服务器的挂载，可新增 / 测试 / 启停 / 删除；
  新增时三种类型切换，**前缀自动带**（本地任意绝对路径 · `115:/` · `rclone:`）。
- **媒体库路径选择也换成三类型切换 + 前缀自动带**（`MountSourceEditor`，服务器弹窗与媒体库共用）。
- **存量数据不自动删**：已下线类型的残留行照旧保留，列表直接标红并写清怎么改成三种之一
  （这些后端 rclone 都支持，粘一份 rclone.conf 即可）。`build_provider` 的报错也一并改成
  可操作的说法，而不是只报一个「不支持的挂载类型」让人猜改哪一栏。

### 未完成

- **独立的「存储来源」页没有删**。该页还承担着目录浏览与 EA 挂载体检，这两项尚未并入
  服务器页；直接删路由会让它们无处可去。功能已部分并入（服务器弹窗可建/测/启停/删），
  等浏览与体检也搬过去再摘菜单，避免出现「入口没了、能力也没了」的中间态。

## [2.42.12] - 2026-10-03

### 存储挂载精简到三种（只做能力），挂载熔断器整体删除

- **只剩三种来源，且类型由路径前缀决定**：本地硬盘（`/media` 开头）、
  115 网盘（`115:/` 开头）、rclone（`rclone:` 开头，形如 `rclone:gdrive/Movies`）。
  后台不再有类型下拉——这三种的配置本来就只有一条路径加少量可选项，多一个下拉
  只会让人选出不匹配的组合。认不出来的前缀保存时直接 400 并写清正确写法，
  不静默按调用方给的类型存。
- **删干净其余类型**：WebDAV / AList / S3 / 阿里云盘 / 夸克 / OneDrive /
  Google Drive 原生，连同 STRM 独立类型一起删除——后端模块（`mount_cloud.py`、
  `mount_google.py`、`direct_url.py`、`file_id_cache.py`）、模型
  （`RcloneRemote` / `ServiceAccountFile` / `MountFileIdCache`）、前端界面与
  文档一并移除。STRM 的**能力**没丢：`.strm` 小文件改由本地硬盘挂载承载。
  它们现在都应该改走 rclone（这类后端 rclone 全都支持）。
- **rclone.conf 完全由用户自理**：删掉「rclone remote 管理」页与
  `rclone_admin.py` / `rclone_manager.py`（面板不再代管凭据、不再从库里生成配置）。
  改成在后台「rclone.conf」页粘贴标准 INI 文本，落盘到
  `data/rclone/rclone.conf`（权限 600，先写临时文件再改名），每条 rclone 命令
  自动带 `--config` 指过去。**原文不回显**（里面全是 token）。rclone 装在哪、
  配置写在哪由用户决定，不写死。
- **挂载熔断器整块删除**（`mounts.py` 里的 `MountBreakerOpen` / `_BREAKERS` /
  `mount_breaker_open` / `mount_breaker_stats` / `breaker_reset`，以及
  `enrich_worker` / `server_ops` / 进度接口 / 运维页上的「挂载熔断中」卡片）。
  它把「网盘临时连不上」和「配置写错了」当成同一件事，前者被误判成后者，
  结果整个挂载被拉黑几分钟。
- **留下来的是它的另一半**：「存储读不动 → 条目打回 pending + 长退避」这条不删。
  否则一个挂不上的 remote 会让整库条目 5 次尝试后全部终结，补全队列再也回不来。
  原来的 `MOUNT_BREAKER_RETRY_SEC` 随之改名为 `MOUNT_UNAVAILABLE_RETRY_SEC`
  （讲的是自己的事情，不再跟着熔断器消失）。
- **换来源会明确提醒重扫**：改一条挂载的路径前缀（`115:/` ↔ `rclone:` ↔ `/media`）
  就等于换来源，后端回传 `mount_type_changed`，后台弹提示要求重新扫描对应媒体库。
  原因：库里已入库条目的 `mount://<id>/<rel>` 是**旧**来源解释出来的，换提供者后
  不会自己改口径——不提醒就会静默地把一堆条目解释错。
- **数据库迁移不自动执行**：三张没人再读的表由
  `scripts/drop_removed_mount_tables.py` 清理，必须显式 `--yes` 才动手，先打印
  会删多少行。不删也不影响功能（ORM 里已经没有了）。发行镜像按设计不含
  `scripts/`（客户看不到源码），所以 DEPLOY.md 与运维文档给的是等价的 `psql`
  删表命令，自建部署才用脚本。

### 未做真机点检

本次没有做「登录 ghcr → pull → up」的端到端点检（需要真实包权限，沙箱跑不了）。
已验证的是：12 项 `check_*.py` 全过、pytest 1158 passed、双端 type-check + build
rc=0，存储挂载 / 挂载体检 / 可达性 / 扫描队列 / 队列并发演练等冒烟全部通过
（后者从「真 WebDAV 服务」改成「真 rclone RC 服务」，保留真 HTTP、真延迟与服务端
观测，只是换了协议）。

### 已知遗留

- `probe_worker` 的**配额熔断器**（连续 HTTP 403 暂停 worker）**保留**：它保护的是
  上游配额，与被删的挂载熔断器不是一回事，文案已从「Google Drive 配额」改为
  「上游配额」。
- 升级前存在的 WebDAV / AList / S3 等旧类型挂载行会变成「不支持的挂载类型」并
  **大声报错**（而不是静默当空库）；管理员需在后台删掉或改绑成 rclone 挂载。
  这是有意选的失败形态——问题会当场出现在挂载列表里，而不是让扫描悄悄扫出空库。

## [2.42.11] - 2026-10-03

### 授权版环境变量：修掉两个「照文档走仍然起不来」的阻断

授权客户的部署动作只有「照 DEPLOY.md 敲几条命令」，所以 `env.example`、
编排与文档之间的任何不一致都是**启动阻断**，而不是文档瑕疵。逐项对照后发现
两个真的会把客户卡住的问题，**均用真容器复现过**。

- **改密码后连不上自己的数据库**：`env.example` 里写死了
  `DATABASE_URL=postgresql://aetrix:aetrix@...`，而 DEPLOY.md 让客户用 sed 改
  `POSTGRES_PASSWORD`。两处密码从此不一致，后端一直报
  `password authentication failed for user "aetrix"`，容器反复重启，
  而报错里完全看不出是哪一行配错了。
  实测：起真 PostgreSQL，用 `aetrix` 连 → `FATAL`；用 sed 后的密码连 → 成功。
  修法：**`env.example` 不再出现 `DATABASE_URL`**，连接串交给
  `docker-compose.prod.yml` 拼（它内部已经嵌套引用 `POSTGRES_PASSWORD`）。
  实测修完后：渲染出的 URL 密码与 PG 密码一致，且真连 + 建表写入成功。
- **服务永远等不到就绪**：`env.example` 里 `REDIS_ENABLED=false`，但
  compose 的健康检查硬断言 Redis 里有 worker 心跳，且 `backend/worker.py`
  在 `REDIS_ENABLED=false` 时直接 `return 1` 拒绝启动。
  实测：真跑 worker → 「Redis 不可用…worker 拒绝启动」。
  也就是说 false 不是「关掉缓存跑得更快」，而是永远不 healthy。
  修法：改成 `true`，并写明「只有不用 compose 的单机测试才可能需要 false」。
- **`PORT` / `HOST` 是死变量**：授权版对外端口是 `AETRIX_PORT`，
  compose 不读 `PORT`/`HOST`。留着生效值会让客户改了以为有用、实际白改。
  已降为注释并标注原因。
- **补齐 5 个 compose 里用到但 env.example 没提的变量**：
  `EA_INTERNAL_PORT`（固定绑 127.0.0.1，标注「不要改」）、
  `RCLONE_CONFIG_DIR` / `RCLONE_SA_DIR` / `RCLONE_SERVE_REMOTE` /
  `RCLONE_VFS_CACHE_MIN_FREE`。客户在 compose 里看到 `${...}` 不至于一头雾水。
- **DEPLOY.md 同步**：第 3 步加了「确认两行都改到了」的检查命令与 macOS
  `sed -i ''` 的差异提示；新增「必须改 / 不用动」两张表（后者点名
  `REDIS_ENABLED`、`DATABASE_URL`、`MEDIA_ROOT`、`EMBY_PUBLIC_URL`）；
  常见问题补上本次两个故障的**症状 → 排查命令**。
- **新增 `scripts/check_env_contract.py`（接入 CI）**：把上述不变量写成断言。
  已做反向验证 10 项（含逐字还原上面两个真实 bug），全部 rc=1 被捕获。

> 过程中的一个教训：第一版修法把 `DATABASE_URL` 写成
> `postgresql://aetrix:${POSTGRES_PASSWORD}@...`，看着能自动同步，实测渲染出的是
> **空密码** —— `env_file:` 的值不做插值，插值只认 shell / `--env-file` / 同目录
> `.env`。是这个「先验证再改」的过程把它挡下来的，否则等于用一个更隐蔽的故障
> 换掉原来那个。

未做真机点检：完整「登录 ghcr → pull → up」需真实包权限。但本次两个故障
已在本地用真 PostgreSQL / 真 worker 复现并验证修好。


## [2.42.10] - 2026-10-03

### 发行镜像拆分 target：自建能跑运维脚本，客户镜像仍然不带

[2.42.9] 把后端镜像改成白名单 COPY（不带源码），但连带把 `scripts/` 也排除了，
于是 `docker exec` 进容器跑运维脚本（造管理员、迁库、备份恢复）这条路断了。
本条用一个独立的构建 target 把两个需求分开：**运维脚本进自建镜像，不进客户镜像**。

- **`Dockerfile.backend` 拆成三个 stage**：`base`（白名单拷贝，即发行内容）
  → `with-scripts`（补 `scripts/`，自建/开发用）→ `runtime`（空收尾 stage）。
- **stage 顺序是刻意的，不是随手排的**：`docker build` 不带 `--target` 时构建的是
  **最后一个** stage。若把 `with-scripts` 放在末尾，任何人执行
  `docker build -f Dockerfile.backend .` 都会拿到带测试脚本的镜像 ——
  而发布流水线当时正是这么调的，会**静默**推到 ghcr.io。末尾空壳让默认值始终等于
  精简版，发布脚本另外显式写 `--target runtime`，两道保险。
- **`.dockerignore` 不再排除 `scripts/`**：排除它的话 `with-scripts` 那行 COPY 会
  **静默产出空目录** —— 不报错、构建绿、镜像正常起来，直到 exec 进去才发现一个脚本
  都没有。放行的代价仅是构建上下文多约 2 MB（实测 2.3 MB），且发行镜像走白名单，
  不会因此带上它。
- **两条路径在 compose 层面就分开**：`docker-compose.yml`（自建）的 `aetrix-api`
  用 `target: with-scripts`；`docker-compose.prod.yml`（客户）只拉 ghcr 镜像，
  无 `build:`、无 `target:`。
- **新增出厂自检断言**：`publish-images.yml` 在推送前检查镜像里 `/app/scripts`
  条目数为 0 —— 万一 `--target` 写错或 stage 顺序被调，推送前就停下。
- **新增静态门禁 `scripts/check_image_targets.py`**（已接入 CI）：把上述不变量
  写死成断言。它们的共同点是**破坏时不报错**，review 看不住。已做反向验证：
  逐个破坏 9 个不变量（含「三个 stage 名都在、只把 runtime 挪到中间」这种
  最隐蔽的），全部 rc=1 被捕获。

实测：两个 target 均构建通过；`runtime` 的 `/app` 恰好 5 项、`/app/scripts` 0 个，
`with-scripts` 6 项、82 个脚本；ops 镜像内 `python scripts/create_admin.py --dry-run`
真跑通（能 `import backend`、能建库），精简镜像内同命令报 `No such file`。
两侧 compose 渲染均 rc=0。未做真机点检：完整「登录 ghcr → pull → up」需真实包权限。

## [2.42.9] - 2026-10-03

### 私有镜像发布流程（授权客户部署，不再给源码）

仓库转私有后，授权客户走「拉私有镜像」而不是「拿到源码自己部署」。本条把这条
链路从构建到交付全部打通，并顺手修掉两个**以前从未被验证过**的镜像构建问题。

- **发布工作流**（`.github/workflows/publish-images.yml`）：监听 CI 的完成事件，
  **只有 main 的 push 且 CI 全绿才发镜像**。直接 `on: push` 的话，CI 跑红的合并
  也会发，而 ghcr.io 的 `latest` 一被覆盖就无法回退。
  拒绝来自 fork 的 PR 触发（`workflow_run` 对它们的 CI 同样会触发，不加
  event/head_branch 判断的话，一个 fork 分支变绿就能往 ghcr 推镜像）。
  镜像：`ghcr.io/xzb177/aetrix-api` 与 `ghcr.io/xzb177/aetrix-web`，
  各打三个 tag：版本号、`sha-<短哈希>`、`latest`。
- **镜像里不再带源码**：后端 Dockerfile 原来是 `COPY . /app`，会把两个前端的完整
  TypeScript 源码、tests/、docs/、scripts/ 一并打进镜像。改为**白名单 COPY**
  （`backend/`、`emby_api/`、`web_player/`、`serve.py`、`VERSION`）。
  用白名单而不是加黑名单规则，是因为黑名单永远会漏：以后新增一个顶层目录，
  忘了写进 .dockerignore 就跟着进镜像了；白名单则是「不写就不进」。
  这五条白名单每一条都有具体理由，其中 `emby_api/` 最隐蔽 ——
  `backend/run_all.py` 是用 `python -m emby_api.main` 这个**字符串**拉起它的，
  搜 import 语句搜不到，漏了容器起来就只剩一个进程。
  顺带把 `venv/` 加进 .dockerignore（此前它整个进了构建上下文）。
- **修前端镜像构建失败**：`Dockerfile.frontend` 原来在 `node:22-alpine` 上构建，
  实测必挂：`Source phase import "vite/modulepreload-polyfill" in "index.html" must
  be external`（发生在 vite 还没转换到业务模块时）。同样的 package-lock.json 在
  CI（ubuntu + node 22）是绿的，所以只出在 musl 这条路径；rollup 的 musl native 包
  安装是正常的（`@rollup/rollup-linux-x64-musl` 确实装上了），不是依赖缺失。
  构建阶段改用 `node:22-slim`，把构建环境与 CI 对齐；运行阶段仍是 nginx:alpine，
  镜像体积不受影响（vite 产物是纯静态文件）。
  **这条以前从没暴露过**：CI 从来不构建 Docker 镜像。
- **生产部署编排**（`docker-compose.prod.yml`）：只拉官方镜像、不含 `build:`，
  与自建用的 `docker-compose.yml` 并存（一个 build、一个 pull，职责不混）。
  `AETRIX_IMAGE_TAG` 选版本，留空跟随 `latest`。
- **`DEPLOY.md`**：客户侧 3 步（装 Docker → 登录 ghcr → `docker compose up`），
  含 PAT 生成步骤、版本升级/回滚、授权权限的授予与回收。

**关于「收权」的边界（DEPLOY.md 里已写明，不藏）：** 移除 GitHub 协作者只让对方
**之后**拉不到；已经 pull 到客户机器上的镜像不会被远程删除，要真正收回必须客户
自己 `docker image rm`。所以交付时不该让客户锁 `latest`，而应按授权单独发版本
tag 的凭据。

**未做真机点检：** 整个客户链路（真实 GHCR 登录 → pull → up）需要真实的包权限，
沙箱里无法端到端跑。本地已验证到：两个镜像都能构建、后端镜像内只有 5 个白名单条目
且无 `.git`／无 `.vue`、run_all 三进程（api/worker/ea）在 Redis 下全活、
`/api/health` 与 EA 的 `/watch` 均返回 200、生产 compose 用 `env.example` 渲染通过。

## [2.42.8] - 2026-10-01

### 用户端顶栏导航重做（只改样式）+ 播放线路选择（直连 / 中转）

本条版本里有两个同日合并的改动，各自已随 PR 独立交付：顶栏是**纯样式**收敛
（用户实拍反馈），线路选择是**新功能**（用户维度偏好）。本节只做版本标记与记录。

- **顶栏导航重做（PR #247）**：实拍照片里顶栏右侧挤着「⚡ 1,021」积分胶囊与
  「会员生效中」胶囊（红圈处），而这两条信息在首页资产卡与用户菜单里都已经有了 ——
  顶栏是个重复入口，手机上（≤768px）尤其挤。改法：
  - 撤掉两颗 pill，并清掉它们整条数据链：`pointsApi` 引入、`pointsBalance`、
    每轮轮询里的 `refreshPoints()`、`showSubPill` / `subPillText` / `isFreeRealm`
    三个 computed，以及 `.assets-group` / `.points-chip` / `.sub-chip` 与呼吸动画
    样式（含 prefers-reduced-motion 块）。**会员状态没有消失**：用户菜单头部的会员
    徽章（`menuBadge`）与 `refreshSubscription` 原样保留。
  - 右侧消息（信封）与头像改成**统一圆形图标按钮**（`.round-btn`：38×38、
    `--au-r-full`、`--au-surface` 底 + 1px 描边、悬停升一档、按下 scale .96、
    键盘焦点环），对齐管理台顶栏的圆形图标按钮语言；消息按钮保留「展开中」与
    「有未读告警」两态。
  - 顶栏昵称文字删除（按管理台实拍那种「只留头像圆圈」），昵称改由 `aria-label` /
    `title` 承载 —— 读屏与悬停仍能知道这是谁的菜单。
  - 顶栏外观切换器改用与管理端侧栏抽屉同一套配方：槽间距 3→6px、内距 3→4px、
    圆角胶囊→`--au-r-md`，选项 `--au-r-sm`，选中态仍是主色实底 +
    `--au-on-primary`（深色主题下深字、浅色下白字）+ 微光晕，字重加粗到 700。
    移动端段高保持 40px（可点面积不缩）。
  - `AppHeader.vue` 1311 → 1119 行（净 −191）。
- **播放线路选择（PR #248）**：新增用户维度的「直连 / 中转」开关，默认直连，
  老用户行为不变。
  - 后端：新表 `user_play_lines`（启动时 `create_all` 自动建，升级部署无需手工迁移）、
    `backend/emby_server/play_line.py`、`GET` / `PUT /api/user/emby/play-line`。
  - 播放决策：`video_stream` 对可 302 的来源先读偏好 —— **relay 跳过一切 302**
    直接交给服务器代理转发（流量过 VPS，适合客户端直连网盘不通、一直转圈的用户）；
    **direct 保持原行为**（先试 Google 直链 302 → `?direct=true` → 代理）。
  - 读取 fail-open：无记录 / 脏数据 / 读取异常一律按 direct —— 偏好只是辅助，
    读不到不能让播放 500；写入非法值返回 400。
  - 用户端：个人中心新增「播放设置」面板，两档分段选择（沿用主题切换器的视觉语言），
    切换后重新播放生效；`src/api/user.ts` 封装接口。
  - 测试：`tests/test_play_line.py`（10 例：偏好读写 / 用户隔离 / 非法回退 +
    `video_stream` 三分支）。

## [2.42.7] - 2026-10-01

### 修复：合并容器里后台任务跑了两份（`AETRIX_ROLE=all`）

线上实测：`docker logs aetrix-api | grep -c "补全 worker 启动"` 返回 **2** —— 同一套
后台任务在两个进程里各起了一份。

根因：合并容器用 `backend/run_all.py` 起 api / worker / ea 三个子进程，而它
`Popen(..., env=os.environ.copy())` 不覆盖角色，容器 env 又是 `AETRIX_ROLE=all`；
`backend/main.py` 只把 `"api"` 认成 API 角色（`_is_api_role = _role == "api"`），
`all` 于是被当成「单体模式」，`serve.py` 这个子进程也在 lifespan 里把 janitor /
probe / enrich / reminders / auto_scan / db_backup / chase_new 整套后台任务起了一遍，
而 `backend/worker.py` 子进程同样起一套。`start()` 的幂等只靠模块级变量，跨进程无效。

实际影响（都不看日志发现不了）：

- 补全 worker 是 **2×ENRICH_WORKERS** 线程、**两个互不相通的 TMDB 令牌桶**
  （`_tmdb_limiter` 是模块级、进程内状态），限额被打成两倍；
- 定时扫描调度器有两个，同一份定时配置会被两个进程各排一次；
- `scan_queue` 一直停在**单体模式的进程内队列**上（`if AETRIX_ROLE == "api":` 六个
  分支全不成立），v2.41 的 api/worker 拆分从未真正生效 —— 扫描一直在 API 进程里跑。

修法：`run_all.start()` 给 **api 子进程**显式注入 `AETRIX_ROLE=api`，并把有效角色打进
启动日志（上一次正是「三个子进程共用一个 all」从日志上看不出来）。`worker` / `ea`
沿用容器 env 不动：只有 `"api"` 会被特殊对待，worker 靠「`!= api`」走进程内队列正是
它需要的，`ea`（emby_api）整棵树不读这个变量。

安全性核对：`backend/main.py` 里 `if not _is_api_role:` 门住的 7 个后台任务，
`backend/worker.py` 的启动清单**逐条对应**（worker 还多一个 Redis 扫描队列消费线程），
没有任何任务是只有 main.py 会起的；`scripts/update.sh` 读的是 `docker inspect` 的
**容器级** env，不受子进程注入影响。另新增 `tests/test_run_all_roles.py`：钉住角色
注入、worker/ea 不被覆盖、以及「worker 必须接过角色门后的每一个后台任务」这条前提。

- **管理端页脚版本号修正（同一类「标记不可信」的病）**：`admin_frontend/src/
  composables/branding.ts` 的 `APP_VERSION` 停在 `v2.33.0`，而 `VERSION` 已到
  2.42.6 —— 页脚「Aetrix v2.33.0」**永远显示同一个值**，根本没法用来看线上跑的
  是哪个构建（而这正是部署验证最需要它的场景）。现同步成 `v2.42.7`，并把这一项
  纳入 `scripts/check_version.py` 门禁：以后前端页脚版本与 `VERSION` 漂了，CI 直接拦。

### 修复：拆分布署下管理端「扫描队列」看不到「正在扫描 / 最近完成」

扫描真正搬到 worker 进程执行之后（本版把角色注入修好，v2.41 的拆分才首次真正生效），
紧接着暴露出面板的一个后续问题：

`scan_queue.snapshot()` 的「正在扫描」「最近完成」两列读的是 `_RUNNING` / `_HISTORY`，
这两个都是**进程内**状态。API 进程只负责把扫描请求推进 Redis（`enqueue()` 在
`AETRIX_ROLE=api` 下走桥接），任务的内存队列在 worker 里，所以这两列在 API 进程里
**结构上永远是空的**：面板上只剩 Redis 的「排队中」，扫完（队列一空）连整个「扫描队列」
卡片都直接不显示。

修法：谁执行扫描，决定这两列从哪来。

- 本进程就是执行者（单体 / worker）：仍用进程内队列，实时、带排队原因，与升级前一致；
- 本进程不是执行者（API 角色 + Redis 桥接可用）：从**库里的跨进程状态**合成
  （`Library.scan_status` / `scan_progress` / `last_scan_at`）——worker 每
  `SCAN_PROGRESS_FLUSH_SECONDS` 秒把进度刷进库、结束时写结果，这些字段本来就是
  跨进程的。每条标 `via="db"`，快照里带 `view` 字段，面板据此写明
  「进度来自执行节点」，不把刷盘快照当实时值卖。

顺带修掉同一片区域的两个小问题：

- Redis 排队项里只有 `library_id` / `trigger`，「排队中」列于是只有位置、没有库名
  ——现在从库里补上名字；
- 库里不留触发方与排队时长，面板显示「—」/ 留空，不编一个看起来像真的数字。

读库失败时两列退化为空（不抛异常）：前端每 3 秒轮询一次这个接口，少两列也比
整个面板 500 强。

## [2.42.6] - 2026-10-01

### 用户端 UI 重做（批次 1）：7 处线上问题修复 + 双主题硬编码收敛

本轮是用户端 UI 重做的第一批：先把用户实拍反馈的 7 处问题修掉，
并清掉「深色主题写死色值」这一类根因（它同时导致多处浅色下不可读）。
后台继续用 Element Plus，只做样式与令牌层修正，不动组件体系。

> ①②③④⑤⑥ 随 PR #242 合并（merge `6e521df`）；**⑦ 是本批次的补遗**，
> 因改动完成时 #242 已被合并，故另开 PR 单独交付，其余条目不变。

- **① 商店/钱包卡码兑换框主题串色**：根因是 `.au-input` 直接写死
  `rgba(7,11,18,.55)` 的深色底（`styles/index.css` 的 `.input` 同病），
  不看主题——浅色页面上就是一块深灰输入框。新增语义令牌
  `--au-input-bg / --au-input-bg-focus / --au-input-border`（深色＝原值，
  浅色＝白底 + 略深描边），两处输入框改消费令牌，组件与页面零改动。
  实测：浅色下输入框 `#ffffff`、文字对比度 14:1；深色不变。
  主按钮保留品牌渐变（`--au-gradient`，浅色下已是压深过的深青→深紫，白字）。
- **② 头像抽屉「外观」切换器选中态发虚**：旧版只有 10% 青底 + 淡描边，
  浅色下几乎看不出选了哪一档。改为**主色实底胶囊**（`--au-primary` 底 +
  `--au-on-primary` 字 + 微光晕），段高 34→40px（手机）、图标与文字对齐、
  补 `:focus-visible` 键盘焦点环；三档宽度等分且不裁切。
- **③ 首页顶栏资产胶囊挤在一起**：旧版手机端间距压到 0.25rem、高度缩到 28px，
  两颗胶囊视觉上粘成一坨。改为间距 0.5rem（超窄屏 0.375rem）、
  高度与桌面一致 30px、统一内距，省空间改从品牌 mark 与容器间距上找；
  「会员生效中」字号不下调、不裁切。实测 375 / 390px 下间距 6/8/8px、
  两胶囊等高、无横向溢出（最坏情况「1,012」四位数积分）。
- **④ 后台「服务器与线路」页说明文像一堵墙**：根因不在那一页，而是全站共用的
  `.admin-page-desc` 用了 `--font-size-2xl`（20px）。统一降为正文 14px +
  次要文字色 + 1.7 行高；涉及其它 10 个页面同步生效（已逐页复核）。
- **⑤ 后台支付网关警告横幅对比度太低**：旧版写死 `#fde68a`（浅黄）压浅琥珀底，
  浅色下几乎读不出来。改走语义令牌 `--warning-strong` / `--success-strong`，
  字重 500。实测对比度 **1.4:1 → 6.65:1（浅色）/ 9.18:1（深色）**。
- **⑥ 后台侧栏抽屉外观切换器太窄太挤**：段高 28→34px、间距 4→6px、
  内距 4→6px，选中态同用户端改为主色实底胶囊 + 微光晕，补焦点环；
  与下方账号块之间加 1px 分隔线并把间距拉到 14px（原先只隔 10px，看着叠在一起）。
- **⑦ 后台「测试连接」按钮按下后变成深底深字（线上实拍）**：根因有两层——
  ① `element-plus-theme.css` 里那块「默认按钮强制覆盖」用**直接属性**把 hover/focus
  写成了硬编码深蓝 `#1d2836` + 白色 24% 描边，而且 `:focus` 也在其中：手机点完
  按钮会一直「粘」在这个深色态（截图正是这个状态）；② 实心按钮的文字色走
  Element Plus 自带的组件级变量，它排在主题文件之后、同优先级下取胜，
  于是主色按钮一直是白字（亮青底 1.8:1）。现在：默认按钮的悬停/聚焦/按下
  全部走主题令牌（淡色阶）且排除禁用态，实心/描边/文字按钮的选择器双类化
  以免被 EP 盖掉，禁用态补齐（原先禁用按钮和可用的一样亮）。
  实测（服务器管理弹窗五颗按钮 × 深浅双主题 × 常态/悬停/按下/聚焦/禁用/触摸）：
  主按钮 1.81:1 → 5.45:1（浅色）/ 9.32:1（深色），默认按钮 13.3~15.4:1，
  描边按钮 ≥4.7:1，键盘焦点环可见，触摸点击不再粘住深色态。
- **品牌主色改为随主题分叉（附带根因）**：后台 `branding.ts` 一直往
  `documentElement.style` 写 inline 自定义属性，而 inline 优先级最高——
  `html[data-theme='light']` 里的浅色主色**永远**赢不了它，所以白日模式下
  主色仍是亮青 `#22d3ee`，压白底只有 ~1.9:1（说明文字、描边按钮都受影响）。
  改为注入一段 `<style>`（深色 `:root:root` 用原色，浅色
  `:root:root[data-theme='light']` 把品牌色连乘压暗到白底可读），
  派生色（悬停 / 浅底 / 描边 / 主色上的文字）跟着有效色重算——
  与用户端 v2.42.3 的同一套做法对齐。
- **浅色主题硬编码收敛（根因类）**：把两端仍然是「只按深色主题取值」的写死色
  统一换成已有语义令牌，共 30 余处 —— 后台新增样式/表格徽章/日志 chip/
  Element Plus 的 plain 按钮、错误提示、tag、message、alert、默认按钮的
  悬停与按下底色（原本是深蓝 `#1d2836`），以及 RcloneRemotes 里写死的近白底
  `#f5f7fa`（深色主题下是一块亮斑）。

## [2.42.5] - 2026-09-30

### 管理后台 toast 修复 + 用户端删除媒体库/搜索 + 签到按钮重做

- **修管理后台「所有操作都没反馈」**：根因不是没弹 toast，而是 ElMessage 的
  theme-chalk 基础样式（`position:fixed` 等）根本没进包——各视图都是直接
  `import { ElMessage }`，绕过了 unplugin 的按需样式注入，toast DOM 出现了但
  `position:static` 掉到文档流末尾（视口外）。`main.ts` 手动引入
  message / message-box / loading 三个函数式组件样式，并补注册 `v-loading` 指令。
  实测：失败 toast `position:fixed` 顶部居中可见，双主题下均正确。
- **拦截器统一反馈**：写操作（post/put/patch/del/upload）失败统一弹红色
  ElMessage.error（含后端 detail / 422 校验信息）；查询保持安静；`silent: true`
  跳过。主要操作按钮补 loading 态（公告、卡码、工单、登录日志清理、设备处置），
  防重复点击。
- **用户端删除媒体库与搜索页**（v2.42.4 只退到菜单，本轮连页面一起删）：
  路由 /media、/library、/search、/favorites、/history 及
  LibraryHomeView / LibraryView / SearchView / FavoritesView / HistoryView、
  MediaCard / MediaRow / QuickViewSheet 组件与对应契约测试全部删除；老地址落
  兜底 404。/media/:id 详情与 /watch 播放深链保留（首页/求片/个人中心在用）。
  首页观影数据卡改为纯展示卡；主导航与底部坞仍是三项（首页/商店/我的）——
  生产站看到四项是部署滞后，非代码问题。
- **签到按钮重做**：金渐变实底 + 内高光 + 底部深金描边的「金币打卡」胶囊，
  悬停光泽扫过（纯 transform）、按压回弹、到账弹跳庆祝；右上角连签火苗徽章；
  已签态换成优雅的成功确认胶囊（不再是禁用灰）。金色三档入 aurora.css 令牌。
- **商店充值 tab 占位闪现修复**：充值套餐首载先渲染骨架行（ shimmer），
  请求回来后再渲染空态或列表——不再闪一下「暂无可用充值套餐」。
- **后台审美打磨（验收反馈）**：
  - 仪表盘后端服务状态不再直出英文枚举：`redis_unavailable` 显示「Redis 不可用」，
    非健康状态改用警示色小号字，不再和 30px 大数字抢权重；
  - Element Plus 内置文案全站中文：`ElConfigProvider` + zh-cn locale 注入组件树
    （按需引入不能 `app.use(ElementPlus)`，会全量打包）——分页的
    「Go to previous page / page 2」变成「上一页 / 第 2 页」，表格 / 下拉空态、
    日期选择器等组件库自带英文一并中文化；
  - 表格空单元格统一显示「—」（DataTable 兜底），留白不再既像「没数据」
    又像「没加载出来」；
  - 用户列表：「订阅」列的灰色文字「未订阅」改灰色徽章；角色位始终有值，
    非管理员显示灰色「用户」徽章（之前是空白）；
  - 「存储健康」KPI 脚注允许换行：此前 `nowrap + ellipsis` 把
    「异常 2（目录不存在…」截断成「（日…」，看不到具体是哪些来源；
  - 系统设置能力卡按列打包（`column-width`）：「AI 与智能」「站点与品牌」这类
    只有 1 张卡的分组不再各占一整行、右侧大片留白，分组本身不被拆开。

## [2.42.4] - 2026-09-30

### 导航定稿「首页 / 商店 / 我的」+ 管理后台统一设计语言与双主题

- **主导航定稿**：一级导航收敛为「首页 (/) / 商店 (/wallet) / 我的 (/profile)」
  三项——商店即钱包一体（积分、订阅方案、消费记录都在里面），不再单设钱包入口；
  媒体库与全局搜索退到菜单「观影」分组，底部坞同步。`/wallet` 路由标题改「商店」。
- **首页收敛收尾**：删除「新手任务」区块（含全套数据与样式）；欢迎卡加静态装饰
  光圆（`.hero-orb`，主色低透明度 + 大半径模糊，深浅两主题都验证无阅读干扰）；
  问候标题加大到 20px / 桌面 24px。
- **功能色定稿（换位）**：积分 = 金（warning 系）、订阅 = 青（primary）、
  观影 = 紫。首页资产卡 tone 与顶栏资产 pill（`.points-chip` / `.sub-chip`）
  同步换位，hover 态一致。
- **管理后台统一设计语言**：品牌渐变按钮退役（用户端同款反馈「突兀」），
  `--gradient-brand` 等变量名保留、值改主色实色，历史页面一次性统一；卡片圆角
  16px、阴影减淡；KPI/统计数字统一 `.stat-num`（等宽 + tabular-nums + 800 字重，
  仪表感）；EP tag 药丸化（功能色底 + 描边）、progress 内条改功能色实色。
- **管理后台双主题（白日 / 黑暗 / 跟随系统）**：`tokens.css` 新增
  `html[data-theme='light']` 完整浅色调色板（暖调纸白 #f5f8fb 底、纯白表面、
  深蓝黑文字、主色压深 #0891b2 保 4.5:1，语义色同步压深），EP 变量浅色分叉；
  `useAdminTheme()` 三档持久化（与用户端同 key），侧栏 foot 加「外观」三档分段、
  顶栏加快捷切换；`admin/index.html` 主题预套用脚本消除首屏闪烁，
  meta theme-color 深浅跟随；Layout 写死的深色值（顶栏毛玻璃、chip hover、
  抽屉阴影等）令牌化，浅色不再穿帮。实测（Playwright）：三档切换即改
  `html[data-theme]` 且持久化，浅色 body #f5f8fb、KPI 等宽字体、meta 同步。
- **修 CI 冒烟回归（f0aa598 引入）**：`pop_scan_request` 改用 `_blocking_redis()`
  时直接从 `REDIS_URL` 新建客户端，绕过了单测打在 `_redis()` 上的桩——push 走
  FakeRedis、pop 连真 Redis，无 Redis 的 CI 上 7 个队列用例必炸（Connection
  refused → None），main 近四次 CI 冒烟全红。改为从共享客户端复制连接参数构造
  专用客户端（保留「socket 超时 > 阻塞时长」的修复意图），桩原样复用；
  无 Redis 环境本地复现 → 修复 → 11 passed / 全套 607 passed。

## [2.42.3] - 2026-09-30

### 修复移动端外观切换点不到 + 用户菜单纸片人式重做

移动端反馈「黑暗模式/白日模式/跟随系统点不到、没反应」，排查出三个叠加根因：

- **底部坞盖住下拉**：顶栏与底部坞同为 z-index 50，而坞在 DOM 里靠后——同一
  堆叠上下文内后者在上，下拉菜单的下缘（正好是外观切换和退出登录）被坞压住，
  点到的是坞的空白区。坞降到 40，同上下文内 60（下拉）> 50（顶栏）> 40（坞）。
- **header 的 backdrop-filter 改变了 fixed 的参照物**：filter/transform 会让元素
  成为 fixed 后代的包含块，fixed 面板实际相对 header 而非视口定位（真机自测
  抓到 y=-510 的飞出屏）。两个下拉（消息 / 用户菜单）改为 `Teleport to body`，
  移动端 bottom 锚在 `--au-dock-space` 之上、内部滚动，菜单永不滑进拇指区。
- **品牌色 inline 压过主题覆盖**：站点品牌能力把 `--au-primary` 族写在
  `documentElement.style`（inline 优先级最高），浅色主题的 CSS 覆盖永远赢不了，
  白底上主色仍是亮青（发白看不清）。改为注入 `<style>` 双规则：深色用管理员
  原色，浅色自动把品牌色压暗到白底可读（亮度阈值迭代），派生色跟随重算。

用户菜单同步按纸片人标准重做：头像 + 昵称 + @id + 状态徽章（会员金 / 公益服青 /
未开通灰）头部 → 外观三档（显眼位置，触控目标 36px+）→ 操作项列表（40px 触控、
hover 图标染色、active 压底）→ 红色退出独立分区；rounded-2xl 大圆角 + shadow-2
实底卡片，打开动画柔和上浮缩放（opacity/transform，reduced-motion 降级），
点击外部关闭兼容 Teleport。消息下拉同步受益（同样不再被坞盖住）。

移动端真机自测（Playwright 390×844 触控视口）：三档切换全部生效并持久化、
外观控件与操作项均完整落在视口内且高于底部坞、浅色主色 #137483 / 深色 #22d3ee。

## [2.42.2] - 2026-09-30

### 首页只做资产与服务：砍掉海报墙 + 双主题（白日/黑暗/跟随系统）+ 性能优化

参考的纸片人 dashboard 只有账户资产与服务入口、没有媒体浏览，首页据此继续收敛：

- **首页不再有媒体浏览**：「最近上新」海报墙与空态卡移除，媒体浏览统一走媒体库
  独立页（顶栏 / 底部坞入口不变）。首页结构收敛为：欢迎卡 → 说明条幅 →
  资产卡（积分 / 订阅 / 观影数据）→ 新手任务 → 进行中的事项 / 正在播放 → 帮助中心。
- **说明条幅**：置顶公告（可关闭，本次会话不再弹）优先；无公告时按账号状态兑底
  （公益服免费开放 / 会员未开通），图标走对应功能色。
- **双主题**：新增语义别名层（`--bg-canvas` / `--bg-surface` / `--text-main` /
  `--text-muted` / `--border-subtle` / `--accent-primary`），组件只消费语义层；
  白日模式在 `html[data-theme="light"]` 独立调色（暖调纸白 #f5f8fb 底、纯白表面、
  深蓝黑文字、主色压深两档保证 4.5:1），不是反色。渐变（logo 文字 / 主按钮 /
  头像）同步压深，白字对比达标。三档切换（跟随系统 / 白日 / 黑暗）在头像菜单
  「外观」里，跟随系统实时响应 `prefers-color-scheme`；index.html 预套用脚本
  + 启动画面浅色版消除首屏闪烁；meta theme-color 跟随切换。
- **性能**：卡片级 backdrop-blur 只在桌面深色保留（移动端与浅色不启用，
  多层 blur 是低端机掉帧主因）；临期脉动从逐帧重绘 box-shadow 改为伪元素
  扩散环（只动 opacity/transform）；进度条从改 width 改为 scaleX（合成器线程，
  不触发布局）；装饰光晕保持纯 opacity 无 will-change；入场动画补
  prefers-reduced-motion 降级。
- 加载链路简化：首页不再分「关键 / 延后」两波请求（海报墙没了，首屏只剩骨架
  + 一批资产请求），公告并入同批。
- 质量门禁：双配置 type-check + build 通过；令牌 / 路由 / 版本（2.42.2）/ await /
  品牌门禁全过；pytest 588 passed。

## [2.42.1] - 2026-09-30

### 用户端首页重做：资产仪表盘（借鉴纸片人控制台设计语言）

首页从「内容货架 + 服务台混合体」收敛为「资产仪表盘」口径，借鉴纸片人世界控制台的
设计语言，但品牌色维持 Aetrix 体系（电光青 + 极光紫 + 会员金），不引入新色系：

- **顶栏资产 pill 常驻**：积分（品牌青）与订阅状态（会员金；临期转警示金并轻脉动）
  在所有断点常驻顶栏——原来移动端是直接隐藏积分徽章的；公益服显示「公益服」胶囊，
  不出现任何购买引导。订阅态与未读轮询同频刷新，买完/续完回到任意页面立即更新。
- **四段式资产卡**：积分 / 订阅 / 观影数据三张卡，统一规格（16px 圆角、1px 细边框、
  极克制阴影、移动端 20px / 桌面 28px 内边距）：① 44px 圆角图标盒（功能色 10% 底
  + 20% 边框）+ 标题徽章 ② 巨型等宽数字（移动 30px / 桌面 48px，染功能色，
  订阅卡含按真实周期算的进度条）③ 说明文案 ④ 底部 border-t 分隔条（左灰说明
  + 右功能色 CTA）。每种资产一个固定功能色，从顶栏 pill → 卡片图标 / 数字 / CTA
  全链路同色（青 / 金 / 紫），tone 变量定义收敛在 aurora.css 全局表。
- **hero 收敛为欢迎卡**：右侧会员卡并入订阅资产卡（不再一屏讲两遍会员），
  双栏网格变单列流，保留双光晕装饰（右上青 + 左下紫低透明度 blur）。
- **移动端底部导航坞回归（≤768px）**：与顶栏主导航完全同源（primaryNav），
  毛玻璃底 + 当前项品牌色软底胶囊；≤768px 顶栏退成单行（品牌 + 资产 pill + 账号），
  769~900px 仍走顶栏第二行滑动选项卡。页面底部留白统一走 --au-dock-space，
  不再各自写媒体查询；首页资产卡单列、内容区 padding 收窄。
- **去同义重复**：「我的面板」里与资产卡重复的「我的观影」数字卡移除
  （口径与 v2.10.3 去掉消息卡一致：同一个数据只在一处）；进行中的事项卡
  单独一条时自动占满整行（auto-fit 网格）。
- 质量门禁：user_frontend type-check + build 通过；设计令牌契约 / 路由契约检查通过；
  版本号对齐 2.42.1。

## [2.42.0] - 2026-09-30

### 默认数据库切换到 PostgreSQL + 迁移链路修复

SQLite 是单写锁模型：扫描、播放进度上报、订单并发一撞上就 `database is locked`，
备份也只能做文件级快照。这一版把**默认数据库改成 PostgreSQL** —— `DATABASE_TYPE`
的代码默认值就是 `postgresql`。Docker Compose 部署由 compose 自动配好连接串与
`postgres` 服务，不用手工连线；老部署要继续用 SQLite 必须**显式**写
`DATABASE_TYPE=sqlite`（见 `env.example` 的「SQLite 仅保留给老部署兜底」一节）。
裸机部署注意：没显式设连接串时，它会是 `env.example` 里那个 Compose 内部服务名
`postgres`，连不上就直接退出，**不会静默退回 SQLite** —— 这是有意的。

同时修了一组只在升级现场才踩得到的迁移链路问题
（[PR #221](https://github.com/xzb177/Aetrix-Portal/pull/221)）：

- **跨进程建表竞态**：api / worker / EA 三个进程同时 `init_db` 会互相踩，用迁移锁串行化；
- **孤儿外键与自引用外键**：导入顺序不对会让外键指向不存在的行，按依赖顺序建表；
- 删掉已过时的手写 DDL 迁移脚本，统一走 `scripts/migrate_sqlite_to_pg.py`，并补了迁移文档；
- 新增 PG 兼容门禁 `scripts/smoke_test_pg_compat.py`（建表 / 迁移方言 / 类型读写 / `ilike`），
  `DATETIME` → `TIMESTAMP`、`BOOLEAN DEFAULT 0` → `FALSE` 这类方言映射由它盯住。

### 部署形态：前后端分离，后端合并为单容器

- **前后端分离**：后端只跑 API，不再把前端 `dist` 打进镜像；两个前端由 nginx 容器单独
  serve（`Dockerfile.frontend` + `docker/nginx.conf`），`/api/*` 与 `/emby/*` 反代到后端，
  管理端仍在 `/admin/`。好处是改前端不用重建后端镜像。
- **api + worker + ea 合并为一个容器**（`backend/run_all.py` 同时起三个进程），
  Compose 服务数 8 → 6，少一层编排也少一处配置漂移。
- EA 的内部端口只发布在宿主机本机地址上，公网入口统一走 nginx。

### 借鉴 twilight-kotomi 的四项能力（[PR #227](https://github.com/xzb177/Aetrix-Portal/pull/227)）

- **配置自愈**（`backend/config_self_heal.py`）：启动时按 `env.example` 补 `.env` 缺失项、
  按各模块已有的常量注册表补 `system_configs` 缺失行。纪律是**只补缺失、绝不覆盖**，
  失败只记日志、不拦启动。
- **诱饵码**：`HONEY-` 前缀识别（`backend/codes.py::is_honeypot`），**零表结构改动** ——
  不为它建列。用了诱饵码的人会被自动封禁，而兑换入口统一回「卡码无效」，不暴露这是陷阱。
- **首次运行向导**：空实例打开管理端会进向导建第一个管理员；建完**永久关闭**入口
  （`POST /api/admin/setup` 之后恒返回 403），限流 8 次/分/IP。
- **配置热重载**（`backend/integrations/store.py`）：配置分两档读 —— `read_value(s)` 直查 DB、
  永远最新（后台展示 / daemon 轮询 / 写后回读走这档），`get_value(s)` 走进程内短 TTL 缓存
  （默认 60 秒）。后台改完，同一进程内「写时失效」立即生效，跨进程最多 60 秒生效。

### 管理端深色主题两次修复

- **默认按钮发白**：`el-button` 的默认类型不走 `--el-color-primary`，补规则压住。
- **按需引入后 Element Plus 自带样式盖掉深色变量**
  （[PR #231](https://github.com/xzb177/Aetrix-Portal/pull/231)）：管理端改成 element-plus
  按需引入后，EP 自带样式被打包到 `element-plus-theme.css` **之后**，它那份
  `:root{--el-color-white:#ffffff; --el-fill-color-blank:#ffffff; --el-bg-color:#ffffff;
  --el-color-primary:#409eff}` 以「同权重、后来者胜」压过深色变量 —— 界面退回浅色：
  输入框 / 下拉 / 弹窗发白、主色变 EP 默认蓝、固定列表头露白块。修法是把深色变量块的
  选择器从 `:root` 提到 `html:root`（权重 0,1,1 > 0,1,0，**与打包顺序无关**），并把 EP
  写死 `background:#fff` 的固定列补丁格置为透明。

### 公测前加固（[PR #232](https://github.com/xzb177/Aetrix-Portal/pull/232)）

- **配置自愈不再改写「数据落点 / 对外身份 / 暴露面」**：自愈读的是 `env.example` 里**生效的
  默认行**，而那些值只对「按示例新建的部署」成立。实测一个只写了 JWT 密钥的 `.env` 会被
  追加 85 个 key，其中包括数据库类型、连接串、数据库口令、对外 Emby 地址、服务器标识、
  网关开关。最坏的组合：老部署把连接串交给 compose 的 `environment` 注入、`.env` 里没写它
  —— 自愈把字面量写进 `.env` 后，compose 的插值改从 `.env` 取，下次重启就用错口令连库，
  **整站失去数据库**；而且自愈跑在 lifespan 里（`database.py` 早已 import 完），本次启动
  照常、下次重启才炸。现在这些 key 只记一条 WARNING 点名，不再自动补齐。
- **EA 内部端口收回本机**：原先绑 `0.0.0.0`，等于给公网开了第二个绕过 nginx 的前门
  （绕过限流 / 安全头 / 后续 TLS 终止），还会按 Host 头把自己宣告成那个端口、把客户端诱过去。
- **备份链路真正可用**：内置「数据库定时备份」只做 SQLite 文件级备份，跑 PostgreSQL 时
  照常调度但**明确跳过**；给 PG 用的旧脚本备份的是早已不存在的 `portal_user` /
  `portal_admin`、用户默认 `postgres`、目录指向不存在的路径、还要 cron 环境里的口令
  （cron 没有环境变量 → 每次直接退出，静默无产出），另一份的容器名默认 `aetrix_postgres`
  （下划线）与实际 `aetrix-postgres`（连字符）不符。现在合并为一份 `scripts/backup_db.sh`：
  在容器内 `pg_dump`、校验产出是合法 gzip 且含 `CREATE TABLE`（空壳丢弃，不留假备份）、
  按天数保留；`docs/operations.md` 补上 PostgreSQL 的备份 / 恢复与 cron 口径。

### 媒体库来源写错格式时，提示指向真原因

现场：媒体库的 `paths` 里存的是**从 JSON / 列表里连方括号一起复制**过来的
`["mount://1/nastool/剧集/儿童"]`。它不是挂载路径，会被当成本机目录，于是**每次扫描**
都报「目录不存在或不可读」—— 看着像挂载坏了或网络不通，把排查引到错方向，而问题只在格式上。

现在扫描期与保存期用**同一套措辞**（`mounts.explain_local_path_failure`）：

- 带壳的直接给出改法：「去掉外层括号/引号后是 `mount://1/nastool/剧集/儿童`」
  （也认多元素列表被按逗号切开的半截 `["mount://1/a"` / `"mount://1/b"]`）；
- `mount://` 前缀写错（全角冒号 / 大小写 / 挂载 ID 不是数字）说明该写成什么；
- 确实只是目录不存在 / 读不到时，保持原措辞不变（不为了花哨把通用情况也改了）。

后台「路径」输入框的说明也补了「一行一个，不要带方括号或引号」，
`docs/operations.md` 的「挂载来源不可用」一节加了四种写法错误的对照表。

### 验证

- `python -m pytest tests/ -q`：607 项通过。
- 九条静态门禁全部通过：`check_await_consistency` / `check_hardcoded_secrets` /
  `check_version` / `check_branding` / `check_admin_audit_coverage` / `check_auth_coverage` /
  `check_blocking_routes` / `check_frontend_routes` / `check_frontend_tokens`。
- CI：前端 `user_frontend` / `admin_frontend`（type-check + build）、后端冒烟、PG 兼容冒烟、
  部署自检（真起 uvicorn + 真发 HTTP）五项全绿。
- 深色主题：对构建产物做层叠判定 + 无头 Chromium 实测（桌面 1440 与手机 390×844）——
  输入框 `#16202d`、弹窗 `#101823`、主色按钮 `#22d3ee`，下拉与固定列均为深色。
- 公测加固：自愈护栏用真实 `env.example` 复现（85 → 79 个 key，6 个危险 key 全部拦住、
  无害项照常补齐）；`scripts/backup_db.sh` 用假 `docker` 影子件跑通成功路径与三条失败路径
  （容器未运行 / `pg_dump` 失败 / 空壳 dump，均退出 1 且不留半成品）。

## [2.41.0] - 2026-09-27

### 后端拆分：后台任务从 API 进程里拆出去

扫描、探测、补全、定时扫描、追新这些后台任务原先和网页服务挤在同一个进程里：
一次全量扫描就能让「点什么都要转 30 秒」。这一版按 `AETRIX_ROLE` 把进程拆开：

- `api`（`backend/main.py`）：只服务 HTTP，不再启动后台任务（走这个入口时会打日志说明）；
- `worker`（`backend/worker.py`）：只跑后台任务；
- `ea`：只提供 Emby 协议面；
- **不设 `AETRIX_ROLE` 时保持单进程旧行为**（向后兼容，老部署不用改配置）。

多进程之间的协调：

- **只跑一个**：同一份后台任务由 Redis 锁保证集群里只有一个实例在跑，不会多进程各扫一遍；
- **扫描队列 Redis 桥接**（`backend/emby_server/scan_queue_redis.py`）：入队 / 取消 / 进度
  跨进程可见；
- Redis 任务**处理完成之后才 ACK**，进程崩溃不会把任务丢掉。

`scripts/update.sh` 的备份容器探测跟着改成按 `AETRIX_ROLE` 认容器 —— 此前它硬编码的服务名
`aetrix` 在拆分后已不存在，于是每次都走进「未发现正在运行的 aetrix 容器」分支：看着只是
warn，实际是**静默跳过了数据库备份**。

### 验证

- `scripts/smoke_test_scan_queue.py`：按挂载串行化 / 并发上限 / 排队与取消 / 进度 / 远程限流，
  以及队列关掉时回到升级前行为。
- `scripts/verify_scan_queue_concurrency.py`：四个库同时点扫描的复现演练（真 WebDAV + 真接口，
  升级前后对照 —— 原先四个库同时在跑且同一挂载上叠着多个任务，现在最多 2 个在跑、同一挂载
  零重叠、有库被明确告知在等哪个挂载、四个库都不失败）。
- CI：两条前端与后端冒烟全部通过。

## [2.40.0] - 2026-09-26

### 分层扫描 L1/L2/L3 + 文件指纹秒跳

扫描以前是「一轮里把所有事做完」：列目录、写条目、读 NFO、拉 TMDB、ffprobe 全串在一起。
根因是旧的 `_can_skip_file()` 把「文件变没变」和「元数据全不全」绞在同一个条件里 ——
配了 TMDB 的剧集库于是每轮都全量重做，增量形同虚设。这一版把两件事拆开、扫描分三层：

- **L1（前台，秒级）**：只做文件发现 + 指纹 + 极简入库，扫完立刻能在媒体库里看见条目，
  不再等刮削完成；
- **L2/L3（后台）**：side 图片、NFO、TMDB 刮削、探测交给
  `backend/emby_server/enrich_worker.py` 慢慢补完。

机制：

- `file_fingerprint`：本地 `path|size|mtime_ns`，远程 `path|size` 的 md5。指纹命中**且**
  `enrich_status='done'` 时整文件秒跳，一次 IO 都不做；
- `enrich_status`（`pending` / `done`）把「文件变没变」与「元数据全不全」解耦：文件没动就跳过，
  元数据没补全就排队，互不牵连；
- `enrich_attempts` / `enrich_next_retry_at`：补全失败按指数退避重试、超限转 `failed`，
  不会卡在队列里空转；原子抢单保证多个补全线程不重复做同一件事；
- **探测**：`background` 模式走 `probe_worker`（v2.39.0 引入）；`inline` 模式在分层开启时
  也推迟到 `enrich_worker` 排队，避免远程直链的 ffprobe 拖慢秒级入库；关掉分层则恢复当场探测；
- 两个开关可运行时回退：`SCAN_LAYERED=1`（默认开）、`SCAN_FAST_SKIP=1`（默认开），
  任一置 0 即回到升级前的行为。

老库由 `_auto_migrate` 自动补列，补出来的 `enrich_status` 是 `pending`，所以升级上来的库会在
下一轮扫描后把待补全条目慢慢补完，不需要人工干预。

### 验证

- `tests/test_layered_scan.py`：文件指纹秒跳 + L1 极简入库（含本机文件 L1 行为断言）。
- `tests/test_enrich_worker.py`：原子抢单 / 重试退避 / 防洪峰 / 字幕落库 / 进度接口。
- `scripts/smoke_test_scan_incremental.py`：未变动重扫不再逐文件写库但条目一条不少、
  变了一定处理、库里缺行必须重建、远程挂载参与增量且不多列目录。
- `scripts/smoke_test_scan_budget.py`：批量查库 / 批量落盘 / 并行 IO / 目录只列一次。

## [2.39.0] - 2026-09-25

### 后端：`async` 路由里的同步 DB 清零（阻塞路由基线 24 → 0）

`scripts/check_blocking_routes.py` 盯的是「`async def` 路由里做同步 SQLAlchemy」：
FastAPI 只在事件循环上跑 `async` 端点，一次查询 / 提交就能让全站（包括别人的播放）排队。
这一版把清单上**剩下的 24 条全部做掉**，基线现在是空集（`BASELINE = set()`）。
背景与「为什么不能一次机械替换」见 [`docs/performance.md`](docs/performance.md) 二·七、
二·八、二·九、二·一〇；这一版的逐域改动与口径写在
[`docs/performance-blocking-routes.md`](docs/performance-blocking-routes.md)。

修法两种，按「这个端点有没有必须 await 的东西」选：体内没有 await 的**直接改同步 `def`**
（FastAPI 自动丢线程池，业务代码一个字不用动）；确实要 await（WebSocket 通知 / 读表单 /
子进程与磁盘 / 外部推送）的，把同步写库那段拆成同步函数交给 `await run_in_threadpool(...)`。

**管理域（`backend/api/admin.py` / `admin_economy.py` / `reminders_admin.py`）**

- 订阅授予 / 延长、站内消息、广播、公告增改、工单回复与关闭、求片状态更新：校验 + 落库 + 审计
  各拆一个同步函数下放线程池；通知（`notify_admin_event` / `notify_all_users`）仍 await。
  提交后要用的值（套餐名 / 到期时间 / 工单标题 / 用户 id）一律先取成纯值，避免回到循环上
  触发一次隐式回查（`Session.commit()` 会过期所有实例）。
- 人工补单 `economy_mark_order_paid`：整段（查单 → 反幂等 → 履约 → 审计）移进模块级
  `_mark_order_paid()`，由路由用 `run_in_threadpool` 调用。
- 手动调账（`economy_adjust_points`）、到期提醒（`run_expiry_reminders`）的落库与审计同样下放。

**用户 / 经济域（`backend/api/user.py` / `economy.py` / `coupons_admin.py`）**

- 签到、兑换码核销、支付回调：验签 / 查单 / 金额核对 / 原子占位 / 发奖 / 履约 / 审计整段下放；
  只有 `await request.form()` / `await request.json()` 与发通知留在事件循环上。
- `economy._fulfill_order` 本来就是 `async def`，但**体内一个 await 都没有**（回调与人工补单
  都被迫在循环上跑它），现在改回同步函数，两个调用方都用 `run_in_threadpool` 调它。
- 核销记录两个端点（`list_coupon_usages` / `list_coupon_usages_by_code`）体内没有任何 await，
  直接改同步 `def`，并把查询抽成 `coupon_usages_payload` 共用。
- 消息「全部标已读」的批量 UPDATE、新建 / 回复工单的落库同样下放。

**协议面（`backend/emby_server/api.py` / `portal.py`）**

- `rate_item`（收藏 / 标记已看）：只有 `await request.json()` 留在循环上，读条目 + 写
  `UserMediaData` + 序列化整段进工作线程。
- 结束播放会话（自己停 / 管理员停）与媒体库扫描入队：查库与写回下放；停转码
  （`stop_transcodes_for_async`）与「转发给归属节点」（`push_scan`）仍是 await。

**两条护栏跟着收口**

- `scripts/check_admin_audit_coverage.py` 现在跟一层委托：端点用
  `await run_in_threadpool(_helper, ...)` 把整段写库（**含审计**）交给本模块函数时不再误报
  ——但被调用函数里也没写 `_audit(...)` 的照旧点名；审计判定同时改成语边界匹配，
  名字以 `_audit` 结尾的普通函数（`_worker_without_audit(`）不再被当成「写了审计」。
- `scripts/smoke_test_static_guards.py` 的自检跟着更新：阻塞路由那条从「基线非空」改成
  「真实代码树上一条都没有」（防作弊靠与独立重扫**逐条相等** + 合成样本必须失败），
  并新增「委托写法」的正反两个样本。

### 验证

- `python -m pytest tests/ -q`：112 项通过。
- `python scripts/check_blocking_routes.py`：真实代码树 **0 条**、基线 **0 条**；
  `check_await_consistency` / `check_admin_audit_coverage` / `check_auth_coverage` /
  `check_version` / `check_branding` / `check_hardcoded_secrets` 全部通过。
- `python scripts/smoke_test_static_guards.py`：47 项全过（含阻塞路由 0 条与委托正反样本）。
- 与改动直接相关的冒烟（实测）：经济 19 项、退款/关单 76 项、并发 39 项、优惠券 109 项、
  到期提醒 50 项、后端热路径 44 项、后台写操作审计 59 项、Emby 主链路与会话 44 项、
  播放链路 20 项、转码停止 38 项、后台 v2.4.0 与运营 v2.6.0、用户端查漏补缺全部通过。

## [2.38.0] - 2026-09-25

### 事务范围：写事务里绝不出现 IO（根治 `database is locked` 与 30 秒超时）

线上现场：后台点一次媒体库**全量扫描**，2 分钟内 9 次
`sqlite3.OperationalError: database is locked`，`royalbot_unified.db-wal` 长期 4.3MB 不 checkpoint，
前端 axios 30 秒超时。WAL 与 `busy_timeout`（30 秒）本身都没问题——**问题在事务范围**：
SQLite 只有一个写者，写锁从第一次写一直握到 `COMMIT`，而扫描器把网络 IO 夹在了这两个点之间。

**根因（旧实现把 IO 放在写事务里）**

- `scanner._scan_library_body` 外挂字幕段：先 `db.flush()` 写入，再
  `scan_file.provider.list_dir()`（rclone 网络 IO）、`find_external_subtitles*()`，然后才随批次 `commit`；
- 同一函数的刮削段：`tmdb_client.search` / `enrich` / `refresh_images` 的网络调用与 session 写操作交错；
- 同一函数的探测段：`_result(pending.probe)` 在写库循环里等 ffprobe 子进程；
- `maintenance.prune_scan_runs`：先删孤儿行（写事务由此打开）→ 再逐库查询第 N 行之后 → 才提交；
- `image_store.prune`：读引用之后事务一直挂着，中间去删几万个磁盘文件。

于是所有其它写请求（后台改设置 / 用户注册 / 卡码核销）都排在一段「等 rclone 的写事务」后面，
等到 `busy_timeout` 耗尽报错——正好也是前端 30 秒超时。

**改法：预取在事务外，事务只包纯 DB 写**

- `scanner._prepare_and_prefetch` 在批次开头把这一批的 IO（ffprobe / 目录列举 / TMDB 搜索与详情）
  **全部提交并取回**，结果存成纯值挂在 `_Pending` 上（`probe_data` / `side_data` / `tmdb_hit` / `tmdb_details`）；
- 写库循环只读纯值；详情改走新增的 `tmdb.apply_details` / `apply_images`（只套用已取回的数据，不发请求），
  详情预取口径同时改成「覆盖写库那一步所有可能用到的分支」；
- `maintenance.prune_scan_runs` 先把要删的 id 全查出来（此时还没有任何写），再一次性删 + 提交；
- `image_store.prune` 引用读完就结束读事务，之后再删磁盘文件。

**兜底：写锁退避重试（不是解法）——`backend/db_retry.py`**

事务范围干净之后，仍会有别人短暂占着写锁（另一台 EA / 另一个进程 / 备份 / `VACUUM` / checkpoint）。
写路径统一走 `retry_write` / `commit_with_retry`：指数退避（0.25 → 0.5 → 1 秒）最多 3 次，
**只对写锁类错误重试**（其它 `OperationalError` 原样抛出），重试到底仍失败才报错——
不吞错误、不假装成功。已覆盖扫描批次提交与状态写回、维护周期清理、卡码核销 / 注册落库 /
订单退款 / 人工补单。

**业务语义未变**：扫描结果、外挂字幕 `stream_index` 合成规则、`Subtitles/{Index}` 拼地址逻辑不变。

### 四个资金链路 bug（读-改-写 → 条件 UPDATE / 单一事务）

- **卡码并发重复核销（高危）**：`codes.redeem_code` / `consume` 先读 `use_count` 再 +1，
  并发提交同一张 `max_uses=1` 的卡码会各发一份会员天数。现在发奖前用条件
  `UPDATE ... WHERE 仍可用` 原子占位（`codes.claim_code`，与 `economy.redeem_exchange_code` 同一写法），
  `rowcount=0` 直接拒绝；`api/emby_portal.register` 抢不到占位就回滚整条注册。
- **退款并发导致双重回滚（中高）**：`orders_admin.refund_order` 的「状态检查 → 冲正积分 / 回滚会员天数 →
  写回 `refunded`」不是幂等的，并发退两次会各扣一次。现在用条件 `UPDATE` 把 `paid` 原子推进到
  `refunded`（`economy._claim_order` 的写法），抢不到的那一边直接返回「已处理」。
- **履约缺料被静默吞单（中高）**：`economy._fulfill_order` 里 `if user and plan:` 不成立就什么都不做，
  订单却被标成 `paid`、优惠券照常 `consume`。现在抛 `FulfillmentError` → 回调回滚并返回 `fail`
  （网关会重试 / 转人工），订单保持未支付；后台「人工补单」遇到同样情况返回可读的 400。
- **卡码核销两阶段提交不一致（中低）**：`codes.grant_membership_days` / `consume` 各自 `commit`，
  中间崩一次就是「码烧了、会员没到账」或反过来。现在去掉内部 `commit`（改 `flush` 拿主键），
  由调用方一个事务提交（与 `economy._grant_subscription` 的无 commit 设计一致）；
  `api/user.py::redeem_membership_code` 同时从 `async def` 改为同步 `def`（整条链路无 await，
  正好不再把占位与发天数压在事件循环上）。

### 验证

- `scripts/smoke_test_scan_write_concurrency.py`（新增，进 CI）：给扫描器真正会走的 IO 出口
  （`os.listdir` / ffprobe / TMDB 会话）装上「有没有落在未提交的写事务里」的探针，**探针先自证有效**
  （人为在写事务里做一次同样的 IO，必须记到 1 次违规），再跑真实全量扫描；另一个线程循环打真实的
  写接口 `POST /api/admin/economy/plans`。实测（132 个文件 / 多批次）：写事务里的 IO **0 次**、
  扫描期间最长写事务 **约 150 ms**、92~96 次并发写请求中位 **14 ms** / 最大 **0.18 s**、
  `database is locked` **0 条**、写锁兜底重试 **0 次**（旧形态会等于那一批的网络 IO 时长，
  远程挂载上直接超过 30 秒）。
- `scripts/smoke_test_concurrency.py` 新增两节（39/39 通过）：4 个线程并发核销同一张 `max_uses=1`
  的卡码 → 只有 1 个成功、`use_count` 恰好 1、用满自动停用、会员天数只多发 30 天、`used_by` 只记一个账号，
  用尽后再核销被拒且不再发天数；4 个线程并发退款同一笔已支付订单 → 只有 1 次真正冲正、积分只扣回一次、
  冲正流水只记一笔。
- `scripts/smoke_test_refunds.py` 新增第 7 节（76 项通过）：套餐下单后被删 / 用户下单后被删时回调返回 `fail`、
  订单保持 `pending`、不发订阅、优惠券额度不被 `consume`；后台人工补单返回 400 + 说明而不是 500；
  套餐修好后回调恢复正常并真的发出订阅（保证正常路径没被挡掉）。
- `pytest tests/ -q` 112 项通过；`check_await_consistency` / `check_blocking_routes`（阻塞路由 24/24，无新增）
  / `check_version` / `check_branding` / `check_auth_coverage` / `check_admin_audit_coverage` /
  `check_hardcoded_secrets` 全部通过；`smoke_test_scan_budget` / `smoke_test_scan_incremental` /
  `smoke_test_maintenance` / `smoke_test_scan_cleanup` / `smoke_test_scan_result` /
  `smoke_test_playback_chain` / `smoke_test_economy` 全部通过。
- 文档：新增 [`docs/performance-transactions.md`](docs/performance-transactions.md)（事务范围 / 写锁兜底 /
  四个资金链路 bug 的根因与实测数字），并在 `docs/performance.md` 顶部加了导引。

## [2.37.0] - 2026-09-25

### 探测 / 体检层：写库下放线程池（阻塞路由基线 37 → 24）

上一版拆掉了通知层，并在文档里写明「下一个批次按探测层收口」。这一版就是它。

**根在 `backend/servers.py::probe_and_store`**

它是「保存服务器 / 体检 / 激活 / 节点同步」四条业务共用的那一段：先 `await probe_server(...)`
（真发 HTTP，8 秒超时都可能），然后**在同一个 `async` 函数里**读这一行、写结论、提交。
也就是说后台点一次「测试连接」，全站（包括别人的播放）就排在那次探测后面；多服部署里
「Emby 总览」开着 `live=True` 更是一次串起好几台机器。现在探测仍 await，
「读这一行」与「落库」两段同步 SQLAlchemy 整段下放 `run_in_threadpool`。

**同一手法改完 13 个端点**

- `api/servers.py`：`create_server` / `update_server`（唯一性检查 + 落库 + 切换旧配置键）、
  `activate_server`（体检 + 激活 + 审计）、`refresh_mount_health_now`（取当前 EA + 重拉体检）、
  `emby_overview`（整页读几十次库并按服汇总 → `load()` + `build()` 两段下放）；
- `api/emby_servers.py`：`refresh_mount_health`（读写 EA 体检快照夹在 HTTP 两侧）、
  `save_server` / `test_server` / `refresh_server_mounts`；体内没有 await 的 `get_servers`
  与 `api/servers.py::servers_summary` 直接改同步 `def`（FastAPI 自己丢线程池）；
- `emby_server/portal_mount_routes.py`：`check_all_mounts` / `test_saved_mount` / `browse_mount`；
- `api/realms.py::sync_realm_nodes`、`emby_server/portal.py::verify_pan115_account`。

**一个专门的坑：提交之后不能再回读 ORM**

`Session.commit()` 默认过期所有实例，之后哪怕只读 `server.url` 也会在**事件循环**上发一条
SELECT。这类隐式回查 `check_blocking_routes.py` 抓不到（它只认函数体里的 `db.xxx(`），
但真机上就是「后台点一下、别人卡一下」。所以这次统一的做法是：**要用的值在工作线程里
取成纯值再带回来**（`server_id` / `kind` / `url` / `realm_id`），并且让
`admin_core._audit` 也能直接收**管理员主键整数**，async 端点提前把 id 取好，
审计与其它写落进同一个线程池任务。

**验证**

- `scripts/smoke_test_backend_hot_paths.py` 新增第 5 节（进 CI）：给「这台机器写库就是慢」
  建模（命中 `remote_servers` 的 UPDATE 先睡 0.35 秒），先自证方法有效——同样的 UPDATE 直接跑在
  事件循环上，心跳空洞 **0.365s**；再走真实路由：`POST /api/admin/servers/{id}/test` 空洞
  **0.075s**、`POST /api/admin/emby/servers/mounts/refresh`（慢快照）空洞 **0.011s**，
  同时体检结论、EA 体检快照都真的落库；`activate` 之后服务器真被设为当前使用，
  且审计里记的正是这位管理员（走通 `_audit` 收整数那条路）；
- `scripts/check_blocking_routes.py`：阻塞型 async 路由 **24 个**（基线 24，无新增）；
- `pytest tests/ -q`、`check_await_consistency` / `check_auth_coverage` /
  `check_admin_audit_coverage` / `check_version` / `check_hardcoded_secrets` 全部通过；
  `smoke_test_servers.py` 81/81（含激活 / 重名 409 / 测试连接各分支）、`smoke_test_mount_health.py`
  40/40、`smoke_test_realms.py`、`smoke_test_mounts.py`、`smoke_test_emby_connection.py` 通过。

## [2.36.0] - 2026-09-25

### 求片链路与通知层：写库下放线程池（阻塞路由基线 39 → 37）

上一版把两条最热的协议面路径（会话上报、分类菜单）从事件循环上挪开了，也把
「真正的瓶颈是通知层自己也在 `async` 里写库」写进了文档。这一版就来拆这一层。

**通知层（影响面最大的一处）**

`notify_staff_users` / `notify_all_users` 原先在 `async` 函数里查人、逐条写站内消息再提交：
调用它们的**每一个**功能（求片、工单、公告、订阅、经济）都因此把全站请求按在这次写事务后面。
现在落库段各自拆成同步函数（`_persist_staff_messages` / `_persist_broadcast_messages`）下放线程池，
WebSocket 实时推送仍是 await——对外行为、返回的管理员/用户数量都没变。广播给几万个用户
写站内消息这种「一次写几万行」的活儿，是最不该占着事件循环的写。

**求片两条链路（文档清单上剩下的两条热路径）**

- 用户提交求片（`POST /api/user/media-seek`）：去重 / 每日额度 / 落库拆成 `_create_media_seek_sync`
  下放线程池；400 / 409 / 429 的语义与文案不变（异常从线程里原样抛给客户端）；
- 后台推送求片（`POST /api/admin/media-seek/{id}/push`）：取件与「记录结果 + 审计」各拆成同步函数
  下放线程池；提交后**返回面板要的纯值**，避免回到事件循环上再触发一次隐式回查。
  推送失败仍然如实记 `push_status=failed` / `pushed_at` 并写审计（不假装成功）；
- `servers.push_media_seek` 里两次挑服务器的同步查库（`active_config`）同样下放线程池。

**验证**

- `scripts/smoke_test_backend_hot_paths.py` 新增第 4 节（进 CI）：先自证方法有效——把站内信落库
  直接跑在事件循环上，心跳空洞 **0.36s**；再让「这台机器写站内信慢 0.35 秒」的真实请求进来提交
  求片，空洞只有 **0.012s**，同时求片记录与管理员站内消息都真的落库；后台推送在没有可用 qB 时
  如实返回失败并留下 `push_status=failed` 与一条审计；
- `scripts/smoke_test_servers.py`（推送端点 5 个分支）、`smoke_test_user_v250.py`（求片提交 /
  去重 409 / 额度 429 / 撤回）、`smoke_test_realms.py`、`smoke_test_admin_v240.py`：全部通过；
- `scripts/check_blocking_routes.py`：阻塞型 async 路由 **37 个**（基线 37，无新增）；
  `pytest tests/ -q`、`check_version` / `check_await_consistency` / `check_auth_coverage` 通过。

## [2.35.0] - 2026-09-25

### 后端提速与稳定性：热路径不再占事件循环、分类菜单走索引、库长到后面也不退化

这一版不加功能，只处理「用户能感觉到的卡」与「跑久了会慢慢变差」的四件事。每条都在
`scripts/smoke_test_backend_hot_paths.py` 里量出数字或数出 SQL（进 CI），不靠“看起来没问题”。

**并发播放不再互相卡（会话进度上报下放线程池）**

`POST /Sessions/Playing/Progress` 是全站最热的写路径：每个正在播放的客户端每 10 秒上报一次，
一次上报要读会话 / 写位置 / 写观看进度（2~3 条 SQL + 1 次提交）。它必须 `await request.json()`，
原先这几条同步 SQL 就跟着跑在**事件循环**上——多路并发播放时，所有人的其它请求都得排在这些
写事务后面。现在三个会话上报端点（`Playing` / `Progress` / `Stopped`）只负责解析 JSON，整段
同步写库下放线程池。脚本用后台心跳量出结果：把同样 0.35s 的慢写跑在循环上，心跳空洞 0.36s；
走真实路由后空洞 **0.011s**，而且会话与观看进度确实写进去了（活儿照做，只是不在循环上）。

**分类菜单端点不再读整列（`/Genres`、`/Studios`、相似推荐）**

`/Genres`、`/Studios` 原先每个请求都把整库的 `genres` / `studios` 文本列读回来再切分——十万级库
约 190ms，而且随库**线性增长**（客户端每打开一次筛选面板就付一次）。现在走
`emby_item_facets` 的覆盖索引（带取值缓存）；老库没回填完才退回旧口径，取值逐条一致。
同理「相似推荐」的流派匹配从 `genres ILIKE '%…%'` 换成关联表条件（每开一个详情页少一次全库扫描），
`/Items/Counts` 的三次计数合并成一次 `GROUP BY`，`user_views` 等三条没有 `await` 的异步路由
改回同步 `def`（阻塞路由基线 41 → 39，只减不增）。

**刚扫完的片子立刻能在筛选面板看到（筛选菜单变更后刷新）**

筛选菜单取值来自全库，原先只能等 TTL（默认 300 秒）自然过期——刚扫完的片子最长 5 分钟看不到。
现在分类值一变（扫描 / 图片修复 / 删条目的**同一个 flush 钩子**）就把缓存标记成陈旧，下一次
请求重建；重建带最小间隔（`EMBY_FILTERS_MIN_REFRESH`，默认 5 秒），扫描期间每批条目都会触发，
不能让它退化成「扫描时每请求一次」（脚本用 SQL 计数钉住：连续 5 次改动 + 5 次请求，至多重建一次）。
显式调用 `invalidate_filters_cache()`（后台改元数据 / 测试直接写库）则清空缓存、下次请求必然重建。

**库长到几十万行后查询计划不跟着退化（周期 `PRAGMA optimize`）**

看护周期里加了一条 SQLite `PRAGMA optimize`：数据分布变了之后，查询计划不能还按很久以前的
统计信息估行数（估错就是全表扫）。绝大多数时候它什么都不做（毫秒级），只在某张表确实该 `ANALYZE`
时才动手；非 SQLite 直接跳过。维护周期返回里多一个 `query_plans_optimized` 布尔，便于观测。

**同版本一并带上（此前只进了代码、没进日志）**

- **rclone 挂载的播放地址按 rc-serve 的 `[remote:]` 语法拼接**：旧写法 `/remote:path/x.mkv` 会被
  rc-serve 当成叫 `remote:path` 的本机目录而 404——列目录一切正常，只有真正取流时才炸，很容易被
  误判成「rc-serve 没开」；同时「rc-serve 可能未开启」的探测改成打 remote 根目录页，不再拿一个本来
  就错的 URL 去探（那会误报）。`scripts/smoke_test_mounts.py` 里那条 rc-serve 断言此前与实现不一致
  （CI 一直红着），已按新语法更新；
- **版本号改为单一来源**：根目录 `VERSION` 为准，后端 `/api/health` 与两个前端 `package.json` 都读它，
  `scripts/check_version.py` 进 CI 核对漂移；本版 `VERSION` 与两个前端一并升到 `2.35.0`；
- **部署**：`docker-compose.yml` 里 rclone 配置目录改为可写——只读挂载下 rclone 刷新 OAuth token
  落盘会失败（`read-only file system`），表面能读，token 一过期就再也续不回来。

#### 验证

- `pytest tests/ -q`：97 项通过；
- `scripts/smoke_test_backend_hot_paths.py`（新增，进 CI）：21 项全通过（事件循环空洞、菜单 SQL、
  缓存刷新节奏、计数口径）；
- `scripts/smoke_test_item_facets.py`（55 项）、`scripts/smoke_test_item_facets_upgrade.py`、
  `scripts/smoke_test_media_search.py`、`scripts/smoke_test_emby_sessions.py`（44 项）、
  `scripts/smoke_test_maintenance.py`：全部通过；
- `scripts/check_blocking_routes.py`：阻塞型 async 路由 39 个（基线 39，无新增）；
  `check_await_consistency.py` / `check_auth_coverage.py` / `check_version.py`：通过；
- `scripts/smoke_test_mounts.py`（含更新后的 rc-serve 断言）、`smoke_test_mount_cache.py`：通过；
- `scripts/benchmark_item_facets.py --guard`：性能护栏全部通过（无回归）。

## [2.34.0] - 2026-09-24

### 用户端首页补上「我的面板」：观影数据 / 进行中的事项 / 正在播放

用户端首页（面板）此前只有经济速览、追新与继续观看，**账号名下正在发生的事**看不到：看了多久、
求片处理到哪一步、有没有别的设备正在播。这一版在账号速览条下面补上三块——都是门户独有、
客户端给不了的：

- **我的观影**：累计观看时长（不足 1 小时按分钟显示，新用户不会一上来就是「0 小时」）、播放次数、
  看过影片；右上角一个「观看记录」入口落到媒体库的观看记录分段；
- **进行中的事项**：「我的求片」（待处理 + 处理中数量，没有进行中的就说已入库几部）与「我的工单」
  （进行中 / 已处理）两行**真实状态**，各自深链到 `/request`、`/tickets`；数量为 0 时显示「—」
  而不是一个孤零零的 0，下面一行说清下一步会发生什么——0 读起来像「功能坏了」，不像「没事可做」；
- **正在播放**：只在本账号**真有会话时**才出现，给出片名、设备 / 客户端 / 转码或直连、进度条与
  一键「结束」，不必先绕进个人中心才能停掉别处的播放。

口径写在视图头部（与顶栏铃铛 / 消息中心同一分工）：面板只做「一眼看到状态 + 点进去办事」，
**完整的会话清单与设备管理仍在个人中心**（控制面板）——同一个语义，摘要在一处、全量在另一处，
不会两处都铺全，也不会因为首页多了一块就多出一份要维护的重复入口。

实现上**未改后端、未加路由、未引入新依赖**：复用 `/api/user/emby/stats`、`/api/user/emby/sessions`
（含结束会话）、`/api/user/media-seek`、`/api/user/tickets` 四个已有端点，各自 `catch` 成空值，
任一项失败都不影响其它块与整页；样式全部沿用 `--au-*` 设计令牌，860px 以下两块面板竖排，
加载中与账号速览条同一口径压暗，避免一闪而过的「—」被读成「没有数据」。

#### 验证

- `cd user_frontend && npm run type-check`：通过；
- `cd user_frontend && npm run build`：通过，产物含新的 `HomeView` chunk（`HomeView-*.js` / `HomeView-*.css`）；
- `python3 scripts/check_frontend_tokens.py`：通过（6 项，引用的令牌都有定义、无循环引用、无孤立样式表）；
- `python3 scripts/check_frontend_routes.py`：通过（7 个跳转点全部指向已声明的路由）；
- 运行中的服务已换成新构建：首页 `index` chunk 已指向新的 `HomeView` chunk，该 chunk 返回 200
  且包含新面板文案；`/api/health` 返回 `healthy`；
- 未改后端：无新增接口、无迁移，四个面板数据端点均为既有接口，口径与个人中心 / 求片页 / 工单页一致。

## [2.33.0] - 2026-09-24

### 管理端其余常驻说明统一改为可折叠面板

上一版先把管理员与权限页、存储来源页最占版面的说明收进了 `NoticePanel`。这次把其余页面里
同类的**常驻整块说明**一并统一，避免说明文字继续把真正要操作的列表、策略和配置卡到首屏之外：

- **服务器页**：新增「每一类分别是干什么的」，默认收起，说明 EA / 已有 Emby / MoviePilot /
  qBittorrent 的职责与入口关系；
- **服管理页**：新增「一个服一个的东西有哪些」，默认收起，说明套餐、订阅、媒体库、存储、
  服务器、卡码与求片的服级隔离口径；
- **客户端策略页**：新增「为什么这些策略在这里」，默认收起，说明客户端能力与系统设置中站点运营参数的边界；
- **系统设置页**：把外部服务能力的凭据说明收进面板，默认收起；支付网关是否配置完成仍保持常驻状态提示，
  因为它是当前状态而不是帮助文字。

四个面板都复用了同一个 `NoticePanel`：标题行整行可点、收起时显示摘要、展开状态按独立键记在
`localStorage`，窄屏继续沿用组件自身的 1024px / 640px 响应式规则。权限不足、连接失败、待处理告警、
弹窗内提示和字段旁短提示仍保持常驻，避免把需要马上处理的状态藏起来。

#### 验证

- `cd admin_frontend && npm run type-check`：通过；
- `cd admin_frontend && npm run build`：通过，产物包含独立的 `NoticePanel` chunk；
- `python3 scripts/check_frontend_tokens.py`：通过（6 项）；
- `python3 scripts/check_frontend_routes.py`：通过（7 个跳转点）；
- `git diff --check`：通过；
- 构建产物可用于手工演示：打开上述四页，点击面板标题即可展开 / 收起；刷新后展开状态应按
  `admin.notice.<storage-key>` 恢复，未主动展开的面板保持收起。

## [2.32.0] - 2026-09-24

### 用户端地址不再拿写死的 localhost 当配置，后台说明改为可折叠

两处都是「刚装好、还没配任何服务」时第一眼看到的东西：一条本来不该报的红提示，
以及几块一直占着版面的说明文字。

#### 1. 「用户端账号卡显示的地址是 http://localhost:8000」不再冤报（本轮最要紧的一处）

`resolve_emby_base_url` 的最后一档回退是写死的 `http://localhost:8000`。面板自己开着协议面时
（一体化部署，`ENABLE_EMBY_GATEWAY=true`），这条回退会被当成「地址解析成 localhost」的证据，
于是在一台刚装好、什么都没配的服务器上，媒体库页第一眼就是一条红提示：
「只有这台机器自己能连」。那个地址从来不是谁配的，只是占位——而且后果不止是红：
**用户端账号卡真的把 localhost 下发下去**，用户照抄进播放器就连不上，测试机上一试就是坏的。

- 解析优先级补上「当前请求用的地址」，排在环境变量之后、占位之前：面板与接口同源，
  管理员正用 `http://<服务器IP>:8000` 访问时，解析出来的就是它，账号卡下发的也是它；
- 同时告诉调用方这个地址是**哪来的**（`url_source`：服地址 / 服务入口 / 环境变量 / 推断 / 没配），
  面板上「没配」不再拿占位地址当证据报红，而是按当前访问地址显示并标明「按当前访问地址」；
- 配过的一律优先（多服、反代、换域名都靠这条），只有**真的把 localhost 填进配置**才报红；
- 顺手修掉环回地址识别的一处漏洞：IPv6 字面量带方括号（`http://[::1]:8000`），按 `:` 切
  会把主机名切成 `[`，于是环回地址被当成正常地址、这条提示永远不报。

#### 2. 整块说明文字收成一行，需要时再展开

管理员与权限页（角色说明、三条护栏）、存储来源页（支持的来源类型）以前是常驻的：
打开页面第一眼看到的全是解释，真正要操作的清单（管理员列表、挂载列表）被挤到屏幕外，
手机上一次滑动都到不了。现在这三块都收成一行标题（带一句话摘要，不点开也知道讲什么），
点开才展开；展开状态记在浏览器本地（和左侧导航分组折叠同一个思路），看过一次的人
下次进来还是开着的。

## [2.31.0] - 2026-09-24

### 扫描与刮削：让「上一轮跑过的东西」真的不必再跑

这一段只围绕一个事实：扫描的代价几乎不在解析文件名，而在**重复的网络与数据库往返**。
四处「每轮都重做一遍」被找了出来：配了 TMDB 的剧集库里每一集、写库用不到的 TMDB 详情、
一个「满了就整表清空」的缓存，以及没有外挂字幕时也要查的那一次 `max()`。

#### 1. 配了 TMDB 的剧集库，增量扫描此前等于关掉（本轮最要紧的一处）

`should_scrape` 对「没有 tmdb_id」的条目恒返回「要刮」，而**集本来就不参与刮削**
（写库循环里的口径是 `item_type in ("series", "movie")`：集与季没有被刮的可能）。但这条
判断此前落在所有条目的跳过判定里，于是剧集库（最常见的库型）只要配了 TMDB，每一轮重扫都得
完整处理每一集——找图、列目录、重建字幕轨、提交。追新（重扫基本没变的内容）本来是增量扫描的
主要场景，等于对它完全失效：重扫耗时与冷扫几乎一样。

- 现在只有**电影 / 剧集**参与刮削相关的跳过判断，集与季按「目录没变就跳过」处理；
- 安全边界一条没放松：目录变了、文件大小变了、库里缺行、带补图标记的条目照旧完整处理
  （目录变了一点就整目录重做，宁可多做也不能漏）。

#### 2. 详情预取的口径与写库那一步对齐（不再白花配额）

写库那一步只在「新条目（要补 IMDb Id 与别名）」或「带补图标记」时才取详情；预取却对**每个
搜索命中**都无条件拉一次详情——「到期重刮但信息已经齐」的条目那一次详情是白花的。现在
`want_details` 由调用方按同一口径算好再传给预取，两边不会再各算一套。

#### 3. TMDB 缓存满了不再整表清空

搜索与详情各占一条，一万部片子刚好撞上缓存上限（两万条）。旧实现满了就 `clear()`，把
**刚预热好的结果连整张表一起扔掉**——写库线程随后要取同一个片名，就变成一次真的网络请求。
规模越接近上限越容易撞上，配额也就白花。现在：

- 满了先清过期条目，再按写入顺序淘汰最旧的一批（保留 75%），并多淘汰一条给正在写的条目
  腾位置（否则下一次写入立刻又触发一轮淘汰）；
- 读取时顺手删掉过期条目，不再让它们占名额、把还在窗口内的预热结果挤出去。

#### 4. 没有外挂字幕时不再为字幕轨查一次 `max()`

外挂字幕轨要合成 `stream_index`（客户端靠它拼 `/Videos/{id}/{mid}/Subtitles/{Index}/Stream.{Format}`），
分配前会查一次 `max(stream_index)`。旧实现无论有没有外挂字幕都查——十万条目的库就是十万次多余
的 SELECT。现在只为**真的有外挂字幕**的条目查，并把「只读目录、不碰数据库」的外挂字幕判定提到
数据库操作之前，两件事不再混在一步里。

#### 测试

- `scripts/smoke_test_scan_incremental.py` 新增「剧集库 + 配了 TMDB」一段：冷扫入库
  （1 剧 + 1 季 + N 集）、未变动重扫**每一集都被跳过**（`side` / `probe` 均为 0）、目录里新增
  一集后整目录重做；另有一条**机制复核**（集没有 tmdb_id → `should_scrape` 对它恒为「要刮」），
  说明上面那条 PASS 只有在集被排除在刮削条件之外时才成立。
- 新增 `tests/test_tmdb_cache.py`（8 条，pytest）：缓存命中、`None` 也是命中（「没搜到」
  区别于「没查过」）、过期条目读取即删、满载时按最旧淘汰且**保留最近结果**（旧实现会整表清空）、
  先清过期再淘汰、覆盖已有键不误伤别人；以及详情预取的三种口径（搜索命中但写库用不到详情 →
  不拉；搜索落空但有库里 id → 用 id 取；不需要搜索时不搜）。
- 回归：`smoke_test_scan_budget` / `scan_queue` / `scan_cleanup` / `scan_result` /
  `smoke_test_maintenance` 与 `tests/` 下 pytest 全部通过。

## [2.30.0] - 2026-09-24

### 统一品牌与命名：RoyalBot → Aetrix

仓库里一半叫新名、一半叫旧名的阶段最难用：改个站名要在两种叫法之间猜，客户端列表里会
出现两台同名服务器。这一版把所有面向人与面向部署的名字收敛到同一个：**Aetrix**（仓库
`Aetrix-Portal`，控制面 EM / 数据面 EA 的代号不变）。

#### 改动

- **仓库与文档统一**：README 标题与许可证署名、两份部署文档、监控配置、运维脚本抬头
  全部改到同一个名字。
- **前端**：管理端标题 `RoyalBot Admin` → `Aetrix Admin`、包名与站点默认名 `RoyalBot`
  → `Aetrix`、设计令牌注释；两者与用户端 manifest 一致。
- **数据库默认文件名** `royalbot_unified.db` → `aetrix_unified.db`，并带**不改动老部署**的兼容：
  新名不存在而老文件还在时继续沿用老文件（见下）。
- **运维标识跟着走**：PostgreSQL 默认库 / 账号、容器名 `aetrix_postgres`、systemd 单元
  `aetrix-em` / `aetrix-ea`、备份目录 `/root/Aetrix-Portal/backups`、备份产物 `aetrix_*.sql.gz`、
  Prometheus 作业名与告警文案。
- **脚本不再写死库文件名**：`create_admin.py` / `deploy_check.py` / 各冒烟测试原先各自写死
  `sqlite:///./royalbot_unified.db`，改品牌后会「在错的库里建管理员」——现在统一交给
  `backend.database` 解析（`default_sqlite_url()`），升级上来的部署继续用原库文件。
- **README 瘦身**：内联的 700 行版本历史与 `CHANGELOG.md` 重复（还把 README 撑到 97 KB），
  改为「最近两个版本 + 指向 CHANGELOG.md」；瘦身时顺手留下的 `README-old.md` 快照已删除
  （内容与 CHANGELOG 重复、仓库里没有任何地方引用它，留着只会让品牌检查多一个整文件豁免，
  而整文件豁免意味着那份文件里的旧名永远不再被看见）。

#### 安全与护栏

- **`scripts/check_emby_sync.sh` 里写死的 Emby API Key 与生产域名移出**：公开仓库里写死
  凭据等于已泄露。现在 `EMBY_URL` / `EMBY_API_KEY` 只从环境读，缺失时明确报错退出（退出码 2）。
- **支付回调金额核对**：验签只证明「通知来自网关」，不证明钱数；回调报了金额就与订单实付
  逐笔核对，对不上不发货（返回 `fail`），没带金额则记一行日志按原逻辑发货。
- **回调验签改用 `hmac.compare_digest`**，不再逐字节短路比较。
- **挂载路径越界**：`exists()` / `size()` 与 `list_dir` / `resolve` / `read_text` 统一口径，
  `../` 或符号链接不再探到挂载根之外（越界按「不存在 / 拿不到大小」处理，不是 500）。
- **关工单此前不写审计**：`POST /api/admin/tickets/{id}/close` 关单并推送用户，但两个兄弟端点
  （`PUT /tickets/{id}`、`POST /tickets/{id}/reply`）都写了审计、它没写——用户收到「工单已被关闭」，
  运营在操作日志里查不到是谁关的。现在补上（重复关单也留记录，并标明 `already_closed`）。
- **能力测试的「真实投递」也留审计**：能力测试不改配置，但会用管理员自己填的凭据**真的发出去东西**
  （邮件 / Telegram），所以同样留审计——只记能力名与结果，**不记 payload**（里面可能带收件人与凭据）。
- **管理后台写操作审计中间件**（`backend/emby_server/audit.py`）：`/api/admin/emby/*` 的成功
  写操作落进操作日志——删媒体库 / 删条目 / 删 115 账号 / 停全站转码以前在「操作日志」里查不到。
  写这条的护栏（`scripts/smoke_test_admin_audit.py`）时又抓出三个真 bug，一并修了：
  - **`/115/*` 的动作名全是死条目**：路径段 `115` 是字面量命名空间，不是 id，
    但「全数字就当 id」的口径把它换成 `{}`，于是删 / 改 / 验 115 账号在日志里只剩
    一句没有语义的 `emby_admin_write`。现在表里出现过的字面量段永不当作 id。
  - **会话键正则不认真实会话键**：`session_routes` 用 `secrets.token_urlsafe(16)` 生成
    （含 `-` / `_` / 大写），而正则只认小写字母与数字 → 「结束会话」在多数情况下没有动作名。
  - **子动作被记成父动作的名字**：「只差一段就退一级」的回退对字面量段也生效，
    `/mounts/test`（测一个还没保存的挂载）会被记成「创建存储挂载」——运维会看到一条并不存在的操作。
    现在回退只对 id 占位符 `{}` 生效。

#### 新增护栏（CI 门禁）

- `scripts/check_auth_coverage.py`（新）：**`/api/admin` 与 `/api/user` 下每个写端点都必须有鉴权**。
  漏掉一个 `Depends(get_current_user)` 不会让任何检查变红（路由照常注册、类型检查照常通过、
  已有冒烟也不会去点那个新端点），结果就是一个「既没登录也能写」的接口。排摸结果是干净的：
  112 个写端点里只有 5 个没鉴权依赖，全是注册 / 登录 / 刷新令牌 / 支付网关回调（靠验签）/
  管理员登录（靠限流 + 验证码 + 登录日志）——但这五条现在是**显式登记**的，
  而不是靠没人发现；免鉴权登记必须写清替代机制，名单里出现死条目也会报错。
- `scripts/check_admin_audit_coverage.py`（新）：**每个 `/api/admin` 写端点都必须留下审计**。
  `/api/admin/emby/*` 由中间件覆盖，其余域必须自己调 `_audit(...)`；例外（纯探测端点与登录本身，
  后者已落在登录日志里）要显式登记并写清原因，名单里出现死条目同样报错。它看的是**真实路由**
  而不是文件，所以新增端点不需要谁记得改清单——检查会自己发现；
  `smoke_test_static_guards.py` 喂它一个合成模块，证明它会响、且不误报只读端点与中间件前缀。
- `scripts/check_branding.py`：仓库里不许再出现旧品牌名（`brand-scan: allow` 显式放行）。
- `scripts/check_hardcoded_secrets.py`：仓库里不许出现像真的密钥（`secret-scan: allow` 显式放行）。
- 三条静态护栏都进了 CI；`scripts/smoke_test_static_guards.py` 会喂它们违规样本，
  证明「护栏真的会响」而不只是永远返回 0。
- `scripts/smoke_test_admin_audit.py`（新）：后台写操作审计的行为护栏，含两个结构性断言——
  **应用里每个写端点都必须能解析出动作名**，以及 **`ACTION_TABLE` 里不许有死条目**。
  这两条正是「写了但没生效」的捕手（`/115/*` 四条规则曾全部匹配不上）。

#### ⚠️ 升级需要留意

- `EMBY_SERVER_ID` 默认值改为 `aetrix-emby-server`。若 `.env` 没显式设过它，升级后客户端会
  把服务器认成新的一台（重登一次即可）；想避免就在 `.env` 里保留旧值。
- 用旧栈脚本（容器名 `royalbot_postgres`、PostgreSQL 库 / 账号 `royalbot`）的部署：默认值
  变了，请用环境变量覆盖（`DB_CONTAINER` / `DB_USER` / `DB_NAME`）或沿用你自己的配置。
- **数据库文件不会被自动改名**：老部署继续用原来的库文件，新装才是 `aetrix_unified.db`。

## [2.29.0] - 2026-09-23

### 后台：把「一排按钮直接做事」收成「一个按钮 + 弹窗里详细操作」

后台很多行的末尾并排摆着 4~5 个按钮（批准 / 拒绝 / 转交 / 标记上架、测试 / 设为当前 / 编辑 /
停用 / 删除…），点下去往往只有一个确认框；而这些动作**需要的信息**又散在带 tooltip 的列里，
得来回扫。这一版把这类操作统一成同一个形状：**行里只留一个入口按钮，弹窗里先看全部事实，
再动手**。

#### 改动（九个页面）

- **求片管理**：5 个按钮 + 3 个二次输入框 → 一个「处理」弹窗（片名 / 年份 / 用户 / 求给哪个服 /
  用户备注 / 转交结果 / 当前状态 + 处理备注 + qB 链接输入 + 全部动作）。动作做完弹窗**不关**，
  就地换新快照——「批准 → 标记上架」不用再重新找到那一行；被当前状态筛选挡掉时如实说明并关闭
- **管理员与权限**：授权从「一直占着版面的输入框 + 下拉 + 按钮」改成页头一个按钮 + 弹窗
  （角色说明跟着选中的角色走）；行里的角色下拉 + 停用 + 撤销 → 「管理」弹窗（详情 + 角色 +
  启用/停用 + 撤销 + 护栏说明，改不动的原因直接写在弹窗里：这是你自己 / 需要超级管理员）
- **设备与安全**：封禁 / 移除两个按钮（只有一句确认框，看不到设备 ID、客户端版本、首末次时间）
  → 「详情」弹窗：设备全貌 + 两个动作各自的后果说明
- **订单**：待支付行的「补单 / 关闭」、已支付行的「退款」→ 统一一个「详情 / 处理」弹窗
  （订单号 / 商品 / 用户 / 金额 / 支付方式 / 创建与支付时间），可用动作随状态给，
  退款仍走那个勾选项多的退款弹窗
- **公告管理**：置顶 / 停用 / 删除收进「管理」弹窗（正文全文终于能读完，不再只有两行截断），
  每个动作的影响写在弹窗里（停用 ≠ 删除）；编辑保留为独立按钮（最高频）
- **优惠券**：记录 / 编辑 / 停用 / 删除 → 「管理」弹窗（折扣与人话口径、额度与占用中、
  门槛与封顶、有效期、为什么不可用、想删却有核销记录要怎么办）
- **115 账号**：编辑 / 校验 / 浏览 / 删除 → 「管理」弹窗（Cookie 是否已配置、上次校验结果与
  失败原因、默认账号、上次校验时间）；校验与浏览从弹窗入口进入，不叠加两层弹窗
- **存储来源**：浏览 / 测试 / 编辑 / 删除 → 「管理」弹窗（来源、归属服、被哪些媒体库使用、
  EM 与 EA 两边各自的结论与原始报错、上次测试时间、启停开关）
- **服务器与线路**：入口行 5 个按钮、内容自动化行 4 个按钮 → 统一的「管理」弹窗（连接、
  节点认领与 EA 自称、挂载体检、媒体库归属、逐行告警；动作里「设为当前」只给出流入口，
  与改动前一致）

#### 顺带修掉三处「写了但没生效」

- `115 账号` 页头部写的是 `admin-page-head` / `admin-page-desc`（这两个类从未定义过，标题栏
  一直没吃到全局样式）→ 改回全局的 `admin-page-header` / `admin-page-subtitle`
- `设备与安全` 的「已封禁」统计立牌用了 `is-danger`，但本页没有这条样式（另一页有）→ 补上
- `.user-name` 在五个页面（操作日志 / 设备 / 邀请 / 订阅 / 媒体库）都在用、全局从未定义 →
  收敛成 `index.css` 里的一条（同一个语义不再在各页不一样）

#### 细节

- 弹窗里的键值行统一用全局 `.kv-list` 原语，只覆盖对齐（弹窗内值靠左：值是片名 / 备注 / 报错
  这类长文本，右对齐读不动）
- 弹窗内刷新快照：动作后把弹窗里的那一行换成后端最新返回（状态徽标、校验结果、额度就地变化），
  否则关掉并说明原因（已删除 / 不再符合当前筛选）
- 从弹窗进「编辑 / 浏览 / 核销记录」时先关掉当前层，不叠两三个弹窗

## [2.28.0] - 2026-09-23

### 播放可达性：把「面板扫描正常、播放节点找不到媒体」变成后台能看见的事

推荐拓扑是四个职责分开：**EM 控制面 / EA 数据面 / 独立 PostgreSQL / 统一媒体存储**，内部全走私网。
这一版把「职责分开之后最容易踩的那个坑」提前变成可查证的结论：媒体库的来源里有两类东西是
**主机相对**的——`Library.paths` 里的本机目录，以及 `local` / `strm` 类型的存储挂载。
分离部署时它们只存在于扫描的那台机器（EM）上，于是：**面板扫描完全正常、`item_count` 照样涨，
但客户端一点播放就是 404/502**，因为库里落的是 EM 上的 `file_path`，EA 那台机器根本没有。

#### 1. 逐库判定：ok / warn / bad，只说有证据的话

- 新模块 `emby_server/reachability.py`：结合「谁在出流」+「本服的 EA」+「EA 视角的挂载体检快照」
  给每条库一个判定与改法：
  - `bad`：**有证据**说明拿不到——EA 体检快照明确报这条挂载在 EA 上读不到（带原始报错），
    或挂载已停用 / 已被删而库里还引用着；
  - `warn`：**无法确认**——本机目录类来源没有任何体检通道（给出「换成共享 WebDAV/rclone」的改法）、
    或共享来源还没在 EA 上体检过；
  - `ok`：出流的机器能拿到内容（本机出流，或 EA 体检明确说能读到，例如两端挂同一份 NFS）。
- 判定不靠猜：没有快照就不报 `bad`（宁可 `warn = 去核实`），有快照就按 EA 的原始结论升级/降级；
  虚拟库（内容来自其它库）单独归类，避免对聚合库误报。

#### 2. 用户端地址一致性（分离部署另一个高发配错）

- 面板已关协议面（`ENABLE_EMBY_GATEWAY=false`）却仍让用户连面板 → `bad`（客户端只会拿到 404 指引页）；
- 地址解析成 `localhost` / `127.0.0.1` → `warn`（只有本机能连）；
- 面板还开着协议面但同时配了 EA 入口 → `warn`（两个地址都能访问，排查时容易看错是哪套在出流）；
- 入口是「已有 Emby」却用本机目录当来源 → `warn`（对方的库不在这台机器上）。

#### 3. 面板可见

- 「媒体库」页新增顶部**播放可达性**面板：出流方式 / 面板协议面开关 / 用户端地址 / EA 体检时间，
  外加逐库的 `读不到`、`待确认` 行（原因 + 改法直接写在行里，不用再读日志）；
- 每条媒体库卡片多一行「播放风险：…」（悬停看原因 + 改法），扫描徽标与最近一次扫描结果照旧；
- 新接口 `GET /api/admin/emby/reachability`（汇总 + 逐库 + 用户端地址）；媒体库列表接口的每条库新增
  `playback` 字段，面板不用再算一遍。

### 文档

- `docs/deploy-ea.md` 新增「推荐拓扑：四个职责分开」：四职责对照表、拓扑图、数据库按角色分账号
  （并明确说明**当前代码还做不到 EA 完全只读**，以及为什么）、媒体存储必须服务化（WebDAV / rclone RC，
  不要依赖任一侧的本机路径）、内部全部走私网只开两个域名、转码目录放 EA 本地 SSD、分阶段落地建议；
  排错表新增「面板扫描正常，客户端点播放 404/502」一行；多机多服的校验清单补上播放可达性一条。

### 测试

- 新增 `scripts/smoke_test_reachability.py`（31 项）并接入 CI：本机路径 / local 挂载 / 共享挂载 / NFS
  同路径 / 虚拟库 / 停用与已删挂载在「面板出流、EA 出流（有/无体检证据）、已有 Emby」各组合下的判定与
  文案，用户端地址一致性的四种情形，汇总排序与计数（停用库不计入），以及 HTTP 面（`/reachability`、
  `/libraries` 带 `playback`、未登录 401、EA 未连接时与其他自建端点一致的 503 闸门）。
- 回归：`smoke_test_scan_queue` / `scan_result` / `smoke_test_ea_split` / `smoke_test_emby_connection` /
  `smoke_test_mount_health` 与两条静态护栏全部通过。

## [2.27.0] - 2026-09-23

### 扫描队列：按远程挂载串行化 + 实时进度（一次线上日志审计的根因修复）

线上现象不是报错而是瓶颈：四个媒体库在同一分钟里依次点「扫描」，四个任务**合法地**同时跑起来。
单个库的互斥只防「同一个库重复扫描」，它不防「不同的库同时打同一个远程挂载」。
于是同一个 WebDAV / rclone 端点被四个任务反复 PROPFIND（日志里那条「同一路径每 5 秒重复一次」），
远端延迟被放大、SQLite 被四个写入方争抢，容器 CPU 打满而 `item_count` 长时间是 0。

#### 1. 同一远程挂载同时只跑一个扫描（根因）

- 媒体库扫描**入队**而不是直接起线程：任务锁住它要读的远程挂载（`mount_ids`），引用同一挂载的库
  **排队**而不是同时打远端；只读本机目录的库不吃这条串行化，照常并行
- **全局并发上限** `EMBY_SCAN_MAX_PARALLEL`（默认 2）：扫描是资源大户（ffprobe + 写库 + 远端 IO），
  不设上限时「多点几下」就能把机器打满
- 入队即就地派发：点完扫描拿到的响应是**真实状态**（已启动 / 排队第几位 / 在等哪个挂载），
  不再是「已入队」的猜测
- 队列开关 `EMBY_SCAN_QUEUE=0` 可回到升级前行为（谁点谁起线程），`EMBY_SCAN_MOUNT_SERIAL=0` 可单独关掉串行化

#### 2. 排队是正常状态（不报 409，可取消）

- 重复点同一个库的扫描**不报错**：合并成一条并记下被点了几次，响应里说明原因（「正在扫描中」
  「已在队列中（第 N 位）」），以及**在等哪个挂载**（挂载名字，不是 id）
- 排队的任务可以取消；**正在跑的不能取消**——中途停下会留下「扫了一半的库」（条目已入库、清理没做）
- 「刷新全部」「一键修复」「Emby 客户端刷新媒体库」也走同一条队列：批量触发不再把前面刚开始跑的库一起推到 WebDAV 上

#### 3. 一轮扫描内同一目录只列一次

- 扫描会话期间目录列举缓存**不看过期时间**：同一目录在一轮扫描里会被反复问到（目录指纹、外挂字幕、
  播放侧预取），TTL 只有 5 秒——扫几分钟就是几十次多余的 PROPFIND
- **远程请求并发上限** `MOUNT_REMOTE_CONCURRENCY`（默认 2）：网盘 / WebDAV / rclone 在高并发下不会更快
  （要么排队要么限流）；本机目录不吃这个名额，本地扫描不会被远程限流拖住

#### 4. 进度可查（不再只看容器 CPU）

- `Library.scan_progress`（老库自动补列）：阶段（枚举 / 处理 / 清理）、已发现、已处理、当前目录、
  本轮远程请求数，由队列每 `EMBY_SCAN_PROGRESS_FLUSH`（默认 2 秒）刷盘——刷新页面 / 另一个进程都能看到
- 进度上报是**包装接入**（`scan_instrument`）而不是往扫描主体里插行：扫描主体（批次化写库、增量指纹、
  清理决策）一行未改，没绑定扫描上下文时包装层完全空转（直接调 `scanner` 的单测行为与升级前一致）
- 进程崩溃 / 重启后由维护周期连进度快照一起清掉（否则面板会把一台已经重启的机器显示成「正在扫」）；
  扫描结束时先清进度再标「已结束」——反过来会留下「面板已显示跑完、库里还挂着上一轮进度」的瞬间

#### 5. 后台可见（媒体库页新增「扫描队列」面板）

- 三列：**正在扫描**（阶段 / 已处理 / 已发现 / 当前目录 / 本轮远程请求）、**排队中**（第几位、
  在等哪个挂载、等了多久、可直接取消）、**最近完成**（结果、耗时、排队时长、错误原因）
- 面板头部写明当前并发上限、挂载是否串行、远程请求 / 复用 / 在飞计数；媒体库卡片上的徽标也区分
  「排队中（第 N 位）」与「扫描中 · 阶段」，并单独一行给实时进度
- 新接口：`GET /api/admin/emby/scan-queue`（快照）、`DELETE /api/admin/emby/scan-queue/{lib_id}`（取消排队）、
  `GET /api/admin/emby/libraries/{lib_id}/scan-live`（单库实时状态，空闲 204）；媒体库列表带 `scan_live`
- 多机部署时如实区分：本进程队列里的任务有实时进度，归属节点在自己机器上扫的只标「由归属节点扫描」
  （不谎报一个本进程看不到的进度）

#### 6. 运维可见：排队决策进日志

- 入队 / 开始 / 结束 / 取消各记一行 INFO（“等挂载 [1]”“排队 1.9s”“同时 2 个在跑”“本轮远程列举 4 次”）：
  出问题时能直接从日志看出是入队了、在等谁，还是已经跑起来了，不用只盯面板上的“扫描中”

### 测试

- 新增 `scripts/verify_scan_queue_concurrency.py`：把那次审计的现场**复现一遍**再对照——
  真 WebDAV 服务（真 PROPFIND / 真 207、可配延迟、记录每个请求的时刻与在飞并发）+ 真管理端接口，
  四个库连点扫描（三个指向同一挂载 + 一个本机目录），分别用「队列关闭（升级前）」与「队列开启」
  跑一遍并打印应用日志、WebDAV 服务端日志、每秒进度时间线与结果对照。实测对照：
  同时在跑的扫描 **4 → 2**、同一挂载最长重叠 **0.7s → 0s**、并发写库互踩导致失败 **2 个 → 0 个**、
  每个远程库都枚举完整个挂载（升级前有 2 个库条目为 0）
- 新增 `scripts/smoke_test_scan_queue.py`（37 项）并接入 CI：按挂载串行化的并发证据（同一挂载未被并发占用）、
  并发上限、排队位置与等待原因、重复点击合并、取消的口径（排队可取消 / 在跑 409 / 不在队列 404）、
  会话内目录只列一次、远程并发上限与本机挂载不吃名额、实时进度与落库快照（结束后清空）、HTTP 面鉴权、
  以及「队列关掉 = 升级前行为」
- 回归：`smoke_test_scan_result` / `scan_budget` / `scan_incremental` / `scan_cleanup` / `maintenance` /
  `mount_cache` / `smoke_test_emby` 与两条静态护栏（await 契约、阻塞路由）全部通过

## [2.26.0] - 2026-09-23

### 管理员角色与权限 + 播放与客户端策略（继续对照云海后台补齐的两块）

云海的后台里有「管理员与权限」与「客户端策略 / 播放选路」，我们这边此前对应的是：后台只有一种
身份（`is_staff`），以及散在系统设置与环境变量里的转码、码率、客户端开关。这一版把这两块补上。

#### 管理员与权限（新增页面 `系统与审计 → 管理员与权限`）

- 🔐 **三层角色**（`backend/admin_roles.py`）：`super` 全部 / `operator` 日常运营 /
  `viewer` 只读审计。判定**只在一处**（`get_current_admin` 与 `require_staff`），
  不做端点级散落判断——散落的判定迟早会漏掉某个写接口，一处漏网等于角色无效
- 🚪 **顺手堵上四处侧门**：Emby 兼容面上的「触发全库重扫」「踢别人的播放会话」「释放全部转码」
  与 `/api/notifications/send|broadcast` 原先各自判 `is_staff`（不在 `/api/admin/*` 前缀下），
  只读角色可以从这些接口绕道写；现在它们与后台走同一处角色判定（运营照旧不受影响）
- 🪜 **升级不变权**：`admin_role` 为空的老管理员按 `super` 处理，升级前后权限完全一致，
  也不会出现「没人进得去后台」
- ✍️ **写权限边界**：`viewer` 的**所有**非 GET/HEAD/OPTIONS 请求一律 403（扫描、挂载体检、
  改库都算写），`operator` 改不了系统 / 经济设置、能力中心（上游 Key）、播放与客户端策略、
  管理员本身
- 🛡️ **三条真会踩到的护栏**：不能改自己（避免把自己关在门外）、不能没有超级管理员
  （最后一名 super 既不能降级也不能撤销）、被停用的账号不能当管理员（那种号永远登不进来）
- 🧾 授权 / 改角色 / 撤销 / 改停用都进审计日志；后台顶栏与侧边栏显示真实角色名
  （以前写死「超级管理员」），非 super 看到的受限入口带锁标记
- 👤 管理员**不新建账号**：后台账号就是前台账号（同一套 token 与密码），这一页只做
  「找到那个人 + 标记 + 定角色」；登录响应与 `/auth/me` 用同一份角色 payload，
  进后台立刻按真实角色渲染，不用先显示成「超级管理员」再纠正

#### 播放与客户端策略（新增页面 `用户与账号 → 客户端策略`）

- 🎞️ **转码开关**：关掉只放直连 / 直接播放（省 CPU，代价是客户端兼容性变差）；
  关闭时 `PlaybackInfo` 直接声明不支持转码，客户端会走直连，而不是拿到一个必然 403 的地址
- 🔢 **并发转码上限 / 码率上限**：上限满了只**拒绝新的**转码请求（正在看的人不被打断，
  不会把整机 CPU 打满让所有人一起卡）；码率上限同时收紧直连判定，超过上限的条目改走转码
- 🚫 **客户端准入**：UA 子串黑 / 白名单（白名单优先，填了就只放行列表内的客户端），
  被拦的客户端连播放信息都拿不到（不只是「播不了」）；**管理员不受限**（排障时不能被自己的策略挡住）
- 📊 **运行态不谎报**：面板显示的是**本进程**正在转码几路 / 上限多少 / 由谁出流（一体化 / EA /
  外部 Emby）与 ffmpeg 是否可用；分离部署时明确提示转码跑在 EA 上
- ⚙️ 缺省值与升级前**完全一致**（允许转码、不限并发、不限码率、不拦客户端），键值仍落在
  `SystemConfig`（EM 与 EA 共用一个库 → 面板改完，出流的 EA 立刻按新策略放行 / 拒绝）
- 🧩 下载开关与设备风控（原在「系统设置」）在这一页也有入口，与「设备与安全 / 登录日志」相邻

### 测试

- `scripts/smoke_test_admin_roles.py`（38 项）与 `scripts/smoke_test_client_policy.py`（27 项）
  已接入 CI：角色判定与写权限边界、升级不变权、三条护栏、策略缺省口径与真实路由拦截

## [2.25.0] - 2026-09-23

### 后台信息架构按交付链重组（借鉴云海后台的信息架构）

以前后台把「服务器 · Emby 总览 / 媒体库 / 存储挂载 / 115 账号 / 求片管理」拆在多个技术分类里，
操作前得先理解内部架构（哪个是节点、哪个是库、哪个是挂载）。现在按一条交付链组织：

| 分组 | 页面 |
| --- | --- |
| 仪表盘 | 仪表盘 |
| 用户与账号 | 用户 · 订阅与权益 · 设备与安全 · 登录日志 |
| 媒体与交付 | 服务器与线路 · 媒体库 · 存储来源 · 115 账号 |
| 求片与内容 | 求片管理 · 公告管理 |
| 运营中心 | 商品与套餐 · 订单 · 兑换码 · 优惠券 · 邀请与积分 · 卡码管理 |
| 服务支持 | 工单 |
| 系统与审计 | 系统设置 · 操作日志 · 服务健康 |

- 🧭 **「多服」不再是一级概念**：服管理（`/realms`）不再占一个一级菜单，入口在「服务器与线路」
  右上角与仪表盘「各服概况」里；**当前服与范围（全部服 / 仅当前服）直接放在「服务器与线路」页顶部**，
  与顶栏切换器同一份状态
- 🖥️ **「服务器 · Emby 总览」→「服务器与线路」**：不要求管理员先把「节点 / 入口 / 线路」拆开理解；
  页头直接给出相邻页入口（服管理 / 存储来源 / 媒体库）
- 🗂️ **「存储挂载」→「存储来源」**：这一列不再只写「绑定 N 个」，而是**直接写出被哪些媒体库使用**
  （最多两个库名 + 其余数量，悬停看全部）
- 📚 **媒体库卡片一屏说完**：服务（归属节点，节点体检不过会变黄）、来源（几条路径 + 几个挂载，
  直接跟上「N 条读不到 / N 条为空」）、扫描状态徽标与增量、条目数——不用来回跳页拼
- 📊 **首页交付链数据卡**（云海式）：用户总数 / 当前播放 / 待处理求片 / 待处理工单 / 扫描状态 /
  存储健康，六张卡每张直接点进对应明细页；扫描与存储两张卡把「异常 / 未体检 / 播放节点够不着」
  写在卡面上，状态色只在真的有事时才亮
- 🏷️ **命名统一**：用户 / 订阅与权益 / 设备与安全 / 登录日志 / 工单 / 订单 的导航名与页内标题、
  浏览器标题三者对齐
- ♻️ **旧路由一个没删**（页面路径全部保留，旧收藏与外部链接照旧打开），`/emby-servers → /servers`
  与 `/transfer-115 → /pan115` 两条历史跳转继续有效

### 测试

- 前端路由契约、设计令牌契约、静态护栏、`vue-tsc` 与两个前端构建全部通过。

## [2.24.0] - 2026-09-23

### 新增：扫描结果按来源拆开（哪条路径 / 挂载扫到了什么）

v2.22.0 / v2.23.0 让「最近一次扫描结果」和「每轮扫描流水」落了库。但那些数字是**所有来源加在
一起**的：挂载配好了、目录也在、里面却一条文件都没有（挂载点被清空 / 账号范围变了 / 路径写错
但恰好存在），总体统计看上去完全正常，只有翻日志才知道某个来源其实空着。

- **每条来源单独记账**：路径 / 挂载各自一行，记「发现了多少个文件」以及这一批的新增、更新、
  探测、刮削、修复、未变，随 `scan_stats` 一起落库，`last_scan` 与每轮流水都能查到；
  - 「发现数」而不是「写库数」：增量扫描会跳过未变化的文件，按写库条数计的话，一个全都没变的
    来源会显示成 0 个文件——那是假象。
- **读不到的来源也有一条**（`kind: unavailable` + 原因 + 文件数 0）：不再出现「配了 3 条来源、
  明细里只有 2 条」这种让人怀疑自己数错的情况；两条路径都仍照旧计进 `failed_roots`、仍触发
  `partial`（原有护栏不变）。
- **挂载半路炸只影响它自己那条明细**：同一轮里别的来源照常记账，错误挂在出错的那条上
  （靠「来源标签 + 原因」前缀归属，不串到下一条头上）。
- **上限与清洗**：一个库最多记 50 条来源，标签 / 原因截断，单条坏数据只丢它自己（不会因为一条
  来源烂掉就把整轮统计丢掉）。
- **管理端**：媒体库卡片在有「别的来源有文件、这条一条都没有」的时候给出提示；扫描记录抽屉
  新增「来源」列（条数 + 空来源数），悬停看每条来源的文件数与增量。

### 测试

- `scripts/smoke_test_scan_result.py`：68 → 87 项，新增第 10 节（顺序与文件数、增量跳过时仍报
  真实发现数、不可用来源、挂载半路炸、条数封顶、坏数据清洗、落库往返、虚拟库空数组）。
- 回归：CI 里的 42 套后端冒烟 + 2 条静态护栏 + pytest 34 项 + 性能护栏全部通过。

## [2.23.1] - 2026-09-23

修复 2.23.0 之后合入的播放地址加固带来的两个真问题（其中一个直接把 main 的 CI 弄红）。

### 修复 (Fixed)
- **内网直链被一刀切拦掉**：`validate_remote_url` 在**解析阶段**就拒绝内网 / 回环地址，
  而这一层面对的是**配置来源**（媒体库 `paths` 里的 `.strm`、存储挂载解析出的地址）——
  局域网 NAS、自建 WebDAV / AList / MinIO、rclone 的本地 HTTP 端点都是这个项目的正常用法
  （`MOUNT_*_BASE` / `MOUNT_S3_REGION` 这类入口就是给自建端点用的）。现在拆成两套口径：
  配置来源**默认放行**内网（只拦非 `http(s)` 与 URL 内嵌凭据），需要更严的部署用
  `EMBY_BLOCK_PRIVATE_MEDIA_URLS=true` 打开，再用 `EMBY_MEDIA_URL_ALLOWLIST` 放行自己的地址。
- **解析失败被当成内网地址**（CI 红的直接原因）：`_resolves_private` 在 `getaddrinfo` 失败时
  fail-closed，于是「DNS 里没有这个名字」＝「内网地址」——离线 / 内网 DNS / 临时解析故障
  会把公开 CDN 直链一起拒掉（`smoke_test_mounts` 里的 `cdn.example.com` 就是这么失败的）。
  现在解析失败一律不算内网：真正的请求会以「源站不可达」如实失败。

### 变更 (Changed)
- **把 SSRF 防护放到真正能被利用的地方—重定向**：代理不再让 httpx 自己跟 3xx（
  `follow_redirects=True` 会把服务器带着你的 Cookie / Basic 引到任意主机），而是自己逐跳追：
  每一跳的目标都过 `reject_private_url`（内网 / 回环 / 云元数据一律拒，不受严格开关影响），
  **跨主机重定向剥掉凭据头**（同主机保留，WebDAV 的相对跳转仍能续上鉴权），最多 5 跳，
  超了给 502 而不是无限转。同步与异步代理同一套口径。
- `tests/` 接入 CI（新增 pytest 步骤）：这批单元测试（路径沙箱 / 播放地址口径）以前没有任何
  运行入口，而“播放地址口径改坏了”本可以在单元层就拦住。

### 测试 (Tests)
- 新增 `tests/test_playback_url_rules.py`（21 项）：配置来源默认放行内网、严格开关与 allowlist、
  非 `http(s)` / 内嵌凭据被拦、解析失败不算内网、重定向目标（内网 / 回环 / 云元数据 / IPv6 回环）
  一律拒、相对 Location 解析、跨主机剥凭据 / 同主机保留。
- `scripts/smoke_test_mounts.py` 补上回归断言（“本机 .strm 指向内网仍可播” 正是这次被打破的场景，
  以及开关 / allowlist / 重定向三组口径）。
- 全套验证：CI 里的 **45 个后端步骤**（含 42 套冒烟 + 性能护栏）本地逐条跑过全绿，
  `tests/` 34 项 pytest 通过；版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 2.23.1。

## [2.23.0] - 2026-09-23

上一版（2.22.0）把「最近一次扫描」变成了可查，但只剩最后一次仍然回答不了一个常见问题：
**这个库是每轮都扫不动，还是只是刚才那一下出问题了？** 这一版把扫描做成流水，并补上
崩溃中断的收尾。仍然沿用 [go-emby](https://github.com/sd87671067/go-emby) 的「活动日志」思路。

### 新增 (Added)
- **扫描流水 `emby_scan_runs`**（新表，`create_all` 建，无需手工迁移）：每轮扫描一行，
  记状态、**触发方**（`manual` 面板 / `client` 客户端刷新 / `node` 归属节点 / `repair` 修复队列）、
  开始与结束时间、耗时、与库里同一份统计、失败原因。
  每库只留最近 `EMBY_SCAN_HISTORY` 条（默认 20，0 = 关闭记录），收尾时就地回收最旧的行；
  媒体库删掉后剩下的行由维护周期收掉（`scan_runs_pruned`）。
- **`GET /api/admin/emby/libraries/{id}/scans`**（新增 `scanner.scan_runs_payload()`，
  与「最近一次」共用 `_scan_metrics` 一份口径）与**后台媒体库卡片的「记录」抽屉**：
  列出最近若干轮的状态 / 触发方 / 增量 / 耗时 / 原因，手机端同样可用（DataTable 两种形态）。

### 修复 (Fixed)
- **崩溃后「扫描中」会永远停在那个状态**：进程被强杀时 `is_scanning` 由启动维护复位，
  但 `scan_status` 与流水行没人收尾——页面就永远显示「扫描中/未完成」，连失败原因都没有。
  现在启动维护同时把两者收尾成 `failed` + 「进程重启，本轮扫描未完成」
  （`reset_stale_scan_flags` 补了终态、新增 `close_stale_scan_runs`），
  且与原来同一套安全口径：本进程真的在扫的库、以及开始时间在阈值内（默认 6 小时）的一律不碰
  ——多机部署时那可能是另一台节点正在扫的库。

### 变更 (Changed)
- 扫描入口新增 `trigger` 参数，四个调用点（面板 / 修复队列 / 节点 / 客户端刷新）各自如实标注；
  未知值收敛成 `manual`（不因为调用方写错就丢记录）。
- 收尾过的流水一定有耗时：调用方没传就从 `started_at` 算出来，不再留空。

### 测试 (Tests)
- `scripts/smoke_test_scan_result.py` 扩到 **68 项断言**：新增流水部分——每轮一条且按时间倒序、
  失败/部分失败的记录带原因与来源、`limit` 生效、每库上限就地回收旧行、
  `EMBY_SCAN_HISTORY=0` 不记流水但扫描照常、媒体库删除后由维护周期回收、
  接口字段契约，以及**崩溃收尾**（标志与流水都收尾 + 阈值内的「正在扫」不被误判）。
- `scripts/smoke_test_maintenance.py` 的维护计数契约加上 `scan_runs_pruned`。
- 回归：扫描清理 / 增量扫描 / 扫描预算 / 长期运行维护 / 存储挂载 / 全站搜索 / EM 主链路 /
  两条静态护栏 全部重跑通过； `vue-tsc`、前端路由与设计令牌契约、`vite build` 均通过。

### 说明 (Notes)
- 版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 2.23.0。

## [2.22.0] - 2026-09-23

扫描的**结果**以前只活在返回值与日志里：管理端刷新一下就没了，想知道「上一轮到底扫到什么」
只能去服务器翻日志。这一版把结果留在库里、挂到媒体库接口上，后台一眼可见。

参考 [go-emby](https://github.com/sd87671067/go-emby) 的「活动与错误日志」设计思路
（每个库的最近一次扫描结果、哪些来源读不到），按本项目既有的「扫描互斥 + 数据库状态」结构实现。

### 新增 (Added)
- **最近一次扫描结果落库**（`emby_libraries` 新增 `scan_status` / `scan_stats` / `scan_error`）：
  `scan_status` 取 `running` / `success` / `partial` / `failed`，`scan_stats` 存 JSON
  （新增 / 更新 / 删除 / 探测 / 刮削 / 修复 / 未变 / 耗时 / 读不到的来源）。
  老库由 `_auto_migrate` 自动补列，补出来的 NULL 等价于「尚未扫描」，行为与升级前一致。
- **`GET /api/admin/emby/libraries` 带回 `last_scan`**（新增 `scanner.scan_result_payload()`，
  管理端与将来的客户端接口共用同一份口径）：状态、完成时间、耗时与各项统计、读不到的来源、
  失败原因。**从未扫描过返回 `null`**——「没扫过」和「扫了但都是 0」是两件事，不能混成一个空结构。
- **后台媒体库卡片显示最近一次扫描**：结果徽标（扫描正常 / 来源不完整 / 扫描失败 / 上次扫描未完成）、
  一行增量摘要（新增 / 更新 / 删除 / 未变 / 修复 / 耗时），以及来源读不到或异常时的原因（悬停看全文）。

### 变更 (Changed)
- **扫描状态与结果改由 `scan_library_sync` 统一写回**（扫描主体拆到 `_scan_library_body`）：
  成功、部分失败、异常三条路都会落库，异常时连异常类型与消息一起记下来，不再只留在日志里。
  写状态失败只记日志，**不影响扫描本身**。
- **`partial` 是有意义的中间态**：只要有任何来源读不到（目录不存在 / 挂载停用 / 网络不通），
  本轮就**跳过清理阶段**——这种结果本身不完整，所以记 `partial` 而不是 `success`，
  原因写明是哪个来源读不到；而「媒体库根本没配来源」与「来源读不到」分开措辞。
- 进程内互斥（`is_scan_active`）的获取与释放收敛到 `scan_library_sync` 一处：
  无论成功、失败还是异常，占位一定会释放，这个库不会因此再也扫不动。

### 测试 (Tests)
- 新增 **`scripts/smoke_test_scan_result.py`（41 项断言）**：从没扫过时 `last_scan` 为 `null`；
  成功一轮的统计落库且**换个 Session 仍能读到**（证明是落库不是内存）、`duration_ms` 不被白名单丢弃；
  重扫覆盖而不是累加；来源读不到 → `partial` + 点名来源 + 跳过清理（磁盘上已消失的条目仍留库）；
  来源恢复 → 回到 `success` 并补上被跳过的清理；扫描抛异常 → `failed` + 原因落库 + 互斥与
  `is_scanning` 均复位（之后还能正常重扫）；扫描中 → `running`；虚拟库算成功、
  **完全没配来源算 `partial`**。
- 回归：扫描清理 / 增量扫描 / 扫描预算 / 全站搜索 / 长期运行维护 / EM 主链路 / 存储挂载 /
  两条静态护栏全部重跑通过；`vue-tsc` 前后端类型检查通过。

### 说明 (Notes)
- 版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 2.22.0。

## [2.21.0] - 2026-09-22

### 变更 (Changed)
- **管理后台按域拆分**：`backend/api/admin.py`（两千多行）拆成 `admin_core.py`（公共骨架：
  `admin_router` / `get_current_admin` / `_audit` / `_generate_code` / `_log_out`）、
  `admin_auth.py`（登录 / 身份 / 改密）、`admin.py`（订阅、用户、卡码、公告、工单、求片、
  操作日志、统计）与 `admin_economy.py`（邀请返利、积分台账、经济设置与统计）。三个模块注册到
  同一个路由对象，**导入顺序 = 拆分前的定义顺序**。已验证：端点声明 391 条无增无减、
  管理路由 53 条注册顺序逐条一致。同时把八个兄弟模块原先从 `admin.py` 取的共享骨架
  统一改为从 `admin_core` 取（拆分本来就是为了去掉这个依赖）。
- **100 个「体内没有 `await`」的 `async` 路由改成同步 `def`**：FastAPI 对 `def` 端点会丢进
  线程池、对 `async def` 端点在事件循环上直接跑，而本项目用的是同步 SQLAlchemy——
  一个查询就能把全站按住。这批端点体内没有任何 `await`，所以只改签名、业务代码一个字没动。
  现在全站同步 `def` 路由 92 个。

### 新增 (Added)
- **两条静态契约护栏（进 CI，且只减不增）**：
  - `scripts/check_await_consistency.py`：`await f(...)` 的目标必须是协程函数。
    把「`async` 端点改成同步 `def`、但别处还在 `await` 复用」这类编译和类型检查都看不出来、
    只有真打到那个请求才炸（`TypeError: object dict can't be used in 'await' expression`）的改动
    挡在门口；
  - `scripts/check_blocking_routes.py`：`async` 路由里出现同步 DB 调用即失败。存量 41 条记在
    脚本的 `BASELINE` 里，改好一条删一行，新增一条过不了 CI。
- **护栏自检 `scripts/smoke_test_static_guards.py`**（进 CI）：护栏最常见的失效方式
  不是报错而是**根本不会响**（正则写歪 / 路径找错 / 基线比实际大，于是永远返回 0）。
  这条拿合成代码树喂两条护栏，每条都要求「真实代码树上退出码为 0」且「合成代码树上必须失败
  并点名」：await 契约看「`async` 端点里 `await` 一个纯同步裸名函数」；阻塞路由额外钉住
  「扫描结果与基线逐条相等」与四种口径用例（`async`+`db.commit` 要报、同步 `def` 不报、
  不碰库不报、直接调用的嵌套函数要报、下放的嵌套函数不报）。

### 说明 (Notes)
- **已知欠账：41 个 `async` 路由仍在事件循环上跑同步 SQLAlchemy。** 它们都有必须 `await`
  的东西（通知推送 / 网络探测 / `await request.json()`），真正麻烦的地方在于那些「必须 await
  的东西」**自己也**在 `async` 函数里写库（`backend/notifications.py` 的三个通知函数、
  `backend/servers.py::probe_and_store`）。所以这不是 41 个端点的事，要连通知层 / 探测层
  一起拆，不能靠一次机械替换收尾。清单、修法与优先顺序（先动热路径
  `compat_routes.session_progress` / `api.user_views`）见 `docs/performance.md` 二·七。
- 8 套此前只在开发机上跑的冒烟测试接入 CI（`emby` / `emby_gateway` / `ea_split` /
  `media_search` / `mounts` / `paywall` / `scan_cleanup` / `user_v250`），CI 里现在共 38 套。
  接入时按「CI 的后端冒烟作业**不构建前端**」这个事实修正了两处环境相关断言：
  - `smoke_test_admin_v240.py`：两条「某条特定订阅必须出现在第一页」改成字段契约断言
    （列表默认 `limit=50` 且按 `end_date` 升序，本机反复跑会把库撑大——CI 空库不会碰到）；
  - `smoke_test_ea_split.py`：两条「门户 SPA 不被 EA 指引路由吃掉」改为**有构建产物时才断言**
    （SPA 本来就只在 `user_frontend/dist` 存在时才挂载，没构建时 `/` 返回 API JSON、
    `/wallet` 返回 404 是正确行为），没构建时打印 SKIP 并说明如何覆盖。
  - 另外 CI 显式 `pip install zhconv`：它是**可选依赖**（不在 `requirements.txt` 里，
    未安装时搜索会自动降级为不做转换），但门禁要覆盖繁简互搜这个特性，
    否则 `smoke_test_media_search` 里那条断言只会 SKIP，等于没测。
  - 全套 38 套已按冒烟作业的真实形态（`FRONTEND_DIST` / `ADMIN_DIST` 指向不存在的目录）
    本地跑过一遍，全绿；并逐一核过所有冒烟测试的第三方导入都在 CI 的安装范围内。

## [2.20.2] - 2026-09-22

### 修复 (Fixed)
- **停止转码堵塞事件循环**（与 v2.12/v2.13 修的是同一类问题，它是漏网的那一个）：
  `stop_transcode()` 要 `terminate` 子进程、等它退出（最多 5 秒）、再递归删掉整场播放的分片目录，
  而「结束自己的播放 / 管理员结束会话 / 停掉全部转码」三条 `async def` 路由直接调它——
  一次结束播放就能把整个进程按住。现在事件循环上一律走新增的异步变体
  （`stop_transcode_async` / `stop_transcodes_for_async` / `stop_all_transcodes_async`：
  阻塞部分 `asyncio.to_thread`，多会话 `asyncio.gather` 并发停），同步版本只留给同步路由与
  后台维护线程。实测同一份 0.6 秒的等待：同步调用让事件循环出现 **0.61s** 空洞，异步只 **0.01s**。
- **「结束播放」其实没停掉转码**（实现过程中发现）：转码会话 id 是 `start_transcode` 生成的
  uuid（只出现在 HLS 播放列表的 `?session=` 上），而这几条路由拿客户端的 `PlaySessionId`
  （播放会话键，随机 `s…`）当 id 用 —— 键对不上，`pop` 永远拿不到东西，进程只能等维护周期
  回收。现在按「用户 + 条目 guid」反查（新增 `find_transcodes` / `stop_transcodes_for[_async]`），
  结束播放真的会释放对应的 ffmpeg 进程与分片目录。

## [2.20.1] - 2026-09-22

### 修复 (Fixed)
- **改完代理后 TMDB 刮削仍走旧出口**：httpx 只在**构造 client 时**读一遍代理环境变量
  （`trust_env`），而刮削客户端是进程级单例、跨很多次扫描活着——于是「代理填了但 TMDB 还是
  连不上」只能靠重启进程解决。现在客户端在每次请求前比一次进程代理指纹
  （`proxy.signature()`），变了就重建连接池并关掉旧的：保存代理、`apply_all()` 启动落地、
  将来别处改环境都会立即生效，指纹未变时不重建（不是每次请求都换连接）。
  其余调用点本来就是「当次请求建、当次请求关」，无需改动。

## [2.20.0] - 2026-09-22

能力中心补齐三件事，并把这套新代码从头审了一遍——审出来的几个真问题一并修了。

### 新增 (Added)
- **站点与品牌**能力（第七张卡片）：站点名称 / Logo / 主题色 / SEO 标题、描述、关键词，
  驱动用户端顶栏站名与 Logo、登录页标题、浏览器标题与 meta，以及后台侧边栏与标题。
  新增公开端点 `GET /api/site/branding`（无需登录，不含任何凭据）。
- **后台登录也可加人机验证**：新增字段「保护管理后台登录」，用户端登录、注册、后台登录共用
  同一个守卫（`captcha.guard`）——后台登录原本是整个站点唯一没有这道闸的登录路径。
  后台登录页随之接上挂件（`admin_frontend/src/components/CaptchaChallenge.vue`）。
- **AI 配额落库**（`ai_usage` 表，唯一约束 `user_id + day`）：原先的进程内字典在多进程部署下
  会被放大成「上限 × 进程数」且重启归零；现在多 worker 共用一份计数，占用额度用条件 UPDATE
  （`count < limit`）实现，并发下不会两个请求都读到 `limit-1` 而各记一次。维护周期按 90 天回收。

### 修复 (Fixed)
- **新加的路由把事件循环按住了**（v2.19.0 引入，同类问题在 v2.12/v2.13 修过一轮）：
  能力测试会真实发起网络请求 / SMTP 投递、保存会落配置、AI 问答是阻塞 httpx（默认超时 60 秒），
  而它们原先都是 `async def`——一次「测试连接」就能把整个进程的播放卡住。现在这六个端点
  全部改成同步 `def`，由 FastAPI 丢进线程池。
- `notifications._load_channels_from_config` 用 `next(get_db())` 拿会话且从不关闭，每次重建
  通知渠道都漏一个连接；改为显式开关。
- 能力配置读取从「一个字段一次查询」改成一次 IN 查询：人机验证挂件从 5 次往返降到 1 次，
  AI 问答从 4 次降到 1 次，能力总览整页从 29 次降到 1 次（新增 `backend/integrations/store.py`，
  写入同样是批量的）。

### 变更 (Changed)
- 品牌相关输入做了**格式校验**：主题色必须是十六进制色（它会被写进 CSS 自定义属性，
  不校验等于把一个可写任意 CSS 的注入点交给后台表单），Logo 只接受 `http(s)://` 或站内相对
  路径（`javascript:` / `data:` 一律忽略）——两项都在能力模块里，接口只下发校验后的值。
- 能力卡片新增 `testable`：没有 `test()` 的能力（站点与品牌）不再显示一个点了只会回
  「不支持测试」的按钮。
- 后台登录失败的人机验证日志带上了用户名（原先只有 IP 与原因）。

### 测试 (Tests)
- `smoke_test_capabilities.py` 扩到 **114 项断言**：后台登录验证（拦下 / 放行 / 关闭后恢复）、
  配额落库与「上限 2 时连续占用只放行 2 次」、跨会话看到同一份计数、过期用量回收、
  品牌端点不泄漏凭据字段、非法主题色与 `javascript:` Logo 被回落、测试按钮标记。

## [2.19.0] - 2026-09-22

**外部服务能力中心**：后台「系统设置」新增六个能力卡片——网络代理、人机验证、邮件与模板、
Telegram 通知、AI 模型设置、IP 与地理位置。

口径只有一句话：**面板只提供能力，凭据全部由你自己填**。项目不内置任何 API Key / 站点密钥 /
SMTP 密码 / 代理地址 / Bot Token，也不锁定任何一家服务商；每个能力都带一个会**真实发起请求**的
测试按钮，把失败原因原样报出来，便于区分「密钥填错」与「网络不通」。

### 新增 (Added)
- **能力框架** `backend/integrations/`：一个能力一个模块，模块声明 `SPEC`（含字段表：
  `str` / `secret` / `int` / `bool` / `select`）、可选的 `apply()`（落地副作用）与
  `test()`（真实连通性）。后台总览、配置表单与测试全部由字段表驱动——**加能力不用改前端**。
  密钥字段只回 `******`：保持 `******` = 不修改，**清空 = 删除已保存的值**（否则密钥只换不撤）；
  操作审计只记字段名、不记值。
- **网络代理**：为后端全部出站请求配 `http` / `https` / `socks5(h)` 代理并测试连通性。
  落地方式写进程环境变量（`HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` / `NO_PROXY`），
  httpx / requests / rclone 都认，**不需要改任何调用点**；关闭即清除，重启自动恢复。
- **人机验证**：登录 / 注册可分别开启 Turnstile / reCAPTCHA / hCaptcha；站点密钥由
  `GET /api/user/auth/captcha` 下发给挂件，私钥永不出后端。**没配置就放行**（不把站点锁在门外），
  校验失败落一条 `人机验证失败` 安全日志。
- **邮件与模板**：SMTP 配置 + 发件人显示名 / 发件地址（非 ASCII 走 RFC 2047 编码），
  保存即重建通知渠道（**不重启就生效**）；「发送测试邮件」真发一封。
  端口 465 走 SSL、其余走 STARTTLS。
- **Telegram 通知**：填 Bot Token 即启用，`getMe` 测试并回显机器人；保存即重建渠道。
- **AI 模型设置**：任何 OpenAI 兼容端点（官方 / 中转 / 本地 Ollama、vLLM、one-api…），
  **多密钥**轮换，可设系统提示词、温度、超时与每用户每日上限。
- **AI 助手（用户侧）**：新页面 `/assistant` 与新端点 `GET /api/user/ai/status`、
  `POST /api/user/ai/ask`；未配置时用户端**不显示可点入口**，页面直接说明原因。
- **IP 与地理位置**：腾讯地图 IP 定位或本地 MMDB（二选一），结果按配置时长缓存、
  **失败结果只缓存 5 分钟**；登录日志写入时自动带上归属地，后台日志列表新增「IP / 归属地」列与
  筛选原因「人机验证失败」。

### 变更 (Changed)
- `LoginLog` 新增 `region` 列（老库由 `_auto_migrate` 自动补列）；未配置归属地能力时一个外部请求都不发。
- 后台「系统设置」页重构为**能力卡片 + 抽屉表单**：卡片显示「已配置 / 已启用」两个维度，
  抽屉按字段表渲染控件，并内联展示测试结果（不再只弹一句 toast）。注册策略与经济分组保持原位。
- `notifications.py`：`EmailChannel` 支持发件人显示名 / 发件地址，新增 `refresh_channels()`，
  让后台保存邮件或 Telegram 配置后**不重启就生效**。

### 测试 (Tests)
- 新增 `scripts/smoke_test_capabilities.py`（**90 项断言**，已接入 CI）：钉住四件事——
  未配置时必须无害（放行 / 不显示 / 不查外部）、密钥永不明文回显且掩码不覆盖原值、
  保存即生效（代理环境变量、通知渠道、GeoIP 缓存失效）、测试真在打网络（假出口断言请求真的发出）。
  另外覆盖：配额与多密钥轮换、客户端伪造 `system` 轮次被丢弃、非法枚举 / 非法整数的回落、
  未声明字段被忽略、审计不落密钥值。

### 文档 (Docs)
- 新增 `docs/capabilities.md`：每个能力要准备什么、填在哪里、用在哪条链路上，以及
  「加一个新能力要写什么」。

## [2.18.0] - 2026-09-22

把「115 下载与转存」整套下线，只保留 **115 账号 + 直挂**。

理由：转存链路是全项目最脆的一块——要抓随时会变的第三方分享接口 + Cookie 型 API +
一套任务状态机 + 落盘状态 + 断点续跑 + 鉴权等待；而收益基本为零（把分享搬进自己账号这件事，
在 115 官方 App / 网页里一分钟就能做一次）。真正长期要用的是 115 直挂：面板直接读网盘目录，
播放时由 EA 按 Range 换直链转发。

### 移除 (Removed)
- **转存 / 下载任务引擎**（`transfer115.py` 926 → **256 行**）：分享链接解析、任务状态机
  （`create_task` / `run_task` / `_process` / 重试 / 取消）、按文件原子落盘、断点续跑全部删除。
  保留账号配置档、Cookie 解析优先级、`Pan115Client`（校验 / 列目录 / 取下载地址）与
  `verify_account`（保留部分函数体与旧实现逐字节一致，仅删掉 `share_snapshot` / `receive`）。
- **后台接口**：`POST /115/parse`、`GET|POST /115/tasks*` 下线（账号 CRUD、`/115/verify`、
  `/115/browse` 保留）。
- **数据模型**：`Pan115Task` 与它的 `__all__` 导出删除。既有库里的 `pan115_tasks` 表不再被读写，
  留表不动（不自动 DROP，避免误删数据）；确认不要了可自行 `DROP TABLE pan115_tasks`。
- **启动逻辑**：EM 启动时不再续跑 115 任务（`resume_pending_tasks` / `cleanup_stale_tmp` 调用与
  `transfer115` 导入一并删除）。
- **环境变量**：`PAN115_STATE_DIR`、`PAN115_STALE_TMP_SECONDS`、`PAN115_SHARE_BASE` 退出
  `env.example`；转码根目录下的历史 `pan115` 状态目录也不在清理保护名单里了（会被维护周期回收，
  `subs` 照旧保护）。
- **前端**：「115 转存」页改写为「115 账号」（路由 `/pan115`，旧地址 `transfer-115` 保留为别名，
  收藏不会 404）：只留账号配置档与目录浏览（用某个配置档实测一次列目录）。转存任务表、
  新建任务弹窗、相关 API 与类型定义全部删除。

### 变更 (Changed)
- `portal.py` 的「存储挂载」端点拆到新模块 `portal_mount_routes.py`（369 行）：portal.py
  **1682 → 1229 行**。路由仍挂在同一个 `admin_emby_router` 上，由 `backend/main.py` 在
  `include_router` 之前导入注册——与既有 `media_routes` / `stream_routes` 的拆分方式一致。
- `resolve_cookie` 找不到指定配置档时抛 `Pan115Error`（原因 `ShareLinkError` 随分享解析一并删除），
  调用方（存储挂载 / 门户）现有的 `Pan115Error` 处理路径直接覆盖，不需要各自适配。

### 测试 (Tests)
- `scripts/smoke_test_115.py` 重写为 **43 项**：直挂客户端（校验 / 列目录 / 取下载地址 /
  目录项判定 / 鉴权类错误）、Cookie 优先级与媒体库绑定回退、账号 API 全流程（含 Cookie 不回传明文、
  删除后绑定解除）、未登录被拒绝、目录浏览，外加「转存接口与任务模型已不存在」的回归护栏。
  该脚本以前没在 CI 里跑过，本次一并接进 smoke 作业。
- 全量 35 套冒烟 + 前端路由/设计令牌契约 + `vue-tsc` 类型检查 + 部署自检全绿。
- 版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 2.18.0。

## [2.17.0] - 2026-09-22

把性能文档「已知瓶颈」里剩下的五项一次做掉：增量扫描、清理候选集、远程挂载目录列举缓存、
刮削图片本地化、扫描限速。主题只有一个——**别做白工**：重扫不重做没变的目录、
播放不再每次重新列网盘目录、封面不再每次去第三方拉。

### 性能 (Performance)
- **增量扫描（目录指纹）**：重扫一个库时绝大多数目录一个文件都没动过，旧实现却照样把每个文件
  走完整个流程（找图片、找字幕、重建外挂字幕轨、每文件一次提交）。现在按目录记指纹——
  本机用「目录 mtime + 目录项数」，远程挂载用「目录项数 + 总字节数」：
  - 指纹没变 → 该目录的文件**不做任何写库工作**，只把 guid 记进 `seen_guids`（否则清理阶段会把
    它们当「文件已删除」批量误删）并确认库里那一行存在且不需要重探/重刮/补详情；
  - 指纹变了 → 整个目录照常完整处理，处理完写回新指纹。
  **安全性**：跳过**永远**以「库里那一行存在且不过期」为前提，指纹只决定图片/字幕/轨道这些
  逐文件的重活要不要重做——即使指纹写早了、随后进程崩了，下次扫描也会因为「查不到那一行」
  重新处理，不会出现文件永远不入库。替换同名文件不一定动目录 mtime，所以前提里还要求
  「文件大小与库里的记录一致」。
  实测 2000 文件 / 40 目录的本机库：未变动重扫 2455 ms → **144 ms**（SQL 8034 → 29，逐文件
  处理 2000 → 0），追新（+1 个文件）136 ms 且只重做它所在的那个目录。`SCAN_INCREMENTAL=0`
  可整体关掉。**没配 TMDB 的部署同样吃到增量**：刮削不可能成功时不该因此让整库每轮重做。
- **清理阶段的候选集**：稳定库不再遍历整库条目（先用两次计数证明「没有可清理的条目」，退回
  遍历时改成**只取 4 个列的 Core 游标 + 每批 5000 条**）。十万条目稳定库 2097 ms → **37 ms**
  （同库 `COUNT(*)` 3.6 ms）；真需要遍历时 2226 ms → 569 ms。
- **远程挂载目录列举下沉到提供者层**：扫描方向本来就有「本次扫描内只列一次」的缓存，而**播放
  方向完全裸奔**（`resolve_play_target` 每次请求新建提供者，实例级 cid/pickcode 缓存对播放等于
  不存在）。现在 `mounts.cached_listing` 把九个提供者（含 115 的 raw 列表）都套上按
  「挂载 + 配置指纹 + 方法 + 目录」的 TTL 缓存，扫描与播放**共用同一份**：
  返回拷贝（调用方会排序/裁剪）、**失败不缓存**（网络抖动不被固化）、同一目录**单飞**
  （锁可重入，子类实现可能套在另一层被缓存的列举上）、`MOUNT_LIST_CACHE_MAX` 给内存封顶，
  扫描开始前 `clear_dir_cache()` 会连它一起清（扫描必须看当下的目录）。
  实测：两次播放从 4 次网盘列举降到 **2 次**；播放侧 `exists` + `size` + `exists` 只列 1 次。
  `MOUNT_LIST_CACHE_SECONDS=0` 可关掉；后台目录选择器走 `list_dir_uncached` 看当下。
- **刮削图片本地化**：封面原先只有 TMDB 远程地址，客户端每取一次图都要由服务器代理去第三方
  拉一趟。现在刮削时把图落一份到本地（`EMBY_IMAGE_DIR`，默认 `<EMBY_TRANSCODE_DIR>/images`），
  远程地址**始终保留**：缓存被清掉（/tmp 重启清空、维护淘汰、换过海报）会**自愈**——取图时
  发现本地不在就按需再落一份并记住路径；落不下来就退回原来的代理，行为与本地化之前一致。
  本地化过的图（在缓存目录里）优先走本地，**用户自己放进媒体目录的封面不受影响**。
  内容寻址 + 按 URL 单飞 + 「临时文件 → fsync → os.replace」原子落盘；
  维护周期回收没被任何条目引用的文件并按 `EMBY_IMAGE_CACHE_MB`（默认 2GB）从旧到新淘汰，
  **有引用的图一张不删**（宁可超上限并记日志），新增文件有保护期。`EMBY_LOCALIZE_IMAGES=0` 关闭。
- **扫描限速**：POSIX 的 `nice` 是**进程级**的，而扫描跑在 API 进程的后台线程里——直接降本进程
  会把正在播放的请求一起降，所以只降**子进程**：ffprobe 加 `nice -n N`（`SCAN_IO_NICE`，默认
  10），能用的系统上同时用 `ionice -c2 -n7` 降 IO 优先级。工具可用性只在启动时探一次
  （`ionice` 存在但内核不允许时不能挡住 ffprobe）。

### 新增 (Added)
- `emby_scan_dirs` 表（`ScanDirState`）：按媒体库 + 目录存指纹，`create_all` 建表，无需手工迁移；
  媒体库删除后由维护周期回收（`scan_dir_states_pruned`）。
- `backend/emby_server/image_store.py`：图片本地化的下载、原子落盘、单飞、按需自愈、清理与容量上限。
- 维护周期新增 `images_pruned` / `images_freed_bytes` 上报。
- `env.example` 新增扫描与图片本地化两组说明（`SCAN_INCREMENTAL` / `SCAN_IO_NICE` /
  `EMBY_LOCALIZE_IMAGES` / `EMBY_IMAGE_DIR` / `EMBY_IMAGE_CACHE_MB` / `EMBY_IMAGE_GRACE_SECONDS` /
  `MOUNT_LIST_CACHE_SECONDS` / `MOUNT_LIST_CACHE_MAX`）。

### 测试 (Tests) — 三个新冒烟脚本（均已接入 CI）
- **`scripts/smoke_test_scan_incremental.py`**（31 项）：未变动重扫不写库且条目一条不少、
  新增文件/新增目录/目录 mtime 变了/字幕新增都要处理、目录 mtime 没变但文件大小变了要重探、
  库里缺行必须重建、补图标记与到期重刮不能被跳过、关闭开关回到全量、远程挂载参与增量且
  不额外多列目录、目录读不到不会被当成「没变」、整个来源读不到时禁止清理、删库后指纹被回收。
- **`scripts/smoke_test_mount_cache.py`**（34 项）：扫描与播放共用一份列举、TTL 生效与过期、
  返回拷贝不被污染、失败不缓存（抖动不被固化）、配置指纹、并发单飞、`clear_dir_cache` 会清它、
  条目上限、开关关闭、播放侧 `exists`/`size` 复用、115 的 raw 列表与 cid 解析共用、两次播放的
  网盘请求数。
- **`scripts/smoke_test_image_localize.py`**（28 项）：落盘原子与内容一致、只下一次、并发单飞、
  404/非图片/不可达三种失败都退回远程图、刮削即本地化、缓存被清后取图自愈并记住路径、
  用户自己的封面不受影响、开关关闭、清理与服务引用保护、容量上限淘汰、维护周期上报。
- 本次开发中被新测试抓到并修掉的真实缺陷：**挂载目录缓存用了不可重入锁**——子类的列举实现
  套在另一层被缓存的列举上时，同一个线程会把自己锁死（表现为远程库扫描直接挂住）。

### 文档 (Docs)
- `docs/performance.md`：「已知瓶颈」六项全部标记完成并各自附实测数字；新增扫描/播放两组
  环境变量；「五、怎么验证」补上三个新脚本。
- 版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 2.17.0。

### 仍开放（都不是「卡住整机」的量级）
- 刮削**命中率**：TMDB 没命中的条目在「已配密钥」的部署里每轮仍会重试（策略如此），
  这类条目多的库会吃掉增量扫描的收益。可以给「试过但没命中」记时间戳、按策略窗口再试（要加列）。
- 清理阶段只在**来源可用**时遍历；再做「目录 → guid 快照」能连那次游标读也省掉。

## [2.16.0] - 2026-09-22

把性能文档「已知瓶颈」里的第 1 项做掉（逐集查剧集/季），并把「验证」本身补齐：
分类关联表多了升级演练测试与 CI 性能护栏，扫描器拆出两个模块。

### 性能 (Performance) — 扫描器不再逐集查剧集/季
- **批次预取剧集与季**：写库循环原来对**每一集**点查两次（先剧集、再季）——即使都走 guid
  唯一索引，十万集也是二十万次往返。现在在批次开头一次 `guid IN (...)` 取回，并按 guid 缓存
  到本次扫描结束（`_ScanContext.series_items` / `season_items`），循环里只在确实没有时才新建。
  存的是对象而不是 id：循环后段还要用它们做「集 → 季 → 剧」的海报回退。
  **可验证**：`smoke_test_scan_budget.py` 用 SQLAlchemy 的 `before_cursor_execute` 事件数 SQL，
  断言「按 guid 的点查 = 0 次」（旧实现同规模下≥ 集数 × 2）；同一条把条目表 SELECT 的上限
  从「文件数的50%」收紧到 12 次。

### 重构 (Refactor) — scanner.py 拆出两个模块（1651 → 1320 行）
- **`backend/emby_server/tmdb.py`**：TMDB 客户端（密钥轮询 + 短 TTL 缓存）。它跟「遍历文件系统」
  是两件事：一个等网络、有配额状态，一个走磁盘、状态多。拆开后客户端可以脱离扫描器单独测。
- **`backend/emby_server/subtitle_match.py`**：外挂字幕的同名判定与语言识别（含发布标签正则），
  本机与远程挂载（115 / WebDAV / AList）共用同一份判定。注意与负责字幕**投递**的
  `subtitles.py` 区分开。
- `scanner.tmdb_client` / `scanner.match_subtitle_names` 等既有引用由 scanner 重新导出，
  调用方（api / portal / 各冒烟脚本）无需改动；`find_external_subtitles` 保留在扫描器里，
  因为它要用**扫描期的目录缓存**。

### 测试 (Tests) — 升级现场与性能护栏
- **新增 `scripts/smoke_test_item_facets_upgrade.py`**（已入 CI）：演练真实老库升级必经过、
  但开发机上很难自然遇到的四种场景：① 未回填完时筛选走降级路径且结果与升级前逐条一致；
  ② 回填**中途被打断**（模拟进程被杀）——异常向上冒、已提交的块保留、水位停在块边界；
  ③ 换个 Session **重启续跑**——补齐、幂等、关联行与条目列逐行一致（无重复/无遗漏/无孤儿）；
  ④ 读请求的**补块预算与冷却**——一次请求按预算分块补完（实测每块 5 条），冷却窗口内的
  并发请求不抢写锁、也不改任何状态。
- **基准新增 `--guard` 断言模式并接入 CI**：判定的是**比值**而非耗时，所以不受 CI 机器快慢影响；
  门禁下限取得很保守（真实差距是阈值的几十倍），只有真改坏了才会红。护栏项目：少见取值筛选
  ≥ 5x、筛选菜单 ≥ 2x、写入钩子开销 ≤ 3x、看护清理 ≤ 2s、每次筛选常数开销 ≤ 50ms、
  5000 取值匹配 ≤ 20ms、回填吞吐 ≥ 500 条/秒、两条路径**条数一致**。
  （已用极小数据量验证过护栏确实会以非 0 退出码结束。）
- `smoke_test_scan_budget.py` 的注释与阈值同步收紧（那条“遗留的逐集查询”已经不存在了）。

## [2.15.1] - 2026-09-22

分类关联表上线后的**代价核实与优化**（这一版不改行为，只改成本与文档）。

### 性能 (Performance) — 按实测结果收成本
- **新增可重复基准 `scripts/benchmark_item_facets.py`**（只在临时库上造数据，不入 CI）：
  固定量出「读省多少、写多花多少」，并对比三种筛选写法。十万条 SQLite 上的结论：
  少见取值筛选 19.9 ms → **0.06 ms（约 300x）**、筛选菜单 153.7 ms → **11.5 ms（13x）**，
  但**常见取值（命中 29% 库）两边持平（21.6 / 21.2 ms）**——索引对「命中三万条」帮不上忙；
  扫描写入每条多 13 µs（1.09x）、看护周期兜底清理 27 ms/600s、每次筛选常数开销 0.8 ms。
  旧口径的代价随库增长（50 万条时列表页约 107 ms），新口径只随命中条数走。
- **修正 2.15.0 里过于乐观的措辞**：原文写「十万级库每次筛选数秒 CPU」——实测 10 万条上
  全表 LIKE 约 20 ms、菜单扫描约 154 ms，「数秒」是 2.12.0 把菜单改成只取列**之前**的量级。
  真实收益的大头是筛选菜单、合成 Id 反查表与少见取值筛选，文档已按实测改写。
- **写入侧省掉一次无谓的 DELETE**：新条目本来就没有关联行，现在只对**分类值真的变过**的
  老条目先删后插（扫描器每批都是新条目，这是热路径上的固定开销），每条写出 15 µs → 13 µs。
- **`ready()` 压成一条 Core 语句**：筛选前要问「关联表能不能代表全库」，原来两次 ORM `query()`
  的固定开销比查询本身还大，现在一次 `SELECT (水位), (MAX(id))`（两个索引查找、不构造实体），
  每次筛选的常数开销 1.07 ms → 0.78 ms。

### 文档 (Docs)
- `docs/performance.md` 新增「实测数字」小节：读/写两头对照表 + 三种筛选写法的对比
  （`IN (子查询)` 最好；`JOIN` 常见取值更慢且多值要额外去重；`EXISTS` 比旧的全表扫描还慢 2x，
  写清楚免得后人「顺手改得更直观」而变慢）。

## [2.15.0] - 2026-09-22

分类筛选不再扫全库：引入 ``emby_items ↔ 分类值`` 关联表（流派 / 工作室 / 标签 / 发行平台），
筛选与取值菜单都改走索引。这是 2.14.0 待办里那条 schema 级改动的落地。

### 性能 (Performance) — 分类筛选走索引
- **新表 `emby_item_facets(item_id, kind, value)`**：`(kind, value, item_id)` 覆盖索引用于筛选，
  `(item_id, kind)` 用于重建与清理，``UNIQUE(item_id, kind, value)`` 防重。
  原先只能写 ``genres ILIKE '%动作%'`` —— **前置通配符用不上任何索引**，客户端每点一次「类型 /
  制作公司」、每次打开筛选菜单都要扫一遍整库（代价与库大小线性相关）。
  现在变成「关联表索引里取出候选 item_id（子查询，覆盖索引扫描）→ 按主键取条目」，
  ``EXPLAIN QUERY PLAN`` 可见 ``SEARCH emby_items USING INTEGER PRIMARY KEY`` +
  ``SEARCH emby_item_facets USING COVERING INDEX idx_item_facet_kind_value``，
  `emby_items` 文本列上不再有 LIKE。
- **语义与升级前逐条一致**：关联表侧的匹配是「全等，或取值里包含该名字（大小写不敏感）」，
  与旧 ``ILIKE '%x%'`` 相同（所以传「Action」照样命中「Action-Adventure」）。
  筛选、合成 Id（点「类型/制作公司」）、虚拟媒体库（按发行平台聚合）、`/Items/Filters`
  的取值菜单共 4 条路径都改到索引上，旧口径只在老库还没回填完时兜底。
- **筛选菜单不再扫全库**：流派 / 标签取值一次覆盖索引拿全；分级与年份改成
  只取单列 ``DISTINCT``（以前是四个列一起读回来物化）。
- **合成 Id 反查表跨请求复用**：以前每个带 ``GenreIds`` / ``StudioIds`` 的列表请求都要
  扫一遍全库拼 ``{合成 Id: 名称}``；现在按关联表的「代际号」缓存（扫描 / 回填 / 删条目都会
  推进代际，因此不会给出过期结果），合成 Id 表本身也从两次索引扫描得到。

### 变更 (Changed) — 关联表的维护（写入侧）
- **挂在 ORM 的 flush 事件上，而不是每个调用点记得调一次**：谁在事务里改/新建/删除条目，
  同一个事务里就把关联行改好（扫描器、图片修复、将来任何新写入路径都覆盖）。
  漏一处不会再静默变脏；要么一起成功，要么一起回滚。
  关联行写入走 Core 层（不会再触发 flush，无递归）；关联表写失败只记日志，不拖垮业务写入。
- **老库自动补齐**：升级上来的库关联表是空的，启动维护按 id 水位分批回填（内存有界，
  每块推进水位并提交，进程被杀不必重来）；筛选路径发现没补完也会按预算（默认 2000 条，
  带 5s 冷却避免并发请求一起抢写锁）补一块，补不完就**退回旧的全表匹配**——结果一样正确，
  只是慢，不会出现「升级后筛选空白」。
- **孤儿行兜底**：批量 Core 删除（扫描清理走的就是这条）绕过 ORM 事件，由周期维护的
  `prune_orphans` 用一条 `NOT IN` SQL 清理；健康检查暴露 ``orphan_count``。
- 版本号：EM `version` / EA `EA_VERSION` / 两个前端 `package.json`（含锁文件）/ 后台顶栏 → 2.15.0。

### 测试 (Tests)
- 新增 `scripts/smoke_test_item_facets.py`（48 项，已接入 CI）：写入侧自动同步（新建 / 多值
  拆分 / 改值不留旧值 / 删条目清关联）、读取侧与旧 LIKE 口径**逐条一致**（含大小写与
  「Action-Adventure」这类包含关系）、合成 Id 与虚拟库平台、筛选菜单取值一致、
  老库补块（筛选路径自动补 + 显式回填幂等）、孤儿行清理、索引证据
  （``EXPLAIN QUERY PLAN`` 走覆盖索引、主查询按主键），以及 3000 条规模下仍走索引且
  结果与 LIKE 口径一致（1000/1000）。
- `scripts/smoke_test_maintenance.py` 补第 8 节：关联行随业务事务建立、批量 Core 删除的孤儿行会被看护周期清掉并上报计数（这两个脚本原本不在 CI 里，靠人工跑）。
- 顺带修掉两个不在 CI 里的测试自身的缺陷（不是本次功能改动引入）：
  `smoke_test_mounts.py` 还在 `asyncio.run()` 调 v2.13.0 已改成同步 def 的 `/Items/{id}/File`（现在按可等待性自适应，端点再改签名也不会静默失效）；
  `smoke_test_media_search.py` 把虚拟媒体库的条目数写死成 1，开发机库文件里有其它用例留下的条目时必然误报
  （改成用同一谓词独立重算 + 断言聚合范围）。

## [2.14.0] - 2026-09-22

2.12.0 那份待办清单里剩下的三项都在这一版落地：并发正确性（优惠券单人限领）、
写放大（播放进度上报）、重复劳动（下载请求的策略判定）。

### 修复 (Fixed) — 并发正确性
- **优惠券单人限领的 TOCTOU**：总限额早就是条件 UPDATE，单人限领却是「先查该人已领几张、
  再插入核销记录」——同一用户的两个请求同时进来都会读到「还没领满」，于是各领一张。
  现在占名额改成条件 ``INSERT ... SELECT``（该人有效核销数 < ``per_user_limit`` 才写入），
  拿到 0 行就是并发下已被自己先一步领满，直接 400；单人名额与总限额两步都在下单事务里，
  任一步失败整笔回滚，不会留下「占了额度却没订单」的半张核销记录。
  并发回归：5 个线程抢同一张 ``per_user_limit=1`` 的券 → 只成功 1 次、核销记录只 1 行、
  `use_count` 只 +1（`scripts/smoke_test_concurrency.py`，CI 里跑）。

### 性能 (Performance) — 播放进度上报的写库节流
- **进度上报不再每报必写库**：客户端每 10s 上报一次（暂停、卡顿的客户端也照报），
  多路并发播放时每个上报都要 UPDATE + commit，SQLite 写锁会排队。现在只有
  「新会话 / 播放结束 / 暂停状态切换 / 拖动（位置跳变 ≥ 30s）」立刻落库，其余上报在
  ``EMBY_PROGRESS_WRITE_SECONDS``（默认 15s）内只写一次；同一次上报里「会话与观看进度
  都没有新信息」时连用户媒体数据的写事务一起跳过（否则等于没省）。
  节流窗口远小于会话回收阈值（``SESSION_STALE_MINUTES``，默认 10 分钟），心跳不会掉队；
  ``EMBY_PROGRESS_WRITE_SECONDS=0`` 回到旧行为。
  回归断言：无新信息的重复上报不写库、拖动/暂停切换立刻落库、被节流的上报仍记下观看进度、
  关掉节流后每报必写（`scripts/smoke_test_emby_sessions.py`，44 项）。

### 性能 (Performance) — 下载请求不再判两遍策略
- **下载策略网关的结论交给路由复用**：`DownloadGuardMiddleware` 已在 ASGI 层判过
  「全局开关 + 该服策略 + 管理员放行」，路由里的 `ensure_download_allowed` 现在直接复用
  （`request.state.download_allowed_by_guard`），同一个下载请求少查 2-3 次库。
  网关没给出结论时（拿不到身份、判定异常、根本没过网关）路由自己照旧判——
  宁可多查一次，不能漏拦。回归断言见 `scripts/smoke_test_paywall.py`。

### 变更 (Changed)
- 版本号：EM `version` / EA `EA_VERSION` / 两个前端 `package.json`（含锁文件）/ 后台顶栏 → 2.14.0。

### 待办 (Next)
- ~~分类筛选的 `ilike '%x%'` 用不上索引~~ → 已在 2.15.0 落地（``emby_item_facets`` 关联表 + 索引）。


## [2.13.0] - 2026-09-22

把 2100+ 行的 `backend/emby_server/api.py` 按关注点拆成四个模块，并借此把**剩下的协议路由**
从事件循环上撤下来。上一版因为「文件太大、编辑工具在 1400 行之后无法可靠匹配」而搁置的部分，
这次先拆文件、再改代码，每一步的改动面都小到可评审、可验证。

### 变更 (Changed) — 模块拆分（对外行为不变）
- `api.py`（1480 行）保留：系统信息 / 认证 / 列表与详情 / 播放核心 / 会话基础工具。
- 新增 `media_routes.py`：图片投递（含带序号的 Backdrop/Thumb 等）、原始文件下载、HLS 的 `hls1` 入口。
- 新增 `compat_routes.py`：播放会话上报（`/Sessions/Playing|Progress|Stopped`）、在线会话列表的旧实现、
  以及 Emby 4.7 协议面补齐端点（筛选值、收藏、相似、演职员、任务、活动日志、库刷新等）。
- 新增 `stream_routes.py`：容器后缀变体流、原始文件端点、字幕投递、HLS 大小写兼容通配（必须最后注册）。
- 四个模块注册到**同一个** `emby_router`（从 `api` 导入同一实例），导入顺序 media → compat → stream
  与拆分前的定义顺序逐条一致——192 条装饰器的方法、路径与顺序已逐一比对。
  因此 `mount_routes` / `image_routes` / `session_routes` 的「按路径 + 端点名替换」逻辑不受影响：
  `image_routes` 改为引用 `media_routes` 的实现，`session_routes` 改为从 `compat_routes` 取上报处理函数，
  并兼容「原处理函数改成同步 def」的情况（`inspect.isawaitable`）。

### 性能 (Performance) — 协议路由不再占用事件循环
- **42 个协议端点由 `async def` 改为同步 `def`**，交给 Starlette 线程池执行：会话上报与能力、
  筛选值 / 流派 / 工作室 / 演员、收藏增删、相似 / 祖先 / 花絮 / 主题曲 / 影评、显示偏好、
  系统端点、计划任务、活动日志、库刷新与扫描触发、字幕投递、原始文件、下载、`hls1` 等。
  它们的同步查询、读盘与远程代理不再串行压在事件循环上（跨机 PostgreSQL 上收益更明显）。
- **图片是媒体库滚动最高频的请求**：`item_image` / `item_image_index` 与两个条件请求包装
  （`item_image_cached` / `item_image_index_cached`）一并同步化，读盘与远程海报代理不再卡事件循环。
- **可转的都转了**：拆分后 `api.py` 只剩 5 个 `async def`（`user_views`、`rate_item`、`playback_info`、
  `video_stream`、`video_hls`），且都是必须异步的——前三个要 `await request.json()`，后两个要
  `await` 异步响应流并被其他模块复用。
- 文档：`docs/performance.md` 新增「协议路由的线程模型」一节，写明模块边界与
  「同步 `def` 优先、只有真的需要 `await` 才写 `async def`」的口径。

### 修复 (Fixed)
- `scripts/smoke_test_ea_split.py`：「EM 分离模式给出 EA 指引」的断言原先假定库里没配过
  「Emby 服务入口」；面板配置优先于环境变量是产品口径，断言改为接受配置值或环境变量，
  在已配置过地址的环境（如本地沙箱库）上不再误报。

### 变更 (Changed)
- 版本号：EM `version` / EA `EA_VERSION` / 两个前端 `package.json`（含锁文件）/ 后台顶栏 → 2.13.0。


## [2.12.0] - 2026-09-22

针对一轮代码评审里**一直还在**的项逐条处理，主线是「不让同步阻塞与并发竞态住在事件循环上」。

### 修复 (Fixed) — 事件循环不再被阻塞
- **协议路由改同步实现，交给 Starlette 线程池**：`api.py` 里 24 个不 ``await`` 任何东西的
  异步路由（`/Users/{uid}/Items`、`Resume`、`Latest`、`Items/Counts`、`Items/Filters`、
  条目详情、`Shows/Seasons`、`Shows/Episodes`、`NextUp`、收藏/已看、`AuthenticateByName`、
  `System/Info` 等）此前把同步 SQLAlchemy 查询压在事件循环上串行执行；现在它们的库查询、
  文件遍历与远程代理都在线程池里跑；同类处理也应用在挂载文件、搜索结果、会话列表/结束、
  节点接口上。**跨机 PostgreSQL 上每查询一次 RTT 就锁一次吞吐的问题随之缓解**。
- **HLS 等待不再阻塞全进程**：`wait_for_file()` 改异步等待（等首片最多 15s、等切片最多 12s
  过去是同步 `time.sleep` 轮询，一个人起播其他人全部排队）；`start_transcode` 里的失效会话
  回收与并发上限保护改到后台线程（原本要 terminate 子进程、等它退出、删目录）。
- **远程代理新增异步版**：`serve_remote_async()` 用 `httpx.AsyncClient`，播放路径上的
  Range 请求不再为「连源站 + 等首字节」把整个事件循环挂住；挂载来源走同步路由进线程池，
  效果相同（凭据不加下发、Range 与透传头口径完全对齐）。
- **bcrypt 不再吃事件循环**：门户注册/登录/改密与管理端登录/改密/重置密码改为同步路由，
  `bcrypt.checkpw`/`hashpw`（单次 100-300ms）在线程池里执行；登录风暴不再让服务假死。
- **筛选菜单不再全表 ORM 物化**：`_filters_payload` 改为「只取需要的列 + ``yield_per`` 分批 +
  TTL 缓存（``EMBY_FILTERS_CACHE_TTL``，默认 300s）」，十万级库不再因此产生数百 MB 内存尖峰；
  扫描后可在进程内调 `invalidate_filters_cache()` 主动失效。
- **随机排序不再全表排序**：`SortBy=Random` 改为「取主键 → 内存抽样 → 按 id 取这一页」，
  不再让数据库把整个结果集物化后 `ORDER BY RANDOM()`。

### 修复 (Fixed) — 正确性与并发
- **后缀 Range 解析错误**：`bytes=-500`（播放器探测 MP4 尾部 moov 时会发）旧实现解析成
  `start=0, end=500`，返回的是文件**开头** 501 字节，部分播放器因此黑屏/播放失败；
  现按 RFC 7233 解析为末 500 字节（越界、`bytes=5-2`、非法值仍 416）。
- **支付履约的读-改-写竞态**：回调重试与管理员补单撞车时，两个事务都读到 `pending` →
  双倍积分 / 双份订阅（跳机器 PostgreSQL 下是实打实的资损）。新增 `_claim_order()`：先做
  条件 `UPDATE ... WHERE status NOT IN ('paid','refunded','closed')` 领取订单，拿到 0 行就跳过发货；
  管理员补单碰到已被别人履约的订单现在返回 **409** 并提示刷新，而不是「成功」两个字。
- **客户端 token 不再每请求提交**：`get_emby_user` 的 `last_used_at` 改为按间隔节流写库
  （``EMBY_TOKEN_TOUCH_SECONDS``，默认 60s，设 0 回到旧行为）；SQLite 补上
  `PRAGMA synchronous=NORMAL`（WAL 下安全），高频写不再每次等 fsync。

### 变更 (Changed)
- **`/metrics` 默认只放行本机/内网**（新增 `backend/metrics_guard.py`）：回环与 RFC1918 正常采集，
  公网请求直接 403 并写告警；需要公网采集时显式 `METRICS_ALLOW_REMOTE=true`。EM / EA 均已接入，
  `docs/operations.md` 的检查项同步更新。
- **ffmpeg 报错不再丢进 DEVNULL**：转码日志写到会话目录里的 `ffmpeg.log`（随会话目录一起清理），
  异常退出时自动把日志尾部提升到服务日志——`deploy-ea.md` 一直让运维在服务日志找转码失败原因，
  现在那里真的有原因。
- 版本号：EM `version` / EA `EA_VERSION` / 两个前端 `package.json`（含锁文件）/ 后台顶栏 → 2.12.0。

### 验证 (Verification)
- 本地全部冒烟通过：`smoke_test_auth`、`smoke_test_admin_v240`、`smoke_test_economy`（19 项）、
  `smoke_test_concurrency`（23/23）、`smoke_test_playback_chain`（20/20）、`smoke_test_emby_sessions`（37 项）、
  `smoke_test_media_search`、`smoke_test_maintenance`、`smoke_test_security_hardening`（43/43）。
- 定向检查：后缀/常规/越界/非法 Range 四类逐一验证；`/metrics` 对 `127.0.0.1`/内网 200、
  公网 403；两个服务均可导入装配，`api.py` 路由由「79 异步 / 0 同步」变为「55 异步 / 24 同步」。

### 待办 (Next)
- **async ORM 这件事还只做了一半**：`api.py` 里仍有 55 个异步路由（含字幕、图片、下载、
  收藏切换与后半段接口），它们还在事件循环上跑同步查询；该文件已超 2000 行，需先拆分再逐个处理。
- 优惠券**单人限领 TOCTOU**（总限额已是条件更新，单人限领的「查-插」仍可并发绕过，P2）。
- 分类筛选的 `ilike '%x%'` 用不上索引；下载类请求「中间件判一遍 + 路由再判一遍」可以合并。

## [2.11.1] - 2026-09-22

把 2.11.0 里因 `emby_server/api.py` 太长而暂时搬不动的两件小事做完——做法沿用仓库既有的
「在 `include_router` 之前精确替换路由」（与 `mount_routes.py` / `session_routes.py` 同一套路），
业务实现仍然只有一份，没有复制粘贴。

### 新增 (Added)
- **图片端点接上条件请求（304）**：新增 `backend/emby_server/image_routes.py`。图片实现
  （季/集回退、远程图代理与 SSRF 防护、缺图排队修复、文件丢失时干净 404）仍在 `api.py`，
  新模块只在其外层处理 `If-None-Match` / `If-Modified-Since`：客户端缓存仍有效时返回
  **304（空响应体）**，不再把同一张海报重传一遍；没有校验器的远程代理图与错误响应原样返回。
  媒体库页面上图片是请求量最大的资源，滚动、返回、切页省下的就是这部分带宽。

### 变更 (Changed)
- **`/Sessions` 在线会话列表批量取关联数据**：`session_routes.py` 的实现此前逐条查条目与用户
  （N 台设备同时播放就是 2N 次查询），现在一次性取回再组装，响应字段与口径不变。
- 版本号：EM `version` / EA `EA_VERSION` / 两个前端 `package.json`（含锁文件）/ 后台顶栏 → 2.11.1。

### 验证 (Verification)
- `python3 -m py_compile` 改动文件通过；两个服务导入后确认 4 条图片路由已被替换为带条件请求的实现。
- 条件请求按真实响应检查：`If-None-Match`（含 `W/` 前缀）与 `If-Modified-Since` 命中返回 304，
  不命中或没有请求头时仍是 200；非文件响应（远程代理图 / 404）不受影响。
- 冒烟：`smoke_test_emby_sessions.py`（37 项）、`smoke_test_media_search.py`、`smoke_test_emby.py`、
  `smoke_test_playback_chain.py`（20/20）全绿。

## [2.11.0] - 2026-09-22

这一版是**后端（EM 面板 + EA 网关）的性能与稳定性**一轮：把列表接口里最贵的 N+1 查询
消掉、把浏览媒体库时请求量最大的图片变成可缓存可复用、把大块二进制从 GZip 里摘出去，
并给 HLS 转码加上并发上限保护。目标是同样一套硬件下：媒体库翻页更快、并发播放更稳。

### 变更 (Changed) — 性能
- **列表接口消除 N+1 查询**：`_item_dto` 此前为每个条目单独查用户媒体数据（进度 / 收藏 / 已看）
  并统计子项数量，一页 100 条就是 200+ 次往返。现在 `_prefetch_list_data()` 在渲染前
  一次性把这一页的媒体数据、系列归属与子项计数全部取回（12 处列表端点——
  `/Users/{uid}/Items`、`Resume`、`Latest`、`Shows/Seasons`、`Shows/Episodes`、`NextUp`、
  收藏 / 筛选 / 分组等——统一接入），分页大小不再影响查询次数。
- **图片响应可缓存**：`serve_image()` 现在带上 `ETag` / `Last-Modified` /
  `Cache-Control: public, max-age=86400` / `Accept-Ranges`。浏览媒体库时同一张海报会被反复请求
  （滚动、返回、切页），旧实现每次都整文件重发；现在客户端在 24h 有效期内直接命中本地缓存。
- **大块二进制不再走 GZip**：EM 与 EA 的 `GZipMiddleware` 排除 `application/octet-stream`
  与 `application/zip`（Starlette 默认只排除 `video/*`、`image/*` 等）。远程挂载代理转发的
  媒体文件此前会按 level 9 压缩，白白吃满 CPU 且对已压缩容器毫无收益。
- **前端静态产物强缓存**：`/assets/*` 与 `/admin/assets/*`（Vite 产物文件名带内容哈希）
  返回 `Cache-Control: public, max-age=31536000, immutable`；SPA 入口 HTML 改为 `no-cache`，
  发新版本后用户刷新即可拿到新构建，不再出现「JS 已换、HTML 还是旧的」错配。

### 新增 (Added) — 稳定性
- **HLS 转码并发上限保护**（`emby_server/streaming.py`）：`enforce_transcode_capacity()` 在每次新建
  转码会话前执行，默认上限为 CPU 核数（`EMBY_MAX_TRANSCODES` 可显式指定）。超过上限时
  **只回收「输出目录闲置超时」（`EMBY_TRANSCODE_IDLE`，默认 120s）的会话**——
  即切清晰度 / 拖进度后客户端重新拉起所留下的、不会上报 `Stopped` 的残留会话；
  若当前都在活跃播放，则宁可短暂超限并写告警，**不会为了给新请求腾位置而掐断正在播放的会话**。

### 验证 (Verification)
- `python3 -m py_compile` 覆盖全部改动文件（`backend/emby_server/api.py`、
  `backend/emby_server/streaming.py`、`backend/main.py`、`emby_api/main.py`）通过；
  两个服务（EM / EA）都能正常导入并完成路由与中间件装配，版本均为 2.11.0。
- **转码上限保护**按真实逻辑跑了四组行为检查：未达上限时不动任何会话；达上限且有闲置会话时
  只回收最久闲置的那一路（含临时分片目录）；超限但全在活跃播放时谁也不断（只告警）；
  多路闲置时按闲置时间从旧到新回收。
- **静态缓存**用 `TestClient` 实测：`/assets/css/*.css` 返回 `max-age=31536000, immutable`、
  SPA 入口 `/` 返回 `no-cache`、`/api/*` 仍是 `no-store`。
- **GZip 排除**已确认在两个 app 上生效（`exclude_content_types` 含 `application/octet-stream`）。
- **图片响应头**实测：`ETag` / `Last-Modified` / `Cache-Control: public, max-age=86400` /
  `Accept-Ranges` / 正确 `content-type` 均按预期下发；文件缺失仍是干净的 404。

### 待办 (Next)
- 图片条件请求（304）与 `/Sessions` 列表批量取数已在 **2.11.1** 落地。
- 播放进度上报（`/Sessions/Playing/Progress`）的写库节流：客户端每 10s 一报，多个并发播放时
  SQLite 写锁会有竞争，计划按「进度实质变化才落库」收敛。

## [2.10.6] - 2026-09-22

v2.6.27 把设计令牌收归 Aurora 单一事实来源后,用户端仍有十几个页面各自写死一批
`rgba(34, 211, 238, …)` / `rgba(7, 11, 18, …)`。这一版把剩下的写死颜色全部换成 Aurora 令牌,
并补齐页面里缺的中间档令牌,让「同一个语义只有一个颜色」在全站真正成立。

### 变更 (Changed) — 用户端硬编码颜色对齐 Aurora 令牌
- **新增页面级缺口令牌**(`aurora.css`):`--au-primary-mid`(hover 底色,比 soft 明显、比 border 温柔)、
  `--au-on-primary` / `--au-on-warning`(亮色块上的深墨色文字)、语义色 `*-border` 档、
  叠加层四档(`--au-overlay-soft/mid/strong/menu`)、`--au-scrim`、`--au-grid-line`、
  图片上的中性叠加(`--au-on-image*`)、进度槽 `--au-track`、阴影色 `--au-shadow-color/text`。
- **页面全部换用令牌**:`HistoryView` / `LibraryHomeView` / `HomeView` / `WalletView` / `TicketsView`
  / `LoginView` / `CheckinView` / `InviteView` / `MessagesView` / `ProfileView` / `RequestView`
  / `FavoritesView` / `ItemDetailView` / `LibraryView` / `WatchView`,以及
  `AppHeader` / `Toast` / `MediaCard` 组件——遮罩、边框、徽标、渐变按钮文字、
  下拉浮层背景不再各自为政;页面里的重复 `@keyframes spin` 收敛到全局 `au-spin`。
- **刻意保留的写死色**:播放器容器纯黑底(与画面本身一致,混入主题蓝调反而显出「框」)、
  横向滑动的 mask 渐变(`#000` 表示遮罩不透明度,不是视觉颜色)——均已注释说明。
- **死代码组件对齐**:`components/ui/` 下无人引用的 Button / Chip / SegmentedControl
  里残留的旧 Emerald 翠绿(`rgba(16, 185, 129, …)`,主色换青之前的遗留)同步换成 Aurora 令牌,
  避免将来启用时又冒出一套旧色。
- **版本号**全站统一升至 2.10.6(两个前端 `package.json`、EM `version`、EA `EA_VERSION`、
  后台 `APP_VERSION`)。

### 验证 (Verification)
- 两个前端 `vue-tsc` 0 错误、`vite build` 通过;设计令牌契约检查
  (`scripts/check_frontend_tokens.py`)通过。
- 全站扫描确认:页面与组件里不再有主题色 / 表面色的硬编码,只剩上述两处刻意保留。

## [2.10.5] - 2026-09-22

v2.10.4 修完用户侧后，这一版把**后台管理员**侧同样过了一遍：补上 2.10.4 改动的后台收尾，
并修掉一个让控制台日志失真的一类写法。

### 修复 (Fixed)
- **取消对话框不再当报错抛**：组件里普遍使用 `await ElMessageBox.confirm(...)` / `prompt(...)`，
  而用户点「取消」「关闭」时 Element Plus 会以 `'cancel'` / `'close'` **拒绝**这个 Promise。
  此前 13 个页面全部没接住它，于是管理员每取消一次对话框，控制台就多一条
  `Unhandled error during execution of native event handler`——真正的异常全淹在里面。
  现于 `main.ts` 统一把这两个值咽掉（其余异常照旧打印）。
- **工单关闭加二次确认**（`admin_frontend/views/Tickets.vue`）：关闭后用户不能再回复，
  此前表格里那颗红色「关闭」一点即生效，误触只能重新开单。

### 变更 (Changed) — 后台管理员体验
- **求片管理补上「已撤回」筛选**：v2.10.4 把「用户撤回」从删行改成落 `withdrawn` 状态后，
  这些条目默认不出现在待办清单里（管理员不用白点），但后台当时也**没有任何入口能看到它们**。
  现在状态下拉多了「已撤回」一项（带对应的徽标与文案），审计时查得到、待办里看不到。
- **工单抽屉有了加载态**：抽屉先开、消息后到，此前拉取期间会先闪一下空对话；现在显示骨架，
  加载完再换成内容，消息为空时明确写「还没有对话内容」。

### 测试 (Tests)
- `scripts/smoke_test_user_v250.py` 再加 2 项断言：后台求片清单默认不含已撤回条目、
  显式传 `status_filter=withdrawn` 时能查到（审计路径可用）。
- 两个前端 `vue-tsc` 0 错误、`vite build` 通过；路由与设计令牌契约检查通过。

## [2.10.4] - 2026-09-22

这一版顺着「体验 + 审查代码 / 底层逻辑 / 功能逻辑」把用户侧三个中心（消息 / 求片 / 工单）
从页面串到后端 handler 看了一遍，把不一致的地方补齐，并修掉一个能让额度失效的逻辑漏洞。

### 修复 (Fixed) — 求片额度可被「撤回」刷新（功能逻辑）
- **撤回求片不再删行，而是改状态 `withdrawn`**。此前 `DELETE /api/user/media-seek/{id}`
  直接删库，而每日额度按「今天还存在的行数」统计——于是
  **提交 → 撤回 → 再提交** 可以无限循环：额度永远是 0/5，而每一次提交都会给全体管理员推一条
  「📥 新的求片请求」站内消息。这既让 `media_seek_daily_limit` 形同虚设，也是一个通知刷屏器。
  现在额度按**今天的提交总数**算（含已撤回），撤回不退还额度；撤回的条目不再出现在用户的
  求片列表里（用户看不到自己已撤销的东西才符合直觉），但后台仍能看到这条提交记录。
- **后台求片清单默认不再列出已撤回的条目**（`GET /api/admin/media-seek`）：待办清单里混进
  用户自己撤掉的请求只会让管理员白点一次。需要审计时显式传 `status_filter=withdrawn`。
- **站内消息 `limit` 收口到 1..200**（`GET /api/user/messages`）：这是用户可控的查询参数，
  不夹一下就能一次把整个收件箱拉走。

### 变更 (Changed) — 用户侧体验
- **工单中心**：创建 / 回复 / 关闭全部改为页内 toast，不再用浏览器的 `alert()`（后者会打断操作、
  在移动端还很粗糙）；后端返回的业务原因（如「工单已关闭，无法回复」）直接透出，不再吞成一句
  「请稍后重试」。头部新增「刷新」按钮（只转图标不闪骨架），列表与对话内容改为骨架屏；
  对话加载 / 发送后自动滚到最新一条，客服侧带署名；标题或描述为空时提交按钮直接置灰。
- **消息中心**：点开一条未读即标为已读（列表页不再为此弹一次 toast），刷新时列表自身进入禁用态，
  避免刷新中途点到别的条目。
- **求片中心**：提交成功后表单保持打开（能看到成功反馈与最新额度），页面在数据到位前显示「加载中」。

### 测试 (Tests)
- `scripts/smoke_test_user_v250.py` 新增 4 项断言：撤回不退额度 → 429、额度按今天的提交数算、
  已撤回的条目不在用户列表里、撤回是改状态而非删行。
- 回归：`smoke_test_user_v250` / `admin_v240` / `servers` / `realms` / `ops_v260` / `coupons` /
  `economy` 全部通过；两个前端 `vue-tsc` 0 错误、`vite build` 通过，路由与设计令牌契约检查通过。

## [2.10.3] - 2026-09-22

v2.10.2 把「看起来重复」修完后，还剩一处**入口重叠**：首页底部的消息卡与顶栏铃铛预览的
是同一批未读（卡片列 2 条、铃铛列 3 条 + 置顶公告）。按「一个语义一个入口」，收成一处。

### 变更 (Changed) — 站内消息只由顶栏铃铛承担
- **首页底部那张「消息中心」卡去掉**（`HomeView`）：它的信息（未读条数 + 最新几条）与
  顶栏带角标的音铃完全重叠，而音铃是**全站可见**的那个——角标随时看得见、点开有预览、
  一脚落到消息中心。首页不再为同一件事再占一条高度。
- **顺带少一组首页请求**：首页不再拉未读计数、公告列表与 50 条未读，只留内容、经济与订阅
  三路数据；未读计数本来就是顶栏在轮询（`/api/user/messages/unread-count`）。
- **清掉随之作废的代码**：首页的消息卡模板、同名合并逻辑、相对时间函数与 `.inbox-*` 样式；
  速览条的 `alert` 格子态（曾用于「这一格有未读」）与 `.cell-dot` 呼吸点一并移除——
  未读提示已经只有一个地方发（顶栏角标）。
- 消息中心本身不变：完整列表、分类、已读标记、公告分类都在 `/messages`。

### 测试 (Tests)
- 前端 `vue-tsc` 0 错误、`vite build` 通过；路由与设计令牌契约检查通过。
- 后端未改动，既有冒烟测试不受影响。

## [2.10.2] - 2026-09-22

用户反馈：「首页底下的站内短信是不是重复了？」。查了库里的真实数据——
**没有一条真重复**（同一收件人 + 同一类型 + 同一标题 + 同一内容的行数为 0），
看起来重复是预览的展示口径问题，两处都修了。

### 修复 (Fixed) — 消息预览不再「看起来重复」
- **同名未读合并成一条并标条数**：一个管理员账号可以有 26 条未读「📥 新的求片请求」
  （内容各不相同：`usr605951 请求《额度片1605951》`），而首页消息卡与铃铛预览只显示标题，
  两行并排就是一模一样的两条。现在合并成 `📥 新的求片请求 ×26`——预览回答的是
  「有什么在等我」，不是「一共收到几条」；每条通知仍完整地留在消息中心。
  为避免报一个偏小的数，预览一次拉 50 条未读：窗口被占满时最旧那一组标成 `×50+`。
- **公告不再以两种身份各列一遍**：发布公告会给每个用户广播一条站内信
  （`📢 标题`，带已读状态、`related_id` 指回公告），而公告本身又在同一处被列一遍
  （置顶公告）——同一条公告写了两遍。现在凡是已经作为未读站内信在列的那条公告，
  不再重复补列公告行；公告只补那些没被站内信覆盖的（如已读的置顶公告）。
- 两处口径一致：首页「账号与支持」的消息卡与顶栏铃铛预览。

### 测试 (Tests)
- 前端 `vue-tsc` 0 错误、`vite build` 通过；路由与设计令牌契约检查通过。
- 后端接口未改动（`related_id` 本来就在 `/api/user/messages` 里下发），既有冒烟测试不受影响。

## [2.10.1] - 2026-09-22

这一版是「一个语义只留一个入口」的收尾：钱包页不再有两个兑换入口，顶栏不再有两个搜索入口。

### 修复 (Fixed)
- **钱包页的两个兑换入口**（用户反馈「很难看、很割裂」）：v2.10.0 把「优惠码」做成了分页上
  独立的一条输入框，而余额卡里已经有「卡码 · 兑换码」面板——同一页两个「输入码 → 应用」的
  框，用户还得先猜手里那张码该填哪边。现在只剩一个入口：**「卡码 · 兑换码 · 优惠券」共用
  同一个输入框与按钮**，由 `codes.preview_code` 判定来源（新增 `kind=coupon` 分支）：
  卡码照旧先预检再确认开通、兑换码照旧一步核销、优惠券按当前商品试算；已应用的券与
  省钱额就挂在同一个面板里（带「清除」），折后价仍留在商品行内。
- **券只适用于另一类商品时不让用户猜**：本页全部用不了就换另一类试算，能用就把用户
  带到有折后价的那一页，而不是只回一句「该券不适用于此商品」。
- **兑换码的指引文案**：预检返回的「这是兑换码，请在「钱包 → 兑换码」中核销」指向的是
  早已收掉的入口，改成「这是兑换码，点「使用」直接核销」。
- **首页会员卡进度条**：旧公式按「剩余天数 / (剩余天数 + 30)」画图，与真实周期无关——
  刚买的 30 天套餐会显示成已过 50%。现在按订阅自己的 `start_date → end_date` 算，
  日期不全就不画那一条。

### 变更 (Changed)
- **搜索升为顶栏一级入口**（首页 / 媒体库 / 搜索 / 我的）：此前搜索只是右上角一个放大镜
  图标，头像菜单里又写了一条同样的「搜索片名」，窄屏更只有一个图标可点。同一件事只留
  一个入口，图标与菜单里的重复条目一并撤掉。
- **首页「账号与支持」区宽屏并排两列**（消息卡 + 连接播放器），不再一前一后各占一条高度；
  窄屏回落成单列。

### 测试 (Tests)
- `scripts/smoke_test_coupons.py` 补 8 项断言（共 109 项）：优惠券 / 兑换码 / 卡码 / 不存在的码
  都能被统一入口认出来、预检不核销（券的占用数一点没动）、停用的券在预检里就说清原因、
  兑换码的指引不再指向已经收掉的旧入口。

## [2.10.0] - 2026-09-22

这一版做两件「让付费链路真正闭环、让导航不再虚胖」的事。

### 新增 (Added) — 优惠券
- **付费时抵扣的能力此前完全不存在**：站点只有兑换码（不花钱直接拿东西），没有优惠券
  （付费时打折）。两者分工在 `backend/coupons.py` 开头写清：优惠券只影响下单金额，
  履约、退款与财务流水的口径不变。
- **额度是预订制的**（这是本模块最关键的一条）：下单就占额度（`reserved`），支付成功转
  `consumed`，订单被关闭/退款转 `released` 并把额度还回去。只在付款成功时计数会让一串
  未支付订单同时绕过限额；而关单/退款不还额度又会让码被白白烧掉。
- **超发防得住**：总额度用条件 UPDATE 抢占（`use_count < max_uses` 才 +1），
  每人限用同样按「占用中 + 已消费」计算，并发下单也超不了上限。
- **两种折扣**：`percent`（实付百分比，90 = 九折）× 可配封顶减免，或 `fixed`（直减金额）；
  可选门槛（满多少可用）与归属服（多服运营下避免 A 服的券在 B 服用）。
  金额一律 `Decimal`、保留两位——钱不能落进浮点数。
- **预览与实付同口径**：用户端试算（`POST /api/user/economy/payment/coupon/quote`）与真正
  下单共用 `coupons.quote`，不会出现「预览 8 折、实付全价」。下单接口新增 `coupon_code`。
- **订单金额 = 实付**：订单上快照原价 / 优惠金额 / 核销记录（新表 `coupon_usages`，
  订单表新增 `list_price`/`discount_amount`/`coupon_usage_id`），对账、邀请返利比例、
  退款都以用户真付的钱为准；套餐改价后历史订单仍能解释。
- **超时未支付自动收尾**：预订不能被一张永远不付款的订单白占着——默认 24 小时后
  自动关单并退回额度（`coupon_reserve_hours` 可调，0 = 不自动清理）；已支付却还挂着
  预订的历史脏数据补记为已用。跑在既有后台线程里，单独批量、不抢前台锁。
- **面板**（`backend/api/coupons_admin.py` + 「运营 · 优惠券」页）：建券（可批量随机码，
  去掉易看错的 0/O/1/I）、改券、启用/停用、核销记录（预订/已用/已释放三态 + 金额快照）、
  总开关与预订时限；**总次数不能改到已占用之下**（否则立即超发），
  **有核销记录的券只能停用不能删**（历史订单要能解释优惠来源）；全部写审计。
- **用户端**：钱包页新增优惠码区（只对当前分页的商品试算，行内直接显示原价删除线与折后价），
  订单列表带优惠码与已减金额；关单或退款后优惠次数自动退回。

### 变更 (Changed) — 导航收敛（方案 A）
- **收藏与观看记录从一级入口降级为媒体库内的分段**（`/media?tab=favorites|history`）：
  它们本质是媒体库的两种视图，却占掉主导航 3/5 个位置；顶栏收成 **首页 / 媒体库 / 我的**
  三项。旧地址 `/favorites`、`/history` 重定向过来，书签、外部链接与站内旧链接不失效。
- **「正在播放」从观看记录页移到个人中心**：它讲的是**控制**（哪台设备在放、能不能停），
  不是「我看过什么」；观看记录页只留历史，并给出一行指向个人中心的提示条。
- **头像菜单补两个直达**：我的订单（`/wallet?tab=orders`）与积分流水（`/wallet?tab=log`）
  原本要先进钱包再点分页，现在一步到位（主导航仍只留三项，不把长尾塞回顶栏）。

### 测试 (Tests)
- 新增 `scripts/smoke_test_coupons.py`（101 项断言）：折扣与封顶口径、门槛/适用范围/归属服、
  过期与停用、预订→已消费→已释放的全生命周期、关单与退款释放额度（幂等不重复归还）、
  每人限用与总额度抢占、预览与实付一致、订单金额=实付、支付回调履约与重放幂等、
  超时预订自动收尾（关单/补记/不误伤未到期）、建券改券删券与审计、开关与鉴权。已接入 CI。

## [2.9.0] - 2026-09-21

上一版补上了「到期前有人提醒」，这一版补上「出了错能收场」：
订单此前**只能「标记已支付」**——不能关、也不能退。线下多转了一笔、用户点错套餐、
支付回调把它搞重复了，后台一点办法都没有，只能去改数据库。

### 新增 (Added)
- **关单**：未支付订单可作废（`pending → closed`）。不碰权益，也不打扰用户（他并没付钱），
  只留审计；已支付订单必须走退款，两种误操作都会被拒。接口 `POST /api/admin/economy/orders/{id}/close`。
- **退款**：`POST /api/admin/economy/orders/{id}/refund`，默认**按账本精确回滚**：
  - 充值订单：按 `PointsLog.ref_id = recharge:{order_id}` 扣回**当时实际发放**的积分
    （不是按订单金额重算），同时撤回邀请人那笔返利（`rebate:{order_id}`）——
    只退买家不退返利，等于站点为一次退款付两遍钱；
  - 订阅订单：按履约时记下的 `subscription_id` / `days_granted` 回滚天数；
    回滚后已过期则置 `cancelled`（真实撤销，与自然到期不是一回事），
    用户还有别的来源的天数时只减这一笔、保持生效中；
  - 三个开关：`revoke_entitlement`（不回滚权益 = 客服补偿场景）、
    `allow_negative`（余额不够时默认**拒绝并回报余额**，显式允许才扣成负数）、
    `reverse_rebate`（是否同时撤回返利）。
- **退过的订单不能再被履约**：支付回调可能晚到或重放（网关重试、关单后又收到回调），
  只判 `status != "paid"` 会把退过的订单再发一次货；现在 `refunded`/`closed` 一律不履约。
- **留痕**：两个订单表新增 `closed_at` / `refunded_at` / `refund_reason`
  （订阅订单另有 `subscription_id` / `days_granted`），退款原因写入操作审计；
  用户收到退款站内信、用户端订单列表能看到状态与原因。
- **面板**：订单管理页支持关闭/退款（退款对话框含原因与三个开关），
  筛选可选已退款/已关闭，并新增「最近的退款 / 关单」列表（带原因与用户）——
  订单表只有状态标签，说不清「为什么退的」。接口 `GET /api/admin/economy/refunds`。

### 测试 (Tests)
- 新增 `scripts/smoke_test_refunds.py`（56 项断言）：关单只对未支付单、重复关单被拒、
  充值退款扣回实发积分（而非重算）+ 撤回返利、余额不足默认拒绝且回报余额、
  允许负余额后可退、回调重放不会二次发货、订阅按订单回滚 30 天并在过期时置 `cancelled`、
  有剩余天数时保持生效中、可只退款不回滚权益、记录列表与审计、用户端可见性、鉴权。
  已接入 CI。

## [2.8.0] - 2026-09-21

这一版补上付费侧最直接的一项缺失：**会员到期前没有任何提醒**。
`AdminEvent.SUBSCRIPTION_EXPIRED` 这个事件类型早就定义在 `notifications.py` 里，
却从来没有一个发送点；续期只能指望用户自己想起来——对会员制站点来说，
这是转化率最高、也最容易白丢的一次触达。

### 新增 (Added)
- **到期续费提醒**（`backend/reminders.py`）：会员到期前按 `expiry_reminder_days`
  （默认 **7 / 3 / 1 天**）自动发站内信 + WebSocket 实时推送，到期那一刻再发一次「已到期」。
  正文写明套餐、归属服、到期日期与续费入口（取面板 `site_url`）。
- **一条订阅一档只发一次**：新表 `subscription_reminders` 带 `(subscription_id, kind)`
  唯一约束，任务每小时跑一轮也不会重复打扰；档位会**推进**——7 天档发过之后，
  掉到 3 天档会再提醒一次（取最贴近的那一档，不会一直沉默到过期）。
- **口径可配**：`expiry_reminder_enabled`（开关，缺省开启）、`expiry_reminder_days`
  （自定义档位，如 `14,2`）；写错时回落默认 7/3/1，而不是让后台任务整体失效。
- **面板可看可跑**：订阅管理页新增「到期续费提醒」卡片——开关、档位、待发（临期/已到期）、
  累计已发、检查间隔与「最近发送」明细；支持**预览**（`dry_run`，不落库不发消息）与
  **立即检查并发送**。接口：`GET/PUT /api/admin/economy/expiry-reminders[/settings]`、
  `POST /api/admin/economy/expiry-reminders/run`（管理员 JWT，普通用户 403）。
- **用户端临期提示**：首页会员卡、个人中心「我的订阅」、钱包套餐页在剩余 ≤ 7 天时
  换成警示色并给出续费引导（阈值 `EXPIRY_WARN_DAYS` 与后台提醒同一口径）。

### 说明 (Notes)
- **不翻 `UserSubscription.status`**：会员状态仍以 `end_date` 现算（与 2.7.0 之前一致），
  到期只发通知、不改写历史记录，`status` 的「曾经买过」语义得以保留。
- 调度只在 **EM（面板）** 启动（`backend/main.py`），EA 是出流单元，
  不自己发通知，多台 EA 不会重复打扰用户；周期 `REMINDER_INTERVAL`（默认 3600 秒）。

### 测试 (Tests)
- 新增 `scripts/smoke_test_reminders.py`（50 项断言）：窗口内才提醒（+10 天不打扰）、
  dry_run 不落库、真跑一轮落站内信、**第二轮 0 发送（幂等）**、唯一约束拦重复、
  档位推进补发 3 天档、开关关闭后一条不发、自定义档位（14,2）与写错回落、
  面板口径与鉴权（未登录 401 / 普通用户 403）、提醒确实进了用户消息中心与未读计数。
  已接入 CI。

## [2.7.0] - 2026-09-21

这一版补上「一个面板同时运营两种服」：付费服（卖会员）与**公益服**（免费开放）。
此前整套系统只有一个假设——「想看就得有会员」，于是公益服只能靠「全局关掉付费墙」
来近似，代价是付费服也一起免费了。现在接入方式是**服自己的属性**。

### 新增 (Added)
- **接入方式按服配置**（`server_realms.access_mode`）：`paid`（付费服，缺省）/ `free`（公益服）。
  面板「服管理」新建/编辑里直接选，卡片上带「公益服 / 付费服」徽标与下载口径；
  概览页与服清单汇总给出公益服数量（`free_realms` / `paid_realms`）。
- **公益服免费开放**：`backend/subscriptions.py` 的 `can_play` / `ensure_playback_allowed`
  按服判定——公益服不需要订阅，网页播放器与第三方客户端（Infuse / Forward…）都能直接看；
  用户端 `me` 的 `subscription_required` 在该服恒为 false，`is_free_realm` / `realm_access_note`
  一并下发。
- **公益规则文案与资源保护**：每服可写 `access_note`（用户端首页、个人中心、详情页展示，
  留空给缺省文案），下载策略 `allow_download` 三态（跟随全局 / 允许 / 禁止）——
  公益服在「跟随全局」时**默认禁止下载**（提示「公益服只提供在线观看，不提供下载」），
  付费服仍跟随站点开关，行为与升级前一致。
- **用户端按接入方式换一套话术**：首页会员卡在公益服变成「公益服 · 免费开放」说明卡（不再推销会员），
  个人中心展示接入方式与规则、钱包的套餐页换成公益说明（不推套餐）、详情页把付费墙位置换成公益说明；
  「我的服」里公益服没订阅也会下发（免费开放是这个服对用户的承诺）。
- **升级兼容**：老库自动补列并回写 `access_mode='paid'`、`allow_download=NULL`，
  没有任何服的文件仍是付费服。

### 测试 (Tests)
- 新增 `scripts/smoke_test_free_realm.py`（44 项断言）：默认服=付费服、公益服免费开放、
  切回付费服付费墙立刻恢复（按服判定不是全局关墙）、公益服默认禁下载与可覆盖、
  账号卡/套餐接口口径、面板汇总与概览的接入方式。已接入 CI。
- 多服运营、播放入口链路、升级路径、经济系统、安全加固、会话、挂载体检、长期运行稳定性
  等现有冒烟测试全部通过；两前端 type-check、路由与设计令牌契约检查、构建通过。

## [2.6.30] - 2026-09-21

这一版收掉导航的最后一条重复（底部导航坞），并把首页的排序改回「门户到底干什么」。

### 变更 (Changed)
- **底部导航坞（AppDock）移除，全站只剩顶栏一套导航**：≥900px 主导航横向铺开，
  ≤900px 顶栏变两行——第一行品牌与账号操作，第二行是**可横向滑动的主导航选项卡**
  （左右渐隐提示可滑、切换页面后当前项自动滑到中间、锚点偏移按两行算）。
  同一批入口不再在窄屏换一套形态、再占掉页面底部一条。
- **首页排序改成「门户做什么」**：客户端（Infuse / Forward…）已经做得很好的事往后放，
  门户独有的价值往前放——「最近入库」（追新）提到第一条，「继续观看」降为后一条；
  会员状态只在一屏里出现一次（首屏会员卡），账号速览条从四格（含会员）收敛为
  三格（积分 / 签到 / 邀请）；新增一条求片提示（库里没有的内容，门户能承接）；
  收藏 / 观看记录 / 个人中心的重复链接从首页撑掉，交给顶栏导航。
- 清掉导航坞时代的底部留白：首页窄屏的 7rem 底距与 `body.has-dock` 那一条都删除。

### 测试 (Tests)
- 路由与设计令牌契约检查、两前端 type-check 与构建继续通过。

## [2.6.29] - 2026-09-21

这一版只做两件事：把「导航」收成一份定义，把「个人中心」收成账号页。
用户端此前在桌面顶栏、移动端 ☰ 抽屉、移动端底部导航坞里各写了一份导航，
条目与顺序都不同（同一批链接在同一屏出现两遍）；个人中心则被当成功能地图，铺了一地磁贴。

### 修复 (Fixed)
- **移动端同一批链接出现两遍**：顶栏的 ☰ 抽屉与底部导航坞是两套并行导航
  （抽屉里还有分组标题与邀请卡），桌面顶栏又是第三种组合。现在顶栏不再有第二个抽屉，
  低频入口统一进头像菜单（全断点一致）。
- **头像菜单里的「管理后台」会落到 404**：它是同源的另一个前端（`/admin/`），
  却写成了 `RouterLink`——用户端路由里没有这条 path，点击会被 catch-all 兜底页接走。
  现在改为浏览器跳转（与个人中心里那处入口口径一致，登录态仍由后台接管）。
- **登录页 / 全屏播放页底部永远空一条**：`body` 在 ≤900px 无条件加 66px 底距，
  没有导航坞的页面也照加。现在按登录态挂 `body.has-dock`，只有导航坞真的在页面上才留白。

### 变更 (Changed)
- **导航收敛成一份定义**（`src/config/navigation.ts`）：`primaryNav` 五个高频目的地
  （首页 / 媒体库 / 收藏 / 观看记录 / 我的）在桌面渲染成顶栏、在移动端渲染成底部坞——
  同名、同序、同图标、同一套激活态，是「一个导航的两种形态」而不是两个导航；
  钱包 / 签到 / 邀请 / 求片 / 工单 / 消息统一收进顶栏头像菜单。
  底部坞中心那个凸起的「签到」圆盘也去掉了：导航条里多一个发光圆盘，主次反而说不清。
  改导航只改这一处，不会再出现「新页面只加进了一处」的漏入口。
- **个人中心重设为账号页**：顶部是身份卡（首字母头像 / 用户名 / VIP / 会员剩余天数 /
  邮箱 / 注册时间 + 累计观看 / 播放次数 / 看过影片三个数字），下面是两栏账号卡
  （Emby 账号 · 我的订阅 · 我的设备 · 安全设置）。功能磁贴与重复的「最近观看」列表
  全部下线——进个人中心是看账号，不是找功能。

### 测试 (Tests)
- `scripts/check_frontend_routes.py` 与 `scripts/check_frontend_tokens.py` 继续通过：
  本轮新增的导航配置、样式与跳转都在门禁覆盖范围内。

## [2.6.28] - 2026-09-21

这一版收拾用户端「功能都在，但用户找不到 / 用不上」的问题：入口分散、
页面宽度不一致、注册表单里少了两个字段，另有一处容器类从来没有被定义过。

### 修复 (Fixed)
- **「卡码注册」模式下从首页进来的用户无法注册**：`registration_mode=code` 时后端要求
  `registration_code`，而门户注册表单里**没有注册码输入框**——只有从带 `?code=` 的链接
  进来才拿得到，点「注册」的人会看到「当前注册需要注册码」却无处可填。现在表单补上
  「注册码」与「邀请码」两个字段（链接带参时自动预填），邀请码也不再只能靠链接传递。
- **邀请返利在用户端几乎找不到**：功能一直都有（`/invite` 是完整页面：邀请码、邀请记录、
  返利台账、活动规则），但入口只有顶栏分组里一个「邀请」，移动端只藏在 ☰ 抽屉的
  扁平列表中间，首页那一格写的是「0 位好友已加入」——读起来像一条数据，不像能点。
  现在：抽屉首屏是一张邀请卡（图标 + 一句说明），首页那一格在未邀请时改成
  「邀请好友得积分」并高亮，个人中心的功能地图里也有带状态说明的入口。
- **播放页正文两侧直接贴屏幕**：`WatchView` 用了 `class="container"`，但这个类
  **从来没有全局定义**（只在 HomeView / ProfileView 等组件的 scoped 样式里各写一份），
  所以播放器下方的内容在桌面端没有居中也没有左右留白。现在 `.container` 进全局样式。
- **工单页自成一派**：`TicketsView` 自己写了一套 600px 容器与 `#fafafa` 标题色，
  比别的页面窄一截，窄屏下标题和按钮还会挤在一行互相压。

### 变更 (Changed)
- **页面骨架收敛成一套**：用户端此前同时存在 1080 / 960 / 680 三种内容宽度（外加一个
  没有定义的），同一个用户从首页走到钱包，内容宽度会跳一次。现在 `.au-page` 与
  `.container` 等价（1080px + 统一留白），窄栏内容用 `.is-narrow`（个人中心 720px），
  六个组件里各自的 scoped 副本全部删除。
- **页头一套**：消息 / 求片 / 观看记录 / 工单各写一份的 `.page-head` 合并进全局，
  并统一补上 `flex-wrap`，窄屏标题与右侧按钮不再互相压。
- 消息中心支持 `?tab=<类型>` 深链：顶栏铃铛点公告预览直接落到「公告」分类，
  不再丢到「全部」让用户自己再找一遍；公告分类有了自己的空状态文案。
- 移动端底部导航坞底色改用 `--au-overlay`（与顶栏、抽屉同一层毛玻璃）。

### 测试 (Tests)
- `scripts/check_frontend_tokens.py` 与 `scripts/check_frontend_routes.py` 继续通过：
  本轮新增的样式与跳转都在门禁覆盖范围内。

## [2.6.27] - 2026-09-21

这一版不加功能，只做**全局 UI 打磨**：把「同一个东西在不同页面长得不一样」和
「样式写了但根本没生效」两类问题收干净，并把它们变成可回归的门禁。
用户端的设计语言从 v2.3 起就换成了 Aurora（电光青 + 毛玻璃），但兼容层、设计令牌与
一份从未被引入的样式表还停在旧主题上，页面之间因此存在两种灰、两种红、两种「成功」。

### 修复 (Fixed)
- **设计令牌自引用（全站文字层级被静默降级）**：`index.css` 的兼容映射里写着
  `--text-primary: var(--text-primary)`、`--text-secondary: var(--text-secondary)`。
  CSS 规范里这是循环引用，变量会变成 invalid at computed-value time：
  所有 `color: var(--text-primary)` 既不报错也不生效，而是静默「继承父级颜色」——
  卡片标题会继承到父级的弱色，层级反了。现在兼容层只做「改名」，全部指向 Aurora 令牌。
- **兼容层写死了旧主题的颜色**：`--bg-glass` 曾写死 `rgba(20,20,20,.8)`、
  `--bg-glass-hover` 取中性灰 `#1a1a1a`——Aurora 的毛玻璃（半透明 + 模糊 + 发光底色）
  被这两行盖成了不透明灰块，页面背景的氛围渐变透不上来。现在指向 `--au-surface*`，
  `.glass` / `.glass-card` / `.card` / `.btn-secondary` 与 `.au-card` 终于是一套表面。
- **三个组件引用了「没人定义」的令牌**：`ListItem` / `SectionHeader` / `StatCard` 用的是
  `mobile-tokens.css` 里的 `--space-*` / `--card-bg` / `--text-caption-*`，而那个文件**从未被
  main.ts 引入**（v2.3 重构时的遗漏）。变量解析失败 → `padding: var(--space-md) 0` 整条声明
  作废：列表项没有内边距、统计卡**没有背景和描边**、区块标题字号字重全丢。现在统一改用
  已经加载的 Neo-Noir / Aurora 令牌，并删掉两个从未被引入的样式文件（`mobile-tokens.css`、
  模板残留的 `assets/base.css`）。
- **同一语义两三个颜色**：成功色曾被写成主色（成功提示与主按钮同一个青，等于没有语义）、
  危险色同时存在 `#ef4444` 与玫瑰红 `#fb7185`、警告色同时有 `#f59e0b` 与 `#fbbf24`、
  链接还是上一版主色留下的绿色。现在语义色（成功 / 警告 / 危险 / 信息）与链接色
  全部指向 Aurora 令牌，全站一个语义一个颜色。
- **主按钮对比度不达标**：旧 `.btn-primary` 是青底白字，对比度约 1.9:1（WCAG AA 要求 4.5:1）。
  现在与 `.au-btn-primary` 同一套外观：渐变底 + 深墨色文字 + 主色发光。
- **通知（Toast）从左侧半个身位滑回来**：容器已经用 `translateX(-50%)` 居中，子项入场又写了
  一次 `translateX(-50%)`，每条通知都会先横向偏移再归位。现在只做「淡入 + 轻微下移」。
- **管理后台「设备风控」手机卡片没有背景**：`var(--card-bg)` 在管理端并未定义（只存在于
  被删掉的移动端令牌里），现改为 `--bg-card`。

### 变更 (Changed)
- **原生控件跟随暗色**：新增 `html { color-scheme: dark }`。不加这一行，复选框 / 下拉 /
  日期选择器 / 滚动条会拿出系统浅色皮肤（白底黑字），是「页面里有几块不是自己的」的主因。
- **用户端补上 `viewport-fit=cover`**：缺了它，iOS 上 `env(safe-area-inset-*)` 恒为 0，
  底部导航坞与吸底按钮为「避开小白条」预留的内边距全部失效（管理端早已有，这里补齐）。
- **首屏启动画面**：`index.html` 内联一层与 Aurora 同色的启动页（氛围底色 + 环形加载），
  JS 挂载前不再闪白底/无排版内容；`lang` 补成 `zh-CN`，`theme-color` 从品牌青改成
  页面底色（移动端滚动时不会出现一条亮青色带）。
- **动效降级补齐**：`prefers-reduced-motion` 现在覆盖入场动画、悬停位移与交错列表，
  并把 `.stagger-*` 显式设为 `opacity: 1`——只关动画会让这批默认 `opacity: 0` 的内容
  永久不可见，这是无障碍降级最常见的翻车点。
- 全局细节：`100dvh`（移动端地址栏收放不跳）、`-webkit-tap-highlight-color`、
  `scroll-padding-top`（锚点不被吸顶导航盖住）、Firefox 滚动条配色、图片不撑破容器。

### 测试 (Tests)
- 新增 `scripts/check_frontend_tokens.py` 并接入 CI（两个前端矩阵都会跑）：
  ① 任何 `var(--x)` 的 `--x` 必须在**被引入**的样式表里定义过；
  ② 禁止令牌自引用与循环引用；③ src 下不允许存在谁都没引入的样式表。
  上面那次事故（未引入的令牌文件 + 自引用）从此构建期就会失败。
- 版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 2.6.27。

## [2.6.26] - 2026-09-21

不加新功能，只修「跑久了会变差」的东西。这一版把**长期运行**当成硬指标：
扫描的内存与磁盘压力自上版降下来之后，剩下会随时间累积的（崩溃残留的状态、异常退出的会话、
临时文件、孤儿子进程、孤儿行）全部收进一个看护模块，并且给它们配了可回归的冒烟脚本。

### 新增 (Added) — 运行期维护（看护线程）
- `backend/emby_server/maintenance.py`：EM 与 EA 启动时各跑一遍残留收尾，之后每 600 秒跑一次
  （`MAINTENANCE_INTERVAL`）：
  - **残留的「扫描中」标志**：进程被强杀后 `is_scanning` 会停在 True（界面永远“扫描中”、
    「刷新全部」永远跳过这台库）。判定用「这次扫描什么时候开始的」（`updated_at`），
    **不用 `last_scan_at`**——它记的是上次扫描结束时间，首次扫大库是 NULL、几天前扫过是旧的，
    两种都会指向“很久没动”而把**正在跑的扫描**清掉（多机部署时更危险：清掉后第二个扫描会
    并发跑，互相把对方刚扫到的条目当“已删除”清理）；本进程注册在扫的库直接跳过。
  - **异常退出的播放会话**：客户端崩溃不会上报 Stopped，心跳超过 10 分钟就标记结束，
    结束时间取上次心跳而不是收尾那一刻。
  - **HLS 转码目录**：重启后注册表为空，旧目录再没人管（`/tmp` 是 tmpfs 的机器上就是内存）；
    启动时全清，之后只清「已停下且超过 5 分钟」的，`subs` / `pan115` 这类共享缓存不动。
  - **字幕缓存**：只增不减的 VTT，先删过期（默认 7 天）再按数量删最旧的（默认 2000 个）。
  - **孤儿子进程**：退出时终止在册的转码会话并清目录，不再留下继续吃 CPU/磁盘的 ffmpeg。
- 会话表默认仍**永久保留**（后台的“总播放次数 / 一周热门 / 按日趋势”靠它）；小机器想控制库体量
  可以设 `SESSION_RETENTION_DAYS=90`，看护线程只裁**已结束**的旧会话（正在播的永远不碰）。
- 健康检查（EM `/api/health`、EA `/api/health`）新增 `runtime`：转码会话数、正在扫描的库数、
  临时目录占用、磁盘余量。目录统计有硬上限，残留再多也不会把容器探针拖慢，被截断时标注
  `transcode_scan_truncated`；健康检查自身任何异常只降级，不会变成 500。

### 修复 (Fixed)
- **扫描清理阶段会整库载入内存**：删文件后的对账从 `query(...).all()` 改成**游标分批**
  （每批 `SCAN_BATCH`），十万级媒体库不再出现几十万个 ORM 对象的内存尖峰。
- **播放进度/收藏的孤儿行会永久累积**：`MediaStream` 有 ORM 级联，`UserMediaData` **没有**——
  旧实现删条目时留下永远指向不存在条目的行，只增不减。现在清理时与媒体流一起显式删除。
- **删库与扫描互斥**：扫描中的库不允许删除（HTTP 409），删除改成分批（每批 500 条）；
  扫描线程在批次边界发现库已删也会提前收工，不再往已删库继续插条目
  （SQLite 复用 rowid，新库撞上同一个 id 时那些条目会“复活”）。
- **后台线程的异常不再无声**：扫描/后台任务抛错会落日志（此前只留在 stderr，界面只看到
  “扫描没反应”）。
- **并发扫描时同一目录会被重复列举**：目录列表缓存是「查 → 取 → 存」两段式的，同一个目录下
  的多个文件被分发到不同工作线程时，几个线程会同时落空并各自真列一次（CI 上表现为 3 个目录列了
  6 次）；远程挂载更贵——每个线程各发一次网盘请求。现在都是**单飞**：同一目录只会真的列一次
  （本机在同一把锁内完成，远程按目录一把锁，不同目录仍然并行）。
- **限流桶无界增长**：原先只清理空队列，打劫过的 IP 留着时间戳就永远不回收；
  现在超过 5000 个桶时按「见过的最大窗口」清掉所有已过期的。

### 测试 (Tests)
- `scripts/smoke_test_scan_budget.py` 新增**并发单飞**断言：用屏障把 8 个线程同时送进同一个目录，
  目录读次数必须恰好为 1（旧实现实测 7 次），不再靠“本机跑得快”蒙混过关。
- 新增 `scripts/smoke_test_maintenance.py`（已接入 CI）：残留标志的复位口径（含
  “正在扫的绝不误清”）、过期会话回收、转码目录/字幕缓存/退出收尾（含**真子进程**被终止）、
  健康检查统计上限、以及清理阶段的资源与数据完整性（小批大小跨批、孤儿行清干净）。

## [2.6.25] - 2026-09-21

把「自建后端要能在小机器上跑」当成硬指标：扫描不再按文件来回查库 / 落盘 / 串行等网络，
大库扫描的内存与磁盘压力都压下来了；顺带重做了首页的消息入口。

### 性能 (Performance) — 扫描器
- **批量查库**：条目查询从「每个文件 1~3 次」变成「每 400 个文件一次」（`SCAN_BATCH`），
  十万个文件从几十万次库往返降到几百次；
- **批量落盘**：扫描主体里每个文件的 `commit()` 退化成 `flush`，真正的提交只在批次边界发生。
  实测 162 个文件的库：提交 166 次 → 5 次，即 fsync 次数降了两个数量级（这是「扫一次库把盘拖住」的主因）；
- **并行 IO**：ffprobe 探测 / 目录列举 / TMDB 搜索与详情放进线程池（`SCAN_WORKERS`，默认 4），
  追新时不再逐条串行等网络；
- **TMDB 响应缓存**：300 秒 / 2 万条上限。预热线程与写库线程调用的同一请求只走一次网络，
  不会因为「预热 + 写库」各调一次就双倍消耗配额；
- **目录只列一次**：同一目录下的图片与字幕查找共用一份列表（旧实现每个文件列两次，
  远程挂载上是两次网络请求）。
- 可调参数：`SCAN_BATCH`（默认 400）、`SCAN_WORKERS`（默认 4），小机器可以调小。

### 变更 (Changed) — 用户端首页消息入口
- 站内消息**不置顶、也不占用账号速览条**：速览条回到四格纯「账号与经济」数据
  （会员 / 积分 / 签到 / 邀请），消息中心变成「站点与账号」区的第一张卡——
  一行标题 + 最多两条最新内容（未读优先，无未读时显示最新公告）+ 未读胶囊。
  随时可进的地方仍是顶栏那个带角标的音铃（点开先看预览）。

### 测试 (Tests)
- 新增 `scripts/smoke_test_scan_budget.py`（已接入 CI）：断言批量查库、批量提交、
  并行探测、并行刮削、TMDB 不重复请求、目录只列一次，以及删文件后清理阶段仍然正确。

## [2.6.24] - 2026-09-21

把「归属」按**它会不会改变判断**分成三类，只给会改变判断的维度加按服，其余保持全局：
卡码（会决定用户在哪个服能播）必须按服且有一个真 bug 要修；求片（出口是全局共享的下载器）
按服只用于说清楚“给哪个服求”；挂载后端早已按服，只差页面用起来。

### 修复 (Fixed)
- **卡码核销完全不看服（多服下会开错会员 / 该开的开不了）**——付费墙自 v2.6.20 起严格按服判定，
  但卡码这条链路没跟上：
  - `has_active_membership` 是全局判定：甲服会员去核销**乙服的注册码**会被拒（提示「已有生效会员」），
    明明是两个互不相干的服；
  - `grant_membership_days` 新建订阅**不写 `realm_id`**：卡码开的会员在按服付费墙下匹配不到任何服
    （EA 上 `target=本服`，`NULL` 不等于本服）——**卡码开的会员在 EA 上根本放不了**；
    更糟的是下次重启时迁移会把这条空归属订阅回填给**默认服**，乙服的卡码就变成了给甲服的会员；
  - 续期码选的是「到期最晚的生效订阅」且**不看服**：乙服的续期码会直接叠加到甲服的订阅上。
  现在核销按**卡码自己的服**判定与发放：只在同服内叠加、新建时写入归属服、
  注册码的「已有会员」判定交给 `subscriptions.has_active_subscription`（与付费墙同一口径）；
  预检/核销结果都会告诉用户“开的是哪个服的会员”；注册时用乙服的注册码注册，拿到的也是乙服的会员。
- **挂载换归属服时媒体库会跨服引用存储**（库还在甲服、挂载已在乙服）：换服时引用它的媒体库一起跟过去（后端已有，本次写进注释与页面提示）。

### 变更 (Changed)
- **卡码管理页按服**：默认只看当前服（一条卡码开的是它所属服的会员），可切「全部服」并显示归属服列；
  「类型化生成」新增**归属服**选择（默认当前服）；`GET/POST/PATCH /registration-codes/*` 均支持 `realm_id`，
  已被核销的卡码不允许改归属（否则「开的是哪个服的会员」对不上账）。
- **存储挂载页按服**：列表默认只看当前服、可切「全部服」（多一列归属服）；新建 / 编辑可指定归属服（默认当前服）。
- **求片改为“说清楚给哪个服”**（出口仍通用）：用户端提交求片时——
  - 只持有一个服的会员 → 自动归到那个服（页面上只给一行说明，不用选）；
  - 持有两个以上服的会员 → 给一个服的选择器，**必须选一个**（不预选，预选错会把片子放进另一个库）；
  - 后端也能自己推导：唯一一个服的会员就归它，多个服又不选则记「未标注」（不替用户猜）。
  后台求片页可按服筛选并显示归属服；用户端列表能看到自己的求片是给哪个服的。
- **钱包写明口径**：积分与余额**全站通用（多服共用一份）**，会员是一个服一个——避免用户以为“换服要充另一份钱”。
- **站内消息不再占通栏**：删掉首页 Hero 之上那条通栏消息条，改为
  - 顶栏消息入口：有未读时是一枚会呼吸的胶囊（「N 条未读」）+ 点开先给预览（未读消息 + 置顶公告 + 「查看全部消息」）；
  - 首页问候语旁的胶囊：未读亮警示色，无未读时显示最新公告标题或安静入口。
  两处都指向消息中心，未读不会再藏在屏幕下面。
- 多服口径写进 [`docs/operations.md`](docs/operations.md)：「什么按服、什么全局、为什么」一目了然。

### 新增 (Added)
- 多服运营冒烟从 67 项扩到 **102 项断言**：卡码按服核销（甲服会员能用乙服的注册码、乙服续期码只延长乙服的会员、
  老卡码未标注归属按当前服）、求片归属（单服自动 / 多服必选 / 未选则未标注）、挂载按服列出、跨服数据不误露。
- `docs/operations.md` 新增「多服运营：什么按服、什么全局」一节（含余额/积分通用的原因）。

### 文档 (Docs)
- 版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 **2.6.24**。

## [2.6.23] - 2026-09-21

把 Emby 相关的东西收成**一页**：以前想知道「这台 EA 到底在不在服务这个服」要跑四个页面拼信息。

### 变更 (Changed)
- **「服务器」页升级为「服务器 · Emby 总览」——Emby 相关功能的唯一入口**（导航同名）。
  页面不再是一张平铺的服务器表，而是**按服分组**给出每台出流入口（EA / 已有 Emby）的全貌：
  | 列 | 回答的问题 |
  | --- | --- |
  | 连接 | 面板还连得上吗（最近一次体检的结论与时间） |
  | 节点 / 归属 | 这台有没有配 `NODE_KEY`、认领到哪条记录、**EA 自己说它属于哪个服** |
  | 挂载体检 | 从**这台机器**看，哪些存储碰不到（挂载是本机相对的，面板测通不代表它能播） |
  | 媒体库 | 归它出流/扫描的库数，以及同服里还没分配的库数 |

  页面顶部给出总览数据（入口数 / 连接正常 / 媒体库 / **未分配节点** / **需要处理**）与「全部服 ↔ 仅当前服」
  范围切换；每张服卡片直接显示「当前用哪个入口出流」与它的地址，并提供「加 EA / 加 Emby」（自动预填该服）。
- **告警不再靠人对账**：EA 自称的服 ≠ 面板登记的服、`NODE_KEY` 两边对不上 / 认领不到、挂载不可达、
  连接失败、从没做过体检、一个服有多台 EA 却没配 `NODE_KEY`、某个服还没有入口 / 还没有「当前使用」——
  全部逐条列在对应行与服卡片下；「面板自己出流」是单机部署的正常常态，不会被当成错误。
- **删掉重复入口的最后一块**：出流入口（EA / Emby）与内容自动化（MoviePilot / qB）在页面上分成两段，
  同一台服务器只会出现一次；旧的「Emby 服务入口」页与它的导航项已于 v2.6.21 下线（`/emby-servers` 跳转保留）。
- **媒体库 / 存储挂载 / 服管理保持各管自己的一摊**（不再重复入口）：媒体库页管内容与扫描、
  存储挂载页管挂载本身与逐条体检、服管理页管服的增删改——总览页只负责「看与入口相关的事」。

### 新增 (Added)
- `GET /api/admin/servers/overview`（Emby 总览）：按服返回每台入口的连接 / 节点身份 / 挂载体检 /
  库归属与告警，附总量与类型统计；`?realm_id=0` 看全部服。`?live=1` 会真的去问每台已启用的 EA
  「你是谁、属于哪个服、负责哪些库」（`GET /api/admin/nodes/me`，共享 `SECRET_KEY` 鉴权，单次 8 秒超时）
  并重拉当前出流 EA 的挂载体检；默认只读已落库的结论，避免打开页面就被慢节点拖住。
- 后台页头新增「一键体检」（live 模式，结果里有多少条需要处理会直接提示）。

### 测试 (Tests)
- `scripts/smoke_test_servers.py` 扩充到 81 项断言：总览只把 EA / Emby 当出流入口（内容自动化不混进来）、
  每行带归属服与服标识、外部 Emby 那行标出「当前入口」、挂载体检与库归属计数、连不上的 EA 被标告警、
  `live` 拉回节点身份（没配 `NODE_KEY` 不误报、探不到就如实报错）、空服给出可操作提示、密钥不外泄。
- 版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 2.6.23。

## [2.6.22] - 2026-09-21

两处体验收尾：站内消息放到最显眼的位置，管理员进后台不再「刷新两次」。

### 变更 (Changed)
- **用户端首页的站内消息条置顶**：v2.6.21 把它从页面底部挪到了速览条之后，但仍然要先看到
  Hero 与账号速览才轮到它。现在它是**页面上第一个可点的东西**（Hero 之上、顶栏之下的第一条）：
  有未读时整条走高亮警示色 + 数字徽标 + 状态圆点，只有公告时直接显示公告标题，什么都没有时
  是一条安静的入口（不隐藏，保证入口永远可发现）。窄屏下收起「去查看」字样，优先保住消息本体。

### 修复 (Fixed)
- **管理员从个人中心进后台会「刷新两次」**：本地存的后台票据只看「有没有、过没过期」就当成
  已登录并放行渲染，但它可能服务端已经不认了（换过 `SECRET_KEY`、账号被回收、或来自另一个会话）——
  于是页面先渲染一遍、几十个请求一起 401、再重载一次，用户看到的就是两次刷新（登录页一闪而过）。

  现在会话在**首屏渲染之前**一次性敲定（`auth.ensureSession()`，路由守卫里 await）：
  1. 本地有票据 → 先问一次 `/api/admin/auth/me`；通过就用它（顺带刷新管理员信息），不通过就丢弃；
  2. 本地票据不可用 → 门户免登（含过期换票）：成功就接管，失败才去登录页并给出原因。

  代价是每次进后台多一个很小的探测请求，换来的是**一次跳转、不再闪错**，并且失效票据
  不会再把用户送到「登录页→自动登回来」的往返里。401 的自愈重载仍作为运行中过期
  （页面开着很久）的兜底保留。
- 版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 2.6.22。

## [2.6.21] - 2026-09-21

本版修掉三处「用户实际会撞上」的毛病：升级上来的库会 500（上一次多服改造引入的回归）、
管理员从个人中心进后台先报两次错、用户端首页看不到站内消息；并把重复的「添加服务器」入口收敛成一个。

### 修复 (Fixed)
- **升级上来的库会缺列，管理后台首页直接 500**（严重）：自动迁移清单写成了 `{表名: 列}` 字典，
  而 `registration_codes`（v2.5.6 与 v2.6.20 两批）与 `movie_requests`（v2.6.19 与 v2.6.20 两批）
  都出现两个同名键——Python 字典字面量里后一个键会**静默覆盖**前一个，于是
  `movie_requests.realm_id` 与 `registration_codes.code_type/days/is_decoy/target_username/source`
  在升级库上永远不会被补上，查询直接报 `no such column`。
  - 全新安装（`create_all` 一次建全）一切正常，所以只在**真实部署升级后**暴露：
    `GET /api/admin/stats/overview`、`GET /api/admin/realms/overview`（后台首页）500、
    卡码页无法读取。
  - 现迁移清单改为**列表**（同一张表可以出现多次，不再互相吞掉），并新增兜底对账
    `_backfill_orm_columns`：以 `Base.metadata` 为准，把**任何**模型声明了而库里没有的列补上
    （只补列、不删列、不改类型），以后新增字段忘了写进清单也不会再炸。
- **管理员从个人中心进后台会先报两句错**：
  - 本地存着的后台票据过期后仍被当成「已登录」，于是先打一串必然 401 的请求，
    弹一句「无效或已过期的凭证」才被送去登录页免登。现启动与每次路由判定都读 JWT 的 `exp`，
    过期直接丢弃并走门户免登，不再产生无效请求。
  - 401 不再是「弹错 + 硬跳登录页」，而是清掉失效票据后**原地重载**，由路由守卫用门户会话
    静默接管（成功即原地恢复）；恢复不了才落回登录页，且 401 本身不再弹全局报错。
    加了 15 秒节流，避免「重载 → 又 401 → 再重载」的死循环。

### 变更 (Changed)
- **「添加服务器」入口收敛为一个**：后台「Emby 服务入口」页（两个格子的旧入口）与「服务器」页
  （可加多台 EA / 已有 Emby / MoviePilot / qBittorrent，并按类型挑一台「当前使用」）在做同一件事。
  现下线旧页与它的导航项，统一到「服务器」页；`/emby-servers` 旧地址保留为跳转，收藏与
  外部链接不会落到 404；用户可见的引导文案（面板报错、服管理页说明）一井指向新入口。
- **用户端首页的站内消息上移到首屏**：消息/公告原先是一条细条，排在「最近入库」之后——
  内容一多就被顶出屏幕，用户根本看不到未读。现改为速览条之后的独立消息卡（未读时高亮 +
  数字徽标），没有新消息时也保留入口，不再依赖滚动到页面底部。
- 版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 2.6.21。

### 测试 (Tests)
- 新增 `scripts/smoke_test_schema_migration.py`（23 项断言，已接入 CI）：拿一个**只有旧列的老库**
  跑完整的 `init_db()`，断言「模型声明的列库里必须都有」、老数据被回填到默认服、迁移幂等，
  并逐个验证当年 500 的那几个接口现在都 200。全新库发现不了这类问题，所以这条用例是必需的。

## [2.6.20] - 2026-09-21

本次把面板从「一套服务」变成**一个可以同时运营多个服的管理平台**，并让**一个后端服能部署到多台
机器上同时出流**：以前的隐含前提是「一套库、一份套餐、一台 EA、一个入口」，多服多机都做不到。

### 新增 (Added)
- **「服」= 一套可独立运营的播放服务**（`server_realms` 表 + `/api/admin/realms`）：内容（媒体库 /
  存储挂载）、卖什么（套餐）、卖给谁（订阅）、谁来放（播放节点）、卡码与求片全部归属到某个服。
  服的清单 / 新建 / 改名 / 停用 / 启用 / 删除（有数据时必须 `move_to` 移交）/ 切换当前服 / 节点体检，
  全部落管理审计；没有服时自动建默认服 `slug='main'`，并把旧数据回填给它。
- **升级不改变行为**（这是整块设计的约束）：默认服沿用**历史配置键名**（`emby_active_mode` /
  `emby_managed_url` / `emby_external_api_key` / 挂载体检快照），其它服用 `<键名>__r<服 id>` 后缀。
  单服部署读到的键一字未变，外部脚本与运维文档不受影响。
- **管理后台「服管理」页**：每服一张卡——运营数据（媒体库 / 条目 / 挂载 / 套餐 / 有效订阅 / 待审
  求片）、该服的播放节点与在线状态、对外地址（用户端账号卡用）；支持新建 / 编辑 / 切换 /
  「节点体检」/ 删除（含数据移交选择）。导航新增「多服运营」分组，含「服管理」与「服务器」。
- **顶栏「当前服」切换器**：服务端 `SystemConfig['active_realm_id']` 是权威来源（多管理员一致），
  切服后各页作用域跟着变；切换完自动重载页面，避免停在旧服数据上。
- **多台 EA 同时出流**（`backend/emby_server/nodes.py`）：
  - EA 用 `NODE_KEY` 认领面板「服务器」里属于自己的那条记录（找不到就自动登记一条）；
  - `REALM`（或在面板里给这台服务器指定的归属服）划定**内容边界**：只提供本服的媒体库、只认本服的订阅；
  - 媒体库新增 `node_id`：**NULL = 未分配**（所有节点可见、由面板扫描），已分配则只有归属节点
    向客户端展示、只有它扫描（面板点「扫描」会转发给它，走共享 `SECRET_KEY` 鉴权）；
  - 可见性用 SQLAlchemy `do_orm_execute` + `with_loader_criteria` 挂在 `Library` / `MediaItem`
    的所有 ORM 查询上，不逐个端点改（漏一个就是内容泄漏）；写入路径不受影响；
  - 两者都没配置时**完全不过滤**：单服单机部署零影响。
- **会员一个服一个**：`user_subscriptions.realm_id`，付费墙按服判定（甲服的会员不能在乙服的 EA 上
  播放）。EA 启动时装进程级「本进程的服」解析器（`subscriptions.set_process_realm_resolver`），
  没装解析器或解析不到时按原来的不分服口径放行。
- **EA 侧新增内部端点**（`X-Panel-Key` 鉴权，与挂载体检同一套）：`GET /api/admin/nodes/me`
  （这台节点是谁、属于哪个服、负责哪些库）、`GET /api/admin/nodes/libraries`、
  `POST /api/admin/nodes/libraries/{id}/scan`（由归属节点执行扫描）。`/api/health` 与
  `GET /api/admin/realms/{id}/sync` 会把节点自称的 `realm_slug` 带回来，配错会当场报警。
- **服务器归属与共用**：服务器新增 `realm_id` 与 `node_key`；EA / Emby 是一个服一个的
  （新增时要选归属服），MoviePilot / qBittorrent 可以声明**「全服共用」**（归属留空，每个服都能用它求片）。
- **用户端**：账号卡按服列出地址与会员（`realms` 数组，顶层字段保持默认服口径），个人中心显示
  「我的服」与会员归属服，钱包里的套餐带归属服名——多服部署下用户能分清该连哪台。
- 新增冒烟测试 `scripts/smoke_test_realms.py`（67 项断言，已接入 CI）：真管理端接口走完整链路
  （默认服与历史键名兼容、套餐/订阅/清单按服、会员按服判定、共用内容自动化、
  媒体库归属服与节点的分配/拒跨服、多节点内容边界、删服必须先移交、默认服不能删）。

### 修复 (Fixed)
- **用户端账号卡读取服参数时引用了不存在的 `request`**：`GET /api/user/emby/server` 在加入
  `?realm_id=` 支持时漏掉了 `Request` 参数，请求会 500（`NameError: name 'request' is not defined`）。
  已补回参数（`scripts/smoke_test_auth.py` 覆盖这条链路）。
- **内容查询的服口径**：媒体库与存储挂载改按 `realms.scope_inclusive` 过滤——`realm_id` 为空的历史
  数据 / 直接入库的内容算「所有服可见」，而不是被严格相等过滤藏起来（与节点可见性同一口径）。
- **订阅清单的搜索框真的能用了**：`GET /api/admin/realms/{id}/subscriptions` 支持 `search`
  （用户名 / 邮箱模糊匹配）；用户端订阅列表补上 `realm_name`。

### 变更 (Changed)
- **订阅 / 套餐 / Emby 服务入口 / 挂载体检快照全部按服读写**：`backend/subscriptions.py`、
  `backend/servers.py`、`backend/api/{admin,economy,user,emby_servers,servers}.py`、
  `backend/emby_server/{portal,mount_health}.py` 的入口都带上了服（默认服沿用历史键名）。
- **面板自带的 Emby 网关只服务一个服**（默认服，可用 `EM_GATEWAY_REALM` 指定）：一体化部署
  升级上来行为不变，多服的内容由各自的 EA 出流。
- 版本号 2.6.19 → 2.6.20；`env.example` 补上 `NODE_KEY` / `REALM` / `EM_GATEWAY_REALM`；
  `docs/deploy-ea.md` 新增「多台 EA 与多个服」章节，`docs/operations.md` 补多服排错条目。

## [2.6.19] - 2026-09-21

本次把「服务器」变成一份可增删改的清单（面板能加多台、能一眼看出接了什么），并把
**求片真的接到下游**：以前求片只能改状态，批了之后得管理员自己去别处搜片子。

### 新增 (Added)
- **服务器清单（`remote_servers` 表 + `/api/admin/servers`）**：四类服务器都能加多台——
  `ea`（本项目自带的 Emby API）、`emby`（已有的 Emby / Jellyfin）、`moviepilot`、`qbittorrent`。
  列表 / 统计 / 新增 / 修改 / 删除 / 启用停用 / 重新体检 / 设为当前使用，全部落管理审计。
  类型字段与密钥声明由后端元数据下发（`SERVER_KINDS`），前端不再自己维护一份。
- **信息展示**：`GET /api/admin/servers/summary` 按类型统计「加了几台 / 几台可用 / 当前用哪台」，
  管理后台顶部四张统计卡与「数据概览」的">服务器接入"卡片共用这份口径；一点直达「服务器」页。
- **新增「服务器」页**（系统分组）：统计卡（可点筛选）、小白说明、清单表格（类型 / 地址 /
  连接状态与原因 / 当前使用）、新增与编辑对话框（字段由元数据驱动，对话框里可先「测试连接」再保存）。
- **MoviePilot 接入**（`backend/moviepilot.py`）：同时支持两套凭据——API_TOKEN（`?token=`）
  用于查订阅，用户名 / 密码（`/api/v1/login/access-token` 换 JWT）用于**新增订阅**。
  体检会分别报告「能不能查」与「能不能提交订阅」，只填 API 密钥时面板会明说还不能提交订阅。
- **qBittorrent 接入**（`backend/qbittorrent.py`）：`/api/v2/auth/login` 换 SID（qB 登录失败
  返回的是 `Fails.` 而不是错误码，所以看响应体而不是状态码）、`/api/v2/app/version` 报版本、
  `/api/v2/torrents/add` 加种（支持磁力 / `.torrent` 直链、保存目录、分类）。
- **求片一键转交**（`POST /api/admin/media-seek/{id}/push`）：`target=moviepilot` 提交为它的订阅
  （片名 / 年份 / `电影`或`电视剧`）；`target=qbittorrent` 加种（没给链接时会明确拒绝——qB 自己
  不会去找片）；`auto` 优先 MoviePilot。结果（交给谁、成没成、为什么没成）落库到求片记录，
  列表回传 `push_target` / `push_status` / `push_message` / `pushed_at`，推送成功后待审求片自动置为已批准。
- 「求片管理」页新增「转交外部服务」列与「交 MoviePilot / 交给 qB」按钮；一个可用目标都没有时
  如实说明并指向「服务器」页，而不是给一个点了只会失败的按钮。
- 新增冒烟测试 `scripts/smoke_test_servers.py`（63 项断言，已接入 CI）：**真起三个 HTTP 服务**
  （假 MoviePilot、假 qBittorrent、真 EA）验证新增即体检、连不上不能设为当前使用、
  激活后旧配置键被换过去、旧页面与新清单互相同步、删除当前使用的服务后不留在悬空模式上、
  密钥永不出接口，以及求片到 MoviePilot / qB 的完整链路。

### 变更 (Changed)
- **EA / Emby 的探测口径收敛到一处**：`backend/api/emby_servers.py` 的 `probe()` 改为调用
  `backend.servers.probe_ea` / `probe_emby`，两个页面（旧的两个格子 / 新的清单）不会对
  「连得上」给出不同结论。
- **EA / Emby 被设为当前使用时**，同步写回既有的 `emby_managed_*` / `emby_external_*` /
  `emby_active_mode`：网关闸门、客户端指引、用户端账号卡、挂载体检全部照旧生效，不需要改它们。
  另一类还有激活的服务器时优先交给它，都没有则回到「面板自己出流」（`panel`）。
- 旧的「Emby 服务入口」页依然可用：它的保存会同步进新清单（且只在连接测试通过时才标记为当前使用），
  导航里「服务器」排在它前面。
- 数据库自动迁移新增 `movie_requests.push_target / push_status / push_message / pushed_at`。

## [2.6.18] - 2026-09-21

本次把「这条挂载在哪台机器上真的能用」变成面板上可查的事实，让分离部署的存储问题在
**添加 EA 服务入口那一步**就暴露出来，而不是等到扫描 `failed_roots` 或播放 502。

### 新增 (Added)
- **EA 挂载体检端点 `GET /api/admin/mounts/health`**：在 EA 进程里对每条启用中的挂载
  跑一遍与后台「测试连接」相同的探测（`resolve` + 路径/目录可读性），返回逐条结论、
  统计、当前播放节点。鉴权走两端本来就共享的 `SECRET_KEY`（请求头 `X-Panel-Key`，
  常量时间比较）；密钥缺失或错误一律 **401** —— 漏配 `SECRET_KEY` 也绝不会变成公开端点。
- **EM 保存/启用 managed EA 时顺带体检**：`PUT /api/admin/emby/servers` 在连接探测通过后
  向 EA 拉一次体检，结果写入 `SystemConfig`（`emby_managed_mounts_health`）；
  `GET /api/admin/emby/servers` 会回一份摘要。新增 `POST /api/admin/emby/servers/mounts/refresh`
  供随时重拉。拉取失败（EA 连不上 / 密钥不一致 / 地址指向别的服务）时**保留上一次逐条结果**，
  只把快照标记为失效，避免一次网络抖动把面板上的结论全抹成未知。
- **「存储挂载」页新增「EM / EA 可达」两列徽标**（可达 / 不可达 / 未体检），逐条给出原因
  与最近体检时间；被媒体库引用、却在当前播放节点（EA）上不可达的挂载会单独报红，
  并说明「本机 / 已挂载目录」「STRM 目录」的路径与 rclone 的 RC 地址都是那台机器上的资源。
  只有当前出流节点是 EA 时才算阻断性告警（一体化与外部 Emby 模式不会误报）。
- `GET /api/admin/emby/mounts` 现在同时返回 `playback_node`、`ea_health` 摘要，以及每条挂载的
  `path_exists` / `em_reachable` / `ea_reachable`（含原因与时间）。
- 新增 `POST /api/admin/emby/mounts/health`：**EM 视角**一键体检，逐条跑探测并把结果写回
  挂载记录的「上次测试」字段（此前后台只能一条一条点「测试连接」）。
- 新增 `scripts/smoke_test_mount_health.py` 并接入 CI：覆盖端点鉴权（无/错密钥 401）、
  结论如实（目录可读→可达、目录不存在→不可达、停用→跳过不体检）、结果不泄露挂载密钥、
  EM 上不存在该端点、面板体检落库、「被库引用却 EA 不可达」可被判出，以及**真起一个 EA 发 HTTP**
  验证 EM 保存服务入口时会拉到 EA 视角的体检并落库。
- `scripts/deploy_check.py` 的分离部署环节补了端到端断言：EA 端点鉴权、EM 保存 EA 时真的
  以 EA 视角体检成功、挂载列表同时给出两个视角的可达性。

## [2.6.17] - 2026-09-21

本次并入 `fix/security-auth-and-deploy` 的用户端会话修复，并修正其登录跳转目标。

### 修复 (Fixed)
- **会话过期后被丢到 404 页而非登录页**：该分支把 `forceLogout()` 的跳转目标改成
  `/m/login`，并把所有 `/m/*` 路径当作登录页；但用户端并没有这套移动端路由
  （只有 `/login`，其余路径落到 catch-all → `NotFoundView`），所以令牌失效时用户会被
  送到「页面未找到」。现已统一跳转 `/login`。
- **重放请求的请求头空值崩溃**：排队重放与主动重放两处 `originalRequest.headers.Authorization`
  现在都先判断 `headers` 是否存在（来自 `fix/security-auth-and-deploy`）。
  同时与 2.6.16 的 `_retry` 单次重放守卫合并，保持「排队重放只重试一次」不变。

## [2.6.16] - 2026-09-21

本次修复管理后台在 `1f9fc5b` 之后无法运行的问题，并补上让这类破坏再也进不来的门禁。

### 修复 (Fixed)
- **管理后台打不开、也登不进去**：`admin_frontend/src/stores/auth.ts` 被换成了
  另一套架构的实现（httpOnly Cookie 会话 + CSRF + `role/permissions`），而本项目的后台
  是 Bearer JWT + `is_staff`，后端既没有 Cookie 会话也没有 CSRF 端点。该实现同时
  - 引入了不存在的模块 `@/types/auth`；
  - 删掉了 `token` / `admin` / `setSession` / `ssoFromPortal` / `ssoNotice`，
    而 `router/index.ts`（每次跳转都会 `await auth.ssoFromPortal()`）、`Login.vue`、
    `Layout.vue`、`Users.vue` 仍在调用它们——路由守卫必然抛 `TypeError`，后台进不去，
    登录提交也会抛错。
  现已还原为与本后端（同一套 JWT + 门户免登）一致的可用实现；那套尚未接线的
  `hasPermission` / `isSuperAdmin` / CSRF 接口全仓 0 引用，故一并移除。
- **管理端类型检查形同虚设**：`type-check` 原先执行 `vue-tsc --noEmit -p tsconfig.json`，
  而该 `tsconfig.json` 是 solution 式的（`"files": []` + references），**一个文件都不检查**；
  `vite build` 不做类型检查，`import type` 在构建时又会被擦除——于是上面那类破坏
  CI 全绿也照样合进主干。现改为直接检查 `tsconfig.app.json` 与 `tsconfig.node.json`，
  并用故意写错的探针文件验证过「改前静默通过、改后如实报错」。
- 暴露出来的两个既有类型错误一并修掉：`DataTable.vue` 手机卡片操作区的 `<slot v-for>`
  不能挂 `:key`（改为 `<template v-for>` 包裹）、`StorageMounts.vue` 未使用的 `isRemote`。

### 变更 (Changed)
- **用户端 401 刷新**：排队的重放请求现在也会置 `_retry = true`，避免新票据仍被拒时
  陷入无限循环刷新（保留 `75f5474` 修复「刷新失败导致排队请求永久挂起」的逻辑）。

## [2.6.15] - 2026-09-21

本次清掉旧架构残留，并重审全部功能链：修掉三条「看着能用、实际不对」的链路
（公告广播、通知投递回执、支付履约后的站内信）。

### 修复 (Fixed)
- **公告只发给当时在线的用户**：`POST/PUT /api/admin/announcements` 走的是
  `NotificationService.broadcast`，而它只遍历「当前 WebSocket 在线用户」，
  于是离线用户**永远收不到这条公告的站内消息**，接口却回了一句「已推送给所有用户」。
  现在 `notify_all_users` 会给**每个启用中的用户**落站内消息（禁用账号不投），
  再对其中在线的人做实时推送，接口返回真实送达人数（`notified_users`）。
- **支付/补单履约后的站内信被静默丢弃**：`_fulfill_order` 在履约事务**提交之前**就调通知，
  通知服务另开 session 写站内信，在 SQLite 上属于「读事务升级为写事务」，会被直接判为
  `database is locked`（不会等锁，`busy_timeout` 无效），结果是**钱收了、用户的「充值成功」站内信没了**。
  现在履约只返回「待发通知」列表，由调用方在 `db.commit()` 之后统一发（`send_fulfill_notifications`）。
- **邮件 / Telegram 通知「假成功」**：两个渠道的发送主体此前是 TODO 注释，
  却一律往通知历史里写 `status="sent"`——后台看起来「已发送」，实际一条都没出去。
  现在真正投递（SMTP / Bot API），失败如实记 `failed` 并写入 `error_message`（新增同名自动迁移列）。

### 变更 (Changed)
- **删除旧拆分栈的全部残留**：`admin_backend/`、`user_backend/`、`docker-compose.yml`、
  `deploy.sh` / `update.sh` / `dev.sh`、旧 `.env` 模板、旧 `nginx.conf`、`user_frontend_dist/`，
  以及从未被文档使用、且 `nginx.conf` 指向已删除服务的三个 `Dockerfile` 与两个前端 `nginx.conf`。
- **删除跑不通的 Telegram 登录 Bot**：`telegram_login_bot/` 调用的是
  `{WEB_URL}/api/user/auth/telegram-login`，而 EM 从未实现该端点、前端也没有入口，
  登录链接必然是 404；连同它在部署文档里的说明一起移除（要做请另做带签名校验的链路）。
- **删除过时文档**：根目录的 `FIX_REPORT.md`、`FRONTEND_BACKEND_LINKAGE.md`
  与 `admin_frontend/docs/navigation-architecture.md`（描述的是未实现的导航重构，
  其组件与配置文件在代码里都不存在），以及 `docs/route-admin-design.md`、
  `pytest.ini` 与走开发库的旧 `tests/`。

### 新增 (Added)
- `scripts/smoke_test_ops_v260.py` 增加公告广播与通知渠道断言：离线用户也收到公告、
  禁用账号不投递、SMTP/TG 失败必须记 `failed` 而不是 `sent`。
- `scripts/smoke_test_economy.py` 增加履约通知断言：待发通知列表 + 提交后站内信真落库 + 重复回调不重复通知。
- `scripts/smoke_test_admin_account.py` 增加门户免登（SSO）断言：升级前后同一个门户 token
  都能直接进后台，管理员从门户登录同样免登。
- `scripts/smoke_test_v251.py` 增加求片链路的端到端断言：用户提交 → 管理端列表/审批 → 用户看到状态并收到站内信。

## [2.6.14] - 2026-09-21

本次修复上一轮审查里剩下的两类问题：**经济链的并发竞态**与**限流可被伪造头绕过**，
并清掉仓库里泄露的旧生产口令。

### 安全 (Security)
- **限流不再可被伪造 `X-Forwarded-For` 绕过**：`client_ip()` 原先取 XFF 的**第一段**，
  而仓库里的 Nginx 用的是 `$proxy_add_x_forwarded_for`（客户端自带的头会排在前面），
  于是每次换一个自造的头就换一个限流桶，登录/注册/核销限流形同虚设。
  现在改为优先 `X-Real-IP`（Nginx 按 `$remote_addr` 硬写，不可伪造），
  其次取 XFF 的**最后一段**（由最近一跳代理追加），最后才是直连地址。
  登录日志记录的 IP 与限流口径同时因此变准。
- **移除仓库里泄露的旧版生产口令**：`docker-compose.yml` 与 `admin_backend/admin_database_user.py` 里
  曾经硬编码过 PostgreSQL 口令（`web.md` 里还含 Redis 口令），现改为必须从环境注入，
  缺失时直接报错而不是默默用提交进仓库的口令。⚠️ 用过旧版栈请**立即更换数据库与 Redis 口令**。

### 修复 (Fixed)
- **单次兑换码可被并发重复核销**：`POST /exchange/redeem` 原来是「先查是否可用 → 再发奖励 → 再 +1」，
  并发下两个请求都能通过检查、发两份奖励。现在改为**先原子占位**（`UPDATE … WHERE 仍可用` 并看 rowcount），
  占位失败直接返回，绝不发奖；占位成功才读回详情发奖。
- **同一用户可被并发重复签到**：签到原来靠「先查后插」，并发下会双双通过检查。
  现在落库前先 `flush()` 让 `(user_id, checkin_date)` 唯一索引兜底，
  冲突返回 400「今天已经签到过啦」而不是 500，且不会发出第二份积分。
- **积分并发累加会丢更新**：`_add_points` 原来是 Python 读改写（`user.points = user.points + n`），
  两个请求各读到同一旧余额时后提交者会覆盖前者。现在改为 SQL 级自增并回读真实余额。
- **邀请奖励可被重复发放**：`apply_invitation` 缺少幂等判断，重复调用会给邀请人与被邀请人各再发一次。
  现在同一被邀请人只允许一条关系记录（模型加唯一索引 + `flush` 兜底 + 提前返回）。
- **同一用户并发叠加会员天数会丢天数**：订阅续期是「读到期末 → 加天数 → 写回」，
  并发发放（同时核销两张码 / 码与支付回调撞车）时后者会覆盖前者。
  现在 `_grant_subscription` 与 `grant_membership_days` 的订阅查询加 `with_for_update()`（PostgreSQL 真行锁）。

### 新增 (Added)
- 新增 `scripts/smoke_test_concurrency.py`（23 项）并接入 CI：用真线程 + 真会话
  验证单次码/签到并发只能成功一次、积分不丢更新、邀请幂等、以及限流 IP 取值的三种优先级。
- `scripts/smoke_test_security_hardening.py` 的后台登录日志校验改为断言 `X-Real-IP` 优先。
- `docs/operations.md` 补充旧版 compose 栈现在需要 `POSTGRES_PASSWORD` 的说明与更换口令提醒。

## [2.6.13] - 2026-09-21

本次是**上线前的安全加固**：把匿名可用的 WebSocket 与通知入口关掉、修掉健康检查里两处恒错的假警报，
并把「Emby 服务入口」的配置真正接到用户侧，同时清掉两处数字 token 兜底、给管理员登录补上审计与限流。

### 安全 (Security)
- **WebSocket 不再允许匿名冒充**：`/ws/{user_id}` 现在必须携带 `?token=<access_token>`（或 `Authorization` 头），
  且 token 所属用户必须与路径中的 `user_id` 一致、账号为启用状态；不满足一律以 1008 关闭。
  此前任何人执行 `ws://host/ws/1` 就能接收 1 号用户的全部实时推送。
- **通知管理接口不再匿名可用**：`/api/notifications/send`、`/broadcast`、`/online-users` 现在仅管理员（is_staff）可访问。
  此前任何人都能向全站用户广播「系统通知」做站内钓鱼。
- **彻底移除两处数字 token 兜底**：删除门户端未被引用的 `get_current_user_compat`（`Bearer <user_id>` 即冒充用户），
  并移除 `EMBY_ALLOW_LEGACY_TOKENS` 开关与其背后的兼容分支。该环境变量自此不再有任何效果。

### 修复 (Fixed)
- **`/api/health/detailed` 不再恒报数据库故障**：`db.execute("SELECT 1")` 在 SQLAlchemy 2.0 下必须用 `text()` 包装，
  之前恒抛异常导致线上监控永远是红的（真挂了反而看不出来）。
- **健康检查不再恒报 Redis 故障**：改为读取 `backend.database` 的模块级 `redis_client`——
  `CacheManager` 上并没有该属性，之前每次检查都抛 `AttributeError`，导致整体状态永远是 `degraded`。
- **「Emby 服务入口」配置真正生效**：用户账号卡的服务器地址与一键导入 scheme 改为优先读配置
  （外部 Emby → `emby_external_url`，分离 EA → `emby_managed_url`，都没配才回退 `EMBY_PUBLIC_URL`），
  EM 的「请去连 EA」指引同样跟随配置；顺带修掉配置 URL 被整体 `.lower()` 导致路径大小写被改写的问题。
- **外部 Emby 模式下账号卡仍可读**：只读的 `GET /api/user/emby/server` 不再被 503 拦截，
  而是明确返回外部服务器地址、`external` 标记与「账号由对方管理」（不再提供本项目的一键导入 scheme），
  自建媒体库 / 扫描 / 挂载等写操作仍然一律 503。

### 新增 (Added)
- **管理员登录审计与限流**：后台登录失败、越权尝试、成功登录与触发限流一并写入登录日志
  （新增事件类型 `admin_login` / `admin_login_failed`，后台「登录与安全日志」可直接筛选），
  并按 IP 限流（1 分钟 8 次），与用户端登录同一口径。
- 新增 `scripts/smoke_test_security_hardening.py`（43 项断言）并接入 CI，覆盖 WebSocket 鉴权、通知接口鉴权、
  健康检查、入口配置驱动 `base_url`、数字 token 已失效、后台登录审计与限流。

## [2.6.12] - 2026-09-21

本次修复自建 Emby 播放会话端点的鉴权与会话隔离。

### 修复 (Fixed)
- **会话列表不再匿名泄露**：`GET /emby/Sessions` 与 `GET /Sessions` 现在要求有效的 Emby token；普通用户只看到自己的会话，管理员可查看全站会话
- **会话结束不再越权**：`DELETE /emby/Sessions/{key}` 与 `DELETE /Sessions/{key}` 现在要求鉴权，普通用户只能结束自己的会话，管理员可结束任意会话，不存在的会话返回 404
- **会话键不再可预测**：未提供或提供非法 / 被其他用户占用的 `PlaySessionId` 时改用随机键，避免根据用户 ID 与条目 ID 构造会话键后操纵他人播放
- 新增 `scripts/smoke_test_emby_sessions.py`，覆盖匿名 401、普通用户隔离、越权 403、管理员全量管理、随机会话键与进度上报回归，并接入 CI

## [2.6.11] - 2026-09-20

本次是**管理后台的推倒重做**：先把「按钮发白、字看不清」的根因修掉，再重做外壳、统一页面原语，
并把手机上最难受的几个页面从「宽表格」改成卡片列表。

### 修复 (Fixed)
- **按钮「发白、字看不见」的真正原因**：Element Plus 对**没有 type 的按钮**不会加 `.el-button--default` 类，
  因此它会退回组件默认的 `--el-button-bg-color: var(--el-fill-color-blank)`——而 EP 自己的默认值是 **`#fff`**，
  配上一版设的浅色文字，就是白底白字（截图里的「查询」「停止全部转码」「生成平台虚拟库」全是这个）。
  上一版只覆盖了 `.el-button--default`，所以永远没生效。现在从源头改：`--el-fill-color-blank` 换成主题表面色，
  并按「实心 / 描边 / 文字」三种形态重新给出**前景+背景配对**（亮底配深字、深底配白字），全部过 AA；
  同时修掉输入框、开关、标签、分页、弹层等一批 EP 默认值在深色主题下的残留浅色
- **字体看不清**：上一版把文字写成「半透明白」叠在深色上，小字号标签（如设置页的「积分/次」）
  实际对比度只有 2~3:1。现在文字**一律实色**并按 WCAG AA 挑选：正文 ≈16:1、次级 ≈11:1、
  标签 ≈7:1、说明文字 ≈4.9:1；基准字号 13→14px，并调整行高与字重
- **手机上表单标签依然右对齐**：Element Plus 用 `.el-form--label-right .el-form-item__label`（0,2,0）
  把标签推到右侧，上一版只写了 `.el-form-item__label`（0,1,0）根本压不住；现在按正确权重覆盖，
  并把主要表单改为 `label-position="top"`
- **各页面里写死的第三方灰度被清掉**：`#737373`、`#a3a3a3`、`#262626`、`#171717` 这些是别的模板的默认兜底值，
  既不属于本主题、对比度也不达标，统一换成主题令牌

### 变更 (Changed) — 外壳与控制台视觉重做
- **侧边栏**：品牌区 + **可折叠分组导航**（展开状态记在 localStorage，进入页面自动展开所在分组，
  组标题带箭头）+ 底部管理员区（修改密码 / 回到前台 / 退出登录 / 版本号）；
  ≤1024px 整体变抽屉（遮罩、锁背景滚动、Esc / 点遮罩 / 切路由关闭）
- **顶栏**：页面标题 + 分组小字，右侧刷新按钮与管理员菜单；窄屏只留头像
- **统一页面原语**：卡片、分组标题、统计瓦片、工具条 / 过滤器、徽标、空状态、键值行全局只定义一次，
  用 `.admin-layout .admin-content`（0,3,0）前缀压住各页面的 scoped 样式——以前 12 个页面各写一份 `.toolbar`、
  各写一套 `.mini-badge`，这是「一团乱」的另一个来源
- 新增品牌图标位与 `--sidebar-w/--header-h` 尺寸令牌，顶栏与侧边栏高度对齐

### 新增 (Added)
- **`DataTable` 组件：一套标记两种形态**——≥641px 渲染 `el-table`，≤640px 把每行渲染成一张卡片
  （`mobile: 'title'` 的列当标题，其余列变成「标签 / 值」两列，`key: 'actions'` 的列落到卡片底部操作区）。
  这样同一个页面的单元格逻辑只写一次，手机上不再需要横向拖动，也不会出现「操作按钮被固定列盖住文字」

### 移动端 (Mobile)
- 已迁移到 `DataTable`（手机即卡片列表）：**卡码管理**、**115 转存**（任务 + 账号两张表）、
  **邀请与积分**（邀请记录 + 积分流水）、**媒体库**（在线会话）、**系统设置**（表单改顶部标签）
- **媒体库卡片**：手机上「存储挂载 / 刮削策略 / 115 账号」改为标签在上、控件在下（此前中文标签被挤成竖排两行），
  路径允许换行显示；操作按钮拉满整行
- 行内操作统一为**描边按钮**（不再是几乎看不见的文字按钮），触屏下最小高度 32px；导航项与分组标题 ≥46px
- 登录页、弹窗、抽屉在小屏的边距与安全区（`env(safe-area-inset-*)`）一并复核

### 验证 (Verified)
- `admin_frontend`：`vue-tsc` 0 错误；`vite build` 通过，并在产物中确认新主题真的编入且**晚于** Element Plus 自身样式
  （`--el-fill-color-blank` 覆盖位于 367054 字节，EP 默认值位于 4260 字节；主色按钮的深色前景位于 369773 字节，
  EP 的白字默认值位于 37750 字节），即「白底白字」已从根上修掉
- `scripts/smoke_test_admin_v240.py`、`scripts/smoke_test_auth.py`、`scripts/deploy_check.py`（真起服务 + 真实 HTTP）全部通过，版本上报 2.6.11
- 说明：本环境没有可用浏览器，无法做像素级断言；已交付页面请实际用手机复核一次

### 待办
- 尚未迁移到卡片列表的页面（仍是桌面表格，手机横向滚动）：用户管理、商品与套餐、订单、兑换码、设备、
  登录日志、文件挂载、求片、公告、工单、操作日志、数据概览

## [2.6.10] - 2026-09-20

本次两件事：**管理后台在手机 / 平板上重新适配，并修掉三处让整个后台错位的根节点样式缺陷**；以及此前的工具链与 CI 变更。

### 界面 (UI)
- **布局错位的三个真正源头（都在 `admin_frontend/index.html` 的初始加载样式里）**：
  - `#app` 被写成 `display: flex` + `align-items/justify-content: center`：后台外壳 `.admin-layout` 因此被居中，超宽内容（宽表格）会被向两侧顶，**左边直接被顶出屏幕且无法滚到**；现只保留「铺满视口」
  - 同样的规则里 `#app { background: #0a0a0a }` 盖住了 body 的极光渐变，整个后台其实是铺在纯黑上而不是主题色上；现改为透明，渐变正常透出
  - 首屏「加载中…」是挂在 `#app.loading::before` 上的伪元素，而 `.loading` 类**没有任何代码会移除**，于是它作为 `#app` 的一个 flex 项**永久并排挂在界面左上/中侧**；现改为固定定位的全屏遮罩，并在 `main.ts` 挂载完成后移除类名
- **管理后台按断点重新适配**：
  - 侧边栏变抽屉的断点从 `<768px` 提前到 `≤1024px`——原来 768～1024px（平板、小窗笔记本）仍是挤压的桌面布局，侧边栏占掉 236px 后又把内容挤扁；抽屉支持遮罩、锁背景滚动、Esc / 点遮罩 / 切换路由关闭，收起时 `visibility: hidden` 避免焦点落在屏幕外的菜单项上
  - 汉堡按钮改为**只在窄屏出现**：此前桌面顶栏一直挂着一个点了没意义的按钮
  - 新增全局适配层 `admin_frontend/src/styles/responsive.css`（断点 ≤1024 / ≤768 / ≤640 与触屏 `pointer: coarse`）：页面头竖排、过滤器换行、模板里写死的控件宽度在手机上铺满整行、表单 label 上移（`label-width` 是 inline 宽度，窄屏留给输入框只剩百来像素）、表格字号与内边距收紧、dialog / drawer 收成 94vw、分页居中并允许换行、触控目标放大到 ≥44px
  - 页面级细节：Users 列表在手机上隐藏「注册时间」（详情抽屉里本就有）并收窄其余列与操作列，避免操作列被推到屏幕外；Dashboard 图表高度、坐标轴标签、双栏统计在窄屏做减法
  - `index.html` 补 `viewport-fit=cover` 与 `theme-color`，配合 `env(safe-area-inset-*)` 适配刘海屏与底部安全区（抽屉底部、内容区底部、登录页）

### 新增 (Added)
- **`scripts/create_admin.py`：一条命令造出管理后台账号**。新库里一个账号都没有，而第一个注册的用户**不会**被自动提升为管理员（有意的安全设计），以前只能手工改库 `web_users.is_staff` 或先注册再跑 `_e2e_mkstaff.py`：
  - 默认不带参数：创建（或升级）`admin` 并生成随机强密码后打印；`-u/-p` 指定账号密码
  - 不带 `-p` 且账号已存在时**重置为新的随机密码**（忘了密码时就用它），只升级权限加 `--no-password`
  - 幂等可重复执行；用户名 / 密码长度校验与门户注册同一口径；同时补齐 Emby 客户端凭据（门户密码即播放密码）；`--dry-run` 只看状态不写库
- `scripts/smoke_test_admin_account.py`（25 项断言）：建号、幂等、不改密码语义、重置密码、升级普通账号、Emby 凭据、真登录 `/api/admin/auth/login` 与 `/api/admin/auth/me`、非管理员 403、非法用户名 / 过短密码被拒；已接入 CI 的冒烟测试任务
- `docs/deploy-em.md`「首次登录与管理」与 README 最短部署路径改用新脚本：**明确指出 `scripts/reset_admin_password.py` 面向 v2.0 之前的旧拆分栈（`admin_backend.admin_users`），在 EM / EA 架构下用不了**——之前文档指向它，照着做会改错库

### 工具链 (CI)
- 新增 [`.github/workflows/ci.yml`](./.github/workflows/ci.yml)：push 到 `main` / PR / 手动触发时跑三组检查——
  - **前端 · user_frontend 与 admin_frontend**（矩阵）：`npm ci`（锁文件与 `package.json` 不同步即失败）→ `npm run type-check`（vue-tsc）→ `npm run build`；构建产物打成 tar 传给自检（`dist/` 在 `.gitignore` 里，直接上传目录可能被忽略规则过滤成空产物）
  - **后端 · 冒烟测试**：`scripts/smoke_test_auth.py`（门户认证端到端）与 `scripts/smoke_test_admin_v240.py`（管理端接口，含后台免登 / 非管理员被拒）。不依赖构建产物，所以单独成 job，改后端时更快拿到反馈
  - **后端 · 部署自检**：把上一步的 `dist` 还原到 EM **实际托管**的位置后跑 `scripts/deploy_check.py`——真起 uvicorn、真发 HTTP，覆盖单进程与 EM/EA 分离两套形态；自检用独立数据库（`ci.db`）与显式 `SECRET_KEY`，即一次「从零部署」
- 至此 PR 有了真正的状态检查：此前仓库没有任何 CI，`gh pr checks` 永远是「no checks reported」，门禁只能靠本地手跑
- **`main` 分支保护：这四个检查已设为必需状态检查**（`前端 · user_frontend` / `前端 · admin_frontend` / `后端 · 冒烟测试` / `后端 · 部署自检（真起服务 + 真发 HTTP）`）——检查未通过时 GitHub 直接拒绝合并（`BLOCKED`），全部通过才转为 `CLEAN`。未开启 `strict`（要求分支先与 `main` 同步）与 `enforce_admins`（仓库管理员仍可直接向 `main` 推送）
- `admin_frontend` 补上 `type-check` 脚本（`vue-tsc --noEmit -p tsconfig.json`），与用户端口径一致
- README 补 CI 徽章与「持续集成（CI）」小节（含与 CI 同序的本地复现命令）

### 修复 (Fixed)
- **管理后台整体错位 / 移动端仍是 PC 布局**：见上方「界面 (UI)」——根节点 flex 居中 + 常驻加载提示 + 不透明背景三处，加上侧边栏断点太靠后
- **`user_frontend/package-lock.json` 与 `package.json` 不同步**（锁文件里缺 `hls.js`、自身版本还停在 `0.0.0`）：`npm ci` 会直接报 `Missing: hls.js@1.7.3 from lock file` 而失败，CI 的锁文件门禁要求两者一致；现按 `package.json` 重新生成

## [2.6.9] - 2026-09-20

本次解决「管理员进了后台还要再输一遍账号」的重复动作：把门户与管理后台打通为**单点登录**。

### 新增 (Added)
- **门户免登（单点登录）**：管理后台与用户端本就同源、共用同一个 `SECRET_KEY` 与同一套 JWT（`get_current_admin` 只认 `is_staff` 的 `WebUser`），但两端各自存了一份 token（门户 `access_token` / 后台 `admin_access_token`），导致管理员在门户登录后打开 `/admin/` 还要再登录一次。现在后台在挂载路由前用门户会话**静默探测一次** `/api/admin/auth/me`：通过即接管该会话直接进入，**不再出现登录页**
  - 门户 access token 已过期时，自动用门户 `refresh_token` 换新后重试（轮换后的票据同时写回门户键，两个前端都不断线）；换不到才落到登录页
  - 探测失败给出可操作提示：门户账号不是管理员 → 「当前门户账号没有管理员权限，请使用管理员账号登录」；门户会话失效 → 「门户登录状态已失效，请重新登录」。探测只在有门户会话时发起，**不发无谓请求、不产生循环跳转**
  - 探测走裸 `axios`，不经过管理端拦截器，因此 401/403 不会触发全局报错与强制跳转
- **用户端新增「管理后台」入口**：个人中心「安全设置」卡片对 `is_staff` 账号显示入口，一次登录即可从门户直达后台
- `GET /api/user/auth/me`（及登录 / 注册 / 刷新返回的 `user`）新增 `is_staff` 字段，前端据此判断是否展示后台入口

### 变更 (Changed)
- 后台「退出登录」是真退出：主动退出后**本标签页内**不再用门户会话自动免登（否则点了退出会被立刻登回去），关闭页面或重新在门户登录后恢复
- `docs/deploy-em.md`「首次登录与管理」补充免登行为说明
- 版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 2.6.10

### 验证 (Verified)
- `scripts/smoke_test_admin_v240.py` 新增 3 项断言（门户 token 直接通过 `/api/admin/auth/me`、门户返回 `is_staff`、非管理员门户 token 被拒 403），全量通过
- `scripts/smoke_test_auth.py` 门户认证端到端回归通过；`vue-tsc` 双前端无新增类型错误

## [2.6.8] - 2026-09-20

本次做了一次**真实部署自检**（真起 uvicorn + 真发 HTTP），并修掉它发现的一处客户端兼容缺口。

### 新增 (Added)
- **`scripts/deploy_check.py`（部署自检）**：不依赖 TestClient，真的跑一遍 `python serve.py` 再发 HTTP：
  依赖 / 数据库 / 静态产物预检 → 冷启动就绪（超时会打出服务日志）→ 用户端与管理端首页 →
  Emby 协议面 → 登录与权限（错密码、未带 token）→ 挂载类型表（含 rclone）→ 挂载 CRUD 与目录浏览 →
  媒体库绑定挂载并触发扫描 → 条目真的入库 → 收尾清理与优雅退出。退出码 0 即通过；
  `--port` / `--timeout` / `--keep` 可选。共 46 项断言。
- **分离部署（推荐产线形态）也进自检**：同一库里再起一套 `ENABLE_EMBY_GATEWAY=false` 的 EM
  与独立 `serve_emby.py`，验证 EM 交出协议面并给出「去连 EA」404 指引、EA 上报
  `service=ea` 且 `paired_with_em=true`、EA 的 `/emby/*` 与裸根路径都可用；
  最关键的一步是**跨服务配对**——客户端在 EA 上用 EM 写的账号 `AuthenticateByName` 拿到
  token，再用该 token 读到同一用户（`Bearer` 与 `X-Emby-Token` 两种形式都验）。
- **配对闸门进自检（负向断言）**：不设 `SECRET_KEY` 时 EA 必须拒绝启动并打出提示——
  自检会真的跑一次，确认它没「带病起来」。
- **构建产物新鲜度检查**：EM 实际托管的是 `user_frontend/dist` 与 `admin_frontend/dist`
  （可用 `FRONTEND_DIST` / `ADMIN_DIST` 覆盖），自检按这两个目录判定存在性，
  并在「产物比 `src/` 旧」时警告——否则会部署出一套旧界面而代码里看不出来。

### 修复 (Fixed)
- **`/emby/System/Info/Public` 返回 404（客户端发现服务的第一个请求）**：此前只注册了首字母大写的
  裸路径 `/System/Info/Public` 与全小写的 `/emby/system/info/public`，客户端按 Emby 文档拼
  `/emby/System/Info/Public` 时会 404。现在三种写法都注册，`System/Ping` 与
  `Branding/Configuration` 同理；`smoke_test_ea_split.py` 补上了对应断言。
- 冒烟测试的计数断言改为按**本测试独有的路径前缀**统计，并在开跑前清理孤儿条目：
  开发库共用一个 SQLite，删除后主键会被复用，按库 id / 全局计数会被其它测试的残留行干扰。
- 自检用的账号现在同时建好**自建 Emby 凭据**（`emby_username` / `emby_password`）：
  客户端认证走的是这两个字段，只给门户密码时 EA 会（正确地）返回 401。

### 变更 (Changed)
- 自检不需要显式配置即可跑（自造 `SECRET_KEY` 并给出 `WARN`），但**生产必须显式设置**：
  不设时 EM 用临时随机密钥，EA 会直接拒绝启动。
- 版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 2.6.8

## [2.6.7] - 2026-09-20

本次在存储挂载里新增 **rclone 挂载**：直接复用 rclone 的 remote，把 rclone 支持的
所有后端（Google Drive / OneDrive / S3 / 115 / 夸克 / WebDAV / SFTP …）接进媒体库，
**不需要把网盘挂到本机**，配置也不用在面板里再抄一遍密钥。

### 新增 (Added)
- **`rclone` 挂载类型**（第十种），两种模式：
  - **`rc`（推荐）**：连正在运行的 `rclone rcd --rc-serve`——列目录 / 测试走 RC API
    （`/operations/list`、`/config/listremotes`），播放地址直接用 rc-serve 暴露的
    `http://<RC 地址>/<remote:path>`，rclone 自己处理 Range，EA 照旧代理转发；
    RC 密码（`rc_user` / `rc_pass`）与 rc 地址都不下发客户端，并支持 Basic 认证
  - **`cli`（兜底）**：直接调 rclone 命令（`lsjson` 列目录、`cat` 读 `.strm` 与字幕、
    `link` 取公开直链）；后端不支持公开链接时给出可操作提示，而不是默默播不了
- **后台「获取 remote 列表」**：用表单里尚未保存的 RC 地址 / 路径去问远端有哪些 remote，
  选完可继续补子目录（如 `gdrive:Movies`）；新增 `GET /api/admin/emby/mounts/rclone/remotes`
- 类型元数据新增 `remotes` 标记与 `rclone_fs` 字段类型，前端据此渲染 remote 选择器
- 新增配置项：`MOUNT_RCLONE_BIN` / `MOUNT_RCLONE_CONFIG` / `MOUNT_RCLONE_RC_URL`

### 变更 (Changed)
- 扩展挂载类型改为**模块导入即注册**（`mount_cloud` / `mount_rclone`）：无论先导入哪个模块，
  类型表与提供者都是完整的；新增类型只需一个模块 + 一次导入
- 命令模式下的 rclone 路径 / 配置文件（含「获取 remote 列表」接口）会从表单值完整透传，
  支持非 PATH 安装（如 `/opt/rclone/rclone`）
- 挂载冒烟测试扩到 170 项（新增 rclone 的 rc / cli 两组）
- 版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 2.6.7

### 修复 (Fixed)
- **115 任务状态目录不再残留临时文件**：原子落盘（临时文件 + `os.replace`）在进程被强杀时会
  留下 `.tmp`；现在 EM 启动时与后续落盘会清理超过 `PAN115_STALE_TMP_SECONDS`（默认 900 秒）的残留，
  正在写入的文件不受影响
- 冒烟测试不再误报：挂载测试开跑前先清掉上一轮崩溃留下的残留，「同一目录不重复入库」
  改为按本测试的媒体库计数（全局计数会被其它测试遗留的孤儿行干扰）；115 测试只断言
  「超过清理阈值」的临时文件，不再把原子写入的瞬时文件当成残留

## [2.6.6] - 2026-09-20

本次落地「存储挂载」：**挂载就是把内容接进媒体库的一种方式**。媒体库通过「绑定挂载」引用它，
同一个挂载可以被多个库共用；115 直挂只是其中一种，另外八种是同类能力。

### 新增 (Added)
- **九种挂载类型**（`MOUNT_TYPES`），统一落到同一套扫描 / 播放 / 字幕链路：
  | 类型 | 说明 |
  | --- | --- |
  | `local` | 本机目录（rclone / CloudDrive2 / SMB / NFS 挂到本机后的目录） |
  | `strm` | STRM 直链目录（本地只放 `.strm` 小文件，内容是播放直链） |
  | `115` | 115 网盘直挂（Cookie 型 API，不依赖 115 OpenAPI） |
  | `webdav` | 群晖 / Nextcloud / 自建 WebDAV |
  | `alist` | AList / OpenList（一个挂载聚合多种网盘） |
  | `s3` | S3 兼容对象存储（AWS S3 / MinIO / Cloudflare R2 / Backblaze） |
  | `aliyun` | 阿里云盘（Open API，refresh_token 换 access_token） |
  | `quark` | 夸克网盘（Cookie 型 API） |
  | `onedrive` | OneDrive / SharePoint（Microsoft Graph，按路径寻址） |
- **虚拟路径 `mount://<挂载 id>/<相对路径>`**：远程来源的条目入库为虚拟路径，播放 / 探测时才由提供者解析成真实直链，再由 EA 按 Range **代理转发**——Cookie、令牌、预签名参数都不下发到客户端，条目也不会因为客户端直链过期而失效
- **扫描安全网**：来源不可用（目录不存在 / 挂载停用 / 凭据失效 / 网络不通）时记入 `failed_roots` 并**跳过清理阶段**，不会把「读不到」当成「文件已删除」批量误删条目；已停用挂载的条目在存在性判断里同样视为不可用
- **媒体库可绑定多个挂载**（`emby_libraries.mount_ids`），路径与挂载可以混合使用；绑定已停用的挂载会被拒绝，删除挂载会自动解绑并返回解绑数量
- **类型元数据驱动一切**：后台下拉、表单字段、必填校验、密钥脱敏、目录浏览入口都读同一份类型元数据（`group` / `browse` / `root_key` / `fields[*].required|secret|type`），新增类型只改注册表，不用改前端
- **S3 用 SigV4 预签名直链**（纯标准库实现，不引入 boto3）：列目录走 `ListObjectsV2` + `delimiter=/`（支持子前缀当挂载根），取对象走预签名 GET；支持 path-style 与 virtual-hosted 两种寻址，也支持 Session Token
- **阿里云盘自动换票**：`refresh_token` 换 `access_token` 并在实例内复用，`drive_id` 未填时取默认网盘；填了 `client_id` / `client_secret` 时走开放平台换票接口
- **OneDrive 按路径寻址**：直接请求 `/root:/目录/子目录:/children`，不需要缓存 file id；取文件用 Graph `/content` 的 302 预授权地址
- **目录浏览回写挂载根**：后台可浏览远程目录，点目录即把「根目录标识」写回表单（115 的 `cid`、S3 的 `prefix`、网盘的目录 ID、OneDrive 的路径）
- 新增冒烟测试 `scripts/smoke_test_mounts.py`（141 项断言）：类型注册表与必填 / 脱敏规则、虚拟路径、本机与远程与云端四组挂载、扫描集成、播放与 Range 代理、字幕钩子、管理端 API

### 变更 (Changed)
- 后台新增「存储挂载」页（内容分组）：类型卡片、挂载列表（启用开关 / 测试结果 / 绑定数）、新建与编辑、目录浏览；媒体库页可为每个库绑定多个挂载
- 媒体库保存时要求「至少一个路径或一个挂载」；挂载配置变更后返回 `rescan_required`（扫描使用固定配置快照）
- `env.example` 新增挂载段：`MOUNT_TIMEOUT` / `MOUNT_UA` / `MOUNT_S3_REGION` / `MOUNT_ALIYUN_ENDPOINT` / `MOUNT_ALIYUN_AUTH_URL` / `MOUNT_QUARK_BASE` / `MOUNT_QUARK_UA` / `MOUNT_GRAPH_BASE` / `MOUNT_GRAPH_TOKEN_URL`
- 版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 2.6.6

### 修复 (Fixed)
- **远程挂载里的 `.strm` 现在按内容解析**：以前会把这个小文本文件本身当成媒体交给客户端（表现为「能扫到、一播就报不支持」）。现在任何挂载里的 `.strm` 都先读内容再当直链播，扫描阶段还能从直链推断真实容器（推断不出来不阻断入库）
- 子目录读不到时只记日志并跳过，不再让一个没权限的目录拖垮整个媒体库的扫描

## [2.6.5] - 2026-09-20

本次落地「115 下载与转存」：分享链接 → 转存 / 取下载地址 → 整理扫描入库。

### 新增 (Added)
- **115 账号配置档（Cookie 型，不依赖 115 OpenAPI）**
  - 支持命名多账号与默认账号；**媒体库可单独绑定账号**（`emby_libraries.account_115_id`），未绑定时回退默认账号，再回退服务器级 `PAN115_COOKIE` —— 历史部署里只在 .env 放一个 Cookie 的写法继续可用
  - 绑定账号被停用或删除时自动回退到默认账号（删除配置档会同时解除媒体库绑定，不留悬空引用）
  - 后台可即时校验 Cookie（未保存的也可以先测再存）；**Cookie 不回传明文**，列表只给长度与尾部片段
- **115 分享链接转存 / 取下载地址任务**
  - 支持 `https://115.com/s/xxxx?password=abcd`、`...#abcd`、「链接 + 提取码：abcd」整段口令、以及纯分享码
  - 目标目录可直接浏览 115 网盘再选：路径浏览按「表单 Cookie → 账号配置档 → 已保存 Cookie」解析，**刚粘贴还没保存的 Cookie 也能直接用来浏览**
  - 完成后按绑定媒体库**触发一次扫描**，进入统一整理与入库流程；任务与扫描都用独立数据库 Session
- **任务按文件原子持久化**：分享快照条目、已完成文件键、下载地址同时写入共享数据库与 `PAN115_STATE_DIR` 下的 JSON 文件，写文件走「临时文件 → fsync → os.replace」，进程在任意时刻被杀都读到完整状态
- **任务可续跑**：启动时把 `running`（上次进程被杀）拉回 `pending` 重新入队，已完成文件靠 `payload.done_keys` 跳过，**不会重复转存**；分享快照已落库时不再重复拉取
- **Cookie 失效不丢任务**：鉴权类错误置 `waiting_auth` 并保留任务，修好账号后重试即可从断点继续；未配置 Cookie 同样保留而不是直接失败
- 重复提交同一分享链接时合并到既有未完成任务，不重复排队
- 新增冒烟测试 `scripts/smoke_test_115.py`（64 项断言）：链接解析、Cookie 优先级与降级、账号 API、任务生命周期、原子落盘、续跑去重、Cookie 失效恢复、null 归一化

### 变更 (Changed)
- 后台新增「115 转存」页（内容分组）：转存任务列表（状态 / 进度 / 分享内容 / 下载地址展开）+ 账号配置档管理；媒体库页可直接给每个库绑定 115 账号
- `env.example` 新增 115 段：`PAN115_COOKIE` / `PAN115_STATE_DIR` / `PAN115_WEBAPI_BASE` / `PAN115_SHARE_BASE` / `PAN115_TIMEOUT`
- 版本号：后端 / EA / 用户端 / 管理端 / 后台顶栏 2.6.5

### 修复 (Fixed)
- **115 任务响应里的 `items` / `urls` 永远序列化为数组**：历史上 `items/urls` 为 `null` 的响应会把前端转存记录页整体打崩
- 转存任务使用独立 Session：请求级 Session 在请求结束后会被关闭，复用会报 `transaction is closed`，也会与扫描线程抢 SQLite 写锁

## [2.6.4] - 2026-09-20

本次落地「媒体库 / 搜索与刮削 / 图片与字幕」一组能力。

### 新增 (Added)
- **按发行平台自动生成虚拟媒体库**：识别片名/目录里的发行组标签（NF / DSNP / ATVP / AMZN / HMAX / HULU / PMTP / PCOK / CR …），`POST /api/admin/emby/libraries/virtual` 可为 Netflix / Disney+ / Apple TV+ / Prime Video / Max / Hulu / Paramount+ / Peacock / Crunchyroll 生成虚拟库
  - 虚拟库没有自己的目录，是**跨库的平台视图**：条目仍归属原媒体库，打开虚拟库时按 `emby_items.platforms` 聚合，客户端看到的与普通媒体库一致（`CollectionType=mixed`）
  - **可按实例开关**：`ENABLE_VIRTUAL_LIBRARIES=false`（EA 可独立配置）或单个库停用时，虚拟库既不出现在客户端媒体库列表，用 guid 直达也返回 404
  - 未识别到平台标签时不会凭空建库（`POST` 不带 `platforms` 时按库里实际出现过的标签生成，可选 `prune` 清理空库）
- **媒体库刮削策略**：`missing_only`（仅缺失时刮削，默认）/ `3m` / `6m` / `1y` / `all`，后台媒体库卡片与新建弹窗均可直接选择；「缺元数据的条目」在任何策略下都会补刮，不会因为窗口未到而永远空着
- **多密钥轮询**：`TMDB_API_KEYS=key1,key2,key3` 逗号分隔，某个密钥超配额（429）或无效（401）时自动轮到下一个，整库刮削不会因为一个 key 限额就停滞
- **IMDb Id 与多别名落库**：条目补齐 `imdb_id` 与 `aliases`（TMDB 搜索命中的原名 + 详情接口的 alternative titles），并写入客户端可见的 `ProviderIds.Imdb`
- **图片丢失自动排队修复**：数据库里有图片记录但本地文件已丢（换盘 / 迁移 / 挂载掉线）或远程图失效时，图片接口返回干净的 **404**（旧实现会把异常抛成 5xx，客户端当成鉴权/服务器故障反复重试），同时把条目排进修复队列，下一轮扫描换成 TMDB 远程图
- 后台新增 `GET /api/admin/emby/libraries/repair/queue`（待修复条目）与 `POST /api/admin/emby/libraries/repair/run`（立即修复）；媒体库页顶部会在有缺图时出现「修复缺图 (N)」按钮
- **季 / 集图片回退**：集无图 → 季图片 → 剧集海报，不再出现整库空白集图
- 新增冒烟测试 `scripts/smoke_test_media_search.py`（93 项断言）：命名识别、平台识别、字幕 sidecar、刮削策略、探测缓存、搜索排序、扫描安全网、虚拟媒体库与协议接口

### 变更 (Changed)
- **搜索重新排序**：`/Users/{id}/Items?SearchTerm=` 与 `/Search/Hints` 统一改为相关度排序——**标题完全匹配 > 标题前缀 > 词边界前缀 > 标题包含 > 别名 > 分类元数据 > 模糊**，旧实现只是 SQL `LIKE` 过滤后按名称排序，精确命中的标题会被「名字里恰好也含这几个字」的条目挤到后面
- **中英文 / 繁简体 / 多别名匹配**：SQL 预筛同时覆盖 `name` / `original_title` / `sort_name` / `aliases`，并把「繁→简」「简→繁」「去掉发布标签」的变体一起放进去（依赖 `zhconv`，未安装时自动降级为不转换，不影响搜索可用性）
- 搜索接口迁到 `backend/emby_server/search_api.py`（`api.py` 已近 2000 行），并在 EM / EA 中**先于** `emby_router` 注册，保证该实现优先生效
- **类型 / 制作公司 / 年份点进去能筛选**：客户端点开这些虚拟条目后会带着我们生成的 Id 请求 `/Items`，旧实现找不到条目就退回按 guid 匹配（等于空集或整库）。现在能反查合成 Id（含 `GenreIds` / `StudioIds` / `Years` 参数）并返回真实筛选结果
- **扫描任务使用固定配置快照**：任务只认触发时拍下的那份路径/策略，运行中改配置不会「一半旧一半新」；保存后重新触发即用新路径，**旧路径不会被继续扫描**
- 同一媒体库**重复扫描会被拒绝**（进程内互斥，`409`）；数据库里的 `is_scanning` 仅作展示，进程崩溃不会把库永久卡在「扫描中」
- 扫描并发与开销：已探测过的文件不再重复 `ffprobe`（除非文件大小变了），外挂字幕与视频探测解耦（换字幕不需要重探视频）
- 后台媒体库接口返回 `scrape_policy` / `is_virtual` / `platform`，并附带可选策略列表；`PUT /libraries/{id}` 返回 `rescan_required` 提示需重新扫描才生效

### 修复 (Fixed)
- **整季包被当成电影**：`S09`、`Season 9`、`第九季`、`第9季` 这类**只有季号**的命名旧实现认不出季（裸 `S09` 仅剧集库启用，避免 `S1m0ne` 被当成第 1 季），季包现在会正确建为剧集+季
- **每集都会发 TMDB 搜索**（浪费配额并污染 `tmdb_id` / 简介 / 图片，且让「只补缺」策略失效）：刮削现在只发生在电影/剧集这类顶层条目，集通过季/剧集继承元数据与图片
- **片名里的点分隔符没有转成空格**：`CLEAN_RE` 的正则把引号写在了字符类外，导致 `Rick.and.Morty` 原样入库；同时质量标签识别补上了被空格分隔的形式（`WEB DL`）
- **发行平台标签混进片名**：`Alpha.Target.2024.1080p.NF.WEB-DL` 曾入库为 `Alpha Target  NF`，现在会把非首词的平台标签一并去掉
- **目录列表不完整时禁止清理**：根目录不可用或遍历报错时跳过清理阶段，不再把「读取失败」当成「文件已删除」而误删整库记录
- **外挂字幕识别过窄**：旧实现只认「与视频完全同名」或「同名 + 点后缀」，现在兼容较短字幕名（`Show.S01E01.ass`）、发行组差异、多版本媒体（`1080p` 与 `2160p` 各配自己的字幕）、rclone/GD sidecar（`Show.S01E01.mkv.zh.srt`）与中文标注，且不会把同目录**别的集**的字幕认给本集
- 字幕语言识别改为标签表（`chs` / `cht` / `简体` / `双语` / sidecar 后缀等）并按 Emby 三字码上报

## [2.6.3] - 2026-09-20

### 新增 (Added)
- **EM / EA 分离部署**：把「运营面板」与「Emby API 后端」拆成两个可独立部署的服务
  - **EA（新增 `emby_api/` + `serve_emby.py`）**：独立的 Emby 协议网关，客户端直连它。协议面同时提供 `/emby/*` 与**裸根路径**（`/System/Info`、`/Users/AuthenticateByName` …），因此 EA 独占一个地址；直连流 / HLS 转码 / 字幕投递 / 图片 / 进度上报全在 EA 侧，`/metrics` 与 `/api/health` 独立可观测
  - **EA 不能脱离 EM 单独运行**：启动时硬校验「共享 `SECRET_KEY`」与「共享库里存在 EM 建的表（`web_users` / `emby_libraries` / `emby_api_tokens`）」，不满足即拒绝启动并给出可执行提示；配了 `EM_PANEL_URL` 则额外探测 EM 可达性（仅告警——EM 重启不应禁断正在播放的会话）
  - **EM 仍是唯一事实来源**（用户 / 套餐 / 订阅 / 邀请 / 卡码 / 风控 / 媒体库），新增 `ENABLE_EMBY_GATEWAY` 开关：置 `false` 时 EM 不再提供协议面，并对误连的客户端返回明确的「请连 EA」404 指引（按 `emby_router` 实际声明的全部裸根路径注册，以后新增协议路由不会漏）
  - 分离部署下**前端无需改动**：网页播放器仍走同源 `/emby/*`，由 EM 的 Nginx 反代到 EA，不引入跳域
- `env.example` 新增分离部署配置段：`ENABLE_EMBY_GATEWAY` / `EMBY_API_PORT` / `EMBY_API_PUBLIC_URL` / `EM_PANEL_URL`
- 新增冒烟测试 `scripts/smoke_test_ea_split.py`：15 项断言覆盖配对闸门、EA 协议面（含裸根路径）、EA 健康上报、EM 默认模式行为不变、EM 分离模式的客户端指引与 SPA 不受影响

### 变更 (Changed)
- 文档中心按 EM / EA 重编排：新增 `docs/deploy-em.md`（面板：域名、前端构建、运营配置）与 `docs/deploy-ea.md`（网关：配对硬依赖、独立地址、客户端接入、限流与设备控制）；原 `deploy-server.md` / `deploy-web.md` 的内容并入这两篇（故不再单独存在）
- 版本号：后端 / 用户端 / 管理端 / EA 2.6.3（`backend/main.py`、`emby_api/main.py`、`user_frontend/package.json`、`admin_frontend/package.json` 与后台顶栏 `APP_VERSION` 四处同步）

### 修复 (Fixed)
- `backend/main.py` 的 FastAPI `version` 字段此前停留在 2.6.0、与 CHANGELOG 不一致，现同步

## [2.6.2] - 2026-09-20

### 文档 (Docs)
- **新增文档中心 `docs/`**（对标成熟媒体站项目的文档中心结构），共 4 篇（其中 `deploy-server.md` / `deploy-web.md` 已在 2.6.3 并入 EM / EA 两篇）：
  - [`docs/README.md`](docs/README.md)：文档中心索引——架构一图、按场景的阅读路径、为什么统一后端必须单进程
  - `docs/deploy-server.md`：服务端部署——环境要求、`env.example` 逐项说明、启动与 systemd 常驻、媒体库创建与扫描、Emby 客户端接入、转码与直连、监控
  - `docs/deploy-web.md`：门户与管理后台部署——两个前端的构建与固定产物路径、静态托管、同源约束、Nginx + HTTPS 最小可用配置、首次登录与验证清单
  - [`docs/operations.md`](docs/operations.md)：上线检查清单、安全基线（含风险最高的三项配置）、SQLite / PostgreSQL 备份与恢复、常见问题排错
- README「部署」章节改为指向文档中心并保留最短部署路径；同步修正原描述——自带的一键脚本（`deploy.sh` / `docker-compose.yml`）面向 v2.0 之前的拆分栈，统一后端的部署入口是 `serve.py` 单进程
- 文档中明确 Freebuff Hosting 只构建 React（Vite + React / Next.js / CRA），与本项目 Vue + Python 的形态不兼容，替代路径为服务器部署；并提示仓库内提交的 `nginx/ssl/` 证书不要直接用于生产
- 本版为**纯文档变更**，不涉及代码，应用版本号保持用户端 2.6.1

## [2.6.1] - 2026-09-20

### 变更 (Changed)
- **钱包核销入口二合一**：原先「充值积分」页顶部的「兑换码」与「购买订阅」页里的「会员卡码」是两个各自独立的输入框，用户拿到一串码得先自己判断该填哪一个，填错只会得到一句报错。现收敛为**一个**「卡码 · 兑换码」入口：提交后先走 `POST /api/user/membership/redeem/preview` 自动识别来源——会员卡码展示类型与天数、确认后再核销，兑换码直接走积分 / 订阅核销，邀请码则提示去注册页使用
- **订阅页的卡码表单下线**：只留一行虚线指引（点击把焦点交回顶部唯一入口），既消除了「两个入口该用哪个」的困惑，又保留可发现性
- 版本号：用户端 2.6.1

### 文档 (Docs)
- README 新增「部署」章节：说明 Freebuff Hosting 只构建 React（Vite + React / Next.js / CRA），与本项目 Vue + Python 的形态不兼容，并给出项目自带的 Docker Compose 部署路径

## [2.6.0] - 2026-09-20

### 新增 (Added)
- **卡码体系**（新增 `backend/codes.py`）：注册码 / 续期码 / 白名单码三类卡码，含**诱饵码**（蜜罐：在盗版渠道流通，使用即自动封禁账号并落安全日志）与**指名码**（仅限指定账号使用）；核销入口 `POST /api/user/membership/redeem`（含预检 `preview`，前端先确认类型与天数再核销），注册页凭码注册同样支持类型化授予
- **卡码管理台**：`POST /api/admin/registration-codes/generate`（类型/天数/次数/有效期/随机算法/诱饵/指名）、`GET /registration-codes/stats`（按类型统计 + 诱饵命中 + 累计授予天数）、`GET /registration-codes/list`（类型/状态/关键字筛选）、`PATCH`（停用/备注）与 `DELETE`（未使用的可回收，已核销的保留审计）
- **设备风控**（新增 `backend/devices.py`）：客户端 `AuthenticateByName` 登录即登记设备（名称/客户端/版本/IP/首末次出现），支持**每用户设备上限**（`device_limit_per_user`）与**超限自动踢最久未使用设备**（`device_limit_auto_evict`）；管理端 `GET /api/admin/devices`·`/devices/stats`、`PUT /devices/{id}`（封禁/解封，封禁同时吊销令牌）、`DELETE /devices/{id}`（踢下线）；用户端 `GET /api/user/emby/devices` 与 `DELETE /api/user/emby/devices/{id}` 自助清理
- **登录与安全日志**（新增 `backend/authlog.py` + `LoginLog` 表）：记录门户/客户端登录成功与失败、设备超限被拒、诱饵码触发封禁等事件；管理端 `GET /api/admin/login-logs`（用户名/IP/事件/结果筛选 + 24h 汇总）与 `POST /login-logs/purge`（按保留天数清理，`login_log_retention_days` 可配）
- **下载策略**：站点级 `allow_download` 开关，关闭后客户端下载与拉取一致被拦（管理员不受限）
- 管理端新增「卡码管理 / 设备管理 / 登录与安全日志」页面与侧栏分组；用户端新增钱包「会员卡码」核销面板与个人中心「我的设备」卡

### 修复 (Fixed)
- **`X-Emby-Authorization` 解析丢失客户端名**：首个字段带着认证方案前缀（`MediaBrowser Client="Infuse"`），原实现把 `MediaBrowser Client` 整体当作键名，导致 `Client` 永远取不到、设备审查里所有客户端都显示成默认名。现剥离方案前缀
- **`/Items/{id}/File` 可绕过下载开关**：该路径与 `/Download` 等价但只校验会员身份，站点关闭下载仍能直接拉文件；现由网关级中间件按路径兜底（覆盖 `/Download`、`/Items/{id}/File` 及未来新增的下载类路径）
- **白名单卡码天数语义不一致**：生成时写入 36500 天而展示/授予口径认 `-1` 为永久，导致前端显示「36500 天」；现统一为 `-1`
- **设备封禁未被强制执行**：管理端封禁设备后该设备仍可登录，现于设备登记阶段直接拒绝并提示

### 变更 (Changed)
- 版本号：后端 / 用户端 / 管理端 2.6.0

## [2.5.5] - 2026-09-20

### 新增 (Added)
- **Emby 网关客户端兼容补齐**（对照 Emby 4.7 官方 API 面，新增 40+ 端点）：`Search/Hints` 全局搜索、`Genres`/`Studios`/`Persons` 分类元数据、`Items/Counts` 与 `Items/Filters` 统计筛选、`FavoriteItems` POST/DELETE 规范收藏路由、`Items/{id}/Similar` 相似推荐、`Items/{id}/Ancestors` 祖先链路、`Users/Public`·`Users/Me`·`Users/{id}/Policy` 用户面、`Sessions/Logout`、`System/Endpoint`、`Library/MediaFolders`、`UserViews`、`Videos/ActiveEncodings` 释放转码、`Library/Refresh` 库刷新、`PlaybackInfo` 的 GET 与用户维度变体
- **字幕投递**（新增 `backend/emby_server/subtitles.py`）：`/Videos/{id}/{MediaSourceId}/Subtitles/{Index}/Stream.{Format}` 全路径族（含 `{StartPositionTicks}` 变体）；外挂字幕直出并做中文编码嗅探（UTF-8/GB18030/Big5），内封文本字幕用 ffmpeg 抽取为 WebVTT 并按「条目+轨道」缓存；图片类字幕（PGS/VobSub）明确返回 415
- 用户端网页播放器接入字幕轨：自动挂载服务端标记的默认文本字幕

### 修复 (Fixed)
- **网页端 HLS 播放实际不可用**：master playlist 派生的变体与切片地址只带 `session` 票据、不带 `api_key`，而 hls.js 不会给子请求附加认证头，导致变体与切片全部 401。现变体与切片地址自带 `api_key`
- **外挂字幕永远拉不到**：扫描器写入外挂字幕轨时未设置 `stream_index`（为 `None`），客户端据此拼出 `Subtitles/None`；现按已探测轨道序号顺延分配
- **`/Items/{id}/Images/Backdrop/0` 直接 404**：图片路由只注册了 `Primary/{index}`，其它类型带序号地址无路由；现改为通用 `{Type}/{Index}`
- **列表筛选参数被忽略**：`Filters=IsFavorite|IsPlayed|IsUnplayed|IsResumable`、`Ids` 批量查询、`SortBy=Random` 此前不生效（筛选后返回全量），现已支持
- **条目详情缺 `RunTimeTicks` / `Container`**：客户端无法展示时长与进度条，现已补齐
- **HLS 切片过早 404**：客户端请求切片往往早于 ffmpeg 写出，现短暂等待（播放列表 15s / 切片 12s）并在 ffmpeg 已退出时明确返回 503 而非空列表
- **转码进程与目录泄漏**：同一用户重复请求同一影片会反复 fork ffmpeg，且客户端异常断开不会回收；现复用进行中的会话并回收失效/超龄会话
- **同步能力如实上报**：`SupportsSynchronization` 由 `true` 改为 `false`（未实现 `/Sync/*`），避免客户端发起无法完成的离线同步
- **`/Users/Public` 不泄露账号**：未认证请求返回空列表（Jellyfin 默认隐私策略），强制客户端手动输入用户名

### 变更 (Changed)
- 版本号：后端 / 用户端 2.5.5

## [2.5.4] - 2026-09-20

### 变更 (Changed)
- **首页 Hero 主行动降级为文字级快捷入口**：原本的通栏「进入媒体库」大按钮与会员卡的「立即开通会员」在同一屏抢视觉，且媒体库已在顶栏与底部导航各有一个入口。现改为 13px 文字链接（`进入媒体库 ›` ｜ `继续观看《片名》›`），hover 才点亮主色，把首屏主 CTA 让给会员开通
- **会员卡内 CTA 去重**：未开通时卡内右上角的「开通 ›」文字链与下方通栏按钮重复，现仅在**已开通**时显示「续费 ›」，未开通时只保留唯一的「立即开通会员」
- 首页随之清理不再使用的 `.btn / .btn-primary / .btn-ghost` 样式与 `Play` 图标引用（无死代码残留）
- 版本号：后端 / 用户端 2.5.4

## [2.5.3] - 2026-09-20

### 修复 (Fixed)
- **首页与个人中心的 Emby 账号信息重复**：首页「站点与设备」整块（服务器地址 / 用户名 / 密码 / 复制 / 一键导入）与个人中心的「Emby 账号」卡几乎完全一致，信息重复且两个入口的行为略有不一致。现将凭据与一键导入**收归个人中心**（为个人中心补齐了原本只在首页有的「一键导入到客户端」按钮），首页只保留一行指引入口（标题 + 一句说明 + 跳转）
- 首页随之清理了不再需要的账号卡请求与复制逻辑（少一次请求、少一套重复状态）

### 变更 (Changed)
- 首页分组标题由「站点与设备」改为「站点与账号」，与内容（不再携带凭据）保持一致
- 版本号：后端 / 用户端 2.5.3

## [2.5.2] - 2026-09-20

### 新增 (Added)
- **付费墙（会员门禁）**
  - 新增 `backend/subscriptions.py` 作为订阅判定与门禁的单一事实来源：`has_active_subscription` / `subscription_required` / `can_play` / `ensure_playback_allowed`
  - 开关于 `SystemConfig`：`subscription_required`（布尔，默认开启）、`subscription_gate_message`（自定义拦截文案）
  - 门禁覆盖：`PlaybackInfo`（不发放播放地址）、`/emby/Videos/{id}/stream`（直连拉流）、HLS 转码会话创建（`master.m3u8` / `hls1`，先于 ffmpeg 检查）、`/emby/Items/{id}/Download`（下载）—— 网页端与 Infuse 等外部客户端同一门槛，无法绕过
  - 管理员（`is_staff`）始终放行；关闭开关后全员放行
- **用户端付费墙体验**
  - 播放页在 403 拦截时展示付费墙卡片（权益说明 + 「开通会员」CTA + 返回详情），而不是抛一个播放错误
  - 详情页在付费墙开启且非会员时提前展示会员提示条（可直达订阅页），不再等用户点了播放才知道
  - `/api/user/auth/me` 新增 `subscription_required` 字段，前端 store 暴露 `subscriptionRequired` / `needsSubscription`
- **钱包到账确认**：支付回跳（`?order=` 兼容 `?paid=1`）后自动轮询订单状态（3s × 10 次），到账即刷新积分与会员身份并提示，无需手动刷新
- **钱包订阅页新增当前会员状态行**（套餐 / 剩余天数 / 到期日，未开通时提示付费墙）

### 变更 (Changed)
- **首页布局优化**：Hero 改为双栏（左：问候与主行动；右：会员状态卡——已开通显示套餐与到期进度，未开通显示付费墙引导）；内容区新增「我的内容」「站点与设备」分组标签与节奏；移动端 Hero 自动堆叠，数据条与字距/间距重新校准
- **管理端系统设置新增「付费墙（会员门禁）」分组**：要求有效订阅开关 + 拦截提示文案（配置白名单同步扩充）
- 版本号：后端 / 用户端 2.5.2

## [2.5.1] - 2026-09-20

### 修复 (Fixed)
- **会员身份（VIP）恒不生效**：`GET /api/user/auth/me` 的 `is_vip` 此前硬编码为 `false`，订阅生效后个人中心 / 顶栏的 VIP 徽章永不显示。现由订阅派生：存在 `status=active` 且 `end_date > now` 的订阅即为会员，与后台订阅总览同一判定口径
- **过期订阅仍显示「生效中」**：`GET /api/user/subscriptions` 直接透传库内 `status`（无人随时间翻转），现按 `end_date` 归一生效状态，并新增 `is_current` 字段与 `days_left` 下限保护
- **工单回复无通知**：用户回复工单后管理员收不到任何提醒（代码中为 `TODO`），现落站内消息 + WebSocket 实时推送，与新建工单同一链路
- **工单回复接口契约错误**：回复端点复用「创建工单」请求模型，强制要求 `title`，前端只能传占位标题。现改为独立的 `TicketReplyRequest`，并补上空内容 / 超长（2000 字）校验
- **每日求片上限不可配置**：管理端经济设置白名单未包含 `media_seek_daily_limit`，该限制在用户端硬编码兜底。现纳入白名单并在系统设置页新增入口
- **用户端忽略功能开关**：钱包的兑换 / 充值 / 订阅购买与签到页的签到入口未读取后端开关，管理员关闭后用户仍可操作并收到报错。现关闭时禁用入口并给出提示（钱包新增调用 `GET /api/user/economy/exchange/config`）

### 变更 (Changed)
- 管理端「系统设置」把兑换与求片合并为「兑换码 · 求片」分组，新增「每日求片上限」字段
- 版本号：后端 / 用户端 2.5.1、管理端 2.4.1

## [2.5.0] - 2026-09-20

### 新增 (Added)
- **观看记录（用户端）**
  - `GET /api/user/emby/history`：按条目去重的观看历史（保留最近一次会话的设备/客户端/进度，合并用户媒体数据，支持类型筛选与分页）
  - 用户端新增 `/history` 页面：设备/进度/播放方式一览，可从个人中心与顶栏进入
- **全局搜索**
  - 用户端新增 `/search` 页：跨整个媒体库检索（电影 / 剧集 / 单集分组展示）、最近搜索本地留存、无结果时一键跳转求片
  - 媒体库首页新增搜索入口，顶栏新增搜索图标（移动端抽屉同样可达）
- **我的收藏**
  - 用户端新增 `/favorites` 页：类型筛选 + 一键取消收藏（乐观更新，失败回滚）。此前收藏只能在详情页切换，后端接口存在却没有列表页
- **我的播放会话**
  - `GET /api/user/emby/sessions`：查看自己正在播放的会话（设备 / 客户端 / 进度）
  - `DELETE /api/user/emby/sessions/{key}`：结束自己的播放（同时释放转码进程，越权结束他人会话返回 404）
- **求片能力**
  - `GET /api/user/media-seek/lookup`：提交前库存预检，已在库直接给播放入口且不占额度
  - `GET /api/user/media-seek`：列表返回今日额度（已用 / 上限 / 剩余）
  - `DELETE /api/user/media-seek/{id}`：撤回尚未被处理的求片

### 变更 (Changed)
- 求片提交增加双层校验：同名处理中去重（409）与每日额度上限（`media_seek_daily_limit` 可配，默认 5 条，超限 429）
- 求片中心页重做：库存检查卡片、额度进度、状态筛选、撤回操作，并支持 `/request?name=xxx` 预填片名
- 消息中心重做：按消息类型深链至求片 / 工单 / 积分 / 订阅页面
- 首页重写为「内容 + 状态」仪表盘：账号速览数据条、继续观看、最近入库、站点动态、连接播放器；功能入口归还导航
- 顶栏按业务域分组并新增搜索 / 收藏入口与积分余额徽章；移动端新增底部导航坞（中心凸起签到项）
- 媒体库首页、媒体卡片、媒体行等组件由残留旧绿色硬编码统一切换为 Aurora 设计令牌

### 修复 (Fixed)
- 实现 `notify_staff_users()`：用户提交求片 / 工单后写入站内消息并实时推送给全部启用中的管理员（此前仅 TODO，后台必须手动刷新才能发现新请求）
- 删除死接口 `/api/user/emby-servers`、`/api/user/playback-sessions`（前端已无引用）

## [2.4.0] - 2026-09-20

### 新增 (Added)
- **管理后台缺失接口补齐**
  - `GET /api/admin/announcements`：公告列表（含停用项、`active_only` 过滤）——此前公告页只能发布/编辑，无法列出
  - `GET /api/admin/tickets/{id}/messages`：工单会话内容（含管理员署名）——此前工单只能盲回复
  - `GET /api/admin/users/{id}`：用户 360° 画像（资料 / 订阅历史 / 积分台账 / 订单 / 邀请 / 签到 / 观看）
  - `GET /api/admin/stats/trend`：趋势统计（按日补零：新增用户 / 播放 / 营收 / 签到）
  - `GET /api/admin/economy/subscriptions`：订阅总览（生效中 / 7 天内到期 / 已过期 + 用户名搜索）
- **管理后台页面**
  - 订阅管理页（总览四格 + 延长 / 续订）
  - 系统设置页（注册策略 + 签到 / 兑换 / 支付网关 / 邀请返利分组可视化编辑）
  - 用户管理新增详情抽屉（订阅 / 积分 / 订单 / 邀请四个标签页）与正规操作对话框
  - 数据概览新增待办条、交易概览、SVG 趋势图（7/14/30 天）

### 变更 (Changed)
- 侧边栏按业务域分组（概览 / 用户与订阅 / 运营 / 内容 / 支持 / 系统），顶栏改为面包屑 + 管理员菜单（接上修改密码）
- 管理端主题与用户端 Aurora（电光青 #22d3ee）对齐，Element Plus 组件与表格/抽屉/对话框全部随主题
- 「邀请与积分」页的原始键值设置对话框下线，配置统一收归「系统设置」

### 修复 (Fixed)
- 401 拦截器与页面 catch 重复弹错（同一错误弹两次提示）
- 用户/积分调整由手填数据库 ID 改为用户名远程搜索
- 工单状态 / 优先级、公告停用启用可在界面上直接操作（此前接口存在但前端未接）
- 删除未被引用的死样式文件，补齐缺失的全局重置与 body 背景

## [2025-01-10]

### 新增 (Added)
- **一键部署脚本** (`deploy.sh`)
  - 支持 `--build` 强制重新构建镜像
  - 支持 `--no-cache` 构建时不使用缓存
  - 支持 `--backend` 仅部署后端服务
  - 支持 `--frontend` 仅部署前端服务
  - 支持 `--bot` 仅部署 Telegram Bot
  - 支持 `--rollback` 回滚到上一版本
  - 支持 `--status` 显示服务状态
  - 支持 `--logs` 显示服务日志
  - 支持 `--update` 更新代码并部署
  - 自动环境检查（Docker、Docker Compose、.env 文件）
  - 健康检查功能（本地 + 外部）
  - 旧镜像自动清理

### 功能特性
- 彩色日志输出（INFO/SUCCESS/WARNING/ERROR）
- 服务健康状态检测
- Git 代码自动更新
- 容器状态监控
- 完整的帮助文档

### 使用示例
```bash
./deploy.sh              # 部署所有服务
./deploy.sh --build      # 强制重新构建并部署
./deploy.sh --backend    # 仅部署后端服务
./deploy.sh --update     # 更新代码并部署
./deploy.sh --rollback   # 回滚到上一版本
```

---

## 之前版本

### [初始版本]
- RoyalBot Emby Portal 基础部署配置
- Docker Compose 配置
- Nginx 反向代理配置
- 基础环境变量模板
