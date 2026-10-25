"""扫描触发源标签归一化回归测试。

背景（2026-10-14 enrich 大翻转事故调查）：
``SCAN_TRIGGERS`` 白名单长期只有 ("manual","client","node","repair")，
``normalize_scan_trigger`` 把白名单之外的一切真触发源
（chase-new / drive-changes / strm-gen / auto / fs-event / rescrape）
全部改写成 "manual" 落库——过去 14 天 222 个 scan run 在 DB 里全是 manual，
复盘时被误读为"有人在点扫描"，差点把事故归因算到用户头上。

修复：白名单补全为代码里真实存在的 12 个触发值；
未知值仍回退 "manual"（原设计意图保留）。

本文件三层覆盖：
1. normalize 单函数：12 个真值原样保留、未知值回退 manual；
2. 端到端落库：enqueue（真函数）→ ScanTask 保留真值 →
   begin_scan（真函数）→ ScanRun 行的 trigger_kind 与传入一致；
3. 未知值端到端：队列透传原值、落库时回退 manual（设计语义）。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.emby_server import models as emby_models
from backend.emby_server import scan_queue
from backend.emby_server import scanner


KNOWN_TRIGGERS = (
    "manual", "client", "node", "repair",
    "auto", "fs-event", "drive-changes", "chase-new", "strm-gen", "rescrape",
    "resume", "byos_binding",
)


class TestNormalizeScanTrigger:
    def test_whitelist_matches_real_world_triggers(self):
        """白名单必须包含代码里真实存在的所有触发值（少一个就会重演事故）。"""
        assert set(scanner.SCAN_TRIGGERS) == set(KNOWN_TRIGGERS)

    @pytest.mark.parametrize("trigger", KNOWN_TRIGGERS)
    def test_known_triggers_preserved(self, trigger):
        """每个真实触发值经归一化必须原样保留。"""
        assert scanner.normalize_scan_trigger(trigger) == trigger

    @pytest.mark.parametrize("trigger", KNOWN_TRIGGERS)
    def test_known_triggers_case_and_whitespace_tolerant(self, trigger):
        """大小写/首尾空白不该改变归因（normalize 本来就做 strip+lower）。"""
        assert scanner.normalize_scan_trigger("  %s  " % trigger.upper()) == trigger

    @pytest.mark.parametrize("trigger", ["bogus", "MANUAL2", "scan", "chase_new",
                                         "drivechanges", "auto-scan"])
    def test_unknown_triggers_fall_back_to_manual(self, trigger):
        """未知值回退 manual：调用方写错触发名也不能丢扫描流水。"""
        assert scanner.normalize_scan_trigger(trigger) == "manual"

    @pytest.mark.parametrize("trigger", [None, "", "   "])
    def test_empty_triggers_fall_back_to_manual(self, trigger):
        assert scanner.normalize_scan_trigger(trigger) == "manual"


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    emby_models.Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add(emby_models.Library(id=77, guid="test-lib-trigger-77",
                                    name="触发标签库", paths="", mount_ids=""))
    session.commit()
    try:
        yield session
    finally:
        session.close()


def _fake_library(lib_id: int):
    return SimpleNamespace(
        id=lib_id, name="触发标签库", paths="", mount_ids="",
        collection_type=None, scrape_policy=None,
    )


class TestEnqueueToDbTrigger:
    """端到端：enqueue → begin_scan 落库，trigger_kind 与传入一致。

    #841 的教训：只测 normalize 函数本身等于没测——必须走真实落库路径。
    """

    @pytest.mark.parametrize("trigger", KNOWN_TRIGGERS)
    def test_enqueue_to_begin_scan_keeps_trigger(self, db, trigger):
        lib_id = 770 + KNOWN_TRIGGERS.index(trigger)
        db.add(emby_models.Library(id=lib_id, guid="test-lib-%d" % lib_id,
                                   name="库%d" % lib_id, paths="", mount_ids=""))
        db.commit()
        scan_queue._QUEUE.clear()

        # 第一段链路：入队（真函数），ScanTask 必须原样保留触发值
        result = scan_queue.enqueue(_fake_library(lib_id), trigger=trigger)
        assert result["created"] is True
        assert result["task"]["trigger"] == trigger

        # 第二段链路：begin_scan（真函数）落库，ScanRun 触发值与传入一致
        lib = db.query(emby_models.Library).filter_by(id=lib_id).one()
        run_id = scanner.begin_scan(db, lib, trigger=result["task"]["trigger"])
        assert run_id is not None
        run = db.query(emby_models.ScanRun).filter_by(id=run_id).one()
        assert run.trigger == trigger

    def test_unknown_trigger_falls_back_to_manual_in_db(self, db):
        """未知触发值：队列透传原值（不改队列语义），落库时归一化为 manual。"""
        scan_queue._QUEUE.clear()
        result = scan_queue.enqueue(_fake_library(77), trigger="bogus")
        assert result["task"]["trigger"] == "bogus"

        lib = db.query(emby_models.Library).filter_by(id=77).one()
        run_id = scanner.begin_scan(db, lib, trigger=result["task"]["trigger"])
        run = db.query(emby_models.ScanRun).filter_by(id=run_id).one()
        assert run.trigger == "manual"

    def test_begin_scan_default_is_manual(self, db):
        """默认参数不传 trigger 时落库 manual（调用方省略时的老语义）。"""
        lib = db.query(emby_models.Library).filter_by(id=77).one()
        run_id = scanner.begin_scan(db, lib)
        run = db.query(emby_models.ScanRun).filter_by(id=run_id).one()
        assert run.trigger == "manual"
