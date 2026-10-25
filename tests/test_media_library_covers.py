"""媒体库学习 L1：库封面链（代表海报 → 自研渲染 → SVG 占位）。

隔离临时 SQLite，不连生产库。策略顺序每次调用现读 env（D2 调序
不用重启）；开关是模块常量，测试 patch 后复原。
"""

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

from backend.database import SessionLocal, init_db
from backend.emby_server import image_store, library_covers
from backend.emby_server import models as em

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        for table in (em.MediaItem, em.Library):
            s.query(table).delete()
        s.commit()
        yield s
    finally:
        s.close()


def _lib(db, name="库"):
    lib = em.Library(name=name, guid=f"lib-{name}-{os.urandom(4).hex()}")
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _item(db, lib, name, tmdb=None, imdb=None, poster=None, remote=None):
    item = em.MediaItem(
        name=name, guid=f"item-{os.urandom(6).hex()}",
        library_id=lib.id, item_type="movie",
        tmdb_id=tmdb, imdb_id=imdb,
        poster_path=poster, primary_image_url=remote,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def test_representative_groups_by_identity_and_prefers_newest_with_poster(db, monkeypatch):
    """同身份键只留最低 id 行；多代表取 id 最大且有海报的一个；无身份不作代表。"""
    monkeypatch.setattr(library_covers, "BORROW_ENABLED", 1)
    lib = _lib(db, "代表")
    a = _item(db, lib, "A", tmdb="1", remote="https://img.example/a.jpg")
    _item(db, lib, "A2", tmdb="1")  # 同身份，无海报，不抢代表
    b = _item(db, lib, "B", tmdb="2", remote="https://img.example/b.jpg")
    _item(db, lib, "C", remote="https://img.example/c.jpg")  # 无身份，不作代表
    rep = library_covers.resolve_library_representative(db, lib.id)
    assert rep.id == b.id
    assert rep.id != a.id


def test_cover_falls_back_to_svg_when_no_representative_and_no_cover(db, monkeypatch):
    monkeypatch.setattr(library_covers, "BORROW_ENABLED", 1)
    lib = _lib(db, "空库")
    result = library_covers.resolve_library_cover(db, lib, "poster")
    assert result["source"] == "svg"
    assert result["url"].endswith("/cover-placeholder.svg")


def test_cover_legacy_when_switch_off(db, monkeypatch):
    """开关关：只回旧行为（直传/渲染封面），不借代表、不出占位策略。"""
    monkeypatch.setattr(library_covers, "BORROW_ENABLED", 0)
    lib = _lib(db, "旧行为")
    lib.cover_path = "library-covers/x.webp"
    db.commit()
    assert library_covers.resolve_library_cover(db, lib, "poster")["source"] == "uploaded"
    lib.cover_template = "poster"
    db.commit()
    assert library_covers.resolve_library_cover(db, lib, "poster")["source"] == "render"
    lib.cover_path = None
    lib.cover_template = None
    db.commit()
    assert library_covers.resolve_library_cover(db, lib, "poster") == {"url": None, "source": "none"}


def test_cover_strategy_order_reorderable_and_unknown_tokens_dropped(db, monkeypatch):
    """策略顺序可调：svg 在前时即使有代表也先出占位；未知步骤丢弃。"""
    monkeypatch.setattr(library_covers, "BORROW_ENABLED", 1)
    lib = _lib(db, "调序")
    _item(db, lib, "A", tmdb="9", remote="https://img.example/a.jpg")
    monkeypatch.setenv("LIBRARY_COVER_STRATEGY_ORDER", "svg,borrow")
    assert library_covers.resolve_library_cover(db, lib, "poster")["source"] == "svg"
    monkeypatch.setenv("LIBRARY_COVER_STRATEGY_ORDER", "bogus,borrow")
    assert library_covers.resolve_library_cover(db, lib, "poster")["source"] == "borrow"


def test_cover_logo_always_none(db, monkeypatch):
    monkeypatch.setattr(library_covers, "BORROW_ENABLED", 1)
    lib = _lib(db, "logo")
    lib.cover_path = "library-covers/x.webp"
    _item(db, lib, "A", tmdb="9", remote="https://img.example/a.jpg")
    db.commit()
    assert library_covers.resolve_library_cover(db, lib, "logo") == {"url": None, "source": "none"}


def test_cover_borrow_local_poster_uses_images_route(db, monkeypatch, tmp_path):
    """代表本地缓存图（文件在）→ Images 路由，不是直接给本地路径。"""
    monkeypatch.setattr(library_covers, "BORROW_ENABLED", 1)
    monkeypatch.setattr(image_store, "image_dir", lambda: str(tmp_path))
    poster = tmp_path / "a.jpg"
    poster.write_bytes(b"fake")
    lib = _lib(db, "本地")
    item = _item(db, lib, "A", tmdb="7", poster=str(poster))
    url = library_covers._item_poster_url(item)
    assert url == f"/emby/Items/{item.guid}/Images/Primary"
