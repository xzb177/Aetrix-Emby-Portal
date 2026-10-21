"""链路规划 Step 4：状态机"无永久终态"。

feature flag：``enrich_none_auto_reschedule``（SystemConfig，默认 0=关）。
flag 开时：
1. none 定时回捞：janitor 每轮顺带把「超期」的 done+none+无 tmdb_id 剧/电影
   翻回 pending（单轮硬上限 2000，写死；按最旧优先；清 TMDB 搜索阴性缓存；
   priority=50）。「上次回捞时间」复用 enrich_next_retry_at（不加 migration）。
2. ``_incomplete``（资料不全）5 次后转 done+none（等自动回捞），不再进 failed；
   「搜过无命中」终态化时记录终态化时间（enrich_next_retry_at=now）。
flag 关时行为与 Step 4 前完全一致。

测试覆盖：
1. flag 默认关；
2. _incomplete 5 次：flag 开 → done+none（不进 failed）；flag 关 → failed（旧行为）；
3. _incomplete 未超限：仍 pending + 退避（flag 开也不变）；
4. 回捞：超期 none → pending（priority=50、attempts 清零、记回捞时间）；
   未超期 / 有 tmdb_id / 单集 / flag 关 → 不动；
5. 单轮上限 2000（写死）+ 最旧优先；
6. janitor 循环调用回捞（接线）；
7. 搜过无命中终态化：flag 开记时间，flag 关保持 None（旧行为）；
8. none 存量只读计数（按类型分组）。
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

import threading
import time
import uuid
from datetime import datetime, timedelta
from unittest import mock

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker as ew
from backend.integrations import store

init_db()

FLAG = ew.CFG_ENRICH_NONE_AUTO_RESCHEDULE
DAYS = ew.CFG_ENRICH_NONE_RESCHEDULE_DAYS
MAX_ROUNDS = ew.CFG_ENRICH_NONE_MAX_RESCHEDULES


def _clear_tuning_cache():
    ew._enrich_tuning_cache["at"] = 0.0
    ew._enrich_tuning_cache["cfg"] = None


def _set_flag(value: str):
    _clear_tuning_cache()
    db = SessionLocal()
    try:
        store.write_values(db, {FLAG: value}, {FLAG: "Step 4 测试开关"})
        db.commit()
    finally:
        db.close()
    store.invalidate(FLAG)
    _clear_tuning_cache()


def _drop_flag():
    from backend import models as _models
    db = SessionLocal()
    try:
        db.query(_models.SystemConfig).filter(
            _models.SystemConfig.key == FLAG
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
    store.invalidate(FLAG)
    _clear_tuning_cache()


def _set_max_rounds(value: str):
    _clear_tuning_cache()
    db = SessionLocal()
    try:
        store.write_values(db, {MAX_ROUNDS: value}, {MAX_ROUNDS: "Step 4 补强测试轮数上限"})
        db.commit()
    finally:
        db.close()
    store.invalidate(MAX_ROUNDS)
    _clear_tuning_cache()


@pytest.fixture(autouse=True)
def _flag_off():
    _set_flag("0")
    _set_max_rounds("6")
    yield
    _drop_flag()
    _set_max_rounds("6")


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(em.MediaStream).delete()
        s.query(em.MediaItem).delete()
        s.commit()
        yield s
    finally:
        s.close()


def _flag_on():
    _set_flag("1")


def _make_item(db, **kw):
    it = em.MediaItem(
        guid=uuid.uuid4().hex,
        library_id=kw.pop("library_id", 1),
        item_type=kw.pop("item_type", "series"),
        name=kw.pop("name", "测试"),
        file_path=kw.pop("file_path", "/tmp/x.mp4"),
        enrich_status=kw.pop("enrich_status", "pending"),
        **kw,
    )
    db.add(it)
    db.commit()
    return it


def _incomplete_fetched():
    """构造 _incomplete 路径的 fetched：tmdb 配了、有 hit（但没落 id）→
    _searched_no_hit=False → _incomplete=True。"""
    return {"ok": True, "poster": None, "fanart": None, "external_subs": [],
            "nfo_data": None, "tmdb_hit": {"id": 999, "title": "x"},
            "tmdb_details": None, "error": None}


def test_flag_default_off(db):
    assert ew.enrich_tuning_value(FLAG, db) is False


def test_incomplete_five_times_flag_off_goes_failed(db):
    """flag 关 = 旧行为：资料不全 5 次 → failed"""
    it = _make_item(db, name="资料不全旧行为", production_year=None,
                    enrich_attempts=4)
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client:
        fake_client.configured = True
        ew._enrich_apply(db, it, _incomplete_fetched())
    db.commit()
    db.refresh(it)
    assert it.enrich_status == "failed"
    assert it.enrich_next_retry_at is None


def test_incomplete_five_times_flag_on_goes_none_not_failed(db):
    """flag 开：资料不全 5 次 → done+none（数据性缺失，不进 failed），记终态化时间"""
    _flag_on()
    it = _make_item(db, name="资料不全新行为", production_year=None,
                    enrich_attempts=4)
    before = datetime.now()
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client:
        fake_client.configured = True
        ew._enrich_apply(db, it, _incomplete_fetched())
    db.commit()
    db.refresh(it)
    assert it.enrich_status == "done", "资料不全超限应转 done+none，不进 failed"
    assert it.metadata_source == "none"
    assert it.enrich_attempts == 0, "回捞时从零开始计次"
    assert it.enrich_next_retry_at is not None
    assert before <= it.enrich_next_retry_at <= datetime.now()


def test_incomplete_before_limit_stays_pending(db):
    """未超限仍走退避 pending（flag 开也不改变）"""
    _flag_on()
    it = _make_item(db, name="资料不全退避中", production_year=None,
                    enrich_attempts=2)
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client:
        fake_client.configured = True
        ew._enrich_apply(db, it, _incomplete_fetched())
    db.commit()
    db.refresh(it)
    assert it.enrich_status == "pending"
    assert it.enrich_attempts == 3
    assert it.enrich_next_retry_at is not None


def _none_row(db, name, **kw):
    kw.setdefault("enrich_status", "done")
    kw.setdefault("metadata_source", "none")
    kw.setdefault("file_fingerprint", "fp-" + name)
    return _make_item(db, name=name, **kw)


def test_auto_reschedule_flips_overdue(db):
    """flag 开：超期 none → pending；未超期/有tmdb_id/单集/pending 不动"""
    _flag_on()
    old = datetime.now() - timedelta(days=31)
    overdue = _none_row(db, "超期剧", enrich_next_retry_at=old)
    legacy = _none_row(db, "旧存量剧", enrich_next_retry_at=None)  # Step 4 前的存量
    fresh = _none_row(db, "刚终态剧", enrich_next_retry_at=datetime.now())
    _none_row(db, "有id剧", enrich_next_retry_at=old, tmdb_id="123")
    _none_row(db, "单集", item_type="episode", enrich_next_retry_at=old)
    _make_item(db, name="本来就pending", enrich_status="pending",
               file_fingerprint="fp-p")

    n = ew.enrich_none_auto_reschedule(db)
    assert n == 2
    for it in (overdue, legacy):
        db.refresh(it)
        assert it.enrich_status == "pending"
        assert it.enrich_attempts == 0
        assert it.enrich_priority == ew.ENRICH_PRIORITY_RETRY_UNMATCHED == 50
        assert it.enrich_next_retry_at is not None
        # 翻回时记回捞时间：下次超期前不再被捞
        assert it.enrich_next_retry_at >= old
    db.refresh(fresh)
    assert fresh.enrich_status == "done", "未超期不动"
    assert db.query(em.MediaItem).filter(
        em.MediaItem.enrich_status == "pending").count() == 3  # 2 翻回 + 1 原有


def test_auto_reschedule_flag_off_is_noop(db):
    """flag 关：超期 none 也一动不动"""
    old = datetime.now() - timedelta(days=31)
    _none_row(db, "超期剧", enrich_next_retry_at=old)
    n = ew.enrich_none_auto_reschedule(db)
    assert n == 0
    assert db.query(em.MediaItem).filter(
        em.MediaItem.enrich_status == "pending").count() == 0


def test_auto_reschedule_batch_limit_and_oldest_first(db, monkeypatch):
    """单轮硬上限 2000（写死）+ 最旧优先：上限调小验证只捞最旧的"""
    assert ew.ENRICH_NONE_AUTO_RESCHEDULE_BATCH == 2000, "上限必须写死 2000"
    _flag_on()
    monkeypatch.setattr(ew, "ENRICH_NONE_AUTO_RESCHEDULE_BATCH", 2)
    base = datetime.now()
    newest = _none_row(db, "最新", enrich_next_retry_at=base - timedelta(days=31))
    middle = _none_row(db, "中间", enrich_next_retry_at=base - timedelta(days=60))
    oldest = _none_row(db, "最旧", enrich_next_retry_at=None)  # NULL 视为最旧
    n = ew.enrich_none_auto_reschedule(db)
    assert n == 2
    db.refresh(oldest)
    db.refresh(middle)
    db.refresh(newest)
    assert oldest.enrich_status == "pending"
    assert middle.enrich_status == "pending"
    assert newest.enrich_status == "done", "捞满 2 条即停，下一轮继续"


def test_janitor_loop_calls_auto_reschedule(db, monkeypatch):
    """接线：janitor 每轮调用 none 回捞（flag 关时回捞内部返回 0）"""
    calls = []
    monkeypatch.setattr(ew, "enrich_none_auto_reschedule",
                        lambda d: calls.append(1) or 0)
    t = threading.Thread(target=ew._janitor_loop, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 10
        while not calls and time.monotonic() < deadline:
            time.sleep(0.05)
        assert calls, "janitor 循环必须调用 enrich_none_auto_reschedule"
    finally:
        ew._stop_event.set()
        t.join(timeout=15)
        # stop_event 是模块级：复位，避免污染其它测试
        ew._stop_event.clear()
    assert not t.is_alive(), "janitor 应在 stop 后退出"


def test_searched_no_hit_records_finalization_time_flag_on(db):
    """搜过无命中终态化：flag 开记终态化时间（供回捞口径），flag 关保持 None"""
    fetched = {"ok": True, "poster": None, "fanart": None, "external_subs": [],
               "nfo_data": None, "tmdb_hit": None, "tmdb_details": None,
               "error": None}
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client:
        fake_client.configured = True
        it = _make_item(db, name="搜不到的剧")
        ew._enrich_apply(db, it, fetched)
    db.commit()
    db.refresh(it)
    assert it.enrich_status == "done" and it.metadata_source == "none"
    assert it.enrich_next_retry_at is None, "flag 关保持旧行为"

    _flag_on()
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client:
        fake_client.configured = True
        it2 = _make_item(db, name="搜不到的剧2")
        ew._enrich_apply(db, it2, fetched)
    db.commit()
    db.refresh(it2)
    assert it2.enrich_status == "done" and it2.metadata_source == "none"
    assert it2.enrich_next_retry_at is not None, "flag 开记录终态化时间"


def test_none_stock_stats(db):
    """只读计数：按类型分组 + 下一轮可捞回数"""
    _flag_on()
    old = datetime.now() - timedelta(days=31)
    _none_row(db, "剧1", enrich_next_retry_at=old)
    _none_row(db, "剧2", enrich_next_retry_at=None)
    _none_row(db, "电影1", item_type="movie", enrich_next_retry_at=old)
    _none_row(db, "单集1", item_type="episode", enrich_next_retry_at=old)
    _none_row(db, "有id剧", enrich_next_retry_at=old, tmdb_id="1")
    stats = ew.none_stock_stats(db)
    assert stats["by_type"] == {"series": 2, "movie": 1, "episode": 1}
    assert stats["total"] == 4
    # 可捞回：剧/电影 + 超期（单集不在回捞范围）
    assert stats["reschedulable_now"] == 3
    assert stats["cycle_days"] == 14, "周期默认 14 天（Step 4 补强由 30 改为 14）"
    assert stats["max_rounds"] == 6
    assert stats["auto_reschedule_enabled"] is True
    assert stats["batch_limit"] == 2000


# ================= Step 4 补强：轮数上限 + 锁定跳过 =================

def test_none_tuning_specs_registered(db):
    """新键注册：周期默认 14、轮数上限默认 6。

    开关 spec 缺省 2026-10-21 现值对齐后为 "1"（生产现值=开）；本文件
    fixture 写库强制关，故 cfg 读到 False（库行优先于 spec 缺省）。"""
    specs = {k: (default, kind) for k, _, default, kind, _, _, _
             in ew._ENRICH_TUNING_SPECS}
    assert specs[ew.CFG_ENRICH_NONE_RESCHEDULE_DAYS] == ("14", "int")
    assert specs[ew.CFG_ENRICH_NONE_MAX_RESCHEDULES] == ("6", "int")
    assert specs[ew.CFG_ENRICH_NONE_AUTO_RESCHEDULE] == ("1", "flag1")
    cfg = ew.get_enrich_tuning_config(db)["values"]
    assert cfg[ew.CFG_ENRICH_NONE_RESCHEDULE_DAYS] == 14
    assert cfg[ew.CFG_ENRICH_NONE_MAX_RESCHEDULES] == 6
    assert cfg[ew.CFG_ENRICH_NONE_AUTO_RESCHEDULE] is False


def test_models_have_none_reschedules_column():
    assert hasattr(em.MediaItem, "enrich_none_reschedules")


def test_migration_list_has_none_reschedules_column():
    import inspect as _inspect
    src = _inspect.getsource(_dbmod._auto_migrate)
    assert '("enrich_none_reschedules", "INTEGER", "0")' in src


def test_auto_reschedule_skips_locked(db):
    """metadata_locked 的行永不被翻回：抢单本就跳过锁定行，
    翻回只会让它永久卡在 pending、污染待刮计数"""
    _flag_on()
    old = datetime.now() - timedelta(days=31)
    locked = _none_row(db, "锁定剧", enrich_next_retry_at=old,
                       metadata_locked=True)
    n = ew.enrich_none_auto_reschedule(db)
    assert n == 0
    db.refresh(locked)
    assert locked.enrich_status == "done", "锁定的行不动"
    # 只读计数同口径：锁定行也不计入可捞回
    assert ew.none_stock_stats(db)["reschedulable_now"] == 0


def test_auto_reschedule_round_cap(db):
    """轮数上限：达上限的不动；未达上限的翻回且计数 +1"""
    _flag_on()
    _set_max_rounds("1")
    old = datetime.now() - timedelta(days=31)
    capped = _none_row(db, "已满轮剧", enrich_next_retry_at=old,
                       enrich_none_reschedules=1)
    fresh = _none_row(db, "还有轮次剧", enrich_next_retry_at=old,
                      enrich_none_reschedules=0)
    n = ew.enrich_none_auto_reschedule(db)
    assert n == 1
    db.refresh(capped)
    assert capped.enrich_status == "done", "达上限永久保留 done"
    assert capped.enrich_none_reschedules == 1
    db.refresh(fresh)
    assert fresh.enrich_status == "pending"
    assert fresh.enrich_priority == ew.ENRICH_PRIORITY_RETRY_UNMATCHED == 50
    assert fresh.enrich_none_reschedules == 1, "翻回时计数 +1"


def test_flipped_row_is_claimable(db):
    """翻回的行必须真的能被工人认领（与 _claim_batch 认领口径同形断言）"""
    _flag_on()
    old = datetime.now() - timedelta(days=31)
    it = _none_row(db, "待认领剧", enrich_next_retry_at=old)
    n = ew.enrich_none_auto_reschedule(db)
    assert n == 1
    db.refresh(it)
    assert it.enrich_status == "pending"
    assert it.enrich_attempts == 0
    # _claim_batch 核心认领口径：pending + 未到退避期 + 未锁定 + 非合并副本
    from sqlalchemy import or_ as _or
    hit = (db.query(em.MediaItem.id)
           .filter(em.MediaItem.enrich_status == "pending",
                   em.MediaItem.metadata_locked.isnot(True),
                   em.MediaItem.merged_into_id.is_(None),
                   _or(em.MediaItem.enrich_next_retry_at.is_(None),
                       em.MediaItem.enrich_next_retry_at <= datetime.now()))
           .filter(em.MediaItem.id == it.id).count())
    assert hit == 1, "翻回行必须落在工人认领口径内"
