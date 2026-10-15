"""自建 Emby 服务器：Emby 协议兼容 API

覆盖 Emby 客户端核心接口：
- 系统信息 / 认证（Users/AuthenticateByName, /Users/{id}）
- 项目列表 / 详情 / 精选（/Users/{uid}/Items, /Users/{uid}/Items/Resume, Latest）
- 播放信息（PlaybackInfo, /Videos/{id}/stream, /Videos/{id}/master.m3u8, /videos/{id}/hls1/...）
- 图片（/Items/{id}/Images/Primary|Backdrop|Logo|Thumb）
- 会话上报（/Sessions/Playing, /Sessions/Playing/Progress, /Sessions/Playing/Stopped）
- 收藏/已看（/Users/{uid}/FavoriteItems, PlayedItems, /Users/{uid}/Items/{iid}/Rating）
- 电视首页（/Shows/{id}/Seasons, /Shows/{id}/Episodes, /Shows/NextUp）
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import secrets
import shutil
import time
import urllib.parse
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session, aliased, joinedload

from backend import library_scope, models, playback_policy
from backend.database import SessionLocal, get_db
from backend.emby_server import cdn
from backend.emby_server.async_db import release_db_before_response
from backend.emby_server import facets
from backend.emby_server.fastjson import json_route
from backend.emby_server import image_store
from backend.emby_server import line_stats
from backend.emby_server import dedup as dedup_lib
from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib
from backend.emby_server import play_sign
from backend.emby_server import playback_prewarm
from backend.emby_server.query_params import qlimit, qstart
from backend.emby_server import soft_delete
from backend.emby_server import title_beautify
from backend.emby_server import missing_episodes as _missing_episodes
from backend.emby_server.playback_security import safe_child_name
from backend.emby_server import subtitles as subs
from backend.emby_server.auth import (
    _token_from_request,
    get_emby_user,
    get_play_user,
    parse_emby_authorization,
)
from backend.emby_server.facets import count_virtual_items  # 索引版（虚拟库条目数）
from backend.emby_server.scanner import (
    ScanInProgress,
    parse_media_filename,
    scan_library_sync,
)
from backend.emby_server.search import (
    CANDIDATE_LIMIT as SEARCH_CANDIDATE_LIMIT,
    rank_items,
    search_variants,
)
from backend.emby_server.streaming import (
    active_transcode_ids,
    get_transcode,
    transcode_capacity,
    serve_file,
    touch_transcode,
    serve_image,
    serve_remote,
    serve_remote_async,
    start_transcode,
    find_transcodes,
    stop_all_transcodes,
    stop_all_transcodes_async,
    stop_transcode,
    stop_transcode_async,
    stop_transcodes_for,
    stop_transcodes_for_async,
    stop_user_transcodes,
    stop_user_transcodes_async,
    transcode_alive,
    wait_for_file,
)
from backend.subscriptions import ensure_download_allowed, ensure_playback_allowed

logger = logging.getLogger(__name__)

emby_router = APIRouter(tags=["EmbyServer"])

TICKS = 10_000_000
SERVER_VERSION = "4.8.0.0"
# v2.30.0：媒体服务器身份默认值跟着品牌统一。改品牌后没有显式设置 EMBY_SERVER_ID 的
# 部署，客户端会把服务器认成新的一台（重登一次即可）；想避免就在 .env 里保留旧值。
SERVER_ID = os.getenv("EMBY_SERVER_ID", "aetrix-emby-server")


def item_guid_for(db: Session, item_id) -> Optional[str]:
    """播放会话的 ``item_id`` → 条目 guid

    转码会话在 ``streaming._TRANSCODE_PROCS`` 里按 guid 记（那个 uuid 只出现在 HLS 播放列表的
    ``?session=`` 上），而「结束播放」手上只有播放会话的 ``item_id``；两者之间需要这一座桥。
    旧实现在那里直接拿播放会话键去 pop，键对不上，等于什么都没停到。
    """
    if not item_id:
        return None
    row = db.query(em.MediaItem.guid).filter(em.MediaItem.id == item_id).first()
    return row[0] if row else None


def _iso(dt) -> str:
    """ISO8601 with timezone suffix for iOS strict parsing.
    iOS JSONDecoder requires 'Z' or offset; naive datetime gets 'Z'.
    Emby/.NET 小数秒恒为 7 位 tick 精度，6 位会被严格客户端拒收。"""
    if not dt:
        return None
    s = dt.isoformat()
    # Emby/.NET 小数秒恒为 7 位 tick 精度；6 位会被严格客户端拒收。
    t = s.find("T")
    if t != -1:
        dot = s.find(".", t)
        if dot != -1:
            end = dot + 1
            while end < len(s) and s[end].isdigit():
                end += 1
            if end - dot - 1 == 6:
                s = s[:end] + "0" + s[end:]
    # 如果没有时区信息，补 Z（按 UTC 处理）
    if s[-1] not in ("Z", "+", "-") or (s[-1] in ("+", "-") and "T" not in s):
        # 检查是否已有时区偏移（如 +08:00）
        if "+" not in s[10:] and s.count("-") <= 2:
            s += "Z"
    return s


def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _public_base_url(request: Request) -> str:
    """System/Info 专用的对外地址：干净的公开地址，不带反代内部痕迹。

    优先 EMBY_PUBLIC_URL 环境变量（部署方明确配的对外地址）；没配时按反代头
    还原：协议取 X-Forwarded-Proto、主机取 X-Forwarded-Host/Host，并去掉与
    协议对应的默认端口（https:443 / http:80，非默认端口保留）。
    以前这里直接回显 request.base_url：TLS 在反代终结时 scheme 变 http、
    客户端 Host 带 :443 时端口也被原样带回，严格客户端（ddPlayer 等）
    会因为地址对不上而拒收整份服务器信息。
    """
    env = os.getenv("EMBY_PUBLIC_URL", "").rstrip("/")
    if env.startswith(("http://", "https://")):
        return env
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "http").split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or "").split(",")[0].strip()
    if not host:
        return str(request.base_url).rstrip("/")
    if (proto == "https" and host.endswith(":443")) or (proto == "http" and host.endswith(":80")):
        host = host.rsplit(":", 1)[0]
    return f"{proto}://{host}"


def _visible_in_session(it: em.MediaItem) -> bool:
    """identity map 里取到的条目是否「查得到」（与软删除查询钩子同口径）"""
    if it.deleted_at is None:
        return True
    from backend.emby_server import soft_delete
    return not soft_delete.soft_delete_enabled() or soft_delete._allow_deleted.get()


def _image_chain(item: em.MediaItem, kind: str, db: Session) -> list[str]:
    """图片回退链：条目自身 → 季 → 剧集海报

    季与集常常没有自己的图片；不回退就会整库空白图。
    """
    def srcs(it: em.MediaItem) -> list[str]:
        if kind == "Primary":
            remote, local = it.primary_image_url or "", it.poster_path or ""
        else:
            remote, local = it.backdrop_image_url or "", it.backdrop_path or ""
        # 刮削图片本地化的那份缓存（在我们自己的图片目录里）优先走本地，且必须真在磁盘上：
        # 缓存被清掉（/tmp 重启后清空、维护周期淘汰）时直接退回远程图，
        # 取图时会按需再落一份（media_routes.item_image），行为与本地化之前完全一致。
        if local and image_store.is_cached_path(local) and os.path.isfile(local):
            return [local, remote]
        return [remote, local]

    chain = srcs(item)
    if item.item_type in ("episode", "season"):
        parents: list[em.MediaItem] = []
        if item.parent_id:
            # db.get 先查 identity map：列表接口 _prefetch_list_data 已把季/剧加载进会话，
            # 命中时零 SQL（原先 query().first() 每条集/季都发一次，含集列表 N+1）。
            # 未命中时 get 照常发 SELECT，仍经过软删除 / 内容可见性钩子；命中时钩子不跑，
            # 这里补上软删除判定，结果与原查询一致。
            parent = db.get(em.MediaItem, item.parent_id)
            if parent is not None and not _visible_in_session(parent):
                parent = None
            if parent:
                parents.append(parent)
        series = item.series
        if series is not None and series not in parents:
            parents.append(series)
        for parent in parents:
            chain.extend(srcs(parent))
    return [s for s in chain if s]


def _first_image(item: em.MediaItem, kind: str, db: Session) -> str | None:
    chain = _image_chain(item, kind, db)
    return chain[0] if chain else None


def season_poster_url(item: em.MediaItem, db: Session) -> str | None:
    """季条目的海报地址（含父剧集回退），供门户 API 序列化用。

    渲染顺序：季自身图 → 父剧集图 → None（前端显示文字占位）。
    回退口径与 ``_image_chain`` 一致：parent_id 优先（季的直接父），
    series_id 兜底；父条目须通过软删除可见性检查。
    只对 ``season`` 类型做回退——movie / series / episode 原样返回，
    父剧集也没图时返回 None（保持现有占位，不误伤）。
    """
    if item.poster_path or item.primary_image_url:
        return f"/emby/Items/{item.guid}/Images/Primary"
    if item.item_type != "season":
        return None
    parent = None
    if item.parent_id:
        parent = db.get(em.MediaItem, item.parent_id)
        if parent is not None and not _visible_in_session(parent):
            parent = None
    if parent is None and item.series_id:
        parent = db.get(em.MediaItem, item.series_id)
        if parent is not None and not _visible_in_session(parent):
            parent = None
    if parent is not None and (parent.poster_path or parent.primary_image_url):
        return f"/emby/Items/{parent.guid}/Images/Primary"
    return None


def _queue_image_repair(db: Session, item: em.MediaItem, stale_src: str) -> None:
    """把“数据库有图、取不到图”的条目排进修复队列

    重新刮削时会换成 TMDB 远程图；同时把失效的本地路径清掉，避免每次请求都白读一次磁盘。
    """
    try:
        changed = False
        if stale_src and not stale_src.startswith("http"):
            if item.poster_path == stale_src:
                item.poster_path = None
                changed = True
            if item.backdrop_path == stale_src:
                item.backdrop_path = None
                changed = True
        if item.repair_requested_at is None:
            item.repair_requested_at = datetime.now()
            changed = True
        if changed:
            db.commit()
    except Exception as e:  # noqa: BLE001 — 修复排队失败不能影响图片接口本身
        logger.warning("图片修复排队失败 item=%s: %s", item.guid, e)
        db.rollback()


def _image_url(base: str, item: em.MediaItem, kind: str = "Primary") -> str | None:
    if kind == "Primary":
        src = item.primary_image_url or item.poster_path
    else:
        src = item.backdrop_image_url or item.backdrop_path
    if not src:
        return None
    if src.startswith("http"):
        return src
    return f"{base}/emby/Items/{item.guid}/Images/{kind}"


def _download_ok(db: Session) -> bool:
    """站点是否允许下载（按 Session 缓存，避免列表里每个条目都查一次配置）"""
    cache = db.info.setdefault("_aetrix_download_ok", {})
    if "value" not in cache:
        from backend.subscriptions import download_allowed

        cache["value"] = download_allowed(db)
    return cache["value"]


# ==================== 列表批量预取（消除 N+1）====================

def _prefetch_list_data(db: Session, user_id: int, items: list[em.MediaItem]) -> None:
    """一次性预取列表页需要的关联数据，消除 _item_dto 的 N+1 查询

    旧实现：一页 100 个条目 = 100 次 UMD 查询 + series/parent 惰性加载
    （每集 2 次）+ series/season 的 ChildCount 各 1 次 ≈ 400+ 次查询。
    客户端首页一次要拉 Resume/Latest/NextUp/多库列表，全站延迟被这些
    小查询成倍放大——这正是比 Emby 慢的主因之一。

    预取结果挂在 ``db.info``（随请求会话销毁，不会跨请求串数据），
    _item_dto 优先用预取结果，单独取条目（详情页等）时回退为原查询。
    """
    if not items:
        return
    item_ids = [i.id for i in items]

    # 1) 用户媒体数据（进度/收藏/已看）
    umd_map: dict[int, em.UserMediaData] = {
        u.item_id: u
        for u in db.query(em.UserMediaData).filter(
            em.UserMediaData.user_id == user_id,
            em.UserMediaData.item_id.in_(item_ids),
        ).all()
    }

    # 2) 集的 series / parent（一次性载入，避免逐条惰性加载）
    series_ids = {i.series_id for i in items if i.series_id}
    parent_ids = {i.parent_id for i in items if i.parent_id}
    relations: dict[int, em.MediaItem] = {}
    related_ids = (series_ids | parent_ids) - set(item_ids)
    if related_ids:
        relations = {
            r.id: r
            for r in db.query(em.MediaItem).filter(em.MediaItem.id.in_(related_ids)).all()
        }
    for it in items:
        if it.series_id is not None and it.series_id not in item_ids:
            it.series = relations.get(it.series_id)
        if it.parent_id is not None and it.parent_id not in item_ids:
            it.parent = relations.get(it.parent_id)

    # 3) series / season 的子项计数（ChildCount）
    counts: dict[int, int] = {}
    series_ids_all = [i.id for i in items if i.item_type == "series"]
    season_ids_all = [i.id for i in items if i.item_type == "season"]
    if series_ids_all:
        rows = (
            db.query(em.MediaItem.series_id, func.count(em.MediaItem.id))
            .filter(em.MediaItem.series_id.in_(series_ids_all),
                    em.MediaItem.item_type == "episode",
                    em.MediaItem.merged_into_id.is_(None))
            .group_by(em.MediaItem.series_id)
            .all()
        )
        counts.update({sid: n for sid, n in rows})
    if season_ids_all:
        rows = (
            db.query(em.MediaItem.parent_id, func.count(em.MediaItem.id))
            .filter(em.MediaItem.parent_id.in_(season_ids_all))
            .group_by(em.MediaItem.parent_id)
            .all()
        )
        counts.update({pid: n for pid, n in rows})

    # 3b) series 未播放集数（卡片「未看 N 集」角标用，一条分组 SQL，不逐卡查询）
    # 单集多版本：被合并的版本不计入（与上面 total 计数、_item_dto 回退查询同口径，
    # 否则同一集的多个版本会把角标虚高）。
    unplayed: dict[int, int] = {}
    if series_ids_all:
        rows = (
            db.query(em.MediaItem.series_id, func.count(em.MediaItem.id))
            .filter(em.MediaItem.series_id.in_(series_ids_all),
                    em.MediaItem.item_type == "episode",
                    em.MediaItem.merged_into_id.is_(None))
            .outerjoin(em.UserMediaData,
                       (em.UserMediaData.item_id == em.MediaItem.id)
                       & (em.UserMediaData.user_id == user_id))
            .filter(em.UserMediaData.played.isnot(True))
            .group_by(em.MediaItem.series_id)
            .all()
        )
        unplayed = {sid: n for sid, n in rows}

    # 4) 演员表（v2.51.0：详情页 People 用，一条 IN 查询，不逐条 N+1）
    people_map: dict[int, list] = {}
    # 集/季没有自己的演员行（_apply_cast 只给 series/movie 写），详情页回退到
    # 所属剧集的演员表——剧集 id 一并预取，仍是一条 IN 查询。
    people_ids = set(item_ids) | {
        i.series_id for i in items
        if i.series_id and i.item_type in ("episode", "season")}
    people_rows = (
        db.query(em.EmbyPerson)
        .filter(em.EmbyPerson.item_id.in_(people_ids))
        .order_by(em.EmbyPerson.item_id, em.EmbyPerson.sort_order, em.EmbyPerson.id)
        .all()
    )
    for p in people_rows:
        people_map.setdefault(p.item_id, []).append(p)

    db.info["_aetrix_prefetch"] = {
        "umd": umd_map,
        "counts": counts,
        "unplayed": unplayed,
        "people": people_map,
        "items": {i.id: i for i in items},
    }


def _prefetched(db: Session) -> dict:
    return db.info.get("_aetrix_prefetch") or {}


def _parent_dir(file_path):
    """取文件所在目录（多版本分组键）。"""
    if not file_path or "/" not in file_path:
        return None
    return file_path.rsplit("/", 1)[0]


def _version_base_name(file_path):
    """文件名去掉扩展名和" - 版本后缀"后的主名。

    例如 "1314容祖儿演唱会 (2014) - 1080p.mkv" -> "1314容祖儿演唱会 (2014)"。
    同一资源的不同版本通常只有" - "后面的版本描述不同。
    """
    if not file_path or "/" not in file_path:
        return None
    fname = file_path.rsplit("/", 1)[1]
    if "." in fname:
        fname = fname.rsplit(".", 1)[0]
    if " - " in fname:
        fname = fname.split(" - ", 1)[0]
    return fname.strip() or None


def _series_source_dir(item, db):
    if item.item_type != "series":
        return None
    row = (
        db.query(em.MediaItem.file_path)
        .filter(
            em.MediaItem.parent_id.in_(
                db.query(em.MediaItem.id).filter(
                    em.MediaItem.parent_id == item.id,
                    em.MediaItem.item_type == "season",
                )
            ),
            em.MediaItem.item_type == "episode",
            em.MediaItem.file_path.isnot(None),
        )
        .first()
    )
    if not row or not row[0]:
        return None
    fp = row[0]
    parts = fp.split("/")
    for i, p in enumerate(parts):
        if p.startswith("Season"):
            return "/".join(parts[:i])
    return _parent_dir(fp)

def _series_siblings(item, db):
    if item.item_type != "series":
        return []
    src = _series_source_dir(item, db)
    if not src:
        return []
    cands = (
        db.query(em.MediaItem)
        .filter(
            em.MediaItem.library_id == item.library_id,
            em.MediaItem.item_type == "series",
            em.MediaItem.name == item.name,
            em.MediaItem.is_hidden == False,
        )
        .order_by(em.MediaItem.id)
        .all()
    )
    return [s for s in cands if _series_source_dir(s, db) == src]


def _version_siblings(item, db):
    """找同一电影的所有版本：同库、同目录、**同主文件名**的 movie 条目。

    只对 movie 类型生效（剧集的单集即便同目录也是不同集，不合并）。
    用文件名（而非数据库 name 字段）分组：name 字段常带"576p""DIY中字"等版本后缀，
    导致同一资源的不同版本 name 不同，无法合并；文件名去掉" - 版本"后缀后才是真同名。
    """
    if item.item_type != "movie":
        return []
    pdir = _parent_dir(item.file_path)
    base = _version_base_name(item.file_path)
    if not pdir or not base:
        return []
    cands = (
        db.query(em.MediaItem)
        .filter(
            em.MediaItem.library_id == item.library_id,
            em.MediaItem.item_type == "movie",
            em.MediaItem.is_hidden == False,
            em.MediaItem.file_path.like(pdir + "/%"),
        )
        .order_by(em.MediaItem.id)
        .all()
    )
    # 二次过滤：主文件名相同才算同一版本组（避免同目录下不同电影误合并）
    return [s for s in cands if _version_base_name(s.file_path) == base]


def _version_label(item):
    """版本显示名：对标标准 Emby。

    标准 Emby 口径（官方 Wiki "Multiversion movies"）：" - " 后面的文本原样
    作为版本名展示，不拼接其他信息。大小由 Versions[].Size 字段单独下发，
    客户端自行展示，不要塞进 Name 里。
    无 " - " 后缀时（标准 Emby 不会把这类文件并为多版本，我们的分组更宽松），
    按分辨率给一个标准 "Xp" 命名。
    """
    fp = item.file_path or ""
    name = fp.rsplit("/", 1)[-1] if "/" in fp else fp
    if "." in name:
        name = name.rsplit(".", 1)[0]
    if " - " in name:
        return name.split(" - ", 1)[1]
    h = item.height or 0
    if h >= 2160:
        return "4K"
    if h >= 1080:
        return "1080p"
    if h >= 720:
        return "720p"
    return "480p"


def _item_etag(item: em.MediaItem) -> str:
    """条目 ETag（官方 BaseItemDto.Etag，DtoService 按 ``item.GetEtag(user)`` 下发）

    客户端拿它做列表/详情的缓存失效：任何会改变展示的元数据（标题、入库时间、
    简介、海报、时长、大小）变了，Etag 就变，缓存自动重拉。
    emby_items 没有 updated_at 列，所以把“会影响展示的列”拼进哈希。
    """
    raw = "|".join(
        str(x) for x in (
            item.guid, item.name, item.sort_name, item.date_added,
            item.overview, item.poster_path, item.duration_ticks, item.size,
        )
    )
    return hashlib.md5(raw.encode("utf-8", "ignore")).hexdigest()


def _display_name(item: em.MediaItem, series_names: dict | None = None) -> str:
    """展示用标题：分集走标题美化（占位/垃圾名友好化），其他类型原样。

    纯展示层（StrmAssistant 对标），不写库、不调外部 API，默认生效。
    """
    if item.item_type == "episode":
        series_name = (series_names or {}).get(item.series_id) if item.series_id else None
        return title_beautify.beautify_episode_title(
            item.name, item.file_path, item.season_number,
            item.episode_number, series_name)
    return item.name or ""


def _item_dto(item: em.MediaItem, base: str, user_id: int, db: Session, full: bool = False,
              api_key: str = "", auth_qs: str = "",
              series_names: dict | None = None) -> dict:
    download_ok = _download_ok(db)
    prefetch = _prefetched(db)
    umd = prefetch.get("umd", {}).get(item.id)
    if umd is None and item.id not in prefetch.get("items", {}):
        umd = (
            db.query(em.UserMediaData)
            .filter(em.UserMediaData.user_id == user_id, em.UserMediaData.item_id == item.id)
            .first()
        )
    # 对标 FakEmby/官方 Emby：数组/map 字段必须发 []/{}, 不能省略也不能 null。
    # 三方客户端（SenPlayer/Lenna）对这些字段裸调 .length/.filter，null 直接崩。
    dto = {
        "Name": _display_name(item, series_names),
        "SortName": item.sort_name or item.name or "",
        "Id": item.guid,
        "ServerId": SERVER_ID,
        "Etag": _item_etag(item),
        "Type": _emby_type(item.item_type),
        "IsFolder": item.item_type in ("series", "season"),
        "MediaType": "Video",
        "LocationType": "FileSystem",
        "IsHidden": False,
        "CanDelete": False,
        "CanDownload": download_ok,
        "SupportsSync": False,
        "SupportsContentDownloading": download_ok,
        "LockData": False,
        "LockedFields": [],
        # 数组类：必须 []，不能 null/省略
        "Genres": [g for g in (item.genres or "").split(",") if g],
        "GenreItems": [],
        "Tags": [t for t in (item.tags or "").split(",") if t],
        "Taglines": [],
        "Studios": [{"Name": s, "Id": s} for s in (item.studios or "").split(",") if s],
        # v2.51.0：国家/语言由 TMDB details 落库（origin_country / spoken_languages）；
        # 没刮到的条目仍是 []（与以前一致，客户端按 [] 处理）。
        "Countries": [c for c in (item.countries or "").split(",") if c],
        "Languages": [l for l in (item.languages or "").split(",") if l],
        # v2.51.0：演员表走 emby_people（TMDB credits 刮削），不再硬编码 []。
        "People": _people_dto(item, db, prefetch),
        "RemoteTrailers": [],
        "ExternalUrls": _external_urls(item),
        "Subviews": [],
        "BackdropImageTags": (["1"] if (
            item.backdrop_path or item.backdrop_image_url
            or (item.item_type in ("episode", "season")
                and _first_image(item, "Backdrop", db))
        ) else []),
        # 集/季通常不单独存 TMDB 图片；图片接口会按「自身 → 季 → 剧」回退。
        # 这里的 ImageTags 也必须反映这条回退链，否则客户端根本不会发图片请求，
        # 详情页就会显示灰色占位图（数据库明明已有剧集海报）。
        "ImageTags": {
            "Primary": "1"
        } if (item.poster_path or item.primary_image_url
               or (item.item_type in ("episode", "season")
                   and _first_image(item, "Primary", db))) else {},
        # 官方 AttachBasicFields 无条件置空字典（有模糊图时才填）；三方客户端裸读
        "ImageBlurHashes": {},
        "ProviderIds": {
            k: v for k, v in (
                ("Tmdb", item.tmdb_id),
                ("Imdb", item.imdb_id),
                ("Douban", getattr(item, "douban_id", None)),
                ("Bangumi", getattr(item, "bangumi_id", None)),
            ) if v
        },
        "UserData": _user_data_dto(umd, item.guid),
        "MediaSources": [],
        # 三方客户端 Fields 参数常要的字段
        "Status": "Continuing",
        "PrimaryImageAspectRatio": 0.6666667,
        "SyncStatus": "Synced",
    }
    # 可选字段：有值才加
    if item.production_year:
        dto["ProductionYear"] = item.production_year
    if item.community_rating:
        dto["CommunityRating"] = item.community_rating
    if item.official_rating:
        dto["OfficialRating"] = item.official_rating
    ov = (item.overview or "")[:300] if not full else (item.overview or "")
    if ov:
        dto["Overview"] = ov
    if item.premiere_date:
        dto["PremiereDate"] = _iso(item.premiere_date)
    if item.date_added:
        dto["DateCreated"] = _iso(item.date_added)
    # 冒烟测试要求详情接口必返这两个字段；无值时给默认值
    dto["RunTimeTicks"] = _run_time_ticks(item)
    dto["Container"] = item.container or ""
    if item.bitrate:
        dto["Bitrate"] = item.bitrate
    if item.width:
        dto["Width"] = item.width
    if item.height:
        dto["Height"] = item.height
    # 文件名解析的视频信息（v2.49.0，零 Drive 调用）：Emby 兼容字段名
    if item.video_codec:
        dto["VideoCodec"] = item.video_codec
    if item.audio_codec:
        dto["AudioCodec"] = item.audio_codec
    dto["IsHD"] = bool((item.height or 0) >= 720)
    if item.original_title:
        dto["OriginalTitle"] = item.original_title
    cc = _child_count(item, db)
    if cc is not None:
        dto["ChildCount"] = cc
    if item.item_type == "series":
        # 剧集卡片「未看 N 集」角标：列表页走上面的批量预取，单个详情回退为单条查询
        unp = prefetch.get("unplayed", {}).get(item.id)
        if unp is None and item.id not in prefetch.get("items", {}):
            unp = (
                db.query(func.count(em.MediaItem.id))
                .filter(em.MediaItem.series_id == item.id,
                        em.MediaItem.item_type == "episode",
                        em.MediaItem.merged_into_id.is_(None))
                .outerjoin(em.UserMediaData,
                           (em.UserMediaData.item_id == em.MediaItem.id)
                           & (em.UserMediaData.user_id == user_id))
                .filter(em.UserMediaData.played.isnot(True))
                .scalar()
            ) or 0
        dto["UserData"]["UnplayedItemCount"] = unp or 0
    if item.item_type == "episode":
        dto.update({
            "SeriesId": item.series.guid if item.series else None,
            "SeriesName": item.series.name if item.series else None,
            "SeasonId": item.parent.guid if item.parent else None,
            "SeasonName": item.parent.name if item.parent else None,
            "ParentIndexNumber": item.season_number,
            "IndexNumber": item.episode_number,
        })
    if item.item_type == "season":
        dto.update({"SeriesId": item.series.guid if item.series else None,
                    "SeriesName": item.series.name if item.series else None,
                    "IndexNumber": item.season_number})
    if full:
        dto["MediaSources"] = [_media_source(item, base, api_key, db, auth_qs,
                                             synthesize=True)]
        dto["MediaSourceCount"] = 1
        # 片头片尾标记 → Chapters（对标 StrmAssistant #3：播放器显示"跳过片头"按钮）
        try:
            from backend.emby_server import intro_marker as _im
            dto["Chapters"] = _im.to_chapters(_im.get_markers(db, item.id))
        except Exception as exc:
            logger.debug("Chapters 加载失败 item=%s: %s", getattr(item, "id", "?"), exc)
            dto["Chapters"] = []
        # 多版本：同一目录下的其他版本 + 物理合并的版本，供详情页版本切换器使用
        # （对标 StrmAssistant MergeMultiVersionTask：合并后用户要在详情页看到并切换版本）
        if item.item_type == "movie":
            sibs = _version_siblings(item, db)
            # 物理合并的版本（merged_into_id 指向本条目）：_version_siblings
            # 只找同目录同名文件，跨目录的合并版本在这里补上
            try:
                from backend.emby_server import merge_versions_worker as _mvw
                # 如果本条目是被合并的，先找到主记录
                primary_id = item.id
                if getattr(item, "merged_into_id", None):
                    primary_id = item.merged_into_id
                merged = _mvw.get_alternate_versions(db, primary_id)
            except Exception:
                merged = []
            # 合并两个来源，按 id 去重（sibs 已按 id 排序，merged 首个是主记录）
            seen_ids = set()
            all_versions = []
            for s in list(sibs) + merged:
                if s.id not in seen_ids:
                    seen_ids.add(s.id)
                    all_versions.append(s)
            all_versions.sort(key=lambda s: s.id)
            if len(all_versions) > 1:
                dto["Versions"] = [
                    {
                        "Id": s.guid,
                        "Name": _version_label(s),
                        "Height": s.height or 0,
                        "Width": s.width or 0,
                        "Size": s.size or 0,
                        "Container": s.container or "",
                        "IsPrimary": s.id == primary_id,
                    }
                    for s in all_versions
                ]
            else:
                dto["Versions"] = []
    return dto


def _emby_type(t: str) -> str:
    return {"movie": "Movie", "series": "Series", "season": "Season", "episode": "Episode"}.get(t, "Movie")


def _child_count(item: em.MediaItem, db: Session) -> int | None:
    if item.item_type not in ("series", "season"):
        return None
    prefetch = _prefetched(db)
    counts = prefetch.get("counts", {})
    if item.id in counts:
        return counts[item.id] or None
    return (
        soft_delete.count_visible(db, em.MediaItem.series_id == item.id,
                                  em.MediaItem.item_type == "episode",
                                  em.MediaItem.merged_into_id.is_(None))
        if item.item_type == "series"
        # season 分支同样排除被合并的版本（与 series 分支同口径），
        # 否则季封面的集数会把隐藏版本也算进去
        else soft_delete.count_visible(db, em.MediaItem.parent_id == item.id,
                                       em.MediaItem.merged_into_id.is_(None))
    ) or None


def _people_dto(item: em.MediaItem, db: Session, prefetch: dict) -> list[dict]:
    """条目演员表（v2.51.0，Emby People 口径）。

    列表页走 ``_prefetch_list_data`` 的批量预取（不 N+1）；单条（详情页等）
    回退为单查。有头像的才给 ``PrimaryImageTag``——客户端只对有这个标记的
    条目发图片请求（见 ``_item_dto`` 的 ImageTags 回退链注释，同理）。

    注意 ``people_map is not None`` 的判法：预取跑过但该条目没有演员时，
    ``map.get(item.id)`` 是 None——这时**不能**回退单查，否则列表页里每个
    没演员的条目都会多一次查询，预取就白做了。
    """
    people_map = prefetch.get("people")

    def _rows_of(item_id):
        if people_map is not None:
            return people_map.get(item_id, [])
        return (
            db.query(em.EmbyPerson)
            .filter(em.EmbyPerson.item_id == item_id)
            .order_by(em.EmbyPerson.sort_order, em.EmbyPerson.id)
            .all()
        )

    rows = _rows_of(item.id)
    if not rows and item.item_type in ("episode", "season") and item.series_id:
        # 集/季自己没有演员 → 用剧集的演员表（Emby 官方单集详情同样展示剧集主演）
        rows = _rows_of(item.series_id)
    out: list[dict] = []
    for p in rows:
        # Id：三方客户端（以及 Emby 官方 Web）按 /Items/{Person.Id}/Images/Primary
        # 拉头像——以前不给 Id，客户端拼不出地址，只能显示首字占位。用行 id（数字串，
        # 与条目的 32 位 guid 不会撞），media_routes.item_image 能把它解析回演员。
        entry = {"Name": p.name or "", "Id": str(p.id), "Role": p.role or "",
                 "Type": "Actor"}
        if p.image:
            # 图片也可走 /emby/Persons/{name}/Images/Primary（media_routes），
            # tag 用行 id：稳定且唯一，换头像不影响（图片内容寻址）。
            entry["PrimaryImageTag"] = str(p.id)
        out.append(entry)
    return out


def _person_item_dto(person: em.EmbyPerson, db: Session) -> dict:
    """演员详情（/Items/{Person.Id}）：最小可用的 Person BaseItemDto。

    同名演员只要有一行带头像就给 Primary 标记（取图按名字找，见 media_routes）。
    """
    pid = str(person.id)
    has_img = bool(person.image) or db.query(em.EmbyPerson.id).filter(
        em.EmbyPerson.name == person.name, em.EmbyPerson.image.isnot(None),
        em.EmbyPerson.image != "").first() is not None
    return {
        "Name": person.name or "", "Id": pid, "ServerId": SERVER_ID,
        "Type": "Person", "IsFolder": False, "MediaType": "Unknown",
        "ImageTags": {"Primary": pid} if has_img else {},
        "BackdropImageTags": [], "ImageBlurHashes": {},
        "Genres": [], "Tags": [], "People": [], "Studios": [],
        "ProviderIds": {"Tmdb": person.person_tmdb_id}
        if (person.person_tmdb_id or "").isdigit() else {},
        "UserData": _user_data_dto(None, pid),
    }


def _user_data_dto(umd, item_id: str = "") -> dict:
    # 对标 FakEmby：UserData 永不为 null，必填字段全给默认值
    base = {"PlaybackPositionTicks": 0, "PlayCount": 0, "Played": False,
            "IsFavorite": False, "PlayedPercentage": 0.0}
    if not umd:
        d = dict(base)
    else:
        d = {
            "PlaybackPositionTicks": umd.playback_position_ticks or 0,
            "PlayCount": umd.play_count or 0,
            "Played": bool(umd.played),
            "IsFavorite": bool(umd.is_favorite),
            "PlayedPercentage": 0.0,
        }
        if umd.last_played_at:
            d["LastPlayedDate"] = _iso(umd.last_played_at)
    # 官方 GetUserItemDataDto（Jellyfin DtoService）无条件下发 ItemId 与 Key：
    # 三方客户端用 ItemId 把播放进度/收藏写回条目、用 Key 做本地缓存键，
    # 缺字段时严格解码器直接失败（与 Views 缺 ServerId 同一类问题）。
    if item_id:
        d["ItemId"] = item_id
        d["Key"] = item_id
    return d


def _bearer_raw(request: Request) -> str:
    """提取原始 Bearer 值（JWT 或客户端 token）

    注意（H2）：**不要**把返回值直接拼进 URL——它可能是门户 / 管理员 JWT。
    需要放进 URL 的凭据一律走 ``_api_key_for``（只回显 Emby 客户端 token）或
    ``_url_auth_qs``（门户 JWT 换成短期播放签名）。
    """
    raw = request.headers.get("Authorization", "")
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    return request.query_params.get("api_key", "")


def _strip_nulls(obj):
    """递归移除 dict 中的 None 值（iOS 客户端对 null 敏感）。

    - dict：去掉值为 None 的键，递归处理剩余值
    - list：递归处理每个元素（保留 None 元素，避免打乱数组索引语义；
      如需去掉数组中的 None，调用方自行处理）
    - 其他：原样返回

    用于 PlaybackInfo / MediaSource / MediaStream 等播放相关 DTO，
    对标 _item_dto 的 null 处理（数组给 []、字符串给 ""，可选字段有值才加）。
    """
    if isinstance(obj, dict):
        return {k: _strip_nulls(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_nulls(v) for v in obj]
    return obj


def _url_quote(text: str) -> str:
    return urllib.parse.quote(text or "", safe="")


def _external_urls(item: em.MediaItem) -> list:
    """按条目已有的 TMDB/IMDb/豆瓣 等 id 生成外部链接（客户端详情页「链接」区）

    以前这里写死 ``[]``，客户端拿不到任何链接来源，只能自己按 ProviderIds 猜一个
    （于是详情页常常只剩一个来源）。Emby 协议里这组字段就是给客户端展示用的。
    """
    urls = []
    name = (item.name or "").strip()
    is_series = item.item_type in ("series", "season", "episode")
    if item.tmdb_id:
        path = "tv" if is_series else "movie"
        urls.append({
            "Name": "TMDB",
            "Url": f"https://www.themoviedb.org/{path}/{item.tmdb_id}",
        })
    if item.imdb_id:
        urls.append({
            "Name": "IMDb",
            "Url": f"https://www.imdb.com/title/{item.imdb_id}/",
        })
    douban = getattr(item, "douban_id", None)
    if douban:
        kind = "tv" if is_series else "movie"
        urls.append({
            "Name": "豆瓣",
            "Url": f"https://movie.douban.com/{kind}/{douban}/",
        })
    bangumi = getattr(item, "bangumi_id", None)
    if bangumi:
        urls.append({
            "Name": "Bangumi",
            "Url": f"https://bgm.tv/subject/{bangumi}",
        })
    # 豆瓣/Bangumi 没有内建 id 时，按片名给出搜索入口——比什么都没有好
    if not douban and name and (is_series or item.metadata_source == "douban"):
        urls.append({
            "Name": "豆瓣",
            "Url": "https://search.douban.com/movie/subject_search?search_text="
                    + _url_quote(name),
        })
    return urls


def _stream_dto(s, base: str, item: em.MediaItem, api_key: str, db: Session = None,
                auth_qs: str = "") -> dict:
    # ffprobe 的 video stream 经常不单独给 bitrate（尤其是远程 HEVC 文件），
    # 但条目级 probe 已经有可靠的总 bitrate / 分辨率。不能把 0 映射成客户端的
    # 「1kbps」假数据，也不能把 3840×1920 丢掉。
    is_video = (s.stream_type or "").lower() == "video"
    dto = {
        "Index": s.stream_index, "Type": s.stream_type, "Codec": s.codec,
        "Language": s.language, "DisplayTitle": s.display_title or s.language,
        "Title": s.title, "IsDefault": bool(s.is_default),
        "IsForced": bool(s.is_forced), "IsExternal": bool(s.is_external),
        "Channels": s.channels,
        "BitRate": (s.bit_rate or item.bitrate or None) if is_video else s.bit_rate,
    }
    # 客户端「媒体信息」页逐行显示的字段（Emby 协议标准命名），有值才发
    for attr, key in (
        ("frame_rate", "FrameRate"),
        ("video_range", "VideoRange"),
        ("profile", "Profile"),
        ("level", "Level"),
        ("pixel_format", "PixelFormat"),
        ("aspect_ratio", "AspectRatio"),
        ("bit_depth", "BitDepth"),
        ("sample_rate", "SampleRate"),
        ("channel_layout", "ChannelLayout"),
        ("sample_format", "SampleFormat"),
    ):
        val = getattr(s, attr, None)
        if val not in (None, ""):
            dto[key] = val
    if is_video and item.width and item.height:
        # 没有 stream 里的画面比例时，用分辨率算一个（客户端要显示这一行）
        if not getattr(s, "aspect_ratio", None):
            from math import gcd
            g = gcd(int(item.width), int(item.height)) or 1
            dto.setdefault("AspectRatio", f"{int(item.width) // g}:{int(item.height) // g}")
    if is_video:
        if item.width:
            dto["Width"] = item.width
        if item.height:
            dto["Height"] = item.height
    if (s.stream_type or "").lower() == "subtitle":
        text_track = subs.is_text_track(s.codec)
        dto["IsTextSubtitleStream"] = text_track
        dto["SupportsExternalStream"] = text_track
        dto["DeliveryMethod"] = "External"
        if text_track:
            # 客户端靠 DeliveryUrl 发现字幕地址；缺失会表现为“服务器无字幕”
            # 鉴权查询串：显式给了 auth_qs（门户 → 播放签名）就用它，否则沿用 Emby 的 api_key
            delivery = (
                f"{base}/emby/Videos/{item.guid}/{item.guid}"
                f"/Subtitles/{s.stream_index}/Stream.vtt?{auth_qs or f'api_key={api_key}'}"
            )
            # CDN 预留（第 2/3 层）：启用时字幕也走 CDN 域名（回源到本服务）
            if db is not None:
                delivery = cdn.rewrite_url(db, delivery, base)
            dto["DeliveryUrl"] = delivery
    # iOS 客户端（Lenna/SenPlayer）对 null 敏感，递归去掉 None 字段
    return _strip_nulls(dto)


def _default_subtitle_index(item: em.MediaItem):
    for s in item.streams:
        if (s.stream_type or "").lower() == "subtitle" and subs.is_text_track(s.codec):
            return s.stream_index
    return None


# 文件扩展名 -> 容器格式的安全映射（只做 container 回退，不猜测编码）。
# 原理：扩展名可靠地反映容器格式；缺失 container 会导致客户端无法判断直放兼容性
# 而误走转码。这里只补 container，绝不虚报 video/audio codec（宁可保守）。
_CONTAINER_BY_EXT = {
    ".mp4": "mp4", ".m4v": "mp4",
    ".mkv": "mkv",
    ".avi": "avi",
    ".mov": "mov",
    ".ts": "ts", ".m2ts": "m2ts", ".mts": "m2ts",
    ".webm": "webm",
    ".flv": "flv",
    ".wmv": "wmv",
    ".mpg": "mpeg", ".mpeg": "mpeg",
}


def _container_of(item: em.MediaItem) -> Optional[str]:
    """取容器格式：优先扫描数据，缺失时从文件扩展名安全回退。"""
    if item.container:
        return item.container
    path = (item.file_path or "").lower()
    # 去掉 URL 参数（如 ?xxx）
    path = path.split("?")[0]
    for ext, container in _CONTAINER_BY_EXT.items():
        if path.endswith(ext):
            return container
    return None


def hide_media_path() -> bool:
    """EMBY_HIDE_MEDIA_PATH（默认开）：客户端协议响应里不下发服务器上的媒体路径。

    MediaSources[].Path 会被三方客户端直接显示在「媒体信息」里（如
    ``/strm/剧集/…/xx.strm``），泄露服务器目录结构。播放全走 DirectStreamUrl /
    /Videos/{item.guid}/stream（按条目 id，不读 Path），省略它不影响播放。
    管理端 / 扫描等内部逻辑直接读 ``MediaItem.file_path``，不受影响。
    """
    return (os.getenv("EMBY_HIDE_MEDIA_PATH", "1") or "1").strip().lower() not in (
        "0", "false", "no", "off")


_CODEC_NORMALIZE = {
    "h.264": "h264", "h264": "h264", "x264": "h264", "avc": "h264",
    "h.265": "hevc", "h265": "hevc", "x265": "hevc", "hevc": "hevc",
    "e-ac3": "eac3", "eac3": "eac3", "ac3": "ac3", "aac": "aac", "flac": "flac",
    "mp3": "mp3", "dts": "dts", "dts-hd": "dts", "truehd": "truehd",
}


def _norm_codec(codec) -> str:
    c = str(codec or "").strip().lower()
    return _CODEC_NORMALIZE.get(c, c)


def _synthetic_streams(item: em.MediaItem, existing: list) -> list[dict]:
    """还没探测到内封轨道时，用文件名/条目级信息拼最小的视频/音频轨（只给详情页）。

    strm / 远程挂载扫描阶段不探测，按需探测又要等用户点开后才跑：这段时间里客户端
    「媒体信息」只剩空壳。这里用扫描时文件名解析出的分辨率/编码/来源（或探测已写到
    条目上的宽高编码）拼出视频/音频各一条；**不进 PlaybackInfo**——起播决策仍只看
    真实探测结果，不拿猜测值影响直放/转码判断。拿不到编码的不拼（不虚报）。
    """
    from backend.emby_server import filename_meta

    meta = filename_meta.parse_filename(item.file_path or "") if item.file_path else {}
    vcodec = _norm_codec(item.video_codec or meta.get("video_codec"))
    acodec = _norm_codec(item.audio_codec or meta.get("audio_codec"))
    resolution = item.video_resolution or meta.get("resolution")
    width, height = item.width or 0, item.height or 0
    if not (width and height):
        width, height = filename_meta.resolution_to_wh(resolution)
    source = item.media_source or meta.get("source") or ""
    used = {s.get("Index") for s in existing}

    def _free(start: int) -> int:
        i = start
        while i in used:
            i += 1
        used.add(i)
        return i

    out: list[dict] = []
    if vcodec or height:
        label = " ".join(x for x in (
            resolution or (f"{height}p" if height else ""),
            vcodec.upper() if vcodec else "", source) if x)
        v = {"Index": _free(0), "Type": "Video", "Codec": vcodec or None,
             "DisplayTitle": label or None, "IsDefault": True, "IsForced": False,
             "IsExternal": False, "IsInterlaced": False,
             "Width": width or None, "Height": height or None,
             "BitRate": item.bitrate or None}
        if width and height:
            from math import gcd
            g = gcd(int(width), int(height)) or 1
            v["AspectRatio"] = f"{int(width) // g}:{int(height) // g}"
        out.append(_strip_nulls(v))
    if acodec:
        out.append(_strip_nulls({
            "Index": _free(1), "Type": "Audio", "Codec": acodec,
            "DisplayTitle": acodec.upper(), "IsDefault": True, "IsForced": False,
            "IsExternal": False}))
    return out


def _media_source(item: em.MediaItem, base: str, api_key: str = "", db: Session = None,
                  auth_qs: str = "", synthesize: bool = False) -> dict:
    dto = {
        "Id": item.guid,
        "Name": item.name,
        # 隐藏时置 None，由下面的 _strip_nulls 整个去掉（官方 Path 可空，客户端兼容）
        "Path": None if hide_media_path() else item.file_path,
        "Protocol": "File",
        "Type": "Default",
        "Container": _container_of(item),
        "Size": item.size,
        # 兼容修复：RunTimeTicks 必须始终存在（0 = 未知），缺字段时三方客户端
        # 会显示默认的 1 分钟。之前 or None 会被 _strip_nulls 删掉字段。
        "RunTimeTicks": _run_time_ticks(item),
        "Bitrate": item.bitrate or None,
        "SupportsDirectPlay": True,
        "SupportsDirectStream": True,
        "SupportsTranscoding": True,
        "IsRemote": False,
        # 官方 MediaSourceInfo 的非空值类型/集合字段（对照
        # MediaBrowser.Model/Dto/MediaSourceInfo.cs，均在构造器里初始化，序列化恒发）。
        # 缺这些 bool/数组键时，严格解码 MediaSourceInfo 的三方客户端在
        # PlaybackInfo 这一步就解析失败——那会直接表现为“不能播”。
        "ReadAtNativeFramerate": False,
        "IgnoreDts": False,
        "IgnoreIndex": False,
        "GenPtsInput": False,
        "IsInfiniteStream": False,
        "UseMostCompatibleTranscodingProfile": False,
        "RequiresOpening": False,
        "RequiresClosing": False,
        "SupportsProbing": True,
        "HasSegments": False,
        "Formats": [],
        "MediaAttachments": [],
        "RequiredHttpHeaders": {},
        "DefaultSubtitleStreamIndex": _default_subtitle_index(item),
        "MediaStreams": [_stream_dto(s, base, item, api_key, db, auth_qs) for s in item.streams],
    }
    if synthesize and not any(
            (st.get("Type") or "").lower() in ("video", "audio")
            for st in dto["MediaStreams"]):
        synth = _synthetic_streams(item, dto["MediaStreams"])
        if synth:
            dto["MediaStreams"] = synth + dto["MediaStreams"]
    # 文件名解析的视频信息（v2.49.0）：MediaStreams 为空时客户端「媒体信息」页
    # 也有编码可显示；有流信息时以流为准（这里只是回退）。
    if item.video_codec:
        dto["VideoCodec"] = item.video_codec
    if item.audio_codec:
        dto["AudioCodec"] = item.audio_codec
    # iOS 客户端（Lenna/SenPlayer）对 null 敏感，递归去掉 None 字段
    # 注：DirectStreamUrl / TranscodingUrl 由调用方（playback_info）在拿到 _play_target 后
    # 拼装，CDN 域名改写也在那一层完成（这里只管字幕 DeliveryUrl）。
    return _strip_nulls(dto)


def _require_item(db: Session, item_id: str) -> em.MediaItem:
    """按 guid 取条目（**不做**可见范围判断）

    协议端点（按用户取条目的地方）一律用 ``_require_visible_item``；直接调用本函数的
    协议端点会被 ``scripts/check_item_scope.py`` 护栏拦下（安全修复 H1）。
    """
    item = db.query(em.MediaItem).filter(em.MediaItem.guid == item_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Item not found")
    return item


def _item_visible(db: Session, user, item) -> bool:
    """条目所在媒体库对该用户可见吗（口径与列表 / 浏览的 ``_library_scope`` 完全一致）"""
    allowed = _library_scope(db, user)
    return allowed is None or getattr(item, "library_id", None) in allowed


def _require_visible_item(db: Session, user, item_id: str) -> em.MediaItem:
    """按 guid 取条目 + 媒体库可见范围校验（安全修复 H1）

    所有「按条目 id 取东西」的协议端点（详情、PlaybackInfo、直放、HLS、拉文件、下载、字幕、
    季 / 集、相似、祖先、收藏 / 已看 / 评分、播放进度上报）统一走这里：
    guid 可由路径推算（``md5("rb:item:" + path)``），不能当作访问凭据。
    """
    item = _require_item(db, item_id)
    if not _item_visible(db, user, item):
        raise HTTPException(status_code=403, detail="Library not accessible")
    return item


def _run_time_ticks(item: em.MediaItem) -> int:
    """RunTimeTicks：探测出的真实时长优先；没有时用元数据片长（TMDB/豆瓣），
    集/季再回退到剧集的单集片长；都没有才是 0（字段必须在，见 _media_source 注释）。"""
    if item.duration_ticks:
        return int(item.duration_ticks)
    meta = getattr(item, "metadata_runtime_ticks", 0) or 0
    if meta:
        return int(meta)
    if item.item_type == "episode" and item.series_id:
        series = item.series
        if series is not None and (getattr(series, "metadata_runtime_ticks", 0) or 0):
            return int(series.metadata_runtime_ticks)
    return 0


def _now_playing_dto(session: em.PlaybackSession, item: em.MediaItem, user) -> dict:
    return {
        "Id": session.session_key,
        "UserId": str(user.id),
        "UserName": user.username,
        "Client": session.client_name or "Emby Web",
        "DeviceName": session.device_name or "Unknown Device",
        "ApplicationVersion": session.client_version or "1.0",
        "NowPlayingItem": {
            "Id": item.guid, "Name": item.name, "Type": _emby_type(item.item_type),
            "RunTimeTicks": _run_time_ticks(item), "ProductionYear": item.production_year,
            "MediaType": "Video",
        },
        "PlayState": {
            "PositionTicks": session.position_ticks or 0,
            "IsPaused": bool(session.is_paused),
            "PlayMethod": session.play_method or "DirectStream",
            "CanSeek": True,
        },
        "PlayMethod": session.play_method or "DirectStream",
        "TranscodingInfo": {"CompletionPercentage": 100} if session.play_method == "Transcode" else None,
    }


# ==================== 系统信息 ====================

# 客户端发现服务时第一条请求就是它：Emby 官方文档写的是 /emby/System/Info/Public，
# 而别的客户端可能会用裸路径或全小写，所以三种写法都注册（缺一个就会 404 连不上）。
@emby_router.get("/emby/System/Info")
@emby_router.get("/emby/System/Info/Public")
@emby_router.get("/emby/system/info")
@emby_router.get("/emby/system/info/public")
@emby_router.get("/System/Info")
@emby_router.get("/System/Info/Public")
def system_info(request: Request):
    return {
        "Id": SERVER_ID,
        "ServerName": os.getenv("EMBY_SERVER_NAME", "Aetrix Media Server"),
        "Version": SERVER_VERSION,
        "ProductName": "Emby Server",
        "OperatingSystem": "Linux",
        "TranscodingJobs": 0,
        "LocalAddress": _public_base_url(request),
        "WanAddress": _public_base_url(request),
        "SupportsLibraryMonitor": False,
        # 未实现 /Sync/* 离线同步接口，如实上报 False，避免客户端尝试无法完成的离线同步
        "SupportsSynchronization": False,
        "StartupWizardCompleted": True,
        "HasUpdateAvailable": False,
        "CastReceiverApplications": [],
        "CanSelfRestart": False,
        "CanLaunchWebApp": True,
        "HavePendingRestart": False,
        "CompletedInstallations": [],
    }


@emby_router.get("/emby/System/Ping")
@emby_router.get("/emby/system/ping")
@emby_router.get("/System/Ping")
def system_ping():
    return PlainTextResponse("Emby Server")


# 三方客户端（SenPlayer 等）启动时会请求，用于服务器发现；返回空列表即可
@emby_router.get("/emby/System/Ext/ServerDomains")
@emby_router.get("/System/Ext/ServerDomains")
def system_ext_server_domains():
    return []


@emby_router.post("/emby/System/Ping")
@emby_router.post("/emby/system/ping")
@emby_router.post("/System/Ping")
def system_ping_post():
    return PlainTextResponse("Emby Server")


@emby_router.get("/emby/Branding/Configuration")
@emby_router.get("/emby/branding/config")
@emby_router.get("/Branding/Configuration")
def branding_config():
    return {"LoginDisclaimer": "", "CustomCss": "", "SplashscreenEnabled": False}


@emby_router.get("/emby")
def emby_root():
    return {"ProductName": "Emby Server", "Version": SERVER_VERSION, "Id": SERVER_ID}


# ==================== 认证 ====================

@emby_router.post("/emby/Users/AuthenticateByName")
@emby_router.post("/Users/AuthenticateByName")
def authenticate_by_name(
    request: Request,
    credentials: dict = Body(...),
    db: Session = Depends(get_db),
):
    username = (credentials.get("Username") or "").strip()
    password = credentials.get("Pw") or credentials.get("password") or ""

    # 限流：每分钟最多 10 次认证尝试（防暴力破解）。桶按「出口 IP + 账号」建——
    # 纯按 IP 会在 NAT 下连坐：家庭/宿舍/公司共用一个公网出口，一个人把密码输错
    # 几次（或客户端拿着过期凭据反复重试），同一 IP 下的**所有人**立刻开始收 429，
    # 客户端再按 Retry-After 退避，用户看到的就是「登录卡很久」。爆破针对的是
    # 某个账号，所以按账号建档；出口 IP 这一层由网关中间件的 15 次/分钟规则兜底
    # （换用户名扫也会被它挡住）。
    from backend.ratelimit import check_rate_limit, client_ip

    ip = client_ip(request)
    account = username.lower() or "-"  # 空用户名（探活 / 畸形请求）落进公共桶
    allowed, retry_after = check_rate_limit(f"emby-auth:{ip}:{account}", 10, 60)
    if not allowed:
        return Response(
            content='{"error": "TooManyAttempts"}',
            status_code=429, media_type="application/json",
            headers={"Retry-After": str(retry_after)},
        )

    user = None
    if username:
        # 优先自建 Emby 凭据，其次门户账号
        user = db.query(models.WebUser).filter(
            or_(models.WebUser.emby_username == username, models.WebUser.username == username)
        ).first()

    from backend.emby_server.auth import issue_token, verify_and_migrate_emby_password

    def _auth_fail(detail: str = "用户名或密码错误"):
        from backend.authlog import record_event

        record_event(
            db, username=username, user_id=user.id if user else None, ip=ip,
            agent=request.headers.get("user-agent"), success=False,
            reason="emby_login_failed", detail=detail,
        )
        # 对齐官方 Emby：认证失败返回 401 空 body。
        # 曾返回 {"error": "InvalidUsernameOrPassword"} JSON，官方 iOS 客户端会尝试
        # 按认证成功结构解析该 body，报"数据解析错误"；真 Emby 是空 body，客户端则
        # 正常提示用户名或密码不正确（2026-09-26 线上实测）。
        return Response(status_code=401)

    if not user or not user.is_active or not password:
        return _auth_fail()

    # 密码校验：支持哈希与明文；明文/旧格式在成功后透明升级为 bcrypt-sha256。
    # A-P1-3："校验+升级"收敛到 verify_and_migrate_emby_password 一处，
    # 不再手写 startswith 分支（ensure 内已含 needs_password_rehash 判断）。
    if not verify_and_migrate_emby_password(db, user, password):
        return _auth_fail()

    # 设备数上限：超限时拒绝签发（管理员不受限），并落安全日志
    from backend.authlog import record_event
    from backend.devices import DeviceLimitExceeded

    try:
        token_value, token_row = issue_token(db, user, request)
    except DeviceLimitExceeded as exc:
        record_event(
            db, username=user.username, user_id=user.id, ip=ip,
            agent=request.headers.get("user-agent"), success=False,
            reason="device_limit", detail=exc.message,
        )
        return Response(
            content=json.dumps({"error": "DeviceLimitExceeded", "message": exc.message},
                               ensure_ascii=False),
            status_code=403, media_type="application/json; charset=utf-8",
        )

    auth = parse_emby_authorization(request.headers.get("X-Emby-Authorization"))
    record_event(
        db, username=user.username, user_id=user.id, ip=ip,
        agent=request.headers.get("user-agent"), success=True, reason="emby_login",
        detail=f"{auth.get('Client') or 'Emby Client'} / {token_row.device_id}",
    )
    # 防共享·跨城市轨迹（默认 off，不判定也不写库）。命中 enforce 时账号已被停用、
    # 刚签发的令牌也已吊销，所以直接拒绝本次登录而不是「登进去发现不能用」
    from backend import share_guard

    verdict = share_guard.note_activity(db, user, ip)
    if verdict and verdict.get("blocked"):
        return Response(
            content=json.dumps(
                {"error": "AccountSuspended",
                 "message": "检测到该账号在短时间内于多个城市登录，已被暂停使用，请联系管理员"},
                ensure_ascii=False,
            ),
            status_code=403, media_type="application/json; charset=utf-8",
        )
    access_token = token_value
    server_id = SERVER_ID
    user_dto = _user_dto(user, db)
    # SessionInfo 字段对照官方 SessionInfo（MediaBrowser.Controller/Session/SessionInfo.cs）：
    # PlayState / AdditionalUsers / NowPlayingQueue 在官方构造器里就初始化（恒发），
    # IsActive / HasCustomDeviceName / SupportsMediaControl 是非空 bool（恒发），
    # SupportedCommands / PlayableMediaTypes 在未上报能力时为空数组（恒发）。
    # 缺这些字段时，严格解码 SessionInfo 的三方客户端在登录这一步就解析失败。
    return {
        "User": user_dto,
        "AccessToken": access_token,
        "ServerId": server_id,
        "SessionInfo": {
            "UserId": str(user.id),
            "UserName": user.username,
            "Client": auth.get("Client") or "Emby",
            "DeviceName": auth.get("Device") or "Unknown Device",
            "DeviceId": token_row.device_id,
            "Id": token_row.token[:16],
            "ServerId": server_id,
            "ApplicationVersion": auth.get("Version") or "1.0",
            "IsAuthenticated": True,
            "IsActive": True,
            "HasCustomDeviceName": False,
            "LastActivityDate": _iso(datetime.now()),
            "SupportsRemoteControl": True,
            "SupportsMediaControl": True,
            "PlayState": {
                "CanSeek": False,
                "IsPaused": False,
                "IsMuted": False,
                "RepeatMode": "RepeatNone",
            },
            "AdditionalUsers": [],
            "NowPlayingQueue": [],
            "SupportedCommands": [],
            "PlayableMediaTypes": [],
        },
    }


# ==================== 客户端兼容公共工具 ====================


def _guid_of(kind: str, name: str) -> str:
    """按名称生成稳定的 Emby 风格 32 位 Id（用于 Genre/Studio 等虚拟条目）"""
    return hashlib.md5(f"{kind}:{name}".encode("utf-8")).hexdigest()


def _virtual_libraries_enabled() -> bool:
    """虚拟媒体库总开关（按 EA 实例生效）

    关闭时虚拟媒体库既不出现在客户端视图里，直接用 guid 访问也会 404，
    与“未开启时不会出现在客户端或直达接口”一致。
    """
    return os.getenv("ENABLE_VIRTUAL_LIBRARIES", "true").strip().lower() not in ("0", "false", "no")


def _facet_or_legacy(column, kind: str, names: list[str], db: Session):
    """分类筛选条件：优先走关联表的索引，老库没回填完就退回全表 ILIKE

    关联表命中后不再碰 `emby_items` 的文本列；两者的匹配语义一致
    （关联表侧是「全等或取值里包含」，与 ``col ILIKE '%x%'`` 相同，含大小写不敏感），
    所以回填过程中混用两种路径也不会出现两种结果。
    传了库里不存在的分类名时返回“空结果”条件（与 ``ILIKE`` 匹配不到任何行一致）。
    """
    if not names:
        return None
    if facets.ensure_ready(db):
        return facets.candidate_condition(kind, names, db)
    return or_(*[column.ilike(f"%{name}%") for name in names])


def _synthetic_map(db: Session) -> dict[str, tuple[str, str]]:
    """{合成 Id: (类型, 名称)} 反查表（类型 / 工作室 / 年份 右键筛选用）

    客户端点开「类型 / 制作公司 / 年份」后会带着我们生成的 Id 再请求 /Items。
    旧实现找不到条目就退回按 guid 匹配，等于返回空（或整库）——点进去白点。
    """
    buckets: dict[str, set[str]] = {"Genre": set(), "Studio": set(), "Year": set()}
    if facets.ensure_ready(db):
        # 走关联表：两次索引扫描（取值很少），不再扫全库文本列
        buckets["Genre"].update(facets.kind_values(db, facets.KIND_GENRE))
        buckets["Studio"].update(facets.kind_values(db, facets.KIND_STUDIO))
    else:
        # 老库还没回填完：沿用旧口径，宁可慢也不能少（否则客户端点类型会空）
        for genres, studios in db.query(
            em.MediaItem.genres, em.MediaItem.studios
        ).all():
            buckets["Genre"].update(g for g in (genres or "").split(",") if g)
            buckets["Studio"].update(s for s in (studios or "").split(",") if s)
    for (year,) in (
        db.query(em.MediaItem.production_year)
        .filter(em.MediaItem.production_year.isnot(None))
        .distinct()
        .all()
    ):
        buckets["Year"].add(str(year))
    return {
        _guid_of(kind, name): (kind, name)
        for kind, names in buckets.items()
        for name in names
    }


def _synthetic_lookup(db: Session) -> dict[str, tuple[str, str]]:
    """两级缓存的反查表：同一请求内复用，跨请求按「关联表代际」复用

    合成 Id 表只随扫描 / 回填变化（代际号会变），因此跨请求缓存不会给出过期结果；
    以前每次带 GenreIds/StudioIds 的列表请求都要扫一遍全库。
    """
    cached = db.info.get("_aetrix_synthetic_ids")
    if cached is None:
        generation = facets.values_generation()
        if _SYNTHETIC_CACHE.get("map") is not None and _SYNTHETIC_CACHE.get("gen") == generation:
            cached = _SYNTHETIC_CACHE["map"]
        else:
            cached = _synthetic_map(db)
            _SYNTHETIC_CACHE["gen"] = generation
            _SYNTHETIC_CACHE["map"] = cached
        db.info["_aetrix_synthetic_ids"] = cached
    return cached


def _empty_items() -> dict:
    return {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}


def _query_result(items: list, user: models.WebUser, db: Session, base: str) -> dict:
    _prefetch_list_data(db, user.id, list(items))
    return {
        "Items": [_item_dto(i, base, user.id, db) for i in items],
        "TotalRecordCount": len(items),
        "StartIndex": 0,
    }


def _api_key_for(db: Session, request: Request) -> str:
    """拼接播放/字幕地址用的 api_key：回显请求中的 **Emby 客户端 token**（URL 编码后）。

    注意：不能用库里的 token 字段——P1 #200 后库中只存 SHA256 哈希，
    把哈希拼进 URL 会导致服务端二次哈希校验失败（401）。

    安全修复 H2：门户 / 管理员 **JWT 绝不回显进 URL**（会落进反代 / CDN 日志、浏览器历史、
    Referer）。请求凭据是 JWT 时返回空串，调用方改用 ``_url_auth_qs`` 的短期播放签名。
    第三方 Emby 客户端（Infuse 等）本来就把自己的 token 放在 ``api_key`` 里，保持兼容。
    """
    import urllib.parse as _up

    from backend.security import resolve_jwt_user_id

    raw = (_token_from_request(request) or _bearer_raw(request) or "").strip()
    if not raw or resolve_jwt_user_id(raw) is not None or raw.count(".") == 2:
        return ""
    return _up.quote(raw, safe="")


# 门户（JWT）播放地址的签名有效期：网页端暂停 / 切片请求可能拖很久，默认 6 小时。
# 签名只绑定「这个用户 + 这一部片」，泄露的影响远小于整把 JWT（含管理员 JWT）。
PLAY_SIGN_URL_TTL = int(os.getenv("PLAY_SIGN_URL_TTL", "21600") or 21600)


def _url_auth_qs(db: Session, request: Request, user, item_guid: str) -> str:
    """放进播放 / 字幕 / HLS 地址的鉴权查询串（不含前导 ``?`` / ``&``）

    - 请求带的是 Emby 客户端 token：``api_key=<token>``（Emby 协议兼容，第三方客户端依赖它）；
    - 否则（门户 JWT / 管理员 JWT）：``uid&exp&sign`` 短期播放签名（``play_sign``），
      JWT 不进 URL（H2）。
    """
    key = _api_key_for(db, request)
    if key:
        return f"api_key={key}"
    exp, sig = play_sign.issue_play_sign(user.id, item_guid, PLAY_SIGN_URL_TTL)
    return f"uid={user.id}&exp={exp}&sign={sig}"


def _echo_auth_qs(request: Request) -> str:
    """HLS 子请求（变体 / 切片）沿用本次请求的凭据：签名原样带下去，Emby token 回显，JWT 丢弃"""
    import urllib.parse as _up

    # Emby 客户端 token 优先：它与升级前完全一致（切片可能在 15 分钟签名过期后才请求）
    key = _api_key_for(None, request)
    if key:
        return f"api_key={key}"
    q = request.query_params
    if q.get("sign") and q.get("exp") and q.get("uid"):
        return "uid={}&exp={}&sign={}".format(
            _up.quote(q.get("uid"), safe=""), _up.quote(q.get("exp"), safe=""),
            _up.quote(q.get("sign"), safe=""))
    return ""


def _policy_dto(user: models.WebUser) -> dict:
    """UserPolicy：对照官方 ``MediaBrowser.Model.Users.UserPolicy`` 逐字段下发

    官方 UserPolicy 的 bool / int / 数组都是非空值类型，序列化时**每个键都发**；
    旧实现只发 13 个键，严格解码整套 UserPolicy 的三方客户端会因缺键失败。
    这里把官方字段补齐（值取官方构造器默认值），与本服务策略相关的几项按实际下发：
    管理员才 EnableContentDeletion，下载/播放始终放行（下载另有全局开关在
    CanDownload 上拦），并发口径用本服务自有的 SimultaneousStreamLimit。
    """
    admin = bool(user.is_staff)
    return {
        "IsAdministrator": admin,
        "IsHidden": False,
        "IsDisabled": not bool(user.is_active),
        "EnableCollectionManagement": False,
        "EnableSubtitleManagement": False,
        "EnableLyricManagement": False,
        "EnableContentDeletion": admin,
        "EnableContentDeletionFromFolders": [],
        "EnableContentDownloading": True,
        "EnableMediaPlayback": True,
        "EnableAudioPlaybackTranscoding": True,
        "EnableVideoPlaybackTranscoding": bool(getattr(user, "enable_video_transcoding", True)),
        "EnablePlaybackRemuxing": True,
        "ForceRemoteSourceTranscoding": False,
        "EnableSyncTranscoding": True,
        "EnableMediaConversion": True,
        "EnableAllDevices": True,
        "EnabledDevices": [],
        "EnableAllFolders": True,
        "EnabledFolders": [],
        "EnableAllChannels": True,
        "EnabledChannels": [],
        "EnableUserPreferenceAccess": True,
        "EnableRemoteControlOfOtherUsers": False,
        "EnableSharedDeviceControl": True,
        "EnableRemoteAccess": True,
        "EnableLiveTvManagement": True,
        "EnableLiveTvAccess": True,
        "EnablePublicSharing": False,
        "BlockUnratedItems": [],
        "BlockedTags": [],
        "AllowedTags": [],
        "BlockedMediaFolders": [],
        "BlockedChannels": [],
        "AccessSchedules": [],
        "InvalidLoginAttemptCount": 0,
        "LoginAttemptsBeforeLockout": -1,
        "MaxActiveSessions": 0,
        "RemoteClientBitrateLimit": 0,
        "AuthenticationProviderId": "Emby",
        "PasswordResetProviderId": "Emby",
        "SyncPlayAccess": "CreateAndJoinGroups",
        # 本服务自有的并发口径（官方无此字段，多发不碍事，客户端读它限并发）
        "SimultaneousStreamLimit": 3,
    }


# ---- 用户：/Users/Public、/Users/Me 必须注册在 /Users/{user_id} 之前 ----

@emby_router.get("/emby/Users/Public")
@emby_router.get("/Users/Public")
def users_public():
    """客户端登录页的用户列表

    按 Jellyfin 默认隐私策略返回空数组：本服务账号即门户账号，未认证地枚举用户列表
    会泄露全站账号。返回空列表（而非 404）让客户端走“手动输入用户名”分支。
    """
    return []


@emby_router.get("/emby/Users/Me")
@emby_router.get("/Users/Me")
def users_me(user: models.WebUser = Depends(get_emby_user), db: Session = Depends(get_db)):
    return _user_dto(user, db)


@emby_router.get("/emby/Users")
@emby_router.get("/Users")
def users_list(user: models.WebUser = Depends(get_emby_user), db: Session = Depends(get_db)):
    if not user.is_staff:
        raise HTTPException(status_code=403, detail="Forbidden")
    rows = db.query(models.WebUser).filter(models.WebUser.is_active == True).all()  # noqa: E712
    return [_user_dto(u, db) for u in rows]


def _user_dto(user: models.WebUser, db: Session) -> dict:
    # Policy 与 /Users/{id}/Policy 同一个出口（_policy_dto），两处不再各写一份
    policy = _policy_dto(user)
    # Configuration 对照官方 UserConfiguration：数组字段与 bool 字段官方恒发，
    # 只有字符串可空字段才省略。缺 GroupedFolders / OrderedViews 这类数组键时，
    # 严格解码器会在登录后的第一个 /Users/Me 请求上失败。
    configuration = {
        "SubtitleLanguagePreference": "chi",
        "AudioLanguagePreference": "chi",
        "PlayDefaultAudioTrack": True,
        "DisplayMissingEpisodes": False,
        "GroupedFolders": [],
        "DisplayCollectionsView": False,
        "EnableLocalPassword": False,
        "OrderedViews": [],
        "LatestItemsExcludes": [],
        "MyMediaExcludes": [],
        "HidePlayedInLatest": True,
        "RememberAudioSelections": True,
        "RememberSubtitleSelections": True,
        "EnableNextEpisodeAutoPlay": True,
    }
    return {
        "Id": str(user.id),
        "Name": user.emby_username or user.username,
        "ServerId": SERVER_ID,
        "HasPassword": True,
        "HasConfiguredPassword": True,
        "PrimaryImageTag": None,
        "IsAdministrator": bool(user.is_staff),
        "Policy": policy,
        "Configuration": configuration,
    }


@emby_router.get("/emby/Users/{user_id}")
@emby_router.get("/Users/{user_id}")
def get_user(user_id: str, user: models.WebUser = Depends(get_emby_user),
                   db: Session = Depends(get_db)):
    if user_id not in (str(user.id), "me", user.emby_username or "", user.username):
        raise HTTPException(status_code=403, detail="Forbidden")
    return _user_dto(user, db)


def _library_item_count(db, lib) -> int:
    """媒体库条目数：优先用扫描结束时写入的缓存；为 None（扫描未完成/失败过）
    时实时计数，口径与扫描器一致（只计 movie/series）。"""
    if lib.item_count is not None:
        return lib.item_count
    # count_visible 而非 Query.count()：后者数得到已下架的条目（软删除可见性过滤
    # 下不到它包出来的子查询），客户端会看到比实际能浏览到的更多条目
    return soft_delete.count_visible(
        db,
        em.MediaItem.library_id == lib.id,
        em.MediaItem.item_type.in_(["movie", "series"]),
    )


def _library_cover_abs(cover_path: str | None) -> str | None:
    """库封面相对路径转绝对路径（cover_path 存的是 library-covers/<guid>.webp）"""
    if not cover_path:
        return None
    import os as _os
    if _os.path.isabs(cover_path):
        return cover_path
    from backend.emby_server import image_store as _is
    return _os.path.abspath(_os.path.join(_is.image_dir(), cover_path))


def _library_cover_tag(cover_path: str | None) -> str:
    """库封面的 ImageTag：用文件 mtime 做 tag，换封面后客户端缓存失效"""
    import os as _os
    abs_path = _library_cover_abs(cover_path)
    if not abs_path:
        return "1"
    try:
        return str(int(_os.path.getmtime(abs_path)))
    except OSError:
        return "1"


def _library_scope(db: Session, user) -> "set[int] | None":
    """这个用户能看到哪些媒体库 id；``None`` = 不过滤（绝大多数部署）

    两层规则（服务器默认范围 + 指定用户覆盖）都在 ``backend/library_scope.py``，
    这里只负责调用。走 ``effective_ids_safe``：读失败按「全部不可见」处理（S12
    fail-closed，空集合 → 列表为空、按条目取 403），工作人员不受影响。
    """
    return library_scope.effective_ids_safe(db, user)


def _scope_items(query, allowed: "set[int] | None"):
    """把可见范围挂到条目查询上（``None`` = 不加条件）"""
    return library_scope.scope_query(query, allowed)


# Views 的 DateCreated 固定回退值：created_at 为 NULL 且库内没有任何条目时使用。
# 恒为 naive datetime，_iso() 输出 "2020-01-01T00:00:00Z"。
_LIB_CREATED_AT_FALLBACK = datetime(2020, 1, 1)


@emby_router.get("/emby/Users/{user_id}/Views")
@emby_router.get("/Users/{user_id}/Views")
def user_views(user_id: str, user: models.WebUser = Depends(get_emby_user),
                     db: Session = Depends(get_db)):
    libs = db.query(em.Library).filter(em.Library.is_enabled == True).all()  # noqa: E712
    virtual_on = _virtual_libraries_enabled()
    allowed = _library_scope(db, user)
    # DateCreated 恒发（官方 dto.DateCreated 全字段默认下发；缺失会让严格的
    # 三方客户端整条解析失败，ddPlayer 即为其一）。历史迁移库的 created_at
    # 可能为 NULL：先回退该库内最早入库条目的 date_added（与条目数同一口径），
    # 空库再回退固定常量——保证每个条目恒带 DateCreated。
    need_earliest = [lib.id for lib in libs if not lib.created_at]
    _lib_earliest_added: dict = {}
    if need_earliest:
        rows = (
            db.query(em.MediaItem.library_id, func.min(em.MediaItem.date_added))
            .filter(
                em.MediaItem.library_id.in_(need_earliest),
                em.MediaItem.item_type.in_(["movie", "series"]),
            )
            .group_by(em.MediaItem.library_id)
            .all()
        )
        # 软删除可见性由全局事件钩子（soft_delete._hide_soft_deleted）作用于
        # MediaItem 查询自动生效，无需额外条件。
        _lib_earliest_added = {lid: dt for lid, dt in rows if dt is not None}
    items = []
    for lib in libs:
        # 服务器默认范围 / 个人覆盖：不在名单里的库不出现在客户端的媒体库列表里
        if allowed is not None and lib.id not in allowed:
            continue
        is_virtual = bool(getattr(lib, "is_virtual", False))
        # 虚拟媒体库（按发行平台生成）：只在总开关 + 该库开启时才出现在客户端
        if is_virtual and not virtual_on:
            continue
        # 媒体库封面：有 cover_path 才给 Primary 标记，客户端才会去拉 /Images/Primary
        cover_path = getattr(lib, "cover_path", None)
        cover_tag = _library_cover_tag(cover_path)
        image_tags = {"Primary": cover_tag} if cover_path else {}
        child_count = count_virtual_items(db, lib) if is_virtual else _library_item_count(db, lib)
        # 虚拟库跨电影/剧集聚合，统一按 mixed 上报，客户端才能正常当普通文件夹浏览
        collection_type = "mixed" if is_virtual else lib.collection_type
        # 字段集对照官方 CollectionFolder BaseItemDto（Jellyfin DtoService，
        # ``new DtoOptions()`` = 全字段默认开启 + UserViewsController 显式加
        # PrimaryImageAspectRatio/DisplayPreferencesId）。少一个字段就可能被
        # 严格的三方客户端整条丢弃（PR #372 的 ServerId 只是其中第一个）。
        entry = {
            "Name": lib.name,
            "ServerId": SERVER_ID,
            "Id": lib.guid,
            # 官方 dto.Etag = item.GetEtag(user)：改名 / 换封面 / 条目数变化即失效
            "Etag": hashlib.md5(
                f"{lib.guid}|{lib.name}|{cover_tag}|{child_count}".encode("utf-8")
            ).hexdigest(),
            "Type": "CollectionFolder",
            # 官方 dto.MediaType 恒发（CollectionFolder → Unknown）
            "MediaType": "Unknown",
            "SortName": lib.name,
            "LocationType": "FileSystem",
            "IsFolder": True,
            # 协议面不提供删除媒体库的端点，如实为否（官方按权限下发）
            "CanDelete": False,
            "CanDownload": _download_ok(db),
            "Tags": [],                       # 官方 dto.Tags（全字段默认下发）
            "ImageTags": image_tags,
            "BackdropImageTags": [],
            "ImageBlurHashes": {},            # 官方 AttachBasicFields 无条件置空字典
            # 官方 UserViewsController 显式请求的字段：客户端按它给每个库存显示偏好
            "DisplayPreferencesId": lib.guid,
            "UserData": {
                "PlaybackPositionTicks": 0, "PlayCount": 0, "Played": False,
                "IsFavorite": False, "PlayedPercentage": 0.0,
                # 官方 GetUserItemDataDto 恒发 ItemId / Key
                "ItemId": lib.guid, "Key": lib.guid,
            },
            "ChildCount": child_count,
        }
        if collection_type:
            # 官方 CollectionType 为 null 时字段整个不发（nullable 枚举省略）
            entry["CollectionType"] = collection_type
        if cover_path:
            # 官方 Views 显式带 PrimaryImageAspectRatio；无 Primary 图时官方为 null 不发
            entry["PrimaryImageAspectRatio"] = 0.6666667
        # 官方 dto.DateCreated = item.DateCreated（全字段默认下发）：恒发；
        # created_at 为空的库回退库内最早入库时间，空库回退固定常量。
        entry["DateCreated"] = _iso(
            lib.created_at or _lib_earliest_added.get(lib.id)
            or _LIB_CREATED_AT_FALLBACK
        )
        items.append(entry)
    return {"Items": items, "TotalRecordCount": len(items), "StartIndex": 0}


@json_route(emby_router, "/emby/Users/{user_id}/Items", "/Users/{user_id}/Items")
def get_items(
    request: Request,
    user: models.WebUser = Depends(get_emby_user),
    db: Session = Depends(get_db),
):
    return _query_items(request, user, db, _base_url(request))


@emby_router.get("/emby/Users/{user_id}/Suggestions")
@emby_router.get("/Users/{user_id}/Suggestions")
def get_suggestions(user_id: str,
                    request: Request,
                    user: models.WebUser = Depends(get_emby_user),
                    db: Session = Depends(get_db)):
    """Homepage suggestions: latest additions plus site-wide popular items."""
    limit = int(request.query_params.get("Limit") or 20)
    limit = max(1, min(limit, 50))
    allowed = _library_scope(db, user)
    base_q = (
        db.query(em.MediaItem)
        .filter(
            em.MediaItem.item_type.in_(["movie", "series"]),
            em.MediaItem.is_hidden == False,  # noqa: E712
        )
    )
    base_q = _scope_items(base_q, allowed)
    latest = (
        base_q.order_by(em.MediaItem.date_added.desc().nullslast(),
                        em.MediaItem.id.desc())
        .limit(limit)
        .all()
    )
    popular = []
    try:
        pop_rows = (
            db.query(
                em.MediaItem.id,
                func.sum(em.UserMediaData.play_count).label("plays"),
            )
            .join(em.UserMediaData, em.UserMediaData.item_id == em.MediaItem.id)
            .filter(em.UserMediaData.play_count > 0)
            .group_by(em.MediaItem.id)
            .order_by(func.sum(em.UserMediaData.play_count).desc())
            .limit(limit * 3)
            .all()
        )
        pop_ids = [r.id for r in pop_rows if r.plays]
        if pop_ids:
            pop_items = base_q.filter(em.MediaItem.id.in_(pop_ids)).all()
            series_plays = {}
            play_map = {r.id: r.plays for r in pop_rows}
            for it in pop_items:
                top_id = it.series_id or it.id
                series_plays[top_id] = series_plays.get(top_id, 0) + play_map.get(it.id, 0)
            top_ids = sorted(series_plays, key=lambda i: series_plays[i], reverse=True)[:limit]
            if top_ids:
                id_order = {iid: n for n, iid in enumerate(top_ids)}
                popular = sorted(
                    base_q.filter(em.MediaItem.id.in_(top_ids)).all(),
                    key=lambda it: id_order.get(it.id, 9999),
                )
    except Exception:
        popular = []
    seen = set()
    items = []
    for it in list(latest) + list(popular):
        if it.id not in seen:
            seen.add(it.id)
            items.append(it)
        if len(items) >= limit:
            break
    base = _base_url(request)
    _prefetch_list_data(db, user.id, items)
    return {"Items": [_item_dto(i, base, user.id, db) for i in items],
            "TotalRecordCount": len(items), "StartIndex": 0}


_DEDUP_FIELDS = (
    em.MediaItem.id,
    em.MediaItem.item_type,
    em.MediaItem.library_id,
    em.MediaItem.tmdb_id,
    em.MediaItem.name,
    em.MediaItem.production_year,
    em.MediaItem.poster_path,
    em.MediaItem.primary_image_url,
    em.MediaItem.overview,
    em.MediaItem.series_id,
    em.MediaItem.season_number,
    em.MediaItem.episode_number,
)


def _series_key_map(db, series_ids) -> dict:
    """series id → 去重键（无键时 ("__raw__", sid)）；episode 的去重键要用"""
    series_key_map: dict[int, tuple] = {}
    if series_ids:
        srows = db.query(
            em.MediaItem.id, em.MediaItem.library_id,
            em.MediaItem.tmdb_id, em.MediaItem.name,
            em.MediaItem.production_year,
        ).filter(em.MediaItem.id.in_(list(series_ids))).all()
        for sid, lib_id, tmdb_id, name, year in srows:
            key = dedup_lib.dedup_key_for(lib_id, "series", tmdb_id, name, year)
            series_key_map[sid] = key or ("__raw__", sid)
    return series_key_map


def _dedup_keep_ids(rows, series_key_map: dict) -> set:
    """按去重键分组选主（rows 为 :data:`_DEDUP_FIELDS` 顺序的元组），返回保留的 id 集合"""
    groups: dict = {}
    order: list = []
    for r in rows:
        (iid, itype, lib_id, tmdb_id, name, year, poster,
         primary_url, overview, series_id, season_no, ep_no) = r
        if itype in dedup_lib.DEDUP_TYPES:
            key = dedup_lib.dedup_key_for(lib_id, itype, tmdb_id, name, year)
            if key is None:
                key = ("__raw__", iid)
        elif itype == "episode":
            # 集去重：同 series 去重键 + 同季集号只保留一条
            skey = series_key_map.get(series_id, ("__raw__", series_id or 0))
            key = ("episode", skey, season_no or 0, ep_no or 0)
        else:
            key = ("__raw__", iid)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append((iid, bool(poster or primary_url),
                            bool((tmdb_id or "").strip()),
                            bool((overview or "").strip())))

    keep: set[int] = set()
    for key in order:
        group = groups[key]
        if len(group) == 1:
            keep.add(group[0][0])
            continue
        # 选主：有海报 > 有 tmdb_id > 有简介 > id 最小
        primary = min(group, key=lambda x: (0 if x[1] else 1,
                                            0 if x[2] else 1,
                                            0 if x[3] else 1,
                                            x[0]))
        keep.add(primary[0])
    return keep


def _dedup_primary_ids(cand_rows, db) -> list[int]:
    """对候选 (id, item_type, file_path, library_id) 去重，只保留主记录的 id（保持原顺序）。

    去重在分页之前做，保证 Limit/StartIndex 语义正确：
    Limit=20 就返回 20 条不重复的，TotalRecordCount 也是去重后的总数。

    去重键（参考 Emby 多版本逻辑）：
    - movie/series：有 tmdb_id 用 (library_id, item_type, tmdb_id)，
      无则用 (library_id, item_type, 归一化标题, 年份) —— 不同路径的同一部合并
    - episode：(series去重键, season_number, episode_number) —— 同一集只保留一条

    主记录选择：有海报 > 有 tmdb_id > 有简介 > id 最小（最早入库）。

    这是**全量**实现（O(候选数)），P2 之后只作为 :func:`_dedup_dropped_ids` 放弃时的
    回退与对照测试的基准。
    """
    ids = [row[0] for row in cand_rows]
    if not ids:
        return []

    # 批量取出去重需要的字段（1 次查询）
    rows = db.query(*_DEDUP_FIELDS).filter(em.MediaItem.id.in_(ids)).all()
    series_ids_needed = {r[9] for r in rows if r[1] == "episode" and r[9]}
    keep = _dedup_keep_ids(rows, _series_key_map(db, series_ids_needed))
    return [row[0] for row in cand_rows if row[0] in keep]


#: P2：重复候选 / 被去掉的 id 超过这些量就回退全量实现（避免巨型 IN / CASE）
_DEDUP_MAX_SUSPECTS = int(os.getenv("ITEMS_DEDUP_MAX_SUSPECTS", "20000") or 20000)
_DEDUP_MAX_DROPPED = int(os.getenv("ITEMS_DEDUP_MAX_DROPPED", "5000") or 5000)
_DEDUP_MAX_DUP_SERIES = 2000


def _chunks(seq, n=900):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _dedup_dropped_ids(query, db) -> Optional[set]:
    """P2：只找出候选集里**会被去重去掉**的 id（SQL 里先圈出可能重复的行）。

    旧实现每页都把全部候选拉回 Python 去重（O(全库)/请求，与页码无关）。现在：

    1. 在候选集上用窗口函数 ``COUNT(*) OVER (PARTITION BY 粗桶)`` 圈出「同桶不止一条」的行。
       粗桶是精确去重键的**超集**（键相同 ⇒ 桶相同），所以桶里只有一条的行一定不会被去掉：
       - movie/series 有 tmdb：库 + 类型 + tmdb_id（去掉空白）
       - movie/series 无 tmdb：库 + 类型 + 年份（归一化标题在 Python 里算）
       - episode：剧的去重组 + 季号 + 集号（剧的去重组由全部 series 行在 Python 里算，
         只把真正重复的剧写进 CASE）
       - 其它类型：不参与去重
    2. 只对圈出来的行取字段、跑与旧实现**同一个**分组选主函数（:func:`_dedup_keep_ids`）。

    返回被去掉的 id 集合；圈出来的行太多（几乎全库都是重复）时返回 None，调用方回退全量实现。
    """
    from sqlalchemy import String, case, cast, literal

    MI = em.MediaItem
    # --- 剧的去重组（episode 桶要用）---
    # 能跨剧合并的集，其所属剧必然被某条候选集引用：只看这些剧（不是全部 series）
    ep_series = (query.filter(MI.item_type == "episode", MI.series_id.isnot(None))
                 .with_entities(MI.series_id).order_by(None).distinct().subquery())
    series_group_expr = func.coalesce(MI.series_id, 0)
    srows = db.query(MI.id, MI.library_id, MI.tmdb_id, MI.name, MI.production_year).filter(
        MI.item_type == "series", MI.id.in_(select(ep_series.c.series_id))).all()
    if srows:
        by_key: dict = {}
        for sid, lib_id, tmdb_id, name, year in srows:
            key = dedup_lib.dedup_key_for(lib_id, "series", tmdb_id, name, year)
            if key is not None:
                by_key.setdefault(key, []).append(sid)
        rep: dict[int, int] = {}
        for members in by_key.values():
            if len(members) > 1:
                head = min(members)
                for sid in members:
                    rep[sid] = head
        if len(rep) > _DEDUP_MAX_DUP_SERIES:
            return None
        if rep:
            series_group_expr = case(rep, value=MI.series_id, else_=func.coalesce(MI.series_id, 0))

    tmdb_clean = func.trim(func.replace(func.replace(func.replace(
        func.coalesce(MI.tmdb_id, ""), "\t", ""), "\n", ""), "\r", ""))
    s = lambda x: cast(x, String)  # noqa: E731
    bucket = case(
        (MI.item_type.in_(dedup_lib.DEDUP_TYPES) & (func.length(tmdb_clean) > 0),
         literal("T|") + s(MI.library_id) + "|" + MI.item_type + "|" + tmdb_clean),
        (MI.item_type.in_(dedup_lib.DEDUP_TYPES),
         literal("N|") + s(func.coalesce(MI.library_id, 0)) + "|" + MI.item_type + "|"
         + s(func.coalesce(MI.production_year, 0))),
        (MI.item_type == "episode",
         literal("E|") + s(series_group_expr) + "|" + s(func.coalesce(MI.season_number, 0))
         + "|" + s(func.coalesce(MI.episode_number, 0))),
        else_=literal("R|") + s(MI.id),
    )
    cand = query.with_entities(
        MI.id.label("cid"),
        func.count().over(partition_by=bucket).label("bucket_n"),
    ).order_by(None).subquery()
    suspect_ids = [r[0] for r in db.query(cand.c.cid).filter(cand.c.bucket_n > 1).limit(
        _DEDUP_MAX_SUSPECTS + 1).all()]
    if len(suspect_ids) > _DEDUP_MAX_SUSPECTS:
        return None
    if not suspect_ids:
        return set()
    unique_ids = list(dict.fromkeys(suspect_ids))
    rows = []
    for chunk in _chunks(unique_ids):
        rows.extend(db.query(*_DEDUP_FIELDS).filter(MI.id.in_(chunk)).all())
    series_ids_needed = {r[9] for r in rows if r[1] == "episode" and r[9]}
    keep = _dedup_keep_ids(rows, _series_key_map(db, series_ids_needed))
    dropped = set(unique_ids) - keep
    if len(dropped) > _DEDUP_MAX_DROPPED:
        return None
    return dropped


def _query_items(request: Request, user: models.WebUser, db: Session, base: str) -> dict:
    q = request.query_params
    parent_id = q.get("ParentId")
    include_types = (q.get("IncludeItemTypes") or "").split(",")
    # 是否显式指定了条目类型：顶层浏览/搜索默认只返回 series/movie，
    # 季/单集只允许出现在剧集详情页（显式 IncludeItemTypes 或专用端点）。
    explicit_types = bool(include_types and include_types[0])
    exclude_types = (q.get("ExcludeItemTypes") or "").split(",")
    sort_by = (q.get("SortBy") or "SortName").split(",")
    sort_order = (q.get("SortOrder") or "Ascending").split(",")
    search = (q.get("SearchTerm") or "").strip()
    genres = (q.get("Genres") or "").split("|")
    years = (q.get("Years") or "").split(",")
    # 分级与标签：/Items/Filters 一直把它们列进筛选菜单，但列表端点此前完全忽略——
    # 客户端（含本站用户端）照着菜单选了却拿到全量结果，看着像「筛选没生效」。
    ratings = (q.get("OfficialRatings") or "").split(",")
    tags = (q.get("Tags") or "").split("|")
    # G1-P1-1：Limit/StartIndex 钳制（原裸 int() 无上界，Limit=1000000 可一次拉全库；
    # 畸形输入还直接 500）。条目列表是协议最高频入口，上限给 500。
    start = qstart(q)
    limit = qlimit(q, default=100, hi=500)
    recursive = (q.get("Recursive") or "false").lower() == "true"
    allowed = _library_scope(db, user)
    random_sort = any(c.strip().lower() == "random" for c in sort_by)
    ids = [x.strip() for value in q.getlist("Ids") for x in value.split(",") if x.strip()]

    def _synthetic_names(kind: str, param: str) -> list[str]:
        """把客户端回传的 类型/工作室/年份 Id 还原成名称

        Emby 客户端点「类型/制作公司/年份」时，会用我们给出的**合成 Id**
        再请求 /Items（ParentId 或 GenreIds/StudioIds 参数）。
        """
        raw = [x.strip() for value in q.getlist(param) for x in value.split(",") if x.strip()]
        if not raw:
            return []
        lookup = _synthetic_lookup(db)
        out: list[str] = []
        for rid in raw:
            hit = lookup.get(rid)
            if hit and hit[0] == kind and hit[1] not in out:
                out.append(hit[1])
        return out

    # Filters / IsFavorite 等筛选：客户端“只看收藏 / 已看 / 未看 / 继续观看”依赖它，
    # 旧实现忽略这些参数，导致筛选后返回全量。
    filters = {f.strip().lower() for f in (q.get("Filters") or "").split(",") if f.strip()}
    for flag in ("IsFavorite", "IsPlayed", "IsUnplayed", "IsResumable"):
        if (q.get(flag) or "").lower() == "true":
            filters.add(flag.lower())

    query = db.query(em.MediaItem).filter(em.MediaItem.is_hidden == False)  # noqa: E712

    if ids:
        query = query.filter(em.MediaItem.guid.in_(ids))

    if not ids:
        # 单集多版本：被合并的版本不独立出现在列表里（与 get_episodes 同口径）。
        # 注意只管 episode：电影/剧集的多版本仍走 dedup 层（历史行为，不动）。
        # 按 guid 直接取条目时不过滤——版本切换/播放走直接取条目。
        query = query.filter(
            or_(em.MediaItem.item_type != "episode",
                em.MediaItem.merged_into_id.is_(None))
        )

    if parent_id:
        parent = db.query(em.MediaItem).filter(em.MediaItem.guid == parent_id).first()
        if parent:
            if parent.item_type in ("series", "season"):
                if parent.item_type == "series":
                    # 去重合并：该剧在不同路径可能有多条 series 记录，
                    # 集数要合并展示（查出同去重键的所有 series id）
                    series_ids = dedup_lib.find_duplicate_ids(
                        db, parent.library_id, "series",
                        tmdb_id=parent.tmdb_id, name=parent.name,
                        year=parent.production_year,
                    )
                    series_ids.append(parent.id)
                    series_ids = list(set(series_ids))
                    query = query.filter(
                        or_(em.MediaItem.series_id.in_(series_ids),
                            em.MediaItem.id.in_(series_ids))
                    )
                else:
                    query = query.filter(
                        or_(em.MediaItem.parent_id == parent.id, em.MediaItem.id == parent.id)
                    )
        else:
            # 可能是媒体库
            lib = db.query(em.Library).filter(em.Library.guid == parent_id).first()
            if lib:
                if allowed is not None and lib.id not in allowed:
                    # 直接拿别的库的 guid 打进来：当成不存在（不泄露库名与是否真的存在）
                    raise HTTPException(status_code=404, detail="Not found")
                if getattr(lib, "is_virtual", False):
                    # 虚拟媒体库：跨库的发行平台视图；未开启时直达也 404
                    if not (_virtual_libraries_enabled() and lib.is_enabled):
                        raise HTTPException(status_code=404, detail="Not found")
                    platform = (lib.platform or "").strip()
                    platform_cond = _facet_or_legacy(
                        em.MediaItem.platforms, facets.KIND_PLATFORM, [platform], db
                    ) if platform else None
                    query = (
                        query.filter(platform_cond) if platform_cond is not None
                        else query.filter(em.MediaItem.id == -1)
                    )
                else:
                    query = query.filter(em.MediaItem.library_id == lib.id)
                if not explicit_types:
                    # 媒体库顶层浏览默认只看 series/movie：
                    # 季/单集卡片只出现在剧集详情页内，不平铺到顶层。
                    query = query.filter(em.MediaItem.item_type.in_(["movie", "series"]))
            else:
                # 类型 / 工作室 / 年份的合成 Id：点进去要得到真实筛选结果
                hit = _synthetic_lookup(db).get(parent_id)
                if hit and hit[0] == "Genre":
                    cond = _facet_or_legacy(em.MediaItem.genres, facets.KIND_GENRE, [hit[1]], db)
                    query = query.filter(cond) if cond is not None else query.filter(false())
                elif hit and hit[0] == "Studio":
                    cond = _facet_or_legacy(em.MediaItem.studios, facets.KIND_STUDIO, [hit[1]], db)
                    query = query.filter(cond) if cond is not None else query.filter(false())
                elif hit and hit[0] == "Year" and str(hit[1]).isdigit():
                    query = query.filter(em.MediaItem.production_year == int(hit[1]))
                else:
                    query = query.filter(em.MediaItem.guid == parent_id)
    elif not recursive:
        # 非递归默认返回顶层
        query = query.filter(em.MediaItem.item_type.in_(["movie", "series"]))

    # 媒体库可见范围：搜索 / 递归浏览 / Latest 都走这条路径，一处过滤全覆盖
    query = _scope_items(query, allowed)

    if search and not parent_id and not explicit_types:
        # 全局搜索默认只返回顶层（series/movie）：季/单集不出现在顶层搜索结果里。
        query = query.filter(em.MediaItem.item_type.in_(["movie", "series"]))

    if include_types and include_types[0]:
        type_map = {"Movie": "movie", "Series": "series", "Season": "season", "Episode": "episode"}
        mapped = [type_map.get(t.strip(), t.strip().lower()) for t in include_types if t.strip()]
        if mapped:
            query = query.filter(em.MediaItem.item_type.in_(mapped))

    if exclude_types and exclude_types[0]:
        type_map = {"Movie": "movie", "Series": "series", "Season": "season", "Episode": "episode"}
        mapped = [type_map.get(t.strip(), t.strip().lower()) for t in exclude_types if t.strip()]
        if mapped:
            query = query.filter(~em.MediaItem.item_type.in_(mapped))

    if search:
        # SQL 预筛：标题族 + 别名族，并把繁简/发布标签变体一起放进来，
        # 精排交给 rank_items（完全匹配 > 前缀 > 别名 > 分类 > 模糊）。
        clauses = []
        for variant in search_variants(search):
            like = f"%{variant}%"
            clauses.extend([
                em.MediaItem.name.ilike(like),
                em.MediaItem.original_title.ilike(like),
                em.MediaItem.sort_name.ilike(like),
                em.MediaItem.aliases.ilike(like),
            ])
        if clauses:
            query = query.filter(or_(*clauses))

    # 分类筛选：关联表命中（索引）为主，老库未回填完时自动退回文本列 ILIKE
    genre_names = [g for g in genres if g] or _synthetic_names("Genre", "GenreIds")
    if genre_names:
        cond = _facet_or_legacy(em.MediaItem.genres, facets.KIND_GENRE, genre_names, db)
        if cond is not None:
            query = query.filter(cond)

    studio_names = _synthetic_names("Studio", "StudioIds")
    if studio_names:
        cond = _facet_or_legacy(em.MediaItem.studios, facets.KIND_STUDIO, studio_names, db)
        if cond is not None:
            query = query.filter(cond)

    year_values: list[int] = []
    for y in years:
        if y and y.strip().isdigit():
            year_values.append(int(y))
    for name in _synthetic_names("Year", "Years"):
        if name.isdigit() and int(name) not in year_values:
            year_values.append(int(name))
    if year_values:
        query = query.filter(em.MediaItem.production_year.in_(year_values))

    # 分级是单值列，直接比较（与年份同一类，不需要关联表）
    rating_values = [r.strip() for r in ratings if r.strip()]
    if rating_values:
        query = query.filter(em.MediaItem.official_rating.in_(rating_values))

    # 标签与「类型」一样是多值列，走同一条关联表路径（老库未回填完时退回 ILIKE）
    tag_names = [t for t in tags if t]
    if tag_names:
        cond = _facet_or_legacy(em.MediaItem.tags, facets.KIND_TAG, tag_names, db)
        if cond is not None:
            query = query.filter(cond)

    # 用户数据（UserMediaData）按需外连接：播放类筛选或按用户数据排序（DatePlayed /
    # PlayCount）时才需要。S9：旧实现只在筛选时 join，``SortBy=DatePlayed`` 单独出现
    # 会引用未 join 的列 → SQLite ``no such column`` / PG ``missing FROM-clause`` 500。
    # (user_id, item_id) 有唯一约束，外连接不会让一条条目变成多行。
    _user_sort_keys = {"dateplayed", "playcount"}
    _wants_user_sort = any(c.strip().lower() in _user_sort_keys for c in sort_by)
    if filters & {"isfavorite", "isplayed", "isunplayed", "isresumable"} or _wants_user_sort:
        query = query.outerjoin(
            em.UserMediaData,
            (em.UserMediaData.item_id == em.MediaItem.id)
            & (em.UserMediaData.user_id == user.id),
        )
        if "isfavorite" in filters:
            query = query.filter(em.UserMediaData.is_favorite == True)  # noqa: E712
        if "isplayed" in filters:
            query = query.filter(em.UserMediaData.played == True)  # noqa: E712
        if "isunplayed" in filters:
            query = query.filter(
                or_(em.UserMediaData.played == False, em.UserMediaData.played.is_(None))  # noqa: E712
            )
        if "isresumable" in filters:
            query = query.filter(em.UserMediaData.playback_position_ticks > 0)

    # 排序
    # DateLastContentAdded: 剧集取其下所有单集的最大入库时间（新集入库即刷新），
    # 其他类型取自身 date_added。Rex 等第三方客户端按此排序最新更新。
    _ChildItem = aliased(em.MediaItem)
    _date_last_content_added = func.coalesce(
        select(func.max(_ChildItem.date_added))
        .where(_ChildItem.series_id == em.MediaItem.id)
        .correlate(em.MediaItem)
        .scalar_subquery(),
        em.MediaItem.date_added,
    )
    order_cols = []
    _desc = bool(sort_order and sort_order[0].lower().startswith("desc"))
    for col in sort_by:
        key = col.strip()
        if key in ("DatePlayed", "PlayCount"):
            # 没有用户数据的条目（外连接得 NULL）不论升降序都排在最后；
            # 用 ``IS NULL`` 先排而非 NULLS LAST，SQLite / PG 写法一致。
            c = em.UserMediaData.last_played_at if key == "DatePlayed" else em.UserMediaData.play_count
            order_cols.append(c.is_(None).asc())
            order_cols.append(c.desc() if _desc else c.asc())
            continue
        c = {
            "SortName": em.MediaItem.sort_name, "Name": em.MediaItem.name,
            "DateCreated": em.MediaItem.date_added, "ProductionYear": em.MediaItem.production_year,
            "CommunityRating": em.MediaItem.community_rating,
            "DateLastContentAdded": _date_last_content_added,
        }.get(key)
        if c is None:
            continue
        order_cols.append(c.desc() if _desc else c.asc())
    if not order_cols:
        order_cols = [em.MediaItem.sort_name.asc()]

    if search and not random_sort:
        # 搜索时按相关度排版。旧实现只用 SQL LIKE 过滤 + 按名称排序，
        # 于是精确命中的标题会被“名字里恰好也含这几个字”的条目挤到后面。
        candidates = (
            query.order_by(em.MediaItem.sort_name.asc()).limit(SEARCH_CANDIDATE_LIMIT).all()
        )
        ranked = rank_items(candidates, search)  # 相关度排序（完全匹配 > 前缀 > 别名 > 模糊）
        page = ranked[start:start + limit]
        _prefetch_list_data(db, user.id, page)
        return {
            "Items": [_item_dto(i, base, user.id, db) for i in page],
            "TotalRecordCount": len(ranked),
            "StartIndex": start,
        }

    # 无限滚动的海报墙不需要总数：COUNT(*) 在带筛选/关联时很贵。
    # EnableTotalRecordCount=false 时跳过 COUNT，用“多取一行”做有没有下一页的探测。
    # 注意：Emby 官方从不返回 TotalRecordCount=-1——第三方播放器用
    # TotalRecordCount > Items.length 判断分页，-1 会导致列表显示不全。
    # 因此跳过总数时返回本页数量作为非负总数，继续加载用 HasMore 判断。
    # 默认 true：第三方 Emby 客户端的行为与以前完全一致。
    want_total = (q.get("EnableTotalRecordCount") or "true").strip().lower() not in (
        "false", "0", "no", "off")
    has_more: bool | None = None
    if random_sort:
        # ``ORDER BY RANDOM()`` 会让数据库把整个结果集物化再排序（十万级库就是全表排序）。
        # 随机排序只需要一个随机子集：先取主键、在内存里抽样，再按抽到的 id 取这一页，
        # 成本从「全表排序 + 全行物化」降到「扫主键 + 取 N 行」。
        # 注意：随机模式下跳过去重（Emby 官方随机也是全量随机），保持原有行为。
        id_rows = query.with_entities(em.MediaItem.id).all()
        ids = [row[0] for row in id_rows]
        if not ids:
            return {"Items": [], "TotalRecordCount": 0, "StartIndex": start}
        picked = random.sample(ids, min(len(ids), start + limit))[start:start + limit]
        items = db.query(em.MediaItem).filter(em.MediaItem.id.in_(picked)).all() if picked else []
        position = {item_id: index for index, item_id in enumerate(picked)}
        items.sort(key=lambda item: position.get(item.id, 0))
        total = len(ids) if want_total else len(picked)
    else:
        # 修复：去重必须在分页之前，否则 Limit=20 可能只返回 2-3 条
        # （第三方播放器靠 TotalRecordCount + 分页加载，去重后数量不对会显示不全）。
        #
        # P2（性能审查）：旧实现每页都把全部候选 (id,type,path,lib) 拉回 Python、再按
        # IN(全部 id) 取 12 列（含简介全文）去重后切片——O(全库)/请求，翻第 1 页和第 28 页
        # 一样慢。现在只在 SQL 里圈出「可能重复」的行在 Python 里精确判定，被去掉的 id
        # 用 NOT IN 排除，排序 / 分页 / 计数全部下推到 SQL。结果与旧实现逐条一致
        # （tests/test_items_dedup_sql.py 对照）；重复行多到离谱时回退旧实现。
        # 排序加 id 作为最终决胜键：同名/同时间的并列行在分页之间顺序稳定（否则
        # LIMIT/OFFSET 下并列行可能在两页重复或漏掉）。
        order_cols = [*order_cols, em.MediaItem.id.asc()]
        dropped = _dedup_dropped_ids(query, db)
        if dropped is None:
            cand_rows = (
                query.order_by(*order_cols)
                .with_entities(
                    em.MediaItem.id,
                    em.MediaItem.item_type,
                    em.MediaItem.file_path,
                    em.MediaItem.library_id,
                )
                .all()
            )
            primary_ids = _dedup_primary_ids(cand_rows, db)
            page_ids = primary_ids[start:start + limit]
            # Emby 官方从不返回 -1：跳过总数时用本页数量，保证非负
            total = len(primary_ids) if want_total else len(page_ids)
            if not want_total:
                # 多取一个判断有没有下一页
                has_more = len(primary_ids) > start + limit
        else:
            kept = query.filter(~em.MediaItem.id.in_(dropped)) if dropped else query
            fetch = limit + (0 if want_total else 1)
            page_ids = [
                row[0] for row in kept.order_by(*order_cols)
                .with_entities(em.MediaItem.id).offset(start).limit(fetch).all()
            ]
            if not want_total:
                has_more = len(page_ids) > limit
                page_ids = page_ids[:limit]
                total = len(page_ids)
            else:
                total = kept.order_by(None).count()
        if not page_ids:
            items = []
        else:
            items = db.query(em.MediaItem).filter(em.MediaItem.id.in_(page_ids)).all()
            position = {item_id: index for index, item_id in enumerate(page_ids)}
            items.sort(key=lambda item: position.get(item.id, 0))
    _prefetch_list_data(db, user.id, items)

    return {
        "Items": [_item_dto(i, base, user.id, db) for i in items],
        "TotalRecordCount": total,
        "StartIndex": start,
        # 跳过总数时才带：前端无限滚动用它判断要不要继续加载
        **({} if has_more is None else {"HasMore": has_more}),
    }


@json_route(emby_router, "/emby/Users/{user_id}/Items/Resume", "/Users/{user_id}/Items/Resume")
def get_resume(request: Request, user: models.WebUser = Depends(get_emby_user),
                     db: Session = Depends(get_db)):
    """继续观看：进度只写在单集行上，剧集行永远没有进度；如果直接过滤掉
    episode，剧集的续看永远为空（功能性 bug）。但用户要求 Resume 不出现
    单集卡片（iOS 首页「第 1 集」投诉），所以这里把单集按 series_id 折叠成
    所属剧集返回——官方 Resume 返回的是单集，这是刻意的产品折中。
    """
    # G1-P1-2：Limit 钳制 200（原无上界）
    limit = qlimit(request.query_params, default=12)
    rows = (
        _scope_items(
            db.query(em.UserMediaData, em.MediaItem)
            .join(em.MediaItem, em.MediaItem.id == em.UserMediaData.item_id)
            .filter(
                em.UserMediaData.user_id == user.id,
                em.UserMediaData.playback_position_ticks > 0,
                em.UserMediaData.played == False,  # noqa: E712
                em.MediaItem.item_type.in_(["movie", "series", "episode"]),
                em.MediaItem.is_hidden == False,  # noqa: E712
            ),
            _library_scope(db, user),
        )
        .order_by(em.UserMediaData.last_played_at.desc())
        .limit(limit)
        .all()
    )
    base = _base_url(request)

    # 单集折叠成剧集：进度只在单集行上，series 行永远没有进度；
    # 直接返回单集会在首页出现「第 1 集」卡片（用户投诉），故按 series_id
    # 折叠成所属剧集。同一部剧只保留最近一次播放的（rows 已按 last_played_at 倒序）。
    sids = {i.series_id for _umd, i in rows if i.item_type == "episode" and i.series_id}
    series_map = {}
    if sids:
        series_items = _scope_items(
            db.query(em.MediaItem).filter(
                em.MediaItem.id.in_(sids),
                em.MediaItem.is_hidden == False,  # noqa: E712
            ),
            _library_scope(db, user),
        ).all()
        series_map = {s.id: s for s in series_items}

    items = []
    seen_series = set()
    for _umd, i in rows:
        if i.item_type == "episode":
            sid = i.series_id
            if not sid or sid in seen_series:
                continue
            series_item = series_map.get(sid)
            if series_item is None:
                continue
            seen_series.add(sid)
            items.append(series_item)
        else:
            if i.item_type == "series":
                if i.id in seen_series:
                    continue
                seen_series.add(i.id)
            items.append(i)
    # 去重：同一部剧/电影（同 tmdb_id）在不同路径有多条记录时只保留一条
    items = dedup_lib.deduplicate_items(items)
    _prefetch_list_data(db, user.id, items)
    return {"Items": [_item_dto(i, base, user.id, db) for i in items],
            "TotalRecordCount": len(items), "StartIndex": 0}


@json_route(emby_router, "/emby/Users/{user_id}/Items/Latest", "/Users/{user_id}/Items/Latest")
def get_latest(request: Request, user: models.WebUser = Depends(get_emby_user),
                     db: Session = Depends(get_db)):
    """Latest returns a bare JSON array (Emby/Jellyfin official behavior).

    SenPlayer calls Latest per library (ParentId=<library guid>) for the
    per-library poster rows, so ParentId must filter by library. Unknown
    ParentId -> no extra filter (same as before).
    """
    # G1-P1-2：Limit 钳制 200（原无上界）
    limit = qlimit(request.query_params, default=16)
    q = db.query(em.MediaItem).filter(
        em.MediaItem.item_type.in_(["movie", "series"]),
        em.MediaItem.is_hidden == False,  # noqa: E712
    )
    parent_id = request.query_params.get("ParentId")
    if parent_id:
        lib = db.query(em.Library).filter(em.Library.guid == parent_id).first()
        if lib is not None:
            q = q.filter(em.MediaItem.library_id == lib.id)
    items = (
        _scope_items(q, _library_scope(db, user))
        .order_by(em.MediaItem.date_added.desc())
        .limit(limit)
        .all()
    )
    # 去重：同一部剧/电影在不同路径有多条记录时只保留一条（首页海报行不重复）
    items = dedup_lib.deduplicate_items(items)
    base = _base_url(request)
    _prefetch_list_data(db, user.id, items)
    return [_item_dto(item, base, user.id, db) for item in items]


# /Items/Counts、/Items/Filters、/Items/Intros 必须注册在 /Items/{item_id} 之前，
# 否则会被当成 guid 解析而 404。

@emby_router.get("/emby/Items/Counts")
@emby_router.get("/Items/Counts")
def items_counts(user: models.WebUser = Depends(get_emby_user), db: Session = Depends(get_db)):
    # 一次 GROUP BY 顶掉三个 item_type 计数（客户端启动时就会问这个端点），
    # 总数也走 Core 的 COUNT(*)（不再让 ORM 把实体包一层子查询）。
    # 计数同样按可见范围收：否则客户端导航栏显示 120 部、点进去只有 40 部。
    allowed = _library_scope(db, user)
    by_type = dict(
        _scope_items(
            db.query(em.MediaItem.item_type, func.count())
            .filter(em.MediaItem.is_hidden == False),  # noqa: E712
            allowed,
        )
        .group_by(em.MediaItem.item_type)
        .all()
    )

    return {
        "MovieCount": int(by_type.get("movie", 0)),
        "SeriesCount": int(by_type.get("series", 0)),
        "EpisodeCount": int(by_type.get("episode", 0)),
        "ItemCount": int(_scope_items(
            db.query(func.count()).select_from(em.MediaItem)
            .filter(em.MediaItem.is_hidden == False),  # noqa: E712
            allowed,
        ).scalar() or 0),
        "AlbumCount": 0, "SongCount": 0, "ArtistCount": 0, "AlbumArtistCount": 0,
        "MusicVideoCount": 0, "TrailerCount": 0, "BoxSetCount": 0, "BookCount": 0,
    }


@emby_router.get("/emby/Items/Intros")
@emby_router.get("/Items/Intros")
def items_intros(user: models.WebUser = Depends(get_emby_user)):
    return _empty_items()


# 筛选菜单的取值来自全库（genres/tags/rating/year 都是逗号分隔的文本列）。
# 旧实现每次都把整库 ORM 对象化后遍历：十万级库就是数百 MB 内存尖峰 + 数秒 CPU，
# 而客户端会反复打开这个面板。现改为「只取需要的列 + 分批拉取 + TTL 缓存」。
#
# v2.35.0：缓存不再只是「自己过期」。分类值一变（扫描、图片修复、删条目的同一个
# flush 钩子）就会把这份缓存标成陈旧，下一次请求重建——否则刚扫完的片子在客户端
# 的「类型 / 制片公司」里最长 5 分钟看不到（见 facets.add_change_listener）。
# 重建带最小间隔：扫描期间每个批次都会触发失效，十万级库重建一次约 80ms，
# 不能让它变成「扫描时每请求一次」。
_FILTERS_CACHE: dict = {"at": 0.0, "payload": None, "stale_at": 0.0}
# 合成 Id 反查表的跨请求缓存（按关联表代际失效，见 _synthetic_lookup）
_SYNTHETIC_CACHE: dict = {"gen": None, "map": None}
_FILTERS_CACHE_TTL = float(os.getenv("EMBY_FILTERS_CACHE_TTL", "300") or 300)
# 被标记陈旧后，最快多久重建一次（0 = 只要有变化就立刻重建）
_FILTERS_MIN_REFRESH = float(os.getenv("EMBY_FILTERS_MIN_REFRESH", "5") or 0)


def invalidate_filters_cache() -> None:
    """显式失效筛选菜单缓存：下一次请求必须重建（不等 TTL、不受最小重建间隔约束）

    这个入口是给「明确知道分类值变了」的调用方用的（后台改元数据、测试直接写库）。
    扫描 / 图片修复 / 删条目不经过它：那些挂在 facets 的变更回调上
    （_mark_filters_menu_stale），带最小重建间隔，免得扫描期间每批条目都把菜单重建一次。
    """
    _FILTERS_CACHE["at"] = 0.0
    _FILTERS_CACHE["stale_at"] = 0.0
    _FILTERS_CACHE["payload"] = None


def _mark_filters_menu_stale() -> None:
    """分类值变了（扫描 / 图片修复 / 删条目的同一个 flush 钩子）：把菜单标记成陈旧

    下一次请求会重建，但重建带最小间隔（EMBY_FILTERS_MIN_REFRESH，默认 5 秒）：
    扫描期间每批条目都会触发一次，十万级库重建一次约 80 ms，不能让它变成
    「扫描时每请求一次」。回调本身是纯内存操作，直接挂在写入事务的 flush 钩子上。
    """
    if _FILTERS_CACHE.get("payload") is not None and not _FILTERS_CACHE.get("stale_at"):
        _FILTERS_CACHE["stale_at"] = time.monotonic()


facets.add_change_listener(_mark_filters_menu_stale)


def _filters_payload(db: Session) -> dict:
    cached = _FILTERS_CACHE.get("payload")
    now = time.monotonic()
    if cached is not None:
        stale = bool(_FILTERS_CACHE.get("stale_at"))
        if not stale and now - _FILTERS_CACHE["at"] < _FILTERS_CACHE_TTL:
            return cached
        # 刚被标成陈旧（典型：正在扫描，每批条目都会触发一次）：先给出上一份，
        # 免得扫描期间客户端每点一次筛选面板就重建一次菜单
        if stale and now - _FILTERS_CACHE["stale_at"] < _FILTERS_MIN_REFRESH:
            return cached
    if facets.ensure_ready(db):
        # 流派 / 标签走关联表：一次覆盖索引扇描拿全取值，不再扫全库文本列。
        # （可见性差异：关联表不记 is_hidden，隐藏条目独有的分类值也会出现在菜单里；
        #   点进去的结果集仍会过滤掉隐藏条目，所以只是菜单多一个可选项。）
        genres: set = set(facets.kind_values(db, facets.KIND_GENRE))
        tags: set = set(facets.kind_values(db, facets.KIND_TAG))
    else:
        # 老库还没回填完：沿用旧口径
        genres, tags = set(), set()
        for item_genres, item_tags in (
            db.query(em.MediaItem.genres, em.MediaItem.tags)
            .filter(em.MediaItem.is_hidden == False)  # noqa: E712
            .yield_per(1000)
        ):
            genres.update(g for g in (item_genres or "").split(",") if g)
            tags.update(t for t in (item_tags or "").split(",") if t)
    # 分级与年份是单值列：DISTINCT 只取需要的列（不再把四个列一起读回来物化）
    ratings = {
        r
        for (r,) in db.query(em.MediaItem.official_rating)
        .filter(em.MediaItem.is_hidden == False, em.MediaItem.official_rating.isnot(None))  # noqa: E712
        .distinct()
        .all()
        if r
    }
    years = {
        y
        for (y,) in db.query(em.MediaItem.production_year)
        .filter(em.MediaItem.is_hidden == False, em.MediaItem.production_year.isnot(None))  # noqa: E712
        .distinct()
        .all()
        if y
    }
    payload = {
        "Genres": sorted(genres),
        "Tags": sorted(tags),
        "OfficialRatings": sorted(ratings),
        "Years": sorted(years, reverse=True),
    }
    _FILTERS_CACHE["at"] = time.monotonic()
    _FILTERS_CACHE["payload"] = payload
    return payload


@emby_router.get("/emby/Items/Filters")
@emby_router.get("/Items/Filters")
@emby_router.get("/emby/Items/Filters2")
@emby_router.get("/Items/Filters2")
def items_filters(user: models.WebUser = Depends(get_emby_user), db: Session = Depends(get_db)):
    return _filters_payload(db)






@json_route(emby_router, "/emby/Items/{item_id}", "/Items/{item_id}", "/emby/Users/{user_id}/Items/{item_id}", "/Users/{user_id}/Items/{item_id}")
def get_item_detail(
    item_id: str,
    request: Request,
    user: models.WebUser = Depends(get_emby_user),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,  # noqa: FastAPI 只认裸 BackgroundTasks 注解做特殊注入
):
    if item_id.isdigit():
        # People[].Id 是 emby_people 的行 id（数字串）；客户端点演员会来取详情
        person = db.query(em.EmbyPerson).filter(em.EmbyPerson.id == int(item_id)).first()
        if person is not None:
            return _person_item_dto(person, db)
    item = _require_visible_item(db, user, item_id)
    # 按需探测：缺媒体信息的电影/剧集入队，后台限流探测，不阻塞详情页（v2.51.0）
    try:
        from backend.emby_server import probe_worker
        probe_worker.maybe_enqueue(db, item)
    except Exception:  # noqa: BLE001 — 入队失败不影响详情页
        pass
    # P1：详情页预热 .strm 链路 —— 用户在详情页停留 3~10s，足够把 1.9s 的
    # Drive 跳转走完并写入 P0-2 跳转缓存；点播放时直接命中缓存。
    # 只预热 .strm（maybe_warm 本来也会跳过非 .strm，这里先过滤，避免每次
    # 详情页都起后台任务+读配置）。BackgroundTasks 在响应发出后执行，
    # 不阻塞详情页；与 PlaybackInfo 的预热共用同一事件循环，信号量(4)/
    # 去重(300s)/抖动控制不变，不会放大 Google API 请求（同一文件 300s 内只预热一次）。
    try:
        if (background_tasks is not None and item.file_path
                and str(item.file_path).lower().endswith(".strm")):
            from types import SimpleNamespace
            from backend.emby_server import strm_prewarm

            _warm_item = SimpleNamespace(
                file_path=item.file_path,
                library=getattr(item, "library", None),
                guid=getattr(item, "guid", None),
            )

            async def _warm_and_close():
                from backend.database import SessionLocal
                _warm_db = SessionLocal()
                try:
                    await strm_prewarm.maybe_warm(_warm_db, _warm_item)
                finally:
                    try:
                        _warm_db.close()
                    except Exception:
                        pass

            background_tasks.add_task(_warm_and_close)
    except Exception:  # noqa: BLE001 — 预热失败不影响详情页
        pass
    return _item_dto(item, _base_url(request), user.id, db, full=True,
                     auth_qs=_url_auth_qs(db, request, user, item.guid))


def _season_belongs_to(season: em.MediaItem, show: em.MediaItem) -> bool:
    """这个 season 是不是 show 自己的季（show 可能是 series，也可能本身是 season）

    判据按 Emby 的层级关系来：
    - show 是 series → season.series_id == series.id
    - show 是 season → season.id == show.id（此时不该再按父剧筛）

    这个校验存在的意义：客户端可能带着**别的剧的** SeasonId 来请求，
    那样套上去只会查出 0 条，详情页整片空白（见 get_episodes 的说明）。
    """
    if season.item_type != "season":
        return False
    if show.item_type == "season":
        return season.id == show.id
    return season.series_id == show.id


@json_route(emby_router, "/emby/Shows/{item_id}/Seasons", "/Shows/{item_id}/Seasons")
def get_seasons(item_id: str, request: Request,
                      user: models.WebUser = Depends(get_emby_user),
                      db: Session = Depends(get_db)):
    item = _require_visible_item(db, user, item_id)
    seasons = (
        db.query(em.MediaItem)
        .filter(em.MediaItem.series_id == item.id, em.MediaItem.item_type == "season")
        .order_by(em.MediaItem.season_number)
        .all()
    )
    base = _base_url(request)
    _prefetch_list_data(db, user.id, seasons)
    return {"Items": [_item_dto(s, base, user.id, db) for s in seasons],
            "TotalRecordCount": len(seasons), "StartIndex": 0}


@json_route(emby_router, "/emby/Shows/{item_id}/Episodes", "/Shows/{item_id}/Episodes")
def get_episodes(item_id: str, request: Request,
                       user: models.WebUser = Depends(get_emby_user),
                       db: Session = Depends(get_db)):
    item = _require_visible_item(db, user, item_id)
    season_id = request.query_params.get("SeasonId")
    # 系统性去重合并：该剧在不同路径可能有多条 series 记录，集数合并展示
    series_ids = [item.id]
    if item.item_type == "series":
        dup_ids = dedup_lib.find_duplicate_ids(
            db, item.library_id, "series",
            tmdb_id=item.tmdb_id, name=item.name,
            year=item.production_year,
        )
        series_ids = list(set(series_ids + dup_ids))
    query = db.query(em.MediaItem).filter(
        or_(em.MediaItem.series_id.in_(series_ids), em.MediaItem.parent_id == item.id),
        em.MediaItem.item_type == "episode",
        # 单集多版本：被合并的版本不独立出现在列表里，走主记录的 AlternateVersions
        em.MediaItem.merged_into_id.is_(None),
    )
    if season_id:
        season = db.query(em.MediaItem).filter(em.MediaItem.guid == season_id).first()
        if season and _season_belongs_to(season, item):
            query = query.filter(em.MediaItem.parent_id == season.id)
        else:
            # 客户端带了不属于本剧的 SeasonId（缓存串了、或集数列表被别处复用）。
            # 以前这里照样把 parent_id == 别人的季 套上去，于是 200 + 空 Items：
            # 详情页「第几集」整片空白，而客户端无法区分「这季没集」和「参数错了」。
            # 现在按本剧口径忽略这个筛选，返回本剧全部集 —— 宁可多给，不可给空。
            logger.info(
                "Episodes 收到不匹配的 SeasonId，忽略该筛选：series=%s season_id=%s",
                item.guid, season_id)
    episodes = query.order_by(em.MediaItem.season_number, em.MediaItem.episode_number).all()
    # 集级别去重：同一季集号只保留一条（不同 series 记录下的重复集）
    seen = set()
    deduped = []
    for e in episodes:
        key = (e.season_number or 0, e.episode_number or 0)
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    episodes = deduped
    base = _base_url(request)
    _prefetch_list_data(db, user.id, episodes)
    # 分集标题美化：相关剧名（供文件名去剧名前缀用）。_prefetch_list_data 已把剧载入
    # 并挂到 e.series（查不到 / 已软删的为 None），直接取，不再单独查一次。
    series_names: dict[int, str] = {
        e.series_id: (e.series.name or "")
        for e in episodes
        if e.series_id and e.series is not None
    }
    # Fields=MediaSources：官方 Emby 客户端在选集接口带该参数时期望直接拿到可播放源；
    # 以前这里恒为 []（_item_dto 默认），部分三方客户端因此选集转圈。
    fields = {f.strip().lower() for f in request.query_params.get("Fields", "").split(",")}
    want_sources = "mediasources" in fields
    return {"Items": [_item_dto(e, base, user.id, db, series_names=series_names,
                               full=want_sources,
                               auth_qs=_url_auth_qs(db, request, user, e.guid)
                               if want_sources else "")
                      for e in episodes],
            "TotalRecordCount": len(episodes), "StartIndex": 0}


@emby_router.get("/emby/Shows/{item_id}/MissingEpisodes")
@emby_router.get("/Shows/{item_id}/MissingEpisodes")
def get_missing_episodes(item_id: str,
                         user: models.WebUser = Depends(get_emby_user),
                         db: Session = Depends(get_db)):
    """缺失集数（StrmAssistant 对标）：本地集 vs TMDB 预期集。

    轻量通用能力：纯计算，只读磁盘缓存，零网络请求、无后台任务，默认生效。
    优先用用户选定的剧集组（有缓存时），否则用 TMDB TV 详情（有缓存时）。
    """
    item = _require_visible_item(db, user, item_id)
    if item.item_type != "series":
        raise HTTPException(status_code=400, detail="Not a series")
    return _missing_episodes.compute_missing(db, item)


def _watched_exists(user_id: int):
    """「该条目被这个用户看过 / 有进度 / 有播放次数」的关联子查询（NextUp 口径）"""
    U = em.UserMediaData
    return (
        select(U.id)
        .where(
            U.item_id == em.MediaItem.id,
            U.user_id == user_id,
            or_(U.played == True, U.playback_position_ticks > 0, U.play_count > 0),  # noqa: E712
        )
        .correlate(em.MediaItem)
        .exists()
    )


def _next_up_first_unwatched(db: Session, user_id: int, series_ids) -> dict:
    """一条 SQL 取若干部剧各自「第一集未看的」：{series_id: MediaItem}

    口径与旧的逐剧查询完全一致：item_type=episode、未隐藏、未看过（NOT EXISTS），
    按 season_number / episode_number（NULL 排最后）/ id 取第一条。
    """
    if not series_ids:
        return {}
    MI = em.MediaItem
    rn = func.row_number().over(
        partition_by=MI.series_id,
        order_by=(MI.season_number.asc().nullslast(), MI.episode_number.asc().nullslast(),
                  MI.id.asc()),
    ).label("rn")
    ranked = (
        db.query(MI.id.label("eid"), rn)
        .filter(
            MI.item_type == "episode",
            MI.series_id.in_(list(series_ids)),
            MI.is_hidden == False,  # noqa: E712
            MI.merged_into_id.is_(None),
            ~_watched_exists(user_id),
        )
        .subquery()
    )
    first_ids = db.query(ranked.c.eid).filter(ranked.c.rn == 1)
    rows = db.query(MI).filter(MI.id.in_(first_ids)).all()
    return {row.series_id: row for row in rows}


@json_route(emby_router, "/emby/Shows/NextUp", "/Shows/NextUp", "/emby/Users/{user_id}/Shows/NextUp", "/Users/{user_id}/Shows/NextUp")
def get_next_up(request: Request, user: models.WebUser = Depends(get_emby_user),
                db: Session = Depends(get_db), user_id: str = ""):
    """接下来看：每部「已开看」的剧集只返回下一集未看的单集。

    旧实现直接返回全库未播放单集（按季/集号排序），导致首页把每部剧的
    第 1 集平铺展示。正确口径对齐 Emby NextUp：只收录用户已开看
    （看过至少一集 / 有播放进度 / 有播放次数）的剧集，每部取第一集
    未看的单集，按该剧最近播放时间倒序。
    """
    # G1-P1-2：Limit 钳制 200（原无上界；batch = max(limit*2, 32) 会随 limit 放大整轮扫描）
    limit = qlimit(request.query_params, default=20)
    requested_series_guid = (request.query_params.get("SeriesId") or "").strip()
    allowed = _library_scope(db, user)
    if requested_series_guid:
        # 详情页进入某部未观看的剧时，客户端会先问
        # ``NextUp?SeriesId=<本剧>``。旧实现完全忽略 SeriesId，反而从全库
        # 的已看剧里挑下一集；于是客户端会把另一部剧的 E02 当成本剧首集。
        requested_series = db.query(em.MediaItem).filter(
            em.MediaItem.guid == requested_series_guid,
            em.MediaItem.item_type == "series",
        ).first()
        if requested_series is None:
            return {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}
        if allowed is not None and requested_series.library_id not in allowed:
            # 别的库的剧直接拿来问「下一集」：不泄露它的存在，当没看见
            return {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}
        next_episode = (
            db.query(em.MediaItem)
            .filter(
                em.MediaItem.item_type == "episode",
                em.MediaItem.series_id == requested_series.id,
                em.MediaItem.is_hidden == False,  # noqa: E712
                em.MediaItem.merged_into_id.is_(None),
                ~_watched_exists(user.id),
            )
            .order_by(
                em.MediaItem.season_number.asc().nullslast(),
                em.MediaItem.episode_number.asc().nullslast(),
                em.MediaItem.id.asc(),
            )
            .first()
        )
        if next_episode is None:
            return {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}
        base = _base_url(request)
        _prefetch_list_data(db, user.id, [next_episode])
        return {"Items": [_item_dto(next_episode, base, user.id, db)],
                "TotalRecordCount": 1, "StartIndex": 0}

    watched = (
        db.query(em.UserMediaData.item_id, em.UserMediaData.last_played_at)
        .filter(
            em.UserMediaData.user_id == user.id,
            or_(
                em.UserMediaData.played == True,  # noqa: E712
                em.UserMediaData.playback_position_ticks > 0,
                em.UserMediaData.play_count > 0,
            ),
        )
        .all()
    )
    if not watched:
        return {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}
    last_played = {}
    for w in watched:
        if w.last_played_at and w.item_id not in last_played:
            last_played[w.item_id] = w.last_played_at
    ep_series = (
        _scope_items(
            db.query(em.MediaItem.id, em.MediaItem.series_id)
            # P3：EXISTS 关联子查询代替 IN (全部已看 id)，参数量不随观看记录增长
            .filter(_watched_exists(user.id), em.MediaItem.series_id.isnot(None)),
            allowed,
        )
        .all()
    )
    series_ids = sorted({r.series_id for r in ep_series})
    if not series_ids:
        return {"Items": [], "TotalRecordCount": 0, "StartIndex": 0}
    series_recent = {}
    for eid, sid in ep_series:
        ts = last_played.get(eid)
        if ts and (sid not in series_recent or ts > series_recent[sid]):
            series_recent[sid] = ts
    # P3（性能审查）：旧实现每部已开看的剧一条查询，且每条都绑定 ``NOT IN (全部已看 id)``
    # （352 条 SQL / 1.2s；重度用户几千条观看记录时参数量随之线性增长）。现在：
    # - 剧按「最近播放」倒序（并列按 series_id 升序，与旧实现的稳定排序一致）分批处理，
    #   凑够 limit 部就停；
    # - 每批一条 SQL：ROW_NUMBER() OVER (PARTITION BY series_id ORDER BY 季, 集, id)
    #   取每部剧第一集未看的，「未看」用 NOT EXISTS 关联子查询（不再绑定已看 id 列表）。
    ordered_series = sorted(series_ids, key=lambda sid: series_recent.get(sid) or datetime.min,
                            reverse=True)
    ranked = []
    batch = max(limit * 2, 32)
    for i in range(0, len(ordered_series), batch):
        chunk = ordered_series[i:i + batch]
        nxt_map = _next_up_first_unwatched(db, user.id, chunk)
        for sid in chunk:
            nxt = nxt_map.get(sid)
            if nxt is not None:
                ranked.append((series_recent.get(sid), nxt))
        if len(ranked) >= limit:
            break
    episodes = [e for _, e in ranked[:limit]]
    base = _base_url(request)
    _prefetch_list_data(db, user.id, episodes)
    return {"Items": [_item_dto(e, base, user.id, db) for e in episodes],
            "TotalRecordCount": len(episodes), "StartIndex": 0}


@emby_router.get("/emby/Users/{user_id}/FavoriteItems")
@emby_router.get("/Users/{user_id}/FavoriteItems")
def get_favorites(request: Request, user: models.WebUser = Depends(get_emby_user),
                        db: Session = Depends(get_db)):
    rows = (
        db.query(em.MediaItem)
        .join(em.UserMediaData, em.UserMediaData.item_id == em.MediaItem.id)
        .filter(em.UserMediaData.user_id == user.id, em.UserMediaData.is_favorite == True)  # noqa: E712
        .order_by(em.UserMediaData.updated_at.desc())
        .all()
    )
    base = _base_url(request)
    _prefetch_list_data(db, user.id, rows)
    return {"Items": [_item_dto(i, base, user.id, db) for i in rows],
            "TotalRecordCount": len(rows), "StartIndex": 0}


@emby_router.get("/emby/Users/{user_id}/PlayedItems")
@emby_router.get("/Users/{user_id}/PlayedItems")
def get_played_items(request: Request, user: models.WebUser = Depends(get_emby_user),
                           db: Session = Depends(get_db)):
    rows = (
        db.query(em.MediaItem)
        .join(em.UserMediaData, em.UserMediaData.item_id == em.MediaItem.id)
        .filter(em.UserMediaData.user_id == user.id, em.UserMediaData.played == True)  # noqa: E712
        .order_by(em.UserMediaData.last_played_at.desc())
        .all()
    )
    base = _base_url(request)
    _prefetch_list_data(db, user.id, rows)
    return {"Items": [_item_dto(i, base, user.id, db) for i in rows],
            "TotalRecordCount": len(rows), "StartIndex": 0}


@emby_router.post("/emby/Users/{user_id}/Items/{item_id}/Rating")
@emby_router.post("/Users/{user_id}/Items/{item_id}/Rating")
async def rate_item(
    item_id: str, user_id: str, request: Request,
    user: models.WebUser = Depends(get_emby_user),
    db: Session = Depends(get_db),
):
    body = await request.json()      # 唯一必须 await 的东西，读完就离开事件循环

    def _rate() -> dict:
        """收藏 / 标记已看：读条目、读写 UserMediaData、序列化都不在循环上"""
        item = _require_visible_item(db, user, item_id)
        umd = db.query(em.UserMediaData).filter(
            em.UserMediaData.user_id == user.id, em.UserMediaData.item_id == item.id
        ).first()
        if not umd:
            umd = em.UserMediaData(user_id=user.id, item_id=item.id)
            db.add(umd)
        if "IsFavorite" in body:
            umd.is_favorite = bool(body["IsFavorite"])
        if "Played" in body:
            umd.played = bool(body["Played"])
            if umd.played:
                umd.play_count = (umd.play_count or 0) + 1
                umd.last_played_at = datetime.now()
        db.commit()
        return _user_data_dto(umd, item.guid)

    return await run_in_threadpool(_rate)


@emby_router.post("/emby/Users/{user_id}/PlayedItems/{item_id}")
@emby_router.post("/Users/{user_id}/PlayedItems/{item_id}")
def mark_played(item_id: str, user_id: str,
                      user: models.WebUser = Depends(get_emby_user),
                      db: Session = Depends(get_db)):
    item = _require_visible_item(db, user, item_id)
    umd = db.query(em.UserMediaData).filter(
        em.UserMediaData.user_id == user.id, em.UserMediaData.item_id == item.id
    ).first()
    if not umd:
        umd = em.UserMediaData(user_id=user.id, item_id=item.id)
        db.add(umd)
    umd.played = True
    umd.play_count = (umd.play_count or 0) + 1
    umd.last_played_at = datetime.now()
    umd.playback_position_ticks = 0
    db.commit()
    return _user_data_dto(umd, item.guid)


@emby_router.delete("/emby/Users/{user_id}/PlayedItems/{item_id}")
@emby_router.delete("/Users/{user_id}/PlayedItems/{item_id}")
def mark_unplayed(item_id: str, user_id: str,
                        user: models.WebUser = Depends(get_emby_user),
                        db: Session = Depends(get_db)):
    item = _require_visible_item(db, user, item_id)
    umd = db.query(em.UserMediaData).filter(
        em.UserMediaData.user_id == user.id, em.UserMediaData.item_id == item.id
    ).first()
    if umd:
        umd.played = False
        umd.play_count = 0
        umd.playback_position_ticks = 0
        db.commit()
    return _user_data_dto(umd, item.guid)


# ==================== 播放 ====================

def _play_target(db: Session, item: em.MediaItem):
    """把条目的 ``file_path`` 解析成播放目标（本机文件 或 挂载直链）

    挂载来源（115 / WebDAV / AList / STRM）解析失败时给出明确状态码，不抛 500：

    - 挂载被删除 / 停用 → 404（该条目需要重新扫描或重新绑定挂载）
    - 凭据失效（Cookie / 令牌 / 密码）→ 503（可修，修好后重试即可）
    """
    try:
        return mount_lib.resolve_play_target(item.file_path, db, item.library)
    except mount_lib.MountAuthError as exc:
        raise HTTPException(status_code=503, detail=f"媒体来源凭据失效：{exc}")
    except mount_lib.MountError as exc:
        raise HTTPException(status_code=404, detail=f"媒体来源不可用：{exc}")


@emby_router.post("/emby/Items/{item_id}/PlaybackInfo")
@emby_router.post("/Items/{item_id}/PlaybackInfo")
@emby_router.get("/emby/Items/{item_id}/PlaybackInfo")
@emby_router.get("/Items/{item_id}/PlaybackInfo")
@emby_router.post("/emby/Users/{user_id}/Items/{item_id}/PlaybackInfo")
@emby_router.post("/Users/{user_id}/Items/{item_id}/PlaybackInfo")
@emby_router.get("/emby/Users/{user_id}/Items/{item_id}/PlaybackInfo")
@emby_router.get("/Users/{user_id}/Items/{item_id}/PlaybackInfo")
async def playback_info(
    item_id: str, request: Request, user_id: str = "",
    user: models.WebUser = Depends(get_emby_user),
    db: Session = Depends(get_db),
):
    # 播放链路耗时日志（[play-timing]）：定位"点播放→出画面"慢在哪。
    # 开关：PLAY_TIMING_LOG=0 关闭。只加日志，不改逻辑。
    from backend.emby_server.play_timing import new_timer
    _pt = new_timer(f"playbackinfo item={item_id}")
    # P0（2026-09-29）：async 路由里直接调同步 DB 会卡住单 worker 的事件循环。
    # 所有碰 DB 的同步 helper 都经 run_db 扔线程池。
    from backend.emby_server.async_db import run_db
    # H1：PlaybackInfo 同样要过可见范围（此前只校验了付费墙）
    item = await run_db(_require_visible_item, db, user, item_id)
    # 付费墙：未订阅不发放播放地址（网页端据此展示开通引导，客户端同样不能绕过）
    await run_db(ensure_playback_allowed, db, user)
    _pt.mark("auth")
    # 客户端策略（v2.26.0）：被拦的客户端连播放地址都不该拿到
    await run_db(playback_policy.ensure_client_allowed, db, user, request.headers.get("user-agent"))
    # 按需媒体信息探测：远程文件缺 codec 时入队，后台限流探测，不阻塞播放（v2.51.0）
    try:
        from backend.emby_server import probe_worker
        await run_db(probe_worker.maybe_enqueue, db, item)
    except Exception:  # noqa: BLE001
        pass
    if not item.file_path:
        # 没有媒体路径（虚拟库聚合条目 / 容器 / 源文件已丢失）：
        # 不要发放指向不存在目标的播放地址，否则客户端拿到一个必 404 的 URL。
        # 返回空 MediaSources 是 Emby 客户端认可的「无可播放源」。
        _pt.mark("no_file_path")
        _pt.done()
        return _strip_nulls({
            "MediaSources": [],
            "PlaySessionId": secrets.token_hex(8),
            "ErrorCode": None,
        })
    base = _base_url(request)
    body = {}
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        pass
    device_profile = body.get("DeviceProfile") or {}
    max_bitrate = int(body.get("MaxStreamingBitrate") or 0) or int(
        (device_profile.get("MaxStreamingBitrate") or 0)
    ) or 120_000_000
    # 2026-10 简化：删除服务端码率钳制，客户端要多少给多少（学 Linger 薄服务器思路）
    # 转码开关：关掉就按「只能直连」答复，客户端会直接走直连（而不是拿到一个必 403 的地址）
    allow_transcode = (await run_db(playback_policy.transcode_enabled, db) or bool(user.is_staff)) and bool(getattr(user, "enable_video_transcoding", True))

    # PlaybackInfo 短期缓存（5 分钟）：同一部片子短时间内重复请求直接走 Redis，
    # 省掉 _media_source 的 DB 查询。PlaySessionId 和 api_key 每次重新生成，不进缓存。
    # 缓存 key 包含 user_id（权限不同）+ 设备 profile 指纹 + 码率上限。
    cache_ttl = int(os.getenv("PLAYBACKINFO_CACHE_TTL", "300") or "300")
    cache_key = None
    cached_source = None
    if cache_ttl > 0:
        try:
            profile_fp = hashlib.md5(
                json.dumps(device_profile, sort_keys=True, ensure_ascii=False).encode("utf-8")
            ).hexdigest()[:12]
            cache_key = f"pi:{item.guid}:{user.id}:{profile_fp}:{max_bitrate // 1000}"
            from backend.database import CacheManager
            # S4：redis-py 是同步客户端，Redis 故障时一次 get 最长卡 socket 超时——放线程池
            hit = await run_db(CacheManager.get, cache_key)
            if hit:
                cached_source = json.loads(hit)
        except Exception:  # noqa: BLE001 — 缓存只是优化，失败就走正常流程
            cached_source = None

    if cached_source is None:
        _pt.mark("cache_miss")
        # _media_source 读 item.streams（懒加载关系），必须在线程池里，不能直接在事件循环上碰
        media_source = await run_db(_media_source, item, base, "", db)
        _pt.mark("media_source")
        if cache_key and cache_ttl > 0:
            try:
                from backend.database import CacheManager
                await run_db(CacheManager.set, cache_key,
                             json.dumps(media_source, ensure_ascii=False), ttl=cache_ttl)
            except Exception:  # noqa: BLE001
                pass
    else:
        _pt.mark("cache_hit")
        media_source = cached_source
    if hide_media_path():
        # 升级前写进缓存的 media_source 可能还带着 Path（TTL 内），这里兜底去掉
        media_source.pop("Path", None)
    direct = item.bitrate and item.bitrate <= max_bitrate
    # api_key：只回显 Emby 客户端 token（第三方客户端兼容）。
    # 安全修复 H2：门户 / 管理员 JWT 不再进 URL——网页端只拿短期播放签名（uid/exp/sign）。
    api_key = _api_key_for(db, request)
    # CDN 域名（管理员级开关）：启用时播放面 URL 换 CDN 域名（回源到本服务，
    # 鉴权查询串原样透传；CDN 侧的缓存规则由管理员配置）。未启用时 URL
    # 与升级前逐字节一致。
    use_cdn = await run_db(cdn.enabled, db)
    # 播放短期签名（双轨）：老客户端继续用 api_key；新 URL 额外带 uid/exp/sign，
    # 播放端点优先验签。签名有效期可配（play_sign_ttl_seconds，默认 15 分钟）、绑定
    # user_id+item_id，泄露后窗口极小。网页端（无 api_key）保留 6 小时：
    # 暂停/切片请求可能拖很久（见 PLAY_SIGN_URL_TTL 注释），缩短会断长片播放。
    # P0：async 路由里同步 DB 读走 run_db 线程池（store 热缓存命中时只是内存读）。
    play_ttl = await run_db(play_sign.play_sign_ttl_seconds, db) if api_key else PLAY_SIGN_URL_TTL
    play_exp, play_sig = play_sign.issue_play_sign(user.id, item.guid, play_ttl)
    signed_qs = f"&uid={user.id}&exp={play_exp}&sign={play_sig}"
    key_qs = f"&api_key={api_key}" if api_key else ""
    # 字幕 DeliveryUrl：缓存里的 media_source 不带凭据（不能把别人的凭据缓存出去），
    # 这里按本次请求补上——Emby 客户端补 api_key，门户（JWT）补播放签名，JWT 不进 URL（H2）
    sub_auth = f"api_key={api_key}" if api_key else f"uid={user.id}&exp={play_exp}&sign={play_sig}"
    for _st in media_source.get("MediaStreams") or []:
        _du = _st.get("DeliveryUrl") if isinstance(_st, dict) else None
        if _du and _du.endswith("?api_key="):
            _st["DeliveryUrl"] = _du[: -len("api_key=")] + sub_auth
    stream_url = (
        f"{base}/emby/Videos/{item.guid}/stream?static=true&MediaSourceId={item.guid}{key_qs}{signed_qs}"
    )
    transcoding_url = (
        f"{base}/emby/videos/{item.guid}/master.m3u8?MediaSourceId={item.guid}{key_qs}{signed_qs}"
    )
    # 统一播放 URL 改写（横切能力只许一套）：流节点 > 加速域名 > 原样。
    # 签名查询串原样保留，流节点用同一 SECRET_KEY 验签。
    from backend.emby_server import stream_accel
    stream_url, transcoding_url, _rewrite_src = stream_accel.rewrite_playback_urls_unified(
        db, stream_url, transcoding_url, base)
    # CDN 改写是另一套（老功能）：流节点/加速域名命中时跳过——它们本身就是边缘入口。
    if use_cdn and _rewrite_src is None:
        stream_url = cdn.rewrite_url(db, stream_url, base)
        transcoding_url = cdn.rewrite_url(db, transcoding_url, base)
    media_source.update({
        "SupportsDirectPlay": True,
        "SupportsDirectStream": bool(direct),
        "SupportsTranscoding": allow_transcode,
        "DirectStreamUrl": stream_url,
    })
    if allow_transcode:
        media_source["TranscodingUrl"] = transcoding_url

    try:
        is_pb = request.query_params.get("IsPlayback", "false").lower() == "true"
        if is_pb and item.file_path:
            # 点播即预热（默认 10MB，可配）：并发上限/去重/超时/指标全在
            # playback_prewarm 里；DB 读走 run_db（async 纪律），线程只读文件。
            # 保留 IsPlayback 门控：读 10MB 成本高，只在真点播时触发。
            await run_db(playback_prewarm.maybe_prewarm, db, item.file_path)
        # .strm 链路预热（P0-1）：每次 PlaybackInfo 都触发，不依赖 IsPlayback
        # 参数（客户端实际不带该参数，门控导致预热从未生效）。只是
        # Range: bytes=0-1 探路，成本 1KB。PlaybackInfo 和 /stream 之间有
        # 0.3~2 秒窗口，提前把 Drive 连接暖好。
        # 后台协程，不阻塞 PlaybackInfo 返回；失败静默。
        # session 说明（P3-3）：_warm_db 是给后台任务新建的独立 session（不是请求的 db——
        # 请求的 session 在请求结束时关闭，不能跨协程用）。warm 经 run_db 在线程池
        # 顺序复用该 session（await 期间不另碰，符合 async_db 纪律）；请求结束前
        # 后台任务持有该 session，_warm_and_close 的 finally 负责关闭。
        if item.file_path:
            try:
                from types import SimpleNamespace
                from backend.emby_server import strm_prewarm
                from backend.database import SessionLocal
                _warm_item = SimpleNamespace(
                    file_path=item.file_path,
                    library=getattr(item, "library", None),
                    guid=getattr(item, "guid", None),
                )
                _warm_db = SessionLocal()

                async def _warm_and_close():
                    try:
                        await strm_prewarm.maybe_warm(_warm_db, _warm_item)
                    finally:
                        try:
                            _warm_db.close()
                        except Exception:
                            pass

                asyncio.create_task(_warm_and_close())
                _pt.mark("prewarm_trigger")
            except Exception:
                pass
    except Exception:
        pass

    _pt.done()
    return _strip_nulls({
        "MediaSources": [media_source],
        # 每次播放会话一个独立票据，客户端据此上报进度
        "PlaySessionId": secrets.token_hex(8),
        "ErrorCode": None,
    })


def _observe_traffic(response):
    """给播放响应包一层计字节（可观测用），并记一次请求

    只包 ``body_iterator``，不改状态码、不改头、不改内容——包装器把原迭代器
    原样吐出去，只在旁边数一下一共流了多少字节。计数在迭代器跑完后上报一次，
    不是每块加一次锁；流中途报错/断开也会上报（已经发出去的字节是真的）。

    **Starlette 的 ``FileResponse`` 不走 body_iterator**（它自己处理 Range，
    可能直接用 sendfile），所以那种响应只记请求、不记字节。宁可少算，
    也不按 Content-Length 记账：客户端中途断开时那样会把没发出去的字节算进去，
    面板上的「出流量」就会虚高——宁可口径窄，也不能给一个偏大的数。
    """
    line_stats.record_request()
    iterator = getattr(response, "body_iterator", None)
    if iterator is None:
        return response

    async def _counting():
        total = 0
        try:
            async for chunk in iterator:
                total += len(chunk)
                yield chunk
        finally:
            line_stats.record_bytes(total)

    response.body_iterator = _counting()
    return response


@emby_router.get("/emby/Videos/{item_id}/stream")
@emby_router.get("/Videos/{item_id}/stream")
@release_db_before_response
async def video_stream(
    item_id: str, request: Request,
    user: models.WebUser = Depends(get_play_user),
    db: Session = Depends(get_db),
):
    # 播放链路耗时日志（[play-timing]）：定位"点播放→出画面"慢在哪。
    # 开关：PLAY_TIMING_LOG=0 关闭。只加日志，不改逻辑。
    from backend.emby_server.play_timing import new_timer
    _pt = new_timer(f"stream item={item_id}")
    # P0（2026-09-29）：async 路由里直接调同步 DB 会卡住单 worker 的事件循环。
    from backend.emby_server.async_db import run_db
    # 安全修复（P0 / H1）：条目必须在用户可见的库范围内（统一 helper）
    item = await run_db(_require_visible_item, db, user, item_id)
    _pt.mark("auth_visible")
    # 授权已由 get_play_user 依赖完成（短期签名优先，回退 Emby token / JWT）
    await run_db(ensure_playback_allowed, db, user)
    _pt.mark("auth_playback")
    await run_db(playback_policy.ensure_client_allowed, db, user, request.headers.get("user-agent"))
    _pt.mark("auth_client")
    # 防盗链：白名单为空时直接放行（默认关闭，兼容第三方客户端）
    await run_db(play_sign.check_referer, request, db)
    _pt.mark("auth_referer")
    media_type = f"video/{item.container}" if item.container else "video/mp4"
    target = await run_db(_play_target, db, item)
    _pt.mark("play_target")
    if target.kind == "url":
        # 单一播放路径（2026-10 简化）：中转 + 本地缓存自动层。
        # 分片缓存头：让 CF 边缘能缓存回源结果。
        seg_cache = cdn.cache_control_for(str(request.url.path))
        # 中转：本服务代理转发。Range 与状态码透传，凭据不下发。
        #
        # 远程代理用异步客户端：连源站与等首字节都在等待 I/O，
        # 不能让一个用户的拖动进度条把整个事件循环卡住。
        # 计时器传给 serve_remote_async：它在返回前打 ttfb 等 mark 并 done()。
        return _observe_traffic(
            await serve_remote_async(target.value, request, target.headers, media_type,
                                     cache_control=seg_cache, pt=_pt))
    # 本机文件：直接流形态，分片可被 CDN/浏览器缓存（第 2/3 层预留的另一半）
    _pt.mark("serve_file")
    _pt.done()
    return serve_file(target.value, request, media_type,
                      cache_control=cdn.cache_control_for(str(request.url.path)))


@emby_router.get("/emby/videos/{item_id}/stream.mkv")
@emby_router.get("/emby/videos/{item_id}/stream.mp4")
@emby_router.get("/emby/videos/{item_id}/stream.strm")
@emby_router.get("/emby/Videos/{item_id}/stream.mkv")
@emby_router.get("/emby/Videos/{item_id}/stream.mp4")
@emby_router.get("/emby/Videos/{item_id}/stream.strm")
async def video_stream_ext(
    item_id: str,
    request: Request,
    user: models.WebUser = Depends(get_play_user),
    db: Session = Depends(get_db),
):
    """带扩展名的直接流路由（iOS 客户端用 .mkv/.mp4 后缀请求；

    SenPlayer 按 Emby 约定用 PlaybackInfo 的 Container 拼 stream.strm 取流）
    """
    return await video_stream(item_id, request, user, db)


@emby_router.get("/emby/videos/{item_id}/{transcode_path:path}")
@emby_router.get("/videos/{item_id}/{transcode_path:path}")
@release_db_before_response
async def video_hls(
    item_id: str, transcode_path: str, request: Request,
    user: models.WebUser = Depends(get_play_user),
    db: Session = Depends(get_db),
):
    # SenPlayer 等客户端按 Emby 约定拼 /emby/videos/{id}/stream.{ext} 取流；
    # stream.* 是直接流不是转码路径。若被 catch-all 误收会起 ffmpeg 返 m3u8，
    # 客户端等视频字节流等不到就一直转圈。一律转交直连处理（放最前，避免
    # 与 video_stream 重复做可见性校验；双层 release_db 关闭会话是幂等的）。
    if transcode_path.startswith("stream."):
        return await video_stream(item_id, request, user, db)
    # P0（2026-09-29）：async 路由里直接调同步 DB 会卡住单 worker 的事件循环。
    from backend.emby_server.async_db import run_db
    # 安全修复（P0 / H1）：条目必须在用户可见的库范围内（统一 helper）
    item = await run_db(_require_visible_item, db, user, item_id)
    base = _base_url(request)
    q = request.query_params

    # 已存在的转码会话：直接回放列表/切片
    # 注意：切片请求走 session 票据校验（HLS 播放器无法对切片附加 api_key），
    # 会话本身只在建立转码（master.m3u8 首次请求）时经过完整鉴权创建。
    # H2：子请求沿用本次请求的签名 / Emby token；门户 JWT 不回显进播放列表
    auth_qs = _echo_auth_qs(request)
    existing = q.get("session")
    if existing and get_transcode(existing):
        info = get_transcode(existing)
        # S6：客户端还在拉播放列表 / 切片 = 还有人在看（闲置回收按它判定）
        touch_transcode(existing)
        try:
            file_path = safe_child_name(info["dir"], transcode_path)
            playlist = safe_child_name(info["dir"], "master.m3u8")
        except ValueError:
            raise HTTPException(status_code=404, detail="Invalid transcode path")
        if transcode_path.endswith(".m3u8"):
            # ffmpeg 写完首个切片才落盘播放列表；直接返回空列表会让播放器判定播放失败
            await wait_for_file(playlist, timeout=15.0)
            if not os.path.isfile(playlist):
                if not transcode_alive(existing):
                    raise HTTPException(status_code=503, detail="转码进程已退出，请重新发起播放")
                raise HTTPException(status_code=504, detail="转码尚未产出播放列表")
            # S3：重写要读 CDN 配置（同步 DB）与磁盘上的播放列表：放线程池
            content = await run_db(_rewrite_playlist, info["dir"], base, item.guid, existing,
                                   db=db, auth_qs=auth_qs)
            return Response(
                content, media_type="application/vnd.apple.mpegurl",
                # 播放列表绝不进 CDN/浏览器缓存：内容随时变（会话回收后失效）
                headers={"Cache-Control": cdn.NO_STORE},
            )
        # 客户端请求切片往往早于 ffmpeg 写出，短暂等待而非立即 404
        if not await wait_for_file(file_path, timeout=12.0):
            if not transcode_alive(existing):
                raise HTTPException(status_code=503, detail="转码进程已退出，请重新发起播放")
            raise HTTPException(status_code=404, detail="Segment not ready")
        media_type = "video/mp2t" if transcode_path.endswith(".ts") else "application/octet-stream"
        # 分片是 CDN 缓存的全部意义所在：ffmpeg 写完就不变，边缘放多久都安全。
        # （CDN 未启用时浏览器也能短缓存热点，无副作用。）
        return FileResponse(file_path, media_type=media_type,
                            headers={"Cache-Control": cdn.SEGMENT_CACHE_HEADER})

    # 新转码请求（付费墙 + 客户端与转码策略：建立会话前校验）
    # S3：整段准备（策略校验的同步 DB、115 取直链的同步 HTTP、本地缓存查询/入队、
    # 起 ffmpeg 的 Popen）一次性扔进线程池——旧实现直接在事件循环上做，115 慢的时候
    # 单 worker 的 EA 整个卡住最长 20s+，所有人的播放 / 列表都停。
    content = await run_db(_prepare_new_transcode, db, user, item, request, base, auth_qs)
    return Response(content=content, media_type="application/vnd.apple.mpegurl")


def _prepare_new_transcode(db: Session, user, item, request: Request, base: str,
                           auth_qs: str) -> str:
    """建立一次新的 HLS 转码会话，返回 master 播放列表文本（同步，必须在线程里调）

    包含：防盗链 / 付费墙 / 客户端与转码策略校验、码率归档、播放目标解析（可能是
    115 取直链等同步网络调用）、本地缓存线路查询与入队、转码缓存复用或起 ffmpeg、
    子请求鉴权串与 CDN 改写。
    """
    q = request.query_params
    # 防盗链：白名单为空时直接放行（默认关闭，兼容第三方客户端）；
    # 切片子请求走 session 票据，不经过这里。
    play_sign.check_referer(request, db)
    ensure_playback_allowed(db, user)
    playback_policy.ensure_client_allowed(db, user, request.headers.get("user-agent"))
    # 并发上限按**本机**正在跑的转码数判定：分离部署时 EA 就是那台播放节点
    playback_policy.ensure_transcode_allowed(
        db, user, running=len(active_transcode_ids()), capacity=transcode_capacity(),
    )
    if not shutil.which(os.getenv("EMBY_FFMPEG_PATH", "ffmpeg")):
        raise HTTPException(status_code=503, detail="服务器未安装 ffmpeg，无法转码；请使用直连播放")
    # 2026-10 简化：删除服务端码率钳制，客户端要多少转多少
    video_bitrate = int(q.get("VideoBitrate") or q.get("videoBitrate") or 4_000_000)
    height = int(q.get("Height") or 0) or None
    # 2026-10 简化：删除服务端三档转码。客户端要多少码率/分辨率就转多少，
    # 只在源片分辨率低于请求时不做无意义的上采样（学 Linger 薄服务器思路）。
    from backend.emby_server import transcode as transcode_mod
    height = transcode_mod.clamp_to_source(height, getattr(item, "height", None))
    start_ticks = int(q.get("PositionTicks") or 0)
    start_seconds = start_ticks / TICKS
    target = _play_target(db, item)
    # 转码的拉流字节由 ffmpeg 进程走，不经过本服务的响应体，
    # 所以这里只记请求不记流量（面板上已标明流量口径不含转码拉流）。
    line_stats.record_request()
    # 按需转码 P1：缓存命中直接复用，不再起 ffmpeg；
    # 2 路硬限制超了就 503，让客户端降级走直连
    fingerprint = getattr(item, "file_fingerprint", None)
    cached_dir = transcode_mod.find_cache(item.guid, video_bitrate, height, fingerprint)
    if cached_dir:
        session_id = transcode_mod.register_cache_session(
            cached_dir, user_id=user.id, item_guid=item.guid,
            video_bitrate=video_bitrate, height=height)
    else:
        # D-P1-1：用 transcode_slot() 保证"检查→启动"原子，
        # 突发请求不会绕过并发上限打满 CPU。
        with transcode_mod.transcode_slot():
            session_id = start_transcode(
                target.value, start_seconds, video_bitrate, height,
                user_id=user.id, item_guid=item.guid, input_headers=target.headers,
                cache_key=transcode_mod.cache_key(item.guid, video_bitrate, height, fingerprint),
                fingerprint=fingerprint,
            )
    # 变体与切片地址必须自带 api_key：hls.js 等播放器不会给子请求附加认证头，
    # 旧实现只带 session 导致全部子请求 401（网页端 HLS 播放实际不可用）。
    # CDN 预留（第 2/3 层）：启用时变体/切片都走 CDN 域名（回源本服务）。
    if not auth_qs:
        # 首次请求是 header 里的 JWT（无签名 / 无 Emby token）：给子请求签一把短期播放签名
        auth_qs = _url_auth_qs(db, request, user, item.guid)
    variant_url = (
        f"{base}/emby/videos/{item.guid}/main.m3u8"
        f"?session={session_id}&{auth_qs}"
    )
    if cdn.enabled(db):
        variant_url = cdn.rewrite_url(db, variant_url, base)
    return f"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH={video_bitrate}\n{variant_url}\n"


def _rewrite_playlist(out_dir: str, base: str, item_guid_value: str, session_id: str,  # noqa: D401
                     api_key: str = "", db: Session = None, auth_qs: str = "") -> str:
    """重写 ffmpeg 播放列表：切片指向本服务，并带上 session 票据与 api_key

    不带 api_key 时播放器对切片子请求不会附加认证头，会直接 401。
    CDN 预留（第 2/3 层）：启用时切片行换 CDN 域名（回源本服务）——热门分片
    由边缘缓存，躲源站（Google Drive）单文件配额；播放列表本身不变。
    """
    master = os.path.join(out_dir, "master.m3u8")
    if not os.path.isfile(master):
        return "#EXTM3U\n"
    with open(master, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    if auth_qs:
        suffix = f"&{auth_qs}"
    else:
        suffix = f"&api_key={urllib.parse.quote(api_key)}" if api_key else ""
    seg_base = base
    if db is not None and cdn.enabled(db):
        seg_base = cdn.origin_base(db, base)
    lines = []
    for line in content.splitlines():
        if line.endswith((".ts", ".m4s", ".aac", ".vtt")):
            lines.append(f"{seg_base}/emby/videos/{item_guid_value}/{line}?session={session_id}{suffix}")
        else:
            lines.append(line)
    return "\n".join(lines) + "\n"

# ==================== 拆分说明（v2.13.0）====================
# 本文件原先 2100+ 行，已按关注点拆出三个模块（路由注册顺序与拆分前完全一致）：
#   - media_routes.py  图片投递 / 下载 / hls1
#   - compat_routes.py 会话上报与协议补齐端点
#   - stream_routes.py 播放流容器变体 / 原始文件 / 字幕投递 / HLS 通配
# 三者把路由注册到上面这个 emby_router 上（由 backend/main.py 与 emby_api/main.py 导入触发）。
