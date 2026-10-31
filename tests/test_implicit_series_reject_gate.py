"""A-097：隐式 series 被绑定复核拒绝后，详情仍落库污染 series 行。

旧代码只在拒绝时跳过 apply()，平级的 apply_details 照常执行：错误 show 的
imdb_id / 简介 / 评分 / 类型 / 别名 / metadata_source 全写进 series 行，
随后 last_scraped_at 又记为已尝试。修法：拒绝分支不执行 apply_details，
且不记 last_scraped_at；NFO 补图也不用被拒绝的详情。
"""
import inspect
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

from types import SimpleNamespace
from unittest import mock

from backend.emby_server import scanner

_WRONG_DETAILS = {
    "id": 999, "media_type": "tv", "name": "Wrong Show",
    "first_air_date": "1999-01-01",
    "overview": "wrong overview", "vote_average": 9.9,
    "genres": [{"id": 1, "name": "WrongGenre"}],
    "external_ids": {"imdb_id": "tt9999999"},
    "alternative_titles": {"results": [{"title": "Wrong Alias"}]},
}
_WRONG_HIT = {"id": 999, "media_type": "tv", "name": "Wrong Show",
              "first_air_date": "1999-01-01"}
_RIGHT_DETAILS = dict(_WRONG_DETAILS, first_air_date="2024-01-01",
                       name="Right Show")
_RIGHT_HIT = {"id": 100, "media_type": "tv", "name": "Right Show",
              "first_air_date": "2024-01-01"}


def _series_row(**kw):
    base = dict(name="Right Show", production_year=2024, tmdb_id=None,
                imdb_id=None, overview=None, community_rating=None,
                genres=None, aliases=None, metadata_source=None,
                last_scraped_at=None, poster_path=None, primary_image_url=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _ctx(hit, details):
    return SimpleNamespace(
        series_tmdb_results={"sg1": (hit, details)} if hit is not None else {},
        series_tmdb_searched={"sg1"},
        nfo_series_details={"sg1": details} if details is not None else {},
    )


def test_rejected_hit_applies_nothing(monkeypatch):
    # 年份 1999 vs 条目 2024：真实 binding_reject_reason 判 year_mismatch
    series = _series_row()
    stats = {}
    with mock.patch.object(scanner._tmdb.tmdb_client, "apply") as apply, \
            mock.patch.object(scanner._tmdb.tmdb_client, "apply_details") as details:
        applied, rejected = scanner._apply_implicit_series_tmdb(
            _ctx(_WRONG_HIT, _WRONG_DETAILS), series, "sg1", stats)
    assert (applied, rejected) == (False, True)
    apply.assert_not_called()
    details.assert_not_called()
    assert series.tmdb_id is None
    assert series.imdb_id is None
    assert series.overview is None
    assert series.metadata_source is None
    assert stats == {}


def test_accepted_hit_applies_both(monkeypatch):
    from collections import defaultdict
    series = _series_row()
    stats = defaultdict(int)  # 生产侧 stats 为 defaultdict，与写库循环一致
    with mock.patch.object(scanner._tmdb.tmdb_client, "apply") as apply, \
            mock.patch.object(scanner._tmdb.tmdb_client, "apply_details") as details:
        applied, rejected = scanner._apply_implicit_series_tmdb(
            _ctx(_RIGHT_HIT, _RIGHT_DETAILS), series, "sg1", stats)
    assert (applied, rejected) == (True, False)
    apply.assert_called_once()
    details.assert_called_once()
    assert stats.get("scraped") == 1


def test_no_result_or_existing_tmdb_is_noop(monkeypatch):
    series = _series_row()
    with mock.patch.object(scanner._tmdb.tmdb_client, "apply") as apply, \
            mock.patch.object(scanner._tmdb.tmdb_client, "apply_details") as details:
        assert scanner._apply_implicit_series_tmdb(
            _ctx(None, None), series, "sg1", {}) == (False, False)
        assert scanner._apply_implicit_series_tmdb(
            _ctx(_RIGHT_HIT, _RIGHT_DETAILS), _series_row(tmdb_id="100"),
            "sg1", {}) == (False, False)
    apply.assert_not_called()
    details.assert_not_called()


def test_write_loop_guards_use_rejection_flag():
    # 结构守卫：写库循环里 last_scraped_at 的标记与 NFO 补图都必须以
    # 拒绝标志为守卫，否则“被拒绝的错误命中”会被当成“干净地搜过”。
    src = inspect.getsource(scanner._scan_library_body)
    assert "and not _series_rej" in src
    assert "if details is not None and not _series_rej:" in src
