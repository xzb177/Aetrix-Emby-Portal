"""手动识别 P1/P2：全量刷新（mode=all）、IMDb ID、TMDB 首选语言。

P0（候选搜索）由另一个任务负责，这里只覆盖 P1/P2 的后端逻辑：
- P2a：bind_tmdb_id 的 mode=all 先清空 TMDB 字段再重填（保留 name）
- P2b：tt 开头的 IMDb ID 经 /find 换算成 TMDB ID 再走现有流程
- P2c：SystemConfig.tmdb_preferred_language 的读写 + _request 统一注入 language
"""
import os
import tempfile
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmp = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}"

from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

from backend.database import init_db  # noqa: E402

init_db()

import pytest  # noqa: E402

from backend.api import admin_scrape  # noqa: E402
from backend.emby_server import models as em  # noqa: E402
from backend.emby_server import tmdb as tmdb_mod  # noqa: E402


@pytest.fixture()
def own_db():
    """本用例自建库并设为当前 factory（用完还原），不依赖测试执行顺序"""
    from backend import models as base_models
    engine = _ce("sqlite:///:memory:")
    original = _dbmod.SessionLocal
    _dbmod.configure_session_local(_sm(bind=engine))
    base_models.Base.metadata.create_all(bind=engine)
    em.Base.metadata.create_all(bind=engine)
    db = _dbmod.SessionLocal()
    try:
        lib = em.Library(guid="L" * 32, name="库", collection_type="tvshows", paths="")
        db.add(lib)
        db.flush()
        yield db, lib
    finally:
        db.close()
        _dbmod.configure_session_local(original)
        engine.dispose()


def _movie(db, lib, name="旧片名", **kw):
    base = dict(guid="m" * 32, item_type="movie", name=name, overview="旧简介",
                tmdb_id=None, metadata_source="none", enrich_status="failed",
                enrich_attempts=5, enrich_next_retry_at=None, last_scraped_at=None)
    base.update(kw)
    it = em.MediaItem(library_id=lib.id, **base)
    db.add(it)
    db.flush()
    return it


_DETAILS = {"id": 12345, "title": "新片名", "overview": "新简介",
            "vote_average": 8.5, "poster_path": "/p.jpg",
            "external_ids": {"imdb_id": "tt9999999"}, "alternative_titles": {}}


class _FakeClient:
    """假的 tmdb_client：apply_* 直接落字段，便于断言「清了又重填」"""
    configured = True

    def __init__(self, details=None, find=None):
        self._details = _DETAILS if details is None else details
        self._find = find
        self.details_calls = 0

    def details(self, *a, **k):
        self.details_calls += 1
        return self._details

    def find_by_imdb(self, imdb_id):
        return self._find

    def apply_details(self, item, data):
        # 与真客户端 tmdb_client.apply_details 同契约：只填空、不覆盖（fill-only）。
        # WS1/B3 门放宽后：即使条目已有 imdb_id/别名，只要任一文字字段缺失
        # 就调 apply_details——已有简介/评分绝不许被覆盖，mock 必须建模这一点。
        if not (getattr(item, "overview", None) or "").strip():
            item.overview = data.get("overview")
        if getattr(item, "community_rating", None) is None:
            item.community_rating = data.get("vote_average")
        if not getattr(item, "imdb_id", None):
            item.imdb_id = (data.get("external_ids") or {}).get("imdb_id")
        if not (getattr(item, "aliases", None) or "").strip():
            item.aliases = "新片名"

    def apply_images(self, item, data):
        item.primary_image_url = "https://img.example/p.jpg"
        return True


@pytest.fixture()
def no_prewarm(monkeypatch):
    """prewarm_images 真下载图片：测试里不发 HTTP"""
    monkeypatch.setattr(admin_scrape, "prewarm_images", lambda *a, **k: 0)


# ==================== P2a：全量刷新 ====================

