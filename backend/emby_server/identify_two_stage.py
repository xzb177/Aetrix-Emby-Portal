"""识别 / 补资料两段式 + 豆瓣 Frodo 补资料源（清单第 13 项，FMBY #17 / #14）。

对应魂 1（身份与证据先行）：刮削的写库拆成两段——

- 识别段：只定身份绑定（provider / 实体 / 置信度 / 证据），写身份列
  （tmdb_id / imdb_id）与一段识别记录，不写任何资料字段；
- 补资料段：只在已绑定条目上跑，只填空缺、永不覆盖已有资料
  （2026-10-19 定版口径：只填空缺永不覆盖）。

两段独立开关、独立重试（默认双开，FMBY #17 闭环后即真实路径）：
识别段提交后即落库，补资料失败不回滚身份；识别未定的条目不进补
资料段（由现行写库路径接管审查 / none 终态）。

与现行 enrich_worker 的分工：IO 阶段（_enrich_fetch）仍是证据采集的
共享步骤；写库阶段在任一段开关开时走两段提交，开关全关时与现行逐字
一致。本模块不碰扫描器、播放变体、身份锚（source_id / provider_file_id
是另一路的地盘）、绑定管理、列表接口、凭据密封。

Frodo 在本模块里的位置：补资料段的缺口来源之一（不是识别段的认亲
依据——认亲只认 TMDB / NFO / 别名给出的身份，Frodo 片名搜不用来定
身份）。桥用的 IMDb 编号解析口径见 _frodo_imdb_id：条目已有 →
本轮 tmdb_details → nfo_data，只用已有证据、不另拉详情。
Frodo 腿复用 #1203 熔断注册表（douban_frodo 源名，403 / 429
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

#: 识别段总开关（默认开，FMBY #17 闭环：两段式已是真实刮削路径，
#: 不再是仅供测试的实验开关；要回旧写库须显式把两段开关都置 0）。
#: 启动时读一次，热路径不碰 env。
IDENTIFY_STAGE_ENABLED = env_int("IDENTIFY_STAGE_ENABLED", 1, 0, 1)
#: 补资料段总开关（默认开）。与识别段独立：可只开识别攒绑定，也可只开
#: 补资料给历史已绑定条目填空。
FILL_STAGE_ENABLED = env_int("FILL_STAGE_ENABLED", 1, 0, 1)
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


def _clean_identity(value):
    """身份值的归一：None 或纯空白字符串都不算身份（返回 None）。

    与 ``_empty`` / ``enrich_queues._is_empty`` 同 strip 口径（空格与
    ``\t\n\r\f\v`` 都算空白）。字符串去首尾空白后返回——真实编号里
    不该有首尾空白，归一后 ``external_id`` 不再带空格；非字符串按原
    真值（0 / 空容器仍算空，与原裸 ``or`` 链的跳过语义一致）。
    """
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned if cleaned else None
    return value if value else None


def _first_identity(*values):
    """按优先序取第一个非空身份（替代裸 ``or`` 链）。

    裸 ``or`` 只跳过 falsy 值：纯空白字符串（如 ``"  "``、``"\t"``）
    是 truthy，会被当成已有身份返回——空白身份就此冒充绑定，还会
    压住后一顺位上真实的身份值。本函数逐个归一，只认非空值。
    """
    for value in values:
        cleaned = _clean_identity(value)
        if cleaned is not None:
            return cleaned
    return None


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

    空白不是绑定（根治，2026-10-27）：各来源身份值先按 strip 口径归
    一再取优先序，None / 空串 / 纯空白字符串一律不算身份——裸 ``or``
    会把纯空白当成已有绑定返回并 upsert 一段 identified 记录，是 B 批
    （#1374）实锤的域外同根点。归一后空白身份在 extract_binding 层
    即返回 None；B 批在 enrich_worker 层的域内否决仍保留作防御纵深，
    两层口径一致、不互相替代。

    置信度不编数字：显式来源（条目自带 / NFO 给的 tmdb_id）记
    ``explicit``，搜索命中记 ``search``——证据快照里写清判定依据，
    调用方与审查员看的是证据本身，不是分数。
    """
    fetched = fetched or {}
    nfo_data = fetched.get("nfo_data") or {}
    details = fetched.get("tmdb_details") or {}
    item_tmdb = _clean_identity(getattr(item, "tmdb_id", None))
    nfo_tmdb = _clean_identity(nfo_data.get("tmdb_id"))
    had_tmdb = item_tmdb is not None
    tmdb_id = _first_identity(
        getattr(item, "tmdb_id", None),
        fetched.get("tmdb_id"),
        nfo_data.get("tmdb_id"),
    )
    imdb_id = _first_identity(
        getattr(item, "imdb_id", None),
        details.get("imdb_id"),
        nfo_data.get("imdb_id"),
        # 接线批：已批建议的 fetched 直接带顶层 imdb_id（与 tmdb_id 的
        # 顶层读取对称），优先级最低，不动已有 / 详情 / NFO 的优先序。
        fetched.get("imdb_id"),
    )
    if not tmdb_id and not imdb_id:
        return None
    if tmdb_id:
        provider, external_id = "tmdb", str(tmdb_id)
    else:
        provider, external_id = "imdb", str(imdb_id)
    confidence = (
        "explicit"
        if (had_tmdb or nfo_tmdb is not None)
        else "search"
    )
    evidence = {
        "tmdb_id_source": (
            "item" if had_tmdb else ("nfo" if nfo_tmdb is not None else "fetched")
        ),
        "has_details": bool(details),
        "has_nfo": bool(nfo_data),
        "item_type": getattr(item, "item_type", None),
        "item_title": getattr(item, "name", None),
    }
    # AI 提名标记透传（清单第 17 项）：未来 AI 提名器经 fetched 带
    # ai_suggested / ai_sources / ai_weight；不带时证据形状与今天逐字
    # 一致，政策门对现行行为零影响。
    if "ai_suggested" in fetched:
        evidence["ai_suggested"] = bool(fetched.get("ai_suggested"))
    # 人工拍板标记透传（接线批）：已批的 AI 建议落库时带
    # human_approved，门拒不再适用（人定了、AI 只是提名）；未拍板时
    # 不带此键，门行为与今天逐字一致。
    if fetched.get("human_approved"):
        evidence["human_approved"] = True
        if fetched.get("approved_by"):
            evidence["approved_by"] = str(fetched.get("approved_by"))
    if fetched.get("ai_sources") is not None:
        evidence["sources"] = list(fetched.get("ai_sources") or [])
    elif fetched.get("sources") is not None:
        evidence["sources"] = list(fetched.get("sources") or [])
    if fetched.get("ai_weight") is not None:
        evidence["ai_weight"] = fetched.get("ai_weight")
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
    # 规则包打戳（FMBY 清单 #18）：每次写段记录都钉当刻生效的规则包，
    # 列上钉版本 + 生效哈希，证据快照里同步一份（决策日志口径一致）。
    # 不改 binding 本身（只拷一份证据加键），调用方手里的 binding 不变。
    from backend.emby_server.rule_pack import default_pack as _default_pack

    _pack = _default_pack()
    record.rule_pack_version = _pack.version
    record.rule_pack_hash = _pack.effective_hash
    if binding is not None:
        record.provider = binding.get("provider")
        record.external_id = binding.get("external_id")
        record.confidence = binding.get("confidence")
        _evidence = dict(binding.get("evidence") or {})
        _evidence["rule_pack_version"] = _pack.version
        _evidence["rule_pack_hash"] = _pack.effective_hash
        record.evidence_json = json.dumps(_evidence, ensure_ascii=False)
    record.updated_at = now


