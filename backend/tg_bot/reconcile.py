# -*- coding: utf-8 -*-
"""TG Bot 群-号一致性对账（embyboss 借鉴第 3 项）。

管理员私聊 /reconcile 触发：把站内已绑号用户逐一拿去名单群核验
在群状态（sender.get_chat_member），找出不一致：

- ② 已绑号但不在任何名单群；其中 ③ 仍有公益资格的是重点提示
  （与退群处置 #1527 的口径呼应：不在群就不该留着资格）；
- ① 在群但没绑站内号：Telegram Bot API 没有群成员枚举接口
  （只有 getChatMember 单人核验），无法全量统计——报告里如实
  标注不可枚举，绝不拿已知样本冒充全量。

处置纪律（两段式，默认只报告、零自动处置）：
- 报告阶段全只读：不建行、不改行、不提交事务；
- 唯一处置＝取消 ③ 的公益资格，必须二次确认：报告卡按钮
  （reconcile:ask）只出确认卡、不执行；确认卡按钮
  （reconcile:do）才执行。执行前对每个目标逐一复核在群状态，
  已回群 / 接口失败 / 资格已无的一律跳过并在回执中列明；
- 执行口径与退群处置（#1527 group_leave）一致：is_welfare=False
  + WelfareGrantLog(channel="group_leave", days=0,
  granted_by=操作管理员 id)；另按管理端现行口径写 AdminLog
  （action="welfare_revoke"，details 注明来源 tg_reconcile）。
  不删号、不动能量（points）、不动查看凭证；
- 本模块没有任何定时 / 自动处置路径，只被命令与按钮回调调用。

核验是顺序执行：getChatMember 一人一次 HTTP，且 token 读取走
调用方 session（SQLAlchemy Session 非线程安全，不共用并发）。
单次核验人数有上限（MAX_CHECKS），超出的在报告覆盖说明里标注。
"""
from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass, field

from backend import models
from backend.tg_bot import layout, policy, sender

logger = logging.getLogger(__name__)

# 单次对账最多逐一核验的已绑用户数；超出部分不核验，报告如实标注
MAX_CHECKS = 500
# 报告卡每类明细最多列几人，超出截断并注明总数
DETAIL_LIMIT = 10
# 确认卡名单最多列几人，超出截断并注明总数
CONFIRM_LIMIT = 20
# 对账结果缓存（按管理员 TG 号）：二次确认两步之间复用同一次对账，
# 避免连点按钮反复打 TG 接口；执行时仍逐一复核，不拿缓存做裁决
CACHE_TTL_SECONDS = 600.0

_CACHE: dict[int, tuple[float, "ReconcileResult"]] = {}


@dataclass
class ReconcileResult:
    groups: list[int] = field(default_factory=list)
    bound_total: int = 0
    checked: int = 0
    not_in_group: list = field(default_factory=list)     # ② WebUser
    welfare_targets: list = field(default_factory=list)  # ③ ② 中仍有资格者
    unknown: list = field(default_factory=list)          # 接口失败未核验者

    @property
    def truncated(self) -> bool:
        return self.checked < self.bound_total


def clear_cache() -> None:
    _CACHE.clear()


def cache_result(admin_tg_id: int, result: ReconcileResult) -> None:
    _CACHE[int(admin_tg_id)] = (time.monotonic(), result)


def cached_result(admin_tg_id: int) -> ReconcileResult | None:
    hit = _CACHE.get(int(admin_tg_id))
    if hit is None:
        return None
    ts, result = hit
    if time.monotonic() - ts > CACHE_TTL_SECONDS:
        _CACHE.pop(int(admin_tg_id), None)
        return None
    return result


def _member_status(db, groups, tg_id, checker):
    """逐群核验：任一群在群即在群；全部群明确不在才算不在；
    有群接口失败且无群确认在群 → 未核验（None），绝不算作不在群。"""
    saw_error = False
    for chat_id in groups:
        is_member, _status = checker(db, chat_id, int(tg_id))
        if is_member is True:
            return True
        if is_member is None:
            saw_error = True
    return None if saw_error else False


