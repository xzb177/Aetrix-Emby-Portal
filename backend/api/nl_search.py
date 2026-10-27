"""自然语言搜片用户端路由（第三批第二单，默认关）

挂点：用户端搜索（/api/user/nl-search），不在首页/发现页加任何东西；
前端挂接留给后续单（搜索页），本单只交接口。

鉴权与可见性：沿用用户端 ``get_current_user``；可见库集合按
``library_scope.effective_ids_safe`` 现算（读失败 → 空集 fail-closed，
此时一律无结果，不越权）。开关关 → 403（先于查库）。
响应明示是否回落为关键词搜（``fallback``），不编造结果、不做推荐排序。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import nl_search as nl
from backend import library_scope

router = APIRouter(prefix="/api/user", tags=["用户端"])


@router.get("/nl-search")
def nl_search(q: str = Query(..., min_length=1, max_length=200),
              limit: int = Query(20, ge=1, le=100),
              user=Depends(get_current_user), db: Session = Depends(get_db)):
    if not nl.enabled():
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="自然语言搜片未开启")
    allowed = library_scope.effective_ids_safe(db, user)
    try:
        return nl.search_items(db, q, allowed, limit=limit)
    except nl.NlSearchDisabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="自然语言搜片未开启")
