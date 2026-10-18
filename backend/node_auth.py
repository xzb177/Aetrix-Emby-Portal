"""面板 ↔ 节点（EM ↔ EA / 流节点）之间的身份证明（安全修复 S3）

此前 EM 直接把 ``SECRET_KEY``（JWT 与播放签名的根密钥）放进请求头 ``X-Panel-Key``，
发给管理员在后台填写的任意「服务器地址」，还跟随重定向——运营把地址改成自己的主机
就能拿到根密钥，伪造任意用户（含超管）的 JWT、拉走 ``/api/admin/stream-nodes/bundle``。

现在：

1. **独立的节点密钥**：环境变量 ``NODE_SHARED_SECRET``；未设置时回退为
   ``HMAC-SHA256(SECRET_KEY, "aetrix-node-auth")``（十六进制）。两端 ``SECRET_KEY`` 本来就
   一致，所以不配新变量也能互认——但派生值与 ``SECRET_KEY`` 不同，泄露它**不能**签 JWT。
2. **EM 发起的请求不再携带任何密钥本身**，只带签名头：
   ``X-Panel-Ts`` / ``X-Panel-Nonce`` / ``X-Panel-Sign`` =
   ``HMAC-SHA256(node_secret, "{ts}\\n{nonce}\\n{METHOD}\\n{path}")``。
   签名绑定方法与路径、5 分钟有效、nonce 进程内去重；截获的签名只能在 5 分钟内对同一路径
   用一次，换不出 bundle 等其他端点。
3. 运维脚本（``deploy-streaming-node.sh`` / curl）仍可用静态头 ``X-Panel-Key: <节点密钥>``
   ——这是运维人员自己发往自己的 EM，且发出去的是节点密钥而不是 ``SECRET_KEY``。
4. **不再接受** ``X-Panel-Key: <SECRET_KEY>``：旧版 EM / 旧脚本必须一起升级。
5. EM 侧所有探测都 ``follow_redirects=False``（见 mount_health / nodes）。

查看本机的节点密钥（给部署脚本 / curl 用）：``python -m backend.node_auth``。
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from typing import Optional
from urllib.parse import urlsplit
from backend.emby_server.env_util import env_int

PANEL_KEY_HEADER = "X-Panel-Key"
PANEL_TS_HEADER = "X-Panel-Ts"
PANEL_NONCE_HEADER = "X-Panel-Nonce"
PANEL_SIGN_HEADER = "X-Panel-Sign"

NODE_SECRET_ENV = "NODE_SHARED_SECRET"
_DERIVE_LABEL = b"aetrix-node-auth"

# 签名有效窗口（秒）：两端时钟允许的偏差
SIGN_MAX_SKEW = env_int("NODE_AUTH_MAX_SKEW", 300)

_nonce_lock = threading.Lock()
_seen_nonces: dict[str, float] = {}
_MAX_NONCES = 20000


def node_shared_secret() -> str:
    """本机的节点密钥：显式配置优先，否则由 SECRET_KEY 单向派生（永远不是 SECRET_KEY 本身）"""
    explicit = (os.getenv(NODE_SECRET_ENV) or "").strip()
    if explicit:
        return explicit
    root = (os.getenv("SECRET_KEY") or "").strip()
    if not root:
        return ""
    return hmac.new(root.encode("utf-8"), _DERIVE_LABEL, hashlib.sha256).hexdigest()


def _canonical(ts: str, nonce: str, method: str, path: str) -> bytes:
    return f"{ts}\n{nonce}\n{(method or '').upper()}\n{path or '/'}".encode("utf-8")


def _sign(secret: str, ts: str, nonce: str, method: str, path: str) -> str:
    return hmac.new(secret.encode("utf-8"), _canonical(ts, nonce, method, path),
                    hashlib.sha256).hexdigest()


def signed_headers(method: str, url: str, secret: Optional[str] = None) -> dict:
    """EM 发起请求时用的签名头（只含签名，不含任何密钥）；没有密钥时返回空 dict"""
    key = secret if secret is not None else node_shared_secret()
    if not key:
        return {}
    path = urlsplit(url).path or "/"
    ts = str(int(time.time()))
    nonce = secrets.token_hex(16)
    return {
        PANEL_TS_HEADER: ts,
        PANEL_NONCE_HEADER: nonce,
        PANEL_SIGN_HEADER: _sign(key, ts, nonce, method, path),
    }


def _remember_nonce(nonce: str, now: float) -> bool:
    """记录 nonce；已见过返回 False（重放）

    P2 修复（审查）：此前 nonce 去重是进程内 dict，多 worker 下重放可打到
    另一个 worker 绕过。现优先用 Redis（SET NX + 过期，多进程共享）；
    Redis 不可用时回退进程内 dict（单进程部署仍有效）。
    """
    # 先试 Redis（跨进程共享）
    try:
        from backend.database import redis_client
        if redis_client is not None:
            key = f"node_auth:nonce:{nonce}"
            # SET NX：已存在返回 None（重放）；不存在则设置并返回 True
            ok = redis_client.set(key, "1", nx=True, ex=SIGN_MAX_SKEW * 2)
            return bool(ok)
    except Exception:
        pass
    # 回退：进程内 dict
    with _nonce_lock:
        if len(_seen_nonces) > _MAX_NONCES:
            cutoff = now - SIGN_MAX_SKEW * 2
            for k in [k for k, v in _seen_nonces.items() if v < cutoff]:
                _seen_nonces.pop(k, None)
        if nonce in _seen_nonces:
            return False
        _seen_nonces[nonce] = now
        return True


def verify_headers(headers, method: str, path: str) -> bool:
    """校验一次请求：签名头（EM 用）或静态节点密钥头（运维脚本用），二者其一即可"""
    expected = node_shared_secret()
    if not expected:
        return False

    sign = (headers.get(PANEL_SIGN_HEADER) or "").strip()
    if sign:
        ts = (headers.get(PANEL_TS_HEADER) or "").strip()
        nonce = (headers.get(PANEL_NONCE_HEADER) or "").strip()
        if not ts or not nonce or len(nonce) > 128:
            return False
        try:
            ts_val = int(ts)
        except ValueError:
            return False
        now = time.time()
        if abs(now - ts_val) > SIGN_MAX_SKEW:
            return False
        good = _sign(expected, ts, nonce, method, path)
        if not hmac.compare_digest(good, sign):
            return False
        return _remember_nonce(nonce, now)

    provided = (headers.get(PANEL_KEY_HEADER) or "").strip()
    return bool(provided) and hmac.compare_digest(expected, provided)


def verify_signed_headers(headers, method: str, path: str) -> bool:
    """只接受 HMAC 签名头（不接受静态 X-Panel-Key）。

    P1 修复（审查）：/bundle 一类「下发根凭据」的端点，不能用长期静态
    bearer 头换走 SECRET_KEY / DATABASE_URL / SA 私钥。静态头躺在各流节点
    的 .env、部署脚本、shell 历史里，泄露面太大；签名头有时效（±5 分钟）
    + nonce 防重放 + 绑定 method+path。
    """
    expected = node_shared_secret()
    if not expected:
        return False
    sign = (headers.get(PANEL_SIGN_HEADER) or "").strip()
    ts = (headers.get(PANEL_TS_HEADER) or "").strip()
    nonce = (headers.get(PANEL_NONCE_HEADER) or "").strip()
    if not sign or not ts or not nonce or len(nonce) > 128:
        return False
    try:
        ts_val = int(ts)
    except ValueError:
        return False
    now = time.time()
    if abs(now - ts_val) > SIGN_MAX_SKEW:
        return False
    good = _sign(expected, ts, nonce, method, path)
    if not hmac.compare_digest(good, sign):
        return False
    return _remember_nonce(nonce, now)


if __name__ == "__main__":  # pragma: no cover - 运维辅助
    value = node_shared_secret()
    if not value:
        raise SystemExit("SECRET_KEY / NODE_SHARED_SECRET 都没有设置")
    print(value)
