"""服（ServerRealm）—— 一个面板同时运营多个服务单元

## 概念

**一个服 = 一套可以独立运营的播放服务**：

| 维度 | 落在哪 |
|---|---|
| 内容（媒体库 / 存储挂载） | ``emby_libraries.realm_id`` / ``storage_mounts.realm_id`` |
| 卖什么（套餐） | ``subscription_plans.realm_id`` |
| 卖给谁（订阅） | ``user_subscriptions.realm_id``（同一用户可在多个服各有一份，互不影响） |
| 谁来放（后端服 EA） | ``remote_servers.realm_id``（kind=ea） |
| 卡码 / 求片 | ``registration_codes.realm_id`` / ``movie_requests.realm_id`` |

**一个后端服可以部署到多台机器**：每台机器跑一个 EA、用 ``NODE_KEY`` 认领自己的记录、
用 ``REALM`` 认领自己属于哪个服；同一个服的多台 EA **同时对外出流**，各放自己碰得到的内容
（见 ``backend/emby_server/nodes.py``）。

## 向后兼容（这一条决定了整套设计的形状）

单服部署升级上来**不能有任何行为变化**，所以：

1. 迁移时若一个服都没有，自动建默认服 ``slug='main'``，并把所有旧数据回填给它；
2. 默认服沿用**历史配置键名**（``emby_active_mode`` / ``emby_managed_url`` …），
   其它服用 ``<键名>__r<服 id>`` 后缀。于是「一个服一个 Emby 服务入口」成立，
   而老部署读到的键一字未变（见 ``realm_key``）；
3. ``active_realm_id``（面板顶部的「当前服」）为空时，一律回退到默认服。
"""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend import models
from backend.integrations import store
from backend.emby_server import models as em
from backend.emby_server import soft_delete

logger = logging.getLogger(__name__)

DEFAULT_SLUG = "main"
CONFIG_ACTIVE_REALM = "active_realm_id"

# 接入方式：付费服（需要订阅）与公益服（免费开放，不需要订阅）
ACCESS_PAID = "paid"
ACCESS_FREE = "free"
ACCESS_MODES = (ACCESS_PAID, ACCESS_FREE)
# 公益服没写规则文案时的缺省提示（用户端展示；也作为拦截提示）
DEFAULT_FREE_NOTE = "本服为公益服 · 免费开放：无需开通会员即可观看全库内容。资源请勿下载、转卖或外传，账号仅限本人使用。"
DEFAULT_FREE_DOWNLOAD_NOTE = "公益服只提供在线观看，不提供下载"


def normalize_access_mode(mode: Optional[str]) -> str:
    """把外部传进来的接入方式收敛成 paid / free（非法值一律当作 paid）"""
    value = (mode or "").strip().lower()
    return ACCESS_FREE if value == ACCESS_FREE else ACCESS_PAID


def access_mode_of(db: Session, realm_id: Optional[int]) -> str:
    """某个服的接入方式（查不到这个服时按 paid 处理，即升级前的行为）"""
    realm = get_realm(db, realm_id)
    return normalize_access_mode(realm.access_mode if realm else None)


def is_free_realm(db: Session, realm_id: Optional[int]) -> bool:
    """这个服是不是公益服（免费开放）"""
    return access_mode_of(db, realm_id) == ACCESS_FREE


def access_note_of(db: Session, realm_id: Optional[int]) -> str:
    """该服的规则文案：公益服没写时给一条缺省提示，付费服返回空串"""
    realm = get_realm(db, realm_id)
    if realm is None:
        return ""
    note = (realm.access_note or "").strip()
    if note:
        return note
    return DEFAULT_FREE_NOTE if normalize_access_mode(realm.access_mode) == ACCESS_FREE else ""


def download_policy_of(db: Session, realm_id: Optional[int]) -> Optional[bool]:
    """该服的下载策略：None=跟随全局（公益服默认禁止）/ True=允许 / False=禁止"""
    realm = get_realm(db, realm_id)
    return None if realm is None else realm.allow_download

