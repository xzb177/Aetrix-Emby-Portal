"""追新：检测挂载上的新资源，自动触发扫描 + 刮削

设计见 workspace/designs/chase-new.md v2

核心思路：
- 每 N 分钟（用户可配）检查一次
- local 类型挂载（含 rclone 挂载的 Drive/S3 等）：用文件 mtime 检测新增
- 发现新视频文件 → 整库入 scan_queue（trigger="chase-new"）
  → 扫描器增量秒跳，只处理新文件
- 新入库条目自动进 enrich 队列刮削（NFO→TMDB→豆瓣）

v1 范围：
- 本机目录：文件 mtime 检测（``find -newermt``）
- rclone RC 挂载：逐层列举 + ``ModTime`` 窗口过滤（走 ``mounts`` 公共通道）
- 115 直挂暂不直接检测（远端 API 不给可靠的 mtime），依赖每日定时扫描兜底

2026-10：远程检测曾绕过 ``mounts`` 的公共通道直接调 rclone RC，是一条无上限的
旁路（生产 24 小时 5.2 万条报错）。现在统一走 ``build_provider`` → ``list_dir``，
吃缓存 / 单飞 / 限流 / 退避 / 统计，并占用追新自己的小名额。
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from backend import models as base_models
from backend.database import SessionLocal
from backend.emby_server import models as em
from backend.emby_server import mounts as mount_lib
from backend.emby_server import mount_rclone
from backend.emby_server import mount_drive  # noqa: F401 — 导入即注册 drive 提供者
from backend.emby_server import scan_queue

logger = logging.getLogger(__name__)

CONFIG_ENABLED = "chase_new_enabled"
CONFIG_INTERVAL = "chase_new_interval"
#: v2.45.0：**排除清单**（逗号分隔的库 id）。空 = 全部启用库都监听。
#:
#: 为什么从「包含清单」改成「排除清单」：包含清单空 = 全部，于是想关掉 A 库就必须先去
#: B 库打开开关、让清单被写出来，再回来把 A 删掉——反直觉且容易漏。现在直接往排除
#: 清单里加 A 就行，一次点击到位。
#:
#: 旧键 ``CONFIG_LIBRARIES``（包含清单）只在下述迁移里读一次：新键存在就以新键为准；
#: 新键不存在而旧键非空 = 老部署，把「启用库 − 包含清单」算成排除清单写进去。
#: 不这么做的话，老部署升级后会**静默变成监听全部库**（原来的「只听 A、B」变成全听），
#: 相当于一次扫描风暴。
CONFIG_EXCLUDED = "chase_new_excluded"
CONFIG_LIBRARIES = "chase_new_libraries"
CONFIG_LAST_CHECK = "chase_new_last_check"
CONFIG_LAST_FOUND = "chase_new_last_found"

DEFAULT_INTERVAL = 10  # 分钟
MIN_INTERVAL = 5
MAX_INTERVAL = 120

#: 单个目录的 ``find`` 超时秒数。超时不会丢数据——会退回 ``_scandir_new_videos``
#: （慢但能跑完），所以这里不需要设很大；生产上 FUSE 目录 60 秒都扫不完，
#: 早降级早拿到结果。
_FIND_TIMEOUT_SEC = max(10, int(os.getenv("CHASE_NEW_FIND_TIMEOUT", "60") or 60))

# 视频扩展名白名单（.iso 光盘镜像：部分客户端可直接播放整盘）
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".ts", ".m2ts", ".wmv", ".flv", ".mov", ".rmvb", ".mpg", ".mpeg", ".webm", ".iso"}

def _tracked_exts(db: Session) -> set[str]:
    """本轮要监听的扩展名：视频扩展名（.strm 纸条时代已结束，不再监听）。"""
    return set(VIDEO_EXTS)

# 追新在一个季目录里最多往下钻几层。rclone 的一次递归列举会把整个子树拍平返回，
# 国产剧 1.4 万个文件时生产实测要几分钟、远超任何合理超时。改成逐层走公共通道后
# 每一跳都是「一个小目录」，但仍需要上限：层数不封顶，遇到结构异常深的面粉盘
# 会变成无界遍历。3 层足够盖住「季目录 / 特别篇 / 压制组」这类真实结构。
CHASE_MAX_DEPTH = max(1, int(os.getenv("CHASE_MAX_DEPTH", "3") or 3))
# 单个季目录递归时最多收多少条目：卡住不是因为结构，而是有人往里塞了几万个文件。
CHASE_MAX_ENTRIES = max(100, int(os.getenv("CHASE_MAX_ENTRIES", "5000") or 5000))

_WATCHER_STARTED = False
_WATCHER_LOCK = threading.Lock()


def _get_config(db: Session, key: str, default: str = "") -> str:
    """统一读（只许这一套）：daemon 轮询走直查，永远最新"""
    from backend.integrations import store
    return store.read_value(db, key, default)


def _config_exists(db: Session, key: str) -> bool:
    """配置行是否已存在（区分「没配过」与「配了但值为空」）

    迁移靠它判断能不能走：新键存在 = 已经在新语义下，不必再看旧键。
    """
    row = db.query(base_models.SystemConfig).filter(
        base_models.SystemConfig.key == key).first()
    return row is not None


def _set_config(db: Session, key: str, value: str) -> None:
    row = db.query(base_models.SystemConfig).filter(base_models.SystemConfig.key == key).first()
    if row:
        row.value = value
    else:
        row = base_models.SystemConfig(key=key, value=value)
        db.add(row)
    db.commit()


# ---------------------------------------------------------------------------
# 追新快照 / 源状态 / 运行历史 / 单例锁（P0-2 + P1-1 + P1-2）
# ---------------------------------------------------------------------------

def _get_source_state(db: Session, source_key: str):
    """取追新源状态行，没有返回 None。"""
    return db.get(em.ChaseSourceState, source_key)


def _touch_source_state(db: Session, source_key: str, ok: bool,
                        error: str = "", snapshot_now: bool = False) -> None:
    """更新源状态（get-or-create，commit）：
    - ok=True：last_ok_at=now(UTC)，consec_failures=0，last_error=""；
      snapshot_now=True 时 last_snapshot_at=now。
    - ok=False：consec_failures+1，last_error=error[:500]。
    """
    now = datetime.now(timezone.utc)
    state = _get_source_state(db, source_key)
    if state is None:
        state = em.ChaseSourceState(source_key=source_key)
        db.add(state)
    if ok:
        state.last_ok_at = now
        state.consec_failures = 0
        state.last_error = ""
        if snapshot_now:
            state.last_snapshot_at = now
    else:
        state.consec_failures = (state.consec_failures or 0) + 1
        state.last_error = error[:500]
    db.commit()


def _diff_against_snapshot(db: Session, library_id: int, source_key: str,
                           entries: list[tuple[str, float, float]],
                           baseline_if_empty: bool = True) -> list[str]:
    """文件指纹快照 diff。entries 是 (rel_path, size, mod_ts) 列表。

    - 一次查出该 (library_id, source_key) 的全部快照行建 dict。
    - 无记录 → 新增行（first_seen_at=last_seen_at=now UTC）并计入返回；
      size 或 mod_ts 变化 → 更新行并计入返回（mod_ts 比较用 abs 差 > 1e-6）。
    - 已见且未变 → 只更新 last_seen_at。
    - baseline_if_empty=True 且该源快照完全为空（首次建基线）→ 全部插入但
      返回 []（避免部署后第一轮把全库当新增触发扫描风暴）。调用方按
      「这个源是否从没成功检查过」传 baseline_if_empty（state 行不存在或
      last_ok_at 为空——注意失败也会建 state 行）：成功检查过一次之后新文件
      必须上报。
      注意：entries 本身为空时不算「建基线」——空目录首轮无行可插，
      若此时返回基线语义，之后新文件进来会因「快照仍为空」被再次当成基线
      静默吞掉（新库的第一个 .strm 永远触发不了扫描）。空 entries 直接走
      正常 diff（本来就是空，无风暴可言）。
    - 最后 db.commit() 一次。返回新增/变更的 rel_path 列表。
    """
    now = datetime.now(timezone.utc)
    existing = {
        row.rel_path: row
        for row in db.query(em.ChaseFileSnapshot).filter(
            em.ChaseFileSnapshot.library_id == library_id,
            em.ChaseFileSnapshot.source_key == source_key,
        ).all()
    }
    changed: list[str] = []

    if baseline_if_empty and not existing and entries:
        for rel_path, size, mod_ts in entries:
            db.add(em.ChaseFileSnapshot(
                library_id=library_id,
                source_key=source_key,
                rel_path=rel_path,
                size=size or 0,
                mod_ts=mod_ts or 0.0,
                first_seen_at=now,
                last_seen_at=now,
            ))
        db.commit()
        return []

    for rel_path, size, mod_ts in entries:
        size = size or 0
        mod_ts = mod_ts or 0.0
        row = existing.get(rel_path)
        if row is None:
            db.add(em.ChaseFileSnapshot(
                library_id=library_id,
                source_key=source_key,
                rel_path=rel_path,
                size=size,
                mod_ts=mod_ts,
                first_seen_at=now,
                last_seen_at=now,
            ))
            changed.append(rel_path)
            continue
        if (row.size or 0) != size or abs((row.mod_ts or 0.0) - mod_ts) > 1e-6:
            row.size = size
            row.mod_ts = mod_ts
            row.last_seen_at = now
            changed.append(rel_path)
        else:
            row.last_seen_at = now
    db.commit()
    return changed


def record_chase_run(db: Session, source: str, started_at: datetime,
                     finished_at: datetime, libs_checked: int = 0,
                     files_listed: int = 0, new_found: int = 0,
                     scans_triggered: int = 0, status: str = "ok",
                     error: str = "") -> None:
    """写一行追新运行历史（chase_run），commit。status 取 ok/partial/fail。"""
    db.add(em.ChaseRun(
        started_at=started_at, finished_at=finished_at, source=source,
        libs_checked=libs_checked, files_listed=files_listed,
        new_found=new_found, scans_triggered=scans_triggered,
        status=status, error=error or ""))
    db.commit()


_ADVISORY_LOCK_KEY = 20261010
_LOCAL_ADVISORY_LOCK = threading.Lock()


def _try_advisory_lock(db: Session) -> bool:
    """拿 PG advisory 锁（单例轮询用）。成功 True；拿不到 False。

    api 与 worker 两个容器都会起追新线程，锁保证实际只跑一份；
    拿不到锁的本轮跳过并打 debug 日志。
    """
    from sqlalchemy import text  # 函数内导入
    try:
        row = db.execute(
            text("SELECT pg_try_advisory_lock(:k)"),
            {"k": _ADVISORY_LOCK_KEY}).first()
        return bool(row and row[0])
    except Exception:
        # sqlite 等没有 pg_try_advisory_lock：降级为进程内线程锁（测试/单机够用）
        db.rollback()
        return _LOCAL_ADVISORY_LOCK.acquire(blocking=False)


def _release_advisory_lock(db: Session) -> None:
    """释放 ``_try_advisory_lock`` 拿到的锁。

    PG 的 session 级 advisory 锁**不会**随 ``Session.close()`` 释放：close 只把连接
    还回连接池，物理连接还活着，锁就一直挂在那条池连接上。之后本进程从池里拿到
    别的连接、或另一个容器来抢，都 ``pg_try_advisory_lock`` 失败——追新从此
    每轮「未拿到锁，跳过」，静默停摆。所以必须显式 ``pg_advisory_unlock``。
    """
    from sqlalchemy import text  # 函数内导入
    try:
        db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY})
        db.commit()
        return
    except Exception:
        # sqlite 等：走的是进程内线程锁
        try:
            db.rollback()
        except Exception:
            pass
    if _LOCAL_ADVISORY_LOCK.locked():
        _LOCAL_ADVISORY_LOCK.release()


def _parse_interval(raw: str) -> int:
    """解析轮询间隔，非法值按默认处理"""
    try:
        minutes = int(raw)
    except (ValueError, TypeError):
        logger.warning("[chase-new] 间隔配置非法 %r，按默认 %d 分钟", raw, DEFAULT_INTERVAL)
        return DEFAULT_INTERVAL
    if minutes < MIN_INTERVAL:
        logger.warning("[chase-new] 间隔 %d < 最小 %d，按最小处理", minutes, MIN_INTERVAL)
        return MIN_INTERVAL
    if minutes > MAX_INTERVAL:
        logger.warning("[chase-new] 间隔 %d > 最大 %d，按最大处理", minutes, MAX_INTERVAL)
        return MAX_INTERVAL
    return minutes


def _library_local_paths(library, db: Session) -> list[str]:
    """获取库的本机可读路径（local 类型挂载 + paths 里的本机目录）"""
    paths = []
    # paths 里的本机目录（非 mount:// 开头）——走 mounts 的统一拆法（全角逗号/去空/去重）
    for p in mount_lib.split_library_paths(getattr(library, "paths", "")):
        if not p.startswith("mount://") and os.path.isdir(p):
            paths.append(p)
    # mount_ids 引用的 local 类型挂载
    # 注意：按 AGENTS.md 教训，paths 用 mount://子目录时 mount_ids 应为空
    # 这里只处理 mount_ids 指向的 local 挂载的根
    mount_ids = mount_lib.parse_mount_ids(library)
    if mount_ids:
        mounts = db.query(em.StorageMount).filter(em.StorageMount.id.in_(mount_ids)).all()
        for m in mounts:
            if getattr(m, "mount_type", "") == "local" and getattr(m, "is_enabled", False):
                mp = getattr(m, "mount_path", "") or getattr(m, "local_path", "")
                if mp and os.path.isdir(mp):
                    paths.append(mp)
    return paths


MOUNT_PATH_PREFIX = "mount://"


def _library_mount_sources(library, db: Session) -> list[tuple[int, str]]:
    """库里引用的远程挂载：返回 ``[(mount_id, 挂载内相对目录), ...]``

    追新必须覆盖**远程**媒体源。生产实测全部库都是 ``mount://3/MoviePilot/...``
    （rclone RC），只查本机目录的旧实现等于一个库都没看——线程在跑、
    last_check 在更新，却永远发现不了新资源。
    """
    sources: list[tuple[int, str]] = []
    # 与扫描/监听同一个拆法（mounts.split_library_paths），不再自己 split(",")
    for p in mount_lib.split_library_paths(getattr(library, "paths", "")):
        if not p.startswith(MOUNT_PATH_PREFIX):
            continue
        rest = p[len(MOUNT_PATH_PREFIX):]
        mid, _, rel = rest.partition("/")
        if not mid.isdigit():
            continue
        sources.append((int(mid), "/" + rel.lstrip("/")))
    return sources


def _chase_provider(mount, db: Session):
    """构造追新要用的提供者（只覆盖**能给出远端 modTime** 的远程挂载）

    返回 None 表示这个挂载追新管不了（cli 模式 / 本机目录 / 未启用 / 构造失败），
    调用方直接跳过。构造失败只记日志不抛：这个挂载坏了不该让整轮追新中断。

    判据是「条目带得上 ``mod_ts``」——追新全靠它做新增窗口过滤。所以：
    - rclone rc 模式：``/operations/list`` 的 ModTime（纳秒，由 parse_mod_ts 归一）；
    - gdrive 原生：Drive ``modifiedTime``（RFC3339）；
    - cli 模式靠子进程列目录、ModTime 口径不同，且追新不是它的主场景，仍然排除。
    """
    if not getattr(mount, "is_enabled", False):
        return None
    try:
        provider = mount_lib.build_provider(mount, db)
    except Exception as exc:  # noqa: BLE001 — 构造失败（含未知类型）= 跳过该挂载
        logger.warning("[chase-new] 挂载 %s 构造提供者失败: %s",
                       getattr(mount, "id", None), exc)
        return None
    kind = getattr(provider, "kind", "")
    if kind != "remote":
        return None                      # 本机目录由 find -newermt 那条路处理
    mode = getattr(provider, "mode", "")
    if mode and mode != mount_rclone.MODE_RC:
        return None                      # rclone cli 模式：ModTime 口径不同，排除
    return provider


def _mount_url(mount_id: int, rel: str) -> str:
    """挂载内相对路径 → ``mount://<id>/<rel>``"""
    return f"{MOUNT_PATH_PREFIX}{int(mount_id)}/{(rel or '').lstrip('/')}"


