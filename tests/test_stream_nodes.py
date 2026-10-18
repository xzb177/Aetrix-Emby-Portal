"""流节点（分离架构）URL 改写与注册的回归测试。

第一回归口径：**无配置 = 与升级前逐字节一致**（未注册远端节点时播放 URL
原样返回，hit=False）。其余钉住：公网地址守卫、改写只换基址前缀、
base 对不上时宁可不改写、管理接口的注册/去重/移除。

（2026-10-17 转码删除后）：``rewrite_playback_urls`` 不再接收/处理
``transcoding_url``，签名 ``(db, stream_url, base)``、返回二元组
``(stream_url, hit)``。TranscodingUrl 已不存在，调用方只改写播放 URL。

用隔离的内存 SQLite，不碰生产库。
"""
import inspect

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


BASE = "https://portal.example.com"
NODE = "https://stream.example.com"
STREAM = (BASE + "/emby/Videos/abc123/stream?static=true&MediaSourceId=abc123"
          "&api_key=k&uid=1&exp=999&sign=s")


def _register(db, url=NODE, name="n1", weight=100):
    from backend.emby_server import stream_nodes
    return stream_nodes.register_node(db, url, name, weight)


# ---------- 新签名（转码删除后）：(db, stream_url, base) -> (stream_url, hit) ----------

def test_new_signature_has_no_transcoding_url():
    """2026-10-17：改写函数不再接收 transcoding_url 参数，返回二元组。"""
    from backend.emby_server import stream_nodes
    params = list(inspect.signature(stream_nodes.rewrite_playback_urls).parameters)
    assert params == ["db", "stream_url", "base"], params


# ---------- 默认：无配置不改写 ----------

def test_no_config_no_rewrite(db):
    from backend.emby_server import stream_nodes
    s, hit = stream_nodes.rewrite_playback_urls(db, STREAM, BASE)
    assert (s, hit) == (STREAM, False)


# ---------- 注册后改写 ----------

def test_register_then_rewrite(db):
    from backend.emby_server import stream_nodes
    _register(db)
    s, hit = stream_nodes.rewrite_playback_urls(db, STREAM, BASE)
    assert hit is True
    assert s.startswith(NODE + "/emby/Videos/abc123/stream?")
    assert "uid=1&exp=999&sign=s" in s  # 签名查询串原样保留


def test_rewrite_only_prefix(db):
    from backend.emby_server import stream_nodes
    _register(db)
    # URL 不以 base 开头：不换，不能造出坏 URL
    other = "https://other.example.com/emby/Videos/x/stream"
    s, hit = stream_nodes.rewrite_playback_urls(db, other, BASE)
    assert (s, hit) == (other, False)


def test_same_origin_no_rewrite(db):
    from backend.emby_server import stream_nodes
    _register(db, url=BASE)  # 节点就是本机域名
    s, hit = stream_nodes.rewrite_playback_urls(db, STREAM, BASE)
    assert (s, hit) == (STREAM, False)


# ---------- 公网守卫 ----------

def test_reject_private_node_url(db):
    from backend.emby_server import stream_nodes
    for bad in ("http://127.0.0.1:8000", "http://192.168.1.10:8001",
                "http://10.0.0.5/", "http://localhost:8001", ""):
        with pytest.raises(ValueError):
            stream_nodes.register_node(db, bad)


def test_default_loopback_never_rewritten(db):
    from backend.emby_server import stream_nodes
    # node_health 无配置时的默认本机条目是回环地址：改写层必须忽略
    s, hit = stream_nodes.rewrite_playback_urls(db, STREAM, BASE)
    assert hit is False
    assert "127.0.0.1" not in s


def test_is_public_node_url():
    from backend.emby_server import stream_nodes as sn
    assert sn._is_public_node_url("https://stream.example.com") is True
    assert sn._is_public_node_url("https://stream.example.com:8443/x") is True
    assert sn._is_public_node_url("http://127.0.0.1:8000") is False
    assert sn._is_public_node_url("http://[::1]:8001") is False
    assert sn._is_public_node_url("http://192.168.0.1/") is False
    assert sn._is_public_node_url("") is False
    assert sn._is_public_node_url("not a url") is False


# ---------- 注册管理 ----------

def test_register_dedup_by_url(db):
    from backend.emby_server import stream_nodes
    _register(db, name="old", weight=10)
    _register(db, name="new", weight=200)
    nodes = stream_nodes._raw_nodes(db)
    assert len(nodes) == 1
    assert nodes[0]["name"] == "new"
    assert nodes[0]["weight"] == 200


def test_unregister(db):
    from backend.emby_server import stream_nodes
    _register(db)
    assert stream_nodes.unregister_node(db, NODE) is True
    assert stream_nodes._raw_nodes(db) == []
    assert stream_nodes.unregister_node(db, NODE) is False
    # 移除后恢复默认行为
    s, hit = stream_nodes.rewrite_playback_urls(db, STREAM, BASE)
    assert hit is False


# ---------- r30 F1：路径边界匹配 ----------

def test_rewrite_rejects_non_boundary_prefix(db):
    """base 落在路径段中间（/emby vs /embyfoo）时按未命中处理，不造坏 URL。"""
    from backend.emby_server import stream_nodes
    _register(db)
    bad_stream = "https://portal.example.com/embyfoo/x"
    base = "https://portal.example.com/emby"
    s, hit = stream_nodes.rewrite_playback_urls(db, bad_stream, base)
    assert (s, hit) == (bad_stream, False)


def test_rewrite_boundary_match_regression(db):
    """真实调用方形态（base=纯主机，URL 后紧跟路径/查询）改写行为逐字节不变。"""
    from backend.emby_server import stream_nodes
    _register(db)
    s, hit = stream_nodes.rewrite_playback_urls(db, STREAM, BASE)
    assert hit is True
    assert s == STREAM.replace(BASE, NODE, 1)


# ---------- r30 F2：注册 scheme 白名单 ----------

def test_register_rejects_non_http_scheme(db):
    """ftp 等非 http(s) scheme 即使通过公网判定也不得注册。"""
    from backend.emby_server import stream_nodes
    for bad in ("ftp://a.b/c", "FTP://A.B/C", "gopher://x.y/"):
        with pytest.raises(ValueError, match="http"):
            stream_nodes.register_node(db, bad)


def test_register_accepts_uppercase_https(db):
    """scheme 大小写不敏感：HTTPS 开头照常注册。"""
    from backend.emby_server import stream_nodes
    node = stream_nodes.register_node(db, "HTTPS://upper.example.com:8443/x")
    assert node["url"] == "HTTPS://upper.example.com:8443/x"
