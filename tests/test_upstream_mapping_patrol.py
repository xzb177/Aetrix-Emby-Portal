"""映射消费抽样验证（上游聚合链优化 O-09）。

钉死的行为：
- 第 1 层（数据层）：按 evidence 分层抽样，本地死行 / 源失效计数
  与置信度分布照实报；全程只读，映射行一行不改；
- 第 2 层（存在性）：带源凭据 GET 对方 /Items/{id}，200 核身份
  （tmdb/imdb 精确、title+year 核名+年），404 记 missing、断连记
  unreachable、凭据失效整源记 credential_failed；
- 不一致率（mismatch+missing / 已判定）超阈值（默认 5%）报红；
  凭据失效同样报红；
- 周期门：默认开、默认 7 天一轮；未到周期 / 开关关 → 不跑；
- 第 3 层（播放层）只手动：存在性没过的样本绝不进 resolve，
  resolve 失败按错误码计数。

HTTP 全部注入替身；DB 用内存 SQLite。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from datetime import datetime, timedelta
from urllib.parse import urlsplit

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import models as em
from backend.emby_server import upstream_mapping_patrol as ump
from backend.emby_server import upstream_playback as upb
from backend.emby_server import upstream_sources as us
from backend.integrations import store as config_store


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


@pytest.fixture(autouse=True)
def _clear_config_cache():
    config_store._ttl_cache.clear()
    yield
    config_store._ttl_cache.clear()


def _set_config(db, key, value):
    config_store.write_values(db, {key: value})
    db.commit()
    config_store._ttl_cache.clear()


def _source(db, *, name="src-1", enabled=True, auth_method="api_key",
            secret="k3y", source_type="emby"):
    row = em.UpstreamSource(
        name=name, source_type=source_type,
        base_url="https://up.example.com/emby", auth_method=auth_method,
        username="u1" if auth_method == "password" else None,
        sealed_secret=us.seal_secret(secret) if secret is not None else None,
        enabled=enabled, credential_scope="source",
        health_status="unknown")
    db.add(row)
    db.flush()
    return row


def _item(db, *, item_id, name="肖申克的救赎", tmdb=None, imdb=None,
          year=1994, **overrides):
    fields = dict(
        id=item_id, guid=f"guid-{item_id}", library_id=1,
        item_type="movie", name=name, tmdb_id=tmdb, imdb_id=imdb,
        production_year=year, merged_into_id=None)
    fields.update(overrides)
    row = em.MediaItem(**fields)
    db.add(row)
    db.flush()
    return row


def _mapping(db, *, local_id, source_id, upstream_id, evidence="tmdb",
             confidence=0.9):
    row = em.UpstreamItemMapping(
        local_item_id=local_id, source_id=source_id,
        upstream_item_id=upstream_id, confidence=confidence,
        evidence=evidence)
    db.add(row)
    db.flush()
    return row


def _get_from_bodies(bodies):
    """造 http_get 替身：按 URL 里的上游条目 id 查表。

    bodies: {upstream_id: (status, body)}，缺省 (404, None)。
    """
    def _get(url, headers, timeout):
        upstream_id = urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
        return bodies.get(upstream_id, (404, None))
    return _get


# ---------------------------------------------------------------------------
# 配置口径
# ---------------------------------------------------------------------------

def test_config_defaults(db):
    assert ump.patrol_enabled(db) is True
    assert ump.patrol_interval_days(db) == 7
    assert ump.patrol_sample_size(db) == 30
    assert ump.patrol_alert_pct(db) == 5


def test_config_overrides_and_clamps(db):
    _set_config(db, ump.KEY_PATROL_ENABLED, "0")
    _set_config(db, ump.KEY_PATROL_INTERVAL_DAYS, "0")
    _set_config(db, ump.KEY_PATROL_SAMPLE_SIZE, "999")
    _set_config(db, ump.KEY_PATROL_ALERT_PCT, "250")
    assert ump.patrol_enabled(db) is False
    assert ump.patrol_interval_days(db) == 1
    assert ump.patrol_sample_size(db) == 200
    assert ump.patrol_alert_pct(db) == 100


# ---------------------------------------------------------------------------
# 第 1 + 2 层
# ---------------------------------------------------------------------------

def test_layer1_and_2_counts(db):
    src = _source(db)
    dead_src = _source(db, name="src-2", enabled=False)
    _item(db, item_id=1, tmdb="278")
    _item(db, item_id=2, tmdb="999", deleted_at=datetime.now())
    _mapping(db, local_id=1, source_id=src.id, upstream_id="up-ok")
    _mapping(db, local_id=2, source_id=src.id, upstream_id="up-deadlocal")
    _mapping(db, local_id=1, source_id=dead_src.id, upstream_id="up-deadsrc",
             confidence=0.5)
    _mapping(db, local_id=4242, source_id=src.id, upstream_id="up-nolocal")
    db.commit()
    get = _get_from_bodies({
        "up-ok": (200, {"Name": "肖申克的救赎", "ProductionYear": 1994,
                        "ProviderIds": {"Tmdb": "278"}}),
    })
    report = ump.run_mapping_patrol(db, http_get=get)
    tmdb = report["by_evidence"]["tmdb"]
    assert tmdb["sampled"] == 4
    assert tmdb["local_dead"] == 2  # 软删条目 + 条目行不存在
    assert tmdb["source_dead"] == 1  # 停用源那条
    assert tmdb["confidence"]["high"] == 3  # 0.9 × 3
    assert tmdb["confidence"]["low"] == 1  # 0.5 那条
    assert report["totals"]["tmdb"] == 4
    existence = report["existence"]
    assert existence["ok"] == 1
    assert existence["skipped"] == 3
    assert report["alert"] is False


def test_layer2_mismatch_and_missing(db):
    src = _source(db)
    _item(db, item_id=1, tmdb="278")
    _item(db, item_id=2, tmdb="550")
    _item(db, item_id=3, tmdb="680")
    _mapping(db, local_id=1, source_id=src.id, upstream_id="up-ok")
    _mapping(db, local_id=2, source_id=src.id, upstream_id="up-wrong")
    _mapping(db, local_id=3, source_id=src.id, upstream_id="up-gone")
    db.commit()
    get = _get_from_bodies({
        "up-ok": (200, {"ProviderIds": {"Tmdb": "278"}}),
        "up-wrong": (200, {"ProviderIds": {"Tmdb": "551"}}),
        # up-gone 缺省 404
    })
    report = ump.run_mapping_patrol(db, http_get=get)
    existence = report["existence"]
    assert existence["ok"] == 1
    assert existence["mismatch"] == 1
    assert existence["missing"] == 1
    # 不一致率 2/3 ≈ 66% > 5% → 报红
    assert report["alert"] is True
    assert report["mismatch_rate"] == pytest.approx(2 / 3)


def test_layer2_title_year_stratum(db):
    src = _source(db)
    _item(db, item_id=1, name="肖申克的救赎", year=1994)
    _item(db, item_id=2, name="阿甘正传", year=1994)
    _mapping(db, local_id=1, source_id=src.id, upstream_id="up-t1",
             evidence="title+year", confidence=0.7)
    _mapping(db, local_id=2, source_id=src.id, upstream_id="up-t2",
             evidence="title+year", confidence=0.7)
    db.commit()
    get = _get_from_bodies({
        "up-t1": (200, {"Name": "肖申克的救赎", "ProductionYear": 1994,
                        "ProviderIds": {}}),
        "up-t2": (200, {"Name": "阿甘正传", "ProductionYear": 1995,
                        "ProviderIds": {}}),
    })
    report = ump.run_mapping_patrol(db, http_get=get)
    assert report["existence"]["ok"] == 1
    assert report["existence"]["mismatch"] == 1  # 年份对不上


def test_layer2_series_episode_stratum(db):
    # O-08 的 series+episode 证据层：无自身身份的单集核季集号
    src = _source(db)
    _item(db, item_id=1, name="第2集", item_type="episode",
          season_number=1, episode_number=2)
    _item(db, item_id=2, name="第3集", item_type="episode",
          season_number=1, episode_number=3)
    _mapping(db, local_id=1, source_id=src.id, upstream_id="up-e1",
             evidence="series+episode", confidence=0.9)
    _mapping(db, local_id=2, source_id=src.id, upstream_id="up-e2",
             evidence="series+episode", confidence=0.9)
    db.commit()
    get = _get_from_bodies({
        "up-e1": (200, {"IndexNumber": 2, "ParentIndexNumber": 1,
                        "ProviderIds": {}}),
        # 对方那条其实是第 4 集：配错集，必须算 mismatch
        "up-e2": (200, {"IndexNumber": 4, "ParentIndexNumber": 1,
                        "ProviderIds": {}}),
    })
    report = ump.run_mapping_patrol(db, http_get=get)
    stratum = report["by_evidence"]["series+episode"]
    assert stratum["sampled"] == 2
    assert report["totals"]["series+episode"] == 2
    assert report["existence"]["ok"] == 1
    assert report["existence"]["mismatch"] == 1


def test_series_episode_identity_checked_first(db):
    # 单集有自身身份时先核身份：身份对上即 ok（季集号不再参与判定）
    src = _source(db)
    _item(db, item_id=1, name="第2集", item_type="episode", tmdb="555",
          season_number=1, episode_number=2)
    _item(db, item_id=2, name="第5集", item_type="episode", tmdb="556",
          season_number=1, episode_number=5)
    _mapping(db, local_id=1, source_id=src.id, upstream_id="up-i1",
             evidence="series+episode", confidence=0.9)
    _mapping(db, local_id=2, source_id=src.id, upstream_id="up-i2",
             evidence="series+episode", confidence=0.9)
    db.commit()
    get = _get_from_bodies({
        "up-i1": (200, {"IndexNumber": 99, "ParentIndexNumber": 9,
                        "ProviderIds": {"Tmdb": "555"}}),
        "up-i2": (200, {"IndexNumber": 5, "ParentIndexNumber": 1,
                        "ProviderIds": {"Tmdb": "999"}}),
    })
    report = ump.run_mapping_patrol(db, http_get=get)
    assert report["existence"]["ok"] == 1
    assert report["existence"]["mismatch"] == 1


def test_layer2_unreachable_not_mismatch(db):
    src = _source(db)
    _item(db, item_id=1, tmdb="278")
    _mapping(db, local_id=1, source_id=src.id, upstream_id="up-x")
    db.commit()
    get = _get_from_bodies({"up-x": (None, None)})
    report = ump.run_mapping_patrol(db, http_get=get)
    assert report["existence"]["unreachable"] == 1
    assert report["existence"]["mismatch"] == 0
    assert report["alert"] is False  # 打不通不是不一致，不报红


def test_alert_threshold_boundary(db):
    src = _source(db)
    bodies = {}
    for i in range(20):
        _item(db, item_id=100 + i, tmdb=str(1000 + i))
        _mapping(db, local_id=100 + i, source_id=src.id,
                 upstream_id=f"up-{i}")
        bodies[f"up-{i}"] = (200, {"ProviderIds": {"Tmdb": str(1000 + i)}})
    db.commit()
    # 1/20 = 5.0%，不超阈值（严格大于才报红）
    bodies["up-0"] = (200, {"ProviderIds": {"Tmdb": "bogus"}})
    report = ump.run_mapping_patrol(db, http_get=_get_from_bodies(bodies))
    assert report["mismatch_rate"] == pytest.approx(0.05)
    assert report["alert"] is False
    # 2/20 = 10% > 5% → 报红
    bodies["up-1"] = (200, {"ProviderIds": {"Tmdb": "bogus"}})
    report = ump.run_mapping_patrol(db, http_get=_get_from_bodies(bodies))
    assert report["alert"] is True


def test_credential_failure_alerts(db):
    src = _source(db, auth_method="password", secret="pw")
    _item(db, item_id=1, tmdb="278")
    _mapping(db, local_id=1, source_id=src.id, upstream_id="up-1")
    db.commit()

    def _post(url, payload, headers, timeout):
        return 401, {"Message": "invalid"}

    report = ump.run_mapping_patrol(
        db, http_get=_get_from_bodies({}), http_post=_post)
    assert report["existence"]["credential_failed"] == 1
    assert report["alert"] is True


def test_patrol_is_read_only(db):
    src = _source(db)
    _item(db, item_id=1, tmdb="278")
    row = _mapping(db, local_id=1, source_id=src.id, upstream_id="up-1")
    db.commit()
    before = (db.query(em.UpstreamItemMapping).count(), row.updated_at)
    get = _get_from_bodies({
        "up-1": (200, {"ProviderIds": {"Tmdb": "999"}}),  # 故意不一致
    })
    ump.run_mapping_patrol(db, http_get=get)
    db.expire_all()
    after_row = db.query(em.UpstreamItemMapping).one()
    assert db.query(em.UpstreamItemMapping).count() == before[0]
    assert after_row.updated_at == before[1]  # 验错了也只报数，绝不改行


# ---------------------------------------------------------------------------
# 周期门
# ---------------------------------------------------------------------------

def test_maybe_run_period_gate(db):
    src = _source(db)
    _item(db, item_id=1, tmdb="278")
    _mapping(db, local_id=1, source_id=src.id, upstream_id="up-1")
    db.commit()
    get = _get_from_bodies({
        "up-1": (200, {"ProviderIds": {"Tmdb": "278"}}),
    })
    now = datetime(2026, 10, 31, 12, 0, 0)
    first = ump.maybe_run_mapping_patrol(db, now=now, http_get=get)
    assert first is not None and first["sampled"] == 1
    # 同一周期内不再跑
    again = ump.maybe_run_mapping_patrol(
        db, now=now + timedelta(days=3), http_get=get)
    assert again is None
    # 过了 7 天又能跑
    later = ump.maybe_run_mapping_patrol(
        db, now=now + timedelta(days=8), http_get=get)
    assert later is not None


def test_maybe_run_disabled(db):
    _set_config(db, ump.KEY_PATROL_ENABLED, "0")
    assert ump.maybe_run_mapping_patrol(db, http_get=_get_from_bodies({})) \
        is None


# ---------------------------------------------------------------------------
# 接线：janitor 周期挂载 + 管理端第 3 层入口
# ---------------------------------------------------------------------------

def test_janitor_runs_due_patrol(db_factory, monkeypatch):
    from backend import database as _database
    from backend.emby_server import maintenance as maint
    from backend.emby_server import upstream_aggregate_run as uar
    monkeypatch.setattr(_database, "SessionLocal", db_factory)
    db = db_factory()
    try:
        src = _source(db)
        _item(db, item_id=1, tmdb="278")
        _mapping(db, local_id=1, source_id=src.id, upstream_id="up-1")
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(uar, "_default_http_get", _get_from_bodies({
        "up-1": (200, {"ProviderIds": {"Tmdb": "278"}}),
    }))
    result = maint.janitor_tick()
    assert result["upstream_mapping_patrol_sampled"] == 1
    assert result["upstream_mapping_patrol_alert"] is False


def test_admin_verify_playback_endpoint(db, monkeypatch):
    from types import SimpleNamespace
    from backend.api import admin_upstream_sources as admin_ups
    from backend.emby_server import upstream_aggregate_run as uar
    src = _source(db)
    _item(db, item_id=1, tmdb="278")
    _mapping(db, local_id=1, source_id=src.id, upstream_id="up-1")
    db.commit()
    monkeypatch.setattr(uar, "_default_http_get", _get_from_bodies({
        "up-1": (200, {"ProviderIds": {"Tmdb": "278"}}),
    }))
    monkeypatch.setattr(
        upb, "resolve_upstream_stream",
        lambda db_, source_id, upstream_item_id, **kw:
            "https://up.example.com/emby/Videos/up-1/stream")
    staff = SimpleNamespace(id=7, is_staff=True, username="admin")
    report = admin_ups.verify_upstream_playback_samples(
        src.id, staff=staff, db=db)
    assert report["sampled"] == 1
    assert report["resolved"] == 1
    assert report["failed"] == {}


def test_admin_verify_playback_endpoint_404(db):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from backend.api import admin_upstream_sources as admin_ups
    staff = SimpleNamespace(id=7, is_staff=True, username="admin")
    with pytest.raises(HTTPException) as excinfo:
        admin_ups.verify_upstream_playback_samples(9999, staff=staff, db=db)
    assert excinfo.value.status_code == 404


# ---------------------------------------------------------------------------
# 第 3 层（手动）
# ---------------------------------------------------------------------------

def test_verify_playback_samples(db):
    src = _source(db)
    _item(db, item_id=1, tmdb="278")
    _item(db, item_id=2, tmdb="550")
    _item(db, item_id=3, tmdb="680")
    _mapping(db, local_id=1, source_id=src.id, upstream_id="up-ok")
    _mapping(db, local_id=2, source_id=src.id, upstream_id="up-bad")
    _mapping(db, local_id=3, source_id=src.id, upstream_id="up-gone")
    db.commit()
    get = _get_from_bodies({
        "up-ok": (200, {"ProviderIds": {"Tmdb": "278"}}),
        "up-bad": (200, {"ProviderIds": {"Tmdb": "550"}}),
        # up-gone 404：存在性没过，绝不进 resolve
    })
    seen = []

    def _resolver(db_, source_id, upstream_item_id, **kwargs):
        seen.append(upstream_item_id)
        if upstream_item_id == "up-bad":
            raise upb.UpstreamPlaybackError(
                502, "upstream_unavailable", "上游不可用",
                source_id=source_id)
        return "https://up.example.com/emby/Videos/up-ok/stream"

    report = ump.verify_playback_samples(
        db, http_get=get, resolver=_resolver)
    assert report["existence_ok"] == 2
    assert report["resolved"] == 1
    assert report["failed"] == {"upstream_unavailable": 1}
    assert sorted(seen) == ["up-bad", "up-ok"]  # up-gone 没进 resolve
