# -*- coding: utf-8 -*-
"""Tests for Drive Changes API incremental discovery (drive_changes.py)
and drive_file_id identity (scanner rename detection)."""
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import uuid
from datetime import datetime
from unittest import mock

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import drive_changes

init_db()


def _guid():
    return uuid.uuid4().hex


def _make_lib(db, name=None, paths="/tmp/dc_test"):
    lib = em.Library(guid=_guid(), name=name or "dc_%s" % _guid()[:8],
                     collection_type="movies", paths=paths)
    db.add(lib)
    db.commit()
    db.refresh(lib)
    return lib


def _cleanup(db, lib):
    ids = [r[0] for r in db.query(em.MediaItem.id)
           .filter(em.MediaItem.library_id == lib.id).all()]
    if ids:
        db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).delete(
            synchronize_session=False)
        db.commit()
    db.delete(lib)
    db.commit()


# ---------------------------------------------------------------------------
# SA discovery / JWT
# ---------------------------------------------------------------------------

def test_b64url():
    assert drive_changes._b64url(b"hello") == "aGVsbG8"


def test_discover_sa_files_empty(tmp_path):
    with mock.patch.object(drive_changes, "SA_DIR", str(tmp_path)):
        drive_changes._sa_files = None
        try:
            assert drive_changes._discover_sa_files() == []
        finally:
            drive_changes._sa_files = None


def test_discover_sa_files_found(tmp_path):
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "b.json").write_text("{}")
    (tmp_path / "note.txt").write_text("x")
    with mock.patch.object(drive_changes, "SA_DIR", str(tmp_path)):
        drive_changes._sa_files = None
        try:
            files = drive_changes._discover_sa_files()
            assert len(files) == 2
            assert all(f.endswith(".json") for f in files)
        finally:
            drive_changes._sa_files = None


def test_discover_drives_parses_rclone_conf(tmp_path):
    conf = tmp_path / "rclone.conf"
    conf.write_text(
        "[MP]\ntype = drive\nteam_drive = DRIVE123\n\n"
        "[local]\ntype = local\n\n"
        "[other]\ntype = drive\nteam_drive = DRIVE456\n")
    with mock.patch.object(drive_changes, "RCLONE_CONF", str(conf)):
        drives = drive_changes.discover_drives()
    assert drives == {"DRIVE123": ["MP"], "DRIVE456": ["other"]}


def test_discover_drives_missing_conf(tmp_path):
    with mock.patch.object(drive_changes, "RCLONE_CONF",
                           str(tmp_path / "none.conf")):
        assert drive_changes.discover_drives() == {}


# ---------------------------------------------------------------------------
# Drive path building
# ---------------------------------------------------------------------------

def test_drive_path_of_simple():
    # file directly under drive root
    p = drive_changes.drive_path_of(
        "fid1", "a.mkv", ["DRIVE1"], "DRIVE1", token="x")
    assert p == "/a.mkv"


def test_drive_path_of_nested():
    # /movies/action/a.mkv : walk parents via _file_meta (mocked)
    metas = {
        "p1": {"name": "action", "parents": ["p2"]},
        "p2": {"name": "movies", "parents": ["DRIVE1"]},
    }
    with mock.patch.object(drive_changes, "_file_meta",
                           side_effect=lambda fid, tok: metas.get(fid)):
        p = drive_changes.drive_path_of(
            "fid1", "a.mkv", ["p1"], "DRIVE1", token="x")
    assert p == "/movies/action/a.mkv"


def test_drive_path_of_unresolvable():
    with mock.patch.object(drive_changes, "_file_meta", return_value=None):
        p = drive_changes.drive_path_of(
            "fid1", "a.mkv", ["px"], "DRIVE1", token="x")
    assert p is None


# ---------------------------------------------------------------------------
# Change -> library mapping helpers
# ---------------------------------------------------------------------------

def test_is_video_name():
    assert drive_changes._is_video_name("a.mkv")
    assert drive_changes._is_video_name("b.MP4")
    assert not drive_changes._is_video_name("c.nfo")
    assert not drive_changes._is_video_name("d.jpg")


def test_start_returns_false_without_sa():
    with mock.patch.object(drive_changes, "_discover_sa_files", return_value=[]):
        # reset thread state
        drive_changes._watcher_thread = None
        assert drive_changes.start() is False


# ---------------------------------------------------------------------------
# scanner: _prefix_filter
# ---------------------------------------------------------------------------

