"""多源元数据：七个源的具体客户端与统一命中结构

## 统一命中结构（``Hit``）

各站返回的东西千奇百怪，但引擎只认这七个字段。**源只负责"取到并归一化"，
不负责"要不要写进条目"**——那是 ``engine`` 的事（这样才谈得上优先级与失败隔离）。

## 每个源的能力与代价（如实写，不夸大）

| 源 | 要 key | 中文 | 简介 | 评分 | 备注 |
|---|---|---|---|---|---|
| Bangumi | 否 | 强 | 有 | 有 | 中文番剧最准；公开接口，UA 礼仪限速 |
| 豆瓣 | 否 | 强 | **没有** | **没有** | subject_suggest 只给标题/年份/海报/ID |
| AniList | 否 | 中 | 有 | 有 | GraphQL，动漫覆盖好，中文标题是 native title |
| TVmaze | 否 | 弱 | 有 | 有 | 英文剧集强，中文片名常常搜不到 |
| OMDb | **是** | 弱 | 有 | 有 | 只有电影；剧集直接不参与 |
| TheTVDB | **是** | 无 | 有 | 有 | v4 API，要 token |
| TMDB | **是** | 中 | 有 | 有 | 字段最全，但中文剧集/综艺收录偏少 → 默认排最后 |

**字段取不到就留空**：宁可少一个字段，也不要拿不可靠的来源填（豆瓣没有简介就是没有）。
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Optional

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 12
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


class SourceError(RuntimeError):
    """这一源这次失败了（引擎会隔离掉，不影响其它源）"""


@dataclass
class Hit:
    """归一化后的命中：一个源对一部片能给出的全部信息"""

    source: str
    title: str = ""
    original_title: str = ""
    overview: str = ""
    genres: list = field(default_factory=list)
    rating: Optional[float] = None
    year: Optional[int] = None
    poster: Optional[str] = None
    lang: str = "en"
    external_ids: dict = field(default_factory=dict)
    kind_hint: str = ""

    def has(self, field_name: str) -> bool:
        value = getattr(self, field_name, None)
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, dict)):
            return bool(value)
        return value is not None


@dataclass
class SourceSpec:
    """源的静态元信息（能力、要不要 key、天生给哪种语言）"""
    id: str
    label: str
    requires_key: bool
    lang: str
    note: str
    search: Callable
    #: key 去哪申请（界面直接展示，避免管理员去搜索引擎里找）
    apply_url: str = ""
    apply_hint: str = ""
    #: 密钥存在哪个 SystemConfig 键（告诉用户“在哪填”）
    key_storage: str = ""


# ---------------------------------------------------------------------------
# HTTP 小工具：统一超时 / UA / 异常口径
# ---------------------------------------------------------------------------

def _http_json(url: str, *, headers: Optional[dict] = None, data: Optional[bytes] = None,
               timeout: int = REQUEST_TIMEOUT):
    """发一个请求并解析 JSON。**失败一律抛 ``SourceError``**（引擎负责隔离）"""
    req = urllib.request.Request(url, headers={"User-Agent": _UA, **(headers or {})},
                                 data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001 — 统一成 SourceError，交给引擎隔离
        raise SourceError(f"{type(exc).__name__}: {exc}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise SourceError("返回的不是 JSON") from exc


def _http_obj(url: str, **kwargs) -> dict:
    """同 :func:`_http_json`，但要求返回对象（TVmaze 那种数组用另一个）"""
    data = _http_json(url, **kwargs)
    if not isinstance(data, dict):
        raise SourceError("返回结构不是对象")
    return data


def _year_of(raw) -> Optional[int]:
    text = str(raw or "")[:4]
    return int(text) if text.isdigit() else None


def _clean_tags(text: str) -> str:
    """HTML 标签剥掉（TVmaze / TVDB 的简介是 HTML）"""
    return re.sub(r"<[^>]+>", "", str(text or "")).strip()


# ---------------------------------------------------------------------------
# 1. TMDB：复用既有客户端（它自己已经有多密钥与冷却）
# ---------------------------------------------------------------------------

def _hit_from_tmdb_raw(raw: dict, kind: str) -> Hit:
    """TMDB 搜索/find 结果 dict → Hit（search 与 IMDb 桥共用一套口径）"""
    return Hit(
        source="tmdb",
        title=str(raw.get("title") or raw.get("name") or ""),
        original_title=str(raw.get("original_title") or raw.get("original_name") or ""),
        overview=str(raw.get("overview") or ""),
        genres=[str(g.get("name")) for g in (raw.get("genres") or []) if isinstance(g, dict)],
        rating=_number(raw.get("vote_average")),
        year=_year_of(raw.get("release_date") or raw.get("first_air_date")),
        poster=_tmdb_poster(raw),
        lang="en",
        external_ids={"tmdb": str(raw.get("id") or "")} if raw.get("id") else {},
        kind_hint="movie" if kind == "movie" else "tv",
    )


def search_tmdb(title: str, year: Optional[int], kind: str, *, pool, gate) -> Optional[Hit]:
    from backend.emby_server import tmdb as tmdb_lib

    gate.acquire()
    if not tmdb_lib.tmdb_client.configured:
        raise SourceError("没有可用的 TMDB 密钥")
    raw = tmdb_lib.tmdb_client.search(title, year, kind)
    if not raw:
        return None
    return _hit_from_tmdb_raw(raw, kind)


def find_tmdb_by_imdb(imdb_id: str, year: Optional[int], kind: str, *, pool, gate) -> Optional[Hit]:
    """IMDb 编号 → TMDB 精确落点（救援链的桥二）。

    ``find_by_imdb`` 是精确查询，返回的候选天然可信；仍加一道年份门
    （与条目年份差 >1 视为对不上），防止同名重拍串号。
    """
    from backend.emby_server import tmdb as tmdb_lib

    gate.acquire()
    if not tmdb_lib.tmdb_client.configured:
        raise SourceError("没有可用的 TMDB 密钥")
    found = tmdb_lib.tmdb_client.find_by_imdb(imdb_id) or {}
    key = "movie_results" if kind == "movie" else "tv_results"
    for raw in found.get(key) or []:
        cand_year = _year_of(raw.get("release_date") or raw.get("first_air_date"))
        if year and cand_year and abs(cand_year - int(year)) > 1:
            continue
        return _hit_from_tmdb_raw(raw, kind)
    return None


def _tmdb_poster(raw: dict) -> Optional[str]:
    path = raw.get("poster_path")
    if not path:
        return None
    from backend.emby_server import tmdb as tmdb_lib

    return f"{tmdb_lib.image_base()}/w500{path}"


def _number(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # 各站评分口径不同（10 分制 / 100 分制），这里统一到 10 分制
    if number > 10:
        number = number / 10.0 if number <= 100 else number / 10.0
    return round(number, 2) if 0 <= number <= 10 else None


# ---------------------------------------------------------------------------
# 2. 豆瓣：复用 altmeta（subject_suggest，没有简介与评分）
# ---------------------------------------------------------------------------

def search_douban(title: str, year: Optional[int], kind: str, *, pool, gate) -> Optional[Hit]:
    from backend.emby_server import altmeta

    gate.acquire()
    hit = altmeta.search(title, year, kind, min_interval=0.0)
    if not hit:
        return None
    return Hit(
        source="douban",
        title=str(hit.get("title") or ""),
        overview="",            # subject_suggest 不给简介（如实留空）
        rating=None,            # 同理不评分
        year=_year_of(hit.get("year")),
        poster=str(hit.get("image") or "") or None,
        lang="zh",
        external_ids={"douban": str(hit.get("id") or "")} if hit.get("id") else {},
    )


# ---------------------------------------------------------------------------
# 3. Bangumi：复用 altmeta（中文名最准，番剧强）
# ---------------------------------------------------------------------------

def search_bangumi(title: str, year: Optional[int], kind: str, *, pool, gate) -> Optional[Hit]:
    from backend.emby_server import altmeta

    gate.acquire()
    hit = altmeta.search_bangumi(title, year, kind, min_interval=0.0)
    if not hit:
        return None
    return Hit(
        source="bangumi",
        title=str(hit.get("title") or ""),
        original_title=str(hit.get("original_title") or ""),
        year=_year_of(hit.get("year")),
        poster=str(hit.get("image") or "") or None,
        lang="zh",
        external_ids={"bangumi": str(hit.get("id") or "")} if hit.get("id") else {},
    )


# ---------------------------------------------------------------------------
# 4. AniList：GraphQL，动漫覆盖好，native title 就是本地语言名
# ---------------------------------------------------------------------------

ANILIST_URL = "https://graphql.anilist.co"
ANILIST_QUERY = """
query ($search: String, $year: Int, $type: MediaType) {
  Page(page: 1, perPage: 5) {
    media(search: $search, type: $type, startDate_greater: $year, startDate_lesser: $year) {
      id title { romaji english native } description(asHtml: false)
      averageScore genres coverImage { extraLarge large } startDate { year } format
    }
  }
}
"""


def search_anilist(title: str, year: Optional[int], kind: str, *, pool, gate) -> Optional[Hit]:
    gate.acquire()
    wanted = "MOVIE" if kind == "movie" else "TV"
    # 年份用「起始年 = 目标年」粗筛；传 0 表示不限（AniList 的筛选对 null 敏感）
    payload = json.dumps({"query": ANILIST_QUERY, "variables": {
        "search": (title or "").strip(), "year": int(year or 0), "type": wanted,
    }}).encode("utf-8")
    data = _http_obj(ANILIST_URL, headers={"Content-Type": "application/json"}, data=payload)
    if data.get("errors"):
        raise SourceError(str((data["errors"] or [{}])[0].get("message") or "GraphQL 报错"))
    media = ((data.get("data") or {}).get("Page") or {}).get("media") or []
    if not media:
        return None
    hit = _best_anilist(media, title)
    if hit is None:
        return None
    titles = hit.get("title") or {}
    native = str(titles.get("native") or "")
    romaji = str(titles.get("romaji") or "")
    cover = hit.get("coverImage") or {}
    return Hit(
        source="anilist",
        # 中文优先开关下拿 native（本地语言）名；没有 native 就退回 romaji
        title=native or romaji,
        original_title=romaji,
        overview=_clean_tags(hit.get("description")),
        genres=[str(g) for g in (hit.get("genres") or [])],
        rating=_number(hit.get("averageScore")),
        year=_year_of((hit.get("startDate") or {}).get("year")),
        poster=str(cover.get("extraLarge") or cover.get("large") or "") or None,
        lang="zh" if _has_cjk(native) else "en",
        external_ids={"anilist": str(hit.get("id"))} if hit.get("id") else {},
        kind_hint=str(hit.get("format") or ""),
    )


def _best_anilist(media: list, want: str) -> Optional[dict]:
    """标题必须完全一致或互为前缀（同系列作品宁可漏掉，别写错片名）"""
    target = _norm(want)
    for item in media:
        if not isinstance(item, dict):
            continue
        titles = item.get("title") or {}
        for candidate in (titles.get("native"), titles.get("romaji"), titles.get("english")):
            got = _norm(str(candidate or ""))
            if got and (got == target or got.startswith(target) or target.startswith(got)):
                return item
    return None


def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in str(text or ""))


def _norm(text: str) -> str:
    return re.sub(r"[\s\.\-_:,!！?？'\"“”‘’()（）\[\]【】]", "", str(text or "")).lower()


def _title_consistent(want: str, got: str) -> bool:
    """标题一致性：归一化后全等或互为前缀（与本文件 TVMaze/AniList 同口径）。

    任一方为空即不一致——没法验证 = 不可信。
    """
    w = _norm(want)
    g = _norm(got)
    if not w or not g:
        return False
    return g == w or g.startswith(w) or w.startswith(g)


# ---------------------------------------------------------------------------
# 5. TVmaze：免费、无 key，英文剧集强
# ---------------------------------------------------------------------------

TVMAZE_SEARCH = "https://api.tvmaze.com/search/shows"


def search_tvmaze(title: str, year: Optional[int], kind: str, *, pool, gate) -> Optional[Hit]:
    if kind == "movie":
        return None               # TVmaze 只有剧集
    gate.acquire()
    url = f"{TVMAZE_SEARCH}?q={urllib.parse.quote((title or '').strip())}"
    data = _http_json(url)
    items = data if isinstance(data, list) else [data]
    best = None
    for entry in items:
        show = (entry or {}).get("show") if isinstance(entry, dict) else None
        if not isinstance(show, dict):
            continue
        got = _norm(show.get("name") or "")
        target = _norm(title)
        if got and (got == target or got.startswith(target) or target.startswith(got)):
            best = show
            break
    if best is None:
        return None
    image = best.get("image") or {}
    ext_ids = {"tvmaze": str(best.get("id"))} if best.get("id") else {}
    # IMDb 编号一并带上：引擎的「IMDb 接力」靠它去 TMDB 精确落点
    imdb = str((best.get("externals") or {}).get("imdb") or "")
    if imdb:
        ext_ids["imdb"] = imdb
    return Hit(
        source="tvmaze",
        title=str(best.get("name") or ""),
        overview=_clean_tags(best.get("summary")),
        genres=[str(g) for g in (best.get("genres") or [])],
        rating=_number((best.get("rating") or {}).get("average")),
        year=_year_of((best.get("premiered") or "")[:4]),
        poster=str(image.get("original") or image.get("medium") or "") or None,
        lang="en",
        external_ids=ext_ids,
    )


# ---------------------------------------------------------------------------
# 6. OMDb：只有电影，需要免费 key（omdb 无 key 时整源不参与）
# ---------------------------------------------------------------------------

OMDB_URL = "http://www.omdbapi.com/"


def search_omdb(title: str, year: Optional[int], kind: str, *, pool, gate) -> Optional[Hit]:
    if kind != "movie":
        return None               # OMDb 不索引剧集，剧集条目直接不查
    key = pool.pick_any()
    if not key:
        raise SourceError("没有可用的 OMDb 密钥")
    gate.acquire()
    params = {"apikey": key, "t": (title or "").strip()}
    if year:
        params["y"] = str(year)
    data = _http_obj(f"{OMDB_URL}?{urllib.parse.urlencode(params)}")
    if str(data.get("Response") or "").lower() == "false":
        error = str(data.get("Error") or "")
        if "key" in error.lower():
            pool.note_invalid(key)
            raise SourceError("OMDb 密钥无效")
        return None
    # OMDb 的 t= 是模糊 best-guess：拿冷门/中文名去问，会返回毫不相干的
    # 片（2026-10-21 生产实证：《彼得58》→ 捷克建筑纪录片《Architektura
    # ČSSR 58–89》、《逐爱之心 2》→《穿普拉达的女王2》，只对上年份）。
    # 返回的标题/年份跟问的对不上就当没命中——它的 imdbID 会被引擎当真
    # 喂给 IMDb 接力绑 TMDB，模糊命中在这里就是错绑源头。宁可 none 不乱认。
    if not _title_consistent(title, str(data.get("Title") or "")):
        return None
    got_year = _year_of(data.get("Year"))
    if year and got_year and int(year) != got_year:
        return None
    genres = [g.strip() for g in str(data.get("Genre") or "").split(",") if g.strip()]
    return Hit(
        source="omdb",
        title=str(data.get("Title") or ""),
        overview=str(data.get("Plot") or ""),
        genres=genres,
        rating=_number(data.get("imdbRating")),
        year=_year_of(data.get("Year")),
        poster=str(data.get("Poster") or "") or None,
        lang="en",
        external_ids={"imdb": str(data.get("imdbID") or "")} if data.get("imdbID") else {},
    )


# ---------------------------------------------------------------------------
# 7. TheTVDB：v4 API，需要 token
# ---------------------------------------------------------------------------

TVDB_SEARCH = "https://api4.thetvdb.com/v4/search"
TVDB_LOGIN = "https://api4.thetvdb.com/v4/login"

# 每把 key 独立缓存登录 token：{api_key: (token, 获取时间戳)}
# TVDB v4 的 key 不能直接当 Bearer 用，必须先 POST /login 换 JWT
_tvdb_token_cache: dict = {}
_tvdb_token_lock = __import__("threading").Lock()
_TVDB_TOKEN_TTL = 20 * 3600  # 20 小时，比官方过期稍短，提前换


def _tvdb_bearer(api_key: str) -> str:
    """拿 key 换 Bearer token（带缓存，过期/401 自动重登）"""
    import time as _time
    now = _time.time()
    with _tvdb_token_lock:
        hit = _tvdb_token_cache.get(api_key)
        if hit and now - hit[1] < _TVDB_TOKEN_TTL:
            return hit[0]
    body = json.dumps({"apikey": api_key}).encode("utf-8")
    data = _http_obj(TVDB_LOGIN, headers={"Content-Type": "application/json"}, data=body)
    token = (data.get("data") or {}).get("token") or ""
    if not token:
        raise SourceError("TheTVDB 登录失败：没拿到 token")
    with _tvdb_token_lock:
        _tvdb_token_cache[api_key] = (token, now)
    return token


def _tvdb_invalidate(api_key: str) -> None:
    with _tvdb_token_lock:
        _tvdb_token_cache.pop(api_key, None)


def search_tvdb(title: str, year: Optional[int], kind: str, *, pool, gate) -> Optional[Hit]:
    if kind == "movie":
        return None               # v4 的 search 只覆盖剧集
    key = pool.pick_any()
    if not key:
        raise SourceError("没有可用的 TheTVDB 密钥")
    gate.acquire()
    query = f"{title} {year}".strip() if year else (title or "").strip()
    url = f"{TVDB_SEARCH}?query={urllib.parse.quote(query)}&limit=5"
    bearer = _tvdb_bearer(key)
    try:
        data = _http_obj(url, headers={"Authorization": f"Bearer {bearer}"})
    except SourceError as e:
        # token 可能过期：清缓存重登一次再试
        if "401" in str(e) or "unauthorized" in str(e).lower():
            _tvdb_invalidate(key)
            bearer = _tvdb_bearer(key)
            data = _http_obj(url, headers={"Authorization": f"Bearer {bearer}"})
        else:
            raise
    results = data.get("data") or []
    best = None
    for entry in results:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("objectType") or "series") != "series":
            continue
        got = _norm(entry.get("name") or "")
        target = _norm(title)
        if got and (got == target or got.startswith(target) or target.startswith(got)):
            best = entry
            break
    if best is None:
        return None
    remote = best.get("remote_ids") or []
    ids = {str(r.get("sourceName")): str(r.get("id"))
           for r in remote if isinstance(r, dict) and r.get("id")}
    ids.setdefault("tvdb", str(best.get("tvdbId") or ""))
    return Hit(
        source="tvdb",
        title=str(best.get("name") or ""),
        overview=str(best.get("overview") or ""),
        genres=[str(g.get("name")) for g in (best.get("genres") or []) if isinstance(g, dict)],
        rating=_number(best.get("score")),
        year=_year_of(str(best.get("year") or "")),
        lang="en",
        external_ids={k: v for k, v in ids.items() if v},
    )


SPECS: list = [
    SourceSpec("bangumi", "Bangumi（番组）", False, "zh",
               "中文番剧最准；公开接口，按 UA 礼仪限速", search_bangumi),
    SourceSpec("douban", "豆瓣", False, "zh",
               "中文剧命中率高；subject_suggest 只给标题/年份/海报，没有简介与评分", search_douban),
    SourceSpec("anilist", "AniList", False, "zh",
               "动漫覆盖好，简介与评分齐全；GraphQL 公开接口", search_anilist),
    SourceSpec("tvmaze", "TVmaze", False, "en",
               "英文剧集强、免费无 key；中文片名常常搜不到", search_tvmaze),
    SourceSpec("omdb", "OMDb（IMDb 数据）", True, "en",
               "需要免费 key；只索引电影，剧集不参与", search_omdb,
               apply_url="https://www.omdbapi.com/apikey.aspx",
               apply_hint="OMDb key 免费申请：填个邮箱就能拿到，每日 1000 次（够个人用）。"
                          "只索引**电影**，剧集条目不会用它。",
               key_storage="meta_source_keys_omdb"),
    SourceSpec("tvdb", "TheTVDB", True, "en",
               "需要 API key；只索引剧集", search_tvdb,
               apply_url="https://thetvdb.com/",
               apply_hint="TheTVDB key 免费申请：注册账号后在「API」页申请 v4 API key，"
                          "免费档每月 5000 次查询。只索引**剧集**。",
               key_storage="meta_source_keys_tvdb"),
    SourceSpec("tmdb", "TMDB", True, "en",
               "字段最全，但中文剧集/综艺收录偏少，默认排在最后", search_tmdb,
               apply_url="https://www.themoviedb.org/settings/api",
               apply_hint="TMDB key 免费申请：账号设置页直接生成 v3 API Key。"
                          "密钥填在下方「TMDB 密钥池与镜像」卡片里（那里也是唯一入口）。",
               key_storage="tmdb_api_keys"),
]

SPEC_BY_ID = {spec.id: spec for spec in SPECS}

__all__ = ["Hit", "SPECS", "SPEC_BY_ID", "SourceError", "SourceSpec"]
