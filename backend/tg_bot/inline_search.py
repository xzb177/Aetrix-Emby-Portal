# -*- coding: utf-8 -*-
"""TG Bot 内联搜片（借鉴清单第 6 项）：任意聊天里输入 @bot 片名，
Telegram 弹出站内片库候选卡片，点一张把片名卡发到聊天里。

硬规矩（与 queries.py 同源）：
- **守播放门禁（#1341）**：内联结果只给片名 / 年份 / 类型 / 海报缩略，
  绝不下发任何播放地址、直链、中继链接或令牌；点开卡片发出的也只是
  片名卡 + 「打开站内搜索片名」的引导。未绑定用户能搜到标题（与站内
  浏览口径一致），但只回绑定引导、不给结果。
- **搜索口径复用站内现行搜索**：候选预筛与精排走
  ``backend.emby_server.search`` 的 ``search_variants`` / ``rank_items``
  （与 /Search/Hints 同一套），可见性同口径：is_hidden 排除、软删排除
  （显式 deleted_at IS NULL，与 nl_search.search_items 的写法一致）、
  merged 副本排除、``library_scope`` 按人算可见库范围。
- 只读：本模块不建行、不改行、不提交事务。
- 海报缩略走 ``emby_server.api._image_url``（与详情页同源）：远程图直用，
  本地图拼 ``{site_url}/emby/Items/{guid}/Images/Primary``；拼不出公网
  http 地址时宁可无图（纯文本结果），不为内联现造图片管线。
"""
from __future__ import annotations

import logging

from sqlalchemy import or_

from backend import library_scope
from backend.emby_server import models as em
from backend.emby_server.search import CANDIDATE_LIMIT, rank_items, search_variants
from backend.integrations import store
from backend.tg_bot import identity, layout, policy, sender

logger = logging.getLogger(__name__)

#: 内联结果条数上限（Telegram 规格 ≤50；卡片场景取前 15 条足矣）
RESULT_LIMIT = 15
#: 查询词最短长度：短于 2 个字符不查库（单字太泛、纯属打字中间态）
MIN_QUERY_LENGTH = 2
#: 内联只出「作品」卡：电影 / 剧集；单集不单独成卡（搜剧名出整剧）
_INLINE_TYPES = ("movie", "series")
_TYPE_TEXT = {"movie": "电影", "series": "剧集"}
_CACHE_HIT = 30
_CACHE_GUIDE = 5


def _search_clauses(term: str) -> list:
    """SQL 预筛子句：与 search_api._search_clauses 同一列族（标题族+别名族+拼音）。"""
    clauses = []
    for variant in search_variants(term):
        like = f"%{variant}%"
        clauses.extend([
            em.MediaItem.name.ilike(like),
            em.MediaItem.original_title.ilike(like),
            em.MediaItem.sort_name.ilike(like),
            em.MediaItem.aliases.ilike(like),
            em.MediaItem.pinyin_full.ilike(like),
            em.MediaItem.pinyin_initials.ilike(like),
        ])
    return clauses


def search_items(db, user, term: str, limit: int = RESULT_LIMIT) -> list:
    """按站内现行搜索口径查片库，返回精排后的前 limit 条 MediaItem。"""
    query = db.query(em.MediaItem).filter(em.MediaItem.is_hidden == False)  # noqa: E712
    # 软删排除：显式写明，与 nl_search.search_items 同口径（不单靠全局 ORM 钩子）
    query = query.filter(em.MediaItem.deleted_at.is_(None))
    # merged 副本不单独成卡（与条目列表口径一致）
    query = query.filter(em.MediaItem.merged_into_id.is_(None))
    query = query.filter(em.MediaItem.item_type.in_(_INLINE_TYPES))
    # 媒体库可见范围按人算（读失败 fail-closed 返回空集 → 无结果）
    query = library_scope.scope_query(
        query, library_scope.effective_ids_safe(db, user))
    clauses = _search_clauses(term)
    if not clauses:
        return []
    query = query.filter(or_(*clauses))
    rows = query.order_by(em.MediaItem.sort_name.asc()).limit(CANDIDATE_LIMIT).all()
    return rank_items(rows, term)[:limit]


