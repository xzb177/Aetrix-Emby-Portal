"""CDN 域名预留（播放三层第 2/3 层极简预留版）单元测试。

第一回归口径：**默认关闭 = 与升级前逐字节一致**（未配置时所有播放 URL 原样返回）。
其余钉住：域名归一化的边界、改写只碰本服务前缀、缓存头口径（分片可缓存 / 播放列表、
API、302 no-store）、cdn 线路已注册。

用隔离的内存 SQLite，不碰生产库。
"""
import pytest


@pytest.fixture()
def db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend import models
    from backend.integrations import store

    # 热读缓存（store）是进程级全局的：不清掉会把上一个用例的库态带进来
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
CDN = "https://cdn.example.com"


def _enable(db, domain="cdn.example.com"):
    from backend.emby_server import cdn

    return cdn.write_config(db, domain, True)


# ---------- 域名归一化 ----------

def test_normalize_domain_accepts_common_shapes():
    from backend.emby_server import cdn

    assert cdn.normalize_domain("cdn.example.com") == CDN
    assert cdn.normalize_domain("https://cdn.example.com") == CDN
    assert cdn.normalize_domain("  https://CDN.Example.com/  ") == CDN
    assert cdn.normalize_domain("http://cdn.example.com:8080/path/x") == "http://cdn.example.com:8080"
    assert cdn.normalize_domain("cdn.example.com:8443") == "https://cdn.example.com:8443"
    # 单标签主机名（内网 / 本机调试）也放行：合法性只到「是不是个主机名」
    assert cdn.normalize_domain("localhost") == "https://localhost"


def test_normalize_domain_rejects_garbage():
    from backend.emby_server import cdn

    for raw in (None, "", "   ", "bad domain", "ftp://cdn.example.com",
                "https://", "https:///path", "not_a_domain", "-bad.example.com",
                "https://1.2.3.4", "cdn.example.com:abc"):
        assert cdn.normalize_domain(raw) is None, raw


# ---------- 默认关闭：逐字节兼容 ----------

def test_default_off_is_byte_identical(db):
    from backend.emby_server import cdn

    url = f"{BASE}/emby/Videos/abc/stream?static=true&api_key=tok"
    assert cdn.enabled(db) is False
    assert cdn.configured_domain(db) == ""
    assert cdn.rewrite_url(db, url, BASE) == url
    assert cdn.origin_base(db, BASE) == BASE
    payload = cdn.config_payload(db)
    assert payload["enabled"] is False
    assert payload["normalized"] == ""
    assert payload["segment_cache_header"] == cdn.SEGMENT_CACHE_HEADER


def test_switch_without_valid_domain_stays_off(db):
    """只把开关打开、域名空/非法：不生效（一个写错的域名不能把播放打挂）"""
    from backend import models
    from backend.emby_server import cdn
    from backend.integrations import store

    db.add(models.SystemConfig(key=cdn.CONFIG_CDN_ENABLED, value="true"))
    db.commit()
    store.invalidate()
    assert cdn.enabled(db) is False


def test_disabled_with_dirty_domain_does_not_break_playback(db):
    from backend.emby_server import cdn

    url = f"{BASE}/emby/Videos/abc/stream?static=true&api_key=tok"
    cdn.write_config(db, "cdn.example.com/x y", False)
    assert cdn.configured_domain(db) == "cdn.example.com/x y"
    assert cdn.enabled(db) is False
    assert cdn.rewrite_url(db, url, BASE) == url
    assert cdn.origin_base(db, BASE) == BASE


# ---------- 写配置 / 启用 ----------

def test_write_config_enables_with_normalized_domain(db):
    state = _enable(db)
    assert state["enabled"] is True
    assert state["domain"] == CDN
    assert state["normalized"] == CDN
    # 归一化后的域名落库：EA/EM 两边读到的都是同一份干净值
    from backend.emby_server import cdn

    assert cdn.configured_domain(db) == CDN


def test_write_config_rejects_invalid_domain_when_enabling(db):
    from backend.emby_server import cdn

    with pytest.raises(ValueError):
        cdn.write_config(db, "bad domain", True)
    assert cdn.enabled(db) is False
    assert cdn.configured_domain(db) == ""


