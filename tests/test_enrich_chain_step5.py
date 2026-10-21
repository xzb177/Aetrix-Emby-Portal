"""链路规划 Step 5：完成口径字段级。

feature flag：``enrich_field_completeness``（SystemConfig，默认 0=关）。
flag 开时：
1. 缺失字段记录：``_enrich_apply`` 写库时对标题/年份/海报/简介/类型/演员
   逐字段记有无（日志 + 进程内计数，**不加 migration**；只读缺失率走
   ``field_missing_stats()`` 的 SQL 口径）。
2. 低优先级补简介队列：核心字段齐（tmdb_id，或标题/年份/海报三者有其二）
   但缺次要字段（简介/类型/演员）→ pending + ``enrich_priority=-10``
   （低于默认 0；``_claim_batch`` 按 priority desc 认领，天然排在主队列之后，
   不另起认领通道，不饿死主队列）。
3. 防反复重刮（v2.49.0 教训）：低优先级条目为"补次要字段"最多多跑一轮——
   补完那一轮（认领时 priority=-10 的那次）无论拿到与否直接终态化，
   不再为简介/类型/演员反复进队列。
flag 关时口径 = 今天的"三者有其二"（逐行一致）。

测试覆盖：
1. flag 默认关；
2. flag 关：核心齐缺简介 → done（旧行为）；
3. flag 开：核心齐缺简介/类型/演员 → pending + priority=-10（低优先级）；
4. flag 开：六字段全齐 → done；
5. flag 开：补过一轮（priority=-10 入场）仍缺简介 → 直接 done，不反复；
6. flag 开：简介有但类型/演员缺 → 同样低优先级补一轮；
7. flag 开：核心不齐 → 仍走旧的资料不全退避（不变）；
8. _claim_batch：priority=-10 的组天然排在 priority=0 的组之后；
9. field_missing_stats：各字段缺失数/缺失率只读统计；
10. 样本对比：简介缺失率（flag 关 vs 开）。
"""
import os
import tempfile

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

from backend import database as _dbmod
from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

_dbmod.engine = _ce(os.environ["DATABASE_URL"])
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import uuid
from unittest import mock

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker as ew
from backend.integrations import store

init_db()

FLAG = ew.CFG_ENRICH_FIELD_COMPLETENESS


def _clear_tuning_cache():
    ew._enrich_tuning_cache["at"] = 0.0
    ew._enrich_tuning_cache["cfg"] = None


def _set_flag(value: str):
    # 2026-10-21 A 类摘面板：该 flag 已 env-only（DB 行不再生效），
    # 改经 env ENRICH_FIELD_COMPLETENESS 控制；fixture teardown 会清掉
    import os
    os.environ["ENRICH_FIELD_COMPLETENESS"] = value
    _clear_tuning_cache()


@pytest.fixture(autouse=True)
def _flag_off():
    # 每个测试前把 flag 经 env 复位为 0（保证本文件内默认关）；
    # teardown 删除 env（恢复真实世界缺省=开），防泄漏到其它测试文件。
    import os
    _set_flag("0")
    yield
    os.environ.pop("ENRICH_FIELD_COMPLETENESS", None)
    _clear_tuning_cache()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        s.query(em.MediaStream).delete()
        s.query(em.EmbyPerson).delete()
        s.query(em.MediaItem).delete()
        s.commit()
        yield s
    finally:
        s.close()


def _flag_on():
    _set_flag("1")


def _make_item(db, **kw):
    it = em.MediaItem(
        guid=uuid.uuid4().hex,
        library_id=kw.pop("library_id", 1),
        item_type=kw.pop("item_type", "series"),
        name=kw.pop("name", "测试"),
        file_path=kw.pop("file_path", "/tmp/x.mp4"),
        enrich_status=kw.pop("enrich_status", "pending"),
        **kw,
    )
    db.add(it)
    db.commit()
    return it


def _add_cast(db, item, name="演员甲"):
    p = em.EmbyPerson(item_id=item.id, name=name, sort_order=0)
    db.add(p)
    db.commit()
    return p


