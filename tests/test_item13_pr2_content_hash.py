"""#13 PR-2：content_hash 采集透传（只写、不改任何判定）。

覆盖：
- Drive _LIST_FIELDS 含 md5Checksum/headRevisionId/version（不含 etag：
  共享盘列举带 etag 会被谷歌回 400，见 2026-10-25 挂载 3 中断）；
  _entries_of 把 md5Checksum→content_hash（algo md5）、etag、
  headRevisionId/version→revision；无 md5 的文件与目录为空串；
  walk_media 的 MountFile 带全四槽。
- rclone：CLI lsjson 带 --hash；_raw_listing 保留 Hashes；
  list_dir 按 md5 > sha1 > sha256 偏好取；RC 模式无 Hashes → null；
  通用 walk（MountEntry→MountFile）透传哈希。
- 115：walk_media 与 list_dir 的哈希恒为空（sha1 未实测，缺位处理）。
- 写库 helper：有哈希才写列、无哈希不清除旧值；identity_json 只含
  本轮实际拿到的证据键、零证据时不动旧快照。
- 回填脚本跑过之后存量行的 content_hash 原样不动。
"""

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.emby_server import models as em
from backend.emby_server import mounts as mnt
from backend.emby_server import mount_drive
from backend.emby_server import mount_rclone
from backend.emby_server import scanner as scanner_mod
from backend.emby_server.mount_drive import DriveMount
from backend.emby_server.mount_rclone import RcloneMount, _pick_hash
from backend.emby_server.mounts import MountEntry, Pan115Mount
from backend.emby_server.scanner import ScanFile, _apply_scan_identity


@pytest.fixture(autouse=True)
def _no_listing_cache():
    mnt.invalidate_list_cache()
    yield
    mnt.invalidate_list_cache()


# ---------------- Drive ----------------

def test_drive_list_fields_include_hash_and_revision():
    # 精确值回归：共享盘列举带 etag 会 400（2026-10-25 挂载 3 中断），
    # 此处锁死字段串，误加 etag 即红。
    assert mount_drive._LIST_FIELDS == (
        "nextPageToken,files(id,name,mimeType,size,modifiedTime,trashed,"
        "md5Checksum,headRevisionId,version)")
    assert "etag" not in mount_drive._LIST_FIELDS


def _drive_provider(monkeypatch, items):
    provider = DriveMount(SimpleNamespace(
        id=99, name="gdrive", mount_type="drive",
        path="drive:DRIVEID", config={}))
    monkeypatch.setattr(provider, "_list_all", lambda fid: items)
    return provider


def test_drive_entries_map_md5_etag_revision(monkeypatch):
    items = [
        {"id": "f1", "name": "a.mkv", "mimeType": "video/x-matroska",
         "size": "100", "modifiedTime": "2024-01-02T03:04:05.000Z",
         "md5Checksum": "md5aaa", "etag": "etag1",
         "headRevisionId": "rev9", "version": "7"},
        {"id": "f2", "name": "doc", "mimeType": "application/vnd.google-apps.document",
         "modifiedTime": "2024-01-02T03:04:05.000Z"},
        {"id": "d1", "name": "sub", "mimeType": "application/vnd.google-apps.folder",
         "modifiedTime": "2024-01-02T03:04:05.000Z",
         "md5Checksum": "should-be-ignored"},
    ]
    provider = _drive_provider(monkeypatch, items)
    by_name = {e.name: e for e in provider._entries_of("root", "", cache_children=False)}
    f = by_name["a.mkv"]
    assert f.content_hash == "md5aaa" and f.content_hash_algo == "md5"
    assert f.etag == "etag1" and f.revision == "rev9"
    assert f.file_id == "f1"
    doc = by_name["doc"]
    assert doc.content_hash == "" and doc.content_hash_algo == ""
    d = by_name["sub"]
    assert d.is_dir and d.file_id == "" and d.content_hash == ""


def test_drive_walk_media_carries_hash(monkeypatch):
    provider = DriveMount(SimpleNamespace(
        id=99, name="gdrive", mount_type="drive",
        path="drive:DRIVEID", config={}))
    entry = MountEntry(name="a.mkv", rel="/a.mkv", is_dir=False, size=100,
                       file_id="f1", content_hash="md5aaa", content_hash_algo="md5",
                       etag="etag1", revision="rev9")
    monkeypatch.setattr(provider, "_fid_of", lambda root: "root")
    monkeypatch.setattr(provider, "_list_by_id", lambda fid: [entry])
    files = list(provider.walk_media(root="/"))
    assert len(files) == 1
    mf = files[0]
    assert mf.content_hash == "md5aaa" and mf.content_hash_algo == "md5"
    assert mf.etag == "etag1" and mf.revision == "rev9"


