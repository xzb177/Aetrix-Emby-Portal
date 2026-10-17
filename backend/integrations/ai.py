"""能力：AI 模型设置（OpenAI 兼容端点 + 多密钥）

只提供能力：端点、模型、密钥全部由管理员自己填，项目不内置任何 key。任何 OpenAI 兼容
服务（官方 / 中转 / 本地 Ollama、vLLM、one-api…）改 ``base_url`` 即可用。

- **多密钥**：一行一个（也支持逗号分隔），每次调用轮换，单把 key 被限流时不会整体不可用；
- **用户侧消费点**：``POST /api/user/ai/ask``（登录用户，按 ``ai_daily_limit`` 限流）；
- **测试**：真实发一次极小的 completion，把模型回显给管理员看。

配额从 v2.20.0 起**落库**（``ai_usage`` 表 + 唯一约束）：进程内字典在多进程部署下会被放大成
「上限 × 进程数」、重启还会归零；现在是同一份计数，且占用额度用的是条件 UPDATE，
并发下不会两个请求都读到 ``limit-1`` 而各记一次。
"""
from __future__ import annotations

import itertools
import logging
import threading
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from backend import models
from backend.integrations import store

logger = logging.getLogger(__name__)

SPEC = {
    "title": "AI 模型设置",
    "desc": "AI 提供商、模型、API 端点与多密钥；用于用户端「AI 客服」。",
    "group": "AI 与智能",
    "docs_hint": "OpenAI 兼容接口：Base URL 通常以 /v1 结尾。密钥一行一个，会轮换使用。",
    "fields": [
        {"key": "ai_enabled", "label": "启用 AI 能力", "type": "bool", "default": "true"},
        {"key": "ai_base_url", "label": "API 端点（Base URL）", "type": "str",
         "default": "https://api.openai.com/v1", "required": True,
         "placeholder": "https://api.openai.com/v1"},
        {"key": "ai_api_keys", "label": "API 密钥（可多把）", "type": "secret",
         "hint": "一行一个或逗号分隔；轮换使用"},
        {"key": "ai_model", "label": "模型", "type": "str", "default": "gpt-4o-mini",
         "required": True, "placeholder": "gpt-4o-mini / deepseek-chat / qwen2.5:7b"},
        {"key": "ai_system_prompt", "label": "系统提示词", "type": "str",
         "default": "你是本站的客服助手，回答要简短、友好，只回答与本站使用相关的问题。",
         "hint": "服务端会追加一条身份约束：被问到模型/厂商时统一回答「我是本站的 AI 客服助手」，不透露具体模型名称",
         "placeholder": "客服人设 / 回答边界"},
        {"key": "ai_temperature", "label": "温度", "type": "str", "default": "0.7"},
        {"key": "ai_max_tokens", "label": "单次最大回复长度", "type": "int", "default": "512"},
        {"key": "ai_timeout", "label": "超时（秒）", "type": "int", "default": "60"},
        {"key": "ai_daily_limit", "label": "每用户每日提问上限", "type": "int", "default": "20",
         "hint": "0 = 不限制"},
    ],
    "test_label": "测试模型",
}

_FIELD_KEYS = [f["key"] for f in SPEC["fields"]]
_FIELD_DEFAULTS = {f["key"]: (f.get("default") or "") for f in SPEC["fields"]}

# 轮换用；读取配置不再走这个锁（配置是批量查的）
_lock = threading.Lock()
_counter = itertools.count()


def _settings(db: Session) -> dict:
    """一次查询取回本能力的全部配置（原先读端点/模型/密钥/上限要 4 次往返）"""
    return store.read_values(db, _FIELD_KEYS, _FIELD_DEFAULTS)


def _value(settings: dict, key: str, default: str = "") -> str:
    return str(settings.get(key, default) or default).strip()


def enabled(db: Session) -> bool:
    return _value(_settings(db), "ai_enabled", "true").lower() == "true"


def _keys_of(settings: dict) -> list[str]:
    """多密钥：换行 / 逗号 / 分号都认"""
    raw = _value(settings, "ai_api_keys")
    parts = raw.replace(";", "\n").replace(",", "\n").split("\n")
    return [p.strip() for p in parts if p.strip()]


def api_keys(db: Session) -> list[str]:
    return _keys_of(_settings(db))


def config(db: Session) -> dict:
    return _config_of(_settings(db))


def _config_of(settings: dict) -> dict:
    try:
        temperature = float(_value(settings, "ai_temperature", "0.7") or 0.7)
    except ValueError:
        temperature = 0.7
    try:
        max_tokens = int(_value(settings, "ai_max_tokens", "512") or 512)
    except ValueError:
        max_tokens = 512
    try:
        timeout = float(_value(settings, "ai_timeout", "60") or 60)
    except ValueError:
        timeout = 60.0
    return {
        "base_url": (_value(settings, "ai_base_url", "https://api.openai.com/v1") or "").rstrip("/"),
        "model": _value(settings, "ai_model", "gpt-4o-mini"),
        "system_prompt": _value(settings, "ai_system_prompt"),
        "temperature": temperature,
        "max_tokens": max(1, max_tokens),
        "timeout": max(1.0, timeout),
    }


