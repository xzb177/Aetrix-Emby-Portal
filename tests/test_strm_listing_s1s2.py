"""Tests for S1 (subtree-scoped listing) + S2 (skip full verify on incremental rounds).

S1 等价性：同一份模拟 Drive 数据集，旧扁平拉取（oracle，函数体冻结自
main@6c10828，逐字节未改）vs 新子树限定列举，产出 (rel, fid, size)
多重集合必须逐项一致；同时断言新路径的 API 请求数显著更少。
S2：_round_is_fully_incremental 判定 + _verify_completeness_light 语义。
不碰网络：_get_token / _list_files_page / _drive_modules 全部 monkeypatch。
"""
import os
import re
import sys
from collections import Counter
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.emby_server import strm_gen as sg
from typing import Optional  # noqa: F401  (oracle 函数体内的注解用)


def _make_oracle():
    """把冻结的旧实现包成可调用函数（闭包注入它引用的模块级名字）。

    函数体与 main@6c10828 的 iter_drive_videos 逐字节一致，仅函数名不同。
    """
    _page_fetcher = sg._page_fetcher
    _resolve_dir_id = sg._resolve_dir_id
    _safe_name = sg._safe_name
    is_video_path = sg.is_video_path
    SKIP_DIR_NAMES = sg.SKIP_DIR_NAMES
    _set_progress = sg._set_progress
    # _drive_modules 必须每次调用时重新从 sg 取（与旧代码的模块全局查找语义一致），
    # 否则 monkeypatch 换掉 sg._drive_modules 后 oracle 还在用旧的。
    def _drive_modules():
        return sg._drive_modules()
    logger = sg.logger

    def oracle_iter_drive_videos(drive_id: str, dir_path: str, strict: bool = True):
        """流式列出源目录下所有视频文件。

        yield (root_relpath, file_id, size)。root_relpath 为相对网盘根路径。

        strict=True（默认）：源目录不存在时抛 RuntimeError（显式配置的盘，
        管理员需要知道配错了）。
        strict=False：源目录不存在时记 warning 并返回空（自动发现模式下，
        不是每个盘都有媒体目录，缺目录是预期情况，不应报错）。

        实现：扁平全量拉取 + 本地建树。
        旧实现是 BFS、每个目录发一次 files.list（"'{folder_id}' in parents"）：
        8.5 万文件 / 数千目录 = 数千次 API，每次 200~500ms，列举阶段要
        10~30 分钟，且全程不更新进度——前端一直显示"列举 Drive 文件…"
        且计数全 0，看起来像卡死（用户实报）。
        新实现一次 files.list（无 parents 限定，pageSize=1000）分页拉完全盘条目，
        本地按 parents 建树再从源目录 BFS 展开：API 调用从数千降到约 100，
        且每拉完一页就汇报进度，前端能看到条目数在涨。
        代价：会拉取源目录之外的条目元数据（仅 id/name/parents，不下载内容）；
        对 MoviePilot 这类单用途盘可忽略。

        （修 P1-1：旧实现只在开始时取一次 token，8 万级文件的长列举
        会因 SA token 过期而整体作废——_page_fetcher 的 401 自动刷新保留）。
        """
        fetch = _page_fetcher(drive_id)  # 内部已做 SA 凭据检查 + 401 自动刷新
        dir_id = _resolve_dir_id(fetch, drive_id, dir_path)
        if dir_path and not dir_id:
            if not strict:
                logger.warning("strm_gen: drive=%s 上找不到源目录 %r，已跳过该盘",
                               drive_id, dir_path)
                return
            raise RuntimeError(f"Drive 上找不到源目录: {dir_path!r}")

        # 1. 扁平拉取：全盘条目（文件+目录）一次拉完，本地按 parents 建树。
        #    只取源目录子树在第 2 步过滤，这里不加 in parents 限定。
        children: dict[str, list[dict]] = {}
        listed = 0
        page_token: Optional[str] = None
        while True:
            files, page_token = fetch("trashed=false", page_token)
            for f in files:
                if f.get("trashed"):
                    continue
                for pid in f.get("parents") or []:
                    children.setdefault(pid, []).append(f)
            listed += len(files)
            _set_progress(total=listed,
                          current=f"正在列举 Drive 文件…（已拉取 {listed} 个条目）")
            if not page_token:
                break
        logger.info("strm_gen: drive=%s 扁平拉取 %d 个条目", drive_id, listed)

        # 2. 本地 BFS：从源目录（或网盘根）逐层展开，只取源目录子树。
        #    网盘根相对路径前缀：dir_path 本身（与旧实现一致，状态键保持兼容）。
        norm = dir_path.strip("/")
        if norm:
            stack: list[tuple[str, str]] = [(dir_id, "")]
            root_prefix = norm + "/"
        else:
            # 共享盘根目录：'root' 别名只对个人盘有效，共享盘顶层用 drive_id。
            # 原来无条件用 'root' in parents，共享盘配空源目录时列举结果恒为空
            # （静默丢全部）——这里换成本地建树的等价逻辑。
            dc = _drive_modules()
            try:
                is_personal = dc._is_personal_drive_key(drive_id)
            except Exception:
                is_personal = False
            stack = [("root" if is_personal else drive_id, "")]
            root_prefix = ""

        while stack:
            folder_id, rel_prefix = stack.pop()
            for f in children.get(folder_id, []):
                name = _safe_name(f.get("name", ""))
                fid = f.get("id", "")
                if not name or not fid:
                    continue
                if f.get("mimeType") == "application/vnd.google-apps.folder":
                    if name.startswith(".") or name in SKIP_DIR_NAMES:
                        continue
                    stack.append((fid, rel_prefix + name + "/"))
                else:
                    rel = root_prefix + rel_prefix + name
                    if not is_video_path(rel):
                        continue
                    try:
                        size = int(f.get("size") or 0)
                    except (ValueError, TypeError):
                        size = 0
                    yield rel, fid, size



    return oracle_iter_drive_videos


