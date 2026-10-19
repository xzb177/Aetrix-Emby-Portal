"""用户端 · AI 助手（能力：AI 模型设置）

只提供能力：模型、端点、密钥都由管理员在后台「系统设置 → AI 模型设置」里自己填，
项目不内置任何 key。没配好或没启用时，前端据 ``/status`` 直接不显示入口——不会出现
「点了之后报 500」这种半成品状态。

- ``GET  /api/user/ai/status``：是否可用 + 今日剩余次数（用户端据此显示/隐藏入口）
- ``POST /api/user/ai/ask``  ：提问（登录用户，按 ``ai_daily_limit`` 限流，默认 20 次/天）

两个端点都是**同步** ``def``：模型调用是阻塞的 httpx 请求（默认超时 60 秒），放进
``async def`` 会把整个事件循环按住——那正是 v2.12/v2.13 花大力气修掉的那类问题。
FastAPI 会把同步端点丢进线程池，播放与协议面不受影响。
"""
from __future__ import annotations

import logging
import threading
import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend import models
from backend.api.emby_portal import get_current_user_jwt
from backend.database import get_db
from backend.integrations import ai as ai_capability

logger = logging.getLogger(__name__)

assistant_router = APIRouter(prefix="/api/user/ai", tags=["用户端-AI 助手"])

MAX_QUESTION_CHARS = 1000
# F-P1-15：history 上限（条数 + 单条长度），防解析内存被刷爆
MAX_HISTORY_ITEMS = 20
MAX_HISTORY_ITEM_CHARS = 4000
# F-P1-16：模型调用并发上限。ai_capability.ask 是阻塞调用（默认 60 秒超时），
# sync 端点跑在 FastAPI 共享线程池；无上限时多用户并发提问可占满线程池，
# 把 /status 等同步端点饿死。用信号量限并发，超限直接 429（不排队，避免雪崩）。
AI_ASK_MAX_CONCURRENT = 4
_ai_ask_semaphore = threading.Semaphore(AI_ASK_MAX_CONCURRENT)


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=MAX_QUESTION_CHARS)
    history: Optional[List[dict]] = None


@assistant_router.get("/status")
def assistant_status(
    current_user: models.WebUser = Depends(get_current_user_jwt),
    db: Session = Depends(get_db),
):
    """助手是否可用 + 今日剩余次数（前端据此显示/隐藏入口与提示）"""
    if not ai_capability.available(db):
        return {"enabled": False,
                "reason": "管理员尚未配置 AI 模型（在「系统设置 → AI 模型设置」里填写端点、模型与密钥）",
                "daily_limit": 0, "used": 0, "remaining": None}
    return {"enabled": True, "reason": "", **ai_capability.quota(db, current_user.id)}


@assistant_router.post("/ask")
def assistant_ask(
    req: AskRequest,
    current_user: models.WebUser = Depends(get_current_user_jwt),
    db: Session = Depends(get_db),
):
    """向配置好的模型提一个问题（多把密钥轮换；上游失败原因原样透出）"""
    if not ai_capability.available(db):
        raise HTTPException(status_code=503, detail="AI 助手未启用或尚未配置（系统设置 → AI 模型设置）")

    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="问题不能为空")

    # F-P1-15：history 限条数 + 限单条长度（请求体在额度检查前就被完整解析，
    # 不限制则普通用户可借此消耗解析内存）
    history = req.history or []
    if len(history) > MAX_HISTORY_ITEMS:
        raise HTTPException(
            status_code=400,
            detail=f"历史记录最多 {MAX_HISTORY_ITEMS} 条",
        )
    for item in history:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="历史记录格式错误")
        blob = (item.get("content") or "")
        if not isinstance(blob, str):
            blob = str(blob)
        if len(blob) > MAX_HISTORY_ITEM_CHARS:
            raise HTTPException(
                status_code=400,
                detail=f"单条历史记录不能超过 {MAX_HISTORY_ITEM_CHARS} 字符",
            )

    # F-P1-16 修复：先占并发槽再扣配额，避免撞忙时白白消耗配额
    if not _ai_ask_semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="AI 助手正忙（并发已满），请稍后再试",
        )

    # 再占额度：并发时靠条件 UPDATE 判定，不会两个请求都放行
    # r48 遗留 ① / r51：request_id 是回滚审计键（ai_refund_log.request_id
    # 唯一约束做"已退"标记）。每请求只走一次失败分支，天然只退一次；
    # 客户端重试是新请求（新 request_id），走"新扣+新退"口径，净效果正确。
    request_id = uuid.uuid4().hex
    limit = ai_capability.daily_limit(db)
    allowed, used, deducted = ai_capability.safe_consume(db, current_user.id, limit)
    if not allowed:
        _ai_ask_semaphore.release()
        raise HTTPException(
            status_code=429,
            detail=f"今日提问次数已用完（上限 {limit} 次），明天再来吧",
        )

    try:
        result = ai_capability.ask(db, question, history)
    finally:
        _ai_ask_semaphore.release()
    if not result.get("ok"):
        # r48 遗留 ①：上游明确失败时退回本次预扣的配额。只在本次真实扣减过
        # （deducted）时才退——fail-open 穿透的请求没扣过，退了就是误减。
        # 单次退由"每请求只走一次本分支"的结构保证；request_id 写入
        # ai_refund_log 做审计与防重标记（见 ai_capability.refund_quota）。
        if deducted:
            ok_refund = ai_capability.refund_quota(db, current_user.id, request_id)
            logger.debug("AI 配额回滚 user=%s request=%s: %s",
                         current_user.id, request_id, "已退" if ok_refund else "跳过")
        # 上游的错误信息（含模型名与 HTTP 状态）对排错很有用，原样转给用户端
        raise HTTPException(status_code=502, detail=result.get("message") or "模型调用失败")
    remaining = None if limit <= 0 else max(0, limit - used)
    # 不回传 model：用户不需要看到模型名称（气泡里不显示，响应里也不该有），
    # 管理端的「测试模型」仍会回显模型供排错。
    return {
        "answer": result.get("answer", ""),
        "daily_limit": limit,
        "used": used,
        "remaining": remaining,
    }


__all__ = ["assistant_router"]