def test_bind_all_mode_clears_tmdb_fields_and_refills(own_db, monkeypatch, no_prewarm):
    """mode=all：旧的 TMDB 字段被清空再重填；片名 name 不动"""
    db, lib = own_db
    it = _movie(db, lib, name="管理员手改的名字", overview="旧简介",
                community_rating=1.0, genres="旧类型", aliases="旧别名",
                primary_image_url="https://old.example/p.jpg", poster_path="/old/p.jpg",
                imdb_id="tt0000001", tmdb_id="111", metadata_source="tmdb")
    fake = _FakeClient()
    monkeypatch.setattr(admin_scrape, "tmdb_client", fake)

    res = admin_scrape.bind_tmdb_id(
        it.id, admin_scrape.TmdbBindRequest(tmdb_id="12345", mode="all"),
        SimpleNamespace(id=1), db)

    assert res["success"] is True
    assert it.tmdb_id == "12345"
    assert it.name == "管理员手改的名字", "name 不在清空列表里，必须保留"
    assert it.overview == "新简介"
    assert it.community_rating == 8.5
    assert it.imdb_id == "tt9999999", "imdb_id 清掉后要按新详情重填"
    assert it.primary_image_url == "https://img.example/p.jpg"
    assert "全量刷新" in "；".join(res["notes"])


def test_bind_missing_mode_keeps_existing_fields(own_db, monkeypatch, no_prewarm):
    """mode=missing（默认）：已有字段不动，只补缺失"""
    db, lib = own_db
    it = _movie(db, lib, overview="旧简介", community_rating=7.0,
                imdb_id="tt0000001", aliases="旧别名",
                primary_image_url="https://old.example/p.jpg")
    fake = _FakeClient()
    monkeypatch.setattr(admin_scrape, "tmdb_client", fake)

    res = admin_scrape.bind_tmdb_id(
        it.id, admin_scrape.TmdbBindRequest(tmdb_id="12345"),
        SimpleNamespace(id=1), db)

    assert res["success"] is True
    assert it.overview == "旧简介", "missing 模式不许覆盖已有简介"
    assert it.community_rating == 7.0
    assert it.primary_image_url == "https://old.example/p.jpg"
    assert "全量刷新" not in "；".join(res["notes"])


def test_bind_missing_mode_fills_when_empty(own_db, monkeypatch, no_prewarm):
    """mode=missing：字段空着就补上（回归旧行为）"""
    db, lib = own_db
    it = _movie(db, lib, overview="", imdb_id=None, aliases=None,
                primary_image_url=None, poster_path=None)
    monkeypatch.setattr(admin_scrape, "tmdb_client", _FakeClient())
    res = admin_scrape.bind_tmdb_id(
        it.id, admin_scrape.TmdbBindRequest(tmdb_id="12345", mode="missing"),
        SimpleNamespace(id=1), db)
    assert res["success"] is True
    assert it.overview == "新简介"
    assert it.primary_image_url == "https://img.example/p.jpg"


def test_bind_rejects_bad_mode(own_db):
    """mode 非法 → 400，且不写入"""
    from fastapi import HTTPException
    db, lib = own_db
    it = _movie(db, lib)
    with pytest.raises(HTTPException) as e:
        admin_scrape.bind_tmdb_id(
            it.id, admin_scrape.TmdbBindRequest(tmdb_id="12345", mode="overwrite"),
            SimpleNamespace(id=1), db)
    assert e.value.status_code == 400
    assert it.tmdb_id is None


def test_bind_all_mode_requires_tmdb_config(own_db, monkeypatch, no_prewarm):
    """全量刷新没 key 就取不到数据：直接 400，不能先清再说"""
    from fastapi import HTTPException
    db, lib = own_db
    it = _movie(db, lib, overview="旧简介")
    fake = _FakeClient()
    fake.configured = False
    monkeypatch.setattr(admin_scrape, "tmdb_client", fake)
    with pytest.raises(HTTPException) as e:
        admin_scrape.bind_tmdb_id(
            it.id, admin_scrape.TmdbBindRequest(tmdb_id="12345", mode="all", verify=False),
            SimpleNamespace(id=1), db)
    assert e.value.status_code == 400
    assert it.overview == "旧简介", "失败时旧数据必须还在"