# ---------------- rclone ----------------

@pytest.mark.parametrize(("hashes", "expected"), [
    ({"md5": "m", "sha1": "s", "sha256": "h"}, ("m", "md5")),
    ({"sha1": "s", "sha256": "h"}, ("s", "sha1")),
    ({"sha256": "h"}, ("h", "sha256")),
    ({"sha1": ""}, ("", "")),
    ({}, ("", "")),
])
def test_pick_hash_preference(hashes, expected):
    assert _pick_hash(hashes) == expected


def _rclone_mount(config, path="rclone:gdrive/Movies"):
    return RcloneMount(em.StorageMount(
        name="测试 rclone", mount_type="rclone", path=path,
        config=mnt.dump_config(config), is_enabled=True))


def test_rclone_cli_lsjson_passes_hash_flag(monkeypatch, tmp_path):
    conf = tmp_path / "rclone.conf"
    conf.write_text("[gdrive]\ntype = drive\n", encoding="utf-8")
    monkeypatch.setattr(mount_rclone, "CONF_PATH", str(conf))
    commands = []

    def fake_run(cmd, **_kwargs):
        commands.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=b"[]", stderr=b"")

    monkeypatch.setattr(mount_rclone.subprocess, "run", fake_run)
    monkeypatch.setattr(mount_rclone.shutil, "which", lambda _b: "/usr/bin/rclone")
    provider = _rclone_mount({"mode": "cli"})
    assert provider._cli_lsjson("/") == []
    assert commands and commands[0][:2] == ["rclone", "lsjson"]
    assert "--hash" in commands[0]


def test_rclone_list_dir_maps_hashes_with_preference(monkeypatch):
    provider = _rclone_mount({"mode": "cli"})
    monkeypatch.setattr(provider, "_heal_fs", lambda: None)
    monkeypatch.setattr(provider, "_cli_lsjson", lambda rel: [
        {"Path": "a.mkv", "Name": "a.mkv", "Size": 10, "IsDir": False,
         "ModTime": "2024-01-02T03:04:05.000Z", "ID": "id-a",
         "Hashes": {"sha1": "s1", "md5": "m1"}},
        {"Path": "b.mkv", "Name": "b.mkv", "Size": 20, "IsDir": False,
         "ModTime": "2024-01-02T03:04:05.000Z", "ID": "id-b",
         "Hashes": {"sha256": "h2"}},
        {"Path": "sub", "Name": "sub", "Size": 0, "IsDir": True,
         "ModTime": "2024-01-02T03:04:05.000Z", "ID": ""},
    ])
    by_name = {e.name: e for e in provider.list_dir("/")}
    assert (by_name["a.mkv"].content_hash, by_name["a.mkv"].content_hash_algo) == ("m1", "md5")
    assert (by_name["b.mkv"].content_hash, by_name["b.mkv"].content_hash_algo) == ("h2", "sha256")
    assert by_name["sub"].content_hash == ""


def test_rclone_rc_mode_hash_is_null(monkeypatch):
    """RC 模式钉死 null 分支：PR-1 实测 operations/list 两个候选 opt 均
    不成立（hash:true 被忽略、hashTypes 报错），故不取哈希、不编参数。"""
    provider = _rclone_mount({"mode": "rc", "fs": "gdrive:Movies"})
    monkeypatch.setattr(provider, "_heal_fs", lambda: None)
    seen = {}

    def fake_rc_list(rc_url, fs, remote, **_kwargs):
        seen["called"] = True
        return [{"Path": "a.mkv", "Name": "a.mkv", "Size": 10, "IsDir": False,
                 "ModTime": "2024-01-02T03:04:05.000Z", "ID": "id-a"}]

    monkeypatch.setattr(mount_rclone, "_rc_list_items", fake_rc_list)
    entries = provider.list_dir("/")
    assert seen.get("called") is True
    assert len(entries) == 1
    assert entries[0].content_hash == "" and entries[0].content_hash_algo == ""


def test_generic_walk_carries_hash(monkeypatch):
    """通用 walk（MountEntry→MountFile）透传哈希：经 rclone CLI 端到端。"""
    provider = _rclone_mount({"mode": "cli"})
    monkeypatch.setattr(provider, "_heal_fs", lambda: None)

    def fake_lsjson(rel):
        if rel == "/":
            return [{"Path": "a.mkv", "Name": "a.mkv", "Size": 10, "IsDir": False,
                     "ModTime": "2024-01-02T03:04:05.000Z", "ID": "id-a",
                     "Hashes": {"md5": "m1"}}]
        return []

    monkeypatch.setattr(provider, "_cli_lsjson", fake_lsjson)
    files = list(provider.walk_media(root="/"))
    assert len(files) == 1
    assert files[0].content_hash == "m1" and files[0].content_hash_algo == "md5"


