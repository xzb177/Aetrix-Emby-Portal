# -*- coding: utf-8 -*-
"""单集多版本（episode_versions）测试。

覆盖：
1. version_signature：从文件名提取版本签名（零 IO）
2. choose_primary：确定性主记录选择（分辨率 > 大小 > id）
3. merge_episode_versions：分组、合并、幂等、主记录删除后晋升、孤儿回收
4. 总开关 EPISODE_VERSIONS_ENABLED=0 时不做任何事
"""

import os
import sys

sys.path.insert(0, ".")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.emby_server import episode_versions as ev
from backend.emby_server import models as em
import backend.models  # noqa: F401 — 注册 web_users 等表，供 create_all 建表


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _mk(db, **kw):
    """造一个 episode 行。"""
    defaults = dict(
        guid=kw.pop("guid", f"g{kw.get('id', 0)}"),
        library_id=1,
        item_type="episode",
        name="S01E01",
        series_id=100,
        season_number=1,
        episode_number=1,
        file_path="/strm/剧/S01E01.strm",
    )
    defaults.update(kw)
    # id 显式指定时 SQLite 允许
    item = em.MediaItem(**defaults)
    db.add(item)
    db.commit()
    return item


# ---------- version_signature ----------

def test_version_signature_parses_quality_tags():
    sig = ev.version_signature("/strm/剧/第51集 - 2160p WEB-DL H265 DDP 2.0 - HDSWEB.strm")
    assert sig[0] == "2160p"
    assert sig[1] == "WEB-DL"
    assert sig[2] == "H.265"


def test_version_signature_empty_for_plain_name():
    assert ev.version_signature("/strm/剧/S01E01.strm") == (None, None, None)


def test_version_signature_none_safe():
    assert ev.version_signature(None) == (None, None, None)
    assert ev.version_signature("") == (None, None, None)


def test_version_signature_distinguishes_versions():
    a = ev.version_signature("/x/第51集 - 2160p WEB-DL.strm")
    b = ev.version_signature("/x/第51集 - 2160p WEB-DL H265 DDP 2.0 - HDSWEB.strm")
    assert a != b  # 同集不同版本签名必须不同，否则改名识别会互认


# ---------- choose_primary ----------

def test_choose_primary_prefers_higher_resolution(db):
    lo = _mk(db, id=1, guid="g1", height=720, size=100,
             file_path="/x/S01E01 - 720p.strm")
    hi = _mk(db, id=2, guid="g2", height=2160, size=50,
             file_path="/x/S01E01 - 2160p.strm")
    assert ev.choose_primary([lo, hi]).id == hi.id


def test_choose_primary_falls_back_to_filename(db):
    # 无探测数据时用文件名解析回退（零 IO）
    lo = _mk(db, id=1, guid="g1", height=0, size=100,
             file_path="/x/S01E01 - 720p.strm")
    hi = _mk(db, id=2, guid="g2", height=0, size=50,
             file_path="/x/S01E01 - 2160p.strm")
    assert ev.choose_primary([lo, hi]).id == hi.id


def test_choose_primary_tiebreak_by_id(db):
    a = _mk(db, id=1, guid="g1", height=1080, size=100, file_path="/x/a.strm")
    b = _mk(db, id=2, guid="g2", height=1080, size=100, file_path="/x/b.strm")
    # 分辨率、大小都相同 → id 小的赢，确定性
    assert ev.choose_primary([b, a]).id == a.id


# ---------- merge_episode_versions ----------

def test_merge_two_versions(db):
    v1 = _mk(db, id=1, guid="g1", height=720, size=100,
             file_path="/x/S01E01 - 720p.strm")
    v2 = _mk(db, id=2, guid="g2", height=2160, size=200,
             file_path="/x/S01E01 - 2160p.strm")
    stats = ev.merge_episode_versions(db, library_id=1)
    db.commit()
    assert stats["groups"] == 1
    assert stats["merged"] == 1
    db.refresh(v1)
    db.refresh(v2)
    # 2160p 成为主记录
    assert v2.merged_into_id is None
    assert v1.merged_into_id == v2.id


def test_merge_is_idempotent(db):
    _mk(db, id=1, guid="g1", height=720, file_path="/x/S01E01 - 720p.strm")
    _mk(db, id=2, guid="g2", height=2160, file_path="/x/S01E01 - 2160p.strm")
    ev.merge_episode_versions(db, library_id=1)
    stats = ev.merge_episode_versions(db, library_id=1)
    db.commit()
    assert stats["groups"] == 1  # 组还在（2 行可见）
    assert stats["merged"] == 0  # 但无需再合并


