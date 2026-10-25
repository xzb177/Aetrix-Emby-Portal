"""识别 / 补资料两段式 + 豆瓣 Frodo 补资料源（清单第 13 项，FMBY #17 / #14）。

对应魂 1（身份与证据先行）：刮削的写库拆成两段——

- 识别段：只定身份绑定（provider / 实体 / 置信度 / 证据），写身份列
  （tmdb_id / imdb_id）与一段识别记录，不写任何资料字段；
- 补资料段：只在已绑定条目上跑，只填空缺、永不覆盖已有资料
  （2026-10-19 定版口径：只填空缺永不覆盖）。

两段独立开关、独立重试：识别段提交后即落库，补资料失败不回滚身份；
识别未定的条目不进补资料段（由现行写库路径接管审查 / none 终态）。

与现行 enrich_worker 的分工：IO 阶段（_enrich_fetch）仍是证据采集的
共享步骤；写库阶段在任一段开关开时走两段提交，开关全关时与现行逐字
一致。本模块不碰扫描器、播放变体、身份锚（source_id / provider_file_id
是另一路的地盘）、绑定管理、列表接口、凭据密封。

Frodo 在本模块里的位置：补资料段的缺口来源之一（不是识别段的认亲
依据——认亲只认 TMDB / NFO / 别名给出的身份，Frodo 片名搜不用来定
身份）。Frodo 腿复用 #1203 熔断注册表（douban_frodo 源名，403 / 429
立即熔断、冷却约 60 秒半开）；熔断中、封禁中、任何异常都只返回空
——不挡其他源、不静默出旧数、不抛异常进补资料流程。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from backend.emby_server import models as em
from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

#: 识别段总开关（默认关）。启动时读一次，热路径不碰 env。
IDENTIFY_STAGE_ENABLED = env_int("IDENTIFY_STAGE_ENABLED", 0, 0, 1)
#: 补资料段总开关（默认关）。与识别段独立：可只开识别攒绑定，也可只开
#: 补资料给历史已绑定条目填空。
FILL_STAGE_ENABLED = env_int("FILL_STAGE_ENABLED", 0, 0, 1)
#: Frodo 补资料源开关（默认关）。开时补资料段才向 Frodo 问缺口字段。
IDENTIFY_FRODO_FILL_ENABLED = env_int("IDENTIFY_FRODO_FILL_ENABLED", 0, 0, 1)
#: 已识别未填空条目回 pending 后的重认领延迟（秒）：识别段提交后若补资料
#: 段关着，本条回队等补资料段认领；延迟避免识别→回队→识别空转。
IDENTIFY_UNFILLED_RETRY_SEC = env_int("IDENTIFY_UNFILLED_RETRY_SEC", 300, 0, 86400)

#: 补资料段允许写的资料字段（白名单制：不在表里的字段一律不写）。
#: overview / poster_path / backdrop_path / community_rating 四项而已——
#: 演员、季海报、字幕、NFO 等仍归现行写库路径，本段不越权。
FILL_FIELDS = ("overview", "poster_path", "backdrop_path", "community_rating")

#: Frodo 补资料时问的缺口字段（overview / 评分 / 海报三项；backdrop 不问
#: Frodo——它的 image 口径是海报，不是背景图）。
FRODO_FILL_FIELDS = ("overview", "community_rating", "poster_path")


def identify_enabled() -> bool:
    """识别段是否开启（全仓读开关的唯一入口；测试 patch 本模块常量）。"""
    return IDENTIFY_STAGE_ENABLED > 0


def fill_enabled() -> bool:
    """补资料段是否开启（全仓读开关的唯一入口；测试 patch 本模块常量）。"""
    return FILL_STAGE_ENABLED > 0


def frodo_fill_enabled() -> bool:
    """Frodo 补资料源是否开启。"""
    return IDENTIFY_FRODO_FILL_ENABLED > 0


def any_enabled() -> bool:
    """任一段开着即走两段写库；全关 = 现行写库逐字一致。"""
    return identify_enabled() or fill_enabled()


def unfilled_retry_sec() -> int:
    """已识别未填空条目的回队延迟（秒）。"""
    return IDENTIFY_UNFILLED_RETRY_SEC


def _empty(value) -> bool:
    """资料字段的“空”判定：None 或空白字符串都算空（评分只认 None）。"""
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _item_year(item) -> Optional[int]:
    """条目年份（Frodo 按标题搜用；取不到就 None，不编年份）。"""
    year = getattr(item, "production_year", None)
    if isinstance(year, int) and year > 0:
        return year
    return None


def extract_binding(item, fetched: dict) -> Optional[dict]:
    """从已采集证据里抽身份绑定（纯函数，不写库）。

    绑定来源与现行写库同口径：tmdb_id 取 fetched（NFO / 别名 / 搜索命中
    都已在 IO 阶段落进 fetched["tmdb_id"]）或条目已有值；imdb_id 取
    TMDB 详情、NFO 或条目已有值。身份列只认已有、不覆盖：条目已有
    tmdb_id 时不以 fetched 的去换（与现行 ``item.tmdb_id or fetched``
    同口径）。

    置信度不编数字：显式来源（条目自带 / NFO 给的 tmdb_id）记
    ``explicit``，搜索命中记 ``search``——证据快照里写清判定依据，
    调用方与审查员看的是证据本身，不是分数。
    """
    fetched = fetched or {}
    nfo_data = fetched.get("nfo_data") or {}
    details = fetched.get("tmdb_details") or {}
    had_tmdb = bool(getattr(item, "tmdb_id", None))
    tmdb_id = (
        getattr(item, "tmdb_id", None)
        or fetched.get("tmdb_id")
        or nfo_data.get("tmdb_id")
    )
    imdb_id = (
        getattr(item, "imdb_id", None)
        or details.get("imdb_id")
        or nfo_data.get("imdb_id")
    )
    if not tmdb_id and not imdb_id:
        return None
    if tmdb_id:
        provider, external_id = "tmdb", str(tmdb_id)
    else:
        provider, external_id = "imdb", str(imdb_id)
    confidence = (
        "explicit"
        if (had_tmdb or bool(nfo_data.get("tmdb_id")))
        else "search"
    )
    evidence = {
        "tmdb_id_source": (
            "item" if had_tmdb else ("nfo" if nfo_data.get("tmdb_id") else "fetched")
        ),
        "has_details": bool(details),
        "has_nfo": bool(nfo_data),
        "item_type": getattr(item, "item_type", None),
        "item_title": getattr(item, "name", None),
    }
    return {
        "provider": provider,
        "external_id": external_id,
        "tmdb_id": str(tmdb_id) if tmdb_id else None,
        "imdb_id": str(imdb_id) if imdb_id else None,
        "confidence": confidence,
        "evidence": evidence,
    }


def _upsert_stage_record(db, item_id: int, stage: str, binding: Optional[dict]) -> None:
    """识别 / 补资料段记录 upsert（按 item_id 一行，状态集中维护）。"""
    now = datetime.now()
    record = (
        db.query(em.EnrichStageRecord)
        .filter(em.EnrichStageRecord.item_id == item_id)
        .first()
    )
    if record is None:
        record = em.EnrichStageRecord(item_id=item_id)
        db.add(record)
    record.stage = stage
    if binding is not None:
        record.provider = binding.get("provider")
        record.external_id = binding.get("external_id")
        record.confidence = binding.get("confidence")
        record.evidence_json = json.dumps(
            binding.get("evidence") or {}, ensure_ascii=False
        )
    record.updated_at = now


def apply_identity(db, item, fetched: dict) -> Optional[dict]:
    """识别段：只写身份列 + 一段识别记录，不写任何资料字段。

    返回绑定 dict；无绑定（fetched 与条目都没有 tmdb / imdb 身份）时
    返回 None 且什么都不写——调用方据此判定“识别未定”，不进补资料段。
    """
    binding = extract_binding(item, fetched)
    if binding is None:
        return None
    if _empty(getattr(item, "tmdb_id", None)) and binding.get("tmdb_id"):
        item.tmdb_id = binding["tmdb_id"]
    if _empty(getattr(item, "imdb_id", None)) and binding.get("imdb_id"):
        item.imdb_id = binding["imdb_id"]
    _upsert_stage_record(db, item.id, "identified", binding)
    logger.info(
        "识别段提交 id=%s provider=%s external_id=%s confidence=%s",
        item.id, binding["provider"], binding["external_id"],
        binding["confidence"],
    )
    return binding


def fill_candidates_from_fetched(fetched: dict) -> dict:
    """从已采集证据里抽补资料候选（纯函数，不写库）。

    只取白名单字段；空值直接丢掉——空候选不算“填上了”，也不覆盖。
    """
    fetched = fetched or {}
    details = fetched.get("tmdb_details") or {}
    candidates = {
        "overview": details.get("overview") or "",
        "poster_path": fetched.get("poster") or details.get("poster_path") or "",
        "backdrop_path": fetched.get("fanart") or details.get("backdrop_path") or "",
        "community_rating": details.get("vote_average"),
    }
    return {k: v for k, v in candidates.items() if not _empty(v)}


def _frodo_candidates(item) -> dict:
    """Frodo 补资料候选（无开关内层，不写库、不抛异常）。

    本函数不管独立 Frodo 开关：开关由调用方（fill_from_frodo 或聚合
    通道）各自把关——聚合通道是自己的开关，开聚合即开它里面的
    Frodo 腿。熔断 / 封禁 / 异常三道闸都在本层：只返回空，不挡其他源。
    """
    try:
        from backend.emby_server.metasources import breaker as provider_breaker
        from backend.emby_server.metasources import douban_frodo as frodo
    except Exception as exc:  # noqa: BLE001 — Frodo 腿不可用不影响补资料段
        logger.warning("Frodo 补资料源导入失败，跳过 id=%s: %s", item.id, exc)
        return {}
    if not provider_breaker.allow(frodo.PROVIDER):
        logger.warning("Frodo 熔断中，补资料跳过 Frodo id=%s", item.id)
        return {}
    try:
        from backend.emby_server import douban as douban_leg

        if douban_leg.ban_remaining() > 0:
            logger.warning("豆瓣封禁期，补资料跳过 Frodo id=%s", item.id)
            return {}
    except Exception as exc:  # noqa: BLE001 — 封禁状态问不到时保守跳过 Frodo
        logger.warning("豆瓣封禁状态未知，补资料跳过 Frodo id=%s: %s", item.id, exc)
        return {}
    try:
        subject = None
        imdb_id = getattr(item, "imdb_id", None)
        if imdb_id:
            subject = frodo.find_by_imdb(str(imdb_id), _item_year(item))
        if subject is None:
            title = getattr(item, "name", None)
            if title:
                subject = frodo.search_by_title(str(title), _item_year(item))
        if not subject:
            return {}
        mapped = {
            "overview": subject.get("overview") or "",
            "community_rating": subject.get("rating"),
            "poster_path": subject.get("image") or "",
        }
        return {k: v for k, v in mapped.items() if not _empty(v)}
    except Exception as exc:  # noqa: BLE001 — Frodo 任何失败都不进补资料流程
        logger.warning("Frodo 补资料失败，跳过 id=%s: %s", item.id, exc)
        return {}


def fill_from_frodo(item) -> dict:
    """Frodo 补资料缺口（只返回候选 dict，不写库、不抛异常）。

    路线：有 imdb_id 走 IMDb 桥精确换豆瓣条目（不拿片名撞运气）；
    无 imdb_id 时走 Frodo 片名搜（模块内已有置信门槛，过不了当没搜到）。
    熔断中 / 封禁中 / 任何异常 → 返回 {}，绝不挡其他源、不出旧数。
    熔断记账在 douban_frodo 模块的请求层（allow / record 已在内），
    本层不重复记账（重复记会把失败计数翻倍、提前熔断）。
    """
    if not frodo_fill_enabled():
        return {}
    return _frodo_candidates(item)


def apply_fill(db, item, fetched: dict) -> list:
    """补资料段：只在已绑定条目上、只填空缺字段。

    调用方须保证条目已绑定（tmdb_id 或 imdb_id 非空）；未绑定时本函数
    拒绝写任何字段、返回 []（防线，不依赖调用方自觉）。
    顺序：先用已采集证据填，仍缺的 overview / 评分 / 海报再问 Frodo；
    Frodo 只补缺口，不覆盖已填。Frodo 之后仍缺的字段再问中文聚合
    通道（聚合开关关时它返回空，行为与本批之前逐字一致；聚合开时
    它按源优先级逐字段补缺，Frodo 腿在聚合里不受独立 Frodo 开关
    管辖——见 zh_aggregate 模块头注）。返回实际填上的字段名列表。
    """
    if _empty(getattr(item, "tmdb_id", None)) and _empty(
        getattr(item, "imdb_id", None)
    ):
        logger.warning("补资料段拒绝未绑定条目 id=%s（不写任何字段）", item.id)
        return []
    filled = []
    candidates = fill_candidates_from_fetched(fetched)
    for field in FILL_FIELDS:
        if _empty(getattr(item, field, None)) and field in candidates:
            setattr(item, field, candidates[field])
            filled.append(field)
    still_missing = [
        f for f in FRODO_FILL_FIELDS
        if f not in filled and _empty(getattr(item, f, None))
    ]
    if still_missing:
        frodo_candidates = fill_from_frodo(item)
        for field in still_missing:
            if field in frodo_candidates:
                setattr(item, field, frodo_candidates[field])
                filled.append(field)
    agg_missing = [
        f for f in FILL_FIELDS
        if f not in filled and _empty(getattr(item, f, None))
    ]
    if agg_missing:
        try:
            from backend.emby_server.metasources import zh_aggregate as _zh

            agg_candidates, _agg_prov = _zh.aggregate(item, fetched, agg_missing)
        except Exception as exc:  # noqa: BLE001 — 聚合失败不拖垮补资料段
            logger.warning("中文聚合失败，跳过 id=%s: %s", item.id, exc)
            agg_candidates = {}
        for field in agg_missing:
            if field in agg_candidates:
                setattr(item, field, agg_candidates[field])
                filled.append(field)
    binding = None
    if getattr(item, "tmdb_id", None) or getattr(item, "imdb_id", None):
        binding = {
            "provider": "tmdb" if getattr(item, "tmdb_id", None) else "imdb",
            "external_id": str(
                getattr(item, "tmdb_id", None) or getattr(item, "imdb_id", None)
            ),
            "confidence": None,
            "evidence": {"filled_fields": filled},
        }
    _upsert_stage_record(db, item.id, "filled", binding)
    logger.info("补资料段提交 id=%s filled=%s", item.id, filled)
    return filled
