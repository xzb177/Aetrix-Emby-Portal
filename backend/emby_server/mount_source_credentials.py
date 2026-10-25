# -*- coding: utf-8 -*-
"""源级挂载凭据迁移 + KEK 文件信封（FMBY 挂载学习，清单第 4 步）。

只做第 4 步，多一件不做（维护工人、健康落库、断旧路均另开 PR）：

1. **KEK 文件信封**（挂载-6 照抄）：KEK 存服务器文件，目录只从
   ``MOUNT_KEK_DIR`` 读（复用 ``mount_sources.resolve_kek_dir``，无代码
   默认值）；文件名按 provider 取同款命名法——``key_id`` 即文件主名，
   文件为 ``<key_id>.b64``（如 ``pan115-kek.b64``、轮换代
   ``pan115-kek-v2.b64``），内容为一行 Fernet 密钥（urlsafe base64）。
   密文一律带 ``key_id`` 信封：KEK 密封值前缀 ``kek-v1:``，行上
   ``key_id`` 标明用哪把 KEK；25-1 旧密封（前缀 ``fernet-v1:``、
   ``key_id="fernet-v1"``）按原口径解，**永不因 KEK 落地而失效**。
2. **源级凭据迁移**：``mount_credentials`` 的 ``scope=source`` 行
   （服级共享凭据）。用户域（scope=user）已由 BYOS 接线，**本模块
   不碰用户域凭据路径**：读写一律限定 ``scope="source"`` 且
   ``mount_id`` 非空，绑定行（mount_id 为空）永远不在本模块的
   查询/迁移/轮换范围内。
3. **双读回落**（设计第 4 步要求）：凭据表读不到（无行/空密封）
   时回落旧路径并记警告——回落只发生在“表里没有”，不发生在
   “表里有但解不开”：密封损坏、KEK 缺失/权限不对时响亮报错，
   绝不静默换成另一份凭据。
4. **步骤旗标默认关**：``MOUNT_SOURCE_CREDENTIALS_ENABLED=1``
   才启用 115 挂载的表优先读；关时全站行为逐字不变（旧路径唯一
   权威）。迁移与轮换是显式调用的运维函数，不在导入/启动时跑。

向后兼容（本 PR 的硬承诺，见 PR 描述）：
- 旧密封行（fernet-v1）只读不重写：迁移跳过已有源级行（不覆盖、
  不重加密）；读路径按前缀/key_id 分发，旧行与 KEK 行双读共存。
- 读路径不做懒重封：重封只发生在显式调用的轮换函数里，且轮换
  前先校验新 KEK 可用、逐行先解旧、解不开的行原样跳过不写。
- 轮换周期暂定 90 天（挂载-6 暂定附注，非 FMBY 值）；轮换时双读
  期记警告，``keep_mount_ids`` 可按挂载单独保留旧密钥——旧 KEK
  文件在保留挂载全部轮换前不得删除，否则保留行解不开（轮换函数
  只跳过不写，不会把行写坏）。

安全硬线：本模块任何日志、异常、返回值里只出现 ``has_secret``
布尔语义、挂载 id、key_id 与状态词，**绝不出现凭据原文、KEK
内容、密封值**。KEK 文件权限必须是 0600 且不是符号链接，否则
拒绝加载（fail-closed）；文件内容只进内存 Fernet 对象，不落日志。
"""

from __future__ import annotations

import json
import logging
import os
import re
import stat
from pathlib import Path

from backend.emby_server import models as em
from backend.emby_server import upstream_sources as ups
from backend.emby_server.env_util import env_int
from backend.emby_server.mount_sources import resolve_kek_dir

logger = logging.getLogger(__name__)

#: 源级凭据表优先读的总闸（默认关）。静态 env 扫描口径：
#: ``*_ENV = "NAME"`` 常量即读取证据，见 test_perfection_r44_envdoc。
SOURCE_CREDENTIALS_ENABLED_ENV = "MOUNT_SOURCE_CREDENTIALS_ENABLED"

#: KEK 密封值前缀（格式变了就换前缀，旧值按前缀分发，不静默兼容）。
KEK_SEAL_PREFIX = "kek-v1:"

#: 25-1 旧密封前缀与 key_id（与 25-1 方案标识同一值，
#: 这里只读 upstream 的前缀常量，不另起第二套标识）。
LEGACY_SEAL_PREFIX = ups._SEAL_PREFIX
LEGACY_KEY_ID = ups._SEAL_PREFIX.rstrip(":")

