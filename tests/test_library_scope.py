"""媒体库可见范围（backend/library_scope.py）单元测试

三条约束，每一条都对应一个「做错了会让用户什么都看不见」的事故：

1. **默认不打断现有行为**：三个配置键的出厂值就是「不过滤」，裸库里一行配置
   都没有时，普通用户与工作人员看到的都必须是全部启用的库。
2. **空列表不能等于全部隐藏**：启用却一个库都没选 / 选的库被删光，
   都必须退回「不过滤」而不是把媒体库清空。
3. **覆盖可关**：指定用户单独覆盖关掉后，该用户恢复跟随服务器默认。

外加一层**接入点**验证（``user_views`` 是客户端媒体库列表的唯一出口），
保证后端算出来的集合真的落到了客户端看到的列表上。

全部用隔离的内存 SQLite，不碰网络、不碰生产库。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import library_scope, models
from backend.emby_server import api as emby_api
from backend.emby_server import models as em
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    # 配置热缓存是进程级的（key 不带库名）：进出都清一次，避免污染别的内存库测试
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def _user(db, username="alice", **kw):
    row = models.WebUser(username=username, password_hash="x", **kw)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _library(db, name, **kw):
    row = em.Library(guid=f"guid-{name}", name=name, **kw)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _ids(db):
    return {lib.id for lib in db.query(em.Library).all()}


# ==================== 1. 默认不打断现有行为 ====================


def test_defaults_are_permissive(db):
    """出厂默认三个键 = 「不过滤」，裸库里没有行也必须读出这个结论"""
    assert library_scope.DEFAULTS[library_scope.CONFIG_DEFAULT_ENABLED] == "0"
    assert library_scope.DEFAULTS[library_scope.CONFIG_DEFAULT_LIBRARIES] == "[]"
    assert library_scope.DEFAULTS[library_scope.CONFIG_USER_OVERRIDES] == "{}"

    _library(db, "电影")
    user = _user(db)
    assert library_scope.default_policy(db) == {"enabled": False, "library_ids": []}
    assert library_scope.effective_ids(db, user) is None
    assert library_scope.effective_ids(db, _user(db, "boss", is_staff=True)) is None


def test_broken_config_is_treated_as_unconfigured(db):
    """坏 JSON / 类型不对 → 当成「没配」，不能让浏览页 500"""
    from backend import models as m

    db.add(m.SystemConfig(key=library_scope.CONFIG_DEFAULT_LIBRARIES, value="{oops"))
    db.add(m.SystemConfig(key=library_scope.CONFIG_USER_OVERRIDES, value="[1,2]"))
    db.commit()
    store.invalidate()

    user = _user(db)
    assert library_scope.default_policy(db)["library_ids"] == []
    assert library_scope.user_overrides(db) == {}
    assert library_scope.effective_ids(db, user) is None


# ==================== 2. 服务器默认范围 ====================


def test_default_scope_restricts_normal_users(db):
    _library(db, "电影")
    staff_lib = _library(db, "内部")
    user = _user(db)

    library_scope.write_default(db, True, [staff_lib.id])
    db.commit()

    open_lib_id = list(_ids(db) - {staff_lib.id})[0]
    assert library_scope.effective_ids(db, user) == {staff_lib.id}
    assert library_scope.library_allowed(db, user, staff_lib.id) is True
    assert library_scope.library_allowed(db, user, open_lib_id) is False
    assert library_scope.library_allowed(db, user, None) is True, "没归到库的条目放行"


def test_staff_exempt(db):
    """工作人员排障要看到全部库，与防共享对管理员豁免同一个理由"""
    _library(db, "电影")
    staff_lib = _library(db, "内部")
    boss = _user(db, "boss", is_staff=True)

    library_scope.write_default(db, True, [staff_lib.id])
    db.commit()

    assert library_scope.effective_ids(db, boss) is None


def test_default_off_means_everything_visible(db):
    """关掉 = 恢复到全部可见（不是「一个都看不到」）"""
    lib = _library(db, "电影")
    _library(db, "剧集")
    user = _user(db)

    library_scope.write_default(db, True, [lib.id])
    assert library_scope.effective_ids(db, user) == {lib.id}

    library_scope.write_default(db, False, [lib.id])
    db.commit()
    assert library_scope.effective_ids(db, user) is None


# ==================== 3. 空列表不能等于全部隐藏 ====================


def test_enabling_with_no_library_is_rejected(db):
    """开开关却一个库都没勾 = 配置错误，必须报错而不是把全站媒体库清空"""
    _library(db, "电影")
    with pytest.raises(ValueError):
        library_scope.write_default(db, True, [])
    # 被拒之后配置保持原样（关闭）
    assert library_scope.default_policy(db)["enabled"] is False


def test_enabled_but_all_libraries_deleted_falls_back(db):
    """开着开关，但选中的库被删光 → 退回不过滤，不能变成一个都看不到"""
    lib = _library(db, "电影")
    user = _user(db)
    library_scope.write_default(db, True, [lib.id])
    db.delete(lib)
    db.commit()
    store.invalidate()

    assert library_scope.effective_ids(db, user) is None
    payload = library_scope.policy_payload(db)
    assert payload["default"]["active"] is False, "后台要看得见「没生效」这件事"


def test_unknown_library_ids_are_dropped_on_write(db):
    """写入时就把已删掉的库 id 剔干净：配置里留一堆死 id 只会在后台显示成
    「已删除的库 #7」；只剩死 id 还要启用 = 一个有效库都没有，直接拒绝"""
    keep = _library(db, "电影")
    user = _user(db)

    # 混入不存在的 id：只留真实存在的那一个
    applied = library_scope.write_default(db, True, [keep.id, 99999])
    assert applied["library_ids"] == [keep.id]
    assert library_scope.effective_ids(db, user) == {keep.id}

    # 全是不存在的 id：拒绝——这次写入一个字节都不落库，原配置原样不动
    with pytest.raises(ValueError):
        library_scope.write_default(db, True, [99999])
    assert library_scope.default_policy(db)["enabled"] is True, "被拒之后原配置不动"
    with pytest.raises(ValueError):
        library_scope.write_user_override(db, user.id, True, [99999])


