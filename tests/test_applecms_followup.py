# -*- coding: utf-8 -*-
"""苹果 CMS 后续四件之 ②③④ 测试（全离线）。

- ③ 逐站解析：开关关时逐字旧行为（解析器绝不被调）；开关开时
  注入的逐站解析函数按线路名派发，返回直链才放行。
- ④ 未配对展示：同步产出聚合对象，展示函数本地在前、未配对
  只补本地没有的作品；hide 模式下不出现。
- ② 管理触发路由：只接受苹果 CMS 源；两闸关（disabled 结果）
  映射 409；其它类型源 400；查无此源 404。
"""
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.api import admin_upstream_sources as admin_ups
from backend.emby_server import applecms_fetcher as acf
from backend.emby_server import applecms_production as acp
from backend.emby_server import applecms_sync as sync
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server import upstream_playback as up
from backend.emby_server import upstream_sources as ups

PARSE_ONLY_VOD = {
    "vod_play_from": "解析线路",
    "vod_play_url": "第01集$playtoken203#第02集$playtoken204",
}
PARSE_ONLY_DETAIL = {
    "code": 1, "page": 1, "pagecount": 1,
    "list": [{"vod_id": 203, **PARSE_ONLY_VOD}],
}


def _resolver_by_line(address, group_name):
    """测试用逐站解析器：只认「解析线路」这一线路名（数据驱动
    的线路名，非 hardcode 站名——换线路名即不认）。"""
    if group_name == "解析线路" and address.startswith("playtoken"):
        return "https://cdn.example.com/resolved.m3u8"
    return None


# ---------------- ③ 逐站解析（fetcher 层） ----------------


def test_parse_switch_off_resolver_never_called():
    calls = []

    def resolver(address, group_name):
        calls.append((address, group_name))
        return "https://cdn.example.com/resolved.m3u8"

    assert acf.resolve_play(PARSE_ONLY_VOD, 1, parse_lines_enabled=False,
                            parse_resolver=resolver) == (
        "unplayable", "parse_not_enabled")
    assert calls == []


def test_parse_switch_on_resolver_direct_wins():
    result = acf.resolve_play(PARSE_ONLY_VOD, 2, parse_lines_enabled=True,
                              parse_resolver=_resolver_by_line)
    assert result == ("ok", "解析线路", "https://cdn.example.com/resolved.m3u8")


def test_parse_resolver_non_direct_or_none_is_rule_missing():
    assert acf.resolve_play(
        PARSE_ONLY_VOD, 1, parse_lines_enabled=True,
        parse_resolver=lambda a, g: "playtoken-again") == (
        "unplayable", "parse_rule_missing")
    assert acf.resolve_play(
        PARSE_ONLY_VOD, 1, parse_lines_enabled=True,
        parse_resolver=lambda a, g: None) == (
        "unplayable", "parse_rule_missing")

    def _boom(address, group_name):
        raise RuntimeError("解析器炸了")

    assert acf.resolve_play(
        PARSE_ONLY_VOD, 1, parse_lines_enabled=True,
        parse_resolver=_boom) == ("unplayable", "parse_rule_missing")
    # 无解析器时旧行为逐字保留。
    assert acf.resolve_play(
        PARSE_ONLY_VOD, 1, parse_lines_enabled=True) == (
        "unplayable", "parse_rule_missing")


def test_parse_resolver_not_used_when_direct_group_exists():
    calls = []
    vod = {
        "vod_play_from": "直链线路$$$解析线路",
        "vod_play_url": "第01集$https://cdn.example.com/ep1.mp4"
                        "$$$第01集$playtoken203",
    }

    def resolver(address, group_name):
        calls.append(address)
        return "https://cdn.example.com/resolved.m3u8"

    assert acf.resolve_play(vod, 1, parse_lines_enabled=True,
                            parse_resolver=resolver) == (
        "ok", "直链线路", "https://cdn.example.com/ep1.mp4")
    assert calls == []


# ---------------- ③ 逐站解析（播放分支透传） ----------------


def _cms_source():
    return SimpleNamespace(
        id=7, source_type="applecms", base_url="https://cms.example.com",
        enabled=True, user_agent=None, referer=None,
        extra_headers_json=None)


def test_playback_forwards_parse_resolver(monkeypatch):
    monkeypatch.setattr(ups, "get_source", lambda db, sid: _cms_source())
    monkeypatch.setenv("UPSTREAM_PLAYBACK_ENABLED", "1")
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    monkeypatch.setenv("APPLECMS_PARSE_LINES_ENABLED", "1")
    monkeypatch.delenv("APPLECMS_TIER_GATE", raising=False)

    def http_get(url, headers, timeout):
        return 200, PARSE_ONLY_DETAIL

    captured = []

    def probe(url, headers, timeout):
        captured.append(url)
        return 206, None

    url = up.resolve_upstream_stream(
        None, 7, "203", http_get=http_get, http_probe=probe,
        parse_resolver=_resolver_by_line)
    assert url == "https://cdn.example.com/resolved.m3u8"
    assert captured == [url]


