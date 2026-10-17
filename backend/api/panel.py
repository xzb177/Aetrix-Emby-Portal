"""v4 探险家日志面板 · 后端 API

新增 backend/api/panel.py，前缀 /api/panel/*。
全部接口需登录（复用 get_current_user，不许第二套鉴权）。
返回体统一 {code, msg, data}。

特性开关：PANEL_V4_ENABLED（默认开；关则接口返回 403 disabled）。
只做加法，不改现有 API。
"""
from __future__ import annotations

import json
import logging
import os
import random
import threading
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models
from backend.database import get_db
from backend.api.user import get_current_user
from backend.api.admin_core import get_current_admin
from backend.ratelimit import check_rate_limit

logger = logging.getLogger("aetrix.panel")

router = APIRouter(prefix="/api/panel", tags=["面板-v4"])

PET_STAGES_KEY = "panel_pet_stages"

# 馆灵阶段出厂默认（与 GET 回退口径一致；用户端回退处复用此常量）
PET_STAGES_DEFAULT = [
    {"name": "蛋", "icon": "🥚", "hours": 0},
    {"name": "夜枭", "icon": "🦉", "hours": 50},
    {"name": "守夜者", "icon": "🦅", "hours": 200},
    {"name": "星语者", "icon": "🦄", "hours": 500},
    {"name": "馆灵", "icon": "🐉", "hours": 1000},
]


def _read_pet_stages(db: Session) -> list:
    """读馆灵阶段配置；未配置/非法时回退到出厂默认。"""
    try:
        from backend.servers import config_value
        raw = config_value(db, PET_STAGES_KEY, "")
        stages = json.loads(raw) if raw else []
    except Exception:  # noqa: BLE001
        stages = []
    return stages if stages else [dict(s) for s in PET_STAGES_DEFAULT]


def _validate_pet_stages(stages) -> list:
    """校验管理员提交的阶段配置，返回规范化后的 list；非法抛 HTTPException(400)。"""
    if not isinstance(stages, list) or not stages:
        raise HTTPException(status_code=400, detail="stages 必须是非空数组")
    norm = []
    for i, st in enumerate(stages):
        if not isinstance(st, dict):
            raise HTTPException(status_code=400, detail=f"stages[{i}] 必须是对象")
        name = st.get("name")
        icon = st.get("icon", "")
        hours = st.get("hours")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(status_code=400, detail=f"stages[{i}].name 必须是非空字符串")
        if not isinstance(icon, str):
            raise HTTPException(status_code=400, detail=f"stages[{i}].icon 必须是字符串")
        if isinstance(hours, bool) or not isinstance(hours, (int, float)) or hours < 0:
            raise HTTPException(status_code=400, detail=f"stages[{i}].hours 必须是非负数")
        norm.append({"name": name.strip(), "icon": icon, "hours": hours})
    return norm


admin_router = APIRouter(prefix="/api/admin/panel", tags=["管理后台-面板v4"])


class PetStagesIn(BaseModel):
    stages: list


