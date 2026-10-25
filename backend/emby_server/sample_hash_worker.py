# -*- coding: utf-8 -*-
"""本地文件的采样内容哈希后台队列（FMBY 第二批 #13 PR-4）。

定位（定稿 §2.1 / §6 PR-4）：
- 只给「源不给哈希、本机又能直接读到文件」的行补采样哈希：本地路径行。
  mount:// 行（115/Drive/rclone）采样等于把文件下载一遍、烧配额，绝不碰；
  .strm 行的内容是链接文本、不是媒体，采样无意义，排除。
- 采样哈希 = size + 头 1MiB + 中段 1MiB + 尾 1MiB 的 sha256 前 32 位十六进制
  字符（algo 记 ``sample-sha256``）。它与全量哈希**不是同一种证据**：
  认亲层按 (hash, algo) 成对比较，采样哈希只与采样哈希比；且采样哈希认领
  时须再叠加归一化标题一致（见 scanner._sample_title_matches）。文档里写死：
  采样 ≠ 全量，采样撞（同压制复制）是预期内的、不是 bug。
- 绝不进扫描热路径：本模块只在后台线程里跑，逐文件限速（文件间 sleep），
  每轮有条数上限；扫描主链路零改动。默认关，可独立回滚（关开关即停，
  已落库的采样哈希只是证据列，不影响任何判定——认亲层开关另管）。
- 只填空：content_hash 已有值（源给的 md5/sha1 或已算过的采样）的行一律
  不动，绝不拿采样覆盖源给的全量哈希。identity_json 只做合并式追加（保留
  旧证据键、解析失败时原样不动，绝不洗掉旧快照）。
- 管理后台展示页不归本模块：按分工，展示页（含配套只读接口）由管理后台
  专线在 PR 落地后统一做，本 PR 不起新管理页、不加管理端点。

开关与限速（env，r44 防漂移：env_int/env_float 直读，见 env.example 末尾）：
- SCAN_SAMPLE_HASH_ENABLED：默认 0（关）。
- SCAN_SAMPLE_HASH_MAX_FILES：每轮最多处理行数，默认 50。
- SCAN_SAMPLE_HASH_DELAY_SEC：每成功读一文件后的 sleep（秒），默认 0.5。
- SCAN_SAMPLE_HASH_INTERVAL_SEC：后台轮次间隔（秒），默认 3600。
"""

import hashlib
import json
import logging
import os
import struct
import threading
import time

from backend.emby_server.env_util import env_float, env_int

logger = logging.getLogger(__name__)

SAMPLE_ALGO = "sample-sha256"
SAMPLE_HASH_LEN = 32  # sha256 十六进制摘要的前 32 位字符
WINDOW_BYTES = 1024 * 1024  # 头/中段/尾各 1MiB（定稿 §2.1 固定口径）

SAMPLE_HASH_ENABLED = env_int("SCAN_SAMPLE_HASH_ENABLED", 0, 0, 1) == 1
SAMPLE_HASH_MAX_FILES = env_int("SCAN_SAMPLE_HASH_MAX_FILES", 50, 1, 10000)
SAMPLE_HASH_DELAY_SEC = env_float("SCAN_SAMPLE_HASH_DELAY_SEC", 0.5, 0.0, 60.0)
SAMPLE_HASH_INTERVAL_SEC = env_float(
    "SCAN_SAMPLE_HASH_INTERVAL_SEC", 3600.0, 60.0, 86400.0)

_stop_event = threading.Event()
_start_lock = threading.Lock()
_sample_thread = None


