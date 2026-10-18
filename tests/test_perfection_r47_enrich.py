"""r47（enrich 刮削管线资源有界 + SA 反风控实证）回归测试。

F1：drive_auth.drop_sa_affinity 死代码删除（全仓零调用方、__all__ 未收录、
    docstring 承诺的"403 断亲和换 SA"从未实现且与既定退避口径冲突）。
F2：drive_changes._watcher_loop 异常分支固定 300s → _jittered_interval()
    （与文件自身反风控抖动设计一致）。
F3：enrich_worker._note_enrich_batch_feed 在 done_rows 为空提前返回前
    db.rollback()（避免 idle-in-transaction 占池位到下一轮）。
"""
import inspect
import os
import tempfile

import pytest

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")
_fd, _tmppath = tempfile.mkstemp(suffix=".db"); os.close(_fd)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmppath}"

from backend.emby_server import drive_auth  # noqa: E402
from backend.emby_server import drive_changes  # noqa: E402
from backend.emby_server import enrich_worker  # noqa: E402


# ---------------------------------------------------------------- F1
def test_drop_sa_affinity_removed():
    assert not hasattr(drive_auth, "drop_sa_affinity"), \
        "drop_sa_affinity 应已删除（死代码）"
    assert "drop_sa_affinity" not in drive_auth.__all__


def test_drive_auth_public_api_intact():
    for name in ("get_drive_bearer_token", "drive_auth_headers",
                 "drive_api_media_url", "note_drive_unauthorized",
                 "invalidate_drive_bearer_token", "premint_tokens", "sa_count"):
        assert callable(getattr(drive_auth, name)), name


# ---------------------------------------------------------------- F2
def test_watcher_loop_jittered_by_default_fixed_only_on_jitter_failure():
    src = inspect.getsource(drive_changes._watcher_loop)
    # 默认走抖动；固定间隔只在抖动函数自身抛异常时作为保底（守护线程不许死）
    assert "wait_s = _jittered_interval()" in src
    assert src.count("wait_s = float(POLL_INTERVAL)") == 1


def test_jittered_interval_within_bounds():
    vals = [drive_changes._jittered_interval() for _ in range(50)]
    lo = drive_changes.POLL_INTERVAL * (1 - drive_changes.POLL_JITTER_RATIO)
    hi = drive_changes.POLL_INTERVAL * (1 + drive_changes.POLL_JITTER_RATIO)
    assert all(lo <= v <= hi for v in vals), (min(vals), max(vals))
    # 真在抖动，不是固定值
    assert len(set(vals)) > 1


# ---------------------------------------------------------------- F3
class _Q:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.rollbacks = 0

    def query(self, *a):
        return _Q(self._rows)

    def rollback(self):
        self.rollbacks += 1


class _Item:
    def __init__(self, i):
        self.id = i


@pytest.fixture()
def feed_env(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.panel_feed.note_feed",
                        lambda db, text, **kw: calls.append((text, kw)))
    monkeypatch.setattr(enrich_worker, "_enrich_feed_last_ts", 0.0)
    return calls


def test_note_feed_empty_done_rows_rollbacks(feed_env):
    db = _FakeDB(rows=[])
    enrich_worker._note_enrich_batch_feed(db, [_Item(1)])
    assert db.rollbacks == 1, "空 done_rows 提前返回前必须 rollback"
    assert feed_env == [], "无补全条目时不应写 feed"


def test_note_feed_normal_path_unchanged(feed_env):
    db = _FakeDB(rows=[(7, "测试剧", 3)])
    enrich_worker._note_enrich_batch_feed(db, [_Item(7)])
    assert db.rollbacks == 0, "正常路径不应多一次 rollback"
    assert len(feed_env) == 1
    text, kw = feed_env[0]
    assert "测试剧" in text and kw.get("library_id") == 3


def test_note_feed_throttled_no_query():
    db = _FakeDB(rows=[(7, "测试剧", 3)])
    import time
    enrich_worker._enrich_feed_last_ts = time.monotonic()  # 节流命中
    try:
        enrich_worker._note_enrich_batch_feed(db, [_Item(7)])
    finally:
        enrich_worker._enrich_feed_last_ts = 0.0
    assert db.rollbacks == 0, "节流命中时连 SELECT 都没做，不应 rollback"
