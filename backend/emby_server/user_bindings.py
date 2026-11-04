"""BYOS 用户存储绑定（P1：绑定管理 + 115 直链播放）。

只做 P1 的三件事，多一件不做：
1. 绑定管理：建绑、验证、解绑、失效标记（user_storage_bindings
   行 + mount_credentials 行 scope=user，一行绑定只引用一行凭据，
   不另起第三套秘密存储）；
2. 115 直链播放：绑定用户播 115 型条目时，按该绑定自己的凭据
   现签现发 302 给播放器自拉（零服务器带宽），凭据绝不跨用户复用；
3. 护栏：无绑定用户与现有共享库（scope=source）行为逐字不变。

不做：Drive OAuth（P2）、按绑定扫描入库（P3）、路径授权表、
挂载门控逻辑。Drive 绑定创建在 P1 明确拒绝（见 create_binding），
因为 Drive 凭据验证随 P2 的 OAuth 上线——存一个验不活的 Drive
凭据行会违反“绑完先验活”的设计口径。

失效三档（照抄定稿 BYOS-4 语义，文案为暂定、用户一句话可推翻）：
- auth_error（凭据错）→ 绑定 invalid；
- expired（登录态过期，115 上游 401/403 定性为 cookie_expired）→
  绑定 expired；
- throttled（风控/被限）→ 绑定 invalid，直至用户主动重验恢复。
三档之外的一过性失败（网络等）不落状态、不改行，只把错误抛回
调用方重试——不把“网抖了一下”写成“凭据死了”。

另有一档不是凭据失效、是账号资格问题：pan115_vip_required
（2026-10-29 用户亲定）——115 非 VIP 账号绑了也播不了（下载/接口
对非会员关闭），故建绑/重验时 Cookie 验活通过后追加 VIP 校验：
确凿非 VIP → 落 invalid + 该 kind 明示原因（不许说成 Cookie
失效）；查不清（网络/上游 5xx/响应形状对不上）不拦、照常走。
开关 pan115_bind_vip_check_enabled（SystemConfig，默认开）。

夸克一期（2026-10-31）：provider="quark" 照 pan115 的 Cookie 型
形态接入——建绑即验活（/config + 列根目录，见 quarkdrive）、失效
三档同口径、过期文案点名夸克；上游轮换的 __puus 在验活后回写
密封凭据，只存初始值会慢性失效。入口开关 quark_bind_enabled
（SystemConfig，默认开）。一期只做绑定：转存/副本点播走夸克盘
是二期，本模块不实现。 会员校验同日追加（用户亲点，照 115 对齐）：
验活通过后调 /member 判会员三态，确凿非会员落 invalid +
quark_vip_required，开关 quark_bind_vip_check_enabled
（SystemConfig，默认开），查不清（unknown/异常）不拦。

凭据纪律：本模块是唯一经手绑定凭据明文的地方（创建时接收入参、
验证时解密封后立即用、用完不存变量外泄）；日志与返回值永远不出
凭据原文、不出密封值（只出 has_credential 布尔值）。
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import threading
import time
import uuid

import logging
from datetime import datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server.env_util import env_int
from backend.emby_server.upstream_sources import (
    SealedSecretError,
    seal_secret,
    unseal_secret,
)
from backend.integrations import store

logger = logging.getLogger(__name__)

#: 单用户绑定账号上限（BYOS-2 暂定：走配置不写死，默认值 3 是
#: BYOS 设计稿建议值、不是 FMBY 值，用户一句话可推翻）。
BINDING_LIMIT_ENV = "BYOS_BINDING_LIMIT_PER_USER"
BINDING_LIMIT_DEFAULT = 3

#: 失效三档 → 绑定状态（throttled 致 invalid：被限期间不再出直链，
#: 用户重验通过即恢复；不静默回落共享凭据）。
ERROR_KIND_TO_STATUS = {
    "auth_error": "invalid",
    "expired": "expired",
    "throttled": "invalid",
    "pan115_vip_required": "invalid",
    "quark_vip_required": "invalid",
}

#: 取链通道大小限制的用户端文案（不落绑定状态，故不进失效三档）。
_SIZE_LIMIT_MESSAGE = (
    "该文件较大，115 取链通道暂时受限，请稍后重试。")

#: 失效三档 → 用户端文案（照抄定稿 BYOS-4 暂定三句，非 FMBY 原文）。
BINDING_ERROR_MESSAGES = {
    "auth_error": "账号凭据不对，请重新绑定。",
    "expired": "登录已过期，请重新绑定。",
    "throttled": "账号被限制，请稍后再试或更换账号绑定。",
    "pan115_vip_required": "该 115 账号不是 VIP，115 绑定需要 VIP 账号，请开通后重新绑定。",
    "quark_vip_required": "该夸克账号不是会员，夸克绑定需要会员账号（免费账号下载限速、容量有限，无法正常使用），请开通会员后重新绑定。",
}

#: 115 直链变体名（见 play_variants.BYOS115_DIRECT）。
PROVIDER_PAN115 = "pan115"
PROVIDER_DRIVE = "drive"
PROVIDER_QUARK = "quark"

#: 已下线的用户绑定 provider（2026-11-02 站主亲定：夸克与 115 的
#: 用户绑定线整体删除）。建绑/重验/取流一律拒绝，名单不再返回，
#: 存量绑定行代码层视为不存在；存量数据本身一期不删（二期清理）。
REMOVED_PROVIDERS = (PROVIDER_PAN115, PROVIDER_QUARK)
REMOVED_PROVIDER_MESSAGE = "该网盘已下线，请改绑 Google Drive。"

#: 存量收敛落盘档位（2026-11-03 收敛单）：已下线 provider 的存量
#: active 行统一落 invalid + last_error_kind="provider_removed"
#: （档位名与转存域 PROVIDER_REMOVED_KIND 同名同义、终态不可用），
#: 用户端文案就是 REMOVED_PROVIDER_MESSAGE 本句，不另编新文案；
#: 凭据行由收敛脚本按 _mark_failed 同口径同步落同一档位。
ERROR_KIND_TO_STATUS["provider_removed"] = "invalid"
BINDING_ERROR_MESSAGES["provider_removed"] = REMOVED_PROVIDER_MESSAGE

#: 谷歌 Drive 绑定开关（SystemConfig 键，2026-11-04 站主亲令：绑定
#: 能力保留，但弄一个开关把绑定关掉、现置关）。管理端在既有
#: 「Drive API 限速」卡片上可见可改（写入走 config_service 家族
#: drive_api 校验，word 风格存 "true"/"false"）。
#: 开关只管「新建 Drive 绑定」一个动作：OAuth 发起与回调完成被拒、
#: providers 名单不返回 drive；已有绑定记录、解绑、状态查询与已绑
#: 用户的既有能力（播放/取流/转存）一律不受影响。
KEY_DRIVE_BINDING_ENABLED = "drive_binding_enabled"

#: 开关关闭时新建绑定的拒绝文案（用户端原样展示）。
DRIVE_BINDING_DISABLED_MESSAGE = "网盘绑定暂未开放。"


def drive_binding_enabled(db: Session) -> bool:
    """Drive 绑定开关现值（读取唯一实现）。

    默认关：无配置行 / 值非法 / 读取异常一律按关处理，只有字面
    "true" 才算开（word 风格，与 register_gift_* 开关同口径）。
    读口径走 store 热读（60 秒 TTL + 写时失效），管理端保存后
    本进程立即生效、其余进程在 TTL 内跟上。
    """
    try:
        raw = store.get_value(db, KEY_DRIVE_BINDING_ENABLED, "")
    except Exception:  # noqa: BLE001 配置层异常不许把绑定入口打成 500
        return False
    return str(raw).strip().lower() == "true"


class BindingError(RuntimeError):
    """绑定操作失败（带可分类的 kind，供调用方定状态码与文案）。

    kind 取值：auth_error / expired / throttled（落状态三档）、
    pan115_vip_required / quark_vip_required（非会员账号，落 invalid）、
    validation（入参/上限，400）、conflict（同名绑定且凭据不同，
    409）、unsupported（P1 不支持的 provider，如 drive，400）、
    not_found（非本人或不存在，404）、
    disabled（Drive 绑定开关关闭，403，不落状态）、
    transient（一过性失败，不落状态，调用方按 502 处理）。
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def binding_limit_per_user() -> int:
    """单用户绑定上限（配置，不写死；非法值回落默认值 3）"""
    return env_int(BINDING_LIMIT_ENV, BINDING_LIMIT_DEFAULT, lo=1)


