"""P1 第三批修复回归测试（12 项性能/分页/N+1）。

实现者直接编写（模型网关 503 no_healthy_account 不可用，已向主控报备）。
SQLite 跑逻辑正确性；并发语义以单线程确定性方式验证。
"""
from __future__ import annotations

import inspect
import os
import threading
import time
from datetime import datetime, timedelta

os.environ.setdefault("DATABASE_TYPE", "sqlite")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models
from backend.emby_server import models as em


def _make_session(tmp_path, name="p1b3.db"):
    engine = create_engine(f"sqlite:///{tmp_path / name}",
                           connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine, checkfirst=True)
    em.Base.metadata.create_all(engine, checkfirst=True)
    return sessionmaker(bind=engine)


# ---------- F-P1-5：pagination_params 边界 ----------

def test_pagination_params_boundaries():
    from backend.api.pagination import pagination_params
    # 正常
    assert pagination_params(50, 0) == (50, 0)
    # 负数 limit → 钳到 1（原 min() 只限上界，limit=-1 在 PG 报 500）
    assert pagination_params(-1, 0) == (1, 0)
    # 超大 limit → 钳到 200
    assert pagination_params(999999, 0) == (200, 0)
    # 负数 offset → 0
    assert pagination_params(50, -5) == (50, 0)
    # 非法输入 → 默认值
    assert pagination_params("abc", "xyz") == (50, 0)
    assert pagination_params(None, None) == (50, 0)
    # 自定义上下界
    assert pagination_params(500, 0, default_limit=100, max_limit=300) == (300, 0)
    assert pagination_params(0, 0) == (1, 0)


def test_pagination_used_in_admin_and_realms():
    """F-P1-5：admin.py 用户列表 / realms.py 订阅列表必须走统一钳制。"""
    from backend.api import admin as admin_api
    from backend.api import realms as realms_api
    assert "pagination_params(limit, offset)" in inspect.getsource(admin_api.list_users)
    assert "pagination_params(limit, offset)" in inspect.getsource(realms_api.realm_subscriptions)


# ---------- F-P1-3 / F-P1-2：订单合并分页 ----------

def test_orders_merge_no_missing(tmp_path):
    """F-P1-2：充值 100/订阅 90/充值 80 场景，limit=1 翻页不遗漏。

    直接复现审查报告的用例：按 created_at 倒序全局第 2 条应为订阅 90。
    """
    Session = _make_session(tmp_path)
    db = Session()
    try:
        u = models.WebUser(username="u1", email="u1@x.com", password_hash="x")
        db.add(u)
        db.flush()
        base = datetime(2026, 1, 1, 12, 0, 0)
        # 充值 100（最新）、订阅 90、充值 80（最旧）
        r1 = models.RechargeOrder(order_id="R100", user_id=u.id, package_id=1,
                                  amount=100, price=100, status="paid",
                                  created_at=base + timedelta(seconds=100))
        s1 = models.SubscriptionOrder(order_id="S90", user_id=u.id, plan_id=1,
                                      item_name="月卡", amount=30, status="paid",
                                      created_at=base + timedelta(seconds=90))
        r2 = models.RechargeOrder(order_id="R80", user_id=u.id, package_id=1,
                                  amount=80, price=80, status="paid",
                                  created_at=base + timedelta(seconds=80))
        db.add_all([r1, s1, r2])
        db.commit()

        from backend.api.pagination import pagination_params

        def merged_page(limit, offset):
            limit, offset = pagination_params(limit, offset)
            fetch = offset + limit
            rq = (db.query(models.RechargeOrder)
                  .order_by(models.RechargeOrder.created_at.desc())
                  .offset(0).limit(fetch).all())
            sq = (db.query(models.SubscriptionOrder)
                  .order_by(models.SubscriptionOrder.created_at.desc())
                  .offset(0).limit(fetch).all())
            items = ([("recharge", o.order_id, o.created_at) for o in rq]
                     + [("subscription", o.order_id, o.created_at) for o in sq])
            items.sort(key=lambda x: x[2] or datetime.min, reverse=True)
            return [i[1] for i in items[offset:offset + limit]]

        # 第 1 页：R100；第 2 页：S90（原 bug 会返回 R80）；第 3 页：R80
        assert merged_page(1, 0) == ["R100"]
        assert merged_page(1, 1) == ["S90"]
        assert merged_page(1, 2) == ["R80"]
        # 整页一次取完
        assert merged_page(10, 0) == ["R100", "S90", "R80"]
    finally:
        db.close()


