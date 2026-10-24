# -*- coding: utf-8 -*-
"""挂载模型地基（FMBY 挂载学习，任务清单第 1 项）。

只做四件事，多一件不做：
1. 新表读写：``library_sources`` 来源行、``storage_mounts`` 五能力位列、
   ``mount_credentials`` 凭据行（见 ``models``）。
2. 回填：从现行 ``Library.paths`` / ``mount_ids`` 派生来源行。
3. 双读校验：新表读数与现行 ``mounts.library_sources()`` 输出逐项比对，
   差异只报不拦。
4. 凭据密封：复用 25-1 ``seal_secret``；KEK 目录只从配置读。

**权威切换（清单第 2 项）**：来源解析的权威从旧列切到新表——扫描、
追新、探针、监听、Drive Changes 与管理端保存一律经本模块读/写新表；
新表无行或读表失败时显式记警告并回落旧 ``paths`` / ``mount_ids``
解析（回落不是静默失败）；旧列在双写期由反向同步维持，供旧代码与
回滚读。能力门控在本模块的读取里落地（``can_list``）；凭据迁移是
第 4 项、断旧路是第 20 项，均另开 PR，本模块不许提前做其中任何一项。

命名注意：``models.LibrarySource``（落库 ORM 行）与
``mounts.LibrarySource``（扫描期 dataclass）同名不同物；本模块一律
用 ``em.LibrarySource`` 限定引用 ORM 行，不裸 import。
"""

from __future__ import annotations

import logging
import os
from types import SimpleNamespace

from backend.emby_server import models as em
from backend.emby_server import upstream_sources as ups
from backend.emby_server.mounts import (
    build_library_sources,
)
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


# ---------------------------------------------------------------- 来源切表（清单第 2 项）

def new_table_rows(db, library) -> list[dict] | None:
    """新表来源行（无回落）：有行时回行列表，无行 / 读表失败 /
    db 为 None 时回 None 并记警告——调用方回落旧列解析。

    返回 ``[{mount_id, sub_path, is_enabled}]``（新表 id 顺序）。
    需要“回落即派生”的调用方用 ``read_source_rows``。
    """
    if db is None:
        logger.warning("库 id=%s 无 db 会话，来源行回落旧 paths/mount_ids",
                       getattr(library, "id", "?"))
        return None
    try:
        rows = db.query(em.LibrarySource).filter(
            em.LibrarySource.library_id == library.id).order_by(
            em.LibrarySource.id).all()
    except Exception as exc:  # noqa: BLE001 — 表缺失/旧夹具都回落旧配置
        logger.warning("库 id=%s 新表来源行读取失败，回落旧 paths/mount_ids：%s",
                       getattr(library, "id", "?"), exc)
        return None
    if not rows:
        logger.warning("库 id=%s 新表无来源行，回落旧 paths/mount_ids（回填未跑或已回滚）",
                       getattr(library, "id", "?"))
        return None
    return [{"mount_id": row.mount_id, "sub_path": row.sub_path,
             "is_enabled": bool(getattr(row, "is_enabled", True))}
            for row in rows]


def read_source_rows(db, library) -> list[dict]:
    """库的来源行（新表优先，旧配置派生回落）。

    返回 ``[{mount_id, sub_path, is_enabled}]``（行顺序 = 新表 id 顺序，
    或派生顺序）。新表无行、读表失败、db 为 None 时，一律回落旧
    ``paths`` / ``mount_ids`` 派生（``derive_source_rows``，子目录胜出
    口径与旧解析逐字一致）并记警告——回落是显式的，不是静默失败。
    追新 / 探针 / 监听 / Drive Changes 的来源枚举走本函数，不许各自
    再拆一次旧列。
    """
    rows = new_table_rows(db, library)
    if rows is not None:
        return rows
    derived, warnings = derive_source_rows(library)
    for warning in warnings:
        logger.warning(warning)
    return [{"mount_id": row["mount_id"], "sub_path": row["sub_path"],
             "is_enabled": True} for row in derived]


