"""Hark 6 链路第 3 项（刮削）复现测试。

覆盖：
1. 年份解析：Blade Runner 2049 (2017) / Wonder Woman 1984 / Inception.2010 / Dune.2021
2. 集号解析：独立 E05 / 绝对集数 Frieren - 28 / 中文数字第十二集 / 4 位集数
3. 资料不全：退避 + 最大次数 → failed（不再无限空转）
4. 手动锁定：bind 后自动锁定；抢单后被锁定 → apply 不覆盖
5. 原子抢单：双会话下同一批候选只被赢走一次
6. TMDB 年份回退：带年份搜空 → 去年份再搜
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db"); os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"
from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm
_dbmod.engine = _ce(os.environ["DATABASE_URL"], connect_args={"timeout": 2})
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import uuid
from datetime import datetime, timedelta
from unittest import mock

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker
from backend.emby_server.scanner import parse_media_filename as pmf

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(em.MediaStream).delete()
        s.query(em.MediaItem).delete()
        s.commit()
        yield s
    finally:
        s.close()


def _make_item(db, **kw):
    it = em.MediaItem(
        guid=uuid.uuid4().hex,
        library_id=kw.pop("library_id", 1),
        item_type=kw.pop("item_type", "movie"),
        name=kw.pop("name", "测试"),
        file_path=kw.pop("file_path", "/tmp/x.mp4"),
        enrich_status=kw.pop("enrich_status", "pending"),
        **kw,
    )
    db.add(it)
    db.commit()
    return it


# ==================== 1. 年份解析 ====================

def test_year_parenthesized_beats_title_number():
    """Blade Runner 2049 (2017)：括号年份优先，2049 是片名不是年份"""
    r = pmf("/m/Blade Runner 2049 (2017).mkv", "movies")
    assert r["year"] == 2017, r
    assert "2017" not in r["name"], r
    assert "2049" in r["name"], r  # 2049 是片名的一部分，保留


def test_year_trailing_bare_number():
    """Wonder Woman 1984：末尾裸年份要认到"""
    r = pmf("/m/Wonder Woman 1984.mkv", "movies")
    assert r["year"] == 1984, r


def test_year_dot_separated_end_of_stem():
    """Inception.2010.mkv：点分隔、位于 stem 末尾"""
    r = pmf("/m/Inception.2010.mkv", "movies")
    assert r["year"] == 2010, r
    assert r["name"] == "Inception", r


def test_year_dune_2021():
    """Dune.2021.mkv：同名不同年的根因，年份必须解析出来"""
    r = pmf("/m/Dune.2021.mkv", "movies")
    assert r["year"] == 2021, r


def test_year_title_that_is_a_year():
    """2012.2009.1080p.mkv：片名本身就是年份时，取真正的上映年（最后一个有效候选）"""
    r = pmf("/m/2012.2009.1080p.mkv", "movies")
    assert r["year"] == 2009, r


def test_year_regression_parenthesized():
    r = pmf("/m/Some Movie (2019).mkv", "movies")
    assert r["year"] == 2019, r


def test_year_not_confused_by_resolution():
    r = pmf("/m/Some Movie.1080p.WEB-DL.mkv", "movies")
    assert r["year"] is None, r


# ==================== 2. 集号解析 ====================

def test_episode_standalone_e():
    """独立 E05（不带季号）"""
    r = pmf("/t/Show/Show E05.mkv", "tvshows")
    assert (r["season"], r["episode"]) == (1, 5), r


def test_episode_absolute_number():
    """动画绝对集数：Frieren - 28"""
    r = pmf("/t/Frieren/Frieren - 28.mkv", "tvshows")
    assert (r["season"], r["episode"]) == (1, 28), r
    assert r["name"] == "Frieren", r


def test_episode_chinese_numeral():
    """第十二集：中文数字"""
    r = pmf("/t/Show/Show 第十二集.mkv", "tvshows")
    assert (r["season"], r["episode"]) == (1, 12), r


def test_episode_four_digits():
    """4 位集数"""
    r = pmf("/t/Show/Show E1234.mkv", "tvshows")
    assert (r["season"], r["episode"]) == (1, 1234), r


def test_episode_regression_sxxeyy():
    r = pmf("/t/Show/Show S01E02.mkv", "tvshows")
    assert (r["season"], r["episode"]) == (1, 2), r


def test_episode_regression_chinese():
    r = pmf("/t/Show/Show 第5集.mkv", "tvshows")
    assert (r["season"], r["episode"]) == (1, 5), r


def test_episode_season_pack_not_episode():
    """季包 The 100 Season 1：不能把 Season 1 的 1 当成集号"""
    r = pmf("/t/The 100/The 100 Season 1.mkv", "tvshows")
    assert r["season"] == 1, r
    assert r["episode"] is None, r
    assert r["name"] == "The 100", r


def test_movie_trailing_number_not_episode():
    """电影库：Movie 2 不能被误判成 episode（item_type 判定依赖 season is not None）"""
    r = pmf("/m/Movie 2/Movie 2.mkv", "movies")
    assert r["season"] is None and r["episode"] is None, r


def test_episode_number_like_year_not_double_counted():
    """S01E2019：E 后面的年份段数字是集号，不应再被当成年份"""
    r = pmf("/t/Show/Show S01E2019.mkv", "tvshows")
    assert r["episode"] == 2019, r
    assert r["year"] is None, r


# ==================== 3. 资料不全：退避 + 上限 ====================

def _apply_incomplete(db, item, attempts):
    item.enrich_status = "enriching"
    item.enrich_attempts = attempts
    db.commit()
    # 只有标题、没有年份/海报/tmdb_id → _incomplete
    item.name = "只有标题的片子"
    item.production_year = None
    item.poster_path = None
    item.primary_image_url = None
    item.tmdb_id = None
    item.overview = None
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as tc:
        tc.configured = False
        enrich_worker._enrich_apply(db, item, {"ok": True})
    db.commit()
    db.refresh(item)
    return item


def test_incomplete_backs_off_instead_of_reset(db):
    """资料不全不再清零 attempts、无退避：应计次 + 指数退避"""
    item = _make_item(db, item_type="movie", enrich_status="pending")
    _apply_incomplete(db, item, 0)
    assert item.enrich_status == "pending", item.enrich_status
    assert item.enrich_attempts == 1, item.enrich_attempts
    assert item.enrich_next_retry_at is not None
    delta = (item.enrich_next_retry_at - datetime.now()).total_seconds()
    assert 30 < delta <= 90, delta  # 基数 60s 退避


def test_incomplete_fails_after_max_attempts(db, monkeypatch):
    """资料不全 5 次后转 failed，不再无限空转。

    前提是 none 定时回捞关：该 flag 缺省开，开着时 5 次后转 done+none
    等回捞（Step 4 语义）；本例经保存路径关掉。"""
    from backend.emby_server import enrich_worker as _ew
    from backend import models as _models
    from backend.integrations import store as _store
    _ew.save_enrich_tuning_config(db, {_ew.CFG_ENRICH_NONE_AUTO_RESCHEDULE: False})
    try:
        item = _make_item(db, item_type="movie", enrich_status="pending")
        _apply_incomplete(db, item, 4)  # 第 5 次
        assert item.enrich_status == "failed", item.enrich_status
        assert item.enrich_attempts == 5
    finally:
        db.query(_models.SystemConfig).filter(
            _models.SystemConfig.key == _ew.CFG_ENRICH_NONE_AUTO_RESCHEDULE
        ).delete(synchronize_session=False)
        db.commit()
        _store.invalidate(_ew.CFG_ENRICH_NONE_AUTO_RESCHEDULE)
        _ew._enrich_tuning_cache["at"] = 0.0
        _ew._enrich_tuning_cache["cfg"] = None


def test_incomplete_second_round_backoff_doubles(db):
    item = _make_item(db, item_type="movie", enrich_status="pending")
    _apply_incomplete(db, item, 1)
    delta = (item.enrich_next_retry_at - datetime.now()).total_seconds()
    assert 90 < delta <= 180, delta  # 120s


# ==================== 4. 手动锁定 ====================

def test_apply_skips_locked_item(db):
    """抢单后被锁定：apply 不再写入自动结果，直接 done"""
    item = _make_item(db, item_type="movie", enrich_status="enriching",
                      metadata_locked=True, name="管理员手改的片名")
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as tc:
        tc.configured = False
        enrich_worker._enrich_apply(db, item, {"ok": True, "tmdb_id": "999"})
    db.commit()
    db.refresh(item)
    assert item.tmdb_id is None, "锁定时不应写入自动刮削的 tmdb_id"
    assert item.name == "管理员手改的片名"
    assert item.enrich_status == "done"


# ==================== 5. 原子抢单 ====================

def test_claim_group_second_caller_wins_nothing(db):
    """同一批候选，第二个会话的原子认领赢走 0 行（不再重复处理）"""
    from datetime import datetime as _dt
    gid_items = [_make_item(db, item_type="movie", name=f"m{i}") for i in range(5)]
    s1 = SessionLocal()
    s2 = SessionLocal()
    try:
        now = _dt.now()
        q1 = (s1.query(em.MediaItem)
              .filter(em.MediaItem.enrich_status == "pending",
                      em.MediaItem.metadata_locked.isnot(True)))
        won1 = enrich_worker._claim_group_atomic(s1, q1, now)
        # s2 在 s1 提交前抢同一批
        q2 = (s2.query(em.MediaItem)
              .filter(em.MediaItem.enrich_status == "pending",
                      em.MediaItem.metadata_locked.isnot(True)))
        won2 = enrich_worker._claim_group_atomic(s2, q2, now)
        s1.commit()
        s2.rollback()
        assert len(won1) == 5, len(won1)
        assert len(won2) == 0, len(won2)
        # 赢到的行状态正确
        assert all(r.enrich_status == "enriching" for r in won1)
        assert all(r.enrich_claim_token for r in won1)
    finally:
        s1.close()
        s2.close()


# ==================== TMDB：年份二次确认 + 年份回退 ====================

def _fake_client(monkeypatch, script):
    """script: dict[(query, year)] -> list[hit]；记录调用"""
    from backend.emby_server import tmdb as tmdb_mod
    calls = []

    class FakeClient(tmdb_mod.TmdbClient):
        def __init__(self):
            self._stats = {"short_circuit": 0, "retry": 0, "net_fail": 0}

        def _search_raw(self, query, year, kind="movie", lang=None):
            calls.append((query, year))
            return script.get((query, year), script.get((None, year), []))

    return FakeClient(), calls


def test_search_year_fallback_when_filtered_empty():
    """带年份搜出空结果（TMDB 直接返回空）→ 去年份重搜，命中带年份的标题"""
    from backend.emby_server import tmdb as tmdb_mod
    ww1984 = {"title": "Wonder Woman 1984", "release_date": "2020-10-16",
              "id": 464052}
    script = {
        # 带年份搜：TMDB 直接返回空（不是"没命中"是"没返回"）
        (None, 1984): [],
        # 去年份：返回结果
        (None, None): [ww1984],
    }
    client, calls = _fake_client(None, script)
    hit = client.search("Wonder Woman", year=1984, kind="movie")
    assert hit is not None and hit["id"] == 464052, hit
    # 确认真的走过回退（有 year=None 的调用）
    assert any(y is None for _, y in calls), calls


def test_search_no_fallback_when_results_exist():
    """带年份搜有结果 → 不触发回退（省请求）"""
    dune2021 = {"title": "Dune", "release_date": "2021-09-15", "id": 438631}
    script = {(None, 2021): [dune2021]}
    client, calls = _fake_client(None, script)
    hit = client.search("Dune", year=2021, kind="movie")
    assert hit is not None and hit["id"] == 438631, hit
    assert not any(y is None for _, y in calls), calls


def test_hit_score_year_prefers_matching_year():
    """同名不同年：年份对上的胜出，不只看 TMDB 返回顺序"""
    from backend.emby_server.tmdb import _hit_score
    dune84 = {"title": "Dune", "release_date": "1984-12-14", "id": 841}
    dune21 = {"title": "Dune", "release_date": "2021-09-15", "id": 438631}
    sc84 = _hit_score("Dune", "Dune", dune84, True, year=2021)
    sc21 = _hit_score("Dune", "Dune", dune21, True, year=2021)
    assert sc84 is not None and sc21 is not None
    assert sc21 > sc84, (sc84, sc21)


def test_hit_score_year_from_title_digits_not_misused():
    """名字里的 4 位数字是标题一部分时（如 2049），不按年份否决"""
    from backend.emby_server.tmdb import _hit_score
    br2049 = {"title": "Blade Runner 2049", "release_date": "2017-10-04",
              "id": 335984}
    # 解析出的年份是 2017（不是 2049）：正确结果不被降权
    sc = _hit_score("Blade Runner 2049", "Blade Runner 2049", br2049,
                    True, year=2017)
    assert sc is not None and sc[0] == 2, sc
    # 年份对不上 → 降一级（但不是 None）
    sc2 = _hit_score("Blade Runner 2049", "Blade Runner 2049", br2049,
                     True, year=2019)
    assert sc2 is not None and sc2 < sc, (sc, sc2)


def test_search_candidates_include_year_suffixed():
    """已知年份时，候选里要有"清洗名 YYYY"（年份可能是标题一部分）"""
    from backend.emby_server.tmdb import _search_candidates
    cands = _search_candidates("Wonder Woman", year=1984)
    assert "Wonder Woman 1984" in [q for q, _ in cands], cands
    # 不传 year 时没有（保持旧行为）
    cands2 = _search_candidates("Wonder Woman")
    assert "Wonder Woman 1984" not in [q for q, _ in cands2]