def _walk_files(provider, rel: str, max_depth: int, max_entries: int,
               exts: set[str] | None = None) -> list:
    """从 ``rel`` 往下逐层找视频文件（每一跳都走公共通道）

    旧实现是一次 ``recurse=True`` 把整个子树拍平拿回来（国产剧 1.4 万个文件要几分钟、
    超时）。这里改成**逐层列一层**：单次响应小、稳，而且每一跳都吃得到缓存、单飞锁、
    限流名额、熔断保护与统计——这正是这个修复要的东西。

    层数与条目数都有上限：没有上限的话，结构异常深或异常大的目录会变成无界遍历。
    """
    found: list = []
    seen_dirs: set[str] = set()
    current = ["/" + (rel or "").lstrip("/")]
    depth = 0
    while current and depth < max_depth and len(found) < max_entries:
        nxt: list[str] = []
        for one in current:
            try:
                entries = provider.list_dir(one)
            except Exception as exc:  # noqa: BLE001 — 单个目录失败不影响其它
                logger.warning("[chase-new] 列 %s 失败: %s", one, exc)
                continue
            for entry in entries:
                if len(found) >= max_entries:
                    break
                if entry.is_dir:
                    child = "/" + (entry.rel or "").lstrip("/")
                    if child not in seen_dirs:
                        seen_dirs.add(child)
                        nxt.append(child)
                    continue
                if os.path.splitext(entry.name)[1].lower() in (exts or VIDEO_EXTS):
                    found.append(entry)
        current = nxt
        depth += 1
    if len(found) >= max_entries:
        logger.warning("[chase-new] %s 递归达到条目上限 %d（可能有异常大的目录），已截断",
                       rel, max_entries)
    return found


