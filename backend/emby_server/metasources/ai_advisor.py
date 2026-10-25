"""AI 参谋（FMBY 高级刮削候选 3，第二批）。

定位（侦察口径，一字不差地落码）：

- AI 只当参谋、不当裁决：本模块只产建议，不定身份、不写库。
  参谋绝不直接写身份绑定——落库只能由人工拍板后、走现行
  identify 写库路径完成，本模块不提供任何写绑定 / 写库函数。
- 建议必须走 #1274 已落的 ai_policy 门：权重上限、无非 AI 证据
  不许定、强弱阈值，全部由 ai_policy.py 定，本模块绝不另起一套。
  本模块只调用 gate 的 ``evaluate_binding_evidence``（与
  ``classify_confidence`` 作展示分档），不在此复写任何阈值与判定。
- 防御性导入已退役：#1284 已合署，``ai_policy`` 已在主线，本模块
  直接导入政策门（2026-10-25 接线批）。门缺失不再静默关闭——
  导入失败即在导入时暴露，不把缺门伪装成“参谋关着”。
- 提供方与模型一律走 env（``AI_ADVISOR_PROVIDER`` /
  ``AI_ADVISOR_MODEL``），本模块不写死任何具体模型、不内置任何
  HTTP 客户端：建议内容由调用方注入的 provider 可调用对象产出，
  env 里的提供方 / 模型名只作建议的记账标签。无配置（开关关、
  提供方或模型为空、gate 缺失、provider 未注入）时整个参谋链路
  静默关闭，不报错、不阻塞主流程。
- 全程记账：建议内容（provider 原样返回）、采纳与否、落库结果
  都记进建议自带的 audit  trail；调用方把这条 trail 持久化后，
  建议的来龙去脉才有据可查。建议状态机：pending（待人工拍板）
  / refused_by_policy（门拒，永不进入待批）/ approved（人工已批，
  待走现行路径落库）/ rejected（人工驳回）。
- diff 待批：``build_pending_diff`` 只产出“拟改动”描述，不应用。
  门拒的建议没有 diff（返回 None），未批的 diff 标注
  awaiting_human_approval，已批的标注 approved_awaiting_application。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace

from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

#: AI 参谋总开关（默认关）。关时行为与今天逐字一致：不产建议。
AI_ADVISOR_ENABLED = env_int("AI_ADVISOR_ENABLED", 0, 0, 1)

#: 建议提供方（env 配置，如某网关 / 客户端名）；空 = 未配置 = 参谋关闭。
#: 本模块不解析、不校验、不据此构造任何客户端，只作记账标签。
AI_ADVISOR_PROVIDER_ENV = "AI_ADVISOR_PROVIDER"
#: 建议模型（env 配置）；空 = 未配置 = 参谋关闭；绝不写死具体模型。
AI_ADVISOR_MODEL_ENV = "AI_ADVISOR_MODEL"

#: 政策门：#1284 已合署，直接导入（防御性导入已退役）。
from backend.emby_server import ai_policy as _gate


def _cfg_provider() -> str:
    """env 里的提供方名（去空白；空 = 未配置）。"""
    return (os.getenv(AI_ADVISOR_PROVIDER_ENV) or "").strip()


def _cfg_model() -> str:
    """env 里的模型名（去空白；空 = 未配置）。"""
    return (os.getenv(AI_ADVISOR_MODEL_ENV) or "").strip()


def advisor_enabled() -> bool:
    """参谋链路是否开启（全仓读开关的唯一入口；测试 patch 本模块常量）。

    四者缺一即关闭：总开关、政策门模块、提供方配置、模型配置。
    """
    return (
        AI_ADVISOR_ENABLED > 0
        and _gate is not None
        and bool(_cfg_provider())
        and bool(_cfg_model())
    )


def _non_ai_sources(existing_evidence: dict | None) -> list:
    """从调用方已有的（非 AI）证据里抽来源名。

    与 identify_two_stage 的证据口径复用：evidence 带 ``sources``
    列表时直接用它；否则有 tmdb/imdb 身份即视作一份既有证据
    （来源名记 ``existing``，不编具体源名）。空证据 = 无非 AI 证据。
    """
    evidence = existing_evidence or {}
    sources = evidence.get("sources")
    if isinstance(sources, (list, tuple)):
        return [str(s) for s in sources if str(s).strip()]
    if evidence.get("tmdb_id") or evidence.get("imdb_id"):
        return ["existing"]
    return []


@dataclass
class Suggestion:
    """一条 AI 建议（纯数据，不写库）。

    ``raw`` 是 provider 原样返回的内容（记账用，原样保留）；
    ``audit`` 是本建议的事件 trail（请求 / 拍板 / 落库结果）。
    """

    item_key: str
    proposed_tmdb_id: str | None = None
    proposed_imdb_id: str | None = None
    ai_weight: float | None = None
    confidence_band: str | None = None
    status: str = "pending"
    reason: str = "ok"
    provider: str = ""
    model: str = ""
    raw: dict = field(default_factory=dict)
    audit: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """记账快照（调用方持久化这份，不要只记结论）。"""
        return {
            "item_key": self.item_key,
            "proposed_tmdb_id": self.proposed_tmdb_id,
            "proposed_imdb_id": self.proposed_imdb_id,
            "ai_weight": self.ai_weight,
            "confidence_band": self.confidence_band,
            "status": self.status,
            "reason": self.reason,
            "provider": self.provider,
            "model": self.model,
            "raw": dict(self.raw),
            "audit": [dict(e) for e in self.audit],
        }


def _audit(suggestion: Suggestion, event: str, detail: str = "") -> Suggestion:
    """给建议追加一条 audit 事件（返回同一对象，调用方负责持久化）。"""
    suggestion.audit.append({"event": event, "detail": detail})
    return suggestion


def request_suggestion(item_key, existing_evidence=None, provider=None):
    """向 AI 参谋要一条建议；链路关闭时静默返回 None。

    ``provider`` 是调用方注入的可调用对象
    ``provider(item_key, existing_evidence) -> dict | None``，返回的
    dict 可带 ``tmdb_id`` / ``imdb_id`` / ``ai_weight``。provider 未注入、
    返回空、抛异常时一律返回 None（记日志，不抛异常、不阻塞主流程）。

    门的裁决：gate_evidence 里 AI 来源恒为 ``ai``，非 AI 来源取自
    调用方已有证据；纯 AI 提名时门判 ``allowed=False``，建议状态记
    ``refused_by_policy``——它仍作为一条记账记录返回（调用方可查
    “AI 提过、门拒了”），但永远没有可用的 diff、永远不进入待批。
    """
    if not advisor_enabled():
        return None
    if provider is None:
        logger.debug("AI 参谋未注入 provider，静默关闭（item=%s）", item_key)
        return None
    try:
        raw = provider(item_key, existing_evidence or {})
    except Exception as exc:  # provider 坏了不能拖垮主流程
        logger.warning("AI 参谋 provider 失败，静默关闭（item=%s）: %s", item_key, exc)
        return None
    if not isinstance(raw, dict) or not raw:
        return None
    gate_evidence = {
        "ai_suggested": True,
        "sources": ["ai", *_non_ai_sources(existing_evidence)],
        "ai_weight": raw.get("ai_weight"),
    }
    verdict = _gate.evaluate_binding_evidence(gate_evidence)
    band = None
    classify = getattr(_gate, "classify_confidence", None)
    if callable(classify):
        try:
            band = classify(raw.get("ai_weight"))
        except Exception:
            band = None
    suggestion = Suggestion(
        item_key=str(item_key),
        proposed_tmdb_id=(
            str(raw["tmdb_id"]) if raw.get("tmdb_id") is not None else None
        ),
        proposed_imdb_id=(
            str(raw["imdb_id"]) if raw.get("imdb_id") is not None else None
        ),
        ai_weight=verdict.get("ai_weight"),
        confidence_band=band,
        status="pending" if verdict.get("allowed") else "refused_by_policy",
        reason=str(verdict.get("reason") or ""),
        provider=_cfg_provider(),
        model=_cfg_model(),
        raw=dict(raw),
    )
    _audit(suggestion, "requested",
           "gate=%s" % suggestion.reason)
    return suggestion


def approve_suggestion(suggestion, approver: str = "human") -> Suggestion:
    """人工拍板通过（只改建议状态，不写任何身份绑定）。

    仅 pending 可批；门拒的建议批不动（原样返回并记一条
    approve_blocked 事件），驳回过的建议不复批。
    """
    if suggestion is None:
        return None
    if suggestion.status != "pending":
        return _audit(suggestion, "approve_blocked",
                      "status=%s approver=%s" % (suggestion.status, approver))
    approved = replace(suggestion, status="approved")
    approved.audit = list(suggestion.audit)
    return _audit(approved, "approved", "approver=%s" % approver)


def reject_suggestion(suggestion, approver: str = "human") -> Suggestion:
    """人工驳回（只改建议状态，不写任何身份绑定）。"""
    if suggestion is None:
        return None
    if suggestion.status != "pending":
        return _audit(suggestion, "reject_blocked",
                      "status=%s approver=%s" % (suggestion.status, approver))
    rejected = replace(suggestion, status="rejected")
    rejected.audit = list(suggestion.audit)
    return _audit(rejected, "rejected", "approver=%s" % approver)


def build_pending_diff(suggestion) -> dict | None:
    """产出拟改动 diff（描述，不应用；门拒 / 已驳回无 diff）。

    落库应用不在本模块：已批的 diff 须由人工确认后、走现行
    identify 写库路径应用，应用结果用 ``record_outcome`` 记账。
    """
    if suggestion is None or suggestion.status not in ("pending", "approved"):
        return None
    return {
        "item_key": suggestion.item_key,
        "proposed_tmdb_id": suggestion.proposed_tmdb_id,
        "proposed_imdb_id": suggestion.proposed_imdb_id,
        "ai_weight": suggestion.ai_weight,
        "status": (
            "awaiting_human_approval"
            if suggestion.status == "pending"
            else "approved_awaiting_application"
        ),
        "note": "本 diff 仅描述拟改动，不应用；落库走现行 identify 写库路径。",
    }


def record_outcome(suggestion, outcome: str, detail: str = "") -> Suggestion:
    """记一条落库结果（``applied`` / ``not_applied`` / ``failed`` 等）。

    本函数只记账：调用方在现行路径里真正落库（或决定不落库）后，
    把结果与建议的 audit trail 对上，事后有据可查。
    """
    if suggestion is None:
        return None
    return _audit(suggestion, "outcome:%s" % outcome, detail)