#: provider 专属文案覆盖（只覆盖与 provider 强相关的句子，如
#: expired 里点名了 115 的那句；其余档位各 provider 共用
#: BINDING_ERROR_MESSAGES，不另编新档位）。
_PROVIDER_ERROR_MESSAGE_OVERRIDES = {
    "quark": {
        "expired": "夸克登录已过期，请重新绑定。",
        # 风控/限频不是过期：明说临时拦截，免得用户对着好好的
        # Cookie 反复重绑（2026-11-02 误判修复配套文案）。
        "throttled": "夸克临时拦截了这次验证（风控或操作频繁），"
                     "账号没有过期，请稍后再试。",
    },
}


def error_message(kind: str | None, provider: str | None = None) -> str:
    """失效档位 → 用户端文案；未知档位回落通用句（不编新档位）"""
    if provider:
        override = _PROVIDER_ERROR_MESSAGE_OVERRIDES.get(provider, {})
        if kind in override:
            return override[kind]
    if kind in BINDING_ERROR_MESSAGES:
        return BINDING_ERROR_MESSAGES[kind]
    return "绑定不可用，请重新绑定。"


#: 挂载类型 → 用户端绑定 provider（GET /storage-bindings/providers 用）。
#: 只映射 BYOS 有绑定入口的三类：drive（含 gdrive / google 系别名）、115 系与 quark；
#: 其余类型（local / rclone / yun139 等）没有绑定入口，不映射、不返回。
_MOUNT_TYPE_TO_BINDING_PROVIDER = {
    "drive": "drive", "gdrive": "drive", "google": "drive",
    "googledrive": "drive", "google_drive": "drive",
    "115": "pan115", "pan115": "pan115",
    "quark": "quark",
}


