# -*- coding: utf-8 -*-
"""转存副本清理（2026-10-27 用户亲定：副本是缓存，不是原件）。

口径（写死，不许按直觉放宽）：
- 源片一直在共享盘，删副本不丢内容；用户再点开按现行门禁自动
  重新转存再播，只是多等几秒。所以“不看了”就该清，否则用户盘
  会被转存副本塞满（2026-10-27 当天用户 1 一人就攒了 42 条）。
- stale 立即删：status='stale' 的行已不再用于播放（巡检两次
  strike 算死，或播放中实证标死），清掉其在用户绑定盘转存目录
  里的文件，并把行标为 'deleted'（行留作审计，source_file_id
  永留档；选路只认 ready，deleted 行绝不会再被选到）。
- ready 按 LRU、按需腾地方：不按固定天数一刀切，不批量删；
  只有调用方明确给出腾地方的需求（need_bytes > 0 或
  max_count > 0）时，才按“最久没播”顺序逐条腾，腾够即停。
  没有需求时 evict_lru_variants 一条都不删（函数内硬性拦截，
  不是文档约定）。
- 追踪先行：mark_variant_played 由会话上报（Playing / Progress /
  Stopped）调用，把对应用户、对应条目的 ready 行 last_played_at
  回写。回写带 1 小时节流（同行 1 小时内只写一次），不破坏会话
  上报本身的写库节流。追踪修好前，LRU 排序只能用会话进度作
  代理（调用方传 session_progress），且首轮清理只删 stale、
  不碰 ready——2026-10-27 实证：用户 1 当天实际播过的 4 部，
  变体 last_played_at 全空，拿它排 LRU 会误删今天播过的。
- 解绑不自动删：delete_binding（user_bindings.py）不调用本模块；
  解绑时转存目录原样留在用户盘里，须另行问用户。任何清理代码
  都只许碰转存目录内的 variant_file_id，绝不按盘根遍历删。
- 盘外文件不动：删除只接受来自变体行的 variant_file_id，且
  fid 必须过正则（Drive fid 字符集，无斜杠、无点、无路径成分）；
  不合规的行计 skipped_unsafe，行不动、文件不动。
- 行来源即目录保证：变体行只由转存工人在“目录核对”（在用户
  转存目录里按名核到）后登记，所以 userdrive: 行的 fid 天生就
  是该用户转存目录里的文件；本模块不按文件名遍历、不按盘根
  遍历。残留：Drive files.delete 按 fid 删，本模块在删前用
  get_file 确认存在性（404 即按已删处理），但不另做 parents
  复核——parents 复核需 files.list 按父目录查询，现有客户端
  没有该口径，不在本 PR 编造。
- 115 行不删：pan115: 行的删除口径（115 删除 API）未经实测，
  本模块不编造调用；这类行计 skipped_unsupported，行不动、
  文件不动，留待 115 删除口径实测后再补。
- 每一次删（含已删）都记日志：用户、条目、文件 id、大小、
  结果；统计以返回的 stats 为准，调用方按 stats 审计。
"""

import logging
import re
from datetime import datetime, timedelta

from backend.emby_server import models as em
from backend.emby_server import transfer_variant as tv

logger = logging.getLogger(__name__)

DELETED_STATUS = "deleted"
# Drive file id 字符集：字母数字 + _-，无斜杠、无点，绝无路径成分。
_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
# 播放回写节流：同一 ready 行 1 小时内只写一次 last_played_at。
PLAYED_WRITE_MIN_INTERVAL_SEC = 3600


def is_safe_variant_file_id(fid) -> bool:
    """变体文件 id 是否可删：非空、Drive fid 字符集、无路径成分。"""
    if not isinstance(fid, str):
        return False
    return bool(_FILE_ID_RE.match(fid.strip())) and fid.strip() == fid


def mark_variant_played(db, user_id, item_id, now=None) -> bool:
    """会话上报回写：该用户该条目的 ready 变体 last_played_at = now。

    只写 ready + owner_scope='user' + owner_ref=str(user_id) 的行，
    绝不跨用户、绝不动 stale/released/deleted 行。1 小时内已写过
    的行跳过（不产生写事务）。返回 True = 本次写了库。
    调用方（会话上报）必须 try/except 包住本函数：追踪失败绝不
    影响播放上报本身。
    """
    if user_id is None or item_id is None:
        return False
    now = now or datetime.now()
    rows = (db.query(em.TransferVariant)
            .filter(em.TransferVariant.item_id == item_id,
                    em.TransferVariant.owner_scope == "user",
                    em.TransferVariant.owner_ref == str(user_id),
                    em.TransferVariant.status == "ready")
            .all())
    changed = False
    cutoff = now - timedelta(seconds=PLAYED_WRITE_MIN_INTERVAL_SEC)
    for row in rows:
        last = getattr(row, "last_played_at", None)
        if last is not None and last >= cutoff:
            continue
        row.last_played_at = now
        changed = True
    if changed:
        db.commit()
    return changed


