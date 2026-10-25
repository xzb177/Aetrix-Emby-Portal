"""单集认领配额制（链路规划 Step 3）回归测试。

需求：夜间窗口从"禁令制"改"配额制"。
- feature flag enrich_episode_quota_mode（默认 0=关）：关时行为与今天逐字节一致；
- flag 开：夜间窗口内不限配额；窗口外每天限 enrich_episode_day_quota 条单集；
- 豆瓣封禁中有效配额减半（写死 0.5）；配额设 0 = 回到禁令制（回滚口径）；
- 配额用尽后顶层条目（movie/series）认领不受影响。

- 全部在隔离临时 SQLite 里跑，不连生产库；
- 不测真实 TMDB 网络。
"""
import os
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db"); os.close(_fd)
os.environ["DATABASE_URL"]=f"sqlite:///{_tmppath}"
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

_NIGHT_ENVS = (
    enrich_worker.ENRICH_NIGHT_START_ENV,
    enrich_worker.ENRICH_NIGHT_END_ENV,
    enrich_worker.ENRICH_NIGHT_TZ_ENV,
)
_QUOTA_KEYS = (
    enrich_worker.CFG_ENRICH_EPISODE_DAY_QUOTA,
    enrich_worker.CFG_ENRICH_EPISODE_QUOTA_MODE,
    enrich_worker.CFG_EPISODE_NIGHT_ONLY,
)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        # 每个测试前清空（隔离，避免测试间污染）
        s.query(em.MediaStream).delete()
        s.query(em.MediaItem).delete()
        from backend import models as _models
        s.query(_models.SystemConfig).filter(
            _models.SystemConfig.key.in_(_QUOTA_KEYS)).delete(
            synchronize_session=False)
        s.commit()
        yield s
        s.query(_models.SystemConfig).filter(
            _models.SystemConfig.key.in_(_QUOTA_KEYS)).delete(
            synchronize_session=False)
        s.commit()
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clear_caches_and_env():
    """每个测试前清掉两个 60 秒 TTL 缓存 + 相关环境变量"""
    for cache in (enrich_worker._enrich_tuning_cache, enrich_worker._night_cfg_cache):
        cache["at"] = 0.0
        cache["cfg"] = None
    for k in _NIGHT_ENVS:
        os.environ.pop(k, None)
    yield
    for cache in (enrich_worker._enrich_tuning_cache, enrich_worker._night_cfg_cache):
        cache["at"] = 0.0
        cache["cfg"] = None
    for k in _NIGHT_ENVS:
        os.environ.pop(k, None)


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


def _enable_quota_mode(db, quota=2000, night_only=True, in_window=False,
                       monkeypatch=None):
    """配额制：flag 开 + 配额 + 夜间开关状态；窗口判断按 in_window 固定。"""
    enrich_worker.save_enrich_tuning_config(
        db, {enrich_worker.CFG_ENRICH_EPISODE_QUOTA_MODE: True,
             enrich_worker.CFG_ENRICH_EPISODE_DAY_QUOTA: quota})
    if monkeypatch is not None:
        monkeypatch.setattr(
            enrich_worker, "_in_night_window", lambda now, s, e, tz=None: in_window)
    # 开关只认库。缺省已是开；要关必须落 "0"，不能再靠环境变量。
    if not night_only:
        enrich_worker.save_episode_night_config(db, False, "02:00", "06:00")
        enrich_worker._night_cfg_cache["at"] = 0.0
        enrich_worker._night_cfg_cache["cfg"] = None


def _mark_claimed_today(db, n):
    """预造 n 条"今天已认领"的单集（口径：enrich_claimed_at >= 今日 0 点）"""
    now = datetime.now()
    for i in range(n):
        _make_item(db, item_type="episode", name=f"已认领{i}",
                   enrich_status="done", enrich_claimed_at=now)


# ---------- flag 关：行为与今天一致 ----------

