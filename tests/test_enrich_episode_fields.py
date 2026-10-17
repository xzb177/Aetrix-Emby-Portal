"""单集分项开关测试（批3：刮削瘦身收官）。

覆盖：
- _episode_field_config：默认值全开（与现状一致）、环境变量优先、DB 回退、
  save/get 回环、管理后台字段名与前端 EnrichEpisodeFieldsConfig 接口一致
- TmdbClient.apply_episode(fields=...)：分项关时的跳过行为、三项全开=现状行为
- _fetch_episode_tmdb：三项全关时不打 TMDB 请求；只关剧照时跳过预热
- 夜间总开关优先：夜间开关开 + 窗口外时，单集在认领层即被跳过，
  分项开关不能绕过（分项只管"写什么"，不管"何时抢单"）

- 全部在隔离临时 SQLite 里跑，不连生产库；
- 不测真实 TMDB 网络。
"""
import os
import tempfile
import uuid

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db"); os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm
_dbmod.engine = _ce(os.environ["DATABASE_URL"])
# 必须走 configure_session_local：直接赋值会把 SessionLocal 代理顶掉，
# 之后各模块 import 到的是被冻结的真 factory，又会查旧库。
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import pytest
from types import SimpleNamespace

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker
from backend.emby_server.tmdb import TmdbClient

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        # 每个测试前清空（隔离，避免测试间污染）
        s.query(em.MediaStream).delete()
        s.query(em.MediaItem).delete()
        s.commit()
        yield s
    finally:
        s.close()


@pytest.fixture(autouse=True)
def _clear_field_cfg_cache(monkeypatch, db):
    """每个测试前清掉分项开关的 60 秒 TTL 缓存 + 环境变量 + DB 行，避免测试间污染"""
    enrich_worker._ep_fields_cfg_cache["at"] = 0.0
    enrich_worker._ep_fields_cfg_cache["cfg"] = None
    for k in (enrich_worker.ENRICH_EPISODE_TITLE_ENV,
              enrich_worker.ENRICH_EPISODE_OVERVIEW_ENV,
              enrich_worker.ENRICH_EPISODE_STILL_ENV):
        monkeypatch.delenv(k, raising=False)
    from backend import models as _models
    db.query(_models.SystemConfig).filter(
        _models.SystemConfig.key.in_([
            enrich_worker.CFG_EPISODE_TITLE,
            enrich_worker.CFG_EPISODE_OVERVIEW,
            enrich_worker.CFG_EPISODE_STILL,
        ])).delete(synchronize_session=False)
    db.commit()
    yield
    enrich_worker._ep_fields_cfg_cache["at"] = 0.0
    enrich_worker._ep_fields_cfg_cache["cfg"] = None


def _make_item(db, **kw):
    it = em.MediaItem(
        guid=uuid.uuid4().hex,
        library_id=kw.pop("library_id", 1),
        item_type=kw.pop("item_type", "movie"),
        name=kw.pop("name", "测试"),
        file_path=kw.pop("file_path", "/tmp/x.mp4"),
        enrich_status=kw.pop("enrich_status", "pending"),
        **kw,
    )
    db.add(it)
    db.commit()
    return it


def _ep_item(name="第 1 集", overview=""):
    return SimpleNamespace(name=name, overview=overview, sort_name=None)


def _ep_data(name="摩登到访", overview="第一集的简介", still="/still1.jpg"):
    return {"episode_number": 1, "name": name, "overview": overview,
            "still_path": still}


# ---------- _episode_field_config ----------

def test_field_config_defaults_all_on(db):
    """默认值：三项全开 = 与现状行为完全一致（升级零行为变化）"""
    cfg = enrich_worker._episode_field_config(db)
    assert cfg["title"] is True
    assert cfg["overview"] is True
    assert cfg["still"] is True
    assert cfg["from_env"] is False


def test_field_config_env_wins(db, monkeypatch):
    """环境变量优先：显式关掉标题"""
    monkeypatch.setenv(enrich_worker.ENRICH_EPISODE_TITLE_ENV, "0")
    cfg = enrich_worker._episode_field_config(db)
    assert cfg["title"] is False
    assert cfg["overview"] is True
    assert cfg["still"] is True
    assert cfg["from_env"] is True


def test_field_config_env_explicit_off_all(db, monkeypatch):
    """三个环境变量全关：三项全关"""
    monkeypatch.setenv(enrich_worker.ENRICH_EPISODE_TITLE_ENV, "0")
    monkeypatch.setenv(enrich_worker.ENRICH_EPISODE_OVERVIEW_ENV, "off")
    monkeypatch.setenv(enrich_worker.ENRICH_EPISODE_STILL_ENV, "no")
    cfg = enrich_worker._episode_field_config(db)
    assert cfg == {"title": False, "overview": False, "still": False,
                   "from_env": True}


