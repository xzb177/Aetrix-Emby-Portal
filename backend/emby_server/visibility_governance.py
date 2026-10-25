"""可见性治理流水线（FMBY 媒体库学习 L7：只学机制，不抄表名/字段名）。

魂：可见性是一条可审计的治理流水线（谁、何时、为什么、据什么规则），
不是条目上一个布尔位；决策可追溯、可复核、可回放。

决策函数 = 身份货架案 A 判定口径（货架严 / 搜索宽 / 直达放行），
与 identity_gate.py 的判据同源、只换了一层 Python 实现（SQL 表达式
版在 identity_gate.identity_clause，Python 行判定版在这里，两边口径
必须一致：movie/series 看自身 tmdb_id/imdb_id strip 后非空；
season/episode 跟随所属 series；只有豆瓣身份不算；空白串不算；
已软删的 series 不能替它的集绑定；其余类型不在治理内，恒通过）。

硬约束（测试必须证明，缺一不可）：
① 开关关时行为与现行逐字一致：resolve_* 走现行货架口径
   （IDENTITY_SHELF_GATE 关 = 全露出，开 = 案 A）。
② 开关开时露出结果与案 A 完全一致，只补审计链：resolve_* 恒等于
   案 A 判定，与复核动作无关——复核动作只追加决策行与事件，露出
   变化需另行拍板，绝不擅自让复核动作改露出。

不可变性：事件表（VisibilityEvent）与决策表（ItemVisibilityDecision）
在本模块里只有 INSERT 路径——本文件没有任何更新或删除调用，也不给
这两张表留更新/删除函数；任务表的状态流转（pending →
approved / rejected）是队列本身的状态机，只改任务行，不改事件与决策行。

总开关：env ``VISIBILITY_GOVERNANCE_ENABLED``（默认 0=关），模块加载
时读一次（与 IDENTITY_SHELF_GATE 同口径，改需重启）。管理后台不另立
开关（2026-10-19 原则：开关只走 env/文件配置）。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from backend.emby_server import models as em
from backend.emby_server.env_util import env_int
from backend.emby_server import identity_gate

logger = logging.getLogger(__name__)

#: 可见性治理总开关（默认关）。启动时读一次，热路径不碰 env。
VISIBILITY_GOVERNANCE_ENABLED = env_int("VISIBILITY_GOVERNANCE_ENABLED", 0, 0, 1)

_SURFACES = ("shelf", "search", "direct")


def governance_enabled() -> bool:
    """治理流水线是否开启（全仓读开关的唯一入口；测试 patch 本模块常量）。"""
    return VISIBILITY_GOVERNANCE_ENABLED > 0


def _own_bound(item) -> bool:
    """行自身已绑定：tmdb_id / imdb_id 任一 strip 后非空（与 identity_gate._own_bound 同口径）。"""
    for v in (getattr(item, "tmdb_id", None), getattr(item, "imdb_id", None)):
        if v is not None and str(v).strip():
            return True
    return False


def item_bound(db, item) -> bool:
    """案 A 绑定判定（Python 行判定版）：

    - movie/series：自身绑定；
    - season/episode：自身绑定，或所属 series 行绑定（已软删的 series 不算）；
    - 其余类型：不在治理内，恒 True。
    """
    item_type = getattr(item, "item_type", None)
    if item_type in ("movie", "series"):
        return _own_bound(item)
    if item_type in ("season", "episode"):
        if _own_bound(item):
            return True
        series_id = getattr(item, "series_id", None)
        if series_id is None:
            return False
        series = (db.query(em.MediaItem)
                  .filter(em.MediaItem.id == series_id).first())
        if series is None or getattr(series, "deleted_at", None) is not None:
            return False
        return _own_bound(series)
    return True


def case_a_visible(db, item, surface: str) -> bool:
    """案 A 露出判定：货架面严（只露已绑定），搜索面宽、直达面放行（恒 True）。

    surface 取值只许 shelf / search / direct，其他值抛 ValueError
    （不静默回落，避免调用方拼错面却拿到一个“看似合理”的结果）。
    """
    if surface not in _SURFACES:
        raise ValueError(f"未知可见性面：{surface!r}（只许 shelf/search/direct）")
    if surface == "shelf":
        return item_bound(db, item)
    return True


def resolve_visibility(db, item, surface: str) -> bool:
    """露出解析（唯一出口）：

    - 开关关：现行口径逐字一致——货架面走 IDENTITY_SHELF_GATE
      （关 = 全露出，开 = 案 A），搜索/直达面恒 True；
    - 开关开：恒等于案 A 判定（货架面 = 绑定与否，搜索/直达面恒 True），
      与治理任务/决策行/事件的任何内容无关。
    """
    if not governance_enabled():
        if surface == "shelf" and identity_gate.gate_enabled():
            return item_bound(db, item)
        if surface not in _SURFACES:
            raise ValueError(f"未知可见性面：{surface!r}（只许 shelf/search/direct）")
        return True
    return case_a_visible(db, item, surface)


def _append_event(db, task_id, item_id, event_type: str, actor, detail: dict) -> em.VisibilityEvent:
    """只追加事件（本模块唯一的事件写路径；无更新/删除路径）。"""
    ev = em.VisibilityEvent(
        task_id=task_id, item_id=item_id, event_type=event_type,
        actor=actor, detail_json=json.dumps(detail, ensure_ascii=False))
    db.add(ev)
    db.flush()
    return ev


def create_task(db, item, surface: str, reason: str = "",
                actor: Optional[int] = None) -> em.VisibilityGovernanceTask:
    """新建复核任务 + task_created 事件（不改变任何露出结果）。"""
    if surface not in _SURFACES:
        raise ValueError(f"未知可见性面：{surface!r}（只许 shelf/search/direct）")
    task = em.VisibilityGovernanceTask(
        item_id=item.id, item_name=getattr(item, "name", None),
        surface=surface, status="pending", reason=reason or "")
    db.add(task)
    db.flush()
    _append_event(db, task.id, item.id, "task_created", actor,
                  {"surface": surface, "reason": reason or ""})
    return task


def list_tasks(db, status: Optional[str] = None, page: int = 1,
               page_size: int = 20):
    """复核队列分页（最新在前；status 可筛 pending/approved/rejected）。"""
    if status is not None and status not in em.VISIBILITY_TASK_STATUSES:
        raise ValueError(f"未知任务状态：{status!r}")
    q = db.query(em.VisibilityGovernanceTask)
    if status is not None:
        q = q.filter(em.VisibilityGovernanceTask.status == status)
    total = q.count()
    rows = (q.order_by(em.VisibilityGovernanceTask.created_at.desc(),
                       em.VisibilityGovernanceTask.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return rows, total


def get_task(db, task_id: int) -> Optional[em.VisibilityGovernanceTask]:
    """按 id 取任务（不存在返回 None，调用方转 404）。"""
    return (db.query(em.VisibilityGovernanceTask)
            .filter(em.VisibilityGovernanceTask.id == task_id).first())


def review_task(db, task_id: int, approve: bool, actor: Optional[int] = None,
                note: str = "") -> em.VisibilityGovernanceTask:
    """处置复核任务（准入/驳回留痕）。

    只做三件事：① 任务行状态流转 pending → approved / rejected；
    ② 追加一条决策行（visible = 处置当刻的案 A 判定结果，审计快照，
    露出读路径不读它）；③ 追加事件。**不改任何条目行、不改露出**：
    处置前后 resolve_visibility 的返回值恒等（测试证明）。
    非 pending 任务重复处置抛 ValueError（409 由调用方转）。
    """
    task = get_task(db, task_id)
    if task is None:
        raise ValueError(f"治理任务不存在：{task_id}")
    if task.status != "pending":
        raise ValueError(f"该治理任务已处置（{task.status}），不能重复操作")
    item = (db.query(em.MediaItem)
            .filter(em.MediaItem.id == task.item_id).first())
    visible = case_a_visible(db, item, task.surface) if item is not None else False
    rule_key = ("case_a_bound" if visible and task.surface == "shelf"
                else "case_a_unbound" if task.surface == "shelf"
                else "case_a_open")
    task.status = "approved" if approve else "rejected"
    task.decided_by = actor
    task.decided_at = datetime.now()
    task.decision_note = note or ""
    db.flush()
    decision = em.ItemVisibilityDecision(
        item_id=task.item_id, surface=task.surface, visible=visible,
        rule_key=rule_key, task_id=task.id)
    db.add(decision)
    db.flush()
    _append_event(db, task.id, task.item_id,
                  "task_approved" if approve else "task_rejected", actor,
                  {"surface": task.surface, "note": note or "",
                   "rule_key": rule_key, "visible_audit": visible})
    _append_event(db, task.id, task.item_id, "decision_recorded", actor,
                  {"surface": task.surface, "rule_key": rule_key,
                   "visible_audit": visible})
    return task


def list_events(db, item_id: Optional[int] = None, page: int = 1,
                page_size: int = 50):
    """事件流水分页（只读；最新在前）。"""
    q = db.query(em.VisibilityEvent)
    if item_id is not None:
        q = q.filter(em.VisibilityEvent.item_id == item_id)
    total = q.count()
    rows = (q.order_by(em.VisibilityEvent.created_at.desc(),
                       em.VisibilityEvent.id.desc())
            .offset((page - 1) * page_size).limit(page_size).all())
    return rows, total


def list_decisions(db, item_id: int):
    """某条目的决策行（审计用，最新在前；露出读路径不读本表）。"""
    return (db.query(em.ItemVisibilityDecision)
            .filter(em.ItemVisibilityDecision.item_id == item_id)
            .order_by(em.ItemVisibilityDecision.created_at.desc(),
                      em.ItemVisibilityDecision.id.desc()).all())
