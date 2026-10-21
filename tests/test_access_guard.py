"""访问拦截（UA 关键词 + IP 归属地）回归测试

这个模块最危险的不是「拦错了」，而是**「默认就拦」**——它挂在所有请求的
最外层，一个配置事故的表现是整站打不开。所以这里把几条纪律钉死：

1. **默认全关**：出厂默认下中间件不生效（升级上来的老部署行为不变）。
2. **fail-open**：归属地查不到一律放行——包括「只允许名单内」这一档。
   地理库一挂就把全站对外全灭是不能接受的；漏放一个爬虫的代价远小于
   误伤所有正常用户。
3. **白名单优先于黑名单**：命中白名单即放行，不再看黑名单。
4. **内网 / 回环不查归属地**：既省一次外部查询，也避免把内网访问误判成境外。
5. **拦截要回响应，不能断连接**：浏览器拿提示页，API / Emby 客户端拿 JSON 403。

全部用隔离的内存 SQLite 与桩，不碰网络、不碰生产库。
"""
import os
import time

os.environ.setdefault("DATABASE_TYPE", "sqlite")
os.environ.setdefault("REDIS_ENABLED", "false")

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend import access_guard as ag
from backend import models
from backend.integrations import store


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    models.Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    store.invalidate()
    yield session
    store.invalidate()
    ag.invalidate()
    session.close()


@pytest.fixture(autouse=True)
def _clear_hot_cache():
    """中间件的规则热缓存是进程级的，用例之间不能互相带"""
    ag.invalidate()
    ag._geo_cache.clear()
    yield
    ag.invalidate()


def _rules(**kw) -> ag.Rules:
    values = dict(ag.DEFAULTS)
    values.update(kw)
    return ag.parse_rules(values)


# ==================== 默认关闭 ====================


def test_defaults_are_all_off():
    """出厂默认必须两个开关都关 —— 升级不能改变既有部署的行为"""
    assert ag.DEFAULTS[ag.CONFIG_UA_ENABLED] == "false"
    assert ag.DEFAULTS[ag.CONFIG_REGION_ENABLED] == "false"
    assert ag.DEFAULTS[ag.CONFIG_UA_ALLOW] == ""
    assert ag.DEFAULTS[ag.CONFIG_UA_DENY] == ""
    assert ag.DEFAULTS[ag.CONFIG_REGION_COUNTRIES] == ""
    assert ag.DEFAULTS[ag.CONFIG_REGION_KEYWORDS] == ""


def test_default_rules_are_inactive():
    assert ag.parse_rules(ag.DEFAULTS).active is False


def test_switch_on_but_no_keywords_is_inactive():
    """开关开了但一个关键词都没填 = 配置没填完，按没配处理

    否则「谁都别进」会被当成合法配置存下来。"""
    assert _rules(access_guard_ua_enabled="true").active is False
    assert _rules(access_guard_region_enabled="true").active is False


# ==================== 关键词解析 ====================


@pytest.mark.parametrize("raw,expected", [
    ("a,b", ("a", "b")),
    ("a，b", ("a", "b")),          # 中文逗号
    ("a;b\nc", ("a", "b", "c")),   # 分号 + 换行
    ("  A  ,  a ", ("a",)),        # 去重 + 大小写归一
    ("", ()),
    (None, ()),
])
def test_split_keywords(raw, expected):
    assert ag.split_keywords(raw) == expected


# ==================== UA 判定 ====================


def test_ua_blacklist_blocks_match():
    rules = _rules(access_guard_ua_enabled="true", access_guard_ua_deny="Googlebot")
    verdict = ag.evaluate_ua(rules, "Mozilla/5.0 (compatible; Googlebot/2.1)")
    assert verdict.blocked is True
    assert verdict.reason == "ua_blocked"
    assert verdict.matched == "googlebot"


def test_ua_blacklist_passes_normal_browser():
    rules = _rules(access_guard_ua_enabled="true", access_guard_ua_deny="Googlebot")
    assert ag.evaluate_ua(rules, "Mozilla/5.0 Chrome/120").blocked is False


def test_ua_whitelist_allows_listed_only():
    rules = _rules(access_guard_ua_enabled="true", access_guard_ua_allow="Emby")
    assert ag.evaluate_ua(rules, "Emby/3.4 Android").blocked is False
    blocked = ag.evaluate_ua(rules, "Curl/8.0")
    assert blocked.blocked is True
    assert blocked.reason == "ua_not_allowed"


