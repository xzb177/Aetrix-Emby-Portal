#!/usr/bin/env python3
"""创建 / 升级管理后台账号（当前统一后端 EM 的 `web_users`）

管理后台与门户**共用同一套账号**：`web_users.is_staff = true` 才是管理员，
登录入口是 `POST /api/admin/auth/login`（也可以先在门户登录，再从 `/admin/` 免登进去）。

新库里一个账号都没有，而且**第一个注册的用户不会被自动提升为管理员**（有意的安全设计），
所以自建部署的「第一个管理员」用它来造。库由 `DATABASE_URL` 决定；不设时用仓库根目录下的
SQLite 库（新装为 `aetrix_unified.db`，升级上来的部署沿用原来那个库文件——改品牌不会换名字）。

核心建账号逻辑在 `backend/admin_accounts.py`（与首次运行向导共用，唯一实现），
这里只保留命令行参数解析与打印。

用法::

    python3 scripts/create_admin.py                            # 创建/升级 admin，随机强密码并打印
    python3 scripts/create_admin.py -u boss -p 'Passw0rd!23'    # 指定账号与密码
    python3 scripts/create_admin.py -u boss --no-password       # 只把已有账号升级为管理员，不改密码

幂等：账号已存在时只升级权限（默认会把密码重置为新的随机密码，`--no-password` 则完全不动密码），
同时补齐 Emby 客户端凭据（门户密码即播放密码）。退出码 0 = 账号已就绪。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 不在 .env 里也要能跑：默认单机 SQLite（与 serve.py / deploy_check.py 同口径）
# 这里**不写死库文件名**：交给 backend.database 解析，升级上来的老部署会继续用原库文件，
# 否则「建管理员」会静悄悄建出一个空库，人在错的库里找不到自己刚建的账号。
os.environ.setdefault("DATABASE_TYPE", "sqlite")

from backend.admin_accounts import (  # noqa: E402 — 必须在 sys.path 就绪后导入
    AdminAccountError,
    PASSWORD_RECOMMENDED_LENGTH,
    check_password_strength,
    generate_password,
    upsert_admin,
    validate_username,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="创建 / 升级管理后台账号（web_users.is_staff = true）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="库由环境变量 DATABASE_URL 决定；不设时用仓库根目录下的 SQLite 库（默认 aetrix_unified.db，老部署沿用原文件名）",
    )
    parser.add_argument("-u", "--username", default="admin", help="账号名（默认 admin）")
    parser.add_argument("-p", "--password", default=None,
                        help="密码；不传则账号已存在时不改密码，不存在时自动生成随机密码")
    parser.add_argument("--no-password", action="store_true",
                        help="完全不动密码（只升级权限，账号必须已存在）")
    parser.add_argument("--dry-run", action="store_true", help="只检查参数与账号状态，不写库")
    args = parser.parse_args()

    if args.no_password and args.password:
        parser.error("--no-password 与 -p 不能同时使用")

    from backend import models  # noqa: F401 — 保证建表时注册全部模型
    from backend.database import DATABASE_TYPE, DATABASE_URL, SessionLocal, init_db

    try:
        username = validate_username(args.username)
        password = args.password
        if password:
            warn = check_password_strength(password)
            if warn:
                print(f"⚠️  {warn}")
    except AdminAccountError as exc:
        print(f"❌ {exc}")
        return 1

    if args.dry_run:
        init_db()
        db = SessionLocal()
        try:
            exists = db.query(models.WebUser).filter(
                models.WebUser.username == username).first() is not None
        finally:
            db.close()
        print(f"（dry-run）账号 {username!r} {'已存在，会被升级为管理员' if exists else '不存在，会新建'}；"
              f"库：{DATABASE_TYPE} ({DATABASE_URL})")
        return 0

    init_db()
    db = SessionLocal()
    try:
        user, created, applied = upsert_admin(
            db, username, password=password, reset_password=not args.no_password,
        )
        # 先把要展示的字段取出来（会话关掉后 ORM 实例会 detached，不能再去读属性）
        summary = {
            "username": user.username,
            "id": user.id,
            "is_staff": bool(user.is_staff),
            "is_active": bool(user.is_active),
            "emby_username": user.emby_username,
        }
    except AdminAccountError as exc:
        print(f"❌ {exc}")
        return 1
    finally:
        db.close()

    action = "已创建" if created else "已升级为管理员"
    print(f"\n✅ 管理后台账号{action}")
    print(f"   用户名   : {summary['username']}")
    if applied:
        suffix = "" if created else "   （本次已重置）"
        print(f"   密码     : {applied}{suffix}")
        print("              ⚠️ 仅本次打印，请登录后尽快修改（门户「个人中心」或后台右上角）")
    else:
        print("   密码     : 未改动（--no-password），沿用原密码")
    print(f"   用户 id  : {summary['id']}（is_staff={summary['is_staff']}, "
          f"is_active={summary['is_active']}）")
    print(f"   数据库   : {DATABASE_TYPE} ({DATABASE_URL})")
    if summary["emby_username"]:
        print(f"   Emby 账号: {summary['emby_username']}")
    else:
        print("   Emby 账号: 未设置（在接线员页自行创建；用户名设好后不可修改）")
    print("\n   登录入口 : 门户 / 登录后后台免登进 /admin/，或直接到 /admin/ 用上面的账号密码登录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
