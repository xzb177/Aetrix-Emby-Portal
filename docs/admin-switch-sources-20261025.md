# 管理后台开关事实来源表（批 6，2026-10-25）

> 范围：审计报告《管理后台梳理审计报告（2026-10-25）》的 M12＋M13＋M14＋M15＋M16。
> 本批只做“口径收尾”：核对事实、写明来源、补文档。**不改任何开关的读取逻辑，不删任何数据库行，不动生产。**
> 本批不碰批 5（命名与导航），不碰其他批。

## 0. 基线与核对方法

- 代码基线：核对基线 tip `5eef134b305831d6e7293aafd603251038fde886`（审计基线 `166a9073` 已过期；
  批 4 回单时主线在 `2778c5df`，其后 #1303、#1305、#1306、#1308、#1312、#1314、#1311、#1174 陆续合并，
  其中 #1314 就是批 4 本身；本批开工时 tip 为 `7bc71934`，工作期间主线四次前进，本批每次都按规则停下
  重排到新 tip 并重验行号。末次重排区间（#1174）删了 `ENRICH_EPISODE_NIGHT_ONLY` 的 env 读取
  （该键不是本批的键，对本批结论无影响）、改写了 `enrich_worker.py` 夜间段并动了 `env.example` 一行注释；
  重验后更新的引用：`env.example` 558 行之后引用全部 -1，`enrich_worker.py` 引用区间 636–843→626–833，
  `store.py` 的 `PANEL_REMOVED_SWITCHES` 区间 57-110→57-104（仍为 45 对）；其余引用行号未动。
  另纠正本文档一处引用：`save_enrich_tuning_config` 的忽略点是 `:831-833`，`:758-759` 是读口径。
- 审计行号仅供定位：下表行号均为当前 tip 实测行号，凡与审计报告不一致处以本表为准，并在表中注明。
- 核对方法（每键都做了，不是抽查）：
  1. 在代码里找到真实读取点（`os.getenv`／`env_int`／`env_float`／`store.get_value`），记下代码缺省；
  2. 在 `env.example` 里查该键是否登记、登记值是否与代码缺省逐字一致；
  3. 在 `admin_frontend/src` 里查该键有无面板写入口；
  4. 独立静态扫描：代码实际读取的环境变量键 396 个，`env.example` 登记键 461 个，**缺失 0 个**；
     防漂移测试 `tests/test_perfection_r44_envdoc.py` 基线 4 passed（见 §5）。
- 批 4 分支（`origin/admin-cleanup-batch4-20261025`）只新增了 `docs/admin-no-frontend-endpoints-20261025.md`，
  与本批文档无文件名冲突、无内容重叠。

## 1. 总优先级口径

| 场景 | 生效顺序 |
|---|---|
| 已摘面板的 45 键（M12，`store.py` 的 `PANEL_REMOVED_SWITCHES`） | **env（显式设置且非空）＞ DB 存量行＞代码缺省**。env 为空时不接管，DB 行继续生效；只有 env 显式写了值（含 `"0"`／`"false"`）才覆盖 DB 行。实现：`backend/integrations/store.py` 的 `switch_env_value`＋`get_values`／`read_values` 的 `_apply_switch_env`。 |
| 纯 env-only 键（M15，模块加载时读一次） | **env＞代码缺省**。没有 DB 这一档：DB 里即使有同名行也不读。改 env 后必须重启对应进程才生效（import 时读一次，见各键备注）。 |
| 刮削调优键（M13 相关，`enrich_worker.py`） | env 设置即接管（`from_env` 会在面板标出）；未设 env 时走 DB 行；env-only 的三个键（见 §3）连 DB 档都没有，只读 env，缺省即生产现值。 |
| DB-only 键（M14／M16，见 §4） | **DB 行＞代码缺省**。没有 env 档、没有面板档。改 DB 行即改行为（60 秒 store 缓存内生效，跨进程靠 TTL 兜底）。 |

一句话：**排障时先看 env，再看 DB 行，最后看代码缺省；只看 DB 行会漏掉 env 覆盖，只看面板会漏掉所有 env-only 与 DB-only 键。**