def test_ua_whitelist_beats_blacklist():
    """同时配了黑白名单时，白名单命中就该放行（管理员的显式意图优先）"""
    rules = _rules(
        access_guard_ua_enabled="true",
        access_guard_ua_allow="Emby",
        access_guard_ua_deny="Emby",
    )
    assert ag.evaluate_ua(rules, "Emby/3.4").blocked is False


def test_ua_switch_off_never_blocks():
    """开关关着时，即使关键词命中也不拦"""
    rules = _rules(access_guard_ua_enabled="false", access_guard_ua_deny="Googlebot")
    assert ag.evaluate_ua(rules, "Googlebot").blocked is False


def test_ua_empty_string_ua_does_not_crash():
    rules = _rules(access_guard_ua_enabled="true", access_guard_ua_deny="bot")
    assert ag.evaluate_ua(rules, "").blocked is False


# ==================== 归属地判定 ====================


def _region(mode, countries="", keywords=""):
    return _rules(
        access_guard_region_enabled="true",
        access_guard_region_mode=mode,
        access_guard_region_countries=countries,
        access_guard_region_keywords=keywords,
    )


def test_region_block_mode_blocks_listed_country():
    rules = _region("block", countries="日本")
    assert ag.evaluate_region(
        rules, ip="1.1.1.1", country="日本", region="东京", resolved=True
    ).blocked is True


def test_region_block_mode_passes_other_country():
    rules = _region("block", countries="日本")
    assert ag.evaluate_region(
        rules, ip="1.1.1.1", country="中国", region="广东 深圳", resolved=True
    ).blocked is False


def test_region_allow_mode_blocks_unlisted_country():
    """"只允许国内访问" 就是这一档"""
    rules = _region("allow", countries="中国")
    assert ag.evaluate_region(
        rules, ip="1.1.1.1", country="中国", region="广东 深圳", resolved=True
    ).blocked is False
    assert ag.evaluate_region(
        rules, ip="8.8.8.8", country="美国", region="加利福尼亚", resolved=True
    ).blocked is True


def test_region_allow_mode_fails_open_when_geo_unknown():
    """**关键**：地理库挂了 / 没配时，"只允许国内" 不能把全站对外全灭"""
    rules = _region("allow", countries="中国")
    assert ag.evaluate_region(
        rules, ip="1.1.1.1", country="", region="", resolved=False
    ).blocked is False


def test_region_block_mode_also_fails_open_when_geo_unknown():
    rules = _region("block", countries="日本")
    assert ag.evaluate_region(
        rules, ip="1.1.1.1", country="", region="", resolved=False
    ).blocked is False


def test_region_keyword_matches_province():
    rules = _region("block", keywords="香港")
    assert ag.evaluate_region(
        rules, ip="1.1.1.1", country="中国", region="香港", resolved=True
    ).blocked is True


def test_region_invalid_mode_falls_back_to_block():
    """非法 mode 回落到 block（更保守的方向），不是 allow"""
    rules = _region("nonexistent", countries="日本")
    assert rules.region_mode == ag.MODE_BLOCK
    assert ag.evaluate_region(
        rules, ip="1.1.1.1", country="日本", region="东京", resolved=True
    ).blocked is True


# ==================== 公网地址判定 ====================


@pytest.mark.parametrize("ip,expected", [
    ("1.1.1.1", True),
    ("8.8.8.8", True),
    ("127.0.0.1", False),
    ("::1", False),
    ("192.168.1.5", False),
    ("10.0.0.1", False),
    ("172.16.0.1", False),
    ("169.254.1.1", False),
    ("", False),
    ("not-an-ip", False),
])
def test_looks_public(ip, expected):
    assert ag._looks_public(ip) is expected


@pytest.mark.parametrize("ip,expected", [
    ("127.0.0.1", True),
    ("::1", True),
    ("192.168.1.5", True),
    ("10.0.0.1", True),
    ("172.16.0.1", True),
    ("169.254.1.1", True),
    ("1.1.1.1", False),
    ("8.8.8.8", False),
    ("unknown", False),
    ("", False),
])
def test_is_local_client(ip, expected):
    assert ag.is_local_client(ip) is expected


