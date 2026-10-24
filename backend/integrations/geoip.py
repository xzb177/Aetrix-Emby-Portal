"""能力：IP 与地理位置（腾讯地图 IP 定位 / 本地 MMDB 库）

给风控与日志用：登录日志、设备列表里的 IP 能显示归属地。两种提供方任选其一，Key 与库文件
都由管理员自己提供：

- **腾讯地图 IP 定位**：填一个 WebService Key 即可（无需自备库）；
- **本地 MMDB 库**：给出 GeoLite2 / IPIP 的 ``.mmdb`` 路径，需要环境里有 ``maxminddb``。

查询结果按 ``geoip_cache_hours`` 缓存（同一 IP 不反复问外部接口），查不到就是查不到，
不编造地区。
"""
from __future__ import annotations

import time
from sqlalchemy.orm import Session

SPEC = {
    "title": "IP 与地理位置",
    "desc": "腾讯地图 IP 定位或本地 GeoIP 库；用于登录日志与设备风控显示归属地。",
    "group": "网络与安全",
    "docs_hint": "两种提供方任选：有 Key 用腾讯地图；有库文件（.mmdb）用本地查询。",
    "fields": [
        {"key": "geoip_provider", "label": "提供方", "type": "select", "default": "none",
         "options": ["none", "tencent", "mmdb"]},
        {"key": "geoip_tencent_key", "label": "腾讯地图 Key", "type": "secret"},
        {"key": "geoip_mmdb_path", "label": "MMDB 文件路径", "type": "str", "default": "",
         "placeholder": "/opt/geoip/GeoLite2-City.mmdb"},
        {"key": "geoip_cache_hours", "label": "缓存时长（小时）", "type": "int", "default": "24"},
    ],
    "test_label": "测试定位",
}

TENCENT_URL = "https://apis.map.qq.com/ws/location/v1/ip"
# ip -> (写入时间, 结果, 该条的存活秒数)
_cache: dict[str, tuple[float, dict, float]] = {}
# 提供方读得比谁都勤（每写一条登录日志就要问一次要不要查），走统一热读的短 TTL 缓存：
# 后台保存配置时会调 apply() 清掉它，因此「保存后立即生效」不受影响。
_PROVIDER_TTL = 30.0
# 失败结果只留 5 分钟：瞬时故障不至于让一个 IP 一整天查不到归属地
_FAILURE_TTL = 300.0
# 缓存条数上限（异常场景下别让它无限长大）
_MAX_CACHE = 5000


def _value(db: Session, key: str, default: str = "") -> str:
    """统一热读（只许这一套）：短 TTL 缓存，保存时失效"""
    from backend.integrations import store
    return store.get_value(db, key, default, ttl=_PROVIDER_TTL).strip()


def provider(db: Session) -> str:
    name = _value(db, "geoip_provider", "none").lower()
    return name if name in ("tencent", "mmdb") else "none"


def _invalidate() -> None:
    """配置变更后清缓存（保存能力时调用，保证立即生效）"""
    from backend.integrations import store
    store.invalidate()
    _cache.clear()


def apply(db: Session) -> None:
    _invalidate()


def is_configured(values: dict) -> bool:
    """选中的那家提供方所需的东西填齐了才算配过"""
    name = str(values.get("geoip_provider") or "none").lower()
    if name == "tencent":
        return bool(values.get("geoip_tencent_key"))
    if name == "mmdb":
        return bool(values.get("geoip_mmdb_path"))
    return False


def is_enabled(values: dict) -> bool:
    """当前是否真在解析归属地"""
    return is_configured(values)


