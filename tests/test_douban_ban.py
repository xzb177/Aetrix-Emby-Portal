"""豆瓣封禁（403/429/验证码页）不能当成「没搜中」：
抛瞬态错误 + 全局冷却 + 条目留待重试（不落 TMDB/none、不烧重试次数）；锁定条目不走豆瓣预搜。"""
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
_dbmod.configure_session_local(_sm(bind=_dbmod.engine))

import io
import urllib.error
import uuid
from unittest import mock

import pytest

from backend.database import SessionLocal, init_db
from backend.emby_server import models as em
from backend.emby_server import enrich_worker
from backend.emby_server import douban as dbn

init_db()


@pytest.fixture(autouse=True)
def _reset_ban():
    # 批一 #8：_trip_ban 会同步喂统一熔断注册表，进程全局状态一并复位，
    # 不许把开着的断路器漏给同进程的其他测试文件
    from backend.emby_server.metasources import breaker as provider_breaker
    dbn.reset_ban()
    provider_breaker.reset_all()
    yield
    dbn.reset_ban()
    provider_breaker.reset_all()


class _Resp(io.BytesIO):
    def __init__(self, body: str, url: str):
        super().__init__(body.encode("utf-8"))
        self._url = url

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_403_raises_and_starts_cooldown():
    calls = []

    def fake_open(req, timeout=None):
        calls.append(req.full_url)
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)

    c = dbn.DoubanClient(interval=0)
    with mock.patch.object(dbn.urllib.request, "urlopen", fake_open):
        with pytest.raises(dbn.DoubanBannedError):
            c.search("沦陷", 2026, "series")
        # 冷却期内不再发请求，直接抛
        with pytest.raises(dbn.DoubanBannedError):
            c.get_details("123")
    assert len(calls) == 1
    assert dbn.ban_remaining() > 0


def test_captcha_page_detected():
    def fake_open(req, timeout=None):
        return _Resp("<html><title>禁止访问</title>检测到有异常请求</html>",
                     "https://sec.douban.com/b?r=x")

    c = dbn.DoubanClient(interval=0)
    with mock.patch.object(dbn.urllib.request, "urlopen", fake_open):
        with pytest.raises(dbn.DoubanBannedError):
            c.search("沦陷", 2026, "series")


def test_normal_no_match_still_none():
    def fake_open(req, timeout=None):
        return _Resp("[]", req.full_url)

    c = dbn.DoubanClient(interval=0)
    with mock.patch.object(dbn.urllib.request, "urlopen", fake_open):
        assert c.search("沦陷", 2026, "series") is None
    assert dbn.ban_remaining() == 0


def _series(db, **kw):
    it = em.MediaItem(guid=uuid.uuid4().hex, library_id=1, item_type="series",
                      name=kw.pop("name", "沦陷"), production_year=2026,
                      enrich_status="enriching", enrich_attempts=0, **kw)
    db.add(it)
    db.commit()
    return it


def test_banned_item_goes_to_quiet_backfill_not_hot_retry():
    """豆瓣中途撞封（兜底返回 banned）：用户口径（2026-10-15）——不许再走
    900 秒冷却热重试，进 -20 安静补刮队列：pending、无定时重试、attempts
    不涨、不判 none；解封后由 _claim_batch 放行补刮（见下面两个测试）。"""
    db = SessionLocal()
    try:
        item = _series(db)
        fake = mock.MagicMock()
        fake.search.side_effect = dbn.DoubanBannedError("403")
        with mock.patch.object(dbn, "client", fake), \
                mock.patch.object(dbn, "enabled", return_value=True), \
                mock.patch.object(dbn, "min_interval", return_value=0.0):
            outcome = enrich_worker._process_item(db, item, {})
        db.expire_all()
        fresh = db.query(em.MediaItem).filter(em.MediaItem.id == item.id).first()
        assert fresh.enrich_status == "pending"
        assert fresh.enrich_priority == enrich_worker.ENRICH_PRIORITY_DOUBAN_BACKFILL
        assert fresh.enrich_next_retry_at is None, "安静补刮队列不许带热重试定时"
        assert fresh.metadata_source in (None, "")
        assert (fresh.enrich_attempts or 0) == 0, "封禁不是条目失败，不能烧重试次数"
        assert outcome == "retry"
    finally:
        db.close()


