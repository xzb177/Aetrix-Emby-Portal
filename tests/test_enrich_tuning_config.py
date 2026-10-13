"""刮削性能参数配置面收口（链路规划 Step 1）回归测试。

需求：原来 env-only 的关键旋钮（线程数 / 批次 / TMDB 批量 / 重试上限 /
退避基数 / 认领租约）搬进 SystemConfig + 后台 UI；env 保留为部署覆盖。
口径照抄 _episode_night_config：
- 任一环境变量被设置（含显式 "0"）即 env 接管，后台改了也不生效；
- 未设置 env 时读 SystemConfig，60 秒 TTL 缓存热生效；
- 管理后台保存后立即清缓存。
纯搬迁：未设置时行为与今天逐字节一致。

- 全部在隔离临时 SQLite 里跑，不连生产库；
- 不测真实 TMDB 网络。
"""
import os
import tempfile

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

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import enrich_worker

init_db()

_ENV_KEYS = (
    enrich_worker.ENRICH_WORKERS_ENV,
    enrich_worker.ENRICH_BATCH_ENV,
    enrich_worker.ENRICH_TMDB_BATCH_ENV,
    enrich_worker.ENRICH_MAX_ATTEMPTS_ENV,
    enrich_worker.ENRICH_RETRY_BASE_SEC_ENV,
    enrich_worker.ENRICH_CLAIM_LEASE_SEC_ENV,
)


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        # 每个测试前清空 SystemConfig 里的本步 key（隔离，避免测试间污染）
        from backend import models as _models
        s.query(_models.SystemConfig).filter(
            _models.SystemConfig.key.in_([
                enrich_worker.CFG_ENRICH_WORKERS,
                enrich_worker.CFG_ENRICH_BATCH,
                enrich_worker.CFG_ENRICH_TMDB_BATCH,
                enrich_worker.CFG_ENRICH_MAX_ATTEMPTS,
                enrich_worker.CFG_ENRICH_RETRY_BASE_SEC,
                enrich_worker.CFG_ENRICH_CLAIM_LEASE_SEC,
                enrich_worker.CFG_ENRICH_EPISODE_DAY_QUOTA,
                enrich_worker.CFG_ENRICH_NONE_RESCHEDULE_DAYS,
            ])).delete(synchronize_session=False)
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clear_tuning_cache_and_env():
    """每个测试前清掉 60 秒 TTL 缓存 + 相关环境变量，避免测试间污染"""
    enrich_worker._enrich_tuning_cache["at"] = 0.0
    enrich_worker._enrich_tuning_cache["cfg"] = None
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    yield
    enrich_worker._enrich_tuning_cache["at"] = 0.0
    enrich_worker._enrich_tuning_cache["cfg"] = None
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


# ---------- 默认值：未设置时与今天一致 ----------

def test_defaults_match_today(db):
    cfg = enrich_worker.get_enrich_tuning_config(db)
    v = cfg["values"]
    assert v[enrich_worker.CFG_ENRICH_WORKERS] == 8
    assert v[enrich_worker.CFG_ENRICH_BATCH] == 100
    assert v[enrich_worker.CFG_ENRICH_TMDB_BATCH] is True
    assert v[enrich_worker.CFG_ENRICH_MAX_ATTEMPTS] == 5
    assert v[enrich_worker.CFG_ENRICH_RETRY_BASE_SEC] == 60
    assert v[enrich_worker.CFG_ENRICH_CLAIM_LEASE_SEC] == 900
    # 新占坑位：用户拍板值
    assert v[enrich_worker.CFG_ENRICH_EPISODE_DAY_QUOTA] == 2000
    assert v[enrich_worker.CFG_ENRICH_NONE_RESCHEDULE_DAYS] == 30
    assert all(f is False for f in cfg["from_env"].values())


def test_module_constants_unchanged():
    """import 期常量保持今天的 env-only 快照口径（未设置 env 时 = 默认值）"""
    assert enrich_worker.ENRICH_WORKERS == 8
    assert enrich_worker.ENRICH_BATCH == 100
    assert enrich_worker.ENRICH_TMDB_BATCH is True
    assert enrich_worker.ENRICH_MAX_ATTEMPTS == 5
    assert enrich_worker.ENRICH_RETRY_BASE_SEC == 60
    assert enrich_worker.ENRICH_CLAIM_LEASE_SEC == 900


