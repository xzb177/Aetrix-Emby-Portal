"""播放门禁（2026-10-26 用户亲定硬规矩，无开关）。

规矩：用户只有绑定了自己的网盘账号才能点播；没绑定的用户只能浏览
片库、不能点播。管理员与普通用户一视同仁，不设豁免。

2026-11-03 用户改定（「无界片库免绑定，本地库门禁照旧」）：无界
（CMS 上游供源）片库的播放走上游线路直发，不过用户网盘、不耗共享
盘配额，门禁的防风控理由不成立——条目所属库为 CMS 库时免绑定；
本地/挂载库原样不变。判定只许走本模块的
``binding_required_for_item`` / ``binding_required_for_upstream``
单一入口，调用点不许各写一份；CMS 库的定义与媒体库体检同一份
（``upstream_source_ids`` 经 ``applecms_library`` 解析后非空）。

执行口径：
- 只拦播放，不拦浏览：片库浏览、搜索、详情页一律不调本模块；本模块
  只在播放链接/变体下发处与直接流端点被调用。
- 判定标准：请求播放的用户（WebUser.id）名下至少有一条 status 为
  ``active`` 的用户存储绑定。2026-11-02 起夸克/115 用户绑定线已
  下线，只认 drive 绑定（存量夸克/115 行视为不存在，见删除单）；
  无 Drive 绑定、或绑定全是 invalid / expired，一律 403。
- 只拦新下发的播放，不追溯已下发的链接：请求带有效的 play_sign 时，
  该签名本身就是 PlaybackInfo 门禁后下发的凭证（门禁上线前下发的在
  TTL 内自然过期），本次消费不拦截；无签名、签名无效、或走 Emby token
  直接调流端点时，视为新播放，必须过门禁。
- 被拦时返回 403 + 大白话原因，前端点播处据此展示去“我的”页绑定的引导。
- 下载、字幕、浏览类接口不在本模块管辖内（点播 ≠ 下载）。
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from backend import models
from backend.emby_server import models as em

# 被拦时返回给客户端的大白话（前端点播处据此展示去“我的”页绑定的引导）。
GATE_MESSAGE = "请先绑定自己的网盘账号才能点播"

# 名下存在已下线绑定（115 / 夸克存量行）的人被拦时的专用大白话：
# 这批人不是“没绑”，是绑的线被下线了——必须明说已下线、改绑
# Drive 才能播，不许与从未绑定的人混用上面那句笼统提示
# （2026-11-03 存量收敛单；选文案仍在本模块单一入口里做）。
GATE_MESSAGE_REMOVED_PROVIDER = (
    "你之前绑定的 115 / 夸克网盘已下线，请改绑 Google Drive 才能点播")


def has_active_binding(db: Session, user_id: int) -> bool:
    """该用户名下是否存在至少一条 active 的 Drive 用户存储绑定。

    只认 provider == "drive"（夸克/115 绑定线 2026-11-02 已下线，
    存量行视为不存在）；只认本用户自己的行，绝不跨用户；
    invalid / expired 行不算。
    """
    return (
        db.query(em.UserStorageBinding)
        .filter(
            em.UserStorageBinding.user_id == user_id,
            em.UserStorageBinding.provider == "drive",
            em.UserStorageBinding.status == "active",
        )
        .first()
        is not None
    )


def has_removed_provider_binding(db: Session, user_id: int) -> bool:
    """该用户名下是否存在已下线 provider（115 / 夸克）的存量绑定行。

    不论行状态：active 是删线时未收敛的存量，expired / invalid 同样
    说明此人绑过这两家——被门禁拦时据此出下线专用文案。provider
    名单只认 ``user_bindings.REMOVED_PROVIDERS`` 一处（函数内懒引，
    与本仓既有防循环口径一致）。
    """
    from backend.emby_server.user_bindings import REMOVED_PROVIDERS

    return (
        db.query(em.UserStorageBinding)
        .filter(
            em.UserStorageBinding.user_id == user_id,
            em.UserStorageBinding.provider.in_(REMOVED_PROVIDERS),
        )
        .first()
        is not None
    )


def _library_is_cms(db: Session, library_id) -> bool:
    """条目所属库是否为 CMS（无界）库。

    定义与媒体库体检（reachability）同一份：库行 ``upstream_source_ids``
    经 ``applecms_library.parse_upstream_source_ids`` 解析后非空。
    库 id 为空 / 查不到库行一律 False（归非 CMS，门禁照旧，fail-closed）。
    """
    if library_id is None:
        return False
    library = (
        db.query(em.Library)
        .filter(em.Library.id == library_id)
        .first()
    )
    if library is None:
        return False
    from backend.emby_server import applecms_library as _cms_lib

    return bool(_cms_lib.parse_upstream_source_ids(library))


def binding_required_for_item(db: Session, item) -> bool:
    """该条目的播放是否需要过绑定门禁（按条目判定的单一入口）。

    CMS 库条目 → False（免绑定）；其余 → True。条目为空、没挂库、
    查不到库一律 True（fail-closed，照旧过门禁）。
    """
    if item is None:
        return True
    library_id = getattr(item, "library_id", None)
    if library_id is None:
        return True
    return not _library_is_cms(db, library_id)


def binding_required_for_upstream(
    db: Session, source_id, upstream_item_id
) -> bool:
    """上游兜底口（按源 id + 上游条目 id 寻址）是否需要过绑定门禁。

    兜底口没有本地条目 id 入参，经映射表反查本地条目再走
    ``binding_required_for_item`` 同一判定：映射到的本地条目全部
    属 CMS 库才免；查不到映射、映射条目已不存在、或任一映射条目
    属本地库 → True（fail-closed）。
    """
    rows = (
        db.query(em.UpstreamItemMapping.local_item_id)
        .filter(
            em.UpstreamItemMapping.source_id == source_id,
            em.UpstreamItemMapping.upstream_item_id == upstream_item_id,
        )
        .all()
    )
    if not rows:
        return True
    item_ids = {row[0] for row in rows}
    items = (
        db.query(em.MediaItem)
        .filter(em.MediaItem.id.in_(item_ids))
        .all()
    )
    if len(items) != len(item_ids):
        return True
    return any(binding_required_for_item(db, it) for it in items)


def _request_uses_issued_play_sign(request: Request, db: Session) -> bool:
    """请求是否在消费一条已下发的播放签名。

    判定条件与播放端点鉴权（get_play_user 的签名分支）同口径：URL 带
    sign/exp/uid、路径带 item_id、防盗链总开关开着、且签名校验通过。
    任一条件不满足都视为“不是已下发链接的消费”，交给门禁判定。
    """
    # 合成请求可能没有 query_string：Starlette 的 query_params 属性
    # 会因 scope 缺键直接抛 KeyError（getattr 拦不住）。先走正常
    # 属性，拿不到时退回 scope 安全读，解析口径与 Starlette 一致
    #（latin-1 解码、保留空值、重键取后值），门禁语义不变。
    try:
        query = request.query_params
    except (KeyError, AttributeError):
        from urllib.parse import parse_qsl

        scope = getattr(request, "scope", None) or {}
        raw = scope.get("query_string", b"") if isinstance(scope, dict) else b""
        if isinstance(raw, bytes):
            raw = raw.decode("latin-1")
        query = dict(parse_qsl(raw, keep_blank_values=True))
    query = query or {}
    path_params = getattr(request, "path_params", None) or {}
    sign = query.get("sign")
    exp_s = query.get("exp")
    uid_s = query.get("uid")
    item_id = path_params.get("item_id")
    if not (sign and exp_s and uid_s and item_id):
        return False
    from backend.emby_server import play_sign

    if not play_sign.hotlink_enabled(db):
        return False
    try:
        uid = int(uid_s)
        exp = int(exp_s)
    except (TypeError, ValueError):
        return False
    return bool(play_sign.verify_play_sign(uid, item_id, exp, sign))


def _ensure_license_playback_allowed(db: Session) -> None:
    """软件授权闸门（发行版专用）：与绑定门禁同层，只拦开始播放。

    总开关 env AETRIX_LICENSE_ENFORCE 默认关——关时
    ``license_client.ensure_playback_allowed`` 第一行即返回，行为
    与没有这道闸门完全一致（自家部署永不受影响）；发行包默认开，
    无效（停用/过期过宽限等）时抛 403 + 大白话。判读只用本地缓存
    令牌，不联网。口径见 backend/license_client.py 模块头。
    """
    from backend import license_client

    license_client.ensure_playback_allowed(db)


def ensure_binding_playback_allowed(
    db: Session, user: models.WebUser, request: Request | None = None
) -> None:
    """播放门禁：无 active 绑定时抛 403。

    ``request`` 为空时无条件判定（PlaybackInfo / 上游兜底口：本次调用
    本身就是新下发）；传了 ``request`` 且请求带有效 play_sign 时放行
    （消费已下发的链接，不追溯）。用户对象取不到 id 时按无绑定处理
    （fail-closed：403 而不是 500）。
    """
    _ensure_license_playback_allowed(db)
    if request is not None and _request_uses_issued_play_sign(request, db):
        return
    user_id = getattr(user, "id", None)
    if user_id is not None and has_active_binding(db, user_id):
        return
    if user_id is not None and has_removed_provider_binding(db, user_id):
        raise HTTPException(
            status_code=403, detail=GATE_MESSAGE_REMOVED_PROVIDER)
    raise HTTPException(status_code=403, detail=GATE_MESSAGE)


def ensure_binding_playback_allowed_for_item(
    db: Session,
    user: models.WebUser,
    item,
    request: Request | None = None,
) -> None:
    """按条目口径的播放门禁：CMS 库条目免绑定，其余与
    ``ensure_binding_playback_allowed`` 逐字同口径（403 文案不变、
    已下发签名消费不追溯）。"""
    # 软件授权是整机口径、不分条目：CMS 免绑定也免不了授权校验，
    # 必须在免绑定早退之前先过（enforce 关时为空操作）。
    _ensure_license_playback_allowed(db)
    if not binding_required_for_item(db, item):
        return
    ensure_binding_playback_allowed(db, user, request)


def ensure_binding_playback_allowed_for_upstream(
    db: Session, user: models.WebUser, source_id, upstream_item_id
) -> None:
    """上游兜底口的播放门禁：映射全属 CMS 库条目时免绑定，其余与
    ``ensure_binding_playback_allowed`` 同口径（兜底口无已下发链接
    消费一说，不传 request）。"""
    # 同 _for_item：软件授权整机口径，先于免绑定早退。
    _ensure_license_playback_allowed(db)
    if not binding_required_for_upstream(db, source_id, upstream_item_id):
        return
    ensure_binding_playback_allowed(db, user)


def sign_allowed_for_item(db: Session, user_id, item) -> bool:
    """详情/清单面给该条目下发 play_sign 签名轨的判定（单一入口）。

    播放签名是「已过门禁才下发」的凭证：已绑定用户恒可签；未绑定
    用户只有条目免门禁（CMS 库）时可签——免门禁等价于已过门禁，
    不签则直放详情源的客户端拿不到可用地址。user_id 为空恒 False
    （与既有 ``bool(user_id)`` 前置同口径）。

    goal-33 PR-3（design §4.1）新增池分支：未绑定用户池
    eligible 且该条目有 ready 池副本时也可签（池播出已接通，
    签名消费时由流端点现场走池门禁选路到池副本）；不 ready
    不签。池判定实现在 pool_gate.pool_sign_allowed（本模块不
    反向持有池逻辑，懒引防循环）。
    """
    if not user_id:
        return False
    if has_active_binding(db, user_id):
        return True
    if not binding_required_for_item(db, item):
        return True
    from backend.emby_server import pool_gate as _pool_gate

    return _pool_gate.pool_sign_allowed(db, user_id, item)


def raise_for_transfer_gate(gate_result) -> None:
    """首播转存门禁的拦截映射（与 video_stream 同口径，全站只此一份）。

    ``transfer_variant.ensure_user_copy_gate`` 的 verdict 里，
    preparing / failed 回 409（detail 优先用门禁给的说明，缺省文案
    与 video_stream 一致），error 回 503 fail-closed（不走共享）；
    proceed / serve_115_copy 不在此拦截——放行后的选路（含 115
    副本现签、Drive 副本 fid）归各端点既有逻辑，本函数不动。
    """
    verdict = (gate_result or {}).get("verdict")
    if verdict == "preparing":
        raise HTTPException(
            status_code=409,
            detail=(gate_result or {}).get("detail") or
            "转存准备中：正在把文件转存到你的绑定盘，转存完成后可播")
    if verdict == "failed":
        raise HTTPException(
            status_code=409,
            detail=(gate_result or {}).get("detail") or
            "转存失败，不走共享播放：请稍后重试或联系管理员")
    if verdict == "error":
        raise HTTPException(
            status_code=503,
            detail="转存门禁暂不可用，稍后重试（未走共享播放）")