def _drive_changes_active() -> bool:
    """drive_changes 守护线程是否在跑。在跑则远程源靠 Changes API（O(Δ)），本轮跳过快照列举。"""
    try:
        from backend.emby_server import drive_changes as dc
        return dc.is_running()
    except Exception:
        return False


def _parent_prefix(p: str) -> str:
    """新文件路径 → 其所在目录（定向扫描的 prefixes 用）。
    mount://<id>/a/b/f.mkv → mount://<id>/a/b；本机路径用 os.path.dirname。"""
    if p.startswith(MOUNT_PATH_PREFIX):
        rest = p[len(MOUNT_PATH_PREFIX):]
        slash = rest.find("/")
        if slash == -1:
            return p
        mount_part = rest[:slash]
        parent = os.path.dirname(rest[slash:]) or "/"
        return MOUNT_PATH_PREFIX + mount_part + parent
    return os.path.dirname(p)


#: 远程快照 diff 每源最小间隔（秒）：文件级全量列举贵，平时靠 drive_changes
REMOTE_SNAPSHOT_MIN_INTERVAL = 3600
#: 远程快照列举的条目上限（国产剧单库上万文件，默认 5000 会截断漏检）
REMOTE_SNAPSHOT_MAX_ENTRIES = 100000
#: 从库根往下走的深度：库根/剧集/季 = 2 层，再加 CHASE_MAX_DEPTH（季内 特别篇/压制组）
_REMOTE_WALK_DEPTH = CHASE_MAX_DEPTH + 2


