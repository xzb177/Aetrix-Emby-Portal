"""Google Drive 服务端凭据中转模块。

定位：
- .strm 文件中的 Google Drive 直链保持私有（不公开分享），服务端 relay 拉流时
  通过本模块自动附加服务账号（SA）的 Bearer token，客户端永远看不到直链。
- SA 的发现与 access_token 换取（JWT 断言换 token）统一复用 ``drive_changes``
  模块的既有实现（``_discover_sa_files`` / ``_sa_access_token``），
  横切能力只维护一套，本模块不重复实现 JWT 逻辑。
- access_token 在本模块再做一层进程内"按 SA 分账号缓存"：以 SA 的
  ``client_email`` 为 key，每个 SA 独立存 (token, expire_at)，TTL 55 分钟，
  剩余有效期大于 120 秒直接命中；由 threading.Lock 保护，减少重复换 token 的开销。
- 任何失败（无可用 SA、换 token 异常等）均返回 None，由调用方降级处理，
  本模块永不抛出异常。
"""

from __future__ import annotations

import logging
import threading
import time
from types import ModuleType
from typing import Any
from urllib.parse import parse_qsl, urlparse

__all__ = [
    "DRIVE_HOSTS",
    "is_drive_url",
    "get_drive_bearer_token",
    "drive_auth_headers",
    "drive_api_media_url",
]

logger = logging.getLogger(__name__)

#: 需要进行鉴权的 Google Drive 相关域名（host 小写比较）
DRIVE_HOSTS: frozenset[str] = frozenset(
    {
        "drive.google.com",
        "drive.usercontent.google.com",
        "docs.google.com",
        "www.googleapis.com",
    }
)

# Google access_token 通常 1 小时有效，这里按 55 分钟缓存，留出余量
_TOKEN_TTL_SECONDS: float = 55 * 60
# 缓存 token 剩余有效期大于该值（秒）时直接命中
_TOKEN_MIN_REMAIN_SECONDS: float = 120

_cache_lock = threading.Lock()
# P1 修复（审查）：token 缓存从"全局单条"改为"按 SA 分账号"。
# key = SA 的 client_email（稳定唯一标识），value = (token, expire_at)。
# expire_at 以 time.monotonic() 为基准。此前全局缓存 55 分钟，
# 导致 100 个 SA 的轮询形同虚设、流量全压在单个 SA 上把配额干爆（probe 403）。
_token_cache: dict[str, tuple[str, float]] = {}

# SA 轮换：复用 drive_changes._sa_rotation_start（带随机抖动的统一实现），
# 本模块不再维护独立计数器（横切能力只许一套）。


def _reset_state() -> None:
    """清空按 SA 分账号的 token 缓存。

    测试/调试用的显式重置点（生产路径不会调用）。
    SA 轮换计数器已收敛到 drive_changes._sa_rotation_start，抖动本身
    不需要确定性复位，故这里不再复位轮询起点。
    """
    with _cache_lock:
        _token_cache.clear()


def _load_drive_changes() -> ModuleType:
    """惰性导入并返回 drive_changes 模块。

    drive_changes 依赖较重且可能拉起线程，故不在模块顶层导入，等到真正需要
    凭据时才加载，同时规避潜在的循环导入。
    """
    try:
        from . import drive_changes
    except ImportError:  # 兼容扁平导入（直接以目录方式运行）的场景
        import drive_changes
    return drive_changes


def is_drive_url(url: str) -> bool:
    """判断 url 是否指向 Google Drive 相关域名（host 小写比较）。"""
    if not url:
        return False
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = (parsed.hostname or "").lower()
    return host in DRIVE_HOSTS


def _is_sa_dict(obj: Any) -> bool:
    """判断对象是否为可用的 SA 凭据字典（含 client_email 与 private_key）。"""
    return (
        isinstance(obj, dict)
        and bool(obj.get("client_email"))
        and bool(obj.get("private_key"))
    )


