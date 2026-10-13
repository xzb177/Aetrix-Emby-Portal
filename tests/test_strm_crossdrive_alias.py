"""跨盘快捷方式别名展开回归测试（2026-10-14）。

背景：R33 的别名机制只按"同盘快捷方式"设计。个人盘 `video/` 下的两个快捷方式
（剧集/影库）指向**别人盘**的目录时：
- _expand_alias_folders 的本地 folder_children 里没有外部 tid → 只返回 [tid]；
- _process_alias_queue 的 list_files([tid]) 只列目标根直接子项，子目录被
  `if mt == FOLDER_MIME: continue` 丢弃；
- 主循环的 _emit_video_paths 因 dir_entries 里没有外部目录 id 而产出为空。
→ 跨盘快捷方式零入库（生产 06:09 轮实证：目标已解析、子树已列举，DB 零行）。

修复：_expand_alias_folders_cross_drive —— tid 不在本地 map 时走 Drive API
BFS 递归展开。本文件端到端断言 iter_drive_videos 的实际产出（rel, fid, size），
不只断言内部解析函数（#841 的教训：mock 测试从没看过真实产出）。

mock 语义（忠实于生产实测）：
- 无 `in parents` 的列举查询：只返回被查询 drive_id 自己的条目
  （阶段一在个人盘只拉到 paul 自己的 9 个目录，看不到外盘树）；
- 带 `'X' in parents` 的查询：返回 parents 含 X 的条目（不分盘），模拟
  spaces=drive + includeItemsFromAllDrives 能穿透到快捷方式目标
  （生产日志实证：22:09:18 起数百页翻页 200 OK）。
"""
import os
import re
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.emby_server import strm_gen as sg

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
PERSONAL = "myDrive:paul_emby"
EXT_A = "0ALvomXL_TKCXUk9PVA"  # 影库目标所在盘（别人盘）
EXT_B = "0ANA-hW6eYfitUk9PVA"  # 剧集目标所在盘（别人盘）


def _fld(fid, name, parents, drive, trashed=False):
    e = {"id": fid, "name": name, "mimeType": FOLDER_MIME,
         "parents": list(parents), "_drive": drive}
    if trashed:
        e["trashed"] = True
    return e


def _file(fid, name, parents, drive, size=100, mimeType=None):
    e = {"id": fid, "name": name, "parents": list(parents), "_drive": drive}
    if mimeType:
        e["mimeType"] = mimeType
    if size is not None:
        e["size"] = str(size)
    return e


def _shortcut(fid, name, parents, drive, target_id):
    e = _file(fid, name, parents, drive, size=0, mimeType=SHORTCUT_MIME)
    e["shortcutDetails"] = {"targetId": target_id}
    return e


def build_cross_drive_dataset():
    """个人盘 video/ 下两个快捷方式 → 两个外盘目标，各含嵌套子目录与视频文件。"""
    E = []
    # —— 个人盘自己：video/ + 2 个快捷方式（无原生视频文件）——
    E.append(_fld("fld_video", "video", ["root"], PERSONAL))
    E.append(_fld("fld_gs", "gs", ["root"], PERSONAL))
    E.append(_fld("fld_remux", "remux", ["root"], PERSONAL))
    E.append(_shortcut("sc_lib", "影库", ["fld_video"], PERSONAL, "tgt_lib"))
    E.append(_shortcut("sc_show", "剧集", ["fld_video"], PERSONAL, "tgt_show"))
    # —— 外盘 A：影库目标（根下只有子目录，视频在深层）——
    E.append(_fld("tgt_lib", "影库根", [EXT_A], EXT_A))
    E.append(_fld("tgt_lib_sub", "动作片", ["tgt_lib"], EXT_A))
    E.append(_fld("tgt_lib_sub2", "2024", ["tgt_lib_sub"], EXT_A))
    E.append(_file("v_lib1", "片1.mkv", ["tgt_lib_sub"], EXT_A, size=1001))
    E.append(_file("v_lib2", "片2.mp4", ["tgt_lib_sub2"], EXT_A, size=1002))
    E.append(_file("v_lib3", "说明.nfo", ["tgt_lib_sub"], EXT_A, size=10))
    # —— 外盘 B：剧集目标 ——
    E.append(_fld("tgt_show", "剧集根", [EXT_B], EXT_B))
    E.append(_fld("tgt_show_sub", "国产剧", ["tgt_show"], EXT_B))
    E.append(_file("v_show1", "剧1 - S01E01.mkv", ["tgt_show_sub"], EXT_B, size=2001))
    # —— 同盘快捷方式（回归对照）：个人盘内 gs/ 下的快捷方式指同盘目录 ——
    E.append(_fld("fld_music", "音乐", ["fld_gs"], PERSONAL))
    E.append(_file("v_same", "歌1.mkv", ["fld_music"], PERSONAL, size=3001))
    E.append(_shortcut("sc_same", "音乐快捷", ["fld_video"], PERSONAL, "fld_music"))
    return E


