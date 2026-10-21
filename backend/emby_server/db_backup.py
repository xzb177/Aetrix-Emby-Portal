"""数据库定时备份：SQLite 在线备份与 PostgreSQL pg_dump + gzip 压缩 + 按天轮转 + daemon 调度

要解决的问题：生产库是 SQLite 单文件，一旦损坏 / 误删 / 宿主机故障，
订单、积分、订阅全丢。之前备份脚本全是 PostgreSQL 口径（pg_dump），
与生产实际不匹配，等于没有备份。

设计（参照 auto_scan.py 的 daemon 模式——"功能只提供能力"）：
- 配置（SystemConfig）：``backup_enabled``（"1"/"0"，默认 "1"，开箱即有）、
  ``backup_time``（"HH:MM"，默认 "03:00"，服务器本地时间）、
  ``backup_keep_days``（默认 "7"，保留最近 N 天）、
  ``backup_last_run``（"YYYY-MM-DD"，同一天不重复跑）。
- 备份方式：``sqlite3.Connection.backup()`` 在线备份——DB 正在被读写时也安全。
  不要直接 cp 数据库文件（WAL 模式下可能拷到写一半的状态）。
  同理，恢复脚本的"防手滑安全备份"也必须走在线备份，裸 cp 在 WAL 下拿到的是旧数据。
- 位置：SQLite 与数据库文件同目录的 ``backups/`` 子目录，
  文件名 ``<库名>-YYYYMMDD-HHMMSS.db.gz``（gzip 压缩）；
  PostgreSQL 落 ``PG_BACKUP_DIR``（默认 ``/var/backups/aetrix``），
  文件名 ``<库名>-YYYYMMDD-HHMMSS.dump.gz``（pg_dump custom + gzip）。
  轮转 / 列表 / 残留清扫三个函数统一走 ``_backup_dirs()`` 双目录口径，
  ``keep_days`` 对两者同样生效（P2-2，2026-10-12）。
- 可靠性（这几条都是踩过的坑，勿删）：
  - ``_BACKUP_LOCK`` 互斥——手动接口与定时线程可并发，不互斥会写出内容交错的损坏库。
  - 落盘先写 ``.part-*.db.gz`` 再 ``os.replace`` 原子改名，中断不留下半截"有效"备份。
  - 写前查磁盘余量——备份与主库同卷，写到一半 ENOSPC 会把主库一起写挂。
  - ``CONFIG_LAST_RUN`` 只在**成功后**写——先占位会让失败变成"今天已跑过"，当天永不重试。
  - ``sweep_stale_temp_files()`` 回收强杀残留的未压缩全库副本。
- 非 SQLite / PostgreSQL 部署（例如 MySQL）：``get_config()`` 返回 ``supported: false``
  且调度线程不启动，不静默假装有备份。SQLite 与 PostgreSQL 都走内置备份
  （PG 为 ``pg_dump``，见 ``_run_pg_backup_locked``）；MySQL 请用原生工具。
- 调度：worker / EM 启动时 ``start_backup_scheduler()`` 起 daemon 线程，
  每 60 秒检查一次；到点且当天没跑过就执行。任何异常只记日志，线程永不退出。
"""

from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from backend.database import SessionLocal, engine
from backend.integrations import store

logger = logging.getLogger(__name__)

CONFIG_ENABLED = "backup_enabled"
CONFIG_TIME = "backup_time"
CONFIG_KEEP_DAYS = "backup_keep_days"
CONFIG_LAST_RUN = "backup_last_run"

DEFAULT_ENABLED = True
DEFAULT_TIME = "03:00"
DEFAULT_KEEP_DAYS = 7
CHECK_INTERVAL = 60  # 秒

