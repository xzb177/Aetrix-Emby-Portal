# -*- coding: utf-8 -*-
"""FMBY 批一 #3：按「挂载+路径前缀」限同时播放路数（``stream_limits``）。

定稿 ``~/workspace/reports/fmby-b1-item3-stream-limit-plan-20261024.md``，
逐条落 §三 的测试钉：

1. 达上限拒绝：第 max+1 个不同租约被拒且拒绝原因非空（不许静默失败）。
2. 未达放行；同租约重复 acquire 只计 1（Range 分片刷新不重复占位）。
3. 播放结束释放后可再播（release 走 Stopped 的同一路径）。
4. 计数泄漏靠 TTL 收敛：租约过期后名额自动回来，不靠 release。
5. 两进程口径：两份独立模块状态共用同一 Redis 后端，合计计数不翻倍
   ——计数只在 Redis 里，模块自身没有进程内计数状态。
6. 无策略零行为变化：无策略 / 坏 JSON / 非挂载路径一律放行且不碰 Redis。
7. Redis 宕机 fail-open：取不到连接或脚本抛错时放行。
8. 多策略嵌套：内外两条都命中须全过，外层满时回滚已占名额不留半截。

全离线：Redis 用内存 FakeRedis（只实现本模块用到的 eval/zrem 语义），
system_configs 读取桩掉 store.get_value，db 传 None。
"""
import importlib
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import pytest

from backend.emby_server import stream_limits as sl


class FakeRedis:
    """只实现 stream_limits 用到的命令：eval（本模块唯一的那段 Lua 的
    等价语义）与 zrem。zsets: {key: {member: score}}。"""

    def __init__(self, boom=False):
        self.zsets = {}
        self.boom = boom
        self.eval_calls = 0

    def eval(self, script, numkeys, key, now, ttl, lease_id, max_streams):
        self.eval_calls += 1
        if self.boom:
            raise RuntimeError("redis down")
        zset = self.zsets.setdefault(key, {})
        # Lua 等价语义：清过期 → 已有租约刷新放行 → 未满占位 → 满返回 0
        for member in [m for m, s in zset.items() if s <= float(now)]:
            del zset[member]
        if lease_id in zset:
            zset[lease_id] = float(now) + float(ttl)
            return 1
        if len(zset) >= int(max_streams):
            return 0
        zset[lease_id] = float(now) + float(ttl)
        return 1

    def zrem(self, key, member):
        zset = self.zsets.get(key) or {}
        zset.pop(member, None)
        return 1

    def zcard(self, key):
        return len(self.zsets.get(key) or {})


PATH = "mount://3/电影/a.mkv"
POLICY_1 = '[{"mount_id": 3, "prefix": "/电影", "max_streams": 1}]'