def run_reconcile(db, cfg=None, checker=None) -> ReconcileResult:
    """执行一次对账（全只读）。checker 默认 sender.get_chat_member。"""
    if cfg is None:
        cfg = policy.read_bot_config(db)
    if checker is None:
        checker = sender.get_chat_member
    groups = sorted(policy.parse_group_ids(cfg.get("bot_group_ids", "")))
    result = ReconcileResult(groups=groups)
    if not groups:
        return result
    bound = (
        db.query(models.WebUser)
        .filter(models.WebUser.telegram_id.isnot(None))
        .order_by(models.WebUser.id.asc())
        .all()
    )
    result.bound_total = len(bound)
    targets = bound[:MAX_CHECKS]
    result.checked = len(targets)
    for user in targets:
        status = _member_status(db, groups, user.telegram_id, checker)
        if status is True:
            continue
        if status is None:
            result.unknown.append(user)
        else:
            result.not_in_group.append(user)
            if bool(getattr(user, "is_welfare", False)):
                result.welfare_targets.append(user)
    return result


def _name_list(users, limit: int) -> str:
    """名单展示：站内用户名（TG 号）；站内不存 TG 用户名，只出号。"""
    shown = users[:limit]
    parts = [
        f"{html.escape(str(u.username or ''))}（TG {u.telegram_id}）"
        for u in shown
    ]
    text = "、".join(parts) if parts else "—"
    if len(users) > len(shown):
        text += f" …等共 {len(users)} 人"
    return text


_COVERAGE_NOTE_1 = (
    "在群未绑号无法全量统计：Telegram Bot API 没有群成员枚举接口，"
    "只能逐人核验已知账号；没绑号的群成员 bot 无从枚举。"
)


def render_report(result: ReconcileResult) -> tuple[str, dict | None]:
    """对账报告卡（只报告，零处置）。"""
    if not result.groups:
        return layout.card(
            layout.title("群号一致性对账"),
            [
                layout.row("⚠️", "未配置名单群",
                           "bot_group_ids 为空，没有可核验的群，本次无法对账"),
            ],
            notes=[_COVERAGE_NOTE_1],
        ), layout.back_markup()
    rows = [
        layout.row("👥", "名单群", f"{len(result.groups)} 个"),
        layout.row("🔗", "已绑号用户", result.bound_total),
        layout.row("🧾", "本次已核验", result.checked),
        layout.row("🚪", "已绑但不在群", f"{len(result.not_in_group)} 人"),
        layout.row("🎫", "仍有公益资格", f"{len(result.welfare_targets)} 人"),
        layout.row("❓", "未核验", f"{len(result.unknown)} 人"),
    ]
    notes = []
    if result.not_in_group:
        notes.append("不在群明细：" + _name_list(result.not_in_group, DETAIL_LIMIT))
    if result.welfare_targets:
        notes.append("其中有资格：" + _name_list(result.welfare_targets, DETAIL_LIMIT))
    if result.unknown:
        notes.append(
            "未核验明细（接口失败，不算不在群）："
            + _name_list(result.unknown, DETAIL_LIMIT))
    notes.append(_COVERAGE_NOTE_1)
    if result.truncated:
        notes.append(
            f"覆盖说明：已绑号共 {result.bound_total} 人，单次最多核验 "
            f"{MAX_CHECKS} 人，本次只核验前 {result.checked} 人，结论仅覆盖这部分。")
    if result.unknown:
        notes.append("覆盖说明：未核验者是 TG 接口失败，状态未知，不计入不在群。")
    markup_rows = []
    if result.welfare_targets:
        markup_rows.append([{
            "text": f"🎫 取消这批的公益资格（{len(result.welfare_targets)} 人）",
            "callback_data": "reconcile:ask",
        }])
    markup_rows.append(layout.home_row())
    return layout.card(layout.title("群号一致性对账"), rows, notes), {
        "inline_keyboard": markup_rows,
    }


