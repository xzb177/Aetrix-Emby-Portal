"""播放策略能力：对象存储直链（direct）可配 + Drive 302 直链 + 远程挂载专用中继。

播放口径（2026-10-27 用户改令，直链为主线）：
默认走 Drive 302 直链（drive.usercontent，播放器自拉，字节不经服务器，
含 confirm=t 处理）；Drive SA 中继只作可切换兜底（drive_relay_enabled
开关仍在，不删中继代码、不删 302 代码），仅直链不可用（可用性表）或
客户端显式选 drive_relay 时才走中继。此前“中继为主线、302 为兜底”的
口径（2026-10-23 额度应急）已作废；中继 2026-10-17 删过一次、2026-10-23
为额度应急加回，本模块顶部的“中继已全删”旧注记同样作废。
远程挂载（115 等 mount://）仍走服务端专用中继代理，与 Drive 中继开关
无关（凭据不下发）。

设计口径（2026-10-16 用户拍板）：只做通用能力、不做死业务逻辑——
策略与直链源全部走 SystemConfig + 管理后台，用哪种由服主决定。
``direct`` 时，仅当条目的 drive_file_id 在服主登记的直链名单（热门副本）
内、且至少有一个完整可用的 S3 兼容直链源（R2 / B2 / Bunny 等）时，才给
播放器发 302 签名直链，字节不经本机；Drive 条目默认 302 到谷歌 usercontent
直链（``drive_direct_play_enabled`` 开关）；115 等远程挂载（``mount://``）
走服务端专用中继代理（``streaming.serve_mount_relay_async``），凭据不下发。

中继已按用户要求全删（2026-10-17），只剩远程挂载专用中继；原配额感知
降级（``_quota_blocked`` 调用的 ``streaming.drive_quota_blocked_retry_after``
已随 #958/#959 中继删除）已同步清理；本模块只剩直链/回落两条路。

实测依据（reports/playback-302-recon-20261016.md）：网盘 uc 直链无鉴权
不可用（登录墙/病毒扫描确认页），302 的合法载体只有自有对象存储的
签名 URL。签名用手写 SigV4 query-string 预签名，不引入 boto3 依赖。

风控红线：本模块对 Google 侧零新增请求（签名是本地计算），不触碰
Drive 请求形态与频率。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import threading
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlsplit

from sqlalchemy.orm import Session

from backend.integrations import store

logger = logging.getLogger(__name__)

#: （配置瘦身第 1 批，2026-10-17：以下 5 个死键已从配置面删除——写入端
#: 随 admin_playback.py 删除而消失，读侧恒为默认值。行为与删除前逐字一致：
#: get_strategy 永为 relay、get_sources 永为空、get_hotlist 永为空、
#: get_key_template 永为 DEFAULT_KEY_TEMPLATE、get_ttl_sec 永为 DEFAULT_TTL_SEC。
#: maybe_direct_url 因此恒返回 None（与今天完全一致）。）
#: Drive 文件直链播放（最简模式，2026-10-16 用户拍板学 raisemby）：
#: 开启时条目有 drive_file_id 即 302 到谷歌 usercontent 直链，播放器自拉，
#: 服务器退出字节通道；关闭时行为与旧中继路径完全一致（回滚兜底）。
#: 默认开。
KEY_DRIVE_DIRECT_PLAY = "drive_direct_play_enabled"
#: Drive SA 中继（2026-10-23 额度应急）：开启时中继成为可切换兜底，
#: 默认仍走直链，仅直链不可用（可用性表）或显式选 drive_relay 时走
#: 服务端中继（SA 凭据取流、字节过服务器），不经公开下载池、file_id 不出门。
#: 默认关；与直链开关并存，直链优先（2026-10-27 用户改令直链为主线；
#: 直链开关关时整体回旧路径）。
KEY_DRIVE_RELAY = "drive_relay_enabled"
#: Drive SA 中继取流 failover 总尝试次数（2026-10-29 用户亲令「中转加上
#: 换号能力」）：共享/挂载中继上游回 401/403/429 时失效当前 SA 凭据并
#: 轮换下一个 SA 重试，本值含首次尝试（3 = 首试 + 最多换 2 个 SA）。
#: 404 不换号、原样透传（#1192 定版）。仅 SystemConfig 键（非 env）。
KEY_DRIVE_RELAY_SA_FAILOVER_MAX_ATTEMPTS = \
    "drive_relay_sa_failover_max_attempts"
#: failover 总尝试次数缺省与钳制（防呆：至少试 1 次；上限防配置写飞
#: 后一次播放把整个 SA 池轮一遍）。
DEFAULT_DRIVE_RELAY_SA_FAILOVER_MAX_ATTEMPTS = 3
MIN_DRIVE_RELAY_SA_FAILOVER_MAX_ATTEMPTS = 1
MAX_DRIVE_RELAY_SA_FAILOVER_MAX_ATTEMPTS = 10
#: 用户副本额度后路（2026-10-29 用户亲令）：本人 Drive 副本的公开
#: usercontent 直链被谷歌额度页挡住（probe 判 quota）时，video_stream
#: 先用用户本人绑定令牌走 API 中继取同一份副本（API 额度与公开下载
#: 池不是一个池子）；关掉即回到 quota 现行口径（115 优先 / 409 明示）。
#: 默认开。仅 SystemConfig 键（非 env）。
KEY_USERCOPY_QUOTA_RELAY_FALLBACK = "usercopy_quota_relay_fallback_enabled"
#: IntroDB 自动片头片尾（2026-10-17 用户拍板学 raisemby）：开启时播放信息
#: 组装 Chapters 会把众包库 api.introdb.app 的分段并入手打标记。默认开。
KEY_INTRODB_ENABLED = "introdb_enabled"


def introdb_enabled(db: Session) -> bool:
    """IntroDB 自动片头片尾开关（默认开）。

    读失败 fail-safe 回默认开并记日志——开关读不到时不该悄悄关掉功能。
    """
    try:
        raw = (store.get_value(db, KEY_INTRODB_ENABLED, "true") or "").strip().lower()
    except Exception:  # noqa: BLE001 - 测试桩/异常 DB 场景下保默认
        logger.warning("introdb_enabled 读取失败，回默认开", exc_info=True)
        return True
    return raw not in ("0", "false", "no", "off")

STRATEGY_RELAY = "relay"
STRATEGY_DIRECT = "direct"
STRATEGIES = (STRATEGY_RELAY, STRATEGY_DIRECT)

DEFAULT_KEY_TEMPLATE = "{file_id}"
DEFAULT_TTL_SEC = 3600
MIN_TTL_SEC = 60
MAX_TTL_SEC = 86400

#: 302 直链响应的固定卫生头：签名 URL 短时有效且个人化，绝不能被播放器、
#: 浏览器或中间 CDN 缓存复用（缓存了轻则过期播不了、重则串给别人）；
#: 不把门户域名当 Referer 发给对象存储（防存储端防盗链误杀）。
#: 调用方（api.video_stream）下发 302 时必须带上，不要裸 RedirectResponse。
DIRECT_REDIRECT_HEADERS = {
    "Cache-Control": "no-cache, no-store",
    "Referrer-Policy": "no-referrer",
}


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

def get_strategy(db: Session) -> str:
    """播放策略：恒为 relay（配置瘦身第 1 批：死键删除，行为与删除前一致）。"""
    return STRATEGY_RELAY


def get_sources(db: Session) -> list[dict]:
    """直链源清单：恒为空（配置瘦身第 1 批：死键删除，行为与删除前一致）。"""
    return []


def get_hotlist(db: Session) -> set[str]:
    """已在直链源备有副本的 drive_file_id 集合：恒为空（配置瘦身第 1 批）。"""
    return set()


def get_key_template(db: Session) -> str:
    """播放直链对象键模板：恒为 DEFAULT_KEY_TEMPLATE（配置瘦身第 1 批）。"""
    return DEFAULT_KEY_TEMPLATE


def get_ttl_sec(db: Session) -> int:
    """播放直链签名有效期：恒为 DEFAULT_TTL_SEC（配置瘦身第 1 批）。"""
    return DEFAULT_TTL_SEC


def drive_direct_play_enabled(db: Session) -> bool:
    """Drive 文件直链播放开关（最简模式，默认开）。

    开启时 video_stream 对有 drive_file_id 的条目直接 302 到谷歌
    usercontent 直链；关闭时走原路径（对象存储直链/中继）。读失败
    fail-safe 回默认开并记日志——开关读不到时不该把播放打回旧路。
    """
    try:
        raw = (store.get_value(db, KEY_DRIVE_DIRECT_PLAY, "true") or "").strip().lower()
    except Exception:  # noqa: BLE001 - 测试桩/异常 DB 场景下保默认
        logger.warning("drive_direct_play_enabled 读取失败，回默认开", exc_info=True)
        return True
    return raw not in ("0", "false", "no", "off")


def drive_relay_enabled(db: Session) -> bool:
    """Drive SA 中继开关（默认关，可切换兜底）。

    开启时中继成为可切换兜底：默认路线仍走直链，仅直链在可用性表
    中不可用、或客户端显式选 drive_relay / byos_drive_relay 时走
    服务端中继（serve_drive_relay_async）：SA 凭据取流、字节过服务器，
    既绕开公开下载池的共用额度，也不再把 file_id 下发给客户端。
    读失败 fail-safe 回默认关——中继是兜底路，开关读不到时不贸然改道。
    """
    try:
        raw = (store.get_value(db, KEY_DRIVE_RELAY, "false") or "").strip().lower()
    except Exception:  # noqa: BLE001 - 测试桩/异常 DB 场景下保默认
        logger.warning("drive_relay_enabled 读取失败，回默认关", exc_info=True)
        return False
    return raw not in ("0", "false", "no", "off", "")


def drive_relay_sa_failover_max_attempts(db: Session) -> int:
    """Drive SA 中继取流 failover 总尝试次数（默认 3，钳制 [1, 10]）。

    读法与 drive_relay_enabled 同款（store 热读 + fail-safe）：读失败 /
    非整数一律回默认 3——failover 是中继的抗造兜底，配置读不到时不该
    把重试次数悄悄改没，也不能被非法值放大成无界轮换。
    """
    try:
        raw = (store.get_value(
            db, KEY_DRIVE_RELAY_SA_FAILOVER_MAX_ATTEMPTS,
            str(DEFAULT_DRIVE_RELAY_SA_FAILOVER_MAX_ATTEMPTS)) or "").strip()
        value = int(raw)
    except Exception:  # noqa: BLE001 - 测试桩/异常 DB/非法值场景下保默认
        logger.warning(
            "drive_relay_sa_failover_max_attempts 读取失败，回默认 %d",
            DEFAULT_DRIVE_RELAY_SA_FAILOVER_MAX_ATTEMPTS, exc_info=True)
        return DEFAULT_DRIVE_RELAY_SA_FAILOVER_MAX_ATTEMPTS
    return max(MIN_DRIVE_RELAY_SA_FAILOVER_MAX_ATTEMPTS,
               min(MAX_DRIVE_RELAY_SA_FAILOVER_MAX_ATTEMPTS, value))


def usercopy_quota_relay_fallback_enabled(db: Session) -> bool:
    """用户副本额度后路开关（默认开）。

    读法与 drive_direct_play_enabled 同款（store 热读 + fail-safe）：
    读失败回默认开并记日志——后路是救播放的，开关读不到时不该悄悄
    把它关掉；真关掉的行为也只是回到 quota 现行口径（115 / 409），
    不伤数据。关的口径与同类键一致：0/false/no/off 视为关。
    """
    try:
        raw = (store.get_value(
            db, KEY_USERCOPY_QUOTA_RELAY_FALLBACK, "true") or "").strip().lower()
    except Exception:  # noqa: BLE001 - 测试桩/异常 DB 场景下保默认
        logger.warning(
            "usercopy_quota_relay_fallback_enabled 读取失败，回默认开",
            exc_info=True)
        return True
    return raw not in ("0", "false", "no", "off")
# ---------------------------------------------------------------------------
# 可观测：直链/回落计数（进程内，重启清零；与 prewarm 计数同口径）
# ---------------------------------------------------------------------------

_COUNTERS = {"direct_302": 0, "fallback_relay": 0}
_COUNTERS_LOCK = threading.Lock()


def _bump_counter(name: str) -> None:
    with _COUNTERS_LOCK:
        _COUNTERS[name] = _COUNTERS.get(name, 0) + 1


def strategy_counters() -> dict:
    """策略判定计数快照（direct_302=发过 302 / fallback_relay=未命中直链）。"""
    with _COUNTERS_LOCK:
        return dict(_COUNTERS)


def strategy_counters_reset() -> None:
    """计数清零（单元测试隔离用）。"""
    with _COUNTERS_LOCK:
        for k in _COUNTERS:
            _COUNTERS[k] = 0


# ---------------------------------------------------------------------------
# S3 SigV4 预签名（query-string 形态，GET 专用）
# ---------------------------------------------------------------------------

def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _encode_path(path: str) -> str:
    """逐段百分号编码路径（不编码 '/' 本身）。"""
    return "/".join(quote(seg, safe="-_.~") for seg in path.split("/"))


def presign_s3_get(
    *,
    endpoint: str,
    bucket: str,
    key: str,
    region: str,
    access_key_id: str,
    secret_key: str,
    expires_sec: int,
    path_style: bool = True,
    now: Optional[datetime] = None,
) -> str:
    """生成 S3 兼容 GET 预签名 URL（R2/B2/S3 通用）。

    纯本地计算、无网络请求。``now`` 仅供测试注入。
    """
    parts = urlsplit(endpoint)
    scheme = parts.scheme or "https"
    endpoint_host = parts.netloc
    if path_style:
        host = endpoint_host
        raw_path = f"/{bucket}/{key}"
    else:
        host = f"{bucket}.{endpoint_host}"
        raw_path = f"/{key}"
    canonical_uri = _encode_path(raw_path)

    now = now or datetime.now(timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    scope = f"{datestamp}/{region}/s3/aws4_request"

    query_params = [
        ("X-Amz-Algorithm", "AWS4-HMAC-SHA256"),
        ("X-Amz-Credential", f"{access_key_id}/{scope}"),
        ("X-Amz-Date", amzdate),
        ("X-Amz-Expires", str(int(expires_sec))),
        ("X-Amz-SignedHeaders", "host"),
    ]
    canonical_qs = "&".join(
        f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}"
        for k, v in sorted(query_params)
    )
    canonical_request = "\n".join([
        "GET",
        canonical_uri,
        canonical_qs,
        f"host:{host}",
        "",
        "host",
        "UNSIGNED-PAYLOAD",
    ])
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256",
        amzdate,
        scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    k_date = _hmac_sha256(("AWS4" + secret_key).encode("utf-8"), datestamp)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, "s3")
    k_signing = _hmac_sha256(k_service, "aws4_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"{scheme}://{host}{canonical_uri}?{canonical_qs}&X-Amz-Signature={signature}"


# ---------------------------------------------------------------------------
# 策略判定：给 video_stream 用的唯一入口
# ---------------------------------------------------------------------------

def _source_ready(src: dict) -> bool:
    if src.get("enabled") is False:
        return False
    for field in ("endpoint", "bucket", "region", "access_key_id", "secret_key"):
        if not str(src.get(field) or "").strip():
            return False
    endpoint = str(src.get("endpoint") or "").strip()
    return endpoint.startswith("https://") or endpoint.startswith("http://")


def maybe_direct_url(db: Session, drive_file_id: Optional[str]) -> Optional[str]:
    """策略为 direct 且该文件在直链名单内时返回签名直链，否则 None。

    任何配置缺失/损坏/签名异常都返回 None——能力故障永不打断播放。
    """
    try:
        if get_strategy(db) != STRATEGY_DIRECT:
            return None
        fid = (drive_file_id or "").strip()
        if not fid:
            return None
        if fid not in get_hotlist(db):
            return None
        template = get_key_template(db)
        try:
            object_key = template.format(file_id=fid)
        except (KeyError, IndexError, ValueError):
            logger.warning("直链对象键模板非法: %r", template)
            _bump_counter("fallback_relay")
            return None
        if not object_key or ".." in object_key.split("/"):
            _bump_counter("fallback_relay")
            return None
        ttl = get_ttl_sec(db)
        for src in get_sources(db):
            if not _source_ready(src):
                continue
            url = presign_s3_get(
                endpoint=str(src["endpoint"]).strip(),
                bucket=str(src["bucket"]).strip(),
                key=object_key,
                region=str(src["region"]).strip(),
                access_key_id=str(src["access_key_id"]).strip(),
                secret_key=str(src["secret_key"]).strip(),
                expires_sec=ttl,
                path_style=bool(src.get("path_style", True)),
            )
            _bump_counter("direct_302")
            logger.info(
                "playback direct 302: source=%s bucket=%s key=%s",
                src.get("name") or "-", src.get("bucket"), object_key,
            )
            return url
        _bump_counter("fallback_relay")
        logger.info("playback direct: 无可用直链源 file_id=%s，回落中继", fid)
        return None
    except Exception:  # noqa: BLE001 - 能力层兜底：异常一律回落中继
        _bump_counter("fallback_relay")
        logger.exception("maybe_direct_url 异常，回落中继")
        return None
