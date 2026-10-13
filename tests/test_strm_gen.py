"""Tests for backend/emby_server/strm_gen.py — .strm 生成器内置版。

覆盖：路径映射、视频判定、冲突解决、配置归一化、完整性校验。
不碰网络（Drive API 部分用 monkeypatch 隔离）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.emby_server import strm_gen as sg


class TestStrmRelpath:
    def test_strip_prefix(self):
        assert sg.strm_relpath("MoviePilot/剧集/xx.mkv", "MoviePilot/") == "剧集/xx.strm"

    def test_no_prefix(self):
        assert sg.strm_relpath("电影/xx.mp4", "") == "电影/xx.strm"

    def test_prefix_not_matched(self):
        # 前缀不匹配时保留原路径
        assert sg.strm_relpath("Other/xx.mkv", "MoviePilot/") == "Other/xx.strm"

    def test_nested(self):
        assert sg.strm_relpath(
            "MoviePilot/剧集/国产剧/惊枝 (2026)/Season 1/惊枝 - S01E01.mkv",
            "MoviePilot/",
        ) == "剧集/国产剧/惊枝 (2026)/Season 1/惊枝 - S01E01.strm"


class TestIsVideoPath:
    def test_video(self):
        assert sg.is_video_path("MoviePilot/剧集/xx.mkv")
        assert sg.is_video_path("MoviePilot/电影/xx.MP4")  # 大小写

    def test_not_video(self):
        assert not sg.is_video_path("MoviePilot/剧集/xx.nfo")
        assert not sg.is_video_path("MoviePilot/剧集/xx.jpg")

    def test_skip_hidden(self):
        assert not sg.is_video_path("MoviePilot/.hidden/xx.mkv")
        assert not sg.is_video_path("MoviePilot/剧集/.DS_Store.mkv")

    def test_skip_system_dirs(self):
        assert not sg.is_video_path("MoviePilot/剧集/@eaDir/xx.mkv")
        assert not sg.is_video_path("MoviePilot/剧集/#recycle/xx.mkv")

    def test_all_supported_exts(self):
        for ext in sg.VIDEO_EXTS:
            assert sg.is_video_path(f"a/b{ext}"), ext


class TestDriveUrl:
    def test_format(self):
        url = sg.drive_url("1QALUKEAwNgo1J8y2EipIDTcVDwIrpX_y")
        assert url == ("https://drive.google.com/uc?export=download"
                       "&id=1QALUKEAwNgo1J8y2EipIDTcVDwIrpX_y&confirm=t")


class TestResolveCollision:
    def test_no_collision(self):
        assert sg._resolve_collision("a/b.strm", set()) == "a/b.strm"

    def test_collision(self):
        used = {"a/b.strm"}
        assert sg._resolve_collision("a/b.strm", used) == "a/b-2.strm"

    def test_multiple(self):
        used = {"a/b.strm", "a/b-2.strm"}
        assert sg._resolve_collision("a/b.strm", used) == "a/b-3.strm"


class TestVerifyCompleteness:
    def _videos(self):
        # (rel, fid, size)
        return [
            ("MoviePilot/剧集/A/Season 1/A - S01E01.mkv", "f1", 100),
            ("MoviePilot/剧集/A/Season 1/A - S01E02.mkv", "f2", 100),
            ("MoviePilot/剧集/A/Season 1/A - S01E03.mkv", "f3", 100),
            ("MoviePilot/剧集/B/Season 1/B - S01E01.mkv", "f4", 100),
        ]

    def _make_state_on_disk(self, videos, tmp_path, skip=()):
        # state: {rel: (fid, size, spath)}，并在 tmp_path 下创建真实 .strm 文件
        import os
        state = {}
        for rel, fid, size in videos:
            if any(s in rel for s in skip):
                continue
            spath = rel.replace("MoviePilot/", "").replace(".mkv", ".strm")
            full = os.path.join(str(tmp_path), spath)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write("x")
            state[rel] = (fid, size, spath)
        return state

    def test_all_complete(self, tmp_path):
        videos = self._videos()
        state = self._make_state_on_disk(videos, tmp_path)
        result = sg._verify_completeness(videos, state, "MoviePilot/", str(tmp_path))
        assert result["total_series"] == 2  # A/Season 1, B/Season 1
        assert result["incomplete_series"] == 0

    def test_missing_detected(self, tmp_path):
        videos = self._videos()
        # state 缺了 A 的 E03
        state = self._make_state_on_disk(videos, tmp_path, skip=("E03",))
        result = sg._verify_completeness(videos, state, "MoviePilot/", str(tmp_path))
        assert result["incomplete_series"] == 1
        inc = result["incomplete"][0]
        assert inc["series"] == "剧集/A/Season 1"
        assert inc["missing_count"] == 1
        assert any("E03" in m for m in inc["missing_sample"])


    def test_missing_file_on_disk_counts_as_missing(self, tmp_path):
        # DB 有行但 .strm 文件不在磁盘 → 计为缺失（P3-2）
        videos = self._videos()
        state = self._make_state_on_disk(videos, tmp_path)
        # 删掉一个 .strm 文件
        import os
        gone = os.path.join(str(tmp_path), "剧集/A/Season 1/A - S01E01.strm")
        os.remove(gone)
        result = sg._verify_completeness(videos, state, "MoviePilot/", str(tmp_path))
        assert result["incomplete_series"] == 1
        inc = result["incomplete"][0]
        assert inc["series"] == "剧集/A/Season 1"
        assert inc["missing_count"] == 1


class TestSchedule:
    def test_source_dir(self):
        assert sg.source_dir(_FakeDb({"strm_gen_source_dir": "MoviePilot/"})) == "MoviePilot/"
        assert sg.source_dir(_FakeDb({"strm_gen_source_dir": ""})) == ""
        assert sg.source_dir(_FakeDb({})) == "MoviePilot/"  # 默认

    def test_enabled(self):
        assert sg.enabled(_FakeDb({"strm_gen_enabled": "true"}))
        assert not sg.enabled(_FakeDb({"strm_gen_enabled": "false"}))
        assert sg.enabled(_FakeDb({}))  # 默认 true


class _FakeDb:
    """最小 store.get_value 替身。"""

    def __init__(self, values: dict):
        self._values = values


# 给 _db_config 打桩：strm_gen._db_config(db, key, default)
@pytest.fixture(autouse=True)
def _patch_db_config(monkeypatch):
    def fake_db_config(db, key, default=""):
        if isinstance(db, _FakeDb):
            return db._values.get(key, default)
        return default
    monkeypatch.setattr(sg, "_db_config", fake_db_config)


class TestStrmGenModel:
    """P0-1：StrmGenFile 表必须存在（原来引用了但没建表，状态读写静默失败）。"""

    def test_table_exists(self):
        from backend.emby_server.models import StrmGenFile
        assert StrmGenFile.__tablename__ == "strm_gen_files"
        cols = {c.key for c in StrmGenFile.__table__.columns}
        assert {"remote_path", "file_id", "size", "strm_path", "updated_at"} <= cols

    def test_remote_path_is_pk(self):
        from backend.emby_server.models import StrmGenFile
        pks = [c.key for c in StrmGenFile.__table__.primary_key.columns]
        assert pks == ["remote_path"]


class TestRoutesRegistered:
    """P0-3：5 个 API 路由必须挂在 admin_emby_router 上（原来从未被 import）。"""

    def test_five_routes(self):
        import backend.api.admin_strm_gen  # noqa: F401  # 触发装饰器注册
        from backend.emby_server.portal import admin_emby_router
        by_path: dict[str, set[str]] = {}
        for r in admin_emby_router.routes:
            p = getattr(r, "path", "")
            if "strm-gen" in p:
                by_path.setdefault(p, set()).update(getattr(r, "methods", None) or set())
        assert by_path.get("/api/admin/emby/strm-gen/config", set()) >= {"GET", "PUT"}
        assert by_path.get("/api/admin/emby/strm-gen/trigger", set()) >= {"POST"}
        assert by_path.get("/api/admin/emby/strm-gen/progress", set()) >= {"GET"}
        assert by_path.get("/api/admin/emby/strm-gen/missing", set()) >= {"GET"}


class TestRunControl:
    """P3-6：运行权原子获取，不再有触发竞态。"""

    def test_try_acquire(self):
        sg._set_progress(running=False, phase="idle")
        try:
            assert sg.try_acquire() is True
            assert sg.try_acquire() is False  # 第二次拿不到
        finally:
            sg._set_progress(running=False, phase="idle")
        assert sg.try_acquire() is True
        sg._set_progress(running=False, phase="idle")

    def test_run_generation_rejects_when_running(self):
        sg._set_progress(running=True, phase="generating")
        try:
            res = sg.run_generation(None)
            assert res["ok"] is False
            assert "运行中" in res["error"]
        finally:
            sg._set_progress(running=False, phase="idle")

    def test_start_scheduler_idempotent(self, monkeypatch):
        # 不真正起线程：只验证幂等标记逻辑
        import threading
        monkeypatch.setattr(sg, "_scheduler_started", True)
        before = threading.active_count()
        assert sg.start_scheduler() is True
        assert threading.active_count() == before  # 没起新线程
        monkeypatch.setattr(sg, "_scheduler_started", False)


class TestSafeRelpath:
    """P3-7：输出路径防目录穿越。"""

    def test_dotdot_stripped(self):
        assert sg._safe_relpath("a/../../b") == "a/b"
        assert sg._safe_relpath("../x") == "x"

    def test_strm_relpath_sanitized(self):
        assert sg.strm_relpath("MoviePilot/../x.mkv", "MoviePilot/") == "x.strm"


class TestPageFetcher:
    """P1-1：列举中途 401 自动刷新 token 续跑。"""

    def test_refresh_on_401(self, monkeypatch):
        calls = []
        tokens = ["old", "new"]
        monkeypatch.setattr(sg, "_get_token", lambda: tokens.pop(0))
        def fake_list_page(token, drive_id, query, page_token):
            calls.append(token)
            if token == "old":
                raise PermissionError("Drive token 无效或过期")
            return ([{"id": "1"}], None)
        monkeypatch.setattr(sg, "_list_files_page", fake_list_page)
        fetch = sg._page_fetcher("d1")
        files, _ = fetch("q", None)
        assert files == [{"id": "1"}]
        assert calls == ["old", "new"]

    def test_reraise_when_refresh_fails(self, monkeypatch):
        monkeypatch.setattr(sg, "_get_token", lambda: "same")
        def fake_list_page(token, drive_id, query, page_token):
            raise PermissionError("Drive token 无效或过期")
        monkeypatch.setattr(sg, "_list_files_page", fake_list_page)
        fetch = sg._page_fetcher("d1")
        import pytest as _pytest
        with _pytest.raises(PermissionError):
            fetch("q", None)


class TestVerifyDisk:
    """P3-2：完整性校验同时验磁盘真实存在。"""

    def test_missing_on_disk_counts_as_incomplete(self, tmp_path):
        videos = [
            ("MoviePilot/剧集/A/Season 1/A - S01E01.mkv", "f1", 100),
            ("MoviePilot/剧集/A/Season 1/A - S01E02.mkv", "f2", 100),
        ]
        # DB 有行，但磁盘上一个文件都没有
        state = {rel: (fid, size, rel.replace(".mkv", ".strm").replace("MoviePilot/", ""))
                 for rel, fid, size in videos}
        result = sg._verify_completeness(videos, state, "MoviePilot/", strm_root=str(tmp_path))
        assert result["incomplete_series"] == 1
        assert result["incomplete"][0]["strm_count"] == 0

    def test_present_on_disk_counts_as_complete(self, tmp_path):
        videos = [("MoviePilot/剧集/A/Season 1/A - S01E01.mkv", "f1", 100)]
        spath = "剧集/A/Season 1/A - S01E01.strm"
        (tmp_path / "剧集/A/Season 1").mkdir(parents=True)
        (tmp_path / spath).write_text("x")
        state = {videos[0][0]: ("f1", 100, spath)}
        result = sg._verify_completeness(videos, state, "MoviePilot/", strm_root=str(tmp_path))
        assert result["incomplete_series"] == 0


class TestRunGenerationMocked:
    """核心流程 mock 测试（Drive/DB/落盘全部隔离）。"""

    def _patch_all(self, tmp_path, monkeypatch, videos, state=None, drives=None):
        monkeypatch.setattr(sg, "iter_drive_videos",
                            lambda drive_id, dir_path, strict=True, **kwargs: iter(list(videos)))
        # 新并行列举直接调 DriveChangesLister.iter_videos（worker 线程内）：
        # 让它走 mocked 的 sg.iter_drive_videos
        import backend.emby_server.strm_source as ssrc

        def fake_lister_iter(self, incremental=True):
            return sg.iter_drive_videos(self.drive_id, self.dir_path)

        monkeypatch.setattr(ssrc.DriveChangesLister, "iter_videos", fake_lister_iter)
        monkeypatch.setattr(ssrc.DriveChangesLister, "save_checkpoint", lambda self: None)
        monkeypatch.setattr(sg, "_discover_drives",
                            lambda: drives if drives is not None else {"d1": ["sa1"]})
        monkeypatch.setattr(sg, "_container_strm_root", lambda db: str(tmp_path))
        monkeypatch.setattr(sg, "_load_state", lambda: dict(state or {}))
        saved: dict = {}
        monkeypatch.setattr(sg, "_save_state_rows",
                            lambda rows: saved.update({r[0]: r for r in rows}))
        deleted: list = []
        monkeypatch.setattr(sg, "_delete_state_rows",
                            lambda rows, db=None: deleted.extend(rows))
        # _release_progress 里真实的 store.set_value 打桩掉
        import backend.integrations.store as _store
        monkeypatch.setattr(_store, "write_values", lambda *a, **k: 0)
        return saved, deleted

    def test_full_run_creates_strm(self, tmp_path, monkeypatch):
        videos = [
            ("MoviePilot/剧集/A/Season 1/A - S01E01.mkv", "fid1", 100),
            ("MoviePilot/剧集/A/Season 1/A - S01E02.mkv", "fid2", 200),
        ]
        self._patch_all(tmp_path, monkeypatch, videos)
        try:
            res = sg.run_generation(_FakeDb({}), full=False)
        finally:
            sg._set_progress(running=False, phase="idle")
        assert res["ok"] is True
        assert res["stats"]["generated"] == 2
        p = tmp_path / "剧集/A/Season 1/A - S01E01.strm"
        assert p.exists()
        assert "fid1" in p.read_text()

    def test_incremental_skips_unchanged(self, tmp_path, monkeypatch):
        videos = [("MoviePilot/剧集/A/Season 1/A - S01E01.mkv", "fid1", 100)]
        state = {"MoviePilot/剧集/A/Season 1/A - S01E01.mkv":
                 ("fid1", 100, "剧集/A/Season 1/A - S01E01.strm")}
        (tmp_path / "剧集/A/Season 1").mkdir(parents=True)
        (tmp_path / "剧集/A/Season 1/A - S01E01.strm").write_text("old")
        self._patch_all(tmp_path, monkeypatch, videos, state=state)
        try:
            res = sg.run_generation(_FakeDb({}), full=False)
        finally:
            sg._set_progress(running=False, phase="idle")
        assert res["stats"]["skipped"] == 1
        assert res["stats"]["generated"] == 0

    def test_size_change_rebuilds(self, tmp_path, monkeypatch):
        """P3-9：size 变化也触发重建（原来只比 file_id）。"""
        videos = [("MoviePilot/剧集/A/Season 1/A - S01E01.mkv", "fid1", 999)]
        state = {"MoviePilot/剧集/A/Season 1/A - S01E01.mkv":
                 ("fid1", 100, "剧集/A/Season 1/A - S01E01.strm")}
        (tmp_path / "剧集/A/Season 1").mkdir(parents=True)
        (tmp_path / "剧集/A/Season 1/A - S01E01.strm").write_text("old")
        self._patch_all(tmp_path, monkeypatch, videos, state=state)
        try:
            res = sg.run_generation(_FakeDb({}), full=False)
        finally:
            sg._set_progress(running=False, phase="idle")
        assert res["stats"]["generated"] == 1
        assert res["stats"]["skipped"] == 0

    def test_multi_drive(self, tmp_path, monkeypatch):
        """P1-2：不指定 drive 时遍历所有共享盘（原来只跑第一个）。"""
        def fake_iter(drive_id, dir_path, strict=True, **kwargs):
            return iter([(f"MoviePilot/{drive_id}/a.mkv", f"fid-{drive_id}", 10)])
        monkeypatch.setattr(sg, "iter_drive_videos", fake_iter)
        monkeypatch.setattr(sg, "_discover_drives", lambda: {"d1": ["s"], "d2": ["s"]})
        monkeypatch.setattr(sg, "_container_strm_root", lambda db: str(tmp_path))
        monkeypatch.setattr(sg, "_load_state", lambda: {})
        monkeypatch.setattr(sg, "_save_state_rows", lambda rows: None)
        import backend.integrations.store as _store
        monkeypatch.setattr(_store, "write_values", lambda *a, **k: 0)
        try:
            res = sg.run_generation(_FakeDb({}), full=False)
        finally:
            sg._set_progress(running=False, phase="idle")
        assert res["ok"] is True
        assert res["stats"]["generated"] == 2
        assert (tmp_path / "d1/a.strm").exists()
        assert (tmp_path / "d2/a.strm").exists()

    def test_full_run_generates(self, tmp_path, monkeypatch):
        """回归：全量模式必须真的生成（曾因生成循环缩进落在 else 分支里，
        full=True 时空转一轮、一个文件都不生成）。"""
        videos = [("MoviePilot/剧集/A/a.mkv", "fid1", 100)]
        self._patch_all(tmp_path, monkeypatch, videos)
        try:
            res = sg.run_generation(_FakeDb({}), full=True)
        finally:
            sg._set_progress(running=False, phase="idle")
        assert res["ok"] is True
        assert res["stats"]["generated"] == 1
        assert (tmp_path / "剧集/A/a.strm").exists()

    def test_prune_removes_stale(self, tmp_path, monkeypatch):
        """P2-3（新语义）：全量 + dry_run 关闭时，删除全量列举没见到的过期条目。
        增量不再做全量 stale 对比（增量列举只有 delta，会误杀；删除信号走墓碑）。"""
        videos = [("MoviePilot/剧集/A/a.mkv", "fid1", 100)]
        state = {
            "MoviePilot/剧集/A/a.mkv": ("fid1", 100, "剧集/A/a.strm"),
            "MoviePilot/剧集/OLD/old.mkv": ("fidOld", 100, "剧集/OLD/old.strm"),
        }
        (tmp_path / "剧集/A").mkdir(parents=True)
        (tmp_path / "剧集/A/a.strm").write_text("x")
        (tmp_path / "剧集/OLD").mkdir(parents=True)
        (tmp_path / "剧集/OLD/old.strm").write_text("x")
        _saved, deleted = self._patch_all(tmp_path, monkeypatch, videos, state=state)
        try:
            res = sg.run_generation(
                _FakeDb({"strm_gen_prune": "true",
                         "strm_gen_prune_dry_run": "false"}), full=True)
        finally:
            sg._set_progress(running=False, phase="idle")
        assert res["ok"] is True
        assert res["stats"]["pruned"] == 1
        assert not (tmp_path / "剧集/OLD/old.strm").exists()
        assert (tmp_path / "剧集/A/a.strm").exists()
        assert deleted == ["MoviePilot/剧集/OLD/old.mkv"]

    def test_prune_dry_run_by_default(self, tmp_path, monkeypatch):
        """默认 dry_run=true：只记 would-delete，不真删。"""
        videos = [("MoviePilot/剧集/A/a.mkv", "fid1", 100)]
        state = {"MoviePilot/剧集/OLD/old.mkv": ("fidOld", 100, "剧集/OLD/old.strm")}
        (tmp_path / "剧集/OLD").mkdir(parents=True)
        (tmp_path / "剧集/OLD/old.strm").write_text("x")
        _saved, deleted = self._patch_all(tmp_path, monkeypatch, videos, state=state)
        try:
            res = sg.run_generation(_FakeDb({}), full=True)
        finally:
            sg._set_progress(running=False, phase="idle")
        assert res["stats"]["pruned"] == 0
        assert (tmp_path / "剧集/OLD/old.strm").exists()
        assert deleted == []
        wd = res["stats"].get("prune_would_delete")
        assert wd and wd["count"] == 1

    def test_prune_incremental_stale_not_compared(self, tmp_path, monkeypatch):
        """增量不做全量 stale 对比：state 里有但 delta 里没有的条目不动。"""
        videos = [("MoviePilot/剧集/A/a.mkv", "fid1", 100)]
        state = {"MoviePilot/剧集/OLD/old.mkv": ("fidOld", 100, "剧集/OLD/old.strm")}
        (tmp_path / "剧集/OLD").mkdir(parents=True)
        (tmp_path / "剧集/OLD/old.strm").write_text("x")
        _saved, deleted = self._patch_all(tmp_path, monkeypatch, videos, state=state)
        try:
            res = sg.run_generation(
                _FakeDb({"strm_gen_prune": "true",
                         "strm_gen_prune_dry_run": "false"}), full=False)
        finally:
            sg._set_progress(running=False, phase="idle")
        assert res["stats"]["pruned"] == 0
        assert (tmp_path / "剧集/OLD/old.strm").exists()
        assert deleted == []


class TestListDrives:
    """list_drives 按 drive_id 排序展示。"""

    def test_list_drives_sorted(self, monkeypatch):
        monkeypatch.setattr(sg, "_discover_drives",
                            lambda: {"zz": ["r1"], "aa": ["r2"]})

        class FakeDc:
            @staticmethod
            def _is_personal_drive_key(did):
                return did.startswith("myDrive:")

        monkeypatch.setattr(sg, "_drive_modules", lambda: FakeDc())
        drives = sg.list_drives()
        assert [d["drive_id"] for d in drives] == ["aa", "zz"]


class TestSafeName:
    """P3-7：文件名清理。"""

    def test_strips_dotdot(self):
        assert sg._safe_name("..") == ""
        assert sg._safe_name("../a") == "a"


class TestResolveDirIdPagination:
    """P2-1：_resolve_dir_id 应翻页查找（fetch 闭包版）。"""

    def test_finds_on_second_page(self):
        calls = {"n": 0}

        def fetch(query, page_token=None):
            calls["n"] += 1
            # 第一页没命中但有下一页；目标在第二页
            if page_token is None:
                return ([], "p2")
            return ([{"id": "dir123", "name": "MoviePilot", "trashed": False}], None)

        got = sg._resolve_dir_id(fetch, "d1", "MoviePilot/")
        assert got == "dir123"
        assert calls["n"] == 2

    def test_returns_none_when_not_found(self):
        def fetch(query, page_token=None):
            return ([], None)

        assert sg._resolve_dir_id(fetch, "d1", "不存在的目录/") is None


def _flat_drive_fixture():
    """构造一个共享盘 d1 的扁平条目：目录树 MoviePilot/剧集[/Season 1] + 干扰项。"""
    F = "application/vnd.google-apps.folder"
    return [
        {"id": "f_mp", "name": "MoviePilot", "mimeType": F, "parents": ["d1"]},
        {"id": "f_tv", "name": "剧集", "mimeType": F, "parents": ["f_mp"]},
        {"id": "f_s1", "name": "Season 1", "mimeType": F, "parents": ["f_tv"]},
        {"id": "f_h", "name": ".hidden", "mimeType": F, "parents": ["f_mp"]},
        {"id": "v1", "name": "a.mkv", "mimeType": "video/x-matroska",
         "size": "100", "parents": ["f_tv"]},
        {"id": "v2", "name": "b.mp4", "mimeType": "video/mp4",
         "size": "200", "parents": ["f_s1"]},
        {"id": "v3", "name": "c.txt", "mimeType": "text/plain",
         "size": "10", "parents": ["f_s1"]},
        {"id": "v4", "name": "d.mkv", "mimeType": "video/x-matroska",
         "size": "50", "parents": ["f_h"]},
        {"id": "v5", "name": "t.mkv", "mimeType": "video/x-matroska",
         "size": "60", "parents": ["f_tv"], "trashed": True},
        {"id": "v9", "name": "other.mkv", "mimeType": "video/x-matroska",
         "size": "70", "parents": ["d1"]},
    ]


def _make_fake_fetcher(items, page_size=2):
    """fake _page_fetcher：目录解析走 name 查询，扁平拉取走分页。"""
    calls = {"n": 0}

    def fetch(query, page_token=None):
        calls["n"] += 1
        if "mimeType='application/vnd.google-apps.folder'" in query:
            # _resolve_dir_id 的按名查找
            hit = [f for f in items
                   if f.get("mimeType") == "application/vnd.google-apps.folder"
                   and "name='MoviePilot'" in query and f["name"] == "MoviePilot"]
            return (hit, None)
        # 扁平拉取：按 page_size 分页
        start = int(page_token) if page_token else 0
        page = items[start:start + page_size]
        nxt = str(start + page_size) if start + page_size < len(items) else None
        return (page, nxt)

    fetch.calls = calls
    return fetch


class TestIterDriveVideosFlat:
    """扁平拉取 + 本地建树（旧路径，subtree_listing=False 显式指定）。

    S1 之后默认走子树限定列举；这里锁定旧路径的行为不回归。"""

    def _patch(self, monkeypatch, items, page_size=2):
        fake_fetch = _make_fake_fetcher(items, page_size)
        monkeypatch.setattr(sg, "_page_fetcher", lambda drive_id: fake_fetch)
        return fake_fetch

    def test_yields_videos_with_source_prefix(self, monkeypatch):
        self._patch(monkeypatch, _flat_drive_fixture())
        got = sorted(sg.iter_drive_videos("d1", "MoviePilot/", subtree_listing=False))
        assert got == [
            ("MoviePilot/剧集/Season 1/b.mp4", "v2", 200),
            ("MoviePilot/剧集/a.mkv", "v1", 100),
        ]

    def test_excludes_outside_source_dir(self, monkeypatch):
        # v9 在 d1 根（源目录之外）、c.txt 非视频、v4 在隐藏目录、
        # v5 已删除——都不应出现
        self._patch(monkeypatch, _flat_drive_fixture())
        ids = {fid for _rel, fid, _size in sg.iter_drive_videos("d1", "MoviePilot/", subtree_listing=False)}
        assert ids == {"v1", "v2"}

    def test_source_dir_not_found_raises(self, monkeypatch):
        self._patch(monkeypatch, _flat_drive_fixture())
        import pytest as _pytest
        with _pytest.raises(RuntimeError, match="找不到源目录"):
            list(sg.iter_drive_videos("d1", "不存在的目录/"))

    def test_progress_updated_during_flat_pull(self, monkeypatch):
        self._patch(monkeypatch, _flat_drive_fixture(), page_size=2)
        seen = []
        monkeypatch.setattr(sg, "_set_progress",
                            lambda **kw: seen.append(kw.get("total")))
        list(sg.iter_drive_videos("d1", "MoviePilot/", subtree_listing=False))
        # 10 条 / 每页 2 条 = 5 次进度汇报，且单调递增
        assert len(seen) == 5
        assert seen == sorted(seen) and seen[-1] == 10

    def test_single_flat_query_not_per_folder(self, monkeypatch):
        # 扁平拉取：API 调用次数 = 目录解析(1) + 分页数，不随目录数增长
        fake_fetch = self._patch(monkeypatch, _flat_drive_fixture(), page_size=5)
        list(sg.iter_drive_videos("d1", "MoviePilot/", subtree_listing=False))
        # 1（目录解析）+ 2（10 条 / 每页 5 条）
        assert fake_fetch.calls["n"] == 3

    def test_root_dir_shared_drive(self, monkeypatch):
        self._patch(monkeypatch, _flat_drive_fixture())
        got = sorted(sg.iter_drive_videos("d1", "", subtree_listing=False))
        rels = [r for r, _f, _s in got]
        # 网盘根：所有视频都列出，rel 不带源目录前缀剥离（根即无前缀）
        assert "other.mkv" in rels
        assert "MoviePilot/剧集/a.mkv" in rels
        assert "MoviePilot/剧集/Season 1/b.mp4" in rels



class TestWriteStrmFileTmpRace:
    def test_tmp_name_unique_per_writer(self, monkeypatch, tmp_path):
        """_write_strm_file 的 tmp 文件名必须带 pid+线程 id：worker 的签名刷新
        也可能并发写同一文件，确定性的 "<file>.tmp" 会交错写入撕裂内容。"""
        import os as _os

        seen = []
        real_replace = _os.replace

        def fake_replace(src, dst):
            seen.append(src)
            return real_replace(src, dst)

        monkeypatch.setattr(sg.os, "replace", fake_replace)
        target = tmp_path / "v.strm"
        assert sg._write_strm_file(str(target), "https://drive.example.com/uc?id=ABC")
        assert seen, "expected a tmp write"
        assert seen[0] != str(target) + ".tmp", "tmp 仍是确定性命名，跨进程会撞车"
        assert f".tmp.{_os.getpid()}." in seen[0], f"tmp 缺少 pid 后缀: {seen[0]}"
        assert target.read_text(encoding="utf-8") == "https://drive.example.com/uc?id=ABC\n"
        # 成功路径也不留 tmp
        assert [p for p in tmp_path.iterdir() if ".tmp." in p.name] == []

    def test_tmp_cleaned_on_replace_failure(self, monkeypatch, tmp_path):
        """replace 失败时自己的 tmp 必须被清理，不留残片。"""
        import os as _os

        def fake_replace(src, dst):
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(sg.os, "replace", fake_replace)
        target = tmp_path / "v.strm"
        assert sg._write_strm_file(str(target), "https://drive.example.com/uc?id=ABC") is False
        assert [p for p in tmp_path.iterdir() if ".tmp." in p.name] == []


class TestGetProgressBackfill:
    """get_progress(db) 的 DB 回填：进程重启后（内存进度为初始值）应返回
    DB 里上次执行的真实统计，而不是全 0。

    注：模块级 autouse fixture `_patch_db_config` 已把 `sg._db_config` 打桩，
    配合 `_FakeDb(values)` 使用即可，无需再 patch store。
    """

    @pytest.fixture(autouse=True)
    def _reset_progress(self):
        import copy

        original = copy.deepcopy(sg._progress)
        with sg._lock:
            sg._progress.clear()
            sg._progress.update({
                "running": False,
                "started_at": None,
                "finished_at": None,
                "phase": "idle",
                "total": 0,
                "done": 0,
                "generated": 0,
                "skipped": 0,
                "failed": 0,
                "errors": [],
                "current": "",
            })
        yield
        with sg._lock:
            sg._progress.clear()
            sg._progress.update(original)

    def _last_run_db(self):
        return _FakeDb({
            sg.CONFIG_LAST_STATS: ('{"generated": 120, "skipped": 8000, "failed": 3,'
                                   ' "pruned": 5, "total": 8123}'),
            sg.CONFIG_LAST_RUN: "2026-10-12T03:00:00+00:00",
        })

    def test_backfill_after_restart(self):
        """模拟进程重启：内存进度为初始值，调 get_progress(db) 应回填 DB 统计。"""
        prog = sg.get_progress(db=self._last_run_db())
        assert prog["phase"] == "done"
        assert prog["generated"] == 120
        assert prog["skipped"] == 8000
        assert prog["failed"] == 3
        assert prog["total"] == 8123
        assert prog["finished_at"] == "2026-10-12T03:00:00+00:00"
        assert prog["done"] == 120 + 8000 + 3

        # 回填结果已缓存进内存：无 db 的调用也返回同样值（前端轮询不每次查 DB）
        cached = sg.get_progress()
        assert cached["phase"] == "done"
        assert cached["generated"] == 120
        assert cached["skipped"] == 8000
        assert cached["failed"] == 3
        assert cached["total"] == 8123
        assert cached["finished_at"] == "2026-10-12T03:00:00+00:00"
        assert cached["done"] == 120 + 8000 + 3

    def test_no_backfill_when_running(self):
        """正在运行的任务进度绝不能被回填覆盖。"""
        with sg._lock:
            sg._progress.update({
                "running": True,
                "phase": "generating",
                "started_at": "2026-10-12T03:00:00+00:00",
                "generated": 7,
            })

        prog = sg.get_progress(db=self._last_run_db())
        assert prog["running"] is True
        assert prog["phase"] == "generating"
        assert prog["generated"] == 7

    def test_no_backfill_when_done_in_memory(self):
        """内存里已有上次运行结果（phase=done）时，不用 DB 回填覆盖。"""
        with sg._lock:
            sg._progress.update({
                "phase": "done",
                "started_at": "2026-10-11T03:00:00+00:00",
                "finished_at": "2026-10-11T04:00:00+00:00",
                "generated": 50,
                "total": 100,
            })

        prog = sg.get_progress(db=self._last_run_db())
        assert prog["phase"] == "done"
        assert prog["generated"] == 50  # 内存值保留，不被 DB 的 120 覆盖
        assert prog["finished_at"] == "2026-10-11T04:00:00+00:00"

    def test_degrade_on_db_error(self, monkeypatch):
        """DB 读异常时降级返回 idle，不抛异常。"""
        def boom(db, key, default=""):
            raise RuntimeError("db down")

        monkeypatch.setattr(sg, "_db_config", boom)

        prog = sg.get_progress(db=object())
        assert prog["phase"] == "idle"
        assert prog["running"] is False
        assert prog["total"] == 0
        assert prog["generated"] == 0
        assert prog["skipped"] == 0
        assert prog["failed"] == 0
        assert prog["done"] == 0

    def test_no_stats_in_db(self):
        """DB 里没有上次统计（全新安装）时返回 idle 全 0。"""
        prog = sg.get_progress(db=_FakeDb({}))
        assert prog["phase"] == "idle"
        assert prog["running"] is False
        assert prog["total"] == 0
        assert prog["generated"] == 0
        assert prog["skipped"] == 0
        assert prog["failed"] == 0
        assert prog["done"] == 0

    def test_malformed_stats_json(self):
        """统计 JSON 损坏时降级返回 idle，不抛异常。"""
        db = _FakeDb({sg.CONFIG_LAST_STATS: "{not-json"})
        prog = sg.get_progress(db=db)
        assert prog["phase"] == "idle"
        assert prog["total"] == 0


class TestIterDriveVideosStrict:
    """iter_drive_videos strict 参数：源目录缺失时的行为。"""

    def test_strict_true_raises(self, monkeypatch):
        """strict=True（默认）：源目录不存在 → 抛 RuntimeError。"""
        import backend.emby_server.strm_gen as sg2
        monkeypatch.setattr(sg2, "_page_fetcher", lambda did: lambda q, pt=None: ([], None))
        monkeypatch.setattr(sg2, "_resolve_dir_id", lambda fetch, did, path: None)
        with pytest.raises(RuntimeError, match="找不到源目录"):
            list(sg2.iter_drive_videos("drive123", "MoviePilot/"))

    def test_strict_false_skips_gracefully(self, monkeypatch):
        """strict=False：源目录不存在 → warning + 返回空，不抛异常。"""
        import backend.emby_server.strm_gen as sg2
        monkeypatch.setattr(sg2, "_page_fetcher", lambda did: lambda q, pt=None: ([], None))
        monkeypatch.setattr(sg2, "_resolve_dir_id", lambda fetch, did, path: None)
        result = list(sg2.iter_drive_videos("drive123", "MoviePilot/", strict=False))
        assert result == []

    def test_empty_dir_path_no_check(self, monkeypatch):
        """dir_path 为空（网盘根）时不检查目录，直接走列举。"""
        import backend.emby_server.strm_gen as sg2
        called = {}
        def fake_fetch(q, pt=None):
            called["q"] = q
            return ([], None)
        monkeypatch.setattr(sg2, "_page_fetcher", lambda did: fake_fetch)
        # _resolve_dir_id 对空路径返回 None，但空 dir_path 不触发 strict 检查
        result = list(sg2.iter_drive_videos("drive123", "", strict=True, subtree_listing=False))
        assert result == []
        assert called.get("q") == "trashed=false"


class TestPerDriveFailureIsolation:
    """单盘失败隔离：一个盘失败不拖死整轮（2026-10-12 修复）。"""

    def _patch(self, tmp_path, monkeypatch, drive_results):
        """drive_results: {did: videos|Exception}"""
        def fake_iter(drive_id, dir_path, strict=True, **kwargs):
            r = drive_results[drive_id]
            if isinstance(r, Exception):
                raise r
            return iter(r)
        monkeypatch.setattr(sg, "iter_drive_videos", fake_iter)
        # 注意：DriveChangesLister 会调 _get_saved_token（真实 DB），这里直接
        # mock 掉 lister 让它走 fake_iter
        import backend.emby_server.strm_source as ssrc
        orig_init = ssrc.DriveChangesLister.__init__
        def patched_init(self, drive_id, dir_path, strict_dir=True, **kwargs):
            orig_init(self, drive_id, dir_path, strict_dir=strict_dir, **kwargs)
        monkeypatch.setattr(ssrc.DriveChangesLister, "__init__", patched_init)
        # 让 lister.iter_videos 直接调 mocked 的 sg.iter_drive_videos
        def fake_lister_iter(self, incremental=True):
            return sg.iter_drive_videos(self.drive_id, self.dir_path,
                                        strict=self.strict_dir)
        monkeypatch.setattr(ssrc.DriveChangesLister, "iter_videos", fake_lister_iter)
        monkeypatch.setattr(ssrc.DriveChangesLister, "_get_saved_token", lambda self: "tok")
        monkeypatch.setattr(ssrc.DriveChangesLister, "save_checkpoint", lambda self: None)
        import backend.emby_server.drive_changes as dc
        monkeypatch.setattr(dc, "list_changes", lambda token, did: ([], "newtok"))
        monkeypatch.setattr(sg, "_discover_drives",
                            lambda: {did: ["s"] for did in drive_results})
        monkeypatch.setattr(sg, "_container_strm_root", lambda db: str(tmp_path))
        monkeypatch.setattr(sg, "_load_state", lambda: {})
        monkeypatch.setattr(sg, "_save_state_rows", lambda rows: None)
        monkeypatch.setattr(sg, "_delete_state_rows", lambda rows: None)
        import backend.integrations.store as _store
        monkeypatch.setattr(_store, "write_values", lambda *a, **k: 0)
        monkeypatch.setattr(_store, "get_value", lambda *a, **k: "")

    def test_one_drive_fails_others_succeed(self, tmp_path, monkeypatch):
        """一个盘 403，另一个盘正常 → 整轮成功，失败盘被跳过。"""
        self._patch(tmp_path, monkeypatch, {
            "good": [("MoviePilot/a.mkv", "fid1", 100)],
            "bad": RuntimeError("403 Forbidden"),
        })
        try:
            res = sg.run_generation(_FakeDb({}), full=False)
        finally:
            sg._set_progress(running=False, phase="idle")
        assert res["ok"] is True
        assert res["stats"]["generated"] == 1
        assert res["stats"]["sources"]["bad"]["error"] is not None
        assert res["stats"]["sources"]["good"]["error"] is None

    def test_all_drives_fail(self, tmp_path, monkeypatch):
        """所有盘都失败 → 整轮标记失败。"""
        self._patch(tmp_path, monkeypatch, {
            "bad1": RuntimeError("403 Forbidden"),
            "bad2": RuntimeError("404 Not Found"),
        })
        try:
            res = sg.run_generation(_FakeDb({}), full=False)
        finally:
            sg._set_progress(running=False, phase="idle")
        assert res["ok"] is False
        assert "列举失败" in res["error"]