# ---------- env 优先 ----------

def test_env_takes_over(db):
    os.environ[enrich_worker.ENRICH_BATCH_ENV] = "50"
    os.environ[enrich_worker.ENRICH_MAX_ATTEMPTS_ENV] = "0"  # 显式 0 也是 env 接管
    cfg = enrich_worker.get_enrich_tuning_config(db)
    assert cfg["values"][enrich_worker.CFG_ENRICH_BATCH] == 50
    assert cfg["from_env"][enrich_worker.CFG_ENRICH_BATCH] is True
    # 显式 0：env 接管，下限钳制 max(1, 0) = 1（与今天 import 期口径一致）
    assert cfg["values"][enrich_worker.CFG_ENRICH_MAX_ATTEMPTS] == 1
    assert cfg["from_env"][enrich_worker.CFG_ENRICH_MAX_ATTEMPTS] is True
    # 没设 env 的项不受影响
    assert cfg["from_env"][enrich_worker.CFG_ENRICH_RETRY_BASE_SEC] is False


def test_env_beats_system_config(db):
    """env 设置后，后台改了也不生效（from_env=True）"""
    os.environ[enrich_worker.ENRICH_BATCH_ENV] = "50"
    enrich_worker.save_enrich_tuning_config(db, {enrich_worker.CFG_ENRICH_BATCH: "250"})
    cfg = enrich_worker.get_enrich_tuning_config(db)
    assert cfg["values"][enrich_worker.CFG_ENRICH_BATCH] == 50
    assert cfg["from_env"][enrich_worker.CFG_ENRICH_BATCH] is True


def test_env_tmdb_batch_flag_semantics(db):
    """flag1 口径照抄现状：只有 "1" 为真"""
    assert enrich_worker.get_enrich_tuning_config(db)["values"][
        enrich_worker.CFG_ENRICH_TMDB_BATCH] is True
    os.environ[enrich_worker.ENRICH_TMDB_BATCH_ENV] = "0"
    enrich_worker._enrich_tuning_cache["at"] = 0.0
    enrich_worker._enrich_tuning_cache["cfg"] = None
    assert enrich_worker.get_enrich_tuning_config(db)["values"][
        enrich_worker.CFG_ENRICH_TMDB_BATCH] is False
    os.environ[enrich_worker.ENRICH_TMDB_BATCH_ENV] = "1"
    enrich_worker._enrich_tuning_cache["at"] = 0.0
    enrich_worker._enrich_tuning_cache["cfg"] = None
    assert enrich_worker.get_enrich_tuning_config(db)["values"][
        enrich_worker.CFG_ENRICH_TMDB_BATCH] is True


# ---------- 线程数解析 ----------

def test_resolve_workers_default_8(db):
    assert enrich_worker._resolve_enrich_workers(db) == 8
    assert enrich_worker._resolve_enrich_workers() == 8


def test_resolve_workers_env(db):
    os.environ[enrich_worker.ENRICH_WORKERS_ENV] = "4"
    assert enrich_worker._resolve_enrich_workers(db) == 4
    # 上限 16（io_bound 口径，与 _cpu_workers 一致）
    os.environ[enrich_worker.ENRICH_WORKERS_ENV] = "100"
    enrich_worker._enrich_tuning_cache["at"] = 0.0
    enrich_worker._enrich_tuning_cache["cfg"] = None
    assert enrich_worker._resolve_enrich_workers(db) == 16


def test_resolve_workers_from_system_config(db):
    enrich_worker.save_enrich_tuning_config(db, {enrich_worker.CFG_ENRICH_WORKERS: "6"})
    assert enrich_worker._resolve_enrich_workers(db) == 6


# ---------- 保存 / 读取 / 缓存 ----------

