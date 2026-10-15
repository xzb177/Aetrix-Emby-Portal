"""求片中心核心闭环（backend/media_seek.py + 两端落库函数）单元测试

覆盖需求五条对应的实现面：

1. **额度走配置**：SystemConfig ``media_seek_daily_limit`` 覆盖默认值，脏值回退，
   自愈注册表里也确实有这一个键（新库开箱就有可编辑的行）；
2. **剧集按整季申请**：季的校验 / 归一化 / 展示文案，脏输入抛错，电影忽略该字段；
3. **TMDB 候选搜索**：未配置 TMDB 时诚实降级；已在库的候选标出来并沉底；
   季列表查得到就用、查不到回空表（前端退回手填季）；
4. **提交落库**：同名同季去重、跨季放行，每日额度 429，tmdb_id 只收数字；
5. **管理端**：拒绝必须写理由；「标记已入库」先核验片真的在库里（409 → force 兜底），
   审计落 AdminLog。

全部用隔离的内存 SQLite，不碰网络（TMDB 客户端用假替身）、不碰生产库。
"""
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import media_seek, models
from backend.api import admin as admin_api
from backend.api import user as user_api
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


# ==================== 工具 ====================

def _user(db, username="seeker"):
    user = models.WebUser(username=username, password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _request(db, user, *, name="星际穿越", status="pending", season=None,
             tmdb_id=None, mtype="movie", created_at=None):
    row = models.MovieRequest(
        user_id=user.id, movie_name=name, type=mtype, status=status,
        season=season, tmdb_id=tmdb_id,
    )
    if created_at is not None:
        row.created_at = created_at
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _media_item(db, *, name="星际穿越", tmdb_id=None, item_type="movie", guid=None):
    lib = em.Library(guid=guid or f"lib-{name}", name="测试库", collection_type="movies", paths="")
    db.add(lib)
    db.flush()
    item = em.MediaItem(
        guid=guid or f"item-{name}", library_id=lib.id, item_type=item_type,
        name=name, tmdb_id=tmdb_id, file_path=f"/media/{name}.mkv",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


class FakeTmdb:
    """TMDB 客户端替身：只提供求片链路用到的两个方法"""

    def __init__(self, *, configured=True, hits=None, details=None):
        self.configured = configured
        self._hits = hits or {}
        self._details = details or {}

    def search_candidates(self, name, kind, limit=6):
        return list(self._hits.get(kind, []))[:limit]

    def details(self, tmdb_id, kind):
        return self._details.get(str(tmdb_id))


MOVIE_HIT = {
    "id": 157336, "title": "星际穿越", "release_date": "2014-11-05",
    "poster_path": "/cover.jpg", "vote_average": 8.4, "overview": "穿越虫洞的故事",
}
TV_HIT = {
    "id": 1396, "name": "绝命毒师", "first_air_date": "2008-01-20",
    "poster_path": None, "vote_average": 8.9, "overview": "化学老师",
}


# ==================== 1. 额度走配置 ====================

def test_daily_limit_default_and_override(db):
    assert media_seek.daily_limit(db) == media_seek.DEFAULT_DAILY_LIMIT

    db.add(models.SystemConfig(key=media_seek.CONFIG_DAILY_LIMIT, value="9"))
    db.commit()
    assert media_seek.daily_limit(db) == 9

    cfg = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == media_seek.CONFIG_DAILY_LIMIT
    ).first()
    cfg.value = "abc"
    db.commit()
    assert media_seek.daily_limit(db) == media_seek.DEFAULT_DAILY_LIMIT, "脏值要回默认值"

    cfg.value = "0"
    db.commit()
    assert media_seek.daily_limit(db) == 1, "0 / 负数至少留 1 条，不能把求片锁死"


def test_quota_counts_today_including_withdrawn(db):
    user = _user(db)
    _request(db, user, status="withdrawn")
    old = _request(db, user, name="老片", status="completed")
    old.created_at = datetime.now() - timedelta(days=2)
    db.commit()

    quota = media_seek.quota(db, user.id)
    assert quota["used_today"] == 1, "撤回不退还额度（否则可以无限刷新）"
    assert quota["daily_limit"] == media_seek.DEFAULT_DAILY_LIMIT
    assert quota["remaining"] == media_seek.DEFAULT_DAILY_LIMIT - 1


def test_quota_remaining_never_negative(db):
    user = _user(db)
    cfg = models.SystemConfig(key=media_seek.CONFIG_DAILY_LIMIT, value="1")
    db.add(cfg)
    db.commit()
    _request(db, user)
    _request(db, user, name="第二部")
    assert media_seek.quota(db, user.id)["remaining"] == 0


def test_quota_key_registered_in_self_heal():
    from backend import config_self_heal

    defaults = {key: value for key, value, _ in config_self_heal.collect_system_config_defaults()}
    assert defaults[media_seek.CONFIG_DAILY_LIMIT] == str(media_seek.DEFAULT_DAILY_LIMIT)


# ==================== 1b. 月度额度（公益/付费区分） ====================

def _welfare_user(db, username="welfare_seeker", expired=False):
    user = _user(db, username=username)
    user.is_welfare = True
    user.welfare_expires_at = (
        datetime.now() - timedelta(days=1) if expired else datetime.now() + timedelta(days=30)
    )
    db.commit()
    db.refresh(user)
    return user


def test_monthly_quota_defaults_by_user_kind(db):
    welfare = _welfare_user(db)
    paid = _user(db, username="paid_seeker")

    wq = media_seek.monthly_quota(db, welfare)
    assert wq["kind"] == "welfare"
    assert wq["monthly_limit"] == media_seek.DEFAULT_MONTHLY_WELFARE == 3
    assert wq["monthly_used"] == 0
    assert wq["monthly_remaining"] == 3

    pq = media_seek.monthly_quota(db, paid)
    assert pq["kind"] == "paid"
    assert pq["monthly_limit"] == media_seek.DEFAULT_MONTHLY_PAID == 10
    assert pq["monthly_remaining"] == 10


def test_monthly_quota_config_override_and_dirty_fallback(db):
    welfare = _welfare_user(db)
    db.add(models.SystemConfig(key=media_seek.CONFIG_MONTHLY_WELFARE, value="7"))
    db.commit()
    assert media_seek.monthly_limit(db, welfare) == 7

    cfg = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == media_seek.CONFIG_MONTHLY_WELFARE
    ).first()
    cfg.value = "not-a-number"
    db.commit()
    assert media_seek.monthly_limit(db, welfare) == media_seek.DEFAULT_MONTHLY_WELFARE, "脏值要回默认值"