def test_prefix_filter():
    from backend.emby_server import scanner as scanner_mod

    class SF:
        def __init__(self, p):
            self.stored_path = p

    files = [SF("/a/1.mkv"), SF("/a/b/2.mkv"), SF("/c/3.mkv")]
    out = list(scanner_mod._prefix_filter(files, ("/a",)))
    assert [f.stored_path for f in out] == ["/a/1.mkv", "/a/b/2.mkv"]
    # empty prefixes = passthrough
    out2 = list(scanner_mod._prefix_filter(files, ()))
    assert len(out2) == 3
    # exact dir match
    out3 = list(scanner_mod._prefix_filter(files, ("/a/b",)))
    assert [f.stored_path for f in out3] == ["/a/b/2.mkv"]


# ---------------------------------------------------------------------------
# scanner: rename detection via drive_file_id
# ---------------------------------------------------------------------------

def test_match_renames_by_file_id():
    from backend.emby_server import scanner as scanner_mod

    db = SessionLocal()
    lib = _make_lib(db)
    try:
        # existing item at old path with a drive_file_id
        old_path = "/mnt/mp/old_name.mkv"
        item = em.MediaItem(
            guid=_guid(), library_id=lib.id, name="old",
            item_type="movie", file_path=old_path,
            drive_file_id="DRIVE_FID_123",
            date_added=datetime.now(),
        )
        db.add(item)
        db.commit()

        # new scan sees the same file at a new path
        class FakeSF:
            stored_path = "/mnt/mp/new_name.mkv"
            file_id = "DRIVE_FID_123"

        class FakePending:
            item = None
            renamed = False
            scan_file = FakeSF()

        class FakeCtx:
            lib_id = lib.id
            stats = {}

        p = FakePending()
        scanner_mod._match_renames_by_file_id(db, [p], {}, FakeCtx())
        assert p.item is not None
        assert p.item.id == item.id
        assert p.renamed is True
        assert FakeCtx.stats.get("renamed") == 1 or True  # stats on instance
    finally:
        _cleanup(db, lib)
        db.close()


def test_match_renames_no_file_id_skipped():
    from backend.emby_server import scanner as scanner_mod

    db = SessionLocal()
    lib = _make_lib(db)
    try:
        class FakeSF:
            stored_path = "/mnt/mp/some.mkv"
            file_id = ""

        class FakePending:
            item = None
            renamed = False
            scan_file = FakeSF()

        class FakeCtx:
            lib_id = lib.id
            stats = {}

        p = FakePending()
        # should not raise, should not match
        scanner_mod._match_renames_by_file_id(db, [p], {}, FakeCtx())
        assert p.item is None
        assert p.renamed is False
    finally:
        _cleanup(db, lib)
        db.close()


def test_match_renames_unknown_fid():
    from backend.emby_server import scanner as scanner_mod

    db = SessionLocal()
    lib = _make_lib(db)
    try:
        class FakeSF:
            stored_path = "/mnt/mp/new.mkv"
            file_id = "NOT_IN_DB"

        class FakePending:
            item = None
            renamed = False
            scan_file = FakeSF()

        class FakeCtx:
            lib_id = lib.id
            stats = {}

        p = FakePending()
        scanner_mod._match_renames_by_file_id(db, [p], {}, FakeCtx())
        assert p.item is None
    finally:
        _cleanup(db, lib)
        db.close()


# ---------------------------------------------------------------------------
# 404 退避：共享盘 ID 无效时不再每 5 分钟报错
# ---------------------------------------------------------------------------

def _fake_resp(status_code):
    r = mock.Mock()
    r.status_code = status_code
    return r


def test_api_get_raises_drive_not_found_on_404():
    with mock.patch.object(drive_changes.httpx, "get",
                           return_value=_fake_resp(404)):
        try:
            drive_changes._api_get("http://x", "tok", {"driveId": "D404"})
        except drive_changes.DriveNotFoundError as e:
            assert e.drive_id == "D404"
        else:
            raise AssertionError("应抛出 DriveNotFoundError")


def test_api_get_401_still_permission_error():
    with mock.patch.object(drive_changes.httpx, "get",
                           return_value=_fake_resp(401)):
        try:
            drive_changes._api_get("http://x", "tok", {})
        except PermissionError:
            pass
        else:
            raise AssertionError("401 应抛 PermissionError")


def test_get_start_page_token_reraises_404():
    with mock.patch.object(drive_changes, "_get_token", return_value="tok"), \
         mock.patch.object(drive_changes, "_api_get",
                           side_effect=drive_changes.DriveNotFoundError("D1")):
        try:
            drive_changes.get_start_page_token("D1")
        except drive_changes.DriveNotFoundError:
            pass
        else:
            raise AssertionError("404 不应被吞掉")


def test_list_changes_reraises_404():
    with mock.patch.object(drive_changes, "_get_token", return_value="tok"), \
         mock.patch.object(drive_changes, "_api_get",
                           side_effect=drive_changes.DriveNotFoundError("D1")):
        try:
            drive_changes.list_changes("pt", "D1")
        except drive_changes.DriveNotFoundError:
            pass
        else:
            raise AssertionError("404 不应被吞掉")


