"""AI 参谋政策（清单第 17 项，FMBY #17；并入 #15/#17 的政策落码）。

对应魂 1（身份与证据先行）：AI 只许提名、不许定案。

- MAX_AI_WEIGHT = 0.2：AI 建议在任何一次绑定判定里的权重上限两成，
  写死在政策里（不是可调 env——调了就不是政策）。
- REQUIRE_NON_AI_EVIDENCE = True：无非 AI 证据不许定——纯 AI 提名
  的绑定一律不落库，走现行审查 / none 终态。
- STRONG_THRESHOLD = 0.9 / WEAK_THRESHOLD = 0.65：强弱阈值；强 =
  可直接绑定，弱 = 须复核，低于弱阈 = 证据不足。

证据契约：绑定证据（identify_two_stage.extract_binding 产出的
evidence dict）可带 AI 标记——``ai_suggested``（bool）与 ``sources``
（来源名列表，如 ["tmdb", "ai"]）及 ``ai_weight``（float）。不带任何
AI 标记的证据视为非 AI 证据（现行证据天然如此，政策门对现行行为
零影响）。

总开关：env ``ENRICH_AI_POLICY_ENABLED``（默认 0=关）。关时政策门
不启用，identify 行为与今天逐字一致；开时 apply_identity 在写身份
前过政策门。本模块是纯函数 + env 读取，不依赖任何 backend 模块，
identify_two_stage 可安全导入。
"""

from __future__ import annotations

import logging

from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

#: AI 建议权重上限（两成），写死在政策里。
MAX_AI_WEIGHT = 0.2
#: 无非 AI 证据不许定（纯 AI 提名不落库）。
REQUIRE_NON_AI_EVIDENCE = True
#: 强阈值：置信度 >= 0.9 可直接绑定。
STRONG_THRESHOLD = 0.9
#: 弱阈值：0.65 <= 置信度 < 0.9 须复核；低于 0.65 为证据不足。
WEAK_THRESHOLD = 0.65

#: 视为 AI 来源的来源名（sources 列表里出现即算 AI 证据）。
AI_SOURCE_NAMES = frozenset({"ai", "ai_suggest", "ai_suggestion"})

#: AI 政策门总开关（默认关）。启动时读一次，热路径不碰 env。
AI_POLICY_ENABLED = env_int("ENRICH_AI_POLICY_ENABLED", 0, 0, 1)


def ai_policy_enabled() -> bool:
    """AI 政策门是否开启（全仓读开关的唯一入口；测试 patch 本模块常量）。"""
    return AI_POLICY_ENABLED > 0


def cap_ai_weight(weight) -> float | None:
    """把 AI 权重钳制到政策上限；None / 非数字返回 None（不编数字）。"""
    if weight is None:
        return None
    try:
        value = float(weight)
    except (TypeError, ValueError):
        return None
    if value != value:  # NaN 不进政策计算
        return None
    return max(0.0, min(MAX_AI_WEIGHT, value))


def classify_confidence(score) -> str:
    """置信度分档：strong / weak / insufficient（None / 非数字 = 不足）。"""
    if score is None:
        return "insufficient"
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "insufficient"
    if value != value:
        return "insufficient"
    if value >= STRONG_THRESHOLD:
        return "strong"
    if value >= WEAK_THRESHOLD:
        return "weak"
    return "insufficient"


def evaluate_binding_evidence(evidence: dict | None) -> dict:
    """评定一次绑定证据（纯函数，不写库）。

    返回 {"allowed", "ai_weight", "reason"}：

    - 不带 AI 标记的证据：allowed=True（现行证据天然如此）；
    - AI 与非 AI 证据并存：allowed=True，ai_weight 钳制到 0.2；
    - 纯 AI 证据（ai_suggested 且无非 AI 来源，或 sources 全是 AI）：
      REQUIRE_NON_AI_EVIDENCE 下 allowed=False，调用方不落库。
    """
    evidence = evidence or {}
    sources = evidence.get("sources")
    if sources is None:
        sources = evidence.get("ai_sources")
    sources = list(sources or [])
    ai_suggested = bool(evidence.get("ai_suggested"))
    ai_present = ai_suggested or any(s in AI_SOURCE_NAMES for s in sources)
    if not ai_present:
        return {"allowed": True, "ai_weight": None, "reason": "ok"}
    non_ai_sources = [s for s in sources if s not in AI_SOURCE_NAMES]
    capped = cap_ai_weight(evidence.get("ai_weight"))
    if REQUIRE_NON_AI_EVIDENCE and not non_ai_sources:
        return {
            "allowed": False,
            "ai_weight": capped,
            "reason": "ai_only_refused",
        }
    return {"allowed": True, "ai_weight": capped, "reason": "ok_capped"}
