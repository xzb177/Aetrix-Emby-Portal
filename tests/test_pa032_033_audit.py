"""PA-032/033 回归：管理端 artwork 4 写口与备份 2 写口必须留审计。

隔离内存 SQLite，直接调端点函数；不碰生产库、不调真实备份。
"""

import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend import models  # noqa: E402
from backend.api import admin_artwork, admin_services  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.integrations import store  # noqa: E402


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()
    engine.dispose()


def _admin(db):
    admin = models.WebUser(username="admin", password_hash="x",
                           is_active=True, is_staff=True)
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def _item(db):
    lib = em.Library(name="库", guid="lib-pa032")
    db.add(lib)
    db.commit()
    item = em.MediaItem(name="片", guid="item-pa032", library_id=lib.id,
                        item_type="movie")
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _logs(db, action):
    return db.query(models.AdminLog).filter(
        models.AdminLog.action == action).all()


# ==================== PA-032：artwork 4 写口 ====================

def test_set_override_audited(db):
    admin, item = _admin(db), _item(db)
    admin_artwork.set_override(
        admin_artwork.OverrideIn(item_id=item.id, kind="Primary",
                                 image_url="https://img.example/a.jpg"),
        admin=admin, db=db)
    rows = _logs(db, "artwork_override_set")
    assert len(rows) == 1
    assert rows[0].admin_user_id == admin.id
    assert rows[0].target_id == item.id


def test_delete_override_audited(db):
    admin, item = _admin(db), _item(db)
    admin_artwork.set_override(
        admin_artwork.OverrideIn(item_id=item.id, kind="Primary",
                                 image_url="https://img.example/a.jpg"),
        admin=admin, db=db)
    admin_artwork.delete_override(item.id, "Primary", admin=admin, db=db)
    rows = _logs(db, "artwork_override_delete")
    assert len(rows) == 1
    assert rows[0].admin_user_id == admin.id
    assert rows[0].target_id == item.id


def test_cover_strategy_audited(db):
    admin = _admin(db)
    admin_artwork.set_cover_strategy(
        admin_artwork.CoverStrategyIn(order=["borrow", "render"]),
        admin=admin, db=db)
    rows = _logs(db, "cover_strategy_save")
    assert len(rows) == 1
    assert rows[0].admin_user_id == admin.id
    assert rows[0].details["steps"] == 2


def test_cover_mirror_retry_audited(db):
    admin = _admin(db)
    row = em.CoverMirror(source_hash="hash-pa032", status="failed",
                         source_url="https://img.example/cover.jpg")
    db.add(row)
    db.commit()
    admin_artwork.cover_mirror_retry(
        admin_artwork.CoverMirrorRetryIn(source_hash="hash-pa032"),
        admin=admin, db=db)
    rows = _logs(db, "cover_mirror_retry")
    assert len(rows) == 1
    assert rows[0].admin_user_id == admin.id
    assert rows[0].details["source_hash"] == "hash-pa032"


# ==================== PA-033：备份 2 写口 ====================

def test_backup_config_audited(db):
    admin = _admin(db)
    admin_services.update_backup_config(
        admin_services.BackupConfigIn(enabled=True, time="03:00",
                                      keep_days=7),
        admin=admin, db=db)
    rows = _logs(db, "backup_config_update")
    assert len(rows) == 1
    assert rows[0].admin_user_id == admin.id
    assert rows[0].details["keep_days"] == 7


def test_backup_run_audited(db, monkeypatch):
    from backend.emby_server import db_backup
    monkeypatch.setattr(db_backup, "get_config",
                        lambda db: {"keep_days": 7})
    monkeypatch.setattr(db_backup, "run_backup_now",
                        lambda reason: {"name": "x.dump.gz", "size": 1})
    monkeypatch.setattr(db_backup, "prune_old_backups", lambda n: 3)
    monkeypatch.setattr(db_backup, "list_backups", lambda: [])
    admin = _admin(db)
    admin_services.run_backup_now(admin=admin, db=db)
    rows = _logs(db, "backup_run_manual")
    assert len(rows) == 1
    assert rows[0].admin_user_id == admin.id
    assert rows[0].details["name"] == "x.dump.gz"
    assert rows[0].details["pruned"] == 3
