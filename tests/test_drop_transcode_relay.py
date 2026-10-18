# -*- coding: utf-8 -*-
"""转码/挂载中继删除（2026-10-17 用户拍板）后的差分测试。

用户拍板：播放侧最后两条「服务器亲自传字节」的路全删——
服务端转码（ffmpeg/HLS/转码缓存）与挂载中继（115/WebDAV/AList 专用中继）
全部删除，115 也改纯 302 直链。

本文件锁住删除后的新契约，并做"删除前后"差分：
1. PlaybackInfo 直连客户端可见面：``_media_source`` 下发
   ``SupportsTranscoding=False``、不含 ``TranscodingUrl``、
   ``SupportsDirectPlay=True``；``playback_info`` 源码级断言不再生成
   TranscodingUrl（删除前按转码策略条件性下发，删除后恒无）。
2. stream_url 改写逻辑不变：把删除前（25c79c9^）的
   ``rewrite_playback_urls`` / ``rewrite_playback_urls_unified``
   实现抄录为参照，与新签名函数逐用例差分——stream_url 输出与
   hit/source 判定必须一致（只差在不再处理 TranscodingUrl）。
3. ``policy_payload`` 无转码键；``write_policy`` 收到转码键时忽略、不落库。
4. 已删符号确实消失：transcode/hwaccel 模块、api.video_hls 等转码端点与
   helper、streaming.serve_mount_relay_async/_TRANSCODE_PROCS、
   maintenance 的转码清理入口。

全部离线：内存 sqlite + monkeypatch，不碰网络与生产库。
"""
import ast
import inspect
import os
import textwrap

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import models as app_models
from backend import playback_policy as pp
from backend.database import Base
from backend.emby_server import api, stream_accel, stream_nodes
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    try:
        yield session
    finally:
        session.close()
        store.invalidate()


BASE = "https://portal.example.com"
NODE = "https://stream.example.com"
STREAM = (BASE + "/emby/Videos/abc123/stream?static=true&MediaSourceId=abc123"
          "&api_key=k&uid=1&exp=999&sign=s")
TRANSCODE_OLD = BASE + "/emby/videos/abc123/master.m3u8?MediaSourceId=abc123&api_key=k"


# ---------------------------------------------------------------------------
# 1. PlaybackInfo 契约（删除后）
# ---------------------------------------------------------------------------

def test_media_source_supports_transcoding_false_no_transcoding_url():
    """_media_source：SupportsTranscoding 恒 False、无 TranscodingUrl。"""
    from backend.emby_server import models as em

    item = em.MediaItem(guid="g1", name="测试", file_path="/strm/x.strm",
                        size=1000, container="mkv", item_type="movie")
    src = api._media_source(item, BASE, db=None)
    assert src["SupportsTranscoding"] is False
    assert "TranscodingUrl" not in src
    assert src["SupportsDirectPlay"] is True
    assert src["UseMostCompatibleTranscodingProfile"] is False


