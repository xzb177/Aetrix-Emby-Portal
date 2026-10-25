# -*- coding: utf-8 -*-
"""移动云盘 139 挂载（FMBY 挂载学习，挂载-4 照抄项）。

**运行时契约不属于本模块**：传输、握手、错误分级、会话管理一律复用
``backend.emby_server.provider_runtime``（115 那一路在 PR #1251 立定的
共享契约，本 PR 将该文件原样拷入、逐字节一致，见完工回报；115 先合
并时变基丢弃本 PR 的拷贝即可）。本模块只做 139 自己的三样：
139 handler、139 扩展登记、宿主侧 ``Yun139Mount``（复用
``mount_sidecar.SidecarMountProvider`` 的四口翻译与能力映射表）。

照抄对象（证据出自两份设计稿与深挖报告）：
- 外置 sidecar：独立 Python 进程 + UDS + 协议 ``external-provider-runtime-v1``、
  manifest ``external-provider-runtime-manifest-v1``；
- 8 项基础能力 + 139 扩展（见 ``YUN139_EXTENSIONS``）；
- 变体形态：原画 + 云转码（分享空间多变体、个人空间只有原画一条），
  变体公开载荷不含 URL（地址按变体再解析一次）；
- 账号池：``yun139_account_pools`` + ``yun139_account_pool_members``，
  多账号加权轮用、失败进冷却；
- 凭据档案：复用 ``mount_credentials`` 一张表（挂载稿 §4.2 拼法），
  本模块只提供档案视图适配，不新建凭据表。

**本 PR 不做、也不许从本模块顺带做的事**（地盘红线）：
- 不碰播放变体选路：本模块只提供 ``Yun139Mount.resolve_play_variants``
  （变体解析），不改 ``play_variants.list_variants`` / video_stream；
- 不碰 BYOS 绑定：不改 ``user_bindings``，凭据档案只读 ``mount_credentials``；
- 不碰 #13 认亲与阶段 8 文件；不另起管理页（管理后台 UI 由专线统一做）。

**默认关**：env ``YUN139_ENABLED`` 默认 0。关时 ``Yun139Mount`` 构造即
响亮报错（不是静默空库）；开时才走 sidecar。回滚 = 关开关。

**与 115 sidecar 分发口径的差异（有意为之）**：115 sidecar 不进
``build_provider`` 分发（进程内 Pan115Mount 仍是默认路径，sidecar 须显式
构造）；139 没有进程内形态，sidecar 就是唯一形态，故 ``Yun139Mount``
照 DriveMount 同款注册进 ``_PROVIDERS`` 与 ``MOUNT_TYPES``（路径前缀
``139:/``），否则 139 挂载无任何构造入口。

**139 专属假设清单（契约层面的已由共享契约定死，不再列；以下只剩
139 自身未实测处，115 契约落地后仍待对账）**：
1. 扩展名（139 自定，符合“须以 ``yun139.`` 开头”的契约规则）：
   ``yun139.resolve_play_variants``（变体清单，公开载荷不含 URL）、
   ``yun139.resolve_play_target``（带变体上下文的换链——共享契约的基础
   ``resolve_play_target(session, path)`` 没有 variant_id 槽位，变体上下文
   只能走 139 自己的扩展，不许偷改基础签名）、
   ``yun139.create_outer_link_transfer``（FMBY 实测名）与
   ``yun139.transfer_status``（为查状态补的同伴扩展）。
2. 变体 id 生成（半实测）：有高度用“前缀-h{高度}”、否则用标签 slug、
   冲突加 ``-2/-3`` 后缀是实测规则；前缀本身未写明，本模块取变体
   ``kind`` 作前缀，属假设。
3. 转存状态值（假设）：前端字段只实测到 ``original_preparation /
   transfer_state / requires_transfer`` 存在，取值未实测；本模块用
   ``preparing / ready / failed`` 三态，转存 API 的实际调用（139 外链
   转存接口本身）未实测、不在本 PR 内实现——sidecar 的转存注册表只管
   状态机，不管真去调 139 转存接口。
4. 账号池语义（半实测）：表字段是实测；``max_concurrent`` 的口径
   （池内总并发还是单成员并发）在证据里未写明，本模块按“池内总并发”
   实现；成功清零 fail_count 并解冻、失败 +1 并进冷却，是我方语义
   （非 FMBY 实测值）。
5. 换链凭据（139 与 115 不同）：115 宿主重贴自己现取的 Cookie；
   139 在宿主侧没有 Cookie 可贴（凭据在 sidecar 会话侧），故
   ``Yun139Mount`` 不走父类的 Cookie 重贴路径，sidecar 回包的 URL
   视为已预签名；宿主侧仍剥掉回包里万一出现的 Cookie/Authorization
   头（防御性，不信任回包凭据）。若 139 上游实际要求宿主贴头，
   这是待补的协议缺口（见未决事项）。
6. 凭据档案字段（映射说明）：FMBY 档案的 ``can_refresh /
   authorization_expires_at`` 在我方 ``mount_credentials`` 里没有对应列，
   档案视图里 ``can_refresh`` 恒 False（与能力映射恒 False 一致）、
   ``authorization_expires_at`` 为 None；补列是凭据分离 lane（挂载第 4 步）
   的事，本 PR 不擅自加列。
7. 139 上游 API（未实测）：本模块不含任何真实 139 HTTP 调用；sidecar
   的上游以 ``Yun139Upstream`` 接口注入，默认无上游（方法报契约的
   ``not_implemented``）。真实 139 接口须另有实测证据才能接。
8. 会话数据（139 自定）：宿主建会话时交 ``{"space", "pool_id"}``
   （挂载配置，非秘密）；本 PR 的 handler 除归属外暂不消费这两项，
   供未来按空间/池分流时用，不许往会话里塞凭据原文。

证据：``fmby-mount-adoption-design-20261024.md`` §1.2/§1.3、
``fmby-copy-decisions-20261024.md`` 挂载-4、
``fmby-playback-exclusive-deepdive-20261024.md`` §2（变体结构、
public_payload 不含 url、select_variant、个人空间单原画）与 §6。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from backend.emby_server import mounts as mount_lib
from backend.emby_server.env_util import env_int
from backend.emby_server.mount_sidecar import SidecarMountProvider
from backend.emby_server.mounts import MountError, PlayTarget
from backend.emby_server.provider_runtime import (
    BASE_CAPABILITIES,
    PROTOCOL,
    ProviderRuntimeClient,
    ProviderRuntimeError,
    encode_bytes,
    register_provider_extensions,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 常量

MOUNT_YUN139 = "yun139"

#: 139 扩展（登记进共享契约的 PROVIDER_EXTENSIONS；握手时校验归属与齐全）。
YUN139_EXTENSIONS: tuple[str, ...] = (
    "yun139.resolve_play_variants",
    "yun139.resolve_play_target",
    "yun139.create_outer_link_transfer",
    "yun139.transfer_status",
)

#: 变体 kind：原画（实测，个人空间唯一变体 kind）与云转码
#: （播放目标枚举里的 transcoded，云端转的 HLS、非本机转码）。
VARIANT_KIND_ORIGINAL = "original"
VARIANT_KIND_TRANSCODED = "transcoded"


# ---------------------------------------------------------------- 开关与配置

def yun139_enabled() -> bool:
    """139 挂载总开关（env ``YUN139_ENABLED``，默认关；每次现读）。"""
    return os.getenv("YUN139_ENABLED", "0") not in ("0", "false", "False", "")


def sidecar_text_max_bytes() -> int:
    """旁挂文本（.strm / NFO / 字幕）单次读取上限（字节；我方起步值）。"""
    return env_int("YUN139_SIDECAR_TEXT_MAX_BYTES", 1048576, 1024)


def pool_default_cooldown_sec() -> int:
    """账号池默认冷却秒数：池行 cooldown_seconds 为空时的回落值（我方起步值）。"""
    return env_int("YUN139_POOL_DEFAULT_COOLDOWN_SEC", 60, 0)


def pool_default_max_concurrent() -> int:
    """账号池默认并发上限：池行 max_concurrent 为空时的回落值；0 = 不限。"""
    return env_int("YUN139_POOL_DEFAULT_MAX_CONCURRENT", 0, 0)


# ---------------------------------------------------------------- 变体

class Yun139VariantNotFound(MountError):
    """变体 id 精确匹配不上（FMBY select_variant 语义：报 404，不静默回落）。"""


@dataclass
class Yun139Variant:
    """139 播放变体（字段取自深挖报告的 RuntimePlaybackVariant）。

    ``url`` 只在 sidecar 内部解析时存在；对外公开载荷一律不含 url
    （``public_payload()``，FMBY 实测：清单阶段不下发可直接播的地址，
    地址按变体再解析一次）。
    """

    id: str
    label: str
    kind: str
    url: str = ""
    is_default: bool = False
    requires_transfer: bool = False
    width: int = 0
    height: int = 0
    bitrate: int = 0
    container: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    size: int = 0
    expires_at: str = ""

    def public_payload(self) -> dict:
        """公开载荷：除 url 外的全部字段（FMBY public_payload 口径）。"""
        return {
            "id": self.id, "label": self.label, "kind": self.kind,
            "is_default": self.is_default,
            "requires_transfer": self.requires_transfer,
            "width": self.width, "height": self.height,
            "bitrate": self.bitrate, "container": self.container,
            "video_codec": self.video_codec, "audio_codec": self.audio_codec,
            "size": self.size, "expires_at": self.expires_at,
        }


#: 公开载荷白名单（解析 sidecar 回包时只认这些键，url 一律丢弃）。
_VARIANT_PUBLIC_KEYS = (
    "id", "label", "kind", "is_default", "requires_transfer", "width",
    "height", "bitrate", "container", "video_codec", "audio_codec",
    "size", "expires_at",
)


def parse_variant_payload(payload: dict) -> Yun139Variant:
    """sidecar 变体回包 → Yun139Variant（非白名单键一律丢弃，url 永不进公开变体）。"""
    data = {key: payload.get(key) for key in _VARIANT_PUBLIC_KEYS}
    return Yun139Variant(
        id=str(data["id"] or ""), label=str(data["label"] or ""),
        kind=str(data["kind"] or ""),
        is_default=bool(data["is_default"]),
        requires_transfer=bool(data["requires_transfer"]),
        width=int(data["width"] or 0), height=int(data["height"] or 0),
        bitrate=int(data["bitrate"] or 0),
        container=str(data["container"] or ""),
        video_codec=str(data["video_codec"] or ""),
        audio_codec=str(data["audio_codec"] or ""),
        size=int(data["size"] or 0),
        expires_at=str(data["expires_at"] or ""),
    )


def _slug_label(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").strip().lower()).strip("-")
    return slug or "variant"


def variant_id_for(kind: str, label: str, height: int,
                   existing: set[str] | frozenset[str]) -> str:
    """变体 id 生成（实测规则，前缀取 kind 属假设，见模块头假设 2）。

    有高度 → ``{kind}-h{height}``；否则用标签 slug；冲突加 -2/-3 后缀。
    """
    base = f"{kind}-h{int(height)}" if height else _slug_label(label)
    if base not in existing:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing:
        suffix += 1
    return f"{base}-{suffix}"


def select_variant(variants: list[Yun139Variant],
                   variant_id: Optional[str]) -> Yun139Variant:
    """FMBY select_variant（实测语义）：

    - 给了 variant_id：精确匹配，匹配不上报 Yun139VariantNotFound
      （404），**不许静默回落到默认条**——变体选择权是真的；
    - 没给：取 is_default 的那条；没有默认条取第 0 条；空清单报错。
    """
    if variant_id:
        for variant in variants:
            if variant.id == variant_id:
                return variant
        raise Yun139VariantNotFound(f"分享播放版本不存在: {variant_id}")
    if not variants:
        raise Yun139VariantNotFound("无可用播放变体")
    for variant in variants:
        if variant.is_default:
            return variant
    return variants[0]


def personal_space_variants() -> list[Yun139Variant]:
    """个人空间变体清单（实测：只有一条原画，is_default=True，无需转存）。"""
    return [Yun139Variant(id="original", label="原画", kind=VARIANT_KIND_ORIGINAL,
                          is_default=True, requires_transfer=False)]


# ---------------------------------------------------------------- 转存状态机

#: 转存状态（假设，见模块头假设 3：取值未实测，三态是我方状态机）。
TRANSFER_PREPARING = "preparing"
TRANSFER_READY = "ready"
TRANSFER_FAILED = "failed"


@dataclass
class TransferTask:
    """一次外链转存任务（sidecar 侧状态，不含 139 真实转存调用）。"""

    id: str
    share_ref: str = ""
    provider_file_id: str = ""
    state: str = TRANSFER_PREPARING
    error: str = ""


class Yun139TransferRegistry:
    """转存任务注册表（sidecar 进程内状态；重启即丢，任务本身可重建）。

    只管状态机：创建 → preparing；上游完成回调 mark_ready；
    上游失败 mark_failed。**不调任何 139 转存接口**（接口未实测，
    见模块头假设 3）。
    """

    def __init__(self) -> None:
        self._tasks: dict[str, TransferTask] = {}
        self._counter = 0

    def create(self, share_ref: str = "", provider_file_id: str = "") -> TransferTask:
        self._counter += 1
        task = TransferTask(id=f"yun139-transfer-{self._counter}",
                            share_ref=share_ref,
                            provider_file_id=provider_file_id)
        self._tasks[task.id] = task
        return task

    def get(self, transfer_id: str) -> TransferTask:
        task = self._tasks.get(transfer_id or "")
        if task is None:
            raise MountError(f"转存任务不存在: {transfer_id}")
        return task

    def mark_ready(self, transfer_id: str, provider_file_id: str = "") -> TransferTask:
        task = self.get(transfer_id)
        task.state = TRANSFER_READY
        if provider_file_id:
            task.provider_file_id = provider_file_id
        return task

    def mark_failed(self, transfer_id: str, error: str = "") -> TransferTask:
        task = self.get(transfer_id)
        task.state = TRANSFER_FAILED
        task.error = error or ""
        return task

    def as_dict(self, task: TransferTask) -> dict:
        return {"transfer_id": task.id, "share_ref": task.share_ref,
                "provider_file_id": task.provider_file_id,
                "state": task.state, "error": task.error}


# ---------------------------------------------------------------- 账号池（纯逻辑，不碰 DB）

@dataclass
class PoolMemberState:
    """池成员的运行时状态（属性名与 ORM 列同名，DB 行可直接喂进来）。"""

    id: Any = None
    weight: int = 1
    fail_count: int = 0
    #: 冷却截止（epoch 秒；None = 未冷却）。ORM 的 DateTime 由 DB 助手换算。
    cooldown_until: Optional[float] = None


class Yun139AccountPool:
    """139 账号池纯逻辑（表字段实测，语义见模块头假设 4）。

    - 策略 ``weighted``：平滑加权轮询（确定性、可测）；
      ``round_robin``：按顺序轮换（忽略 weight）；
    - ``max_concurrent``：池内总并发上限（0/None = 不限）；超限时
      ``pick`` 回 None（调用方按“暂无可用账号”处理，不许偷偷超发）；
    - ``pick`` 只选未冷却成员；``acquire`` / ``release`` 管并发计数；
    - ``record_failure``：fail_count +1 并进冷却；
      ``record_success``：fail_count 清零并解冻。
    """

    def __init__(self, members: list[PoolMemberState], *,
                 strategy: str = "weighted",
                 cooldown_seconds: Optional[int] = None,
                 max_concurrent: Optional[int] = None) -> None:
        self.members = list(members)
        self.strategy = strategy if strategy in ("weighted", "round_robin") else "weighted"
        self.cooldown_seconds = (
            cooldown_seconds if cooldown_seconds is not None
            else pool_default_cooldown_sec())
        self.max_concurrent = (
            max_concurrent if max_concurrent is not None
            else pool_default_max_concurrent())
        self._in_flight: dict[Any, int] = {}
        self._current_weight: dict[Any, int] = {m.id: 0 for m in self.members}
        self._rr_index = 0

    def in_flight_total(self) -> int:
        return sum(self._in_flight.values())

    def available(self, now: float) -> list[PoolMemberState]:
        if self.max_concurrent and self.in_flight_total() >= self.max_concurrent:
            return []
        return [m for m in self.members
                if m.cooldown_until is None or m.cooldown_until <= now]

    def pick(self, now: float) -> Optional[PoolMemberState]:
        candidates = self.available(now)
        if not candidates:
            return None
        if self.strategy == "round_robin":
            member = candidates[self._rr_index % len(candidates)]
            self._rr_index += 1
            return member
        total = sum(max(int(m.weight or 0), 0) for m in candidates)
        if total <= 0:
            return candidates[0]
        best: Optional[PoolMemberState] = None
        for member in candidates:
            self._current_weight[member.id] = (
                self._current_weight.get(member.id, 0) + max(int(member.weight or 0), 0))
            if best is None or self._current_weight[member.id] > self._current_weight[best.id]:
                best = member
        assert best is not None
        self._current_weight[best.id] -= total
        return best

    def acquire(self, member_id: Any) -> None:
        self._in_flight[member_id] = self._in_flight.get(member_id, 0) + 1

    def release(self, member_id: Any) -> None:
        if self._in_flight.get(member_id, 0) > 0:
            self._in_flight[member_id] -= 1

    def record_failure(self, member_id: Any, now: float) -> Optional[PoolMemberState]:
        for member in self.members:
            if member.id == member_id:
                member.fail_count = int(member.fail_count or 0) + 1
                member.cooldown_until = now + self.cooldown_seconds
                return member
        return None

    def record_success(self, member_id: Any) -> Optional[PoolMemberState]:
        for member in self.members:
            if member.id == member_id:
                member.fail_count = 0
                member.cooldown_until = None
                return member
        return None


# ---------------------------------------------------------------- 凭据档案视图（只读 mount_credentials，不新建凭据表）

def credential_profile_for_row(row) -> dict:
    """凭据行 → 139 凭据档案视图（字段映射见模块头假设 6）。

    ``can_refresh`` 恒 False：我方凭据表无此列，且能力映射恒 False——
    声明是“保证”不是“尽力”，没实测出刷新路径就不敢写 True。
    ``authorization_expires_at`` 为 None：同理，无列可读，不编值。
    """
    return {
        "provider": MOUNT_YUN139,
        "credential_id": getattr(row, "id", None),
        "key_id": getattr(row, "key_id", None),
        "status": getattr(row, "status", None),
        "last_error_kind": getattr(row, "last_error_kind", None),
        "last_verified_at": getattr(row, "last_verified_at", None),
        "can_refresh": False,
        "authorization_expires_at": None,
    }


def profile_for_mount(db, mount_id: int, scope: str = "source",
                      owner_user_id: Optional[int] = None) -> Optional[dict]:
    """某挂载的 139 凭据档案（无行回 None，不抛异常、不回落别处）。"""
    if db is None:
        return None
    try:
        from backend.emby_server import models as em
        query = db.query(em.MountCredential).filter(
            em.MountCredential.mount_id == mount_id,
            em.MountCredential.scope == scope)
        if scope == "user":
            query = query.filter(em.MountCredential.owner_user_id == owner_user_id)
        row = query.first()
    except Exception:  # noqa: BLE001 — 表缺失/旧夹具都按“无档案”处理
        return None
    if row is None:
        return None
    return credential_profile_for_row(row)


# ---------------------------------------------------------------- 账号池 DB 助手（行 ↔ 纯逻辑换算，flush 不 commit）

def _epoch_to_datetime(epoch: Optional[float]) -> Optional[datetime]:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).replace(tzinfo=None)


def _datetime_to_epoch(value) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, datetime):
        aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return aware.timestamp()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_pool(db, pool_id: int) -> Optional[Yun139AccountPool]:
    """从 DB 载入账号池（无池回 None）。调用方负责 commit/rollback。"""
    from backend.emby_server import models as em
    pool_row = db.query(em.Yun139AccountPool).filter(
        em.Yun139AccountPool.id == pool_id).first()
    if pool_row is None:
        return None
    members = []
    for row in db.query(em.Yun139AccountPoolMember).filter(
            em.Yun139AccountPoolMember.pool_id == pool_id).order_by(
            em.Yun139AccountPoolMember.id).all():
        members.append(PoolMemberState(
            id=row.id, weight=int(row.weight or 0),
            fail_count=int(row.fail_count or 0),
            cooldown_until=_datetime_to_epoch(row.cooldown_until)))
    return Yun139AccountPool(
        members, strategy=getattr(pool_row, "strategy", "weighted"),
        cooldown_seconds=getattr(pool_row, "cooldown_seconds", None),
        max_concurrent=getattr(pool_row, "max_concurrent", None))


def apply_pool_failure(db, pool: Yun139AccountPool, member_id: Any,
                       now: Optional[float] = None) -> bool:
    """池成员失败：纯逻辑 + DB 行同步（fail_count/cooldown_until），只 flush。"""
    import time as _time

    from backend.emby_server import models as em
    moment = now if now is not None else _time.time()
    member = pool.record_failure(member_id, moment)
    if member is None:
        return False
    row = db.query(em.Yun139AccountPoolMember).filter(
        em.Yun139AccountPoolMember.id == member_id).first()
    if row is not None:
        row.fail_count = member.fail_count
        row.cooldown_until = _epoch_to_datetime(member.cooldown_until)
        db.flush()
    return True


def apply_pool_success(db, pool: Yun139AccountPool, member_id: Any) -> bool:
    """池成员成功：纯逻辑 + DB 行同步（清零 fail_count、解冻），只 flush。"""
    from backend.emby_server import models as em
    member = pool.record_success(member_id)
    if member is None:
        return False
    row = db.query(em.Yun139AccountPoolMember).filter(
        em.Yun139AccountPoolMember.id == member_id).first()
    if row is not None:
        row.fail_count = member.fail_count
        row.cooldown_until = None
        db.flush()
    return True


# ---------------------------------------------------------------- sidecar 上游接口（待实测，默认无上游）

class Yun139Upstream:
    """139 真实上游接口（方法名与 handler 调用一一对应）。

    本 PR 不提供真实实现：139 HTTP API 未实测（无真实凭据、无接口
    文档证据），任何“看起来能跑”的实现都是编造。sidecar 默认无上游，
    对应方法报契约的 ``not_implemented``；真实上游须持实测证据另开 PR。

    回包形态（与宿主侧 ``SidecarMountProvider`` 的消费口径对齐）：
    - ``list_page``：``{"entries": [{name, path, is_dir, size, file_id,
      entry_id}], "next_page_token": ...}``（path 缺省时 handler 按父目录补）；
    - ``stat``：单个 entry 形态的 dict；
    - ``open_range``：字节；
    - ``resolve_play_target``：``{"url": ..., "headers": {...非秘密头...}}``；
    - ``resolve_play_variants``：``list[Yun139Variant]``（可含 url，handler
      下发前一律转公开载荷剥掉）。
    """

    def list_page(self, path: str, page_token: Optional[str] = None) -> dict:
        raise ProviderRuntimeError("not_implemented", "139 上游未配置")

    def stat(self, path: str) -> dict:
        raise ProviderRuntimeError("not_implemented", "139 上游未配置")

    def open_range(self, path: str, byte_offset: int,
                   byte_length: Optional[int] = None) -> bytes:
        raise ProviderRuntimeError("not_implemented", "139 上游未配置")

    def resolve_play_target(self, path: str, *,
                            provider_file_id: Optional[str] = None,
                            variant_id: Optional[str] = None,
                            original_transfer: bool = False,
                            original_direct: bool = False) -> dict:
        raise ProviderRuntimeError("not_implemented", "139 上游未配置")

    def resolve_play_variants(self, path: str, *,
                              provider_file_id: Optional[str] = None) -> list[Yun139Variant]:
        raise ProviderRuntimeError("not_implemented", "139 上游未配置")


# ---------------------------------------------------------------- sidecar handler（契约 duck-type，见 provider_runtime）

class Yun139SidecarHandler:
    """139 sidecar handler：只实现 139 自己的逻辑，传输/会话/握手归契约。

    会话数据（``session_upsert`` 交来的 ``{"space", "pool_id"}``）只记
    归属、不含凭据；上游调用不消费会话数据（本 PR 无凭据可消费）。
    错误一律用契约 kind：缺失 → ``not_found``，上游未接 → 
    ``not_implemented``，参数非法 → ``invalid_request``。
    """

    provider = MOUNT_YUN139
    extensions = YUN139_EXTENSIONS

    def __init__(self, upstream: Optional[Yun139Upstream] = None,
                 transfers: Optional[Yun139TransferRegistry] = None) -> None:
        self.upstream = upstream
        self.transfers = transfers or Yun139TransferRegistry()
        self._session_data: dict[str, dict] = {}

    # ---- 会话钩子（服务端已管会话存活，本钩子只记 handler 侧归属） ----

    def session_upsert(self, session_id: str, data: dict) -> None:
        self._session_data[session_id] = dict(data or {})

    def session_remove(self, session_id: str) -> None:
        self._session_data.pop(session_id, None)

    # ---- 8 项基础能力 ----

    def list_page(self, session, path: str, page_token, page_size) -> dict:
        self._need_upstream()
        rel = "/" + (path or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        page = self.upstream.list_page(rel, page_token)
        if not isinstance(page, dict):
            raise ProviderRuntimeError("internal", "139 上游 list_page 回包不是对象")
        raw = page.get("entries") or []
        if not isinstance(raw, list):
            raise ProviderRuntimeError("internal", "139 上游 entries 不是列表")
        entries = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "")
            if not name:
                continue
            entries.append({
                "name": name,
                "path": str(item.get("path") or (
                    f"{rel.rstrip('/')}/{name}" if rel != "/" else f"/{name}")),
                "is_dir": bool(item.get("is_dir")),
                "size": int(item.get("size") or 0),
                "file_id": str(item.get("file_id") or ""),
                "entry_id": str(item.get("entry_id") or ""),
            })
        return {"path": rel, "entries": entries,
                "next_page_token": page.get("next_page_token"),
                "total": page.get("total", len(entries))}

    def stat(self, session, path: str) -> dict:
        self._need_upstream()
        rel = "/" + (path or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        result = self.upstream.stat(rel)
        if not isinstance(result, dict) or not result.get("name"):
            if isinstance(result, dict) and result.get("file_id") is not None:
                result = dict(result)
                result.setdefault("path", rel)
                return result
            raise ProviderRuntimeError("not_found", f"139 上找不到：{rel}")
        result = dict(result)
        result.setdefault("path", rel)
        return result

    def open_range(self, session, path: str, byte_offset: int, byte_length) -> dict:
        self._need_upstream()
        offset = int(byte_offset or 0)
        if offset < 0 or (byte_length is not None and int(byte_length) <= 0):
            raise ProviderRuntimeError(
                "invalid_request",
                f"无效的取段范围：offset={offset} length={byte_length}")
        data = self.upstream.open_range(
            "/" + (path or "").lstrip("/"),
            offset, None if byte_length is None else int(byte_length))
        return {"data_b64": encode_bytes(data), "byte_offset": offset,
                "byte_length": len(data)}

    def resolve_display_asset(self, session, path: str, byte_offset: int,
                              byte_length) -> dict:
        """展示资产直接复用 open_range（契约口径，不另起取图路径）。"""
        return self.open_range(session, path, byte_offset, byte_length)

    def resolve_play_target(self, session, path: str) -> dict:
        self._need_upstream()
        rel = "/" + (path or "").lstrip("/")
        target = self.upstream.resolve_play_target(rel)
        return self._clean_target(target, rel)

    def health(self, session) -> dict:
        return {"ok": True, "provider": MOUNT_YUN139, "protocol": PROTOCOL,
                "capabilities": list(BASE_CAPABILITIES),
                "extensions": list(YUN139_EXTENSIONS)}

    # ---- 139 扩展 ----

    def call_extension(self, session, name: str, params: dict) -> dict:
        params = params or {}
        if name == "yun139.resolve_play_variants":
            self._need_upstream()
            variants = self.upstream.resolve_play_variants(
                "/" + (params.get("path") or "").lstrip("/"),
                provider_file_id=params.get("provider_file_id"))
            return {"variants": [v.public_payload() for v in variants]}
        if name == "yun139.resolve_play_target":
            self._need_upstream()
            rel = "/" + (params.get("path") or "").lstrip("/")
            variant_id = params.get("variant_id")
            if variant_id and hasattr(self.upstream, "resolve_play_variants"):
                variants = self.upstream.resolve_play_variants(
                    rel, provider_file_id=params.get("provider_file_id"))
                try:
                    select_variant(variants, str(variant_id))
                except Yun139VariantNotFound as exc:
                    raise ProviderRuntimeError("not_found", str(exc)) from exc
            target = self.upstream.resolve_play_target(
                rel, provider_file_id=params.get("provider_file_id"),
                variant_id=variant_id,
                original_transfer=bool(params.get("original_transfer")),
                original_direct=bool(params.get("original_direct")))
            return self._clean_target(target, rel)
        if name == "yun139.create_outer_link_transfer":
            task = self.transfers.create(
                str(params.get("share_ref") or ""),
                str(params.get("provider_file_id") or ""))
            return self.transfers.as_dict(task)
        if name == "yun139.transfer_status":
            try:
                task = self.transfers.get(str(params.get("transfer_id") or ""))
            except MountError as exc:
                raise ProviderRuntimeError("not_found", str(exc)) from exc
            return self.transfers.as_dict(task)
        raise ProviderRuntimeError("not_implemented", f"未实现的 139 扩展：{name}")

    # ---- 内务 ----

    def _need_upstream(self) -> None:
        if self.upstream is None:
            raise ProviderRuntimeError("not_implemented", "139 上游未配置")

    @staticmethod
    def _clean_target(target, rel: str) -> dict:
        """换链回包清洗：只留 url 与非秘密头（契约：不回 Cookie）。"""
        if not isinstance(target, dict) or not target.get("url"):
            raise ProviderRuntimeError("not_found", f"139 未返回播放地址：{rel}")
        headers = {k: v for k, v in dict(target.get("headers") or {}).items()
                   if k not in ("Cookie", "Authorization")}
        return {"url": target["url"], "headers": headers}


# ---------------------------------------------------------------- 宿主侧提供者（复用 SidecarMountProvider 的四口翻译）

class Yun139Mount(SidecarMountProvider):
    """移动云盘 139 宿主 provider（sidecar 形态，唯一形态）。

    复用项（不另起）：传输/握手/会话/分页/能力映射（``manifest_to_capabilities``）
    全在 ``SidecarMountProvider``；本类只定 139 的三样——provider 名、
    扩展表、会话数据，外加变体/转存两个扩展口与旁挂限长。

    门控合成说明：父类的门是 115 专用的 ``PAN115_SIDECAR_ENABLED``，
    139 不吃它——本类先验自己的 ``YUN139_ENABLED``（默认关），再把
    显式构造的 ``ProviderRuntimeClient`` 交给父类（父类 client 非空时
    跳过它自己的门）。生产路径的 client 由本类构造，不是测试注入。
    """

    provider = MOUNT_YUN139
    required_extensions = YUN139_EXTENSIONS
    mount_type = MOUNT_YUN139

    def __init__(self, mount, db=None, library=None, client=None,
                 socket_path: str | None = None, session_id: str | None = None):
        if client is None and not yun139_enabled():
            raise MountError(
                "139 挂载未启用（YUN139_ENABLED=1 开启；默认关，"
                "关时不许静默当成空库）")
        # 会话数据在父类 __init__ 里就要用（握手后立即 session_upsert），
        # 故先于 super().__init__ 从挂载配置里解析出来。
        cfg = mount_lib.parse_config(mount)
        self._space = (cfg.get("space") or "personal")
        self._pool_id = cfg.get("pool_id")
        if client is None:
            client = ProviderRuntimeClient(MOUNT_YUN139, socket_path)
        super().__init__(mount, db, library, client=client,
                         socket_path=socket_path, session_id=session_id)

    def _session_data(self) -> dict:
        # 只交挂载配置（空间/池），不交任何凭据原文（见模块头假设 8）。
        return {"space": self._space, "pool_id": self._pool_id}

    def _cookie(self) -> str:
        # 139 没有宿主侧 Cookie：父类的 Cookie 重贴路径对 139 不适用，
        # 本类覆写了 resolve_play_target 故永不走这条；万一被调到，
        # 响亮报错而不回空串（空 Cookie 头贴出去比报错更坏）。
        raise MountError("139 无 Cookie 凭据：换链回包不含 Cookie，宿主不重贴")

    # ---- 换链（基础口 + 变体扩展口；变体上下文只走扩展，不改基础签名） ----

    def resolve_play_target(self, rel: str, variant_id: Optional[str] = None,
                            original_transfer: bool = False,
                            original_direct: bool = False) -> PlayTarget:
        rel = "/" + (rel or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        try:
            if variant_id is not None or original_transfer or original_direct:
                params: dict[str, Any] = {
                    "session_id": self.session_id, "path": rel,
                    "original_transfer": bool(original_transfer),
                    "original_direct": bool(original_direct),
                }
                if variant_id is not None:
                    params["variant_id"] = variant_id
                file_id = self._file_id_of(rel)
                if file_id:
                    params["provider_file_id"] = file_id
                result = self.client.call_extension(
                    "yun139.resolve_play_target", params)
            else:
                result = self.client.resolve_play_target(self.session_id, rel)
        except ProviderRuntimeError as exc:
            raise MountError(f"139 换链失败：{exc}") from exc
        url = (result or {}).get("url") or ""
        if not url:
            raise MountError(f"139 未返回播放地址：{rel}")
        headers = {k: v for k, v in dict((result or {}).get("headers") or {}).items()
                   if k not in ("Cookie", "Authorization")}
        return PlayTarget("url", url, headers)

    # ---- 变体解析口（本 PR 的变体解析交付物；不接线到播放选路） ----

    def resolve_play_variants(self, rel: str) -> list[Yun139Variant]:
        rel = "/" + (rel or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        params: dict[str, Any] = {"session_id": self.session_id, "path": rel}
        file_id = self._file_id_of(rel)
        if file_id:
            params["provider_file_id"] = file_id
        try:
            result = self.client.call_extension(
                "yun139.resolve_play_variants", params)
        except ProviderRuntimeError as exc:
            raise MountError(f"139 变体解析失败：{exc}") from exc
        raw = (result or {}).get("variants") or []
        if not isinstance(raw, list):
            raise MountError("139 变体回包 variants 不是列表")
        return [parse_variant_payload(item) for item in raw
                if isinstance(item, dict)]

    # ---- 转存口（经 sidecar 注册表；执行层待实测，见模块头假设 3） ----

    def request_transfer(self, share_ref: str = "",
                         provider_file_id: str = "") -> dict:
        try:
            return self.client.call_extension(
                "yun139.create_outer_link_transfer",
                {"session_id": self.session_id, "share_ref": share_ref,
                 "provider_file_id": provider_file_id})
        except ProviderRuntimeError as exc:
            raise MountError(f"139 转存创建失败：{exc}") from exc

    def transfer_status(self, transfer_id: str) -> dict:
        try:
            return self.client.call_extension(
                "yun139.transfer_status",
                {"session_id": self.session_id, "transfer_id": transfer_id})
        except ProviderRuntimeError as exc:
            raise MountError(f"139 转存查询失败：{exc}") from exc

    # ---- 旁挂（父类走 open_range 全量取；139 加一道限长，防大文件整段读） ----

    def read_text(self, rel: str) -> str:
        rel = "/" + (rel or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        limit = sidecar_text_max_bytes()
        try:
            size = int((self.stat(rel) or {}).get("size") or 0)
        except MountError:
            size = 0
        if size and size > limit:
            raise MountError(f"139 旁挂文本过大（{size} 字节 > 上限 {limit}），不整段读")
        return super().read_text(rel)

    # ---- 内务 ----

    def _file_id_of(self, rel: str) -> str:
        """相对路径 → provider 文件 id（stat 优先，父目录列举回落；拿不到回空串）。"""
        try:
            file_id = str((self.stat(rel) or {}).get("file_id") or "")
            if file_id:
                return file_id
        except MountError:
            pass
        try:
            parent, _, name = rel.rpartition("/")
            for entry in self.list_dir(parent or "/"):
                if entry.name == name:
                    return entry.file_id
        except MountError:
            pass
        return ""


# ---- 139 扩展登记 + 类型与提供者注册 ----

register_provider_extensions(MOUNT_YUN139, YUN139_EXTENSIONS)
mount_lib.register_mount_types([{
    "value": MOUNT_YUN139,
    "label": "移动云盘 139",
    "kind": "remote",
    "group": "cloud",
    "hint": "移动云盘 139 sidecar（独立进程 + UDS，协议 "
            "external-provider-runtime-v1）。默认关（YUN139_ENABLED=1 开启）；"
            "路径以 139:/ 开头。",
    "needs_path": False,
    "browse": True,
    "root_key": "",
    "fields": [
        {"key": "root", "label": "根目录", "placeholder": "/ = 盘根目录"},
        {"key": "space", "label": "空间", "type": "select",
         "options": [{"label": "个人空间（只有原画）", "value": "personal"},
                     {"label": "分享空间（原画 + 云转码）", "value": "share"}]},
        {"key": "pool_id", "label": "账号池 ID（可选）",
         "placeholder": "留空 = 不用账号池"},
    ],
}])
mount_lib.register_providers({MOUNT_YUN139: Yun139Mount})
