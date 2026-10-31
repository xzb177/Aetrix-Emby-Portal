# -*- coding: utf-8 -*-
"""360° 审计 A-092（P2）：115 分片续传 ListParts 无翻页。

缺陷：_oss_list_parts 只发一次 uploadId 查询，不带
part-number-marker、不读 IsTruncated/NextPartNumberMarker；
OSS ListParts 单页默认上限 1000 片，>1000 片（约 >9.8 GiB）
的文件续传时，持久化片清单（2048 片）与服务端当页
（1000 片）恒不等 → _multipart_resume 判 _ResumeStale →
multipart_upload abort 旧会话重走全新上传，已传片白传、
重试三遍仍失败。

修法：_oss_list_parts 按 part-number-marker 翻页取全量，
比对全量后再判一致；真正不一致时仍判 stale（防修过头）。

全部离线：ListParts HTTP、上传、complete 一律桩替身；
不碰生产凭据、不发真实上游请求。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import io
from urllib.parse import parse_qsl, urlsplit

import pytest

from backend.emby_server import transfer115

PARTSIZE = 10 * 1024 * 1024


def _listparts_xml(numbers, truncated, next_marker=None):
    parts = "".join(
        "<Part><PartNumber>%d</PartNumber>"
        "<ETag>\"ETAG%05d\"</ETag></Part>" % (n, n)
        for n in numbers)
    marker = ("" if next_marker is None else
              "<NextPartNumberMarker>%d</NextPartNumberMarker>" % next_marker)
    return ("<ListPartsResult><IsTruncated>%s</IsTruncated>%s%s"
            "</ListPartsResult>"
            % ("true" if truncated else "false", marker, parts)).encode()


def _client():
    return transfer115.Pan115Client("COOKIE")


def _patch_listparts(monkeypatch, pages):
    """pages：marker（None 表首页）-> XML；记录每次请求的 query。"""
    requests = []

    def _fake(method, url, headers=None, **kw):
        query = dict(parse_qsl(urlsplit(url).query))
        requests.append(query)
        marker = query.get("part-number-marker")
        key = None if marker is None else int(marker)
        assert key in pages, "未预期 ListParts 翻页 marker：%r" % (marker,)
        return 200, {}, pages[key]

    monkeypatch.setattr(transfer115, "_http_full", _fake)
    return requests


def _saved(n):
    return [{"PartNumber": i + 1, "ETag": "ETAG%05d" % (i + 1)}
            for i in range(n)]


def _resume_session(saved):
    return {"bucket": "b", "object_key": "o", "upload_id": "u",
            "callback": {"callback": "http://example/cb"},
            "parts": [dict(p) for p in saved]}


def _stub_resume_client(monkeypatch, client, uploaded):
    client.upload_token = lambda: {"AccessKeyId": "AK",
                                   "AccessKeySecret": "SK",
                                   "SecurityToken": "ST"}

    def _upload(token, bucket, object_key, upload_id, fileobj, filesize,
                start_number=1, start_offset=0, on_part=None,
                refresher=None):
        uploaded.append((start_number, start_offset))
        return []

    client._oss_upload_parts = _upload
    client._oss_multipart_complete = lambda *a, **k: {
        "data": {"file_id": "f1", "pick_code": "pc1",
                 "sha1": "A" * 40}}


def test_listparts_paginates_until_not_truncated(monkeypatch):
    client = _client()
    pages = {None: _listparts_xml(range(1, 1001), True, 1000),
             1000: _listparts_xml(range(1001, 2049), False)}
    requests = _patch_listparts(monkeypatch, pages)
    parts = client._oss_list_parts({"AccessKeyId": "AK",
                                    "AccessKeySecret": "SK"}, "b", "o", "u")
    assert [p["PartNumber"] for p in parts] == list(range(1, 2049))
    assert len(requests) == 2
    assert requests[1].get("part-number-marker") == "1000"
    assert "part-number-marker" not in requests[0]


def test_listparts_single_page_unchanged(monkeypatch):
    client = _client()
    pages = {None: _listparts_xml(range(1, 601), False)}
    requests = _patch_listparts(monkeypatch, pages)
    parts = client._oss_list_parts({"AccessKeyId": "AK",
                                    "AccessKeySecret": "SK"}, "b", "o", "u")
    assert [p["PartNumber"] for p in parts] == list(range(1, 601))
    assert len(requests) == 1


def test_listparts_truncated_without_marker_stops(monkeypatch):
    """服务端称 truncated 却不给 marker：只取已有页，不死循环。"""
    client = _client()
    pages = {None: _listparts_xml(range(1, 1001), True, None)}
    requests = _patch_listparts(monkeypatch, pages)
    parts = client._oss_list_parts({"AccessKeyId": "AK",
                                    "AccessKeySecret": "SK"}, "b", "o", "u")
    assert len(parts) == 1000
    assert len(requests) == 1


def test_resume_paginated_server_list_uploads_remaining(monkeypatch):
    client = _client()
    pages = {None: _listparts_xml(range(1, 1001), True, 1000),
             1000: _listparts_xml(range(1001, 2049), False)}
    _patch_listparts(monkeypatch, pages)
    uploaded = []
    _stub_resume_client(monkeypatch, client, uploaded)
    filesize = 2049 * PARTSIZE  # 2048 片已成、剩 1 片待传
    out = client._multipart_resume(io.BytesIO(b""), filesize, "A" * 40,
                                   _resume_session(_saved(2048)), None)
    assert out["outcome"] == "uploaded"
    assert uploaded == [(2049, 2048 * PARTSIZE)]


def test_resume_true_mismatch_still_stale(monkeypatch):
    """防修过头：服务端全量与持久化真不一致时仍判 stale。"""
    client = _client()
    xml = _listparts_xml(range(1, 2049), False)
    # 改掉其中一片的 ETag，制造真不一致
    xml = xml.replace(b"ETAG01000", b"ETAG99999")
    pages = {None: xml}
    _patch_listparts(monkeypatch, pages)
    uploaded = []
    _stub_resume_client(monkeypatch, client, uploaded)
    with pytest.raises(transfer115._ResumeStale):
        client._multipart_resume(io.BytesIO(b""), 2049 * PARTSIZE, "A" * 40,
                                 _resume_session(_saved(2048)), None)
    assert uploaded == []


def test_multipart_upload_resume_paginated_does_not_abort(monkeypatch):
    client = _client()
    pages = {None: _listparts_xml(range(1, 1001), True, 1000),
             1000: _listparts_xml(range(1001, 2049), False)}
    _patch_listparts(monkeypatch, pages)
    uploaded = []
    _stub_resume_client(monkeypatch, client, uploaded)
    aborted = []
    client.abort_multipart_session = lambda s: aborted.append(s)
    client._upload_init = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("不应走到全新上传"))
    out = client.multipart_upload(io.BytesIO(b""), "a.mkv",
                                  2049 * PARTSIZE, "A" * 40, "900",
                                  resume_session=_resume_session(
                                      _saved(2048)))
    assert out["outcome"] == "uploaded"
    assert aborted == []
    assert uploaded == [(2049, 2048 * PARTSIZE)]
