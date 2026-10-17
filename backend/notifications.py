"""
多渠道通知服务
支持站内信、邮件、Telegram 等多种通知渠道
实现管理员操作与用户前台的联动
"""
import asyncio
import ipaddress
import logging
from typing import Optional, List, Dict, Any
from datetime import datetime
from urllib.parse import urlparse
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_

from backend.database import SessionLocal, get_db
from backend import models
from backend.websocket import manager, send_notification

logger = logging.getLogger(__name__)


# ==================== 通知渠道抽象 ====================

# Telegram 富媒体常量：media group 最多 10 张；caption 上限 1024 字符
MAX_MEDIA_GROUP = 10
MAX_CAPTION_LEN = 1024


def _truncate_caption(text: str, limit: int = MAX_CAPTION_LEN) -> str:
    """安全截断 HTML caption：不把标签拦腰截断，未闭合的 b/i 补上闭合。

    防止 Telegram 因 "can't parse entities" 拒收。预留闭合标签的位置，
    保证返回值一定不超过 limit。
    """
    import re

    if len(text) <= limit:
        return text
    # 预留最多 8 字符给 </b></i> 闭合标签
    cut = re.sub(r"<[^>]*$", "", text[: max(0, limit - 8)])
    for tag in ("b", "i"):
        if cut.count(f"<{tag}>") > cut.count(f"</{tag}>"):
            cut += f"</{tag}>"
    return cut

class NotificationChannel:
    """通知渠道基类"""

    async def send(self, user_id: int, title: str, content: str, data: Optional[dict] = None) -> bool:
        """发送通知"""
        raise NotImplementedError


class InAppChannel(NotificationChannel):
    """站内信通知渠道"""

    def __init__(self):
        self.name = "in_app"

    async def send(self, user_id: int, title: str, content: str,
                   message_type: str = "system",
                   related_id: Optional[int] = None,
                   from_user_id: Optional[int] = None,
                   db: Session = None) -> bool:
        """发送站内消息"""
        # G2-P1-1：自己创建的会话必须显式关闭，不能靠生成器 GC
        _own_db = db is None
        try:
            # 创建数据库记录
            if db is None:
                db = next(get_db())

            message = models.StationMessage(
                from_user_id=from_user_id,
                to_user_id=user_id,
                title=title,
                content=content,
                message_type=message_type,
                related_id=related_id,
                is_read=False
            )
            db.add(message)
            db.commit()
            db.refresh(message)

            # 同时通过 WebSocket 推送
            await send_notification(
                notification_type=f"station.{message_type}",
                user_id=user_id,
                title=title,
                message=content,
                data={
                    "message_id": message.id,
                    "message_type": message_type,
                    "related_id": related_id
                }
            )

            logger.info(f"站内消息已发送给用户 {user_id}: {title}")
            return True

        except Exception as e:
            logger.error(f"发送站内消息失败: {e}")
            if db:
                db.rollback()
            return False
        finally:
            # G2-P1-1：自己创建的会话必须关闭
            if _own_db and db is not None:
                try:
                    db.close()
                except Exception:
                    pass


