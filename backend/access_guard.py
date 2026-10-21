"""访问拦截（UA 关键词 + IP 归属地）

在 ASGI 层拦两类请求，两类都**默认关闭**，管理员不显式打开就等于不存在：

1. **UA 关键词**：黑名单 / 白名单。屏蔽爬虫与特定客户端走黑名单；
   「只放行这几个客户端」走白名单。命中规则按**子串**匹配，大小写不敏感。
2. **IP 归属地**：按国家 / 地区关键词，走「屏蔽列表内」或「只允许列表内」
   两种方向（``只允许国内访问`` 是后者的一个用法）。

为什么不逐端点判
----------------
拦截的价值恰恰在于「覆盖现在还没写的端点」——像 ``DownloadGuardMiddleware``
注释里说的那样，只在已知路径上判断，将来新增一条同类路径就漏判了。所以这里
和它一样在网关层兜底，并且用**纯 ASGI 中间件**而不是 ``BaseHTTPMiddleware``：
后者会包裹响应体，影响本站的大文件流式播放。

失败一律放行（fail-open）
------------------------
这是本模块最重要的一条纪律，与 ``share_guard`` 里「查不到城市就不判定」同源：

* 规则没开 → 直接放行（默认状态，见下）；
* 归属地查不到（没配地理库 / 提供方报错 / 内网与回环地址）→ **当作不在名单内
  且不判定**。特别地「只允许列表内」这一档在查不到时也放行——否则地理库挂了的
  那一刻整个站会对外全灭，而误伤正常用户的代价远高于漏放一个爬虫；
* 任何判定异常都吞掉并放行，**绝不因为拦截功能自己故障而把站点锁死**。

性能
----
「全部规则关闭」是默认状态，也是绝大多数部署的常态，这条路径必须**零开销**：

* ``Rules.active`` 为假时，中间件只做一次 ``time.monotonic()`` 比较就透传，
  不开数据库会话、不 ``await``、不做任何字符串处理；
* 规则本身按短 TTL 缓存在进程内（``_hot``），过期时经 ``run_in_threadpool``
  刷新一次——同步 SQLAlchemy 与地理查询都留在线程池，不占事件循环
  （口径同 ``scripts/check_blocking_routes.py``）；
* UA 判定是纯字符串运算，放在事件循环上直接做；只有**开了归属地**才下放线程池
  （腾讯地图那档是一次带 8 秒超时的外部请求，绝不能挡在事件循环前面）；
* 归属地按 IP 再缓存一层（``_GEO_TTL``）：一个 IP 的连打不会反复问地理库。

配置
----
全部落在 ``SystemConfig``，默认值只在本模块的 ``DEFAULTS`` 里定义一次，
``config_self_heal`` 引用它补缺失行。读取走 ``integrations.store`` 的统一热读。
"""
from __future__ import annotations

import ipaddress
import logging
import time
from dataclasses import dataclass
from html import escape
from typing import Iterable, Optional

from fastapi import Request
from fastapi.responses import HTMLResponse, JSONResponse

from backend.ratelimit import client_ip

logger = logging.getLogger(__name__)

from backend import models  # noqa: E402  - models 依赖较重，放最后避免循环导入

# ==================== 配置键与默认值（默认值只在这里定义一次） ====================

CONFIG_UA_ENABLED = "access_guard_ua_enabled"
CONFIG_UA_ALLOW = "access_guard_ua_allow"
CONFIG_UA_DENY = "access_guard_ua_deny"
CONFIG_REGION_ENABLED = "access_guard_region_enabled"
CONFIG_REGION_MODE = "access_guard_region_mode"
CONFIG_REGION_COUNTRIES = "access_guard_region_countries"
CONFIG_REGION_KEYWORDS = "access_guard_region_keywords"

#: 出厂默认：两个开关都是 false —— 升级上来的老部署行为与升级前**完全一致**，
#: 不会出现「升级完突然有人打不开站」。
DEFAULTS: dict[str, str] = {
    CONFIG_UA_ENABLED: "false",
    CONFIG_UA_ALLOW: "",
    CONFIG_UA_DENY: "",
    CONFIG_REGION_ENABLED: "false",
    CONFIG_REGION_MODE: "block",
    CONFIG_REGION_COUNTRIES: "",
    CONFIG_REGION_KEYWORDS: "",
}

