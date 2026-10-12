# -*- coding: utf-8 -*-
"""一键体检：只读诊断规则引擎（阶段一）

## 为什么要做

「健康一页」让服主看得见红黄灯，但灯亮了之后「是什么、严不严重、该查哪」
还是要翻日志猜。这个模块把 9 条高频故障模式写成规则：点一下，跑一遍，
输出大白话结论 + 检查步骤。

## 设计原则

1. **只读**：诊断永不写库、不改配置、不重启任何东西。所有数据走
   ``observability.probe_subsystems``（只读探针，不打 Drive API）。
2. **宁可漏报不要误报**：证据不足（探针没拿到数据）时，规则如实说
   "暂无数据、未发现异常"，不猜、不吓人。
3. **阈值全可配置**：``DIAG_*`` 环境变量，默认值只是兜底（铁律：只做
   能力，不做死业务逻辑）。跟 system_alerts 的 ``ALERT_*`` 一样，
   不进 env.example（出厂默认即可）。
4. **每条规则永远给结论**：正常也说一句"正常"，报告是一份完整清单，
   不是只有出问题才说话。
5. **文案大白话**：title 一句话说清；description 讲"是什么 + 影响"；
   steps 讲"先查哪、再查哪"。

## 阶段划分

- 阶段一（本模块）：只读诊断。
- 阶段二（``backend/diagnosis_fix.py`` + ``POST /api/admin/diagnosis/fix``）：
  白名单修复动作，每项二次确认 + 写审计日志。
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

__all__ = [
    "LEVELS",
    "RULES",
    "Finding",
    "run_diagnosis",
    "get_threshold",
]


# ---------------------------------------------------------------------------
# 阈值（全部可配置，默认值只是兜底）
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, str(default))))
    except (ValueError, TypeError):
        return default


# 规则 id → 环境变量名 → 默认值（文档即契约）
THRESHOLD_DEFS: dict[str, tuple[str, float]] = {
    "disk_warn_pct": ("DIAG_DISK_WARN_PCT", 15.0),       # 磁盘余量低于此 → 注意
    "disk_crit_pct": ("DIAG_DISK_CRIT_PCT", 10.0),       # 磁盘余量低于此 → 严重
    "probe_queue_warn": ("DIAG_PROBE_QUEUE_WARN", 5000),  # 探测待办超过此 → 注意
    "enrich_backlog_floor": ("DIAG_ENRICH_BACKLOG_FLOOR", 1000),  # 积压少于此不算停滞
    "enrich_idle_sec": ("DIAG_ENRICH_IDLE_SEC", 7200),   # 积压且这么久无产出 → 停滞
    "api_error_ratio": ("DIAG_API_ERROR_RATIO", 0.05),   # API 错误率超过此 → 注意
}


def get_threshold(rule_id: str) -> float:
    """取某条规则的当前阈值（环境变量 > 默认值）。"""
    env_name, default = THRESHOLD_DEFS[rule_id]
    return _env_float(env_name, default)


# ---------------------------------------------------------------------------
# 结论结构
# ---------------------------------------------------------------------------

LEVELS = ("info", "warn", "critical")
_LEVEL_CN = {"info": "正常", "warn": "需要注意", "critical": "严重"}


def Finding(
    rule_id: str,
    name: str,
    level: str,
    title: str,
    description: str,
    steps: Optional[list] = None,
    evidence: Optional[dict] = None,
) -> dict:
    assert level in LEVELS, f"未知 level: {level}"
    return {
        "rule_id": rule_id,
        "name": name,
        "level": level,
        "level_cn": _LEVEL_CN[level],
        "title": title,
        "description": description,
        "steps": steps or [],
        "evidence": evidence or {},
    }


def _metrics(subs: dict, key: str) -> dict:
    return (subs.get(key) or {}).get("metrics") or {}


def _status(subs: dict, key: str) -> str:
    return (subs.get(key) or {}).get("status") or "unknown"


# ---------------------------------------------------------------------------
# 规则（每条：subs → Finding；证据不足时如实说，不猜）
# ---------------------------------------------------------------------------

def _rule_db(subs: dict) -> dict:
    st = _status(subs, "database")
    if st == "down":
        msg = (subs.get("database") or {}).get("message", "")
        return Finding(
            "db_health", "数据库", "critical",
            "数据库连不上",
            f"后端连不上数据库，所有读写都会失败，这是最严重的一类问题。{msg}",
            ["检查数据库容器/服务是否在运行",
             "检查 DATABASE_URL 配置是否正确",
             "看后端启动日志里的连接报错"],
            {"status": st},
        )
    return Finding(
        "db_health", "数据库", "info",
        "数据库连通正常",
        "数据库可以正常读写。",
        [],
        {"status": st},
    )


def _rule_disk(subs: dict) -> dict:
    m = _metrics(subs, "disk")
    free = m.get("disk_free_percent")
    if free is None:
        return Finding(
            "disk_space", "磁盘空间", "warn",
            "磁盘信息读不到",
            "没能读到磁盘余量，无法判断空间是否够用（可能是采集异常，不是磁盘真有问题）。",
            ["去服务器上看一眼磁盘用量",
             "如果磁盘其实正常，不用管，下次体检会自动恢复"],
            {},
        )
    free_f = float(free)
    crit = get_threshold("disk_crit_pct")
    warn = get_threshold("disk_warn_pct")
    ev = {"disk_free_percent": round(free_f, 1),
          "transcode_sessions": m.get("transcode_sessions", 0)}
    if free_f < crit:
        return Finding(
            "disk_space", "磁盘空间", "critical",
            f"磁盘快满了，只剩 {free_f:.1f}%",
            "磁盘余量极低，转码、下载、缩略图都可能写不进去，随时可能出故障。",
            ["立即清理：删旧转码缓存、旧日志、没用的备份",
             "转码会话较多时先停掉不紧急的转码任务",
             "长期看：加磁盘或把缓存目录迁到大盘"],
            ev,
        )
    if free_f < warn:
        return Finding(
            "disk_space", "磁盘空间", "warn",
            f"磁盘余量偏低，还剩 {free_f:.1f}%",
            "还能用，但已经进了预警线，再涨文件可能会不够写。",
            ["清理转码缓存和旧日志",
             "关注增长趋势，提前规划扩容"],
            ev,
        )
    return Finding(
        "disk_space", "磁盘空间", "info",
        f"磁盘余量充足（{free_f:.0f}%）",
        "磁盘空间够用。",
        [],
        ev,
    )


def _rule_probe(subs: dict) -> dict:
    m = _metrics(subs, "probe")
    pending = m.get("pending_ready")
    if pending is None:
        return Finding(
            "probe_queue", "探测队列", "info",
            "探测状态暂无数据",
            "没读到探测队列数据，无法判断（可能是探测器没开）。",
            [],
            {},
        )
    pending = int(pending)
    running = bool(m.get("running"))
    paused = bool(m.get("paused"))
    thr = get_threshold("probe_queue_warn")
    ev = {"pending_ready": pending, "retrying": m.get("retrying", 0),
          "running": running, "paused": paused,
          "rate_per_min": m.get("rate_per_min"),
          "eta_hours": m.get("eta_hours")}
    # 最严重：有活儿但没人干
    if pending > 0 and not running and not paused:
        return Finding(
            "probe_queue", "探测队列", "critical",
            f"探测器没在跑，但有 {pending} 条在排队",
            "待探测的任务在积压，但探测 worker 没有运行，队列只会越堆越多。",
            ["检查 worker 容器/进程是否存活",
             "看 worker 日志有没有报错或被 OOM 杀掉",
             "确认探测总开关没有被关掉"],
            ev,
        )
    if pending >= thr:
        eta = m.get("eta_hours")
        eta_txt = f"，按当前速度约 {eta:.1f} 小时消化" if eta else ""
        return Finding(
            "probe_queue", "探测队列", "warn",
            f"探测队列积压 {pending} 条",
            f"待探测任务较多{eta_txt}。不影响已探测过的内容播放，只是新内容的信息会来得慢一些。",
            ["不用急：队列在正常消化，等它跑完",
             "如果一直涨：检查 worker 速率，或错开扫描高峰"],
            ev,
        )
    return Finding(
        "probe_queue", "探测队列", "info",
        "探测队列正常" if pending == 0 else f"探测队列少量排队（{pending} 条）",
        "探测任务在正常消化。" if pending else "没有待探测任务。",
        [],
        ev,
    )


def _rule_breakers(subs: dict) -> dict:
    m = _metrics(subs, "probe")
    breakers = m.get("open_breakers") or []
    if breakers:
        names = "、".join(str(b) for b in breakers[:5])
        more = f"等 {len(breakers)} 个" if len(breakers) > 5 else ""
        return Finding(
            "probe_breakers", "SA 熔断", "warn",
            f"有挂载的 SA 被熔断（{more or names}）",
            "部分挂载的 Google 账号触发了限流/配额保护，被暂时熔断。熔断是保护机制，"
            "不是故障；相关挂载的探测会变慢，但不会丢数据。",
            ["不用手动干预：熔断到期会自动恢复",
             "如果反复熔断：检查该挂载的请求是否过于集中，错开高峰"],
            {"open_breakers": breakers},
        )
    return Finding(
        "probe_breakers", "SA 熔断", "info",
        "没有熔断中的挂载",
        "所有挂载的 SA 状态正常。",
        [],
        {},
    )


def _enrich_night_paused(db) -> bool:
    """enrich 夜间模式且当前不在窗口内 → 白天零产出是预期的。"""
    try:
        from backend.emby_server import enrich_worker
        cfg = enrich_worker.get_episode_night_config(db)
        return bool(cfg.get("enabled")) and not bool(cfg.get("in_window"))
    except Exception:  # noqa: BLE001 — 读不到配置按"不暂停"处理（宁可误报不漏报）
        return False


def _rule_enrich(subs: dict, db) -> dict:
    m = _metrics(subs, "enrich")
    backlog = int(m.get("backlog") or 0)
    idle_sec = m.get("idle_sec")
    floor = int(get_threshold("enrich_backlog_floor"))
    idle_thr = get_threshold("enrich_idle_sec")
    ev = {"backlog": backlog, "idle_sec": idle_sec,
          "done_per_min": m.get("done_per_min", 0)}
    if _enrich_night_paused(db):
        return Finding(
            "enrich_stall", "元数据补全", "info",
            "补全在夜间模式，白天休息是正常的",
            f"当前积压 {backlog} 条，但补全配置为夜间运行，白天零产出是预期行为，"
            "不是故障。凌晨窗口到了会自动开工。",
            [],
            ev,
        )
    if backlog >= floor and idle_sec is not None and float(idle_sec) >= idle_thr:
        hrs = float(idle_sec) / 3600
        return Finding(
            "enrich_stall", "元数据补全", "warn",
            f"补全停滞：{backlog} 条积压、{hrs:.1f} 小时无产出",
            "补全队列有积压且长时间没有产出，海报/简介等元数据不会更新，"
            "但不影响播放。",
            ["检查 enrich worker 是否存活",
             "看补全日志有没有 TMDB 配额/网络报错",
             "确认夜间模式时间窗口配置是否正确"],
            ev,
        )
    return Finding(
        "enrich_stall", "元数据补全", "info",
        "补全运行正常" if backlog else "补全队列为空",
        f"当前积压 {backlog} 条，在正常消化。" if backlog else "没有待补全任务。",
        [],
        ev,
    )


def _rule_scan(subs: dict) -> dict:
    m = _metrics(subs, "scan")
    failed = int(m.get("failed") or 0)
    scanning = int(m.get("scanning") or 0)
    if failed > 0:
        names = "、".join(
            str(x.get("name", "?")) for x in (m.get("last_failed") or [])[:3])
        return Finding(
            "scan_failed", "媒体扫描", "warn",
            f"{failed} 个库扫描失败" + (f"（{names}）" if names else ""),
            "有媒体库的扫描没跑完，可能是进程重启打断的（数据一般不丢），"
            "也可能是源目录出了问题。",
            ["先看该库的扫描日志，确认是进程重启打断还是真报错",
             "如果是进程重启打断：手动触发一次增量扫描即可",
             "如果反复失败：检查挂载/源目录是否可访问"],
            {"failed": failed, "last_failed": m.get("last_failed") or []},
        )
    if scanning > 0:
        return Finding(
            "scan_failed", "媒体扫描", "info",
            f"{scanning} 个库正在扫描",
            "扫描进行中，属正常状态。",
            [],
            {"scanning": scanning},
        )
    return Finding(
        "scan_failed", "媒体扫描", "info",
        "扫描状态正常",
        "没有失败的扫描任务。",
        [],
        {},
    )


def _rule_sa(subs: dict) -> dict:
    m = _metrics(subs, "sa_quota")
    count = m.get("sa_count")
    if count is None:
        return Finding(
            "sa_health", "SA 账号", "info",
            "SA 状态暂无数据",
            "没读到 SA 账号数据，无法判断。",
            [],
            {},
        )
    if int(count) == 0:
        return Finding(
            "sa_health", "SA 账号", "critical",
            "一个可用的 SA 账号都没有",
            "读云盘全靠 SA 账号轮询，一个都没配好的话，Drive 相关功能（扫描、"
            "探测、播放）都会不可用。",
            ["去「Google Drive 账号」页检查 SA 配置",
             "确认 SA 密钥文件还在、没有被删"],
            {"sa_count": 0},
        )
    return Finding(
        "sa_health", "SA 账号", "info",
        f"{count} 个 SA 就绪",
        "SA 账号数量正常。（真实配额水位不主动查询，避免探针自己触发 Google 风控。）",
        [],
        {"sa_count": count},
    )


def _rule_api(subs: dict) -> dict:
    m = _metrics(subs, "api")
    ratio = m.get("global_error_ratio")
    if ratio is None:
        return Finding(
            "api_errors", "接口指标", "info",
            "接口指标暂无数据",
            "进程内还没有采集到足够的请求样本（刚重启或流量极低时常见）。",
            [],
            {},
        )
    ratio_f = float(ratio)
    thr = get_threshold("api_error_ratio")
    p99 = m.get("global_p99_ms") or 0
    ev = {"global_error_ratio": ratio_f, "global_p99_ms": p99}
    if ratio_f >= thr:
        return Finding(
            "api_errors", "接口指标", "warn",
            f"接口错误率偏高（{ratio_f:.1%}）",
            "最近接口请求中 5xx 偏多，可能是后端某个环节在报错，用户端会感到卡顿或失败。",
            ["看后端日志，按时间对上错误高峰",
             "检查数据库/Redis 是否正常",
             "如果刚部署完：观察 10 分钟，看是否回落"],
            ev,
        )
    return Finding(
        "api_errors", "接口指标", "info",
        "接口指标正常",
        f"错误率 {ratio_f:.2%}，P99 延迟 {p99:.0f}ms。",
        [],
        ev,
    )


def _rule_workers(subs: dict) -> dict:
    m = _metrics(subs, "workers")
    dead = m.get("dead") or []
    total = m.get("total")
    if dead:
        names = "、".join(str(d) for d in dead[:5])
        return Finding(
            "workers_alive", "后台线程", "warn",
            f"后台线程有死亡：{names}",
            "有后台 worker 线程挂了，它负责的那块活（扫描/探测/补全等）会停滞，"
            "直到进程重启。",
            ["重启 worker/api 容器让线程重建",
             "看日志找线程死亡的原因（异常/ OOM）"],
            {"total": total, "dead": dead},
        )
    if total is None:
        return Finding(
            "workers_alive", "后台线程", "info",
            "后台线程状态暂无数据",
            "没读到 worker 状态，无法判断。",
            [],
            {},
        )
    return Finding(
        "workers_alive", "后台线程", "info",
        f"{total} 个后台线程存活",
        "后台线程都在正常工作。",
        [],
        {"total": total},
    )


# 规则注册表：新增规则只加一行（函数签名：subs → Finding；
# 需要 db 的规则用 (subs, db) 并在 _NEEDS_DB 登记）
_RULES: list[tuple[str, str, Callable]] = [
    ("db_health", "数据库", _rule_db),
    ("disk_space", "磁盘空间", _rule_disk),
    ("probe_queue", "探测队列", _rule_probe),
    ("probe_breakers", "SA 熔断", _rule_breakers),
    ("enrich_stall", "元数据补全", _rule_enrich),
    ("scan_failed", "媒体扫描", _rule_scan),
    ("sa_health", "SA 账号", _rule_sa),
    ("api_errors", "接口指标", _rule_api),
    ("workers_alive", "后台线程", _rule_workers),
]

_NEEDS_DB = {"enrich_stall"}

# 对外暴露规则清单（UI 展示"本次检查了哪些项"用）
RULES = [{"rule_id": rid, "name": name} for rid, name, _ in _RULES]


def run_diagnosis(db) -> dict:
    """跑一轮只读诊断，返回大白话报告。

    只读保证：本函数只调 observability.probe_subsystems（只读探针）
    和 enrich 夜间配置读取，不写库、不改配置、不重启任何东西。
    单个规则异常只跳过该规则，不影响其他。
    """
    from backend.emby_server import observability

    try:
        subs = observability.probe_subsystems(db)
    except Exception as exc:  # noqa: BLE001 — 探针整体失败也给一份报告
        logger.warning("体检：探针执行失败: %s", exc)
        subs = {}

    findings: list[dict] = []
    for rule_id, name, fn in _RULES:
        try:
            if rule_id in _NEEDS_DB:
                findings.append(fn(subs, db))
            else:
                findings.append(fn(subs))
        except Exception:  # noqa: BLE001 — 单个规则异常不影响其他规则
            logger.warning("体检规则 %s 执行异常", rule_id, exc_info=True)
            findings.append(Finding(
                rule_id, name, "info",
                "该项检查暂不可用",
                "检查过程中出了点小问题，没拿到结论，不代表有故障。",
                ["稍后重新体检一次"],
                {},
            ))

    counts = {"info": 0, "warn": 0, "critical": 0}
    for f in findings:
        counts[f["level"]] = counts.get(f["level"], 0) + 1

    return {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": len(findings),
            "info": counts["info"],
            "warn": counts["warn"],
            "critical": counts["critical"],
        },
        "findings": findings,
        "note": "只读体检：本次检查没有修改任何配置、没有重启任何服务。"
                "标\"需要注意\"/\"严重\"的项：带「一键修复」按钮的是安全可自动修的"
                "（执行前二次确认、每次执行写审计日志），其余请按步骤手动处理。",
    }
