"""季重复与 series ChildCount 协议口径回归（2026-11-08 现场）。

现场形状（日韩剧库《模范出租车》）：
- 正主剧行名下没有任何季行；合并分身剧行名下有 3 行季，season_number 全 NULL，
  其中「第 3 季」有两行，S3 的集被拆在两行下。
- 旧 get_seasons：NULL 季行不进 present，又按集季号合成 2 张虚拟季，
  3 真行 + 2 虚拟 = 5 张重复卡；真行之间也不去重。
- 旧 series ChildCount 填集数（32），客户端按协议把 ChildCount 当季数，
  显示「共32季」；集数应放 RecursiveItemCount。

本文件断言修复后口径：
- Seasons 恰好 2 条（第2季、第3季各一、无虚拟季混入）
- 点代表季行拉 Episodes 能拿到该季全部集（含挂重复行下的那集）
- 剧 DTO ChildCount=季数、RecursiveItemCount=集数
- 季 DTO IndexNumber = 有效季号（NULL 时名字解析兜底）
"""
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
                        library_id=45, date_added=datetime.now(), **kw)


@pytest.fixture(autouse=True)
def _no_scope(monkeypatch):
    monkeypatch.setattr(api, "_library_scope", lambda db, user: None)


@pytest.fixture()
def world():
    """正主 A（tmdb 119769）+ 合并分身 B（merged_into_id=A）。

    - A 名下 0 行季；B 名下 3 行季（season_number 全 NULL）：
      S2「第 2 季」一行、S3a「第 3 季」（挂 2 集）、S3b「第 3 季」（挂 1 集，
      即 S3 的第 3 集——挂在被去重压掉的重复行下）。
    - 集 series_id 全为 A：S2 2 集、S3 共 3 集，总集数 5。
    """
    db = _dbmod.SessionLocal()
    h = {}
    try:
        A = _mk("模范出租车", "series", tmdb_id="119769", production_year=2021)
        db.add(A)
        db.flush()
        B = _mk("模范出租车", "series", tmdb_id="119769", production_year=2021,
                merged_into_id=A.id)
        db.add(B)
        db.flush()
        h["A"], h["B"] = A, B
        S2 = _mk("第 2 季", "season", series_id=B.id, parent_id=B.id,
                 season_number=None)
        S3a = _mk("第 3 季", "season", series_id=B.id, parent_id=B.id,
                  season_number=None)
        S3b = _mk("第 3 季", "season", series_id=B.id, parent_id=B.id,
                  season_number=None)
        db.add_all([S2, S3a, S3b])
        db.flush()
        h["S2"], h["S3a"], h["S3b"] = S2, S3a, S3b
        for i in (1, 2):
            db.add(_mk(f"S2E{i}", "episode", series_id=A.id, parent_id=S2.id,
                       season_number=2, episode_number=i))
        for i in (1, 2):
            db.add(_mk(f"S3E{i}", "episode", series_id=A.id, parent_id=S3a.id,
                       season_number=3, episode_number=i))
        db.add(_mk("S3E3", "episode", series_id=A.id, parent_id=S3b.id,
                   season_number=3, episode_number=3))
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


def test_seasons_deduped_no_virtual(world):
    db = _dbmod.SessionLocal()
    try:
        out = api.get_seasons(world["A"].guid, _req(), _user(), db)
        items = out["Items"]
        assert out["TotalRecordCount"] == 2, [i["Name"] for i in items]
        assert [i["Name"] for i in items] == ["第 2 季", "第 3 季"]
        assert [i["IndexNumber"] for i in items] == [2, 3]
        # 代表行：S3 取集多的 S3a；两条都必须是真行 guid、无虚拟季混入
        assert items[0]["Id"] == world["S2"].guid
        assert items[1]["Id"] == world["S3a"].guid
        assert not any(i["Id"].startswith("vseason-") for i in items)
        # 代表季的 ChildCount 是该季全部集（含重复行下的集）
        assert items[1]["ChildCount"] == 3, items[1]
    finally:
        db.close()


def test_episodes_via_representative_include_duplicate_row_eps(world):
    db = _dbmod.SessionLocal()
    try:
        out = api.get_episodes(
            world["A"].guid, _req({"SeasonId": world["S3a"].guid}),
            _user(), db)
        assert out["TotalRecordCount"] == 3, out["Items"]
        assert {e["IndexNumber"] for e in out["Items"]} == {1, 2, 3}
        out2 = api.get_episodes(
            world["A"].guid, _req({"SeasonId": world["S2"].guid}),
            _user(), db)
        assert out2["TotalRecordCount"] == 2
    finally:
        db.close()


def test_series_childcount_is_season_count(world):
    db = _dbmod.SessionLocal()
    try:
        dto = api._item_dto(world["A"], "http://t/", 1, db)
        assert dto["ChildCount"] == 2, dto.get("ChildCount")
        assert dto["RecursiveItemCount"] == 5, dto.get("RecursiveItemCount")
    finally:
        db.close()


def test_series_childcount_prefetch_matches_fallback(world):
    db = _dbmod.SessionLocal()
    try:
        api._prefetch_list_data(db, 1, [world["A"]])
        dto = api._item_dto(world["A"], "http://t/", 1, db)
        assert dto["ChildCount"] == 2, dto.get("ChildCount")
        assert dto["RecursiveItemCount"] == 5, dto.get("RecursiveItemCount")
    finally:
        db.close()


def test_season_dto_index_number_from_name(world):
    db = _dbmod.SessionLocal()
    try:
        dto3 = api._item_dto(world["S3a"], "http://t/", 1, db)
        assert dto3["IndexNumber"] == 3, dto3.get("IndexNumber")
        dto2 = api._item_dto(world["S2"], "http://t/", 1, db)
        assert dto2["IndexNumber"] == 2, dto2.get("IndexNumber")
    finally:
        db.close()


def test_effective_season_number_helper():
    row = SimpleNamespace(season_number=None, name="第 12 季", id=99)
    assert api._effective_season_number(row) == 12
    row2 = SimpleNamespace(season_number=None, name="Season 4", id=98)
    assert api._effective_season_number(row2) == 4
    row3 = SimpleNamespace(season_number=None, name="特别篇", id=97)
    assert api._effective_season_number(row3) is None
    # 名下集众数兜底：名字解析不出时用集的季号众数
    assert api._effective_season_number(
        row3, ep_numbers_by_parent={97: [5, 5, 2]}) == 5
    # season_number 非空恒优先
    row4 = SimpleNamespace(season_number=7, name="第 1 季", id=96)
    assert api._effective_season_number(row4) == 7