def _find_new_videos_remote(db: Session, library_id: int, mount_id: int,
                            rel_dir: str,
                            exts: set[str] | None = None) -> tuple[list[str], int]:
    """远程挂载：文件指纹快照 diff（P0-2）。

    不再按目录 mtime 过滤——生产实证 Drive 上季目录 mtime=2026-07-30 而其内新剧集
    文件 mtime=2026-08-08，旧实现把这类季目录整个跳过 → 新剧集永远发现不了。
    改为文件级列举（复用 _walk_files 逐层逻辑）拿 (rel_path, size, mod_ts)，
    与 chase_file_snapshot diff：快照无记录=新增，size/mod_ts 变化=变更。

    成本控制：每源每小时最多跑一次（last_snapshot_at）；drive_changes 在跑时
    直接返回空（平时靠 Changes API）。首次建基线返回空（防扫描风暴）。

    返回 (新增/变更文件的 mount:// URL 列表, 本轮列举的文件数)。
    异常直接抛给调用方（调用方记 consec_failures）。
    """
    # 源 key 必须精确到「挂载 + 库内目录」：以前按挂载（mount://3）算，而生产
    # 所有库都在 mount://3 下——第一个库跑完把 last_snapshot_at 置成 now，
    # 同一挂载上的其它库/目录在「每源每小时一次」门控下**永远被跳过**，
    # 新片只有第一个库能被追到。
    skey = _mount_url(mount_id, rel_dir or "/")
    state = _get_source_state(db, skey)
    if state and state.last_snapshot_at:
        last_dt = state.last_snapshot_at
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        if time.time() - last_dt.timestamp() < REMOTE_SNAPSHOT_MIN_INTERVAL:
            logger.debug("[chase-new] 远程源 %s 快照未满 1 小时，跳过", skey)
            return [], 0
    if _drive_changes_active():
        logger.debug("[chase-new] drive_changes 在跑，远程源 %s 本轮跳过快照列举", skey)
        return [], 0
    mount = db.query(em.StorageMount).filter(em.StorageMount.id == mount_id).first()
    if mount is None:
        return [], 0
    provider = _chase_provider(mount, db)
    if provider is None:
        return [], 0
    base = "/" + (rel_dir or "/").lstrip("/")
    with mount_lib.remote_io_purpose(mount_lib.PURPOSE_CHASE):
        entries = _walk_files(provider, base, _REMOTE_WALK_DEPTH,
                              REMOTE_SNAPSHOT_MAX_ENTRIES, exts or _tracked_exts(db))
    # 基线只建一次：从没**成功**检查过才建基线。注意失败也会建 state 行
    # （consec_failures+1 但 last_ok_at 为空），所以不能只看行是否存在——
    # 否则首轮失败、次轮成功时会把全库当新增上报（扫描风暴）。
    # 成功检查过一次之后新文件必须上报。
    changed = _diff_against_snapshot(db, library_id, skey,
                                     [(e.rel, e.size, e.mod_ts) for e in entries],
                                     baseline_if_empty=(state is None or
                                                        state.last_ok_at is None))
    _touch_source_state(db, skey, True, snapshot_now=True)
    return ([_mount_url(mount_id, r) for r in changed], len(entries))


