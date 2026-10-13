"""探险预告页「保密新大陆」占位卡：揭晓内容与日期的可配实现。

为什么做成能力而不是写死：
- 这张卡当初是写死的悬念占位（🌫️ ???「神秘新大陆 · 到日子自动揭晓」）。
  后台想把它变成真正的「到日子揭晓」运营位：揭晓前保密、到日子自动换内容。
- 所以这里只做一件事：把 SystemConfig 里那几个键读成前端可用的**公开视图**，
  并提供管理端写回去的函数。业务文案（标题 / 描述 / 链接）全在配置里，
  代码里只有字段名和默认回退——换一张卡、换一句文案都不用改代码。

| SystemConfig 键 | 含义 | 默认 |
|---|---|---|
| ``teaser_enabled`` | 总开关（关 = 探险预告页不显示这张卡） | 1 |
| ``teaser_reveal_at`` | 揭晓时间（服务器本地时间，ISO，如 2026-11-01T10:00；空 = 一直保密） | 空 |
| ``teaser_pre_title`` | 揭晓前标题 | ``? ? ?`` |
| ``teaser_pre_subtitle`` | 揭晓前副文案 | ``神秘新大陆 · 到日子自动揭晓`` |
| ``teaser_post_title`` | 揭晓后标题（空 = 即使到日子也继续保密，防空卡） | 空 |
| ``teaser_post_desc`` | 揭晓后描述 | 空 |
| ``teaser_post_link`` | 揭晓后跳转（/ 开头站内路由或 http(s) 外链） | 空 |

保密本分：公开视图在**揭晓前永远不下发**揭晓后内容（标题 / 描述 / 链接），
前端想偷看也拿不到。时间口径一律服务器本地 naive datetime（与日历接口的
``date_added`` 同一口径），不信任客户端时区。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

CONFIG_ENABLED = "teaser_enabled"
CONFIG_REVEAL_AT = "teaser_reveal_at"
CONFIG_PRE_TITLE = "teaser_pre_title"
CONFIG_PRE_SUBTITLE = "teaser_pre_subtitle"
CONFIG_POST_TITLE = "teaser_post_title"
CONFIG_POST_DESC = "teaser_post_desc"
CONFIG_POST_LINK = "teaser_post_link"

#: 写白名单：超出这 7 个键的一律拒绝（不是忽略，是 400 —— 配错了要让人知道）
WRITABLE_KEYS = (
    CONFIG_ENABLED,
    CONFIG_REVEAL_AT,
    CONFIG_PRE_TITLE,
    CONFIG_PRE_SUBTITLE,
    CONFIG_POST_TITLE,
    CONFIG_POST_DESC,
    CONFIG_POST_LINK,
)

DEFAULT_PRE_TITLE = "? ? ?"
DEFAULT_PRE_SUBTITLE = "神秘新大陆 · 到日子自动揭晓"

DESCRIPTIONS = {
    CONFIG_ENABLED: "探险预告页是否显示保密新大陆占位卡（关 = 整张卡不显示）",
    CONFIG_REVEAL_AT: "揭晓时间（服务器本地时间，ISO 如 2026-11-01T10:00；留空 = 一直保密）",
    CONFIG_PRE_TITLE: "揭晓前标题（默认 ? ? ?）",
    CONFIG_PRE_SUBTITLE: "揭晓前副文案（默认「神秘新大陆 · 到日子自动揭晓」）",
    CONFIG_POST_TITLE: "揭晓后标题（留空 = 即使到日子也继续保密，防止出现空卡）",
    CONFIG_POST_DESC: "揭晓后描述",
    CONFIG_POST_LINK: "揭晓后跳转（/ 开头站内路由或 http(s) 外链；留空则卡片不可点）",
}


def _truthy(raw: Any, default: bool) -> bool:
    value = str(raw or "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def _parse_reveal_at(raw: Any) -> Optional[datetime]:
    """解析揭晓时间：接受 YYYY-MM-DD / YYYY-MM-DDTHH:MM[:SS]；非法返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        # fromisoformat 不吃末尾的 Z，生产配置里不会出现，但防一手
        return datetime.fromisoformat(text.rstrip("Z"))
    except (ValueError, TypeError):
        return None


def _raw(db, key: str, default: str = "") -> str:
    from backend.integrations import store
    return store.get_value(db, key, default) if db is not None else default


def _is_revealed(enabled: bool, reveal_at: Optional[datetime],
                 post_title: str, now: datetime) -> bool:
    # 揭晓后标题为空也算未就绪：宁可继续保密，不给用户看一张空卡
    return bool(enabled and reveal_at is not None and now >= reveal_at and post_title)


