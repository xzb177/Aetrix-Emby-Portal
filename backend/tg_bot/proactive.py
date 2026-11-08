"""Telegram 主动私聊提醒：求片到货、转存终态。

两件事分开开关，默认开（system_configs，走公益配置注册表；
2026-11-08 私聊通报优化批由默认关改默认开）：

- ``tg_notify_store_arrival_enabled``：入库/扫描对上求片（判定与
  ``find_library_item`` 一致）时，把处理中的求片行标成 completed，
  并私聊求片人和全部附议人。读列表时的旧翻转还在，走同一条入队，
  靠已推记录不重复发。
- ``tg_notify_transfer_done_enabled``：用户自己发起的转存
  （``owner_scope=user`` 且 trigger 属于想看 / 预取 / 点播）到终态时
  私聊本人。服主任务、追剧、配额、找源这类系统单不发。

关着的时候：不发、不写已推记录。

到货消息带一个「去看看」链接按钮（站内没有条目详情页，落到
``site_url`` 下的「我的求片/想看」页；``site_url`` 没配时不带按钮）。
转存失败（终态，不是单内重试）说人话原因；仓库里没有用户可调用的
重试接口，所以只给已有的 ``/whyplay`` 入口（按钮复用 ``menu:whyplay``），
不新造重试口；两个按钮可以并存（whyplay 一行、去看看一行）。

用户级偏好：发送前查「求片到货类」TG 偏好（backend/notify_prefs.py），
关了的用户整批标 muted 静默丢弃，站内信不受影响。

同一用户在合并窗口（``tg_notify_merge_window_sec``，默认 60 秒）里
的多条收成一条。合并消息只列文字；里面有失败项时才挂「为什么播不了」
按钮。条数太多时截断并写「还有 N 部」。

发送只走 ``tg_bot.sender.send_message``。失败不影响入库和转存：
可重试的失败有限次退避；用户屏蔽机器人、403、聊不了直接记成已处理，
不再重试。重复扫描、重复回调、多进程靠唯一约束只留一行。
"""

from __future__ import annotations

import html
import logging
import os
import threading
from datetime import datetime, timedelta

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import models
from backend.database import SessionLocal

logger = logging.getLogger(__name__)

KEY_STORE = "tg_notify_store_arrival_enabled"
KEY_TRANSFER = "tg_notify_transfer_done_enabled"
KEY_WINDOW = "tg_notify_merge_window_sec"

DEFAULT_WINDOW_SEC = 60
MAX_WINDOW_SEC = 3600
MAX_SEND_ATTEMPTS = 3
BACKOFF_SEC = (30, 120, 480)
# 一条消息里最多列多少部，剩下的写成「还有 N 部」（Telegram 正文上限 4096）。
MAX_LINES = 20
TEXT_LIMIT = 3800
# 领走后超过这么久还停在 inflight，视为进程没了，打回 pending 再试。
INFLIGHT_STALE_SEC = 120

# 用户自己发起、到终态要提醒的 trigger。
# store_want / prefetch 是仓库里写明的「用户主动意图」；点播门禁会把
# 这两类在途单提升成 playback，所以终态时 playback 也算用户这条线。
# 追剧、配额、找源、管理端手动、服主 source 单不在这里。
USER_TRIGGERS = frozenset({"store_want", "prefetch", "playback"})

KIND_ARRIVAL = "store_arrival"
KIND_OK = "transfer_ok"
KIND_FAIL = "transfer_fail"

