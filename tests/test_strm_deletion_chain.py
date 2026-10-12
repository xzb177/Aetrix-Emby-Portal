"""Tests for the .strm deletion chain （Drive 删文件 → 删 .strm → 删库条目）.

覆盖 4 块：
1. 墓碑产出：DriveChangesLister removed/trashed → Tombstone；LocalFsLister 指纹 diff
2. 消费+预算+dry_run：_consume_tombstones / _prune_budget_ok / _apply_tombstone_deletions
3. 播放兜底：404→410、只认 404 不认 403、标记幂等
4. 僵尸报告：missing_report 扩展字段、orphan_drive 有界校验

隔离：全部用内存 SQLite + tmp_path，不碰生产数据、不发真实网络请求
（Drive API 部分用 monkeypatch 隔离）。
"""
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.database import Base
from backend.emby_server import models as em
from backend.emby_server import strm_gen as sg
from backend.emby_server import streaming as st
from backend.emby_server.strm_source import Tombstone


# ---------------------------------------------------------------------------
# 隔离基础设施：内存 DB + 配置桩
# ---------------------------------------------------------------------------

@pytest.fixture()
def memdb():
    """内存 SQLite：只建本链路需要的表（含新增的 strm_file_health / source_invalid_at 列）。

    注意：backend.database.Base 是多模块共享的 metadata，直接 create_all 会因
    未导入的模块（如 web_users）报 NoReferencedTableError，这里显式指定表清单。
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(
        engine,
        tables=[em.StrmGenFile.__table__, em.StrmFileHealth.__table__,
                em.MediaItem.__table__],
    )
    Session = sessionmaker(bind=engine)
    sess = Session()
    yield sess
    sess.close()
    engine.dispose()


@pytest.fixture()
def cfg(monkeypatch):
    """_db_config 桩：values 字典控制，未配置走函数默认值。"""
    values = {}

    def fake_db_config(db, key, default=""):
        return values.get(key, default)

    monkeypatch.setattr(sg, "_db_config", fake_db_config)
    return values


def _add_state_row(db, remote_path, file_id, strm_path="x.strm", size=100):
    db.add(em.StrmGenFile(remote_path=remote_path, file_id=file_id,
                          size=size, strm_path=strm_path))
    db.commit()


def _add_media_item(db, guid, drive_file_id, gone=False):
    from datetime import datetime
    item = em.MediaItem(guid=guid, library_id=1, item_type="movie", name="t",
                        drive_file_id=drive_file_id)
    if gone:
        item.source_invalid_at = datetime(2026, 10, 1)
    db.add(item)
    db.commit()
    return item


# ---------------------------------------------------------------------------
# 块1：墓碑产出
# ---------------------------------------------------------------------------

class TestDriveTombstones:
    def test_removed_and_trashed_yield_tombstone(self, monkeypatch):
        from backend.emby_server import drive_changes as dc
        from backend.emby_server import strm_source as ss

        changes = [
            {"fileId": "fid-removed", "removed": True},
            {"fileId": "fid-trashed",
             "file": {"trashed": True, "name": "x.mkv",
                      "mimeType": "video/mp4", "parents": []}},
            {"fileId": "fid-video",
             "file": {"name": "a.mkv", "mimeType": "video/mp4",
                      "parents": [], "size": "10"}},
        ]
        monkeypatch.setattr(dc, "list_changes", lambda tok, did: (changes, "tok2"))
        monkeypatch.setattr(dc, "_get_token", lambda: "at")

        lister = ss.DriveChangesLister("drive-1", "")
        monkeypatch.setattr(lister, "_get_saved_token", lambda: "tok")
        items = list(lister.iter_videos(incremental=True))

        videos, tombstones = sg._split_lister_output(items)
        assert videos == [("a.mkv", "fid-video", 10)]
        by_id = {t.file_id: t for t in tombstones}
        assert set(by_id) == {"fid-removed", "fid-trashed"}
        assert by_id["fid-removed"].reason == "removed"
        assert by_id["fid-trashed"].reason == "trashed"
        for t in tombstones:
            assert t.drive_id == "drive-1"
            assert t.detected_at  # ISO 时间戳

    def test_removed_does_not_call_file_meta(self, monkeypatch):
        """removed 事件不补查 _file_meta（file 为空，查也查不到，浪费配额）。"""
        from backend.emby_server import drive_changes as dc
        from backend.emby_server import strm_source as ss

        monkeypatch.setattr(dc, "list_changes",
                            lambda tok, did: ([{"fileId": "f1", "removed": True}], "t2"))
        monkeypatch.setattr(dc, "_get_token", lambda: "at")
        called = []
        monkeypatch.setattr(dc, "_file_meta",
                            lambda fid, tok: called.append(fid) or {"name": "x"})

        lister = ss.DriveChangesLister("drive-1", "")
        monkeypatch.setattr(lister, "_get_saved_token", lambda: "tok")
        items = list(lister.iter_videos(incremental=True))
        assert called == []
        assert len([i for i in items if isinstance(i, Tombstone)]) == 1


class TestLocalTombstones:
    def test_deleted_file_yields_tombstone(self, tmp_path, monkeypatch):
        from backend.emby_server import strm_source as ss

        # 磁盘上只有 keep.mkv；旧指纹里还有 gone.mkv（已被删）
        (tmp_path / "keep.mkv").write_bytes(b"0" * 10)

        lister = ss.LocalFsLister(str(tmp_path))
        gone_fid = "local:" + __import__("hashlib").sha256(
            "gone.mkv".encode()).hexdigest()[:32]
        old_fp = {"keep.mkv": [1, 10], "gone.mkv": [2, 10]}
        monkeypatch.setattr(lister, "_load_fp", lambda: dict(old_fp))
        monkeypatch.setattr(lister, "_save_fp", lambda fp: None)

        items = list(lister.iter_videos(incremental=True))
        videos, tombstones = sg._split_lister_output(items)
        assert len(tombstones) == 1
        t = tombstones[0]
        assert t.file_id == gone_fid
        assert t.drive_id == lister.drive_key
        assert t.reason == "local_deleted"

    def test_first_run_no_baseline_no_tombstone(self, tmp_path, monkeypatch):
        """首次运行 old_fp 为空 → 不产墓碑（否则全量当删除）。"""
        from backend.emby_server import strm_source as ss

        (tmp_path / "a.mkv").write_bytes(b"0" * 10)
        lister = ss.LocalFsLister(str(tmp_path))
        monkeypatch.setattr(lister, "_load_fp", lambda: {})
        monkeypatch.setattr(lister, "_save_fp", lambda fp: None)

        items = list(lister.iter_videos(incremental=True))
        _, tombstones = sg._split_lister_output(items)
        assert tombstones == []


class TestSplitListerOutput:
    def test_split(self):
        t = Tombstone(file_id="f", drive_id="d", reason="removed", detected_at="x")
        videos, tombstones = sg._split_lister_output([("a", "f1", 1), t, ("b", "f2", 2)])
        assert videos == [("a", "f1", 1), ("b", "f2", 2)]
        assert tombstones == [t]


# ---------------------------------------------------------------------------
# 块2：预算
# ---------------------------------------------------------------------------

class TestPruneBudget:
    def test_empty_state_allows(self, memdb, cfg):
        ok, reason = sg._prune_budget_ok(memdb, doomed_count=5, videos_seen=100)
        assert ok and reason == ""

    def test_zero_result_guard(self, memdb, cfg):
        for i in range(10):
            _add_state_row(memdb, f"r{i}", f"fid{i}")
        ok, reason = sg._prune_budget_ok(memdb, doomed_count=3, videos_seen=0)
        assert not ok
        assert "没列举" in reason

    def test_ratio_threshold(self, memdb, cfg):
        # total=100，ratio=0.2 → limit=max(50, 20)=50
        for i in range(100):
            _add_state_row(memdb, f"r{i}", f"fid{i}")
        ok, _ = sg._prune_budget_ok(memdb, doomed_count=49, videos_seen=100)
        assert ok
        ok, reason = sg._prune_budget_ok(memdb, doomed_count=50, videos_seen=100)
        assert not ok
        assert "50" in reason

    def test_absolute_floor_for_small_lib(self, memdb, cfg):
        # total=10 → limit=max(50, 2)=50，小库按 50 兜底
        for i in range(10):
            _add_state_row(memdb, f"r{i}", f"fid{i}")
        ok, _ = sg._prune_budget_ok(memdb, doomed_count=49, videos_seen=10)
        assert ok
        ok, _ = sg._prune_budget_ok(memdb, doomed_count=50, videos_seen=10)
        assert not ok

    def test_multi_disk_skip(self, memdb, cfg, tmp_path):
        """多盘：全部跳过，只计数上报，一条不删。"""
        _add_state_row(memdb, "r1", "fid1")
        sp = tmp_path / "x.strm"
        sp.write_text("u\n")
        stats: dict = {}
        state = {"r1": ("fid1", 100, "x.strm")}
        t = Tombstone(file_id="fid1", drive_id="d1", reason="removed", detected_at="x")
        sg._consume_tombstones(memdb, [t], state, str(tmp_path), stats,
                               ["d1", "d2"], videos_seen=10)
        assert stats.get("tombstone_skipped_multi") == 1
        assert sp.exists()  # 没删
        assert memdb.query(em.StrmGenFile).count() == 1

    def test_dedupe_by_drive_and_file(self, memdb, cfg, tmp_path):
        """同一 (drive_id, file_id) 去重，只删一次。"""
        cfg["strm_gen_prune_dry_run"] = "false"
        _add_state_row(memdb, "r1", "fid1")
        (tmp_path / "x.strm").write_text("u\n")
        stats: dict = {}
        state = {"r1": ("fid1", 100, "x.strm")}
        ts = [Tombstone(file_id="fid1", drive_id="d1", reason="removed", detected_at="x")
              for _ in range(3)]
        sg._consume_tombstones(memdb, ts, state, str(tmp_path), stats,
                               ["d1"], videos_seen=10)
        assert stats.get("tombstoned") == 1
        assert stats.get("tombstone_pending") == 1  # 去重后计数


# ---------------------------------------------------------------------------
# 块2：dry_run 与真删
# ---------------------------------------------------------------------------

class TestDryRun:
    def _setup(self, db, root):
        from backend.emby_server import models as em
        for i in range(2):
            _add_state_row(db, f"r{i}", f"fid{i}", strm_path=f"v{i}.strm")
            (root / f"v{i}.strm").write_text("https://x\n")
        return {"r0": ("fid0", 100, "v0.strm"), "r1": ("fid1", 100, "v1.strm")}

    def test_dry_run_only_records(self, memdb, cfg, tmp_path):
        cfg["strm_gen_prune_dry_run"] = "true"
        state = self._setup(memdb, tmp_path)
        stats: dict = {}
        ts = [Tombstone(file_id="fid0", drive_id="d1", reason="removed", detected_at="x")]
        sg._consume_tombstones(memdb, ts, state, str(tmp_path), stats,
                               ["d1"], videos_seen=10)
        wd = stats.get("prune_would_delete")
        assert wd and wd["count"] == 1
        assert wd["sample"][0]["remote_path"] == "r0"
        assert stats.get("tombstoned", 0) == 0
        # 磁盘和状态行都在
        assert (tmp_path / "v0.strm").exists()
        assert memdb.query(em.StrmGenFile).count() == 2

    def test_real_delete(self, memdb, cfg, tmp_path):
        cfg["strm_gen_prune_dry_run"] = "false"
        state = self._setup(memdb, tmp_path)
        stats: dict = {}
        ts = [Tombstone(file_id="fid0", drive_id="d1", reason="removed", detected_at="x")]
        sg._consume_tombstones(memdb, ts, state, str(tmp_path), stats,
                               ["d1"], videos_seen=10)
        memdb.commit()
        assert stats.get("tombstoned") == 1
        assert "prune_would_delete" not in stats
        assert not (tmp_path / "v0.strm").exists()
        assert (tmp_path / "v1.strm").exists()  # 没墓碑的不动
        assert memdb.query(em.StrmGenFile).count() == 1
        assert "r0" not in state

    def test_unknown_tombstone_recorded(self, memdb, cfg, tmp_path):
        """查不到状态行的墓碑：计数 + 记入 strm_file_health 供复核，不删。"""
        cfg["strm_gen_prune_dry_run"] = "false"
        stats: dict = {}
        ts = [Tombstone(file_id="fid-unknown", drive_id="d1",
                        reason="removed", detected_at="x")]
        sg._consume_tombstones(memdb, ts, {}, str(tmp_path), stats,
                               ["d1"], videos_seen=10)
        memdb.commit()
        assert stats.get("tombstone_unknown") == 1
        assert stats.get("tombstoned", 0) == 0
        row = memdb.query(em.StrmFileHealth).filter_by(
            file_id="fid-unknown").first()
        assert row is not None and row.status == "unknown"

    def test_budget_block_aborts_all(self, memdb, cfg, tmp_path):
        """超预算：整批中止，一条都不删，并记 prune_blocked。"""
        cfg["strm_gen_prune_dry_run"] = "false"
        for i in range(100):
            _add_state_row(memdb, f"r{i}", f"fid{i}", strm_path=f"v{i}.strm")
            (tmp_path / f"v{i}.strm").write_text("u\n")
        stats: dict = {}
        state = {f"r{i}": (f"fid{i}", 100, f"v{i}.strm") for i in range(100)}
        ts = [Tombstone(file_id=f"fid{i}", drive_id="d1",
                        reason="removed", detected_at="x") for i in range(60)]
        sg._consume_tombstones(memdb, ts, state, str(tmp_path), stats,
                               ["d1"], videos_seen=100)
        memdb.commit()
        assert stats.get("tombstone_blocked") == 60
        assert stats.get("tombstoned", 0) == 0
        blocked = stats.get("prune_blocked")
        assert blocked and blocked["doomed"] == 60 and blocked["reason"]
        assert memdb.query(em.StrmGenFile).count() == 100  # 一条没删
        assert (tmp_path / "v0.strm").exists()


# ---------------------------------------------------------------------------
# 块2：配置默认值
# ---------------------------------------------------------------------------

class TestConfigDefaults:
    def test_prune_defaults(self, cfg):
        assert sg.prune_enabled(None) is True
        assert sg.prune_dry_run(None) is True
        assert sg.prune_max_ratio(None) == 0.2
        assert sg.prune_max_absolute(None) == 50
        assert sg.verify_new_ids(None) is True

    def test_orphan_defaults(self, cfg):
        assert sg.orphan_check_enabled(None) is True
        assert sg.orphan_check_interval_days(None) == 7
        assert sg.orphan_check_batch(None) == 2000
        assert sg.orphan_check_qps(None) == 2.0

    def test_prune_explicit_false_kept(self, cfg):
        """存量显式 false 不被默认值覆盖。"""
        cfg["strm_gen_prune"] = "false"
        assert sg.prune_enabled(None) is False


# ---------------------------------------------------------------------------
# 块3：播放兜底
# ---------------------------------------------------------------------------

class TestDriveAltMediaFileId:
    def test_extract(self):
        assert st._drive_alt_media_file_id(
            "https://www.googleapis.com/drive/v3/files/abc123?alt=media") == "abc123"

    def test_extract_with_extra_query(self):
        assert st._drive_alt_media_file_id(
            "https://www.googleapis.com/drive/v3/files/abc123?alt=media&x=1") == "abc123"

    def test_non_drive_none(self):
        assert st._drive_alt_media_file_id("https://example.com/video.mp4") is None
        assert st._drive_alt_media_file_id(
            "https://drive.google.com/uc?export=download&id=abc") is None
        assert st._drive_alt_media_file_id("") is None
        assert st._drive_alt_media_file_id(None) is None


class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code
        self.headers = {}

    async def aclose(self):
        pass


class _FakeRelayClient:
    def __init__(self, status_code):
        self._status = status_code

    def build_request(self, method, url, headers=None):
        return (method, url)

    async def send(self, request, stream=True):
        return _FakeResp(self._status)


class TestServeRemote404:
    URL = "https://www.googleapis.com/drive/v3/files/fid-dead-1?alt=media"

    def _run(self, monkeypatch, status_code):
        from fastapi import HTTPException
        monkeypatch.setattr(st, "get_relay_client",
                            lambda: _FakeRelayClient(status_code))
        from backend.emby_server import drive_auth
        monkeypatch.setattr(drive_auth, "drive_api_media_url", lambda u: u)
        noted = []
        monkeypatch.setattr(st, "note_drive_file_gone", noted.append)
        request = types.SimpleNamespace(headers={})
        try:
            import asyncio
            asyncio.run(st.serve_remote_async(self.URL, request))
        except HTTPException as exc:
            return exc, noted
        return None, noted

    def test_404_to_410_and_marked(self, monkeypatch):
        exc, noted = self._run(monkeypatch, 404)
        assert exc is not None and exc.status_code == 410
        assert "删除或移出共享" in exc.detail
        assert noted == ["fid-dead-1"]

    def test_403_not_marked(self, monkeypatch):
        """只认 404，不认 403（403 可能是权限/限流，标记会误伤）。"""
        exc, noted = self._run(monkeypatch, 403)
        assert exc is not None and exc.status_code == 403
        assert noted == []

    def test_404_non_drive_passthrough(self, monkeypatch):
        """非 Drive URL 的 404：保持原行为（不抛 410、不标记）。"""
        from fastapi import HTTPException
        monkeypatch.setattr(st, "get_relay_client",
                            lambda: _FakeRelayClient(404))
        from backend.emby_server import drive_auth
        monkeypatch.setattr(drive_auth, "drive_api_media_url", lambda u: u)
        noted = []
        monkeypatch.setattr(st, "note_drive_file_gone", noted.append)
        request = types.SimpleNamespace(headers={})
        import asyncio
        try:
            asyncio.run(
                st.serve_remote_async("https://example.com/v.mp4", request))
            raised = None
        except HTTPException as exc:
            raised = exc
        assert raised is not None and raised.status_code == 404
        assert noted == []


class TestMarkSourceGone:
    def test_idempotent(self, memdb, monkeypatch):
        _add_media_item(memdb, "g1", "fid-gone-1")
        _add_media_item(memdb, "g2", "fid-other")
        monkeypatch.setattr("backend.database.SessionLocal", lambda: memdb)

        st._mark_source_gone_sync("fid-gone-1")
        row = memdb.query(em.MediaItem).filter_by(guid="g1").first()
        assert row.source_invalid_at is not None
        first = row.source_invalid_at

        # 第二次：幂等，不报错、不改值
        st._mark_source_gone_sync("fid-gone-1")
        row = memdb.query(em.MediaItem).filter_by(guid="g1").first()
        assert row.source_invalid_at == first

        other = memdb.query(em.MediaItem).filter_by(guid="g2").first()
        assert other.source_invalid_at is None

    def test_clear_gone_marks(self, memdb, monkeypatch):
        _add_media_item(memdb, "g1", "fid-back", gone=True)
        _add_media_item(memdb, "g2", "fid-still-gone", gone=True)
        monkeypatch.setattr("backend.database.SessionLocal", lambda: memdb)

        sg._clear_gone_marks(["fid-back", "local:should-be-ignored", ""])
        assert memdb.query(em.MediaItem).filter_by(guid="g1").first() \
            .source_invalid_at is None
        assert memdb.query(em.MediaItem).filter_by(guid="g2").first() \
            .source_invalid_at is not None


# ---------------------------------------------------------------------------
# 块4：僵尸报告
# ---------------------------------------------------------------------------

class TestMissingReportExt:
    def test_extended_fields(self, memdb, cfg, monkeypatch):
        from backend.integrations import store
        from datetime import datetime

        stats = {
            "verify": {
                "total_series": 5, "complete_series": 4, "incomplete_series": 1,
                "incomplete": [],
                "orphan_disk": {"count": 2, "sample": [
                    {"remote_path": "r1", "strm_path": "s1", "file_id": "f1"}]},
            },
            "tombstone_pending": 7,
            "prune_blocked": {"reason": "r", "doomed": 60, "at": "t"},
            "prune_would_delete": {"count": 3, "sample": []},
        }
        monkeypatch.setattr(store, "get_value",
                            lambda db, k, d="": json.dumps(stats) if k == sg.CONFIG_LAST_STATS else d)
        memdb.add(em.StrmFileHealth(file_id="gone1", drive_id="d1", status="gone",
                                    checked_at=datetime(2026, 10, 11)))
        memdb.commit()

        rep = sg.missing_report(memdb)
        assert rep["ok"] and rep["has_data"]
        assert rep["orphan_disk"]["count"] == 2
        assert rep["orphan_drive"]["count"] == 1
        assert rep["orphan_drive"]["sample"][0]["file_id"] == "gone1"
        assert rep["orphan_drive"]["checked_at"] is not None
        assert rep["tombstone_pending"] == 7
        assert rep["prune_blocked"]["doomed"] == 60
        assert rep["prune_would_delete"]["count"] == 3

    def test_old_stats_backward_compat(self, memdb, cfg, monkeypatch):
        """老统计（无新字段）不炸，给默认值。"""
        from backend.integrations import store
        stats = {"verify": {"total_series": 1, "complete_series": 1,
                           "incomplete_series": 0, "incomplete": []}}
        monkeypatch.setattr(store, "get_value",
                            lambda db, k, d="": json.dumps(stats) if k == sg.CONFIG_LAST_STATS else d)
        rep = sg.missing_report(memdb)
        assert rep["ok"]
        assert rep["orphan_disk"] == {"count": 0, "sample": []}
        assert rep["orphan_drive"]["count"] == 0
        assert rep["tombstone_pending"] == 0
        assert rep["prune_blocked"] is None


class TestOrphanDriveCheck:
    def test_bounded_check(self, memdb, cfg, monkeypatch):
        from backend.emby_server import drive_changes as dc
        from backend.emby_server import models as em

        cfg["strm_orphan_check_qps"] = "10"  # 测试提速
        _add_media_item(memdb, "g1", "fid-gone", gone=True)
        _add_media_item(memdb, "g2", "fid-alive", gone=True)

        def fake_api_get(url, token, params):
            if "fid-gone" in url:
                raise dc.DriveNotFoundError("gone")
            if "fid-trashed" in url:
                return {"id": "fid-trashed", "trashed": True}
            if "fid-boom" in url:
                raise RuntimeError("boom")
            return {"id": "x", "trashed": False}

        monkeypatch.setattr(dc, "_get_token", lambda: "tok")
        monkeypatch.setattr(dc, "_api_get", fake_api_get)
        memdb.add(em.StrmFileHealth(file_id="fid-trashed", drive_id="d1",
                                    status="unknown"))
        memdb.add(em.StrmFileHealth(file_id="fid-boom", drive_id="d1",
                                    status="unknown"))
        memdb.commit()
        # _consume_tombstones 需要 state/strm_root：给空实现，避免真删
        monkeypatch.setattr(sg, "_load_state", lambda: {})
        monkeypatch.setattr(sg, "_container_strm_root", lambda db: "/tmp")

        result = sg.run_orphan_drive_check(memdb)
        assert result["ok"] is True
        assert result["checked"] == 4
        assert result["gone"] == 2      # fid-gone（404）+ fid-trashed
        assert result["alive"] == 1     # fid-alive → 复活清标记
        assert result["error"] == 1    # fid-boom

        # gone 落 health 表
        gone_row = memdb.query(em.StrmFileHealth).filter_by(
            file_id="fid-gone").first()
        assert gone_row.status == "gone"
        # alive 的清掉播放标记
        assert memdb.query(em.MediaItem).filter_by(guid="g2").first() \
            .source_invalid_at is None
        # gone 的保留标记（等墓碑消费/dry_run 流程处理）
        assert memdb.query(em.MediaItem).filter_by(guid="g1").first() \
            .source_invalid_at is not None

    def test_no_token_skips(self, memdb, cfg, monkeypatch):
        from backend.emby_server import drive_changes as dc
        _add_media_item(memdb, "g1", "fid-gone", gone=True)
        monkeypatch.setattr(dc, "_get_token", lambda: None)
        result = sg.run_orphan_drive_check(memdb)
        assert result["ok"] is False
        assert result["checked"] == 0

    def test_models(self):
        """新表模型字段齐全。"""
        from backend.emby_server.models import StrmFileHealth
        cols = {c.key for c in StrmFileHealth.__table__.columns}
        assert {"file_id", "drive_id", "status", "checked_at", "note"} <= cols
        assert [c.key for c in StrmFileHealth.__table__.primary_key.columns] == ["file_id"]
