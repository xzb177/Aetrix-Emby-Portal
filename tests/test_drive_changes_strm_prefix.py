"""drive-changes：/strm 库 → Drive 前缀映射（_strm_lib_drive_mappings）。

映射规则（数据驱动，不写死库路径）：
库路径 /strm/<rel> 在 Drive 侧对应 /<source_dir>/<rel>（逐盘、逐源目录展开）。
"""
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.emby_server import drive_changes as dc


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, libs):
        self._libs = libs

    def query(self, model):
        return _FakeQuery(self._libs)


class _FakeLib:
    def __init__(self, lib_id, paths, is_virtual=False):
        self.id = lib_id
        self.paths = paths
        self.is_virtual = is_virtual


def _install_fakes(monkeypatch, libs, src_dirs, drive_cfg="all",
                   strm_root="/strm", drives=None, raw="MoviePilot/,nastool/"):
    """往 sys.modules 里塞假的 models / strm_gen，并 monkeypatch discover_drives。"""
    drives = drives if drives is not None else {
        "0AERUVu4kfWEfUk9PVA": ["MP:"], "myDrive:paul_emby": ["paul_emby:"]}

    fake_models = types.ModuleType("backend.emby_server.models")

    class Library:  # noqa: D106
        is_enabled = True

    fake_models.Library = Library
    monkeypatch.setitem(sys.modules, "backend.emby_server.models", fake_models)

    fake_sg = types.ModuleType("backend.emby_server.strm_gen")
    fake_sg.CONFIG_DRIVE_ID = "strm_gen_drive_id"
    fake_sg._container_strm_root = lambda db: strm_root
    fake_sg.source_dirs = lambda db: [s for s in src_dirs]
    fake_sg.source_dir_raw = lambda db: raw
    fake_sg._db_config = lambda db, k, default="": drive_cfg if k == "strm_gen_drive_id" else default
    monkeypatch.setitem(sys.modules, "backend.emby_server.strm_gen", fake_sg)

    monkeypatch.setattr(dc, "discover_drives", lambda: drives)
    return _FakeDB(libs)


def test_strm_lib_maps_to_all_drives_and_src_dirs(monkeypatch):
    db = _install_fakes(
        monkeypatch,
        libs=[_FakeLib(44, "/strm/剧集/国产剧")],
        src_dirs=["MoviePilot/", "nastool/"],
    )
    got = dc._strm_lib_drive_mappings(db)
    subs = sorted(m[2] for m in got)
    assert subs == sorted([
        "/MoviePilot/剧集/国产剧", "/nastool/剧集/国产剧",
        "/MoviePilot/剧集/国产剧", "/nastool/剧集/国产剧",
    ])
    # 盘 × 源目录：2 盘 × 2 目录 = 4 条
    assert len(got) == 4
    drive_ids = {m[1] for m in got}
    assert drive_ids == {"0AERUVu4kfWEfUk9PVA", "myDrive:paul_emby"}
    # fs 前缀回指库路径原文
    assert {m[3] for m in got} == {"/strm/剧集/国产剧"}


def test_drive_config_single_drive(monkeypatch):
    db = _install_fakes(
        monkeypatch,
        libs=[_FakeLib(44, "/strm/剧集/国产剧")],
        src_dirs=["MoviePilot/"],
        drive_cfg="0AERUVu4kfWEfUk9PVA",
    )
    got = dc._strm_lib_drive_mappings(db)
    assert len(got) == 1
    assert got[0][:3] == (44, "0AERUVu4kfWEfUk9PVA", "/MoviePilot/剧集/国产剧")


def test_non_strm_and_virtual_skipped(monkeypatch):
    db = _install_fakes(
        monkeypatch,
        libs=[
            _FakeLib(1, "/mnt/mp/nastool/剧集"),          # FUSE 路径：本函数不管
            _FakeLib(2, "/strm/剧集/国产剧", is_virtual=True),  # 虚拟库跳过
            _FakeLib(3, ""),                              # 空路径跳过
        ],
        src_dirs=["MoviePilot/"],
    )
    assert dc._strm_lib_drive_mappings(db) == []


def test_strm_root_itself(monkeypatch):
    db = _install_fakes(
        monkeypatch,
        libs=[_FakeLib(9, "/strm")],
        src_dirs=["MoviePilot/"],
        drive_cfg="all",
        drives={"D1": ["MP:"]},
    )
    got = dc._strm_lib_drive_mappings(db)
    assert got == [(9, "D1", "/MoviePilot", "/strm")]


def test_explicit_empty_source_dir_means_drive_root(monkeypatch):
    db = _install_fakes(
        monkeypatch,
        libs=[_FakeLib(44, "/strm/剧集/国产剧")],
        src_dirs=[],
        raw="",
        drives={"D1": ["MP:"]},
    )
    got = dc._strm_lib_drive_mappings(db)
    assert got == [(44, "D1", "/剧集/国产剧", "/strm/剧集/国产剧")]


def test_library_drive_prefixes_includes_strm(monkeypatch):
    """_library_drive_prefixes 对 /strm 库返回非空（生产 11 库场景的回归）。"""
    db = _install_fakes(
        monkeypatch,
        libs=[_FakeLib(44, "/strm/剧集/国产剧"), _FakeLib(46, "/strm/剧集/欧美剧")],
        src_dirs=["MoviePilot/", "nastool/", "video/"],
    )
    got = dc._library_drive_prefixes(db)
    assert got, "_library_drive_prefixes 对 /strm 库仍返回空，增量发现依旧失活"
    by_lib = {}
    for lib_id, _did, sub in got:
        by_lib.setdefault(lib_id, []).append(sub)
    assert "/MoviePilot/剧集/国产剧" in by_lib[44]
    assert "/video/剧集/欧美剧" in by_lib[46]


def test_fs_prefixes_roundtrip(monkeypatch):
    """/strm 映射在 _library_fs_prefixes 里能按 (lib_id, sub) 查回 fs 前缀。"""
    db = _install_fakes(
        monkeypatch,
        libs=[_FakeLib(44, "/strm/剧集/国产剧")],
        src_dirs=["MoviePilot/"],
        drives={"D1": ["MP:"]},
    )
    fs = dc._library_fs_prefixes(db)
    assert fs[(44, "/MoviePilot/剧集/国产剧")] == "/strm/剧集/国产剧"