# 内部错误码 → 人话。未知码用通用说法，绝不把 error_detail 原文发出去。
FAIL_TEXT = {
    "no_binding": "网盘绑定失效了，请到网页重新绑定后再试",
    "auth_error": "网盘授权失效了，请到网页重新绑定后再试",
    "dead": "片源已经失效，暂时转不过去",
    "unsupported": "这种片源暂时不支持转到你的网盘",
    "no_drive": "找不到源文件，暂时转不过去",
    "no_source_fid": "找不到源文件，暂时转不过去",
    "orphan": "文件转过去了，但暂时还不能播",
    "item_gone": "这部片已经下架了",
    "retriable": "转存没有完成，可以稍后再试",
    "insufficient_space": "网盘空间不够，清理一些再试",
    "copy_unresolved": "转存结果对不上，没有登记成功",
    "cross_provider_relay_disabled": "跨网盘转存暂时关闭了",
    "file_only_copy_unavailable": "当前网盘授权没法转存这个文件，请重新绑定后再试",
    "worker_error": "转存中途出了问题，可以稍后再试",
    "provider_removed": "这个网盘已经不再支持转存",
    "ops_cancelled": "这次转存被取消了",
}
GENERIC_FAIL = "转存没有完成，可以稍后再试"

WHYPLAY_MARKUP = {
    "inline_keyboard": [[
        {"text": "为什么播不了", "callback_data": "menu:whyplay"},
    ]],
}

_TRUE = frozenset({"1", "true", "yes", "on"})
_FLUSHER_LOCK = threading.Lock()
_FLUSHER_STARTED = False
_HOOK_INSTALLED = False