def test_monthly_quota_counts_this_month_only(db):
    user = _user(db)
    _request(db, user, status="withdrawn")  # 撤回也计入（与每日口径一致）
    old = _request(db, user, name="上个月的片", status="completed")
    old.created_at = datetime.now() - timedelta(days=40)
    db.commit()

    mq = media_seek.monthly_quota(db, user)
    assert mq["monthly_used"] == 1, "只统计本月，上个月的不算"
    assert mq["monthly_remaining"] == media_seek.DEFAULT_MONTHLY_PAID - 1


def test_monthly_quota_remaining_never_negative(db):
    welfare = _welfare_user(db)
    for i in range(5):
        _request(db, welfare, name=f"片{i}")
    mq = media_seek.monthly_quota(db, welfare)
    assert mq["monthly_used"] == 5
    assert mq["monthly_remaining"] == 0, "剩余不为负"


def test_expired_welfare_counts_as_paid(db):
    user = _welfare_user(db, expired=True)
    mq = media_seek.monthly_quota(db, user)
    assert mq["kind"] == "paid", "公益过期后按付费额度算"
    assert mq["monthly_limit"] == media_seek.DEFAULT_MONTHLY_PAID


# ==================== 2. 剧集按整季申请 ====================

@pytest.mark.parametrize("raw,expected", [
    ("", "all"),
    ("all", "all"),
    ("全季", "all"),
    ("全部", "all"),
    ("2", "2"),
    (" 1、3 ", "1,3"),
    ("1,1,2", "1,2"),
    ("0", "0"),          # 0 = 特别篇
])
def test_normalize_season_series(raw, expected):
    assert media_seek.normalize_season(raw, series=True) == expected


@pytest.mark.parametrize("raw", ["第一季", "abc", "1,a", "100"])
def test_normalize_season_rejects_garbage(raw):
    with pytest.raises(ValueError):
        media_seek.normalize_season(raw, series=True)


def test_normalize_season_many_seasons_rejected():
    with pytest.raises(ValueError):
        media_seek.normalize_season(",".join(str(i) for i in range(40)), series=True)


def test_normalize_season_movie_ignored():
    assert media_seek.normalize_season("3", series=False) == ""
    assert media_seek.normalize_season(None, series=False) == ""


def test_season_label():
    assert media_seek.season_label("all") == "全季"
    assert media_seek.season_label("1,3") == "第 1、3 季"
    assert media_seek.season_label(None) == ""


def test_normalize_tmdb_id():
    assert media_seek.normalize_tmdb_id("157336") == "157336"
    assert media_seek.normalize_tmdb_id(" 42 ") == "42"
    assert media_seek.normalize_tmdb_id("tt157336") == ""
    assert media_seek.normalize_tmdb_id("1234567890123") == ""
    assert media_seek.normalize_tmdb_id(None) == ""


# ==================== 3. TMDB 候选搜索 ====================

