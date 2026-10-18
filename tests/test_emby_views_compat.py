"""第三方客户端协议兼容回归（P0：媒体库列表空白，PR #372 未修利索的部分）

官方 Emby / Jellyfin 的对应实现（对比证据，修复时逐条核对过源码）：

- Views 条目 = CollectionFolder 的 BaseItemDto，由 DtoService 生成，
  ``new DtoOptions()`` 默认**全字段**下发（MediaBrowser.Controller/Dto/DtoOptions.cs：
  ``Fields = allFields ? AllItemFields : []``），UserViewsController 还显式追加
  PrimaryImageAspectRatio / DisplayPreferencesId（Jellyfin.Api/Controllers/UserViewsController.cs：
  ``dtoOptions.Fields = [.. , ItemFields.PrimaryImageAspectRatio, ItemFields.DisplayPreferencesId]``）。
- ServerId 无条件下发（DtoService.GetBaseItemDtoInternal 第一行
  ``new BaseItemDto { ServerId = _appHost.SystemId }``）；MediaType / LocationType /
  Etag / DateCreated / Tags / ImageBlurHashes 由 AttachBasicFields / Etag 分支恒发。
- UserData 的 ItemId / Key 由 GetUserItemDataDto 无条件设置（DtoService.cs）。
- SessionInfo 的 PlayState / AdditionalUsers / NowPlayingQueue 在官方构造器里初始化，
  IsActive / HasCustomDeviceName 为非空 bool，SupportedCommands / PlayableMediaTypes
  在未上报能力时为空数组（MediaBrowser.Controller/Session/SessionInfo.cs）。
- MediaSourceInfo 的 bool / 集合字段在官方构造器里初始化后恒发
  （MediaBrowser.Model/Dto/MediaSourceInfo.cs）。
- UserPolicy / UserConfiguration 的 bool / int / 数组字段全部非空，序列化时每个键都发
  （MediaBrowser.Model/Users/UserPolicy.cs、MediaBrowser.Model/Configuration/UserConfiguration.cs）。

测试口径：全部走**真实路由 + 真实 token**（AuthenticateByName 签发），不直接调内部函数
——用户要求「用真实 token 调接口验证实际返回」。共享 sqlite 库里可能有其他测试种的数据，
所以断言一律**按本次种子的 Id 定点比对**，不做全局计数。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import api as emby_api
from backend.security import hash_password

init_db()

PW = "views-compat-pw"
UNAME = "viewscompat_alice"


def _guid():
    return uuid.uuid4().hex


@pytest.fixture(scope="module")
def seed():
    """独立用户 + 电影库（1 电影带类型、1 剧集含季/集）。返回 guid 字典。

    模块级共享：认证端点限流 10 次/分钟/账号，逐测重建会把后面的用例全卡成 429。
    先清掉同名残留（上一轮运行中途挂掉时 yield 前的清理不会执行），保证可重复跑。
    """
    db = SessionLocal()
    old = db.query(models.WebUser).filter(
        models.WebUser.username == UNAME).first()
    if old:
        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == old.id).delete(synchronize_session=False)
        db.query(models.WebUser).filter(models.WebUser.id == old.id).delete()
        db.commit()
    for stale in db.query(em.Library).filter(
            em.Library.name.like("兼容%库")).all():
        ids = [r[0] for r in db.query(em.MediaItem.id)
               .filter(em.MediaItem.library_id == stale.id).all()]
        if ids:
            db.query(em.ItemFacet).filter(
                em.ItemFacet.item_id.in_(ids)).delete(synchronize_session=False)
            db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
                synchronize_session=False)
        db.query(em.Library).filter(em.Library.id == stale.id).delete()
    db.commit()
    user = models.WebUser(
        username=UNAME,
        password_hash="x",
        is_active=True,
        # 管理员绕过付费墙，才能覆盖 PlaybackInfo（CanDelete 恒为 False，不受身份影响）
        is_staff=True,
        emby_username=UNAME,
        emby_password=hash_password(PW),
    )
    db.add(user)
    lib = em.Library(guid=_guid(), name="兼容测试库", collection_type="movies",
                     is_enabled=True)
    db.add(lib)
    db.flush()

    def mk(item_type, name, **kw):
        it = em.MediaItem(guid=_guid(), library_id=lib.id,
                          item_type=item_type, name=name, **kw)
        db.add(it)
        db.flush()
        return it

    movie = mk("movie", "兼容测试电影 (2024)",
               file_path="/tmp/none/compat-movie.mp4", container="mp4",
               sort_name="兼容测试电影", production_year=2024,
               duration_ticks=600_000_000, genres="兼容动作", studios="兼容片厂")
    series = mk("series", "兼容测试剧 (2023)", file_path="/tmp/none/compat-show",
                sort_name="兼容测试剧", production_year=2023)
    season = mk("season", "Season 01", series_id=series.id, season_number=1)
    ep = mk("episode", "兼容测试剧 S01E01", series_id=series.id,
            parent_id=season.id, season_number=1, episode_number=1,
            file_path="/tmp/none/compat-e01.mp4", container="mp4")
    db.commit()
    data = {
        "user_id": user.id,
        "lib": lib.guid,
        "movie": movie.guid,
        "series": series.guid,
        "season": season.guid,
        "ep": ep.guid,
    }
    db.close()
    yield data
    # 清理：先删条目再删库（外键自引用），用户最后删
    try:
        db = SessionLocal()
        row = db.query(em.Library).filter(em.Library.guid == data["lib"]).first()
        if row is not None:
            ids = [r[0] for r in db.query(em.MediaItem.id)
                   .filter(em.MediaItem.library_id == row.id).all()]
            if ids:
                # 分类关联行必须一起删：批量 delete 不走 session.deleted，
                # facets 的 after_flush 清理钩子不会触发；留下孤儿行后，
                # SQLite 会把刚释放的 rowid 复用给新条目，下一轮种子就撞
                # emby_item_facets 的唯一键（UNIQUE constraint failed）。
                db.query(em.ItemFacet).filter(
                    em.ItemFacet.item_id.in_(ids)).delete(synchronize_session=False)
                db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
                    synchronize_session=False)
            db.query(em.Library).filter(em.Library.id == row.id).delete()
        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == data["user_id"]).delete(
            synchronize_session=False)
        db.query(models.WebUser).filter(
            models.WebUser.id == data["user_id"]).delete()
        db.commit()
        db.close()
    except Exception:  # noqa: BLE001 — 清理失败不掩盖测试本体的结果
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
                 'MediaBrowser Client="CompatTest", Device="iOS", '
                 'DeviceId="compat-dev-1", Version="1.0"'},
    )
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


def _h(token):
    return {"X-Emby-Token": token}


def _views_item(client, token, seed, path=None):
    r = client.get(path or f"/emby/Users/{seed['user_id']}/Views", headers=_h(token))
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) >= {"Items", "TotalRecordCount", "StartIndex"}
    hit = [i for i in body["Items"] if i["Id"] == seed["lib"]]
    assert hit, f"种子媒体库不在 Views 里：{[i.get('Id') for i in body['Items']]}"
    return hit[0]


# ---------- 1. Views（P0 本体）：CollectionFolder 官方字段集 ----------

def test_views_item_has_official_collectionfolder_fields(client, token, seed):
    item = _views_item(client, token, seed)
    assert item["ServerId"] == emby_api.SERVER_ID
    assert item["Type"] == "CollectionFolder"
    assert item["IsFolder"] is True
    # MediaType：官方 dto.MediaType 恒发，CollectionFolder → Unknown
    assert item["MediaType"] == "Unknown"
    assert item["LocationType"] == "FileSystem"
    assert item["SortName"] == "兼容测试库"
    assert item["CollectionType"] == "movies"
    # DisplayPreferencesId：官方 UserViewsController 显式请求的字段
    assert item["DisplayPreferencesId"] == item["Id"]
    # Etag：官方 dto.Etag，非空且稳定（同库两次请求必须一致）
    assert isinstance(item["Etag"], str) and item["Etag"]
    again = _views_item(client, token, seed)
    assert again["Etag"] == item["Etag"]
    # Tags / ImageBlurHashes：官方全字段下发时为 [] / {}
    assert item["Tags"] == []
    assert item["ImageBlurHashes"] == {}
    assert isinstance(item["ImageTags"], dict)
    assert isinstance(item["BackdropImageTags"], list)
    # CanDownload 是 bool；协议面无删除媒体库端点，CanDelete 如实为 False
    assert item["CanDownload"] is True
    assert item["CanDelete"] is False
    assert isinstance(item["ChildCount"], int)
    assert item["DateCreated"].endswith("Z")  # iOS 严格日期解析要求 Z/offset


def test_views_userdata_carries_itemid_and_key(client, token, seed):
    """官方 GetUserItemDataDto 无条件下发 ItemId / Key（缺键 = 严格客户端丢条目）"""
    item = _views_item(client, token, seed)
    ud = item["UserData"]
    for key in ("PlaybackPositionTicks", "PlayCount", "Played", "IsFavorite",
                "PlayedPercentage", "ItemId", "Key"):
        assert key in ud, f"Views UserData 缺 {key}"
    assert ud["ItemId"] == item["Id"]
    assert ud["Key"] == item["Id"]


def test_views_without_cover_omits_primary_image_ratio(client, token, seed):
    """官方 PrimaryImageAspectRatio 无 Primary 图时为 null → 字段整个不发"""
    item = _views_item(client, token, seed)
    assert "PrimaryImageAspectRatio" not in item


def test_views_collectiontype_omitted_when_null(client, token, seed):
    """官方 nullable 枚举为 null 时省略字段（客户端拿到 null 会裸调 .toLowerCase() 崩）"""
    db = SessionLocal()
    lib = em.Library(guid=_guid(), name="兼容无类型库", collection_type="movies",
                     is_enabled=True)
    db.add(lib)
    db.commit()
    lib_id = lib.guid
    # 列级 default 会把 None 顶成 "movies"，这里显式回写 NULL，
    # 模拟老部署里 collection_type 为空的历史行
    db.query(em.Library).filter(em.Library.guid == lib_id).update(
        {"collection_type": None}, synchronize_session=False)
    db.commit()
    db.close()
    try:
        r = client.get(f"/emby/Users/{seed['user_id']}/Views", headers=_h(token))
        assert r.status_code == 200, r.text
        hit = [i for i in r.json()["Items"] if i["Id"] == lib_id]
        assert hit, "无类型的库不在 Views 里"
        assert "CollectionType" not in hit[0]
    finally:
        db = SessionLocal()
        db.query(em.Library).filter(em.Library.guid == lib_id).delete()
        db.commit()
        db.close()


def test_views_aliases_share_identical_shape(client, token, seed):
    """/UserViews 与 /Library/MediaFolders 都是 user_views 的别名，字段集必须一致"""
    main = _views_item(client, token, seed)
    for path in ("/emby/UserViews", "/emby/Library/MediaFolders"):
        alias = _views_item(client, token, seed, path=path)
        assert set(alias) == set(main), f"{path} 字段集与 Views 不一致"
        assert alias["ServerId"] == emby_api.SERVER_ID


def test_root_items_route_matches_user_items(client, token, seed):
    """官方 /Items（不带用户前缀）与 /Users/{id}/Items 是同一出口。

    Reflix 等客户端的媒体库页直接调根级 /Items（带 ParentId/Recursive/
    SortBy=DateLastContentAdded,SortName），缺这条路由时整页 404 加载失败
    （2026-10-15 生产日志实证）。用户由 token 解析，路径里本来就没有用户 id。
    """
    qs = (f"ParentId={seed['lib']}&Recursive=true&IncludeItemTypes=Movie"
          f"&SortBy=DateLastContentAdded,SortName&SortOrder=Descending"
          f"&StartIndex=0&Limit=40"
          f"&Fields=PrimaryImageAspectRatio,ProviderIds,Overview,PremiereDate")
    for path in ("/Items", "/emby/Items"):
        r = client.get(f"{path}?{qs}", headers=_h(token))
        assert r.status_code == 200, f"{path}: {r.text}"
        ids = {i["Id"] for i in r.json()["Items"]}
        assert seed["movie"] in ids, f"{path} 未返回种子电影: {sorted(ids)}"
    r = client.get(f"/Items?{qs}")
    assert r.status_code in (401, 403), f"无 token 应拒绝: {r.status_code}"


# ---------- 2. /Users/{id}/Items 与详情 ----------

def test_items_list_and_detail_have_sortname_etag_and_userdata_ids(client, token, seed):
    r = client.get(f"/emby/Users/{seed['user_id']}/Items?ParentId={seed['lib']}"
                   f"&Recursive=true&Limit=50", headers=_h(token))
    assert r.status_code == 200, r.text
    items = {i["Id"]: i for i in r.json()["Items"]}
    movie = items[seed["movie"]]
    assert movie["SortName"] == "兼容测试电影"
    assert isinstance(movie["Etag"], str) and movie["Etag"]
    assert movie["UserData"]["ItemId"] == seed["movie"]
    assert movie["UserData"]["Key"] == seed["movie"]
    assert movie["ImageBlurHashes"] == {}
    assert movie["ServerId"] == emby_api.SERVER_ID

    r = client.get(f"/emby/Users/{seed['user_id']}/Items/{seed['movie']}",
                   headers=_h(token))
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["SortName"] == "兼容测试电影"
    assert detail["UserData"]["ItemId"] == seed["movie"]
    assert detail["Etag"] == movie["Etag"], "列表与详情的 Etag 必须同源"


def test_episode_carries_index_numbers(client, token, seed):
    """回归：单集详情/列表必须带 IndexNumber / ParentIndexNumber（官方恒发非空值）"""
    r = client.get(f"/emby/Shows/{seed['series']}/Episodes", headers=_h(token))
    assert r.status_code == 200, r.text
    ep = r.json()["Items"][0]
    assert ep["IndexNumber"] == 1
    assert ep["ParentIndexNumber"] == 1


# ---------- 3. 登录：SessionInfo / User.Policy / User.Configuration ----------

def test_auth_session_info_has_official_fields(client, token, seed):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": UNAME, "Pw": PW},
        headers={"X-Emby-Authorization":
                 'MediaBrowser Client="CompatTest", Device="iOS", '
                 'DeviceId="compat-dev-2", Version="1.0"'},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ServerId"] == emby_api.SERVER_ID
    si = body["SessionInfo"]
    for key in ("Id", "UserId", "ServerId", "Client", "DeviceName", "DeviceId",
                "ApplicationVersion", "IsActive", "HasCustomDeviceName",
                "LastActivityDate", "SupportsRemoteControl", "SupportsMediaControl",
                "PlayState", "AdditionalUsers", "NowPlayingQueue",
                "SupportedCommands", "PlayableMediaTypes"):
        assert key in si, f"SessionInfo 缺 {key}"
    assert si["IsActive"] is True
    assert si["AdditionalUsers"] == [] and si["NowPlayingQueue"] == []
    assert si["SupportedCommands"] == [] and si["PlayableMediaTypes"] == []
    ps = si["PlayState"]
    for key in ("CanSeek", "IsPaused", "IsMuted", "RepeatMode"):
        assert key in ps, f"PlayState 缺 {key}"
    assert ps["RepeatMode"] == "RepeatNone"
    assert si["LastActivityDate"].endswith("Z")


def test_user_dto_policy_and_configuration_key_coverage(client, token, seed):
    """官方 UserPolicy / UserConfiguration 的 bool 与数组字段每个键都发"""
    r = client.get("/emby/Users/Me", headers=_h(token))
    assert r.status_code == 200, r.text
    user = r.json()
    policy = user["Policy"]
    official_policy_keys = {
        "IsAdministrator", "IsHidden", "IsDisabled",
        "EnableCollectionManagement", "EnableSubtitleManagement",
        "EnableLyricManagement", "EnableContentDeletion",
        "EnableContentDeletionFromFolders", "EnableContentDownloading",
        "EnableMediaPlayback", "EnableAudioPlaybackTranscoding",
        "EnableVideoPlaybackTranscoding", "EnablePlaybackRemuxing",
        "ForceRemoteSourceTranscoding", "EnableSyncTranscoding",
        "EnableMediaConversion", "EnableAllDevices", "EnabledDevices",
        "EnableAllFolders", "EnabledFolders", "EnableAllChannels",
        "EnabledChannels", "EnableUserPreferenceAccess",
        "EnableRemoteControlOfOtherUsers", "EnableSharedDeviceControl",
        "EnableRemoteAccess", "EnableLiveTvManagement", "EnableLiveTvAccess",
        "EnablePublicSharing", "BlockUnratedItems", "BlockedTags",
        "AllowedTags", "BlockedMediaFolders", "BlockedChannels",
        "AccessSchedules", "InvalidLoginAttemptCount",
        "LoginAttemptsBeforeLockout", "MaxActiveSessions",
        "RemoteClientBitrateLimit", "AuthenticationProviderId",
        "PasswordResetProviderId", "SyncPlayAccess",
    }
    missing = official_policy_keys - set(policy)
    assert not missing, f"Policy 缺官方字段: {sorted(missing)}"
    assert policy["IsAdministrator"] is True  # 种子用户是管理员
    # 2026-10-17 转码删除：转码开关字段仍在（官方口径），但恒下发 False
    assert policy["EnableAudioPlaybackTranscoding"] is False
    assert policy["EnableVideoPlaybackTranscoding"] is False

    cfg = user["Configuration"]
    official_cfg_keys = {
        "GroupedFolders", "DisplayCollectionsView", "EnableLocalPassword",
        "OrderedViews", "LatestItemsExcludes", "MyMediaExcludes",
        "HidePlayedInLatest", "RememberAudioSelections",
        "RememberSubtitleSelections", "EnableNextEpisodeAutoPlay",
        "PlayDefaultAudioTrack", "DisplayMissingEpisodes",
    }
    missing = official_cfg_keys - set(cfg)
    assert not missing, f"Configuration 缺官方字段: {sorted(missing)}"
    assert cfg["OrderedViews"] == [] and cfg["GroupedFolders"] == []


def test_policy_endpoint_matches_user_dto_policy(client, token, seed):
    """/Users/{id}/Policy 与 User.Policy 必须是同一个出口（不再各写一份）"""
    r = client.get(f"/emby/Users/{seed['user_id']}/Policy", headers=_h(token))
    assert r.status_code == 200, r.text
    me = client.get("/emby/Users/Me", headers=_h(token)).json()
    assert r.json() == me["Policy"]


def test_policy_dto_non_admin_defaults():
    """非管理员口径：不删内容、不隐藏、其余按官方默认放行"""
    fake = type("U", (), {"is_staff": False, "is_active": True})()
    p = emby_api._policy_dto(fake)
    assert p["IsAdministrator"] is False
    assert p["EnableContentDeletion"] is False
    assert p["IsHidden"] is False
    assert p["EnableMediaPlayback"] is True


# ---------- 4. 分类元数据（Genres / Studios）----------

def test_genres_and_studios_items_have_official_fields(client, token, seed):
    r = client.get("/emby/Genres", headers=_h(token))
    assert r.status_code == 200, r.text
    hit = [i for i in r.json()["Items"] if i["Name"] == "兼容动作"]
    assert hit, "种子类型不在 /Genres 里"
    item = hit[0]
    assert item["ServerId"] == emby_api.SERVER_ID
    assert item["MediaType"] == "Unknown"
    assert item["ImageBlurHashes"] == {}
    assert item["UserData"]["ItemId"] == item["Id"]
    assert item["UserData"]["Key"] == item["Id"]

    r = client.get("/emby/Genres/兼容动作", headers=_h(token))
    assert r.status_code == 200, r.text
    by_name = r.json()
    assert by_name["ServerId"] == emby_api.SERVER_ID
    assert by_name["Id"] == item["Id"], "按名查询与列表的 Id 必须一致"

    r = client.get("/emby/Studios", headers=_h(token))
    assert r.status_code == 200, r.text
    hit = [i for i in r.json()["Items"] if i["Name"] == "兼容片厂"]
    assert hit and hit[0]["ServerId"] == emby_api.SERVER_ID
    assert "ItemId" in hit[0]["UserData"]


# ---------- 5. PlaybackInfo / MediaSourceInfo ----------

def test_playback_info_media_source_has_official_static_fields(client, token, seed):
    r = client.post(f"/emby/Items/{seed['movie']}/PlaybackInfo",
                    headers=_h(token), json={})
    assert r.status_code == 200, r.text
    body = r.json()
    # 顶层结构与官方 PlaybackInfoResponse 一致：ErrorCode 为 null 时按官方口径
    # 整个不发（也符合本仓库的空值过滤约定，见 tests/test_playback_null_filter.py）
    assert set(body) >= {"MediaSources", "PlaySessionId"}
    assert body.get("ErrorCode") is None
    src = body["MediaSources"][0]
    # 官方 MediaSourceInfo 构造器初始化后恒发的值类型 / 集合字段
    official_static = {
        "ReadAtNativeFramerate": False, "IgnoreDts": False,
        "IgnoreIndex": False, "GenPtsInput": False,
        "IsInfiniteStream": False,
        "UseMostCompatibleTranscodingProfile": False,
        "RequiresOpening": False, "RequiresClosing": False,
        "SupportsProbing": True, "HasSegments": False,
        "Formats": [], "MediaAttachments": [], "RequiredHttpHeaders": {},
    }
    for key, value in official_static.items():
        assert src.get(key) == value, f"MediaSource {key} 应为 {value}"
    assert src["SupportsDirectPlay"] is True
    # 2026-10-17 转码删除：SupportsTranscoding 恒 False，不再下发 TranscodingUrl
    assert src["SupportsTranscoding"] is False
    assert "TranscodingUrl" not in src
    assert src["IsRemote"] is False


# ---------- 6. 收藏 / 已看回包也带 ItemId / Key ----------

def test_rating_response_userdata_carries_item_id(client, token, seed):
    r = client.post(f"/emby/Users/{seed['user_id']}/Items/{seed['movie']}/Rating",
                    headers=_h(token), json={"IsFavorite": True})
    assert r.status_code == 200, r.text
    ud = r.json()
    assert ud["IsFavorite"] is True
    assert ud["ItemId"] == seed["movie"]
    assert ud["Key"] == seed["movie"]


# ---------- 7. DisplayPreferences：每个媒体库一条（官方 /DisplayPreferences/{id}）----------

def test_display_preferences_folder_roundtrip(client, token, seed):
    path = f"/emby/DisplayPreferences/{seed['lib']}"
    r = client.get(path, headers=_h(token))
    assert r.status_code == 200, r.text
    assert r.json()["Id"] == seed["lib"]
    r = client.post(path, headers=_h(token), json={"Id": seed["lib"], "CustomPrefs": {}})
    assert r.status_code == 200, r.text
    assert r.json()["Id"] == seed["lib"]
    # /users 作用域保持原样，不被 {pref_id} 路由抢走
    r = client.get("/emby/DisplayPreferences/users", headers=_h(token))
    assert r.status_code == 200 and r.json()["Id"] == "users"


# ---------- 8. DateCreated 恒发：created_at 为 NULL 的历史迁移库 ----------

def _views_item_by_guid(client, token, seed, guid):
    r = client.get(f"/emby/Users/{seed['user_id']}/Views", headers=_h(token))
    assert r.status_code == 200, r.text
    hit = [i for i in r.json()["Items"] if i["Id"] == guid]
    assert hit, f"库 {guid} 不在 Views 里"
    return hit[0]


def _seed_null_created_lib(dates):
    """created_at=None（复现生产迁移库状态）的库 + 指定 date_added 的条目。

    注意：Library.created_at 列有 ORM default=datetime.now，构造时传 None
    会被 default 覆盖成 now——必须走 Core UPDATE 显式写 NULL，才能复现
    生产里 11 个库 created_at 全 NULL 的状态。
    """
    db = SessionLocal()
    lib = em.Library(guid=_guid(), name="兼容DateCreated库", collection_type="movies",
                     is_enabled=True)
    db.add(lib)
    db.flush()
    for i, dt in enumerate(dates):
        db.add(em.MediaItem(guid=_guid(), library_id=lib.id, item_type="movie",
                            name=f"兼容DC条目{i}", date_added=dt))
    db.commit()
    db.execute(
        em.Library.__table__.update()
        .where(em.Library.__table__.c.id == lib.id)
        .values(created_at=None)
    )
    db.commit()
    data = {"id": lib.id, "guid": lib.guid}
    db.close()
    return data


def _drop_lib(lib):
    db = SessionLocal()
    try:
        ids = [r[0] for r in db.query(em.MediaItem.id)
               .filter(em.MediaItem.library_id == lib["id"]).all()]
        if ids:
            db.query(em.ItemFacet).filter(
                em.ItemFacet.item_id.in_(ids)).delete(synchronize_session=False)
            db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
                synchronize_session=False)
        db.query(em.Library).filter(em.Library.id == lib["id"]).delete()
        db.commit()
    finally:
        db.close()


def test_views_datecreated_falls_back_to_earliest_item(client, token, seed):
    """生产 11 个库的 created_at 全为 NULL（2026-10-15 实测），此前字段整缺，
    ddPlayer 严格解析整条失败。修复后：DateCreated 恒发，回退为库内最早入库时间。"""
    dates = [datetime(2024, 1, 1, 12, 0, 0), datetime(2023, 6, 6, 6, 30, 0),
             datetime(2022, 5, 4, 3, 2, 1)]
    lib = _seed_null_created_lib(dates)
    try:
        item = _views_item_by_guid(client, token, seed, lib["guid"])
        assert "DateCreated" in item, "created_at 为空时 Views 必须恒发 DateCreated"
        assert item["DateCreated"] == emby_api._iso(min(dates))
        assert item["DateCreated"].endswith("Z")
    finally:
        _drop_lib(lib)


def test_views_datecreated_empty_lib_uses_constant(client, token, seed):
    """created_at 为空且库内没有任何条目 → 回退固定常量。"""
    lib = _seed_null_created_lib([])
    try:
        item = _views_item_by_guid(client, token, seed, lib["guid"])
        assert item["DateCreated"] == emby_api._iso(emby_api._LIB_CREATED_AT_FALLBACK)
    finally:
        _drop_lib(lib)
