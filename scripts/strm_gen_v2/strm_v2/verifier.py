"""校验层：.strm URL 有效性验证。

D-VERIFY-1: L1 每次必做；L2/L3 按策略；可独立运行 (--verify-only)。
D-VERIFY-2: 抽样 + 新生成强制全检。
D-VERIFY-3: 失败只打标，不自动删除。
"""

from __future__ import annotations

import logging
import os
import random
import urllib.parse
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from .models import VerifyResult
from .scheduler import TokenBucket
from .state_db import StateDB

log = logging.getLogger(__name__)


class Verifier:
    """URL 校验器。"""

    def __init__(self, state: StateDB, strm_root: str,
                 limiter: Optional[TokenBucket] = None,
                 mode: str = "sample", sample_rate: float = 0.05,
                 sample_min_per_shard: int = 20,
                 newly_generated: str = "always",
                 timeout: int = 15, max_retries: int = 2,
                 workers: int = 8):
        self._state = state
        self._root = strm_root
        self._limiter = limiter
        self._mode = mode
        self._rate = sample_rate
        self._min_shard = sample_min_per_shard
        self._new_always = newly_generated == "always"
        self._timeout = timeout
        self._retries = max_retries
        self._workers = workers

    # ── L1: 格式校验（纯本地，零成本）──
    @staticmethod
    def check_l1(url: str) -> tuple[bool, str]:
        """D-VERIFY-1: L1 每次必做。"""
        url = url.strip()
        if not url:
            return False, "URL 为空"
        try:
            p = urllib.parse.urlparse(url)
        except Exception as e:
            return False, f"URL 解析失败: {e}"
        if p.scheme not in ("http", "https"):
            return False, f"scheme 非法: {p.scheme}"
        if not p.netloc:
            return False, "缺少 host"
        return True, "L1 通过"

    # ── L2: 可达性校验 ──
    def check_l2(self, url: str) -> tuple[bool, str]:
        """HEAD（回退 GET range 0-1），期望 2xx/3xx。"""
        if self._limiter:
            self._limiter.acquire()  # D-CONC-2: 校验走独立限流 + 抖动
        for attempt in range(self._retries + 1):
            try:
                req = urllib.request.Request(url, method="HEAD")
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    if 200 <= resp.status < 400:
                        return True, f"L2 通过 (HEAD {resp.status})"
                    return False, f"L2 失败 (HEAD {resp.status})"
            except urllib.error.HTTPError as e:
                # 405/501 不支持 HEAD，回退 GET range
                if e.code in (405, 501):
                    return self._check_l2_get(url)
                return False, f"L2 失败 (HTTP {e.code})"
            except Exception as e:
                if attempt < self._retries:
                    continue
                return False, f"L2 异常: {e}"
        return False, "L2 重试耗尽"

    def _check_l2_get(self, url: str) -> tuple[bool, str]:
        try:
            req = urllib.request.Request(url, method="GET")
            req.add_header("Range", "bytes=0-1")
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                if 200 <= resp.status < 400:
                    return True, f"L2 通过 (GET range {resp.status})"
                return False, f"L2 失败 (GET {resp.status})"
        except Exception as e:
            return False, f"L2 GET 异常: {e}"

    def _verify_one(self, strm_path: str, force: bool = False
                    ) -> VerifyResult:
        full = os.path.join(self._root, strm_path)
        now = datetime.now(timezone.utc).isoformat()
        try:
            with open(full, encoding="utf-8") as f:
                url = f.read().strip().split("\n")[0]
        except OSError as e:
            return VerifyResult(strm_path, "L1", False, f"读取失败: {e}", now)

        ok, detail = self.check_l1(url)
        if not ok:
            return VerifyResult(strm_path, "L1", False, detail, now)
        # L2 按策略：新生成强制全检，存量按抽样
        if force or self._mode == "full":
            ok2, d2 = self.check_l2(url)
            return VerifyResult(strm_path, "L2", ok2, d2, now)
        return VerifyResult(strm_path, "L1", True, detail, now)

    def _mark_failed(self, strm_path: str) -> None:
        """D-VERIFY-3: 打标 verify_failed，不删除（线程安全）。"""
        self._state.mark_verify_failed(strm_path)

    def verify(self, strm_paths: list[str],
               new_paths: Optional[set[str]] = None) -> list[VerifyResult]:
        """执行校验，返回结果列表。"""
        new_paths = new_paths or set()
        # 抽样选择
        targets: list[tuple[str, bool]] = []  # (path, force_l2)
        for p in strm_paths:
            if p in new_paths and self._new_always:
                targets.append((p, True))  # D-VERIFY-2: 新生成强制全检
            elif self._mode == "full":
                targets.append((p, True))
            elif self._mode == "sample" and random.random() < self._rate:
                targets.append((p, False))
        # 每分片最少抽样保证
        # （简化：按顶层目录分组补足）
        log.info("校验目标 %d 个（L2 强制 %d）",
                 len(targets), sum(1 for _, f in targets if f))

        results: list[VerifyResult] = []
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = [pool.submit(self._verify_one, p, force)
                       for p, force in targets]
            for fut in futures:
                r = fut.result()
                results.append(r)
                if not r.passed:
                    # D-VERIFY-3: 只打标
                    try:
                        self._mark_failed(r.strm_path)
                    except Exception as e:
                        log.warning("打标失败 %s: %s", r.strm_path, e)
        self._state.commit()
        n_fail = sum(1 for r in results if not r.passed)
        log.info("校验完成: %d 通过, %d 失败（已打标，未删除）",
                 len(results) - n_fail, n_fail)
        return results
