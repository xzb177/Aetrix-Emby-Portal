"""SendCommand 投递层（阶段 8）：把 GeneralCommand 推送到播放器会话的 websocket 连接。

POST /Sessions/{id}/Command 只做到服务端接收 + 记账 + 同步执行；
播放器真正收到命令，靠本模块的 websocket 推送——这是 Emby 原生遥控
的投递段（控制器发命令给服务端，服务端经播放器持有的一条长连接推给播放器）。

- 路径：/embywebsocket（Emby/Jellyfin 惯用路径，无 /emby 前缀变体）。
- 鉴权：复用 auth.resolve_token 的 EA token 口径（哈希比对 + 吊销 +
  可用期），不另起一套鉴权；门户 JWT 不认（门户另有 /ws）。
- 寻址：按设备绑定。建连时用 sessionKey（有则以会话行的 device_id
  为准）或 deviceId / token device_id；投递时按会话行 device_id 找连接。
- 单进程前提：serve.py workers=1，注册表放进程内；workers>1 时投递
  须改走 Redis 扇出，本模块不做。
- 诚实边界：pushed 只表示“帧已发往连接”，不是播放器回执；无连接
  时命令仍记账执行（recorded），绝不谎称已作用于播放器。能力申报
  仍 False，真实播放器消费未实测前不翻（r50 诚实化纪律）。
- 不做轮询：用户 02:20 / 22:56 两度定案，轮询落选；本模块不提供
  任何拉命令口。
"""

from __future__ import annotations

import json
import logging
import threading
from types import SimpleNamespace

from fastapi import WebSocket
from starlette.datastructures import Headers, QueryParams

logger = logging.getLogger(__name__)

# (user_id, device_id) -> 该设备的长连接集合。连接只在事件循环线程里
# 增删；投递入口（async 端点）同样跑在事件循环线程，threading.Lock 只
# 防未来同步调用方误入，不包 await。
_CONNECTIONS: dict[tuple[int, str], set[WebSocket]] = {}
_LOCK = threading.Lock()


def _key(user_id: int, device_id: str | None) -> tuple[int, str]:
    return (int(user_id), (device_id or "").strip()[:100] or "unknown-device")


def register_connection(user_id: int, device_id: str, websocket: WebSocket) -> None:
    """建连后登记（同一设备多连接并存，投递时全推）"""
    with _LOCK:
        _CONNECTIONS.setdefault(_key(user_id, device_id), set()).add(websocket)


def unregister_connection(user_id: int, device_id: str, websocket: WebSocket) -> None:
    """断连后摘除（集合空了连键一起删，不留空壳）"""
    key = _key(user_id, device_id)
    with _LOCK:
        conns = _CONNECTIONS.get(key)
        if conns is None:
            return
        conns.discard(websocket)
        if not conns:
            _CONNECTIONS.pop(key, None)


def connection_count(user_id: int, device_id: str) -> int:
    """该设备当前长连接数（测试与排障用）"""
    with _LOCK:
        return len(_CONNECTIONS.get(_key(user_id, device_id), ()))


def _envelope(name: str, arguments: dict | None) -> str:
    """Emby/Jellyfin websocket 帧：GeneralCommand + 原样 Arguments。

    推给播放器的是控制器发来的 Emby 原名（Pause / Seek …），不是内部
    命令名——播放器只认 Emby 名。
    """
    return json.dumps(
        {"MessageType": "GeneralCommand",
         "Data": {"Name": name, "Arguments": dict(arguments) if arguments else {}}},
        ensure_ascii=False,
    )


async def push_general_command(user_id: int, device_id: str | None,
                               name: str, arguments: dict | None) -> int:
    """向该设备的所有长连接推一条 GeneralCommand，返回成功发送数。

    永不抛异常：死连接当场摘除；投递失败不影响命令记账与执行（调用
    方只把返回值写进 delivery 字段，不因投递失败而 500）。
    """
    try:
        payload = _envelope(name, arguments)
    except (TypeError, ValueError):
        logger.warning("SendCommand 投递帧序列化失败（参数不可 JSON 化），跳过投递",
                       exc_info=True)
        return 0
    with _LOCK:
        targets = list(_CONNECTIONS.get(_key(user_id, device_id), ()))
    sent = 0
    for ws in targets:
        try:
            await ws.send_text(payload)
            sent += 1
        except Exception:  # noqa: BLE001 — 死连接直接摘掉，不拖投递
            unregister_connection(user_id, device_id, ws)
    return sent


def _db_session():
    """建一个短命 DB 会话（仅握手用：鉴权 + 绑定查完即关，不占连接池）。
    测试可 monkeypatch 本函数注入夹具会话。"""
    from backend.database import SessionLocal

    return SessionLocal()


async def emby_websocket(websocket: WebSocket) -> None:
    """EA 播放器长连接：鉴权 + 按设备绑定，之后只负责收（忽略）与被推。

    握手失败一律 fail-closed（1008 关连接，不接受匿名长连）：
    token 无效、sessionKey 查不到或不属于该用户，都不建连。
    建连后客户端发来的帧一律忽略（保活等），本口只推不拉。
    """
    db = _db_session()
    try:
        # Starlette 的 Request 只接受 http scope，websocket scope 不能直接
        # 套——改拼 resolve_token 实际用到的最小请求面（头 / 查询串 / 直连
        # 客户端），不另起鉴权逻辑。
        scope = websocket.scope
        client = scope.get("client") or (None, None)
        request = SimpleNamespace(
            headers=Headers(raw=scope.get("headers", [])),
            query_params=QueryParams(
                scope.get("query_string", b"").decode("latin-1")),
            client=SimpleNamespace(host=client[0]),
        )
        from backend.emby_server import auth as _auth
        from backend.emby_server import play_session as _ps

        resolved = _auth.resolve_token(db, request)
        if resolved is None:
            await websocket.close(code=1008, reason="invalid token")
            return
        user, token_row = resolved
        params = request.query_params
        session_key = (params.get("sessionKey") or params.get("PlaySessionId")
                       or "").strip()
        device_id = ""
        if session_key:
            session = _ps.resolve_session(db, session_key)
            if session is None or session.user_id != user.id:
                await websocket.close(code=1008, reason="unknown session")
                return
            device_id = session.device_id or ""
        if not device_id:
            device_id = (params.get("deviceId") or params.get("DeviceId")
                         or "").strip()
        if not device_id:
            device_id = token_row.device_id or ""
        device_id = device_id[:100] or "unknown-device"
    except Exception:  # noqa: BLE001 — 握手任何异常都 fail-closed
        logger.warning("embywebsocket 握手失败", exc_info=True)
        try:
            await websocket.close(code=1008, reason="handshake failed")
        except Exception:  # noqa: BLE001 — 关连接本身失败也认了
            pass
        return
    finally:
        db.close()
    await websocket.accept()
    register_connection(user.id, device_id, websocket)
    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            # 其余帧（文本/二进制/保活）一律忽略，只推不拉
    except Exception:  # noqa: BLE001 — 断连/异常都按正常摘除处理
        pass
    finally:
        unregister_connection(user.id, device_id, websocket)


__all__ = [
    "register_connection",
    "unregister_connection",
    "connection_count",
    "push_general_command",
    "emby_websocket",
]