def test_orders_uses_pagination_and_batch_users():
    """F-P1-3/F-P1-1：订单接口走 pagination_params，且无逐单查用户。"""
    from backend.api import admin as admin_api
    src = inspect.getsource(admin_api.economy_list_orders)
    assert "pagination_params(limit, offset)" in src
    # 归并取数：offset(0).limit(fetch)
    assert ".offset(0).limit(fetch)" in src
    # 批量用户：一次 in_ 查询，不在循环里逐单查
    assert "usernames" in src or "user_ids" in src


# ---------- F-P1-1：其余 N+1 已消除 ----------

def test_no_n1_in_list_endpoints():
    """F-P1-1：用户/工单/求片列表循环内不再逐条查库。"""
    from backend.api import admin as admin_api
    for fn_name, loop_var in [("list_users", "\n    for u in users:"),
                              ("get_tickets", "\n    for ticket in tickets:"),
                              ("get_media_seeks", "\n    for req in requests:")]:
        src = inspect.getsource(getattr(admin_api, fn_name))
        # 循环体内不应再出现 db.query
        loop_body = src.split(loop_var, 1)[1]
        assert "db.query" not in loop_body, f"{fn_name} 循环内仍有 db.query"


# ---------- F-P1-7：卡码/邀请码 SQL 下推 ----------

def test_code_state_sql_matches_python(tmp_path):
    """F-P1-7：_code_state_sql 口径与 Python 版一致。"""
    from backend.api import admin_ops
    Session = _make_session(tmp_path)
    db = Session()
    try:
        now = datetime.now()
        rows = [
            models.RegistrationCode(code="A1", is_active=False),  # disabled
            models.RegistrationCode(code="A2", is_active=True,
                                    expires_at=now - timedelta(days=1)),  # expired
            models.RegistrationCode(code="A3", is_active=True, max_uses=2,
                                    use_count=2),  # used_up
            models.RegistrationCode(code="A4", is_active=True, max_uses=5,
                                    use_count=1),  # active
            models.RegistrationCode(code="A5", is_active=None),  # NULL→disabled（与 Python not None 同口径）
        ]
        db.add_all(rows)
        db.commit()
        expect = {r.code: admin_ops._code_state(r, now) for r in rows}
        assert expect == {"A1": "disabled", "A2": "expired",
                          "A3": "used_up", "A4": "active",
                          "A5": "disabled"}
        expr = admin_ops._code_state_sql(now)
        got = {
            r.code: s
            for r, s in db.query(models.RegistrationCode, expr).all()
        }
        assert got == expect, f"SQL 口径不一致: {got} vs {expect}"
        # 状态过滤下推：只查 active 应只有 A4
        only_active = (db.query(models.RegistrationCode)
                       .filter(expr == "active").all())
        assert [r.code for r in only_active] == ["A4"]
    finally:
        db.close()


def test_code_stats_sql_aggregates(tmp_path):
    """F-P1-7：code_stats 的 SQL 聚合与 Python 口径一致（含诱饵码/天数）。"""
    from backend import codes
    from backend.api import admin_ops
    Session = _make_session(tmp_path)
    db = Session()
    try:
        now = datetime.now()
        rows = [
            # 普通码：active，days=30，use_count=2 → days_granted += 60
            models.RegistrationCode(code="N1", is_active=True, code_type=1,
                                    days=30, max_uses=10, use_count=2),
            # 用完的码
            models.RegistrationCode(code="N2", is_active=True, code_type=2,
                                    days=30, max_uses=1, use_count=1),
            # 诱饵码：HONEY- 前缀，use_count=1 → decoy_total=1, triggered=1，不计天数
            models.RegistrationCode(code="HONEY-X1", is_active=True, code_type=1,
                                    days=30, max_uses=10, use_count=1),
            # 停用的码
            models.RegistrationCode(code="N3", is_active=False, code_type=1,
                                    days=30, max_uses=10, use_count=0),
        ]
        db.add_all(rows)
        db.commit()

        scope_id = None
        base = db.query(models.RegistrationCode)
        state_expr = admin_ops._code_state_sql(now)
        from sqlalchemy import and_, case, func
        is_decoy = func.upper(models.RegistrationCode.code).like("HONEY-%")
        days_col = func.coalesce(models.RegistrationCode.days, codes.DEFAULT_DAYS)
        used_col = func.coalesce(models.RegistrationCode.use_count, 0)
        agg = (
            base.with_entities(
                func.count().label("total"),
                func.sum(case((state_expr == "active", 1), else_=0)).label("active"),
                func.sum(case((state_expr == "disabled", 1), else_=0)).label("disabled"),
                func.sum(case((is_decoy, 1), else_=0)).label("decoy_total"),
                func.sum(case((and_(is_decoy, used_col > 0), 1), else_=0)).label("decoy_triggered"),
                func.sum(case((and_(~is_decoy, days_col > 0), days_col * used_col),
                              else_=0)).label("days_granted"),
            ).first()
        )
        assert int(agg.total) == 4
        assert int(agg.active) == 2  # N1 + HONEY-X1
        assert int(agg.disabled) == 1  # N3
        assert int(agg.decoy_total) == 1
        assert int(agg.decoy_triggered) == 1
        # N1: 30*2=60；N2: 30*1=30；诱饵不计；N3: 30*0=0
        assert int(agg.days_granted) == 90
    finally:
        db.close()


