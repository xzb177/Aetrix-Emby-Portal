# -*- coding: utf-8 -*-
"""挂载模型地基（FMBY 挂载学习，任务清单第 1 项）。

只做四件事，多一件不做：
1. 新表读写：``library_sources`` 来源行、``storage_mounts`` 五能力位列、
   ``mount_credentials`` 凭据行（见 ``models``）。
2. 回填：从现行 ``Library.paths`` / ``mount_ids`` 派生来源行。
3. 双读校验：新表读数与现行 ``mounts.library_sources()`` 输出逐项比对，
   差异只报不拦。
4. 凭据密封：复用 25-1 ``seal_secret``；KEK 目录只从配置读。

**非权威声明**：本模块不改变任何消费方行为。开关默认关（本模块
根本没有开关——它只是不被任何热路径调用）；旧 ``paths`` /
``mount_ids`` 列仍是唯一权威，来源解析、扫描、播放一律不读新表。
能力门控是清单第 2 项、凭据迁移是第 4 项、断旧路是第 20 项，
均另开 PR，本模块不许提前做其中任何一项。

命名注意：``models.LibrarySource``（落库 ORM 行）与
``mounts.LibrarySource``（扫描期 dataclass）同名不同物；本模块一律
用 ``em.LibrarySource`` 限定引用 ORM 行，不裸 import。
"""

from __future__ import annotations

import logging
import os

