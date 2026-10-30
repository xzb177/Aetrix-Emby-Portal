# -*- coding: utf-8 -*-
"""转存变体机制（FMBY original_transfer 机制化，D1 播放源单独立列方案）。

一句话：转存变体不是第三种播放模式，是「换一个 file_id 再走现有
播放链」——把「别人盘里的文件」（全网共用公开池）变成「自己空间里
的独立副本」（自有额度池），播放仍走现行选路（直链优先、中继兜底，
2026-10-27 用户改令直链为主线）。

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
用户盘拷贝已做（2026-10-26 用户亲定“转存到用户绑定盘、先转存
再播”，见下）；D5 应急自动默认关；D6 转存目录名默认
``aetrix-transfer``，目录级排除 + 扫描器首段跳过双保险；D7 工人
放 worker 进程、单线程顺序、每轮最多 10 单；D8 巡检每天一次、
两次 stale 算死；D9 只论 Drive（115 线见本模块后半段，同 provider
内转存）；D10 账单面后置。

BYOS 口径（设计稿 §八底线，照搬）：用户副本只许播给 owner_ref
本人；服主副本不许给有生效中 drive 绑定的用户播（严格于设计稿
§八「否则服主副本」一句——两句冲突时取严格的一句，见 PR 描述）。
2026-10-26 用户亲定升级（覆盖“转存是加分项”旧口径）：绑定用户
首播只走自己绑定盘里的副本，无副本先转存、转完再播，绝不静默
回落共享 SA 中继；变体清单保留该条但呈“准备中”态（列出来但
点不了，见 play_variants.transfer_status）；转存失败报 409/503，
不静默改走别的路。门禁口见 ensure_user_copy_gate（video_stream
在选路之后、换线之前调用）。provider 错配（2026-10-26 20:54
用户亲定，推翻 20:46“保持放行”与 20:04“不跨 provider”两条）：
只绑了另一 provider 的用户也要转存、播绑定盘（绑哪播哪），见
本模块后半段“跨 provider 转存”；用户无任何绑定时仍 proceed
（拦无绑定用户是 #1341 绑定门禁的职责，本门禁不越权）。

失败总原则（设计稿 §六，2026-10-26 补充）：选路与信号处理永不
抛错进播放链（门禁函数内部异常 → error verdict，调用方报 503
fail-closed，绝不静默回落）；工人侧 copy 403/404 不重试、
429/5xx 最多 3 次指数退避、权限失败记 orphan 不删文件。转存后
目录核对（FMBY discover_transfer_target 口径，2026-10-26 全学补齐）：
copy 回执只作期望值，必须在目标目录里按名核到才登记；核不到记
failed/copy_unresolved，不重试、不回落源文件播放。Drive
用户任务两条路径：Path A 用户令牌直拷（anyone-reader 源）；Path B
SA 暂存中转（用户无权源：SA 拷暂存 → anyone-reader 分享给用户 →
用户令牌再拷进自己盘，成功后尽力删暂存）。Path C（SA 拷后转交
所有权）不实现：consumer 间转移须 prospective owner 显式同意
（权限 + 接受两步）且个人↔工作/学校账号互转被禁，Path B 已全面
占优（无同意、无转移配额）。115 线：同 provider 内服务端 copy，
新 fid + pickcode 播放，Cookie 失效报 409/503 不回落。

跨 provider 转存（2026-10-26 20:54 用户亲定“绑哪播哪”，20:56
亲定搬字节学 FMBY，20:57 亲定机制不自己新作）：绑定与条目
provider 不一致时，字节必经 worker 服务器中转（下一边传一边，
内存 O(段大小)，不整文件落盘、不整文件进内存；带宽代价见 PR
描述，不藏）。机制形状照 FMBY：异步任务＋轮询（复用本模块 job
状态机 pending/running/done/failed＋有界重试，不另起状态机）＋
转存后目标目录按名核对（复用全学两路核对逻辑，核不到记
failed/copy_unresolved）＋换链与搬字节分离（先解析源下载链、
再搬字节、最后用新文件 id 解析播放）＋变体选错硬报错（门禁
verdict 之外不回落）。搬字节按 FMBY open_range 口径 Range 分段
取：源侧 401/403 报凭据过期（终态 failed/auth_error）、其余报
远端不可用（有界重试）、搬运层不静默重试（中途失败整单重来，
v1 已知代价）；上传侧 403 等不按凭据过期算（可能是空间/配额），
走有界重试，见各执行器注释。两方向：
- Drive→115：SA 令牌按段取源字节 → 用户 115 Cookie 简单上传
  （115cli 0.3.4 simple_upload 口径：sampleinitupload 取 OSS 表单
  参数 → 表单 POST；上限 5 GiB，超限记 unsupported——多段上传
  需 initupload 加密协议（p115cipher ECDH-AES），未实测不硬编；
  秒传（initupload）同理未实现，v1 一律传全量字节）→ 列用户
  115 转存目录按名核对 → 登记 pan115:user-<uid> 行，播出走现有
  serve_115_copy（用户 Cookie 现签 302）。
- 115→Drive：服主侧 115 账号现解析源 pickcode（绝不用 EA 播放
  的共享 pickcode 缓存）→ 现取 download_url → 按段取字节 →
  用户 Drive 令牌可续传上传 → anyone-reader → 列用户 Drive
  转存目录按名核对 → 登记 userdrive:<uid> 行，播出走现有 Drive
  链（SA 轮换读 anyone-reader 副本）。
凭据口径（任务书原话，写死在本模块注释里）：目标写入一律只用
该用户自己的绑定凭据（Drive 令牌／115 Cookie），绝不用服主档；
源读取走既有共享池读路径（Drive 源→SA 轮换，与中继一致；115
源→服主侧挂载账号，与 EA 播放一致），且 115 源下载链必须现解析、
不写／不用共享 pickcode 缓存。源侧凭据失效（SA 401/403、服主
115 Cookie 失效）只记任务失败，绝不拿去落用户绑定的失败态；
只有用户自己绑定凭据失效时才落该绑定 expired（见执行器内注释）。

未决实测（设计稿 §十二，施工前须知）：Quota 网页信号的精确特征
未拿真实额度爆文件抓包复核，本版只认强信号（整段 200 +
text/html + 体内 Quota 标记）；跨盘 copy 权限语义的真实端到端
实测仍待补（本版 Path A/Path B 逻辑由离线 mock 覆盖，真实 Drive
copy、真实用户令牌、SA 暂存分享链均未实测，不许写成已实测）；
目标盘剩余空间未实查（热门预防立项前必须实查，本版应急量级小、
空间可忽略但仍记账 size_bytes）；115 copy/mkdir 请求形态与 copy
响应新 fid 字段待 115 测试账号实测（生产 pan115 绑定现为 0 条）。
"""

import asyncio
import os
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
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
COPY_IDLE_CLEANUP_INTERVAL_SEC = 3600  # 闲置 TTL 每小时独立一轮（2026-10-28 2 小时单：从每日巡检解耦）
STALE_STRIKES = 2  # 巡检两次 stale 算死
MAX_JOBS_PER_ROUND = 10
STUCK_RUNNING_SEC = 3600  # running 卡住超 1h 回 pending（旧路径兜底）


def _env_int(name, default):
    try:
        value = int(os.getenv(name, "") or "")
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# 可伸缩工人（2026-10-27，一劳永逸版，替代“把 60 秒改 5 秒轮询”）：
# 旧结构的问题不是轮询慢，而是单线程 + 全局按 id 排队 + running 卡
# 1 小时才打回 + 一人爆单挡所有人。把轮询改短只会让空转查库与
# Drive 调用翻倍，用户一多先崩的是 DB 与配额。所以新结构是：
# 事件唤醒（建任务即通知，零任务零负载）+ 认领式多工人（原子
# UPDATE 认领，租约续租，死了自动回队）+ 快慢双通道 + 按用户
# 公平限流 + 队列可观测；60 秒兜底扫保留，绝不改成 5 秒高频轮询。
# 2026-10-29 更新：搬字节路线关停后只剩同 provider 服务端操作一条
# 道（原"快慢双通道"的慢道=跨 provider 搬字节，已消灭）；轮次屏障
# 拆除（滑动窗口补认领，见 process_claimed_round）；并发与
# per-owner 上限走 SystemConfig（lane_concurrency /
# per_owner_inflight）。
# 2026-10-30 更新（用户亲点，推翻 #1487 关停）：跨 provider 搬字节
# 转存重开，恢复 2026-10-26 定版"绑哪播哪"——跨单与同 provider
# 单共用同一条执行道与滑动窗口，但单独限在途并发
# （cross_lane_concurrency，默认 2），慢单不许占满道；总开关
# cross_provider_transfer_enabled 关时完整回退 #1487 关停口径。
# file-only 搬字节后路仍关停（等 drive.readonly 验证恢复），不在
# 本次重开范围。
TRANSFER_WORKER_CONCURRENCY = _env_int("TRANSFER_WORKER_CONCURRENCY", 4)
TRANSFER_WORKER_PER_USER_MAX = _env_int("TRANSFER_WORKER_PER_USER_MAX", 2)
TRANSFER_WORKER_LEASE_SEC = _env_int("TRANSFER_WORKER_LEASE_SEC", 300)
TRANSFER_WORKER_SWEEP_SEC = _env_int("TRANSFER_WORKER_SWEEP_SEC", 60)
TRANSFER_WORKER_AUTH_PAUSE_SEC = _env_int(
    "TRANSFER_WORKER_AUTH_PAUSE_SEC", 900)
TRANSFER_WORKER_ALERT_QUEUE_DEPTH = _env_int(
    "TRANSFER_WORKER_ALERT_QUEUE_DEPTH", 200)
TRANSFER_WORKER_ALERT_WAIT_SEC = _env_int(
    "TRANSFER_WORKER_ALERT_WAIT_SEC", 300)
TRANSFER_WORKER_WAKE_CHANNEL = (
    os.getenv("TRANSFER_WORKER_WAKE_CHANNEL", "transfer:wake")
    or "transfer:wake")


def worker_concurrency():
    """并行执行器数（模块属性可 patch，测试用 monkeypatch 改属性）。"""
    return max(1, min(16, int(TRANSFER_WORKER_CONCURRENCY or 4)))


def worker_per_user_max():
    """每用户同时在途上限（running + 本轮已认领），默认 2。"""
    return max(1, int(TRANSFER_WORKER_PER_USER_MAX or 2))


# ---------------------------------------------------------------------------
# 单道调度配置（2026-10-29 队列极限优化 + 搬字节关停后的新口径）
#
# 搬字节路线取消后（跨 provider 不再执行、file-only 不再搬字节），
# 工人只剩一条道：同 provider 服务端操作（Drive files.copy、115
# copy/秒传，纯 API、不经服务器字节）。原"快慢双通道"的慢道随之
# 消灭；道内仍保留点播优先（playback 在前）与按用户公平。
# 两个上限走 SystemConfig（管理端可改、保存即生效），读取优先级：
# SystemConfig 行 > 环境变量（TRANSFER_WORKER_*，历史定格）> 内置
# 默认。读失败（无 db / 表不存在 / 值非法）一律 fail-safe 回退，
# 绝不因配置读取把工人打停。
# ---------------------------------------------------------------------------

#: 转存快道（唯一执行道）并发数 SystemConfig 键；默认 8：
#: 道内全是秒级服务端 API 操作，旧混合池默认 4 是给含搬字节慢单
#: 定的；点播触发的 copy 要实际无等待，并发给到 8，钳 [1, 32]。
KEY_TRANSFER_FAST_LANE_CONCURRENCY = "transfer_fast_lane_concurrency"
TRANSFER_FAST_LANE_DEFAULT = 8

#: 同一 owner 在道内同时在飞上限 SystemConfig 键；默认沿用现行
#: worker_per_user_max()（env TRANSFER_WORKER_PER_USER_MAX，默认
#: 2），钳 [1, 16]。公平靠的 cap+轮转，不是具体数值。
KEY_TRANSFER_PER_OWNER_INFLIGHT = "transfer_per_owner_inflight"


def _system_config_int(db, key):
    """SystemConfig 整数热读：无行/非法/异常一律返回 None（回退）。"""
    if db is None:
        return None
    try:
        from backend.integrations import store
        raw = store.get_value(db, key, "")
    except Exception:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def lane_concurrency(db=None):
    """执行道并发数：SystemConfig > TRANSFER_WORKER_CONCURRENCY
    显式 env > 默认 8，钳 [1, 32]。db 为 None 时只走 env/默认。"""
    configured = _system_config_int(db, KEY_TRANSFER_FAST_LANE_CONCURRENCY)
    if configured is not None:
        return max(1, min(32, configured))
    if (os.getenv("TRANSFER_WORKER_CONCURRENCY", "") or "").strip():
        return worker_concurrency()
    return TRANSFER_FAST_LANE_DEFAULT


def per_owner_inflight(db=None):
    """单 owner 道内在飞上限：SystemConfig > worker_per_user_max()
    （env/默认 2），钳 [1, 16]。"""
    configured = _system_config_int(db, KEY_TRANSFER_PER_OWNER_INFLIGHT)
    if configured is not None:
        return max(1, min(16, configured))
    return max(1, min(16, worker_per_user_max()))


# ---------------------------------------------------------------------------
# 点播当场先办配置（2026-10-29 用户亲点）：点播触发的同 provider
# 服务端 copy 不再必经工人队列——请求路径当场领取、当场执行，
# 办成即同一请求内放行；办不完才留给工人兜底。三键走
# SystemConfig（管理端可改、保存即生效），读失败一律 fail-safe
# 回默认，绝不因配置读取把点播打停。
# ---------------------------------------------------------------------------

#: 点播当场先办总开关 SystemConfig 键；默认开（用户亲令 + 部署
#: 默认开口径）。关 = 点播完整回退现行纯队列行为（门禁
#: preparing → 等流/409、工人队列办），当场路径一行都不走。
KEY_PLAY_INLINE_TRANSFER_ENABLED = "play_inline_transfer_enabled"
#: 当场执行时间预算（秒）SystemConfig 键；默认 20：同盘 copy
#: 实测约 2.9 秒（找目录 0.25 + 谷歌 copy 1.67 + 核对 0.24 +
#: 加权限 0.68），20 秒覆盖慢服务端 copy 与 115 目录核对绰绰
#: 有余；到点不取消执行（见 maybe_run_play_inline），请求先
#: 回 preparing。钳 [1, 120]，不许配成无限等。
KEY_PLAY_INLINE_TRANSFER_BUDGET_SEC = "play_inline_transfer_budget_sec"
PLAY_INLINE_TRANSFER_BUDGET_DEFAULT = 20
#: API 进程内当场执行并发上限 SystemConfig 键；默认 4（保守）：
#: 当场执行跑在 API 进程的线程里，上限是护主链路的闸——名额
#: 满的请求直接回 preparing 走队列，绝不在请求里排队等名额。
#: 钳 [1, 16]。
KEY_PLAY_INLINE_TRANSFER_CONCURRENCY = "play_inline_transfer_concurrency"
PLAY_INLINE_TRANSFER_CONCURRENCY_DEFAULT = 4


def _system_config_bool(db, key, default=True):
    """SystemConfig 布尔热读：无行/非法/异常一律回 default。

    与 playback_strategy 同类键同口径：0/false/no/off 视为关，
    1/true/yes/on 视为开，其余（空行/乱写）回 default，不猜。
    """
    if db is None:
        return default
    try:
        from backend.integrations import store
        raw = (store.get_value(db, key, "") or "").strip().lower()
    except Exception:
        return default
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return default


def play_inline_enabled(db=None):
    """点播当场先办总开关：SystemConfig 热读，默认开，读失败回默认开。"""
    return _system_config_bool(db, KEY_PLAY_INLINE_TRANSFER_ENABLED, True)


def play_inline_budget_sec(db=None):
    """当场执行时间预算（秒）：SystemConfig > 默认 20，钳 [1, 120]。"""
    configured = _system_config_int(db, KEY_PLAY_INLINE_TRANSFER_BUDGET_SEC)
    if configured is not None:
        return max(1, min(120, configured))
    return PLAY_INLINE_TRANSFER_BUDGET_DEFAULT


def play_inline_concurrency(db=None):
    """当场执行并发上限：SystemConfig > 默认 4，钳 [1, 16]。"""
    configured = _system_config_int(db, KEY_PLAY_INLINE_TRANSFER_CONCURRENCY)
    if configured is not None:
        return max(1, min(16, configured))
    return PLAY_INLINE_TRANSFER_CONCURRENCY_DEFAULT


# ---------------------------------------------------------------------------
# 跨 provider 转存配置（2026-10-30 用户亲点重开，推翻 2026-10-29
# #1487 关停；恢复 2026-10-26 20:54 定版“绑哪播哪”）
#
# 跨 provider 副本只能经服务器分段搬字节（Drive↔115 之间没有
# 服务端 copy），是分钟级慢单：与同 provider 共用同一条执行道
# 与滑动窗口，但单独限在途并发，防慢单占满道饿死同 provider
# 的点播 copy。两键走 SystemConfig（管理端可改、保存即生效），
# 读取口径与 lane_concurrency / per_owner_inflight 同款
# fail-safe：读不到/非法一律回默认，绝不因配置读取把工人打停。
# ---------------------------------------------------------------------------

#: 跨 provider 转存总开关 SystemConfig 键；默认开（用户 2026-10-30
#: 亲点重开）。关 = 完整回退 #1487 关停口径：建单拒绝
#: （cross_provider_relay_disabled）、门禁对跨单 failed 明示、
#: 工人在途跨单终态；同 provider 通道不受影响。
KEY_CROSS_PROVIDER_TRANSFER_ENABLED = "cross_provider_transfer_enabled"

#: 跨 provider 搬字节单独在途并发上限 SystemConfig 键；默认 2
#: （保守）：字节搬运吃带宽与连接，2 条慢单在途时其余道宽全留给
#: 同 provider 服务端 copy；钳 [1, 8]。
KEY_TRANSFER_CROSS_LANE_CONCURRENCY = "transfer_cross_lane_concurrency"
TRANSFER_CROSS_LANE_DEFAULT = 2


def cross_provider_enabled(db=None):
    """跨 provider 转存总开关：SystemConfig 热读，默认开，读失败回默认开。"""
    return _system_config_bool(db, KEY_CROSS_PROVIDER_TRANSFER_ENABLED, True)


def cross_lane_concurrency(db=None):
    """跨 provider 在途并发上限：SystemConfig > 默认 2，钳 [1, 8]。"""
    configured = _system_config_int(db, KEY_TRANSFER_CROSS_LANE_CONCURRENCY)
    if configured is not None:
        return max(1, min(8, configured))
    return TRANSFER_CROSS_LANE_DEFAULT


def transfer_config_payload(db):
    """管理端转存卡片回包：转存四键当前生效值（默认值已折算）。"""
    return {
        "cross_provider_transfer_enabled": cross_provider_enabled(db),
        "transfer_cross_lane_concurrency": cross_lane_concurrency(db),
        "transfer_fast_lane_concurrency": lane_concurrency(db),
        "transfer_per_owner_inflight": per_owner_inflight(db),
    }


def write_transfer_config(db, *, cross_provider_transfer_enabled=None,
                          transfer_cross_lane_concurrency=None,
                          transfer_fast_lane_concurrency=None,
                          transfer_per_owner_inflight=None):
    """管理端转存配置写入：走 config_service 的 transfer 家族校验。

    None = 该键不改。非法值抛 ConfigValueError（ValueError 子类，
    端点转 400），整批不落库。返回 transfer_config_payload。
    """
    values = {}
    if cross_provider_transfer_enabled is not None:
        values[KEY_CROSS_PROVIDER_TRANSFER_ENABLED] = (
            "true" if cross_provider_transfer_enabled else "false")
    if transfer_cross_lane_concurrency is not None:
        values[KEY_TRANSFER_CROSS_LANE_CONCURRENCY] = str(
            transfer_cross_lane_concurrency)
    if transfer_fast_lane_concurrency is not None:
        values[KEY_TRANSFER_FAST_LANE_CONCURRENCY] = str(
            transfer_fast_lane_concurrency)
    if transfer_per_owner_inflight is not None:
        values[KEY_TRANSFER_PER_OWNER_INFLIGHT] = str(
            transfer_per_owner_inflight)
    if values:
        from backend import config_service
        config_service.save_config(db, values, schema="transfer")
    return transfer_config_payload(db)


def worker_lease_sec():
    """认领租约时长（秒），默认 300；执行中由心跳续租。"""
    return max(60, int(TRANSFER_WORKER_LEASE_SEC or 300))


def worker_sweep_sec():
    """兜底扫间隔（秒），默认 60；事件唤醒失败时仍能跑，不许改 5 秒。"""
    return max(30, int(TRANSFER_WORKER_SWEEP_SEC or 60))


def worker_auth_pause_sec():
    """用户凭据死后暂停该用户队列的时长（秒），默认 900。"""
    return max(60, int(TRANSFER_WORKER_AUTH_PAUSE_SEC or 900))


# 极限优化（2026-10-27，用户亲批全做，含预转存）：
# 转存 2.86 秒的分段实测是：找目录约 0.25 秒、谷歌同盘 copy 约
# 1.67 秒、目录核对约 0.24 秒、加 anyone-reader 约 0.68 秒；copy
# 本身是谷歌服务端时间，压不下去。核对与加权限原想并行，但亲验
# 旧测试后否决（anyone 必须加在核到的 id 上、核不到绝不能补权限），
# 故转存提速只靠目录缓存，目标约 2.6 秒，不是 2.3 秒。端到端感知
# 约 24 秒的大头不在转存，在准备中态下播放器拿无签地址连探 404、
# 等它自己再查 PlaybackInfo——等流（§2）与预转存（§3）压的是这一段。
def transfer_folder_cache_ttl_sec() -> int:
    """每用户转存目录 id 缓存 TTL（秒），默认 3600。

    只缓存用户盘（UserDriveApiClient），SA 共享盘目录不缓存：
    共享盘目录删了重建的影响面大，且不在实测的 0.25 秒分段里。
    """
    try:
        value = int(os.getenv("TRANSFER_FOLDER_CACHE_TTL_SEC", "") or "")
    except (TypeError, ValueError):
        return 3600
    return value if value > 0 else 3600


def wait_stream_enabled() -> bool:
    """准备中态等流开关：默认开（关 = 回到旧行为，准备中直接 409）。"""
    return _env_on("TRANSFER_WAIT_STREAM_ENABLED", "1")


def wait_stream_max_sec() -> float:
    """等流最长等待（秒），默认 15，钳在 1–60 秒。"""
    try:
        value = float(os.getenv("TRANSFER_WAIT_STREAM_MAX_SEC", "") or "")
    except (TypeError, ValueError):
        return 15.0
    return min(60.0, max(1.0, value))


def wait_stream_poll_sec() -> float:
    """等流轮询间隔（秒），默认 0.2，钳在 0.05–1 秒。"""
    try:
        value = float(os.getenv("TRANSFER_WAIT_STREAM_POLL_MS", "") or "")
    except (TypeError, ValueError):
        return 0.2
    return min(1.0, max(0.05, value / 1000.0))


def prefetch_daily_max() -> int:
    """每用户每天预转存单数上限，默认 5；0 = 关预转存。"""
    try:
        value = int(os.getenv("TRANSFER_PREFETCH_DAILY_MAX", "") or "")
    except (TypeError, ValueError):
        return 5
    return value if value >= 0 else 5


# 每用户转存目录 id 缓存（进程内，工人进程内跨任务生效）：
# 键含绑定行 id + last_verified_at——重绑原地换凭据时
# last_verified_at 变成新时间，旧缓存键自然失效，不会把旧账号
# 的目录 id 用到新账号上；目录被删的情形由 Path A 的缓存重试
# 兜底（见 _run_drive_user_job），不是靠缓存永不过期硬扛。
_transfer_folder_cache = {}
_transfer_folder_cache_lock = threading.Lock()


def clear_transfer_folder_cache() -> None:
    """清掉全部目录缓存（测试用；生产不主动清，靠 TTL + 指纹）。"""
    with _transfer_folder_cache_lock:
        _transfer_folder_cache.clear()


def _folder_cache_key(binding, drive_id, dir_name):
    bid = getattr(binding, "id", None) if binding is not None else None
    lva = getattr(binding, "last_verified_at", None) \
        if binding is not None else None
    return (str(drive_id or ""), str(dir_name or ""), bid, str(lva))


# Drive 存在性正缓存的跨进程键（须与 api.py 的
# _DRIVE_EXISTS_OK_PREFIX 逐字一致；api 读、工人写）。
# 墓碑键同理（api 已有 _DRIVE_EXISTS_INVAL_PREFIX，工人写正缓存
# 时顺手清墓碑，防止“删过又重拷”被旧墓碑挡住）。
_DRIVE_EXISTS_OK_PREFIX = "aetrix:drive_exists:ok:"
_DRIVE_EXISTS_INVAL_PREFIX = "aetrix:drive_exists:inv:"


def publish_drive_exists_ok(file_id, ttl_sec=None) -> bool:
    """变体登记 ready 后，把该副本 fid 写进 Redis 正缓存。

    EA 的存在性检查是本进程字典 + Redis 墓碑，工人进程写不进
    EA 的字典；跨进程正缓存键是唯一能让第一条流省掉一次
    files.get（约 0.25 秒）的办法。墓碑优先于正缓存（api 侧先查
    墓碑），所以旧墓碑不会被正缓存绕过；这里写正缓存时清墓碑，
    是因为“刚登记 ready”本身就是文件存在的最新证据。
    Redis 不可用 / 键为空时返回 False，绝不抛错进工人。
    """
    fid = (file_id or "").strip()
    if not fid:
        return False
    try:
        ttl = ttl_sec
        if ttl is None:
            try:
                ttl = int(os.getenv("DRIVE_EXISTS_CACHE_TTL_SEC", "") or "")
            except (TypeError, ValueError):
                ttl = 600
        if ttl <= 0:
            return False
        from backend.database import get_redis
        rc = get_redis()
        if rc is None:
            return False
        rc.set(_DRIVE_EXISTS_OK_PREFIX + fid, "1", ex=int(ttl))
        rc.delete(_DRIVE_EXISTS_INVAL_PREFIX + fid)
        return True
    except Exception:  # noqa: BLE001 — 预填只是加速，失败不挡登记
        logger.debug("存在性正缓存预填失败", exc_info=True)
        return False


def _env_on(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or default).strip().lower() not in {
        "0", "false", "no", "off", ""}


def master_enabled() -> bool:
    """转存变体总闸，默认关。关时选路、触发、工人全部空转。"""
    return _env_on("TRANSFER_VARIANT_ENABLED", "0")


def auto_enabled() -> bool:
    """应急自动触发开关（D5 / ``TRANSFER_VARIANT_AUTO_ENABLED``），默认关。

    与 ``TRANSFER_VARIANT_QUOTA_AUTO_ENQUEUE`` 等价，见
    ``quota_auto_enqueue_enabled``：两个键都关时 Quota 强信号只记日志。
    """
    return _env_on("TRANSFER_VARIANT_AUTO_ENABLED", "0")


def quota_auto_enqueue_enabled() -> bool:
    """Quota 强信号是否自动入队。默认关。

    现网键 ``TRANSFER_VARIANT_AUTO_ENABLED``（D5）与方案键
    ``TRANSFER_VARIANT_QUOTA_AUTO_ENQUEUE`` 都默认关。任一显式打开，
    才在既有 24h 节流窗内入队一次。都关：只记信号，播放端返回稳定
    409，不入队，也不换共享 SA / Cookie。
    """
    return auto_enabled() or _env_on(
        "TRANSFER_VARIANT_QUOTA_AUTO_ENQUEUE", "0")


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

    由中继嗅探抛给 video_stream：调用方记信号，并按
    ``quota_auto_enqueue_enabled`` 决定是否入队。绑定用户不换共享
    SA、不改播共享原件，返回稳定 409/503（见
    ``quota_playback_decision``）。未绑定的已下发签名仍走旧回落
    （302 开着走 302，否则 502）。不是 HTTPException，其它中继调用方
    不会误把它当普通上游失败。
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


USERCOPY_QUOTA_DETAIL = "你的谷歌盘今日下载额度已满，约 24 小时后恢复"

USERCOPY_INTERSTITIAL_DETAIL = "你的谷歌盘副本被谷歌拦截页挡住，暂时无法直接播放，请稍后重试"


def usercopy_quota_probe_enabled() -> bool:
    """用户副本额度探测开关（TRANSFER_USERCOPY_QUOTA_PROBE_ENABLED），默认开。

    关掉即回到探测前行为：直接下发用户副本直链（额度页时播放器黑屏）。
    """
    return _env_on("TRANSFER_USERCOPY_QUOTA_PROBE_ENABLED", "1")


def _usercopy_quota_probe_ttl() -> int:
    try:
        ttl = int((os.getenv("TRANSFER_USERCOPY_QUOTA_PROBE_TTL_SEC", "300")
                   or "300").strip())
    except (TypeError, ValueError):
        ttl = 300
    return ttl if ttl > 0 else 300



_PROBE_CLIENT = None
_PROBE_CLIENT_LOCK = threading.Lock()


def _shared_probe_client():
    """额度探测两腿共享的同步 httpx 客户端（2026-10-30 提速）。

    旧口径两腿各走 ``httpx.stream`` 顶层调用：每腿新建客户端、
    新建连接，TLS 每发重付（VPS 到谷歌实测单次握手约
    0.11–0.16s；稳态 PlaybackInfo 808ms 几乎全是这一发新鲜
    probe）。共享只动连接层：判定逻辑、两腿形态、缓存口径
    （#1495 裁决零缓存）一字不变；httpx.Client 本身线程安全，
    stream 上下文退出只关响应、连接归还池中。timeout 与
    follow_redirects 与旧顶层调用逐项一致（3.0s / True）。
    """
    global _PROBE_CLIENT
    client = _PROBE_CLIENT
    if client is None or client.is_closed:
        with _PROBE_CLIENT_LOCK:
            client = _PROBE_CLIENT
            if client is None or client.is_closed:
                client = httpx.Client(
                    timeout=3.0, follow_redirects=True,
                    limits=httpx.Limits(max_connections=8,
                                        max_keepalive_connections=4))
                _PROBE_CLIENT = client
    return client