# ==================== P2b：IMDb ID ====================

def test_preview_resolves_imdb_id(own_db, monkeypatch):
    """预览：tt 开头的 IMDb ID 经 /find 换算成 TMDB ID"""
    db, lib = own_db
    it = _movie(db, lib)
    fake = _FakeClient(find={"movie_results": [{"id": 777}], "tv_results": []})
    monkeypatch.setattr(admin_scrape, "tmdb_client", fake)
    res = admin_scrape.preview_tmdb_id(it.id, "tt1234567", SimpleNamespace(id=1), db)
    assert res["tmdb_id"] == "777"
    assert res["imdb_id"] == "tt1234567"
    assert fake.details_calls == 1, "换算后详情要按 TMDB ID 取"


def test_preview_imdb_prefers_matching_kind(own_db, monkeypatch):
    """剧集条目优先取 tv_results；电影优先取 movie_results"""
    db, lib = own_db
    series = em.MediaItem(library_id=lib.id, guid="s" * 32, item_type="series",
                          name="某剧", tmdb_id=None, metadata_source="none")
    db.add(series)
    db.flush()
    fake = _FakeClient(find={"movie_results": [{"id": 111}],
                             "tv_results": [{"id": 222}]})
    monkeypatch.setattr(admin_scrape, "tmdb_client", fake)
    res = admin_scrape.preview_tmdb_id(series.id, "tt1234567", SimpleNamespace(id=1), db)
    assert res["tmdb_id"] == "222"


def test_preview_imdb_not_found(own_db, monkeypatch):
    """TMDB 的 find 两边都没结果 → 404，不绑定"""
    from fastapi import HTTPException
    db, lib = own_db
    it = _movie(db, lib)
    fake = _FakeClient(find={"movie_results": [], "tv_results": []})
    monkeypatch.setattr(admin_scrape, "tmdb_client", fake)
    with pytest.raises(HTTPException) as e:
        admin_scrape.preview_tmdb_id(it.id, "tt0000000", SimpleNamespace(id=1), db)
    assert e.value.status_code == 404


def test_preview_rejects_garbage_id(own_db):
    """既不是纯数字也不是 tt+数字 → 400"""
    from fastapi import HTTPException
    db, lib = own_db
    it = _movie(db, lib)
    for bad in ("abc", "ttabc", "tt12x", "12 34"):
        with pytest.raises(HTTPException) as e:
            admin_scrape.preview_tmdb_id(it.id, bad, SimpleNamespace(id=1), db)
        assert e.value.status_code == 400, bad


def test_bind_with_imdb_stores_tmdb_id(own_db, monkeypatch, no_prewarm):
    """绑定：库里存的是换算后的 TMDB ID，不是 tt 号"""
    db, lib = own_db
    it = _movie(db, lib)
    fake = _FakeClient(find={"movie_results": [{"id": 555}], "tv_results": []})
    monkeypatch.setattr(admin_scrape, "tmdb_client", fake)
    res = admin_scrape.bind_tmdb_id(
        it.id, admin_scrape.TmdbBindRequest(tmdb_id="tt7654321"),
        SimpleNamespace(id=1), db)
    assert res["success"] is True
    assert it.tmdb_id == "555"
    assert any("IMDb tt7654321 → TMDB 555" in n for n in res["notes"])


def test_bind_imdb_without_key_rejected(own_db, monkeypatch):
    """没配 TMDB Key 时 IMDb ID 解析不出来 → 400"""
    from fastapi import HTTPException
    db, lib = own_db
    it = _movie(db, lib)
    fake = _FakeClient()
    fake.configured = False
    monkeypatch.setattr(admin_scrape, "tmdb_client", fake)
    with pytest.raises(HTTPException) as e:
        admin_scrape.bind_tmdb_id(
            it.id, admin_scrape.TmdbBindRequest(tmdb_id="tt7654321"),
            SimpleNamespace(id=1), db)
    assert e.value.status_code == 400


