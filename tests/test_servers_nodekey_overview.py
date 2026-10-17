"""Emby 总览（/overview）NODE_KEY 认领一致性判定的回归测试。

回归对象：`backend/api/servers.py::emby_overview` 曾拿
`servers.serialize()` 打码后的 `"已配置"`/`""` 去和 EA 上报的真 key 做相等
比较（P2 安全打码是有意为之，不许改），导致生产天天误报
「EA 上报的 NODE_KEY「ea-01」与面板登记的「已配置」不一致」。

判定逻辑已抽成纯函数 `_node_key_claim_warnings`，此处钉住它的全部语义；
另用 `servers.serialize` 钉住打码口径（保证修复不会把打码顺手改掉）。

用隔离的内存 SQLite，不碰生产库。
"""
import pytest


@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend import models
    from backend.integrations import store

    store.invalidate()
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        store.invalidate()


def _warn(**kwargs):
    from backend.api import servers as servers_api

    return servers_api._node_key_claim_warnings(**kwargs)


# ---------- 主回归：生产场景不再误报 ----------

def test_match_and_claimed_no_warning():
    """登记 ea-01、EA 上报 ea-01 且认领成功：零告警（曾误报的就是这个组合）。"""
    assert _warn(reported_key="ea-01", claimed=True, registered_key="ea-01") == []


def test_masked_value_never_matches_raw():
    """模拟旧 bug 的输入：若有人把打码值「已配置」传进来，仍要报出不一致（别反过来）。"""
    warns = _warn(reported_key="ea-01", claimed=True, registered_key="已配置")
    assert len(warns) == 1
    assert "不一致" in warns[0]


# ---------- 其余分支语义钉住 ----------

def test_unclaimed_warns():
    warns = _warn(reported_key="ea-01", claimed=False, registered_key="ea-01")
    assert len(warns) == 1
    assert "认领不到" in warns[0]


def test_mismatch_warns_with_both_values():
    warns = _warn(reported_key="ea-02", claimed=True, registered_key="ea-01")
    assert len(warns) == 1
    assert "ea-02" in warns[0] and "ea-01" in warns[0]


def test_panel_has_key_but_ea_reports_none():
    warns = _warn(reported_key="", claimed=False, registered_key="ea-01")
    assert len(warns) == 1
    assert "请核对" in warns[0]


def test_nothing_configured_no_warning():
    """两边都没配：identity 分支不告警（多 EA 检查在别处做）。"""
    assert _warn(reported_key="", claimed=False, registered_key="") == []


# ---------- serialize 的打码口径不许被改 ----------

def test_serialize_still_masks_node_key(db):
    """P2 安全修复的语义：对外永远只给「已配置」/""，不给明文。"""
    from backend import models
    from backend import servers as registry

    s = models.RemoteServer(name="t-ea", kind="ea", url="http://ea-test:18080",
                            node_key="ea-01", is_enabled=True)
    db.add(s)
    db.commit()
    data = registry.serialize(db, s)
    assert data["node_key"] == "已配置"
    assert data["node_key_set"] is True

    s.node_key = None
    db.commit()
    data = registry.serialize(db, s)
    assert data["node_key"] == ""
    assert data["node_key_set"] is False