# 这些配置键是「一个服一个」的：默认服用原键名，其它服加 __r<id> 后缀
REALM_CONFIG_BASES = (
    "emby_active_mode",
    "emby_managed_url",
    "emby_managed_enabled",
    "emby_managed_reachable",
    "emby_external_url",
    "emby_external_enabled",
    "emby_external_reachable",
    "emby_external_api_key",
    # 两服共存 P1-5：订阅购买类开关按服（默认服仍读原键名；
    # subscription_buy_self_earned_only 已于 2026-11-02 随来源限定删除）
    "subscription_purchase_enabled",
    "subscription_points_purchase_enabled",
    # 挂载体检快照：挂载跟着服走，结论文案也必须按服分别存
    "emby_managed_mounts_health",
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


# ==================== 读 ====================

def list_realms(db: Session, include_disabled: bool = True) -> list[models.ServerRealm]:
    query = db.query(models.ServerRealm)
    if not include_disabled:
        query = query.filter(models.ServerRealm.is_active.is_(True))
    return query.order_by(models.ServerRealm.sort_order, models.ServerRealm.id).all()


def get_realm(db: Session, realm_id: Optional[int]) -> Optional[models.ServerRealm]:
    if realm_id is None:
        return None
    return db.query(models.ServerRealm).filter(models.ServerRealm.id == int(realm_id)).first()


def realm_by_slug(db: Session, slug: str) -> Optional[models.ServerRealm]:
    slug = (slug or "").strip().lower()
    if not slug:
        return None
    return db.query(models.ServerRealm).filter(models.ServerRealm.slug == slug).first()


def legacy_realm(db: Session) -> Optional[models.ServerRealm]:
    """默认服（升级时自动创建的那个，id 最小）

    它沿用历史配置键名，也是「不能删」的那个——删了老键就没人读了。
    """
    return db.query(models.ServerRealm).order_by(models.ServerRealm.id).first()


def legacy_realm_id(db: Session) -> Optional[int]:
    realm = legacy_realm(db)
    return realm.id if realm else None


def ensure_default_realm(db: Session) -> models.ServerRealm:
    """保证至少有一个服（空库首次启动时用）"""
    realm = legacy_realm(db)
    if realm:
        return realm
    realm = models.ServerRealm(
        name="默认服", slug=DEFAULT_SLUG, url="",
        description="首次启动自动创建；可改名，也可以再添加更多服",
    )
    db.add(realm)
    db.commit()
    db.refresh(realm)
    logger.info("已创建默认服：%s（id=%s，slug=%s）", realm.name, realm.id, realm.slug)
    return realm


def active_realm(db: Session) -> models.ServerRealm:
    """面板当前操作的服（后台各页默认作用域）"""
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == CONFIG_ACTIVE_REALM).first()
    realm = get_realm(db, int(row.value)) if row and str(row.value or "").strip().isdigit() else None
    if realm and realm.is_active:
        return realm
    return ensure_default_realm(db)


def active_realm_id(db: Session) -> int:
    return active_realm(db).id


def set_active_realm(db: Session, realm_id: int) -> None:
    realm = get_realm(db, realm_id)
    if not realm:
        raise ValueError("服不存在")
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == CONFIG_ACTIVE_REALM).first()
    if row:
        row.value = str(realm.id)
    else:
        db.add(models.SystemConfig(key=CONFIG_ACTIVE_REALM, value=str(realm.id),
                                   description="面板当前操作的服（多服运营）"))
    db.commit()


def resolve(db: Session, ref) -> Optional[models.ServerRealm]:
    """把接口参数解析成一个服

    - ``None`` / 空 → 当前服（``active_realm_id``）
    - 数字 → 按 id
    - 字符串 → 先按 slug，再按名字（EA 用 ``REALM`` 环境变量认领时两种都可能写）
    """
    if ref is None or (isinstance(ref, str) and not ref.strip()):
        return active_realm(db)
    if isinstance(ref, int) or (isinstance(ref, str) and ref.strip().isdigit()):
        return get_realm(db, int(ref))
    realm = realm_by_slug(db, str(ref))
    if realm:
        return realm
    return db.query(models.ServerRealm).filter(models.ServerRealm.name == str(ref).strip()).first()


def realm_from_env(db: Session) -> Optional[models.ServerRealm]:
    """EA 侧用环境变量 ``REALM``（slug 或 id）认领自己属于哪个服"""
    raw = (os.getenv("REALM") or os.getenv("SERVER_REALM") or "").strip()
    if not raw:
        return None
    realm = resolve(db, raw)
    if realm is None:
        logger.warning("REALM=%s 在「服」里找不到：本进程不做服过滤（按默认服处理）", raw)
    return realm