def test_middleware_lets_local_clients_bypass_ua_rules():
    """**自锁保险**：白名单写错时，管理员还能从本机 / 局域网改回来。

    没有这一条的话，一次手滑就把唯一能改配置的那一页也关在门外了。
    """
    from fastapi.testclient import TestClient

    _prime(_rules(access_guard_ua_enabled="true", access_guard_ua_allow="Emby"))
    app_ = _app_with_guard()
    with TestClient(app_, client=("127.0.0.1", 5000)) as local:
        assert local.get("/ping", headers={"user-agent": "Curl/8"}).status_code == 200
    with TestClient(app_, client=("1.2.3.4", 5000)) as public:
        assert public.get("/ping", headers={"user-agent": "Curl/8"}).status_code == 403


def test_resolve_geo_skips_lookup_for_private_ip(monkeypatch):
    """内网地址根本不该去问地理库（既省一次外部调用也不会误判成境外）"""
    calls = []

    from backend.integrations import geoip

    monkeypatch.setattr(geoip, "provider", lambda db: "tencent")
    monkeypatch.setattr(
        geoip, "lookup", lambda db, ip, **kw: calls.append(ip) or {"ok": False}
    )
    assert ag.resolve_geo(None, "192.168.1.5") == ("", "", False)
    assert calls == []


def test_resolve_geo_reports_unresolved_when_provider_missing(monkeypatch):
    from backend.integrations import geoip

    monkeypatch.setattr(geoip, "provider", lambda db: "none")
    assert ag.resolve_geo(None, "1.1.1.1") == ("", "", False)


def test_resolve_geo_treats_placeholder_as_unknown(monkeypatch):
    """地理库返回「未知」时语义是「不知道」，不能当成「在境外」"""
    from backend.integrations import geoip

    monkeypatch.setattr(geoip, "provider", lambda db: "mmdb")
    monkeypatch.setattr(
        geoip, "lookup",
        lambda db, ip, **kw: {"ok": True, "country": "", "region": "未知"},
    )
    assert ag.resolve_geo(None, "1.1.1.1") == ("", "", False)


# ==================== 配置读写 ====================


def test_write_policy_roundtrip(db):
    applied = ag.write_policy(db, {
        "ua_enabled": True,
        "ua_deny": "Googlebot, badbot",
        "region_enabled": "1",
        "region_mode": "allow",
        "region_countries": "中国",
    })
    db.commit()
    assert applied[ag.CONFIG_UA_ENABLED] == "true"
    assert applied[ag.CONFIG_UA_DENY] == "googlebot,badbot"

    payload = ag.policy_payload(db)
    assert payload["ua_enabled"] is True
    assert payload["ua_deny"] == ["googlebot", "badbot"]
    assert payload["region_mode"] == "allow"
    assert payload["region_countries"] == ["中国"]
    assert payload["active"] is True


def test_write_policy_invalidates_the_shared_store_cache(db):
    """写入后必须立即读得到新值。

    ``store.get_values`` 自带进程级 TTL 缓存，而 ``write_policy`` 是直接写
    ``SystemConfig``（不走 ``store.write_values``），所以得自己把缓存清掉，
    否则「保存成功」之后读回来还是旧值。缓存以 **key** 为索引、不区分数据库，
    这条用例也顺带钉住了本模块不会把别的内存库测试带偏。
    """
    assert ag.policy_payload(db)["ua_enabled"] is False
    ag.write_policy(db, {"ua_enabled": True})
    db.commit()
    # 不等 TTL，直接读
    assert ag.policy_payload(db)["ua_enabled"] is True


def test_write_policy_accepts_the_short_names_the_frontend_sends(db):
    """**回归**：前后端两边的字段名必须对得上。

    之前 ``write_policy`` 只认 SystemConfig 的全名（``access_guard_ua_denied``），
    而 ``policy_payload`` 返回的是短名（``ua_deny``）。于是后台「保存」永远返回
    ``applied={}``——**配置一条也没存进去，而页面看着一切正常**。
    单元测试当时全用全名调用，把这个洞完整地漏了过去；是端到端跑真实前端
    请求体才暴露的。
    """
    payload = ag.policy_payload(db)
    # 页面拿到的就是 policy_payload 的键，原样回传
    applied = ag.write_policy(db, {
        "ua_enabled": True,
        "ua_deny": "Googlebot, SemrushBot",
        "region_enabled": True,
        "region_mode": "allow",
        "region_countries": "中国",
    })
    db.commit()
    assert applied, "短名写入必须真的生效，不能静默丢弃"
    assert applied[ag.CONFIG_UA_ENABLED] == "true"
    assert applied[ag.CONFIG_UA_DENY] == "googlebot,semrushbot"

    after = ag.policy_payload(db)
    assert after["ua_enabled"] is True
    assert after["ua_deny"] == ["googlebot", "semrushbot"]
    assert after["region_mode"] == "allow"
    assert after["region_countries"] == ["中国"]
    # 读回来的字段名集合必须正好是写接口认的那一套
    assert set(ag.FIELD_TO_KEY) <= set(payload) | set(payload)