class EmailChannel(NotificationChannel):
    """邮件通知渠道"""

    def __init__(self, smtp_host: str = None, smtp_port: int = 587,
                 smtp_user: str = None, smtp_password: str = None,
                 from_name: str = "", from_email: str = ""):
        self.name = "email"
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        # 发件人显示名 / 发件地址（留空则用 SMTP 账号），后台「邮件与模板」里配
        self.from_name = (from_name or "").strip()
        self.from_email = (from_email or "").strip()
        self.enabled = bool(smtp_host and smtp_user)

    async def send(self, user_id: int, title: str, content: str,
                   data: Optional[dict] = None, db: Session = None) -> bool:
        """发送邮件通知"""
        if not self.enabled:
            logger.warning("邮件通知未配置，跳过发送")
            return False

        # G2-P1-1：自己创建的会话必须显式关闭
        _own_db = db is None
        try:
            # 获取用户邮箱
            if db is None:
                db = next(get_db())

            user = db.query(models.WebUser).filter(models.WebUser.id == user_id).first()
            if not user or not user.email:
                logger.warning(f"用户 {user_id} 没有邮箱地址")
                return False

            # 真正投递：SMTP 发送失败必须如实上报，不能记录成 sent
            # （此前这里只写了一条 status="sent" 的历史记录，邮件从未发出）
            ok, error = await asyncio.to_thread(self._deliver, user.email, title, content)

            history = models.NotificationHistory(
                notification_type="email",
                target=user.email,
                title=title,
                content=content,
                status="sent" if ok else "failed",
                error_message=error,
                sent_at=datetime.now() if ok else None,
            )
            db.add(history)
            db.commit()

            if ok:
                logger.info(f"邮件已发送给用户 {user_id}: {title}")
            else:
                logger.error(f"邮件发送失败（用户 {user_id}）: {error}")
            return ok

        except Exception as e:
            logger.error(f"发送邮件失败: {e}")
            if db:
                db.rollback()
            return False
        finally:
            # G2-P1-1：自己创建的会话必须关闭
            if _own_db and db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    def _deliver(self, to_email: str, title: str, content: str) -> tuple[bool, Optional[str]]:
        """同步 SMTP 投递（放在线程里跑，避免阻塞事件循环）"""
        import smtplib
        from email.mime.text import MIMEText

        try:
            msg = MIMEText(content, "plain", "utf-8")
            msg["Subject"] = title
            sender = self.from_email or self.smtp_user or ""
            if self.from_name:
                # 非 ASCII 显示名要走 RFC 2047 编码，否则部分客户端显示成乱码
                from email.header import Header

                msg["From"] = f"{Header(self.from_name, 'utf-8').encode()} <{sender}>"
            else:
                msg["From"] = sender
            msg["To"] = to_email

            port = int(self.smtp_port or 587)
            if port == 465:
                server = smtplib.SMTP_SSL(self.smtp_host, port, timeout=15)
            else:
                server = smtplib.SMTP(self.smtp_host, port, timeout=15)
            try:
                if port != 465:
                    server.starttls()
                if self.smtp_user:
                    server.login(self.smtp_user, self.smtp_password or "")
                server.sendmail(self.smtp_user, [to_email], msg.as_string())
            finally:
                server.quit()
            return True, None
        except Exception as exc:  # noqa: BLE001 — 投递失败原因要原样记下来
            return False, f"{type(exc).__name__}: {exc}"