def test_flag_off_daytime_still_bans_episodes(db, monkeypatch):
    """flag 关 + 夜间开关开 + 窗口外 → 白天单集被跳过（禁令制，旧行为）。

    配额制缺省已是开，本例经保存路径显式关掉。"""
    enrich_worker.save_enrich_tuning_config(
        db, {enrich_worker.CFG_ENRICH_EPISODE_QUOTA_MODE: False})
    monkeypatch.setattr(
        enrich_worker, "_in_night_window", lambda now, s, e, tz=None: False)
    _make_item(db, item_type="movie", name="电影A")
    ep = _make_item(db, item_type="episode", name="S01E01", series_id=999)
    claimed = enrich_worker._claim_batch(db, 10)
    claimed_ids = {c.id for c in claimed}
    assert ep.id not in claimed_ids
    assert len(claimed) == 1 and claimed[0].item_type == "movie"


def test_flag_default_off_in_config(db):
    """flag 默认值：2026-10-21 现值对齐后，未设置时 mode=True（开，
    与生产现值一致）；关要经 env ENRICH_EPISODE_QUOTA_MODE=0。"""
    st = enrich_worker.enrich_episode_quota_status(db)
    assert st["mode"] is True


# ---------- flag 开：窗口外配额内可认领 ----------

def test_quota_mode_daytime_claims_episodes_within_quota(db, monkeypatch):
    """flag 开 + 窗口外 + 配额未用尽 → 单集可认领"""
    _enable_quota_mode(db, quota=2000, in_window=False, monkeypatch=monkeypatch)
    ep = _make_item(db, item_type="episode", name="S01E01", series_id=999)
    claimed = enrich_worker._claim_batch(db, 10)
    assert {c.id for c in claimed} == {ep.id}


def test_quota_mode_daytime_blocks_when_exhausted(db, monkeypatch):
    """flag 开 + 窗口外 + 配额用尽 → 单集被跳过；顶层条目不受影响"""
    _enable_quota_mode(db, quota=3, in_window=False, monkeypatch=monkeypatch)
    _mark_claimed_today(db, 3)  # 今天已认领 3 条 = 配额用尽
    movie = _make_item(db, item_type="movie", name="电影A")
    ep = _make_item(db, item_type="episode", name="S01E01", series_id=999)
    claimed = enrich_worker._claim_batch(db, 10)
    claimed_ids = {c.id for c in claimed}
    assert ep.id not in claimed_ids
    assert movie.id in claimed_ids
    db.refresh(ep)
    assert ep.enrich_status == "pending"


def test_quota_mode_night_window_unlimited(db, monkeypatch):
    """flag 开 + 窗口内 → 不限配额（即使今天已用超配额）"""
    _enable_quota_mode(db, quota=1, in_window=True, monkeypatch=monkeypatch)
    _mark_claimed_today(db, 5)  # 已用 5 > 配额 1，但窗口内不限
    ep = _make_item(db, item_type="episode", name="S01E01", series_id=999)
    claimed = enrich_worker._claim_batch(db, 10)
    assert {c.id for c in claimed} == {ep.id}


def test_quota_zero_means_ban_mode(db, monkeypatch):
    """flag 开 + 配额设 0 → 回到禁令制（回滚口径）"""
    _enable_quota_mode(db, quota=0, in_window=False, monkeypatch=monkeypatch)
    movie = _make_item(db, item_type="movie", name="电影A")
    ep = _make_item(db, item_type="episode", name="S01E01", series_id=999)
    claimed = enrich_worker._claim_batch(db, 10)
    claimed_ids = {c.id for c in claimed}
    assert ep.id not in claimed_ids
    assert movie.id in claimed_ids


def test_quota_mode_night_switch_off_unlimited(db, monkeypatch):
    """flag 开但夜间开关没开 → 单集不限（与旧"开关关闭"行为一致）"""
    _enable_quota_mode(db, quota=1, night_only=False,
                       in_window=False, monkeypatch=monkeypatch)
    ep = _make_item(db, item_type="episode", name="S01E01", series_id=999)
    claimed = enrich_worker._claim_batch(db, 10)
    assert {c.id for c in claimed} == {ep.id}


# ---------- 封禁期减半 ----------

