"""回归：同名同大小去重（2026-10-14 用户亲定规则）。

背景：全量轮改名缺陷产出 82,348 个改名孤儿（同一网盘文件两份 .strm），
其中 1,239 组同版本同大小的真重复已手工清掉 1,863 个文件，但网盘本体
还在——下轮生成会因无 DB 登记行而把这些重复重新生成出来。生成器侧必须
治本：待生成文件与已登记/已生成文件同输出 stem、同字节大小 → 只留一份。

用户规则：
- 同作品同集、版本相同（同输出 stem）、大小相同 → 只留一份，其余跳过；
- 版本不同或大小不同 → 合法多版本，全部保留（-2 改名逻辑不变）；
- 判定只依赖 (输出 stem, size)，不解析标题/集数做语义猜测。

覆盖：
- ② 不同 file_id、同 stem、同 size → 只生成一份，deduped 计数正确；
- ②b 增量发现：keeper 已登记在盘，重复以新 (did, rel) 到达 → 跳过；
- ③ 不同 file_id、同 stem、不同 size → 两份都生成（多版本保留）；
- ④ 开关关 → 行为与现状一致（同大小也改名保留两份）；
- ⑤ 跨源目录（nastool/ vs MoviePilot/ 同内容）→ 去重生效。

注：① 同一 file_id 第二轮不再改名由 #834 的 test_strm_full_round_idempotent.py
覆盖（全量幂等复用），此处不重复造。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.emby_server import strm_gen as sg


class _FakeDb:
    def __init__(self, values):
        self._values = values


@pytest.fixture(autouse=True)
def _patch_db_config(monkeypatch):
    def fake_db_config(db, key, default=""):
        if isinstance(db, _FakeDb):
            return db._values.get(key, default)
        return default

    monkeypatch.setattr(sg, "_db_config", fake_db_config)


def _patch_all(tmp_path, monkeypatch, videos, state=None, drives=None):
    """与 test_strm_full_round_idempotent._patch_all 同口径。"""
    get = videos if callable(videos) else (lambda drive_id: list(videos))
    monkeypatch.setattr(
        sg, "iter_drive_videos",
        lambda drive_id, dir_path, strict=True, **kwargs: iter(get(drive_id)),
    )
    import backend.emby_server.strm_source as ssrc

    def fake_lister_iter(self, incremental=True):
        return sg.iter_drive_videos(self.drive_id, self.dir_path)

    monkeypatch.setattr(ssrc.DriveChangesLister, "iter_videos", fake_lister_iter)
    monkeypatch.setattr(ssrc.DriveChangesLister, "save_checkpoint", lambda self: None)
    monkeypatch.setattr(sg, "_discover_drives",
                        lambda: drives if drives is not None else {"d1": ["sa1"]})
    monkeypatch.setattr(sg, "_container_strm_root", lambda db: str(tmp_path))
    monkeypatch.setattr(sg, "_load_state", lambda: dict(state or {}))
    saved: dict = {}
    monkeypatch.setattr(sg, "_save_state_rows",
                        lambda rows: saved.update({(r[0], r[1]): r for r in rows}))
    deleted: list = []
    monkeypatch.setattr(sg, "_delete_state_rows",
                        lambda rows, db=None: deleted.extend(rows))
    import backend.integrations.store as _store
    monkeypatch.setattr(_store, "write_values", lambda *a, **k: 0)
    return saved, deleted


def _run(monkeypatch, tmp_path, videos, db_values, state=None):
    _patch_all(tmp_path, monkeypatch, videos, state=state)
    try:
        return sg.run_generation(_FakeDb(db_values))
    finally:
        sg._set_progress(running=False, phase="idle")


def _strm_files(tmp_path):
    return sorted(str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*.strm"))


STEM = "剧集/A/Season 1/A - S01E01 - 1080p WEB.strm"


def _two_drives(monkeypatch, tmp_path, vids_by_drive, db_values, state=None):
    """双盘 harness：同一 rel 在两个盘各有一份（不同 file_id）。"""
    saved, _deleted = _patch_all(tmp_path, monkeypatch,
                                 lambda drive_id: list(vids_by_drive.get(drive_id, [])),
                                 state=state,
                                 drives={"d1": ["sa1"], "d2": ["sa2"]})
    try:
        return sg.run_generation(_FakeDb(db_values))
    finally:
        sg._set_progress(running=False, phase="idle")


REL = "MoviePilot/剧集/A/Season 1/A - S01E01 - 1080p WEB.mkv"


class TestDedupeSameVersionSize:
    def test_same_stem_same_size_only_one_generated(self, tmp_path, monkeypatch):
        """② 不同 file_id、同 stem、同 size → 只生成一份，deduped=1。"""
        vids = {
            "d1": [(REL, "fid1", 100)],
            "d2": [(REL, "fid2", 100)],
        }
        res = _two_drives(monkeypatch, tmp_path, vids, {})
        assert res["ok"] is True
        stats = res["stats"]
        assert stats["generated"] == 1
        assert stats["deduped"] == 1
        assert stats["failed"] == 0
        files = _strm_files(tmp_path)
        assert files == [STEM]
        assert not any("-2" in f for f in files)

    def test_incremental_duplicate_of_registered_keeper_skipped(self, tmp_path, monkeypatch):
        """②b 增量：keeper 已登记在盘，重复以新 (did, rel) 到达 → 跳过生成。"""
        state = {("d1", REL): ("fid1", 100, STEM)}
        p = tmp_path / STEM
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("signed-url-placeholder")
        vids = {
            "d1": [(REL, "fid1", 100)],
            "d2": [(REL, "fid2", 100)],
        }
        res = _two_drives(monkeypatch, tmp_path, vids, {}, state=state)
        assert res["ok"] is True
        stats = res["stats"]
        # d1 keeper 走增量 skip（同 fid 同 size），d2 副本走 dedupe
        assert stats["deduped"] == 1
        assert stats["failed"] == 0
        files = _strm_files(tmp_path)
        assert files == [STEM]

    def test_same_stem_diff_size_keeps_both(self, tmp_path, monkeypatch):
        """③ 不同 file_id、同 stem、不同 size → 多版本，两份都生成。"""
        vids = {
            "d1": [(REL, "fid1", 100)],
            "d2": [(REL, "fid2", 200)],
        }
        res = _two_drives(monkeypatch, tmp_path, vids, {})
        assert res["ok"] is True
        stats = res["stats"]
        assert stats["generated"] == 2
        assert stats["deduped"] == 0
        files = _strm_files(tmp_path)
        assert files == sorted([STEM, STEM.replace(".strm", "-2.strm")])

    def test_switch_off_restores_legacy_behavior(self, tmp_path, monkeypatch):
        """④ 开关关 → 现状行为：同 stem 同 size 也改名保留两份。"""
        vids = {
            "d1": [(REL, "fid1", 100)],
            "d2": [(REL, "fid2", 100)],
        }
        res = _two_drives(monkeypatch, tmp_path, vids,
                          {sg.CONFIG_DEDUPE_SAME_SIZE: "false"})
        assert res["ok"] is True
        stats = res["stats"]
        assert stats["generated"] == 2
        assert stats["deduped"] == 0
        files = _strm_files(tmp_path)
        assert files == sorted([STEM, STEM.replace(".strm", "-2.strm")])

    def test_full_round_same_stem_same_size_deduped(self, tmp_path, monkeypatch):
        """全量轮：两盘同一 rel、同大小的新文件 → 去重只留一份。

        与 #834 复用逻辑共存：(did, rel) 无登记行才进防撞路径，
        去重只作用于真新文件，已登记的走复用分支（见 #834 用例）。
        """
        vids = {
            "d1": [(REL, "fid1", 100)],
            "d2": [(REL, "fid2", 100)],
        }
        saved, _deleted = _patch_all(tmp_path, monkeypatch,
                                     lambda drive_id: list(vids.get(drive_id, [])),
                                     drives={"d1": ["sa1"], "d2": ["sa2"]})
        try:
            res = sg.run_generation(_FakeDb({}), full=True)
        finally:
            sg._set_progress(running=False, phase="idle")
        assert res["ok"] is True
        stats = res["stats"]
        assert stats["generated"] == 1
        assert stats["deduped"] == 1
        files = _strm_files(tmp_path)
        assert files == [STEM]

    def test_cross_source_dirs_deduped(self, tmp_path, monkeypatch):
        """⑤ 跨源目录：nastool/ 与 MoviePilot/ 同内容同大小 → 去重生效。"""
        vids = [
            ("MoviePilot/剧集/A/Season 1/A - S01E01 - 1080p WEB.mkv", "fid1", 100),
            ("nastool/剧集/A/Season 1/A - S01E01 - 1080p WEB.mkv", "fid2", 100),
        ]
        res = _run(monkeypatch, tmp_path, vids,
                   {"strm_gen_source_dir": "MoviePilot/,nastool/"})
        assert res["ok"] is True
        stats = res["stats"]
        assert stats["generated"] == 1
        assert stats["deduped"] == 1
        files = _strm_files(tmp_path)
        assert files == [STEM]

    def test_unknown_size_never_deduped(self, tmp_path, monkeypatch):
        """R34：大小未知（size=0）的两个不同文件 → 不去重，各自生成（-2 改名）。"""
        vids = {
            "d1": [(REL, "fid1", 0)],
            "d2": [(REL, "fid2", 0)],
        }
        res = _two_drives(monkeypatch, tmp_path, vids, {})
        assert res["ok"] is True
        stats = res["stats"]
        assert stats["generated"] == 2
        assert stats["deduped"] == 0
        files = _strm_files(tmp_path)
        assert len(files) == 2
        assert any("-2" in f for f in files)

    def test_progress_has_deduped_key(self, tmp_path, monkeypatch):
        """R34：_progress 初始即含 deduped 键；reset 后清零不残留。"""
        assert "deduped" in sg._progress
        sg._set_progress(deduped=7)
        assert sg._progress["deduped"] == 7
        sg._reset_progress()
        assert sg._progress["deduped"] == 0
        sg._set_progress(running=False, phase="idle")


class TestDedupeConfigReader:
    def test_default_on(self):
        assert sg.dedupe_same_size_enabled(_FakeDb({})) is True

    def test_explicit_off(self):
        assert sg.dedupe_same_size_enabled(
            _FakeDb({sg.CONFIG_DEDUPE_SAME_SIZE: "false"})) is False

    def test_truthy_forms(self):
        for v in ("1", "true", "yes", "on", "TRUE"):
            assert sg.dedupe_same_size_enabled(
                _FakeDb({sg.CONFIG_DEDUPE_SAME_SIZE: v})) is True