DESCRIPTIONS: dict[str, str] = {
    CONFIG_UA_ENABLED: "访问拦截·UA 规则开关（true/false，默认 false）",
    CONFIG_UA_ALLOW: "访问拦截·UA 白名单关键词（逗号或换行分隔，非空时只放行命中的）",
    CONFIG_UA_DENY: "访问拦截·UA 黑名单关键词（逗号或换行分隔，命中即拦）",
    CONFIG_REGION_ENABLED: "访问拦截·IP 归属地规则开关（true/false，默认 false）",
    CONFIG_REGION_MODE: "访问拦截·归属地方向（block=屏蔽名单内 / allow=只允许名单内）",
    CONFIG_REGION_COUNTRIES: "访问拦截·国家/地区关键词（只与国家名比对，如「中国」）",
    CONFIG_REGION_KEYWORDS: "访问拦截·省/市关键词（与地区串比对，如「香港」）",
}

#: 归属地两种方向
MODE_BLOCK = "block"
MODE_ALLOW = "allow"
MODES = (MODE_BLOCK, MODE_ALLOW)

#: 后台与前端之间的**短名** → SystemConfig 的键。
#:
#: ``policy_payload`` 返回的是短名（也是页面上那几个字段名），所以写回也必须是
#: 短名；两边的名字必须在这里对上。用短名当 SystemConfig 键写出去会得到一个
#: 没人读的行，而前端读回短名时全是空——「存不进去也看不出来」。
FIELD_TO_KEY = {
    "ua_enabled": CONFIG_UA_ENABLED,
    "ua_allow": CONFIG_UA_ALLOW,
    "ua_deny": CONFIG_UA_DENY,
    "region_enabled": CONFIG_REGION_ENABLED,
    "region_mode": CONFIG_REGION_MODE,
    "region_countries": CONFIG_REGION_COUNTRIES,
    "region_keywords": CONFIG_REGION_KEYWORDS,
}

MODE_LABELS = {
    MODE_BLOCK: "屏蔽名单内的地区",
    MODE_ALLOW: "只允许名单内的地区",
}

_TRUE = {"1", "true", "yes", "on"}

#: 地理库给出的占位串，语义是「不知道」而不是「别处」（与 share_guard 同口径）
_UNKNOWN_REGIONS = {"", "未知", "unknown", "unassigned", "n/a", "-"}

#: 进程内规则缓存存活秒数。管理员保存配置时调 :func:`invalidate` 立即失效，
#: 所以这个 TTL 只是跨进程（API / worker 是独立进程）的兜底。
_HOT_TTL = 5.0

#: 归属地判定结果的进程内缓存秒数（geoip 自己也有缓存，这层挡的是「同一 IP
#: 连续请求时反复走一遍 provider 判定」的重复计算）。
_GEO_TTL = 60.0

#: 同一条规则反复命中时的告警节流（秒），避免爬虫把日志刷爆
_LOG_THROTTLE = 60.0


# ==================== 规则 ====================


def _bool(value: object) -> bool:
    return str(value or "").strip().lower() in _TRUE


def split_keywords(raw: object) -> tuple[str, ...]:
    """拆关键词：逗号 / 中文逗号 / 分号 / 换行 / 空白都算分隔

    去重且保持填写顺序（管理员在页面上看到的顺序 = 实际生效的顺序）。
    """
    text = str(raw or "")
    for sep in (",", "，", ";", "；", "\n", "\r", "\t"):
        text = text.replace(sep, " ")
    seen: set[str] = set()
    out: list[str] = []
    for piece in text.split(" "):
        word = piece.strip().lower()
        if not word or word in seen:
            continue
        seen.add(word)
        out.append(word)
    return tuple(out)