def resolve_imdb_id(item, fetched: dict, details_fetcher=None):
    """TMDB→IMDb 显式解析步（已拍板放行）。

    返回 (imdb_id, source)，解析不到时 (None, None)；source 取值：
    item / details / details_external_ids / nfo / fetched /
    tmdb_details（显式拉详情）。顺序只填空缺、永不覆盖，不降级
    标题猜：条目已有 imdb_id → 本轮 tmdb_details.imdb_id →
    tmdb_details.external_ids.imdb_id（TV 详情的 IMDb 号只在这里，
    顶层没有）→ nfo_data.imdb_id → fetched 顶层 imdb_id →
    有 tmdb_id 且给了 details_fetcher 时显式拉一次 TMDB 详情
    （details() 自带 append_to_response=external_ids，电影与剧集
    同口径），从详情的 imdb_id / external_ids.imdb_id 取。

    details_fetcher 不给时只读已有证据，不发请求——调用方（识别段
    分发）负责在生产链路上传入 TMDB 客户端；测试注入替身。
    只有 movie / series 才走网络解析：episode / season 的 TMDB
    编号不在 movie / tv 详情端点口径内，不许拿错端点猜。
    任何异常只记日志返回 (None, None)，绝不拖垮识别段。
    """
    # strip 归一（完美审查第三轮）：本函数此前全是裸 or / 裸真值判定，
    # 与 #1377 的 extract_binding 口径脱节——空白 imdb 会被当成“条目
    # 已有”返回并落库；空白 item tmdb 会压住 fetched 的真实 tmdb，
    # 导致详情取数器拿着 "  " 去调 TMDB。以下各步只认归一后的非空值。
    imdb_id = _clean_identity(getattr(item, "imdb_id", None))
    if imdb_id is not None:
        return str(imdb_id), "item"
    fetched = fetched or {}
    details = fetched.get("tmdb_details") or {}
    _details_imdb = _clean_identity(details.get("imdb_id"))
    if _details_imdb is not None:
        return str(_details_imdb), "details"
    external_ids = details.get("external_ids") or {}
    _ext_imdb = _clean_identity(
        external_ids.get("imdb_id")) if isinstance(
            external_ids, dict) else None
    if _ext_imdb is not None:
        return str(_ext_imdb), "details_external_ids"
    nfo_data = fetched.get("nfo_data") or {}
    _nfo_imdb = _clean_identity(nfo_data.get("imdb_id"))
    if _nfo_imdb is not None:
        return str(_nfo_imdb), "nfo"
    _fetched_imdb = _clean_identity(fetched.get("imdb_id"))
    if _fetched_imdb is not None:
        return str(_fetched_imdb), "fetched"
    tmdb_id = _first_identity(
        getattr(item, "tmdb_id", None),
        fetched.get("tmdb_id"),
        nfo_data.get("tmdb_id"),
    )
    if not tmdb_id or details_fetcher is None:
        return None, None
    if getattr(item, "item_type", None) not in ("movie", "series"):
        return None, None
    kind = "series" if getattr(item, "item_type", None) == "series" else "movie"
    try:
        data = details_fetcher(str(tmdb_id), kind)
    except Exception as exc:  # noqa: BLE001 — 解析失败 = 本轮无 IMDb 身份
        logger.warning(
            "TMDB→IMDb 显式解析失败 id=%s tmdb_id=%s: %s",
            getattr(item, "id", None), tmdb_id, exc)
        return None, None
    if not isinstance(data, dict):
        return None, None
    resolved = data.get("imdb_id")
    if not resolved:
        ext = data.get("external_ids") or {}
        if isinstance(ext, dict):
            resolved = ext.get("imdb_id")
    if resolved:
        return str(resolved), "tmdb_details"
    return None, None