def test_banned_item_flag_off_keeps_legacy_cooldown_requeue():
    """降级开关被管理员显式关掉（紧急回退）：沿用旧的冷却延后行为
    （带 next_retry_at 定时、不进 -20 队列），保证回退口可用。"""
    db = SessionLocal()
    try:
        item = _series(db)
        fake = mock.MagicMock()
        fake.search.side_effect = dbn.DoubanBannedError("403")
        with mock.patch.object(dbn, "client", fake), \
                mock.patch.object(dbn, "enabled", return_value=True), \
                mock.patch.object(dbn, "min_interval", return_value=0.0), \
                mock.patch.object(enrich_worker, "_douban_ban_downgrade_flag_on",
                                  return_value=False):
            outcome = enrich_worker._process_item(db, item, {})
        db.expire_all()
        fresh = db.query(em.MediaItem).filter(em.MediaItem.id == item.id).first()
        assert fresh.enrich_status == "pending"
        assert fresh.enrich_next_retry_at is not None
        assert fresh.enrich_priority != enrich_worker.ENRICH_PRIORITY_DOUBAN_BACKFILL
        assert outcome == "retry"
    finally:
        db.close()


def test_ban_downgrade_flag_defaults_on():
    """用户明令「豆瓣被封就跳过」：配置从未写过时默认开（不是默认关）。"""
    from backend import models as _models
    from backend.integrations import store
    db = SessionLocal()
    try:
        db.query(_models.SystemConfig).filter(
            _models.SystemConfig.key == enrich_worker.CFG_DOUBAN_BAN_DOWNGRADE
        ).delete()
        db.commit()
        store.invalidate(enrich_worker.CFG_DOUBAN_BAN_DOWNGRADE)
        assert enrich_worker._douban_ban_downgrade_flag_on() is True
        cfg = enrich_worker.get_douban_ban_downgrade_config(db)
        assert cfg["enabled"] is True
    finally:
        db.close()


def test_locked_item_skips_douban_prephase():
    db = SessionLocal()
    try:
        item = _series(db, name="锁定剧", metadata_locked=True)
        snap = enrich_worker._snapshot_item(item)
        fake = mock.MagicMock()
        with mock.patch.object(dbn, "client", fake), \
                mock.patch.object(dbn, "enabled", return_value=True), \
                mock.patch.object(dbn, "min_interval", return_value=0.0):
            enrich_worker._enrich_fetch_pre(snap)
        fake.search.assert_not_called()
    finally:
        db.close()


def test_ban_backoff_is_exponential_with_jitter():
    """连续被封时冷却指数退避（900→1800→3600…封顶7200），且带抖动不固定。"""
    import time as _time
    seen = set()
    for _ in range(5):
        dbn.reset_ban()
        dbn._trip_ban("test")
        r = dbn.ban_remaining()
        assert 900 <= r <= 960, f"首次冷却应在 [900,960]，实际 {r}"
        seen.add(round(r, 1))
    # 抖动：5 次冷却时长不全相同
    assert len(seen) > 1, "冷却没有随机抖动"

    dbn.reset_ban()
    dbn._trip_ban("t1")
    first = dbn.ban_remaining()
    # 不重置 strikes，直接再封一次 → 退避翻倍
    dbn._banned_until = 0  # 模拟冷却结束但 strikes 保留
    dbn._trip_ban("t2")
    second = dbn.ban_remaining()
    assert 1800 <= second <= 1860, f"第二次冷却应在 [1800,1860]，实际 {second}"
    assert second > first
    assert dbn.ban_strikes() == 2
    dbn.reset_ban()
    assert dbn.ban_strikes() == 0


def test_rate_limiter_has_jitter():
    """限速器等待时间带抖动：同样 interval，两次 acquire 的实际间隔不完全相同。"""
    import time as _time
    lim = dbn._RateLimiter()
    intervals = []
    for _ in range(4):
        t0 = _time.monotonic()
        lim.acquire(0.05)
        intervals.append(_time.monotonic() - t0)
    # 第一次不限（_last=0），后三次应在 [0.05, 0.08] 附近且不全等
    waits = intervals[1:]
    assert all(0.04 <= w <= 0.15 for w in waits), waits
    assert len({round(w, 3) for w in waits}) > 1, "限速器没有抖动"


# ==================== v2.57.0 提速包 item2：封禁期跳过 TMDB 预搜索 ====================

from types import SimpleNamespace as _SN


def _ban_item(name="狂飙", kind="series", tmdb_id=None, locked=False):
    return _SN(name=name, tmdb_id=tmdb_id, metadata_locked=locked,
              production_year=2023)


def _ban_on(monkeypatch):
    """模拟豆瓣封禁中：ban_remaining>0，CJK 判定走真实现，兜底开关开着。"""
    monkeypatch.setattr(dbn, "ban_remaining", lambda: 600.0)
    monkeypatch.setattr(enrich_worker, "_cached_douban_cfg",
                        lambda: (True, 2.5))


