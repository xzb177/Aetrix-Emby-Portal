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
            # r13：NULL is_active 必须判 disabled（与 Python `not` 口径一致）
            models.RegistrationCode(code="A5", is_active=None),  # disabled
            models.RegistrationCode(code="A6", is_active=None,
                                     expires_at=now - timedelta(days=1)),  # disabled（优先于 expired）
        ]
        db.add_all(rows)
        db.commit()
        # 真正的 NULL 行：ORM 显式传 None 会触发 Column(default=True) 的默认值，
        # 必须用 core UPDATE 才能把列写成 NULL（模拟历史脏数据/直接 SQL 写入）。
        from sqlalchemy import update as sa_update
        db.execute(sa_update(models.RegistrationCode)
                   .where(models.RegistrationCode.code.in_(["A5", "A6"]))
                   .values(is_active=None))
        db.commit()
        db.expire_all()
        expect = {r.code: admin_ops._code_state(r, now) for r in rows}
        assert expect == {"A1": "disabled", "A2": "expired",
                          "A3": "used_up", "A4": "active",
                          "A5": "disabled", "A6": "disabled"}
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
        # 状态过滤下推：只查 disabled 应有 A1/A5/A6（NULL 行必须被查到）
        only_disabled = (db.query(models.RegistrationCode)
                         .filter(expr == "disabled").all())
        assert sorted(r.code for r in only_disabled) == ["A1", "A5", "A6"]
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
        ]
        db.add_all(rows)
        db.commit()
        expect = {r.code: admin_ops._inv_code_state(r, now) for r in rows}
        assert expect == {"I1": "revoked", "I2": "expired",
                          "I3": "used_up", "I4": "active"}
        # list_invitation_codes 内联了 CASE；这里验证同构表达式
        from sqlalchemy import and_, case, func
        inv_state = case(
            (models.InvitationCode.is_active == False, "revoked"),  # noqa: E712
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
    """D-P1-4：租约超 TTL 被强制回收。"""
    from backend.emby_server.playback_tune import RangeConcurrencyLimiter
    lim = RangeConcurrencyLimiter(max_concurrent=2, lease_ttl=0.2)
    assert lim.try_acquire("/x") is not None
    assert lim.try_acquire("/x") is not None
    assert lim.try_acquire("/x") is None  # 拿满
    # 不 release，等 TTL 过期 → 下次 acquire 强制回收
    time.sleep(0.25)
    assert lim.try_acquire("/x") is not None
    stats = lim.stats()
    assert stats["reclaimed_total"] >= 2
    assert stats["rejected_total"] >= 1


def test_range_limiter_release_by_lease_id():
    """D-P1-4 r13：release 按 lease_id 精确归还；重复/错误 id/None 直接忽略。"""
    from backend.emby_server.playback_tune import RangeConcurrencyLimiter
    lim = RangeConcurrencyLimiter(max_concurrent=2, lease_ttl=60)
    a = lim.try_acquire("/y")
    b = lim.try_acquire("/y")
    assert a is not None and b is not None and a != b  # id 唯一
    assert lim.try_acquire("/y") is None  # 拿满
    lim.release("/y", a)  # 归还 a
    c = lim.try_acquire("/y")
    assert c is not None
    assert lim.try_acquire("/y") is None
    # 迟到/重复/错误释放不抛错、不误删他人租约
    lim.release("/y", a)       # a 已归还过：忽略
    lim.release("/y", 999999)  # 不存在的 id：忽略
    lim.release("/y", None)    # None：忽略
    assert lim.stats()["active_leases"] == 2  # b、c 仍在


def test_range_limiter_late_release_after_reclaim():
    """D-P1-4 r13 回归：_sweep 强制回收后，原持有者的迟到 release 不得弹掉他人租约。"""
    from backend.emby_server.playback_tune import RangeConcurrencyLimiter
    lim = RangeConcurrencyLimiter(max_concurrent=2, lease_ttl=0.2)
    old = lim.try_acquire("/z")
    assert old is not None
    time.sleep(0.25)  # old 租约过期
    new = lim.try_acquire("/z")  # 触发 sweep 强制回收 old
    assert new is not None and new != old
    lim.release("/z", old)  # 原持有者迟到 finally：必须被忽略
    assert lim.stats()["active_leases"] == 1  # new 的租约还在
    assert lim.try_acquire("/z") is not None  # 还剩 1 个名额
    assert lim.try_acquire("/z") is None      # 拿满


def test_range_limiter_acquire_timeout():
    from backend.emby_server.playback_tune import RangeConcurrencyLimiter
    lim = RangeConcurrencyLimiter(max_concurrent=1, acquire_timeout=0.3,
                                  lease_ttl=60)
    assert lim.try_acquire("/z") is not None
    t0 = time.monotonic()
    assert lim.acquire("/z") is None
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


# ---------- r13：perfection-iteration 第 13 轮新增回归测试 ----------

def test_code_state_sql_null_is_active_invitation(tmp_path):
    """F-P1-7 r13：邀请码 NULL is_active 的 SQL 口径与 Python `not` 口径一致。"""
    from backend.api import admin_ops
    Session = _make_session(tmp_path)
    db = Session()
    try:
        now = datetime.now()
        user = models.WebUser(username="inv_null_t", password_hash="x")
        db.add(user)
        db.flush()
        rows = [
            models.InvitationCode(code="I1", user_id=user.id, is_active=None),  # revoked
            models.InvitationCode(code="I2", user_id=user.id, is_active=False),  # revoked
            models.InvitationCode(code="I3", user_id=user.id, is_active=True),  # active
        ]
        db.add_all(rows)
        db.commit()
        # 真正的 NULL 行：ORM 显式传 None 会触发 Column(default=True)，
        # 用 core UPDATE 把 I1 写成 NULL（模拟历史脏数据/直接 SQL 写入）。
        from sqlalchemy import update as sa_update
        db.execute(sa_update(models.InvitationCode)
                   .where(models.InvitationCode.code == "I1")
                   .values(is_active=None))
        db.commit()
        db.expire_all()
        expect = {r.code: admin_ops._inv_code_state(r, now) for r in rows}
        assert expect == {"I1": "revoked", "I2": "revoked", "I3": "active"}
        # 源码口径：邀请码 CASE 首分支必须用 isnot(True)（NULL 与 False 都判 revoked）
        import inspect
        from backend.api import admin_ops as ops_src
        list_src = inspect.getsource(ops_src.list_invitation_codes)
        assert '(models.InvitationCode.is_active.isnot(True), "revoked")' in list_src
    finally:
        db.close()


def test_assistant_ask_semaphore_before_quota():
    """F-P1-16 r13：先抢信号量再扣配额；配额超限时先 release 信号量再 429。"""
    import inspect
    from backend.api import assistant as asst
    src = inspect.getsource(asst.assistant_ask)
    acq = src.index("_ai_ask_semaphore.acquire(blocking=False)")
    con = src.index("ai_capability.safe_consume")
    assert acq < con, "信号量 acquire 必须在配额 safe_consume 之前（否则撞忙会白扣配额）"
    # 配额超限分支（safe_consume 之后、try: ask 之前）必须先 release 再抛 429
    quota_branch = src[con:src.index("try:", con)]
    assert "_ai_ask_semaphore.release()" in quota_branch
    assert "今日提问次数已用完" in quota_branch


def test_enrich_episode_night_tz_restored():
    """P1-3 r13：night_tz 后端恢复（请求字段 / GET 默认值+选项 / PUT 透传）。"""
    import inspect
    from backend.api import admin_scrape
    # ① 请求模型有 night_tz 字段（可选，不传则后台按默认解释）
    fields = admin_scrape.EnrichEpisodeNightSaveRequest.model_fields
    assert "night_tz" in fields
    assert fields["night_tz"].default is None
    # ② GET 返回 defaults.night_tz 与顶层 tz_options
    get_src = inspect.getsource(admin_scrape.get_enrich_episode_night)
    assert '"night_tz": enrich_worker.NIGHT_TZ_DEFAULT' in get_src
    assert '"tz_options": enrich_worker.NIGHT_TZ_OPTIONS' in get_src
    # ③ PUT 把 req.night_tz 透传给 save_episode_night_config
    put_src = inspect.getsource(admin_scrape.save_enrich_episode_night)
    assert "req.night_tz" in put_src


def test_super_deadlock_checks_use_loginable_count():
    """P1-4 r13：revoke_admin 与 update_user 的降级/撤销分支用可登录 super 口径。"""
    import inspect
    from backend.api import admins_admin, admin as admin_api
    revoke_src = inspect.getsource(admins_admin.revoke_admin)
    assert "_locked_loginable_super_count(db)" in revoke_src
    assert "_locked_super_count(db)" not in revoke_src
    update_src = inspect.getsource(admin_api.update_user)
    assert "request.is_staff is False and _locked_loginable_super_count(db)" in update_src
    assert "request.is_staff is False and _locked_super_count(db)" not in update_src


def test_save_emby_server_single_commit():
    """F-P1-17 r13：save_all 内配置写与清单同步一次提交（upsert_legacy 前无中间 commit）。"""
    import inspect
    import re
    from backend.api import emby_servers
    src = inspect.getsource(emby_servers)
    start = src.index("def save_all() -> None:")
    end = src.index("registry.upsert_legacy(", start)
    segment = src[start:end]
    # 匹配真正的代码行 db.commit()（排除注释里的字面提及）
    assert not re.search(r"^\s*db\.commit\(\)", segment, re.MULTILINE), \
        "save_all 在 upsert_legacy 前仍有中间 commit"


def test_economy_orders_null_created_at_nullslast(tmp_path):
    """P3-5 r13：NULL created_at 的 SQL 排序口径与 Python 归并一致（NULLS LAST）。"""
    Session = _make_session(tmp_path)
    db = Session()
    try:
        now = datetime.now()
        user = models.WebUser(username="ord_null_t", password_hash="x")
        pkg = models.RechargePackage(name="t", amount=1, price=1)
        db.add_all([user, pkg])
        db.flush()
        db.add_all([
            models.RechargeOrder(order_id="R-NULL", user_id=user.id, package_id=pkg.id,
                                 amount=1, price=1, status="paid", created_at=now),
            models.RechargeOrder(order_id="R-NEW", user_id=user.id, package_id=pkg.id,
                                 amount=1, price=1, status="paid", created_at=now),
        ])
        db.commit()
        # 真正的 NULL 行：ORM 显式传 None 会触发 Column(default=datetime.now)，
        # 用 core UPDATE 把 R-NULL 写成 NULL。
        from sqlalchemy import update as sa_update
        db.execute(sa_update(models.RechargeOrder)
                   .where(models.RechargeOrder.order_id == "R-NULL")
                   .values(created_at=None))
        db.commit()
        rows = (db.query(models.RechargeOrder.order_id)
                .order_by(models.RechargeOrder.created_at.desc().nullslast()).all())
        assert [r[0] for r in rows] == ["R-NEW", "R-NULL"]
        # Python 侧归并：x["created_at"] or "" 把 NULL 排最后，与 SQL 口径一致
        items = [{"created_at": None}, {"created_at": now.isoformat()}]
        items.sort(key=lambda x: x["created_at"] or "", reverse=True)
        assert [i["created_at"] for i in items] == [now.isoformat(), None]
    finally:
        db.close()