def _probe_read_head(resp, limit):
    """读响应前段字节（上限 limit），读满即停——绝不整段下载。

    唯一的例外：响应带 Content-Length 且已读字节已覆盖它（额度页
    等小体积响应才走得到），多探一口确认 EOF，连接才能归还共享
    池复用；大文件永远走不到这一口（已读 << 总长），提前关响应
    的旧行为一字不差。
    """
    head = b""
    it = iter(resp.iter_bytes())
    for chunk in it:
        head += chunk
        if len(head) >= limit:
            total = resp.headers.get("content-length")
            if total is not None:
                try:
                    done = int(total) <= len(head)
                except (TypeError, ValueError):
                    done = False
                if done:
                    try:
                        next(it)
                    except StopIteration:
                        pass
            break
    return head


def _usercopy_probe_http(url):
    """额度探测第一腿 HTTP 入口（测试打桩用）：开放式 Range 形态。

    返回 (status, content_type, head_bytes)，请求带 ``Range: bytes=0-``。
    这条形态能抓到一部分额度页，但**不**与播放器真实请求同口径：
    2026-10-30 生产实证（variant 627）——同一副本 Range 形态两次回
    206 视频、整段 GET / HEAD 却回 200 额度页，播放器吃额度页判死
    而本腿放行。故只作第一腿：判 ok 前必须再过整拉形态第二腿
    （``_usercopy_probe_http_get``）。只读响应头 + 前段字节后立刻
    关响应，不下整段。不带任何凭据。
    """
    with _shared_probe_client().stream(
            "GET", url, headers={"Range": "bytes=0-"}) as resp:
        ctype = resp.headers.get("content-type", "")
        limit = 4096 if ctype.lower().startswith("text/html") else 512
        head = _probe_read_head(resp, limit)
        return resp.status_code, ctype, head


_SCAN_PAGE_MARKERS = (b"virus scan", b"virusscan",
                      b"too large for google to scan",
                      b"cannot scan this file", b"can't scan this file")


def _html_page_kind(status, content_type, body):
    """下载地址回 200 + text/html 时的插页分类。

    返回 "quota"（额度页）/ "scan"（病毒扫描警告页）/ "html"（其它
    谷歌插页）；不是 200 HTML 时返回 None（不归插页）。下载地址回
    HTML 永远不是视频字节——凡命中即直链不可安全播；分类只决定
    判定名、日志与明示文案，不决定是否放行。
    """
    if status != 200:
        return None
    ctype = (content_type or "").lower()
    if not ctype.startswith("text/html"):
        return None
    if is_quota_page_bytes(status, content_type, body):
        return "quota"
    if isinstance(body, str):
        body = body.encode("utf-8", "ignore")
    text = (body or b"").lower()
    if any(m in text for m in _SCAN_PAGE_MARKERS):
        return "scan"
    return "html"


def _usercopy_probe_http_get(url):
    """额度探测第二腿 HTTP 入口（测试打桩用）：整拉形态。

    与播放器起播同形态：plain GET、不带 Range。只读响应头 + 前段
    字节（HTML 读 4096、其余 512）后立刻关响应，严禁整文件下载。
    返回 (status, content_type, head_bytes)。不带任何凭据。
    """
    with _shared_probe_client().stream("GET", url) as resp:
        ctype = resp.headers.get("content-type", "")
        limit = 4096 if ctype.lower().startswith("text/html") else 512
        head = _probe_read_head(resp, limit)
        return resp.status_code, ctype, head


def _usercopy_quota_cache_key(user_id, fid) -> str:
    return f"usercopy_quota:{user_id}:{fid}"


def usercopy_quota_probe_cached(user_id, fid) -> bool:
    """quota 探测缓存是否已命中（video_stream / PlaybackInfo 打日志用）。

    只读缓存、不触发探测：调用方必须在 probe 之前先问一次——probe
    跑完后缓存里就有了，再问恒为真，顺序不能反。缓存不可用按未命中。
    """
    fid = (fid or "").strip()
    if not fid:
        return False
    try:
        from backend.database import CacheManager
        return CacheManager.get(_usercopy_quota_cache_key(user_id, fid),
                                domain="playback") in (
                                    "ok", "quota", "interstitial")
    except Exception:  # noqa: BLE001 — 缓存不可用按未命中
        return False


def _usercopy_quota_detail_key(user_id, fid) -> str:
    return f"usercopy_quota_detail:{user_id}:{fid}"


def usercopy_quota_probe_detail(user_id, fid) -> dict:
    """只读探测明细（判定点打结构化日志用），不触发探测。

    明细随判定以同 TTL 写在判定缓存旁：shape=range（第一腿 Range
    形态）/ get（第二腿整拉形态），page=quota / scan / html。无记录
    或缓存不可用返回 {}。永不抛异常。
    """
    fid = (fid or "").strip()
    if not fid:
        return {}
    try:
        from backend.database import CacheManager
        raw = CacheManager.get(_usercopy_quota_detail_key(user_id, fid),
                               domain="playback")
    except Exception:  # noqa: BLE001 — 明细读不到不影响播放
        return {}
    if not raw or not isinstance(raw, str):
        return {}
    shape, _, page = raw.partition(":")
    detail = {}
    if shape:
        detail["shape"] = shape
    if page:
        detail["page"] = page
    return detail


def probe_user_copy_quota(user_id, fid) -> str:
    """探测用户副本免凭证直链是否可被播放器安全拉流。

    两腿形态（2026-10-30 事故修订）：第一腿开放 Range 判额度页强
    信号；第一腿放行时再跑第二腿整拉形态（plain GET、无 Range、
    与播放器起播同形态）——Range 回 206 视频不代表整拉可播（生产
    实证：同一副本 Range 206、整拉 / HEAD 回额度页，播放器吃页
    判死）。第二腿回 HTML 插页只记信号：额度页记 page=quota，
    其它插页（病毒扫描警告页等）记 page=scan / html。

    注意：返回值是出法裁决口径，只认 Range 腿——Range 腿判 quota
    才返回 "quota"（判定点据此改道中继 / 115 / 409）；Range 腿健康
    时恒返回 "ok"，哪怕第二腿探到额度页/插页（2026-10-30 01:40
    证据修订，用户「直链优先、中继第二位」定版；与 #1490 契约对
    齐：已下发地址的探测判定恒为 ok）。GET 腿信号只写进 detail
    （shape=get、page=quota / scan / html），判定点按 detail 打信
    号日志、不改变出法。专门钉这条规矩的回归测试见
    tests/test_usercopy_probe_get_shape.py 的
    test_rule_direct_first_relay_second_*）。

    返回 "ok" / "quota" / "error"（探测本身失败）。
    ok / quota / interstitial 按 (user, fid) 短缓存（默认 300 秒，
    可调；判定形态与插页分类同 TTL 存明细缓存），error 不缓存。
    只请求用户本人副本的 usercontent 地址：不带任何令牌、不碰共享
    原件、不碰 SA。永不抛异常。开关关时恒返回 "ok"。

    改道判定（PlaybackInfo / video_stream）不许直接调本函数吃
    缓存裁决，必须走 probe_user_copy_quota_decision（先失效缓存
    再探测）：缓存里的 quota 可能是几分钟前谷歌间歇性额度页留
    下的，同一 fid 此刻 Range 已回 206 真字节——2026-10-30
    08:29 事故正是缓存 quota（TTL 300 秒）把 user 1 反复改道
    中继、播放器 8 次重连播不出，而当场 Range 实测 206。
    """
    if not usercopy_quota_probe_enabled():
        return "ok"
    fid = (fid or "").strip()
    if not fid:
        return "error"
    key = _usercopy_quota_cache_key(user_id, fid)
    try:
        from backend.database import CacheManager
        cached = CacheManager.get(key, domain="playback")
        if cached in ("ok", "quota", "interstitial"):
            return cached
    except Exception:  # noqa: BLE001 — 缓存不可用按未命中
        CacheManager = None  # type: ignore[assignment]
    try:
        from backend.emby_server.drive_auth import drive_usercontent_download_url
        url = drive_usercontent_download_url(fid)
        status, ctype, head = _usercopy_probe_http(url)
    except Exception:  # noqa: BLE001 — 探测失败不阻断播放
        logger.info("用户副本额度探测失败 user=%s fid=%s，照常下发",
                    user_id, fid, exc_info=True)
        return "error"
    verdict = None
    shape = None
    page = None
    if is_quota_page_bytes(status, ctype, head):
        verdict, shape, page = "quota", "range", "quota"
    elif status not in (200, 206):
        # 404/403/5xx 不归额度：交给后续既有存在性/错误口径处理。
        return "error"
    if verdict is None:
        # 第二腿（整拉形态）：Range 放行只证明分段可拉，播放器起播
        # 的整拉 / HEAD 形态可能撞插页，必须按播放器口径再判一次。
        try:
            g_status, g_ctype, g_head = _usercopy_probe_http_get(url)
        except Exception:  # noqa: BLE001 — 第二腿失败不推翻第一腿
            logger.info("用户副本整拉探测失败 user=%s fid=%s，"
                        "按 Range 形态放行", user_id, fid, exc_info=True)
            verdict, shape = "ok", "range"
        else:
            kind = _html_page_kind(g_status, g_ctype, g_head)
            if kind is not None:
                # GET 腿信号只进 detail：出法裁决口径下 Range 健康即
                # ok（与 #1490 契约对齐），绝不因整拉形态改 verdict。
                verdict, shape, page = "ok", "get", kind
            else:
                verdict, shape = "ok", "get"
    if verdict == "ok":
        try:
            publish_drive_exists_ok(fid)
        except Exception:  # noqa: BLE001
            pass
    try:
        if CacheManager is not None:
            CacheManager.set(key, verdict, _usercopy_quota_probe_ttl(),
                             domain="playback")
            CacheManager.set(
                _usercopy_quota_detail_key(user_id, fid),
                f"{shape or ''}:{page or ''}",
                _usercopy_quota_probe_ttl(), domain="playback")
    except Exception:  # noqa: BLE001
        pass
    return verdict


def probe_user_copy_quota_decision(user_id, fid) -> str:
    """改道判定专用入口（PlaybackInfo / video_stream 两判定点）。

    改道判定零缓存：先失效 (user, fid) 的裁决与明细缓存，再走
    probe_user_copy_quota——缓存被清后 probe 必然当场跑新鲜两腿，
    新结果照常回写缓存（非判定调用随后仍可复用）。本函数与 probe
    的区别只在缓存，不在判定逻辑：出法仍只认 Range 腿（#1492）。

    为什么判定不许吃缓存：缓存里的 quota 裁决可能是几分钟前
    谷歌间歇性额度页留下的（TTL 默认 300 秒），同一 fid 此刻
    Range 已回 206 真字节。2026-10-30 08:29 事故：user 1 播
    《大唐妖探》本人副本，日志连续 byos_usercopy_quota
    verdict=quota cached=True 反复改道中继、播放器 8 次重连播
    不出，同一时段实测该 fid 的 Range 已回 206。

    失效失败（缓存不可用）不阻断：吞掉异常照常探测——probe 对
    缓存异常本就按未命中处理，最坏退化为原缓存口径。永不抛异常
    的口径与 probe 一致（probe 本身永不抛）。
    """
    fid = (fid or "").strip()
    if fid:
        try:
            from backend.database import CacheManager
            CacheManager.delete(_usercopy_quota_cache_key(user_id, fid),
                                domain="playback")
            CacheManager.delete(_usercopy_quota_detail_key(user_id, fid),
                                domain="playback")
        except Exception:  # noqa: BLE001 — 失效失败不阻断判定
            pass
    return probe_user_copy_quota(user_id, fid)


def ready_user115_variant(db, item_id, user_id):
    """该用户该条目 ready 的 115 副本行（或 None）。额度回落只看本人 115。"""
    try:
        return _newest_ready_for_scheme(db, item_id, "user", str(user_id),
                                        "pan115:")
    except Exception:  # noqa: BLE001
        logger.warning("查 115 副本失败 item=%s user=%s", item_id, user_id,
                       exc_info=True)
        return None


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


class _CrossAuthError(_DriveError):
    """跨 provider 搬字节：源侧凭据失效（源下载 401/403）。

    终态 failed/auth_error，不重试。注意：失效的是源侧凭据（SA
    令牌／服主 115 Cookie），绝不能拿去落用户绑定的失败态——只有
    用户自己绑定凭据失效（用户 115 Cookie 401/403、用户 Drive
    令牌刷新失败）才落该绑定，见各执行器。
    """


class _CrossRemoteError(_DriveError):
    """跨 provider 搬字节：远端不可用（源下载非 401/403 失败、上传
    侧失败、网络异常）。有界重试（_schedule_retry），搬运层内部
    不静默重试——中途失败整单重来（v1 已知代价）。"""


# 跨 provider 搬字节分段大小（Range 取段与 Drive 可续传上传共用）
_CROSS_SEGMENT = 8 * 1024 * 1024
# 115 简单上传上限（115cli 0.3.4 MAX_SIMPLE_UPLOAD_SIZE = 5 GiB）
_115_SIMPLE_UPLOAD_LIMIT = 5 * 1024 * 1024 * 1024
# Drive 可续传上传端点（与 mount_drive.FILES_URL 的 API 域名一致）
_DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"


class _NotFound(_DriveError):
    pass


class _Forbidden(_DriveError):
    pass


class _Retriable(_DriveError):
    pass


class _CopyForbidden(_DriveError):
    """copy 403/404：原件已死或无权，不重试（设计稿 §六）。"""


class _CopyQuotaExceeded(_DriveError):
    """copy 403 且响应体判定为用户盘存储配额不足。

    刻意不继承 _CopyForbidden：配额不足不是“原件已死”，调用方
    绝不能把它当死文件走 Path B 或记 dead；它只走空间处理
    （先腾再重试一次，仍不足则明确失败，绝不静默回落共享）。
    未接住它的调用方会落入通用 except → _schedule_retry（有界
    重试），同样不是死文件口径。
    """


class _SpaceInsufficient(Exception):
    """用户盘空间不足（转存前实测或 copy 中途配额复核的终态）。

    属性 file_size / free_bytes / shortfall 必须齐全才算“报清
    三数”；about 未测到时 free_bytes / shortfall 为 None，消息
    里如实写“剩余空间未测到”，不编数字。调用方接住它一律
    _fail_job insufficient_space，绝不转 Path B、不转重试。
    """

    def __init__(self, file_size, free_bytes, shortfall,
                 evicted=None, bytes_freed=0):
        self.file_size = file_size
        self.free_bytes = free_bytes
        self.shortfall = shortfall
        self.evicted = list(evicted or [])
        self.bytes_freed = bytes_freed
        if file_size is None:
            detail = (f"用户盘空间不足：文件大小未知，"
                      f"copy 中途仍报存储配额不足（已腾 "
                      f"{len(self.evicted)} 条、{bytes_freed} 字节），"
                      f"未静默回落共享")
        elif free_bytes is None or shortfall is None:
            detail = (f"用户盘空间不足：文件 {file_size} 字节，"
                      f"剩余空间未测到（已腾 {len(self.evicted)} 条、"
                      f"{bytes_freed} 字节），未静默回落共享")
        else:
            detail = (f"用户盘空间不足：文件 {file_size} 字节，"
                      f"剩余 {free_bytes} 字节，还差 {shortfall} 字节"
                      f"（已腾 {len(self.evicted)} 条、"
                      f"{bytes_freed} 字节），未静默回落共享")
        super().__init__(detail)


def _is_storage_quota_response(resp):
    """判定一次 Drive 403 响应是不是存储配额不足。

    只认强信号，二者居一即算：
    - 响应体 error.errors[] 里 reason == storageQuotaExceeded；
    - 响应体 error.message 里含 storage quota（不区分大小写）。
    刻意排除下载配额：reason == downloadQuotaExceeded 或消息含
    download quota 时不算——下载配额是读侧限流，不是盘满了，
    腾地方对它无效。响应体解析失败一律不算（按旧口径走
    _CopyForbidden，不把不明 403 误判成盘满）。
    """
    try:
        data = resp.json() or {}
    except Exception:  # noqa: BLE001 — 解析失败 = 不是强信号
        return False
    if not isinstance(data, dict):
        return False
    err = data.get("error") or {}
    if not isinstance(err, dict):
        return False
    for entry in err.get("errors") or []:
        if not isinstance(entry, dict):
            continue
        reason = str(entry.get("reason") or "").strip().lower()
        if reason == "storagequotaexceeded":
            return True
        if reason == "downloadquotaexceeded":
            return False
    message = str(err.get("message") or "").lower()
    if "download quota" in message:
        return False
    return "storage quota" in message


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
    return _active_binding(db, user_id, "drive") is not None


def has_active_pan115_binding(db, user_id) -> bool:
    """用户是否有生效中的 115 绑定（只认 provider=pan115 且 active）。"""
    if user_id is None:
        return False
    return _active_binding(db, user_id, "pan115") is not None


def _active_binding(db, user_id, provider):
    """该用户该 provider 的生效中绑定行（最早建的一个），或 None。

    只取本人行，绝不跨用户；绑定行本身不携带凭据原文，凭据由
    各执行器按绑定行另行解封（Drive 走 drive_oauth、115 走
    upstream_sources.unseal_secret），本函数不碰凭据。
    """
    if user_id is None:
        return None
    return (db.query(em.UserStorageBinding)
            .filter(em.UserStorageBinding.user_id == user_id,
                    em.UserStorageBinding.provider == provider,
                    em.UserStorageBinding.status == "active")
            .order_by(em.UserStorageBinding.id)
            .first())


def _item_provider(db, item):
    """条目所属 provider：drive | pan115 | None（不在门禁范围）。

    只认两类路径：mount://（按挂载类型判定）与 115:/（115 直挂）。
    本地路径、rclone、byos:// 与其它形状一律返回 None——门禁对不
    认识的形状保持旧行为（proceed），绝不因判定不清拦播放。
    """
    path = (getattr(item, "file_path", "") or "").strip()
    if path.startswith("115:/"):
        return "pan115"
    if not path.startswith("mount://"):
        return None
    try:
        from backend.emby_server import mounts as mount_lib
        parsed = mount_lib.parse_mount_path(path)
    except Exception:
        return None
    if parsed is None:
        return None
    mount_id, _rel = parsed
    try:
        mount = (db.query(em.StorageMount)
                 .filter(em.StorageMount.id == mount_id)
                 .first())
    except Exception:
        return None
    if mount is None:
        return None
    if mount.mount_type == getattr(mount_lib, "MOUNT_PAN115", "115"):
        return "pan115"
    if mount.mount_type == getattr(mount_lib, "MOUNT_DRIVE", "drive"):
        return "drive"
    return None


def _source_drive_id(db, item, provider):
    """任务行的源盘标识（Drive：源共享盘 id；115：源挂载标记）。

    Drive 源盘取自 file_path 的 mount:// 挂载配置（drive:<盘 id>），
    取不到时回落空串（工人再按任务 drive_id 校验，不静默猜盘）。
    115 源挂载标记为 ``pan115:mount-<id>``，115:/ 直挂路径无挂载
    时为空串（115 工人不依赖 drive_id，靠条目 fid + 用户 Cookie）。
    """
    path = (getattr(item, "file_path", "") or "").strip()
    if provider == "pan115":
        if path.startswith("mount://"):
            try:
                from backend.emby_server import mounts as mount_lib
                parsed = mount_lib.parse_mount_path(path)
            except Exception:
                parsed = None
            if parsed is not None:
                return f"pan115:mount-{parsed[0]}"
        return ""
    if not path.startswith("mount://"):
        return ""
    try:
        from backend.emby_server import mounts as mount_lib
        parsed = mount_lib.parse_mount_path(path)
    except Exception:
        return ""
    if parsed is None:
        return ""
    try:
        mount = (db.query(em.StorageMount)
                 .filter(em.StorageMount.id == parsed[0])
                 .first())
    except Exception:
        return ""
    if mount is None:
        return ""
    mount_path = (getattr(mount, "path", "") or "").strip()
    if mount_path.startswith("drive:"):
        return mount_path[len("drive:"):].strip()
    return mount_path or ""


def create_user_job(db, item, user_id, provider, trigger="playback",
                    force=False):
    """给该用户建一个用户盘转存任务（门禁与 Quota 信号共用）。

    - 总闸关 → master_off；无条目 id → no_item_id；
    - 24h 内同归属桶已有任务 → throttled（不重复建）；
    - 115 且条目无源 fid → no_source_fid（不建任务：无 fid 无从
      copy，等扫描补 fid 后下次播放再建，不污染节流窗）；
    - Drive 且无源盘标识 → no_drive（不猜盘）。
    成功返回任务行（已提交）。调用方按 TransferError.kind 映射。
    force=True 仅供门禁对 failed-retriable 任务的重触发：24h 内
    已失败的任务不挡重建（否则重触发永远撞上自己留下的失败行，
    等于没修）；其它调用方一律不传，重触发预算由门禁侧把关。
    """
    if not master_enabled():
        raise TransferError("master_off", "转存变体总闸未开")
    item_id = getattr(item, "id", None)
    if item_id is None:
        raise TransferError("no_item_id", "条目无 id")
    owner_ref = str(user_id)
    if not force and _recent_job_exists(db, item_id, "user", owner_ref):
        raise TransferError("throttled", "24h 内该用户已有转存任务")
    orig_fid = (getattr(item, "drive_file_id", "") or "").strip()
    if provider == "pan115" and not orig_fid:
        raise TransferError("no_source_fid", "115 条目无源 fid，无法转存")
    source_drive_id = _source_drive_id(db, item, provider)
    if provider == "drive" and not source_drive_id:
        raise TransferError("no_drive", "Drive 条目取不到源盘标识")
    dest = (f"userdrive:{user_id}" if provider == "drive"
            else f"pan115:user-{user_id}")
    job = em.TransferJob(item_id=item_id, trigger=trigger,
                         drive_id=source_drive_id or None,
                         provider=provider, owner_scope="user",
                         owner_ref=owner_ref, dest_drive_id=dest,
                         status="pending")
    db.add(job)
    db.commit()
    notify_transfer_wakeup(job.id)
    return job


# ---------------------------------------------------------------------------
# 跨 provider 转存（2026-10-26 20:54 用户亲定“绑哪播哪”）
# ---------------------------------------------------------------------------

def _is_cross_job(job):
    """任务行是否跨 provider：provider=源，dest_drive_id=目标盘。

    - Drive 源任务 dest 以 pan115: 开头 → 落用户 115 盘；
    - 115 源任务 dest 以 userdrive: 开头 → 落用户 Drive 盘。
    同 provider 任务的 dest 与 provider 同侧（drive→userdrive:、
    pan115→pan115:），不会误判。任务行无需新列，provider 字段
    保留为源 provider（巡检/统计口径不变）。
    """
    provider = (getattr(job, "provider", "") or "").strip()
    dest = (getattr(job, "dest_drive_id", "") or "").strip()
    return ((provider == "drive" and dest.startswith("pan115:"))
            or (provider == "pan115" and dest.startswith("userdrive:")))


def _latest_job_for_dest(db, item_id, owner_scope, owner_ref, dest):
    """该条目该归属桶内、指定落盘的最新一行任务（或 None）。"""
    return (db.query(em.TransferJob)
            .filter(em.TransferJob.item_id == item_id,
                    em.TransferJob.owner_scope == owner_scope,
                    em.TransferJob.owner_ref == owner_ref,
                    em.TransferJob.dest_drive_id == dest)
            .order_by(em.TransferJob.id.desc())
            .first())


def _newest_ready_for_scheme(db, item_id, owner_scope, owner_ref, scheme):
    """该条目该归属下、指定落盘方案的最新 ready 行（或 None）。

    跨流程必须按 variant_drive_id 前缀挑行：双绑定用户先后有过
    同 provider 行与跨行时，通用 _newest_ready 可能挑到另一盘的
    行——播出路由按 variant_drive_id 前缀定（pan115:→serve_115_copy，
    userdrive:→proceed），挑错盘即播错盘。单绑定用户只有一种
    副本可能，但仍按方案过滤，不依赖“只有一种可能”的假设。
    """
    return (db.query(em.TransferVariant)
            .filter(em.TransferVariant.item_id == item_id,
                    em.TransferVariant.owner_scope == owner_scope,
                    em.TransferVariant.owner_ref == owner_ref,
                    em.TransferVariant.status == "ready",
                    em.TransferVariant.variant_drive_id.startswith(scheme))
            .order_by(em.TransferVariant.id.desc())
            .first())


#: 跨 provider 转存关闭时的稳定 error_kind（建单拒绝、门禁明示与
#: worker 在途兜底共用）：2026-10-29 #1487 关停时引入；2026-10-30
#: 重开后只在总开关 cross_provider_transfer_enabled 关时出现
#: （建单抛它、工人在途跨单以它终态），开关开时不再产生新行，
#: 但关停期的存量失败行仍带这个 kind，门禁跨分支对它单独分流。
CROSS_PROVIDER_DISABLED_KIND = "cross_provider_relay_disabled"
CROSS_PROVIDER_DISABLED_DETAIL = (
    "跨 provider 转存已停用（总开关关），"
    "只支持同 provider 转存")
#: file-only 绑定走不通服务端 copy 时的稳定 error_kind：file-only
#: 令牌看不见非应用自创文件，copy 永不可能成功；#1470 的搬字节
#: 后路随搬字节路线一并关停，失败明示等只读授权恢复。
FILE_ONLY_COPY_UNAVAILABLE_KIND = "file_only_copy_unavailable"
FILE_ONLY_COPY_UNAVAILABLE_DETAIL = (
    "file-only 授权无法服务端 copy 该文件（令牌看不见源文件），"
    "搬字节转存已停用；请等只读授权（drive.readonly）恢复后"
    "重新绑定再播")


def create_cross_provider_job(db, item, user_id, source_provider,
                              trigger="playback", force=False):
    """给该用户建一个跨 provider 转存任务（门禁共用）。

    校验与 create_user_job 同款（总闸、条目 id、24h 同归属桶节流、
    115 源须有 fid、Drive 源须有源盘标识），节流桶沿用 (item, user)
    单桶——同 provider 任务与跨任务互斥，同一用户同一条目 24h 内
    不会同时在途两单。provider=源 provider，dest_drive_id=目标盘
    （drive 源→pan115:user-<uid>，pan115 源→userdrive:<uid>）。
    force=True 仅供门禁对 failed-retriable 任务的重触发，语义同
    create_user_job（24h 内已失败的任务不挡重建）。

    2026-10-29 曾随搬字节路线一并关停（#1487），2026-10-30 用户
    亲点重开：恢复建单。总开关 cross_provider_transfer_enabled
    关时回到关停口径——不建单，抛 cross_provider_relay_disabled；
    全部既有调用方（门禁/预取/quota/重建）都已按 TransferError.kind
    映射成 failed/skipped，口径自洽。开关关时的存量 pending 跨单
    由 worker 执行入口兜底终态化（run_transfer_job_once），不静默
    吞掉。
    """
    if not cross_provider_enabled(db):
        raise TransferError(CROSS_PROVIDER_DISABLED_KIND,
                            CROSS_PROVIDER_DISABLED_DETAIL)
    if not master_enabled():
        raise TransferError("master_off", "转存变体总闸未开")
    item_id = getattr(item, "id", None)
    if item_id is None:
        raise TransferError("no_item_id", "条目无 id")
    owner_ref = str(user_id)
    if not force and _recent_job_exists(db, item_id, "user", owner_ref):
        raise TransferError("throttled", "24h 内该用户已有转存任务")
    orig_fid = (getattr(item, "drive_file_id", "") or "").strip()
    if source_provider == "pan115" and not orig_fid:
        raise TransferError("no_source_fid", "115 条目无源 fid，无法转存")
    source_drive_id = _source_drive_id(db, item, source_provider)
    if source_provider == "drive" and not source_drive_id:
        raise TransferError("no_drive", "Drive 条目取不到源盘标识")
    dest = (f"pan115:user-{user_id}" if source_provider == "drive"
            else f"userdrive:{user_id}")
    job = em.TransferJob(item_id=item_id, trigger=trigger,
                         drive_id=source_drive_id or None,
                         provider=source_provider, owner_scope="user",
                         owner_ref=owner_ref, dest_drive_id=dest,
                         status="pending")
    db.add(job)
    db.commit()
    notify_transfer_wakeup(job.id)
    return job


# 门禁 verdict 与调用方映射（video_stream / playback_info 共用语义）：
# - proceed：放行（走现有播放链；Drive 就绪副本已由选路解析到）。
# - preparing：转存进行中或节流中，播放端呈“转存准备中”（409）。
# - failed：转存已死/失败，不走共享播放（409/503）。
# - error：门禁内部异常，fail-closed 报 503，绝不静默回落。
# - serve_115_copy：115 用户副本已就绪，调用方须用用户 Cookie +
#   副本 pickcode 现签 302（绝不走共享 115 链），variant 行随 dict 回。
#
# 绑定用户可以 proceed 共享原件的原因只有下面这些明示放行。
# 其余 proceed（含 no_drive_file / no_item_id）一律改成 failed，
# 调用方再按 409 挡掉，不许静默播共享原件。
SHARED_PROCEED_REASONS = frozenset({
    "master_off", "no_user", "play_sign_consume", "byos_item",
    "unknown_provider", "no_matching_binding",
})

# Quota 强信号 / 门禁挡共享原件时返回给客户端的稳定文案。
QUOTA_COPY_PENDING_DETAIL = "请稍候，正在拷到你的盘"
QUOTA_AUTO_OFF_DETAIL = (
    "共享盘额度已满，应急自动入队关着，本次不入队、不改播共享原件")
QUOTA_READY_RETRY_DETAIL = (
    "额度页已命中，不改播共享原件。你的盘里已有副本，请重试播放")
