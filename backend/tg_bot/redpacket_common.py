"""TG 红包共享逻辑（B3）：总开关、callback_data 编解码、按钮与消息文案。"""
from __future__ import annotations

from backend.integrations import store
from backend.tg_bot import layout

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
    保留 "总额 X 积分 / 共 N 个 / 剩余 M 个" 口径（测试与客户端展示依赖）。
    """
    return layout.card(
        layout.title("幸运红包！", sender_name),
        [
            layout.row("🧧", "红包", f"<b>{sender_name} 的幸运红包</b>"),
            layout.row("💰", "总额", f"<b>总额 {packet.total_amount} 积分</b> ｜ <b>共 {packet.total_count} 个</b>"),
            layout.row("📦", "剩余", f"<b>剩余 {packet.remaining_count} 个</b>（待领 <b>{packet.remaining_amount}</b> 积分）"),
            layout.row("⏰", "有效期", "24 小时后过期，手慢无"),
        ],
        ["👇 点击下方按钮开抢"],
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
            layout.row("💸", "退款", "剩余积分已退回发送者"),
        ]
    return layout.card(
        layout.title("红包！", sender_name),
        [
            layout.row("🧧", "红包", f"<b>{sender_name} 的红包</b>"),
            layout.row("💰", "总额", f"总额 {packet.total_amount} 积分 ｜ 共 {packet.total_count} 个"),
            *status_rows,
        ],
    )
