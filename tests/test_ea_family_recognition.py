"""EA Shows 接口的「合并家族认亲」：分身行名下的季/集应对露面行可达。

生产实证背景：一部剧在库里常有多个 series 行——露面行
（merged_into_id 为空）+ 若干被合并隐藏的分身行（merged_into_id 指向
露面行，可多层）。真集常挂在分身的季下。

旧问题有二：
1. get_seasons 只查 ``series_id == item.id`` 的季——分身的季不返回，
   客户端看到空季列表；
2. get_episodes 靠 find_duplicate_ids 认亲（同库 + tmdb 相同，或同库 +
   归一化名相同 + 年份 ±1）：分身名带年份后缀「(2024)」时归一化后多了
   数字对不上；露面行有 tmdb 时只走 tmdb 路径，同名但 tmdb 为空的
   未刮削分身也对不上（生产实测 3,747 部受此影响）。带露面季 SeasonId
   时 ``_season_belongs_to`` 判不属于本剧 → 以前掉进「忽略筛选返回全部」
   的兜底。

修复：``api._family_series_ids`` 沿 merged_into_id 反向 BFS 组装家族
series_id 集合（同库 + tmdb 相等/归一化名相等，准入只对露面行判定，
不链式放宽）；get_episodes/get_seasons 用该集合扩口径，家族季视为属于本剧。
"""
import os
import tempfile
import uuid
from datetime import datetime

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy import or_  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

from backend.database import init_db  # noqa: E402

init_db()

import pytest  # noqa: E402

from backend.emby_server import api  # noqa: E402
from backend.emby_server import dedup as dedup_lib  # noqa: E402
from backend.emby_server import models as em  # noqa: E402


def _mk(name, item_type, **kw):
    return em.MediaItem(guid=uuid.uuid4().hex, name=name, item_type=item_type,
                        library_id=1, date_added=datetime.now(), **kw)


@pytest.fixture()
def world():
    """造一整套家族/反例场景。返回名字到条目的 dict。

    - A 露面行《某剧》(tmdb T1)；A_season 自有季（0 集）
    - B 分身《某剧 (2024)》(tmdb T1) merged_into A；S 季(series_id=B) 下 2 集
    - B2 分身《某剧》(tmdb 空) merged_into A；S2 季(series_id=B2) 下 1 集
      （隔离：dedup 的 tmdb 路径抓不到 B2，只能靠家族认亲）
    - C 孙行《某剧》(tmdb 空) merged_into B；SC 季(series_id=C) 下 1 集
    - D 同名不同 tmdb、未合并《某剧》(tmdb T2)；SD 季下 1 集（反例）
    - W 错合并行《完全不同》(tmdb TW) merged_into A；SW 季下 1 集（反例）
    - A2/B2 互指环（反例：防环）
    """
    db = _dbmod.SessionLocal()
    h = {}
    try:
        A = _mk("某剧", "series", tmdb_id="T1", production_year=2024)
        db.add(A)
        db.flush()
        h["A"] = A
        h["A_season"] = _mk("第 1 季", "season", series_id=A.id, parent_id=A.id,
                            season_number=1)
        db.add(h["A_season"])

        B = _mk("某剧 (2024)", "series", tmdb_id="T1", production_year=2024,
                merged_into_id=A.id)
        db.add(B)
        db.flush()
        h["B"] = B
        S = _mk("第 1 季", "season", series_id=B.id, parent_id=B.id,
                season_number=1)
        db.add(S)
        db.flush()
        h["S"] = S
        for i in range(2):
            db.add(_mk(f"B 第 {i + 1} 集", "episode", series_id=B.id,
                       parent_id=S.id, season_number=1, episode_number=i + 1))

        B2 = _mk("某剧", "series", tmdb_id=None, production_year=2024,
                 merged_into_id=A.id)
        db.add(B2)
        db.flush()
        h["B2"] = B2
        S2 = _mk("第 1 季", "season", series_id=B2.id, parent_id=B2.id,
                 season_number=1)
        db.add(S2)
        db.flush()
        h["S2"] = S2
        db.add(_mk("B2 第 1 集", "episode", series_id=B2.id, parent_id=S2.id,
                   season_number=1, episode_number=1))

        C = _mk("某剧", "series", tmdb_id=None, production_year=2024,
                merged_into_id=B.id)
        db.add(C)
        db.flush()
        h["C"] = C
        SC = _mk("第 1 季", "season", series_id=C.id, parent_id=C.id,
                 season_number=1)
        db.add(SC)
        db.flush()
        h["SC"] = SC
        db.add(_mk("C 第 1 集", "episode", series_id=C.id, parent_id=SC.id,
                   season_number=1, episode_number=1))

        D = _mk("某剧", "series", tmdb_id="T2", production_year=2024)
        db.add(D)
        db.flush()
        h["D"] = D
        SD = _mk("第 1 季", "season", series_id=D.id, parent_id=D.id,
                 season_number=1)
        db.add(SD)
        db.flush()
        h["SD"] = SD
        db.add(_mk("D 第 1 集", "episode", series_id=D.id, parent_id=SD.id,
                   season_number=1, episode_number=1))

        W = _mk("完全不同", "series", tmdb_id="TW", production_year=2020,
                merged_into_id=A.id)
        db.add(W)
        db.flush()
        h["W"] = W
        SW = _mk("第 1 季", "season", series_id=W.id, parent_id=W.id,
                 season_number=1)
        db.add(SW)
        db.flush()
        h["SW"] = SW
        db.add(_mk("W 第 1 集", "episode", series_id=W.id, parent_id=SW.id,
                   season_number=1, episode_number=1))

        A2 = _mk("甲", "series", tmdb_id="TA")
        B2c = _mk("乙", "series", tmdb_id="TB")
        db.add_all([A2, B2c])
        db.flush()
        A2.merged_into_id = B2c.id
        B2c.merged_into_id = A2.id
        db.flush()
        h["A2"] = A2
        h["B2c"] = B2c

        db.commit()
        yield h
        db.query(em.MediaItem).delete()
        db.commit()
    finally:
        db.close()


