"""播放门禁（2026-10-26 用户亲定硬规矩，无开关、无例外）。

规矩：用户只有绑定了自己的网盘账号才能点播；没绑定的用户只能浏览
片库、不能点播。管理员与普通用户一视同仁，不设豁免。

执行口径：
- 只拦播放，不拦浏览：片库浏览、搜索、详情页一律不调本模块；本模块
  只在播放链接/变体下发处与直接流端点被调用。
- 判定标准：请求播放的用户（WebUser.id）名下至少有一条 status 为
  ``active`` 的用户存储绑定（drive / pan115 任一都算）。无绑定、或绑定
  全是 invalid / expired，一律 403。
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


def has_active_binding(db: Session, user_id: int) -> bool:
    """该用户名下是否存在至少一条 active 的用户存储绑定。

    不区分 provider（drive / pan115 任一都算）；只认本用户自己的行，
    绝不跨用户；invalid / expired 行不算。
    """
    return (
        db.query(em.UserStorageBinding)
        .filter(
            em.UserStorageBinding.user_id == user_id,
            em.UserStorageBinding.status == "active",
        )
        .first()
        is not None
    )


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


def ensure_binding_playback_allowed(
    db: Session, user: models.WebUser, request: Request | None = None
) -> None:
    """播放门禁：无 active 绑定时抛 403。

    ``request`` 为空时无条件判定（PlaybackInfo / 上游兜底口：本次调用
    本身就是新下发）；传了 ``request`` 且请求带有效 play_sign 时放行
    （消费已下发的链接，不追溯）。用户对象取不到 id 时按无绑定处理
    （fail-closed：403 而不是 500）。
    """
    if request is not None and _request_uses_issued_play_sign(request, db):
        return
    user_id = getattr(user, "id", None)
    if user_id is not None and has_active_binding(db, user_id):
        return
    raise HTTPException(status_code=403, detail=GATE_MESSAGE)


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