#: 额外开放的绑定入口（逗号分隔，取值 drive / pan115 / quark）。用于"后台没有该类
#: 型挂载、但要让用户先绑上"的场景，例如 Drive 源条目经跨盘转存给 115
#: 用户播放：源走 SA 读，用户只需 115 绑定，后台无需 115 挂载。
#: 默认空 = 行为不变（只按启用挂载映射）。非法值忽略。
EXTRA_PROVIDERS_ENV = "BYOS_EXTRA_BINDING_PROVIDERS"


def _extra_binding_providers() -> set[str]:
    raw = os.getenv(EXTRA_PROVIDERS_ENV, "") or ""
    vals = {v.strip().lower() for v in raw.split(",") if v.strip()}
    return {v for v in vals if v in ("drive", "pan115", "quark")}


def list_binding_providers(db: Session) -> list[str]:
    """后台实际启用的挂载类型映射后的 provider 名单（只读小口的查询本体）。

    只返回映射后的 provider 名，不返回挂载路径、凭据、账号等任何敏感字段；
    只有 is_enabled 的挂载行才计入（NULL 按列默认值启用计）。
    drive 另受绑定开关管辖：开关关时（默认关）即使有启用挂载也不返回。
    """
    rows = (
        db.query(em.StorageMount.mount_type)
        .filter(em.StorageMount.is_enabled.isnot(False))
        .distinct()
        .all()
    )
    found = set()
    for (raw,) in rows:
        mapped = _MOUNT_TYPE_TO_BINDING_PROVIDER.get((raw or "").strip().lower())
        if mapped:
            found.add(mapped)
    found |= _extra_binding_providers()
    # 已下线 provider 永不出现在名单里（挂载映射与 env 额外名单
    # 两侧都过滤，前端据此整块不渲染这两家）。
    for removed in REMOVED_PROVIDERS:
        found.discard(removed)
    # Drive 绑定开关关时 drive 永不出现在名单里：与挂载驱动口径
    # 叠加，挂载映射与 env 额外名单两侧都压过——两个条件任一不
    # 满足都不给绑（开关见 KEY_DRIVE_BINDING_ENABLED）。
    if not drive_binding_enabled(db):
        found.discard(PROVIDER_DRIVE)
    # 固定顺序输出；前端只做 includes 判断，不依赖顺序
    return [p for p in ("drive", "pan115", "quark") if p in found]


