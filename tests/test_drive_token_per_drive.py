"""按盘取 Drive 凭据（get_token_for_drive）——全项目唯一入口。

覆盖：
- 共享盘 key → SA 轮换（_get_token）原样
- myDrive:<remote> → remote 自身 OAuth（rclone.conf）
- OAuth access_token 未过期 → 直接复用，不发 HTTP
- OAuth 过期 → refresh_token 刷新；失败抛 DriveAuthError（不静默回退 SA）
- 缺配置 → DriveAuthError，且错误信息不含凭据值
- drive_changes 个人盘轮询（get_start_page_token / list_changes）走 OAuth 路径

凭据安全：测试只用伪造配置，永不使用真实 rclone.conf。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.emby_server import drive_changes as dc

FAKE_REMOTE = "paul_emby"
PERSONAL_KEY = f"myDrive:{FAKE_REMOTE}"
SHARED_KEY = "0AERUVu4kfWEfUk9PVA"


def _write_conf(tmp_path, token_json):
    p = tmp_path / "rclone.conf"
    p.write_text(
        "[paul_emby]\n"
        "type = drive\n"
        "client_id = test-client-id\n"
        "client_secret = test-client-secret\n"
        f"token = {json.dumps(token_json)}\n",
        encoding="utf-8",
    )
    return str(p)


@pytest.fixture(autouse=True)
def _clean_oauth_cache():
    dc._oauth_token_cache.clear()
    yield
    dc._oauth_token_cache.clear()


def test_shared_drive_uses_sa_rotation(monkeypatch):
    """共享盘 key → 走 SA 轮换，原样。"""
    monkeypatch.setattr(dc, "_get_token", lambda: "SA-TOKEN")
    assert dc.get_token_for_drive(SHARED_KEY) == "SA-TOKEN"
    assert dc.get_token_for_drive(SHARED_KEY, force_refresh=True) == "SA-TOKEN"


def test_personal_drive_reuses_valid_access_token(tmp_path, monkeypatch):
    """conf 里 access_token 未过期 → 直接复用，不发任何 HTTP。"""
    conf = _write_conf(tmp_path, {
        "access_token": "AT-STILL-VALID",
        "refresh_token": "RT-1",
        "expiry": "2999-01-01T00:00:00Z",
    })
    monkeypatch.setattr(dc, "RCLONE_CONF", conf)

    def _no_http(*a, **k):
        raise AssertionError("should not hit network")

    monkeypatch.setattr(dc.httpx, "post", _no_http)
    assert dc.get_token_for_drive(PERSONAL_KEY) == "AT-STILL-VALID"


def test_personal_drive_refreshes_expired_token(tmp_path, monkeypatch):
    """过期 → 走 refresh_token 刷新，并缓存。"""
    conf = _write_conf(tmp_path, {
        "access_token": "AT-OLD",
        "refresh_token": "RT-1",
        "expiry": "2020-01-01T00:00:00Z",
    })
    monkeypatch.setattr(dc, "RCLONE_CONF", conf)

    calls = []

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"access_token": "AT-NEW", "expires_in": 3600}

    def _fake_post(url, data=None, timeout=None):
        calls.append(data)
        assert data["grant_type"] == "refresh_token"
        assert data["refresh_token"] == "RT-1"
        assert "client_secret" in data
        return _Resp()

    monkeypatch.setattr(dc.httpx, "post", _fake_post)
    assert dc.get_token_for_drive(PERSONAL_KEY) == "AT-NEW"
    assert len(calls) == 1
    # 第二次走缓存，不再刷新
    assert dc.get_token_for_drive(PERSONAL_KEY) == "AT-NEW"
    assert len(calls) == 1


def test_personal_drive_refresh_failure_raises_no_silent_fallback(tmp_path, monkeypatch):
    """刷新失败 → 抛 DriveAuthError，不静默回退 SA 池；错误信息不含凭据。"""
    conf = _write_conf(tmp_path, {
        "access_token": "AT-OLD",
        "refresh_token": "RT-BAD",
        "expiry": "2020-01-01T00:00:00Z",
    })
    monkeypatch.setattr(dc, "RCLONE_CONF", conf)

    def _boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(dc.httpx, "post", _boom)
    # 即使 SA 池可用，也不许回退
    monkeypatch.setattr(dc, "_get_token", lambda: "SA-TOKEN")
    with pytest.raises(dc.DriveAuthError) as ei:
        dc.get_token_for_drive(PERSONAL_KEY)
    assert "AT-OLD" not in str(ei.value)
    assert "RT-BAD" not in str(ei.value)


def test_personal_drive_missing_conf_raises(tmp_path, monkeypatch):
    """remote 无 OAuth 配置 → DriveAuthError。"""
    p = tmp_path / "rclone.conf"
    p.write_text("[other]\ntype = drive\n", encoding="utf-8")
    monkeypatch.setattr(dc, "RCLONE_CONF", str(p))
    monkeypatch.setattr(dc, "_get_token", lambda: "SA-TOKEN")
    with pytest.raises(dc.DriveAuthError):
        dc.get_token_for_drive("myDrive:paul_emby")


def test_get_start_page_token_personal_uses_oauth(monkeypatch):
    """个人盘 getStartPageToken：用 OAuth token，传 spaces=drive。"""
    seen = {}

    monkeypatch.setattr(dc, "get_token_for_drive",
                        lambda did, force_refresh=False: "OAUTH-T" if did == PERSONAL_KEY else None)

    def _fake_api(url, token, params):
        seen["token"] = token
        seen["params"] = params
        return {"startPageToken": "PT-1"}

    monkeypatch.setattr(dc, "_api_get", _fake_api)
    assert dc.get_start_page_token(PERSONAL_KEY) == "PT-1"
    assert seen["token"] == "OAUTH-T"
    assert seen["params"]["spaces"] == "drive"
    assert "driveId" not in seen["params"]


def test_list_changes_personal_uses_oauth(monkeypatch):
    """个人盘 changes 轮询：用 OAuth token。"""
    monkeypatch.setattr(dc, "get_token_for_drive", lambda did, force_refresh=False: "OAUTH-T")

    def _fake_api(url, token, params):
        assert token == "OAUTH-T"
        assert params["spaces"] == "drive"
        return {"changes": [], "newStartPageToken": "PT-2"}

    monkeypatch.setattr(dc, "_api_get", _fake_api)
    changes, new_token = dc.list_changes("PT-1", PERSONAL_KEY)
    assert changes == [] and new_token == "PT-2"
