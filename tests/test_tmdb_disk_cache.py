"""TMDB 磁盘缓存（第 7 批）：命中免请求 / TTL / 阴性缓存 / 容量淘汰 / 单飞 / 降级。

与 ``tests/test_tmdb_cache.py``（进程内 L1 缓存 + 详情预取口径，v2.31）互补：
这里守的是磁盘 L2（跨进程/跨重启），纯文件缓存、不需要数据库。
"""
import json
import os
import threading
import time
from unittest import mock

import pytest

from backend.emby_server import tmdb, tmdb_cache


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """把缓存目录指到临时目录，并复位运行期降级/告警状态（配置是动态读 env 的）"""
    d = tmp_path / "tmdb-cache"
    monkeypatch.setenv("EMBY_TMDB_CACHE_DIR", str(d))
    monkeypatch.setattr(tmdb_cache, "_disabled", False)
    monkeypatch.setattr(tmdb_cache, "_warned", False)
    return d


def _fake_client(monkeypatch, api_results=None):
    """造一个不联网的 TmdbClient：_get 按查询词返回预设结果，并计数。

    注意要把 api_keys/session 注入（否则 ``_search_raw`` 在未配置时直接短路）。
    结果里必须带 name/title：``_hit_score`` 认标题字段，光有 id 不算命中。
    """
    client = tmdb.TmdbClient()
    calls = []

    def fake_get(path, params):
        query = params.get("query")
        calls.append((path, query))
        preset = (api_results or {}).get(query)
        if preset is not None:
            return preset
        return {"results": [{"id": abs(hash(query)) % 9000 + 1, "name": query,
                             "title": query}]}

    monkeypatch.setattr(client, "_get", fake_get)
    client.api_keys = ["fake-key"]
    client.api_key = "fake-key"
    client.session = object()          # 非 None 即可：_ensure_session 只补记指纹
    client._cache.clear()
    return client, calls


def test_search_roundtrip_and_ttl(cache_dir):
    """写入后命中；把时间戳改老 → 过期未命中且文件被删"""
    tmdb_cache.save_search("tv", "漫长的季节", 2023, [{"id": 1}])
    hit, payload = tmdb_cache.load_search("tv", "漫长的季节", 2023)
    assert hit and payload == [{"id": 1}]

    path = tmdb_cache._key_path("tv", "漫长的季节", 2023)
    with open(path, encoding="utf-8") as f:
        envelope = json.load(f)
    envelope["at"] -= tmdb_cache._cfg_ttl_search() + 10
    with open(path, "w", encoding="utf-8") as f:
        json.dump(envelope, f)

    hit, payload = tmdb_cache.load_search("tv", "漫长的季节", 2023)
    assert not hit and payload is None
    assert not os.path.exists(path), "过期文件应被顺手删除"


def test_negative_search_has_shorter_ttl(cache_dir):
    """阴性（空 results）也落盘，但用更短的 TTL"""
    tmdb_cache.save_search("tv", "注定搜不到", 0, [])
    hit, payload = tmdb_cache.load_search("tv", "注定搜不到", 0)
    assert hit and payload == []

    path = tmdb_cache._key_path("tv", "注定搜不到", 0)
    with open(path, encoding="utf-8") as f:
        envelope = json.load(f)
    # 比阴性 TTL 老、但比阳性 TTL 新：必须按阴性口径判过期
    envelope["at"] -= tmdb_cache._cfg_ttl_negative() + 10
    with open(path, "w", encoding="utf-8") as f:
        json.dump(envelope, f)
    hit, _ = tmdb_cache.load_search("tv", "注定搜不到", 0)
    assert not hit, "阴性结果必须用短 TTL"


def test_normalized_key_dedupes(cache_dir):
    """键归一化：大小写/全角标点不同的同一部片共用一份缓存；年份是独立维度"""
    tmdb_cache.save_search("tv", tmdb._norm_text("90 Day: The Last Resort"), 2023,
                           [{"id": 9}])
    hit, payload = tmdb_cache.load_search(
        "tv", tmdb._norm_text("90 Day： The Last Resort"), 2023)
    assert hit and payload == [{"id": 9}]
    hit, _ = tmdb_cache.load_search("tv", tmdb._norm_text("90 Day: The Last Resort"), 2024)
    assert not hit


