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

import json
import logging
import threading
from typing import Optional

from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.emby_server import strm_gen as _gen
from backend.emby_server.portal import admin_emby_router, require_staff
from backend.integrations import store

logger = logging.getLogger(__name__)


class StrmGenConfigSave(BaseModel):
    enabled: bool = Field(default=True, description="总开关")
    source_dir: str = Field(default="MoviePilot/",
                            description="Drive 源目录（相对网盘根），逗号分隔支持多目录，"
                                        "如 \"MoviePilot/,nastool/\"")
    drive_id: str = Field(default="", description="指定 Drive ID；空或 \"all\"=所有发现的盘")
    prune: bool = Field(
        default=False,
        description="清理 Drive 上已不存在的 .strm 文件及状态行（默认开+演习：dry_run=true 时只记 would-delete 不真删）",
    )
    dedupe_same_size: bool = Field(
        default=True,
        description="同名同大小去重：生成时输出文件名相同且字节大小相同时只留一份、"
                    "跳过重复生成；大小不同=多版本，照常保留。默认开；"
                    "关闭则完全回到现状行为（撞名全部走 -2/-3 改名）",
    )
    interval_minutes: int = Field(
        default=60,
        description="增量间隔分钟数（1-1440），0=关闭",
    )
    lock_timeout_minutes: Optional[int] = Field(
        default=None,
        description="运行锁僵死超时（分钟），0=关闭自愈，默认 180",
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
    # 删除保护 / 僵尸校验 8 项：Optional，None=前端没传=不改（只写显式提供的值）。
    # dry_run 默认 true，管理员确认 would-delete 清单无误后手动关闭才真正 armed。
    prune_dry_run: Optional[bool] = Field(
        default=None, description="删除保护：演习开关，true=只记 would-delete 不真删",
    )
    prune_max_ratio: Optional[float] = Field(
        default=None, description="删除保护：单轮删除比例上限 (0, 1]，默认 0.2",
    )
    prune_max_absolute: Optional[int] = Field(
        default=None, description="删除保护：单轮删除绝对上限 ≥1，默认 50",
    )
    verify_new_ids: Optional[bool] = Field(
        default=None, description="增量中新文件写盘前是否验一次 file_id",
    )
    orphan_check_enabled: Optional[bool] = Field(
        default=None, description="反向僵尸校验任务开关",
    )
    orphan_check_interval_days: Optional[float] = Field(
        default=None, description="反向僵尸校验周期（天）≥1，默认 7",
    )
    orphan_check_batch: Optional[int] = Field(
        default=None, description="反向校验每轮最多校验数 100-10000，默认 2000",
    )
    orphan_check_qps: Optional[float] = Field(
        default=None, description="反向校验 per-SA QPS 上限 0.5-10，默认 2.0",
    )


class StrmGenTrigger(BaseModel):
    full: bool = Field(default=False, description="true=全量重建，false=增量")


def _last_stats_dict(db: Session) -> dict:
    """读上次生成统计（dict），失败/无数据时返回 {}。"""
    try:
        raw = store.get_value(db, _gen.CONFIG_LAST_STATS, "")
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _dedupe_same_size_enabled(db: Session) -> bool:
    """同名同大小去重开关。默认 true=开（用户 2026-10-14 亲定规则）。"""
    return bool(_gen.dedupe_same_size_enabled(db))


def _read_config(db: Session) -> dict:
    return {
        "enabled": _gen.enabled(db),
        "dedupe_same_size": _dedupe_same_size_enabled(db),
        # 原文回显：多目录配置不被截断（source_dir 兼容旧前端，取第一项）
        "source_dir": _gen.source_dir(db),
        "source_dir_raw": _gen.source_dir_raw(db),
        "source_dirs": _gen.source_dirs(db),
        "drive_id": _gen.drive_id_config(db),
        "prune": _gen.prune_enabled(db),
        "interval_minutes": _gen.interval_minutes(db),
        "lock_timeout_minutes": _gen.lock_timeout_minutes(db),
        "source_type": _gen.source_type(db),
        "local_root": _gen.local_root(db),
        "drive_concurrency": _gen.drive_concurrency(db),
        "api_qps": _gen.api_qps(db),
        # .strm 删除链路（块2/块4）：读+写（PUT 只写显式提供的值）
        "prune_dry_run": _gen.prune_dry_run(db),
        "prune_max_ratio": _gen.prune_max_ratio(db),
        "prune_max_absolute": _gen.prune_max_absolute(db),
        "verify_new_ids": _gen.verify_new_ids(db),
        "orphan_check_enabled": _gen.orphan_check_enabled(db),
        "orphan_check_interval_days": _gen.orphan_check_interval_days(db),
        "orphan_check_batch": _gen.orphan_check_batch(db),
        "orphan_check_qps": _gen.orphan_check_qps(db),
        # R10：dry_run 长期提醒——配置页"删除保护"分组据此渲染黄条
        **_gen.dry_run_stale_info(db, _last_stats_dict(db)),
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
    # 源目录归一化：逗号分隔多目录；只允许相对路径，防止写到奇怪的地方
    raw_src = (body.source_dir or "").strip().replace("\\", "/")
    src_parts = []
    for part in raw_src.split(","):
        p = part.strip().strip("/")
        if not p:
            continue
        if ".." in p.split("/"):
            raise HTTPException(status_code=400, detail="源目录不能包含 ..")
        src_parts.append(p + "/")
    src = ",".join(src_parts)
    # 增量间隔：0=关闭，1-1440 分钟
    iv = int(body.interval_minutes or 0)
    if iv < 0 or iv > 1440:
        raise HTTPException(status_code=400, detail="增量间隔应在 0-1440 分钟之间")
    # 运行锁僵死超时：0=关闭，1-1440 分钟
    if body.lock_timeout_minutes is not None:
        ltm = int(body.lock_timeout_minutes)
        if ltm < 0 or ltm > 1440:
            raise HTTPException(status_code=400, detail="运行锁僵死超时应在 0-1440 分钟之间")
    # 数据源类型校验
    st = (body.source_type or "drive").strip().lower()
    if st not in ("drive", "local"):
        raise HTTPException(status_code=400, detail="数据源类型只能是 drive 或 local")
    # 并发数校验：1-10（防风控上限）
    cc = int(body.drive_concurrency or 3)
    if cc < 1 or cc > 10:
        raise HTTPException(status_code=400, detail="并行列举并发数应在 1-10 之间")
    # API 速率校验：1.0-20.0 QPS
    qps = float(body.api_qps or 8.0)
    if qps < 1.0 or qps > 20.0:
        raise HTTPException(status_code=400, detail="API 速率上限应在 1.0-20.0 QPS 之间")
    # 删除保护/僵尸校验 8 项：只校验+写入显式提供的值（None=没传=不改）
    if body.prune_max_ratio is not None and not (0 < float(body.prune_max_ratio) <= 1):
        raise HTTPException(status_code=400, detail="删除比例上限应在 (0, 1] 范围内")
    if body.prune_max_absolute is not None and int(body.prune_max_absolute) < 1:
        raise HTTPException(status_code=400, detail="删除绝对上限应 ≥ 1")
    if body.orphan_check_interval_days is not None and float(body.orphan_check_interval_days) < 1:
        raise HTTPException(status_code=400, detail="僵尸校验间隔应 ≥ 1 天")
    if body.orphan_check_batch is not None and not (100 <= int(body.orphan_check_batch) <= 10000):
        raise HTTPException(status_code=400, detail="僵尸校验批量应在 100-10000 之间")
    if body.orphan_check_qps is not None and not (0.5 <= float(body.orphan_check_qps) <= 10):
        raise HTTPException(status_code=400, detail="僵尸校验 QPS 应在 0.5-10 之间")
    # 注意：store 只有 write_values（批量写），没有 set_value；
    # 且 write_values 不自动提交，提交时机由调用方决定。
    values = {
        _gen.CONFIG_ENABLED: "true" if body.enabled else "false",
        _gen.CONFIG_SOURCE_DIR: src,
        _gen.CONFIG_DRIVE_ID: (body.drive_id or "").strip(),
        _gen.CONFIG_PRUNE: "true" if body.prune else "false",
        # 同名同大小去重开关（本 PR 后端函数与配置同 PR 落地，直接引用）
        _gen.CONFIG_DEDUPE_SAME_SIZE:
            "true" if body.dedupe_same_size else "false",
        _gen.CONFIG_INTERVAL: str(iv),
        _gen.CONFIG_SOURCE_TYPE: st,
        _gen.CONFIG_LOCAL_ROOT: (body.local_root or "/media").strip(),
        _gen.CONFIG_CONCURRENCY: str(cc),
        _gen.CONFIG_API_QPS: str(qps),
    }
    descriptions = {
        _gen.CONFIG_ENABLED: ".strm 生成器总开关",
        _gen.CONFIG_SOURCE_DIR: ".strm 生成器 Drive 源目录",
        _gen.CONFIG_DRIVE_ID: ".strm 生成器指定 Drive ID",
        _gen.CONFIG_PRUNE: ".strm 生成器是否清理 Drive 已不存在的文件",
        _gen.CONFIG_DEDUPE_SAME_SIZE:
            ".strm 生成器同名同大小去重（开=同名同大小只留一份，默认开）",
        _gen.CONFIG_INTERVAL: ".strm 生成器增量间隔（分钟），0=关闭",
        _gen.CONFIG_SOURCE_TYPE: ".strm 生成器数据源类型",
        _gen.CONFIG_LOCAL_ROOT: ".strm 生成器本地源根目录",
        _gen.CONFIG_CONCURRENCY: ".strm 生成器多盘并行列举并发数（1-10）",
        _gen.CONFIG_API_QPS: ".strm 生成器 Drive API 全局速率上限 QPS（1.0-20.0）",
    }
    # 8 个删除保护/僵尸校验配置：只写显式提供的值
    if body.prune_dry_run is not None:
        values[_gen.CONFIG_PRUNE_DRY_RUN] = "true" if body.prune_dry_run else "false"
        descriptions[_gen.CONFIG_PRUNE_DRY_RUN] = ".strm 删除保护演习开关（true=只记 would-delete 不真删）"
    if body.prune_max_ratio is not None:
        values[_gen.CONFIG_PRUNE_MAX_RATIO] = str(float(body.prune_max_ratio))
        descriptions[_gen.CONFIG_PRUNE_MAX_RATIO] = ".strm 删除保护单轮删除比例上限"
    if body.prune_max_absolute is not None:
        values[_gen.CONFIG_PRUNE_MAX_ABSOLUTE] = str(int(body.prune_max_absolute))
        descriptions[_gen.CONFIG_PRUNE_MAX_ABSOLUTE] = ".strm 删除保护单轮删除绝对上限"
    if body.lock_timeout_minutes is not None:
        values[_gen.CONFIG_LOCK_TIMEOUT_MIN] = str(int(body.lock_timeout_minutes))
        descriptions[_gen.CONFIG_LOCK_TIMEOUT_MIN] = ".strm 生成器运行锁僵死超时（分钟），0=关闭自愈"
    if body.verify_new_ids is not None:
        values[_gen.CONFIG_VERIFY_NEW_IDS] = "true" if body.verify_new_ids else "false"
        descriptions[_gen.CONFIG_VERIFY_NEW_IDS] = ".strm 增量新文件写盘前校验 file_id"
    if body.orphan_check_enabled is not None:
        values[_gen.CONFIG_ORPHAN_ENABLED] = "true" if body.orphan_check_enabled else "false"
        descriptions[_gen.CONFIG_ORPHAN_ENABLED] = ".strm 反向僵尸校验任务开关"
    if body.orphan_check_interval_days is not None:
        values[_gen.CONFIG_ORPHAN_INTERVAL_DAYS] = str(float(body.orphan_check_interval_days))
        descriptions[_gen.CONFIG_ORPHAN_INTERVAL_DAYS] = ".strm 反向僵尸校验周期（天）"
    if body.orphan_check_batch is not None:
        values[_gen.CONFIG_ORPHAN_BATCH] = str(int(body.orphan_check_batch))
        descriptions[_gen.CONFIG_ORPHAN_BATCH] = ".strm 反向校验每轮最多校验数"
    if body.orphan_check_qps is not None:
        values[_gen.CONFIG_ORPHAN_QPS] = str(float(body.orphan_check_qps))
        descriptions[_gen.CONFIG_ORPHAN_QPS] = ".strm 反向校验 per-SA QPS 上限"
    store.write_values(db, values, descriptions)
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