## 2. M15：env-only 开关事实来源表

结论先说：**审计时“多不在 env.example”的缺口，在当前 tip 已全部闭合。**
审计点名的键、两个“族”的全部成员、以及批 4 之后新增的在飞键，当前 tip 都已登记在
`env.example`，且登记值与代码缺省逐字一致（有一处审计后默认值本身变了，见表下注 2）。
按任务规则“已在 env.example 的不重复加”，**本批对 `env.example` 的改动为 0 行**——不是漏做，
是复核证明已无缺失；重复追加只会制造重复行。

表中“env.example 行号”为当前 tip 实测行；“DB”栏的“—”表示该键没有 DB 档（代码不读库）。

### 2.1 审计点名键

| env 键 | env.example（行号：登记值） | DB | 代码缺省（读取点） | 备注 |
|---|---|---|---|---|
| `IDENTITY_SHELF_GATE` | 805：`0` | — | 0＝关（`backend/emby_server/identity_gate.py:66`） | import 时读一次，改后需重启；开后须全库 bump 清列表缓存，否则不生效 |
| `IDENTITY_SHELF_PHASE2_ENABLED` | 810：`0` | — | 0＝关（`identity_gate.py:69`） | 须总开关同时开才生效 |
| `IDENTITY_SHELF_PHASE3_ENABLED` | 815：`0` | — | 0＝关（`identity_gate.py:72`） | 须总开关同时开才生效 |
| `COLLECTIONS_ENABLED` | 819：`0` | — | 0＝关（`backend/emby_server/media_sets.py:40`） | 关时合集接口惰性 404 |
| `COLLECTION_SHOW_UNBOUND` | 823：`0` | — | 0＝受案 A 门控（`media_sets.py:42`） | 1＝合集内未绑定成员全露 |
| `VISIBILITY_GOVERNANCE_ENABLED` | 827：`0` | — | 0＝关（`backend/emby_server/visibility_governance.py:43`） | L7 治理流水线总开关 |
| `IDENTIFY_REVIEW_QUEUE_ENABLED` | 1049：`0` | — | 0＝关（`backend/emby_server/identify_review.py:44`） | 有审查队列页面（数据页），但总闸本身无面板开关 |
| `PLAY_VARIANTS_ENABLED` | 1066：`0` | — | 0＝关（`backend/emby_server/play_variants.py:56`） | import 时读一次，改后需重启 |
| `PLAYBACK_VARIANT_SWITCH_ENABLED` | 1184：`1` | — | 1＝开（`backend/emby_server/variant_switch.py:46,59`） | 见注 2：审计时缺省还是 0，当前 tip 已改为默认开 |
| `PLAYBACK_BUS_ENABLED` | 1101：`0` | — | 0＝关（`backend/emby_server/playback_bus.py:76`） | 有 PlaybackBus.vue（命令／会话页），但总闸无面板开关；关时发命令回 503 |
| `PLAYBACK_BUS_SNAPSHOT_INTERVAL_SEC` | 1102：`15` | — | 15 秒（总线快照间隔） | 与总闸同段登记 |
| `PLAYBACK_BUS_COMMAND_TIMEOUT_SEC` | 1103：`10` | — | 10 秒（命令超时） | 与总闸同段登记 |
| `PINYIN_SORT_ENABLED` | 895：`1` | — | 1＝开（`backend/emby_server/pinyin_sort.py:31-32`） | 关则回退 `name.lower()` 排序 |
| `SCAN_DIR_DUE_SCHEDULING` | 1069：`1` | — | 1＝开（`backend/emby_server/scanner.py:2681`） | 关则核对深度回到旧行为 |
| `SCAN_DIR_RECHECK_SEC` | 1072：`86400` | — | 86400 秒＝24 小时（`scanner.py:2690`，钳制 300–2592000） | 目录到期周期 |
| `SCAN_SOURCE_SNAPSHOT_ENABLED` | 1076：`1` | — | 1＝开 | 按源行落目录指纹快照 |
| `PROVIDER_BREAKER_ENABLED` | 732：`1` | — | 1＝开（`backend/emby_server/metasources/breaker.py:67`） | 设 0 关闭整套熔断 |
| `PROVIDER_BREAKER_THRESHOLD` | 734：`3` | — | 3 次（`breaker.py:73`） | 连续失败几次开断 |
| `PROVIDER_BREAKER_COOLDOWN_SEC` | 736：`60` | — | 60 秒（`breaker.py:78`） | 开断后冷却 |
| `PROVIDER_BREAKER_FAST_SEC` | 738：`60` | — | 60 秒（`breaker.py:82`） | 403／429 一次即开断的冷却 |