def test_details_roundtrip_and_no_save_on_failure(cache_dir):
    """details 落盘/命中；空数据（请求失败）不落盘"""
    tmdb_cache.save_details("tv", "42", {"id": 42, "name": "x"})
    hit, payload = tmdb_cache.load_details("tv", "42")
    assert hit and payload == {"id": 42, "name": "x"}

    assert tmdb_cache.save_details("tv", "43", None) is False
    hit, _ = tmdb_cache.load_details("tv", "43")
    assert not hit


def test_client_search_hits_disk_across_instances(cache_dir, monkeypatch):
    """端到端：换一个全新 client 实例（L1 空），同一搜索 0 次 API 调用"""
    preset = {"Some Show (2024)": {"results": []},          # 带年份的候选没命中
              "Some Show": {"results": [{"id": 7, "name": "Some Show"}]}}
    client1, calls1 = _fake_client(monkeypatch, api_results=preset)
    got = client1.search("Some Show (2024)", 2024, "series")
    assert got and got["id"] == 7 and got["name"] == "Some Show"
    assert calls1, "首次必须真的发请求"

    client2, calls2 = _fake_client(monkeypatch, api_results=preset)   # 新实例 = L1 全空
    got2 = client2.search("Some Show (2024)", 2024, "series")
    assert got2 and got2["id"] == 7
    assert calls2 == [], "第二次必须完全走磁盘缓存，一个请求都不发"

    # 大小写变体（归一化同键）也全命中
    client3, calls3 = _fake_client(monkeypatch, api_results=preset)
    got3 = client3.search("some show (2024)", 2024, "series")
    assert got3 and got3["id"] == 7
    assert calls3 == [], "归一化后同键的写法必须复用缓存"


def test_client_does_not_cache_failures(cache_dir, monkeypatch):
    """请求失败（data=None）不落盘：下一轮重试照旧真的发请求"""
    client, _ = _fake_client(monkeypatch)
    monkeypatch.setattr(client, "_get", lambda path, params: None)   # 网络失败
    assert client.search("失败剧", 2020, "series") is None
    assert not os.path.exists(
        tmdb_cache._key_path("tv", tmdb._norm_text("失败剧"), 2020))

    client2, calls2 = _fake_client(monkeypatch)
    assert client2.search("失败剧", 2020, "series") is not None
    assert calls2, "失败结果不能被缓存固化成「没有」"


def test_client_details_hits_disk(cache_dir, monkeypatch):
    # v2.51.0 起 details 带 append_to_response=credits：mock 载荷也要带 credits，
    # 否则会被当成升级前的老缓存而重拉（见下个测试）。
    payload = {"id": "42", "name": "剧", "credits": {"cast": []}}
    client1, _ = _fake_client(monkeypatch)
    with mock.patch.object(client1, "_get", return_value=dict(payload)):
        d1 = client1.details("42", "series")
    assert d1 == payload
    client2, _ = _fake_client(monkeypatch)
    with mock.patch.object(client2, "_get",
                           side_effect=AssertionError("不应再发请求")):
        d2 = client2.details("42", "series")
    assert d2 == payload


def test_client_details_refetches_stale_payload_without_credits(cache_dir, monkeypatch):
    """v2.51.0 前的老缓存（无 credits 键）视为过期：重拉一次，并用新载荷覆盖旧缓存。"""
    tmdb_cache.save_details("tv", "42", {"id": "42", "name": "老载荷"})
    client, _ = _fake_client(monkeypatch)
    fresh = {"id": "42", "name": "新载荷", "credits": {"cast": []}}
    with mock.patch.object(client, "_get", return_value=dict(fresh)) as m_get:
        got = client.details("42", "series")
    assert m_get.call_count == 1
    assert got == fresh
    # 新载荷已落盘：下一次不再请求
    client2, _ = _fake_client(monkeypatch)
    with mock.patch.object(client2, "_get",
                           side_effect=AssertionError("不应再发请求")):
        assert client2.details("42", "series") == fresh