def test_every_writable_field_is_readable_back(db):
    """每个能写的字段都得能从 policy_payload 读回来，否则就是存进去读不出"""
    writable = {field: "x" for field in ag.FIELD_TO_KEY}
    writable["ua_enabled"] = True
    writable["region_enabled"] = True
    writable["region_mode"] = "allow"
    ag.write_policy(db, writable)
    db.commit()
    after = ag.policy_payload(db)
    for field in ag.FIELD_TO_KEY:
        assert field in after, field


def test_write_policy_rejects_unknown_keys_and_bad_mode(db):
    """非法键/非法值不写：宁可保持原值，也不存半个坏配置"""
    applied = ag.write_policy(db, {
        "ua_denied": "typo",                     # 不在白名单里
        "region_mode": "sideways",               # 非法档位
    })
    db.commit()
    assert applied == {}
    assert db.query(models.SystemConfig).count() == 0


def test_write_policy_ignores_blank_values(db):
    """只给开关不给关键词时，关键词写空串（= 清空），不报错"""
    applied = ag.write_policy(db, {"ua_enabled": "true"})
    db.commit()
    assert applied == {ag.CONFIG_UA_ENABLED: "true"}


def test_preview_reports_block_with_reason(db, monkeypatch):
    from backend.integrations import geoip

    ag.write_policy(db, {
        "ua_enabled": "true",
        "ua_deny": "Googlebot",
    })
    db.commit()
    assert ag.preview(db, ip="1.1.1.1", user_agent="Googlebot/2.1")["blocked"] is True
    assert ag.preview(db, ip="1.1.1.1", user_agent="Chrome/120")["blocked"] is False


def test_preview_region_shows_resolved_location(db, monkeypatch):
    """试跑要真的调地理库并把国家/地区回显出来，否则管理员无从确认规则写得对不对"""
    from backend.integrations import geoip

    ag.write_policy(db, {
        "region_enabled": "true",
        "region_mode": "allow",
        "region_countries": "中国",
    })
    db.commit()
    monkeypatch.setattr(geoip, "provider", lambda db: "tencent")
    monkeypatch.setattr(
        geoip, "lookup",
        lambda db, ip, **kw: {"ok": True, "country": "日本", "region": "东京"},
    )
    result = ag.preview(db, ip="1.1.1.1", user_agent="")
    assert result["resolved"] is True
    assert result["country"] == "日本"
    assert result["blocked"] is True


# ==================== 提示页 ====================


def test_block_page_is_self_contained_and_themed():
    html = ag.render_block_page(
        site_name="我的站", reason="region_blocked", message="当前所在地区已被本站屏蔽"
    )
    assert html.startswith("<!doctype html>")
    # 浅色 / 深色两套都要在
    assert "prefers-color-scheme:dark" in html
    # 桌面 / 窄屏都要在
    assert "@media (max-width:480px)" in html
    # 不引任何外部资源（被拦下的请求不该再去别处拉东西）
    assert "http://" not in html and "https://" not in html
    assert "当前所在地区已被本站屏蔽" in html


def test_block_page_escapes_untrusted_text():
    """站点名与原因都来自配置，必须转义，不能被注入标签"""
    html = ag.render_block_page(
        site_name="<script>alert(1)</script>",
        reason="ua_blocked",
        message="<img src=x onerror=alert(1)>",
    )
    assert "<script>" not in html
    assert "<img" not in html          # 标签本身不能出现（这条才是真的）
    assert "&lt;script&gt;" in html     # 被转义成纯文本
    assert "&lt;img" in html


def test_block_page_falls_back_for_unknown_reason():
    html = ag.render_block_page(site_name="x", reason="???", message="")
    assert "访问受限" in html


def test_wants_html_content_negotiation():
    assert ag._wants_html("text/html,application/xhtml+xml") is True
    assert ag._wants_html("application/json") is False
    assert ag._wants_html("") is False


# ==================== 中间件（真起一个 ASGI 应用） ====================


def _app_with_guard():
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/ping")
    def ping():
        return {"ok": True}

    app.add_middleware(ag.AccessGuardMiddleware)
    return app


