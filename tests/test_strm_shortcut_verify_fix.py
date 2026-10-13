"""2026-10-14 补跑收尾三修复的回归测试。

fix1（P0）：_list_files_page 的 files.list 必须带 shortcutDetails 字段，
            否则 _resolve_shortcut_target 永远拿到空、快捷方式被静默跳过
            （个人盘两快捷方式零解析的根因）。
fix2（P1）：verify 的"缺失"判定豁免本轮去重跳过的文件
            （deduped 故意不登记状态行，不是生成漏了）。
fix3（P1）：verify 的 missing_count 与 breakdown 同源
            （同名文件夹产出重复 rel 时不再虚增）。

不碰网络：Drive API 部分全部 monkeypatch 隔离。
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.emby_server import strm_gen as sg


class TestShortcutFieldsPresent:
    def _fetch(self):
        def fetch(query, page_token=None):
            raise AssertionError("不应走到 files.list")
        fetch.token_box = {"token": "tok"}
        return fetch

    def test_list_fields_include_shortcut_details(self, monkeypatch):
        # files.list 必须带 shortcutDetails：否则列举到的快捷方式解析恒为 None
        captured = {}

        def fake_api_get(url, token, params):
            captured["params"] = params
            return {"files": [], "nextPageToken": None}

        from types import SimpleNamespace
        monkeypatch.setattr(
            sg, "_drive_modules",
            lambda: SimpleNamespace(_api_get=fake_api_get,
                                    _is_personal_drive_key=lambda did: False))
        sg._list_files_page("tok", "drv", "q")
        fields = captured["params"]["fields"]
        assert "shortcutDetails" in fields

    def test_missing_shortcut_details_warns_and_none(self, monkeypatch, caplog):
        # 列举 item 没有 shortcutDetails（旧 fields 的病症）→ None + warning
        from types import SimpleNamespace

        def no_api(u, t, p):
            raise AssertionError("target 未知，不应调 files.get")

        monkeypatch.setattr(
            sg, "_drive_modules",
            lambda: SimpleNamespace(_api_get=no_api,
                                    _is_personal_drive_key=lambda did: False))
        item = {"id": "scX", "mimeType": sg.SHORTCUT_MIME, "name": "剧集"}
        with caplog.at_level(logging.WARNING):
            assert sg._resolve_shortcut_target(self._fetch(), item, "did") is None
        assert any("shortcutDetails" in r.message for r in caplog.records), \
            [r.message for r in caplog.records]

    def test_shortcut_details_present_resolves(self, monkeypatch):
        # 有 shortcutDetails → 解析出 target_id（不受 warning 逻辑影响）
        from types import SimpleNamespace
        api_map = {
            "fT": {"id": "fT", "name": "t.mkv", "mimeType": None,
                   "trashed": False, "size": "7"},
        }
        monkeypatch.setattr(
            sg, "_drive_modules",
            lambda: SimpleNamespace(_api_get=lambda u, t, p: api_map[u.rsplit("/", 1)[-1]],
                                    _is_personal_drive_key=lambda did: False))
        item = {"id": "sc1", "shortcutDetails": {"targetId": "fT"}}
        body = sg._resolve_shortcut_target(self._fetch(), item, "did")
        assert body and body["id"] == "fT"


class TestVerifyDedupExemption:
    def _videos(self):
        return [
            ("d1", "MoviePilot/剧集/A/Season 1/A - S01E01.mkv", "f1", 100),
            ("d1", "MoviePilot/剧集/A/Season 1/A - S01E02.mkv", "f2", 100),
        ]

    def _state_on_disk(self, videos, tmp_path, skip=()):
        state = {}
        for did, rel, fid, size in videos:
            if any(s in rel for s in skip):
                continue
            spath = rel.replace("MoviePilot/", "").replace(".mkv", ".strm")
            full = os.path.join(str(tmp_path), spath)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write("x")
            state[(did, rel)] = (fid, size, spath)
        return state

    def test_deduped_rel_not_counted_missing(self, tmp_path):
        # 本轮 E02 被去重跳过（故意无 state 行）→ 不算缺失，不进榜单
        videos = self._videos()
        state = self._state_on_disk(videos, tmp_path, skip=("E02",))
        deduped = {("d1", "MoviePilot/剧集/A/Season 1/A - S01E02.mkv")}
        result = sg._verify_completeness(videos, state, ["MoviePilot/"],
                                         str(tmp_path), deduped_rels=deduped)
        assert result["incomplete_series"] == 0, result["incomplete"]

    def test_deduped_exempt_but_real_missing_still_counted(self, tmp_path):
        # 同一季：E02 去重豁免，E01 真缺失 → 只计 E01，dedup_satisfied=1
        videos = self._videos()
        state = self._state_on_disk(videos, tmp_path, skip=("E01", "E02"))
        deduped = {("d1", "MoviePilot/剧集/A/Season 1/A - S01E02.mkv")}
        result = sg._verify_completeness(videos, state, ["MoviePilot/"],
                                         str(tmp_path), deduped_rels=deduped)
        assert result["incomplete_series"] == 1
        inc = result["incomplete"][0]
        assert inc["missing_count"] == 1
        assert inc["missing_breakdown"]["not_generated"] == 1
        assert inc["missing_breakdown"]["dedup_satisfied"] == 1
        assert all("E01" in m for m in inc["missing_sample"])

    def test_light_dedup_exempt(self, tmp_path):
        # 轻量版同理：去重豁免不进榜单
        videos = self._videos()
        state = self._state_on_disk(videos, tmp_path, skip=("E02",))
        deduped = {("d1", "MoviePilot/剧集/A/Season 1/A - S01E02.mkv")}
        result = sg._verify_completeness_light(videos, state, ["MoviePilot/"],
                                               str(tmp_path), deduped_rels=deduped)
        assert result["incomplete_series"] == 0, result["incomplete"]


class TestVerifyMissingCountNoDuplicateInflation:
    def _dup_videos(self):
        # 同名文件夹产出重复 rel：60 条条目 / 30 个唯一 rel（郎君不如意案）
        vids = []
        for i in range(30):
            rel = f"nastool/剧集/国产剧/郎君不如意 (2023)/Season 1/E{i:02d}.mkv"
            vids.append(("d1", rel, f"fA{i}", 100))
            vids.append(("d1", rel, f"fB{i}", 100))
        return vids

    def _state_on_disk(self, videos, tmp_path, skip_n=0):
        # 按唯一 rel 写 state（DB 30 行，唯一 rel 口径）
        state = {}
        seen = set()
        skipped = 0
        for did, rel, fid, size in videos:
            if rel in seen:
                continue
            seen.add(rel)
            if skipped < skip_n:
                skipped += 1
                continue
            spath = rel.replace("nastool/", "").replace(".mkv", ".strm")
            full = os.path.join(str(tmp_path), spath)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w") as f:
                f.write("x")
            state[(did, rel)] = (fid, size, spath)
        return state

    def test_duplicate_rels_no_inflation(self, tmp_path):
        # 60 条条目 / 30 个唯一 rel / 30 行 → missing_count 必须为 0（旧口径虚增 30）
        videos = self._dup_videos()
        state = self._state_on_disk(videos, tmp_path)
        result = sg._verify_completeness(videos, state, ["nastool/"], str(tmp_path))
        assert result["incomplete_series"] == 0, result["incomplete"]

    def test_drive_count_uses_unique_rels(self, tmp_path):
        # 缺 1 行：drive_count 应为去重口径 30（不是 60），missing_count=1
        videos = self._dup_videos()
        state = self._state_on_disk(videos, tmp_path, skip_n=1)
        result = sg._verify_completeness(videos, state, ["nastool/"], str(tmp_path))
        assert result["incomplete_series"] == 1
        inc = result["incomplete"][0]
        assert inc["drive_count"] == 30
        assert inc["missing_count"] == 1
        assert inc["missing_breakdown"]["not_generated"] == 1
