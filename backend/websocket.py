"""
WebSocket 实时通知服务
支持用户订阅不同频道的实时通知
"""
import json
import asyncio
from typing import Dict, Set, Optional, Any
from datetime import datetime
from fastapi import Request, WebSocket, WebSocketDisconnect
from redis import asyncio as aioredis
import logging

logger = logging.getLogger(__name__)


class ConnectionManager:
    """WebSocket 连接管理器"""

    def __init__(self):
        # 存储所有活跃连接 {user_id: {websocket}}
        self.active_connections: Dict[int, Set[WebSocket]] = {}
        # 存储用户订阅的频道 {user_id: set of channels}
        self.user_channels: Dict[int, Set[str]] = {}
        # 频道订阅者 {channel: set of user_ids}
        self.channel_subscribers: Dict[str, Set[int]] = {}
        # Redis 发布订阅
        self.redis_pubsub = None

    async def connect(self, websocket: WebSocket, user_id: int):
        """接受新连接"""
        await websocket.accept()
        if user_id not in self.active_connections:
            self.active_connections[user_id] = set()
        self.active_connections[user_id].add(websocket)
        logger.info(f"用户 {user_id} WebSocket 连接成功")

    def disconnect(self, websocket: WebSocket, user_id: int):
        """断开连接"""
        if user_id in self.active_connections:
            self.active_connections[user_id].discard(websocket)
            if not self.active_connections[user_id]:
                del self.active_connections[user_id]
                # 清理用户频道订阅
                if user_id in self.user_channels:
                    for channel in self.user_channels[user_id]:
                        if channel in self.channel_subscribers:
                            self.channel_subscribers[channel].discard(user_id)
                    del self.user_channels[user_id]
        logger.info(f"用户 {user_id} WebSocket 断开连接")

    async def subscribe(self, user_id: int, channels: list):
        """订阅频道"""
        if user_id not in self.user_channels:
            self.user_channels[user_id] = set()
        for channel in channels:
            self.user_channels[user_id].add(channel)
            if channel not in self.channel_subscribers:
                self.channel_subscribers[channel] = set()
            self.channel_subscribers[channel].add(user_id)
        logger.info(f"用户 {user_id} 订阅频道: {channels}")

    async def unsubscribe(self, user_id: int, channels: list):
        """取消订阅频道"""
        if user_id in self.user_channels:
            for channel in channels:
                self.user_channels[user_id].discard(channel)
                if channel in self.channel_subscribers:
                    self.channel_subscribers[channel].discard(user_id)
        logger.info(f"用户 {user_id} 取消订阅频道: {channels}")

    async def send_personal(self, message: dict, user_id: int):
        """发送个人消息"""
        if user_id in self.active_connections:
            disconnected = set()
            for connection in self.active_connections[user_id]:
                try:
                    await connection.send_json(message)
                except Exception as e:
                    logger.error(f"发送消息给用户 {user_id} 失败: {e}")
                    disconnected.add(connection)
            # 清理断开的连接
            for conn in disconnected:
                self.active_connections[user_id].discard(conn)

    async def broadcast_to_channel(self, channel: str, message: dict):
        """向频道广播消息"""
        if channel in self.channel_subscribers:
            for user_id in self.channel_subscribers[channel]:
                await self.send_personal(message, user_id)


    def get_online_count(self) -> int:
        """获取在线用户数"""
        return len(self.active_connections)

    def get_online_users(self) -> list:
        """获取在线用户ID列表"""
        return list(self.active_connections.keys())


# 全局连接管理器实例
manager = ConnectionManager()


# ==================== 通知消息类型 ====================

class NotificationType:
    """通知类型常量"""
    # 系统通知
    SYSTEM_ANNOUNCEMENT = "system.announcement"
    SYSTEM_MAINTENANCE = "system.maintenance"

    # 订阅相关
    SUBSCRIPTION_PURCHASED = "subscription.purchased"
    SUBSCRIPTION_EXPIRED = "subscription.expired"
    SUBSCRIPTION_RENEWED = "subscription.renewed"

    # 工单相关
    TICKET_CREATED = "ticket.created"
    TICKET_REPLIED = "ticket.replied"
    TICKET_RESOLVED = "ticket.resolved"

    # Emby 相关
    EMBY_ACCOUNT_CREATED = "emby.account_created"
    EMBY_ACCOUNT_EXPIRED = "emby.account_expired"
    EMBY_SESSION_STARTED = "emby.session_started"

    # 支付相关
    PAYMENT_SUCCESS = "payment.success"
    PAYMENT_FAILED = "payment.failed"

    # 邀请相关
    INVITATION_NEW_USER = "invitation.new_user"
    INVITATION_REWARD = "invitation.reward"


