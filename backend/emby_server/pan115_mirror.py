# -*- coding: utf-8 -*-
"""115 增量镜像（FMBY #21）：持久代际镜像 + 轮询差分 + 调度。

FMBY 原形：全量同步落一代镜像（pan115_raw_nodes：node_id /
parent_directory_id / pickcode / sha1 / is_deleted /
last_seen_generation），之后靠 life 事件 + 脏目录做差分刷新，
核心侧对应 pan115_sync_checkpoints / pan115_sync_tasks /
pan115_life_events。我方已有的 #1328 sidecar 只做到了前半段的
进程内形态：full_sync 的 generation 是单调计数、life_poll 快照
只存 sidecar 进程内存、不落盘、不跨进程；diff_refresh 是
stateless 重列。本模块补上缺的那半：镜像、检查点、事件全部
落库，调度由 worker 常驻线程跑，重启后快照不丢。

诚实口径（写进 PR 描述，不许反向宣称）：
- 115 没有原生推送、没有 delta 接口，list_dir 也不给 mtime：
  所以“增量”在本模块指**增量应用**，不是增量枚举。枚举仍需
  逐目录列一次（与全量同 API 量），但只有指纹变化的节点才写
  事件、才值得下游处理；未变化节点只刷新 last_seen_generation，
  不产生事件、不触发下游。
- full 与 incremental 的区别是删除判定范围，不是枚举量：
  full 递归整树，轮末把本代际未见的节点全标 deleted；
  incremental 只列调用方给的 watched_cids（默认全部已知目录），
  只有“父目录被本轮列到、而子项不在列举里”的节点才标
  deleted，未列到的目录的子项绝不标 deleted——一次部分列举
  绝不能误判成删除，这正是脏目录语义；被标删的目录其子树
  按 path 前缀级联标删（目录没了，里面东西一定也没了），
  级联不到的残留由下一轮 full 清理。调度器首轮（无检查点）
  走 full，之后走 incremental。
- 指纹 = name / size / sha1 / pickcode / is_dir 的哈希。sha1
  常为空，空时指纹退化为名 + 大小 + pickcode，不拿 pickcode
  冒充哈希（pickcode 只是 115 的下载凭证标识）。
- 本模块不改 emby_items：由镜像事件驱动条目增删改（替代扫描器
  全量 walk）是下一单，扫描器是多路并行在改的共享文件，本单
  不强行接。下游消费口是 events_since / dirty_directories。
- 去重：(scope, node_key) 唯一约束 + 同轮内先胜，同一节点在
  一轮里出现两次只记一次；重复跑同一代际内容不产生重复行、
  不产生重复事件（指纹相同即 unchanged）。
- 调度默认关：PAN115_MIRROR_ENABLED 默认 0。开闸后无 115 账号
  时线程不启动；Cookie 失效只记检查点 last_error，不 crash 循环。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime

from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

PAN115_MIRROR_ENABLED = (
    (os.getenv("PAN115_MIRROR_ENABLED", "0") or "0").strip().lower()
    not in {"0", "false", "no", "off"}
)
PAN115_MIRROR_INTERVAL_SEC = env_int("PAN115_MIRROR_INTERVAL_SEC", 1800, 300)

MAX_SYNC_ENTRIES = 100000
MAX_SYNC_DIRS = 20000

EVENT_ADDED = "added"
EVENT_CHANGED = "changed"
EVENT_DELETED = "deleted"
EVENT_TYPES = (EVENT_ADDED, EVENT_CHANGED, EVENT_DELETED)


# ---------------------------------------------------------------------------
# 指纹与身份
# ---------------------------------------------------------------------------

def entry_fingerprint(entry: dict) -> str:
    """115 条目指纹：名 + 大小 + sha1 + pickcode + 是否目录。"""
    raw = "|".join([
        str(entry.get("name") or ""),
        str(int(entry.get("size") or 0)),
        str(entry.get("sha1") or ""),
        str(entry.get("pickcode") or ""),
        "d" if entry.get("is_dir") else "f",
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def node_key_for(entry: dict, parent_key: str) -> tuple[str, bool]:
    """节点身份键 + 是否有真实 fid。

    目录：dir:<cid>；有 fid 的文件：fid:<fid>；无 fid 的文件退化为
    path:<parent_key>:<name> 且 has_fid=False——绝不拿父目录 cid
    冒充文件身份（#13 PR-3 教训：同目录文件会共享身份错认）。
    """
    if entry.get("is_dir"):
        return "dir:%s" % (entry.get("cid") or ""), True
    fid = str(entry.get("fid") or "")
    if fid:
        return "fid:%s" % fid, True
    return "path:%s:%s" % (parent_key, entry.get("name") or ""), False


def _join_path(parent_path: str, name: str) -> str:
    if not parent_path or parent_path == "/":
        return "/" + (name or "")
    return parent_path.rstrip("/") + "/" + (name or "")


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

def walk_full(lister, root_cid: str) -> tuple[list[dict], bool]:
    """递归整树：返回 (节点列表, 是否截断)。

    节点 dict：node_key / parent_key / name / path / is_dir / size /
    pickcode / sha1 / has_fid / fingerprint。目录环用已访 cid 集合
    挡住；节点数超 MAX_SYNC_ENTRIES 即截断（与 sidecar 同口径）。
    """
    nodes: list[dict] = []
    seen_keys: set[str] = set()
    visited_dirs: set[str] = set()
    # 根目录子项的 parent_key 统一为 dir:<root_cid>，与增量列举同口径，
    # 否则 full 与 incremental 的删除判定会对不上。
    queue: list[tuple[str, str, str]] = [
        (str(root_cid or "0"), "dir:%s" % (root_cid or "0"), "/")]
    truncated = False
    while queue:
        cid, parent_key, parent_path = queue.pop(0)
        if cid in visited_dirs:
            continue
        visited_dirs.add(cid)
        if len(visited_dirs) > MAX_SYNC_DIRS:
            truncated = True
            break
        try:
            entries = lister(cid) or []
        except Exception:
            raise
        for entry in entries:
            key, has_fid = node_key_for(entry, parent_key)
            if key in seen_keys:
                continue  # 同轮去重：先胜
            seen_keys.add(key)
            path = _join_path(parent_path, str(entry.get("name") or ""))
            nodes.append({
                "node_key": key,
                "parent_key": parent_key,
                "name": str(entry.get("name") or ""),
                "path": path,
                "is_dir": bool(entry.get("is_dir")),
                "size": int(entry.get("size") or 0),
                "pickcode": str(entry.get("pickcode") or ""),
                "sha1": str(entry.get("sha1") or ""),
                "has_fid": has_fid,
                "fingerprint": entry_fingerprint(entry),
            })
            if len(nodes) >= MAX_SYNC_ENTRIES:
                return nodes, True
            if entry.get("is_dir"):
                child_cid = str(entry.get("cid") or "")
                if child_cid and child_cid not in visited_dirs:
                    queue.append((child_cid, key, path))
    return nodes, truncated


def list_watched(lister, watched_cids) -> list[dict]:
    """增量枚举：只列 watched 各自的直接子项，不递归下钻。

    watched 元素为 cid 字符串或 (cid, parent_path) 元组；每个子项
    的 parent_key 取其所在目录的 dir:<cid>，path 由父路径拼接。
    目录子项本身只记一行，不展开——展开是下一轮 full 或该子目录
    被列入 watched 时的事。未列到的目录绝不参与删除判定。
    """
    nodes: list[dict] = []
    seen_keys: set[str] = set()
    for spec in watched_cids or []:
        if isinstance(spec, (tuple, list)):
            cid, parent_path = str(spec[0]), str(spec[1] or "/")
        else:
            cid, parent_path = str(spec), "/"
        parent_key = "dir:%s" % cid
        for entry in lister(cid) or []:
            key, has_fid = node_key_for(entry, parent_key)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            nodes.append({
                "node_key": key,
                "parent_key": parent_key,
                "name": str(entry.get("name") or ""),
                "path": _join_path(parent_path, str(entry.get("name") or "")),
                "is_dir": bool(entry.get("is_dir")),
                "size": int(entry.get("size") or 0),
                "pickcode": str(entry.get("pickcode") or ""),
                "sha1": str(entry.get("sha1") or ""),
                "has_fid": has_fid,
                "fingerprint": entry_fingerprint(entry),
            })
            if len(nodes) >= MAX_SYNC_ENTRIES:
                return nodes
    return nodes


# ---------------------------------------------------------------------------
# 落库：代际应用
# ---------------------------------------------------------------------------

def _get_checkpoint(db, scope: str):
    from backend.emby_server import models as em

    row = db.query(em.Pan115SyncCheckpoint).filter(
        em.Pan115SyncCheckpoint.scope == scope).first()
    if row is None:
        row = em.Pan115SyncCheckpoint(scope=scope, root_cid="0")
        db.add(row)
        db.flush()
    return row


def run_sync(db, *, scope: str, lister, root_cid: str,
             mode: str = "full", watched_cids: list[str] | None = None) -> dict:
    """跑一轮同步，单事务落库。返回计数 dict。

    mode="full"：递归整树，轮末未见节点标 deleted；
    mode="incremental"：只列 watched_cids（默认全部已知目录），
    未列到的目录不标 deleted；增量列举里路径为空的节点沿用
    镜像旧行的 path，不把 path 洗成空。
    失败时异常上抛、事务由调用方回滚，检查点不推进（断点续跑）。
    """
    from backend.emby_server import models as em

    if mode not in ("full", "incremental"):
        raise ValueError("mode 须为 full 或 incremental，收到 %r" % mode)
    if mode == "incremental" and watched_cids is None:
        dir_rows = db.query(em.Pan115RawNode).filter(
            em.Pan115RawNode.scope == scope,
            em.Pan115RawNode.is_dir == True,  # noqa: E712
            em.Pan115RawNode.is_deleted == False,  # noqa: E712
        ).all()
        watched_cids = [
            (r.node_key[4:], r.path or "/") for r in dir_rows
            if r.node_key.startswith("dir:")
        ]
        if root_cid and str(root_cid) not in [str(c) for c, _ in watched_cids]:
            watched_cids.append((str(root_cid), "/"))

    checkpoint = _get_checkpoint(db, scope)
    generation = int(checkpoint.last_generation or 0) + 1

    if mode == "full":
        fresh, truncated = walk_full(lister, root_cid)
    else:
        fresh = list_watched(lister, watched_cids or [])
        truncated = False

    existing = {
        r.node_key: r for r in db.query(em.Pan115RawNode).filter(
            em.Pan115RawNode.scope == scope).all()
    }
    fresh_keys = set()
    counts = {"added": 0, "changed": 0, "deleted": 0, "unchanged": 0,
              "truncated": bool(truncated)}
    for node in fresh:
        key = node["node_key"]
        fresh_keys.add(key)
        row = existing.get(key)
        path = node["path"] or (row.path if row is not None else "")
        if row is None:
            db.add(em.Pan115RawNode(
                scope=scope, node_key=key, parent_key=node["parent_key"],
                name=node["name"], path=path, is_dir=node["is_dir"],
                size=node["size"], pickcode=node["pickcode"],
                sha1=node["sha1"], has_fid=node["has_fid"],
                fingerprint=node["fingerprint"],
                last_seen_generation=generation, is_deleted=False))
            db.add(em.Pan115LifeEvent(
                scope=scope, generation=generation, event_type=EVENT_ADDED,
                node_key=key, path=path, fingerprint=node["fingerprint"]))
            counts["added"] += 1
        elif row.is_deleted or row.fingerprint != node["fingerprint"]:
            event = EVENT_ADDED if row.is_deleted else EVENT_CHANGED
            row.parent_key = node["parent_key"]
            row.name = node["name"]
            row.path = path
            row.is_dir = node["is_dir"]
            row.size = node["size"]
            row.pickcode = node["pickcode"]
            row.sha1 = node["sha1"]
            row.has_fid = node["has_fid"]
            row.fingerprint = node["fingerprint"]
            row.last_seen_generation = generation
            row.is_deleted = False
            db.add(em.Pan115LifeEvent(
                scope=scope, generation=generation, event_type=event,
                node_key=key, path=path, fingerprint=node["fingerprint"]))
            counts["changed" if event == EVENT_CHANGED else "added"] += 1
        else:
            row.last_seen_generation = generation
            counts["unchanged"] += 1

    # 删除判定：full 覆盖全部父目录；incremental 只覆盖本轮列到的
    # 父目录（watched 的 dir:<cid>），未列到的目录的子项不动。
    if mode == "full":
        listed_parents = None
    else:
        listed_parents = set()
        for spec in (watched_cids or []):
            cid = spec[0] if isinstance(spec, (tuple, list)) else spec
            listed_parents.add("dir:%s" % cid)

    def _mark_deleted(row) -> None:
        row.is_deleted = True
        row.last_seen_generation = generation
        db.add(em.Pan115LifeEvent(
            scope=scope, generation=generation,
            event_type=EVENT_DELETED, node_key=row.node_key, path=row.path,
            fingerprint=row.fingerprint))
        counts["deleted"] += 1

    deleted_dir_paths: list[str] = []
    for key, row in existing.items():
        if key in fresh_keys or row.is_deleted:
            continue
        if listed_parents is None or row.parent_key in listed_parents:
            _mark_deleted(row)
            if row.is_dir and row.path:
                deleted_dir_paths.append(row.path)
    # 目录删除级联子树：目录没了，path 前缀在其下的行一定也没了。
    if deleted_dir_paths:
        for key, row in existing.items():
            if row.is_deleted:
                continue
            if any(row.path.startswith(p + "/") for p in deleted_dir_paths):
                _mark_deleted(row)

    checkpoint.root_cid = str(root_cid or "0")
    checkpoint.last_generation = generation
    checkpoint.last_mode = mode
    checkpoint.last_counts = json.dumps(counts, ensure_ascii=False)
    checkpoint.last_error = ""
    checkpoint.last_run_at = datetime.now()
    db.commit()
    logger.info("115 镜像 scope=%s 代际=%s 模式=%s %s",
                scope, generation, mode, counts)
    return {"scope": scope, "generation": generation, "mode": mode, **counts}


def events_since(db, scope: str, generation: int) -> list:
    """取某代际之后（含）的生命事件，供下游消费。"""
    from backend.emby_server import models as em

    return db.query(em.Pan115LifeEvent).filter(
        em.Pan115LifeEvent.scope == scope,
        em.Pan115LifeEvent.generation >= int(generation),
    ).order_by(em.Pan115LifeEvent.id).all()


def dirty_directories(db, scope: str, generation: int) -> list[str]:
    """某代际里有子项变化的父目录 path（去重、排序）。

    事件行本身不存父目录，父目录从事件节点的镜像行 parent_key
    反查：parent_key 为 dir:<cid> 的取该目录行的 path。
    """
    from backend.emby_server import models as em

    events = events_since(db, scope, generation)
    node_keys = {e.node_key for e in events}
    parents = set()
    if node_keys:
        rows = db.query(em.Pan115RawNode).filter(
            em.Pan115RawNode.scope == scope,
            em.Pan115RawNode.node_key.in_(list(node_keys))).all()
        parents = {r.parent_key for r in rows if r.parent_key}
    dir_keys = {p for p in parents if p.startswith("dir:")}
    paths = set()
    if dir_keys:
        dir_rows = db.query(em.Pan115RawNode).filter(
            em.Pan115RawNode.scope == scope,
            em.Pan115RawNode.node_key.in_(sorted(dir_keys))).all()
        paths = {r.path for r in dir_rows if r.path}
    return sorted(paths)


# ---------------------------------------------------------------------------
# 调度：worker 常驻线程（默认关）
# ---------------------------------------------------------------------------

_mirror_thread: threading.Thread | None = None
_mirror_stop = threading.Event()
_mirror_lock = threading.Lock()


def _discover_scopes(db) -> list[dict]:
    """可同步的 scope：启用的 115 账号配置档各一个 scope。

    挂载级 scope（按库根 cid）是下一单：账号级根 cid=0 已覆盖
    整号，挂载只是账号树下的一个子目录。
    """
    from backend.emby_server import models as em

    accounts = db.query(em.Pan115Account).filter(
        em.Pan115Account.is_enabled == True).all()  # noqa: E712
    return [{"scope": "account:%s" % a.id, "account_id": a.id,
             "root_cid": "0"} for a in accounts]


def _run_one_tick() -> None:
    from backend.database import SessionLocal
    from backend.emby_server import transfer115

    db = SessionLocal()
    try:
        scopes = _discover_scopes(db)
        for spec in scopes:
            session = SessionLocal()
            try:
                from backend.emby_server import models as em

                checkpoint = session.query(
                    em.Pan115SyncCheckpoint).filter(
                    em.Pan115SyncCheckpoint.scope == spec["scope"]).first()
                mode = "full" if checkpoint is None else "incremental"
                try:
                    cookie, _src = transfer115.resolve_cookie(
                        session, account_id=spec["account_id"])
                    ua = transfer115.resolve_ua(
                        session, account_id=spec["account_id"])
                    if not cookie:
                        raise transfer115.Pan115Error("115 账号无可用 Cookie")
                    client = transfer115.Pan115Client(cookie, ua=ua)
                    run_sync(session, scope=spec["scope"],
                             lister=client.list_dir,
                             root_cid=spec["root_cid"], mode=mode)
                except Exception as exc:  # noqa: BLE001 — 单 scope 失败不杀整轮
                    session.rollback()
                    checkpoint = _get_checkpoint(session, spec["scope"])
                    checkpoint.last_error = str(exc)[:500]
                    session.commit()
                    logger.warning("115 镜像 scope=%s 本轮失败：%s",
                                   spec["scope"], exc)
            finally:
                session.close()
    finally:
        db.close()


def _mirror_loop() -> None:
    while not _mirror_stop.is_set():
        try:
            with _mirror_lock:
                _run_one_tick()
        except Exception as exc:  # noqa: BLE001 — 调度循环永不因单轮异常退出
            logger.warning("115 镜像调度轮次异常（下轮重试）：%s", exc)
        _mirror_stop.wait(PAN115_MIRROR_INTERVAL_SEC)


def start() -> bool:
    """启动 115 镜像调度线程。返回是否成功启动。"""
    global _mirror_thread
    if not PAN115_MIRROR_ENABLED:
        logger.info("115 增量镜像已禁用（PAN115_MIRROR_ENABLED=0）")
        return False
    if _mirror_thread and _mirror_thread.is_alive():
        return True
    _mirror_stop.clear()
    _mirror_thread = threading.Thread(
        target=_mirror_loop, daemon=True, name="pan115-mirror")
    _mirror_thread.start()
    return True


def stop(timeout: float = 5.0) -> None:
    """停调度线程（测试与关停用）。"""
    global _mirror_thread
    _mirror_stop.set()
    if _mirror_thread and _mirror_thread.is_alive():
        _mirror_thread.join(timeout=timeout)
    _mirror_thread = None