def _thumb_url(db, item) -> str | None:
    """海报缩略地址（详情页同源口径）；拿不到公网 http 地址返回 None。"""
    from backend.emby_server.api import _image_url

    base = (store.get_value(db, "site_url", "") or "").strip().rstrip("/")
    url = _image_url(base, item)
    if url and url.startswith("http"):
        return url
    return None


def _site_markup(db) -> dict | None:
    """卡片下挂「打开站内」按钮（只到站内首页，不带任何播放地址）。"""
    base = (store.get_value(db, "site_url", "") or "").strip().rstrip("/")
    if not base:
        return None
    return {"inline_keyboard": [[{"text": "🌐 打开站内", "url": base}]]}


def _build_result(db, item, markup) -> dict:
    type_text = _TYPE_TEXT.get(item.item_type, item.item_type)
    year = item.production_year
    desc_parts = [type_text]
    if year:
        desc_parts.append(str(year))
    headline = f"{item.name}（{year}）" if year else str(item.name)
    text = (
        f"🎬 <b>{layout.esc(headline)}</b>\n"
        f"· 🏷️ 类型 | {layout.esc(type_text)}\n"
        "在站内片库里有这部，打开站内搜片名即可观看。"
    )
    result = {
        "type": "article",
        "id": f"m{item.id}",
        "title": str(item.name),
        "description": " · ".join(desc_parts),
        "input_message_content": {"message_text": text, "parse_mode": "HTML"},
    }
    thumb = _thumb_url(db, item)
    if thumb:
        result["thumb_url"] = thumb
    if markup:
        result["reply_markup"] = markup
    return result


def handle_inline_query(db, iq: dict, cfg: dict) -> None:
    """处理一条 inline_query update：任何分支都必须 answer，否则客户端一直转圈。"""
    iq_id = iq.get("id")
    if not iq_id:
        return
    tg_user = iq.get("from") or {}
    tg_id = tg_user.get("id")
    term = (iq.get("query") or "").strip()

    def _answer(results, cache_time=_CACHE_GUIDE, **kw):
        sender.answer_inline_query(
            db, iq_id, results, cache_time=cache_time, is_personal=True, **kw)

    # 开关关闭：静默回空（内联场景不刷屏解释）
    if not policy.is_inline_search_enabled(cfg):
        _answer([])
        return

    # 限流：复用 router 的用户级限流实现（一套实现，不另造），
    # 键名独立于命令；超限回空，客户端缓存几秒后可再试。
    if tg_id:
        from backend.tg_bot import router

        if not router._user_cmd_allow(
                int(tg_id), "inline_query", policy.rate_limit_seconds(cfg)):
            _answer([], cache_time=3)
            return

    # 未绑定：不给片库结果，只给一条跳私聊绑定的引导（switch_pm）。
    # 选空结果+switch_pm 而不是塞一张引导卡：引导卡点开会把绑定提示
    # 发进当前聊天（可能是在别人群里），徒增噪音；switch_pm 是
    # Telegram 给这类「先去私聊办手续」场景的正道。
    user = identity.resolve(db, int(tg_id)) if tg_id else None
    if user is None:
        _answer([], switch_pm_text="🔗 先绑定站内账号，才能搜片",
                switch_pm_parameter="bind")
        return

    # 空查询 / 太短：不查库，回一句输入提示
    if len(term) < MIN_QUERY_LENGTH:
        _answer([], switch_pm_text="🎬 输入片名，在站内片库里搜",
                switch_pm_parameter="search")
        return

    items = search_items(db, user, term)
    markup = _site_markup(db)
    _answer([_build_result(db, it, markup) for it in items],
            cache_time=_CACHE_HIT)