def test_save_load_roundtrip(db):
    out = enrich_worker.save_enrich_tuning_config(db, {
        enrich_worker.CFG_ENRICH_BATCH: "250",
        enrich_worker.CFG_ENRICH_TMDB_BATCH: False,
        enrich_worker.CFG_ENRICH_EPISODE_DAY_QUOTA: "1500",
    })
    assert out["values"][enrich_worker.CFG_ENRICH_BATCH] == 250
    assert out["raw"][enrich_worker.CFG_ENRICH_BATCH] == "250"
    assert out["values"][enrich_worker.CFG_ENRICH_TMDB_BATCH] is False
    assert out["raw"][enrich_worker.CFG_ENRICH_TMDB_BATCH] == "0"
    assert out["values"][enrich_worker.CFG_ENRICH_EPISODE_DAY_QUOTA] == 1500
    # 没碰的项保持默认
    assert out["values"][enrich_worker.CFG_ENRICH_MAX_ATTEMPTS] == 5


def test_save_clears_cache(db):
    """保存后立即清缓存：下一次读取即用新值，不用等 60 秒 TTL"""
    before = enrich_worker.get_enrich_tuning_config(db)
    assert before["values"][enrich_worker.CFG_ENRICH_BATCH] == 100
    enrich_worker.save_enrich_tuning_config(db, {enrich_worker.CFG_ENRICH_BATCH: "250"})
    after = enrich_worker.get_enrich_tuning_config(db)
    assert after["values"][enrich_worker.CFG_ENRICH_BATCH] == 250


def test_save_ignores_unknown_keys(db):
    out = enrich_worker.save_enrich_tuning_config(db, {"not_a_real_key": "1"})
    assert "not_a_real_key" not in out["values"]


def test_save_normalizes_illegal_value(db):
    """非法值回退默认，保证库里永远是干净值"""
    out = enrich_worker.save_enrich_tuning_config(db, {enrich_worker.CFG_ENRICH_BATCH: "abc"})
    assert out["values"][enrich_worker.CFG_ENRICH_BATCH] == 100
    assert out["raw"][enrich_worker.CFG_ENRICH_BATCH] == "100"


def test_placeholder_keys_have_no_env(db):
    """两个占坑位没有 env 通道：from_env 恒 False，env_names 为 None"""
    cfg = enrich_worker.get_enrich_tuning_config(db)
    assert cfg["env_names"][enrich_worker.CFG_ENRICH_EPISODE_DAY_QUOTA] is None
    assert cfg["env_names"][enrich_worker.CFG_ENRICH_NONE_RESCHEDULE_DAYS] is None
    assert cfg["from_env"][enrich_worker.CFG_ENRICH_EPISODE_DAY_QUOTA] is False


# ---------- DB 池 ----------

def test_db_pool_env_priority(monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "25")
    assert _dbmod._resolve_db_pool_size() == 25


def test_db_pool_bootstrap_reads_system_config(monkeypatch, tmp_path):
    """未设 env 时：engine 创建前用 bootstrap 连接读 system_configs"""
    monkeypatch.delenv("DB_POOL_SIZE", raising=False)
    p = tmp_path / "pooltest.db"
    eng = _ce(f"sqlite:///{p}")
    from sqlalchemy import text as _text
    with eng.begin() as conn:
        conn.execute(_text("CREATE TABLE system_configs (key VARCHAR(50), value TEXT)"))
        conn.execute(_text("INSERT INTO system_configs (key, value) VALUES ('db_pool_size', '20')"))
    eng.dispose()
    monkeypatch.setattr(_dbmod, "DATABASE_URL", f"sqlite:///{p}")
    assert _dbmod._resolve_db_pool_size() == 20


def test_db_pool_bootstrap_falls_back_when_table_missing(monkeypatch, tmp_path):
    """表不存在（如迁移前）→ 回退默认 10，永不抛异常"""
    monkeypatch.delenv("DB_POOL_SIZE", raising=False)
    p = tmp_path / "empty.db"
    from sqlalchemy import text as _text
    eng = _ce(f"sqlite:///{p}")
    with eng.begin() as conn:
        conn.execute(_text("SELECT 1"))
    eng.dispose()
    monkeypatch.setattr(_dbmod, "DATABASE_URL", f"sqlite:///{p}")
    assert _dbmod._resolve_db_pool_size() == 10


def test_db_pool_config_shape():
    cfg = _dbmod.db_pool_config()
    assert cfg["pool_size"] >= 1
    assert cfg["default"] == 10
    assert cfg["restart_required"] is True
    assert cfg["env_name"] == "DB_POOL_SIZE"