def _series_ids(db, show):
    """复刻 get_episodes/get_seasons 的 series_ids 组装口径"""
    ids = [show.id]
    if show.item_type == "series":
        dup_ids = dedup_lib.find_duplicate_ids(
            db, show.library_id, "series",
            tmdb_id=show.tmdb_id, name=show.name,
            year=show.production_year)
        ids = list(set(ids + dup_ids))
        ids = list(set(ids) | api._family_series_ids(db, show))
    return ids


def _seasons(db, show):
    """复刻 get_seasons 的查询"""
    return (db.query(em.MediaItem)
            .filter(em.MediaItem.series_id.in_(_series_ids(db, show)),
                    em.MediaItem.item_type == "season",
                    em.MediaItem.merged_into_id.is_(None))
            .order_by(em.MediaItem.season_number).all())


def _episodes(db, show, season_id=None):
    """复刻 get_episodes 的查询（含家族季归属判定）"""
    series_ids = _series_ids(db, show)
    query = db.query(em.MediaItem).filter(
        or_(em.MediaItem.series_id.in_(series_ids),
            em.MediaItem.parent_id == show.id),
        em.MediaItem.item_type == "episode",
        em.MediaItem.merged_into_id.is_(None))
    if season_id:
        season = db.query(em.MediaItem).filter(
            em.MediaItem.guid == season_id).first()
        family_season = (
            show.item_type == "series"
            and season is not None
            and season.item_type == "season"
            and season.series_id in series_ids
        )
        if season and (api._season_belongs_to(season, show) or family_season):
            query = query.filter(em.MediaItem.parent_id == season.id)
        # else：兜底——忽略不匹配的 SeasonId，返回本剧全部集（口径不变）
    return query.all()


def test_family_series_ids_admits_clones(world):
    """家族集合：同 tmdb 的 B、同名的 B2、多层的 C 都入集"""
    db = _dbmod.SessionLocal()
    try:
        fam = api._family_series_ids(db, world["A"])
        assert world["A"].id in fam
        assert world["B"].id in fam, "同 tmdb 的分身应入集"
        assert world["B2"].id in fam, "同名（tmdb 空）的分身应入集"
        assert world["C"].id in fam, "多层孙行应入集"
        assert world["D"].id not in fam, "未合并的同名异 tmdb 行不入集"
        assert world["W"].id not in fam, "错合并的异名异 tmdb 行不入集"
    finally:
        db.close()


def test_family_seasons_listed(world):
    """get_seasons 应列出分身的季（含 tmdb 为空、只能靠家族认亲的 B2 季）"""
    db = _dbmod.SessionLocal()
    try:
        seasons = _seasons(db, world["A"])
        guids = {s.guid for s in seasons}
        assert world["S"].guid in guids
        assert world["S2"].guid in guids
        assert world["SC"].guid in guids
        assert world["SD"].guid not in guids, "反例剧的季不能串进来"
        assert world["SW"].guid not in guids, "错合并行的季不能串进来"
    finally:
        db.close()


def test_family_episodes_reachable_with_and_without_season_id(world):
    """带分身季 SeasonId / 不带，都应返回家族全部集"""
    db = _dbmod.SessionLocal()
    try:
        with_season = _episodes(db, world["A"], season_id=world["S"].guid)
        assert len(with_season) == 2, "家族季应视为属于本剧并按季过滤"
        names = {e.name for e in with_season}
        assert names == {"B 第 1 集", "B 第 2 集"}

        all_eps = _episodes(db, world["A"])
        names = {e.name for e in all_eps}
        assert names == {"B 第 1 集", "B 第 2 集", "B2 第 1 集", "C 第 1 集"}, \
            "不带 SeasonId 应返回家族全部集"
    finally:
        db.close()