def _fetched_minimal():
    """最小 fetched：不触发任何抓取分支，只走写库判定。"""
    return {"ok": True, "poster": None, "fanart": None, "external_subs": [],
            "nfo_data": None, "tmdb_hit": None, "tmdb_details": None,
            "error": None}


def _apply(db, item, fetched=None):
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client:
        fake_client.configured = True
        ew._enrich_apply(db, item, fetched or _fetched_minimal())
    db.commit()
    db.refresh(item)


def test_flag_default_off(db):
    assert ew.enrich_tuning_value(FLAG, db) is False


def test_flag_off_old_behavior_core_complete_missing_overview_goes_done(db):
    """flag 关 = 今天口径：核心齐（tmdb_id）缺简介 → done"""
    it = _make_item(db, name="旧口径剧", tmdb_id="123",
                    production_year=2020, poster_path="/tmp/p.jpg",
                    overview=None, genres=None)
    _apply(db, it)
    assert it.enrich_status == "done", "flag 关时缺简介不拦路（v2.49.0 口径）"
    assert it.enrich_priority == 0


def test_flag_on_core_complete_missing_overview_goes_low_priority(db):
    """flag 开：核心齐但缺简介/类型/演员 → pending + priority=-10（低优先级）"""
    _flag_on()
    it = _make_item(db, name="缺简介剧", tmdb_id="123",
                    production_year=2020, poster_path="/tmp/p.jpg",
                    overview=None, genres=None)
    _apply(db, it)
    assert it.enrich_status == "pending", "缺简介不应直接 done，应进低优先级队列"
    assert it.enrich_priority == ew.ENRICH_PRIORITY_FIELD_REFILL == -10
    assert it.enrich_attempts == 0, "低优先级补一轮不计入失败次数"
    assert it.enrich_next_retry_at is None, "到点即可认领（排序天然靠后）"


def test_flag_on_all_fields_present_goes_done(db):
    """flag 开：六字段全齐 → done"""
    _flag_on()
    it = _make_item(db, name="全齐剧", tmdb_id="123",
                    production_year=2020, poster_path="/tmp/p.jpg",
                    overview="简介", genres="剧情")
    _add_cast(db, it)
    _apply(db, it)
    assert it.enrich_status == "done"
    assert it.enrich_priority == 0


def test_flag_on_refill_round_terminalizes_no_repeat(db):
    """★ 防反复重刮（v2.49.0 红线）：补过一轮仍缺简介 → 直接 done，不再反复。

    模拟两轮：
    - 第 1 轮：pending(priority=0) → 判缺简介 → pending + priority=-10；
    - 第 2 轮（认领时 priority=-10 的就是补次要字段那一轮）：仍缺简介 →
      直接终态化 done，priority 清零。此后它是 done，不会再进队列。
    """
    _flag_on()
    it = _make_item(db, name="补简介剧", tmdb_id="123",
                    production_year=2020, poster_path="/tmp/p.jpg",
                    overview=None, genres=None)
    # 第 1 轮：进低优先级队列
    _apply(db, it)
    assert it.enrich_status == "pending"
    assert it.enrich_priority == -10
    # 第 2 轮：补次要字段那一轮（priority=-10 入场），简介还是没拿到
    _apply(db, it)
    assert it.enrich_status == "done", "补过一轮必须终态化，不许反复"
    assert it.enrich_priority == 0, "终态化后优先级清零"
    # 进程内计数：判定 2 次、入队 1 次、终态化 1 次
    assert ew._field_completeness_counters["refill_enqueued"] >= 1
    assert ew._field_completeness_counters["refill_terminalized"] >= 1


def test_flag_on_refill_round_got_overview_goes_done(db):
    """补的那一轮拿到了简介 → 正常 done（不再缺次要字段）"""
    _flag_on()
    it = _make_item(db, name="补到简介剧", tmdb_id="123",
                    production_year=2020, poster_path="/tmp/p.jpg",
                    overview=None, genres=None,
                    enrich_priority=-10)  # 模拟已进过低优先级队列
    # 这一轮抓到了简介/类型/演员
    it.overview = "本轮拿到的简介"
    it.genres = "剧情"
    _add_cast(db, it)
    _apply(db, it)
    assert it.enrich_status == "done"
    assert it.enrich_priority == 0