def test_merge_ignores_single_version(db):
    _mk(db, id=1, guid="g1", file_path="/x/S01E01.strm")
    stats = ev.merge_episode_versions(db, library_id=1)
    db.commit()
    assert stats["groups"] == 0


def test_merge_ignores_deleted(db):
    _mk(db, id=1, guid="g1", file_path="/x/S01E01 - 720p.strm")
    v2 = _mk(db, id=2, guid="g2", file_path="/x/S01E01 - 2160p.strm")
    from datetime import datetime
    v2.deleted_at = datetime.now()
    db.commit()
    stats = ev.merge_episode_versions(db, library_id=1)
    db.commit()
    assert stats["groups"] == 0


def test_merge_promotes_on_primary_deleted(db):
    from datetime import datetime
    v1 = _mk(db, id=1, guid="g1", height=720, file_path="/x/S01E01 - 720p.strm")
    v2 = _mk(db, id=2, guid="g2", height=2160, file_path="/x/S01E01 - 2160p.strm")
    ev.merge_episode_versions(db, library_id=1)
    db.commit()
    db.refresh(v1)
    db.refresh(v2)
    assert v1.merged_into_id == v2.id
    # 主记录（2160p）的文件被删
    v2.deleted_at = datetime.now()
    db.commit()
    stats = ev.merge_episode_versions(db, library_id=1)
    db.commit()
    db.refresh(v1)
    # 720p 晋升为主记录，恢复独立（主记录已删 → 孤儿回收）
    assert v1.merged_into_id is None
    assert stats["unmerged_orphans"] == 1


def test_merge_respects_library_boundary(db):
    _mk(db, id=1, guid="g1", library_id=1, file_path="/x/S01E01 - 720p.strm")
    _mk(db, id=2, guid="g2", library_id=2, file_path="/x/S01E01 - 2160p.strm")
    stats = ev.merge_episode_versions(db, library_id=1)
    db.commit()
    assert stats["groups"] == 0  # 跨库不合并


def test_merge_does_not_touch_movies(db):
    _mk(db, id=1, guid="g1", item_type="movie", series_id=None,
        season_number=None, episode_number=None, file_path="/x/Movie - 720p.strm")
    _mk(db, id=2, guid="g2", item_type="movie", series_id=None,
        season_number=None, episode_number=None, file_path="/x/Movie - 2160p.strm")
    stats = ev.merge_episode_versions(db, library_id=1)
    db.commit()
    assert stats["groups"] == 0  # 电影走 merge_versions_worker，不管


def test_merge_disabled_by_env(db, monkeypatch):
    # 直接 patch 模块常量（不 reload 模块：reload 在全量测试进程里有副作用风险）
    monkeypatch.setattr(ev, "EPISODE_VERSIONS_ENABLED", False)
    _mk(db, id=1, guid="g1", file_path="/x/S01E01 - 720p.strm")
    _mk(db, id=2, guid="g2", file_path="/x/S01E01 - 2160p.strm")
    stats = ev.merge_episode_versions(db, library_id=1)
    db.commit()
    assert stats == {"groups": 0, "merged": 0, "promoted": 0,
                     "unmerged_orphans": 0}


def test_get_version_group(db):
    v1 = _mk(db, id=1, guid="g1", height=720, file_path="/x/S01E01 - 720p.strm")
    v2 = _mk(db, id=2, guid="g2", height=2160, file_path="/x/S01E01 - 2160p.strm")
    ev.merge_episode_versions(db, library_id=1)
    group = ev.get_version_group(db, v1)
    assert {r.id for r in group} == {v1.id, v2.id}


# ---------- P0: _guid_held_by_other ----------

def test_guid_held_by_other(db):
    from backend.emby_server import scanner as _sc
    _mk(db, id=1, guid="g1", file_path="/x/a.strm")
    _mk(db, id=2, guid="g2", file_path="/x/b.strm")
    # g1 被 id=1 占用，对 row 2 来说是"别人"
    assert _sc._guid_held_by_other(db, "g1", 2) == 1
    # 对自己不算占用
    assert _sc._guid_held_by_other(db, "g1", 1) is None
    # 不存在的 guid
    assert _sc._guid_held_by_other(db, "gX", 1) is None


def test_guid_held_by_other_sees_soft_deleted(db):
    from datetime import datetime
    from backend.emby_server import scanner as _sc
    _mk(db, id=1, guid="g1", file_path="/x/a.strm")
    v2 = _mk(db, id=2, guid="g2", file_path="/x/b.strm")
    v2.deleted_at = datetime.now()
    db.commit()
    # 软删除的行仍占唯一键，必须算占用（否则 UPDATE 照样撞库）
    assert _sc._guid_held_by_other(db, "g2", 1) == 2
