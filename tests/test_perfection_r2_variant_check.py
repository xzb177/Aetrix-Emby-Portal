# -*- coding: utf-8 -*-
"""后端完美审查第二轮第 4 项：变体校验收紧。

钉住的口径（只改校验传参，不改清单语义、不改门禁、不改首播口径）：
- PlaybackInfo 下发清单时已把 ``_mount_can_generate_play_target``
  传给 ``list_variants``（api.py 清单调用点）；取流侧
  ``_available_variant_names``（video_stream 校验用）此前漏传该参数，
  默认 None 使 mount_relay 恒进可用名，清单扣掉的变体在取流侧
  仍能通过 resolve（绕过窗口；下游 _play_target 409 兜底仍在，
  收紧只关窗口，不影响合法播放）。
- 本文件只证明取流侧与清单逐字一致：能力 False 的挂载，
  ``_available_variant_names`` 不含 mount_relay，显式选它按
  ``resolve_requested_variant`` 现成语义报 409；能力 True 的挂载
  仍含 mount_relay，显式选它正常通过。

全部离线：FakeDB 只回挂载行，不碰真库、不碰 Drive、不碰外网。
健康总闸用 monkeypatch 钉 True（与健康门隔离，健康门另有覆盖）。
"""
import os
from types import SimpleNamespace

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from backend.emby_server import api, play_variants
from backend.emby_server import playback_source_health as _psh


class _FakeQuery:
    def __init__(self, row=None):
        self._row = row

    def filter(self, *args, **kwargs):
        return self

    def first(self):
        return self._row


class _FakeDB:
    def __init__(self, row=None):
        self._row = row

    def query(self, *args, **kwargs):
        return _FakeQuery(self._row)


def _mount_item():
    return SimpleNamespace(guid="g-mount", drive_file_id="",
                           file_path="mount://3/x.mkv", library_id=None)


def _user():
    return SimpleNamespace(id=11)


def test_stream_side_excludes_mount_relay_when_capability_false(monkeypatch):
    """未知挂载类型能力全 False：取流侧可用名不含 mount_relay，选它报 409。"""
    monkeypatch.setattr(_psh, "item_source_available",
                        lambda db, item: True)
    # 未知类型 → mount_sources.capabilities_for_mount_type 全 False，
    # 走真实 _mount_can_generate_play_target，不桩参数本身。
    db = _FakeDB(SimpleNamespace(id=3, mount_type="bogus-unknown"))
    names = api._available_variant_names(db, _mount_item(), _user())
    assert names == []
    assert play_variants.resolve_requested_variant(
        {"variant": "mount_relay"}, names) == (None, 409)


def test_stream_side_keeps_mount_relay_when_capability_true(monkeypatch):
    """drive 挂载能力 True：取流侧可用名仍含 mount_relay，选它正常通过。"""
    monkeypatch.setattr(_psh, "item_source_available",
                        lambda db, item: True)
    db = _FakeDB(SimpleNamespace(id=3, mount_type="drive"))
    names = api._available_variant_names(db, _mount_item(), _user())
    assert names == ["mount_relay"]
    assert play_variants.resolve_requested_variant(
        {"variant": "mount_relay"}, names) == ("mount_relay", None)