def test_presearch_skip_during_ban_for_cjk_series(monkeypatch):
    _ban_on(monkeypatch)
    assert enrich_worker._douban_ban_presearch_skip(
        _ban_item("狂飙", "series"), "series") is True


def test_no_skip_when_not_banned(monkeypatch):
    monkeypatch.setattr(dbn, "ban_remaining", lambda: 0.0)
    monkeypatch.setattr(enrich_worker, "_cached_douban_cfg",
                        lambda: (True, 2.5))
    assert enrich_worker._douban_ban_presearch_skip(
        _ban_item("狂飙", "series"), "series") is False


def test_no_skip_for_non_cjk_title(monkeypatch):
    _ban_on(monkeypatch)
    assert enrich_worker._douban_ban_presearch_skip(
        _ban_item("Breaking Bad", "series"), "series") is False


def test_no_skip_when_item_has_tmdb_id(monkeypatch):
    _ban_on(monkeypatch)
    assert enrich_worker._douban_ban_presearch_skip(
        _ban_item("狂飙", "series", tmdb_id="123"), "series") is False


def test_no_skip_for_episode_kind(monkeypatch):
    _ban_on(monkeypatch)
    assert enrich_worker._douban_ban_presearch_skip(
        _ban_item("狂飙", "episode"), "episode") is False


def test_no_skip_when_douban_disabled(monkeypatch):
    monkeypatch.setattr(dbn, "ban_remaining", lambda: 600.0)
    monkeypatch.setattr(enrich_worker, "_cached_douban_cfg",
                        lambda: (False, 2.5))
    assert enrich_worker._douban_ban_presearch_skip(
        _ban_item("狂飙", "series"), "series") is False


# ---------------------------------------------------------------------------
# 豆瓣封禁安静补刮队列（2026-10-15 用户口径：被封就跳过、解封后补刮）
# ---------------------------------------------------------------------------

def _movie(db, **kw):
    it = em.MediaItem(guid=uuid.uuid4().hex, library_id=1, item_type="movie",
                      name=kw.pop("name", "补刮电影"), production_year=2024,
                      enrich_status=kw.pop("enrich_status", "pending"),
                      enrich_attempts=0, **kw)
    db.add(it)
    db.commit()
    return it


def test_claim_batch_holds_backfill_while_banned(monkeypatch):
    """封禁未解除时 _claim_batch 不认领 -20 补刮条目（认领了也用不上豆瓣，
    只会空转）；解封后自动放行。普通条目不受影响。"""
    db = SessionLocal()
    try:
        normal = _movie(db, name="正常条目", enrich_priority=0)
        backfill = _movie(
            db, name="补刮条目",
            enrich_priority=enrich_worker.ENRICH_PRIORITY_DOUBAN_BACKFILL)
        monkeypatch.setattr(dbn, "ban_remaining", lambda: 600.0)
        batch = enrich_worker._claim_batch(db, 500)
        ids = [b.id for b in batch]
        assert normal.id in ids
        assert backfill.id not in ids, "封禁期认领补刮条目 = 空转，不许认领"
        monkeypatch.setattr(dbn, "ban_remaining", lambda: 0.0)
        batch2 = enrich_worker._claim_batch(db, 500)
        assert backfill.id in [b.id for b in batch2], "解封后补刮条目必须被放行"
    finally:
        db.close()


def _no_hit_fetched(**extra):
    base = {"ok": True, "poster": None, "fanart": None, "external_subs": [],
            "nfo_data": None, "tmdb_id": None, "tmdb_hit": None,
            "tmdb_details": None, "error": None}
    base.update(extra)
    return base


def test_apply_ban_skipped_no_hit_goes_backfill_not_none(monkeypatch):
    """封禁期豆瓣被跳过、本轮各源都没刮到：不许判 none 终态，进 -20 补刮
    队列等解封后补跑豆瓣（metadata_source 不许写 none、attempts 不涨）。"""
    db = SessionLocal()
    try:
        it = _movie(db, name="封禁期未命中", enrich_status="enriching")
        monkeypatch.setattr(enrich_worker, "_cached_douban_cfg",
                            lambda: (True, 2.5))
        with mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client:
            fake_client.configured = True
            enrich_worker._enrich_apply(
                db, it, _no_hit_fetched(_ban_downgrade_skipped_douban=True))
        db.commit()
        db.expire_all()
        fresh = db.query(em.MediaItem).filter(em.MediaItem.id == it.id).first()
        assert fresh.enrich_status == "pending"
        assert fresh.enrich_priority == enrich_worker.ENRICH_PRIORITY_DOUBAN_BACKFILL
        assert fresh.metadata_source in (None, ""), "封禁期没刮到不许判 none 终态"
        assert (fresh.enrich_attempts or 0) == 0
        assert fresh.enrich_next_retry_at is None
    finally:
        db.close()


