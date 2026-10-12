"""strm 生成器通用增量能力测试。

覆盖：
1. SourceLister 接口契约（防风控：JITTER_RATIO/RATE_LIMIT_DESC 必须定义）
2. LocalFsLister 增量检测（mtime+size 指纹）
3. interval_minutes / jitter_ratio 配置解析
4. _jittered_interval_sec 抖动范围
5. _trigger_strm_scan 只推 /strm 库
"""
import json
import os
import tempfile
import time

import pytest


class TestSourceListerContract:
    """接口契约：每种实现必须落实防风控（用户原话：每次挂载方式都要防风控）。"""

    def test_drive_lister_has_antibot(self):
        from backend.emby_server.strm_source import DriveChangesLister
        assert 0 < DriveChangesLister.JITTER_RATIO <= 0.5
        assert DriveChangesLister.RATE_LIMIT_DESC != "未定义"

    def test_local_lister_has_antibot(self):
        from backend.emby_server.strm_source import LocalFsLister
        assert 0 < LocalFsLister.JITTER_RATIO <= 0.5
        assert LocalFsLister.RATE_LIMIT_DESC != "未定义"

    def test_get_lister_unknown_type(self):
        from backend.emby_server.strm_source import get_lister
        with pytest.raises(ValueError):
            get_lister("rclone")


class TestLocalFsLister:
    """本地文件指纹增量检测。"""

    def _make_files(self, root, files):
        for rel, content in files.items():
            full = os.path.join(root, rel)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write(content)

    def test_full_lists_all_videos(self):
        from backend.emby_server.strm_source import LocalFsLister
        with tempfile.TemporaryDirectory() as root:
            self._make_files(root, {
                "a.mp4": "x" * 100,
                "sub/b.mkv": "y" * 200,
                "note.txt": "not video",
            })
            lister = LocalFsLister(root)
            #  mock 掉 checkpoint 持久化
            lister.save_checkpoint = lambda: None
            videos = list(lister.iter_videos(incremental=False))
            rels = sorted(r for r, _, _ in videos)
            assert rels == ["a.mp4", "sub/b.mkv"]

    def test_incremental_detects_changed(self):
        from backend.emby_server.strm_source import LocalFsLister
        with tempfile.TemporaryDirectory() as root:
            self._make_files(root, {"a.mp4": "x" * 100, "b.mp4": "y" * 100})
            lister = LocalFsLister(root)
            saved = {}
            lister._save_fp = lambda fp: saved.update(fp)
            lister._load_fp = lambda: dict(saved)
            # 第一次全量，建立指纹
            all1 = list(lister.iter_videos(incremental=False))
            assert len(all1) == 2
            lister.save_checkpoint()
            # 改一个文件
            time.sleep(0.02)
            self._make_files(root, {"a.mp4": "x" * 200})
            # 增量只返回变化的
            lister2 = LocalFsLister(root)
            lister2._save_fp = lambda fp: saved.update(fp)
            lister2._load_fp = lambda: dict(saved)
            # 避免 key 冲突：用同一个 fp key
            lister2._fp_key = lister._fp_key
            inc = list(lister2.iter_videos(incremental=True))
            assert len(inc) == 1
            assert inc[0][0] == "a.mp4"

    def test_incremental_no_change_returns_empty(self):
        from backend.emby_server.strm_source import LocalFsLister
        with tempfile.TemporaryDirectory() as root:
            self._make_files(root, {"a.mp4": "x" * 100})
            lister = LocalFsLister(root)
            saved = {}
            lister._save_fp = lambda fp: saved.update(fp)
            lister._load_fp = lambda: dict(saved)
            list(lister.iter_videos(incremental=False))
            lister.save_checkpoint()
            lister2 = LocalFsLister(root)
            lister2._save_fp = lambda fp: saved.update(fp)
            lister2._load_fp = lambda: dict(saved)
            lister2._fp_key = lister._fp_key
            inc = list(lister2.iter_videos(incremental=True))
            assert inc == []


class FakeDB:
    """最小 fake db：get_value 按 key 返回预设。"""

    def __init__(self, values):
        self.values = values


