"""P0 起播加速测试：strm_prewarm 预热器 / Drive 跳转缓存 / SA token 预铸。

- strm_prewarm：配置读取、去重、开关、非 .strm 跳过、指标
- streaming 跳转缓存：file_id 提取、缓存读写、TTL 过期、403 失效、0=关闭
- drive_auth 预铸：sa_count、_all_sa_dicts 去重、premint 幂等（mock 换 token）
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# strm_prewarm
# ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self, values: dict):
        self._values = values

    def get_values(self, db, keys, defaults):
        return {k: self._values.get(k, defaults.get(k, "")) for k in keys}


def _fake_db(values: dict):
    import backend.emby_server.strm_prewarm as sp
    return SimpleNamespace(_sp_values=values)


@pytest.fixture
def clean_prewarm():
    import backend.emby_server.strm_prewarm as sp
    sp.reset_state()
    yield sp
    sp.reset_state()


def _patch_store(sp, values: dict):
    """把 strm_prewarm.read_config 里的 store.get_values 替换为假数据。"""
    fake = _FakeStore(values)
    return patch("backend.integrations.store.get_values", fake.get_values)


def test_prewarm_read_config_defaults(clean_prewarm):
    sp = clean_prewarm
    with _patch_store(sp, {}):
        cfg = sp.read_config(object())
    assert cfg["enabled"] is True
    assert cfg["dedup_seconds"] == 300
    assert cfg["max_concurrent"] == 4


def test_prewarm_read_config_disabled(clean_prewarm):
    sp = clean_prewarm
    with _patch_store(sp, {"strm_prewarm_enabled": "false"}):
        cfg = sp.read_config(object())
    assert cfg["enabled"] is False


def test_prewarm_read_config_bounds(clean_prewarm):
    sp = clean_prewarm
    with _patch_store(sp, {"strm_prewarm_max_concurrent": "999"}):
        cfg = sp.read_config(object())
    assert cfg["max_concurrent"] == 16  # 钳到上限
    with _patch_store(sp, {"strm_prewarm_max_concurrent": "abc"}):
        cfg = sp.read_config(object())
    assert cfg["max_concurrent"] == 4  # 非法回默认


def test_prewarm_skips_non_strm(clean_prewarm):
    sp = clean_prewarm
    item = SimpleNamespace(file_path="/media/movie.mkv", library=None, guid="x")
    asyncio.run(sp.warm(object(), item))
    # 非 .strm 直接返回，不抛异常，warmed 计数不变
    assert sp.metrics_snapshot()["counters"]["warmed"] == 0


def test_prewarm_maybe_warm_disabled(clean_prewarm):
    sp = clean_prewarm
    item = SimpleNamespace(file_path="/strm/a.strm", library=None, guid="x")
    with _patch_store(sp, {"strm_prewarm_enabled": "0"}):
        asyncio.run(sp.maybe_warm(object(), item))
    snap = sp.metrics_snapshot()["counters"]
    assert snap["skipped_disabled"] >= 1


def test_prewarm_dedup(clean_prewarm):
    sp = clean_prewarm
    item = SimpleNamespace(file_path="/strm/b.strm", library=None, guid="y")

    async def fake_warm(db, item):
        pass

    with _patch_store(sp, {}), patch.object(sp, "warm", fake_warm):
        asyncio.run(sp.maybe_warm(object(), item))
        asyncio.run(sp.maybe_warm(object(), item))  # 第二次命中去重
    snap = sp.metrics_snapshot()["counters"]
    assert snap["skipped_dedup"] >= 1


def test_prewarm_metrics_never_throws(clean_prewarm):
    sp = clean_prewarm
    assert isinstance(sp.metrics_snapshot(), dict)


# ---------------------------------------------------------------------------
# streaming 跳转缓存
# ---------------------------------------------------------------------------

def test_drive_file_id_extract():
    from backend.emby_server.streaming import _drive_file_id
    url = "https://www.googleapis.com/drive/v3/files/abc123XYZ?alt=media"
    assert _drive_file_id(url) == "abc123XYZ"
    assert _drive_file_id("https://example.com/x") is None
    assert _drive_file_id("") is None
    assert _drive_file_id("not a url") is None


def test_redirect_cache_set_get():
    from backend.emby_server import streaming as st
    st._redirect_cache.clear()
    st._redirect_cache_set("fid1", "https://usercontent/final")
    assert st._redirect_cache_get("fid1") == "https://usercontent/final"
    st._redirect_cache.clear()


def test_redirect_cache_invalidate():
    from backend.emby_server import streaming as st
    st._redirect_cache.clear()
    st._redirect_cache_set("fid2", "https://usercontent/final2")
    st._redirect_cache_invalidate("fid2")
    assert st._redirect_cache_get("fid2") is None
    st._redirect_cache.clear()


def test_redirect_cache_ttl_expiry():
    from backend.emby_server import streaming as st
    st._redirect_cache.clear()
    # 手动写入一条已过期的
    with st._redirect_cache_lock:
        st._redirect_cache["fid3"] = ("https://usercontent/old", time.monotonic() - 1)
    assert st._redirect_cache_get("fid3") is None
    st._redirect_cache.clear()


def test_redirect_cache_disabled_when_ttl_zero(monkeypatch):
    from backend.emby_server import streaming as st
    monkeypatch.setattr(st, "_DRIVE_REDIRECT_CACHE_TTL", 0.0)
    st._redirect_cache.clear()
    st._redirect_cache_set("fid4", "https://usercontent/x")
    assert st._redirect_cache_get("fid4") is None


# ---------------------------------------------------------------------------
# drive_auth 预铸
# ---------------------------------------------------------------------------

def test_sa_count_no_crash():
    from backend.emby_server import drive_auth
    # 无 SA 文件时返回 0，不抛异常
    assert isinstance(drive_auth.sa_count(), int)


def test_premint_no_sa():
    from backend.emby_server import drive_auth
    with patch.object(drive_auth, "_all_sa_dicts", return_value=[]):
        result = drive_auth.premint_tokens()
    assert result == {"total": 0, "ok": 0, "failed": 0}


def test_premint_success_mocked():
    from backend.emby_server import drive_auth
    fake_sas = [
        {"client_email": "a@x.iam.gserviceaccount.com", "private_key": "k"},
        {"client_email": "b@x.iam.gserviceaccount.com", "private_key": "k"},
    ]
    drive_auth._reset_state()
    try:
        with patch.object(drive_auth, "_all_sa_dicts", return_value=fake_sas), \
             patch("backend.emby_server.drive_auth._load_drive_changes") as mock_load:
            mock_dc = SimpleNamespace(
                _sa_access_token=lambda sa, min_remaining=0: "tok-" + sa["client_email"])
            mock_load.return_value = mock_dc
            result = drive_auth.premint_tokens(max_workers=2)
        assert result["total"] == 2
        assert result["ok"] == 2
        assert result["failed"] == 0
        # 缓存里有 token 了（直接查 _token_cache，不走 _pick_sa 的真实 SA 发现）
        assert len(drive_auth._token_cache) == 2
    finally:
        drive_auth._reset_state()


def test_premint_idempotent_skips_cached():
    """已缓存且有效时跳过换取（幂等）。"""
    from backend.emby_server import drive_auth
    fake_sas = [{"client_email": "c@x.iam.gserviceaccount.com", "private_key": "k"}]
    drive_auth._reset_state()
    try:
        calls = []

        def fake_token(sa, min_remaining=0):
            calls.append(sa["client_email"])
            return "tok-c"

        with patch.object(drive_auth, "_all_sa_dicts", return_value=fake_sas), \
             patch("backend.emby_server.drive_auth._load_drive_changes") as mock_load:
            mock_load.return_value = SimpleNamespace(_sa_access_token=fake_token)
            drive_auth.premint_tokens(max_workers=1)
            drive_auth.premint_tokens(max_workers=1)  # 第二次应命中缓存
        assert len(calls) == 1  # 只换了一次
    finally:
        drive_auth._reset_state()


def test_all_sa_dicts_dedup():
    from backend.emby_server import drive_auth
    dup_sas = [
        {"client_email": "d@x.iam.gserviceaccount.com", "private_key": "k1"},
        {"client_email": "d@x.iam.gserviceaccount.com", "private_key": "k2"},
        {"client_email": "", "private_key": "k3"},  # 无 email 跳过
    ]
    with patch("backend.emby_server.drive_auth._load_drive_changes") as mock_load:
        mock_load.return_value = SimpleNamespace(_discover_sa_files=lambda: dup_sas)
        # _read_sa_file 不会被调用（已经是 dict），直接走 _is_sa_dict
        result = drive_auth._all_sa_dicts()
    assert len(result) == 1
    assert result[0]["client_email"] == "d@x.iam.gserviceaccount.com"