#: key_id 合法形态：文件主名，不许含路径分隔符、``..`` 与空白，
#: 否则 ``<key_id>.b64`` 会跑出 KEK 目录（路径穿越守卫）。
_KEY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")


class KEKError(ups.SealedSecretError):
    """KEK 不可用（未配目录/文件缺失/权限不对/内容非法）。

    是 SealedSecretError 的子类：凭据消费方按“凭据失效”统一处理；
    轮换/迁移入口可单独捕获本类，分清“KEK 问题”与“旧密文损坏”。
    """


def source_credentials_enabled() -> bool:
    """源级凭据表优先读是否开启（默认关）。"""
    return env_int(SOURCE_CREDENTIALS_ENABLED_ENV, 0, lo=0, hi=1) == 1


# ---------------------------------------------------------------- KEK 文件

#: KEK 缓存：key_id -> (mtime_ns, size, Fernet 密钥字节)。文件被替换
#: （mtime/size 变）即失效重读，轮换后不读旧密钥；进程内不缓存
#: 明文凭据，只缓存 KEK 材料（仍只在内存，不进日志）。
_KEK_CACHE: dict[str, tuple[int, int, bytes]] = {}


def clear_kek_cache() -> None:
    """清空 KEK 缓存（测试与轮换后显式刷新用，不碰磁盘）。"""
    _KEK_CACHE.clear()


def kek_file_path(kek_dir: str, key_id: str) -> Path:
    """key_id → KEK 文件路径（``<key_id>.b64``，同款命名法）。

    key_id 非法、含路径成分时直接拒绝，不做任何规范化猜测。
    """
    if not _KEY_ID_RE.match(key_id or "") or ".." in key_id:
        raise KEKError("KEK key_id 非法（只许字母数字与 _ . -，不许路径成分）")
    return Path(kek_dir) / f"{key_id}.b64"


