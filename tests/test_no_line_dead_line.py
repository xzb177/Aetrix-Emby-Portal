"""全线已死条目自动隐藏（清理二期，2026-11-05 用户亲定）回归。

背景：第一批（test_no_line_hidden.py）藏的是「整树没有任何启用源
线路」的条目。本批处理另一类：源还开着、但探活（2026-11-04/05
单线全量探活，口径＝应用取流路径 resolve_mapped_upstream_stream）
确认整部片每条线都已死（dead/gone）的条目。

锁住的口径：
- 候选：无界（kind=applecms）库的顶层活行、当前可见；子树全部
  映射都在探活结果里且结论全 ∈ {dead, gone}；至少一条映射的源
  当前 enabled。源停用的不进本批（那是第一批 reason 的地盘）；
  多线未全探的（数据缺失）不进；已藏的不进。
- 落藏前新鲜复检：探活数据会旧，每条线按同一取流路径再探一次，
  仍全 dead/gone 才藏；复检出 live（活过来了）不藏；复检出
  unknown（非播放类错误、判不了）也不藏——没实锤不许藏。
- 台账用新 reason=all_lines_dead，与第一批 no_enabled_line 严格
  分开：第一批对账不许 claimed / 不许解藏本批条目，本批轮转复检
  也不许动第一批在册条目。
- 解藏靠有界轮转复检：每轮至多 limit（硬顶 500）条在册条目，
  按藏入先后轮转；任一条线复检 live 即解藏。同步轮收尾的挂接
  带最小间隔节流，不许每轮全量复检。
- 幂等：重复落藏/复检不产生新台账行、不重复计数。

测试形态照 test_no_line_hidden.py：种子直落 app 库；复检探针用
注入的假函数（mapping_id → 结论），真实取流分类另有专门用例
monkeypatch resolve 本身验证。
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy.orm import Session

import backend.models as base_models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import no_line_hidden as nlh
from backend.integrations import store

init_db()

SRC_ON = "dlh_启用源"
SRC_OFF = "dlh_停用源"
LIB_MOVIES = "dlh_电影库"
LIB_TV = "dlh_剧集库"

# 候选判定种子（只读用例共用）
C_DEAD = "dlh_全死电影甲"
C_DEADGONE = "dlh_全死电影乙"
C_LIVE = "dlh_活线电影"
C_DEADOFF = "dlh_停用源全死电影"
C_MULTI = "dlh_多线半覆盖电影"
C_UNPROBED = "dlh_未探电影"
C_NOMAP = "dlh_无映射电影"
C_MANUAL = "dlh_手工藏全死电影"
C_SDEAD = "dlh_全死剧"
C_SPART = "dlh_半探剧"
CANDIDATE_SEED_NAMES = [
    C_DEAD, C_DEADGONE, C_LIVE, C_DEADOFF, C_MULTI, C_UNPROBED,
    C_NOMAP, C_MANUAL, C_SDEAD, "dlh_全死剧季", "dlh_全死剧集一",
    "dlh_全死剧集二", C_SPART, "dlh_半探剧季", "dlh_半探剧集一",
    "dlh_半探剧集二",
]

# 变更类用例自建条目的名字（各自 finally 清）
OWN_NAMES: list[str] = []

RECHECK_KEY = "all_lines_dead_recheck_last_at"


def _guid() -> str:
    return uuid.uuid4().hex


def _purge(db: Session, names: list[str]) -> None:
    if names:
        rows = db.query(em.MediaItem).filter(
            em.MediaItem.name.in_(names)).all()
        ids = [r.id for r in rows]
        if ids:
            db.query(em.ItemHiddenLedger).filter(
                em.ItemHiddenLedger.item_id.in_(ids)
            ).delete(synchronize_session=False)
            db.query(em.UpstreamItemMapping).filter(
                em.UpstreamItemMapping.local_item_id.in_(ids)
            ).delete(synchronize_session=False)
            db.query(em.MediaItem).filter(
                em.MediaItem.id.in_(ids)).delete(synchronize_session=False)
    db.commit()


def _cleanup(db: Session) -> None:
    _purge(db, CANDIDATE_SEED_NAMES + OWN_NAMES)
    db.query(em.UpstreamSource).filter(
        em.UpstreamSource.name.in_([SRC_ON, SRC_OFF])).delete(
        synchronize_session=False)
    db.query(em.Library).filter(
        em.Library.name.in_([LIB_MOVIES, LIB_TV])).delete(
        synchronize_session=False)
    row = db.query(base_models.SystemConfig).filter_by(
        key=RECHECK_KEY).first()
    if row is not None:
        db.delete(row)
    db.commit()
    store.invalidate(RECHECK_KEY)


def _item(db, lib, name, item_type="movie", parent=None, series=None,
          hidden=False):
    it = em.MediaItem(guid=_guid(), library_id=lib.id, item_type=item_type,
                      name=name, parent_id=parent.id if parent else None,
                      series_id=series.id if series else None,
                      is_hidden=hidden)
    db.add(it)
    db.flush()
    return it


def _map(db, item, src, up_id):
    m = em.UpstreamItemMapping(
        local_item_id=item.id, source_id=src.id, upstream_item_id=up_id,
        confidence=0.9, evidence="tmdb")
    db.add(m)
    db.flush()
    return m


def _fresh(item_id):
    db = SessionLocal()
    try:
        row = db.get(em.MediaItem, item_id)
        return row.is_hidden if row else None
    finally:
        db.close()


def _ledger(item_id, reason=None):
    db = SessionLocal()
    try:
        q = db.query(em.ItemHiddenLedger).filter(
            em.ItemHiddenLedger.item_id == item_id)
        if reason is not None:
            q = q.filter(em.ItemHiddenLedger.reason == reason)
        return [(r.reason, r.restored_at is not None) for r in q.all()]
    finally:
        db.close()


def _fake_probe(table, calls=None):
    def probe(db, local_item_id, mapping_id):
        if calls is not None:
            calls.append(mapping_id)
        return table.get(mapping_id, "unknown")
    return probe


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    _cleanup(db)

    def _src(name, enabled):
        s = em.UpstreamSource(
            name=name, source_type="applecms",
            base_url=f"https://{name}.example.com", auth_method="api_key",
            enabled=enabled)
        db.add(s)
        db.flush()
        return s

    src_on = _src(SRC_ON, True)
    src_off = _src(SRC_OFF, False)

    def _lib(name, ctype):
        lib = em.Library(guid=_guid(), name=name, kind="applecms",
                         collection_type=ctype, is_enabled=True,
                         upstream_source_ids=f"{src_on.id},{src_off.id}")
        db.add(lib)
        db.flush()
        return lib

    lib_m = _lib(LIB_MOVIES, "movies")
    lib_tv = _lib(LIB_TV, "tvshows")

    items, verdicts = {}, {}

    def movie(name, src, verdict, hidden=False):
        it = _item(db, lib_m, name, hidden=hidden)
        m = _map(db, it, src, f"vod-{name}")
        if verdict is not None:
            verdicts[m.id] = verdict
        items[name] = it.id
        return m

    movie(C_DEAD, src_on, "dead")
    movie(C_DEADGONE, src_on, "gone")
    movie(C_LIVE, src_on, "live")
    movie(C_DEADOFF, src_off, "dead")
    m_multi1 = movie(C_MULTI, src_on, "dead")
    _map(db, db.get(em.MediaItem, items[C_MULTI]), src_on, "vod-multi-2")
    movie(C_UNPROBED, src_on, None)
    items[C_NOMAP] = _item(db, lib_m, C_NOMAP).id
    movie(C_MANUAL, src_on, "dead", hidden=True)

    def series(name, ep_verdicts):
        s = _item(db, lib_tv, name, item_type="series")
        season = _item(db, lib_tv, f"{name}季", item_type="season",
                       parent=s, series=s)
        for i, v in enumerate(ep_verdicts, 1):
            ep = _item(db, lib_tv, f"{name}集{'一二'[i - 1]}",
                       item_type="episode", parent=season, series=s)
            m = _map(db, ep, src_on, f"ep-{name}-{i}")
            if v is not None:
                verdicts[m.id] = v
        items[name] = s.id
        return s

    series(C_SDEAD, ["dead", "gone"])
    series(C_SPART, ["dead", None])

    db.commit()
    data = {"src_on": src_on.id, "src_off": src_off.id,
            "lib_m": lib_m.id, "lib_tv": lib_tv.id,
            "ids": items, "verdicts": verdicts}
    db.close()
    yield data
    db = SessionLocal()
    try:
        _cleanup(db)
    finally:
        db.close()


def _own_movie(seed, name, verdict="dead"):
    """变更类用例自建一条单线电影（源开），登记清理。"""
    OWN_NAMES.append(name)
    db = SessionLocal()
    try:
        lib = db.get(em.Library, seed["lib_m"])
        src = db.get(em.UpstreamSource, seed["src_on"])
        it = _item(db, lib, name)
        m = _map(db, it, src, f"vod-{name}")
        db.commit()
        return it.id, m.id
    finally:
        db.close()


def _hide_dead(seed, name, probe_table=None):
    """走正式落藏路径把自建条目藏掉，返回 (item_id, mapping_id)。"""
    item_id, mapping_id = _own_movie(seed, name)
    table = probe_table if probe_table is not None else {mapping_id: "dead"}
    db = SessionLocal()
    try:
        stats = nlh.verify_and_hide_dead_line_items(
            db, [item_id], probe=_fake_probe(table))
        assert stats["hidden"] == 1
    finally:
        db.close()
    assert _fresh(item_id) is True
    return item_id, mapping_id


# ---------------------------------------------------------------- 候选判定

def test_candidate_judgement(seed):
    db = SessionLocal()
    try:
        lib_ids = nlh.applecms_library_ids(db)
        got = set(nlh.find_dead_line_candidate_ids(
            db, lib_ids, seed["verdicts"]))
    finally:
        db.close()
    ids = seed["ids"]
    mine = got & set(ids.values())
    assert mine == {ids[C_DEAD], ids[C_DEADGONE], ids[C_SDEAD]}
    # 逐条点名不命中：有活线 / 源停用 / 多线未全探 / 未探 / 无映射 /
    # 已藏（手工藏）/ 剧只探了一半
    for name in (C_LIVE, C_DEADOFF, C_MULTI, C_UNPROBED, C_NOMAP,
                 C_MANUAL, C_SPART):
        assert ids[name] not in got, name


# ---------------------------------------------------------------- 落藏复检

def test_verify_hide_requires_fresh_all_dead(seed):
    revived_id, revived_map = _own_movie(seed, "dlh_复检活过来")
    gone_id, gone_map = _own_movie(seed, "dlh_复检仍死")
    unk_id, unk_map = _own_movie(seed, "dlh_复检判不了")
    probe = _fake_probe({revived_map: "live", gone_map: "gone",
                         unk_map: "unknown"})
    db = SessionLocal()
    try:
        stats = nlh.verify_and_hide_dead_line_items(
            db, [revived_id, gone_id, unk_id], probe=probe)
    finally:
        db.close()
    try:
        assert stats["hidden"] == 1 and stats["revived"] == 1
        assert stats["unknown"] == 1
        assert _fresh(revived_id) is False   # 活过来了，不藏
        assert _fresh(gone_id) is True       # 复检仍死，藏
        assert _fresh(unk_id) is False       # 判不了，不藏（没实锤不许藏）
        assert _ledger(gone_id) == [(nlh.REASON_ALL_LINES_DEAD, False)]
        assert _ledger(revived_id) == []
        assert _ledger(unk_id) == []
    finally:
        db = SessionLocal()
        _purge(db, ["dlh_复检活过来", "dlh_复检仍死", "dlh_复检判不了"])
        db.close()


def test_verify_hide_idempotent_and_skips_hidden(seed):
    item_id, _ = _hide_dead(seed, "dlh_幂等条目")
    try:
        db = SessionLocal()
        try:
            stats = nlh.verify_and_hide_dead_line_items(
                db, [item_id], probe=_fake_probe({}))
        finally:
            db.close()
        assert stats["hidden"] == 0
        assert _ledger(item_id) == [(nlh.REASON_ALL_LINES_DEAD, False)]
    finally:
        db = SessionLocal()
        _purge(db, ["dlh_幂等条目"])
        db.close()


# ---------------------------------------------------------------- 探针分类

def test_probe_mapping_classification(monkeypatch):
    from backend.emby_server import upstream_playback as upb

    def ok(db, item_id, mapping_id=None):
        return "https://cdn.example.com/x/index.m3u8"

    def gone(db, item_id, mapping_id=None):
        raise upb._CmsEvictableFailure(
            404, "upstream_mapping_not_found", "所选线路已不可用")

    def dead(db, item_id, mapping_id=None):
        raise upb.UpstreamPlaybackError(
            502, "upstream_http_error", "上游请求失败")

    def boom(db, item_id, mapping_id=None):
        raise RuntimeError("dns exploded")

    db = SessionLocal()
    try:
        for fake, want in ((ok, "live"), (gone, "gone"),
                           (dead, "dead"), (boom, "unknown")):
            monkeypatch.setattr(
                upb, "resolve_mapped_upstream_stream", fake)
            assert nlh.probe_mapping(db, 1, mapping_id=1) == want
    finally:
        db.close()


# ---------------------------------------------------------------- 轮转解藏

def test_rotation_restores_only_own_reason(seed):
    a_id, a_map = _hide_dead(seed, "dlh_轮转复活")
    c_id, c_map = _hide_dead(seed, "dlh_轮转仍死")
    # 第一批在册条目（手工造行模拟）：轮转不许碰
    b_id, _b_map = _own_movie(seed, "dlh_第一批在册")
    db = SessionLocal()
    try:
        row = db.get(em.MediaItem, b_id)
        row.is_hidden = True
        db.add(em.ItemHiddenLedger(
            item_id=b_id, reason=nlh.REASON_NO_ENABLED_LINE,
            hidden_at=datetime.now()))
        db.commit()
    finally:
        db.close()
    try:
        probe = _fake_probe({a_map: "live", c_map: "dead"})
        db = SessionLocal()
        try:
            stats = nlh.recheck_restore_dead_line_items(db, probe=probe)
        finally:
            db.close()
        assert stats["restored"] == 1
        assert _fresh(a_id) is False
        assert _ledger(a_id) == [(nlh.REASON_ALL_LINES_DEAD, True)]
        assert _fresh(c_id) is True
        assert _ledger(c_id) == [(nlh.REASON_ALL_LINES_DEAD, False)]
        # 第一批的条目与台账行原样不动
        assert _fresh(b_id) is True
        assert _ledger(b_id) == [(nlh.REASON_NO_ENABLED_LINE, False)]
    finally:
        db = SessionLocal()
        _purge(db, ["dlh_轮转复活", "dlh_轮转仍死", "dlh_第一批在册"])
        db.close()


def test_rotation_limit_is_bounded(seed):
    made = [_hide_dead(seed, f"dlh_轮转上限{i}") for i in range(3)]
    try:
        probe = _fake_probe({m: "live" for _i, m in made})
        db = SessionLocal()
        try:
            stats = nlh.recheck_restore_dead_line_items(
                db, limit=2, probe=probe)
        finally:
            db.close()
        assert stats["restored"] == 2
        still = [i for i, _m in made if _fresh(i) is True]
        assert len(still) == 1
        # 传超硬顶的 limit 也不许越界（在册只剩 1 条，全处理完即止）
        db = SessionLocal()
        try:
            stats = nlh.recheck_restore_dead_line_items(
                db, limit=99999, probe=probe)
        finally:
            db.close()
        assert stats["restored"] == 1
    finally:
        db = SessionLocal()
        _purge(db, [f"dlh_轮转上限{i}" for i in range(3)])
        db.close()


def test_rotation_idempotent_when_still_dead(seed):
    item_id, mapping_id = _hide_dead(seed, "dlh_轮转幂等")
    try:
        probe = _fake_probe({mapping_id: "dead"})
        for _ in range(2):
            db = SessionLocal()
            try:
                stats = nlh.recheck_restore_dead_line_items(
                    db, probe=probe)
            finally:
                db.close()
            assert stats["restored"] == 0
        assert _fresh(item_id) is True
        assert _ledger(item_id) == [(nlh.REASON_ALL_LINES_DEAD, False)]
    finally:
        db = SessionLocal()
        _purge(db, ["dlh_轮转幂等"])
        db.close()


# ---------------------------------------------------------------- 节流挂接

def _set_stamp_ago(seconds):
    db = SessionLocal()
    try:
        store.write_values(
            db, {RECHECK_KEY: str(int(
                (datetime.now() - timedelta(seconds=seconds)).timestamp()))})
        db.commit()
    finally:
        db.close()


def test_maybe_recheck_throttles(seed):
    item_id, mapping_id = _hide_dead(seed, "dlh_节流条目")
    try:
        probe = _fake_probe({mapping_id: "live"})
        _set_stamp_ago(10 * 3600)  # 上次跑是很久以前 → 本轮应执行
        db = SessionLocal()
        try:
            first = nlh.maybe_recheck_restore_dead_line_items(
                db, probe=probe)
            second = nlh.maybe_recheck_restore_dead_line_items(
                db, probe=probe)
        finally:
            db.close()
        assert not first.get("throttled")
        assert first["restored"] == 1
        assert second.get("throttled") is True
        assert _fresh(item_id) is False
    finally:
        db = SessionLocal()
        _purge(db, ["dlh_节流条目"])
        db.close()


# ---------------------------------------------------------------- 两批隔离

def test_batch1_reconcile_never_touches_batch2_rows(seed):
    # 用专属源建条目，第二批藏后把源停掉：条目此时同时符合第一批
    # 口径（无启用线），第一批对账不许 claimed、更不许解藏它。
    db = SessionLocal()
    try:
        src = em.UpstreamSource(
            name="dlh_隔离专属源", source_type="applecms",
            base_url="https://iso.example.com", auth_method="api_key",
            enabled=True)
        db.add(src)
        db.flush()
        lib = db.get(em.Library, seed["lib_m"])
        it = _item(db, lib, "dlh_两批隔离条目")
        m = _map(db, it, src, "vod-iso")
        db.commit()
        item_id, mapping_id, src_id = it.id, m.id, src.id
    finally:
        db.close()
    OWN_NAMES.append("dlh_两批隔离条目")
    try:
        db = SessionLocal()
        try:
            stats = nlh.verify_and_hide_dead_line_items(
                db, [item_id], probe=_fake_probe({mapping_id: "dead"}))
            assert stats["hidden"] == 1
            src = db.get(em.UpstreamSource, src_id)
            src.enabled = False
            db.commit()
        finally:
            db.close()
        db = SessionLocal()
        try:
            nlh.reconcile_no_line_hidden(db)
        finally:
            db.close()
        assert _fresh(item_id) is True  # 没被第一批解藏
        assert _ledger(item_id) == [(nlh.REASON_ALL_LINES_DEAD, False)]
    finally:
        db = SessionLocal()
        _purge(db, ["dlh_两批隔离条目"])
        db.query(em.UpstreamSource).filter(
            em.UpstreamSource.name == "dlh_隔离专属源").delete(
            synchronize_session=False)
        db.commit()
        db.close()


# ---------------------------------------------------------------- 同步轮挂接

def test_sync_round_tail_runs_rotation(seed, monkeypatch):
    item_id, mapping_id = _hide_dead(seed, "dlh_同步轮解藏")
    try:
        _set_stamp_ago(10 * 3600)  # 节流已过
        # 轮转默认探针走模块全局 probe_mapping，替成假探针
        monkeypatch.setattr(
            nlh, "probe_mapping",
            lambda db, local_item_id, mapping_id=None: "live")
        from backend.emby_server import applecms_library_sync as als
        monkeypatch.setenv("APPLECMS_LIBRARY_SYNC_ENABLED", "1")
        monkeypatch.setattr(als, "sync_library",
                            lambda db, library, **kw: {"blocked": "stub"})
        results = als.run_library_sync_round(session_factory=SessionLocal)
        assert results
        assert _fresh(item_id) is False  # 同步轮收尾轮转把它解藏了
    finally:
        db = SessionLocal()
        _purge(db, ["dlh_同步轮解藏"])
        db.close()
