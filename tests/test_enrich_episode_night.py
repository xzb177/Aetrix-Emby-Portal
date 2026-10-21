"""单集夜间刮削回归测试。

需求：ENRICH_EPISODE_NIGHT_ONLY=1 时，白天只刮顶层条目（movie/series），
单集（episode）只在夜间窗口 [ENRICH_NIGHT_START, ENRICH_NIGHT_END) 内刮削。
默认关闭，不改变现有行为。

- 全部在隔离临时 SQLite 里跑，不连生产库；
- 不测真实 TMDB 网络。
"""
import os
import tempfile
from datetime import datetime

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db"); os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm
_dbmod.engine = _ce(os.environ["DATABASE_URL"])
# 必须走 configure_session_local：直接赋值会把 SessionLocal 代理顶掉，
# 之后各模块 import 到的是被冻结的真 factory，又会查旧库。
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import uuid

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        # 每个测试前清空（隔离，避免测试间污染）
        s.query(em.MediaStream).delete()
        s.query(em.MediaItem).delete()
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clear_night_cfg_cache():
    """每个测试前清掉夜间配置的 60 秒 TTL 缓存 + 环境变量，避免测试间污染"""
    enrich_worker._night_cfg_cache["at"] = 0.0
    enrich_worker._night_cfg_cache["cfg"] = None
    for k in (enrich_worker.ENRICH_EPISODE_NIGHT_ONLY_ENV,
              enrich_worker.ENRICH_NIGHT_START_ENV,
              enrich_worker.ENRICH_NIGHT_END_ENV):
        os.environ.pop(k, None)
    yield
    enrich_worker._night_cfg_cache["at"] = 0.0
    enrich_worker._night_cfg_cache["cfg"] = None
    for k in (enrich_worker.ENRICH_EPISODE_NIGHT_ONLY_ENV,
              enrich_worker.ENRICH_NIGHT_START_ENV,
              enrich_worker.ENRICH_NIGHT_END_ENV):
        os.environ.pop(k, None)
    from backend import models as _models
    from backend.integrations import store as _store
    s = SessionLocal()
    try:
        s.query(_models.SystemConfig).filter(
            _models.SystemConfig.key.in_([
                enrich_worker.CFG_EPISODE_NIGHT_ONLY,
                enrich_worker.CFG_ENRICH_EPISODE_QUOTA_MODE,
            ])).delete(synchronize_session=False)
        s.commit()
    except Exception:
        s.rollback()
    finally:
        s.close()
    _store.invalidate(enrich_worker.CFG_EPISODE_NIGHT_ONLY,
                      enrich_worker.CFG_ENRICH_EPISODE_QUOTA_MODE)
    enrich_worker._enrich_tuning_cache["at"] = 0.0
    enrich_worker._enrich_tuning_cache["cfg"] = None


def _make_item(db, **kw):
    it = em.MediaItem(
        guid=uuid.uuid4().hex,
        library_id=kw.pop("library_id", 1),
        item_type=kw.pop("item_type", "movie"),
        name=kw.pop("name", "测试"),
        file_path=kw.pop("file_path", "/tmp/x.mp4"),
        enrich_status=kw.pop("enrich_status", "pending"),
        **kw,
    )
    db.add(it)
    db.commit()
    return it


# ---------- _parse_hhmm ----------

def test_parse_hhmm_valid():
    assert enrich_worker._parse_hhmm("02:00", "06:00") == "02:00"
    assert enrich_worker._parse_hhmm("2:5", "06:00") == "02:05"
    assert enrich_worker._parse_hhmm(" 23:59 ", "06:00") == "23:59"


def test_parse_hhmm_invalid_falls_back():
    assert enrich_worker._parse_hhmm("99:99", "06:00") == "06:00"
    assert enrich_worker._parse_hhmm("abc", "06:00") == "06:00"
    assert enrich_worker._parse_hhmm("", "06:00") == "06:00"
    assert enrich_worker._parse_hhmm(None, "06:00") == "06:00"


# ---------- _in_night_window ----------

def test_in_night_window_normal():
    # 普通窗口 02:00-06:00：左闭右开（tz=UTC 时 naive 按 UTC 解释，与旧行为一致）
    assert enrich_worker._in_night_window(datetime(2026, 1, 1, 2, 0), "02:00", "06:00", "UTC")
    assert enrich_worker._in_night_window(datetime(2026, 1, 1, 5, 59), "02:00", "06:00", "UTC")
    assert not enrich_worker._in_night_window(datetime(2026, 1, 1, 6, 0), "02:00", "06:00", "UTC")
    assert not enrich_worker._in_night_window(datetime(2026, 1, 1, 1, 59), "02:00", "06:00", "UTC")
    assert not enrich_worker._in_night_window(datetime(2026, 1, 1, 12, 0), "02:00", "06:00", "UTC")


