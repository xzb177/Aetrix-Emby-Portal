"""管理员角色与权限（v2.26.0）

此前后台只有一种身份：``is_staff``。要给运营 / 客服 / 审计的人开一个号，就得把
「改系统设置、改上游 Key、管管理员」一起交出去——实际使用里没人愿意这样。

这里在原身份之上加一层**角色**（存 ``web_users.admin_role``）：

- ``super``：全部。**必须显式写 ``admin_role='super'``**——空值 / 未知值一律按最低权限
  ``viewer`` 处理（fail closed，安全修复 S1）。升级前没写过角色的老管理员由启动期迁移
  ``ensure_legacy_admin_roles``（见本文件末尾，``init_db`` 调用）一次性把**最早的那个
  管理员**（安装向导 / ``create_admin.py`` 建的号）写成 super；其余空角色管理员保持只读，
  由超管在「管理员」页显式授予角色；若库里一个 super 都没有，启动时自愈提升最早的管理员；
- ``operator``：日常运营全都能做（用户、订阅、经济流水、卡码、优惠券、订单、求片、工单、
  公告、设备、媒体库、存储来源、服务器与线路、扫描与修复、切当前服），
  但改不了**系统设置 / 经济设置、能力中心（上游 Key）、播放与客户端策略、管理员与权限**；
- ``viewer``：只读（GET / HEAD / OPTIONS），给审计与排查看。

判定的位置只有一个：``get_current_admin``（``/api/admin/*``）与 ``require_staff``
（``/api/admin/emby/*``）。**不做端点级散落判断**——散落的判断迟早会有漏网的写接口，
一处漏网就等于角色无效。
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request

ROLE_SUPER = "super"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"

ROLES = (ROLE_SUPER, ROLE_OPERATOR, ROLE_VIEWER)

ROLE_LABELS = {
    ROLE_SUPER: "超级管理员",
    ROLE_OPERATOR: "运营",
    ROLE_VIEWER: "只读审计",
}

ROLE_HINTS = {
    ROLE_SUPER: "全部权限：含系统设置、上游 Key（能力中心）、播放与客户端策略、管理员与权限",
    ROLE_OPERATOR: "日常运营：用户 / 订阅 / 订单 / 卡码 / 求片 / 工单 / 媒体库 / 存储来源 / 服务器；不能改系统设置与权限",
    ROLE_VIEWER: "只读：能看所有页面与数据，任何修改都会被拒绝（适合审计与排查）",
}

# 只读判定：这三个方法不算修改
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# 仅超级管理员可写的前缀：系统与结构，不是日常运营（读仍然人人可看）
SUPER_ONLY_PREFIXES = (
    "/api/admin/admins",           # 管理员与权限本身
    "/api/admin/economy/settings",  # 系统 / 经济设置
    "/api/admin/playback/policy",   # 播放与客户端策略（影响所有人能不能看）
    "/api/admin/capabilities",      # 能力中心：上游 Key
    "/api/admin/share-guard",       # 防共享：enforce 档会停用用户 / 拦下会话
    # 访问拦截：UA 白名单 / 地区封禁是**全站**开关。一个只读名单写歪就能把所有人
    # （包括别的管理员）挡在门外，而且这玩意儿本身就是「把自己锁在门外」的保险，
    # 出了事只能回这一页改——所以与防共享同级，只给超管。
    "/api/admin/access-guard",
    # API Key / Service Token：运营自己给自己发一把 admin scope 的 Key，
    # 就能绕过上面所有名单——创建/吊销只给超管（只读 GET 不受影响）。
    "/api/admin/api-keys",
    # 批 7 补后端批（2026-10-26）：以下 5 口此前只在 B 档（运营可写），
    # 逐项核对均为系统级 / 审计级写操作，收进超管名单。前缀判定只拦非安全
    # 方法，各口的 GET 读口不受影响（运营仍可看配置、只读审计仍可读）。
    "/api/admin/emby/mounts/rclone/conf",  # rclone.conf 整份覆盖（凭据材料；
    # 与后缀 /rclone-conf 区分：那条是服务器维度的 rclone-conf 读写都拦，
    # 这里只拦写，GET 仍只回 remote 名、不回明文）
    "/api/admin/settings/registration",  # 注册总开关（开/关影响全站新用户入口）
    "/api/admin/hotlink-config",  # 防盗链总开关 + 播放签名 TTL
    "/api/admin/login-logs/purge",  # 清空登录审计记录（删证据的操作只给超管）
    "/api/admin/items-dedup-cache-config",  # 列表去重缓存配置
    "/api/admin/transfer-config",  # 转存总开关（含跨 provider）+ 并发上限：影响全站播放路径
)

# 仅超级管理员可读写（不分读写方法）的路径后缀：凭据材料类端点。
# rclone.conf 含网盘凭据，仅超管可读写；server_id 在路径中间
# （/api/admin/servers/{id}/rclone-conf），前缀匹配切不出来，故用后缀收进集中判定——
# 仍不做端点级散落判断（R49 收编 servers.py 的散落 _require_super）。
# 口径补正（2026-10-19）：原 PR 文把 GET 读放宽成「人人可看」，冒烟测试确诊这是回归
# （旧 _require_super 拦的是读写、冒烟断言「读写都被拒」）——恢复成读也要拦。
SUPER_ONLY_SUFFIXES = (
    "/rclone-conf",
)


def normalize_role(raw: Optional[str]) -> str:
    """把库里的值收敛到枚举内

    空值 / 未知值一律当 **viewer（最低权限）**——fail closed（安全修复 S1）。
    此前空值按 super 处理，导致运营只要把任意账号的 ``is_staff`` 置真就能造出一个超管。
    升级上来的老管理员由 ``ensure_legacy_admin_roles`` 在启动时显式写角色，不靠这里兜底。
    """
    value = (raw or "").strip().lower()
    return value if value in ROLES else ROLE_VIEWER


def role_of(user) -> str:
    """某个管理员当前的角色（非管理员调用者本身已被鉴权拦下）"""
    return normalize_role(getattr(user, "admin_role", None))


def role_label(role: str) -> str:
    return ROLE_LABELS.get(role, role)


def can_write(user) -> bool:
    """这个角色能不能做写操作（前端据此把按钮置灰，服务端仍然自己判定一次）"""
    return role_of(user) != ROLE_VIEWER


def is_super(user) -> bool:
    return role_of(user) == ROLE_SUPER


def blocked_reason(request: Request, user) -> Optional[str]:
    """这次请求为什么被拦（None = 放行）

    只读角色的**所有**写操作都拦；运营角色只拦 ``SUPER_ONLY_PREFIXES`` 下的写操作；
    ``SUPER_ONLY_SUFFIXES`` 下的路径（凭据类，含 rclone.conf）**读写一律**只限超管。
    """
    path = getattr(getattr(request, "url", None), "path", "") or ""
    method = (getattr(request, "method", "") or "").upper()
    role = role_of(user)
    if role == ROLE_VIEWER and method not in SAFE_METHODS:
        return "当前账号是「只读审计」角色：可以查看，不能修改；需要写权限请让超级管理员调整角色"
    if role != ROLE_SUPER:
        # 凭据类路径后缀（rclone.conf 含网盘凭据）：读写一律只限超管，
        # 不区分 GET / HEAD / OPTIONS（2026-10-19 补正：之前只拦写是回归）。
        for suffix in SUPER_ONLY_SUFFIXES:
            if path.endswith(suffix):
                return "这一项需要「超级管理员」角色（当前：{}）".format(role_label(role))
        if method not in SAFE_METHODS:
            for prefix in SUPER_ONLY_PREFIXES:
                if path.startswith(prefix):
                    return "这一项需要「超级管理员」角色（当前：{}）".format(role_label(role))
    return None


def ensure_admin_allowed(request: Request, user) -> None:
    """鉴权依赖里统一调用：不满足就 403，并说清缺什么"""
    reason = blocked_reason(request, user)
    if reason:
        raise HTTPException(status_code=403, detail=reason)


def roles_payload() -> list:
    """角色元数据（前端不自己维护一份，改后端一处即可）"""
    return [
        {"value": role, "label": ROLE_LABELS[role], "hint": ROLE_HINTS[role]}
        for role in ROLES
    ]


def permission_payload(user) -> dict:
    """当前账号能做什么（``GET /api/admin/auth/me`` 带回，前端据此置灰入口）"""
    role = role_of(user)
    return {
        "role": role,
        "role_label": role_label(role),
        "can_write": role != ROLE_VIEWER,
        "is_super": role == ROLE_SUPER,
    }


# ---------------------------------------------------------------------------
# 启动期迁移 / 自愈（安全修复 S1）
# ---------------------------------------------------------------------------

LEGACY_MIGRATION_KEY = "security.admin_role_legacy_migrated"


def ensure_legacy_admin_roles(db) -> dict:
    """把升级前「空角色 = super」的语义显式落库，然后再也不靠空值判 super

    1. **一次性迁移**（``system_configs`` 里记标记，只跑一次）：最早创建的那个空角色管理员
       （安装向导 / ``scripts/create_admin.py`` 建的号，即原始站长）写成 ``super``；
       其余空角色管理员**不自动提权**，按 viewer（只读）处理并打警告日志，
       由超管在「管理员」页显式授予角色。
    2. **防锁死自愈**（每次启动）：库里一个启用中的 super 都没有时，把最早的启用中管理员
       提为 super——只在「没人能管」时生效，已有超管时绝不触发，所以无法被用来提权。

    返回做了什么（便于日志 / 测试）。任何异常都不阻断启动。
    """
    from backend import models

    result = {"migrated": [], "left_viewer": [], "healed": None}
    staff_q = db.query(models.WebUser).filter(models.WebUser.is_staff.is_(True))

    def _is_blank(u) -> bool:
        return not (getattr(u, "admin_role", None) or "").strip()

    done = db.query(models.SystemConfig).filter(
        models.SystemConfig.key == LEGACY_MIGRATION_KEY
    ).first()
    if done is None:
        blanks = [u for u in staff_q.order_by(models.WebUser.id).all() if _is_blank(u)]
        if blanks:
            owner = blanks[0]
            owner.admin_role = ROLE_SUPER
            result["migrated"].append(owner.id)
            result["left_viewer"] = [u.id for u in blanks[1:]]
        db.add(models.SystemConfig(
            key=LEGACY_MIGRATION_KEY,
            value="1",
            description="S1：空角色管理员已迁移（最早的管理员 → super，其余按只读）",
        ))
        db.commit()

    has_super = any(
        role_of(u) == ROLE_SUPER and u.is_active
        for u in staff_q.all()
    )
    if not has_super:
        first = staff_q.filter(models.WebUser.is_active.is_(True)).order_by(models.WebUser.id).first()
        if first is not None:
            first.admin_role = ROLE_SUPER
            result["healed"] = first.id
            db.commit()
    return result
