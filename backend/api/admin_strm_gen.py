"""管理后台 · .strm 生成器

把 Google Drive 视频批量生成 .strm 文件的能力收进后端：
- 配置：总开关 / Drive 源目录 / 增量间隔 / 过期清理开关
  （全部 SystemConfig，热生效）
- 手动触发：增量（默认）或全量重建
- 进度查询：轮询看 listing/generating/verifying 各阶段进度
- 缺集报告：上次生成的完整性校验结果（哪个剧缺了哪几集）

路由挂在 admin_emby_router 上（/api/admin/emby/strm-gen/*）。
"""
from __future__ import annotations

import logging
import threading

from fastapi import Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.emby_server import strm_gen as _gen
from backend.emby_server.portal import admin_emby_router, require_staff
from backend.integrations import store

logger = logging.getLogger(__name__)


class StrmGenConfigSave(BaseModel):
    enabled: bool = Field(default=True, description="总开关")
    source_dir: str = Field(default="MoviePilot/", description="Drive 源目录（相对网盘根）")
    drive_id: str = Field(default="", description="指定 Drive ID，空=自动选择")
    prune: bool = Field(
        default=False,
        description="清理 Drive 上已不存在的 .strm 文件及状态行（不可逆，默认只上报不删）",
    )
    interval_minutes: int = Field(
        default=60,
        description="增量间隔分钟数（1-1440），0=关闭",
    )
    source_type: str = Field(
        default="drive",
        description="数据源类型：drive=Google Drive，local=本地目录",
    )
    local_root: str = Field(
        default="/media",
        description="本地数据源根目录（source_type=local 时有效）",
    )
    drive_concurrency: int = Field(
        default=3,
        description="多盘并行列举并发数（1-10），默认 3（保守防风控）",
    )
    api_qps: float = Field(
        default=8.0,
        description="Drive API 全局速率上限 QPS（1.0-20.0），默认 8.0（保守）",
    )


class StrmGenTrigger(BaseModel):
    full: bool = Field(default=False, description="true=全量重建，false=增量")


def _read_config(db: Session) -> dict:
    return {
        "enabled": _gen.enabled(db),
        "source_dir": _gen.source_dir(db),
        "drive_id": _gen.drive_id_config(db),
        "prune": _gen.prune_enabled(db),
        "interval_minutes": _gen.interval_minutes(db),
        "source_type": _gen.source_type(db),
        "local_root": _gen.local_root(db),
        "drive_concurrency": _gen.drive_concurrency(db),
        "api_qps": _gen.api_qps(db),
        "last_run": store.get_value(db, _gen.CONFIG_LAST_RUN, ""),
    }


@admin_emby_router.get("/strm-gen/config")
def strm_gen_config(db: Session = Depends(get_db), _staff=Depends(require_staff)):
    """读 .strm 生成器配置。"""
    return {"success": True, "config": _read_config(db)}


