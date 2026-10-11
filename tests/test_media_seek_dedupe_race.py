"""求片去重竞态回归测试。

根因：
    `_create_media_seek_sync` 里的「去重 SELECT」原本排在 `lock_user_row` 之前，是典型的锁外「先查后写」。
    同一用户双击 / 并发提交同一部片时：
      T1：去重查不到 → 拿锁 → 额度 OK → INSERT → commit
      T2：去重查不到（跑在 T1 提交之前）→ 在锁上等待 → T1 提交后拿到锁 → 额度仍有余额 → INSERT → commit
    结果两条一模一样的 pending 求片入库，409 去重承诺被绕过，两条都进管理审核、都占额度、都触发通知。

修复：
    把去重块整体下移到 `lock_user_row(db, user.id)` 之后，与每日 / 月度额度检查共用同一把用户行锁。
    串行化后，后到者的去重 SELECT 是拿到锁之后发出的新语句：PG READ COMMITTED 下能看到 T1 已提交的行
    → 抛 409；SQLite 下写锁同样保证串行。去重本就按 user_id 过滤，用用户行锁串行化粒度正好。

覆盖：
    a) test_dedupe_check_runs_inside_user_row_lock —— 事件顺序断言：locked 必须先于去重 SELECT（修前失败、修后通过）。
    b) test_dedupe_concurrent_pg —— 真 PG 双线程 barrier 对齐并发，恰好一个成功、一个 409，库里只 1 条 pending。
    c) test_dedupe_sequential_sqlite —— 串行语义回归，锁内去重没破坏正常路径、409 文案与跨片放行。

全部离线：不碰网络、不碰生产库（b 只在显式配置了 postgres 的 DATABASE_URL 时才跑，否则 skip）。
"""
import os
import sys
import threading
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, func
from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, ".")

from backend import media_seek, models  # noqa: E402
from backend.api import economy  # noqa: E402
from backend.api import user as user_api  # noqa: E402
from backend.integrations import store  # noqa: E402


# ---------------------------------------------------------------------------
# 夹具与辅助
# ---------------------------------------------------------------------------
@pytest.fixture
def db():
    """内存 SQLite 库 + 独立 Session（离线，单线程用）。"""
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    store.invalidate()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()
        store.invalidate()


