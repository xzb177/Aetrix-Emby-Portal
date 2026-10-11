"""执行层：并发生成/更新/删除 .strm。

D-FLOW-1: 所有写出经临时文件 + fsync + 原子 rename，禁止直接覆盖。
D-FLOW-2: 删除策略可配 retain/trash/delete，默认 trash。
"""

from __future__ import annotations

import logging
import os
import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Optional

from .models import FileState, Plan, PlanItem
from .scheduler import SARotator
from .state_db import StateDB

log = logging.getLogger(__name__)


class Executor:
    """Plan 执行器。"""

    def __init__(self, state: StateDB, strm_root: str, url_template: str,
                 delete_policy: str = "trash", trash_dir: str = "",
                 workers: int = 16, rotator: Optional[SARotator] = None,
                 dry_run: bool = False):
        self._state = state
        self._root = strm_root
        self._url_tpl = url_template
        self._policy = delete_policy
        self._trash = trash_dir or os.path.join(strm_root, ".trash")
        self._workers = workers
        self._rotator = rotator
        self._dry_run = dry_run

    def _url(self, file_id: str, record=None) -> str:
        ctx = {"file_id": file_id}
        if record:
            ctx.update(name=record.name, size=str(record.size),
                       md5=record.md5 or "")
        try:
            return self._url_tpl.format(**ctx)
        except KeyError as e:
            # D-VERIFY-1 L1: 模板变量缺失是致命配置错误，快速失败
            raise RuntimeError(f"url_template 缺少变量 {e}: {self._url_tpl}")

    def _safe_path(self, rel_path: str) -> str:
        """P1 修复：路径遍历防护。确保最终路径在 strm_root 内。"""
        # 规范化并确保不逃逸
        full = os.path.normpath(os.path.join(self._root, rel_path))
        root = os.path.normpath(self._root)
        if not (full == root or full.startswith(root + os.sep)):
            raise RuntimeError(f"路径遍历拦截: {rel_path}")
        return full

    def _atomic_write(self, rel_path: str, content: str) -> None:
        """D-FLOW-1: tmp + fsync + rename 原子写出。"""
        full = self._safe_path(rel_path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        tmp = full + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, full)  # 原子替换

    def _ensure_shared(self, file_id: str) -> bool:
        """确保文件公开（复用 v1 能力，token 按 SA 独立缓存）。"""
        if not self._rotator:
            return True  # 无 SA 配置时跳过
        import json
        import urllib.request
        import urllib.error
        key = self._rotator.next()
        if not key:
            return False
        token = key.get_token()
        if not token:
            return False
        url = (f"https://www.googleapis.com/drive/v3/files/{file_id}"
               f"/permissions?supportsAllDrives=true")
        body = json.dumps({"type": "anyone", "role": "reader"}).encode()
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30):
                return True
        except urllib.error.HTTPError as e:
            if e.code == 409:
                return True
            return False
        except Exception:
            return False

    def _do_item(self, item: PlanItem) -> tuple[str, str]:
        """执行单个 PlanItem，返回 (action, strm_path)。"""
        if self._dry_run:
            return item.action, item.new_strm_path or item.old_strm_path or ""

        try:
            if item.action in ("create", "update"):
                rec = item.record
                assert rec is not None
                if not self._ensure_shared(rec.file_id):
                    log.warning("公开分享失败 %s，直链可能无法播放", rec.path)
                url = self._url(rec.file_id, rec)
                # D-VERIFY-1 L1: URL 格式校验（零成本）
                if not url.startswith(("http://", "https://")):
                    raise RuntimeError(f"URL 格式非法: {url[:80]}")
                self._atomic_write(item.new_strm_path, url + "\n")
                self._state.upsert(FileState(
                    file_id=rec.file_id, path=rec.path, md5=rec.md5,
                    size=rec.size, strm_path=item.new_strm_path,
                    status="done",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                ))
                return item.action, item.new_strm_path

            if item.action == "move":
                # D-INCR-2: 纯移动，内容复用，只搬位置
                old_full = os.path.join(self._root, item.old_strm_path)
                new_full = os.path.join(self._root, item.new_strm_path)
                os.makedirs(os.path.dirname(new_full), exist_ok=True)
                if os.path.exists(old_full):
                    os.replace(old_full, new_full)
                else:
                    # 旧文件不存在，按 create 处理
                    rec = item.record
                    url = self._url(rec.file_id, rec)
                    self._atomic_write(item.new_strm_path, url + "\n")
                rec = item.record
                self._state.upsert(FileState(
                    file_id=rec.file_id, path=rec.path, md5=rec.md5,
                    size=rec.size, strm_path=item.new_strm_path,
                    status="done",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                ))
                return "move", item.new_strm_path

            if item.action == "delete":
                self._handle_delete(item.old_strm_path)
                return "delete", item.old_strm_path

        except Exception as e:
            log.error("执行失败 [%s] %s: %s",
                      item.action, item.new_strm_path or item.old_strm_path, e)
            raise
        return item.action, ""

    def _handle_delete(self, strm_path: str) -> None:
        """D-FLOW-2: 按 delete_policy 处置。"""
        full = os.path.join(self._root, strm_path)
        if self._policy == "retain":
            log.info("retain 模式，保留 %s", strm_path)
            return
        if not os.path.exists(full):
            return
        if self._policy == "trash":
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            dest = os.path.join(self._trash, today, strm_path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(full, dest)
            log.info("已移入回收站: %s", strm_path)
        else:  # delete
            os.remove(full)
            log.info("已删除: %s", strm_path)

    def execute(self, plan: Plan, checkpoint_every: int = 500) -> dict[str, int]:
        """并发执行 Plan，每 checkpoint_every 提交一次状态。"""
        stats = {"create": 0, "update": 0, "move": 0, "delete": 0, "error": 0}
        if self._dry_run:
            for item in plan.items:
                log.info("[dry-run] %s: %s (%s)",
                         item.action, item.new_strm_path or item.old_strm_path,
                         item.reason)
                stats[item.action] = stats.get(item.action, 0) + 1
            return stats

        done = 0
        with ThreadPoolExecutor(max_workers=self._workers) as pool:
            futures = [pool.submit(self._do_item, item)
                       for item in plan.items]
            for fut in futures:
                try:
                    action, _ = fut.result()
                    stats[action] = stats.get(action, 0) + 1
                except Exception:
                    stats["error"] += 1
                done += 1
                # D-INCR-5: 页级 checkpoint
                if done % checkpoint_every == 0:
                    self._state.commit()
                    log.info("checkpoint: 已执行 %d/%d", done, len(plan.items))
        self._state.commit()
        return stats
