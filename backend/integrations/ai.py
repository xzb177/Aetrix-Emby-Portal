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
from datetime import datetime, timedelta
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
# 能力框架的卡片状态判定仍需读到该键（integrations.raw_values_many 据此补读）
EXTRA_READ_KEYS = {"ai_enabled": "true"}
# A 类开关摘面板（2026-10-21）：ai_enabled 不再出现在能力表单里，但仍是
# 运行时开关——改走 env AI_ENABLED（缺省开），读取集合保留该键。
_FIELD_KEYS = _FIELD_KEYS + ["ai_enabled"]
_FIELD_DEFAULTS = {**_FIELD_DEFAULTS, "ai_enabled": "true"}

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


def _record_deduction(db: Session, user_id: int, day: str,
                      request_id: Optional[str]) -> None:
    """扣费归属日落账（跨午夜退款修复）：退款必须退回「原扣费那一天」，
    所以扣费时把归属日随 request_id 落到 ai_refund_log（refunded=0）。
    best-effort：写失败不许影响已经成功的扣费（退款缺归属行时会回退到
    按退款当天退的老口径，不会多退）。"""
    if not request_id:
        return
    try:
        db.add(models.AiRefundLog(user_id=user_id, day=day,
                                  request_id=request_id, refunded=False))
        db.commit()
    except IntegrityError:
        # request_id 被复用（正常流程是 uuid4，几乎不可能）：沿用既有归属行
        db.rollback()
        logger.warning("AI 配额扣费归属行已存在 request_id=%s，沿用既有归属日",
                       request_id)
    except SQLAlchemyError as exc:
        db.rollback()
        logger.warning("AI 配额扣费归属行写入失败（退款将回退到按当天退）: %s", exc)