ORACLE = _make_oracle()

FOLDER_MIME = "application/vnd.google-apps.folder"
SHORTCUT_MIME = "application/vnd.google-apps.shortcut"
SHARED_DRIVE_ID = "0AERUVu4kfWEfUk9PVA"


def _fld(fid, name, parents, trashed=False):
    e = {"id": fid, "name": name, "mimeType": FOLDER_MIME,
         "parents": list(parents)}
    if trashed:
        e["trashed"] = True
    return e


def _file(fid, name, parents, size=100, mimeType=None, trashed=False):
    e = {"id": fid, "name": name, "parents": list(parents)}
    if mimeType:
        e["mimeType"] = mimeType
    if size is not None:
        e["size"] = str(size)
    if trashed:
        e["trashed"] = True
    return e


def build_dataset(n_shows=40, eps_per_season=10, seasons_per_show=3,
                  noise_dirs=30, noise_per_dir=100):
    """构造模拟 Drive 数据集，覆盖各种边缘情况。"""
    E = []
    D = SHARED_DRIVE_ID
    E.append(_fld("fld_mp", "MoviePilot", [D]))
    E.append(_fld("fld_tv", "剧集", ["fld_mp"]))
    E.append(_fld("fld_cd", "国产剧", ["fld_tv"]))
    E.append(_fld("fld_mv", "电影", ["fld_mp"]))
    n = [0]

    def vid(fid, name, parents, size=100, **kw):
        E.append(_file(fid, name, parents, size=size, **kw))

    for i in range(n_shows):
        show = f"fld_show{i:02d}"
        E.append(_fld(show, f"剧{i:02d}", ["fld_cd"]))
        for s in range(1, seasons_per_show + 1):
            sea = f"fld_s{i:02d}_{s}"
            E.append(_fld(sea, f"Season {s}", [show]))
            for e in range(1, eps_per_season + 1):
                n[0] += 1
                vid(f"f{n[0]}", f"剧{i:02d} - S{s:02d}E{e:02d}.mkv",
                    [sea], size=100 + n[0])
    # —— 边缘情况 ——
    # 同名目录（Drive 允许重名）：两个 "Season 1" 在同一父目录下
    E.append(_fld("fld_dup1", "同名季", ["fld_cd"]))
    E.append(_fld("fld_dup2", "同名季", ["fld_cd"]))
    vid("f_dup1", "a.mkv", ["fld_dup1"], size=11)
    vid("f_dup2", "a.mkv", ["fld_dup2"], size=22)
    # 隐藏目录 / 系统目录（应被跳过，子树不可见）
    E.append(_fld("fld_hidden", ".hidden", ["fld_cd"]))
    vid("f_h1", "secret.mkv", ["fld_hidden"], size=33)
    E.append(_fld("fld_ea", "@eaDir", ["fld_cd"]))
    vid("f_ea1", "thumb.mkv", ["fld_ea"], size=44)
    # 非视频 / 点号文件（应被跳过）
    vid("f_nfo", "info.nfo", ["fld_cd"], size=55)
    vid("f_dot", ".dotfile.mkv", ["fld_cd"], size=66)
    # 快捷方式（旧行为：非目录 + 扩展名命中 → 会被列出）
    vid("f_sc", "link.mkv", ["fld_cd"], size=0, mimeType=SHORTCUT_MIME)
    # 多 parent 文件（旧行为：每个 parent 链各产出一条）
    vid("f_multi", "multi.mkv", ["fld_mv", "fld_tv"], size=77)
    # 已删除（应被跳过）
    vid("f_trash", "gone.mkv", ["fld_mv"], size=88, trashed=True)
    E.append(_fld("fld_trashdir", "trashdir", ["fld_mv"], trashed=True))
    vid("f_trash2", "gone2.mkv", ["fld_trashdir"], size=99)
    # 源目录根下的直接文件
    vid("f_rootfile", "根文件.mkv", ["fld_mp"], size=111)
    # 名字带视频扩展名的目录（旧行为：当目录展开，不当文件产出）
    E.append(_fld("fld_weirddir", "weird.mkv", ["fld_cd"]))
    vid("f_weird", "inner.mkv", ["fld_weirddir"], size=222)
    # 源目录外的子树（MoviePilot 查询时不可见）
    E.append(_fld("fld_nas", "nastool", [D]))
    for d in range(noise_dirs):
        nd = f"fld_noise{d}"
        E.append(_fld(nd, f"noise{d}", ["fld_nas"]))
        for k in range(noise_per_dir):
            n[0] += 1
            vid(f"f{n[0]}", f"n{k}.mkv", [nd], size=10)
    # parent 指向不存在目录的文件（两边都不可见）
    vid("f_orphan", "orphan.mkv", ["fld_missing"], size=123)
    # 盘根文件（dir_path="" 的根列举用）
    vid("f_driveroot", "driveroot.mkv", [D], size=456)
    # 带单引号的目录名（_resolve_dir_id 转义用；不在 MoviePilot 下）
    E.append(_fld("fld_q", "Q' Dir", [D]))
    vid("f_q", "q.mkv", ["fld_q"], size=789)
    return E


