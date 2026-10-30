# -*- coding: utf-8 -*-
"""A-016：115 服主挂载直链缓存必须按凭据作用域隔离。

旧 bug：缓存键只有 pickcode，进程全局共享——两服主账号同 pickcode
时，后解析者拿到先解析账号的签名地址。
修法：键为（凭据 SHA256 摘要，pickcode），摘要绝不是 Cookie 明文；
同一凭据的两挂载共用同一签名地址是正确的（URL 只取决于账号＋文件）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import hashlib
from types import SimpleNamespace

import pytest

from backend.emby_server import mounts as mnt

PC = "pc-scope-nail"


@pytest.fixture(autouse=True)
def _clear_115_url_cache():
    with mnt._pan115_url_cache_lock:
        mnt._pan115_url_cache.clear()
    yield
    with mnt._pan115_url_cache_lock:
        mnt._pan115_url_cache.clear()


def _mount_with_cookie(monkeypatch, cookie, calls):
    """构造一个 Cookie 直出的 Pan115Mount，download_url 按凭据出链并计数。"""

    class _FakeClient:
        def __init__(self, cookie_arg, ua=None):
            self._cookie_arg = cookie_arg

        def download_url(self, pickcode):
            calls.append((self._cookie_arg, pickcode))
            return f"https://115.example.com/dl/{self._cookie_arg}/{pickcode}"

    monkeypatch.setattr(mnt.transfer115, "Pan115Client", _FakeClient)
    monkeypatch.setattr(
        mnt.Pan115Mount, "_locate",
        lambda self, rel: {"pickcode": PC, "name": "a.mkv", "size": 3})
    mount = SimpleNamespace(path="115:/", config={})
    return mnt.Pan115Mount(mount, db=None, cookie_override=cookie)


def test_same_pickcode_different_cookies_get_own_urls(monkeypatch):
    calls = []
    mount_a = _mount_with_cookie(monkeypatch, "cookie-A", calls)
    mount_b = _mount_with_cookie(monkeypatch, "cookie-B", calls)
    target_a = mount_a.resolve_play_target("/a.mkv")
    target_b = mount_b.resolve_play_target("/a.mkv")
    assert target_a.value != target_b.value
    assert "cookie-A" in target_a.value
    assert "cookie-B" in target_b.value
    # 各自只换链一次：第二次解析走各自作用域的缓存
    assert mount_a.resolve_play_target("/a.mkv").value == target_a.value
    assert mount_b.resolve_play_target("/a.mkv").value == target_b.value
    assert calls.count(("cookie-A", PC)) == 1
    assert calls.count(("cookie-B", PC)) == 1


def test_cookie_rotation_misses_old_cache(monkeypatch):
    calls = []
    mount_old = _mount_with_cookie(monkeypatch, "cookie-A", calls)
    url_old = mount_old.resolve_play_target("/a.mkv").value
    mount_new = _mount_with_cookie(monkeypatch, "cookie-A2", calls)
    url_new = mount_new.resolve_play_target("/a.mkv").value
    assert url_new != url_old
    assert "cookie-A2" in url_new
    assert calls.count(("cookie-A2", PC)) == 1


def test_invalidate_by_pickcode_purges_all_scopes():
    scope_a = mnt._pan115_cache_scope("cookie-A")
    scope_b = mnt._pan115_cache_scope("cookie-B")
    mnt._set_cached_115_url(PC, "https://115.example.com/a", scope_a)
    mnt._set_cached_115_url(PC, "https://115.example.com/b", scope_b)
    # 中继侧只有 X-Pickcode、不知道账号：不给 scope 必须全量清
    mnt._invalidate_115_url(PC)
    assert mnt._get_cached_115_url(PC, scope_a) is None
    assert mnt._get_cached_115_url(PC, scope_b) is None


def test_invalidate_scoped_purges_only_that_scope():
    scope_a = mnt._pan115_cache_scope("cookie-A")
    scope_b = mnt._pan115_cache_scope("cookie-B")
    mnt._set_cached_115_url(PC, "https://115.example.com/a", scope_a)
    mnt._set_cached_115_url(PC, "https://115.example.com/b", scope_b)
    mnt._invalidate_115_url(PC, scope_a)
    assert mnt._get_cached_115_url(PC, scope_a) is None
    assert mnt._get_cached_115_url(PC, scope_b) == "https://115.example.com/b"


def test_scope_is_digest_not_plaintext():
    scope = mnt._pan115_cache_scope("cookie-A")
    assert "cookie-A" not in scope
    assert scope == hashlib.sha256(b"cookie-A").hexdigest()
    assert scope != mnt._pan115_cache_scope("cookie-B")
