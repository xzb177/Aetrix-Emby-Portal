from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from typing import Any

from backend import database as dbmod
from backend.integrations import store

logger = logging.getLogger(__name__)

TOKEN_TTL = 600
RATE_LIMIT_PER_HOUR = 10

# Redis 不可用时的内存兜底
_mem_lock = threading.Lock()
_mem_tokens: dict[str, tuple[dict[str, Any], float]] = {}  # token -> (payload, 过期时间戳)
_mem_rate: dict[int, list[float]] = {}  # user_id -> [时间戳] 滑窗 1 小时


def _redis():
    """获取 redis 客户端，不可用时返回 None"""
    try:
        return getattr(dbmod, "redis_client", None)
    except Exception:
        return None


def _allow_create(user_id: int) -> bool:
    """限流检查：未超限返回 True（Redis 计数失败时回退内存滑窗）"""
    client = _redis()
    if client is not None:
        try:
            key = f"tg_login_token_rate:{user_id}"
            count = client.incr(key)
            if count == 1:
                client.expire(key, 3600)
            return count <= RATE_LIMIT_PER_HOUR
        except Exception as exc:
            logger.warning("tg_login_token Redis 计数失败，回退内存限流: %s", exc)
    now = time.time()
    with _mem_lock:
        window = _mem_rate.setdefault(user_id, [])
        window[:] = [ts for ts in window if now - ts < 3600]
        if len(window) >= RATE_LIMIT_PER_HOUR:
            return False
        window.append(now)
        return True


def create_login_token(user_id: int, telegram_id: int) -> str | None:
    """创建一键免密登录一次性 token（限流内），失败返回 None"""
    try:
        if not _allow_create(user_id):
            logger.info("tg_login_token 限流触发 user_id=%s", user_id)
            return None
        token = secrets.token_urlsafe(32)
        payload = {"user_id": user_id, "telegram_id": telegram_id, "created_at": time.time()}
        payload_json = json.dumps(payload)
        client = _redis()
        if client is not None:
            try:
                client.setex(f"tg_login_token:{token}", TOKEN_TTL, payload_json)
            except Exception as exc:
                logger.warning("tg_login_token Redis 写入失败，回退内存存储: %s", exc)
                with _mem_lock:
                    _mem_tokens[token] = (payload, time.time() + TOKEN_TTL)
        else:
            with _mem_lock:
                _mem_tokens[token] = (payload, time.time() + TOKEN_TTL)
            logger.warning("Redis 不可用，tg_login_token 使用内存存储")
        # 审计日志：仅记录 token 前 6 位，不记明文
        logger.info(
            "tg_login_token 创建 user_id=%s telegram_id=%s token=%s",
            user_id, telegram_id, token[:6],
        )
        return token
    except Exception as exc:
        logger.error("tg_login_token 创建失败: %s", exc)
        return None


def consume_login_token(token: str) -> dict | None:
    """原子消费一次性 token（取删），无效/过期返回 None，否则返回 payload dict"""
    try:
        token = (token or "").strip()
        if not token:
            return None
        client = _redis()
        if client is not None:
            try:
                raw = None
                if hasattr(client, "getdel"):
                    raw = client.getdel(f"tg_login_token:{token}")
                else:
                    raw = client.get(f"tg_login_token:{token}")
                    if raw is not None:
                        client.delete(f"tg_login_token:{token}")
                if raw is None:
                    return None
                payload = json.loads(raw)
                return payload if isinstance(payload, dict) else None
            except Exception as exc:
                logger.warning("tg_login_token Redis 读取失败，回退内存: %s", exc)
        with _mem_lock:
            item = _mem_tokens.pop(token, None)
        if item is None:
            return None
        payload, expire_ts = item
        if time.time() > expire_ts:
            return None
        return payload
    except Exception as exc:
        logger.error("tg_login_token 消费失败: %s", exc)
        return None


def build_login_url(db, user) -> str | None:
    """基于站点地址与一次性 token 构建一键登录链接，无法构建返回 None"""
    try:
        # r40：站点地址只许一套键。``site_url`` 是正统键（支付设置「站点地址」，
        # 有后台写入通道）；``site_base_url`` 是历史遗留键（无任何写入通道，
        # 仅作回退兼容手写过该键的库）。
        base = (
            store.get_value(db, "site_url", "")
            or store.get_value(db, "site_base_url", "")
            or ""
        ).rstrip("/")
        if not base:
            return None
        token = create_login_token(user.id, user.telegram_id)
        if token is None:
            return None
        return f"{base}/tg-login?token={token}"
    except Exception as exc:
        logger.error("tg_login_url 构建失败: %s", exc)
        return None
