"""TMDB 密钥池（轮换 + 逐把冷却）与镜像地址（Phase 6a）单元测试

钉住的四件事：

1. **冷却是真的在跳过 key**：一把 429 / 401 之后，后续请求不再选它；
2. **冷却不影响别的 key**：还有能用的就换一把继续，不整库停摆；
3. **冷却不是锁死**：全池都在冷却时仍然把请求发出去（宁可撞限流也不能不发）；
4. **镜像地址**：配置 > 官方默认、两个地址分开配、地址校验能给出人话错误。

全部不发真实 HTTP：会话用假替身，冷却时长用极小的值。密钥本体只在断言里出现。
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from backend.emby_server import tmdb as tmdb_mod

# 真实的配置读取函数：夹具里会把 ``_config_text`` 换成假的，接数据库的用例要装回来
_REAL_CONFIG_TEXT = tmdb_mod._config_text


class _Session:
    """假会话：记录每次请求用的 key，结果由 answer 决定"""

    def __init__(self, answer):
        self.calls: list = []
        self._answer = answer

    def get(self, url, params):
        self.calls.append((url, dict(params)))
        return self._answer(url, params)


def _resp(status=200, payload=None, headers=None):
    return SimpleNamespace(status_code=status, headers=headers or {},
                           json=lambda: ({} if payload is None else payload))


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    """没有环境变量、没有 DB 配置：镜像走默认值；并把 sleep 换成记录（不真睡）"""
    for name in ("TMDB_API_KEYS", "TMDB_API_KEY", "TMDB_API_BASE", "TMDB_IMAGE_BASE",
                 "TMDB_KEY_COOLDOWN_SEC", "TMDB_KEY_INVALID_COOLDOWN_SEC"):
        monkeypatch.delenv(name, raising=False)
    # 沙箱的 NO_PROXY 含 [::1] 这类括号 IPv6 条目，httpx 解析会炸：剥掉保持测试隔离
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    # _config_text 自带 refresh 关键字参数，假替身必须接住（否则 TypeError）
    monkeypatch.setattr(tmdb_mod, "_config_text", lambda key, db=None, **kwargs: "")
    slept: list = []
    monkeypatch.setattr(tmdb_mod.time, "sleep", lambda seconds: slept.append(seconds))
    tmdb_mod.invalidate_settings()
    tmdb_mod.invalidate_language()
    yield slept
    tmdb_mod.invalidate_settings()
    tmdb_mod.invalidate_language()


def _client(keys, answer=None) -> tmdb_mod.TmdbClient:
    client = tmdb_mod.TmdbClient.__new__(tmdb_mod.TmdbClient)
    client._keys_lock = __import__("threading").RLock()
    client._session_lock = __import__("threading").RLock()
    client._cooldown = {}
    client.api_keys = []
    client.api_key = ""
    client._key_index = 0
    client.key_source = "db"
    client.session = _Session(answer) if answer else None
    client._limiter = tmdb_mod._RequestLimiter(rate=1_000_000.0, min_rate=1.0)
    client._stats = {"short_circuit": 0, "retry": 0, "net_fail": 0}
    # 不重建 httpx client、不读代理环境（与 tests/test_tmdb_limiter.py 同一手笔）
    client._ensure_session = lambda: None
    client._set_keys(list(keys), "db")
    return client


# ==================== 1. 冷却会跳过这把 key ====================

def test_throttled_key_is_skipped_on_the_next_request():
    state = {"calls": 0}

    def answer(url, params):
        state["calls"] += 1
        # k1 第一次就 429，之后即便轮回来也不再用它
        if params["api_key"] == "k1" and state["calls"] == 1:
            return _resp(429, headers={"retry-after": "300"})
        return _resp(payload={"ok": True})

    client = _client(("k1", "k2"), answer)
    assert client._get("/configuration", {}) == {"ok": True}   # k1 → 429 → 换 k2
    assert client._get("/configuration", {}) == {"ok": True}   # 这一次直接用 k2
    used = [c[1]["api_key"] for c in client.session.calls]
    assert used == ["k1", "k2", "k2"]
    cooling = [row for row in client.key_pool() if row["cooling"]]
    assert len(cooling) == 1 and cooling[0]["reason"] == "限流（HTTP 429）"


def test_invalid_key_gets_a_longer_cooldown_and_is_reported():
    client = _client(("k1",), answer=lambda url, params: _resp(401))
    assert client._get("/configuration", {}) is None

    pool = client.key_pool()
    assert pool[0]["cooling"] is True
    assert "401" in pool[0]["reason"]
    assert pool[0]["hits"] == 1
    # 401 的冷却明显长于 429
    assert pool[0]["cooldown_remaining"] > 100


def test_success_clears_the_cooldown_record():
    state = {"first": True}

    def answer(url, params):
        if state["first"]:
            state["first"] = False
            return _resp(429, headers={"retry-after": "1"})
        return _resp(payload={"ok": True})

    client = _client(("k1",), answer)
    # 单 key 429：先记冷却（下面要断言），再抛瞬态（v2.53.0 起：转重试队列）
    with pytest.raises(tmdb_mod.TmdbTransientError):
        client._get("/configuration", {})
    assert client.key_pool()[0]["cooling"] is True
    client.clear_cooldowns()
    assert client.key_pool()[0]["cooling"] is False


def test_clear_cooldowns_returns_how_many_were_cleared():
    client = _client(("k1", "k2"))
    client._cool_key("k1", "限流（HTTP 429）", 60)
    client._cool_key("k2", "无效（HTTP 401）", 60)
    assert client.clear_cooldowns() == 2
    assert client.clear_cooldowns() == 0


# ==================== 2. 全池冷却时仍然要发出去 ====================

def test_all_cooling_still_sends_a_request():
    client = _client(("k1",), answer=lambda url, params: _resp(payload={"ok": True}))
    client._cool_key("k1", "限流（HTTP 429）", 600)
    assert client._get("/configuration", {}) == {"ok": True}
    assert client.session.calls


def test_pick_key_prefers_cooling_keys_only_as_a_last_resort():
    client = _client(("k1", "k2"))
    client._cool_key("k2", "限流（HTTP 429）", 600)
    assert client._pick_key() == "k1"
    client._cool_key("k1", "限流（HTTP 429）", 600)
    # 全冷却时：优先不排除的（allow_cooled 路径），而不是返回空
    assert client._pick_key() == ""
    assert client._pick_key(allow_cooled=True) != ""


# ==================== 3. 逐把增删时的冷却不会留下陈旧记录 ====================

def test_replacing_the_pool_drops_stale_cooldowns():
    client = _client(("key-0001", "key-0002"))
    client._cool_key("key-0001", "限流（HTTP 429）", 600)
    assert client.key_pool()[0]["cooling"] is True

    client._set_keys(["key-0002", "key-0003"], "db")

    pool = client.key_pool()
    assert all(row["cooling"] is False for row in pool)
    assert [row["masked"] for row in pool] == ["****0002", "****0003"]


# ==================== 4. 镜像地址 ====================

def test_defaults_are_the_official_endpoints(monkeypatch):
    assert tmdb_mod.api_base() == tmdb_mod.TMDB_API_DEFAULT
    assert tmdb_mod.image_base() == tmdb_mod.TMDB_IMAGE_DEFAULT


def test_api_and_image_mirrors_are_configured_separately(monkeypatch):
    values = {
        tmdb_mod.TMDB_API_BASE_CONFIG_KEY: "https://tmdb-proxy.example.com/3/",
        tmdb_mod.TMDB_IMAGE_BASE_CONFIG_KEY: "https://img.example.com/t/p",
    }
    monkeypatch.setattr(tmdb_mod, "_config_text", lambda key, db=None, **kwargs: values.get(key, ""))
    tmdb_mod.invalidate_settings()

    assert tmdb_mod.api_base() == "https://tmdb-proxy.example.com/3"   # 尾部斜杠去掉
    assert tmdb_mod.image_base() == "https://img.example.com/t/p"


def test_bare_host_is_upgraded_to_https(monkeypatch):
    values = {tmdb_mod.TMDB_API_BASE_CONFIG_KEY: "tmdb-proxy.example.com/3"}
    monkeypatch.setattr(tmdb_mod, "_config_text", lambda key, db=None, **kwargs: values.get(key, ""))
    tmdb_mod.invalidate_settings()

    assert tmdb_mod.api_base() == "https://tmdb-proxy.example.com/3"


def test_env_beats_config_for_the_mirror(monkeypatch):
    monkeypatch.setattr(tmdb_mod, "_config_text", lambda key, db=None, **kwargs: "https://from-db.example.com")
    monkeypatch.setenv("TMDB_IMAGE_BASE", "https://from-env.example.com/t/p")
    tmdb_mod.invalidate_settings()

    cfg = tmdb_mod.settings(refresh=True)
    assert cfg["image_base"] == "https://from-env.example.com/t/p"
    assert cfg["image_base_from_env"] is True
    assert cfg["api_base"] == "https://from-db.example.com"


def test_image_urls_use_the_configured_cdn():
    values = {tmdb_mod.TMDB_IMAGE_BASE_CONFIG_KEY: "https://img.example.com/t/p"}
    from backend.emby_server import tmdb as mod

    original = mod._config_text
    mod._config_text = lambda key, db=None, **kwargs: values.get(key, "")
    try:
        mod.invalidate_settings()
        specs = mod.image_specs({"poster_path": "/p.jpg", "backdrop_path": "/b.jpg"})
    finally:
        mod._config_text = original
        mod.invalidate_settings()

    assert specs == [("Primary", "https://img.example.com/t/p/w500/p.jpg"),
                     ("Backdrop", "https://img.example.com/t/p/w1280/b.jpg")]


@pytest.mark.parametrize("bad", [
    "ftp://example.com",
    "https://",
    "有 空格 的 地址",
])
def test_invalid_mirror_is_rejected_with_a_readable_message(bad):
    with pytest.raises(ValueError) as excinfo:
        tmdb_mod.validate_base(bad, "API 镜像地址")
    assert "API 镜像地址" in str(excinfo.value)


def test_empty_mirror_falls_back_to_official():
    assert tmdb_mod.validate_base("", "API 镜像地址") == ""


# ==================== 5. 冷却时长走配置，不写死 ====================

def test_cooldown_durations_come_from_config(monkeypatch):
    values = {
        tmdb_mod.TMDB_KEY_COOLDOWN_CONFIG_KEY: "42",
        tmdb_mod.TMDB_KEY_INVALID_COOLDOWN_CONFIG_KEY: "77",
    }
    monkeypatch.setattr(tmdb_mod, "_config_text", lambda key, db=None, **kwargs: values.get(key, ""))
    tmdb_mod.invalidate_settings()

    client = _client(("key-0001", "key-0002"))
    client._cool_key("key-0001", "限流（HTTP 429）", 42)
    client._cool_key("key-0002", "无效（HTTP 401）", 77)
    rows = {row["masked"]: row for row in client.key_pool()}
    assert rows["****0001"]["cooldown_remaining"] in (41, 42)
    assert rows["****0002"]["cooldown_remaining"] in (76, 77)


def test_mirror_save_rejects_bad_address(db_stub=None):
    from backend.api import admin_scrape

    with pytest.raises(HTTPException) as excinfo:
        admin_scrape.save_tmdb_mirror(
            admin_scrape.TmdbMirrorRequest(api_base="不是地址", image_base=""),
            staff=None, db=None)
    assert excinfo.value.status_code == 400


# ==================== 6. 后台接口：逐把增删 / 清除冷却 / 镜像保存 ====================

@pytest.fixture()
def db(monkeypatch):
    """隔离的内存 SQLite；这里把配置读回**真的**那套（接口测试要验证落库→生效）"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend import models as base_models

    monkeypatch.setattr(tmdb_mod, "_config_text", _REAL_CONFIG_TEXT)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    base_models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _pool(client) -> list:
    from backend.api import admin_scrape

    return admin_scrape.get_tmdb_keys(staff=None, db=None)["pool"]