async def send_notification(
    notification_type: str,
    user_id: int,
    title: str,
    message: str,
    data: Optional[dict] = None
):
    """发送通知给指定用户"""
    notification = {
        "type": notification_type,
        "title": title,
        "message": message,
        "data": data or {},
        "timestamp": datetime.now().isoformat(),
    }
    await manager.send_personal(notification, user_id)


async def broadcast_notification(
    notification_type: str,
    channel: str,
    title: str,
    message: str,
    data: Optional[dict] = None
):
    """广播通知到频道"""
    notification = {
        "type": notification_type,
        "title": title,
        "message": message,
        "data": data or {},
        "timestamp": datetime.now().isoformat(),
    }
    await manager.broadcast_to_channel(channel, notification)


# ==================== FastAPI WebSocket 路由 ====================

from fastapi import APIRouter, Depends, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.database import get_db

websocket_router = APIRouter(tags=["WebSocket"])

# 1008 = policy violation（鉴权失败，标准 WebSocket 关闭码）
WS_UNAUTHORIZED_CODE = 1008


def authenticate_ws_token(raw: Optional[str]) -> Optional[int]:
    """校验 WebSocket 凭证，返回可信 user_id；无效/被禁用/无凭证返回 None

    只接受本项目签发的 JWT access token。早期版本允许「纯数字即 user_id」的
    兜底，任何匿名请求都能冒充任意用户接收推送，已彻底移除。

    r54 横切复查：补 token 新鲜度检查（jti 吊销 + token_version），与
    ``emby_server/auth.py`` 的 JWT 回退路径（A-P1-2）同口径——改密/登出后
    旧 token 建连即拒。``enforce_token_freshness`` 抛出的 HTTPException 在
    WS 上下文里转 None（无 HTTP 响应可抛，fail-closed）。
    """
    if not raw:
        return None
    from backend.database import SessionLocal
    from backend.security import decode_token, enforce_token_freshness, resolve_jwt_user_id

    user_id = resolve_jwt_user_id(raw)
    if user_id is None:
        return None
    db = SessionLocal()
    try:
        from backend import models
        from fastapi import HTTPException

        user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
        if not user or not user.is_active:
            return None
        payload = decode_token(raw, expected_type="access")
        if payload is None:
            return None
        try:
            enforce_token_freshness(db, payload, user)
        except HTTPException:
            return None
        return user.id
    except Exception as e:  # noqa: BLE001 — 鉴权失败一律视为未通过
        logger.error(f"WebSocket 鉴权查询失败: {e}")
        return None
    finally:
        db.close()


@websocket_router.websocket("/ws/{user_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    user_id: int,
    token: Optional[str] = Query(default=None),
):
    """WebSocket 连接端点

    鉴权：必须携带 `?token=<access_token>`（浏览器 WebSocket API 无法自定义请求头，
    因此以查询参数为主；非浏览器客户端也可用 `Authorization: Bearer`）。
    路径中的 user_id 只是客户端「声明自己是谁」，真实身份以 token 为准，
    两者不一致直接以 1008 关闭，防止匿名订阅他人的推送。
    """
    raw = token or websocket.headers.get("authorization", "").replace("Bearer", "").strip()
    auth_user_id = authenticate_ws_token(raw)
    if auth_user_id is None or auth_user_id != user_id:
        logger.warning(
            f"WebSocket 鉴权失败：声明 user_id={user_id}，来源 {websocket.client}"
        )
        await websocket.close(code=WS_UNAUTHORIZED_CODE)
        return

    user_id = auth_user_id
    await manager.connect(websocket, user_id)

    try:
        # 发送连接成功消息
        await websocket.send_json({
            "type": "connection.connected",
            "message": "WebSocket 连接成功",
            "user_id": user_id,
            "timestamp": datetime.now().isoformat(),
        })

        while True:
            # 接收客户端消息
            data = await websocket.receive_json()
            action = data.get("action")

            if action == "subscribe":
                # 订阅频道
                channels = data.get("channels", [])
                await manager.subscribe(user_id, channels)
                await websocket.send_json({
                    "type": "subscription.success",
                    "channels": channels,
                    "message": f"已订阅: {', '.join(channels)}",
                })

            elif action == "unsubscribe":
                # 取消订阅
                channels = data.get("channels", [])
                await manager.unsubscribe(user_id, channels)
                await websocket.send_json({
                    "type": "unsubscription.success",
                    "channels": channels,
                    "message": f"已取消订阅: {', '.join(channels)}",
                })

            elif action == "ping":
                # 心跳检测
                await websocket.send_json({
                    "type": "pong",
                    "timestamp": datetime.now().isoformat(),
                })

    except WebSocketDisconnect:
        manager.disconnect(websocket, user_id)
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}")
        manager.disconnect(websocket, user_id)


