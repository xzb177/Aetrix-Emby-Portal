"""多源补缺（2026-10-23 用户口径）：TMDB 已认出但缺字段时去其他源补。

与 TMDB-miss 兜底分工：miss 是「没认出」走识别；补缺是「认出了但不全」
（缺海报/简介/类型/评分）走多源引擎，结果共用写库阶段 Phase 6b 落库点
（apply_to_item 只填空缺）。本地/网盘（side 图、NFO）已给的不算缺。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = "sqlite:///" + _tmppath

from backend import database as _dbmod  # noqa: E402
from sqlalchemy import create_engine as _ce  # noqa: E402
from sqlalchemy.orm import sessionmaker as _sm  # noqa: E402

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

from types import SimpleNamespace  # noqa: E402
from unittest import mock  # noqa: E402

from backend.database import init_db  # noqa: E402
from backend.emby_server import enrich_worker as ew  # noqa: E402

init_db()


def _item(**over):
    base = dict(
        name="测试电影", production_year=2024, tmdb_id=None, imdb_id=None,
        poster_path=None, primary_image_url=None, overview="",
        genres="", community_rating=None, file_path="/x/test.mkv",
        item_type="movie",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _result(**over):
    base = {
        "poster": None, "fanart": None, "external_subs": [],
        "nfo_data": None, "tmdb_hit": None, "tmdb_details": None,
        "ok": True, "error": None,
    }
    base.update(over)
    return base


_FULL_DETAILS = {
    "poster_path": "/p.jpg", "overview": "简介",
    "genres": [{"name": "剧情"}], "vote_average": 8.0,
}


class TestMissingFields:
    def test_all_missing_when_tmdb_gave_nothing(self):
        item = _item()
        result = _result(tmdb_hit={"id": 1}, tmdb_details={})
        assert ew._tmdb_stage_missing_fields(item, result) == [
            "poster", "overview", "genres", "rating"]

    def test_nothing_missing_when_details_full(self):
        item = _item()
        result = _result(tmdb_hit={"id": 1}, tmdb_details=dict(_FULL_DETAILS))
        assert ew._tmdb_stage_missing_fields(item, result) == []

    def test_item_existing_values_count(self):
        item = _item(poster_path="/local.jpg", overview="已有简介",
                     genres="喜剧", community_rating=7.5)
        result = _result(tmdb_hit={"id": 1}, tmdb_details={})
        assert ew._tmdb_stage_missing_fields(item, result) == []

    def test_side_poster_and_nfo_count(self):
        # 本地/网盘已给的（side 海报、NFO 简介/类型/评分）不算缺，不白问多源
        item = _item()
        result = _result(
            poster="/side/poster.jpg",
            nfo_data={"plot": "NFO 简介", "genres": ["动画"], "rating": 8.1},
            tmdb_hit={"id": 1}, tmdb_details={})
        assert ew._tmdb_stage_missing_fields(item, result) == []

    def test_hit_poster_path_counts(self):
        item = _item(overview="有", genres="有", community_rating=1.0)
        result = _result(tmdb_hit={"id": 1, "poster_path": "/h.jpg"})
        assert ew._tmdb_stage_missing_fields(item, result) == []


class TestFillGate:
    def setup_method(self):
        self._orig_master = ew._multisource_master_on

    def teardown_method(self):
        ew._multisource_master_on = self._orig_master

    def _master(self, monkeypatch, on=True):
        monkeypatch.setattr(ew, "_multisource_master_on", lambda: on)

    def test_hit_but_incomplete_goes(self, monkeypatch):
        self._master(monkeypatch)
        result = _result(tmdb_hit={"id": 1}, tmdb_details={"overview": "有"})
        assert ew._should_multisource_fill_missing(
            _item(), "movie", result) is True

    def test_hit_and_complete_skips(self, monkeypatch):
        self._master(monkeypatch)
        result = _result(tmdb_hit={"id": 1}, tmdb_details=dict(_FULL_DETAILS))
        assert ew._should_multisource_fill_missing(
            _item(), "movie", result) is False

    def test_nfo_tmdb_id_counts_as_identified(self, monkeypatch):
        self._master(monkeypatch)
        result = _result(tmdb_id="42", nfo_data={"title": "X"})
        assert ew._should_multisource_fill_missing(
            _item(), "series", result) is True

    def test_unidentified_skips(self, monkeypatch):
        # 没认出的条目归 miss 兜底管，这里不插手（哪怕什么都缺）
        self._master(monkeypatch)
        result = _result()
        assert ew._should_multisource_fill_missing(
            _item(), "movie", result) is False

    def test_episode_and_season_skip(self, monkeypatch):
        self._master(monkeypatch)
        result = _result(tmdb_hit={"id": 1}, tmdb_details={})
        assert ew._should_multisource_fill_missing(
            _item(), "episode", result) is False
        assert ew._should_multisource_fill_missing(
            _item(), "season", result) is False

    def test_already_collected_skips(self, monkeypatch):
        self._master(monkeypatch)
        result = _result(tmdb_hit={"id": 1}, tmdb_details={},
                         multisource={"fields": {}})
        assert ew._should_multisource_fill_missing(
            _item(), "movie", result) is False

    def test_master_switch_off_skips(self, monkeypatch):
        self._master(monkeypatch, on=False)
        result = _result(tmdb_hit={"id": 1}, tmdb_details={})
        assert ew._should_multisource_fill_missing(
            _item(), "movie", result) is False


def _run_post(monkeypatch, result, item, apply_fn, guarded):
    """驱动 _enrich_fetch_tmdb_post（批量路径），盯补缺钩子是否触发。"""
    from backend.emby_server import tmdb as tmdb_mod

    stages = []

    class _Prog:
        def note_stage(self, s):
            stages.append(s)

    ctx = {"kind": "movie", "needs_repair": False,
           "tmdb_plan": {"op": "search", "name": "测试电影",
                         "year": 2024, "kind": "movie"}}
    monkeypatch.setattr(ew, "_apply_tmdb_plan_result", apply_fn)
    monkeypatch.setattr(ew, "_douban_ban_downgrade_active", lambda: False)
    monkeypatch.setattr(ew, "_multisource_master_on", lambda: True)
    monkeypatch.setattr(ew, "_collect_multisource_guarded", guarded)
    monkeypatch.setattr(ew, "progress", _Prog())
    monkeypatch.setattr(tmdb_mod, "poster_language", lambda: "system")
    monkeypatch.setattr(tmdb_mod, "prewarm_images", lambda *a, **k: [])
    monkeypatch.setattr(tmdb_mod, "cast_list", lambda *a, **k: [])
    out = ew._enrich_fetch_tmdb_post(
        result, item, ctx,
        batch_out={"hit": None, "details": None, "transient": False})
    return out, stages


class TestFillHook:
    def test_fill_runs_on_hit_but_incomplete(self, monkeypatch):
        calls = []

        def apply_fn(result, item, plan, batch_out, kind):
            result["tmdb_hit"] = {"id": 7}
            result["tmdb_details"] = {"overview": "只有简介"}

        def guarded(item, kind, result):
            calls.append(kind)
            result["multisource"] = {"fields": {"poster": "https://x/p.jpg"}}
            return True

        out, stages = _run_post(
            monkeypatch, _result(), _item(), apply_fn, guarded)
        assert calls == ["movie"]
        assert out["multisource"]["fields"]["poster"] == "https://x/p.jpg"
        assert "enrich_multisource_fill" in stages

    def test_fill_skips_when_complete(self, monkeypatch):
        calls = []

        def apply_fn(result, item, plan, batch_out, kind):
            result["tmdb_hit"] = {"id": 7}
            result["tmdb_details"] = dict(_FULL_DETAILS)

        def guarded(item, kind, result):
            calls.append(kind)
            return True

        _run_post(monkeypatch, _result(), _item(), apply_fn, guarded)
        assert calls == []

    def test_fill_skips_when_fetch_failed(self, monkeypatch):
        calls = []

        def apply_fn(result, item, plan, batch_out, kind):
            raise RuntimeError("tmdb boom")

        def guarded(item, kind, result):
            calls.append(kind)
            return True

        out, _stages = _run_post(
            monkeypatch, _result(), _item(), apply_fn, guarded)
        assert out["ok"] is False
        assert calls == []