@dataclass(frozen=True)
class Rules:
    """一份已解析的规则（不可变，可安全跨线程共享）"""

    ua_enabled: bool = False
    ua_allow: tuple[str, ...] = ()
    ua_deny: tuple[str, ...] = ()
    region_enabled: bool = False
    region_mode: str = MODE_BLOCK
    countries: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()

    @property
    def active(self) -> bool:
        """是否需要做任何判定。全关时中间件走零开销透传。

        开关开着但**一个关键词都没填**也算不生效：那不是「谁都别进」，
        而是配置没填完，按没配处理。
        """
        ua_on = bool(self.ua_enabled and (self.ua_allow or self.ua_deny))
        region_on = bool(
            self.region_enabled and (self.countries or self.keywords)
        )
        return ua_on or region_on


@dataclass(frozen=True)
class Verdict:
    """一次判定的结果。``blocked=False`` 时其余字段无意义。"""

    blocked: bool
    reason: str = ""
    message: str = ""
    matched: str = ""
    detail: str = ""


ALLOW = Verdict(False)


def parse_rules(values: dict) -> Rules:
    """把 ``SystemConfig`` 的原始值解析成 :class:`Rules`（非法值回落到安全侧）"""
    mode = str(values.get(CONFIG_REGION_MODE) or "").strip().lower()
    return Rules(
        ua_enabled=_bool(values.get(CONFIG_UA_ENABLED)),
        ua_allow=split_keywords(values.get(CONFIG_UA_ALLOW)),
        ua_deny=split_keywords(values.get(CONFIG_UA_DENY)),
        region_enabled=_bool(values.get(CONFIG_REGION_ENABLED)),
        region_mode=mode if mode in MODES else MODE_BLOCK,
        countries=split_keywords(values.get(CONFIG_REGION_COUNTRIES)),
        keywords=split_keywords(values.get(CONFIG_REGION_KEYWORDS)),
    )


def load_rules(db) -> Rules:
    """从库里读一份规则（一次 IN 查询取回全部键）"""
    from backend.integrations import store

    values = store.get_values(db, list(DEFAULTS.keys()), DEFAULTS)
    return parse_rules(values)


# ==================== 判定（纯函数，不碰数据库 / 网络） ====================


def match_keywords(haystack: str, words: Iterable[str]) -> str:
    """返回第一个命中的关键词（子串匹配，大小写不敏感），没命中返回空串"""
    if not haystack:
        return ""
    low = haystack.lower()
    for word in words:
        if word and word in low:
            return word
    return ""


def evaluate_ua(rules: Rules, user_agent: str) -> Verdict:
    """UA 判定。白名单优先于黑名单（命中白名单一律放行）。"""
    if not rules.ua_enabled or not (rules.ua_allow or rules.ua_deny):
        return ALLOW

    ua = str(user_agent or "").strip()
    low = ua.lower()

    if rules.ua_allow:
        # 白名单优先：命中即放行，连黑名单都不再看。
        # 一个都没命中 = 不在允许名单内 → 拦。
        if not match_keywords(low, rules.ua_allow):
            return Verdict(
                True,
                reason="ua_not_allowed",
                message="当前客户端不在允许访问的名单内",
                detail=ua[:200],
            )
        return ALLOW

    hit = match_keywords(low, rules.ua_deny)
    if hit:
        return Verdict(
            True,
            reason="ua_blocked",
            message="当前客户端已被站点屏蔽",
            matched=hit,
            detail=ua[:200],
        )
    return ALLOW


def evaluate_region(
    rules: Rules,
    *,
    ip: str,
    country: str,
    region: str,
    resolved: bool,
) -> Verdict:
    """归属地判定。

    ``resolved=False`` 表示**没查出来**（没配地理库 / 查询失败 / 内网地址）。
    这时一律放行：「只允许名单内」也不能把「不知道在哪」当成「在名单外」，
    否则地理库一挂全站对外全灭。
    """
    if not rules.region_enabled or not (rules.countries or rules.keywords):
        return ALLOW
    if not resolved:
        return ALLOW

    country_hit = match_keywords(country, rules.countries)
    region_hit = match_keywords(region, rules.keywords)

    if rules.region_mode == MODE_ALLOW:
        if country_hit or region_hit:
            return ALLOW
        return Verdict(
            True,
            reason="region_not_allowed",
            message="当前所在地区不在本站允许访问的范围内",
            detail=" ".join(p for p in (country, region, ip) if p)[:200],
        )

    hit = country_hit or region_hit
    if hit:
        return Verdict(
            True,
            reason="region_blocked",
            message="当前所在地区已被本站屏蔽",
            matched=hit,
            detail=" ".join(p for p in (country, region, ip) if p)[:200],
        )
    return ALLOW


