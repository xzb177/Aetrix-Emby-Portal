"""元数据源密钥：单一存储 + 逐源行内保存 + 全链路无明文

需求背景（Phase 6c）：TMDB 的密钥此前存了两份（6a 的 ``tmdb_api_keys`` 与
6b 的 ``meta_source_keys_tmdb``），后台显示两遍；OMDb / TheTVDB 只能走命令行写库。
这里钉住：单一存储、行内保存即生效、任何出口都不出现明文。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.emby_server import tmdb as tmdb_lib
from backend.emby_server.metasources import config as ms_config
from backend.emby_server.metasources import sources as ms_sources
from backend.integrations import store
from backend.models import SystemConfig

SECRET_TMDB_1 = "TMDBKEY000123"
SECRET_TMDB_2 = "TMDBKEY000456"
SECRET_OMDB = "OMDBKEY-abc123"
SECRET_TVDB = "TVDBKEY-xyz789"
ALL_SECRETS = (SECRET_TMDB_1, SECRET_TMDB_2, SECRET_OMDB, SECRET_TVDB)


@pytest.fixture(autouse=True)
def _no_proxy_env(monkeypatch):
    """环境隔离：本机沙盒的 NO_PROXY 含 httpx 解析不了的条目（如 [::1]），
    构造 httpx.Client 会直接炸 InvalidURL。本模块测试走本地内存库、从不
    需要真实代理，这里先清掉（与 test_admin_tmdb_scrape 的 _clean_cache 同口径）。"""
    for _var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                 "all_proxy", "ALL_PROXY", "no_proxy", "NO_PROXY"):
        monkeypatch.delenv(_var, raising=False)
    yield


@pytest.fixture()
def db():
    """隔离内存库；另外清掉 store 的全局配置读缓存（它是进程级、按 key 缓存的）"""
    from backend import models as base_models

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    base_models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    session.close()
    store.invalidate()


@pytest.fixture()
def keys_pool():
    """TMDB 客户端的密钥池是进程内的，写完记得还原，别漏给别的用例"""
    original = list(tmdb_lib.tmdb_client.api_keys)
    yield
    tmdb_lib.tmdb_client._set_keys(original, "test")


def _view(db):
    from backend.api import admin_scrape
    return admin_scrape._meta_sources_view(db)


def _add(db, source_id, key):
    from backend.api import admin_scrape
    return admin_scrape.add_meta_source_key(
        source_id, admin_scrape.MetaSourceKeyAddRequest(key=key), staff=None, db=db)


# ---------------------------------------------------------------------------
# 1. TMDB 单一存储
# ---------------------------------------------------------------------------

def test_tmdb_key_goes_to_the_single_storage(db, keys_pool):
    """从多源接口给 TMDB 加的 key，落点是 6a 的 tmdb_api_keys"""
    _add(db, "tmdb", SECRET_TMDB_1)
    assert store.get_value(db, "tmdb_api_keys", "") == SECRET_TMDB_1
    # 废弃的分叉键压根不该被创建
    assert store.get_value(db, "meta_source_keys_tmdb", "") == ""


def test_tmdb_key_takes_effect_without_restart(db, keys_pool):
    """写完立刻热生效：真正发请求的 TmdbClient 要拿到新钥匙（不是下次重启才有）"""
    _add(db, "tmdb", SECRET_TMDB_1)
    assert SECRET_TMDB_1 in tmdb_lib.tmdb_client.api_keys
    _add(db, "tmdb", SECRET_TMDB_2)
    assert set(tmdb_lib.tmdb_client.api_keys) == {SECRET_TMDB_1, SECRET_TMDB_2}


def test_tmdb_reads_back_from_single_storage(db, keys_pool):
    db.add(SystemConfig(key="tmdb_api_keys", value=SECRET_TMDB_1))
    db.commit()
    store.invalidate()
    cfg = ms_config.read_config(db, ms_sources.SPECS)
    assert cfg.by_id("tmdb").keys == [SECRET_TMDB_1]
    assert cfg.by_id("tmdb").keys_legacy is False
    assert cfg.by_id("tmdb").key_storage == "tmdb_api_keys"


def test_legacy_tmdb_key_is_flagged_but_not_silently_used(db, keys_pool):
    """旧键里的值**不当场生效**，只标出来等启动自愈合并

    为什么不当场生效：真正发请求的是 ``TmdbClient``，它只读 ``tmdb_api_keys``。
    让配置层显示“有 key”但一把都用不上，比明说“没有”更坑。
    """
    db.add(SystemConfig(key="meta_source_keys_tmdb", value=SECRET_TMDB_1))
    db.commit()
    store.invalidate()
    cfg = ms_config.read_config(db, ms_sources.SPECS)
    assert cfg.by_id("tmdb").keys == []              # 不把它当生效值
    assert cfg.by_id("tmdb").keys_legacy is True      # 但界面会提醒
    row = next(r for r in _view(db)["sources"] if r["id"] == "tmdb")
    assert row["keys_legacy"] is True


def test_startup_heal_moves_legacy_tmdb_key_into_the_single_storage(db, keys_pool):
    """启动自愈：旧键 → tmdb_api_keys，一次性、幂等"""
    from backend import config_self_heal

    db.add(SystemConfig(key="meta_source_keys_tmdb", value=SECRET_TMDB_1))
    db.commit()
    store.invalidate()

    config_self_heal.heal_system_config(db)
    assert store.get_value(db, "tmdb_api_keys", "") == SECRET_TMDB_1
    assert store.get_value(db, "meta_source_keys_tmdb", "") == ""
    store.invalidate()
    cfg = ms_config.read_config(db, ms_sources.SPECS)
    assert cfg.by_id("tmdb").keys == [SECRET_TMDB_1]
    assert cfg.by_id("tmdb").keys_legacy is False

    # 幂等：再跑一次不报错、不动值
    config_self_heal.heal_system_config(db)
    assert store.get_value(db, "tmdb_api_keys", "") == SECRET_TMDB_1


def test_heal_never_overwrites_the_primary_storage(db, keys_pool):
    """两边都有值时**以正式入口为准**，迁移不能猜、也不能覆盖用户后填的"""
    from backend import config_self_heal

    db.add(SystemConfig(key="meta_source_keys_tmdb", value=SECRET_TMDB_1))
    db.add(SystemConfig(key="tmdb_api_keys", value=SECRET_TMDB_2))
    db.commit()
    store.invalidate()
    config_self_heal.heal_system_config(db)
    assert store.get_value(db, "tmdb_api_keys", "") == SECRET_TMDB_2


def test_heal_logs_no_secret_material(db, keys_pool, caplog):
    """迁移只记键名，不记密钥原文"""
    import logging

    from backend import config_self_heal

    db.add(SystemConfig(key="meta_source_keys_tmdb", value=SECRET_TMDB_1))
    db.commit()
    store.invalidate()
    with caplog.at_level(logging.INFO):
        config_self_heal.heal_system_config(db)
    assert SECRET_TMDB_1 not in caplog.text


def test_primary_storage_wins_over_legacy(db, keys_pool):
    """两处都有值时以 tmdb_api_keys 为准（那是真正发请求用的那个）"""
    db.add(SystemConfig(key="meta_source_keys_tmdb", value=SECRET_TMDB_1))
    db.add(SystemConfig(key="tmdb_api_keys", value=SECRET_TMDB_2))
    db.commit()
    store.invalidate()
    cfg = ms_config.read_config(db, ms_sources.SPECS)
    assert cfg.by_id("tmdb").keys == [SECRET_TMDB_2]
    assert cfg.by_id("tmdb").keys_legacy is True   # 旧键还有残留，但不影响生效值


def test_tmdb_row_points_at_the_pool_card_instead_of_a_second_input(db, keys_pool):
    """后台只留一处 TMDB 填写入口：多源卡片这行只给跳转，不给第二个输入框"""
    row = next(r for r in _view(db)["sources"] if r["id"] == "tmdb")
    assert row["key_entry"] == "pool_card"
    assert row["key_storage"] == "tmdb_api_keys"


def test_tmdb_uses_the_real_pool_so_cooldowns_agree_across_cards(db, keys_pool):
    """多源卡片里 TMDB 的冷却状态必须来自 6a 那个池——不能是另一个各自为政的池"""
    tmdb_lib.tmdb_client._set_keys([SECRET_TMDB_1, SECRET_TMDB_2], "test")
    tmdb_lib.tmdb_client.note_key_invalid(SECRET_TMDB_1)
    row = next(r for r in _view(db)["sources"] if r["id"] == "tmdb")
    states = {k["masked"]: k["cooling"] for k in row["keys"]}
    assert states["****0123"] is True
    assert states["****0456"] is False


def test_tmdb_never_writes_the_dead_config_key(db, keys_pool):
    """函数层面堵死：即使有人把 tmdb 塞进 keys，也不会写进废弃键"""
    ms_config.write_config(db, enabled=True, prefer_chinese=True, order=[],
                           toggles={}, keys={"tmdb": [SECRET_TMDB_1]})
    assert store.get_value(db, "meta_source_keys_tmdb", "") == ""
    assert store.get_value(db, "tmdb_api_keys", "") == SECRET_TMDB_1


def test_self_heal_registers_one_key_store_per_source():
    """新装部署：后台要能填全部源的 key，且 TMDB 只注册一处"""
    from backend import config_self_heal

    keys = [k for k, _v, _d in config_self_heal.collect_system_config_defaults()]
    assert "tmdb_api_keys" in keys
    assert "meta_source_keys_tmdb" not in keys
    assert "meta_source_keys_omdb" in keys
    assert "meta_source_keys_tvdb" in keys
    assert len(keys) == len(set(keys)), "配置键不允许重复注册"


# ---------------------------------------------------------------------------
# 2. OMDb / TheTVDB：行内填写
# ---------------------------------------------------------------------------

def test_omdb_and_tvdb_keys_can_be_filled_from_the_admin(db):
    for source_id, secret in (("omdb", SECRET_OMDB), ("tvdb", SECRET_TVDB)):
        view = _add(db, source_id, secret)
        row = next(r for r in view["sources"] if r["id"] == source_id)
        assert row["key_count"] == 1
        assert row["keys"][0]["masked"].endswith(secret[-4:])
        # 行内入口 + 告诉用户存在哪个配置键
        assert row["key_entry"] == "inline"
        assert row["key_storage"] == f"meta_source_keys_{source_id}"
        # 配了就不再是「不参与」
        assert row["skipped_reason"] == ""
    store.invalidate()
    cfg = ms_config.read_config(db, ms_sources.SPECS)
    assert cfg.by_id("omdb").keys == [SECRET_OMDB]
    assert cfg.by_id("tvdb").keys == [SECRET_TVDB]


def test_each_keyed_source_explains_where_to_apply_for_it():
    """每个要 key 的源都要有一句人话说明 + 申请链接，不能让管理员自己去搜"""
    for spec in ms_sources.SPECS:
        if not spec.requires_key:
            continue
        assert spec.apply_url, f"{spec.id} 缺申请链接"
        assert spec.apply_hint, f"{spec.id} 缺申请说明"
        assert spec.apply_url.startswith("https://")


def test_view_carries_apply_hint_and_storage(db):
    view = _view(db)
    omdb = next(r for r in view["sources"] if r["id"] == "omdb")
    assert "omdbapi.com" in omdb["apply_url"]
    assert "免费" in omdb["apply_hint"]
    tvdb = next(r for r in view["sources"] if r["id"] == "tvdb")
    assert "thetvdb.com" in tvdb["apply_url"]
    assert "剧集" in tvdb["apply_hint"]


def test_key_add_and_delete_are_index_based_and_masked(db):
    from backend.api import admin_scrape

    _add(db, "omdb", SECRET_OMDB)
    view = _add(db, "omdb", SECRET_OMDB + "-2")
    row = next(r for r in view["sources"] if r["id"] == "omdb")
    assert [k["index"] for k in row["keys"]] == [1, 2]
    view = admin_scrape.delete_meta_source_key("omdb", 1, staff=None, db=db)
    row = next(r for r in view["sources"] if r["id"] == "omdb")
    assert row["key_count"] == 1
    assert row["keys"][0]["masked"].endswith("c123-2"[-4:])


# ---------------------------------------------------------------------------
# 3. 明文绝不出口
# ---------------------------------------------------------------------------

def test_no_secret_ever_appears_in_the_view(db, keys_pool):
    _add(db, "tmdb", SECRET_TMDB_1)
    _add(db, "omdb", SECRET_OMDB)
    _add(db, "tvdb", SECRET_TVDB)
    payload = json.dumps(_view(db), ensure_ascii=False)
    for secret in (SECRET_TMDB_1, SECRET_OMDB, SECRET_TVDB):
        assert secret not in payload


def test_masks_keep_only_last_four_characters(db):
    _add(db, "omdb", "ABCDEFGH1234")
    row = next(r for r in _view(db)["sources"] if r["id"] == "omdb")
    assert row["keys"][0]["masked"] == "****1234"
    assert len(row["keys"][0]["masked"]) == 8


def test_short_or_odd_keys_do_not_leak():
    """短 key 也不能因为「太短就整个显示」而泄露"""
    from backend.emby_server.metasources import keypool

    assert keypool.mask("abc") == "****"
    assert keypool.mask("") == "****"
    assert keypool.mask("1234") == "****"
    assert keypool.mask("12345") == "****2345"