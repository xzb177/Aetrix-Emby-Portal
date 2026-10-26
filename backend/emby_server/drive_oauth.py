"""BYOS P2：Drive OAuth 全套 + 用户凭据维护（清单第 9 项）。

只做 P2 的三件事，多一件不做：
1. 凭据分离：Drive 用户凭据只走 mount_credentials（scope=user，
   密封 + key_id 信封，与 P1 的 115 绑定同款拼法：绑定行引用凭据
   行，不另起第三套）。取用户 Drive 令牌的唯一口是
   :func:`get_user_drive_access_token`——它只读 scope=user 且
   owner_user_id=本人的凭据行，**绝不**回落 SA 池、rclone.conf、
   scope=source 行；回落会查错盘、跨用户。
2. Drive OAuth 全套：授权接入（PKCE S256 + access_type=offline +
   prompt=consent，保证拿到 refresh_token）→ 回调换 token → refresh
   续期 → 状态机（复用 P1 绑定状态机 active/invalid/expired 与
   失效三档文案）。绑完先验活（Drive about）才置 active；失效分
   凭据错 / 过期 / 风控三档，不静默回落。
3. 凭据维护工人：后台常驻刷新 / 过期标记 / 节流退避（照抄 FMBY
   凭据维护工人口径：启动即跑一轮 + 周期跑 + 单行失败隔离 +
   汇总日志），开关门控、默认关；失败只记警告、不挡播放与扫描。

不做：用户令牌中继播放接线（P2 的取令牌口已留好，video_stream
选路改动是后续项，本 PR 不动选路）、按绑定扫描入库（P3）、
115 绑定逻辑重写（只扩展 create/verify 的 drive 分支文案与恢复口）。

凭据纪律：本模块是唯一经手 Drive 用户令牌明文的地方（换 token、
刷新、验活时解密封后立即用、用完不存变量外泄）；日志与返回值
永远不出令牌原文、不出密封值、不出 code_verifier（只出
has_credential 布尔值与状态）。OAuth state 行是单次有效、10 分钟
过期；Google 授权码本身也是一次性的——换 token 失败无论哪档，
state 行一律删除，失败重试必须重新走授权（不存在“用旧 state
再换一次”）。

服务器级 OAuth 应用：client_id / client_secret 是部署者在 Google
Cloud 注册的应用凭据，走文件配置（env），不进库、不按用户存；
回调地址 = EMBY_PUBLIC_URL + 固定路径，部署者须在 Google Cloud
把该地址注册为授权重定向 URI；EMBY_PUBLIC_URL 未配时 start 口
响亮拒绝，不猜 localhost（localhost 回调对真实用户不可用）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server import user_bindings as ub
from backend.emby_server.drive_changes import (
    OAUTH_TOKEN_REFRESH_MARGIN_SEC,
    TOKEN_URL,
)
from backend.emby_server.env_util import env_int
from backend.emby_server.public_url import configured_public_url
from backend.emby_server.upstream_sources import (
    SealedSecretError,
    seal_secret,
    unseal_secret,
)

logger = logging.getLogger(__name__)

#: Google OAuth 端点（TOKEN_URL 复用 drive_changes 的唯一常量）。
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
DRIVE_ABOUT_URL = "https://www.googleapis.com/drive/v3/about"
#: 只读 scope：读侧（共享源读取、BYOS 已有文件读取、用户副本读取）
#: 全靠它。写侧（转存目录建立、files.copy 建副本）不走它。
DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
#: 写 scope（Non-sensitive）：只覆盖“应用创建或打开的文件”——
#: 转存目录 aetrix-transfer 与转存副本都是应用创建的，正在此覆盖内。
#: 注意：单 file 不够——读共享源文件与 BYOS 已有文件都不在 file
#: 覆盖里，故授权请求是两档并列（readonly + file），缺一不可；
#: 全盘 drive（Restricted + CASA）只当组合实测失败时的 fallback，
#: 不擅自升级。
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
#: 授权请求实际发出的范围（空格分隔；Google 回的已授范围可能因
#: 用户在同意屏上逐项取消而少于此串，回调以实授为准入库）。
DRIVE_OAUTH_SCOPES = (DRIVE_READONLY_SCOPE, DRIVE_FILE_SCOPE)
DRIVE_OAUTH_SCOPE = " ".join(DRIVE_OAUTH_SCOPES)
#: 回调路径（挂在 portal 的 user_emby_router 下，前缀固定）。
OAUTH_CALLBACK_PATH = "/api/user/emby/storage-bindings/drive/oauth/callback"

#: 服务器级 OAuth 应用凭据（部署者文件配置，不进库）。
CLIENT_ID_ENV = "DRIVE_OAUTH_CLIENT_ID"
CLIENT_SECRET_ENV = "DRIVE_OAUTH_CLIENT_SECRET"
#: 维护工人开关与周期（默认关；周期是我方起步值，非 FMBY 常量）。
MAINT_ENABLED_ENV = "BYOS_DRIVE_CRED_MAINT_ENABLED"
MAINT_INTERVAL_ENV = "BYOS_DRIVE_CRED_MAINT_INTERVAL_SEC"
MAINT_INTERVAL_DEFAULT = 3600
#: 授权 state 有效期（秒；我方起步值）。
STATE_TTL_ENV = "DRIVE_OAUTH_STATE_TTL_SEC"
STATE_TTL_DEFAULT = 600


class DriveOAuthError(ub.BindingError):
    """Drive OAuth 失败（复用 BindingError 的 kind 体系，调用方同口径
    定状态码与文案；kind 取值与 user_bindings 一致）。"""


def _client_id() -> str:
    return (os.getenv(CLIENT_ID_ENV, "") or "").strip()


def _client_secret() -> str:
    return (os.getenv(CLIENT_SECRET_ENV, "") or "").strip()


def redirect_uri() -> str:
    """OAuth 回调地址；EMBY_PUBLIC_URL 未配时返回空串（调用方响亮拒绝）。"""
    base = configured_public_url()
    if not base:
        return ""
    return base + OAUTH_CALLBACK_PATH


def maint_enabled() -> bool:
    return env_int(MAINT_ENABLED_ENV, 0, 0, 1) == 1


def maint_interval_sec() -> int:
    return env_int(MAINT_INTERVAL_ENV, MAINT_INTERVAL_DEFAULT, lo=60)


def state_ttl_sec() -> int:
    return env_int(STATE_TTL_ENV, STATE_TTL_DEFAULT, lo=60)


# ---------------------------------------------------------------------------
# 令牌 JSON 编解码（密封值里存的就是这份 JSON，不存明文列）
# ---------------------------------------------------------------------------

def encode_drive_token(access_token: str, refresh_token: str,
                       expires_at: int, scope: str | None = None) -> str:
    """密封令牌 JSON：scope 存实授范围（Google token 响应原样回的
    空格分隔串；刷新时沿用旧存值，不许用请求串覆盖——刷新响应
    未必带 scope，拿请求串覆盖会把“用户只授了只读”谎报成已授
    写权限，needs_rebind 判定就全错）。None = 未知（老绑定），
    判定上一律按需重绑处理。"""
    return json.dumps({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": int(expires_at),
        "token_type": "Bearer",
        "scope": scope,
    })


def decode_drive_token(text: str) -> dict:
    try:
        data = json.loads(text or "")
    except Exception as exc:
        raise DriveOAuthError(
            "auth_error", "Drive 凭据格式损坏，请重新绑定。") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise DriveOAuthError(
            "auth_error", "Drive 凭据缺少 access_token，请重新绑定。")
    return data


# ---------------------------------------------------------------------------
# HTTP 失败分类（token 端点 / Drive API 共用；一过性失败返回 None）
# ---------------------------------------------------------------------------

def _classify_http_failure(status: int | None, body) -> str | None:
    """HTTP 失败 → 三档 kind；一过性失败（5xx / 网络错）返回 None。

    - invalid_grant（refresh_token 被用户撤销或过期）→ expired；
    - invalid_client / unauthorized_client（服务器应用凭据错）→
      auth_error（部署者问题，用户重绑也修不好，但绑定确实不可用）；
    - 429 / rateLimitExceeded / userRateLimitExceeded → throttled；
    - 401（about 验活时令牌被拒）→ expired（调用方先刷新再定）；
    - 其余 403 → auth_error；5xx / 无状态码 → None（一过性）。
    """
    text = ""
    error_code = ""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            error_code = str(err.get("message", "") or "")
            text = str(err.get("message", "") or "")
            for detail in err.get("errors", []) or []:
                if isinstance(detail, dict):
                    text += " " + str(detail.get("reason", "") or "")
        else:
            error_code = str(err or "")
            text = error_code
    elif isinstance(body, str):
        text = body
    lowered = text.lower()
    if error_code == "invalid_grant" or "invalid_grant" in lowered:
        return "expired"
    if status == 429 or "ratelimitexceeded" in lowered.replace(" ", ""):
        return "throttled"
    if error_code in ("invalid_client", "unauthorized_client"):
        return "auth_error"
    if status == 401:
        return "expired"
    if status == 403:
        return "auth_error"
    return None


class _HttpResult:
    """httpx 响应的最小表面（测试桩只要实现这三个属性/方法）。"""

    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, dict):
            return self._payload
        raise ValueError("not json")

    @property
    def text(self) -> str:
        return self._payload if isinstance(self._payload, str) else ""


def _default_http_post(url, data=None, timeout=30):
    import httpx

    return httpx.post(url, data=data, timeout=timeout)


def _default_http_get(url, headers=None, timeout=30):
    import httpx

    return httpx.get(url, headers=headers, timeout=timeout)


def _read_error_body(resp) -> object:
    try:
        return resp.json()
    except Exception:
        try:
            return resp.text or ""
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# OAuth：授权 URL、换 token、刷新、验活
# ---------------------------------------------------------------------------

def build_authorization_url(redirect_uri_value: str, state: str,
                            code_challenge: str) -> str:
    """Google 授权地址（access_type=offline + prompt=consent 是拿
    refresh_token 的必要条件，缺了 refresh_token 的绑定无法维护，
    完成时直接判 auth_error，不存半截行）。"""
    from urllib.parse import urlencode

    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri_value,
        "response_type": "code",
        "scope": DRIVE_OAUTH_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return AUTH_URL + "?" + urlencode(params)


def exchange_code_for_tokens(code: str, redirect_uri_value: str,
                             code_verifier: str,
                             http_post=None) -> dict:
    """授权码换 token（单次调用；失败按三档分类，网络/5xx 抛 transient）。"""
    post = http_post or _default_http_post
    try:
        resp = post(TOKEN_URL, data={
            "code": code,
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "redirect_uri": redirect_uri_value,
            "grant_type": "authorization_code",
            "code_verifier": code_verifier,
        }, timeout=30)
    except Exception as exc:
        raise DriveOAuthError(
            "transient", f"换 token 时网络失败（可重试）：{exc}") from exc
    if resp.status_code != 200:
        kind = _classify_http_failure(resp.status_code,
                                      _read_error_body(resp))
        if kind is None:
            raise DriveOAuthError(
                "transient", f"换 token 时上游失败（可重试）：HTTP {resp.status_code}")
        raise DriveOAuthError(kind, ub.error_message(kind))
    try:
        body = resp.json()
    except Exception as exc:
        raise DriveOAuthError(
            "transient", "换 token 返回非 JSON（可重试）。") from exc
    access = (body.get("access_token") or "").strip()
    if not access:
        raise DriveOAuthError("auth_error", "换 token 未返回 access_token。")
    refresh = (body.get("refresh_token") or "").strip()
    if not refresh:
        # 没拿到 refresh_token 的绑定无法维护（过期即死），不成行。
        raise DriveOAuthError(
            "auth_error", "Google 未返回 refresh_token（须离线访问授权），请重新授权。")
    expires_in = int(body.get("expires_in", 3600) or 3600)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "expires_at": int(time.time()) + expires_in,
        # Google token 响应原样回的已授范围（空格分隔）；用户在同意屏
        # 上逐项取消时可能少于请求串，回调以此为准入库，不许拿请求串冒充。
        "scope": (body.get("scope") or "").strip() or None,
    }


def refresh_access_token(refresh_token: str,
                         http_post=None) -> dict:
    """refresh_token 换新 access_token（Google 可能轮换 refresh_token，
    回了新的就用新的，没回就沿用旧的；失败分类与换 token 同口径）。"""
    post = http_post or _default_http_post
    try:
        resp = post(TOKEN_URL, data={
            "grant_type": "refresh_token",
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "refresh_token": refresh_token,
        }, timeout=30)
    except Exception as exc:
        raise DriveOAuthError(
            "transient", f"刷新令牌时网络失败（可重试）：{exc}") from exc
    if resp.status_code != 200:
        kind = _classify_http_failure(resp.status_code,
                                      _read_error_body(resp))
        if kind is None:
            raise DriveOAuthError(
                "transient", f"刷新令牌时上游失败（可重试）：HTTP {resp.status_code}")
        raise DriveOAuthError(kind, ub.error_message(kind))
    try:
        body = resp.json()
    except Exception as exc:
        raise DriveOAuthError(
            "transient", "刷新令牌返回非 JSON（可重试）。") from exc
    access = (body.get("access_token") or "").strip()
    if not access:
        raise DriveOAuthError("auth_error", "刷新令牌未返回 access_token。")
    new_refresh = (body.get("refresh_token") or "").strip() or refresh_token
    expires_in = int(body.get("expires_in", 3600) or 3600)
    return {
        "access_token": access,
        "refresh_token": new_refresh,
        "expires_at": int(time.time()) + expires_in,
    }


def check_drive_liveness(access_token: str, http_get=None) -> dict:
    """Drive 验活：about 接口（只读 user 段，证明令牌真能读盘）。

    401 → expired（调用方先刷新再定）；403 限流 → throttled；
    其余 403 → auth_error；5xx / 网络错 → transient（不落状态）。
    返回 about 的 user 段（仅证明用，不入库）。
    """
    get = http_get or _default_http_get
    try:
        resp = get(DRIVE_ABOUT_URL + "?fields=user",
                   headers={"Authorization": f"Bearer {access_token}"},
                   timeout=30)
    except Exception as exc:
        raise DriveOAuthError(
            "transient", f"验活时网络失败（可重试）：{exc}") from exc
    if resp.status_code != 200:
        kind = _classify_http_failure(resp.status_code,
                                      _read_error_body(resp))
        if kind is None:
            raise DriveOAuthError(
                "transient", f"验活时上游失败（可重试）：HTTP {resp.status_code}")
        raise DriveOAuthError(kind, ub.error_message(kind))
    try:
        body = resp.json()
    except Exception as exc:
        raise DriveOAuthError("transient", "验活返回非 JSON（可重试）。") from exc
    return body.get("user", {}) if isinstance(body, dict) else {}


# ---------------------------------------------------------------------------
# 授权 state 行（单次有效、TTL 内有效；行里只存哈希与密封 verifier）
# ---------------------------------------------------------------------------

def _hash_state(state: str) -> str:
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("utf-8")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


#: 转存工人建目录失败的 error_detail 前缀（transfer_variant.py
#: 建目录 403 时原样记入；重置判定只认这个前缀，不认别的失败）。
MKDIR_FAILED_PREFIX = "建转存目录失败"


def granted_scope_set(granted_scopes: str | None) -> set:
    """已授范围串（空格分隔）→ 集合；None/空串 → 空集。"""
    return set((granted_scopes or "").split()) - {""}


def binding_needs_rebind(granted_scopes: str | None) -> bool:
    """Drive 绑定是否需重绑：已授范围里缺 drive.file 即需重绑。
    老绑定无记录（None）一律按需重绑——老代码只申请过只读，
    不可能自带写权限，猜“已授”就是谎报。"""
    return DRIVE_FILE_SCOPE not in granted_scope_set(granted_scopes)


def reset_mkdir_failed_jobs(db: Session, user_id: int) -> int:
    """重绑成功后，把该用户因建转存目录失败（只读令牌写盘 403）
    而 failed 的转存任务打回 pending、attempts 清零、错误与重试
    时间清掉——工人下一轮即按现取的新绑定凭据重跑（工人只捡
    pending，failed 不再处理；门禁对 failed 返 409 终态；24h 节流
    又挡新任务——三重死锁，不重置这单永远播不了）。
    只动 error_detail 以“建转存目录失败”开头的 failed 行：其它
    失败原因（源失效、凭据错等）不在本次重绑的修复范围，不许
    顺手复活。返回重置行数（调用方提交）。"""
    rows = (
        db.query(em.TransferJob)
        .filter(em.TransferJob.owner_scope == "user",
                em.TransferJob.owner_ref == str(user_id),
                em.TransferJob.status == "failed",
                em.TransferJob.error_detail.like(MKDIR_FAILED_PREFIX + "%"))
        .all()
    )
    for job in rows:
        job.status = "pending"
        job.attempts = 0
        job.error_kind = None
        job.error_detail = None
        job.next_retry_at = None
    return len(rows)


#: Google 令牌吊销端点（重绑卫生用，成败不影响重绑结果）。
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


def _revoke_refresh_token_best_effort(refresh_token: str,
                                      http_post=None) -> None:
    """旧 refresh 尽力吊销：成败都不影响重绑结果（旧令牌即使未
    吊销，也会在用户撤销或达到 Google 上限时失效；吊销只是卫生，
    失败不许把已成功的重绑拖垮）。"""
    post = http_post or _default_http_post
    try:
        post(REVOKE_URL, data={"token": refresh_token}, timeout=30)
    except Exception:
        logger.warning("重绑旧 refresh 吊销失败（忽略）", exc_info=True)


def _validate_label_and_quota(db: Session, user_id: int,
                              account_label: str,
                              exclude_binding_id: int | None = None) -> str:
    """P1 同款校验（label 1–100 字、同 provider 重名拒、上限拒），
    供 start 与 complete 两处共用（complete 时重查，防并发抢名）。
    exclude_binding_id：重绑时传目标绑定 id——同名校验跳过自身、
    上限计数不算自身（重绑不新增行，不许因“已达上限”把重绑挡在
    门外）。"""
    label = (account_label or "").strip()
    if not label or len(label) > 100:
        raise DriveOAuthError("validation", "account_label 需为 1–100 字。")
    existing_q = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.user_id == user_id,
                em.UserStorageBinding.provider == ub.PROVIDER_DRIVE,
                em.UserStorageBinding.account_label == label)
    )
    if exclude_binding_id is not None:
        existing_q = existing_q.filter(
            em.UserStorageBinding.id != exclude_binding_id)
    if existing_q.first() is not None:
        raise DriveOAuthError(
            "validation", f"同 provider 下已存在同名绑定「{label}」。")
    count_q = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.user_id == user_id)
    )
    if exclude_binding_id is not None:
        count_q = count_q.filter(
            em.UserStorageBinding.id != exclude_binding_id)
    if count_q.count() >= ub.binding_limit_per_user():
        raise DriveOAuthError(
            "validation",
            f"绑定已达上限（{ub.binding_limit_per_user()} 个），请先解绑不用的账号。")
    return label


def start_drive_oauth(db: Session, user_id: int, account_label: str,
                      redirect_uri_value: str | None = None,
                      rebind_binding_id: int | None = None) -> dict:
    """开始 Drive 授权：建 state 行、返回 Google 授权地址。

    此时不建绑定行与凭据行——绑完先验活，没换到 token 就不成行。
    前置缺失响亮拒绝：服务器应用凭据未配、对外地址未配、label
    非法、重名、超上限，全部 validation（400），不生成 state。
    rebind_binding_id：重绑时传目标绑定 id——目标须是本人名下的
    Drive 绑定行，否则 validation；state 行记下它，回调成功时原地
    换凭据（不新增行、不改 label、不改绑定 id），回调失败时目标行
    落失败态（不另建失败行）。重绑中途放弃：旧绑定原样不动，
    不像“先删后绑”那样丢行。
    """
    if not _client_id() or not _client_secret():
        raise DriveOAuthError(
            "validation",
            "服务器未配置 Drive OAuth 应用凭据（DRIVE_OAUTH_CLIENT_ID/SECRET），"
            "部署者须先在 Google Cloud 注册应用并配好。")
    uri = redirect_uri_value or redirect_uri()
    if not uri:
        raise DriveOAuthError(
            "validation",
            "服务器未配置对外地址（EMBY_PUBLIC_URL），部署者须先配好并在 "
            "Google Cloud 把回调地址注册为授权重定向 URI。")
    rebind_target = None
    if rebind_binding_id is not None:
        rebind_target = (
            db.query(em.UserStorageBinding)
            .filter(em.UserStorageBinding.id == rebind_binding_id,
                    em.UserStorageBinding.user_id == user_id,
                    em.UserStorageBinding.provider == ub.PROVIDER_DRIVE)
            .first()
        )
        if rebind_target is None:
            raise DriveOAuthError(
                "validation", "重绑目标不存在或不属于本人。")
    label = _validate_label_and_quota(db, user_id, account_label,
                                      exclude_binding_id=rebind_binding_id)
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    now = datetime.now()
    row = em.DriveOAuthState(
        state_hash=_hash_state(state), user_id=user_id,
        account_label=label, sealed_verifier=seal_secret(verifier),
        created_at=now, expires_at=now + timedelta(seconds=state_ttl_sec()),
        rebind_binding_id=rebind_binding_id)
    db.add(row)
    db.commit()
    return {
        "authorization_url": build_authorization_url(uri, state, challenge),
        "expires_in": state_ttl_sec(),
    }


def _create_failed_drive_rows(db: Session, user_id: int, label: str,
                              kind: str) -> em.UserStorageBinding:
    """三档失败落行（P1 同款语义：行在、状态是失败态、客户端展示
    重绑引导，不静默、不回落）。"""
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential="", key_id="fernet-v1", status="invalid")
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider=ub.PROVIDER_DRIVE, account_label=label,
        credential_id=cred.id, status="invalid")
    db.add(binding)
    db.flush()
    ub._mark_failed(db, binding, kind)
    db.commit()
    return binding


def complete_drive_oauth(db: Session, state: str, code: str,
                         redirect_uri_value: str | None = None,
                         http_post=None, http_get=None,
                         ) -> em.UserStorageBinding:
    """回调完成：state 单次验讫 → 换 token → 验活 → 建双行。

    - state 查无 / 过期 → not_found（404），不泄露 state 是否存在过；
    - 换 token / 验活属三档内 → 建失败态双行并返回该行（调用方按
      200 + status 展示重绑引导，与 P1 的 201 + status 同口径）；
    - 一过性失败 → 不落行，抛 transient（502），用户须重新走授权
      （Google 授权码本身也是一次性，旧码换不第二次）；
    - 成功 → 双行 active + last_verified_at。
    无论成败，state 行在首次使用后即删（单次有效）。
    重绑（state 行带 rebind_binding_id）：成功时原地换目标行的凭据
    （凭据行 id、绑定行 id、label 全不变，只换密封令牌 + 已授范围
    + active 态），并把该用户建转存目录失败的 failed 任务打回
    pending；旧 refresh_token 尽力吊销（失败不挡重绑）；失败属三档
    内时目标行落失败态（不另建行）；目标行中途已删时响亮拒绝，
    不另建行（防 label 唯一约束冲突）。
    """
    uri = redirect_uri_value or redirect_uri()
    row = (
        db.query(em.DriveOAuthState)
        .filter(em.DriveOAuthState.state_hash == _hash_state(state or ""))
        .first()
    )
    if row is None:
        raise DriveOAuthError("not_found", "授权会话不存在或已使用，请重新授权。")
    user_id, label = row.user_id, row.account_label
    rebind_binding_id = getattr(row, "rebind_binding_id", None)
    try:
        verifier = unseal_secret(row.sealed_verifier or "")
    except SealedSecretError as exc:
        db.delete(row)
        db.commit()
        raise DriveOAuthError(
            "auth_error", "授权会话已失效（密钥轮换），请重新授权。") from exc
    expired = row.expires_at is not None and row.expires_at <= datetime.now()
    db.delete(row)
    db.commit()
    if expired:
        raise DriveOAuthError("not_found", "授权会话已过期，请重新授权。")
    if not uri:
        raise DriveOAuthError(
            "validation", "服务器未配置对外地址（EMBY_PUBLIC_URL）。")
    try:
        label = _validate_label_and_quota(
            db, user_id, label, exclude_binding_id=rebind_binding_id)
    except DriveOAuthError:
        raise
    rebind_target = None
    if rebind_binding_id is not None:
        rebind_target = (
            db.query(em.UserStorageBinding)
            .filter(em.UserStorageBinding.id == rebind_binding_id,
                    em.UserStorageBinding.user_id == user_id,
                    em.UserStorageBinding.provider == ub.PROVIDER_DRIVE)
            .first()
        )
        if rebind_target is None:
            raise DriveOAuthError(
                "validation", "重绑目标已不存在，请重新绑定。")
    try:
        tokens = exchange_code_for_tokens(code, uri, verifier,
                                          http_post=http_post)
    except DriveOAuthError as exc:
        if exc.kind in ("auth_error", "expired", "throttled"):
            if rebind_target is not None:
                ub._mark_failed(db, rebind_target, exc.kind)
                db.commit()
                return rebind_target
            return _create_failed_drive_rows(db, user_id, label, exc.kind)
        raise
    try:
        check_drive_liveness(tokens["access_token"], http_get=http_get)
    except DriveOAuthError as exc:
        if exc.kind in ("auth_error", "expired", "throttled"):
            if rebind_target is not None:
                ub._mark_failed(db, rebind_target, exc.kind)
                db.commit()
                return rebind_target
            return _create_failed_drive_rows(db, user_id, label, exc.kind)
        raise
    granted = tokens.get("scope")
    if rebind_target is not None:
        # 原地换凭据：凭据行 id 与绑定行 id 不变，旧 refresh 尽力吊销。
        # 凭据行只取一次；旧令牌解不开时 old_refresh 留空（不挡重绑），
        # 凭据行本身缺失则响亮失败（用户可删后重绑，不静默另建行）。
        cred = ub._credential_of(db, rebind_target)
        old_refresh = None
        try:
            old_refresh = decode_drive_token(
                unseal_secret(cred.sealed_credential or "")
            ).get("refresh_token")
        except Exception:
            old_refresh = None
        cred.sealed_credential = seal_secret(encode_drive_token(
            tokens["access_token"], tokens["refresh_token"],
            tokens["expires_at"], scope=granted))
        now = datetime.now()
        cred.status = "active"
        cred.last_verified_at = now
        cred.last_error_kind = None
        rebind_target.granted_scopes = granted
        rebind_target.status = "active"
        rebind_target.last_verified_at = now
        rebind_target.last_error_kind = None
        reset_mkdir_failed_jobs(db, user_id)
        db.commit()
        if old_refresh and old_refresh != tokens["refresh_token"]:
            _revoke_refresh_token_best_effort(old_refresh, http_post)
        return rebind_target
    cred = em.MountCredential(
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential=seal_secret(encode_drive_token(
            tokens["access_token"], tokens["refresh_token"],
            tokens["expires_at"], scope=granted)),
        key_id="fernet-v1", status="active")
    db.add(cred)
    db.flush()
    binding = em.UserStorageBinding(
        user_id=user_id, provider=ub.PROVIDER_DRIVE, account_label=label,
        credential_id=cred.id, status="active", granted_scopes=granted)
    db.add(binding)
    db.flush()
    now = datetime.now()
    binding.last_verified_at = now
    binding.last_error_kind = None
    cred.last_verified_at = now
    cred.last_error_kind = None
    db.commit()
    return binding


# ---------------------------------------------------------------------------
# 凭据分离：用户 Drive 令牌唯一口（绝不回落 SA 池 / rclone.conf /
# scope=source 行；回落会查错盘、跨用户）
# ---------------------------------------------------------------------------

def _drive_credential_of(db: Session,
                         binding: em.UserStorageBinding) -> tuple[em.MountCredential, dict]:
    """绑定行 →（凭据行，解开的令牌 JSON）。

    凭据行归属（scope=user 且 owner=本人）不对 = 数据异常，响亮报错
    不 fallback；密封解不开 → auth_error（P1 同款：密钥轮换后旧密文
    解不开，须重绑）。
    """
    cred = ub._credential_of(db, binding)
    try:
        token_text = unseal_secret(cred.sealed_credential or "")
    except SealedSecretError as exc:
        raise DriveOAuthError(
            "auth_error", "Drive 凭据已失效（密钥轮换后旧密文解不开），请重新绑定。") from exc
    if not token_text:
        raise DriveOAuthError("auth_error", "Drive 凭据为空，请重新绑定。")
    return cred, decode_drive_token(token_text)


def _store_refreshed_token(db: Session, binding: em.UserStorageBinding,
                           cred: em.MountCredential, tokens: dict) -> None:
    """刷新结果写回（密封 JSON 全量替换；调用方提交）。
    scope 沿用旧存值：刷新响应未必带 scope，拿请求串覆盖会把
    “用户只授了只读”谎报成已授写权限，needs_rebind 判定就全错；
    旧存值解不开时存 None（未知→需重绑），不猜。"""
    old_scope = None
    try:
        old_scope = decode_drive_token(
            unseal_secret(cred.sealed_credential or "")).get("scope")
    except Exception:
        old_scope = None
    cred.sealed_credential = seal_secret(encode_drive_token(
        tokens["access_token"], tokens["refresh_token"],
        tokens["expires_at"], scope=old_scope))
    now = datetime.now()
    cred.status = "active"
    cred.last_verified_at = now
    cred.last_error_kind = None
    binding.status = "active"
    binding.last_verified_at = now
    binding.last_error_kind = None


def _apply_refresh_failure(db: Session, binding: em.UserStorageBinding,
                           kind: str) -> None:
    """刷新失败落态（调用方提交）：expired / auth_error 落行；
    throttled 不落行——token 端点 429 是上游节流，不是凭据死亡，
    落 invalid 会逼用户重绑，错杀（与 P1 的 115 直链映射有意不同，
    115 throttled 是账号被限、此处是端点被限，原因已写清）。"""
    if kind in ("expired", "auth_error"):
        ub._mark_failed(db, binding, kind)


def get_binding_drive_access_token(db: Session, binding,
                                 http_post=None) -> str:
    """指定绑定行的 Drive 令牌口（BYOS P3 按绑定扫描用）。

    与 get_user_drive_access_token 同一套刷新/落态逻辑，只是不做
    “最早 active 绑定”查找：调用方已按绑定分账选好行，本口绝不
    替调用方换一行——换行就是跨绑定复用凭据。
    """
    cred, token = _drive_credential_of(db, binding)
    expires_at = int(token.get("expires_at") or 0)
    if expires_at > int(time.time()) + OAUTH_TOKEN_REFRESH_MARGIN_SEC:
        return token["access_token"]
    refresh_token = (token.get("refresh_token") or "").strip()
    if not refresh_token:
        _apply_refresh_failure(db, binding, "expired")
        db.commit()
        raise DriveOAuthError("expired", ub.error_message("expired"))
    try:
        new_tokens = refresh_access_token(refresh_token, http_post=http_post)
    except DriveOAuthError as exc:
        if exc.kind in ("expired", "auth_error"):
            _apply_refresh_failure(db, binding, exc.kind)
            db.commit()
        elif exc.kind == "throttled":
            raise DriveOAuthError(
                "transient", "刷新令牌时上游限流（可重试，凭据未改）。") from exc
        raise
    _store_refreshed_token(db, binding, cred, new_tokens)
    db.commit()
    return new_tokens["access_token"]




def get_user_drive_access_token(db: Session, user_id: int,
                                http_post=None) -> str:
    """该用户 Drive 令牌唯一口（播放 / 扫描 / 工人共用，不许各写一套）。

    - 无 active 绑定 → not_found（调用方按“无可用绑定”处理，绝不
      回落共享 SA——回落会把别人的盘或共享盘当成该用户的盘）；
    - 未过期（留 300 秒余量）→ 直接返回 access_token；
    - 快过期 → 刷新后返回新 token 并写回；刷新失败属 expired /
      auth_error → 落行并抛对应 kind；throttled / transient → 不改
      行，抛 transient（调用方重试，凭据本身没死）。
    """
    binding = ub.get_active_binding(db, user_id, ub.PROVIDER_DRIVE)
    if binding is None:
        raise DriveOAuthError("not_found", "当前用户没有可用的 Drive 绑定。")
    return get_binding_drive_access_token(db, binding, http_post=http_post)


def refresh_drive_binding(db: Session, user_id: int, binding_id: int,
                          http_post=None, http_get=None,
                          ) -> em.UserStorageBinding:
    """Drive 绑定重验（失效后恢复口，P1 verify_binding 的 drive 版）。

    本人行才可重验；强制刷新 + 验活双通过才回 active；三档失败落
    行并返回该行；一过性失败不改状态。
    """
    binding = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.id == binding_id,
                em.UserStorageBinding.user_id == user_id)
        .first()
    )
    if binding is None:
        raise DriveOAuthError("not_found", "绑定不存在或不属于当前用户。")
    if binding.provider != ub.PROVIDER_DRIVE:
        raise DriveOAuthError("unsupported", "该 provider 不走 Drive 重验。")
    try:
        cred, token = _drive_credential_of(db, binding)
    except DriveOAuthError as exc:
        # 失败态行（换 token 失败时建的空凭据行）重验：无 token 可刷，
        # 直接按 auth_error 落行并返回该行（与 P1 verify 返回失败行同口径）；
        # transient（凭据行缺失/归属异常）仍抛回调用方。
        if exc.kind == "auth_error":
            _apply_refresh_failure(db, binding, "auth_error")
            db.commit()
            return binding
        raise
    refresh_token = (token.get("refresh_token") or "").strip()
    if not refresh_token:
        _apply_refresh_failure(db, binding, "expired")
        db.commit()
        return binding
    try:
        new_tokens = refresh_access_token(refresh_token, http_post=http_post)
    except DriveOAuthError as exc:
        if exc.kind in ("expired", "auth_error"):
            _apply_refresh_failure(db, binding, exc.kind)
            db.commit()
            return binding
        raise
    try:
        check_drive_liveness(new_tokens["access_token"], http_get=http_get)
    except DriveOAuthError as exc:
        if exc.kind in ("expired", "auth_error"):
            _apply_refresh_failure(db, binding, exc.kind)
            db.commit()
            return binding
        raise
    _store_refreshed_token(db, binding, cred, new_tokens)
    db.commit()
    return binding


# ---------------------------------------------------------------------------
# 凭据维护工人：启动即跑一轮 + 周期跑（FMBY 工人口径）
# ---------------------------------------------------------------------------

def sweep_drive_credentials(db: Session, http_post=None,
                            http_get=None) -> dict:
    """扫一遍全部 Drive 用户绑定，刷新快过期令牌、标记死凭据。

    - 只扫 provider=drive 的绑定行经 credential_id 指到的凭据行；
      scope=source 行（SA 池等服级凭据）永远不进本循环、不读不写；
    - active 行且令牌快过期（300 秒余量内）→ 刷新；已 expired 行
      不再试刷（invalid_grant 刷了也白刷），计 auth_expired；
    - invalid 行跳过（须用户重验/重绑，工人不自动复活）；
    - 单行失败隔离：该行记数、记警告，继续扫下一行；
    - 汇总一行日志（scanned / refreshed / auth_expired / auth_error /
      throttled / transient_errors / skipped），与 FMBY 维护日志同款。
    返回汇总 dict（调用方 / 测试断言用）。
    """
    summary = {
        "scanned": 0, "refreshed": 0, "auth_expired": 0, "auth_error": 0,
        "throttled": 0, "transient_errors": 0, "skipped": 0,
    }
    rows = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.provider == ub.PROVIDER_DRIVE)
        .order_by(em.UserStorageBinding.id.asc())
        .all()
    )
    for binding in rows:
        summary["scanned"] += 1
        cred = (
            db.query(em.MountCredential)
            .filter(em.MountCredential.id == binding.credential_id)
            .first()
        )
        if cred is None:
            summary["skipped"] += 1
            logger.warning("Drive 维护跳过绑定 #%s：凭据行缺失", binding.id)
            continue
        if cred.scope != "user" or cred.owner_user_id != binding.user_id:
            # 源级凭据行绝不由用户维护工人碰（分账铁律），只记警告。
            summary["skipped"] += 1
            logger.warning("Drive 维护跳过绑定 #%s：凭据行非本人 scope=user 行",
                           binding.id)
            continue
        if binding.status == "expired":
            summary["auth_expired"] += 1
            continue
        if binding.status != "active":
            summary["skipped"] += 1
            continue
        try:
            token = decode_drive_token(
                unseal_secret(cred.sealed_credential or ""))
        except (SealedSecretError, DriveOAuthError):
            _apply_refresh_failure(db, binding, "auth_error")
            db.commit()
            summary["auth_error"] += 1
            continue
        expires_at = int(token.get("expires_at") or 0)
        if expires_at > int(time.time()) + OAUTH_TOKEN_REFRESH_MARGIN_SEC:
            summary["skipped"] += 1
            continue
        refresh_token = (token.get("refresh_token") or "").strip()
        if not refresh_token:
            _apply_refresh_failure(db, binding, "expired")
            db.commit()
            summary["auth_expired"] += 1
            continue
        try:
            new_tokens = refresh_access_token(refresh_token,
                                              http_post=http_post)
        except DriveOAuthError as exc:
            if exc.kind == "expired":
                _apply_refresh_failure(db, binding, "expired")
                db.commit()
                summary["auth_expired"] += 1
            elif exc.kind == "auth_error":
                _apply_refresh_failure(db, binding, "auth_error")
                db.commit()
                summary["auth_error"] += 1
            elif exc.kind == "throttled":
                summary["throttled"] += 1
                logger.warning("Drive 维护绑定 #%s 刷新被限流（不落行，下轮再试）",
                               binding.id)
            else:
                summary["transient_errors"] += 1
                logger.warning("Drive 维护绑定 #%s 刷新一过性失败（下轮再试）",
                               binding.id)
            continue
        _store_refreshed_token(db, binding, cred, new_tokens)
        db.commit()
        summary["refreshed"] += 1
    logger.info("Drive 凭据维护完成 scanned=%(scanned)s refreshed=%(refreshed)s "
                "auth_expired=%(auth_expired)s auth_error=%(auth_error)s "
                "throttled=%(throttled)s transient_errors=%(transient_errors)s "
                "skipped=%(skipped)s", summary)
    return summary


_probe_thread = None
_probe_lock = threading.Lock()


def start_drive_cred_maintenance_worker(stop_event=None) -> bool:
    """启动 Drive 凭据维护工人（worker.py 调用，照 list_projection 写法）。

    开关关时返回 False、不启动；幂等：已有存活线程时直接返回 True。
    启动即跑一轮，之后按周期跑；单轮异常只记警告、不停循环。
    """
    global _probe_thread
    if not maint_enabled():
        return False
    with _probe_lock:
        alive = _probe_thread
        if alive is not None and alive.is_alive():
            return True

        def _loop() -> None:
            from backend.database import SessionLocal

            try:
                session = SessionLocal()
                try:
                    sweep_drive_credentials(session)
                finally:
                    session.close()
            except Exception as exc:  # noqa: BLE001 — 首轮失败不挡工人
                logger.warning("Drive 凭据维护首轮失败（可忽略）: %s", exc)
            interval = maint_interval_sec()
            while stop_event is None or not stop_event.is_set():
                try:
                    if stop_event is not None:
                        if stop_event.wait(timeout=interval):
                            break
                    else:
                        time.sleep(interval)
                except Exception:  # noqa: BLE001 — 睡眠被打断也继续
                    break
                if stop_event is not None and stop_event.is_set():
                    break
                try:
                    session = SessionLocal()
                    try:
                        sweep_drive_credentials(session)
                    finally:
                        session.close()
                except Exception as exc:  # noqa: BLE001 — 单轮失败不停循环
                    logger.warning("Drive 凭据维护单轮失败（可忽略）: %s", exc)

        thread = threading.Thread(target=_loop, daemon=True,
                                  name="drive-cred-maintenance")
        thread.start()
        _probe_thread = thread
        return True
