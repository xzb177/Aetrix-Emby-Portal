"""StrmAssistant #9 对标：演职人员增强（RefreshPersonTask）

定时刷新演员信息：
1. 去重：同 person_tmdb_id 的重复演员行只保留一条（按 item_id 分组）
2. 补全：头像为空的演员，尝试从 TMDB 拉取最新头像（有 person_tmdb_id 按 id；
   没有的按名字精确搜 TMDB 人物）；仍缺的按条目走豆瓣演职员页兜底，
   顺带把 TMDB 录成拼音的角色名换成豆瓣的中文角色名
3. 节奏：去重每 PERSON_REFRESH_INTERVAL_SEC（默认 1 天）；补头像每
   PERSON_FILL_INTERVAL_SEC（默认 30 分钟），enrich 写入缺头像的演员后 kick() 立即跑

与 enrich 的 _apply_cast 互补：enrich 在刮削时写演员，
本任务在后台定期检查数据质量。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional
from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

# 全量巡检（去重）默认 24 小时一次；补头像走更短的间隔（新入库的演员不用等一天），
# enrich 写入缺头像的演员后会 kick() 立刻唤醒一轮。
REFRESH_INTERVAL_SEC = env_int("PERSON_REFRESH_INTERVAL_SEC", 86400)
FILL_INTERVAL_SEC = env_int("PERSON_FILL_INTERVAL_SEC", 1800)
# 同一行/同一条目补失败后多久再试（进程内记忆，防止每轮反复打同一批）
RETRY_AFTER_SEC = env_int("PERSON_FILL_RETRY_SEC", 86400)
# 单轮最多处理多少行 / 多少个条目（豆瓣兜底按条目算，每条目 1~2 次请求）
FILL_BATCH = env_int("PERSON_FILL_BATCH", 50)
DOUBAN_ITEMS_PER_RUN = env_int("PERSON_DOUBAN_ITEMS_PER_RUN", 10)
# 名字搜不到 TMDB 人物时写这个标记，之后不再按名字搜（仍可走豆瓣兜底）
NO_TMDB_MARK = "none"

_tried_lock = threading.Lock()
_tried_rows: dict = {}    # person row id -> monotonic
_tried_items: dict = {}   # item id -> monotonic


def _recently_tried(memo: dict, key) -> bool:
    with _tried_lock:
        t = memo.get(key)
        return t is not None and time.monotonic() - t < RETRY_AFTER_SEC


def _mark_tried(memo: dict, key) -> None:
    with _tried_lock:
        memo[key] = time.monotonic()
        if len(memo) > 200000:
            memo.clear()


def deduplicate_persons(db) -> int:
    """删除重复的演员行（保留数据最全的一条）。返回删除数。

    一条 SQL 用窗口函数搞定（N+1 → 1）：
    按 (item_id, person_tmdb_id) 分组，优先保留有头像的行，其次 id 最小，
    其余全部删除。
    """
    from sqlalchemy import text

    # PG 与 SQLite 都支持窗口函数
    result = db.execute(text("""
        DELETE FROM emby_people
        WHERE id IN (
            SELECT id FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY item_id, person_tmdb_id
                           ORDER BY (image IS NULL OR image = ''), id ASC
                       ) AS rn
                FROM emby_people
                WHERE person_tmdb_id IS NOT NULL AND person_tmdb_id != ''
                  AND person_tmdb_id != 'none'
            ) ranked
            WHERE rn > 1
        )
    """))
    deleted = result.rowcount or 0
    if deleted:
        db.commit()
    # SQLite 的 rowcount 对 DELETE 可能返回 -1，降级为 1（表示"删过"）
    return max(deleted, 0) if deleted != -1 else 1


def _missing_image_clause(em):
    return (em.EmbyPerson.image.is_(None)) | (em.EmbyPerson.image == "")


def find_persons_missing_image(db, limit: int = 100) -> list:
    """找头像为空但有（有效）person_tmdb_id 的演员（可按 id 补全）。"""
    from backend.emby_server import models as em

    rows = (
        db.query(em.EmbyPerson)
        .filter(em.EmbyPerson.person_tmdb_id.isnot(None))
        .filter(em.EmbyPerson.person_tmdb_id != "")
        .filter(em.EmbyPerson.person_tmdb_id != NO_TMDB_MARK)
        .filter(_missing_image_clause(em))
        .order_by(em.EmbyPerson.id.desc())  # 新入库的先补
        .limit(limit * 4)
        .all()
    )
    return [p for p in rows if not _recently_tried(_tried_rows, p.id)][:limit]


def find_persons_without_tmdb_id(db, limit: int = 100) -> list:
    """头像为空、也没有 person_tmdb_id 的演员（只能按名字找）。"""
    from backend.emby_server import models as em

    rows = (
        db.query(em.EmbyPerson)
        .filter((em.EmbyPerson.person_tmdb_id.is_(None))
                | (em.EmbyPerson.person_tmdb_id == ""))
        .filter(_missing_image_clause(em))
        .order_by(em.EmbyPerson.id.desc())
        .limit(limit * 4)
        .all()
    )
    return [p for p in rows if not _recently_tried(_tried_rows, p.id)][:limit]


def _profile_url(base: str, profile: str) -> str:
    return f"{base}/w185{profile}" if profile else ""


def _search_person_by_name(client, name: str) -> Optional[dict]:
    """TMDB /search/person：只接受名字完全一致的结果（宁缺毋错）。"""
    data = client._get("/search/person", {"query": name}) or {}
    hits = [h for h in (data.get("results") or []) if isinstance(h, dict)]
    exact = [h for h in hits if str(h.get("name") or "").strip() == name.strip()]
    if not exact:
        return None
    # 多个同名：优先有头像的；仍多个就按 TMDB 热度取第一（结果本身已按热度排）
    with_img = [h for h in exact if h.get("profile_path")]
    return (with_img or exact)[0]


def refresh_person_images(db) -> int:
    """补全缺失的演员头像。返回补全数。失败不抛异常（后台任务不该崩）。

    1. 有 person_tmdb_id → TMDB ``/person/{id}``；
    2. 没有 person_tmdb_id → TMDB ``/search/person`` 按名字精确匹配，顺手补上 id
       （豆瓣/NFO 等来源写的演员、或旧数据）；搜不到记 ``none`` 不再搜；
    3. 仍缺头像的 → 按条目走豆瓣演职员页兜底（见 ``fill_from_douban``）。
    """
    from backend.emby_server.tmdb import tmdb_client, image_base

    fixed = 0
    client = tmdb_client
    if getattr(client, "configured", True):
        base = image_base()
        for p in find_persons_missing_image(db, limit=FILL_BATCH):
            _mark_tried(_tried_rows, p.id)
            try:
                data = client._get(f"/person/{p.person_tmdb_id}", {"language": "en-US"})
                profile = (data or {}).get("profile_path")
                if profile:
                    p.image = _profile_url(base, profile)
                    fixed += 1
            except Exception as exc:  # noqa: BLE001 — 单个失败不影响其他
                logger.debug("刷新演员头像失败 %s: %s", p.name, exc)
        for p in find_persons_without_tmdb_id(db, limit=FILL_BATCH):
            _mark_tried(_tried_rows, p.id)
            try:
                hit = _search_person_by_name(client, p.name or "")
            except Exception as exc:  # noqa: BLE001
                logger.debug("按名字搜演员失败 %s: %s", p.name, exc)
                continue
            if not hit:
                p.person_tmdb_id = NO_TMDB_MARK
                continue
            p.person_tmdb_id = str(hit.get("id") or "")[:32] or NO_TMDB_MARK
            if hit.get("profile_path"):
                p.image = _profile_url(base, hit["profile_path"])
                fixed += 1
        db.commit()
    try:
        fixed += fill_from_douban(db)
    except Exception as exc:  # noqa: BLE001
        logger.debug("豆瓣补演员失败: %s", exc)
    return fixed


def _items_needing_douban(db, limit: int) -> list:
    """有演员缺头像、或有汉字演员却没有角色名的剧集/电影（豆瓣能补这两样）。"""
    from backend.emby_server import models as em

    ids = [
        r[0] for r in db.query(em.EmbyPerson.item_id)
        .filter(_missing_image_clause(em) | (em.EmbyPerson.role.is_(None))
                | (em.EmbyPerson.role == ""))
        .group_by(em.EmbyPerson.item_id)
        .order_by(func_max_id(em).desc())
        .limit(limit * 10)
        .all()
    ]
    ids = [i for i in ids if not _recently_tried(_tried_items, i)]
    if not ids:
        return []
    items = (
        db.query(em.MediaItem)
        .filter(em.MediaItem.id.in_(ids),
                em.MediaItem.item_type.in_(("series", "movie")))
        .all()
    )
    return items[:limit]


def func_max_id(em):
    from sqlalchemy import func
    return func.max(em.EmbyPerson.id)


def fill_from_douban(db, limit: int = None) -> int:
    """豆瓣兜底：按条目取 ``/subject/{id}/celebrities``，按名字补头像与中文角色名。

    只补空的：头像为空才写头像；角色为空、或是汉字演员配拼音角色（TMDB 常见）时才写角色。
    """
    from backend.emby_server import douban as _dbn
    from backend.emby_server import models as em
    from backend.emby_server.tmdb import is_romanized_role

    limit = DOUBAN_ITEMS_PER_RUN if limit is None else limit
    if limit <= 0:
        return 0
    try:
        if not _dbn.enabled(db):
            return 0
        _dbn.client._interval = _dbn.min_interval(db)
    except Exception:  # noqa: BLE001
        pass
    fixed = 0
    for item in _items_needing_douban(db, limit):
        _mark_tried(_tried_items, item.id)
        if not _dbn.has_cjk(item.name or ""):
            continue
        try:
            hit = _dbn.client.search(item.name or "", item.production_year, item.item_type)
            if not hit or not hit.get("id"):
                continue
            celebs = _dbn.client.get_celebrities(hit["id"])
        except _dbn.DoubanBannedError:
            # 被封禁：本轮到此为止；本条不算试过，冷却后下一轮再补
            with _tried_lock:
                _tried_items.pop(item.id, None)
            break
        if not celebs:
            continue
        rows = db.query(em.EmbyPerson).filter(em.EmbyPerson.item_id == item.id).all()
        for p in rows:
            c = next((c for c in celebs if _dbn.name_matches(p.name, c["name"])), None)
            if not c:
                continue
            if c.get("image") and not p.image:
                p.image = c["image"][:1024]
                fixed += 1
            if c.get("role") and (not p.role or is_romanized_role(p.name, p.role)):
                p.role = c["role"][:200]
        db.commit()
    return fixed


def run_once(db) -> dict:
    """执行一轮：去重 + 补头像。返回统计。"""
    t0 = time.monotonic()
    deduped = deduplicate_persons(db)
    images_fixed = refresh_person_images(db)
    return {
        "deduplicated": deduped,
        "images_fixed": images_fixed,
        "elapsed_sec": round(time.monotonic() - t0, 2),
    }


_start_lock = threading.Lock()
_started = False
_stop_event = threading.Event()
_wake = threading.Event()


def kick() -> None:
    """有新演员缺头像（enrich 刚写入）：唤醒后台线程立刻补一轮，不用等间隔。"""
    _wake.set()


def _run_fill_once() -> None:
    from backend.database import get_db

    db = next(get_db())
    try:
        n = refresh_person_images(db)
        if n:
            logger.info("演员头像补全 %d 条", n)
    finally:
        db.close()


def _run_loop_once() -> None:
    """跑一轮（供 _loop 与启动即跑共用）。"""
    from backend.database import get_db

    db = next(get_db())
    try:
        stats = run_once(db)
        logger.info("演员刷新完成: %s", stats)
    finally:
        db.close()


def start() -> bool:
    """启动定时任务（后台线程，启动即跑一轮，之后每 24 小时跑一轮）。

    幂等：重复调用只起一个线程。
    """
    global _started
    with _start_lock:
        if _started:
            return True
        _started = True

    def _loop():
        # 启动即跑一轮（与其它定时任务惯例一致），再按间隔 sleep
        try:
            _run_loop_once()
        except Exception as exc:  # noqa: BLE001 — 后台任务不崩
            logger.warning("演员刷新失败: %s", exc)
        finally:
            from backend.emby_server import worker_registry as _wr
            _wr.heartbeat("refresh_person")
        last_full = time.monotonic()
        while not _stop_event.is_set():
            try:
                # 补头像走短间隔（或被 kick 唤醒）；去重仍按全量间隔
                _wake.wait(max(1, min(FILL_INTERVAL_SEC, REFRESH_INTERVAL_SEC)))
                _wake.clear()
                if _stop_event.is_set():
                    break
                if time.monotonic() - last_full >= REFRESH_INTERVAL_SEC:
                    last_full = time.monotonic()
                    _run_loop_once()
                else:
                    _run_fill_once()
            except Exception as exc:  # noqa: BLE001 — 后台任务不崩
                logger.warning("演员刷新失败: %s", exc)
            finally:
                from backend.emby_server import worker_registry as _wr
                _wr.heartbeat("refresh_person")

    t = threading.Thread(target=_loop, daemon=True, name="refresh-person")
    t.start()
    from backend.emby_server import worker_registry as _wr
    _wr.register("refresh_person", t)
    return True


def stop() -> None:
    """停止后台线程（与其他 worker 对齐，测试/优雅关闭用）。"""
    _stop_event.set()
    _wake.set()
