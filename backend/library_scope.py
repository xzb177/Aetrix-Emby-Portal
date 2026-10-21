"""媒体库可见范围（v2.43.0）：服务器默认范围 + 指定用户单独覆盖

## 解决什么

一个站点上可能同时有「全服都能看的电影库」和「只给某几个人看的内部库」。
在本功能之前，客户端的媒体库列表（``/emby/Users/{id}/Views``）是**按全站**
返回的——只要库是启用状态，谁登录都能看到它。

这里加两层，顺序就是生效顺序：

1. **服务器默认范围**：全站默认只暴露哪些库。**默认关闭** = 所有启用的库照旧
   全部可见，升级上来的部署行为与升级前逐字一致。
2. **指定用户单独覆盖**：某个用户显式打开覆盖后，他看到的是自己那份列表；
   **把覆盖关掉 = 恢复跟随服务器默认**（列表本身留着，方便再打开）。

## 三条容易写错的地方

1. **默认必须不打断现有行为。** 三个配置键的出厂值就是「不过滤」
   （``enabled=0``、``libraries=[]``、``overrides={}``），裸库里没有任何行时
   读出来的结论也必须是「全部可见」。
2. **空列表不能等于「全部隐藏」。** 如果开着开关却一个库都没选（或选中的库
   全被删了），照字面执行会把所有人的媒体库清空——那是比没生效严重得多的事故。
   所以：写入时**拒绝**空列表启用；读取时解析为空则**退回不过滤**，并在
   payload 里如实报 ``active=false`` 让后台看得见。
3. **工作人员不受限制。** ``is_staff`` 豁免——排障时必须能看到全部库，
   与 ``share_guard`` 对管理员豁免是同一个理由。

## 判定口径

生效的可见集合 ``None`` = 不过滤（绝大多数部署都是这一档）；
``set[int]`` = 只有这些库 id 可见。集合按 **库 id** 存（不是 guid）：查询端
（条目列表 / 搜索 / 继续观看）筛的本来就是 ``MediaItem.library_id``，用 id
省一次字符串比对，也省掉 guid→id 的往返。

配置读取走 ``integrations.store`` 的短 TTL 热读（与 ``playback_policy`` /
``share_guard`` 同一套），所以后台保存后最多 60 秒内全局生效，同一进程里
保存即生效（``write_values`` 内部就会 ``invalidate``）。

默认值只在本模块定义一次，``config_self_heal`` 引用 ``DEFAULTS`` 补缺失行。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy.orm import Session

from backend.integrations import store

logger = logging.getLogger(__name__)

# ==================== 配置键与默认值（默认值只在这里定义一次） ====================

CONFIG_DEFAULT_ENABLED = "library_scope_default_enabled"
CONFIG_DEFAULT_LIBRARIES = "library_scope_default_libraries"
CONFIG_USER_OVERRIDES = "library_scope_user_overrides"

#: 出厂默认：关闭 + 空列表 + 无覆盖 = 所有启用的库可见（升级前行为）
DEFAULTS: dict[str, str] = {
    CONFIG_DEFAULT_ENABLED: "0",
    CONFIG_DEFAULT_LIBRARIES: "[]",
    CONFIG_USER_OVERRIDES: "{}",
}

#: 一次最多勾选多少个库（防御性上限：配置值被人手写成垃圾时不至于撑爆查询）
MAX_LIBRARIES = 500

#: 单个用户的覆盖最多保留多少个（同上）
MAX_OVERRIDES = 2000

#: 下拉里最多列多少个用户（配置页的载荷上限，不影响覆盖本身）
MAX_OPTIONS = 1000


def _raw(db: Session, key: str) -> str:
    """统一热读（与 ``playback_policy`` / ``share_guard`` 同一套）"""
    return (store.get_value(db, key, DEFAULTS.get(key, "")) or "").strip()


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "on")


def _parse_list(raw) -> list[int]:
    """JSON 文本（或已解析出的列表）→ 库 id 列表。

    坏数据一律当成「没配」：一条脏配置不该把浏览页炸掉。
    """
    if not isinstance(raw, str):
        parsed = raw
    else:
        try:
            parsed = json.loads(raw or "[]")
        except (ValueError, TypeError):
            return []
    if not isinstance(parsed, list):
        return []
    out: list[int] = []
    for one in parsed:
        try:
            value = int(one)
        except (TypeError, ValueError):
            continue
        if value not in out:
            out.append(value)
    return out


def _parse_overrides(raw: str) -> dict[str, dict]:
    """``{"<user_id>": {"enabled": bool, "libraries": [id…]}}`` → 归一化后的字典"""
    try:
        parsed = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    out: dict[str, dict] = {}
    for key, value in list(parsed.items())[:MAX_OVERRIDES]:
        try:
            uid = int(key)
        except (TypeError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        out[str(uid)] = {
            "enabled": bool(value.get("enabled", True)),
            "libraries": _parse_list(value.get("libraries")),
        }
    return out


def _resolve(db: Session, library_ids: list[int]) -> list[int]:
    """只保留**还存在**的库：删掉的库 id 不该继续占着名额（也不该让
    「一个都没剩下」被当成「一个都不让看」，见模块头第 2 条）

    这里**故意不加缓存**：它跑在登录与条目列表的热路径上，听起来该缓存，
    但对面只是一张几十行的表上的 ``WHERE id IN (…)``，比一次配置热读还便宜；
    而缓存一旦命中过期数据，后果是「刚删掉的库还被当成存在」——为了微秒级的
    优化去换一个「配置与现实对不上」的 bug 不划算。
    """
    if not library_ids:
        return []
    from backend.emby_server import models as em

    rows = db.query(em.Library.id).filter(em.Library.id.in_(library_ids)).all()
    known = {row[0] for row in rows}
    return [i for i in library_ids if i in known][:MAX_LIBRARIES]


# ==================== 读取 ====================


def default_policy(db: Session) -> dict:
    """服务器默认范围（``enabled=False`` = 不过滤，所有启用的库可见）"""
    ids = _resolve(db, _parse_list(_raw(db, CONFIG_DEFAULT_LIBRARIES)))
    return {
        "enabled": _truthy(_raw(db, CONFIG_DEFAULT_ENABLED)),
        "library_ids": ids,
    }


def user_overrides(db: Session) -> dict[str, dict]:
    """全部用户覆盖，键是 ``str(user_id)``"""
    return _parse_overrides(_raw(db, CONFIG_USER_OVERRIDES))


def user_override(db: Session, user_id: int) -> Optional[dict]:
    """单个用户的覆盖（没有返回 ``None`` = 这个人跟随默认）"""
    return user_overrides(db).get(str(int(user_id or 0)))


def _fallback_policy(db: Session) -> Optional[set[int]]:
    dp = default_policy(db)
    if not dp["enabled"] or not dp["library_ids"]:
        return None
    return set(dp["library_ids"])


def effective_ids(db: Session, user) -> Optional[set[int]]:
    """这个人实际能看到哪些库；``None`` = 不过滤

    判定顺序：工作人员豁免 → 个人覆盖（开着且非空才生效，否则落回默认）
    → 服务器默认（关闭或空 = 不过滤）。
    """
    if user is None:
        return None
    if getattr(user, "is_staff", False):
        return None

    override = user_overrides(db).get(str(int(user.id or 0)))
    if override is not None and override.get("enabled"):
        ids = _resolve(db, override.get("libraries") or [])
        if ids:
            return set(ids)
        # 覆盖开着但一个有效库都没有（选的库被删光了）：不能变成「一个都看不到」，
        # 落回服务器默认——与「覆盖不存在」同一条路，不额外制造一个更坏的分支。
        # 用 debug 而不是 info：这行在条目列表的热路径上，坏配置会长期存在，
        # 每个请求刷一条 info 会把日志淹掉。
        logger.debug("用户 %s 的媒体库覆盖解析为空，改按服务器默认生效", user.id)

    return _fallback_policy(db)


def effective_ids_safe(db: Session, user) -> Optional[set[int]]:
    """``effective_ids``，但读失败时**一律拒绝（fail-closed）**

    S12（安全）：旧实现读失败按「不过滤」处理——DB 抖动 / 会话处于失败事务时，
    H1 的条目可见性校验（详情、播放、下载都走 ``_require_visible_item``）会退化成
    「全部可见」，把一次稳定性故障变成越权。现在读失败返回空集合：列表 / 搜索
    暂时为空、按条目取则 403，并记一条 warning；故障恢复后自然恢复。

    工作人员（``is_staff``）照旧不受限制——豁免判断在任何读取之前，不受影响；
    这里再兜一次，保证管理员排障时不会因为配置读不到被挡在门外。
    配置读写那侧用裸的 ``effective_ids``，好让测试抓得到真错误。
    """
    try:
        return effective_ids(db, user)
    except Exception:  # noqa: BLE001 — 读失败不能放大成越权
        if user is not None and getattr(user, "is_staff", False):
            return None
        logger.warning("读取媒体库可见范围失败，本次按不可见处理（fail-closed） user=%s",
                       getattr(user, "id", None), exc_info=True)
        return set()


def scope_query(query, allowed: Optional[set[int]]):
    """把可见范围挂到「按 ``MediaItem`` 查」的查询上（``None`` = 不加条件）

    ``library_id IS NULL`` 一并放行，与 ``nodes.visible_library_ids`` 同一口径：
    没归到库的条目不该因为本功能凭空消失。
    """
    if allowed is None:
        return query
    if not allowed:
        from sqlalchemy import false

        return query.filter(false())
    from sqlalchemy import or_

    from backend.emby_server import models as em

    return query.filter(or_(em.MediaItem.library_id.is_(None),
                            em.MediaItem.library_id.in_(sorted(allowed))))


def library_allowed(db: Session, user, library_id) -> bool:
    """单个库对这个人可见吗（``library_id`` 为空 = 不是库内条目，放行）"""
    if library_id is None:
        return True
    allowed = effective_ids(db, user)
    if allowed is None:
        return True
    return int(library_id) in allowed


def item_allowed(db: Session, user, item) -> bool:
    """条目对这个人可见吗（按它所属的媒体库判断）"""
    if item is None:
        return False
    return library_allowed(db, user, getattr(item, "library_id", None))


# ==================== 写入 ====================


def _write_single(db: Session, key: str, value: str) -> None:
    store.write_values(db, {key: value}, {key: DESCRIPTIONS.get(key, "")})
    store.invalidate(key)


DESCRIPTIONS = {
    CONFIG_DEFAULT_ENABLED: "媒体库可见范围·服务器默认总开关（关闭 = 所有启用的库都可见）",
    CONFIG_DEFAULT_LIBRARIES: "媒体库可见范围·服务器默认可见的库 id（JSON 数组，总开关关闭时无效）",
    CONFIG_USER_OVERRIDES: "媒体库可见范围·逐用户覆盖（JSON：{用户id:{enabled,libraries}}），"
                            "enabled=false 表示该用户恢复跟随服务器默认",
}


def write_default(db: Session, enabled: bool, library_ids) -> dict:
    """保存服务器默认范围

    ``enabled=True`` 但一个库都没选时**拒绝**（``ValueError``）：开着开关却
    解析为空，照字面执行等于把全站媒体库清空。
    """
    # 写入时就把不存在的 id 剔掉：配置里存一堆已删除的库 id，
    # 除了让 payload 显示成「已删除的库 #7」之外毫无意义。
    ids = _resolve(db, _normalize_ids(library_ids))
    if enabled and not ids:
        raise ValueError("启用默认范围至少要勾选一个存在的媒体库")
    if not ids:
        enabled = False
    _write_single(db, CONFIG_DEFAULT_ENABLED, "1" if enabled else "0")
    _write_single(db, CONFIG_DEFAULT_LIBRARIES, json.dumps(ids, ensure_ascii=False))
    return {"enabled": enabled, "library_ids": ids}


def _normalize_ids(library_ids) -> list[int]:
    out: list[int] = []
    for one in list(library_ids or [])[:MAX_LIBRARIES]:
        try:
            value = int(one)
        except (TypeError, ValueError):
            continue
        if value not in out:
            out.append(value)
    return out


def write_user_override(db: Session, user_id: int, enabled: bool,
                        library_ids) -> dict:
    """保存某个用户的单独覆盖（``enabled=False`` = 恢复跟随默认，列表保留）"""
    uid = int(user_id or 0)
    if uid <= 0:
        raise ValueError("用户不存在")
    ids = _resolve(db, _normalize_ids(library_ids))
    if enabled and not ids:
        raise ValueError("启用个人覆盖至少要勾选一个存在的媒体库")

    overrides = user_overrides(db)
    overrides[str(uid)] = {"enabled": bool(enabled), "libraries": ids}
    _write_single(db, CONFIG_USER_OVERRIDES,
                  json.dumps(overrides, ensure_ascii=False))
    return {"user_id": uid, "enabled": bool(enabled), "library_ids": ids}


def remove_user_override(db: Session, user_id: int) -> dict:
    """删掉某个用户的覆盖 = 彻底回到跟随默认"""
    uid = int(user_id or 0)
    overrides = user_overrides(db)
    existed = overrides.pop(str(uid), None)
    _write_single(db, CONFIG_USER_OVERRIDES,
                  json.dumps(overrides, ensure_ascii=False))
    return {"user_id": uid, "removed": existed is not None}


# ==================== 管理端 payload ====================


def library_options(db: Session) -> list[dict]:
    """全部媒体库（供后台勾选）——按名称排序，标注启用/虚拟状态"""
    from backend.emby_server import models as em

    rows = db.query(em.Library).order_by(em.Library.name.asc()).all()
    return [
        {
            "id": lib.id,
            "guid": lib.guid,
            "name": lib.name,
            "is_enabled": bool(lib.is_enabled),
            "is_virtual": bool(getattr(lib, "is_virtual", False)),
            "item_count": int(getattr(lib, "item_count", 0) or 0),
        }
        for lib in rows
    ]


def user_options(db: Session) -> list[dict]:
    """可选的用户（供后台选人加覆盖）；工作人员也会列出来，但他们的名单不会生效

    上限只防配置页被一个几万人的站点拖垮：真超过时只列前 N 个，
    后台照样能用用户搜索另找（覆盖是按 user_id 存的，不依赖这份名单）。
    """
    from backend import models

    rows = (
        db.query(models.WebUser)
        .order_by(models.WebUser.username.asc())
        .limit(MAX_OPTIONS)
        .all()
    )
    return [
        {"id": u.id, "username": u.username, "is_staff": bool(u.is_staff),
         "is_active": bool(u.is_active)}
        for u in rows
    ]


def policy_payload(db: Session) -> dict:
    """后台「可见范围」页需要的一份完整配置（前端不自己拼默认值）"""
    dp = default_policy(db)
    options = library_options(db)
    users = user_options(db)
    known = {o["id"] for o in options}
    default_ids = [i for i in dp["library_ids"] if i in known]
    known_overrides = {
        uid: {
            "enabled": bool(value.get("enabled")),
            "library_ids": [i for i in (value.get("libraries") or []) if i in known],
        }
        for uid, value in user_overrides(db).items()
    }
    return {
        "default": {
            "enabled": dp["enabled"],
            "library_ids": default_ids,
            # 真的在生效吗（开了但解析为空 = 没生效，后台要看得见这件事）
            "active": bool(dp["enabled"] and default_ids),
        },
        "overrides": known_overrides,
        "libraries": options,
        "counts": {"libraries": len(options), "overrides": len(known_overrides),
                   "users": len(users)},
        "users": users,
        "labels": {
            "default_title": "服务器默认范围",
            "note": "默认关闭 = 所有启用的库都可见（与升级前一致）；"
                    "打开并勾选后，未单独覆盖的用户只看到勾选的库。",
        },
    }


__all__ = [
    "CONFIG_DEFAULT_ENABLED",
    "CONFIG_DEFAULT_LIBRARIES",
    "CONFIG_USER_OVERRIDES",
    "DEFAULTS",
    "DESCRIPTIONS",
    "default_policy",
    "effective_ids",
    "effective_ids_safe",
    "scope_query",
    "item_allowed",
    "library_allowed",
    "policy_payload",
    "user_options",
    "remove_user_override",
    "user_override",
    "user_overrides",
    "write_default",
    "write_user_override",
]