### 2.2 两个“族”的全部成员

| 族 | 成员与缺省 | env.example 登记 |
|---|---|---|
| 变体可用性探活族 | `VARIANT_AVAILABILITY_PROBE_ENABLED`＝0（`variant_availability.py:100`）、`_INTERVAL_SEC`＝300（`:106`）、`_FAILURE_THRESHOLD`＝3（`:111`）、`_TIMEOUT_SEC`＝10（`:116`）、`_MAX_TARGETS_PER_CYCLE`＝20（`:121`）、`_WINDOW_HOURS`＝24（`:126`） | 1145–1150 行，六键齐全，值逐字一致 |
| 中文聚合族 | `ZH_AGGREGATE_ENABLED`＝0（`zh_aggregate.py:40`）、`ZH_AGGREGATE_SOURCES`（`:61`，见注 1）、`ZH_AGGREGATE_LANGUAGE`＝空（`:77`）、`ZH_AGGREGATE_REGION`＝空（`:81`） | 1256–1260 行，四键齐全 |

注 1：`ZH_AGGREGATE_SOURCES` 的代码写法是 `os.getenv(..., "")`，空名单时回退内置默认
`douban_frodo,tmdb_details`（`zh_aggregate.py:49-52`）；`env.example:1257` 登记的正是这个回退值，
因此“登记值＝实际生效缺省”，不算不一致。未知源名会被忽略，不会悄悄问到别的源。

注 2：`PLAYBACK_VARIANT_SWITCH_ENABLED` 在审计报告里写的是“默认 0＝关”，但当前 tip 代码
（`variant_switch.py:24` 注释“默认开；显式设 0 回滚”＋`:59` 的 `or "1"` 缺省）与
`env.example:1185` 的 `1` 一致，均为默认开。审计结论已过期，以当前 tip 为准。

### 2.3 批 4 之后新增的在飞键（本批复核时已在主线，且已登记）

| env 键 | 代码缺省（读取点） | env.example |
|---|---|---|
| `ENRICH_TWO_QUEUE_ENABLED` | 0＝关（`backend/emby_server/enrich_queues.py:30`） | 1293：`0` |
| `ENRICH_AI_POLICY_ENABLED` | 0＝关（`backend/emby_server/ai_policy.py:45`） | 1295：`0` |
| `AI_ADVISOR_ENABLED`（＋`AI_ADVISOR_PROVIDER`／`AI_ADVISOR_MODEL`，任一为空即参谋关闭） | 0＝关（`backend/emby_server/metasources/ai_advisor.py:42`） | 1302–1304：`0`／空／空 |
| `PEOPLE_SCRAPE_ENABLED` | 0＝关（`backend/emby_server/metasources/people_scrape.py:34`） | 1308：`0` |
| `APPLECMS_PROBE_ENABLED` | 0＝关（`backend/emby_server/applecms_probe.py:50`） | 1314：`0` |
| `LIBRARY_SOURCE_HEALTH_ENABLED` | 0＝关（`backend/emby_server/library_source_health.py:61`） | 1285：`0` |
| `LIBRARY_SOURCE_HEALTH_HIDE_AFTER` | 3 次（`library_source_health.py:66`） | 1287：`3` |
| `LIBRARY_SOURCE_HEALTH_PILOT_LIBRARY_IDS` | 空＝对全部库过滤（`library_source_health_pilot.py:28`） | 1291：空 |
| `MOUNT_HEALTH_FETCH_TIMEOUT` | 60 秒（`backend/emby_server/mount_health.py:60`） | 903：`60` |
| `MOUNT_SOURCE_CREDENTIALS_ENABLED` | 0＝关（`backend/emby_server/mount_source_credentials.py:86`，挂载第 4 步源级凭据表优先读总闸） | 1117：`0` |
| `SCAN_CREDIBILITY_GUARD_ENABLED`（＋`SCAN_CREDIBILITY_MAX_RATIO`＝0.5） | 1＝开 | 1156–1157：`1`／`0.5` |

