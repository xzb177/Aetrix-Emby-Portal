"""任务一：虚拟库派生封面（借代表海报 / 回退 / SVG 占位）。

隔离临时 SQLite，不连生产库。成员口径与 facets.count_virtual_items
一致（平台聚合、movie/series、排除 is_hidden）；两条成员查询分支
（ItemFacet 关联表 / platforms ILIKE 回退）都要测到。

分支控制纪律（用例间顺序无关）：
- ILIKE 用例开头把回填水位 item_facets_backfill_max_id 置 0（库里有
  条目时 ready() 为 False → 走 ILIKE）；关联表用例开头给自己的条目
  写 facet 行并把水位置到全库最大 id（ready() 为 True → 走关联表）。
- 模块级种子（Views / 出图用例）两边都留证据：条目既写 platforms
  又同步 facet 行，所以无论水位在哪边，种子成员集都一致。
- 纯函数用例只动 vcover-pure% 行，模块种子只动 vcover-seed% 行，
  互不删除对方；ItemFacet 孤儿行随条目一起删（SQLite rowid 复用
  撞唯一键的旧坑，见 test_emby_views_compat.py）。
"""

import base64
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import facets, library_covers
from backend.emby_server import models as em
from backend.security import hash_password

init_db()

# 1x1 PNG（出图用例：代表条目本地海报文件）。
_PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

UNAME = "vcover_seed_user"
PW = "vcover-seed-pw"
PURE = "vcover-pure"
SEED = "vcover-seed"


def _guid(prefix):
    return f"{prefix}-{os.urandom(6).hex()}"


def _set_watermark(db, value):
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == facets.BACKFILL_KEY).first()
    if row is None:
        db.add(models.SystemConfig(key=facets.BACKFILL_KEY, value=str(value)))
    else:
        row.value = str(value)
    db.commit()


def _max_item_id(db):
    row = db.query(em.MediaItem.id).order_by(em.MediaItem.id.desc()).first()
    return row[0] if row else 0


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


def _del_items(db, ids):
    if ids:
        db.query(em.ItemFacet).filter(
            em.ItemFacet.item_id.in_(ids)).delete(synchronize_session=False)
        db.query(em.MediaItem).filter(
            em.MediaItem.id.in_(ids)).delete(synchronize_session=False)
        db.commit()


@pytest.fixture()
def db():
    """纯函数用例：只清 vcover-pure 行（模块种子不受影响）。"""
    s = SessionLocal()
    try:
        libs = [r[0] for r in s.query(em.Library.id).filter(
            em.Library.name.like(f"{PURE}%")).all()]
        if libs:
            ids = [r[0] for r in s.query(em.MediaItem.id).filter(
                em.MediaItem.library_id.in_(libs)).all()]
            _del_items(s, ids)
            s.query(em.Library).filter(em.Library.id.in_(libs)).delete(
                synchronize_session=False)
            s.commit()
        yield s
    finally:
        s.close()


def _pure_setup(db, platform):
    """纯函数用例种子：宿主真实库 + 该平台的虚拟库行（仅 resolve 用，
    不走 Views；虚拟库行本身不要求 is_virtual，resolve 只读 platform）。"""
    host = _lib(db, f"{PURE}-host")
    vlib = _lib(db, f"{PURE}-vlib", is_virtual=True, platform=platform)
    return host, vlib


def test_virtual_borrows_representative_poster_ilike(db):
    """借代表（ILIKE 分支）：同身份键只留最低 id 行，再取 id 最大且有
    海报的代表；无身份行、代表内无海报行都不抢。"""
    _set_watermark(db, 0)
    host, vlib = _pure_setup(db, "pure-plat")
    a = _item(db, host, f"{PURE}-a", item_type="movie", platforms="pure-plat",
              tmdb_id="100", primary_image_url="https://img.example/a.jpg")
    _item(db, host, f"{PURE}-a2", item_type="movie", platforms="pure-plat",
          tmdb_id="100")  # 同身份、无海报：不抢代表
    b = _item(db, host, f"{PURE}-b", item_type="movie", platforms="pure-plat",
              tmdb_id="200", primary_image_url="https://img.example/b.jpg")
    _item(db, host, f"{PURE}-c", item_type="movie", platforms="pure-plat",
          primary_image_url="https://img.example/c.jpg")  # 无身份：不当代表
    hidden = _item(db, host, f"{PURE}-hidden", item_type="movie",
                   platforms="pure-plat", tmdb_id="300",
                   primary_image_url="https://img.example/h.jpg")
    hidden.is_hidden = True
    db.commit()
    res = library_covers.resolve_virtual_library_cover(db, vlib)
    assert res["source"] == "borrow"
    assert res["item_guid"] == b.guid
    assert res["tag"].startswith("vb-")
    assert a.guid != res["item_guid"]


