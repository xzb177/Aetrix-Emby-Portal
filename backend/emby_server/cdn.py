"""分片缓存头策略（2026-10-17 瘦身：CDN 域名预留能力已删除）

本模块原先是「CDN 域名预留」（播放三层第 2/3 层）：域名配置 + 启用开关
（``cdn_domain`` / ``cdn_enabled``）+ 播放 URL 改写。2026-10-17 用户拍板删除
整个 CDN 配置能力（生产从未启用，且与流节点的域名改写重复）。

**保留的另一半**：分片缓存头口径——它与 CDN 域名配置无关，是给
转码/直流分片的通用 HTTP 缓存语义，被以下几处直接使用：

- ``backend/emby_server/api.py``：直流文件的 ``Cache-Control``、
  转码播放列表（no-store）、转码分片（可缓存）；
- ``backend/main.py`` 的 ASGI 中间件：分片路径跳过强制 no-store
  （用 ``is_segment_path`` 判定）。

- 视频/音频**分片**（HLS .ts/.m4s、直接流）→ ``public, max-age``，让边缘/浏览器
  缓存热门分片（分片写完不可变，缓存多久都安全）；
- 播放列表（.m3u8）、API、302 跳转 → ``no-store``，绝不被缓存。
"""
from __future__ import annotations

# 分片缓存时长（秒）：分片写完不可变，边缘可以放心缓存。
# （转码删除后不再有转码会话分片；这里只服务直接流分片。）
SEGMENT_CACHE_SECONDS = 6 * 3600
# 边缘缓存一般要比浏览器激进：给 s-maxage 留长一些，浏览器短一点
SEGMENT_CACHE_HEADER = f"public, max-age={SEGMENT_CACHE_SECONDS}, s-maxage={SEGMENT_CACHE_SECONDS}, immutable"
# API 响应、302 跳转、播放列表：绝不能被缓存（凭据在查询串里/内容随时变）
NO_STORE = "no-store"


def is_segment_path(path: str) -> bool:
    """这是不是可以缓存的视频/音频分片请求。

    只认分片形态：HLS 切片（.ts/.m4s/.aac 包在 videos/{id}/ 下）与直接流
    （/stream 或 /stream.{容器}）。播放列表、API、302 都不算。
    """
    lowered = (path or "").lower().split("?", 1)[0]
    if lowered.endswith((".m3u8", ".json", ".xml", ".vtt", ".srt")):
        return False
    if "/videos/" in lowered and lowered.endswith((".ts", ".m4s", ".aac")):
        return True
    return lowered.endswith(("/stream", ".mkv", ".mp4", ".strm", ".ts", ".m4s", ".aac")) and \
        ("/videos/" in lowered or lowered.endswith(("/stream",)) or
         ".stream." in lowered.rsplit("/", 1)[-1])


def cache_control_for(path: str) -> str:
    """按路径给 Cache-Control：分片可缓存，其余一律 no-store。"""
    return SEGMENT_CACHE_HEADER if is_segment_path(path) else NO_STORE