class FakeDrive:
    """模拟 Drive files.list：按查询语义过滤，分页返回，记录每次请求。"""

    PAGE = 7  # 小分页，强制多页，验证翻页逻辑

    def __init__(self, entries):
        self.entries = entries
        self.calls = []  # list[(query, page_token)]

    def list_page(self, token, drive_id, query, page_token=None):
        self.calls.append((query, page_token))
        matched = [e for e in self.entries if self._match(e, query)]
        start = int(page_token) if page_token else 0
        page = matched[start:start + self.PAGE]
        nxt = str(start + self.PAGE) if start + self.PAGE < len(matched) else None
        return page, nxt

    @staticmethod
    def _match(e, q):
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
            parents = e.get("parents") or []
            if " or " in q:
                if not any(i in parents for i in ids):
                    return False
            elif not all(i in parents for i in ids):
                return False
        return True


@pytest.fixture()
def drive_env(monkeypatch):
    entries = build_dataset()
    fake = FakeDrive(entries)
    monkeypatch.setattr(sg, "_get_token", lambda: "tok")
    monkeypatch.setattr(sg, "_list_files_page", fake.list_page)
    monkeypatch.setattr(
        sg, "_drive_modules",
        lambda: SimpleNamespace(_is_personal_drive_key=lambda did: False))
    return fake, entries


def _run_both(monkeypatch, fake, drive_id, dir_path, strict=True,
              personal=False):
    if personal:
        monkeypatch.setattr(
            sg, "_drive_modules",
            lambda: SimpleNamespace(_is_personal_drive_key=lambda did: True))
    fake.calls.clear()
    new = list(sg.iter_drive_videos(drive_id, dir_path, strict=strict,
                                    subtree_listing=True))
    new_calls = len(fake.calls)
    fake.calls.clear()
    old = list(ORACLE(drive_id, dir_path, strict=strict))
    old_calls = len(fake.calls)
    return new, new_calls, old, old_calls