def test_apply_backfill_round_no_hit_goes_none(monkeypatch):
    """补刮轮（priority 已是 -20）豆瓣可用、仍没刮到 → 这次是真没有：
    正常 none 终态收尾、优先级清零，不许无限留在补刮队列。"""
    db = SessionLocal()
    try:
        it = _movie(db, name="补刮轮真没有", enrich_status="enriching",
                    enrich_priority=enrich_worker.ENRICH_PRIORITY_DOUBAN_BACKFILL)
        monkeypatch.setattr(enrich_worker, "_cached_douban_cfg",
                            lambda: (True, 2.5))
        with mock.patch("backend.emby_server.tmdb.tmdb_client") as fake_client:
            fake_client.configured = True
            enrich_worker._enrich_apply(
                db, it, _no_hit_fetched(_ban_downgrade_skipped_douban=True))
        db.commit()
        db.expire_all()
        fresh = db.query(em.MediaItem).filter(em.MediaItem.id == it.id).first()
        assert fresh.enrich_status == "done"
        assert fresh.metadata_source == "none"
        assert fresh.enrich_priority == 0
    finally:
        db.close()


def test_backfill_round_uses_douban_after_unban(monkeypatch):
    """解封后补刮轮真的会跑豆瓣兜底：-20 条目经 pre→post→apply，豆瓣命中
    落库、状态 done、优先级清零。

    字段级完成口径缺省开：开着时本例条目缺字段会先回捞一轮而不是 done；
    本例只验豆瓣解封补刮，经保存路径关掉。
    """
    from backend import models as _models
    from backend.integrations import store as _store
    db = SessionLocal()
    try:
        enrich_worker.save_enrich_tuning_config(db, {
            enrich_worker.CFG_ENRICH_FIELD_COMPLETENESS: False,
        })
        item = _series(
            db, name="解封补刮",
            enrich_priority=enrich_worker.ENRICH_PRIORITY_DOUBAN_BACKFILL)
        snap = enrich_worker._snapshot_item(item)
        fake_dbn = mock.MagicMock()
        fake_dbn.search.return_value = {"id": "3700002", "title": "解封补刮",
                                        "year": "2026", "image": "",
                                        "type": "tv"}
        fake_dbn.get_details.return_value = {"title": "解封补刮",
                                             "overview": "简介",
                                             "runtime_ticks": 0}
        fake_dbn.get_celebrities.return_value = []
        fake_tmdb = mock.MagicMock()
        fake_tmdb.configured = True
        with mock.patch("backend.emby_server.tmdb.tmdb_client", fake_tmdb), \
                mock.patch.object(dbn, "client", fake_dbn), \
                mock.patch.object(enrich_worker, "_cached_douban_cfg",
                                  return_value=(True, 2.5)), \
                mock.patch.object(enrich_worker, "_cached_altmeta_cfg",
                                  return_value=(True, 2.5, 2.5)), \
                mock.patch.object(enrich_worker, "_alias_tmdb_id",
                                  return_value=None), \
                mock.patch("backend.emby_server.scanner._tmdb_work",
                           return_value=(None, None)):
            result, ctx = enrich_worker._enrich_fetch_pre(snap)
            out = enrich_worker._enrich_fetch_tmdb_post(result, item, ctx)
        assert out.get("douban_fallback_hit"), "解封后补刮轮必须真跑豆瓣兜底"
        fresh = db.query(em.MediaItem).filter(em.MediaItem.id == item.id).first()
        enrich_worker._enrich_apply(db, fresh, out)
        db.commit()
        db.expire_all()
        fresh = db.query(em.MediaItem).filter(em.MediaItem.id == item.id).first()
        assert fresh.enrich_status == "done"
        assert fresh.enrich_priority == 0
        assert fresh.metadata_source != "none"
    finally:
        db.query(_models.SystemConfig).filter(
            _models.SystemConfig.key == enrich_worker.CFG_ENRICH_FIELD_COMPLETENESS
        ).delete(synchronize_session=False)
        db.commit()
        _store.invalidate(enrich_worker.CFG_ENRICH_FIELD_COMPLETENESS)
        enrich_worker._enrich_tuning_cache["at"] = 0.0
        enrich_worker._enrich_tuning_cache["cfg"] = None
        db.close()
