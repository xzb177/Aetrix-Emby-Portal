"""EA 第三方播放器兼容缺口回归（2026-10-22 晚真实用户测试撞到的 404 批）

来源：~/workspace/reports/ea-player-compat-audit-20261022.md 的 G1–G8、G10。
当晚 Hills 等客户端在 EA 日志里真实撞到的缺口：

- G1：/Years、/Tags、/OfficialRatings 筛选分面列表端点缺失（各 404×7）
- G2：SortBy=PremiereDate 不支持、静默回退按名称排序
- G3：POST /Users/{id}/Items/{id}/HideFromResume 缺失（404×2）
- G4：/Users/{id}/Items/{id}/SpecialFeatures 带用户前缀形态缺失（404×7）
- G5：GET /MediaSegments/{id} 缺失（404×2）
- G6：GET /Items/{id}/ThumbnailSet 缺失（404×3）
- G7：弹幕插件路径 /api/danmu/{id}（+/raw）缺失（404×9）
- G8：双重前缀 /emby/emby/… 整串 404（×10）
- G10：GET /Branding/Logo 缺失（404×2）

口径与 test_emby_views_compat.py 一致：真实路由 + AuthenticateByName 真实
token；共享 sqlite 库按本次种子 Id 定点断言，不做全局计数。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
import xml.etree.ElementTree as ET
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import facets
from backend.security import hash_password

init_db()

PW = "ea-gaps-pw"
UNAME = "eagaps_alice"


def _guid():
    return uuid.uuid4().hex


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    old = db.query(models.WebUser).filter(
        models.WebUser.username == UNAME).first()
    if old:
        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == old.id).delete(synchronize_session=False)
        db.query(em.UserMediaData).filter(
            em.UserMediaData.user_id == old.id).delete(synchronize_session=False)
        db.query(models.WebUser).filter(models.WebUser.id == old.id).delete()
        db.commit()
    for stale in db.query(em.Library).filter(
            em.Library.name.like("缺口%库")).all():
        ids = [r[0] for r in db.query(em.MediaItem.id)
               .filter(em.MediaItem.library_id == stale.id).all()]
        if ids:
            db.query(em.IntroMarker).filter(
                em.IntroMarker.item_id.in_(ids)).delete(synchronize_session=False)
            db.query(em.UserMediaData).filter(
                em.UserMediaData.item_id.in_(ids)).delete(synchronize_session=False)
            db.query(em.ItemFacet).filter(
                em.ItemFacet.item_id.in_(ids)).delete(synchronize_session=False)
            db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
                synchronize_session=False)
        db.query(em.Library).filter(em.Library.id == stale.id).delete()
    db.commit()

    user = models.WebUser(
        username=UNAME, password_hash="x", is_active=True, is_staff=True,
        emby_username=UNAME, emby_password=hash_password(PW),
    )
    db.add(user)
    lib = em.Library(guid=_guid(), name="缺口测试库", collection_type="mixed",
                     is_enabled=True)
    db.add(lib)
    db.flush()

    def mk(item_type, name, **kw):
        it = em.MediaItem(guid=_guid(), library_id=lib.id,
                          item_type=item_type, name=name, **kw)
        db.add(it)
        db.flush()
        return it

    # 排序对照组：名字顺序与发行日期顺序刻意相反——若 PremiereDate 被静默
    # 回退成按名称排序，断言立刻露馅。
    movie_new = mk("movie", "ZZZ 新映电影", sort_name="ZZZ 新映电影",
                   file_path="/tmp/none/gaps-new.mp4", container="mp4",
                   production_year=2024, official_rating="PG-13",
                   genres="缺口动作", tags="缺口标签",
                   premiere_date=datetime(2024, 6, 1))
    movie_old = mk("movie", "AAA 旧映电影", sort_name="AAA 旧映电影",
                   file_path="/tmp/none/gaps-old.mp4", container="mp4",
                   production_year=1999, official_rating="R",
                   genres="缺口剧情", tags="缺口标签",
                   premiere_date=datetime(1999, 1, 15))
    series = mk("series", "缺口测试剧", sort_name="缺口测试剧",
                production_year=2023)
    season = mk("season", "Season 01", series_id=series.id, season_number=1)
    ep = mk("episode", "缺口测试剧 S01E01", series_id=series.id,
            parent_id=season.id, season_number=1, episode_number=1,
            file_path="/tmp/none/gaps-e01.mp4", container="mp4")
    marker = em.IntroMarker(item_id=ep.id, marker_type="intro",
                            start_ms=1000, end_ms=61000, source="manual")
    db.add(marker)
    db.commit()
    # facets 关联表就绪状态决定 _filters_payload 走索引还是文本列回退；
    # 文本列回退同样能拿到 tags/genres，这里只需保证 Items/Filters 可用。
    facets.ensure_ready(db)
    data = {
        "user_id": user.id, "lib": lib.guid,
        "movie_new": movie_new.guid, "movie_old": movie_old.guid,
        "series": series.guid, "ep": ep.guid, "ep_id": ep.id,
        "movie_new_id": movie_new.id, "movie_old_id": movie_old.id,
    }
    db.close()
    yield data
    try:
        db = SessionLocal()
        row = db.query(em.Library).filter(em.Library.guid == data["lib"]).first()
        if row is not None:
            ids = [r[0] for r in db.query(em.MediaItem.id)
                   .filter(em.MediaItem.library_id == row.id).all()]
            if ids:
                db.query(em.IntroMarker).filter(
                    em.IntroMarker.item_id.in_(ids)).delete(synchronize_session=False)
                db.query(em.UserMediaData).filter(
                    em.UserMediaData.item_id.in_(ids)).delete(synchronize_session=False)
                db.query(em.ItemFacet).filter(
                    em.ItemFacet.item_id.in_(ids)).delete(synchronize_session=False)
                db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
                    synchronize_session=False)
            db.query(em.Library).filter(em.Library.id == row.id).delete()
        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == data["user_id"]).delete(
            synchronize_session=False)
        db.query(em.UserMediaData).filter(
            em.UserMediaData.user_id == data["user_id"]).delete(
            synchronize_session=False)
        db.query(models.WebUser).filter(
            models.WebUser.id == data["user_id"]).delete()
        db.commit()
        db.close()
    except Exception:  # noqa: BLE001 — 清理失败不掩盖测试本体
        db.rollback()
        db.close()


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app)


@pytest.fixture(scope="module")
def token(client, seed):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": UNAME, "Pw": PW},
        headers={"X-Emby-Authorization":
                 'MediaBrowser Client="GapTest", Device="iOS", '
                 'DeviceId="gap-dev-1", Version="1.0"'},
    )
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


def _h(token):
    return {"X-Emby-Token": token}


def _facet_names(client, token, path):
    r = client.get(path, headers=_h(token))
    assert r.status_code == 200, f"{path}: {r.text}"
    body = r.json()
    assert set(body) >= {"Items", "TotalRecordCount", "StartIndex"}
    return body["Items"]


# ---------- G1：筛选分面列表端点 ----------

def test_years_endpoint_lists_seed_years(client, token, seed):
    for path in ("/emby/Years", "/Years"):
        items = _facet_names(client, token, path)
        hit = [i for i in items if i["Name"] == "2024"]
        assert hit, f"{path} 未包含种子年份 2024：{[i['Name'] for i in items]}"
        assert hit[0]["Type"] == "Year"
        assert hit[0]["ServerId"]
        assert hit[0]["Id"]


def test_tags_endpoint_lists_seed_tags(client, token, seed):
    items = _facet_names(client, token, "/emby/Tags")
    hit = [i for i in items if i["Name"] == "缺口标签"]
    assert hit, f"未包含种子标签：{[i['Name'] for i in items]}"
    assert hit[0]["Type"] == "Tag"
    assert hit[0]["ServerId"]


def test_official_ratings_endpoint_lists_seed_ratings(client, token, seed):
    items = _facet_names(client, token, "/emby/OfficialRatings")
    hit = [i for i in items if i["Name"] == "PG-13"]
    assert hit, f"未包含种子分级：{[i['Name'] for i in items]}"
    assert hit[0]["Type"] == "OfficialRating"
    assert hit[0]["ServerId"]


# ---------- G2：SortBy=PremiereDate 真生效 ----------

def _items_sorted(client, token, seed, sort_order):
    r = client.get(
        f"/emby/Users/{seed['user_id']}/Items",
        params={"ParentId": seed["lib"], "IncludeItemTypes": "movie",
                "SortBy": "PremiereDate", "SortOrder": sort_order,
                "Recursive": "true"},
        headers=_h(token),
    )
    assert r.status_code == 200, r.text
    ids = [i["Id"] for i in r.json()["Items"]
           if i["Id"] in (seed["movie_new"], seed["movie_old"])]
    assert len(ids) == 2, f"种子电影未齐：{ids}"
    return ids


def test_sort_by_premiere_date_desc(client, token, seed):
    ids = _items_sorted(client, token, seed, "Descending")
    assert ids == [seed["movie_new"], seed["movie_old"]], (
        f"PremiereDate 降序不对（疑似回退名称排序）：{ids}")


def test_sort_by_premiere_date_asc(client, token, seed):
    ids = _items_sorted(client, token, seed, "Ascending")
    assert ids == [seed["movie_old"], seed["movie_new"]], (
        f"PremiereDate 升序不对（疑似回退名称排序）：{ids}")


# ---------- G3：HideFromResume ----------

def _seed_progress(seed, item_db_id):
    db = SessionLocal()
    db.query(em.UserMediaData).filter(
        em.UserMediaData.user_id == seed["user_id"],
        em.UserMediaData.item_id == item_db_id).delete(synchronize_session=False)
    db.add(em.UserMediaData(
        user_id=seed["user_id"], item_id=item_db_id,
        playback_position_ticks=123_000_000, played=False,
        last_played_at=datetime.now()))
    db.commit()
    db.close()


def _resume_ids(client, token, seed):
    r = client.get(f"/emby/Users/{seed['user_id']}/Items/Resume",
                   headers=_h(token))
    assert r.status_code == 200, r.text
    return [i["Id"] for i in r.json()["Items"]]


def test_hide_from_resume_movie(client, token, seed):
    _seed_progress(seed, seed["movie_new_id"])
    assert seed["movie_new"] in _resume_ids(client, token, seed)
    r = client.post(
        f"/emby/Users/{seed['user_id']}/Items/{seed['movie_new']}/HideFromResume",
        headers=_h(token))
    assert r.status_code == 200, r.text
    assert seed["movie_new"] not in _resume_ids(client, token, seed)
    # 标记落库且进度本身没被清掉（隐藏只是不展示）
    db = SessionLocal()
    umd = db.query(em.UserMediaData).filter(
        em.UserMediaData.user_id == seed["user_id"],
        em.UserMediaData.item_id == seed["movie_new_id"]).first()
    db.close()
    assert umd is not None and umd.hide_from_resume is True
    assert umd.playback_position_ticks == 123_000_000


def test_hide_from_resume_series_hides_folded_episode(client, token, seed):
    # 进度在单集行上，Resume 折叠成剧集卡片；对剧集 guid 点隐藏必须生效
    _seed_progress(seed, seed["ep_id"])
    assert seed["series"] in _resume_ids(client, token, seed)
    r = client.post(
        f"/emby/Users/{seed['user_id']}/Items/{seed['series']}/HideFromResume",
        headers=_h(token))
    assert r.status_code == 200, r.text
    assert seed["series"] not in _resume_ids(client, token, seed)


# ---------- G4：SpecialFeatures 带用户前缀形态 ----------

def test_special_features_user_prefixed_paths(client, token, seed):
    for path in (
        f"/emby/Users/{seed['user_id']}/Items/{seed['movie_new']}/SpecialFeatures",
        f"/Users/{seed['user_id']}/Items/{seed['movie_new']}/SpecialFeatures",
    ):
        r = client.get(path, headers=_h(token))
        assert r.status_code == 200, f"{path}: {r.text}"
        assert r.json()["Items"] == []


# ---------- G5：MediaSegments ----------

def test_media_segments_maps_intro_markers(client, token, seed):
    r = client.get(f"/emby/MediaSegments/{seed['ep']}", headers=_h(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["TotalRecordCount"] == 1
    seg = body["Items"][0]
    assert seg["ItemId"] == seed["ep"]
    assert seg["Type"] == "Intro"
    assert seg["StartTicks"] == 1000 * 10000
    assert seg["EndTicks"] == 61000 * 10000


def test_media_segments_empty_when_no_markers(client, token, seed):
    r = client.get(f"/emby/MediaSegments/{seed['movie_new']}",
                   headers=_h(token))
    assert r.status_code == 200, r.text
    assert r.json()["Items"] == []
    assert r.json()["TotalRecordCount"] == 0


# ---------- G6：ThumbnailSet 空集 ----------

def test_thumbnail_set_returns_empty_set(client, token, seed):
    r = client.get(f"/emby/Items/{seed['movie_new']}/ThumbnailSet",
                   headers=_h(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["Items"] == []
    assert body["TotalRecordCount"] == 0


# ---------- G7：弹幕空兼容 ----------

def test_danmu_endpoints_return_empty_xml(client, token, seed):
    for path in (f"/emby/api/danmu/{seed['movie_new']}",
                 f"/emby/api/danmu/{seed['movie_new']}/raw"):
        r = client.get(path, headers=_h(token))
        assert r.status_code == 200, f"{path}: {r.text}"
        root = ET.fromstring(r.text)
        assert root.tag == "i"
        assert root.findall("d") == []


# ---------- G10：Branding/Logo ----------

def test_branding_logo_returns_image(client, token, seed):
    r = client.get("/emby/Branding/Logo", headers=_h(token))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/")
    assert len(r.content) > 0


# ---------- G8：双重 /emby 前缀归一化 ----------

def test_double_prefix_middleware_collapses_paths():
    from emby_api.asgi_middleware import DoubleEmbyPrefixMiddleware

    seen = {}

    async def echo_app(scope, receive, send):
        seen["path"] = scope.get("path")
        from starlette.responses import PlainTextResponse
        await PlainTextResponse("ok")(scope, receive, send)

    client = TestClient(DoubleEmbyPrefixMiddleware(echo_app))
    client.get("/emby/emby/System/Ping")
    assert seen["path"] == "/emby/System/Ping"
    client.get("/emby/emby/emby/Years")
    assert seen["path"] == "/emby/Years"
    client.get("/emby/System/Ping")
    assert seen["path"] == "/emby/System/Ping"
    client.get("/api/user/x")
    assert seen["path"] == "/api/user/x"


def test_double_prefix_reaches_ea_routes():
    from emby_api.main import app

    ea_client = TestClient(app)
    r = ea_client.get("/emby/emby/System/Ping")
    assert r.status_code == 200, r.text
    assert "Emby Server" in r.text
    r = ea_client.get("/emby/emby/emby/System/Info/Public")
    assert r.status_code == 200, r.text
    assert r.json()["ProductName"] == "Emby Server"
