"""定向扫描真定向：带 limit_prefixes 的扫描只走目标子树（回归）

钉住的契约：

- Drive：``walk_media_targeted`` 的产出与「整源遍历 + 同前缀过滤」逐项
  一致（含同名文件夹第二分支、真撞改名）；列举只发生在脊柱 + 目标子树内，
  无关目录零列举；``walk_media`` 不带 limit 时行为不变。
- 扫描器分拣（``_targeted_walk_plan`` / ``_mount_files``）：没有任何前缀
  落入的来源整源跳过（provider 零调用）；通用 provider 逐个目标子树走
  既有 ``walk_media(root=...)``；覆盖型前缀回退整源遍历。
- 本机来源（``_local_targeted_roots``）：只走目标目录；单文件前缀回退整源。
- walk 打点：``list_calls`` / ``walk_ms`` 写进 scan run stats（来源明细 +
  整轮合计键 ``walk_list_calls`` / ``walk_ms``），且能过 encode 落库白名单。

不碰网络、不碰数据库：``drive_changes`` 的 API 入口被替换成内存假实现，
目录树是字典（与 test_drive_mount.py 同款）。
"""
import os

# 必须在 import backend 之前钉住 sqlite（同 test_drive_mount.py）
os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import re
from types import SimpleNamespace

import pytest

from backend.emby_server import drive_changes
from backend.emby_server import mount_drive  # noqa: F401 — 导入即注册 drive 提供者
from backend.emby_server import mounts as mnt
from backend.emby_server import scanner
from backend.emby_server.mount_drive import DriveMount

FOLDER = "application/vnd.google-apps.folder"
DRIVE_ID = "0ATESTDRIVE"


def _folder(fid, name):
    return {"id": fid, "name": name, "mimeType": FOLDER}


def _file(fid, name, size="1000"):
    return {"id": fid, "name": name, "mimeType": "video/x-matroska",
            "size": size, "modifiedTime": "2026-10-30T00:00:00.000Z"}


# folder id → 条目。形状仿 2026-10-30 剖查的动漫库：目标只是
# 动漫/剧A/Season 1 一个目录；同级有同名文件夹第二分支（剧A 有两份），
# 另一个大子树（电影）与目标完全无关。
TREE = {
    DRIVE_ID: [
        _folder("F_ANIME", "动漫"),
        _folder("F_MOV", "电影"),
    ],
    "F_ANIME": [
        _folder("F_SHOWA", "剧A"),
        _folder("F_SHOWA2", "剧A"),
        _folder("F_SHOWB", "剧B"),
    ],
    "F_SHOWA": [
        _folder("F_A_S1", "Season 1"),
        _folder("F_A_S2", "Season 2"),
    ],
    "F_SHOWA2": [
        _folder("F_A2_S1", "Season 1"),
    ],
    "F_A_S1": [
        _file("VA1", "ep1.mkv"),
        _file("VA2", "ep2.mkv"),
    ],
    "F_A_S2": [
        _file("VA3", "ep1.mkv"),
    ],
    "F_A2_S1": [
        _file("VA9", "ep1.mkv"),  # 与 F_A_S1 的 ep1.mkv 同文本路径 → 真撞改名
    ],
    "F_SHOWB": [
        _folder("F_B_S1", "Season 1"),
    ],
    "F_B_S1": [
        _file("VB1", "ep1.mkv"),
        _file("VB2", "ep2.mkv"),
        _file("VB3", "ep3.mkv"),
    ],
    "F_MOV": [
        _folder("F_MOVX", "片X"),
    ],
    "F_MOVX": [
        _file("VM1", "片X.mkv"),
        _file("VM2", "片X2.mkv"),
    ],
}

ALL_FOLDERS = set(TREE)

API_CALLS: list[dict] = []


def _listed_folders():
    out = set()
    for params in API_CALLS:
        match = re.search(r"'([^']+)' in parents", params.get("q", ""))
        if match:
            out.add(match.group(1))
    return out


def _fake_api_get(url, token, params):
    API_CALLS.append(dict(params))
    match = re.search(r"'([^']+)' in parents", params.get("q", ""))
    folder_id = match.group(1) if match else ""
    return {"files": TREE.get(folder_id, [])}