def test_search_candidates_marks_in_library_and_sinks_it(db, monkeypatch):
    from backend.emby_server import tmdb

    _media_item(db, name="星际穿越", tmdb_id="157336")
    monkeypatch.setattr(tmdb, "tmdb_client", FakeTmdb(hits={
        "movie": [
            {"id": 157336, "title": "星际穿越", "release_date": "2014-11-05",
             "vote_average": 8.4, "poster_path": "/cover.jpg"},
            {"id": 27205, "title": "盗梦空间", "release_date": "2010-07-16",
             "vote_average": 8.4, "poster_path": "/inception.jpg"},
        ],
        "series": [],
    }))

    result = media_seek.search_candidates(db, "星际")
    assert result["configured"] is True
    names = [r["name"] for r in result["results"]]
    assert names == ["盗梦空间", "星际穿越"], "已在库的要沉到底部"
    owned = result["results"][1]
    assert owned["in_library"] is True and owned["library_item_id"] == "item-星际穿越"
    fresh = result["results"][0]
    assert fresh["in_library"] is False
    assert fresh["year"] == "2010" and fresh["kind"] == "movie"
    assert fresh["poster_url"] == "https://image.tmdb.org/t/p/w300/inception.jpg"


def test_search_candidates_unconfigured_is_honest(db, monkeypatch):
    from backend.emby_server import tmdb

    monkeypatch.setattr(tmdb, "tmdb_client", FakeTmdb(configured=False))
    result = media_seek.search_candidates(db, "任何")
    assert result == {"configured": False, "degraded": False, "results": []}


def test_search_candidates_empty_query(db, monkeypatch):
    from backend.emby_server import tmdb

    monkeypatch.setattr(tmdb, "tmdb_client", FakeTmdb(hits={}))
    result = media_seek.search_candidates(db, "  ")
    assert result["configured"] is True and result["results"] == []


def test_seasons_for_returns_sorted_list(db, monkeypatch):
    from backend.emby_server import tmdb

    monkeypatch.setattr(tmdb, "tmdb_client", FakeTmdb(details={"1396": {
        "seasons": [
            {"season_number": 2, "name": "第 2 季", "episode_count": 13, "air_date": "2009-03-08"},
            {"season_number": 0, "name": "特别篇", "episode_count": 5, "air_date": ""},
            {"season_number": 1, "name": "第 1 季", "episode_count": 7, "air_date": "2008-01-20"},
        ],
    }}))
    seasons = media_seek.seasons_for("1396")
    assert [s["season_number"] for s in seasons] == [0, 1, 2]
    assert seasons[1]["episode_count"] == 7


def test_seasons_for_unconfigured_or_bad_id(db, monkeypatch):
    from backend.emby_server import tmdb

    monkeypatch.setattr(tmdb, "tmdb_client", FakeTmdb(configured=False))
    assert media_seek.seasons_for("1396") == []
    monkeypatch.setattr(tmdb, "tmdb_client", FakeTmdb(details={}))
    assert media_seek.seasons_for("not-a-id") == []


# ==================== 3b. 库内匹配 ====================

def test_library_hit_by_tmdb_id_then_name(db):
    _media_item(db, name="星际穿越（重制）", tmdb_id="157336")
    by_id = media_seek.library_hit(db, tmdb_id="157336")
    assert by_id is not None and by_id.name == "星际穿越（重制）"

    by_name = media_seek.library_hit(db, name="星际", item_type="movie")
    assert by_name is not None

    assert media_seek.library_hit(db, tmdb_id="999999", name="不存在的片") is None


def test_find_library_match_uses_request_fields(db):
    row = _request(db, _user(db), name="绝命毒师", tmdb_id="1396", mtype="series")
    assert media_seek.find_library_match(db, row) is None

    item = _media_item(db, name="绝命毒师", tmdb_id="1396", item_type="series", guid="guid-bb")
    match = media_seek.find_library_match(db, row)
    assert match is not None and match.guid == item.guid


# ==================== 4. 提交落库 ====================

def _payload(**overrides):
    data = {"movie_name": "星际穿越"}
    data.update(overrides)
    return user_api.MediaSeekRequest(**data)


def test_create_movie_ignores_season_and_stores_tmdb(db):
    user = _user(db)
    rid = user_api._create_media_seek_sync(
        db, user, "星际穿越", _payload(tmdb_id="157336", season="2", type="movie"),
    )
    row = db.query(models.MovieRequest).filter(models.MovieRequest.id == rid).first()
    assert row.season is None, "电影不记季"
    assert row.tmdb_id == "157336"
    assert row.status == "pending"


def test_create_series_defaults_to_all_seasons(db):
    user = _user(db)
    rid = user_api._create_media_seek_sync(
        db, user, "绝命毒师", _payload(movie_name="绝命毒师", type="series", season=""),
    )
    row = db.query(models.MovieRequest).filter(models.MovieRequest.id == rid).first()
    assert row.season == media_seek.SEASON_ALL, "剧集不填季 = 全季（显式入库）"


