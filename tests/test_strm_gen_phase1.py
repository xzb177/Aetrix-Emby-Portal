"""Tests for .strm 生成器第一阶段改造（A1+A2+A3，2026-10-13）。

覆盖：
- A1：_extract_strm_file_id（file_id 提取纯函数）
- A1：_backfill_state_from_disk（磁盘反推，用 tmp 目录，不碰真实磁盘/DB）
- A2：_parse_drive_batch_response（batch 响应解析纯函数）
- A2：_drive_file_ids_invalid_batch（无 token 放行、batch 异常回退逐个）
- A3：DriveChangesLister.used_incremental_path（三条列举路径的标记）

不碰网络（Drive API 部分全部 monkeypatch 隔离）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.emby_server import strm_gen as sg
from backend.emby_server import strm_source as ss


class TestExtractStrmFileId:
    def test_drive_url(self):
        line = sg.drive_url("1aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789") + "\n"
        assert sg._extract_strm_file_id(line) == "1aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"

    def test_local_source(self):
        # 本地源 .strm 也是同一格式（id=local:sha…），提取数据源无关
        line = sg.drive_url("local:9f86d081884c7d659a2feaa0c55ad015") + "\n"
        assert sg._extract_strm_file_id(line) == "local:9f86d081884c7d659a2feaa0c55ad015"

    def test_no_id(self):
        assert sg._extract_strm_file_id("not a url at all\n") is None

    def test_empty(self):
        assert sg._extract_strm_file_id("") is None
        assert sg._extract_strm_file_id(None) is None

    def test_stops_at_ampersand(self):
        # id= 后到下一个 & 为止，不多吃 confirm=t
        line = "https://drive.google.com/uc?export=download&id=FID123&confirm=t\n"
        assert sg._extract_strm_file_id(line) == "FID123"


class TestBackfillStateFromDisk:
    def _write_strm(self, root, rel, fid):
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(sg.drive_url(fid) + "\n")

    def test_backfill_matches_by_file_id(self, tmp_path, monkeypatch):
        # 磁盘上有 2 个 .strm（含一个 -2 碰撞后缀），状态表缺 3 行中的 2 行
        strm_root = str(tmp_path / "strm")
        self._write_strm(strm_root, "剧集/xx.strm", "FID_AAA")
        self._write_strm(strm_root, "剧集/xx-2.strm", "FID_BBB")  # 碰撞后缀
        with open(os.path.join(strm_root, "readme.txt"), "w") as fh:
            fh.write("非 strm 文件应被忽略\n")

        state = {("d1", "电影/old.mkv"): ("FID_OLD", 10, "电影/old.strm")}
        all_videos = [
            ("d1", "电影/old.mkv", "FID_OLD", 10),   # 已在 state：不缺行
            ("d1", "剧集/xx.mkv", "FID_AAA", 100),   # 缺行，磁盘有
            ("d1", "剧集/xx2.mkv", "FID_BBB", 200),  # 缺行，磁盘有（-2 后缀也对上）
            ("d1", "剧集/new.mkv", "FID_NEW", 300),  # 缺行，磁盘没有
        ]
        saved = []
        monkeypatch.setattr(sg, "_save_state_rows", lambda rows: saved.extend(rows))

        res = sg._backfill_state_from_disk(all_videos, state, strm_root)

        assert res == {"backfilled": 2, "unmatched": 1}
        # 用 file_id 内容匹配：-2 碰撞后缀自动对上，不靠路径反推
        assert saved == [
            ("d1", "剧集/xx.mkv", "FID_AAA", 100, "剧集/xx.strm"),
            ("d1", "剧集/xx2.mkv", "FID_BBB", 200, "剧集/xx-2.strm"),
        ]
        # 内存 state 同步更新，主循环走 skip 路径
        assert state[("d1", "剧集/xx.mkv")] == ("FID_AAA", 100, "剧集/xx.strm")
        assert state[("d1", "剧集/xx2.mkv")] == ("FID_BBB", 200, "剧集/xx-2.strm")
        assert ("d1", "剧集/new.mkv") not in state

    def test_no_missing_zero_overhead(self, tmp_path, monkeypatch):
        # 无缺行：直接返回，不扫磁盘（walk 被调就炸）
        def _boom(*a, **k):
            raise AssertionError("无缺行时不应 walk 磁盘")
        monkeypatch.setattr(os, "walk", _boom)
        saved = []
        monkeypatch.setattr(sg, "_save_state_rows", lambda rows: saved.extend(rows))

        state = {("d1", "a.mkv"): ("FID_A", 1, "a.strm")}
        res = sg._backfill_state_from_disk([("d1", "a.mkv", "FID_A", 1)],
                                           state, str(tmp_path))
        assert res == {"backfilled": 0, "unmatched": 0}
        assert saved == []

    def test_unreadable_file_skipped(self, tmp_path, monkeypatch):
        # 读不到第一行的文件跳过，不中断反推
        strm_root = str(tmp_path / "strm")
        os.makedirs(strm_root)
        bad = os.path.join(strm_root, "bad.strm")
        os.makedirs(bad)  # 目录冒充 .strm 文件：open 会 OSError
        self._write_strm(strm_root, "ok.strm", "FID_OK")
        saved = []
        monkeypatch.setattr(sg, "_save_state_rows", lambda rows: saved.extend(rows))

        res = sg._backfill_state_from_disk([("d1", "ok.mkv", "FID_OK", 5)], {},
                                           strm_root)
        assert res == {"backfilled": 1, "unmatched": 0}
        assert saved == [("d1", "ok.mkv", "FID_OK", 5, "ok.strm")]


def _mock_batch_text(items):
    """造 Drive batch multipart 响应：items=[(idx, status)]。"""
    boundary = "batch_resp_123"
    parts = []
    for idx, status in items:
        parts.append("--" + boundary)
        parts.append("Content-Type: application/http")
        parts.append("Content-ID: item-%d" % idx)
        parts.append("")
        parts.append("HTTP/1.1 %d %s" % (status, {200: "OK", 404: "Not Found",
                                                  403: "Forbidden"}.get(status, "?")))
        parts.append("Content-Type: application/json; charset=UTF-8")
        parts.append("")
        parts.append('{"id": "whatever"}' if status == 200 else
                     '{"error": {"code": %d}}' % status)
    parts.append("--" + boundary + "--")
    return "\r\n".join(parts)


class TestParseDriveBatchResponse:
    def test_mixed(self):
        fids = ["F1", "F2", "F3"]
        text = _mock_batch_text([(0, 200), (1, 404), (2, 403)])
        assert sg._parse_drive_batch_response(text, fids) == {"F2", "F3"}

    def test_all_valid(self):
        fids = ["F1", "F2"]
        text = _mock_batch_text([(0, 200), (1, 200)])
        assert sg._parse_drive_batch_response(text, fids) == set()

    def test_missing_part_counts_invalid(self):
        # 子 part 缺失（Content-ID 对不上）→ 判无效
        fids = ["F1", "F2"]
        text = _mock_batch_text([(0, 200)])
        assert sg._parse_drive_batch_response(text, fids) == {"F2"}

    def test_part_without_status_counts_invalid(self):
        fids = ["F1"]
        text = "--b\r\nContent-ID: item-0\r\n\r\nno status line here\r\n--b--"
        assert sg._parse_drive_batch_response(text, fids) == {"F1"}


class TestDriveFileIdsInvalidBatch:
    # 注意：#830 起本函数吃 (fid, drive_id) 二元组、按盘取凭据
    # （get_token_for_drive），旧的纯 fid 列表 + _get_token 写法已作废。
    def test_no_token_returns_empty(self, monkeypatch):
        # 无 token → 空集（宁可放行），且不发任何请求
        from backend.emby_server import drive_changes as dc
        monkeypatch.setattr(dc, "get_token_for_drive", lambda did: None)
        def _boom(*a, **k):
            raise AssertionError("无 token 时不应发请求")
        monkeypatch.setattr(sg, "_drive_batch_check", _boom)
        assert sg._drive_file_ids_invalid_batch(
            [("F1", "D1"), ("F2", "D1")]) == set()

    def test_empty_input(self):
        assert sg._drive_file_ids_invalid_batch([]) == set()

    def test_batch_failure_falls_back_per_file(self, monkeypatch):
        # batch 整体异常 → 回退逐个 _drive_file_id_valid（只慢不坏）
        from backend.emby_server import drive_changes as dc
        monkeypatch.setattr(dc, "get_token_for_drive", lambda did: "tok")
        monkeypatch.setattr(dc._api_limiter, "acquire", lambda: None)
        def _raise(fids, token):
            raise RuntimeError("batch 整体 500")
        monkeypatch.setattr(sg, "_drive_batch_check", _raise)
        monkeypatch.setattr(sg, "_drive_file_id_valid",
                            lambda fid, did: fid != "BAD")
        assert sg._drive_file_ids_invalid_batch(
            [("OK1", "D1"), ("BAD", "D1"), ("OK2", "D1")]) == {"BAD"}

    def test_success_path(self, monkeypatch):
        from backend.emby_server import drive_changes as dc
        monkeypatch.setattr(dc, "get_token_for_drive", lambda did: "tok")
        monkeypatch.setattr(dc._api_limiter, "acquire", lambda: None)
        monkeypatch.setattr(sg, "_drive_batch_check",
                            lambda fids, token: {"F_BAD"})
        assert sg._drive_file_ids_invalid_batch(
            [("F_OK", "D1"), ("F_BAD", "D1")]) == {"F_BAD"}


class TestIncrementalPathFlag:
    def test_default_false(self):
        l = ss.DriveChangesLister("did123", "Movies/")
        assert l.used_incremental_path is False

    def test_full_mode_false(self, monkeypatch):
        # incremental=False → 全量回退，标记 False
        def _fake_iter(drive_id, dir_path, strict=True, **kwargs):
            yield ("a/b.mkv", "FID1", 100)
        monkeypatch.setattr(sg, "iter_drive_videos", _fake_iter)
        l = ss.DriveChangesLister("did123", "Movies/")
        items = list(l.iter_videos(incremental=False))
        assert l.used_incremental_path is False
        assert items == [("a/b.mkv", "FID1", 100)]

    def test_first_time_baseline_false(self, monkeypatch):
        # 无检查点 → 首次建基线（全量），标记 False
        from backend.emby_server import drive_changes as dc
        monkeypatch.setattr(ss.DriveChangesLister, "_get_saved_token",
                            lambda self: None)
        monkeypatch.setattr(dc, "get_start_page_token", lambda drive_id: "tok0")
        monkeypatch.setattr(sg, "iter_drive_videos",
                            lambda drive_id, dir_path, strict=True, **kwargs:
                            iter([("a/b.mkv", "FID1", 100)]))
        l = ss.DriveChangesLister("did123", "Movies/")
        list(l.iter_videos(incremental=True))
        assert l.used_incremental_path is False

    def test_changes_path_true(self, monkeypatch):
        # 有检查点 → 走 changes（即使 0 变化也是增量语义），标记 True
        from backend.emby_server import drive_changes as dc
        monkeypatch.setattr(ss.DriveChangesLister, "_get_saved_token",
                            lambda self: "savedtok")
        monkeypatch.setattr(dc, "list_changes",
                            lambda page_token, drive_id: ([], "newtok"))
        l = ss.DriveChangesLister("did123", "")
        list(l.iter_videos(incremental=True))
        assert l.used_incremental_path is True