def consume(db: Session, user_id: int, limit: Optional[int] = None,
            request_id: Optional[str] = None) -> tuple[bool, int, bool]:
    """占用一次配额：返回 ``(是否允许, 今日已用, 本次是否真实扣减)``

    并发下不能「先读再写」（两个请求都读到 limit-1 就都会放行），所以：

    1. 条件 UPDATE ``... AND count < limit``——只有真的没到上限才会加一，``rowcount`` 说明结果；
    2. 一行都没更新时，要么是当天还没有记录（插入 count=1），要么是已到上限——插入会因为
       唯一约束失败，那正好就是「已到上限」的判定依据。

    SQLite 与 PostgreSQL 都支持这套写法（不依赖 ``ON CONFLICT`` 方言）。

    r48 遗留 ①：第三个返回值 ``deducted`` 标记本次是否真的发生了扣减——fail-open
    穿透（见 ``safe_consume``）时为 False，调用方做上游失败回滚时必须先看它，
    否则会对没扣过的请求误减计数。
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
            _record_deduction(db, user_id, day, request_id)
            return True, used_today(db, user_id), True
        if not _insert_first(db, user_id, day):
            return False, used_today(db, user_id), False
        _record_deduction(db, user_id, day, request_id)
        return True, 1, True
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
    _record_deduction(db, user_id, day, request_id)
    return True, used_today(db, user_id), True


def safe_consume(db: Session, user_id: int, limit: Optional[int] = None,
                 request_id: Optional[str] = None) -> tuple[bool, int, bool]:
    """配额占用的「不带病拦人」包装

    计数写不进去（例如库被锁、只读副本）时**放行**并记一条警告：AI 助手的价值是答问题，
    不该因为一张统计表写失败就把用户挡在门外（真出这种故障时，日志里看得见）。

    r48 遗留 ①：fail-open 穿透时 ``deducted=False``——调用方在上游失败时**不许**
    对这类请求做回滚（没扣过，退了就是误减别人的计数）。
    """
    try:
        return consume(db, user_id, limit, request_id)
    except SQLAlchemyError as exc:
        logger.warning("AI 配额计数失败，本次放行: %s", exc)
        try:
            return True, used_today(db, user_id), False
        except SQLAlchemyError:
            # 写失败的场景（连接池耗尽等）读大概率也失败：used 置 0，
            # 「不带病拦人」的放行承诺不变（r30 F2）。
            return True, 0, False


def refund_quota(db: Session, user_id: int, request_id: str) -> bool:
    """上游调用失败时退回本次预扣的配额（r48 遗留 ①）

    跨午夜漂移修复：退款退回**扣费归属日**的账（consume 时随 request_id
    落在 ai_refund_log.day），不按退款时刻的当天退——23:59 扣的配额，
    次日退款也退回当天。

    幂等（不回退 #1094 语义）：
    - 同一 request_id 用「refunded=0 → 1」的原子认领（条件 UPDATE 看
      rowcount）：抢到认领的才退一次，并发双退被行锁挡住，第二个认领到
      0 行返回 False；
    - 老数据行（升级前在退款时写入，迁移后 refunded=TRUE=已退标记）
      重放是空操作。
    - 归属行缺失（升级过渡期在途请求 / 调用方没传 request_id）：回退到
      老口径（按退款当天记账本＋退当天），行为与升级前一致。

    返回 True=本次确实执行了一次回滚；False=已退过/无需退/回滚写失败。
    回滚写失败时只记警告不抛异常：配额是统计口径，不能因为退账把 502 变成 500。
    """
    if not request_id:
        logger.warning("AI 配额回滚缺 request_id，跳过（无法保证幂等）")
        return False
    try:
        # ① 原子认领：只有归属行存在且未退过才能抢到；user_id 必须一致，
        # 防止错位认领（r55：归属行是 A 的、传入 user_id 是 B 时，不能把 A 的
        # 行认领走再去减 B 的账——防御纵深，当前唯一调用方 assistant.py 传参一致）。
        claimed = (
            db.query(models.AiRefundLog)
            .filter(models.AiRefundLog.request_id == request_id,
                    models.AiRefundLog.user_id == user_id,
                    models.AiRefundLog.refunded == False)  # noqa: E712 — SQLAlchemy 语义
            .update({models.AiRefundLog.refunded: True},
                    synchronize_session=False)
        )
        if claimed:
            row = (db.query(models.AiRefundLog.day)
                     .filter(models.AiRefundLog.request_id == request_id)
                     .first())
            day = row[0] if row and row[0] else _today()
            db.query(models.AiUsage).filter(
                models.AiUsage.user_id == user_id,
                models.AiUsage.day == day,
                models.AiUsage.count > 0,
            ).update(
                {models.AiUsage.count: models.AiUsage.count - 1,
                 models.AiUsage.updated_at: datetime.now()},
                synchronize_session=False,
            )
            db.commit()
        else:
            db.rollback()
            known = (db.query(models.AiRefundLog.id)
                       .filter(models.AiRefundLog.request_id == request_id)
                       .first())
            if known is not None:
                # 已退过（或老数据行）：空操作
                return False
            # ② 老口径兜底：归属行缺失，按退款当天记账本＋退当天
            day = _today()
            db.add(models.AiRefundLog(user_id=user_id, day=day,
                                      request_id=request_id, refunded=True))
            db.flush()  # 唯一约束冲突在这里抛 IntegrityError
            db.query(models.AiUsage).filter(
                models.AiUsage.user_id == user_id,
                models.AiUsage.day == day,
                models.AiUsage.count > 0,
            ).update(
                {models.AiUsage.count: models.AiUsage.count - 1,
                 models.AiUsage.updated_at: datetime.now()},
                synchronize_session=False,
            )
            db.commit()
    except IntegrityError:
        # 同一 request_id 并发走到老口径兜底：第二个是空操作
        db.rollback()
        return False
    except SQLAlchemyError as exc:
        logger.warning("AI 配额回滚失败，本次不退: %s", exc)
        db.rollback()
        return False
    # 顺手清 90 天前的幂等账本（与 prune_ai_usage 同口径，best-effort）
    try:
        cutoff = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        (db.query(models.AiRefundLog)
           .filter(models.AiRefundLog.day < cutoff)
           .delete(synchronize_session=False))
        db.commit()
    except SQLAlchemyError as exc:
        logger.warning("AI 回滚账本清理失败（不影响本次回滚）: %s", exc)
        db.rollback()
    return True


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
