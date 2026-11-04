"""Kodi / Emby 风格 NFO 解析与落库。

远端网盘里如果已经有刮削（``tvshow.nfo`` / ``movie.nfo`` / 与视频同名的 ``.nfo``），
扫描时优先使用它，不再调 TMDB 搜索（B 方案：TMDB 只用来补海报/背景图）。
"""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.emby_server.pinyin_sort import make_sort_name

logger = logging.getLogger(__name__)

# 支持的 NFO 根标签：电影 / 剧集 / 单集 / 季
_NFO_ROOTS = {"movie", "tvshow", "episodedetails", "season"}


def _text(root: ET.Element, tag: str) -> str:
    child = root.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return ""


def _texts(root: ET.Element, tag: str) -> List[str]:
    out: List[str] = []
    for child in root.findall(tag):
        if child.text and child.text.strip():
            out.append(child.text.strip())
    return out


def _pick_rating(root: ET.Element) -> Optional[float]:
    """Kodi 两种评分写法：

    - ``<ratings><rating name="tmdb" default="true"><value>8</value></rating></ratings>``
    - 直写 ``<rating>8</rating>``
    """
    ratings = root.find("ratings")
    if ratings is not None:
        fallback: Optional[float] = None
        for r in ratings.findall("rating"):
            name = (r.get("name") or "").lower()
            default = (r.get("default") or "").lower() == "true"
            try:
                value = float(_text(r, "value"))
            except (TypeError, ValueError):
                continue
            if name == "tmdb" or default:
                return round(value, 1)
            if fallback is None:
                fallback = value
        if fallback is not None:
            return round(fallback, 1)
    try:
        return round(float(_text(root, "rating")), 1)
    except (TypeError, ValueError):
        return None


def _pick_id(root: ET.Element, id_type: str) -> str:
    """tmdb / imdb / tvdb 三种写法：``<tmdbid>`` / ``<imdb_id>`` / ``<uniqueid type="tmdb">``"""
    tag_map = {
        "tmdb": ("tmdbid",),
        "imdb": ("imdb_id", "imdbid"),
        "tvdb": ("tvdbid",),
    }
    for tag in tag_map.get(id_type, ()):
        value = _text(root, tag)
        if value:
            return value
    for u in root.findall("uniqueid"):
        if (u.get("type") or "").lower() == id_type and u.text and u.text.strip():
            return u.text.strip()
    return ""


def _pick_year(root: ET.Element) -> Optional[int]:
    for tag in ("year", "premiered", "aired", "releasedate"):
        m = re.search(r"(\d{4})", _text(root, tag))
        if m:
            year = int(m.group(1))
            if 1900 <= year <= datetime.now().year + 2:
                return year
    return None


def _parse_streamdetails(root):
    """Parse <fileinfo><streamdetails>, extract video/audio tech specs.

    Kodi NFO format:
    <fileinfo><streamdetails>
      <video><codec>h264</codec><width>1920</width><height>1080</height></video>
      <audio><codec>aac</codec><channels>2</channels></audio>
    </streamdetails></fileinfo>

    Returns dict with possible keys: video_codec, video_width,
    video_height, video_duration (seconds), audio_codec, audio_channels.
    Empty dict if no streamdetails.
    """
    out = {}
    fileinfo = root.find("fileinfo")
    if fileinfo is None:
        return out
    sd = fileinfo.find("streamdetails")
    if sd is None:
        return out
    video = sd.find("video")
    if video is not None:
        codec = _text(video, "codec") or _text(video, "micodec")
        if codec:
            out["video_codec"] = codec
        try:
            w = int(_text(video, "width"))
            if w > 0:
                out["video_width"] = w
        except (TypeError, ValueError):
            pass
        try:
            h = int(_text(video, "height"))
            if h > 0:
                out["video_height"] = h
        except (TypeError, ValueError):
            pass
        try:
            dur = int(_text(video, "durationinseconds"))
            if dur > 0:
                out["video_duration"] = dur
        except (TypeError, ValueError):
            pass
    audio = sd.find("audio")
    if audio is not None:
        codec = _text(audio, "codec") or _text(audio, "micodec")
        if codec:
            out["audio_codec"] = codec
        try:
            ch = int(_text(audio, "channels"))
            if ch > 0:
                out["audio_channels"] = ch
        except (TypeError, ValueError):
            pass
    return out


