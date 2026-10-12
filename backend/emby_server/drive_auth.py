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
- 401 失效机制：播放 relay 层（streaming.py 的 serve_remote / serve_remote_async）
  在 Drive 源站返回 401 时，经 note_drive_unauthorized 按 token 值失效该坏 token，
  drive_auth 外层缓存与 drive_changes 内层缓存会被同时清除，下次请求重新换取新 token。
  按值而非按 email 清除，避免误删并发场景下刚重换出来的新 token。
"""

from __future__ import annotations

import hashlib
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
    "invalidate_drive_bearer_token",
    "note_drive_unauthorized",
    "premint_tokens",
    "sa_count",
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

# P2-1 修复（审查）：_all_sa_dicts 的结果模块级缓存。
# SA 文件在生产环境静态不变，缓存语义与 drive_changes._discover_sa_files
# 已有的"发现一次、永不失效"一致。_pick_sa_affine 在每次 /stream（get_drive_bearer_token
# 带 key）都调 _all_sa_dicts()：不缓存时每次 open+json.loads 全部 100 个
# SA 文件（含 private_key 大字段），实测 ~30ms/次，占起播优化收益的 5-10%。
# 线程安全：预铸（premint_threads）与请求线程可能并发首调，用 _sa_dicts_lock
# 双重检查锁保护；缓存建成后以不可变 tuple 快照发布，读路径无锁（CPython
# 全局变量赋值原子）。缓存不可变的语义：每次返回一份新的 list 拷贝，
# 防止调用方篡改污染快照。
_sa_dicts_lock = threading.Lock()
_sa_dicts_cache: tuple[dict[str, Any], ...] | None = None
# P1 修复（审查）：token 缓存从"全局单条"改为"按 SA 分账号"。
# key = SA 的 client_email（稳定唯一标识），value = (token, expire_at)。
# expire_at 以 time.monotonic() 为基准。此前全局缓存 55 分钟，
# 导致 100 个 SA 的轮询形同虚设、流量全压在单个 SA 上把配额干爆（probe 403）。
_token_cache: dict[str, tuple[str, float]] = {}

# SA 轮换：复用 drive_changes._sa_rotation_start（带随机抖动的统一实现），
# 本模块不再维护独立计数器（横切能力只许一套）。


def _reset_state() -> None:
    """清空按 SA 分账号的 token 缓存与 SA 字典列表缓存。

    测试/调试用的显式重置点（生产路径不会调用）。
    SA 轮换计数器已收敛到 drive_changes._sa_rotation_start，抖动本身
    不需要确定性复位，故这里不再复位轮询起点。
    """
    global _sa_dicts_cache
    with _cache_lock:
        _token_cache.clear()
    with _sa_dicts_lock:
        _sa_dicts_cache = None


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
        if not sa_files:
            return None
        # 轮换起点：经 drive_changes 的统一实现取得（带随机抖动，反风控）。
        # 放进 try：懒加载模块上的动态属性调用若缺失会抛 AttributeError，
        # 本模块承诺永不抛异常（调用方依赖该契约做降级），此时返回 None。
        start = drive_changes._sa_rotation_start(len(sa_files))
    except Exception:
        return None
    for offset in range(len(sa_files)):
        entry = sa_files[(start + offset) % len(sa_files)]
        try:
            sa = entry if _is_sa_dict(entry) else _read_sa_file(entry)
        except Exception:
            continue
        if _is_sa_dict(sa):
            return sa
    return None


# 亲和窗口（秒）：同一 key 在该窗口内经 hash 固定到同一 SA；到期后
# bucket 递增，hash 输入变化，自动落到另一个 SA，无需状态机/定时器。
_SA_AFFINITY_WINDOW_SECONDS = 100.0


def _pick_sa_affine(key: str) -> dict[str, Any] | None:
    """按时间分桶的确定性 hash 亲和选 SA（P1 起播加速，2026-10-12）。

    同一 ``key``（file_id）在同一 100s bucket 内永远算出同一 SA → 该 SA 的
    3300s token 缓存必命中；bucket 递增后 hash 输入变化 → 落到另一个 SA
    （伪随机，无规律可循，防风控：不固定 hammer 某一个 SA）。

    任何异常（无 SA / hash 失败）都回退到 ``_pick_sa()`` 抖动轮换；永不抛异常。
    """
    try:
        sas = _all_sa_dicts()
        if not sas:
            return _pick_sa()
        bucket = int(time.monotonic() // _SA_AFFINITY_WINDOW_SECONDS)
        digest = hashlib.sha256(f"{key}:{bucket}".encode("utf-8")).digest()
        idx = int.from_bytes(digest, "big") % len(sas)
        sa = sas[idx]
        if _is_sa_dict(sa):
            return sa
    except Exception:
        pass
    return _pick_sa()


def _drive_file_id_from_url(url: str) -> str | None:
    """从 Drive URL 提取 file_id（供 P1 SA 亲和），提取失败返回 None。

    支持两种形式：
    - ``https://drive.google.com/uc?export=download&id=<FILE_ID>``（query id 参数）
    - ``https://www.googleapis.com/drive/v3/files/<FILE_ID>?alt=media``（path 第 4 段）

    与 ``streaming._drive_file_id`` 是同逻辑（横切只许一套：后续重构应统一，
    本次不扩大改动面）。永不抛异常。
    """
    try:
        if not url:
            return None
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host == "drive.google.com" and parsed.path.rstrip("/") == "/uc":
            return dict(parse_qsl(parsed.query)).get("id") or None
        if host == "www.googleapis.com":
            parts = parsed.path.strip("/").split("/")
            # drive / v3 / files / <FILE_ID>
            if len(parts) >= 4 and parts[0] == "drive" and parts[1] == "v3" \
                    and parts[2] == "files":
                return parts[3] or None
    except Exception:
        return None
    return None


def get_drive_bearer_token(key: str | None = None) -> str | None:
    """获取 Drive 访问用的 Bearer access_token；失败返回 None，永不抛异常。

    每次调用都先经 ``_pick_sa()`` round-robin 选出一个 SA，再以该 SA 的
    ``client_email`` 为 key 查进程内分账号缓存：剩余有效期大于 120 秒直接返回；
    否则经 ``drive_changes._sa_access_token`` 换取新 token，并按 55 分钟写入
    该 SA 自己的缓存条目。换 token 失败/返回空值时不写缓存，避免空值污染
    后续请求；无可用 SA 时直接返回 None，不查缓存。

    关键不变式：外层按 ``_TOKEN_TTL_SECONDS``（3300 秒）缓存，故向内层索取
    token 时要求 ``min_remaining = _TOKEN_TTL_SECONDS + _TOKEN_MIN_REMAIN_SECONDS``
    （3420 秒）——内层只会返回真实剩余寿命 ≥3420 秒的 token（不够就重新换取），
    因此外层写入的 3300 秒缓存永远不会超出 token 的真实寿命。内层的剩余寿命
    比较全部在其内部用 wall clock（``time.time()``）完成，不做跨时间基准换算。

    ``key``（P1，2026-10-12）：可选的亲和键（通常为 file_id）。非空时按
    ``sha256(key:bucket) % n`` 时间分桶亲和选 SA——100s 窗口内同一 key 固定
    SA、窗口到期自动换 SA；``None`` 时保持原有的抖动轮换行为，不变。
    """
    try:
        drive_changes = _load_drive_changes()
        sa = _pick_sa_affine(key) if key else _pick_sa()
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
            # 要求内层返回剩余寿命 ≥ 外层缓存时长 + 余量，保证外层缓存不超真实寿命
            token = drive_changes._sa_access_token(
                sa, min_remaining=_TOKEN_TTL_SECONDS + _TOKEN_MIN_REMAIN_SECONDS)
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
    """为 Drive 直链构造请求头；非 Drive 域名或无 token 时返回空字典。

    P1（2026-10-12）：内部从 url 提取 file_id 作为亲和 key 传给
    get_drive_bearer_token——调用方（mounts._strm_play_target）零改动。
    """
    if not is_drive_url(url):
        return {}
    token = get_drive_bearer_token(key=_drive_file_id_from_url(url))
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def invalidate_drive_bearer_token(token: str) -> bool:
    """按 token 值精确失效 Drive Bearer token 缓存（内外两层同时清除）。

    401 毒化修复：Drive 源站返回 401 说明该 token 已失效，但外层缓存
    ``_token_cache``（key=client_email）与 ``drive_changes`` 模块内部的同名
    缓存都会把坏 token 原样吐回，最长毒化 55 分钟。这里按 **token 值** 匹配
    清除外层所有值相等的条目，并对涉及的 client_email 顺带清掉
    ``drive_changes._token_cache`` 的同 key 条目，使下次请求重新换取新 token。

    只按值删、不按 email 删：并发场景下同一 SA 可能已被其他线程换成新 token，
    按 email 删会误删刚换到的新 token，造成无谓的重复换取。

    Args:
        token: 待失效的 access_token 原始值（不含 "Bearer " 前缀）。

    Returns:
        是否至少清除了一条外层缓存条目；token 为空或任何异常均返回 False。
    """
    if not token:
        return False
    try:
        with _cache_lock:
            emails = [
                email
                for email, (cached_token, _expire_at) in _token_cache.items()
                if cached_token == token
            ]
            if not emails:
                return False
            for email in emails:
                _token_cache.pop(email, None)
            # best-effort 清除 drive_changes 内层缓存：内层 key 同样是 client_email，
            # 不清内层会把坏 token 原样吐回（实测只清外层无效）。
            try:
                drive_changes = _load_drive_changes()
                inner_cache = getattr(drive_changes, "_token_cache", None)
                if isinstance(inner_cache, dict):
                    for email in emails:
                        inner_cache.pop(email, None)
            except Exception:
                # 拿不到模块 / 无该属性 / 内层实现变更：静默跳过，外层已失效
                pass
            logger.info(
                "drive_auth: 已按值失效 %d 个 SA 的 Drive Bearer token 缓存（401 毒化修复）",
                len(emails),
            )
            return True
    except Exception as exc:
        logger.warning("drive_auth: 按值失效 Drive Bearer token 缓存失败: %s", exc)
        return False


def note_drive_unauthorized(url: str, auth_header: str) -> bool:
    """记录一次 Drive 源站 401 未授权，并按 token 值失效缓存。

    供播放 relay 层（streaming.py 的 serve_remote / serve_remote_async）
    在源站返回 401 时调用。

    参数:
        url: 触发 401 的请求 URL，仅当指向 Drive 相关域名时才处理。
        auth_header: 形如 ``Bearer <token>`` 的 Authorization 头，
            scheme 大小写不敏感，允许前后空白，token 部分 strip 后须非空。

    返回:
        True 表示成功失效了对应 token 的缓存（内外两层），
        False 表示 url 非 Drive、header 格式不合法或未命中缓存。

    本函数永不抛异常。
    """
    try:
        if not is_drive_url(url):
            return False

        if not isinstance(auth_header, str):
            return False

        parts = auth_header.strip().split(None, 1)
        if len(parts) != 2:
            return False

        scheme, token = parts[0], parts[1].strip()
        if scheme.lower() != "bearer" or not token:
            return False

        return invalidate_drive_bearer_token(token)
    except Exception:
        logger.warning(
            "note_drive_unauthorized failed for url=%r", url, exc_info=True
        )
        return False


def drive_api_media_url(url: str) -> str:
    """把 Drive uc 直链转换为 Drive API alt=media 下载地址.

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


