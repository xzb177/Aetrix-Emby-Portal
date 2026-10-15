import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
import sys
import importlib.util
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as bm
from backend.emby_server import models as em

TestingSessionLocal = sessionmaker(
    bind=create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
)


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    bm.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    TestingSessionLocal.configure(bind=engine)
    session = TestingSessionLocal()
    yield session
    session.close()


def test_backfill_processes_ledger_items_after_non_ledger_items(db, monkeypatch):
    lib = em.Library(guid="lib1", name="t")
    db.add(lib)
    db.flush()

    db.add_all([
        em.MediaItem(guid="t1", library_id=lib.id, item_type="movie",
                     name="t1", file_path="/strm/a1.strm", size=100),
        em.MediaItem(guid="t2", library_id=lib.id, item_type="movie",
                     name="t2", file_path="/strm/a2.strm", size=100),
        em.MediaItem(guid="t3", library_id=lib.id, item_type="movie",
                     name="t3", file_path="/strm/a3.strm", size=100),
        em.MediaItem(guid="t4", library_id=lib.id, item_type="episode",
                     name="t4", file_path="/strm/a4.strm", size=100),
    ])

    db.add_all([
        em.StrmGenFile(drive_id="d1", remote_path="r3", file_id="f3",
                       strm_path="a3.strm", size=2000000),
        em.StrmGenFile(drive_id="d1", remote_path="r4", file_id="f4",
                       strm_path="a4.strm", size=3000000),
    ])
    db.commit()

    spec = importlib.util.spec_from_file_location("bf", "scripts/backfill_strm_video_size.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    monkeypatch.setattr(sys, "argv", ["bf", "--apply", "--batch-size", "2"])
    monkeypatch.setattr(mod, "SessionLocal", TestingSessionLocal)

    assert mod.main() == 0

    db.expire_all()
    by_guid = {i.guid: i for i in db.query(em.MediaItem).all()}
    assert by_guid["t3"].size == 2000000
    assert by_guid["t4"].size == 3000000
    assert by_guid["t1"].size == 100
    assert by_guid["t2"].size == 100