@admin_router.get("/pet-stages")
def admin_pet_stages_get(
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """管理后台：读馆灵阶段配置（未配置时返回出厂默认）。"""
    return {"stages": _read_pet_stages(db)}


@admin_router.put("/pet-stages")
def admin_pet_stages_put(
    body: PetStagesIn,
    current_admin: models.WebUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """管理后台：写馆灵阶段配置（JSON 数组，hours 为累计观影小时阈值）。"""
    from backend.servers import set_config
    stages = _validate_pet_stages(body.stages)
    set_config(db, PET_STAGES_KEY, json.dumps(stages, ensure_ascii=False), "馆灵阶段配置（管理后台）")
    db.commit()
    return {"stages": stages}


def _panel_enabled() -> bool:
    return os.getenv("PANEL_V4_ENABLED", "1") != "0"


def _guard():
    if not _panel_enabled():
        raise HTTPException(status_code=403, detail="面板 v4 未启用")


def _ok(data) -> dict:
    return {"code": 0, "msg": "ok", "data": data}


def _greeting() -> str:
    # 生产服务器本地时间为 UTC，直接用 datetime.now() 会把北京时间错位 8 小时
    # （2026-10-15 生产验收实证：北京时间 19 点显示"上午好"）。
    # 问候语纯展示，按 Asia/Shanghai 取小时；zoneinfo 不可用时回退旧行为。
    # 注意：签到"今天"边界（backend/api/economy.py:96）等涉账口径不在此改，
    # 如需一并对齐需用户拍板。
    try:
        h = datetime.now(ZoneInfo("Asia/Shanghai")).hour
    except Exception:  # noqa: BLE001 — 缺 tzdata 等极端情况回退
        h = datetime.now().hour
    if h < 5:
        return "夜深了"
    if h < 9:
        return "早上好"
    if h < 12:
        return "上午好"
    if h < 14:
        return "中午好"
    if h < 18:
        return "下午好"
    return "晚上好"


def _now_local_naive() -> datetime:
    """返回 Asia/Shanghai 的 naive 本地时间，仅作展示用（问候语/日期标签）。

    注意：不要拿它直接去比较 date_added 等入库时间列——那些列由 scanner
    以容器本地时间（UTC）naive 写入；做"今日"窗口比较请用
    _today_start_utc_naive()，否则每天 00:00–08:00（CST）入库的条目会被
    算成"昨天"（2026-10-16 生产实证）。
    """
    try:
        return datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
    except Exception:  # noqa: BLE001 — 缺 tzdata 等极端情况回退
        return datetime.now()


def _today_start_utc_naive() -> datetime:
    """上海自然日 00:00，换算成 UTC naive——与入库时间存储帧对齐。

    MediaItem.date_added 由 scanner 以容器本地时间（UTC）naive 写入；
    直接用 Shanghai-naive 的 00:00 去比较，会把每天 00:00–08:00（CST）
    入库的条目排除在"今日"之外。返回的起点已是 UTC 帧，可直接与
    date_added 比较。
    """
    try:
        now_sh = datetime.now(ZoneInfo("Asia/Shanghai"))
    except Exception:  # noqa: BLE001 — 缺 tzdata 等极端情况回退
        now_sh = datetime.now().astimezone()
    start_sh = now_sh.replace(hour=0, minute=0, second=0, microsecond=0)
    return start_sh.astimezone(timezone.utc).replace(tzinfo=None)


def _today_parcel_items(db: Session, limit: int = 5) -> list:
    """今日封存候选（/parcels 与 /today 的 parcel_count 同口径）。

    口径：今日整部新入库的 movie/series ＋ 今日有新 episode 入库的 series
    （同一部剧只出现一次）；按"今日最新动态时间"倒序；上限 limit。
    合并副本（merged_into_id 非空）不计入；软删除行由 ORM 全局钩子过滤。
    """
    from backend.emby_server import models as em

    start = _today_start_utc_naive()
    try:
        whole = db.query(em.MediaItem).filter(
            em.MediaItem.date_added >= start,
            em.MediaItem.item_type.in_(["movie", "series"]),
            em.MediaItem.merged_into_id.is_(None),
        ).all()
        ep_rows = (
            db.query(em.MediaItem.series_id, func.max(em.MediaItem.date_added))
            .filter(
                em.MediaItem.date_added >= start,
                em.MediaItem.item_type == "episode",
                em.MediaItem.series_id.isnot(None),
            )
            .group_by(em.MediaItem.series_id)
            .all()
        )
    except Exception:  # noqa: BLE001
        logger.exception("parcels 候选查询失败")
        return []
    ep_latest = {sid: mx for sid, mx in ep_rows if sid is not None}
    series_by_id: dict = {}
    if ep_latest:
        try:
            for s in db.query(em.MediaItem).filter(
                em.MediaItem.id.in_(list(ep_latest.keys())),
                em.MediaItem.merged_into_id.is_(None),
            ).all():
                series_by_id[s.id] = s
        except Exception:  # noqa: BLE001
            logger.exception("parcels 剧集回查失败")
    merged: dict = {}
    for it in whole:
        merged[it.id] = (it.date_added or start, it)
    for sid, s in series_by_id.items():
        key = ep_latest.get(sid) or start
        prev = merged.get(sid)
        if prev is None or key > prev[0]:
            merged[sid] = (key, s)
    ordered = sorted(merged.values(), key=lambda t: t[0], reverse=True)
    return [it for _, it in ordered[:limit]]


def _streak(db: Session, user_id: int) -> int:
    rec = db.query(models.CheckinRecord).filter_by(user_id=user_id).order_by(
        models.CheckinRecord.checkin_date.desc()).first()
    return rec.streak if rec and rec.streak else 0


def _emby_base_url(db: Session) -> str:
    """复用现有深链能力：Emby 服务器地址"""
    try:
        from backend.emby_server.portal import resolve_emby_base_url
        return resolve_emby_base_url(db) or ""
    except Exception:  # noqa: BLE001
        return ""


def _item_deep_link(db: Session, item_id: int) -> str:
    base = _emby_base_url(db)
    if not base:
        return ""
    return f"{base}/web/index.html#!/item?id={item_id}"


# ==================== 1. GET /api/panel/today ====================

@router.get("/today")
def panel_today(
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    _guard()
    now = _now_local_naive()
    # r35：PlaybackSession.start_time 是 UTC-naive 入库（compat_routes._upsert_session
    # 以容器本地时间写入），此前拿上海-naive 的 00:00 去比 → 每天 00:00–08:00（CST）
    # 「今日观影人数」漏计（窗口超前 8 小时），08:00 之后又把昨天 08:00–24:00 的
    # 观看者算进来（窗口滞后 8 小时）。改走上海自然日→UTC 帧的起点。
    today_start = _today_start_utc_naive()

    from backend.emby_server import models as em

    # 今日封存候选（与 /parcels 同口径：整部新入库 + 今日有新集的剧）
    try:
        parcel_items = _today_parcel_items(db, limit=5)
        parcel_ids = [it.id for it in parcel_items]
    except Exception:  # noqa: BLE001
        logger.warning("today 封存候选查询失败", exc_info=True)
        parcel_ids = []
    unsealed = 0
    if parcel_ids:
        unsealed = db.query(func.count(models.PanelUnseal.id)).filter(
            models.PanelUnseal.user_id == user.id,
            models.PanelUnseal.item_id.in_(parcel_ids),
        ).scalar() or 0

    streak_days = _streak(db, user.id)

    # 今日观影人数（P-6 社交证明：只报聚合数，不露个人）
    try:
        today_viewers = (
            db.query(em.PlaybackSession.user_id)
            .filter(
                em.PlaybackSession.start_time >= today_start,
                em.PlaybackSession.user_id.isnot(None),
            )
            .distinct()
            .count()
        ) or 0
    except Exception:  # noqa: BLE001
        logger.warning("panel_today today_viewers failed", exc_info=True)
        today_viewers = 0

    # 连签里程碑临界（P-2：里程碑与提醒窗口均走后台配置）
    try:
        from backend.integrations import world_streak

        _scfg = world_streak.read(db)
        _nm = world_streak.next_milestone(
            streak_days, world_streak.parse_milestones(_scfg.get("streak_milestones", ""))
        )
        streak_milestone = (
            {
                "next": _nm[0],
                "remaining": _nm[1],
                "warn_within": int(_scfg.get("streak_warn_within") or 1),
            }
            if _nm
            else None
        )
    except Exception:  # noqa: BLE001
        logger.warning("panel_today streak_milestone failed", exc_info=True)
        streak_milestone = None

    # 今日一句话（P-7：时段文案池按日轮换，同日稳定）
    try:
        from backend.integrations import world_copy

        _h = now.hour
        _daypart = (
            "dawn" if 5 <= _h < 9 else "day" if 9 <= _h < 17 else "dusk" if 17 <= _h < 19 else "night"
        )
        daypart_line = world_copy.pick_line(
            world_copy.read(db).get("daypart_lines", ""), _daypart, now.toordinal()
        )
    except Exception:  # noqa: BLE001
        logger.warning("panel_today daypart_line failed", exc_info=True)
        daypart_line = None

    return _ok({
        "greeting": _greeting(),
        "date_label": now.strftime("%Y-%m-%d"),
        "weekday": ["一", "二", "三", "四", "五", "六", "日"][now.weekday()],
        "parcel_count": len(parcel_ids),
        "unsealed_count": unsealed,
        "streak_days": streak_days,
        "letter_readable": True,  # 每日一封每天都可读
        "nickname": getattr(user, "nickname", None) or user.username,
        "today_viewers": today_viewers,
        "streak_milestone": streak_milestone,
        "daypart_line": daypart_line,
    })


# ==================== 2. GET /api/panel/parcels ====================

@router.get("/parcels")
def panel_parcels(
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    _guard()
    items = _today_parcel_items(db, limit=5)

    parcel_id_list = [it.id for it in items]
    unsealed_ids = {r[0] for r in db.query(models.PanelUnseal.item_id).filter(
        models.PanelUnseal.user_id == user.id,
        models.PanelUnseal.item_id.in_(parcel_id_list)).all()} if parcel_id_list else set()

    # r20: base URL 循环外解析一次（原逐条调 resolve_emby_base_url，5 条=5 次重复查询）
    base_url = _emby_base_url(db)
    parcels = []
    for it in items:
        parcels.append({
            "id": it.id,
            "name": it.name,
            "item_type": it.item_type,
            "production_year": it.production_year,
            "video_resolution": getattr(it, "video_resolution", None),
            "added_at": it.date_added.isoformat() if it.date_added else None,
            "unsealed": it.id in unsealed_ids,
            "deep_link": f"{base_url}/web/index.html#!/item?id={it.id}" if base_url else "",
        })
    return _ok({"parcels": parcels})


# ==================== 3. POST /api/panel/parcels/{item_id}/unseal ====================

@router.post("/parcels/{item_id}/unseal")
def panel_unseal(
    item_id: int,
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    _guard()
    # 幂等：已拆直接返回
    exists = db.query(models.PanelUnseal).filter_by(
        user_id=user.id, item_id=item_id).first()
    if exists:
        return _ok({"unsealed": True, "already": True})

    from backend.emby_server import models as em
    item = db.query(em.MediaItem).filter_by(id=item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="条目不存在")
    try:
        db.add(models.PanelUnseal(user_id=user.id, item_id=item_id))
        db.commit()
        already = False
    except IntegrityError:  # 并发重复拆：UNIQUE 冲突，重查确认后返回 already=True
        db.rollback()
        already = db.query(models.PanelUnseal).filter_by(
            user_id=user.id, item_id=item_id).first() is not None
    except Exception:  # noqa: BLE001
        db.rollback()
        raise  # 非冲突异常不再吞成假成功
    return _ok({
        "unsealed": True,
        "already": already,
        "item": {
            "id": item.id,
            "name": item.name,
            "item_type": item.item_type,
            "production_year": item.production_year,
            "deep_link": _item_deep_link(db, item.id),
        },
    })


# ==================== 4. GET /api/panel/pulse ====================

def _evaluate_achievements_async(user_id: int) -> None:
    """后台线程跑成就评估（幂等）。

    GET /collector 本身不再写库：评估在请求外异步执行，解锁结果通过
    achievements[].unlocked 在下次请求可见（最终一致）。
    """
    try:
        from backend.database import SessionLocal
        db2 = SessionLocal()
        try:
            from backend.panel_achievements import evaluate
            evaluate(user_id, db2)
        finally:
            db2.close()
    except Exception:  # noqa: BLE001
        logger.exception("成就后台评估失败")


def _derive_activity(db: Session, health: dict | None = None) -> list:
    """推导版后台活动文案（worker 埋点补上后无缝替换为读 panel_activity 表）

    health 由调用方传入（panel_pulse 只调一次 _collect_health），避免重复全量聚合。
    """
    lines = []
    # 优先读埋点表
    try:
        rows = db.query(models.PanelActivity).order_by(
            models.PanelActivity.created_at.desc()).limit(2).all()
        if rows:
            return [{"text": r.text, "at": r.created_at.isoformat()} for r in rows]
    except Exception:  # noqa: BLE001
        pass
    # 推导版：从 health 快照推（调用方传入；无快照则跳过推导）
    try:
        h = health or {}
        metrics = h.get("metrics", {}) or {}
        backlog = metrics.get("enrich_backlog", metrics.get("backlog", 0))
        if backlog:
            lines.append(f"正在消化 {backlog} 条待刮削")
        issues = h.get("issues", []) or []
        for issue in issues[:1]:
            # issue 是 {"level","key","message"} dict，直接 str() 会泄漏原始 dict
            #（线上曾显示 {'level': 'down', 'key': 'enrich_backlog', ...}），只取 message
            if isinstance(issue, dict):
                if issue.get("key") == "enrich_backlog" and backlog:
                    continue  # 已在上面用 backlog 行表达，避免重复
                msg = issue.get("message") or ""
            else:
                msg = str(issue)
            msg = str(msg).strip()
            if msg:
                lines.append(msg[:60])
    except Exception:  # noqa: BLE001
        pass
    if not lines:
        lines.append("服务器运行中，一切正常")
    # 凌晨单集刮削预告
    now = _now_local_naive()
    if now.hour < 2:
        lines.append("02:00 将开始刮削单集")
    return [{"text": t, "at": now.isoformat()} for t in lines[:2]]


@router.get("/pulse")
def panel_pulse(
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    _guard()
    health = {}
    health_raw: dict | None = None
    try:
        from backend.main import _collect_health
        h = _collect_health()
        health_raw = h
        health = {
            "status": h.get("status"),
            "level": h.get("level"),
            "issues_count": len(h.get("issues", []) or []),
        }
    except Exception:  # noqa: BLE001
        health = {"status": "unknown", "level": "unknown", "issues_count": 0}
    return _ok({
        "health": health,
        "activities": _derive_activity(db, health_raw),
        "timestamp": _now_local_naive().isoformat(),
    })


# ==================== 5. GET /api/panel/letter ====================

@router.get("/letter")
def panel_letter(
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    _guard()
    now = _now_local_naive()
    yesterday_start = (now - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    sessions = db.query(models.EmbySession).filter(
        models.EmbySession.user_id == user.id,
        models.EmbySession.start_time >= yesterday_start,
        models.EmbySession.start_time < today_start,
    ).all()
    total_secs = sum(s.play_duration or 0 for s in sessions)
    # 看完的条目（去重，播放超 1 分钟）
    watched = sorted({s.item_name for s in sessions
                      if s.item_name and (s.play_duration or 0) > 60})
    # 口味偏移：昨日最多的 genre（尽力而为）
    taste_shift = ""
    try:
        from backend.panel_achievements import user_tastes
        tastes = user_tastes(db, user.id)
        top = sorted(tastes.items(), key=lambda kv: kv[1]["count"], reverse=True)
        if top and top[0][1]["count"] > 0:
            taste_shift = f"最近偏爱「{top[0][1]['name']}」"
    except Exception:  # noqa: BLE001
        pass

    hours = total_secs // 3600
    minutes = (total_secs % 3600) // 60
    return _ok({
        "date": (now - timedelta(days=1)).strftime("%Y-%m-%d"),
        "watch_duration": f"{hours}小时{minutes}分" if hours else f"{minutes}分",
        "watch_count": len(watched),
        "watched": watched[:5],
        "taste_shift": taste_shift,
        "streak": _streak(db, user.id),
        "teaser": "今日封存已送达，去拆盲盒吧 📦",
    })


# ==================== 5.6 GET /api/panel/feed ====================
# 发现页「实时动态流」：按时间倒序的事件数组，语义=跟随当前服。
# realm_id 不传时用面板当前服（realms.active_realm_id）；传了则只返回该服可见
# 的事件（realm 匹配，或 realm_id 为 NULL 的全局事件）。不聚合、不加页签。

@router.get("/feed")
def panel_feed(
    realm_id: int | None = Query(None, description="服 id，不传=面板当前服"),
    limit: int = Query(12, ge=1, le=50, description="返回条数，默认 12，上限 50"),
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    _guard()
    try:
        from backend import realms as _realms
        rid = realm_id if realm_id is not None else _realms.active_realm_id(db)
    except Exception:  # noqa: BLE001 — 服解析失败时退化为全局视图
        logger.warning("feed 服解析失败", exc_info=True)
        rid = realm_id
    from backend.panel_feed import read_feed
    items = read_feed(db, rid, limit)
    return _ok({"items": items, "realm_id": rid, "limit": limit})


# ==================== 5.7 GET /api/panel/home（perf 优化 2026-10-13） ====================
# 首页合并接口：一次返回 today/parcels/pulse/letter，把 4 次跨洋 RTT 并成 1 次。
# 直接调用上方已定义的 4 个函数，不复制逻辑；单个失败隔离为 None，不拖垮其他区块。


def _section_or_none(fn, db, user):
    """调单个区块函数；异常则隔离为 None，不拖垮整个 /home。"""
    try:
        return fn(db, user)["data"]
    except Exception:  # noqa: BLE001
        logger.warning("panel_home 区块 %s 失败，已隔离", fn.__name__, exc_info=True)
        return None


@router.get("/home")
def panel_home(
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    _guard()
    return _ok({
        "today": _section_or_none(panel_today, db, user),
        "parcels": _section_or_none(panel_parcels, db, user),
        "pulse": _section_or_none(panel_pulse, db, user),
        "letter": _section_or_none(panel_letter, db, user),
        "board": _section_or_none(panel_board, db, user),
    })


# ==================== 6. GET /api/panel/collector ====================

@router.get("/collector")
def panel_collector(
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    _guard()
    from backend.panel_achievements import user_tastes

    # r20: 成就评估移出请求热路径——后台线程异步跑（幂等），GET 本身不再写库。
    # 本轮 new_unlocks 恒为空；解锁状态通过 achievements[].unlocked 下次可见。
    threading.Thread(
        target=_evaluate_achievements_async, args=(user.id,), daemon=True).start()
    new_unlocks: list = []

    tastes = {}
    try:
        tastes = user_tastes(db, user.id)
    except Exception:  # noqa: BLE001
        logger.exception("口味聚合失败")

    # 成就列表（定义 + 解锁状态）
    defs = db.query(models.PanelAchievementDef).filter_by(
        enabled=True).order_by(models.PanelAchievementDef.sort).all()
    unlocked = {r[0] for r in db.query(
        models.PanelAchievementUnlock.ach_key).filter_by(user_id=user.id).all()}
    achievements = [{
        "key": d.key,
        "name": d.name,
        "desc": d.desc,
        "icon": d.icon,
        "unlocked": d.key in unlocked,
    } for d in defs]

    # 馆灵：累计观影小时推导（阈值走 SystemConfig，可配置）
    total_secs = db.query(func.coalesce(func.sum(models.EmbySession.play_duration), 0)).filter(
        models.EmbySession.user_id == user.id).scalar() or 0
    total_hours = total_secs / 3600
    try:
        from backend.servers import config_value
        stages_raw = config_value(db, "panel_pet_stages", "")
        stages = json.loads(stages_raw) if stages_raw else []
    except Exception:  # noqa: BLE001
        stages = []
    if not stages:
        stages = [dict(s) for s in PET_STAGES_DEFAULT]
    cur_idx = 0
    for i, st in enumerate(stages):
        if total_hours >= st["hours"]:
            cur_idx = i
    cur = stages[cur_idx]
    nxt = stages[cur_idx + 1] if cur_idx + 1 < len(stages) else None
    if nxt:
        span = nxt["hours"] - cur["hours"]
        progress = min(1.0, max(0.0, (total_hours - cur["hours"]) / span)) if span else 1.0
    else:
        progress = 1.0

    # 佩戴中的徽章（存账号，见 POST /worn-badge）；纯展示，异常兜底 None
    try:
        from backend.panel_achievements import worn_badge_payload
        worn_badge = worn_badge_payload(db, user.id)
    except Exception:  # noqa: BLE001
        worn_badge = None

    points = getattr(user, "points", 0) or 0
    return _ok({
        "nickname": getattr(user, "nickname", None) or user.username,
        "points": points,
        "watch_hours": round(total_hours, 1),
        "streak": _streak(db, user.id),
        "achievement_count": len(unlocked),
        "tastes": tastes,
        "achievements": achievements,
        "worn_badge": worn_badge,
        "new_unlocks": new_unlocks,
        "pet": {
            "stage": cur_idx,
            "name": cur["name"],
            "icon": cur["icon"],
            "hours": round(total_hours, 1),
            "next_name": nxt["name"] if nxt else None,
            "next_icon": nxt["icon"] if nxt else None,
            "next_hours": nxt["hours"] if nxt else None,
            "progress": round(progress, 3),
            "stages": [{"name": s["name"], "icon": s["icon"]} for s in stages],
        },
    })


# ==================== 6.5 POST /api/panel/worn-badge ====================

class WornBadgeIn(BaseModel):
    key: str | None = None


@router.post("/worn-badge")
def set_worn_badge(
    req: WornBadgeIn,
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    """佩戴/摘下成就徽章。佩戴状态存账号（web_users.worn_badge_key），
    不再存浏览器本地——换账号不串号、换设备跟着走。"""
    _guard()
    key = (req.key or "").strip()
    if not key:
        user.worn_badge_key = None
        db.commit()
        return _ok({"worn_badge": None})
    d = db.query(models.PanelAchievementDef).filter_by(key=key, enabled=True).first()
    if not d:
        raise HTTPException(status_code=400, detail="这枚徽章不存在或已下架")
    unlocked = db.query(models.PanelAchievementUnlock).filter_by(
        user_id=user.id, ach_key=key).first()
    if not unlocked:
        raise HTTPException(status_code=400, detail="你还没有解锁这枚徽章")
    user.worn_badge_key = key
    db.commit()
    return _ok({"worn_badge": {"key": d.key, "name": d.name, "icon": d.icon}})


# ==================== 6.6 探险世界：告示牌 / 留言墙 / 抽一签 / 隐私 ====================

def panel_board(db, user):
    """世界入口告示牌：服主留言（后台可配），异常隔离为 None 不拖垮 /home。"""
    _guard()
    from backend.integrations import world_board
    return _ok(world_board.public_info(db))


class WallPostIn(BaseModel):
    content: str


@router.get("/wall")
def wall_list(
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    """留言墙：最新 20 条未隐藏留言"""
    _guard()
    from backend.emby_server import models as em
    rows = (
        db.query(em.WorldWallPost)
        .filter(em.WorldWallPost.is_hidden == False)  # noqa: E712
        .order_by(em.WorldWallPost.created_at.desc())
        .limit(20)
        .all()
    )
    return _ok({
        "posts": [
            {
                "id": r.id,
                "nickname": r.nickname,
                "content": r.content,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "mine": r.user_id == user.id,
            }
            for r in rows
        ]
    })


@router.post("/wall")
def wall_post(
    payload: WallPostIn,
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    """发布留言：限 60 字，10 分钟 1 条"""
    _guard()
    from backend.emby_server import models as em
    content = (payload.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="留言内容不能为空")
    if len(content) > 60:
        raise HTTPException(status_code=400, detail="留言最多 60 字")
    allowed, _ = check_rate_limit(f"wall:{user.id}", 1, 600)
    if not allowed:
        raise HTTPException(status_code=429, detail="留言太频繁，10 分钟后再试")
    post = em.WorldWallPost(user_id=user.id, nickname=user.username, content=content)
    db.add(post)
    db.commit()
    db.refresh(post)
    return _ok({"id": post.id})


def _fortune_today():
    """抽签自然日（Asia/Shanghai）"""
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


@router.get("/fortune")
def fortune_today(
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    """今日抽签结果（未抽返回 drew=False）"""
    _guard()
    from backend.emby_server import models as em
    rec = (
        db.query(em.WorldFortuneDraw)
        .filter(
            em.WorldFortuneDraw.user_id == user.id,
            em.WorldFortuneDraw.draw_date == _fortune_today(),
        )
        .first()
    )
    if not rec:
        return _ok({"drew": False})
    return _ok({"drew": True, "level": rec.level, "text": rec.text,
                "yi": rec.yi, "ji": rec.ji})


@router.post("/fortune")
def fortune_draw(
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    """抽一签：每天一次。当日首次随机抽一条落库；并发冲突走唯一索引兜底重查。不挂积分。"""
    _guard()
    from backend.emby_server import models as em
    from backend.integrations import world_fortune
    today = _fortune_today()
    rec = (
        db.query(em.WorldFortuneDraw)
        .filter(
            em.WorldFortuneDraw.user_id == user.id,
            em.WorldFortuneDraw.draw_date == today,
        )
        .first()
    )
    if rec:
        return _ok({"drew": True, "level": rec.level, "text": rec.text,
                    "yi": rec.yi, "ji": rec.ji})
    pool = world_fortune.parse_pool(world_fortune.read(db).get("fortune_pool", ""))
    if not pool:
        raise HTTPException(status_code=500, detail="签文池未配置，请联系管理员")
    pick = random.choice(pool)
    rec = em.WorldFortuneDraw(
        user_id=user.id, draw_date=today,
        level=pick["level"], text=pick["text"], yi=pick["yi"], ji=pick["ji"],
    )
    db.add(rec)
    try:
        db.commit()
    except IntegrityError:
        # 并发同时抽：唯一索引兜底，重查已落库的那条
        db.rollback()
        rec = (
            db.query(em.WorldFortuneDraw)
            .filter(
                em.WorldFortuneDraw.user_id == user.id,
                em.WorldFortuneDraw.draw_date == today,
            )
            .first()
        )
        if not rec:
            raise
    db.refresh(rec)
    return _ok({"drew": True, "level": rec.level, "text": rec.text,
                "yi": rec.yi, "ji": rec.ji})


class PrivacyIn(BaseModel):
    show_on_leaderboard: bool


@router.get("/privacy")
def get_privacy(
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    """我的隐私设置：是否允许上榜（观影碑时长榜）"""
    _guard()
    db.refresh(user)
    return _ok({"show_on_leaderboard": bool(getattr(user, "show_on_leaderboard", True))})


@router.put("/privacy")
def set_privacy(
    payload: PrivacyIn,
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    """设置是否允许上榜（观影碑时长榜）"""
    _guard()
    user.show_on_leaderboard = bool(payload.show_on_leaderboard)
    db.commit()
    return _ok({"show_on_leaderboard": bool(user.show_on_leaderboard)})


# ==================== 7. GET /api/panel/server ====================

@router.get("/server")
def panel_server(
    db: Session = Depends(get_db),
    user: models.WebUser = Depends(get_current_user),
):
    _guard()
    from backend.emby_server import models as em

    # vitals：四格（尽量从 health 取，失败则占位）
    vitals = {"status": "unknown", "backlog": 0, "online": 0, "version": ""}
    try:
        from backend.main import _collect_health
        h = _collect_health()
        metrics = h.get("metrics", {}) or {}
        vitals = {
            "status": h.get("status"),
            "level": h.get("level"),
            "backlog": metrics.get("enrich_backlog", metrics.get("backlog", 0)),
            "version": getattr(__import__("backend.main", fromlist=["app"]).app, "version", ""),
        }
    except Exception:  # noqa: BLE001
        pass

    # 片库生态：每库条目数
    eco = []
    try:
        lib_counts = db.query(
            em.MediaItem.library_id, func.count(em.MediaItem.id)
        ).filter(em.MediaItem.item_type.in_(["movie", "series"])).group_by(
            em.MediaItem.library_id).all()
        # 库名映射
        lib_names = {}
        try:
            for lib in db.query(em.Library).all():
                lib_names[lib.id] = lib.name
        except Exception:  # noqa: BLE001
            pass
        eco = [{
            "library_id": lid,
            "name": lib_names.get(lid, f"库 {lid}"),
            "count": cnt,
        } for lid, cnt in sorted(lib_counts, key=lambda x: -x[1])]
    except Exception:  # noqa: BLE001
        logger.exception("片库生态查询失败")

    # 生长记录：过去 7 天每天新入库数（替换"机关"假开关的实用组件）。
    # r28 打磨：单条 GROUP BY 一次查 7 天 —— 原来 7 次 COUNT 轮询，面板每次打开都打 7 遍。
    # r35：date_added 是 UTC-naive 入库，此前按 func.date 的 UTC 日分组、标签却按
    # 上海自然日画 → 每天 00:00–08:00（CST）入库的条目被算到前一天。改为取原始
    # 时间戳、Python 侧 +8h 按上海自然日归桶（方言无关，与 r34 F8 同口径）。
    growth = []
    try:
        today = _now_local_naive().replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = _today_start_utc_naive() - timedelta(days=6)
        rows = db.query(
            em.MediaItem.date_added,
            func.count(em.MediaItem.id),
        ).filter(
            em.MediaItem.date_added >= week_start,
            em.MediaItem.item_type.in_(["movie", "series"]),
        ).group_by(em.MediaItem.date_added).all()
        by_day = {}
        for ts, cnt in rows:
            if ts is None:
                continue
            day = (ts + timedelta(hours=8)).date()
            by_day[day] = by_day.get(day, 0) + (cnt or 0)
        for i in range(6, -1, -1):
            day = today - timedelta(days=i)
            growth.append({
                "date": day.strftime("%m-%d"),
                "weekday": ["一", "二", "三", "四", "五", "六", "日"][day.weekday()],
                "count": by_day.get(day.date(), 0),
            })
    except Exception:  # noqa: BLE001
        logger.exception("生长记录查询失败")

    # 雷达任务：固定 4 个后台任务 + 状态
    radar_tasks = [
        {"key": "enrich", "name": "刮削", "active": bool(vitals.get("backlog"))},
        {"key": "probe", "name": "探针", "active": True},
        {"key": "scan", "name": "扫描", "active": False},
        {"key": "night", "name": "单集", "active": False},
    ]

    # 面板是否可写（管理员）
    is_admin = bool(getattr(user, "is_staff", False))

    return _ok({
        "vitals": vitals,
        "eco": eco,
        "radar_tasks": radar_tasks,
        "growth": growth,
        "is_admin": is_admin,
    })


# ==================== 活动埋点（供 worker 调用） ====================

def push_activity(db: Session, text: str, event_type: str = "system",
                  library: str | None = None,
                  realm_id: int | None = None) -> None:
    """写入一条后台活动（每服分区环形保留最近 500 条，见 backend/panel_feed）

    event_type ∈ {scan, enrich, play, system}；library=库名标签；
    realm_id=None=全局事件（所有服可见）。
    """
    from backend.panel_feed import note_feed
    note_feed(db, text, event_type=event_type, library_name=library,
              realm_id=realm_id)