def _from_tencent(db: Session, ip: str) -> dict:
    key = _value(db, "geoip_tencent_key")
    if not key:
        return {"ok": False, "message": "未配置腾讯地图 Key"}
    import httpx

    try:
        with httpx.Client(timeout=8.0) as client:
            resp = client.get(TENCENT_URL, params={"ip": ip, "key": key})
        body = resp.json() if resp.status_code == 200 else {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"腾讯地图不可达：{type(exc).__name__}: {exc}"}
    if body.get("status") != 0:
        return {"ok": False, "message": f"定位失败：{body.get('message') or f'HTTP {resp.status_code}'}"}
    result = body.get("result") or {}
    ad_info = result.get("ad_info") or {}
    # nation 单独留着给「只允许国内访问」这类**国家级**判定用：region 里
    # 「中国」是被摘掉的（那是绝大多数请求，留在串里只会把 region 撑长）。
    # 这里只加字段、不改 region 的算法，老调用方（防共享 / 登录日志）行为不变。
    country = str(ad_info.get("nation") or "").strip()
    parts = [ad_info.get("nation"), ad_info.get("province"), ad_info.get("city"),
             ad_info.get("district")]
    region = " ".join([str(p) for p in parts if p and str(p) != "中国"]) or "未知"
    return {
        "ok": True,
        "ip": ip,
        "region": region,
        "country": country,
        "isp": ad_info.get("isp") or "",
        "source": "tencent",
        "detail": {"location": result.get("location") or {}},
    }


def _from_mmdb(db: Session, ip: str) -> dict:
    import os

    path = _value(db, "geoip_mmdb_path")
    if not path:
        return {"ok": False, "message": "未配置 MMDB 文件路径"}
    # 先看文件再看依赖：路径填错时更需要被告知「文件不存在」，而不是被误导去装依赖
    if not os.path.exists(path):
        return {"ok": False, "message": f"库文件不存在：{path}"}
    try:
        import maxminddb  # 可选依赖：只在本地方案下需要
    except ImportError:
        return {"ok": False, "message": "本地方案需要 maxminddb 依赖（pip install maxminddb）"}
    try:
        with maxminddb.open_database(path) as reader:
            record = reader.get(ip) or {}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "message": f"查询失败：{type(exc).__name__}: {exc}"}

    def _name(node: dict) -> str:
        names = (node or {}).get("names") or {}
        return names.get("zh-CN") or names.get("en") or ""

    subdivisions = record.get("subdivisions") or []
    country = _name(record.get("country"))
    parts = [
        country,
        _name(subdivisions[0]) if subdivisions else "",
        _name(record.get("city")),
    ]
    region = " ".join([p for p in parts if p]) or "未知"
    return {"ok": True, "ip": ip, "region": region, "country": country,
            "isp": "", "source": "mmdb", "detail": {}}


def lookup(db: Session, ip: str, *, use_cache: bool = True) -> dict:
    """查询单个 IP 的归属地（带缓存）"""
    ip = (ip or "").strip()
    if not ip:
        return {"ok": False, "message": "没有 IP"}
    name = provider(db)
    if name == "none":
        return {"ok": False, "message": "未配置 IP 归属地提供方"}
    now = time.time()
    if use_cache:
        hit = _cache.get(ip)
        if hit and now - hit[0] < hit[2]:
            return dict(hit[1], cached=True)
    result = _from_tencent(db, ip) if name == "tencent" else _from_mmdb(db, ip)
    if use_cache:
        ttl = _cache_ttl(db)
        if not result.get("ok"):
            # 查不到 / 接口报错也要缓存，否则每次登录都去问一次外部接口
            ttl = min(ttl, _FAILURE_TTL)
        if len(_cache) >= _MAX_CACHE:
            _cache.clear()
        _cache[ip] = (now, result, ttl)
    return dict(result, cached=False)


def _cache_ttl(db: Session) -> float:
    try:
        hours = int(_value(db, "geoip_cache_hours", "24") or 24)
    except ValueError:
        hours = 24
    return max(0, hours) * 3600


def region_of(db: Session, ip: str) -> str:
    """只取地区串（查不到返回空串），给日志列表批量用"""
    result = lookup(db, ip)
    return result.get("region", "") if result.get("ok") else ""


def test(db: Session, payload: dict) -> dict:
    ip = (payload.get("ip") or "8.8.8.8").strip()
    result = lookup(db, ip, use_cache=False)
    if not result.get("ok"):
        return {"ok": False, "message": result.get("message", "查询失败"), "detail": {"ip": ip}}
    return {"ok": True, "message": f"{ip} → {result['region']}"
                                   f"{'（' + result['isp'] + '）' if result.get('isp') else ''}",
            "detail": {"ip": ip, "source": result.get("source"), "raw": result.get("detail")}}
