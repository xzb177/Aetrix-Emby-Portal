# -*- coding: utf-8 -*-
"""苹果 CMS P1 合同测试（全离线，不碰真实 CMS 站）。

覆盖任务单 P1 的合同面（既有实现已在主线，见对账说明）：
- 抓取器：list 分页循环、code 非 1 按上游不可用（bad_code）。
- 播放解析：detail 取 vod、首个全直链组选中、集序号取址、
  解析型组在解析开关关时记不可播、不伪装可播。
- 配对合同：身份 = vod_id；vod_douban_id 不读不配对；
  证据固定 title+year、置信度封顶 0.7。
- 总闸：APPLECMS_ENABLED 关时 applecms 源不可创建；
  开时可建；关闸不影响 emby 旧类型建源。

本文件不做试点站实测：真实 CMS 端点待用户给，届时另做实测。
"""

import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import applecms_fetcher as acf
from backend.emby_server import applecms_sync as sync
from backend.emby_server import models as em
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server import upstream_sources as ups

PUBLIC_URL = "https://cms.example.com/"


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


def _page(items, page, pagecount):
    return (200, {"code": 1, "page": page, "pagecount": pagecount,
                  "limit": 2, "total": 3, "list": items})


def test_fetch_list_pagination_and_bad_code():
    seen_urls = []

    def fake_get(url, headers, timeout):
        seen_urls.append(url)
        if "pg=1" in url:
            return _page([{"vod_id": "1", "vod_name": "甲"},
                          {"vod_id": "2", "vod_name": "乙"}], 1, 2)
        return _page([{"vod_id": "3", "vod_name": "丙"}], 2, 2)

    vods = list(acf.iter_pages("https://cms.example.com", http_get=fake_get))
    assert [v["vod_id"] for v in vods] == ["1", "2", "3"]
    assert all("ac=list" in u for u in seen_urls)

    def bad_get(url, headers, timeout):
        return (200, {"code": 0, "msg": "no", "list": []})

    with pytest.raises(acf.AppleCMSError) as exc_info:
        acf.fetch_page("https://cms.example.com", http_get=bad_get)
    assert exc_info.value.kind == "bad_code"
    assert exc_info.value.cms_code == 0


def test_detail_direct_group_and_episode_index():
    detail = {"code": 1, "page": 1, "pagecount": 1, "list": [{
        "vod_id": "77",
        "vod_play_from": "解析线路$$$直链线路",
        "vod_play_url": (
            "第01集$playtoken77"
            "$$$第01集$https://cdn.example.com/ep1.mp4"
            "#第02集$https://cdn.example.com/ep2.mp4"),
    }]}

    def fake_get(url, headers, timeout):
        assert "ac=detail" in url and "ids=77" in url
        return (200, detail)

    vod = acf.fetch_detail("https://cms.example.com", "77",
                           http_get=fake_get)
    assert vod["vod_id"] == "77"
    # 首个全直链组是第二组，集序号按组内顺序取，不从集标题文字解析。
    assert acf.resolve_play(vod, 2) == (
        "ok", "直链线路", "https://cdn.example.com/ep2.mp4")
    assert acf.resolve_play(vod, 3) == (
        "unplayable", "episode_out_of_range")


def test_parse_only_group_is_unplayable_not_faked():
    vod = {"vod_play_from": "解析线路",
           "vod_play_url": "第01集$playtoken77"}
    assert acf.resolve_play(vod, 1, parse_lines_enabled=False) == (
        "unplayable", "parse_not_enabled")


def test_pairing_contract_vod_id_only_confidence_capped():
    entry = {"vod_id": "123", "vod_name": "测试片", "vod_year": "2020",
             "vod_douban_id": "9999999",
             "vod_play_url": "第01集$https://cdn.example.com/ep1.mp4"}
    record = sync.cms_entry_to_record(entry, source_id="cms-1")
    assert record.upstream_item_id == "123"
    assert record.tmdb_id is None and record.imdb_id is None
    # 配对键只走标题+年份退化分支（无身份级 id），不读 vod_douban_id。
    key = agg.aggregate_key_for(record)
    assert "2020" in key
    assert sync.MATCH_EVIDENCE == "title+year"
    assert sync.MATCH_CONFIDENCE_CAP == 0.7
    assert sync._match_confidence() == 0.7


def test_gate_off_rejects_applecms_creation_but_not_emby(db, monkeypatch):
    monkeypatch.setenv("APPLECMS_ENABLED", "0")
    with pytest.raises(ups.UpstreamSourceError):
        ups.create_source(db, name="CMS 关闸", source_type="applecms",
                          base_url=PUBLIC_URL, auth_method="none")
    assert db.query(em.UpstreamSource).count() == 0
    # 关闸不影响旧类型：emby 照常可建。
    row = ups.create_source(db, name="旧 Emby", source_type="emby",
                            base_url="https://upstream.example.com:8096/",
                            auth_method="password", username="u",
                            secret="p")
    assert row.source_type == "emby" and row.enabled is False


def test_gate_on_allows_applecms_none_auth_creation(db, monkeypatch):
    monkeypatch.setenv("APPLECMS_ENABLED", "1")
    row = ups.create_source(db, name="CMS 开闸", source_type="applecms",
                            base_url=PUBLIC_URL, auth_method="none")
    assert row.source_type == "applecms"
    assert row.auth_method == "none"
    assert row.username is None and row.sealed_secret is None
    assert row.enabled is False