def render_confirm(db, admin_tg_id: int) -> tuple[str, dict | None]:
    """二次确认卡：列明将影响的账号与动作口径，点确认才执行。"""
    result = cached_result(admin_tg_id)
    if result is None:
        result = run_reconcile(db)
        cache_result(admin_tg_id, result)
    targets = result.welfare_targets
    if not targets:
        return layout.card(
            layout.title("取消公益资格"),
            [layout.row("✅", "处置名单", "当前没有仍有资格的不在群账号，无需处置")],
        ), layout.back_markup()
    rows = [
        layout.row("🎫", "将取消资格", f"{len(targets)} 人"),
        layout.row("🛠️", "动作口径",
                   "只取消公益资格（退群口径 group_leave 留痕），"
                   "不删号、不动能量与查看凭证"),
        layout.row("🔁", "执行前复核", "逐一再查在群状态，已回群的自动跳过"),
    ]
    notes = ["名单：" + _name_list(targets, CONFIRM_LIMIT)]
    markup = {"inline_keyboard": [
        [
            {"text": "✅ 确认执行", "callback_data": "reconcile:do"},
            {"text": "✖️ 放弃", "callback_data": "reconcile:abort"},
        ],
        layout.home_row(),
    ]}
    return layout.card(layout.title("确认取消公益资格"), rows, notes), markup


def execute_cancel_welfare(db, admin, result: ReconcileResult, checker=None):
    """执行处置：对 ③ 名单逐一复核后取消资格（group_leave 口径）。

    返回 (done, skipped)：done 为实际取消的 WebUser 列表，
    skipped 为 (WebUser, 原因) 列表。整个批次一次提交。
    """
    if checker is None:
        checker = sender.get_chat_member
    done, skipped = [], []
    for target in result.welfare_targets:
        user = db.query(models.WebUser).filter(
            models.WebUser.id == target.id).first()
        if user is None or not bool(getattr(user, "is_welfare", False)):
            skipped.append((target, "资格已无"))
            continue
        status = _member_status(db, result.groups, user.telegram_id, checker)
        if status is True:
            skipped.append((user, "已回群"))
            continue
        if status is None:
            skipped.append((user, "接口失败未核验"))
            continue
        user.is_welfare = False
        db.add(models.WelfareGrantLog(
            user_id=user.id, channel="group_leave", days=0,
            granted_by=admin.id))
        db.add(models.AdminLog(
            admin_user_id=admin.id,
            action="welfare_revoke",
            target_type="user",
            target_id=user.id,
            details={"source": "tg_reconcile", "channel": "group_leave"},
        ))
        done.append(user)
    if done:
        db.commit()
    logger.info(
        "tg reconcile welfare cancelled: admin=%s done=%s skipped=%s",
        admin.id, [u.id for u in done], [u.id for u, _ in skipped])
    return done, skipped


def render_receipt(done, skipped) -> tuple[str, dict | None]:
    """处置回执卡：实际处置名单与跳过名单都列明。"""
    rows = [
        layout.row("✅", "已取消资格", f"{len(done)} 人"),
        layout.row("⏭️", "已跳过", f"{len(skipped)} 人"),
    ]
    notes = []
    if done:
        notes.append("已处置：" + _name_list(done, DETAIL_LIMIT))
    for user, reason in skipped:
        notes.append(
            f"跳过：{html.escape(str(user.username or ''))}"
            f"（TG {user.telegram_id}）— {reason}")
    return layout.card(layout.title("对账处置回执"), rows, notes),         layout.back_markup()


def render_abort() -> tuple[str, dict | None]:
    return layout.card(
        layout.title("取消公益资格"),
        [layout.row("✖️", "已放弃", "未做任何处置，账号与资格都没动")],
    ), layout.back_markup()
