"""管理员账号核心逻辑 —— 全项目**唯一实现**。

``scripts/create_admin.py``（命令行建/升级管理员）与
``POST /api/admin/setup``（首次运行向导建第一个管理员）都走这里，
不要再写第二套（建账号口径：用户名规则 / 密码强度 / Emby 播放凭据同步）。
"""

from __future__ import annotations

import secrets
import string

PASSWORD_MIN_LENGTH = 6
PASSWORD_RECOMMENDED_LENGTH = 12


class AdminAccountError(RuntimeError):
    """参数或状态不成立，直接给用户看的错误。"""


def generate_password(length: int = 16) -> str:
    """生成同时含大小写、数字与符号的随机密码"""
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*-_=+"
    required = (string.ascii_lowercase, string.ascii_uppercase, string.digits, "!@#$%^&*-_=+")
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if all(any(c in group for c in pwd) for group in required):
            return pwd


def validate_username(username: str) -> str:
    """用户名规则与门户注册完全一致，避免造出「后台能建、门户登不上」的账号"""
    from backend.api.emby_portal import USERNAME_RE

    username = (username or "").strip()
    if not USERNAME_RE.match(username):
        raise AdminAccountError(
            f"用户名不合法：{username!r}（只允许字母 / 数字 / 下划线，长度 3-32，与门户注册一致）"
        )
    return username


def check_password_strength(password: str) -> str | None:
    """返回警告文案（密码够强时为 None）；过短直接拒绝"""
    if len(password) < PASSWORD_MIN_LENGTH:
        raise AdminAccountError(f"密码太短：至少 {PASSWORD_MIN_LENGTH} 位（与门户注册一致）")
    if len(password) < PASSWORD_RECOMMENDED_LENGTH:
        return f"密码短于 {PASSWORD_RECOMMENDED_LENGTH} 位，建议用更长的随机密码"
    return None


def upsert_admin(db, username: str, password: str | None = None,
                 reset_password: bool = False) -> tuple[object, bool, str | None]:
    """创建或升级管理员账号，返回 ``(user, created, applied_password)``

    - 账号不存在：必须给密码（``password`` 或 ``reset_password=True`` 自动生成），否则报错；
    - 账号已存在：只升级 ``is_staff`` / ``is_active``；``password`` 给了就改，
      ``reset_password=True`` 则重置为新的随机密码，两者都没有则**不动密码**；
    - 不自动生成接线用户名（``emby_username``）。用户在接线员页自己设，设好后不可改。
      本次如果写了门户密码，会同步 ``emby_password``（bcrypt）。没有接线用户名时，
      客户端仍可用门户用户名 + 这份播放密码登录（``AuthenticateByName`` 两个字段都认）。

    ``applied_password`` 是本次真正写进库的密码（没有改密码时为 None）。
    """
    from backend import models, register_channel
    from backend.emby_server.auth import ensure_emby_credentials
    from backend.security import hash_password

    applied: str | None = password
    if applied:
        check_password_strength(applied)

    user = db.query(models.WebUser).filter(models.WebUser.username == username).first()
    created = user is None

    if created:
        if not applied:
            applied = generate_password() if reset_password else None
        if not applied:
            raise AdminAccountError("账号不存在：新建管理员必须给密码（-p），或让它自动生成随机密码")
        user = models.WebUser(
            username=username,
            password_hash=hash_password(applied),
            is_staff=True,
            is_active=True,
            # 服务器本机建的号（安装向导 / create_admin.py）= 站长：显式 super。
            # 空角色现在按只读处理（安全修复 S1），所以必须写明。
            admin_role="super",
            # 注册渠道归因（v2.44.0）：管理员建的号不混进「开放注册」
            register_channel=register_channel.ADMIN,
        )
        db.add(user)
    else:
        if reset_password and not applied:
            applied = generate_password()
        user.is_staff = True
        user.is_active = True
        # 只有能在服务器上执行命令的人才走得到这里：显式写 super（S1：空角色不再等于 super）
        user.admin_role = "super"
        if applied:
            user.password_hash = hash_password(applied)

    db.commit()
    db.refresh(user)
    # 给了密码就同步播放密码哈希。不在这里发明接线用户名。
    ensure_emby_credentials(db, user, password=applied)
    db.refresh(user)
    return user, created, applied


