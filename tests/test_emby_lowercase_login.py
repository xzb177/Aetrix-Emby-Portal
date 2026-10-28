"""Emby 登录接口小写路径兼容（2026-10-28 生产实测 AfuseKt 405 回归）。

来源：2026-10-28 10:36-10:37 生产日志，AfuseKt/Android/OnePlus 播放器
POST /emby/Users/authenticatebyname（全小写）被回 405 Method Not Allowed。
机制：小写路径没对上登录接口，却对上了只认 GET 的 /Users/{user_id} 路径格式。

口径与 test_ea_player_compat_gaps.py 一致：真实路由 + 真实登录；
小写与大写路径行为一致：合法登录两边都 200 且签发可用 token，
错误密码两边都 401 空 body。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient

from backend import models
from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.security import hash_password

init_db()

PW = "lowerlogin-pw"
UNAME = "lowerlogin_alice"

AUTH_HEADERS = {"X-Emby-Authorization":
                'MediaBrowser Client="LowerLoginTest", Device="iOS", '
                'DeviceId="lower-dev-1", Version="1.0"'}


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    old = db.query(models.WebUser).filter(
        models.WebUser.username == UNAME).first()
    if old:
        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == old.id).delete(synchronize_session=False)
        db.query(models.WebUser).filter(models.WebUser.id == old.id).delete()
        db.commit()
    user = models.WebUser(
        username=UNAME, password_hash="x", is_active=True, is_staff=False,
        emby_username=UNAME, emby_password=hash_password(PW),
    )
    db.add(user)
    db.commit()
    user_id = user.id
    db.close()
    yield {"user_id": user_id}
    db = SessionLocal()
    try:
        db.query(em.EmbyApiToken).filter(
            em.EmbyApiToken.user_id == user_id).delete(synchronize_session=False)
        db.query(models.WebUser).filter(models.WebUser.id == user_id).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app)


def _login(client, path, password=PW):
    return client.post(
        path,
        json={"Username": UNAME, "Pw": password},
        headers=AUTH_HEADERS,
    )


def test_uppercase_login_ok(client, seed):
    r = _login(client, "/emby/Users/AuthenticateByName")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["AccessToken"]
    assert body["User"]["Name"] == UNAME


def test_lowercase_emby_login_ok(client, seed):
    r = _login(client, "/emby/Users/authenticatebyname")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["AccessToken"]
    assert body["User"]["Name"] == UNAME


def test_lowercase_bare_login_ok(client, seed):
    r = _login(client, "/Users/authenticatebyname")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["AccessToken"]
    assert body["User"]["Name"] == UNAME


def test_wrong_password_401_both_casings(client, seed):
    for path in ("/emby/Users/AuthenticateByName",
                 "/emby/Users/authenticatebyname"):
        r = _login(client, path, password="wrong-pw")
        assert r.status_code == 401, f"{path}: {r.status_code} {r.text}"
        assert r.content == b"", f"{path}: 失败 body 必须为空，实际为 {r.content!r}"


def test_lowercase_token_works(client, seed):
    r = _login(client, "/emby/Users/authenticatebyname")
    assert r.status_code == 200, r.text
    token = r.json()["AccessToken"]
    user_id = r.json()["User"]["Id"]
    r2 = client.get(f"/emby/Users/{user_id}",
                    headers={"X-Emby-Token": token})
    assert r2.status_code == 200, r2.text