def load_kek(key_id: str) -> bytes:
    """加载一把 KEK（返回 Fernet 密钥字节）。

    fail-closed 清单：目录未配 → 错；文件不存在/不是常规文件/
    是符号链接 → 错；权限不是 0600 → 错；内容不是合法 Fernet
    密钥 → 错。任何一步的报错里都不带文件内容与目录以外的信息。
    """
    kek_dir = resolve_kek_dir()
    if not kek_dir:
        raise KEKError("KEK 目录未配置（MOUNT_KEK_DIR 为空），KEK 信封不可用")
    path = kek_file_path(kek_dir, key_id)
    try:
        st = path.lstat()
    except OSError as exc:
        raise KEKError("KEK 文件不可读") from exc
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        raise KEKError("KEK 文件不是常规文件（拒绝符号链接与特殊文件）")
    if stat.S_IMODE(st.st_mode) != 0o600:
        raise KEKError("KEK 文件权限不是 0600，拒绝加载")
    cached = _KEK_CACHE.get(key_id)
    if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
        return cached[2]
    try:
        content = path.read_bytes().decode("ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise KEKError("KEK 文件不可读") from exc
    try:
        from cryptography.fernet import Fernet

        Fernet(content.encode("ascii"))
    except Exception as exc:  # noqa: BLE001 — 非法密钥只有“非法”这一个结论
        raise KEKError("KEK 文件内容不是合法 Fernet 密钥") from exc
    key = content.encode("ascii")
    _KEK_CACHE[key_id] = (st.st_mtime_ns, st.st_size, key)
    return key


def seal_with_kek(plaintext: str, key_id: str) -> str:
    """用指定 KEK 密封明文，返回 ``kek-v1:<token>``（不含 key_id，
    key_id 存在行上，与密文组成信封）。"""
    if not plaintext:
        raise ValueError("KEK 密封不接受空明文（空凭据不建行，见 upsert）")
    from cryptography.fernet import Fernet

    token = Fernet(load_kek(key_id)).encrypt(plaintext.encode("utf-8"))
    return KEK_SEAL_PREFIX + token.decode("ascii")


def unseal_with_kek(sealed: str, key_id: str) -> str:
    """用指定 KEK 解开 ``kek-v1:`` 密封值。key_id 缺失/KEK 不可用/
    解不开时一律 SealedSecretError（不猜文件名、不回落旧密钥）。"""
    if not sealed or not sealed.startswith(KEK_SEAL_PREFIX):
        raise ups.SealedSecretError("密封凭据格式不正确（非 KEK 密封值）")
    if not key_id:
        raise ups.SealedSecretError("KEK 密封行缺少 key_id，无法定位密钥文件")
    try:
        key = load_kek(key_id)
    except KEKError as exc:
        raise ups.SealedSecretError("KEK 不可用，源级凭据解不开") from exc
    try:
        from cryptography.fernet import Fernet, InvalidToken

        raw = Fernet(key).decrypt(sealed[len(KEK_SEAL_PREFIX):].encode("ascii"))
    except (InvalidToken, ValueError) as exc:
        raise ups.SealedSecretError("KEK 密封凭据解不开（密钥已换或值已损坏）") from exc
    return raw.decode("utf-8")


# ---------------------------------------------------------------- 行级读写

def get_source_credential_row(db, mount_id) -> em.MountCredential | None:
    """某挂载的源级凭据行（scope=source）。mount_id 为空时直接回
    None——绝不把 mount_id=NULL 的用户绑定行误当成源级行。"""
    if mount_id is None:
        return None
    return (
        db.query(em.MountCredential)
        .filter(em.MountCredential.mount_id == mount_id,
                em.MountCredential.scope == "source")
        .first()
    )


def unseal_credential_row(row: em.MountCredential) -> str:
    """按行上的 key_id/前缀分发解密封（双读核心）。

    - ``kek-v1:`` → KEK 信封（key_id 定位文件）；
    - ``fernet-v1:`` → 25-1 旧密封（SECRET_KEY 派生），旧行永可读；
    - 其他/空 → SealedSecretError（响亮，不猜、不回落）。
    """
    sealed = (row.sealed_credential or "")
    if not sealed:
        raise ups.SealedSecretError("源级凭据行密封值为空")
    if sealed.startswith(KEK_SEAL_PREFIX):
        return unseal_with_kek(sealed, row.key_id or "")
    if sealed.startswith(LEGACY_SEAL_PREFIX):
        return ups.unseal_secret(sealed)
    raise ups.SealedSecretError("源级凭据行密封格式未知（非本站密封值）")


def load_source_credential(db, mount_id) -> str | None:
    """读某挂载的源级凭据明文。

    - 无行 / 空密封 → None（调用方回落旧路径并记警告）；
    - 有行 → 解密封；解不开时抛 SealedSecretError（调用方响亮报错，
      **不**回落旧路径——表里有但解不开与表里没有是两回事）。
    行状态非 active 时仍返回值并记警告（状态语义归维护工人，
    本步不替它拦；警告里只有状态词，没有凭据内容）。
    """
    row = get_source_credential_row(db, mount_id)
    if row is None or not (row.sealed_credential or ""):
        return None
    if (row.status or "active") != "active":
        logger.warning("挂载 id=%s 源级凭据行状态为 %s，仍返回其凭据（状态语义归维护工人）",
                       mount_id, row.status)
    return unseal_credential_row(row)


def seal_source_credential(plaintext: str, *, key_id: str | None = None) -> tuple[str, str]:
    """密封一份源级凭据，返回 ``(密封值, key_id)``。

    - key_id 为空 → 25-1 旧密封（默认，现有行为逐字不变）；
    - key_id 给定 → KEK 信封（先校验 KEK 可用，不可用即错、零写入）。
    """
    if not plaintext:
        raise ValueError("源级凭据不接受空明文")
    if key_id is None:
        return ups.seal_secret(plaintext), LEGACY_KEY_ID
    return seal_with_kek(plaintext, key_id), key_id


def upsert_source_credential(db, mount_id, plaintext: str, *,
                             key_id: str | None = None) -> em.MountCredential:
    """新建或更新某挂载的源级凭据行（只 flush、不 commit，调用方提交）。

    已有行 → 只更新密封值/key_id/状态（不删行、不改 mount_id 与
    scope）；无行 → 新建 active 行。owner_user_id 恒为空（源级行
    不归属任何用户，与用户绑定行泾渭分明）。
    """
    if mount_id is None:
        raise ValueError("源级凭据行必须附属挂载（mount_id 不许为空）")
    sealed, key_id_out = seal_source_credential(plaintext, key_id=key_id)
    row = get_source_credential_row(db, mount_id)
    if row is None:
        row = em.MountCredential(mount_id=mount_id, scope="source",
                                 owner_user_id=None, sealed_credential=sealed,
                                 key_id=key_id_out, status="active")
        db.add(row)
    else:
        row.sealed_credential = sealed
        row.key_id = key_id_out
        row.status = "active"
        row.last_error_kind = None
    db.flush()
    return row


# ---------------------------------------------------------------- 迁移（快照语义）

def _mount_config(mount) -> dict:
    """挂载 config 列（JSON 文本或 dict）→ dict；解析失败回空 dict
    （该挂载随后按“无显式凭据”跳过，不炸迁移整轮）。"""
    raw = getattr(mount, "config", "") or ""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _snapshot_legacy_credential(db, mount) -> tuple[str | None, str]:
    """某挂载的旧路径有效凭据快照（只读旧路径，不写表）。

    返回 ``(明文或 None, 跳过原因或 "")``。快照语义必须写清：
    迁移只把“迁移这一刻旧路径算出的那一份”封进表，之后旧路径
    再改（账号档换 Cookie、环境变量换值）不会自动同步到表里——
    要同步须重跑迁移（已有行跳过，须先由管理员删行或调 upsert），
    或保持旗标关闭继续走旧路径。这是快照迁移的已知边界，不是 bug。
    - 115：只迁挂载自带的显式凭据（config account_id / config
      cookie）；走库绑定/默认账号/环境链的挂载不做单挂载快照
      （正确凭据随库而变，一行装不下），跳过并记原因；
    - drive：迁 SA 引用（SA 目录路径，文件本身不动，drive_auth
      取 token 路径本步不动）；
    - rclone：迁 conf 引用（该挂载的 rclone.conf 路径，RC 账号
      密码是服务器基础设施，不进表）；
    - 其余类型：无源级凭据可迁，跳过。
    返回的明文只在迁移函数内流转，进密封后即丢弃，不进日志。
    """
    mount_type = (getattr(mount, "mount_type", "") or "").strip()
    config = _mount_config(mount)
    if mount_type == "115":
        account_id = config.get("account_id")
        try:
            account_id = int(account_id) if account_id not in (None, "", "0") else None
        except (TypeError, ValueError):
            account_id = None
        if account_id is not None:
            account = db.query(em.Pan115Account).filter(
                em.Pan115Account.id == account_id).first()
            if account is None or not getattr(account, "is_enabled", True):
                return None, "115 账号档不存在或已停用"
            from backend.emby_server.transfer115 import normalize_cookie

            cookie = normalize_cookie(account.cookie or "")
            if not cookie:
                return None, "115 账号档凭据为空"
            return cookie, ""
        from backend.emby_server.transfer115 import normalize_cookie

        cookie = normalize_cookie(config.get("cookie") or "")
        if cookie:
            return cookie, ""
        return None, ("115 挂载无显式账号/配置凭据，凭据随库绑定/默认/环境链而变，"
                      "不做单挂载快照（保持旧路径）")
    if mount_type == "drive":
        from backend.emby_server import drive_changes

        ref = (drive_changes.SA_DIR or "").strip()
        return json.dumps({"kind": "drive_sa_ref", "ref": ref},
                          ensure_ascii=False, sort_keys=True), ""
    if mount_type == "rclone":
        from backend.emby_server import mount_rclone

        ref = ((config.get("rclone_config") or "").strip()
               or mount_rclone.conf_path_for_server(getattr(mount, "server_id", None)))
        return json.dumps({"kind": "rclone_conf_ref", "ref": ref},
                          ensure_ascii=False, sort_keys=True), ""
    return None, f"挂载类型 {mount_type or '?'} 无源级凭据可迁"


def migrate_source_credentials(db, *, key_id: str | None = None,
                               mount_ids=None, dry_run: bool = False) -> dict:
    """把源级凭据快照迁入 ``mount_credentials``（幂等，可重跑）。

    - key_id 给定 → KEK 密封（先校验 KEK 可用，不可用整轮零写入）；
      不给 → 25-1 旧密封（与现行用户域行同一形态）。
    - 已有源级行的挂载一律跳过：**不覆盖、不重加密**（向后兼容
      硬承诺；要换密封须走轮换函数，不要删行重迁）。
    - dry_run 时只计数、不写不提交；非 dry_run 时整轮结束提交一次。
    返回 ``{migrated, existing, skipped, dry_run, reasons}``：reasons
    只含挂载 id/类型/跳过原因，不含任何凭据内容。
    """
    if key_id is not None:
        load_kek(key_id)  # 先校验：KEK 不可用则整轮零写入
    mounts = db.query(em.StorageMount).order_by(em.StorageMount.id).all()
    if mount_ids is not None:
        wanted = set(mount_ids)
        mounts = [m for m in mounts if m.id in wanted]
    stats: dict = {"migrated": 0, "existing": 0, "skipped": 0,
                   "dry_run": bool(dry_run), "reasons": []}
    for mount in mounts:
        if get_source_credential_row(db, mount.id) is not None:
            stats["existing"] += 1
            stats["reasons"].append(
                {"mount_id": mount.id, "mount_type": mount.mount_type,
                 "result": "existing",
                 "reason": "已有源级行，不覆盖、不重加密"})
            continue
        plaintext, reason = _snapshot_legacy_credential(db, mount)
        if plaintext is None:
            stats["skipped"] += 1
            stats["reasons"].append(
                {"mount_id": mount.id, "mount_type": mount.mount_type,
                 "result": "skipped", "reason": reason})
            continue
        if dry_run:
            stats["migrated"] += 1
            stats["reasons"].append(
                {"mount_id": mount.id, "mount_type": mount.mount_type,
                 "result": "would_migrate", "reason": ""})
            continue
        upsert_source_credential(db, mount.id, plaintext, key_id=key_id)
        stats["migrated"] += 1
        stats["reasons"].append(
            {"mount_id": mount.id, "mount_type": mount.mount_type,
             "result": "migrated", "reason": ""})
    if not dry_run:
        db.commit()
    return stats


# ---------------------------------------------------------------- 轮换（挂载-6）

def rotate_source_credentials(db, *, new_key_id: str, mount_ids=None,
                              keep_mount_ids=(), dry_run: bool = False) -> dict:
    """源级凭据轮换：新 key_id + 旧密文重封（挂载-6 照抄机制）。

    - 先校验新 KEK 可用：不可用则整轮零写入（fail-closed）。
    - 逐行先解旧（旧行可能是 fernet-v1 或旧 KEK）：解不开的行跳过
      不写（行保持原样，绝不把解不出的行写成坏密文）；旧 KEK 文件
      已删的行同样跳过——双读期旧文件必须留着。
    - ``keep_mount_ids`` 里的挂载单独保留旧密钥（按挂载回落），
      记警告（双读期可见）；其余行重封为新 key_id。
    - dry_run 时只计数、不写不提交；非 dry_run 时整轮结束提交一次。
    - 轮换周期暂定 90 天（策略，不进代码常量：到期的判断与调度归
      维护工人，本函数只做重封这一件事）。
    返回 ``{rotated, kept, failed, dry_run, reasons}``（reasons 不含
    凭据内容；failed 行的旧 key_id 只记标识，不记密钥材料）。
    """
    load_kek(new_key_id)  # 先校验：新 KEK 不可用则整轮零写入
    kept = set(keep_mount_ids or ())
    rows = (db.query(em.MountCredential)
            .filter(em.MountCredential.scope == "source",
                    em.MountCredential.mount_id.isnot(None))
            .order_by(em.MountCredential.id).all())
    if mount_ids is not None:
        wanted = set(mount_ids)
        rows = [r for r in rows if r.mount_id in wanted]
    stats: dict = {"rotated": 0, "kept": 0, "failed": 0,
                   "dry_run": bool(dry_run), "reasons": []}
    for row in rows:
        if row.mount_id in kept:
            stats["kept"] += 1
            logger.warning("挂载 id=%s 轮换保留旧 key_id（双读期，旧 KEK 文件暂勿删除）",
                           row.mount_id)
            stats["reasons"].append(
                {"mount_id": row.mount_id, "result": "kept",
                 "reason": "按挂载保留旧密钥"})
            continue
        try:
            plaintext = unseal_credential_row(row)
        except ups.SealedSecretError:
            stats["failed"] += 1
            logger.warning("挂载 id=%s 旧密文解不开，轮换跳过不写（行保持原样）",
                           row.mount_id)
            stats["reasons"].append(
                {"mount_id": row.mount_id, "result": "failed",
                 "reason": "旧密文解不开"})
            continue
        if dry_run:
            stats["rotated"] += 1
            stats["reasons"].append(
                {"mount_id": row.mount_id, "result": "would_rotate", "reason": ""})
            continue
        row.sealed_credential = seal_with_kek(plaintext, new_key_id)
        row.key_id = new_key_id
        stats["rotated"] += 1
        stats["reasons"].append(
            {"mount_id": row.mount_id, "result": "rotated", "reason": ""})
    if not dry_run:
        db.commit()
    return stats