def sa_count() -> int:
    """返回发现的 SA 数量（预铸进度/指标用；异常时返回 0）。"""
    try:
        drive_changes = _load_drive_changes()
        sa_files = drive_changes._discover_sa_files() or []
        return len(sa_files)
    except Exception:
        return 0


def _all_sa_dicts() -> list[dict[str, Any]]:
    """返回所有可用的 SA 字典列表（去重按 client_email）。

    供 premint_tokens 与 _pick_sa_affine（SA 亲和热路径）用：
    前者逐个预铸 token，避免冷启动时第一次播放现换
    （JWT 签名 + 1 次 oauth2 POST，约 300~600ms）；
    后者在每次 /stream 都调用。

    P2-1（2026-10-12 审查）：结果在模块级缓存——SA 文件静态不变，
    每次重读 100 个文件的 open+json.loads（~30ms/次）是纯浪费。
    缓存经 _sa_dicts_lock 双重检查锁建成，不可变 tuple 快照发布；
    失败路径（异常/无 SA）不缓存，每次重试发现（语义与原先一致）。
    """
    global _sa_dicts_cache
    # 快路径：缓存已建成时无锁读取（CPython 全局变量赋值原子）。
    snapshot = _sa_dicts_cache
    if snapshot is not None:
        return list(snapshot)
    with _sa_dicts_lock:
        snapshot = _sa_dicts_cache
        if snapshot is not None:
            return list(snapshot)
        try:
            drive_changes = _load_drive_changes()
            sa_files = drive_changes._discover_sa_files() or []
        except Exception:
            return []
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for entry in sa_files:
            try:
                sa = entry if _is_sa_dict(entry) else _read_sa_file(entry)
            except Exception:
                continue
            if _is_sa_dict(sa):
                email = sa.get("client_email")
                if email and email not in seen:
                    seen.add(email)
                    result.append(sa)
        # 只缓存非空结果：空结果（无 SA/全部读失败）不冻结，每次重试
        # 重新发现——与 _discover_sa_files"无 SA 时每次都重新扫"语义一致。
        if result:
            _sa_dicts_cache = tuple(result)
            snapshot = _sa_dicts_cache
            return list(snapshot)
        return result