def test_find_by_imdb_uses_find_endpoint(monkeypatch):
    """TmdbClient.find_by_imdb：走 /find/{id}?external_source=imdb_id，带缓存"""
    monkeypatch.setattr(tmdb_mod, "preferred_language", lambda db=None: "zh-CN")
    calls = []

    def fake_get(self, path, params):
        calls.append((path, params))
        return {"movie_results": [{"id": 42}]}

    client = tmdb_mod.TmdbClient.__new__(tmdb_mod.TmdbClient)
    monkeypatch.setattr(tmdb_mod.TmdbClient, "_get", fake_get)
    tmdb_mod.TmdbClient._cache.clear()
    try:
        first = client.find_by_imdb("tt1234567")
        second = client.find_by_imdb("tt1234567")
        assert first == {"movie_results": [{"id": 42}]}
        assert second is first or second == first
        assert len(calls) == 1, "第二次必须命中 L1 缓存，不再发请求"
        assert calls[0][0] == "/find/tt1234567"
        assert calls[0][1] == {"external_source": "imdb_id"}
    finally:
        tmdb_mod.TmdbClient._cache.clear()


# ==================== P2c：首选语言 ====================

class _null_ctx:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_request_injects_preferred_language(monkeypatch):
    """_request：所有请求统一带 language；调用方显式传的优先"""
    monkeypatch.setattr(tmdb_mod, "preferred_language", lambda db=None: "en-US")
    captured = {}

    class _Session:
        def get(self, url, params):
            captured.update(params)
            return SimpleNamespace(status_code=200)

    class _Limiter:
        def acquire(self):
            pass

    client = tmdb_mod.TmdbClient.__new__(tmdb_mod.TmdbClient)
    client.session = _Session()
    client._limiter = _Limiter()
    client.api_key = "k"
    client._stats = {"short_circuit": 0, "retry": 0, "net_fail": 0}

    monkeypatch.setattr("backend.emby_server.scan_progress.stage_timer",
                        lambda name: _null_ctx())
    client._request("/movie/1", {"query": "x"})
    assert captured["language"] == "en-US"
    assert captured["api_key"] == "k"

    captured.clear()
    client._request("/movie/1", {"language": "ja-JP"})
    assert captured["language"] == "ja-JP", "调用方显式传的 language 不被覆盖"


def test_language_get_put_roundtrip(own_db, monkeypatch):
    """GET/PUT /scrape/tmdb-language：保存即生效，非法值 400"""
    from fastapi import HTTPException
    from backend.integrations import store
    db, _lib = own_db
    user = SimpleNamespace(id=1)
    # 进程级缓存都是全局的（tmdb._LANGUAGE_CACHE 30s、store._ttl_cache 60s），
    # 先清干净再测，避免被同进程里先跑的用例污染。
    tmdb_mod.invalidate_language()
    store.invalidate()
    # TMDB_LANGUAGE 环境变量优先级高于 DB（CI 环境里被设为 zh-CN），
    # 不清掉的话保存后读回永远是环境变量的值，测试必失败。
    monkeypatch.delenv("TMDB_LANGUAGE", raising=False)

    got = admin_scrape.get_tmdb_language(user, db)
    assert got["language"] == "zh-CN", "默认 zh-CN"
    assert "zh-TW" in got["options"]

    res = admin_scrape.save_tmdb_language(
        admin_scrape.TmdbLanguageSaveRequest(language="en-US"), user, db)
    assert res["success"] is True
    assert admin_scrape.get_tmdb_language(user, db)["language"] == "en-US"
    assert tmdb_mod.preferred_language(db) == "en-US", "读配置口径与热路径一致"

    with pytest.raises(HTTPException) as e:
        admin_scrape.save_tmdb_language(
            admin_scrape.TmdbLanguageSaveRequest(language="fr-FR"), user, db)
    assert e.value.status_code == 400
    # 收尾清缓存：别把 en-US 留给后面的用例
    tmdb_mod.invalidate_language()
    store.invalidate()
    assert admin_scrape.get_tmdb_language(user, db)["language"] == "en-US", "非法值不许覆盖"
    tmdb_mod.invalidate_language()