IDENTITY_GUARD = (
    "身份约束：当用户询问你是什么模型、由哪家提供、底层用什么技术时，"
    "统一回答「我是本站的 AI 客服助手」，不要透露、不要猜测、也不要讨论具体模型名称。"
)


def _system_prompt_with_guard(prompt: str) -> str:
    """在管理员配置的人设后面追加身份约束（已含则不重复追加）

    放在代码里而不是只改默认值：
    - 存量部署的 ``ai_system_prompt`` 已经落库，改默认值对它们无效；
    - 管理员改提示词时不应该能把「不透露模型名」这条一起改掉。
    """
    text = (prompt or "").strip()
    if not text:
        return IDENTITY_GUARD
    if IDENTITY_GUARD in text:
        return text
    return f"{text}\n\n{IDENTITY_GUARD}"


def _next_key(keys: list[str]) -> str:
    if not keys:
        return ""
    with _lock:
        index = next(_counter)
    return keys[index % len(keys)]


def chat(db: Session, messages: list[dict], *, max_tokens: Optional[int] = None,
         use_system_prompt: bool = True) -> dict:
    """发一次 chat/completions；返回 ``{ok, answer, model, message}``"""
    settings = _settings(db)
    cfg = _config_of(settings)
    keys = _keys_of(settings)
    if not cfg["base_url"] or not cfg["model"]:
        return {"ok": False, "message": "未配置 API 端点或模型"}
    if not keys:
        return {"ok": False, "message": "未配置 API 密钥"}
    key = _next_key(keys)
    payload_messages = []
    if use_system_prompt:
        system_prompt = _system_prompt_with_guard(cfg["system_prompt"])
        if system_prompt:
            payload_messages.append({"role": "system", "content": system_prompt})
    payload_messages.extend(messages)
    import httpx

    try:
        with httpx.Client(timeout=cfg["timeout"]) as client:
            resp = client.post(
                f"{cfg['base_url']}/chat/completions",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={
                    "model": cfg["model"],
                    "messages": payload_messages,
                    "temperature": cfg["temperature"],
                    "max_tokens": int(max_tokens or cfg["max_tokens"]),
                },
            )
        if resp.status_code >= 400:
            detail = resp.text[:300]
            return {"ok": False, "message": f"上游 HTTP {resp.status_code}：{detail}",
                    "detail": {"model": cfg["model"]}}
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 — 上游不可达要如实报出
        return {"ok": False, "message": f"{type(exc).__name__}: {exc}", "detail": {"model": cfg["model"]}}
    choices = body.get("choices") or []
    answer = ""
    if choices:
        answer = ((choices[0].get("message") or {}).get("content") or "").strip()
    if not answer:
        return {"ok": False, "message": "上游没有返回内容", "detail": {"model": cfg["model"]}}
    usage = body.get("usage") or {}
    return {"ok": True, "answer": answer, "model": body.get("model") or cfg["model"],
            "message": "", "detail": {"usage": usage, "keys": len(keys)}}


def ask(db: Session, question: str, history: Optional[list[dict]] = None) -> dict:
    """用户侧问答入口（history 为 ``[{role, content}]``，最多取最近 8 轮）

    history 来自客户端，因此只接受 user / assistant 两种角色——否则用户可以直接塞一条
    ``role=system`` 把自己变成系统提示词，绕开管理员设定的人设与回答边界。
    """
    messages: list[dict] = []
    for turn in list(history or []):
        if not isinstance(turn, dict):
            continue
        role = str(turn.get("role") or "").strip().lower()
        content = turn.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str) or not content.strip():
            continue
        messages.append({"role": role, "content": content[:4000]})
    messages = messages[-8:]
    # P2 修复（审查）：question 无长度限制，单个超大 prompt 可烧掉 API 额度。
    # 与 history 同口径截断到 4000 字符。
    messages.append({"role": "user", "content": question[:4000]})
    return chat(db, messages)


# ==================== 每日配额（落库，多进程共用一份） ====================


def _today() -> str:
    """自然日口径：Asia/Shanghai（与面板问候语/每日一签同口径）。

    生产容器系统时区为 UTC：直接用 ``datetime.now()`` 会让用户北京时间
    00:00–08:00 的提问记到「昨天」，配额要到次日 08:00 才重置。
    """
    try:
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001 — 缺 tzdata 等极端情况回退
        return datetime.now().strftime("%Y-%m-%d")


def daily_limit(db: Session) -> int:
    """每用户每日提问上限（0 = 不限）"""
    try:
        return max(0, int(_value(_settings(db), "ai_daily_limit", "20") or 0))
    except ValueError:
        return 0


