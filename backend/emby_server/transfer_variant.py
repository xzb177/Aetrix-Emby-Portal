# -*- coding: utf-8 -*-
"""转存变体机制（FMBY original_transfer 机制化，D1 播放源单独立列方案）。

一句话：转存变体不是第三种播放模式，是「换一个 file_id 再走现有
播放链」——把「别人盘里的文件」（全网共用公开池）变成「自己空间里
的独立副本」（自有额度池），播放仍走现行选路（中继优先、302 兜底）。

本模块覆盖设计稿第一步 + 第二步（热门预防后置，见 D2）：

- 副本登记表 + 手动触发（管理端单条目「转存」按钮）+ 播放选路读
  副本 fid + 扫描锚定保护 + 总闸（默认关）。
- 应急自动触发（整段 Quota 网页信号识别 + 入队 + 24h 节流 + 负缓存
  语义由任务表承载）+ 副本健康巡检 + 孤儿标记（v1 不自动删文件）。

D1 已定（播放源单独立列）：条目 ``drive_file_id`` 永远指原件，
播放源指针只活在 ``emby_transfer_variants`` 表——重扫改写
``drive_file_id`` 也动不了播放源；回滚四级：

1. 开关级：总闸关 → 播放链忽略全部副本，全站回原件链，零数据改动。
2. 条目级：单条目解除绑定 → ready 行变 released，回原件；行留作审计。
3. 数据级：原件 fid 一直留档（登记表 source_file_id + 条目本身），
   删副本文件不影响原件播放（删前确认无条目指向）。
4. 最坏情况：登记表全删 → 全站回原件行为（选路默认读 drive_file_id）。

未定项按设计稿开关默认走，不返工：D2 热门预防后置；D3 保留策略
字段已留（last_played_at/last_checked_at），删除逻辑后置；D4 BYOS
用户盘拷贝未做（见下）；D5 应急自动默认关；D6 转存目录名默认
``aetrix-transfer``，目录级排除 + 扫描器首段跳过双保险；D7 工人
放 worker 进程、单线程顺序、每轮最多 10 单；D8 巡检每天一次、
两次 stale 算死；D9 只论 Drive；D10 账单面后置。

BYOS 口径（设计稿 §八底线，照搬）：用户副本只许播给 owner_ref
本人；服主副本不许给有生效中 drive 绑定的用户播（严格于设计稿
§八「否则服主副本」一句——两句冲突时取严格的一句，见 PR 描述）。
用户盘拷贝创建路径是 D4 后续项：本版自动触发遇到 BYOS 用户只记
日志不建服主副本；手动触发只建服主副本。

失败总原则（设计稿 §六）：转存是加分项，不是播放的前置条件。
选路与信号处理永不抛错进播放链；工人侧 copy 403/404 不重试、
429/5xx 最多 3 次指数退避、权限失败记 orphan 不删文件。

未决实测（设计稿 §十二，施工前须知）：Quota 网页信号的精确特征
未拿真实额度爆文件抓包复核，本版只认强信号（整段 200 +
text/html + 体内 Quota 标记）；跨盘 copy 权限语义未实测，本版只做
同盘拷贝（源与副本同盘）；目标盘剩余空间未实查（热门预防立项前
必须实查，本版应急量级小、空间可忽略但仍记账 size_bytes）。
"""

import os
import threading
import time
from datetime import datetime, timedelta

import httpx

from backend.emby_server import models as em
from backend.emby_server.drive_changes import get_token_for_drive
from backend.emby_server.mount_drive import FILES_URL

logger = __import__("logging").getLogger(__name__)

# ---------------------------------------------------------------------------
# 开关（env，默认全关；新增键必须同步写进 env.example，见 r44 契约测试）
# ---------------------------------------------------------------------------

