"""CMS 库（kind=applecms）派生封面：借代表海报 / 回退 / SVG 占位。

与虚拟库派生同口径，差别只在成员：CMS 成员是 library_id 直属行
（movie/series、排除 is_hidden 与 merged 副本），不走平台聚合。
隔离临时 SQLite，不连生产库；种子前缀 cmscover-pure / cmscover-seed，
只清自己前缀的行。本地库与虚拟库回归由 test_virtual_library_cover.py
与 test_media_library_covers.py 覆盖，本文件另带五件套的本地库回归。
"""

import base64
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)

from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm


import pytest

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import library_covers
from backend.emby_server import models as em
from backend.security import hash_password


_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

UNAME = "cmscover_seed_user"
PW = "cmscover-seed-pw"
PURE = "cmscover-pure"
SEED = "cmscover-seed"


@pytest.fixture(scope="module", autouse=True)
def _hygiene_module_engine():
    """测试卫生：本文件独占临时库在模块夹具内换绑、模块结束原样还原。

    导入期顶层换绑会让全局 engine/SessionLocal 在 collection 后停在本文件
    的临时库上，后面的文件/用例撞 no such table 或读错库（#1668 同族）；
    换绑只在本模块用例期间生效。
    """
    _hyg_saved_engine = _dbmod.engine
    _hyg_saved_factory = _dbmod.SessionLocal._factory
    _hyg_saved_url = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"
    _dbmod.engine = _ce(os.environ["DATABASE_URL"])
    _dbmod.configure_session_local(_sm(bind=_dbmod.engine))
    init_db()
    _hyg_engine = _dbmod.engine
    try:
        yield
    finally:
        _dbmod.engine = _hyg_saved_engine
        _dbmod.configure_session_local(_hyg_saved_factory)
        if _hyg_saved_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = _hyg_saved_url
        _hyg_engine.dispose()

def _guid(prefix):
    return f"{prefix}-{os.urandom(6).hex()}"


def _lib(db, name, **kw):
    lib = em.Library(name=name, guid=_guid("lib"), **kw)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _item(db, lib, name, **kw):
    item = em.MediaItem(name=name, guid=_guid("item"), library_id=lib.id, **kw)
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@pytest.fixture()
def db():
    """纯函数用例：只清 cmscover-pure 行（模块种子不受影响）。"""
    s = SessionLocal()
    try:
        libs = [r[0] for r in s.query(em.Library.id).filter(
            em.Library.name.like(f"{PURE}%")).all()]
        if libs:
            ids = [r[0] for r in s.query(em.MediaItem.id).filter(
                em.MediaItem.library_id.in_(libs)).all()]
            if ids:
                s.query(em.MediaItem).filter(
                    em.MediaItem.id.in_(ids)).delete(synchronize_session=False)
            s.query(em.Library).filter(em.Library.id.in_(libs)).delete(
                synchronize_session=False)
            s.commit()
        yield s
    finally:
        s.close()


def test_cms_borrows_representative_from_direct_members(db):
    """借代表：成员只认本库直属行；同身份只留最低 id，再取 id 最大且
    有海报者。别库的条目、隐藏行、合并副本、集行都不算数。"""
    lib = _lib(db, f"{PURE}-cms", kind="applecms")
    other = _lib(db, f"{PURE}-other")
    a = _item(db, lib, f"{PURE}-a", item_type="movie", tmdb_id="100",
              primary_image_url="https://img.example/a.jpg")
    _item(db, lib, f"{PURE}-a2", item_type="movie", tmdb_id="100")
    b = _item(db, lib, f"{PURE}-b", item_type="series", tmdb_id="200",
              primary_image_url="https://img.example/b.jpg")
    _item(db, other, f"{PURE}-alien", item_type="movie", tmdb_id="999",
          primary_image_url="https://img.example/alien.jpg")
    hidden = _item(db, lib, f"{PURE}-hidden", item_type="movie",
                   tmdb_id="300",
                   primary_image_url="https://img.example/h.jpg")
    hidden.is_hidden = True
    merged = _item(db, lib, f"{PURE}-merged", item_type="movie",
                   tmdb_id="400",
                   primary_image_url="https://img.example/m.jpg")
    merged.merged_into_id = a.id
    _item(db, lib, f"{PURE}-ep", item_type="episode", tmdb_id="500",
          primary_image_url="https://img.example/e.jpg")
    db.commit()
    res = library_covers.resolve_cms_library_cover(db, lib)
    assert res["source"] == "borrow"
    assert res["item_guid"] == b.guid
    assert res["tag"].startswith("vb-")


