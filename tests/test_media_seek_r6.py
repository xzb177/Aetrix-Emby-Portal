"""求片中心 r6 轮修复回归测试（backend/media_seek.py）

覆盖 r6 轮亲验属实的 9 项修复（DeepSeek 审查 R5-D.2~D.16 线索 → 协调员亲验 →
DeepSeek 逐项产出 diff，协调员落地）：
- F1 tmdb_id 精确匹配加类型过滤（防 movie/tv 撞号）
- F2 片名回退匹配加年份校验（防同名重拍片误判）
- F3 脏数据容错（vote_average / episode_count 不再 500）
- F4 isdigit 加 isascii（Unicode 数字不再抛 Python 原文/500）
- F5 LIKE 通配符转义（%/_ 不再匹配任意条目）
- F6 死代码 `num < 0` 删除（无行为变化，仅防回归）
- F7 search_candidates 新增 degraded 标记（TMDB 故障可区分）
- F8 season_label "0" → 特别篇（与 normalize_season 口径一致）
- F9 单字中文片名可命中（纯 ASCII 短词仍拦截）

全部用隔离的内存 SQLite，不碰网络（TMDB 客户端用假替身）、不碰生产库。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import media_seek, models
from backend.emby_server import models as em
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def _item(db, *, name, tmdb_id=None, item_type="movie", year=None, guid=None):
    lib = em.Library(guid=f"lib-{guid or name}", name="测试库",
                     collection_type="movies", paths="")
    db.add(lib)
    db.flush()
    row = em.MediaItem(
        guid=guid or f"item-{name}-{tmdb_id or 'x'}", library_id=lib.id,
        item_type=item_type, name=name, tmdb_id=tmdb_id,
        file_path=f"/media/{name}.mkv",
    )
    if year is not None:
        row.production_year = year
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class FakeTmdb:
    def __init__(self, *, configured=True, hits=None, details=None, fail_kinds=()):
        self.configured = configured
        self._hits = hits or {}
        self._details = details or {}
        self._fail_kinds = set(fail_kinds)

    def search_candidates(self, name, kind, limit=6):
        if kind in self._fail_kinds:
            raise RuntimeError("tmdb boom")
        return list(self._hits.get(kind, []))[:limit]

    def details(self, tmdb_id, kind):
        return self._details.get(str(tmdb_id))


# ==================== F1：tmdb_id 分支类型过滤 ====================

def test_library_hit_tmdb_id_respects_type(db):
    _item(db, name="撞号电影", tmdb_id="12345", item_type="movie", guid="g-movie")
    _item(db, name="撞号剧集", tmdb_id="12345", item_type="series", guid="g-series")

    hit = media_seek.library_hit(db, tmdb_id="12345", item_type="series")
    assert hit is not None and hit.guid == "g-series", "剧集搜索不应被同号电影顶掉"

    hit = media_seek.library_hit(db, tmdb_id="12345", item_type="movie")
    assert hit is not None and hit.guid == "g-movie"

    # 不传类型时保持原行为：精确匹配到任一条即可
    hit = media_seek.library_hit(db, tmdb_id="12345")
    assert hit is not None and hit.tmdb_id == "12345"


# ==================== F2：片名回退年份校验 ====================

def test_library_hit_name_fallback_checks_year(db):
    _item(db, name="重拍片", item_type="movie", year=1995, guid="g-old")
    _item(db, name="重拍片", item_type="movie", year=2024, guid="g-new")

    hit = media_seek.library_hit(db, name="重拍片", item_type="movie", year="2024")
    assert hit is not None and hit.guid == "g-new", "应命中 2024 版而非旧版"

    hit = media_seek.library_hit(db, name="重拍片", item_type="movie", year="1995")
    assert hit is not None and hit.guid == "g-old"

    # 年份缺失/解析失败 → 不过滤，保持原行为
    assert media_seek.library_hit(db, name="重拍片", item_type="movie") is not None
    assert media_seek.library_hit(db, name="重拍片", item_type="movie", year="abcd") is not None


def test_library_hit_year_allows_null_production_year(db):
    _item(db, name="老条目", item_type="movie", year=None, guid="g-noyear")
    hit = media_seek.library_hit(db, name="老条目", item_type="movie", year="2024")
    assert hit is not None and hit.guid == "g-noyear", "没刮削年份的老条目不应被漏判"


def test_find_library_match_passes_year(db):
    user = models.WebUser(username="seeker6", password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    _item(db, name="重拍片2", item_type="movie", year=1995, guid="g2-old")
    _item(db, name="重拍片2", item_type="movie", year=2024, guid="g2-new")
    req = models.MovieRequest(user_id=user.id, movie_name="重拍片2",
                              type="movie", year="2024", status="pending")
    db.add(req)
    db.commit()
    match = media_seek.find_library_match(db, req)
    assert match is not None and match.guid == "g2-new"


# ==================== F3：脏数据容错 ====================

def test_safe_float_and_int():
    assert media_seek._safe_float("N/A") == 0.0
    assert media_seek._safe_float(None) == 0.0
    assert media_seek._safe_float("8.4") == 8.4
    assert media_seek._safe_int("abc") == 0
    assert media_seek._safe_int(None) == 0
    assert media_seek._safe_int("13") == 13


def test_search_candidates_dirty_rating_no_500(db, monkeypatch):
    from backend.emby_server import tmdb
    monkeypatch.setattr(tmdb, "tmdb_client", FakeTmdb(hits={
        "movie": [{"id": 1, "title": "脏评分片", "release_date": "2020-01-01",
                   "vote_average": "N/A", "poster_path": None}],
        "series": [],
    }))
    result = media_seek.search_candidates(db, "脏评分")
    assert result["results"][0]["rating"] is None


def test_seasons_for_dirty_episode_count_no_500(monkeypatch):
    from backend.emby_server import tmdb
    monkeypatch.setattr(tmdb, "tmdb_client", FakeTmdb(details={"1396": {
        "seasons": [{"season_number": 1, "name": "第 1 季",
                     "episode_count": "未知", "air_date": ""}],
    }}))
    seasons = media_seek.seasons_for("1396")
    assert seasons[0]["episode_count"] == 0


# ==================== F4：isdigit 加 isascii ====================

def test_normalize_season_unicode_digit_friendly_error():
    with pytest.raises(ValueError, match="季要填数字"):
        media_seek.normalize_season("²", series=True)


def test_normalize_tmdb_id_unicode_digit():
    assert media_seek.normalize_tmdb_id("²") == ""
    assert media_seek.normalize_tmdb_id("157336") == "157336"


def test_daily_limit_unicode_digit_falls_back(db):
    db.add(models.SystemConfig(key=media_seek.CONFIG_DAILY_LIMIT, value="²"))
    db.commit()
    assert media_seek.daily_limit(db) == media_seek.DEFAULT_DAILY_LIMIT


def test_monthly_limit_unicode_digit_falls_back(db):
    db.add(models.SystemConfig(key=media_seek.CONFIG_MONTHLY_WELFARE, value="²"))
    db.commit()
    assert media_seek._monthly_limit_value(
        db, media_seek.CONFIG_MONTHLY_WELFARE,
        media_seek.DEFAULT_MONTHLY_WELFARE) == media_seek.DEFAULT_MONTHLY_WELFARE


# ==================== F5：LIKE 转义 ====================

def test_library_hit_like_wildcard_escaped(db):
    _item(db, name="普通电影", item_type="movie", guid="g-normal")
    # "%" 是通配符：转义后应按字面匹配，库里没有含 % 的名字 → 无命中
    assert media_seek.library_hit(db, name="%") is None
    assert media_seek.library_hit(db, name="_") is None
    # 正常片名仍可命中
    assert media_seek.library_hit(db, name="普通电影") is not None


# ==================== F6：死代码删除（行为不变） ====================

def test_normalize_season_negative_still_rejected():
    # "-1" 的 isdigit() 为 False，走友好报错分支（行为与删死代码前一致）
    with pytest.raises(ValueError, match="季要填数字"):
        media_seek.normalize_season("-1", series=True)
    assert media_seek.normalize_season("0", series=True) == "0"


# ==================== F7：degraded 标记 ====================

def test_search_candidates_degraded_on_kind_failure(db, monkeypatch):
    from backend.emby_server import tmdb
    monkeypatch.setattr(tmdb, "tmdb_client", FakeTmdb(
        hits={"movie": [{"id": 1, "title": "仅电影", "release_date": "2020-01-01",
                        "vote_average": 7.0, "poster_path": None}]},
        fail_kinds=("series",),
    ))
    result = media_seek.search_candidates(db, "仅")
    assert result["degraded"] is True
    assert [r["name"] for r in result["results"]] == ["仅电影"]

    monkeypatch.setattr(tmdb, "tmdb_client", FakeTmdb(fail_kinds=("movie", "series")))
    result = media_seek.search_candidates(db, "全挂")
    assert result == {"configured": True, "degraded": True, "results": []}


def test_search_candidates_degraded_false_on_success(db, monkeypatch):
    from backend.emby_server import tmdb
    monkeypatch.setattr(tmdb, "tmdb_client", FakeTmdb(hits={"movie": [], "series": []}))
    result = media_seek.search_candidates(db, "无结果")
    assert result["degraded"] is False and result["results"] == []


# ==================== F8：season_label 特别篇 ====================

def test_season_label_specials():
    assert media_seek.season_label("0") == "特别篇"
    assert media_seek.season_label("0,1") == "第 特别篇、1 季"
    assert media_seek.season_label("1,3") == "第 1、3 季"
    assert media_seek.season_label("all") == "全季"
    assert media_seek.season_label(None) == ""


# ==================== F9：单字中文片名 ====================

def test_library_hit_single_cjk_char(db):
    _item(db, name="她", item_type="movie", guid="g-her")
    hit = media_seek.library_hit(db, name="她", item_type="movie")
    assert hit is not None and hit.guid == "g-her"

    # 纯 ASCII 单字符仍拦截（防通配式全表扫）
    assert media_seek.library_hit(db, name="a") is None
    assert media_seek.library_hit(db, name="") is None