class TelegramChannel(NotificationChannel):
    """Telegram Bot API 渠道：文本 + 富媒体（sendPhoto / sendMediaGroup）"""

    def __init__(self, bot_token: str = None):
        self.name = "telegram"
        self.bot_token = bot_token
        self.enabled = bool(bot_token)

    async def send(self, user_id: int, title: str, content: str,
                   data: Optional[dict] = None, db: Session = None) -> bool:
        """发送 Telegram 通知"""
        if not self.enabled:
            logger.warning("Telegram 通知未配置，跳过发送")
            return False

        # G2-P1-1：自己创建的会话必须显式关闭
        _own_db = db is None
        try:
            # 获取用户的 Telegram ID
            if db is None:
                db = next(get_db())

            # 查找用户的 Telegram ID（r30 N1：旧 telegram_users 表已废弃、
            # 全仓零写入点；绑定统一走 WebUser.telegram_id）
            wu = db.query(models.WebUser).filter(
                models.WebUser.id == user_id
            ).first()
            tg_chat_id = wu.telegram_id if wu else None

            if not tg_chat_id:
                logger.warning(f"用户 {user_id} 没有关联 Telegram 账号")
                return False

            # 真正投递：此前只有一条 status="sent" 的历史记录，消息从未发出
            ok, error = await self._deliver(tg_chat_id, title, content)

            history = models.NotificationHistory(
                notification_type="telegram",
                target=str(tg_chat_id),
                title=title,
                content=content,
                status="sent" if ok else "failed",
                error_message=error,
                sent_at=datetime.now() if ok else None,
            )
            db.add(history)
            db.commit()

            if ok:
                logger.info(f"Telegram 消息已发送给用户 {user_id}")
            else:
                logger.error(f"Telegram 消息发送失败（用户 {user_id}）: {error}")
            return ok

        except Exception as e:
            logger.error(f"发送 Telegram 消息失败: {e}")
            if db:
                db.rollback()
            return False
        finally:
            # G2-P1-1：自己创建的会话必须关闭
            if _own_db and db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    async def _deliver(self, chat_id: int, title: str, content: str) -> tuple[bool, Optional[str]]:
        """调用 Telegram Bot API 投递"""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": f"*{title}*\n\n{content}",
                          "parse_mode": "Markdown"},
                )
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
            return True, None
        except Exception as exc:  # noqa: BLE001 — 投递失败原因要原样记下来
            return False, f"{type(exc).__name__}: {exc}"

    async def _api(self, method: str, payload: dict) -> tuple[bool, Optional[str]]:
        """Bot API 通用调用：返回 (ok, error)。"""
        import httpx

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    f"https://api.telegram.org/bot{self.bot_token}/{method}",
                    json=payload,
                )
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
            body = resp.json()
            if not body.get("ok"):
                return False, f"API error: {str(body)[:200]}"
            return True, None
        except Exception as exc:  # noqa: BLE001 — 投递失败原因要原样记下来
            return False, f"{type(exc).__name__}: {exc}"

    async def send_rich(
        self,
        chat_id: int,
        *,
        text: str,
        photos: Optional[List[str]] = None,
        title: str = "通知",
    ) -> tuple[bool, Optional[str]]:
        """富媒体发送（新片入库通知用）。

        - 无图 → sendMessage
        - 1 张图 → sendPhoto（caption 挂图上）
        - 2~10 张图 → sendMediaGroup（caption 挂第一张）
        - 发图失败 → 自动降级为纯文本消息，保证通知不丢

        text 为 HTML（parse_mode=HTML），调用方负责转义。
        返回 (ok, error)，不抛异常。
        """
        if not self.enabled:
            return False, "Telegram 通知未配置"

        photos = [p for p in (photos or []) if p][:MAX_MEDIA_GROUP]

        ok: bool
        error: Optional[str]
        try:
            if not photos:
                ok, error = await self._api("sendMessage", {
                    "chat_id": chat_id, "text": text, "parse_mode": "HTML",
                })
            elif len(photos) == 1:
                ok, error = await self._api("sendPhoto", {
                    "chat_id": chat_id, "photo": photos[0],
                    "caption": _truncate_caption(text), "parse_mode": "HTML",
                })
                if not ok:
                    # 图挂了（URL 失效等）→ 降级纯文本
                    logger.warning("sendPhoto 失败，降级为纯文本: %s", error)
                    ok, error = await self._api("sendMessage", {
                        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
                    })
            else:
                media = [
                    {
                        "type": "photo", "media": url,
                        **({"caption": _truncate_caption(text), "parse_mode": "HTML"}
                           if i == 0 else {}),
                    }
                    for i, url in enumerate(photos)
                ]
                ok, error = await self._api("sendMediaGroup", {
                    "chat_id": chat_id, "media": media,
                })
                if not ok:
                    logger.warning("sendMediaGroup 失败，降级为纯文本: %s", error)
                    ok, error = await self._api("sendMessage", {
                        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
                    })
        except Exception as exc:  # noqa: BLE001 — 富媒体发送绝不抛异常
            return False, f"{type(exc).__name__}: {exc}"

        self._record_history(chat_id, title, text, ok, error)
        return ok, error

    def _record_history(
        self, chat_id: int, title: str, text: str, ok: bool,
        error: Optional[str],
    ) -> None:
        """记一条通知历史（失败也不影响主流程）。"""
        try:
            db = next(get_db())
            try:
                history = models.NotificationHistory(
                    notification_type="telegram",
                    target=str(chat_id),
                    title=title,
                    content=text[:2000],
                    status="sent" if ok else "failed",
                    error_message=error,
                    sent_at=datetime.now() if ok else None,
                )
                db.add(history)
                db.commit()
            finally:
                db.close()
        except Exception:  # noqa: BLE001 — 历史记录失败不影响通知本身
            logger.warning("通知历史记录失败", exc_info=True)