THROTTLE_SEC = 86400  # 24h：同一条目 24h 内尝试过转存（无论成败）不再自动入队
MAX_ATTEMPTS = 3  # copy 可重试类失败的最多尝试次数（含首次）
RETRY_BASE_SEC = 60  # 退避基数：60s、120s、240s
WORKER_INTERVAL_SEC = 60
PATROL_INTERVAL_SEC = 86400  # 巡检每天一次
STALE_STRIKES = 2  # 巡检两次 stale 算死
MAX_JOBS_PER_ROUND = 10
STUCK_RUNNING_SEC = 3600  # running 卡住超 1h 回 pending


def _env_on(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() not in {
        "0", "false", "no", "off", ""}


def master_enabled() -> bool:
    """转存变体总闸，默认关。关时选路、触发、工人全部空转。"""
    return _env_on("TRANSFER_VARIANT_ENABLED", "0")


def auto_enabled() -> bool:
    """应急自动触发开关，默认关（D5：手动验证过后再开）。"""
    return _env_on("TRANSFER_VARIANT_AUTO_ENABLED", "0")


def transfer_dir_name() -> str:
    """转存目录名（目标盘根目录下，默认 aetrix-transfer）。"""
    name = (os.getenv("TRANSFER_VARIANT_DIR", "aetrix-transfer")
            or "aetrix-transfer").strip().strip("/")
    return name or "aetrix-transfer"


# ---------------------------------------------------------------------------
# Quota 网页信号（强信号 only：整段 200 + text/html + 体内 Quota 标记）
# ---------------------------------------------------------------------------

_QUOTA_MARKERS = (b"quota exceeded", b"quotaexceeded",
                  b"downloadquotaexceeded")


class QuotaPageError(Exception):
    """上游整段响应是 Quota 额度页（不是视频字节）。

    由中继嗅探抛给 video_stream：调用方记信号 + 入队转存，本次播放
    回落现行链（302 开着走 302，否则干净 502）。不是 HTTPException，
    其它中继调用方（目前只有 video_stream）不会误把它当普通上游失败。
    """


def is_quota_page_bytes(status, content_type, body) -> bool:
    """Quota 网页判定：三个条件全中才算强信号，模糊信号一律不认。

    - ``status`` 必须 200（403 只走现行 _drive_403_error_reason 口径）；
    - ``content_type`` 必须 text/html 开头（视频字节绝不可能是 html）；
    - 体内含 Quota 标记（大小写不敏感）。
    """
    if status != 200:
        return False
    ctype = (content_type or "").lower()
    if not ctype.startswith("text/html"):
        return False
    if isinstance(body, str):
        body = body.encode("utf-8", "ignore")
    text = (body or b"").lower()
    return any(m in text for m in _QUOTA_MARKERS)


def is_transfer_dir_rel(rel: str) -> bool:
    """扫描排除判定：rel 首段 == 转存目录名即转存目录下的文件。

    rel 是挂载根相对路径（如 ``/MoviePilot/...`` 或
    ``aetrix-transfer/x.mp4``）；转存目录在目标盘根目录，首段命中即
    跳过——即使某库的扫描源误指到转存目录也不会入库（防御纵深；
    正常布局下转存目录本就不在任何库的扫描源内）。
    """
    name = transfer_dir_name()
    if not name:
        return False
    parts = [p for p in (rel or "").split("/") if p]
    return bool(parts) and parts[0] == name


class TransferError(Exception):
    """转存触发校验失败（端点映射成 HTTP 码）。kind 见各 raise 处。"""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


class _DriveError(Exception):
    pass


class _NotFound(_DriveError):
    pass


class _Forbidden(_DriveError):
    pass


class _Retriable(_DriveError):
    pass


class _CopyForbidden(_DriveError):
    """copy 403/404：原件已死或无权，不重试（设计稿 §六）。"""


# ---------------------------------------------------------------------------
# 选路与触发（纯读/纯触发，不碰播放链的写路径）
# ---------------------------------------------------------------------------

def _newest_ready(db, item_id, owner_scope, owner_ref=None):
    """该条目该 scope 下最新的一行 ready 登记（按 id 倒序取 1）。"""
    q = (db.query(em.TransferVariant)
         .filter(em.TransferVariant.item_id == item_id,
                 em.TransferVariant.owner_scope == owner_scope,
                 em.TransferVariant.status == "ready"))
    if owner_ref is not None:
        q = q.filter(em.TransferVariant.owner_ref == owner_ref)
    return q.order_by(em.TransferVariant.id.desc()).first()


def has_active_drive_binding(db, user_id) -> bool:
    """用户是否有生效中的 Drive 绑定（只认 provider=drive 且 active）。

    只绑了 115 的用户不算 Drive BYOS——避免误伤 Drive 条目播放。
    """
    if user_id is None:
        return False
    row = (db.query(em.UserStorageBinding)
           .filter(em.UserStorageBinding.user_id == user_id,
                   em.UserStorageBinding.provider == "drive",
                   em.UserStorageBinding.status == "active")
           .first())
    return row is not None


def get_ready_variant_for_user(db, item_id, user_id):
    """该用户在该条目上可播的副本登记行（或 None）。

    顺序（D4 严格底线）：①用户自己的副本（owner_ref == user_id）；
    ②用户没有生效中 drive 绑定时，服主副本；③否则 None。
    user_id 为 None（后台/未登录语境）按服主模式走规则 ②。
    """
    if user_id is not None:
        row = _newest_ready(db, item_id, "user", str(user_id))
        if row is not None:
            return row
    if user_id is None or not has_active_drive_binding(db, user_id):
        row = _newest_ready(db, item_id, "source")
        if row is not None:
            return row
    return None


def resolve_play_file_id(db, item, user):
    """播放源解析：返回 (fid, variant_row_or_None)。

    纯读，绝不写库——不把选路写进播放热路径。任何异常都回原件
    （fail-safe：转存逻辑崩了，播放仍走老链路）。
    解析以 item_id 为键，不以 drive_file_id 为键：重扫改写
    drive_file_id 之后，副本仍可被解析到（锚定保护的核心）。
    """
    orig = (getattr(item, "drive_file_id", "") or "").strip()
    if not master_enabled() or not orig:
        return orig, None
    item_id = getattr(item, "id", None)
    if item_id is None:
        return orig, None
    try:
        row = get_ready_variant_for_user(db, item_id, getattr(user, "id", None))
    except Exception:
        logger.warning("转存变体选路查询异常，回原件 item=%s",
                       item_id, exc_info=True)
        return orig, None
    if row is None:
        return orig, None
    return row.variant_file_id, row


def _recent_job_exists(db, item_id, window_sec=THROTTLE_SEC) -> bool:
    cutoff = datetime.now() - timedelta(seconds=window_sec)
    row = (db.query(em.TransferJob)
           .filter(em.TransferJob.item_id == item_id,
                   em.TransferJob.created_at >= cutoff)
           .first())
    return row is not None


def note_quota_signal(db, item, user, playing_variant, drive_id):
    """Quota 网页信号处理：绝不抛异常（播放绝不能被转存逻辑拖垮）。

    返回 dict：action in ignored/skipped/throttled/queued/error。
    - playing_variant 非空 → 正在播的副本爆额度是确定性证据，立即
      标 stale（不走巡检的两次规则），下次播放不再选它；
    - BYOS 用户 → 只记日志，不建服主副本（用户盘拷贝是 D4 后续项）；
    - 已有健康副本 → 跳过（不要对同一条目堆副本）；
    - 24h 内已有任何任务 → 节流（throttled，不建新任务）；
    - 自动开关关 → 只记日志，返回 skipped/auto_off（供人工手动触发）。
    """
    try:
        item_id = getattr(item, "id", None)
        if not master_enabled():
            logger.info("转存变体 Quota 信号：总闸关，忽略 item=%s", item_id)
            return {"action": "ignored", "reason": "master_off"}
        orig_fid = (getattr(item, "drive_file_id", "") or "").strip()
        if not orig_fid:
            return {"action": "ignored", "reason": "no_drive_file"}
        if item_id is None:
            return {"action": "ignored", "reason": "no_item_id"}
        if playing_variant is not None:
            playing_variant.status = "stale"
            playing_variant.last_checked_at = datetime.now()
            db.commit()
            logger.warning(
                "转存变体副本播放中爆额度，已标 stale：item=%s variant=%s",
                item_id, playing_variant.id)
        user_id = getattr(user, "id", None)
        if user_id is not None and has_active_drive_binding(db, user_id):
            logger.info(
                "转存变体 Quota 信号：BYOS 用户不自动建服主副本 item=%s user=%s",
                item_id, user_id)
            return {"action": "skipped", "reason": "byos_user"}
        ready = get_ready_variant_for_user(db, item_id, user_id)
        if ready is not None:
            return {"action": "skipped", "reason": "has_ready_variant"}
        if _recent_job_exists(db, item_id):
            return {"action": "throttled", "reason": "job_within_24h"}
        if not auto_enabled():
            logger.info(
                "转存变体 Quota 信号：自动入队默认关，仅记日志 item=%s",
                item_id)
            return {"action": "skipped", "reason": "auto_off"}
        job = em.TransferJob(item_id=item_id, trigger="auto",
                             drive_id=drive_id, status="pending")
        db.add(job)
        db.commit()
        logger.warning("转存变体应急自动入队：item=%s job=%s", item_id, job.id)
        return {"action": "queued", "job_id": job.id}
    except Exception:
        logger.warning("转存变体 Quota 信号处理异常（已吞掉，不影响播放）",
                       exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return {"action": "error"}


def create_manual_job(db, item, drive_id, force=False):
    """手动触发建队：校验失败抛 TransferError（端点映射 HTTP 码）。

    - 总闸关 → master_off；无 drive_file_id → no_drive_file；
    - 已有健康服主副本 → has_ready_variant（不重复转存）；
    - 24h 内有任务且 force=False → throttled；force=True 为管理员
      显式越过节流（须经 _audit 记日志，端点负责）。
    """
    if not master_enabled():
        raise TransferError("master_off", "转存变体总闸未开")
    orig_fid = (getattr(item, "drive_file_id", "") or "").strip()
    if not orig_fid:
        raise TransferError("no_drive_file", "条目没有 drive_file_id")
    item_id = getattr(item, "id", None)
    if item_id is None:
        raise TransferError("no_item_id", "条目无 id")
    existing = _newest_ready(db, item_id, "source")
    if existing is not None:
        raise TransferError("has_ready_variant", "已有健康服主副本，不重复转存")
    if not force and _recent_job_exists(db, item_id):
        raise TransferError("throttled", "24h 内已尝试过转存")
    job = em.TransferJob(item_id=item_id, trigger="manual",
                         drive_id=drive_id, status="pending")
    db.add(job)
    db.commit()
    return job


def release_binding(db, item_id) -> int:
    """条目级解除绑定（回滚第 2 级）：该条目全部 ready 行变 released。

    返回释放的行数。行留作审计、文件不动（不自动删副本文件）。
    """
    rows = (db.query(em.TransferVariant)
            .filter(em.TransferVariant.item_id == item_id,
                    em.TransferVariant.status == "ready")
            .all())
    for row in rows:
        row.status = "released"
    if rows:
        db.commit()
    return len(rows)


def _release_ready_rows(db, item_id, owner_scope) -> int:
    """把该条目该 scope 的 ready 行置 released（成功建新副本前调用，
    保证一物一生效源；调用方负责 commit）。"""
    rows = (db.query(em.TransferVariant)
            .filter(em.TransferVariant.item_id == item_id,
                    em.TransferVariant.owner_scope == owner_scope,
                    em.TransferVariant.status == "ready")
            .all())
    for row in rows:
        row.status = "released"
    return len(rows)


# ---------------------------------------------------------------------------
# Drive API 客户端（生产路径；copy 权限设置等未决实测见模块头）
# ---------------------------------------------------------------------------

_FOLDER_MIME = "application/vnd.google-apps.folder"


def _to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class DriveApiClient:
    """转存工人用的 Drive API 客户端。

    凭据一律走 drive_changes.get_token_for_drive（全项目唯一入口）：
    ``myDrive:`` 开头走该 remote 自身 OAuth，否则走共享盘 SA 轮换。
    401 时强制刷新 token 重试一次。v1 只做同盘拷贝——源与副本同盘
    （drive_id 既是源盘也是落盘），不碰跨盘权限语义（设计稿 §十二
    未决实测，留 D4 后置）。
    """

    def _headers(self, drive_id, force_refresh=False):
        token = get_token_for_drive(drive_id, force_refresh=force_refresh)
        if not token:
            raise _Retriable("Drive 凭据不可用")
        return {"Authorization": f"Bearer {token}"}

    def _request(self, method, url, drive_id, **kwargs):
        """一次请求 + 401 刷新重试一次；网络异常转 _Retriable。"""
        kwargs.setdefault("timeout", 60)
        try:
            resp = httpx.request(method, url,
                                 headers=self._headers(drive_id), **kwargs)
        except (httpx.TimeoutException, httpx.ConnectError,
                httpx.RemoteProtocolError, httpx.ReadError) as exc:
            raise _Retriable(f"Drive 网络异常：{type(exc).__name__}") from exc
        if resp.status_code == 401:
            try:
                resp = httpx.request(
                    method, url,
                    headers=self._headers(drive_id, force_refresh=True),
                    **kwargs)
            except (httpx.TimeoutException, httpx.ConnectError,
                    httpx.RemoteProtocolError, httpx.ReadError) as exc:
                raise _Retriable(
                    f"Drive 网络异常（刷新后）：{type(exc).__name__}") from exc
        return resp

    @staticmethod
    def _is_personal(drive_id):
        return (drive_id or "").startswith("myDrive:")

    @classmethod
    def _root_parent(cls, drive_id):
        # 个人盘根用 "root"；共享盘的根文件夹 id 就是 drive id 本身。
        return "root" if cls._is_personal(drive_id) else drive_id

    @classmethod
    def _list_params(cls, drive_id):
        if cls._is_personal(drive_id):
            return {"spaces": "drive"}
        return {"driveId": drive_id, "corpora": "drive"}

    def ensure_folder(self, drive_id, dir_name):
        """转存目录在就返回 id，没有就建（同盘根目录下）。"""
        root = self._root_parent(drive_id)
        escaped = dir_name.replace("'", "\\'")
        q = (f"name='{escaped}' and '{root}' in parents "
             f"and mimeType='{_FOLDER_MIME}' and trashed=false")
        params = {"q": q, "supportsAllDrives": "true",
                  "fields": "files(id,name)"}
        params.update(self._list_params(drive_id))
        resp = self._request("GET", FILES_URL, drive_id, params=params)
        if resp.status_code == 200:
            files = (resp.json() or {}).get("files") or []
            if files:
                return str(files[0]["id"])
        else:
            raise _Retriable(f"列转存目录失败：{resp.status_code}")
        body = {"name": dir_name, "mimeType": _FOLDER_MIME, "parents": [root]}
        resp = self._request("POST", FILES_URL, drive_id, json=body,
                             params={"supportsAllDrives": "true"})
        if resp.status_code in (200, 201):
            fid = (resp.json() or {}).get("id")
            if fid:
                return str(fid)
        raise _Retriable(f"建转存目录失败：{resp.status_code}")

    def copy_file(self, drive_id, source_fid, folder_id, name):
        """同盘 files.copy；403/404 转 _CopyForbidden（死文件，不重试）。"""
        url = f"{FILES_URL}/{source_fid}/copy"
        body = {"name": name, "parents": [folder_id]}
        resp = self._request("POST", url, drive_id, json=body,
                             params={"supportsAllDrives": "true"},
                             timeout=120)
        if resp.status_code in (200, 201):
            data = resp.json() or {}
            return {"id": str(data.get("id") or ""),
                    "size": _to_int(data.get("size"))}
        if resp.status_code in (403, 404):
            raise _CopyForbidden(f"copy 被拒：{resp.status_code}")
        raise _Retriable(f"copy 失败：{resp.status_code}")

    def add_anyone_reader(self, drive_id, fid):
        """给副本补 anyone reader（中继与 302 两条链都可用，见 PR 描述
        与设计稿 §4.1 的口径说明）。非 2xx 抛异常，调用方记 orphan。"""
        url = f"{FILES_URL}/{fid}/permissions"
        body = {"role": "reader", "type": "anyone"}
        resp = self._request("POST", url, drive_id, json=body,
                             params={"supportsAllDrives": "true"})
        if resp.status_code not in (200, 201):
            raise Exception(f"补 anyone 权限失败：{resp.status_code}")

    def get_file(self, drive_id, fid):
        """巡检用 files.get：404/已进回收站 → _NotFound；403 → _Forbidden；
        429/5xx/网络 → _Retriable；其它状态 → _Retriable（有界重试）。"""
        url = f"{FILES_URL}/{fid}"
        params = {"supportsAllDrives": "true", "fields": "id,trashed,size"}
        resp = self._request("GET", url, drive_id, params=params)
        if resp.status_code == 200:
            data = resp.json() or {}
            if data.get("trashed"):
                raise _NotFound("副本已进回收站")
            return data
        if resp.status_code == 404:
            raise _NotFound("副本不存在")
        if resp.status_code == 403:
            raise _Forbidden("副本无权访问")
        raise _Retriable(f"files.get 失败：{resp.status_code}")


# ---------------------------------------------------------------------------
# 工人（单线程顺序执行，调用方是 worker 守护；内部提交）
# ---------------------------------------------------------------------------

def _copy_name_for_item(item):
    """副本文件名：取条目 file_path 的最后一段，空则用条目名，再空
    用 item-<id>。mount://<id>/<rel> 与本机路径都按最后一段取。"""
    path = (getattr(item, "file_path", "") or "").rstrip("/")
    base = path.rsplit("/", 1)[-1] if path else ""
    if not base:
        base = getattr(item, "name", "") or f"item-{getattr(item, 'id', '')}"
    return base


def _schedule_retry(db, job, exc):
    """有界重试：3 次内 pending + 指数退避（60/120/240 秒），满 3 次
    failed/retriable。返回 outcome 字符串（供统计）。"""
    if job.attempts >= MAX_ATTEMPTS:
        job.status = "failed"
        job.error_kind = "retriable"
        job.error_detail = str(exc)[:500]
        job.next_retry_at = None
        db.commit()
        return "failed"
    delay = RETRY_BASE_SEC * (2 ** (job.attempts - 1))
    job.status = "pending"
    job.error_kind = "retriable"
    job.error_detail = str(exc)[:500]
    job.next_retry_at = datetime.now() + timedelta(seconds=delay)
    db.commit()
    return "retried"


def run_transfer_job_once(db, job, drive_api):
    """执行一个转存任务（工人单线程顺序调用；内部提交）。

    返回 {"job_id", "outcome", ...}，outcome in done/failed/retried。
    失败总原则：任何一步失败都不影响现行播放链（任务表只记结果）。
    """
    item = None
    if job.item_id is not None:
        item = (db.query(em.MediaItem)
                .filter(em.MediaItem.id == job.item_id,
                        em.MediaItem.deleted_at.is_(None))
                .first())
    if item is None or not (getattr(item, "drive_file_id", "") or "").strip():
        job.status = "failed"
        job.error_kind = "item_gone"
        job.error_detail = "条目不存在或无 drive_file_id"
        db.commit()
        return {"job_id": job.id, "outcome": "failed", "error_kind": "item_gone"}
    if not job.drive_id:
        job.status = "failed"
        job.error_kind = "no_drive"
        job.error_detail = "任务无 drive_id"
        db.commit()
        return {"job_id": job.id, "outcome": "failed", "error_kind": "no_drive"}
    job.status = "running"
    job.attempts += 1
    db.commit()
    orig_fid = item.drive_file_id.strip()
    try:
        folder_id = drive_api.ensure_folder(job.drive_id, transfer_dir_name())
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    try:
        copied = drive_api.copy_file(job.drive_id, orig_fid, folder_id,
                                     _copy_name_for_item(item))
    except _CopyForbidden as exc:
        # 原件已死/无权：不重试，记死文件口径（不删条目，不动原件）。
        job.status = "failed"
        job.error_kind = "dead"
        job.error_detail = str(exc)[:500]
        db.commit()
        return {"job_id": job.id, "outcome": "failed", "error_kind": "dead"}
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    variant_fid = (copied or {}).get("id") or ""
    if not variant_fid:
        return {"job_id": job.id,
                "outcome": _schedule_retry(
                    db, job, Exception("copy 未返回新 file_id"))}
    try:
        drive_api.add_anyone_reader(job.drive_id, variant_fid)
    except Exception as exc:
        # 副本已建但不可播：记 orphan，不删文件，等管理员处置。
        # 注意：不释放旧 ready 行（旧副本若在，继续生效；孤儿是额外行）。
        row = em.TransferVariant(
            item_id=job.item_id, source_file_id=orig_fid,
            variant_file_id=variant_fid, variant_drive_id=job.drive_id,
            owner_scope="source", owner_ref=None, status="orphan",
            size_bytes=_to_int((copied or {}).get("size"))
            or _to_int(getattr(item, "size", None)))
        db.add(row)
        job.status = "failed"
        job.error_kind = "orphan"
        job.error_detail = str(exc)[:500]
        db.commit()
        return {"job_id": job.id, "outcome": "failed", "error_kind": "orphan"}
    # 成功：同 scope 旧 ready 行先 released，保证一物一生效源。
    _release_ready_rows(db, job.item_id, "source")
    row = em.TransferVariant(
        item_id=job.item_id, source_file_id=orig_fid,
        variant_file_id=variant_fid, variant_drive_id=job.drive_id,
        owner_scope="source", owner_ref=None, status="ready",
        size_bytes=_to_int((copied or {}).get("size"))
        or _to_int(getattr(item, "size", None)))
    db.add(row)
    job.status = "done"
    job.error_kind = None
    job.error_detail = None
    job.next_retry_at = None
    db.commit()
    return {"job_id": job.id, "outcome": "done", "variant_id": row.id}


def process_due_jobs_once(db, drive_api_factory, now=None):
    """一轮到期任务处理：先把 running 卡住超 1h 的打回 pending（不改
    attempts），再按 id 顺序、每轮最多 10 单逐个跑（单线程即限并发 1）。"""
    now = now or datetime.now()
    stats = {"processed": 0, "done": 0, "failed": 0, "retried": 0,
             "stuck_reset": 0}
    stuck = (db.query(em.TransferJob)
             .filter(em.TransferJob.status == "running",
                     em.TransferJob.updated_at
                     < now - timedelta(seconds=STUCK_RUNNING_SEC))
             .all())
    for job in stuck:
        job.status = "pending"
        job.next_retry_at = None
        stats["stuck_reset"] += 1
    if stuck:
        db.commit()
    due = (db.query(em.TransferJob)
           .filter(em.TransferJob.status == "pending",
                   (em.TransferJob.next_retry_at.is_(None))
                   | (em.TransferJob.next_retry_at <= now))
           .order_by(em.TransferJob.id)
           .limit(MAX_JOBS_PER_ROUND)
           .all())
    for job in due:
        try:
            result = run_transfer_job_once(db, job, drive_api_factory())
        except Exception:
            logger.warning("转存任务执行异常 job=%s", job.id, exc_info=True)
            try:
                db.rollback()
            except Exception:
                pass
            stats["failed"] += 1
            continue
        stats["processed"] += 1
        outcome = result.get("outcome")
        if outcome == "done":
            stats["done"] += 1
        elif outcome == "failed":
            stats["failed"] += 1
        else:
            stats["retried"] += 1
    return stats


def patrol_once(db, drive_api_factory, now=None):
    """副本健康巡检：只读 + 标记，不动原件与条目身份。

    - 条目不存在/已软删 → 登记行置 orphan（文件留存，等管理员复核）；
    - get_file 成功 → stale_count 清零；
    - 404/403 → strike+1，满 2 次置 stale（解除锚定，下次播放回原件；
      重拷只由下次播放 Quota 事件在节流允许时触发，巡检不自动重拷）；
    - 其它异常（429/5xx/网络）→ 只记日志，不记 strike。
    孤儿文件的自动删除不在 v1（删用户数据自动执行风险高，留复核）。
    """
    now = now or datetime.now()
    stats = {"checked": 0, "stale": 0, "orphaned_items": 0, "errors": 0}
    client = drive_api_factory()
    rows = db.query(em.TransferVariant).filter(
        em.TransferVariant.status == "ready").all()
    for row in rows:
        stats["checked"] += 1
        item = (db.query(em.MediaItem)
                .filter(em.MediaItem.id == row.item_id,
                        em.MediaItem.deleted_at.is_(None))
                .first())
        if item is None:
            row.status = "orphan"
            stats["orphaned_items"] += 1
            continue
        try:
            client.get_file(row.variant_drive_id or "", row.variant_file_id)
        except (_NotFound, _Forbidden):
            row.stale_count += 1
            row.last_checked_at = now
            if row.stale_count >= STALE_STRIKES:
                row.status = "stale"
                stats["stale"] += 1
        except Exception:
            stats["errors"] += 1
            logger.warning("转存巡检 get_file 异常 variant=%s（不记 strike）",
                           row.id, exc_info=True)
            continue
        else:
            row.stale_count = 0
            row.last_checked_at = now
    db.commit()
    return stats


def start_transfer_daemon():
    """启动转存工人守护线程（必须显式调用；import 时绝不自动启动）。

    总闸关时空转等待（不发请求、不读写任务）；开时每 60 秒一轮：
    到期任务 + 每天一次巡检（进程内记忆上次巡检时间）。任何异常只记
    warning、不炸线程。返回线程对象，调用方持有以便观测。
    """
    stop = threading.Event()
    state = {"last_patrol": 0.0}

    def _loop():
        while not stop.is_set():
            try:
                if master_enabled():
                    from backend.database import SessionLocal
                    db = SessionLocal()
                    try:
                        process_due_jobs_once(db, DriveApiClient)
                        now_ts = time.time()
                        if now_ts - state["last_patrol"] >= PATROL_INTERVAL_SEC:
                            patrol_once(db, DriveApiClient)
                            state["last_patrol"] = now_ts
                    finally:
                        db.close()
            except Exception:
                logger.warning("转存变体工人轮次异常，已跳过本轮", exc_info=True)
            stop.wait(WORKER_INTERVAL_SEC)

    thread = threading.Thread(target=_loop, name="transfer-variant",
                              daemon=True)
    thread.start()
    return thread


__all__ = [
    "THROTTLE_SEC", "MAX_ATTEMPTS", "RETRY_BASE_SEC", "WORKER_INTERVAL_SEC",
    "PATROL_INTERVAL_SEC", "STALE_STRIKES", "MAX_JOBS_PER_ROUND",
    "STUCK_RUNNING_SEC",
    "master_enabled", "auto_enabled", "transfer_dir_name",
    "QuotaPageError", "is_quota_page_bytes", "is_transfer_dir_rel",
    "TransferError",
    "has_active_drive_binding", "get_ready_variant_for_user",
    "resolve_play_file_id", "note_quota_signal", "create_manual_job",
    "release_binding",
    "DriveApiClient",
    "run_transfer_job_once", "process_due_jobs_once", "patrol_once",
    "start_transfer_daemon",
]