def compute_sample_hash(path: str):
    """算一个本地文件的采样哈希；读不到/空文件/异常返回 None（不抛）。

    窗口：头 1MiB + 中段 1MiB（以文件正中为中心）+ 尾 1MiB；小文件窗口
    重叠时按实际读到的字节算（仍确定性）。哈希输入先拼 8 字节大端 size，
    使同内容不同长度的文件必不撞（size 本身也是认亲层的比对分量）。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    if size <= 0:
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(WINDOW_BYTES)
            mid_start = max(0, size // 2 - WINDOW_BYTES // 2)
            f.seek(mid_start)
            mid = f.read(WINDOW_BYTES)
            f.seek(max(0, size - WINDOW_BYTES))
            tail = f.read(WINDOW_BYTES)
    except OSError:
        return None
    digest = hashlib.sha256()
    digest.update(struct.pack(">Q", size))
    digest.update(head)
    digest.update(mid)
    digest.update(tail)
    return digest.hexdigest()[:SAMPLE_HASH_LEN]


def _is_sampleable_path(file_path) -> bool:
    """本 worker 能采样的路径：本地真实路径，且不是挂载路径。"""
    if not file_path:
        return False
    if file_path.startswith("mount://"):
        return False
    return os.path.isfile(file_path)


def run_once(db, max_files=None, delay_sec=None) -> dict:
    """跑一轮采样补填（供后台线程与测试共用；不启动线程、不读全局开关）。

    只取 content_hash 为空（NULL 或空串）的 movie/episode 行，排除 .strm 行；
    每行只在 content_hash 仍为空时写（二次校验，防并发写覆盖源哈希）。
    返回 {"checked", "filled", "skipped_missing", "failed"}。
    """
    from backend.emby_server import models as em

    if max_files is None:
        max_files = SAMPLE_HASH_MAX_FILES
    if delay_sec is None:
        delay_sec = SAMPLE_HASH_DELAY_SEC
    stats = {"checked": 0, "filled": 0, "skipped_missing": 0, "failed": 0}
    rows = (
        db.query(em.MediaItem)
        .filter(
            em.MediaItem.item_type.in_(("movie", "episode")),
            em.MediaItem.file_path.isnot(None),
            # container 为 NULL 的老行不能被 `!= 'strm'` 误滤（SQL NULL 比较
            # 结果为 NULL 即不命中）；mount:// 行在 SQL 层直接排除，免得占
            # 每轮条数上限、饿死后面的本地行。
            (em.MediaItem.container.is_(None))
            | (em.MediaItem.container != "strm"),
            ~em.MediaItem.file_path.like("mount://%"),
        )
        .filter(
            (em.MediaItem.content_hash.is_(None))
            | (em.MediaItem.content_hash == "")
        )
        .order_by(em.MediaItem.id)
        .limit(max_files)
        .all()
    )
    for row in rows:
        stats["checked"] += 1
        path = getattr(row, "file_path", None)
        if not _is_sampleable_path(path):
            # 远端挂载路径/已删文件：本轮不处理、不记失败（不是本 worker 的错）。
            stats["skipped_missing"] += 1
            continue
        if getattr(row, "content_hash", None):
            continue  # 二次校验：本轮内已被别的写入填上，绝不覆盖
        digest = compute_sample_hash(path)
        if digest is None:
            stats["failed"] += 1
            continue
        row.content_hash = digest
        row.content_hash_algo = SAMPLE_ALGO
        # identity_json 合并式追加：保留旧证据键；旧快照解析失败时原样不动。
        old_json = getattr(row, "identity_json", None)
        if old_json:
            try:
                evidence = json.loads(old_json)
            except (ValueError, TypeError):
                evidence = None
            if isinstance(evidence, dict):
                evidence["content_hash"] = digest
                evidence["content_hash_algo"] = SAMPLE_ALGO
                row.identity_json = json.dumps(
                    evidence, sort_keys=True, ensure_ascii=False)
        else:
            row.identity_json = json.dumps(
                {"content_hash": digest, "content_hash_algo": SAMPLE_ALGO},
                sort_keys=True, ensure_ascii=False)
        stats["filled"] += 1
        if delay_sec:
            time.sleep(delay_sec)
    db.commit()
    return stats


def _sample_loop():
    logger.info("采样哈希 worker 启动（间隔 %.0f 秒）", SAMPLE_HASH_INTERVAL_SEC)
    while not _stop_event.is_set():
        try:
            from backend.database import SessionLocal

            db = SessionLocal()
            try:
                stats = run_once(db)
            finally:
                db.close()
            if stats["filled"]:
                logger.info("采样哈希轮次完成：%s", stats)
        except Exception as e:  # noqa: BLE001 — 后台轮次失败只记日志，不炸线程
            logger.warning("采样哈希轮次失败: %s", e)
        finally:
            try:
                from backend.emby_server import worker_registry as _wr

                _wr.heartbeat("sample_hash")
            except Exception:  # noqa: BLE001 — 注册表缺失时 heartbeat 可忽略
                pass
        _stop_event.wait(SAMPLE_HASH_INTERVAL_SEC)


def start() -> bool:
    """启动采样哈希后台线程（默认关：开关关时返回 False，不起线程）。"""
    global _sample_thread
    if not SAMPLE_HASH_ENABLED:
        logger.info("采样哈希 worker 已禁用（SCAN_SAMPLE_HASH_ENABLED=0）")
        return False
    with _start_lock:
        if _sample_thread and _sample_thread.is_alive():
            return True
        _stop_event.clear()
        _sample_thread = threading.Thread(
            target=_sample_loop, name="sample-hash", daemon=True)
        _sample_thread.start()
        try:
            from backend.emby_server import worker_registry as _wr

            _wr.register("sample_hash", _sample_thread)
        except Exception:  # noqa: BLE001 — 注册失败不影响线程本身
            pass
        return True


def stop():
    """停采样哈希后台线程（测试/关机用）。"""
    _stop_event.set()