# ==================== 多渠道通知管理器 ====================

class WebhookChannel(NotificationChannel):
    """通用 webhook 渠道：POST JSON 到配置的 URL。

    通用能力，不写死任何第三方格式（企业微信/钉钉/自建系统都接得上）：
    payload = {"title", "content", "data", "ts", "source": "aetrix"}。
    可选 HMAC-SHA256 签名（X-Signature 头），接收方用 webhook_alert_secret 验签。

    注意：webhook 是"广播到 URL"不是"发给某用户"，send() 的 user_id 参数
    仅为保持渠道接口一致而保留，会被忽略（但会写进 payload 供接收方参考）。

    R16 P3-9 SSRF 防护：URL 仅允许 http/https，且拦截 localhost / .local /
    .internal、私网与保留地址（如 10/8、172.16/12、192.168/16、127/8、
    169.254/16、::1、fe80::/10、fc00::/7）。不安全时只记 warning 并禁用该
    渠道（enabled=False），不抛异常、不断配置加载——管理员手滑配错地址时
    其他渠道照常工作。
    """

    @staticmethod
    def _is_safe_url(url: str) -> bool:
        """SSRF 检查：只放行公网 http/https 地址。"""
        try:
            parsed = urlparse(url)
        except Exception:  # noqa: BLE001
            return False
        if parsed.scheme not in ("http", "https"):
            return False
        host = (parsed.hostname or "").lower().rstrip(".")
        if not host:
            return False
        if (host == "localhost" or host.endswith(".localhost")
                or host.endswith(".local") or host.endswith(".internal")):
            return False
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return True  # 普通公网域名（不是字面量 IP）→ 放行
        # 字面量 IP：私网 / 环回 / 链路本地 / 保留 / 组播 / 未指定一律拦截
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            return False
        return True

    def __init__(self, url: str = None, secret: str = None, timeout: int = 10):
        self.name = "webhook"
        self.url = (url or "").strip()
        self.secret = (secret or "").strip()
        self.timeout = timeout
        self.enabled = bool(self.url)
        if self.enabled and not self._is_safe_url(self.url):
            logger.warning("webhook URL 不安全（疑似内网/保留地址），"
                           "已禁用该渠道: %s", self.url)
            self.enabled = False

    def _sign(self, body: bytes) -> str:
        import hashlib
        import hmac
        return hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()

    async def send(self, user_id: int, title: str, content: str,
                   data: Optional[dict] = None, db: Session = None) -> bool:
        """POST 告警 JSON 到 webhook URL。"""
        if not self.enabled:
            logger.warning("webhook 通知未配置 URL，跳过发送")
            return False
        import json as _json
        import httpx

        payload = {
            "title": title,
            "content": content,
            "user_id": user_id,
            "data": data or {},
            "ts": int(datetime.now().timestamp()),
            "source": "aetrix",
        }
        body = _json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.secret:
            headers["X-Signature"] = self._sign(body)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.post(self.url, content=body, headers=headers)
            if resp.status_code // 100 != 2:
                logger.error("webhook 发送失败: HTTP %s %s",
                             resp.status_code, resp.text[:200])
                return False
            logger.info("webhook 通知已发送: %s", title)
            return True
        except Exception as exc:  # noqa: BLE001 — 投递失败如实返回，不抛异常
            logger.error("webhook 发送异常: %s: %s", type(exc).__name__, exc)
            return False


