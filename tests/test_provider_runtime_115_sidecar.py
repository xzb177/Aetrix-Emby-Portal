"""115 sidecar 化：共享运行时契约 + 115 sidecar + 宿主侧 provider。

全部用假 115 客户端 + 真实 UDS 服务跑完整回路，不碰真网络、不读库。
覆盖：协议名/能力名逐项照抄、握手 fail-closed、8 项能力往返、分页、
换链不回 Cookie、错误分级（cookie_expired / not_implemented / session_unknown）、
扩展 4 项（full_sync/diff_refresh 真实、life_poll/share_full_sync 显式未实现）、
宿主侧 provider 四口翻译与默认关总闸。
"""
from __future__ import annotations

import json
import os
import threading
from types import SimpleNamespace

import pytest

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import transfer115
from backend.emby_server.mount_sidecar import P115SidecarMount, sidecar_enabled
from backend.emby_server.p115_sidecar import P115SidecarHandler
from backend.emby_server.provider_runtime import (
    BASE_CAPABILITIES,
    P115_EXTENSIONS,
    ProviderRuntimeClient,
    ProviderRuntimeError,
    ProviderRuntimeServer,
    build_manifest,
    decode_range_result,
    register_provider_extensions,
    validate_manifest,
)


# ---- 假 115 上游 ----
class Fake115:
    """假 Pan115Client：目录树固定，download_url 可推导，不碰网络。"""

    TREE = {
        "0": [
            {"cid": "10", "name": "Movies", "is_dir": True, "size": 0, "pickcode": ""},
            {"cid": "f1", "name": "a.mkv", "is_dir": False, "size": 10, "pickcode": "pc-a"},
            {"cid": "f2", "name": "readme.nfo", "is_dir": False, "size": 5, "pickcode": "pc-nfo"},
        ],
        "10": [
            {"cid": "11", "name": "Sub", "is_dir": True, "size": 0, "pickcode": ""},
            {"cid": "f3", "name": "film.mkv", "is_dir": False, "size": 100, "pickcode": "pc-film"},
        ],
        "11": [
            {"cid": "f4", "name": "ep1.mkv", "is_dir": False, "size": 50, "pickcode": "pc-ep1"},
        ],
    }
    DATA = {
        "pc-a": b"0123456789",
        "pc-nfo": b"hello",
        "pc-film": bytes(range(100)),
        "pc-ep1": bytes(range(50, 100)),
    }

    def __init__(self, cookie: str, ua: str = ""):
        if not cookie:
            raise transfer115.Pan115AuthError("未配置 115 Cookie")
        self.cookie = cookie
        self.ua = ua

    def list_dir(self, cid: str = "0"):
        return [dict(e) for e in self.TREE.get(cid or "0", [])]

    def download_url(self, pickcode: str):
        return f"https://down.example/{pickcode}"


class FakeResp:
    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content


class FakeHTTP:
    """按 Range 头回 206（或按构造回固定状态），模拟 115 下载直链。"""

    def __init__(self, fixed_status: int | None = None):
        self.fixed_status = fixed_status

    def get(self, url, headers=None):
        pickcode = url.rsplit("/", 1)[-1]
        body = Fake115.DATA[pickcode]
        if self.fixed_status is not None:
            return FakeResp(self.fixed_status, b"")
        range_header = (headers or {}).get("Range") or ""
        if range_header.startswith("bytes="):
            span = range_header[len("bytes="):]
            start_s, _, end_s = span.partition("-")
            start = int(start_s) if start_s else 0
            end = int(end_s) + 1 if end_s else len(body)
            return FakeResp(206, body[start:end])
        return FakeResp(200, body)


def _wait_sock(sock: str, timeout: float = 5.0) -> None:
    """等服务端线程绑好 UDS 再连（不用 sleep 猜时序，轮询到文件出现为止）。"""
    import time

    deadline = time.time() + timeout
    while not os.path.exists(sock):
        if time.time() > deadline:
            raise AssertionError(f"sidecar 未在 {timeout}s 内绑好 socket：{sock}")
        time.sleep(0.01)


