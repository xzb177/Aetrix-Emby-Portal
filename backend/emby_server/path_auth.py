"""三层路径授权判定单点（FMBY 挂载-7 照抄，清单第 15 项）。

FMBY 三层叠加（照抄定稿挂载-7：三层全做，不后置）：
- 角色模板级：``role_templates`` + ``role_template_source_path_grants``
- 用户级：``user_source_path_grants``
- 库级：``library_access_grants``

判定语义（本模块唯一口径，不许各处散写）：
- 只对 ``mount://`` 路径条目做判定；非挂载路径（本机文件等）不在
  授权域内，直接放行。
- 三层里对该挂载有行的层才构成约束；三层全无行 → 放行（与现行
  逐字一致：默认放行面不变，收紧只走显式授权）。
- 有行的层必须全部通过：条目相对路径须落在该层至少一个授权前缀
  之下（前缀匹配与 stream_limits 同口径：根 "/" 全匹配，其余
  相等或 ``prefix + "/"`` 前缀）。任一有行层不通过 → 拒绝。
- 库层取条目所属库（``MediaItem.library_id``），不是请求参数。
- DB 异常时 fail-closed（拒绝并记警告）：授权是门禁，库抖一下就
  全放行是本末倒置；与 stream_limits 的 fail-open 区分开（限流是
  护栏、授权是门禁，两者取舍本来就不一样）。

本 PR 只落判定函数 + 表 + 测试，**不接播放请求链路**（与 #1227
总线骨架 → #1229 接线同一打法：先骨架、后接线）。接线是后续项：
播放入口调 :func:`check_path_access`，拒绝时按 403 + 人话原因
返回；接线前生产行为逐字不变。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

#: 种子角色三档（照抄 FMBY 角色种子）。
SEED_TEMPLATES = ("User", "RestrictedUser", "Admin")


def normalize_prefix(raw: object) -> Optional[str]:
    """前缀归一化：与 stream_limits._normalize_prefix 同口径。

    带前导 /、无尾随 /（根为 "/"）；空 / 非字符串 → None。
    """
    if not isinstance(raw, str):
        return None
    prefix = raw.strip()
    if not prefix:
        return None
    if not prefix.startswith("/"):
        prefix = "/" + prefix
    if len(prefix) > 1:
        prefix = prefix.rstrip("/")
    return prefix or "/"


def under_prefix(prefix: str, rel: str) -> bool:
    """rel 是否落在 prefix 之下（与 stream_limits._prefix_matches 同口径）。"""
    if prefix == "/":
        return True
    return rel == prefix or rel.startswith(prefix + "/")


def ensure_seed_templates(db) -> dict:
    """幂等建种子模板（按名查已存在再插，不存在才建）。

    返回 ``{name: id}``。不 wired 进 init_db：种子落点是管理端
    接线时的后续项，本函数供管理端 / 手动触发调用。
    """
    from backend.emby_server import models as em

    out = {}
    for name in SEED_TEMPLATES:
        row = db.query(em.RoleTemplate).filter(
            em.RoleTemplate.name == name).first()
        if row is None:
            row = em.RoleTemplate(name=name, description="",
                                  is_seed=True)
            db.add(row)
            db.commit()
        out[name] = row.id
    return out


def _prefixes_for_template(db, template_id: int, mount_id: int) -> set:
    from backend.emby_server import models as em

    rows = db.query(em.RoleTemplateSourcePathGrant.path_prefix).filter(
        em.RoleTemplateSourcePathGrant.role_template_id == template_id,
        em.RoleTemplateSourcePathGrant.mount_id == mount_id).all()
    return {r[0] for r in rows}


def _prefixes_for_user(db, user_id: int, mount_id: int) -> set:
    from backend.emby_server import models as em

    rows = db.query(em.UserSourcePathGrant.path_prefix).filter(
        em.UserSourcePathGrant.user_id == user_id,
        em.UserSourcePathGrant.mount_id == mount_id).all()
    return {r[0] for r in rows}


def _prefixes_for_library(db, library_id, mount_id: int) -> set:
    from backend.emby_server import models as em

    if library_id is None:
        return set()
    rows = db.query(em.LibraryAccessGrant.path_prefix).filter(
        em.LibraryAccessGrant.library_id == library_id,
        em.LibraryAccessGrant.mount_id == mount_id).all()
    return {r[0] for r in rows}


def check_path_access(db, user_id: int, item) -> Optional[str]:
    """三层路径授权判定（唯一口径）。

    放行返回 None；拒绝返回人话原因。非挂载路径直接放行；
    三层全无行直接放行；任一有行层不通过即拒绝。
    user_id 为 None 时只算库层（模板层与用户层无归属可查）。
    """
    from backend.emby_server import models as em
    from backend.emby_server import mounts as mount_lib

    try:
        parsed = mount_lib.parse_mount_path(
            getattr(item, "file_path", "") or "")
        if parsed is None:
            return None
        mount_id, rel = parsed
        layers = []  # (层名, 前缀集），只收有行的层
        if user_id is not None:
            mapping = db.query(em.UserRoleTemplate).filter(
                em.UserRoleTemplate.user_id == user_id).first()
            if mapping is not None:
                prefixes = _prefixes_for_template(
                    db, mapping.role_template_id, mount_id)
                if prefixes:
                    layers.append(("角色模板", prefixes))
            prefixes = _prefixes_for_user(db, user_id, mount_id)
            if prefixes:
                layers.append(("用户", prefixes))
        prefixes = _prefixes_for_library(
            db, getattr(item, "library_id", None), mount_id)
        if prefixes:
            layers.append(("库", prefixes))
        if not layers:
            return None
        for layer_name, prefixes in layers:
            if not any(under_prefix(p, rel) for p in prefixes):
                return f"该路径未获{layer_name}授权，无法播放"
        return None
    except Exception:  # noqa: BLE001 — 授权门禁 fail-closed
        logger.warning("路径授权判定异常，本次拒绝", exc_info=True)
        return "授权判定异常，无法确认播放权限"
