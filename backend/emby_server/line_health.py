"""播放可观测（2026-10-17 起 302 直链时代）：单路径快照组装

Drive 文件 302 到谷歌直链（播放器自拉，字节不经本机）；
115/WebDAV/AList/rclone 挂载源暂不支持（410）；转码走 ffmpeg 自拉。
面板展示：播放流量（本进程）：请求数 / 出流量字节（302 时代恒接近 0）。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from backend.emby_server import line_stats


def snapshot(db: Session) -> dict:
    """单路径完整快照（管理端一次拿全）"""
    counts = line_stats.snapshot()
    counter = counts[0] if counts else {}

    return {
        "path": "relay",
        "label": "302 直链",
        "summary": "Drive 文件 302 到谷歌直链（播放器自拉，字节不经本机）；"
                   "115 等挂载源同样 302 到挂载解析出的直链；本机文件直接流。",
        "requests": int(counter.get("requests") or 0),
        "bytes_out": int(counter.get("bytes_out") or 0),
        "idle_seconds": counter.get("idle_seconds"),
        "uptime_seconds": line_stats.uptime_seconds(),
        "scope_note": "流量计数只统计本进程（302 直链时代播放字节不经本机，"
                      "出流量恒接近 0；不含转码时 ffmpeg 的拉流）。",
        # 兼容老前端：lines 保留单元素（label 与顶层快照一致）
        "lines": [{
            "line": "relay",
            "label": "302 直链",
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
