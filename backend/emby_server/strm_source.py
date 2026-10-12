"""strm 生成器的通用数据源抽象。

设计目标：增量检测逻辑与具体挂载类型解耦，新增挂载类型只需实现 SourceLister 接口，
所有挂载方式都能用到增量能力，不能只优化 Google Drive。

防风控契约（用户原话："每次挂载方式都要防风控"，接口层面强制）：
每种实现必须遵守以下两条，不允许有漏网之鱼：
1. jitter：列举/请求间隔加入随机抖动，避免机械的固定节拍被识别为机器行为。
   Drive 走 API 怕被 Google 风控，本地走 stat 怕打爆 IO，rclone 走 FUSE/API 同样怕被限流。
2. rate_limit：对高频操作（API 调用、文件 stat）做限速，单位时间操作数有上限。
具体参数由各实现自行定义，但必须在类 docstring 中说明取值依据。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
from typing import Any, Iterator, Optional

logger = logging.getLogger(__name__)


class SourceLister:
    """数据源增量列举接口。yield (relpath, file_id, size)。

    file_id 是数据源内的唯一标识（Drive 用 file id，本地用相对路径的 hash）。
    relpath 是相对源根目录的路径（与 strm_gen 的状态键兼容）。
    """

    # 子类必须覆盖：抖动比例（0-0.5），默认 0.2
    JITTER_RATIO: float = 0.2
    # 子类必须覆盖：限速说明（人类可读，供日志/文档）
    RATE_LIMIT_DESC: str = "未定义"

    def iter_videos(self, incremental: bool = True) -> Iterator[tuple[str, str, int]]:
        """列举视频文件。incremental=True 时只返回变化的文件。"""
        raise NotImplementedError

    def save_checkpoint(self) -> None:
        """增量成功后持久化检查点（如下次增量的起点）。"""
        pass

    def _jitter_sleep(self, base_sec: float) -> None:
        """带抖动的短暂休眠。base_sec 为基准秒数，实际按 uniform(1-ratio, 1+ratio) 打散。

        反风控：避免 burst 请求的固定节拍被识别为机器行为。
        """
        ratio = max(0.0, min(0.5, self.JITTER_RATIO))
        if ratio <= 0:
            time.sleep(base_sec)
            return
        time.sleep(base_sec * random.uniform(1.0 - ratio, 1.0 + ratio))


class DriveChangesLister(SourceLister):
    """Google Drive 数据源：增量用 Changes API，全量回退到 files.list。

    防风控说明：
    - Changes API 每次只拉变化（通常几十条），配额消耗远小于全量 files.list（约 100 次调用）。
    - 连续多页拉取时间隔加 0.1-0.3 秒随机抖动，避免 burst 打爆配额。
    - JITTER_RATIO=0.2：与 drive_changes 模块的 POLL_JITTER_RATIO 保持一致。
    """

    JITTER_RATIO: float = 0.2
    RATE_LIMIT_DESC: str = (
        "Changes API 按需拉取，单次增量通常 1-3 次 API 调用；"
        "多页连续拉取时间隔 0.1-0.3s 随机抖动；"
        "全量回退时复用 strm_gen.iter_drive_videos（约 100 次调用，带 401 自动刷新）"
    )

    # SystemConfig key 前缀：增量检查点（changes page token）
    TOKEN_KEY_PREFIX = "strm_gen_changes_token_"

    def __init__(self, drive_id: str, dir_path: str, strict_dir: bool = True):
        self.drive_id = drive_id
        self.dir_path = (dir_path or "").strip().strip("/")
        # strict_dir=False：自动发现模式下，源目录不存在时跳过该盘而非报错
        # （不是每个盘都有媒体目录，缺目录是预期情况）
        self.strict_dir = strict_dir
        self._new_token: Optional[str] = None
        # 可观测性计数器（2026-10-12）：列举完成后调用方可读
        self.yielded_count: int = 0          # 成功 yield 的视频文件数
        self.path_resolve_skipped: int = 0  # _build_relpath 失败跳过的 changes 数

    def _db(self):
        from backend.database import SessionLocal
        return SessionLocal()

    def _get_saved_token(self) -> Optional[str]:
        try:
            from backend.integrations import store
            db = self._db()
            try:
                return store.get_value(db, self.TOKEN_KEY_PREFIX + self.drive_id, "") or None
            finally:
                db.close()
        except Exception:
            return None

    def _save_token(self, token: str) -> None:
        try:
            from backend.integrations import store
            db = self._db()
            try:
                store.write_values(db, {self.TOKEN_KEY_PREFIX + self.drive_id: token}, {})
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.warning("strm_source: 保存 changes token 失败: %s", exc)

    def save_checkpoint(self) -> None:
        if self._new_token:
            self._save_token(self._new_token)
            self._new_token = None

    def iter_videos(self, incremental: bool = True) -> Iterator[tuple[str, str, int]]:
        if not incremental:
            # 全量：回退到现有的 files.list 实现
            from backend.emby_server import strm_gen
            yield from strm_gen.iter_drive_videos(
                self.drive_id, self.dir_path, strict=self.strict_dir)
            return

        from backend.emby_server import drive_changes as dc

        token = self._get_saved_token()
        if not token:
            # 首次增量：拿初始 token，本次按全量跑一遍建立基线
            # （下次增量才有起点；本次不丢数据）
            logger.info("strm_source: drive=%s 首次增量，无检查点，本次全量建立基线", self.drive_id)
            token = dc.get_start_page_token(self.drive_id)
            if token:
                self._new_token = token
            from backend.emby_server import strm_gen
            yield from strm_gen.iter_drive_videos(
                self.drive_id, self.dir_path, strict=self.strict_dir)
            return

        # 增量：拉 changes（2026-10-12 起不再吞错：API 失败直接抛，
        # 调用方按"列举失败"处理，绝不能当成"本轮无变化"）
        changes, new_token = dc.list_changes(token, self.drive_id)
        if new_token:
            self._new_token = new_token
        if not changes:
            logger.info("strm_source: drive=%s 本轮无变化", self.drive_id)
            return

        logger.info("strm_source: drive=%s 本轮 changes=%d", self.drive_id, len(changes))

        # changes 只给 fileId，需要解析出完整路径并过滤到 dir_path 子树
        # 用 drive_changes._file_meta 建路径（有内存缓存）
        from backend.emby_server import strm_gen as sg
        access_token = dc._get_token()
        count = 0
        for ch in changes:
            fid = ch.get("fileId", "")
            if not fid:
                continue
            # 删除/移走的文件：removed=True 或 trashed=True，yield 一个墓碑标记
            # （调用方按 file_id 查不到则跳过；删除由 prune 逻辑处理）
            if ch.get("removed"):
                continue
            f = ch.get("file") or {}
            if f.get("trashed"):
                continue
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                continue
            name = f.get("name", "")
            if not name:
                # file 详情缺失时补查一次
                meta = dc._file_meta(fid, access_token) if access_token else None
                if not meta:
                    continue
                name = meta.get("name", "")
                parents = meta.get("parents", [])
            else:
                parents = f.get("parents", [])

            # 建相对路径：用 parents 链回溯
            rel = self._build_relpath(fid, name, parents, access_token)
            if rel is None:
                self.path_resolve_skipped += 1
                continue
            # 过滤到 dir_path 子树
            prefix = (self.dir_path + "/") if self.dir_path else ""
            if prefix and not (rel == self.dir_path or rel.startswith(prefix)):
                continue
            if not sg.is_video_path(rel):
                continue
            try:
                size = int(f.get("size") or 0)
            except (ValueError, TypeError):
                # changes 的 file 可能没带 size，补查
                meta = dc._file_meta(fid, access_token) if access_token else None
                try:
                    size = int((meta or {}).get("size") or 0)
                except (ValueError, TypeError):
                    size = 0
            count += 1
            self.yielded_count += 1
            # 页间抖动：避免连续补查打爆配额
            if count % 50 == 0:
                self._jitter_sleep(0.2)
            # 大目录进度日志：8 万+ 文件卡住时能定位（每 1000 个打一次）
            if count % 1000 == 0:
                logger.info("strm_source: drive=%s 已列举 %d 个视频文件", self.drive_id, count)
            yield rel, fid, size

    def _build_relpath(self, fid: str, name: str, parents: list,
                       access_token: Optional[str]) -> Optional[str]:
        """从 parents 链回溯出相对网盘根的路径。失败返回 None。"""
        from backend.emby_server import drive_changes as dc
        from backend.emby_server import strm_gen as sg

        parts = [sg._safe_name(name)]
        seen = {fid}
        pid = parents[0] if parents else None
        depth = 0
        while pid and depth < 32:
            depth += 1
            if pid == self.drive_id or pid == "root":
                break
            meta = dc._file_meta(pid, access_token) if access_token else None
            if not meta:
                return None
            pname = sg._safe_name(meta.get("name", ""))
            if not pname:
                return None
            parts.append(pname)
            if pid in seen:
                return None
            seen.add(pid)
            pparents = meta.get("parents", [])
            pid = pparents[0] if pparents else None
        parts.reverse()
        return "/".join(parts)


class LocalFsLister(SourceLister):
    """本地文件系统数据源：用 (mtime_ns, size) 指纹做增量检测。

    防风控说明（本地 IO 同样要防"风控"——打满 IO 会拖慢播放主链路）：
    - walk 过程中每 1000 个文件 sleep random(0.01, 0.05) 秒，避免 IO burst。
    - 指纹比对是纯内存操作，零网络开销。
    - JITTER_RATIO=0.2：与 Drive 实现保持一致，调度层统一抖动。
    """

    JITTER_RATIO: float = 0.2
    RATE_LIMIT_DESC: str = (
        "本地 walk，每 1000 个文件 sleep 0.01-0.05s 随机抖动，避免 IO burst 影响播放主链路；"
        "指纹比对纯内存，零网络开销"
    )

    # SystemConfig key 前缀：文件指纹库（JSON：{relpath: [mtime_ns, size]}）
    FP_KEY_PREFIX = "strm_gen_local_fp_"

    def __init__(self, root_path: str):
        self.root_path = os.path.abspath(root_path)
        key_hash = hashlib.sha256(self.root_path.encode()).hexdigest()[:16]
        self._fp_key = self.FP_KEY_PREFIX + key_hash
        self._new_fp: Optional[dict] = None

    def _db(self):
        from backend.database import SessionLocal
        return SessionLocal()

    def _load_fp(self) -> dict:
        try:
            from backend.integrations import store
            db = self._db()
            try:
                raw = store.get_value(db, self._fp_key, "") or ""
                return json.loads(raw) if raw else {}
            finally:
                db.close()
        except Exception:
            return {}

    def _save_fp(self, fp: dict) -> None:
        try:
            from backend.integrations import store
            db = self._db()
            try:
                store.write_values(db, {self._fp_key: json.dumps(fp)}, {})
                db.commit()
            finally:
                db.close()
        except Exception as exc:
            logger.warning("strm_source: 保存本地指纹库失败: %s", exc)

    def save_checkpoint(self) -> None:
        if self._new_fp is not None:
            self._save_fp(self._new_fp)
            self._new_fp = None

    def iter_videos(self, incremental: bool = True) -> Iterator[tuple[str, str, int]]:
        from backend.emby_server import strm_gen as sg

        old_fp = self._load_fp() if incremental else {}
        new_fp: dict[str, list] = {}
        n = 0

        for dirpath, dirnames, filenames in os.walk(self.root_path):
            # 跳过系统目录
            dirnames[:] = [d for d in dirnames
                           if not d.startswith(".") and d not in sg.SKIP_DIR_NAMES]
            for fn in filenames:
                n += 1
                # 防风控：每 1000 个文件加微小抖动，避免 IO burst
                if n % 1000 == 0:
                    self._jitter_sleep(0.03)
                if not sg.is_video_path(fn):
                    continue
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, self.root_path).replace(os.sep, "/")
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                fp = [st.st_mtime_ns, st.st_size]
                new_fp[rel] = fp
                if incremental and old_fp.get(rel) == fp:
                    continue
                # file_id 用相对路径的 sha256（本地文件无稳定 id，路径即身份）
                fid = "local:" + hashlib.sha256(rel.encode()).hexdigest()[:32]
                yield rel, fid, st.st_size

        self._new_fp = new_fp


def get_lister(source_type: str, **kwargs) -> SourceLister:
    """按数据源类型创建 Lister。新增挂载类型时在这里注册。"""
    st = (source_type or "drive").strip().lower()
    if st == "drive":
        return DriveChangesLister(kwargs["drive_id"], kwargs.get("dir_path", ""))
    if st == "local":
        return LocalFsLister(kwargs["root_path"])
    raise ValueError(f"不支持的数据源类型: {source_type!r}")
