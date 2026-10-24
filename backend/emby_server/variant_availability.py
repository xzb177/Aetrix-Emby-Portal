"""变体级可用性落库 + 真探活（FMBY 播放全换阶段 4，清单第 4 项）。

对应魂 2（读写分离）与魂 6（状态集中维护）：某条播放变体还活不活，
做成 PG 表里的状态，而不是进程里的 dict 或某次起播的偶然成败。
FMBY 的同款字段在 ``upstream_playback_variants``（``availability_status``、
``last_checked_at``）；我方变体在 ``play_variants.py`` 里是静态
dataclass、没有 DB 行可加列，所以新表是唯一落点（不在既有变体模型
上加列——那里根本没有行）。

## 状态机（与 25-3 源级口径同款，见 upstream_health.py）

- 无记录当可用：调度过滤口 ``is_variant_available`` 在查不到行时返回
  True，绝不因“没探过”就把变体摘掉。
- Unknown 是探活结论、不是状态：Unknown 只推进 ``last_checked_at``，
  状态、失败计数、失败原因一律不动；无行时 Unknown 不建行（没有信息
  就不要凭空造一行）。
- 成功复位：一次成功就把状态复位为 available、失败计数清零、
  ``unavailable_since`` 清空、``last_success_at`` 更新。
- 失败计数与翻转：失败累计 ``consecutive_failures``；达到阈值才翻为
  unavailable 并记 ``unavailable_since``（首次翻转时间），之后继续
  失败只累计计数、不刷新翻转时间。

## 探活（真探活，不是 ping）

探活 = 对该变体的真实取流路径做一次最小验证：解析出目标（变体
resolve）+ 小 Range 试读（``bytes=0-0``，只证可达、不搬字节）。
失败分两类，不静默回落（探活只上报结论，绝不替客户端换变体）：

- ``credential_error``：401 / 403、取不到 Drive 凭据——凭据层的问题；
- ``remote_unavailable``：其它 4xx / 5xx / 超时 / 传输错——远端的问题；
- ``unknown``：目标本身没了（条目删了、挂载删了/停用了、Range 回
  404）——这是“探无可探”，不记变体的账。

## 本阶段的能力边界（诚实标注）

- 只落库 + 只探活，不接调度：本阶段不改 ``list_variants``、不改
  video_stream 的选路。可用性表现在只是“看得见的状态”，调度侧按
  它过滤是阶段 6 / 阶段 7 的事；本阶段合并不改变任何一次真实播放。
- 工人默认关：``VARIANT_AVAILABILITY_PROBE_ENABLED`` 默认 0；关时
  工人不发任何请求、不写任何状态，全站行为逐字不变。
- 探活目标来自真实播放：工人每轮从最近成功起播事件里按（变体，
  挂载）取最新条目当探针——只探用户真在用的路径，不凭空编目标；
  从未成功播过的变体没有探针、不建行、无记录当可用。
- Drive 探活只读 1 字节：Range ``bytes=0-0``；周期默认 300 秒、每轮
  每变体每挂载最多一个探针，配额成本可控。探活单次尝试、不做凭据
  轮换（403 换 SA 是阶段 6 的事，本阶段 401/403 只记 credential_error）。
- ``local_file`` 不探：本机文件没有远端路径可探，磁盘健康不是变体
  可用性；该变体永远无行、无记录当可用。

## 数值来源（不冒充 FMBY 实测值）

周期 300 秒、阈值 3 次、超时 10 秒、每轮上限 20 个探针、采样窗口
24 小时，全部是**我方起步值**（阈值 3 与 25-3 缺失宽限 3 轮同口径），
FMBY 探活阈值与间隔在深挖中没有实测到常量；全部走 env，改数不用
改代码。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional

from sqlalchemy.orm import Session

from backend.emby_server.env_util import env_float, env_int

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 状态值与探活结论
# ---------------------------------------------------------------------------

#: 变体可用性状态（FMBY upstream_playback_variants.availability_status 口径）。
STATUS_AVAILABLE = "available"
STATUS_UNAVAILABLE = "unavailable"

#: 探活结论。unknown 是结论、不是状态——它永远不写进状态字段。
OUTCOME_OK = "ok"
OUTCOME_UNKNOWN = "unknown"
OUTCOME_CREDENTIAL_ERROR = "credential_error"
OUTCOME_REMOTE_UNAVAILABLE = "remote_unavailable"

#: 凭据错的 HTTP 状态（与 drive_auth / 中继 403 轮换口径一致）。
_CREDENTIAL_STATUSES = frozenset({401, 403})


# ---------------------------------------------------------------------------
# 配置（全部 env；默认值是我方起步值，非 FMBY 实测常量）
# ---------------------------------------------------------------------------

def probe_enabled() -> bool:
    """变体探活总开关，默认关。关时工人不发请求、不写状态。"""
    return (str(os.getenv("VARIANT_AVAILABILITY_PROBE_ENABLED", "0") or "0")
            .strip().lower() not in {"0", "false", "no", "off"})


def probe_interval_sec() -> float:
    """探活周期（秒）。默认 300：真实解析 + Range 读有配额成本，周期放宽。"""
    return env_float("VARIANT_AVAILABILITY_PROBE_INTERVAL_SEC", 300.0, 10.0, 86400.0)


def failure_threshold() -> int:
    """连续失败几次翻为 unavailable。默认 3，与 25-3 缺失宽限 3 轮同口径。"""
    return env_int("VARIANT_AVAILABILITY_FAILURE_THRESHOLD", 3, 1, 100)


def probe_timeout_sec() -> float:
    """单次 Range 读超时（秒）。默认 10：只读 1 字节，10 秒足够定生死。"""
    return env_float("VARIANT_AVAILABILITY_PROBE_TIMEOUT_SEC", 10.0, 0.5, 120.0)


def max_targets_per_cycle() -> int:
    """每轮最多探针数。默认 20：变体 × 挂载的组合数有上限，防轮次爆炸。"""
    return env_int("VARIANT_AVAILABILITY_MAX_TARGETS_PER_CYCLE", 20, 1, 200)


def probe_window_hours() -> float:
    """探针采样窗口（小时）。默认 24：只从最近 24 小时成功起播里取探针。"""
    return env_float("VARIANT_AVAILABILITY_PROBE_WINDOW_HOURS", 24.0, 1.0, 720.0)


# ---------------------------------------------------------------------------
# 探活结论与探针
# ---------------------------------------------------------------------------

class ProbeError(Exception):
    """探活中可分类的失败。kind ∈ unknown / credential_error / remote_unavailable。"""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(detail or kind)
        self.kind = kind
        self.detail = detail


@dataclass
class ProbeOutcome:
    """一次探活的结论。"""

    kind: str  # ok | unknown | credential_error | remote_unavailable
    http_status: Optional[int] = None
    detail: str = ""


@dataclass
class ProbeTarget:
    """一个探针：某变体在某挂载（或全局）下、某条目的取流路径。"""

    variant: str
    variant_key: str
    item_id: Optional[int] = None
    file_path: str = ""
    drive_file_id: str = ""
    mount_id: Optional[int] = None


def variant_key_for(variant: str, mount_id: Optional[int] = None) -> str:
    """变体可用性行键：挂载中继按挂载分行，其余变体全局一行。

    mount_relay 的健康随挂载走（某挂载的 115 Cookie 挂了不等于别的
    挂载也挂了），所以键里带挂载 id；Drive 两条变体走全局 SA 池与
    全局中继路径，键就是变体名本身。
    """
    if variant == "mount_relay" and mount_id is not None:
        return f"mount_relay:{mount_id}"
    return variant


def classify_http_status(status: Optional[int], content_type: Optional[str] = None) -> str:
    """把一次 Range 试读的 HTTP 状态译成探活结论。

    - 206：字节真回来了，ok；
    - 200：只有非 HTML 才算 ok（2026-10-19 探测塌方教训：usercontent
      入口对私有文件会回 200 的 Google 登录页 HTML，那不是字节）；
    - 401 / 403：凭据错；
    - 404：目标本身没了，unknown（不记变体的账）；
    - 其余：远端不可用。
    """
    if status == 206:
        return OUTCOME_OK
    if status == 200:
        if (content_type or "").split(";")[0].strip().lower() == "text/html":
            return OUTCOME_REMOTE_UNAVAILABLE
        return OUTCOME_OK
    if status in _CREDENTIAL_STATUSES:
        return OUTCOME_CREDENTIAL_ERROR
    if status == 404:
        return OUTCOME_UNKNOWN
    return OUTCOME_REMOTE_UNAVAILABLE


def http_range_probe(url: str, headers: Optional[dict] = None,
                     timeout: float = 10.0) -> tuple[int, Optional[str]]:
    """对 URL 做一次 Range bytes=0-0 试读，只拿状态行与内容类型、不读体。

    返回 ``(http_status, content_type)``；传输层失败（超时、DNS、
    连接被拒）抛 ``ProbeError(remote_unavailable)``，由调用方记账。
    401/403 不抛——HTTPError 的状态码本身就是结论，交给
    :func:`classify_http_status` 定性。
    """
    req = urllib.request.Request(
        url, headers={"Range": "bytes=0-0", **(headers or {})}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("Content-Type")
    except urllib.error.HTTPError as e:
        return e.code, (e.headers.get("Content-Type") if e.headers else None)
    except Exception as e:  # noqa: BLE001 - 超时/DNS/连接错统一算远端不可用
        raise ProbeError(OUTCOME_REMOTE_UNAVAILABLE, f"range probe failed: {e}") from e


def probe_fetch(fetch_fn: Callable[[], tuple[int, Optional[str]]]) -> ProbeOutcome:
    """跑一次取流探针并定性（纯判定，IO 全在注入的 fetch_fn 里）。

    ``fetch_fn`` 返回 ``(http_status, content_type)``，或抛
    ``ProbeError``（resolve 层已分好类：目标没了 → unknown，凭据
    错 → credential_error，其余 → remote_unavailable）。探活只上报
    结论，绝不返回 URL、绝不替客户端换变体（不静默回落）。
    """
    try:
        status, content_type = fetch_fn()
    except ProbeError as e:
        return ProbeOutcome(kind=e.kind, detail=e.detail)
    except Exception as e:  # noqa: BLE001 - 未分类异常不冤判凭据错
        return ProbeOutcome(kind=OUTCOME_REMOTE_UNAVAILABLE, detail=str(e))
    kind = classify_http_status(status, content_type)
    return ProbeOutcome(kind=kind, http_status=status)


# ---------------------------------------------------------------------------
# 状态机（落库）
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now()


def apply_probe_result(db: Session, variant_key: str, outcome: ProbeOutcome,
                       threshold: Optional[int] = None) -> object | None:
    """把一次探活结论写进可用性行（调用方负责提交）。

    - ok：状态复位 available、失败计数清零、unavailable_since 清空、
      last_success_at 与 last_checked_at 更新；
    - unknown：只推进 last_checked_at，状态/计数/失败原因一律不动；
      无行时不建行；
    - credential_error / remote_unavailable：失败计数 +1、记失败原因、
      推进 last_checked_at；计数达阈值才翻 unavailable 并记
      unavailable_since（首次翻转时间，之后不再刷新）。
    返回写后的行（unknown 且无行时返回 None）。
    """
    from backend.emby_server import models as em

    if threshold is None:
        threshold = failure_threshold()
    row = (db.query(em.VariantAvailability)
           .filter(em.VariantAvailability.variant_key == variant_key)
           .one_or_none())
    now = _now()
    if outcome.kind == OUTCOME_UNKNOWN:
        if row is None:
            return None
        row.last_checked_at = now
        return row
    if row is None:
        row = em.VariantAvailability(variant_key=variant_key)
        db.add(row)
    row.last_checked_at = now
    if outcome.kind == OUTCOME_OK:
        row.availability_status = STATUS_AVAILABLE
        row.consecutive_failures = 0
        row.last_error_kind = None
        row.last_success_at = now
        row.unavailable_since = None
        return row
    # 失败两类：计数 +1，达阈值才翻。
    row.consecutive_failures = (row.consecutive_failures or 0) + 1
    row.last_error_kind = outcome.kind
    if (row.consecutive_failures >= threshold
            and row.availability_status != STATUS_UNAVAILABLE):
        row.availability_status = STATUS_UNAVAILABLE
        row.unavailable_since = now
    return row


def is_variant_available(db: Session, variant_key: str) -> bool:
    """调度过滤口（本阶段未接线，留给阶段 6/7）：无记录当可用；
    只有明确 unavailable 才算不可用。"""
    from backend.emby_server import models as em

    row = (db.query(em.VariantAvailability)
           .filter(em.VariantAvailability.variant_key == variant_key)
           .one_or_none())
    if row is None:
        return True
    return row.availability_status == STATUS_AVAILABLE


def get_availability(db: Session, variant_key: str) -> object | None:
    """读一行可用性记录（无行返回 None，调用方按“当可用”处理）。"""
    from backend.emby_server import models as em

    return (db.query(em.VariantAvailability)
            .filter(em.VariantAvailability.variant_key == variant_key)
            .one_or_none())


# ---------------------------------------------------------------------------
# 工人：探针采样与周期检查
# ---------------------------------------------------------------------------

def build_probe_targets(db: Session, *, limit: Optional[int] = None,
                        window_hours: Optional[float] = None) -> list[ProbeTarget]:
    """从最近成功起播事件里采样探针（每变体每挂载取最新一条）。

    只取 result=ok 的事件：探针要验证的是“昨天还播得通的路径今天还
    通不通”，失败事件本身不能当探针（它证明不了路径、只证明某次
    起播挂了）。local_file 不采样（无远端路径可探，见模块 docstring）。
    """
    from backend.emby_server import models as em

    if limit is None:
        limit = max_targets_per_cycle()
    if window_hours is None:
        window_hours = probe_window_hours()
    since = _now() - timedelta(hours=window_hours)
    events = (
        db.query(em.PlayStartEvent)
        .filter(em.PlayStartEvent.result == "ok",
                em.PlayStartEvent.created_at >= since,
                em.PlayStartEvent.variant.isnot(None))
        .order_by(em.PlayStartEvent.created_at.desc())
        .limit(500)
        .all()
    )
    seen: set[tuple[str, Optional[int]]] = set()
    targets: list[ProbeTarget] = []
    for ev in events:
        variant = (ev.variant or "").strip()
        if not variant or variant == "local_file":
            continue
        group = (variant, ev.mount_id if variant == "mount_relay" else None)
        if group in seen:
            continue
        seen.add(group)
        item = None
        if ev.item_id is not None:
            item = (db.query(em.MediaItem)
                    .filter(em.MediaItem.id == ev.item_id)
                    .one_or_none())
        if item is None:
            continue
        file_path = getattr(item, "file_path", "") or ""
        drive_file_id = (getattr(item, "drive_file_id", "") or "").strip()
        if not file_path and not drive_file_id:
            continue
        targets.append(ProbeTarget(
            variant=variant,
            variant_key=variant_key_for(variant, group[1]),
            item_id=item.id,
            file_path=file_path,
            drive_file_id=drive_file_id,
            mount_id=group[1],
        ))
        if len(targets) >= limit:
            break
    return targets


def build_fetch_fn(db: Session, target: ProbeTarget) -> Callable[[], tuple[int, Optional[str]]]:
    """给一个探针造取流函数（resolve + Range 读，单次尝试、不轮换凭据）。

    - mount_relay：走既有 ``mounts.resolve_play_target``（只读 file_path
      解析，不搬字节）；解析出 url 则 Range 读，kind=local 则读本机
      文件头 1 字节；挂载不存在/已停用 → unknown，解析其它错 →
      remote_unavailable。
    - drive_relay / drive_direct：Drive API alt=media + SA Bearer 单次
      Range 读；取不到凭据 → credential_error（不轮换、不重试）。
    返回的函数只做这一次尝试，绝不替客户端换变体。
    """
    timeout = probe_timeout_sec()

    def _fetch() -> tuple[int, Optional[str]]:
        if target.variant == "mount_relay":
            from backend.emby_server import mounts as mount_lib

            try:
                resolved = mount_lib.resolve_play_target(target.file_path, db)
            except mount_lib.MountError as e:
                msg = str(e)
                if "不存在" in msg or "已停用" in msg:
                    raise ProbeError(OUTCOME_UNKNOWN, msg) from e
                raise ProbeError(OUTCOME_REMOTE_UNAVAILABLE, msg) from e
            if resolved.kind == "local":
                try:
                    with open(resolved.value, "rb") as f:
                        f.read(1)
                    return 206, None
                except FileNotFoundError as e:
                    raise ProbeError(OUTCOME_UNKNOWN, str(e)) from e
                except OSError as e:
                    raise ProbeError(OUTCOME_REMOTE_UNAVAILABLE, str(e)) from e
            return http_range_probe(resolved.value, resolved.headers, timeout)
        if target.variant in ("drive_relay", "drive_direct"):
            if not target.drive_file_id:
                raise ProbeError(OUTCOME_UNKNOWN, "drive item has no drive_file_id")
            from backend.emby_server import drive_auth

            token = drive_auth.get_drive_bearer_token()
            if not token:
                raise ProbeError(OUTCOME_CREDENTIAL_ERROR, "no drive bearer token")
            url = ("https://www.googleapis.com/drive/v3/files/"
                   + target.drive_file_id + "?alt=media&supportsAllDrives=true")
            return http_range_probe(url, {"Authorization": f"Bearer {token}"}, timeout)
        raise ProbeError(OUTCOME_UNKNOWN, f"variant {target.variant} has no probe path")

    return _fetch


def probe_variants_once(session_factory, *, target_builder=None) -> dict | None:
    """工人单轮：采样探针 → 逐个探活 → 落库提交。

    开关关时返回 None、不发任何请求、不写任何状态（测试与工人共用
    这个门）。单轮内某探针抛未分类异常只记警告、不中断整轮；提交
    失败回滚整轮（行是新增表，回滚不影响旧播放）。
    """
    if not probe_enabled():
        return None
    builder = target_builder or build_probe_targets
    db = None
    try:
        db = session_factory()
        targets = builder(db)
        threshold = failure_threshold()
        stats = {"targets": len(targets), "ok": 0, "unknown": 0,
                 "credential_error": 0, "remote_unavailable": 0}
        for target in targets:
            try:
                outcome = probe_fetch(build_fetch_fn(db, target))
            except Exception as e:  # noqa: BLE001 - 单探针异常不炸整轮
                logger.warning("变体探活探针异常（%s），本探针跳过: %s",
                               target.variant_key, e)
                continue
            apply_probe_result(db, target.variant_key, outcome, threshold)
            stats[outcome.kind] = stats.get(outcome.kind, 0) + 1
        db.commit()
        return stats
    except Exception:
        logger.warning("变体探活单轮失败（回滚，不影响播放）", exc_info=True)
        if db is not None:
            try:
                db.rollback()
            except Exception:  # noqa: BLE001 - 回滚失败只记日志
                pass
        return None
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:  # noqa: BLE001 - 关闭失败只记日志
                pass


def run_probe_loop(session_factory, stop_event=None) -> None:
    """工人循环：每间隔一轮；单轮失败只记警告、不停循环。"""
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        probe_variants_once(session_factory)
        interval = probe_interval_sec()
        if stop_event is not None:
            if stop_event.wait(interval):
                return
        else:
            time.sleep(interval)


_probe_thread: threading.Thread | None = None
_probe_lock = threading.Lock()


def start_probe_worker(stop_event=None) -> bool:
    """启动变体探活工人（worker.py 调用，照 list_projection 的写法）。

    开关关时返回 False、不启动；幂等：已有存活线程时直接返回 True。
    """
    global _probe_thread
    if not probe_enabled():
        return False
    with _probe_lock:
        alive = _probe_thread
        if alive is not None and alive.is_alive():
            return True
        from backend.database import SessionLocal

        t = threading.Thread(
            target=run_probe_loop,
            args=(SessionLocal,),
            kwargs={"stop_event": stop_event},
            daemon=True, name="variant-availability-probe",
        )
        t.start()
        _probe_thread = t
        return True
