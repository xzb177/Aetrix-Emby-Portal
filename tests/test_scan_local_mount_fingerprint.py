"""A-096：本机可读挂载分支丢 mtime，文件指纹退化为 path|size。

同尺寸原地替换的文件：fast-skip 与目录指纹双双放行，元数据永久陈旧。
修法：LocalMount.walk_media 一次 stat 同时取 size 与 mtime 填 mod_ts；
_mount_files 本机分支补 remote_mod_ts（与远程分支同口径）。
"""
import ast
import hashlib
import inspect
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace

from backend.emby_server import mounts as mounts_lib
from backend.emby_server import scanner


def test_local_mount_walk_media_fills_mod_ts(tmp_path):
    f = tmp_path / "show.S01E01.mkv"
    f.write_bytes(b"x" * 100)
    os.utime(f, (1700000000, 1700000000))
    mount = mounts_lib.LocalMount(SimpleNamespace(path=str(tmp_path), mount_type="local"))
    files = list(mount.walk_media("/"))
    assert len(files) == 1
    assert files[0].size == 100
    assert files[0].mod_ts == 1700000000.0


def test_mount_files_local_branch_carries_mod_ts(monkeypatch):
    provider = SimpleNamespace(
        local_root="/mnt/fake",
        walk_media=lambda root="/": [
            mounts_lib.MountFile(rel="/a.mkv", name="a.mkv", size=100, mod_ts=1700000123.0)
        ],
    )
    src = SimpleNamespace(mount=SimpleNamespace(id=7), subpath="/", source_row_id=None)
    monkeypatch.setattr(
        scanner.mount_lib, "local_play_target",
        lambda p: SimpleNamespace(value=p))
    out = list(scanner._mount_files(src, provider, []))
    assert len(out) == 1
    assert out[0].remote_mod_ts == 1700000123.0


def test_fingerprint_changes_with_mod_ts():
    base = dict(stored_path="/mnt/fake/a.mkv", name="a.mkv",
                local_dir="/mnt/fake", size=100)
    fp_old = scanner._file_fingerprint(scanner.ScanFile(**base, remote_mod_ts=0.0))
    # 无 mod 时间时保持老口径 path|size，不抖动存量指纹
    assert fp_old == hashlib.md5(b"/mnt/fake/a.mkv|100").hexdigest()
    fp1 = scanner._file_fingerprint(scanner.ScanFile(**base, remote_mod_ts=1700000000.0))
    fp2 = scanner._file_fingerprint(scanner.ScanFile(**base, remote_mod_ts=1700000060.0))
    assert fp1 != fp_old
    assert fp2 != fp1


def test_both_mount_file_branches_carry_fingerprint_kwarg():
    # 静态守卫：_mount_files 内两个 ScanFile(...) 构造都必须带指纹相关 kwarg，
    # 否则本地分支的同尺寸替换又会悄悄退化回 path|size。
    tree = ast.parse(inspect.getsource(scanner._mount_files))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "ScanFile"]
    assert len(calls) == 2
    for call in calls:
        kws = {kw.arg for kw in call.keywords}
        assert kws & {"remote_mod_ts", "mtime_ns"}, (
            "ScanFile 构造缺指纹 kwarg: %s" % sorted(kws))
