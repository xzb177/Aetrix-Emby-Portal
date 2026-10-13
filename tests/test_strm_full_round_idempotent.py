"""回归：全量轮幂等——已登记文件不改名（82,348 个幽灵的根因）。

今晚（2026-10-14）生产实测：全量轮把自己已登记的文件全当成了撞名，
192,761 个文件里 80,997 个被批量改名成 -2/-3，磁盘上留下 82,348 个孤儿
（磁盘有、DB 无记录）。

机制：全量模式 state 被故意重置（state={}），但防撞名集合 used 是从磁盘
walk 加载的。主循环的 old = state.get((did, rel)) 在全量下恒为 None，
于是每个文件都走 _resolve_collision——磁盘上自己上一轮的文件还在，
全部被误判成撞名改名。DB 按 (drive_id, remote_path) 主键 upsert 掩盖了
这一切（行数对、路径全变 -2），老文件在磁盘上成孤儿。

修复要求：同一 (drive_id, remote_path) 在 DB 里已有登记路径时，全量轮
必须复用该路径原样重写/校验，绝不改名；改名只许发生在"两个不同 file_id
真映射到同一目标名"的真撞名。
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
    """与 test_strm_gen.TestRunGenerationMocked._patch_all 同口径。
    videos 可为 list（各盘相同）或 callable(drive_id) -> list。"""
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


class TestFullRoundIdempotent:
    def test_full_round_reuses_registered_paths(self, tmp_path, monkeypatch):
        """今晚场景复现：磁盘+DB 都有旧记录 → 全量重跑 → 零改名、零新增幽灵。"""
        rels = [
            "MoviePilot/剧集/A/Season 1/A - S01E01.mkv",
            "MoviePilot/剧集/A/Season 1/A - S01E02.mkv",
            "MoviePilot/电影/B (2024)/B - 1080p.mkv",
        ]
        vids = [(r, f"fid{i}", 100 + i) for i, r in enumerate(rels)]
        state = {("d1", r): (f"fid{i}", 100 + i, sg.strm_relpath(r, "MoviePilot/"))
                 for i, r in enumerate(rels)}
        # 磁盘上是上轮生成的文件（内容陈旧也没关系）
        for (_did, _rel), (_fid, _size, spath) in state.items():
            p = tmp_path / spath
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("stale content")

        saved, _deleted = _patch_all(tmp_path, monkeypatch, vids, state=state)
        try:
            res = sg.run_generation(_FakeDb({}), full=True)
        finally:
            sg._set_progress(running=False, phase="idle")

        assert res["ok"] is True
        assert res["stats"]["generated"] == 3
        assert res["stats"]["failed"] == 0
        # 零改名：磁盘上没有任何 -2/-3 后缀文件
        renamed = [p for p in tmp_path.rglob("*.strm")
                   if p.stem.endswith(("-2", "-3"))]
        assert renamed == []
        # 零新增幽灵：磁盘文件集合 == DB 登记路径集合
        on_disk = {p.relative_to(tmp_path).as_posix()
                   for p in tmp_path.rglob("*.strm")}
        assert on_disk == {v[2] for v in state.values()}
        # DB 行路径原样（没有被改成 -2）
        for key, row in saved.items():
            assert row[4] == state[key][2]
        # 内容是本轮重写过的（新 URL 在内容里）
        for (_did, _rel), (_fid, _size, spath) in state.items():
            assert _fid in (tmp_path / spath).read_text()

    def test_full_round_true_collision_still_separated(self, tmp_path, monkeypatch):
        """真撞名：两盘同一 rel → 同一目标名、大小不同（多版本），后者必须分开（仍走 -2）。

        注：同 stem 同大小如今走去重（用户 2026-10-14 亲定规则，只留一份），
        见 tests/test_strm_gen_dedupe.py；此处用不同大小覆盖"真撞名仍分离"。
        """
        def per_drive(drive_id):
            return [("MoviePilot/剧集/x.mkv", f"fid-{drive_id}",
                     100 if drive_id == "d1" else 200)]

        _patch_all(tmp_path, monkeypatch, per_drive,
                    drives={"d1": ["s"], "d2": ["s"]})
        try:
            res = sg.run_generation(_FakeDb({}), full=True)
        finally:
            sg._set_progress(running=False, phase="idle")

        assert res["ok"] is True
        assert res["stats"]["generated"] == 2
        p1 = tmp_path / "剧集/x.strm"
        p2 = tmp_path / "剧集/x-2.strm"
        assert p1.exists() and p2.exists()
        texts = {p1.read_text(), p2.read_text()}
        assert any("fid-d1" in t for t in texts)
        assert any("fid-d2" in t for t in texts)