def used_today(db: Session, user_id: int) -> int:
    """今日已提问次数（跨天自动归零）"""
    row = (
        db.query(models.AiUsage.count)
        .filter(models.AiUsage.user_id == user_id, models.AiUsage.day == _today())
        .first()
    )
    return int(row[0]) if row and row[0] else 0


def quota(db: Session, user_id: int) -> dict:
    """配额快照：``{daily_limit, used, remaining}``（remaining 为 None 表示不限）"""
    limit = daily_limit(db)
    used = used_today(db, user_id)
    return {"daily_limit": limit, "used": used,
            "remaining": None if limit <= 0 else max(0, limit - used)}


def _insert_first(db: Session, user_id: int, day: str) -> bool:
    """插入当天的第一行；行已存在（并发抢先）时返回 False"""
    db.add(models.AiUsage(user_id=user_id, day=day, count=1))
    try:
        db.commit()
        return True
    except IntegrityError:
        db.rollback()
        return False


def consume(db: Session, user_id: int, limit: Optional[int] = None) -> tuple[bool, int]:
    """占用一次配额：返回 ``(是否允许, 今日已用)``

    并发下不能「先读再写」（两个请求都读到 limit-1 就都会放行），所以：

    1. 条件 UPDATE ``... AND count < limit``——只有真的没到上限才会加一，``rowcount`` 说明结果；
    2. 一行都没更新时，要么是当天还没有记录（插入 count=1），要么是已到上限——插入会因为
       唯一约束失败，那正好就是「已到上限」的判定依据。

    SQLite 与 PostgreSQL 都支持这套写法（不依赖 ``ON CONFLICT`` 方言）。
    """
    if limit is None:
        limit = daily_limit(db)
    day = _today()
    if limit > 0:
        condition = (models.AiUsage.user_id == user_id,
                     models.AiUsage.day == day,
                     models.AiUsage.count < limit)
        updated = (
            db.query(models.AiUsage)
            .filter(*condition)
            .update({models.AiUsage.count: models.AiUsage.count + 1,
                     models.AiUsage.updated_at: datetime.now()},
                    synchronize_session=False)
        )
        db.commit()
        if updated:
            return True, used_today(db, user_id)
        if not _insert_first(db, user_id, day):
            return False, used_today(db, user_id)
        return True, 1
    # 不限次数也记账（便于统计），不影响放行
    updated = (
        db.query(models.AiUsage)
        .filter(models.AiUsage.user_id == user_id, models.AiUsage.day == day)
        .update({models.AiUsage.count: models.AiUsage.count + 1,
                 models.AiUsage.updated_at: datetime.now()},
                synchronize_session=False)
    )
    db.commit()
    if not updated:
        _insert_first(db, user_id, day)
    return True, used_today(db, user_id)


def safe_consume(db: Session, user_id: int, limit: Optional[int] = None) -> tuple[bool, int]:
    """配额占用的「不带病拦人」包装

    计数写不进去（例如库被锁、只读副本）时**放行**并记一条警告：AI 助手的价值是答问题，
    不该因为一张统计表写失败就把用户挡在门外（真出这种故障时，日志里看得见）。
    """
    try:
        return consume(db, user_id, limit)
    except SQLAlchemyError as exc:
        logger.warning("AI 配额计数失败，本次放行: %s", exc)
        try:
            return True, used_today(db, user_id)
        except SQLAlchemyError:
            # 写失败的场景（连接池耗尽等）读大概率也失败：used 置 0，
            # 「不带病拦人」的放行承诺不变（r30 F2）。
            return True, 0


def is_configured(values: dict) -> bool:
    """端点、模型、密钥都齐了才算配过（不看开关）"""
    return (bool(values.get("ai_api_keys")) and bool(values.get("ai_model"))
            and bool(values.get("ai_base_url")))


def is_enabled(values: dict) -> bool:
    return str(values.get("ai_enabled") or "").lower() == "true" and is_configured(values)


def available(db: Session) -> bool:
    """是否可用：启用且已配齐（一次查询判定）"""
    settings = _settings(db)
    return (str(settings.get("ai_enabled") or "").lower() == "true"
            and is_configured(settings))


def test(db: Session, payload: dict) -> dict:
    cfg = config(db)
    if not api_keys(db):
        return {"ok": False, "message": "未配置 API 密钥（保存后再测）"}
    result = chat(db, [{"role": "user", "content": "回复两个字：可用"}], max_tokens=16)
    if not result.get("ok"):
        return {"ok": False, "message": result.get("message") or "调用失败",
                "detail": {"base_url": cfg["base_url"], "model": cfg["model"]}}
    return {"ok": True,
            "message": f"模型可用：{result['model']} → {result['answer'][:60]}",
            "detail": {"base_url": cfg["base_url"], "keys": result.get("detail", {}).get("keys")}}
