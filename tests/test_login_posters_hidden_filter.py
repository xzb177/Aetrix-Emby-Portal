"""登录页海报墙 PA-011 补漏回归。

口径：is_hidden=true 且 deleted_at IS NULL 的带海报条目（观看时间恢复的
隐形墓碑形态）不许进公开海报墙候选；普通可见条目照常入选。
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from unittest import mock

from backend.api import site
from backend.emby_server import models as em


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    em.MediaItem.__table__.create(engine, checkfirst=True)
    session = sessionmaker(bind=engine)()
    # 海报墙有进程内缓存与 single-flight 锁，逐测重置
    site._login_posters_cache = {"entries": {}}
    if site._login_posters_lock.locked():
        try:
            site._login_posters_lock.release()
        except RuntimeError:
            pass
    saved = {}
    for key in ("EMBY_PUBLIC_URL", "FORWARDED_ALLOW_IPS", "TRUSTED_PROXIES"):
        saved[key] = os.environ.get(key)
        os.environ.pop(key, None)
    yield session
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    session.close()
    engine.dispose()


def _movie(db, name, *, hidden=False, seq=0):
    it = em.MediaItem(
        guid=f"{seq:032x}",
        library_id=58,
        item_type="movie",
        name=name,
        poster_path=f"/posters/{name}.jpg",
        is_hidden=hidden,
    )
    db.add(it)
    db.commit()
    return it


def test_hidden_movie_with_poster_not_in_login_posters(db):
    _movie(db, "隐形墓碑片", hidden=True, seq=1)
    visible = _movie(db, "正常可见片", hidden=False, seq=2)

    with mock.patch.object(site.realms, "active_realm_id", return_value=None), \
            mock.patch.object(site, "resolve_emby_base_url", return_value=""):
        result = site.get_login_posters(db=db, request=None)

    names = [item["name"] for item in result["items"]]
    assert "正常可见片" in names
    assert "隐形墓碑片" not in names
    assert [item["id"] for item in result["items"]] == [visible.guid]