def test_field_config_db_backed(db):
    """DB 回退：save 后 get 读到新值（走 SystemConfig）"""
    saved = enrich_worker.save_episode_field_config(
        db, title=False, overview=True, still=False)
    assert saved["title"] is False
    assert saved["overview"] is True
    assert saved["still"] is False
    # 新会话再读（save 已清缓存，这里验证写库 + 回读）
    cfg = enrich_worker.get_episode_field_config(db)
    assert cfg["title"] is False
    assert cfg["overview"] is True
    assert cfg["still"] is False


def test_field_config_ttl_cache(db, monkeypatch):
    """60 秒 TTL 缓存：DB 改了但缓存期内读旧值（与夜间配置同口径）"""
    from backend.integrations import store
    cfg1 = enrich_worker._episode_field_config(db)
    assert cfg1["title"] is True
    # 绕开 save 直接写库（不清缓存）
    store.write_values(db, {enrich_worker.CFG_EPISODE_TITLE: "0"},
                       {enrich_worker.CFG_EPISODE_TITLE: "t"})
    db.commit()
    cfg2 = enrich_worker._episode_field_config(db)
    assert cfg2["title"] is True  # 缓存未过期，仍读旧值
    # 清缓存后读到新值
    enrich_worker._ep_fields_cfg_cache["at"] = 0.0
    cfg3 = enrich_worker._episode_field_config(db)
    assert cfg3["title"] is False


def test_public_config_field_names_match_frontend(db):
    """get_episode_field_config 返回的顶层字段必须与前端
    EnrichEpisodeFieldsConfig 接口一致（title/overview/still/from_env）"""
    cfg = enrich_worker.get_episode_field_config(db)
    for k in ("title", "overview", "still", "from_env"):
        assert k in cfg, f"缺字段 {k}"


# ---------- apply_episode 分项门控 ----------

def test_apply_episode_fields_none_is_current_behavior():
    """fields=None（三项全开）= 现状行为：占位标题被真实标题覆盖、
    简介只填空、剧照路径返回"""
    item = _ep_item(name="第 1 集", overview="")
    result = TmdbClient.apply_episode(item, _ep_data())
    assert item.name == "摩登到访"
    assert item.overview == "第一集的简介"
    assert result["updated"] is True
    assert result["still_path"] == "/still1.jpg"
    # 显式全开 dict 与 None 行为一致
    item2 = _ep_item(name="第 1 集", overview="")
    result2 = TmdbClient.apply_episode(
        item2, _ep_data(), fields={"title": True, "overview": True, "still": True})
    assert item2.name == "摩登到访"
    assert item2.overview == "第一集的简介"
    assert result2 == result


def test_apply_episode_title_off():
    """标题关：占位标题不被覆盖，其它项照常"""
    item = _ep_item(name="第 1 集", overview="")
    result = TmdbClient.apply_episode(
        item, _ep_data(), fields={"title": False, "overview": True, "still": True})
    assert item.name == "第 1 集"  # 没被覆盖
    assert item.overview == "第一集的简介"  # 简介照常
    assert result["still_path"] == "/still1.jpg"  # 剧照照常


def test_apply_episode_overview_off():
    """简介关：简介不填，其它项照常"""
    item = _ep_item(name="第 1 集", overview="")
    result = TmdbClient.apply_episode(
        item, _ep_data(), fields={"title": True, "overview": False, "still": True})
    assert item.name == "摩登到访"  # 标题照常
    assert item.overview == ""  # 简介没写
    assert result["still_path"] == "/still1.jpg"


def test_apply_episode_still_off():
    """剧照关：不返回剧照路径，标题/简介照常"""
    item = _ep_item(name="第 1 集", overview="")
    result = TmdbClient.apply_episode(
        item, _ep_data(), fields={"title": True, "overview": True, "still": False})
    assert item.name == "摩登到访"
    assert item.overview == "第一集的简介"
    assert result["still_path"] is None


def test_apply_episode_all_off_no_writes():
    """三项全关：什么都不写"""
    item = _ep_item(name="第 1 集", overview="")
    result = TmdbClient.apply_episode(
        item, _ep_data(), fields={"title": False, "overview": False, "still": False})
    assert item.name == "第 1 集"
    assert item.overview == ""
    assert result == {"updated": False, "still_path": None}


