"""缺集普查桶 C1：奇偶双倍编号的安全归一（enrich 管线自动触发）。

病象：源头 release 即双倍编号（如《囚犯医生》文件名就是
S01E02…S01E32，16 个文件演 16 集），库里行与文件一致、什么都不缺，
但播放器按 1–32 排列看着缺了一半。

判定口径（宁漏勿错，门槛拉满）：某季满足全部条件才重编号——
(a) 该季有 tmdb_id 且拿到 TMDB 该季权威集数 N；
(b) 库内该季可见集行恰好 N 条，且集号各不相同；
(c) 集号恰好是 2,4,6,…,2N（一条不多一条不少）。
满足时按集号排序映射为 1..N 落库。任一条件不满足一律不动。

历史：PR #1092 的 C1 数据工作是外部一次性脚本（仓库里未留任何
parity/renumber 代码，本文件即产品级自动归一的唯一实现）。
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

import uuid

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker as ew
from backend.emby_server.tmdb import TmdbClient
init_db()


def _reset_state():
    ew._PARITY_RENUMBER_DONE.clear()


@pytest.fixture()
def db():
    # _PARITY_RENUMBER_DONE 是进程级状态：全量跑时排在本文件之前的其它
    # enrich 测试会经 _enrich_apply 触发归一检查、留下 (series_id, 1) 键，
    # 本文件第一个用例的新剧 id 与之撞键会被误判「已判定过」而跳过。
    # 故 setup 也要清（只靠 teardown 清防不住跨文件污染）。
    _reset_state()
    s = SessionLocal()
    try:
        s.query(em.MediaStream).delete()
        s.query(em.MediaItem).delete()
        yield s
    finally:
        _reset_state()
        s.close()


def _series(db, tmdb_id="239577", name="囚犯医生"):
    it = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=1, item_type="series",
        name=name, file_path="/tmp/s", tmdb_id=tmdb_id,
        enrich_status="pending")
    db.add(it)
    db.commit()
    return it


_SEC = 10_000_000  # 1e7 ticks = 1 秒


def _episodes(db, series, numbers, season=1, dur_min=None, ref_min=None):
    """dur_min/ref_min：整季统一的实测分钟数与参照分钟数；传 (per_ep_list) 可逐行。

    dur_min 为 None 表示该行无实测（duration_ticks=0）；ref 同理。
    """
    for i, n in enumerate(numbers):
        d = dur_min[i] if isinstance(dur_min, (list, tuple)) else dur_min
        r = ref_min[i] if isinstance(ref_min, (list, tuple)) else ref_min
        db.add(em.MediaItem(
            guid=uuid.uuid4().hex, library_id=1, item_type="episode",
            name=f"第{n}集", file_path=f"/tmp/s{n}.mp4",
            series_id=series.id, season_number=season,
            episode_number=n, enrich_status="pending",
            duration_ticks=int(d * 60 * _SEC) if d else 0,
            metadata_runtime_ticks=int(r * 60 * _SEC) if r else 0))
    db.commit()


def _fake_season(n):
    def _se(self, tmdb_id, season_number):
        return [{"episode_number": i} for i in range(1, n + 1)]
    return _se


def _nums(db, series_id):
    rows = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode",
        em.MediaItem.series_id == series_id,
        em.MediaItem.deleted_at.is_(None),
        em.MediaItem.merged_into_id.is_(None),
    ).order_by(em.MediaItem.id).all()
    return sorted(r.episode_number for r in rows)


# 1. 完美偶数列＋TMDB 对上 → 归一
def test_perfect_even_sequence_renumbered(db, monkeypatch):
    s = _series(db)
    _episodes(db, s, [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32])
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(16))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is True
    db.expire_all()
    assert _nums(db, s.id) == list(range(1, 17))


# 2. 幂等：第二次跑不再处理
def test_idempotent_second_run_noop(db, monkeypatch):
    s = _series(db)
    _episodes(db, s, [2, 4, 6, 8])
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(4))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is True
    assert ew._maybe_parity_renumber_season(db, first) is False
    db.expire_all()
    assert _nums(db, s.id) == [1, 2, 3, 4]


# 3. 集数对不上（TMDB 16，库里 15）→ 不动
def test_count_mismatch_untouched(db, monkeypatch):
    s = _series(db)
    nums = [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30]
    _episodes(db, s, nums)
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(16))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is False
    db.expire_all()
    assert _nums(db, s.id) == sorted(nums)


# 4. 混奇偶（2,3,4,5,6…）→ 不动
def test_mixed_parity_untouched(db, monkeypatch):
    s = _series(db)
    nums = [1, 2, 3, 4, 5, 6, 7, 8]
    _episodes(db, s, nums)
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(8))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is False
    db.expire_all()
    assert _nums(db, s.id) == nums


# 5. 已正常编号 1..N → 不动
def test_normal_numbering_untouched(db, monkeypatch):
    s = _series(db)
    _episodes(db, s, [1, 2, 3, 4, 5, 6, 7, 8])
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(8))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is False
    db.expire_all()
    assert _nums(db, s.id) == [1, 2, 3, 4, 5, 6, 7, 8]


# 6. 重复集号（未合并重复行）→ 不动
def test_duplicate_episode_numbers_untouched(db, monkeypatch):
    s = _series(db)
    _episodes(db, s, [2, 4, 4, 6])
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(4))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is False
    db.expire_all()
    assert _nums(db, s.id) == [2, 4, 4, 6]


# 7. TMDB 季数据拿不到（None）→ 不动
def test_tmdb_season_none_untouched(db, monkeypatch):
    s = _series(db)
    _episodes(db, s, [2, 4, 6, 8])
    monkeypatch.setattr(
        TmdbClient, "season_episodes", lambda self, a, b: None)
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is False
    db.expire_all()
    assert _nums(db, s.id) == [2, 4, 6, 8]


# 8. TMDB 瞬态失败抛错 → 不动
def test_tmdb_transient_error_untouched(db, monkeypatch):
    from backend.emby_server.tmdb import TmdbTransientError
    s = _series(db)
    _episodes(db, s, [2, 4, 6, 8])

    def _boom(self, a, b):
        raise TmdbTransientError("429")
    monkeypatch.setattr(TmdbClient, "season_episodes", _boom)
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is False
    db.expire_all()
    assert _nums(db, s.id) == [2, 4, 6, 8]


# 9. 正主剧无 tmdb_id → 不动
def test_no_series_tmdb_id_untouched(db, monkeypatch):
    s = _series(db, tmdb_id=None)
    _episodes(db, s, [2, 4, 6, 8])
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(4))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is False
    db.expire_all()
    assert _nums(db, s.id) == [2, 4, 6, 8]


# 10. 软删行/合并副本行不计入集数 → 凑数对不上就静默不动
def test_softdeleted_rows_excluded(db, monkeypatch):
    from datetime import datetime
    s = _series(db)
    _episodes(db, s, [2, 4, 6, 8])
    # 再加一个软删的「E10」行：它不该被计数
    gone = em.MediaItem(
        guid=uuid.uuid4().hex, library_id=1, item_type="episode",
        name="第10集", file_path="/tmp/s10.mp4", series_id=s.id,
        season_number=1, episode_number=10, enrich_status="pending",
        deleted_at=datetime(2026, 1, 1))
    db.add(gone)
    db.commit()
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(4))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode",
        em.MediaItem.deleted_at.is_(None)).first()
    assert ew._maybe_parity_renumber_season(db, first) is True
    db.expire_all()
    assert _nums(db, s.id) == [1, 2, 3, 4]


# 11. 无开关、默认执行：SystemConfig 里不写任何键，完美偶数列照常归一
def test_default_behavior_without_switch(db, monkeypatch):
    from backend import models as _models
    s = _series(db)
    _episodes(db, s, [2, 4, 6, 8])
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(4))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    # 确认库里没有任何与本功能相关的开关配置行
    assert db.query(_models.SystemConfig).filter(
        _models.SystemConfig.key.like("%parity%")).count() == 0
    assert ew._maybe_parity_renumber_season(db, first) is True
    db.expire_all()
    assert _nums(db, s.id) == [1, 2, 3, 4]


# 12. 日志可审计：逐行含 item_id 与 旧→新
def test_rollback_friendly_logging(db, monkeypatch, caplog):
    import logging
    s = _series(db)
    _episodes(db, s, [2, 4])
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(2))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    with caplog.at_level(logging.INFO, logger="backend.emby_server.enrich_worker"):
        ew._maybe_parity_renumber_season(db, first)
    text = caplog.text
    assert "[parity-renumber]" in text
    assert "2→1" in text and "4→2" in text


# ── 路径 B（甲案：TMDB 集数==2×可见行数＋时长证据） ──

# 13. 路径 B 全长正片命中 → 归一（TMDB 8 集，4 行实测≈2×参照）
def test_path_b_full_length_renumbered(db, monkeypatch, caplog):
    import logging
    s = _series(db)
    _episodes(db, s, [2, 4, 6, 8], dur_min=60, ref_min=30)
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(8))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    with caplog.at_level(logging.INFO, logger="backend.emby_server.enrich_worker"):
        assert ew._maybe_parity_renumber_season(db, first) is True
    db.expire_all()
    assert _nums(db, s.id) == [1, 2, 3, 4]
    assert "path=B" in caplog.text


# 14. 路径 B 半集文件（实测≈1×参照，单数集真缺）→ 不动
def test_path_b_half_length_untouched(db, monkeypatch):
    s = _series(db)
    _episodes(db, s, [2, 4, 6, 8], dur_min=30, ref_min=30)
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(8))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is False
    db.expire_all()
    assert _nums(db, s.id) == [2, 4, 6, 8]


# 15. 路径 B 可比行不足（只有 2 行有实测，<3）→ 不动
def test_path_b_insufficient_evidence_untouched(db, monkeypatch):
    s = _series(db)
    _episodes(db, s, [2, 4, 6, 8], dur_min=[60, 60, None, None], ref_min=30)
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(8))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is False
    db.expire_all()
    assert _nums(db, s.id) == [2, 4, 6, 8]


# 16. 路径 B 达标行不足 2/3（4 行可比，只有 2 行达标）→ 不动
def test_path_b_low_hit_ratio_untouched(db, monkeypatch):
    s = _series(db)
    _episodes(db, s, [2, 4, 6, 8], dur_min=[60, 60, 30, 30], ref_min=30)
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(8))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is False
    db.expire_all()
    assert _nums(db, s.id) == [2, 4, 6, 8]


# 17. 路径 B TMDB 非 2 倍（TMDB 6 集，可见 4 行）→ 不动
def test_path_b_tmdb_not_double_untouched(db, monkeypatch):
    s = _series(db)
    _episodes(db, s, [2, 4, 6, 8], dur_min=60, ref_min=30)
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(6))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is False
    db.expire_all()
    assert _nums(db, s.id) == [2, 4, 6, 8]


# 18. 路径 B 幂等：归一后再跑不再命中图案
# （注：路径 B 时长判据要求可比行 ≥3，2 集季天然走不通 B，属口径内）
def test_path_b_idempotent(db, monkeypatch):
    s = _series(db)
    _episodes(db, s, [2, 4, 6, 8], dur_min=60, ref_min=30)
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(8))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is True
    db.expire_all()
    assert _nums(db, s.id) == [1, 2, 3, 4]
    # 归一后集号为 1..N，不再命中偶数列图案（清掉 DONE 集模拟跨进程再判）
    ew._PARITY_RENUMBER_DONE.clear()
    first2 = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first2) is False
    assert _nums(db, s.id) == [1, 2, 3, 4]


# ── 巡检兜底 ──

# 19. 巡检候选预筛：命中图案的季被喂到、非图案/N<2 不喂
def test_parity_patrol_candidates(db):
    good = _series(db, name="全长剧")
    _episodes(db, good, [2, 4, 6])
    bad = _series(db, name="正常剧")
    _episodes(db, bad, [1, 2, 3])
    tiny = _series(db, name="单集")
    _episodes(db, tiny, [2])
    cands = ew._parity_patrol_candidates(db)
    assert (good.id, 1) in cands
    assert not any(sid == bad.id for sid, _ in cands)
    assert not any(sid == tiny.id for sid, _ in cands)


# 20. 巡检整链：候选季经 janitor 函数归一，非候选不动
def test_parity_patrol_rescue_end_to_end(db, monkeypatch):
    s = _series(db)
    _episodes(db, s, [2, 4, 6, 8], dur_min=60, ref_min=30)
    other = _series(db, name="正常剧")
    _episodes(db, other, [1, 2, 3, 4], dur_min=60, ref_min=30)
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(8))
    assert ew.parity_patrol_rescue(db) == 1
    db.expire_all()
    assert _nums(db, s.id) == [1, 2, 3, 4]
    assert _nums(db, other.id) == [1, 2, 3, 4]


# 21. 巡检不受进程级 DONE 集挡住：本进程 enrich 早先已判过该季
# （键在 _PARITY_RENUMBER_DONE 里）、集号又被改回偶数列时，巡检强制
# 重判、照样归一
def test_parity_patrol_rescue_ignores_done_cache(db, monkeypatch):
    s = _series(db, name="回滚剧")
    _episodes(db, s, [2, 4, 6, 8], dur_min=60, ref_min=30)
    monkeypatch.setattr(TmdbClient, "season_episodes", _fake_season(8))
    # 模拟本进程 enrich 早先已判过该季（key 留在进程级 DONE 集里）
    ew._PARITY_RENUMBER_DONE.add((s.id, 1))
    first = db.query(em.MediaItem).filter(
        em.MediaItem.item_type == "episode").first()
    assert ew._maybe_parity_renumber_season(db, first) is False  # 被 DONE 挡住
    db.expire_all()
    assert ew.parity_patrol_rescue(db) == 1  # 巡检强制重判，照样归一
    db.expire_all()
    assert _nums(db, s.id) == [1, 2, 3, 4]
