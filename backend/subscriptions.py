"""订阅有效性判定与付费墙（全站单一事实来源）

订阅行（UserSubscription.status）不会随时间自动翻转为 expired，
因此「是否会员」统一按 end_date 现算，避免各入口口径不一致。

**一个服一个订阅**：多服部署下，判定必须带上服（``realm_id``）——
甲服的白票不应该能在乙服的 EA 上播放。传入方式：

- 显式参数（EM 管理端 / 用户端按具体服查询时）；
- 否则用**本进程的服**（EA 的进程级解析器，启动时按节点身份安装，见
  ``emby_server/nodes.py`` 与 ``emby_api/main.py``）；
- 都没有时用**当前服**（EM 面板上的门户请求；解析不到服时才退回「不做服过滤」）。

**接入方式（v2.7.0 公益服）**：一个服可以是

- ``paid``（付费服，缺省）：按下面的付费墙判定；
- ``free``（公益服）：**免费开放**，不需要订阅就能看（会员与否不影响播放），
  但默认禁止下载（可用该服的下载策略覆盖）。

做任何与「能不能看」相关的判断都走 ``can_play`` / ``ensure_playback_allowed``，
不要在业务代码里自己写 ``has_active_subscription``——否则公益服会到处漏出付费墙。

付费墙开关：
- SystemConfig["subscription_required"]（布尔，默认 true）是否要求有效订阅才能播放
- SystemConfig["subscription_gate_message"]（字符串，可选）自定义拦截提示文案
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Callable, Optional

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from backend import models, realms, vitality as _vitality

logger = logging.getLogger(__name__)

# 进程级「本进程服务哪个服」的解析器（EA 启动时装上）
_realm_resolver: Optional[Callable[[], Optional[int]]] = None


def set_process_realm_resolver(resolver: Optional[Callable[[], Optional[int]]]) -> None:
    """由 EA 启动流程装上一个「本进程的服」解析器（EM 不装，保持跨服口径）"""
    global _realm_resolver
    _realm_resolver = resolver


def process_realm_id() -> Optional[int]:
    """本进程的服（没装解析器或解析不到时返回 None = 不做服过滤）"""
    if _realm_resolver is None:
        return None
    try:
        return _realm_resolver()
    except Exception as exc:  # noqa: BLE001 — 解析失败不能把播放整体拦住
        logger.warning("解析本进程的服失败（按不过滤处理）: %s", exc)
        return None


def resolve_realm_id(realm_id: Optional[int] = None) -> Optional[int]:
    return realm_id if realm_id is not None else process_realm_id()


def _scoped_realm_id(db: Session, realm_id: Optional[int] = None) -> Optional[int]:
    """把「没指定服」解析成具体服：本进程的服 → 面板当前服

    EM（面板）上的用户端请求没有进程级服，此时按面板当前服判定，
    否则公益服/付费墙会因“解析不到服”而退回跨服口径。
    """
    target = resolve_realm_id(realm_id)
    if target is not None:
        return target
    try:
        return realms.active_realm_id(db)
    except Exception as exc:  # noqa: BLE001 — 解析不到服时退回旧口径（不阻断播放）
        logger.warning("解析当前服失败（按不过滤处理）: %s", exc)
        return None


def realm_access_mode(db: Session, realm_id: Optional[int] = None) -> str:
    """该服（默认本进程/当前服）的接入方式：paid / free"""
    return realms.access_mode_of(db, _scoped_realm_id(db, realm_id))


def is_free_realm(db: Session, realm_id: Optional[int] = None) -> bool:
    """这个服是不是公益服（免费开放，不需要订阅）"""
    return realm_access_mode(db, realm_id) == realms.ACCESS_FREE


def realm_access_note(db: Session, realm_id: Optional[int] = None) -> str:
    """该服的规则文案（公益服没写时给缺省提示；付费服为空串）"""
    return realms.access_note_of(db, _scoped_realm_id(db, realm_id))


def view_grant(db: Session, user: models.WebUser,
               realm_id: Optional[int] = None) -> tuple[bool, Optional[models.EmbyViewUnlock]]:
    """用户有没有权限查看某服的 Emby 账号 / 线路（**全站单一事实来源**）

    规则：付费服 = 有效订阅即权限；公益服 = 花积分解锁即权限（``EmbyViewUnlock``）。
    判定一律按 ``end_date`` / ``expires_at`` **现算**，不信行上的 status 字段
    （它不会随时间自动翻转，见本模块开头）。

    ``backend/emby_server/portal.py`` 的账号卡与服卡片都调这里——以前那段逻辑
    私有地写在 portal 里，管理端想回答「这个人到底被授权了什么」只能复制一份，
    两份迟早会漂。
    """
    target = _scoped_realm_id(db, realm_id)
    if realms.is_free_realm(db, target):
        now = datetime.now()
        unlock = (
            db.query(models.EmbyViewUnlock)
            .filter(
                models.EmbyViewUnlock.user_id == user.id,
                models.EmbyViewUnlock.realm_id == target,
                or_(models.EmbyViewUnlock.expires_at.is_(None),
                    models.EmbyViewUnlock.expires_at > now),
            )
            .order_by(models.EmbyViewUnlock.unlocked_at.desc())
            .first()
        )
        return (unlock is not None), unlock
    return has_active_subscription(db, user.id, resolve_realm_id(realm_id)), None

CONFIG_REQUIRED = "subscription_required"
CONFIG_MESSAGE = "subscription_gate_message"
CONFIG_ALLOW_DOWNLOAD = "allow_download"

DEFAULT_GATE_MESSAGE = "需要有效的会员订阅才能播放，请先开通会员"
DOWNLOAD_GATE_MESSAGE = "当前站点已关闭下载，仅支持在线观看"
FREE_REALM_DOWNLOAD_MESSAGE = realms.DEFAULT_FREE_DOWNLOAD_NOTE


def has_active_subscription(db: Session, user_id: int, realm_id: Optional[int] = None) -> bool:
    """是否持有「生效中且未到期」的订阅（可按服判定）"""
    target = _scoped_realm_id(db, realm_id)
    query = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == user_id,
        models.UserSubscription.status == "active",
        models.UserSubscription.end_date > datetime.now(),
    )
    if target is not None:
        query = query.filter(models.UserSubscription.realm_id == target)
    return query.first() is not None


def active_subscription(db: Session, user_id: int, realm_id: Optional[int] = None):
    """取一份生效中的订阅（按服；用于展示用户到底买的是哪个服）"""
    target = _scoped_realm_id(db, realm_id)
    query = db.query(models.UserSubscription).filter(
        models.UserSubscription.user_id == user_id,
        models.UserSubscription.status == "active",
        models.UserSubscription.end_date > datetime.now(),
    )
    if target is not None:
        query = query.filter(models.UserSubscription.realm_id == target)
    return query.order_by(models.UserSubscription.end_date.desc()).first()


def _config_value(db: Session, key: str) -> Optional[str]:
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    return row.value if row else None


def subscription_required(db: Session) -> bool:
    """付费墙是否开启（缺省开启：项目默认按会员制运营）"""
    raw = _config_value(db, CONFIG_REQUIRED)
    if raw is None or str(raw).strip() == "":
        return True
    return str(raw).strip().lower() == "true"


def realm_requires_subscription(db: Session, realm_id: Optional[int] = None) -> bool:
    """这个服是否要会员才能看：公益服永远不要（即使全局付费墙开着）"""
    if is_free_realm(db, realm_id):
        return False
    return subscription_required(db)


def gate_message(db: Session) -> str:
    raw = _config_value(db, CONFIG_MESSAGE)
    text = (raw or "").strip()
    return text or DEFAULT_GATE_MESSAGE


def can_play(db: Session, user: models.WebUser, realm_id: Optional[int] = None) -> bool:
    """是否放行播放：管理员始终放行；公益服免费开放；付费墙关闭时全员放行

    订阅判定与免费服判定用同一作用域（显式服 → 本进程的服 → 面板当前服）：
    EA 上进程级解析器已装，行为与升级前完全一致；EM 上缺省时按面板当前服判定，
    不再退回跨服口径。
    """
    if getattr(user, "is_staff", False):
        return True
    target = _scoped_realm_id(db, realm_id)
    if realms.access_mode_of(db, target) == realms.ACCESS_FREE:
        # 公益服：免费开放，不是「白票」——这是这个服自己选的运营方式
        return True
    if not subscription_required(db):
        return True
    return has_active_subscription(db, user.id, resolve_realm_id(realm_id))


def ensure_playback_allowed(db: Session, user: models.WebUser,
                           realm_id: Optional[int] = None) -> None:
    """播放前校验：未订阅时抛 403（前端据此展示付费墙引导）

    ``realm_id`` 留空时按**本进程的服**判定（EA 上就是这台机器所属的服）。
    """
    if not can_play(db, user, realm_id):
        raise HTTPException(status_code=403, detail=gate_message(db))
    # C1 活力值：公益服用户活力值低于阈值时限制观影（管理员/非公益用户不受影响）
    _vitality.ensure_can_play(db, user)


def download_allowed(db: Session, realm_id: Optional[int] = None) -> bool:
    """本站点是否允许下载（缺省允许；关闭后第三方播放器触发的下载一并拦下）

    两层：全局开关（``allow_download`` 配置）与**该服的下载策略**——
    公益服没单独配策略时默认禁止下载（公益服靠免费开放引流，不能让整库被拖走）；
    付费服没单独配策略时跟随全局开关（与升级前完全一致）。
    """
    raw = _config_value(db, CONFIG_ALLOW_DOWNLOAD)
    global_allowed = True if raw is None or str(raw).strip() == "" else str(raw).strip().lower() == "true"
    if not global_allowed:
        return False
    target = _scoped_realm_id(db, realm_id)
    policy = realms.download_policy_of(db, target)
    if policy is not None:
        return bool(policy)
    return not realms.is_free_realm(db, target)


def ensure_download_allowed(db: Session, user: models.WebUser,
                            realm_id: Optional[int] = None,
                            request=None) -> None:
    """下载前校验：站点/该服关闭下载时抛 403（管理员依然放行，便于排查）

    ``request`` 带上下载策略网关（``DownloadGuardMiddleware``）已经判过的结论时直接复用：
    下载类请求「中间件判一遍、路由再判一遍」查的是同一件事（全局开关 + 该服策略），
    网关判过就不必重查。不传 ``request`` 或网关没给出结论时行为与以前完全一致。
    """
    state = getattr(request, "state", None) if request is not None else None
    if realm_id is None and state is not None and getattr(state, "download_allowed_by_guard", False):
        return
    if getattr(user, "is_staff", False):
        return
    if download_allowed(db, realm_id):
        return
    raise HTTPException(status_code=403, detail=download_gate_message(db, realm_id))


def download_gate_message(db: Session, realm_id: Optional[int] = None) -> str:
    """下载被拦时的提示：公益服说清「只提供在线观看」，其余沿用站点文案"""
    target = _scoped_realm_id(db, realm_id)
    if realms.is_free_realm(db, target) and realms.download_policy_of(db, target) is None:
        return FREE_REALM_DOWNLOAD_MESSAGE
    return DOWNLOAD_GATE_MESSAGE


__all__ = [
    "CONFIG_REQUIRED",
    "CONFIG_MESSAGE",
    "CONFIG_ALLOW_DOWNLOAD",
    "DEFAULT_GATE_MESSAGE",
    "DOWNLOAD_GATE_MESSAGE",
    "FREE_REALM_DOWNLOAD_MESSAGE",
    "active_subscription",
    "has_active_subscription",
    "is_free_realm",
    "process_realm_id",
    "realm_access_mode",
    "realm_access_note",
    "realm_requires_subscription",
    "resolve_realm_id",
    "set_process_realm_resolver",
    "subscription_required",
    "gate_message",
    "can_play",
    "ensure_playback_allowed",
    "view_grant",
    "download_allowed",
    "download_gate_message",
    "ensure_download_allowed",
]
