"""软件授权客户端校验（发行版专用，默认关）。

口径（2026-11-07 授权售卖方案定版，aetrix-license-plan-20261107
第四节之 3「校验：回连验真，但绝不掐脖子」）：

- 总开关 env ``AETRIX_LICENSE_ENFORCE``，**默认关**：关时本模块一切
  入口都是空操作——不发任何请求、不读令牌、播放不拦。自家生产走
  源码部署、永不受影响；只有发行包（scripts/build_release.sh 产出
  的 docker-compose）默认把开关打开。
- 开时：启动后由后台线程异步做一次校验（绝不阻塞启动，授权服务
  连不上不许拖垮应用），之后每 24 小时复验。无缓存令牌先走
  ``/api/v1/activate`` 激活绑定，已有令牌走 ``/api/v1/verify``。
- 验过的签名令牌缓存在 system_configs（``license_token``），播放
  判定只做本地验签 + 看到期，**不联网**。授权服务连不上/临时故障
  时用缓存：令牌到期后 ``grace_days``（服务端签发时写入，默认 7）
  天宽限内一切照常。
- 无效（码不存在 / 被停用 / 已过期且过宽限 / 绑定不匹配 / 令牌验
  签不过）只拦「开始播放」一件事：执法点在 playback_gate 的三个
  ensure_* 入口（与绑定门禁同一层），浏览、登录、管理后台、填新
  码全部不受影响；填入有效码并校验通过后立刻恢复。
- 机器指纹：首次生成（机器标识 / MAC / 主机名组合 + 随机盐的
  SHA256）后落库（``license_fingerprint``），后续一直复用，容器
  重启不变；只把哈希发出去。
- 隐私：校验请求只传授权码、机器指纹哈希、绑定域名、版本号四样，
  绝不传任何用户与片库数据（tests/test_license_client.py 白名单
  断言守住）。
- 到期提醒：到期前 30 / 7 / 1 天分级，管理端授权区展示（一期只
  做站内，见 backend/api/admin_license.py）。

服务端契约（~/workspace/license-server/license_service/api.py）：
POST /api/v1/activate|verify，请求体 {code, domain, machine_fp,
version}；成功 {ok, license:{code,tier,domain,fp_sha256,issued_at,
expires_at,grace_days}, signature}，signature 是 Ed25519 对载荷
canonical JSON（sort_keys + 紧凑分隔符）的签名（base64）；失败
{ok:false, reason, message}，reason ∈ not_found / disabled /
expired / not_activated / binding_mismatch / bad_request /
rate_limited。GET /api/v1/pubkey 给验签公钥。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import platform
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend.integrations import store

logger = logging.getLogger(__name__)

# ------------------------------------------------------------- 配置

LICENSE_ENFORCE_ENV = "AETRIX_LICENSE_ENFORCE"
LICENSE_SERVER_ENV = "AETRIX_LICENSE_SERVER"
LICENSE_PUBKEY_ENV = "AETRIX_LICENSE_PUBKEY"
LICENSE_CODE_ENV = "AETRIX_LICENSE_CODE"
LICENSE_DOMAIN_ENV = "AETRIX_LICENSE_DOMAIN"

DEFAULT_SERVER = "https://license.135505.autos"
#: 验签公钥默认值：与授权服务线上 GET /api/v1/pubkey 实测一致
#: （2026-11-07）；服务端换钥时用 env AETRIX_LICENSE_PUBKEY 覆盖，
#: tests 里有守卫断言防漂移。
DEFAULT_PUBKEY_B64 = "3dn939gNHaoWyqWNcaA8/ZY2jWJTrqjCspIHLthKt+o="

#: system_configs 键（授权码与绑定域名由管理端填，env 作兜底）
KEY_CODE = "license_code"
KEY_DOMAIN = "license_domain"
KEY_FINGERPRINT = "license_fingerprint"
KEY_TOKEN = "license_token"
KEY_LAST_CHECKED_AT = "license_last_checked_at"
KEY_LAST_RESULT = "license_last_result"

GRACE_DAYS_FALLBACK = 7
VERIFY_INTERVAL_SECONDS = 24 * 60 * 60
STARTUP_DELAY_SECONDS = 20.0
HTTP_TIMEOUT_SECONDS = 8.0
STATE_CACHE_SECONDS = 30.0

#: 拦截时的大白话（只出现在开始播放处，指路管理后台自救）
BLOCK_MESSAGES = {
    "no_code": "软件授权未激活：请管理员在管理后台「系统设置 → 软件授权」填入授权码后重试",
    "no_domain": "软件授权未激活：请管理员在管理后台「系统设置 → 软件授权」填入绑定域名后重试",
    "not_activated": "软件授权尚未完成激活：请管理员在管理后台「系统设置 → 软件授权」点「立即校验」，或稍后再试",
    "disabled": "本机软件授权已被停用，暂时不能播放；请联系授权提供方处理",
    "not_found": "授权码不存在或已被删除，暂时不能播放；请管理员在管理后台核对授权码",
    "expired": "本机软件授权已到期并超过宽限期，暂时不能播放；续费后请管理员在管理后台点「立即校验」",
    "binding_mismatch": "本机授权绑定信息不匹配（域名或机器已变更），暂时不能播放；请联系授权提供方重置绑定后重新校验",
    "invalid_token": "本机授权凭证校验未通过，暂时不能播放；请管理员在管理后台点「立即校验」",
}
BLOCK_MESSAGE_FALLBACK = "软件授权校验未通过，暂时不能播放；请管理员在管理后台「系统设置 → 软件授权」处理"

#: 服务端明确撤销类 reason：即便缓存令牌没到期，得知后立即生效。
#: expired 不在此列——过期后的宽限正是靠本地令牌的 expires_at +
#: grace_days 计算，服务端回 expired 不额外收紧。
_REVOKE_REASONS = frozenset({"disabled", "not_found"})


def enforced() -> bool:
    """总开关：env AETRIX_LICENSE_ENFORCE ∈ {1,true,yes,on} 才算开。"""
    return (os.environ.get(LICENSE_ENFORCE_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def server_url() -> str:
    return (os.environ.get(LICENSE_SERVER_ENV) or "").strip() or DEFAULT_SERVER


def pubkey_b64() -> str:
    return (os.environ.get(LICENSE_PUBKEY_ENV) or "").strip() or DEFAULT_PUBKEY_B64


def normalize_domain(raw: str) -> str:
    """域名归一化：与授权服务端 util.normalize_domain 同口径。"""
    d = (raw or "").strip().lower()
    d = re.sub(r"^https?://", "", d)
    d = d.split("/")[0].split("?")[0]
    d = d.split(":")[0]
    return d.rstrip(".")


def configured_code(db: Session) -> str:
    """授权码：system_configs（管理端填）优先，env 兜底。"""
    value = store.get_value(db, KEY_CODE, "")
    if value and value.strip():
        return value.strip().upper()
    return (os.environ.get(LICENSE_CODE_ENV) or "").strip().upper()


def configured_domain(db: Session) -> str:
    """绑定域名：system_configs 优先，env 兜底；统一归一化。"""
    value = store.get_value(db, KEY_DOMAIN, "")
    if value and value.strip():
        return normalize_domain(value)
    return normalize_domain(os.environ.get(LICENSE_DOMAIN_ENV) or "")


# ------------------------------------------------------------- 时间

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text) -> Optional[datetime]:
    if not text:
        return None
    try:
        return datetime.strptime(
            str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------- 指纹

def _fingerprint_material() -> str:
    parts = []
    for path in ("/etc/machine-id",
                 "/sys/class/dmi/id/product_uuid"):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                value = fh.read().strip()
            if value:
                parts.append(value)
        except OSError:
            pass
    try:
        parts.append(str(uuid.getnode()))
    except Exception:  # noqa: BLE001
        pass
    try:
        parts.append(platform.node() or "")
    except Exception:  # noqa: BLE001
        pass
    return "|".join(p for p in parts if p)


def machine_fingerprint(db: Session) -> str:
    """机器指纹哈希：首次生成后落库，之后一直复用（容器重启不变）。

    生成 = SHA256(机器标识/MAC/主机名组合 + 随机盐)。随机盐保证
    模板机克隆、机器标识缺失时也不会与别人撞指纹；真正让它稳定
    的是落库复用这一步。只把哈希发给授权服务，原文不出本机。
    """
    existing = store.read_value(db, KEY_FINGERPRINT, "")
    if existing and existing.strip():
        return existing.strip()
    material = _fingerprint_material()
    fp = hashlib.sha256(
        f"{material}|{secrets.token_hex(16)}".encode("utf-8")).hexdigest()
    store.write_values(
        db, {KEY_FINGERPRINT: fp},
        descriptions={KEY_FINGERPRINT: "软件授权机器指纹哈希（首次生成后固定复用）"})
    db.commit()
    return fp


def _fingerprint_sha(fp: str) -> str:
    """服务端落库口径：fp_sha256 = SHA256(客户端发去的 machine_fp)。"""
    return hashlib.sha256((fp or "").encode("utf-8")).hexdigest()


# ------------------------------------------------------------- 令牌

def canonical_bytes(payload) -> bytes:
    """签名载荷的 canonical 序列化：与授权服务端 signing.py 同口径。"""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
        ensure_ascii=False).encode("utf-8")


def verify_token_signature(payload, signature_b64,
                           pubkey_b64_value: Optional[str] = None) -> bool:
    """Ed25519 本地验签：签名对不上（篡改/换钥）一律 False，不抛。"""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
        pub = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(pubkey_b64_value or pubkey_b64()))
        pub.verify(base64.b64decode(signature_b64 or ""),
                   canonical_bytes(payload))
        return True
    except Exception:  # noqa: BLE001 — 验签失败就是失败，不区分原因
        return False


def _load_token(db: Session):
    """读缓存令牌：返回 (payload dict, signature str)，无/坏返回 (None, None)。"""
    raw = store.get_value(db, KEY_TOKEN, "")
    if not raw:
        return None, None
    try:
        data = json.loads(raw)
        payload = data.get("license")
        signature = data.get("signature")
        if not isinstance(payload, dict) or not signature:
            return None, None
        return payload, str(signature)
    except (ValueError, AttributeError):
        return None, None


def _save_token(db: Session, payload, signature: str) -> None:
    store.write_values(
        db, {KEY_TOKEN: json.dumps(
            {"license": payload, "signature": signature})},
        descriptions={KEY_TOKEN: "软件授权签名令牌缓存（JSON）"})


def _record_check(db: Session, now: datetime, result: str) -> None:
    store.write_values(db, {
        KEY_LAST_CHECKED_AT: _iso(now),
        KEY_LAST_RESULT: result,
    })
    db.commit()
    invalidate_state_cache()


# ------------------------------------------------------------- 状态评估

_state_cache = {"at": 0.0, "key": None, "value": None}
_state_cache_lock = threading.Lock()


def invalidate_state_cache() -> None:
    with _state_cache_lock:
        _state_cache["at"] = 0.0
        _state_cache["key"] = None
        _state_cache["value"] = None


def _blocked(reason: str, **extra) -> dict:
    state = {
        "state": "blocked", "allowed": False, "reason": reason,
        "in_grace": False, "tier": None, "expires_at": None,
        "days_left": None, "reminder": None,
    }
    state.update(extra)
    return state


def _evaluate_fresh(db: Session, now: datetime) -> dict:
    """状态机本体：只读本地数据（缓存令牌 + 配置），不联网不写库。"""
    base = {
        "enforced": True,
        "server": server_url(),
        "code": configured_code(db),
        "domain": configured_domain(db),
        "last_checked_at": store.get_value(db, KEY_LAST_CHECKED_AT, ""),
        "last_result": store.get_value(db, KEY_LAST_RESULT, ""),
    }
    if not base["code"]:
        return {**base, **_blocked("no_code")}
    if not base["domain"]:
        return {**base, **_blocked("no_domain")}

    payload, signature = _load_token(db)
    if payload is None:
        # 从未拿到令牌：若服务端明确回过撤销类结论，照结论拦
        if base["last_result"] in _REVOKE_REASONS:
            return {**base, **_blocked(base["last_result"])}
        return {**base, **_blocked("not_activated")}
    if not verify_token_signature(payload, signature):
        return {**base, **_blocked("invalid_token")}

    # 绑定核对：令牌必须绑的就是本机（码 / 域名 / 指纹三样对上）
    fp = store.get_value(db, KEY_FINGERPRINT, "")
    if (str(payload.get("code") or "").upper() != base["code"]
            or normalize_domain(str(payload.get("domain") or ""))
            != base["domain"]
            or (fp and payload.get("fp_sha256") != _fingerprint_sha(fp))):
        return {**base, **_blocked(
            "binding_mismatch",
            tier=payload.get("tier"),
            expires_at=payload.get("expires_at"))}

    # 服务端撤销（停用 / 码被删）：复验时间晚于令牌签发时间才算数，
    # 防止旧记录误伤刚换好的新令牌。
    issued_at = _parse_iso(payload.get("issued_at"))
    last_checked = _parse_iso(base["last_checked_at"])
    if (base["last_result"] in _REVOKE_REASONS
            and last_checked is not None and issued_at is not None
            and last_checked >= issued_at):
        return {**base, **_blocked(
            base["last_result"],
            tier=payload.get("tier"),
            expires_at=payload.get("expires_at"))}

    expires_at = _parse_iso(payload.get("expires_at"))
    if expires_at is None:
        return {**base, **_blocked(
            "invalid_token", tier=payload.get("tier"))}
    grace_days = payload.get("grace_days")
    if not isinstance(grace_days, int) or grace_days < 0:
        grace_days = GRACE_DAYS_FALLBACK

    common = {"tier": payload.get("tier"),
              "expires_at": payload.get("expires_at")}
    if now <= expires_at:
        days_left = (expires_at - now).days
        reminder = None
        if days_left <= 1:
            reminder = "1"
        elif days_left <= 7:
            reminder = "7"
        elif days_left <= 30:
            reminder = "30"
        return {**base, **common, "state": "ok", "allowed": True,
                "reason": "", "in_grace": False,
                "days_left": days_left, "reminder": reminder}
    if now <= expires_at + timedelta(days=grace_days):
        return {**base, **common, "state": "grace", "allowed": True,
                "reason": "", "in_grace": True,
                "days_left": 0, "reminder": "grace"}
    return {**base, **_blocked("expired", **common)}


def evaluate(db: Session, now: Optional[datetime] = None) -> dict:
    """当前授权状态（播放判定与管理端展示共用同一份）。

    enforce 关 → 恒为 disabled/allowed。开时走本地评估；无显式
    ``now`` 时带 30 秒进程内缓存（播放热路径不反复验签），保存
    配置 / 完成校验会主动失效缓存。
    """
    if not enforced():
        return {
            "enforced": False, "state": "disabled", "allowed": True,
            "reason": "", "in_grace": False, "tier": None,
            "expires_at": None, "days_left": None, "reminder": None,
            "server": server_url(),
            "code": "", "domain": "",
            "last_checked_at": "", "last_result": "",
        }
    if now is not None:
        return _evaluate_fresh(db, now)
    payload, _sig = _load_token(db)
    cache_key = (
        configured_code(db), configured_domain(db),
        json.dumps(payload, sort_keys=True) if payload else "",
        store.get_value(db, KEY_LAST_RESULT, ""),
        store.get_value(db, KEY_LAST_CHECKED_AT, ""),
    )
    with _state_cache_lock:
        if (_state_cache["key"] == cache_key
                and time.monotonic() - _state_cache["at"]
                < STATE_CACHE_SECONDS):
            return dict(_state_cache["value"])
    value = _evaluate_fresh(db, _now())
    with _state_cache_lock:
        _state_cache.update(
            {"at": time.monotonic(), "key": cache_key, "value": value})
    return dict(value)


def ensure_playback_allowed(db: Session) -> None:
    """开始播放的授权闸门：无效时抛 403 + 大白话；其余一律放行。

    enforce 关时第一行就返回（零行为变化）；判读只用本地缓存
    令牌，不联网——授权服务抖动永远不会直接打到播放路径上。
    """
    if not enforced():
        return
    state = evaluate(db)
    if state["allowed"]:
        return
    reason = state.get("reason") or ""
    raise HTTPException(
        status_code=403,
        detail=BLOCK_MESSAGES.get(reason, BLOCK_MESSAGE_FALLBACK))


def admin_status(db: Session) -> dict:
    """管理端授权区要展示的全部信息（状态 + 配置回显）。"""
    state = evaluate(db)
    if not state["enforced"]:
        # 关时也回显已保存的码/域名，方便发行前预填与排查
        state = {**state,
                 "code": configured_code(db),
                 "domain": configured_domain(db),
                 "last_checked_at": store.get_value(
                     db, KEY_LAST_CHECKED_AT, ""),
                 "last_result": store.get_value(db, KEY_LAST_RESULT, "")}
    return state


# ------------------------------------------------------------- 回连校验

def _default_poster(url: str, body: dict, timeout: float):
    """真实 HTTP POST（JSON）：返回 (status, dict)；网络层错误抛异常。"""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(
                resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            payload = {}
        return exc.code, payload


def build_request_body(db: Session) -> dict:
    """校验请求体：只许四类字段（授权码 / 指纹哈希 / 域名 / 版本）。

    隐私红线：绝不带任何用户、片库、播放数据——白名单在此一处
    构造，tests 断言键集合恒等。
    """
    from backend.version import app_version

    return {
        "code": configured_code(db),
        "domain": configured_domain(db),
        "machine_fp": machine_fingerprint(db),
        "version": app_version(),
    }


def refresh(db: Session, *, poster: Optional[Callable] = None,
            now: Optional[datetime] = None, force: bool = False) -> dict:
    """向授权服务做一次校验并落缓存，返回最新状态。

    - enforce 关且非 force：空操作（零请求），返回 disabled 状态。
      force 只供管理端「立即校验」/保存后即时校验这种管理员显式
      动作使用，自动路径（后台线程、播放）永不带 force。
    - 无缓存令牌 → activate（首次绑定）；有 → verify。
    - 网络异常：记 last_result=unreachable，保留旧令牌——放行与否
      由 evaluate 按令牌到期 + 宽限决定，不在这里一刀切。
    """
    now = now or _now()
    if not enforced() and not force:
        return evaluate(db, now=now)
    code = configured_code(db)
    domain = configured_domain(db)
    if not code:
        _record_check(db, now, "no_code")
        return evaluate(db, now=now)
    if not domain:
        _record_check(db, now, "no_domain")
        return evaluate(db, now=now)

    poster = poster or _default_poster
    body = build_request_body(db)
    payload, _sig = _load_token(db)
    action = "verify" if payload is not None else "activate"
    url = f"{server_url().rstrip('/')}/api/v1/{action}"
    try:
        status, data = poster(url, body, HTTP_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — 连不上是常态分支，不许冒泡
        logger.warning("授权校验连不上授权服务（%s）: %s", action, exc)
        _record_check(db, now, "unreachable")
        return evaluate(db, now=now)

    if status == 200 and data.get("ok") and data.get("license"):
        _save_token(db, data["license"], str(data.get("signature") or ""))
        db.commit()
        _record_check(db, now, "ok")
    else:
        reason = str(data.get("reason") or f"http_{status}")
        logger.warning("授权校验未通过（%s）: %s", action, reason)
        _record_check(db, now, reason)
    return evaluate(db, now=now)


# ------------------------------------------------------------- 后台线程

_started = False
_start_lock = threading.Lock()


def _run_loop() -> None:
    """启动后先等一小会儿再首验，之后每 24 小时复验一次。

    全程吞异常：授权服务或本机 DB 出任何问题都不许影响主进程，
    状态变化只体现在 evaluate 的结果里。
    """
    time.sleep(STARTUP_DELAY_SECONDS)
    while True:
        try:
            from backend.database import SessionLocal

            with SessionLocal() as db:
                refresh(db)
        except Exception as exc:  # noqa: BLE001
            logger.warning("授权后台校验异常（可忽略）: %s", exc)
        time.sleep(VERIFY_INTERVAL_SECONDS)


def start() -> bool:
    """启动后台复验线程（幂等）。enforce 关时不起线程、返回 False。"""
    global _started
    if not enforced():
        return False
    with _start_lock:
        if _started:
            return False
        _started = True
    thread = threading.Thread(
        target=_run_loop, name="license-verify", daemon=True)
    thread.start()
    logger.info("软件授权后台校验已启动（每 24 小时复验一次）")
    return True


__all__ = [
    "LICENSE_ENFORCE_ENV", "LICENSE_SERVER_ENV", "LICENSE_PUBKEY_ENV", "LICENSE_CODE_ENV", "LICENSE_DOMAIN_ENV",
    "DEFAULT_SERVER", "DEFAULT_PUBKEY_B64",
    "KEY_CODE", "KEY_DOMAIN", "KEY_FINGERPRINT", "KEY_TOKEN",
    "KEY_LAST_CHECKED_AT", "KEY_LAST_RESULT",
    "BLOCK_MESSAGES", "enforced", "server_url", "pubkey_b64",
    "normalize_domain", "configured_code", "configured_domain",
    "machine_fingerprint", "canonical_bytes", "verify_token_signature",
    "evaluate", "ensure_playback_allowed", "admin_status",
    "build_request_body", "refresh", "start", "invalidate_state_cache",
]
