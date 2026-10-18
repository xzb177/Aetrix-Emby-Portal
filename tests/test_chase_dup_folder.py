"""追新（change_watcher）同名文件夹遮蔽修复。

背景：Drive 挂载 #1036 把扫描列举（walk_media）改成按 folder id 合并同名
文件夹命名空间，但追新走的 ``change_watcher._walk_files`` 还是文本逐层解析
（每级只认首个同名文件夹）——新文件若落在第二个同名分支里，追新永远发现
不了，只能等全量扫描兜底。

本文件钉住修复后的三条契约（全部用内存假 Drive，不碰网络与生产库）：
1. 第二分支的新文件能被发现（旧语义下被遮蔽）。
2. 双分支真撞（同文本路径、不同 file_id）不重复报新：确定性改名后 rel 唯一，
   快照主键不撞、跨轮不抖。
3. 无同名文件夹的目录树：合并枚举与旧文本遍历产出完全一致（零行为变化）；
   非 Drive 提供者（rclone 等）仍走旧逻辑，一字不动。

假 Drive 模式照 tests/test_drive_mount.py：替换 ``drive_changes._api_get``，
folder id → 条目字典。
"""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import re
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models as base_models
from backend.emby_server import change_watcher as cw
from backend.emby_server import drive_changes
from backend.emby_server import models as em
from backend.emby_server import mounts as mnt
from backend.emby_server import mount_drive  # noqa: F401 — 导入即注册 drive 提供者
from backend.emby_server.mount_drive import DriveMount

FOLDER = "application/vnd.google-apps.folder"
DRIVE_ID = "0AERUVu4kfWEfUk9PVA"


def _folder(fid, name):
    return {"id": fid, "name": name, "mimeType": FOLDER}


def _file(fid, name, size="1000", mime="video/x-matroska"):
    return {"id": fid, "name": name, "mimeType": mime, "size": size,
            "modifiedTime": "2026-10-18T00:00:00.000Z"}


def _mount():
    return SimpleNamespace(
        id=99, name="gdrive-generic", mount_type="drive",
        path=f"drive:{DRIVE_ID}", config={},
    )


def _api_get_for(tree):
    def _fake(url, token, params):
        match = re.search(r"'([^']+)' in parents", params.get("q", ""))
        folder_id = match.group(1) if match else ""
        return {"files": tree.get(folder_id, [])}
    return _fake


def _fake_token(drive_id, force_refresh=False):
    return "tok-fake"


def _fake_next_sa():
    return {"client_email": "fake@sa.iam.gserviceaccount.com"}


def _fake_sa_access_token(sa, min_remaining=60):
    return "tok-sa-fake"


@pytest.fixture(autouse=True)
def _fake_drive_auth(monkeypatch):
    """Drive 凭据层垫掉（与 test_drive_mount.py 同款）：只测遍历语义，不碰凭据。"""
    monkeypatch.setattr(drive_changes, "get_token_for_drive", _fake_token)
    monkeypatch.setattr(drive_changes, "next_shared_sa", _fake_next_sa)
    monkeypatch.setattr(drive_changes, "_sa_access_token", _fake_sa_access_token)
    yield


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                           poolclass=StaticPool)
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


# ---------- 树 1：同名双分支，新文件只在第二分支 ----------

TREE_DUP = {
    DRIVE_ID: [_folder("F_ROOT", "剧集")],
    "F_ROOT": [
        _folder("F_A", "黑白密码 (2023)"),  # 第一分支
        _folder("F_B", "黑白密码 (2023)"),  # 第二分支（旧遍历走不到）
    ],
    "F_A": [_folder("F_A_S1", "Season 1")],
    "F_B": [_folder("F_B_S1", "Season 1")],
    "F_A_S1": [_file("V_A1", "别的剧集 - S01E01.mkv")],
    "F_B_S1": [_file("V_B16", "黑白密码 - S01E16.mkv")],  # 只在第二分支
}