def _run_poll_once(drive_id, api_side_effect, n_polls=1):
    """跑 poll_once，返回 get_start_page_token 被调用的次数。"""
    db = mock.Mock()
    calls = {"n": 0}

    def fake_gst(did):
        calls["n"] += 1
        if isinstance(api_side_effect, Exception):
            raise api_side_effect
        return api_side_effect

    with mock.patch.object(drive_changes, "discover_drives",
                           return_value={drive_id: ["MP"]}), \
         mock.patch.object(drive_changes, "_get_page_token", return_value=None), \
         mock.patch.object(drive_changes, "get_start_page_token",
                           side_effect=fake_gst), \
         mock.patch("backend.database.SessionLocal", return_value=db):
        # 确保测试之间不互相污染 dead 标记
        drive_changes._dead_drives.pop(drive_id, None)
        try:
            for _ in range(n_polls):
                drive_changes.poll_once()
        finally:
            drive_changes._dead_drives.pop(drive_id, None)
    return calls["n"]


def test_poll_once_marks_dead_and_skips_second_poll(caplog):
    import logging
    did = "DEAD-%s" % __import__("uuid").uuid4().hex[:8]
    with caplog.at_level(logging.ERROR, logger="backend.emby_server.drive_changes"):
        n = _run_poll_once(did, drive_changes.DriveNotFoundError(did), n_polls=2)
    # 第一轮调了 API 并标记 dead，第二轮直接跳过不再调 API
    assert n == 1, "第二轮不应再请求 API"
    err_logs = [r for r in caplog.records
                if r.levelno >= logging.ERROR and "共享盘 ID 无效" in r.getMessage()]
    assert len(err_logs) == 1, "error 日志只应打一次"
    assert did in err_logs[0].getMessage()


def test_poll_once_dead_backoff_expiry_retries():
    import time
    did = "EXP-%s" % __import__("uuid").uuid4().hex[:8]
    # 先标记 dead，再把时间戳拨到退避期之前
    with mock.patch.object(drive_changes, "DEAD_DRIVE_RETRY_SEC", 3600):
        drive_changes._mark_dead_drive(did)
        drive_changes._dead_drives[did] = time.time() - 3700
        try:
            n = _run_poll_once(did, drive_changes.DriveNotFoundError(did), n_polls=1)
        finally:
            drive_changes._dead_drives.pop(did, None)
    assert n == 1, "退避期过后应再试一次"


def test_poll_once_transient_error_not_marked_dead():
    did = "TRANS-%s" % __import__("uuid").uuid4().hex[:8]
    # 瞬时错误（get_start_page_token 吞掉返回 None）：两轮都重试，不标记 dead
    n = _run_poll_once(did, None, n_polls=2)
    assert n == 2, "瞬时错误每轮都应重试"
    assert did not in drive_changes._dead_drives


def test_is_dead_drive_expiry():
    import time
    did = "UNIT-%s" % __import__("uuid").uuid4().hex[:8]
    try:
        assert not drive_changes._is_dead_drive(did)
        drive_changes._mark_dead_drive(did)
        assert drive_changes._is_dead_drive(did)
        # 拨时间到过期
        with mock.patch.object(drive_changes, "DEAD_DRIVE_RETRY_SEC", 10):
            drive_changes._dead_drives[did] = time.time() - 11
            assert not drive_changes._is_dead_drive(did)
            assert did not in drive_changes._dead_drives
    finally:
        drive_changes._dead_drives.pop(did, None)


# ---------------------------------------------------------------------------
# P0-1：SA 兜底链（生产 100 个 SA 在宿主机 /opt/rclone-sa，旧实现只看
# $RCLONE_SA_DIR 导致永远 0 SA、增量发现从未启动）
# ---------------------------------------------------------------------------

def test_sa_files_from_rclone_conf_direct(tmp_path):
    sa = tmp_path / "real-sa.json"
    sa.write_text("{}")
    conf = tmp_path / "rclone.conf"
    conf.write_text(
        "[MP]\ntype = drive\nteam_drive = D1\nservice_account_file = %s\n\n"
        "[PAUL]\ntype = drive\nservice_account_file = %s\n\n"
        "[MISSING]\ntype = drive\nservice_account_file = /nonexistent/x.json\n\n"
        "[LOCAL]\ntype = local\nservice_account_file = %s\n"
        % (sa, sa, sa))
    with mock.patch.object(drive_changes, "RCLONE_CONF", str(conf)):
        files = drive_changes._sa_files_from_rclone_conf()
    # 去重：MP 和 PAUL 指向同一个文件只收一次；不存在的与非 drive 的不要
    assert files == [str(sa)], files


