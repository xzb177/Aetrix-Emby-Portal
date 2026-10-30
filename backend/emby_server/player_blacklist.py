"""播放器登录黑名单（2026-10-30 用户亲定）。

管理员可配的播放器黑名单：命中的播放器在 Emby 客户端**登录环节**被拒
（403），只拦登录——不拦已下发令牌的后续请求、不拦网页端登录。

配置：SystemConfig player_login_blacklist，JSON 数组，每条规则
{"label": 名称, "match": [子串...], "exclude": [子串...]}。

- 行不存在 = 用代码级默认名单 DEFAULT_RULES（用户 2026-10-30 亲点）；
- 行存在且为空数组 = 功能不起作用；
- 管理员保存走 config_service 校验（config_schema 的 json_rules 类型），
  非法结构保存时即 400；运行时解析失败按空名单处理并记 warning（fail-open，
  不让一条坏配置把所有人锁在门外）。

匹配语义：对 Emby Client 名（X-Emby-Authorization 的 Client 或
X-Emby-Client 头）与 User-Agent 两处做大小写不敏感子串匹配；
任一 match 命中且 exclude 全部未命中才算拉黑——exclude 优先，
Infuse-Direct 靠 exclude=["infuse-direct"] 保命，绝不能被误伤
（其 Client 名即 "Infuse-Direct"、UA 为 "Infuse-Direct/8.5.8"，生产亲验）。

执法点：auth.issue_token 顶部——所有 Emby 客户端登录签发令牌都必经
此处，一处实现全入口生效。本服务现有的登录入口只有 AuthenticateByName
（AuthenticateWithToken / QuickConnect 登录不存在，/quickconnect/info
只是兼容探活口，不签发令牌）。
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from backend import config_schema

logger = logging.getLogger(__name__)

CONFIG_KEY = "player_login_blacklist"

#: 命中时回给客户端的提示（AuthenticateByName 的 403 body 里带 message）
BLOCK_MESSAGE = "该播放器已被服务器停用，请更换播放器"

#: 代码级默认名单（用户 2026-10-30 亲点两条）。管理员在后台保存后以库中
#: 值为准；删行（保存空串）即回到这份默认。
#: 注意：爆米花在生产暂无登录样本，"popcorn"/"爆米花" 两个模式串是
#: provisional——上线后若有漏网样本，按其设备记录里的确切 Client 名
#: 在后台改配置补齐。
DEFAULT_RULES: list[dict] = [
    {"label": "爆米花", "match": ["popcorn", "爆米花"], "exclude": []},
    {"label": "Infuse 媒体库模式", "match": ["infuse"], "exclude": ["infuse-direct"]},
]


class PlayerLoginBlacklisted(Exception):
    """登录命中播放器黑名单（由 AuthenticateByName 转成可读的 403 响应）"""

    def __init__(self, label: str, client: str, user_agent: str):
        super().__init__(BLOCK_MESSAGE)
        self.message = BLOCK_MESSAGE
        self.label = label
        self.client = client
        self.user_agent = user_agent


def load_rules(db) -> list[dict]:
    """读当前生效规则：行不存在用默认名单；行存在按其 JSON 解析。

    读取走 integrations.store 的统一热读（写时失效，保存即生效）。
    解析/结构异常按空名单处理（fail-open）并记 warning。
    """
    from backend.integrations import store

    raw = store.get_value(db, CONFIG_KEY, "")
    if not raw or not str(raw).strip():
        return [dict(rule) for rule in DEFAULT_RULES]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        logger.warning("%s 配置不是合法 JSON，按空名单处理", CONFIG_KEY)
        return []
    return config_schema.coerce_player_blacklist_rules(parsed, strict=False)


def match_blacklist(rules: list[dict], client: str, user_agent: str) -> Optional[dict]:
    """命中返回规则 dict，未命中返回 None。

    对 Client 名与 User-Agent 两处做大小写不敏感子串匹配：
    任一 match 命中且 exclude 全部未命中才算命中（exclude 优先）。
    """
    haystacks = [h.lower() for h in (client or "", user_agent or "") if h]
    if not haystacks:
        return None
    for rule in rules or []:
        matches = [str(p).lower() for p in (rule.get("match") or []) if str(p).strip()]
        if not matches:
            continue
        excludes = [str(p).lower() for p in (rule.get("exclude") or []) if str(p).strip()]
        if any(p in h for p in excludes for h in haystacks):
            continue
        if any(p in h for p in matches for h in haystacks):
            return rule
    return None


def client_name_from_request(request) -> str:
    """从请求取 Emby Client 名：X-Emby-Authorization 的 Client 优先，
    回退 X-Emby-Client 头（与 issue_token 的取法同一口径）。"""
    from backend.emby_server.auth import parse_emby_authorization

    auth = parse_emby_authorization(request.headers.get("X-Emby-Authorization"))
    return auth.get("Client") or request.headers.get("X-Emby-Client") or ""


def enforce_player_login_blacklist(db, request) -> None:
    """登录执法：命中黑名单抛 PlayerLoginBlacklisted，未命中无操作。

    只在签发令牌前调用（auth.issue_token 顶部），不碰已下发令牌的请求。
    """
    client = client_name_from_request(request)
    user_agent = request.headers.get("user-agent") or ""
    rule = match_blacklist(load_rules(db), client, user_agent)
    if rule is not None:
        raise PlayerLoginBlacklisted(
            label=str(rule.get("label") or ""), client=client, user_agent=user_agent,
        )