# ==================== 4. 用户单独覆盖 ====================


def test_user_override_beats_default(db):
    default_lib = _library(db, "电影")
    _library(db, "剧集")
    mine = _library(db, "内部")
    user = _user(db)

    library_scope.write_default(db, True, [default_lib.id])
    library_scope.write_user_override(db, user.id, True, [mine.id])
    db.commit()

    assert library_scope.effective_ids(db, user) == {mine.id}
    # 没有覆盖的用户仍然吃默认
    other = _user(db, "bob")
    assert library_scope.effective_ids(db, other) == {default_lib.id}


def test_turning_override_off_returns_to_default(db):
    default_lib = _library(db, "电影")
    mine = _library(db, "内部")
    user = _user(db)

    library_scope.write_default(db, True, [default_lib.id])
    library_scope.write_user_override(db, user.id, True, [mine.id])
    library_scope.write_user_override(db, user.id, False, [mine.id])
    db.commit()

    assert library_scope.effective_ids(db, user) == {default_lib.id}
    # 关闭只是不生效，列表留着，方便再打开
    kept = library_scope.user_override(db, user.id)
    assert kept is not None and kept["enabled"] is False
    assert kept["libraries"] == [mine.id]


def test_removing_override_fully_returns_to_default(db):
    default_lib = _library(db, "电影")
    mine = _library(db, "内部")
    user = _user(db)

    library_scope.write_default(db, True, [default_lib.id])
    library_scope.write_user_override(db, user.id, True, [mine.id])
    db.commit()
    assert library_scope.remove_user_override(db, user.id)["removed"] is True

    assert library_scope.user_override(db, user.id) is None
    assert library_scope.effective_ids(db, user) == {default_lib.id}


