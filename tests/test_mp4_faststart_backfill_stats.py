# -*- coding: utf-8 -*-
"""run_backfill 计数口径（第三轮第 4 路审 #1381 时发现）。

回归：此前 already_front 结果既不计 rewritten 也不计 kept，
summary 里 processed 比 rewritten + kept 多，运维看 summary 会
以为有任务丢了。该结果真实可达：重写成功但 mark_moov_front 记库
失败（模块明言容忍）后，下一轮库里仍是 moov=back，加工时实测
文件头已在前即返回 already_front。本文件钉住 rewritten + kept
== processed，且 already_front 计 kept（原件原样保留）。
全部离线：process 用桩替身，不碰真实 Drive、不碰生产库。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import mp4_faststart as fs


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv(fs.BACKFILL_ENV, "1")
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    lib = em.Library(guid="glib", name="电影", collection_type="movies",
                     paths="", storage_backends="")
    session.add(lib)
    session.commit()
    session.add(em.MediaItem(
        guid="back-big", library_id=lib.id, name="back-big",
        item_type="movie", container="mp4", moov_position="back",
        size=3 * 1024 * 1024 * 1024, file_path="mount://3/a.mp4",
        drive_file_id="fid123"))
    session.commit()
    yield session
    session.close()


def test_backfill_counts_already_front_as_kept(db):
    summary = fs.run_backfill(
        db, apply=True,
        process=lambda job: {"ok": True, "reason": "already_front",
                             "skipped": True})
    assert summary["ran"] is True
    assert summary["candidates"] == 1
    assert summary["processed"] == 1
    assert summary["rewritten"] == 0
    assert summary["kept"] == 1
    assert summary["rewritten"] + summary["kept"] == summary["processed"]


def test_backfill_failed_still_counts_as_kept(db):
    summary = fs.run_backfill(
        db, apply=True,
        process=lambda job: {"ok": False, "reason": "no"})
    assert summary["rewritten"] == 0
    assert summary["kept"] == 1
    assert summary["rewritten"] + summary["kept"] == summary["processed"]