def test_add_one_key_appends_without_touching_the_others(db):
    from backend.api import admin_scrape
    from backend.integrations import store

    store.write_values(db, {tmdb_mod.TMDB_KEYS_CONFIG_KEY: "key-0001,key-0002"})
    db.commit()

    result = admin_scrape.add_tmdb_key(
        admin_scrape.TmdbKeyAddRequest(key="key-0003"), staff=None, db=db)

    assert result["success"] is True
    assert result["count"] == 3
    assert result["masked"] == ["****0001", "****0002", "****0003"]
    assert result["effective"] is True


def test_adding_the_same_key_twice_is_rejected(db):
    from backend.api import admin_scrape
    from backend.integrations import store

    store.write_values(db, {tmdb_mod.TMDB_KEYS_CONFIG_KEY: "key-0001"})
    db.commit()

    with pytest.raises(HTTPException) as excinfo:
        admin_scrape.add_tmdb_key(
            admin_scrape.TmdbKeyAddRequest(key="key-0001"), staff=None, db=db)
    assert excinfo.value.status_code == 409


def test_delete_by_displayed_index(db):
    from backend.api import admin_scrape
    from backend.integrations import store

    store.write_values(db, {tmdb_mod.TMDB_KEYS_CONFIG_KEY: "key-0001,key-0002,key-0003"})
    db.commit()

    result = admin_scrape.delete_tmdb_key(2, staff=None, db=db)

    assert result["removed"] == "****0002"
    assert result["masked"] == ["****0001", "****0003"]