class NotificationService:
    """通知服务管理器"""

    def __init__(self):
        self.channels: Dict[str, NotificationChannel] = {
            "in_app": InAppChannel(),
        }
        # 从配置加载其他渠道
        self._config_loaded = False
        self._load_channels_from_config()

    def reload_channels(self) -> None:
        """重新从配置加载渠道

        后台「邮件与模板 / Telegram 通知」改完配置要立刻生效（不必重启进程），
        所以保存动作会调这里：站内信保留，其余渠道按最新配置重建。
        """
        self.channels = {"in_app": InAppChannel()}
        self._config_loaded = False
        self._load_channels_from_config()

    def _load_channels_from_config(self):
        """从系统配置加载通知渠道"""
        if self._config_loaded:
            return

        # 以前用 next(get_db()) 拿会话且从不关闭：每次重建渠道都漏一个连接（同一个会话被
        # 拿来做一次查询就丢掉了，SQLite 上表现为连接一直不释放）。这里显式开关。
        db = SessionLocal()
        try:

            # 获取邮件配置
            email_configs = db.query(models.SystemConfig).filter(
                models.SystemConfig.key.like('email_%')
            ).all()
            email_config = {cfg.key: cfg.value for cfg in email_configs}

            if email_config.get('email_enabled') == 'true':
                self.channels['email'] = EmailChannel(
                    smtp_host=email_config.get('email_smtp_host'),
                    smtp_port=int(email_config.get('email_smtp_port', 587)),
                    smtp_user=email_config.get('email_smtp_user'),
                    smtp_password=email_config.get('email_smtp_password'),
                    from_name=email_config.get('email_from_name') or "",
                    from_email=email_config.get('email_from_email') or "",
                )
                logger.info("邮件通知渠道已启用")

            # 获取 Telegram 配置
            tg_configs = db.query(models.SystemConfig).filter(
                models.SystemConfig.key.like('telegram_%')
            ).all()
            tg_config = {cfg.key: cfg.value for cfg in tg_configs}

            if tg_config.get('telegram_bot_token'):
                self.channels['telegram'] = TelegramChannel(
                    bot_token=tg_config.get('telegram_bot_token')
                )
                logger.info("Telegram 通知渠道已启用")

            # 通用 webhook 渠道：后台「通知设置」里配 URL（可选 secret 做 HMAC 验签）。
            # key 用 webhook_ 前缀，与 telegram_% / email_% 的查询模式保持一致。
            webhook_url = db.query(models.SystemConfig).filter(
                models.SystemConfig.key == 'webhook_alert_url'
            ).first()
            webhook_secret = db.query(models.SystemConfig).filter(
                models.SystemConfig.key == 'webhook_alert_secret'
            ).first()
            url_val = (webhook_url.value if webhook_url else "" or "").strip()
            if url_val:
                self.channels['webhook'] = WebhookChannel(
                    url=url_val,
                    secret=(webhook_secret.value if webhook_secret else "") or "",
                )
                logger.info("webhook 通知渠道已启用")

            self._config_loaded = True

        except Exception as e:
            logger.error(f"加载通知渠道配置失败: {e}")
        finally:
            db.close()

    def reload_config(self):
        """重新加载配置"""
        self._config_loaded = False
        self._load_channels_from_config()

    def add_channel(self, name: str, channel: NotificationChannel):
        """添加通知渠道"""
        self.channels[name] = channel

    async def send(self, user_id: int, title: str, content: str,
                   channels: Optional[List[str]] = None,
                   message_type: str = "system",
                   related_id: Optional[int] = None,
                   from_user_id: Optional[int] = None,
                   data: Optional[dict] = None,
                   db: Optional[Session] = None) -> Dict[str, bool]:
        """
        发送多渠道通知

        Args:
            user_id: 接收用户ID
            title: 通知标题
            content: 通知内容
            channels: 通知渠道列表，默认使用所有可用渠道
            message_type: 消息类型
            related_id: 关联ID
            from_user_id: 发送者ID（管理员）
            data: 附加数据

        Returns:
            各渠道发送结果 {channel_name: success}
        """
        if channels is None:
            channels = ["in_app"]  # 默认只发送站内信

        results = {}
        # G2-P1-1：自己创建的会话必须显式关闭
        _own_db = db is None
        db_session = db or next(get_db())
        try:
            for channel_name in channels:
                if channel_name not in self.channels:
                    logger.warning(f"通知渠道 {channel_name} 不存在")
                    results[channel_name] = False
                    continue

                channel = self.channels[channel_name]

                if channel_name == "in_app":
                    success = await channel.send(
                        user_id=user_id,
                        title=title,
                        content=content,
                        message_type=message_type,
                        related_id=related_id,
                        from_user_id=from_user_id,
                        db=db_session
                    )
                else:
                    success = await channel.send(
                        user_id=user_id,
                        title=title,
                        content=content,
                        data=data,
                        db=db_session
                    )

                results[channel_name] = success

        finally:
            # G2-P1-1：自己创建的会话必须关闭
            if _own_db and db_session is not None:
                try:
                    db_session.close()
                except Exception:
                    pass

        return results

    async def get_unread_count(self, user_id: int, db: Optional[Session] = None) -> int:
        """获取用户未读消息数量"""
        # G2-P1-1：自己创建的会话必须显式关闭
        _own_db = db is None
        if db is None:
            db = next(get_db())
        try:
            count = db.query(models.StationMessage).filter(
                and_(
                    models.StationMessage.to_user_id == user_id,
                    models.StationMessage.is_read == False
                )
            ).count()

            return count
        finally:
            if _own_db and db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    async def mark_as_read(self, message_id: int, user_id: int, db: Optional[Session] = None) -> bool:
        """标记消息为已读"""
        # G2-P1-1：自己创建的会话必须显式关闭
        _own_db = db is None
        if db is None:
            db = next(get_db())
        try:
            message = db.query(models.StationMessage).filter(
                and_(
                    models.StationMessage.id == message_id,
                    models.StationMessage.to_user_id == user_id
                )
            ).first()

            if message:
                message.is_read = True
                message.read_at = datetime.now()
                db.commit()

                # 通过 WebSocket 通知未读数更新
                unread_count = await self.get_unread_count(user_id, db)
                await send_notification(
                    notification_type="station.unread_count",
                    user_id=user_id,
                    title="",
                    message="",
                    data={"unread_count": unread_count}
                )

                return True

            return False
        finally:
            if _own_db and db is not None:
                try:
                    db.close()
                except Exception:
                    pass

    async def get_messages(self, user_id: int, limit: int = 50,
                           unread_only: bool = False, db: Optional[Session] = None) -> List[models.StationMessage]:
        """获取用户消息列表"""
        # G2-P1-1：自己创建的会话必须显式关闭
        _own_db = db is None
        if db is None:
            db = next(get_db())
        try:
            query = db.query(models.StationMessage).filter(
                models.StationMessage.to_user_id == user_id
            )

            if unread_only:
                query = query.filter(models.StationMessage.is_read == False)

            messages = query.order_by(models.StationMessage.created_at.desc()).limit(limit).all()

            return messages
        finally:
            if _own_db and db is not None:
                try:
                    db.close()
                except Exception:
                    pass