def test_single_flight_merges_concurrent_searches(cache_dir, monkeypatch):
    """单飞：多线程同时搜同一片名 → 只有一个线程真的发请求"""
    client, calls = _fake_client(monkeypatch)
    release = threading.Event()

    def slow_get(path, params):
        calls.append(params.get("query"))
        release.wait(2)          # 第一个线程按住不放，放大竞态窗口
        return {"results": [{"id": 1, "name": params.get("query")}]}

    monkeypatch.setattr(client, "_get", slow_get)
    got: list = []

    def worker():
        got.append(client.search("并发剧", 2021, "series"))

    t1 = threading.Thread(target=worker)
    t1.start()
    time.sleep(0.15)             # 确保 t1 已拿到锁并进入请求
    t2 = threading.Thread(target=worker)
    t2.start()
    time.sleep(0.2)
    release.set()
    t1.join(3)
    t2.join(3)
    assert got == [{"id": 1, "name": "并发剧"}] * 2
    assert len(calls) == 1, f"单飞必须合并并发请求（实际 {len(calls)} 次）"


def test_invalidate_search_purges_entries(cache_dir, monkeypatch):
    """按片名失效：该片名候选的缓存（含阴性）都被删掉"""
    # 语言是缓存维度（v2.49.0）：读写/失效都带同一语言
    monkeypatch.setattr(tmdb, "preferred_language", lambda db=None: "zh-CN")
    client, _ = _fake_client(monkeypatch, api_results={
        "Some Show (2024)": {"results": []},
        "Some Show": {"results": []},
    })
    client.search("Some Show (2024)", 2024, "series")
    path = tmdb_cache._key_path("tv", tmdb._norm_text("Some Show"), 2024, "zh-CN")
    assert os.path.exists(path)

    removed = tmdb_cache.invalidate_search("Some Show (2024)", 2024, "series", "zh-CN")
    assert removed >= 1
    assert not os.path.exists(path)


def test_prune_evicts_oldest_over_cap(cache_dir, monkeypatch):
    """容量淘汰：超限时按 mtime 从旧到新删到 80%"""
    monkeypatch.setenv("EMBY_TMDB_CACHE_MB", "1")
    keys = [("tv", "老条目", 0), ("tv", "中条目", 0), ("tv", "新条目", 0)]
    paths = []
    for endpoint, name, year in keys:
        tmdb_cache.save_search(endpoint, name, year, ["x" * 400_000])  # 每个 ~400KB
        paths.append(tmdb_cache._key_path(endpoint, name, year))
    now = time.time()
    os.utime(paths[0], (now - 7200, now - 7200))   # 最旧
    os.utime(paths[1], (now - 3600, now - 3600))   # 居中

    result = tmdb_cache.prune(force=True)
    assert result["evicted"] >= 1, result
    assert not os.path.exists(paths[0]), "最旧的必须先被淘汰"
    assert os.path.exists(paths[2]), "最新的必须保留"
    assert result["bytes"] <= int(1 * 1024 * 1024 * 0.8) + 1


def test_expired_files_removed_by_prune(cache_dir, monkeypatch):
    """超过最大 TTL 的文件由巡检删除（不依赖读取时顺手删）

    巡检无法从文件名分辨类型，过期判定用三类 TTL 的最大值——所以三个 TTL
    都要压到测试窗口内，这本身也是「最保守的过期口径」的契约。
    """
    for env in ("EMBY_TMDB_TTL_SEARCH", "EMBY_TMDB_TTL_NEGATIVE",
                "EMBY_TMDB_TTL_DETAILS"):
        monkeypatch.setenv(env, "60")
    tmdb_cache.save_details("tv", "999", {"id": 999})
    path = tmdb_cache._key_path("tv", "999", 0)
    os.utime(path, (time.time() - 3600, time.time() - 3600))
    result = tmdb_cache.prune(force=True)
    assert result["expired_removed"] >= 1
    assert not os.path.exists(path)