def test_virtual_falls_back_to_first_poster_member_ilike(db):
    """回退（ILIKE 分支）：无已绑定代表时，按 id 升序取首个有海报成员；
    已绑定但无海报者不挡回退。"""
    _set_watermark(db, 0)
    host, vlib = _pure_setup(db, "pure-plat2")
    bound_no_poster = _item(db, host, f"{PURE}-bound", item_type="series",
                            platforms="pure-plat2", tmdb_id="900")
    first = _item(db, host, f"{PURE}-first", item_type="movie",
                  platforms="pure-plat2",
                  primary_image_url="https://img.example/first.jpg")
    _item(db, host, f"{PURE}-second", item_type="movie",
          platforms="pure-plat2",
          primary_image_url="https://img.example/second.jpg")
    res = library_covers.resolve_virtual_library_cover(db, vlib)
    assert res["source"] == "fallback"
    assert res["item_guid"] == first.guid
    assert res["tag"].startswith("vf-")
    assert bound_no_poster.guid != res["item_guid"]


def test_virtual_svg_when_no_poster_ilike(db):
    """占位（ILIKE 分支）：成员存在但全无海报 → svg；平台为空 → svg。"""
    _set_watermark(db, 0)
    host, vlib = _pure_setup(db, "pure-plat3")
    _item(db, host, f"{PURE}-noposter", item_type="movie",
          platforms="pure-plat3", tmdb_id="700")
    res = library_covers.resolve_virtual_library_cover(db, vlib)
    assert res["source"] == "svg"
    assert res["item_id"] is None and res["item_guid"] is None
    assert res["tag"].startswith("vs-")
    empty = _lib(db, f"{PURE}-empty", is_virtual=True, platform="")
    res2 = library_covers.resolve_virtual_library_cover(db, empty)
    assert res2["source"] == "svg"


def test_virtual_borrows_via_facet_table(db):
    """借代表（关联表分支）：facet 行已同步且水位到顶时，走关联表也
    借到同一代表；与 ILIKE 分支结果一致。"""
    host, vlib = _pure_setup(db, "pure-plat4")
    a = _item(db, host, f"{PURE}-fa", item_type="movie",
              platforms="pure-plat4", tmdb_id="101",
              primary_image_url="https://img.example/fa.jpg")
    _item(db, host, f"{PURE}-fa2", item_type="movie",
          platforms="pure-plat4", tmdb_id="101")
    b = _item(db, host, f"{PURE}-fb", item_type="movie",
              platforms="pure-plat4", tmdb_id="202",
              primary_image_url="https://img.example/fb.jpg")
    for it in (a, b):
        facets.sync_item(db, it)
    db.commit()
    _set_watermark(db, _max_item_id(db))
    assert facets.ready(db) is True
    res = library_covers.resolve_virtual_library_cover(db, vlib)
    assert res["source"] == "borrow"
    assert res["item_guid"] == b.guid


# ---------------- 模块级种子：Views 与出图用例 ----------------

@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    # 先清上一轮残留（中途挂掉时清理没跑，保证可重复跑）。
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
            db.query(em.ItemFacet).filter(
                em.ItemFacet.item_id.in_(ids)).delete(synchronize_session=False)
            db.query(em.MediaItem).filter(
                em.MediaItem.id.in_(ids)).delete(synchronize_session=False)
        db.query(em.Library).filter(em.Library.id == stale.id).delete()
    db.commit()

    user = models.WebUser(username=UNAME, password_hash=hash_password(PW),
                          is_active=True, is_staff=True, emby_username=UNAME,
                          emby_password=hash_password(PW))
    db.add(user)
    host = _lib(db, f"{SEED}-host")
    real_with_cover = _lib(db, f"{SEED}-real-cover",
                           cover_path="library-covers/fake-seed.webp")
    real_no_cover = _lib(db, f"{SEED}-real-nocover")

    # Views 用虚拟库：借代表（远程海报）。
    v_remote = _lib(db, f"{SEED}-vremote", is_virtual=True,
                    platform="seed-remote")
    _item(db, host, f"{SEED}-ra", item_type="movie",
          platforms="seed-remote", tmdb_id="1001",
          primary_image_url="https://img.example/ra.jpg")
    _item(db, host, f"{SEED}-rb", item_type="movie",
          platforms="seed-remote", tmdb_id="1002",
          primary_image_url="https://img.example/rb.jpg")

    # 出图用虚拟库：代表条目本地海报（EMBY_IMAGE_DIR 内的真文件）。
    img_dir = tempfile.mkdtemp(prefix="vcover-img-")
    os.environ["EMBY_IMAGE_DIR"] = img_dir
    poster_path = os.path.join(img_dir, "rep-poster.png")
    with open(poster_path, "wb") as f:
        f.write(_PNG_1X1)
    v_local = _lib(db, f"{SEED}-vlocal", is_virtual=True, platform="seed-local")
    _item(db, host, f"{SEED}-local-rep", item_type="movie",
          platforms="seed-local", tmdb_id="2001", poster_path=poster_path)

    # 出图 / Views 用虚拟库：全无海报 → SVG 占位。
    v_svg = _lib(db, f"{SEED}-vsvg", is_virtual=True, platform="seed-svg")
    _item(db, host, f"{SEED}-svg-item", item_type="movie",
          platforms="seed-svg", tmdb_id="3001")

    # 种子成员 facet 行 + 水位到顶（关联表分支下种子成员集也一致）。
    for it in db.query(em.MediaItem).filter(
            em.MediaItem.name.like(f"{SEED}%")).all():
        facets.sync_item(db, it)
    db.commit()
    _set_watermark(db, _max_item_id(db))
    data = {
        "user_id": user.id,
        "host": host.guid,
        "real_with_cover": real_with_cover.guid,
        "real_no_cover": real_no_cover.guid,
        "v_remote": v_remote.guid,
        "v_local": v_local.guid,
        "v_svg": v_svg.guid,
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
                db.query(em.ItemFacet).filter(
                    em.ItemFacet.item_id.in_(ids)).delete(
                    synchronize_session=False)
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
                 'MediaBrowser Client="VcoverTest", Device="iOS", '
                 'DeviceId="vcover-dev-1", Version="1.0"'},
    )
    assert r.status_code == 200, r.text
    return r.json()["AccessToken"]