# ==================== 管理员操作事件 ====================

class AdminEvent:
    """管理员操作事件常量"""

    # 兑换码事件
    EXCHANGE_CODE_GENERATED = "exchange_code.generated"
    EXCHANGE_CODE_USED = "exchange_code.used"

    # 工单事件
    TICKET_CREATED = "ticket.created"
    TICKET_REPLIED = "ticket.replied"
    TICKET_CLOSED = "ticket.closed"

    # 公告事件
    ANNOUNCEMENT_PUBLISHED = "announcement.published"
    ANNOUNCEMENT_UPDATED = "announcement.updated"
    ANNOUNCEMENT_DELETED = "announcement.deleted"

    # 订阅事件
    SUBSCRIPTION_MANUAL = "subscription.manual"
    SUBSCRIPTION_EXTENDED = "subscription.extended"
    SUBSCRIPTION_EXPIRED = "subscription.expired"
    # 到期前提醒（后台周期任务发出，见 backend/reminders.py）
    SUBSCRIPTION_REMINDER = "subscription.reminder"

    # 订单事件（经济）
    RECHARGE_SUCCESS = "economy.recharge_success"
    SUBSCRIPTION_SUCCESS = "economy.subscription_success"
    # 退款：属于“钱”的事件，不要复用 subscription.expired（那个语义是会员到期）
    ORDER_REFUNDED = "economy.order_refunded"

    # 求片事件
    MEDIA_SEEK_APPROVED = "media_seek.approved"
    MEDIA_SEEK_REJECTED = "media_seek.rejected"
    MEDIA_SEEK_COMPLETED = "media_seek.completed"

    # 系统事件
    SYSTEM_MAINTENANCE = "system.maintenance"
    USER_WARNING = "user.warning"
    USER_BANNED = "user.banned"