#: 远端身份认亲可继承绑定的条目类型（FMBY #13 接线）：只有 movie /
#: series 行才 carries tmdb_id / imdb_id 绑定；episode / season 行的
#: 编号不在 movie / tv 详情端点口径内，不许拿 donor 的绑定去贴。
_REMOTE_IDENTITY_INHERIT_TYPES = ("movie", "series")


def _donor_binding(rows) -> tuple:
    """donor 行 → (binding 对, 是否歧义)。

    binding 对 = (tmdb_id, imdb_id)，空值归一为 None；行里两个号都空
    的不算 donor。恰好一种 binding 对才可继承；两种及以上互相不
    一致的对 = 歧义，返回 (None, True)，调用方不得再用弱层去覆盖
    强层的歧义（宁可错分不可错认，与扫描器认亲层同口径）。
    """
    pairs = set()
    for row in rows:
        # strip 归一（完美审查第三轮）：裸 or 会把纯空白 donor 行
        # （如 tmdb_id="  "）算成有效 donor——不但能继承出空白绑定，
        # 还会在与真实 donor 并存时把“恰好一种”毒化成歧义、挡掉继承。
        tmdb = _clean_identity(getattr(row, "tmdb_id", None))
        imdb = _clean_identity(getattr(row, "imdb_id", None))
        if tmdb or imdb:
            pairs.add((str(tmdb) if tmdb else None,
                       str(imdb) if imdb else None))
    if not pairs:
        return None, False
    if len(pairs) > 1:
        return None, True
    return next(iter(pairs)), False