def test_override_with_deleted_libraries_falls_back_to_default(db):
    """个人覆盖选的库被删光 → 回落服务器默认，而不是「一个都看不到」"""
    default_lib = _library(db, "电影")
    mine = _library(db, "内部")
    user = _user(db)
    library_scope.write_default(db, True, [default_lib.id])
    library_scope.write_user_override(db, user.id, True, [mine.id])
    db.delete(mine)
    db.commit()
    store.invalidate()

    assert library_scope.effective_ids(db, user) == {default_lib.id}


def test_override_enabling_without_library_is_rejected(db):
    user = _user(db)
    with pytest.raises(ValueError):
        library_scope.write_user_override(db, user.id, True, [])


def test_override_on_top_of_default_off(db):
    """默认关闭时，单独覆盖仍然有效——这就是「指定用户单独覆盖」的意义"""
    _library(db, "电影")
    mine = _library(db, "内部")
    user = _user(db)
    library_scope.write_user_override(db, user.id, True, [mine.id])
    db.commit()

    assert library_scope.effective_ids(db, user) == {mine.id}


# ==================== 5. 接入点：客户端媒体库列表 ====================


def test_user_views_hides_out_of_scope_libraries(db):
    """``user_views`` 是客户端媒体库列表的唯一出口，必须照可见范围过滤"""
    open_lib = _library(db, "电影", is_enabled=True)
    secret_lib = _library(db, "内部", is_enabled=True)
    _library(db, "已停用", is_enabled=False)
    user = _user(db)

    # 默认：全部启用的库都在（升级前行为）
    names = {i["Name"] for i in emby_api.user_views("me", user=user, db=db)["Items"]}
    assert names == {"电影", "内部"}

    library_scope.write_default(db, True, [open_lib.id])
    db.commit()
    names = {i["Name"] for i in emby_api.user_views("me", user=user, db=db)["Items"]}
    assert names == {"电影"}, "停用的库不该出现，没选中的也不该出现"

    # 工作人员不受限（含已停用的仍然不显示——那是 is_enabled 的事）
    boss = _user(db, "boss", is_staff=True)
    names = {i["Name"] for i in emby_api.user_views("me", user=boss, db=db)["Items"]}
    assert names == {"电影", "内部"}


def test_user_views_applies_user_override(db):
    open_lib = _library(db, "电影")
    secret_lib = _library(db, "内部")
    user = _user(db)
    library_scope.write_default(db, True, [open_lib.id])
    library_scope.write_user_override(db, user.id, True, [secret_lib.id])
    db.commit()

    names = {i["Name"] for i in emby_api.user_views("me", user=user, db=db)["Items"]}
    assert names == {"内部"}

    library_scope.write_user_override(db, user.id, False, [secret_lib.id])
    db.commit()
    names = {i["Name"] for i in emby_api.user_views("me", user=user, db=db)["Items"]}
    assert names == {"电影"}


# ==================== 6. 查询层：条目过滤 ====================


def _movie(db, lib, title):
    row = em.MediaItem(guid=f"g-{title}", item_type="movie", name=title,
                       library_id=lib.id, is_hidden=False)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_scope_query_filters_by_library(db):
    """条目查询的过滤语义：不在名单里的库里的条目不出现在列表/搜索里，
    没归库的条目（library_id IS NULL）一并放行，与 nodes 同一口径"""
    from backend import library_scope as ls

    open_lib = _library(db, "电影")
    secret_lib = _library(db, "内部")
    shown = _movie(db, open_lib, "公开片")
    hidden = _movie(db, secret_lib, "内部片")
    user = _user(db)
    library_scope.write_default(db, True, [open_lib.id])
    db.commit()

    allowed = ls.effective_ids(db, user)
    rows = ls.scope_query(db.query(em.MediaItem), allowed).all()
    assert [r.name for r in rows] == ["公开片"]

    # 不过滤时原样放行
    all_rows = ls.scope_query(db.query(em.MediaItem), None).all()
    assert {r.name for r in all_rows} == {"公开片", "内部片"}

    # 空名单（防御分支）= 什么都查不到，而不是全部放行
    assert ls.scope_query(db.query(em.MediaItem), set()).all() == []
    assert shown.id != hidden.id