def test_cms_falls_back_to_first_poster_member(db):
    """回退：无已绑定代表时按 id 升序取首个有海报的直属成员。"""
    lib = _lib(db, f"{PURE}-cms2", kind="applecms")
    _item(db, lib, f"{PURE}-bound", item_type="series", tmdb_id="900")
    first = _item(db, lib, f"{PURE}-first", item_type="movie",
                  primary_image_url="https://img.example/first.jpg")
    _item(db, lib, f"{PURE}-second", item_type="movie",
          primary_image_url="https://img.example/second.jpg")
    res = library_covers.resolve_cms_library_cover(db, lib)
    assert res["source"] == "fallback"
    assert res["item_guid"] == first.guid
    assert res["tag"].startswith("vf-")


def test_cms_svg_when_no_poster_or_empty(db):
    """占位：成员全无海报 → svg；空 CMS 库 → svg。"""
    lib = _lib(db, f"{PURE}-cms3", kind="applecms")
    _item(db, lib, f"{PURE}-noposter", item_type="movie", tmdb_id="700")
    res = library_covers.resolve_cms_library_cover(db, lib)
    assert res["source"] == "svg"
    assert res["item_id"] is None and res["item_guid"] is None
    assert res["tag"].startswith("vs-")
    empty = _lib(db, f"{PURE}-cms4", kind="applecms")
    assert library_covers.resolve_cms_library_cover(db, empty)["source"] == "svg"


def test_five_piece_cms_derives_without_borrow_switch(db, monkeypatch):
    """五件套：借图总开关关着时，CMS 库无封面也派生（生产现状）；
    有代表海报 → borrow+远程 URL，无海报 → svg 占位 URL。"""
    monkeypatch.setattr(library_covers, "BORROW_ENABLED", 0)
    lib = _lib(db, f"{PURE}-cms5", kind="applecms")
    _item(db, lib, f"{PURE}-m", item_type="movie", tmdb_id="11",
          primary_image_url="https://img.example/m.jpg")
    result = library_covers.resolve_library_cover(db, lib, "poster")
    assert result == {"url": "https://img.example/m.jpg", "source": "borrow"}
    empty = _lib(db, f"{PURE}-cms6", kind="applecms")
    result2 = library_covers.resolve_library_cover(db, empty, "poster")
    assert result2["source"] == "svg"
    assert result2["url"].endswith("/cover-placeholder.svg")
    assert library_covers.resolve_library_cover(db, lib, "logo") == {
        "url": None, "source": "none"}


def test_five_piece_cms_explicit_cover_and_local_regression(db, monkeypatch):
    """五件套回归：CMS 库有显式 cover_path 时不走派生（旧口径）；
    本地库无封面在开关关时仍 none，本地库行为逐字不变。"""
    monkeypatch.setattr(library_covers, "BORROW_ENABLED", 0)
    lib = _lib(db, f"{PURE}-cms7", kind="applecms")
    _item(db, lib, f"{PURE}-m", item_type="movie", tmdb_id="12",
          primary_image_url="https://img.example/m2.jpg")
    lib.cover_path = "library-covers/x.webp"
    db.commit()
    assert library_covers.resolve_library_cover(db, lib, "poster")["source"] == "uploaded"
    local = _lib(db, f"{PURE}-local")
    _item(db, local, f"{PURE}-lm", item_type="movie", tmdb_id="13",
          primary_image_url="https://img.example/lm.jpg")
    assert library_covers.resolve_library_cover(db, local, "poster") == {
        "url": None, "source": "none"}


# ---------------- 模块级种子：Views 与出图用例 ----------------