def premint_tokens(max_workers: int = 4) -> dict[str, int]:
    """为所有 SA 预铸 access_token 并写入分账号缓存。

    P0-3（2026-10-12）：起播加速。部署后/空闲后第一次播放要现做
    JWT→token 交换（约 300~600ms），预铸后冷启动直接命中缓存。

    - 线程池有界并发（默认 4），每个 SA 独立换 token，互不阻塞。
    - 每个任务前随机抖动 0~0.5 秒，反风控（避免 100 个并发打到 Google）。
    - 单个 SA 失败不影响其他（try/except 包住）。
    - token endpoint 不是 Drive API 配额，零风控风险。
    - 永不抛异常，返回 {"total": N, "ok": M, "failed": K}。

    注意：换 token 是 CPU + 网络混合操作（RSA 签名 + 1 次 POST），
    用线程池而非 asyncio（drive_changes._sa_access_token 是同步函数）。
    """
    import random
    from concurrent.futures import ThreadPoolExecutor

    result = {"total": 0, "ok": 0, "failed": 0}
    try:
        sas = _all_sa_dicts()
        result["total"] = len(sas)
        if not sas:
            return result

        def _mint_one(sa: dict[str, Any]) -> bool:
            try:
                # 抖动：打散 100 个 SA 的换 token 请求
                time.sleep(random.uniform(0, 0.5))
                email = sa.get("client_email")
                if not email:
                    return False
                # 先查缓存：还有效就跳过（premint 是幂等的）
                now = time.monotonic()
                with _cache_lock:
                    cached = _token_cache.get(email)
                    if cached and now + _TOKEN_MIN_REMAIN_SECONDS < cached[1]:
                        return True
                drive_changes = _load_drive_changes()
                token = drive_changes._sa_access_token(
                    sa, min_remaining=_TOKEN_TTL_SECONDS + _TOKEN_MIN_REMAIN_SECONDS)
                if not token:
                    return False
                with _cache_lock:
                    # P3-4：持锁写回前复查——启动瞬间可能与 get_drive_bearer_token
                    # 并发换同一 SA 的 token（_mint_one 是"持锁查→释放→线程池换→
                    # 持锁写"，查-换-写非原子）。若该 SA 已被他人写入且仍有效，
                    # 丢弃本次结果，消除重复换 token。
                    cached = _token_cache.get(email)
                    if cached and time.monotonic() + _TOKEN_MIN_REMAIN_SECONDS < cached[1]:
                        return True
                    _token_cache[email] = (token, time.monotonic() + _TOKEN_TTL_SECONDS)
                return True
            except Exception:
                return False

        workers = max(1, min(16, int(max_workers)))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="sa-premint") as pool:
            for ok in pool.map(_mint_one, sas):
                if ok:
                    result["ok"] += 1
                else:
                    result["failed"] += 1
    except Exception as exc:
        logger.warning("drive_auth: SA token 预铸失败（不影响启动）: %s", exc)
    logger.info("drive_auth: SA token 预铸完成 total=%d ok=%d failed=%d",
                result["total"], result["ok"], result["failed"])
    return result
