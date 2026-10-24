# -*- coding: utf-8 -*-
"""FMBY 批一 #1：播放解析结果 TTL 缓存（``resolve_play_target`` 层）。

定稿 ``~/workspace/reports/fmby-b1-item1-resolve-cache-plan-20261024.md``，
本文件逐条落 §7 的测试钉：

1. 命中钉：同 (mount, rel) 两次解析，provider 的 resolve 只被调一次，
   第二次结果 kind/value 与第一次一致。
2. 凭据现挂钉（本项最重要的一根）：115 Cookie / Drive Bearer 在两次解析
   之间轮换，第二次返回的凭据头必须是**新值**；且缓存值里根本没有凭据头
   ——证明凭据没进缓存（防 2026-10-11 全局 token 缓存架空 SA 轮询事故重演）。
3. TTL 钉：过期后重新解析；TTL=0 时恒不缓存。
4. 失效钉：模拟中继上游 404 后，解析缓存与 115 换链缓存**同时**被清
   （streaming.py 的连带动作）。
5. 停用挂载钉：缓存热时停用挂载，再解析必须抛 MountError（缓存不许
   短路 is_enabled 检查）。
6. 配置指纹钉：同 mount 改 config 后旧条目不命中。
7. 删除失效钉：``invalidate_resolve_cache_for_path`` 与 scanner 的调用点
   （``_evict_gone_drive_files``）都能清掉对应条目。
8. 隔离钉：新缓存是模块级全局状态，autouse fixture 每例 reset
   （照 #1203 熔断 ``breaker.reset_all()`` 先例）。

另加固：默认提供者（未覆写拆分钩子）只要返回凭据头就**不缓存**（安全
默认）；kind="local" 不缓存；STRM 目标的 Bearer 命中后现挂。

全部离线：db 用桩、115/Drive 的客户端方法桩掉、网络只连 127.0.0.1 测试源站。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.emby_server import api as ea_api
from backend.emby_server import drive_auth
from backend.emby_server import models as em
from backend.emby_server import mount_drive
from backend.emby_server import mounts as mnt
from backend.emby_server import scanner
from backend.emby_server import streaming

PC = "pc-nail"          # 115 测试 pickcode
FID = "FIDNAIL123"      # Drive 测试 file_id


@pytest.fixture(autouse=True)
def _isolate_resolve_cache():
    """测试钉 8：模块级全局状态每例 reset，前后都清。"""
    mnt.reset_resolve_cache()
    mnt._invalidate_115_url(PC)
    yield
    mnt.reset_resolve_cache()
    mnt._invalidate_115_url(PC)


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    """沙箱代理环境变量会让 httpx 建 Client 时报错，与被测逻辑无关。"""
    for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
              "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(k, raising=False)


# ---------------- 桩 ----------------

class _FakeQuery:
    def __init__(self, mounts):
        self._mounts = mounts

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._mounts[0] if self._mounts else None


class _FakeDB:
    """只够 load_mount 用：query(...).filter(...).first() 返回挂载行。"""

    def __init__(self, mounts):
        self._mounts = mounts

    def query(self, *args, **kwargs):
        return _FakeQuery(self._mounts)


def _mount(mount_id=7, mount_type="pan115", config=None, enabled=True, path=""):
    m = em.StorageMount(name=f"测试挂载{mount_id}", mount_type=mount_type,
                        path=path, config=mnt.dump_config(config or {}),
                        is_enabled=enabled)
    m.id = mount_id
    return m


class _CountingProvider(mnt.MountProvider):
    """计数型假提供者：resolve 调一次计数 +1，返回可配的 PlayTarget。"""

    calls = 0
    headers = {"User-Agent": "counting-ua"}
    url = "https://example.com/v/a.mkv"
    kind_value = "url"

    def resolve(self, rel):
        type(self).calls += 1
        return mnt.PlayTarget(self.kind_value, self.url, dict(self.headers))


@pytest.fixture()
def counting(monkeypatch):
    _CountingProvider.calls = 0
    _CountingProvider.headers = {"User-Agent": "counting-ua"}
    _CountingProvider.url = "https://example.com/v/a.mkv"
    _CountingProvider.kind_value = "url"
    monkeypatch.setattr(
        mnt, "build_provider",
        lambda mount, db=None, library=None: _CountingProvider(mount, db, library))
    return _CountingProvider


# ---------------- 测试钉 1：命中 ----------------

def test_second_resolve_hits_cache(counting):
    mount = _mount()
    db = _FakeDB([mount])
    t1 = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    t2 = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert counting.calls == 1, "第二次解析必须命中缓存，不许再调 provider.resolve"
    assert (t2.kind, t2.value) == (t1.kind, t1.value) == ("url", counting.url)
    assert mnt.resolve_cache_stats()["hits"] == 1


def test_local_kind_not_cached(counting):
    """kind=local 解析就是路径校验，没有远程成本，不许进缓存。"""
    counting.kind_value = "local"
    counting.url = "/data/a.mkv"
    mount = _mount()
    db = _FakeDB([mount])
    mnt.resolve_play_target("mount://7/a.mkv", db)
    mnt.resolve_play_target("mount://7/a.mkv", db)
    assert counting.calls == 2
    assert mnt.resolve_cache_stats()["entries"] == 0


def test_default_provider_with_credential_headers_is_not_cached(counting):
    """安全默认：未覆写拆分钩子的提供者只要返回凭据头，就不许缓存。"""
    counting.headers = {"User-Agent": "ua", "Cookie": "c=1"}
    mount = _mount()
    db = _FakeDB([mount])
    mnt.resolve_play_target("mount://7/a.mkv", db)
    mnt.resolve_play_target("mount://7/a.mkv", db)
    assert counting.calls == 2, "含凭据头的默认提供者结果不许进缓存"
    assert mnt.resolve_cache_stats()["entries"] == 0


# ---------------- 测试钉 3：TTL ----------------

def test_ttl_zero_disables_cache(counting, monkeypatch):
    monkeypatch.setattr(mnt, "RESOLVE_TARGET_CACHE_TTL_SEC", 0)
    mount = _mount()
    db = _FakeDB([mount])
    mnt.resolve_play_target("mount://7/a.mkv", db)
    mnt.resolve_play_target("mount://7/a.mkv", db)
    assert counting.calls == 2, "TTL=0 时恒不缓存，每次都要真解析"
    assert mnt.resolve_cache_stats()["entries"] == 0


def test_expired_entry_is_resolved_again(counting):
    mount = _mount()
    db = _FakeDB([mount])
    mnt.resolve_play_target("mount://7/a.mkv", db)
    assert counting.calls == 1
    # 把缓存条目的到期时刻拨到过去（等价于 TTL 已过）
    with mnt._RESOLVE_CACHE_LOCK:
        for key in list(mnt._RESOLVE_CACHE):
            _exp, target = mnt._RESOLVE_CACHE[key]
            mnt._RESOLVE_CACHE[key] = (time.monotonic() - 1, target)
    mnt.resolve_play_target("mount://7/a.mkv", db)
    assert counting.calls == 2, "过期后必须重新解析"


# ---------------- 测试钉 5：停用挂载 ----------------

def test_disabled_mount_not_short_circuited_by_cache(counting):
    mount = _mount()
    db = _FakeDB([mount])
    mnt.resolve_play_target("mount://7/a.mkv", db)   # 先把缓存喂热
    assert counting.calls == 1
    mount.is_enabled = False
    with pytest.raises(mnt.MountError):
        mnt.resolve_play_target("mount://7/a.mkv", db)
    assert counting.calls == 1, "停用挂载必须在查缓存之前就抛错"


# ---------------- 测试钉 6：配置指纹 ----------------

def test_config_change_misses_old_entry(counting):
    mount = _mount(config={"cid": "0"})
    db = _FakeDB([mount])
    mnt.resolve_play_target("mount://7/a.mkv", db)
    assert counting.calls == 1
    mount.config = mnt.dump_config({"cid": "99"})   # 换配置 → 指纹变
    mnt.resolve_play_target("mount://7/a.mkv", db)
    assert counting.calls == 2, "配置变了不许命中旧条目"


# ---------------- 测试钉 2：凭据现挂（115） ----------------

@pytest.fixture()
def pan115(monkeypatch):
    """真 Pan115Mount + 桩 _locate/_client/_cookie/_ua；cookie 可中途轮换。"""
    holder = {"cookie": "cookie-1", "locate": 0, "download": 0}

    def _locate(self, rel):
        holder["locate"] += 1
        return {"pickcode": PC, "name": "a.mkv", "size": 3}

    def _client(self):
        def download_url(pickcode):
            holder["download"] += 1
            return f"https://115.example.com/dl/{pickcode}"
        return SimpleNamespace(download_url=download_url)

    monkeypatch.setattr(mnt.Pan115Mount, "_locate", _locate)
    monkeypatch.setattr(mnt.Pan115Mount, "_client", _client)
    monkeypatch.setattr(mnt.Pan115Mount, "_cookie", lambda self: holder["cookie"])
    monkeypatch.setattr(mnt.Pan115Mount, "_ua", lambda self: "ua-115")
    return holder


def test_115_cookie_is_reattached_fresh_never_cached(pan115):
    mount = _mount(mount_type="115", config={"cid": "0"})
    db = _FakeDB([mount])
    t1 = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert t1.headers["Cookie"] == "cookie-1"
    assert t1.headers["X-Pickcode"] == PC

    pan115["cookie"] = "cookie-2"     # 账号侧轮换 Cookie
    t2 = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert pan115["locate"] == 1, "第二次应命中解析缓存，不再步行定位"
    assert pan115["download"] == 1, "换链也只许发生一次"
    assert t2.headers["Cookie"] == "cookie-2", \
        "命中后现挂的必须是新 Cookie——凭据绝不许钉在缓存里"

    # 缓存值本身：一份凭据都不许有；X-Pickcode（失效钥匙）必须保留
    with mnt._RESOLVE_CACHE_LOCK:
        stored = [row[1] for row in mnt._RESOLVE_CACHE.values()]
    assert len(stored) == 1
    assert "Cookie" not in stored[0].headers
    assert stored[0].headers["X-Pickcode"] == PC


# ---------------- 测试钉 2：凭据现挂（Drive） ----------------

@pytest.fixture()
def drive(monkeypatch):
    """真 DriveMount + 桩 _locate 与 drive_auth_headers；token 可轮换。"""
    holder = {"token": "token-1", "locate": 0, "affinities": []}

    def _locate(self, rel):
        holder["locate"] += 1
        return SimpleNamespace(file_id=FID)

    def _headers(url, affinity_key=None):
        holder["affinities"].append(affinity_key)
        return {"Authorization": f"Bearer {holder['token']}"}

    monkeypatch.setattr(mount_drive.DriveMount, "_locate", _locate)
    monkeypatch.setattr(drive_auth, "drive_auth_headers", _headers)
    return holder


def test_drive_bearer_is_reattached_fresh_never_cached(drive):
    mount = _mount(mount_type="drive", path="drive:SHAREDID", config={})
    db = _FakeDB([mount])
    t1 = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert t1.headers["Authorization"] == "Bearer token-1"

    drive["token"] = "token-2"        # SA 侧换了新 token
    t2 = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert drive["locate"] == 1, "第二次应命中解析缓存，不再步行定位"
    assert t2.headers["Authorization"] == "Bearer token-2", \
        "命中后现挂的必须是新 token——不许把某个 SA 的 token 钉进缓存"
    # 两次取头的亲和键都必须是 file_id（现挂时从直链 id 参数还原）
    assert drive["affinities"] == [FID, FID]

    with mnt._RESOLVE_CACHE_LOCK:
        stored = [row[1] for row in mnt._RESOLVE_CACHE.values()]
    assert len(stored) == 1
    assert "Authorization" not in stored[0].headers


# ---------------- STRM 目标：Bearer 现挂 ----------------

class _StrmProvider(mnt.MountProvider):
    """read_text 给一条 Drive 直链内容；resolve 对 .strm 根本不该被调。"""

    reads = 0

    def read_text(self, rel):
        type(self).reads += 1
        return "https://drive.google.com/uc?id=STRMFID&export=download\n"

    def resolve(self, rel):  # pragma: no cover - 被调到就是错
        raise AssertionError("STRM 走 resolve_final 的内容解析，不许调 resolve")


def test_strm_bearer_reattached_on_hit(monkeypatch):
    _StrmProvider.reads = 0
    holder = {"token": "strm-token-1"}
    monkeypatch.setattr(
        drive_auth, "drive_auth_headers",
        lambda url, affinity_key=None: {"Authorization": f"Bearer {holder['token']}"})
    monkeypatch.setattr(
        mnt, "build_provider",
        lambda mount, db=None, library=None: _StrmProvider(mount, db, library))
    mount = _mount(mount_type="drive", path="drive:X", config={})
    db = _FakeDB([mount])

    t1 = mnt.resolve_play_target("mount://7/x.strm", db)
    assert t1.headers["Authorization"] == "Bearer strm-token-1"
    holder["token"] = "strm-token-2"
    t2 = mnt.resolve_play_target("mount://7/x.strm", db)
    assert _StrmProvider.reads == 1, "第二次应命中缓存，不再读 STRM 内容"
    assert t2.headers["Authorization"] == "Bearer strm-token-2"
    with mnt._RESOLVE_CACHE_LOCK:
        stored = [row[1] for row in mnt._RESOLVE_CACHE.values()]
    assert "Authorization" not in stored[0].headers


# ---------------- 测试钉 4：中继 404 连带失效 ----------------

class _NotFoundHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "4")
        self.end_headers()
        self.wfile.write(b"nope")

    def log_message(self, *args):
        pass


@pytest.fixture()
def notfound_upstream():
    srv = HTTPServer(("127.0.0.1", 0), _NotFoundHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()
    srv.server_close()


def _relay_request():
    scope = {"type": "http", "method": "GET", "path": "/Videos/item/stream",
             "raw_path": b"/Videos/item/stream", "query_string": b"",
             "headers": [], "scheme": "http", "server": ("testserver", 80),
             "client": ("testclient", 50000), "root_path": ""}
    return Request(scope)


def test_relay_404_clears_resolve_and_115_url_caches(pan115, notfound_upstream):
    mount = _mount(mount_type="115", config={"cid": "0"})
    db = _FakeDB([mount])
    target = mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    # 两份缓存都已喂热：解析缓存 1 条 + 115 换链缓存有值
    assert mnt.resolve_cache_stats()["entries"] == 1
    assert mnt._get_cached_115_url(PC) is not None

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(streaming.serve_mount_relay_async(
            notfound_upstream + "/dl/" + PC, _relay_request(),
            {"User-Agent": "ua-115", "Cookie": "cookie-1", "X-Pickcode": PC},
            "video/mp4"))
    assert exc_info.value.status_code == 404
    assert mnt._get_cached_115_url(PC) is None, "115 换链缓存必须被清"
    assert mnt.resolve_cache_stats()["entries"] == 0, \
        "解析缓存必须连带被清——否则死链会被它续命一个 TTL"


# ---------------- 测试钉 7：删除失效 ----------------

def test_invalidate_for_path_drops_single_entry(counting):
    mount = _mount()
    db = _FakeDB([mount])
    mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    mnt.resolve_play_target("mount://7/电影/b.mkv", db)
    assert mnt.resolve_cache_stats()["entries"] == 2
    mnt.invalidate_resolve_cache_for_path("mount://7/电影/a.mkv")
    assert mnt.resolve_cache_stats()["entries"] == 1
    mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert counting.calls == 3, "被清的那条必须重新解析，没清的不许误伤"
    # 非挂载路径 / 空路径：不抛、不误清
    mnt.invalidate_resolve_cache_for_path("/local/x.mkv")
    mnt.invalidate_resolve_cache_for_path("")
    mnt.invalidate_resolve_cache_for_path(None)


def test_scanner_evict_call_point_also_clears_resolve_cache(counting, monkeypatch):
    """scanner 确认文件丢失的调用点：存在性失效与解析失效同点位连带。"""
    monkeypatch.setattr(ea_api, "invalidate_drive_exists_for_item",
                        lambda db, item: None)
    mount = _mount()
    db = _FakeDB([mount])
    mnt.resolve_play_target("mount://7/电影/a.mkv", db)
    assert mnt.resolve_cache_stats()["entries"] == 1
    scanner._evict_gone_drive_files(
        db, SimpleNamespace(id=1), [("mount://7/电影/a.mkv", "fid-gone")])
    assert mnt.resolve_cache_stats()["entries"] == 0