@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    old = db.query(models.WebUser).filter(
        models.WebUser.username == UNAME).first()
    if old:
        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == old.id).delete(synchronize_session=False)
        db.query(models.WebUser).filter(
            models.WebUser.id == old.id).delete()
    for stale in db.query(em.Library).filter(
            em.Library.name.like(f"{SEED}%")).all():
        ids = [r[0] for r in db.query(em.MediaItem.id).filter(
            em.MediaItem.library_id == stale.id).all()]
        if ids:
            db.query(em.MediaItem).filter(
                em.MediaItem.id.in_(ids)).delete(synchronize_session=False)
        db.query(em.Library).filter(em.Library.id == stale.id).delete()
    db.commit()

    user = models.WebUser(username=UNAME, password_hash=hash_password(PW),
                          is_active=True, is_staff=True, emby_username=UNAME,
                          emby_password=hash_password(PW))
    db.add(user)
    # 借代表（本地海报真文件）：Views 标记 + 出图 200 都验它。
    img_dir = tempfile.mkdtemp(prefix="cmscover-img-")
    os.environ["EMBY_IMAGE_DIR"] = img_dir
    poster_path = os.path.join(img_dir, "rep-poster.png")
    with open(poster_path, "wb") as f:
        f.write(_PNG_1X1)
    cms_local = _lib(db, f"{SEED}-cmslocal", kind="applecms")
    _item(db, cms_local, f"{SEED}-rep", item_type="movie", tmdb_id="2001",
          poster_path=poster_path)
    # 无海报：Views 占位标记 + 出图 SVG。
    cms_svg = _lib(db, f"{SEED}-cmssvg", kind="applecms")
    _item(db, cms_svg, f"{SEED}-svg-item", item_type="movie", tmdb_id="3001")
    # 本地真实库回归：无封面仍无 Primary。
    real_no_cover = _lib(db, f"{SEED}-real-nocover")
    data = {
        "user_id": user.id,
        "cms_local": cms_local.guid,
        "cms_svg": cms_svg.guid,
        "real_no_cover": real_no_cover.guid,
        "png": _PNG_1X1,
    }
    db.close()
    yield data
    try:
        db = SessionLocal()
        for stale in db.query(em.Library).filter(
                em.Library.name.like(f"{SEED}%")).all():
            ids = [r[0] for r in db.query(em.MediaItem.id).filter(
                em.MediaItem.library_id == stale.id).all()]
            if ids:
                db.query(em.MediaItem).filter(
                    em.MediaItem.id.in_(ids)).delete(synchronize_session=False)
            db.query(em.Library).filter(em.Library.id == stale.id).delete()
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
    from fastapi.testclient import TestClient
    return TestClient(app)


@pytest.fixture(scope="module")
def token(client, seed):
    r = client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": UNAME, "Pw": PW},
        headers={"X-Emby-Authorization":
                 'MediaBrowser Client="CmsCoverTest", Device="iOS", '
                 'DeviceId="cmscover-dev-1", Version="1.0"'},
    )
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


def _views_entries(client, token, seed):
    r = client.get(f"/emby/Users/{seed['user_id']}/Views",
                   headers={"X-Emby-Token": token})
    assert r.status_code == 200, r.text
    return {it["Id"]: it for it in r.json()["Items"]}


def test_views_cms_borrow_gets_primary_tag(client, token, seed):
    """Views 接线：CMS 库（有海报成员）拿到派生 Primary 标记，tag 与
    resolve_cms_library_cover 一致，宽高比与虚拟库 borrow 同口径。"""
    entries = _views_entries(client, token, seed)
    v = entries[seed["cms_local"]]
    assert v["ImageTags"]["Primary"].startswith("vb-")
    assert v["PrimaryImageAspectRatio"] == pytest.approx(0.6666667)
    db = SessionLocal()
    lib = db.query(em.Library).filter(
        em.Library.guid == seed["cms_local"]).first()
    derived = library_covers.resolve_cms_library_cover(db, lib)
    db.close()
    assert v["ImageTags"]["Primary"] == derived["tag"]


def test_views_cms_svg_and_real_lib_regression(client, token, seed):
    """Views 接线：CMS 库无海报 → 占位标记 + 800/450；本地真实库无
    封面仍无 Primary 标记、无宽高比（回归）。"""
    entries = _views_entries(client, token, seed)
    v = entries[seed["cms_svg"]]
    assert v["ImageTags"]["Primary"].startswith("vs-")
    assert v["PrimaryImageAspectRatio"] == pytest.approx(800 / 450)
    real = entries[seed["real_no_cover"]]
    assert real["ImageTags"] == {}
    assert "PrimaryImageAspectRatio" not in real


def test_image_cms_borrow_serves_poster_bytes(client, token, seed):
    """出图接线：CMS 库按 guid 拉 Primary → 200 且是代表条目海报字节，
    不是只给标记不给图。"""
    r = client.get(f"/emby/Items/{seed['cms_local']}/Images/Primary",
                   headers={"X-Emby-Token": token})
    assert r.status_code == 200, r.text[:200]
    assert r.content == seed["png"]


def test_image_cms_svg_serves_placeholder(client, token, seed):
    """出图接线：CMS 库无海报 → 200 SVG 占位字节。"""
    r = client.get(f"/emby/Items/{seed['cms_svg']}/Images/Primary",
                   headers={"X-Emby-Token": token})
    assert r.status_code == 200, r.text[:200]
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.content == library_covers.SVG_PLACEHOLDER_BYTES


def test_image_cms_requires_credentials(client, seed):
    """PA-036 口径不破：库封面不在无凭据兼容范围内，CMS 库封面无
    凭据仍 401。"""
    r = client.get(f"/emby/Items/{seed['cms_local']}/Images/Primary")
    assert r.status_code == 401