def test_in_night_window_cross_midnight():
    # 跨午夜窗口 22:00-06:00（tz=UTC 时 naive 按 UTC 解释，与旧行为一致）
    assert enrich_worker._in_night_window(datetime(2026, 1, 1, 22, 0), "22:00", "06:00", "UTC")
    assert enrich_worker._in_night_window(datetime(2026, 1, 1, 23, 30), "22:00", "06:00", "UTC")
    assert enrich_worker._in_night_window(datetime(2026, 1, 1, 0, 0), "22:00", "06:00", "UTC")
    assert enrich_worker._in_night_window(datetime(2026, 1, 1, 5, 59), "22:00", "06:00", "UTC")
    assert not enrich_worker._in_night_window(datetime(2026, 1, 1, 6, 0), "22:00", "06:00", "UTC")
    assert not enrich_worker._in_night_window(datetime(2026, 1, 1, 21, 59), "22:00", "06:00", "UTC")
    assert not enrich_worker._in_night_window(datetime(2026, 1, 1, 12, 0), "22:00", "06:00", "UTC")


# ---------- _episode_night_config ----------

def test_night_config_default_off(db):
    """缺省值：2026-10-21 现值对齐后，不读库、不设环境变量时
    enabled=True（生产现值=开）；关要设 env 或种行 "0"。"""
    cfg = enrich_worker._episode_night_config(db)
    assert cfg["enabled"] is True
    assert cfg["night_start"] == "02:00"
    assert cfg["night_end"] == "06:00"
    assert cfg["from_env"] is False


def test_night_config_env_wins(db):
    """环境变量优先：设置后 from_env=True"""
    os.environ[enrich_worker.ENRICH_EPISODE_NIGHT_ONLY_ENV] = "1"
    os.environ[enrich_worker.ENRICH_NIGHT_START_ENV] = "22:00"
    os.environ[enrich_worker.ENRICH_NIGHT_END_ENV] = "06:00"
    cfg = enrich_worker._episode_night_config(db)
    assert cfg["enabled"] is True
    assert cfg["night_start"] == "22:00"
    assert cfg["night_end"] == "06:00"
    assert cfg["from_env"] is True


def test_night_config_env_explicit_off(db):
    """环境变量显式 0：关闭且 from_env=True（后台改了不生效，要有提示）"""
    os.environ[enrich_worker.ENRICH_EPISODE_NIGHT_ONLY_ENV] = "0"
    cfg = enrich_worker._episode_night_config(db)
    assert cfg["enabled"] is False
    assert cfg["from_env"] is True


def test_night_config_db_backed(db):
    """未设环境变量时读 SystemConfig（管理后台可配）"""
    from backend.integrations import store
    store.write_values(
        db,
        {enrich_worker.CFG_EPISODE_NIGHT_ONLY: "1",
         enrich_worker.CFG_NIGHT_START: "01:00",
         enrich_worker.CFG_NIGHT_END: "05:00"},
        {enrich_worker.CFG_EPISODE_NIGHT_ONLY: "test"},
    )
    db.commit()
    cfg = enrich_worker._episode_night_config(db)
    assert cfg["enabled"] is True
    assert cfg["night_start"] == "01:00"
    assert cfg["night_end"] == "05:00"
    assert cfg["from_env"] is False


def test_night_config_ttl_cache(db, monkeypatch):
    """60 秒 TTL 缓存：缓存期内不重复读库"""
    from backend.integrations import store
    store.write_values(db, {enrich_worker.CFG_EPISODE_NIGHT_ONLY: "1"}, {})
    db.commit()
    cfg1 = enrich_worker._episode_night_config(db)
    assert cfg1["enabled"] is True
    # 改库但不清缓存：缓存期内仍读到旧值
    store.write_values(db, {enrich_worker.CFG_EPISODE_NIGHT_ONLY: "0"}, {})
    db.commit()
    cfg2 = enrich_worker._episode_night_config(db)
    assert cfg2["enabled"] is True
    # save_episode_night_config 会清缓存：下一次读到新值
    enrich_worker.save_episode_night_config(db, False, "02:00", "06:00")
    cfg3 = enrich_worker._episode_night_config(db)
    assert cfg3["enabled"] is False


# ---------- _claim_batch ----------