# ==================== 每服配置 ====================

def realm_key(db: Session, base: str, realm_id: Optional[int]) -> str:
    """某服的配置键名：默认服 → 原键名；其它服 → ``<base>__r<id>``

    这样老部署（只有一个服）读写的还是 ``emby_active_mode`` 这些历史键，
    外部脚本与运维文档不受影响。
    """
    if realm_id is None:
        return base
    if base not in REALM_CONFIG_BASES:
        return base
    if int(realm_id) == legacy_realm_id(db):
        return base
    return f"{base}__r{int(realm_id)}"


def realm_config(db: Session, base: str, realm_id: Optional[int] = None, default: str = "") -> str:
    """读某服的配置（``realm_id`` 为空 → 当前服）"""
    if realm_id is None:
        realm_id = active_realm_id(db)
    key = realm_key(db, base, realm_id)
    return store.get_value(db, key, default) or ""


def set_realm_config(db: Session, base: str, value: str, realm_id: Optional[int] = None,
                     description: str = "") -> None:
    """写某服的配置（``realm_id`` 为空 → 当前服）"""
    if realm_id is None:
        realm_id = active_realm_id(db)
    key = realm_key(db, base, realm_id)
    row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
    if row:
        row.value = str(value)
    else:
        db.add(models.SystemConfig(key=key, value=str(value), description=description))
    store.invalidate(key)  # 写时失效：同一进程内保存即生效


def realm_public_url(db: Session, realm_id: Optional[int] = None) -> str:
    """该服对外的 Emby 地址（用户端账号卡用）

    优先服自己填的地址；其次服内「Emby 服务入口」配的地址；最后才回退全局
    ``EMBY_PUBLIC_URL``——这样多服部署下每个服给用户的地址都是对的。
    """
    realm = get_realm(db, realm_id) if realm_id is not None else active_realm(db)
    if realm is None:
        return os.getenv("EMBY_PUBLIC_URL", "").rstrip("/")
    if (realm.url or "").strip():
        return realm.url.strip().rstrip("/")
    mode = realm_config(db, "emby_active_mode", realm.id, "managed_ea")
    if mode == "external":
        url = realm_config(db, "emby_external_url", realm.id)
    else:
        url = realm_config(db, "emby_managed_url", realm.id)
    if url.strip():
        return url.strip().rstrip("/")
    return os.getenv("EMBY_PUBLIC_URL", "").rstrip("/") if realm.id == legacy_realm_id(db) else ""


# ==================== 作用域 ====================

def scope(query, column, realm_id: Optional[int]):
    """给查询加上「属于这个服」的条件

    ``realm_id is None`` 表示不过滤（跨服统计等显式场景），而不是「没有服」。

    用于**业务数据**（套餐 / 订阅 / 卡码 / 求片 / 服务器）：它们必须属于某个服，
    升级时由 ``_ensure_default_realm`` 统一回填，所以这里是严格相等。
    """
    if realm_id is None:
        return query
    return query.filter(column == int(realm_id))


def scope_inclusive(query, column, realm_id: Optional[int]):
    """按服过滤**内容**，并把「没标注服」（``NULL``）的也算进来

    内容（媒体库 / 存储挂载）与业务数据的口径不同：``NULL`` 在这套系统里一直是
    「未分配 → 所有服、所有节点都有份」的意思（见 ``emby_server/nodes.py``），
    升级前的老数据、以及绕过面板直接入库的内容都靠它不被藏起来。
    因此内容查询一律用这个函数，而不是 ``scope``。
    """
    if realm_id is None:
        return query
    return query.filter(or_(column.is_(None), column == int(realm_id)))


def transfer(db: Session, obj, realm_id: Optional[int]) -> bool:
    """把一条记录改到另一个服（返回是否真的变了）"""
    if realm_id is None or obj is None:
        return False
    if getattr(obj, "realm_id", None) == int(realm_id):
        return False
    setattr(obj, "realm_id", int(realm_id))
    return True