def inherit_binding_from_remote_identity(db, item) -> Optional[dict]:
    """远端身份认亲（FMBY #13）：识别段的 donor 继承。

    扫描器认亲层（scanner 的 file_id / content_hash / 指纹层）只在
    扫描入库时复用旧行；刮削识别段此前完全不认远端身份——同一文件
    被扫进两个库、或改名后旧行已绑定而新行未绑定时，识别段只能靠
    标题搜索重认，远端身份白白作废。本函数把认亲接进识别段：当
    extract_binding 无绑定时，按远端身份找已绑定的 donor 行继承。

    层序（与扫描器同口径，强层优先）：
    - file_id 层：同 drive_file_id 的可见行（deleted_at IS NULL 且
      merged_into_id IS NULL，本行除外）；
    - content_hash 层：同 (content_hash, content_hash_algo, size)，
      hash 与 algo 均非空、size > 0。
    任一层 donor 歧义（同一身份对应两种及以上不一致的绑定）即返回
    None，不再退到弱层。movie / series 以外不继承；条目已有任一
    号时不继承（只填空缺、永不覆盖）；DB 异常只记日志返回 None，
    绝不拖垮识别段。返回的 binding 形状与 extract_binding 一致，
    confidence 记 ``inherited``，证据里钉 donor 行 id 与层名（不钉
    原始 fid / hash 值，审计时按行反查即可）。
    """
    try:
        if getattr(item, "item_type", None) not in _REMOTE_IDENTITY_INHERIT_TYPES:
            return None
        # strip 口径（完美审查第三轮）：裸 or 会把纯空白身份当成“已有号”，
        # 让空白条目永远走不到 donor 继承——extract_binding 刚因空白返回
        # None，继承门又因同一空白把它挡在门外，空白条目无路可认。
        if (_clean_identity(getattr(item, "tmdb_id", None)) is not None
                or _clean_identity(getattr(item, "imdb_id", None)) is not None):
            return None
        item_id = getattr(item, "id", None)
        MI = em.MediaItem
        visible = (
            MI.deleted_at.is_(None),
            MI.merged_into_id.is_(None),
        )
        donor_rows = []
        layer = None
        fid = getattr(item, "drive_file_id", None) or ""
        if fid:
            query = db.query(MI).filter(
                MI.drive_file_id == fid, *visible)
            if item_id is not None:
                query = query.filter(MI.id != item_id)
            donor_rows = query.all()
            pair, ambiguous = _donor_binding(donor_rows)
            if ambiguous:
                logger.warning(
                    "远端身份认亲放弃（file_id 歧义）id=%s fid 命中 %s 行",
                    item_id, len(donor_rows))
                return None
            if pair is not None:
                layer = "file_id"
        if layer is None:
            _hash = getattr(item, "content_hash", None) or ""
            _algo = getattr(item, "content_hash_algo", None) or ""
            _size = getattr(item, "size", 0) or 0
            if _hash and _algo and _size > 0:
                query = db.query(MI).filter(
                    MI.content_hash == _hash,
                    MI.content_hash_algo == _algo,
                    MI.size == _size,
                    *visible)
                if item_id is not None:
                    query = query.filter(MI.id != item_id)
                donor_rows = query.all()
                pair, ambiguous = _donor_binding(donor_rows)
                if ambiguous:
                    logger.warning(
                        "远端身份认亲放弃（content_hash 歧义）id=%s",
                        item_id)
                    return None
                if pair is not None:
                    layer = "content_hash"
        if layer is None:
            return None
        tmdb_id, imdb_id = pair
        donor_ids = sorted({r.id for r in donor_rows
                            if getattr(r, "id", None) is not None})
        if tmdb_id:
            provider, external_id = "tmdb", str(tmdb_id)
        else:
            provider, external_id = "imdb", str(imdb_id)
        return {
            "provider": provider,
            "external_id": external_id,
            "tmdb_id": str(tmdb_id) if tmdb_id else None,
            "imdb_id": str(imdb_id) if imdb_id else None,
            "confidence": "inherited",
            "evidence": {
                "remote_identity_layer": layer,
                "donor_item_id": donor_ids[0] if donor_ids else None,
                "donor_count": len(donor_ids),
                "item_type": getattr(item, "item_type", None),
                "item_title": getattr(item, "name", None),
            },
        }
    except Exception as exc:  # noqa: BLE001 — 认亲失败 = 本轮无继承绑定
        logger.warning(
            "远端身份认亲失败 id=%s: %s", getattr(item, "id", None), exc)
        return None


