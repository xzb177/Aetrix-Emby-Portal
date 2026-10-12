"""P0 起播加速测试：strm_prewarm 预热器 / Drive 跳转缓存 / SA token 预铸。

- strm_prewarm：配置读取、去重、开关、非 .strm 跳过、指标
- streaming 跳转缓存：file_id 提取、缓存读写、TTL 过期、403 失效、0=关闭
- drive_auth 预铸：sa_count、_all_sa_dicts 去重、premint 幂等（mock 换 token）
"""
from __future__ import annotations

import asyncio
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
    # _all_sa_dicts 结果模块级缓存：先重置，避免其他测试残留的缓存干扰
    drive_auth._reset_state()
    try:
        with patch("backend.emby_server.drive_auth._load_drive_changes") as mock_load:
            mock_load.return_value = SimpleNamespace(_discover_sa_files=lambda: dup_sas)
            # _read_sa_file 不会被调用（已经是 dict），直接走 _is_sa_dict
            result = drive_auth._all_sa_dicts()
        assert len(result) == 1
        assert result[0]["client_email"] == "d@x.iam.gserviceaccount.com"
    finally:
        drive_auth._reset_state()


# ---------------------------------------------------------------------------
# 回归：.strm 预热不能被 IsPlayback 门控（2026-10-12 生产事故）
# ---------------------------------------------------------------------------

def test_strm_prewarm_not_gated_by_isplayback():
    """回归测试：.strm 预热必须在 IsPlayback 门控之外。

    2026-10-12 生产证实：客户端不带 IsPlayback=true，预热被包在门控里
    导致从未触发（metrics 全 0）。修复后 .strm 预热每次 PlaybackInfo 都触发，
    只有 10MB 文件预热保留 IsPlayback 门控。
    """
    import inspect
    from backend.emby_server import api as api_mod
    src = inspect.getsource(api_mod.playback_info)
    lines = src.splitlines()

    gate_idx = next(
        i for i, l in enumerate(lines) if "if is_pb and item.file_path:" in l
    )
    gate_indent = len(lines[gate_idx]) - len(lines[gate_idx].lstrip())

    warm_call_idx = next(
        i for i, l in enumerate(lines) if "strm_prewarm.maybe_warm" in l
    )
    # 从调用点往上找最近的 `if item.file_path:`（.strm 预热的守卫）
    guard_idx = next(
        i for i in range(warm_call_idx - 1, gate_idx, -1)
        if lines[i].lstrip().startswith("if item.file_path:")
    )
    guard_indent = len(lines[guard_idx]) - len(lines[guard_idx].lstrip())

    assert guard_indent == gate_indent, (
        f".strm 预热守卫缩进({guard_indent}) != IsPlayback 门控缩进({gate_indent})，"
        "预热仍被 IsPlayback 参数门控"
    )


def test_10mb_prewarm_keeps_isplayback_gate():
    """10MB 文件预热保留 IsPlayback 门控（读 10MB 成本高，只在真点播时触发）。"""
    import inspect
    from backend.emby_server import api as api_mod
    src = inspect.getsource(api_mod.playback_info)
    lines = src.splitlines()

    gate_idx = next(
        i for i, l in enumerate(lines) if "if is_pb and item.file_path:" in l
    )
    # playback_prewarm.maybe_prewarm 必须在门控块内部（缩进更大）
    warm_idx = next(
        i for i, l in enumerate(lines) if "playback_prewarm.maybe_prewarm" in l
    )
    assert warm_idx > gate_idx, "10MB 预热调用应在门控之后"
    gate_indent = len(lines[gate_idx]) - len(lines[gate_idx].lstrip())
    warm_indent = len(lines[warm_idx]) - len(lines[warm_idx].lstrip())
    assert warm_indent > gate_indent, "10MB 预热应保留在 IsPlayback 门控内部"


# ---------------------------------------------------------------------------
# resolve_drive_final_url：跟随跳转 + 写 P0-2 缓存
# ---------------------------------------------------------------------------

def _make_fake_streaming_client(statuses: list):
    """构造假 httpx 客户端：按顺序返回指定状态码/Location。"""
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, status_code, headers=None):
            self.status_code = status_code
            self.headers = headers or {}
            self.closed = False

        async def aclose(self):
            self.closed = True

    class FakeClient:
        def build_request(self, method, url, headers=None):
            return (method, url, headers)

        async def send(self, request, stream=True):
            method, url, headers = request
            calls.append(url)
            code, location = statuses[min(len(calls) - 1, len(statuses) - 1)]
            h = {"location": location} if location else {}
            return FakeResponse(code, h)

    return FakeClient(), calls


def test_resolve_final_url_follows_redirect():
    """307 跳转跟随 → 返回最终 URL（跳转缓存已删除，每次都跟随）。"""
    import backend.emby_server.streaming as st

    file_id = "TEST_WARM_REDIRECT_X1"
    api_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"
    final_url = "https://drive.usercontent.google.com/download?id=XYZ"
    orig_url = "https://example.com/movie.strm"

    fake_client, calls = _make_fake_streaming_client([
        (307, final_url),   # 第一跳：API 地址 → 307 到 usercontent
        (200, None),        # 第二跳：最终地址 200
    ])

    async def run():
        from urllib.parse import urljoin
        with patch("backend.emby_server.streaming.get_relay_client", return_value=fake_client):
            with patch("backend.emby_server.drive_auth.drive_api_media_url", return_value=api_url):
                # _next_redirect 的内网校验已有专门测试，这里只测跳转跟随逻辑
                with patch("backend.emby_server.streaming._next_redirect",
                           side_effect=lambda base, loc: (urljoin(base, loc), {})):
                    return await st.resolve_drive_final_url(orig_url, {"Authorization": "Bearer <redacted>"})

    result = asyncio.run(run())
    assert result == final_url, f"应返回最终 URL，实际 {result}"
    # 两跳都走了
    assert calls == [api_url, final_url], f"应按顺序请求两跳，实际 {calls}"



def test_resolve_final_url_returns_none_on_redirect_loop():
    """跳转超限时静默返回 None，不抛异常。"""
    import backend.emby_server.streaming as st

    file_id = "TEST_WARM_LOOP_X2"
    api_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"

    # 永远 307 指回自己
    fake_client, _ = _make_fake_streaming_client([(307, api_url)] * 10)

    async def run():
        with patch("backend.emby_server.streaming.get_relay_client", return_value=fake_client):
            with patch("backend.emby_server.drive_auth.drive_api_media_url", return_value=api_url):
                return await st.resolve_drive_final_url("https://example.com/loop.strm")

    assert asyncio.run(run()) is None


def test_resolve_final_url_returns_none_on_bad_target():
    """非法重定向目标（_next_redirect 拒绝）时静默返回 None。"""
    import backend.emby_server.streaming as st

    file_id = "TEST_WARM_BAD_X3"
    api_url = f"https://www.googleapis.com/drive/v3/files/{file_id}?alt=media"

    # 307 指向内网地址，_next_redirect 会拒绝
    fake_client, _ = _make_fake_streaming_client([(307, "http://192.168.1.1/evil")])

    async def run():
        with patch("backend.emby_server.streaming.get_relay_client", return_value=fake_client):
            with patch("backend.emby_server.drive_auth.drive_api_media_url", return_value=api_url):
                return await st.resolve_drive_final_url("https://example.com/evil.strm")

    assert asyncio.run(run()) is None
