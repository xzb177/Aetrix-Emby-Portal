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
import httpx
import json
import logging
import os
import random
import secrets
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Body, Depends, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from sqlalchemy import and_, false, func, or_, select
from sqlalchemy.orm import Session, aliased, joinedload

from backend import library_scope, models, playback_policy
from backend.database import SessionLocal, get_db
from backend.emby_server import cdn
from backend.emby_server.async_db import release_db_before_response
from backend.emby_server import facets
from backend.emby_server.fastjson import json_route
from backend.emby_server import image_store
from backend.emby_server import item_anchor
from backend.emby_server import line_stats
from backend.emby_server import dedup as dedup_lib
from backend.emby_server import items_dedup_cache as _dedup_cache
from backend.emby_server import items_list_cache as _list_cache
from backend.emby_server import list_projection
from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib
from backend.emby_server import play_session
from backend.emby_server import play_sign
from backend.emby_server import play_variants
from backend.emby_server.public_url import client_base_url
from backend.emby_server.query_params import qlimit, qstart
from backend.emby_server import soft_delete
from backend.emby_server import title_beautify
from backend.emby_server import missing_episodes as _missing_episodes

from backend.emby_server import identity_gate, subtitles as subs
from backend.emby_server.auth import (
    _token_from_request,
    get_emby_user,
    get_play_user,
    parse_emby_authorization,
)
from backend.emby_server.env_util import env_float, env_int
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
    serve_file,
    serve_drive_relay_async,
    serve_image,
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

    「结束播放」手上只有播放会话的 ``item_id``，而需要按条目 guid
    定位条目；两者之间需要这一座桥。（转码删除前这里还解释过转码
    会话注册表与播放会话键对不上的旧坑，转码删除后该注册表已不存在。）
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
    """客户端绝对 URL 的基址（播放、字幕、图片、HLS 共用）。

    见 ``public_url.client_base_url``：优先 ``EMBY_PUBLIC_URL``，否则只在可信反代上
    采信 ``X-Forwarded-Proto`` / ``X-Forwarded-Host``，最后才是 ``request.base_url``。
    以前直接回显 ``request.base_url``：TLS 在 nginx 终结、对端是 Docker 网关时 scheme
    仍是 http，SenPlayer 不跟随 80→443 的 301，DirectStreamUrl 永远打不到本服务；
    Host 还可能是源站 IP。
    """
    return client_base_url(request)


def _public_base_url(request: Request) -> str:
    """System/Info 的 LocalAddress / WanAddress。与 ``_base_url`` 同一套基址。"""
    return client_base_url(request)


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


def _provider_ids(item):
    # 单集编号下放：episode 自身无 tmdb/imdb 时继承所属剧的（供 IntroDB 查片头、
    # 第三方客户端跨服匹配按「剧编号+季集号」定位）。
    # 注意语义：这是「所属剧的编号」，不是单集专属编号；剧也没有则不编造。
    tmdb = getattr(item, "tmdb_id", None)
    imdb = getattr(item, "imdb_id", None)
    # 按字段独立继承：自身缺哪个补哪个（自身有 imdb 不影响继承 tmdb）
    if getattr(item, "item_type", "") == "episode" and not (tmdb and imdb):
        series = getattr(item, "series", None)  # 列表页已由 _prefetch_list_data 批量预取
        if series is not None:
            tmdb = tmdb or getattr(series, "tmdb_id", None)
            imdb = imdb or getattr(series, "imdb_id", None)
    return {
        k: v for k, v in (
            ("Tmdb", tmdb),
            ("Imdb", imdb),
            ("Douban", getattr(item, "douban_id", None)),
            ("Bangumi", getattr(item, "bangumi_id", None)),
        ) if v
    }


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
        "ProviderIds": _provider_ids(item),
        "UserData": _user_data_dto(umd, item.guid, _run_time_ticks(item)),
        "MediaSources": [],
        # 三方客户端 Fields 参数常要的字段
        "PrimaryImageAspectRatio": 0.6666667,
        "SyncStatus": "Synced",
    }
    # 真实 Emby 的 Status 是剧集专属字段（SeriesStatus）；电影/季/集不发（null 省略）。
    # 以前全类型硬编码 "Continuing"，与官方口径对齐：只给 series 发。
    if item.item_type == "series":
        dto["Status"] = "Continuing"
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
        # 详情面 MediaSource 的转码申报须与 PlaybackInfo 同源（批二 #11）：
        # 单口要用户本人（策略 or staff 且个人开关），按 user_id 取行传入；
        # 详情是单条目路径，多一次主键查询可接受。取不到就传 None（按策略值）。
        _src_user = None
        try:
            _src_user = db.query(models.WebUser).filter(
                models.WebUser.id == user_id).first()
        except Exception as exc:  # noqa: BLE001
            logger.warning("详情 MediaSource 用户查询失败 user_id=%s: %s",
                           user_id, exc)
        dto["MediaSources"] = [_media_source(item, base, api_key, db, auth_qs,
                                             synthesize=True, user=_src_user)]
        dto["MediaSourceCount"] = 1
        # 片头片尾标记 → Chapters：复用 MediaSource 里已算好的（_chapters_for_item
        # 同一套口径），不重复跑两次 intro 管道
        dto["Chapters"] = (dto["MediaSources"][0].get("Chapters")
                           if dto["MediaSources"] else []) or []
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
            except Exception as exc:  # noqa: BLE001
                logger.warning("合并版本查询失败 item=%s: %s",
                               getattr(item, "id", "?"), exc)
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


