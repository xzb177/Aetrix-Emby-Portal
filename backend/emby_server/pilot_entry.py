"""pilot 独立入口执法（2026-11-05 用户亲定，专属线路形态改定）。

pilot 域名（飞牛反代）是白名单用户的独立连接入口：播放器把服务器
地址填成 pilot 域名，请求经飞牛 nginx 反代到本站时带标记头
``X-Aetrix-Pilot-Entry``。入口认定唯一口径在 upstream_playback
.match_entry_line（专属线路通用能力：头值与任一启用中线路的
入口密钥逐字一致 → 命中该线路；外部伪造同名头直连 emby 域名、
值不对的一律按普通入口处理）。准入按命中线路的人群判定
（user_matches_line，本期只有 whitelist）。

本模块只管「pilot 入口 + 非白名单」的拒绝执法，两道：

1. 登录/签发令牌：auth.issue_token 顶部（与播放器登录黑名单同一
   执法点）——非白名单经 pilot 入口登录直接拒，不签发令牌；
2. PlaybackInfo：api.playback_info 入口——覆盖「拿别处签发的令牌
   经 pilot 入口混进来」：令牌来源不问，请求带合法 pilot 标记且
   现算非白名单即拒。

未认证的公开端点（System/Info/Public 等播放器探活）不走这两道，
服务器发现与添加服务器流程不受影响。白名单判定只用
codes.is_whitelist_account（#1668 唯一口径），不另立名单。
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: 拒绝机器码（登录响应与 PlaybackInfo 响应同码）。
CODE = "pilot_entry_whitelist_only"

#: 拒绝文案（友好明示，用户可见）。
MESSAGE = "专属入口仅白名单用户可用"


class PilotEntryWhitelistOnly(Exception):
    """pilot 入口非白名单登录（由 AuthenticateByName 转 403 响应）。"""

    def __init__(self):
        super().__init__(MESSAGE)
        self.code = CODE
        self.message = MESSAGE


def is_pilot_entry(db, request) -> bool:
    """入口认定转发唯一实现（upstream_playback.is_pilot_entry）。"""
    from backend.emby_server import upstream_playback as _up

    return _up.is_pilot_entry(db, request)


def match_line(db, request):
    """命中线路转发唯一实现（upstream_playback.match_entry_line）。

    命中返回线路记录（后续选线/执法按该线路的人群、基址、范围
    走），未命中返回 None（普通入口）。
    """
    from backend.emby_server import upstream_playback as _up

    return _up.match_entry_line(db, request)


def user_is_whitelist(db, user) -> bool:
    """白名单现算（codes.is_whitelist_account 唯一口径）。"""
    if user is None:
        return False
    from backend import codes as _codes

    return bool(_codes.is_whitelist_account(db, user))


def user_matches_line(db, user, line) -> bool:
    """用户是否命中线路的准入人群（本期只实现 whitelist）。

    未知人群取值一律不命中（fail-closed）：新人群必须先在此落地
    判定，才许在管理端配置出现。
    """
    if line is None or user is None:
        return False
    from backend.emby_server import upstream_playback as _up

    if line.get("audience") == _up.AUDIENCE_WHITELIST:
        return user_is_whitelist(db, user)
    return False


def lines_for_user(db, user) -> list:
    """用户有资格使用的启用线路（用户端「我的专属线路」唯一口径）。"""
    from backend.emby_server import upstream_playback as _up

    return [line for line in _up.exclusive_lines(db)
            if line["enabled"] and user_matches_line(db, user, line)]


def enforce_login(db, request, user) -> None:
    """登录执法：命中线路 + 人群不符抛 PilotEntryWhitelistOnly。

    只在签发令牌前调用（auth.issue_token 顶部），不碰普通入口登录。
    拒绝码与文案与单线形态时逐字一致。
    """
    line = match_line(db, request)
    if line is not None and not user_matches_line(db, user, line):
        raise PilotEntryWhitelistOnly()


def pilot_entry_rejection(db, request, user):
    """PlaybackInfo 执法判定：应拒绝时返回 (status, body)，否则 None。

    与登录执法同口径同码同文案；调用方据此回 JSONResponse。
    """
    line = match_line(db, request)
    if line is not None and not user_matches_line(db, user, line):
        return 403, {"code": CODE, "message": MESSAGE}
    return None
