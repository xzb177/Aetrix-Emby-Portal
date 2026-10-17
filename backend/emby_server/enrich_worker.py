"""分层扫描 L2/L3：后台补全 worker（v2.40.0）

SCAN_LAYERED=1 时，扫描（L1）只做文件发现 + 指纹 + 极简入库（秒级可见），
side 图片 / NFO / TMDB 刮削全部交给本 worker 在后台补全——这就是「分层」：

- 队列持久化在 ``emby_items.enrich_status``（pending=待补全 done=已补全
  failed=重试超限 enriching=处理中），进程/容器重启不丢；启动时把崩溃残留的
  ``'enriching'`` 打回 ``'pending'``（断点续补），运行期由 janitor 按租约回收
  （v2.42.9：抢单写 ``enrich_claimed_at``，停滞超过租约的僵尸行周期打回
  pending，不再只能靠重启恢复）；
- 原子抢任务：SELECT FOR UPDATE SKIP LOCKED，多 worker/多进程不重复处理；
- 成组抢单（v2.42.9 第 5 批）：按父级（同一部剧 = 一个组）整组抢走、父级在前，
  组内共享一份 ``_ScanContext``（目录列举 / NFO 缓存）——同一季只列一次；
- 纯继承（v2.42.9 第 5 批）：父级已 done 且有图/tmdb_id、本集无自带 NFO 时，
  不读 NFO、不查 TMDB，图片沿父级回退，零网络落 done；
- 失败指数退避：attempts 计数，next_retry_at 调度，5 次后转 failed；
- 挂载不可用（v2.42.12）：存储真的读不动时（``MountError``），该挂载的条目打回
  pending + 长 next_retry_at（**不是** failed，attempts 不涨），挂载恢复后自然重新入队；
  否则一个挂不上的网盘会把整个队列烧成 failed，重扫也救不回来。
- 新文件优先（date_added 倒序），TMDB 令牌桶限速；
- NFO 优先原则不变：NFO 管文字，TMDB 只补图和缺失字段；
- 补全是幂等的：重复补同一条目只会覆盖出相同结果；
- IO（网络/磁盘）全部在 DB 写事务之外做，写库是单条短事务。
"""
from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.database import SessionLocal
from backend.emby_server import models as em
from backend.emby_server import scan_progress as progress
from backend.emby_server.cpu_budget import background_workers as _cpu_workers

logger = logging.getLogger(__name__)

# ---- 可调参数（环境变量） ----
# 注：TMDB 限速（ENRICH_TMDB_PER_SEC）在 v2.42.9 挪进了 tmdb.TmdbClient 的**请求级**
# 令牌桶。旧实现是在这里按**条目**扣 token，而一个条目背后是 0~6 次 HTTP
# （中文标题 4~5 个候选搜索最贵），于是「2/秒」实际打出去 4~12 请求/秒，
# 而且扫描那条路（scanner._tmdb_work）完全没有限速。现在只有一个桶。
# 并发按 CPU 自适应（cpu_budget）：CPU 密集型默认按核数一半，永远给前台留一核。
# enrich 是 IO 密集型（单条 90% 时间等网络，采样实证 2026-10-13）：走 io_bound
# 模式，不按 CPU 截断——未配置默认 8，ENRICH_WORKERS=8 即 8 线程。
# 后台不抢主链路由 max_cap(16) + TMDB 令牌桶/图片信号量/DB 池独立限流保证。
ENRICH_WORKERS = _cpu_workers("ENRICH_WORKERS", io_bound=True)
ENRICH_BATCH = max(10, int(os.getenv("ENRICH_BATCH", "100") or 100))
# TMDB 批量开关：1=抢单后批量调 batch_search/batch_details，0=逐条原逻辑
ENRICH_TMDB_BATCH = os.getenv("ENRICH_TMDB_BATCH", "1") == "1"
ENRICH_IDLE_POLL_SEC = max(5, int(os.getenv("ENRICH_IDLE_POLL_SEC", "30") or 30))
ENRICH_ENABLED = (os.getenv("ENRICH_WORKER", "1") or "1").strip().lower() not in {
    "0", "false", "no", "off",
}
# 重试：最多 5 次，退避基数 60 秒（60s, 120s, 240s, 480s, 960s）
ENRICH_MAX_ATTEMPTS = max(1, int(os.getenv("ENRICH_MAX_ATTEMPTS", "5") or 5))
ENRICH_RETRY_BASE_SEC = max(10, int(os.getenv("ENRICH_RETRY_BASE_SEC", "60") or 60))
# claim 租约（v2.42.9）：抢单时写 enrich_claimed_at，enriching 停滞超过租约即视为
# worker 死亡，janitor 打回 pending。单条补全 = 若干次远程 IO + TMDB，正常远不到
# 15 分钟；真跑超了被回收也只是幂等地多做一次（重试本来就是这么设计的）。
ENRICH_CLAIM_LEASE_SEC = max(60, int(os.getenv("ENRICH_CLAIM_LEASE_SEC", "900") or 900))
# janitor 扫描周期：远小于租约即可，回收延迟上限 = 租约 + 周期
ENRICH_JANITOR_INTERVAL_SEC = max(10, int(os.getenv("ENRICH_JANITOR_INTERVAL_SEC", "60") or 60))

# 调度优先级（v2.42.9 处方 4，probe_priority 先例）：
#   100 = repair（用户主动修复，数量少、可见）——置顶
#    50 = 重试未匹配项（管理员显式触发的补搜）
#     0 = 默认（沿用旧口径：新入库优先）
ENRICH_PRIORITY_REPAIR = 100
ENRICH_PRIORITY_RETRY_UNMATCHED = 50
# Step 5（链路规划）：字段级完成口径的低优先级补字段优先级。
# 低于默认 0：_claim_batch 按 enrich_priority desc 认领，天然排在主队列之后；
# 不另起认领通道、不饿死主队列。补简介只多打 TMDB 详情（约 1 请求/条），
# 每条最多一轮（见 _enrich_apply 的 Step 5 注释），反风控增量可控。
ENRICH_PRIORITY_FIELD_REFILL = -10
# 豆瓣封禁补刮（2026-10-15 用户口径：豆瓣被封就跳过、解封后补刮）：封禁期因
# 跳过豆瓣而没刮全的条目进这个最低优先级安静等候——_claim_batch 在封禁未
# 解除时根本不认领它们（零空转、不烧配额），解封后自动被轮到补跑一轮。
# 低于字段补全的 -10：补刮是「欠账」，排在所有正常工作之后。
ENRICH_PRIORITY_DOUBAN_BACKFILL = -20
# Step 5 缺失字段分组：core=核心字段（标题/年份/海报，旧"三者有其二"口径），
# minor=次要字段（简介/类型/演员）。核心齐但缺次要字段 → 低优先级补一轮。
ENRICH_COMPLETENESS_CORE_FIELDS = ("title", "year", "poster")
ENRICH_COMPLETENESS_MINOR_FIELDS = ("overview", "genres", "cast")
# Step 4（链路规划）：none 定时回捞单轮硬上限——写死 2000，不许可配放大。
# 反风控：单轮最多 2000 条重走 TMDB search（6-9 请求/条），分散在 janitor
# 周期里；上限放大只会线性增加 TMDB 请求量，故不开放配置。
ENRICH_NONE_AUTO_RESCHEDULE_BATCH = 2000
# 按库轮转（处方 4）：每个 worker 进程记住「上一轮从哪个库接着抢」，老分类不再被
# 新入库条目饿死。库里没有待处理条目时自动跳到下一个库，不多等一轮。
ENRICH_LIBRARY_FAIRNESS = (os.getenv("ENRICH_LIBRARY_FAIRNESS", "1") or "1").strip().lower() not in {
    "0", "false", "no", "off",
}
# 零 API 别名索引（处方 12）：进程内 TTL 缓存的重建周期与行数上限
_ALIAS_INDEX_TTL = max(30, int(os.getenv("ENRICH_ALIAS_INDEX_TTL", "300") or 300))
_ALIAS_INDEX_MAX = max(1000, int(os.getenv("ENRICH_ALIAS_INDEX_MAX", "50000") or 50000))
_alias_index: dict = {"rows": None, "at": 0.0}

# ---- 单集夜间刮削（默认关闭：不改变现有行为） ----
# ENRICH_EPISODE_NIGHT_ONLY=1 时，白天只刮顶层条目（movie/series/season），
# 单集（episode）只在夜间窗口 [ENRICH_NIGHT_START, ENRICH_NIGHT_END) 内刮削。
# 目的：白天优先把电影/剧集本身的元数据补出来，单集留到低峰期再补。
# 口径：环境变量优先；未设置时读 SystemConfig（管理后台可配，60 秒缓存热生效，
# 无需重启）。时间按可配置时区（enrich_night_tz，默认 Asia/Shanghai）解释，
# 不再是服务器本地时间；窗口支持跨午夜（如 22:00-06:00）。
ENRICH_EPISODE_NIGHT_ONLY_ENV = "ENRICH_EPISODE_NIGHT_ONLY"
ENRICH_NIGHT_START_ENV = "ENRICH_NIGHT_START"
ENRICH_NIGHT_END_ENV = "ENRICH_NIGHT_END"
ENRICH_NIGHT_TZ_ENV = "ENRICH_NIGHT_TZ"
CFG_EPISODE_NIGHT_ONLY = "enrich_episode_night_only"
CFG_NIGHT_START = "enrich_night_start"
CFG_NIGHT_END = "enrich_night_end"
CFG_NIGHT_TZ = "enrich_night_tz"
NIGHT_ONLY_DEFAULT = False
NIGHT_START_DEFAULT = "02:00"
NIGHT_END_DEFAULT = "06:00"
NIGHT_TZ_DEFAULT = "Asia/Shanghai"
# 管理后台时区下拉的常用选项（allow-create 允许手填任意 IANA 时区名）
NIGHT_TZ_OPTIONS = [
    "Asia/Shanghai",
    "Asia/Tokyo",
    "Asia/Singapore",
    "Asia/Hong_Kong",
    "Europe/London",
    "Europe/Paris",
    "America/New_York",
    "America/Los_Angeles",
    "Australia/Sydney",
    "UTC",
]
_NIGHT_CFG_TTL_SEC = 60.0
_night_cfg_cache: dict = {"at": 0.0, "cfg": None}

# ---- 单集分项开关（批3：刮削瘦身收官）----
# 单集（episode）下沉时，「标题 / 简介 / 剧照」三项各自可开可关。
# 口径：环境变量优先；未设置时读 SystemConfig（管理后台可配，60 秒缓存热生效，
# 无需重启）。默认值全部开 = 与现状行为完全一致（升级零行为变化）。
# 关掉某一项后，工人下沉到单集时跳过该项的写入（TMDB 与 NFO 都不写该项），
# 但夜间窗口 / 认领门控 / repair 语义一字不动（分项开关只管「写什么」，不管「何时抢单」）。
ENRICH_EPISODE_TITLE_ENV = "ENRICH_EPISODE_TITLE"
ENRICH_EPISODE_OVERVIEW_ENV = "ENRICH_EPISODE_OVERVIEW"
ENRICH_EPISODE_STILL_ENV = "ENRICH_EPISODE_STILL"
CFG_EPISODE_TITLE = "enrich_episode_title"
CFG_EPISODE_OVERVIEW = "enrich_episode_overview"
CFG_EPISODE_STILL = "enrich_episode_still"
EPISODE_FIELD_DEFAULTS = {"title": True, "overview": True, "still": True}
_EP_FIELDS_CFG_TTL_SEC = 60.0
_ep_fields_cfg_cache: dict = {"at": 0.0, "cfg": None}

# 单集等父级 ping-pong 上限（P2 修复，完美审查 C-P2-4）：父级长期 pending
#（如长退避链）时，单集每 300s 被抢单打回一次、"不计失败次数"→无限循环，
# 每次消耗抢单与写库。进程内计连续等待次数：指数退避 300s→600s…（上限 2h），
# 超 20 次按父级终态失败处理（走 none，管理端"重试未匹配项"可捞回）。
_PARENT_WAIT_MAX_TIMES = 20
_PARENT_WAIT_BASE_SEC = 300
_PARENT_WAIT_MAX_SEC = 7200
# 注（R18-P3）：_parent_wait_counts 的读-改-写在 32 个 worker 线程下存在
# 轻微竞态（两线程同读 5、各写回 6，少计一次；clear 也可能被同时触发）。
# 经评估接受现状不加锁，理由：1）计数差一两次只影响退避时长（300s 粒度）
# 和 20 次上限的触发时机，不影响正确性；2）GIL 保证单次 dict 操作原子，
# 不会丢 key；3）加锁会把高频热路径串行化，收益为负。若未来观察到上限
# 触发明显偏晚，再考虑 threading.Lock 包住读-改-写。
_parent_wait_counts: dict[int, int] = {}

# douban / altmeta 开关配置的 60s TTL 进程内缓存（P2 修复，完美审查 C-P2-5）：
# 之前逐条目开短会话读配置（32 worker × 批量 100 条 = 大量短会话），
# 与 _episode_night_config 同一口径。管理后台保存后最多 60 秒生效。
_DOUBAN_CFG_TTL_SEC = 60.0
_douban_cfg_cache: dict = {"at": 0.0, "cfg": None}
_ALTMETA_CFG_TTL_SEC = 60.0
_altmeta_cfg_cache: dict = {"at": 0.0, "cfg": None}


def _cached_douban_cfg() -> tuple:
    """豆瓣开关 (enabled, min_interval)，60s TTL 进程内缓存。"""
    from backend.emby_server import douban as _dbn
    now_ts = time.monotonic()
    cached = _douban_cfg_cache["cfg"]
    if cached is not None and now_ts - _douban_cfg_cache["at"] < _DOUBAN_CFG_TTL_SEC:
        return cached
    _db = SessionLocal()
    try:
        on = _dbn.enabled(_db)
        iv = _dbn.min_interval(_db) if on else 0.0
    finally:
        _db.close()
    _douban_cfg_cache["cfg"] = (on, iv)
    _douban_cfg_cache["at"] = now_ts
    return on, iv


def _douban_switch_on() -> bool:
    """豆瓣可选开关（走 60s TTL 进程内缓存的 ``_cached_douban_cfg``）。

    批 1（刮削瘦身）：轻量豆瓣入口（altmeta.search）在开关关闭时短路，
    避免「只开 altmeta 不开豆瓣」时打出绕过开关的豆瓣请求。
    读不到配置时按关处理（fail-closed）。
    """
    try:
        on, _iv = _cached_douban_cfg()
        return bool(on)
    except Exception:  # noqa: BLE001
        return False


def _cached_altmeta_cfg() -> tuple:
    """altmeta 开关 (enabled, min_interval, min_interval_bangumi)，60s TTL 进程内缓存。"""
    from backend.emby_server import altmeta
    now_ts = time.monotonic()
    cached = _altmeta_cfg_cache["cfg"]
    if cached is not None and now_ts - _altmeta_cfg_cache["at"] < _ALTMETA_CFG_TTL_SEC:
        return cached
    _db = SessionLocal()
    try:
        ok = altmeta.enabled(_db)
        if ok:
            iv = altmeta.min_interval(_db)
            bgm_iv = altmeta.min_interval_bangumi(_db)
        else:
            iv, bgm_iv = 0.0, 0.0
    finally:
        _db.close()
    _altmeta_cfg_cache["cfg"] = (ok, iv, bgm_iv)
    _altmeta_cfg_cache["at"] = now_ts
    return ok, iv, bgm_iv


# ---------------------------------------------------------------------------
# 豆瓣封禁降级（链路规划 Step 0.5）
#
# feature flag：enrich_douban_ban_downgrade（SystemConfig，默认 "1"=开；
# 2026-10-15 起默认开——用户明令「豆瓣被封就跳过」，默认行为必须符合该
# 口径；管理员显式设 "0" 是紧急回退到旧行为的逃生口）。
# flag 开 + 豆瓣封禁中（ban_remaining()>0）时：
#   - 预搜索处不再标 douban_banned 延后，TMDB 照常跑；
#   - 豆瓣 rich 兜底直接跳过（一次豆瓣请求都不发），走多源兜底分支；
#   - 多源/TMDB 命中从严（标题+年份都对上才落库，宁可 none 不乱认）。
# flag 关时行为与现状逐字节一致。解封后自动恢复原顺序（运行时逐条判定，
# 无状态翻转，无需任何回切动作）。
# ---------------------------------------------------------------------------
CFG_DOUBAN_BAN_DOWNGRADE = "enrich_douban_ban_downgrade"


def _douban_ban_downgrade_flag_on() -> bool:
    """Step 0.5 开关：读 SystemConfig。

    读口径走 store.get_value（全项目唯一一套带 60s TTL 热缓存的读法），
    管理后台保存后最多 60 秒生效。key 缺失按开（默认口径，见上方注释）；
    读异常时按关处理（无法确认配置面貌时的保守回退）。
    """
    from backend.integrations import store
    _db = SessionLocal()
    try:
        return store.get_value(_db, CFG_DOUBAN_BAN_DOWNGRADE, "1") == "1"
    except Exception:  # noqa: BLE001 — 读不到就当关着，行为与现状一致
        return False
    finally:
        _db.close()


def _douban_ban_downgrade_active() -> bool:
    """Step 0.5 降级是否激活：flag 开 + 豆瓣封禁中（逐条运行时判定）。

    Step 2（兜底链统一）会把这段判断收编进统一兜底链——独立小函数，
    方便到时整体搬迁，不留两套。
    """
    if not _douban_ban_downgrade_flag_on():
        return False
    try:
        from backend.emby_server import douban as _dbn
        return _dbn.ban_remaining() > 0
    except Exception:  # noqa: BLE001 — 判定不了就不降级，保守
        return False


def _ban_downgrade_strict_hit_ok(item_name: str, item_year,
                                 cand_title: str, cand_year) -> bool:
    """Step 0.5 从严匹配：标题+年份都对上才算命中（宁可 none 不乱认）。

    标题口径照抄 ``altmeta.search``（:189-191）：清洗（去空白与常见标点、
    转小写）后必须完全一致，或一方是另一方的前缀。
    年份：条目与候选都有值时必须相等；任一方缺失不否决。
    标题任一方为空 → 直接否决（没法验证 = 不可信）。
    """
    _punct = r"[\s:：·\-—_\.、,，!！?？~～'\"“”‘’()（）\[\]【】]+"
    got = re.sub(_punct, "", (cand_title or "").lower())
    want = re.sub(_punct, "", (item_name or "").lower())
    if not got or not want:
        return False
    if got != want and not (got.startswith(want) or want.startswith(got)):
        return False
    try:
        iy = int(item_year) if item_year else None
    except (TypeError, ValueError):
        iy = None
    try:
        cy = int(str(cand_year)[:4]) if cand_year else None
    except (TypeError, ValueError):
        cy = None
    if iy and cy and iy != cy:
        return False
    return True


def get_douban_ban_downgrade_config(db) -> dict:
    """管理后台用：豆瓣封禁降级开关 + 当前封禁状态（只读）。"""
    from backend.integrations import store
    from backend.emby_server import douban as _dbn
    remaining = max(0.0, _dbn.ban_remaining())
    return {
        "enabled": store.get_value(db, CFG_DOUBAN_BAN_DOWNGRADE, "1") == "1",
        "banned": remaining > 0,
        "ban_remaining_sec": round(remaining, 1),
    }


def save_douban_ban_downgrade_config(db, enabled: bool) -> dict:
    """管理后台用：保存豆瓣封禁降级开关（写 SystemConfig，60 秒内热生效）。"""
    from backend.integrations import store
    store.write_values(
        db,
        {CFG_DOUBAN_BAN_DOWNGRADE: "1" if enabled else "0"},
        {CFG_DOUBAN_BAN_DOWNGRADE:
         "豆瓣封禁降级开关（链路规划 Step 0.5）：封禁期跳过豆瓣走多源兜底"},
    )
    db.commit()
    return get_douban_ban_downgrade_config(db)


# ---------------------------------------------------------------------------
# 兜底链路统一（链路规划 Step 2）
#
# feature flag：enrich_unified_fallback（SystemConfig，默认 "0"=关）。
# flag 开时：TMDB 未命中后的兜底只剩两个调用点——豆瓣 rich（CJK 专属）→
# 多源引擎（含豆瓣源）。轻量 altmeta.search 已并入引擎做"豆瓣源"，不再直调；
# 原 else 分支（Phase 6b）的 _collect_multisource 调用与轻量直调被统一调用点
# 取代（flag 包裹，暂不物理删除，下一轮规划确认无回归后再删）；
# _should_multisource_on_tmdb_miss 里的独立 tmdb_miss_fallback 判断降级为
# 多源总开关的子项（总开关是唯一的门）。
# flag 关时行为与 Step 1 部署后逐字节一致（回滚=关 flag）。
# ---------------------------------------------------------------------------
CFG_UNIFIED_FALLBACK = "enrich_unified_fallback"


def _unified_fallback_flag_on() -> bool:
    """Step 2 开关：读 SystemConfig。

    读口径走 store.get_value（全项目唯一一套带 60s TTL 热缓存的读法），
    管理后台保存后最多 60 秒生效。读不到/异常时按关处理（行为=现状）。
    """
    from backend.integrations import store
    _db = SessionLocal()
    try:
        return store.get_value(_db, CFG_UNIFIED_FALLBACK, "0") == "1"
    except Exception:  # noqa: BLE001 — 读不到就当关着，行为与现状一致
        return False
    finally:
        _db.close()


def get_unified_fallback_config(db) -> dict:
    """管理后台用：兜底链路统一开关（只读）。"""
    from backend.integrations import store
    return {
        "enabled": store.get_value(db, CFG_UNIFIED_FALLBACK, "0") == "1",
    }


def save_unified_fallback_config(db, enabled: bool) -> dict:
    """管理后台用：保存兜底链路统一开关（写 SystemConfig，60 秒内热生效）。"""
    from backend.integrations import store
    store.write_values(
        db,
        {CFG_UNIFIED_FALLBACK: "1" if enabled else "0"},
        {CFG_UNIFIED_FALLBACK:
         "兜底链路统一开关（链路规划 Step 2）：一个豆瓣入口，rich→多源引擎"},
    )
    db.commit()
    return get_unified_fallback_config(db)


def _parse_hhmm(s: Optional[str], default: str) -> str:
    """解析 HH:MM，非法则回退到默认值（保证下游比较永不抛异常）"""
    s = (s or "").strip()
    try:
        hh, mm = s.split(":")
        h, m = int(hh), int(mm)
        if 0 <= h < 24 and 0 <= m < 60:
            return f"{h:02d}:{m:02d}"
    except (ValueError, AttributeError):
        pass
    return default


def _parse_tz(name: Optional[str], default: str = NIGHT_TZ_DEFAULT) -> ZoneInfo:
    """把 IANA 时区名解析为 ZoneInfo；非法/缺失时回退到 default，再失败回退 UTC。

    永不抛异常，保证抢单路径不受坏配置影响。
    """
    for candidate in (name, default, "UTC"):
        if not candidate or not isinstance(candidate, str):
            continue
        try:
            return ZoneInfo(candidate.strip())
        except (ZoneInfoNotFoundError, ValueError, TypeError, OSError):
            continue
    return ZoneInfo("UTC")  # 理论上不可达


def _in_night_window(now: datetime, start: str, end: str,
                     tz: Optional[str | ZoneInfo] = None) -> bool:
    """now 是否在 [start, end) 夜间窗口内；start > end 视为跨午夜窗口。

    时间按 tz（IANA 时区名或 ZoneInfo，默认 NIGHT_TZ_DEFAULT）解释：
    naive 的 now 先按 UTC 理解，再转换到目标时区后比较 hour/minute。
    """
    zone = _parse_tz(tz) if not isinstance(tz, ZoneInfo) else tz
    aware = now if now.tzinfo is not None else now.replace(tzinfo=ZoneInfo("UTC"))
    local = aware.astimezone(zone)
    t = (local.hour, local.minute)
    s = (int(start[:2]), int(start[3:5]))
    e = (int(end[:2]), int(end[3:5]))
    if s <= e:
        return s <= t < e
    return t >= s or t < e