def _pick_thumb(root: ET.Element) -> str:
    """Kodi 海报：``<thumb aspect="poster">URL</thumb>``（可多个）。

    取 aspect 为 poster（或未标 aspect）的第一个非空值；只认 http(s) 地址，
    相对路径在网盘语境里取不到，写进去只会变成坏图。
    """
    for t in root.findall("thumb"):
        if (t.get("aspect") or "poster").lower() != "poster":
            continue
        value = (t.text or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


def _pick_fanart(root: ET.Element) -> str:
    """Kodi 背景图：``<fanart><thumb>URL</thumb></fanart>``，取第一个 http(s) 值。"""
    fanart = root.find("fanart")
    if fanart is None:
        return ""
    for t in fanart.findall("thumb"):
        value = (t.text or "").strip()
        if value.startswith(("http://", "https://")):
            return value
    return ""


def _parse_actors(root: ET.Element) -> List[Dict[str, str]]:
    """解析 ``<actor>`` 列表（Emby/Jellyfin/Kodi 标准结构，Nova N3-1）。

    ``<actor><name>…</name><role>…</role><type>Actor</type><thumb>URL</thumb></actor>``
    → ``[{"name", "role", "image"}]``，保持文档顺序（即戏份顺序）。
    - 无 ``<name>`` 的丢弃；同名重复只留第一条；
    - ``<type>`` 明确写了非 Actor（如 Director）的不是演员，不收；
    - 头像只收 http(s)（与 ``_pick_thumb`` 同口径：相对路径在网盘语境取不到）。
    """
    out: List[Dict[str, str]] = []
    seen = set()
    for el in root.findall("actor"):
        name = _text(el, "name")
        if not name or name in seen:
            continue
        ptype = _text(el, "type")
        if ptype and ptype.lower() != "actor":
            continue
        seen.add(name)
        thumb = _text(el, "thumb")
        if not thumb.startswith(("http://", "https://")):
            thumb = ""
        out.append({"name": name, "role": _text(el, "role"), "image": thumb})
    return out


def parse_nfo(text: str) -> Optional[Dict[str, Any]]:
    """解析 NFO 文本 → 元数据 dict；解析失败或没有可用字段返回 None。"""
    if not text or not isinstance(text, str):
        return None
    text = text.lstrip("﻿").strip()
    if not text.startswith("<"):
        return None
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    kind = (root.tag or "").lower()
    if kind not in _NFO_ROOTS:
        return None
    data: Dict[str, Any] = {
        "nfo_kind": kind,
        "title": _text(root, "title"),
        "originaltitle": _text(root, "originaltitle"),
        # 简介：plot 为空时退回 outline（部分刮削器只写 outline）
        "plot": _text(root, "plot") or _text(root, "outline"),
        "tagline": _text(root, "tagline"),
        "rating": _pick_rating(root),
        "year": _pick_year(root),
        "genres": _texts(root, "genre"),
        "studios": _texts(root, "studio"),
        "mpaa": _text(root, "mpaa"),
        "tmdb_id": _pick_id(root, "tmdb"),
        "imdb_id": _pick_id(root, "imdb"),
        "tvdb_id": _pick_id(root, "tvdb"),
        # 本地自带图（整理工具常把海报/背景地址写进 NFO）：落库只填空缺
        "poster": _pick_thumb(root),
        "fanart": _pick_fanart(root),
        # 演员（name/role/头像）：落库走 apply_nfo 的 db 分支（N3-1）
        "actors": _parse_actors(root),
    }
    # 媒体流信息（fileinfo/streamdetails）：NFO 优先于文件名解析
    data["streamdetails"] = _parse_streamdetails(root)
    if kind == "episodedetails":
        data["season"] = _text(root, "season")
        data["episode"] = _text(root, "episode")
    if not (data["title"] or data["plot"] or data["tmdb_id"] or data["imdb_id"]):
        return None
    return data


def _merge_aliases(item: Any, names: List[Optional[str]]) -> None:
    existing = [a for a in (item.aliases or "").split(",") if a]
    merged = existing + [n.strip() for n in names if n and n.strip() not in existing]
    if merged:
        item.aliases = ",".join(dict.fromkeys(merged))[:2000]


def _apply_nfo_cast(db: Any, item: Any, actors: List[Dict[str, str]]) -> None:
    """把 NFO 演员写进既有 ``emby_people`` 管道（Nova N3-1，不新造一套）。

    边界与既有演员管道（enrich ``_apply_cast`` / ``_apply_douban_cast``）同款：
    - 条目还没有演员行时才新增（name/role/头像按 NFO，顺序即 sort_order）；
    - 已有演员行（TMDB/豆瓣先写或上轮写过）时不新增、不删、不重排，只按
      名字给已有行补空 role / 空头像——NFO 与在线演员表不互相整表覆盖；
    - 演员是增强信息：失败只记日志，绝不影响 NFO 文本落库主流程。
    """
    if not actors or getattr(item, "id", None) is None:
        return
    try:
        from backend.emby_server import models as em

        existing = db.query(em.EmbyPerson).filter(
            em.EmbyPerson.item_id == item.id).all()
        missing_image = False
        if existing:
            by_name = {a["name"]: a for a in actors}
            for p in existing:
                a = by_name.get(p.name)
                if a:
                    if a.get("role") and not p.role:
                        p.role = str(a["role"])[:200]
                    if a.get("image") and not p.image:
                        p.image = str(a["image"])[:1024]
                missing_image = missing_image or not p.image
        else:
            for idx, a in enumerate(actors):
                db.add(em.EmbyPerson(
                    item_id=item.id,
                    name=str(a["name"])[:200],
                    role=str(a.get("role") or "")[:200],
                    image=str(a.get("image") or "")[:1024],
                    sort_order=idx,
                    person_tmdb_id="",
                ))
                missing_image = missing_image or not a.get("image")
        if missing_image:
            # 有演员没头像：唤醒演员补全线程按名补图（与 _apply_cast 同款），
            # 写事务里不碰网络。
            try:
                from backend.emby_server import refresh_person_worker as _rpw
                _rpw.kick()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 — 演员落库失败不该影响主流程
        logger.debug("NFO 演员落库失败 %s: %s", getattr(item, "name", ""), exc)


def apply_nfo(item: Any, data: Dict[str, Any], kind: str,
              skip_overview: bool = False, db: Any = None) -> None:
    """把 NFO 元数据落到 MediaItem 上（用户整理的文本优先于 TMDB）。

    kind: movie / series / episode / season。episode / season 只写简介与评分，
    名称由调用方按展示格式处理；只有 movie / series 参与 TMDB 刮削策略，
    也只有它们写 ``last_scraped_at``。

    skip_overview: 批3（单集分项开关）——episode 的简介分项关时跳过 plot 写入，
    其它字段（评分/流信息）不受影响。默认 False = 现状行为。

    db: 传 DB 会话时（扫描/enrich 的落库点）把 NFO 演员一并写进
    ``emby_people``（仅 movie / series，见 ``_apply_nfo_cast`` 的边界）；
    不传 db 时行为与旧版逐字一致（纯字段落库）。
    """
    if kind in ("movie", "series"):
        if data.get("title"):
            item.name = data["title"]
            item.sort_name = make_sort_name(data["title"])
        if data.get("originaltitle"):
            item.original_title = data["originaltitle"]
        _merge_aliases(item, [data.get("title"), data.get("originaltitle")])
        if data.get("year") and not item.production_year:
            item.production_year = data["year"]
        if data.get("mpaa"):
            item.official_rating = data["mpaa"]
        if data.get("genres"):
            item.genres = ",".join(data["genres"])
        if data.get("studios"):
            item.studios = ",".join(data["studios"])
        if data.get("tmdb_id"):
            item.tmdb_id = str(data["tmdb_id"])
        if data.get("imdb_id"):
            item.imdb_id = data["imdb_id"]
        # NFO 自带海报/背景地址：只填空缺——side 本地图与 TMDB 图在本轮
        # 前面的阶段已处理（B 方案：图以它们为准），这里只兜「都没有」。
        if data.get("poster") and not (
                getattr(item, "poster_path", None)
                or getattr(item, "primary_image_url", None)):
            item.primary_image_url = data["poster"]
        if data.get("fanart") and not (
                getattr(item, "backdrop_path", None)
                or getattr(item, "backdrop_image_url", None)):
            item.backdrop_image_url = data["fanart"]
        item.last_scraped_at = datetime.now()
        # 文字来自本地 NFO（用户自己整理的，最权威）
        item.metadata_source = "nfo"
    # 媒体流信息（NFO streamdetails 优先于文件名解析，更准确）
    sd = data.get("streamdetails") or {}
    if sd.get("video_codec"):
        item.video_codec = sd["video_codec"]
    if sd.get("video_width"):
        item.width = sd["video_width"]
    if sd.get("video_height"):
        item.height = sd["video_height"]
    if sd.get("video_duration"):
        # duration_ticks 是 100ns ticks
        item.duration_ticks = int(sd["video_duration"] * 10_000_000)
    if sd.get("audio_codec"):
        item.audio_codec = sd["audio_codec"]
    if data.get("plot") and not (skip_overview and kind == "episode"):
        item.overview = data["plot"]
    if data.get("rating") is not None:
        item.community_rating = data["rating"]
    # 演员（N3-1）：与文本字段同门控——只有 movie / series 落库；
    # episode / season 的 NFO 演员不在本项范围（Nova 未实证、口径未定版）。
    if db is not None and kind in ("movie", "series"):
        _apply_nfo_cast(db, item, data.get("actors") or [])