def apply_identity(db, item, fetched: dict, tmdb_details_fetcher=None) -> Optional[dict]:
    """识别段：只写身份列 + 一段识别记录，不写任何资料字段。

    返回绑定 dict；无绑定（fetched 与条目都没有 tmdb / imdb 身份）时
    返回 None 且什么都不写——调用方据此判定“识别未定”，不进补资料段。

    tmdb_details_fetcher：TMDB 详情取数器，签名 (tmdb_id, kind) -> dict，
    由识别段分发传入生产客户端；不给时只读已有证据。绑定有 TMDB
    身份却无 IMDb 身份时，用它显式解析一步（见 resolve_imdb_id）；
    解析不到不改任何已有值，开关语义与此前逐字一致。

    AI 政策门（清单第 17 项，开关 ENRICH_AI_POLICY_ENABLED 默认关）：
    开时纯 AI 提名的绑定一律拒绝落库（返回 None，走现行审查 / none
    终态）；AI 与非 AI 证据并存时落库，但证据里的 ai_weight 钳制到 0.2。
    例外（接线批）：fetched 带 human_approved 时是人工已拍板的建议，
    裁决权在人不在 AI，门拒不再适用（权重仍钳制到 0.2）。
    """
    binding = extract_binding(item, fetched)
    if binding is None:
        # FMBY #13 接线：标题 / NFO / 详情都给不出身份时，按远端身份
        # 向已绑定的 donor 行继承；继承不到仍返回 None，行为与此前一致。
        # 继承绑定同样走下面的 AI 政策门与 IMDb 解析步，不另开口子。
        binding = inherit_binding_from_remote_identity(db, item)
    if binding is None:
        return None
    from backend.emby_server import ai_policy as _aip

    if _aip.ai_policy_enabled():
        verdict = _aip.evaluate_binding_evidence(binding["evidence"])
        if not verdict["allowed"] and not binding["evidence"].get("human_approved"):
            logger.warning(
                "AI 政策门拒绝纯 AI 绑定 id=%s reason=%s",
                item.id, verdict["reason"])
            return None
        if verdict["ai_weight"] is not None:
            binding["evidence"]["ai_weight"] = verdict["ai_weight"]
    if binding.get("tmdb_id") and not binding.get("imdb_id"):
        resolved, source = resolve_imdb_id(item, fetched, tmdb_details_fetcher)
        if resolved:
            binding["imdb_id"] = resolved
            if source == "tmdb_details":
                binding["evidence"]["imdb_id_source"] = "tmdb_explicit"
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


