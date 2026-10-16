"""管理端：播放策略能力配置（中继/直链、直链源、直链名单）。

口径（2026-10-16）：只做能力——用什么策略由服主在后台决定。
默认 relay 与现行行为完全一致。密钥回显一律打码；保存时密钥留空
表示「不改」，按源名称匹配旧值保留。
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.emby_server import playback_strategy as _ps
from backend.emby_server.portal import admin_emby_router, require_staff
from backend.integrations import store

_SECRET_MASK = "********"
_MAX_SOURCES = 8


class DirectSourceIn(BaseModel):
    name: str = ""
    type: str = "s3"
    endpoint: str = ""
    bucket: str = ""
    region: str = ""
    access_key_id: str = ""
    secret_key: str = ""
    path_style: bool = True
    enabled: bool = True


class PlaybackConfigSave(BaseModel):
    strategy: str = _ps.STRATEGY_RELAY
    sources: list[DirectSourceIn] = Field(default_factory=list)
    hotlist: list[str] = Field(default_factory=list)
    key_template: str = _ps.DEFAULT_KEY_TEMPLATE
    url_ttl_sec: int = _ps.DEFAULT_TTL_SEC


def _mask_sources(sources: list[dict]) -> list[dict]:
    out = []
    for src in sources:
        item = dict(src)
        secret = str(item.get("secret_key") or "")
        item["secret_key"] = _SECRET_MASK if secret else ""
        item["has_secret_key"] = bool(secret)
        out.append(item)
    return out


def _read_config(db: Session) -> dict:
    return {
        "strategy": _ps.get_strategy(db),
        "sources": _mask_sources(_ps.get_sources(db)),
        "hotlist": sorted(_ps.get_hotlist(db)),
        "key_template": _ps.get_key_template(db),
        "url_ttl_sec": _ps.get_ttl_sec(db),
    }


@admin_emby_router.get("/playback/config")
def playback_config(db: Session = Depends(get_db), _staff=Depends(require_staff)):
    """读播放策略配置（密钥打码）。"""
    return {"success": True, "config": _read_config(db)}


@admin_emby_router.put("/playback/config")
def playback_config_save(body: PlaybackConfigSave,
                         db: Session = Depends(get_db),
                         _staff=Depends(require_staff)):
    """写播放策略配置（保存即热生效；配置缓存 TTL 内刷新）。"""
    strategy = (body.strategy or "").strip().lower()
    if strategy not in _ps.STRATEGIES:
        raise HTTPException(
            status_code=400,
            detail=f"播放策略只能是 {'/'.join(_ps.STRATEGIES)}",
        )
    ttl = int(body.url_ttl_sec or _ps.DEFAULT_TTL_SEC)
    if not (_ps.MIN_TTL_SEC <= ttl <= _ps.MAX_TTL_SEC):
        raise HTTPException(
            status_code=400,
            detail=f"直链有效期应在 {_ps.MIN_TTL_SEC}-{_ps.MAX_TTL_SEC} 秒之间",
        )
    template = (body.key_template or "").strip()
    if "{file_id}" not in template or ".." in template.split("/"):
        raise HTTPException(
            status_code=400,
            detail="对象键模板必须包含 {file_id} 且不允许 ..",
        )
    if len(body.sources) > _MAX_SOURCES:
        raise HTTPException(status_code=400, detail=f"直链源最多 {_MAX_SOURCES} 个")

    old_by_name = {str(s.get("name") or ""): s for s in _ps.get_sources(db)}
    sources: list[dict] = []
    seen_names: set[str] = set()
    for src in body.sources:
        name = src.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="直链源名称不能为空")
        if name in seen_names:
            raise HTTPException(status_code=400, detail=f"直链源名称重复：{name}")
        seen_names.add(name)
        if (src.type or "s3").strip().lower() != "s3":
            raise HTTPException(status_code=400, detail="直链源类型目前只支持 s3（兼容 R2/B2/Bunny）")
        endpoint = src.endpoint.strip()
        if endpoint and not (endpoint.startswith("https://") or endpoint.startswith("http://")):
            raise HTTPException(status_code=400, detail=f"直链源 {name} 的 endpoint 必须以 http(s):// 开头")
        secret = (src.secret_key or "").strip()
        if not secret or secret == _SECRET_MASK:
            # 留空/打码回传 = 不改：沿用同名旧源的密钥
            secret = str(old_by_name.get(name, {}).get("secret_key") or "")
        sources.append({
            "name": name,
            "type": "s3",
            "endpoint": endpoint,
            "bucket": src.bucket.strip(),
            "region": src.region.strip(),
            "access_key_id": src.access_key_id.strip(),
            "secret_key": secret,
            "path_style": bool(src.path_style),
            "enabled": bool(src.enabled),
        })

    hotlist = sorted({str(x).strip() for x in body.hotlist if str(x).strip()})

    values = {
        _ps.KEY_STRATEGY: strategy,
        _ps.KEY_SOURCES: json.dumps(sources, ensure_ascii=False),
        _ps.KEY_HOTLIST: json.dumps(hotlist, ensure_ascii=False),
        _ps.KEY_KEY_TEMPLATE: template,
        _ps.KEY_TTL: str(ttl),
    }
    descriptions = {
        _ps.KEY_STRATEGY: "播放策略（relay=中继 / direct=对象存储直链优先，名单外回落中继）",
        _ps.KEY_SOURCES: "播放直链源清单（S3 兼容：R2/B2/Bunny）",
        _ps.KEY_HOTLIST: "播放直链名单（已在直链源备有副本的 drive_file_id）",
        _ps.KEY_KEY_TEMPLATE: "播放直链对象键模板（{file_id} 占位）",
        _ps.KEY_TTL: "播放直链签名有效期（秒）",
    }
    store.write_values(db, values, descriptions)
    db.commit()
    return {"success": True, "config": _read_config(db)}
