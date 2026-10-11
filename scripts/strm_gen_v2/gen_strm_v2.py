#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_strm_v2: .strm 生成同步服务核心（v2 重构版）。

8层架构（详见 DESIGN.md）：
  采集 Collector → 状态 StateDB → 规划 Planner → 执行 Executor
  校验 Verifier ←→ 规则引擎（脏数据）
  调度 Scheduler（横切：分片/checkpoint/并发/限流/抖动）
  报告 Reporter（脏数据/校验/运行报告）

核心流程：
  init → snapshot → diff → plan → execute → verify → report

用法：
  python3 gen_strm_v2.py --config config/strm_gen.yaml
  python3 gen_strm_v2.py --dry-run          # 干跑：只出 Plan 与报告
  python3 gen_strm_v2.py --verify-only      # 只跑校验
  python3 gen_strm_v2.py --shard 电影        # 只跑指定库
  python3 gen_strm_v2.py --full              # 强制全量（忽略增量游标）
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

# 允许直接运行（无需安装）
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from strm_v2.config_loader import Config, ConfigError
from strm_v2.state_db import StateDB
from strm_v2.scheduler import SARotator, TokenBucket
from strm_v2.collector import Collector, DriveClient
from strm_v2.planner import Planner
from strm_v2.rules_engine import RulesEngine
from strm_v2.executor import Executor
from strm_v2.verifier import Verifier
from strm_v2.reporter import Reporter


def setup_logging(level: str, log_file: str = "") -> None:
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="gen_strm_v2: .strm 同步服务核心")
    ap.add_argument("--config", default="config/strm_gen.yaml",
                    help="主配置文件路径")
    ap.add_argument("--dry-run", action="store_true",
                    help="干跑：只出 Plan 与报告，不写盘 (D-CONF-2)")
    ap.add_argument("--verify-only", action="store_true", help="只跑校验层")
    ap.add_argument("--full", action="store_true", help="强制全量采集")
    ap.add_argument("--shard", action="append", default=[],
                    help="只跑指定分片（库名），可多次指定")
    ap.add_argument("--resume", action="store_true", default=True,
                    help="断点续跑（默认开启）")
    args = ap.parse_args()

    # ── init: 加载配置 ──
    try:
        cfg = Config.load(args.config)
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2
    setup_logging(cfg.get("run", "log_level", default="INFO"),
                  cfg.get("run", "log_file", default=""))
    log = logging.getLogger("gen_strm_v2")

    dry_run = args.dry_run or cfg.dry_run
    shard_filter = set(args.shard) or None
    if not args.shard:
        cfg_shards = cfg.get("run", "shards", default=[])
        shard_filter = set(cfg_shards) or None

    log.info("gen_strm_v2 启动 (dry_run=%s, shards=%s)",
             dry_run, shard_filter or "全部")

    # ── init: 状态层 ──
    state = StateDB(cfg.state_db_path)
    # v1 迁移（D-INCR-6 兼容）
    v1_path = cfg.get("incremental", "v1_state_db", default="")
    if v1_path:
        n = state.migrate_from_v1(v1_path)
        if n:
            log.info("从 v1 状态库迁移 %d 条记录", n)

    # ── init: 调度层（SA 轮换 + 限流）──
    sa_dir = cfg.require("source", "sa_credentials_dir")
    cb = cfg.get("circuit_breaker", default={}) or {}
    rotator = SARotator(
        sa_dir,
        cooldown_seconds=cb.get("cooldown_seconds", 600),
        error_threshold=cb.get("error_threshold", 3),
    )
    rl = cfg.get("rate_limit", default={}) or {}
    drive_limiter = TokenBucket(
        qps=rl.get("drive_api_qps", 8.0),
        jitter_ratio=rl.get("jitter_ratio", 0.3),
    )
    verify_limiter = TokenBucket(
        qps=rl.get("verify_qps", 4.0),
        jitter_ratio=rl.get("jitter_ratio", 0.3),
    )
    log.info("SA 凭证 %d 个，Drive 限流 %.1f QPS", len(rotator),
             rl.get("drive_api_qps", 8.0))

    reporter = Reporter(
        cfg.require("report", "dir"),
        keep_days=cfg.get("report", "keep_days", default=30),
    )

    # ── verify-only 模式 ──
    if args.verify_only:
        return run_verify_only(cfg, state, reporter, verify_limiter, dry_run)

    # ── snapshot: 采集 ──
    client = DriveClient(
        rotator, drive_limiter,
        backoff_base=rl.get("backoff_base_seconds", 1.0),
        backoff_max=rl.get("backoff_max_seconds", 300.0),
    )
    collector = Collector(
        client, state,
        drive_root_id=cfg.require("source", "drive_root_id"),
        include_mime=cfg.get("source", "include_mime", default=["video/"]),
        exclude_pattern=cfg.get("source", "exclude_pattern", default=[]),
    )
    mode = cfg.get("incremental", "mode", default="incremental")
    is_full = args.full or mode == "full"
    if is_full:
        log.info("全量采集模式")
        records = collector.collect_full(shard_filter)
    else:
        log.info("增量采集模式（Changes API）")
        records = collector.collect_incremental()

    # ── diff + plan: 规划 ──
    rules = RulesEngine(
        cfg.resolve(cfg.get("dirty_data", "rules_file",
                            default="config/dirty_rules.yaml")),
        mirror_mode=cfg.get("dirty_data", "mirror_mode", default="strict"),
    )
    planner = Planner(state, rules,
                      strip_prefix=cfg.get("target", "strip_prefix",
                                           default="MoviePilot/"))
    plan = planner.plan(records, shard_filter, is_full=is_full)
    reporter.plan_summary(plan)
    if plan.dirty and cfg.get("report", "dirty_report", default=True):
        reporter.dirty_report(plan.dirty)

    if dry_run:
        log.info("dry-run 模式：到此止步，未写盘")
        state.close()
        return 0

    # ── execute: 执行 ──
    conc = cfg.get("concurrency", default={}) or {}
    executor = Executor(
        state,
        strm_root=cfg.strm_root,
        url_template=cfg.url_template,
        delete_policy=cfg.get("target", "delete_policy", default="trash"),
        trash_dir=cfg.get("target", "trash_dir", default=""),
        workers=conc.get("executor_workers", 16),
        rotator=rotator,
        dry_run=False,
    )
    stats = executor.execute(
        plan,
        checkpoint_every=cfg.get("incremental", "checkpoint_every",
                                 default=500),
    )
    log.info("执行完成: %s", stats)

    # ── verify: 校验 ──
    vcfg = cfg.get("verify", default={}) or {}
    new_paths = {i.new_strm_path for i in plan.items
                 if i.action in ("create", "update") and i.new_strm_path}
    if vcfg.get("mode", "sample") != "off":
        verifier = Verifier(
            state, cfg.strm_root, limiter=verify_limiter,
            mode=vcfg.get("mode", "sample"),
            sample_rate=vcfg.get("sample_rate", 0.05),
            sample_min_per_shard=vcfg.get("sample_min_per_shard", 20),
            newly_generated=vcfg.get("newly_generated", "always"),
            timeout=vcfg.get("timeout_seconds", 15),
            max_retries=vcfg.get("max_retries", 2),
            workers=conc.get("verifier_workers", 8),
        )
        all_paths = [i.new_strm_path for i in plan.items if i.new_strm_path]
        results = verifier.verify(all_paths, new_paths)
        if cfg.get("report", "verify_report", default=True):
            reporter.verify_report(results)
        stats["verify_failed"] = sum(1 for r in results if not r.passed)

    # ── report ──
    if cfg.get("report", "run_report", default=True):
        reporter.run_report({
            "mode": mode, "dry_run": dry_run,
            "shards": sorted(shard_filter) if shard_filter else "all",
            **stats,
            "dirty_hits": len(plan.dirty),
        })
    reporter.cleanup_old()
    state.close()
    log.info("gen_strm_v2 完成")
    return 0 if stats.get("error", 0) == 0 else 1