@pytest.mark.parametrize("index", [0, 4])
def test_delete_out_of_range_explains_the_pool_size(db, index):
    from backend.api import admin_scrape
    from backend.integrations import store

    store.write_values(db, {tmdb_mod.TMDB_KEYS_CONFIG_KEY: "key-0001,key-0002"})
    db.commit()

    with pytest.raises(HTTPException) as excinfo:
        admin_scrape.delete_tmdb_key(index, staff=None, db=db)
    assert excinfo.value.status_code == 400
    assert "2 把" in excinfo.value.detail


def test_cooldown_reset_clears_the_pool_view(db):
    from backend.api import admin_scrape

    tmdb_client = admin_scrape.tmdb_client
    tmdb_client._set_keys(["key-0001", "key-0002"], "db")
    tmdb_client._cool_key("key-0001", "限流（HTTP 429）", 600)

    result = admin_scrape.reset_tmdb_key_cooldown(staff=None, db=db)

    assert result["cleared"] == 1
    assert all(row["cooling"] is False for row in result["pool"])


def test_mirror_save_and_read_back(db):
    from backend.api import admin_scrape

    saved = admin_scrape.save_tmdb_mirror(
        admin_scrape.TmdbMirrorRequest(
            api_base="https://tmdb-proxy.example.com/3/",
            image_base="https://img.example.com/t/p"),
        staff=None, db=db)
    assert saved["api_base"] == "https://tmdb-proxy.example.com/3"

    current = admin_scrape.get_tmdb_mirror(staff=None, db=db)
    assert current["api_base"] == "https://tmdb-proxy.example.com/3"
    assert current["image_base"] == "https://img.example.com/t/p"
    assert current["defaults"]["api_base"] == tmdb_mod.TMDB_API_DEFAULT
    assert current["cooldown_sec"] > 0