def serialize_binding(binding: em.UserStorageBinding) -> dict:
    """绑定行 → 用户端响应：绝不含凭据原文、密封值与内部凭据行号。
    Drive 行另出 needs_rebind（已授范围缺 drive.file 即 True，老绑定
    无记录也 True）与 granted_scopes（实授范围列表，无记录为 None），
    供“我的网盘”挂重绑引导；115 行 needs_rebind 恒 False。"""
    needs_rebind = False
    granted_list = None
    if binding.provider == "drive":
        granted_list = (
            (binding.granted_scopes or "").split()
            if binding.granted_scopes else None
        )
        # drive_oauth → user_bindings 的顶层 import 会循环，函数内懒引。
        from backend.emby_server import drive_oauth
        needs_rebind = drive_oauth.binding_needs_rebind(
            binding.granted_scopes)
    return {
        "id": binding.id,
        "provider": binding.provider,
        "account_label": binding.account_label,
        "status": binding.status,
        "last_verified_at": (
            binding.last_verified_at.isoformat()
            if binding.last_verified_at else None
        ),
        "last_error_kind": binding.last_error_kind,
        "error_message": (
            error_message(binding.last_error_kind, binding.provider)
            if binding.last_error_kind else None
        ),
        "has_credential": True,
        "needs_rebind": needs_rebind,
        "granted_scopes": granted_list,
        "created_at": (
            binding.created_at.isoformat() if binding.created_at else None
        ),
    }


def get_active_binding(db: Session, user_id: int,
                       provider: str) -> em.UserStorageBinding | None:
    """该用户该 provider 下最早建的 active 绑定（P1 播放选凭据口）。

    多个 active 绑定时取最早建的一个（P1 口径，写进文档；P3 按绑定
    扫描入库后，条目与绑定的归属由扫描线定，不再靠“最早”）。
    只返回本用户自己的行，绝不跨用户。
    已下线 provider（REMOVED_PROVIDERS）恒回 None：存量行视为不存在。
    """
    if provider in REMOVED_PROVIDERS:
        return None
    return (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.user_id == user_id,
                em.UserStorageBinding.provider == provider,
                em.UserStorageBinding.status == "active")
        .order_by(em.UserStorageBinding.id.asc())
        .first()
    )


#: per-binding 凭据锁的 Redis 键前缀（规格：binding:<id>）。
_BINDING_LOCK_PREFIX = "binding:"
#: 锁 TTL（毫秒）：临界区可能含整段上传（分钟级），靠续租线程
#: 保活，TTL 只防持锁进程猝死后死锁；续租间隔 = TTL/3。
_BINDING_LOCK_TTL_MS = 120_000
_LOCK_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) end return 0")
_LOCK_RENEW_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('pexpire', KEYS[1], ARGV[2]) end return 0")


#: 夸克 Cookie 的轮换字段（上游每次响应都可能轮换，只有这两个）。
_ROTATION_COOKIE_KEYS = ("__puus", "__pus")


def _cookie_field(cookie: str, name: str) -> str:
    """从 Cookie 串里取一个字段值，取不到回空串。"""
    for chunk in (cookie or "").split(";"):
        chunk = chunk.strip()
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        if key.strip() == name:
            return value.strip()
    return ""


def _same_credential(submitted: str, stored: str) -> bool:
    """两份 Cookie 是否同一凭据：轮换字段不计。

    夸克验活会轮换 __puus/__pus 并回写库内凭据，因此“同一表单
    重提”时提交值与库内值常只差轮换字段——必须忽略它们比较，
    否则重试会被误判成不同账号。115 凭据无轮换字段，等价于
    明文相等比较。无 '=' 的裸片段两边一致忽略。
    """

    def parts(cookie: str) -> tuple:
        pairs = {}
        bare = []
        for chunk in (cookie or "").split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            if "=" in chunk:
                key, value = chunk.split("=", 1)
                key = key.strip()
                if key in _ROTATION_COOKIE_KEYS:
                    continue
                pairs[key] = value.strip()
            else:
                # 无 '=' 的裸片段（如桩里的 COOKIE-XYZ）也参与比较：
                # 忽略它们会把不同裸凭据误判成同一凭据。
                bare.append(chunk)
        return pairs, sorted(bare)

    return parts(submitted) == parts(stored)


def _credential_of(db: Session,
                   binding: em.UserStorageBinding) -> em.MountCredential:
    cred = (
        db.query(em.MountCredential)
        .filter(em.MountCredential.id == binding.credential_id)
        .first()
    )
    if cred is None:
        raise BindingError("transient", "绑定凭据行缺失，请重新绑定。")
    if cred.scope != "user" or cred.owner_user_id != binding.user_id:
        # 凭据行归属与绑定行不一致 = 数据异常，响亮报错、不 fallback
        raise BindingError("transient", "绑定凭据归属异常，请重新绑定。")
    return cred


