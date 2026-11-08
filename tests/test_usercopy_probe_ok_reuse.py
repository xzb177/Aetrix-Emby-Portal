# -*- coding: utf-8 -*-
"""用户副本额度探测 ok 裁决短时复用（2026-11-08 用户批准取舍）。

浪费点盘点（drive-api-census-20261108）第 2 条：健康副本每次
判定都重探——PlaybackInfo 与 video_stream 各判一次、每次 2 个
usercontent 请求。旧口径（2026-10-30 08:29 事故）对非 quota 的
缓存裁决一律先失效再新鲜探测；2026-11-08 用户亲点取舍：ok
裁决在 TTL（默认 300 秒、SystemConfig 可配）内直接复用，只对
quota 裁决保持现口径。

钉死：
- 同一 (user, fid) 两次判定只探一次，第二判定吃 Redis 复用。
- 复用落 Redis（跨 aetrix-api / aetrix-ea 两进程）；Redis 不
  可用（None / 抛错）时行为与现状逐字一致（每次新鲜探测）。
- quota / error 裁决绝不写复用：判定缓存到期后仍当场重探。
- 池路径（user_id 形如 poolvar:<variant_id>）不走复用，行为
  逐字不变（熔断恢复探测必须当场探，不许吃陈 ok）。
- TTL=0 即关复用。
全部离线。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest  # noqa: E402

from backend.emby_server import transfer_variant as tv  # noqa: E402

COPY_FID = "USERcopyFID11111111111111111111"

QUOTA_HTML = (b"<!DOCTYPE html><html><head><title>Google Drive - Quota "
              b"exceeded</title></head><body>quota</body></html>")
VIDEO_HEAD = b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00"


class _FakeRedis:
    """最小 Redis 替身：decode_responses=True 口径，值皆 str。"""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.store[key] = value
        return True

    def delete(self, *keys):
        n = 0
        for k in keys:
            if self.store.pop(k, None) is not None:
                n += 1
        return n


class _RaisingRedis:
    def get(self, key):
        raise RuntimeError("redis down")

    def set(self, key, value, ex=None):
        raise RuntimeError("redis down")

    def delete(self, *keys):
        raise RuntimeError("redis down")


@pytest.fixture(autouse=True)
def _no_cache_no_sa(monkeypatch):
    """判定缓存走内存字典（与既有 probe 测试同款）；禁 SA 令牌。"""
    store = {}

    class _CM:
        @staticmethod
        def get(key, *, domain=None):
            return store.get((domain, key))

        @staticmethod
        def set(key, value, ttl=300, *, domain=None):
            store[(domain, key)] = value
            return True

        @staticmethod
        def delete(key, *, domain=None):
            store.pop((domain, key), None)
            return True

    monkeypatch.setattr("backend.database.CacheManager", _CM)
    monkeypatch.setattr(tv, "publish_drive_exists_ok",
                        lambda fid, **k: True)
    monkeypatch.setenv("TRANSFER_USERCOPY_QUOTA_PROBE_ENABLED", "1")
    yield store


@pytest.fixture()
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr("backend.database.get_redis", lambda: fake)
    return fake


def _stub_legs(monkeypatch, range_resp, get_resp):
    calls = {"range": 0, "get": 0}

    def fake_range(url):
        calls["range"] += 1
        if isinstance(range_resp, Exception):
            raise range_resp
        return range_resp

    def fake_get(url):
        calls["get"] += 1
        if isinstance(get_resp, Exception):
            raise get_resp
        return get_resp

    monkeypatch.setattr(tv, "_usercopy_probe_http", fake_range)
    monkeypatch.setattr(tv, "_usercopy_probe_http_get", fake_get)
    return calls


def _ok_legs(monkeypatch):
    return _stub_legs(monkeypatch,
                      (206, "video/mp4", VIDEO_HEAD),
                      (200, "video/mp4", VIDEO_HEAD))


def _clear_verdict_cache():
    from backend.database import CacheManager
    CacheManager.delete(tv._usercopy_quota_cache_key(7, COPY_FID),
                        domain="playback")
    CacheManager.delete(tv._usercopy_quota_detail_key(7, COPY_FID),
                        domain="playback")


# ---------- 主用例：ok 裁决短时复用 ----------

def test_decision_reuses_ok_within_ttl(fake_redis, monkeypatch):
    """同一副本两次判定只探一次：第二判定吃 Redis 复用、零外呼。"""
    calls = _ok_legs(monkeypatch)
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "ok"
    assert calls == {"range": 1, "get": 1}
    # 复用记录已落 Redis，键含用户 + 副本文件标识。
    key = tv._usercopy_probe_ok_reuse_key(7, COPY_FID)
    assert fake_redis.store.get(key) == "1"
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "ok"
    assert calls == {"range": 1, "get": 1}


def test_decision_reuse_survives_verdict_cache_clear(fake_redis,
                                                     monkeypatch):
    """判定缓存被清（模拟另一进程内存缓存未命中）时，Redis 复用
    仍挡住重探——这是跨进程复用的立身之本。"""
    calls = _ok_legs(monkeypatch)
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "ok"
    _clear_verdict_cache()
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "ok"
    assert calls == {"range": 1, "get": 1}


# ---------- quota / error 绝不复用 ----------

def test_decision_quota_never_reused(fake_redis, monkeypatch):
    """quota 裁决：判定缓存内吃缓存（2026-10-31 定案不变）；
    缓存到期后当场重探——复用层绝不给 quota 留记录。"""
    calls = _stub_legs(
        monkeypatch,
        (200, "text/html; charset=utf-8", QUOTA_HTML),
        AssertionError("Range 已判 quota 不许跑第二腿"))
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "quota"
    assert calls == {"range": 1, "get": 0}
    assert fake_redis.store == {}
    # 判定缓存仍在：零重探（现口径逐字不变）。
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "quota"
    assert calls == {"range": 1, "get": 0}
    # 判定缓存到期：必须当场重探，不许吃任何复用。
    _clear_verdict_cache()
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "quota"
    assert calls == {"range": 2, "get": 0}


def test_decision_error_never_reused(fake_redis, monkeypatch):
    """探测本身失败（error）不写复用：下一次判定照常重探。"""
    calls = _stub_legs(monkeypatch,
                       RuntimeError("网络抖动"),
                       AssertionError("第一腿已失败不许跑第二腿"))
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "error"
    assert fake_redis.store == {}
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "error"
    assert calls == {"range": 2, "get": 0}


# ---------- Redis 不可用：行为等于现状 ----------

def test_decision_redis_none_falls_back_to_fresh_probe(monkeypatch):
    """get_redis 返回 None（未启用/熔断）：两次判定各探一次，
    与复用上线前逐字一致，且不抛错。"""
    monkeypatch.setattr("backend.database.get_redis", lambda: None)
    calls = _ok_legs(monkeypatch)
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "ok"
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "ok"
    assert calls == {"range": 2, "get": 2}


def test_decision_redis_raising_falls_back_to_fresh_probe(monkeypatch):
    """Redis 调用抛错：吞掉异常照常探测，绝不挡播放。"""
    monkeypatch.setattr("backend.database.get_redis",
                        lambda: _RaisingRedis())
    calls = _ok_legs(monkeypatch)
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "ok"
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "ok"
    assert calls == {"range": 2, "get": 2}


# ---------- 边界：池路径与开关 ----------

def test_decision_poolvar_never_reused(fake_redis, monkeypatch):
    """池路径（poolvar:<variant_id>）不走复用：熔断恢复探测必须
    当场探——两次判定各探一次，Redis 里不留池的复用记录。"""
    calls = _ok_legs(monkeypatch)
    assert tv.probe_user_copy_quota_decision(
        "poolvar:9", COPY_FID) == "ok"
    assert tv.probe_user_copy_quota_decision(
        "poolvar:9", COPY_FID) == "ok"
    assert calls == {"range": 2, "get": 2}
    assert fake_redis.store == {}


def test_decision_reuse_ttl_zero_disables(fake_redis, monkeypatch):
    """SystemConfig 把复用 TTL 设 0 = 关：两次判定各探一次。"""
    monkeypatch.setattr(tv, "_usercopy_probe_ok_reuse_ttl", lambda: 0)
    calls = _ok_legs(monkeypatch)
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "ok"
    assert tv.probe_user_copy_quota_decision(7, COPY_FID) == "ok"
    assert calls == {"range": 2, "get": 2}
    assert fake_redis.store == {}


def test_reuse_ttl_default_and_config(monkeypatch):
    """TTL 读取：无 SystemConfig 行 → 默认 300；有行 → 用行值。"""
    from backend.integrations import store as _store

    monkeypatch.setattr(_store, "get_value",
                        lambda db, key, default="": default)
    assert tv._usercopy_probe_ok_reuse_ttl() == 300
    monkeypatch.setattr(_store, "get_value",
                        lambda db, key, default="": "120")
    assert tv._usercopy_probe_ok_reuse_ttl() == 120
    monkeypatch.setattr(_store, "get_value",
                        lambda db, key, default="": "0")
    assert tv._usercopy_probe_ok_reuse_ttl() == 0
    monkeypatch.setattr(_store, "get_value",
                        lambda db, key, default="": "abc")
    assert tv._usercopy_probe_ok_reuse_ttl() == 300
