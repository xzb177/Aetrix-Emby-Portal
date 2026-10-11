"""本地缓存目录可写性校验测试。

背景：write_config 只调 os.makedirs 建目录，但"目录存在"≠"目录可写"
（只读挂载、权限不足时 makedirs 不报错，下载时才失败，用户一头雾水）。
修复后：保存时探写一次，不可写直接 400 说清楚；worker 的 cache_dir(create=True)
同样探写，不可写抛 OSError（调用方已有 except OSError 记 failed）。
"""
import os
import stat

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.emby_server import local_cache
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    session.close()


def _write_config(db, **overrides):
    kwargs = dict(enabled=True, dir="", max_gb_value=500, hot_days_value=7,
                  hot_plays_value=3, rate_mbps_value=20)
    kwargs.update(overrides)
    return local_cache.write_config(db, **kwargs)


def test_write_config_creates_missing_dir(db, tmp_path):
    """目录不存在时自动创建（回归：用户报"不会自动创建缓存目录"）"""
    target = str(tmp_path / "a" / "b" / "media_cache")
    assert not os.path.exists(target)
    _write_config(db, dir=target)
    assert os.path.isdir(target)


def test_write_config_rejects_unwritable_dir(db, tmp_path, monkeypatch):
    """目录存在但不可写时，保存直接 400（ValueError），不留坏配置"""
    target = str(tmp_path / "readonly_cache")
    os.makedirs(target, exist_ok=True)
    # root 无视文件权限位，改用 mock 模拟 open 写失败
    real_open = open

    def _fail_open(path, mode="r", *args, **kwargs):
        if str(path).startswith(target) and "w" in mode:
            raise OSError(13, "Permission denied")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fail_open)
    with pytest.raises(ValueError, match="不可写"):
        _write_config(db, dir=target)
    # 配置没写进去（不会存半个坏配置）
    assert local_cache.configured_dir(db) != target


def test_write_config_rejects_relative_dir(db):
    """相对路径直接 400"""
    with pytest.raises(ValueError, match="绝对路径"):
        _write_config(db, dir="relative/path/cache")


def test_write_config_rejects_file_in_the_way(db, tmp_path):
    """同名文件挡路时 400"""
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file")
    with pytest.raises(ValueError, match="同名文件"):
        _write_config(db, dir=str(blocker))


def test_cache_dir_create_probes_writable(db, tmp_path, monkeypatch):
    """worker 路径 cache_dir(create=True)：不可写抛 OSError（调用方记 failed）"""
    target = tmp_path / "worker_cache"
    target.mkdir()
    real_open = open

    def _fail_open(path, mode="r", *args, **kwargs):
        if str(path).startswith(str(target)) and "w" in mode:
            raise OSError(13, "Permission denied")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", _fail_open)
    monkeypatch.setattr(local_cache, "configured_dir", lambda _db: str(target))
    with pytest.raises(OSError, match="不可写"):
        local_cache.cache_dir(db, create=True)


def test_cache_dir_create_ok(db, tmp_path, monkeypatch):
    """worker 路径正常时建目录并返回"""
    target = str(tmp_path / "new" / "worker_cache")
    monkeypatch.setattr(local_cache, "configured_dir", lambda _db: target)
    result = local_cache.cache_dir(db, create=True)
    assert result == target
    assert os.path.isdir(target)
