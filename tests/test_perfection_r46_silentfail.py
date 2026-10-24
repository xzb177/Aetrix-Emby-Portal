# -*- coding: utf-8 -*-
"""r46 静默失败专项回归测试。

隔离：内存 SQLite，不碰开发库/生产库，不 import backend.main。
覆盖 r46 的 5 项修复：
  F1 post-commit 通知统一 quiet 守卫（11 个裸调点 + 2 处内联保护并入）
  F2 _derive_activity 数据源异常不再谎报"一切正常"
  F3 gdrive_sa_status 读失败与真空值区分（仅静态断言，见 test_sa_status_flags_present）
  F4 重置密码 Emby 同步失败诚实返回 emby_synced
  F5 streaming._drive_403_error_reason 函数恢复（NameError 回归）
"""
import asyncio
import os

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend import models
from backend import notifications as notif
from backend.api import panel as panel_api


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


# ---------------- F1：quiet 守卫 ----------------

def test_quiet_helper_success_passthrough(monkeypatch):
    """成功时原样透传调用并返回 True。"""
    seen = {}

    async def fake_send(event_type, **kwargs):
        seen["event_type"] = event_type
        seen.update(kwargs)
        return {"in_app": True}

    monkeypatch.setattr(notif, "notify_admin_event", fake_send)
    ok = asyncio.run(notif.notify_admin_event_quiet(
        event_type="economy.checkin",
        log_ctx="checkin user_id=7",
        user_id=7, title="t", content="c",
    ))
    assert ok is True
    assert seen["event_type"] == "economy.checkin"
    assert seen["user_id"] == 7


def test_quiet_helper_swallows_exception(monkeypatch, caplog):
    """通知抛异常时不向上传播、返回 False、记 warning 日志。"""
    async def boom(event_type, **kwargs):
        raise RuntimeError("pool exhausted")

    monkeypatch.setattr(notif, "notify_admin_event", boom)
    with caplog.at_level("WARNING", logger="backend.notifications"):
        ok = asyncio.run(notif.notify_admin_event_quiet(
            event_type="economy.redeem",
            log_ctx="redeem user_id=9",
            user_id=9, title="t", content="c",
        ))
    assert ok is False
    assert any("redeem user_id=9" in r.getMessage() for r in caplog.records), \
        "必须记下可定位的日志（含 log_ctx）"


def test_send_fulfill_notifications_swallows(monkeypatch):
    """履约通知走统一守卫：单条失败不影响整批、不抛异常。"""
    async def boom(event_type, **kwargs):
        raise RuntimeError("channel down")

    monkeypatch.setattr(notif, "notify_admin_event", boom)
    from backend.api import economy as economy_api
    # 不抛异常即通过
    asyncio.run(economy_api.send_fulfill_notifications([
        {"event_type": "economy.fulfill", "user_id": 1,
         "title": "t", "content": "c"},
    ]))


def test_converted_sites_use_quiet_guard():
    """静态兜底：11 个 post-commit 裸调点必须全部走 quiet 守卫。

    （行为测试覆盖 helper 本身；这里防未来有人加回裸调。）
    """
    tree = os.path.join(os.path.dirname(__file__), "..")
    bad = []
    # 允许裸调的例外：
    #  - admin.py 的 send_user_message：发站内信是主动作，需返回值判失败（非 post-commit 附带通知）
    #  - reminders.py：整文件有自己的重试语义（失败撤占位、下轮补发）
    #  - notifications.py：helper 自身实现
    for rel in ("backend/api/admin.py", "backend/api/admin_economy.py",
                "backend/api/economy.py", "backend/api/orders_admin.py"):
        p = os.path.join(tree, rel)
        srclines = open(p, encoding="utf-8").read().split("\n")
        for i, line in enumerate(srclines, start=1):
            if "await notify_admin_event(" not in line:
                continue
            if "notify_admin_event_quiet(" in line:
                continue
            if rel == "backend/api/admin.py" and line.strip().startswith("results = "):
                continue  # send_user_message 主动作，需返回值
            bad.append(f"{rel}:{i}:{line.strip()[:60]}")
    assert not bad, f"发现 post-commit 裸调 notify_admin_event：{bad}"