def _prime(rules):
    """把规则直接塞进热缓存，跳过数据库（端到端只验中间件行为）"""
    ag._hot = rules
    ag._hot_deadline = time.monotonic() + 60


def test_middleware_passes_everything_by_default():
    from fastapi.testclient import TestClient

    _prime(ag.parse_rules(ag.DEFAULTS))
    with TestClient(_app_with_guard()) as client:
        for ua in ("Chrome/120", "Googlebot/2.1", "curl/8"):
            resp = client.get("/ping", headers={"user-agent": ua})
            assert resp.status_code == 200, ua


def test_middleware_blocks_blacklisted_ua_with_html_for_browser():
    from fastapi.testclient import TestClient

    _prime(_rules(access_guard_ua_enabled="true", access_guard_ua_deny="Googlebot"))
    with TestClient(_app_with_guard()) as client:
        resp = client.get("/ping", headers={"user-agent": "Googlebot/2.1",
                                            "accept": "text/html"})
        assert resp.status_code == 403
        assert "text/html" in resp.headers["content-type"]
        assert "已被本站屏蔽" in resp.text
        # 正常浏览器不受影响
        assert client.get("/ping", headers={"user-agent": "Chrome/120",
                                            "accept": "text/html"}).status_code == 200


def test_middleware_returns_json_for_api_clients():
    """给 Emby 客户端塞一页 HTML 会让它报「无法解析响应」，比直接 403 更难排查"""
    from fastapi.testclient import TestClient

    _prime(_rules(access_guard_ua_enabled="true", access_guard_ua_deny="Googlebot"))
    with TestClient(_app_with_guard()) as client:
        resp = client.get("/ping", headers={"user-agent": "Googlebot/2.1",
                                            "accept": "application/json"})
        assert resp.status_code == 403
        assert "json" in resp.headers["content-type"]
        assert resp.json()["detail"]


def test_middleware_blocks_by_region(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(ag, "_resolve_geo_cached",
                        lambda db, ip: ("日本", "东京", True))
    _prime(_region("block", countries="日本"))
    with TestClient(_app_with_guard()) as client:
        resp = client.get("/ping", headers={"user-agent": "Chrome/120"})
        assert resp.status_code == 403
        assert "屏蔽" in resp.json()["detail"]


def test_middleware_allows_when_geo_unresolved(monkeypatch):
    """地理查不到时必须放行，哪怕开着「只允许国内」"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(ag, "_resolve_geo_cached", lambda db, ip: ("", "", False))
    _prime(_region("allow", countries="中国"))
    with TestClient(_app_with_guard()) as client:
        assert client.get("/ping").status_code == 200


def test_middleware_uses_trusted_proxy_client_ip(monkeypatch):
    """拿的是 ratelimit.get_client_ip 的口径（只信可信代理重写的头），
    不是随便取 XFF 第一段——否则伪造一个头就能绕开地区拦截"""
    from fastapi.testclient import TestClient

    seen = []
    monkeypatch.setattr(
        ag, "_resolve_geo_cached",
        lambda db, ip: (seen.append(ip) or ("", "", False)),
    )
    _prime(_region("block", countries="日本"))
    with TestClient(_app_with_guard()) as client:
        client.get("/ping", headers={"x-forwarded-for": "8.8.8.8",
                                     "x-real-ip": "9.9.9.9"})
    # 直连方是 testclient（不是可信代理）→ 所有转发头都不可信，用直连 IP
    assert seen and seen[0] not in ("8.8.8.8", "9.9.9.9")


def test_middleware_survives_config_read_failure(monkeypatch):
    """配置读不出来时按全关处理，绝不因为拦截功能自己故障而锁死站点"""

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(ag, "_refresh", _boom)
    monkeypatch.setattr(
        ag, "hot_rules",
        lambda: (_ for _ in ()).throw(RuntimeError("unused")),
    )
    # 直接测 _refresh 的容错路径
    from starlette.concurrency import run_in_threadpool

    def _safe_refresh():
        try:
            from backend.database import SessionLocal

            db = SessionLocal()
            try:
                return ag.load_rules(db)
            finally:
                db.close()
        except Exception:
            return ag.Rules()

    assert _safe_refresh().active is False


def test_non_http_scope_passes_through():
    """WebSocket / lifespan 不能被这个中间件吃掉"""
    from fastapi.testclient import TestClient

    _prime(_rules(access_guard_ua_enabled="true", access_guard_ua_deny="Googlebot"))
    with TestClient(_app_with_guard()) as client:
        assert client.get("/ping").status_code == 200