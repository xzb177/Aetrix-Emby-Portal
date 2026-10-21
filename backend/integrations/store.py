"""能力配置的批量读写

能力模块原先各自写了一个「按 key 查一次 SystemConfig」的小助手，读一次配置要 4~6 次往返
（人机验证挂件一次要问 5 个键：提供方 / 站点密钥 / 私钥 / 两个动作开关）。这里统一成
**一次 IN 查询取回一批**，语义与逐键查询完全一致：

- 行存在 → 用行里的值（**哪怕是空串**，空串是「用户清空过」，不能被默认值顶掉）；
- 行不存在 → 用默认值。

写回同样是批量：一次 IN 查询取回已有行，缺的再补 INSERT，避免每字段一次往返。

读分两档（配置热重载 · 方案 A）：

- ``read_value(s)``：直查 DB，永远最新。管理后台展示、daemon 轮询、写后回读走这档。
- ``get_value(s)``：进程内短 TTL 缓存（默认 60 秒），给高频读路径用
  （每次播放、每次登录日志、每次出站请求……）。管理后台改完，同一进程内
  「写时失效」立即生效；跨进程（API / worker / EA 是独立进程）靠 TTL 兜底，
  最多 60 秒生效。

``default`` 不进缓存：同一个 key 在不同调用点可能传不同的 default，
缓存里只记「行是否存在 + 行里的原文」，default 每次现算。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Iterable, Mapping, Optional

from sqlalchemy.orm import Session

from backend import models

DEFAULTS_SENTINEL = object()

# ---------------------------------------------------------------------------
# A 类开关摘面板（2026-10-21 施工，用户 2026-10-19「功能开关从简」原则）：
# 以下 46 个键管理后台不再提供开关（UI 与各面板写路径已同步摘除）。
# 不在本表、仍走「DB 行 > 代码缺省」的键：
# - 缺省=关、留在面板（服主 2026-10-21 口径）：签到 / 群发言积分 / 系统告警 /
#   访问守卫两项 / 后台登录人机验证 / 邮件 / 代理 / 媒体库默认范围；
#   备用元数据 altmeta_enabled 缺省关、向来无面板，只认数据库。
# - 缺省=开、服主 2026-10-21 13:53 要求留在面板（代码缺省保持 #1163 的开，
#   读口径回到摘面板前）：auto_scan_enabled / chase_new_enabled / douban_enabled /
#   promotion_reward_enabled / stream_accel_enabled / meta_sources_enabled /
#   meta_sources_tmdb_miss_fallback / enrich_episode_night_only /
#   enrich_unified_fallback / enrich_episode_quota_mode /
#   enrich_none_auto_reschedule / enrich_failed_auto_reschedule /
#   enrich_field_completeness。
# 本表取值口径：
#   env（键名大写；显式设置且非空即接管，含 "0"/"false"）
#   > SystemConfig 存量行（摘除前的生产现值，行不删、原样保留作次级来源）
#   > 各消费方的代码缺省（已逐键对齐生产现值，摘除后行为零变化）。
# env 名逐个写明：tests/test_perfection_r44_envdoc.py 的 INDIRECT_READS
# 以本表为动态读取证据（key.upper() 动态拼名，静态扫描抓不到）。
# ---------------------------------------------------------------------------
PANEL_REMOVED_SWITCHES: tuple[tuple[str, str], ...] = (
    ("ai_enabled", "AI_ENABLED"),
    ("backup_enabled", "BACKUP_ENABLED"),
    ("bot_cmd_bind", "BOT_CMD_BIND"),
    ("bot_cmd_checkin", "BOT_CMD_CHECKIN"),
    ("bot_cmd_points", "BOT_CMD_POINTS"),
    ("bot_cmd_redeem", "BOT_CMD_REDEEM"),
    ("bot_enabled", "BOT_ENABLED"),
    ("bot_redpacket_enabled", "BOT_REDPACKET_ENABLED"),
    ("captcha_protect_login", "CAPTCHA_PROTECT_LOGIN"),
    ("captcha_protect_register", "CAPTCHA_PROTECT_REGISTER"),
    ("coupon_enabled", "COUPON_ENABLED"),
    ("drive_direct_play_enabled", "DRIVE_DIRECT_PLAY_ENABLED"),
    ("enrich_douban_ban_downgrade", "ENRICH_DOUBAN_BAN_DOWNGRADE"),
    ("enrich_tmdb_batch", "ENRICH_TMDB_BATCH"),
    ("exchange_enabled", "EXCHANGE_ENABLED"),
    ("expiry_reminder_enabled", "EXPIRY_REMINDER_ENABLED"),
    ("hotlink_protection_enabled", "HOTLINK_PROTECTION_ENABLED"),
    ("invitation_enabled", "INVITATION_ENABLED"),
    ("items_dedup_cache_enabled", "ITEMS_DEDUP_CACHE_ENABLED"),
    ("items_list_cache_enabled", "ITEMS_LIST_CACHE_ENABLED"),
    ("lottery_auto_draw_enabled", "LOTTERY_AUTO_DRAW_ENABLED"),
    ("lottery_enabled", "LOTTERY_ENABLED"),
    ("lottery_notify_winners", "LOTTERY_NOTIFY_WINNERS"),
    ("media_seek_enabled", "MEDIA_SEEK_ENABLED"),
    ("media_seek_notify_voters", "MEDIA_SEEK_NOTIFY_VOTERS"),
    ("media_seek_vote_enabled", "MEDIA_SEEK_VOTE_ENABLED"),
    ("meta_source_enabled_anilist", "META_SOURCE_ENABLED_ANILIST"),
    ("meta_source_enabled_bangumi", "META_SOURCE_ENABLED_BANGUMI"),
    ("meta_source_enabled_douban", "META_SOURCE_ENABLED_DOUBAN"),
    ("meta_source_enabled_omdb", "META_SOURCE_ENABLED_OMDB"),
    ("meta_source_enabled_tmdb", "META_SOURCE_ENABLED_TMDB"),
    ("meta_source_enabled_tvdb", "META_SOURCE_ENABLED_TVDB"),
    ("meta_source_enabled_tvmaze", "META_SOURCE_ENABLED_TVMAZE"),
    ("meta_sources_prefer_chinese", "META_SOURCES_PREFER_CHINESE"),
    ("new_media_notify_enabled", "NEW_MEDIA_NOTIFY_ENABLED"),
    ("playback_transcode_enabled", "PLAYBACK_TRANSCODE_ENABLED"),
    ("points_audit_enabled", "POINTS_AUDIT_ENABLED"),
    ("points_transfer_enabled", "POINTS_TRANSFER_ENABLED"),
    ("redpacket_refund_enabled", "REDPACKET_REFUND_ENABLED"),
    ("register_ratelimit_enabled", "REGISTER_RATELIMIT_ENABLED"),
    ("series_merge_enabled", "SERIES_MERGE_ENABLED"),
    ("series_merge_loose_enabled", "SERIES_MERGE_LOOSE_ENABLED"),
    ("series_merge_require_year", "SERIES_MERGE_REQUIRE_YEAR"),
    ("teaser_enabled", "TEASER_ENABLED"),
    ("tg_bind_guide_enabled", "TG_BIND_GUIDE_ENABLED"),
    ("vitality_enabled", "VITALITY_ENABLED"),
)

#: 已摘面板开关的 DB 键集合（消费方判定用）
PANEL_REMOVED_SWITCH_KEYS = frozenset(k for k, _ in PANEL_REMOVED_SWITCHES)
_PANEL_REMOVED_ENV_NAMES = dict(PANEL_REMOVED_SWITCHES)


def switch_env_value(key: str) -> Optional[str]:
    """已摘面板开关的文件配置值：env 显式设置（非空）时返回原文，否则 None。

    非摘面板键一律返回 None（口径不变）。直接 ORM 读取的消费方（少数本地
    小助手）也统一走这里判定，保证全仓只有这一套 env 接管口径。
    """
    env_name = _PANEL_REMOVED_ENV_NAMES.get(key)
    if env_name is None:
        return None
    v = os.getenv(env_name)
    if v is None or v.strip() == "":
        return None
    return v


def _apply_switch_env(out: dict, key_list) -> None:
    """给一批读结果叠加 env 接管（摘面板开关专用，内部用）。"""
    for key in key_list:
        env_v = switch_env_value(key)
        if env_v is not None:
            out[key] = env_v



def read_values(
    db: Session,
    keys: Iterable[str],
    defaults: Optional[Mapping[str, Any]] = None,
) -> dict[str, str]:
    """一次查询取回一批配置键（缺失的用 defaults 里的值，没有就是空串）"""
    key_list = list(keys)
    if not key_list:
        return {}
    rows = (
        db.query(models.SystemConfig.key, models.SystemConfig.value)
        .filter(models.SystemConfig.key.in_(key_list))
        .all()
    )
    found = {key: ("" if value is None else str(value)) for key, value in rows}
    out: dict[str, str] = {}
    for key in key_list:
        if key in found:
            out[key] = found[key]
            continue
        default = (defaults or {}).get(key, DEFAULTS_SENTINEL)
        out[key] = "" if default is DEFAULTS_SENTINEL or default is None else str(default)
    _apply_switch_env(out, key_list)
    return out


def read_value(db: Session, key: str, default: str = "") -> str:
    """单键读取（内部仍走一次查询；只有确实只用一个键时才用它）"""
    return read_values(db, [key], {key: default})[key]


def write_values(db: Session, values: Mapping[str, str], descriptions: Optional[Mapping[str, str]] = None) -> int:
    """批量写入（一次查询已有行 + 缺失补插）；返回写入的键数

    只负责写，**不提交**——提交时机由调用方决定（能力保存要把所有字段与审计放在同一个事务里）。
    写完即调 ``invalidate()``：同一进程内保存即生效（读路径只认提交后的 DB 值；
    并发读恰好落在“失效→提交”窗口内时，最多按 TTL 陈旧，不会更久）。
    """
    pairs = {str(k): ("" if v is None else str(v)) for k, v in (values or {}).items()}
    if not pairs:
        return 0
    rows = (
        db.query(models.SystemConfig)
        .filter(models.SystemConfig.key.in_(list(pairs)))
        .all()
    )
    existing = {row.key: row for row in rows}
    for key, value in pairs.items():
        row = existing.get(key)
        if row is not None:
            row.value = value
            continue
        db.add(models.SystemConfig(key=key, value=value,
                                   description=(descriptions or {}).get(key) or None))
    invalidate(*pairs.keys())
    return len(pairs)


# ---------------- 短 TTL 热缓存（读多写少的配置走这里）----------------

#: 默认存活秒数：管理后台改完，最多这么久全局生效
TTL_DEFAULT = 60.0
#: 缓存条数上限（有界：配置键就几百个，正常到不了；到了就整清重建）
_TTL_CACHE_MAX = 2000
# key -> (写入时间戳, 行是否存在, 行值原文)
_ttl_cache: dict[str, tuple[float, bool, str]] = {}
_ttl_lock = threading.Lock()


def get_value(db: Session, key: str, default: str = "", *,
              ttl: float = TTL_DEFAULT) -> str:
    """热读单键：高频读取统一走这里（全项目只许这一套带缓存的读法）。

    ``ttl<=0`` 时退化成直查 DB。行不存在时返回 ``default``（default 不进缓存）。
    """
    return get_values(db, [key], {str(key): default}, ttl=ttl)[str(key)]


def get_values(db: Session, keys: Iterable[str],
               defaults: Optional[Mapping[str, Any]] = None, *,
               ttl: float = TTL_DEFAULT) -> dict[str, str]:
    """热读一批键：缓存命中的直接返回，未命中的一次 IN 查询补齐。

    语义与 ``read_values`` 一致：行存在用行里的值（哪怕空串），行不存在用 defaults。
    """
    key_list = [str(k) for k in keys]
    if not key_list:
        return {}
    now = time.monotonic()
    results: dict[str, tuple[bool, str]] = {}
    missing: list[str] = []
    with _ttl_lock:
        for key in key_list:
            hit = _ttl_cache.get(key)
            if hit is not None and now - hit[0] < ttl:
                results[key] = (hit[1], hit[2])
            elif key not in results:
                missing.append(key)
    if missing:
        rows = (
            db.query(models.SystemConfig.key, models.SystemConfig.value)
            .filter(models.SystemConfig.key.in_(missing))
            .all()
        )
        found_map = {k: ("" if v is None else str(v)) for k, v in rows}
        stamp = time.monotonic()
        with _ttl_lock:
            if len(_ttl_cache) >= _TTL_CACHE_MAX:
                _ttl_cache.clear()
            for key in missing:
                if key in found_map:
                    results[key] = (True, found_map[key])
                    _ttl_cache[key] = (stamp, True, found_map[key])
                else:
                    results[key] = (False, "")
                    _ttl_cache[key] = (stamp, False, "")
    out: dict[str, str] = {}
    for key in key_list:
        found, value = results[key]
        if found:
            out[key] = value
            continue
        default = (defaults or {}).get(key, DEFAULTS_SENTINEL)
        out[key] = "" if default is DEFAULTS_SENTINEL or default is None else str(default)
    _apply_switch_env(out, key_list)
    return out


def invalidate(*keys: str) -> int:
    """清热缓存：无参=全清；有参=只清指定键。返回清掉的条数。

    写路径（``write_values`` 与各模块的直接 upsert）写完即调：
    同一进程内「保存即生效」，跨进程靠 TTL 兜底。
    """
    with _ttl_lock:
        if not keys:
            n = len(_ttl_cache)
            _ttl_cache.clear()
            return n
        n = 0
        for key in keys:
            if _ttl_cache.pop(str(key), None) is not None:
                n += 1
        return n