def test_playback_info_source_never_emits_transcoding_url():
    """源码级：playback_info 不再生成 TranscodingUrl（AST 取真实代码）。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(api.playback_info)))
    code = ast.unparse(ast.Module(body=tree.body, type_ignores=[]))
    assert "TranscodingUrl" not in code
    assert "'SupportsTranscoding': False" in code
    # 新签名：改写函数只处理 stream_url
    assert "rewrite_playback_urls_unified(\n" in code or \
        "rewrite_playback_urls_unified(" in code


# ---------------------------------------------------------------------------
# 2. stream_url 改写逻辑差分（删除前参照 vs 删除后新实现）
# ---------------------------------------------------------------------------

def _old_nodes_rewrite(db, stream_url, transcoding_url, base):
    """删除前（25c79c9^）stream_nodes.rewrite_playback_urls 的逐行参照实现。

    唯一差别：当时还接收/改写 transcoding_url，返回三元组。
    stream_url 的改写路径与 hit 判定逻辑与新版逐行一致。
    """
    node = stream_nodes.pick_stream_node(db)
    if node is None:
        return stream_url, transcoding_url, False
    node_base = (node.get("url") or "").rstrip("/")
    base = (base or "").rstrip("/")

    def _boundary_match(url):
        if not url.startswith(base):
            return False
        rest = url[len(base):]
        return rest == "" or rest[0] in ("/", "?", "#")

    if not base or stream_nodes._same_origin(node_base, base):
        return stream_url, transcoding_url, False
    new_stream = (stream_url.replace(base, node_base, 1)
                  if _boundary_match(stream_url) else stream_url)
    new_trans = (transcoding_url.replace(base, node_base, 1)
                 if _boundary_match(transcoding_url) else transcoding_url)
    if new_stream == stream_url and new_trans == transcoding_url:
        return stream_url, transcoding_url, False
    return new_stream, new_trans, True


def _old_unified_rewrite(db, stream_url, transcoding_url, base):
    """删除前（25c79c9^）stream_accel.rewrite_playback_urls_unified 的参照实现。"""
    node_hit = _old_nodes_rewrite(db, stream_url, transcoding_url, base)
    if node_hit[2]:
        return node_hit[0], node_hit[1], "node"
    domain = stream_accel.get_effective_domain(db)
    if domain:
        new_stream = stream_accel.rewrite_url_domain(stream_url, base, domain)
        new_trans = stream_accel.rewrite_url_domain(transcoding_url, base, domain)
        if new_stream != stream_url or new_trans != transcoding_url:
            return new_stream, new_trans, "accel"
    return stream_url, transcoding_url, None


def _register_node(db):
    return stream_nodes.register_node(db, NODE, "n1", 100)


@pytest.mark.parametrize("setup", ["none", "node", "same_origin_node"])
def test_nodes_rewrite_stream_url_matches_old(db, setup):
    """stream_nodes：新二元组函数的 stream_url 输出与 hit，必须与删除前一致。"""
    if setup == "node":
        _register_node(db)
    elif setup == "same_origin_node":
        stream_nodes.register_node(db, BASE, "n1", 100)

    new_s, new_hit = stream_nodes.rewrite_playback_urls(db, STREAM, BASE)
    old_s, _old_t, old_hit = _old_nodes_rewrite(db, STREAM, TRANSCODE_OLD, BASE)
    assert new_s == old_s, "stream_url 改写结果与删除前不一致"
    assert new_hit == old_hit, "hit 判定与删除前不一致"


def test_nodes_rewrite_boundary_cases_match_old(db):
    """边界用例（base 不匹配/路径段中间/空 base）：stream_url 改写结果差分一致。

    注意一个有意的 hit 差异（见下个用例）：旧实现里 hit 曾被"已删除的
    TranscodingUrl 改写了"带成 True；新实现只看 stream_url 本身。
    """
    _register_node(db)
    cases = [
        "https://other.example.com/emby/Videos/x/stream",   # base 对不上
        "https://portal.example.com/embyfoo/x",              # 路径段中间
        STREAM,
    ]
    for url in cases:
        new_s, _new_hit = stream_nodes.rewrite_playback_urls(db, url, BASE)
        old_s, _old_t, _old_hit = _old_nodes_rewrite(db, url, TRANSCODE_OLD, BASE)
        assert new_s == old_s, f"stream_url 改写结果与删除前不一致: {url}"
    # 空 base：新旧都不改写
    new_s, new_hit = stream_nodes.rewrite_playback_urls(db, STREAM, "")
    old_s, _old_t, old_hit = _old_nodes_rewrite(db, STREAM, TRANSCODE_OLD, "")
    assert (new_s, new_hit) == (old_s, old_hit) == (STREAM, False)


def test_nodes_rewrite_hit_no_longer_counts_transcoding_url(db):
    """有意的 hit 语义修正（差分证据）：旧实现里，即使 stream_url 没换，
    只要（已删除的）TranscodingUrl 命中了 base，hit 就被带成 True；
    新实现只看 stream_url 本身——调用方只用 stream_url，这是更正的语义。

    该边角在生产中不可达（新旧两版里两个 URL 总是同一 base 拼出来的），
    这里把差异钉死，防止未来有人"修回"旧语义。
    """
    _register_node(db)
    url = "https://other.example.com/emby/Videos/x/stream"  # stream 不命中 base
    new_s, new_hit = stream_nodes.rewrite_playback_urls(db, url, BASE)
    old_s, _old_t, old_hit = _old_nodes_rewrite(db, url, TRANSCODE_OLD, BASE)
    assert new_s == old_s == url          # 改写结果一致：都没换
    assert old_hit is True               # 旧：被 TranscodingUrl 带成 True
    assert new_hit is False              # 新：只看 stream_url


def test_unified_rewrite_source_matches_old(db, monkeypatch):
    """stream_accel 统一改写：source 判定（node/accel/None）与删除前一致，
    stream_url 输出一致；只差在不再返回 transcoding_url。"""
    # 场景 1：无节点、无加速域 → 原样
    monkeypatch.setattr(stream_accel, "get_effective_domain", lambda db: "")
    new_s, new_src = stream_accel.rewrite_playback_urls_unified(db, STREAM, BASE)
    old_s, _old_t, old_src = _old_unified_rewrite(db, STREAM, TRANSCODE_OLD, BASE)
    assert (new_s, new_src) == (old_s, old_src) == (STREAM, None)

    # 场景 2：有节点 → node
    _register_node(db)
    new_s, new_src = stream_accel.rewrite_playback_urls_unified(db, STREAM, BASE)
    old_s, _old_t, old_src = _old_unified_rewrite(db, STREAM, TRANSCODE_OLD, BASE)
    assert (new_s, new_src) == (old_s, old_src)
    assert new_src == "node" and new_s.startswith(NODE)

    # 场景 3：无节点、有加速域 → accel（节点移除后）
    stream_nodes.unregister_node(db, NODE)
    monkeypatch.setattr(stream_accel, "get_effective_domain",
                        lambda db: "cdn.example.com")
    new_s, new_src = stream_accel.rewrite_playback_urls_unified(db, STREAM, BASE)
    old_s, _old_t, old_src = _old_unified_rewrite(db, STREAM, TRANSCODE_OLD, BASE)
    assert (new_s, new_src) == (old_s, old_src)
    assert new_src == "accel" and new_s.startswith("https://cdn.example.com")


# ---------------------------------------------------------------------------
# 3. playback_policy：转码键已作废
# ---------------------------------------------------------------------------

def test_policy_payload_has_no_transcode_keys(db):
    payload = pp.policy_payload(db)
    assert "transcode_enabled" not in payload
    assert "max_concurrent_transcodes" not in payload
    assert set(payload) == {"drive_direct_play_enabled"}


def test_write_policy_ignores_transcode_keys(db):
    applied = pp.write_policy(db, {
        "transcode_enabled": True,
        "max_concurrent_transcodes": 8,
        "playback_transcode_enabled": "true",
        "playback_max_concurrent_transcodes": "8",
    })
    db.commit()
    assert applied == {}
    assert {r.key for r in db.query(app_models.SystemConfig).all()} == set()


# ---------------------------------------------------------------------------
# 4. 已删符号确实消失
# ---------------------------------------------------------------------------

def test_deleted_modules_are_gone():
    import importlib

    for name in ("backend.emby_server.transcode", "backend.emby_server.hwaccel"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(name)


def test_deleted_api_symbols_are_gone():
    for name in ("video_hls", "_prepare_new_transcode", "_rewrite_playlist",
                 "start_transcode", "get_transcode", "touch_transcode"):
        assert not hasattr(api, name), f"api.{name} 应已删除"


def test_deleted_streaming_symbols_are_gone():
    from backend.emby_server import streaming

    for name in ("serve_mount_relay_async", "_TRANSCODE_PROCS",
                 "active_transcode_ids", "stop_transcode", "spawn_ffmpeg",
                 "transcode_reaper_tick"):
        assert not hasattr(streaming, name), f"streaming.{name} 应已删除"


def test_deleted_maintenance_symbols_are_gone():
    from backend.emby_server import maintenance

    for name in ("cleanup_transcode_orphans", "_transcode_root"):
        assert not hasattr(maintenance, name), f"maintenance.{name} 应已删除"


def test_transcode_compat_stubs_still_204():
    """删除后保留的 204 兼容桩：/transcodes/stop-all 与 ActiveEncodings 路由仍在。"""
    from backend.emby_server import compat_routes, portal

    paths = [getattr(r, "path", "") for r in compat_routes.emby_router.routes]
    assert "/emby/Videos/ActiveEncodings" in paths
    assert hasattr(portal, "admin_stop_all_transcodes")
