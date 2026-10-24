"""自建 Emby 媒体服务器数据模型"""
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    event,
    text,
)

from backend.database import Base


class Library(Base):
    """媒体库"""

    __tablename__ = "emby_libraries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    guid = Column(String(64), unique=True, nullable=False, index=True)
    name = Column(String(200), nullable=False)
    # 属于哪个服（server_realms.id）。服之间内容隔离：某个服的 EA 只提供本服的库。
    # 老部署由迁移统一回填到默认服，行为不变。
    realm_id = Column(Integer)
    collection_type = Column(String(30), default="movies")  # movies / tvshows / mixed
    paths = Column(Text, default="")  # 逗号分隔的扫描根目录；虚拟媒体库为空
    # 与 :attr:`paths` **逐条对应**的存储后端（逗号分隔）：local / rclone / 115。
    # 路径与后端分离后，界面上不再出现 ``mount://1/...`` 这种技术前缀——展示时用
    # 「挂载内路径 + 后端标签」，入库时仍然把前缀拼回 paths（见 mounts.library_path_entries
    # 与 mounts.assemble_library_path），所以这一列纯粹是给界面回显用的旁挂信息。
    # 老库没有这一列时读出来是空串，后端按路径前缀 + 挂载类型现推，行为不变。
    storage_backends = Column(Text, default="")
    is_enabled = Column(Boolean, default=True)
    is_scanning = Column(Boolean, default=False)
    # 刮削策略：missing_only / 3m / 6m / 1y / all（见 scanner.SCAN_POLICIES）
    scrape_policy = Column(String(20), default="missing_only")
    # 管理员上传的媒体库封面；保存为图片目录下的相对路径，扫描/刮削不会覆盖它。
    cover_path = Column(String(500))
    # 封面自动生成：样式 + 标题文字。留空 = 沿用直传的 cover_path 不生成。
    # 生成时从库里挑**最新入库**（last_scraped_at 最近）的海报自动拼图，
    # 所以刚补完刮削的库换个样式重生成就是新海报，不用管理员传图。
    cover_template = Column(String(20))    # poster / visual / filmstrip
    cover_title = Column(String(100))      # 支持 {library} {type} {year}
    cover_subtitle = Column(String(100))   # 同上
    # 新片入库后自动重新生成封面（v2.43.1）：扫完一轮且**确实有新增条目**时，按当前
    # cover_template / 标题重拼一次封面。默认关闭——它会在扫描线程里多花一次渲染时间，
    # 而且多数库的封面本来就不用天天变。
    cover_auto_regen = Column(Boolean, default=False)
    # 增量扫描（v2.44.0，默认开）：走已有的目录/文件指纹秒跳（scanner.SCAN_INCREMENTAL
    # 只能全局关，这个是每库粒度）。关掉 = 每轮都把目录当没变过，完整处理一遍。
    incremental_scan = Column(Boolean, default=True)
    # 实时监听（v2.44.0，默认开）：本机目录有变动就自动触发一轮增量扫描，不用等定时扫描。
    # 只对**本机路径**生效（远程挂载拿不到可靠的事件源，仍靠定时扫描）。
    # 监听起不来时自动降级为定时扫描，并在设置里说明，不会默默不工作。
    fs_watch = Column(Boolean, default=True)
    # 虚拟媒体库：按发行平台（Netflix / Disney+ …）自动生成，没有自己的文件与路径
    is_virtual = Column(Boolean, default=False)
    platform = Column(String(30))  # 虚拟库对应的平台 id（见 scanner.PLATFORM_LABELS）
    # 绑定 115 账号配置档（pan115_accounts.id）：不同媒体库可用不同 115 账号转存/下载。
    # 未绑定时回退到默认账号，再回退到服务器级 PAN115_COOKIE（兼容旧部署）。
    account_115_id = Column(Integer)
    # 负责这台库的播放节点（remote_servers.id，kind=ea 的那条）。
    # **NULL = 未分配**：任何节点都能看到它、由面板（EM）扫描——单节点部署与
    # 「刚加完节点还没来得及分配」的过渡期都靠这个语义保持与以前完全一致。
    # 已分配时：只有那台节点会向客户端展示它、只有它会扫描它（见 emby_server/nodes.py）。
    node_id = Column(Integer)
    # 绑定的存储挂载（storage_mounts.id，逗号分隔）：媒体库的内容来源之一。
    # `paths` 是**本机目录**（rclone / CloudDrive2 / SMB 挂载盘最终也是本机目录），
    # `mount_ids` 则是显式声明的挂载来源（本地目录 / STRM / 115 直挂 / WebDAV / AList），
    # 两者可同时存在，扫描时一起遍历；远程挂载的条目 file_path 形如 mount://<id>/<相对路径>。
    mount_ids = Column(Text, default="")
    last_scan_at = Column(DateTime)
    # 最近一次扫描的结果（v2.22.0）：只靠 is_scanning + last_scan_at 看不出「扫得怎么样」——
    # 新增/更新/删除多少、哪些来源读不到、有没有异常都只留在日志里，扫描结束就查不到了。
    # 这里把结果落库，管理端列表直接带回（见 scanner.begin_scan / finish_scan / scan_result_payload）。
    # scan_status: running / success / partial / failed / interrupted
    # （partial = 有来源不可用、已跳过清理；interrupted = 被进程重启打断，启动时自动续扫）
    scan_status = Column(String(20))
    scan_stats = Column(Text)          # JSON：added/updated/removed/probed/scraped/unchanged…
    scan_error = Column(String(500))   # 异常摘要（failed / interrupted 时写入），来源失败原因进 stats.failed_roots
    # 扫描进行中的进度快照（v2.27.0）：只有 running 时有值，结束时清空。
    # 只靠 scan_status=running 看不出「扫到哪了」——四库同点扫描时 item_count 长时间是 0，
    # 面板只能显示「扫描中」；这里带上阶段 / 已发现 / 已处理 / 当前目录 / 本轮远程请求数，
    # 由 scan_queue 的刷盘线程每几秒写一次。
    scan_progress = Column(Text)       # JSON：phase/enumerated/processed/current/remote_lists…
    # 本轮扫描**开始**的时刻，用来判断「是不是崩溃残留」。
    # 不能用 updated_at：进度刷盘（scan_queue.flush_once）每几秒写一次 scan_progress，
    # ORM 的 onupdate 会连带刷新 updated_at——于是它永远是「刚刚」，让
    # reset_stale_scan_flags 的超时判定恒不成立，卡死的库永远复位不了
    # （生产事故：部署打断扫描后两个库永远显示「扫描中」）。
    scan_started_at = Column(DateTime)
    item_count = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class MediaItem(Base):
    """媒体条目（电影 / 剧集 / 季 / 集）"""

    __tablename__ = "emby_items"
    __table_args__ = (
        Index("idx_item_lib_type", "library_id", "item_type"),
        Index("idx_item_parent", "parent_id"),
        Index("idx_item_series", "series_id"),
        # 两阶段扫描 Phase 2：后台探测按（状态，优先级）取待探测条目
        Index("idx_item_probe", "probe_status", "probe_priority", "id"),
        # 退避到期判定 / 按状态统计「重试中」（v2.53）
        Index("idx_item_probe_retry", "probe_status", "probe_next_retry_at"),
        # 补全队列抢单：WHERE enrich_status='pending' AND enrich_next_retry_at<=now
        # ORDER BY enrich_priority DESC。之前 enrich_status 只有单列索引，而抢单还
        # 带一个 to-time 条件与优先级排序，PG 得自己过滤 + 排序，积压一多就是全表级
        # 开销（探测队列的 idx_item_probe 就是为此建的，补全队列当时漏了）。
        Index("idx_item_enrich", "enrich_status", "enrich_next_retry_at",
              "enrich_priority"),
        # 补全进度统计（2026-10-23 压测实锤）：进度快照按 enrich_claimed_at >=
        # 今天逐类型计数，没有索引时每条都是 Parallel Seq Scan（cost 约 5.5 万、
        # 每分钟数十次），能拖住近 1 个核。等价 DDL：
        # CREATE INDEX idx_item_enrich_claimed ON emby_items (enrich_claimed_at)
        Index("idx_item_enrich_claimed", "enrich_claimed_at"),
        # 抢单去重聚合（同日实锤）：_claim_batch 的按剧去重子查询
        # GROUP BY coalesce(series_id, id)，原先走单列状态索引位图扫 7.8 万行
        # （含已软删/已合并的死行）。部分索引只含活行（约 2.4 万），INCLUDE
        # 带上聚合要的三列供 index-only 扫描。非 PG 方言忽略 include/where。
        Index("idx_item_enrich_claim_cover", "enrich_status", "enrich_next_retry_at",
              postgresql_include=["series_id", "date_added", "library_id"],
              postgresql_where=text("deleted_at IS NULL AND merged_into_id IS NULL")),
        # 追新日历：按「入库时间落在某月」取条目（date_added BETWEEN 起 止）。
        # 没有索引时这是一张全表扫 + filesort，条目量上万后打开日历要几秒。
        # 复合第二列带上 item_type：日历的类型筛选（电影 / 剧集 / 单集）
        # 绝大多数时候都带，能把回表行数再压一截。
        Index("idx_item_added", "date_added", "item_type"),
        # 软删除（v2.48.0）：几乎所有查询都带 `library_id = ? AND deleted_at IS NULL`
        # （可见性由全局过滤器拼上）。把 deleted_at 跟在 library_id 后面，已下架的
        # 行能在索引里就被跳过，不用先回表再过滤。
        Index("idx_item_lib_deleted", "library_id", "deleted_at"),
        # /emby/Items 默认浏览：WHERE is_hidden=false AND deleted_at IS NULL
        # （deleted_at 由全局 ORM 钩子自动拼上）+ item_type 等值/IN
        # + ORDER BY sort_name, id。部分索引只含可见行，翻页与 COUNT 不再全表扫。
        Index("idx_item_browse_sort", "item_type", "sort_name", "id",
              postgresql_where=text("is_hidden = false AND deleted_at IS NULL")),
        # P0 修复（2026-10-17 压测实锤）：Recursive=true（不带 ParentId）时没有
        # item_type 等值过滤，idx_item_browse_sort 的首列用不上，ORDER BY sort_name, id
        # 只能全表扫 + 排序。这个索引去掉首列，专供无类型过滤的浏览排序与 COUNT。
        # 部分谓词与查询条件一致，规划器可用。
        Index("idx_item_browse_sort_all", "sort_name", "id",
              postgresql_where=text("is_hidden = false AND deleted_at IS NULL")),
        # 搜索 trigram GIN 索引（r7）：/emby/Items?SearchTerm=、/emby/Search/Hints、
        # 求片中心的 ILIKE 预筛都走这四列。postgresql_using / postgresql_ops
        # 是 PG 专属参数，SQLite 下 create_all 会忽略它们、退化为普通 btree 索引。
        # 索引名必须与 _LEGACY_INDEXES 老库迁移同名（幂等检查按名跳过）。
        Index("idx_item_name_trgm", "name",
              postgresql_using="gin", postgresql_ops={"name": "gin_trgm_ops"}),
        Index("idx_item_original_title_trgm", "original_title",
              postgresql_using="gin",
              postgresql_ops={"original_title": "gin_trgm_ops"}),
        Index("idx_item_sort_name_trgm", "sort_name",
              postgresql_using="gin", postgresql_ops={"sort_name": "gin_trgm_ops"}),
        Index("idx_item_aliases_trgm", "aliases",
              postgresql_using="gin", postgresql_ops={"aliases": "gin_trgm_ops"}),
        # 拼音搜索字段同款 trigram 索引（2026-10-24，批二 #16）：预筛对
        # pinyin_full / pinyin_initials 做 ILIKE 包含，与上面四列同一基建。
        Index("idx_item_pinyin_full_trgm", "pinyin_full",
              postgresql_using="gin", postgresql_ops={"pinyin_full": "gin_trgm_ops"}),
        Index("idx_item_pinyin_initials_trgm", "pinyin_initials",
              postgresql_using="gin",
              postgresql_ops={"pinyin_initials": "gin_trgm_ops"}),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 32 位稳定 ID（Emby 风格），由路径 md5 生成
    guid = Column(String(32), unique=True, nullable=False, index=True)
    library_id = Column(Integer, ForeignKey("emby_libraries.id"), nullable=False)
    item_type = Column(String(20), nullable=False)  # movie / series / season / episode
    name = Column(String(300), nullable=False)
    original_title = Column(String(300))
    sort_name = Column(String(300))
    overview = Column(Text)
    tagline = Column(String(300))

    # 层级
    parent_id = Column(Integer, ForeignKey("emby_items.id"))
    series_id = Column(Integer, ForeignKey("emby_items.id"))
    season_number = Column(Integer)
    episode_number = Column(Integer)
    production_year = Column(Integer)

    # 评分与元数据
    community_rating = Column(Float)
    official_rating = Column(String(20))  # 分级
    genres = Column(Text, default="")  # 逗号分隔
    tags = Column(Text, default="")
    studios = Column(Text, default="")
    # 发行平台（逗号分隔的平台 id）：扫描时从文件名/目录标签识别，供虚拟媒体库聚合
    platforms = Column(Text, default="")
    tmdb_id = Column(String(20), index=True)
    imdb_id = Column(String(20))
    # Q3 同剧治理：宽松重识别只跑一次（无论命中与否），打时间戳防无限重试。
    # NULL = 还没跑过宽松重识别。老库由 database._auto_migrate 补列。
    tmdb_loose_repair_at = Column(DateTime, default=None)
    # 多别名（逗号分隔）：TMDB 的 also known as / 原名等，供中英文与繁简搜索命中
    aliases = Column(Text, default="")
    # 拼音搜索派生字段（2026-10-24，FMBY 学习批二 #16）：由 name 生成的
    # 全拼 / 首字母紧凑键（「流浪地球」→ liulangdiqiu / lldq），供搜索预筛
    # 与精排的拼音层使用。派生数据、不许人工维护：落库钩子
    # fill_item_pinyin_fields 按 name 重算（见本文件后段），存量由
    # scripts/backfill_pinyin_search_fields.py 回填。与 sort_name 分工：
    # sort_name 是排序键，首字母只是寄生其上的旧搜索通道；这两列是搜索
    # 的正式落点，不受 PINYIN_SORT_ENABLED 开关影响。
    pinyin_full = Column(Text)
    pinyin_initials = Column(Text)
    # v2.51.0 演员刮削：TMDB details 的 origin_country / spoken_languages，
    # EA 详情页 Countries / Languages 用。逗号分隔；老库补列后为空串（= 以前的 []）。
    countries = Column(Text, default="")
    languages = Column(Text, default="")

    # 媒体文件
    file_path = Column(String(1024), index=True)
    # Drive file_id: Google Drive stable ID, unchanged on rename/move.
    # Scanner matches existing items by it: rename/move only updates
    # path, never deletes the record or loses metadata.
    drive_file_id = Column(String(64), index=True)
    # 身份锚（清单第 8 项，挂载模型第 3 步）：条目自身身份，不再只靠 mount:// 字符串。
    # source_id 指向 library_sources 行（无外键：源行被管理端删了重插后锚悬空，
    # 播放回落现行 mount:// 解析，见 item_anchor）；provider_file_id 是供应商侧文件 id
    # （Drive 即 drive_file_id、rclone 即远端 id（若有），115/本机为空）。
    # 两列均可空：老库补列后为空；新入库只写扫描时能确定的，不确定的留空绝不猜；
    # 存量回填是后续批次的事，本 PR 不写回填。
    source_id = Column(Integer, nullable=True)
    provider_file_id = Column(String(128), nullable=True)
    # Drive 链接分享（2026-10-19 播放事故治本）：播放 302 是 usercontent 匿名
    # 直链，要求文件有 anyone reader 权限。新文件入库后 drive_share 模块异步
    # 补开；enrich janitor 巡检兜底。NULL=尚未确认，True=已确认，False=待重试。
    drive_share_ok = Column(Boolean, default=None)
    drive_share_checked_at = Column(DateTime)  # 上次确认/补开尝试时间
    container = Column(String(20))
    size = Column(BigInteger, default=0)
    # 增量扫描（v2.50.0）：文件修改时间戳，用于 mtime 比对跳过未变更文件。
    # 为空（老数据）时视为已变更，走一次全量比对后回填。
    file_mtime = Column(Float, default=0)
    duration_ticks = Column(BigInteger, default=0)  # 100ns ticks
    # 元数据给的片长（TMDB runtime / episode_run_time、豆瓣 duration），只作探测前的
    # 展示回退：duration_ticks 是探测出的真实时长，二者分开存，互不覆盖，也不影响
    # 「缺时长 → 入队探测」的判定。
    metadata_runtime_ticks = Column(BigInteger, default=0)
    bitrate = Column(Integer, default=0)
    width = Column(Integer, default=0)
    height = Column(Integer, default=0)
    video_codec = Column(String(30))
    audio_codec = Column(String(30))
    audio_languages = Column(String(200), default="")
    subtitle_languages = Column(String(200), default="")
    # 文件名解析（v2.49.0）：扫描时从文件名提取，零 Drive 调用。
    # video_codec 复用已有列；这里新增分辨率与发行来源。
    video_resolution = Column(String(10))  # 480p / 720p / 1080p / 2160p
    media_source = Column(String(30))  # WEB-DL / BluRay / HDTV / DVDRip / WEBRip

    # 图片
    poster_path = Column(String(1024))  # 本地海报文件
    backdrop_path = Column(String(1024))
    primary_image_url = Column(String(1024))  # TMDB 刮削到的远程图
    backdrop_image_url = Column(String(1024))

    # 状态
    last_probed_at = Column(DateTime)  # 上次 ffprobe 时间，供刮削策略判断是否重探
    # moov position (v2.50.0): front=faststart, back=at end, NULL=non-MP4/unprobed
    moov_position = Column(String(10))
    # 两阶段扫描（v2.39.0）：Phase 1 只入库结构不做 ffprobe，需要探测的条目由
    # 后台 worker 按优先级探测。pending=待探测 probing=探测中 done=已探测 failed=放弃。
    probe_status = Column(String(20), default="pending")
    probe_priority = Column(Integer, default=0)  # 越大越先探；新文件 100，按需插队 1000
    probe_attempts = Column(Integer, default=0)  # 已尝试次数，超限转 failed
    probe_next_retry_at = Column(DateTime)  # 下次可重试时间（退避）
    # v2.53 探测 worker 重构：抢单租约 + 最近一次失败原因。
    # probe_claimed_at：标 probing 的时刻；超过 PROBE_CLAIM_TTL_SEC 仍是 probing
    # 视为抢单者已死（进程崩溃/线程卡死），由 reclaim 放回 pending。
    probe_claimed_at = Column(DateTime)
    probe_last_error = Column(String(255))
    last_scraped_at = Column(DateTime)  # 上次刮削时间，供 3m/6m/1y 策略判断是否到期
    # 数据库里有图片记录但本地文件丢失时置位，等待后台重新刮削修复
    repair_requested_at = Column(DateTime)
    is_hidden = Column(Boolean, default=False)
    date_added = Column(DateTime, default=datetime.now)
    premiere_date = Column(DateTime)
    date_modified = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    # 分层扫描（v2.40.0）：L1 只做文件发现与秒跳，L2/L3 后台补全元数据。
    # file_fingerprint: 文件指纹（本地=path|size|mtime_ns，远程=path|size 的 md5）。
    #   指纹命中且 enrich_status='done' 时整文件秒跳，不做任何 IO。
    # enrich_status: 补全状态 pending=待补全（L2/L3 排队中） done=已补全。
    #   「文件变没变」与「元数据全不全」解耦——旧 _can_skip_file 把条件绞在一起，
    #   配了 TMDB 的剧集库每轮都全量重做，增量形同虚设，这是根因。
    file_fingerprint = Column(String(64), index=True)
    enrich_status = Column(String(20), default='pending', index=True)
    # v2.40.0 补全 worker 重试：attempts=已尝试次数，超限转 failed；
    # next_retry_at=下次可重试时间（指数退避）。老库补列后默认 0/NULL。
    enrich_attempts = Column(Integer, default=0)
    enrich_next_retry_at = Column(DateTime)
    # v2.42.9 claim 租约：worker 抢到这条（置 enriching）的时刻。
    # 运行期崩溃的 worker 会留下永远 enriching 的僵尸行——旧实现只在启动时整体
    # 回收一次，运行期没有兜底。有了这个时间戳，janitor 可以周期扫描「租约超时」
    # 的行并打回 pending（见 enrich_worker._reclaim_stale），不必等重启。
    # 老库补列后为 NULL：视为「旧格式 enriching」，由 date_modified 近似判定。
    enrich_claimed_at = Column(DateTime)
    # 抢单令牌（v2.54）：原子认领的归属标记。_claim_batch 先按条件
    # UPDATE ... WHERE enrich_status='pending' 抢行（单条原子语句，PG/SQLite
    # 都不会重复认领），再用本批次的 token 把赢到的行查回来。
    # 老库补列后为 NULL；janitor/完成/失败释放租约时一并清空。
    enrich_claim_token = Column(String(32), default=None)
    # WS1/T1：最近一次补全失败的原因（成功即清零；资料不全超限写固定文案）。
    # 老库补列后为 NULL：历史 failed 行没有错误原文。
    enrich_last_error = Column(String(255))
    # WS1/T1：failed 自动回捞已执行的轮数。达上限（enrich_failed_max_reschedules）
    # 的行永久保留 failed，不再被 janitor 自动捞回。老库补列后为 0。
    enrich_failed_reschedules = Column(Integer, default=0)
    # Step 4 补强：none 自动回捞已执行的轮数。达上限（enrich_none_max_reschedules）
    # 的行永久保留 done，不再被 janitor 自动捞回（防搜不中的冷门条目无限空转）。
    # 老库补列后为 0。列模式仿 WS1/T1 的 enrich_failed_reschedules。
    enrich_none_reschedules = Column(Integer, default=0)
    # v2.42.9 处方 4 调度优先级：越大越先补全（0 = 默认，沿用旧口径）。
    # repair（用户主动修复）置 100；「重试未匹配项」置 50。只影响同库内的排序，
    # 跨库公平由按库轮转保证（见 enrich_worker._claim_batch）。
    # probe_priority 的先例：Integer 列即可，无需新表。
    enrich_priority = Column(Integer, default=0)
    # 元数据锁定（手动识别 P3，Emby 式）：管理员手动整理过的条目置 True，
    # 后台自动补全（enrich worker 抢单）不再碰它——自动刷新不会覆盖手动成果。
    # 手动操作（手动绑定 bind-tmdb、手动重刮 rescrape）不受锁定影响：
    # 锁定防的是「自动」，手动永远优先。
    # 老库补列后为 0（False）= 未锁定，行为与升级前完全一致（见 database._auto_migrate）。
    metadata_locked = Column(Boolean, default=False)
    # 元数据来源标记：这条条目的文字/图片**实际来自哪里**。
    # 取值：nfo=本地 NFO；tmdb=TMDB 搜索/详情；tmdb_img=NFO 给文字、TMDB 补图；
    # none=刮削跑过但没拿到数据（仍缺 tmdb_id/简介）。NULL=历史数据未标记。
    #
    # 以前只能靠 `last_scraped_at IS NULL` 反推"没刮到"，那把"没试过"和
    # "试过但失败"混为一谈；加上这个字段后，"刮没刮干净"可以直接查、
    # 可以按来源筛选，也为将来接入新数据源（豆瓣等）留出位置。
    metadata_source = Column(String(20))
    # 多源元数据（Phase 6b）：各源合并的外部 ID，JSON 对象
    # ``{"tmdb": "60300", "imdb": "tt0903747", "douban": "1292052", ...}``。
    # 为什么不用 tmdb_id / imdb_id 两列：那两个列已被扫描/求片链路直接读写
    # （含 ``is not None`` 之类的判空），塞进 7 个源会让那些判空全部失真。
    # 这里另存一份完整的，**只增不改**已有列；tmdb/imdb 同时回写那两列（引擎负责）。
    # 用途：跨源去重、换源时直接命中、改名后重新匹配。
    external_ids = Column(Text)
    # 软删除（v2.48.0）：清理阶段不再物理删条目，只写这个时刻。
    # NULL = 正常可见；非 NULL = 已下架。读路径由 backend/emby_server/soft_delete.py
    # 的全局 ORM 过滤器自动挡掉，文件重新出现时扫描器会把它清空（连播放进度一起回来）。
    deleted_at = Column(DateTime, default=None)
    # 缺失宽限（2026-10-24，FMBY 学习批一 #6 / 魂「对外部变化克制」）：
    # 清理阶段确认文件不在来源里时不再当轮下架，先累计缺失次数：
    # missing_since = 首次确认缺失的时刻，missing_count = 连续缺失轮数；
    # 达到 SCAN_MISSING_GRACE_SCANS（默认 3 轮）才真正写 deleted_at。
    # 文件重新出现即清零。单次观测（挂载抖动/目录半读）不作数，防误删。
    missing_since = Column(DateTime, default=None)
    # server_default 必须有：仓库里存在绕过 ORM 的裸 SQL/Core 直插
    # （如搜索测试的批量 INSERT），只靠 Python 侧 default 时直插会撞
    # NOT NULL——2026-10-24 CI 实证（test_search_pinyin_initials）。
    missing_count = Column(Integer, default=0, server_default="0", nullable=False)
    # 多版本合并（对标 StrmAssistant MergeMultiVersionTask）：
    # 同 tmdb_id/imdb_id 的重复条目合并为一条，主记录为 NULL，
    # 被合并的条目指向主记录 id。API 列表默认过滤掉被合并的，
    # 播放时可通过主记录查看/选择多版本。
    # NULL = 主记录（或未参与合并）。
    merged_into_id = Column(Integer, ForeignKey("emby_items.id"), default=None, index=True)


from sqlalchemy.orm import relationship  # noqa: E402

MediaItem.library = relationship("Library", foreign_keys=[MediaItem.library_id])
MediaItem.parent = relationship("MediaItem", remote_side="MediaItem.id", foreign_keys=[MediaItem.parent_id])
MediaItem.series = relationship(
    "MediaItem", remote_side="MediaItem.id", foreign_keys=[MediaItem.series_id]
)


class EmbyPerson(Base):
    """演员/主创（v2.51.0）：TMDB credits 刮削落库，供 EA 详情页返回 People。

    新表：由 ``create_all`` 自动建表，老库无需 ALTER（见 database._auto_migrate
    的注释：新表不在迁移列表里）。
    ``image`` 存头像**远程 URL**（唯一事实来源）：本地那份走 image_store 预热/
    按需自愈，与 MediaItem.primary_image_url 同口径——删缓存不丢数据。
    """

    __tablename__ = "emby_people"
    __table_args__ = (
        Index("idx_person_item_order", "item_id", "sort_order"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey("emby_items.id"), nullable=False, index=True)
    # name 建索引：/emby/Persons 按名字分组、/emby/Persons/{name}/Images/Primary
    # 按名字查，都走这个索引（演员行数 = 条目数 ×10，全表扫太贵）。
    name = Column(String(200), nullable=False, index=True)
    role = Column(String(200))       # 饰演角色（TMDB character）
    image = Column(String(1024))     # 头像远程 URL（TMDB profile_path 拼出来的）
    sort_order = Column(Integer, default=0)  # TMDB cast 原顺序（戏份排序）
    # StrmAssistant #9 对标：TMDB person id，供刷新演员详情用
    person_tmdb_id = Column(String(32), index=True)


class MediaStream(Base):
    """媒体流轨道（视频/音频/字幕）"""

    __tablename__ = "emby_media_streams"
    __table_args__ = (Index("idx_stream_item", "item_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey("emby_items.id"), nullable=False)
    stream_index = Column(Integer, default=0)
    stream_type = Column(String(10))  # Video / Audio / Subtitle
    codec = Column(String(30))
    language = Column(String(20))
    # StringDataRightTruncation（生产事故）：ffprobe 的 tags.title（多为流描述句，
    # 不受 200 字限制）与外挂字幕文件名都可能超 VARCHAR(200)，单条 INSERT 失败会
    # 把整个写事务带崩（扫描/探测/补全一起遭殃）。放宽到 500；写库前的截断保护见
    # ``MediaStream.sanitize_stream_strings``（before_insert 钩子，双保险）。
    display_title = Column(String(500))
    title = Column(String(500))
    is_default = Column(Boolean, default=False)
    is_forced = Column(Boolean, default=False)
    is_external = Column(Boolean, default=False)
    external_path = Column(String(1024))
    channels = Column(Integer)
    bit_rate = Column(Integer)
    # ffprobe 的逐流细节：客户端「媒体信息」页会直接显示这些，缺了就只剩编码/码率几行
    frame_rate = Column(String(20))        # 帧率（"29.970003" 这类原始值）
    video_range = Column(String(20))       # 动态范围：SDR / HDR10 / HLG / DolbyVision
    profile = Column(String(30))           # 编码 Profile：High / Main 10
    level = Column(String(30))             # 编码 Level：40 / 51
    pixel_format = Column(String(30))      # 像素格式：yuv420p10le
    aspect_ratio = Column(String(20))      # 画面比例：16:9
    bit_depth = Column(Integer)            # 位深：8 / 10 / 12
    sample_rate = Column(Integer)          # 音频采样率：48000
    channel_layout = Column(String(30))    # 声道布局：5.1
    sample_format = Column(String(20))     # 音频格式：fltp
    created_at = Column(DateTime, default=datetime.now)


MediaItem.streams = relationship(
    "MediaStream", foreign_keys=[MediaStream.item_id], cascade="all, delete-orphan"
)


# 字符串列宽：写库前截断保护的数据源（见 MediaStream.sanitize_stream_strings）。
# 长度取自上面的列定义——改列宽时这里自动跟着变，不会再出现两边漂移。
_MEDIA_STREAM_STR_LIMITS = {
    col.name: col.type.length
    for col in MediaStream.__table__.columns
    if isinstance(col.type, String) and col.type.length
}


def sanitize_stream_strings(target: "MediaStream") -> "MediaStream":
    """把超长的字符串字段截到列宽以内，绝不因 title 过长炸掉整条写事务。

    StringDataRightTruncation 的杀伤面是一条 INSERT 拖死一个事务：扫描里一个
    条目的轨道重建失败 = 同批所有条目写库失败；探测/补全 worker 同理。除了
    ffprobe 报的长描述，还有外挂字幕文件名（用户自己起的名字，长度不可控）。
    """
    for attr, limit in _MEDIA_STREAM_STR_LIMITS.items():
        value = getattr(target, attr, None)
        if value is not None and len(value) > limit:
            setattr(target, attr, value[:limit])
    return target


# 写库前最后一道闸：任何入口（扫描/探测/补全/未来的新代码）忘了预截断，
# 也不会再因一条 title 过长把整个事务拖死。before_insert 只覆盖走 ORM
# 单元工作（session.add）的写入 —— 与本仓库所有写入点一致。
event.listen(
    MediaStream, "before_insert",
    lambda mapper, connection, target: sanitize_stream_strings(target),
)


def fill_item_pinyin_fields(target: "MediaItem") -> "MediaItem":
    """按 name 重算拼音搜索派生字段（pinyin_full / pinyin_initials）。

    派生字段的唯一事实来源是 name：任何 ORM 写入（扫描入库/刮削改名/
    手动整理）在落库前统一重算，字段与标题永不漂移；生成走
    pinyin_sort.make_pinyin_fields（带 lru_cache，同名重算近零成本），
    与存量回填脚本共用同一入口。惰性 import 与 tmdb.py 引用
    make_sort_name 同款，避免模型层加载顺序问题。Core 直插/直改不走
    本钩子（与 sanitize_stream_strings 的覆盖面一致），靠回填脚本兜底。
    """
    from backend.emby_server.pinyin_sort import make_pinyin_fields

    full, initials = make_pinyin_fields(target.name or "")
    target.pinyin_full = full
    target.pinyin_initials = initials
    return target


# 条目落库收口：insert 与改名 update 都要让拼音派生字段跟随 name。
event.listen(
    MediaItem, "before_insert",
    lambda mapper, connection, target: fill_item_pinyin_fields(target),
)
event.listen(
    MediaItem, "before_update",
    lambda mapper, connection, target: fill_item_pinyin_fields(target),
)


class UserMediaData(Base):
    """用户媒体数据（播放进度 / 收藏 / 已看）"""

    __tablename__ = "emby_user_media_data"
    __table_args__ = (
        UniqueConstraint("user_id", "item_id", name="uq_umdata_user_item"),
        Index("idx_umdata_user", "user_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("web_users.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("emby_items.id"), nullable=False)
    playback_position_ticks = Column(BigInteger, default=0)
    # 历史最远进度（FMBY 学习批一 #4「进度双记」）：与 playback_position_ticks
    # 并存——position 是当前位置（拖回去重看会变小），max 是本轮观看的最远点，
    # 只增不减；完播/手动标记已看时与 position 一起清零（同一轮观看口径）。
    # 可空：NULL = 这行还没被双记写过（升级前老数据 / 迁移刚补列），
    # 读与算一律回退 playback_position_ticks，不靠回填（见 compat_routes 的
    # effective_max_position_ticks）。老库靠 database._auto_migrate 补列。
    max_position_ticks = Column(BigInteger)
    play_count = Column(Integer, default=0)
    played = Column(Boolean, default=False)
    is_favorite = Column(Boolean, default=False)
    # 「从继续观看隐藏」（客户端 HideFromResume）：只影响 Resume 列表，
    # 不动播放进度本身。老库靠 database 的 ORM 列对账自动补列（默认 0）。
    hide_from_resume = Column(Boolean, default=False)
    last_played_at = Column(DateTime)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class PlaybackSession(Base):
    """播放会话"""

    __tablename__ = "emby_playback_sessions"

    # 复合索引 (ended_at, last_update_at)：janitor 每 10 分钟执行
    #   WHERE ended_at IS NULL AND last_update_at < cutoff
    # ended_at 作为等值前缀、last_update_at 作为范围后缀，可命中索引范围扫描，
    # 避免表增长后全表扫描。
    __table_args__ = (
        Index("ix_emby_playback_sessions_ended_last_update", "ended_at", "last_update_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_key = Column(String(64), unique=True, index=True)
    # 阶段 3 一等会话（FMBY 播放全换）：服务端在 PlaybackInfo 时签发的一等
    # 会话 id。开关开时它同时写进 session_key（客户端上报、停播、租约身份
    # 都按这个键走，旧路径不用改键）；开关关时本列为空、行为与旧版逐字一致。
    # 老库靠 database 的 ORM 列对账自动补列（可空，老行留空 = 旧会话）。
    server_session_id = Column(String(64), unique=True, index=True)
    # PlaybackInfo 时确定的变体（第一条 = 现行主线）；变体切换仍由 #1208
    # 起播打点按次记录，不靠事后改会话行（客户端不回传变体选择）。
    variant = Column(String(50))
    # 设备稳定标识（X-Emby-Authorization DeviceId）：租约归属的推导键。
    # 流限流的身份口径（api._stream_client_identity）是“设备优先，其次
    # PlaySessionId，最后用户”；服务端签发会话时身份即 ses:<server_id>，
    # 客户端回传 PlaySessionId 的流请求租约就归属到这个会话。
    device_id = Column(String(100))
    lease_identity = Column(String(110))
    # 预建会话待首次上报：PlaybackInfo 预建的行在首次 Playing/Progress
    # 上报前不得跳过 share_guard 判定（见 compat_routes._upsert_session）。
    # 判定通过后由上报路径清零；开关关时本列恒 False，旧行为不变。
    awaiting_first_report = Column(Boolean, default=False)
    user_id = Column(Integer, ForeignKey("web_users.id"))
    item_id = Column(Integer, ForeignKey("emby_items.id"))
    device_name = Column(String(100))
    client_name = Column(String(100))
    client_version = Column(String(50))
    remote_addr = Column(String(64))
    play_method = Column(String(30))  # DirectStream / Transcode
    start_time = Column(DateTime, default=datetime.now)
    last_update_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    position_ticks = Column(BigInteger, default=0)
    is_paused = Column(Boolean, default=False)
    ended_at = Column(DateTime)


class PlayStartEvent(Base):
    """起播事件打点（FMBY 学习批一 #2，对齐 FMBY 的 playback_start_events）

    每次「起播尝试」在调度层记一行：用的哪家源（provider / mount_id）、
    走的哪条分发（method）、解析耗时（resolve_ms）、成败与错误类型。
    管理端按 provider × error_kind 聚合后能回答「起播慢是慢在哪家网盘、
    失败是哪类错误」——此前耗时只在 [play-timing] 日志里、计数只在
    line_stats 进程内，都没法事后聚合。

    写入走 play_events 的进程内队列 + 后台批量落库：起播热路径只做
    一次 put_nowait，写库失败静默丢弃，绝不拖慢起播本身。

    不加外键（照 ScanRun 先例）：条目/用户/挂载删掉后事件行要留着供
    统计，聚合时按 mount_id 尽力补挂载名，补不到不影响计数。
    """

    __tablename__ = "emby_play_start_events"
    __table_args__ = (
        Index("ix_play_start_events_provider_created", "provider", "created_at"),
        Index("ix_play_start_events_result_created", "result", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    user_id = Column(Integer)          # 发起播放的用户（解析事件经共用函数时可能为空）
    item_id = Column(Integer)          # emby_items.id（数值主键，非 guid）
    # 源维度：drive（Drive 直链/中继快路径）/ mount（挂载解析，细类看 mount_id
    # 关联的 StorageMount.mount_type）/ local（本机文件）/ unknown
    provider = Column(String(30), nullable=False, default="unknown")
    mount_id = Column(Integer)
    # 分发方式：relay（Drive SA 中继）/ direct_302（Drive 快路径直链）/
    # resolve（回落 _play_target 解析，含 mount 中继与本机文件的调度尝试）/
    # mount_relay / local_file（PR-2 按变体取流时 _play_target 的调度尝试
    # 按所选变体记，不再一律记 resolve；无变体参数时仍记 resolve）
    method = Column(String(30))
    # 实际走的变体名（FMBY 批二 #11 PR-2，见 play_events.note_play_start
    # 的 variant 参数）：drive_relay / drive_direct / mount_relay /
    # local_file；转码删除后不再有 transcode_hls。老事件与无变体调用为 NULL。
    variant = Column(String(30))
    result = Column(String(16), nullable=False, default="ok")  # ok / error
    # 错误分类：auth（凭据失效 503）/ mount_unavailable（挂载不可用 404）/
    # upstream_<状态码>（中继上游错误）/ resolve_error（未分类解析异常）
    error_kind = Column(String(40))
    resolve_ms = Column(Integer)       # 解析/调度段耗时（毫秒）
    total_ms = Column(Integer)         # 预留：整段起播耗时（本期未填）


class WorldWallPost(Base):
    """探险世界 · 到此一游留言墙"""
    __tablename__ = "world_wall_posts"
    __table_args__ = (Index("ix_world_wall_posts_created", "created_at"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("web_users.id"), nullable=False, index=True)
    nickname = Column(String(50))  # 留言时的用户名快照（展示用）
    content = Column(String(200), nullable=False)
    is_hidden = Column(Boolean, default=False, nullable=False, server_default="0")  # 后台删除=隐藏
    created_at = Column(DateTime, default=datetime.now)


class WorldFortuneDraw(Base):
    """探险世界 · 每日抽一签记录"""
    __tablename__ = "world_fortune_draws"
    __table_args__ = (
        Index("idx_fortune_user_date", "user_id", "draw_date", unique=True),
        Index("idx_fortune_date", "draw_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("web_users.id"), nullable=False, index=True)
    draw_date = Column(Date, nullable=False)  # 抽签日期（自然日，Asia/Shanghai）
    level = Column(String(20))  # 等级：上上签/上签/中签/下签
    text = Column(String(200))  # 签文
    yi = Column(String(100))  # 宜
    ji = Column(String(100))  # 忌
    created_at = Column(DateTime, default=datetime.now)


class ItemFacet(Base):
    """条目 ↔ 分类值（流派 / 工作室 / 标签 / 发行平台）关联表（v2.15.0）

    这些值原先只存在 :class:`MediaItem` 的逗号分隔文本列里，筛选时只能写
    ``genres ILIKE '%动作%'`` —— 前置通配符用不上任何索引，每次筛选都是整库扫描。
    现在由扫描器与元数据刮削在写条目的同时维护本表，筛选改成先在
    ``(kind, value, item_id)`` 上定位 item_id，再按 id 过滤条目（见 `facets.py`）。
    """

    __tablename__ = "emby_item_facets"

    __table_args__ = (
        # 筛选走这条：kind + value 定位候选条目
        Index("idx_item_facet_kind_value", "kind", "value", "item_id"),
        # 重建单个条目 / 清理孤儿行走这条
        Index("idx_item_facet_item", "item_id", "kind"),
        UniqueConstraint("item_id", "kind", "value", name="uq_item_facet"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey("emby_items.id"), nullable=False)
    # genre / studio / tag / platform
    kind = Column(String(16), nullable=False)
    value = Column(String(200), nullable=False)


class ScanDirState(Base):
    """增量扫描的目录指纹（v2.17.0；清单指纹 + 到期调度 v2.62.0）

    「追新」场景里绝大多数目录一个文件都没动过，但重扫会把里面每个文件重走一遍：
    找本地图片、找外挂字幕、重建外挂字幕轨、提交事务（实测 2000 个文件的重扫 ≈ 冷扫
    的 95%，约 4 条 SQL + 1 次 commit / 文件）。

    这里按目录存一份**清单指纹**（``fingerprint``，格式 ``v2:<md5>``）：对目录清单的
    元数据（本机：目录 mtime + 全部条目名；挂载：每条目的名/大小/远端 ModTime）
    做哈希——只用列举时本来就拿到的元数据，不为算指纹逐个打开文件。指纹没变的
    目录直接跳过逐文件的活，只把 guid 记进 ``seen_guids`` 并确认库里那一行
    存在且不需要重探/重刮——库中缺行、大小变了、要补图补详情都照旧完整处理，
    所以“跳过”永远不会漏掉还没入库的文件。详见 `scanner` 的增量扫描小节。

    **到期调度**（FMBY 学习批二 #12）：``next_due_at`` 是这个目录下一次「到期
    重验」的时间，``last_decision`` 是最近一轮对它下的判定（first / changed /
    due / not_due）。未到期且清单未变的目录整段跳过（连到期重刮这类「到期才做」
    的判定都推迟）；到期后走完整核对，核对完把 next_due_at 往后推一个周期。
    两个字段让「哪个目录何时到期、为何触发」在库里直接可查。
    """

    __tablename__ = "emby_scan_dirs"

    __table_args__ = (
        UniqueConstraint("library_id", "dir_key", name="uq_scan_dir_state"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    library_id = Column(Integer, nullable=False, index=True)
    # 本机绝对路径，或 mount://<挂载 id>/<相对路径>
    dir_key = Column(String(1000), nullable=False)
    fingerprint = Column(String(128), nullable=False)
    updated_at = Column(DateTime, default=datetime.now)
    # 到期调度（批二 #12）：NULL = 还没排期（按已到期处理，先完整核对再排期）
    next_due_at = Column(DateTime, default=None)
    # 最近一轮的目录判定：first / changed / due / not_due（可观测，见 scanner）
    last_decision = Column(String(32), default=None)


class ScanRun(Base):
    """一次媒体库扫描的记录（v2.23.0）

    只留「最近一次」不够解释「为什么这个库的内容一直没更新」：一个库每轮都失败、
    或只是最近一轮失败，处理方式完全不同。这里按库留最近若干轮的流水
    （状态 / 触发方 / 耗时 / 同一份统计 / 失败原因），管理端可查。

    保留条数由 ``scanner.SCAN_RUN_KEEP`` 控制（``EMBY_SCAN_HISTORY=0`` 关闭记录），
    每轮扫描结束后就地回收旧行；媒体库删掉后剩下的行由维护周期回收。
    """

    __tablename__ = "emby_scan_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # 不加外键：媒体库删除后这些行要能被维护周期清掉（而不是阻断删除）
    library_id = Column(Integer, nullable=False, index=True)
    # running / success / partial / failed / interrupted（与 Library.scan_status 同一套取值）
    status = Column(String(20), nullable=False)
    # 谁触发的：manual（面板按钮）/ client（Emby 客户端刷新）/ node（归属节点）/ repair（修复队列）。
    # 列名不叫 trigger：它在 MySQL / PG 里是保留字，裸写要处处加引号（属性名照旧叫 trigger）
    trigger = Column("trigger_kind", String(20))
    started_at = Column(DateTime, default=datetime.now)
    finished_at = Column(DateTime)
    duration_ms = Column(Integer)
    stats = Column(Text)          # JSON，与 Library.scan_stats 同一份编码
    # 检查点专属落点（FMBY 学习批一 #7）：定向扫描的续扫范围（resume_scope）。
    # 以前借住 stats，但 stats 在收尾时会被整轮统计覆写，范围只能靠白名单
    # 夹带续命；这一列只由 begin_scan 写、收尾永不触碰，被打断后
    # interrupted_resume_prefixes 先读它、老行（无此列值）再回退 stats。
    state_json = Column(Text)     # JSON，编码与 stats 同一套（encode_scan_stats）
    error = Column(String(500))   # partial / failed 的原因摘要

    __table_args__ = (
        # 跨进程防重（FMBY 学习批一 #7）：同库同时只许一条 running 流水。
        # worker 与 EA 是两个进程，各自的进程内互斥（scanner._acquire_scan）
        # 挡不住跨进程同扫一个库，最后一道闸落在库层：begin_scan 插入撞上
        # 这条部分唯一索引 → 抛 ScanInProgress（见 scanner.begin_scan）。
        # 存量库由 database._migrate_scan_run_single_running 先收尾同库重复
        # 的僵尸 running 行再补建同名索引（顺序反了 CREATE INDEX 会失败）。
        Index("uq_scan_runs_one_running", "library_id", unique=True,
              sqlite_where=text("status = 'running'"),
              postgresql_where=text("status = 'running'")),
    )


#: 识别审查队列（FMBY 学习批二 #15）的 reason / status 允许值——**全仓唯一
#: 定义点**。下方 IdentifyReviewTask 的 CheckConstraint SQL 直接由这两个元组
#: 拼出，identify_review 的代码校验也只读这两个元组：约束与枚举同源、永不
#: 漂移。FMBY 自己在这条链上栽过：入队写的 reason_code 不在 DB CHECK 允许
#: 集里，「识别→审核」整条链静默断裂——本仓用同源生成从结构上堵死这坑。
#: 新增取值只许改这里（约束随之下轮建表/迁移生效，代码校验立即生效）。
IDENTIFY_REVIEW_REASONS = ("no_confident_match",)
IDENTIFY_REVIEW_STATUSES = ("pending", "bound", "ignored", "requeued")


def _in_sql(column: str, values: tuple) -> str:
    """把允许值元组拼成 CHECK 约束用的 IN (...) SQL 片段（值均为内部常量）。"""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class IdentifyReviewTask(Base):
    """低置信度识别的人工审查队列（FMBY 学习批二 #15；魂 1 身份与证据先行、
    魂 6 状态集中维护）。

    TMDB 真的搜过、有候选、但无一过置信线（search 返回 None）的条目，旧
    口径直接终态 none、候选当场丢弃——管理员事后想人工认片，手里什么都
    不剩。开启审查队列（env IDENTIFY_REVIEW_QUEUE_ENABLED=1）后，这类条目
    在终态化的同一事务里入队：候选与分数的证据快照（evidence_json）随行
    保存，管理员在后台逐条处置（绑定到指定 TMDB / 忽略 / 重跑识别）。

    不加外键（照 ScanRun 先例）：条目删掉后审查行留着供追溯，条目名/年份
    在入队时快照进本行，列表不必回查条目表。
    """

    __tablename__ = "emby_identify_review_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now,
                        nullable=False)
    item_id = Column(Integer, nullable=False, index=True)  # emby_items.id（非 guid）
    # 入队时条目快照：管理员列表直接可读，条目改名/删除不影响追溯
    item_name = Column(String(500))
    item_type = Column(String(20))       # series / movie
    item_year = Column(Integer)
    # 取值只许出自 IDENTIFY_REVIEW_REASONS / IDENTIFY_REVIEW_STATUSES，
    # 约束 SQL 由同元组拼出（见上方注释），代码与 DB 永不漂移。
    reason = Column(String(40), nullable=False)
    status = Column(String(20), nullable=False, default="pending")
    # 证据快照 JSON：{query, kind, searched_year, captured_at,
    # candidates: [{tmdb_id, title, original_title, year, tier, rank,
    # popularity, poster_path}]}——tier 为 null 即被置信线拒掉的候选。
    evidence_json = Column(Text)
    resolved_by = Column(Integer)        # web_users.id（处置管理员）
    resolved_at = Column(DateTime)
    resolved_tmdb_id = Column(String(32))  # status=bound 时绑定的 TMDB ID

    __table_args__ = (
        CheckConstraint(_in_sql("reason", IDENTIFY_REVIEW_REASONS),
                        name="ck_identify_review_reason"),
        CheckConstraint(_in_sql("status", IDENTIFY_REVIEW_STATUSES),
                        name="ck_identify_review_status"),
        # 幂等闸：同一条目同时只许一条 pending——重复识别收尾时更新既有行
        # 的证据，不许堆多条 pending（与 uq_scan_runs_one_running 同款）。
        Index("uq_identify_review_one_pending", "item_id", unique=True,
              sqlite_where=text("status = 'pending'"),
              postgresql_where=text("status = 'pending'")),
        Index("ix_identify_review_status_created", "status", "created_at"),
    )


#: 上游源（FMBY 学习 #25 25-1）的 source_type / auth_method /
#: credential_scope / health_status 允许值——**全仓唯一定义点**。下方
#: UpstreamSource 的 CheckConstraint SQL 直接由这四个元组拼出，
#: upstream_sources 的代码校验也只读这四个元组：约束与枚举同源、永不
#: 漂移（FMBY 自己在 reason_code 上栽过漂移 bug 的教训，见上）。
#: 新增取值只许改这里。
#:
#: 证据口径（2026-10-24 追加，见 upstream_sources 模块头）：
#: - source_type：第六阶段实测只建过 Emby 型；jellyfin 列入是用户
#:   2026-10-24 亲定的首批口径（Emby/Jellyfin 同 API 族），非第六阶段
#:   单独实测；apple_cms 明确后置（第六阶段只见其许可证项，目录同步
#:   与播放均未实测），不在允许集里——传它直接被拒。
#: - auth_method：第六阶段实测是账号密码密封；api_key 是 Emby 协议的
#:   标准形态、未在第六阶段上游建源时单独实测，列入但报告里标待复验。
#: - credential_scope：源级一把（source）与用户映射（user）两个取值。
#:   user 取值由 2026-10-24 用户亲令“那个项目怎么做的，咱们照抄”
#:   显式重开（照抄定稿：BYOS-1 OAuth 先行、挂载稿 §4.2 凭据归两处），
#:   25-1 原话“按用户映射无证据不许悄悄定死”至此解除——重开走的是
#:   用户裁定，不是悄悄定死。默认仍为 source（upstream_sources
#:   创建口默认值不变），user 行只由挂载凭据表（mount_credentials）
#:   与 BYOS 绑定线使用。
#: - health_status：占位字段（探活是 25-3 的活），取值集先定死，25-3
#:   的工人只许写这三个值，不许自造第四种状态。
UPSTREAM_SOURCE_TYPES = ("emby", "jellyfin")
UPSTREAM_AUTH_METHODS = ("password", "api_key")
UPSTREAM_CREDENTIAL_SCOPES = ("source", "user")
UPSTREAM_HEALTH_STATUSES = ("unknown", "healthy", "unhealthy")

#: BYOS 用户存储绑定（P1）：provider 枚举全仓唯一定义点，首批只列
#: drive、pan115，其余后加；枚举与约束同源（FMBY reason_code 漂移
#: 教训），不许在别处手写字符串。drive 的绑定验证随 P2（OAuth）
#: 上线，P1 的创建口只接受 pan115（见 user_bindings）。
USER_STORAGE_PROVIDERS = ("drive", "pan115")
#: 绑定状态：active（验活通过）/ invalid（凭据错或被限）/
#: expired（登录态过期）。未验活不置 active（设计稿 §3.2）。
USER_STORAGE_BINDING_STATUSES = ("active", "invalid", "expired")
#: 绑定失效三档（照抄定稿 BYOS-4 语义）：凭据错 / 过期 / 风控。
#: last_error_kind 只许写这三个值，不许统一写“绑定失败”。
USER_STORAGE_BINDING_ERROR_KINDS = ("auth_error", "expired", "throttled")


class UpstreamSource(Base):
    """上游源（FMBY 学习 #25 25-1：只做源管理，不做聚合/探活/播放接入）。

    学的是 FMBY 第六阶段实测的上游源形态（只学机制、不抄闭源代码）：
    一行 = 一个别人的媒体服务（Emby/Jellyfin 型），字段与 FMBY 实测的
    ``upstream_sources`` 表对齐：source_type、base_url、auth_method、
    sealed_secret（密封凭据）、extra_headers_json、user_agent、referer。
    FMBY 那张表在第六阶段实测里没有 enabled / 健康状态列——本表的
    enabled 与 health_status 是我方治理面补的字段（默认关、状态未知），
    不是 FMBY 实测值，报告里已显式标出，25-3 探活工人只许写
    UPSTREAM_HEALTH_STATUSES 里的值。

    凭据纪律（魂 1 + 凭据不外泄）：
    - 凭据只存密封值（``sealed_secret``，Fernet 密封，密钥派生自
      SECRET_KEY），接口与日志永远只出 ``has_secret`` 布尔值，不出
      凭据原文、不出密封值本身；
    - 源级一把凭据（credential_scope 恒为 ``source``）：一个源配一份
      管理员录入的凭据，替全站向该上游请求；按用户映射的模型在
      FMBY 第六阶段无证据，属待裁定项，不在本表留第二套路径；
    - 密封密钥随 SECRET_KEY 走：SECRET_KEY 一旦更换，旧密封值解不开，
      视为凭据失效、管理员须重录，绝不静默回落明文或空凭据。

    默认关：新建源 ``enabled`` 恒为 False（创建接口不接受 enabled 入参），
    启用是单独的显式动作；关着的源对全站行为逐字无影响——25-2 聚合
    工人与 25-4 播放接入将来只许读 enabled 的源，关着的源连目录都
    不许拉（该约束由调用方落实，本表只保证默认值）。

    不加外键（照 ScanRun 先例）：25-2/25-3 落地后，源删了，目录与
    线路行要留着供追溯与对账，删源时是否联动清理由 25-2/25-3 的
    设计定，本步删源只删本行（见 upstream_sources.delete_source）。
    """

    __tablename__ = "upstream_sources"

    __table_args__ = (
        CheckConstraint(_in_sql("source_type", UPSTREAM_SOURCE_TYPES),
                        name="ck_upstream_source_type"),
        CheckConstraint(_in_sql("auth_method", UPSTREAM_AUTH_METHODS),
                        name="ck_upstream_auth_method"),
        CheckConstraint(_in_sql("credential_scope", UPSTREAM_CREDENTIAL_SCOPES),
                        name="ck_upstream_credential_scope"),
        CheckConstraint(_in_sql("health_status", UPSTREAM_HEALTH_STATUSES),
                        name="ck_upstream_health_status"),
        Index("ix_upstream_sources_enabled", "enabled"),
        Index("ix_upstream_sources_type", "source_type"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(80), unique=True, nullable=False)
    source_type = Column(String(20), nullable=False)  # emby / jellyfin
    base_url = Column(String(500), nullable=False)  # 已归一化：去尾斜杠
    auth_method = Column(String(20), nullable=False, default="password")
    username = Column(String(200))  # password 形态必填；api_key 形态为空
    # 密封凭据（Fernet，见 upstream_sources.seal_secret）：密码形态存密码、
    # api_key 形态存 api_key；为空 = 该源未配凭据（匿名访问上游）。
    sealed_secret = Column(Text)
    # 附加请求头 JSON（对象，键值均为字符串），25-2/25-4 向该上游发请求
    # 时原样带上；不是凭据通道——凭据只走 sealed_secret。
    extra_headers_json = Column(Text)
    user_agent = Column(String(300))
    referer = Column(String(500))
    # 我方治理字段（非 FMBY 实测列）：默认关；健康状态占位，探活是 25-3。
    enabled = Column(Boolean, nullable=False, default=False)
    credential_scope = Column(String(20), nullable=False, default="source")
    health_status = Column(String(20), nullable=False, default="unknown")
    last_checked_at = Column(DateTime)  # 25-3 探活工人写，25-1 不写
    note = Column(String(500))
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now,
                        nullable=False)


class EnrichFlipEvent(Base):
    """enrich done→pending 批量翻转的告警事件（v2.55.x）

    背景（2026-10-11 生产事故）：17:52 发生 5723 条 done→pending 翻转，
    触发源未知；旧容器在 17:54 部署时被重建，docker 日志丢失，无法追查。
    此后任何单次批量翻转（≥ ENRICH_FLIP_ALERT_THRESHOLD，默认 100 条）
    都在这里留一条持久记录：
    - 存在 PG，容器重建 / 日志轮转都丢不掉 —— 这就是「现场保留」；
    - /api/health 暴露 enrich_flip_last_hour 指标；
    - 管理端可按库、按时间查「谁把多少条翻回去了」。
    由 ``create_all`` 自动建表，老库无需 ALTER（见 database._auto_migrate）。
    """

    __tablename__ = "enrich_flip_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    # 哪次扫描触发的（扫描之外的触发源如 retry_unmatched 为空）
    library_id = Column(Integer, nullable=True, index=True)
    scan_run_id = Column(Integer, nullable=True)
    # 触发源：scan（扫描器 size_changed 等）/ retry_unmatched（管理端手动重试未匹配）
    trigger = Column(String(40), nullable=False, default="scan")
    flip_count = Column(Integer, nullable=False)
    threshold = Column(Integer, nullable=False)  # 触发时的告警阈值（可配置，会变）
    details = Column(Text)  # JSON：{reasons: {size_changed: N}, item_types: {...}, sample_ids: [...]}


class StorageMount(Base):
    """存储挂载：媒体库的内容来源

    一个挂载 = 一种「把内容接到媒体库上」的方式。挂载本身不拥有条目，
    媒体库通过 `Library.mount_ids` 引用它，因此同一个挂载可以被多个库共用。

    支持的类型（见 ``mounts.MOUNT_TYPES``，v2.42.12 起砍到三种，``drive`` 是之后
    按同一套提供者接口新加的第四种）：

    - ``local``：本地硬盘。路径以 ``/media`` 开头；目录里的 ``.strm`` 文件照样扫描成直链；
    - ``115``：115 网盘。路径以 ``115:/`` 开头，Cookie 型 API 直接读网盘目录，
      **不需要本地挂载**；
    - ``rclone``：rclone 任意后端。路径以 ``rclone:`` 开头，rclone.conf 由用户自己粘贴。
    - ``drive``：Google 云盘。路径以 ``drive:`` 开头（后接共享盘 ID），Drive API
      直读、凭据走服务账号轮换，实现在 ``mount_drive.py``。

    远程挂载的媒体由 EA 按 Range 代理转发，播放地址不落到客户端，Cookie / 令牌不下发
    （带 drive_file_id 的条目例外，api 层直接 302 到 Drive 直链）。
    """

    __tablename__ = "storage_mounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    # 属于哪个服（server_realms.id）：挂载是主机相对的资源，跟着服走
    realm_id = Column(Integer)
    # 由哪台 EA（remote_servers.id，kind='ea'）实际去读这个来源。**可空**：
    # 空 = 老数据，回退到该服已激活的 EA，再不济用共享的那份 rclone.conf。
    # 有了它，「每台 EA 一份 rclone.conf」才有地方挂：配置跟着跑它的那台机器走。
    server_id = Column(Integer, index=True)
    # local / 115 / rclone / drive。**由 path 的前缀决定**（mounts.detect_mount_type），
    # 保留这一列是因为已有数据与大量查询按它分组，不再允许手工改。
    mount_type = Column(String(20), nullable=False, default="local")
    # local：/media 下的本机目录；115：115:/…；rclone：rclone:remote/路径；
    # drive：drive:<共享盘 ID>（个人盘 drive:myDrive:<remote 名>）
    path = Column(String(1024), default="")
    # 类型相关配置（JSON 文本）：115 的 cid/账号、rclone 的调用方式、drive 的 root_id 等
    config = Column(Text, default="{}")
    is_enabled = Column(Boolean, default=True)
    remark = Column(String(300), default="")
    # 最近一次连接测试结果（仅展示，不参与鉴权判断）
    last_checked_at = Column(DateTime)
    last_check_ok = Column(Boolean)
    last_check_message = Column(String(300))
    # 五能力位声明（FMBY 挂载模型地基，2026-10-24 清单第 1 项）。
    # 只声明、不执法：上层按能力门控是清单第 2 项，另开 PR；本步任何
    # 消费方都不许读这五列做分支判断，行为与今天逐字一致。列值由
    # 回填按 mount_type 从 ProviderCapabilities 声明派生（见
    # mount_sources.capabilities_for_mount_type），不是用户手填；
    # 老库补列走 database._auto_migrate（BOOLEAN 默认 0）。
    can_list = Column(Boolean, default=False)
    can_random_read = Column(Boolean, default=False)
    can_read_sidecar = Column(Boolean, default=False)
    can_generate_play_target = Column(Boolean, default=False)
    can_refresh_credentials = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class LibrarySource(Base):
    """媒体库来源行（FMBY 挂载模型地基，清单第 1 项：建源表）。

    一行 = 一个库的一个来源：挂载 + 子目录，或本机目录。
    与 mounts.LibrarySource（扫描来源 dataclass）同名但不同物：本类是
    落库的 ORM 行，那个是扫描期内存对象；新模块一律用 em.LibrarySource
    限定引用，不许裸 import 混用。

    不变量（模型层，不是消费方过滤）：同一库下（mount_id, sub_path）
    不重复——paths 子目录与 mount_ids 整挂载并存的危险组合在模型层
    即不可能（回填时子目录行胜出并记警告，见 mount_sources）。
    mount_id 为空 = 本机目录行，sub_path 存本机路径。无外键（照
    upstream_sources 先例：源删了行留着供追溯，删源联动清理另议）。
    NULL mount_id 在 PG/SQLite 唯一约束里互不相等，故本机行的去重
    靠回填幂等（按三元组查已存在再插），不靠约束兜底。
    """

    __tablename__ = "library_sources"

    __table_args__ = (
        UniqueConstraint("library_id", "mount_id", "sub_path",
                         name="uq_library_sources_library_mount_subpath"),
        Index("ix_library_sources_library", "library_id"),
        Index("ix_library_sources_mount", "mount_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    library_id = Column(Integer, nullable=False)
    mount_id = Column(Integer, nullable=True)  # 空 = 本机目录
    sub_path = Column(String(1024), nullable=False, default="/")
    include_paths = Column(Text, default="")
    exclude_paths = Column(Text, default="")
    scan_priority = Column(Integer, default=0)
    enable_detail_prefetch = Column(Boolean, default=False)
    is_enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)


class MountCredential(Base):
    """挂载凭据行（FMBY 挂载模型地基，清单第 1 项：凭据分家）。

    一行 = 一个挂载的一份凭据：scope=source（服级共享）或 scope=user
    （用户绑定，owner_user_id 为绑定用户）。凭据本体只存 25-1 密封值
    （Fernet，见 upstream_sources.seal_secret）；key_id 是信封密钥编号：
    地基阶段记 25-1 密封方案标识（fernet-v1），KEK 文件信封
    （key_id → MOUNT_KEK_DIR 下密钥文件）是清单第 4 步，另开 PR。
    status：active / invalid / expired；last_error_kind 分三档
    （凭据错 / 过期 / 风控），不许统一写“绑定失败”。
    地基阶段只建表、不迁移任何现存凭据（115 Cookie、SA 引用迁入
    是第 4 步），旧凭据路径保持唯一权威。

    BYOS P1 补充：绑定凭据行不附属于任何挂载（凭据归用户账号，
    不归挂载行），故 mount_id 可空（NULL）。唯一约束
    （mount_id, scope, owner_user_id）在 PG/SQLite 里对 NULL 行
    天然豁免（NULL 互不相等），于是挂载附属行仍受“一挂载一 scope
    一用户一行”约束，绑定行则一绑定一行、不受其限——P1 前该约束
    曾使同一用户第二个绑定直接报 UNIQUE 冲突（实测），改可空是
    修正而非放宽：挂载附属行的约束语义逐字不变。
    """

    __tablename__ = "mount_credentials"

    __table_args__ = (
        CheckConstraint(_in_sql("scope", UPSTREAM_CREDENTIAL_SCOPES),
                        name="ck_mount_credential_scope"),
        UniqueConstraint("mount_id", "scope", "owner_user_id",
                         name="uq_mount_credentials_mount_scope_owner"),
        Index("ix_mount_credentials_mount", "mount_id"),
        Index("ix_mount_credentials_owner", "owner_user_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    mount_id = Column(Integer, nullable=True)  # BYOS 绑定行：NULL（不附属挂载）
    scope = Column(String(20), nullable=False, default="source")
    owner_user_id = Column(Integer, nullable=True)  # scope=user 时必填
    sealed_credential = Column(Text)
    key_id = Column(String(64))
    status = Column(String(20), default="active")
    last_verified_at = Column(DateTime)
    last_error_kind = Column(String(100))
    created_at = Column(DateTime, default=datetime.now)


class UserStorageBinding(Base):
    """BYOS 用户存储绑定（P1：绑定管理 + 115 直链播放）。

    一行 = 一个用户绑的一个网盘账号（用户-facing 的绑定记录）。
    凭据密文本体**不**存在本表：只存 ``credential_id`` 指向
    ``mount_credentials`` 的一行（scope=user、owner_user_id=本用户），
    密封、key_id 信封、状态机都在凭据行——不另起第三套秘密存储
    （挂载稿 §4.2 拼法：绑定行引用凭据行）。

    - provider：USER_STORAGE_PROVIDERS 单点枚举（首批 drive/pan115）；
    - account_label：用户自己起的名字，仅展示用；同一用户同一
      provider 下不重复（唯一约束），不同 provider 可重名；
    - status：active 须经验证后才置（绑完先验活，见 user_bindings）；
      invalid（凭据错/被限）/ expired（登录态过期）；
    - last_error_kind：三档（auth_error / expired / throttled），
      文案见 user_bindings.BINDING_ERROR_MESSAGES，不许统一写
      “绑定失败”；
    - 解绑 = 删本行 + 其凭据行，不留残留（user_bindings
      .delete_binding，单事务）；
    - 本表不存任何凭据原文与密封值：接口与日志只出 has_credential
      布尔值，credential_id 是内部行号、不进用户端响应。
    """

    __tablename__ = "user_storage_bindings"

    __table_args__ = (
        CheckConstraint(_in_sql("provider", USER_STORAGE_PROVIDERS),
                        name="ck_user_storage_binding_provider"),
        CheckConstraint(_in_sql("status", USER_STORAGE_BINDING_STATUSES),
                        name="ck_user_storage_binding_status"),
        UniqueConstraint("user_id", "provider", "account_label",
                         name="uq_user_storage_binding_label"),
        Index("ix_user_storage_bindings_user", "user_id"),
        Index("ix_user_storage_bindings_credential", "credential_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("web_users.id"), nullable=False)
    provider = Column(String(20), nullable=False)
    account_label = Column(String(100), nullable=False)
    credential_id = Column(Integer, ForeignKey("mount_credentials.id"),
                           nullable=False)
    status = Column(String(20), nullable=False, default="invalid")
    last_verified_at = Column(DateTime)
    last_error_kind = Column(String(100))
    created_at = Column(DateTime, default=datetime.now)


class Pan115Account(Base):
    """115 账号配置档（Cookie 型，不依赖 115 OpenAPI）

    一个站点可以配置多个命名账号（如「主号」「影库专号」），其中一个作为默认账号；
    媒体库可以单独绑定账号（`Library.account_115_id`），未绑定时回退到默认账号，
    再回退到服务器级 `PAN115_COOKIE`（兼容历史上只在 .env 里放一个 Cookie 的部署）。
    """

    __tablename__ = "pan115_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), unique=True, nullable=False)
    cookie = Column(Text, nullable=False)
    # 该配置档发请求时用的 UA（预置下拉里选，留空用服务器级 MOUNT_UA）。
    # 115 的直链接口对 UA 有偏好，不同配置档可以指向不同设备。
    ua = Column(String(300), default="")
    is_default = Column(Boolean, default=False)
    is_enabled = Column(Boolean, default=True)
    remark = Column(String(300), default="")
    # 最近一次校验结果（仅展示，不参与鉴权判断）
    last_verified_at = Column(DateTime)
    last_verify_ok = Column(Boolean)
    last_verify_message = Column(String(300))
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class EmbyApiToken(Base):
    """Emby 客户端 Access Token

    P1 安全说明：``token`` 列存的是 SHA256 哈希（64 位 hex），不是明文。
    签发时把明文给客户端，校验时对输入哈希后再比对（见 auth.hash_emby_token）。
    """

    __tablename__ = "emby_api_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String(64), unique=True, index=True, nullable=False)  # SHA256 hex，非明文
    user_id = Column(Integer, ForeignKey("web_users.id"), nullable=False)
    device_id = Column(String(100))
    app_name = Column(String(100))
    app_version = Column(String(50))
    last_ip = Column(String(64))
    created_at = Column(DateTime, default=datetime.now)
    last_used_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    is_revoked = Column(Boolean, default=False)
    # token 有效期：新签发默认 30 天（见 play_sign.token_expiry_default）；
    # 为空 = 历史 token，视为永不过期（不强制老客户端重新登录）。
    # 列由 _backfill_orm_columns 幂等补齐，无需手写迁移。
    expires_at = Column(DateTime, nullable=True)


# 软删除的全局可见性过滤器（v2.48.0）在这里装上：它注册在 sqlalchemy.orm.Session
# 类上，所以只要 MediaItem 这个模型被导入过，它就生效——也就是说，本仓库里任何
# 会查条目的代码（不只 emby_server 下的）都自动看不到已下架的条目。
from backend.emby_server import soft_delete as _soft_delete  # noqa: E402,F401


class IntroMarker(Base):
    """片头片尾标记（对标 StrmAssistant #3）。

    marker_type: intro（片头）/ outro（片尾）/ credits（字幕）
    时间单位：毫秒（与 Emby Chapter 标记对齐）。
    数据来源：manual（手动标记）/ auto（自动探测，预留）
    """
    __tablename__ = "emby_intro_markers"
    # item_id 列级已有 index=True，这里不再重复建（避免同一列两个索引）
    __table_args__ = ()

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey("emby_items.id"), nullable=False, index=True)
    marker_type = Column(String(20), nullable=False)  # intro / outro / credits
    start_ms = Column(BigInteger, nullable=False, default=0)
    end_ms = Column(BigInteger, nullable=False, default=0)
    source = Column(String(20), default="manual")  # manual / auto
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)


class IntroSegment(Base):
    """IntroDB 自动片头片尾分段缓存（2026-10-17 学 raisemby 接入）。

    segment_type: intro（片头）/ recap（前情提要）/ outro（片尾）/
    credits（字幕）/ post-credits（片尾彩蛋）；segment_type='none' 是
    负缓存哨兵行（拉过但上游没分段）。
    时间单位：毫秒（与 IntroMarker 对齐，直接进 to_chapters 下发）。
    缓存 TTL：正缓存 30 天、负缓存 7 天（见 introdb.py），按 fetched_at 判。
    新表由 Base.metadata.create_all 自动建。
    """
    __tablename__ = "emby_intro_segments"
    __table_args__ = ()

    id = Column(Integer, primary_key=True, autoincrement=True)
    item_id = Column(Integer, ForeignKey("emby_items.id"), nullable=False, index=True)
    segment_type = Column(String(20), nullable=False)  # intro/recap/outro/credits/post-credits/none
    start_ms = Column(BigInteger, nullable=False, default=0)
    end_ms = Column(BigInteger, nullable=False, default=0)
    source = Column(String(20), default="introdb")  # introdb
    fetched_at = Column(DateTime, default=datetime.now)


class ChaseFileSnapshot(Base):
    """追新文件指纹快照（P0-2）：文件级 diff 取代目录 mtime 过滤。

    生产实证：Drive 上季目录 mtime=2026-07-30，但其内新剧集文件 mtime=2026-08-08，
    旧实现按目录 mtime 整个跳过 → 新剧集永远发现不了。改为文件级 (size, mod_ts)
    快照 diff：快照无记录=新增；size 或 mod_ts 变化=变更。
    """

    __tablename__ = "chase_file_snapshot"

    library_id = Column(Integer, primary_key=True)
    # mount://<id>（远程挂载）或本地绝对路径（本机目录）
    source_key = Column(String(512), primary_key=True)
    # 挂载内相对路径（以 / 开头）或相对本地根的路径
    rel_path = Column(String(1024), primary_key=True)
    size = Column(BigInteger, default=0)
    mod_ts = Column(Float, default=0.0)
    first_seen_at = Column(DateTime)
    last_seen_at = Column(DateTime)


class ChaseRun(Base):
    """追新运行历史（P1-1）：poll（change_watcher）与 drive-changes 每轮各写一行。"""

    __tablename__ = "chase_run"

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime, nullable=False)
    finished_at = Column(DateTime)
    source = Column(String(20), default="poll")  # poll / drive-changes
    libs_checked = Column(Integer, default=0)
    files_listed = Column(Integer, default=0)
    new_found = Column(Integer, default=0)
    scans_triggered = Column(Integer, default=0)
    status = Column(String(10), default="ok")  # ok / partial / fail
    error = Column(Text)


class ChaseSourceState(Base):
    """追新源状态（P1-1）：每个监听源（挂载/本地目录）一行。

    last_ok_at 替代内存态 since_ts（容器重建不丢失）；last_snapshot_at 控制远程
    快照 diff 每源每小时最多一次；consec_failures>=3 → 管理后台告警。
    """

    __tablename__ = "chase_source_state"

    # mount://<id>（远程挂载）或本地绝对路径
    source_key = Column(String(512), primary_key=True)
    last_ok_at = Column(DateTime)
    last_snapshot_at = Column(DateTime)
    consec_failures = Column(Integer, default=0)
    last_error = Column(Text)


class LibraryListProjection(Base):
    """媒体库列表投影行（FMBY 列表投影读模型，清单第 16 项试点）。

    一行 = 一个非虚拟启用库在 user_views 列表页上现算的那三个值：
    条目数（ChildCount）、最早入库时间（DateCreated 回退源）、封面 tag。
    读侧只读本表（一次 IN 查询），不现算；写侧由周期重建工人
    （见 list_projection）统一刷新，不由任何写路径（扫描/入库/绑定）
    逐个打脏——试点期零写路径改动。

    最终一致、延迟可观测：computed_at 是本行算出的时刻；读路径按
    LIST_PROJECTION_MAX_STALE_SEC 判过期，过期/缺行/读错一律回落
    现算并记警告，绝不静默出旧数（FMBY #19 实测教训：投影延迟肉眼
    可见，界面不许假装实时）。

    无外键（照 LibrarySource 先例）：库删了行由重建清理，不靠级联。
    新表由 init_db 的 create_all 建出，不走 _auto_migrate（该函数只补
    已有表的列，见 database.py 注释“新表由 create_all 建，不在此列”）。
    """

    __tablename__ = "emby_library_list_projection"

    __table_args__ = (
        UniqueConstraint("library_id", name="uq_library_list_projection_library"),
        Index("ix_library_list_projection_library", "library_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    library_id = Column(Integer, nullable=False)
    child_count = Column(Integer, nullable=False, default=0)
    earliest_added = Column(DateTime, nullable=True)
    cover_tag = Column(String(64), nullable=False, default="1")
    computed_at = Column(DateTime, nullable=False, default=datetime.now)


__all__ = [
    "Library",
    "ScanRun",
    "MediaItem",
    "MediaStream",
    "UserMediaData",
    "PlaybackSession",
    "StorageMount",
    "Pan115Account",
    "EmbyApiToken",
    "IntroMarker",
    "IntroSegment",
    "ChaseFileSnapshot",
    "ChaseRun",
    "ChaseSourceState",
]


class DirectoryScanSnapshot(Base):
    """按源维度的目录扫描快照（任务清单第 11 项：扫描目录指纹）。

    FMBY ``directory_scan_snapshots`` 形态：``library_source_id``、
    ``directory_path``、``listing_fingerprint``、``entry_count``、
    ``last_scan_at``、``last_refresh_at``、``next_due_at``。

    与旧 ``ScanDirState``（``emby_scan_dirs``，``library_id + dir_key``）
    的关系：同一个判定（first / changed / due / not_due），同一套周期
    （``SCAN_DIR_RECHECK_SEC``）与总开关（``SCAN_DIR_DUE_SCHEDULING``），
    不另起第二套增量语义；快照是按源维度的权威记录，同一库多源时各源
    目录互不干扰；旧表继续双写、作为解析不到源行的目录的回落。

    两点与 FMBY 原形的偏差（有意为之，见 ``scan_snapshots`` 模块）：
    1. 多带一列 ``library_id``：我方管理端保存库时删掉源行重插（源行 id
       会变），只凭源 id 查快照会在每次保存后 orphan；按（库，路径）
       查到旧行时认领（把源 id 更新为当前行），快照不因保存而丢。
    2. 两道唯一约束并存：FMBY 的（源，路径）+ 稳定性的（库，路径）——
       同一库同一路径在任一时刻只归一个源（最长前缀匹配），两约束不冲突。
    """

    __tablename__ = "directory_scan_snapshots"

    __table_args__ = (
        UniqueConstraint("library_source_id", "directory_path",
                         name="uq_dir_scan_snapshot_source"),
        UniqueConstraint("library_id", "directory_path",
                         name="uq_dir_scan_snapshot_library"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    library_source_id = Column(Integer, nullable=False, index=True)
    library_id = Column(Integer, nullable=False, index=True)
    # 与扫描器 dir_key 同值：本机绝对路径，或 mount://<挂载 id>/<相对路径>
    directory_path = Column(String(1000), nullable=False)
    listing_fingerprint = Column(String(128), nullable=False)
    entry_count = Column(Integer, nullable=False, default=0)
    # 最近一次见到该目录清单（哪怕判定是未到期跳过，也算见过清单）
    last_scan_at = Column(DateTime, default=None)
    # 最近一次对该目录做完整核对（first / changed / due 判定）
    last_refresh_at = Column(DateTime, default=None)
    # 下一次到期重验时刻（NULL = 还没排期，按已到期处理）
    next_due_at = Column(DateTime, default=None)
    updated_at = Column(DateTime, default=datetime.now)