def nodes_of_realm(db: Session, realm_id: int) -> list[models.RemoteServer]:
    """这个服下的后端服（EA）——一个服可以有多台，同时出流"""
    return (db.query(models.RemoteServer)
            .filter(models.RemoteServer.realm_id == int(realm_id),
                    models.RemoteServer.kind == "ea")
            .order_by(models.RemoteServer.id)
            .all())


# ==================== 统计 ====================

def stats(db: Session, realm_id: int) -> dict:
    """一个服的运营数据（服管理页的卡片与概览页用）

    这里是后台最容易被低估的热路径：服管理页一次请求会对每个服调用一次，
    旧实现把库 / 套餐 / 订阅 / 节点整行拉进 Python，再 count / 去重。
    大库下不该把「统计」变成物化几十万条记录；数据库只返回计数与计划 ID。
    """
    now = datetime.now()

    lib_rows = db.query(em.Library.id, em.Library.is_enabled).filter(
        em.Library.realm_id == realm_id
    ).all()
    lib_ids = [row[0] for row in lib_rows]
    item_count = (
        # 假数字修复（2026-10-19 系统清点 F3）：与媒体库列表页对齐只数可见。
        # 注意 count_visible 本身不带 merged 过滤（实测：不加 merged 条件
        # 付费服=218,085，加了=178,778），必须显式加 merged_into_id IS NULL。
        soft_delete.count_visible(db, em.MediaItem.library_id.in_(lib_ids),
                                  em.MediaItem.merged_into_id.is_(None))
        if lib_ids else 0
    )

    plan_q = db.query(models.SubscriptionPlan.id).filter(
        models.SubscriptionPlan.realm_id == realm_id
    )
    plan_ids = [row[0] for row in plan_q.all()]

    # 不再把所有有效订阅物化到 Python：active / 7 天内 / 去重用户均由 DB 聚合。
    sub_filter = (
        models.UserSubscription.realm_id == realm_id,
        models.UserSubscription.status == "active",
        models.UserSubscription.end_date > now,
    )
    active_subs = db.query(func.count(models.UserSubscription.id)).filter(*sub_filter).scalar() or 0
    expiring = db.query(func.count(models.UserSubscription.id)).filter(
        *sub_filter,
        models.UserSubscription.end_date <= now + timedelta(days=7),
    ).scalar() or 0
    subscribers = db.query(func.count(func.distinct(models.UserSubscription.user_id))).filter(
        *sub_filter
    ).scalar() or 0

    node_q = db.query(models.RemoteServer.id, models.RemoteServer.last_check_ok).filter(
        models.RemoteServer.realm_id == realm_id,
        models.RemoteServer.kind == "ea",
    )
    node_rows = node_q.all()
    mounts = db.query(func.count(em.StorageMount.id)).filter(
        em.StorageMount.realm_id == realm_id
    ).scalar() or 0
    pending_requests = db.query(func.count(models.MovieRequest.id)).filter(
        models.MovieRequest.realm_id == realm_id,
        models.MovieRequest.status == "pending",
    ).scalar() or 0
    return {
        "libraries": len(lib_ids),
        "enabled_libraries": sum(1 for _, enabled in lib_rows if enabled),
        "items": int(item_count),
        "mounts": int(mounts),
        "plans": len(plan_ids),
        "plan_ids": plan_ids,
        "active_subscriptions": int(active_subs),
        "expiring_subscriptions": int(expiring),
        "subscribers": int(subscribers),
        "nodes": len(node_rows),
        "nodes_online": sum(1 for _, ok in node_rows if ok is True),
        "pending_requests": int(pending_requests),
    }