def _episode_night_config(db=None) -> dict:
    """单集夜间刮削的有效配置（环境变量优先，否则读 SystemConfig）。

    db 可空：传了 db 才读库；60 秒 TTL 缓存，管理后台保存后最多 60 秒生效。
    返回 {"enabled": bool, "night_start": "HH:MM", "night_end": "HH:MM",
           "night_tz": "Asia/Shanghai", "from_env": bool, "in_window": bool}
    （字段名与管理后台前端 EnrichEpisodeNightConfig 接口保持一致）
    """
    now_ts = time.monotonic()
    cached = _night_cfg_cache["cfg"]
    if cached is not None and now_ts - _night_cfg_cache["at"] < _NIGHT_CFG_TTL_SEC:
        cfg = dict(cached)
    else:
        env_enabled = (os.getenv(ENRICH_EPISODE_NIGHT_ONLY_ENV) or "").strip().lower()
        env_start = (os.getenv(ENRICH_NIGHT_START_ENV) or "").strip()
        env_end = (os.getenv(ENRICH_NIGHT_END_ENV) or "").strip()
        env_tz = (os.getenv(ENRICH_NIGHT_TZ_ENV) or "").strip()
        # 任一环境变量被设置（含显式 "0"）即视为 env 接管：后台改了也不生效，
        # from_env=True 让管理后台显示提示，避免用户困惑。
        from_env = bool(env_enabled) or bool(env_start) or bool(env_end) or bool(env_tz)
        if env_enabled not in {"", "0", "false", "no", "off"}:
            enabled = True
        elif env_enabled in {"0", "false", "no", "off"}:
            enabled = False
        else:
            enabled = NIGHT_ONLY_DEFAULT
            if db is not None:
                try:
                    from backend.integrations import store
                    enabled = store.read_value(
                        db, CFG_EPISODE_NIGHT_ONLY, "0") == "1"
                except Exception:  # noqa: BLE001 — 读库失败就按默认关，不挡抢单
                    logger.warning("单集夜间配置读库失败，按默认关闭", exc_info=True)
        start = _parse_hhmm(env_start or None, NIGHT_START_DEFAULT)
        end = _parse_hhmm(env_end or None, NIGHT_END_DEFAULT)
        tz = _parse_tz(env_tz or None, NIGHT_TZ_DEFAULT).key
        if not env_start and not env_end and db is not None and not from_env:
            try:
                from backend.integrations import store
                start = _parse_hhmm(
                    store.read_value(db, CFG_NIGHT_START, NIGHT_START_DEFAULT),
                    NIGHT_START_DEFAULT)
                end = _parse_hhmm(
                    store.read_value(db, CFG_NIGHT_END, NIGHT_END_DEFAULT),
                    NIGHT_END_DEFAULT)
                # 老配置可能没有 tz 字段：读不到时用默认值（向后兼容）
                tz = _parse_tz(
                    store.read_value(db, CFG_NIGHT_TZ, NIGHT_TZ_DEFAULT),
                    NIGHT_TZ_DEFAULT).key
            except Exception:  # noqa: BLE001
                logger.warning("夜间窗口读库失败，用默认值", exc_info=True)
        cfg = {"enabled": enabled, "night_start": start, "night_end": end,
               "night_tz": tz, "from_env": from_env}
        _night_cfg_cache["at"] = now_ts
        _night_cfg_cache["cfg"] = dict(cfg)
    cfg["in_window"] = _in_night_window(
        datetime.now(), cfg["night_start"], cfg["night_end"], cfg["night_tz"])
    return cfg


def get_episode_night_config(db) -> dict:
    """管理后台用：当前生效的单集夜间刮削配置（含是否在窗口内）"""
    return _episode_night_config(db)


def save_episode_night_config(db, enabled: bool, start: str, end: str,
                            tz: Optional[str] = None) -> dict:
    """管理后台用：保存单集夜间刮削配置（写 SystemConfig，60 秒内热生效）"""
    from backend.integrations import store
    start = _parse_hhmm(start, NIGHT_START_DEFAULT)
    end = _parse_hhmm(end, NIGHT_END_DEFAULT)
    tz = _parse_tz(tz, NIGHT_TZ_DEFAULT).key
    store.write_values(
        db,
        {CFG_EPISODE_NIGHT_ONLY: "1" if enabled else "0",
         CFG_NIGHT_START: start,
         CFG_NIGHT_END: end,
         CFG_NIGHT_TZ: tz},
        {CFG_EPISODE_NIGHT_ONLY: "单集仅夜间刮削开关（白天只刮顶层条目）",
         CFG_NIGHT_START: "夜间窗口开始（HH:MM，按 enrich_night_tz 时区解释）",
         CFG_NIGHT_END: "夜间窗口结束（HH:MM，按 enrich_night_tz 时区解释）",
         CFG_NIGHT_TZ: "夜间窗口时区（IANA 时区名，如 Asia/Shanghai）"},
    )
    db.commit()
    db.expire_all()
    # 立即清缓存：保存后下一次抢单就用新值，不用等 60 秒 TTL
    _night_cfg_cache["at"] = 0.0
    _night_cfg_cache["cfg"] = None
    logger.info("单集夜间刮削配置已保存: enabled=%s window=%s-%s tz=%s",
                enabled, start, end, tz)
    return get_episode_night_config(db)


def _episode_field_config(db=None) -> dict:
    """单集分项开关的有效配置（环境变量优先，否则读 SystemConfig）。

    db 可空：传了 db 才读库；60 秒 TTL 缓存，管理后台保存后最多 60 秒生效。
    返回 {"title": bool, "overview": bool, "still": bool, "from_env": bool}
    （字段名与管理后台前端 EnrichEpisodeFieldsConfig 接口保持一致）。
    默认值全部 True = 与现状行为完全一致。
    """
    now_ts = time.monotonic()
    cached = _ep_fields_cfg_cache["cfg"]
    if cached is not None and now_ts - _ep_fields_cfg_cache["at"] < _EP_FIELDS_CFG_TTL_SEC:
        return dict(cached)
    env_title = (os.getenv(ENRICH_EPISODE_TITLE_ENV) or "").strip().lower()
    env_overview = (os.getenv(ENRICH_EPISODE_OVERVIEW_ENV) or "").strip().lower()
    env_still = (os.getenv(ENRICH_EPISODE_STILL_ENV) or "").strip().lower()
    # 任一环境变量被设置（含显式 "0"）即视为 env 接管：后台改了也不生效，
    # from_env=True 让管理后台显示提示，避免用户困惑。
    from_env = bool(env_title) or bool(env_overview) or bool(env_still)

    def _resolve(env_val: str, cfg_key: str, db) -> bool:
        if env_val not in {"", "0", "false", "no", "off"} and env_val:
            return True
        if env_val in {"0", "false", "no", "off"}:
            return False
        default = EPISODE_FIELD_DEFAULTS[cfg_key.rsplit("_", 1)[-1]]
        if db is not None:
            try:
                from backend.integrations import store
                return store.read_value(
                    db, cfg_key, "1" if default else "0") == "1"
            except Exception:  # noqa: BLE001 — 读库失败就按默认（全开），不挡刮削
                logger.warning("单集分项开关读库失败，按默认全开", exc_info=True)
        return default

    cfg = {
        "title": _resolve(env_title, CFG_EPISODE_TITLE, db),
        "overview": _resolve(env_overview, CFG_EPISODE_OVERVIEW, db),
        "still": _resolve(env_still, CFG_EPISODE_STILL, db),
        "from_env": from_env,
    }
    _ep_fields_cfg_cache["at"] = now_ts
    _ep_fields_cfg_cache["cfg"] = dict(cfg)
    return cfg


def get_episode_field_config(db) -> dict:
    """管理后台用：当前生效的单集分项开关"""
    return _episode_field_config(db)


def save_episode_field_config(db, title: bool, overview: bool, still: bool) -> dict:
    """管理后台用：保存单集分项开关（写 SystemConfig，60 秒内热生效）"""
    from backend.integrations import store
    store.write_values(
        db,
        {CFG_EPISODE_TITLE: "1" if title else "0",
         CFG_EPISODE_OVERVIEW: "1" if overview else "0",
         CFG_EPISODE_STILL: "1" if still else "0"},
        {CFG_EPISODE_TITLE: "单集刮削：是否写入单集标题（默认开）",
         CFG_EPISODE_OVERVIEW: "单集刮削：是否写入单集简介（默认开）",
         CFG_EPISODE_STILL: "单集刮削：是否写入单集剧照（默认开）"},
    )
    db.commit()
    db.expire_all()
    # 立即清缓存：保存后下一次刮削就用新值，不用等 60 秒 TTL
    _ep_fields_cfg_cache["at"] = 0.0
    _ep_fields_cfg_cache["cfg"] = None
    logger.info("单集分项开关已保存: title=%s overview=%s still=%s",
                title, overview, still)
    return get_episode_field_config(db)

# ---------------------------------------------------------------------------
# 刮削性能参数（链路规划 Step 1：配置面收口）
#
# 原来 env-only 的关键旋钮搬进 SystemConfig + 后台 UI；env 保留为部署覆盖。
# 口径照抄 _episode_night_config：
#   - 任一环境变量被设置（含显式 "0"）即 env 接管，后台改了也不生效，
#     from_env=True 让管理后台显示提示；
#   - 未设置 env 时读 SystemConfig（backend.integrations.store），
#     60 秒 TTL 缓存热生效；管理后台保存后立即清缓存，下一次抢单即用新值。
# 纯搬迁：未设置时行为与今天逐字节一致（各默认值见 _ENRICH_TUNING_SPECS）。
# ---------------------------------------------------------------------------
ENRICH_WORKERS_ENV = "ENRICH_WORKERS"
ENRICH_BATCH_ENV = "ENRICH_BATCH"
ENRICH_TMDB_BATCH_ENV = "ENRICH_TMDB_BATCH"
ENRICH_MAX_ATTEMPTS_ENV = "ENRICH_MAX_ATTEMPTS"
ENRICH_RETRY_BASE_SEC_ENV = "ENRICH_RETRY_BASE_SEC"
ENRICH_CLAIM_LEASE_SEC_ENV = "ENRICH_CLAIM_LEASE_SEC"

CFG_ENRICH_WORKERS = "enrich_workers"
CFG_ENRICH_BATCH = "enrich_batch"
CFG_ENRICH_TMDB_BATCH = "enrich_tmdb_batch"
CFG_ENRICH_MAX_ATTEMPTS = "enrich_max_attempts"
CFG_ENRICH_RETRY_BASE_SEC = "enrich_retry_base_sec"
CFG_ENRICH_CLAIM_LEASE_SEC = "enrich_claim_lease_sec"
# Step 3 的配额制开关（flag，默认 0=关）；Step 4 的占坑位仍只可配不接入
CFG_ENRICH_EPISODE_DAY_QUOTA = "enrich_episode_day_quota"
CFG_ENRICH_EPISODE_QUOTA_MODE = "enrich_episode_quota_mode"
CFG_ENRICH_NONE_RESCHEDULE_DAYS = "enrich_none_reschedule_days"
# Step 4 的 feature flag：none 定时自动回捞开关（默认 0=关，关时行为与 Step 4 前一致）
CFG_ENRICH_NONE_AUTO_RESCHEDULE = "enrich_none_auto_reschedule"
# Step 5 的 feature flag：字段级完成口径开关（默认 0=关，关时口径=旧"三者有其二"）
CFG_ENRICH_FIELD_COMPLETENESS = "enrich_field_completeness"

# (config_key, env_name, 默认值str, 类型, 下限钳制, 中文说明, 是否重启生效)
# 类型：workers=io_bound 线程口径（正整数→max(1,min(v,16))，缺省 8）；
#       int=整数下限钳制；flag1=是否为 "1"（口径照抄现状）。
_ENRICH_TUNING_SPECS = [
    (CFG_ENRICH_WORKERS, ENRICH_WORKERS_ENV, "8", "workers", None,
     "补全 worker 线程数（IO 密集型，未配置默认 8，上限 16；改完需重启 worker）", True),
    (CFG_ENRICH_BATCH, ENRICH_BATCH_ENV, "100", "int", 10,
     "每批认领条数（下限 10）", False),
    (CFG_ENRICH_TMDB_BATCH, ENRICH_TMDB_BATCH_ENV, "1", "flag1", None,
     "TMDB 批量开关：1=批量调 batch_search/batch_details，0=逐条原逻辑", False),
    (CFG_ENRICH_MAX_ATTEMPTS, ENRICH_MAX_ATTEMPTS_ENV, "5", "int", 1,
     "异常失败重试上限次数（资料不全的退避基数见下一项）", False),
    (CFG_ENRICH_RETRY_BASE_SEC, ENRICH_RETRY_BASE_SEC_ENV, "60", "int", 10,
     "重试退避基数（秒）：按 60s×2ⁿ 指数退避", False),
    (CFG_ENRICH_CLAIM_LEASE_SEC, ENRICH_CLAIM_LEASE_SEC_ENV, "900", "int", 60,
     "认领租约（秒）：worker 停滞超过租约即视为死亡、由 janitor 回收", False),
    (CFG_ENRICH_EPISODE_DAY_QUOTA, None, "2000", "int", 0,
     "白天单集日配额（配额制开时生效；夜间窗口内不限；设 0 = 回到夜间禁令制）", False),
    (CFG_ENRICH_EPISODE_QUOTA_MODE, None, "0", "flag1", None,
     "单集认领配额制开关（链路规划 Step 3）：1=白天单集按日配额认领（夜间不限），"
     "0=夜间窗口禁令制（默认，行为与 Step 3 前一致）", False),
    (CFG_ENRICH_NONE_RESCHEDULE_DAYS, None, "30", "int", 1,
     "【Step 4】none 条目自动回捞周期（天）：enrich_none_auto_reschedule=1 时生效，"
     "开关关闭时改这里不影响实际行为", False),
    (CFG_ENRICH_NONE_AUTO_RESCHEDULE, None, "0", "flag1", None,
     "【Step 4】none 条目定时自动回捞开关：1=开（超周期的 done+none 剧/电影由 "
     "janitor 按最旧优先、单轮 2000 条翻回 pending；资料不全 5 次后转 done+none "
     "而非 failed），0=关（默认，行为与 Step 4 前完全一致）", False),
    (CFG_ENRICH_FIELD_COMPLETENESS, None, "0", "flag1", None,
     "【Step 5】完成口径字段级开关：1=开（核心字段齐但缺简介/类型/演员的条目"
     "进低优先级队列补一轮，priority=-10，天然排后；每条最多多跑一轮，补完"
     "无论拿到与否直接终态化，不反复重刮），0=关（默认，口径=旧的"
     "「标题/年份/海报三者有其二」）", False),
]
# 快速查表：key → spec
_ENRICH_TUNING_BY_KEY = {spec[0]: spec for spec in _ENRICH_TUNING_SPECS}

_ENRICH_TUNING_TTL_SEC = 60.0
_enrich_tuning_cache: dict = {"at": 0.0, "cfg": None}


def _parse_tuning_value(kind: str, minimum, value_str: str, default: str):
    """按类型解析单个性能参数，保证永不抛异常、非法值回退默认。"""
    if kind == "workers":
        # 口径与 _cpu_workers(io_bound=True) 一致：未配置默认 8，
        # 手动配置按 max(1, min(v, 16)) 钳制，非法值按未配置处理。
        try:
            v = int((value_str or "").strip())
        except (TypeError, ValueError):
            v = 0
        return max(1, min(v, 16)) if v > 0 else 8
    if kind == "flag1":
        # 原口径：os.getenv("ENRICH_TMDB_BATCH", "1") == "1"（字符串精确比较）。
        # 补：管理后台开关发 JSON 布尔，save 路径里 str(val) 后变成 "True"/
        # "False"，必须显式归一化——否则"开"会存成"0"（Step 1 留下的 latent bug，
        # Step 3 接入配额制开关前修好）。
        if isinstance(value_str, bool):
            return value_str
        _low = "" if value_str is None else str(value_str).strip().lower()
        if _low in ("true", "1"):
            return True
        if _low in ("false", "0"):
            return False
        return (value_str if value_str is not None else "1") == "1"
    try:
        v = int(str(value_str).strip())
    except (TypeError, ValueError):
        v = int(default)
    return max(minimum, v)


def _enrich_tuning_config(db=None) -> dict:
    """刮削性能参数的有效配置（环境变量优先，否则读 SystemConfig）。

    db 可空：传了 db 才读库；60 秒 TTL 缓存，管理后台保存后最多 60 秒生效。
    返回 {"values": {key: 解析后值}, "raw": {key: 字符串值},
           "from_env": {key: bool}, "restart_required": [CFG_ENRICH_WORKERS]}
    """
    now_ts = time.monotonic()
    cached = _enrich_tuning_cache["cfg"]
    if cached is not None and now_ts - _enrich_tuning_cache["at"] < _ENRICH_TUNING_TTL_SEC:
        return {k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v)
                for k, v in cached.items()}
    values, raw, from_env = {}, {}, {}
    for key, env_name, default, kind, minimum, _desc, _restart in _ENRICH_TUNING_SPECS:
        env_raw = (os.getenv(env_name) or "").strip() if env_name else ""
        taken = bool(env_raw)
        from_env[key] = taken
        if taken:
            # flag1 保持原口径不对 env 做 strip（现状就是 == "1" 精确比较）
            s = os.getenv(env_name) if kind == "flag1" else env_raw
        elif db is not None:
            try:
                from backend.integrations import store
                s = store.read_value(db, key, default)
            except Exception:  # noqa: BLE001 — 读库失败就按默认，不挡抢单
                logger.warning("刮削性能参数 %s 读库失败，用默认值", key, exc_info=True)
                s = default
        else:
            s = default
        raw[key] = s
        values[key] = _parse_tuning_value(kind, minimum, s, default)
    cfg = {"values": values, "raw": raw, "from_env": from_env,
           "restart_required": [CFG_ENRICH_WORKERS]}
    _enrich_tuning_cache["at"] = now_ts
    _enrich_tuning_cache["cfg"] = {k: (dict(v) if isinstance(v, dict) else list(v))
                                   for k, v in cfg.items()}
    return cfg


def enrich_tuning_value(key: str, db=None):
    """取单个刮削性能参数的解析后值（worker 热路径用；db 可空）。"""
    return _enrich_tuning_config(db)["values"][key]


def _resolve_enrich_workers(db=None) -> int:
    """线程数：进程启动（start()）时解析，env → SystemConfig → 默认 8。

    运行时动态增减线程不做（不安全），改这里要重启 worker 进程。
    """
    return int(_enrich_tuning_config(db)["values"][CFG_ENRICH_WORKERS])


def get_enrich_tuning_config(db) -> dict:
    """管理后台用：当前生效的刮削性能参数（含 from_env / 默认值 / 中文说明）。"""
    cfg = _enrich_tuning_config(db)
    return {
        "values": cfg["values"],
        "raw": cfg["raw"],
        "from_env": cfg["from_env"],
        "restart_required": cfg["restart_required"],
        "order": [spec[0] for spec in _ENRICH_TUNING_SPECS],
        "defaults": {spec[0]: spec[2] for spec in _ENRICH_TUNING_SPECS},
        "descriptions": {spec[0]: spec[5] for spec in _ENRICH_TUNING_SPECS},
        "env_names": {spec[0]: spec[1] for spec in _ENRICH_TUNING_SPECS},
    }


def save_enrich_tuning_config(db, values: dict) -> dict:
    """管理后台用：保存刮削性能参数（写 SystemConfig；除线程数外 60 秒内热生效）。

    values 未知的 key 会被忽略；非法值回退该项默认值，保证库里永远是干净值。
    """
    from backend.integrations import store
    payload, descriptions = {}, {}
    for key, val in (values or {}).items():
        spec = _ENRICH_TUNING_BY_KEY.get(key)
        if spec is None:
            logger.warning("刮削性能参数忽略未知 key: %s", key)
            continue
        _k, _env, default, kind, minimum, desc, _restart = spec
        s = str(val).strip() if val is not None else ""
        parsed = _parse_tuning_value(kind, minimum, s if s else default, default)
        payload[key] = ("1" if parsed else "0") if kind == "flag1" else str(int(parsed))
        descriptions[key] = desc
    if payload:
        store.write_values(db, payload, descriptions)
        db.commit()
        db.expire_all()
    # 立即清缓存：保存后下一次抢单就用新值，不用等 60 秒 TTL
    _enrich_tuning_cache["at"] = 0.0
    _enrich_tuning_cache["cfg"] = None
    logger.info("刮削性能参数已保存: %s", payload)
    return get_enrich_tuning_config(db)


_worker_threads: list = []
_effective_workers: int | None = None  # start() 时解析的实际线程数（供进度上报）
_stop_event = threading.Event()

# 发现页实时动态流：刮削批次埋点的进程内节流（补全 worker 是常驻循环，
# 不节流会把 feed 刷满）。同一进程内两次埋点至少间隔 10 分钟。
_ENRICH_FEED_MIN_INTERVAL_SEC = 600
_enrich_feed_last_ts = 0.0
_enrich_feed_lock = threading.Lock()


def _note_enrich_batch_feed(db, batch: list) -> None:
    """一批刮削认领处理完后 → 发现页 feed 埋点（真实触发：本轮确实补全了条目）。

    文案「刮削更新 · 《X》等 N 部资料已补全」，type=enrich。
    库名取首条已补全条目的库。10 分钟节流：高频追赶期不刷屏。
    """
    global _enrich_feed_last_ts
    try:
        with _enrich_feed_lock:
            now = time.monotonic()
            if now - _enrich_feed_last_ts < _ENRICH_FEED_MIN_INTERVAL_SEC:
                return
            _enrich_feed_last_ts = now
        from backend.emby_server import models as _em
        from backend.panel_feed import note_feed

        batch_ids = [getattr(it, "id", None) for it in (batch or [])]
        batch_ids = [i for i in batch_ids if i]
        if not batch_ids:
            return
        # 只统计本轮从 pending → done 的条目（_claim_batch 只认领待补全条目）
        done_rows = (db.query(_em.MediaItem.id, _em.MediaItem.name,
                             _em.MediaItem.library_id)
                     .filter(_em.MediaItem.id.in_(batch_ids),
                             _em.MediaItem.enrich_status == "done")
                     .all())
        if not done_rows:
            return
        first = done_rows[0]
        lib_id = first[2]
        if len(done_rows) == 1:
            text = f"刮削更新 · 《{first[1] or '未知'}》资料已补全"
        else:
            text = f"刮削更新 · 《{first[1] or '未知'}》等 {len(done_rows)} 部资料已补全"
        note_feed(db, text, event_type="enrich", library_id=lib_id)
    except Exception:  # noqa: BLE001 — 埋点失败绝不能影响补全循环
        logger.warning("刮削 feed 埋点失败", exc_info=True)
_worker_lock = threading.Lock()


@dataclass(frozen=True)
class _EnrichItemSnapshot:
    """补全 IO 阶段使用的只读条目快照

    ``_claim_batch`` 提交后 ORM 对象仍绑定在 worker 的 Session 上。若随后先查父级，
    SQLAlchemy 会开启一个事务；再拿这个对象去读 FUSE / 请求 TMDB，就会把该事务
    挂在慢 IO 上。把 IO 需要的字段复制出来，rollback 后再进入 IO，数据库连接
    在整个慢阶段保持空闲且无事务。
    """
    id: int
    library_id: int
    enrich_attempts: int = 0
    item_type: str = ""
    name: str = ""
    file_path: Optional[str] = None
    size: int = 0
    container: str = ""
    production_year: Optional[int] = None
    poster_path: Optional[str] = None
    primary_image_url: Optional[str] = None
    imdb_id: Optional[str] = None
    aliases: Optional[str] = None
    tmdb_id: Optional[str] = None
    repair_requested_at: Optional[datetime] = None
    overview: Optional[str] = None
    series_id: Optional[int] = None
    parent_id: Optional[int] = None
    metadata_locked: bool = False
    # 认领归属校验用：僵尸 worker 防护（_claim_still_ours）。慢 IO 阶段不碰 ORM，
    # 写回/记失败时凭它判断"这行是否还属于我这次认领"。
    enrich_claim_token: Optional[str] = None


def _claim_still_ours(item: Any, claim_token: Optional[str]) -> bool:
    """纯函数：判断当前行是否仍属于本 worker 的认领（不碰 DB）。

    背景：worker 认领后若慢 IO 超过租约，janitor 会把行打回 pending，
    别的 worker 会重新认领并处理完。此时原 worker 是"僵尸"——它迟到的
    _mark_failed / _requeue_* 绝不许覆盖行的新状态（曾把 done 翻成 failed，
    终态卡死）。claim_token 为 None 时不做归属校验，保持旧行为（向后兼容）。
    """
    if claim_token is None:
        return True
    if item is None:
        return False
    if getattr(item, "enrich_status", None) != "enriching":
        return False
    if getattr(item, "enrich_claim_token", None) != claim_token:
        return False
    return True


def _snapshot_item(item: Any) -> _EnrichItemSnapshot:
    """在当前短事务内读取 IO 所需字段，返回与 Session 无关的快照"""
    return _EnrichItemSnapshot(
        id=item.id,
        library_id=item.library_id,
        enrich_attempts=getattr(item, "enrich_attempts", 0) or 0,
        item_type=item.item_type or "",
        name=item.name or "",
        file_path=item.file_path,
        size=item.size or 0,
        container=item.container or "",
        production_year=item.production_year,
        poster_path=item.poster_path,
        primary_image_url=item.primary_image_url,
        imdb_id=item.imdb_id,
        aliases=item.aliases,
        tmdb_id=item.tmdb_id,
        repair_requested_at=item.repair_requested_at,
        overview=item.overview,
        metadata_locked=bool(getattr(item, "metadata_locked", False)),
        series_id=item.series_id,
        parent_id=item.parent_id,
        enrich_claim_token=getattr(item, "enrich_claim_token", None),
    )


