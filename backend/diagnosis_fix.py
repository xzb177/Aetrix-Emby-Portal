# -*- coding: utf-8 -*-
"""一键体检 · 自动修复（阶段二）

在阶段一「只读诊断」之上，给部分规则配修复动作。

## 安全纪律（铁律，违反即回滚）

1. **白名单制**：只有登记在 ``FIXES`` 里的 rule_id 才能执行修复。
   不在白名单的（未知规则 / 危险操作）一律拒绝，只给手动步骤。
2. **修复动作只做三类安全可逆的事**：
   - 清理可再生的过期缓存（转码遗留目录、过期字幕缓存）；
   - 把卡住的探测游标放回 pending（租约过期才算卡住，幂等、可重跑）；
   - 触发已有的 worker 监督重启（和每 30 秒自动监督做的是同一件事）。
   删数据、改配置、重启服务 **永远不进白名单**。
3. **失败不抛**：修复函数内部异常由 API 层捕获，转结构化错误返回，
   不拖死主流程。
4. **每次执行都写审计**：成功 / 失败 / 被拒绝都落 ``admin_logs``
  （走 ``admin_core._audit`` 现有机制）。
5. **阈值全可配置**：``DIAG_FIX_*`` 环境变量，默认值只是兜底
   （铁律：只做能力，不做死业务逻辑）。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Callable
from backend.emby_server.env_util import env_float

logger = logging.getLogger(__name__)

__all__ = [
    "FIXES",
    "MANUAL_ONLY",
    "FIX_THRESHOLD_DEFS",
    "FixSpec",
    "get_fix",
    "get_fix_threshold",
    "is_fixable",
    "list_fixes",
]


# ---------------------------------------------------------------------------
# 阈值（全部可配置，默认值只是兜底；文档即契约）
# ---------------------------------------------------------------------------

FIX_THRESHOLD_DEFS: dict[str, tuple[str, float]] = {
    # 只删 mtime 超过这么多秒的遗留转码目录（0 = 不看年龄全删遗留）
    "transcode_min_age_sec": ("DIAG_FIX_TRANSCODE_MIN_AGE_SEC", 3600.0),
    # 探测租约 TTL（秒）；<= 0 时用 probe_worker 模块默认值
    "probe_claim_ttl_sec": ("DIAG_FIX_PROBE_TTL_SEC", -1.0),
    # 1 = 顺带淘汰过期字幕缓存；0 = 只清转码目录
    "subtitle_cache": ("DIAG_FIX_SUBTITLE_CACHE", 1.0),
}


def get_fix_threshold(name: str) -> float:
    """取某项修复动作的当前阈值（环境变量 > 默认值）。"""
    env_name, default = FIX_THRESHOLD_DEFS[name]
    return env_float(env_name, default)


# ---------------------------------------------------------------------------
# 修复动作（白名单）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FixSpec:
    rule_id: str
    name: str            # 按钮文案，如"清理过期缓存"
    description: str     # 大白话：这个修复会做什么、不会动什么
    confirm_text: str    # 二次确认弹窗文案
    run: Callable        # (db) -> dict 结果；内部只做安全可逆操作


def _fix_disk_space(db) -> dict:
    """disk_space：清理过期缓存。

    只删可再生的东西：不属于任何在跑会话的遗留 HLS 转码目录
    （``maintenance.cleanup_transcode_orphans``，跳过共享目录与在跑会话），
    可选再淘汰过期字幕缓存。删掉的下次需要时会自动重新生成。
    """
    from backend.emby_server import maintenance

    min_age = get_fix_threshold("transcode_min_age_sec")
    removed_dirs = maintenance.cleanup_transcode_orphans(min_age_seconds=min_age)
    removed_subs = 0
    if get_fix_threshold("subtitle_cache") >= 1.0:
        removed_subs = maintenance.prune_subtitle_cache()
    return {
        "removed_transcode_dirs": int(removed_dirs),
        "removed_subtitle_files": int(removed_subs),
        "detail": (
            f"清理了 {removed_dirs} 个遗留转码目录、{removed_subs} 个过期字幕缓存文件。"
            "删的都是可再生的临时文件，在跑的转码任务没动。"
        ),
    }


def _fix_probe_stuck(db) -> dict:
    """probe_queue：重置卡住的探测游标。

    只回收「租约过期」的 probing（``probe_worker.reclaim_stale`` 默认 TTL），
    把它们放回 pending 等待重探。正在正常跑的条目（租约有效）不动；
    操作幂等，最坏情况只是某些条目被重新探测一次。
    """
    from backend.emby_server import probe_worker

    ttl = get_fix_threshold("probe_claim_ttl_sec")
    if ttl <= 0:
        reclaimed = probe_worker.reclaim_stale(db)
    else:
        reclaimed = probe_worker.reclaim_stale(db, ttl_sec=ttl)
    return {
        "reclaimed": int(reclaimed),
        "detail": (
            f"把 {reclaimed} 条卡住的探测任务放回了排队。"
            "只动租约过期的，正在跑的不动；放回去的条目会按正常流程重探。"
        ),
    }


def _fix_workers_dead(db) -> dict:
    """workers_alive：重启停滞的 worker。

    直接跑一轮 ``worker_registry.supervise_once()`` —— 和每 30 秒自动跑的
    监督线程做的是同一件事：只重启「登记了 restart 回调」的已死线程。
    没有 restart 回调的线程（通常要重启容器才行）不动，如实报告，
    让管理员按手动步骤处理。
    """
    from backend.emby_server import worker_registry

    restarted = worker_registry.supervise_once() or []
    # 快照里仍是 crashed、且是本进程的（跨进程的 via=redis 这里动不了）、
    # 又没被本轮重启的 → 自动修修不了
    snap = worker_registry.snapshot() or {}
    not_restartable = sorted(
        name for name, info in snap.items()
        if not info.get("alive") and "via" not in info and name not in restarted
    )
    detail = f"重启了 {len(restarted)} 个后台线程：{', '.join(restarted) or '无'}。"
    if not_restartable:
        detail += (
            f"以下 {len(not_restartable)} 个线程没有登记重启回调，自动修不了"
            f"（{', '.join(not_restartable)}），请按手动步骤重启对应容器。"
        )
    return {
        "restarted": list(restarted),
        "not_restartable": not_restartable,
        "detail": detail,
    }


FIXES: dict[str, FixSpec] = {
    "disk_space": FixSpec(
        rule_id="disk_space",
        name="清理过期缓存",
        description="删除遗留的转码临时目录和过期字幕缓存（都是可再生的临时文件，"
                    "在跑的任务不动），释放磁盘空间。",
        confirm_text="将清理遗留转码目录与过期字幕缓存（可再生的临时文件，"
                     "在跑的任务不受影响）。确认执行吗？",
        run=_fix_disk_space,
    ),
    "probe_queue": FixSpec(
        rule_id="probe_queue",
        name="重置卡住的探测任务",
        description="把租约过期、卡住的探测任务放回排队等待重探（正在正常跑的不动）。"
                    "操作可重跑，最坏只是部分条目被重新探测一次。",
        confirm_text="将把卡住的探测任务放回排队（只动租约过期的，正在跑的不动）。"
                     "确认执行吗？",
        run=_fix_probe_stuck,
    ),
    "workers_alive": FixSpec(
        rule_id="workers_alive",
        name="重启停滞的后台线程",
        description="立即跑一轮 worker 监督：只重启登记了自动重启回调的已死线程"
                    "（和每 30 秒自动监督做的事一样）。没有重启回调的线程不会动，"
                    "需要手动重启容器。",
        confirm_text="将立即重启已死的后台线程（只重启登记了自动重启的，"
                     "不动其他）。确认执行吗？",
        run=_fix_workers_dead,
    ),
}


# ---------------------------------------------------------------------------
# 不许自动修的规则：只给原因 + 手动步骤
# ---------------------------------------------------------------------------

MANUAL_ONLY: dict[str, dict] = {
    "db_health": {
        "reason": "数据库连不上通常是服务或配置问题，自动改配置可能让情况更糟。",
        "steps": ["检查数据库容器/服务是否在运行",
                  "检查 DATABASE_URL 等配置是否正确",
                  "看后端启动日志里的连接报错"],
    },
    "probe_breakers": {
        "reason": "熔断是 Google 配额的保护机制，提前解除可能触发 Google 风控。",
        "steps": ["不用手动干预：熔断到期会自动恢复",
                  "如果反复熔断：检查该挂载的请求是否过于集中，错开高峰"],
    },
    "enrich_stall": {
        "reason": "停滞多因 TMDB 配额或网络问题，自动重试可能烧掉配额。",
        "steps": ["检查 enrich worker 是否存活",
                  "看补全日志有没有 TMDB 配额/网络报错",
                  "确认夜间模式时间窗口配置是否正确"],
    },
    "scan_failed": {
        "reason": "扫描失败要先看日志确认原因，盲目重扫可能重复失败。",
        "steps": ["先看该库的扫描日志，确认是进程重启打断还是真报错",
                  "如果是进程重启打断：手动触发一次增量扫描即可",
                  "如果反复失败：检查挂载/源目录是否可访问"],
    },
    "sa_health": {
        "reason": "补 SA 密钥文件是敏感操作，必须人工确认。",
        "steps": ["去「Google Drive 账号」页检查 SA 配置",
                  "确认 SA 密钥文件还在、没有被删"],
    },
    "api_errors": {
        "reason": "接口报错根因未知，自动处理可能掩盖真正的问题。",
        "steps": ["看后端日志，按时间对上错误高峰",
                  "检查数据库/Redis 是否正常",
                  "如果刚部署完：观察 10 分钟，看是否回落"],
    },
}


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def get_fix(rule_id: str) -> FixSpec | None:
    """取某条规则的修复规格；不在白名单返回 None。"""
    return FIXES.get(rule_id)


def is_fixable(rule_id: str) -> bool:
    """该规则是否支持一键修复（白名单）。"""
    return rule_id in FIXES


def list_fixes() -> dict:
    """给 API / 前端用的修复动作清单（不含 run 函数）。"""
    return {
        rid: {"name": s.name, "description": s.description,
              "confirm_text": s.confirm_text}
        for rid, s in FIXES.items()
    }