class FakeCrossDrive:
    """drive 感知的模拟 Drive：见模块 docstring 的 mock 语义。"""

    PAGE = 50

    def __init__(self, entries, by_id):
        self.entries = entries
        self.by_id = by_id
        self.calls = []

    def list_page(self, token, drive_id, query, page_token=None):
        self.calls.append((query, page_token))
        matched = [e for e in self.entries if self._match(e, drive_id, query)]
        start = int(page_token) if page_token else 0
        page = matched[start:start + self.PAGE]
        nxt = str(start + self.PAGE) if start + self.PAGE < len(matched) else None
        # 去掉内部 _drive 标记，模拟真实 API 条目
        clean = [{k: v for k, v in e.items() if not k.startswith("_")} for e in page]
        return clean, nxt

    @staticmethod
    def _match(e, drive_id, q):
        if "trashed=false" in q and e.get("trashed"):
            return False
        m = re.search(r"mimeType='([^']+)'", q)
        if m and e.get("mimeType") != m.group(1):
            return False
        m = re.search(r"name='((?:[^'\\]|\\.)*)'", q)
        if m:
            want = m.group(1).replace("\\'", "'")
            if e.get("name") != want:
                return False
        ids = re.findall(r"'([^']+)' in parents", q)
        if ids:
            # parents 查询：跨盘可达（spaces=drive + includeItemsFromAllDrives）
            parents = e.get("parents") or []
            if " or " in q:
                return any(i in parents for i in ids)
            return all(i in parents for i in ids)
        # 无 parents 限定：只返回被查询盘自己的条目（阶段一语义）
        return e.get("_drive") == drive_id

    def api_get(self, url, token, params):
        tid = url.rstrip("/").rsplit("/", 1)[-1]
        e = self.by_id.get(tid)
        if e is None:
            raise RuntimeError(f"target not found: {tid}")
        return {"id": e["id"], "name": e["name"],
                "mimeType": e.get("mimeType"), "driveId": e["_drive"],
                "parents": e.get("parents"), "size": e.get("size")}


@pytest.fixture()
def xdrive_env(monkeypatch):
    entries = build_cross_drive_dataset()
    by_id = {e["id"]: e for e in entries}
    fake = FakeCrossDrive(entries, by_id)

    def fake_page_fetcher(drive_id):
        def fetch(query, page_token=None):
            return fake.list_page("tok", drive_id, query, page_token)
        fetch.token_box = {"token": "tok"}
        return fetch

    monkeypatch.setattr(sg, "_page_fetcher", fake_page_fetcher)
    monkeypatch.setattr(sg, "_get_token", lambda did=None, **k: "tok")
    monkeypatch.setattr(
        sg, "_drive_modules",
        lambda: SimpleNamespace(
            _is_personal_drive_key=lambda did: did == PERSONAL,
            _api_get=fake.api_get))
    return fake


def _run(monkeypatch, drive_id, dir_paths):
    return list(sg.iter_drive_videos(
        drive_id, dir_paths, strict=False, subtree_listing=True,
        resolve_shortcuts=True))


class TestCrossDriveAlias:
    def test_external_targets_yield_alias_paths(self, xdrive_env, monkeypatch):
        """跨盘快捷方式：外盘嵌套子目录里的视频文件必须产出别名前缀路径。"""
        out = _run(monkeypatch, PERSONAL, ["video"])
        rels = sorted(r for r, fid, size in out)
        assert rels == [
            "video/剧集/国产剧/剧1 - S01E01.mkv",
            "video/影库/动作片/2024/片2.mp4",
            "video/影库/动作片/片1.mkv",
            "video/音乐快捷/歌1.mkv",
        ]
        by_rel = {r: (fid, size) for r, fid, size in out}
        assert by_rel["video/影库/动作片/片1.mkv"] == ("v_lib1", 1001)
        assert by_rel["video/影库/动作片/2024/片2.mp4"] == ("v_lib2", 1002)
        assert by_rel["video/剧集/国产剧/剧1 - S01E01.mkv"] == ("v_show1", 2001)
        # 非视频不产出
        assert not any(r.endswith(".nfo") for r in rels)

    def test_same_drive_shortcut_unchanged(self, xdrive_env, monkeypatch):
        """同盘快捷方式：行为与修复前一致（别名前缀路径产出）。"""
        out = _run(monkeypatch, PERSONAL, ["video"])
        rels = [r for r, fid, size in out]
        assert "video/音乐快捷/歌1.mkv" in rels

    def test_shortcut_resolve_off_yields_nothing_extra(self, xdrive_env, monkeypatch):
        """开关关闭：快捷方式不解析，外盘内容零产出（旧行为保持）。"""
        out = list(sg.iter_drive_videos(
            PERSONAL, ["video"], strict=False, subtree_listing=True,
            resolve_shortcuts=False))
        assert out == []

    def test_expand_folders_local_fast_path_unchanged(self):
        """同盘 tid 走本地快路径：与 _expand_alias_folders 结果一致。"""
        folder_children = {
            "t1": [{"id": "s1", "name": "子目录", "mimeType": FOLDER_MIME}],
        }
        calls = []

        def fetch(q, page_token=None):
            calls.append(q)
            return [], None

        alias_map, folders = sg._expand_alias_folders_cross_drive(
            fetch, folder_children, "t1", [("video/", "影库/")])
        assert folders == ["t1", "s1"]
        assert alias_map["s1"] == [("video/", "影库/子目录/")]
        assert calls == []  # 本地快路径：零 API 调用