def _user(db, username):
    """建一个最小可用的用户行。"""
    user = models.WebUser(username=username, password_hash="x", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _payload(**overrides):
    """构造一个求片请求体，默认是一部电影。"""
    data = {"movie_name": "星际穿越", "type": "movie"}
    data.update(overrides)
    return user_api.MediaSeekRequest(**data)


def _pg_url():
    """真 PG 连接串；没配或不是 postgres 就返回 None（由调用方 skip）。"""
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith("postgresql"):
        return None
    if url.startswith("postgresql://"):
        # 与 backend/database.py 同一口径：没装 psycopg(v3) 就显式走 psycopg2
        try:
            import psycopg  # noqa: F401
        except ImportError:
            url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


# ---------------------------------------------------------------------------
# a) 事件顺序：去重 SELECT 必须在用户行锁之后
# ---------------------------------------------------------------------------
def test_dedupe_check_runs_inside_user_row_lock(db, monkeypatch):
    """去重 SELECT 必须发生在 lock_user_row 之后（修前：顺序颠倒 → 断言失败）。

    做法：给 `backend.api.economy.lock_user_row` 和 `Session.query` 各加一个事件探针。
    `_create_media_seek_sync` 里是延迟导入（`from backend.api.economy import lock_user_row`），
    每次调用都重新从模块取属性，所以 monkeypatch 模块属性一定生效。
    本用例中断言的是「第一次查 MovieRequest 之前锁已经拿到了」：修前去重 SELECT 在锁外，
    修后去重与额度统计的 SELECT 都在锁内。
    """
    events = []

    # 1) 用户行锁 spy：记一笔后调用原实现，行为完全不变
    real_lock = economy.lock_user_row

    def spy_lock(session, user_id):
        events.append("locked")
        return real_lock(session, user_id)

    monkeypatch.setattr(economy, "lock_user_row", spy_lock)

    # 2) Session.query spy：只要查 MovieRequest 就记一笔（不拦截其它查询，也不改行为）
    real_query = Session.query

    def spy_query(self, entity, *args, **kwargs):
        try:
            if isinstance(entity, type) and issubclass(entity, models.MovieRequest):
                events.append("movie_request_queried")
        except TypeError:  # 非类实体（列、函数等）直接放过
            pass
        return real_query(self, entity, *args, **kwargs)

    monkeypatch.setattr(Session, "query", spy_query)

    user = _user(db, f"dedupe-order-{uuid.uuid4().hex[:8]}")
    req_id = user_api._create_media_seek_sync(db, user, "星际穿越", _payload())
    assert isinstance(req_id, int) and req_id > 0

    assert "locked" in events, f"没有观察到 lock_user_row 调用：{events}"
    assert "movie_request_queried" in events, f"没有观察到 MovieRequest 查询：{events}"
    assert events.index("locked") < events.index("movie_request_queried"), (
        f"MovieRequest 查询跑在了用户行锁之前，并发下 409 去重可被绕过：{events}"
    )


# ---------------------------------------------------------------------------
# b) 真 PG 双线程竞态：恰好一个 409
# ---------------------------------------------------------------------------
def test_dedupe_concurrent_pg():
    """两个线程 barrier 对齐后同时提交同一部片：修后恰好一个成功、一个 409，库里只 1 条 pending。"""
    url = _pg_url()
    if not url:
        pytest.skip("未配置 PostgreSQL 的 DATABASE_URL，跳过真并发竞态测试")

    engine = create_engine(url, pool_size=5, max_overflow=5)
    SessionLocal = sessionmaker(bind=engine)
    store.invalidate()

    # 用 uuid 后缀避免跨运行残留干扰
    tag = uuid.uuid4().hex[:8]
    name = f"并发去重-{tag}"

    setup = SessionLocal()
    try:
        user_id = _user(setup, f"pg-race-{tag}").id
    finally:
        setup.close()

    barrier = threading.Barrier(2)
    results = []
    guard = threading.Lock()

    def worker():
        session = SessionLocal()
        try:
            user = session.get(models.WebUser, user_id)
            barrier.wait(timeout=10)  # 对齐后同时冲
            req_id = user_api._create_media_seek_sync(session, user, name, _payload(movie_name=name))
            with guard:
                results.append(("ok", req_id))
        except HTTPException as exc:
            session.rollback()
            with guard:
                results.append(("http", exc.status_code))
        except Exception as exc:  # noqa: BLE001 - 线程里的意外异常也要带回主线程断言
            session.rollback()
            with guard:
                results.append(("error", repr(exc)))
        finally:
            session.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    ok = [r for r in results if r[0] == "ok"]
    conflicts = [r for r in results if r[0] == "http" and r[1] == 409]
    errors = [r for r in results if r[0] == "error"]

    assert not errors, f"并发线程出现意外异常：{errors}"
    assert len(ok) == 1, f"期望恰好一个成功，实际 {results}"
    assert len(conflicts) == 1, f"期望恰好一个 409，实际 {results}"

    check = SessionLocal()
    try:
        rows = check.query(models.MovieRequest).filter(
            models.MovieRequest.user_id == user_id,
            func.lower(models.MovieRequest.movie_name) == name.lower(),
            models.MovieRequest.status.in_(media_seek.ACTIVE_STATUSES),
        ).count()
    finally:
        check.close()

    assert rows == 1, f"库里该用户该片名应只有 1 条处理中的求片，实际 {rows}"

    store.invalidate()
    engine.dispose()


# ---------------------------------------------------------------------------
# c) SQLite 串行语义：正常路径没被锁内去重搞坏
# ---------------------------------------------------------------------------
def test_dedupe_sequential_sqlite(db):
    """同名第二次 409（文案不变），不同片名照常放行，锁内去重没误伤正常路径。"""
    user = _user(db, f"dedupe-seq-{uuid.uuid4().hex[:8]}")

    first_id = user_api._create_media_seek_sync(db, user, "星际穿越", _payload())
    assert isinstance(first_id, int) and first_id > 0

    with pytest.raises(HTTPException) as excinfo:
        user_api._create_media_seek_sync(db, user, "星际穿越", _payload())
    assert excinfo.value.status_code == 409
    assert "已在处理中" in str(excinfo.value.detail)
    # 模拟调用方：校验失败后回滚，释放行锁（生产里由线程池 wrapper 负责）
    db.rollback()

    # 换一部片：去重不该挡（锁内检查是按 name + season 过滤的）
    other_id = user_api._create_media_seek_sync(db, user, "盗梦空间", _payload(movie_name="盗梦空间"))
    assert other_id != first_id

    rows = db.query(models.MovieRequest).filter(models.MovieRequest.user_id == user.id).count()
    assert rows == 2