@pytest.fixture()
def fake(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(sl, "_redis", lambda: client)
    return client


def _policies(monkeypatch, raw):
    from backend.integrations import store

    monkeypatch.setattr(store, "get_value", lambda db, key, default="": raw)


def test_limit_reached_rejects_with_reason(monkeypatch, fake):
    _policies(monkeypatch, POLICY_1)
    assert sl.acquire(None, "dev:A", "item-1", PATH) is None
    reason = sl.acquire(None, "dev:B", "item-2", PATH)
    assert reason  # 明确拒绝原因，不许静默
    assert "上限" in reason


def test_same_lease_refresh_counts_once(monkeypatch, fake):
    _policies(monkeypatch, POLICY_1)
    # 同一设备同条目的 Range 分片连发：始终同一租约，只占 1 路
    for _ in range(5):
        assert sl.acquire(None, "dev:A", "item-1", PATH) is None
    (key,) = list(fake.zsets)
    assert fake.zcard(key) == 1


def test_release_frees_slot(monkeypatch, fake):
    _policies(monkeypatch, POLICY_1)
    assert sl.acquire(None, "dev:A", "item-1", PATH) is None
    assert sl.acquire(None, "dev:B", "item-2", PATH) is not None
    sl.release(None, "dev:A", "item-1", PATH)
    assert sl.acquire(None, "dev:B", "item-2", PATH) is None


def test_expired_lease_converges_without_release(monkeypatch, fake):
    _policies(monkeypatch, POLICY_1)
    assert sl.acquire(None, "dev:A", "item-1", PATH) is None
    (key,) = list(fake.zsets)
    # 把租约 score 拨到过去 = TTL 已到期（没人 release 的泄漏场景）
    for member in fake.zsets[key]:
        fake.zsets[key][member] = 1.0
    assert sl.acquire(None, "dev:B", "item-2", PATH) is None


def test_two_processes_share_one_count(monkeypatch, fake):
    _policies(monkeypatch, POLICY_1)
    other = importlib.reload(sl)  # 模拟另一个进程的独立模块状态
    monkeypatch.setattr(other, "_redis", lambda: fake)
    try:
        assert sl.acquire(None, "dev:A", "item-1", PATH) is None
        # 另一进程再来一路：合计仍受 max=1 约束，不许各算各的翻倍
        assert other.acquire(None, "dev:B", "item-2", PATH) is not None
    finally:
        importlib.reload(sl)


def test_no_policy_zero_behavior_change(monkeypatch):
    calls = []

    def _boom_redis():
        calls.append(1)
        raise AssertionError("无策略时不许碰 Redis")

    monkeypatch.setattr(sl, "_redis", _boom_redis)
    _policies(monkeypatch, "")
    assert sl.acquire(None, "dev:A", "item-1", PATH) is None
    # 非挂载路径同样直接放行
    _policies(monkeypatch, POLICY_1)
    assert sl.acquire(None, "dev:A", "item-1", "/local/file.mkv") is None
    # 不在策略前缀下的挂载路径放行
    assert sl.acquire(None, "dev:A", "item-1", "mount://3/剧集/a.mkv") is None
    assert not calls


def test_bad_policy_json_fails_open(monkeypatch, fake):
    _policies(monkeypatch, "{not json")
    assert sl.acquire(None, "dev:A", "item-1", PATH) is None
    _policies(monkeypatch, '[{"mount_id": "x", "prefix": 1}]')
    assert sl.acquire(None, "dev:A", "item-1", PATH) is None


def test_policy_read_error_fails_open(monkeypatch, fake):
    from backend.integrations import store

    def _boom(db, key, default=""):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "get_value", _boom)
    assert sl.acquire(None, "dev:A", "item-1", PATH) is None


def test_redis_down_fails_open(monkeypatch):
    _policies(monkeypatch, POLICY_1)
    monkeypatch.setattr(sl, "_redis", lambda: None)
    assert sl.acquire(None, "dev:A", "item-1", PATH) is None
    monkeypatch.setattr(sl, "_redis", lambda: FakeRedis(boom=True))
    assert sl.acquire(None, "dev:A", "item-1", PATH) is None


def test_nested_policies_rollback_on_reject(monkeypatch, fake):
    _policies(monkeypatch,
              '[{"mount_id": 3, "prefix": "/", "max_streams": 5},'
              ' {"mount_id": 3, "prefix": "/电影", "max_streams": 1}]')
    assert sl.acquire(None, "dev:A", "item-1", PATH) is None
    # 第二路：整挂载层未满、/电影 层已满 → 拒，且整挂载层不许留半截租约
    assert sl.acquire(None, "dev:B", "item-2", PATH) is not None
    counts = sorted(fake.zcard(k) for k in fake.zsets)
    assert counts == [1, 1]


def test_parse_and_match_units():
    policies = sl.parse_policies(
        '[{"mount_id": 3, "prefix": "电影/", "max_streams": 2}]')
    assert len(policies) == 1
    assert policies[0].prefix == "/电影"  # 规范化：补前导、去尾随
    assert sl.match_policies(policies, 3, "/电影/a.mkv")
    assert sl.match_policies(policies, 3, "/电影")
    assert not sl.match_policies(policies, 3, "/电影节/a.mkv")  # 前缀按段匹配
    assert not sl.match_policies(policies, 4, "/电影/a.mkv")
    assert sl.parse_policies("") == []
    assert sl.parse_policies(None) == []