def _mark_failed(db: Session, binding: em.UserStorageBinding,
                 kind: str) -> None:
    """绑定行 + 凭据行同步落失败状态（单事务内由调用方提交）"""
    status = ERROR_KIND_TO_STATUS[kind]
    binding.status = status
    binding.last_error_kind = kind
    cred = _credential_of(db, binding)
    cred.status = status
    cred.last_error_kind = kind
    logger.warning("绑定 #%s（用户 %s，%s）标记失败：%s",
                   binding.id, binding.user_id, binding.provider, kind)


def create_binding(db: Session, user_id: int, provider: str,
                   account_label: str, credential: str,
                   ) -> em.UserStorageBinding:
    """建绑 + 同步验活（P1 唯一建绑口）。

    - provider 必须在 USER_STORAGE_PROVIDERS 内；drive 在 P1 拒绝
      （验证随 P2 OAuth 上线，不存验不活的行），错误明说 P2；
    - label 去空白后 1–100 字；同用户同 provider 下已有同名绑定时，
      同凭据（轮换字段不计）幂等返回已有行、不同凭据 409 人话提示，
      并发撞唯一约束的竞态分支走同一口径，永不 500；
    - 上限：该用户全部绑定行（含失败态）计入上限，超了拒绝
      （删掉死行即可腾位，不静默挤掉旧行）；
    - 验活通过：绑定 + 凭据行双双 active + last_verified_at；
    - 验活失败属三档内：两行同步落失败态并返回该行（调用方按
      201 + status 告知用户，不静默、不回落）；
    - 一过性失败：不落行（已插的行回滚），抛 transient。
    返回绑定行（调用方提交后 status 即最终态）。
    """
    provider = (provider or "").strip()
    if provider not in em.USER_STORAGE_PROVIDERS:
        raise BindingError("validation", f"不支持的 provider：{provider}")
    if provider in REMOVED_PROVIDERS:
        # 已下线（2026-11-02 删除单）：在任何凭据处理之前拒绝，
        # 不落行、不验活。
        raise BindingError("unsupported", REMOVED_PROVIDER_MESSAGE)
    if provider == PROVIDER_DRIVE:
        # Drive 不收明文凭据：唯一建绑口是 OAuth start/callback
        # （见 drive_oauth），存一个验不活的 Drive 凭据行会违反
        # “绑完先验活”，故明确拒绝、不落行。
        raise BindingError(
            "unsupported",
            "Drive 绑定请走 OAuth 授权：POST /storage-bindings/drive/oauth/start。")
    raise BindingError("unsupported",
                       "该 provider 不支持建绑。")


def verify_binding(db: Session, user_id: int,
                   binding_id: int) -> em.UserStorageBinding:
    """重验绑定（失效后恢复口）：本人行才可重验。

    - 解密封失败（SealedSecretError，密钥轮换后旧密文解不开）→
      响亮报错，不静默回落（复用 25-1 口径：凭据失效请重录）；
    - 验活通过 → 双行 active + last_verified_at；
    - 验活失败属三档内 → 双行同步落失败态并返回该行；
    - 一过性失败 → 不改状态，抛 transient。
    """
    binding = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.id == binding_id,
                em.UserStorageBinding.user_id == user_id)
        .first()
    )
    if binding is None:
        raise BindingError("not_found", "绑定不存在或不属于当前用户。")
    if binding.provider in REMOVED_PROVIDERS:
        # 已下线 provider 的存量行不许重验复活（2026-11-02 删除单）。
        raise BindingError("unsupported", REMOVED_PROVIDER_MESSAGE)
    if binding.provider == PROVIDER_DRIVE:
        # P2：Drive 重验 = 强制刷新 + 验活（drive_oauth，局部导入
        # 防循环：drive_oauth 顶层导入本模块）。
        from backend.emby_server import drive_oauth

        return drive_oauth.refresh_drive_binding(db, user_id, binding_id)
    raise BindingError("unsupported",
                       "该 provider 不支持重验。")