def serialize(
    db: Session,
    realm: models.ServerRealm,
    with_stats: bool = True,
    *,
    stats_data: Optional[dict] = None,
    legacy_id: Optional[int] = None,
) -> dict:
    """序列化一个服。

    ``stats_data`` / ``legacy_id`` 是批量列表页的复用入口：
    ``GET /realms`` 会同时组装卡片与顶部汇总，旧实现把同一服的统计算两遍，
    服数一多就变成 2N 轮 count。单服调用不传时保持原行为。
    """
    mode = normalize_access_mode(realm.access_mode)
    if legacy_id is None:
        legacy_id = legacy_realm_id(db)
    data = {
        "id": realm.id,
        "name": realm.name,
        "slug": realm.slug,
        "url": realm.url or "",
        "description": realm.description or "",
        # 接入方式与公益设置：面板列表/卡片直接展示，用户端也读同一口径
        "access_mode": mode,
        "is_free": mode == ACCESS_FREE,
        "access_note": access_note_of(db, realm.id),
        "allow_download": realm.allow_download,
        "is_active": bool(realm.is_active),
        "sort_order": realm.sort_order or 0,
        "is_default": realm.id == legacy_id,
        "public_url": realm_public_url(db, realm.id),
        "nodes": [
            {
                "id": node.id, "name": node.name, "url": node.url,
                "node_key": node.node_key or "", "is_enabled": bool(node.is_enabled),
                "is_active": bool(node.is_active),
                "online": node.last_check_ok is True,
                "last_checked_at": node.last_checked_at.isoformat() if node.last_checked_at else None,
                "last_check_message": node.last_check_message or "",
            }
            for node in nodes_of_realm(db, realm.id)
        ],
        "created_at": realm.created_at.isoformat() if realm.created_at else None,
    }
    if with_stats:
        data["stats"] = stats_data if stats_data is not None else stats(db, realm.id)
    return data


# ==================== 写 ====================

def validate_slug(slug: str) -> str:
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError("标识只能用小写字母、数字、下划线或短横线，且以字母或数字开头（≤40 字符）")
    return slug


def create_realm(db: Session, *, name: str, slug: str = "", url: str = "",
                 description: str = "", is_active: bool = True,
                 access_mode: str = ACCESS_PAID, access_note: str = "",
                 allow_download: Optional[bool] = None) -> models.ServerRealm:
    name = (name or "").strip()
    if not name:
        raise ValueError("请填写服的名称")
    if db.query(models.ServerRealm).filter(models.ServerRealm.name == name).first():
        raise ValueError(f"已存在同名的服：{name}")
    slug = validate_slug(slug or _auto_slug(db, name))
    if db.query(models.ServerRealm).filter(models.ServerRealm.slug == slug).first():
        raise ValueError(f"标识 {slug} 已被占用")
    realm = models.ServerRealm(
        name=name, slug=slug, url=(url or "").strip().rstrip("/"),
        description=(description or "").strip(), is_active=bool(is_active),
        access_mode=normalize_access_mode(access_mode),
        access_note=(access_note or "").strip(),
        allow_download=allow_download,
        sort_order=(db.query(models.ServerRealm).count() + 1),
    )
    db.add(realm)
    db.commit()
    db.refresh(realm)
    logger.info("已创建服：%s（id=%s，slug=%s）", realm.name, realm.id, realm.slug)
    return realm


def _auto_slug(db: Session, name: str) -> str:
    """没填标识时按名字生成一个 ASCII slug（中文名 → realm<N>）"""
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    base = ascii_slug if _SLUG_RE.match(ascii_slug) else "realm"
    candidate = base
    i = 2
    while db.query(models.ServerRealm).filter(models.ServerRealm.slug == candidate).first():
        candidate = f"{base}-{i}"
        i += 1
    return candidate


def update_realm(db: Session, realm: models.ServerRealm, *, name: Optional[str] = None,
                 url: Optional[str] = None, description: Optional[str] = None,
                 is_active: Optional[bool] = None, sort_order: Optional[int] = None,
                 access_mode: Optional[str] = None, access_note: Optional[str] = None,
                 allow_download: Optional[bool] = None,
                 download_policy: Optional[str] = None) -> models.ServerRealm:
    """改服

    ``access_mode`` / ``access_note`` / ``allow_download`` 是 v2.7.0 的公益服字段。
    下载策略用三态表达：``download_policy`` 取 ``"follow"`` / ``"allow"`` / ``"deny"``
    （``allow_download`` 直接传 bool 也可以，等价于 allow / deny）。
    """
    if name is not None:
        name = name.strip()
        if not name:
            raise ValueError("服的名称不能为空")
        clash = (db.query(models.ServerRealm)
                 .filter(models.ServerRealm.name == name, models.ServerRealm.id != realm.id)
                 .first())
        if clash:
            raise ValueError(f"已存在同名的服：{name}")
        realm.name = name
    if url is not None:
        realm.url = url.strip().rstrip("/")
    if description is not None:
        realm.description = description.strip()
    if sort_order is not None:
        realm.sort_order = int(sort_order)
    if is_active is not None:
        realm.is_active = bool(is_active)
    if access_mode is not None:
        realm.access_mode = normalize_access_mode(access_mode)
    if access_note is not None:
        realm.access_note = access_note.strip()
    if download_policy is not None:
        policy = download_policy.strip().lower()
        if policy == "follow":
            realm.allow_download = None
        elif policy in ("allow", "deny"):
            realm.allow_download = policy == "allow"
    if allow_download is not None:
        realm.allow_download = bool(allow_download)
    db.commit()
    db.refresh(realm)
    return realm


