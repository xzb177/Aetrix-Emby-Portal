"""管理后台 · 首次运行向导（setup_mode，借鉴 twilight-kotomi）

全新部署没有管理员时，管理后台不再靠「默认 admin/admin 裸奔」：
第一次打开 /admin/ 会被前端送到初始化向导页（/setup），在这里建第一个管理员；
成功后入口**永久关闭**（``setup_completed=True``），此后 POST 直接 403。

两个端点都是免鉴权的——还没有身份可鉴（见
``scripts/check_auth_coverage.py`` 的 PRE_AUTH 登记与
``scripts/check_admin_audit_coverage.py`` 的 EXCEPTIONS 登记）：

- ``GET  /api/admin/setup/status`` → ``{"setup_completed": bool}``
- ``POST /api/admin/setup``         → ``{username, password}`` 建第一个管理员并置旗

安全要点（都是真实会踩到的坑）：

1. **完成后永久 403**：置旗成功后 POST 只认 ``setup_completed``，不再看别的；
2. **防「旗丢了、库里有管理员」**：如果库里已经有 ``is_staff`` 账号但旗没置
   （比如从老版本升级上来、或被人手动删了配置行），视为已完成：自愈补旗并 403，
   不给攻击者留一个「再造一个超级管理员」的洞；
3. **限流**：与管理员登录同口径（8 次/分钟/IP），防字典跑向导入口；
4. **不写第二套建账号逻辑**：复用 ``backend/admin_accounts.py`` 的
   ``validate_username / check_password_strength / upsert_admin``；
5. **部署者凭据（P0-4）**：部署时设置 ``SETUP_BOOTSTRAP_TOKEN`` 环境变量，
   向导 POST 必须携带一致的 ``bootstrap_token``，防未初始化时被任意访问者抢占管理员；
6. **跨进程互斥（P0-5）**：``threading.Lock`` 只在进程内有效，多 worker 下
   两个进程可同时通过检查并创建不同用户名的管理员。改用 PG advisory lock
   （SQLite 沿用进程内锁）把「检查→创建→置旗」做成临界区。
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from backend import models
from backend.admin_accounts import (
    AdminAccountError,
    check_password_strength,
    upsert_admin,
    validate_username,
)
from backend.authlog import client_ip as log_ip, record_event, user_agent
from backend.database import get_db
from backend.ratelimit import check_rate_limit, client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/setup", tags=["管理后台·首次运行向导"])

SETUP_COMPLETED_KEY = "setup_completed"
SETUP_COMPLETED_DESC = "首次运行向导是否已完成（True 后初始化入口永久关闭）"

# P0-4 修复：一次性 bootstrap token。
# 部署时通过环境变量 SETUP_BOOTSTRAP_TOKEN 注入（受保护渠道交付，如密钥管理/部署脚本），
# 向导 POST 必须携带一致的 token 才能建第一个管理员。成功后入口永久关闭，token 自然失效。
# 未设置时保持旧行为（兼容已部署实例），但打 warning 提醒部署者配置。
def _bootstrap_token() -> str:
    return (os.getenv("SETUP_BOOTSTRAP_TOKEN") or "").strip()


# P0-5 修复：跨进程的 setup 互斥锁。
# threading.Lock 只在进程内有效；多 worker 下两个进程可同时通过 is_setup_completed
# 检查，各自 upsert 不同用户名的管理员。改用 PG advisory lock（跨进程）；
# SQLite（测试/单机）沿用进程内锁。
_setup_thread_lock = threading.Lock()
# pg_advisory_lock 的 64 位 key：由固定字符串派生，避免与其他锁冲突
_SETUP_PG_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"aetrix-setup-first-admin").digest()[:8], "big", signed=True
)


def _is_postgres(db: Session) -> bool:
    try:
        return db.bind.dialect.name == "postgresql"
    except Exception:
        return False


class _SetupLock:
    """跨进程 setup 互斥锁（上下文管理器）。"""

    def __init__(self, db: Session):
        self.db = db
        self._pg_locked = False

    def __enter__(self):
        if _is_postgres(self.db):
            # 会话级 advisory lock，跨进程互斥；finally 里显式解锁
            self.db.execute(
                text("SELECT pg_advisory_lock(:k)"), {"k": _SETUP_PG_LOCK_KEY}
            )
            self._pg_locked = True
        else:
            _setup_thread_lock.acquire()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._pg_locked:
            try:
                self.db.execute(
                    text("SELECT pg_advisory_unlock(:k)"), {"k": _SETUP_PG_LOCK_KEY}
                )
            except Exception:
                # 解锁失败不影响已完成的操作；连接归还池前会清理
                logger.warning("setup advisory lock 解锁失败", exc_info=True)
            self._pg_locked = False
        else:
            _setup_thread_lock.release()
        return False


def is_setup_completed(db: Session) -> bool:
    """向导是否已完成。

    两个真相源：``setup_completed`` 配置旗，以及库里是否已有管理员。
    库里已有管理员但旗没置 = 老版本升级/配置行丢失，视为已完成并自愈补旗，
    防止向导入口在已有管理员的部署上被重新打开。
    """
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == SETUP_COMPLETED_KEY
    ).first()
    if row and (row.value or "").strip().lower() == "true":
        return True
    has_admin = (
        db.query(models.WebUser.id)
        .filter(models.WebUser.is_staff.is_(True))
        .first()
        is not None
    )
    if has_admin:
        # 自愈：管理员已存在，补旗，避免入口悬空
        if row:
            row.value = "true"
        else:
            db.add(models.SystemConfig(
                key=SETUP_COMPLETED_KEY, value="true", description=SETUP_COMPLETED_DESC,
            ))
        db.commit()
        logger.warning("setup_completed 旗缺失但库里已有管理员：已自愈补旗，向导入口保持关闭")
        return True
    return False


def _mark_setup_completed(db: Session) -> None:
    row = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == SETUP_COMPLETED_KEY
    ).first()
    if row:
        row.value = "true"
    else:
        db.add(models.SystemConfig(
            key=SETUP_COMPLETED_KEY, value="true", description=SETUP_COMPLETED_DESC,
        ))
    db.commit()


@router.get("/status")
def setup_status(db: Session = Depends(get_db)) -> dict:
    """向导状态：前端进 /admin/ 前先问一次，决定去向导页还是登录页。"""
    return {"setup_completed": is_setup_completed(db)}


class SetupRequest(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)
    # P0-4：一次性 bootstrap token。部署时通过 SETUP_BOOTSTRAP_TOKEN 环境变量注入；
    # 设置了该变量时，请求必须携带一致的 token，否则 403。
    bootstrap_token: str | None = Field(default=None, max_length=256)


@router.post("")
def setup_first_admin(
    http_request: Request,
    request: SetupRequest,
    db: Session = Depends(get_db),
) -> dict:
    """建第一个管理员并永久关闭向导入口。完成后再次调用一律 403。"""
    ip = client_ip(http_request)

    # P0-4 修复：部署者凭据。部署时设置了 SETUP_BOOTSTRAP_TOKEN 的，
    # 请求必须携带一致的 bootstrap_token，否则任何能访问该接口的人
    # 都可在管理员完成初始化前抢先创建管理员并永久关闭向导。
    expected_token = _bootstrap_token()
    if expected_token:
        provided = (request.bootstrap_token or "").strip()
        # secrets.compare_digest 防时序攻击
        import secrets
        if not provided or not secrets.compare_digest(provided, expected_token):
            record_event(
                db, username=request.username.strip(), ip=log_ip(http_request),
                agent=user_agent(http_request), success=False, reason="admin_setup_failed",
                detail="bootstrap token 缺失或错误",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="初始化需要部署者凭据（bootstrap token）",
            )
    else:
        logger.warning(
            "SETUP_BOOTSTRAP_TOKEN 未设置：向导入口无部署者凭据保护，"
            "建议在部署时配置该环境变量"
        )

    allowed, retry_after = check_rate_limit(f"admin_setup:{ip}", 8, 60)
    if not allowed:
        record_event(
            db, username=request.username.strip(), ip=log_ip(http_request),
            agent=user_agent(http_request), success=False, reason="admin_setup_failed",
            detail=f"尝试过于频繁，已限流（{retry_after}s）",
        )
        raise HTTPException(status_code=429, detail="尝试过于频繁，请稍后再试")

    # P0-5 修复：跨进程互斥。threading.Lock 只在进程内有效，多 worker 下
    # 两个进程可同时通过 is_setup_completed 检查并各自创建不同用户名的管理员。
    # 现在用 PG advisory lock（SQLite 沿用进程内锁）把"检查→创建→置旗"做成临界区。
    with _SetupLock(db):
        if is_setup_completed(db):
            record_event(
                db, username=request.username.strip(), ip=log_ip(http_request),
                agent=user_agent(http_request), success=False, reason="admin_setup_failed",
                detail="初始化已完成，入口已关闭",
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="初始化已完成：向导入口已永久关闭",
            )

        try:
            username = validate_username(request.username)
            check_password_strength(request.password)
        except AdminAccountError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        try:
            user, created, _applied = upsert_admin(db, username, password=request.password)
        except AdminAccountError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        _mark_setup_completed(db)
        user_id = user.id

    record_event(
        db, username=username, user_id=user_id, ip=log_ip(http_request),
        agent=user_agent(http_request), success=True, reason="admin_setup",
        detail="首次运行向导创建第一个管理员" if created else "首次运行向导（账号已存在，已升级为管理员）",
    )
    logger.info("首次运行向导完成：管理员 %r 已就绪，向导入口永久关闭", username)
    return {"ok": True, "username": username, "setup_completed": True}