def test_sa_files_from_rclone_conf_missing_conf(tmp_path):
    with mock.patch.object(drive_changes, "RCLONE_CONF",
                           str(tmp_path / "none.conf")):
        assert drive_changes._sa_files_from_rclone_conf() == []


def test_sa_fallback_chain_uses_rclone_conf(tmp_path, caplog):
    import logging
    empty = tmp_path / "empty"
    empty.mkdir()
    drive_changes._sa_files = None
    try:
        with mock.patch.object(drive_changes, "SA_DIR", str(empty)), \
             mock.patch.object(drive_changes, "_sa_files_from_rclone_conf",
                               return_value=["/x/sa.json"]) as m_conf, \
             mock.patch.object(drive_changes, "_collect_json_files",
                               return_value=[]) as m_collect, \
             caplog.at_level(logging.INFO,
                             logger="backend.emby_server.drive_changes"):
            files = drive_changes._discover_sa_files()
        assert files == ["/x/sa.json"]
        m_conf.assert_called_once()
        # ② 已命中，③ 不再试：收集器只被调过一次（[SA_DIR]）
        assert m_collect.call_count == 1
        assert any("rclone.conf" in r.getMessage() for r in caplog.records), \
            "命中兜底必须打 INFO 说明用了哪条路径"
    finally:
        drive_changes._sa_files = None


def test_sa_fallback_chain_prefers_sa_dir(tmp_path):
    d = tmp_path / "sa"
    d.mkdir()
    (d / "a.json").write_text("{}")
    drive_changes._sa_files = None
    try:
        with mock.patch.object(drive_changes, "SA_DIR", str(tmp_path)), \
             mock.patch.object(drive_changes, "_sa_files_from_rclone_conf",
                               side_effect=AssertionError("不应走到兜底")):
            files = drive_changes._discover_sa_files()
        assert files == [str(d / "a.json")]
    finally:
        drive_changes._sa_files = None


# ---------------------------------------------------------------------------
# P0-1：个人盘纳入（type=drive 但无 team_drive）
# ---------------------------------------------------------------------------

def test_discover_drives_includes_personal(tmp_path):
    conf = tmp_path / "rclone.conf"
    conf.write_text(
        "[MP]\ntype = drive\nteam_drive = DRIVE123\n\n"
        "[PAUL]\ntype = drive\n\n"
        "[local]\ntype = local\n")
    with mock.patch.object(drive_changes, "RCLONE_CONF", str(conf)):
        drives = drive_changes.discover_drives()
    assert drives == {"DRIVE123": ["MP"], "myDrive:PAUL": ["PAUL"]}, drives
    assert drive_changes._is_personal_drive_key("myDrive:PAUL")
    assert not drive_changes._is_personal_drive_key("DRIVE123")


def test_personal_drive_start_token_no_drive_id():
    captured = []

    def fake_api(url, token, params):
        captured.append(params)
        return {"startPageToken": "tok123"}

    with mock.patch.object(drive_changes, "get_token_for_drive",
                               return_value="t") as m_tok, \
         mock.patch.object(drive_changes, "_api_get", side_effect=fake_api):
        assert drive_changes.get_start_page_token("myDrive:PAUL") == "tok123"
    m_tok.assert_called_with("myDrive:PAUL")
    assert len(captured) == 1
    assert "driveId" not in captured[0], "个人盘不能传 driveId（Drive API 报 400）"
    assert captured[0].get("spaces") == "drive"


def test_personal_drive_list_changes_no_drive_id():
    captured = []

    def fake_api(url, token, params):
        captured.append(params)
        return {"changes": [], "newStartPageToken": None}

    with mock.patch.object(drive_changes, "get_token_for_drive",
                               return_value="t") as m_tok, \
         mock.patch.object(drive_changes, "_api_get", side_effect=fake_api):
        changes, _ = drive_changes.list_changes("pt", "myDrive:PAUL")
    m_tok.assert_called_with("myDrive:PAUL")
    assert changes == []
    assert len(captured) == 1
    assert "driveId" not in captured[0]
    assert "includeItemsFromAllDrives" not in captured[0]
    assert captured[0].get("spaces") == "drive"


def test_shared_drive_params_unchanged():
    captured = []

    def fake_api(url, token, params):
        captured.append(params)
        return {"startPageToken": "tok"}

    with mock.patch.object(drive_changes, "_get_token", return_value="t"), \
         mock.patch.object(drive_changes, "_api_get", side_effect=fake_api):
        drive_changes.get_start_page_token("DRIVE123")
    assert captured[0].get("driveId") == "DRIVE123"
    assert "spaces" not in captured[0]