# ---------- URL 改写范围 ----------

def test_rewrite_only_touches_own_origin(db):
    from backend.emby_server import cdn

    _enable(db)
    own = f"{BASE}/emby/Videos/abc/1.ts?session=s"
    assert cdn.rewrite_url(db, own, BASE) == f"{CDN}/emby/Videos/abc/1.ts?session=s"
    # 其它服务前缀（Google 直链 / 第三方）一律原样返回：本模块只做域名预留
    other = "https://www.googleapis.com/drive/v3/files/x?alt=media"
    assert cdn.rewrite_url(db, other, BASE) == other
    assert cdn.rewrite_url(db, "", BASE) == ""
    # 基址的尾斜杠不参与匹配
    assert cdn.rewrite_url(db, f"{BASE}/emby/x", BASE + "/") == f"{CDN}/emby/x"
    assert cdn.origin_base(db, BASE) == CDN


# ---------- 缓存头口径 ----------

def test_cache_control_taxonomy():
    from backend.emby_server import cdn

    # 分片（HLS 切片 / 直接流）：可被 CDN 边缘缓存
    assert cdn.is_segment_path("/emby/videos/abc/1.ts") is True
    assert cdn.is_segment_path("/emby/videos/abc/seg.m4s?session=x") is True
    assert cdn.is_segment_path("/emby/Videos/x/stream?static=true") is True
    assert cdn.is_segment_path("/emby/videos/x/stream.mkv") is True
    # 播放列表 / API / 字幕清单：绝不能被缓存
    assert cdn.is_segment_path("/emby/videos/x/main.m3u8") is False
    assert cdn.is_segment_path("/emby/Users/abc") is False
    assert cdn.is_segment_path("/emby/videos/x/subtitles.vtt") is False
    assert cdn.cache_control_for("/emby/videos/x/main.m3u8") == "no-store"
    assert cdn.cache_control_for("/emby/Users/abc") == cdn.NO_STORE
    assert cdn.cache_control_for("/emby/videos/x/1.ts") == cdn.SEGMENT_CACHE_HEADER
    assert cdn.SEGMENT_CACHE_HEADER == "public, max-age=21600, s-maxage=21600, immutable"


# ---------- 与 play_line / 播放面打通 ----------

def test_play_lines_single_path():
    """2026-10 简化：只有中转一条路径"""
    from backend.emby_server import cdn, play_line

    assert cdn.play_lines() == play_line.PLAY_LINES
    assert cdn.play_lines() == ("relay",)
    assert play_line.LINE_RELAY == "relay"


def test_play_line_pref_payload_exposes_cdn_flag(db):
    """用户端只在 CDN 真的启用后才展示 cdn 线路（否则就是一个点了没变化的死选项）"""
    from types import SimpleNamespace

    from backend.emby_server import portal

    user = SimpleNamespace(id=42)
    assert portal.get_play_line_pref(user, db) == {
        "line": "relay", "cdn_enabled": False,
    }
    _enable(db)
    payload = portal.get_play_line_pref(user, db)
    assert payload["line"] == "relay"
    assert payload["cdn_enabled"] is True


def test_subtitle_delivery_url_rewritten_only_when_enabled(db):
    from types import SimpleNamespace

    from backend.emby_server import api

    item = SimpleNamespace(guid="abc", bitrate=None, width=None, height=None)
    sub = SimpleNamespace(
        stream_index=2, stream_type="subtitle", codec="srt", language="chi",
        display_title=None, title=None, is_default=False, is_forced=False,
        is_external=False, channels=None, bit_rate=None,
    )
    expected = "/emby/Videos/abc/abc/Subtitles/2/Stream.vtt?api_key=tok"

    off = api._stream_dto(sub, BASE, item, "tok", db)
    assert off["DeliveryUrl"] == BASE + expected

    _enable(db)
    on = api._stream_dto(sub, BASE, item, "tok", db)
    assert on["DeliveryUrl"] == CDN + expected