def _read_sa_file(path: str) -> dict[str, Any] | None:
    """读取 SA json 文件。

    ``drive_changes`` 只导出了 ``_discover_sa_files``（文件列表）与
    ``_sa_access_token``（JWT 换 token），没有导出"读单个 SA 文件"的辅助函数，
    这里用标准库读一次（JSON 解析失败返回 None）。
    """
    import json

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _pick_sa() -> dict[str, Any] | None:
    """从 drive_changes 发现的 SA 文件中轮询取一个可用的 SA 字典。

    P1 修复（审查）：此前永远取第一个可用 SA，全站播放回源流量压在单个 SA
    配额上（与"100 子账号轮询"的设计相悖）。现按 round-robin 轮换，
    轮换实现统一收敛到 drive_changes._sa_rotation_start，本模块不再维护独立计数器。
    """
    try:
        drive_changes = _load_drive_changes()
        sa_files = drive_changes._discover_sa_files() or []
    except Exception:
        return None
    if not sa_files:
        return None
    # 轮换起点：经 drive_changes 的统一实现取得（带随机抖动，反风控）
    start = drive_changes._sa_rotation_start(len(sa_files))
    for offset in range(len(sa_files)):
        entry = sa_files[(start + offset) % len(sa_files)]
        try:
            sa = entry if _is_sa_dict(entry) else _read_sa_file(entry)
        except Exception:
            continue
        if _is_sa_dict(sa):
            return sa
    return None


def get_drive_bearer_token() -> str | None:
    """获取 Drive 访问用的 Bearer access_token；失败返回 None，永不抛异常。

    每次调用都先经 ``_pick_sa()`` round-robin 选出一个 SA，再以该 SA 的
    ``client_email`` 为 key 查进程内分账号缓存：剩余有效期大于 120 秒直接返回；
    否则经 ``drive_changes._sa_access_token`` 换取新 token，并按 55 分钟写入
    该 SA 自己的缓存条目。换 token 失败/返回空值时不写缓存，避免空值污染
    后续请求；无可用 SA 时直接返回 None，不查缓存。
    """
    try:
        drive_changes = _load_drive_changes()
        sa = _pick_sa()
    except Exception as exc:
        logger.warning("drive_auth: 加载 Drive 凭据模块失败，调用方降级处理: %s", exc)
        return None
    if not sa:
        logger.debug("drive_auth: 未发现可用的 Google Drive 服务账号凭据")
        return None
    email = sa.get("client_email")
    if not email:
        logger.debug("drive_auth: SA 缺少 client_email，无法建立分账号缓存")
        return None
    now = time.monotonic()
    # 查缓存 → 换 token → 写缓存 全程持锁：同一 SA 不会并发重复换取
    with _cache_lock:
        cached = _token_cache.get(email)
        if cached and now + _TOKEN_MIN_REMAIN_SECONDS < cached[1]:
            return cached[0]
        try:
            token = drive_changes._sa_access_token(sa)
        except Exception as exc:
            logger.warning(
                "drive_auth: 为 SA %s 换取 Drive Bearer token 失败，调用方降级处理: %s",
                email,
                exc,
            )
            return None
        if not token:
            # 空值不入缓存，下次调用重新尝试换取
            return None
        _token_cache[email] = (token, now + _TOKEN_TTL_SECONDS)
        return token


def drive_auth_headers(url: str) -> dict[str, str]:
    """为 Drive 直链构造请求头；非 Drive 域名或无 token 时返回空字典。"""
    if not is_drive_url(url):
        return {}
    token = get_drive_bearer_token()
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def drive_api_media_url(url: str) -> str:
    """把 Drive uc 直链转换为 Drive API alt=media 下载地址。

    ``https://drive.google.com/uc?export=download&id=<FILE_ID>`` 即使带上 SA 的
    Bearer token 也只会返回登录页（私有文件无法这样下载）；Drive API 的
    ``https://www.googleapis.com/drive/v3/files/<FILE_ID>?alt=media`` 才是
    Bearer token 能用的下载端点，支持 Range 分片。

    非 uc 格式的 URL 原样返回，永不抛异常。
    """
    try:
        if not url:
            return url
        parsed = urlparse(url)
        if (parsed.hostname or "").lower() != "drive.google.com":
            return url
        if parsed.path.rstrip("/") != "/uc":
            return url
        params = dict(parse_qsl(parsed.query))
        file_id = params.get("id")
        if not file_id:
            return url
        return "https://www.googleapis.com/drive/v3/files/" + file_id + "?alt=media"
    except Exception:
        return url