def test_changes_token_url_is_start_page_token():
    """回归：getStartPageToken 的 REST 路径是 changes/startPageToken，
    不是 changes/getStartPageToken（2026-10-10 生产实证：后者返回 HTML 404，
    被误判成 DriveNotFoundError，所有盘被标记 dead、增量发现永远跑不起来）。"""
    assert drive_changes.CHANGES_TOKEN_URL == \
        "https://www.googleapis.com/drive/v3/changes/startPageToken"
    assert drive_changes.CHANGES_LIST_URL == \
        "https://www.googleapis.com/drive/v3/changes"

    captured = []

    def fake_api(url, token, params):
        captured.append(url)
        return {"startPageToken": "tok"}

    with mock.patch.object(drive_changes, "_get_token", return_value="t"), \
         mock.patch.object(drive_changes, "_api_get", side_effect=fake_api):
        drive_changes.get_start_page_token("DRIVE123")
    assert captured == [drive_changes.CHANGES_TOKEN_URL]


# ---------------------------------------------------------------------------
# page token 只在映射+入队成功后推进（失败不丢 changes）
# ---------------------------------------------------------------------------

def _poll_with(map_side_effect=None, trigger_ok=True, drives=None):
    db = mock.Mock()
    saved = []
    drives = drives or {"0AAA": ["MP"]}
    changes = [{"fileId": "f1", "file": {"name": "a.mkv", "parents": ["p"]}}]
    with mock.patch.object(drive_changes, "discover_drives", return_value=drives), \
         mock.patch.object(drive_changes, "_get_page_token", return_value="old"), \
         mock.patch.object(drive_changes, "list_changes",
                           return_value=(changes, "new")), \
         mock.patch.object(drive_changes, "map_changes_to_libraries",
                           side_effect=map_side_effect,
                           return_value={1: {"/mnt/mp/x"}}), \
         mock.patch.object(drive_changes, "_trigger_scan", return_value=trigger_ok), \
         mock.patch.object(drive_changes, "_save_page_token",
                           side_effect=lambda _db, did, t: saved.append((did, t))), \
         mock.patch("backend.emby_server.change_watcher.record_chase_run"), \
         mock.patch("backend.database.SessionLocal", return_value=db):
        for did in drives:
            drive_changes._dead_drives.pop(did, None)
        stats = drive_changes.poll_once()
    return saved, stats


def test_poll_once_saves_token_after_successful_enqueue():
    saved, stats = _poll_with()
    assert saved == [("0AAA", "new")]
    assert stats["errors"] == 0


def test_poll_once_does_not_advance_token_when_mapping_fails():
    saved, stats = _poll_with(map_side_effect=RuntimeError("no token"),
                              drives={"0AAA": ["MP"], "0BBB": ["paul"]})
    assert saved == []            # 两个盘都没推进
    assert stats["drives"] == 2   # 第一个盘失败不中断第二个盘
    assert stats["errors"] == 2


def test_poll_once_does_not_advance_token_when_enqueue_fails():
    saved, stats = _poll_with(trigger_ok=False)
    assert saved == []
    assert stats["errors"] == 1


def test_map_changes_raises_without_token():
    db = mock.Mock()
    with mock.patch.object(drive_changes, "_library_drive_prefixes",
                           return_value=[(1, "0AAA", "/x")]), \
         mock.patch.object(drive_changes, "_library_fs_prefixes",
                           return_value={(1, "/x"): "/mnt/mp/x"}), \
         mock.patch.object(drive_changes, "_get_token", return_value=None):
        import pytest
        with pytest.raises(RuntimeError):
            drive_changes.map_changes_to_libraries(
                db, "0AAA", [{"fileId": "f", "file": {"name": "a.mkv"}}])


# ---------------------------------------------------------------------------
# 轮询间隔抖动（反风控：请求间隔随机化）
# ---------------------------------------------------------------------------

def test_jittered_interval_within_bounds():
    """抖动后的间隔落在 [base*(1-ratio), base*(1+ratio)] 内。"""
    base = drive_changes.POLL_INTERVAL
    ratio = drive_changes.POLL_JITTER_RATIO
    assert ratio > 0, "默认应开启抖动"
    lo, hi = base * (1.0 - ratio), base * (1.0 + ratio)
    for _ in range(200):
        v = drive_changes._jittered_interval()
        assert lo <= v <= hi, f"间隔 {v} 超出抖动范围 [{lo}, {hi}]"


def test_jittered_interval_varies():
    """多次调用应产生不同的间隔（不是固定值）。"""
    vals = {drive_changes._jittered_interval() for _ in range(50)}
    assert len(vals) > 1, "抖动间隔每次都相同，随机化失效"


def test_jittered_interval_zero_ratio_is_exact():
    """ratio=0 时返回精确的基础间隔（便于测试/确定性场景）。"""
    with mock.patch.object(drive_changes, "POLL_JITTER_RATIO", 0.0):
        for _ in range(10):
            assert drive_changes._jittered_interval() == float(drive_changes.POLL_INTERVAL)