def run_verify_only(cfg: Config, state: StateDB, reporter: Reporter,
                    limiter: TokenBucket, dry_run: bool) -> int:
    """--verify-only: 独立运行校验层 (D-VERIFY-1)。"""
    log = logging.getLogger("gen_strm_v2")
    log.info("verify-only 模式：扫描本地 .strm 并校验")
    vcfg = cfg.get("verify", default={}) or {}
    conc = cfg.get("concurrency", default={}) or {}
    verifier = Verifier(
        state, cfg.strm_root, limiter=limiter,
        mode=vcfg.get("mode", "sample"),
        sample_rate=vcfg.get("sample_rate", 0.05),
        sample_min_per_shard=vcfg.get("sample_min_per_shard", 20),
        newly_generated="never",  # 独立运行时不强制
        timeout=vcfg.get("timeout_seconds", 15),
        max_retries=vcfg.get("max_retries", 2),
        workers=conc.get("verifier_workers", 8),
    )
    # 收集本地所有 .strm
    paths = []
    for root, _, files in os.walk(cfg.strm_root):
        for fn in files:
            if fn.endswith(".strm"):
                full = os.path.join(root, fn)
                paths.append(os.path.relpath(full, cfg.strm_root))
    log.info("本地 .strm 共 %d 个", len(paths))
    if dry_run:
        log.info("dry-run：跳过实际校验")
        return 0
    results = verifier.verify(paths)
    reporter.verify_report(results)
    state.close()
    n_fail = sum(1 for r in results if not r.passed)
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