def test_flag_on_overview_present_but_genres_cast_missing(db):
    """flag 开：简介有了但类型/演员缺 → 同样低优先级补一轮"""
    _flag_on()
    it = _make_item(db, name="缺类型演员剧", tmdb_id="123",
                    production_year=2020, poster_path="/tmp/p.jpg",
                    overview="有简介", genres=None)
    _apply(db, it)
    assert it.enrich_status == "pending"
    assert it.enrich_priority == -10
    # 补完那一轮仍缺 → 终态化，不反复
    _apply(db, it)
    assert it.enrich_status == "done"
    assert it.enrich_priority == 0


def test_flag_on_core_incomplete_still_backoff(db):
    """flag 开：核心不齐（只有标题）→ 仍走旧的资料不全退避，不变"""
    _flag_on()
    it = _make_item(db, name="只有标题", tmdb_id=None,
                    production_year=None, poster_path=None,
                    overview=None, enrich_attempts=0)
    # tmdb_hit 有命中但没落下 tmdb_id → _searched_no_hit=False → 走 _incomplete
    fetched = dict(_fetched_minimal(), tmdb_hit={"id": 999, "title": "x"})
    _apply(db, it, fetched)
    assert it.enrich_status == "pending"
    assert it.enrich_attempts == 1, "资料不全仍按失败计次 + 指数退避"
    assert it.enrich_next_retry_at is not None


def test_claim_batch_low_priority_sorts_after_main(db):
    """低优先级队列不饿死主队列：priority=-10 的组天然排在 0 的组之后认领"""
    main = _make_item(db, name="主队列剧", enrich_status="pending",
                      enrich_priority=0)
    low = _make_item(db, name="低优先级剧", enrich_status="pending",
                     enrich_priority=-10)
    claimed = ew._claim_batch(db, 1)
    assert len(claimed) == 1
    assert claimed[0].id == main.id, "主队列（priority=0）必须先被认领"
    # 主队列空了之后，低优先级才能被认领
    db.refresh(main)
    main.enrich_status = "done"
    db.commit()
    claimed2 = ew._claim_batch(db, 1)
    assert len(claimed2) == 1
    assert claimed2[0].id == low.id, "主队列空闲时低优先级才被消费"


def test_field_missing_stats(db):
    """只读缺失率：done+实际刮到数据的条目口径，各字段缺失数/缺失率"""
    full = _make_item(db, name="全齐", tmdb_id="1", production_year=2020,
                      poster_path="/tmp/p.jpg", overview="简介", genres="剧情",
                      enrich_status="done", metadata_source="tmdb")
    _add_cast(db, full)
    no_overview = _make_item(db, name="缺简介", tmdb_id="2",
                             production_year=2021, poster_path="/tmp/p.jpg",
                             overview=None, genres="剧情",
                             enrich_status="done", metadata_source="tmdb")
    _add_cast(db, no_overview)
    no_year_genres = _make_item(db, name="缺年份类型", tmdb_id="3",
                                production_year=None, poster_path="/tmp/p.jpg",
                                overview="简介", genres=None,
                                enrich_status="done", metadata_source="tmdb")
    # 口径外：搜过没有 / 还没刮完 → 不计入
    _make_item(db, name="搜过没有", enrich_status="done",
               metadata_source="none")
    _make_item(db, name="待刮", enrich_status="pending",
               metadata_source="tmdb")
    stats = ew.field_missing_stats(db)
    assert stats["total"] == 3
    assert stats["missing"]["overview"] == 1
    assert stats["missing"]["year"] == 1
    assert stats["missing"]["genres"] == 1
    assert stats["missing"]["title"] == 0
    assert stats["missing"]["poster"] == 0
    assert stats["missing"]["cast"] == 1, "第三条无演员行"
    assert stats["rates"]["overview"] == round(1 / 3, 4)
    assert stats["field_completeness_enabled"] is False


