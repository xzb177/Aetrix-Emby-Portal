"""DB 连接池大小按进程角色取默认值（2026-10-28 洪峰根治回归测试）。

背景：2026-10-28 晚洪峰（10 分钟注册 154 人）打满 api 单进程 50 槽
（pool 25 + overflow 25），请求在鉴权依赖里等连接 30 秒超时、容器标
unhealthy；重启只能止血，洪峰持续即复发（21:21 再次报 QueuePool）。
根治：只抬 api 进程的 pool_size 默认值（25→45，生产峰值
api(45+25)+worker(25+25)+EA(25+25)=170，PG max_connections=200 留 30 富余），
worker / EA 未被打满、保持 25 不动；DB_POOL_SIZE env 仍可覆盖一切。

本文件只测配置读取逻辑（默认值与 env 覆盖），不连真实 PG：
- 未设 env 时按 AETRIX_ROLE 取默认值（api=45，其余=25）；
- 设了 env 时一律以 env 为准（角色默认值不生效）；
- 非法 env 值回落到角色默认值，0 被下限钳制为 1。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest  # noqa: E402

from backend import database as _dbmod  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_pool_env(monkeypatch):
    monkeypatch.delenv("DB_POOL_SIZE", raising=False)
    monkeypatch.delenv("AETRIX_ROLE", raising=False)
    yield


def test_api_role_default_45(monkeypatch):
    monkeypatch.setenv("AETRIX_ROLE", "api")
    assert _dbmod._default_pool_size() == 45
    assert _dbmod._resolve_db_pool_size() == 45
    assert _dbmod.db_pool_config()["default"] == 45


def test_worker_role_default_25(monkeypatch):
    monkeypatch.setenv("AETRIX_ROLE", "worker")
    assert _dbmod._default_pool_size() == 25
    assert _dbmod._resolve_db_pool_size() == 25


def test_no_role_default_25():
    assert _dbmod._default_pool_size() == 25
    assert _dbmod._resolve_db_pool_size() == 25


def test_env_overrides_role_default(monkeypatch):
    monkeypatch.setenv("AETRIX_ROLE", "api")
    monkeypatch.setenv("DB_POOL_SIZE", "60")
    assert _dbmod._resolve_db_pool_size() == 60
    monkeypatch.setenv("AETRIX_ROLE", "worker")
    monkeypatch.setenv("DB_POOL_SIZE", "10")
    assert _dbmod._resolve_db_pool_size() == 10


def test_env_invalid_falls_back_to_role_default(monkeypatch):
    monkeypatch.setenv("AETRIX_ROLE", "api")
    monkeypatch.setenv("DB_POOL_SIZE", "not-a-number")
    assert _dbmod._resolve_db_pool_size() == 45


def test_env_zero_clamped_to_one(monkeypatch):
    monkeypatch.setenv("AETRIX_ROLE", "api")
    monkeypatch.setenv("DB_POOL_SIZE", "0")
    assert _dbmod._resolve_db_pool_size() == 1
