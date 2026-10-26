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

凭据纪律：本模块是唯一经手绑定凭据明文的地方（创建时接收入参、
验证时解密封后立即用、用完不存变量外泄）；日志与返回值永远不出
凭据原文、不出密封值（只出 has_credential 布尔值）。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server import transfer115
from backend.emby_server.env_util import env_int
from backend.emby_server.upstream_sources import (
    SealedSecretError,
    seal_secret,
    unseal_secret,
)

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
}

#: 失效三档 → 用户端文案（照抄定稿 BYOS-4 暂定三句，非 FMBY 原文）。
BINDING_ERROR_MESSAGES = {
    "auth_error": "账号凭据不对，请重新绑定。",
    "expired": "115 登录已过期，请重新绑定。",
    "throttled": "账号被限制，请稍后再试或更换账号绑定。",
}

#: 115 直链变体名（见 play_variants.BYOS115_DIRECT）。
PROVIDER_PAN115 = "pan115"
PROVIDER_DRIVE = "drive"


class BindingError(RuntimeError):
    """绑定操作失败（带可分类的 kind，供调用方定状态码与文案）。

    kind 取值：auth_error / expired / throttled（落状态三档）、
    validation（入参/上限/重名，400）、unsupported（P1 不支持的
    provider，如 drive，400）、not_found（非本人或不存在，404）、
    transient（一过性失败，不落状态，调用方按 502 处理）。
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


def binding_limit_per_user() -> int:
    """单用户绑定上限（配置，不写死；非法值回落默认值 3）"""
    return env_int(BINDING_LIMIT_ENV, BINDING_LIMIT_DEFAULT, lo=1)


def error_message(kind: str | None) -> str:
    """失效档位 → 用户端文案；未知档位回落通用句（不编新档位）"""
    if kind in BINDING_ERROR_MESSAGES:
        return BINDING_ERROR_MESSAGES[kind]
    return "绑定不可用，请重新绑定。"


#: 挂载类型 → 用户端绑定 provider（GET /storage-bindings/providers 用）。
#: 只映射 BYOS 有绑定入口的两类：drive（含 gdrive / google 系别名）与 115 系；
#: 其余类型（local / rclone / yun139 等）没有绑定入口，不映射、不返回。
_MOUNT_TYPE_TO_BINDING_PROVIDER = {
    "drive": "drive", "gdrive": "drive", "google": "drive",
    "googledrive": "drive", "google_drive": "drive",
    "115": "pan115", "pan115": "pan115",
}


def list_binding_providers(db: Session) -> list[str]:
    """后台实际启用的挂载类型映射后的 provider 名单（只读小口的查询本体）。

    只返回映射后的 provider 名，不返回挂载路径、凭据、账号等任何敏感字段；
    只有 is_enabled 的挂载行才计入（NULL 按列默认值启用计）。
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
    # 固定顺序输出；前端只做 includes 判断，不依赖顺序
    return [p for p in ("drive", "pan115") if p in found]


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
            error_message(binding.last_error_kind)
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
    """
    return (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.user_id == user_id,
                em.UserStorageBinding.provider == provider,
                em.UserStorageBinding.status == "active")
        .order_by(em.UserStorageBinding.id.asc())
        .first()
    )


def binding_115_cookie(db: Session, user_id: int,
                       binding_id: int) -> str:
    """指定绑定行的 115 Cookie（BYOS P3 按绑定扫描列举用，唯一口）。

    - 只认本人行、只认 pan115 行：他人行 / 非 115 行按 not_found 处理
      （不泄露他人绑定存在性，与 delete_binding 同口径）；
    - 凭据行归属不对（scope 非 user、owner 非本人）→ transient 响亮报错，
      不回落共享 Cookie——回落会把别人的盘当成该用户的盘；
    - 解密封失败 → 绑定落 auth_error 后抛 auth_error（P1 直链同款口径，
      密钥轮换后旧密文解不开，须重绑）；
    - 返回的明文只给调用方当次列举用：调用方（byos_scan）不得记日志、
      不得进返回值、不得跨绑定复用。
    """
    binding = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.id == binding_id,
                em.UserStorageBinding.user_id == user_id)
        .first()
    )
    if binding is None or binding.provider != PROVIDER_PAN115:
        raise BindingError("not_found", "绑定不存在或不属于当前用户。")
    cred = _credential_of(db, binding)
    try:
        cookie = unseal_secret(cred.sealed_credential or "")
    except SealedSecretError as exc:
        _mark_failed(db, binding, "auth_error")
        db.commit()
        raise BindingError(
            "auth_error", BINDING_ERROR_MESSAGES["auth_error"]) from exc
    if not cookie:
        _mark_failed(db, binding, "auth_error")
        db.commit()
        raise BindingError("auth_error", BINDING_ERROR_MESSAGES["auth_error"])
    return cookie


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


def _classify_pan115_error(exc: Exception) -> tuple[str, str] | None:
    """115 失败 → (error_kind, 文案)，一过性失败返回 None（不落状态）。

    - Pan115AuthError（含 HTTP 401/403、auth errno、登录态失效）→
      expired（照抄定稿 BYOS-4：401/403 定性为 cookie_expired）；
    - 含限流信号的 Pan115Error → throttled；
    - 其余 Pan115Error / 网络异常 → None（一过性，不落状态）。
    """
    if isinstance(exc, transfer115.Pan115AuthError):
        return "expired", BINDING_ERROR_MESSAGES["expired"]
    if isinstance(exc, transfer115.Pan115Error):
        text = str(exc)
        if ("429" in text or "频繁" in text or "限流" in text
                or "too many" in text.lower()):
            return "throttled", BINDING_ERROR_MESSAGES["throttled"]
        return None
    return None


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


def _verify_pan115_cookie(cookie: str) -> None:
    """115 验活：check + 列根目录（设计稿 §3.3：轻量验证）。

    两步都通才算活；check 只证明会话有效，列根目录证明该会话
    真能读目录（直链播放的前提）。异常按 _classify_pan115_error
    分类后由调用方定状态。
    """
    client = transfer115.Pan115Client(cookie)
    client.check()
    client.list_dir("0")


def create_binding(db: Session, user_id: int, provider: str,
                   account_label: str, credential: str,
                   ) -> em.UserStorageBinding:
    """建绑 + 同步验活（P1 唯一建绑口）。

    - provider 必须在 USER_STORAGE_PROVIDERS 内；drive 在 P1 拒绝
      （验证随 P2 OAuth 上线，不存验不活的行），错误明说 P2；
    - label 去空白后 1–100 字；同用户同 provider 下重名拒绝；
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
    if provider == PROVIDER_DRIVE:
        # Drive 不收明文凭据：唯一建绑口是 OAuth start/callback
        # （见 drive_oauth），存一个验不活的 Drive 凭据行会违反
        # “绑完先验活”，故明确拒绝、不落行。
        raise BindingError(
            "unsupported",
            "Drive 绑定请走 OAuth 授权：POST /storage-bindings/drive/oauth/start，"
            "本口只接受 115 绑定。")
    label = (account_label or "").strip()
    if not label or len(label) > 100:
        raise BindingError("validation", "account_label 需为 1–100 字。")
    cookie = transfer115.normalize_cookie(credential or "")
    if not cookie:
        raise BindingError("validation", "凭据不能为空。")
    existing = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.user_id == user_id,
                em.UserStorageBinding.provider == provider,
                em.UserStorageBinding.account_label == label)
        .first()
    )
    if existing is not None:
        raise BindingError(
            "validation", f"同 provider 下已存在同名绑定「{label}」。")
    count = (
        db.query(em.UserStorageBinding)
        .filter(em.UserStorageBinding.user_id == user_id)
        .count()
    )
    if count >= binding_limit_per_user():
        raise BindingError(
            "validation",
            f"绑定已达上限（{binding_limit_per_user()} 个），请先解绑不用的账号。")

    cred = em.MountCredential(
        # mount_id=NULL：绑定凭据不附属任何挂载（见 MountCredential
        # docstring；NULL 行不受（mount_id, scope, owner）唯一约束限，
        # 一用户多绑定才建得出来）。
        mount_id=None, scope="user", owner_user_id=user_id,
        sealed_credential=seal_secret(cookie), key_id="fernet-v1",
        status="invalid")
    db.add(cred)
    db.flush()  # 取 cred.id，供绑定行引用
    binding = em.UserStorageBinding(
        user_id=user_id, provider=provider, account_label=label,
        credential_id=cred.id, status="invalid")
    db.add(binding)
    db.flush()

    try:
        _verify_pan115_cookie(cookie)
    except Exception as exc:  # noqa: BLE001 — 分类后决定落态或回滚
        classified = _classify_pan115_error(exc)
        if classified is None:
            db.rollback()
            raise BindingError(
                "transient", f"验证时上游失败（未落行，可重试）：{exc}") from exc
        kind, _message = classified
        _mark_failed(db, binding, kind)
        db.commit()
        return binding

    now = datetime.now()
    binding.status = "active"
    binding.last_verified_at = now
    binding.last_error_kind = None
    cred.status = "active"
    cred.last_verified_at = now
    cred.last_error_kind = None
    db.commit()
    return binding


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
    if binding.provider != PROVIDER_PAN115:
        if binding.provider == PROVIDER_DRIVE:
            # P2：Drive 重验 = 强制刷新 + 验活（drive_oauth，局部导入
            # 防循环：drive_oauth 顶层导入本模块）。
            from backend.emby_server import drive_oauth

            return drive_oauth.refresh_drive_binding(db, user_id, binding_id)
        raise BindingError("unsupported", "该 provider 不支持重验。")
    cred = _credential_of(db, binding)
    try:
        cookie = unseal_secret(cred.sealed_credential or "")
    except SealedSecretError as exc:
        raise BindingError(
            "auth_error", "凭据已失效（密钥轮换后旧密文解不开），请重新绑定。") from exc
    if not cookie:
        _mark_failed(db, binding, "auth_error")
        db.commit()
        return binding
    try:
        _verify_pan115_cookie(cookie)
    except Exception as exc:  # noqa: BLE001 — 分类后决定落态或不改
        classified = _classify_pan115_error(exc)
        if classified is None:
            raise BindingError(
                "transient", f"重验时上游失败（状态未改，可重试）：{exc}") from exc
        kind, _message = classified
        _mark_failed(db, binding, kind)
        db.commit()
        return binding
    now = datetime.now()
    binding.status = "active"
    binding.last_verified_at = now
    binding.last_error_kind = None
    cred.status = "active"
    cred.last_verified_at = now
    cred.last_error_kind = None
    db.commit()
    return binding


