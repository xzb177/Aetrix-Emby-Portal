"""播放可观测（2026-10 简化）：单路径快照组装

只有一条播放路径（中转），面板展示：
- 播放流量（本进程）：请求数 / 出流量字节
- 本地缓存层状态：启用/命中率/占用
- CDN 域名状态：启用/域名
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from backend.emby_server import cdn, line_stats

logger = logging.getLogger(__name__)


def _cdn_state(db: Session) -> dict:
    try:
        return cdn.config_payload(db)
    except Exception:  # noqa: BLE001
        logger.debug("读取 CDN 配置失败（按未启用计）", exc_info=True)
        return {"enabled": False, "domain": "", "normalized": None}


def snapshot(db: Session) -> dict:
    """单路径完整快照（管理端一次拿全）"""
    counts = line_stats.snapshot()
    counter = counts[0] if counts else {}
    cdn_state = _cdn_state(db)

    return {
        "path": "relay",
        "label": "代理中转",
        "summary": "本服务代理转发（流量过 VPS）；CF 在域名前挡一层，"
                   "热门分片由边缘缓存",
        "requests": int(counter.get("requests") or 0),
        "bytes_out": int(counter.get("bytes_out") or 0),
        "idle_seconds": counter.get("idle_seconds"),
        "uptime_seconds": line_stats.uptime_seconds(),
        "cdn": {
            "enabled": bool(cdn_state.get("enabled")),
            "domain": cdn_state.get("normalized") or "",
            "ready_note": (
                f"回源域名 {cdn_state.get('normalized')}"
                if cdn_state.get("enabled") and cdn_state.get("normalized")
                else "未启用" if not cdn_state.get("enabled")
                else "已启用但域名未填（或不合法）"
            ),
        },
        "scope_note": "流量计数只统计本进程（流量确实只从本机出去，"
                      "跨节点合并会重复计算）。"
                      "流量指本服务响应体实际吐出的字节（Range 分片与远程代理），"
                      "不含转码时 ffmpeg 的拉流与整文件直发。",
        # 兼容老前端：lines 保留单元素，label 与原来一致
        "lines": [{
            "line": "relay",
            "label": "代理中转",
            "requests": int(counter.get("requests") or 0),
            "bytes_out": int(counter.get("bytes_out") or 0),
            "idle_seconds": counter.get("idle_seconds"),
        }],
        "total_users": 0,
        "default_line": "relay",
    }


def card_for(db: Session, line: str) -> Optional[dict]:
    """单路径卡片（兼容旧调用）"""
    snap = snapshot(db)
    for card in snap["lines"]:
        if card["line"] == line:
            return card
    return None


__all__ = ["card_for", "snapshot"]
