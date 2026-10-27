"""播放交付形态：302 直链 vs 令牌流（FMBY 播放机制形状，全部学第 6 项）。

FMBY 深挖的区分（报告 fmby-playback-exclusive-deepdive-20261024 §2/§3，
只学机制形状、不抄代码）：
- 302 直链：服务端不吐一个字节，只给播放器一个可直接拉的上游地址；
  Range 由播放器直接跟上游谈，服务端不解析、不透传、不对齐。
- 令牌流：服务端带凭据向上游取字节、再吐给播放器；客户端 Range
  必须端到端透传到上游/本地文件，响应必须是 206 且 Content-Range
  与实际字节区间精确对齐（``bytes start-end/total``），否则播放器
  会按错区间续播。

我方现状（本模块不重复实现，只钉死口径与纯函数）：
- 302 直链：drive_direct / byos115_direct —— video_stream 下发 302
  （drive_usercontent_download_url / 115 现签地址），字节不经服务器。
- 令牌流：drive_relay / mount_relay / byos_drive_relay / local_file
  —— Range 透传与 206 对齐的唯一实现在 streaming.py：
  serve_mount_relay_async（Range 原样发上游、上游 206/Content-Range
  原样透回）与 serve_file（本地文件 Range → 206，非法 Range → 416）。
  本模块不许出现第二套 Range 解析/转发实现（横切只许一套）。
- 服务端转码已删除：不存在“服务端转码流”这种交付形态，不要虚列。

本模块只提供两样纯东西：变体 → 交付形态的映射（供打点与文档
口径统一），以及 Range 解析/Content-Range 拼串的纯函数（供测试
钉死 206 对齐语义；生产路径仍走 streaming.py 的实现，两者语义
必须一致，测试即契约）。
"""

from __future__ import annotations

DIRECT_302 = "direct_302"
TOKEN_STREAM = "token_stream"

#: 变体名 → 交付形态。未知变体名默认令牌流（fail-closed：宁可走
#: 服务端吐字节的可控路径，也不把未知变体当成直链下发）。
_DELIVERY_BY_VARIANT = {
    "drive_direct": DIRECT_302,
    "byos115_direct": DIRECT_302,
    "drive_relay": TOKEN_STREAM,
    "mount_relay": TOKEN_STREAM,
    "byos_drive_relay": TOKEN_STREAM,
    "local_file": TOKEN_STREAM,
}


def delivery_mode_for_variant(variant_name: str) -> str:
    """变体的交付形态：302 直链（服务端不吐字节）或令牌流（206）。"""
    return _DELIVERY_BY_VARIANT.get((variant_name or "").strip(), TOKEN_STREAM)


def parse_range_header(header, total: int):
    """解析 ``Range: bytes=start-end``（纯函数，206 对齐契约）。

    返回 ``(start, end)``（闭区间，含 end）或 None（无 Range /
    非法 / 超出范围——调用方按 416 处理，与 streaming.serve_file
    同口径）：
    - ``bytes=100-199`` → (100, 199)；
    - ``bytes=100-`` → (100, total-1)；
    - ``bytes=-500``（后缀）→ (total-500, total-1)；
    - end 超过 total-1 时截断到 total-1；start >= total 时 None；
    - 多区间（逗号）与非 bytes 单位一律 None（本架构只做单区间）。
    """
    if total is None or total <= 0:
        return None
    if not header:
        return None
    text = str(header).strip()
    if not text.lower().startswith("bytes="):
        return None
    spec = text[len("bytes="):].strip()
    if "," in spec:
        return None
    if spec.startswith("-"):
        try:
            suffix = int(spec[1:])
        except ValueError:
            return None
        if suffix <= 0:
            return None
        start = max(0, total - suffix)
        return (start, total - 1)
    parts = spec.split("-", 1)
    if len(parts) != 2 or not parts[0]:
        return None
    try:
        start = int(parts[0])
        end = int(parts[1]) if parts[1] else total - 1
    except ValueError:
        return None
    if start < 0 or start >= total:
        return None
    if end < start:
        return None
    return (start, min(end, total - 1))


def content_range_header(start: int, end: int, total: int) -> str:
    """206 必需的 Content-Range 拼串：``bytes start-end/total``。"""
    return f"bytes {int(start)}-{int(end)}/{int(total)}"
