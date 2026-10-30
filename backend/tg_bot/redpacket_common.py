"""TG 红包共享逻辑（B3）：总开关、callback_data 编解码、按钮与消息文案。"""
from __future__ import annotations

import logging

from backend.integrations import store
from backend.tg_bot import layout, sender

logger = logging.getLogger(__name__)

# callback_data 前缀，按钮回调形如 "redpacket_claim:<packet_id>"
CALLBACK_PREFIX = "redpacket_claim:"
# 红包功能开关（SystemConfig），"true"=开启，默认开启（B4 统一命名）
CONFIG_KEY = "bot_redpacket_enabled"


def enabled(db) -> bool:
    """红包功能开关是否开启（默认开启）。"""
    return store.get_value(db, CONFIG_KEY, "true").strip().lower() == "true"


def parse_claim_data(data: str | None) -> int | None:
    """解析按钮 callback_data，返回 packet_id；格式不对返回 None。"""
    if not data or not data.startswith(CALLBACK_PREFIX):
        return None
    raw = data[len(CALLBACK_PREFIX):]
    if not raw or not raw.isdigit():
        return None
    return int(raw)


def claim_markup(packet_id: int) -> dict:
    """抢红包 inline 按钮的 reply_markup。"""
    return {
        "inline_keyboard": [
            [{"text": "🧧 抢红包", "callback_data": f"{CALLBACK_PREFIX}{packet_id}"}]
        ]
    }


def packet_text(packet, sender_name: str) -> str:
    """红包进行中的按钮消息正文（卡片式）。

    packet 是 backend.models.RedPacket 对象；sender_name 须已做 HTML 转义。
    保留 "总额 X 能量 / 共 N 个 / 剩余 M 个" 口径（测试与客户端展示依赖）。
    """
    return layout.card(
        layout.title("幸运红包！", sender_name),
        [
            layout.row("🧧", "红包", f"<b>{sender_name} 的幸运红包</b>"),
            layout.row("💰", "总额", f"<b>总额 {packet.total_amount} 能量</b> ｜ <b>共 {packet.total_count} 个</b>"),
            layout.row("📦", "剩余", f"<b>剩余 {packet.remaining_count} 个</b>（待领 <b>{packet.remaining_amount}</b> 能量）"),
            layout.row("⏰", "有效期", "24 小时后过期，手慢无"),
        ],
        ["👇 点击下方按钮开抢"],
    )


def claim_feedback_text(claimer_name: str, amount: int, remaining_count: int) -> str:
    """抢红包成功后的群内反馈消息正文（卡片式，纯文案函数）。

    claimer_name 须已做 HTML 转义（与 packet_text 的 sender_name 同口径）。
    这条反馈消息按 bot_msg_autodelete_seconds 自毁（默认 30 秒），
    红包消息本体不在此列、永不自毁。
    """
    return layout.card(
        layout.title("红包动态"),
        [
            layout.row("🧧", "抢红包",
                       f"<b>{claimer_name}</b> 抢到了 <b>{amount} 能量</b> · 还剩 <b>{remaining_count} 个</b>"),
        ],
    )


def finished_text(packet, sender_name: str, reason: str) -> str:
    """红包终态的按钮消息正文（卡片式）。

    reason: "empty"（已抢完）/ "expired"（已过期）。
    """
    if reason == "empty":
        status_rows = [layout.row("🎉", "状态", "<b>红包已抢完</b>")]
    else:
        status_rows = [
            layout.row("⌛", "状态", "<b>红包已过期</b>"),
            layout.row("💸", "退款", "剩余能量已退回发送者"),
        ]
    return layout.card(
        layout.title("红包！", sender_name),
        [
            layout.row("🧧", "红包", f"<b>{sender_name} 的红包</b>"),
            layout.row("💰", "总额", f"总额 {packet.total_amount} 能量 ｜ 共 {packet.total_count} 个"),
            *status_rows,
        ],
    )


def send_packet_message(db, chat_id: int, text: str,
                        reply_markup: dict | None = None) -> tuple[bool, str | None]:
    """发送红包消息本体：封面照片消息优先，失败一律回落纯文本。

    本体形态（用户 2026-10-30 群内真机截图实证后定）：封面图
    （layout.REDPACKET_IMAGE）+ caption=现行 packet_text 文案 + 现行
    抢按钮。caption 现长约 200 字、远低于 1024 上限（测试有锁）。
    红包创建绝不因封面失败而失败：封面资产缺失、caption 超限或
    sendPhoto 失败时回落 send_message 纯文本（与改造前形态一致）。
    本体永不自毁的豁免在 router 分发路径上判定，本函数不涉自毁调度。
    """
    if layout.REDPACKET_IMAGE.is_file() and layout.caption_fits(text):
        ok, err = sender.send_photo(
            db, chat_id, text, reply_markup,
            photo_path=layout.REDPACKET_IMAGE)
        if ok:
            return True, None
        logger.warning(
            "tg redpacket cover photo send failed, falling back to text: "
            "chat=%s err=%s", chat_id, err)
    return sender.send_message(db, chat_id, text, reply_markup)