# ==================== 归属地解析（唯一会碰外部的地方） ====================


def _looks_public(ip: str) -> bool:
    """只对**公网**地址查归属地。

    回环 / 私网 / 链路本地 / 保留地址没有有意义的公网归属地（查了也只会得到
    「未知」），既省掉一次外部查询，也避免把「内网访问」误判成境外。
    """
    text = (ip or "").strip()
    if not text:
        return False
    # XFF 里可能带 :port
    if text.count(":") == 1 and "." in text:
        text = text.split(":", 1)[0]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    return not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def is_local_client(ip: str) -> bool:
    """来源是不是回环 / 内网。

    **UA 规则对这类来源一律放行**：站在服务器同一台机器或同一个局域网里访问的
    就是运维本人，不是爬虫。两个理由：

    1. 与 ``metrics_guard.is_local_host`` 同一口径（全站已有 precedent，
       不另立一套「什么算内网」）；
    2. 它是一道**「把自己锁在门外」的保险**。UA 白名单一旦填错（比如只填了
       某个客户端的名字、没包含管理员自己的浏览器），整站对管理员关闭，而
       唯一能改回来的就是这一页——没有第二条路时那就是一个不可自愈的事故。

    注意这只对 **UA** 生效，归属地规则不因它放行：内网地址本来就没有归属地，
    ``resolve_geo`` 查不到就按 fail-open 放行了。
    """
    text = (ip or "").strip()
    if not text or text == "unknown":
        return False
    if text.count(":") == 1 and "." in text:
        text = text.split(":", 1)[0]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    return bool(addr.is_loopback or addr.is_private or addr.is_link_local)


def resolve_geo(db, ip: str) -> tuple[str, str, bool]:
    """IP → (国家, 地区串, 是否查得到)。查不到就是查不到，不编造。"""
    from backend.integrations import geoip

    if geoip.provider(db) == "none":
        return "", "", False
    if not _looks_public(ip):
        return "", "", False
    result = geoip.lookup(db, ip)
    if not result.get("ok"):
        return "", "", False
    country = str(result.get("country") or "").strip()
    region = str(result.get("region") or "").strip()
    known = not (
        country.lower() in _UNKNOWN_REGIONS and region.lower() in _UNKNOWN_REGIONS
    )
    if not known:
        return "", "", False
    return country, region, True


# ==================== 进程内热缓存 ====================

_hot: Optional[Rules] = None
_hot_deadline: float = 0.0
_geo_cache: dict[str, tuple[float, str, str, bool]] = {}
_last_log: dict[str, float] = {}


def invalidate() -> None:
    """配置变更后调用：同进程内保存即生效"""
    global _hot, _hot_deadline
    _hot = None
    _hot_deadline = 0.0


def _refresh() -> Rules:
    """重新从库里读规则（必须在线程池里调用：同步 SQLAlchemy）"""
    global _hot, _hot_deadline
    try:
        from backend.database import SessionLocal

        db = SessionLocal()
        try:
            rules = load_rules(db)
        finally:
            db.close()
    except Exception:  # pragma: no cover - 配置读不到就当没配，绝不因此锁死站点
        logger.exception("访问拦截规则读取失败，按全部关闭处理")
        rules = Rules()
    _hot = rules
    _hot_deadline = time.monotonic() + _HOT_TTL
    return rules


def hot_rules() -> Optional[Rules]:
    """同步取当前规则；缓存有效时零 I/O，否则返回 ``None`` 让调用方去线程池刷新"""
    if _hot is not None and time.monotonic() < _hot_deadline:
        return _hot
    return None