def _find_new_videos_local(db: Session, library_id: int, base: str,
                           fallback_since_ts: float,
                           exts: set[str] | None = None) -> tuple[list[str], int]:
    """本机目录：find -newermt（since 取该源持久化的 last_ok_at，容器重建不丢失）
    + 快照 diff 二次确认（防 FUSE mtime 抖动误报）。
    返回 (新增/变更文件绝对路径列表, 本轮候选文件数)。失败时记源失败并抛给调用方。
    """
    state = _get_source_state(db, base)
    since_ts = fallback_since_ts
    if state and state.last_ok_at:
        last_dt = state.last_ok_at
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        since_ts = last_dt.timestamp()
    try:
        candidates = _find_new_videos([base], since_ts, exts or _tracked_exts(db))
        entries = []
        for p in candidates:
            try:
                st = os.stat(p)
            except OSError:
                continue
            rel = os.path.relpath(p, base).replace(os.sep, "/")
            entries.append(("/" + rel, st.st_size, st.st_mtime))
        # 基线只建一次：从没**成功**检查过才建基线。注意失败也会建 state 行
        # （_touch_source_state(ok=False) 是 get-or-create，last_ok_at 为空），
        # 所以不能只看行是否存在——否则首轮失败、次轮成功时会把全库当新增
        # 上报（扫描风暴）。只要成功检查过一次（哪怕当时是空目录），之后的新
        # 文件都必须上报——否则空目录首轮无行可插，下一轮「快照仍为空」会被
        # 再次当成基线静默吞掉，新库的第一个 .strm 永远触发不了扫描。
        changed = _diff_against_snapshot(db, library_id, base, entries,
                                         baseline_if_empty=(state is None or
                                                            state.last_ok_at is None))
        _touch_source_state(db, base, True)
        return ([os.path.join(base, r.lstrip("/")) for r in changed], len(entries))
    except Exception as exc:
        _touch_source_state(db, base, False, error=str(exc))
        raise



