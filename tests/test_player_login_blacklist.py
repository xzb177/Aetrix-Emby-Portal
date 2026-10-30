"""播放器登录黑名单回归（2026-10-30 用户亲定）。

口径：真实路由 + 真实登录（同 test_emby_lowercase_login.py）。
默认名单（配置行不存在）即用户亲点的两条：爆米花、Infuse 媒体库模式；
Infuse-Direct 靠 exclude 保命、绝不能误伤。

覆盖：
- Client="Popcorn" 登录 403；UA 含 Popcorn 403；大小写不敏感；
- Client="Infuse" / UA "Infuse/8.5.8"（媒体库模式）403；
- Client="Infuse-Direct" / UA "Infuse-Direct/8.5.8" 放行 200；
- SenPlayer 放行；无 Client 的浏览器 UA 放行；
- 拒绝落 login_logs（reason=player_blacklisted，含 username/UA）；
- 配置为空数组时谁都不拦；自定义规则保存后即时生效；
- 保存侧结构校验（config_service.save_config）：坏结构 ConfigValueError；
- 匹配函数单元：exclude 优先、Client/UA 任一命中、空 match 规则不触发。
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

PW = "blacklist-pw"
# 每个用例一个独立用户名：登录限流按 (IP, 账号) 建桶（10 次/分钟），
# 共用一个账号会让后面的用例吃 429。
USERS = [
    "bl_popcorn", "bl_popcorn_ua", "bl_popcorn_case", "bl_infuse",
    "bl_infuse_direct", "bl_senplayer", "bl_browser", "bl_empty_cfg",
    "bl_custom_cfg", "bl_logcheck",
]


@pytest.fixture(scope="module")
def seed():
    db = SessionLocal()
    ids = {}
    try:
        for uname in USERS:
            old = db.query(models.WebUser).filter(
                models.WebUser.username == uname).first()
            if old:
                db.query(em.EmbyApiToken).filter(
                    em.EmbyApiToken.user_id == old.id).delete(
                    synchronize_session=False)
                db.query(models.WebUser).filter(
                    models.WebUser.id == old.id).delete()
                db.commit()
            user = models.WebUser(
                username=uname, password_hash="x", is_active=True,
                is_staff=False, emby_username=uname,
            )
            user.emby_password = hash_password(PW)
            db.add(user)
            db.commit()
            ids[uname] = user.id
    finally:
        db.close()
    yield ids
    db = SessionLocal()
    try:
        for uid in ids.values():
            db.query(em.EmbyApiToken).filter(
                em.EmbyApiToken.user_id == uid).delete(synchronize_session=False)
            db.query(models.WebUser).filter(models.WebUser.id == uid).delete()
        db.commit()
    finally:
        db.close()


@pytest.fixture(scope="module")
def client():
    from backend.main import app
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_blacklist_config():
    """每个用例前后都把黑名单配置行清掉，回到「行不存在 = 默认名单」。"""
    _set_blacklist(None)
    yield
    _set_blacklist(None)


def _set_blacklist(value):
    """走管理端同一条保存通道（config_service.save_config）写配置。

    value=None 表示删行（回默认名单）；list 表示覆盖（含 [] = 停用）。
    """
    from backend import config_service
    from backend.emby_server.player_blacklist import CONFIG_KEY

    db = SessionLocal()
    try:
        config_service.save_config(db, {CONFIG_KEY: value},
                                   schema="player_blacklist")
        db.commit()
    finally:
        db.close()


def _login(client, uname, client_name=None, user_agent=None, device_id=None):
    auth_parts = []
    if client_name is not None:
        auth_parts.append(f'Client="{client_name}"')
    auth_parts += ['Device="iPhone"',
                   f'DeviceId="{device_id or ("dev-" + uname)}"',
                   'Version="1.0"']
    headers = {"X-Emby-Authorization": "MediaBrowser " + ", ".join(auth_parts)}
    if user_agent is not None:
        headers["User-Agent"] = user_agent
    return client.post(
        "/emby/Users/AuthenticateByName",
        json={"Username": uname, "Pw": PW},
        headers=headers,
    )


# ---------- 默认名单：命中 ----------

def test_popcorn_client_rejected(client, seed):
    r = _login(client, "bl_popcorn", client_name="Popcorn",
               user_agent="Popcorn/1.0")
    assert r.status_code == 403, r.text
    assert "该播放器已被服务器停用，请更换播放器" in r.text


def test_popcorn_user_agent_rejected(client, seed):
    # Client 名不命中、只有 UA 含 Popcorn 也要拦（两处都查）
    r = _login(client, "bl_popcorn_ua", client_name="SomePlayer",
               user_agent="PopcornVideo/2.3 (iOS)")
    assert r.status_code == 403, r.text
    assert "该播放器已被服务器停用" in r.text


def test_popcorn_case_insensitive(client, seed):
    r = _login(client, "bl_popcorn_case", client_name="POPCORN",
               user_agent="POPCORN/9.9")
    assert r.status_code == 403, r.text


def test_infuse_library_mode_rejected(client, seed):
    r = _login(client, "bl_infuse", client_name="Infuse",
               user_agent="Infuse/8.5.8")
    assert r.status_code == 403, r.text
    assert "该播放器已被服务器停用，请更换播放器" in r.text


# ---------- 默认名单：不能误伤 ----------

def test_infuse_direct_not_rejected(client, seed):
    # exclude=infuse-direct 必须生效：Direct 模式是正常用户在用的
    r = _login(client, "bl_infuse_direct", client_name="Infuse-Direct",
               user_agent="Infuse-Direct/8.5.8")
    assert r.status_code == 200, r.text
    assert r.json()["AccessToken"]


def test_senplayer_not_rejected(client, seed):
    r = _login(client, "bl_senplayer", client_name="SenPlayer",
               user_agent="SenPlayer/6.3.0")
    assert r.status_code == 200, r.text
    assert r.json()["AccessToken"]


def test_browser_user_agent_not_rejected(client, seed):
    # 无 Client 名 + 浏览器 UA（网页端形态）不许误伤
    r = _login(client, "bl_browser", client_name=None,
               user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)")
    assert r.status_code == 200, r.text


# ---------- 拒绝记录 ----------

def test_rejection_writes_login_log(client, seed):
    r = _login(client, "bl_logcheck", client_name="Popcorn",
               user_agent="Popcorn/1.2.3")
    assert r.status_code == 403, r.text
    db = SessionLocal()
    try:
        row = (
            db.query(models.LoginLog)
            .filter(models.LoginLog.username == "bl_logcheck",
                    models.LoginLog.reason == "player_blacklisted")
            .order_by(models.LoginLog.id.desc())
            .first()
        )
        assert row is not None, "拒绝必须在 login_logs 留一条 player_blacklisted"
        assert row.success is False
        assert "Popcorn" in (row.user_agent or "")
        assert "爆米花" in (row.detail or "")
    finally:
        db.close()


# ---------- 配置覆盖 ----------

def test_empty_blacklist_allows_everyone(client, seed):
    _set_blacklist([])
    r = _login(client, "bl_empty_cfg", client_name="Popcorn",
               user_agent="Popcorn/1.0")
    assert r.status_code == 200, r.text
    assert r.json()["AccessToken"]


def test_custom_rules_take_effect(client, seed):
    _set_blacklist([{"label": "测试播放器", "match": ["someplayer"],
                     "exclude": []}])
    # 默认名单已被覆盖：Popcorn 不再拦
    r = _login(client, "bl_custom_cfg", client_name="Popcorn",
               user_agent="Popcorn/1.0", device_id="dev-custom-1")
    assert r.status_code == 200, r.text
    # 新规则命中 SomePlayer
    r = _login(client, "bl_custom_cfg", client_name="SomePlayer",
               user_agent="SomePlayer/1.0", device_id="dev-custom-2")
    assert r.status_code == 403, r.text


# ---------- 保存侧校验 ----------

def test_save_config_rejects_bad_structure():
    from backend import config_service
    from backend.config_schema import ConfigValueError
    from backend.emby_server.player_blacklist import CONFIG_KEY

    db = SessionLocal()
    try:
        with pytest.raises(ConfigValueError):
            config_service.save_config(
                db, {CONFIG_KEY: [{"match": "popcorn"}]},
                schema="player_blacklist")
        with pytest.raises(ConfigValueError):
            config_service.save_config(
                db, {CONFIG_KEY: "not-a-json-array"},
                schema="player_blacklist")
        db.rollback()
    finally:
        db.close()


def test_save_config_accepts_json_text():
    from backend.integrations import store
    from backend.emby_server.player_blacklist import CONFIG_KEY

    _set_blacklist('[{"label": "x", "match": ["pop"], "exclude": []}]')
    db = SessionLocal()
    try:
        raw = store.get_value(db, CONFIG_KEY, "")
        assert '"pop"' in raw
    finally:
        db.close()


# ---------- 匹配函数单元 ----------

def test_match_blacklist_semantics():
    from backend.emby_server.player_blacklist import (
        DEFAULT_RULES, match_blacklist)

    # exclude 优先于 match（跨字段也算：Client 命中 exclude 时 UA 命中 match 也不拦）
    assert match_blacklist(DEFAULT_RULES, "Infuse-Direct", "Infuse/8.5.8") is None
    assert match_blacklist(DEFAULT_RULES, "Infuse", "Infuse-Direct/8.5.8") is None
    # 任一字段命中即可
    assert match_blacklist(DEFAULT_RULES, "Infuse", "") is not None
    assert match_blacklist(DEFAULT_RULES, "", "Infuse/8.5.8") is not None
    assert match_blacklist(DEFAULT_RULES, "爆米花", "") is not None
    # 大小写不敏感
    assert match_blacklist(DEFAULT_RULES, "PoPcOrN", "") is not None
    assert match_blacklist(DEFAULT_RULES, "INFUSE", "") is not None
    # 不相关客户端不命中；空名单不命中；空 match 的规则不触发
    assert match_blacklist(DEFAULT_RULES, "SenPlayer", "SenPlayer/6.3.0") is None
    assert match_blacklist([], "Popcorn", "Popcorn/1.0") is None
    assert match_blacklist([{"label": "e", "match": [], "exclude": []}],
                           "Popcorn", "") is None
    assert match_blacklist(DEFAULT_RULES, "", "") is None