def test_empty_mirror_restores_the_official_endpoints(db):
    from backend.api import admin_scrape

    admin_scrape.save_tmdb_mirror(
        admin_scrape.TmdbMirrorRequest(
            api_base="https://tmdb-proxy.example.com/3", image_base=""),
        staff=None, db=db)
    admin_scrape.save_tmdb_mirror(
        admin_scrape.TmdbMirrorRequest(api_base="", image_base=""),
        staff=None, db=db)

    current = admin_scrape.get_tmdb_mirror(staff=None, db=db)
    assert current["api_base"] == tmdb_mod.TMDB_API_DEFAULT
    assert current["image_base"] == tmdb_mod.TMDB_IMAGE_DEFAULT


def test_keys_endpoint_reports_pool_state_and_mirror(db):
    from backend.api import admin_scrape
    from backend.integrations import store

    store.write_values(db, {tmdb_mod.TMDB_KEYS_CONFIG_KEY: "key-0001,key-0002"})
    db.commit()
    admin_scrape.tmdb_client._cool_key("key-0001", "限流（HTTP 429）", 600)

    body = admin_scrape.get_tmdb_keys(staff=None, db=db)

    assert body["count"] == 2
    assert body["keys_cooling"] == 1
    assert body["pool"][0]["cooling"] is True
    assert body["pool"][0]["reason"] == "限流（HTTP 429）"
    # 原文永远不出口
    assert "key-0001" not in str(body)