def test_graceful_degradation_on_unwritable_dir(tmp_path, monkeypatch):
    """缓存目录建不出来 → 自动降级：不抛异常，刮削照常走 API"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("EMBY_TMDB_CACHE_DIR", str(blocker / "sub"))  # 父级是文件
    monkeypatch.setattr(tmdb_cache, "_warned", False)
    monkeypatch.setattr(tmdb_cache, "_disabled", False)

    assert tmdb_cache.save_search("tv", "x", 0, []) is False
    assert tmdb_cache.enabled() is False, "持续不可用应整体禁用"

    client, calls = _fake_client(monkeypatch)
    assert client.search("剧", 2022, "series") is not None
    assert calls, "降级后必须照常真的发请求"


def test_cache_disabled_by_env(cache_dir, monkeypatch):
    """EMBY_TMDB_CACHE=0：磁盘读写全部短路，API 照常（每次新实例都真的请求）"""
    monkeypatch.setenv("EMBY_TMDB_CACHE", "0")
    writes_before = tmdb_cache.stats()["writes"]
    client1, calls1 = _fake_client(monkeypatch)
    assert client1.search("剧", 2023, "series") is not None
    client2, calls2 = _fake_client(monkeypatch)
    assert client2.search("剧", 2023, "series") is not None
    assert calls1 and calls2, "缓存关闭时每次都真的请求"
    assert tmdb_cache.stats()["writes"] == writes_before, "关闭时一个文件都不该写"


# ---------------------------------------------------------------------------
# 双份快照 + 命中计数（2026-10-24，FMBY 学习批一 #8）
# ---------------------------------------------------------------------------

def test_envelope_keeps_normalized_raw_and_hit_count(cache_dir):
    """归一化载荷 + 原始响应双份都在信封里；命中累计随每次读 +1"""
    raw = {"results": [{"id": 1, "name": "漫长的季节"}], "page": 1, "total_pages": 1}
    tmdb_cache.save_search("tv", "漫长的季节", 2023, [{"id": 1}], raw=raw)

    hit, envelope = tmdb_cache.load_entry("tv", "漫长的季节", 2023, kind="search")
    assert hit
    assert envelope["v"] == [{"id": 1}]            # 归一化那份
    assert envelope["raw"] == raw                  # 原始那份，一字不差
    assert envelope["hits"] == 1                   # 这次读是第 1 次命中

    tmdb_cache.load_search("tv", "漫长的季节", 2023)
    hit, envelope = tmdb_cache.load_entry("tv", "漫长的季节", 2023, kind="search")
    assert envelope["hits"] == 3                   # 每次命中都累计（load_entry 自身也算）


def test_oversized_raw_is_stored_truncated_and_marked(cache_dir, monkeypatch):
    """原始响应超单条上限 → 只存截断标记（注明截断 + 原始字节数），不撑爆缓存盘"""
    monkeypatch.setenv("EMBY_TMDB_CACHE_RAW_MAX_KB", "1")   # 上限 1 KB
    big_raw = {"overview": "x" * 5000}
    tmdb_cache.save_details("movie", "999", {"id": 999, "title": "大片"}, raw=big_raw)

    hit, envelope = tmdb_cache.load_entry("movie", "999", kind="details")
    assert hit
    assert envelope["v"] == {"id": 999, "title": "大片"}     # 归一化那份不受影响
    stored = envelope["raw"]
    assert stored["_truncated"] is True                     # 截断必须注明
    assert stored["_bytes"] > 1024
    assert stored["head"]                                    # 留了开头节选


def test_legacy_envelope_without_raw_still_reads(cache_dir):
    """老信封（只有 at/v）照样命中：hits 按 0 起、raw 为 None，命中后补上计数键"""
    path = tmdb_cache._key_path("movie", "278", 0)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"at": time.time(), "v": {"id": 278, "title": "肖申克的救赎"}}, f)

    hit, payload = tmdb_cache.load_details("movie", "278")
    assert hit and payload["id"] == 278
    with open(path, encoding="utf-8") as f:
        assert json.load(f)["hits"] == 1                    # 命中后计数键已补上