# ---------------- F2：_derive_activity 诚实降级 ----------------

class _BoomDB:
    def query(self, *a, **k):
        raise RuntimeError("db gone")


def test_derive_activity_degraded_honest():
    """埋点表读失败 → 不谎报一切正常，打 warning 日志。"""
    lines = panel_api._derive_activity(_BoomDB(), health=None)
    texts = [x["text"] for x in lines]
    assert any("暂不可用" in t for t in texts), texts
    assert not any("一切正常" in t for t in texts), texts


def test_derive_activity_normal_empty(db):
    """数据源正常但无数据 → 保留原来的'一切正常'（非异常路径不动）。"""
    lines = panel_api._derive_activity(db, health=None)
    texts = [x["text"] for x in lines]
    assert any("一切正常" in t for t in texts), texts


def test_derive_activity_logs_on_failure(caplog):
    """数据源异常必须留日志可定位（r46 前是裸 pass）。"""
    with caplog.at_level("WARNING", logger="aetrix.panel"):
        panel_api._derive_activity(_BoomDB(), health=None)
    assert any("埋点表读取失败" in r.getMessage() for r in caplog.records)


# ---------------- F3：sa-status 诚实标记（静态） ----------------

def test_sa_status_flags_present():
    """gdrive_sa_status 返回体必须带 sa_state_ok / cache_stat_ok 标记。"""
    import inspect
    from backend.api import admin as admin_api
    src = inspect.getsource(admin_api.gdrive_sa_status)
    assert '"sa_state_ok"' in src and '"cache_stat_ok"' in src
    # 读失败分支必须记日志，不能再是裸 pass
    assert src.count("logger.warning") >= 2


# ---------------- F4：重置密码部分成功（静态口径） ----------------

def test_reset_password_returns_emby_synced_flag():
    """reset_user_password 响应必须带 emby_synced，失败时 message 诚实。"""
    import inspect
    from backend.api import admin as admin_api
    # 找到 reset 密码的路由函数（按函数名定位，避免行号漂移）
    fn = None
    for name in ("reset_user_password",):
        if hasattr(admin_api, name):
            fn = getattr(admin_api, name)
    assert fn is not None, "reset_user_password 路由函数不存在"
    src = inspect.getsource(fn)
    assert '"emby_synced"' in src
    assert "Emby 播放密码同步失败" in src


# ---------------- F5：_drive_403_error_reason 恢复 ----------------

def test_drive_403_error_reason_exists_and_parses():
    """函数必须存在（r46 前 def 行丢失导致 NameError，被外层吞成静默 None）。"""
    from backend.emby_server import streaming as st
    assert callable(st._drive_403_error_reason)
    reason, msg = st._drive_403_error_reason(
        b'{"error": {"errors": [{"reason": "rateLimitExceeded"}],'
        b' "message": "Rate exceeded."}}'
    )
    assert reason == "rateLimitExceeded"
    assert msg == "Rate exceeded."


def test_drive_403_error_reason_never_raises():
    """脏输入永不抛异常（契约：解析失败返回 ("", "")）。"""
    from backend.emby_server import streaming as st
    for bad in (b"", b"not json", b"[1,2]", b'{"error": null}',
                b'{"error": {"errors": "x"}}', None):
        assert st._drive_403_error_reason(bad) == ("", ""), bad


# ---------------- r46 补遗：播放/扫描/enrich 静默点 ----------------

def test_raw_nodes_corrupt_json_warns(caplog):
    """流节点配置写坏 → warning 日志 + 返回 []（原来静默 pass）。"""
    from backend.emby_server import stream_nodes as sn

    class _Row:
        value = "{bad json"

    class _Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return _Row()

    class _DB:
        def query(self, *a, **k):
            return _Q()

    with caplog.at_level("WARNING", logger="backend.emby_server.stream_nodes"):
        assert sn._raw_nodes(_DB()) == []
    assert any("流节点配置解析失败" in r.getMessage() for r in caplog.records)


