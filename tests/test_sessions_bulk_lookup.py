"""回归：compat_routes.get_sessions 的 N+1 批量化修复。

此前 /Sessions（compat_routes.get_sessions）在结果循环里逐条查 MediaItem 与
WebUser：N 条在线会话就是 2N 条 SQL。本轮改为 sessions + items + users 共 3 条
批量 IN 查询，本文件用 before_cursor_execute 事件监听数 SQL 条数并断言 ≤4
（留 1 条方言余量），同时验证行为与旧实现逐字一致：
- 缺 item 或缺 user 的会话跳过，不入结果；
- 非 staff 只看自己的，staff 看全站；
- 按 last_update_at 倒序，DTO 字段 Id / UserId / UserName / NowPlayingItem.Name 不变；
- 没有活跃会话时只发 1 条 sessions 查询，不发多余的 items/users 批量查询
  （空集合不给空 IN 列表）。
建库方式与 tests/test_play_auth_uniform.py 相同：sqlite 内存库直接建 Session +
两套模型表（backend.models 与 backend.emby_server.models）。
"""
from datetime import datetime, timedelta

import pytest
from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

import backend.security as sec

sec.SECRET_KEY = "test-secret-key-for-sessions-bulk-0000"

from backend import models
from backend.emby_server import compat_routes
from backend.emby_server import models as em

_T0 = datetime(2026, 1, 1)


@pytest.fixture()
def bulk_env():
    """sqlite 内存库：5 用户 / 5 条目 / 5 条活跃会话 + 缺 item、缺 user 各 1 条。"""
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    # expire_on_commit=False：避免过期属性刷新的 SELECT 混进 SQL 计数
    db = sessionmaker(bind=engine, expire_on_commit=False)()

    lib = em.Library(guid="lib-sess-bulk", name="电影", collection_type="movies",
                     is_enabled=True)
    db.add(lib)
    db.flush()

    # 5 个用户：users[0] 为 staff（调用方），其余为普通用户
    users = [
        models.WebUser(username=f"sessbulk{i}", password_hash="x",
                       is_active=True, is_staff=(i == 0))
        for i in range(5)
    ]
    db.add_all(users)
    items = [
        em.MediaItem(guid=f"g-sess-bulk-{i}", name=f"群像电影{i}",
                     item_type="movie", library_id=lib.id)
        for i in range(5)
    ]
    db.add_all(items)
    db.flush()

    # 5 条完好的活跃会话：users[i] 在看 items[i]，last_update_at 递增保证排序可断言
    good = []
    for i in range(5):
        s = em.PlaybackSession(
            session_key=f"sk-good-{i}", user_id=users[i].id, item_id=items[i].id,
            start_time=_T0, last_update_at=_T0 + timedelta(minutes=i),
        )
        db.add(s)
        good.append((s, users[i], items[i]))

    # 两条应被跳过的会话：sk-gone-item 的条目行、sk-gone-user 的用户行随后硬删，
    # 会话行本身保持未结束（ended_at 为空）——两方查不到行就必须跳过
    gone_user = models.WebUser(username="sessbulk-gone", password_hash="x",
                               is_active=True)
    gone_item = em.MediaItem(guid="g-sess-bulk-gone", name="已删电影",
                             item_type="movie", library_id=lib.id)
    db.add_all([gone_user, gone_item])
    db.flush()
    db.add(em.PlaybackSession(session_key="sk-gone-item", user_id=users[1].id,
                              item_id=gone_item.id, last_update_at=_T0))
    db.add(em.PlaybackSession(session_key="sk-gone-user", user_id=gone_user.id,
                              item_id=items[0].id, last_update_at=_T0))
    db.commit()

    db.delete(gone_user)
    db.delete(gone_item)
    db.commit()
    return db, engine, good


def _get_with_count(engine, db, caller):
    """跑一次 get_sessions，返回 (结果, 执行期间的 SQL 语句列表)。"""
    queries = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        queries.append(statement)

    req = Request({"type": "http", "method": "GET", "scheme": "http",
                   "path": "/emby/Sessions", "query_string": b"", "headers": [],
                   "client": ("127.0.0.1", 5000), "server": ("t", 80)})
    event.listen(engine, "before_cursor_execute", _count)
    try:
        result = compat_routes.get_sessions(req, db, caller)
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    return result, queries


def test_get_sessions_bulk_lookup(bulk_env):
    """staff 调用：≤4 条 SQL 完成，跳过语义 / 鉴权范围 / 排序 / DTO 与旧实现一致。"""
    db, engine, good = bulk_env
    staff = good[0][1]

    # ---- staff 看全站：5 条完好会话，SQL 共 3 条（sessions + items + users）----
    result, queries = _get_with_count(engine, db, staff)

    assert len(result) == 5, f"应返回 5 条 DTO，实测 {len(result)}"
    by_id = {dto["Id"]: dto for dto in result}
    assert set(by_id) == {s.session_key for s, _owner, _item in good}
    assert "sk-gone-item" not in by_id, "缺条目的会话必须被跳过"
    assert "sk-gone-user" not in by_id, "缺用户的会话必须被跳过"

    assert len(queries) <= 4, f"应 ≤4 条 SQL，实测 {len(queries)}: {queries}"

    # 排序不变：last_update_at 倒序，最新的 sk-good-4 在前
    assert [dto["Id"] for dto in result] == [f"sk-good-{i}" for i in range(4, -1, -1)]

    # DTO 字段与旧的逐条实现一致
    for s, owner, item in good:
        dto = by_id[s.session_key]
        assert dto["UserId"] == str(owner.id)
        assert dto["UserName"] == owner.username
        assert dto["NowPlayingItem"]["Name"] == item.name

    # ---- 非 staff 只看自己：users[1] 完好会话仅 sk-good-1（sk-gone-item 被跳过）----
    non_staff = good[1][1]
    assert non_staff.is_staff is False
    mine, mine_queries = _get_with_count(engine, db, non_staff)
    assert [dto["Id"] for dto in mine] == ["sk-good-1"]
    assert len(mine_queries) <= 4, mine_queries

    # ---- 无活跃会话：只发 1 条 sessions 查询，不发 items/users 的批量 IN 查询 ----
    db.query(em.PlaybackSession).update(
        {em.PlaybackSession.ended_at: _T0}, synchronize_session=False)
    db.commit()
    empty, empty_queries = _get_with_count(engine, db, staff)
    assert empty == []
    assert len(empty_queries) == 1, empty_queries
    assert not any("emby_items" in q or "web_users" in q for q in empty_queries), \
        empty_queries