def _flusher_enabled() -> bool:
    return os.getenv("TG_PROACTIVE_FLUSHER", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _install_commit_hook() -> None:
    """入队行随业务事务一起提交；提交成功后再唤醒发送线程。"""
    global _HOOK_INSTALLED
    if _HOOK_INSTALLED:
        return
    _HOOK_INSTALLED = True

    @event.listens_for(Session, "after_commit")
    def _after_commit(session):
        info = getattr(session, "info", None)
        if info is None or not info.pop("tg_proactive_wake", False):
            return
        wake_flusher()

    @event.listens_for(Session, "after_rollback")
    def _after_rollback(session):
        info = getattr(session, "info", None)
        if info is not None:
            info.pop("tg_proactive_wake", None)


_install_commit_hook()


def _is_true(raw) -> bool:
    return str(raw or "").strip().lower() in _TRUE


def _config_on(db: Session, key: str, default: bool = False) -> bool:
    """缺行按 ``default``；显式写了值按值走。关 = 不发、不写已推记录。"""
    try:
        row = (
            db.query(models.SystemConfig)
            .filter(models.SystemConfig.key == key)
            .first()
        )
    except Exception:
        logger.warning("主动提醒开关读取失败 key=%s", key, exc_info=True)
        return default
    if row is None:
        return default
    return _is_true(row.value)


def store_arrival_enabled(db: Session) -> bool:
    # 2026-11-08 私聊通报优化批：到货类统一默认开（缺行=开）。
    return _config_on(db, KEY_STORE, default=True)


def transfer_done_enabled(db: Session) -> bool:
    return _config_on(db, KEY_TRANSFER, default=True)


def merge_window_sec(db: Session) -> int:
    try:
        row = (
            db.query(models.SystemConfig)
            .filter(models.SystemConfig.key == KEY_WINDOW)
            .first()
        )
        raw = str(row.value).strip() if row is not None and row.value is not None else ""
        value = int(raw) if raw else DEFAULT_WINDOW_SEC
    except Exception:
        return DEFAULT_WINDOW_SEC
    if value < 0:
        return 0
    if value > MAX_WINDOW_SEC:
        return MAX_WINDOW_SEC
    return value


def human_fail_reason(error_kind: str | None) -> str:
    """错误码说成人话。不认识的码给通用说法，不带内部原文。"""
    kind = (error_kind or "").strip()
    return FAIL_TEXT.get(kind, GENERIC_FAIL)


def _mark_wake(db: Session) -> None:
    try:
        db.info["tg_proactive_wake"] = True
    except Exception:
        logger.warning("主动提醒无法标记提交后发送", exc_info=True)


def _claim(db: Session, kind: str, subject_id: int, user_id: int,
           title: str, detail: str) -> bool:
    """插入一行 pending。已有行（含并发同时插）返回 False，不抛给调用方。

    用保存点，撞唯一约束时只回滚这一插，不把外层的入库/转存事务冲掉。
    """
    row = models.TgProactiveNotice(
        kind=kind,
        subject_id=int(subject_id),
        user_id=int(user_id),
        title=(title or "这部片")[:255],
        detail=(detail or "")[:200],
        status="pending",
        attempts=0,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        _drop_row(db, row)
        return False
    except Exception:
        _drop_row(db, row)
        logger.warning("主动提醒入队失败 kind=%s subject=%s", kind, subject_id,
                       exc_info=True)
        return False
    _mark_wake(db)
    return True


def _drop_row(db: Session, row) -> None:
    """保存点回滚后别让这行跟着外层事务再插一次。"""
    try:
        if row in db:
            db.expunge(row)
    except Exception:
        pass


def _bound_user_ids(db: Session, user_ids: set[int]) -> list[int]:
    """只留下绑了 Telegram 的用户。没绑的跳过：不报错、不写行、不重试。"""
    if not user_ids:
        return []
    rows = (
        db.query(models.WebUser.id)
        .filter(
            models.WebUser.id.in_(list(user_ids)),
            models.WebUser.telegram_id.isnot(None),
            models.WebUser.telegram_id != 0,
        )
        .all()
    )
    return [row[0] for row in rows]


def note_store_request_completed(db: Session, request_row,
                                 voter_ids=None) -> int:
    """求片行已经是 completed 时，给求片人和附议人各入队一次。

    开关关着直接返回 0，不写已推记录。调用方负责提交。

    ``voter_ids``：None（默认）= 附议人从 MovieRequestVote 全量派生；
    显式传名单（含空名单）= 只用该名单——管理员手动标记入库的调用方
    先按 media_seek 的「通知附议者」门控筛过再传进来，避免两处口径打架。
    """
    if not store_arrival_enabled(db):
        return 0
    if getattr(request_row, "status", "") != "completed":
        return 0
    request_id = getattr(request_row, "id", None)
    if request_id is None:
        return 0
    user_ids = set()
    owner = getattr(request_row, "user_id", None)
    if owner is not None:
        user_ids.add(int(owner))
    if voter_ids is None:
        votes = (
            db.query(models.MovieRequestVote.user_id)
            .filter(models.MovieRequestVote.request_id == request_id)
            .all()
        )
        voter_ids = [row[0] for row in votes]
    user_ids.update(int(uid) for uid in voter_ids if uid is not None)
    title = (getattr(request_row, "movie_name", "") or "").strip() or "这部片"
    claimed = 0
    for user_id in _bound_user_ids(db, user_ids):
        if _claim(db, KIND_ARRIVAL, request_id, user_id, title, ""):
            claimed += 1
    return claimed


def sync_arrived_requests(db: Session) -> int:
    """扫描侧：处理中的求片若已对上库，标 completed 并入队。

    开关关着什么都不改。判定复用 ``find_library_item``。
    """
    if not store_arrival_enabled(db):
        return 0
    from backend.api.store import find_library_item
    from backend import media_seek

    rows = (
        db.query(models.MovieRequest)
        .filter(
            models.MovieRequest.status.in_(media_seek.ACTIVE_STATUSES),
            models.MovieRequest.tmdb_id.isnot(None),
            models.MovieRequest.tmdb_id != "",
        )
        .all()
    )
    changed = 0
    for row in rows:
        if find_library_item(db, row.tmdb_id) is None:
            continue
        row.status = "completed"
        note_store_request_completed(db, row)
        changed += 1
    if changed:
        db.commit()
    return changed


def maybe_sync_store_arrivals(db: Session | None = None) -> None:
    """扫描成功后的入口。自己开线程、自己开会话，异常不外抛。

    传入扫描自己的会话时，先在那上面看开关：关着就不另起线程。
    """
    if db is not None:
        try:
            if not store_arrival_enabled(db):
                return
        except Exception:
            logger.warning("求片到货提醒开关读取失败", exc_info=True)
            return

    def _work() -> None:
        db = SessionLocal()
        try:
            sync_arrived_requests(db)
        except Exception:
            logger.warning("求片到货提醒扫描侧失败", exc_info=True)
            try:
                db.rollback()
            except Exception:
                pass
        finally:
            db.close()

    threading.Thread(
        target=_work, name="tg-store-arrival", daemon=True,
    ).start()


def note_transfer_terminal(db: Session, job) -> bool:
    """转存任务已是 done/failed 时，给发起用户入队一次。

    非终态（还在重试、pending）、服主单、系统 trigger、开关关、
    没绑 Telegram：直接返回，不写已推记录。调用方负责提交。
    """
    status = (getattr(job, "status", "") or "").strip()
    if status not in ("done", "failed"):
        return False
    if (getattr(job, "owner_scope", "") or "").strip() != "user":
        return False
    trigger = (getattr(job, "trigger", "") or "").strip()
    if trigger not in USER_TRIGGERS:
        return False
    if not transfer_done_enabled(db):
        return False
    job_id = getattr(job, "id", None)
    if job_id is None:
        return False
    try:
        user_id = int(str(getattr(job, "owner_ref", "") or "").strip())
    except (TypeError, ValueError):
        return False
    if not _bound_user_ids(db, {user_id}):
        return False
    title = _item_title(db, getattr(job, "item_id", None))
    if status == "done":
        return _claim(db, KIND_OK, job_id, user_id, title, "")
    reason = human_fail_reason(getattr(job, "error_kind", None))
    return _claim(db, KIND_FAIL, job_id, user_id, title, reason)


def _item_title(db: Session, item_id) -> str:
    if item_id is None:
        return "这部片"
    try:
        from backend.emby_server import models as em
        row = db.query(em.MediaItem.name).filter(em.MediaItem.id == item_id).first()
    except Exception:
        logger.warning("主动提醒查片名失败 item=%s", item_id, exc_info=True)
        return "这部片"
    name = (row[0] if row else "") or ""
    name = str(name).strip()
    return name or "这部片"


def _esc(value: str) -> str:
    return html.escape(value or "", quote=False)


def _line(row) -> str:
    title = _esc(row.title or "这部片")
    if row.kind == KIND_ARRIVAL:
        return f"你要的《{title}》到了"
    if row.kind == KIND_OK:
        return f"《{title}》可以看了"
    reason = _esc(row.detail or GENERIC_FAIL)
    return f"《{title}》没转成：{reason}"


def render_notice(rows: list, watch_url: str = "") -> tuple[str, dict | None]:
    """拼一条合并消息。

    失败项排在前面，截断时人话原因不容易被「还有 N 部」吃掉。
    按钮：有失败项挂 whyplay 行；``watch_url`` 非空且含到货/转存成功行时
    再加一行「去看看」。两者并存时 whyplay 在前、去看看在后。
    """
    ordered = sorted(
        rows,
        key=lambda row: (0 if row.kind == KIND_FAIL else 1, getattr(row, "id", 0) or 0),
    )
    hidden = 0
    shown = ordered
    if len(ordered) > MAX_LINES:
        hidden = len(ordered) - MAX_LINES
        shown = ordered[:MAX_LINES]
    has_fail = any(row.kind == KIND_FAIL for row in ordered)
    if len(ordered) == 1 and hidden == 0:
        text = _line(ordered[0])
        if has_fail:
            text += "\n想再了解，私聊发 /whyplay"
    else:
        lines = ["这几部有消息："]
        lines.extend("· " + _line(row) for row in shown)
        if hidden:
            lines.append(f"还有 {hidden} 部")
        if has_fail:
            lines.append("想再了解，私聊发 /whyplay")
        text = "\n".join(lines)
    if len(text) > TEXT_LIMIT:
        text = text[: TEXT_LIMIT - 1] + "…"
    buttons = []
    if has_fail:
        buttons.append(WHYPLAY_MARKUP["inline_keyboard"][0])
    has_arrival = any(
        row.kind in (KIND_ARRIVAL, KIND_OK) for row in ordered)
    if watch_url and has_arrival:
        buttons.append([{"text": "去看看", "url": watch_url}])
    markup = {"inline_keyboard": buttons} if buttons else None
    return text, markup


def _watch_url(db: Session) -> str:
    """「去看看」的落点：站内没有条目详情页，统一落「我的求片/想看」页。

    ``site_url`` 没配时返回空串＝不带按钮（不猜域名）。
    """
    try:
        from backend.integrations import store as _store

        # 直查库不用 TTL 热缓存：发送路径低频，且改完 site_url 应立即生效
        base = str(_store.read_value(db, "site_url", "") or "").strip()
    except Exception:
        return ""
    if not base:
        return ""
    return base.rstrip("/") + "/want"


def _permanent_failure(err: str | None) -> bool:
    text = (err or "").lower()
    needles = (
        "403",
        "forbidden",
        "blocked",
        "deactivated",
        "chat not found",
        "user not found",
        "bot can't initiate",
        "have no rights",
        "peer_id_invalid",
    )
    return any(needle in text for needle in needles)


def _eligible(rows, now: datetime) -> list:
    ready = []
    for row in rows:
        due = row.next_attempt_at
        if due is None or due <= now:
            ready.append(row)
    return ready


def _requeue_stale_inflight(db: Session, now: datetime) -> None:
    cutoff = now - timedelta(seconds=INFLIGHT_STALE_SEC)
    stale = (
        db.query(models.TgProactiveNotice)
        .filter(
            models.TgProactiveNotice.status == "inflight",
            models.TgProactiveNotice.updated_at.isnot(None),
            models.TgProactiveNotice.updated_at <= cutoff,
        )
        .all()
    )
    for row in stale:
        if (row.attempts or 0) >= MAX_SEND_ATTEMPTS:
            row.status = "gave_up"
        else:
            row.status = "pending"
        row.updated_at = now
    if stale:
        db.commit()


def flush_due(db: Session, now: datetime | None = None) -> int:
    """把到点的 pending 按用户合并后发出。返回实际调用发送的次数。

    开关关着的那一类留在表里不发。发送异常只记日志。
    """
    now = now or datetime.now()
    store_on = store_arrival_enabled(db)
    transfer_on = transfer_done_enabled(db)
    if not store_on and not transfer_on:
        return 0
    _requeue_stale_inflight(db, now)
    window = merge_window_sec(db)
    user_ids = [
        row[0]
        for row in (
            db.query(models.TgProactiveNotice.user_id)
            .filter(models.TgProactiveNotice.status == "pending")
            .distinct()
            .all()
        )
    ]
    sent_calls = 0
    for user_id in user_ids:
        try:
            sent_calls += _flush_user(
                db, int(user_id), now, window, store_on, transfer_on,
            )
        except Exception:
            logger.warning("主动提醒发送失败 user=%s", user_id, exc_info=True)
            try:
                db.rollback()
            except Exception:
                pass
    return sent_calls


def _flush_user(db: Session, user_id: int, now: datetime, window: int,
                store_on: bool, transfer_on: bool) -> int:
    pending = (
        db.query(models.TgProactiveNotice)
        .filter(
            models.TgProactiveNotice.user_id == user_id,
            models.TgProactiveNotice.status == "pending",
        )
        .order_by(models.TgProactiveNotice.id)
        .all()
    )
    allowed = []
    for row in pending:
        if row.kind == KIND_ARRIVAL and not store_on:
            continue
        if row.kind in (KIND_OK, KIND_FAIL) and not transfer_on:
            continue
        allowed.append(row)
    if allowed:
        # 用户偏好：关了「求片到货类」TG 的用户，这批整批静默丢弃
        #（标 muted，不补发；站内信不受影响）。
        from backend import notify_prefs

        if not notify_prefs.tg_allowed(
                db, user_id, notify_prefs.CAT_SEEK):
            for row in allowed:
                row.status = "muted"
                row.updated_at = now
            db.commit()
            return 0
    ready = _eligible(allowed, now)
    if not ready:
        return 0
    oldest = min((row.created_at or now) for row in ready)
    if oldest + timedelta(seconds=window) > now:
        return 0
    claimed = []
    for row in ready:
        updated = (
            db.query(models.TgProactiveNotice)
            .filter(
                models.TgProactiveNotice.id == row.id,
                models.TgProactiveNotice.status == "pending",
            )
            .update(
                {
                    "status": "inflight",
                    "attempts": (row.attempts or 0) + 1,
                    "updated_at": now,
                },
                synchronize_session=False,
            )
        )
        if updated:
            claimed.append(row.id)
    db.commit()
    if not claimed:
        return 0
    rows = (
        db.query(models.TgProactiveNotice)
        .filter(models.TgProactiveNotice.id.in_(claimed))
        .order_by(models.TgProactiveNotice.id)
        .all()
    )
    user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
    chat_id = getattr(user, "telegram_id", None) if user is not None else None
    if not chat_id:
        for row in rows:
            row.status = "blocked"
            row.updated_at = now
        db.commit()
        return 0
    text, markup = render_notice(rows, watch_url=_watch_url(db))
    ok, err = _send(db, int(chat_id), text, markup)
    if ok:
        for row in rows:
            row.status = "sent"
            row.updated_at = now
        db.commit()
        return 1
    if _permanent_failure(err):
        for row in rows:
            row.status = "blocked"
            row.updated_at = now
        db.commit()
        logger.info("主动提醒不再重试 user=%s（对方收不到）", user_id)
        return 1
    for row in rows:
        if (row.attempts or 0) >= MAX_SEND_ATTEMPTS:
            row.status = "gave_up"
            row.next_attempt_at = None
        else:
            row.status = "pending"
            step = min((row.attempts or 1) - 1, len(BACKOFF_SEC) - 1)
            row.next_attempt_at = now + timedelta(seconds=BACKOFF_SEC[max(step, 0)])
        row.updated_at = now
    db.commit()
    logger.info("主动提醒稍后重试 user=%s", user_id)
    return 1


def _send(db: Session, chat_id: int, text: str, markup: dict | None):
    from backend.tg_bot import sender

    try:
        return sender.send_message(
            db, chat_id, text, reply_markup=markup, parse_mode="HTML",
        )
    except Exception:
        logger.warning("主动提醒发送抛错 user_chat=%s", chat_id, exc_info=True)
        return False, "发送异常"


def wake_flusher() -> None:
    """进程内只起一个发送线程。测试可设 TG_PROACTIVE_FLUSHER=0 关掉。"""
    global _FLUSHER_STARTED
    if not _flusher_enabled():
        return
    with _FLUSHER_LOCK:
        if _FLUSHER_STARTED:
            return
        _FLUSHER_STARTED = True

    def _loop() -> None:
        waiter = threading.Event()
        while True:
            db = SessionLocal()
            try:
                flush_due(db)
            except Exception:
                logger.warning("主动提醒发送轮失败", exc_info=True)
                try:
                    db.rollback()
                except Exception:
                    pass
            finally:
                db.close()
            waiter.wait(5)

    threading.Thread(
        target=_loop, name="tg-proactive-flush", daemon=True,
    ).start()


def run_flush_tick() -> int:
    """给定时调度用的一轮。异常不外抛。"""
    db = SessionLocal()
    try:
        return flush_due(db)
    except Exception:
        logger.warning("主动提醒调度轮失败", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return 0
    finally:
        db.close()