# ==================== REST API 用于发送通知 ====================

notification_router = APIRouter(prefix="/api/notifications", tags=["通知"])

_staff_scheme = HTTPBearer(auto_error=False)


def require_staff_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_staff_scheme),
    db: Session = Depends(get_db),
):
    """通知管理接口鉴权：必须是启用状态的管理员（is_staff）

    这几个接口原先是完全匿名的，任何人都能向全站广播「系统通知」做站内钓鱼；
    现改为仅管理员可用。用户端的实时通知走站内通知表 / 内部 manager 推送，
    不依赖这些 HTTP 接口。

    角色（v2.26.0）：发送 / 广播是写操作，与 ``/api/admin/*`` 用同一处判定
    （``backend/admin_roles.py``）——只读审计角色不能从这条侧门绕道发全站通知。

    r54 横切复查：此前这里是第二套手写实现（纯验签，不查 jti 吊销 /
    token_version），改密/登出后旧 token 在 120 分钟有效期内仍能调发送 /
    广播接口，违反 ``security.enforce_token_freshness``「所有 JWT 鉴权依赖
    都必须调这里」的横切纪律。现直接复用
    ``backend.api.admin_core.get_current_admin``（与 ``/api/admin/*`` 完全同一
    口径：验签 + 新鲜度双检查 + is_staff/is_active + admin_roles）。
    函数内导入防循环（同 ``portal._bearer_admin`` 模式）。
    """
    from backend.api.admin_core import get_current_admin

    return get_current_admin(request, credentials, db)


class SendNotificationRequest(BaseModel):
    """发送通知请求"""
    user_id: int
    title: str
    message: str
    notification_type: str
    data: Optional[dict] = None


class BroadcastRequest(BaseModel):
    """广播通知请求"""
    channel: str
    title: str
    message: str
    notification_type: str
    data: Optional[dict] = None


@notification_router.post("/send")
async def send_notification_endpoint(
    request: SendNotificationRequest,
    _: object = Depends(require_staff_user),
):
    """发送通知给指定用户（API）"""
    await send_notification(
        notification_type=request.notification_type,
        user_id=request.user_id,
        title=request.title,
        message=request.message,
        data=request.data,
    )
    return {"success": True, "message": "通知已发送"}


@notification_router.post("/broadcast")
async def broadcast_notification_endpoint(
    request: BroadcastRequest,
    _: object = Depends(require_staff_user),
):
    """广播通知到频道（API）"""
    await broadcast_notification(
        notification_type=request.notification_type,
        channel=request.channel,
        title=request.title,
        message=request.message,
        data=request.data,
    )
    return {"success": True, "message": "广播已发送"}


@notification_router.get("/online-users")
async def get_online_users(_: object = Depends(require_staff_user)):
    """获取在线用户列表（API）"""
    return {
        "count": manager.get_online_count(),
        "users": manager.get_online_users(),
    }


# 导出
__all__ = [
    "manager",
    "websocket_router",
    "notification_router",
    "authenticate_ws_token",
    "require_staff_user",
    "send_notification",
    "broadcast_notification",
    "NotificationType",
]