def _frodo_imdb_id(item, fetched: Optional[dict] = None) -> Optional[str]:
    """Frodo 桥用的 IMDb 编号解析（与 extract_binding 同口径，不发请求）。

    顺序：条目已有 imdb_id → 本轮已采集证据 tmdb_details.imdb_id →
    nfo_data.imdb_id。只用本轮已有的证据：不为搭桥另拉 TMDB 详情
    （刮削瘦身已定：缺 imdb_id 不再是拉详情的理由，见 enrich_worker
    的详情收窄注释；要补那一步须另行拍板，不在本函数悄悄做）。
    """
    imdb_id = getattr(item, "imdb_id", None)
    if imdb_id:
        return str(imdb_id)
    fetched = fetched or {}
    details = fetched.get("tmdb_details") or {}
    if details.get("imdb_id"):
        return str(details["imdb_id"])
    nfo_data = fetched.get("nfo_data") or {}
    if nfo_data.get("imdb_id"):
        return str(nfo_data["imdb_id"])
    return None


def _frodo_candidates(item, fetched: Optional[dict] = None) -> dict:
    """Frodo 补资料候选（无开关内层，不写库、不抛异常）。

    本函数不管独立 Frodo 开关：开关由调用方（fill_from_frodo 或聚合
    通道）各自把关——聚合通道是自己的开关，开聚合即开它里面的
    Frodo 腿。熔断 / 封禁 / 异常三道闸都在本层：只返回空，不挡其他源。
    桥用的 IMDb 编号按 _frodo_imdb_id 解析：条目没有时用本轮已采集
    证据里的编号（详情 / NFO），都不给才退回片名搜。
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
        imdb_id = _frodo_imdb_id(item, fetched)
        if imdb_id:
            subject = frodo.find_by_imdb(imdb_id, _item_year(item))
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


def fill_from_frodo(item, fetched: Optional[dict] = None) -> dict:
    """Frodo 补资料缺口（只返回候选 dict，不写库、不抛异常）。

    路线：有 imdb_id 走 IMDb 桥精确换豆瓣条目（不拿片名撞运气）；
    无 imdb_id 时走 Frodo 片名搜（模块内已有置信门槛，过不了当没搜到）。
    桥用的编号按 _frodo_imdb_id 解析：条目没有时用本轮已采集证据里
    的编号，调用方（apply_fill / 聚合通道）须把 fetched 透传进来，
    不传时行为与此前逐字一致。熔断中 / 封禁中 / 任何异常 → 返回 {}，
    绝不挡其他源、不出旧数。熔断记账在 douban_frodo 模块的请求层
    （allow / record 已在内），本层不重复记账（重复记会把失败计数
    翻倍、提前熔断）。
    """
    if not frodo_fill_enabled():
        return {}
    return _frodo_candidates(item, fetched)


def _default_people_fetcher(fetched):
    """默认人物抓取：复用本轮已采集的 TMDB credits（不新增 IO）。

    按名字精确匹配本轮 credits；名字不在 credits 里时返回 None（该行
    本轮跳过，不编来源、不另发请求）。拿不到 credits 时返回 None，
    调用方随之跳过人物补缺。
    """
    try:
        from backend.emby_server.tmdb import cast_list as _cast_list
    except Exception:  # noqa: BLE001 — TMDB 客户端不可用 = 无抓取源
        return None
    details = (fetched or {}).get("tmdb_details") or {}
    try:
        cast = _cast_list(details)
    except Exception:  # noqa: BLE001 — credits 解析失败 = 无抓取源
        return None
    by_name = {c["name"]: c for c in cast if c.get("name")}
    if not by_name:
        return None

    def _fetch(record):
        entry = by_name.get((record or {}).get("name"))
        if entry is None:
            return None
        return {
            "name": entry["name"],
            "role": entry.get("role") or "",
            "image": entry.get("image") or "",
            "person_tmdb_id": entry.get("person_id") or "",
        }

    return _fetch


def _apply_people_fill(db, item, fetched, people_fetcher=None):
    """补资料段人物补缺（只填 emby_people 现有三列、永不覆盖）。

    记录源 = 本条目已有的人物行（不新建行，建行仍归现行
    _apply_cast）；抓取源默认复用本轮 TMDB credits，调用方可注入
    fetcher。开关关 / 无行 / 无抓取源时静默跳过；任何异常只记日志，
    绝不拖垮补资料段。返回实际改动的字段数（日志用，不进返回值）。
    """
    try:
        from backend.emby_server.metasources import people_scrape as _people
    except Exception as exc:  # noqa: BLE001 — 人物模块不可用不影响补资料
        logger.warning("人物刮削模块不可用，跳过 id=%s: %s", item.id, exc)
        return 0
    if not _people.people_scrape_enabled():
        return 0
    if people_fetcher is None:
        people_fetcher = _default_people_fetcher(fetched)
    if people_fetcher is None:
        return 0
    try:
        rows = (
            db.query(em.EmbyPerson)
            .filter(em.EmbyPerson.item_id == item.id)
            .all()
        )
    except Exception as exc:  # noqa: BLE001 — 人物行读不到不影响补资料
        logger.warning("人物行读取失败，跳过 id=%s: %s", item.id, exc)
        return 0
    if not rows:
        return 0
    records = [
        {
            "name": r.name,
            "role": r.role or "",
            "image": r.image or "",
            "person_tmdb_id": r.person_tmdb_id or "",
        }
        for r in rows
    ]
    try:
        result = _people.scrape_people(records, people_fetcher)
    except Exception as exc:  # noqa: BLE001 — 刮削失败不影响补资料
        logger.warning("人物刮削失败，跳过 id=%s: %s", item.id, exc)
        return 0
    filled_by_name = {
        (entry.get("record") or {}).get("name"): entry.get("record")
        for entry in (result.get("filled") or [])
    }
    changed = 0
    for row in rows:
        record = filled_by_name.get(row.name)
        if record is not None:
            changed += len(_people.apply_filled_to_person(row, record))
    if changed:
        logger.info("补资料段人物补缺 id=%s changed=%s", item.id, changed)
    return changed


def apply_fill(db, item, fetched: dict, people_fetcher=None) -> list:
    """补资料段：只在已绑定条目上、只填空缺字段。

    调用方须保证条目已绑定（tmdb_id 或 imdb_id 非空）；未绑定时本函数
    拒绝写任何字段、返回 []（防线，不依赖调用方自觉）。
    顺序：先用已采集证据填，仍缺的 overview / 评分 / 海报再问 Frodo；
    Frodo 只补缺口，不覆盖已填。Frodo 之后仍缺的字段再问中文聚合
    通道（聚合开关关时它返回空，行为与本批之前逐字一致；聚合开时
    它按源优先级逐字段补缺，Frodo 腿在聚合里不受独立 Frodo 开关
    管辖——见 zh_aggregate 模块头注）。最后跑人物补缺（接线批）：
    只填本条目已有人物行的 role / image / person_tmdb_id 空缺，
    永不覆盖、不新建行；开关关时不问源。返回实际填上的字段名列表。
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
        frodo_candidates = fill_from_frodo(item, fetched)
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
    try:
        _apply_people_fill(db, item, fetched, people_fetcher)
    except Exception as exc:  # noqa: BLE001 — 人物补缺绝不拖垮补资料段
        logger.warning("补资料段人物补缺失败，跳过 id=%s: %s", item.id, exc)
    binding = None
    # strip 归一（完美审查第三轮）：裸 or 会让空白 tmdb 压住真实 imdb，
    # 补资料段记录 upsert 成 provider=tmdb / external_id="  " 的脏证据。
    _fill_tmdb = _clean_identity(getattr(item, "tmdb_id", None))
    _fill_imdb = _clean_identity(getattr(item, "imdb_id", None))
    if _fill_tmdb or _fill_imdb:
        binding = {
            "provider": "tmdb" if _fill_tmdb else "imdb",
            "external_id": str(_fill_tmdb or _fill_imdb),
            "confidence": None,
            "evidence": {"filled_fields": filled},
        }
    _upsert_stage_record(db, item.id, "filled", binding)
    logger.info("补资料段提交 id=%s filled=%s", item.id, filled)
    return filled