def _scanfile_from_item(item: Any) -> Optional[Any]:
    """从 DB 条目重建最小 ScanFile（只够 side_info / NFO 用）"""
    from backend.emby_server import scanner as _sc
    from backend.emby_server import mounts as mount_lib

    path = (item.file_path or "").strip()
    if not path:
        return None
    name = os.path.basename(path)
    size = item.size or 0
    container = item.container or ""
    if path.startswith("mount://"):
        rest = path[len("mount://"):]
        mount_id_str, _, rel = rest.partition("/")
        try:
            mount_id = int(mount_id_str)
        except ValueError:
            return None
        if not rel.startswith("/"):
            rel = "/" + rel
        try:
            from backend.database import SessionLocal as _SL
            _db = _SL()
            try:
                mount = _db.query(em.StorageMount).filter(
                    em.StorageMount.id == mount_id).first()
                if mount is None:
                    return None
                provider = mount_lib.build_provider(mount, _db)
            finally:
                _db.close()
        except Exception as exc:  # noqa: BLE001
            logger.warning("补全 worker 构建 provider 失败 mount=%s: %s", mount_id, exc)
            return None
        return _sc.ScanFile(
            stored_path=path, name=name, local_dir=None,
            dir_rel=os.path.dirname(rel) or "/",
            size=size, container=container,
            mount_id=mount_id, rel=rel, provider=provider,
        )
    dirpath = os.path.dirname(path)
    return _sc.ScanFile(
        stored_path=path, name=name, local_dir=dirpath,
        size=size, container=container,
    )


def _shared_ctx(item: Any, holder: Optional[dict]):
    """同组（同一部剧）内共享的 ``_ScanContext``（v2.42.9 第 5 批・处方 1）

    ctx 里的目录列举 / NFO 缓存正是扫描器「同目录只列一次」的机制，但旧实现
    每条补全都新建一个 ctx → 同一季的目录每条集列一次、``tvshow.nfo`` 每条集重读。
    ``holder`` 由 ``_worker_loop`` 按组管理（换剧即重建），缓存不随积压增长；
    holder 为 None（旧调用方 / 单测直调）时行为与旧实现一致——每条新建。
    """
    from backend.emby_server import scanner as _sc

    if holder is not None and holder.get("ctx") is not None:
        return holder["ctx"]
    snap = _sc.LibrarySnapshot(
        library_id=item.library_id, name="", collection_type="",
        paths=(), scrape_policy="smart",
    )
    ctx = _sc._ScanContext(snap=snap, lib_id=item.library_id, stats={})
    if holder is not None:
        holder["ctx"] = ctx
    return ctx


def _episode_has_own_nfo(ctx, scan_file) -> bool:
    """本集有没有自己的 NFO——只看目录列举（ctx 缓存里现成的那份），不读任何文件

    纯继承（处方 3）的判定条件之一：「一季一次列举」的返回里就含本季所有文件名，
    谁有 .nfo 一眼可见。判定不了（列举异常）时保守地当「有」——不走捷径。
    """
    from backend.emby_server import scanner as _sc

    try:
        if scan_file.local_dir is not None:
            names = set(_sc._list_dir_cached(scan_file.local_dir))
        else:
            names = {e.name for e in _sc._mount_dir_entries(
                ctx, scan_file, scan_file.dir_rel)}
    except Exception:  # noqa: BLE001 — 判定不了就不走捷径
        return True
    return any(cand in names
               for cand in _sc._nfo_candidates("episode", scan_file.name))


def _collect_multisource(item: Any, kind: str, result: dict) -> bool:
    """多源补全（Phase 6b）：总开关关着时返回 False，调用方走原豆瓣/Bangumi 兜底

    单独开一个函数而不是内联，是为了测试能直接调它（不启动 worker 线程）。
    配置读取用短会话，用完即关——IO 阶段没有写事务，不能借用。

    返回 False 的两种情况都意味着「**没接管这一块**」：开关关着、配置读不到
    （数据库异常时宁可走老路，也不能把兵底白白丢掉）。
    """
    from backend.emby_server.metasources import config as ms_config
    from backend.emby_server.metasources import engine as ms_engine
    from backend.emby_server.metasources import sources as ms_sources

    _db = SessionLocal()
    try:
        snapshot = ms_config.read_config(_db, ms_sources.SPECS)
    except Exception as exc:  # noqa: BLE001 — 读不到配置就当没开，走原来的兵底
        logger.debug("多源配置读取失败，回退到豆瓣兵底：%s", exc)
        return False
    finally:
        _db.close()
    if not snapshot.enabled:
        return False
    try:
        collected = ms_engine.collect(
            None, item.name or "", item.production_year, kind, snapshot=snapshot)
    except Exception as exc:  # noqa: BLE001 — 多源失败不影响主流程
        logger.debug("多源采集失败 %s: %s", getattr(item, "name", ""), exc)
        result["multisource_error"] = str(exc)[:200]
        return True
    if _douban_ban_downgrade_active():
        # Step 0.5（链路规划）：降级期间多源归并命中必须标题+年份都对上才落库，
        # 不满足 → 丢弃并视为未命中（宁可 none 不乱认）。
        # 返回 False（=没接管）：调用方继续走 Bangumi/终态 none 口径。
        _ms_fields = (collected.fields or {})
        if not _ban_downgrade_strict_hit_ok(
                getattr(item, "name", ""), getattr(item, "production_year", None),
                _ms_fields.get("title"), _ms_fields.get("year")):
            logger.info(
                "豆瓣封禁降级期多源命中从严丢弃：条目=%r 年=%s 归并标题=%r 年=%s",
                getattr(item, "name", ""), getattr(item, "production_year", None),
                _ms_fields.get("title"), _ms_fields.get("year"))
            result["multisource_strict_rejected"] = True
            return False
    result["multisource"] = collected.as_dict()
    return True


# ---------------------------------------------------------------------------
# 方案1 (v2.54.0): TMDB-miss 时的多源兜底（带性能护栏）
#
# 背景：v2.42.9 为救性能（8 个线程全卡在豆瓣+Bangumi 慢查询上），在 TMDB 已配置
# 且搜过无命中时直接跳过整个 fallback，导致多源引擎永不执行、后台配置的源优先级
# 形同虚设。现在加三道护栏后恢复：
#   1. 只对中文标题触发 —— TMDB 对中文收录差，中文源能补上；纯英文标题 TMDB 已是
#      最优，再问中文源是浪费；
#   2. 多源引擎自带 40s 总超时 + 7 源上限（metasources/config.py）；
#   3. 信号量限并发 —— 最多 2 个线程同时跑多源，防止慢源拖垮所有 worker。
# ---------------------------------------------------------------------------
_MULTISOURCE_SEMAPHORE = threading.Semaphore(2)


def _should_multisource_on_tmdb_miss(item: Any, kind: str) -> bool:
    """TMDB 搜过无命中时，是否值得走多源兜底。

    只对中文标题的剧/电影触发：TMDB 对中文剧集/综艺收录偏少，中文源能补上；
    非中文标题走 TMDB 已经是最优解，不必再烧一次多源。

    前置检查多源总开关：关着时直接返回 False，避免不必要的 DB 会话与引擎调用
    （测试环境默认关闭，保持原有行为不变）。

    v2.54.0 新增独立开关 ``meta_sources_tmdb_miss_fallback``（默认 0 关闭）：
    只有显式打开时才在 TMDB-miss 路径走多源兜底。避免默认开启改变现有行为。
    v2.58.0 起该开关由配置中心统一管理（``ms_config.Snapshot.tmdb_miss_fallback``），
    后台「多源元数据补全」卡里有这个开关，不再这里直查 SystemConfig。

    链路规划 Step 2：该独立开关降级为多源总开关的子项。统一兜底链
    （``enrich_unified_fallback=1``）启用时，总开关是唯一的门，不再要求
    该独立开关也开着；该键保留（回滚/兼容），语义见 ``ms_config``。
    未启用时保持原语义：须与总开关同时开着才走兜底。
    """
    if kind not in ("series", "movie"):
        return False
    if getattr(item, "tmdb_id", None):
        return False
    name = (item.name or "").strip()
    if not name:
        return False
    try:
        from backend.emby_server import douban as _dbn
        if not _dbn.has_cjk(name):
            return False
    except Exception:  # noqa: BLE001 — 判定不了就不走兜底，不影响主流程
        return False
    # 前置检查总开关；统一兜底链下独立开关不再独立门控（已降级为子项）
    try:
        from backend.emby_server.metasources import config as ms_config
        from backend.emby_server.metasources import sources as ms_sources
        _db = SessionLocal()
        try:
            snapshot = ms_config.read_config(_db, ms_sources.SPECS)
            if not snapshot.enabled:
                return False
            # 链路规划 Step 2：tmdb_miss_fallback 降级为总开关的子项。
            # 统一兜底链启用时总开关是唯一的门；未启用时保持原语义
            # （须与总开关同时开着）。
            # 配置中心统一读写（write_config 同进程即时失效快照，跨进程 30s TTL），
            # 这里不再直查 SystemConfig，避免两套读法打架。
            if _unified_fallback_flag_on():
                return True
            if not snapshot.tmdb_miss_fallback:
                return False
        finally:
            _db.close()
    except Exception:  # noqa: BLE001 — 读不到配置就不走兜底
        return False
    return True


def _multisource_master_on() -> bool:
    """多源总开关是否开着（Step 2 统一兜底链：原 else 分支的门控口径）。

    原 else 分支（Phase 6b）的 ``_collect_multisource`` 只看总开关、不要求
    中文标题（TMDB 未配置 / repair 请求时豆瓣是主数据源）。统一兜底链下
    repair 与 TMDB 未配置仍走这个口径——repair 的行为不变。
    读不到配置时按关处理（保守，不走兜底）。
    """
    try:
        from backend.emby_server.metasources import config as ms_config
        from backend.emby_server.metasources import sources as ms_sources
        _db = SessionLocal()
        try:
            return bool(ms_config.read_config(_db, ms_sources.SPECS).enabled)
        finally:
            _db.close()
    except Exception:  # noqa: BLE001 — 读不到配置就不走兜底
        return False


def _fallback_unified(item: Any, kind: str, result: dict,
                      needs_repair: bool) -> None:
    """Step 2 统一兜底链：豆瓣 rich（CJK 专属）之后，只剩多源引擎这一个调用点。

    一个豆瓣入口：轻量 ``altmeta.search`` 已并入多源引擎做"豆瓣源"
    （``metasources/sources.py`` 的 ``search_douban``），这里不再直调
    ``altmeta.search`` / ``search_bangumi``。

    门控口径与原分支逐一对应（可达性分析，TMDB 配置 × repair）：
    - TMDB 已配置 & 非 repair：``_should_multisource_on_tmdb_miss``
      （中文标题 + 总开关；``tmdb_miss_fallback`` 已降级为子项）；
    - repair / TMDB 未配置：只看多源总开关（原 else 的 ``_collect_multisource``
      口径，不要求中文标题；repair 仍能走到引擎，行为不变）。

    Step 0.5 收编：降级判断复用 ``_douban_ban_downgrade_active()``（rich 跳过
    在调用方已处理）；引擎自身的封禁感知（封禁期跳过豆瓣源、零豆瓣请求）保留，
    不另起一套 flag 或判断。

    反风控：引擎内豆瓣源走 ``altmeta.search``（单条目单次请求），与原轻量直调
    同口径；引擎自带逐源限速（``meta_source_rate_douban``，默认 1.0s），
    不改任何限流/超时/退避参数。
    """
    from backend.emby_server.tmdb import tmdb_client
    try:
        if tmdb_client.configured and not needs_repair:
            _ms_go = _should_multisource_on_tmdb_miss(item, kind)
        else:
            # 原 else 口径：只看多源总开关（不要求中文标题）。
            _ms_go = _multisource_master_on()
        if _ms_go and _collect_multisource_guarded(item, kind, result):
            progress.note_stage("enrich_multisource_fallback")
        else:
            progress.note_stage("enrich_fallback_skip")
    except Exception:  # noqa: BLE001 — 兜底失败不影响主流程
        progress.note_stage("enrich_fallback_skip")


def _collect_multisource_guarded(item: Any, kind: str, result: dict) -> bool:
    """带并发限制的多源采集（方案1 的护栏3）。

    拿不到信号量时直接返回 False（调用方记 skip，不阻塞 worker 线程）。
    """
    if not _MULTISOURCE_SEMAPHORE.acquire(blocking=False):
        logger.debug("多源并发已满(2)，跳过兜底 %r", getattr(item, "name", ""))
        return False
    try:
        return _collect_multisource(item, kind, result)
    finally:
        _MULTISOURCE_SEMAPHORE.release()


def _fetch_episode_tmdb(item: Any, inherit_parent: Optional[dict],
                       result: dict) -> None:
    """单集 TMDB 补全（v2.50.0）：父级剧有 tmdb_id 时，拉取本集的标题/简介/剧照。

    走季接口批量拉（`TmdbClient.season_episodes` 带两级缓存），同一季的多个集
    只打 1 次 TMDB。结果存入 result["episode_tmdb"]，写库阶段由
    `_enrich_apply` 落库。

    批3（分项开关）：标题 / 简介 / 剧照三项全关时，本函数直接返回，
    一个 TMDB 请求都不打（与「单集下沉总开关关」同效）；只关剧照时，
    跳过剧照预热（不写 episode_still_url）。

    「TMDB 没这集」才静默（不影响 pure_inherit 的主流程）：只是本集拿不到标题/剧照，
    图片仍沿父级回退。瞬态失败（网络/限流，``TmdbTransientError``）**上抛**：
    整条进补全重试队列，下一轮把本集标题/剧照补上——吞掉它就是静默写 done
    （问题一：时好时坏的另一个缺口）。
    """
    from backend.emby_server.tmdb import (
        image_base,
        prewarm_images,
        tmdb_client,
        TmdbTransientError,
    )

    # 批3：分项开关（IO 阶段无 db，用环境变量/缓存口径，与夜间配置同理）
    ep_fields = _episode_field_config()
    if not (ep_fields["title"] or ep_fields["overview"] or ep_fields["still"]):
        return

    series_tmdb_id = (inherit_parent or {}).get("tmdb_id")
    if not series_tmdb_id:
        # 标记「因父级无 id 而跳过」：写库阶段据此判定竞态（父级在并发中刚落 id，
        # v2.58.0 联动兜底）。与「跑过但 TMDB 没这集」（确定性结论，不重跑）严格区分。
        result["episode_tmdb_skipped_no_parent_id"] = True
        return
    season_no = getattr(item, "season_number", None)
    ep_no = getattr(item, "episode_number", None)
    if season_no is None or ep_no is None:
        return
    try:
        ep_data = tmdb_client.find_episode(str(series_tmdb_id), season_no, ep_no)
    except TmdbTransientError:
        # 瞬态失败不静默：上抛让整条进重试队列（见函数头），
        # 与「TMDB 没这集」的静默路径严格区分
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("单集 TMDB 查询失败 series=%s S%sE%s: %s",
                     series_tmdb_id, season_no, ep_no, exc)
        return
    if not ep_data:
        return
    result["episode_tmdb"] = ep_data
    # 剧照预热（IO 阶段下载，写库阶段只落字段，与海报同一口径）
    # 批3：剧照分项关时跳过预热（不写 episode_still_url，写库阶段也就无从落图）
    still_path = ep_data.get("still_path")
    if still_path and ep_fields["still"]:
        try:
            base = image_base()
            # 剧照 16:9，用 w500（与海报同尺寸口径）
            still_url = f"{base}/w500{still_path}"
            result["episode_still_url"] = still_url
            prewarm_images(extra=[still_url])
        except Exception as exc:  # noqa: BLE001
            logger.debug("单集剧照预热失败: %s", exc)


def _tmdb_plan_for_item(item, nfo_data, kind, needs_repair):
    """根据 item 与 nfo 规划 TMDB 操作（纯函数，无网络调用）。

    返回三种计划之一：
    - {"op": "details", ...}：需要拉取 TMDB 详情
    - {"op": "search", ...}：需要按名称搜索
    - {"op": "none"}：无需操作
    """
    from backend.emby_server.tmdb import tmdb_client

    name = getattr(item, "name", None)
    production_year = getattr(item, "production_year", None)
    poster_path = getattr(item, "poster_path", None)
    primary_image_url = getattr(item, "primary_image_url", None)

    # 1) NFO 中已有 tmdb_id：仅在需要补详情且客户端已配置时拉取
    # 批2（刮削瘦身）：拉详情的理由收窄——缺 imdb_id/别名不再是理由
    # （那是文字字段，TMDB 按新口径不再负责补）；只留缺图与修图请求。
    if nfo_data and nfo_data.get("tmdb_id"):
        tmdb_id = str(nfo_data["tmdb_id"])
        want_details = bool(
            needs_repair
            or not (poster_path or primary_image_url))
        if want_details and tmdb_client.configured:
            return {"op": "details", "tmdb_id": tmdb_id, "why": "nfo", "kind": kind,
            "name": name or "", "year": production_year}
        return {"op": "none"}

    # 2) 影视类：先查别名映射，命中则直接拉详情，否则按名称搜索
    # （v2.56.0 起 TMDB 为主：不再跳过豆瓣命中的条目，豆瓣只在 TMDB 未命中时兜底）
    if tmdb_client.configured and kind in ("series", "movie"):
        alias_id = _alias_tmdb_id(name or "")
        if alias_id:
            return {"op": "details", "tmdb_id": str(alias_id), "why": "alias", "kind": kind,
            "name": name or "", "year": production_year}
        return {"op": "search", "name": name or "", "year": production_year,
                "kind": kind, "existing_id": getattr(item, "tmdb_id", None)}

    # 3) 其余情况不做任何操作
    return {"op": "none"}

def _run_tmdb_batch(indexed_plans):
    """三轮批量执行 TMDB 请求。

    注意：op 为 "details" 且 why 为 "alias" 的 plan 必须额外携带 "name" 和
    "year" 字段，供 Round 2 的 stale-alias 回退 search 使用（details plan
    本身没有 name/year，调用方需预先填入）。
    """
    from backend.emby_server.tmdb import tmdb_client

    out = {}  # key -> {"hit": None, "details": None, "transient": False}
    if not indexed_plans:
        return out
    for key, _ in indexed_plans:
        out[key] = {"hit": None, "details": None, "transient": False}

    # Round 1: details
    r1_keys = []
    r1_ids = []
    for key, plan in indexed_plans:
        if plan["op"] == "details":
            r1_keys.append(key)
            r1_ids.append((plan["tmdb_id"], plan["kind"]))
    r1_transient = set()
    r1_details = {}
    if r1_ids:
        results, transient_idx = tmdb_client.batch_details(
            r1_ids, return_transient=True
        )
        for i, key in enumerate(r1_keys):
            r1_details[key] = results[i]
            if i in transient_idx:
                r1_transient.add(key)

    # Round 2: search（含 stale alias 回退）
    r2_keys = []
    r2_queries = []
    r2_is_stale = {}  # key -> True 表示是 stale-alias 回退的 search
    for key, plan in indexed_plans:
        if plan["op"] == "search":
            r2_keys.append(key)
            r2_queries.append((plan["name"], plan["year"], plan["kind"]))
        elif plan["op"] == "details" and plan.get("why") == "alias":
            # r1 未命中且非瞬时错误 → stale，用 plan 里的 name/year/kind 回退 search
            if r1_details.get(key) is None and key not in r1_transient:
                r2_keys.append(key)
                r2_queries.append((plan["name"], plan["year"], plan["kind"]))
                r2_is_stale[key] = True
    r2_hits = {}
    r2_transient = set()
    if r2_queries:
        hits, transient_idx = tmdb_client.batch_search(
            r2_queries, return_transient=True
        )
        for i, key in enumerate(r2_keys):
            r2_hits[key] = hits[i]
            if i in transient_idx:
                r2_transient.add(key)

    # Round 3: details for hits + existing_id fallback
    r3_keys = []
    r3_ids = []
    for key, plan in indexed_plans:
        if key not in r2_hits:
            continue
        hit = r2_hits[key]
        if hit is not None:
            r3_keys.append(key)
            r3_ids.append((str(hit["id"]), plan["kind"]))
        elif plan.get("existing_id"):
            r3_keys.append(key)
            r3_ids.append((plan["existing_id"], plan["kind"]))
    r3_details = {}
    r3_transient = set()
    if r3_ids:
        results, transient_idx = tmdb_client.batch_details(
            r3_ids, return_transient=True
        )
        for i, key in enumerate(r3_keys):
            r3_details[key] = results[i]
            if i in transient_idx:
                r3_transient.add(key)

    # 汇总到 out
    for key, plan in indexed_plans:
        if plan["op"] == "search" or r2_is_stale.get(key):
            # search plan 或 stale alias 回退：hit/details 来自 r2/r3
            out[key]["hit"] = r2_hits.get(key)
            out[key]["details"] = r3_details.get(key)
            out[key]["transient"] = (key in r2_transient) or (key in r3_transient)
        else:
            # details plan (why nfo/alias 且未 stale)
            out[key]["details"] = r1_details.get(key)
            out[key]["transient"] = key in r1_transient

    return out

def _apply_tmdb_plan_result(result, item, plan, out, kind):
    """把批量 TMDB 结果按 plan 写回 result dict。

    参数:
        result: dict，已有 tmdb_hit/tmdb_details 等键，可直接修改。
        item: 对象，有 .name 属性。
        plan: dict，形如 {"op": "details", "tmdb_id": str, "why": "nfo"|"alias",
            "kind": str, "name": str, "year": ...} 或 {"op": "search", ...}。
        out: dict，形如 {"hit": hit_or_None, "details": details_or_None,
            "transient": bool}。
        kind: 字符串，条目类型（本函数未直接使用，保留对齐调用方签名）。

    语义:
        - transient：标记失败并返回，等待后续重试。
        - details/nfo：直接写 tmdb_details。
        - details/alias：命中则写 tmdb_id 与 tmdb_details；stale（TMDB 404）
          则回退到 search 的 hit/details。
        - search：写 tmdb_hit 与 tmdb_details。
    """
    if out.get("transient"):
        result["ok"] = False
        result["error"] = "tmdb transient, retry later"
        return
    op = plan["op"]
    if op == "details" and plan.get("why") == "nfo":
        result["tmdb_details"] = out["details"]
    elif op == "details" and plan.get("why") == "alias":
        progress.note_stage("enrich_alias_hit")
        if out["details"] is not None:
            result["tmdb_id"] = plan["tmdb_id"]
            result["tmdb_details"] = out["details"]
        else:
            # stale：回退到 search 的结果（out 里已是 search 的 hit/details）
            progress.note_stage("enrich_alias_stale")
            logger.warning("alias tmdb_id=%s stale (TMDB 404), fallback to search name=%r",
                           plan["tmdb_id"], item.name)
            result["tmdb_hit"] = out["hit"]
            if out["hit"] and out["hit"].get("id"):
                result["tmdb_id"] = str(out["hit"]["id"])
            result["tmdb_details"] = out["details"]
    elif op == "search":
        result["tmdb_hit"] = out["hit"]
        result["tmdb_details"] = out["details"]