### 2.4 M15 结论

- 补了哪些键：**0 个**。审计时缺失的键在审计基线之后、当前 tip 之前已被其他 PR 陆续登记进
  `env.example`（均带中文注释，写清含义与缺省），本批逐键复核值与代码缺省一致，故不重复追加。
- 部署者改这些开关的唯一办法仍是：改 env 文件＋重启对应进程；管理面板上找不到开关是符合
  10-19 原则（功能默认开、想关去文件配置）的有意设计，不是缺口。本批不给它们补面板入口——
  是否给面板入口是产品决策，不在本批擅自定。

## 3. M13：面板写明示——当前 tip 已不存在写入口，本批不改前端

审计时的 M13 是“面板上改 `enrich_workers`／`enrich_batch` 以为生效、实际被静默忽略”。
当前 tip 复核结果：**写入口已经不存在，明示也已经做到**，因此本批对 `admin_frontend` 的改动为 0。

证据（当前 tip，`admin_frontend/src/views/EmbyAdmin.vue`）：

| 项 | 证据 |
|---|---|
| 三个 env-only 键只读展示 | `:1136` 的 `TUNING_ENV_ONLY_KEYS = ['enrich_workers', 'enrich_batch', 'enrich_tmdb_batch']`；模板里这三个键不渲染输入框，只渲染只读值＋黄色标签“环境变量 … 专供，面板不可改”（`:2290` 附近行）；`db_pool_size` 同样只读展示＋“环境变量 DB_POOL_SIZE 专供，面板不可改”（`:2297-2301`） |
| 保存时不下发 | `saveEnrichTuningAction`（`:1205` 起）在提交前把三个 env-only 键从 payload 中删掉（`:1210`），注释“面板不可写，不下发（后端也会忽略）” |
| 后端双重兜底 | `backend/emby_server/enrich_worker.py:758-759`（读口径：env-only 键不读 DB 行、未设 env 用缺省）与 `:831-833` 的 `save_enrich_tuning_config`（面板写入一律忽略、不写库）两处；`backend/database.py:106-111` 的 `db_pool_size` 只读 env |
| 面板顶部已有总说明 | “线程数／每批认领条数／DB 连接池大小为环境变量专供（面板只读，改完需重启对应进程）。其余保存后 60 秒内热生效。” |

事实来源表（M13 三键，缺省＝生产现值，摘面板不回退）：

| 面板键 | env 键 | 代码缺省 | 读取点 |
|---|---|---|---|
| `enrich_workers`（线程数） | `ENRICH_WORKERS`（`env.example:534`：`8`） | 8（上限 16，改完需重启 worker） | `enrich_worker.py:659-660` |
| `enrich_batch`（每批认领条数） | `ENRICH_BATCH`（`env.example:536`：`200`） | 200（下限 10） | `enrich_worker.py:661-662` |
| `db_pool_size`（DB 连接池大小） | `DB_POOL_SIZE`（`env.example:151`：`25`，未注释的生效行） | 25 | `backend/database.py:106-111` |

本批在“摘除控件”与“旁边明示”二选一中，**选的是“现状已是明示，不再动”**：
理由是当前实现已同时满足明示（只读＋标签＋顶部说明）与防误写（前端不下发＋后端忽略）两层，
再摘掉控件反而会让部署者看不到当前生效值（线程数／批次／池大小是排障常用数），得不偿失。
附带：`enrich_tmdb_batch`（`ENRICH_TMDB_BATCH`，缺省 1）与上述三键同待遇，面板同样只读。

## 4. M14＋M16：入口补齐——选“补 env＋文档”路线，但本批只落文档，不改读取逻辑