QUOTA_UNAVAILABLE_DETAIL = (
    "共享盘额度已满，未能入队转存，不改播共享原件")
QUOTA_NO_SHARED_DETAIL = "共享盘额度已满，不改播共享原件"
SHARED_BLOCKED_DETAIL = "转存未就绪，不走共享播放"

#: provider 人话名（门禁跨 provider 文案用）。
_PROVIDER_LABELS = {"drive": "谷歌盘", "pan115": "115 网盘"}


def _provider_label(provider):
    return _PROVIDER_LABELS.get((provider or "").strip(), provider or "?")


def _cross_preparing_detail(source_provider, dest_provider):
    """跨 provider 准备中文案：说清片在哪边、往哪边转、备好再播。"""
    return (f"这部片存放在{_provider_label(source_provider)}，"
            f"你绑定的是{_provider_label(dest_provider)}："
            f"正在转存到你的{_provider_label(dest_provider)}，"
            "转存完成后即可播放")


def _cross_failed_detail(dest_provider, job):
    """跨 provider 失败文案：带任务行上的人话原因，不只给笼统失败。"""
    reason = (getattr(job, "error_detail", "") or "").strip()
    if reason:
        return f"转存到你的{_provider_label(dest_provider)}失败：{reason}"
    return (f"转存到你的{_provider_label(dest_provider)}失败，"
            "不走共享播放：请稍后重试或联系管理员")

_TERMINAL_JOB_KINDS = {
    "dead", "auth_error", "unsupported", "retriable", "copy_unresolved",
    "orphan", "item_gone", "no_drive",
    # 2026-10-29 搬字节关停新增终态：门禁见存量失败行直接判
    # failed 明示，不落 preparing/throttled 误导用户继续等。
    # 2026-10-30 跨 provider 重开后，cross_provider_relay_disabled
    # 已摘除：它只可能出现在关停期的存量跨单失败行上，门禁跨
    # 分支对它单独分流（开关开→预算内重触发新单、关→failed
    # 明示），不再靠本集合直判；同 provider 分支对它另有遗留行
    # 放行守卫（见门禁实现），不会误伤同 provider 失败行。
    # file_only_copy_unavailable 保留：file-only 搬字节后路仍
    # 关停（等 drive.readonly 验证恢复），其失败行继续直判
    # failed，不许落 preparing 让用户空等。
    "file_only_copy_unavailable",
}


def log_byos(event: str, **fields) -> None:
    """方案 2 结构化日志。

    事件名只认 ``byos_gate_block`` / ``byos_enqueue`` /
    ``byos_quota_force`` / ``byos_ready_play`` / ``byos_usercopy_quota`` /
    ``byos_usercopy_interstitial`` / ``byos_usercopy_quota_relay``。
    日志失败不影响播放。
    """
    try:
        extras = " ".join(
            f"{key}={value}" for key, value in fields.items()
            if value is not None)
        logger.info("[byos] %s %s", event, extras)
    except Exception:  # noqa: BLE001 — 观测失败不能挡播放
        pass


def _observe_gate(result, item, user) -> None:
    """门禁出口打点：挡住 / 入队 / 就绪播出。proceed 明示放行不打。"""
    verdict = (result or {}).get("verdict")
    reason = (result or {}).get("reason")
    fields = {
        "item": getattr(item, "id", None),
        "user": getattr(user, "id", None),
        "verdict": verdict,
        "reason": reason,
        "job_id": (result or {}).get("job_id"),
    }
    if reason == "job_created":
        log_byos("byos_enqueue", **fields)
    if verdict in ("preparing", "failed", "error"):
        log_byos("byos_gate_block", **fields)
    elif verdict == "serve_115_copy" or reason == "has_ready_variant":
        log_byos("byos_ready_play", **fields)


def bound_shared_play_blocked(gate, *, has_binding, play_variant) -> bool:
    """绑定用户没有本人 ready 副本时，不许静默播共享原件。

    明示放行（总闸关、已下发签名、无绑定、byos 条目等）返回 False。
    preparing / failed / error / serve_115_copy 由调用方按原状态码
    处理，这里不重复挡。``has_ready_variant`` 但调用方没换成用户
    副本 fid 时仍返回 True。
    """
    if not has_binding:
        return False
    if (play_variant is not None
            and getattr(play_variant, "owner_scope", None) == "user"):
        return False
    verdict = (gate or {}).get("verdict")
    reason = (gate or {}).get("reason")
    if verdict in ("preparing", "failed", "error", "serve_115_copy"):
        return False
    if verdict == "proceed" and reason in SHARED_PROCEED_REASONS:
        return False
    return True


def prefer_user_variant(play_fid, play_variant, gate):
    """门禁确认本人 ready 副本时，播出 fid 以门禁行为准。

    ``resolve_play_file_id`` 查询异常会回共享原件。门禁已经拿到
    用户副本行时，不许沿用那个原件。
    """
    variant = (gate or {}).get("variant")
    if ((gate or {}).get("verdict") == "proceed"
            and (gate or {}).get("reason") == "has_ready_variant"
            and variant is not None
            and getattr(variant, "owner_scope", None) == "user"):
        fid = (getattr(variant, "variant_file_id", "") or "").strip()
        if fid:
            return fid, variant
    return play_fid, play_variant