from backend.emby_server import models as em
from backend.emby_server import upstream_sources as ups
from backend.emby_server.mounts import (
    library_sources as legacy_library_sources,
)
from backend.emby_server.mounts import (
    parse_mount_ids,
    parse_mount_path,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- KEK 配置

#: KEK 目录的环境键。只从部署配置读，代码里**不设默认值**：未设置时
#: ``resolve_kek_dir()`` 回 None，表示 KEK 信封不可用（地基阶段不做
#: KEK 信封操作，该能力是清单第 4 步）。KEK 材料本身绝不进仓库。
MOUNT_KEK_DIR_ENV = "MOUNT_KEK_DIR"

#: 地基阶段 key_id 的取值：25-1 密封方案标识（``_SEAL_PREFIX`` 去冒号）。
#: KEK 文件信封（key_id → MOUNT_KEK_DIR 下密钥文件）是第 4 步，届时
#: key_id 改为 KEK 文件编号，本常量只标记“本行由 25-1 方案密封”。
SEAL_KEY_ID = ups._SEAL_PREFIX.rstrip(":")


def resolve_kek_dir() -> str | None:
    """KEK 目录（部署配置）。未设置回 None，不设代码默认值。"""
    value = (os.environ.get(MOUNT_KEK_DIR_ENV) or "").strip()
    return value or None


def seal_mount_credential(plaintext: str) -> tuple[str, str]:
    """密封一份挂载凭据，返回 ``(密封值, key_id)``。

    密封复用 25-1 ``seal_secret``（Fernet，SECRET_KEY 派生）；原文
    绝不进日志、不进返回值之外的任何地方。
    """
    return ups.seal_secret(plaintext), SEAL_KEY_ID


def unseal_mount_credential(sealed: str) -> str:
    """解开一份挂载凭据。损坏/密钥已换 → ``SealedSecretError``（响亮）。"""
    return ups.unseal_secret(sealed)


# ---------------------------------------------------------------- 能力声明派生

#: 按挂载类型派生的五能力位（与各 provider 的 ``capabilities()`` 声明
#: 逐字一致，见 mounts.py / mount_drive.py / mount_rclone.py）。
#: 未知类型一律全 False + 警告，不静默当全 True。
CAPABILITY_BY_MOUNT_TYPE: dict[str, dict[str, bool]] = {
    "local": {
        "can_list": True, "can_random_read": True, "can_read_sidecar": True,
        "can_generate_play_target": True, "can_refresh_credentials": False,
    },
    "115": {
        "can_list": True, "can_random_read": True, "can_read_sidecar": True,
        "can_generate_play_target": True, "can_refresh_credentials": False,
    },
    "drive": {
        "can_list": True, "can_random_read": True, "can_read_sidecar": True,
        "can_generate_play_target": True, "can_refresh_credentials": True,
    },
    "rclone": {
        "can_list": True, "can_random_read": True, "can_read_sidecar": True,
        "can_generate_play_target": True, "can_refresh_credentials": False,
    },
}

_CAPABILITY_COLUMNS = (
    "can_list", "can_random_read", "can_read_sidecar",
    "can_generate_play_target", "can_refresh_credentials",
)


def capabilities_for_mount_type(mount_type: str) -> dict[str, bool]:
    """某挂载类型的五能力位声明；未知类型全 False。"""
    caps = CAPABILITY_BY_MOUNT_TYPE.get(mount_type or "")
    if caps is None:
        logger.warning("未知挂载类型 %r：能力位全置 False，不执法", mount_type)
        return {name: False for name in _CAPABILITY_COLUMNS}
    return dict(caps)


# ---------------------------------------------------------------- 派生与回填

def derive_source_rows(library) -> tuple[list[dict], list[str]]:
    """从旧配置派生来源行（纯解析，不碰磁盘、不碰网络、不建 provider）。

    派生口径与现行 ``mounts.library_sources()`` 的过滤逐字一致：
    ``paths`` 里的 ``mount://<id>/<子目录>`` → 子目录行；``mount_ids``
    里与子目录同挂载的整挂载来源被过滤掉（2026-09-26 演唱会事故
    教训）；其余 ``mount_ids`` → 根行（sub_path='/'）；其余 paths
    条目 → 本机行（mount_id=None，sub_path=原路径）。

    返回 ``(行, 警告)``：危险组合（子目录与整挂载并存）只记警告，
    不中断、不改旧配置——旧配置仍是权威。
    """
    rows: list[dict] = []
    warnings: list[str] = []
    seen: set[tuple[object, str]] = set()

    def _add(mount_id, sub_path: str) -> None:
        key = (mount_id, sub_path)
        if key in seen:
            return
        seen.add(key)
        rows.append({"mount_id": mount_id, "sub_path": sub_path})

    subpath_mount_ids: set[int] = set()
    local_paths: list[str] = []
    for raw in (getattr(library, "paths", "") or "").split(","):
        path = raw.strip()
        if not path:
            continue
        parsed = parse_mount_path(path)
        if parsed is not None:
            subpath_mount_ids.add(parsed[0])
            _add(parsed[0], parsed[1])
        else:
            local_paths.append(path)

    mount_ids = parse_mount_ids(library)
    dangerous = [mid for mid in mount_ids if mid in subpath_mount_ids]
    if dangerous:
        warnings.append(
            f"库「{getattr(library, 'name', library.id)}」"
            f"（id={getattr(library, 'id', '?')}）paths 子目录与 "
            f"mount_ids 同挂载并存（挂载 {dangerous}）：回填只留子目录行，"
            f"旧配置不动（2026-09-26 演唱会事故教训）"
        )
    for mount_id in mount_ids:
        if mount_id not in subpath_mount_ids:
            _add(mount_id, "/")
    for path in local_paths:
        _add(None, path)
    return rows, warnings


def backfill_library_sources(db, library=None) -> dict:
    """回填来源行 + 能力位列（幂等，可重跑）。

    - 只插新表 ``library_sources``、只写 ``storage_mounts`` 五能力位列；
      ``Library.paths`` / ``mount_ids`` 只读不写。
    - 已存在的行（library_id, mount_id, sub_path）跳过不重插。
    - 能力位列按 mount_type 派生覆写（派生值，不是用户手填）。
    - 危险组合只记警告（见 ``derive_source_rows``），不中断。
    返回统计：``{libraries, rows_written, rows_skipped, warnings}``。
    """
    if library is not None:
        libraries = [library]
    else:
        libraries = db.query(em.Library).order_by(em.Library.id).all()
    stats = {"libraries": 0, "rows_written": 0, "rows_skipped": 0,
             "warnings": []}
    for lib in libraries:
        stats["libraries"] += 1
        rows, warnings = derive_source_rows(lib)
        for warning in warnings:
            logger.warning(warning)
            stats["warnings"].append(warning)
        existing = {
            (row.mount_id, row.sub_path)
            for row in db.query(em.LibrarySource).filter(
                em.LibrarySource.library_id == lib.id).all()
        }
        for row in rows:
            if (row["mount_id"], row["sub_path"]) in existing:
                stats["rows_skipped"] += 1
                continue
            db.add(em.LibrarySource(
                library_id=lib.id, mount_id=row["mount_id"],
                sub_path=row["sub_path"]))
            stats["rows_written"] += 1
        db.commit()
    # 能力位列按 mount_type 派生覆写（派生值，不是用户手填），与库数
    # 无关：库为零时也必须跑，否则空库实例的能力列永远补不上。
    for mount in db.query(em.StorageMount).all():
        caps = capabilities_for_mount_type(mount.mount_type)
        for name in _CAPABILITY_COLUMNS:
            setattr(mount, name, caps[name])
    db.commit()
    return stats


# ---------------------------------------------------------------- 双读校验

def _old_source_keys(sources) -> set[tuple[str, object, str]]:
    """现行 ``library_sources()`` 输出的来源键集合。

    mount 行 → ("mount", mount_id, subpath)；local 行 → ("local", None, path)。
    """
    keys: set[tuple[str, object, str]] = set()
    for src in sources:
        if src.kind == "mount" and src.mount is not None:
            keys.add(("mount", src.mount.id, src.subpath))
        elif src.kind == "local":
            keys.add(("local", None, src.path))
    return keys


def _new_source_keys(db, library_id: int) -> set[tuple[str, object, str]]:
    """新表来源行的键集合（与旧侧同一键形）。"""
    keys: set[tuple[str, object, str]] = set()
    for row in db.query(em.LibrarySource).filter(
            em.LibrarySource.library_id == library_id).all():
        if row.mount_id is not None:
            keys.add(("mount", row.mount_id, row.sub_path))
        else:
            keys.add(("local", None, row.sub_path))
    return keys


def compare_library_sources(db, library) -> dict:
    """新表读数与现行 ``library_sources()`` 输出逐项比对（只报不拦）。

    返回 ``{matched, only_new, only_old, failed, warnings}``：
    - matched：两边都有的来源键；
    - only_new：只在新表里的行（含旧侧因不可用被记 failed 的对应行，
      可用 failed 的 reason 关联）；
    - only_old：只在旧输出里的来源键；
    - failed：旧侧不可用来源（路径不存在 / 挂载不存在 / 已停用 /
      子目录不可读），新表不复制可用性判断，只如实列出。
    差异永不抛异常、不拦任何流程；旧侧调用本身异常时回
    ``{"error": ...}``（同样只报不拦）。
    """
    try:
        sources, failed = legacy_library_sources(library, db)
    except Exception as exc:  # noqa: BLE001 — 校验脚本不许炸调用方
        logger.warning("双读校验旧侧调用失败（库 id=%s）：%s",
                       getattr(library, "id", "?"), exc)
        return {"matched": set(), "only_new": set(), "only_old": set(),
                "failed": [], "warnings": [f"旧侧调用失败：{exc}"],
                "error": str(exc)}
    old_keys = _old_source_keys(sources)
    new_keys = _new_source_keys(db, library.id)
    return {
        "matched": old_keys & new_keys,
        "only_new": new_keys - old_keys,
        "only_old": old_keys - new_keys,
        "failed": failed,
        "warnings": [],
    }
