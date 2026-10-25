"""AI 参谋接线层（高级刮削第二批的接线批）。

本层是参谋模块与现行写库路径之间的唯一接线口，守模块原则：

- 只提名不裁决：本层不产绑定判定——建议的 allowed 与否由
  ai_policy 门定；建议能不能进待批、批了能不能落库，裁决权在门
  与人工手里，不在本层。
- 不直接写身份：落库只走现行 identify 写库路径
  （``identify_two_stage.apply_identity``），本层不提供任何直写
  tmdb_id / imdb_id 的旁路。已批 diff 应用时 fetched 带
  ``human_approved`` 标记：纯 AI 提名被门拒是“AI 独自定案”的拒，
  人工拍板后是“人定了、AI 只是提名”，门拒不再适用（权重仍钳制
  到 0.2）；未拍板的纯 AI 建议走本层也批不动、落不了库。
- 审计持久化：建议一产出即落 ``emby_ai_suggestions`` 行（含全量
  to_dict 快照与 audit trail），拍板 / 落库结果追加进同一行的
  audit，不另起审计表、不复用识别审查队列表（那张表的 reason
  CHECK 只允许 no_confident_match，参谋状态机与它不是一回事）。
- 提供方注入：provider 由调用方注入；本层自带的默认 provider 走
  现有 ``integrations/ai.py`` 的 chat（管理员自己配的端点 / 模型 /
  密钥，本层不写死任何模型、不内置任何 HTTP 客户端）。env 里的
  AI_ADVISOR_PROVIDER / AI_ADVISOR_MODEL 只作建议的记账标签，
  真正调的模型是管理员在 integrations/ai 里配的那个，raw 里记
  实际模型名以便对账。
- 不自动触发：本批不给 enrich 流水线加自动参谋触发——参谋一次
  请求烧一次管理员配的 AI 额度，而纯 AI 提名在门那里本就会被拒
  （无非 AI 证据时进不了待批），自动触发等于花钱产出只能看不能
  批的记录。请求一律由显式调用方发起（后续管理端拍板入口接这里），
  触发策略另行定。
- 提交归调用方：本层只 add / flush，不 commit（与 apply_identity、
  _upsert_stage_record 同口径）；调用方在自己的事务边界提交。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from backend.emby_server import models as em
from backend.emby_server.metasources import ai_advisor as advisor

logger = logging.getLogger(__name__)

#: 提名 prompt 要求模型只回 JSON（多余文字由解析层容忍、只取首个对象）。
_NOMINATE_SYSTEM = (
    "你是影视身份提名助手。只根据片名、年份、类型提名最可能的 "
    "TMDB / IMDb 身份，不确定就留空。只返回一个 JSON 对象，形如 "
    '{"tmdb_id": "60300", "imdb_id": "tt0903747", "ai_weight": 0.5}；'
    "不知道的字段填 null，不要编造，不要加任何解释文字。"
)

_TMDB_RE = re.compile(r"^\d+$")
_IMDB_RE = re.compile(r"^tt\d+$")


def build_provider(db):
    """默认 provider：走 integrations/ai 的 chat（管理员自配）。

    未配置 / 不可用时返回 None——参谋链路随之静默关闭，不报错、
    不阻塞调用方。provider 签名与 ai_advisor 约定一致：
    ``provider(item_key, existing_evidence) -> dict | None``。
    """
    try:
        from backend.integrations import ai as _ai
    except Exception as exc:  # noqa: BLE001 — AI 能力不可用不拖垮接线层
        logger.warning("AI 能力模块不可用，参谋 provider 关闭：%s", exc)
        return None
    try:
        if not _ai.available(db):
            return None
    except Exception as exc:  # noqa: BLE001 — 配置读不到时保守关闭
        logger.warning("AI 能力配置读取失败，参谋 provider 关闭：%s", exc)
        return None

    def _provider(item_key, existing_evidence):
        evidence = existing_evidence or {}
        title = evidence.get("title") or evidence.get("item_title") or ""
        year = evidence.get("year") or evidence.get("item_year") or ""
        kind = evidence.get("item_type") or ""
        user_text = (
            f"片名：{title}\n年份：{year}\n类型：{kind}\n条目：{item_key}\n"
            "请提名 TMDB / IMDb 身份（只回 JSON）。"
        )
        try:
            result = _ai.chat(
                db,
                [
                    {"role": "system", "content": _NOMINATE_SYSTEM},
                    {"role": "user", "content": user_text},
                ],
                max_tokens=256,
                use_system_prompt=False,
            )
        except Exception as exc:  # noqa: BLE001 — 上游失败 = 无建议
            logger.warning("AI 参谋 chat 调用失败（item=%s）：%s", item_key, exc)
            return None
        if not isinstance(result, dict) or not result.get("ok"):
            return None
        parsed = _parse_nomination(result.get("answer") or "")
        if parsed is None:
            return None
        parsed["model"] = result.get("model") or ""
        return parsed

    return _provider


def _parse_nomination(answer: str) -> dict | None:
    """从模型回答里抠首个 JSON 对象并校验字段（不编数字、不编 id）。

    tmdb_id 只认纯数字串，imdb_id 只认 tt + 数字；ai_weight 转不成
    数字时记 None（门会按 None 处理）。两个 id 都空时返回 None——
    空提名不是一条建议。
    """
    if not isinstance(answer, str) or not answer.strip():
        return None
    start = answer.find("{")
    end = answer.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        raw = json.loads(answer[start:end + 1])
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    tmdb_id = raw.get("tmdb_id")
    imdb_id = raw.get("imdb_id")
    tmdb_id = str(tmdb_id) if isinstance(tmdb_id, (str, int)) else None
    if tmdb_id is not None and not _TMDB_RE.match(tmdb_id.strip()):
        tmdb_id = None
    elif tmdb_id is not None:
        tmdb_id = tmdb_id.strip()
    imdb_id = str(imdb_id) if isinstance(imdb_id, str) else None
    if imdb_id is not None and not _IMDB_RE.match(imdb_id.strip()):
        imdb_id = None
    elif imdb_id is not None:
        imdb_id = imdb_id.strip()
    if not tmdb_id and not imdb_id:
        return None
    try:
        weight = float(raw["ai_weight"]) if raw.get("ai_weight") is not None else None
    except (TypeError, ValueError):
        weight = None
    if weight is not None and weight != weight:  # NaN 不进门
        weight = None
    return {"tmdb_id": tmdb_id, "imdb_id": imdb_id, "ai_weight": weight}


def row_to_suggestion(row) -> advisor.Suggestion | None:
    """把建议行还原成 Suggestion（状态与 audit 原样回来）。"""
    if row is None:
        return None
    try:
        snapshot = json.loads(row.suggestion_json or "{}")
    except (ValueError, TypeError):
        snapshot = {}
    return advisor.Suggestion(
        item_key=str(snapshot.get("item_key") or row.item_id),
        proposed_tmdb_id=snapshot.get("proposed_tmdb_id"),
        proposed_imdb_id=snapshot.get("proposed_imdb_id"),
        ai_weight=snapshot.get("ai_weight"),
        confidence_band=snapshot.get("confidence_band"),
        status=str(snapshot.get("status") or row.status),
        reason=str(snapshot.get("reason") or ""),
        provider=str(snapshot.get("provider") or row.provider or ""),
        model=str(snapshot.get("model") or row.model or ""),
        raw=dict(snapshot.get("raw") or {}),
        audit=[dict(e) for e in (snapshot.get("audit") or [])],
    )


def _sync_row_from_suggestion(row, suggestion) -> None:
    """把建议的最新状态写回行（不提交，调用方提交）。"""
    row.status = suggestion.status
    row.proposed_tmdb_id = suggestion.proposed_tmdb_id
    row.proposed_imdb_id = suggestion.proposed_imdb_id
    row.ai_weight = suggestion.ai_weight
    row.confidence_band = suggestion.confidence_band
    row.reason = suggestion.reason
    row.suggestion_json = json.dumps(suggestion.to_dict(), ensure_ascii=False)


def request_and_persist_suggestion(db, item, existing_evidence=None,
                                   provider=None):
    """要一条建议并持久化（链路关闭 / 无建议时返回 None，不落行）。

    provider 为空时用默认 provider（integrations/ai）；默认 provider
    拿不到（未配置）时整个调用静默返回 None——不产空行、不记假账。
    本函数只 add / flush，不 commit。
    """
    if provider is None:
        provider = build_provider(db)
    if provider is None:
        return None
    suggestion = advisor.request_suggestion(
        str(item.id), existing_evidence or {}, provider)
    if suggestion is None:
        return None
    row = em.AiAdvisorSuggestion(
        item_id=item.id,
        status=suggestion.status,
        provider=suggestion.provider,
        model=suggestion.model,
        proposed_tmdb_id=suggestion.proposed_tmdb_id,
        proposed_imdb_id=suggestion.proposed_imdb_id,
        ai_weight=suggestion.ai_weight,
        confidence_band=suggestion.confidence_band,
        reason=suggestion.reason,
        suggestion_json=json.dumps(suggestion.to_dict(), ensure_ascii=False),
    )
    db.add(row)
    db.flush()
    logger.info("AI 参谋建议已持久化 item=%s status=%s provider=%s",
                item.id, suggestion.status, suggestion.provider)
    return row


def get_suggestion_row(db, row_id):
    """取建议行（没有时返回 None，不抛异常）。"""
    return (
        db.query(em.AiAdvisorSuggestion)
        .filter(em.AiAdvisorSuggestion.id == row_id)
        .first()
    )


def approve_suggestion(db, row_id, approver: str = "human"):
    """人工拍板通过（只改建议状态，不写任何身份绑定）。

    门拒 / 已驳回的建议批不动（原样返回行，audit 里记一条
    approve_blocked）。本函数只 flush，不 commit。
    """
    row = get_suggestion_row(db, row_id)
    if row is None:
        return None
    suggestion = row_to_suggestion(row)
    approved = advisor.approve_suggestion(suggestion, approver)
    _sync_row_from_suggestion(row, approved)
    row.approved_by = approver
    db.flush()
    return row


def reject_suggestion(db, row_id, approver: str = "human"):
    """人工驳回（只改建议状态，不写任何身份绑定）。"""
    row = get_suggestion_row(db, row_id)
    if row is None:
        return None
    suggestion = row_to_suggestion(row)
    rejected = advisor.reject_suggestion(suggestion, approver)
    _sync_row_from_suggestion(row, rejected)
    db.flush()
    return row


def apply_approved_suggestion(db, row_id):
    """把已批 diff 走现行 identify 写库路径落库。

    返回 ``(binding | None, outcome)``：outcome 取值
    ``applied`` / ``not_approved`` / ``item_missing`` / ``no_binding``
    / ``apply_failed``。未批、条目已删、无身份可写时一律不写库，
    但会在行 audit 里记一条 outcome（有据可查“为什么没落”）。
    本函数只 flush，不 commit。
    """
    from backend.emby_server import identify_two_stage as ts

    row = get_suggestion_row(db, row_id)
    if row is None:
        return None, "not_found"
    if row.status != "approved":
        return None, "not_approved"
    suggestion = row_to_suggestion(row)
    item = (
        db.query(em.MediaItem)
        .filter(em.MediaItem.id == row.item_id)
        .first()
    )
    if item is None:
        failed = advisor.record_outcome(suggestion, "failed", "item_missing")
        _sync_row_from_suggestion(row, failed)
        db.flush()
        return None, "item_missing"
    fetched = {
        "tmdb_id": row.proposed_tmdb_id,
        "imdb_id": row.proposed_imdb_id,
        "human_approved": True,
        "approved_by": row.approved_by or "human",
    }
    try:
        binding = ts.apply_identity(db, item, fetched)
    except Exception as exc:  # noqa: BLE001 — 落库异常不拖垮调用方
        logger.warning("已批建议落库异常 item=%s: %s", row.item_id, exc)
        failed = advisor.record_outcome(suggestion, "failed", str(exc)[:200])
        _sync_row_from_suggestion(row, failed)
        db.flush()
        return None, "apply_failed"
    if binding is None:
        failed = advisor.record_outcome(
            suggestion, "failed", "no_binding_or_gate_refused")
        _sync_row_from_suggestion(row, failed)
        db.flush()
        return None, "no_binding"
    applied = advisor.record_outcome(
        suggestion, "applied",
        "provider=%s external_id=%s" % (binding["provider"],
                                        binding["external_id"]))
    _sync_row_from_suggestion(row, applied)
    row.applied_at = datetime.now()
    db.flush()
    logger.info("已批建议已落库 item=%s provider=%s external_id=%s",
                row.item_id, binding["provider"], binding["external_id"])
    return binding, "applied"