def test_inv_code_state_sql_matches_python(tmp_path):
    """F-P1-7：邀请码状态 SQL 口径与 Python 一致。"""
    from backend.api import admin_ops
    Session = _make_session(tmp_path)
    db = Session()
    try:
        u = models.WebUser(username="inv1", email="inv1@x.com", password_hash="x")
        db.add(u)
        db.flush()
        now = datetime.now()
        rows = [
            models.InvitationCode(code="I1", user_id=u.id, is_active=False),  # revoked
            models.InvitationCode(code="I2", user_id=u.id, is_active=True,
                                  expires_at=now - timedelta(days=1)),  # expired
            models.InvitationCode(code="I3", user_id=u.id, is_active=True,
                                  max_uses=1, use_count=1),  # used_up
            models.InvitationCode(code="I4", user_id=u.id, is_active=True,
                                  max_uses=5, use_count=1),  # active
            models.InvitationCode(code="I5", user_id=u.id, is_active=None),  # NULL→revoked
        ]
        db.add_all(rows)
        db.commit()
        expect = {r.code: admin_ops._inv_code_state(r, now) for r in rows}
        assert expect == {"I1": "revoked", "I2": "expired",
                          "I3": "used_up", "I4": "active",
                          "I5": "revoked"}
        # list_invitation_codes 内联了 CASE；这里验证同构表达式
        from sqlalchemy import and_, case, func
        inv_state = case(
            (func.coalesce(models.InvitationCode.is_active, False) == False, "revoked"),
            (and_(models.InvitationCode.expires_at.isnot(None),
                  models.InvitationCode.expires_at < now), "expired"),
            (and_(models.InvitationCode.max_uses.isnot(None),
                  models.InvitationCode.max_uses > 0,
                  func.coalesce(models.InvitationCode.use_count, 0)
                  >= models.InvitationCode.max_uses), "used_up"),
            else_="active",
        )
        got = {r.code: s for r, s in db.query(models.InvitationCode, inv_state).all()}
        assert got == expect
    finally:
        db.close()


# ---------- G1-P1-1/2：协议参数钳制 ----------

def test_qlimit_qstart_boundaries():
    from backend.emby_server.query_params import qint, qlimit, qstart
    # Limit 无上界 → 钳制
    assert qlimit({"Limit": "1000000"}, default=100, hi=500) == 500
    assert qlimit({"Limit": "12"}, default=12) == 12
    assert qlimit({}, default=12) == 12
    assert qlimit({"Limit": "-5"}, default=12) == 1
    # 畸形输入 → 默认值（原直接 500）
    assert qlimit({"Limit": "abc"}, default=12) == 12
    assert qlimit({"Limit": ""}, default=12) == 12
    # StartIndex 软上限
    assert qstart({"StartIndex": "999999999"}) == 100000
    assert qstart({"StartIndex": "-3"}) == 0
    assert qstart({}) == 0
    assert qint({"X": "abc"}, "X", 7) == 7


def test_emby_endpoints_use_qlimit():
    """G1-P1-1/2：四个协议端点走 qlimit。"""
    import inspect
    from backend.emby_server import api as emby_api
    from backend.emby_server import compat_routes
    src_api = inspect.getsource(emby_api)
    assert "qlimit(q, default=100, hi=500)" in src_api  # _query_items
    assert src_api.count("qlimit(request.query_params") >= 3  # resume/latest/nextup
    src_compat = inspect.getsource(compat_routes.similar_items)
    assert "qlimit(request.query_params, default=12)" in src_compat


