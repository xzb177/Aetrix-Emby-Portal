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
- 115 删除口径证据等级（2026-10-27 115 能力收尾时亲验，GRADE 0）：
  仓库里没有任何 115 删除端点、参数或响应形态的记录，也没有
  真实账号验证；本模块不猜端点、不写删除调用。任何后续删除
  代码落地前，必须先由持 115 测试账号的人确认端点 + 参数 +
  成功/失败响应形态，并把确认结果写进测试清单，空口断言不算。
  为让这批行可被运维/测试者逐条核对，stale 清理与 LRU 腾地方
  的 stats 里都带 skipped_rows（variant_id / owner_ref / item_id /
  drive_id / file_id / reason），115 行的 reason 一律为
  pan115_delete_unverified。
- LRU 腾地方只腾 Drive 行：select_lru_candidates 本身只选不删，
  但 evict_lru_variants 曾把 pan115: 行也交给 Drive 客户端——Drive
  files.get 查 115 fid 必然 404，会被误标 already_gone（115 文件
  实际还在）。本模块起，evict 循环里非 userdrive: 行一律跳过
  （计 skipped_unsupported、进 skipped_rows、行不动、不调客户端），
  与 stale 路径同口径。
- 每一次删（含已删）都记日志：用户、条目、文件 id、大小、
  结果；统计以返回的 stats 为准，调用方按 stats 审计。
- 转存前量空间（2026-10-27 用户亲定“转存前量空间，不够先腾
  旧副本”）：ensure_space_for_transfer 是自动触发的唯一入口，
  只对 Drive 用户盘生效。about.get 取用户盘剩余空间，剩余 >=
  文件大小直接转、不腾；不足时 need_bytes = 文件大小 - 剩余
  空间，用 evict 按最久没播顺序腾同用户 ready 副本、腾够即停。
  正在播的不腾：活跃会话（ended_at 为空且未暂停过期——
  last_update_at 在 600 秒内，或 is_paused）对应条目的副本，
  以及 600 秒内有播放回写的副本，一律排除；正在转的本条
  （exclude_item_ids）同样排除。腾完仍不足时明确失败并报清
  三数（文件大小、剩余空间、还差多少），绝不静默回落共享。
  about.get 拿不到、文件大小未知、无可用客户端时 fail-closed
  的含义只针对删除侧：不据此腾任何地方，照旧走 copy（旧行为），
  copy 中途的配额错仍由调用方的腾一次重试兜底——绝不
  因为一次配额查询抖动就把转存直接判死。115 盘无 Drive
  about.get 口径，本单不覆盖：evict 只删 userdrive: 行，
  pan115: 行一律跳过、行不动。
- 追剧保护（2026-10-29，生产实证：用户正在追的剧的副本被 LRU
  腾掉，再播又要重新转存）：触发条件不变——仍然只有空间不足
  时才腾，不新增任何定时或主动删除。ensure_space 改为两轮：
  第一轮排除“正在播 + exclude_item_ids + 受保护”；腾完仍不够
  时第二轮只排除“正在播 + exclude_item_ids”，按原 LRU 顺序只在
  受保护行里继续腾，被迫删掉的受保护行逐条 warning——宁可删
  受保护行，也不阻断新转存。受保护 = protected_variant_ids：
  (1) TRANSFER_EVICT_RECENT_HOURS（默认 6）小时内播过的 ready
  副本，“播过”与 LRU 排序键同口径（变体 last_played_at > 会话
  进度代理 session_progress；不看 created_at，刚转存没播过的不
  算）；(2) 最近在追的剧：按 emby_user_media_data.last_played_at
  与该用户会话上报取每部剧最近播的那一集，按 season_number /
  episode_number 排序，当前集 + 其后 TRANSFER_EVICT_NEXT_EPISODES
  （默认 2）集的 ready 副本。保护集算不出来时记 warning、按无
  保护处理（退回原行为），不阻断转存；“正在播”的排除集算不出
  来仍按 active_check_failed 一条不腾（硬排除，口径不变）。