def _effective_last_played(row, session_progress):
    """LRU 排序键：变体 last_played_at > 会话进度代理 > 创建时间。

    session_progress 是 {(user_id, item_id): datetime}，由调用方
    从 emby_playback_sessions 的上报里取（追踪修好前的代理口径）。
    全空时回落 created_at，绝不回落成“现在”（回落成现在会把
    老副本排到最后、永远腾不掉）。
    """
    last = getattr(row, "last_played_at", None)
    if last is not None:
        return last
    if session_progress:
        try:
            proxy = session_progress.get(
                (int(row.owner_ref), row.item_id))
        except (TypeError, ValueError):
            proxy = None
        if proxy is not None:
            return proxy
    created = getattr(row, "created_at", None)
    if created is not None:
        return created
    return datetime.min


def select_lru_candidates(db, user_id, limit=None, *,
                          session_progress=None, exclude_ids=()):
    """该用户 ready 变体按 LRU 顺序列出（只选，不删）。

    排序：最久没播在前（effective 升序），同值按 created_at、
    再按 id 升序——排序是全序的，同一批输入同一批输出，不随机。
    limit 为 None = 全部；调用方（evict）必须另给需求量，否则
    不删（见 evict_lru_variants 的硬性拦截）。
    """
    q = (db.query(em.TransferVariant)
         .filter(em.TransferVariant.owner_scope == "user",
                 em.TransferVariant.owner_ref == str(user_id),
                 em.TransferVariant.status == "ready")
         .order_by(em.TransferVariant.id))
    rows = q.all()
    if exclude_ids:
        excluded = set(exclude_ids)
        rows = [r for r in rows if r.id not in excluded]
    rows.sort(key=lambda r: (_effective_last_played(r, session_progress),
                             getattr(r, "created_at", None) or datetime.min,
                             r.id))
    if limit is not None:
        rows = rows[:max(0, int(limit))]
    return rows


def _log_pre_delete(row, reason):
    logger.info(
        "[transfer-cleanup] 删前记账 reason=%s user=%s item=%s "
        "variant=%s file=%s size=%s dir=%s",
        reason, row.owner_ref, row.item_id, row.id,
        row.variant_file_id, getattr(row, "size_bytes", None),
        tv.transfer_dir_name())


def _delete_one_drive_file(db, row, client, drive_marker, now):
    """删一个 Drive 用户副本（供 stale 与 LRU 共用，同一套守卫）。

    返回 (outcome, freed_bytes)：outcome in deleted / already_gone /
    failed。删前先 get_file：404/_NotFound 即文件已不在，行直接
    标 deleted（不再发 delete，避免对不存在的文件写盘）；其它
    get 失败不删、行不动。delete 成功才标 deleted 并提交；
    delete 失败行不动、计 failed。调用方负责 client 的来源（该
    用户自己的绑定令牌），本函数不碰凭据、不换绑定。
    """
    fid = (getattr(row, "variant_file_id", "") or "").strip()
    if not is_safe_variant_file_id(fid):
        logger.warning(
            "[transfer-cleanup] 跳过不合规 fid user=%s item=%s "
            "variant=%s file=%r", row.owner_ref, row.item_id, row.id,
            getattr(row, "variant_file_id", ""))
        return "skipped_unsafe", 0
    get_file = getattr(client, "get_file", None)
    if get_file is not None:
        try:
            get_file(drive_marker, fid)
        except Exception as exc:  # noqa: BLE001 — 客户端抛 _NotFound 即已删
            if type(exc).__name__ == "_NotFound" or "404" in str(exc):
                row.status = DELETED_STATUS
                row.last_checked_at = now
                db.commit()
                return "already_gone", 0
            logger.warning(
                "[transfer-cleanup] 删前核查失败，不删 user=%s item=%s "
                "variant=%s file=%s err=%s", row.owner_ref, row.item_id,
                row.id, fid, exc)
            return "failed", 0
    try:
        client.delete_file(drive_marker, fid)
    except Exception as exc:  # noqa: BLE001 — 删失败行不动，fail-closed
        logger.warning(
            "[transfer-cleanup] 删文件失败，行不动 user=%s item=%s "
            "variant=%s file=%s err=%s", row.owner_ref, row.item_id,
            row.id, fid, exc)
        return "failed", 0
    row.status = DELETED_STATUS
    row.last_checked_at = now
    db.commit()
    size = getattr(row, "size_bytes", None) or 0
    return "deleted", int(size)


def _user_drive_client(db, user_id, drive_client_factory):
    """该用户的 Drive 客户端（工厂入参，生产由调用方按绑定行给）。

    工厂签名：factory(db, user_id) -> (client, drive_marker)，
    drive_marker 形如 "myDrive:<uid>"（与工人 Path A 同款）。
    工厂返回 None 即该用户无可用客户端，调用方计 skipped。
    """
    if drive_client_factory is None:
        return None, None
    return drive_client_factory(db, user_id)


