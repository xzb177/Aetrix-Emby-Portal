"""智能字幕用户端路由（第三批第一单，默认关）

挂点：用户端（/api/user/...），不在首页/发现页加任何东西；
前端挂接留给后续单（播放流程页/账号页），本单只交接口。

鉴权与可见性：沿用用户端 ``get_current_user``；条目先查活行，
再按 ``library_scope.item_allowed`` 判定该用户可见，不可见 → 403，
不存在 → 404。开关关 → 403（先于查库，避免开关关时泄露条目存在性）。
生成的双语轨只落服务端字幕缓存区，接口只回文件名与条数，
不回显服务器绝对路径。
"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from typing import List, Optional

from backend.api.user import get_current_user
from backend.database import get_db
from backend.emby_server import models as em
from backend.emby_server import smart_subtitles as ss
from backend import library_scope

router = APIRouter(prefix="/api/user", tags=["用户端"])


class MatchRequest(BaseModel):
    candidates: List[str] = Field(default_factory=list)


class BilingualRequest(BaseModel):
    source_text: str
    source_lang: str = "eng"
    target_lang: str = "chi"
    translations: Optional[List[str]] = None
    translate: bool = False


def _visible_item(db: Session, user, item_id: int):
    item = db.query(em.MediaItem).filter(
        em.MediaItem.id == item_id,
        em.MediaItem.deleted_at.is_(None),
        em.MediaItem.merged_into_id.is_(None),
    ).first()
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="条目不存在")
    if not library_scope.item_allowed(db, user, item):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="该条目不在你的可见范围内")
    return item


@router.post("/smart-subtitles/{item_id}/match")
def match_subtitles(item_id: int, req: MatchRequest,
                    user=Depends(get_current_user), db: Session = Depends(get_db)):
    if not ss.resolve_enabled(db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="智能字幕未开启")
    item = _visible_item(db, user, item_id)
    matched = ss.match_for_item(_match_base_name(item), item.season_number,
                                item.episode_number, req.candidates)
    return {"item_id": item_id, "matched": matched}


def _match_base_name(item) -> str:
    """匹配用名：优先用文件路径的文件名，file_path 为空时回退展示名。

    字幕同名判定的本来输入就是文件名（见 subtitle_match 的
    find_external_subtitles_in：basename(file_path)）。展示名经 NFO /
    刮削改写后可能与文件名语言不同（如文件名英文、展示名中文），直接
    用展示名会把 exact sidecar 也判丢，静默返回空列表。
    """
    raw = (item.file_path or "").strip()
    base = os.path.basename(raw) if raw else ""
    return base or item.name


@router.post("/smart-subtitles/{item_id}/bilingual")
def create_bilingual(item_id: int, req: BilingualRequest,
                     user=Depends(get_current_user),
                     db: Session = Depends(get_db)):
    if not ss.resolve_enabled(db):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="智能字幕未开启")
    _visible_item(db, user, item_id)
    try:
        if req.translations is not None:
            result = ss.generate_bilingual_track(
                item_id, req.source_text, req.source_lang, req.target_lang,
                translations=req.translations, db=db)
        elif req.translate:
            result = ss.generate_bilingual_track(
                item_id, req.source_text, req.source_lang, req.target_lang, db=db)
        else:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                                detail="须提供 translations，或将 translate 置 true 走配置化翻译")
    except ss.SmartSubtitleDisabled:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="智能字幕未开启")
    except ss.TranslateNotConfigured as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail=str(exc))
    except (ss.CueMismatch, ss.TranslateFailed, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail=str(exc))
    return {"item_id": item_id, **result}