def _resolve_geo_cached(db, ip: str) -> tuple[str, str, bool]:
    """带进程内 TTL 的归属地解析（同一 IP 的连打不重复问地理库）"""
    key = (ip or "").strip()
    now = time.monotonic()
    hit = _geo_cache.get(key)
    if hit is not None and now - hit[0] < _GEO_TTL:
        return hit[1], hit[2], hit[3]
    result = resolve_geo(db, ip)
    if len(_geo_cache) >= 4096:
        _geo_cache.clear()
    _geo_cache[key] = (now, result[0], result[1], result[2])
    return result


# ==================== 提示页 ====================

#: 403 固定用这个码：请求本身合法，是服务端策略拒绝了它（4xx 而非 5xx）。
BLOCK_STATUS = 403

_TONE = {
    "ua_blocked": ("访问受限", "你的客户端已被本站屏蔽"),
    "ua_not_allowed": ("访问受限", "当前客户端不在允许访问的名单内"),
    "region_blocked": ("访问受限", "当前所在地区已被本站屏蔽"),
    "region_not_allowed": ("访问受限", "当前所在地区不在本站允许访问的范围内"),
    "unavailable": ("访问受限", "当前请求无法处理"),
}

_BLOCK_CSS = """
*,*::before,*::after{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  min-height:100vh;display:flex;align-items:center;justify-content:center;
  padding:24px;background:#e9edf1;color:#1f2328;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",
    "Hiragino Sans GB","Microsoft YaHei",sans-serif;
  -webkit-font-smoothing:antialiased;line-height:1.6;
}
.card{
  width:100%;max-width:30rem;background:#fff;border:1px solid #98a2ad;
  border-radius:14px;padding:32px 28px;text-align:center;
  box-shadow:0 1px 3px rgba(31,35,40,.08);
}
.badge{
  display:inline-flex;align-items:center;justify-content:center;
  width:52px;height:52px;border-radius:50%;margin-bottom:18px;
  background:#fff4e5;color:#8a5a00;border:1px solid #cf9440;
}
.badge svg{width:26px;height:26px}
h1{margin:0 0 8px;font-size:1.25rem;font-weight:650;letter-spacing:.01em}
p.lead{margin:0 0 20px;color:#57606a;font-size:.95rem}
.note{
  margin:0;padding:12px 14px;border-radius:10px;font-size:.85rem;
  background:#f1f4f7;border:1px solid #a2acb8;color:#57606a;text-align:left;
}
.site{margin-top:22px;font-size:.8rem;color:#57606a}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.85em}
/* 深色：跟随系统，与用户端 aurora 主题的观感保持一致 */
@media (prefers-color-scheme:dark){
  body{background:#0d1117;color:#e6edf3}
  .card{background:#1b222d;border-color:#4a525c;box-shadow:none}
  .badge{background:#2a2110;color:#e3b341;border-color:#7a5a1c}
  p.lead{color:#9aa5b1}
  .note{background:#11161d;border-color:#4d5560;color:#9aa5b1}
  .site{color:#9aa5b1}
}
/* 窄屏：手机上把留白收一点，避免一张卡片上下都是空的 */
@media (max-width:480px){
  body{padding:16px;align-items:flex-start}
  .card{padding:26px 20px;margin-top:8vh;border-radius:12px}
  .badge{width:44px;height:44px;margin-bottom:14px}
  h1{font-size:1.1rem}
}
@media (prefers-reduced-motion:no-preference){
  .card{animation:rise .28s ease-out both}
}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
"""

_ICON = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"'
    ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    '<circle cx="12" cy="12" r="10"/><path d="M4.9 4.9l14.2 14.2"/>'
    '<path d="M12 8v5"/><path d="M12 16.5h.01"/></svg>'
)


def _wants_html(accept: str) -> bool:
    """按 ``Accept`` 决定回 HTML 还是 JSON。

    浏览器拿到提示页，Emby 客户端与各类 API 拿到结构化 403 —— 给客户端塞一页
    HTML 反而会让它报「无法解析响应」，比直接 403 更难排查。
    """
    return "text/html" in (accept or "").lower()