def _enrich_fetch_pre(item: Any, holder: Optional[dict] = None,
                      inherit_parent: Optional[dict] = None):
    """IO 阶段前半：side 图片/字幕 → NFO（v2.56.0 起 TMDB 为主，豆瓣只在 TMDB 未命中时兜底）。

    返回 (result, tmdb_ctx)：tmdb_ctx 为 None 表示已终态（pure_inherit 或失败），
    无需走 TMDB；否则 tmdb_ctx 含 kind/needs_repair/tmdb_plan，供批量或单条 TMDB 用。

    返回待写入的数据包，写库阶段只做纯 DB 操作。

    v2.42.9 第 5 批（处方 1+3）：
    - ``holder``：同组（同一部剧）内共享的 ``_ScanContext``（目录列举 / NFO 缓存），
      由 ``_worker_loop`` 在组边界上重置——同一季的目录只列一次、tvshow.nfo 只读一次；
    - ``inherit_parent``：父级剧已 done 且有图/tmdb_id 时的快照（见 ``_inherit_parent_info``）。
      本集没有自己的 NFO 时走**纯继承**：不读 NFO、不查 TMDB，图片在写库阶段沿父级回退。
    """
    from backend.emby_server import scanner as _sc
    from backend.emby_server import nfo as nfo_lib
    from backend.emby_server.tmdb import tmdb_client

    result: dict = {
        "poster": None, "fanart": None, "external_subs": [],
        "nfo_data": None, "tmdb_hit": None, "tmdb_details": None,
        "ok": True, "error": None,
    }
    item_type = item.item_type or ""
    # NFO kind 口径：series→tvshow.nfo，season→season.nfo，episode→episodedetails，movie→movie
    kind = {"series": "series", "season": "season",
            "episode": "episode", "movie": "movie"}.get(item_type, "")
    scan_file = _scanfile_from_item(item)

    # series / season 是目录聚合出来的容器条目，天生没有 file_path（文件在 episode 上）。
    # 以前这里直接判失败，而下面第 3 步的 TMDB 只需要 name + year，根本用不到
    # file_path —— 于是所有剧集/季永远刮削不了，重试到超限后锁死 failed。
    # 现在：没有 scan_file 时跳过 side/NFO 这两个「确实需要本地文件」的步骤，
    # 但仍然走 TMDB。episode/movie 有 file_path，不走这条退路。
    ctx = None
    nfo_data = None
    if scan_file is not None:
        ctx = _shared_ctx(item, holder)

        # 1. side 图片 / 外挂字幕（本地图片优先，不覆盖已有）
        try:
            side_poster, side_fanart, external = _sc._side_info(ctx, scan_file)
            result["poster"] = side_poster
            result["fanart"] = side_fanart
            result["external_subs"] = list(external or [])
        except Exception as exc:  # noqa: BLE001
            logger.debug("补全 side 失败 %s: %s", item.file_path, exc)

        # 1.5 纯继承快路径（处方 3）：父级已 done、本集没有自己的 NFO →
        #     目录列举已在上一步进 ctx 缓存（同一份），谁有 .nfo 一眼可见；
        #     不读 NFO、不查 TMDB，图片在写库阶段沿父级回退。
        #     有自带 NFO 的集不走这条（NFO 里可能有本集专属数据）。
        if (inherit_parent and kind == "episode"
                and not _episode_has_own_nfo(ctx, scan_file)):
            result["pure_inherit"] = True
            progress.note_stage("enrich_pure_inherit")
            # 单集 TMDB 补全（v2.50.0）：父级有 tmdb_id 时拉取本集标题/简介/剧照
            # 季接口带缓存，同一季多集只打 1 次 TMDB；失败静默，不影响继承主流程
            _fetch_episode_tmdb(item, inherit_parent, result)
            return result, None

        # 2. NFO（B 方案：NFO 管文字；series/season/episode/movie 全支持）
        if kind:
            try:
                nfo_data, _s1, _s2 = _sc._nfo_work(ctx, scan_file, kind) or (None, None, None)
                result["nfo_data"] = nfo_data
            except Exception as exc:  # noqa: BLE001
                logger.debug("补全 NFO 失败 %s: %s", item.file_path, exc)
    elif kind in ("episode", "movie"):
        # 真正需要本地文件却没有 → 无从补全，判失败让退避重试
        result["ok"] = False
        result["error"] = "无 file_path，无法重建 ScanFile"
        return result, None
    else:
        logger.debug("补全 %s 无 file_path（容器条目），仅走 TMDB：%s",
                     item_type, item.name)

    # 2.5 优先级（v2.56.0 起）：TMDB 为主、豆瓣兜底。
    # 中文标题不再先走豆瓣——TMDB 先搜，搜不中才在 _enrich_fetch_tmdb_post
    # 里走豆瓣 rich 兜底（search + details + cast）。豆瓣反封禁机制保留，
    # 兜底阶段被封禁同样整条留待冷却后重试。

    # 单集 TMDB 数据（v2.55.0）：有自带 NFO 的单集不走纯继承，但父级有 tmdb_id 时
    # 仍应拉取本集标题/简介/剧照——之前只在纯继承分支调 _fetch_episode_tmdb，
    # 有 NFO 的单集永远拿不到单集级数据。在这里补调（幂等，失败静默）。
    if (kind == "episode" and inherit_parent
            and not result.get("pure_inherit")
            and not result.get("episode_tmdb")):
        from backend.emby_server.tmdb import TmdbTransientError as _TTE
        try:
            _fetch_episode_tmdb(item, inherit_parent, result)
        except _TTE:
            # 瞬态失败（网络/限流）上抛：整条进重试队列，与纯继承分支同口径
            raise
        except Exception:  # noqa: BLE001 — 非瞬态失败静默，不影响主流程
            pass
    # NFO tmdb_id 无条件落 result（原 TMDB 段首行逻辑，前移以便批量规划）
    if nfo_data and nfo_data.get("tmdb_id"):
        result["tmdb_id"] = str(nfo_data["tmdb_id"])
    needs_repair = bool(getattr(item, "repair_requested_at", None))
    tmdb_plan = _tmdb_plan_for_item(
        item, nfo_data, kind, needs_repair)
    # v2.57.0 提速包 item2：豆瓣封禁期内，注定走豆瓣兜底的 CJK 条目不再烧 TMDB
    # 预搜索（search 计划=6-9 个请求/条，采样 10/10 未命中）。直接标 banned 延后，
    # Phase 2 批量因 plan=none 跳过它，一个 TMDB 请求都不烧。
    # needs_repair（用户等着的修复请求）不跳过：宁可多烧几次也不多等一个周期。
    #
    # Step 0.5（链路规划）：降级激活时不再整条延后——跳过豆瓣这一站，TMDB 预搜索
    # 照常跑（"注定延后所以不烧预搜索"的前提已不存在）。flag 关时下面 else 分支
    # 与现状逐字节一致。
    if (tmdb_plan.get("op") == "search" and not needs_repair
            and _douban_ban_presearch_skip(item, kind)):
        if _douban_ban_downgrade_active():
            logger.info("豆瓣封禁降级激活：%r 不延后，TMDB 预搜索照常跑",
                        getattr(item, "name", ""))
        else:
            result["ok"] = False
            result["douban_banned"] = True
            result["error"] = "douban banned; tmdb pre-search skipped during ban"
            tmdb_plan = {"op": "none"}
    tmdb_ctx = {
        "kind": kind,
        "needs_repair": needs_repair,
        "tmdb_plan": tmdb_plan,
    }
    return result, tmdb_ctx


def _douban_ban_presearch_skip(item: Any, kind: str) -> bool:
    """豆瓣封禁期是否跳过 TMDB 预搜索（v2.57.0 提速包 item2）。

    条件与 _douban_fallback_on_tmdb_miss 的触发条件同口径：
    封禁中 + 剧/电影 + 无 tmdb_id + 未锁定 + CJK 标题 + 豆瓣兜底开着。
    这类条目这轮注定走「豆瓣兜底 → banned → 延后」，TMDB 搜索的 6-9 个请求
    纯属浪费（生产采样：CJK 剧 TMDB 搜索 10/10 未命中；封禁期约 400+ 请求/小时
    白烧，2026-10-13）。

    tradeoff：封禁期内 TMDB 本可命中的 CJK 条目会被多延后一个封禁周期。
    判定不了（异常）时保守返回 False，不跳过。
    """
    if kind not in ("series", "movie"):
        return False
    if getattr(item, "tmdb_id", None):
        return False
    if getattr(item, "metadata_locked", False):
        return False
    name = (getattr(item, "name", None) or "").strip()
    if not name:
        return False
    from backend.emby_server import douban as _dbn
    if _dbn.ban_remaining() <= 0:
        return False
    try:
        if not _dbn.has_cjk(name):
            return False
    except Exception:  # noqa: BLE001 — 判定不了就不跳过（保守）
        return False
    try:
        on, _iv = _cached_douban_cfg()
    except Exception:  # noqa: BLE001
        on = False
    return bool(on)


def _douban_backfill_candidate(item: Any, kind: str) -> bool:
    """该条目是否「欠一次豆瓣」：条件与 _douban_fallback_on_tmdb_miss 的触发
    口径一致（剧/电影 + 无 tmdb_id + 未锁定 + CJK 标题 + 豆瓣兜底开着），但
    不看封禁状态——调用方正是因为本轮豆瓣被跳过/不可用才来判定要不要给它
    留补刮记号。判定不了（异常）时保守返回 False，不留记号。
    """
    if kind not in ("series", "movie"):
        return False
    if getattr(item, "tmdb_id", None):
        return False
    if getattr(item, "metadata_locked", False):
        return False
    name = (getattr(item, "name", None) or "").strip()
    if not name:
        return False
    from backend.emby_server import douban as _dbn
    try:
        if not _dbn.has_cjk(name):
            return False
    except Exception:  # noqa: BLE001 — 判定不了就不留记号（保守）
        return False
    try:
        on, _iv = _cached_douban_cfg()
    except Exception:  # noqa: BLE001
        on = False
    return bool(on)


def _douban_fallback_on_tmdb_miss(item: Any, kind: str, result: dict) -> object:
    """TMDB 未命中时的豆瓣 rich 兜底（v2.56.0 起：TMDB 为主、豆瓣兜底）。

    只对中文标题的剧/电影触发：TMDB 对中文剧集/综艺收录偏少，豆瓣能补上；
    非中文标题走 TMDB 已经是最优解，不必再烧豆瓣。

    做 search + get_details + get_celebrities 三件套（与旧"豆瓣优先"同口径），
    结果存 ``douban_fallback_hit`` / ``douban_details`` / ``douban_cast``，
    写库阶段只补 TMDB 没给的字段（见 _enrich_apply）。

    豆瓣反封禁机制完整保留：被封禁时返回 "banned"，调用方整条留待冷却后
    重试（不烧重试次数、不终态化）。

    返回：True=命中 / False=没命中 / "banned"=被封禁。
    """
    from backend.emby_server import douban as _dbn

    if kind not in ("series", "movie"):
        return False
    if getattr(item, "tmdb_id", None):
        return False
    if getattr(item, "metadata_locked", False):
        return False
    name = (item.name or "").strip()
    if not name:
        return False
    try:
        if not _dbn.has_cjk(name):
            return False
    except Exception:  # noqa: BLE001 — 判定不了就不走兜底
        return False
    try:
        # P2（完美审查 C-P2-5）：豆瓣开关走 60s TTL 进程内缓存
        _dbn_on, _dbn_iv = _cached_douban_cfg()
        if not _dbn_on:
            return False
        _dbn.client._interval = _dbn_iv
        _dh = _dbn.client.search(name, item.production_year, kind)
        if not (_dh and _dh.get("id")):
            return False
        result["douban_fallback_hit"] = _dh
        result["_douban_fallback_tried"] = True
        _dd = _dbn.client.get_details(_dh["id"])
        if _dd:
            result["douban_details"] = _dd
        # TMDB 没给演员表时，用豆瓣演职员表兜底（写库阶段 _apply_douban_cast）
        try:
            _dc = _dbn.client.get_celebrities(_dh["id"])
        except _dbn.DoubanBannedError:
            raise
        except Exception as exc:  # noqa: BLE001 — 演员表是增强信息
            logger.debug("豆瓣兜底演职员表失败 %s: %s", item.name, exc)
            _dc = []
        if _dc:
            result["douban_cast"] = _dc
        progress.note_stage("enrich_douban_fallback")
        logger.info("[douban] 兜底命中 %r -> %s", item.name, _dh.get("title"))
        return True
    except _dbn.DoubanBannedError as exc:
        # 封禁 ≠ 没搜中：整条留待冷却后重试（不烧重试次数）
        result["_douban_fallback_tried"] = True
        logger.debug("豆瓣兜底被封禁 %s: %s", item.name, exc)
        return "banned"
    except Exception as exc:  # noqa: BLE001 — 兜底失败不影响主流程
        logger.debug("豆瓣兜底失败 %s: %s", item.name, exc)
        return False