路线说明：任务定的是“补 env＋文档、不补面板”。本批逐键核对后发现：**全部 8＋1 个键的读取路径
都不在管理后台域文件里**——它们分别落在播放文件、刮削／元数据文件、缓存模块里。
按任务硬性边界（“绝不改那些文件的读取逻辑，播放／刮削域改动另立批”），本批对这些键
**一个 env 读取都不补、一个面板入口都不加**，只在本文档中写明“当前唯一事实来源＋缺入口现状”。
这不是拖延：给播放／刮削文件加 env 读取会改变生效优先级（env 会压过现行 DB 值），
属于行为改动，必须另立批做回归，不能塞进清理批。

逐键现状（“事实来源”栏就是部署者今天唯一能改它的办法）：

| 键 | 当前唯一事实来源 | 代码缺省 | 读取点 | 缺口与延期理由 |
|---|---|---|---|---|
| `drive_relay_enabled`（M14，影响最大） | **DB 行**（无 env、无面板、无 schema 登记） | 关（`"false"`） | `backend/emby_server/playback_strategy.py:51,132-143` | 中继是播放主线，开中继今天只能写库；与 `drive_direct_play_enabled` 不对称——后者是 M12 已摘面板键，有 env `DRIVE_DIRECT_PLAY_ENABLED`（`store.py` 接管，env＞DB＞缺省开）＋ ClientPolicy 只读展示。补 env 入口须改播放文件读取逻辑，另立批；另立批时缺省仍取关、不得擅自改默认开 |
| `introdb_enabled` | **DB 行**（无 env、无面板） | 开（`"true"`） | `playback_strategy.py:57-70` | IntroDB 自动片头片尾；读失败 fail-safe 回默认开。补入口同上，另立批 |
| `enrich_tmdb_text_fill_only` | **env `ENRICH_TMDB_TEXT_FILL_ONLY`＞DB 行**（有 env，`env.example:556`：`1`；无面板） | 开（True＝只填空、不覆盖；显式 `"0"` 回退覆盖） | `backend/emby_server/tmdb.py:92,904-920`（进程内 30 秒缓存） | 审计时“无后台 UI”的说法仍成立，但“无 env”的说法已过期：当前 tip 已有 env 且已登记。补面板入口须动刮削域，另立批；在此之前部署者走 env 即可 |
| `items_list_cache_ttl_seconds` | **DB 行**（无 env、无面板写入口） | 300 秒（范围 10–3600） | `backend/emby_server/items_list_cache.py:66,134-143` | 连 env 都没有，只能改代码缺省或写 DB 行；模块内已有 `config_payload()`（注释“供将来管理端点”），将来接管理端点时直接用它，不要另造读取口。补入口须动缓存模块，另立批 |
| `items_list_cache_max_ids` | **DB 行**（无 env、无面板写入口） | 5000（范围 100–50000） | `items_list_cache.py:67,145-153` | 同上。注意同模块的 `items_list_cache_enabled` 本身是 M12 已摘面板键（有 env `ITEMS_LIST_CACHE_ENABLED`），不要把“开关有 env”误读成“ttl／max 也有 env” |
| `items_dedup_cache_ttl_seconds` | **DB 行**（无 env、无面板写入口） | 60 秒（范围 10–3600） | `backend/emby_server/items_dedup_cache.py:57,117-125` | 仅在 `backend/config_schema.py:201` 登记了类型与范围，无 `save_config` 调用方，登记≠入口。补入口另立批 |
| `items_dedup_cache_max_entries` | **DB 行**（无 env、无面板写入口） | 2000（范围 100–20000） | `items_dedup_cache.py:58,126-134`；登记 `config_schema.py:202` | 同上。同模块的 `items_dedup_cache_enabled` 是 M12 已摘面板键（有 env），ttl／max 没有 |
| `site_base_url` | **DB 行，仅作回退**（无任何写入通道） | 空 | `backend/tg_bot/login_token.py:125-130` | 遗留回退键：正统键 `site_url` 有面板入口（支付设置“站点地址”），`site_base_url` 只在 `site_url` 为空时兜底。不要给它补入口——补了就是鼓励两套键并存；正确方向是将来清理回退逻辑，另立批 |
| `meta_source_keys_tmdb`（附带遗留键，不计入 8 项） | **无**（写路径已堵死） | — | `backend/emby_server/metasources/config.py:48-56`；一次性迁移见 `backend/config_self_heal.py:446` | 现行 TMDB 密钥走 `tmdb_api_keys`＋管理端密钥池端点；旧键只剩迁移读取，不要补入口、不要恢复写入 |