@pytest.fixture()
def dup_drive(monkeypatch):
    monkeypatch.setattr(drive_changes, "_api_get", _api_get_for(TREE_DUP))
    yield


def test_walk_files_discovers_second_branch_new_file(dup_drive):
    """第二分支的新文件能被追新发现（旧文本遍历只认首个同名文件夹，会漏掉）"""
    provider = DriveMount(_mount())
    found = cw._walk_files(provider, "/剧集", 8, 100000)
    by_rel = {e.rel: e for e in found}
    assert by_rel["/剧集/黑白密码 (2023)/Season 1/黑白密码 - S01E16.mkv"].file_id == "V_B16"
    assert by_rel["/剧集/黑白密码 (2023)/Season 1/别的剧集 - S01E01.mkv"].file_id == "V_A1"
    assert len(found) == 2


# ---------- 树 2：真撞（同文本路径、不同 file_id） ----------

TREE_COLLIDE = {
    DRIVE_ID: [_folder("F_C_ROOT", "综艺")],
    "F_C_ROOT": [
        _folder("F_C_A", "同名节目"),
        _folder("F_C_B", "同名节目"),
    ],
    "F_C_A": [_file("V_CA", "S01E01.mkv")],
    "F_C_B": [_file("V_CB", "S01E01.mkv")],  # 真撞：同文本路径，不同 fid
}


@pytest.fixture()
def collide_drive(monkeypatch):
    monkeypatch.setattr(drive_changes, "_api_get", _api_get_for(TREE_COLLIDE))
    yield


def _entries_tuples(found):
    return [(e.rel, e.size or 0, e.mod_ts or 0.0) for e in found]


def test_walk_files_true_collision_no_double_report(collide_drive, db):
    """真撞不重复报新：改名后 rel 唯一，快照主键不撞、跨轮不抖"""
    provider = DriveMount(_mount())
    found = cw._walk_files(provider, "/综艺", 8, 100000)
    rels = sorted(e.rel for e in found)
    assert rels == ["/综艺/同名节目/S01E01 [2].mkv", "/综艺/同名节目/S01E01.mkv"]
    # 第一轮建基线：不报（防扫描风暴），但两行都落快照（主键 rel_path 唯一不撞）
    changed = cw._diff_against_snapshot(db, 7, "mount://99/综艺",
                                        _entries_tuples(found),
                                        baseline_if_empty=True)
    assert changed == []
    rows = db.query(em.ChaseFileSnapshot).filter(
        em.ChaseFileSnapshot.library_id == 7,
        em.ChaseFileSnapshot.source_key == "mount://99/综艺").all()
    assert sorted(r.rel_path for r in rows) == rels
    # 第二轮无变化：零上报（不重复报新）
    changed2 = cw._diff_against_snapshot(db, 7, "mount://99/综艺",
                                         _entries_tuples(found),
                                         baseline_if_empty=False)
    assert changed2 == []


# ---------- 树 3：无同名（回归：与旧文本遍历一致） ----------

TREE_PLAIN = {
    DRIVE_ID: [_folder("F_M", "电影")],
    "F_M": [
        _file("V_M1", "a.mkv"),
        _file("V_M2", "b.mp4"),
        _file("V_M4", "d.rmvb", mime="video/rmvb"),  # 追新口径的视频，扫描缺省集没有
        _folder("F_M_SUB", "子目录"),
    ],
    "F_M_SUB": [_file("V_M3", "c.mkv")],
}


@pytest.fixture()
def plain_drive(monkeypatch):
    monkeypatch.setattr(drive_changes, "_api_get", _api_get_for(TREE_PLAIN))
    yield


class _OldTextProvider:
    """模拟旧 _walk_files 的文本逐层语义（首个匹配）：无同名树下即旧行为本身。"""

    def __init__(self, tree):
        self._tree = tree

    def list_dir(self, rel):
        out = []
        for item in self._tree.get(rel, []):
            is_dir = item["mimeType"] == FOLDER
            out.append(mnt.MountEntry(
                name=item["name"], rel=rel.rstrip("/") + "/" + item["name"],
                is_dir=is_dir, size=int(item.get("size") or 0),
                entry_id=item["id"] if is_dir else "",
                file_id="" if is_dir else item["id"],
                mod_ts=0.0))
        return out


