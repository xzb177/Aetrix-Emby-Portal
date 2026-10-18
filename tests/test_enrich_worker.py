"""补全 worker（v2.40.0）集成测试：原子抢单 / 重试退避 / 防洪峰 / 字幕落库 / 进度。

- 全部在隔离临时 SQLite 里跑，不连生产库；
- 不测真实 TMDB 网络（mock _enrich_fetch）。
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
_dbmod.engine = _ce(os.environ["DATABASE_URL"])
# 必须走 configure_session_local：直接赋值会把 SessionLocal 代理顶掉，
# 之后各模块 import 到的是被冻结的真 factory，又会查旧库。
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import uuid
from datetime import datetime, timedelta
from unittest import mock

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker

init_db()


@pytest.fixture()
def db():
    s = SessionLocal()
    try:
        # 每个测试前清空（隔离，避免测试间污染）
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


def test_suppress_flood_marks_scraped_old_data_done(db):
    """防洪峰：有 last_scraped_at / tmdb_id 的老数据（无指纹）直接标 done"""
    _make_item(db, file_fingerprint=None, last_scraped_at=datetime.now())
    _make_item(db, file_fingerprint=None, tmdb_id="123")
    _make_item(db, file_fingerprint=None)  # 真需要补的，保留 pending
    _make_item(db, file_fingerprint="abc", enrich_status="pending")  # L1 新数据不动
    n = enrich_worker._suppress_flood(db)
    assert n == 2
    statuses = sorted(
        r[0] for r in db.query(em.MediaItem.enrich_status).all())
    assert statuses == ["done", "done", "pending", "pending"]


def test_claim_batch_skips_not_yet_due(db):
    """未到重试时间的 pending 不被抢走"""
    future = datetime.now() + timedelta(hours=1)
    _make_item(db, enrich_next_retry_at=future)
    _make_item(db, enrich_next_retry_at=None)
    claimed = enrich_worker._claim_batch(db, 10)
    assert len(claimed) == 1
    assert claimed[0].enrich_next_retry_at is None
    # 抢到的已标 enriching
    assert claimed[0].enrich_status == "enriching"


def test_mark_failed_backoff_and_failed(db):
    """失败指数退避：1次后 pending+60s，5次后转 failed"""
    it = _make_item(db, enrich_attempts=0)
    enrich_worker._mark_failed(db, it.id, 0, "boom")
    db.refresh(it)
    assert it.enrich_status == "pending"
    assert it.enrich_attempts == 1
    assert it.enrich_next_retry_at is not None
    delta = (it.enrich_next_retry_at - datetime.now()).total_seconds()
    assert 30 < delta <= 90  # 基数 60s 退避

    enrich_worker._mark_failed(db, it.id, 4, "boom")
    db.refresh(it)
    assert it.enrich_status == "failed"
    assert it.enrich_attempts == 5
    assert it.enrich_next_retry_at is None


def test_recover_crashed(db):
    """崩溃残留的 enriching 打回 pending"""
    _make_item(db, enrich_status="enriching")
    _make_item(db, enrich_status="enriching")
    n = enrich_worker._recover_crashed(db)
    assert n == 2
    assert db.query(em.MediaItem).filter(
        em.MediaItem.enrich_status == "enriching").count() == 0


def test_enrich_apply_writes_subtitles(db):
    """字幕落库：外挂字幕写入 MediaStream，旧字幕先清

    条目预置 tmdb_id + overview：未刮干净的条目现在会退回 pending（可重试），
    这条只验字幕，故给它完整元数据以走 done 分支。
    """
    it = _make_item(db, item_type="movie", tmdb_id="999", overview="已有简介")
    # 先放一条旧字幕
    db.add(em.MediaStream(item_id=it.id, stream_index=0,
                          stream_type="Subtitle", is_external=True,
                          external_path="/old.srt", language="eng"))
    # 再放一条内嵌字幕（不能被误删）
    db.add(em.MediaStream(item_id=it.id, stream_index=1,
                          stream_type="Subtitle", is_external=False,
                          language="eng"))
    db.commit()
    fetched = {
        "poster": None, "fanart": None,
        "external_subs": [("chi", "/new_chi.srt"), ("eng", "/new_eng.srt")],
        "nfo_data": None, "tmdb_hit": None, "tmdb_details": None,
        "ok": True,
    }
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as _m:
        enrich_worker._enrich_apply(db, it, fetched)
    db.commit()
    subs = db.query(em.MediaStream).filter(
        em.MediaStream.item_id == it.id,
        em.MediaStream.stream_type == "Subtitle").order_by(
            em.MediaStream.stream_index).all()
    ext_paths = [s.external_path for s in subs if s.is_external]
    assert ext_paths == ["/new_chi.srt", "/new_eng.srt"]
    # 内嵌字幕保留
    assert any(not s.is_external for s in subs)
    assert it.enrich_status == "done"


def test_get_progress(db):
    """进度接口：各状态计数 + 重试中 + probe"""
    _make_item(db, enrich_status="pending")
    _make_item(db, enrich_status="pending",
               enrich_next_retry_at=datetime.now() + timedelta(minutes=5))
    _make_item(db, enrich_status="failed")
    _make_item(db, enrich_status="done")
    p = enrich_worker.get_progress()
    assert p["enrich"]["pending"] == 2
    assert p["enrich"]["retrying"] == 1
    assert p["enrich"]["failed"] == 1
    assert p["enrich"]["done"] == 1
    assert p["workers"] == enrich_worker.ENRICH_WORKERS
    # v2.42.9 第 4 批：熔断快照 + 租约配置必须出现在进度里（管理端靠它定位坏挂载）
    assert "claim_lease_sec" in p
    assert p["claim_lease_sec"] == enrich_worker.ENRICH_CLAIM_LEASE_SEC
    assert "mount_breakers" not in p, "熔断器删了，进度里不该再有这个字段"


def test_get_progress_counts_visible_only(db):
    """进度接口只数可见行：已合并副本与软删行的 pending 不得计入。

    回归 2026-10-19 生产事故：get_progress 按全表口径报出待处理 29,798，
    实际可见（deleted_at 空且 merged_into_id 空）pending 只有 1。
    """
    # 可见行：2 条 pending（含 1 条带重试时间）、1 条 done
    _make_item(db, enrich_status="pending")
    _make_item(db, enrich_status="pending",
               enrich_next_retry_at=datetime.now() + timedelta(minutes=5))
    _make_item(db, enrich_status="done")
    # 已合并副本行：merged_into_id 非空，worker 按设计不处理
    _make_item(db, enrich_status="pending", merged_into_id=999)
    _make_item(db, enrich_status="pending",
               merged_into_id=999,
               enrich_next_retry_at=datetime.now() + timedelta(minutes=5))
    # 软删行：deleted_at 非空，等待清理
    _make_item(db, enrich_status="pending", deleted_at=datetime.now())
    p = enrich_worker.get_progress()
    assert p["enrich"]["pending"] == 2, "只数可见 pending（2），不得数进副本/软删行"
    assert p["enrich"]["retrying"] == 1, "retrying 同样只数可见行"
    assert p["enrich"]["done"] == 1


# ==================== claim 租约 + janitor（v2.42.9）====================


def test_claim_batch_sets_claimed_at(db):
    """抢单时写 claim 租约（janitor 判定僵尸行的依据）"""
    it = _make_item(db)
    claimed = enrich_worker._claim_batch(db, 10)
    assert len(claimed) == 1
    assert claimed[0].id == it.id
    assert claimed[0].enrich_status == "enriching"
    assert claimed[0].enrich_claimed_at is not None
    # 租约时刻应该就是刚刚
    delta = (datetime.now() - claimed[0].enrich_claimed_at).total_seconds()
    assert -5 < delta < 30


def test_enrich_apply_clears_claimed_at(db):
    """补全完成释放租约：enriching 停留期间之外不占着 claim 时间戳"""
    it = _make_item(db, item_type="movie", tmdb_id="999", overview="已有简介",
                    enrich_status="enriching", enrich_claimed_at=datetime.now())
    fetched = {"poster": None, "fanart": None, "external_subs": [],
               "nfo_data": None, "tmdb_hit": None, "tmdb_details": None,
               "ok": True}
    with mock.patch("backend.emby_server.tmdb.tmdb_client"):
        enrich_worker._enrich_apply(db, it, fetched)
    db.commit()
    assert it.enrich_claimed_at is None
    assert it.enrich_status == "done"


def test_mark_failed_clears_claimed_at(db):
    """失败（含转 failed）也释放租约，避免 janitor 重复回收同一行"""
    it = _make_item(db, enrich_status="enriching",
                    enrich_claimed_at=datetime.now())
    enrich_worker._mark_failed(db, it.id, 0, "boom")
    db.refresh(it)
    assert it.enrich_claimed_at is None
    assert it.enrich_status == "pending"


def test_recover_crashed_clears_claimed_at(db):
    """启动恢复：enriching → pending 且清空租约"""
    _make_item(db, enrich_status="enriching", enrich_claimed_at=datetime.now())
    n = enrich_worker._recover_crashed(db)
    assert n == 1
    row = db.query(em.MediaItem).first()
    assert row.enrich_status == "pending"
    assert row.enrich_claimed_at is None


def test_reclaim_stale_reclaims_expired_leases(db):
    """janitor：租约过期的 enriching 打回 pending；新鲜租约与 pending 不动。

    - 行 A：20 分钟前抢的 → 回收（清租约、状态 pending，attempts/next_retry 不动）；
    - 行 B：刚抢的 → 保留 enriching；
    - 行 C：老库升级上来的历史行（claimed_at=NULL、date_modified 很老）→ 回收；
    - 行 D：老格式但 date_modified 很新 → 保留（不误伤老代码正在处理的行）；
    - 行 E：pending → 不关 janitor 的事。
    """
    now = datetime.now()
    a = _make_item(db, enrich_status="enriching",
                   enrich_claimed_at=now - timedelta(seconds=enrich_worker.ENRICH_CLAIM_LEASE_SEC + 60),
                   enrich_attempts=1)
    b = _make_item(db, enrich_status="enriching", enrich_claimed_at=now)
    c = _make_item(db, enrich_status="enriching", enrich_claimed_at=None,
                   date_modified=now - timedelta(hours=2))
    d = _make_item(db, enrich_status="enriching", enrich_claimed_at=None,
                   date_modified=now)
    e = _make_item(db, enrich_status="pending")
    n = enrich_worker._reclaim_stale(db)
    assert n == 2
    db.refresh(a); db.refresh(b); db.refresh(c); db.refresh(d); db.refresh(e)
    assert a.enrich_status == "pending" and a.enrich_claimed_at is None
    assert a.enrich_attempts == 1          # 回收不是失败：attempts 不涨
    assert b.enrich_status == "enriching" and b.enrich_claimed_at is not None
    assert c.enrich_status == "pending" and c.enrich_claimed_at is None
    assert d.enrich_status == "enriching"
    assert e.enrich_status == "pending"


def test_reclaim_stale_custom_lease(db):
    """janitor 支持自定义租约（测试 / 运维调参用）"""
    now = datetime.now()
    _make_item(db, enrich_status="enriching",
               enrich_claimed_at=now - timedelta(seconds=120))
    assert enrich_worker._reclaim_stale(db, lease_sec=60) == 1
    assert db.query(em.MediaItem).filter(
        em.MediaItem.enrich_status == "enriching").count() == 0


# ==================== 存储不可用：打回 pending 而非 failed ====================


@pytest.fixture()
def mount_lib():
    from backend.emby_server import mounts as mnt
    return mnt


def test_process_item_mount_error_requeues_not_failed(db, mount_lib):
    """IO 中抛 MountError（存储读不动）：打回 pending + 长退避，不走 _mark_failed

    这是删掉熔断器之后**留下来**的那部分：熔断器自己没了，但「网盘挂了别把队列烧成
    failed」这件事得留着——否则一个挂不上的 remote 会让整库条目 5 次尝试后全部
    终结，补全队列再也回不来。
    """
    mnt = mount_lib
    it = _make_item(db, file_path="mount://7/Movies/b.mkv",
                    enrich_status="enriching", enrich_claimed_at=datetime.now())

    def dead_mount(*_a, **_k):
        raise mnt.MountError("网盘连接超时（测试用）")

    with mock.patch.object(enrich_worker, "_enrich_fetch", dead_mount):
        outcome = enrich_worker._process_item(db, it)
    assert outcome == "mount_unavailable"
    db.refresh(it)
    assert it.enrich_status == "pending"
    assert it.enrich_attempts == 0
    assert it.enrich_claimed_at is None
    assert it.enrich_next_retry_at is not None
    delta = (it.enrich_next_retry_at - datetime.now()).total_seconds()
    assert mnt.MOUNT_UNAVAILABLE_RETRY_SEC - 120 < delta <= mnt.MOUNT_UNAVAILABLE_RETRY_SEC


def test_mount_breaker_module_is_gone(mount_lib):
    """熔断器整块删除：这些名字不能再存在（有人 import 就会当场炸）"""
    for gone in ("MountBreakerOpen", "breaker_reset", "mount_breaker_open",
                 "mount_breaker_stats", "_BREAKERS", "MOUNT_BREAKER_THRESHOLD",
                 "MOUNT_BREAKER_COOLDOWN_SEC", "MOUNT_BREAKER_RETRY_SEC"):
        assert not hasattr(mount_lib, gone), f"{gone} 应已随熔断器一起删除"


def test_process_item_non_mount_error_still_fails_normally(db, mount_lib):
    """非挂载异常（如 TMDB 之外的真实 bug）仍走原有 attempts/退避路径"""
    it = _make_item(db, file_path="/tmp/local.mp4")

    def broken(*_a, **_k):
        raise RuntimeError("非挂载错误（测试用）")

    with mock.patch.object(enrich_worker, "_enrich_fetch", broken):
        outcome = enrich_worker._process_item(db, it)
    assert outcome == "retry"
    db.refresh(it)
    assert it.enrich_attempts == 1
    assert it.enrich_status == "pending"


def test_mount_id_of():
    assert enrich_worker._mount_id_of("mount://12/Movies/a.mkv") == 12
    assert enrich_worker._mount_id_of("mount://12") == 12
    assert enrich_worker._mount_id_of("/media/movies") is None
    assert enrich_worker._mount_id_of(None) is None
    assert enrich_worker._mount_id_of("mount://abc/x") is None


# ==================== 第 5 批：成组抢单 + ctx 复用 + 纯继承（v2.42.9）====================


def test_claim_batch_groups_series_parent_first(db):
    """成组抢单（处方 2）：同一部剧整组抢走，父级排在最前；组按最近入库倒序"""
    now = datetime.now()
    series = _make_item(db, item_type="series", name="组抢剧",
                        enrich_status="pending",
                        date_added=now - timedelta(hours=1))
    eps = [
        _make_item(db, item_type="episode", name=f"第 1 季 第 {n} 集",
                   file_path=f"mount://9/剧/Season 1/E{n:02d}.mkv",
                   series_id=series.id, parent_id=series.id,
                   season_number=1, episode_number=n,
                   enrich_status="pending",
                   date_added=now - timedelta(minutes=60 - n))
        for n in (1, 2, 3)
    ]
    movie = _make_item(db, item_type="movie", name="新入库的电影",
                       enrich_status="pending",
                       date_added=now)  # 最新 → 组排最前
    claimed = enrich_worker._claim_batch(db, 10)
    ids = [c.id for c in claimed]
    # 组之间：最新的电影组在前；组内：父级（剧）先于集；整组连在一起
    assert ids[0] == movie.id
    assert ids[1] == series.id
    assert ids[2:] == [e.id for e in eps]
    assert len(ids) == 5
    # 抢到的都置 enriching + 租约
    assert all(c.enrich_status == "enriching" and c.enrich_claimed_at for c in claimed)


def test_claim_batch_keeps_group_whole_across_batches(db):
    """批量截断时组剩余部分仍在队里，下一批继续抢（同一部剧不被打散）"""
    now = datetime.now()
    series = _make_item(db, item_type="series", name="长剧",
                        enrich_status="pending", date_added=now)
    for n in (1, 2, 3, 4):
        _make_item(db, item_type="episode", name=f"E{n}",
                   file_path=f"mount://9/长剧/S1/E{n}.mkv",
                   series_id=series.id, season_number=1, episode_number=n,
                   enrich_status="pending",
                   date_added=now - timedelta(minutes=n))
    first = enrich_worker._claim_batch(db, 2)
    assert len(first) == 2
    assert first[0].id == series.id          # 父级先抢
    rest = enrich_worker._claim_batch(db, 10)
    # 剩余三集（同组）被下一批抢走
    assert all(c.series_id == series.id for c in rest)
    assert len(rest) == 3


def test_claim_batch_skips_locked_out_and_not_due_groups(db):
    """未到重试时间的组不进候选；无组可选返回空列表"""
    now = datetime.now()
    _make_item(db, enrich_next_retry_at=now + timedelta(hours=1))
    assert enrich_worker._claim_batch(db, 10) == []


# ==================== 第 6 批：调度公平性 + 终态化 + 别名（v2.42.9）====================


def test_claim_batch_repair_jumps_the_queue(db):
    """处方 4：repair 行（priority=100）排在默认 0 的组前面，父级先于子集"""
    now = datetime.now()
    normal_movie = _make_item(db, name="普通新电影", enrich_status="pending",
                              date_added=now)  # 最新
    repair_series = _make_item(db, item_type="series", name="修复中的剧",
                               enrich_status="pending",
                               enrich_priority=100,
                               date_added=now - timedelta(hours=2))
    ep = _make_item(db, item_type="episode", name="第 1 集",
                    file_path="mount://9/修复中的剧/S1/E01.mkv",
                    series_id=repair_series.id, parent_id=repair_series.id,
                    enrich_status="pending",
                    date_added=now - timedelta(hours=2))
    claimed = enrich_worker._claim_batch(db, 10)
    ids = [c.id for c in claimed]
    # repair 组最高优先：整组在前，父级先于集
    assert ids[0] == repair_series.id
    assert ids[1] == ep.id
    assert ids[2] == normal_movie.id


def test_claim_batch_library_round_robin(db):
    """处方 4：跨库轮转——上一轮抢了库 A，这一轮库 B 的组排前面"""
    # 直接重置游标：_library_turn(None) 只是无参读（不会写），清不掉
    # 前序测试（如 repair 置顶测试抢了库 1）留下的游标，全量跑时会串扰。
    enrich_worker._library_turn_state["lib_id"] = None
    now = datetime.now()
    lib_a = em.Library(guid=uuid.uuid4().hex, name="库A", collection_type="movies", paths="")
    lib_b = em.Library(guid=uuid.uuid4().hex, name="库B", collection_type="movies", paths="")
    db.add_all([lib_a, lib_b]); db.flush()
    a_item = _make_item(db, name="A库新条目", enrich_status="pending",
                        library_id=lib_a.id, date_added=now)
    b_item = _make_item(db, name="B库旧条目", enrich_status="pending",
                        library_id=lib_b.id,
                        date_added=now - timedelta(hours=1))
    first = enrich_worker._claim_batch(db, 10)
    assert [c.id for c in first] == [a_item.id, b_item.id]  # 无游标：按最近入库
    db.rollback()  # 释放 enriching 状态（测试不真处理）
    db.query(em.MediaItem).filter(em.MediaItem.id.in_([a_item.id, b_item.id])).update(
        {"enrich_status": "pending", "enrich_claimed_at": None},
        synchronize_session=False)
    db.commit()
    second = enrich_worker._claim_batch(db, 10)
    # 游标在库 A：库 B 的组被优先（不再被新条目饿死）
    assert [c.id for c in second] == [b_item.id, a_item.id]


def test_repair_claimed_even_before_retry_due(db):
    """处方 4：repair 行未到 next_retry_at 也被立即抢走（用户等着的）"""
    future = datetime.now() + timedelta(hours=1)
    _make_item(db, name="等退避的", enrich_next_retry_at=future)
    repair = _make_item(db, name="等修复的", enrich_next_retry_at=future,
                        enrich_priority=100, repair_requested_at=datetime.now())
    claimed = enrich_worker._claim_batch(db, 10)
    assert [c.id for c in claimed] == [repair.id]


def test_retry_unmatched_requeues_terminal_none_items(db):
    """处方 5：「重试未匹配项」只捞 done+none+无 tmdb_id 的 series/movie，置 priority=50"""
    _make_item(db, item_type="series", name="终态无望剧",
               enrich_status="done", metadata_source="none",
               file_fingerprint="fp1")
    _make_item(db, item_type="movie", name="终态无望电影",
               enrich_status="done", metadata_source="none",
               file_fingerprint="fp2")
    _make_item(db, item_type="series", name="已命中的剧",
               enrich_status="done", metadata_source="tmdb", tmdb_id="42",
               file_fingerprint="fp3")  # 不动
    _make_item(db, item_type="episode", name="纯继承集（不该动）",
               enrich_status="done", metadata_source="inherit",
               file_path="mount://9/x/E01.mkv",
               file_fingerprint="fp4")
    n = enrich_worker.retry_unmatched(db)
    assert n == 2
    rows = {r.name: r for r in db.query(em.MediaItem).all()}
    for name in ("终态无望剧", "终态无望电影"):
        assert rows[name].enrich_status == "pending"
        assert rows[name].enrich_priority == 50
    assert rows["已命中的剧"].enrich_status == "done"
    assert rows["纯继承集（不该动）"].enrich_status == "done"


def test_retry_unmatched_purges_search_cache(db, tmp_path, monkeypatch):
    """第 7 批：重试未匹配必须清掉阴性搜索缓存，否则补搜命中旧缓存一个请求都不发"""
    from backend.emby_server import tmdb, tmdb_cache

    monkeypatch.setenv("EMBY_TMDB_CACHE_DIR", str(tmp_path / "tmdb-cache"))
    monkeypatch.setattr(tmdb_cache, "_disabled", False)
    # 语言是缓存维度：worker 按当前首选语言清，这里固定住
    monkeypatch.setattr(tmdb, "preferred_language", lambda db=None: "zh-CN")
    _make_item(db, item_type="series", name="终态无望剧",
               enrich_status="done", metadata_source="none",
               file_fingerprint="fp-cache")
    # 预置一份阴性缓存（与 worker 补搜时同键：归一化名字 + year 0 + 语言维度）
    norm = tmdb._norm_text("终态无望剧")
    tmdb_cache.save_search("tv", norm, 0, [], "zh-CN")
    path = tmdb_cache._key_path("tv", norm, 0, "zh-CN")
    assert os.path.exists(path)

    n = enrich_worker.retry_unmatched(db)
    assert n == 1
    assert not os.path.exists(path), "重试入队必须同时清掉旧缓存"


def test_apply_terminal_done_for_searched_without_hit(db):
    """处方 5：TMDB 搜过且无高置信命中 → 终态 done + none，不再退回 pending"""
    it = _make_item(db, item_type="series", name="注定搜不到的剧")
    fetched = {"ok": True, "poster": None, "fanart": None, "external_subs": [],
               "nfo_data": None, "tmdb_hit": None, "tmdb_details": None,
               "error": None}
    with mock.patch.object(enrich_worker, "_alias_tmdb_id", return_value=None), \
            mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client:
        fake_client.configured = True
        enrich_worker._enrich_apply(db, it, fetched)
    db.commit()  # _enrich_apply 只改内存对象；commit 后再查才反映真实落库结果
    db.refresh(it)
    assert it.enrich_status == "done"
    assert it.metadata_source == "none"
    assert it.enrich_next_retry_at is None


def test_apply_stays_pending_when_infra_failure_or_repair(db):
    """处方 5 不误伤：网络失败（ok=False）与 repair 请求仍走重试路"""
    it = _make_item(db, item_type="series", name="网络失败的剧")
    fetched = {"ok": False, "error": "网络超时", "nfo_data": None,
               "tmdb_hit": None, "tmdb_details": None}
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client:
        fake_client.configured = True
        enrich_worker._enrich_apply(db, it, fetched)
    db.commit()
    db.refresh(it)
    assert it.enrich_status == "pending", "基础设施失败必须可重试"

    it2 = _make_item(db, item_type="series", name="修复请求的剧",
                     repair_requested_at=datetime.now())
    fetched2 = {"ok": True, "error": None, "nfo_data": None,
                "tmdb_hit": None, "tmdb_details": None}
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client:
        fake_client.configured = True
        enrich_worker._enrich_apply(db, it2, fetched2)
    db.commit()
    db.refresh(it2)
    assert it2.enrich_status == "pending", "repair 必须重试直到成功或超限"


def test_apply_priority_reset_after_processing(db):
    """处方 4：优先级消费完归零（repair=100 也不残留）"""
    it = _make_item(db, item_type="series", name="优先级消费",
                    enrich_priority=100, repair_requested_at=datetime.now())
    fetched = {"ok": True, "nfo_data": None, "tmdb_hit": None, "tmdb_details": None}
    with mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client:
        fake_client.configured = False
        enrich_worker._enrich_apply(db, it, fetched)
    db.commit()
    db.refresh(it)
    assert it.enrich_priority == 0


def test_alias_index_hit_and_ttl_rebuild(db):
    """处方 12：别名索引命中返回 tmdb_id；名字大小写/标点差异不影响命中"""
    _make_item(db, item_type="series", name="漫长的季节",
               tmdb_id="119", aliases="The Long Season,漫長的季節")
    enrich_worker._alias_index["rows"] = None  # 强制重建
    got = enrich_worker._alias_tmdb_id("The Long Season")
    assert got == "119"
    got2 = enrich_worker._alias_tmdb_id("漫长的季节！")  # 标点会被归一化掉
    assert got2 == "119"
    assert enrich_worker._alias_tmdb_id("完全无关的名字") is None


def test_fetch_skips_alt_fallback_when_tmdb_configured(db, monkeypatch):
    """父级快速失败：TMDB 已配置且搜过无命中 → 跳过豆瓣/Bangumi（零网络等待）"""
    calls = {"douban": 0, "bgm": 0}
    class _Boom:
        def __getattr__(self, item):
            raise AssertionError("兜底源不该被调用")
    import backend.emby_server.altmeta as altmeta
    def _boom_search(*a, **k):
        calls["douban"] += 1
        raise AssertionError("TMDB 已配置时豆瓣兜底必须被跳过")
    def _boom_bgm(*a, **k):
        calls["bgm"] += 1
        raise AssertionError("TMDB 已配置时 Bangumi 兜底必须被跳过")
    monkeypatch.setattr(altmeta, "search", _boom_search)
    monkeypatch.setattr(altmeta, "search_bangumi", _boom_bgm)
    monkeypatch.setattr(altmeta, "enabled", lambda db: True)

    it = _make_item(db, item_type="series", name="快失败父级", file_path="")
    from backend.emby_server import scanner as _sc
    with mock.patch.object(enrich_worker, "_alias_tmdb_id", return_value=None), \
            mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client, \
            mock.patch("backend.emby_server.douban.enabled", return_value=False):
        fake_client.configured = True
        with mock.patch.object(_sc, "_tmdb_work", return_value=(None, None)):
            fetched = enrich_worker._enrich_fetch(it, holder=None)
    assert fetched.get("tmdb_hit") is None
    assert calls["douban"] == 0 and calls["bgm"] == 0, "串行兑底一次都不该发生"
    # 跳过动作可观测
    stats = enrich_worker.progress.stage_stats()
    assert any("fallback_skip" in k for k in stats), stats


class _CountingProvider:
    """假远程提供者：数 list_dir / read_text 次数（验收处方 1 的核心指标）"""

    def __init__(self, nfo_names=(), sub_names=()):
        self.list_dir_calls: list = []
        self.read_text_calls: list = []
        self._nfo = set(nfo_names)
        self._sub = set(sub_names)

    def list_dir(self, rel: str = "/"):
        self.list_dir_calls.append(rel)
        base = rel.rstrip("/").rpartition("/")[2] or ""
        out = []
        if base in ("Season 1", "剧名 (2020)"):
            for n in (1, 2, 3):
                out.append(type("E", (), {"name": f"E{n:02d}.mkv",
                                          "rel": f"{rel.rstrip('/')}/E{n:02d}.mkv",
                                          "is_dir": False})())
            for nfo in sorted(self._nfo):
                out.append(type("F", (), {"name": nfo,
                                          "rel": f"{rel.rstrip('/')}/{nfo}",
                                          "is_dir": False})())
        if rel.rstrip("/").endswith("剧名 (2020)") or rel == "/剧名 (2020)":
            pass
        return out

    def read_text(self, rel: str) -> str:
        self.read_text_calls.append(rel)
        if rel.endswith("tvshow.nfo"):
            return "<tvshow><title>剧名</title></tvshow>"
        if rel.endswith(".nfo"):
            return "<episodedetails><title>E</title></episodedetails>"
        raise FileNotFoundError(rel)


def _episode_scan_file(monkeypatch, provider, name="E01.mkv"):
    """把 _scanfile_from_item 换成假提供者，避免真去查 StorageMount 表"""
    from backend.emby_server import scanner as _sc
    scan_file = _sc.ScanFile(
        stored_path=f"mount://9/剧名 (2020)/Season 1/{name}", name=name,
        local_dir=None, dir_rel="/剧名 (2020)/Season 1", size=1, container="mkv",
        mount_id=9, rel=f"/剧名 (2020)/Season 1/{name}", provider=provider,
    )
    monkeypatch.setattr(enrich_worker, "_scanfile_from_item",
                        lambda it: scan_file)
    return scan_file


def _episode_item(db, series=None, **kw):
    kw.setdefault("item_type", "episode")
    kw.setdefault("file_path", f"mount://9/剧名 (2020)/Season 1/{kw.get('name', 'E01.mkv')}")
    if series is not None:
        kw.setdefault("series_id", series.id)
        kw.setdefault("parent_id", series.id)
    return _make_item(db, **kw)


def test_shared_ctx_reuses_listing_within_group(db, monkeypatch):
    """ctx 复用（处方 1）：同一组里同目录只列一次，tvshow.nfo 只读一次"""
    provider = _CountingProvider(nfo_names=("tvshow.nfo",))
    _episode_scan_file(monkeypatch, provider)
    it1 = _episode_item(db)
    it2 = _episode_item(db, name="E02.mkv")
    holder: dict = {}
    enrich_worker._enrich_fetch(it1, holder=holder)
    enrich_worker._enrich_fetch(it2, holder=holder)
    # 同一目录只列一次（旧实现每条集都列）；tvshow.nfo 全组只读一次
    assert provider.list_dir_calls.count("/剧名 (2020)/Season 1") == 1
    tvshow_reads = sum(1 for r in provider.read_text_calls
                       if r.endswith("tvshow.nfo"))
    assert tvshow_reads == 1


def test_shared_ctx_rebuilt_on_new_group(db, monkeypatch):
    """组边界重置 holder：新组重建 ctx，缓存不跨组携带"""
    p1 = _CountingProvider()
    _episode_scan_file(monkeypatch, p1)
    it1 = _episode_item(db)
    holder: dict = {}
    enrich_worker._enrich_fetch(it1, holder=holder)
    ctx1 = holder["ctx"]
    enrich_worker._enrich_fetch(_episode_item(db, name="E02.mkv"), holder=holder)
    assert holder["ctx"] is ctx1
    # 新组：holder 置空后重建
    holder = {}
    enrich_worker._enrich_fetch(_episode_item(db, name="E03.mkv"), holder=holder)
    assert holder["ctx"] is not ctx1


def test_pure_inherit_zero_network_and_copies_parent(db, monkeypatch):
    """纯继承（处方 3）：父级已 done 且有图、本集无自带 NFO → 零 NFO/TMDB，落 done"""
    from backend.emby_server import scan_progress as _progress
    before = _progress.stage_stats().get("enrich_pure_inherit", {}).get("count", 0)
    series = _make_item(db, item_type="series", name="父级已完成的剧",
                        enrich_status="done", tmdb_id="777",
                        poster_path="/img/p.jpg", backdrop_path="/img/f.jpg")
    provider = _CountingProvider()   # 目录里没有任何 .nfo
    _episode_scan_file(monkeypatch, provider)
    it = _episode_item(db, series=series, enrich_status="enriching",
                       enrich_claimed_at=datetime.now())
    parent = enrich_worker._inherit_parent_info(db, it)
    assert parent is not None and parent["series_id"] == series.id
    res = enrich_worker._enrich_fetch(it, holder={}, inherit_parent=parent)
    assert res.get("pure_inherit") is True
    assert res["nfo_data"] is None and res["tmdb_hit"] is None
    assert provider.read_text_calls == []          # 一个 NFO 都没读
    # 写库阶段：图片沿父级回退 + 落 done + 标 inherit + 释放租约
    enrich_worker._enrich_apply(db, it, res)
    db.commit()
    assert it.enrich_status == "done"
    assert it.metadata_source == "inherit"
    assert it.enrich_claimed_at is None
    assert it.poster_path == "/img/p.jpg"          # 父级海报回退（既有逻辑）
    assert it.backdrop_path == "/img/f.jpg"
    # 计数器可见（用增量断言，不清全局计数器）
    after = _progress.stage_stats().get("enrich_pure_inherit", {}).get("count", 0)
    assert after == before + 1


def test_pure_inherit_never_overwrites_existing_fields(db, monkeypatch):
    """「不覆盖已有字段」回归：本集已有自己的图 / metadata_source 时不动它们"""
    series = _make_item(db, item_type="series", name="父级有图的剧",
                        enrich_status="done", tmdb_id="888",
                        poster_path="/img/parent.jpg")
    provider = _CountingProvider()
    _episode_scan_file(monkeypatch, provider)
    it = _episode_item(db, series=series, poster_path="/img/mine.jpg",
                       metadata_source="nfo")
    parent = enrich_worker._inherit_parent_info(db, it)
    res = enrich_worker._enrich_fetch(it, holder={}, inherit_parent=parent)
    assert res.get("pure_inherit") is True
    enrich_worker._enrich_apply(db, it, res)
    db.commit()
    assert it.poster_path == "/img/mine.jpg"       # 不被父级图覆盖
    assert it.metadata_source == "nfo"             # 不被 inherit 覆盖


def test_pure_inherit_requires_parent_done_and_no_repair(db, monkeypatch):
    """父级未 done / 本集带修复标记 → 不走捷径，照旧完整抓取"""
    series_pending = _make_item(db, item_type="series", name="还没刮完的剧",
                                enrich_status="pending", tmdb_id="999",
                                poster_path="/img/p.jpg")
    provider = _CountingProvider()
    _episode_scan_file(monkeypatch, provider)
    it1 = _episode_item(db, series=series_pending)
    assert enrich_worker._inherit_parent_info(db, it1) is None

    series_done = _make_item(db, item_type="series", name="已完成的剧",
                             enrich_status="done", tmdb_id="999",
                             poster_path="/img/p.jpg")
    it2 = _episode_item(db, series=series_done,
                        repair_requested_at=datetime.now())
    assert enrich_worker._inherit_parent_info(db, it2) is None


def test_episode_with_own_nfo_takes_full_path(db, monkeypatch):
    """本集有自己的 NFO → 不走纯继承（NFO 里可能有本集专属数据）"""
    series = _make_item(db, item_type="series", name="有 NFO 的剧",
                        enrich_status="done", tmdb_id="555",
                        poster_path="/img/p.jpg")
    provider = _CountingProvider(nfo_names=("E01.mkv.nfo",))
    _episode_scan_file(monkeypatch, provider)
    it = _episode_item(db, series=series)
    parent = enrich_worker._inherit_parent_info(db, it)
    assert parent is not None
    res = enrich_worker._enrich_fetch(it, holder={}, inherit_parent=parent)
    assert not res.get("pure_inherit")
    assert provider.read_text_calls, "有自带 NFO 就应该真去读"


def test_process_item_closes_read_transaction_before_slow_io(db, monkeypatch):
    """补全进入 FUSE/TMDB 前，Session 不得停在 idle in transaction。"""
    it = _make_item(db, item_type="movie", name="事务边界测试",
                    file_path="/tmp/transaction-boundary.mp4",
                    enrich_status="enriching", enrich_attempts=2)
    seen = {}

    def fake_fetch(item, **_kwargs):
        seen["type"] = type(item)
        seen["in_transaction"] = db.in_transaction()
        return {"ok": False, "error": "测试用失败"}

    monkeypatch.setattr(enrich_worker, "_enrich_fetch", fake_fetch)
    outcome = enrich_worker._process_item(db, it)

    assert outcome == "retry"
    assert seen["type"] is enrich_worker._EnrichItemSnapshot
    assert seen["in_transaction"] is False
    assert not db.in_transaction()


def test_empty_claim_rolls_back_read_transaction(db):
    """没有待补全条目时，worker 睡眠前也不能留下 idle in transaction。"""
    db.rollback()
    assert enrich_worker._claim_batch(db, 10) == []
    assert not db.in_transaction()


# ---------- 瞬态失败与「真搜不到」的边界（问题一：刮削时好时坏） ----------

def test_transient_tmdb_failure_retries_instead_of_terminal(db, monkeypatch):
    """瞬态 TMDB 失败 → ok=False → 退避重试；绝不写终态 done+none

    复现（scripts/repro_scrape_stability.py，修复前）：网络抖动被吞成
    ``search()=None`` → ``_enrich_apply`` 写终态 ``done + metadata_source='none'``，
    条目从此不再重试——「时好时坏」的根因。
    """
    from backend.emby_server import tmdb as tmdb_mod

    def boom(*a, **k):
        raise tmdb_mod.TmdbTransientError(
            "TMDB 网络请求失败（重试耗尽）: /search/tv")

    monkeypatch.setattr(tmdb_mod.TmdbClient, "configured",
                        property(lambda self: True))
    monkeypatch.setattr(tmdb_mod.tmdb_client, "search", boom)
    monkeypatch.setattr("backend.emby_server.douban.enabled", lambda db: False)

    it = _make_item(db, item_type="series", name="黑鸟", file_path=None)
    outcome = enrich_worker._process_item(db, it)
    db.refresh(it)

    assert outcome == "retry"
    assert it.enrich_status == "pending"
    assert it.enrich_attempts == 1
    assert it.enrich_next_retry_at is not None, "应排入指数退避"
    assert it.metadata_source != "none", "瞬态失败不许写成「搜过没有」终态"


def test_genuine_miss_still_terminal_none(db, monkeypatch):
    """对照组：真搜不到（正常返回 None）仍是终态 done+none——新旧边界不变"""
    from backend.emby_server import tmdb as tmdb_mod

    monkeypatch.setattr(tmdb_mod.TmdbClient, "configured",
                        property(lambda self: True))
    monkeypatch.setattr(tmdb_mod.tmdb_client, "search", lambda *a, **k: None)
    monkeypatch.setattr("backend.emby_server.douban.enabled", lambda db: False)

    it = _make_item(db, item_type="series", name="不存在的剧", file_path=None)
    outcome = enrich_worker._process_item(db, it)
    db.refresh(it)

    assert outcome == "done"
    assert it.metadata_source == "none"
    assert it.enrich_status == "done"


# ---------------------------------------------------------------------------
# v2.54.0 方案1 测试：TMDB-miss 时的多源兜底
# ---------------------------------------------------------------------------

def test_should_multisource_on_tmdb_miss_english_title(db):
    """非中文标题不走多源兜底（TMDB 已是最优）"""
    from backend.emby_server import enrich_worker as ew
    it = _make_item(db, item_type="series", name="Breaking Bad", file_path=None)
    assert ew._should_multisource_on_tmdb_miss(it, "series") is False


def test_should_multisource_on_tmdb_miss_non_series(db):
    """非剧/电影类型不走多源兜底"""
    from backend.emby_server import enrich_worker as ew
    it = _make_item(db, item_type="episode", name="中文剧集", file_path=None)
    assert ew._should_multisource_on_tmdb_miss(it, "episode") is False


def test_should_multisource_on_tmdb_miss_with_tmdb_id(db):
    """已有 tmdb_id 的不走多源兜底"""
    from backend.emby_server import enrich_worker as ew
    it = _make_item(db, item_type="series", name="中文剧",
                    file_path=None, tmdb_id="12345")
    assert ew._should_multisource_on_tmdb_miss(it, "series") is False


def test_should_multisource_on_tmdb_miss_switch_off(db):
    """多源总开关关闭时不走兜底（默认行为不变）"""
    from backend.emby_server import enrich_worker as ew
    from backend.models import SystemConfig
    # 确保开关关闭
    cfg = db.query(SystemConfig).filter_by(key="meta_sources_enabled").first()
    if cfg:
        cfg.value = "0"
    else:
        db.add(SystemConfig(key="meta_sources_enabled", value="0"))
    db.commit()

    it = _make_item(db, item_type="series", name="中文剧集", file_path=None)
    assert ew._should_multisource_on_tmdb_miss(it, "series") is False


def test_collect_multisource_guarded_semaphore():
    """并发信号量：拿不到时返回 False，不阻塞"""
    from backend.emby_server import enrich_worker as ew
    # 占满信号量（2 个许可）
    assert ew._MULTISOURCE_SEMAPHORE.acquire(blocking=False)
    assert ew._MULTISOURCE_SEMAPHORE.acquire(blocking=False)
    try:
        # 第三个拿不到
        from unittest.mock import MagicMock
        item = MagicMock()
        item.name = "测试"
        result = {}
        assert ew._collect_multisource_guarded(item, "series", result) is False
    finally:
        ew._MULTISOURCE_SEMAPHORE.release()
        ew._MULTISOURCE_SEMAPHORE.release()


def test_stop_clears_registry_without_error():
    """stop() 不应抛 UnboundLocalError，且应清空线程注册表并 join 所有线程"""
    from unittest.mock import MagicMock

    from backend.emby_server import enrich_worker as ew

    # 备份全局状态，测试结束后恢复，避免污染其他测试
    orig_threads = ew._worker_threads
    orig_stop_event = ew._stop_event
    try:
        # 伪造两个线程对象，验证 join 均以 timeout=10 调用
        t1, t2 = MagicMock(), MagicMock()
        ew._worker_threads = [t1, t2]

        ew.stop()  # 修复前此处必抛 UnboundLocalError

        assert ew._worker_threads == []
        for t in (t1, t2):
            t.join.assert_called_once_with(timeout=10)

        # 注册表为空时再次调用也不应报错
        ew.stop()
        assert ew._worker_threads == []
    finally:
        ew._worker_threads = orig_threads
        ew._stop_event = orig_stop_event
        ew._stop_event.clear()
