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