def test_l1_cache_key_includes_language():
    """L1 缓存键带语言维度：切语言后旧语言的条目不再命中"""
    client = tmdb_mod.TmdbClient.__new__(tmdb_mod.TmdbClient)
    tmdb_mod.TmdbClient._cache.clear()
    try:
        client._cache_put(("details", "movie", "1", "zh-CN"), {"title": "中文标题"})
        assert client._cache_get(("details", "movie", "1", "zh-CN")) == {"title": "中文标题"}
        assert client._cache_get(("details", "movie", "1", "en-US")) is tmdb_mod._MISS
    finally:
        tmdb_mod.TmdbClient._cache.clear()


def test_disk_cache_key_includes_language(tmp_path, monkeypatch):
    """磁盘缓存键带语言维度：同条目不同语言落不同文件"""
    from backend.emby_server import tmdb_cache
    monkeypatch.setattr(tmdb_cache, "_cfg_dir", lambda: str(tmp_path))
    monkeypatch.setattr(tmdb_cache, "_cfg_enabled", lambda: True)
    zh = tmdb_cache._key_path("movie", "1", 0, "zh-CN")
    en = tmdb_cache._key_path("movie", "1", 0, "en-US")
    assert zh != en
    assert tmdb_cache._key_path("movie", "1", 0, "zh-CN") == zh

    assert tmdb_cache.save_details("movie", "1", {"title": "中文"}, "zh-CN") is True
    hit, data = tmdb_cache.load_details("movie", "1", "zh-CN")
    assert hit and data == {"title": "中文"}
    hit, _ = tmdb_cache.load_details("movie", "1", "en-US")
    assert not hit, "en-US 不该命中 zh-CN 的缓存"


def test_details_uses_language_in_cache_key(monkeypatch):
    """details()：换语言后走新的缓存键，会重新请求；发出的 language 跟随配置"""
    monkeypatch.setattr(tmdb_mod, "preferred_language", lambda db=None: "zh-CN")
    calls = []

    def fake_get(self, path, params):
        calls.append((path, params))
        # _has_credits：v2.51.0 起 details 载荷必须带 credits 键才算有效缓存
        return {"id": 7, "title": "t", "credits": {"cast": []}}

    client = tmdb_mod.TmdbClient.__new__(tmdb_mod.TmdbClient)
    monkeypatch.setattr(tmdb_mod.TmdbClient, "_get", fake_get)
    monkeypatch.setattr("backend.emby_server.scan_progress.stage_timer",
                        lambda name: _null_ctx())
    # 磁盘缓存关掉，只看 L1 行为
    monkeypatch.setattr(tmdb_mod.tmdb_cache, "load_details", lambda *a, **k: (False, None))
    monkeypatch.setattr(tmdb_mod.tmdb_cache, "save_details", lambda *a, **k: True)
    tmdb_mod.TmdbClient._cache.clear()
    try:
        client.details("7", "movie")
        client.details("7", "movie")
        assert len(calls) == 1, "同语言第二次走 L1"
        assert calls[0][1]["language"] == "zh-CN", "发出的 language 必须是配置值"
        monkeypatch.setattr(tmdb_mod, "preferred_language", lambda db=None: "en-US")
        client.details("7", "movie")
        assert len(calls) == 2, "换语言后缓存键不同，必须重新请求"
        assert calls[1][1]["language"] == "en-US"
    finally:
        tmdb_mod.TmdbClient._cache.clear()