BACKUP_DIRNAME = "backups"
FILENAME_RE = re.compile(r"^(.+)-(\d{8})-(\d{6})\.db\.gz$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

_SCHEDULER_STARTED = False
_SCHEDULER_LOCK = threading.Lock()
# 备份执行互斥：手动接口跑在 FastAPI 线程池、定时跑在调度线程，两者可并发进入。
# 不加锁时两个调用落在同一秒 → tmp 与目标同名 → 两个连接同时写同一个文件 → 损坏备份。
_BACKUP_LOCK = threading.Lock()
# 崩溃残留清扫时保留的最小 mtime（秒）：太新的可能是正在跑的，别删。
_STALE_KEEP_SECONDS = 3600
# 空间不足时仍需预留的字节数（给主库写入留活路，备份不能把库写挂）
_FREE_RESERVE_BYTES = 512 * 1024 * 1024
# 峰值占用倍数：未压缩 tmp 副本 + gzip 成品，按最坏情况 1.0 + 1.0 估
_PEAK_SPACE_FACTOR = 2.2


def parse_time(value: str | None) -> str | None:
    """校验 "HH:MM"；合法返回规范化字符串，否则返回 None。"""
    text = (value or "").strip()
    m = _TIME_RE.match(text)
    if not m:
        return None
    return f"{m.group(1)}:{m.group(2)}"


def get_db_file_path() -> Path | None:
    """从 engine URL 解析 SQLite 数据库文件路径；非 sqlite / 内存库返回 None。"""
    try:
        url = engine.url
    except Exception:  # noqa: BLE001 — engine 未就绪时当没有
        return None
    if url.drivername != "sqlite":
        return None
    db_name = url.database
    if not db_name or db_name == ":memory:":
        return None
    return Path(db_name).resolve()


def _is_postgres() -> bool:
    """当前是否为 PostgreSQL 部署（E-P1-4）"""
    try:
        url = engine.url
    except Exception:  # noqa: BLE001
        return False
    return url.drivername in ("postgresql", "postgresql+psycopg", "postgresql+psycopg2")


def _get_pg_backup_dir() -> Path | None:
    """PG 备份目录：环境变量 PG_BACKUP_DIR，默认 /var/backups/aetrix（E-P1-4）"""
    d = os.getenv("PG_BACKUP_DIR", "/var/backups/aetrix")
    try:
        p = Path(d)
        p.mkdir(parents=True, exist_ok=True)
        return p
    except OSError:
        return None


def get_backup_dir() -> Path | None:
    """备份目录：与数据库文件同目录的 backups/。"""
    db_path = get_db_file_path()
    if db_path is None:
        return None
    return db_path.parent / BACKUP_DIRNAME


def _pg_backup_dir_readonly() -> Path | None:
    """PG 备份目录（只读版）：list/prune/sweep 用，不触发 mkdir。

    ``_get_pg_backup_dir`` 会 mkdir——轮转/列表是读操作，不该有写副作用。
    """
    p = Path(os.getenv("PG_BACKUP_DIR", "/var/backups/aetrix"))
    return p if p.is_dir() else None


def _backup_dirs() -> list[Path]:
    """所有可能落备份文件的目录（P2-2，2026-10-12）。

    SQLite 目录（``get_backup_dir``）+ PG 目录（``_pg_backup_dir_readonly``，只读版不 mkdir）。
    ``prune_old_backups`` / ``list_backups`` / ``sweep_stale_temp_files``
    三者统一走这里——之前 PG 分支的 ``*.dump.gz`` 三处全看不到，
    每天一个全量 dump 无限堆积（生产是 PG）。
    """
    dirs: list[Path] = []
    sqlite_dir = get_backup_dir()
    if sqlite_dir is not None and sqlite_dir.is_dir():
        dirs.append(sqlite_dir)
    if _is_postgres():
        pg_dir = _pg_backup_dir_readonly()
        if pg_dir is not None and pg_dir not in dirs:
            dirs.append(pg_dir)
    return dirs


def _backup_filename(db_path: Path, when: datetime) -> str:
    stem = db_path.stem  # aetrix_unified
    return f"{stem}-{when.strftime('%Y%m%d-%H%M%S')}.db.gz"


def _ensure_free_space(backup_dir: Path, db_size: int) -> None:
    """空间不够就别开始：备份写到一半 ENOSPC 会连带把主库写挂。

    备份与主库同卷（默认布局），所以余量不足时必须提前拒绝，而不是写一半失败。
    """
    need = int(db_size * _PEAK_SPACE_FACTOR) + _FREE_RESERVE_BYTES
    try:
        free = shutil.disk_usage(backup_dir).free
    except OSError as exc:  # noqa: BLE001 — 拿不到余量不阻断备份
        logger.warning("无法读取备份目录剩余空间，跳过空间预检：%s", exc)
        return
    if free < need:
        raise RuntimeError(
            f"剩余空间不足，拒绝备份：可用 {free / 1024**3:.1f} GB，"
            f"本次约需 {need / 1024**3:.1f} GB（库 {db_size / 1024**3:.1f} GB 的临时副本 + 压缩成品）"
        )


def run_backup_now(reason: str = "manual") -> dict:
    """立即执行一次备份（含轮转）。返回 {name, size, path}。

    失败时抛异常（调用方决定是记日志还是转 500）。
    已有备份在跑时抛 RuntimeError（不排队，排队没意义——备份是每日一次的低频操作）。
    """
    if not _BACKUP_LOCK.acquire(blocking=False):
        raise RuntimeError("已有备份正在执行，请稍后重试")
    try:
        return _run_backup_locked(reason)
    finally:
        _BACKUP_LOCK.release()


def _run_backup_locked(reason: str) -> dict:
    """备份主体。调用方必须已持有 ``_BACKUP_LOCK``。"""
    # E-P1-4：PostgreSQL 走 pg_dump 分支
    if _is_postgres():
        return _run_pg_backup_locked(reason)
    db_path = get_db_file_path()
    if db_path is None:
        raise RuntimeError("当前不是 SQLite 文件数据库，跳过备份（pg/mysql 请用原生工具）")
    if not db_path.exists():
        raise RuntimeError(f"数据库文件不存在：{db_path}")
    backup_dir = get_backup_dir()
    assert backup_dir is not None
    backup_dir.mkdir(parents=True, exist_ok=True)

    try:
        db_size = db_path.stat().st_size
    except OSError as exc:  # noqa: BLE001
        raise RuntimeError(f"读取数据库文件大小失败：{exc}") from exc
    _ensure_free_space(backup_dir, db_size)

    # 文件名带上 pid + 随机串：即便互斥被绕过也不会撞名
    now = datetime.now()
    unique = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
    name = _backup_filename(db_path, now)
    final_path = backup_dir / name
    part_path = backup_dir / f".part-{unique}.db.gz"

    # 在线备份到临时文件（同目录，保证同文件系统，写完再改名，原子性更好）
    tmp_db = backup_dir / f".tmp-{unique}.db"
    try:
        src = sqlite3.connect(str(db_path), timeout=30)
        try:
            dst = sqlite3.connect(str(tmp_db))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        # gzip 压缩到 .part 临时名：中断时不会留下一个"看起来有效"的半截 .db.gz
        with open(tmp_db, "rb") as f_in, gzip.open(part_path, "wb", compresslevel=6) as f_out:
            shutil.copyfileobj(f_in, f_out)
        os.replace(part_path, final_path)  # 原子改名，出现在列表里的必然是完整文件
    finally:
        tmp_db.unlink(missing_ok=True)
        part_path.unlink(missing_ok=True)  # 成功时已改名，这里是 no-op

    size = final_path.stat().st_size
    logger.info("数据库备份完成（%s）：%s（%.1f MB）", reason, name, size / 1024 / 1024)
    return {"name": name, "size": size, "path": str(final_path)}


def _run_pg_backup_locked(reason: str) -> dict:
    """PostgreSQL 在线备份分支（E-P1-4）：pg_dump --format=custom + gzip。

    用 custom 格式（二进制）而非 plain SQL：恢复时 pg_restore 可并行，
    且不依赖 SQL 文本的版本兼容性。
    """
    import subprocess

    backup_dir = _get_pg_backup_dir()
    if backup_dir is None:
        raise RuntimeError("PG 备份目录不可写（PG_BACKUP_DIR）")
    try:
        url = engine.url
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"读取数据库连接信息失败：{exc}") from exc

    # 从 SQLAlchemy URL 提取 pg_dump 参数
    host = url.host or "localhost"
    port = url.port or 5432
    dbname = url.database
    user = url.username
    password = url.password
    if not dbname:
        raise RuntimeError("无法从连接串解析数据库名")

    now = datetime.now()
    name = f"{dbname}-{now.strftime('%Y%m%d-%H%M%S')}.dump.gz"
    final_path = backup_dir / name
    unique = f"{os.getpid()}-{uuid.uuid4().hex[:6]}"
    part_path = backup_dir / f".part-{unique}.dump.gz"

    env = dict(os.environ)
    if password:
        env["PGPASSWORD"] = password

    cmd = [
        "pg_dump",
        "-h", host, "-p", str(port),
        "-U", user or "postgres",
        "-F", "c",  # custom 格式
        "-Z", "6",  # gzip 压缩级别
        "-f", str(part_path),
        dbname,
    ]
    try:
        proc = subprocess.run(
            cmd, env=env, capture_output=True, text=True, timeout=3600,
        )
    except FileNotFoundError:
        # pg_dump 不可用时清理 part 文件并抛错
        part_path.unlink(missing_ok=True)
        raise RuntimeError("pg_dump 未安装，无法备份 PostgreSQL") from None
    except subprocess.TimeoutExpired:
        part_path.unlink(missing_ok=True)
        raise RuntimeError("pg_dump 超时（1小时）") from None
    if proc.returncode != 0:
        part_path.unlink(missing_ok=True)
        err = (proc.stderr or "")[:500]
        raise RuntimeError(f"pg_dump 失败（exit {proc.returncode}）：{err}")
    # 原子改名
    os.replace(part_path, final_path)
    size = final_path.stat().st_size
    logger.info("PG 数据库备份完成（%s）：%s（%.1f MB）", reason, name, size / 1024 / 1024)
    return {"name": name, "size": size, "path": str(final_path)}