# ---------------- ④ 未配对展示 ----------------


def _sync_unmatched(monkeypatch, mode):
    monkeypatch.setenv(sync.APPLECMS_ENABLED_ENV, "1")
    monkeypatch.setenv(sync.UPSTREAM_SYNC_ENABLED_ENV, "1")
    monkeypatch.setenv(sync.MAPPING_ENABLED_ENV, "1")
    entry = {"vod_id": "v1", "vod_name": "未配对的片", "vod_year": "2020"}
    result = sync.sync_source(
        SimpleNamespace(id=7),
        fetch_page=lambda *a, **k: {"list": [entry], "pagecount": 1},
        list_bindings=lambda src: [{"t": 1, "sync_enabled": True}],
        list_locals=lambda binding: [],
        record_map=lambda **kw: None,
        set_health=lambda sid, h: None,
        sleep=lambda s: None,
        unmatched_mode=mode,
    )
    return result


def test_unmatched_items_kept_for_presentation(monkeypatch):
    result = _sync_unmatched(monkeypatch, "parallel")
    assert result.unmatched == ["title:未配对的片:2020"]
    assert len(result.unmatched_items) == 1
    item = result.unmatched_items[0]
    assert item.name == "未配对的片"
    assert item.production_year == 2020
    assert item.lines and item.lines[0].upstream_item_id == "v1"


def test_presentation_local_first_unmatched_supplements(monkeypatch):
    result = _sync_unmatched(monkeypatch, "parallel")
    local_same = [agg.PresentationEntry(
        aggregate_key=result.unmatched_items[0].aggregate_key,
        origin="local", name="本地同名片")]
    assert sync.select_unmatched_for_presentation(
        result, local_same) == local_same
    local_other = [agg.PresentationEntry(
        aggregate_key="title:本地片:2019", origin="local", name="本地片")]
    presented = sync.select_unmatched_for_presentation(result, local_other)
    assert [e.origin for e in presented] == ["local", "upstream:7"]
    assert presented[1].name == "未配对的片"


def test_presentation_hide_excludes_unmatched(monkeypatch):
    result = _sync_unmatched(monkeypatch, "parallel")
    local = [agg.PresentationEntry(
        aggregate_key="title:本地片:2019", origin="local", name="本地片")]
    assert sync.select_unmatched_for_presentation(
        result, local, unmatched_mode="hide") == local
    hidden = _sync_unmatched(monkeypatch, "hide")
    assert hidden.unmatched_items == [] and hidden.hidden == 1
    assert sync.select_unmatched_for_presentation(hidden, local) == local


# ---------------- ② 管理触发路由 ----------------


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


@pytest.fixture()
def staff():
    return SimpleNamespace(id=7, is_staff=True, username="admin")


def _create_source(db, source_type):
    return ups.create_source(
        db, name=f"源-{source_type}", source_type=source_type,
        base_url="https://cms.example.com", auth_method="api_key",
        secret=None)


def _fake_result(**kw):
    base = {"source_id": "7", "scanned": 2, "mapped": 1,
            "unmatched": ["title:x:2020"], "hidden": 0, "errors": [],
            "health": "healthy"}
    base.update(kw)
    return sync.SyncResult(**base)


def test_admin_sync_route_applecms_success(monkeypatch, db, staff):
    row = _create_source(db, "applecms")
    db.commit()
    calls = []

    def fake_trigger(db_arg, source_id):
        calls.append(source_id)
        return _fake_result()

    monkeypatch.setattr(acp, "production_trigger_applecms_sync", fake_trigger)
    body = admin_ups.sync_applecms_upstream_source(
        row.id, staff=staff, db=db)
    assert calls == [row.id]
    assert body["health"] == "healthy"
    assert body["scanned"] == 2 and body["mapped"] == 1
    assert body["unmatched_count"] == 1
    assert "playtoken" not in str(body) and "http" not in str(body)


def test_admin_sync_route_rejects_non_applecms(db, staff):
    row = _create_source(db, "emby")
    db.commit()
    with pytest.raises(HTTPException) as exc:
        admin_ups.sync_applecms_upstream_source(row.id, staff=staff, db=db)
    assert exc.value.status_code == 400


def test_admin_sync_route_disabled_maps_409(monkeypatch, db, staff):
    row = _create_source(db, "applecms")
    db.commit()
    monkeypatch.setattr(
        acp, "production_trigger_applecms_sync",
        lambda db_arg, sid: _fake_result(health="disabled"))
    with pytest.raises(HTTPException) as exc:
        admin_ups.sync_applecms_upstream_source(row.id, staff=staff, db=db)
    assert exc.value.status_code == 409


def test_admin_sync_route_missing_source_404(db, staff):
    with pytest.raises(HTTPException) as exc:
        admin_ups.sync_applecms_upstream_source(999999, staff=staff, db=db)
    assert exc.value.status_code == 404