async def notify_admin_event(
    event_type: str,
    user_id: Optional[int],
    title: str,
    content: str,
    related_id: Optional[int] = None,
    from_admin_id: Optional[int] = None,
    channels: Optional[List[str]] = None
) -> Dict[str, bool]:
    """
    触发管理员操作事件通知

    这是实现前后台联动的核心函数
    管理员在后台的操作会通过这个函数通知到用户前台

    数据流：
    管理员操作 → 后台API → notify_admin_event() → NotificationService.send()
            → 站内信数据库 + WebSocket推送 → 用户前台实时更新
    """
    message_type = event_type.split('.')[0]  # ticket, announcement, subscription, exchange_code, media_seek

    notification_service = get_notification_service()

    return await notification_service.send(
        user_id=user_id,
        title=title,
        content=content,
        channels=channels or ["in_app"],
        message_type=message_type,
        related_id=related_id,
        from_user_id=from_admin_id
    )


def _persist_staff_messages(
    db: Session,
    title: str,
    content: str,
    message_type: str,
    related_id: Optional[int],
) -> List[int]:
    """同步落库：给每位启用中的管理员写一条站内消息，返回管理员 id 列表

    这一步整段跑在**线程池**里（见 notify_staff_users）：同步 SQLAlchemy 跑在事件循环上会
    把全站请求按在这次写事务后面（SQLite 是亚毫秒，跨机 PostgreSQL 每查询一个 RTT）。
    """
    staff_users = db.query(models.WebUser).filter(
        models.WebUser.is_staff == True,  # noqa: E712
        models.WebUser.is_active == True,  # noqa: E712
    ).all()
    if not staff_users:
        return []

    for staff in staff_users:
        db.add(models.StationMessage(
            from_user_id=None,
            to_user_id=staff.id,
            title=title,
            content=content,
            message_type=message_type,
            related_id=related_id,
            is_read=False,
        ))
    db.commit()
    return [staff.id for staff in staff_users]


