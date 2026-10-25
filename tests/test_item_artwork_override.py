"""媒体库学习 L2：条目图人工覆盖（覆盖表 + 开关门控）。

隔离临时 SQLite。读侧（get/override_url）受开关门控；写侧（set/
cancel）不受开关影响（可先配好再开开关）。
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
from backend.emby_server import item_artwork
from backend.emby_server import models as em

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        for table in (em.MediaItemArtworkOverride, em.MediaItem, em.Library):
            s.query(table).delete()
        s.commit()
        yield s
    finally:
        s.close()


def _item(db):
    lib = em.Library(name="库", guid=f"lib-{os.urandom(4).hex()}")
    db.add(lib)
    db.commit()
    item = em.MediaItem(
        name="片", guid=f"item-{os.urandom(6).hex()}",
        library_id=lib.id, item_type="movie",
        primary_image_url="https://img.example/old.jpg",
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def test_set_get_cancel_roundtrip(db, monkeypatch):
    monkeypatch.setattr(item_artwork, "OVERRIDE_ENABLED", 1)
    item = _item(db)
    row = item_artwork.set_override(db, item.id, "Primary", "https://img.example/new.jpg")
    assert row.item_id == item.id
    assert item_artwork.override_url(db, item.id, "Primary") == "https://img.example/new.jpg"
    assert item_artwork.override_url(db, item.id, "Backdrop") is None
    # upsert：再设同一键只更新，不新增行
    item_artwork.set_override(db, item.id, "Primary", "https://img.example/new2.jpg")
    assert db.query(em.MediaItemArtworkOverride).count() == 1
    assert item_artwork.override_url(db, item.id, "Primary") == "https://img.example/new2.jpg"
    assert item_artwork.cancel_override(db, item.id, "Primary") is True
    assert item_artwork.override_url(db, item.id, "Primary") is None
    assert item_artwork.cancel_override(db, item.id, "Primary") is False


def test_read_gated_by_switch_write_not(db, monkeypatch):
    """开关关：读侧一律 None；写侧照常写（开开关后即生效）。"""
    item = _item(db)
    monkeypatch.setattr(item_artwork, "OVERRIDE_ENABLED", 0)
    item_artwork.set_override(db, item.id, "Primary", "https://img.example/new.jpg")
    assert item_artwork.override_url(db, item.id, "Primary") is None
    monkeypatch.setattr(item_artwork, "OVERRIDE_ENABLED", 1)
    assert item_artwork.override_url(db, item.id, "Primary") == "https://img.example/new.jpg"


def test_set_rejects_bad_kind_and_non_http_url(db):
    item = _item(db)
    with pytest.raises(ValueError):
        item_artwork.set_override(db, item.id, "Logo", "https://img.example/x.jpg")
    with pytest.raises(ValueError):
        item_artwork.set_override(db, item.id, "Primary", "/tmp/evil.jpg")
    with pytest.raises(ValueError):
        item_artwork.set_override(db, item.id, "Primary", "javascript:alert(1)")
    assert db.query(em.MediaItemArtworkOverride).count() == 0
