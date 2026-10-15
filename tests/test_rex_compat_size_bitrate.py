"""Rex 兼容修复测试：size 对齐账本 + 码率推导兜底。

覆盖：
- api._effective_bitrate：探测值优先；缺失时 size×8÷duration_ticks 推导；
  size 或时长缺一个就不推导（返回 None，不编数）。
- scanner._sync_video_size_from_ledger：纸条口径 .strm 条目从账本取视频口径
  size 回填；账本无/纸条口径/已是视频口径/容器行/非 .strm 均不动。
- scripts/backfill_strm_video_size：dry-run 口径（join 键与筛选条件）。
"""
import importlib.util
import os
import sys
import tempfile
import types

import pytest
from sqlalchemy import create_engine, func
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend import models as core  # noqa: F401  确保 Base 元数据已注册
from backend.database import Base
from backend.emby_server import api as emby_api
from backend.emby_server import models as em
from backend.emby_server import scanner

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


class TestSyncVideoSizeFromLedger:
    def _ledger(self, db, strm_path, size, drive_id="d1", remote_path=None):
        db.add(em.StrmGenFile(
            drive_id=drive_id,
            remote_path=remote_path or strm_path.replace(".strm", ".mkv"),
            file_id="f1", size=size, strm_path=strm_path))
        db.commit()

    def test_stub_size_synced_from_ledger(self, db):
        self._ledger(db, "电影/外语电影/xx.strm", 4_800_000_000)
        it = _item(item_type="movie", size=145,
                   file_path="/strm/电影/外语电影/xx.strm")
        db.add(it)
        db.commit()
        assert scanner._sync_video_size_from_ledger(db, it) is True
        assert it.size == 4_800_000_000

    def test_video_caliber_not_touched(self, db):
        self._ledger(db, "电影/外语电影/yy.strm", 4_800_000_000)
        it = _item(item_type="movie", size=4_700_000_000,
                   file_path="/strm/电影/外语电影/yy.strm")
        db.add(it)
        db.commit()
        assert scanner._sync_video_size_from_ledger(db, it) is False
        assert it.size == 4_700_000_000

    def test_no_ledger_row_keeps_status_quo(self, db):
        it = _item(item_type="episode", size=200,
                   file_path="/strm/剧集/国产剧/zz.strm")
        db.add(it)
        db.commit()
        assert scanner._sync_video_size_from_ledger(db, it) is False
        assert it.size == 200

    def test_ledger_stub_size_not_written_back(self, db):
        # 账本里也只有纸条口径 → 不拿纸条大小乱填，保持现状
        self._ledger(db, "电影/外语电影/ww.strm", 300)
        it = _item(item_type="movie", size=145,
                   file_path="/strm/电影/外语电影/ww.strm")
        db.add(it)
        db.commit()
        assert scanner._sync_video_size_from_ledger(db, it) is False
        assert it.size == 145

    def test_container_rows_not_touched(self, db):
        self._ledger(db, "剧集/国产剧/xx.strm", 4_800_000_000)
        it = _item(item_type="series", size=0, file_path="/strm/剧集/国产剧/xx.strm")
        db.add(it)
        db.commit()
        assert scanner._sync_video_size_from_ledger(db, it) is False
        assert it.size == 0

    def test_non_strm_not_touched(self, db):
        it = _item(item_type="movie", size=123,
                   file_path="/media/电影/xx.mkv")
        db.add(it)
        db.commit()
        assert scanner._sync_video_size_from_ledger(db, it) is False
        assert it.size == 123

    def test_null_size_synced(self, db):
        self._ledger(db, "电影/外语电影/nn.strm", 2_000_000_000)
        it = _item(item_type="movie", size=None,
                   file_path="/strm/电影/外语电影/nn.strm")
        db.add(it)
        db.commit()
        assert scanner._sync_video_size_from_ledger(db, it) is True
        assert it.size == 2_000_000_000


def _load_backfill_script():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts",
                        "backfill_strm_video_size.py")
    spec = importlib.util.spec_from_file_location("backfill_strm_video_size", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestBackfillScriptCaliber:
    """回填脚本的口径：join 键与筛选条件（dry-run 口径与 scanner helper 一致）"""

    def _seed(self, db, mod):
        # 纸条口径 + 账本有视频口径 → 应回填
        db.add(em.MediaItem(guid="A" * 32, library_id=1, item_type="movie",
                            name="甲", file_path="/strm/电影/甲.strm", size=145))
        # 纸条口径 + 账本无此文件 → 跳过
        db.add(em.MediaItem(guid="B" * 32, library_id=1, item_type="movie",
                            name="乙", file_path="/strm/电影/乙.strm", size=200))
        # 已是视频口径 → 不在候选
        db.add(em.MediaItem(guid="C" * 32, library_id=1, item_type="movie",
                            name="丙", file_path="/strm/电影/丙.strm",
                            size=4_800_000_000))
        # 容器行 → 不在候选
        db.add(em.MediaItem(guid="D" * 32, library_id=1, item_type="series",
                            name="丁", file_path="/strm/剧集/丁.strm", size=0))
        # 合并副本 → 不在候选
        db.add(em.MediaItem(guid="E" * 32, library_id=1, item_type="movie",
                            name="戊", file_path="/strm/电影/戊.strm", size=100,
                            merged_into_id=1))
        db.add(em.StrmGenFile(drive_id="d1", remote_path="电影/甲.mkv",
                              file_id="f1", size=4_800_000_000,
                              strm_path="电影/甲.strm"))
        db.commit()

    def test_candidate_selection(self, db):
        mod = _load_backfill_script()
        self._seed(db, mod)
        rel = func.substring(em.MediaItem.file_path, 7)
        ledger_exists = (
            em.StrmGenFile.__table__.select()
            .where(em.StrmGenFile.strm_path == rel)
            .where(em.StrmGenFile.size > mod._STUB_MAX)
            .exists()
        )
        rows = (
            db.query(em.MediaItem.guid)
            .filter(em.MediaItem.item_type.in_(["movie", "episode"]))
            .filter(em.MediaItem.merged_into_id.is_(None))
            .filter(em.MediaItem.file_path.like("%.strm"))
            .filter(mod._stub_caliber())
            .filter(ledger_exists)
            .all()
        )
        assert [r[0] for r in rows] == ["A" * 32]

    def test_ledger_size_map(self, db):
        mod = _load_backfill_script()
        self._seed(db, mod)
        m = mod._ledger_size_map(db, ["电影/甲.strm", "电影/乙.strm"])
        assert m == {"电影/甲.strm": 4_800_000_000}

    def test_stub_max_constant(self):
        mod = _load_backfill_script()
        assert mod._STUB_MAX == 64 * 1024