def public_view(db, now: Optional[datetime] = None) -> Optional[dict]:
    """日历接口下发的公开视图（只读，供探险预告页渲染占位卡）。

    - 开关关闭 → None（前端不渲染这张卡）
    - 未到揭晓日（或没配日期 / 揭晓后内容没配）→ 保密视图：只有揭晓前文案
      和倒计时天数，**不含**揭晓后标题 / 描述 / 链接
    - 到日子 → 揭晓视图：标题 / 描述 / 链接（链接为空则为 None）
    """
    now = now or datetime.now()
    if not _truthy(_raw(db, CONFIG_ENABLED), True):
        return None
    reveal_at = _parse_reveal_at(_raw(db, CONFIG_REVEAL_AT))
    post_title = _raw(db, CONFIG_POST_TITLE).strip()
    if _is_revealed(True, reveal_at, post_title, now):
        link = _raw(db, CONFIG_POST_LINK).strip() or None
        return {
            "status": "revealed",
            "title": post_title,
            "description": _raw(db, CONFIG_POST_DESC).strip(),
            "link": link,
        }
    countdown_days = (reveal_at.date() - now.date()).days if reveal_at else None
    return {
        "status": "secret",
        "title": _raw(db, CONFIG_PRE_TITLE).strip() or DEFAULT_PRE_TITLE,
        "subtitle": _raw(db, CONFIG_PRE_SUBTITLE).strip() or DEFAULT_PRE_SUBTITLE,
        "countdown_days": countdown_days,
    }


def read_admin(db) -> dict:
    """管理端读：全部 7 个字段 + 派生状态（供后台展示「保密中 / 已揭晓 / 已关闭」）。"""
    now = datetime.now()
    enabled = _truthy(_raw(db, CONFIG_ENABLED), True)
    reveal_at = _parse_reveal_at(_raw(db, CONFIG_REVEAL_AT))
    post_title = _raw(db, CONFIG_POST_TITLE).strip()
    revealed = _is_revealed(enabled, reveal_at, post_title, now)
    return {
        "enabled": enabled,
        "reveal_at": _raw(db, CONFIG_REVEAL_AT).strip(),
        "pre_title": _raw(db, CONFIG_PRE_TITLE).strip(),
        "pre_subtitle": _raw(db, CONFIG_PRE_SUBTITLE).strip(),
        "post_title": post_title,
        "post_desc": _raw(db, CONFIG_POST_DESC).strip(),
        "post_link": _raw(db, CONFIG_POST_LINK).strip(),
        "status": "off" if not enabled else ("revealed" if revealed else "secret"),
        "countdown_days": (reveal_at.date() - now.date()).days if reveal_at else None,
    }


def _validate_link(link: str) -> None:
    text = (link or "").strip()
    if not text:
        return
    if text.startswith("/") or text.startswith("http://") or text.startswith("https://"):
        return
    raise ValueError("跳转链接只接受 / 开头的站内路由或 http(s) 外链")


def write_admin(db, values: dict) -> dict:
    """管理端写：白名单校验 + 落库，返回写完后的管理视图。

    未知 key / 非法日期 / 非法链接直接抛 ValueError（调用方转 400），
    不静默忽略——配错了要让管理员当场知道。
    """
    from backend.integrations import store

    values = values or {}
    unknown = [k for k in values if k not in WRITABLE_KEYS]
    if unknown:
        raise ValueError(f"未知配置项: {', '.join(unknown)}")

    payload: dict[str, str] = {}
    if CONFIG_ENABLED in values:
        payload[CONFIG_ENABLED] = "1" if _truthy(values[CONFIG_ENABLED], True) else "0"
    if CONFIG_REVEAL_AT in values:
        raw = str(values[CONFIG_REVEAL_AT] or "").strip()
        if raw and _parse_reveal_at(raw) is None:
            raise ValueError("揭晓时间格式不对，用 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM")
        payload[CONFIG_REVEAL_AT] = raw
    for key in (CONFIG_PRE_TITLE, CONFIG_PRE_SUBTITLE, CONFIG_POST_TITLE,
                CONFIG_POST_DESC, CONFIG_POST_LINK):
        if key in values:
            payload[key] = str(values[key] if values[key] is not None else "").strip()
    if CONFIG_POST_LINK in payload:
        _validate_link(payload[CONFIG_POST_LINK])

    if payload:
        store.write_values(db, payload, {k: DESCRIPTIONS[k] for k in payload})
        db.commit()
        db.expire_all()
    return read_admin(db)
