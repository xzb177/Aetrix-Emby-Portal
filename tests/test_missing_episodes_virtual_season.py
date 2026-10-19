"""桶B 虚拟季（缺集普查 2026-10-19）：有可见集但无可见季行的季，
get_seasons 合成虚拟季条目返回；get_episodes 认虚拟季 Id 按季号筛集。

场景：
- 死壳季：季行被合并/软删（不可见），集可见 → 补「第 N 季」
- 无季行：季行根本不存在，集可见 → 补「第 N 季」
- 正常剧：季行齐全 → 返回内容与排序不变，不多不少
"""
import inspect
import os
import tempfile
import uuid
from datetime import datetime
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmppath

from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

from backend.database import init_db  # noqa: E402

init_db()

import pytest  # noqa: E402

from backend.emby_server import api  # noqa: E402
from backend.emby_server import models as em  # noqa: E402


def _mk(name, item_type, **kw):
    return em.MediaItem(guid=uuid.uuid4().hex, name=name, item_type=item_type,
                        library_id=1, date_added=datetime.now(), **kw)


@pytest.fixture(autouse=True)
def _no_scope(monkeypatch):
    # 测试只验季列表逻辑：用户对全库可见
    monkeypatch.setattr(api, "_library_scope", lambda db, user: None)


@pytest.fixture()
def world():
    """A：露面行《某剧》(tmdb T1)
    - S1 真季行（2 集可见）
    - S2 死壳季：季行 merged_into S1（不可见），集挂在 S2 季下仍可见
    - S3 无季行：3 集直接可见
    - S4 真季行但 0 集（不触发虚拟季，也不受影响）
    """
    db = _dbmod.SessionLocal()
    h = {}
    try:
        A = _mk("某剧", "series", tmdb_id="T1", production_year=2024)
        db.add(A)
        db.flush()
        h["A"] = A
        S1 = _mk("第 1 季", "season", series_id=A.id, parent_id=A.id, season_number=1)
        S4 = _mk("第 4 季", "season", series_id=A.id, parent_id=A.id, season_number=4)
        db.add_all([S1, S4])
        db.flush()
        h["S1"], h["S4"] = S1, S4
        for i in (1, 2):
            db.add(_mk(f"E{i}", "episode", series_id=A.id, parent_id=S1.id,
                       season_number=1, episode_number=i))
        # 死壳季 S2：季行被合并进 S1
        S2 = _mk("第 2 季", "season", series_id=A.id, parent_id=A.id,
                 season_number=2, merged_into_id=S1.id)
        db.add(S2)
        db.flush()
        h["S2"] = S2
        for i in (1, 2, 3):
            db.add(_mk(f"S2E{i}", "episode", series_id=A.id, parent_id=S2.id,
                       season_number=2, episode_number=i))
        # S3：无季行，集可见
        for i in (1,):
            db.add(_mk(f"S3E{i}", "episode", series_id=A.id, parent_id=A.id,
                       season_number=3, episode_number=i))
        db.commit()
        yield h
        db.query(em.MediaItem).delete()
        db.commit()
    finally:
        db.close()


def _req(qs=None):
    return SimpleNamespace(base_url="http://t/", query_params=qs or {})


def _user():
    return SimpleNamespace(id=1, username="t")


def test_virtual_seasons_synthesized(world):
    """死壳季 S2 + 无季行 S3 都应补虚拟季；S1/S4 真季行原样。"""
    db = _dbmod.SessionLocal()
    try:
        out = api.get_seasons(world["A"].guid, _req(), _user(), db)
        items = out["Items"]
        assert out["TotalRecordCount"] == 4, [i["Name"] for i in items]
        names = [i["Name"] for i in items]
        assert names == ["第 1 季", "第 2 季", "第 3 季", "第 4 季"], names
        v2 = items[1]
        assert v2["Type"] == "Season" and v2["IsFolder"] is True
        assert v2["Id"].startswith("vseason-")
        assert v2["IndexNumber"] == 2
        assert v2["ChildCount"] == 3
        assert v2["SeriesId"] == world["A"].guid
        assert v2["SeriesName"] == "某剧"
        v3 = items[2]
        assert v3["IndexNumber"] == 3 and v3["ChildCount"] == 1
        # 真季行的 DTO 不动：S1 仍是真实 guid，S4 ChildCount 仍为 0/无该键
        assert items[0]["Id"] == world["S1"].guid
        assert items[3]["Id"] == world["S4"].guid
    finally:
        db.close()


def test_normal_series_unchanged(world):
    """季行齐全的剧：只删虚拟季触发条件后，返回与改前口径一致。"""
    db = _dbmod.SessionLocal()
    try:
        # 删掉 S2/S3 的集 → 无缺口
        db.query(em.MediaItem).filter(
            em.MediaItem.item_type == "episode",
            em.MediaItem.season_number.in_([2, 3])).delete()
        db.commit()
        out = api.get_seasons(world["A"].guid, _req(), _user(), db)
        assert out["TotalRecordCount"] == 2
        assert [i["Id"] for i in out["Items"]] == [world["S1"].guid, world["S4"].guid]
    finally:
        db.close()


def test_episodes_with_virtual_season_id(world):
    """带虚拟季 Id 取集：只回该季的集。"""
    db = _dbmod.SessionLocal()
    try:
        vid = api._virtual_season_id(world["A"].id, 2)
        out = api.get_episodes(world["A"].guid, _req({"SeasonId": vid}), _user(), db)
        assert out["TotalRecordCount"] == 3
        assert {e["IndexNumber"] for e in out["Items"]} == {1, 2, 3}
        # 伪造/过期虚拟季 Id（无可见集）→ 沿用旧兜底：返回本剧全部集
        bad = api._virtual_season_id(world["A"].id, 9)
        out2 = api.get_episodes(world["A"].guid, _req({"SeasonId": bad}), _user(), db)
        assert out2["TotalRecordCount"] == 6
        # 垃圾 Id → 同旧兜底
        out3 = api.get_episodes(world["A"].guid, _req({"SeasonId": "nope"}),
                                _user(), db)
        assert out3["TotalRecordCount"] == 6
    finally:
        db.close()


def test_parse_virtual_season_id():
    assert api._parse_virtual_season_id("vseason-12-3") == (12, 3)
    assert api._parse_virtual_season_id("vseason-12-0") == (12, 0)
    assert api._parse_virtual_season_id("vseason-0-3") is None
    assert api._parse_virtual_season_id("vseason-12--1") is None
    assert api._parse_virtual_season_id("vseason-abc-3") is None
    assert api._parse_virtual_season_id("abcdef123456") is None
    assert api._parse_virtual_season_id(None) is None


def test_episode_scope_shared():
    """get_episodes 与 _missing_season_numbers 必须用同一基础集口径：
    两处都经 _episode_base_filters 组装，漂移时这里先红。"""
    src_ep = inspect.getsource(api.get_episodes)
    src_missing = inspect.getsource(api._missing_season_numbers)
    src_helper = inspect.getsource(api._episode_base_filters)
    assert "_episode_base_filters(item, series_ids)" in src_ep
    assert "_episode_base_filters(item, series_ids)" in src_missing
    for frag in ("series_id.in_(series_ids)", 'item_type == "episode"',
                 "merged_into_id.is_(None)"):
        assert frag in src_helper, f"基础口径疑似变化：缺 {frag!r}"