_PLAIN_TEXT_TREE = {
    "/电影": [
        {"id": "V_M1", "name": "a.mkv", "mimeType": "x", "size": "1000"},
        {"id": "V_M2", "name": "b.mp4", "mimeType": "x", "size": "1000"},
        {"id": "V_M4", "name": "d.rmvb", "mimeType": "x", "size": "1000"},
        {"id": "F_M_SUB", "name": "子目录", "mimeType": FOLDER},
    ],
    "/电影/子目录": [
        {"id": "V_M3", "name": "c.mkv", "mimeType": "x", "size": "1000"},
    ],
}


def test_walk_files_no_dup_matches_old_text_walk(plain_drive):
    """无同名文件夹：合并枚举与旧文本遍历产出完全一致（零行为变化）"""
    drive_provider = DriveMount(_mount())
    merged = {(e.rel, e.file_id) for e in
              cw._walk_files(drive_provider, "/电影", 8, 100000)}
    old = {(e.rel, e.file_id) for e in
           cw._walk_files(_OldTextProvider(_PLAIN_TEXT_TREE), "/电影", 8, 100000)}
    assert merged == old
    assert {r for r, _ in merged} == {
        "/电影/a.mkv", "/电影/b.mp4", "/电影/d.rmvb", "/电影/子目录/c.mkv"}


def test_walk_files_non_drive_provider_keeps_old_path():
    """非 Drive 提供者（无 walk_media_merged）：仍走旧文本逻辑，一字不动"""
    provider = _OldTextProvider(_PLAIN_TEXT_TREE)
    assert not hasattr(provider, "walk_media_merged")
    found = cw._walk_files(provider, "/电影", 8, 100000)
    assert sorted(e.rel for e in found) == [
        "/电影/a.mkv", "/电影/b.mp4", "/电影/d.rmvb", "/电影/子目录/c.mkv"]


def test_walk_media_merged_exts_param(plain_drive):
    """exts 参数透传：追新传自己的 VIDEO_EXTS（含 .rmvb），缺省集会漏"""
    provider = DriveMount(_mount())
    only_mkv = {f.rel for f in provider.walk_media_merged(root="/电影", exts={".mkv"})}
    assert only_mkv == {"/电影/a.mkv", "/电影/子目录/c.mkv"}
    default_exts = {f.rel for f in provider.walk_media(root="/电影")}
    assert "/电影/d.rmvb" not in default_exts  # 缺省 REMOTE_MEDIA_EXTS 无 .rmvb
    via_walk_files = {e.rel for e in
                      cw._walk_files(provider, "/电影", 8, 100000, exts={".rmvb"})}
    assert via_walk_files == {"/电影/d.rmvb"}


def test_walk_files_merged_truncation(plain_drive):
    """max_entries 截断：达到上限即停"""
    provider = DriveMount(_mount())
    found = cw._walk_files(provider, "/电影", 8, 2)
    assert len(found) == 2


def test_walk_files_merged_subdir_failure_tolerated(monkeypatch):
    """子目录列举失败：已产出照常返回，不整轮抛错（追新一贯契约）"""
    failing = {"F_B_S1"}

    def _fail(url, token, params):
        match = re.search(r"'([^']+)' in parents", params.get("q", ""))
        folder_id = match.group(1) if match else ""
        if folder_id in failing:
            raise drive_changes.DriveNotFoundError(folder_id)
        return {"files": TREE_DUP.get(folder_id, [])}

    monkeypatch.setattr(drive_changes, "_api_get", _fail)
    provider = DriveMount(_mount())
    found = cw._walk_files(provider, "/剧集", 8, 100000)  # 不抛
    assert [e.file_id for e in found] == ["V_A1"]