def delete_binding(db: Session, user_id: int, binding_id: int,
                   http_post=None) -> None:
    """解绑：删绑定行 + 其凭据行（单事务，不留残留）。

    非本人行按不存在处理（不泄露他人绑定存在性）。
    转存副本政策（2026-10-27 清理规则第 5 条，写死）：解绑时
    不自动删用户盘转存目录里的副本，也不改变体行——副本仍是
    用户盘里的文件，删不删须另行问用户；本函数绝不调用清理
    模块，测试 test_transfer_cleanup.py 的解绑用例锁死这一点。
    防外流例外（2026-10-28，收权限≠删副本）：解绑时先收回该
    绑定名下用户副本的 anyone-reader 权限（只收权限：文件不动、
    行不删、行状态与 file_id 不变；唯一写的是新增审计列
    share_revoked_at，故不违反上述锁死规则——锁死用例断言的
    正是行状态与 file_id 不变）。顺序是关键：
    必须在删凭据行之前用尚有效的绑定令牌执行收回；令牌已失效
    时 best-effort（记日志、不阻塞解绑、不抛异常）。
    """
    binding = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.id == binding_id,
                em.UserStorageBinding.user_id == user_id)
        .first()
    )
    if binding is None:
        raise BindingError("not_found", "绑定不存在或不属于当前用户。")
    cred = (
        db.query(em.MountCredential)
        .filter(em.MountCredential.id == binding.credential_id)
        .first()
    )
    try:
        from backend.emby_server import transfer_variant as _tv
        _tv.revoke_user_copy_sharing(db, user_id, binding=binding)
    except Exception:  # noqa: BLE001 — 收回失败绝不阻塞解绑
        logger.warning("解绑收回副本分享失败 user=%s binding=%s",
                       user_id, binding_id, exc_info=True)
    if binding.provider == PROVIDER_DRIVE and cred is not None:
        # 解绑断净（2026-10-30 安全收口）：删行前用尚存的 refresh
        # token 调 Google 吊销端点，让谷歌侧授权一并失效——只删
        # 本地行、不断谷歌侧授权，令牌值若曾外泄，解绑后照样能
        # 用。顺序在收回分享之后（收回还要用绑定令牌）、删行
        # 之前。best-effort：解不开/吊销失败只记日志，绝不阻塞
        # 解绑（与重绑的 _revoke_refresh_token_best_effort 同口径，
        # 直接复用同一实现）。pan115 是 Cookie 型凭据、没有
        # Google 授权可吊销，跳过。
        try:
            from backend.emby_server import drive_oauth as _do
            _refresh = _do.decode_drive_token(
                unseal_secret(cred.sealed_credential or "")
            ).get("refresh_token")
            if _refresh:
                _do._revoke_refresh_token_best_effort(_refresh, http_post)
        except Exception:  # noqa: BLE001 — 吊销失败绝不阻塞解绑
            logger.warning("解绑吊销 Google 令牌失败 user=%s binding=%s",
                           user_id, binding_id, exc_info=True)
    db.delete(binding)
    # 先把绑定行 flush 落库再删凭据行：两模型间没有 ORM relationship，
    # 单次 flush 时 SQLAlchemy 不保证先删绑定行，生产 PG 上曾出现凭据行
    # 先删、撞 user_storage_bindings_credential_id_fkey 外键、整事务回滚
    # （DELETE /storage-bindings/{id} 500）。flush 不等于提交，单事务
    # 原子性不变：后一步失败仍整体回滚。
    db.flush()
    if cred is not None:
        db.delete(cred)
    db.commit()


def list_bindings(db: Session, user_id: int) -> list[dict]:
    """本人绑定列表（序列化，绝不含凭据材料）"""
    rows = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.user_id == user_id)
        .order_by(em.UserStorageBinding.id.asc())
        .all()
    )
    return [serialize_binding(r) for r in rows]


class AliveProbeResult(str):
    """存活探测结论（str 子类）：本体是 "alive" / "dead" / "unknown"，
    另带探测最终 HTTP 状态码 ``status``（探测异常/空地址时 None）。

    出流逐跳留痕要状态码（只知道 dead 不知道是 404 还是 410，
    下次取证还是瞎），用 str 子类透传：全部既有 == 比较与分支
    判断行为不变；桩返回裸 str 时 getattr 取不到 status，留痕
    侧自然降级。
    """

    def __new__(cls, verdict: str, status=None):
        obj = super().__new__(cls, verdict)
        obj.status = status
        return obj