def delete_realm(db: Session, realm: models.ServerRealm, *, move_to: Optional[int] = None) -> dict:
    """删掉一个服

    默认**拒绝**删除还有数据的服（订阅/套餐/媒体库/服务器…），除非 ``move_to`` 指定了
    另一个服把数据整体移交过去。默认服永远不能删：历史配置键挂在它身上。
    """
    if realm.id == legacy_realm_id(db):
        raise ValueError("默认服不能删除（历史配置挂在它上面）；可以改名，或把数据移走后新建另一套")
    if move_to is not None and int(move_to) == realm.id:
        raise ValueError("不能把数据移交给被删除的服自己")
    target = get_realm(db, move_to) if move_to is not None else None
    if move_to is not None and target is None:
        raise ValueError("要移交到的服不存在")

    counts = stats(db, realm.id)
    payload = {
        "libraries": counts["libraries"], "mounts": counts["mounts"],
        "plans": counts["plans"], "subscriptions": counts["active_subscriptions"],
    }
    if target is None and any(payload.values()):
        raise ValueError(
            "这个服还有数据（媒体库 %d / 挂载 %d / 套餐 %d / 有效订阅 %d），"
            "请先清空，或指定「数据移交给另一个服」" % (
                payload["libraries"], payload["mounts"], payload["plans"], payload["subscriptions"])
        )

    if target is not None:
        for model in (em.Library, em.StorageMount, models.SubscriptionPlan,
                      models.UserSubscription, models.RegistrationCode,
                      models.MovieRequest, models.RemoteServer):
            (db.query(model)
             .filter(model.realm_id == realm.id)
             .update({"realm_id": target.id}, synchronize_session=False))
        db.commit()

    # 该服的每服配置键一起清掉，避免删服后残留脏配置
    for base in REALM_CONFIG_BASES:
        key = realm_key(db, base, realm.id)
        if key != base:
            db.query(models.SystemConfig).filter(models.SystemConfig.key == key).delete()
    db.delete(realm)
    db.commit()
    if active_realm_id(db) == realm.id:
        set_active_realm(db, target.id if target else legacy_realm_id(db))
    logger.info("已删除服 #%s（数据移交给 %s）", realm.id, target.id if target else "无")
    return {"deleted": realm.id, "moved_to": target.id if target else None, **payload}


def claim(db: Session, realm_id: Optional[int]) -> int:
    """把某条新记录归到某个服；没指定时归当前服（写入口统一调它）"""
    if realm_id is not None:
        return int(realm_id)
    return active_realm_id(db)


__all__ = [
    "ACCESS_FREE",
    "ACCESS_MODES",
    "ACCESS_PAID",
    "CONFIG_ACTIVE_REALM",
    "DEFAULT_FREE_DOWNLOAD_NOTE",
    "DEFAULT_FREE_NOTE",
    "DEFAULT_SLUG",
    "REALM_CONFIG_BASES",
    "access_mode_of",
    "access_note_of",
    "active_realm",
    "active_realm_id",
    "claim",
    "download_policy_of",
    "is_free_realm",
    "normalize_access_mode",
    "create_realm",
    "delete_realm",
    "ensure_default_realm",
    "get_realm",
    "legacy_realm",
    "legacy_realm_id",
    "list_realms",
    "nodes_of_realm",
    "realm_by_slug",
    "realm_config",
    "realm_from_env",
    "realm_key",
    "realm_public_url",
    "resolve",
    "scope",
    "scope_inclusive",
    "serialize",
    "set_active_realm",
    "set_realm_config",
    "stats",
    "transfer",
    "update_realm",
    "validate_slug",
]
