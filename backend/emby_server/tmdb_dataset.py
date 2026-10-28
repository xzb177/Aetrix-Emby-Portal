"""302 Next A1：本地 TMDB 数据集。

搜索只读本表，不在请求时现调 TMDB（硬口径）。数据集的增长只来自
同步任务（本模块的 ``sync_once`` + ``start_dataset_scheduler``），
绝不由搜索请求触发现调——否则每次详情现调 TMDB 又慢又容易被限流，
「302 Next」的体验就立不住。

字段只存展示用的（标题、原名、年份、简介、类型名、海报路径、评分），
不存版权文本全文，不存任何密钥与用户数据。
同步复用既有 ``TmdbClient``（key 池、限流、熔断都在它里面），不另起
一套 TMDB 调用；未配置 key 时同步干净跳过，不抛、不重试、不写坏数据。

新鲜度：``TmdbDatasetMeta`` 单行记 last_success_at / total_rows /
last_error；同步断了会悄悄变旧，所以失败也要记，搜索响应里带出
新鲜度，让用户看得到数据新不新鲜。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime

from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server import next302_gate as gate

logger = logging.getLogger(__name__)

SYNC_LISTS = ("trending", "popular", "top_rated")
MEDIA_TYPES = ("movie", "tv")

_SCHEDULER_STARTED = False
_SCHEDULER_LOCK = threading.Lock()


def _norm_row(raw: dict, media_type: str, source: str) -> dict | None:
    """TMDB 原始 dict → 数据集行（只取展示字段）。没 tmdb id 的不要。"""
    tid = raw.get("id")
    if tid is None:
        return None
    title = (raw.get("title") or raw.get("name") or "").strip()
    if not title:
        return None
    date = (raw.get("release_date") or raw.get("first_air_date") or "")
    year = None
    if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit():
        year = int(date[:4])
    return {
        "tmdb_id": str(tid),
        "media_type": media_type,
        "title": title,
        "original_title": (raw.get("original_title")
                            or raw.get("original_name") or ""),
        "year": year,
        "overview": raw.get("overview") or "",
        "genres": raw.get("_genre_names") or [],
        "poster_path": raw.get("poster_path") or "",
        "vote_average": raw.get("vote_average"),
        "release_date": date if isinstance(date, str) else "",
        "source": source,
    }


def upsert_items(db: Session, rows: list[dict]) -> tuple[int, int]:
    """按 (tmdb_id, media_type) upsert。返回 (新增数, 更新数)。"""
    inserted = 0
    updated = 0
    now = datetime.now()
    for row in rows:
        if not row.get("tmdb_id") or not row.get("media_type"):
            continue
        existing = (
            db.query(em.TmdbDatasetItem)
            .filter(em.TmdbDatasetItem.tmdb_id == row["tmdb_id"],
                    em.TmdbDatasetItem.media_type == row["media_type"])
            .first()
        )
        genres_json = json.dumps(row.get("genres") or [], ensure_ascii=False)
        if existing is None:
            db.add(em.TmdbDatasetItem(
                tmdb_id=row["tmdb_id"],
                media_type=row["media_type"],
                title=row["title"],
                original_title=row.get("original_title") or "",
                year=row.get("year"),
                overview=row.get("overview") or "",
                genres_json=genres_json,
                poster_path=row.get("poster_path") or "",
                vote_average=row.get("vote_average"),
                release_date=row.get("release_date") or "",
                source=row.get("source") or "",
                updated_at=now, last_synced_at=now))
            inserted += 1
        else:
            existing.title = row["title"]
            existing.original_title = row.get("original_title") or ""
            existing.year = row.get("year")
            existing.overview = row.get("overview") or ""
            existing.genres_json = genres_json
            existing.poster_path = row.get("poster_path") or ""
            existing.vote_average = row.get("vote_average")
            existing.release_date = row.get("release_date") or ""
            existing.source = row.get("source") or ""
            existing.updated_at = now
            existing.last_synced_at = now
            updated += 1
    db.flush()
    return inserted, updated


def _like_escape(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _rank_key(q_lower: str, title_lower: str, in_library: bool,
              vote: float | None) -> tuple:
    """排序：标题全等 → 前缀 → 包含；同档库内在前；再按评分降序。"""
    if title_lower == q_lower:
        tier = 0
    elif title_lower.startswith(q_lower):
        tier = 1
    else:
        tier = 2
    return (tier, 0 if in_library else 1, -(vote or 0.0), title_lower)


def search_local(db: Session, q: str, media_type: str | None = None,
                 limit: int = 20,
                 allowed_library_ids: set[int] | None = None,
                 library_known: bool = True) -> dict:
    """本地数据集搜索 + 库内混排（不调 TMDB、不建任务、不写库）。

    库可见范围三态（调用方按 ``effective_ids`` 原口径算，绝不猜）：
    - ``library_known=False``（范围读失败）：只回数据集行，且每行
      ``library_known=False``，前端不许把任何一行标成「库外」——
      因为库内关系没算出来，不是真没有。
    - ``library_known=True`` 且 ``allowed_library_ids=None``（工作人员
      豁免 / 不过滤）：库内标记不过滤。
    - ``library_known=True`` 且 ``allowed_library_ids`` 为集合：只标
      该集合内的库；空集 = 一个可见库都没有，标记全空（fail-closed）。
    """
    key = (q or "").strip()
    if not key:
        return {"results": [], "total": 0, "freshness": get_freshness(db)}
    like = f"%{_like_escape(key)}%"
    query = db.query(em.TmdbDatasetItem).filter(
        (em.TmdbDatasetItem.title.ilike(like, escape="\\"))
        | (em.TmdbDatasetItem.original_title.ilike(like, escape="\\")))
    if media_type in MEDIA_TYPES:
        query = query.filter(em.TmdbDatasetItem.media_type == media_type)
    # 先按标题近似取一批再精排：数据集量大时 ILIKE 全表扫，候选集封顶。
    candidates = query.limit(300).all()

    tmdb_ids = [c.tmdb_id for c in candidates]
    lib_map: dict[str, em.MediaItem] = {}
    if tmdb_ids and library_known:
        lib_query = (
            db.query(em.MediaItem)
            .filter(em.MediaItem.tmdb_id.in_(tmdb_ids),
                    em.MediaItem.deleted_at.is_(None),
                    em.MediaItem.merged_into_id.is_(None))
        )
        if allowed_library_ids is not None:
            lib_query = lib_query.filter(
                em.MediaItem.library_id.in_(allowed_library_ids))
        lib_rows = (
            lib_query
            .order_by(em.MediaItem.tmdb_id,
                      em.MediaItem.item_type,
                      em.MediaItem.id)
            .all()
        )
        # 同 tmdb_id 多行时留主条（movie/series 优先于季/集，id 最小）。
        for row in lib_rows:
            prev = lib_map.get(row.tmdb_id)
            if prev is None:
                lib_map[row.tmdb_id] = row
            elif prev.item_type not in ("movie", "series") \
                    and row.item_type in ("movie", "series"):
                lib_map[row.tmdb_id] = row

    q_lower = key.lower()
    results = []
    for c in candidates:
        lib = lib_map.get(c.tmdb_id)
        results.append(_dataset_dict(
            c, in_library=lib is not None,
            library_item_id=lib.id if lib is not None else None,
            library_known=library_known))
    # 库内但不在数据集里的条目也混排进来（库内条目可能还没被同步到）。
    if library_known:
        seen = {c.tmdb_id for c in candidates}
        extra_query = (
            db.query(em.MediaItem)
            .filter(em.MediaItem.name.ilike(like, escape="\\"),
                    em.MediaItem.deleted_at.is_(None),
                    em.MediaItem.merged_into_id.is_(None))
        )
        if allowed_library_ids is not None:
            extra_query = extra_query.filter(
                em.MediaItem.library_id.in_(allowed_library_ids))
        extra = (
            extra_query
            .order_by(em.MediaItem.id)
            .limit(100)
            .all()
        )
        for row in extra:
            if row.tmdb_id and row.tmdb_id in seen:
                continue
            results.append({
                "tmdb_id": row.tmdb_id or "",
                "media_type": "tv" if row.item_type == "series"
                              else "movie",
                "title": row.name,
                "original_title": row.original_title or "",
                "year": row.production_year,
                "overview": row.overview or "",
                "genres": [],
                "poster_path": "",
                "vote_average": None,
                "in_library": True,
                "library_item_id": row.id,
                "library_known": True,
                "dataset_only": False,
            })
    results.sort(key=lambda r: _rank_key(
        q_lower, (r["title"] or "").lower(), r["in_library"],
        r["vote_average"]))
    total = len(results)
    return {"results": results[:max(1, limit)], "total": total,
            "freshness": get_freshness(db)}


def _dataset_dict(row: em.TmdbDatasetItem, *, in_library: bool,
                  library_item_id: int | None,
                  library_known: bool) -> dict:
    try:
        genres = json.loads(row.genres_json or "[]")
    except (ValueError, TypeError):
        genres = []
    return {
        "tmdb_id": row.tmdb_id,
        "media_type": row.media_type,
        "title": row.title,
        "original_title": row.original_title or "",
        "year": row.year,
        "overview": row.overview or "",
        "genres": genres if isinstance(genres, list) else [],
        "poster_path": row.poster_path or "",
        "vote_average": row.vote_average,
        "in_library": in_library,
        "library_item_id": library_item_id,
        "library_known": library_known,
        "dataset_only": True,
    }


def get_freshness(db: Session) -> dict:
    """数据集新鲜度：没同步过也明说，不编一个时间。"""
    meta = db.query(em.TmdbDatasetMeta).filter(
        em.TmdbDatasetMeta.id == 1).first()
    if meta is None:
        return {"last_success_at": None, "total_rows": 0,
                "last_error": None, "synced": False}
    return {
        "last_success_at": meta.last_success_at.isoformat()
        if meta.last_success_at else None,
        "total_rows": meta.total_rows or 0,
        "last_error": meta.last_error,
        "synced": meta.last_success_at is not None,
    }


def _write_meta(db: Session, *, success: bool, total_rows: int,
                error: str | None) -> None:
    now = datetime.now()
    meta = db.query(em.TmdbDatasetMeta).filter(
        em.TmdbDatasetMeta.id == 1).first()
    if meta is None:
        meta = em.TmdbDatasetMeta(id=1)
        db.add(meta)
    meta.last_sync_at = now
    if success:
        meta.last_success_at = now
    # 成功与失败原因不互相掩盖：部分失败时 last_success_at 照样前进，
    # 但 last_error 仍留着失败信息；全成功时 error=None 自然清掉。
    meta.last_error = error
    meta.total_rows = total_rows
    meta.updated_at = now
    db.flush()


def sync_once(db: Session, client=None, pages_per_list: int = 2) -> dict:
    """跑一轮数据集同步（榜单页拉取 + upsert + 新鲜度）。

    未配置 TMDB key 时干净跳过（status=skipped），不抛、不重试。
    单榜单/单页失败只记 last_error，不中断整轮；至少一页成功即算
    本轮成功（last_success_at 前进），失败信息仍留在 last_error 里
    供观测——成功与失败原因不互相掩盖。
    """
    from backend.emby_server.tmdb import TmdbClient
    if client is None:
        client = TmdbClient()
    if getattr(client, "key_source", "none") == "none" \
            and not getattr(client, "api_keys", []):
        _write_meta(db, success=False, total_rows=_count_rows(db),
                    error="no_tmdb_key（未配置 TMDB key，同步跳过）")
        db.commit()
        return {"status": "skipped", "reason": "no_tmdb_key",
                "fetched": 0, "inserted": 0, "updated": 0, "errors": []}
    errors: list[str] = []
    fetched = 0
    inserted = 0
    updated = 0
    genre_maps: dict[str, dict] = {}
    for list_name in SYNC_LISTS:
        for media_type in MEDIA_TYPES:
            if media_type not in genre_maps:
                try:
                    genre_maps[media_type] = client.genre_names(media_type)
                except Exception:  # noqa: BLE001 — 类型名拿不到就空着，不挡同步
                    genre_maps[media_type] = {}
            for page in range(1, max(1, pages_per_list) + 1):
                try:
                    raws = client.list_page(list_name, media_type, page)
                except Exception as exc:  # noqa: BLE001 — 单页失败记下继续
                    errors.append(f"{list_name}/{media_type}/p{page}: {exc}")
                    continue
                rows = []
                for raw in raws or []:
                    gids = raw.get("genre_ids") or []
                    names = [genre_maps[media_type].get(gid)
                             for gid in gids]
                    raw = dict(raw)
                    raw["_genre_names"] = [n for n in names if n]
                    norm = _norm_row(raw, media_type, list_name)
                    if norm is not None:
                        rows.append(norm)
                fetched += len(rows)
                try:
                    ins, upd = upsert_items(db, rows)
                except Exception as exc:  # noqa: BLE001 — 单页写库失败记下继续
                    db.rollback()
                    errors.append(
                        f"{list_name}/{media_type}/p{page} 写库失败: {exc}")
                    continue
                inserted += ins
                updated += upd
    _write_meta(db, success=not errors or inserted > 0 or updated > 0,
                total_rows=_count_rows(db),
                error="; ".join(errors) if errors else None)
    db.commit()
    return {"status": "ok" if not errors else "partial",
            "fetched": fetched, "inserted": inserted, "updated": updated,
            "errors": errors}


def _count_rows(db: Session) -> int:
    return db.query(em.TmdbDatasetItem).count()


def start_dataset_scheduler() -> bool:
    """启动数据集定时同步线程（同一进程只启动一次；daemon）。

    默认关：总闸或数据集子开关任一关着都直接返回 False，不起线程、
    不发任何 TMDB 请求。worker.py 里按既有调度器口径调用，失败可忽略。
    """
    global _SCHEDULER_STARTED
    with _SCHEDULER_LOCK:
        if _SCHEDULER_STARTED:
            return False
        _SCHEDULER_STARTED = True

    def _loop() -> None:
        from backend.database import SessionLocal
        while True:
            try:
                if gate.dataset_enabled():
                    db = SessionLocal()
                    try:
                        sync_once(db)
                    finally:
                        db.close()
            except Exception:  # noqa: BLE001 — 同步线程绝不因一次异常死掉
                logger.exception("302 Next 数据集同步失败（下轮重试）")
            time.sleep(gate.dataset_sync_interval_sec())

    thread = threading.Thread(target=_loop, name="next302-dataset-sync",
                              daemon=True)
    thread.start()
    return True