# ---------------- 115（缺位钉死） ----------------

def _pan115_provider(monkeypatch, raw_items):
    provider = Pan115Mount(SimpleNamespace(path="115:/", config={}),
                           cookie_override="COOKIE-TEST")
    monkeypatch.setattr(provider, "_cid_of", lambda rel: "0")
    monkeypatch.setattr(provider, "_raw_list", lambda cid: list(raw_items))
    return provider


def test_115_walk_and_list_dir_hash_is_null(monkeypatch):
    """115 sha1 至今未实测（生产无 115 挂载、密封凭据绝不能挪来探测），
    在拿到真实 /files 响应前哈希一律缺位；pickcode 绝不冒充哈希。"""
    raw = [{"name": "a.mkv", "is_dir": False, "size": 10,
            "cid": "c1", "pickcode": "pc1"},
           {"name": "sub", "is_dir": True, "size": 0, "cid": "c2"}]
    provider = _pan115_provider(monkeypatch, raw)
    # 夹具按 cid 分流：根目录列出两项，子目录为空（否则同一批 raw 会
    # 在每个 cid 下重复产出，属夹具失真、非代码问题）。
    monkeypatch.setattr(provider, "_raw_list",
                        lambda cid: list(raw) if cid == "0" else [])
    files = list(provider.walk_media(root="/"))
    assert len(files) == 1
    assert files[0].content_hash == "" and files[0].content_hash_algo == ""
    entries = provider.list_dir("/")
    assert all(e.content_hash == "" for e in entries)


# ---------------- 写库 helper ----------------

def _item_with(**kwargs):
    item = em.MediaItem()
    for key, value in kwargs.items():
        setattr(item, key, value)
    return item


def _scanfile(**kwargs):
    base = {"stored_path": "/x/a.mkv", "name": "a.mkv", "local_dir": None}
    base.update(kwargs)
    return ScanFile(**base)


def test_apply_scan_identity_writes_columns_and_json():
    item = _item_with(content_hash=None, content_hash_algo=None, identity_json=None)
    sf = _scanfile(content_hash="md5aaa", content_hash_algo="md5",
                   etag="etag1", revision="rev9")
    _apply_scan_identity(item, sf)
    assert item.content_hash == "md5aaa" and item.content_hash_algo == "md5"
    assert json.loads(item.identity_json) == {
        "content_hash": "md5aaa", "content_hash_algo": "md5",
        "etag": "etag1", "revision": "rev9"}


def test_apply_scan_identity_absent_hash_keeps_existing():
    item = _item_with(content_hash="old", content_hash_algo="md5",
                      identity_json='{"content_hash": "old"}')
    _apply_scan_identity(item, _scanfile())
    assert item.content_hash == "old" and item.content_hash_algo == "md5"
    assert item.identity_json == '{"content_hash": "old"}'


def test_apply_scan_identity_partial_evidence_only_present_keys():
    item = _item_with(content_hash=None, content_hash_algo=None, identity_json=None)
    _apply_scan_identity(item, _scanfile(content_hash="h2", content_hash_algo="sha256"))
    assert json.loads(item.identity_json) == {
        "content_hash": "h2", "content_hash_algo": "sha256"}


# ---------------- 回填脚本不碰哈希 ----------------

def _make_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    em.Base.metadata.create_all(engine, tables=[
        em.Library.__table__, em.MediaItem.__table__])
    return sessionmaker(bind=engine)()


def test_backfill_script_leaves_hash_untouched(tmp_path):
    from scripts.backfill_scan_identity import backfill_scan_identity

    s = _make_session(tmp_path)
    lib = em.Library(guid="lib-m", name="m", collection_type="movies")
    s.add(lib)
    s.flush()
    row = em.MediaItem(guid="g1", library_id=lib.id, item_type="movie",
                       name="x", file_path="/movies/肖申克的救赎 (1994).mkv",
                       content_hash="keepme", content_hash_algo="md5")
    s.add(row)
    s.commit()
    backfill_scan_identity(s)
    s.refresh(row)
    assert row.scan_title == "肖申克的救赎"
    assert row.content_hash == "keepme" and row.content_hash_algo == "md5"
    assert row.identity_json is None