def _enrich_fetch_tmdb_post(result: dict, item: Any, tmdb_ctx: dict,
                              batch_out: Optional[dict] = None) -> dict:
    """IO 阶段后半：TMDB（批量或单条）→ 原语言海报 → 豆瓣兜底 → 图片预热。

    batch_out 为 None 时走原逐条逻辑（_sc._tmdb_work）；否则为
    _run_tmdb_batch 返回的单条结果 {"hit","details","transient"}，走批量应用。
    返回 result（原地修改）。
    """
    from backend.emby_server import scanner as _sc
    from backend.emby_server.tmdb import tmdb_client

    kind = tmdb_ctx["kind"]
    needs_repair = tmdb_ctx["needs_repair"]
    nfo_data = result.get("nfo_data")
    # 3. TMDB（NFO 有 tmdb_id 就不搜，只按需取详情补图/补缺；
    # v2.56.0 起 TMDB 为主：不再跳过任何条目，豆瓣只在 TMDB 未命中时兜底）
    # result["tmdb_id"] 已在 pre 阶段赋值（NFO 有 tmdb_id 时）
    try:
        if batch_out is not None:
            # 批量路径：规划已在 pre 阶段完成，直接应用批量结果
            _apply_tmdb_plan_result(
                result, item, tmdb_ctx["tmdb_plan"], batch_out, kind)
        else:
                if nfo_data and nfo_data.get("tmdb_id"):
                    tmdb_id = str(nfo_data["tmdb_id"])
                    result["tmdb_id"] = tmdb_id
                    want_details = bool(
                        needs_repair
                        or not (item.poster_path or item.primary_image_url)
                        or not (item.imdb_id and item.aliases))
                    if want_details and tmdb_client.configured:
                        _hit, details = _sc._tmdb_work(
                            False, "", None, kind, tmdb_id, True)
                        result["tmdb_details"] = details
                elif tmdb_client.configured and kind in ("series", "movie"):
                    # 处方 12・零 API 别名匹配：同一部剧已在库里（换文件名/换译名重扫）
                    # 时，1 次 details 请求替代 4~5 次候选搜索——且绝不写错名字
                    # （只补 id/图/别名，名字以库里那份为准）。
                    alias_id = _alias_tmdb_id(item.name or "")
                    if alias_id:
                        progress.note_stage("enrich_alias_hit")
                        _hit, details = _sc._tmdb_work(
                            False, "", None, kind, alias_id, True)
                        if details is None:
                            progress.note_stage("enrich_alias_stale")
                            logger.warning(
                                "alias tmdb_id=%s stale (TMDB 404), fallback to search name=%r",
                                alias_id, item.name)
                            hit, details = _sc._tmdb_work(
                                True, item.name or "", item.production_year, kind,
                                None, True)
                            result["tmdb_hit"] = hit
                            if hit and hit.get("id"):
                                result["tmdb_id"] = str(hit["id"])
                        else:
                            result["tmdb_id"] = alias_id
                        result["tmdb_details"] = details
                    else:
                        hit, details = _sc._tmdb_work(
                            True, item.name or "", item.production_year, kind,
                            getattr(item, "tmdb_id", None), True)
                        result["tmdb_hit"] = hit
                        result["tmdb_details"] = details
        # Step 0.5（链路规划）：降级期间 TMDB 命中同样从严——标题+年份都对上
        # 才算命中，不满足 → 丢弃命中，继续走下面的多源兜底（宁可 none 不乱认）。
        # 只筛 search 出来的 hit：单条/批量路径里 tmdb_hit 只在 search 分支
        # （含别名 stale 回退 search）被赋值，NFO/别名按 id 取详情的不受影响。
        # 标题取 hit 的任一官方变体（TMDB 可能用原名命中中文查询）。
        if _douban_ban_downgrade_active() and result.get("tmdb_hit"):
            _th = result.get("tmdb_hit") or {}
            _t_titles = [_th.get("title"), _th.get("name"),
                         _th.get("original_title"), _th.get("original_name")]
            _t_year = ((_th.get("first_air_date") or _th.get("release_date")
                        or "")[:4])
            if not any(_ban_downgrade_strict_hit_ok(
                    getattr(item, "name", ""),
                    getattr(item, "production_year", None),
                    _t, _t_year) for _t in _t_titles if _t):
                logger.info(
                    "豆瓣封禁降级期 TMDB 命中从严丢弃：条目=%r 年=%s 命中标题=%r 年=%s",
                    getattr(item, "name", ""),
                    getattr(item, "production_year", None),
                    _th.get("title") or _th.get("name"), _t_year or None)
                result.pop("tmdb_hit", None)
                result.pop("tmdb_details", None)
                result["tmdb_strict_rejected"] = True
        # 原语言海报（StrmAssistant #10）：IO 阶段预取语言偏好的海报，
        # 写库阶段直接用（写事务里不碰网络，见模块注释处方 1）。
        # poster_language() == "system" 时 images_with_language 返回 None，不额外请求。
        try:
            from backend.emby_server.tmdb import poster_language as _poster_lang
            if _poster_lang() != "system":
                _hit_for_img = result.get("tmdb_hit") or {}
                _tid = str(_hit_for_img.get("id") or getattr(item, "tmdb_id", "") or "")
                if _tid:
                    result["tmdb_images_lang"] = tmdb_client.images_with_language(_tid, kind)
        except Exception:
            result["tmdb_images_lang"] = None
        # v2.57.0 提速包 item2：pre 阶段已在豆瓣封禁期判定跳过（TMDB 预搜索
        # 没跑），这里不再试豆瓣兜底——直接返回，调用方走 _requeue_douban_banned
        # 延后。等价于兜底返回 "banned" 的那条路（ok=False + douban_banned）。
        if result.get("douban_banned"):
            return result
        # 豆瓣兜底（v2.56.0 起：TMDB 为主、豆瓣兜底）。
        # TMDB 搜过无命中时，中文标题先走豆瓣 rich 兜底（search + details + cast，
        # 与旧"豆瓣优先"同口径，只是时机从"TMDB 之前"挪到"TMDB 未命中之后"）。
        # 只补 TMDB 没给的（标题/年份/海报/简介/演员），绝不覆盖已有数据。
        #
        # v2.42.9 父级快速失败（生产实测：第 5 批后 65 条/分 → 0.3 条/分）：
        # 成组抢单让父级先行，而 TMDB 搜不到的父级会串行等豆瓣 + Bangumi
        # 兑底（各 15s 超时），8 个线程全部卡死在几十个父级上，
        # 后面上万单集排队。现在：TMDB 已配置且真的搜过（无高置信命中）时
        # 直接跳过兜底——父级由写库阶段终态化（done + none，见 _enrich_apply），
        # 子集立刻纯继承；兜底仍保留：TMDB 未配置（豆瓣是主数据源）、
        # repair 请求（用户等着的）。跳过的随时可用管理端「重试未匹配项」补搜。
        #
        # v2.54.0 方案1：TMDB 搜过无命中时，对中文标题恢复多源兜底
        # （_should_multisource_on_tmdb_miss + _collect_multisource_guarded，
        # 三道护栏见函数上方的注释）。非中文标题仍跳过——TMDB 已是最优。
        # 外层 try 包住：任何异常都回退到 skip，绝不影响原有终态逻辑。
        if (kind in ("series", "movie") and not getattr(item, "tmdb_id", None)
                and not result.get("tmdb_hit")):
            # Step 0.5（链路规划）：降级激活时跳过豆瓣 rich 兜底——一次豆瓣请求
            # 都不发，直接走下面的多源兜底分支。flag 关时走原逻辑。
            if _douban_ban_downgrade_active():
                logger.info("豆瓣封禁降级激活：%r 跳过豆瓣 rich 兜底，走多源兜底",
                            getattr(item, "name", ""))
                result["_ban_downgrade_skipped_douban"] = True
                _fb = False  # 等价于豆瓣没命中，下游走多源兜底
            else:
                # TMDB 为主、豆瓣兜底：先试豆瓣 rich 兜底（中文标题）
                _fb = _douban_fallback_on_tmdb_miss(item, kind, result)
            if _fb == "banned":
                # 封禁 ≠ 没搜中：整条留待冷却后重试（不烧重试次数、不终态化）
                result["ok"] = False
                result["douban_banned"] = True
                result["error"] = "douban banned on fallback"
                return result
            if _fb:
                pass  # 豆瓣兜底已命中，不再走多源/轻量兜底（避免重复打豆瓣）
            elif _unified_fallback_flag_on():
                # ★ 链路规划 Step 2：统一兜底链。rich（CJK 专属）之后只剩
                # 多源引擎（含豆瓣源）这一个调用点——一个豆瓣入口。
                # 原 elif/else 分支用 flag 包裹保留（暂不物理删除），
                # 下一轮规划确认无回归后再删。
                _fallback_unified(item, kind, result, needs_repair)
            elif tmdb_client.configured and not needs_repair:
                try:
                    if _should_multisource_on_tmdb_miss(item, kind):
                        if _collect_multisource_guarded(item, kind, result):
                            progress.note_stage("enrich_multisource_fallback")
                        else:
                            progress.note_stage("enrich_fallback_skip")
                    else:
                        progress.note_stage("enrich_fallback_skip")
                except Exception:  # noqa: BLE001 — 兜底失败不影响主流程
                    progress.note_stage("enrich_fallback_skip")
            else:
                # Phase 6b（Step 2 flag 包裹保留，暂不物理删除）：
                # 多源总开关打开时，这一块交给多源引擎
                # （七个源按顺序问、字段按序填充、单源失败隔离）。
                # 关着时走原来的豆瓣 → Bangumi 兜底，行为与升级前一致。
                # enrich_unified_fallback=1 时走上面的 _fallback_unified，
                # 不再进入这里。
                if _collect_multisource(item, kind, result):
                    progress.note_stage("enrich_multisource")
                else:
                    from backend.emby_server import altmeta
                    # P2（完美审查 C-P2-5）：配置读走 60s TTL 进程内缓存
                    # （_cached_altmeta_cfg），不再逐条目开短会话。
                    # （_enrich_fetch 是 IO 阶段函数、只收 item 没有 db，
                    # 之前因此另开短会话，用完即关。）
                    _ok, _interval, _bgm_interval = _cached_altmeta_cfg()
                    if _ok:
                        # v2.56.0：rich 豆瓣兜底已试过豆瓣就不再重复打（省一次请求，
                        # 且 rich 路径自带反封禁保护）。没试过才走轻量 altmeta.search。
                        # Step 0.5（链路规划）：降级激活时轻量豆瓣搜索同样跳过
                        # （封禁期零豆瓣请求），只留 Bangumi。
                        if not result.get("_douban_fallback_tried"):
                            if _douban_ban_downgrade_active():
                                logger.info(
                                    "豆瓣封禁降级激活：%r 跳过轻量豆瓣搜索",
                                    item.name)
                                result.setdefault("douban_hit", None)
                            elif not _douban_switch_on():
                                # 批 1：豆瓣降为可选开关。只开 altmeta 不开豆瓣时，
                                # 轻量豆瓣搜索同样不打（避免绕过开关的豆瓣请求）。
                                logger.info("豆瓣开关已关闭：%r 跳过轻量豆瓣搜索",
                                            item.name)
                                result.setdefault("douban_hit", None)
                            else:
                                try:
                                    result["douban_hit"] = altmeta.search(
                                        item.name or "", item.production_year, kind, _interval)
                                except Exception as exc:  # noqa: BLE001 — 兜底源失败不影响主流程
                                    logger.debug("豆瓣兜底失败 %s: %s", item.name, exc)
                        # 豆瓣限流/没命中时再试 Bangumi——它有 name_cn 与封面，
                        # 对 TMDB 收录差的中文剧集特别有用（实测命中 B-PROJECT、落语朱音）。
                        if not result["douban_hit"]:
                            try:
                                result["bangumi_hit"] = altmeta.search_bangumi(
                                    item.name or "", item.production_year, kind,
                                    _bgm_interval)
                            except Exception as exc:  # noqa: BLE001
                                logger.debug("Bangumi 兜底失败 %s: %s", item.name, exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("补全 TMDB 失败 %s: %s", item.file_path, exc)
        result["ok"] = False
        result["error"] = str(exc)[:200]

    # 4. 图片预热（IO 阶段）：把这次要落的图先下到本地，写库阶段就只落字段
    #    （v2.42.9：以前 _enrich_apply → apply → _set_image → localize 在**写事务里**
    #    真的发 HTTP，一张图超时 15s 就把数据库写锁攥 15s，违反本模块自己的声明；
    #    现在写事务里的 localize 是 allow_download=False，预热没赶上的图只是「这轮没
    #    本地化」，交给取图时的按需自愈）。豆瓣/Bangumi 兜底图走 extra。
    from backend.emby_server.tmdb import prewarm_images, cast_list
    # v2.51.0 演员头像也在 IO 阶段预热：写事务里不碰网络（见模块注释处方 1）。
    # 口径与 _enrich_apply 写 emby_people 用的是同一个 cast_list，预热和落库对得上。
    cast_urls = [c["image"] for c in cast_list(result.get("tmdb_details"))
                 if c.get("image")]
    if not cast_urls:
        cast_urls = [c["image"] for c in _douban_cast_rows(result.get("douban_cast"))
                     if c.get("image")]
    result["images_prewarmed"] = prewarm_images(
        result.get("tmdb_hit"), result.get("tmdb_details"),
        extra=[(result.get(_k) or {}).get("image")
               for _k in ("douban_hit", "bangumi_hit")]
        + [((result.get("multisource") or {}).get("fields") or {}).get("poster")]
        + cast_urls)

    return result


def _enrich_fetch(item: Any, holder: Optional[dict] = None,
                  inherit_parent: Optional[dict] = None) -> dict:
    """IO 阶段（无 DB 写事务）：side 图片/字幕 → NFO → TMDB。单条路径（批量失败回退用）。

    行为与重构前逐行等价：pre → 单条 TMDB → post。
    """
    result, tmdb_ctx = _enrich_fetch_pre(item, holder, inherit_parent)
    if tmdb_ctx is None:
        return result
    return _enrich_fetch_tmdb_post(result, item, tmdb_ctx, batch_out=None)


def _douban_cast_rows(celebs: Optional[list]) -> list[dict]:
    """豆瓣演职员表 → 与 ``cast_list`` 同口径的行（name/role/image/sort_order）。

    预热与落库共用这一份，保证两边看到的演员表一致。
    """
    from backend.emby_server import douban as _dbn
    from backend.emby_server.tmdb import TMDB_CAST_LIMIT
    out: list[dict] = []
    for c in celebs or []:
        if len(out) >= TMDB_CAST_LIMIT:
            break
        if not isinstance(c, dict):
            continue
        name = _dbn.display_name(str(c.get("name") or ""))
        if not name:
            continue
        out.append({"name": name, "role": str(c.get("role") or ""),
                    "image": str(c.get("image") or ""),
                    "sort_order": len(out), "person_id": ""})
    return out


def _apply_douban_cast(db, item: Any, celebs: Optional[list]) -> None:
    """写库阶段：豆瓣兜底命中、TMDB 没给演员表时，用豆瓣演职员表写 ``emby_people``。

    幂等：条目已有演员行就不动（TMDB 写过的、或上一轮写过的）。失败只记日志。
    """
    rows = _douban_cast_rows(celebs)
    if not rows:
        return
    try:
        exists = db.query(em.EmbyPerson.id).filter(
            em.EmbyPerson.item_id == item.id).first()
        if exists:
            return
        for c in rows:
            db.add(em.EmbyPerson(
                item_id=item.id, name=c["name"][:200], role=c["role"][:200],
                image=c["image"][:1024], sort_order=c["sort_order"],
                person_tmdb_id=""))
        if any(not c["image"] for c in rows):
            try:
                from backend.emby_server import refresh_person_worker as _rpw
                _rpw.kick()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("豆瓣演员落库失败 %s: %s", getattr(item, "name", ""), exc)


def _apply_cast(db, item: Any, details: Optional[dict]) -> None:
    """写库阶段：把 details 里的演员表写入 ``emby_people``（幂等）。

    details 自带 credits（``append_to_response``，一次请求，不新增 TMDB 调用）。
    **只在该条目还没有演员行时写**（已有行只按名字补空头像/空 TMDB id）：补全重跑/
    修复重跑不会产生重复行；头像已在
    IO 阶段预热（``_enrich_fetch``），写事务里不碰网络。失败只记日志，
    不影响主流程（演员表是增强信息，不是核心元数据）。
    """
    if not details:
        return
    try:
        from backend.emby_server.tmdb import cast_list as _cast_list
        cast = _cast_list(details)
        if not cast:
            return
        existing = db.query(em.EmbyPerson).filter(
            em.EmbyPerson.item_id == item.id).all()
        missing_image = False
        if existing:
            # 已有演员行不重写（防重复），但把空着的头像 / TMDB id 按名字补上：
            # 以前演员表写一次就再也不更新，首刮时 TMDB 还没头像的演员永远是占位。
            by_name = {c["name"]: c for c in cast}
            for p in existing:
                c = by_name.get(p.name)
                if c and c.get("image") and not p.image:
                    p.image = str(c["image"])[:1024]
                if c and c.get("person_id") and not p.person_tmdb_id:
                    p.person_tmdb_id = str(c["person_id"])[:32]
                missing_image = missing_image or not p.image
        else:
            for c in cast:
                db.add(em.EmbyPerson(
                    item_id=item.id,
                    name=str(c["name"])[:200],
                    role=str(c.get("role") or "")[:200],
                    image=str(c.get("image") or "")[:1024],
                    sort_order=int(c.get("sort_order") or 0),
                    person_tmdb_id=str(c.get("person_id") or "")[:32],
                ))
                missing_image = missing_image or not c.get("image")
        if missing_image:
            # 有演员没头像：唤醒演员补全线程（TMDB 人物 / 豆瓣兜底），不在写事务里发请求
            try:
                from backend.emby_server import refresh_person_worker as _rpw
                _rpw.kick()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 — 演员落库失败不该影响主流程
        logger.debug("演员落库失败 %s: %s", getattr(item, "name", ""), exc)


def _parent_should_wait(db, item: Any) -> bool:
    """单集等待父级（v2.55.0）：episode 从不走 TMDB 搜索，只靠父级继承。

    父级还没 done（pending/enriching，如父级在退避期没被一起抢单）时返回 True：
    调用方把条目打回 pending 等待（此时标 'none' 是误杀——父级 done 后本集本可继承；
    生产实证：41,811 个标 'none' 的单集父级有 tmdb_id，本可继承却被标死）。
    父级终态（done 但无 tmdb_id/图、failed 等）时返回 False：调用方继续走 'none'。

    P2 修复（完美审查 C-P2-4）：等待计次 + 指数退避（300s→600s…上限 2h），
    防 300s ping-pong 无限循环（父级长期 pending 时每 300s 被抢单打回一次、
    "不计失败次数"→无限空转烧抢单与写库）。超 20 次返回 False，按父级终态
    失败处理（走 none，管理端"重试未匹配项"可捞回）。
    """
    parent_status = ""
    if item.series_id:
        try:
            _p = db.query(em.MediaItem).filter(
                em.MediaItem.id == item.series_id).first()
            parent_status = (_p.enrich_status or "") if _p is not None else ""
        except Exception:  # noqa: BLE001 — 父级查询失败不影响主流程
            parent_status = ""
    if parent_status not in ("pending", "enriching"):
        # 父级已终态（或查不到）：清等待计数，走正常流程
        _parent_wait_counts.pop(item.id, None)
        return False
    waits = _parent_wait_counts.get(item.id, 0) + 1
    if waits > _PARENT_WAIT_MAX_TIMES:
        _parent_wait_counts.pop(item.id, None)
        logger.warning("单集等待父级超限转 none id=%s 等待次数=%s",
                       item.id, waits)
        return False
    _parent_wait_counts[item.id] = waits
    if len(_parent_wait_counts) > 20000:
        _parent_wait_counts.clear()
    backoff = min(_PARENT_WAIT_BASE_SEC * (2 ** (waits - 1)),
                  _PARENT_WAIT_MAX_SEC)
    item.enrich_status = "pending"
    item.enrich_next_retry_at = datetime.now() + timedelta(seconds=backoff)
    item.enrich_claimed_at = None
    item.enrich_claim_token = None
    item.enrich_priority = 0
    return True


def _enrich_apply(db, item: Any, fetched: dict) -> None:
    """写库阶段（单条短事务内调用）：把 IO 阶段拿到的数据落库。"""
    from backend.emby_server import nfo as nfo_lib
    from backend.emby_server.tmdb import tmdb_client
    from sqlalchemy import func as _func

    # 联动快照（v2.58.0）：父级剧本轮是否新拿到 tmdb_id。放在所有落库之前，
    # 写库末尾用它判定要不要翻转名下空壳 inherit 单集。
    had_tmdb_id = bool(getattr(item, "tmdb_id", None))

    # 锁后认领：如果条目在抢单之后被管理员锁定，不再写入任何自动结果，
    # 直接 done 并释放租约（之前这里不检查，锁定形同虚设）。
    # 注意：手动操作（bind-tmdb / rescrape）不走这个函数，不受影响。
    if getattr(item, "metadata_locked", False):
        logger.info("补全跳过（抢单后被锁定）id=%s name=%r",
                    getattr(item, "id", "?"), getattr(item, "name", ""))
        item.enrich_status = "done"
        item.enrich_claimed_at = None
        item.enrich_claim_token = None
        _maybe_queue_probe(db, item)
        return

    item_type = item.item_type or ""
    kind = {"series": "series", "season": "season",
            "episode": "episode", "movie": "movie"}.get(item_type, "")

    # 1. side 图片（不覆盖已有）
    if fetched.get("poster") and not item.poster_path:
        item.poster_path = fetched["poster"]
    if fetched.get("fanart") and not item.backdrop_path:
        item.backdrop_path = fetched["fanart"]

    # 2. 外挂字幕 → MediaStream（总是刷新，与视频探测无关）
    external = fetched.get("external_subs") or []
    if external or True:  # 无字幕也要清理旧的字幕轨（文件删了字幕的场景）
        db.query(em.MediaStream).filter(
            em.MediaStream.item_id == item.id,
            em.MediaStream.is_external.is_(True),
        ).delete(synchronize_session=False)
        next_index = 0
        if external:
            next_index = db.query(_func.max(em.MediaStream.stream_index)).filter(
                em.MediaStream.item_id == item.id).scalar() or 0
        for offset, (lang, sub_path) in enumerate(external, start=1):
            stream = em.MediaStream(
                item_id=item.id, stream_index=next_index + offset,
                stream_type="Subtitle",
                codec=os.path.splitext(sub_path)[1].lstrip("."),
                language=lang, display_title=os.path.basename(sub_path),
                is_default=(offset == 1),
                is_external=True, external_path=sub_path,
            )
            # 字幕文件名长度不可控，写库前截断（双保险）
            em.sanitize_stream_strings(stream)
            db.add(stream)

    # 3. NFO + TMDB
    nfo_data = fetched.get("nfo_data")
    needs_repair = bool(getattr(item, "repair_requested_at", None))
    # 批3：单集分项开关（只读一次，供下面 NFO/TMDB 单集写入门控用；
    # 非 episode 条目不走分项门控，读了也不用）
    _ep_fields = _episode_field_config(db) if item_type == "episode" else None
    _ep_skip_overview = bool(
        _ep_fields is not None and not _ep_fields["overview"])
    if fetched.get("tmdb_id"):
        item.tmdb_id = item.tmdb_id or fetched["tmdb_id"]
        details = fetched.get("tmdb_details")
        if details:
            # 批2（刮削瘦身）：apply_details 门控收窄。apply_details 现在是
            # 「只填空不覆盖」，简介/类型不再是拉详情的理由——只留：
            # 修图请求、缺图、缺身份标识（imdb_id）。缺别名也不再是理由。
            _needs_details = (
                needs_repair
                or not (item.poster_path or item.primary_image_url)
                or not item.imdb_id
            )
            if not (nfo_data or {}).get("imdb_id") and _needs_details:
                tmdb_client.apply_details(item, details)
            if needs_repair or not (item.poster_path or item.primary_image_url):
                tmdb_client.apply_images(item, details,
                                         images_lang=fetched.get("tmdb_images_lang"))
        if nfo_data:
            nfo_lib.apply_nfo(item, nfo_data, kind,
                                          skip_overview=_ep_skip_overview)
    elif fetched.get("tmdb_hit"):
        tmdb_client.apply(item, fetched["tmdb_hit"], kind)
        details = fetched.get("tmdb_details")
        # 批2：与上面同口径（缺简介/缺类型不再是理由）
        _needs_details_b = (
            needs_repair
            or not (item.poster_path or item.primary_image_url)
            or not item.imdb_id
        )
        if details and _needs_details_b:
            tmdb_client.apply_details(item, details)
        if nfo_data:
            nfo_lib.apply_nfo(item, nfo_data, kind,
                                          skip_overview=_ep_skip_overview)
    elif nfo_data and not item.overview:
        nfo_lib.apply_nfo(item, nfo_data, kind,
                                          skip_overview=_ep_skip_overview)

    # 3.5 演员表（v2.51.0）：details 自带 credits；幂等，只在没有演员行时写。
    _apply_cast(db, item, fetched.get("tmdb_details"))
    if fetched.get("douban_cast"):
        db.flush()
        _apply_douban_cast(db, item, fetched.get("douban_cast"))
    # 元数据片长（只补空）：apply_details 被「已有 imdb+别名」短路时也要落
    try:
        from backend.emby_server.tmdb import apply_runtime as _apply_rt
        _apply_rt(item, fetched.get("tmdb_details"))
    except Exception:  # noqa: BLE001
        pass

    # 单集 TMDB 数据（v2.50.0）：标题/简介/剧照
    # 在父级图片回退之前应用——有专属剧照的集用自己的，没有的才回退
    # 批3：按分项开关裁剪（_ep_fields 在上面「3. NFO + TMDB」开头已读；默认全开 = 现状行为）
    episode_tmdb = fetched.get("episode_tmdb")
    if item_type == "episode" and episode_tmdb:
        from backend.emby_server.tmdb import TmdbClient
        TmdbClient.apply_episode(item, episode_tmdb, fields=_ep_fields)
        # 剧照：IO 阶段已预热，写库阶段只落字段（与海报同一口径）
        # 批3：剧照分项关时跳过（双保险：IO 阶段已不预热，这里也不落字段）
        still_url = fetched.get("episode_still_url") if _ep_fields["still"] else None
        if still_url and not (item.poster_path or item.primary_image_url):
            from backend.emby_server import image_store
            item.primary_image_url = still_url
            local = image_store.localize(still_url, allow_download=False)
            if local:
                item.poster_path = local

    # 集/季通常没有独立 TMDB 图片，但客户端会把它们作为独立卡片展示。
    # 物化一份父级图片到条目上，配合 API 的图片回退链，避免不同客户端只看
    # 自身 ImageTags 时出现灰色占位图。优先季，其次剧集；绝不覆盖集级专属图。
    if item_type in ("episode", "season") and not (
        item.poster_path or item.primary_image_url
    ):
        parents = []
        if item.parent_id:
            p = db.query(em.MediaItem).filter(em.MediaItem.id == item.parent_id).first()
            if p:
                parents.append(p)
        if item.series_id:
            s = db.query(em.MediaItem).filter(em.MediaItem.id == item.series_id).first()
            if s and all(s.id != p.id for p in parents):
                parents.append(s)
        for parent in parents:
            if parent.poster_path or parent.primary_image_url:
                item.poster_path = parent.poster_path
                item.primary_image_url = parent.primary_image_url
                break
    if item_type in ("episode", "season") and not (
        item.backdrop_path or item.backdrop_image_url
    ):
        parents = []
        if item.parent_id:
            p = db.query(em.MediaItem).filter(em.MediaItem.id == item.parent_id).first()
            if p:
                parents.append(p)
        if item.series_id:
            s = db.query(em.MediaItem).filter(em.MediaItem.id == item.series_id).first()
            if s and all(s.id != p.id for p in parents):
                parents.append(s)
        for parent in parents:
            if parent.backdrop_path or parent.backdrop_image_url:
                item.backdrop_path = parent.backdrop_path
                item.backdrop_image_url = parent.backdrop_image_url
                break

    if needs_repair:
        item.repair_requested_at = None

    # 判定「是否真的刮干净」，而不是「跑过就算完成」。
    # 旧实现无条件 done：TMDB 搜不到（网络抖动/限流/当次匹配失败）的条目被标记成
    # 补全完成，此后 _claim_batch 只捞 pending，永远不会再重试——于是
    # series 3263 条里 464 条永久缺 TMDB，且没有任何重试迹象。
    # 现在：核心元数据缺失就退回 pending（可重试），刮到了才 done。
    # 豆瓣兜底命中落库（v2.56.0 起：TMDB 为主、豆瓣兜底）：search 给标题/年份/海报，
    # details 给简介/评分。只补 TMDB 没给的缺项，不覆盖 NFO/TMDB 已有数据。
    _dbn_first = fetched.get("douban_fallback_hit")
    if _dbn_first:
        try:
            _dbn_details = fetched.get("douban_details") or {}
            if _dbn_first.get("title") and not (item.overview or "").strip() \
                    and not item.poster_path and not item.primary_image_url:
                from backend.emby_server.tmdb import clean_title as _ct
                _n = _ct(_dbn_first["title"])
                if _n:
                    item.name = _n
            if _dbn_first.get("year") and not item.production_year:
                try:
                    item.production_year = int(_dbn_first["year"])
                except (TypeError, ValueError):
                    pass
            if _dbn_details.get("overview") and not (item.overview or "").strip():
                item.overview = _dbn_details["overview"][:2000]
            if _dbn_details.get("runtime_ticks") and not (
                    getattr(item, "metadata_runtime_ticks", 0) or 0):
                item.metadata_runtime_ticks = int(_dbn_details["runtime_ticks"])
            if _dbn_details.get("rating"):
                try:
                    item.community_rating = float(_dbn_details["rating"])
                except (TypeError, ValueError):
                    pass
            if _dbn_first.get("image") and not item.poster_path \
                    and not item.primary_image_url:
                from backend.emby_server.tmdb import _set_image as _si
                _si(item, "Primary", _dbn_first["image"])
            if not item.last_scraped_at:
                item.last_scraped_at = datetime.now()
            item.metadata_source = "douban"
        except Exception as exc:
            logger.debug("豆瓣兜底落库失败 %s: %s", getattr(item, "name", ""), exc)
            _dbn_first = None

    # 豆瓣兜底结果落库（只在没有 TMDB 命中时）
    douban_hit = fetched.get("douban_hit")
    alt_hit = douban_hit
    alt_source = "douban"
    if not alt_hit:
        alt_hit = fetched.get("bangumi_hit")
        alt_source = "bangumi"
    if alt_hit:
        try:
            from backend.emby_server import altmeta as _alt
            _alt.apply(item, alt_hit, source=alt_source)
        except Exception as exc:  # noqa: BLE001 — 兜底落库失败不该影响主流程
            logger.debug("兜底落库失败 %s: %s", getattr(item, "name", ""), exc)
            alt_hit = None

    # Phase 6b：多源归并结果落库（与豆瓣兜底同一个位置——都只在 TMDB 没命中时发生）。
    # 只补缺项；用户主动「修复」时允许覆盖（fill_missing_only 跟 needs_repair 反向）。
    multi = fetched.get("multisource")
    multi_fields = (multi or {}).get("fields") or {}
    if multi_fields:
        try:
            from backend.emby_server.metasources import engine as _ms_engine
            _ms_engine.apply_to_item(
                item, _ms_engine.CollectResult.from_dict(multi),
                fill_missing_only=not needs_repair)
            if not item.last_scraped_at:
                item.last_scraped_at = datetime.now()
        except Exception as exc:  # noqa: BLE001 — 多源落库失败不该影响主流程
            logger.debug("多源落库失败 %s: %s", getattr(item, "name", ""), exc)
            multi_fields = {}

    _incomplete = False
    _searched_no_hit = False  # Step 4：TMDB 搜过无高置信命中（终态 none 口径）
    _fc_refill_enqueued = False  # Step 5：低优先级补次要字段已入队（跳过尾部判定）
    _douban_backfill_enqueued = False  # 豆瓣封禁补刮已入队（跳过尾部判定，同上）
    if fetched.get("pure_inherit"):
        # 纯继承（处方 3）：本集的数据来自父级剧（图片回退在上面已完成），
        # 不是「跑过没拿到」——单独标 inherit，避免落进无望队列的口径。
        # 之前失败过（none）的条目现在拿到数据了，也从 none 改标；
        # 真正的来源标记（nfo/tmdb/douban…）不动。
        if not item.metadata_source or item.metadata_source == "none":
            item.metadata_source = "inherit"
        # 竞态兜底（v2.58.0）：抓取阶段父级还没有 tmdb_id（_fetch_episode_tmdb 被跳过，
        # 见 episode_tmdb_skipped_no_parent_id），但写库时父级已经有了（父级在同批/
        # 并发中刚落 id）→ 打回 pending 短退避，下一轮纯继承会用新 id 拉单集数据。
        # 只处理「跳过」：抓取跑过但 TMDB 没这集是确定性结论，不重跑（否则死循环）。
        if fetched.get("episode_tmdb_skipped_no_parent_id") and item.series_id:
            _p = (db.query(em.MediaItem)
                  .filter(em.MediaItem.id == item.series_id)
                  .populate_existing().first())
            if _p is not None and _p.tmdb_id:
                logger.info("单集继承竞态：父级已补 tmdb_id，打回 pending 短退避 id=%s",
                            item.id)
                item.enrich_status = "pending"
                item.enrich_attempts = 0
                item.enrich_next_retry_at = datetime.now() + timedelta(seconds=300)
                item.enrich_claimed_at = None
                item.enrich_claim_token = None
                item.enrich_priority = 0
                _maybe_queue_probe(db, item)
                return
    elif kind in ("series", "movie"):
        if (tmdb_client.configured and not item.tmdb_id and not alt_hit
                and not multi_fields and not _dbn_first):
            # 处方 5（终态化）：search **真的跑过**且无高置信命中 =「搜过、没有」——
            # 这批条目在旧实现里走 5 次重试 × 每 60~960 秒重打 4~5 个候选搜索，
            # 结果必然是空：积压数字永不下降，白白烧掉约 7 万次 TMDB 请求。
            # 终态 done + metadata_source='none'（语义正好是「跑过但没拿到」），
            # 管理端「重试未匹配项」（retry_unmatched）随时可把它们捞回来。
            # 别误伤：ok=False（网络/限流失败）、repair 请求、NFO 带 tmdb_id、
            # 本次搜到了但写库没拿到 id——这四种都走老的重试路。
            _searched_no_hit = bool(
                fetched.get("ok")
                and not needs_repair
                and not fetched.get("tmdb_id")
                and not (nfo_data or {}).get("tmdb_id")
                and not fetched.get("tmdb_hit")
            )
            if _searched_no_hit:
                if (fetched.get("_ban_downgrade_skipped_douban")
                        and item.enrich_priority != ENRICH_PRIORITY_DOUBAN_BACKFILL
                        and _douban_backfill_candidate(item, kind)):
                    # 豆瓣封禁期被跳过、本轮没刮到 ≠「搜过没有」：这条只是
                    # 没轮到用豆瓣，不许判 none 终态（用户 2026-10-15 明令）。
                    # 进最低优先级安静补刮队列，解封后自动补跑一轮豆瓣；
                    # 不烧 attempts、无定时重试。本轮若已是补刮轮（priority
                    # 已是 -20）则不再进队列，往下走正常 none 终态——那一轮
                    # 豆瓣是可用的，真搜不到就是真没有。
                    _douban_backfill_enqueued = True
                    item.enrich_status = "pending"
                    item.enrich_priority = ENRICH_PRIORITY_DOUBAN_BACKFILL
                    item.enrich_attempts = 0
                    item.enrich_next_retry_at = None
                    logger.info("豆瓣封禁期跳过豆瓣未命中，进安静补刮队列"
                                "（解封后补）id=%s name=%r", item.id, item.name)
                else:
                    # 豆瓣/Bangumi 也没兜到（alt_hit 为空才会进到这里）：
                    # 显式记 none，与「从未标记过」区分开。
                    item.metadata_source = "none"
                    # 「跑过但没拿到」必须可见：以前这条是静默写入，
                    # 网络抖动被吞成搜不到时根本查不出（问题一的可见性缺口）
                    logger.info("TMDB 搜过无高置信命中（终态 none）id=%s name=%r year=%s",
                                item.id, item.name, item.production_year)
            else:
                _incomplete = True
        else:
            _searched_no_hit = False
        # ★ Step 5（链路规划）：字段级完成口径（feature flag，默认关）。
        # 关时下面整块不执行，行为与 Step 5 前逐行一致。
        # 开时：核心字段齐（tmdb_id，或标题/年份/海报三者有其二）但缺次要字段
        # （简介/类型/演员）的条目不直接 done，而是 pending + 低优先级
        # （enrich_priority=-10，_claim_batch 按 priority desc 认领，天然排在
        # 主队列之后；不另起认领通道，不饿死主队列）补一轮。
        # 反风控：补简介只多打 TMDB 详情（约 1 请求/条），每条最多一轮，增量可控。
        # P1-1(r32)：本轮抓取失败（_incomplete）时不进补队列——失败轮次进队列会
        # 重置 attempts=0 且尾部跳过失败计次，attempts 在 0/1 振荡永不到 failed
        # （v2.49.0"队列永远不降"同类 bug），持续烧 TMDB 配额；失败走正常退避
        # /failed 路径（与 flag 关时逐行一致）。
        if (not _searched_no_hit
                and not _incomplete
                and enrich_tuning_value(CFG_ENRICH_FIELD_COMPLETENESS, db)):
            _fc_missing = _field_completeness_missing(db, item)
            _field_completeness_counters["checked"] += 1
            if _fc_missing:
                # 缺失字段记录：逐字段记有无（日志 + 进程内计数，不加 migration；
                # 只读缺失率见 field_missing_stats() 的 SQL 口径）。
                logger.info("字段级口径：缺失字段=%s id=%s name=%r",
                            ",".join(_fc_missing), item.id, item.name)
            _fc_has_core = bool(item.tmdb_id) or sum(
                1 for f in ENRICH_COMPLETENESS_CORE_FIELDS
                if f not in _fc_missing) >= 2
            _fc_minor = [f for f in ENRICH_COMPLETENESS_MINOR_FIELDS
                         if f in _fc_missing]
            if _fc_has_core and _fc_minor:
                if item.enrich_priority == ENRICH_PRIORITY_FIELD_REFILL:
                    # ★ 防反复重刮（v2.49.0 教训）：这条已经为"补次要字段"多跑过
                    # 一轮（本轮就是那一轮：认领时 priority=-10 的就是它）——无论
                    # 这一轮拿到与否，直接按新口径终态化，不再为简介/类型/演员
                    # 反复进队列。每条目为"补次要字段"最多多跑一轮。
                    # 标记就是 priority 本身：_reclaim_stale 回收僵尸租约时不动
                    # priority，崩溃重认领回来仍然是 -10，照样终态化，不会多跑。
                    _field_completeness_counters["refill_terminalized"] += 1
                    logger.info("字段级口径：补次要字段已跑过一轮，直接终态化 "
                                "(仍缺=%s) id=%s", ",".join(_fc_minor), item.id)
                else:
                    item.enrich_status = "pending"
                    item.enrich_priority = ENRICH_PRIORITY_FIELD_REFILL
                    item.enrich_attempts = 0
                    item.enrich_next_retry_at = None
                    _fc_refill_enqueued = True
                    _field_completeness_counters["refill_enqueued"] += 1
                    logger.info("字段级口径：核心齐缺次要字段=%s，进低优先级补一轮 "
                                "id=%s name=%r", ",".join(_fc_minor), item.id,
                                item.name)
        if not _searched_no_hit and not (item.overview or "").strip():
            # 豆瓣 subject_suggest 不提供简介：补到标题/年份/海报就算完成，
            # 否则这批条目会永远停在 pending 反复重试。
            # （终态分支例外：TMDB 都搜过了还没有 id，overview 只能来自
            #   TMDB/兜底，重试也不会有——不再为它白付一轮退避。）
            #
            # v2.49.0 修正：原实现只问「兜底源有没有命中」，**从不看条目自己是否
            # 已经拿到核心字段**。生产实测（2026-10-05）：外语电影库里 571 条
            # 全部有标题 + 年份、104 条有 tmdb_id，却因为简介为空被判 _incomplete
            # → 退回 pending → worker 反复重刮，队列永远不降。
            # 口径改成「条目自身的核心字段齐了就完成」：标题 / 年份 / 海报三者有其二，
            # 或已有 tmdb_id，即视为刮干净。简介缺失只降级为可重试而非永久 pending。
            #
            # 注意**不能**把 alt_hit / multi_fields 算进来：它们只说明「这一轮抓到了
            # 什么」，不说明条目是否已经刮干净——把它们算进去会让只有标题、没有年份
            # 和海报的条目被判完成（测试 test_stays_pending_when_only_title_... 就是
            # 抓这个的），真正没刮干净的条目被静默放过。
            has_core = bool(item.tmdb_id) or sum(bool(x) for x in (
                (item.name or "").strip(),
                item.production_year is not None,
                bool(item.poster_path or item.primary_image_url),
            )) >= 2
            if not has_core:
                _incomplete = True
    # 单集等待父级（v2.55.0）：episode 从不走 TMDB 搜索，只靠父级继承。
    # 父级还没 done（pending/enriching，如父级在退避期没被一起抢单）时，
    # 此时标 'none' 是误杀——父级 done 后本集本可继承。退回 pending 等 5 分钟，
    # 不计失败次数。父级终态失败（done 但无 tmdb_id/图）时才走下面的 'none'。
    # 生产实证：41,811 个标 'none' 的单集父级有 tmdb_id，本可继承却被标死。
    if (item_type == "episode" and not fetched.get("pure_inherit")
            and not item.metadata_source):
        # P2（完美审查 C-P2-4）：父级未 done 时打回 pending 等待（_parent_should_wait
        # 内含等待计次 + 指数退避，防 300s ping-pong 无限循环）；超限或父级已终态
        # 时返回 False，继续往下走 'none' 终态。
        if _parent_should_wait(db, item):
            _maybe_queue_probe(db, item)
            return
    # 「跑过但没拿到数据」显式记为 none，和「从未标记过」(NULL) 区分开。
    # 这样一条 SQL 就能问出"到底哪些没刮干净"，不用再靠 last_scraped_at 反推。
    if (not _douban_backfill_enqueued and not item.metadata_source
            and kind in ("series", "movie", "season", "episode")):
        item.metadata_source = "none"
    if _fc_refill_enqueued or _douban_backfill_enqueued:
        # Step 5 / 豆瓣补刮：低优先级补一轮——保持上面设置的 pending +
        # priority（-10 / -20），跳过下面的 done/_incomplete 判定与优先级
        # 清零（优先级由补完那一轮的 _enrich_apply 消费并清零，见上面的
        # 防反复重刮注释）。
        pass
    elif _incomplete:
        # 资料不全：按失败计次 + 指数退避，超限转 failed。
        # 之前这里 attempts 清零且 next_retry_at 置空 → 下一轮抢单立刻重新领取 →
        # 无限空转：队列永远不降，还白烧 TMDB 配额。
        # Step 1：重试参数后台可配（60s 热生效）
        _tuning = _enrich_tuning_config(db)
        attempts = (item.enrich_attempts or 0) + 1
        item.enrich_attempts = attempts
        if attempts >= _tuning["values"][CFG_ENRICH_MAX_ATTEMPTS]:
            if _tuning["values"][CFG_ENRICH_NONE_AUTO_RESCHEDULE]:
                # Step 4（链路规划）：资料不全 = 数据性缺失（当前无解），转 done+none
                # 等自动回捞，不再进 failed（failed 只留给异常：网络/挂载/代码）。
                # 同一个 flag 包裹：关 flag 时行为与 Step 4 前完全一致（转 failed）。
                item.enrich_status = "done"
                item.metadata_source = "none"
                item.enrich_attempts = 0
                # 记本次终态化时间：回捞周期后 janitor 按最旧优先自动捞回
                item.enrich_next_retry_at = datetime.now()
                # 批2小修：none 诚实终态写 last_scraped_at——「跑过、搜过无命中」
                # 与「从未跑过」必须能一条 SQL 区分开（第 0 批假完成事故的教训）。
                # 回捞仍只看 enrich_next_retry_at，这里不破坏 janitor 语义。
                item.last_scraped_at = datetime.now()
                logger.warning("补全资料不全超限转 none（数据性缺失，等自动回捞）"
                               "id=%s attempts=%s", item.id, attempts)
            else:
                item.enrich_status = "failed"
                item.enrich_next_retry_at = None
                logger.warning("补全资料不全超限转 failed id=%s attempts=%s",
                               item.id, attempts)
        else:
            backoff = _tuning["values"][CFG_ENRICH_RETRY_BASE_SEC] * (2 ** (attempts - 1))
            item.enrich_status = "pending"
            item.enrich_next_retry_at = datetime.now() + timedelta(seconds=backoff)
            logger.info("补全资料不全待重试 id=%s attempts=%s %ss后",
                        item.id, attempts, backoff)
    else:
        item.enrich_status = "done"
        item.enrich_attempts = 0
        if _searched_no_hit and enrich_tuning_value(CFG_ENRICH_NONE_AUTO_RESCHEDULE, db):
            # Step 4：flag 开时，「搜过无命中」的终态 none 记本次终态化时间，
            # 回捞周期后 janitor 自动捞回；关时保持旧行为（next_retry_at=None）。
            item.enrich_next_retry_at = datetime.now()
        else:
            item.enrich_next_retry_at = None
        # 批2小修：none 诚实终态写 last_scraped_at（同上：与「从未跑过」区分，
        # 口径自明）。非 none 的 done 项不受影响（apply 已写过）。
        # getattr 兜底：调用方（含测试里的轻量替身）未必带这个字段。
        if item.metadata_source == "none" and not getattr(item, "last_scraped_at", None):
            item.last_scraped_at = datetime.now()
    item.enrich_claimed_at = None  # 处理完毕，释放 claim 租约（v2.42.9）
    item.enrich_claim_token = None
    # 优先级消费完归零（处方 4）：repair 的 100 在上面已随 repair_requested_at
    # 清除；重试未匹配的 50 也一样——残留值会让这个条目在未来的重试里永久插队。
    # Step 5 例外：低优先级补一轮的 -10 不能在这里清零——它是"已跑过一轮"的
    # 标记，留给补完那一轮的 _enrich_apply 识别并终态化（不清零就终态不了，
    # 会反复进低优先级队列）。
    if not _fc_refill_enqueued and not _douban_backfill_enqueued:
        item.enrich_priority = 0

    # 常态联动（v2.58.0）：series 本轮新拿到 tmdb_id（此前没有）→ 名下空壳
    # inherit 单集翻回 pending。只对 series：_fetch_episode_tmdb 只认 series 的
    # tmdb_id（季接口按 series id 拉），season 的 id 不驱动单集抓取。
    # 翻转放在 commit 之后做（调用方根据 fetched 里的标记执行），避免子集在本事务
    # 提交前后并发写入 done+inherit 的竞态（窗口从整个抓取阶段缩到一次查询）。
    if item_type == "series" and item.tmdb_id and not had_tmdb_id:
        fetched["_parent_tmdb_newly_acquired"] = item.id

    # probe 衔接：需要探测的送进 probe 队列（幂等）
    _maybe_queue_probe(db, item)


def _maybe_queue_probe(db, item: Any) -> None:
    """probe 衔接：需要探测的送进 probe 队列（幂等）。

    从 _enrich_apply 尾部提取：锁定时跳过元数据写入，但探测（技术信息，
    非元数据）仍要入队，所以两处都要调。
    """
    from backend.emby_server import scanner as _sc
    try:
        if _sc.needs_probe(item, item.file_path or "", item.size or 0):
            if getattr(item, "probe_status", None) not in ("pending", "probing"):
                item.probe_status = "pending"
                item.probe_priority = max(item.probe_priority or 0, 100)
                item.probe_attempts = 0
                item.probe_next_retry_at = None
    except Exception:  # noqa: BLE001
        pass


def _claim_group_atomic(db, q, now) -> list:
    """原子认领一组候选：单条 UPDATE ... WHERE enrich_status='pending'。

    背景：旧实现是 SELECT（PG 上 FOR UPDATE SKIP LOCKED）→ 内存改状态 →
    commit。SQLite 不支持 SKIP LOCKED（退化成普通查询），多 worker 并发时
    两个线程会选中同一批 pending 行 → 同一条目被处理 1.3~2.5 次。
    新实现把「选中→改状态」压成一条带 status='pending' 谓词的原子 UPDATE：
    并发下只有一个赢家。赢到的行用本批次 token 查回，保证处理的是自己
    赢到的行（候选顺序即 q 自带的 ORDER BY/LIMIT 顺序）。

    SQLite 上输家的 UPDATE 会撞 busy（忙等待后仍冲突）→ 返回 []，
    本轮跳过，下轮再抢；worker 循环会重试，不丢任务。
    """
    from sqlalchemy.exc import OperationalError
    cand_ids = [r[0] for r in q.with_entities(em.MediaItem.id).all()]
    if not cand_ids:
        return []
    token = uuid.uuid4().hex
    try:
        db.query(em.MediaItem).filter(
            em.MediaItem.id.in_(cand_ids),
            em.MediaItem.enrich_status == "pending",
        ).update(
            {"enrich_status": "enriching",
             "enrich_claimed_at": now,  # claim 租约：janitor 据此回收僵尸行
             "enrich_claim_token": token},
            synchronize_session=False)
        db.flush()
    except OperationalError:
        # 并发写冲突（主要在 SQLite 上）：本轮放弃，下轮再抢
        db.rollback()
        return []
    # bulk UPDATE 绕过了 identity map；逐出旧对象，保证查回的是赢到的行
    db.expire_all()
    rows_by_id = {r.id: r for r in db.query(em.MediaItem)
                  .filter(em.MediaItem.enrich_claim_token == token).all()}
    return [rows_by_id[i] for i in cand_ids if i in rows_by_id]


# ---------------------------------------------------------------------------
# 单集认领配额制（链路规划 Step 3）
#
# 夜间窗口从"禁令制"改"配额制"：
#   - feature flag enrich_episode_quota_mode（SystemConfig，默认 0=关）。
#     关时下面逻辑全部短路，行为与 Step 3 前逐字节一致。
#   - flag 开 + 夜间窗口内（或夜间开关根本没开）→ 单集不限量；
#   - flag 开 + 窗口外 → 当天已认领 episode 条数 < 有效配额时才认领单集；
#   - 豆瓣封禁中 → 有效配额按 0.5 系数减半（写死，UI 文字说明）；
#   - 配额设 0 → 窗口外永远跳过单集 = 回到禁令制（回滚口径）。
#
# 配额口径：enrich_claimed_at >= 今日 0 点（服务器本地时间）且
# item_type='episode' 的行数。_claim_batch 里一次 COUNT 查询，不给抢单加负担。
#
# 反风控：配额放开只影响"认不认领"，不改变抓取路径。单集主要走纯继承
# （不读 NFO、不查 TMDB：见 _enrich_fetch_pre 纯继承快路径 +
# _parent_should_wait 的注释 "episode 从不走 TMDB 搜索，只靠父级继承"；
# 同轮拉单集数据走 _fetch_episode_tmdb，按 key 独立桶限流（#794）），
# 从不走豆瓣 search（_douban_fallback_on_tmdb_miss 与
# _douban_ban_presearch_skip 都只认 kind in ("series", "movie")）。
# 所以放开配额不增加豆瓣 search 请求。
# ---------------------------------------------------------------------------
EPISODE_QUOTA_BAN_FACTOR = 0.5  # 封禁期配额减半（写死，UI 文字说明）


def _douban_currently_banned() -> bool:
    """豆瓣当前是否封禁中（封禁冷却剩余 > 0）。判定不了 → 保守按未封禁。"""
    try:
        from backend.emby_server import douban as _dbn
        return _dbn.ban_remaining() > 0
    except Exception:  # noqa: BLE001
        return False


def enrich_episode_quota_used_today(db) -> int:
    """当天已认领的 episode 条数（一次 COUNT，抢单外不产生额外负担）。"""
    from sqlalchemy import func as _func
    today0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return (db.query(_func.count(em.MediaItem.id))
            .filter(em.MediaItem.item_type == "episode",
                    em.MediaItem.enrich_claimed_at.isnot(None),
                    em.MediaItem.enrich_claimed_at >= today0)
            .scalar() or 0)


def enrich_episode_quota_status(db) -> dict:
    """配额状态（管理后台只读展示 + _claim_batch 用尽判定用）。"""
    mode = bool(enrich_tuning_value(CFG_ENRICH_EPISODE_QUOTA_MODE, db))
    quota = int(enrich_tuning_value(CFG_ENRICH_EPISODE_DAY_QUOTA, db))
    banned = _douban_currently_banned()
    effective = int(quota * EPISODE_QUOTA_BAN_FACTOR) if banned else quota
    night = _episode_night_config(db)
    used = enrich_episode_quota_used_today(db)
    night_enabled = bool(night["enabled"])
    in_window = bool(night["in_window"])
    return {
        "mode": mode,
        "day_quota": quota,
        "used_today": used,
        "banned": banned,
        "effective_quota": effective,
        "night_enabled": night_enabled,
        "in_window": in_window,
        # flag 关时=旧行为；夜间开关没开时=旧行为；窗口内不限；
        # 窗口外：用尽才拦（顶层条目不受配额影响，见 _claim_batch）。
        "episode_allowed_now": (not mode) or (not night_enabled)
        or in_window or used < effective,
    }


def _claim_batch(db, limit: int) -> list:
    """原子抢一批待补全条目（多 worker/多进程不重复）。

    认领是原子的（_claim_group_atomic）：单条
    UPDATE ... WHERE enrich_status='pending'，PG/SQLite 都不会重复认领。
    只抢「到重试时间」的（next_retry_at IS NULL 或已到期）。

    v2.42.9 第 5 批（处方 2）：**按父级成组**抢单。组 = ``coalesce(series_id, id)``
    ——剧条目与它名下所有集共享同一个组键，整组一次抢走，父级排在最前。
    于是同一部剧的条目落在同一个 worker、按季/集顺序连着处理：
    ``_ScanContext`` 的目录列举 / NFO 缓存才能在条目间复用（处方 1），
    父级先落 done、子集才能纯继承（处方 3）。

    v2.42.9 第 6 批（处方 4）：调度公平性 + 修复置顶——
    - ``enrich_priority desc``：repair（用户主动修复）置顶，重试未匹配次之；
    - **按库轮转**（``_library_turn``）：组仍按「最近入库」倒序，但跨库先绕圈，
      老分类不再被新入库条目饿死（``ENRICH_LIBRARY_FAIRNESS=0`` 关闭）。
    """
    from sqlalchemy import case as _case, func as _func, or_ as _or

    now = datetime.now()
    due = _or(em.MediaItem.enrich_next_retry_at.is_(None),
              em.MediaItem.enrich_next_retry_at <= now,
              # 处方 4：repair 是用户主动触发（图片修复排队），数量少、可见，
              # 不应陪 4 万条积压等退避——立即置顶抢走（priority=100）。
              em.MediaItem.repair_requested_at.isnot(None))
    # 元数据锁定（P3，Emby 式）：管理员手动锁定的条目，自动补全永远跳过。
    # isnot(True) 而不是 == False：容忍 NULL 旧行（migration 给 DEFAULT 0，
    # 但手写 SQL/旧版本 ORM 可能留 NULL），NULL 视为未锁定。
    unlocked = em.MediaItem.metadata_locked.isnot(True)
    # 单集夜间刮削：开关开且当前不在夜间窗口内 → 抢单时跳过单集（episode），
    # 白天优先刮顶层条目（movie/series/season）。夜间窗口内不限制。
    # 时间用服务器本地时间。默认关闭，不改变现有行为。
    # 链路规划 Step 3：配额制 flag（enrich_episode_quota_mode）开时，下面分支
    # 改为"窗口外按日配额限单集"，flag 关时走原禁令制口径。
    _night_cfg = _episode_night_config(db)
    if enrich_tuning_value(CFG_ENRICH_EPISODE_QUOTA_MODE, db):
        # 链路规划 Step 3：配额制。窗口语义从"白天禁令"改为
        # "白天单集日配额 X，夜间不限"：
        #   - 夜间开关没开 / 在窗口内 → 不限；
        #   - 窗口外 → 当天已认领单集数 < 有效配额时才认领（用尽后顶层条目不受影响）。
        #   配额设 0 = 用尽判定恒成立 = 回到禁令制（回滚口径）。
        _qs = enrich_episode_quota_status(db)
        _skip_episode = (_qs["night_enabled"] and not _qs["in_window"]
                         and _qs["used_today"] >= _qs["effective_quota"])
        logger.debug("单集配额制：窗口内=%s 已用=%s/有效配额=%s（封禁减半=%s）%s",
                     _qs["in_window"], _qs["used_today"],
                     _qs["effective_quota"], _qs["banned"],
                     "跳过单集" if _skip_episode else "可认领单集")
    else:
        # flag 关：行为与 Step 3 前逐字节一致（禁令制）
        _skip_episode = _night_cfg["enabled"] and not _night_cfg["in_window"]
        if _skip_episode:
            logger.debug("单集夜间刮削：当前 %s 不在夜间窗口 %s-%s，跳过单集",
                         datetime.now().strftime("%H:%M"),
                         _night_cfg["night_start"], _night_cfg["night_end"])
    not_episode = True if not _skip_episode else (em.MediaItem.item_type != "episode")
    # 豆瓣封禁补刮队列（priority=-20）：封禁没解除时不认领——认领了也用不上
    # 豆瓣，只会空转。解封后 ban_remaining()==0 自动放行，-20 排在队末安静
    # 轮到。判定不了（异常）时不拦，保守不饿死队列。
    _ban_backfill_hold = False
    try:
        from backend.emby_server import douban as _dbn_hold
        _ban_backfill_hold = _dbn_hold.ban_remaining() > 0
    except Exception:  # noqa: BLE001
        _ban_backfill_hold = False
    not_ban_backfill = True if not _ban_backfill_hold else (
        em.MediaItem.enrich_priority != ENRICH_PRIORITY_DOUBAN_BACKFILL)
    group_key = _func.coalesce(em.MediaItem.series_id, em.MediaItem.id)

    base_order = (
        # GROUP BY 查询里排序必须用聚合：组优先级 = 组内最大值（PG 硬要求）
        _func.max(em.MediaItem.enrich_priority).desc(),
        _func.max(em.MediaItem.date_added).desc(),
    )

    def _order_with_fairness(q):
        if ENRICH_LIBRARY_FAIRNESS:
            # 上一轮消费过的库沉到队尾（不是置顶）：这样轮转才是真正的「绕圈」——
            # 消费 A → 下一轮 B 先 → 消费 B → 再下一轮 A 先。若置顶，游标库会
            # 连续吃满批次，轮转退化为静态优先级。
            return (q.order_by(_case((em.MediaItem.library_id == _library_turn(), 1),
                                     else_=0),
                               *base_order))
        return q.order_by(*base_order)

    # 多版本（电影/剧集/单集）：被合并的版本不独立补全，元数据走主记录。
    # （probe/subtitle/thumbnail worker 已有同口径过滤。）
    not_merged = em.MediaItem.merged_into_id.is_(None)
    # 先看优先级最高 / 最近入库的若干个组：通常 1~3 个组就能填满一批；被别的 worker
    # 锁住（抢到 0 行）的组自动跳过，继续看下一个。GROUP BY 查询没法带
    # FOR UPDATE（PG 限制），所以是「选组」与「抢行」两步。
    groups_q = (db.query(group_key.label("g"),
                         _func.max(em.MediaItem.date_added).label("latest"),
                         em.MediaItem.library_id.label("lib"))
                .filter(em.MediaItem.enrich_status == "pending", due, unlocked,
                        not_episode, not_merged, not_ban_backfill)
                # library_id 必须显式入组（PG 要求）：组键是全局 id，
                # 同一组恒在同一库内，(group_key, library_id) 与 group_key 等价。
                .group_by(group_key, em.MediaItem.library_id))
    groups = _order_with_fairness(groups_q).limit(20).all()

    claimed: list = []
    for gid, _latest, _lib in groups:
        if len(claimed) >= limit:
            break
        q = (db.query(em.MediaItem)
             .filter(em.MediaItem.enrich_status == "pending", due, unlocked,
                     not_episode, not_merged, not_ban_backfill, group_key == gid)
             .order_by(
                 # 父级（剧/电影）先于子级：父级先落 done，子集才能纯继承
                 _case((em.MediaItem.item_type.in_(["series", "movie"]), 0), else_=1),
                 em.MediaItem.season_number,
                 em.MediaItem.episode_number,
                 em.MediaItem.date_added.desc())
             .limit(limit - len(claimed)))
        # 原子认领：赢到的行才进 batch（并发下同一行只被一个 worker 赢走）
        rows = _claim_group_atomic(db, q, now)
        claimed.extend(rows)
    if claimed:
        db.commit()
        if ENRICH_LIBRARY_FAIRNESS:
            # 这一轮实际抢到的库（按优先级最高的那条算）作为下一轮的起点
            _library_turn(claimed[0].library_id)
    else:
        # 即使只是 SELECT，SQLAlchemy/PG 也会打开事务；worker 接下来会睡眠，
        # 不 rollback 就会留下 idle in transaction，长期占着快照/连接。
        db.rollback()
    return claimed


_library_turn_state = {"lib_id": None}


def _library_turn(start_lib_id: Optional[int] = None) -> Optional[int]:
    """按库轮转的游标（处方 4，进程内即可：每个 worker 线程一个 db session，

    轮转偏移在进程内共享即可达成「老分类不被饿死」——各线程轮转相位不同反而
    让跨库覆盖更均匀）。无参调用：返回上一轮的起点（组排序用）；
    带参调用：记录「这一轮从哪个库抢的」。
    """
    if start_lib_id is not None:
        _library_turn_state["lib_id"] = start_lib_id
        return start_lib_id
    return _library_turn_state["lib_id"]


def _mark_failed(db, item_id: int, attempts: int, error: str,
                 claim_token: Optional[str] = None) -> str:
    """失败：attempts+1，指数退避，超限转 failed。单条短事务。

    返回 "failed" / "retry" / "skip"，供调用方计入完成速率（v2.42.9）。

    claim_token：本次认领的令牌（_claim_batch 写入）。传了就做归属校验：
    行已被 janitor 回收、别人接手后，本 worker 已是"僵尸"，返回 "skip"
    不覆盖新状态（否则会把别人做完的 done 翻成 failed）。为 None 时保持
    旧行为（向后兼容：现有测试/旧调用方）。
    """
    try:
        item = db.query(em.MediaItem).filter(
            em.MediaItem.id == item_id).first()
        if item is None:
            return "skip"
        if not _claim_still_ours(item, claim_token):
            # 僵尸 worker：认领已失效，行有了新主人/新状态，不许覆盖
            logger.debug("补全记失败跳过（认领已失效）id=%s", item_id)
            db.rollback()
            return "skip"
        attempts = (attempts or 0) + 1
        item.enrich_attempts = attempts
        item.enrich_claimed_at = None  # 释放 claim 租约（v2.42.9）
        # Step 1：重试参数后台可配（60s 热生效）
        _tuning = _enrich_tuning_config(db)
        item.enrich_claim_token = None
        if attempts >= _tuning["values"][CFG_ENRICH_MAX_ATTEMPTS]:
            item.enrich_status = "failed"
            item.enrich_next_retry_at = None
            logger.warning("补全重试超限转 failed id=%s attempts=%s err=%s",
                           item_id, attempts, error[:120])
            outcome = "failed"
        else:
            backoff = _tuning["values"][CFG_ENRICH_RETRY_BASE_SEC] * (2 ** (attempts - 1))
            item.enrich_status = "pending"
            item.enrich_next_retry_at = datetime.now() + timedelta(seconds=backoff)
            logger.info("补全失败待重试 id=%s attempts=%s %ss后 err=%s",
                        item_id, attempts, backoff, error[:120])
            outcome = "retry"
        db.commit()
        return outcome
    except Exception as exc:  # noqa: BLE001
        # R4-3：不许静默失败——记失败本身抛异常时也要留痕，否则调用方只看到
        # "skip"，生产日志无迹可查。
        logger.warning("补全记失败异常 id=%s: %s", item_id, exc)
        db.rollback()
        return "skip"


def _recover_crashed(db) -> int:
    """启动时把崩溃残留的 enriching 打回 pending（断点续补）

    启动时进程内没有任何任务在跑，可以整体回收；运行期的兜底是 janitor
    （见 _reclaim_stale）：按 claim 租约只回收停滞超时的行，不会误伤在跑的 worker。
    """
    n = (db.query(em.MediaItem)
         .filter(em.MediaItem.enrich_status == "enriching")
         .update({"enrich_status": "pending", "enrich_claimed_at": None,
                  "enrich_claim_token": None},
                 synchronize_session=False))
    db.commit()
    return n


def _reclaim_stale(db, lease_sec: Optional[int] = None) -> int:
    """janitor：把「enriching 停滞超过租约」的僵尸行打回 pending（运行期回收）

    场景：worker 线程在补全中途抛出了未预期异常而死亡 / 容器被 kill，行就永远
    停在 enriching，谁都不会再碰它（_claim_batch 只捞 pending）。旧实现只在
    start() 时整体回收一次，运行期没有兜底；现在按 claim 时间戳判定：

    - enrich_claimed_at 超出租约 → 打回 pending；
    - enrich_claimed_at 为 NULL（老库升级上来的历史行）→ 用 date_modified 近似
      判定：enriching 行在处理期间不写库，date_modified ≈ 被抢到的时刻。

    打回时不改 attempts / next_retry_at：这不是失败，是「没人认领了」——
    下一次抢单自然捞到（挂载熔断期间的条目已由长 next_retry_at 挡住）。
    """
    from sqlalchemy import and_ as _and, or_ as _or
    # Step 1：租约后台可配（60s 热生效）；显式传参时保持旧口径
    lease = enrich_tuning_value(CFG_ENRICH_CLAIM_LEASE_SEC, db) if lease_sec is None else max(1, int(lease_sec))
    cutoff = datetime.now() - timedelta(seconds=lease)
    n = (db.query(em.MediaItem)
         .filter(em.MediaItem.enrich_status == "enriching")
         .filter(_or(
             em.MediaItem.enrich_claimed_at <= cutoff,
             _and(em.MediaItem.enrich_claimed_at.is_(None),
                  em.MediaItem.date_modified <= cutoff),
         ))
         .update({"enrich_status": "pending", "enrich_claimed_at": None,
                  "enrich_claim_token": None},
                 synchronize_session=False))
    db.commit()
    return n


def _janitor_loop() -> None:
    """租约 janitor 线程：周期回收停滞的 enriching 行（v2.42.9）"""
    # Step 1：租约后台可配（60s 热生效），janitor 每轮重读
    logger.info("补全 janitor 启动（租约 %ss，每 %ss 一轮）",
                enrich_tuning_value(CFG_ENRICH_CLAIM_LEASE_SEC),
                ENRICH_JANITOR_INTERVAL_SEC)
    while not _stop_event.is_set():
        try:
            db = SessionLocal()
            try:
                n = _reclaim_stale(db)
                if n:
                    logger.info("补全 janitor 回收 %d 条超时租约（enriching → pending）", n)
                # Step 4：none 定时回捞（开关默认关；单轮硬上限 2000 条，
                # 与 _reclaim_stale 同一连接、同一轮顺带做，不额外抢 worker 的 DB）
                nr = enrich_none_auto_reschedule(db)
                if nr:
                    logger.info("补全 janitor 回捞 %d 条超期 none（done → pending）", nr)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001 — 一轮失败不该拖垮 janitor
            logger.warning("补全 janitor 执行失败: %s", exc)
        _stop_event.wait(ENRICH_JANITOR_INTERVAL_SEC)
    logger.info("补全 janitor 退出")


def retry_unmatched(db) -> int:
    """「重试未匹配项」（处方 5 的配套后台动作）：把终态无望条目捞回队列。

    终态化（_enrich_apply）把「TMDB 搜过、无高置信命中」的条目标 done +
    metadata_source='none'——它们不再吃重试预算。但这不是永久放弃：
    TMDB 每天都在新增条目、本地别名（Tier 1.5）也在变好，管理员在
    管理后台点一次「重试未匹配项」，这批条目就重新排队补搜一遍。

    只动 done + none + 无 tmdb_id 的 series/movie；置 enrich_priority=50
    （高于默认 0，低于 repair 100）让它们排在队首而不是慢慢等轮转。
    返回捞回的条数。
    """
    filters = (em.MediaItem.enrich_status == "done",
               em.MediaItem.metadata_source == "none",
               em.MediaItem.tmdb_id.is_(None),
               em.MediaItem.item_type.in_(["series", "movie"]),
               em.MediaItem.file_fingerprint.isnot(None))
    # 先把要捞回的名字/年份记下来（第 7 批）：这些片名的阴性搜索结果已落盘
    # 缓存，不删的话 worker 补搜时直接命中旧缓存、一个请求都不发——
    # 管理端的重试就成了摆设。
    affected = {(r[0], r[1], r[2]) for r in db.query(
        em.MediaItem.name, em.MediaItem.production_year, em.MediaItem.item_type
    ).filter(*filters).all()}
    n = (db.query(em.MediaItem).filter(*filters)
         .update({"enrich_status": "pending",
                  "enrich_attempts": 0,
                  "enrich_next_retry_at": None,
                  "enrich_priority": ENRICH_PRIORITY_RETRY_UNMATCHED},
                 synchronize_session=False))
    db.commit()
    if n:
        logger.info("重试未匹配项：%d 条无望条目重新入队（priority=50）", n)
        try:
            from backend.emby_server import tmdb_cache
            from backend.emby_server.tmdb import preferred_language
            lang = preferred_language()
            purged = 0
            for name, year, item_type in affected:
                purged += tmdb_cache.invalidate_search(name, year, item_type, lang)
            if purged:
                logger.info("重试未匹配项：已清掉 %d 份旧搜索缓存（下轮补搜真打 TMDB）", purged)
        except Exception as exc:  # noqa: BLE001 — 缓存清理失败不影响重试入队
            logger.debug("清理 TMDB 搜索缓存失败: %s", exc)
    # v2.55.x 翻转监控：这是管理端手动触发的 done→pending 批量操作，
    # 超阈值同样留告警 + PG 事件（和未知触发源的翻转区分开，trigger 不同）。
    if n:
        try:
            from backend.emby_server import enrich_flip_monitor as _fm
            _fm.alert_flip(
                db, flip_count=n, trigger="retry_unmatched",
                flips=[{"item_id": None, "item_type": "series/movie",
                        "reason": "retry_unmatched"}],
            )
        except Exception as exc:  # noqa: BLE001 — 监控不能影响主流程
            logger.warning("重试未匹配项翻转告警失败: %s", exc)
    return n


def _none_reschedulable_query(db, cutoff):
    """Step 4：超期 none 条目的查询（回捞动作与只读计数共用同一口径）。

    口径：enrich_status='done' + metadata_source='none' + 无 tmdb_id +
    剧/电影 + 有文件指纹 + （enrich_next_retry_at 为 NULL（旧存量/从未记录）或
    <= cutoff（上次回捞/终态化已超过周期））。
    """
    from sqlalchemy import or_ as _or
    return (db.query(em.MediaItem)
            .filter(em.MediaItem.enrich_status == "done",
                    em.MediaItem.metadata_source == "none",
                    em.MediaItem.tmdb_id.is_(None),
                    em.MediaItem.item_type.in_(["series", "movie"]),
                    em.MediaItem.file_fingerprint.isnot(None),
                    _or(em.MediaItem.enrich_next_retry_at.is_(None),
                        em.MediaItem.enrich_next_retry_at <= cutoff)))


def enrich_none_auto_reschedule(db) -> int:
    """Step 4 定时回捞：超周期的 done+none 条目翻回 pending（janitor 每轮顺带）。

    - 总开关：``enrich_none_auto_reschedule``（SystemConfig，默认 0=关）。
      关时直接返回 0，行为与 Step 4 前完全一致。
    - 超期口径：上次回捞（或首次终态化）时间超过
      ``enrich_none_reschedule_days``（默认 30 天）。「上次回捞时间」复用现有
      字段 ``enrich_next_retry_at`` 记录（不加 DB migration）：
      终态化记 now；翻回 pending 时记 now（认领不受阻，且下次超期前不再被捞）。
      NULL = 旧存量（Step 4 前终态化的行），视为已超期、本轮优先捞回。
    - 单轮硬上限 ``ENRICH_NONE_AUTO_RESCHEDULE_BATCH``（2000，写死）：按最旧
      优先（NULL 最旧，其次按时间戳），捞满即停，下一轮 janitor 继续。
    - 翻回动作复用 ``retry_unmatched`` 的逻辑：清 TMDB 搜索阴性缓存 +
      ``enrich_priority=50``。监控用 info 日志（例行调度，不走翻转告警，
      避免每轮 2000 条刷 PG 事件）。
    返回本轮实际翻回的条数。
    """
    if not enrich_tuning_value(CFG_ENRICH_NONE_AUTO_RESCHEDULE, db):
        return 0
    days = enrich_tuning_value(CFG_ENRICH_NONE_RESCHEDULE_DAYS, db)
    cutoff = datetime.now() - timedelta(days=days)
    batch = ENRICH_NONE_AUTO_RESCHEDULE_BATCH
    cands = (_none_reschedulable_query(db, cutoff)
             .with_entities(em.MediaItem.id, em.MediaItem.name,
                            em.MediaItem.production_year, em.MediaItem.item_type)
             .order_by(em.MediaItem.enrich_next_retry_at.asc().nullsfirst(),
                       em.MediaItem.id.asc())
             .limit(batch)
             .all())
    if not cands:
        return 0
    ids = [r[0] for r in cands]
    affected = {(r[1], r[2], r[3]) for r in cands}
    n = (db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids))
         .update({"enrich_status": "pending",
                  "enrich_attempts": 0,
                  "enrich_next_retry_at": datetime.now(),
                  "enrich_priority": ENRICH_PRIORITY_RETRY_UNMATCHED},
                 synchronize_session=False))
    db.commit()
    logger.info("none 定时回捞：%d 条超期无望条目翻回 pending（priority=50，周期 %d 天）",
                n, days)
    if n:
        try:
            from backend.emby_server import tmdb_cache
            from backend.emby_server.tmdb import preferred_language
            lang = preferred_language()
            purged = 0
            for name, year, item_type in affected:
                purged += tmdb_cache.invalidate_search(name, year, item_type, lang)
            if purged:
                logger.info("none 定时回捞：已清掉 %d 份旧搜索缓存（下轮补搜真打 TMDB）",
                            purged)
        except Exception as exc:  # noqa: BLE001 — 缓存清理失败不影响重试入队
            logger.debug("none 定时回捞清理 TMDB 搜索缓存失败: %s", exc)
    return n


