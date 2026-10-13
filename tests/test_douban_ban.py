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
    dbn.reset_ban()
    yield
    dbn.reset_ban()


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


def test_banned_item_requeued_not_none():
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
        assert fresh.metadata_source in (None, "")
        assert (fresh.enrich_attempts or 0) == 0, "封禁不是条目失败，不能烧重试次数"
        assert fresh.enrich_next_retry_at is not None
        assert outcome == "retry"
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