@admin_emby_router.put("/strm-gen/config")
def strm_gen_config_save(body: StrmGenConfigSave,
                         db: Session = Depends(get_db),
                         _staff=Depends(require_staff)):
    """写 .strm 生成器配置（保存即热生效，无需重启）。"""
    # 源目录归一化：只允许相对路径，防止写到奇怪的地方
    src = (body.source_dir or "").strip().replace("\\", "/").strip("/")
    if ".." in src.split("/"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="源目录不能包含 ..")
    # 增量间隔：0=关闭，1-1440 分钟
    iv = int(body.interval_minutes or 0)
    if iv < 0 or iv > 1440:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="增量间隔应在 0-1440 分钟之间")
    # 数据源类型校验
    st = (body.source_type or "drive").strip().lower()
    if st not in ("drive", "local"):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="数据源类型只能是 drive 或 local")
    # 并发数校验：1-10（防风控上限）
    cc = int(body.drive_concurrency or 3)
    if cc < 1 or cc > 10:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="并行列举并发数应在 1-10 之间")
    # API 速率校验：1.0-20.0 QPS
    qps = float(body.api_qps or 8.0)
    if qps < 1.0 or qps > 20.0:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="API 速率上限应在 1.0-20.0 QPS 之间")
    # 注意：store 只有 write_values（批量写），没有 set_value；
    # 且 write_values 不自动提交，提交时机由调用方决定。
    store.write_values(db, {
        _gen.CONFIG_ENABLED: "true" if body.enabled else "false",
        _gen.CONFIG_SOURCE_DIR: (src + "/") if src else "",
        _gen.CONFIG_DRIVE_ID: (body.drive_id or "").strip(),
        _gen.CONFIG_PRUNE: "true" if body.prune else "false",
        _gen.CONFIG_INTERVAL: str(iv),
        _gen.CONFIG_SOURCE_TYPE: st,
        _gen.CONFIG_LOCAL_ROOT: (body.local_root or "/media").strip(),
        _gen.CONFIG_CONCURRENCY: str(cc),
        _gen.CONFIG_API_QPS: str(qps),
    }, {
        _gen.CONFIG_ENABLED: ".strm 生成器总开关",
        _gen.CONFIG_SOURCE_DIR: ".strm 生成器 Drive 源目录",
        _gen.CONFIG_DRIVE_ID: ".strm 生成器指定 Drive ID",
        _gen.CONFIG_PRUNE: ".strm 生成器是否清理 Drive 已不存在的文件",
        _gen.CONFIG_INTERVAL: ".strm 生成器增量间隔（分钟），0=关闭",
        _gen.CONFIG_SOURCE_TYPE: ".strm 生成器数据源类型",
        _gen.CONFIG_LOCAL_ROOT: ".strm 生成器本地源根目录",
        _gen.CONFIG_CONCURRENCY: ".strm 生成器多盘并行列举并发数（1-10）",
        _gen.CONFIG_API_QPS: ".strm 生成器 Drive API 全局速率上限 QPS（1.0-20.0）",
    })
    db.commit()
    return {"success": True, "config": _read_config(db)}


@admin_emby_router.post("/strm-gen/trigger")
def strm_gen_trigger(body: StrmGenTrigger,
                     db: Session = Depends(get_db),
                     _staff=Depends(require_staff)):
    """手动触发一次生成（后台线程执行，立即返回）。

    运行权用 _gen.try_acquire() 原子获取：获取与置位在同一把锁内完成，
    快速连点也不会出现"两次都返回已开始、第二次被静默吞掉"的竞态。
    """
    if not _gen.try_acquire():
        return {"success": False, "error": "已有生成任务在运行中",
                "progress": _gen.get_progress(db)}

    def _run():
        from backend.database import SessionLocal
        sess = SessionLocal()
        try:
            _gen.run_owned(sess, full=body.full)
        finally:
            sess.close()

    t = threading.Thread(target=_run, daemon=True, name="strm-gen-manual")
    t.start()
    return {"success": True, "message": "已开始%s生成" % ("全量" if body.full else "增量")}


@admin_emby_router.get("/strm-gen/progress")
def strm_gen_progress(db: Session = Depends(get_db), _staff=Depends(require_staff)):
    """查生成进度（前端轮询用）。"""
    prog = _gen.get_progress(db)
    return {"success": True, "progress": prog, "config": _read_config(db)}


@admin_emby_router.get("/strm-gen/drives")
def strm_gen_drives(_staff=Depends(require_staff)):
    """列出发现的 Drive（供 drive_id 配置选择）。"""
    try:
        drives = _gen.list_drives()
        return {"success": True, "drives": drives}
    except Exception as exc:
        return {"success": False, "error": str(exc)}


@admin_emby_router.get("/strm-gen/missing")
def strm_gen_missing(limit: int = 100,
                     db: Session = Depends(get_db),
                     _staff=Depends(require_staff)):
    """缺集报告：上次生成时 Drive 有但 .strm 缺失的剧集。"""
    limit = max(1, min(limit, 500))
    return _gen.missing_report(db, limit=limit)