def cleanup_stale_variants(db, drive_client_factory, *, limit=None):
    """首轮清理：只删 stale 行，不碰 ready（硬性范围）。

    返回 stats：{checked, deleted, already_gone, skipped_unsupported,
    skipped_unsafe, skipped_no_client, failed, bytes_freed}。
    - Drive 用户行（variant_drive_id 以 "userdrive:" 开头）走删；
    - pan115 行与其它 scope 行计 skipped_unsupported，行不动；
    - 无可用客户端的行计 skipped_no_client，行不动；
    - 每行删前都记日志（用户、条目、文件 id、大小）。
    本函数不调度、不定时：首轮生产运行须由运维在部署后手动触发
    并复核日志，绝不由巡检默认带跑（见 PR 描述的触发口径）。
    """
    stats = {"checked": 0, "deleted": 0, "already_gone": 0,
             "skipped_unsupported": 0, "skipped_unsafe": 0,
             "skipped_no_client": 0, "failed": 0, "bytes_freed": 0}
    q = (db.query(em.TransferVariant)
         .filter(em.TransferVariant.status == "stale")
         .order_by(em.TransferVariant.id))
    rows = q.all()
    if limit is not None:
        rows = rows[:max(0, int(limit))]
    now = datetime.now()
    clients = {}
    for row in rows:
        stats["checked"] += 1
        drive_id = (getattr(row, "variant_drive_id", "") or "").strip()
        if (getattr(row, "owner_scope", "") != "user"
                or not drive_id.startswith("userdrive:")):
            stats["skipped_unsupported"] += 1
            logger.info(
                "[transfer-cleanup] 跳过非 Drive 用户行 user=%s item=%s "
                "variant=%s drive=%s", row.owner_ref, row.item_id,
                row.id, drive_id)
            continue
        try:
            user_id = int(row.owner_ref)
        except (TypeError, ValueError):
            stats["skipped_unsupported"] += 1
            continue
        if user_id not in clients:
            clients[user_id] = _user_drive_client(db, user_id,
                                                  drive_client_factory)
        client, marker = clients[user_id]
        if client is None:
            stats["skipped_no_client"] += 1
            logger.warning(
                "[transfer-cleanup] 无可用客户端，行不动 user=%s item=%s "
                "variant=%s", row.owner_ref, row.item_id, row.id)
            continue
        _log_pre_delete(row, "stale")
        outcome, freed = _delete_one_drive_file(db, row, client, marker,
                                                now)
        if outcome == "deleted":
            stats["deleted"] += 1
            stats["bytes_freed"] += freed
        elif outcome == "already_gone":
            stats["already_gone"] += 1
        elif outcome == "skipped_unsafe":
            stats["skipped_unsafe"] += 1
        else:
            stats["failed"] += 1
    logger.info("[transfer-cleanup] stale 清理完成 stats=%s", stats)
    return stats


def evict_lru_variants(db, drive_client_factory, user_id, *,
                       need_bytes=None, max_count=None,
                       session_progress=None):
    """按需腾地方：只在调用方给出需求时删 ready 行，否则一条不删。

    需求二选一（至少一个为正数）：need_bytes（要腾出的字节数，
    腾够即停）或 max_count（最多腾几条）。两者都未给时返回
    {"evicted": [], "reason": "no_need"} 且不删任何行——这是
    规则 3 的代码级保证，不是文档约定。
    触发口径（见 PR 描述）：v1 不由工人自动触发，因为用户盘剩余
    空间未实测（Drive about.get 未接线）；需求须由已实测剩余空间
    的调用方（或运维手动）给出。备选口径：接通 about.get 后，改为
    “新转存前实测剩余空间 < 新文件大小时，need_bytes = 新文件
    大小 - 剩余空间”自动触发——该接线不在本 PR，不编造成已接通。
    返回 {evicted: [variant_id...], bytes_freed, reason}。
    """
    if not need_bytes and not max_count:
        return {"evicted": [], "bytes_freed": 0, "reason": "no_need"}
    need = int(need_bytes or 0)
    cap = int(max_count or 0)
    client, marker = _user_drive_client(db, user_id,
                                        drive_client_factory)
    if client is None:
        return {"evicted": [], "bytes_freed": 0,
                "reason": "no_client"}
    candidates = select_lru_candidates(
        db, user_id, session_progress=session_progress)
    evicted = []
    freed = 0
    now = datetime.now()
    for row in candidates:
        if need and freed >= need:
            break
        if cap and len(evicted) >= cap:
            break
        _log_pre_delete(row, "lru_evict")
        outcome, size = _delete_one_drive_file(db, row, client, marker,
                                               now)
        if outcome in ("deleted", "already_gone"):
            evicted.append(row.id)
            freed += size
        else:
            # 删失败不跳过整批：继续往后腾（失败行留 ready，下次
            # 再排），但失败本身已在 _delete_one 里记日志。
            continue
    reason = "done"
    if need and freed < need:
        reason = "insufficient_candidates"
    return {"evicted": evicted, "bytes_freed": freed, "reason": reason}
