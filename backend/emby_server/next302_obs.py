"""302 Next F4：观测日志（脱敏是红线）。

每步留痕，但日志里出现令牌全文即事故：
- 密钥类字段（token / key / secret / password / auth / cookie /
  bearer / apikey，大小写不敏感、子串匹配）一律丢弃，不记、不截断留尾巴。
- 文件身份类字段（file_id / drive_id / variant_file_id /
  source_file_id / pickcode）只记 sha256 前 12 位哈希，不记原文、
  不记前缀——前缀也是身份信息的一部分，哈希才能既可关联又不可逆。
- 调用方不许传嵌套 dict 里藏秘密：本模块只脱敏一层，嵌套值原样
  落盘前会被压成 ``"<nested>"``，防止秘密借嵌套溜过去。
- 值超长截断到 500 字，防止一条日志把盘打爆。

计数器（进程内）：各事件累计数，供管理端看板与灰度观测读；
计数丢了可重建，不作账单依据。
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading

logger = logging.getLogger("next302")

_SECRET_SUBSTRINGS = ("token", "key", "secret", "password", "auth",
                      "cookie", "bearer", "apikey", "api_key")
_ID_KEYS = ("file_id", "drive_id", "variant_file_id", "source_file_id",
            "pickcode", "drive_file_id")
_MAX_VALUE_LEN = 500

_counters: dict[str, int] = {}
_counters_lock = threading.Lock()


def _is_secret_key(name: str) -> bool:
    lowered = name.lower()
    return any(sub in lowered for sub in _SECRET_SUBSTRINGS)


def _hash_id(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def redact(fields: dict) -> dict:
    """脱敏一层字段。返回新 dict，不改入参。"""
    out = {}
    for name, value in (fields or {}).items():
        if _is_secret_key(str(name)):
            continue
        if str(name) in _ID_KEYS:
            out[str(name)] = f"sha256:{_hash_id(value)}"
            continue
        if isinstance(value, dict):
            out[str(name)] = "<nested>"
            continue
        text = value if isinstance(value, str) else json.dumps(
            value, ensure_ascii=False, default=str)
        if len(text) > _MAX_VALUE_LEN:
            text = text[:_MAX_VALUE_LEN] + "…(truncated)"
        out[str(name)] = text
    return out


def log_event(event: str, **fields: object) -> dict:
    """记一条观测日志并累计计数。返回脱敏后的字段（供测试断言）。"""
    clean = redact(dict(fields))
    with _counters_lock:
        _counters[event] = _counters.get(event, 0) + 1
    logger.info("next302 event=%s %s", event,
                json.dumps(clean, ensure_ascii=False))
    return clean


def get_counters() -> dict:
    """计数器快照（拷贝，调用方改不动内部）。"""
    with _counters_lock:
        return dict(_counters)