def delete_binding(db: Session, user_id: int, binding_id: int) -> None:
    """解绑：删绑定行 + 其凭据行（单事务，不留残留）。

    非本人行按不存在处理（不泄露他人绑定存在性）。
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


def resolve_user115_direct_url(db: Session, user_id: int,
                               item: em.MediaItem) -> str:
    """绑定用户播 115 型条目：按该绑定自己的凭据现签直链（P1 播放口）。

    - 条目须为 mount:// 路径且挂载类型为 115，否则 BindingError
     （not_found：本变体不适用，调用方按 409 处理）；
    - 凭据只取该用户自己的 active 绑定（最早建的一个），绝不跨用户；
    - 直链现签现取，**不走共享 pickcode 缓存**：缓存键只有 pickcode，
      跨用户复用会把 A 的签名地址发给 B；
    - 凭据失效（115 401/403 → cookie_expired）→ 绑定落失败态后
      抛 BindingError（kind=expired/throttled/auth_error，调用方按
      409 + 三档文案告知用户，不静默回落共享中继）；
    - 一过性失败 → 不改状态，抛 transient（调用方按 409 通用句处理）。
    返回 115 下载地址（调用方 302 给播放器自拉，字节不过服务器）。
    """
    from backend.emby_server import mounts as mount_lib

    parsed = mount_lib.parse_mount_path(getattr(item, "file_path", "") or "")
    if parsed is None:
        raise BindingError("not_found", "条目不是挂载路径，无绑定直链。")
    mount_id, rel = parsed
    mount = (
        db.query(em.StorageMount)
        .filter(em.StorageMount.id == mount_id)
        .first()
    )
    if mount is None or mount.mount_type != mount_lib.MOUNT_PAN115:
        raise BindingError("not_found", "条目挂载不是 115，无绑定直链。")
    binding = get_active_binding(db, user_id, PROVIDER_PAN115)
    if binding is None:
        raise BindingError("not_found", "当前用户没有可用的 115 绑定。")
    cred = _credential_of(db, binding)
    try:
        cookie = unseal_secret(cred.sealed_credential or "")
    except SealedSecretError as exc:
        _mark_failed(db, binding, "auth_error")
        db.commit()
        raise BindingError(
            "auth_error", BINDING_ERROR_MESSAGES["auth_error"]) from exc
    if not cookie:
        _mark_failed(db, binding, "auth_error")
        db.commit()
        raise BindingError("auth_error", BINDING_ERROR_MESSAGES["auth_error"])
    provider = mount_lib.Pan115Mount(mount, db=db, cookie_override=cookie)
    try:
        entry = provider._locate(rel)
        pickcode = entry.get("pickcode") or ""
        if not pickcode:
            raise mount_lib.MountError(f"115 未返回该文件的 pickcode: {rel}")
        # 现签现取：不用 _get_cached_115_url（键只有 pickcode，跨用户
        # 复用即把 A 的签名地址发给 B；且直链本身有时效，现取最稳）。
        url = transfer115.Pan115Client(cookie).download_url(pickcode)
    except mount_lib.MountAuthError as exc:
        _mark_failed(db, binding, "expired")
        db.commit()
        raise BindingError(
            "expired", BINDING_ERROR_MESSAGES["expired"]) from exc
    except Exception as exc:  # noqa: BLE001 — 分类后决定落态或不改
        classified = _classify_pan115_error(exc)
        if classified is not None:
            kind, message = classified
            _mark_failed(db, binding, kind)
            db.commit()
            raise BindingError(kind, message) from exc
        if isinstance(exc, mount_lib.MountError):
            raise BindingError("not_found", f"115 上找不到文件：{rel}") from exc
        raise BindingError("transient", f"115 直链解析失败：{exc}") from exc
    return url


def resolve_user115_copy_url(db: Session, user_id: int,
                             pickcode: str) -> str:
    """用户盘 115 副本直链：按该用户自己的 Cookie 现签（转存播放口）。

    与 resolve_user115_direct_url 同一套凭据口径（最早 active 绑定、
    现签现取、**不走共享 pickcode 缓存**），区别只在 pickcode 来源：
    这里直接用转存副本行的 pickcode，不从条目路径定位（副本在用户
    自己的盘里，不在共享挂载路径上，定位不到）。
    凭据失效 → BindingError（expired/auth_error），调用方按 409 处理，
    不静默回落共享中继；一过性失败 → transient。
    """
    binding = get_active_binding(db, user_id, PROVIDER_PAN115)
    if binding is None:
        raise BindingError("not_found", "当前用户没有可用的 115 绑定。")
    cred = _credential_of(db, binding)
    try:
        cookie = unseal_secret(cred.sealed_credential or "")
    except SealedSecretError as exc:
        _mark_failed(db, binding, "auth_error")
        db.commit()
        raise BindingError(
            "auth_error", BINDING_ERROR_MESSAGES["auth_error"]) from exc
    if not cookie:
        _mark_failed(db, binding, "auth_error")
        db.commit()
        raise BindingError(
            "auth_error", BINDING_ERROR_MESSAGES["auth_error"])
    try:
        url = transfer115.Pan115Client(cookie).download_url(pickcode)
    except transfer115.Pan115AuthError as exc:
        _mark_failed(db, binding, "expired")
        db.commit()
        raise BindingError(
            "expired", BINDING_ERROR_MESSAGES["expired"]) from exc
    except Exception as exc:  # noqa: BLE001 — 分类后决定落态或不改
        classified = _classify_pan115_error(exc)
        if classified is not None:
            kind, message = classified
            _mark_failed(db, binding, kind)
            db.commit()
            raise BindingError(kind, message) from exc
        raise BindingError("transient", f"115 直链解析失败：{exc}") from exc
    return url