def test_field_missing_stats_excludes_season_episode(db):
    """r33 口径修正：field_missing_stats 只统计 series/movie。

    季(season)/单集(episode)即使已刮完(done + 实际刮到数据)，也不计入口径——
    它们走单集刮削/父级继承另行处理，refill 不覆盖，算进口径会让后台灰度
    判断的缺失率永远降不下来。"""
    _make_item(db, name="剧全齐", tmdb_id="10", production_year=2020,
               poster_path="/tmp/p.jpg", overview="简介", genres="剧情",
               enrich_status="done", metadata_source="tmdb")
    # 口径外：季/集已刮完也不算
    _make_item(db, name="第 1 季", item_type="season", production_year=2020,
               poster_path="/tmp/p.jpg", overview="简介", genres="剧情",
               enrich_status="done", metadata_source="tmdb")
    _make_item(db, name="S01E01", item_type="episode", overview=None,
               enrich_status="done", metadata_source="tmdb")
    stats = ew.field_missing_stats(db)
    assert stats["total"] == 1
    assert stats["missing"]["overview"] == 0, "单集的简介缺失不应计入"
    assert stats["rates"]["overview"] == 0.0


def test_sample_overview_missing_rate_flag_off_vs_on(db):
    """样本对比（handoff 数据）：20 条核心齐缺简介的样本。

    flag 关：全部直接 done → done 且缺简介率 100%；
    flag 开：全部进低优先级 pending（0 条直接 done），补一轮后终态化。
    """
    samples = []
    for i in range(20):
        samples.append(_make_item(
            db, name=f"样本{i}", tmdb_id=str(1000 + i),
            production_year=2020, poster_path="/tmp/p.jpg",
            overview=None, genres=None))
    # flag 关
    for it in samples:
        _apply(db, it)
    done_missing = sum(1 for it in samples
                       if it.enrich_status == "done"
                       and not (it.overview or "").strip())
    assert done_missing == 20, f"flag 关：20 条应全部直接 done 且缺简介，实际 {done_missing}"
    off_rate = done_missing / len(samples)

    # 重置样本，flag 开
    for it in samples:
        it.enrich_status = "pending"
        it.enrich_priority = 0
        it.enrich_attempts = 0
        it.enrich_next_retry_at = None
    db.commit()
    _flag_on()
    for it in samples:
        _apply(db, it)
    direct_done = sum(1 for it in samples if it.enrich_status == "done")
    lowq = sum(1 for it in samples
               if it.enrich_status == "pending" and it.enrich_priority == -10)
    assert direct_done == 0, f"flag 开：不应有直接 done，实际 {direct_done}"
    assert lowq == 20, f"flag 开：20 条应全进低优先级，实际 {lowq}"
    # 补一轮后终态化（简介还是没拿到 → done，不反复）
    for it in samples:
        _apply(db, it)
    terminal = sum(1 for it in samples if it.enrich_status == "done")
    assert terminal == 20
    print(f"\n[样本对比] 简介缺失率：flag 关直接 done 且缺简介 {off_rate:.0%}；"
          f"flag 开：0 条直接 done，20 条进低优先级补一轮后终态化（不反复）")


def test_flag_on_incomplete_round_never_enters_refill(db):
    """P1-1 回归：本轮抓取失败（_incomplete）时不开补队列——attempts 单调递增，
    不在 0/1 振荡，超限转 failed，不烧 TMDB 配额。"""
    _flag_on()
    it = _make_item(db, name="常失败剧", tmdb_id=None,
                    production_year=2020, poster_path="/tmp/p.jpg",
                    overview=None, genres=None)
    # tmdb 有命中但没落下 tmdb_id → _searched_no_hit=False, _incomplete=True
    # （核心靠标题/年份/海报三者有其二凑齐，正是旧代码 ping-pong 的前提）
    fetched = dict(_fetched_minimal(), tmdb_hit={"id": 999, "title": "x"})
    seen = []
    for _ in range(4):
        _apply(db, it, fetched)
        seen.append(it.enrich_attempts)
        assert it.enrich_priority == 0, "失败轮次不许进 -10 补队列"
        assert it.enrich_status == "pending"
    assert seen == [1, 2, 3, 4], f"attempts 必须单调递增，实际 {seen}"
