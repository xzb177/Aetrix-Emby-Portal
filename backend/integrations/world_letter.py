"""能力：探险世界 · 昨日来信

发现页「昨日来信」的附言与今日推荐，管理员在能力中心填写；
``GET /api/panel/letter``（以及 ``/home`` 里的 letter 区块）透出给前端。
个人观影统计仍由 panel 按播放会话计算，本模块不读、不改。

空配置（``letter_enabled`` 默认开）：

- ``letter_teaser`` 留空 → ``teaser`` 回落内置附言，管理员没填之前观感与改前硬编码一致；
- ``letter_recommendation`` 留空 → ``recommendation`` 为 ``""``，前端不显示「今日推荐」。

``letter_enabled`` 关掉时个人统计照常返回，但 ``teaser`` 与 ``recommendation`` 都强制为
``""``（不再回落默认附言）。保存后下一次读取即生效（``store.read_values`` 直查）。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from backend.integrations import store

# 与改前 panel_letter 硬编码一致；附言留空时继续用这句，避免空配置改观感。
FALLBACK_TEASER = "今日封存已送达，去拆盲盒吧 📦"
MAX_TEASER_LEN = 200
MAX_RECOMMENDATION_LEN = 500

SPEC = {
    "title": "昨日来信",
    "desc": "发现页昨日来信的附言与今日推荐。附言留空用默认文案；今日推荐留空则前端不显示。",
    "group": "探险世界",
    "docs_hint": "关掉开关后个人观影统计仍返回，附言与今日推荐变为空（不回落默认附言）。",
    "fields": [
        {"key": "letter_enabled", "label": "启用附言与今日推荐", "type": "bool", "default": "true",
         "hint": "关闭后昨日来信仍返回观影统计，但附言与今日推荐留空"},
        {"key": "letter_teaser", "label": "附言", "type": "str", "default": "",
         "placeholder": FALLBACK_TEASER,
         "hint": "最长 200 字。留空则使用默认附言「今日封存已送达，去拆盲盒吧 📦」"},
        {"key": "letter_recommendation", "label": "今日推荐", "type": "str", "default": "",
         "placeholder": "例如：今晚适合重温《XXX》",
         "hint": "最长 500 字。留空则前端不显示「今日推荐」"},
    ],
}

_FIELD_KEYS = [f["key"] for f in SPEC["fields"]]
_FIELD_DEFAULTS = {f["key"]: (f.get("default") or "") for f in SPEC["fields"]}

_TRUE_VALUES = {"1", "true", "yes", "on"}


def read(db: Session) -> dict:
    """一次查询取回昨日来信配置"""
    return store.read_values(db, _FIELD_KEYS, _FIELD_DEFAULTS)


def _flag_on(raw: object, default: bool = True) -> bool:
    if isinstance(raw, bool):
        return raw
    text = str(raw if raw is not None else "").strip().lower()
    if not text:
        return default
    return text in _TRUE_VALUES


def _clip(raw: object, limit: int) -> str:
    return str(raw or "").strip()[:limit]


def public_info(db: Session) -> dict:
    """给前端的公开文案：``{teaser, recommendation, enabled}``。

    长度在这里截断。关闭开关时两段都是空串，不回落默认附言。
    """
    values = read(db)
    enabled = _flag_on(values.get("letter_enabled"), True)
    if not enabled:
        return {"teaser": "", "recommendation": "", "enabled": False}
    teaser = _clip(values.get("letter_teaser"), MAX_TEASER_LEN) or FALLBACK_TEASER
    recommendation = _clip(values.get("letter_recommendation"), MAX_RECOMMENDATION_LEN)
    return {"teaser": teaser, "recommendation": recommendation, "enabled": True}