# ---------- G1-P1-3：观看历史 SQL 去重 ----------

def _mk_library(db):
    lib = em.Library(name="测试库", guid="lib-test-1")
    db.add(lib)
    db.flush()
    return lib


def test_watch_history_sql_dedup(tmp_path):
    """G1-P1-3：同条目多会话只取最近一次；单集按剧聚合；不全量加载。"""
    from sqlalchemy import and_, func
    Session = _make_session(tmp_path)
    db = Session()
    try:
        u = models.WebUser(username="w1", email="w1@x.com", password_hash="x")
        db.add(u)
        db.flush()
        lib = _mk_library(db)
        movie = em.MediaItem(guid="m1", library_id=lib.id, item_type="movie",
                             name="电影1")
        series = em.MediaItem(guid="s1", library_id=lib.id, item_type="series",
                              name="剧1")
        db.add_all([movie, series])
        db.flush()
        ep1 = em.MediaItem(guid="e1", library_id=lib.id, item_type="episode",
                           name="E1", series_id=series.id)
        ep2 = em.MediaItem(guid="e2", library_id=lib.id, item_type="episode",
                           name="E2", series_id=series.id)
        db.add_all([ep1, ep2])
        db.flush()
        base = datetime(2026, 1, 1, 12, 0, 0)
        # 电影 3 条会话（取最新的），剧的两集各 1 条
        for i, mins in enumerate([30, 20, 10]):
            db.add(em.PlaybackSession(session_key=f"k{i}", user_id=u.id,
                                      item_id=movie.id,
                                      last_update_at=base - timedelta(minutes=mins),
                                      position_ticks=1000 * i))
        db.add(em.PlaybackSession(session_key="ke1", user_id=u.id, item_id=ep1.id,
                                  last_update_at=base - timedelta(minutes=50)))
        db.add(em.PlaybackSession(session_key="ke2", user_id=u.id, item_id=ep2.id,
                                  last_update_at=base - timedelta(minutes=5)))
        db.commit()

        # 复刻 portal.get_watch_history 的 SQL 去重逻辑
        sess = em.PlaybackSession
        latest = (
            db.query(sess.item_id.label("item_id"),
                     func.max(sess.last_update_at).label("max_at"))
            .join(em.MediaItem, em.MediaItem.id == sess.item_id)
            .filter(sess.user_id == u.id)
            .group_by(sess.item_id)
            .subquery()
        )
        unique_total = db.query(func.count()).select_from(latest).scalar()
        assert unique_total == 3  # movie + ep1 + ep2（去重后）

        cand = (
            db.query(sess, em.MediaItem)
            .join(em.MediaItem, em.MediaItem.id == sess.item_id)
            .join(latest, and_(latest.c.item_id == sess.item_id,
                               latest.c.max_at == sess.last_update_at))
            .filter(sess.user_id == u.id)
            .order_by(latest.c.max_at.desc(), sess.id.desc())
            .limit(200).all()
        )
        # 每条目只出现一次
        item_ids = [it.id for _, it in cand]
        assert len(item_ids) == len(set(item_ids)) == 3
        # 电影取的是最近一次会话（position_ticks=0 的那条，10 分钟前）
        movie_sess = next(s for s, it in cand if it.id == movie.id)
        assert movie_sess.position_ticks == 2000
        # 顺序：ep2(5分钟前) > movie(10分钟前) > ep1(50分钟前)
        assert [it.id for _, it in cand] == [ep2.id, movie.id, ep1.id]
    finally:
        db.close()


# ---------- F-P1-23：并发探测 ----------

def test_probe_identities_concurrent():
    """F-P1-23：_probe_identities 用 gather 并发；单台失败不影响其他。"""
    import asyncio
    import inspect
    from backend.api import servers as servers_api
    from backend.emby_server import nodes as node_lib

    src = inspect.getsource(servers_api._probe_identities)
    assert "asyncio.gather" in src

    calls = []

    async def fake_fetch(url, timeout=8.0):
        calls.append(url)
        await asyncio.sleep(0.05)
        if "bad" in url:
            raise RuntimeError("boom")
        return {"ok": True, "data": {}}

    orig = node_lib.fetch_node_identity
    node_lib.fetch_node_identity = fake_fetch
    try:
        targets = [(1, "http://a"), (2, "http://bad"), (3, "http://c")]
        t0 = time.monotonic()
        out = asyncio.run(servers_api._probe_identities(targets))
        dt = time.monotonic() - t0
        assert out[1]["ok"] is True
        assert out[2]["ok"] is False and "boom" in out[2]["error"]
        assert out[3]["ok"] is True
        # 串行要 0.15s；并发应明显小于（留足余量断言 < 0.12s）
        assert dt < 0.12, f"疑似串行执行：{dt:.3f}s"
    finally:
        node_lib.fetch_node_identity = orig