@pytest.fixture(autouse=True)
def _fake_drive(monkeypatch):
    API_CALLS.clear()
    mnt.invalidate_list_cache()
    monkeypatch.setattr(drive_changes, "_api_get", _fake_api_get)
    monkeypatch.setattr(drive_changes, "get_token_for_drive",
                        lambda drive_id, force_refresh=False: "tok-1")
    monkeypatch.setattr(drive_changes, "next_shared_sa",
                        lambda: {"client_email": "sa@example.iam.gserviceaccount.com",
                                 "private_key_id": "k1"})
    monkeypatch.setattr(drive_changes, "_sa_access_token",
                        lambda sa, min_remaining=60: "tok-pin")
    yield
    mnt.invalidate_list_cache()


def _mount(path=f"drive:{DRIVE_ID}", config=None):
    return SimpleNamespace(
        id=99, name="gdrive-test", mount_type="drive",
        path=path, config=config if config is not None else {},
    )


def _rel_hit(rel, *prefixes):
    """与 scanner._prefix_filter 同口径（stored_path 去掉 mount://<id> 前缀）"""
    return any(rel == px or rel.startswith(px.rstrip("/") + "/") for px in prefixes)


# ---------- Drive 遍历：定向 == 整源过滤，且只列脊柱+目标 ----------

def test_drive_targeted_matches_full_walk_filtered():
    full = DriveMount(_mount())
    old = [(f.rel, f.file_id) for f in full.walk_media(root="/")]
    old_listed = _listed_folders()
    assert old_listed == ALL_FOLDERS  # 整源遍历把每棵子树都列了
    assert full.list_call_count == len(ALL_FOLDERS)

    API_CALLS.clear()
    targeted = DriveMount(_mount())
    new = [(f.rel, f.file_id) for f in targeted.walk_media_targeted(
        root="/", limit_rels=["/动漫/剧A/Season 1"])]
    expected = [x for x in old if _rel_hit(x[0], "/动漫/剧A/Season 1")]
    # 逐项一致（含同名第二分支 VA9 的真撞改名 "ep1 [2].mkv"）
    assert new == expected
    assert {rel for rel, _ in new} == {
        "/动漫/剧A/Season 1/ep1.mkv",
        "/动漫/剧A/Season 1/ep1 [2].mkv",
        "/动漫/剧A/Season 1/ep2.mkv",
    }
    # 只列脊柱（盘根/动漫/两个剧A）+ 目标（两个 Season 1），其余零列举
    assert _listed_folders() == {DRIVE_ID, "F_ANIME", "F_SHOWA", "F_SHOWA2",
                                 "F_A_S1", "F_A2_S1"}
    assert targeted.list_call_count == 6


def test_drive_targeted_multiple_prefixes_union():
    full = DriveMount(_mount())
    old = [(f.rel, f.file_id) for f in full.walk_media(root="/")]
    API_CALLS.clear()
    targeted = DriveMount(_mount())
    new = [(f.rel, f.file_id) for f in targeted.walk_media_targeted(
        root="/", limit_rels=["/动漫/剧B/Season 1", "/电影"])]
    expected = [x for x in old
                if _rel_hit(x[0], "/动漫/剧B/Season 1", "/电影")]
    assert sorted(new) == sorted(expected)
    assert len(new) == 5  # 剧B 3 个 + 电影/片X 2 个
    assert _listed_folders() == {DRIVE_ID, "F_ANIME", "F_SHOWB", "F_B_S1",
                                 "F_MOV", "F_MOVX"}


def test_drive_walk_without_limit_unchanged():
    plain = DriveMount(_mount())
    a = [(f.rel, f.file_id) for f in plain.walk_media(root="/")]
    empty_limit = DriveMount(_mount())
    b = [(f.rel, f.file_id) for f in empty_limit.walk_media(root="/", limit_rels=())]
    assert a == b
    assert len(a) == 9  # 全树 9 个媒体文件（含改名后的 VA9）