def render_block_page(*, site_name: str, reason: str, message: str) -> str:
    """自包含的提示页：浅色 / 深色跟随系统，桌面 / 窄屏自适应。

    不引任何外部资源 —— 被拦下的请求不该再去别处拉资源。
    """
    title, lead = _TONE.get(reason, _TONE["unavailable"])
    site = escape(site_name or "本站", quote=True)
    return (
        "<!doctype html>\n"
        '<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{escape(title)} · {site}</title>"
        f"<style>{_BLOCK_CSS}</style></head><body>"
        '<main class="card">'
        f'<div class="badge">{_ICON}</div>'
        f"<h1>{escape(lead)}</h1>"
        '<p class="lead">如果这是误判，请联系站点管理员处理。</p>'
        '<p class="note">拦截原因：<code>'
        f"{escape(message or reason or '策略限制')}</code></p>"
        f'<p class="site">{site}</p>'
        "</main></body></html>"
    )


# ==================== 中间件 ====================


def _throttled_log(reason: str, ip: str, ua: str) -> None:
    """命中时记一条告警，但按 reason 限流，避免被拦的爬虫把日志刷爆"""
    now = time.monotonic()
    last = _last_log.get(reason, 0.0)
    if now - last < _LOG_THROTTLE:
        return
    _last_log[reason] = now
    if len(_last_log) > 64:
        _last_log.clear()
    logger.warning(
        "访问拦截命中 reason=%s ip=%s ua=%r", reason, ip or "未知", (ua or "")[:120]
    )