def _enable_night_only(db, monkeypatch, in_window: bool):
    """打开夜间开关，并固定窗口判断结果。配额制缺省开会盖住夜间禁令，显式关掉。"""
    monkeypatch.setenv(enrich_worker.ENRICH_EPISODE_NIGHT_ONLY_ENV, "1")
    enrich_worker.save_enrich_tuning_config(
        db, {enrich_worker.CFG_ENRICH_EPISODE_QUOTA_MODE: False})
    monkeypatch.setattr(
        enrich_worker, "_in_night_window", lambda now, s, e, tz=None: in_window)


def test_claim_batch_daytime_skips_episodes(db, monkeypatch):
    """白天（不在窗口内）：单集被跳过，只抢到电影/剧集"""
    _enable_night_only(db, monkeypatch, in_window=False)
    _make_item(db, item_type="movie", name="电影A")
    ep = _make_item(db, item_type="episode", name="S01E01", series_id=999)
    claimed = enrich_worker._claim_batch(db, 10)
    claimed_ids = {c.id for c in claimed}
    assert ep.id not in claimed_ids
    assert len(claimed) == 1
    assert claimed[0].item_type == "movie"
    # 单集保持 pending，没被认领
    db.refresh(ep)
    assert ep.enrich_status == "pending"


def test_claim_batch_night_includes_episodes(db, monkeypatch):
    """夜间（在窗口内）：单集正常被抢"""
    _enable_night_only(db, monkeypatch, in_window=True)
    ep = _make_item(db, item_type="episode", name="S01E01", series_id=999)
    claimed = enrich_worker._claim_batch(db, 10)
    assert {c.id for c in claimed} == {ep.id}
    db.refresh(ep)
    assert ep.enrich_status == "enriching"


def test_claim_batch_disabled_includes_episodes(db):
    """开关关闭（默认）：行为不变，单集正常被抢"""
    ep = _make_item(db, item_type="episode", name="S01E01", series_id=999)
    claimed = enrich_worker._claim_batch(db, 10)
    assert {c.id for c in claimed} == {ep.id}


def test_claim_batch_daytime_series_group_excludes_episodes(db, monkeypatch):
    """白天：同一部剧的组被选中（剧集本身 pending），但组内单集不被认领"""
    _enable_night_only(db, monkeypatch, in_window=False)
    series = _make_item(db, item_type="series", name="剧A")
    ep1 = _make_item(db, item_type="episode", name="S01E01", series_id=series.id)
    ep2 = _make_item(db, item_type="episode", name="S01E02", series_id=series.id)
    claimed = enrich_worker._claim_batch(db, 10)
    claimed_ids = {c.id for c in claimed}
    assert series.id in claimed_ids
    assert ep1.id not in claimed_ids
    assert ep2.id not in claimed_ids


# ---------- 管理后台字段名一致性（回归：前端 EnrichEpisodeNightConfig 接口） ----------

def test_public_config_field_names_match_frontend(db):
    """get_episode_night_config 返回的顶层字段必须与前端
    EnrichEpisodeNightConfig 接口一致（night_start/night_end），
    否则管理后台时间选择器显示为空（2026-10-11 回归）。"""
    cfg = enrich_worker.get_episode_night_config(db)
    assert "night_start" in cfg, "顶层缺 night_start，前端读不到"
    assert "night_end" in cfg, "顶层缺 night_end，前端读不到"
    assert "start" not in cfg, "旧字段 start 残留"
    assert "end" not in cfg, "旧字段 end 残留"
    assert cfg["night_start"] == "02:00"
    assert cfg["night_end"] == "06:00"
    assert "enabled" in cfg and "in_window" in cfg and "from_env" in cfg


def test_save_returns_frontend_compatible_fields(db):
    """save_episode_night_config 的返回值同样必须带 night_start/night_end，
    否则保存后前端界面立刻变空。"""
    try:
        cfg = enrich_worker.save_episode_night_config(db, True, "22:00", "06:00")
        assert cfg["enabled"] is True
        assert cfg["night_start"] == "22:00"
        assert cfg["night_end"] == "06:00"
        assert "start" not in cfg and "end" not in cfg
    finally:
        from backend import models as _models
        from backend.integrations import store as _store
        db.query(_models.SystemConfig).filter(
            _models.SystemConfig.key == enrich_worker.CFG_EPISODE_NIGHT_ONLY
        ).delete(synchronize_session=False)
        db.commit()
        _store.invalidate(enrich_worker.CFG_EPISODE_NIGHT_ONLY)
        enrich_worker._night_cfg_cache["at"] = 0.0
        enrich_worker._night_cfg_cache["cfg"] = None