def _find_new_videos(paths: list[str], since_ts: float,
                     exts: set[str] | None = None) -> list[str]:
    """找出 since_ts 之后新增/修改的视频文件（用 find -newermt，C 实现比 os.walk 快）

    FUSE / rclone 挂载上 ``find`` 常在 60 秒内扫不完大目录。旧实现超时后直接
    ``跳过``——那意味着**这个目录的新片这一轮彻底丢失**，直到下一次定时扫描
    才可能补上（生产实测 2026-10-05：``/mnt/paul`` 上 find 超时被跳过多次）。
    现在超时后改走 Python 侧的 scandir（带 mtime 过滤），慢但能跑完；
    两种方式都失败才如实记一条告警，不再静默丢。
    """
    import subprocess
    from datetime import datetime, timezone

    new_files = []
    since_str = datetime.fromtimestamp(since_ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # 构建 find 的扩展名过滤：\( -iname "*.mp4" -o -iname "*.mkv" ... \)
    ext_args = []
    for i, ext in enumerate(sorted(exts or VIDEO_EXTS)):
        if i > 0:
            ext_args.append("-o")
        ext_args.extend(["-iname", f"*{ext}"])

    for base in paths:
        try:
            cmd = ["find", base, "-type", "f", "-newermt", since_str, "("] + ext_args + [")", "-print"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=_FIND_TIMEOUT_SEC)
            if result.returncode == 0 and result.stdout.strip():
                new_files.extend(line for line in result.stdout.strip().split("\n") if line)
            elif result.returncode != 0:
                logger.warning("[chase-new] find %s 失败: %s", base, result.stderr[:200])
        except subprocess.TimeoutExpired:
            logger.warning(
                "[chase-new] find %s 超时（%ss），改用 scandir 兜底",
                base, _FIND_TIMEOUT_SEC)
            new_files.extend(_scandir_new_videos(base, since_ts, exts))
        except Exception as e:
            logger.warning("[chase-new] find %s 异常: %s", base, e)
    return new_files


def _scandir_new_videos(base: str, since_ts: float,
                        exts: set[str] | None = None) -> list[str]:
    """``find`` 超时后的兜底：os.scandir + mtime 过滤（慢，但不会丢整个目录）

    不用 ``os.walk``：它默认对每个目录都 ``stat``，在 FUSE 上会额外放大往返。
    ``scandir`` 的 ``entry.stat()`` 只在需要时付代价，且我们本来就要 mtime。
    单个目录读不动就跳过该目录并记告警——宁可少一个目录，不要整轮失败。
    """
    import os

    suffixes = tuple(e.lower() for e in (exts or VIDEO_EXTS))
    found: list = []
    try:
        with os.scandir(base) as it:
            for entry in it:
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    if not entry.name.lower().endswith(suffixes):
                        continue
                    if entry.stat(follow_symlinks=False).st_mtime <= since_ts:
                        continue
                except OSError:
                    continue      # 单个条目读不动就跳过，不影响其余
                found.append(entry.path)
    except OSError as exc:
        logger.warning("[chase-new] scandir %s 失败: %s", base, exc)
    return found


#: 库 id 的合理上限（SQLite/PG 的主键都是 64 位整数）。粘进来的超长数字串直接丢掉，
#: 不让它在配置里越攒越长
_MAX_LIBRARY_ID = mount_lib.MAX_ID_VALUE


def _parse_ids(raw) -> list[int]:
    """把 "1,2,  3" 这种字串解析成去重后的整数列表（脏值忽略，不报错）

    转发给 ``mounts.parse_id_list``（v2.46.0）：追新配置与 ``Library.mount_ids``
    必须是同一个解析口，否则同一串 id 在两处可能解出不同结果——追新决定谁被监听，
    扫描决定扫哪里，两边不一致就是“界面说在听、实际没扫”。
    """
    return mount_lib.parse_id_list(raw)


def _enabled_library_ids(db: Session) -> list[int]:
    return [row[0] for row in db.query(em.Library.id)
            .filter(em.Library.is_enabled == True).all()]


def resolve_excluded(db: Session) -> list[int]:
    """当前生效的**排除清单**（含旧包含清单的一次性迁移）

    迁移只做一次：新键写成功后旧键清空，之后就只认新键。
    """
    stored = _get_config(db, CONFIG_EXCLUDED, "")
    if _config_exists(db, CONFIG_EXCLUDED):
        return _parse_ids(stored)
    legacy = _get_config(db, CONFIG_LIBRARIES, "")
    if not _parse_ids(legacy):
        # 旧键也是空 = 本来就是「全部监听」，新语义一致，不用写
        return []
    included = set(_parse_ids(legacy))
    migrated = sorted(i for i in _enabled_library_ids(db) if i not in included)
    _set_config(db, CONFIG_EXCLUDED, ",".join(str(i) for i in migrated))
    _set_config(db, CONFIG_LIBRARIES, "")
    logger.info("[chase-new] 旧包含清单 %s 已迁移为排除清单 %s",
                legacy, ",".join(str(i) for i in migrated))
    return migrated


def _check_once() -> None:
    """执行一轮检查：快照 diff 发现新增/变更 → 定向扫描 → 写运行历史。"""
    t0 = time.time()
    started = datetime.now(timezone.utc)
    db = SessionLocal()
    stats = {"libs": 0, "listed": 0, "new": 0, "scans": 0, "errors": 0}
    status = "ok"
    fatal_error = ""
    ran = False
    try:
        if _get_config(db, CONFIG_ENABLED, "0") != "1":
            return
        ran = True
        interval = _parse_interval(_get_config(db, CONFIG_INTERVAL, str(DEFAULT_INTERVAL)))
        fallback_since = time.time() - interval * 2 * 60
        excluded = set(resolve_excluded(db))
        # 扩展名集合每轮只算一次：_get_config 是直查 DB，逐源重复读是 N+1
        exts = _tracked_exts(db)
        libraries = [lib for lib in db.query(em.Library).filter(em.Library.is_enabled == True).all()
                     if lib.id not in excluded]
        for lib in libraries:
            stats["libs"] += 1
            try:
                found: list[str] = []
                for base in _library_local_paths(lib, db):
                    try:
                        paths, n = _find_new_videos_local(db, lib.id, base, fallback_since, exts)
                    except Exception as exc:
                        stats["errors"] += 1
                        logger.warning("[chase-new] 库《%s》本地源 %s 检查失败: %s",
                                       getattr(lib, "name", lib.id), base, exc)
                        continue
                    stats["listed"] += n
                    found += paths
                for mid, rel in _library_mount_sources(lib, db):
                    try:
                        paths, n = _find_new_videos_remote(db, lib.id, mid, rel, exts)
                    except Exception as exc:
                        stats["errors"] += 1
                        logger.warning("[chase-new] 库《%s》远程挂载 %s 检查失败: %s",
                                       getattr(lib, "name", lib.id), mid, exc)
                        continue
                    stats["listed"] += n
                    found += paths
                if found:
                    stats["new"] += len(found)
                    prefixes = sorted({_parent_prefix(p) for p in found})
                    logger.info("[chase-new] 库《%s》发现 %d 个新增/变更文件，定向扫描 %d 个目录",
                                getattr(lib, "name", lib.id), len(found), len(prefixes))
                    scan_queue.enqueue_targeted(lib, prefixes, trigger="chase-new")
                    stats["scans"] += 1
            except Exception as e:
                stats["errors"] += 1
                logger.error("[chase-new] 库《%s》检查失败: %s", getattr(lib, "id", "?"), e)
        _set_config(db, CONFIG_LAST_CHECK, datetime.now(timezone.utc).isoformat())
        _set_config(db, CONFIG_LAST_FOUND, str(stats["new"]))
        if stats["errors"]:
            status = "partial" if (stats["new"] or stats["scans"]) else "fail"
    except Exception as e:
        status = "fail"
        fatal_error = str(e)[:500]
        logger.error("[chase-new] 轮询异常: %s", e)
    finally:
        if ran:
            try:
                record_chase_run(db, source="poll", started_at=started,
                                 finished_at=datetime.now(timezone.utc),
                                 libs_checked=stats["libs"], files_listed=stats["listed"],
                                 new_found=stats["new"], scans_triggered=stats["scans"],
                                 status=status, error=fatal_error)
            except Exception:
                logger.warning("[chase-new] 写运行历史失败", exc_info=True)
        db.close()
    dur = int(time.time() - t0)
    logger.info("[chase-new] round done: libs=%d listed=%d new=%d scans=%d dur=%ds errors=%d",
                stats["libs"], stats["listed"], stats["new"], stats["scans"], dur, stats["errors"])



def _maybe_check_once() -> None:
    """拿 advisory 锁并跑一轮；拿不到就跳过（api/worker 双容器单例，P1-2）。"""
    db = SessionLocal()
    try:
        if _try_advisory_lock(db):
            try:
                _check_once()
            finally:
                # PG advisory 锁不会随 session 关闭释放（连接回池仍存活），必须显式解锁；
                # 降级到进程内锁时释放线程锁
                _release_advisory_lock(db)
        else:
            logger.debug("[chase-new] 未拿到 advisory lock，本轮跳过（另一进程正在跑）")
    finally:
        db.close()



def _watcher_loop() -> None:
    """轮询线程主循环"""
    logger.info("[chase-new] 追新线程启动")
    while True:
        try:
            db = SessionLocal()
            try:
                enabled = _get_config(db, CONFIG_ENABLED, "0") == "1"
                interval = _parse_interval(_get_config(db, CONFIG_INTERVAL, str(DEFAULT_INTERVAL)))
            finally:
                db.close()
            if enabled:
                _maybe_check_once()
            time.sleep(interval * 60)
        except Exception as e:
            logger.error("[chase-new] 线程异常: %s", e)
            time.sleep(60)



def get_config(db: Session) -> dict:
    """追新当前配置（管理后台展示）

    ``excluded`` 是排除清单（v2.45.0 起的唯一口径）；``libraries`` 保留为空串，
    因为老前端还在读它——给一个非空值会让老前端把排除清单当成包含清单用。
    """
    return {
        "enabled": _get_config(db, CONFIG_ENABLED, "0") == "1",
        "interval": _parse_interval(_get_config(db, CONFIG_INTERVAL, str(DEFAULT_INTERVAL))),
        "excluded": ",".join(str(i) for i in resolve_excluded(db)),
        "libraries": "",
        "last_check": _get_config(db, CONFIG_LAST_CHECK, ""),
        "last_found": int(_get_config(db, CONFIG_LAST_FOUND, "0") or "0"),
    }


def save_config(db: Session, enabled: bool, interval: int,
                excluded: str = "", libraries: Optional[str] = None) -> dict:
    """保存追新配置，立即生效

    ``excluded`` 是排除清单。``libraries`` 是**旧字段**（包含清单），只为老调用方保留：
    传了它就按老语义换算成排除清单（启用库 − 包含清单），而不是直接当排除清单存——
    否则一个还在用老前端的部署会把清单含义整个反过来。

    """
    minutes = _parse_interval(str(interval))
    _set_config(db, CONFIG_ENABLED, "1" if enabled else "0")
    _set_config(db, CONFIG_INTERVAL, str(minutes))
    if libraries is not None and not excluded:
        included = set(_parse_ids(libraries))
        ids = sorted(i for i in _enabled_library_ids(db) if i not in included)
    else:
        ids = _parse_ids(excluded)
    _set_config(db, CONFIG_EXCLUDED, ",".join(str(i) for i in ids))
    # 旧键清空：万一还有进程在按老口径读，它看到的是「空 = 全部监听」，
    # 与新语义下的“排除为空”一致，不会出现两边理解打架。
    _set_config(db, CONFIG_LIBRARIES, "")
    logger.info("[chase-new] 配置已保存: enabled=%s interval=%d excluded=%s",
                enabled, minutes, ",".join(str(i) for i in ids))
    # 排除清单同时管着 inotify（v2.46.0 起两边同语义），所以改完要立即重建监听，
    # 否则被排除的库还要等容器重启才真的停下来。重建走 sync_from_db() 内部幂等：
    # 差异对比 + unschedule/schedule，不会因为反复调而叠出第二个 observer。
    try:
        from backend.emby_server import fs_watcher

        fs_watcher.sync_from_db()
    except Exception:  # noqa: BLE001 — 刷新监听失败不影响配置已保存的事实
        logger.warning("[chase-new] 刷新实时监听失败（配置已保存，重启后自愈）", exc_info=True)
    return get_config(db)


def start_chase_new_watcher() -> None:
    """启动追新线程（幂等）"""
    global _WATCHER_STARTED
    with _WATCHER_LOCK:
        if _WATCHER_STARTED:
            return
        _WATCHER_STARTED = True
    t = threading.Thread(target=_watcher_loop, daemon=True, name="chase-new-watcher")
    t.start()
    logger.info("[chase-new] 已启动")
