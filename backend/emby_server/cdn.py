"""CDN 域名预留能力（播放三层第 2/3 层极简预留版）

**定位**：只做「域名预留」——管理员把一个回源到本服务的 CDN 域名填进后台并启用，
播放 URL 就改成走该域名，让 CDN 边缘缓存热门视频分片，躲开 Google Drive 的
单文件下载配额。不做备案、不做厂商对接、不做签名鉴权（CDN 回源时带原样查询串，
本服务的鉴权逻辑不变；域名本身必须由管理员配好回源与缓存规则）。

- 配置落在 ``SystemConfig``（与播放策略同一套机制：``backend/playback_policy.py``
  的热读短 TTL + 保存即失效），EM 与 EA 共用同一个库 → 后台改完两边同时生效；
- **默认关闭**（``cdn_enabled=false`` 且域名为空）：关闭时所有行为与升级前逐字节
  一致，客户端拿到的 URL 与今天完全相同；
- 开启后改写的是**播放面**的 URL（PlaybackInfo 的 DirectStreamUrl / TranscodingUrl、
  字幕 DeliveryUrl、HLS 播放列表与变体行）；302 的 Location 不改写（直链目标是
  Google/源站，不是本服务域名，改了反而坏）；
- 与 ``play_line`` 打通：新增 ``cdn`` 线路（用户维度），选择 CDN 的用户在
  ``video_stream`` 直接按 CDN 回源口径处理（直链 302 保留——CDN 只在前面挡
  回源流量）；未选 cdn 的用户 URL 不改写。
- **缓存头策略**（本模块的另一半）：
  - 视频/音频**分片**（HLS .ts/.m4s、直接流）→ ``public, max-age``，让边缘缓存
    热门分片；
  - 播放列表（.m3u8）、API、302 跳转 → ``no-store``，绝不被缓存。
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from backend import config_service
from backend.integrations import store

# SystemConfig 键（与播放策略键同一命名域）
CONFIG_CDN_DOMAIN = "cdn_domain"
CONFIG_CDN_ENABLED = "cdn_enabled"

# 分片缓存时长（秒）：HLS 分片是不可变的（ffmpeg 写完就不改），边缘可以放心缓存。
# 转码会话的分片同样不可变（一个 session 一份内容，session 票据在查询串里）。
SEGMENT_CACHE_SECONDS = 6 * 3600
# CDN 边缘缓存一般要比浏览器激进：给 s-maxage 留长一些，浏览器短一点
SEGMENT_CACHE_HEADER = f"public, max-age={SEGMENT_CACHE_SECONDS}, s-maxage={SEGMENT_CACHE_SECONDS}, immutable"
# API 响应、302 跳转、播放列表：绝不能被缓存（凭据在查询串里/内容随时变）
NO_STORE = "no-store"

_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_HOST_RE = re.compile(
    r"^(?=.{1,253}\Z)(?!-)[a-z0-9-]{1,63}(?:\.[a-z0-9-]{1,63})*\.?[a-z]{2,63}\Z",
    re.IGNORECASE,
)


def _raw(db: Session, key: str) -> str:
    """统一热读（只许这一套）：短 TTL 缓存，保存时失效"""
    return store.get_value(db, key, "")


def configured_domain(db: Session) -> str:
    """管理员配置的 CDN 域名（原样返回，可能是空串 = 未配置）"""
    return (_raw(db, CONFIG_CDN_DOMAIN) or "").strip()


def enabled(db: Session) -> bool:
    """CDN 预留是否启用：必须显式开关 + 域名合法，两者齐备才生效（默认关闭）"""
    if (_raw(db, CONFIG_CDN_ENABLED) or "").strip().lower() not in ("true", "1", "yes", "on"):
        return False
    return normalize_domain(configured_domain(db)) is not None


def normalize_domain(raw: Optional[str]) -> Optional[str]:
    """把管理员输入归一化成 ``scheme://host``（无尾斜杠）。

    - 允许 ``cdn.example.com``、``https://cdn.example.com``、``https://cdn.example.com/``；
    - 端口、路径都接受但会被规整（路径无意义，直接剥掉）；
    - 协议只许 http/https（省略时默认 https）；非法输入返回 None，
      调用方按「未配置」处理——一个写错的域名绝不能把播放打挂。
    """
    text = (raw or "").strip()
    if not text:
        return None
    if not _SCHEME_RE.match(text):
        text = "https://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    try:
        port = parts.port
    except ValueError:  # 端口非法（如 :abc / :99999）：按未配置处理
        return None
    host = parts.hostname.lower()
    netloc = host + (f":{port}" if port else "")
    if not _HOST_RE.match(host):
        return None
    return f"{parts.scheme}://{netloc}"


def origin_base(db: Session, fallback: str) -> str:
    """播放 URL 该用的基址：启用时是 CDN 域名，否则回落到请求自身（现状）。"""
    if enabled(db):
        cdn = normalize_domain(configured_domain(db))
        if cdn:
            return cdn
    return fallback


def rewrite_url(db: Session, url: str, fallback_base: str) -> str:
    """把 ``fallback_base`` 开头的播放 URL 换成 CDN 域名（未启用/非本服务 URL 原样返回）。

    只处理以 fallback_base（本服务从请求上取到的基址）开头的 URL；任何其它前缀
    （Google 直链、第三方）一律原样返回——本模块只做「域名预留」，绝不改写
    别人的地址。
    """
    if not url:
        return url
    if not enabled(db):
        return url
    cdn = normalize_domain(configured_domain(db))
    if not cdn:
        return url
    base = (fallback_base or "").rstrip("/")
    if base and url.startswith(base + "/"):
        return cdn + url[len(base):]
    return url


def is_segment_path(path: str) -> bool:
    """这是不是 CDN 边缘可以缓存的视频/音频分片请求。

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