class TestSubtreeEquivalence:
    def test_moviepilot_equivalence(self, drive_env, monkeypatch):
        fake, _ = drive_env
        new, new_calls, old, old_calls = _run_both(
            monkeypatch, fake, SHARED_DRIVE_ID, "MoviePilot")
        assert Counter(new) == Counter(old)
        assert len(new) > 1000  # 数据集足够大，有统计意义
        print(f"\n  equivalence OK: {len(new)} files; "
              f"API calls old={old_calls} new={new_calls} "
              f"(ratio {new_calls / old_calls:.2f})")
        # S1 必须显著减少请求数
        assert new_calls < old_calls * 0.7

    def test_flag_fallback_matches_oracle(self, drive_env, monkeypatch):
        fake, _ = drive_env
        fake.calls.clear()
        via_flag = list(sg.iter_drive_videos(
            SHARED_DRIVE_ID, "MoviePilot", subtree_listing=False))
        fake.calls.clear()
        old = list(ORACLE(SHARED_DRIVE_ID, "MoviePilot"))
        assert Counter(via_flag) == Counter(old)

    def test_root_listing_equivalence(self, drive_env, monkeypatch):
        # dir_path=""：整盘根列举（含盘根文件、nastool、Q' Dir）
        fake, _ = drive_env
        new, _, old, _ = _run_both(monkeypatch, fake, SHARED_DRIVE_ID, "")
        assert Counter(new) == Counter(old)
        rels = {r for r, _, _ in new}
        assert "driveroot.mkv" in rels
        assert "nastool/noise0/n0.mkv" in rels
        assert "Q' Dir/q.mkv" in rels

    def test_personal_drive_root_equivalence(self, drive_env, monkeypatch):
        fake, entries = drive_env
        entries.append(_file("f_proot", "proot.mkv", ["root"], size=321))
        new, _, old, _ = _run_both(
            monkeypatch, fake, "personal_key", "", personal=True)
        assert Counter(new) == Counter(old)
        assert ("proot.mkv", "f_proot", 321) in new

    def test_missing_dir_strict_parity(self, drive_env, monkeypatch):
        fake, _ = drive_env
        with pytest.raises(RuntimeError):
            list(sg.iter_drive_videos(SHARED_DRIVE_ID, "不存在的目录"))
        with pytest.raises(RuntimeError):
            list(ORACLE(SHARED_DRIVE_ID, "不存在的目录"))

    def test_missing_dir_nonstrict_parity(self, drive_env, monkeypatch):
        fake, _ = drive_env
        assert list(sg.iter_drive_videos(
            SHARED_DRIVE_ID, "不存在的目录", strict=False)) == []
        assert list(ORACLE(SHARED_DRIVE_ID, "不存在的目录",
                           strict=False)) == []

    def test_skip_rules_parity(self, drive_env, monkeypatch):
        # 隐藏/系统/删除/非视频在新旧两边都被排除
        fake, _ = drive_env
        new, _, old, _ = _run_both(
            monkeypatch, fake, SHARED_DRIVE_ID, "MoviePilot")
        rels = {r for r, _, _ in new}
        assert not any(".hidden" in r for r in rels)
        assert not any("@eaDir" in r for r in rels)
        assert not any(r.endswith(".nfo") for r in rels)
        assert not any("gone" in r for r in rels)
        assert not any("orphan" in r for r in rels)
        # 多 parent 文件在两边各出现两次（每条 parent 链一次）
        assert sum(1 for r, _, _ in new if r.endswith("multi.mkv")) == 2
        # 快捷方式：旧行为会列出（非目录+扩展名命中），新实现保持一致
        assert sum(1 for r, _, _ in new if r.endswith("link.mkv")) == 1
        # 名字像视频的目录：不当文件产出，但其内部文件正常列出
        assert not any(r.endswith("/weird.mkv") and r.count("/") == 3
                       for r in rels)
        assert "MoviePilot/剧集/国产剧/weird.mkv/inner.mkv" in rels

    def test_resolve_dir_id_quote_escaping(self, drive_env, monkeypatch):
        fake, _ = drive_env
        fetch = sg._page_fetcher(SHARED_DRIVE_ID)
        assert fetch is not None
        got = sg._resolve_dir_id(fetch, SHARED_DRIVE_ID, "Q' Dir")
        assert got == "fld_q"