def sweep_stale_temp_files() -> int:
    """清扫崩溃/强杀留下的临时文件。返回删除数量。

    这些文件是**未压缩的完整数据库副本**（.tmp-*.db）或半截压缩包
    （.part-*.db.gz / PG 的 .part-*.dump.gz），``prune_old_backups`` 只 glob
    正式备份后缀（``*.db.gz`` / ``*.dump.gz``），永远看不到也删不掉它们。
    容器被 SIGKILL / 断电时 ``finally`` 不会执行，必须靠这里回收。
    只删超过 1 小时的，避免误伤正在跑的备份。
    SQLite 与 PG 两个目录都扫（P2-2）。
    """
    removed = 0
    for backup_dir in _backup_dirs():
        removed += _sweep_dir(backup_dir)
    if removed:
        logger.warning("备份清理：删除 %d 个崩溃残留的临时文件", removed)
    return removed


def _sweep_dir(backup_dir: Path) -> int:
    """清扫单个备份目录的崩溃残留。返回删除数量。"""
    cutoff = datetime.now().timestamp() - _STALE_KEEP_SECONDS
    removed = 0
    for pattern in (".tmp-*.db", ".part-*.db.gz", ".part-*.dump.gz", ".restore-*.db"):
        for f in backup_dir.glob(pattern):
            try:
                st = f.stat()
                if st.st_mtime >= cutoff:
                    continue  # 太新，可能正在写
                size_mb = st.st_size / 1024 / 1024
                f.unlink()
                removed += 1
                logger.info("清理崩溃残留的备份临时文件：%s（%.1f MB）", f.name, size_mb)
            except OSError as exc:  # noqa: BLE001 — 删不掉记一条，不中断
                logger.warning("清理备份临时文件失败 %s：%s", f.name, exc)
    return removed


