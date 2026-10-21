"""能力：网络代理（后端出站请求统一走代理）

用途：服务器在墙内 / 出口受限时，让后端访问外部服务（TMDB、115、网盘、AI、地图…）走一个
管理员自己填的代理。项目不内置任何代理地址。

落地方式：写进程环境变量（``HTTP_PROXY`` / ``HTTPS_PROXY`` / ``ALL_PROXY`` / ``NO_PROXY``）——
httpx（默认 ``trust_env=True``）、requests、rclone 都认这套变量，因此**不需要改各个调用点**；
关闭时把这些变量清干净，避免残留。
"""
from __future__ import annotations

import os
from typing import Optional

from sqlalchemy.orm import Session

SPEC = {
    "title": "网络代理",
    "desc": "为后端外部请求配置 SOCKS5 / HTTP / HTTPS 代理，并测试连通性。",
    "group": "网络与安全",
    "docs_hint": "留空端口按协议默认（http=8080 / https=443 / socks5=1080）。",
    "fields": [
        {"key": "proxy_enabled", "label": "启用代理", "type": "bool", "default": "false",
         "hint": "关闭后立即清掉进程代理，恢复直连"},
        {"key": "proxy_scheme", "label": "代理协议", "type": "select", "default": "http",
         "options": ["http", "https", "socks5", "socks5h"]},
        {"key": "proxy_host", "label": "代理地址", "type": "str", "required": True,
         "placeholder": "127.0.0.1 或 proxy.example.com"},
        {"key": "proxy_port", "label": "端口", "type": "int", "default": "0"},
        {"key": "proxy_username", "label": "用户名", "type": "str", "default": ""},
        {"key": "proxy_password", "label": "密码", "type": "secret", "default": ""},
        {"key": "proxy_no_proxy", "label": "不走代理的地址", "type": "str", "default": "localhost,127.0.0.1",
         "hint": "逗号分隔：内网域名、Emby 上游、媒体库所在 NAS 等"},
    ],
    "test_label": "测试代理",
}

_DEFAULT_PORT = {"http": 8080, "https": 443, "socks5": 1080, "socks5h": 1080}
ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "all_proxy", "no_proxy")


def _read(db: Session, key: str, default: str = "") -> str:
    """统一热读（只许这一套）：短 TTL 缓存，保存时失效"""
    from backend.integrations import store

    return store.get_value(db, key, default).strip()


def is_configured(values: dict) -> bool:
    """填了代理地址才算配过（不看开关）"""
    return bool(str(values.get("proxy_host") or "").strip())


def is_enabled(values: dict) -> bool:
    """当前出站请求是否真的在走代理"""
    return (str(values.get("proxy_enabled") or "").lower() == "true"
            and is_configured(values))


def build_proxy_url(db: Session) -> str:
    """拼出代理 URL（未启用或没填地址时返回空串）"""
    if _read(db, "proxy_enabled", "false").lower() != "true":
        return ""
    host = _read(db, "proxy_host")
    if not host:
        return ""
    scheme = _read(db, "proxy_scheme", "http") or "http"
    port = _read(db, "proxy_port", "0") or "0"
    try:
        port_num = int(port) or _DEFAULT_PORT.get(scheme, 0)
    except ValueError:
        port_num = _DEFAULT_PORT.get(scheme, 0)
    user = _read(db, "proxy_username")
    password = _read(db, "proxy_password")
    auth = ""
    if user:
        # 用户名/密码里出现 @ : 等字符时按 URL 规范转义
        from urllib.parse import quote

        auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if password else f"{quote(user, safe='')}@"
    return f"{scheme}://{auth}{host}:{port_num}"


def apply(db: Session) -> None:
    """把配置写进进程环境变量（或清掉）"""
    url = build_proxy_url(db)
    no_proxy = _read(db, "proxy_no_proxy", "localhost,127.0.0.1")
    if url:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            os.environ[key] = url
        os.environ["NO_PROXY"] = no_proxy
        os.environ["no_proxy"] = no_proxy
    else:
        for key in ENV_KEYS:
            os.environ.pop(key, None)


def signature() -> str:
    """当前进程代理环境的指纹（只读 ``os.environ``，不查库、不建连接）

    给**长生命周期客户端**用的：httpx 只在构造 client 时读一遍代理环境变量，建好的
    连接池会一直用旧出口，所以像 TMDB 刮削客户端（进程级单例）这种对象必须能察觉
    「代理被改了」。``apply()`` 写的正是这几个变量，因此指纹变化 ⟺ 代理配置变了。
    """
    return "|".join(f"{key}={os.environ.get(key, '')}" for key in ENV_KEYS)


def test(db: Session, payload: dict) -> dict:
    """真实走一次代理：拿一个极小的公共端点验证出口是否可用"""
    import httpx

    url = build_proxy_url(db)
    target = (payload.get("target") or "").strip() or "https://www.gstatic.com/generate_204"
    if not url:
        return {"ok": False, "message": "未启用代理或未填代理地址（保存后再测）",
                "detail": {"target": target}}
    try:
        with httpx.Client(proxy=url, timeout=8.0) as client:
            resp = client.get(target)
        ok = resp.status_code < 400
        return {
            "ok": ok,
            "message": f"代理可用：HTTP {resp.status_code}（{target}）" if ok
                       else f"代理连上了，但目标返回 HTTP {resp.status_code}",
            "detail": {"proxy": _masked(url), "target": target, "status": resp.status_code},
        }
    except Exception as exc:  # noqa: BLE001 — 把真实原因报给管理员
        return {"ok": False, "message": f"走代理失败：{type(exc).__name__}: {exc}",
                "detail": {"proxy": _masked(url), "target": target}}


def _masked(url: str) -> str:
    if "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    _, host = rest.split("@", 1)
    return f"{scheme}://******@{host}"

