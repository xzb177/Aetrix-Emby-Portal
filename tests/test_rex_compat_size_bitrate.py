"""Rex 兼容修复测试：码率推导兜底。

注（2026-10-17）：strm_gen 删除，scanner._sync_video_size_from_ledger 与
scripts/backfill_strm_video_size.py 同步删除，相关测试类同步删除。

覆盖：
- api._effective_bitrate：探测值优先；缺失时 size×8÷duration_ticks 推导；
  size 或时长缺一个就不推导（返回 None，不编数）。
"""
import os
import sys
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.database import Base
from backend.emby_server import api as emby_api
from backend.emby_server import models as em

_STUB_MAX = 64 * 1024


@pytest.fixture
def db():
    path = os.path.join(tempfile.mkdtemp(), "rex-compat.db")
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        yield s


def _item(**kw):
    d = dict(guid="G" * 32, library_id=1, item_type="movie", name="片")
    d.update(kw)
    return em.MediaItem(**d)


class TestEffectiveBitrate:
    def test_probed_wins(self):
        it = _item(bitrate=5605723, size=4_800_000_000, duration_ticks=68_543_360_000)
        assert emby_api._effective_bitrate(it) == 5605723

    def test_derived_from_size_and_duration(self):
        size, ticks = 4_800_000_000, 68_543_360_000
        it = _item(bitrate=None, size=size, duration_ticks=ticks)
        assert emby_api._effective_bitrate(it) == size * 8 * 10_000_000 // ticks

    def test_zero_bitrate_treated_as_missing(self):
        size, ticks = 1_000_000_000, 60_000_000_000
        it = _item(bitrate=0, size=size, duration_ticks=ticks)
        assert emby_api._effective_bitrate(it) == size * 8 * 10_000_000 // ticks

    def test_no_derive_without_size(self):
        it = _item(bitrate=None, size=None, duration_ticks=68_543_360_000)
        assert emby_api._effective_bitrate(it) is None

    def test_no_derive_without_duration(self):
        it = _item(bitrate=None, size=4_800_000_000, duration_ticks=0)
        assert emby_api._effective_bitrate(it) is None

    def test_no_derive_neither(self):
        it = _item(bitrate=None, size=None, duration_ticks=None)
        assert emby_api._effective_bitrate(it) is None