def none_stock_stats(db) -> dict:
    """Step 4 只读：done + metadata_source='none' + 无 tmdb_id 的存量统计。

    - by_type：按 item_type 分组的全量 none 存量（所有类型，含单集/季）；
    - reschedulable_now：当前周期下 janitor 下一轮可捞回的条数
      （剧/电影 + 超期口径，见 _none_reschedulable_query）；
    - batch_limit：单轮硬上限（2000）。
    供管理后台「定时与刮削」只读展示 + 上线前存量计数（handoff SQL 的同口径实现）。
    """
    from sqlalchemy import func
    rows = (db.query(em.MediaItem.item_type, func.count(em.MediaItem.id))
            .filter(em.MediaItem.enrich_status == "done",
                    em.MediaItem.metadata_source == "none",
                    em.MediaItem.tmdb_id.is_(None))
            .group_by(em.MediaItem.item_type)
            .all())
    by_type = {t or "unknown": int(c) for t, c in rows}
    days = enrich_tuning_value(CFG_ENRICH_NONE_RESCHEDULE_DAYS, db)
    cutoff = datetime.now() - timedelta(days=days)
    reschedulable = _none_reschedulable_query(db, cutoff).count()
    return {
        "by_type": by_type,
        "total": sum(by_type.values()),
        "reschedulable_now": reschedulable,
        "cycle_days": days,
        "auto_reschedule_enabled": bool(
            enrich_tuning_value(CFG_ENRICH_NONE_AUTO_RESCHEDULE, db)),
        "batch_limit": ENRICH_NONE_AUTO_RESCHEDULE_BATCH,
    }


