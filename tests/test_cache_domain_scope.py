"""CacheManager 域 scope（FMBY 批一 #10）：统一缓存封装按域分片。

口径钉死：
- 不带 domain 的 get/set/delete/exists/delete_pattern 与从前逐字节一致；
- 带 domain 写入只落 ``rb:d:<域>:<原键>`` 新键；读取先新键、未命中
  回退旧无域键（老键不迁移、靠 TTL 自然消亡）；新键命中后以新键为准；
- delete 带 domain 时新旧两键一并删（否则旧键会把删掉的值读回来）；
- invalidate_domain 只清该域命名空间，其它域与无域键不受影响；
- Redis 可达与 Redis 故障（内存回退）两条路行为一致；
- 非法域 slug 直接 ValueError，不静默落到错域里。
"""
import fnmatch
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

import pytest

from backend import database as dbmod

CM = dbmod.CacheManager


class FakeRedis:
    """dict 背的假 Redis（只实现 CacheManager 用到的方法）。"""

    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def setex(self, key, ttl, value):
        self.store[key] = value
        return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        return n

    def keys(self, pattern):
        return [k for k in self.store if fnmatch.fnmatch(k, pattern)]

    def exists(self, key):
        return 1 if key in self.store else 0


@pytest.fixture()
def fake_redis(monkeypatch):
    fake = FakeRedis()

    def _call(fn, default=None):
        try:
            return fn(fake)
        except Exception:  # noqa: BLE001 — 与真 redis_call 同口径：出错给 default
            return default

    monkeypatch.setattr(dbmod, "redis_call", _call)
    _clear_memory()
    yield fake
    _clear_memory()


@pytest.fixture()
def redis_down(monkeypatch):
    monkeypatch.setattr(dbmod, "redis_call", lambda fn, default=None: default)
    _clear_memory()
    yield
    _clear_memory()


def _clear_memory():
    with CM._memory_cache_lock:
        CM._memory_cache.clear()
        CM._memory_cache_order.clear()


# ==================== 无域：与从前一致 ====================

def test_unscoped_roundtrip_redis(fake_redis):
    assert CM.set("k1", "v1", ttl=60) is True
    assert fake_redis.store == {"rb:k1": "v1"}
    assert CM.get("k1") == "v1"
    assert CM.exists("k1") is True
    assert CM.delete("k1") is True
    assert CM.get("k1") is None
    assert CM.exists("k1") is False


def test_unscoped_roundtrip_memory(redis_down):
    assert CM.set("k1", "v1", ttl=60) is True
    assert CM.get("k1") == "v1"
    assert CM.exists("k1") is True
    assert CM.delete("k1") is True
    assert CM.get("k1") is None


def test_unscoped_delete_pattern_still_works(fake_redis):
    CM.set("aa:1", "x")
    CM.set("aa:2", "y")
    CM.set("bb:1", "z")
    assert CM.delete_pattern("aa:*") == 2
    assert CM.get("bb:1") == "z"


# ==================== 带域：新键落点与隔离 ====================

def test_domain_write_lands_in_domain_namespace(fake_redis):
    CM.set("page", "P", domain="items")
    assert fake_redis.store == {"rb:d:items:page": "P"}
    assert CM.get("page", domain="items") == "P"
    # 无域读与其它域读都看不见
    assert CM.get("page") is None
    assert CM.get("page", domain="browse") is None
    assert CM.exists("page", domain="items") is True
    assert CM.exists("page") is False


def test_domain_read_falls_back_to_legacy_key(fake_redis):
    CM.set("page", "old")                        # 迁移前的老键
    assert CM.get("page", domain="items") == "old"
    CM.set("page", "new", domain="items")        # 新写入落新键
    assert CM.get("page", domain="items") == "new"   # 新键优先
    assert CM.get("page") == "old"                   # 老键原样不动
    assert CM.exists("page", domain="playback") is True  # 回退口径下 exists 也认老键


def test_domain_fallback_in_memory_mode(redis_down):
    CM.set("page", "old")
    assert CM.get("page", domain="items") == "old"
    CM.set("page", "new", domain="items")
    assert CM.get("page", domain="items") == "new"
    assert CM.get("page") == "old"


def test_domain_delete_clears_scoped_and_legacy(fake_redis):
    CM.set("page", "old")
    CM.set("page", "new", domain="items")
    CM.delete("page", domain="items")
    assert CM.get("page", domain="items") is None
    assert CM.get("page") is None
    assert fake_redis.store == {}


# ==================== 按域失效 ====================

def test_invalidate_domain_redis(fake_redis):
    CM.set("a", "1", domain="items")
    CM.set("b", "2", domain="items")
    CM.set("c", "3", domain="playback")
    CM.set("d", "4")
    assert CM.invalidate_domain("items") == 2
    assert CM.get("a", domain="items") is None
    assert CM.get("c", domain="playback") == "3"
    assert CM.get("d") == "4"
    assert CM.invalidate_domain("items") == 0


def test_invalidate_domain_memory(redis_down):
    CM.set("a", "1", domain="assets")
    CM.set("b", "2", domain="assets")
    CM.set("c", "3", domain="browse")
    assert CM.invalidate_domain("assets") == 2
    assert CM.get("a", domain="assets") is None
    assert CM.get("c", domain="browse") == "3"


# ==================== 域校验 ====================

def test_domains_constant_is_the_four_starters():
    assert set(CM.DOMAINS) == {"browse", "items", "playback", "assets"}


@pytest.mark.parametrize("bad", ["", "  ", "a b", "a/b", "a:b", "域"])
def test_invalid_domain_rejected(fake_redis, bad):
    with pytest.raises(ValueError):
        CM.set("k", "v", domain=bad)
    with pytest.raises(ValueError):
        CM.get("k", domain=bad)
    with pytest.raises(ValueError):
        CM.invalidate_domain(bad)


def test_invalidate_domain_requires_a_domain(fake_redis):
    with pytest.raises(ValueError):
        CM.invalidate_domain(None)