def play_lines() -> tuple[str, ...]:
    """暴露给 play_line 的线路注册点（避免循环导入，play_line 反过来 import 这里）"""
    from backend.emby_server import play_line
    return play_line.PLAY_LINES


def config_payload(db: Session) -> dict:
    """管理后台回显（不含任何敏感信息；domain 是管理员自己填的）"""
    domain = configured_domain(db)
    return {
        "domain": domain,
        "normalized": normalize_domain(domain) or "",
        "enabled": enabled(db),
        "segment_cache_header": SEGMENT_CACHE_HEADER,
    }


def write_config(db: Session, domain: str, enabled_flag: bool) -> dict:
    """写回配置（管理后台 PUT）。

    校验失败抛 ``ValueError``（路由转 400 并说清怎么改）；
    域名与开关一起写，保存即失效热缓存（EM/EA 同库，两边同时生效）。

    配置瘦身第 4 批 4-5：落库动作收敛到 ``config_service.save_config``。
    域名归一化（禁路径）保留在端点层，行为与收敛前逐字一致。
    """
    domain_text = (domain or "").strip()[:255]
    if enabled_flag:
        normalized = normalize_domain(domain_text)
        if normalized is None:
            raise ValueError(
                "CDN 域名格式不对：填 cdn.example.com 或 https://cdn.example.com，"
                "不能带路径；启用开关打开时域名必填"
            )
        domain_text = normalized
    # 统一写入通道：校验（纵深防御，端点层已归一化）→ 批量落库 → 写时失效
    # （承接原 store.invalidate() 全清：只动了这两个键，读侧等价）。
    # db.commit() 保留在本函数内部（调用方依赖它提交）。
    config_service.save_config(
        db,
        {
            CONFIG_CDN_DOMAIN: domain_text,
            CONFIG_CDN_ENABLED: "true" if enabled_flag else "false",
        },
        schema="cdn",
    )
    db.commit()
    return config_payload(db)
