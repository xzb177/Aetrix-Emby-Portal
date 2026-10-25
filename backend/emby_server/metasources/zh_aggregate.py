"""官方代理式中文聚合通道（FMBY 高级刮削候选 2，第一批）。

FMBY 的中文命中率实际来自官方代理通道（经 fmby_proxy 取中文简介与
海报），不是 TMDB 直连；本模块是我方对应的聚合口：以补资料阶段的
源身份接入两段式第二阶段（identify_two_stage.apply_fill 在 Frodo
缺口之后问它），不改第一阶段识别逻辑、不定身份、不写库。

聚合策略（写清楚、不 hardcode 片单）：

- 逐字段取胜，不是整源通吃：每个字段（简介 / 海报 / 背景 / 评分）
  独立按源优先级取第一个非空值；某源只有简介没有海报时，海报位
  自动让给下一个源（与 metasources/engine 的“按顺序取字段”同口径）。
- 源顺序可配：``ZH_AGGREGATE_SOURCES`` 逗号名单，默认
  ``douban_frodo,tmdb_details``；未知源名只记日志跳过，不炸流程。
  名单里没有的源永远不问——部署者改名单即改策略，不改代码。
- 源失败隔离：任一源抛异常 / 返回空都只算该源没给，继续问下一个；
  聚合函数本身不抛异常（调用方是补资料段，源坏了不能拖垮写库）。
- 源复用，不重建：``douban_frodo`` 源直接复用 #1241 的 Frodo 桥
  （identify_two_stage._frodo_candidates，含熔断与封禁闸）；注意它
  不受独立 Frodo 开关（IDENTIFY_FRODO_FILL_ENABLED）管辖——聚合
  通道是自己的开关，开聚合即开它里面的 Frodo 腿，关独立开关不
  影响聚合；``tmdb_details`` 源复用补资料段已采集的 TMDB 详情
  （零新增请求，纯粹是“已采集证据在聚合顺序里再算一次”)。
- 语言 / 地区版本：聚合结果按（身份，语言，地区）走 version_cache
  的版本快照（开关独立）；语言 / 地区只从 env 或调用方参数来，
  本模块不写死任何具体语言或地区。
"""

from __future__ import annotations

import logging
import os

from backend.emby_server.env_util import env_int
from backend.emby_server.metasources import version_cache as vc

logger = logging.getLogger(__name__)

#: 聚合通道总开关（默认关）。关时 aggregate 直接返回空，不问任何源。
ZH_AGGREGATE_ENABLED = env_int("ZH_AGGREGATE_ENABLED", 0, 0, 1)

#: 聚合可填的资料字段（与两段式 FILL_FIELDS 同口径：只填这四项）。
AGGREGATE_FIELDS = ("overview", "poster_path", "backdrop_path", "community_rating")

#: 已知源名（名单外的名字一律忽略，不许靠拼写错误悄悄问到别的源）。
KNOWN_SOURCES = ("douban_frodo", "tmdb_details")
#: 默认源顺序：中文简介 / 评分 / 海报先问豆瓣 Frodo，TMDB 已采集详情垫后。
_DEFAULT_SOURCES = "douban_frodo,tmdb_details"

#: 聚合结果的进程内版本缓存（version_cache 的默认实例；测试可替换）。
_CACHE = vc.DEFAULT_CACHE


def aggregate_enabled() -> bool:
    """聚合通道是否开启（全仓读开关的唯一入口；测试 patch 本模块常量）。"""
    return ZH_AGGREGATE_ENABLED > 0


def _cfg_sources() -> list:
    """源优先级名单：env 逗号名单，去重保序、只留已知源；空名单回默认。"""
    raw = os.getenv("ZH_AGGREGATE_SOURCES", "")
    wanted = [s.strip() for s in (raw or "").split(",") if s.strip()]
    if not wanted:
        wanted = [s.strip() for s in _DEFAULT_SOURCES.split(",")]
    ordered, seen = [], set()
    for name in wanted:
        if name not in KNOWN_SOURCES:
            logger.warning("中文聚合忽略未知源 %r（已知源：%s）", name, KNOWN_SOURCES)
            continue
        if name not in seen:
            seen.add(name)
            ordered.append(name)
    return ordered or list(KNOWN_SOURCES)