def test_drive_targeted_no_match_yields_nothing_and_lists_only_root():
    targeted = DriveMount(_mount())
    new = list(targeted.walk_media_targeted(
        root="/", limit_rels=["/不存在/深处"]))
    assert new == []
    assert _listed_folders() == {DRIVE_ID}  # 脊柱第一步就断，无关子树零列举


# ---------- 扫描器分拣：_targeted_walk_plan 三态 ----------

def _remote_src(subpath="/", mount_id=99):
    provider = SimpleNamespace(local_root=None)
    return SimpleNamespace(
        kind="mount", mount=SimpleNamespace(id=mount_id), subpath=subpath,
        provider=provider, source_row_id=None)


def test_plan_target_inside_source():
    plan = scanner._targeted_walk_plan(
        _remote_src(), ("mount://99/动漫/剧A/Season 1",))
    assert plan == ["/动漫/剧A/Season 1"]


def test_plan_no_prefix_in_source_skips():
    assert scanner._targeted_walk_plan(
        _remote_src(), ("mount://98/动漫/剧A/Season 1",)) == []
    assert scanner._targeted_walk_plan(
        _remote_src(subpath="/电影"), ("mount://99/动漫/剧A/Season 1",)) == []


def test_plan_covering_prefix_falls_back_to_full():
    assert scanner._targeted_walk_plan(_remote_src(), ("mount://99",)) is None
    assert scanner._targeted_walk_plan(
        _remote_src(subpath="/动漫"), ("mount://99/动漫",)) is None


def test_plan_contained_targets_deduped():
    plan = scanner._targeted_walk_plan(
        _remote_src(), ("mount://99/动漫/剧A/Season 1", "mount://99/动漫"))
    assert plan == ["/动漫"]


# ---------- _mount_files：通用 provider 分拣 ----------

class _FakeProvider:
    """无 walk_media_targeted 的通用 provider：按 root 记账、按 root 出文件"""

    local_root = None

    def __init__(self, files_by_root):
        self.files_by_root = files_by_root
        self.walk_roots: list[str] = []

    def walk_media(self, root="/"):
        self.walk_roots.append(root)
        yield from self.files_by_root.get(root, [])


def _mf(rel, fid):
    name = rel.rsplit("/", 1)[-1]
    return mnt.MountFile(rel=rel, name=name, size=100, file_id=fid)


_ALL_FILES = [
    _mf("/动漫/剧A/Season 1/ep1.mkv", "VA1"),
    _mf("/动漫/剧A/Season 2/ep1.mkv", "VA3"),
    _mf("/电影/片X/片X.mkv", "VM1"),
]


def _src(provider, subpath="/"):
    return SimpleNamespace(
        kind="mount", mount=SimpleNamespace(id=99), subpath=subpath,
        provider=provider, source_row_id=None)


def test_mount_files_targeted_walks_only_target_subtree():
    provider = _FakeProvider({
        "/": list(_ALL_FILES),
        "/动漫/剧A/Season 1": [_ALL_FILES[0]],
    })
    out = [sf.stored_path for sf in scanner._mount_files(
        _src(provider), provider, [], ("mount://99/动漫/剧A/Season 1",))]
    assert provider.walk_roots == ["/动漫/剧A/Season 1"]
    assert out == ["mount://99/动漫/剧A/Season 1/ep1.mkv"]


def test_mount_files_zero_hit_source_never_touches_provider():
    provider = _FakeProvider({"/": list(_ALL_FILES)})
    out = list(scanner._mount_files(
        _src(provider), provider, [], ("mount://98/动漫/剧A/Season 1",)))
    assert out == []
    assert provider.walk_roots == []  # 整源跳过：零列举调用


def test_mount_files_no_prefix_and_covering_prefix_walk_whole_source():
    provider = _FakeProvider({"/": list(_ALL_FILES)})
    out = [sf.stored_path for sf in scanner._mount_files(_src(provider), provider, [])]
    assert provider.walk_roots == ["/"]
    assert len(out) == 3

    provider2 = _FakeProvider({"/": list(_ALL_FILES)})
    out2 = [sf.stored_path for sf in scanner._mount_files(
        _src(provider2), provider2, [], ("mount://99",))]
    assert provider2.walk_roots == ["/"]
    assert len(out2) == 3