def test_item_allowed_by_library(db):
    """按条目判定（详情/播放前的单点检查用同一份口径）"""
    open_lib = _library(db, "电影")
    secret_lib = _library(db, "内部")
    _movie(db, open_lib, "公开片")
    secret = _movie(db, secret_lib, "内部片")
    user = _user(db)
    library_scope.write_default(db, True, [open_lib.id])
    db.commit()

    assert library_scope.item_allowed(db, user, secret) is False
    assert library_scope.item_allowed(db, user, None) is False
    assert library_scope.item_allowed(db, _user(db, "boss", is_staff=True), secret) is True


# ==================== 7. 管理端 payload ====================


def test_policy_payload_reports_state(db):
    open_lib = _library(db, "电影")
    secret_lib = _library(db, "内部")
    user = _user(db)
    library_scope.write_default(db, True, [open_lib.id])
    library_scope.write_user_override(db, user.id, True, [secret_lib.id])
    db.commit()

    payload = library_scope.policy_payload(db)
    assert payload["default"]["enabled"] is True
    assert payload["default"]["library_ids"] == [open_lib.id]
    assert payload["default"]["active"] is True
    assert payload["overrides"][str(user.id)] == {
        "enabled": True, "library_ids": [secret_lib.id],
    }
    assert payload["counts"]["libraries"] == 2
    assert payload["counts"]["overrides"] == 1
    assert any(u["id"] == user.id for u in payload["users"])
    assert {lib["id"] for lib in payload["libraries"]} == {open_lib.id, secret_lib.id}


# ==================== 8. 读失败 fail-closed（S12） ====================


def _break_scope_reads(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(library_scope, "user_overrides", _boom)
    monkeypatch.setattr(library_scope, "_fallback_policy", _boom)


def test_read_failure_fails_closed_for_normal_user(db, monkeypatch, caplog):
    """可见范围读失败：普通用户一律不可见（空集合），并记 warning——
    不能像旧实现那样退化为「全部可见」把稳定性故障变成越权"""
    import logging

    from fastapi import HTTPException

    lib = _library(db, "电影")
    movie = _movie(db, lib, "公开片")
    user = _user(db)
    _break_scope_reads(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=library_scope.logger.name):
        allowed = library_scope.effective_ids_safe(db, user)
    assert allowed == set()
    assert any("fail-closed" in r.getMessage() for r in caplog.records)

    # 列表 / 搜索：什么都查不到
    assert library_scope.scope_query(db.query(em.MediaItem), allowed).all() == []
    # 按条目取（详情 / 播放 / 下载的统一入口）：403
    assert emby_api._item_visible(db, user, movie) is False
    with pytest.raises(HTTPException) as exc:
        emby_api._require_visible_item(db, user, movie.guid)
    assert exc.value.status_code == 403


def test_read_failure_keeps_staff_unrestricted(db, monkeypatch):
    """工作人员豁免不受影响：读失败时管理员仍能看到全部"""
    lib = _library(db, "电影")
    movie = _movie(db, lib, "公开片")
    boss = _user(db, "boss", is_staff=True)
    _break_scope_reads(monkeypatch)

    assert library_scope.effective_ids_safe(db, boss) is None
    assert emby_api._require_visible_item(db, boss, movie.guid).id == movie.id

    # 即便异常来自更早的位置（effective_ids 整体失败），管理员也兜底放行
    monkeypatch.setattr(library_scope, "effective_ids",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert library_scope.effective_ids_safe(db, boss) is None