def test_apply_episode_partial_fields_dict():
    """fields 只给部分 key：没给的按默认开"""
    item = _ep_item(name="第 1 集", overview="")
    result = TmdbClient.apply_episode(item, _ep_data(), fields={"title": False})
    assert item.name == "第 1 集"
    assert item.overview == "第一集的简介"
    assert result["still_path"] == "/still1.jpg"


# ---------- _fetch_episode_tmdb 分项门控 ----------

def test_fetch_episode_tmdb_all_off_no_request(monkeypatch):
    """三项全关：不打 TMDB 请求（find_episode 一次都不调），
    也不写 episode_tmdb / episode_still_url"""
    monkeypatch.setenv(enrich_worker.ENRICH_EPISODE_TITLE_ENV, "0")
    monkeypatch.setenv(enrich_worker.ENRICH_EPISODE_OVERVIEW_ENV, "0")
    monkeypatch.setenv(enrich_worker.ENRICH_EPISODE_STILL_ENV, "0")

    def _boom(self, *a, **k):
        raise AssertionError("三项全关时不应打 TMDB 请求")
    # 注意：必须 patch 类属性，不能 patch tmdb_client 单例的实例属性——
    # monkeypatch 对实例 setattr 会在 teardown 时把旧的 bound method 写回实例
    # __dict__，形成遮蔽类属性的残留，污染后续测试（实测踩坑）。
    monkeypatch.setattr(TmdbClient, "find_episode", _boom)

    item = SimpleNamespace(season_number=1, episode_number=1)
    result: dict = {}
    enrich_worker._fetch_episode_tmdb(
        item, {"tmdb_id": "12345"}, result)
    assert "episode_tmdb" not in result
    assert "episode_still_url" not in result


def test_fetch_episode_tmdb_still_off_skips_prewarm(monkeypatch):
    """只关剧照：TMDB 照打（标题/简介还要），但跳过剧照预热"""
    monkeypatch.setenv(enrich_worker.ENRICH_EPISODE_STILL_ENV, "0")
    called = []

    def _fake_find(self, sid, season_no, ep_no):
        called.append((sid, season_no, ep_no))
        return {"episode_number": 1, "name": "摩登到访",
                "overview": "简介", "still_path": "/still1.jpg"}
    # 同上：patch 类属性，不 patch 单例实例属性（防实例 __dict__ 残留遮蔽）
    monkeypatch.setattr(TmdbClient, "find_episode", _fake_find)

    item = SimpleNamespace(season_number=1, episode_number=1)
    result: dict = {}
    # 注意：_fetch_episode_tmdb 在函数内 from backend.emby_server.tmdb import
    # prewarm_images；剧照关时预热分支根本不执行（上面已断言无 still_url），
    # 这里只验证请求照打、标题/简介数据照拿。
    enrich_worker._fetch_episode_tmdb(
        item, {"tmdb_id": "12345"}, result)
    assert called == [("12345", 1, 1)]  # 请求照打
    assert result.get("episode_tmdb", {}).get("name") == "摩登到访"
    assert "episode_still_url" not in result  # 剧照 URL 没写


# ---------- 夜间总开关优先（分项不能绕过认领门控） ----------

def _enable_night_only(monkeypatch, in_window: bool):
    monkeypatch.setenv(enrich_worker.ENRICH_EPISODE_NIGHT_ONLY_ENV, "1")
    monkeypatch.setattr(
        enrich_worker, "_in_night_window", lambda *a, **k: in_window)


def test_night_gate_takes_precedence_over_fields(db, monkeypatch):
    """夜间总开关开 + 窗口外：单集在认领层即被跳过——此时分项开关
    （即使全开）也不能让单集被刮。分项只管"写什么"，不管"何时抢单"。"""
    _enable_night_only(monkeypatch, in_window=False)
    # 分项开关全开
    enrich_worker.save_episode_field_config(db, True, True, True)
    ep = _make_item(db, item_type="episode", name="S01E01", series_id=999)
    movie = _make_item(db, item_type="movie", name="电影A")
    claimed = enrich_worker._claim_batch(db, 10)
    claimed_ids = {c.id for c in claimed}
    assert ep.id not in claimed_ids
    assert movie.id in claimed_ids
    db.refresh(ep)
    assert ep.enrich_status == "pending"


def test_fields_apply_when_night_gate_off(db):
    """夜间总开关关（默认）：单集正常被抢，分项开关在写库阶段才起作用"""
    ep = _make_item(db, item_type="episode", name="S01E01", series_id=999)
    claimed = enrich_worker._claim_batch(db, 10)
    assert {c.id for c in claimed} == {ep.id}