class TestIntervalConfig:
    """interval_minutes / jitter_ratio 配置解析。"""

    def _patch_db_config(self, monkeypatch, values):
        import backend.emby_server.strm_gen as sg
        monkeypatch.setattr(sg, "_db_config",
                            lambda db, key, default="": values.get(key, default))

    def test_interval_normal(self, monkeypatch):
        import backend.emby_server.strm_gen as sg
        self._patch_db_config(monkeypatch, {sg.CONFIG_INTERVAL: "30"})
        assert sg.interval_minutes(FakeDB({})) == 30

    def test_interval_zero_means_disabled(self, monkeypatch):
        import backend.emby_server.strm_gen as sg
        self._patch_db_config(monkeypatch, {sg.CONFIG_INTERVAL: "0"})
        assert sg.interval_minutes(FakeDB({})) == 0

    def test_interval_invalid_fallback(self, monkeypatch):
        import backend.emby_server.strm_gen as sg
        self._patch_db_config(monkeypatch, {sg.CONFIG_INTERVAL: "abc"})
        assert sg.interval_minutes(FakeDB({})) == sg.DEFAULT_INTERVAL_MINUTES

    def test_interval_clamped(self, monkeypatch):
        import backend.emby_server.strm_gen as sg
        self._patch_db_config(monkeypatch, {sg.CONFIG_INTERVAL: "9999"})
        assert sg.interval_minutes(FakeDB({})) == 1440

    def test_jitter_ratio_clamped(self, monkeypatch):
        import backend.emby_server.strm_gen as sg
        self._patch_db_config(monkeypatch, {sg.CONFIG_JITTER_RATIO: "0.9"})
        assert sg.jitter_ratio(FakeDB({})) == 0.5

    def test_jitter_zero_disables(self, monkeypatch):
        import backend.emby_server.strm_gen as sg
        self._patch_db_config(monkeypatch, {
            sg.CONFIG_INTERVAL: "60",
            sg.CONFIG_JITTER_RATIO: "0",
        })
        # ratio=0 时精确返回 3600 秒
        assert sg._jittered_interval_sec(FakeDB({})) == 3600.0

    def test_jitter_in_range(self, monkeypatch):
        import backend.emby_server.strm_gen as sg
        self._patch_db_config(monkeypatch, {
            sg.CONFIG_INTERVAL: "60",
            sg.CONFIG_JITTER_RATIO: "0.2",
        })
        for _ in range(20):
            v = sg._jittered_interval_sec(FakeDB({}))
            assert 3600 * 0.8 <= v <= 3600 * 1.2

    def test_source_type_default_drive(self, monkeypatch):
        import backend.emby_server.strm_gen as sg
        self._patch_db_config(monkeypatch, {})
        assert sg.source_type(FakeDB({})) == "drive"

    def test_source_type_invalid_fallback(self, monkeypatch):
        import backend.emby_server.strm_gen as sg
        self._patch_db_config(monkeypatch, {sg.CONFIG_SOURCE_TYPE: "ftp"})
        assert sg.source_type(FakeDB({})) == "drive"


class TestTriggerStrmScan:
    """_trigger_strm_scan 只推 /strm 相关库，失败不抛异常。"""

    def test_only_strm_libraries(self, monkeypatch):
        import backend.emby_server.strm_gen as sg

        class FakeLib:
            is_enabled = True  # 类属性，供 filter(em.Library.is_enabled == True) 使用
            is_virtual = False

            def __init__(self, id, paths, name=""):
                self.id = id
                self.paths = paths
                self.name = name

        libs = [
            FakeLib(1, "/strm/电影,/strm/剧集", name="strm库"),
            FakeLib(2, "/media/电影", name="本地库"),
            FakeLib(3, "/strm", name="strm根"),
        ]

        class FakeQuery:
            def filter(self, *a, **k):
                return self

            def all(self):
                return libs

        class FakeDB2:
            def query(self, *a, **k):
                return FakeQuery()

        pushed = []

        class FakeRedis:
            @staticmethod
            def push_scan_request(library_id, trigger="manual", force_full=False):
                pushed.append((library_id, trigger))
                return {"created": True}

        import backend.emby_server.scan_queue_redis as sqr
        monkeypatch.setattr(sqr, "push_scan_request", FakeRedis.push_scan_request)
        monkeypatch.setattr(sg, "_container_strm_root", lambda db: "/strm")

        #  mock models
        import types
        fake_models = types.SimpleNamespace(Library=FakeLib)
        monkeypatch.setitem(__import__("sys").modules,
                            "backend.emby_server.models", fake_models)

        sg._trigger_strm_scan(FakeDB2())
        pushed_ids = sorted(pid for pid, _ in pushed)
        assert pushed_ids == [1, 3]
        assert all(t == "strm-gen" for _, t in pushed)

    def test_failure_does_not_raise(self, monkeypatch):
        import backend.emby_server.strm_gen as sg

        class BadDB:
            def query(self, *a, **k):
                raise RuntimeError("db down")

        # 不应抛异常
        sg._trigger_strm_scan(BadDB())