def prune_old_backups(keep_days: int) -> int:
    """删除超过 keep_days 的旧备份（按文件 mtime）。返回删除数量。

    SQLite（``*.db.gz``）与 PG（``*.dump.gz``）两个目录都轮转，
    ``keep_days`` 对两者同样生效（P2-2，2026-10-12）。
    """
    dirs = _backup_dirs()
    if not dirs:
        return 0
    cutoff = datetime.now().timestamp() - keep_days * 86400
    removed = 0
    for backup_dir in dirs:
        for f in list(backup_dir.glob("*.db.gz")) + list(backup_dir.glob("*.dump.gz")):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
                    removed += 1
            except OSError as exc:  # noqa: BLE001 — 删不掉记一条，不中断
                logger.warning("删除旧备份失败 %s：%s", f.name, exc)
    if removed:
        logger.info("备份轮转：删除 %d 个超过 %d 天的旧备份", removed, keep_days)
    sweep_stale_temp_files()
    return removed


def list_backups() -> list[dict]:
    """列出备份文件（新的在前）。SQLite 与 PG 两个目录都列（P2-2）。"""
    rows = []
    for backup_dir in _backup_dirs():
        for f in list(backup_dir.glob("*.db.gz")) + list(backup_dir.glob("*.dump.gz")):
            try:
                st = f.stat()
            except OSError:  # noqa: BLE001
                continue
            rows.append({
                "name": f.name,
                "size": st.st_size,
                "created_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            })
    rows.sort(key=lambda r: r["name"], reverse=True)
    return rows


def get_config(db: Session) -> dict:
    """读备份配置（管理后台展示用）。"""
    vals = store.read_values(
        db,
        [CONFIG_ENABLED, CONFIG_TIME, CONFIG_KEEP_DAYS, CONFIG_LAST_RUN],
        {
            CONFIG_ENABLED: "1" if DEFAULT_ENABLED else "0",
            CONFIG_TIME: DEFAULT_TIME,
            CONFIG_KEEP_DAYS: str(DEFAULT_KEEP_DAYS),
            CONFIG_LAST_RUN: "",
        },
    )
    enabled = vals[CONFIG_ENABLED].strip() == "1"
    time_str = parse_time(vals[CONFIG_TIME]) or DEFAULT_TIME
    try:
        keep_days = int(vals[CONFIG_KEEP_DAYS].strip())
    except (TypeError, ValueError):
        keep_days = DEFAULT_KEEP_DAYS
    keep_days = max(1, min(30, keep_days))
    # E-P1-4：SQLite 和 PostgreSQL 都支持内置备份
    supported = get_db_file_path() is not None or _is_postgres()
    return {
        "enabled": enabled if supported else False,
        # 非 SQLite/PG 部署：本模块只管 sqlite/pg，mysql 得用原生工具。
        # 必须显式告诉前端，否则"开关打开"会被读成"有备份在跑"。
        "supported": supported,
        "unsupported_reason": (
            "" if supported
            else "当前不是 SQLite/PG 数据库，内置备份不适用"
        ),
        "time": time_str,
        "keep_days": keep_days,
        "last_run": vals[CONFIG_LAST_RUN].strip(),
        "backups": list_backups(),
    }


def save_config(db: Session, enabled: bool, time_str: str, keep_days: int) -> dict:
    """保存备份配置；非法时抛 ValueError（接口转 400）。"""
    parsed = parse_time(time_str)
    if parsed is None:
        raise ValueError(f"时间格式非法，应为 HH:MM（24 小时制），收到：{time_str!r}")
    if not isinstance(keep_days, int) or not 1 <= keep_days <= 30:
        raise ValueError(f"保留天数非法，应为 1~30 的整数，收到：{keep_days!r}")
    store.write_values(
        db,
        {
            CONFIG_ENABLED: "1" if enabled else "0",
            CONFIG_TIME: parsed,
            CONFIG_KEEP_DAYS: str(keep_days),
        },
        {
            CONFIG_ENABLED: "数据库定时备份开关",
            CONFIG_TIME: "数据库备份每天执行时间（HH:MM，服务器本地时间）",
            CONFIG_KEEP_DAYS: "数据库备份保留天数（1~30）",
        },
    )
    db.commit()
    return get_config(db)


def _should_run_today(now: datetime, time_str: str, last_run: str) -> bool:
    """到点了且今天没跑过。

    刻意用 ``>=`` 而不是字符串全等：原实现要求轮询恰好落在那一分钟，
    而轮询间隔本身就是 60 秒，窗口宽度等于间隔 = 零容错。容器重启、NTP 校正、
    上一轮备份耗时 70 秒——任何一处让 tick 落在 03:01，当天备份就静默永久跳过。
    改成"已过点且今天没跑过"后，错过的分钟会自动补跑。
    """
    return now.strftime("%H:%M") >= time_str and last_run != now.strftime("%Y-%m-%d")


def _tick() -> None:
    """一次检查：到点就备份。异常只记日志，不向上传播。"""
    db = SessionLocal()
    try:
        cfg = get_config(db)
        if not cfg["enabled"]:
            return
        if not cfg.get("supported", True):
            # 非 SQLite/PG 部署：不跑（PostgreSQL 的 supported 为真，会走 pg_dump）。
            # 默认开关是开的，否则管理员会以为"开关打开 = 有备份"，实际每天都静默失败。
            return
        now = datetime.now()
        if not _should_run_today(now, cfg["time"], cfg["last_run"]):
            return
        # 注意：**不要**在备份前先写 CONFIG_LAST_RUN。
        # 先占位会让"备份失败"变成"今天已跑过"——失败后当天永不重试，
        # 而管理后台 last_run 仍显示今天，看着一切正常。
        result = run_backup_now(reason="scheduled")
        prune_old_backups(cfg["keep_days"])
        # 只有真正成功了才记日期
        store.write_values(
            db,
            {CONFIG_LAST_RUN: now.strftime("%Y-%m-%d")},
            {CONFIG_LAST_RUN: "数据库备份上次执行日期（防重复）"},
        )
        db.commit()
        logger.info("定时备份完成：%s", result["name"])
    except Exception as exc:  # noqa: BLE001 — 定时线程绝不能因一次失败退出
        logger.error("定时数据库备份失败（当天仍会重试）：%s", exc)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


def start_backup_scheduler() -> bool:
    """启动备份调度线程（同一进程只启动一次；daemon，随进程退出）。

    跟 auto_scan 保持一致：在 worker / EM（一体化）进程启动，API 专职进程不启动。
    """
    global _SCHEDULER_STARTED
    with _SCHEDULER_LOCK:
        if _SCHEDULER_STARTED:
            return False
        _SCHEDULER_STARTED = True

    def _loop() -> None:
        # 复用同一个 Event：每轮 new 一个 Event().wait() 没有意义
        wake = threading.Event()
        while not wake.wait(CHECK_INTERVAL):
            _tick()
        # wake 被 set 时退出（仅用于测试注入，正常运行不会走到）

    if get_db_file_path() is None and not _is_postgres():
        # 非 SQLite/PG：内置备份不适用。与其每天 03:00 抛一次
        # RuntimeError 再被吞掉，不如启动时就说清楚（前端也会拿到 supported=false）。
        logger.warning(
            "当前不是 SQLite/PG 数据库，已跳过内置定时备份调度"
        )
        return False

    threading.Thread(target=_loop, daemon=True, name="db-backup-scheduler").start()
    logger.info("数据库备份调度已启动（每 %s 秒检查一次，默认 %s 执行，保留 %s 天）",
                CHECK_INTERVAL, DEFAULT_TIME, DEFAULT_KEEP_DAYS)
    return True


__all__ = [
    "CONFIG_ENABLED",
    "CONFIG_KEEP_DAYS",
    "CONFIG_LAST_RUN",
    "CONFIG_TIME",
    "DEFAULT_KEEP_DAYS",
    "DEFAULT_TIME",
    "get_backup_dir",
    "get_config",
    "get_db_file_path",
    "list_backups",
    "parse_time",
    "prune_old_backups",
    "run_backup_now",
    "save_config",
    "sweep_stale_temp_files",
    "start_backup_scheduler",
]