def _views_entries(client, token, seed):
    r = client.get(f"/emby/Users/{seed['user_id']}/Views",
                   headers={"X-Emby-Token": token})
    assert r.status_code == 200, r.text
    items = r.json()["Items"]
    return {it["Id"]: it for it in items}


def test_views_virtual_borrow_gets_primary_tag(client, token, seed):
    """Views 接线：虚拟库（借代表）拿到 Primary 标记与海报宽高比；
    其 tag 与 resolve_virtual_library_cover 的派生 tag 一致。"""
    entries = _views_entries(client, token, seed)
    v = entries[seed["v_remote"]]
    assert v["ImageTags"]["Primary"].startswith("vb-")
    assert v["PrimaryImageAspectRatio"] == pytest.approx(0.6666667)
    db = SessionLocal()
    lib = db.query(em.Library).filter(
        em.Library.guid == seed["v_remote"]).first()
    derived = library_covers.resolve_virtual_library_cover(db, lib)
    db.close()
    assert v["ImageTags"]["Primary"] == derived["tag"]


def test_views_virtual_svg_gets_placeholder_aspect(client, token, seed):
    """Views 接线：虚拟库（无海报）拿到占位标记，宽高比为占位图本身
    的 800/450。"""
    entries = _views_entries(client, token, seed)
    v = entries[seed["v_svg"]]
    assert v["ImageTags"]["Primary"].startswith("vs-")
    assert v["PrimaryImageAspectRatio"] == pytest.approx(800 / 450)


def test_views_real_libraries_unchanged(client, token, seed):
    """真实库不受影响：有 cover_path 的仍用旧 tag + 0.6666667；无
    cover_path 的仍无 Primary 标记、无宽高比。"""
    entries = _views_entries(client, token, seed)
    with_cover = entries[seed["real_with_cover"]]
    assert with_cover["ImageTags"]["Primary"] == "1"  # 文件不存在 → 旧 tag 回退
    assert with_cover["PrimaryImageAspectRatio"] == pytest.approx(0.6666667)
    no_cover = entries[seed["real_no_cover"]]
    assert no_cover["ImageTags"] == {}
    assert "PrimaryImageAspectRatio" not in no_cover


def test_image_virtual_borrow_serves_representative_poster(client, token, seed):
    """出图接线：虚拟库 Primary 出代表条目本地海报原图。"""
    r = client.get(f"/emby/Items/{seed['v_local']}/Images/Primary",
                   headers={"X-Emby-Token": token})
    assert r.status_code == 200, r.text[:200]
    assert r.content == seed["png"]


def test_image_virtual_svg_serves_placeholder(client, token, seed):
    """出图接线：虚拟库无海报时 Primary 直接回 SVG 占位字节。"""
    r = client.get(f"/emby/Items/{seed['v_svg']}/Images/Primary",
                   headers={"X-Emby-Token": token})
    assert r.status_code == 200, r.text[:200]
    assert r.headers["content-type"].startswith("image/svg+xml")
    assert r.content == library_covers.SVG_PLACEHOLDER_BYTES