class TestRoundIsFullyIncremental:
    def _r(self, did, inc, err=None):
        return {"did": did, "incremental_listing": inc, "error": err}

    def test_all_incremental(self):
        rs = [self._r("a", True), self._r("b", True)]
        assert sg._round_is_fully_incremental("drive", False, rs) is True

    def test_full_round(self):
        rs = [self._r("a", True)]
        assert sg._round_is_fully_incremental("drive", True, rs) is False

    def test_local_source(self):
        assert sg._round_is_fully_incremental("local", False, []) is False

    def test_mixed_fallback(self):
        rs = [self._r("a", True), self._r("b", False)]
        assert sg._round_is_fully_incremental("drive", False, rs) is False

    def test_all_failed(self):
        rs = [self._r("a", True, err="boom")]
        assert sg._round_is_fully_incremental("drive", False, rs) is False

    def test_failed_drive_excluded(self):
        # 失败的盘被跳过：只看成功列举的盘
        rs = [self._r("a", True), self._r("b", True, err="boom")]
        assert sg._round_is_fully_incremental("drive", False, rs) is True


class TestVerifyCompletenessLight:
    def _make(self, tmp_path):
        # state：3 个目录；A/B 被本轮 touch，C 不被 touch
        def w(name):
            p = tmp_path / name
            p.write_text("x")
            return name
        videos = [  # 本轮 delta：A 新增 1 个，B 新增 1 个（但 state 缺行 → 缺集）
            ("MoviePilot/A/a2.mkv", "fa2", 10),
            ("MoviePilot/B/b2.mkv", "fb2", 20),
        ]
        state = {
            "MoviePilot/A/a1.mkv": ("fa1", 10, w("a1.strm")),
            "MoviePilot/A/a2.mkv": ("fa2", 10, w("a2.strm")),
            "MoviePilot/B/b1.mkv": ("fb1", 20, w("b1.strm")),
            # B/b2 缺行 → not_generated
            "MoviePilot/C/c1.mkv": ("fc1", 30, "missing.strm"),
            # C 不被 touch：它的磁盘缺失不应被轻量校验计入
        }
        return videos, state

    def test_light_detects_delta_gap(self, tmp_path):
        videos, state = self._make(tmp_path)
        r = sg._verify_completeness_light(videos, state, "MoviePilot/",
                                          strm_root=str(tmp_path))
        assert r["verify_mode"] == "light"
        assert r["total_series"] == 2  # 只统计 touch 过的目录
        by_series = {i["series"]: i for i in r["incomplete"]}
        assert set(by_series) == {"B"}
        assert by_series["B"]["missing_breakdown"]["not_generated"] == 1
        # C 的僵尸不在轻量范围内
        assert r["orphan_disk"]["count"] == 0

    def test_light_scoped_orphan(self, tmp_path):
        videos = [("MoviePilot/A/a2.mkv", "fa2", 10)]
        (tmp_path / "a2.strm").write_text("x")
        state = {
            "MoviePilot/A/a1.mkv": ("fa1", 10, "gone1.strm"),  # touch 目录的僵尸
            "MoviePilot/A/a2.mkv": ("fa2", 10, "a2.strm"),
            "MoviePilot/Z/z9.mkv": ("fz9", 90, "gone9.strm"),  # 未 touch，不计
        }
        r = sg._verify_completeness_light(videos, state, "MoviePilot/",
                                          strm_root=str(tmp_path))
        assert r["orphan_disk"]["count"] == 1
        assert r["orphan_disk"]["sample"][0]["remote_path"] == "MoviePilot/A/a1.mkv"

    def test_light_empty_delta(self):
        r = sg._verify_completeness_light([], {}, "MoviePilot/")
        assert r["verify_mode"] == "light"
        assert r["total_series"] == 0
        assert r["incomplete"] == []

    def test_light_schema_matches_full(self, tmp_path):
        videos, state = self._make(tmp_path)
        light = sg._verify_completeness_light(videos, state, "MoviePilot/",
                                              strm_root=str(tmp_path))
        full = sg._verify_completeness(videos, state, "MoviePilot/",
                                       strm_root=str(tmp_path))
        assert set(light) == set(full)  # 同形：下游消费不 break