## 5. M12：DB 旧行清理——本批不删，原因与前置条件

当前 tip 重数结果：`backend/integrations/store.py:57-104` 的 `PANEL_REMOVED_SWITCHES` 为 **45 对**
（审计报告精确计数 45 项，与当前 tip 一致；审计前底账的 46 项是旧计数，以当前 tip 的 45 为准）。
45 对清单（DB 键 → env 名）：

| DB 键 | env 名 | DB 键 | env 名 |
|---|---|---|---|
| `ai_enabled` | `AI_ENABLED` | `lottery_enabled` | `LOTTERY_ENABLED` |
| `backup_enabled` | `BACKUP_ENABLED` | `lottery_notify_winners` | `LOTTERY_NOTIFY_WINNERS` |
| `bot_cmd_bind` | `BOT_CMD_BIND` | `media_seek_enabled` | `MEDIA_SEEK_ENABLED` |
| `bot_cmd_checkin` | `BOT_CMD_CHECKIN` | `media_seek_notify_voters` | `MEDIA_SEEK_NOTIFY_VOTERS` |
| `bot_cmd_points` | `BOT_CMD_POINTS` | `media_seek_vote_enabled` | `MEDIA_SEEK_VOTE_ENABLED` |
| `bot_cmd_redeem` | `BOT_CMD_REDEEM` | `meta_source_enabled_anilist` | `META_SOURCE_ENABLED_ANILIST` |
| `bot_enabled` | `BOT_ENABLED` | `meta_source_enabled_bangumi` | `META_SOURCE_ENABLED_BANGUMI` |
| `bot_redpacket_enabled` | `BOT_REDPACKET_ENABLED` | `meta_source_enabled_douban` | `META_SOURCE_ENABLED_DOUBAN` |
| `captcha_protect_login` | `CAPTCHA_PROTECT_LOGIN` | `meta_source_enabled_omdb` | `META_SOURCE_ENABLED_OMDB` |
| `captcha_protect_register` | `CAPTCHA_PROTECT_REGISTER` | `meta_source_enabled_tmdb` | `META_SOURCE_ENABLED_TMDB` |
| `coupon_enabled` | `COUPON_ENABLED` | `meta_source_enabled_tvdb` | `META_SOURCE_ENABLED_TVDB` |
| `drive_direct_play_enabled` | `DRIVE_DIRECT_PLAY_ENABLED` | `meta_source_enabled_tvmaze` | `META_SOURCE_ENABLED_TVMAZE` |
| `enrich_douban_ban_downgrade` | `ENRICH_DOUBAN_BAN_DOWNGRADE` | `meta_sources_prefer_chinese` | `META_SOURCES_PREFER_CHINESE` |
| `enrich_tmdb_batch` | `ENRICH_TMDB_BATCH` | `new_media_notify_enabled` | `NEW_MEDIA_NOTIFY_ENABLED` |
| `exchange_enabled` | `EXCHANGE_ENABLED` | `points_audit_enabled` | `POINTS_AUDIT_ENABLED` |
| `expiry_reminder_enabled` | `EXPIRY_REMINDER_ENABLED` | `points_transfer_enabled` | `POINTS_TRANSFER_ENABLED` |
| `hotlink_protection_enabled` | `HOTLINK_PROTECTION_ENABLED` | `redpacket_refund_enabled` | `REDPACKET_REFUND_ENABLED` |
| `invitation_enabled` | `INVITATION_ENABLED` | `register_ratelimit_enabled` | `REGISTER_RATELIMIT_ENABLED` |
| `items_dedup_cache_enabled` | `ITEMS_DEDUP_CACHE_ENABLED` | `series_merge_enabled` | `SERIES_MERGE_ENABLED` |
| `items_list_cache_enabled` | `ITEMS_LIST_CACHE_ENABLED` | `series_merge_loose_enabled` | `SERIES_MERGE_LOOSE_ENABLED` |
| `lottery_auto_draw_enabled` | `LOTTERY_AUTO_DRAW_ENABLED` | `series_merge_require_year` | `SERIES_MERGE_REQUIRE_YEAR` |
| `teaser_enabled` | `TEASER_ENABLED` | `tg_bind_guide_enabled` | `TG_BIND_GUIDE_ENABLED` |
| `vitality_enabled` | `VITALITY_ENABLED` | — | — |

