# -*- coding: utf-8 -*-
"""直链为主线（2026-10-27 用户改令）：变体清单与换线决策的直链优先口径。

钉死：
- 双开关都开时清单第一条恒为 drive_direct，中继列在之后作兜底；
- 中继关时清单只有直链一条，单条不是清单失效——准备中态标注仍对
  单条生效（PlaybackInfo 对单条同样覆盖，见 api.playback_info）；
- 换线决策双可用时首选直链；直链不可用时首选才落到中继；双不可用
  时首选 None；显式选了中继但清单里没有时按 409 纪律明文报错，
  不静默改走直链。

全部离线：DB 用内存 SQLite，开关读数走 monkeypatch（与
test_play_variants.py 的 store 桩同口径）。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend.emby_server import models as em
from backend.emby_server import play_variants
from backend.emby_server import playback_strategy, variant_switch

FID = "1eXUpWksxfCCrIZUye8lo24JGXyr_2ZQO"


@pytest.fixture()
def db_factory(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False},
        poolclass=StaticPool)
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr("backend.database.SessionLocal", factory)
    return factory


@pytest.fixture()
def db(db_factory):
    session = db_factory()
    yield session
    session.close()


def _drive_item(guid):
    item = em.MediaItem()
    item.guid = guid
    item.name = "测试片"
    item.file_path = "mount://3/电影/测试片.mkv"
    item.drive_file_id = FID
    item.item_type = "movie"
    item.container = "mkv"
    return item


def _stub_switches(monkeypatch, *, dd="true", relay="true"):
    real_get = playback_strategy.store.get_value

    def fake_get(db_, key, default="", **kwargs):
        if key == playback_strategy.KEY_DRIVE_DIRECT_PLAY:
            return dd
        if key == playback_strategy.KEY_DRIVE_RELAY:
            return relay
        return real_get(db_, key, default, **kwargs)

    monkeypatch.setattr(playback_strategy.store, "get_value", fake_get)


def _set_availability(db, key, status):
    row = em.VariantAvailability(
        variant_key=key, availability_status=status,
        consecutive_failures=3 if status == "unavailable" else 0)
    db.add(row)
    db.commit()


def test_dual_on_lists_direct_first(db, monkeypatch):
    _stub_switches(monkeypatch)
    names = [v.name for v in play_variants.list_variants(
        db, _drive_item("g-dual"), drive_mount_locatable=True)]
    assert names == ["drive_direct", "drive_relay"]


def test_relay_off_lists_direct_only(db, monkeypatch):
    _stub_switches(monkeypatch, relay="false")
    names = [v.name for v in play_variants.list_variants(
        db, _drive_item("g-norelay"), drive_mount_locatable=True)]
    assert names == ["drive_direct"]


def test_route_choice_both_available_direct_first(db):
    first, avail, unavail = variant_switch.drive_route_choice(db)
    assert first == ("drive_direct", None)
    assert avail == [("drive_direct", None), ("drive_relay", None)]
    assert unavail == []


def test_route_choice_direct_unavailable_falls_to_relay(db):
    _set_availability(db, "drive_direct", "unavailable")
    first, avail, unavail = variant_switch.drive_route_choice(db)
    assert first == ("drive_relay", None)
    assert avail == [("drive_relay", None)]
    assert unavail == [("drive_direct", None)]


def test_route_choice_all_unavailable_none(db):
    _set_availability(db, "drive_direct", "unavailable")
    _set_availability(db, "drive_relay", "unavailable")
    first, avail, unavail = variant_switch.drive_route_choice(db)
    assert first is None
    assert avail == []
    assert unavail == [("drive_direct", None), ("drive_relay", None)]


def test_single_direct_source_preparing_annotation():
    # PlaybackInfo 单变体时下发单源（Id 不带变体后缀）；准备中态标注
    # 必须同样生效，不能只对多源清单生效。
    sources = [{
        "Id": "g-single",
        "DirectPlayUrl": "https://example.test/stream",
        "DirectStreamUrl": "https://example.test/stream",
        "SupportsDirectPlay": True,
        "SupportsDirectStream": True,
    }]
    play_variants.apply_transfer_status(sources, "preparing")
    assert sources[0]["TransferStatus"] == "preparing"
    assert sources[0]["SupportsDirectPlay"] is False
    assert sources[0]["SupportsDirectStream"] is False
    assert "DirectPlayUrl" not in sources[0]
    assert "DirectStreamUrl" not in sources[0]


def test_single_direct_source_ready_unchanged():
    sources = [{
        "Id": "g-single",
        "DirectPlayUrl": "https://example.test/stream",
        "SupportsDirectPlay": True,
    }]
    play_variants.apply_transfer_status(sources, None)
    assert sources[0]["DirectPlayUrl"] == "https://example.test/stream"
    assert sources[0]["SupportsDirectPlay"] is True
    assert "TransferStatus" not in sources[0]


def test_explicit_relay_when_only_direct_available_is_409():
    assert play_variants.resolve_requested_variant(
        {"variant": "drive_relay"}, ["drive_direct"]) == (None, 409)
    assert play_variants.resolve_requested_variant(
        {"variant": "drive_direct"}, ["drive_direct"]) == ("drive_direct", None)