def test_name_only_clone_reachable_dedup_misses(world):
    """B2：露面行有 tmdb 时 dedup 只走 tmdb 路径抓不到 B2，只能靠家族认亲"""
    db = _dbmod.SessionLocal()
    try:
        dup = dedup_lib.find_duplicate_ids(
            db, 1, "series", tmdb_id="T1", name="某剧", year=2024)
        assert world["B2"].id not in dup, "前置：dedup 确实抓不到 B2"
        eps = _episodes(db, world["A"], season_id=world["S2"].guid)
        assert {e.name for e in eps} == {"B2 第 1 集"}
    finally:
        db.close()


def test_multilevel_grandchild_reachable(world):
    """孙行 C（merged_into B → A）的季可达"""
    db = _dbmod.SessionLocal()
    try:
        eps = _episodes(db, world["A"], season_id=world["SC"].guid)
        assert {e.name for e in eps} == {"C 第 1 集"}
    finally:
        db.close()


def test_no_chain_relaxation(world):
    """不链式放宽：C2 只像 B（不像露面行 A）时不入集"""
    db = _dbmod.SessionLocal()
    try:
        # C2：tmdb 与 B 相同(T1)但与 A…这里构造更尖锐的：
        # C2 与 B 同名但 A 叫别的名字——用独立小世界避免污染主场景
        A = _mk("剧A", "series", tmdb_id="T1")
        db.add(A)
        db.flush()
        Bx = _mk("剧A", "series", tmdb_id=None, merged_into_id=A.id)
        db.add(Bx)
        db.flush()
        C2 = _mk("完全无关名", "series", tmdb_id="TBx", merged_into_id=Bx.id)
        # 让 C2 只像 Bx：给 Bx 和 C2 一个共同 tmdb，但与 A 不同
        Bx.tmdb_id = "TSHARED"
        C2.tmdb_id = "TSHARED"
        db.flush()
        fam = api._family_series_ids(db, A)
        assert Bx.id in fam, "Bx 与 A 同名应入集"
        assert C2.id not in fam, "C2 只像 Bx 不像 A，不许链式放宽"
        db.query(em.MediaItem).filter(
            em.MediaItem.id.in_([A.id, Bx.id, C2.id])).delete(
                synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_unmerged_same_name_different_tmdb_falls_back(world):
    """反例：未合并的同名异 tmdb 剧 D，其季传进来仍走兜底（不串剧）"""
    db = _dbmod.SessionLocal()
    try:
        eps = _episodes(db, world["A"], season_id=world["SD"].guid)
        names = {e.name for e in eps}
        assert "D 第 1 集" not in names, "D 的集不能串进 A"
        assert names == {"B 第 1 集", "B 第 2 集", "B2 第 1 集", "C 第 1 集"}, \
            "兜底：返回本剧全部集"
    finally:
        db.close()


def test_wrongly_merged_dissimilar_not_recognized(world):
    """反例：名字完全不同且 tmdb 不同的错合并行 W，不被认亲"""
    db = _dbmod.SessionLocal()
    try:
        eps = _episodes(db, world["A"], season_id=world["SW"].guid)
        names = {e.name for e in eps}
        assert "W 第 1 集" not in names
        assert names == {"B 第 1 集", "B 第 2 集", "B2 第 1 集", "C 第 1 集"}
    finally:
        db.close()


def test_merged_away_season_not_listed_twice(world):
    """已被合并到别的季的分身季（merged_into 非空）不重复列出"""
    db = _dbmod.SessionLocal()
    try:
        Sdup = _mk("第 1 季", "season", series_id=world["B"].id,
                   parent_id=world["B"].id, season_number=1,
                   merged_into_id=world["S"].id)
        db.add(Sdup)
        db.commit()
        guids = {s.guid for s in _seasons(db, world["A"])}
        assert Sdup.guid not in guids
        assert world["S"].guid in guids
        db.delete(Sdup)
        db.commit()
    finally:
        db.close()


def test_cycle_terminates(world):
    """互指环必须终止，不入集、不死循环"""
    db = _dbmod.SessionLocal()
    try:
        fam = api._family_series_ids(db, world["A2"])
        assert fam == {world["A2"].id}
    finally:
        db.close()


def test_depth_cap_bounds_traversal():
    """10 层链：BFS 在上限层截断，不会无限走下去"""
    db = _dbmod.SessionLocal()
    try:
        prev = _mk("连", "series", tmdb_id=None)
        db.add(prev)
        db.flush()
        root_id = prev.id
        chain = []
        for _ in range(10):
            node = _mk("连", "series", tmdb_id=None, merged_into_id=prev.id)
            db.add(node)
            db.flush()
            chain.append(node)
            prev = node
        db.commit()
        root = db.query(em.MediaItem).filter(
            em.MediaItem.id == root_id).one()
        fam = api._family_series_ids(db, root)
        assert len(fam) == 1 + api._FAMILY_BFS_MAX_DEPTH
        assert chain[-1].id not in fam, "第 10 层超出上限，不入集"
        assert chain[api._FAMILY_BFS_MAX_DEPTH - 1].id in fam
        db.query(em.MediaItem).filter(
            em.MediaItem.id.in_([root_id] + [n.id for n in chain])).delete(
                synchronize_session=False)
        db.commit()
    finally:
        db.close()
