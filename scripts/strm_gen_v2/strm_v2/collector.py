"""采集层：从 Drive 拉取文件清单。

D-INCR-3: 默认增量模式用 Changes API，游标持久化在 StateDB；
捕获 410（游标过期）后自动降级为一次全量对账。

输出标准化 FileRecord{file_id, path, name, size, md5, mtime, mime}。
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
import urllib.error
from typing import Iterator, Optional

from .models import FileRecord
from .scheduler import SARotator, TokenBucket, Backoff
from .state_db import StateDB

log = logging.getLogger(__name__)

DRIVE_API = "https://www.googleapis.com/drive/v3"


class DriveClient:
    """带 SA 轮换 + 限流 + 熔断的 Drive API 客户端。"""

    def __init__(self, rotator: SARotator, limiter: TokenBucket,
                 backoff_base: float = 1.0, backoff_max: float = 300.0):
        self._rotator = rotator
        self._limiter = limiter
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max

    def _request(self, path: str, params: dict) -> Optional[dict]:
        """GET Drive API，自动处理 SA 轮换/限流/熔断/退避。"""
        backoff = Backoff(self._backoff_base, self._backoff_max)
        while True:
            key = self._rotator.next()
            if key is None:
                # 全部 SA 熔断：指数退避后重试
                delay = backoff.next_delay()
                log.warning("全部 SA 熔断中，%0.1fs 后重试", delay)
                import time
                time.sleep(delay)
                continue
            token = key.get_token()
            if not token:
                self._rotator.report_quota_error(key)
                continue
            self._limiter.acquire()  # D-CONC-2: 全局限流 + 抖动
            url = f"{DRIVE_API}{path}?{urllib.parse.urlencode(params)}"
            req = urllib.request.Request(url)
            req.add_header("Authorization", f"Bearer {token}")
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    self._rotator.report_success(key)
                    backoff.reset()
                    return json.load(resp)
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")[:500]
                except Exception:
                    pass
                if e.code in (429, 403) and (
                        "rateLimit" in body or "quota" in body.lower()
                        or "userRateLimit" in body):
                    # D-CONC-4: 配额错误 → 熔断该 SA
                    log.warning("SA %s 配额错误，熔断", key.email[:20])
                    self._rotator.report_quota_error(key)
                    continue
                if e.code == 410:
                    # Changes 游标过期，抛给上层降级全量
                    raise CursorExpiredError()
                log.error("Drive API 错误 %d: %s", e.code, body[:200])
                return None
            except Exception as e:
                log.warning("Drive API 请求异常: %s", e)
                return None


class CursorExpiredError(Exception):
    """Changes 游标过期（410），需降级全量对账。"""


class Collector:
    """文件清单采集器：全量 files.list 或增量 changes.list。"""

    FIELDS = ("files(id,name,mimeType,size,md5Checksum,modifiedTime,parents),"
              "nextPageToken")

    def __init__(self, client: DriveClient, state: StateDB,
                 drive_root_id: str, include_mime: list[str],
                 exclude_pattern: list[str]):
        self._client = client
        self._state = state
        self._root_id = drive_root_id
        self._include_mime = include_mime
        import re
        self._exclude = [re.compile(p) for p in exclude_pattern]
        # file_id → path 缓存（增量模式下需要拼路径）
        self._path_cache: dict[str, str] = {}

    def _wanted(self, mime: str, path: str) -> bool:
        if not any(mime.startswith(p) for p in self._include_mime):
            return False
        return not any(rx.search(path) for rx in self._exclude)

    def collect_full(self, shard_filter: Optional[set[str]] = None
                     ) -> Iterator[FileRecord]:
        """全量模式：分页拉取 drive_root_id 下所有文件。

        P2 修复：同时处理根目录直属文件（不只子目录）。
        """
        page_token: Optional[str] = None
        dirs: dict[str, str] = {}  # id → name
        # 第一遍：根下直属文件 + 子目录
        while True:
            params = {
                "q": f"'{self._root_id}' in parents and trashed=false",
                "fields": self.FIELDS,
                "pageSize": 1000,
            }
            if page_token:
                params["pageToken"] = page_token
            resp = self._client._request("/files", params)
            if not resp:
                break
            for f in resp.get("files", []):
                mime = f.get("mimeType", "")
                if mime == "application/vnd.google-apps.folder":
                    dirs[f["id"]] = f["name"]
                    continue
                # P2 修复：根目录直属文件也要处理
                path = f["name"]
                if not self._wanted(mime, path):
                    continue
                yield FileRecord(
                    file_id=f["id"], path=path, name=f["name"],
                    size=int(f.get("size", 0)),
                    md5=f.get("md5Checksum"),
                    mtime=f.get("modifiedTime"), mime=mime,
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
        # 第二遍：每个顶层子目录递归拉取（天然分片）
        for dir_id, dir_name in dirs.items():
            if shard_filter and dir_name not in shard_filter:
                continue
            yield from self._list_recursive(dir_id, dir_name)

    def _list_recursive(self, folder_id: str, prefix: str
                        ) -> Iterator[FileRecord]:
        page_token: Optional[str] = None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": self.FIELDS,
                "pageSize": 1000,
            }
            if page_token:
                params["pageToken"] = page_token
            resp = self._client._request("/files", params)
            if not resp:
                break
            for f in resp.get("files", []):
                mime = f.get("mimeType", "")
                if mime == "application/vnd.google-apps.folder":
                    yield from self._list_recursive(f["id"], f"{prefix}/{f['name']}")
                    continue
                path = f"{prefix}/{f['name']}"
                if not self._wanted(mime, path):
                    continue
                yield FileRecord(
                    file_id=f["id"],
                    path=path,
                    name=f["name"],
                    size=int(f.get("size", 0)),
                    md5=f.get("md5Checksum"),  # 可能缺失
                    mtime=f.get("modifiedTime"),
                    mime=mime,
                )
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def collect_incremental(self) -> Iterator[FileRecord]:
        """增量模式：Changes API。游标存 StateDB，410 自动降级全量。"""
        cursor = self._state.get_meta("changes_cursor")
        if not cursor:
            # 首次：取当前 token 存下，下次从增量开始
            resp = self._client._request(
                "/changes/startPageToken", {"supportsAllDrives": "true"})
            if resp and "startPageToken" in resp:
                self._state.set_meta("changes_cursor", resp["startPageToken"])
            return
        page_token: Optional[str] = cursor
        try:
            while page_token:
                resp = self._client._request("/changes", {
                    "pageToken": page_token,
                    "fields": "nextPageToken,newStartPageToken,"
                              "changes(fileId,removed,file(id,name,mimeType,size,"
                              "md5Checksum,modifiedTime,parents))",
                    "supportsAllDrives": "true",
                })
                if not resp:
                    break
                for ch in resp.get("changes", []):
                    f = ch.get("file") or {}
                    if ch.get("removed"):
                        # 删除事件：path 未知，用特殊标记
                        yield FileRecord(
                            file_id=ch["fileId"], path="", name="",
                            size=0, md5=None, mtime=None, mime="",
                        )
                        continue
                    mime = f.get("mimeType", "")
                    # 增量模式下路径需查父目录（简化：用 name，完整路径由 planner 回填）
                    path = f.get("name", "")
                    if not self._wanted(mime, path):
                        continue
                    yield FileRecord(
                        file_id=f["id"], path=path, name=f.get("name", ""),
                        size=int(f.get("size", 0)),
                        md5=f.get("md5Checksum"),
                        mtime=f.get("modifiedTime"), mime=mime,
                    )
                page_token = resp.get("nextPageToken")
                if "newStartPageToken" in resp:
                    self._state.set_meta(
                        "changes_cursor", resp["newStartPageToken"])
        except CursorExpiredError:
            # D-INCR-3: 410 → 自动降级全量对账
            log.warning("Changes 游标过期，降级为全量对账")
            self._state.set_meta("changes_cursor", "")
            yield from self.collect_full()