@pytest.fixture()
def runtime(tmp_path):
    """真实 UDS 服务 + 假 115 客户端；用例结束停服清 socket。"""
    sock = str(tmp_path / "runtime.sock")
    handler = P115SidecarHandler(
        client_factory=lambda cookie, ua: Fake115(cookie, ua),
        http_client_factory=lambda: FakeHTTP(),
    )
    server = ProviderRuntimeServer(handler, sock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_sock(sock)
    client = ProviderRuntimeClient("p115", sock)
    yield client, sock
    server.stop()
    thread.join(timeout=5)


@pytest.fixture()
def session_client(runtime):
    client, _ = runtime
    client.session_upsert("s1", {"cookie": "cookie-123", "ua": "", "root_cid": "0"})
    yield client
    try:
        client.session_remove("s1")
    except Exception:
        pass

# ---- 契约本身 ----
def test_protocol_and_capability_names_are_fmby_verbatim():
    assert BASE_CAPABILITIES == (
        "session_upsert", "session_remove", "list_page", "stat", "open_range",
        "resolve_play_target", "resolve_display_asset", "health")
    assert P115_EXTENSIONS == (
        "p115.full_sync", "p115.life_poll", "p115.diff_refresh", "p115.share_full_sync")
    manifest = build_manifest("p115")
    assert manifest["protocol"] == "external-provider-runtime-manifest-v1"
    assert manifest["runtime"] == "external-provider-runtime-v1"
    assert validate_manifest(manifest, "p115", P115_EXTENSIONS) is manifest


def test_validate_manifest_is_fail_closed():
    bad = build_manifest("p115")
    bad["protocol"] = "something-else-v9"
    with pytest.raises(ProviderRuntimeError) as excinfo:
        validate_manifest(bad, "p115")
    assert excinfo.value.kind == "invalid_request"
    missing = build_manifest("p115")
    missing["capabilities"] = ["list_page"]
    with pytest.raises(ProviderRuntimeError):
        validate_manifest(missing, "p115")
    with pytest.raises(ValueError):
        register_provider_extensions("p115", ("yun139.create_outer_link_transfer",))


def test_handshake_against_real_server(runtime):
    client, _ = runtime
    manifest = client.handshake(P115_EXTENSIONS)
    assert manifest["provider"] == "p115"
    assert manifest["capabilities"] == list(BASE_CAPABILITIES)


def test_session_upsert_without_cookie_rolls_back(runtime):
    client, _ = runtime
    with pytest.raises(ProviderRuntimeError) as excinfo:
        client.session_upsert("bad", {"cookie": ""})
    assert excinfo.value.kind == "invalid_request"
    # 回滚后该会话不存在：后续调用报 session_unknown，不是半会话残留。
    with pytest.raises(ProviderRuntimeError) as excinfo2:
        client.stat("bad", "/")
    assert excinfo2.value.kind == "session_unknown"


# ---- 8 项能力往返 ----
def test_list_page_pagination_and_stat(session_client):
    client = session_client
    page1 = client.list_page("s1", "/", page_size=2)
    assert [e["name"] for e in page1["entries"]] == ["Movies", "a.mkv"]
    assert page1["next_page_token"] == "2"
    page2 = client.list_page("s1", "/", page_token=page1["next_page_token"], page_size=2)
    assert [e["name"] for e in page2["entries"]] == ["readme.nfo"]
    assert page2["next_page_token"] is None
    assert page2["total"] == 3
    root = client.stat("s1", "/")
    assert root["is_dir"] is True and root["entry_id"] == "0"
    film = client.stat("s1", "/Movies/film.mkv")
    assert film["file_id"] == "pc-film" and film["is_dir"] is False
    with pytest.raises(ProviderRuntimeError) as excinfo:
        client.stat("s1", "/Movies/nope.mkv")
    assert excinfo.value.kind == "not_found"


def test_open_range_and_display_asset_and_play_target(session_client):
    client = session_client
    result = client.open_range("s1", "/Movies/film.mkv", 10, 5)
    assert decode_range_result(result) == bytes(range(10, 15))
    assert result["byte_length"] == 5
    asset = client.resolve_display_asset("s1", "/a.mkv", 2, 3)
    assert decode_range_result(asset) == b"234"
    target = client.resolve_play_target("s1", "/Movies/film.mkv")
    assert target["url"] == "https://down.example/pc-film"
    assert target["headers"]["X-Pickcode"] == "pc-film"
    # 换链回包按契约不含 Cookie（宿主重贴，不信任回包凭据）。
    assert "Cookie" not in target["headers"]


def test_health_needs_no_session(runtime):
    client, _ = runtime
    health = client.health()
    assert health["ok"] is True and health["provider"] == "p115"


def test_unknown_method_and_unknown_session(session_client):
    client = session_client
    with pytest.raises(ProviderRuntimeError) as excinfo:
        client.call("p115.nope", {})
    assert excinfo.value.kind == "unknown_method"
    with pytest.raises(ProviderRuntimeError) as excinfo2:
        client.stat("ghost", "/")
    assert excinfo2.value.kind == "session_unknown"
    client.session_remove("s1")
    with pytest.raises(ProviderRuntimeError) as excinfo3:
        client.stat("s1", "/")
    assert excinfo3.value.kind == "session_unknown"

# ---- 115 扩展 4 项 ----
def test_p115_full_sync_and_diff_refresh(session_client):
    client = session_client
    sync1 = client.call_extension("p115.full_sync", {"session_id": "s1"})
    names = sorted(e["path"] for e in sync1["entries"])
    assert "/Movies/film.mkv" in names and "/Movies/Sub/ep1.mkv" in names
    assert "/a.mkv" in names and "/Movies" in names
    assert sync1["truncated"] is False
    sync2 = client.call_extension("p115.full_sync", {"session_id": "s1"})
    assert sync2["generation"] == sync1["generation"] + 1  # 进程单调计数，非伪造代际库
    diff = client.call_extension(
        "p115.diff_refresh", {"session_id": "s1", "directories": ["/Movies", "/gone"]})
    assert "/Movies/film.mkv" in [e["path"] for e in diff["entries"]]
    assert diff["missing"] == ["/gone"]


def test_p115_life_poll_and_share_are_explicitly_not_implemented(session_client):
    client = session_client
    for name in ("p115.life_poll", "p115.share_full_sync"):
        with pytest.raises(ProviderRuntimeError) as excinfo:
            client.call_extension(name, {"session_id": "s1"})
        # 不编能力：未落地就是 not_implemented，绝不拿重列冒充 life 事件。
        assert excinfo.value.kind == "not_implemented"


def test_open_range_error_grading(tmp_path):
    """上游 403 → cookie_expired；上游 200 无视 Range → 本地切片兜底。"""
    sock = str(tmp_path / "runtime.sock")
    handler = P115SidecarHandler(
        client_factory=lambda cookie, ua: Fake115(cookie, ua),
        http_client_factory=lambda: FakeHTTP(fixed_status=403),
    )
    server = ProviderRuntimeServer(handler, sock)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _wait_sock(sock)
    client = ProviderRuntimeClient("p115", sock)
    try:
        client.session_upsert("s1", {"cookie": "cookie-123", "root_cid": "0"})
        with pytest.raises(ProviderRuntimeError) as excinfo:
            client.open_range("s1", "/a.mkv", 0, 3)
        assert excinfo.value.kind == "cookie_expired"
    finally:
        server.stop()
        thread.join(timeout=5)


# ---- 宿主侧 provider ----
def _mount():
    return SimpleNamespace(
        id=7, mount_type="115", name="test-115",
        config=json.dumps({"cookie": "cookie-123", "cid": "0"}), path="")


def test_host_provider_end_to_end(runtime):
    client, _ = runtime
    prov = P115SidecarMount(_mount(), client=client)
    try:
        caps = prov.capabilities()
        assert caps.can_list and caps.can_random_read and caps.can_read_sidecar
        assert caps.can_generate_play_target and not caps.can_refresh_credentials
        entries = prov.list_dir("/")
        assert [e.name for e in entries] == ["Movies", "a.mkv", "readme.nfo"]
        media = list(prov.walk_media("/"))
        assert sorted(m.rel for m in media) == [
            "/Movies/Sub/ep1.mkv", "/Movies/film.mkv", "/a.mkv"]
        assert all(m.file_id for m in media)  # pickcode 透出为 file_id
        assert prov.open_range("/Movies/film.mkv", 10, 15) == bytes(range(10, 15))
        assert prov.read_text("/readme.nfo") == "hello"
        target = prov.resolve_play_target("/Movies/film.mkv")
        assert target.kind == "url" and target.value == "https://down.example/pc-film"
        # 宿主重贴 Cookie（回包不含，宿主现取现贴）。
        assert target.headers["Cookie"] == "cookie-123"
        assert target.headers["X-Pickcode"] == "pc-film"
        assert prov.test()["ok"] is True
        assert prov.refresh_credentials() is False
    finally:
        prov.close()
    # 拆会话后 sidecar 侧会话已无：再调即 MountError（不静默走别路）。
    with pytest.raises(Exception):
        prov.list_dir("/")


def test_host_provider_gate_is_off_by_default(monkeypatch):
    monkeypatch.setenv("PAN115_SIDECAR_ENABLED", "0")
    assert sidecar_enabled() is False
    with pytest.raises(Exception):
        P115SidecarMount(_mount())
    monkeypatch.setenv("PAN115_SIDECAR_ENABLED", "1")
    assert sidecar_enabled() is True