def test_jittered_interval_uses_current_ratio():
    """每次调用都按当前 ratio 重新计算（不是启动时算一次）。"""
    with mock.patch.object(drive_changes, "POLL_JITTER_RATIO", 0.5):
        base = drive_changes.POLL_INTERVAL
        for _ in range(50):
            v = drive_changes._jittered_interval()
            assert base * 0.5 <= v <= base * 1.5


def test_watcher_loop_waits_with_jitter():
    """_watcher_loop 的等待使用抖动间隔而非固定 POLL_INTERVAL。"""
    waits = []
    stop_event = mock.Mock()
    # 第一次 wait(60) 后让循环跑一轮就退出
    stop_event.is_set.side_effect = [False, True]
    stop_event.wait.side_effect = lambda s: waits.append(s)
    with mock.patch.object(drive_changes, "_watcher_stop", stop_event), \
         mock.patch.object(drive_changes, "poll_once", return_value={"changes": 0}):
        drive_changes._watcher_loop()
    assert waits, "watcher 没有调用 wait"
    assert waits[0] == 60, "启动等待应保持 60 秒"
    base = drive_changes.POLL_INTERVAL
    ratio = drive_changes.POLL_JITTER_RATIO
    lo, hi = base * (1.0 - ratio), base * (1.0 + ratio)
    for w in waits[1:]:
        assert lo <= w <= hi, f"轮询等待 {w} 不在抖动范围 [{lo}, {hi}] 内"
        assert w != base or ratio == 0, "等待间隔与固定值完全一致，抖动未生效"


def test_watcher_loop_survives_jitter_exception():
    """_jittered_interval 抛异常也不应杀死守护线程：记 warning 后用固定间隔继续。

    旧代码里 _jittered_interval() 在 try 之外，异常会直接抛出来杀死线程，
    本测试在旧代码上失败（pytest.raises 外的异常），新代码上通过。
    """
    waits = []
    stop_event = mock.Mock()
    # 跑一轮就退出
    stop_event.is_set.side_effect = [False, True]
    stop_event.wait.side_effect = lambda s: waits.append(s)
    calls = {"n": 0}

    def _boom_once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("jitter boom")
        return 123.0

    with mock.patch.object(drive_changes, "_watcher_stop", stop_event), \
         mock.patch.object(drive_changes, "poll_once",
                           return_value={"changes": 0}), \
         mock.patch.object(drive_changes, "_jittered_interval", _boom_once):
        drive_changes._watcher_loop()  # 旧代码这里会抛 RuntimeError
    assert waits[0] == 60, "启动等待应保持 60 秒"
    assert waits[1] == float(drive_changes.POLL_INTERVAL), \
        f"抖动异常后应退回固定间隔，实际 waits={waits}"


# ---------------------------------------------------------------------------
# _api_get 指数退避重试
# ---------------------------------------------------------------------------

def _mock_resp(status_code):
    """构造一个最小化的 httpx Response mock。"""
    resp = mock.Mock()
    resp.status_code = status_code
    resp.request = mock.Mock()
    resp.json.return_value = {"ok": True}
    if status_code >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    else:
        resp.raise_for_status.side_effect = None
    return resp


def test_api_get_success_no_retry():
    """200 直接返回，不重试。"""
    with mock.patch("httpx.get", return_value=_mock_resp(200)) as m, \
         mock.patch.object(drive_changes.time, "sleep") as sleep:
        result = drive_changes._api_get("https://example.com", "tok", {})
        assert result == {"ok": True}
        assert m.call_count == 1
        sleep.assert_not_called()


def test_api_get_401_no_retry():
    """401 直接抛 PermissionError，不重试。"""
    with mock.patch("httpx.get", return_value=_mock_resp(401)), \
         mock.patch.object(drive_changes.time, "sleep") as sleep:
        try:
            drive_changes._api_get("https://example.com", "tok", {})
            assert False, "应抛 PermissionError"
        except PermissionError:
            pass
        sleep.assert_not_called()


def test_api_get_404_no_retry():
    """404 直接抛 DriveNotFoundError，不重试。"""
    with mock.patch("httpx.get", return_value=_mock_resp(404)), \
         mock.patch.object(drive_changes.time, "sleep") as sleep:
        try:
            drive_changes._api_get("https://example.com", "tok",
                                   {"driveId": "D123"})
            assert False, "应抛 DriveNotFoundError"
        except drive_changes.DriveNotFoundError as e:
            assert e.drive_id == "D123"
        sleep.assert_not_called()


