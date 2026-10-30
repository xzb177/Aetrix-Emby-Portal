"""TG Bot poller 重投递刷屏回归锁（2026-10-30 19:47 私聊刷屏一百多条实证）。

根因：#1527 变基时 heredoc 传脚本吃掉了 _poll_once 里 params 字典
键的引号——{"offset": ..., "timeout": ...} 写成了 {offset: offset,
timeout: timeout}（键是变量本身）。py_compile/AST 语法均合法、当时
只有 allowed_updates 的值丢引号被测试抓到，键无覆盖，就此合入。
后果：getUpdates 请求只带 "758207xxx=758207xxx&30=30&allowed_updates
=..."（生产 httpx 日志逐字实证），offset/timeout 从未到达 Telegram：
服务器永不确认 update、长轮询失效；紧密轮询时表面正常，一旦进程
重启/轮询会话重置就整批重投递，handler 正常执行（无异常、发送全
200），面板类发送被反复重发——19:38 老虎机同局两张结果卡、19:47
起部署重建频密、重启即整批重放，演变为刷屏，offset 在本地缓慢
推进造成「处理在跑、offset 也在动」的假象。

本文件锁三条：
1. _poll_once 必须以字符串键 "offset"/"timeout" 发送参数；
2. 已提交水位（offset）之下的 update 被重投递时绝不二次分发；
3. 毒 update（分发必抛错）连投三次只处理一次，后续投递直接跳过；
   水位之上的新 update 照常处理（不丢正常消息）。
"""
from __future__ import annotations

import os
import queue
import sys
import threading

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
sys.path.insert(0, ".")

import httpx
import pytest

from backend.tg_bot import poller, router


# ---------- 1. getUpdates 参数键必须是字符串 ----------

def test_poll_once_sends_offset_and_timeout_with_string_keys(monkeypatch):
    captured = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"ok": True, "result": []}

    class _Client:
        def __init__(self, timeout=None):
            self.timeout = timeout

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, url, params=None):
            captured["url"] = url
            captured["params"] = params
            return _Resp()

    monkeypatch.setattr(httpx, "Client", _Client)
    out = poller._poll_once("TESTTOKEN", 758207512, timeout=30)
    assert out == []
    params = captured["params"]
    assert all(isinstance(k, str) for k in params), (
        f"params 键必须是字符串，实际: {[type(k).__name__ for k in params]}")
    assert params["offset"] == 758207512
    assert params["timeout"] == 30
    assert "allowed_updates" in params


# ---------- 2/3. 水位过滤：重投递不二次分发、毒 update 只处理一次 ----------

class _Session:
    def close(self):
        pass


@pytest.fixture()
def harness(monkeypatch):
    """真实 _worker_loop 线程＋可计数/可抛错的 dispatch 打桩。"""
    state = {"dispatched": [], "saved": []}
    monkeypatch.setattr(poller, "_UPDATE_QUEUE", queue.Queue())

    import backend.database as dbmod
    monkeypatch.setattr(dbmod, "SessionLocal", lambda: _Session())

    def dispatch(db, u):
        state["dispatched"].append(u["update_id"])
        if u.get("poison"):
            raise RuntimeError("poison update")

    monkeypatch.setattr(router, "dispatch", dispatch)
    t = threading.Thread(target=poller._worker_loop, daemon=True)
    t.start()
    yield state
    poller._UPDATE_QUEUE.put(None)
    t.join(timeout=5)


def _deliver(state, updates, offset):
    return poller._process_updates(
        updates, offset, save=lambda off: state["saved"].append(off))


def test_redelivered_committed_batch_is_not_redispatched(harness):
    batch = [{"update_id": 100}, {"update_id": 101}]
    off = _deliver(harness, batch, 100)
    assert off == 102
    assert harness["dispatched"] == [100, 101]
    # Telegram 整批重投递（重启/会话重置）：水位之下，一条都不许再分发
    off2 = _deliver(harness, batch, off)
    assert off2 == 102
    assert harness["dispatched"] == [100, 101]


def test_poison_update_processed_once_across_redeliveries(harness):
    poison = [{"update_id": 200, "poison": True}]
    off = _deliver(harness, poison, 200)   # 分发抛错，但批次照常提交
    assert off == 201
    assert harness["dispatched"] == [200]
    # 同一条毒 update 再投两次：水位已过，直接跳过，不许再处理
    _deliver(harness, poison, off)
    _deliver(harness, poison, off)
    assert harness["dispatched"] == [200]
    # 水位之上的新消息照常处理（不丢）
    off = _deliver(harness, [{"update_id": 201}], off)
    assert off == 202
    assert harness["dispatched"] == [200, 201]


def test_mixed_batch_only_new_updates_dispatched(harness):
    batch = [{"update_id": 105}, {"update_id": 103}, {"update_id": 104}]
    off = _deliver(harness, batch, 104)  # 103 已在水位之下
    assert off == 106
    assert harness["dispatched"] == [105, 104]
