"""能力：探险世界 · 服主告示牌

世界入口告示牌显示的服主留言，管理员在后台填写；panel home 接口透出给前端。
留言留空则告示牌不显示。配置改完 1 分钟内生效（配置缓存 TTL 60 秒）。
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from backend.integrations import store

SPEC = {
    "title": "服主告示牌",
    "desc": "世界入口告示牌的留言内容与落款，保存后 1 分钟内在世界地图入口显示。",
    "group": "探险世界",
    "docs_hint": "留言留空则告示牌不显示。",
    "fields": [
        {"key": "world_board_text", "label": "告示牌留言", "type": "str", "default": "",
         "placeholder": "例如：本周五晚八点，服主请大家看《XXX》首播！",
         "hint": "支持多行，最长 500 字，留空则告示牌不显示"},
        {"key": "world_board_signature", "label": "落款", "type": "str", "default": "服主",
         "placeholder": "服主"},
    ],
}

_FIELD_KEYS = [f["key"] for f in SPEC["fields"]]
_FIELD_DEFAULTS = {f["key"]: (f.get("default") or "") for f in SPEC["fields"]}

MAX_TEXT_LEN = 500


def read(db: Session) -> dict:
    """一次查询取回告示牌配置"""
    return store.read_values(db, _FIELD_KEYS, _FIELD_DEFAULTS)


def public_info(db: Session) -> dict:
    """给前端的公开信息（只含能公开的字段，没有任何凭据）"""
    values = read(db)
    text = str(values.get("world_board_text") or "").strip()[:MAX_TEXT_LEN]
    return {
        "text": text,
        "signature": str(values.get("world_board_signature") or "").strip() or "服主",
        "visible": bool(text),
    }
