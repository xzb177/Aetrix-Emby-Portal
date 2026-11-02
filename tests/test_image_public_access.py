"""条目图片端点：必须鉴权 + 按可见范围出图（PA-036 口径翻转）

历史口径（H2 时期）：图片端点公开、不校验任何凭据，守卫测试钉住
「谁给它加 token 校验谁就会把首页海报墙打成占位卡」。

2026-11-02 口径翻转（完美审计 R17 PA-036，用户亲点「字面修法」定夺）：
EA 图片端点未鉴权 + 不做可见范围校验，任何人凭 guid 即可取海报 / 背景 /
库封面（guid 可由路径推算、并非秘密）。用户在知晓「Emby token 将进入图片
URL（进反代 / CDN 日志、浏览器历史与 Referer）」的前提下选择字面修法：

- ``item_image`` / ``item_image_index`` 加 ``Depends(get_emby_user)``，
  条目按 ``_require_visible_item`` 同口径校验可见范围（受限库 403）；
- 库封面分支按 ``_library_scope`` 校验库可见性；
- ``person_image`` 至少过 ``get_emby_user``；
- 前端 ``imageUrl`` 等拼装点给图片 URL 拼 ``api_key``（账号卡下发专用
  web token，见 ``rotate_web_portal_token``）；
- 远程代理图与本地图的 ``Cache-Control`` 由 public 改 private，
  防止共享缓存把受限图发给他人。

代价（如实记录）：登录页海报拼贴（未登录、无凭据）在新口径下取不到图，
前端已有的「加载失败即隐藏 + 静态背景兜底」会接住，登录页退化为静态背景。

覆盖：无凭据 401 / 坏凭据 401 / api_key 与 X-Emby-Token 两形态 200 /
集→剧回退 / maxWidth 缩略图 / 受限库条目 403 / 库封面可见 200、受限 403 /
虚拟库派生封面（借可见成员 200、库本身不可见 403、借受限成员 403）/
演员头像（401、过鉴权后 404、本地化后 200）/ 未知条目带凭据 404 /
账号卡 emby_api_key 下发与轮换。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import json
import uuid

import pytest

PIL = pytest.importorskip("PIL")

from fastapi.testclient import TestClient  # noqa: E402

from backend import models  # noqa: E402
from backend.database import SessionLocal, init_db  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.security import hash_password  # noqa: E402

init_db()

PW = "pa36-image-pw"
_SUFFIX = uuid.uuid4().hex[:8]
UNAME = f"pa36img_{_SUFFIX}"


def _guid():
    return uuid.uuid4().hex


@pytest.fixture(scope="module")
def client():
    from emby_api.main import app

    # 不进 lifespan：只测路由本身（启动钩子要 SECRET_KEY / Redis 等运行期依赖）
    return TestClient(app)


@pytest.fixture(scope="module")
def portal_client():
    from backend.main import app

    return TestClient(app)


@pytest.fixture(scope="module")
def seed(tmp_path_factory):
    from PIL import Image

    tmp = tmp_path_factory.mktemp("pa36img")

    def _poster(name, rgb):
        p = tmp / f"{name}_{_guid()}.jpg"
        Image.new("RGB", (800, 1200), rgb).save(p, "JPEG")
        return str(p)

    poster_a = _poster("a", (180, 40, 40))
    backdrop_a = _poster("ab", (40, 90, 160))
    poster_b = _poster("b", (30, 140, 60))
    cover_a = _poster("ca", (200, 160, 30))
    cover_b = _poster("cb", (90, 30, 140))

    db = SessionLocal()
    try:
        user = models.WebUser(
            username=UNAME, password_hash="x", is_active=True, is_staff=False,
            emby_username=UNAME, emby_password=hash_password(PW))
        db.add(user)
        db.flush()

        # 付费服口径下账号卡要有效订阅才算 granted（emby_api_key 才下发）；
        # 订阅判定按 realm_id 精确匹配目标服，NULL 不算，必须挂活动服
        from datetime import datetime, timedelta

        from backend import realms as _realms_mod

        plan = models.SubscriptionPlan(name=f"PA36套餐{_SUFFIX}", price=0,
                                       duration_days=30)
        db.add(plan)
        db.flush()
        db.add(models.UserSubscription(
            user_id=user.id, plan_id=plan.id,
            realm_id=_realms_mod.active_realm_id(db),
            start_date=datetime.now(),
            end_date=datetime.now() + timedelta(days=30), status="active"))
        db.flush()

        lib_a = em.Library(guid=_guid(), name=f"图片A-{_SUFFIX}",
                           collection_type="tvshows", is_enabled=True,
                           cover_path=cover_a)
        lib_b = em.Library(guid=_guid(), name=f"图片B-{_SUFFIX}",
                           collection_type="movies", is_enabled=True,
                           cover_path=cover_b)
        db.add_all([lib_a, lib_b])
        db.flush()

        series = em.MediaItem(guid=_guid(), library_id=lib_a.id,
                              item_type="series", name="沧元图",
                              poster_path=poster_a, backdrop_path=backdrop_a)
        db.add(series)
        db.flush()
        season = em.MediaItem(guid=_guid(), library_id=lib_a.id,
                              item_type="season", name="第 1 季",
                              series_id=series.id, parent_id=series.id,
                              season_number=1)
        db.add(season)
        db.flush()
        episode = em.MediaItem(guid=_guid(), library_id=lib_a.id,
                               item_type="episode", name="沧元图 S01E58",
                               series_id=series.id, parent_id=season.id,
                               season_number=1, episode_number=58)
        movie_b = em.MediaItem(guid=_guid(), library_id=lib_b.id,
                               item_type="movie", name="受限电影",
                               poster_path=poster_b)
        # 平台串带本轮后缀：共享测试库里上一轮播种的残留行不能串进本轮的
        # 虚拟库成员判定（代表按身份键取最低 id，撞车会借到别人库的成员）
        plat_in = f"Pa36Flix{_SUFFIX}"
        plat_other = f"Pa36Other{_SUFFIX}"
        plat_hidden = f"Pa36Hidden{_SUFFIX}"
        member_vis = em.MediaItem(guid=_guid(), library_id=lib_a.id,
                                  item_type="movie", name="可见成员",
                                  poster_path=poster_a, platforms=plat_in,
                                  tmdb_id="111")
        member_other = em.MediaItem(guid=_guid(), library_id=lib_a.id,
                                    item_type="movie", name="别台成员",
                                    poster_path=poster_a, platforms=plat_other,
                                    tmdb_id="222")
        member_hidden = em.MediaItem(guid=_guid(), library_id=lib_b.id,
                                     item_type="movie", name="受限成员",
                                     poster_path=poster_b,
                                     platforms=plat_hidden, tmdb_id="333")
        db.add_all([episode, movie_b, member_vis, member_other, member_hidden])
        db.flush()

        vlib_in = em.Library(guid=_guid(), name=f"虚拟可见-{_SUFFIX}",
                             collection_type="movies", is_enabled=True,
                             is_virtual=True, platform=plat_in)
        vlib_out = em.Library(guid=_guid(), name=f"虚拟不可见-{_SUFFIX}",
                              collection_type="movies", is_enabled=True,
                              is_virtual=True, platform=plat_other)
        vlib_hidden = em.Library(guid=_guid(), name=f"虚拟借受限-{_SUFFIX}",
                                 collection_type="movies", is_enabled=True,
                                 is_virtual=True, platform=plat_hidden)
        db.add_all([vlib_in, vlib_out, vlib_hidden])
        db.flush()

        person_empty = em.EmbyPerson(item_id=series.id, name=f"无图演员{_SUFFIX}",
                                     image="")
        person_img = em.EmbyPerson(item_id=series.id, name=f"有图演员{_SUFFIX}",
                                   image="https://img.example/pa36-a.jpg")
        db.add_all([person_empty, person_img])
        db.flush()

        # 可见范围：只放 lib_a + 两个虚拟库（vlib_out 与 lib_b 不可见）
        from backend import library_scope
        from backend.integrations import store

        row = db.query(models.SystemConfig).filter(
            models.SystemConfig.key == library_scope.CONFIG_USER_OVERRIDES).first()
        old_value = row.value if row else None
        overrides = {}
        if old_value:
            try:
                overrides = json.loads(old_value)
            except ValueError:
                overrides = {}
        overrides[str(user.id)] = {
            "enabled": True,
            "libraries": [lib_a.id, vlib_in.id, vlib_hidden.id],
        }
        if row:
            row.value = json.dumps(overrides)
        else:
            db.add(models.SystemConfig(
                key=library_scope.CONFIG_USER_OVERRIDES,
                value=json.dumps(overrides)))
        db.commit()
        store.invalidate()

        data = {
            "user_id": user.id,
            "series": series.guid, "episode": episode.guid,
            "movie_b": movie_b.guid,
            "lib_a": lib_a.guid, "lib_b": lib_b.guid,
            "vlib_in": vlib_in.guid, "vlib_out": vlib_out.guid,
            "vlib_hidden": vlib_hidden.guid,
            "person_empty": person_empty.name, "person_img": person_img.name,
            "person_img_id": person_img.id,
            "avatar_file": poster_a,
        }
        yield data

        # 还原全局覆盖配置，避免污染同进程后续测试
        row = db.query(models.SystemConfig).filter(
            models.SystemConfig.key == library_scope.CONFIG_USER_OVERRIDES).first()
        if row is not None:
            if old_value is None:
                db.delete(row)
            else:
                row.value = old_value
            db.commit()
        store.invalidate()
    finally:
        db.close()


@pytest.fixture(scope="module")
def token(client, seed):
    r = client.post(
        "/emby/Users/AuthenticateByName", json={"Username": UNAME, "Pw": PW},
        headers={"X-Emby-Authorization":
                 'MediaBrowser Client="PA36Test", Device="iOS", '
                 f'DeviceId="pa36-dev-{_SUFFIX}", Version="1.0"'})
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


def _h(token):
    return {"X-Emby-Token": token}


# ---------- 鉴权门 ----------

@pytest.mark.parametrize("which", ["series", "episode"])
def test_image_requires_credentials(client, seed, which):
    r = client.get(f"/emby/Items/{seed[which]}/Images/Primary")
    assert r.status_code == 401, r.text


def test_image_index_requires_credentials(client, seed):
    r = client.get(f"/emby/Items/{seed['series']}/Images/Backdrop/0")
    assert r.status_code == 401, r.text


def test_image_bad_token_is_401(client, seed):
    r = client.get(f"/emby/Items/{seed['series']}/Images/Primary",
                   params={"api_key": "definitely-not-a-token"})
    assert r.status_code == 401, r.text


def test_person_image_requires_credentials(client, seed):
    r = client.get(f"/emby/Persons/{seed['person_empty']}/Images/Primary")
    assert r.status_code == 401, r.text


def test_library_cover_requires_credentials(client, seed):
    r = client.get(f"/emby/Items/{seed['lib_a']}/Images/Primary")
    assert r.status_code == 401, r.text


# ---------- 可见范围内的正常出图 ----------

@pytest.mark.parametrize("which", ["series", "episode"])
@pytest.mark.parametrize("query", ["", "?maxWidth=320"])
def test_primary_image_with_api_key(client, seed, token, which, query):
    # <img> 形态：api_key 查询参数（Emby 协议传统腿）
    r = client.get(f"/emby/Items/{seed[which]}/Images/Primary{query}",
                   params={"api_key": token})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/")
    assert r.content
    # 受限内容不许共享缓存复用（public → private，PA-036）
    assert "private" in r.headers.get("cache-control", "")


def test_primary_image_with_header_token(client, seed, token):
    r = client.get(f"/emby/Items/{seed['series']}/Images/Primary",
                   headers=_h(token))
    assert r.status_code == 200, r.text


def test_thumbnail_is_smaller_than_original(client, seed, token):
    full = client.get(f"/emby/Items/{seed['series']}/Images/Primary",
                      headers=_h(token))
    small = client.get(f"/emby/Items/{seed['series']}/Images/Primary?maxWidth=320",
                       headers=_h(token))
    assert full.status_code == small.status_code == 200
    assert len(small.content) < len(full.content)


def test_backdrop_index_with_token(client, seed, token):
    r = client.get(f"/emby/Items/{seed['series']}/Images/Backdrop/0",
                   headers=_h(token))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/")


def test_unknown_item_with_token_is_404_not_401(client, token):
    r = client.get(f"/emby/Items/{_guid()}/Images/Primary", headers=_h(token))
    assert r.status_code == 404


def test_library_cover_visible(client, seed, token):
    r = client.get(f"/emby/Items/{seed['lib_a']}/Images/Primary",
                   headers=_h(token))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/")


def test_virtual_library_cover_borrows_visible_member(client, seed, token):
    r = client.get(f"/emby/Items/{seed['vlib_in']}/Images/Primary",
                   headers=_h(token))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/")


# ---------- 可见范围外的拦截 ----------

def test_restricted_library_item_is_403(client, seed, token):
    r = client.get(f"/emby/Items/{seed['movie_b']}/Images/Primary",
                   headers=_h(token))
    assert r.status_code == 403, r.text


def test_restricted_library_cover_is_403(client, seed, token):
    r = client.get(f"/emby/Items/{seed['lib_b']}/Images/Primary",
                   headers=_h(token))
    assert r.status_code == 403, r.text


def test_virtual_library_out_of_scope_is_403(client, seed, token):
    r = client.get(f"/emby/Items/{seed['vlib_out']}/Images/Primary",
                   headers=_h(token))
    assert r.status_code == 403, r.text


def test_virtual_library_borrowing_restricted_member_is_403(client, seed, token):
    r = client.get(f"/emby/Items/{seed['vlib_hidden']}/Images/Primary",
                   headers=_h(token))
    assert r.status_code == 403, r.text


# ---------- 演员头像 ----------

def test_person_image_no_avatar_is_404_after_auth(client, seed, token):
    r = client.get(f"/emby/Persons/{seed['person_empty']}/Images/Primary",
                   headers=_h(token))
    assert r.status_code == 404, r.text


def test_person_image_served_when_localized(client, seed, token, monkeypatch):
    from backend.emby_server import media_routes

    monkeypatch.setattr(media_routes.image_store, "localize",
                        lambda src: seed["avatar_file"])
    r = client.get(f"/emby/Persons/{seed['person_img']}/Images/Primary",
                   headers=_h(token))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("image/")


def test_person_by_digit_id_served_when_localized(client, seed, token,
                                                  monkeypatch):
    from backend.emby_server import media_routes

    monkeypatch.setattr(media_routes.image_store, "localize",
                        lambda src: seed["avatar_file"])
    r = client.get(f"/emby/Items/{seed['person_img_id']}/Images/Primary",
                   headers=_h(token))
    assert r.status_code == 200, r.text


def test_person_by_digit_id_requires_credentials(client, seed):
    r = client.get(f"/emby/Items/{seed['person_img_id']}/Images/Primary")
    assert r.status_code == 401, r.text


# ---------- 账号卡下发专用 web token ----------

def _portal_jwt(user_id):
    from backend.security import create_access_token

    db = SessionLocal()
    try:
        user = db.query(models.WebUser).filter(
            models.WebUser.id == user_id).first()
        return create_access_token(user.id,
                                   token_version=user.token_version or 0)
    finally:
        db.close()


def test_account_card_issues_web_api_key(portal_client, client, seed):
    jwt = _portal_jwt(seed["user_id"])
    r = portal_client.get("/api/user/emby/server",
                          headers={"Authorization": f"Bearer {jwt}"})
    assert r.status_code == 200, r.text
    key1 = r.json().get("emby_api_key")
    assert key1, "账号卡应下发图片用的 emby_api_key"

    # 下发的 key 能直接当 api_key 取可见图
    ok = client.get(f"/emby/Items/{seed['series']}/Images/Primary",
                    params={"api_key": key1})
    assert ok.status_code == 200, ok.text
    # 受限图照样 403（key 是该用户本人的，可见范围不变）
    no = client.get(f"/emby/Items/{seed['movie_b']}/Images/Primary",
                    params={"api_key": key1})
    assert no.status_code == 403, no.text

    # 再次读卡轮换：新 key 不同、旧 key 立即失效
    r2 = portal_client.get("/api/user/emby/server",
                           headers={"Authorization": f"Bearer {jwt}"})
    key2 = r2.json().get("emby_api_key")
    assert key2 and key2 != key1
    stale = client.get(f"/emby/Items/{seed['series']}/Images/Primary",
                       params={"api_key": key1})
    assert stale.status_code == 401, stale.text
    fresh = client.get(f"/emby/Items/{seed['series']}/Images/Primary",
                       params={"api_key": key2})
    assert fresh.status_code == 200, fresh.text