class AccessGuardMiddleware:
    """网关级访问拦截（UA + IP 归属地），默认不生效"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        rules = hot_rules()
        if rules is None:
            # 缓存过期：下放线程池刷新（同步 SQLAlchemy 不占事件循环）。
            # 顺带把「读配置失败」的情况在这里就收敛成「全关」。
            from starlette.concurrency import run_in_threadpool

            rules = await run_in_threadpool(_refresh)

        if not rules.active:
            return await self.app(scope, receive, send)

        # Request 只读 header / 不消费 body，原 receive 仍交给下游路由
        request = Request(scope, receive=None)
        ua = request.headers.get("user-agent") or ""
        ip = client_ip(request)

        # 内网 / 回环来源直接透传（见 is_local_client 的两条理由）
        if is_local_client(ip):
            return await self.app(scope, receive, send)

        verdict = evaluate_ua(rules, ua)
        if not verdict.blocked and rules.region_enabled:
            from starlette.concurrency import run_in_threadpool

            def _region() -> Verdict:
                from backend.database import SessionLocal

                db = SessionLocal()
                try:
                    country, region, resolved = _resolve_geo_cached(db, ip)
                    return evaluate_region(
                        rules,
                        ip=ip,
                        country=country,
                        region=region,
                        resolved=resolved,
                    )
                finally:
                    db.close()

            verdict = await run_in_threadpool(_region)

        if not verdict.blocked:
            return await self.app(scope, receive, send)

        _throttled_log(verdict.reason, ip, ua)
        return await self._respond(scope, receive, send, verdict)

    async def _respond(self, scope, receive, send, verdict: Verdict) -> None:
        accept = ""
        for key, value in scope.get("headers") or ():
            if key == b"accept":
                accept = value.decode("latin-1")
                break

        if _wants_html(accept):
            html = render_block_page(
                site_name=_site_name(),
                reason=verdict.reason,
                message=verdict.message,
            )
            response = HTMLResponse(html, status_code=BLOCK_STATUS)
        else:
            response = JSONResponse(
                {"detail": verdict.message or "访问受限"}, status_code=BLOCK_STATUS
            )
        return await response(scope, receive, send)


def _site_name() -> str:
    """站点名（拿不到就留空，页面会回落成「本站」）。

    刻意吞掉异常：品牌配置读不到不该让拦截响应变成 500。
    """
    try:
        from backend.database import SessionLocal
        from backend.integrations import branding

        db = SessionLocal()
        try:
            return str(branding.public_info(db).get("site_name") or "")
        finally:
            db.close()
    except Exception:  # pragma: no cover
        return ""


# ==================== 后台读写（判定逻辑都在上面，这里只做存取） ====================


def policy_payload(db) -> dict:
    """当前策略（后台读一份，前端不维护默认值）"""
    rules = load_rules(db)
    geo_ready = False
    try:
        from backend.integrations import geoip

        geo_ready = geoip.provider(db) != "none"
    except Exception:  # pragma: no cover
        geo_ready = False
    return {
        "ua_enabled": rules.ua_enabled,
        "ua_allow": list(rules.ua_allow),
        "ua_deny": list(rules.ua_deny),
        "region_enabled": rules.region_enabled,
        "region_mode": rules.region_mode,
        "region_countries": list(rules.countries),
        "region_keywords": list(rules.keywords),
        "active": rules.active,
        "modes": list(MODES),
        "mode_labels": dict(MODE_LABELS),
        "fields": sorted(FIELD_TO_KEY.keys()),
        # 归属地能不能查出来：没配地理能力时地区规则只能「不判定」
        "geo_ready": geo_ready,
    }


def write_policy(db, values: dict) -> dict:
    """写回策略。

    非法值**不写**（宁可保持原值，也不存半个坏配置）。关键词存成规范化后的
    逗号分隔形式，免得后台读回来是一长段带换行的原文。
    """
    applied: dict[str, str] = {}
    for field, value in (values or {}).items():
        key = FIELD_TO_KEY.get(field)
        if key is None:
            continue
        if key in (CONFIG_UA_ENABLED, CONFIG_REGION_ENABLED):
            applied[key] = "true" if _bool(value) else "false"
        elif key == CONFIG_REGION_MODE:
            text = str(value or "").strip().lower()
            if text not in MODES:
                continue
            applied[key] = text
        else:
            applied[key] = ",".join(split_keywords(value))

    if not applied:
        return {}
    for key, value in applied.items():
        row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
        if row:
            row.value = value
        else:
            db.add(models.SystemConfig(key=key, value=value, description=DESCRIPTIONS.get(key)))
    db.flush()
    # 两层缓存都要清：本模块的热缓存，以及 ``integrations.store`` 的短 TTL 缓存。
    # 漏掉后者的话，「保存成功」之后 policy_payload 仍会读回写入前的旧值，
    # 页面看起来就像保存没生效——与不写 store 的区别只在于是否额外踩这一脚。
    from backend.integrations import store

    store.invalidate(*applied.keys())
    invalidate()
    return applied


def preview(db, *, ip: str, user_agent: str) -> dict:
    """用**当前**规则试跑一个样本（不拦截任何东西，只回判定结果）。

    给后台的「试一下」用：开「只允许国内」之前先拿自己的 IP 跑一遍，
    免得规则写歪了把自己也关在门外。
    """
    rules = load_rules(db)
    verdict = evaluate_ua(rules, user_agent)
    country = region = ""
    resolved = False
    if not verdict.blocked and rules.region_enabled:
        country, region, resolved = resolve_geo(db, ip)
        verdict = evaluate_region(
            rules, ip=ip, country=country, region=region, resolved=resolved
        )
    return {
        "ip": ip or "",
        "user_agent": user_agent or "",
        "country": country,
        "region": region,
        "resolved": resolved,
        "active": rules.active,
        "blocked": verdict.blocked,
        "reason": verdict.reason,
        "message": verdict.message,
        "matched": verdict.matched,
    }


__all__ = [
    "CONFIG_UA_ENABLED", "CONFIG_UA_ALLOW", "CONFIG_UA_DENY",
    "CONFIG_REGION_ENABLED", "CONFIG_REGION_MODE",
    "CONFIG_REGION_COUNTRIES", "CONFIG_REGION_KEYWORDS",
    "DEFAULTS", "DESCRIPTIONS", "MODES", "MODE_BLOCK", "MODE_ALLOW", "MODE_LABELS",
    "Rules", "Verdict", "ALLOW", "AccessGuardMiddleware", "FIELD_TO_KEY",
    "split_keywords", "parse_rules", "load_rules", "match_keywords",
    "evaluate_ua", "evaluate_region", "resolve_geo", "is_local_client",
    "policy_payload", "write_policy", "preview", "invalidate",
    "render_block_page", "BLOCK_STATUS",
]