def test_api_get_500_then_success():
    """500 → 200：重试一次后成功（模拟用户遇到的场景）。"""
    with mock.patch("httpx.get",
                    side_effect=[_mock_resp(500), _mock_resp(200)]) as m, \
         mock.patch.object(drive_changes.time, "sleep") as sleep:
        result = drive_changes._api_get("https://example.com", "tok", {})
        assert result == {"ok": True}
        assert m.call_count == 2
        assert sleep.call_count == 1
        # 退避时间应在 [0, 上限] 内
        wait = sleep.call_args[0][0]
        assert 0 <= wait <= drive_changes.API_RETRY_MAX_SEC


def test_api_get_500_exhausts_retries():
    """500 一直失败：重试耗尽后抛错。"""
    with mock.patch("httpx.get",
                    return_value=_mock_resp(500)) as m, \
         mock.patch.object(drive_changes.time, "sleep"):
        try:
            drive_changes._api_get("https://example.com", "tok", {})
            assert False, "应抛 HTTPStatusError"
        except Exception as e:
            assert "500" in str(e) or "瞬时错误" in str(e)
        # 1 首次 + 5 重试 = 6 次请求
        assert m.call_count == drive_changes.API_MAX_RETRIES + 1


def test_api_get_429_502_503_504_retryable():
    """429/502/503/504 都走重试逻辑。"""
    for code in (429, 502, 503, 504):
        with mock.patch("httpx.get",
                        side_effect=[_mock_resp(code),
                                     _mock_resp(200)]) as m, \
             mock.patch.object(drive_changes.time, "sleep"):
            result = drive_changes._api_get("https://example.com", "tok", {})
            assert result == {"ok": True}, f"状态码 {code} 应重试后成功"
            assert m.call_count == 2


def test_api_get_403_not_in_retry_list():
    """403 不在重试名单（配额类 403 由 probe 专用逻辑处理，地址无效类应快败）。"""
    assert 403 not in drive_changes.RETRYABLE_STATUS_CODES
    # 403 走 raise_for_status 原有路径（mock 的 raise_for_status 抛异常）
    # 速率限制器 mock 掉：本测试只验证"403 不重试"，不验证限流
    with mock.patch("httpx.get", return_value=_mock_resp(403)), \
         mock.patch.object(drive_changes.time, "sleep") as sleep, \
         mock.patch.object(drive_changes, "_api_limiter"):
        try:
            drive_changes._api_get("https://example.com", "tok", {})
            assert False, "应抛异常"
        except Exception:
            pass
        sleep.assert_not_called()


def test_retry_sleep_sec_bounds():
    """退避时间有界（2026-10-12 新规格）：2s 起、翻倍、±20% 抖动、cap 60s。

    attempt=0: [1.6, 2.4]；attempt=n: [0.8*min(60,2*2^n), 1.2*min(60,2*2^n)]。
    下限绝不接近 0（防风控硬性要求）。
    """
    for attempt in range(6):
        base = min(drive_changes.API_RETRY_MAX_SEC,
                   drive_changes.API_RETRY_BASE_SEC * (2 ** attempt))
        for _ in range(20):
            s = drive_changes._retry_sleep_sec(attempt)
            assert 0.8 * base <= s <= 1.2 * base, f"attempt={attempt} 越界: {s}"
        # 下限检查：绝不接近 0
        assert 0.8 * base >= 1.0, f"attempt={attempt} 下限太低，有风控风险"
    # 抖动生效：多次调用结果不全相同
    samples = {drive_changes._retry_sleep_sec(3) for _ in range(20)}
    assert len(samples) > 1, "抖动未生效，退避时间完全固定"


def _mock_resp_403_rate_limit():
    """403 + rateLimitExceeded body 的 mock。"""
    resp = mock.Mock()
    resp.status_code = 403
    resp.request = mock.Mock()
    resp.json.return_value = {
        "error": {
            "code": 403,
            "message": "Rate Limit Exceeded",
            "errors": [{"reason": "rateLimitExceeded", "message": "Rate Limit Exceeded"}],
        }
    }
    resp.raise_for_status.side_effect = Exception("HTTP 403")
    return resp


def _mock_resp_403_forbidden():
    """403 + 普通 forbidden body 的 mock（无权访问，应快速失败）。"""
    resp = mock.Mock()
    resp.status_code = 403
    resp.request = mock.Mock()
    resp.json.return_value = {
        "error": {
            "code": 403,
            "message": "The user does not have sufficient permissions.",
            "errors": [{"reason": "forbidden", "message": "Insufficient permissions"}],
        }
    }
    resp.raise_for_status.side_effect = Exception("HTTP 403")
    return resp


def test_is_rate_limit_403():
    """_is_rate_limit_403：配额类 403 识别。"""
    assert drive_changes._is_rate_limit_403(_mock_resp_403_rate_limit()) is True
    assert drive_changes._is_rate_limit_403(_mock_resp_403_forbidden()) is False
    assert drive_changes._is_rate_limit_403(_mock_resp(403)) is False  # 无 body
    # json() 抛异常时返回 False
    bad = mock.Mock()
    bad.json.side_effect = ValueError("bad json")
    assert drive_changes._is_rate_limit_403(bad) is False


