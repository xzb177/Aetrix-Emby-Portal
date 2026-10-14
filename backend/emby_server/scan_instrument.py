"""扫描进度上报的接入层（v2.27.0）

``scanner.py`` 的扫描主体（批次化写库、增量指纹、清理决策）是这个项目里**最容易改坏**的
一段：每次动扫描逻辑，都得回头人工确认「进度上报点还在不在」——上报点散了，迟早会掉一个。
所以进度上报不往主体里插行，而是**包装**接入：装一次（``install``，幂等），给四个点套壳：

- ``_FileCounter.__iter__``：来源里发现一个文件就 +1——枚举阶段对外的可见性
  （「四个库都 running、item_count=0」时，至少能看出还在枚举还是卡在解析）；
- ``_iter_prepared``：真正交给写库循环的文件数（processed）与「当前目录」；
- ``_scan_library_body``：把阶段切回「枚举」（新一轮开始）；
- ``_remove_missing_items``：阶段切到「清理」。

上报的是**哪个库**？当前线程绑定的那个（``scan_progress.current_library_id()``，由
``scan_queue`` 开扫前绑定、结束后解绑）。一个扫描任务全程只在它自己的工作线程里跑，
所以线程绑定足够且不会串库；没有绑定（单测直接调 ``scan_library_sync``）时包装层全部空转——
直接跑扫描的行为与升级前完全一致。

包装只是「观测」：它不改参数、不改返回值、失败也不吞异常；``install()`` 可重复调用。
"""
from __future__ import annotations

import functools
import logging
import os
import threading

from backend.emby_server import scan_progress as progress

logger = logging.getLogger(__name__)

_INSTALL_LOCK = threading.Lock()
_INSTALLED = False


def installed() -> bool:
    return _INSTALLED


def install() -> bool:
    """装一次进度上报（返回 True 表示本次真的装了，False = 之前已装）"""
    global _INSTALLED
    with _INSTALL_LOCK:
        if _INSTALLED:
            return False
        from backend.emby_server import scanner  # 延迟导入：本模块在启动早期就会被调用

        _wrap_file_counter(scanner._FileCounter)
        scanner._iter_prepared = _wrap_iter_prepared(scanner._iter_prepared)
        scanner._scan_library_body = _wrap_body(scanner._scan_library_body)
        scanner._remove_missing_items = _wrap_cleanup(scanner._remove_missing_items)
        _INSTALLED = True
        logger.info("扫描进度上报已接入（枚举 / 处理 / 阶段）")
        return True


def _wrap_file_counter(counter_cls) -> None:
    original = counter_cls.__iter__
    if getattr(original, "_rb_progress_wrapped", False):
        return

    @functools.wraps(original)
    def __iter__(self):
        library_id = progress.current_library_id()
        for item in original(self):
            if library_id is not None and self.total % progress.ENUMERATED_EVERY == 0:
                progress.set_enumerated(library_id, self.total)
            yield item
        if library_id is not None:
            # 收尾补一次：来源里的文件数不是 ENUMERATED_EVERY 的整数倍时，最后几个也要算上
            progress.set_enumerated(library_id, self.total)

    __iter__._rb_progress_wrapped = True  # type: ignore[attr-defined]
    counter_cls.__iter__ = __iter__


def _wrap_iter_prepared(fn):
    if getattr(fn, "_rb_progress_wrapped", False):
        return fn

    @functools.wraps(fn)
    def wrapper(ctx, files, pool, db, process_one, label, counted, stats, failed_roots):
        library_id = progress.current_library_id()
        first = True

        def _wrapped_process(scan_file, pending):
            nonlocal first
            if library_id is not None:
                if first:
                    progress.set_phase(library_id, "processing")
                    first = False
                # 死锁重试会把同一批重走一遍：进度上报是近似值，重复上报无害
                progress.note_processed(library_id, current=_label(scan_file))
            return process_one(scan_file, pending)

        return fn(ctx, files, pool, db, _wrapped_process,
                  label, counted, stats, failed_roots)

    wrapper._rb_progress_wrapped = True  # type: ignore[attr-defined]
    return wrapper


def _wrap_body(fn):
    if getattr(fn, "_rb_progress_wrapped", False):
        return fn

    @functools.wraps(fn)
    def wrapper(db, library, snap):
        library_id = progress.current_library_id() or getattr(library, "id", None)
        if library_id is not None:
            progress.set_phase(library_id, "enumerating")
        return fn(db, library, snap)

    wrapper._rb_progress_wrapped = True  # type: ignore[attr-defined]
    return wrapper


def _wrap_cleanup(fn):
    if getattr(fn, "_rb_progress_wrapped", False):
        return fn

    @functools.wraps(fn)
    def wrapper(db, library, seen_guids):
        library_id = progress.current_library_id() or getattr(library, "id", None)
        if library_id is not None:
            progress.set_phase(library_id, "cleanup")
        return fn(db, library, seen_guids)

    wrapper._rb_progress_wrapped = True  # type: ignore[attr-defined]
    return wrapper


def _label(scan_file) -> str:
    """「当前在哪」：远程挂载显示 mount://<id>/<目录>，本机显示目录（不含文件名）"""
    path = str(getattr(scan_file, "stored_path", "") or "")
    return (os.path.dirname(path) or path)[:progress.CURRENT_MAX]