# Step 5（链路规划）：字段级完成口径的进程内计数。缺失字段"各记有无"走
# 日志 + 这里的进程内计数，**不加 DB migration**（简报口径）；跨进程一致的
# 只读缺失率走下面的 field_missing_stats()（SQL 口径）。
_field_completeness_counters = {
    "checked": 0,            # flag 开时走过字段级判定的条目数
    "refill_enqueued": 0,    # 进低优先级补一轮的条目数
    "refill_terminalized": 0,  # 补过一轮后直接终态化的条目数（不再反复）
}


def _field_completeness_missing(db, item) -> list:
    """Step 5：逐字段查缺失。返回缺失字段名列表（固定顺序：
    title/year/poster=核心，overview/genres/cast=次要）。

    演员口径：emby_people 有无该条目的行（v2.51.0 起 credits 落库即写行）。
    """
    missing = []
    if not (item.name or "").strip():
        missing.append("title")
    if item.production_year is None:
        missing.append("year")
    if not (item.poster_path or item.primary_image_url):
        missing.append("poster")
    if not (item.overview or "").strip():
        missing.append("overview")
    if not (item.genres or "").strip():
        missing.append("genres")
    has_cast = (db.query(em.EmbyPerson.id)
                .filter(em.EmbyPerson.item_id == item.id)
                .first() is not None)
    if not has_cast:
        missing.append("cast")
    return missing


def field_missing_stats(db) -> dict:
    """Step 5 只读：已刮完条目的各字段缺失率（管理后台「定时与刮削」只读展示）。

    口径（实现者定，跨 flag 开关稳定可比）：enrich_status='done' +
    metadata_source 非空且非 'none'（= 实际刮到过数据的条目，不含"搜过没有"）
    + series/movie。缺失率 = 缺该字段的条目数 / 口径内总数。
    只读，不改库。

    口径说明（r33）：刻意只含 series/movie，与 Step 5 低优先级补简介队列的
    覆盖范围对齐——季(season)/单集(episode)走单集刮削/父级继承另行处理，
    refill 不覆盖它们；把它们算进口径会让管理后台的灰度判断（缺失率）
    永远降不下来。底层行数据不删：SQL 只聚合，season/episode 的行还在库里。
    """
    from sqlalchemy import func as _safunc, or_ as _or, and_ as _and

    def _blank(col):
        return _or(col.is_(None), _safunc.trim(col) == "")

    base = (db.query(em.MediaItem)
            .filter(em.MediaItem.enrich_status == "done",
                    em.MediaItem.metadata_source.isnot(None),
                    em.MediaItem.metadata_source != "none",
                    em.MediaItem.item_type.in_(["series", "movie"])))
    total = base.count()
    missing = {
        "title": base.filter(_blank(em.MediaItem.name)).count() if total else 0,
        "year": (base.filter(em.MediaItem.production_year.is_(None)).count()
                 if total else 0),
        # P3(r32)：poster 缺失口径与内存判定 `_field_completeness_missing` 统一——
        # 空字符串也算缺失（此前只用 is_(None)，与 _blank 不一致）。
        "poster": (base.filter(_and(_blank(em.MediaItem.poster_path),
                                    _blank(em.MediaItem.primary_image_url))).count()
                   if total else 0),
        "overview": base.filter(_blank(em.MediaItem.overview)).count() if total else 0,
        "genres": base.filter(_blank(em.MediaItem.genres)).count() if total else 0,
    }
    if total:
        cast_missing = (base.filter(
            ~db.query(em.EmbyPerson.id)
            .filter(em.EmbyPerson.item_id == em.MediaItem.id)
            .exists()).count())
    else:
        cast_missing = 0
    missing["cast"] = cast_missing
    rates = {k: (round(v / total, 4) if total else 0.0)
             for k, v in missing.items()}
    return {
        "total": total,
        "missing": missing,
        "rates": rates,
        "field_completeness_enabled": bool(
            enrich_tuning_value(CFG_ENRICH_FIELD_COMPLETENESS, db)),
    }


def _episode_lacks_own_data(ep: Any, parent: Any) -> bool:
    """单集是否缺自己的单集级数据（v2.58.0 空壳 inherit 联动谓词）。

    只用现成语义，不新造口径：
    - 简介：纯继承不拷父级简介（写库只标 inherit）；inherit 行无自带 NFO，
      非空简介只能来自单集抓取 → 非空即本集数据。
    - 剧照：图片沿父级回退是「物化拷贝」到条目上；与父级当前值一致/为空 =
      没有专属剧照，值不同 = 自己的图（含 sidecar 本地图）。
    - 标题：不作为条件。apply_episode 的覆盖门控是 ^第\\d+集$，生产全库 0 行
      命中（扫描命名是「X 第 N 集 SxxEyy」），标题不是可区分信号；且各落库
      路径都是只补缺项，重跑已有标题的集只会补简介/剧照，不会破坏。
    """
    if (getattr(ep, "overview", None) or "").strip():
        return False
    ep_poster = getattr(ep, "poster_path", None) or ""
    ep_primary = getattr(ep, "primary_image_url", None) or ""
    p_poster = getattr(parent, "poster_path", None) or ""
    p_primary = getattr(parent, "primary_image_url", None) or ""
    if (ep_poster and ep_poster != p_poster) or (ep_primary and ep_primary != p_primary):
        return False
    return True


def _requeue_inherit_children_for_parent(db, parent_id: int) -> int:
    """父级剧新拿到 tmdb_id：名下空壳 inherit 单集翻回 pending（v2.58.0 常态联动）。

    只翻同时满足的：episode + done + metadata_source='inherit' + 未锁定 +
    未合并 + 缺单集级数据（_episode_lacks_own_data）。已有数据的集不动。
    翻转时清掉父级物化的图片拷贝（只清与父级当前值一致的），让重跑的单集剧照
    能落下来；没有专属剧照时写库阶段会重新沿父级回退，展示不断档。
    无循环：触发条件是父级 None→有 id 的单次跃迁；子集重跑不会让父级再次跃迁。
    """
    from sqlalchemy import or_ as _or
    parent = db.query(em.MediaItem).filter(em.MediaItem.id == parent_id).first()
    if parent is None or not getattr(parent, "tmdb_id", None):
        return 0
    p_poster = getattr(parent, "poster_path", None) or ""
    p_primary = getattr(parent, "primary_image_url", None) or ""
    cands = (db.query(em.MediaItem)
             .filter(em.MediaItem.item_type == "episode",
                     em.MediaItem.enrich_status == "done",
                     em.MediaItem.metadata_source == "inherit",
                     em.MediaItem.metadata_locked.isnot(True),
                     em.MediaItem.merged_into_id.is_(None),
                     _or(em.MediaItem.series_id == parent_id,
                         em.MediaItem.parent_id == parent_id))
             .all())
    flipped = 0
    for ep in cands:
        if not _episode_lacks_own_data(ep, parent):
            continue
        if (getattr(ep, "poster_path", None) or "") == p_poster:
            ep.poster_path = None
        if (getattr(ep, "primary_image_url", None) or "") == p_primary:
            ep.primary_image_url = None
        ep.enrich_status = "pending"
        ep.enrich_attempts = 0
        ep.enrich_next_retry_at = None
        ep.enrich_claimed_at = None
        ep.enrich_claim_token = None
        ep.enrich_priority = ENRICH_PRIORITY_RETRY_UNMATCHED
        flipped += 1
    if flipped:
        logger.info("空壳 inherit 联动：父级剧 id=%s 新拿 tmdb_id，%d 个子集翻回 pending",
                    parent_id, flipped)
        try:
            from backend.emby_server import enrich_flip_monitor as _fm
            _fm.alert_flip(db, flip_count=flipped, trigger="inherit_requeue",
                           flips=[{"item_id": None, "item_type": "episode",
                                   "reason": "inherit_requeue"}])
        except Exception as exc:  # noqa: BLE001 — 监控不能影响主流程
            logger.warning("空壳 inherit 联动翻转告警失败: %s", exc)
    return flipped


def requeue_stuck_inherit_children(db) -> int:
    """存量补排（管理端可触发）：所有已有 tmdb_id 的剧，名下空壳 inherit 单集翻回 pending。

    与常态联动同一谓词、同一动作；返回翻回条数。部署后跑一次即可；
    日常由 _enrich_apply 的常态联动覆盖，不需要定时跑。
    """
    total = 0
    parent_ids = [r[0] for r in db.query(em.MediaItem.id).filter(
        em.MediaItem.item_type == "series",
        em.MediaItem.tmdb_id.isnot(None),
        em.MediaItem.tmdb_id != "").all()]
    for pid in parent_ids:
        total += _requeue_inherit_children_for_parent(db, pid)
    db.commit()
    if total:
        logger.info("空壳 inherit 存量补排：共 %d 个子集翻回 pending", total)
    return total


def _maybe_flip_inherit_children(db, fetched: dict) -> None:
    """写库 commit 之后执行常态联动（v2.58.0）。

    _enrich_apply 在父级剧新拿 tmdb_id 时置 fetched["_parent_tmdb_newly_acquired"]；
    这里在父级事务已提交后翻转子集，保证翻转查询能看到所有已提交的子集行。
    自带异常兜底：翻转失败不影响父级已提交的结果（可管理端补跑）。
    """
    pid = (fetched or {}).get("_parent_tmdb_newly_acquired")
    if not pid:
        return
    try:
        _requeue_inherit_children_for_parent(db, pid)
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        logger.warning("空壳 inherit 联动翻转失败 parent_id=%s: %s", pid, exc)


def _alias_tmdb_id(name: str) -> Optional[str]:
    """零 API 本地别名匹配（处方 12）：在已入库条目的 aliases / 名字里找同一部剧。

    Tier 1.5 解决「TMDB 返回的 top10 里挑得出」；这一步解决「已命中的剧
    本来就在库里，换个文件名又搜一遍」。命中后走 details（1 次请求）
    替代 4~5 次候选搜索，且不写错名字（只补 id/图/别名）。
    索引是进程内 TTL 缓存（默认 5 分钟），不随积压增长（上限 5 万行）。
    """
    from backend.emby_server.tmdb import _norm_text as _norm
    key = _norm(name or "")
    if len(key) < 2:
        return None
    now = time.monotonic()
    if (_alias_index["rows"] is None or now - _alias_index["at"] > _ALIAS_INDEX_TTL):
        db = SessionLocal()
        try:
            rows = (db.query(em.MediaItem.tmdb_id, em.MediaItem.name,
                             em.MediaItem.aliases)
                    .filter(em.MediaItem.item_type.in_(["series", "movie"]),
                            em.MediaItem.tmdb_id.isnot(None))
                    .limit(_ALIAS_INDEX_MAX).all())
        except Exception:  # noqa: BLE001 — 索引失败不影响主流程
            rows = []
        finally:
            db.close()
        index: dict = {}
        for tmdb_id, mname, aliases in rows:
            for alias in [mname] + (str(aliases or "").split(",") if aliases else []):
                nk = _norm(alias or "")
                if nk and nk not in index:
                    index[nk] = str(tmdb_id)
        _alias_index["rows"] = index
        _alias_index["at"] = now
    return _alias_index["rows"].get(key)