（上表 45 项：左列 23 项＋右列 22 项；`store.py` 注释原话：DB 行“原样保留作次级来源”。）

**本批不删行的原因（写进 PR 描述同样一句话）：删行是生产写操作，不可逆。**
删行前必须同时满足三项前置条件，本批一项都不满足、也不在本批内补：

1. **全表备份在先**：删前对 `system_configs` 做完整备份，错删可回插；
2. **逐键核对生产现值＝env 缺省**：45 个键逐一核对生产 DB 行现值与代码缺省／env 缺省一致，
   不一致的先对齐（对齐本身就是一次生产写操作，要单独确认），不能假设“摘面板时已对齐”；
3. **用户一句确认**：删行是生产写操作，须用户明确点头后由专批执行。
   本任务明确“不动生产”，故本批不删行、**也不在 PR 里放可直接执行的删行脚本**
   （脚本一旦合署就等于把生产写操作埋进了仓库，下游误跑即删行）。

删行后的口径变化（给专批留的备忘）：删行后这 45 键退化为“env＞代码缺省”，
DB 行不再是次级来源；删行本身不改变任何已设 env 的生效值，但会改变“未设 env 且 DB 行
与缺省不一致”的键的生效值——这正是前置条件第 2 项必须逐键核对的原因。

## 6. 本批改动清单与回滚

| 文件 | 改动 |
|---|---|
| `docs/admin-switch-sources-20261025.md`（新增，本文档） | M12／M13／M14／M15／M16 全部结论与事实来源表 |
| `env.example` | **0 行**：M15 点名键及族成员、批 4 后新增键均已登记且值与代码缺省一致，不重复加 |
| `admin_frontend` 面板文件 | **0 行**：M13 写入口已不存在、明示已做到（§3），不改前端 |

**回滚＝ revert 本 PR**。本批只有文档新增，无行为变化、无配置变化、无生产写入，
revert 后不影响任何开关的生效值。CI 全量未跑（见 §7），合并前以 CI 为准。

## 7. 本地验证与未做项

- 本地验证：`tests/test_perfection_r44_envdoc.py`（env 文档防漂移测试，当前 tip 含该文件），
  改动前基线 4 passed；本文档不改代码与 `env.example`，不影响该测试结论，改动后复跑结果见 PR 回单。
  前端 type-check 未跑：本批未动任何前端文件，无需跑；CI 全量未跑，明说。
- 未做项及原因（逐项）：
  1. `env.example` 补键：未做——复核证明 0 缺失（§2.4），重复加违反任务“已有的不重复加”规则。
  2. `admin_frontend` 面板摘除／明示改动：未做——当前 tip 写入口已不存在、明示已做到（§3），
     再改前端只增回归风险；任务本身允许该项记“已不存在”。
  3. M12 的 45 项 DB 旧行删除：未做——生产写操作，缺备份＋逐键生产现值核对＋用户确认三项前置条件（§5），
     且任务明令不动生产、PR 内不放删行脚本。
  4. M14／M16 的 env 读取补齐与面板入口：未做——读取路径落在播放／刮削／缓存文件，
     任务硬性边界禁止本批改那些文件的读取逻辑；补入口须另立批做回归（§4）。
  5. M14／M16 中 `drive_relay_enabled` 的默认开／关：未动——缺省仍取代码现值“关”，
     改默认是产品决策，不在本批擅自改。
  6. 生产现值核对：未做——本批禁碰生产；凡涉及“生产现值”的表述（如 M13 的“缺省＝生产现值”）
     均引自代码注释与审计报告，未在本批重查生产库，专批执行前必须重查。