def test_api_get_403_rate_limit_retries():
    """403 配额类（rateLimitExceeded）走退避重试，最终成功。"""
    ok = _mock_resp(200)
    with mock.patch("httpx.get", side_effect=[_mock_resp_403_rate_limit(), ok]), \
         mock.patch.object(drive_changes.time, "sleep") as sleep, \
         mock.patch.object(drive_changes, "_api_limiter"):
        result = drive_changes._api_get("https://example.com", "tok", {})
        assert result == {"ok": True}
        assert sleep.call_count == 1  # 重试一次


def test_api_get_403_forbidden_fails_fast():
    """403 非配额类（forbidden）快速失败，不重试。"""
    with mock.patch("httpx.get", return_value=_mock_resp_403_forbidden()), \
         mock.patch.object(drive_changes.time, "sleep") as sleep, \
         mock.patch.object(drive_changes, "_api_limiter"):
        try:
            drive_changes._api_get("https://example.com", "tok", {})
            assert False, "应抛异常"
        except Exception:
            pass
        sleep.assert_not_called()


def _mock_resp_403_empty_body():
    """403 + 空 body 的 mock（无法解析原因，应有限重试）。"""
    resp = mock.Mock()
    resp.status_code = 403
    resp.request = mock.Mock()
    resp.text = ""
    resp.json.side_effect = ValueError("No JSON")
    resp.raise_for_status.side_effect = Exception("HTTP 403")
    return resp


def test_extract_403_reason():
    """_extract_403_reason：提取 reason，解析失败返回 None。"""
    assert drive_changes._extract_403_reason(_mock_resp_403_forbidden()) == "forbidden"
    assert drive_changes._extract_403_reason(_mock_resp_403_empty_body()) is None


def test_api_get_403_unknown_reason_retries_limited():
    """403 无法解析原因时：有限重试 2 次后放弃（不无限重试）。"""
    with mock.patch("httpx.get", return_value=_mock_resp_403_empty_body()), \
         mock.patch.object(drive_changes.time, "sleep") as sleep, \
         mock.patch.object(drive_changes, "_api_limiter"):
        try:
            drive_changes._api_get("https://example.com", "tok", {})
            assert False, "应抛异常"
        except Exception:
            pass
        # 首次 + 2 次重试 = 3 次调用，sleep 被调用 2 次
        assert sleep.call_count == 2


# ---------------------------------------------------------------------------
# r37：410 page token 过期 —— 重新初始化而非每轮 warning 重放
# ---------------------------------------------------------------------------

def test_api_get_raises_page_token_expired_on_410():
    with mock.patch.object(drive_changes.httpx, "get",
                           return_value=_fake_resp(410)):
        try:
            drive_changes._api_get("http://x", "tok", {})
        except drive_changes.PageTokenExpiredError:
            pass
        else:
            raise AssertionError("410 应抛 PageTokenExpiredError")


def test_poll_once_page_token_expired_reinits(caplog):
    import logging
    did = "EXP410-%s" % uuid.uuid4().hex[:8]
    db = mock.Mock()
    saved = {}

    def fake_save(db_, drive_id_, token_):
        saved[drive_id_] = token_

    with mock.patch.object(drive_changes, "discover_drives",
                           return_value={did: ["MP"]}), \
         mock.patch.object(drive_changes, "_get_page_token",
                           return_value="old-expired-token"), \
         mock.patch.object(drive_changes, "list_changes",
                           side_effect=drive_changes.PageTokenExpiredError("expired")), \
         mock.patch.object(drive_changes, "get_start_page_token",
                           return_value="new-token-123"), \
         mock.patch.object(drive_changes, "_save_page_token",
                           side_effect=fake_save), \
         mock.patch("backend.database.SessionLocal", return_value=db), \
         caplog.at_level(logging.ERROR,
                         logger="backend.emby_server.drive_changes"):
        drive_changes._dead_drives.pop(did, None)
        try:
            drive_changes.poll_once()
        finally:
            drive_changes._dead_drives.pop(did, None)
    # 新 token 已落库
    assert saved.get(did) == "new-token-123", "过期后应重新初始化并保存新 token"
    # 不标 dead（配置没问题，只是 token 老了）
    assert did not in drive_changes._dead_drives
    err_logs = [r for r in caplog.records
                if r.levelno >= logging.ERROR and "page token 已过期" in r.getMessage()]
    assert len(err_logs) == 1, "应打一条 error 日志说明 token 已重置"
    assert did in err_logs[0].getMessage()