def read_library_sources(db, library):
    """切表后的扫描来源解析唯一入口（清单第 2 项）。

    新表有行 → 按行建来源（本机行 / 挂载子目录行 / 挂载根行），经
    ``build_library_sources(enforce_can_list=True)`` 做可用性判定与
    能力门控：``can_list=False`` 的挂载记 failed、不进扫描；
    ``is_enabled=False`` 的行同样记 failed（停用来源的条目不许被
    当成“已删除”清理掉，与旧 failed 口径一致）。
    新表无行或读表失败 → 记警告后回落旧 ``library_sources()``
    （旧列解析，不执法门控）——回落是显式的，不是静默改走别源。
    输出顺序与旧解析逐字一致：本机行、挂载子目录行、挂载根行。
    """
    try:
        rows = db.query(em.LibrarySource).filter(
            em.LibrarySource.library_id == library.id).order_by(
            em.LibrarySource.id).all()
    except Exception as exc:  # noqa: BLE001 — 表缺失即回落旧解析
        logger.warning("库 id=%s 新表来源行读取失败，回落旧 paths/mount_ids：%s",
                       getattr(library, "id", "?"), exc)
        return legacy_library_sources(library, db)
    if not rows:
        logger.warning("库 id=%s 新表无来源行，回落旧 paths/mount_ids（回填未跑或已回滚）",
                       getattr(library, "id", "?"))
        return legacy_library_sources(library, db)
    local_paths: list[str] = []
    mount_subpaths: list[tuple[str, int, str]] = []
    mount_ids: list[int] = []
    failed: list[dict] = []
    for row in rows:
        if not getattr(row, "is_enabled", True):
            label = row.sub_path if row.mount_id is None else f"mount://{row.mount_id}{row.sub_path}"
            failed.append({"label": label, "reason": "来源已在新表停用（is_enabled=False），本轮跳过"})
            continue
        if row.mount_id is None:
            local_paths.append(row.sub_path)
        elif (row.sub_path or "/") == "/":
            mount_ids.append(row.mount_id)
        else:
            mount_subpaths.append((f"mount://{row.mount_id}{row.sub_path}",
                                   row.mount_id, row.sub_path))
    sources, built_failed = build_library_sources(
        local_paths, mount_subpaths, mount_ids, db, library,
        enforce_can_list=True)
    # 身份锚（清单第 8 项）：把新表行 id 挂到扫描来源上，供入库写 item.source_id。
    # 只按启用行建表（停用行不在 sources 里，不会误挂）；旧列回落路径（legacy
    # _library_sources）不走这里，来源的 source_row_id 保持 None。
    row_ids: dict = {}
    for row in rows:
        if not getattr(row, "is_enabled", True):
            continue
        if row.mount_id is None:
            row_ids.setdefault(("local", row.sub_path), row.id)
        else:
            row_ids.setdefault(("mount", row.mount_id, row.sub_path or "/"),
                               row.id)
    for src in sources:
        if src.kind == "local":
            src.source_row_id = row_ids.get(("local", src.path))
        elif src.mount is not None:
            src.source_row_id = row_ids.get(
                ("mount", src.mount.id, src.subpath or "/"))
    return sources, failed + built_failed


def reverse_sync_legacy_columns(rows, submitted_paths) -> tuple[str, str]:
    """来源行 + 提交顺序 → 旧列（paths, mount_ids）字符串（双写期反向同步）。

    paths 列 = 提交顺序原样（不重排：管理端回显与旧存储顺序一致，
    重排会打乱界面顺序）；mount_ids 列 = 派生后存活的根行（子目录胜出
    被过滤掉的根行不再出现）。旧解析在同步后的列上复原同一来源集合
    （输出顺序仍按旧规则：本机、子目录、整挂载，与字符串顺序无关）。
    """
    root_ids = [str(row["mount_id"]) for row in rows
                if row["mount_id"] is not None and (row["sub_path"] or "/") == "/"]
    return ",".join(submitted_paths), ",".join(root_ids)


def write_library_sources(db, library, paths, mount_ids) -> dict:
    """管理端保存库：只写新表，旧列反向同步（清单第 2 项，双写期）。

    - ``paths`` / ``mount_ids`` 是提交的旧形态值（调用方已校验）；
    - 按 ``derive_source_rows`` 口径派生行（子目录胜出、危险组合只记
      警告），删掉该库全部旧行后重插；
    - 旧 ``paths`` / ``mount_ids`` 列由 ``reverse_sync_legacy_columns``
      回写，供旧代码与回滚读——旧列不再是权威，只是双写期的影子。
    调用方须保证 ``library.id`` 已落库（新建库先 flush），写完后提交。
    返回 ``{rows, warnings, paths, mount_ids}``。
    """
    shim = SimpleNamespace(paths=",".join(paths),
                           mount_ids=",".join(str(i) for i in mount_ids),
                           name=getattr(library, "name", None),
                           id=getattr(library, "id", None))
    derived, warnings = derive_source_rows(shim)
    for warning in warnings:
        logger.warning(warning)
    db.query(em.LibrarySource).filter(
        em.LibrarySource.library_id == library.id).delete()
    for row in derived:
        db.add(em.LibrarySource(library_id=library.id,
                                mount_id=row["mount_id"],
                                sub_path=row["sub_path"]))
    legacy_paths, legacy_mount_ids = reverse_sync_legacy_columns(derived, paths)
    library.paths = legacy_paths
    library.mount_ids = legacy_mount_ids
    return {"rows": len(derived), "warnings": warnings,
            "paths": legacy_paths, "mount_ids": legacy_mount_ids}
