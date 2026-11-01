"""后端服务状态 API：API 服务 + Worker 服务的健康状态"""
import json
import logging
import os
import time

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.admin_core import _audit, get_current_admin
from backend.database import get_db

logger = logging.getLogger(__name__)

services_router = APIRouter(prefix="/api/admin", tags=["管理后台·服务状态"])


def _get_redis():
    """获取 Redis 连接，失败返回 None"""
    try:
        import redis
        url = os.getenv("REDIS_URL", "redis://redis:6379/0")
        # 容器内 redis 主机名可能是 redis 或 aetrix-redis
        for u in [url, "redis://redis:6379/0", "redis://aetrix-redis:6379/0", "redis://localhost:6379/0"]:
            try:
                r = redis.from_url(u, socket_timeout=2)
                r.ping()
                return r
            except Exception:
                continue
    except Exception:
        pass
    return None


@services_router.get("/services/status")
def get_services_status(admin=Depends(get_current_admin), db: Session = Depends(get_db)):
    """返回后端服务状态：API 自身 + Worker 心跳"""
    now = time.time()

    # API 自身状态
    api_status = {
        "name": "aetrix-api",
        "role": "api",
        "status": "healthy",
        "timestamp": now,
    }

    # Worker 心跳（Redis）
    worker_status = {
        "name": "aetrix-worker",
        "role": "worker",
        "status": "unknown",
        "last_heartbeat": None,
        "lag_seconds": None,
    }
    r = _get_redis()
    if r:
        try:
            raw = r.get("aetrix:worker:heartbeat")
            if raw:
                hb = json.loads(raw)
                updated = hb.get("updated_at", 0)
                lag = now - updated
                worker_status["last_heartbeat"] = updated
                worker_status["lag_seconds"] = round(lag, 1)
                # 心跳 60 秒内算 healthy
                worker_status["status"] = "healthy" if lag < 60 else "stale"
                worker_status["pid"] = hb.get("pid")
                worker_status["started_at"] = hb.get("started_at")
            else:
                worker_status["status"] = "no_heartbeat"
        except Exception as e:
            logger.warning(f"读取 worker 心跳失败: {e}")
            worker_status["status"] = "error"
    else:
        worker_status["status"] = "redis_unavailable"

    return {
        "success": True,
        "services": [api_status, worker_status],
        "timestamp": now,
    }




class BackupConfigIn(BaseModel):
    enabled: bool = True
    time: str = Field(default="03:00", description="每天执行时间，HH:MM（服务器本地时间）")
    keep_days: int = Field(default=7, ge=1, le=30, description="保留最近 N 天的备份")


@services_router.get("/system/backup")
def get_backup_config(admin=Depends(get_current_admin), db: Session = Depends(get_db)):
    """数据库备份配置 + 备份文件列表"""
    from backend.emby_server import db_backup
    return {"success": True, **db_backup.get_config(db)}


@services_router.put("/system/backup")
def update_backup_config(payload: BackupConfigIn, admin=Depends(get_current_admin),
                         db: Session = Depends(get_db)):
    """修改数据库备份配置（开关 / 时间 / 保留天数）；立即生效，无需重启"""
    from backend.emby_server import db_backup
    admin_id = getattr(admin, "id", None)  # save_config 会 commit，先取纯值防过期
    try:
        cfg = db_backup.save_config(db, payload.enabled, payload.time, payload.keep_days)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if admin_id is not None:
        _audit(db, admin_id, "backup_config_update", "system", None,
               {"enabled": payload.enabled, "time": payload.time,
                "keep_days": payload.keep_days})
        db.commit()
    return {"success": True, **cfg}


@services_router.post("/system/backup/run")
def run_backup_now(admin=Depends(get_current_admin), db: Session = Depends(get_db)):
    """立即手动备份一次数据库（含轮转）"""
    from backend.emby_server import db_backup
    cfg = db_backup.get_config(db)
    try:
        result = db_backup.run_backup_now(reason="manual")
        pruned = db_backup.prune_old_backups(cfg["keep_days"])
    except Exception as exc:  # noqa: BLE001 — 转 500，错误信息给管理员看
        logger.error("手动数据库备份失败：%s", exc)
        raise HTTPException(status_code=500, detail=f"备份失败：{exc}")
    admin_id = getattr(admin, "id", None)
    if admin_id is not None:
        _audit(db, admin_id, "backup_run_manual", "backup_file",
               None, {"name": result["name"], "pruned": pruned})
        db.commit()
    logger.info("管理员手动触发数据库备份：%s", result["name"])
    return {"success": True, "backup": {"name": result["name"], "size": result["size"]},
            **{k: v for k, v in cfg.items() if k != "backups"},
            "backups": db_backup.list_backups()}


@services_router.get("/system/backup/files/{name}/info")
def get_backup_file_info(name: str, admin=Depends(get_current_admin),
                         db: Session = Depends(get_db)):
    """备份校验信息（只读）：sha256 / gzip 完整性 / 格式判定 / 人工恢复说明。

    只展示校验信息，**不提供一键覆盖恢复**——覆盖恢复是停服人工操作。
    """
    from backend.emby_server import db_backup
    info = db_backup.backup_info(name)
    if info is None:
        raise HTTPException(status_code=404, detail="备份文件不存在")
    return {"success": True, **info}


@services_router.get("/system/backup/files/{name}/download")
def download_backup_file(name: str, admin=Depends(get_current_admin),
                         db: Session = Depends(get_db)):
    """下载一份备份文件（带审计）。只提供下载 + 校验，不做一键覆盖恢复。"""
    from backend.emby_server import db_backup
    path = db_backup.find_backup(name)
    if path is None:
        raise HTTPException(status_code=404, detail="备份文件不存在")
    _audit(db, admin, "backup.download",
           target_type="backup_file", details={"name": name})
    db.commit()
    return FileResponse(path, filename=name, media_type="application/gzip")


__all__ = ["services_router"]
