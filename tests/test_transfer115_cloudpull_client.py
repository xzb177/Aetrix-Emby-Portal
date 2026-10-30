# -*- coding: utf-8 -*-
"""Pan115Client 云下载口（形态 B，2026-10-30 生产探针实证的
115 web 形态协议）回归：UID 从 Cookie UID 段取、签名走
ct=clouddownload&ac=space、建单走 POST 115.com/web/lixian/
ac=add_task_url、任务列表归一化、删单 hash[0] 载荷、改名走
webapi files/edit。_http_json 一律桩替身，不连真实 115。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import transfer115

COOKIE = "UID=987654_ab12cd; CID=abc; SEID=def"


def _client(monkeypatch, handler):
    calls = []

    def fake_http(method, url, *, params=None, data=None, cookie="",
                  timeout=30, ua=""):
        calls.append({"method": method, "url": url, "params": params,
                      "data": data})
        return handler(calls[-1])

    monkeypatch.setattr(transfer115, "_http_json", fake_http)
    return transfer115.Pan115Client(COOKIE), calls


def test_cloudpull_uid_from_cookie(monkeypatch):
    client, _ = _client(monkeypatch, lambda call: {})
    assert client.cloudpull_uid() == "987654"


def test_cloudpull_sign_fetches_sign_and_time(monkeypatch):
    def handler(call):
        assert call["url"] == "https://115.com/"
        assert call["params"] == {"ct": "clouddownload", "ac": "space"}
        return {"state": True, "sign": "SGN123", "time": 1700000000}

    client, _ = _client(monkeypatch, handler)
    sign, ts = client.cloudpull_sign()
    assert sign == "SGN123"
    assert ts == "1700000000"


def test_cloudpull_add_task_payload(monkeypatch):
    def handler(call):
        if call["params"]["ac"] == "space":
            return {"state": True, "sign": "S1", "time": "1700000001"}
        assert call["method"] == "POST"
        assert call["url"] == "https://115.com/web/lixian/"
        assert call["params"] == {"ct": "lixian", "ac": "add_task_url"}
        assert call["data"] == {
            "url": "https://cdn.example.test/emby/Videos/x/stream.m2ts?sign=q",
            "uid": "987654", "sign": "S1", "time": "1700000001",
            "wp_path_id": "900",
        }
        return {"state": True}

    client, _ = _client(monkeypatch, handler)
    out = client.cloudpull_add_task(
        "https://cdn.example.test/emby/Videos/x/stream.m2ts?sign=q", "900")
    assert out["state"] is True


def test_cloudpull_tasks_normalized(monkeypatch):
    def handler(call):
        assert call["params"] == {"ct": "lixian", "ac": "task_lists"}
        assert call["data"]["uid"] == "987654"
        assert call["data"]["page"] == 1
        return {"state": True, "tasks": [
            {"info_hash": "h1", "url": "u1", "name": "stream.m2ts",
             "status": "2", "percentDone": "100", "file_id": "f1",
             "size": "4096"},
            {"info_hash": "h2", "url": "u2", "status": -1,
             "percentDone": 0},
        ]}

    client, _ = _client(monkeypatch, handler)
    tasks = client.cloudpull_tasks()
    assert tasks[0] == {"info_hash": "h1", "url": "u1",
                        "name": "stream.m2ts", "status": 2,
                        "percentDone": 100.0, "file_id": "f1",
                        "size": 4096}
    assert tasks[1]["status"] == -1
    assert tasks[1]["percentDone"] == 0.0


def test_cloudpull_del_task_payload(monkeypatch):
    def handler(call):
        assert call["params"] == {"ct": "lixian", "ac": "task_del"}
        assert call["data"] == {"hash[0]": "hashXYZ"}
        return {"state": True}

    client, _ = _client(monkeypatch, handler)
    assert client.cloudpull_del_task("hashXYZ")["state"] is True


def test_rename_file_payload(monkeypatch):
    def handler(call):
        assert call["url"].endswith("/files/edit")
        assert call["data"] == {"fid": "f9", "file_name": "relay.mkv"}
        return {"state": True}

    client, _ = _client(monkeypatch, handler)
    assert client.rename_file("f9", "relay.mkv")["state"] is True