def _user_data_dto(umd, item_id: str = "", runtime_ticks: int | None = None) -> dict:
    # 对标 FakEmby：UserData 永不为 null，必填字段全给默认值
    # PlayedPercentage 口径对标真实 Emby/Jellyfin：position_ticks / run_time_ticks * 100。
    # 证据：Jellyfin 真实响应 PlaybackPositionTicks=5918547800 / RunTimeTicks=56177598464
    # → PlayedPercentage=10.535423303637（watchstate#459 实测 dump，公式逐位复算吻合）。
    # 三方客户端用该字段画"继续观看"进度条；恒 0.0 会让看到一半的片子进度条显示 0%。
    # 代码来源（明报）：DeepSeek 本主题 3 种形态全灭（2 次病态 reasoning 空返回 +
    # 1 次网关 RemoteDisconnected），由协调员按已验证逻辑机械落地，待用户裁决是否重做。
    pos = (umd.playback_position_ticks or 0) if umd else 0
    pct = (min(100.0, max(0.0, pos / runtime_ticks * 100.0))
           if (pos > 0 and runtime_ticks) else 0.0)
    base = {"PlaybackPositionTicks": 0, "PlayCount": 0, "Played": False,
            "IsFavorite": False, "PlayedPercentage": 0.0}
    if not umd:
        d = dict(base)
    else:
        d = {
            "PlaybackPositionTicks": pos,
            "PlayCount": umd.play_count or 0,
            "Played": bool(umd.played),
            "IsFavorite": bool(umd.is_favorite),
            "PlayedPercentage": pct,
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
        # 名字必须与 ProviderIds 的键 "Douban" 一致：SenPlayer 按 ProviderIds
        # 自己拼一份链接、再追加 ExternalUrls，靠名字去重；中文名对不上会
        # 在详情页出现两个豆瓣。URL 用 canonical /subject/ 形，按 URL 去重
        # 的客户端也能对上。
        urls.append({
            "Name": "Douban",
            "Url": f"https://movie.douban.com/subject/{douban}/",
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
            "Name": "Douban",
            "Url": "https://search.douban.com/movie/subject_search?search_text="
                    + _url_quote(name),
        })
    return urls


def _emby_num(val):
    """把库里以字符串存的数值型流字段转回 JSON 数字，转不动返回 None（不发该字段）。

    frame_rate / level 在流表里是 String 列：frame_rate 存的是 ffprobe 原始
    avg_frame_rate（如 "30000/1001"、"29.970003"），level 存的是 str(level)
    （如 "51"）。但 Emby 协议里 FrameRate / Level 都是数字，Dart 客户端
    （Hills 等）按 num? 强转，拿到字符串直接抛
    "type 'String' is not a subtype of type 'num?' in type cast"，
    条目详情/播放信息整页打不开。这里在下发边界统一转数字：
    分数按除法算（30000/1001 ≈ 29.97），整数串回 int，小数串回 float。
    """
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        return val
    text = str(val).strip()
    try:
        if "/" in text:
            num, _, den = text.partition("/")
            f = float(num) / float(den)
            return round(f, 3) if f else None
        f = float(text)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if f == int(f):
        return int(f)
    return round(f, 3)


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
        if key in ("FrameRate", "Level"):
            # 协议里是数字、库里存的是字符串，必须转（见 _emby_num）；
            # 转不动就不发这个字段，宁可缺行也不能发字符串把客户端打崩
            val = _emby_num(val)
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
            dto["DeliveryUrl"] = delivery
    # iOS 客户端（Lenna/SenPlayer）对 null 敏感，递归去掉 None 字段
    return _strip_nulls(dto)


def _default_subtitle_index(item: em.MediaItem):
    for s in item.streams:
        if (s.stream_type or "").lower() == "subtitle" and subs.is_text_track(s.codec):
            return s.stream_index
    return None


def _default_audio_stream_index(media_streams: list) -> int | None:
    """默认音频轨索引（r50 补齐，官方 MediaSourceInfo 字段）。

    取 MediaStreams 里 IsDefault 的音频轨的 Index，否则第一条音频轨；
    无音频轨时返回 None（由 _strip_nulls 去掉，不发 null）。
    """
    audios = [st for st in (media_streams or [])
              if isinstance(st, dict) and (st.get("Type") or "").lower() == "audio"]
    for st in audios:
        if st.get("IsDefault"):
            return st.get("Index")
    return audios[0].get("Index") if audios else None


def _chapters_for_item(db, item) -> list:
    """片头片尾标记 → Chapters（r50 抽取；_item_dto 详情与 _media_source 共用一套）。

    对标 StrmAssistant #3：播放器显示"跳过片头"按钮。IntroDB 自动分段
    （2026-10-17 学 raisemby）：按需拉取+缓存后汇入现成 to_chapters 管道，
    手动标记优先；introdb 模块全程 fail-safe，上游故障时这里只剩手动标记，
    播放信息照常返回。
    """
    try:
        from backend.emby_server import intro_marker as _im
        from backend.emby_server import introdb as _idb
        manual = _im.get_markers(db, item.id)
        extra = _idb.get_segments_for_item(db, item)
        return _im.to_chapters(
            _idb.merge_segments(manual, extra, getattr(item, "item_type", None)))
    except Exception as exc:
        logger.debug("Chapters 加载失败 item=%s: %s", getattr(item, "id", "?"), exc)
        return []


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
    ".iso": "iso",
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


def _effective_bitrate(item) -> int | None:
    """Bitrate 发给客户端的值：探测值优先（真实容器总码率），缺失时用 size×8÷时长推导。

    推导值是算术估计（假定恒定码率），不是真实探测值——只能用于播放决策
    （SupportsDirectStream 直放门限）与客户端展示，不能用作精确计量。
    size 或 duration_ticks 任缺一个都不推导，返回 None（会被 _strip_nulls
    整个去掉，绝不编造数字）。真实探测写入 item.bitrate 的路径不受影响。
    """
    bitrate = getattr(item, "bitrate", 0) or 0
    if bitrate:
        return int(bitrate)
    size = getattr(item, "size", 0) or 0
    ticks = getattr(item, "duration_ticks", 0) or 0
    if size and ticks:
        return size * 8 * 10_000_000 // ticks
    return None


def _media_source(item: em.MediaItem, base: str, api_key: str = "", db: Session = None,
                  auth_qs: str = "", synthesize: bool = False, user=None) -> dict:
    # 转码删除（2026-10-24）：服务端不再转码，申报恒 False。
    # 历史上这里硬编码 True，PlaybackInfo 面会被策略覆写、详情 DTO 面
    # 不会，两面申报不一致；删除后两面统一为 False。
    _supports_transcoding = False
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
        # 未探测条目的 Bitrate 兜底：size×8÷duration_ticks 推导（见 _effective_bitrate）。
        # 真实 Emby 扫描后恒有 Bitrate；推导让缺探测条目的直放/展示语义与之对齐。
        "Bitrate": _effective_bitrate(item) or None,
        "SupportsDirectPlay": True,
        "SupportsDirectStream": True,
        "SupportsTranscoding": _supports_transcoding,
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
    # r50：补默认音频轨索引（此前全仓缺失；多音轨时客户端只能逐个试 IsDefault，
    # 与官方字段面不一致）。IsDefault 优先，否则第一条音频轨。
    dto["DefaultAudioStreamIndex"] = _default_audio_stream_index(dto["MediaStreams"])
    if db is not None:
        # r50：MediaSource 也带 Chapters（此前只在详情 DTO 下发；靠 PlaybackInfo
        # 拿章节做"跳过片头"的播放器拿不到）。与详情共用 _chapters_for_item。
        dto["Chapters"] = _chapters_for_item(db, item)
    # 文件名解析的视频信息（v2.49.0）：MediaStreams 为空时客户端「媒体信息」页
    # 也有编码可显示；有流信息时以流为准（这里只是回退）。
    if item.video_codec:
        dto["VideoCodec"] = item.video_codec
    if item.audio_codec:
        dto["AudioCodec"] = item.audio_codec
    # iOS 客户端（Lenna/SenPlayer）对 null 敏感，递归去掉 None 字段
    # 注：DirectStreamUrl 由调用方（playback_info）在拿到 _play_target 后
    # 拼装，CDN 域名改写也在那一层完成（这里只管字幕 DeliveryUrl）。
    # 转码删除后不再有 TranscodingUrl。
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

    # 日志用：这次登录名命中的是自建 Emby 用户名还是门户用户名（只记类型不记值）。
    # 此前日志恒记门户用户名，「接线员随机账号登录异常」类报告无法用日志定案（2026-10-23）
    matched_via = None
    if user is not None:
        matched_via = "emby_username" if user.emby_username == username else "username"

    from backend.emby_server.auth import issue_token, verify_and_migrate_emby_password

    def _auth_fail(detail: str = "用户名或密码错误"):
        from backend.authlog import record_event

        record_event(
            db, username=username, user_id=user.id if user else None, ip=ip,
            agent=request.headers.get("user-agent"), success=False,
            reason="emby_login_failed",
            detail=f"{detail}（via={matched_via}）" if matched_via else detail,
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
        detail=f"{auth.get('Client') or 'Emby Client'} / {token_row.device_id} / via={matched_via}",
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
            # r50 口径统一：与 _user_dto 的 Name 一致（emby_username 优先），
            # 官方两者一致；此前一个用 username、一个用 emby_username。
            "UserName": user.emby_username or user.username,
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
            # r50 诚实化：本服务没有实现任何远程控制接收端点（无 SendCommand /
            # websocket），此前恒 True 属虚假宣称；客户端据此展示遥控 UI 也无处可发。
            "SupportsRemoteControl": False,
            "SupportsMediaControl": False,
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
PLAY_SIGN_URL_TTL = env_int("PLAY_SIGN_URL_TTL", 21600)


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
        "EnableVideoPlaybackTranscoding": False,
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
    if identity_gate.gate_enabled():
        # 身份门开时缓存列是全量口径（含未绑定条目），Views 的 ChildCount
        # 必须与点进去的列表同口径，只数已绑定行——走实时计数（与本函数
        # 缓存为空时的既有回退同路径、同成本），不能用缓存列。
        return soft_delete.count_visible(
            db,
            em.MediaItem.library_id == lib.id,
            em.MediaItem.item_type.in_(["movie", "series"]),
            identity_gate.identity_clause(),
        )
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
    # 列表投影读模型（清单第 16 项试点）：非虚拟库的三值走投影表；
    # 身份门开、开关关、缺行、过期、读错任一 → 整包现行路径（见 list_projection）。
    # 投影行是全量口径，身份门开时 ChildCount 必须按身份过滤现算，不能用。
    _proj = None
    if list_projection.enabled() and not identity_gate.gate_enabled():
        _proj_ids = [lib.id for lib in libs
                     if not bool(getattr(lib, "is_virtual", False))]
        _proj = list_projection.load_projection(db, _proj_ids)
        if _proj is None and _proj_ids:
            logger.warning("媒体库列表投影不可用（缺行/过期/读错），整包回落现算")
    need_earliest = [lib.id for lib in libs if not lib.created_at]
    _lib_earliest_added: dict = {}
    if _proj is not None:
        _lib_earliest_added = {
            lib.id: _proj[lib.id].earliest_added
            for lib in libs
            if not lib.created_at and lib.id in _proj
            and _proj[lib.id].earliest_added is not None
        }
    elif need_earliest:
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
        cover_tag = _proj[lib.id].cover_tag if (_proj is not None and not is_virtual) else _library_cover_tag(cover_path)
        image_tags = {"Primary": cover_tag} if cover_path else {}
        child_count = count_virtual_items(db, lib) if is_virtual else (_proj[lib.id].child_count if _proj is not None else _library_item_count(db, lib))
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


@json_route(emby_router, "/emby/Users/{user_id}/Items", "/Users/{user_id}/Items", "/emby/Items", "/Items")
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
    if identity_gate.gate_enabled():
        # 身份门（货架面）：推荐是系统主动枚举，未绑定条目不进；latest 与
        # popular 都从 base_q 派生，一处挂载两路同口径。
        base_q = base_q.filter(identity_gate.identity_clause())
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
    except Exception as exc:  # noqa: BLE001
        logger.warning("热门播放排行聚合查询失败，降级 popular 为空列表: %s", exc)
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


#: P2：重复候选 / 被去掉的 id 超过这些量就回退全量实现（避免巨型 IN）。
#: 注意：_DEDUP_MAX_DUP_SERIES 已废除——原来它是为保护 case(rep, ...) 巨型 CASE 而设的，
#: rep 条目一多（去纸条化后 merged 行成千上万）CASE 直接让 PG 跑 100 秒+。
#: P0 修复改用 VALUES 映射表 JOIN，rep 再大也只是数据行，不再是表达式分支，
#: 因此不再需要这个上限（2026-10-17 压测实锤）。
_DEDUP_MAX_SUSPECTS = env_int("ITEMS_DEDUP_MAX_SUSPECTS", 100000)
_DEDUP_MAX_DROPPED = env_int("ITEMS_DEDUP_MAX_DROPPED", 50000)


def _chunks(seq, n=900):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _dedup_dropped_ids(query, db) -> Optional[set]:
    """P3：PG 下按去重分支分别跑 ``ROW_NUMBER`` 下推，直接算出被去掉的 id。

    背景：P2 在全部候选行上跑 ``COUNT(*) OVER (PARTITION BY 粗桶)``，
    实测 18.7 万候选里 99.7% 的行桶内只有自己（T 桶零重复、R 桶恒唯一），
    这一步占单次列表请求约 1.9s（2026-10-18 生产实测）。

    新实现利用各分支去重键的精确度差异，分别圈出**可能是重复**的行：
    - T 分支（movie/series 有 tmdb_id）：SQL 桶 == 精确去重键
      ``(库, 类型, 清洗后 tmdb_id)``，``ROW_NUMBER() OVER (PARTITION BY
      精确键 ORDER BY 选主优先级)`` 下推到 DB，``rn > 1`` 即被去掉的 id，
      一次往返、无回表；
    - E 分支（episode）：SQL 桶 == 精确键 ``(剧去重组, 季, 集)``，同理
      ROW_NUMBER 下推；
    - N 分支（movie/series 无 tmdb_id）：SQL 桶是超集（标题归一化在 Python 里算），
      但行数极少（全库约 381 行），全部取出来当候选；
    - R 分支（其它类型）：键恒为 ``("__raw__", id)`` 唯一，不参与去重，直接跳过。

    圈出的候选集与旧窗口实现圈出集的关系：T/E 精确相等、N 为超集、R 同为空
    （旧实现 R 桶 ``'R|' + id`` 恒唯一，永不进候选）。下游仍走同一个
    :func:`_dedup_keep_ids` 精确判定，因此返回的「被去掉的 id 集合」与旧实现
    逐行一致（tests/test_items_dedup_sql.py 对照）。

    剧去重组：与旧实现同一语义（同去重键的剧映射到最小 sid）。旧实现只看
    候选集引用的剧；当前实现仍只看候选集引用的剧（``ep_series`` DISTINCT
    子查询）——不能直接取全量 series：软删除/已合并的剧会把 rep_map 撑到
    上万行，TEMP TABLE 插入从 ~5ms 涨到 ~800ms（2026-10-18 生产实测）。
    未被候选引用的剧在 LEFT JOIN 里永远匹配不上，分组等价性已在 PR 描述中
    证明，对结果无影响。

    返回被去掉的 id 集合；候选太多时返回 None，调用方回退全量实现。
    """
    from sqlalchemy import column, Integer, table, text

    MI = em.MediaItem
    is_pg = bool(db.bind and db.bind.dialect.name == "postgresql")
    # P3.1 修复（2026-10-18）：函数内部用子查询包 ROW_NUMBER，ORM 全局软删除
    # 钩子（_hide_soft_deleted）看不到子查询里的 emby_items（get_final_froms
    # 只认最外层 FROM），已删除行会漏进分区导致误杀可见行。必须显式带
    # deleted_at IS NULL（与钩子重复也无害）。
    query = query.filter(MI.deleted_at.is_(None))
    # --- 剧的去重组（episode 分支要用）---
    # srows 必须是「候选集引用的剧」（与旧实现完全相同的语义）：不能直接取全量
    # series——软删除/已合并的剧会把 rep_map 撑到上万行，TEMP TABLE 插入从 ~5ms
    # 涨到 ~800ms（2026-10-18 生产实测），反而比旧实现慢。
    ep_series = (
        query.with_entities(MI.series_id)
        .filter(MI.item_type == "episode")
        .order_by(None)  # 函数可能被带排序的 query 复用：PG 下 SELECT DISTINCT
        .distinct()      # 遇 ORDER BY 非输出列会报错，这里不需要排序语义
        .subquery()
    )
    srows = (
        db.query(MI.id, MI.library_id, MI.tmdb_id, MI.name, MI.production_year)
        .filter(MI.item_type == "series", MI.id.in_(select(ep_series.c.series_id)))
        .all()
    )
    rep_join = None
    series_group_expr = func.coalesce(MI.series_id, 0)
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
        if rep:
            # P0 修复（2026-10-17 压测实锤）：原来用 case(rep, value=series_id, ...)
            # 把去重组写成 SQL 表达式，rep 条目一多（merged 行成千上万）就是几千个
            # WHEN 分支，PG 实测 105 秒。
            #
            # v2（2026-10-18）：VALUES CTE 写法在 SQLAlchemy 侧编译 250KB SQL 要 321ms/
            # 请求（8.7k 行内联参数）。改成 TEMP TABLE：SQL 静态（编译 ~5ms），
            # 8.7k 行用 executemany 批量插入（~20ms）。同样语义（左连接未命中时回退
            # series_id 自身）。SQLite 测试库不支持 TEMP TABLE ON COMMIT DROP 语法，
            # 走 VALUES CTE 回退路径（测试数据量小，无性能问题）。
            if is_pg:
                db.execute(text("CREATE TEMP TABLE IF NOT EXISTS _dedup_rep_map (sid INTEGER PRIMARY KEY, grp INTEGER) ON COMMIT DROP"))
                db.execute(text("TRUNCATE _dedup_rep_map"))
                # 单条多行 VALUES（一次往返；executemany 1906 行要 0.16s）
                vals = ",".join(f"({sid},{grp})" for sid, grp in rep.items())
                db.execute(text(f"INSERT INTO _dedup_rep_map (sid, grp) VALUES {vals}"))
                rep_tbl = table("_dedup_rep_map", column("sid", Integer), column("grp", Integer))
                rep_join = (rep_tbl, rep_tbl.c.sid == MI.series_id)
                series_group_expr = func.coalesce(rep_tbl.c.grp, MI.series_id, 0)
            else:
                # SQLite（测试）：数据量小，VALUES CTE 足够
                from sqlalchemy import values
                rep_map = values(
                    column("sid", Integer), column("grp", Integer),
                ).data([(sid, grp) for sid, grp in rep.items()]).cte("rep_map")
                rep_join = (rep_map, rep_map.c.sid == MI.series_id)
                series_group_expr = func.coalesce(rep_map.c.grp, MI.series_id, 0)

    # tmdb 去重键：与 dedup_key_for 的 (tmdb_id or "").strip() 严格对应。
    # 生产实测 tmdb_id 无首尾空白（含 \t\n\r 均为 0），btrim/trim 足够精确；
    # 若未来出现 exotic 空白，退化为超集分组（选主仍由精确键决定，不影响正确性，
    # 只是极端情况下可能多算一个候选）。
    _btrim = (lambda c: func.btrim(c, "\t\n\r ")) if is_pg else (lambda c: func.trim(c, "\t\n\r "))
    tmdb_key = func.nullif(_btrim(func.coalesce(MI.tmdb_id, "")), "")
    in_dedup_types = MI.item_type.in_(dedup_lib.DEDUP_TYPES)
    has_tmdb = tmdb_key.isnot(None)

    def _keeper_order():
        """选主优先级（与 _dedup_keep_ids 完全一致）：
        有海报 > 有 tmdb_id > 有简介 > id 最小。布尔 ASC：False(0) 在前。"""
        return [
            (MI.poster_path.is_(None) & MI.primary_image_url.is_(None)).asc(),
            func.nullif(_btrim(func.coalesce(MI.tmdb_id, "")), "").is_(None).asc(),
            func.nullif(_btrim(func.coalesce(MI.overview, "")), "").is_(None).asc(),
            MI.id.asc(),
        ]

    def _dropped_by_rn(base_q, partition_exprs):
        """PG：ROW_NUMBER 按精确去重键分区、按选主优先级排序，
        rn>1 的即被去掉的 id。单条 SQL 直接返回 dropped，无需回表。"""
        sub = (base_q.with_entities(
            MI.id,
            func.row_number().over(
                partition_by=tuple(partition_exprs),
                order_by=_keeper_order()).label("rn")).subquery())
        return {r[0] for r in db.query(sub.c.id).filter(sub.c.rn > 1).all()}

    # T 分支：(库, 类型, tmdb) 精确键
    if is_pg:
        t_dropped = _dropped_by_rn(query.filter(in_dedup_types, has_tmdb),
                                   [MI.library_id, MI.item_type, tmdb_key])
    else:
        t_dropped = set()
    # E 分支：(剧去重组, 季, 集) 精确键
    e_base = (query.outerjoin(*rep_join).filter(MI.item_type == "episode")
              if rep_join is not None
              else query.filter(MI.item_type == "episode"))
    if is_pg:
        e_dropped = _dropped_by_rn(e_base,
                                   [series_group_expr,
                                    func.coalesce(MI.season_number, 0),
                                    func.coalesce(MI.episode_number, 0)])
    else:
        e_dropped = set()
    # N 分支：无 tmdb 的 movie/series（标题归一化在 Python 里算，SQL 做不了），
    # 行数极少（全库约 400），走 Python 精确判定。
    n_ids = [r[0] for r in query.filter(in_dedup_types, ~has_tmdb)
             .with_entities(MI.id)
             .limit(_DEDUP_MAX_SUSPECTS + 1)  # DB 侧上限：超限即回退，防病态 case 拉爆内存
             .all()]
    n_dropped: set = set()
    if n_ids:
        if len(n_ids) > _DEDUP_MAX_SUSPECTS:
            return None
        n_rows = []
        if is_pg:
            from sqlalchemy import text as _text
            id_list = _text("SELECT * FROM unnest(CAST(:ids AS INTEGER[])) AS t(id)").columns(
                column("id", Integer)).cte("n_suspect_ids")
            n_rows = db.query(*_DEDUP_FIELDS).join(
                id_list, id_list.c.id == MI.id).params(ids=n_ids).all()
        else:
            for chunk in _chunks(n_ids):
                n_rows.extend(db.query(*_DEDUP_FIELDS).filter(MI.id.in_(chunk)).all())
        n_keep = _dedup_keep_ids(n_rows, {})
        n_dropped = set(n_ids) - n_keep

    if not is_pg:
        # SQLite（测试）：走原来的 suspects→回表→Python 精确判定路径
        def _dup_member_ids(base_q, group_exprs):
            labels = [e.label(f"dg{i}") for i, e in enumerate(group_exprs)]
            keys = (base_q.with_entities(*labels)
                    .group_by(*group_exprs).having(func.count() > 1).subquery())
            conds = [keys.c[f"dg{i}"] == e for i, e in enumerate(group_exprs)]
            return [r[0] for r in base_q.with_entities(MI.id)
                    .join(keys, and_(*conds)).all()]
        t_ids = _dup_member_ids(query.filter(in_dedup_types, has_tmdb),
                                [MI.library_id, MI.item_type, tmdb_key])
        e_ids = _dup_member_ids(e_base, [series_group_expr,
                                         func.coalesce(MI.season_number, 0),
                                         func.coalesce(MI.episode_number, 0)])
        suspect_ids = t_ids + e_ids + n_ids
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
    else:
        dropped = t_dropped | e_dropped | n_dropped
    if len(dropped) > _DEDUP_MAX_DROPPED:
        return None
    return dropped


_ITEMS_SLOW_LOG_MS = 500.0  # _query_items 分段计时慢日志阈值（ms）；测试可 monkeypatch


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

    # AnyProviderIdEquals：Emby 标准按编号筛选（Reflix 跨服/版本匹配用）。
    # 格式 "Tmdb.519182,Imdb.tt7510222"，逗号分隔任一命中；非法条目忽略。
    # 注意：douban_id/bangumi_id 不是 MediaItem 的列（DTO 里用 getattr 兜底），
    # 这里只支持真实列 tmdb/imdb；其他 provider 前缀忽略。
    _prov_map = {
        "tmdb": em.MediaItem.tmdb_id,
        "imdb": em.MediaItem.imdb_id,
    }
    _prov_conds = []
    for _raw in (q.get("AnyProviderIdEquals") or "").split(","):
        _raw = _raw.strip()
        if "." not in _raw:
            continue
        _p, _pid = _raw.split(".", 1)
        _col = _prov_map.get(_p.strip().lower())
        if _col is not None and _pid.strip():
            _prov_conds.append(_col == _pid.strip())
    if _prov_conds:
        # 单集按所属剧编号也可查到（与 DTO 层 _provider_ids 继承口径一致）
        _hit_series = [r[0] for r in db.query(em.MediaItem.id).filter(
            em.MediaItem.item_type == "series", or_(*_prov_conds)).all()]
        _all_conds = list(_prov_conds)
        if _hit_series:
            _all_conds.append(and_(
                em.MediaItem.item_type == "episode",
                em.MediaItem.series_id.in_(_hit_series)))
        query = query.filter(or_(*_all_conds))

    # HasAnyProviderId：Emby 标准「有任意编号」筛选（与 merge_versions_worker 的注释口径一致）
    if (q.get("HasAnyProviderId") or "").lower() == "true":
        query = query.filter(or_(em.MediaItem.tmdb_id.isnot(None),
                                 em.MediaItem.imdb_id.isnot(None)))

    # 身份准入货架（案 A，一期）：门开时「货架面」只露已绑定条目。挂在
    # 基础查询上，去重层（dropped/primary 恒为本 query 的子集）与随机分支
    # 自然同口径，组内选主只会落在已绑定成员上。拆面纪律（钉死，勿扩）：
    # 带 SearchTerm 是搜索面——不挂（FMBY 实测「浏览严、搜索宽」）；
    # Ids 直查与 AnyProviderIdEquals 编号查是点名/直达——不挂，
    # 凭 guid/编号直达未绑定条目照常（老收藏与在播不断）。
    if (identity_gate.gate_enabled() and not ids and not search
            and not _prov_conds):
        query = query.filter(identity_gate.identity_clause())

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
    elif not recursive and not explicit_types:
        # 非递归默认返回顶层；但客户端显式传了 IncludeItemTypes 时以显式为准
        #（r50：此前 IncludeItemTypes=Episode + 非递归 + 无 ParentId 恒空，
        # 显式要单集被顶层默认吞掉）
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
        # SQL 预筛：标题族 + 别名族 + 拼音派生族，并把繁简/发布标签变体
        # 一起放进来，精排交给 rank_items（完全匹配 > 前缀 > 别名 > 拼音 >
        # 分类 > 模糊）。
        clauses = []
        for variant in search_variants(search):
            like = f"%{variant}%"
            clauses.extend([
                em.MediaItem.name.ilike(like),
                em.MediaItem.original_title.ilike(like),
                em.MediaItem.sort_name.ilike(like),
                em.MediaItem.aliases.ilike(like),
                # 拼音派生字段（批二 #16）：全拼/首字母打法靠这两列捞候选
                em.MediaItem.pinyin_full.ilike(like),
                em.MediaItem.pinyin_initials.ilike(like),
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
            # PremiereDate：Infuse / SenPlayer 等的常用默认排序。此前映射表缺它，
            # 未知键被整段丢弃后静默回退按名称排序（用户体感「排序选了没效果」）。
            "PremiereDate": em.MediaItem.premiere_date,
            "CommunityRating": em.MediaItem.community_rating,
            "DateLastContentAdded": _date_last_content_added,
        }.get(key)
        if c is None:
            # r50：不支持的排序键此前静默回退默认排序；记一条 warning 便于定位
            #（客户端拼错参数时不再一声不吭；回退行为不变）
            logger.warning("Items 忽略不支持的 SortBy=%r，已回退默认排序", key)
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
        # R63-1：随机排序不再全量物化主键。旧实现 `query.with_entities(id).all()`
        # 每次请求把全部匹配 id 拉进 Python（十万级库 = O(全库)/请求）再内存抽样；
        # 旧注释所谓「扫主键 + 取 N 行」里的「扫主键」本身就是全量扫描，并没有真省掉。
        # 新实现：一次 COUNT 定总数，用 row_number() 窗口按随机行号取 id ——
        # 全程只物化本页需要的 id（≤ limit 个），一次索引有序扫描、无排序。
        # 页语义与旧实现一致：旧实现取随机排列的 [start:start+limit] 切片，
        # 每次请求都重排，StartIndex 在随机模式下本就没有跨请求的稳定性；
        # 直接抽 need 个随机行号得到的是同分布的均匀随机子集。
        # 注意：随机模式下跳过去重（Emby 官方随机也是全量随机），保持原有行为。
        total = query.order_by(None).with_entities(func.count()).scalar() or 0
        if not total:
            return {"Items": [], "TotalRecordCount": 0, "StartIndex": start}
        need = min(limit, total - start) if start < total else 0
        picked: list = []
        if need:
            offsets = sorted(random.sample(range(1, total + 1), need))
            id_sub = (
                query.order_by(None).order_by(em.MediaItem.id)
                .with_entities(
                    em.MediaItem.id,
                    func.row_number().over(order_by=em.MediaItem.id).label("rn"),
                )
                .subquery()
            )
            id_rows = (
                db.query(id_sub.c.id, id_sub.c.rn)
                .filter(id_sub.c.rn.in_(offsets))
                .all()
            )
            rn_to_id = {_rn: _id for _id, _rn in id_rows}
            picked = [rn_to_id[rn] for rn in offsets]
            random.shuffle(picked)  # offsets 有序，shuffle 回随机页序（与旧 random.sample 语义一致）
        items = db.query(em.MediaItem).filter(em.MediaItem.id.in_(picked)).all() if picked else []
        position = {item_id: index for index, item_id in enumerate(picked)}
        items.sort(key=lambda item: position.get(item.id, 0))
        total = total if want_total else len(picked)
        # 随机分支这里直接返回：它既没走 page_ids 路径（page_ids 在 else 分支
        # 才定义，继续往下走会 NameError → 500），形态照抄 search 分支。
        _prefetch_list_data(db, user.id, items)
        return {
            "Items": [_item_dto(i, base, user.id, db) for i in items],
            "TotalRecordCount": total,
            "StartIndex": start,
        }
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
        # 列表 ID 清单缓存先行（r41）：命中时请求只剩按主键取本页行 + 用户态
        # 预取 + DTO 构建；去重缓存（dropped 大集合的 Redis 读 + 反序列化）
        # 只在清单未命中时才需要——命中路径上它是纯浪费（结果从未被使用）。
        # 版本号作用域（lib_ids + 各库数据版本）在本请求内恒定，清单/去重两层
        # 共用一次读取：原来各查一次媒体库列表 + 一次版本号直查（每请求 4 次
        # 小查询），现在各一次。
        page_ids = None
        total = None
        # 分段计时（观测）：各段毫秒；总耗时>500ms 打一行结构化日志。
        # 段名：scope_versions=作用域/版本号读取；cache_get=缓存查询（含
        # singleflight 等待者等待时间）；miss_compute=未命中全量计算（去重+
        # 排序+计数+回填）；page_fetch=本页行按主键取回；prefetch=用户态预取；
        # dto_build=DTO 构建。
        _seg = {k: 0.0 for k in ("scope_versions", "cache_get", "miss_compute",
                                 "page_fetch", "prefetch", "dto_build")}
        _t = time.perf_counter()

        def _mark(name):
            nonlocal _t
            _seg[name] += (time.perf_counter() - _t) * 1000.0
            _t = time.perf_counter()

        _lc_ok = _list_cache.is_cacheable(
            filters=filters, sort_by=sort_by, has_ids=bool(ids))
        _lc_libs: Optional[list] = None   # None=作用域未知，整层 fail-closed
        _lc_versions = None
        _lc_flight = None  # singleflight 结果（hit/leader/coalesced/disabled）
        if _lc_ok:
            # 双缓存都关时连作用域都不读：省一次小查询（r41 口径）
            if _list_cache.cache_enabled(db) or _dedup_cache.cache_enabled(db):
                _lc_libs = _list_cache.scope_lib_ids(db, allowed)
            if _lc_libs is None:
                # 作用域未知：整层清单缓存 fail-closed，不读不写；
                # 去重层拿不到预传会走自己的 fail-closed（lib_ids=None → 读失败回 compute() 不写缓存）。
                # 绝不能拿 [] 充数：[] 与 allowed=set() 的空作用域同键，
                # DB 抖动时把清单写进去会污染不可见用户。
                _lc_ok = False
                _mark("scope_versions")
            else:
                # 只在清单缓存开着时预读版本号：开关关时 get/put 内部直接返回，
                # 预读的一次版本号查询反而是浪费；去重层没拿到预传会自己读。
                if _list_cache.cache_enabled(db):
                    _lc_versions = _dedup_cache.read_data_versions(db, _lc_libs)
                _mark("scope_versions")
                # singleflight 合并读：同一 key 并发未命中时只一个领头计算，
                # 其余等待者有界等待后复用结果；超时/领头失败则自行计算。
                # #1053 语义逐字保留：_lc_libs=None（作用域未知）时整层
                # fail-closed（本分支根本到不了这里），空集合与 None 分键
                # 由 scope_lib_ids 保证。
                _lc_flight = _list_cache.coalesced_get(db, q, _lc_libs,
                                                       versions=_lc_versions,
                                                       want_total=want_total)
                _lc_hit = _lc_flight.page
                if _lc_hit is not None and start + limit <= len(_lc_hit.ids):
                    page_ids = _lc_hit.ids[start:start + limit]
                    if want_total:
                        total = _lc_hit.total
                    else:
                        has_more = _lc_hit.total > start + limit
                        total = len(page_ids)
                _mark("cache_get")
        if _seg["scope_versions"] == 0.0:
            # 不可缓存请求（is_cacheable=False）：上面没走作用域分支。
            # 纯观测修正：用独立计时起点——函数入口以来的参数解析时间
            # 不再记到 scope_versions 名下。
            _t = time.perf_counter()
            _mark("scope_versions")
        if page_ids is None:
            with _list_cache.leader_slot(_lc_flight):  # singleflight 领头槽：退出时（成功/抛错）必释放标记、唤醒等待者；非领头时空操作
                # 列表去重缓存（2026-10-18）：dropped 集合按（用户, 查询参数,
                # 各库数据版本）缓存；开关关 / Redis 不可用时逐行走旧路径。
                dropped = _dedup_cache.dedup_dropped_ids_cached(
                    request, query, db, user, allowed,
                    lambda: _dedup_dropped_ids(query, db),
                    lib_ids=_lc_libs if _lc_ok else None,
                    versions=_lc_versions)
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
                    # 未命中清单缓存：走现状的 id-only 排序分页路径（见下）；dropped
                    # 在上面已按需取到（清单命中时根本不查去重缓存，见本函数上方注释）。
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
                        # R4（2026-10-18）：dropped 恒为 query 的子集（T/E/N 分支都从 query
                        # 里圈出），故 count(kept) = count(query) - len(dropped)，数学精确
                        # 相等。省掉 16k NOT IN 的 COUNT（生产实测 ~100ms）。
                        total = query.order_by(None).count() - (len(dropped) if dropped else 0)
                    # 未命中时顺手回填：一条 id-only 全量排序查询的成本被后续命中摊薄。
                    # 排序口径与上面的 page_ids 查询逐字一致（含 id 决胜键），结果天然一致。
                    # 只在 want_total=true 时回填：缓存的 total 必须是真实 kept 总数，
                    # 而 want_total=false 的请求本来就是为了跳过 COUNT 才来的——
                    # 不为它多付一次 COUNT；false 请求只读缓存不写缓存。
                    if _lc_ok and want_total:
                        _lc_max = _list_cache.cache_max_ids(db)
                        _lc_probe = [
                            row[0] for row in kept.order_by(*order_cols)
                            .with_entities(em.MediaItem.id).limit(_lc_max + 1).all()
                        ]
                        _list_cache.put_cached_page(
                            db, q, _lc_libs, _lc_probe[:_lc_max], total,
                            len(_lc_probe) > _lc_max, versions=_lc_versions)
        _mark("miss_compute")
        if not page_ids:
            items = []
        else:
            items = db.query(em.MediaItem).filter(em.MediaItem.id.in_(page_ids)).all()
            position = {item_id: index for index, item_id in enumerate(page_ids)}
            items.sort(key=lambda item: position.get(item.id, 0))
        _mark("page_fetch")
    _prefetch_list_data(db, user.id, items)
    _mark("prefetch")
    _items = [_item_dto(i, base, user.id, db) for i in items]
    _mark("dto_build")
    _seg_total = sum(_seg.values())
    if _seg_total > _ITEMS_SLOW_LOG_MS:
        # 慢才打一行结构化日志，防日志洪水
        logger.warning(
            "items 列表慢请求 total=%.0fms scope_versions=%.0fms "
            "cache_get=%.0fms miss_compute=%.0fms page_fetch=%.0fms "
            "prefetch=%.0fms dto_build=%.0fms flight=%s start=%s limit=%s "
            "want_total=%s n_items=%s",
            _seg_total, _seg["scope_versions"], _seg["cache_get"],
            _seg["miss_compute"], _seg["page_fetch"], _seg["prefetch"],
            _seg["dto_build"],
            _lc_flight.mode if _lc_flight else "n/a",
            start, limit, want_total, len(_items))

    return {
        "Items": _items,
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
                # 客户端 HideFromResume 标记过的条目不再出现在继续观看里
                em.UserMediaData.hide_from_resume == False,  # noqa: E712
                em.MediaItem.item_type.in_(["movie", "series", "episode"]),
                em.MediaItem.is_hidden == False,  # noqa: E712
                # 副本绝不能占 Resume 的 limit 名额：合并 worker 不搬运播放记录，
                # 副本的播放行会先占名额、再被 deduplicate_items 扔掉，导致列表缩水。
                em.MediaItem.merged_into_id.is_(None),
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
                # 被合并的 series 不能当展示条目（会被 deduplicate_items 扔掉致缺行）
                em.MediaItem.merged_into_id.is_(None),
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
        # 副本（merged_into_id 非空）绝不能占掉 Latest 的 limit 名额：
        # 先查 16 条再内存去重时，若最新 16 条全是副本会返回空数组。
        em.MediaItem.merged_into_id.is_(None),
    )
    if identity_gate.gate_enabled():
        # 身份门（货架面）：与上面的副本过滤同理，必须进 SQL——未绑定
        # 条目先占 limit 名额再被剔除会让「最新」列表缩水。
        q = q.filter(identity_gate.identity_clause())
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
    # 身份门（货架面）：计数必须与列表同口径，未绑定条目不计——否则
    # 导航栏写 120 部、点进去只有 40 部。口径定死为「已绑定行数」（沿用
    # 本端点既有的行数语义、只加身份维度；episode/season 按判据跟随
    # series）；与列表 TotalRecordCount 的「代表组数」完全同源留待三期
    # 与代表逻辑一起收，本期不在此先行。
    _gate_clause = (identity_gate.identity_clause()
                    if identity_gate.gate_enabled() else None)
    _by_type_q = db.query(em.MediaItem.item_type, func.count()).filter(
        em.MediaItem.is_hidden == False)  # noqa: E712
    _total_q = db.query(func.count()).select_from(em.MediaItem).filter(
        em.MediaItem.is_hidden == False)  # noqa: E712
    if _gate_clause is not None:
        _by_type_q = _by_type_q.filter(_gate_clause)
        _total_q = _total_q.filter(_gate_clause)
    by_type = dict(
        _scope_items(_by_type_q, allowed)
        .group_by(em.MediaItem.item_type)
        .all()
    )

    return {
        "MovieCount": int(by_type.get("movie", 0)),
        "SeriesCount": int(by_type.get("series", 0)),
        "EpisodeCount": int(by_type.get("episode", 0)),
        "ItemCount": int(_scope_items(_total_q, allowed).scalar() or 0),
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
_FILTERS_CACHE_TTL = env_float("EMBY_FILTERS_CACHE_TTL", 300.0)
# 被标记陈旧后，最快多久重建一次（0 = 只要有变化就立刻重建）
_FILTERS_MIN_REFRESH = env_float("EMBY_FILTERS_MIN_REFRESH", 5.0)


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
    return _item_dto(item, _base_url(request), user.id, db, full=True,
                     auth_qs=_url_auth_qs(db, request, user, item.guid))


_FAMILY_BFS_MAX_DEPTH = 8


def _family_series_ids(db: Session, show: em.MediaItem) -> set:
    """认亲：沿 merged_into_id 反向 BFS 找「分身行」，组装本剧的家族 series_id 集合。

    背景（生产实证）：一部剧在库里常有多个 series 行——露面行
    （merged_into_id 为空）+ 若干被合并隐藏的分身行（merged_into_id 指向
    露面行，可多层）。真集常挂在分身的季下；而 get_episodes 的
    find_duplicate_ids 按「同库 + tmdb 相同，或同库 + 归一化名相同 +
    年份 ±1」认亲——分身名带年份后缀「(2024)」时归一化后多了数字，
    对不上（生产实测 3,747 部受此影响），导致带露面季 SeasonId 的
    Episodes 请求归零（真集 parent 指向分身季）。

    口径（与生产实测口径对齐）：
    - 入口本身是分身行时，先沿 merged_into_id 向上走到露面行（链顶端，
      防环 + 深度上限），再从露面行反向 BFS——家族口径与入口无关；
      互指环/链回环时没有真正的露面行，退回以入口为根（环上异名行
      不得无条件入集，见 test_cycle_terminates）；
    - 反向 BFS（item_type='series'、deleted_at IS NULL），visited 防环，
      层数上限防御（耗尽时打 warning，不静默）；
    - 成员准入只对**露面行**判定：与露面行同 library_id，且
      （tmdb_id 非空且相等，或 dedup.normalize_name(name) 相等）。
      不链式放宽：B 只像 A、不像露面行时，B 不入集；
    - 露面行与入口行本身无条件入集；非 series 的 show 只返回 {show.id}。
    """
    family = {show.id}
    if show.item_type != "series":
        return family
    # 入口可能是分身行（陈旧书签/客户端缓存直达）：先向上走到露面行，
    # 再从露面行反向 BFS，保证家族口径与入口无关。
    root = show
    _seen_up = {show.id}
    for _ in range(_FAMILY_BFS_MAX_DEPTH):
        nxt = root.merged_into_id
        if not nxt:
            break
        if nxt in _seen_up:
            # 互指环/链回环：没有真正的露面行，退回以入口为根——
            # 否则环上的异名行会被无条件入集（test_cycle_terminates）。
            root = show
            break
        parent = (
            db.query(em.MediaItem)
            .filter(
                em.MediaItem.id == nxt,
                em.MediaItem.item_type == "series",
                em.MediaItem.deleted_at.is_(None),
            )
            .first()
        )
        if parent is None:
            break
        _seen_up.add(parent.id)
        root = parent
    family.add(root.id)
    root_norm = dedup_lib.normalize_name(root.name)
    root_tmdb = (root.tmdb_id or "").strip()
    visited = {root.id}
    frontier = [root.id]
    for _ in range(_FAMILY_BFS_MAX_DEPTH):
        if not frontier:
            break
        # 遍历不过滤 library：merged 写入按库隔离（dedup 逐库合并），
        # 跨库中间行只被"穿过"、准入仍要求同库；若将来允许跨库合并，
        # 这里要补 library 过滤。
        children = (
            db.query(em.MediaItem)
            .filter(
                em.MediaItem.merged_into_id.in_(frontier),
                em.MediaItem.item_type == "series",
                em.MediaItem.deleted_at.is_(None),
            )
            .all()
        )
        frontier = []
        for child in children:
            if child.id in visited:
                continue
            visited.add(child.id)
            frontier.append(child.id)
            # 准入只对露面行判定：BFS 继续穿过所有分身行（孙行可能直接像露面行），
            # 但只有像露面行的行才入集。
            if child.library_id != root.library_id:
                continue
            child_tmdb = (child.tmdb_id or "").strip()
            if root_tmdb and child_tmdb and root_tmdb == child_tmdb:
                family.add(child.id)
            elif root_norm and dedup_lib.normalize_name(child.name) == root_norm:
                family.add(child.id)
    if frontier:
        # 深度上限耗尽：家族可能不完整（部分集不可达），必须可观测
        logger.warning("家族 BFS 深度上限(%s)耗尽，家族可能不完整 show=%s root=%s",
                       _FAMILY_BFS_MAX_DEPTH, show.id, root.id)
    return family


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


# 虚拟季的协议 Id：``vseason-<剧 db id>-<季号>``，与真实 guid 形制不同不会碰撞。
_VIRTUAL_SEASON_ID_PREFIX = "vseason-"


def _virtual_season_id(series_db_id: int, season_number: int) -> str:
    return f"{_VIRTUAL_SEASON_ID_PREFIX}{series_db_id}-{season_number}"


def _parse_virtual_season_id(season_id) -> "tuple[int, int] | None":
    """解析虚拟季 Id → (series_db_id, season_number)；非法返回 None。"""
    if not isinstance(season_id, str) or not season_id.startswith(_VIRTUAL_SEASON_ID_PREFIX):
        return None
    try:
        db_id_s, num_s = season_id[len(_VIRTUAL_SEASON_ID_PREFIX):].rsplit("-", 1)
        db_id, num = int(db_id_s), int(num_s)
    except (ValueError, AttributeError):
        return None
    if db_id <= 0 or num < 0:
        return None
    return db_id, num


def _episode_base_filters(item: em.MediaItem, series_ids: list) -> list:
    """get_episodes 的基础集过滤（不带季筛选）：series 口径 = 露面行 +
    find_duplicate_ids 认亲 + _family_series_ids 家族认亲；单集多版本
    （merged_into_id 非空）不独立出现在列表里，走主记录的 AlternateVersions。

    get_seasons 的桶B 虚拟季判定复用同一口径——「有可见集」的定义两处必须
    一致，口径漂移由 tests/test_missing_episodes_virtual_season.py 的
    源码断言发现。
    """
    return [
        or_(em.MediaItem.series_id.in_(series_ids), em.MediaItem.parent_id == item.id),
        em.MediaItem.item_type == "episode",
        em.MediaItem.merged_into_id.is_(None),
    ]


def _missing_season_numbers(db: Session, item: em.MediaItem, series_ids: list,
                            present: set) -> list[int]:
    """桶B（缺集普查 2026-10-19）：有可见集（含家族∪dedup 认亲，与
    get_episodes 同口径）但无可见季行的季号，升序。season_number 为空的不算。"""
    q = db.query(em.MediaItem.season_number).filter(
        *_episode_base_filters(item, series_ids),
        em.MediaItem.season_number.isnot(None),
    )
    if present:
        q = q.filter(~em.MediaItem.season_number.in_(present))
    return sorted({n for (n,) in q.distinct().all()})


def _virtual_season_dto(item: em.MediaItem, season_number: int, ep_count: int,
                        db: Session) -> dict:
    """虚拟季条目 DTO：与 _item_dto 的季分支同字段约定（客户端渲染季行所需的键）。

    季名由季号推导（与库内季行命名 ``第 N 季`` 同形）。图片键留空——虚拟季
    没有独立图片行，客户端按无图占位处理；UserData 为默认未看。
    """
    vid = _virtual_season_id(item.id, season_number)
    name = f"第 {season_number} 季"
    etag = hashlib.md5(
        f"vseason|{item.guid}|{season_number}|{ep_count}".encode("utf-8", "ignore")
    ).hexdigest()
    ok = _download_ok(db)
    dto = {
        "Name": name, "SortName": name, "Id": vid, "ServerId": SERVER_ID,
        "Etag": etag, "Type": "Season", "IsFolder": True, "MediaType": "Video",
        "LocationType": "FileSystem", "IsHidden": False, "CanDelete": False,
        "CanDownload": ok, "SupportsSync": False, "SupportsContentDownloading": ok,
        "LockData": False, "LockedFields": [],
        "Genres": [], "GenreItems": [], "Tags": [], "Taglines": [],
        "Studios": [], "Countries": [], "Languages": [],
        "People": [], "RemoteTrailers": [], "ExternalUrls": [],
        "Subviews": [], "BackdropImageTags": [], "ImageTags": {},
        "ImageBlurHashes": {}, "ProviderIds": _provider_ids(item),
        "UserData": _user_data_dto(None, vid, None),
        "MediaSources": [],
        "PrimaryImageAspectRatio": 0.6666667, "SyncStatus": "Synced",
        "RunTimeTicks": 0, "Container": "", "IsHD": False,
        "ChildCount": ep_count or 0,
        "SeriesId": item.guid, "SeriesName": item.name or "",
        "IndexNumber": season_number,
    }
    if item.production_year:
        dto["ProductionYear"] = item.production_year
    return dto


@json_route(emby_router, "/emby/Shows/{item_id}/Seasons", "/Shows/{item_id}/Seasons")
def get_seasons(item_id: str, request: Request,
                      user: models.WebUser = Depends(get_emby_user),
                      db: Session = Depends(get_db)):
    item = _require_visible_item(db, user, item_id)
    # 合并家族认亲：分身行名下的季也属于本剧（分身名带年份后缀时
    # find_duplicate_ids 的归一化认亲对不上，真集常挂在分身的季下）。
    # 只列 canonical 季：已被合并到别的季的分身季（merged_into_id 非空）不重复列出。
    if item.item_type == "series":
        series_ids = list(set([item.id] + dedup_lib.find_duplicate_ids(
            db, item.library_id, "series",
            tmdb_id=item.tmdb_id, name=item.name,
            year=item.production_year,
        )) | _family_series_ids(db, item))
    else:
        series_ids = [item.id]
    seasons = (
        db.query(em.MediaItem)
        .filter(em.MediaItem.series_id.in_(series_ids), em.MediaItem.item_type == "season",
                em.MediaItem.merged_into_id.is_(None))
        .order_by(em.MediaItem.season_number)
        .all()
    )
    base = _base_url(request)
    _prefetch_list_data(db, user.id, seasons)
    items = [_item_dto(s, base, user.id, db) for s in seasons]
    # 桶B（缺集普查 2026-10-19）：有可见集但无可见季行的季 → 合成虚拟季条目
    # 返回，避免季列表缺口。已有季行的内容与相对排序语义不动：虚拟条目只补
    # 没有的行，按季号并入同一顺序。
    present = {s.season_number for s in seasons if s.season_number is not None}
    missing = _missing_season_numbers(db, item, series_ids, present)
    if missing:
        ep_counts = dict(
            db.query(em.MediaItem.season_number, func.count(em.MediaItem.id))
            .filter(*_episode_base_filters(item, series_ids),
                    em.MediaItem.season_number.in_(missing))
            .group_by(em.MediaItem.season_number).all())
        vdtos = [_virtual_season_dto(item, n, ep_counts.get(n, 0), db)
                 for n in missing]
        keyed = ([((s.season_number if s.season_number is not None else -1), i, d)
                  for i, (s, d) in enumerate(zip(seasons, items))]
                 + [(n, len(seasons) + k, d)
                    for k, (n, d) in enumerate(zip(missing, vdtos))])
        items = [d for _, _, d in sorted(keyed, key=lambda t: (t[0], t[1]))]
    # r50：补 Limit/StartIndex 分页（虚拟季并入最终列表后分页；此前全量一次序列化）
    start = qstart(request.query_params)
    limit = qlimit(request.query_params, default=100)
    total = len(items)
    items = items[start:start + limit]
    return {"Items": items, "TotalRecordCount": total, "StartIndex": start}


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
        # 合并家族认亲（见 _family_series_ids）：分身行即使归一化名对不上，
        # 其名下真集也应可达
        series_ids = list(set(series_ids) | _family_series_ids(db, item))
    # 基础集过滤与 _episode_base_filters 同口径（桶B 虚拟季判定复用它）。
    query = db.query(em.MediaItem).filter(*_episode_base_filters(item, series_ids))
    if season_id:
        season = db.query(em.MediaItem).filter(em.MediaItem.guid == season_id).first()
        # 家族季视为属于本剧：分身行名下的季（series_id ∈ series_ids）正常按
        # parent_id 过滤，不再掉进下面的兜底。_season_belongs_to 本身不改。
        family_season = (
            item.item_type == "series"
            and season is not None
            and season.item_type == "season"
            and season.series_id in series_ids
        )
        if season and (_season_belongs_to(season, item) or family_season):
            query = query.filter(em.MediaItem.parent_id == season.id)
        else:
            # 虚拟季（桶B：get_seasons 合成的 ``vseason-<剧 db id>-<季号>`` 条目）：
            # 真实 guid 查不到；校验它属于本剧口径且真有可见集后按季号筛集。
            # 校验失败（伪造 Id / 已无可见集）时沿用下面的旧兜底。
            parsed = _parse_virtual_season_id(season_id) if season is None else None
            vq = None
            if parsed is not None and parsed[0] in series_ids:
                cand = query.filter(em.MediaItem.season_number == parsed[1])
                if cand.limit(1).first() is not None:
                    vq = cand
            if vq is not None:
                logger.info("Episodes 用虚拟季筛选：series=%s season_number=%s",
                            item.guid, parsed[1])
                query = vq
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
    # r50：补 Limit/StartIndex 分页（去重后分页；此前千集级剧集一次全量序列化）
    start = qstart(request.query_params)
    limit = qlimit(request.query_params, default=100)
    total = len(episodes)
    episodes = episodes[start:start + limit]
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
            "TotalRecordCount": total, "StartIndex": start}


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
    # r50：补 StartIndex（此前翻页永远拿到同一批）；TotalRecordCount 取全部已开看剧集的
    # 真实下一集数（此前是当前页条数）。全量扫描按批走（每批一条窗口函数 SQL，P3 优化保留），
    # 贵重的 _item_dto 序列化只发生在当前页上。
    start = qstart(request.query_params)
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
    episodes = [e for _, e in ranked[start:start + limit]]
    base = _base_url(request)
    _prefetch_list_data(db, user.id, episodes)
    return {"Items": [_item_dto(e, base, user.id, db) for e in episodes],
            "TotalRecordCount": len(ranked), "StartIndex": start}


@emby_router.get("/emby/Users/{user_id}/FavoriteItems")
@emby_router.get("/Users/{user_id}/FavoriteItems")
def get_favorites(request: Request, user: models.WebUser = Depends(get_emby_user),
                        db: Session = Depends(get_db)):
    # r50：补 Limit/StartIndex 分页（此前全量返回、无上界；与 _query_items 的钳制纪律一致）
    start = qstart(request.query_params)
    limit = qlimit(request.query_params, default=100)
    base_q = (
        db.query(em.MediaItem)
        .join(em.UserMediaData, em.UserMediaData.item_id == em.MediaItem.id)
        .filter(em.UserMediaData.user_id == user.id, em.UserMediaData.is_favorite == True)  # noqa: E712
        .order_by(em.UserMediaData.updated_at.desc())
    )
    total = base_q.count()
    rows = base_q.offset(start).limit(limit).all()
    base = _base_url(request)
    _prefetch_list_data(db, user.id, rows)
    return {"Items": [_item_dto(i, base, user.id, db) for i in rows],
            "TotalRecordCount": total, "StartIndex": start}


@emby_router.get("/emby/Users/{user_id}/PlayedItems")
@emby_router.get("/Users/{user_id}/PlayedItems")
def get_played_items(request: Request, user: models.WebUser = Depends(get_emby_user),
                           db: Session = Depends(get_db)):
    # r50：补 Limit/StartIndex 分页（此前全量返回、无上界；与 _query_items 的钳制纪律一致）
    start = qstart(request.query_params)
    limit = qlimit(request.query_params, default=100)
    base_q = (
        db.query(em.MediaItem)
        .join(em.UserMediaData, em.UserMediaData.item_id == em.MediaItem.id)
        .filter(em.UserMediaData.user_id == user.id, em.UserMediaData.played == True)  # noqa: E712
        .order_by(em.UserMediaData.last_played_at.desc())
    )
    total = base_q.count()
    rows = base_q.offset(start).limit(limit).all()
    base = _base_url(request)
    _prefetch_list_data(db, user.id, rows)
    return {"Items": [_item_dto(i, base, user.id, db) for i in rows],
            "TotalRecordCount": total, "StartIndex": start}


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
        return _user_data_dto(umd, item.guid, _run_time_ticks(item))

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
    umd.max_position_ticks = 0  # 进度双记：与断点一起清零（批一 #4）
    db.commit()
    return _user_data_dto(umd, item.guid, _run_time_ticks(item))


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
        umd.max_position_ticks = 0  # 进度双记：与断点一起清零（批一 #4）
        db.commit()
    return _user_data_dto(umd, item.guid, _run_time_ticks(item))


# ==================== 播放 ====================

#: drive 直链 302 前的存在性校验（P0，2026-10-18）：一次 files.get 替代
#: ``_play_target`` 的逐级路径步行。独立执行器，不占用 run_db 的默认线程池。
#:
#: 背景：步行结果在 302 分支根本没用（URL 由 drive_file_id 纯拼串），
#: 但它在 run_db 线程（asyncio.to_thread，默认执行器仅 8 线程）里做
#: 1.3–4.7s 的 Drive API 调用并全程持有 DB 会话——50 并发时打满连接池
#: （25+25），新会话 30s 超时，全线排队约 90s（见
#: ``~/workspace/reports/auth-playback-stall-investigation-20261018.md``）。
#:
#: 独立执行器的理由：这次校验是纯网络 I/O，
#: ① 不能进 run_db 的 8 线程池（50 并发排队正是卡死的放大器）；
#: ② 不能在事件循环里直接调同步 httpx（会卡住单 worker 的事件循环）；
#: ③ 不需要 DB 会话（只用 drive_id + file_id 调 Drive API）。
#: 并发上限写成常量可调：环境变量 DRIVE_EXISTS_CHECK_WORKERS（默认 16）。
#: 16 线程 × 单次 ≤3s 超时：50 并发各 1 次 files.get（典型 0.1–0.5s）
#: 无排队压力；线程只跑网络等待，不碰 DB。
def _drive_exists_workers() -> int:
    """DRIVE_EXISTS_CHECK_WORKERS 解析：走 env_util 统一入口。

    非法值记 warning 后回落 16（旧实现在模块 import 期直接 int(...)，
    env="abc" 时整个 api 模块导入失败、服务起不来——见 r42 env 解析统一）。
    下限钳制 1。
    """
    return env_int("DRIVE_EXISTS_CHECK_WORKERS", 16, 1)


_DRIVE_EXISTS_CHECK_WORKERS = _drive_exists_workers()
_DRIVE_EXISTS_EXECUTOR = ThreadPoolExecutor(
    max_workers=_DRIVE_EXISTS_CHECK_WORKERS,
    thread_name_prefix="drive-exists")
#: 共享 httpx.Client（P2-1，2026-10-18）：顶层 httpx.get 每次新建 TCP+TLS
#: 握手（压测 drive_exists_check p50=1097ms 有握手放大器在内）；
#: httpx.Client 线程安全，16 执行器线程可共享连接。懒初始化——
#: 进程 fork 前不建连接。模块内「共享 HTTP client」只这一套（横切只许一套）。
_shared_drive_exists_client: "Optional[httpx.Client]" = None
_drive_exists_client_lock = threading.Lock()


def _drive_exists_client() -> httpx.Client:
    """存在性校验的共享 client（懒单例，双重检查锁）。"""
    global _shared_drive_exists_client
    if _shared_drive_exists_client is None:
        with _drive_exists_client_lock:
            if _shared_drive_exists_client is None:
                _shared_drive_exists_client = httpx.Client(
                    timeout=_DRIVE_EXISTS_TIMEOUT)
    return _shared_drive_exists_client


def _drive_exists_http_get(url, *, params, headers, timeout):
    """经共享 client 的 files.get（线程安全；测试桩点）。"""
    return _drive_exists_client().get(
        url, params=params, headers=headers, timeout=timeout)


#: files.get 存在性正缓存（P2-3，2026-10-18）：(drive_id, file_id) → 到期时间戳。
#: 播放器重试/连播时同文件被反复播放，免掉重复的 Drive API 调用（省配额）。
#: 只存"文件存在"的断言（到期时间即断言），不存任何用户数据/凭据；
#: 存在性判定不依赖 token 值——SA 轮换/换取不影响缓存有效性，无需失效。
#: 404 不缓存（文件删后恢复应可播）；401/超时/5xx 等异常不缓存
#: （401 毒化失效依赖每次真实判定；异常结果不值得信任）。
#: TTL（2026-10-21 owner 拍板 60s→600s）：环境变量 DRIVE_EXISTS_CACHE_TTL_SEC
#: （默认 600；0 或负数=关闭正缓存，每次都真实 files.get）。调参项只走 env，不进后台。
#: 启动时读一次：非法值的 warning 只打在进程起来的时候，播放热路径不再碰 env。
#: 改 env 需重启。负数由 env_int 钳到 0，即关闭。
#: 删除失效（同日追加）：正缓存拉到 600s 后，文件已在网盘删掉但库还没扫完时，
#: 播放器会一直 302 到死链。扫描确认丢失 / 管理端删条目 / 补开分享见到 404
#: 时摘掉这条缓存；EA 与扫描分进程，摘除记在 Redis 墓碑上，Redis 挂了只清本进程。
#: 另外：同一客户端在短窗口内用相同 Range 起始再请求，当成播放器在谷歌 404 后
#: 的重试，跳过缓存复验。起始字节变了（拖动 / 下一段）仍走缓存。
_DRIVE_EXISTS_CACHE_TTL_DEFAULT = 600
#: 同起始 Range 复验窗口默认秒数（0=关闭）。env DRIVE_EXISTS_RETRY_WINDOW_SEC。
_DRIVE_EXISTS_RETRY_WINDOW_DEFAULT = 15
#: 字典上限：超过即先清过期条目，仍超则整体清空（防无限增长）
_DRIVE_EXISTS_CACHE_MAX = 10000
_drive_exists_cache: dict = {}
_drive_exists_cache_lock = threading.Lock()


def _drive_exists_cache_ttl_from_env() -> int:
    """解析 DRIVE_EXISTS_CACHE_TTL_SEC。非法值 warning 一次并回落默认；负数钳成 0（关闭）。"""
    return env_int("DRIVE_EXISTS_CACHE_TTL_SEC", _DRIVE_EXISTS_CACHE_TTL_DEFAULT,
                   0, 86400)


def _drive_exists_retry_window_from_env() -> int:
    """解析 DRIVE_EXISTS_RETRY_WINDOW_SEC。非法值 warning 一次并回落默认；负数钳成 0（关闭）。"""
    return env_int("DRIVE_EXISTS_RETRY_WINDOW_SEC",
                   _DRIVE_EXISTS_RETRY_WINDOW_DEFAULT, 0, 600)


# 模块导入即进程启动：非法 env 的 warning 只发生在这里，热路径读下面两个整数。
_cache_ttl_sec = _drive_exists_cache_ttl_from_env()
_retry_window_sec = _drive_exists_retry_window_from_env()


def drive_exists_cache_ttl() -> int:
    """存在性正缓存 TTL（秒）。启动时已解析，默认 600，0=关闭。"""
    return _cache_ttl_sec


def drive_exists_retry_window() -> int:
    """同起始 Range 复验窗口（秒）。启动时已解析，默认 15，0=关闭。"""
    return _retry_window_sec


#: 跨进程墓碑：file_id 全局唯一，扫描/管理端不一定解析得出和播放侧相同的 drive_id。
_DRIVE_EXISTS_INVAL_PREFIX = "aetrix:drive_exists:inv:"
#: 跨进程重试记号（值是身份+文件+Range 起始的哈希，不落用户 id 明文）。
_DRIVE_EXISTS_RETRY_PREFIX = "aetrix:drive_exists:retry:"
_drive_exists_retry: dict = {}
_drive_exists_retry_lock = threading.Lock()
_DRIVE_EXISTS_RETRY_MAX = 10000
#: 开播连发窗口。播放器起播时经常在一两百毫秒内连打几次 bytes=0-
#: （探测 bytes=0-1 和真正的 GET，起始字节都是 0）。谷歌 404 之后的重试
#: 至少要多一次 HTTP 往返，几乎总是超过 300ms，并且仍落在复验窗口里。
#: 只对起始字节 0 生效；锚在第一次请求上，连发不把时间戳往后滑。
#: 不进 env：再放宽会把很快返回的 404 重试也吞掉。
_DRIVE_EXISTS_START_BURST_SEC = 0.3


def _drive_exists_redis():
    """EA / 扫描 / 面板分进程时共用的 Redis。未启用或导入失败 → None。"""
    try:
        from backend.database import redis_client
        return redis_client
    except Exception:  # noqa: BLE001
        return None


def _exists_inval_key(file_id: str) -> str:
    return _DRIVE_EXISTS_INVAL_PREFIX + file_id


def _exists_tombstone(file_id: str) -> bool:
    """别的进程宣布这个文件已失效。Redis 不可用 → False（只信本进程缓存）。"""
    try:
        rc = _drive_exists_redis()
        if rc is None:
            return False
        return bool(rc.exists(_exists_inval_key(file_id)))
    except Exception:  # noqa: BLE001
        return False


def _publish_exists_tombstone(file_id: str) -> None:
    try:
        rc = _drive_exists_redis()
        if rc is None:
            return
        ttl = drive_exists_cache_ttl()
        # 盖住当前正缓存可能残留的寿命；缓存关着时仍留默认窗口，避免刚删的条目被旧进程续上
        ex = ttl if ttl > 0 else _DRIVE_EXISTS_CACHE_TTL_DEFAULT
        rc.set(_exists_inval_key(file_id), "1", ex=int(ex))
    except Exception:  # noqa: BLE001
        logger.debug("Drive 存在性缓存墓碑写 Redis 失败", exc_info=True)


def _clear_exists_tombstone(file_id: str) -> None:
    try:
        rc = _drive_exists_redis()
        if rc is None:
            return
        rc.delete(_exists_inval_key(file_id))
    except Exception:  # noqa: BLE001
        logger.debug("Drive 存在性缓存墓碑清除失败", exc_info=True)


def _drop_local_exists(drive_id: str | None, file_id: str) -> None:
    """摘本进程正缓存。drive_id 为空时摘掉该 file_id 的每一条。"""
    with _drive_exists_cache_lock:
        if drive_id:
            _drive_exists_cache.pop((drive_id, file_id), None)
            return
        for key in [k for k in _drive_exists_cache if len(k) == 2 and k[1] == file_id]:
            _drive_exists_cache.pop(key, None)


def invalidate_drive_exists_cache(drive_id: str | None, file_id: str) -> None:
    """摘掉 (drive_id, file_id) 的存在性正缓存，并通知其他进程。

    Redis 不可用时只清本进程，不抛异常（播放/扫描热路径）。
    file_id 为空直接返回。drive_id 为空则按 file_id 摘掉本进程全部匹配项，
    墓碑仍按 file_id 发（Drive file id 全局唯一）。
    """
    file_id = (file_id or "").strip()
    if not file_id:
        return
    drive_id = (drive_id or "").strip() or None
    try:
        _drop_local_exists(drive_id, file_id)
    except Exception:  # noqa: BLE001
        logger.debug("Drive 存在性缓存本进程摘除失败", exc_info=True)
    _publish_exists_tombstone(file_id)


def invalidate_drive_exists_for_item(db, item) -> None:
    """条目已确认不在（扫描丢失 / 管理端删除 / 分享 404）。永不抛。"""
    try:
        file_id = (getattr(item, "drive_file_id", None) or "").strip()
        if not file_id:
            return
        drive_id = None
        try:
            drive_id = _drive_mount_id_for_item(db, item)
        except Exception:  # noqa: BLE001
            drive_id = None
        invalidate_drive_exists_cache(drive_id, file_id)
    except Exception:  # noqa: BLE001
        logger.debug("按条目失效 Drive 存在性缓存失败", exc_info=True)


def _drive_exists_cache_hit(drive_id: str, file_id: str) -> bool:
    """正缓存命中（TTL 内已判定存在，且没有跨进程墓碑）→ True。TTL=0 恒 False。"""
    if drive_exists_cache_ttl() <= 0:
        return False
    now = time.monotonic()
    key = (drive_id, file_id)
    with _drive_exists_cache_lock:
        exp = _drive_exists_cache.get(key)
        if exp is None or exp <= now:
            if exp is not None:
                del _drive_exists_cache[key]
            return False
    # 锁外问 Redis：别的容器已经宣布这个文件没了
    if _exists_tombstone(file_id):
        try:
            _drop_local_exists(drive_id, file_id)
        except Exception:  # noqa: BLE001
            pass
        return False
    return True


def _drive_exists_cache_put(drive_id: str, file_id: str) -> None:
    """files.get 200 后写入正缓存（只在成功路径调用；TTL=0 不写）。

    同时摘掉墓碑：文件又被确认存在，别的进程的「已删除」宣告作废。
    """
    ttl = drive_exists_cache_ttl()
    if ttl <= 0:
        _clear_exists_tombstone(file_id)
        return
    with _drive_exists_cache_lock:
        if len(_drive_exists_cache) >= _DRIVE_EXISTS_CACHE_MAX:
            # 防无限增长：先清过期条目；TTL 拉长到 600s 后仍可能全是活条目，
            # 这时整体清空（最坏只是多打几次 files.get，不影响正确性）
            now = time.monotonic()
            for k in [k for k, e in _drive_exists_cache.items() if e <= now]:
                del _drive_exists_cache[k]
            if len(_drive_exists_cache) >= _DRIVE_EXISTS_CACHE_MAX:
                _drive_exists_cache.clear()
        _drive_exists_cache[(drive_id, file_id)] = time.monotonic() + ttl
    _clear_exists_tombstone(file_id)


def _range_start_token(header: str | None):
    """Range 起始位置。无头与 bytes=0- 不是同一次请求；只比较起始，不比较结束。

    - 无 / 空 → ("absent",)
    - bytes=START-... → ("start", START)
    - bytes=-SUFFIX → ("suffix", SUFFIX)
    - 认不出 → ("raw", 原文)（两次一模一样的怪请求仍能对上）
    """
    if header is None:
        return ("absent",)
    text = str(header).strip()
    if not text:
        return ("absent",)
    first = text.split(",", 1)[0].strip()
    if not first.lower().startswith("bytes="):
        return ("raw", text)
    spec = first.split("=", 1)[1].strip()
    if spec.startswith("-"):
        num = spec[1:].strip()
        if num.isdigit():
            return ("suffix", int(num))
        return ("raw", text)
    start = spec.split("-", 1)[0].strip()
    if start.isdigit():
        return ("start", int(start))
    return ("raw", text)


def _stream_client_identity(request, user) -> str:
    """重试身份：设备优先，其次播放会话，最后用户。

    流地址本身不带 PlaySessionId；Emby 客户端通常仍带 DeviceId 头或查询参数。
    都没有时用用户 id，避免两个用户抢同一文件时互相当成重试。
    """
    headers = getattr(request, "headers", None)
    params = getattr(request, "query_params", None)
    auth = parse_emby_authorization(headers.get("X-Emby-Authorization") if headers else None)
    device = (auth.get("DeviceId") or "")
    if headers is not None and not device:
        device = (headers.get("X-Emby-Device-Id") or headers.get("X-Device-Id") or "")
    if params is not None and not device:
        device = params.get("DeviceId") or ""
    device = str(device or "").strip()
    if device:
        return "dev:" + device[:100]
    session = ""
    if params is not None:
        session = str(params.get("PlaySessionId") or "").strip()
    if session:
        return "ses:" + session[:64]
    uid = getattr(user, "id", None)
    if uid is not None:
        return f"user:{uid}"
    return "anon"


def _retry_redis_key(identity: str, file_id: str, token) -> str:
    raw = f"{identity}\x1f{file_id}\x1f{token!r}".encode()
    return _DRIVE_EXISTS_RETRY_PREFIX + hashlib.sha256(raw).hexdigest()[:32]


def _parse_retry_ts(raw):
    """Redis 里存的是第一次见到该请求的 unix 时间。旧值 ``"1"`` / 坏值 → None。"""
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _opening_burst(token, age: float) -> bool:
    """起始字节 0、且距第一次请求还不到开播连发窗口。"""
    return (
        token == ("start", 0)
        and 0 <= age < _DRIVE_EXISTS_START_BURST_SEC
    )


def _retry_seen_redis(identity: str, file_id: str, token, window: int) -> bool:
    """别的进程在窗口内见过同一请求 → True。Redis 不可用 → False。

    值是第一次见到的 unix 时间。bytes=0- 且年龄还在开播连发窗口内 → False，
    也不刷新 TTL（锚留在第一次）。读不到时间戳（升级前写下的 ``"1"``）宁可
    当成重试，多打一次 files.get。
    """
    try:
        rc = _drive_exists_redis()
        if rc is None:
            return False
        key = _retry_redis_key(identity, file_id, token)
        now = time.time()
        created = rc.set(key, f"{now:.6f}", nx=True, ex=int(window))
        if created:
            return False
        raw = rc.get(key)
        if raw is None:
            return False
        ts = _parse_retry_ts(raw)
        if ts is not None and _opening_burst(token, now - ts):
            return False
        try:
            rc.expire(key, int(window))
        except Exception:  # noqa: BLE001
            pass
        return True
    except Exception:  # noqa: BLE001
        return False


def drive_exists_rapid_retry(request, user, file_id: str) -> bool:
    """同一客户端、同一文件、相同 Range 起始，在窗口内又来了一次 → True。

    第一次只登记，不当重试。起始字节不同（拖动、下一段）→ False，缓存继续有效。
    开播时极短间隔内重复的 bytes=0- 也不当重试（见 ``_DRIVE_EXISTS_START_BURST_SEC``），
    并且不把登记时间往后滑，这样窗口内稍晚的真正重试仍能对上第一次。
    窗口 0 或任何异常 → False，播放照旧走缓存。Redis 挂了只看本进程。
    """
    try:
        file_id = (file_id or "").strip()
        window = drive_exists_retry_window()
        if window <= 0 or not file_id:
            return False
        headers = getattr(request, "headers", None)
        token = _range_start_token(headers.get("Range") if headers else None)
        identity = _stream_client_identity(request, user)
        local_key = (identity, file_id, token)
        now = time.monotonic()
        local_hit = False
        with _drive_exists_retry_lock:
            prev = _drive_exists_retry.get(local_key)
            if prev is not None and _opening_burst(token, now - prev):
                # 连发：保持第一次的时间戳，本进程也不算重试。
                pass
            else:
                _drive_exists_retry[local_key] = now
                if len(_drive_exists_retry) > _DRIVE_EXISTS_RETRY_MAX:
                    cutoff = now - window
                    for key, ts in [kv for kv in _drive_exists_retry.items() if kv[1] < cutoff]:
                        _drive_exists_retry.pop(key, None)
                    if len(_drive_exists_retry) > _DRIVE_EXISTS_RETRY_MAX:
                        _drive_exists_retry.clear()
                        _drive_exists_retry[local_key] = now
                if prev is not None and 0 <= (now - prev) <= window:
                    local_hit = True
        return local_hit or _retry_seen_redis(identity, file_id, token, window)
    except Exception:  # noqa: BLE001
        logger.debug("Drive 存在性缓存重试判定失败", exc_info=True)
        return False


#: 单次 files.get 超时（秒）：只做存在性判定，不许引入新的长等待。
_DRIVE_EXISTS_TIMEOUT = 3.0
_DRIVE_FILE_GET_URL = "https://www.googleapis.com/drive/v3/files/{file_id}"


class _DriveFileGone(Exception):
    """files.get 404：文件在网盘上已不存在 → 上层转 HTTP 404"""


class _DriveFileUnavailable(Exception):
    """files.get 非 404 失败（超时/5xx/凭据问题）→ 上层转 HTTP 503"""


def _enforce_stream_limit(db: Session, request, user, item: em.MediaItem,
                          item_id: str) -> None:
    """按「挂载+路径前缀」限同时播放路数（FMBY 批一 #3，stream_limits）。

    无策略命中时零开销放行；超限抛 429 + 人话原因，不许静默失败。
    计数在 Redis 里跨进程一致，租约式占位（细节见 stream_limits 模块）。
    """
    from backend.emby_server import stream_limits

    reason = stream_limits.acquire(
        db, _stream_client_identity(request, user), str(item_id or ""),
        getattr(item, "file_path", "") or "")
    if reason is not None:
        raise HTTPException(status_code=429, detail=reason)


def _drive_mount_id_for_item(db: Session, item: em.MediaItem) -> Optional[str]:
    """drive 直链快路径用的 drive_id；其他情况返回 None（调用方回落原路径）。

    只接受「file_path 是 mount:// 前缀、挂载存在且启用、类型为 drive」
    的条目，返回该挂载的 drive_id（共享盘 id 或 ``myDrive:<remote>``）。
    同步、只做一次主键查询（毫秒级），调用方经 run_db 走线程池；
    后续的网络校验在独立执行器里跑，不再碰 db。
    """
    parsed = mount_lib.parse_mount_path(getattr(item, "file_path", "") or "")
    if not parsed:
        return None
    mount_id, rel = parsed
    if mount_lib._is_strm_name(rel):
        # .strm 走内容解析（resolve_final 读文件内容），语义与步行不同，
        # 不进快路径，回落原 _play_target。
        return None
    mount = db.query(em.StorageMount).filter(
        em.StorageMount.id == mount_id).first()
    if mount is None or not getattr(mount, "is_enabled", False):
        return None
    if (getattr(mount, "mount_type", "") or "") != mount_lib.MOUNT_DRIVE:
        return None
    # 复用 DriveMount 自身的 drive_id 解析（含 config.drive_id 覆盖与
    # myDrive 个人盘），不重复造逻辑；__init__ 只做字符串解析，无 I/O。
    from backend.emby_server.mount_drive import DriveMount
    provider = mount_lib.build_provider(mount, db, getattr(item, "library", None))
    if not isinstance(provider, DriveMount):
        return None
    return provider.drive_id or None


def _mount_can_generate_play_target(db: Session, item: em.MediaItem) -> Optional[bool]:
    """条目挂载的换链能力（清单第 2 项能力门控，变体清单用）。

    只接受 file_path 是 mount:// 前缀的条目：挂载行按类型派生能力
    （mount_sources.capabilities_for_mount_type，与各 provider 的
    capabilities() 声明逐字一致）；挂载不存在或条目非 mount:// 时回
    None（调用方保持旧行为，不因查不到挂载就把变体扣掉）。
    同步、只做一次主键查询，调用方经 run_db 走线程池。
    """
    from backend.emby_server import mount_sources

    parsed = mount_lib.parse_mount_path(getattr(item, "file_path", "") or "")
    if parsed is None:
        return None
    mount = db.query(em.StorageMount).filter(
        em.StorageMount.id == parsed[0]).first()
    if mount is None:
        return None
    return mount_sources.capabilities_for_mount_type(
        mount.mount_type).get("can_generate_play_target")


def _user115_direct_available(db: Session, item: em.MediaItem,
                              user) -> bool:
    """BYOS P1 绑定直链可用性（变体清单与取流校验共用，同口径）。

    同时满足才 True：条目是 mount:// 路径、挂载类型为 115、该用户
    有 active 的 pan115 绑定。同步、两次主键级查询，调用方经 run_db
    走线程池。清单阶段不真解析直链；无绑定用户恒 False，行为与
    今天逐字一致。
    """
    from backend.emby_server import user_bindings

    parsed = mount_lib.parse_mount_path(getattr(item, "file_path", "") or "")
    if parsed is None:
        return False
    mount = db.query(em.StorageMount).filter(
        em.StorageMount.id == parsed[0]).first()
    if mount is None or mount.mount_type != mount_lib.MOUNT_PAN115:
        return False
    return user_bindings.get_active_binding(
        db, getattr(user, "id", None), "pan115") is not None


def _drive_file_exists_sync(drive_id: str, file_id: str,
                            force_recheck: bool = False) -> None:
    """同步：一次 files.get 判定存在性。只在 _DRIVE_EXISTS_EXECUTOR 里调用。

    404 抛 _DriveFileGone；其他失败抛 _DriveFileUnavailable。永不碰 db。
    凭据走 ``drive_changes.get_token_for_drive``（全项目唯一入口，含
    个人盘 OAuth 与共享盘 SA 轮换）；401 时按 token 值失效内外两层缓存。
    正缓存（TTL 见 drive_exists_cache_ttl，默认 600s）命中时直接返回（A-P2-3）；404 不缓存。
    ``force_recheck``：播放器用相同 Range 起始在短窗口内又来了一次，跳过正缓存
    重新 files.get。404 时摘掉缓存（含跨进程墓碑）；200 时刷新缓存。
    """
    from backend.emby_server import playback_risk
    if not force_recheck and _drive_exists_cache_hit(drive_id, file_id):
        playback_risk.bump("exists_cache_hit")
        return
    playback_risk.bump("exists_cache_miss")
    try:
        _drive_file_exists_remote(drive_id, file_id)
    except _DriveFileGone:
        # 跳过缓存复验得到 404 时，旧的正缓存条目还在，必须摘掉，否则下一次非重试仍 302
        invalidate_drive_exists_cache(drive_id, file_id)
        playback_risk.bump("exists_404")
        raise
    except _DriveFileUnavailable:
        playback_risk.bump("exists_503")
        raise


def _drive_file_exists_remote(drive_id: str, file_id: str) -> None:
    """真实 files.get（缓存未命中时）：成功写正缓存，失败抛异常。"""
    from backend.emby_server import drive_auth, drive_changes
    from backend.emby_server import playback_risk
    try:
        token = drive_changes.get_token_for_drive(drive_id)
    except drive_changes.DriveAuthError as exc:
        # P3-2（2026-10-18）：503 detail 脱敏——不把 DriveAuthError 原文
        # （可能含 rclone remote 名/刷新异常堆栈等实现细节）插进用户可见的
        # 503 detail；只留粗分类。异常链（from exc）保留，服务端日志仍可查。
        raise _DriveFileUnavailable("Drive 凭据失效") from exc
    if not token:
        raise _DriveFileUnavailable("无可用 Drive 凭据")
    url = _DRIVE_FILE_GET_URL.format(file_id=file_id)
    try:
        resp = _drive_exists_http_get(
            url,
            params={"supportsAllDrives": "true", "fields": "id"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=_DRIVE_EXISTS_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 — 超时/网络异常一律转 503
        raise _DriveFileUnavailable(f"files.get 调用失败：{exc}") from exc
    playback_risk.note_drive_status(resp.status_code)
    if resp.status_code == 404:
        raise _DriveFileGone(f"Drive 文件已不存在：{file_id}")
    if resp.status_code == 401:
        # 401 毒化修复：按 token 值失效内外两层缓存，下次请求重新换取
        drive_auth.note_drive_unauthorized(url, f"Bearer {token}")
        raise _DriveFileUnavailable("Drive 凭据失效（401）")
    if resp.status_code != 200:
        raise _DriveFileUnavailable(f"files.get 返回 {resp.status_code}")
    # 200：文件存在 → 写入正缓存（404/401/异常路径不缓存）
    _drive_exists_cache_put(drive_id, file_id)


async def _ensure_drive_file_playable(db: Session, item: em.MediaItem,
                                      file_id: str, force_recheck: bool = False) -> None:
    """drive 直链 302 前的便宜存在性校验（P0，2026-10-18）。

    先经 run_db 做一次毫秒级挂载解析（拿 drive_id；解析失败返回 None
    时调用方回落原路径，本函数不处理）；再把一次 files.get 放进独立
    执行器，不占 run_db 线程、不持有 DB 会话做网络等待。

    语义映射：
    - 404 → HTTP 404（文件已删/不可见；与旧步行 MountError→404 对齐）
    - 401 → 失效坏 token 后 HTTP 503（与旧 MountAuthError→503 对齐）
    - 403/429/5xx/超时/网络异常/无凭据 → HTTP 503。
      注意：旧步行把「列目录失败」（超时/5xx）也归成 MountError→404；
      新语义改成 503 是有意修正——瞬时故障不应读成「文件没了」
      （客户端拿到 404 可能丢弃条目，503 才是可重试信号）。
    - 文件改名/移动：旧步行按路径找、路径没了 → 404；新逻辑以
      drive_file_id 为锚（files.get 直接按 id 查），照样 302 可播——
      这是既定方向（网盘直挂：文件身份以 drive_file_id 为锚）带来的
      改进，不是 bug，后人别把它"修"回去。
    """
    from backend.emby_server.async_db import run_db
    drive_id = await run_db(_drive_mount_id_for_item, db, item)
    if not drive_id:
        # 挂载解析失败（已删/停用/非 drive 挂载/.strm）：回落原路径，
        # 由 _play_target 按旧语义处理（本函数不抛 404，避免误伤）。
        # 调用方约定：返回 True 才走快路径 302；返回 None 则回落原路径。
        return None
    loop = asyncio.get_running_loop()
    try:
        await loop.run_in_executor(
            _DRIVE_EXISTS_EXECUTOR, _drive_file_exists_sync,
            drive_id, file_id, bool(force_recheck))
    except _DriveFileGone as exc:
        raise HTTPException(status_code=404, detail=f"媒体来源不可用：{exc}")
    except _DriveFileUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"媒体来源暂不可用：{exc}")
    return True


def _note_play_start_safe(item: em.MediaItem, **kwargs) -> None:
    """起播事件打点的安全调用口（FMBY 批一 #2）。

    从条目推导源维度后调 play_events.note_play_start。打点模块本身永不抛，
    这里再套一层保险：钩子处任何意外都不许影响起播（测试钉住这一条）。
    """
    try:
        from backend.emby_server import play_events

        file_path = getattr(item, "file_path", "") or ""
        if mount_lib.is_mount_path(file_path):
            parsed = mount_lib.parse_mount_path(file_path)
            provider = "mount"
            mount_id = parsed[0] if parsed else None
        elif (getattr(item, "drive_file_id", "") or "").strip():
            provider, mount_id = "drive", None
        else:
            provider, mount_id = "local", None
        play_events.note_play_start(
            provider=provider, mount_id=mount_id,
            item_id=getattr(item, "id", None), **kwargs)
    except Exception:  # noqa: BLE001 — 打点不许打断起播，见 play_events 铁律
        pass


def _play_target(db: Session, item: em.MediaItem, *, variant=None):
    """把条目的 ``file_path`` 解析成播放目标（本机文件 或 挂载直链）

    ``variant``（FMBY 批二 #11 PR-2，关键字专属、可选）：video_stream
    按变体取流时传进来，调度事件按所选变体记（mount_relay / local_file
    不再一律记 resolve）；不传时行为与升级前逐字一致。签名仍可按
    ``(db, item)`` 两参调用（多处桩替身共用），解析事件仍不带 user_id。
    

    挂载来源（115 / WebDAV / AList / STRM）解析失败时给出明确状态码，不抛 500：

    - 挂载被删除 / 停用 → 404（该条目需要重新扫描或重新绑定挂载）
    - 凭据失效（Cookie / 令牌 / 密码）→ 503（可修，修好后重试即可）

    起播打点（批一 #2）：解析段的成败/耗时/错误类型在此收口记一条事件
    （method=resolve）。**不给本函数加 user 参数**：它被多处桩替身与下载
    端点共用，签名冻结；解析事件因此不带 user_id（快路径事件在
    video_stream 里记，带 user_id）。
    """
    _evt_t0 = time.perf_counter()
    try:
        # 身份锚双读（清单第 8 项）：先按 (source_id, provider_file_id) 定位挂载，
        # 锚失败时回落现行 mount:// 解析——回落是第二路，行为与今天逐字一致。
        # 锚解析本身异常（如桩替身缺字段）不计数、不阻断，直接走路径解析，
        # 真正的 DB 故障由路径解析原样抛出。
        _anchor_mount = None
        try:
            _anchor_mount, _anchor_reason = item_anchor.resolve_anchor(db, item)
        except Exception:  # noqa: BLE001 — 锚只是第一路，路径解析才是权威
            _anchor_mount, _anchor_reason = None, None
        if _anchor_mount is not None:
            item_anchor.note_anchor_used()
            target = mount_lib.resolve_play_target(
                item.file_path, db, item.library, anchor_mount=_anchor_mount)
        else:
            if _anchor_reason is not None and mount_lib.is_mount_path(
                    getattr(item, "file_path", "") or ""):
                item_anchor.note_fallback(_anchor_reason)
            target = mount_lib.resolve_play_target(item.file_path, db, item.library)
    except mount_lib.MountAuthError as exc:
        _note_play_start_safe(
            item, method="resolve", variant=variant, result="error",
            error_kind="auth",
            resolve_ms=int((time.perf_counter() - _evt_t0) * 1000))
        raise HTTPException(status_code=503, detail=f"媒体来源凭据失效：{exc}")
    except mount_lib.MountError as exc:
        _note_play_start_safe(
            item, method="resolve", variant=variant, result="error",
            error_kind="mount_unavailable",
            resolve_ms=int((time.perf_counter() - _evt_t0) * 1000))
        raise HTTPException(status_code=404, detail=f"媒体来源不可用：{exc}")
    except Exception:
        _note_play_start_safe(
            item, method="resolve", variant=variant, result="error",
            error_kind="resolve_error",
            resolve_ms=int((time.perf_counter() - _evt_t0) * 1000))
        raise
    # 按变体取流的成功事件按所选变体记（mount_relay / local_file），
    # 无变体参数时仍记 resolve；错误事件一律记 resolve（见上）。
    _ok_method = variant if variant in (
        play_variants.MOUNT_RELAY, play_variants.LOCAL_FILE) else "resolve"
    _note_play_start_safe(
        item, method=_ok_method, variant=variant, result="ok",
        resolve_ms=int((time.perf_counter() - _evt_t0) * 1000))
    return target


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
    # 按需媒体信息探测：远程文件缺 codec 时入队，后台限流探测，不阻塞播放（v2.51.0）
    try:
        from backend.emby_server import probe_worker
        await run_db(probe_worker.maybe_enqueue, db, item)
    except Exception as exc:  # noqa: BLE001
        # 入队失败不影响本次播放；记日志——探测停摆会导致码率/时长长期缺失
        logger.warning("媒体探测入队失败 item=%s: %s", getattr(item, "id", "?"), exc)
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
    # 转码删除（2026-10-24）：服务端不再转码，PlaybackInfo 不再下发
    # TranscodingUrl，SupportsTranscoding 恒 False。

    # PlaybackInfo 短期缓存（5 分钟）：同一部片子短时间内重复请求直接走 Redis，
    # 省掉 _media_source 的 DB 查询。PlaySessionId 和 api_key 每次重新生成，不进缓存。
    # 缓存 key 包含 user_id（权限不同）+ 设备 profile 指纹 + 码率上限。
    cache_ttl = env_int("PLAYBACKINFO_CACHE_TTL", 300)
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
        media_source = await run_db(_media_source, item, base, "", db, user=user)
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
    # 与真实 Emby 对齐：直放门限用 Bitrate 实际下发值判定（探测值优先、推导兜底），
    # 缺码率条目不再被误判为 SupportsDirectStream=false。
    _eff_bitrate = _effective_bitrate(item)
    direct = _eff_bitrate and _eff_bitrate <= max_bitrate
    # api_key：只回显 Emby 客户端 token（第三方客户端兼容）。
    # 安全修复 H2：门户 / 管理员 JWT 不再进 URL——网页端只拿短期播放签名（uid/exp/sign）。
    api_key = _api_key_for(db, request)
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
    # 统一播放 URL 改写（横切能力只许一套）：流节点 > 加速域名 > 原样。
    # 签名查询串原样保留，流节点用同一 SECRET_KEY 验签。
    # 最简播放（2026-10-16 用户拍板）：Drive 条目且直链开关开启时，跳过
    # 流节点/加速域名改写——DirectStreamUrl 保持本机 /stream，
    # 由 video_stream 直接 302 到谷歌直链，字节不经任何中间层。
    # 转码删除后不再构建 TranscodingUrl，改写链只改写播放 URL。
    from backend.emby_server import playback_strategy as _ps2
    _dd_fid = (getattr(item, "drive_file_id", "") or "").strip()
    if _dd_fid and _ps2.drive_direct_play_enabled(db):
        pass  # 直链播放：跳过改写即可（本分支 _rewrite_src 已无其他消费方）
    else:
        from backend.emby_server import stream_accel
        stream_url, _ = stream_accel.rewrite_playback_urls_unified(
            db, stream_url, base)
    media_source.update({
        "SupportsDirectPlay": True,
        "SupportsDirectStream": bool(direct),
        "SupportsTranscoding": False,
        # r50：补 DirectPlayUrl（此前恒缺，SupportsDirectPlay=True 却无对应地址）。
        # 本架构直放与直接串流是同一条 302（video_stream 302 到网盘直链），故同值。
        "DirectPlayUrl": stream_url,
        "DirectStreamUrl": stream_url,
    })
    media_source.pop("TranscodingUrl", None)

    sources = [media_source]
    _variants = []
    if play_variants.variants_enabled():
        # 变体清单下发（批二 #11）：可用性只看开关 + 条目静态属性 +
        # Drive 挂载可定位（一次主键查询），不在清单阶段真解析直链
        #（与 #1205 解析缓存正交）。第一条恒为现行主线变体，只取第一
        # 条的老客户端无感；可用变体只有一条时保持原单源下发不变样。
        _drive_mount_ok = False
        if _dd_fid:
            _drive_mount_ok = (
                await run_db(_drive_mount_id_for_item, db, item)) is not None
        # 清单第 2 项能力门控：挂载换链能力进变体清单判定。
        _mount_can_generate = await run_db(
            _mount_can_generate_play_target, db, item)
        # BYOS P1：绑定直链信号（该用户有 active 的 pan115 绑定且
        # 条目挂载为 115 时，清单追加 byos115_direct）。
        _user115_ok = await run_db(
            _user115_direct_available, db, item, user)
        _variants = await run_db(
            play_variants.list_variants, db, item,
            drive_mount_locatable=_drive_mount_ok,
            mount_can_generate_play_target=_mount_can_generate,
            user115_direct_available=_user115_ok)
        if len(_variants) > 1:
            sources = play_variants.build_media_sources(
                media_source, _variants, guid=item.guid,
                stream_url=stream_url,
                direct_stream_ok=bool(direct))

    # 阶段 3 一等会话（默认关）：开时 PlaySessionId 不再是每次现生成的随机
    # 票据，而是服务端在 PlaybackInfo 时预建的会话 id——上报侧 session_key
    # 与 PlaybackInfo 侧是同一身份，管理员在“已起播未上报”窗口内已有目标
    # 可打；预建失败绝不能打断 PlaybackInfo，回落旧随机票据。关时逐字旧行为。
    _play_session_id = secrets.token_hex(8)
    if play_session.first_class_enabled():
        try:
            _auth = parse_emby_authorization(
                request.headers.get("X-Emby-Authorization"))
            _sess = await run_db(
                play_session.create_session, db, user, item,
                variant=(_variants[0].name if _variants else None),
                device_id=_auth.get("DeviceId") or None)
            _play_session_id = _sess.server_session_id
        except Exception:  # noqa: BLE001 — 预建失败只回落，不阻断播放
            _play_session_id = secrets.token_hex(8)
    _pt.done()
    return _strip_nulls({
        "MediaSources": sources,
        # 每次播放会话一个独立票据，客户端据此上报进度
        "PlaySessionId": _play_session_id,
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
    if getattr(response, "status_code", None) == 302:
        from backend.emby_server import playback_risk
        playback_risk.bump("redirect_302")
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


def _available_variant_names(db: Session, item: em.MediaItem,
                             user) -> list:
    """video_stream 按变体取流用的可用变体名（与 playback_info 同口径）。

    Drive 挂载可定位走 _drive_mount_id_for_item 一次主键查询；
    清单阶段不真解析直链。只在客户端带了变体时才调用——无变体
    老路径不增加这次查询成本。
    """
    locatable = False
    if (getattr(item, "drive_file_id", "") or "").strip():
        locatable = _drive_mount_id_for_item(db, item) is not None
    return [v.name for v in play_variants.list_variants(
        db, item, drive_mount_locatable=locatable,
        user115_direct_available=_user115_direct_available(db, item, user))]


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
    # 防盗链：白名单为空时直接放行（默认关闭，兼容第三方客户端）
    await run_db(play_sign.check_referer, request, db)
    _pt.mark("auth_referer")
    # 按挂载+路径前缀限同时播放路数（FMBY 批一 #3）：四分支共用此一处闸门，
    # 起播占租约、超限 429；无策略时零行为变化。
    await run_db(_enforce_stream_limit, db, request, user, item, item_id)
    _pt.mark("auth_stream_limit")
    # 按变体取流（FMBY 批二 #11 PR-2）：客户端带了变体（variant 参数 /
    # MediaSourceId 后缀）时，只执行该变体的分支。变体未知报 404、
    # 已知但该条目当下不可用报 409，两种情况都不许静默改走默认条；
    # 没带变体时 _requested_variant 为 None，下面各分支条件与升级前
    # 逐字一致（老客户端行为不变）。租约已在上面按条目占过一次，
    # 租约 id 不含变体（stream_limits._lease_id），同条目两变体只占一额。
    _requested_variant = None
    # #1220 CI 修：变体参数是可选特性，读不到查询串时按“没带变体”
    # 走老路径，不许让整个流端点崩掉。真实 FastAPI 请求一定有
    # query_params（本模块多处同款直读）；这里只兜底鸭子类型调用方
    # （如测试桩）缺该属性的情况，真实请求行为逐字不变。
    _variant_qs = getattr(request, "query_params", None) or {}
    _variant_name, _variant_mismatch = play_variants.parse_requested_variant(
        _variant_qs)
    if _variant_mismatch:
        raise HTTPException(
            status_code=404, detail="播放变体不存在：variant 与 MediaSourceId 不一致")
    if _variant_name is not None:
        _available_names = await run_db(
            _available_variant_names, db, item, user)
        _requested_variant, _variant_error = (
            play_variants.resolve_requested_variant(
                _variant_qs, _available_names))
        if _variant_error == 404:
            raise HTTPException(status_code=404, detail="播放变体不存在")
        if _variant_error == 409:
            raise HTTPException(
                status_code=409, detail="播放变体当前不可用，请选择其他变体")
    # 转码删除（2026-10-24）：不再有 transcode_hls 变体分支；客户端
    # 若仍带 transcode_hls 变体，上面 resolve_requested_variant 已按
    # 「不在四变体枚举里」报 404，不会落到这里。
    media_type = f"video/{item.container}" if item.container else "video/mp4"
    # 最简播放（2026-10-16 用户拍板学 raisemby）：Drive 条目（有 drive_file_id）
    # 且 drive_direct_play_enabled 开关开启时，直接 302 到谷歌 usercontent
    # 直链，播放器自拉；服务器退出字节通道。
    # P0（2026-10-18）：存在性校验通过则直接 302，跳过 _play_target 的
    # Drive 路径步行——步行结果在 302 分支根本没用（URL 由 drive_file_id
    # 纯拼串），却在 run_db 线程里做 1.3–4.7s 的 Drive API 调用并全程持有
    # DB 会话，50 并发时打满连接池（25+25）致全线排队约 90s（见本文件
    # _DRIVE_EXISTS_EXECUTOR 注释与 auth-playback 专项调查报告）。
    from backend.emby_server import playback_strategy as _strategy
    _fid = (getattr(item, "drive_file_id", "") or "").strip()
    _drive_switch_on = bool(_fid) and await run_db(
        _strategy.drive_direct_play_enabled, db)
    if (_drive_switch_on and await run_db(_strategy.drive_relay_enabled, db)
            and (_requested_variant is None
                 or _requested_variant == play_variants.DRIVE_RELAY)):
        _evt_t0 = time.perf_counter()
        _relay_drive_id = await run_db(_drive_mount_id_for_item, db, item)
        if _requested_variant == play_variants.DRIVE_RELAY and not _relay_drive_id:
            # 校验时可定位、执行时已不可定位（挂载被停用等竞态）：
            # 报 409，不许回落到直链或 _play_target（那是静默改走）。
            raise HTTPException(
                status_code=409, detail="播放变体当前不可用：drive_relay")
        if _relay_drive_id:
            # Drive SA 中继（2026-10-23 额度应急）：字节过服务器转发，
            # file_id 不出门、不走公开下载池；存在性由上游取流本身
            # 回答（文件没了上游 404 透传），不再单跑存在性校验。
            # 观测口径与直链快路径一致（_observe_traffic + play-timing）。
            _pt.mark("relay")
            _relay_resp = await serve_drive_relay_async(
                request, _relay_drive_id, _fid,
                media_type=media_type, pt=_pt)
            # 起播打点（批一 #2）：中继首响应回来即记一条，成败看上游状态码
            _note_play_start_safe(
                item, user_id=getattr(user, "id", None), method="relay",
                variant=play_variants.DRIVE_RELAY,
                result="error" if _relay_resp.status_code >= 400 else "ok",
                error_kind=(f"upstream_{_relay_resp.status_code}"
                            if _relay_resp.status_code >= 400 else None),
                resolve_ms=int((time.perf_counter() - _evt_t0) * 1000))
            return _observe_traffic(_relay_resp)
    _fast302 = False
    _evt_t0 = time.perf_counter()
    if _drive_switch_on:
        # 相同起始 Range 的短时重试：播放器多半是谷歌 404 之后又来了一次，
        # 正缓存不能再把死链 302 出去。拖动 / 下一段起始字节不同，不进这里。
        _force = drive_exists_rapid_retry(request, user, _fid)
        _fast302 = await _ensure_drive_file_playable(
            db, item, _fid, force_recheck=_force)
    if _requested_variant == play_variants.DRIVE_DIRECT and not _fast302:
        # 校验时可定位、执行时已不可定位（挂载被停用等竞态）：
        # 报 409，不许回落到 _play_target 的通用分支（那是静默改走）。
        # 文件没了/凭据失效时 _ensure 已直接抛 404/503，不走这里。
        raise HTTPException(
            status_code=409, detail="播放变体当前不可用：drive_direct")
    if _fast302:
        _pt.mark("drive_exists_check")
        _pt.done()
        # 起播打点（批一 #2）：存在性校验通过、302 直链下发记一条
        _note_play_start_safe(
            item, user_id=getattr(user, "id", None), method="direct_302",
            variant=play_variants.DRIVE_DIRECT,
            result="ok",
            resolve_ms=int((time.perf_counter() - _evt_t0) * 1000))
        from backend.emby_server.drive_auth import drive_usercontent_download_url
        return _observe_traffic(
            RedirectResponse(drive_usercontent_download_url(_fid), status_code=302,
                             headers=_strategy.DIRECT_REDIRECT_HEADERS))
    if _requested_variant == play_variants.BYOS115_DIRECT:
        # BYOS P1：绑定用户自己的 115 直链——按绑定凭据现签现发 302，
        # 播放器自拉，字节不过服务器。签名验签 / 来路检查 / 限流闸门
        # 已在上面走过；起播打点与 302 观测与直链分支同口径。
        from backend.emby_server import user_bindings as _ub
        _byos_t0 = time.perf_counter()
        try:
            _byos_url = await run_db(
                _ub.resolve_user115_direct_url, db,
                getattr(user, "id", None), item)
        except _ub.BindingError as exc:
            # 凭据失效三档：resolver 已落失败态，这里只报 409 + 文案，
            # 不静默回落共享中继；一过性/无绑定按通用句报 409。
            _note_play_start_safe(
                item, user_id=getattr(user, "id", None),
                method="byos115_direct",
                variant=play_variants.BYOS115_DIRECT,
                result="error", error_kind=exc.kind,
                resolve_ms=int((time.perf_counter() - _byos_t0) * 1000))
            _detail = exc.message if exc.kind in (
                "auth_error", "expired", "throttled") else \
                "播放变体当前不可用：byos115_direct"
            raise HTTPException(status_code=409, detail=_detail) from exc
        _pt.mark("byos115_direct")
        _pt.done()
        _note_play_start_safe(
            item, user_id=getattr(user, "id", None),
            method="byos115_direct", variant=play_variants.BYOS115_DIRECT,
            result="ok",
            resolve_ms=int((time.perf_counter() - _byos_t0) * 1000))
        return _observe_traffic(
            RedirectResponse(_byos_url, status_code=302,
                             headers=_strategy.DIRECT_REDIRECT_HEADERS))
    if _requested_variant is not None:
        target = await run_db(_play_target, db, item,
                              variant=_requested_variant)
    else:
        target = await run_db(_play_target, db, item)
    _pt.mark("play_target")
    # 按变体取流的不变量：走到这里的只能是挂载中继 / 本机文件两种
    # 变体（Drive 两条与转码已在前面返回）；走到这里说明其分支已消失
    # （开关/挂载竞态），报 409，不许按通用分支静默改走。
    if _requested_variant not in (
            None, play_variants.MOUNT_RELAY, play_variants.LOCAL_FILE):
        raise HTTPException(
            status_code=409, detail="播放变体当前不可用")
    if _requested_variant == play_variants.MOUNT_RELAY and target.kind != "url":
        raise HTTPException(
            status_code=409, detail="播放变体当前不可用：mount_relay")
    if _requested_variant == play_variants.LOCAL_FILE and target.kind == "url":
        raise HTTPException(
            status_code=409, detail="播放变体当前不可用：local_file")
    if target.kind == "url":
        # 播放策略能力：对象存储直链名单命中时 302 到对象存储；Drive 条目
        # 直接 302 到谷歌 usercontent 直链。判定逻辑见 playback_strategy。
        # 回落分支：快路径未命中（挂载解析失败等）时仍走原 _play_target，
        # 这里保留原 drive 302 语义（_fid + 开关）。
        if _drive_switch_on:
            from backend.emby_server.drive_auth import drive_usercontent_download_url
            _pt.mark("drive_direct_302")
            _pt.done()
            return _observe_traffic(
                RedirectResponse(drive_usercontent_download_url(_fid), status_code=302,
                                 headers=_strategy.DIRECT_REDIRECT_HEADERS))
        direct_url = await run_db(
            _strategy.maybe_direct_url, db, getattr(item, "drive_file_id", None))
        # 按变体取流时不走对象存储直链分支（那不是所选变体的分支）。
        if direct_url and _requested_variant is None:
            _pt.mark("direct_302")
            _pt.done()
            return _observe_traffic(
                RedirectResponse(direct_url, status_code=302,
                                 headers=_strategy.DIRECT_REDIRECT_HEADERS))
        # 远程挂载专用中继（2026-10-17 用户拍板：给 115 留一条专用中继，
        # 其他照删）：mount:// 路径解析出的 kind="url" 目标（直链依赖挂载
        # 凭据 Cookie/令牌/UA，下发给客户端即死），由服务端带头代理转发；
        # Drive 条目一律走上面的 302，不许回中继。
        if mount_lib.is_mount_path(getattr(item, "file_path", "")):
            _pt.mark("mount_relay")
            from backend.emby_server.streaming import serve_mount_relay_async
            return _observe_traffic(
                await serve_mount_relay_async(
                    target.value, request, target.headers, media_type, pt=_pt))
        raise HTTPException(status_code=410, detail="relay removed")
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
    """转码地址瘦身（2026-10-24 转码删除）：只保留 stream.* 直连委托。

    SenPlayer 等客户端按 Emby 约定拼 /emby/videos/{id}/stream.{ext} 取流，
    这条通配路由必须继续把它转交 video_stream；其余一切路径（master.m3u8、
    切片、会话地址）都是已删除的转码路径，一律 404，不再起 ffmpeg。
    """
    # stream.* 是直接流不是转码路径；转交 video_stream 做完整鉴权与取流。
    if transcode_path.startswith("stream."):
        return await video_stream(item_id, request, user, db)
    raise HTTPException(status_code=404, detail="服务端转码已移除，请使用直连播放")


# ==================== 拆分说明（v2.13.0）====================
# 本文件原先 2100+ 行，已按关注点拆出三个模块（路由注册顺序与拆分前完全一致）：
#   - media_routes.py  图片投递 / 下载（hls1 转码入口已随转码删除移除）
#   - compat_routes.py 会话上报与协议补齐端点
#   - stream_routes.py 播放流容器变体 / 原始文件 / 字幕投递 / HLS 通配
# 三者把路由注册到上面这个 emby_router 上（由 backend/main.py 与 emby_api/main.py 导入触发）。