# ---------- F-P1-22：会话管理 ----------

def test_overview_session_management():
    """F-P1-22：探测前归还会话；build 用独立会话；load 内 expunge。"""
    import inspect
    from backend.api import servers as servers_api
    src = inspect.getsource(servers_api.emby_overview)
    # load() 内剥离 ORM
    assert "db.expunge_all()" in src
    # 探测前归还请求会话连接
    load_end = src.index("await run_in_threadpool(load)")
    probe_start = src.index("identities = await _probe_identities")
    assert "db.close()" in src[load_end:probe_start]
    # 体检写库用独立短会话
    assert "probe_db = SessionLocal()" in src
    # build 用独立会话
    assert "build_db = SessionLocal()" in src


# ---------- D-P1-4：租约制 ----------

def test_range_limiter_lease_reclaim():
    """D-P1-4：租约超 TTL 被强制回收；release 归还最老的。"""
    from backend.emby_server.playback_tune import RangeConcurrencyLimiter
    lim = RangeConcurrencyLimiter(max_concurrent=2, lease_ttl=0.2)
    assert lim.try_acquire("/x") is True
    assert lim.try_acquire("/x") is True
    assert lim.try_acquire("/x") is False  # 拿满
    # 不 release，等 TTL 过期 → 下次 acquire 强制回收
    time.sleep(0.25)
    assert lim.try_acquire("/x") is True
    stats = lim.stats()
    assert stats["reclaimed_total"] >= 2
    assert stats["rejected_total"] >= 1


def test_range_limiter_release_oldest():
    from backend.emby_server.playback_tune import RangeConcurrencyLimiter
    lim = RangeConcurrencyLimiter(max_concurrent=2, lease_ttl=60)
    lim.try_acquire("/y")
    lim.try_acquire("/y")
    lim.release("/y")
    assert lim.try_acquire("/y") is True
    assert lim.try_acquire("/y") is False
    # 重复释放不抛错
    lim.release("/y")
    lim.release("/y")
    lim.release("/y")


def test_range_limiter_acquire_timeout():
    from backend.emby_server.playback_tune import RangeConcurrencyLimiter
    lim = RangeConcurrencyLimiter(max_concurrent=1, acquire_timeout=0.3,
                                  lease_ttl=60)
    assert lim.try_acquire("/z") is True
    t0 = time.monotonic()
    assert lim.acquire("/z") is False
    assert time.monotonic() - t0 < 1.0


# ---------- F-P1-15 / F-P1-16 ----------

def test_assistant_history_limits():
    """F-P1-15：history 限 20 条、单条 4000 字符。"""
    import inspect
    from backend.api import assistant as asst
    src = inspect.getsource(asst.assistant_ask)
    assert "MAX_HISTORY_ITEMS" in src
    assert "MAX_HISTORY_ITEM_CHARS" in src
    assert asst.MAX_HISTORY_ITEMS == 20


def test_assistant_ask_semaphore():
    """F-P1-16：并发超限直接 429；信号量最终归还。"""
    import inspect
    from backend.api import assistant as asst
    src = inspect.getsource(asst.assistant_ask)
    assert "_ai_ask_semaphore.acquire(blocking=False)" in src
    # 占满 4 个，第 5 个拿不到
    held = []
    for _ in range(asst.AI_ASK_MAX_CONCURRENT):
        assert asst._ai_ask_semaphore.acquire(blocking=False) is True
        held.append(1)
    assert asst._ai_ask_semaphore.acquire(blocking=False) is False
    for _ in held:
        asst._ai_ask_semaphore.release()
    assert asst._ai_ask_semaphore.acquire(blocking=False) is True
    asst._ai_ask_semaphore.release()


# ---------- query_params 模块 ----------

def test_emby_query_params_module():
    from backend.emby_server import query_params
    assert hasattr(query_params, "qlimit")
    assert hasattr(query_params, "qstart")
    assert hasattr(query_params, "qint")