async def notify_staff_users(
    db: Session,
    title: str,
    content: str,
    message_type: str = "system",
    related_id: Optional[int] = None,
) -> int:
    """通知全部管理员（用户 → 后台方向）

    补齐此前用户侧只写了「TODO: 通知管理员」却没有实现的通知链路：
    新工单 / 新求片提交后会落站内消息给每一位启用中的管理员，并实时推送。

    返回通知到的管理员数量。
    """
    staff_ids = await run_in_threadpool(
        _persist_staff_messages, db, title, content, message_type, related_id
    )

    for staff_id in staff_ids:
        try:
            await send_notification(
                notification_type=f"station.{message_type}",
                user_id=staff_id,
                title=title,
                message=content,
                data={"related_id": related_id},
            )
        except Exception as exc:  # 实时推送失败不影响站内消息落库
            logger.warning("管理员实时通知推送失败: %s", exc)

    return len(staff_ids)


def _persist_broadcast_messages(
    title: str,
    content: str,
    message_type: str,
    related_id: Optional[int],
) -> List[int]:
    """同步落库：给每个启用中的用户写一条站内消息，返回用户 id 列表（失败返回空）

    自己开一个会话（不在路由的会话上跑），整段放进线程池——广播要一次写几万行，
    正是最不能占着事件循环的写。失败按「没通知到」处理，由调用方决定怎么回。
    """
    db = next(get_db())
    try:
        user_ids = [
            row[0] for row in db.query(models.WebUser.id).filter(
                models.WebUser.is_active == True  # noqa: E712
            ).all()
        ]
        if not user_ids:
            return []

        db.bulk_save_objects([
            models.StationMessage(
                from_user_id=None,
                to_user_id=uid,
                title=title,
                content=content,
                message_type=message_type,
                related_id=related_id,
                is_read=False,
            )
            for uid in user_ids
        ])
        db.commit()
        return user_ids
    except Exception as exc:  # noqa: BLE001 — 落库失败要让调用方知道
        db.rollback()
        logger.error("广播站内消息落库失败: %s", exc)
        return []
    finally:
        db.close()


async def notify_all_users(
    event_type: str,
    title: str,
    content: str,
    data: Optional[dict] = None
) -> int:
    """广播通知给**全部启用中的用户**（不只是当时在线的人）

    用于：
    - 系统公告发布
    - 系统维护通知
    - 重要系统更新

    此前这里走的是 ``NotificationService.broadcast``，而它只遍历"当前 WebSocket 在线用户"：
    公告发布时不在线的用户**永远收不到这条站内消息**，但接口返回的文案是"已推送给所有用户"。
    现在按站内信语义落库给每个启用中的用户，再对其中在线的人做实时推送，
    返回值是实际收到站内消息的用户数。
    """
    message_type = event_type.split(".")[0]
    related_id = (data or {}).get("announcement_id")
    user_ids = await run_in_threadpool(
        _persist_broadcast_messages, title, content, message_type, related_id
    )
    if not user_ids:
        return 0

    # 在线用户的实时推送（失败不影响已落库的站内消息）
    online = set(manager.get_online_users())
    for uid in user_ids:
        if uid not in online:
            continue
        try:
            await send_notification(
                notification_type=f"station.{message_type}",
                user_id=uid,
                title=title,
                message=content,
                data=data or {"event_type": event_type},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("广播实时推送失败（用户 %s）: %s", uid, exc)

    return len(user_ids)


# ==================== 全局单例 ====================

_notification_service: Optional[NotificationService] = None


def get_notification_service() -> NotificationService:
    """获取通知服务单例"""
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService()
    return _notification_service


def refresh_channels() -> None:
    """配置变更后重建渠道（能力中心保存邮件 / Telegram 配置时调用）"""
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService()
        return
    _notification_service.reload_channels()


# ==================== 导出 ====================

__all__ = [
    "NotificationService",
    "InAppChannel",
    "EmailChannel",
    "TelegramChannel",
    "WebhookChannel",
    "get_notification_service",
    "refresh_channels",
    "notify_admin_event",
    "notify_all_users",
    "notify_staff_users",
    "AdminEvent",
]