def _suppress_flood(db) -> int:
    """防入队洪峰：分层扫描启用前的老数据（无 file_fingerprint）默认 enrich_status
    是 pending，会一次性全进队列。用启发式直接标 done：
    - 有 last_scraped_at（已被旧扫描器刮过）→ done
    - 有 tmdb_id → done
    其余老数据保留 pending（真需要补），但按 date_added 倒序慢慢消化。
    返回标为 done 的条数。
    """
    from sqlalchemy import or_ as _or
    n = (db.query(em.MediaItem)
         .filter(em.MediaItem.enrich_status == "pending")
         .filter(em.MediaItem.file_fingerprint.is_(None))
         .filter(_or(em.MediaItem.last_scraped_at.isnot(None),
                     em.MediaItem.tmdb_id.isnot(None)))
         .update({"enrich_status": "done"}, synchronize_session=False))
    db.commit()
    return n


def _mount_id_of(file_path: Optional[str]) -> Optional[int]:
    """``mount://3/Movies/a.mkv`` → ``3``；非挂载路径返回 None"""
    path = (file_path or "").strip()
    if not path.startswith("mount://"):
        return None
    head = path[len("mount://"):].partition("/")[0]
    return int(head) if head.isdigit() else None


def _requeue_mount_unavailable(db, item_id: int, mount_id: Optional[int] = None,
                                claim_token: Optional[str] = None) -> str:
    """存储读不动：打回 pending + 长 next_retry_at（**不是 failed**）。

    坏挂载上的条目重试只会重复失败：让它们吃长退避，把队列让给健康挂载；
    attempts 不涨（这不是条目本身的失败，是存储不可用），挂载恢复后自然重补。
    单条短事务。

    claim_token：同 _mark_failed，僵尸 worker 不许覆盖行的新状态。
    """
    try:
        from backend.emby_server import mounts as mount_lib
        retry_sec = mount_lib.MOUNT_UNAVAILABLE_RETRY_SEC
        item = db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()
        if item is None:
            return "skip"
        if not _claim_still_ours(item, claim_token):
            logger.debug("补全跳过（认领已失效）id=%s", item_id)
            db.rollback()
            return "skip"
        item.enrich_status = "pending"
        item.enrich_claimed_at = None
        item.enrich_claim_token = None
        item.enrich_next_retry_at = datetime.now() + timedelta(seconds=retry_sec)
        db.commit()
        logger.info("补全跳过（存储不可用）id=%s mount=%s %ss后重试",
                    item_id, mount_id, retry_sec)
        return "mount_unavailable"
    except Exception as exc:  # noqa: BLE001
        # R4-3：同上，不许静默失败。
        logger.warning("补全存储不可用重排异常 id=%s: %s", item_id, exc)
        db.rollback()
        return "skip"


def _requeue_douban_banned(db, item_id: int,
                           claim_token: Optional[str] = None) -> str:
    """豆瓣封禁冷却中：打回 pending，冷却结束后再补；attempts 不涨。单条短事务。

    反惊群（2026-10-13）：原来所有条目都是 ban_remaining()+5s 醒来，冷却一结束
    几千条同时抢单、瞬间再封。现在加 0~10 分钟随机散开，让重试错峰。

    claim_token：同 _mark_failed，僵尸 worker 不许覆盖行的新状态。
    """
    try:
        from backend.emby_server import douban as _dbn
        wait = max(60, int(_dbn.ban_remaining()) + random.uniform(5, 605))
        item = db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()
        if item is None:
            return "skip"
        if not _claim_still_ours(item, claim_token):
            logger.debug("补全延后跳过（认领已失效）id=%s", item_id)
            db.rollback()
            return "skip"
        item.enrich_status = "pending"
        item.enrich_claimed_at = None
        item.enrich_claim_token = None
        item.enrich_next_retry_at = datetime.now() + timedelta(seconds=wait)
        db.commit()
        logger.info("补全延后（豆瓣封禁冷却）id=%s %ss后重试", item_id, wait)
        return "retry"
    except Exception as exc:  # noqa: BLE001
        # R4-3：同上，不许静默失败。
        logger.warning("补全豆瓣封禁重排异常 id=%s: %s", item_id, exc)
        db.rollback()
        return "skip"


def _requeue_for_douban_ban(db, item_id: int,
                            claim_token: Optional[str] = None) -> str:
    """豆瓣封禁导致本轮没法用豆瓣时的统一打回口（_process_item / _process_batch
    的 douban_banned 汇点都走这里）。

    用户口径（2026-10-15）：豆瓣被封就跳过、等解封再补刮——不许再走
    _requeue_douban_banned 的 900 秒冷却热重试（封禁期反复抢单空转、还把
    队列顶部的名额占死，生产实测同一对条目反复 churn、42 条新条目两天没
    被认领）。降级开关开（默认开）时：打回 pending + 最低优先级 -20 安静
    补刮队列——attempts 不涨、无定时重试；_claim_batch 在封禁未解除时不
    认领 -20，解封后自动轮到、补跑一轮豆瓣。开关被管理员显式关掉（紧急
    回退）时：沿用旧的冷却延后，行为与旧版一致。

    claim_token：同 _requeue_douban_banned，僵尸 worker 不许覆盖行的新状态。
    返回值沿用进度计数口径："retry"=本轮未完成（条目已安静入队，不是失败）。
    """
    if not _douban_ban_downgrade_flag_on():
        return _requeue_douban_banned(db, item_id, claim_token=claim_token)
    try:
        item = db.query(em.MediaItem).filter(em.MediaItem.id == item_id).first()
        if item is None:
            return "skip"
        if not _claim_still_ours(item, claim_token):
            logger.debug("补全豆瓣补刮入队跳过（认领已失效）id=%s", item_id)
            db.rollback()
            return "skip"
        item.enrich_status = "pending"
        item.enrich_claimed_at = None
        item.enrich_claim_token = None
        item.enrich_priority = ENRICH_PRIORITY_DOUBAN_BACKFILL
        item.enrich_next_retry_at = None
        db.commit()
        logger.info("补全延后（豆瓣封禁，进安静补刮队列）id=%s", item_id)
        return "retry"
    except Exception as exc:  # noqa: BLE001
        logger.warning("补全豆瓣补刮入队异常 id=%s: %s", item_id, exc)
        db.rollback()
        return "skip"


def _inherit_parent_info(db, item: Any) -> Optional[dict]:
    """纯继承（处方 3）的前提：剧已 done 且有图/tmdb_id，本集无待修复标记。

    返回 None 表示不具备纯继承条件（走完整抓取）；否则返回剧的快照。
    只对 episode 判定：season 本来就不发网络（无文件、不搜 TMDB）。
    ``populate_existing`` 强制重读：父级可能在同批的前一条刚落 done，
    身份映射里的旧对象会骗过这个判定。
    """
    if (item.item_type or "") != "episode":
        return None
    if getattr(item, "repair_requested_at", None):
        return None
    if not item.series_id:
        return None
    series = (db.query(em.MediaItem)
              .filter(em.MediaItem.id == item.series_id)
              .populate_existing().first())
    if series is None:
        return None
    if (series.enrich_status or "") != "done":
        return None
    if not (series.poster_path or series.primary_image_url or series.tmdb_id):
        return None
    return {"series_id": series.id, "tmdb_id": series.tmdb_id}


def _process_item(db, item: Any, holder: Optional[dict] = None) -> str:
    """处理一条已抢到的条目：IO 阶段 → 写库阶段。

    返回 'done' / 'retry' / 'failed' / 'mount_unavailable' / 'skip'
    （'mount_unavailable' = 存储读不动，不算成功也不算失败，不进速率口径）。

    v2.42.9 从 _worker_loop 里提出来：一是要给**整条**计时并记结果（阶段计数 +
    完成速率都靠它），二是让循环回到「抢一批 → 逐条处理」两行。行为与提之前一致。
    """
    # _claim_batch 的 commit 会让 ORM 对象过期；一次性复制字段后，后续 IO 不再
    # 触碰 ORM 对象，避免属性访问重新开启一个事务。
    snapshot = item if isinstance(item, _EnrichItemSnapshot) else _snapshot_item(item)
    item_id = snapshot.id
    attempts = snapshot.enrich_attempts
    mount_id = _mount_id_of(snapshot.file_path)

    # 父级查询是 IO 前唯一允许的 DB 读取；查询后明确 rollback，切断事务。
    inherit_parent = _inherit_parent_info(db, snapshot)
    if db.in_transaction():
        db.rollback()

    # ---- IO 阶段：没有 ORM 对象，也没有数据库事务 ----
    try:
        fetched = _enrich_fetch(snapshot, holder=holder,
                                inherit_parent=inherit_parent)
    except Exception as exc:  # noqa: BLE001
        logger.warning("补全 IO 失败 id=%s: %s", item_id, exc)
        db.rollback()
        from backend.emby_server import mounts as mount_lib
        if isinstance(exc, mount_lib.MountError):
            return _requeue_mount_unavailable(
                db, item_id, mount_id, claim_token=snapshot.enrich_claim_token)
        return _mark_failed(db, item_id, attempts, str(exc),
                            claim_token=snapshot.enrich_claim_token)
    if not fetched.get("ok"):
        db.rollback()
        if fetched.get("douban_banned"):
            return _requeue_for_douban_ban(
                db, item_id, claim_token=snapshot.enrich_claim_token)
        return _mark_failed(db, item_id, attempts,
                            fetched.get("error") or "fetch failed",
                            claim_token=snapshot.enrich_claim_token)

    # ---- 写库阶段：重新加载 ORM，单条短事务 ----
    try:
        db.rollback()  # 防止 IO 前任何只读查询把事务带进写回阶段
        fresh = db.query(em.MediaItem).filter(
            em.MediaItem.id == item_id).first()
        if fresh is None:
            db.rollback()
            return "skip"
        if not _claim_still_ours(fresh, snapshot.enrich_claim_token):
            # R4-4：僵尸 worker 防护——慢 IO 期间认领已失效（janitor 回收/别人
            # 接手），迟到的写库不许覆盖行的新状态（曾把 done 翻成 failed）。
            # 行已是 pending，下轮抢单自然捞回。
            logger.info("补全写库跳过（认领已失效）id=%s", item_id)
            db.rollback()
            return "skip"
        _enrich_apply(db, fresh, fetched)
        _st = (fresh.enrich_status or "")
        outcome = "done" if _st == "done" else ("failed" if _st == "failed" else "retry")
        db.commit()
        _maybe_flip_inherit_children(db, fetched)
        return outcome
    except Exception as exc:  # noqa: BLE001
        logger.warning("补全写库失败 id=%s: %s", item_id, exc)
        db.rollback()
        return _mark_failed(db, item_id, attempts, str(exc),
                            claim_token=snapshot.enrich_claim_token)


def _process_batch(db, batch: list) -> None:
    """批量处理已抢到的条目：pre（逐条）→ TMDB 批量 → post+写库（逐条）。

    与逐条 `_process_item` 等价，只是把 TMDB 环节换成批量：
    1. 逐条跑 `_enrich_fetch_pre`（side/NFO），收集 tmdb_plan；
    2. `_run_tmdb_batch` 一次跑完所有 TMDB 请求；
    3. 逐条跑 `_enrich_fetch_tmdb_post` + 写库（与 `_process_item` 写库段一致）。

    批量级异常 → 回退到逐条 `_process_item`；单条 transient → 该条 ok=False 进重试。
    """
    from backend.emby_server import mounts as mount_lib

    # ---- Phase 1：逐条 pre ----
    # (item, snapshot, result, tmdb_ctx, holder)
    pre_results = []
    holder: Optional[dict] = None
    current_group = None
    for item in batch:
        if _stop_event.is_set():
            break
        gkey = item.series_id or item.id
        if gkey != current_group:
            holder = {}
            current_group = gkey
        snapshot = item if isinstance(item, _EnrichItemSnapshot) else _snapshot_item(item)
        inherit_parent = _inherit_parent_info(db, snapshot)
        if db.in_transaction():
            db.rollback()
        try:
            result, tmdb_ctx = _enrich_fetch_pre(
                snapshot, holder=holder, inherit_parent=inherit_parent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("批量 pre 失败 id=%s: %s", snapshot.id, exc)
            db.rollback()
            # pre 异常按单条失败处理，记一个失败的占位结果
            result = {"ok": False, "error": str(exc)[:200]}
            tmdb_ctx = None
            pre_failed_exc = exc
        else:
            pre_failed_exc = None
        pre_results.append((item, snapshot, result, tmdb_ctx, pre_failed_exc))

    # ---- Phase 2：TMDB 批量 ----
    indexed = []
    for i, (_, snapshot, result, tmdb_ctx, pre_exc) in enumerate(pre_results):
        if tmdb_ctx is None or pre_exc is not None:
            continue
        plan = tmdb_ctx.get("tmdb_plan") or {"op": "none"}
        if plan.get("op") != "none":
            indexed.append((i, plan))
    batch_out = {}
    if indexed:
        try:
            batch_out = _run_tmdb_batch(indexed)
        except Exception as exc:  # noqa: BLE001
            logger.warning("批量 TMDB 失败（%d 条），回退逐条: %s", len(indexed), exc)
            db.rollback()
            # 回退：整批走原逐条路径
            holder2: Optional[dict] = None
            current_group2 = None
            for item in batch:
                if _stop_event.is_set():
                    break
                gkey = item.series_id or item.id
                if gkey != current_group2:
                    holder2 = {}
                    current_group2 = gkey
                started = time.monotonic()
                outcome = _process_item(db, item, holder2)
                progress.note_stage(
                    "enrich_item", (time.monotonic() - started) * 1000.0)
                progress.note_completed(outcome)
            return

    # ---- Phase 3：逐条 post + 写库 ----
    for i, (item, snapshot, result, tmdb_ctx, pre_exc) in enumerate(pre_results):
        if _stop_event.is_set():
            break
        started = time.monotonic()
        item_id = snapshot.id
        attempts = snapshot.enrich_attempts
        mount_id = _mount_id_of(snapshot.file_path)
        try:
            if pre_exc is not None:
                raise pre_exc
            if tmdb_ctx is None:
                fetched = result
            else:
                out = batch_out.get(
                    i, {"hit": None, "details": None, "transient": False})
                fetched = _enrich_fetch_tmdb_post(
                    result, snapshot, tmdb_ctx, batch_out=out)
        except Exception as exc:  # noqa: BLE001
            logger.warning("批量补全 IO 失败 id=%s: %s", item_id, exc)
            db.rollback()
            if isinstance(exc, mount_lib.MountError):
                outcome = _requeue_mount_unavailable(
                    db, item_id, mount_id, claim_token=snapshot.enrich_claim_token)
            else:
                outcome = _mark_failed(db, item_id, attempts, str(exc),
                                       claim_token=snapshot.enrich_claim_token)
            progress.note_stage(
                "enrich_item", (time.monotonic() - started) * 1000.0)
            progress.note_completed(outcome)
            continue
        if not fetched.get("ok"):
            db.rollback()
            if fetched.get("douban_banned"):
                outcome = _requeue_for_douban_ban(
                    db, item_id, claim_token=snapshot.enrich_claim_token)
            else:
                outcome = _mark_failed(db, item_id, attempts,
                                      fetched.get("error") or "fetch failed",
                                      claim_token=snapshot.enrich_claim_token)
            progress.note_stage(
                "enrich_item", (time.monotonic() - started) * 1000.0)
            progress.note_completed(outcome)
            continue
        try:
            db.rollback()
            fresh = db.query(em.MediaItem).filter(
                em.MediaItem.id == item_id).first()
            if fresh is None:
                db.rollback()
                outcome = "skip"
            elif not _claim_still_ours(fresh, snapshot.enrich_claim_token):
                # R4-4：同上，僵尸 worker 迟到写库不许覆盖行的新状态。
                logger.info("批量补全写库跳过（认领已失效）id=%s", item_id)
                db.rollback()
                outcome = "skip"
            else:
                _enrich_apply(db, fresh, fetched)
                _st = (fresh.enrich_status or "")
                outcome = "done" if _st == "done" else ("failed" if _st == "failed" else "retry")
                db.commit()
                _maybe_flip_inherit_children(db, fetched)
        except Exception as exc:  # noqa: BLE001
            logger.warning("批量补全写库失败 id=%s: %s", item_id, exc)
            db.rollback()
            outcome = _mark_failed(db, item_id, attempts, str(exc),
                                   claim_token=snapshot.enrich_claim_token)
        progress.note_stage(
            "enrich_item", (time.monotonic() - started) * 1000.0)
        progress.note_completed(outcome)


def _worker_loop(worker_id: int) -> None:
    logger.info("补全 worker #%d 启动（L2/L3 后台补全）", worker_id)
    db = SessionLocal()
    # _claim_batch 提交后不要让 ORM 条目过期；否则下面访问 series_id / file_path
    # 会重新 SELECT，打开一个事务，再把它带进 FUSE/TMDB 慢 IO。
    db.expire_on_commit = False
    # v2.54.1 统一任务调度：函数内导入 task_scheduler（它内部全是延迟导入，
    # 无循环导入风险）。扫描（用户触发）优先于刮削（后台）——有可派发扫描
    # 在等时降并发让路（保留 ENRICH_PREEMPT_MIN_THREADS 个线程继续刮削，
    # 其余线程短睡后重试），而不是全停。
    from backend.emby_server import task_scheduler as _ts

    try:
        while not _stop_event.is_set():
            # SCAN_PREEMPT_ENRICH=0 可关闭，回退到旧行为（各自抢资源）。
            # acquire_enrich_permit 永不抛异常（内部 fail-safe）。
            _permit = _ts.acquire_enrich_permit()
            if _permit is None:
                _ts.note_enrich_yielded()
                # P2-2：等待加 0~2s 随机抖动，打散 32 线程的对齐惊群
                _stop_event.wait(_ts.preempt_wait_with_jitter())
                continue
            # v2.54.1：permit 必须在批次处理完后归还（try/finally 保证
            # 任何异常路径都不泄漏降并发名额，否则信号量被耗尽后
            # 所有线程永久让路）
            try:
                try:
                    # Step 1：批次大小 / TMDB 批量开关后台可配（60s 热生效）
                    _tuning = _enrich_tuning_config(db)
                    batch = _claim_batch(db, _tuning["values"][CFG_ENRICH_BATCH])
                except Exception as exc:  # noqa: BLE001
                    db.rollback()
                    logger.warning("补全 worker 取单失败: %s", exc)
                    _stop_event.wait(ENRICH_IDLE_POLL_SEC)
                    continue
                if not batch:
                    _stop_event.wait(ENRICH_IDLE_POLL_SEC)
                    continue
                # v2.42.9 第 5 批（处方 1+2）：批次按组（同一部剧）聚在一起返回，
                # 组内条目共享一份 _ScanContext——同一季的目录列举与 tvshow.nfo
                # 只付一次网络成本；组边界上丢弃重建，缓存不随积压增长。
                # v2.53.1 TMDB 批量：多条时走 _process_batch（pre 逐条 → TMDB 批量 →
                # post+写库逐条），批量失败自动回退逐条。
                if _tuning["values"][CFG_ENRICH_TMDB_BATCH] and len(batch) > 1:
                    _process_batch(db, batch)
                else:
                    holder: Optional[dict] = None
                    current_group = None
                    for item in batch:
                        if _stop_event.is_set():
                            break
                        gkey = item.series_id or item.id
                        if gkey != current_group:
                            holder = {}          # 新组：上一组的 ctx 缓存随之释放
                            current_group = gkey
                        # v2.42.9：单条计时 + 结果计数。这两项就是「单条平均耗时」与
                        # 「近 5 分钟 done/分钟」的来源 —— 后面几批优化（纯继承 / ctx 复用 /
                        # 终态化）到底有没有把积压量降下来，靠它们验收。
                        started = time.monotonic()
                        outcome = _process_item(db, item, holder)
                        progress.note_stage(
                            "enrich_item", (time.monotonic() - started) * 1000.0)
                        progress.note_completed(outcome)
                # 发现页实时动态流埋点：本轮批次确实补全条目时写一条（只加写入调用）
                _note_enrich_batch_feed(db, batch)
                # 批次之间释放 session 身份映射，避免长连接内存膨胀
                db.expire_all()
            finally:
                _ts.release_enrich_permit(_permit)

    finally:
        db.close()
    logger.info("补全 worker #%d 退出", worker_id)


def get_progress() -> dict:
    """进度快照：给管理后台接口用。"""
    db = SessionLocal()
    try:
        from sqlalchemy import func as _func
        q = db.query(em.MediaItem.enrich_status,
                     _func.count(em.MediaItem.id)).group_by(
                         em.MediaItem.enrich_status).all()
        by_status = {s or "unknown": c for s, c in q}
        retrying = db.query(_func.count(em.MediaItem.id)).filter(
            em.MediaItem.enrich_status == "pending",
            em.MediaItem.enrich_next_retry_at.isnot(None)).scalar() or 0
        pq = db.query(em.MediaItem.probe_status,
                      _func.count(em.MediaItem.id)).group_by(
                          em.MediaItem.probe_status).all()
        probe_by_status = {s or "unknown": c for s, c in pq}
        return {
            "enrich": {
                "pending": by_status.get("pending", 0),
                "enriching": by_status.get("enriching", 0),
                "done": by_status.get("done", 0),
                "failed": by_status.get("failed", 0),
                "retrying": retrying,
            },
            "probe": probe_by_status,
            # Step 1：线程数为 start() 时解析的实际值（后台可配，重启 worker 生效）
            "workers": _effective_workers if _effective_workers else ENRICH_WORKERS,
            "enabled": ENRICH_ENABLED,
            # 单集夜间刮削当前生效状态（管理后台展示用；读库失败时回退默认关）
            "episode_night": _episode_night_config(db),
            # v2.42.9 可观测性：状态计数回答「还有多少」，这两个回答
            # 「一分钟几条、每条卡在哪一段」。进程内计数，重启归零。
            "stages": progress.stage_stats(),
            "throughput": progress.throughput(),
            "mount_io": progress.remote_stats(),
            # Step 1：租约后台可配（60s 热生效）
            "claim_lease_sec": enrich_tuning_value(CFG_ENRICH_CLAIM_LEASE_SEC, db),
        }
    finally:
        db.close()


def start() -> None:
    """启动后台补全线程（幂等），线程数 = start() 时解析（env → 后台配置 → 默认 8）"""
    global _worker_threads, _effective_workers
    if not ENRICH_ENABLED:
        logger.info("补全 worker 已禁用（ENRICH_WORKER=0）")
        return
    with _worker_lock:
        _worker_threads = [t for t in _worker_threads if t.is_alive()]
        if _worker_threads:
            return
        # 启动前：恢复崩溃残留 + 防老数据洪峰（只做一次）
        db = SessionLocal()
        try:
            n = _recover_crashed(db)
            if n:
                logger.info("补全 worker 恢复 %d 条崩溃残留", n)
            m = _suppress_flood(db)
            if m:
                logger.info("补全 worker 防洪峰：%d 条老数据直接标 done", m)
            # Step 1：线程数进程启动时解析一次（env 优先，否则后台配置），
            # 运行时动态增减线程不做（不安全），改配置需重启 worker。
            _effective_workers = _resolve_enrich_workers(db)
        finally:
            db.close()
        _stop_event.clear()
        for i in range(_effective_workers):
            t = threading.Thread(
                target=_worker_loop, args=(i,),
                name=f"enrich-worker-{i}", daemon=True)
            t.start()
            _worker_threads.append(t)
        # 租约 janitor（v2.42.9）：运行期崩溃的 enriching 行由它周期回收，
        # 不再只能靠重启。与 worker 同一份幂等守卫（_worker_threads）。
        j = threading.Thread(target=_janitor_loop, name="enrich-janitor", daemon=True)
        j.start()
        _worker_threads.append(j)
        logger.info("补全 worker 启动 %d 个线程 + janitor", _effective_workers)


def stop() -> None:
    """停止后台补全线程"""
    global _worker_threads
    _stop_event.set()
    with _worker_lock:
        threads, _worker_threads = _worker_threads, []
    for t in threads:
        t.join(timeout=10)
