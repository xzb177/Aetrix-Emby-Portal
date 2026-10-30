"""用户名大小写口径 —— 全项目唯一实现。

门户用户名（WebUser.username）与接线用户名（WebUser.emby_username）在库里
都是大小写敏感的唯一约束，但客户端登录（AuthenticateByName）同时认两个
字段、转账与指名码又按大小写不敏感匹配。若放行 Foo / foo 这类仅大小写
不同的账号并存，不敏感消费侧就会命中不确定。统一口径：

- 新建账号 / 设置接线名前，用 ``username_conflict`` 做大小写不敏感撞名
  检查（两个字段都查），撞上即拒绝落库；
- 消费侧按用户名不敏感查找时，用 ``find_users_by_username_ci`` 取全部
  命中，命中多个必须明确拒绝，不许 ``.first()`` 任选一个。

AuthenticateByName 本身保持精确匹配（确定性登录），不在这里改。
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from backend import models


def _ci_key(username: str) -> str:
    return (username or "").strip().lower()


def find_users_by_username_ci(
    db: Session,
    username: str,
    *,
    include_emby: bool = False,
    exclude_user_id: Optional[int] = None,
) -> list[models.WebUser]:
    """按大小写不敏感查门户用户名（可选连带接线用户名），按 id 升序返回全部命中。"""
    key = _ci_key(username)
    if not key:
        return []
    conditions = [func.lower(models.WebUser.username) == key]
    if include_emby:
        conditions.append(func.lower(models.WebUser.emby_username) == key)
    query = db.query(models.WebUser).filter(or_(*conditions))
    if exclude_user_id is not None:
        query = query.filter(models.WebUser.id != exclude_user_id)
    return query.order_by(models.WebUser.id.asc()).all()


def username_conflict(
    db: Session,
    username: str,
    *,
    exclude_user_id: Optional[int] = None,
) -> Optional[models.WebUser]:
    """新建/设名前的撞名检查：门户名或接线名大小写不敏感撞上已有账号即冲突。"""
    matches = find_users_by_username_ci(
        db, username, include_emby=True, exclude_user_id=exclude_user_id)
    return matches[0] if matches else None