def ensure_user_copy_public(db, user_id, file_id) -> bool:
    """PlaybackInfo 发用户副本直链前确保 anyone-reader（自愈存量副本）。

    只用该用户本人的生效中 Drive 绑定令牌，给该用户盘上的副本授
    anyone-reader；本次授成（ok）与已授过（already）都算成功。无绑定、
    令牌失效、授权限失败一律返回 False（调用方回落 alt=media+头
    旧链路），永不抛异常。调用方只传用户副本行（owner_scope=user
    且 variant_drive_id 以 userdrive: 开头），绝不用于共享源文件。
    """
    try:
        fid = (file_id or "").strip()
        if not fid or user_id is None:
            return False
        binding = _active_binding(db, user_id, "drive")
        if binding is None:
            return False
        if _copy_public_cache_valid(db, user_id, fid):
            return True
        from backend.emby_server import drive_oauth as _do
        from backend.emby_server import drive_share as _ds
        token = _do.get_binding_drive_access_token(db, binding)
        if not token:
            return False
        ok = _ds.ensure_shared(fid, token) in ("ok", "already")
        if ok:
            _copy_public_cache_put(user_id, fid)
        return ok
    except Exception:
        logger.warning("用户副本 anyone-reader 自愈失败 user=%s",
                       user_id, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# 起播去重（2026-10-29）：已确认公开的副本不再每次 permissions.create
# ---------------------------------------------------------------------------
# 生产实测每次 PlaybackInfo 都打一次 permissions.create（300-400ms），
# 即使副本早已 anyone-reader。缓存值是“确认公开的时间”。命中后仍回库
# 核对，任一不满足即视为失效、走原逻辑重新授权：
# - 该用户名下该 fid 的 Drive 副本行存在且 status == "ready"
#   （LRU / 闲置清理 / 额度信号标 deleted/stale/released 即失效）；
# - share_revoked_at 为空，或早于缓存写入时间（解绑 / 令牌失效 / 闲置
#   收回都会写 share_revoked_at=now，晚于缓存即失效）。
# 这样不依赖逐个失效写点，Redis 不可用（缓存退回本进程内存）时同样
# 正确；收回点另外主动删键，只是让下次少一次回库。核对只读本地库，
# 不打谷歌。

def copy_public_cache_ttl() -> int:
    """已公开缓存 TTL（秒），COPY_PUBLIC_CACHE_TTL_SEC，默认 6h，0 = 关。"""
    raw = (os.getenv("COPY_PUBLIC_CACHE_TTL_SEC", "") or "").strip()
    if not raw:
        return 6 * 3600
    try:
        val = int(raw)
    except (TypeError, ValueError):
        return 6 * 3600
    return val if val >= 0 else 6 * 3600


def _copy_public_key(user_id, fid) -> str:
    return f"usercopy_public:{user_id}:{fid}"


def _copy_public_cache_put(user_id, fid) -> None:
    ttl = copy_public_cache_ttl()
    if ttl <= 0:
        return
    try:
        from backend.database import CacheManager
        CacheManager.set(_copy_public_key(user_id, fid),
                         datetime.now().isoformat(), ttl,
                         domain="playback")
    except Exception:  # noqa: BLE001 — 缓存只是加速
        logger.debug("已公开缓存写入失败", exc_info=True)


def copy_public_cache_drop(user_id, fid) -> None:
    """收回 / 删除副本时主动删键（正确性由命中核对兜底，这里只是提速）。"""
    try:
        from backend.database import CacheManager
        CacheManager.delete(_copy_public_key(user_id, fid),
                            domain="playback")
    except Exception:  # noqa: BLE001
        logger.debug("已公开缓存删除失败", exc_info=True)


def _copy_public_cache_valid(db, user_id, fid) -> bool:
    """缓存命中且回库核对通过 → True；任何异常 / 不确定 → False（走原逻辑）。"""
    if copy_public_cache_ttl() <= 0:
        return False
    try:
        from backend.database import CacheManager
        raw = CacheManager.get(_copy_public_key(user_id, fid),
                               domain="playback")
        if not raw:
            return False
        cached_at = datetime.fromisoformat(str(raw))
        rows = (db.query(em.TransferVariant)
                .filter(em.TransferVariant.owner_scope == "user",
                        em.TransferVariant.owner_ref == str(user_id),
                        em.TransferVariant.variant_drive_id.like(
                            "userdrive:%"),
                        em.TransferVariant.variant_file_id == fid)
                .all())
        if not rows:
            return False
        for row in rows:
            if getattr(row, "status", None) != "ready":
                return False
            revoked = getattr(row, "share_revoked_at", None)
            if revoked is not None and revoked >= cached_at:
                return False
        return True
    except Exception:  # noqa: BLE001
        logger.debug("已公开缓存核对失败，按未命中", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# 防外流三件套（2026-10-28）：解绑收权限 + 闲置 TTL + 发放可查。
# 不碰中继、不碰 #1405 门禁、不改变体默认顺序、不改共享文件权限。
# ---------------------------------------------------------------------------

def copy_idle_revoke_hours() -> int:
    """闲置收权限阈值（小时）：副本最后活动超此小时数未动即收 anyone-reader。

    走 env COPY_IDLE_REVOKE_HOURS，默认 2；非法/<=0 回落默认。
    旧 COPY_IDLE_REVOKE_DAYS 已废弃（整天数表达不了 2 小时，且 0 会
    回落默认 30 天，填 0/小数冒充 2 小时必错），新代码不再读取。
    """
    return _env_int("COPY_IDLE_REVOKE_HOURS", 2)


def copy_idle_delete_hours() -> int:
    """闲置删文件阈值（小时）：副本最后活动超此小时数未动即删文件 + 变体行。

    走 env COPY_IDLE_DELETE_HOURS，默认 2；非法/<=0 回落默认。
    删除 pass 只在持有可用绑定令牌时执行（无令牌删不了文件，
    行留待重绑后处理；链接本身已由解绑/收回 pass 断掉，不外流）。
    旧 COPY_IDLE_DELETE_DAYS 已废弃，新代码不再读取。
    """
    return _env_int("COPY_IDLE_DELETE_HOURS", 2)


# ---------------------------------------------------------------------------
# 安全收口（2026-10-30）：服主副本 TTL 开关 + 死令牌待收回登记/执行
# + Path B 暂存残留登记/扫荡。
# ---------------------------------------------------------------------------

#: 服主副本闲置 TTL 开关（SystemConfig 键，非 env；管理端/运维改
#: system_configs 行即生效）。
KEY_SOURCE_COPY_IDLE_CLEANUP = "transfer_source_copy_idle_cleanup_enabled"


def source_copy_idle_cleanup_enabled(db) -> bool:
    """服主副本纳入闲置 TTL 的开关（默认开）。

    读法与 user_bindings.pan115_bind_vip_check_enabled 同款
    （store 热读 + fail-safe 回默认开）：收口是安全动作，读不到
    配置不该悄悄关掉。
    """
    try:
        from backend.integrations import store
        raw = (store.get_value(
            db, KEY_SOURCE_COPY_IDLE_CLEANUP, "true") or "").strip().lower()
    except Exception:  # noqa: BLE001 - 测试桩/异常 DB 场景下保默认
        logger.warning(
            "transfer_source_copy_idle_cleanup_enabled 读取失败，回默认开",
            exc_info=True)
        return True
    return raw not in ("0", "false", "no", "off")


def mark_share_revoke_pending(db, user_id):
    """死令牌待收回登记：把该用户名下尚未收回（share_revoked_at
    为空）且未登记的用户副本行标 share_revoke_pending_at=now。

    调用点是凭据死亡钩子（drive_oauth._apply_refresh_failure）：
    死亡时能收的已先收（成功行 share_revoked_at 非空、自动
    跳过），剩下的就是令牌已死收不到、或逐行失败的行。副本行
    没有绑定 id 列，登记只能按用户粒度——它只是义务清单，真正
    执行时仍由令牌可见性决定能收哪几行，不扩大作用域。
    不提交、不抛异常，返回登记行数。
    """
    now = datetime.now()
    count = 0
    try:
        for row in _user_copy_rows(db, user_id):
            if getattr(row, "share_revoked_at", None) is None and \
                    getattr(row, "share_revoke_pending_at", None) is None:
                row.share_revoke_pending_at = now
                count += 1
    except Exception:  # noqa: BLE001 — 登记失败不许拖垮调用方
        logger.warning("待收回登记失败 user=%s", user_id, exc_info=True)
    return count


def pending_share_revoke_row_ids(db, user_id):
    """该用户待收回登记在案的行 id（重绑成功时优先补收用）。"""
    try:
        return [row.id for row in _user_copy_rows(db, user_id)
                if getattr(row, "share_revoke_pending_at", None) is not None]
    except Exception:  # noqa: BLE001
        logger.warning("待收回查行失败 user=%s", user_id, exc_info=True)
        return []


def run_pending_share_revokes(db, limit_users=20):
    """死令牌待收回兜底执行：逐个处理有待收回登记的用户。

    凭该用户全部 active Drive 绑定的令牌试收（revoke 本职口径），
    收成功的行在 revoke 内同步清登记。无 active 绑定的用户整批
    跳过：无令牌可执行，登记保留——已知残留（用户解绑后不再
    重绑时，那批副本只能等其在 Google 侧自行撤权；登记行留在
    库里可盘点，不静默丢弃）。不提交、不抛异常。返回 stats。
    """
    stats = {"users": 0, "revoked": 0, "errors": 0}
    try:
        rows = db.query(em.TransferVariant).filter(
            em.TransferVariant.owner_scope == "user",
            em.TransferVariant.share_revoke_pending_at.isnot(None)).all()
    except Exception:  # noqa: BLE001 — 查库失败整轮记 error
        logger.warning("待收回兜底查行失败", exc_info=True)
        stats["errors"] = 1
        return stats
    by_user = {}
    for row in rows:
        by_user.setdefault(row.owner_ref, []).append(row.id)
    for owner_ref, row_ids in list(by_user.items())[:limit_users]:
        try:
            user_id = int(owner_ref)
        except (TypeError, ValueError):
            stats["errors"] += len(row_ids)
            continue
        stats["users"] += 1
        sub = revoke_user_copy_sharing(db, user_id, row_ids=row_ids)
        stats["revoked"] += sub["revoked"] + sub["already_clean"] \
            + sub["gone"]
        stats["errors"] += sub["errors"]
    return stats


def _register_staging_leftover(db, job, item, uid, tmp_fid):
    """Path B 暂存残留登记：暂存文件删不掉时落一行 orphan 登记。

    与暂存分享失败的既有登记同形（owner_scope='user'、
    variant_drive_id=源盘 id、status='orphan'）：清理周期的
    暂存扫荡按此形重试删除、成功删行销账。同 fid 已有登记时
    不重复落行。本函数只 add 不提交（调用方统一提交，与本
    模块既有 orphan 登记同口径）。
    """
    exists = db.query(em.TransferVariant).filter(
        em.TransferVariant.variant_file_id == tmp_fid,
        em.TransferVariant.status == "orphan").first()
    if exists is not None:
        return exists
    row = em.TransferVariant(
        item_id=job.item_id, source_file_id=(item.drive_file_id or "").strip(),
        variant_file_id=tmp_fid, variant_drive_id=job.drive_id,
        owner_scope="user", owner_ref=uid, status="orphan",
        size_bytes=_to_int(getattr(item, "size", None)))
    db.add(row)
    return row


def sweep_staging_leftovers(db, drive_api=None, limit=100):
    """暂存扫荡（清理周期内调）：重试删除登记在案的暂存残留。

    作用域（硬边界）：status='orphan'、owner_scope='user'、
    variant_drive_id 既非 userdrive: 也非 pan115: 前缀的行——
    即文件落在共享源盘、可凭 SA 令牌删除的 Path B 暂存文件
    （分享失败与成功后删不掉两种来源同形）。用户盘上的 orphan
    行不在此列（归闲置 TTL 的删除 pass 按绑定令牌处理）。
    删成功/文件已不存在 → 删行销账；失败 → 留行下轮再试。
    不提交、不抛异常。返回 stats。
    """
    stats = {"checked": 0, "deleted": 0, "errors": 0}
    try:
        rows = db.query(em.TransferVariant).filter(
            em.TransferVariant.status == "orphan",
            em.TransferVariant.owner_scope == "user",
            em.TransferVariant.variant_drive_id.isnot(None),
            ~em.TransferVariant.variant_drive_id.like("userdrive:%"),
            ~em.TransferVariant.variant_drive_id.like("pan115:%"),
        ).order_by(em.TransferVariant.created_at.asc(),
                   em.TransferVariant.id.asc()).limit(limit).all()
    except Exception:  # noqa: BLE001 — 查库失败整轮记 error
        logger.warning("暂存扫荡查行失败", exc_info=True)
        stats["errors"] = 1
        return stats
    stats["checked"] = len(rows)
    if not rows:
        return stats
    client = drive_api or DriveApiClient()
    for row in rows:
        fid = (row.variant_file_id or "").strip()
        if not fid or any(c in fid for c in (" ", "/")):
            stats["errors"] += 1
            continue
        try:
            client.delete_file_ignore_missing(row.variant_drive_id, fid)
        except Exception:  # noqa: BLE001 — 失败留行下轮再试
            logger.warning("暂存扫荡删文件失败 fid=%s", fid,
                           exc_info=True)
            stats["errors"] += 1
            continue
        db.delete(row)
        stats["deleted"] += 1
    return stats


def _stored_drive_token(db, binding):
    """读某绑定行已存未过期的 access_token（不刷新、不重试刷新）。

    供 invalid 钩子用：刷新失败正是调用原因，任何刷新尝试都会
    递归回 _apply_refresh_failure。令牌缺失/已过期（留 60 秒余量）/
    解不开一律返回 None（调用方按“无可用令牌”跳过，不阻塞）。
    """
    try:
        from backend.emby_server import drive_oauth as _do
        _cred, token = _do._drive_credential_of(db, binding)
        tok = (token or {}).get("access_token")
        exp = int((token or {}).get("expires_at") or 0)
        if tok and exp > int(time.time()) + 60:
            return tok
    except Exception:  # noqa: BLE001 — 令牌读不到 = 无可用令牌
        return None
    return None


def _user_copy_rows(db, user_id, row_ids=None):
    """某用户名下全部 Drive 副本行（收回作用域，硬边界）。

    只认 owner_scope='user'、owner_ref=str(user_id)、
    variant_drive_id 以 'userdrive:' 开头的行；共享源文件、115 行、
    别人名下的行一律不在结果里。status 不过滤：stale 行的文件
    可能还在（stale 只是两次巡检没读到）、released 行的文件也在
    用户盘里，链接同样可转发，都要收。
    """
    q = db.query(em.TransferVariant).filter(
        em.TransferVariant.owner_scope == "user",
        em.TransferVariant.owner_ref == str(user_id),
        em.TransferVariant.variant_drive_id.like("userdrive:%"))
    if row_ids is not None:
        q = q.filter(em.TransferVariant.id.in_(list(row_ids)))
    return q.order_by(em.TransferVariant.id).all()


def revoke_user_copy_sharing(db, user_id, binding=None, row_ids=None,
                             allow_refresh=True):
    """收回某用户名下 Drive 副本的 anyone-reader（只收权限）。

    文件不动、变体行不删、不改行状态；重绑后 PlaybackInfo 自愈
    再授的路径已存在，本函数不另起。成功（revoked / already_clean /
    gone）时把行 share_revoked_at 置为 now（审计用：最后一次收回
    时间）；失败的行不置标记，下次再试。

    令牌与客户端（作用域 = 令牌可见性）：
    - binding 给定时只用该绑定的令牌（解绑钩子：只收该账号下能
      看见的文件；其它账号的副本不动——用户仍保有那些绑定，动了
      会断其正常播放，虽自愈可重授但属无谓 churn）；
    - binding 为空时用该用户全部 active Drive 绑定的令牌逐个试
      （TTL 工人：副本可能分属多个绑定账号）；
    - allow_refresh=False 时只用已存未过期令牌（invalid 钩子专用，
      绝不刷新，否则递归回刷新失败路径）；无可用令牌时整批跳过。

    本函数不提交：调用方在流程末尾统一 commit（解绑单事务、工人
    循环、invalid 钩子的调用方均已提交）。永不抛异常：任何失败
    只记 stats.errors + warning，绝不阻塞解绑/工人/刷新失败处理。
    """
    stats = {"total": 0, "revoked": 0, "already_clean": 0, "gone": 0,
             "errors": 0}
    try:
        rows = _user_copy_rows(db, user_id, row_ids)
    except Exception:  # noqa: BLE001 — 查库失败 = 整批记 error
        logger.warning("收回副本分享查行失败 user=%s", user_id,
                       exc_info=True)
        stats["errors"] = 1
        return stats
    stats["total"] = len(rows)
    if not rows:
        return stats
    clients = []
    try:
        if binding is not None:
            if allow_refresh:
                clients = [UserDriveApiClient(db, binding)]
            else:
                token = _stored_drive_token(db, binding)
                if token:
                    clients = [_RawTokenClient(token)]
        elif allow_refresh:
            bindings = db.query(em.UserStorageBinding).filter(
                em.UserStorageBinding.user_id == user_id,
                em.UserStorageBinding.provider == "drive",
                em.UserStorageBinding.status == "active").order_by(
                    em.UserStorageBinding.id).all()
            clients = [UserDriveApiClient(db, b) for b in bindings]
    except Exception:  # noqa: BLE001 — 客户端建不出来 = 整批记 error
        logger.warning("收回副本分享建客户端失败 user=%s", user_id,
                       exc_info=True)
        stats["errors"] = len(rows)
        return stats
    if not clients:
        logger.warning("收回副本分享无可用令牌 user=%s（跳过 %d 行）",
                       user_id, len(rows))
        stats["errors"] = len(rows)
        return stats
    now = datetime.now()
    marker = f"myDrive:{user_id}"
    for row in rows:
        fid = (row.variant_file_id or "").strip()
        if not fid or any(c in fid for c in (" ", "/")):
            stats["errors"] += 1
            continue
        # 收回动作发起前先删缓存：并发 PlaybackInfo 不得再把该 fid
        # 当成已公开；即使远端收回失败，命中核对也会按原逻辑重新确认。
        copy_public_cache_drop(user_id, fid)
        outcome = None
        for client in clients:
            try:
                outcome = client.remove_anyone_reader(marker, fid)
                break
            except Exception:  # noqa: BLE001 — 换下一个令牌再试
                logger.warning("收回副本分享失败 user=%s fid=%s",
                               user_id, fid, exc_info=True)
        if outcome in ("revoked", "already_clean", "gone"):
            stats[outcome] += 1
            row.share_revoked_at = now
            # 待收回义务已履行：同步清登记（安全收口 2026-10-30）。
            row.share_revoke_pending_at = None
        else:
            stats["errors"] += 1
    return stats


def _copy_last_activity_map(db, rows):
    """批量算各副本行的最后活动时间（闲置 TTL 口径，三者取新者）。

    - 变体行 last_played_at（本单起：发放 usercontent 直链时维护）；
    - UserMediaData.last_played_at（该用户该条目的播放进度上报）；
    - 变体行 created_at（从未播过的新副本不算闲置）。
    发放≠实际出画面、进度不上报的播放器会使信号偏旧——取新者
    只能缓解，不能根除（见 PR 报告未知项）。
    """
    user_ids = set()
    for row in rows:
        try:
            user_ids.add(int(row.owner_ref))
        except (TypeError, ValueError):
            pass
    umd_last = {}
    if user_ids:
        for umd in db.query(em.UserMediaData).filter(
                em.UserMediaData.user_id.in_(sorted(user_ids))).all():
            key = (umd.user_id, umd.item_id)
            prev = umd_last.get(key)
            if umd.last_played_at and (prev is None
                                       or umd.last_played_at > prev):
                umd_last[key] = umd.last_played_at
    result = {}
    for row in rows:
        candidates = [row.created_at, row.last_played_at]
        try:
            key = (int(row.owner_ref), row.item_id)
            candidates.append(umd_last.get(key))
        except (TypeError, ValueError):
            pass
        present = [c for c in candidates if c is not None]
        result[row.id] = max(present) if present else None
    return result


def copy_idle_cleanup(db, now=None, limit=100):
    """闲置 TTL 清理（工人每小时独立一轮，有界、不阻塞播放主链路）。

    闲置按整小时取整（1 小时 59 分算 1 小时，不触发；2 小时整触发）。
    顺序：先删除 pass（闲置≥DELETE_HOURS：删文件 + 变体行），再收回
    pass（闲置≥REVOKE_HOURS：只收权限），避免同一轮对同一行又收
    又删。两阈值默认同为 2 小时：此时收回 pass 只处理删除 pass 未
    删掉的行（如删文件失败、fid 非法等记 error 后仍保留的行）；
    无绑定行两 pass 均记 skipped、不记 error。两 pass 每轮各最多
    处理 ``limit`` 行（按 created_at 升序最旧优先），避免首轮对
    存量老副本的雷霆清理。
    删除 pass 须持有可用绑定令牌：无 active 绑定的用户整批跳过
    （行留待重绑后处理；链接已由解绑收回断掉，不构成外流）。
    本函数末尾统一提交；永不抛异常（工人循环外层另有兜底）。
    """
    stats = {"checked": 0, "revoked": 0, "deleted": 0,
             "skipped_no_binding": 0, "errors": 0,
             "source_checked": 0, "source_revoked": 0,
             "source_deleted": 0, "pending_revoked": 0,
             "staging_checked": 0, "staging_deleted": 0}
    now = now or datetime.now()
    revoke_hours = copy_idle_revoke_hours()
    delete_hours = copy_idle_delete_hours()
    try:
        candidates = db.query(em.TransferVariant).filter(
            em.TransferVariant.owner_scope == "user",
            em.TransferVariant.variant_drive_id.like("userdrive:%"),
            em.TransferVariant.status.in_(
                ("ready", "stale", "orphan", "released"))).order_by(
                    em.TransferVariant.created_at.asc(),
                    em.TransferVariant.id.asc()).limit(limit).all()
        last_map = _copy_last_activity_map(db, candidates)
    except Exception:  # noqa: BLE001 — 查库失败整轮记 error
        logger.warning("闲置 TTL 清理查行失败", exc_info=True)
        stats["errors"] = 1
        return stats
    stats["checked"] = len(candidates)

    def _idle_hours(row):
        last = last_map.get(row.id)
        if last is None:
            return 0
        hours = int((now - last).total_seconds() // 3600)
        return hours if hours > 0 else 0

    # 删除 pass（先行：删掉的行不再进收回 pass）。
    deleted_ids = set()
    delete_rows = [r for r in candidates if _idle_hours(r) >= delete_hours]
    by_user = {}
    for row in delete_rows:
        by_user.setdefault(row.owner_ref, []).append(row)
    for owner_ref, rows in by_user.items():
        try:
            user_id = int(owner_ref)
        except (TypeError, ValueError):
            stats["errors"] += len(rows)
            continue
        try:
            bindings = db.query(em.UserStorageBinding).filter(
                em.UserStorageBinding.user_id == user_id,
                em.UserStorageBinding.provider == "drive",
                em.UserStorageBinding.status == "active").order_by(
                    em.UserStorageBinding.id).all()
            clients = [UserDriveApiClient(db, b) for b in bindings]
        except Exception:  # noqa: BLE001
            logger.warning("闲置 TTL 删除 pass 建客户端失败 user=%s",
                           user_id, exc_info=True)
            stats["errors"] += len(rows)
            continue
        if not clients:
            stats["skipped_no_binding"] += len(rows)
            continue
        marker = f"myDrive:{user_id}"
        for row in rows:
            fid = (row.variant_file_id or "").strip()
            if not fid or any(c in fid for c in (" ", "/")):
                stats["errors"] += 1
                continue
            ok = False
            for client in clients:
                try:
                    client.delete_file_ignore_missing(marker, fid)
                    ok = True
                    break
                except Exception:  # noqa: BLE001 — 换下一个令牌再试
                    logger.warning("闲置 TTL 删文件失败 user=%s fid=%s",
                                   user_id, fid, exc_info=True)
            if ok:
                db.delete(row)
                deleted_ids.add(row.id)
                stats["deleted"] += 1
            else:
                stats["errors"] += 1

    # 收回 pass（只处理未被本轮删掉、且闲置≥阈值的行）。
    # 无 active 绑定的用户直接记 skipped_no_binding（不记 error）：
    # 其链接已由解绑收回断掉，每天重复记 error 只是噪音。
    revoke_rows = [r for r in candidates
                   if r.id not in deleted_ids
                   and _idle_hours(r) >= revoke_hours]
    revoke_by_user = {}
    for row in revoke_rows:
        revoke_by_user.setdefault(row.owner_ref, []).append(row)
    for owner_ref, rows in revoke_by_user.items():
        try:
            user_id = int(owner_ref)
        except (TypeError, ValueError):
            stats["errors"] += len(rows)
            continue
        try:
            has_binding = db.query(em.UserStorageBinding).filter(
                em.UserStorageBinding.user_id == user_id,
                em.UserStorageBinding.provider == "drive",
                em.UserStorageBinding.status == "active").first() \
                is not None
        except Exception:  # noqa: BLE001
            has_binding = False
        if not has_binding:
            stats["skipped_no_binding"] += len(rows)
            continue
        sub = revoke_user_copy_sharing(
            db, user_id, row_ids=[r.id for r in rows])
        stats["revoked"] += sub["revoked"] + sub["already_clean"] \
            + sub["gone"]
        stats["errors"] += sub["errors"]

    # 服主副本 pass（2026-10-30 安全收口）：owner_scope='source'
    # 的 ready 行纳入同一套闲置口径（先删后收，与用户行同序），
    # 凭 SA 令牌执行（DriveApiClient 本职）。最后活动 = max（行
    # last_played_at、该条目任一用户的 UserMediaData
    # last_played_at、行 created_at）——任一用户近期播过该条目
    # 即视为不闲置；被清掉的服主副本需要时按现行转存链路重建。
    # 开关 transfer_source_copy_idle_cleanup_enabled 默认开。
    if source_copy_idle_cleanup_enabled(db):
        source_rows = []
        try:
            source_rows = db.query(em.TransferVariant).filter(
                em.TransferVariant.owner_scope == "source",
                em.TransferVariant.status == "ready").order_by(
                    em.TransferVariant.created_at.asc(),
                    em.TransferVariant.id.asc()).limit(limit).all()
        except Exception:  # noqa: BLE001 — 查行失败整轮记 error
            logger.warning("闲置 TTL 清理查服主行失败", exc_info=True)
            stats["errors"] += 1
        stats["source_checked"] = len(source_rows)
        if source_rows:
            umd_max = {}
            try:
                item_ids = sorted({r.item_id for r in source_rows})
                for umd in db.query(em.UserMediaData).filter(
                        em.UserMediaData.item_id.in_(item_ids)).all():
                    if umd.last_played_at and (
                            umd.item_id not in umd_max
                            or umd.last_played_at > umd_max[umd.item_id]):
                        umd_max[umd.item_id] = umd.last_played_at
            except Exception:  # noqa: BLE001 — 活动信号查不到按行自身时间
                logger.warning("闲置 TTL 清理查服主行活动失败",
                               exc_info=True)
            sa_client = None
            for row in source_rows:
                present = [c for c in (row.created_at,
                                       row.last_played_at,
                                       umd_max.get(row.item_id))
                           if c is not None]
                if not present:
                    continue
                idle_h = int((now - max(present)).total_seconds() // 3600)
                if idle_h < revoke_hours:
                    continue
                fid = (row.variant_file_id or "").strip()
                drive_id = (row.variant_drive_id or "").strip()
                if not fid or not drive_id or \
                        any(c in fid for c in (" ", "/")):
                    stats["errors"] += 1
                    continue
                if sa_client is None:
                    sa_client = DriveApiClient()
                if idle_h >= delete_hours:
                    try:
                        sa_client.delete_file_ignore_missing(drive_id, fid)
                    except Exception:  # noqa: BLE001
                        logger.warning("闲置 TTL 删服主副本失败 fid=%s",
                                       fid, exc_info=True)
                        stats["errors"] += 1
                        continue
                    db.delete(row)
                    stats["source_deleted"] += 1
                elif getattr(row, "share_revoked_at", None) is None:
                    try:
                        outcome = sa_client.remove_anyone_reader(
                            drive_id, fid)
                    except Exception:  # noqa: BLE001
                        logger.warning("闲置 TTL 收服主副本权限失败 fid=%s",
                                       fid, exc_info=True)
                        stats["errors"] += 1
                        continue
                    if outcome in ("revoked", "already_clean", "gone"):
                        row.share_revoked_at = now
                        stats["source_revoked"] += 1
                    else:
                        stats["errors"] += 1

    # 死令牌待收回兜底（2026-10-30 安全收口）：登记在案的行不看
    # 闲置时长，凭该用户任一 active 绑定补收，结果并入 stats。
    pending_stats = run_pending_share_revokes(db)
    stats["pending_revoked"] = pending_stats["revoked"]
    stats["errors"] += pending_stats["errors"]

    # 暂存扫荡（2026-10-30 安全收口）：Path B 暂存残留登记行凭
    # SA 令牌重试删除，成功删行销账。
    staging_stats = sweep_staging_leftovers(db, limit=limit)
    stats["staging_checked"] = staging_stats["checked"]
    stats["staging_deleted"] = staging_stats["deleted"]
    stats["errors"] += staging_stats["errors"]
    try:
        db.commit()
    except Exception:  # noqa: BLE001 — 提交失败回滚，不谎报
        logger.warning("闲置 TTL 清理提交失败", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        stats["errors"] += 1
    return stats


def note_usercopy_issued(_db, user_id, item_id, variant_id, fid,
                         via="usercontent"):
    """记一条用户副本直链发放日志 + 刷新副本行最后播放时间。

    用独立短会话（与请求会话隔离）：写 emby_direct_issue_logs 一行，
    并把对应变体行的 last_played_at 置为 now（供闲置 TTL 取新者
    用）。行已删时只记日志、不报错。第一个参数是 run_db 透传的
    请求会话，本函数不使用它（隔离写，绝不污染请求事务）。
    永不抛异常：调用方是播放主链路，记账失败绝不能影响本次
    PlaybackInfo 响应。
    """
    try:
        from backend.database import SessionLocal
        now = datetime.now()
        session = SessionLocal()
        try:
            session.add(em.DirectIssueLog(
                user_id=user_id, item_id=item_id, variant_id=variant_id,
                variant_file_id=fid, via=via, created_at=now))
            if variant_id:
                row = session.query(em.TransferVariant).filter(
                    em.TransferVariant.id == variant_id).first()
                if row is not None:
                    row.last_played_at = now
            session.commit()
        finally:
            session.close()
        return True
    except Exception:  # noqa: BLE001
        logger.warning("直链发放日志写入失败 user=%s item=%s",
                       user_id, item_id, exc_info=True)
        return False


def quota_playback_decision(signal, *, bound: bool) -> dict:
    """Quota 强信号的播放裁决。

    绑定用户 ``failover`` 恒为 False：不换 SA、不改播共享原件。
    状态码：入队或节流中 409；自动入队关着 409；已有副本 409；
    总闸关或信号处理失败 503。未绑定返回 failover，调用方保持
    旧的 302/502 回落（只可能是已下发签名）。
    """
    if not bound:
        return {"failover": True, "status": None, "detail": None}
    action = (signal or {}).get("action")
    reason = (signal or {}).get("reason")
    if action == "error" or reason in ("master_off", "internal"):
        return {"failover": False, "status": 503,
                "detail": QUOTA_UNAVAILABLE_DETAIL}
    if action in ("queued", "throttled") or reason == "job_within_24h":
        return {"failover": False, "status": 409,
                "detail": QUOTA_COPY_PENDING_DETAIL}
    if reason == "auto_off":
        return {"failover": False, "status": 409,
                "detail": QUOTA_AUTO_OFF_DETAIL}
    if reason == "has_ready_variant":
        return {"failover": False, "status": 409,
                "detail": QUOTA_READY_RETRY_DETAIL}
    return {"failover": False, "status": 409,
            "detail": QUOTA_NO_SHARED_DETAIL}


def _emit_quota_bound(result, item_id, user_id) -> dict:
    """绑定用户的 Quota 分支出口：强制事件，入队时再打一条。"""
    log_byos(
        "byos_quota_force",
        item=item_id, user=user_id,
        action=(result or {}).get("action"),
        reason=(result or {}).get("reason"),
        job_id=(result or {}).get("job_id"))
    if (result or {}).get("action") == "queued":
        log_byos(
            "byos_enqueue",
            item=item_id, user=user_id,
            job_id=(result or {}).get("job_id"),
            trigger="quota")
    return result


def _gate_retrigger_budget_ok(db, item_id, user_id, dest=None):
    """门禁重触发预算（2026-10-29 #1453 回归修复）。

    24h 窗口内同一归属桶已有 >=2 行任务时不再重触发：一次失败
    任务 + 一次重触发 = 2 行；第 3 次起不再建——防播放器每秒
    重试把“失败→重建”循环刷成任务洪水。桶口径与节流一致：
    同 provider 按 (item, user)，跨 provider 按 (item, user, dest)。
    """
    cutoff = datetime.now() - timedelta(seconds=THROTTLE_SEC)
    q = (db.query(em.TransferJob)
         .filter(em.TransferJob.item_id == item_id,
                 em.TransferJob.owner_scope == "user",
                 em.TransferJob.owner_ref == str(user_id),
                 em.TransferJob.created_at >= cutoff))
    if dest is not None:
        q = q.filter(em.TransferJob.dest_drive_id == dest)
    return q.count() < 2


def _retrigger_failed_job(db, item, user_id, provider, job, *,
                          cross=False, dest=None):
    """failed-retriable 任务的门禁重触发（2026-10-29 #1453 回归修复）。

    旧行为：失败任务直接判 failed → 调用方 409，且不新建不重试，
    播放器每秒重试也永远 409（黑屏）。新行为：预算内重建一单
    （force 越过 24h 节流——失败行本身否则会挡住重建），返回
    preparing/job_retriggered 走准备中态；预算耗尽或重建被拒时
    仍按失败／节流处理，不无限重试。一次播放请求内：新单在途
    时后续重查门禁只见 job_in_flight，不重复建。
    """
    item_id = getattr(item, "id", None)
    if not _gate_retrigger_budget_ok(db, item_id, user_id, dest=dest):
        return {"verdict": "failed", "reason": "retriable",
                "job_id": job.id}
    try:
        if cross:
            new_job = create_cross_provider_job(
                db, item, user_id, provider, trigger="playback", force=True)
        else:
            new_job = create_user_job(
                db, item, user_id, provider, trigger="playback", force=True)
    except TransferError as exc:
        if exc.kind == "throttled":
            return {"verdict": "preparing", "reason": "throttled",
                    "job_id": job.id}
        return {"verdict": "failed", "reason": exc.kind, "job_id": job.id}
    return {"verdict": "preparing", "reason": "job_retriggered",
            "job_id": new_job.id}


def _rebuild_done_job(db, item, user_id, provider, job, *,
                      cross=False, dest=None):
    """done 任务但本人副本已不在 ready 时的门禁重建（2026-10-29 生产实证）。

    旧行为：24h 内只要有过 done 任务就判 throttled，副本被 LRU /
    闲置清理 / 额度信号标 stale 删掉后，用户 24h 内一直“准备中”
    播不了（生产 99 个用户 357 条卡死）。调用方已确认本人无 ready
    副本（ready 检查在前）。新行为：预算内（与失败重触发同一个
    _gate_retrigger_budget_ok，24h 同桶 < 2 行）force 重建一单，
    返回 preparing/job_rebuilt 走准备中等流；预算耗尽仍 throttled，
    不会被播放器重试刷成任务洪水。只建用户盘任务（files.copy 到
    本人盘），不碰共享原件、不改选路。
    """
    item_id = getattr(item, "id", None)
    if not _gate_retrigger_budget_ok(db, item_id, user_id, dest=dest):
        return {"verdict": "preparing", "reason": "throttled",
                "job_id": job.id}
    try:
        if cross:
            new_job = create_cross_provider_job(
                db, item, user_id, provider, trigger="playback", force=True)
        else:
            new_job = create_user_job(
                db, item, user_id, provider, trigger="playback", force=True)
    except TransferError as exc:
        if exc.kind == "throttled":
            return {"verdict": "preparing", "reason": "throttled",
                    "job_id": job.id}
        return {"verdict": "failed", "reason": exc.kind, "job_id": job.id}
    return {"verdict": "preparing", "reason": "job_rebuilt",
            "job_id": new_job.id}


def rebuild_dead_user115_copy(db, item, user_id):
    """115 死链检出后的重建（serve_115_copy 现签校验专用）。

    与 _rebuild_done_job 同款预算口径：24h 同桶 < 2 行才 force 重建
    一单（_gate_retrigger_budget_ok），预算耗尽不建新单、不刷任务
    洪水。provider==pan115 走同 provider 任务，否则走跨 provider
    任务（Drive 源→用户 115 盘）；只建用户盘任务，不碰共享原件。
    返回 dict：preparing/job_rebuilt | preparing/throttled |
    failed/<kind>，与 _rebuild_done_job 同款形状。
    """
    item_id = getattr(item, "id", None)
    provider = _item_provider(db, item)
    if item_id is None or provider is None:
        return {"verdict": "failed", "reason": "unknown_provider"}
    cross = provider != "pan115"
    dest = f"pan115:user-{user_id}" if cross else None
    if not _gate_retrigger_budget_ok(db, item_id, user_id, dest=dest):
        return {"verdict": "preparing", "reason": "throttled"}
    try:
        if cross:
            new_job = create_cross_provider_job(
                db, item, user_id, provider, trigger="playback", force=True)
        else:
            new_job = create_user_job(
                db, item, user_id, provider, trigger="playback", force=True)
    except TransferError as exc:
        if exc.kind == "throttled":
            return {"verdict": "preparing", "reason": "throttled"}
        return {"verdict": "failed", "reason": exc.kind}
    return {"verdict": "preparing", "reason": "job_rebuilt",
            "job_id": new_job.id}


def rebuild_dead_drive_copy(db, item, user_id):
    """用户 Drive 副本已确认不可用后的重建（serve 路径专用）。

    与 rebuild_dead_user115_copy 同款预算口径：24h 同桶 < 2 行才
    force 重建一单（_gate_retrigger_budget_ok），预算耗尽不建新单、
    不刷任务洪水。provider==drive 走同 provider 任务；115 源→用户
    Drive 盘走跨 provider 任务（dest=userdrive:<uid>）；只建用户盘
    任务，不碰共享原件。返回 dict 与 _rebuild_done_job 同款形状。
    """
    item_id = getattr(item, "id", None)
    provider = _item_provider(db, item)
    if item_id is None or provider is None:
        return {"verdict": "failed", "reason": "unknown_provider"}
    cross = provider != "drive"
    dest = f"userdrive:{user_id}" if cross else None
    if not _gate_retrigger_budget_ok(db, item_id, user_id, dest=dest):
        return {"verdict": "preparing", "reason": "throttled"}
    try:
        if cross:
            new_job = create_cross_provider_job(
                db, item, user_id, provider, trigger="playback", force=True)
        else:
            new_job = create_user_job(
                db, item, user_id, provider, trigger="playback", force=True)
    except TransferError as exc:
        if exc.kind == "throttled":
            return {"verdict": "preparing", "reason": "throttled"}
        return {"verdict": "failed", "reason": exc.kind}
    return {"verdict": "preparing", "reason": "job_rebuilt",
            "job_id": new_job.id}


def reconcile_dead_drive_copy(db, item, user, variant):
    """serve 路径实证用户 Drive 副本不可用（404）后的对账入口。

    生产实锤（2026-10-30，item 688899 / user 1）：副本文件本体仍活在
    用户当前绑定的谷歌账号里，只是解绑旧绑定时 anyone-reader 被
    收回（share_revoked_at 落戳）、变体行仍 ready，非属主令牌一律
    回 404。这类行不该重建：先自愈——绕过已公开缓存、用当前绑定
    令牌重授 anyone-reader；授成（文件活着且已公开）返回 healed，
    调用方重走存在性校验即可放行。自愈失败（文件真被删 / 换了
    账号看不见 / 无可用绑定）才按既有口径记 strike
    （mark_variant_stale_once）并预算内重建
    （rebuild_dead_drive_copy），返回 rebuilding / throttled /
    failed。本函数永不抛异常进播放热路径。
    """
    user_id = getattr(user, "id", None)
    fid = (getattr(variant, "variant_file_id", "") or "").strip()
    if fid and user_id is not None:
        try:
            copy_public_cache_drop(user_id, fid)
            if ensure_user_copy_public(db, user_id, fid):
                return {"action": "healed"}
        except Exception:  # noqa: BLE001
            logger.warning("用户副本死链自愈异常 item=%s user=%s",
                           getattr(item, "id", None), user_id,
                           exc_info=True)
    try:
        mark_variant_stale_once(db, getattr(variant, "id", None))
    except Exception:  # noqa: BLE001
        logger.warning("用户副本死链记 strike 异常", exc_info=True)
    try:
        out = rebuild_dead_drive_copy(db, item, user_id)
    except Exception:  # noqa: BLE001
        logger.warning("用户副本死链重建异常 item=%s user=%s",
                       getattr(item, "id", None), user_id, exc_info=True)
        return {"action": "failed"}
    if (out or {}).get("reason") == "throttled":
        return {"action": "throttled"}
    if (out or {}).get("verdict") == "preparing":
        return {"action": "rebuilding", "job_id": out.get("job_id")}
    return {"action": "failed"}


def mark_variant_stale_once(db, variant_id):
    """给变体行记一次 stale strike（115 死链检出时调用）。

    与转存巡检同款语义：stale_count +1，满 STALE_STRIKES 行状态
    转 stale；下一轮门禁见 stale_count >= 1 即不再直接放行该行，
    改走任务逻辑（准备中/重建），不再反复给播放器发死链。
    行不存在返回 None，否则返回记分后的 stale_count。
    """
    if variant_id is None:
        return None
    row = (db.query(em.TransferVariant)
           .filter(em.TransferVariant.id == variant_id)
           .first())
    if row is None:
        return None
    row.stale_count = (getattr(row, "stale_count", 0) or 0) + 1
    if row.stale_count >= STALE_STRIKES:
        row.status = "stale"
    db.commit()
    return row.stale_count


def ensure_user_copy_gate(db, item, user, *, play_sign_consume=False):
    """首播转存门禁。语义见 ``_ensure_user_copy_gate_impl``。"""
    result = _ensure_user_copy_gate_impl(
        db, item, user, play_sign_consume=play_sign_consume)
    _observe_gate(result, item, user)
    return result


def _ensure_user_copy_gate_impl(db, item, user, *, play_sign_consume=False):
    """首播转存门禁（2026-10-26 用户亲定“先转存再播”）。

    绝不抛异常：内部异常一律吞掉返回 error verdict（调用方 503
    fail-closed）。本函数只读 + 可能建一个用户任务，不改条目身份、
    不改共享行、不碰播放链的写路径。
    返回 dict：{verdict, reason, job_id?, variant?}。
    """
    try:
        if not master_enabled():
            return {"verdict": "proceed", "reason": "master_off"}
        user_id = getattr(user, "id", None)
        if user_id is None:
            return {"verdict": "proceed", "reason": "no_user"}
        if play_sign_consume:
            # 已下发链接消费不追溯（与 #1341 绑定门禁同一口径）。
            return {"verdict": "proceed", "reason": "play_sign_consume"}
        path = (getattr(item, "file_path", "") or "").strip()
        if path.startswith("byos://"):
            # 已在用户盘里的条目：走现有 byos 变体，不再转存。
            return {"verdict": "proceed", "reason": "byos_item"}
        provider = _item_provider(db, item)
        if provider is None:
            return {"verdict": "proceed", "reason": "unknown_provider"}
        binding = _active_binding(db, user_id, provider)
        if binding is None:
            # provider 错配（2026-10-26 20:54 用户亲定“绑哪播哪”）：
            # 只绑了另一 provider → 走跨 provider 转存，不再放行共享链；
            # 无任何绑定 → 仍 proceed（拦无绑定用户是 #1341 绑定门禁
            # 的职责，本门禁不越权）。
            other = "pan115" if provider == "drive" else "drive"
            other_binding = _active_binding(db, user_id, other)
            if other_binding is None:
                return {"verdict": "proceed",
                        "reason": "no_matching_binding"}
            orig_fid = (getattr(item, "drive_file_id", "") or "").strip()
            if provider == "pan115" and not orig_fid:
                return {"verdict": "failed", "reason": "no_source_fid"}
            if provider == "drive" and not orig_fid:
                # 有绑定但没有可转的源 fid：不许 proceed 去播共享原件。
                return {"verdict": "failed", "reason": "no_drive_file"}
            item_id = getattr(item, "id", None)
            if item_id is None:
                return {"verdict": "failed", "reason": "no_item_id"}
            dest = (f"pan115:user-{user_id}" if other == "pan115"
                    else f"userdrive:{user_id}")
            scheme = "pan115:" if other == "pan115" else "userdrive:"
            ready = _newest_ready_for_scheme(db, item_id, "user",
                                             str(user_id), scheme)
            if (ready is not None and other in ("pan115", "drive")
                    and getattr(ready, "stale_count", 0) >= 1):
                # 副本行已吃过一次 stale strike：DB 状态虽仍 ready，
                # 但文件很可能已死（2026-10-29 115 死链实证；Drive
                # 副本 2026-10-30 同款），不再直接放行；落到下面
                # 任务逻辑走重建/准备中。
                ready = None
            if ready is not None:
                if other == "pan115":
                    return {"verdict": "serve_115_copy", "reason": "ready",
                            "variant": ready}
                return {"verdict": "proceed",
                        "reason": "has_ready_variant", "variant": ready}
            job = _latest_job_for_dest(db, item_id, "user",
                                       str(user_id), dest)
            preparing_detail = _cross_preparing_detail(provider, other)
            if job is not None:
                if job.status in ("pending", "running"):
                    return {"verdict": "preparing",
                            "reason": "job_in_flight", "job_id": job.id,
                            "detail": preparing_detail}
                if job.status == "failed":
                    if job.error_kind == CROSS_PROVIDER_DISABLED_KIND:
                        # 关停期（2026-10-29 #1487）遗留的终态行：它
                        # 不是真失败，是路线关停的产物。总开关开 →
                        # 按可重试处理、预算内建新单（不自动复活：
                        # 只在用户点播走到这里时才建）；关 → failed
                        # 明示，口径与关停期一致。
                        if cross_provider_enabled(db):
                            out = _retrigger_failed_job(
                                db, item, user_id, provider, job,
                                cross=True, dest=dest)
                            if out.get("verdict") == "preparing":
                                out.setdefault("detail", preparing_detail)
                            return out
                        return {"verdict": "failed",
                                "reason": job.error_kind,
                                "job_id": job.id,
                                "detail": CROSS_PROVIDER_DISABLED_DETAIL}
                    if job.error_kind == "retriable":
                        return _retrigger_failed_job(
                            db, item, user_id, provider, job, cross=True,
                            dest=dest)
                    if job.error_kind in _TERMINAL_JOB_KINDS:
                        return {"verdict": "failed", "reason": job.error_kind,
                                "job_id": job.id,
                                "detail": _cross_failed_detail(other, job)}
                    return {"verdict": "preparing", "reason": "throttled",
                            "job_id": job.id, "detail": preparing_detail}
                if job.status == "done":
                    if _recent_job_exists(db, item_id, "user",
                                          str(user_id)):
                        # 本人该 scheme 的 ready 副本已查过为空：done
                        # 行留下的只是“副本已被清掉”，预算内重建。
                        return _rebuild_done_job(
                            db, item, user_id, provider, job, cross=True,
                            dest=dest)
            try:
                new_job = create_cross_provider_job(db, item, user_id,
                                                    provider,
                                                    trigger="playback")
            except TransferError as exc:
                if exc.kind == "throttled":
                    return {"verdict": "preparing", "reason": "throttled",
                            "detail": preparing_detail}
                return {"verdict": "failed", "reason": exc.kind,
                        "detail": str(exc)}
            return {"verdict": "preparing", "reason": "job_created",
                    "job_id": new_job.id, "detail": preparing_detail}
        orig_fid = (getattr(item, "drive_file_id", "") or "").strip()
        if provider == "pan115" and not orig_fid:
            # 115 无源 fid：不建任务（不污染节流窗），等扫描补 fid。
            return {"verdict": "failed", "reason": "no_source_fid"}
        if provider == "drive" and not orig_fid:
            # 有绑定但没有可转的源 fid：不许 proceed 去播共享原件。
            return {"verdict": "failed", "reason": "no_drive_file"}
        item_id = getattr(item, "id", None)
        if item_id is None:
            return {"verdict": "failed", "reason": "no_item_id"}
        ready = _newest_ready(db, item_id, "user", str(user_id))
        if (ready is not None and provider in ("pan115", "drive")
                and getattr(ready, "stale_count", 0) >= 1):
            # 同上：吃过 strike 的副本行不直接放行，走下面任务逻辑重建。
            ready = None
        if ready is not None:
            if provider == "pan115":
                return {"verdict": "serve_115_copy", "reason": "ready",
                        "variant": ready}
            return {"verdict": "proceed", "reason": "has_ready_variant",
                    "variant": ready}
        job = _latest_job(db, item_id, "user", str(user_id))
        if job is not None:
            if job.status in ("pending", "running"):
                return {"verdict": "preparing", "reason": "job_in_flight",
                        "job_id": job.id}
            if job.status == "failed":
                if (job.error_kind == CROSS_PROVIDER_DISABLED_KIND
                        and _is_cross_job(job)):
                    # 关停期遗留的跨单失败行（该用户后来补绑了本
                    # provider，本分支按“最新一单”捞到了它）：它不是
                    # 本通道的失败，不许挡住同 provider 建单——不
                    # 返回，落到下方新建同盘单。真同 provider 失败
                    # 行（dest 同侧）一律不走这里、不受影响。
                    pass
                elif job.error_kind == "retriable":
                    return _retrigger_failed_job(
                        db, item, user_id, provider, job)
                elif job.error_kind in _TERMINAL_JOB_KINDS:
                    return {"verdict": "failed", "reason": job.error_kind,
                            "job_id": job.id}
                else:
                    # no_binding / no_source_fid：原因可能自愈（重绑/
                    # 重扫），但节流窗内不重复建，呈准备中 + 节流说明。
                    return {"verdict": "preparing", "reason": "throttled",
                            "job_id": job.id}
            if job.status == "done":
                if _recent_job_exists(db, item_id, "user", str(user_id)):
                    # 副本已释放/变 stale（ready 已查过为空）但节流窗未
                    # 过：预算内重建一单，预算耗尽才 throttled。
                    return _rebuild_done_job(
                        db, item, user_id, provider, job)
                # done 且节流窗已过：落到下面重建。
        try:
            new_job = create_user_job(db, item, user_id, provider,
                                      trigger="playback")
        except TransferError as exc:
            if exc.kind == "throttled":
                return {"verdict": "preparing", "reason": "throttled"}
            return {"verdict": "failed", "reason": exc.kind}
        return {"verdict": "preparing", "reason": "job_created",
                "job_id": new_job.id}
    except Exception:
        logger.warning("首播转存门禁异常（fail-closed，不放行共享播放）",
                       exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return {"verdict": "error", "reason": "internal"}


# ---------------------------------------------------------------------------
# 预转存（2026-10-27 极限优化，用户亲批全做，含预转存）
# ---------------------------------------------------------------------------
# 触发点：EA 详情口 get_item_detail（/emby/Items/{id}），不是列表口。
# 为什么钉在这里：第三方播放器（SenPlayer/REX）没有“停留回调”，
# 自家前端又没有独立详情视图调用 getItem——EA 详情口是唯一能
# 覆盖第三方播放器的触发点；列表浏览（/emby/Items?ParentId=…）
# 绝不触发，否则刷一屏列表就写一屏用户盘。
# “停留”落地成可执行的规则（不是计时器，因为第三方客户端给不出
# 计时器）：主动打开详情页 + 首个可播条目 + 已有 ready 不重复 +
# 24h 节流 + 每用户每天最多 5 单 + 工人每用户 2 单在途上限。
# 每天 5 单是硬帽子：用户一天打开 100 个详情页，最多也只预转 5 个，
# 剩下的等点播时走 playback 快通道（快通道优先于 prefetch 慢通道，
# 见 _pick_candidates：只有 trigger=="playback" 才算快通道）。
def _prefetch_target_item(db, item):
    """详情页对应的首个可播条目（或 None）。

    - 电影 / 单集：本身（须有 drive_file_id，byos:// 排除在外，
      调用方另行判断）；
    - 剧：series_id == 剧 id 的单集，按季号、集号、id 排序取首集
      （季号/集号为空排最后，不靠入库顺序猜）；
    - 季：parent_id == 季 id 的单集，同款排序取首集；parent 为空
      时回落 series_id + season_number 匹配；
    - 其它类型（季之外的容器、人物等）：None，不预转。
    只认活行（deleted_at 为空、merged_into_id 为空）。
    """
    item_type = getattr(item, "item_type", "") or ""
    if item_type in ("movie", "episode"):
        return item
    if item_type == "series":
        q = (db.query(em.MediaItem)
             .filter(em.MediaItem.item_type == "episode",
                     em.MediaItem.series_id == getattr(item, "id", None),
                     em.MediaItem.deleted_at.is_(None),
                     em.MediaItem.merged_into_id.is_(None)))
    elif item_type == "season":
        q = (db.query(em.MediaItem)
             .filter(em.MediaItem.item_type == "episode",
                     em.MediaItem.parent_id == getattr(item, "id", None),
                     em.MediaItem.deleted_at.is_(None),
                     em.MediaItem.merged_into_id.is_(None)))
        first = q.order_by(
            em.MediaItem.season_number.is_(None),
            em.MediaItem.season_number,
            em.MediaItem.episode_number.is_(None),
            em.MediaItem.episode_number,
            em.MediaItem.id).first()
        if first is not None:
            return first
        if getattr(item, "series_id", None) is not None:
            q = (db.query(em.MediaItem)
                 .filter(em.MediaItem.item_type == "episode",
                         em.MediaItem.series_id == item.series_id,
                         em.MediaItem.season_number == item.season_number,
                         em.MediaItem.deleted_at.is_(None),
                         em.MediaItem.merged_into_id.is_(None)))
        else:
            return None
    else:
        return None
    return q.order_by(
        em.MediaItem.season_number.is_(None),
        em.MediaItem.season_number,
        em.MediaItem.episode_number.is_(None),
        em.MediaItem.episode_number,
        em.MediaItem.id).first()


def _prefetch_today_count(db, user_id) -> int:
    """该用户今天已建的 prefetch 单数（本地 naive 时间，与节流同口径）。"""
    today_start = datetime.now().replace(hour=0, minute=0, second=0,
                                         microsecond=0)
    return (db.query(em.TransferJob)
            .filter(em.TransferJob.owner_scope == "user",
                    em.TransferJob.owner_ref == str(user_id),
                    em.TransferJob.trigger == "prefetch",
                    em.TransferJob.created_at >= today_start)
            .count())


def maybe_prefetch_transfer(db, item, user) -> dict:
    """详情页预转存（幂等、便宜、永不抛错进详情页）。

    返回 {"triggered", "reason", "job_id"?, "target_item_id"?}。
    不触发的情形一律明示原因，不静默：master_off / no_user /
    unsupported_type / no_first_episode / byos_item /
    unknown_provider / no_matching_binding / already_ready /
    throttled / daily_cap / no_source_fid / no_drive。
    跨 provider 走现有“绑哪播哪”规则（只绑另一 provider 时建跨单，
    不偷换成同 provider 单）；无任何绑定时不建单（拦无绑定用户
    是 #1341 绑定门禁的职责，预转存不越权）。
    """
    try:
        if not master_enabled():
            return {"triggered": False, "reason": "master_off"}
        user_id = getattr(user, "id", None)
        if user_id is None:
            return {"triggered": False, "reason": "no_user"}
        target = _prefetch_target_item(db, item)
        if target is None:
            reason = ("no_first_episode"
                      if (getattr(item, "item_type", "") or "")
                      in ("series", "season") else "unsupported_type")
            return {"triggered": False, "reason": reason}
        path = (getattr(target, "file_path", "") or "").strip()
        if path.startswith("byos://"):
            return {"triggered": False, "reason": "byos_item"}
        provider = _item_provider(db, target)
        if provider is None:
            return {"triggered": False, "reason": "unknown_provider"}
        binding = _active_binding(db, user_id, provider)
        if binding is None:
            other = "pan115" if provider == "drive" else "drive"
            other_binding = _active_binding(db, user_id, other)
            if other_binding is None:
                return {"triggered": False, "reason": "no_matching_binding"}
            orig_fid = (getattr(target, "drive_file_id", "") or "").strip()
            if provider == "pan115" and not orig_fid:
                return {"triggered": False, "reason": "no_source_fid"}
            if provider == "drive" and not orig_fid:
                return {"triggered": False, "reason": "no_drive_file"}
            dest = (f"pan115:user-{user_id}" if other == "pan115"
                    else f"userdrive:{user_id}")
            scheme = "pan115:" if other == "pan115" else "userdrive:"
            ready = _newest_ready_for_scheme(db, target.id, "user",
                                             str(user_id), scheme)
            if ready is not None:
                return {"triggered": False, "reason": "already_ready",
                        "target_item_id": target.id}
            if _recent_job_exists(db, target.id, "user", str(user_id)):
                return {"triggered": False, "reason": "throttled",
                        "target_item_id": target.id}
            if _prefetch_today_count(db, user_id) >= prefetch_daily_max():
                return {"triggered": False, "reason": "daily_cap",
                        "target_item_id": target.id}
            try:
                job = create_cross_provider_job(db, target, user_id,
                                                provider, trigger="prefetch")
            except TransferError as exc:
                if exc.kind == "throttled":
                    return {"triggered": False, "reason": "throttled",
                            "target_item_id": target.id}
                return {"triggered": False, "reason": exc.kind,
                        "target_item_id": target.id}
            return {"triggered": True, "reason": "job_created",
                    "job_id": job.id, "target_item_id": target.id}
        orig_fid = (getattr(target, "drive_file_id", "") or "").strip()
        if provider == "pan115" and not orig_fid:
            return {"triggered": False, "reason": "no_source_fid"}
        if provider == "drive" and not orig_fid:
            return {"triggered": False, "reason": "no_drive_file"}
        ready = _newest_ready(db, target.id, "user", str(user_id))
        if ready is not None:
            return {"triggered": False, "reason": "already_ready",
                    "target_item_id": target.id}
        if _recent_job_exists(db, target.id, "user", str(user_id)):
            return {"triggered": False, "reason": "throttled",
                    "target_item_id": target.id}
        if _prefetch_today_count(db, user_id) >= prefetch_daily_max():
            return {"triggered": False, "reason": "daily_cap",
                    "target_item_id": target.id}
        try:
            job = create_user_job(db, target, user_id, provider,
                                  trigger="prefetch")
        except TransferError as exc:
            if exc.kind == "throttled":
                return {"triggered": False, "reason": "throttled",
                        "target_item_id": target.id}
            return {"triggered": False, "reason": exc.kind,
                    "target_item_id": target.id}
        return {"triggered": True, "reason": "job_created",
                "job_id": job.id, "target_item_id": target.id}
    except Exception:  # noqa: BLE001 — 预转存绝不能打断详情页
        logger.warning("详情页预转存异常（不影响详情页）", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"triggered": False, "reason": "error"}


async def wait_for_transfer_ready(initial_gate, gate_callable, *,
                                  max_sec=None, poll_sec=None):
    """准备中态等流：每 poll 秒重查一次门禁，直到变体就绪。

    只对 reason 为 job_created / job_in_flight / job_retriggered /
    job_rebuilt
    的 preparing 等：throttled（节流窗内无在途任务）等再久也没用，
    直接回原 verdict，不烧 15 秒。其它 verdict（proceed /
    serve_115_copy / failed / error）一出现即回，不等满时。超时回
    最后一次 preparing verdict，调用方按旧 409 处理。gate_callable
    异常时回初始 preparing verdict（调用方 409，不把等流本身的
    异常读成播放失败）。全程不 serve 任何字节、不回落共享：等到
    的只是“门禁变了”，真正的 302 仍由调用方按正常选路走用户副本。
    """
    gate = initial_gate or {}
    if gate.get("verdict") != "preparing":
        return gate
    if gate.get("reason") not in ("job_created", "job_in_flight",
                                  "job_retriggered", "job_rebuilt"):
        return gate
    if not wait_stream_enabled():
        return gate
    max_s = wait_stream_max_sec() if max_sec is None else float(max_sec)
    poll_s = wait_stream_poll_sec() if poll_sec is None else float(poll_sec)
    deadline = time.monotonic() + max_s
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_s)
        try:
            gate = await gate_callable()
        except Exception:  # noqa: BLE001 — 重查失败不改 verdict
            return initial_gate
        gate = gate or {}
        if gate.get("verdict") != "preparing":
            return gate
    return gate


# ---------------------------------------------------------------------------
# 点播当场先办（2026-10-29 用户亲点“点播当场先办，办不完才转
# 后台”）：工人从点播的必经之路降为兜底。
#
# 形状：门禁判 preparing 且带 job_id 时，请求路径先试当场执行——
# 用与工人同一套认领语义把这一单从 pending 原子领走（与工人
# 互斥），复用工人执行核心 _execute_claimed_job 跑完（心跳/
# 租约/终态/清租约全是工人同一套），办成立即重查门禁、本请求
# 内放行。领不到（工人或另一请求已在办）、不合格（非点播触发/
# 跨 provider/file-only/无绑定）、名额满、预算内没办完——一律
# 原样回 preparing，工人按现行队列兜底，一行状态都不多动。
# ---------------------------------------------------------------------------

#: 当场认领的 claimed_by 标记（与工人 _WORKER_ID 区分，便于观测
#: 一单到底是被请求当场办的还是工人办的）。
_INLINE_WORKER_ID = "inline-" + uuid.uuid4().hex[:12]

_inline_inflight = 0
_inline_inflight_lock = threading.Lock()


def _inline_try_acquire(limit):
    """非阻塞抢当场执行名额：在飞数达上限直接 False，绝不等待。"""
    global _inline_inflight
    with _inline_inflight_lock:
        if _inline_inflight >= max(1, int(limit or 1)):
            return False
        _inline_inflight += 1
        return True


def _inline_release():
    global _inline_inflight
    with _inline_inflight_lock:
        _inline_inflight = max(0, _inline_inflight - 1)


def claim_job_for_inline(db, job_id, worker_id=None, now=None):
    """单单原子认领，与 claim_due_jobs 同语义（工人/请求互斥关键）。

    只认领 pending 且已到期（next_retry_at 为空或已到点）的行：
    条件 UPDATE，rowcount==1 才算领到；已被领走（running）、已
    终态、还在重试退避期的行 rowcount==0。认领即置 running +
    租约（时长与工人同口径）+ claimed_by 并提交；执行中由
    _execute_claimed_job 的心跳续租，与工人完全同一套标记，
    绝不跨 copy 持有数据库行锁。
    """
    from sqlalchemy import text
    now = now or datetime.now()
    worker_id = worker_id or _INLINE_WORKER_ID
    lease_until = now + timedelta(seconds=worker_lease_sec())
    result = db.execute(
        text("UPDATE emby_transfer_jobs SET status='running', "
             "lease_expires_at=:lease, claimed_by=:worker "
             "WHERE id=:jid AND status='pending' "
             "AND (next_retry_at IS NULL OR next_retry_at <= :now)"),
        {"lease": lease_until, "worker": worker_id, "jid": job_id,
         "now": now})
    db.commit()
    return result.rowcount == 1


def _inline_job_eligible(db, job):
    """当场执行只限点播触发的同 provider 服务端 copy：

    - trigger 必须 playback：预取/批量/重建/Quota 信号等非点播
      触发一律仍走工人，请求路径不当场跑；
    - owner_scope 必须 user：服主盘任务不在用户请求里执行；
    - 跨 provider 搬字节（_is_cross_job，2026-10-30 重开）：分钟级
      慢活，绝不在点播请求里当场搬字节——不进当场路径，明确走
      工人后台队列（门禁呈准备中态，转完才可点）；
    - Drive file-only 绑定无服务端 copy 路（令牌看不见源
      文件）：不进当场路径，照旧由工人落
      file_only_copy_unavailable 明确失败，不在这里假装能办；
    - 无生效绑定时执行只会落 no_binding，一样不当场跑（工人
      会按同一 error_kind 落账，口径不变）。
    """
    if job is None:
        return False
    if (getattr(job, "trigger", "") or "") != "playback":
        return False
    if (getattr(job, "owner_scope", "") or "") != "user":
        return False
    if _is_cross_job(job):
        return False
    provider = (getattr(job, "provider", "") or "").strip()
    if provider not in ("drive", "pan115"):
        return False
    try:
        user_id = int((getattr(job, "owner_ref", "") or "").strip())
    except (TypeError, ValueError):
        return False
    binding = _active_binding(db, user_id, provider)
    if binding is None:
        return False
    if provider == "drive" and _is_file_only_binding(binding):
        return False
    return True


def claim_and_execute_inline(job_id, session_factory=None,
                             drive_api_factory=None):
    """当场执行一单（同步，调用方须放线程里跑）。

    独立短会话查合格性 → 原子认领 → 复用工人执行核心
    _execute_claimed_job（自带独立会话/心跳/清租约/metrics，
    与工人执行逐字同一条路，绝不另抄一份 copy 逻辑）。
    返回：执行核心的 outcome（done/failed/retried/missing）；
    没领到 "not_claimed"；不合格 "ineligible"；查不到行
    "missing"；认领段异常 "error"。本函数绝不抛异常——当场
    执行出什么问题都不能把点播请求打成 500，回 preparing 让
    工人兜底才是正解。
    """
    if session_factory is None:
        from backend.database import SessionLocal
        session_factory = SessionLocal
    if drive_api_factory is None:
        drive_api_factory = DriveApiClient
    db = session_factory()
    try:
        job = (db.query(em.TransferJob)
               .filter(em.TransferJob.id == job_id).first())
        if job is None:
            return "missing"
        if not _inline_job_eligible(db, job):
            return "ineligible"
        if not claim_job_for_inline(db, job_id):
            return "not_claimed"
    except Exception:
        logger.warning("点播当场转存：认领失败 job=%s", job_id,
                       exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass
        return "error"
    finally:
        db.close()
    return _execute_claimed_job(job_id, session_factory, drive_api_factory)


def _play_inline_config():
    """当场先办配置快照（独立短会话热读）：{enabled, budget_sec,
    concurrency}。读失败 fail-safe 回默认（开 / 20 秒 / 4）——
    配置读不到时不悄悄关掉救播放的路（与同类开关同口径）。"""
    try:
        from backend.database import SessionLocal
        db = SessionLocal()
        try:
            return {"enabled": play_inline_enabled(db),
                    "budget_sec": play_inline_budget_sec(db),
                    "concurrency": play_inline_concurrency(db)}
        finally:
            db.close()
    except Exception:
        return {"enabled": True,
                "budget_sec": PLAY_INLINE_TRANSFER_BUDGET_DEFAULT,
                "concurrency": PLAY_INLINE_TRANSFER_CONCURRENCY_DEFAULT}


async def maybe_run_play_inline(initial_gate, gate_callable, *,
                                session_factory=None,
                                drive_api_factory=None):
    """点播当场先办（请求路径入口，形状与 wait_for_transfer_ready 同款）。

    initial_gate 为 preparing 且带 job_id 时尝试当场执行，办完用
    gate_callable 重查门禁并返回新 verdict（proceed 时调用方在
    同一请求内继续正常选路放行，用户一次点播直接出画面）。

    以下任一情况原样返回 initial_gate（调用方按现行 preparing
    口径继续，工人队列兜底不变）：
    - verdict 不是 preparing、没有 job_id、或总开关关；
    - 当场执行名额已满（不排队等名额）；
    - 任务不合格或已被工人/另一请求认领（同一 (item, owner)
      任何时刻只有一个执行者，执行核心只会被调一次）；
    - 预算内没办完：执行**不取消**——已认领的单在租约 + 心跳
      保护下由同一执行核心收尾；万一进程/线程中途死掉，租约
      过期回收（reclaim_expired_leases）会把单交还工人。请求
      侧对任务行零额外手术，状态/租约与工人接手完全同口径。

    gate_callable 抛异常时返回 initial_gate（与等流同口径，
    不把重查异常读成播放失败）。
    """
    gate = initial_gate or {}
    if gate.get("verdict") != "preparing":
        return gate
    job_id = gate.get("job_id")
    if not job_id:
        return gate
    cfg = await asyncio.to_thread(_play_inline_config)
    if not cfg["enabled"]:
        return gate
    if not _inline_try_acquire(cfg["concurrency"]):
        logger.info("[byos] play_inline_skipped job=%s reason=no_slot",
                    job_id)
        return gate

    async def _run():
        try:
            return await asyncio.to_thread(
                claim_and_execute_inline, job_id,
                session_factory=session_factory,
                drive_api_factory=drive_api_factory)
        finally:
            _inline_release()

    task = asyncio.ensure_future(_run())
    try:
        outcome = await asyncio.wait_for(
            asyncio.shield(task), timeout=cfg["budget_sec"])
    except (asyncio.TimeoutError, TimeoutError):
        logger.info("[byos] play_inline_timeout job=%s budget_sec=%s",
                    job_id, cfg["budget_sec"])
        return gate
    except Exception:
        logger.warning("点播当场转存异常 job=%s", job_id, exc_info=True)
        return gate
    logger.info("[byos] play_inline_outcome job=%s outcome=%s",
                job_id, outcome)
    if outcome in ("not_claimed", "ineligible", "missing", "error"):
        return gate
    try:
        new_gate = await gate_callable()
    except Exception:  # noqa: BLE001 — 重查失败不改 verdict
        return gate
    return new_gate or gate


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


def _recent_job_exists(db, item_id, owner_scope="source", owner_ref=None,
                       window_sec=THROTTLE_SEC) -> bool:
    """24h 节流：同一条目同一归属桶内有任务行即节流。

    分桶是关键：用户 A 的任务绝不能堵住用户 B 的任务（2026-10-26
    审计发现旧全局口径有此问题）。服主桶 owner_ref 为空。
    """
    cutoff = datetime.now() - timedelta(seconds=window_sec)
    q = (db.query(em.TransferJob)
         .filter(em.TransferJob.item_id == item_id,
                 em.TransferJob.owner_scope == owner_scope,
                 em.TransferJob.created_at >= cutoff))
    if owner_ref is None:
        q = q.filter(em.TransferJob.owner_ref.is_(None))
    else:
        q = q.filter(em.TransferJob.owner_ref == owner_ref)
    return q.first() is not None


def _latest_job(db, item_id, owner_scope, owner_ref=None):
    """该条目该归属桶内最新的一行任务（或 None）。"""
    q = (db.query(em.TransferJob)
         .filter(em.TransferJob.item_id == item_id,
                 em.TransferJob.owner_scope == owner_scope))
    if owner_ref is None:
        q = q.filter(em.TransferJob.owner_ref.is_(None))
    else:
        q = q.filter(em.TransferJob.owner_ref == owner_ref)
    return q.order_by(em.TransferJob.id.desc()).first()


def note_quota_signal(db, item, user, playing_variant, drive_id):
    """Quota 网页信号处理：绝不抛异常（播放绝不能被转存逻辑拖垮）。

    返回 dict：action in ignored/skipped/throttled/queued/error。
    - playing_variant 非空 → 正在播的副本爆额度是确定性证据，立即
      标 stale（不走巡检的两次规则），下次播放不再选它；
    - BYOS 用户 → 建用户盘任务（2026-10-26 口径；旧“只记日志”
      已废止，用户盘拷贝见 D4）；
    - 已有健康副本 → 跳过（不要对同一条目堆副本）；
    - 24h 内同归属桶已有任务 → 节流（throttled，不建新任务）；
    - 自动开关关（``quota_auto_enqueue_enabled`` 默认关）→ 只记日志，
      返回 skipped/auto_off（供人工手动触发）。绑定用户不建服主任务。
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
            # BYOS 用户：额度信号触发给该用户建用户盘任务（不再 skip）。
            # 开关默认关：只记信号，不入队，也不落到下面的服主任务。
            ready_user = _newest_ready(db, item_id, "user", str(user_id))
            if ready_user is not None:
                result = {"action": "skipped", "reason": "has_ready_variant"}
            elif _recent_job_exists(db, item_id, "user", str(user_id)):
                result = {"action": "throttled", "reason": "job_within_24h"}
            elif not quota_auto_enqueue_enabled():
                logger.info(
                    "转存变体 Quota 信号：自动入队关，BYOS 用户仅记日志 "
                    "item=%s user=%s", item_id, user_id)
                result = {"action": "skipped", "reason": "auto_off"}
            else:
                try:
                    job = create_user_job(db, item, user_id, "drive",
                                          trigger="auto")
                except TransferError as exc:
                    if exc.kind == "throttled":
                        result = {"action": "throttled",
                                  "reason": "job_within_24h"}
                    else:
                        result = {"action": "skipped", "reason": exc.kind}
                else:
                    logger.warning(
                        "转存变体应急自动入队（用户盘）：item=%s user=%s "
                        "job=%s", item_id, user_id, job.id)
                    result = {"action": "queued", "job_id": job.id}
            return _emit_quota_bound(result, item_id, user_id)
        if user_id is not None and has_active_pan115_binding(db, user_id):
            # 只绑了 115：入用户 115，绝不建服主 source 任务。
            provider = _item_provider(db, item)
            if provider == "pan115":
                ready_user = _newest_ready_for_scheme(
                    db, item_id, "user", str(user_id), "pan115:")
                enqueue = "same"
            elif provider == "drive":
                ready_user = _newest_ready_for_scheme(
                    db, item_id, "user", str(user_id), "pan115:")
                enqueue = "cross"
            else:
                logger.info(
                    "转存变体 Quota 信号：115 绑定但条目 provider 不明，"
                    "不入队 item=%s user=%s", item_id, user_id)
                return _emit_quota_bound(
                    {"action": "skipped", "reason": "unknown_provider"},
                    item_id, user_id)
            if ready_user is not None:
                result = {"action": "skipped", "reason": "has_ready_variant"}
            elif _recent_job_exists(db, item_id, "user", str(user_id)):
                result = {"action": "throttled", "reason": "job_within_24h"}
            elif not quota_auto_enqueue_enabled():
                logger.info(
                    "转存变体 Quota 信号：自动入队关，115 绑定用户仅记日志 "
                    "item=%s user=%s", item_id, user_id)
                result = {"action": "skipped", "reason": "auto_off"}
            else:
                try:
                    if enqueue == "cross":
                        job = create_cross_provider_job(
                            db, item, user_id, "drive", trigger="auto")
                    else:
                        job = create_user_job(
                            db, item, user_id, "pan115", trigger="auto")
                except TransferError as exc:
                    if exc.kind == "throttled":
                        result = {"action": "throttled",
                                  "reason": "job_within_24h"}
                    else:
                        result = {"action": "skipped", "reason": exc.kind}
                else:
                    logger.warning(
                        "转存变体应急自动入队（用户 115）：item=%s user=%s "
                        "job=%s", item_id, user_id, job.id)
                    result = {"action": "queued", "job_id": job.id}
            return _emit_quota_bound(result, item_id, user_id)
        ready = get_ready_variant_for_user(db, item_id, user_id)
        if ready is not None:
            return {"action": "skipped", "reason": "has_ready_variant"}
        if _recent_job_exists(db, item_id, "source", None):
            return {"action": "throttled", "reason": "job_within_24h"}
        if not quota_auto_enqueue_enabled():
            logger.info(
                "转存变体 Quota 信号：自动入队默认关，仅记日志 item=%s",
                item_id)
            return {"action": "skipped", "reason": "auto_off"}
        job = em.TransferJob(item_id=item_id, trigger="auto",
                             drive_id=drive_id, provider="drive",
                             owner_scope="source", status="pending")
        db.add(job)
        db.commit()
        notify_transfer_wakeup(job.id)
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
    if not force and _recent_job_exists(db, item_id, "source", None):
        raise TransferError("throttled", "24h 内已尝试过转存")
    job = em.TransferJob(item_id=item_id, trigger="manual",
                         drive_id=drive_id, provider="drive",
                         owner_scope="source", status="pending")
    db.add(job)
    db.commit()
    notify_transfer_wakeup(job.id)
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


def _release_ready_rows(db, item_id, owner_scope, owner_ref=None) -> int:
    """把该条目该 scope（用户 scope 时限定该用户）的 ready 行置
    released（成功建新副本前调用，保证一物一生效源；调用方负责
    commit）。用户 scope 不带 owner_ref 过滤会误释他人的行，绝不
    允许：用户 scope 调用方必须传 owner_ref。"""
    q = (db.query(em.TransferVariant)
         .filter(em.TransferVariant.item_id == item_id,
                 em.TransferVariant.owner_scope == owner_scope,
                 em.TransferVariant.status == "ready"))
    if owner_scope == "user":
        q = q.filter(em.TransferVariant.owner_ref == owner_ref)
    rows = q.all()
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


def _drive_error_snippet(resp, limit=300):
    """Drive 错误体摘要（2026-10-29 可观测性修复）。

    旧口径只记状态码，403 精确子原因（如 insufficientPermissions
    与其它 403）无法区分。Drive 错误体是公开的错误结构
    （code/message/errors[]），不含令牌等凭据；空白折叠后截断，
    防日志与 error_detail 膨胀。解析失败回落空串。
    """
    try:
        text = resp.text or ""
    except Exception:  # noqa: BLE001 — 摘要失败不挡主流程
        return ""
    if not text:
        return ""
    return " ".join(text.split())[:limit]


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
        # 共享盘 files.list 的硬要求（Google 原话）：driveId 指定时
        # 或 corpora 含 drive/allDrives 时，必须带
        # includeItemsFromAllDrives=true，否则 403；此前本处缺失，
        # Path B 在源共享盘建暂存目录的第一步即 403（生产 168 单
        # 同款失败，见 tests/test_transfer_shared_list_params.py）。
        # mount_drive / drive_changes 的共享盘列举早已带此参数，
        # 本处是唯一缺失点，四处 files.list 共用此口径。
        return {"driveId": drive_id, "corpora": "drive",
                "includeItemsFromAllDrives": "true"}

    def ensure_folder(self, drive_id, dir_name):
        """转存目录在就返回 id，没有就建（同盘根目录下）。

        列举 403 时的回落（2026-10-29 #1462 引入）：根枚举 403
        后改按名查应用自创文件（不带 parents 条件），查到即复用，
        不重复建；按名查 200 但无匹配时才走创建；按名查也失败时
        不盲建（会重名），带错误体报可重试失败。注意：生产 168 单
        失败的真因并非用户侧根查询，而是共享盘列举缺
        includeItemsFromAllDrives 参数（见 _list_params），回落与
        首查共用同一参数口径，缺参时两者同 403——本回落只兜其它
        403 成因，不替代参数本身。
        """
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
        elif resp.status_code == 403:
            logger.warning(
                "转存目录根枚举 403，走按名查应用自创文件：drive=%s "
                "dir=%s body=%s",
                drive_id, dir_name, _drive_error_snippet(resp))
            fid = self._find_app_folder(drive_id, dir_name)
            if fid is not None:
                return fid
            # 按名查 200 但无匹配：目录确实不存在，落到下面创建。
        else:
            raise _Retriable(
                f"列转存目录失败：{resp.status_code} "
                f"{_drive_error_snippet(resp)}")
        body = {"name": dir_name, "mimeType": _FOLDER_MIME, "parents": [root]}
        resp = self._request("POST", FILES_URL, drive_id, json=body,
                             params={"supportsAllDrives": "true"})
        if resp.status_code in (200, 201):
            fid = (resp.json() or {}).get("id")
            if fid:
                return str(fid)
        raise _Retriable(
            f"建转存目录失败：{resp.status_code} "
            f"{_drive_error_snippet(resp)}")

    def _find_app_folder(self, drive_id, dir_name):
        """按名查应用自创转存目录（file-only 兼容：不带 parents）。

        返回 id（查到）或 None（200 但无匹配，目录确实不存在）。
        非 200 抛 _Retriable（带 Drive 错误体，供日志定性精确
        子原因；调用方不盲建，避免同名目录重复）。
        """
        escaped = dir_name.replace("'", "\\'")
        q = (f"name='{escaped}' and mimeType='{_FOLDER_MIME}' "
             f"and trashed=false")
        params = {"q": q, "supportsAllDrives": "true",
                  "fields": "files(id,name)"}
        params.update(self._list_params(drive_id))
        resp = self._request("GET", FILES_URL, drive_id, params=params)
        if resp.status_code == 200:
            files = (resp.json() or {}).get("files") or []
            if files:
                return str(files[0]["id"])
            return None
        raise _Retriable(
            f"按名列转存目录失败：{resp.status_code} "
            f"{_drive_error_snippet(resp)}")

    def copy_file(self, drive_id, source_fid, folder_id, name):
        """同盘 files.copy；403/404 转 _CopyForbidden（死文件，不重试）。

        唯一例外：403 且响应体判定为存储配额不足时转
        _CopyQuotaExceeded（不继承 _CopyForbidden，调用方只走
        空间处理，绝不能当死文件走 Path B）。401 由 _request
        内部刷新重试，刷新后仍 401 不进这里（_request 原样返回
        401 响应时按旧口径走 _Retriable，不误判成配额问题）。
        """
        url = f"{FILES_URL}/{source_fid}/copy"
        body = {"name": name, "parents": [folder_id]}
        resp = self._request("POST", url, drive_id, json=body,
                             params={"supportsAllDrives": "true"},
                             timeout=120)
        if resp.status_code in (200, 201):
            data = resp.json() or {}
            return {"id": str(data.get("id") or ""),
                    "size": _to_int(data.get("size"))}
        if resp.status_code == 403 and _is_storage_quota_response(resp):
            raise _CopyQuotaExceeded(
                "copy 被拒：用户盘存储配额不足（403 storageQuotaExceeded）")
        if resp.status_code in (403, 404):
            raise _CopyForbidden(f"copy 被拒：{resp.status_code}")
        raise _Retriable(f"copy 失败：{resp.status_code}")

    def about_storage(self, drive_id):
        """用户盘配额只读查询（Drive about.get，fields=storageQuota）。

        返回 {limit, usage, free}（字节整数；limit 解析不到时为
        None，free 随之为 None，表示“不限量/测不到上限”，调用方
        按空间充足处理，不据此腾地方）。free = limit - usage：
        usage 是账号总用量（Drive + 邮件 + 相册），正是配额执法
        口径，不是仅转存目录用量。非 200 一律 _Retriable（调用方
        按 quota_unknown 处理：不腾地方、照旧 copy，不把一次
        about 抖动变成转存失败）。本方法只读配额，不列文件、
        不读个人文件内容。
        """
        url = "https://www.googleapis.com/drive/v3/about"
        resp = self._request("GET", url, drive_id,
                             params={"fields": "storageQuota"})
        if resp.status_code != 200:
            raise _Retriable(f"about 配额查询失败：{resp.status_code}")
        quota = (resp.json() or {}).get("storageQuota") or {}
        limit = _to_int(quota.get("limit"))
        usage = _to_int(quota.get("usage"))
        if limit is None or usage is None:
            return {"limit": limit, "usage": usage, "free": None}
        return {"limit": limit, "usage": usage, "free": limit - usage}

    def add_anyone_reader(self, drive_id, fid):
        """给副本补 anyone reader（中继与 302 两条链都可用，见 PR 描述
        与设计稿 §4.1 的口径说明）。200/201/409 均视为成功：409 是
        文件已有相同权限（幂等，drive_share.ensure_shared 同款口径），
        重试/重登记时不报错；其它非 2xx 抛异常，调用方记 orphan。"""
        url = f"{FILES_URL}/{fid}/permissions"
        body = {"role": "reader", "type": "anyone"}
        resp = self._request("POST", url, drive_id, json=body,
                             params={"supportsAllDrives": "true"})
        if resp.status_code not in (200, 201, 409):
            raise Exception(f"补 anyone 权限失败：{resp.status_code}")

    def list_permissions(self, drive_id, fid):
        """列出指定文件的权限（防外流收回用）。

        404 → _NotFound（与 get_file 同款口径：文件已不在）；
        其它非 200 抛异常，由调用方记 error、不静默当成功。
        """
        url = f"{FILES_URL}/{fid}/permissions"
        resp = self._request("GET", url, drive_id,
                             params={"supportsAllDrives": "true"})
        if resp.status_code == 404:
            raise _NotFound("副本不存在")
        if resp.status_code != 200:
            raise Exception(f"列文件权限失败：{resp.status_code}")
        return (resp.json() or {}).get("permissions") or []

    def remove_anyone_reader(self, drive_id, fid):
        """收回指定文件的 anyone-reader 权限（防外流三件套之一）。

        只删 ``type == "anyone"`` 的权限行，绝不动其它共享人。
        返回：``"revoked"``（删了至少一条）| ``"already_clean"``
        （文件在、但本来就没有 anyone 权限）| ``"gone"``
        （文件已不在）。DELETE 遇 404 同样算成功（权限已随文件
        或先前操作消失）。
        """
        try:
            perms = self.list_permissions(drive_id, fid)
        except _NotFound:
            return "gone"
        anyone_ids = [p.get("id") for p in perms
                      if isinstance(p, dict) and p.get("type") == "anyone"
                      and p.get("id")]
        if not anyone_ids:
            return "already_clean"
        for perm_id in anyone_ids:
            url = f"{FILES_URL}/{fid}/permissions/{perm_id}"
            resp = self._request("DELETE", url, drive_id,
                                 params={"supportsAllDrives": "true"})
            if resp.status_code not in (200, 204, 404):
                raise Exception(f"删 anyone 权限失败：{resp.status_code}")
        return "revoked"

    def delete_file_ignore_missing(self, drive_id, fid):
        """删除用户盘里的单个文件（闲置 TTL 删除 pass 专用）。

        与 Path B 的 delete_file（404 抛异常、调用方忽略）区分：
        本方法 404 算成功（文件已不在 = 目标已达成）；其它非 2xx
        抛异常，调用方不删变体行、不谎报删除。单列方法名，避免
        改动 Path B 既有语义。
        """
        url = f"{FILES_URL}/{fid}"
        resp = self._request("DELETE", url, drive_id,
                             params={"supportsAllDrives": "true"})
        if resp.status_code not in (200, 204, 404):
            raise Exception(f"删文件失败：{resp.status_code}")

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

    def download_range(self, drive_id, fid, start, end):
        """跨 provider 搬字节：Drive 源按 Range 取一段字节。

        FMBY open_range 口径：401/403 → _CrossAuthError（源凭据
        失效，终态）；其它失败（非 206、短读、网络异常）→
        _CrossRemoteError（有界重试）。401 时先按 _request 同款
        逻辑强制刷新令牌重试一次（SA 轮换内部处理），刷新后仍
        401/403 才算凭据失效。返回字节数必须等于请求段长，短读
        即失败（不静默补齐，避免静默损坏文件）。
        """
        url = f"{FILES_URL}/{fid}"
        params = {"supportsAllDrives": "true", "alt": "media"}
        headers = dict(self._headers(drive_id))
        headers["Range"] = f"bytes={start}-{end}"
        try:
            resp = httpx.request("GET", url, params=params,
                                 headers=headers, timeout=120)
        except (httpx.TimeoutException, httpx.ConnectError,
                httpx.RemoteProtocolError, httpx.ReadError) as exc:
            raise _CrossRemoteError(
                f"Drive 源下载网络异常：{type(exc).__name__}") from exc
        if resp.status_code == 401:
            headers = dict(self._headers(drive_id, force_refresh=True))
            headers["Range"] = f"bytes={start}-{end}"
            try:
                resp = httpx.request("GET", url, params=params,
                                     headers=headers, timeout=120)
            except (httpx.TimeoutException, httpx.ConnectError,
                    httpx.RemoteProtocolError, httpx.ReadError) as exc:
                raise _CrossRemoteError(
                    f"Drive 源下载网络异常（刷新后）：{type(exc).__name__}"
                    ) from exc
        if resp.status_code in (401, 403):
            raise _CrossAuthError(
                f"Drive 源下载被拒：{resp.status_code}（源凭据失效）")
        if resp.status_code != 206:
            raise _CrossRemoteError(
                f"Drive 源下载失败：{resp.status_code}（非 206）")
        data = resp.content or b""
        if len(data) != end - start + 1:
            raise _CrossRemoteError(
                f"Drive 源下载短读：要 {end - start + 1} 字节，"
                f"实得 {len(data)} 字节")
        return data

    def find_file_by_name(self, drive_id, folder_id, name):
        """在指定目录下按精确文件名找文件 id（Path B 暂存复用）。

        找不到返回 None；查询失败抛 _Retriable（调用方决定重试）。
        精确名匹配：暂存文件名含任务 id（tmp-<jobid>-<原名>），重试
        时命中即复用，不重复建暂存副本。
        """
        escaped = name.replace("'", "\\'")
        q = (f"name='{escaped}' and '{folder_id}' in parents "
             f"and trashed=false")
        params = {"q": q, "supportsAllDrives": "true",
                  "fields": "files(id,name)"}
        params.update(self._list_params(drive_id))
        resp = self._request("GET", FILES_URL, drive_id, params=params)
        if resp.status_code == 200:
            files = (resp.json() or {}).get("files") or []
            if files:
                return str(files[0]["id"])
            return None
        raise _Retriable(f"按名查找文件失败：{resp.status_code}")

    def list_file_ids_by_name(self, drive_id, folder_id, name):
        """在指定目录下按精确文件名列出全部文件 id（转存后目录核对用）。

        返回 id 列表（可能为空）；查询失败抛 _Retriable（调用方决定
        重试）。与 find_file_by_name 同查询口径，区别是返回全部命中
        而非首个——核对时要区分“恰好一个新文件”与“多个同名冲突”
        （FMBY discover_transfer_target 口径）。
        """
        escaped = name.replace("'", "\\'")
        q = (f"name='{escaped}' and '{folder_id}' in parents "
             f"and trashed=false")
        params = {"q": q, "supportsAllDrives": "true",
                  "fields": "files(id,name)"}
        params.update(self._list_params(drive_id))
        resp = self._request("GET", FILES_URL, drive_id, params=params)
        if resp.status_code == 200:
            files = (resp.json() or {}).get("files") or []
            return [str(f["id"]) for f in files if f.get("id")]
        raise _Retriable(f"按名列出文件失败：{resp.status_code}")

    def delete_file(self, drive_id, fid):
        """删文件（Path B 成功后清暂存，尽力而为）。

        非 2xx 抛异常，调用方忽略（暂存残留记账为已知 v1 缺陷，
        不阻塞任务成功）。
        """
        url = f"{FILES_URL}/{fid}"
        resp = self._request("DELETE", url, drive_id,
                             params={"supportsAllDrives": "true"})
        if resp.status_code not in (200, 204):
            raise Exception(f"删文件失败：{resp.status_code}")


class _RawTokenClient(DriveApiClient):
    """裸令牌 Drive 客户端（仅供绑定 invalid 钩子用）。

    只用调用方给定的已存 access_token 发请求，绝不刷新——invalid
    钩子本身就跑在刷新失败路径里，再刷新会递归。只复用父类的
    list_permissions / remove_anyone_reader / delete_file_ignore_missing
    （它们只调 self._request，不碰令牌解析），其余方法一律不用。
    注意：本类必须放在 DriveApiClient 完整定义之后、
    UserDriveApiClient 之前——插进 DriveApiClient 类体中间会把
    后续方法误收进本类（2026-10-28 自查实证）。
    """

    def __init__(self, token):
        self._raw_token = token

    def _request(self, method, url, drive_id, **kwargs):
        kwargs.setdefault("timeout", 60)
        return httpx.request(
            method, url,
            headers={"Authorization": f"Bearer {self._raw_token}"}, **kwargs)


class UserDriveApiClient(DriveApiClient):
    """用户令牌模式的 Drive 客户端（Path A / Path B 用户侧）。

    与 SA 模式的区别只有凭据来源：固定用指定绑定行的用户令牌，
    不走 get_token_for_drive 轮换，绝不替调用方换绑定行（跨绑定
    复用凭据是红线，见 drive_oauth.get_binding_drive_access_token
    文档）。drive_id 参数只用于 personal/shared 分支判断——调用方
    一律传 ``myDrive:<uid>`` 形标记，触发 personal 分支（root 根、
    spaces=drive）；令牌本身不依赖该标记。
    401 时按绑定行重取一次令牌（drive_oauth 内部含刷新逻辑；刷新
    失败抛 DriveOAuthError，绑定行由 drive_oauth 内部落失败态）。
    """

    def __init__(self, db, binding):
        self._db = db
        self._binding = binding
        self._token = None

    def _headers(self, drive_id, force_refresh=False):
        if self._token is None or force_refresh:
            from backend.emby_server import drive_oauth
            # force_refresh 必须直通令牌口：此前这里吞掉了该参数，
            # 401 重试拿回的仍是同一枚未过期旧令牌，重试必再 401。
            token = drive_oauth.get_binding_drive_access_token(
                self._db, self._binding, force_refresh=force_refresh)
            if not token:
                raise _Retriable("用户 Drive 凭据不可用")
            self._token = token
        return {"Authorization": f"Bearer {self._token}"}

    def ensure_folder_cached(self, drive_id, dir_name):
        """带缓存命中的目录解析，返回 (folder_id, from_cache)。

        缓存键含绑定指纹（见 _folder_cache_key），重绑即失效；
        TTL 默认 3600 秒。调用方（Path A）用 from_cache 决定：
        copy 被拒时若是缓存目录，先失效重解析再拷一次，第二次
        仍被拒才算源无权、走 Path B——避免“目录被删”被误读成
        “源无权”而走错路。
        """
        key = _folder_cache_key(self._binding, drive_id, dir_name)
        now = time.monotonic()
        with _transfer_folder_cache_lock:
            entry = _transfer_folder_cache.get(key)
            if entry is not None and entry[1] > now:
                return entry[0], True
        fid = super().ensure_folder(drive_id, dir_name)
        with _transfer_folder_cache_lock:
            _transfer_folder_cache[key] = (
                fid, time.monotonic() + transfer_folder_cache_ttl_sec())
        return fid, False

    def ensure_folder(self, drive_id, dir_name):
        """转存目录解析（用户盘走缓存；SA 侧父类方法不缓存）。"""
        fid, _ = self.ensure_folder_cached(drive_id, dir_name)
        return fid

    def drop_folder_cache(self, drive_id, dir_name) -> None:
        """丢掉该绑定该目录的缓存条目（Path A 缓存重试用）。"""
        key = _folder_cache_key(self._binding, drive_id, dir_name)
        with _transfer_folder_cache_lock:
            _transfer_folder_cache.pop(key, None)

    def resumable_upload(self, drive_id, name, folder_id, size, reader):
        """跨 provider 搬字节：用户 Drive 令牌可续传上传（标准协议）。

        POST upload/drive/v3/files?uploadType=resumable 取会话 URI
        （元数据 name + parents，X-Upload-Content-Length=size）→
        按 _CROSS_SEGMENT 分段 PUT（Content-Range: bytes s-e/size）：
        200/201 回文件 id；308 按 Range 头续传；401 刷新令牌重试
        当段一次。会话 URI 只活在本任务尝试内，不持久化——中途
        失败整单重来时重建会话（v1 已知代价，与搬运层不静默重试
        一致）。上传侧 403 不按凭据过期算（可能是空间/配额），
        一律 _CrossRemoteError 走有界重试；令牌刷新失败抛
        DriveOAuthError（调用方记 no_binding，与 Path A 同口径）。
        reader 须支持 seek/tell/read 的文件类对象。
        """
        from backend.emby_server.drive_oauth import DriveOAuthError
        params = {"uploadType": "resumable", "supportsAllDrives": "true"}
        meta = {"name": name, "parents": [folder_id]}
        headers = dict(self._headers(drive_id))
        headers["X-Upload-Content-Type"] = "application/octet-stream"
        headers["X-Upload-Content-Length"] = str(size)
        try:
            resp = httpx.request("POST", _DRIVE_UPLOAD_URL, params=params,
                                 json=meta, headers=headers, timeout=120)
        except (httpx.TimeoutException, httpx.ConnectError,
                httpx.RemoteProtocolError, httpx.ReadError) as exc:
            raise _CrossRemoteError(
                f"Drive 可续传会话创建网络异常：{type(exc).__name__}"
                ) from exc
        if resp.status_code == 401:
            headers = dict(self._headers(drive_id, force_refresh=True))
            headers["X-Upload-Content-Type"] = "application/octet-stream"
            headers["X-Upload-Content-Length"] = str(size)
            try:
                resp = httpx.request("POST", _DRIVE_UPLOAD_URL, params=params,
                                     json=meta, headers=headers, timeout=120)
            except (httpx.TimeoutException, httpx.ConnectError,
                    httpx.RemoteProtocolError, httpx.ReadError) as exc:
                raise _CrossRemoteError(
                    f"Drive 可续传会话创建网络异常（刷新后）："
                    f"{type(exc).__name__}") from exc
        session_uri = (resp.headers.get("location") or "").strip()
        if resp.status_code != 200 or not session_uri:
            raise _CrossRemoteError(
                f"Drive 可续传会话创建失败：{resp.status_code}")
        pos = 0
        refreshed = False
        guard = (size + _CROSS_SEGMENT - 1) // _CROSS_SEGMENT + 5
        while pos < size and guard > 0:
            guard -= 1
            end = min(pos + _CROSS_SEGMENT, size) - 1
            reader.seek(pos)
            chunk = reader.read(end - pos + 1)
            if len(chunk) != end - pos + 1:
                raise _CrossRemoteError(
                    f"搬字节读取短读：段 {pos}-{end} 实得 {len(chunk)} 字节")
            put_headers = dict(self._headers(drive_id))
            put_headers["Content-Range"] = f"bytes {pos}-{end}/{size}"
            put_headers["Content-Length"] = str(len(chunk))
            try:
                put = httpx.request("PUT", session_uri, headers=put_headers,
                                    content=chunk, timeout=300)
            except (httpx.TimeoutException, httpx.ConnectError,
                    httpx.RemoteProtocolError, httpx.ReadError) as exc:
                raise _CrossRemoteError(
                    f"Drive 可续传分段上传网络异常：{type(exc).__name__}"
                    ) from exc
            if put.status_code == 401 and not refreshed:
                refreshed = True
                self._headers(drive_id, force_refresh=True)
                continue
            if put.status_code in (200, 201):
                data = put.json() if put.content else {}
                fid = str((data or {}).get("id") or "")
                if not fid:
                    raise _CrossRemoteError("Drive 可续传完成未返回文件 id")
                return fid
            if put.status_code == 308:
                ranged = (put.headers.get("range") or "").strip()
                if ranged.startswith("bytes=0-"):
                    try:
                        pos = int(ranged[len("bytes=0-"):]) + 1
                        continue
                    except ValueError:
                        pass
                raise _CrossRemoteError(
                    "Drive 可续传 308 未带可用 Range 头，无法续传")
            raise _CrossRemoteError(
                f"Drive 可续传分段上传失败：{put.status_code}")
        raise _CrossRemoteError("Drive 可续传上传循环超限（疑似 308 死循环）")


class _SegmentReader:
    """跨 provider 搬字节的分段读取器（内存 O(段大小)，不落盘）。

    fetch(start, end) -> bytes（闭区间）由调用方提供（Drive 源
    download_range 或 115 下载直链 Range GET）。内部按
    _CROSS_SEGMENT 缓存当前窗口；read/seek/tell 语义与文件对象
    一致，供 httpx 表单上传（seek/tell 算 Content-Length）与
    Drive 可续传上传（seek 到段首重读）共用。seek 越界抛
    ValueError；read 到文件尾返回 b""。
    """

    def __init__(self, size, fetch):
        self._size = int(size)
        self._fetch = fetch
        self._pos = 0
        self._win_start = 0
        self._win_data = b""

    def tell(self):
        return self._pos

    def seek(self, offset, whence=0):
        if whence == 0:
            pos = offset
        elif whence == 1:
            pos = self._pos + offset
        elif whence == 2:
            pos = self._size + offset
        else:
            raise ValueError(f"非法 whence：{whence}")
        if pos < 0 or pos > self._size:
            raise ValueError(f"seek 越界：{pos}（大小 {self._size}）")
        self._pos = pos
        return pos

    def read(self, n=-1):
        if n is None or n < 0:
            n = self._size - self._pos
        if n <= 0 or self._pos >= self._size:
            return b""
        out = bytearray()
        while len(out) < n and self._pos < self._size:
            if not (self._win_start <= self._pos
                    < self._win_start + len(self._win_data)):
                win_end = min(self._pos + _CROSS_SEGMENT,
                              self._size) - 1
                self._win_start = self._pos
                self._win_data = self._fetch(self._pos, win_end)
                if len(self._win_data) != win_end - self._pos + 1:
                    raise _CrossRemoteError(
                        f"搬字节取段短读：{self._pos}-{win_end} 实得 "
                        f"{len(self._win_data)} 字节")
            off = self._pos - self._win_start
            take = min(n - len(out), len(self._win_data) - off)
            out += self._win_data[off:off + take]
            self._pos += take
        return bytes(out)


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


def _fail_job(db, job, kind, detail):
    """任务置 failed 并提交（工人各执行器共用）。"""
    job.status = "failed"
    job.error_kind = kind
    job.error_detail = (detail or "")[:500]
    db.commit()
    return {"job_id": job.id, "outcome": "failed", "error_kind": kind}


def _register_ready_variant(db, job, item, variant_fid, variant_drive_id,
                            pickcode=None, size=None):
    """成功登记：先释放同归属旧 ready 行，再建新 ready 行并置任务 done。

    用户 scope 必须带 owner_ref（_release_ready_rows 内强制过滤，
    绝不误释他人行）。
    """
    owner_ref = getattr(job, "owner_ref", None)
    _release_ready_rows(db, job.item_id,
                        getattr(job, "owner_scope", "source") or "source",
                        owner_ref)
    row = em.TransferVariant(
        item_id=job.item_id, source_file_id=item.drive_file_id.strip(),
        variant_file_id=variant_fid, variant_drive_id=variant_drive_id,
        owner_scope=getattr(job, "owner_scope", "source") or "source",
        owner_ref=owner_ref, status="ready", pickcode=pickcode,
        size_bytes=_to_int(size) or _to_int(getattr(item, "size", None)))
    db.add(row)
    job.status = "done"
    job.error_kind = None
    job.error_detail = None
    job.next_retry_at = None
    db.commit()
    # 登记 ready 后预填存在性正缓存（只对 Drive 副本；115 副本的
    # variant_file_id 不是 Drive fid，不进 Drive 存在性命名空间）。
    # 预填失败不影响登记（publish 内部永不抛错）。
    if not (variant_drive_id or "").startswith("pan115:"):
        publish_drive_exists_ok(variant_fid)
    return row


def _verify_drive_copy_in_dir(client, drive_id, folder_id, name,
                             expected_fid):
    """转存后目录核对（FMBY discover_transfer_target 口径）。

    列出目标目录中与副本同名的文件：
    - 回执 id 在其中 → 核到，返回回执 id；
    - 恰好一个同名文件（回执 id 不在其中）→ 核到，返回目录里的 id
      （目录为准）；
    - 一个都没有，或多个同名且无一是回执 id → 核不到，返回 ""。
    调用方核不到时记 failed/copy_unresolved，不重试（避免重复 copy
    产生重复文件），绝不回落源文件播放。目录列举本身失败时抛异常
    （调用方按可重试处理，不记 copy_unresolved）。
    """
    matches = client.list_file_ids_by_name(drive_id, folder_id, name)
    if expected_fid and expected_fid in matches:
        return expected_fid
    if len(matches) == 1:
        return matches[0]
    return ""


def _fresh_quota_numbers(user_client, user_drive, file_size):
    """二次配额失败时的三数复核（只读 about，不腾地方）。

    返回 (free_bytes, shortfall)，about 拿不到时均为 None。
    本函数绝不调用 evict：二次失败时再腾就是无限循环的口子。
    """
    try:
        quota = user_client.about_storage(user_drive)
    except Exception:  # noqa: BLE001 — 复核失败不掩盖配额终态
        return None, None
    free = (quota or {}).get("free")
    if free is None or file_size is None:
        return free, None
    return int(free), int(file_size) - int(free)


def _attempt_user_copy(db, user_client, user_drive, orig_fid, folder_id,
                       name, space_factory, user_id, item_id, file_size,
                       quota_state):
    """用户盘 Path A copy（含 copy 中途配额的一次腾+重试）。

    正常 copy 直接返回回执。copy 抛 _CopyQuotaExceeded 时：
    - 本任务此前未腾过（quota_state["retried"] 为假）：调
      ensure_space 腾一次；腾后仍不足（reason == insufficient）
      → 抛 _SpaceInsufficient（调用方 _fail_job，不走 Path B）；
      否则重试 copy 一次；
    - 重试的那一次仍抛配额错，或本任务已经腾过一次：不再腾、
      不再重试，直接抛 _SpaceInsufficient（三数用 about 复核，
      复核不到时 free/shortfall 为 None）。
    _CopyForbidden 原样上抛（调用方按旧口径走文件夹缓存重试
    或 Path B）；其它异常原样上抛。quota_state 由调用方在整
    个 Path A 任务内共享（初始 copy 与文件夹缓存重试 copy 共
    用），保证一个任务内 copy 中途的腾+重试至多一轮，绝不
    因两个 copy 点各腾一次而变相循环。
    """
    from backend.emby_server import transfer_cleanup as tc
    try:
        return user_client.copy_file(user_drive, orig_fid, folder_id,
                                     name)
    except _CopyQuotaExceeded:
        pass
    if quota_state.get("retried"):
        free, shortfall = _fresh_quota_numbers(
            user_client, user_drive, file_size)
        raise _SpaceInsufficient(file_size, free, shortfall)
    quota_state["retried"] = True
    space = tc.ensure_space_for_transfer(
        db, space_factory, user_id, file_size=file_size,
        exclude_item_ids=[item_id])
    if space["reason"] == "insufficient":
        raise _SpaceInsufficient(
            file_size, space["free_bytes"], space["shortfall"],
            evicted=space["evicted"], bytes_freed=space["bytes_freed"])
    try:
        return user_client.copy_file(user_drive, orig_fid, folder_id,
                                     name)
    except _CopyQuotaExceeded:
        free, shortfall = _fresh_quota_numbers(
            user_client, user_drive, file_size)
        raise _SpaceInsufficient(
            file_size, free, shortfall, evicted=space["evicted"],
            bytes_freed=space["bytes_freed"])


def _run_drive_user_job(db, job, item, drive_api):
    """Drive 用户盘任务：Path A 用户令牌直拷，403/404 转 Path B。

    任务行 drive_id=源盘、dest_drive_id=userdrive:<uid>；登记行
    owner_scope=user、variant_drive_id=userdrive:<uid>（播放链沿用
    SA 轮换读 anyone-reader 副本，中继/302 均不改）。
    必须是真 copy（files.copy），快捷方式/添加到云端硬盘不算。
    转存前量空间：copy 前调 ensure_space_for_transfer（剩余 <
    文件大小时先腾，腾完仍不足直接 _fail_job insufficient_space，
    绝不静默回落共享）；copy 中途配额错走 _attempt_user_copy
    的腾一次重试一次（见该函数说明）。
    """
    from backend.emby_server.drive_oauth import DriveOAuthError
    uid = (getattr(job, "owner_ref", "") or "").strip()
    try:
        user_id = int(uid)
    except (TypeError, ValueError):
        user_id = None
    binding = _active_binding(db, user_id, "drive") if user_id else None
    if binding is None:
        return _fail_job(db, job, "no_binding",
                         "用户 Drive 绑定已失效或不存在（任务创建后解绑，"
                         "不重试；重绑后下次播放节流窗外可重建）")
    user_client = UserDriveApiClient(db, binding)
    user_drive = f"myDrive:{user_id}"
    orig_fid = item.drive_file_id.strip()
    name = _copy_name_for_item(item)
    # 转存前量空间用的工厂：只返回本任务已建好的用户客户端，
    # 绝不替调用方换绑定；其它用户一律无客户端（行不动）。
    from backend.emby_server import transfer_cleanup as _tc
    _space_factory = (lambda db_, uid: (user_client, user_drive)
                      if uid == user_id else (None, None))
    # copy 中途配额腾+重试的共享旗标（初始 copy 与文件夹缓存
    # 重试 copy 共用，一个任务内至多一轮）。
    _quota_state = {"retried": False}
    _file_size = _to_int(getattr(item, "size", None))
    # Path A：用户令牌直拷（anyone-reader 源）。
    # 目录解析走缓存（实测 0.25 秒分段）：假客户端（旧测试桩）
    # 没有 ensure_folder_cached 时退回 ensure_folder，行为逐字旧。
    folder_from_cache = False
    try:
        if hasattr(user_client, "ensure_folder_cached"):
            folder_id, folder_from_cache = user_client.ensure_folder_cached(
                user_drive, transfer_dir_name())
        else:
            folder_id = user_client.ensure_folder(user_drive,
                                                  transfer_dir_name())
        # 转存前量空间：剩余 < 文件大小时先腾，腾完仍不足直接
        # 明确失败（绝不静默回落共享）；其它 reason 照旧走 copy。
        _space = _tc.ensure_space_for_transfer(
            db, _space_factory, user_id, file_size=_file_size,
            exclude_item_ids=[item.id])
        if _space["reason"] == "insufficient":
            return _fail_job(
                db, job, "insufficient_space",
                f"用户盘空间不足：文件 {_space['file_size']} 字节，"
                f"剩余 {_space['free_bytes']} 字节，还差 "
                f"{_space['shortfall']} 字节（已腾 "
                f"{len(_space['evicted'])} 条、"
                f"{_space['bytes_freed']} 字节），未静默回落共享")
        copied = _attempt_user_copy(
            db, user_client, user_drive, orig_fid, folder_id, name,
            _space_factory, user_id, item.id, _file_size, _quota_state)
    except _CopyForbidden:
        if folder_from_cache:
            # 缓存目录可能已被删/换绑：先失效重解析再拷一次，
            # 第二次仍被拒才算源无权、走 Path B（不把目录问题
            # 误读成源无权）。
            try:
                if hasattr(user_client, "drop_folder_cache"):
                    user_client.drop_folder_cache(user_drive,
                                                  transfer_dir_name())
                folder_id = user_client.ensure_folder(user_drive,
                                                      transfer_dir_name())
                copied = _attempt_user_copy(
                    db, user_client, user_drive, orig_fid, folder_id,
                    name, _space_factory, user_id, item.id, _file_size,
                    _quota_state)
            except _CopyForbidden:
                return _run_drive_user_job_path_b(
                    db, job, item, drive_api, user_client, user_drive, name)
            except _SpaceInsufficient as exc:
                return _fail_job(db, job, "insufficient_space", str(exc))
            except DriveOAuthError as exc:
                return _fail_job(db, job, "no_binding",
                                 f"用户 Drive 令牌失效：{exc}")
            except Exception as exc:
                return {"job_id": job.id,
                        "outcome": _schedule_retry(db, job, exc)}
        else:
            # 用户无权源 → Path B（不直接判死，SA 暂存中转）。
            return _run_drive_user_job_path_b(db, job, item, drive_api,
                                              user_client, user_drive, name)
    except _SpaceInsufficient as exc:
        return _fail_job(db, job, "insufficient_space", str(exc))
    except DriveOAuthError as exc:
        return _fail_job(db, job, "no_binding",
                         f"用户 Drive 令牌失效：{exc}")
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    variant_fid = (copied or {}).get("id") or ""
    if not variant_fid:
        return {"job_id": job.id,
                "outcome": _schedule_retry(
                    db, job, Exception("copy 未返回新 file_id"))}
    # 转存后目录核对（FMBY discover 口径）：copy 回执只作期望值，
    # 必须在用户转存目录里按名核到才登记；核不到记 copy_unresolved
    #（不重试、不回落源文件播放），目录列举失败则按可重试处理。
    # 注意（2026-10-27 极限优化时亲验）：核对与加 anyone-reader
    # 不能并行——anyone 必须加在核到的 id 上（目录 id 胜出时与回
    # 执 id 不同），且核不到时绝不能补权限（旧测试
    # test_worker_user_path_a_dir_missing_copy_unresolved /
    # test_worker_user_path_a_dir_id_wins 钉住这两条）。并行省
    # 0.24 秒的代价是改这两条语义，不值得；提速只靠目录缓存。
    try:
        verified_fid = _verify_drive_copy_in_dir(
            user_client, user_drive, folder_id, name, variant_fid)
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    if not verified_fid:
        return _fail_job(
            db, job, "copy_unresolved",
            f"Drive copy 回执 id={variant_fid} 在用户转存目录中核不到"
            f"（目录 id={folder_id}，文件名={name}），不登记、不回落")
    variant_fid = verified_fid
    try:
        user_client.add_anyone_reader(user_drive, variant_fid)
    except Exception as exc:
        # 副本已建但不可播：记 orphan，不删文件，等管理员处置。
        row = em.TransferVariant(
            item_id=job.item_id, source_file_id=orig_fid,
            variant_file_id=variant_fid,
            variant_drive_id=f"userdrive:{user_id}",
            owner_scope="user", owner_ref=uid, status="orphan",
            size_bytes=_to_int((copied or {}).get("size"))
            or _to_int(getattr(item, "size", None)))
        db.add(row)
        return _fail_job(db, job, "orphan", str(exc))
    row = _register_ready_variant(db, job, item, variant_fid,
                                  f"userdrive:{user_id}",
                                  size=(copied or {}).get("size"))
    return {"job_id": job.id, "outcome": "done", "variant_id": row.id}


def _is_file_only_binding(binding) -> bool:
    """绑定是否纯 file-only（有 drive.file、无 drive.readonly）。

    实测定性（2026-10-29 copy 404）：file-only 令牌对非应用自创
    文件（源共享盘原件、SA 暂存副本）files.get 一律 404，files.copy
    永不可能成功；双范围（带 readonly）令牌可见 anyone-reader 源，
    copy 404 只是分享传播延迟、可重试。调用方据此分流：file-only
    的 Path B 用户 copy 被拒时直接明确失败
    （file_only_copy_unavailable），不进重试循环、不走搬字节
    （搬字节路线 2026-10-29 已整条关停）。
    binding 为 None 或桩对象无 granted_scopes 时按非 file-only
    计（走旧口径重试），绝不因桩缺字段改变老行为。
    """
    from backend.emby_server.drive_oauth import (
        DRIVE_FILE_SCOPE, DRIVE_READONLY_SCOPE, granted_scope_set)
    scopes = granted_scope_set(getattr(binding, "granted_scopes", None))
    return DRIVE_FILE_SCOPE in scopes and DRIVE_READONLY_SCOPE not in scopes


def _run_drive_user_job_path_b(db, job, item, drive_api, user_client,
                               user_drive, name):
    """Path B：SA 暂存中转（用户无权源）。

    SA 在源盘 aetrix-transfer 目录下建 tmp-<jobid>-<原名> 暂存副本
    （扫描排除目录，暂存文件不入库）→ anyone-reader 分享给用户 →
    用户令牌再拷进自己盘 → anyone-reader → 登记用户行 → 尽力删暂存。
    SA copy 403/404 → dead；暂存分享后用户 copy 403 → 传播延迟，
    走 retriable（重试时按暂存名复用已建暂存，不重复建）。
    成功后暂存删不掉时落 orphan 登记，清理周期的暂存扫荡
    重试删除（2026-10-30 安全收口，不再无声堆积）。
    """
    from backend.emby_server.drive_oauth import DriveOAuthError
    uid = (getattr(job, "owner_ref", "") or "").strip()
    orig_fid = item.drive_file_id.strip()
    try:
        tmp_folder = drive_api.ensure_folder(job.drive_id,
                                             transfer_dir_name())
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    tmp_name = f"tmp-{job.id}-{name}"
    try:
        tmp_fid = drive_api.find_file_by_name(job.drive_id, tmp_folder,
                                              tmp_name)
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    if not tmp_fid:
        try:
            tmp_copied = drive_api.copy_file(job.drive_id, orig_fid,
                                             tmp_folder, tmp_name)
        except _CopyForbidden as exc:
            # SA 也无权/源已死：真死文件，不重试。
            return _fail_job(db, job, "dead", str(exc))
        except Exception as exc:
            return {"job_id": job.id,
                    "outcome": _schedule_retry(db, job, exc)}
        tmp_fid = (tmp_copied or {}).get("id") or ""
        if not tmp_fid:
            return {"job_id": job.id,
                    "outcome": _schedule_retry(
                        db, job, Exception("SA 暂存 copy 未返回新 file_id"))}
    try:
        drive_api.add_anyone_reader(job.drive_id, tmp_fid)
    except Exception as exc:
        # 暂存已建但无法分享：记 orphan（暂存文件留待人工清理）。
        row = em.TransferVariant(
            item_id=job.item_id, source_file_id=orig_fid,
            variant_file_id=tmp_fid, variant_drive_id=job.drive_id,
            owner_scope="user", owner_ref=uid, status="orphan",
            size_bytes=_to_int(getattr(item, "size", None)))
        db.add(row)
        return _fail_job(db, job, "orphan", str(exc))
    try:
        user_folder = user_client.ensure_folder(user_drive,
                                                transfer_dir_name())
        copied = user_client.copy_file(user_drive, tmp_fid, user_folder,
                                       name)
    except _CopyForbidden as exc:
        if not _is_file_only_binding(getattr(user_client, "_binding", None)):
            # 双范围：分享传播延迟，可重试（重试复用暂存）。
            return {"job_id": job.id,
                    "outcome": _schedule_retry(db, job, exc)}
        # file-only：令牌看不见非应用自创文件（源与暂存 get 均实测
        # 404），user copy 永不可能成功。#1470 曾改走搬字节（SA 按
        # 段下载暂存 → 用户令牌可续传上传），2026-10-29 用户亲令
        # 搬字节路线整条取消——直接明确失败，等只读授权
        # （drive.readonly）恢复、用户重新绑定后再播；不回落搬
        # 字节、不回落共享、不进重试循环。
        return _fail_job(db, job, FILE_ONLY_COPY_UNAVAILABLE_KIND,
                         FILE_ONLY_COPY_UNAVAILABLE_DETAIL)
    except DriveOAuthError as exc:
        return _fail_job(db, job, "no_binding",
                         f"用户 Drive 令牌失效：{exc}")
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    variant_fid = (copied or {}).get("id") or ""
    if not variant_fid:
        return {"job_id": job.id,
                "outcome": _schedule_retry(
                    db, job, Exception("用户盘 copy 未返回新 file_id"))}
    # 转存后目录核对（与 Path A 同口径）：用户盘 copy 回执只作期望值，
    # 必须在用户转存目录里按名核到才登记；核不到记 copy_unresolved
    #（不重试、不回落源文件播放），目录列举失败则按可重试处理。
    # 注意：此时暂存尚未删（删暂存只在成功登记后），核不到失败时暂存
    # 留待下次播放重建任务时按名复用，不在此处删。
    try:
        verified_fid = _verify_drive_copy_in_dir(
            user_client, user_drive, user_folder, name, variant_fid)
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    if not verified_fid:
        return _fail_job(
            db, job, "copy_unresolved",
            f"Drive 用户盘 copy 回执 id={variant_fid} 在转存目录中核不到"
            f"（目录 id={user_folder}，文件名={name}），不登记、不回落")
    variant_fid = verified_fid
    try:
        user_client.add_anyone_reader(user_drive, variant_fid)
    except Exception as exc:
        row = em.TransferVariant(
            item_id=job.item_id, source_file_id=orig_fid,
            variant_file_id=variant_fid,
            variant_drive_id=f"userdrive:{uid}",
            owner_scope="user", owner_ref=uid, status="orphan",
            size_bytes=_to_int((copied or {}).get("size"))
            or _to_int(getattr(item, "size", None)))
        db.add(row)
        return _fail_job(db, job, "orphan", str(exc))
    row = _register_ready_variant(db, job, item, variant_fid,
                                  f"userdrive:{uid}",
                                  size=(copied or {}).get("size"))
    try:
        drive_api.delete_file(job.drive_id, tmp_fid)
    except Exception:
        # 暂存删不掉：落 orphan 登记进暂存扫荡重试，不再只记
        # 日志无声堆积（2026-10-30 安全收口）。
        logger.warning("Path B 暂存清理失败（已登记待扫荡） job=%s tmp=%s",
                       job.id, tmp_fid)
        _register_staging_leftover(db, job, item, uid, tmp_fid)
    return {"job_id": job.id, "outcome": "done", "variant_id": row.id}


def _make_pan115_client(cookie):
    """115 客户端构造口（测试可 monkeypatch 本模块属性替换）。

    生产路径固定用用户自己的 Cookie，绝不用服主/默认账号档。
    """
    from backend.emby_server.transfer115 import Pan115Client
    return Pan115Client(cookie)


def _ensure_115_transfer_dir(client):
    """用户盘转存目录 cid：根目录下找 aetrix-transfer，没有就建。

    建目录响应形态待 115 测试账号实测，故建完必按名重列根目录
    验 cid，不依赖 mkdir 响应字段。
    """
    name = transfer_dir_name()
    for entry in client.list_dir("0"):
        if entry.get("is_dir") and (entry.get("name") or "") == name:
            cid = entry.get("cid") or ""
            if cid:
                return cid
    client.mkdir("0", name)
    for entry in client.list_dir("0"):
        if entry.get("is_dir") and (entry.get("name") or "") == name:
            cid = entry.get("cid") or ""
            if cid:
                return cid
    from backend.emby_server.transfer115 import Pan115Error
    raise Pan115Error("115 建转存目录后仍找不到该目录")


def _extract_115_copy_result(result):
    """从 copy 响应里提新 fid + pickcode（响应形态待实测，多字段容错）。

    fid 候选：fid/file_id/id；pickcode 候选：pickcode/pick_code/pc。
    任一缺失都返回空串，调用方走列目录匹配回落。
    """
    if not isinstance(result, dict):
        return "", ""
    data = result.get("data")
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        data = result
    fid = str(data.get("fid") or data.get("file_id")
              or data.get("id") or "")
    pickcode = str(data.get("pickcode") or data.get("pick_code")
                   or data.get("pc") or "")
    return fid, pickcode


def _match_115_copy_in_dir(client, dest_cid, name, size, expected_fid=None):
    """列目标目录核对新文件（FMBY discover_transfer_target 口径）。

    返回 (fid, pickcode)，核不到返回 ("", "")。只认非目录项。
    - 回执 fid 在目录中 → 返回回执 fid 及其 pickcode；
    - 否则按名 + 大小（大小缺失时只按名）匹配，命中时返回；
    - 否则恰好一个同名文件时返回它（目录为准）；
    - 多个同名且无一是回执 id 时返回 ("", "")（冲突，不猜）。
    注意：目标目录若预先存在同名文件，单同名回落可能命中旧文件——
    回执 fid 路径是主路径，转存目录正常为空（用户自建同名文件是
    边缘情形，见报告已知项）；多同名冲突时宁可记 copy_unresolved
    也不猜一个登记。
    """
    size_int = _to_int(size)
    matches = []
    for entry in client.list_dir(dest_cid):
        if entry.get("is_dir"):
            continue
        if (entry.get("name") or "") != name:
            continue
        matches.append(entry)
    if not matches:
        return "", ""
    if expected_fid:
        for entry in matches:
            if (entry.get("fid") or "") == expected_fid:
                return expected_fid, entry.get("pickcode") or ""
    if size_int:
        for entry in matches:
            if _to_int(entry.get("size")) == size_int:
                return entry.get("fid") or "", entry.get("pickcode") or ""
    if len(matches) == 1:
        entry = matches[0]
        return entry.get("fid") or "", entry.get("pickcode") or ""
    return "", ""


def _run_pan115_job(db, job, item):
    """115 用户盘任务：同 provider 内服务端 copy（不搬字节）。

    凭据：用户自己的 115 Cookie（唯一口 binding_115_cookie，
    绝不用服主档）。
    新 fid + pickcode 登记用户行；Cookie 失效 → failed/auth_error
    + 绑定落 expired（user_bindings._mark_failed），不重试、不回落。
    copy 后必列目标目录核对（回执只作期望值，目录核到的 id 为准）；
    核不到 → failed/copy_unresolved（不重试，避免重复 copy 产生重复文件）。
    """
    from backend.emby_server.transfer115 import Pan115AuthError, Pan115Error
    uid = (getattr(job, "owner_ref", "") or "").strip()
    try:
        user_id = int(uid)
    except (TypeError, ValueError):
        user_id = None
    orig_fid = (item.drive_file_id or "").strip()
    if not orig_fid:
        return _fail_job(db, job, "no_source_fid",
                         "115 条目无源 fid，无法转存（等扫描补 fid）")
    binding = _active_binding(db, user_id, "pan115") if user_id else None
    if binding is None:
        return _fail_job(db, job, "no_binding",
                         "用户 115 绑定已失效或不存在（不重试）")
    # 凭据唯一口：binding_115_cookie（含本人/115 行/凭据归属校验，
    # 解封失败时 helper 内部已落 auth_error 失败态）。
    try:
        from backend.emby_server import user_bindings as ub
        cookie = ub.binding_115_cookie(db, user_id, binding.id)
    except ub.BindingError as exc:
        if exc.kind == "auth_error":
            return _fail_job(db, job, "auth_error", str(exc))
        if exc.kind == "not_found":
            return _fail_job(db, job, "no_binding", str(exc))
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    try:
        client = _make_pan115_client(cookie)
        dest_cid = _ensure_115_transfer_dir(client)
        result = client.copy_file(orig_fid, dest_cid)
        resp_fid, _resp_pickcode = _extract_115_copy_result(result)
        # 转存后目录核对（FMBY discover 口径）：copy 回执只作期望值，
        # 必须在目标目录里按名核到才登记；核不到记 copy_unresolved
        #（不重试，避免重复 copy 产生重复文件；不回落源文件播放）。
        new_fid, pickcode = _match_115_copy_in_dir(
            client, dest_cid, _copy_name_for_item(item),
            getattr(item, "size", None), expected_fid=resp_fid or None)
        if not new_fid:
            return _fail_job(
                db, job, "copy_unresolved",
                f"115 copy 完成但目标目录核不到新文件（源 fid={orig_fid}，"
                f"回执 fid={resp_fid or '无'}，目标目录 cid={dest_cid}，"
                f"请人工核对，不自动重试）")
    except Pan115AuthError as exc:
        try:
            from backend.emby_server import user_bindings as ub
            ub._mark_failed(db, binding, "expired")
        except Exception:
            pass
        return _fail_job(db, job, "auth_error", str(exc))
    except Pan115Error as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    row = _register_ready_variant(db, job, item, new_fid,
                                  f"pan115:user-{uid}",
                                  pickcode=pickcode or None)
    return {"job_id": job.id, "outcome": "done", "variant_id": row.id}


def _find_115_pickcode_by_fid(client, fid):
    """老行 pickcode 回落：根目录 → aetrix-transfer 目录 → 按 fid 匹配。

    找不到返回 ""。只查转存目录（副本只应在此目录）。
    """
    if not fid:
        return ""
    name = transfer_dir_name()
    for entry in client.list_dir("0"):
        if entry.get("is_dir") and (entry.get("name") or "") == name:
            cid = entry.get("cid") or ""
            if not cid:
                continue
            for item in client.list_dir(cid):
                if not item.get("is_dir") and (item.get("fid") or "") == fid:
                    return item.get("pickcode") or ""
            return ""
    return ""


def _patrol_115_row(db, row, stats, now):
    """115 副本巡检：download_url(存量 pickcode) 验活。

    - 成功 → strike 清零；
    - 认证错（Cookie 失效）→ 不记 strike（不是副本的错，绑定落态
      由用户下次播放/工人处理，巡检不改绑定）；
    - 其它错 → strike，满 2 次 stale；
    - pickcode 为空的老行 → 目录匹配回落补 pickcode；
    - 绑定已无 → 跳过（不记 strike、不改行）。
    """
    from backend.emby_server.transfer115 import Pan115AuthError, Pan115Error
    uid = (row.owner_ref or "").strip()
    try:
        user_id = int(uid)
    except (TypeError, ValueError):
        user_id = None
    binding = _active_binding(db, user_id, "pan115") if user_id else None
    if binding is None:
        return
    from backend.emby_server import user_bindings as ub
    try:
        cookie = ub.binding_115_cookie(db, user_id, binding.id)
        client = _make_pan115_client(cookie)
        pickcode = (row.pickcode or "").strip()
        if not pickcode:
            pickcode = _find_115_pickcode_by_fid(client,
                                                 row.variant_file_id)
            if pickcode:
                row.pickcode = pickcode
        if pickcode:
            client.download_url(pickcode)
        else:
            raise _NotFound("115 副本无 pickcode 且目录匹配未找到")
    except Pan115AuthError:
        return  # 不记 strike
    except ub.BindingError:
        return  # 凭据问题不记 strike（绑定落态已由 helper 处理）
    except (_NotFound, _Forbidden, Pan115Error):
        row.stale_count += 1
        row.last_checked_at = now
        if row.stale_count >= STALE_STRIKES:
            row.status = "stale"
            stats["stale"] += 1
    except Exception:
        stats["errors"] += 1
        logger.warning("115 副本巡检异常 variant=%s（不记 strike）",
                       row.id, exc_info=True)
        return
    else:
        row.stale_count = 0
        row.last_checked_at = now


# ---------------------------------------------------------------------------
# 跨 provider 执行器（2026-10-30 重开恢复，机制照 FMBY 形状）
#
# 换链与搬字节分离：先解析源端取链/取段凭据，再分段搬字节，
# 最后用目标端新文件 id 落 ready。源端 Range 取字节口径统一：
# 401/403 = 凭据过期（_CrossAuthError），其余 = 远端不可用
# （_CrossRemoteError），搬运层不静默重试；目标端落盘后必列
# 目标目录按名+fid 核对，核不到记 copy_unresolved 不重试。
# ---------------------------------------------------------------------------

def _resolve_115_source(db, item):
    """跨 provider 115→Drive：服主侧解析源 pickcode（换链步骤）。

    绝不用 EA 播放的共享 pickcode 缓存（_get_cached_115_url）：每次
    现解析。返回 (client, pickcode, size)：
    - mount:// 路径：按挂载行构造 Pan115Mount（db + 条目所属库，
      凭据解析与播放侧同一套：源级凭据表 → 挂载/库绑定账号 →
      默认账号 → 服务器级），_locate 逐级定位；
    - 115:/ 路径：库绑定账号 → 默认账号 → 服务器级解析 Cookie，
      直接按段列目录定位。
    找不到文件 → _NotFound（调用方记 dead）；服主 Cookie 失效 →
    _CrossAuthError（只记任务失败，绝不落用户绑定）；其它 115
    失败 → _CrossRemoteError（有界重试）。
    """
    from backend.emby_server.transfer115 import Pan115AuthError, Pan115Error
    path = (getattr(item, "file_path", "") or "").strip()
    library_row = None
    library_id = getattr(item, "library_id", None)
    if library_id is not None:
        try:
            library_row = (db.query(em.Library)
                           .filter(em.Library.id == library_id).first())
        except Exception:
            library_row = None
    if path.startswith("mount://"):
        try:
            from backend.emby_server import mounts as mount_lib
            parsed = mount_lib.parse_mount_path(path)
        except Exception:
            parsed = None
        if parsed is None:
            raise _CrossRemoteError("115 源 mount 路径解析失败")
        mount_id, rel = parsed
        mount = (db.query(em.StorageMount)
                 .filter(em.StorageMount.id == mount_id).first())
        if mount is None or mount.mount_type != getattr(
                mount_lib, "MOUNT_PAN115", "115"):
            raise _CrossRemoteError("115 源挂载行不存在或类型非 115")
        mount_obj = mount_lib.Pan115Mount(mount, db=db,
                                          library=library_row)
        try:
            entry = mount_obj._locate(rel)
        except mount_lib.MountError as exc:
            if "找不到" in str(exc):
                raise _NotFound(str(exc)) from exc
            raise _CrossRemoteError(str(exc)) from exc
        except Pan115AuthError as exc:
            raise _CrossAuthError(
                f"服主侧 115 凭据失效：{exc}") from exc
        except Pan115Error as exc:
            raise _CrossRemoteError(str(exc)) from exc
        pickcode = (entry.get("pickcode") or "").strip()
        if not pickcode:
            raise _NotFound("115 源未返回 pickcode")
        return (mount_obj._client(), pickcode,
                _to_int(entry.get("size")))
    if path.startswith("115:/"):
        from backend.emby_server import transfer115 as t115
        try:
            cookie, _src = t115.resolve_cookie(db, library=library_row)
        except Pan115Error as exc:
            raise _CrossRemoteError(str(exc)) from exc
        if not cookie:
            raise _CrossAuthError("未解析到服主侧 115 Cookie")
        client = _make_pan115_client(cookie)
        rel = path[len("115:"):].lstrip("/")
        segments = [s for s in rel.split("/") if s]
        if not segments:
            raise _NotFound("115 源路径无文件名")
        try:
            cid = "0"
            for seg in segments[:-1]:
                nxt = next(
                    (e for e in client.list_dir(cid)
                     if (e.get("name") or "") == seg
                     and t115._is_dir(e)),
                    None)
                if nxt is None or not (nxt.get("cid") or ""):
                    raise _NotFound(f"115 源目录缺失：{seg}")
                cid = str(nxt["cid"])
            entry = next(
                (e for e in client.list_dir(cid)
                 if (e.get("name") or "") == segments[-1]
                 and not t115._is_dir(e)),
                None)
        except Pan115AuthError as exc:
            raise _CrossAuthError(
                f"服主侧 115 凭据失效：{exc}") from exc
        except Pan115Error as exc:
            raise _CrossRemoteError(str(exc)) from exc
        if entry is None:
            raise _NotFound(f"115 源文件不存在：{segments[-1]}")
        pickcode = (entry.get("pickcode") or "").strip()
        if not pickcode:
            raise _NotFound("115 源未返回 pickcode")
        return (client, pickcode, _to_int(entry.get("size")))
    raise _CrossRemoteError(f"115 源路径形态未知：{path[:60]}")


def _run_cross_drive_to_115(db, job, item, drive_api):
    """跨 provider Drive→115：SA 源按段取字节 → 用户 115 简单上传。

    字节必经 worker 中转（分段读取器直供 httpx 表单上传，不落盘）。
    用户 115 Cookie 失效 → 绑定落 expired + failed/auth_error（与
    同 provider 115 执行器同口径）；SA 源下载 401/403 → _CrossAuthError
    → failed/auth_error，但绝不碰用户绑定（失效的是源侧 SA 凭据）；
    其余失败 → 有界重试。上传后必列用户 115 转存目录按名核对
    （FMBY discover 口径），核不到记 copy_unresolved 不重试。
    """
    from backend.emby_server.transfer115 import Pan115AuthError, Pan115Error
    from backend.emby_server import user_bindings as ub
    uid = (getattr(job, "owner_ref", "") or "").strip()
    try:
        user_id = int(uid)
    except (TypeError, ValueError):
        user_id = None
    binding = _active_binding(db, user_id, "pan115") if user_id else None
    if binding is None:
        return _fail_job(db, job, "no_binding",
                         "用户 115 绑定已失效或不存在（不重试）")
    try:
        cookie = ub.binding_115_cookie(db, user_id, binding.id)
    except ub.BindingError as exc:
        if exc.kind == "auth_error":
            return _fail_job(db, job, "auth_error", str(exc))
        if exc.kind == "not_found":
            return _fail_job(db, job, "no_binding", str(exc))
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    orig_fid = (item.drive_file_id or "").strip()
    source_drive = (getattr(job, "drive_id", "") or "").strip()
    if not source_drive:
        return _fail_job(db, job, "no_drive", "跨任务无源 Drive 盘标识")
    name = _copy_name_for_item(item)
    try:
        info = drive_api.get_file(source_drive, orig_fid)
        size = _to_int((info or {}).get("size"))
    except (_NotFound, _Forbidden) as exc:
        return _fail_job(db, job, "dead", f"Drive 源已死/无权：{exc}")
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    if not size:
        size = _to_int(getattr(item, "size", None))
    if size <= 0:
        return _fail_job(
            db, job, "unsupported",
            "Drive 源文件大小未知/为零，无法分段搬运（不猜大小）")
    if size > _115_SIMPLE_UPLOAD_LIMIT:
        return _fail_job(
            db, job, "unsupported",
            f"文件 {size} 字节超过 115 简单上传 5 GiB 上限；多段上传需 "
            f"initupload 加密协议（未实测），暂不支持，不静默回落")
    try:
        client = _make_pan115_client(cookie)
        dest_cid = _ensure_115_transfer_dir(client)
    except Pan115AuthError as exc:
        try:
            ub._mark_failed(db, binding, "expired")
        except Exception:
            pass
        return _fail_job(db, job, "auth_error", str(exc))
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    reader = _SegmentReader(
        size,
        lambda s, e: drive_api.download_range(source_drive, orig_fid, s, e))
    try:
        result = client.simple_upload(reader, name, dest_cid)
    except Pan115AuthError as exc:
        try:
            ub._mark_failed(db, binding, "expired")
        except Exception:
            pass
        return _fail_job(db, job, "auth_error", str(exc))
    except _CrossAuthError as exc:
        # SA 源凭据失效：只记任务失败，绝不落用户 115 绑定。
        return _fail_job(db, job, "auth_error", str(exc))
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    resp_fid = (result or {}).get("fid") or ""
    try:
        new_fid, pickcode = _match_115_copy_in_dir(
            client, dest_cid, name, size, expected_fid=resp_fid or None)
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    if not new_fid:
        return _fail_job(
            db, job, "copy_unresolved",
            f"115 跨转存上传完成但目标目录核不到新文件（源 fid={orig_fid}，"
            f"回执 fid={resp_fid or '无'}，目标目录 cid={dest_cid}，"
            f"请人工核对，不自动重试）")
    row = _register_ready_variant(db, job, item, new_fid,
                                  f"pan115:user-{uid}",
                                  pickcode=pickcode or None, size=size)
    return {"job_id": job.id, "outcome": "done", "variant_id": row.id}


def _run_cross_115_to_drive(db, job, item):
    """跨 provider 115→Drive：服主 115 现解析下载链 → 用户 Drive
    可续传上传。

    换链与搬字节分离：先 _resolve_115_source 现解析 pickcode（绝
    不用共享 pickcode 缓存），再现取 download_url 按段取字节，最
    后用用户 Drive 令牌可续传上传。用户 Drive 令牌失效 →
    failed/no_binding（与 Path A 同口径）；服主 115 凭据失效 →
    failed/auth_error 但不碰用户绑定；其余失败 → 有界重试。
    上传后必补 anyone-reader（失败记 orphan）并列用户 Drive 转存
    目录按名核对，核不到记 copy_unresolved。
    """
    from backend.emby_server.transfer115 import Pan115AuthError, Pan115Error
    from backend.emby_server.drive_oauth import DriveOAuthError
    uid = (getattr(job, "owner_ref", "") or "").strip()
    try:
        user_id = int(uid)
    except (TypeError, ValueError):
        user_id = None
    binding = _active_binding(db, user_id, "drive") if user_id else None
    if binding is None:
        return _fail_job(db, job, "no_binding",
                         "用户 Drive 绑定已失效或不存在（不重试）")
    user_client = UserDriveApiClient(db, binding)
    user_drive = f"myDrive:{user_id}"
    name = _copy_name_for_item(item)
    try:
        server_client, pickcode, src_size = _resolve_115_source(db, item)
    except _NotFound as exc:
        return _fail_job(db, job, "dead", f"115 源已不存在：{exc}")
    except _CrossAuthError as exc:
        return _fail_job(db, job, "auth_error", str(exc))
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    try:
        url = server_client.download_url(pickcode)
    except Pan115AuthError as exc:
        return _fail_job(db, job, "auth_error",
                         f"服主侧 115 凭据失效：{exc}")
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    size = src_size or _to_int(getattr(item, "size", None))
    if size <= 0:
        return _fail_job(
            db, job, "unsupported",
            "115 源文件大小未知/为零，无法分段搬运（不猜大小）")
    try:
        folder_id = user_client.ensure_folder(user_drive,
                                              transfer_dir_name())
    except DriveOAuthError as exc:
        return _fail_job(db, job, "no_binding",
                         f"用户 Drive 令牌失效：{exc}")
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}

    def _fetch(start, end):
        headers = {"User-Agent": server_client.ua,
                   "Referer": "https://115.com/",
                   "Cookie": server_client.cookie,
                   "Range": f"bytes={start}-{end}"}
        try:
            resp = httpx.request("GET", url, headers=headers, timeout=120)
        except (httpx.TimeoutException, httpx.ConnectError,
                httpx.RemoteProtocolError, httpx.ReadError) as exc:
            raise _CrossRemoteError(
                f"115 源下载网络异常：{type(exc).__name__}") from exc
        if resp.status_code in (401, 403):
            raise _CrossAuthError(
                f"115 源下载被拒：{resp.status_code}（服主侧凭据失效）")
        if resp.status_code != 206:
            raise _CrossRemoteError(
                f"115 源下载失败：{resp.status_code}（非 206）")
        data = resp.content or b""
        if len(data) != end - start + 1:
            raise _CrossRemoteError(
                f"115 源下载短读：要 {end - start + 1} 字节，"
                f"实得 {len(data)} 字节")
        return data

    reader = _SegmentReader(size, _fetch)
    try:
        new_fid = user_client.resumable_upload(user_drive, name, folder_id,
                                               size, reader)
    except DriveOAuthError as exc:
        return _fail_job(db, job, "no_binding",
                         f"用户 Drive 令牌失效：{exc}")
    except _CrossAuthError as exc:
        return _fail_job(db, job, "auth_error", str(exc))
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    try:
        verified_fid = _verify_drive_copy_in_dir(
            user_client, user_drive, folder_id, name, new_fid)
    except Exception as exc:
        return {"job_id": job.id,
                "outcome": _schedule_retry(db, job, exc)}
    if not verified_fid:
        return _fail_job(
            db, job, "copy_unresolved",
            f"Drive 跨转存上传回执 id={new_fid} 在用户转存目录中核不到"
            f"（目录 id={folder_id}，文件名={name}），不登记、不回落")
    try:
        user_client.add_anyone_reader(user_drive, verified_fid)
    except Exception as exc:
        row = em.TransferVariant(
            item_id=job.item_id, source_file_id=item.drive_file_id.strip(),
            variant_file_id=verified_fid,
            variant_drive_id=f"userdrive:{user_id}",
            owner_scope="user", owner_ref=uid, status="orphan",
            size_bytes=size)
        db.add(row)
        return _fail_job(db, job, "orphan", str(exc))
    row = _register_ready_variant(db, job, item, verified_fid,
                                  f"userdrive:{user_id}", size=size)
    return {"job_id": job.id, "outcome": "done", "variant_id": row.id}


def _run_cross_job(db, job, item, drive_api):
    """跨 provider 任务分发：按 dest_drive_id 定方向（不按 provider）。

    dest pan115: 开头 → Drive→115；dest userdrive: 开头 → 115→Drive；
    其它 dest（理论上到不了，_is_cross_job 已过滤）→ unsupported。
    """
    dest = (getattr(job, "dest_drive_id", "") or "").strip()
    if dest.startswith("pan115:"):
        return _run_cross_drive_to_115(db, job, item, drive_api)
    if dest.startswith("userdrive:"):
        return _run_cross_115_to_drive(db, job, item)
    return _fail_job(db, job, "unsupported",
                     f"跨任务落盘标识未知：{dest}（不重试）")


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
    # 按 provider + 归属分发（2026-10-26 用户盘口径 + 跨 provider）：
    # - 跨任务（dest 与 provider 错侧，见 _is_cross_job）→ 跨执行器
    #   （先于下方同 provider 分支，跨任务的 provider 是源 provider，
    #   不能按 provider 直接分发到同 provider 执行器）；
    # - pan115 + user → 115 执行器（drive_id 可为空，靠条目 fid）；
    # - drive + user → Drive 用户盘执行器（Path A/B）；
    # - drive + source → 下方既有同盘路径（逐字不动）；
    # - 其它组合（pan115 + source、未知 provider）→ 不支持，不重试。
    provider = (getattr(job, "provider", "") or "drive").strip() or "drive"
    owner_scope = (getattr(job, "owner_scope", "") or "source").strip() or "source"
    if (provider not in ("drive", "pan115")
            or (provider == "pan115" and owner_scope != "user")
            or owner_scope not in ("source", "user")):
        job.status = "failed"
        job.error_kind = "unsupported"
        job.error_detail = (
            f"不支持的任务组合 provider={provider} "
            f"owner_scope={owner_scope}（不重试）")
        db.commit()
        return {"job_id": job.id, "outcome": "failed",
                "error_kind": "unsupported"}
    if provider == "drive" and not job.drive_id:
        job.status = "failed"
        job.error_kind = "no_drive"
        job.error_detail = "任务无 drive_id"
        db.commit()
        return {"job_id": job.id, "outcome": "failed", "error_kind": "no_drive"}
    job.status = "running"
    job.attempts += 1
    db.commit()
    if _is_cross_job(job):
        # 跨 provider 单（2026-10-30 重开）：走跨执行器分段搬字节。
        # 总开关关时回 #1487 关停口径——在途跨单直接终态明示，
        # 不执行任何搬字节，不静默吞掉。
        if not cross_provider_enabled(db):
            return _fail_job(db, job, CROSS_PROVIDER_DISABLED_KIND,
                             CROSS_PROVIDER_DISABLED_DETAIL)
        return _run_cross_job(db, job, item, drive_api)
    if provider == "pan115":
        return _run_pan115_job(db, job, item)
    if owner_scope == "user":
        return _run_drive_user_job(db, job, item, drive_api)
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
    """旧单线程一轮（兼容路径，Daemon 已改走 process_claimed_round）：
    先回收租约过期 / running 卡住超 1h 的行（不改 attempts），再按
    id 顺序、每轮最多 10 单逐个跑（单线程即限并发 1）。"""
    now = now or datetime.now()
    stats = {"processed": 0, "done": 0, "failed": 0, "retried": 0,
             "stuck_reset": 0}
    stats["stuck_reset"] = reclaim_expired_leases(db, now=now)
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


# ---------------------------------------------------------------------------
# 可伸缩工人：事件唤醒 / 认领 / 快慢通道 / 按用户限流 / 租约 / 可观测
# ---------------------------------------------------------------------------

_WAKE_EVENT = threading.Event()
_WORKER_ID = uuid.uuid4().hex[:12]
# 死令牌暂停：键 (owner_scope, owner_ref) -> 暂停到期时刻。进程内
# 字典是主路径；Redis 可用时同步写一个带 TTL 的键（见 _paused_store），
# 让多副本工人也能看到同一暂停，Redis 不可用时退回进程内字典，
# 绝不因 Redis 故障把工人打停（fail-open，见上游健康存储同款口径）。
_paused_until = {}
_paused_lock = threading.Lock()
# 可观测计数（进程内，Daemon 每轮日志用；重启清零，不作持久口径）。
_metrics = {"drive_429_total": 0, "jobs_done": 0, "jobs_failed": 0,
            "jobs_retried": 0}
_metrics_lock = threading.Lock()

_AUTH_TERMINAL_KINDS = ("no_binding", "auth_error")


def _owner_key(job_or_scope, ref=None):
    if ref is not None:
        return (str(job_or_scope or "source"), str(ref or ""))
    return (str(getattr(job_or_scope, "owner_scope", "source") or "source"),
            str(getattr(job_or_scope, "owner_ref", "") or ""))


def pause_user_queue(owner_scope, owner_ref, seconds=None, now=None):
    """暂停某用户队列（死令牌场景）：只停该用户，不停整轮。

    seconds 为 None 时读 worker_auth_pause_sec()。同时尽力写 Redis
    带 TTL 键 transfer:paused:<scope>:<ref>，写失败只记 warning。
    """
    until = (now or datetime.now()) + timedelta(
        seconds=seconds if seconds is not None else worker_auth_pause_sec())
    key = (str(owner_scope or "source"), str(owner_ref or ""))
    with _paused_lock:
        _paused_until[key] = until
    try:
        from backend.database import get_redis
        client = get_redis()
        if client is not None:
            client.setex(
                f"transfer:paused:{key[0]}:{key[1]}",
                int((until - (now or datetime.now())).total_seconds()) or 1,
                "1")
    except Exception:
        logger.warning("转存工人：暂停状态写 Redis 失败，退回进程内暂停",
                       exc_info=True)
    return until


def is_user_paused(owner_scope, owner_ref, now=None):
    """该用户队列是否在暂停期内（Redis 键优先，进程内字典兜底）。"""
    now = now or datetime.now()
    key = (str(owner_scope or "source"), str(owner_ref or ""))
    try:
        from backend.database import get_redis
        client = get_redis()
        if client is not None and client.exists(
                f"transfer:paused:{key[0]}:{key[1]}"):
            return True
    except Exception:
        pass
    with _paused_lock:
        until = _paused_until.get(key)
    return until is not None and until > now


def notify_transfer_wakeup(job_id=None):
    """事件唤醒：建任务后调用，尽力通知工人立即干活，永不抛错。

    - 进程内 Event：同进程工人（开发/测试）立即醒；
    - Redis publish：跨容器工人（EA 建任务、worker 干活）立即醒，
      Redis 不可用时返回 False，兜底扫（60 秒）仍能跑，绝不因此
      丢任务、绝不因此改成高频轮询。
    返回 True 表示至少一条唤醒路径已发出（进程内 Event 恒算发出）。
    """
    _WAKE_EVENT.set()
    try:
        from backend.database import get_redis
        client = get_redis()
        if client is not None:
            client.publish(TRANSFER_WORKER_WAKE_CHANNEL,
                           str(job_id or "wake"))
            return True
    except Exception:
        logger.warning("转存工人：唤醒 publish 失败，兜底扫仍会跑",
                       exc_info=True)
    return True


def _wake_subscriber_loop(stop_event):
    """Redis 订阅线程：收到唤醒即置进程内 Event，订阅断了就重连等
    下一轮兜底扫，绝不炸主循环。"""
    while not stop_event.is_set():
        try:
            from backend.database import get_redis
            client = get_redis()
            if client is None:
                stop_event.wait(30)
                continue
            pubsub = client.pubsub()
            pubsub.subscribe(TRANSFER_WORKER_WAKE_CHANNEL)
            for message in pubsub.listen():
                if stop_event.is_set():
                    break
                if message.get("type") == "message":
                    _WAKE_EVENT.set()
        except Exception:
            logger.warning("转存工人：唤醒订阅断开，30 秒后重连",
                           exc_info=True)
            stop_event.wait(30)


def reclaim_expired_leases(db, now=None):
    """租约回收：running 且租约已过期 → pending（attempts 不变），
    兼容旧行：租约 NULL 且 updated_at 距今超 STUCK_RUNNING_SEC
    的 running 行同样打回（升级前遗留行不靠租约也能自愈）。
    返回打回数。调用方负责提交（本函数内提交一次）。"""
    now = now or datetime.now()
    expired = (db.query(em.TransferJob)
               .filter(em.TransferJob.status == "running")
               .filter(
                   (em.TransferJob.lease_expires_at.isnot(None))
                   & (em.TransferJob.lease_expires_at <= now)
                   | (em.TransferJob.lease_expires_at.is_(None))
                   & (em.TransferJob.updated_at
                      < now - timedelta(seconds=STUCK_RUNNING_SEC)))
               .all())
    for job in expired:
        job.status = "pending"
        job.next_retry_at = None
        job.lease_expires_at = None
        job.claimed_by = None
    if expired:
        db.commit()
    return len(expired)


def _running_counts_by_owner(db, now=None):
    """当前 running（租约未过期）按归属计数，用于按用户限流。"""
    now = now or datetime.now()
    rows = (db.query(em.TransferJob)
            .filter(em.TransferJob.status == "running")
            .filter(
                (em.TransferJob.lease_expires_at.is_(None))
                | (em.TransferJob.lease_expires_at > now))
            .all())
    counts = {}
    for row in rows:
        key = _owner_key(row)
        counts[key] = counts.get(key, 0) + 1
    return counts


def _running_cross_count(db, now=None):
    """当前 running（租约未过期）的跨 provider 单数：跨道上限计数。"""
    now = now or datetime.now()
    rows = (db.query(em.TransferJob)
            .filter(em.TransferJob.status == "running")
            .filter(
                (em.TransferJob.lease_expires_at.is_(None))
                | (em.TransferJob.lease_expires_at > now))
            .all())
    return sum(1 for row in rows if _is_cross_job(row))


def _pick_candidates(candidates, limit, running_counts, cap, now=None,
                     cross_cap=None, cross_running=0):
    """公平挑单：点播组（trigger=playback）在前、后台组在后；
    每组内按用户轮流取
    （每轮每用户先取 1 单，再取第 2 单……），直至 limit 或取尽。
    暂停用户直接跳过；已在途达 cap 的用户直接跳过。
    跨 provider 搬字节单另有全局在途上限（cross_cap，None=不限）：
    在途（含本轮已挑）达上限后跨单跳过、同 provider 单照常挑——
    慢单不许占满整条道（2026-10-30 重开硬要求）。
    返回挑中的任务行（不改状态，认领在 claim_due_jobs 里做）。"""
    now = now or datetime.now()
    fast = [c for c in candidates if (c.trigger or "") == "playback"]
    slow = [c for c in candidates if (c.trigger or "") != "playback"]
    picked = []
    used = dict(running_counts)
    picked_cross = 0
    for lane in (fast, slow):
        # 按用户分组，保持每组内 id 顺序
        by_owner = {}
        for job in lane:
            by_owner.setdefault(_owner_key(job), []).append(job)
        round_no = 0
        while len(picked) < limit:
            progressed = False
            for key in sorted(by_owner):
                if len(picked) >= limit:
                    break
                group = by_owner[key]
                if round_no >= len(group):
                    continue
                if is_user_paused(key[0], key[1], now=now):
                    continue
                if used.get(key, 0) >= cap:
                    continue
                job = group[round_no]
                if (cross_cap is not None and _is_cross_job(job)
                        and cross_running + picked_cross >= cross_cap):
                    continue
                picked.append(job)
                if _is_cross_job(job):
                    picked_cross += 1
                used[key] = used.get(key, 0) + 1
                progressed = True
            if not progressed:
                break
            round_no += 1
        if len(picked) >= limit:
            break
    return picked


def claim_due_jobs(db, now=None, limit=None, worker_id=None):
    """认领到期任务：租约回收 → 公平挑单 → 原子认领，返回 job id 列表。

    原子性（多工人互斥的关键）：
    - PostgreSQL：先在同一事务内 SELECT ... FOR UPDATE SKIP LOCKED
      挑行，再 UPDATE 挑中的行置 running + 租约；另一工人同事务
      的 SKIP LOCKED 直接跳过已被锁的行，不阻塞、不重复认领。
    - SQLite（测试）：不支持 FOR UPDATE SKIP LOCKED，走等价路径——
      UPDATE ... WHERE id=? AND status='pending'，rowcount==1 才算
      认领成功；已被另一会话认领的行 rowcount==0，自动跳过。
    认领事务一提交即释放行锁，租约是长时认领标记，绝不跨 copy
    持有数据库行锁（否则 copy 几分钟就把别的工人全堵住）。
    """
    from sqlalchemy import text
    now = now or datetime.now()
    limit = limit if limit is not None else MAX_JOBS_PER_ROUND
    worker_id = worker_id or _WORKER_ID
    reclaim_expired_leases(db, now=now)
    due = (db.query(em.TransferJob)
           .filter(em.TransferJob.status == "pending",
                   (em.TransferJob.next_retry_at.is_(None))
                   | (em.TransferJob.next_retry_at <= now))
           .order_by(em.TransferJob.id)
           .limit(200)
           .all())
    if not due:
        return []
    cap = per_owner_inflight(db)
    running_counts = _running_counts_by_owner(db, now=now)
    picked = _pick_candidates(
        due, limit, running_counts, cap, now=now,
        cross_cap=cross_lane_concurrency(db),
        cross_running=_running_cross_count(db, now=now))
    if not picked:
        return []
    lease_until = now + timedelta(seconds=worker_lease_sec())
    claimed = []
    dialect = ""
    try:
        dialect = db.bind.dialect.name if db.bind is not None else ""
    except Exception:
        dialect = ""
    if dialect == "postgresql":
        try:
            ids = [job.id for job in picked]
            # FOR UPDATE SKIP LOCKED：只锁本事务内挑中的行，另一工人
            # 的同查询直接跳过这些行；随后 UPDATE 并提交即放锁。
            locked = (db.query(em.TransferJob.id)
                      .filter(em.TransferJob.id.in_(ids))
                      .filter(em.TransferJob.status == "pending")
                      .with_for_update(skip_locked=True)
                      .all())
            locked_ids = {row[0] for row in locked}
            for job in picked:
                if job.id not in locked_ids:
                    continue
                job.status = "running"
                job.lease_expires_at = lease_until
                job.claimed_by = worker_id
                claimed.append(job.id)
            db.commit()
            return claimed
        except Exception:
            logger.warning("转存工人：PG 认领 FOR UPDATE 失败，退回原子 UPDATE",
                           exc_info=True)
            try:
                db.rollback()
            except Exception:
                pass
    # 通用等价路径（SQLite + PG 退回）：原子 UPDATE，rowcount 定胜负。
    for job in picked:
        result = db.execute(
            text("UPDATE emby_transfer_jobs SET status='running', "
                 "lease_expires_at=:lease, claimed_by=:worker "
                 "WHERE id=:jid AND status='pending'"),
            {"lease": lease_until, "worker": worker_id, "jid": job.id})
        if result.rowcount == 1:
            claimed.append(job.id)
    db.commit()
    return claimed


def queue_stats(db, now=None):
    """队列可观测口径（纯读，不写库）：深度、等待中位数/p95、
    在途数、失败/重试计数由 Daemon 进程内 _metrics 另记。
    waiting 按 due 任务（pending 且已到期）的 created_at 算。"""
    now = now or datetime.now()
    pending = (db.query(em.TransferJob)
               .filter(em.TransferJob.status == "pending").all())
    due_waits = sorted(
        (now - (job.created_at or now)).total_seconds()
        for job in pending
        if job.next_retry_at is None or job.next_retry_at <= now)
    running = (db.query(em.TransferJob)
               .filter(em.TransferJob.status == "running").count())

    def _pct(values, pct):
        if not values:
            return 0.0
        idx = min(len(values) - 1, int(len(values) * pct / 100.0))
        return float(values[idx])

    median = _pct(due_waits, 50) if due_waits else 0.0
    return {
        "queue_depth": len(pending),
        "due_count": len(due_waits),
        "running": running,
        "waiting_median_sec": median,
        "waiting_p95_sec": _pct(due_waits, 95) if due_waits else 0.0,
        "waiting_max_sec": float(due_waits[-1]) if due_waits else 0.0,
    }


def queue_alerts(stats):
    """超阈值告警（warning 级，Daemon 每轮记；不抛错、不停轮）。"""
    alerts = []
    if stats.get("queue_depth", 0) >= int(TRANSFER_WORKER_ALERT_QUEUE_DEPTH or 200):
        alerts.append(f"转存队列深度 {stats['queue_depth']} 超阈值 "
                      f"{TRANSFER_WORKER_ALERT_QUEUE_DEPTH}")
    if stats.get("waiting_p95_sec", 0) >= int(TRANSFER_WORKER_ALERT_WAIT_SEC or 300):
        alerts.append(f"转存等待 p95 {stats['waiting_p95_sec']:.0f}s 超阈值 "
                      f"{TRANSFER_WORKER_ALERT_WAIT_SEC}s")
    return alerts


def _heartbeat(job_id, session_factory, stop_event, worker_id):
    """租约心跳：执行中每 60 秒（或租约 1/3，取小）续租一次，
    直到任务终态或 stop_event 置位；心跳失败只记 warning，不杀任务。"""
    interval = max(30, min(60, worker_lease_sec() // 3 or 60))
    while not stop_event.wait(interval):
        try:
            db = session_factory()
            try:
                job = db.query(em.TransferJob).filter(
                    em.TransferJob.id == job_id).first()
                if job is None or job.status != "running":
                    return
                job.lease_expires_at = (
                    datetime.now() + timedelta(seconds=worker_lease_sec()))
                db.commit()
            finally:
                db.close()
        except Exception:
            logger.warning("转存工人：租约心跳失败 job=%s", job_id,
                           exc_info=True)


def _execute_claimed_job(job_id, session_factory, drive_api_factory):
    """执行一单已认领任务（独立会话）：跑完清租约；死令牌只暂停
    该用户队列；429 计数进 _metrics。返回 outcome 字符串。"""
    stop_heartbeat = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat, args=(job_id, session_factory, stop_heartbeat,
                                 _WORKER_ID), daemon=True)
    heartbeat.start()
    db = session_factory()
    try:
        job = db.query(em.TransferJob).filter(
            em.TransferJob.id == job_id).first()
        if job is None:
            return "missing"
        try:
            result = run_transfer_job_once(db, job, drive_api_factory())
        except Exception:
            logger.warning("转存任务执行异常 job=%s", job_id, exc_info=True)
            try:
                db.rollback()
            except Exception:
                pass
            return "failed"
        outcome = result.get("outcome", "")
        # 终态清租约（run_transfer_job_once 内部已提交终态；这里只清租约）。
        try:
            fresh = db.query(em.TransferJob).filter(
                em.TransferJob.id == job_id).first()
            if fresh is not None:
                fresh.lease_expires_at = None
                fresh.claimed_by = None
                db.commit()
        except Exception:
            logger.warning("转存工人：清租约失败 job=%s", job_id, exc_info=True)
        with _metrics_lock:
            if outcome == "done":
                _metrics["jobs_done"] += 1
            elif outcome == "failed":
                _metrics["jobs_failed"] += 1
            else:
                _metrics["jobs_retried"] += 1
            detail = (result.get("error_detail") or "") + " " + str(
                getattr(job, "error_detail", "") or "")
            if "429" in detail:
                _metrics["drive_429_total"] += 1
        if outcome == "failed":
            fresh_kind = result.get("error_kind") or getattr(
                job, "error_kind", None)
            if fresh_kind in _AUTH_TERMINAL_KINDS and (
                    getattr(job, "owner_scope", "") or "") == "user":
                pause_user_queue(job.owner_scope, job.owner_ref)
        return outcome
    finally:
        stop_heartbeat.set()
        db.close()


#: 一轮滑动窗口最多处理的任务数：轮内补认领不能无限续（新单持续
#: 涌入时本轮永不返回，主循环的巡检/清理会被饿死），到顶返回，
#: 主循环按 due_count 立即续下一轮。
ROUND_MAX_PROCESSED_MIN = 100
ROUND_MAX_PROCESSED_PER_WORKER = 25

#: 池分支补认领轮询间隔（秒）：wait 必须带超时醒来查空位补认领，
#: 不能只等完成事件——单条慢单在飞、池里明明有空位时，轮中到达
#: 的点播单会被堵到慢单跑完才被认领（2026-10-30 生产实锤：
#: job 1056 慢单跑 5m40s，期间建的 1057/1058 干等到它完成才被
#: 同批补认领，点播用户被堵约 4.5 分钟；守护主循环单线程阻塞在
#: 整轮内，建单唤醒在轮内无人消费）。超时醒来只多跑一次认领
#: 查询（无新单即回 wait），认领/租约/上限语义一概不变。
ROUND_TOP_UP_POLL_SEC = 1.0


def process_claimed_round(session_factory, drive_api_factory, now=None,
                          limit=None):
    """可伸缩一轮（滑动窗口）：按道并发认领，任一单完成立即补
    认领一单——轮次中途新建的单同轮即被吃到，不再等整批跑完
    （2026-10-29 前的 pool.map 轮次屏障：claim 只在轮首发生一次，
    批内最慢一单堵住整轮，轮中到达的点播单只能干等；生产 job
    995/996 即如此被堵）。本轮处理总数有上限（ROUND_MAX_*），
    到顶返回让主循环跑周期任务后按 due 续轮。limit 显式传入时
    沿用旧语义：只认领一批、跑完即返回，不补单。返回统计
    dict；session_factory 每次调用返回独立会话，绝不跨线程
    共用同一 Session。"""
    now = now or datetime.now()
    db = session_factory()
    try:
        stats_before = queue_stats(db, now=now)
        workers = lane_concurrency(db)
        first_limit = workers if limit is None else limit
        claimed = claim_due_jobs(db, now=now, limit=first_limit)
    finally:
        db.close()
    if not claimed:
        return {"processed": 0, "done": 0, "failed": 0, "retried": 0,
                "claimed": [], "queue": stats_before,
                "alerts": queue_alerts(stats_before)}
    sliding = limit is None
    max_processed = (max(ROUND_MAX_PROCESSED_MIN,
                         workers * ROUND_MAX_PROCESSED_PER_WORKER)
                     if sliding else len(claimed))
    all_claimed = list(claimed)
    outcomes = []

    def _top_up(want):
        """补认领至多 want 单（新开短会话，受本轮总量上限约束）。"""
        if want <= 0:
            return []
        room = max_processed - len(all_claimed)
        if room <= 0:
            return []
        db2 = session_factory()
        try:
            return claim_due_jobs(db2, now=datetime.now(),
                                  limit=min(want, room))
        finally:
            db2.close()

    if workers <= 1:
        queue_ids = list(claimed)
        while queue_ids:
            outcomes.append(_execute_claimed_job(
                queue_ids.pop(0), session_factory, drive_api_factory))
            if not queue_ids and sliding:
                more = _top_up(1)
                all_claimed.extend(more)
                queue_ids.extend(more)
    else:
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="transfer-job") as pool:
            futures = {}
            for job_id in claimed:
                fut = pool.submit(_execute_claimed_job, job_id,
                                  session_factory, drive_api_factory)
                futures[fut] = job_id
            while futures:
                done_futs, _pending = wait(
                    list(futures), return_when=FIRST_COMPLETED,
                    timeout=(ROUND_TOP_UP_POLL_SEC if sliding else None))
                for fut in done_futs:
                    outcomes.append(fut.result())
                    del futures[fut]
                if sliding:
                    for job_id in _top_up(workers - len(futures)):
                        fut = pool.submit(
                            _execute_claimed_job, job_id,
                            session_factory, drive_api_factory)
                        futures[fut] = job_id
                        all_claimed.append(job_id)
    stats = {"processed": len(outcomes),
             "done": outcomes.count("done"),
             "failed": outcomes.count("failed"),
             "retried": outcomes.count("retried"),
             "claimed": all_claimed, "queue": stats_before,
             "alerts": queue_alerts(stats_before)}
    for alert in stats["alerts"]:
        logger.warning("转存工人告警：%s", alert)
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
        if (row.variant_drive_id or "").startswith("pan115:"):
            # 115 副本不用 Drive get_file 验活（盘不同、凭据不同）。
            _patrol_115_row(db, row, stats, now)
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


def start_transfer_daemon(session_factory=None, drive_api_factory=None):
    """启动转存工人守护线程（必须显式调用；import 时绝不自动启动）。

    新结构（2026-10-27）：事件唤醒 + 认领式并行 + 60 秒兜底扫。
    - 建任务时 notify_transfer_wakeup() 置 Event / 发 Redis publish，
      主循环立即醒来跑一轮，不等下一轮；无任务时零负载。
    - 兜底扫仍是 60 秒（TRANSFER_WORKER_SWEEP_SEC，默认 60），
      唤醒路径（Redis 断连、进程重启）失效时仍能跑，绝不改 5 秒。
    - 每轮先跑可伸缩认领轮（快通道优先、按用户限流、租约认领、
      并行执行），再按需跑每天一次巡检；闲置 TTL 每小时独立一轮；总闸关时空转等待。
    - session_factory / drive_api_factory 可注入（测试用），默认
      生产值：backend.database.SessionLocal / DriveApiClient。
    任何异常只记 warning、不炸线程。返回线程对象，调用方持有以便观测。
    """
    stop = threading.Event()
    state = {"last_patrol": 0.0, "last_follow_sweep": 0.0,
             "last_idle_cleanup": 0.0}
    if session_factory is None:
        from backend.database import SessionLocal
        session_factory = SessionLocal
    if drive_api_factory is None:
        drive_api_factory = DriveApiClient
    subscriber = threading.Thread(
        target=_wake_subscriber_loop, args=(stop,), daemon=True,
        name="transfer-wake-subscriber")
    subscriber.start()
    thread = threading.Thread(
        target=_daemon_main_loop,
        args=(stop, state, session_factory, drive_api_factory),
        daemon=True, name="transfer-variant")
    thread.start()
    return thread


def _wait_for_wake(stop, timeout_sec):
    """等唤醒或超时：_WAKE_EVENT 置位即提前返回 True；stop 置位
    或超时返回 False。等待期间不查库、不发 Drive 请求（零负载）。"""
    deadline = time.monotonic() + timeout_sec
    while True:
        if stop.is_set():
            return False
        if _WAKE_EVENT.is_set():
            _WAKE_EVENT.clear()
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        # 小步等待，避免错过 stop / wake（步长 0.2 秒，仍零业务负载）。
        _WAKE_EVENT.wait(min(0.2, remaining))


def _idle_cleanup_due(state, now_ts) -> bool:
    """闲置 TTL 本轮是否到点：独立于每日巡检，每小时一轮。

    state 缺 last_idle_cleanup 时按从未跑过处理（首轮即跑）。
    """
    return now_ts - state.get("last_idle_cleanup", 0.0) \
        >= COPY_IDLE_CLEANUP_INTERVAL_SEC


def _daemon_main_loop(stop, state, session_factory, drive_api_factory):
    """Daemon 主循环：唤醒即跑一轮，队列未空立即续跑，空了等唤醒
    或 60 秒兜底扫；巡检每天一次；闲置 TTL 每小时独立一轮；
    异常只记 warning 不炸线程。"""
    while not stop.is_set():
        try:
            if master_enabled():
                stats = process_claimed_round(
                    session_factory, drive_api_factory)
                if stats["claimed"]:
                    db = session_factory()
                    try:
                        remaining = queue_stats(db)["due_count"]
                    finally:
                        db.close()
                    if remaining:
                        continue
                now_ts = time.time()
                if now_ts - state["last_patrol"] >= PATROL_INTERVAL_SEC:
                    db = session_factory()
                    try:
                        patrol_once(db, drive_api_factory)
                    finally:
                        db.close()
                    state["last_patrol"] = now_ts
                # 防外流闲置 TTL（2026-10-28 2 小时单）：从每日巡检解耦，
                # 每小时独立一轮；copy_idle_cleanup 自带 try/except 与
                # 有界批次，异常只记 warning，绝不炸工人循环。
                if _idle_cleanup_due(state, now_ts):
                    state["last_idle_cleanup"] = now_ts
                    try:
                        db = session_factory()
                        try:
                            copy_idle_cleanup(db)
                        finally:
                            db.close()
                    except Exception:
                        logger.warning("闲置 TTL 清理异常，已跳过本轮",
                                       exc_info=True)
                # 追剧扫集（2026-10-27 首批第二单）：只在追剧开关与
                # 转存总闸双开时跑，间隔 SERIES_FOLLOW_SWEEP_SEC
                #（默认 600 秒）；纯查库 + 建任务行，异常只记 warning
                # 不炸工人循环；扫集本身不碰共享链（见 series_follow）。
                from backend.emby_server import series_follow as _sf
                if (_sf.series_follow_enabled()
                        and now_ts - state.get("last_follow_sweep", 0.0)
                        >= _sf.follow_sweep_sec()):
                    state["last_follow_sweep"] = now_ts
                    try:
                        db = session_factory()
                        try:
                            _sf.process_series_follows(db)
                        finally:
                            db.close()
                    except Exception:
                        logger.warning("追剧扫集异常，已跳过本轮",
                                       exc_info=True)
        except Exception:
            logger.warning("转存变体工人轮次异常，已跳过本轮", exc_info=True)
        _wait_for_wake(stop, worker_sweep_sec())


__all__ = [
    "THROTTLE_SEC", "MAX_ATTEMPTS", "RETRY_BASE_SEC", "WORKER_INTERVAL_SEC",
    "PATROL_INTERVAL_SEC", "COPY_IDLE_CLEANUP_INTERVAL_SEC",
    "STALE_STRIKES", "MAX_JOBS_PER_ROUND",
    "STUCK_RUNNING_SEC",
    "master_enabled", "auto_enabled", "transfer_dir_name",
    "QuotaPageError", "is_quota_page_bytes", "is_transfer_dir_rel",
    "TransferError",
    "has_active_drive_binding", "has_active_pan115_binding",
    "get_ready_variant_for_user",
    "resolve_play_file_id", "note_quota_signal", "create_manual_job",
    "create_user_job", "ensure_user_copy_gate",
    "release_binding",
    "DriveApiClient", "UserDriveApiClient",
    "run_transfer_job_once", "process_due_jobs_once", "patrol_once",
    "start_transfer_daemon",
    "worker_concurrency", "worker_per_user_max", "worker_lease_sec",
    "lane_concurrency", "per_owner_inflight",
    "cross_provider_enabled", "cross_lane_concurrency",
    "transfer_config_payload", "write_transfer_config",
    "worker_sweep_sec", "notify_transfer_wakeup", "pause_user_queue",
    "is_user_paused", "reclaim_expired_leases", "claim_due_jobs",
    "queue_stats", "queue_alerts", "process_claimed_round",
    "play_inline_enabled", "play_inline_budget_sec",
    "play_inline_concurrency", "claim_job_for_inline",
    "claim_and_execute_inline", "maybe_run_play_inline",
]