def _cfg_language() -> str:
    return os.getenv("ZH_AGGREGATE_LANGUAGE", "").strip()


def _cfg_region() -> str:
    return os.getenv("ZH_AGGREGATE_REGION", "").strip()


def _empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _identity_of(item):
    """聚合缓存的身份：tmdb_id 优先，次 imdb_id；都没有则不缓存。"""
    ident = getattr(item, "tmdb_id", None) or getattr(item, "imdb_id", None)
    return str(ident).strip() if ident else None

def _source_douban_frodo(item, fetched: dict) -> dict:
    """豆瓣 Frodo 源：复用 #1241 的无开关内层（熔断 / 封禁闸在内）。

    返回已是补资料字段口径（overview / community_rating /
    poster_path）；Frodo 不给背景图，backdrop 位自然空着让给下一源。
    """
    from backend.emby_server import identify_two_stage as ts

    return ts._frodo_candidates(item) or {}


def _source_tmdb_details(item, fetched: dict) -> dict:
    """TMDB 已采集详情源：只读补资料段已采集的 tmdb_details，不发请求。"""
    details = (fetched or {}).get("tmdb_details") or {}
    mapped = {
        "overview": details.get("overview") or "",
        "poster_path": details.get("poster_path") or "",
        "backdrop_path": details.get("backdrop_path") or "",
        "community_rating": details.get("vote_average"),
    }
    return {k: v for k, v in mapped.items() if not _empty(v)}


def _run_source(name: str, item, fetched: dict) -> dict:
    """跑一个源（失败隔离：异常 / 空都只算该源没给，不中断聚合）。"""
    try:
        if name == "douban_frodo":
            return _source_douban_frodo(item, fetched) or {}
        if name == "tmdb_details":
            return _source_tmdb_details(item, fetched) or {}
        return {}
    except Exception as exc:  # noqa: BLE001 — 源坏了不能拖垮补资料段
        logger.warning("中文聚合源 %s 失败，跳过：%s", name, exc)
        return {}


def aggregate(item, fetched: dict, needed_fields=None, *,
              language=None, region=None):
    """中文聚合：返回 ``(候选字段 dict, 字段→源 provenance dict)``。

    只填 ``needed_fields``（默认全部聚合字段）里仍缺的项；开关关时
    返回空，不问任何源、不碰缓存。版本缓存开且身份齐全时：缓存快照
    先当种子（已覆盖的字段不再问源），缺的字段继续跑源、跑完合并
    写回同一版本快照——一次窄 needed 不会把快照越写越窄。
    """
    if not aggregate_enabled():
        return {}, {}
    needed = [f for f in (needed_fields or AGGREGATE_FIELDS)
              if f in AGGREGATE_FIELDS]
    if not needed:
        return {}, {}
    lang = language if language is not None else _cfg_language()
    reg = region if region is not None else _cfg_region()
    ident = _identity_of(item)
    use_cache = bool(ident) and vc.version_cache_enabled()
    candidates, provenance = {}, {}
    if use_cache:
        cached = _CACHE.get(ident, lang, reg)
        if isinstance(cached, dict):
            # 缓存快照可能只含上次 needed 的子集：先当种子用，缺的字段
            # 继续跑源、跑完合并写回，不让一次窄 needed 把快照越写越窄。
            for f, v in (cached.get("candidates") or {}).items():
                if f in needed and not _empty(v):
                    candidates[f] = v
            for f, s in (cached.get("provenance") or {}).items():
                if f in candidates:
                    provenance[f] = s
            if len(candidates) == len(needed):
                return candidates, provenance
    for name in _cfg_sources():
        given = _run_source(name, item, fetched or {})
        for field in needed:
            if field not in candidates and not _empty(given.get(field)):
                candidates[field] = given[field]
                provenance[field] = name
        if len(candidates) == len(needed):
            break
    if use_cache and candidates:
        _CACHE.put(ident, lang, reg,
                   {"candidates": dict(candidates),
                    "provenance": dict(provenance)})
    return candidates, provenance


__all__ = [
    "ZH_AGGREGATE_ENABLED",
    "AGGREGATE_FIELDS",
    "KNOWN_SOURCES",
    "aggregate",
    "aggregate_enabled",
]
