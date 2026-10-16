"""播放策略能力：中继（relay）/ 对象存储直链（direct）可配。

设计口径（2026-10-16 用户拍板）：只做通用能力、不做死业务逻辑——
策略与直链源全部走 SystemConfig + 管理后台，用哪种由服主决定。
默认 ``relay`` = 现行中继路径，行为零变化；``direct`` 时，仅当条目的
drive_file_id 在服主登记的直链名单（热门副本）内、且至少有一个完整
可用的 S3 兼容直链源（R2 / B2 / Bunny 等）时，才给播放器发 302 签名
直链，字节不经本机；其余情况一律回落中继，能力故障永不打断播放。

实测依据（reports/playback-302-recon-20261016.md）：网盘 uc 直链无鉴权
不可用（登录墙/病毒扫描确认页），302 的合法载体只有自有对象存储的
签名 URL。签名用手写 SigV4 query-string 预签名，不引入 boto3 依赖。

风控红线：本模块对 Google 侧零新增请求（签名是本地计算），不触碰
Drive 请求形态与频率。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote, urlsplit

from sqlalchemy.orm import Session

from backend.integrations import store

logger = logging.getLogger(__name__)

KEY_STRATEGY = "playback_strategy"
KEY_SOURCES = "playback_direct_sources"
KEY_HOTLIST = "playback_direct_hotlist"
KEY_KEY_TEMPLATE = "playback_direct_key_template"
KEY_TTL = "playback_direct_url_ttl_sec"

STRATEGY_RELAY = "relay"
STRATEGY_DIRECT = "direct"
STRATEGIES = (STRATEGY_RELAY, STRATEGY_DIRECT)

DEFAULT_KEY_TEMPLATE = "{file_id}"
DEFAULT_TTL_SEC = 3600
MIN_TTL_SEC = 60
MAX_TTL_SEC = 86400


# ---------------------------------------------------------------------------
# 配置读取
# ---------------------------------------------------------------------------

def get_strategy(db: Session) -> str:
    raw = (store.get_value(db, KEY_STRATEGY, STRATEGY_RELAY) or "").strip().lower()
    return raw if raw in STRATEGIES else STRATEGY_RELAY


def get_sources(db: Session) -> list[dict]:
    """直链源清单（JSON 数组）；配置损坏一律回空，永不抛错打断播放。"""
    raw = store.get_value(db, KEY_SOURCES, "")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("playback_direct_sources 不是合法 JSON，忽略")
        return []
    if not isinstance(data, list):
        return []
    return [s for s in data if isinstance(s, dict)]


def get_hotlist(db: Session) -> set[str]:
    """已在直链源备有副本的 drive_file_id 集合（JSON 数组）。"""
    raw = store.get_value(db, KEY_HOTLIST, "")
    if not raw:
        return set()
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("playback_direct_hotlist 不是合法 JSON，忽略")
        return set()
    if not isinstance(data, list):
        return set()
    return {str(x).strip() for x in data if str(x).strip()}


def get_key_template(db: Session) -> str:
    raw = (store.get_value(db, KEY_KEY_TEMPLATE, DEFAULT_KEY_TEMPLATE) or "").strip()
    return raw or DEFAULT_KEY_TEMPLATE


def get_ttl_sec(db: Session) -> int:
    raw = store.get_value(db, KEY_TTL, str(DEFAULT_TTL_SEC))
    try:
        ttl = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_TTL_SEC
    return max(MIN_TTL_SEC, min(MAX_TTL_SEC, ttl))


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
    """策略为 direct 且该文件在直链名单内时返回签名直链，否则 None（回落中继）。

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
            logger.warning("playback_direct_key_template 模板非法: %r", template)
            return None
        if not object_key or ".." in object_key.split("/"):
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
            logger.info(
                "playback direct 302: source=%s bucket=%s key=%s",
                src.get("name") or "-", src.get("bucket"), object_key,
            )
            return url
        return None
    except Exception:  # noqa: BLE001 - 能力层兜底：异常一律回落中继
        logger.exception("maybe_direct_url 异常，回落中继")
        return None