def test_mount_files_with_drive_provider_end_to_end():
    provider = DriveMount(_mount())
    out = [sf.stored_path for sf in scanner._mount_files(
        _src(provider), provider, [], ("mount://99/动漫/剧A/Season 1",))]
    assert sorted(out) == [
        "mount://99/动漫/剧A/Season 1/ep1 [2].mkv",
        "mount://99/动漫/剧A/Season 1/ep1.mkv",
        "mount://99/动漫/剧A/Season 1/ep2.mkv",
    ]
    assert _listed_folders() == {DRIVE_ID, "F_ANIME", "F_SHOWA", "F_SHOWA2",
                                 "F_A_S1", "F_A2_S1"}

    # 前缀属于别的挂载：Drive provider 一次都不许列
    API_CALLS.clear()
    provider2 = DriveMount(_mount())
    out2 = list(scanner._mount_files(
        _src(provider2), provider2, [], ("mount://98/动漫",)))
    assert out2 == []
    assert API_CALLS == []


# ---------- 本机来源分拣 ----------

def test_local_targeted_roots_and_equivalence(tmp_path):
    base = tmp_path / "lib"
    (base / "剧A" / "Season 1").mkdir(parents=True)
    (base / "剧A" / "Season 2").mkdir(parents=True)
    (base / "剧B").mkdir(parents=True)
    for rel in ("剧A/Season 1/a.mkv", "剧A/Season 2/b.mkv", "剧B/c.mkv", "top.mkv"):
        (base / rel).write_bytes(b"x")

    target = str(base / "剧A" / "Season 1")
    assert scanner._local_targeted_roots(str(base), (target,)) == [target]
    assert scanner._local_targeted_roots(str(base), ("/elsewhere/x",)) == []
    assert scanner._local_targeted_roots(str(base), (str(base),)) is None
    # 单文件前缀 → 回退整源（靠过滤精确命中）
    assert scanner._local_targeted_roots(
        str(base), (str(base / "top.mkv"),)) is None

    failed: list = []
    old = {sf.stored_path for sf in scanner._prefix_filter(
        scanner._local_dir_files(str(base), failed), (target,))}
    new = {sf.stored_path for sf in scanner._prefix_filter(
        scanner._local_dir_files(target, failed), (target,))}
    assert old == new == {target + "/a.mkv"}


# ---------- walk 打点进 stats ----------

def test_walk_metrics_recorded_and_encoded():
    import time as _time

    def _slow():
        for i in (1, 2):
            _time.sleep(0.01)  # 让 walk_ms 可测（>0 才写整轮合计键）
            yield i

    provider = SimpleNamespace(list_call_count=10)
    meter = scanner._WalkMeter(_slow(), provider=provider)
    assert list(meter) == [1, 2]
    provider.list_call_count = 17  # 遍历期间发生了 7 次列举调用

    counted = SimpleNamespace(_files=meter, total=2)
    stats: dict = {}
    walk_ms, list_calls = scanner._record_walk_metrics(stats, counted)
    assert list_calls == 7
    assert stats["walk_list_calls"] == 7
    assert stats["walk_ms"] == walk_ms >= 0

    entry = scanner._source_entry("来源甲", {}, stats, 2, [],
                                  walk_ms=walk_ms, list_calls=list_calls)
    encoded = scanner._encode_scan_sources([entry])
    assert encoded[0]["list_calls"] == 7
    assert encoded[0]["walk_ms"] == walk_ms

    assert "walk_list_calls" in scanner.SCAN_STATS_KEYS
    assert "walk_ms" in scanner.SCAN_STATS_KEYS
    blob = scanner.encode_scan_stats(
        {"walk_list_calls": 7, "walk_ms": 3, "sources": [entry]})
    assert '"walk_list_calls": 7' in blob


def test_walk_meter_without_provider_counts_zero_calls():
    meter = scanner._WalkMeter(iter(["a"]))
    assert list(meter) == ["a"]
    assert meter.list_calls == 0
    assert meter.walk_ms >= 0
    counted = SimpleNamespace(_files=meter, total=1)
    stats: dict = {}
    assert scanner._record_walk_metrics(stats, counted)[1] == 0
    assert "walk_list_calls" not in stats