"""

import logging
import os
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
# 正在播排除窗口：活跃会话 last_update_at 在 600 秒内，或
# is_paused 的未结束会话，对应条目的副本一律不腾；600 秒内
# 有播放回写的副本同样不腾（会话行缺失时的代理口径）。
ACTIVE_PLAYING_WINDOW_SEC = 600
# 追剧保护（软排除，空间仍不够时第二轮可删）：近期播放窗口（小时）
# 与追剧往后保护的集数，均可由同名环境变量调；非法值或负数回落
# 默认，0 = 关掉该条保护。调用时读（不在 import 时固化）。
EVICT_RECENT_HOURS_DEFAULT = 6.0
EVICT_NEXT_EPISODES_DEFAULT = 2
# “最近在追”的回看范围与剧数上限（模块常量，不开放环境变量）：
# 只看 14 天内播过的剧，最多取最近 10 部，防止追剧保护面无限扩大
# 后第一轮几乎总腾不够、两轮退化成纯 LRU。
WATCHING_LOOKBACK_DAYS = 14
WATCHING_MAX_SERIES = 10


def active_playing_variant_ids(db, user_id, *, now=None,
                               window_sec=ACTIVE_PLAYING_WINDOW_SEC):
    """该用户“正在播”副本的变体 id 集合（腾地方时一律排除）。

    两条口径取并集，任一命中即排除：
    - 活跃会话：emby_playback_sessions 里 user_id 相同、ended_at
      为空，且 last_update_at 为空（从未上报过，仍算未结束）
      或在窗口内，或 is_paused（暂停仍是播一半的状态，恢复时
      还要读这个副本）。已结束（ended_at 非空）的不算；未暂停
      且 last_update_at 超出窗口的，视为 janitor 口径下的过期
      会话，不再排除（否则僵尸会话会永久锁住副本腾不掉）。
    - 近期播放：同用户 ready 行 last_played_at 在窗口内。
    只返回变体 id，不删任何行；会话查询本身抛异常时异常向
    上抛，由 ensure_space 按 active_check_failed 处理（不腾）。
    """
    now = now or datetime.now()
    cutoff = now - timedelta(seconds=window_sec)
    ids = set()
    sessions = (db.query(em.PlaybackSession)
                .filter(em.PlaybackSession.user_id == user_id,
                        em.PlaybackSession.ended_at.is_(None))
                .all())
    item_ids = set()
    for session in sessions:
        last = getattr(session, "last_update_at", None)
        if (last is None or last >= cutoff
                or bool(getattr(session, "is_paused", False))):
            if getattr(session, "item_id", None) is not None:
                item_ids.add(session.item_id)
    if item_ids:
        rows = (db.query(em.TransferVariant)
                .filter(em.TransferVariant.owner_scope == "user",
                        em.TransferVariant.owner_ref == str(user_id),
                        em.TransferVariant.status == "ready",
                        em.TransferVariant.item_id.in_(item_ids))
                .all())
        ids.update(row.id for row in rows)
    recent = (db.query(em.TransferVariant)
              .filter(em.TransferVariant.owner_scope == "user",
                      em.TransferVariant.owner_ref == str(user_id),
                      em.TransferVariant.status == "ready",
                      em.TransferVariant.last_played_at.isnot(None),
                      em.TransferVariant.last_played_at >= cutoff)
              .all())
    ids.update(row.id for row in recent)
    return ids


def _env_non_negative(name, default, cast):
    """读非负数环境变量：未设/非法/负数回落默认，0 合法（= 关）。"""
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def evict_recent_hours():
    """近期播放保护窗口（小时），TRANSFER_EVICT_RECENT_HOURS，默认 6。"""
    return _env_non_negative("TRANSFER_EVICT_RECENT_HOURS",
                             EVICT_RECENT_HOURS_DEFAULT, float)


def evict_next_episodes():
    """追剧往后保护集数，TRANSFER_EVICT_NEXT_EPISODES，默认 2。"""
    return _env_non_negative("TRANSFER_EVICT_NEXT_EPISODES",
                             EVICT_NEXT_EPISODES_DEFAULT, int)


def _played_marker(row, session_progress):
    """“播过”的时间点：与 _effective_last_played 同口径但不回落创建时间。

    变体 last_played_at > 会话进度代理；两者皆无返回 None（刚转存
    还没播过的副本不算“近期播过”，否则新副本全成受保护行）。
    """
    last = getattr(row, "last_played_at", None)
    if last is not None:
        return last
    if session_progress:
        try:
            return session_progress.get((int(row.owner_ref), row.item_id))
        except (TypeError, ValueError):
            return None
    return None


def _episode_order_key(item):
    """同剧单集排序：季号、集号（缺号排后），再按 id 兜成全序。"""
    season = getattr(item, "season_number", None)
    episode = getattr(item, "episode_number", None)
    return (season is None, season or 0, episode is None, episode or 0,
            item.id)


def _watching_item_ids(db, user_id, now, next_episodes):
    """最近在追的剧：每部剧“当前集 + 其后 next_episodes 集”的条目 id。

    当前集 = 该剧里该用户最近播过的那一集，时间取
    emby_user_media_data.last_played_at 与会话上报（last_update_at，
    缺则 start_time）中较晚者；只看 WATCHING_LOOKBACK_DAYS 天内、
    最近 WATCHING_MAX_SERIES 部剧。已软删、已合并的单集不进排序。
    """
    cutoff = now - timedelta(days=WATCHING_LOOKBACK_DAYS)
    latest = {}

    def _bump(item_id, ts):
        if item_id is None or ts is None or ts < cutoff:
            return
        if item_id not in latest or ts > latest[item_id]:
            latest[item_id] = ts

    umd_rows = (db.query(em.UserMediaData.item_id,
                         em.UserMediaData.last_played_at)
                .filter(em.UserMediaData.user_id == user_id,
                        em.UserMediaData.last_played_at.isnot(None),
                        em.UserMediaData.last_played_at >= cutoff)
                .all())
    for item_id, ts in umd_rows:
        _bump(item_id, ts)
    sess_rows = (db.query(em.PlaybackSession.item_id,
                          em.PlaybackSession.last_update_at,
                          em.PlaybackSession.start_time)
                 .filter(em.PlaybackSession.user_id == user_id,
                         em.PlaybackSession.item_id.isnot(None))
                 .all())
    for item_id, last_update, start in sess_rows:
        _bump(item_id, last_update or start)
    if not latest:
        return set()
    played_items = (db.query(em.MediaItem)
                    .filter(em.MediaItem.id.in_(list(latest)))
                    .all())
    # 每部剧取最近播的那一集（同时刻按 id 兜底，保证确定）。
    current_by_series = {}
    protected = set()
    for item in played_items:
        if (getattr(item, "item_type", "") or "").lower() != "episode":
            continue
        series_id = getattr(item, "series_id", None)
        if series_id is None:
            # 挂不到剧上：找不到后续集，只保护这一集本身。
            protected.add(item.id)
            continue
        ts = latest[item.id]
        cur = current_by_series.get(series_id)
        if cur is None or (ts, item.id) > (latest[cur.id], cur.id):
            current_by_series[series_id] = item
    ranked = sorted(current_by_series.items(),
                    key=lambda kv: (latest[kv[1].id], kv[1].id),
                    reverse=True)[:WATCHING_MAX_SERIES]
    for series_id, current in ranked:
        protected.add(current.id)
        if next_episodes <= 0:
            continue
        episodes = (db.query(em.MediaItem)
                    .filter(em.MediaItem.series_id == series_id,
                            em.MediaItem.deleted_at.is_(None),
                            em.MediaItem.merged_into_id.is_(None))
                    .all())
        episodes = [e for e in episodes
                    if (getattr(e, "item_type", "") or "").lower()
                    == "episode"]
        episodes.sort(key=_episode_order_key)
        cur_key = _episode_order_key(current)
        after = [e for e in episodes if _episode_order_key(e) > cur_key]
        protected.update(e.id for e in after[:next_episodes])
    return protected


def protected_variant_ids(db, user_id, now=None, *, session_progress=None):
    """该用户受保护副本的变体 id 集合（LRU 第一轮软排除，只选不删）。

    两条口径取并集，只返回 ready + 同用户的行：
    - 近期播过：TRANSFER_EVICT_RECENT_HOURS 小时内有播放（与 LRU
      排序键同口径：last_played_at，空则会话进度代理）；
    - 最近在追：见 _watching_item_ids，当前集 + 其后
      TRANSFER_EVICT_NEXT_EPISODES 集；该集被合并的版本行
      （merged_into_id 指向它）的副本一并保护。
    异常原样上抛，由 ensure_space 记 warning 后按无保护处理。
    """
    now = now or datetime.now()
    rows = (db.query(em.TransferVariant)
            .filter(em.TransferVariant.owner_scope == "user",
                    em.TransferVariant.owner_ref == str(user_id),
                    em.TransferVariant.status == "ready")
            .all())
    if not rows:
        return set()
    ids = set()
    hours = evict_recent_hours()
    if hours > 0:
        cutoff = now - timedelta(hours=hours)
        for row in rows:
            marker = _played_marker(row, session_progress)
            if marker is not None and marker >= cutoff:
                ids.add(row.id)
    item_ids = _watching_item_ids(db, user_id, now, evict_next_episodes())
    if item_ids:
        merged = (db.query(em.MediaItem.id)
                  .filter(em.MediaItem.merged_into_id.in_(list(item_ids)))
                  .all())
        item_ids.update(mid for (mid,) in merged)
        ids.update(row.id for row in rows if row.item_id in item_ids)
    return ids


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


def cleanup_stale_variants(db, drive_client_factory, *, limit=None,
                            user_id=None, exclude_variant_ids=()):
    """首轮清理：只删 stale 行，不碰 ready（硬性范围）。

    返回 stats：{checked, deleted, already_gone, skipped_unsupported,
    skipped_unsafe, skipped_no_client, failed, bytes_freed,
    skipped_rows: [{variant_id, owner_ref, item_id, drive_id, file_id,
    reason}]}。
    - Drive 用户行（variant_drive_id 以 "userdrive:" 开头）走删；
    - pan115 行与其它 scope 行计 skipped_unsupported，行不动；
      pan115 行的 reason 一律为 pan115_delete_unverified（删除口径
      证据等级 GRADE 0，见模块说明，本函数不编造删除调用）；
    - 无可用客户端的行计 skipped_no_client，行不动；
    - 每行删前都记日志（用户、条目、文件 id、大小）。
    本函数不调度、不定时：首轮生产运行须由运维在部署后手动触发
    并复核日志，绝不由巡检默认带跑（见 PR 描述的触发口径）。
    user_id 给出时只清该用户的 stale 行（ensure_space_for_transfer
    的腾地方前置 pass 用，不跨用户删）；exclude_variant_ids 命中的
    行（正在播等）一律跳过、行不动。
    """
    stats = {"checked": 0, "deleted": 0, "already_gone": 0,
             "skipped_unsupported": 0, "skipped_unsafe": 0,
             "skipped_no_client": 0, "failed": 0, "bytes_freed": 0,
             "skipped_rows": []}
    q = (db.query(em.TransferVariant)
         .filter(em.TransferVariant.status == "stale")
         .order_by(em.TransferVariant.id))
    if user_id is not None:
        q = q.filter(em.TransferVariant.owner_scope == "user",
                     em.TransferVariant.owner_ref == str(user_id))
    if exclude_variant_ids:
        excluded = set(exclude_variant_ids)
        q = q.filter(~em.TransferVariant.id.in_(excluded))
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
            stats["skipped_rows"].append(_skipped_row(
                row, "pan115_delete_unverified"
                if drive_id.startswith("pan115:") else "unsupported_drive"))
            logger.info(
                "[transfer-cleanup] 跳过非 Drive 用户行 user=%s item=%s "
                "variant=%s drive=%s", row.owner_ref, row.item_id,
                row.id, drive_id)
            continue
        try:
            user_id = int(row.owner_ref)
        except (TypeError, ValueError):
            stats["skipped_unsupported"] += 1
            stats["skipped_rows"].append(_skipped_row(
                row, "unsupported_owner_ref"))
            continue
        if user_id not in clients:
            clients[user_id] = _user_drive_client(db, user_id,
                                                  drive_client_factory)
        client, marker = clients[user_id]
        if client is None:
            stats["skipped_no_client"] += 1
            stats["skipped_rows"].append(_skipped_row(row, "no_client"))
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
            stats["skipped_rows"].append(_skipped_row(row, "unsafe_fid"))
        else:
            stats["failed"] += 1
    logger.info("[transfer-cleanup] stale 清理完成 stats=%s", stats)
    return stats


def _skipped_row(row, reason):
    """跳过行的可核对快照（只含行上已有字段，不调客户端、不写库）。"""
    return {"variant_id": getattr(row, "id", None),
            "owner_ref": getattr(row, "owner_ref", None),
            "item_id": getattr(row, "item_id", None),
            "drive_id": getattr(row, "variant_drive_id", None),
            "file_id": getattr(row, "variant_file_id", None),
            "reason": reason}


def evict_lru_variants(db, drive_client_factory, user_id, *,
                       need_bytes=None, max_count=None,
                       session_progress=None, exclude_variant_ids=()):
    """按需腾地方：只在调用方给出需求时删 ready 行，否则一条不删。

    需求二选一（至少一个为正数）：need_bytes（要腾出的字节数，
    腾够即停）或 max_count（最多腾几条）。两者都未给时返回
    {"evicted": [], "reason": "no_need"} 且不删任何行——这是
    规则 3 的代码级保证，不是文档约定。
    触发口径：自动触发的唯一入口是 ensure_space_for_transfer
    （新转存前实测 about.get，剩余 < 文件大小时 need_bytes =
    文件大小 - 剩余空间）；运维手动给出需求时同样走本函数。
    只腾 Drive 用户盘行：候选里 variant_drive_id 非 userdrive:
    开头的行（含 pan115: 行）一律跳过，计 skipped_unsupported、
    进 skipped_rows、行不动、不调客户端——绝不把 115 fid 交给
    Drive 客户端查删（Drive 查 115 fid 必然 404，会被误标
    already_gone）。exclude_variant_ids 命中的行（正在播、
    正在转的本条）一律不删。
    返回 {evicted: [variant_id...], bytes_freed, reason,
    skipped_unsupported, skipped_rows, skipped_unsafe, failed}，
    其中 skipped_unsafe / failed 与 stale 清理同口径（不合规 fid
    的行计 skipped_unsafe 并进 skipped_rows，删前核查/删除失败
    的行计 failed、行不动），调用方按 stats 审计时不再把“有候选
    但删不掉”误读成“无候选”。
    """
    if not need_bytes and not max_count:
        return {"evicted": [], "bytes_freed": 0, "reason": "no_need",
                "skipped_unsupported": 0, "skipped_rows": [],
                "skipped_unsafe": 0, "failed": 0}
    need = int(need_bytes or 0)
    cap = int(max_count or 0)
    client, marker = _user_drive_client(db, user_id,
                                        drive_client_factory)
    if client is None:
        return {"evicted": [], "bytes_freed": 0,
                "reason": "no_client",
                "skipped_unsupported": 0, "skipped_rows": [],
                "skipped_unsafe": 0, "failed": 0}
    candidates = select_lru_candidates(
        db, user_id, session_progress=session_progress,
        exclude_ids=exclude_variant_ids)
    evicted = []
    freed = 0
    skipped_unsupported = 0
    skipped_rows = []
    skipped_unsafe = 0
    failed = 0
    now = datetime.now()
    for row in candidates:
        if need and freed >= need:
            break
        if cap and len(evicted) >= cap:
            break
        _drive_id = (getattr(row, "variant_drive_id", "") or "").strip()
        if (getattr(row, "owner_scope", "") != "user"
                or not _drive_id.startswith("userdrive:")):
            skipped_unsupported += 1
            skipped_rows.append(_skipped_row(
                row, "pan115_delete_unverified"
                if _drive_id.startswith("pan115:") else "unsupported_drive"))
            logger.info(
                "[transfer-cleanup] LRU 跳过非 Drive 行 user=%s item=%s "
                "variant=%s drive=%s", row.owner_ref, row.item_id,
                row.id, _drive_id)
            continue
        _log_pre_delete(row, "lru_evict")
        outcome, size = _delete_one_drive_file(db, row, client, marker,
                                               now)
        if outcome in ("deleted", "already_gone"):
            evicted.append(row.id)
            freed += size
        elif outcome == "skipped_unsafe":
            # 与 stale 路径同口径：不合规 fid 行计入统计与 skipped_rows，
            # 行不动；继续往后腾，不跳过整批。
            skipped_unsafe += 1
            skipped_rows.append(_skipped_row(row, "unsafe_fid"))
        else:
            # 删失败不跳过整批：继续往后腾（失败行留 ready，下次
            # 再排），失败计数与 _delete_one 里的日志双通道可查。
            failed += 1
    reason = "done"
    if need and freed < need:
        reason = "insufficient_candidates"
    return {"evicted": evicted, "bytes_freed": freed, "reason": reason,
            "skipped_unsupported": skipped_unsupported,
            "skipped_rows": skipped_rows,
            "skipped_unsafe": skipped_unsafe,
            "failed": failed}


def ensure_space_for_transfer(db, drive_client_factory, user_id, *,
                              file_size, session_progress=None,
                              exclude_item_ids=(), now=None):
    """新转存前量空间（自动触发的唯一入口，只对 Drive 用户盘）。

    返回 dict：{reason, proceed, file_size, free_bytes, shortfall,
    evicted, bytes_freed}；真正进入腾地方时另带 protected（本次
    软排除的受保护变体 id）与 evicted_protected（第二轮被迫腾掉
    的受保护变体 id，evicted 已含）。reason 取值：
    - "sufficient"：剩余 >= 文件大小，一条不腾，直接转；
    - "evicted_enough"：腾后估算剩余（实测剩余 + 实际删掉的
      字节数，about 配额有传播延迟，不以二次 about 为准）>=
      文件大小，可以转；
    - "insufficient"：腾完所有可腾副本仍不足，调用方必须明确
      失败并报清三数（file_size / free_bytes / shortfall），绝
      不静默回落共享——proceed 为 False 的唯一情形；
    - "quota_unknown"：about.get 拿不到（客户端无该方法、抛错、
      非 200），删除侧 fail-closed：一条不腾，proceed 为 True
      照旧走 copy（旧行为），copy 中途的配额错仍由调用方兜底；
    - "unknown_size"：文件大小未知/<=0，算不出 need，一条不腾，
      照旧走 copy；
    - "no_client"：工厂给不出该用户客户端，一条不腾，照旧走
      copy（copy 路径自会按无绑定失败）；
    - "active_check_failed"：正在播排除集合算不出来，删除侧
      fail-closed：一条不腾，照旧走 copy（宁可 copy 时报配额
      错，也不冒着删掉正在播的副本的风险硬腾）。
    只腾同用户、转存目录内的 ready 副本：evict 的 userdrive:
    guard + exclude（正在播 + exclude_item_ids 对应的本条）双
    重兜底；个人文件、115 行、stale/deleted 行一律不在候选里
    （候选查询本身只取 status=ready 的用户行）。
    但腾 ready 之前先对本用户跑一轮 stale 清理（A-090）：stale 死
    副本先清、字节先计入估算，stale 够了就不再动 ready 行；stale
    清理本身 best-effort，异常只记 warning、不阻断后续腾 ready。
    返回里 stale_deleted / stale_freed 为本轮 stale pass 的计数与
    字节（bytes_freed 仍只计 ready 腾地方，evicted 仍只含 ready
    行 id）；估算剩余 = free + stale_freed + bytes_freed。
    """
    now = now or datetime.now()
    result = {"reason": "sufficient", "proceed": True, "file_size": file_size,
              "free_bytes": None, "shortfall": None, "evicted": [],
              "bytes_freed": 0, "stale_deleted": 0, "stale_freed": 0}
    size = None
    try:
        size = int(file_size) if file_size is not None else None
    except (TypeError, ValueError):
        size = None
    if size is None or size <= 0:
        result["reason"] = "unknown_size"
        return result
    result["file_size"] = size
    client, marker = _user_drive_client(db, user_id,
                                        drive_client_factory)
    if client is None:
        result["reason"] = "no_client"
        return result
    about = getattr(client, "about_storage", None)
    if about is None:
        logger.warning(
            "[transfer-cleanup] 用户盘配额未测到（客户端无 "
            "about_storage）：user=%s，不腾地方照旧 copy", user_id)
        result["reason"] = "quota_unknown"
        return result
    try:
        quota = about(marker)
    except Exception as exc:  # noqa: BLE001 — about 抖动不判死转存
        logger.warning(
            "[transfer-cleanup] 用户盘配额查询失败：user=%s err=%s，"
            "不腾地方照旧 copy", user_id, exc)
        result["reason"] = "quota_unknown"
        return result
    free = (quota or {}).get("free")
    if free is None:
        # 上限不限量/测不到上限：按空间充足处理，不据此腾地方。
        return result
    free = int(free)
    result["free_bytes"] = free
    if free >= size:
        return result
    need = size - free
    try:
        excluded = active_playing_variant_ids(db, user_id, now=now)
    except Exception as exc:  # noqa: BLE001 — 排除集算不出就不腾
        logger.warning(
            "[transfer-cleanup] 正在播排除集计算失败：user=%s "
            "err=%s，不腾地方照旧 copy", user_id, exc)
        result["reason"] = "active_check_failed"
        return result
    if exclude_item_ids:
        rows = (db.query(em.TransferVariant)
                .filter(em.TransferVariant.owner_scope == "user",
                        em.TransferVariant.owner_ref == str(user_id),
                        em.TransferVariant.status == "ready",
                        em.TransferVariant.item_id.in_(
                            list(exclude_item_ids)))
                .all())
        excluded.update(row.id for row in rows)
    # 追剧保护（软排除）：算不出来就按无保护处理，退回原行为。
    try:
        protected = protected_variant_ids(
            db, user_id, now, session_progress=session_progress)
    except Exception as exc:  # noqa: BLE001 — 保护集失败不阻断转存
        logger.warning(
            "[transfer-cleanup] 追剧保护集计算失败：user=%s err=%s，"
            "按无保护处理（原 LRU 行为）", user_id, exc)
        protected = set()
    soft = set(protected) - excluded
    result["protected"] = sorted(soft)
    result["evicted_protected"] = []
    # A-090：先清本用户 stale 死副本（正在播硬排除），字节先计入
    # 估算；stale 够了就不再动 ready 行。stale pass 本身失败不阻断。
    try:
        stale_stats = cleanup_stale_variants(
            db, drive_client_factory, user_id=user_id,
            exclude_variant_ids=sorted(excluded))
    except Exception as exc:  # noqa: BLE001 — stale 清理失败不阻断腾地方
        logger.warning(
            "[transfer-cleanup] stale 前置清理失败，改走 ready 腾地方 "
            "user=%s err=%s", user_id, exc)
        stale_stats = {"deleted": 0, "bytes_freed": 0}
    result["stale_deleted"] = stale_stats["deleted"]
    result["stale_freed"] = stale_stats["bytes_freed"]
    remaining_need = max(0, need - result["stale_freed"])
    evicted_result = evict_lru_variants(
        db, drive_client_factory, user_id, need_bytes=remaining_need,
        session_progress=session_progress,
        exclude_variant_ids=sorted(excluded | soft))
    result["evicted"] = list(evicted_result["evicted"])
    result["bytes_freed"] = evicted_result["bytes_freed"]
    estimate = free + result["stale_freed"] + result["bytes_freed"]
    if estimate < size and soft:
        # 第二轮：非受保护行第一轮已按 LRU 全过一遍（删掉的已非
        # ready，删失败的不重试），这里只在受保护行里按原 LRU 顺序
        # 继续腾；正在播 + exclude_item_ids 仍是硬排除。
        ready_ids = {
            rid for (rid,) in (
                db.query(em.TransferVariant.id)
                .filter(em.TransferVariant.owner_scope == "user",
                        em.TransferVariant.owner_ref == str(user_id),
                        em.TransferVariant.status == "ready")
                .all())}
        round2 = evict_lru_variants(
            db, drive_client_factory, user_id, need_bytes=size - estimate,
            session_progress=session_progress,
            exclude_variant_ids=sorted(excluded | (ready_ids - soft)))
        forced = [vid for vid in round2["evicted"] if vid in soft]
        for vid in forced:
            logger.warning(
                "[transfer-cleanup] 空间不足，被迫腾掉受保护副本 "
                "user=%s variant=%s（近期播过或在追的剧）",
                user_id, vid)
        result["evicted"].extend(round2["evicted"])
        result["evicted_protected"] = forced
        result["bytes_freed"] += round2["bytes_freed"]
        estimate = free + result["bytes_freed"]
    if estimate >= size:
        result["reason"] = "evicted_enough"
        return result
    result["reason"] = "insufficient"
    result["proceed"] = False
    result["shortfall"] = size - estimate
    return result