def test_stream_accel_node_failure_warns(monkeypatch, caplog):
    """流节点改写抛异常 → warning（原来 debug，生产看不到）+ 回落本机。"""
    from backend.emby_server import stream_accel as sa
    from backend.emby_server import stream_nodes as sn

    def _boom(db, stream_url, base):
        raise RuntimeError("db gone")

    monkeypatch.setattr(sn, "rewrite_playback_urls", _boom)
    monkeypatch.setattr(sa, "get_effective_domain", lambda db: "")
    with caplog.at_level("WARNING", logger="backend.emby_server.stream_accel"):
        out = sa.rewrite_playback_urls_unified(
            None, "https://base/x.mp4", "https://base")
    assert out[1] is None  # 回落：无改写（新返回二元组 (url, source)，source 为 None）
    assert any("流节点改写跳过" in r.getMessage() for r in caplog.records)


def test_run_task_base_exception_marks_failed(monkeypatch):
    """BaseException 穿透 except Exception 时，finally 必须把 RUNNING 改成 FAILED。

    回归：部署重启的 KeyboardInterrupt 曾让任务永远卡"运行中"。
    """
    from backend.emby_server import scan_queue as sq

    class _FakeProgress:
        def begin_scan(self, *a, **k):
            pass

        def remote_stats(self):
            return {"lists": 0, "reused": 0}

        def end_scan(self, *a, **k):
            pass

    class _FakeLib:
        scan_status = "success"

    class _Q:
        def filter(self, *a, **k):
            return self

        def first(self):
            return _FakeLib()

    class _DB:
        expire_on_commit = True

        def query(self, *a, **k):
            return _Q()

        def close(self):
            pass

    def _boom(*a, **k):
        raise KeyboardInterrupt("shutdown")

    from backend.emby_server import scanner as _scanner
    monkeypatch.setattr(_scanner, "scan_library_sync", _boom)
    monkeypatch.setattr(sq, "SessionLocal", lambda: _DB())
    monkeypatch.setattr(sq, "progress", _FakeProgress())
    monkeypatch.setattr(sq, "clear_progress", lambda *a, **k: None)
    monkeypatch.setattr(sq, "_release_locked", lambda *a, **k: None)

    from datetime import datetime
    task = sq.ScanTask(library_id=1, name="t", trigger="manual")
    task.state = sq.STATE_RUNNING
    task.started_at = datetime.now()
    # KeyboardInterrupt 本就该继续向上传（工作线程要死），finally 只负责修状态
    with pytest.raises(KeyboardInterrupt):
        sq._run_task(task)
    assert task.state == sq.STATE_FAILED, task.state
    assert "中断" in (task.error or ""), task.error
    assert task.finished_at is not None


def test_maybe_queue_probe_failure_warns(monkeypatch, caplog):
    """probe 衔接入队抛异常 → warning + 不抛（原来静默 pass）。"""
    from backend.emby_server import enrich_worker as ew
    from backend.emby_server import scanner as _sc

    def _boom(*a, **k):
        raise RuntimeError("db gone")

    monkeypatch.setattr(_sc, "needs_probe", _boom)

    class _Item:
        id = 42
        file_path = "/x.mp4"
        size = 1

    with caplog.at_level("WARNING", logger="backend.emby_server.enrich_worker"):
        ew._maybe_queue_probe(None, _Item())  # 不抛即过
    assert any("probe 衔接入队失败" in r.getMessage() for r in caplog.records)


def test_apply_runtime_failure_logged():
    """apply_runtime 失败分支必须有日志（原来裸 pass 连日志都没有）。"""
    import inspect
    from backend.emby_server import enrich_worker as ew
    src = inspect.getsource(ew)
    i = src.find("apply_runtime as _apply_rt")
    assert i > 0
    block = src[i:i + 400]
    assert "logger.debug" in block or "logger.warning" in block, block


def test_pickcode_invalidate_failure_logged():
    """115 pickcode 缓存失效失败必须记日志（原来 except: pass）。"""
    import inspect
    from backend.emby_server import streaming as st
    src = inspect.getsource(st.serve_mount_relay_async)
    i = src.find("_invalidate_115_url")
    assert i > 0
    block = src[i:i + 300]
    assert "logger.warning" in block, block