def test_ban_halves_effective_quota(db, monkeypatch):
    """豆瓣封禁中 → 有效配额减半（写死 0.5）：quota 2000 → 1000"""
    _enable_quota_mode(db, quota=2000, in_window=False, monkeypatch=monkeypatch)
    monkeypatch.setattr(enrich_worker, "_douban_currently_banned", lambda: True)
    st = enrich_worker.enrich_episode_quota_status(db)
    assert st["banned"] is True
    assert st["effective_quota"] == 1000


def test_ban_exhaustion_blocks_episodes(db, monkeypatch):
    """封禁中用满减半后的配额 → 单集被跳过，顶层不受影响"""
    _enable_quota_mode(db, quota=2000, in_window=False, monkeypatch=monkeypatch)
    monkeypatch.setattr(enrich_worker, "_douban_currently_banned", lambda: True)
    _mark_claimed_today(db, 1000)  # = 减半配额，用尽
    movie = _make_item(db, item_type="movie", name="电影A")
    ep = _make_item(db, item_type="episode", name="S01E01", series_id=999)
    claimed = enrich_worker._claim_batch(db, 10)
    claimed_ids = {c.id for c in claimed}
    assert ep.id not in claimed_ids
    assert movie.id in claimed_ids


def test_no_ban_full_quota(db, monkeypatch):
    """未封禁 → 全额配额"""
    _enable_quota_mode(db, quota=2000, in_window=False, monkeypatch=monkeypatch)
    monkeypatch.setattr(enrich_worker, "_douban_currently_banned", lambda: False)
    st = enrich_worker.enrich_episode_quota_status(db)
    assert st["banned"] is False
    assert st["effective_quota"] == 2000


# ---------- 配额口径 ----------

def test_used_today_counts_only_today(db):
    """当天口径：昨天认领的不算；未认领（claimed_at 为空）的不算"""
    today = datetime.now()
    yesterday = today - timedelta(days=1)
    _make_item(db, item_type="episode", name="今天", enrich_status="done",
               enrich_claimed_at=today)
    _make_item(db, item_type="episode", name="昨天", enrich_status="done",
               enrich_claimed_at=yesterday)
    _make_item(db, item_type="episode", name="未认领", enrich_status="pending")
    _make_item(db, item_type="movie", name="电影", enrich_status="done",
               enrich_claimed_at=today)  # 顶层不计入单集配额
    assert enrich_worker.enrich_episode_quota_used_today(db) == 1


# ---------- flag 存取：布尔归一化（Step 1 latent bug 回归） ----------

def test_quota_mode_flag_save_bool_roundtrip(db):
    """布尔经保存路径落库：True 读到开，False 读到关。"""
    enrich_worker.save_enrich_tuning_config(
        db, {enrich_worker.CFG_ENRICH_EPISODE_QUOTA_MODE: True})
    assert enrich_worker.enrich_tuning_value(
        enrich_worker.CFG_ENRICH_EPISODE_QUOTA_MODE, db) is True
    enrich_worker.save_enrich_tuning_config(
        db, {enrich_worker.CFG_ENRICH_EPISODE_QUOTA_MODE: False})
    assert enrich_worker.enrich_tuning_value(
        enrich_worker.CFG_ENRICH_EPISODE_QUOTA_MODE, db) is False


def test_quota_mode_flag_save_string_roundtrip(db):
    """字符串 "1"/"0" 存取保持原口径。"""
    enrich_worker.save_enrich_tuning_config(
        db, {enrich_worker.CFG_ENRICH_EPISODE_QUOTA_MODE: "1"})
    assert enrich_worker.enrich_tuning_value(
        enrich_worker.CFG_ENRICH_EPISODE_QUOTA_MODE, db) is True
    enrich_worker.save_enrich_tuning_config(
        db, {enrich_worker.CFG_ENRICH_EPISODE_QUOTA_MODE: "0"})
    assert enrich_worker.enrich_tuning_value(
        enrich_worker.CFG_ENRICH_EPISODE_QUOTA_MODE, db) is False