def test_create_series_bad_season_rejected(db):
    user = _user(db)
    with pytest.raises(HTTPException) as exc:
        user_api._create_media_seek_sync(
            db, user, "绝命毒师", _payload(movie_name="绝命毒师", type="series", season="第一季"),
        )
    assert exc.value.status_code == 400


def test_create_dedupe_same_season_but_allows_other_season(db):
    user = _user(db)
    _request(db, user, name="绝命毒师", season="1", mtype="series")
    # 同一季重复提交 → 409
    with pytest.raises(HTTPException) as exc:
        user_api._create_media_seek_sync(
            db, user, "绝命毒师", _payload(movie_name="绝命毒师", type="series", season="1"),
        )
    assert exc.value.status_code == 409
    # 另一季 → 放行（S1 在排队不影响 S2 也来求）
    rid = user_api._create_media_seek_sync(
        db, user, "绝命毒师", _payload(movie_name="绝命毒师", type="series", season="2"),
    )
    row = db.query(models.MovieRequest).filter(models.MovieRequest.id == rid).first()
    assert row.season == "2"


def test_create_enforces_daily_limit_from_config(db):
    user = _user(db)
    db.add(models.SystemConfig(key=media_seek.CONFIG_DAILY_LIMIT, value="2"))
    db.commit()
    _request(db, user, name="片一")
    _request(db, user, name="片二")

    with pytest.raises(HTTPException) as exc:
        user_api._create_media_seek_sync(db, user, "片三", _payload(movie_name="片三"))
    assert exc.value.status_code == 429
    assert "2" in str(exc.value.detail)


# ==================== 5. 管理端审核 / 标记已入库 ====================

def test_review_rejected_requires_reason(db):
    admin = _user(db, username="admin")
    row = _request(db, _user(db, username="user-a"))

    with pytest.raises(HTTPException) as exc:
        admin_api._review_media_seek_sync(db, admin.id, row.id, "rejected", "")
    assert exc.value.status_code == 400

    # 理由填了 → 落库 + 审计 + 用户能看到
    info = admin_api._review_media_seek_sync(db, admin.id, row.id, "rejected", "画质不达标")
    db.refresh(row)
    assert row.status == "rejected" and row.admin_note == "画质不达标"
    assert info["movie_name"] == "星际穿越"
    log = db.query(models.AdminLog).filter(
        models.AdminLog.action == "update_media_seek"
    ).first()
    assert log is not None and log.target_id == row.id


def test_review_only_accepts_approve_or_reject(db):
    admin = _user(db, username="admin")
    row = _request(db, _user(db, username="user-a"))
    with pytest.raises(HTTPException) as exc:
        admin_api._review_media_seek_sync(db, admin.id, row.id, "completed", "done")
    assert exc.value.status_code == 400, "已入库要走 mark-in-library（会核验库存）"


def test_mark_in_library_requires_actual_library_hit(db):
    admin = _user(db, username="admin")
    row = _request(db, _user(db, username="user-b"), name="绝命毒师", tmdb_id="1396", mtype="series")

    with pytest.raises(HTTPException) as exc:
        admin_api._mark_media_seek_sync(db, admin.id, row.id, False)
    assert exc.value.status_code == 409, "库里没有 → 不能标记已入库"

    item = _media_item(db, name="绝命毒师", tmdb_id="1396", item_type="series", guid="guid-bb")
    info = admin_api._mark_media_seek_sync(db, admin.id, row.id, False)
    db.refresh(row)
    assert info["matched"] is True
    assert row.status == "completed"
    assert row.emby_item_id == item.guid, "记下条目 guid，用户端可以直接去观看"
    log = db.query(models.AdminLog).filter(
        models.AdminLog.action == "mark_media_seek_in_library"
    ).first()
    assert log is not None and log.target_id == row.id


def test_mark_in_library_force_and_double_mark(db):
    admin = _user(db, username="admin")
    row = _request(db, _user(db, username="user-c"), name="找不到的片")

    info = admin_api._mark_media_seek_sync(db, admin.id, row.id, True)
    db.refresh(row)
    assert info["matched"] is False
    assert row.status == "completed" and row.emby_item_id is None

    # 已完成的不许重复标记
    with pytest.raises(HTTPException) as exc:
        admin_api._mark_media_seek_sync(db, admin.id, row.id, True)
    assert exc.value.status_code == 400


def test_mark_in_library_missing_request_404(db):
    with pytest.raises(HTTPException) as exc:
        admin_api._mark_media_seek_sync(db, 1, 999999, False)
    assert exc.value.status_code == 404
