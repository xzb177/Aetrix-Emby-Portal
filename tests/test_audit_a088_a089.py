"""360° 审计 R27：A-088（failover 先记录后冷却）与 A-089（带 Range 的 Quota 嗅探）。

A-088：serve_drive_relay_async 的 failover 两分支（额度页 /
HTTPException）旧顺序是先 _note_relay_sa_cooldown（→ invalidate，
内外缓存同值条目被同时清掉）后 _remember_tried（内层反查恒
None），已试集记不上，下一轮重抽排除失效；日志 sa= 字段同样事后
反查退化为 "-"。修法：先记录再冷却，日志用这次记下的邮箱。
本文件用“失效后反查失效”的状态替身模拟真实 invalidate 的副作用
（内外缓存同值场景），不碰真实凭据与网络。轮换抽回同一 SA 时，
真实行为是同一邮箱的新令牌值（旧值已被失效），故 token 序列用
TOK1 → TOK1B（同邮箱）→ TOK2 建模。

A-089：serve_mount_relay_async 的 Quota 嗅探门旧口径要求无 Range；
带 Range 的请求撞上 200 HTML 额度页时 HTML 原样当视频流下发。
修法：去掉 Range 限制，200+HTML 统一抛 QuotaPageError；200+Range+
非 HTML 经 prelude 原样续流；206 本就不满足 status==200，不受影响。
另覆盖第二种上游形态：Range 请求撞 403 时 HTTPException failover
分支照常换号（该分支本就不分 Range）。

monkeypatch 只打类/模块属性，不 patch 单例实例属性。
"""
import asyncio
import logging
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.emby_server import drive_auth
from backend.emby_server import drive_changes as _dc
from backend.emby_server import streaming
from backend.emby_server import transfer_variant as _tv


def _run(coro):
    return asyncio.run(coro)


# ---------------- A-088：failover 顺序 ----------------

_SA_EMAIL = {
    "TOK1": "sa-01@example.test",
    "TOK1B": "sa-01@example.test",
    "TOK2": "sa-02@example.test",
    "TOK3": "sa-03@example.test",
}


class _TokenRig:
    """SA 轮换与失效替身：失效后旧值反查失效，新值仍可反查。"""

    def __init__(self, monkeypatch, tokens=None):
        self.invalidated = []
        self.noted = []
        self._dead = set()
        self._tokens = list(tokens) if tokens else [
            "TOK1", "TOK1B", "TOK2", "TOK3"]
        self._calls = 0

        def fake_token(drive_id, force_refresh=False):
            idx = min(self._calls, len(self._tokens) - 1)
            self._calls += 1
            return self._tokens[idx]

        def fake_invalidate(token):
            self.invalidated.append(token)
            self._dead.add(token)
            return True

        def fake_note(url, header):
            self.noted.append((url, header))
            parts = header.split(None, 1)
            if len(parts) == 2:
                self._dead.add(parts[1])
            return True

        def email_for(token):
            if token in self._dead:
                return None
            return _SA_EMAIL.get(token)

        monkeypatch.setattr(_dc, "get_token_for_drive", fake_token)
        monkeypatch.setattr(drive_auth, "invalidate_drive_bearer_token",
                            fake_invalidate)
        monkeypatch.setattr(drive_auth, "note_drive_unauthorized", fake_note)
        monkeypatch.setattr(_dc, "sa_email_for_token", email_for)
        monkeypatch.setattr(drive_auth, "sa_email_for_token", email_for)


class _FailoverRig:
    """桩 mount + token 替身：首轮 TOK1，次轮轮换先抽回同 SA。"""

    def __init__(self, monkeypatch, outcomes, tokens=None):
        self.tokens = _TokenRig(monkeypatch, tokens)
        self.outcomes = list(outcomes)
        self.mount_auth = []

        async def fake_mount(url, request, headers=None, media_type=None,
                             pt=None, sniff_quota_page=False):
            self.mount_auth.append((headers or {}).get("Authorization"))
            outcome = self.outcomes.pop(0)
            if outcome == "RESP":
                return "RESP"
            if outcome == "QUOTA":
                raise _tv.QuotaPageError("quota page")
            raise HTTPException(status_code=outcome, detail="up")

        monkeypatch.setattr(streaming, "serve_mount_relay_async", fake_mount)

    def relay(self, **kwargs):
        return _run(streaming.serve_drive_relay_async(
            SimpleNamespace(headers={}), "shared1", "FID123", **kwargs))


@pytest.mark.parametrize("first_outcome", [403, 401, "QUOTA"])
def test_a088_failover_records_tried_before_cooldown(
        monkeypatch, first_outcome, caplog):
    """三分支（403 / 401 / 额度页）均先记录已试 SA 再冷却。

    旧码：冷却清掉缓存后记录得 None，已试集为空，次轮接受抽回的
    同 SA（TOK1B），且日志 sa= 退化为 "-"，两断言皆红。
    新码：记录时 TOK1 仍可反查，已试集非空，次轮排除 TOK1B 改取
    TOK2，日志 sa= 为记下的邮箱。
    """
    rig = _FailoverRig(monkeypatch, [first_outcome, "RESP"])
    with caplog.at_level(logging.WARNING):
        assert rig.relay() == "RESP"
    assert rig.mount_auth == ["Bearer TOK1", "Bearer TOK2"]
    assert "sa=sa-01@example.test" in caplog.text


# ---------------- A-089：带 Range 的 Quota 嗅探 ----------------

class _FakeResp:
    def __init__(self, chunks, status_code=200,
                 content_type="text/html; charset=utf-8"):
        self._chunks = list(chunks)
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.closed = False

    async def aiter_bytes(self, _n):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class _FakeClient:
    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


def _sniff_setup(monkeypatch, resp):
    client = _FakeClient()

    async def _follow(c, url, forward, silent=False):
        return resp, url, 0

    monkeypatch.setattr(streaming, "_follow_redirects", _follow)
    monkeypatch.setattr(streaming, "_mount_relay_client", lambda: client)
    return client


def test_a089_range_quota_page_raises(monkeypatch):
    """带 Range 撞 200 HTML 额度页：抛 QuotaPageError，不再原样下发。"""
    resp = _FakeResp([b"<html>Quota exceeded</html>", b"rest"])
    client = _sniff_setup(monkeypatch, resp)
    request = SimpleNamespace(headers={"range": "bytes=0-"})
    with pytest.raises(_tv.QuotaPageError):
        _run(streaming.serve_mount_relay_async(
            "https://example.com/file", request, sniff_quota_page=True))
    assert resp.closed and not client.closed


def test_a089_range_206_not_sniffed(monkeypatch):
    """带 Range 的正常 206：不嗅探、字节完整（回归护栏）。"""
    resp = _FakeResp([b"0123456789"], status_code=206,
                     content_type="video/mp4")
    _sniff_setup(monkeypatch, resp)
    request = SimpleNamespace(headers={"range": "bytes=0-9"})

    async def _serve_and_collect():
        response = await streaming.serve_mount_relay_async(
            "https://example.com/file", request, sniff_quota_page=True)
        return (response.status_code,
                b"".join([c async for c in response.body_iterator]))

    status, body = _run(_serve_and_collect())
    assert status == 206
    assert body == b"0123456789"


def test_a089_range_200_non_html_streams_full_body(monkeypatch):
    """带 Range 的 200 非 HTML（上游忽略 Range 回全文件）：原样续流，
    嗅探预读的首块一个不少（回归护栏）。"""
    resp = _FakeResp([b"AAAA", b"BBBB"], status_code=200,
                     content_type="video/mp4")
    _sniff_setup(monkeypatch, resp)
    request = SimpleNamespace(headers={"range": "bytes=0-"})

    async def _serve_and_collect():
        response = await streaming.serve_mount_relay_async(
            "https://example.com/file", request, sniff_quota_page=True)
        return b"".join([c async for c in response.body_iterator])

    assert _run(_serve_and_collect()) == b"AAAABBBB"


# ---------------- A-088 × A-089：真实中继链路集成 ----------------

class _UpstreamStub:
    """按次序返回响应；记录每次 forward 的 Authorization 与 Range。"""

    def __init__(self, resps):
        self.resps = list(resps)
        self.calls = []

    async def follow(self, client, url, forward, silent=False):
        self.calls.append(
            (forward.get("Authorization"), forward.get("Range")))
        return self.resps.pop(0), url, 0


def _quota_resp():
    return _FakeResp([b"<html>Quota exceeded</html>"])


def test_a088_a089_range_quota_pages_rotate_then_exhaust(
        monkeypatch, caplog):
    """带 Range 连撞 3 张 200 额度页：逐次换号、预算耗尽抛错。

    旧码：第 1 次嗅探门因 Range 跳过，HTML 当流返回，pytest.raises
    即红；新码：每次嗅探抛错、换号（次轮排除抽回的同 SA），第 3 次
    预算耗尽原样抛 QuotaPageError，调用方走现行信号/入队链。
    """
    _TokenRig(monkeypatch)
    upstream = _UpstreamStub([_quota_resp() for _ in range(3)])
    monkeypatch.setattr(streaming, "_follow_redirects", upstream.follow)
    monkeypatch.setattr(streaming, "_mount_relay_client",
                        lambda: _FakeClient())
    request = SimpleNamespace(headers={"range": "bytes=0-"})
    with caplog.at_level(logging.WARNING):
        with pytest.raises(_tv.QuotaPageError):
            _run(streaming.serve_drive_relay_async(
                request, "shared1", "FID123"))
    assert [c[0] for c in upstream.calls] == [
        "Bearer TOK1", "Bearer TOK2", "Bearer TOK3"]
    assert all(c[1] == "bytes=0-" for c in upstream.calls)
    assert "sa=sa-01@example.test" in caplog.text


def test_a089_range_403_still_fails_over(monkeypatch):
    """第二种上游形态：Range 请求撞 403 时 HTTPException 分支照常换号
    （该分支本就不分 Range；若上游对 Range 改回 403，本条即覆盖）。"""
    _TokenRig(monkeypatch)
    upstream = _UpstreamStub([
        _FakeResp([b"forbidden"], status_code=403,
                  content_type="text/html; charset=utf-8"),
        _FakeResp([b"0123456789"], status_code=206,
                  content_type="video/mp4"),
    ])
    monkeypatch.setattr(streaming, "_follow_redirects", upstream.follow)
    monkeypatch.setattr(streaming, "_mount_relay_client",
                        lambda: _FakeClient())
    request = SimpleNamespace(headers={"range": "bytes=0-"})

    async def _serve_and_collect():
        response = await streaming.serve_drive_relay_async(
            request, "shared1", "FID123")
        return (response.status_code,
                b"".join([c async for c in response.body_iterator]))

    status, body = _run(_serve_and_collect())
    assert status == 206
    assert body == b"0123456789"
    assert [c[0] for c in upstream.calls] == ["Bearer TOK1", "Bearer TOK2"]
    assert all(c[1] == "bytes=0-" for c in upstream.calls)
