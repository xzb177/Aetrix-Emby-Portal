"""播放与客户端策略（v2.26.0，2026-10 简化）

以前这些策略散在两处：一部分是环境变量（``EMBY_MAX_TRANSCODES`` / ``EMBY_TRANSCODE_IDLE``，
改一次要登机器重启进程），一部分是设置页里的下载 / 设备开关。运营想要的是
「客户端这边到底允许做什么」——一句话能回答的问题：

- **允许转码吗**（关掉只放直连/直接播放：省 CPU，代价是客户端兼容性变差）；
- **同时几路转码**（超了直接拒绝新的，而不是把正在看的人挤掉）；
- **哪些客户端不许进**（老版本 / 盗版客户端的 UA 子串黑名单，也支持白名单模式）。

2026-10 删除服务端码率钳制：客户端要多少码率给多少（学 Linger 薄服务器思路）。

判定口径与 ``backend/subscriptions.py`` 一致：**管理员不受限**（排障时不能被自己的策略挡住）。

落库仍然是 ``SystemConfig`` 键值（EM 与 EA 用的是同一个库，见 deploy-ea.md），
所以面板上改完最多 60 秒生效（同一进程内保存即生效，跨进程靠短 TTL 兜底）。
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from backend import config_service
from backend.emby_server.playback_strategy import KEY_DRIVE_DIRECT_PLAY
from backend.integrations import store

CONFIG_TRANSCODE_ENABLED = "playback_transcode_enabled"
CONFIG_MAX_TRANSCODES = "playback_max_concurrent_transcodes"
CONFIG_BLOCKED_AGENTS = "client_blocked_agents"
CONFIG_ALLOWED_AGENTS = "client_allowed_agents"

# 键 → 类型（设置页与 PUT 接口共用一份，避免两边各写一套白名单）
POLICY_KEYS = {
    CONFIG_TRANSCODE_ENABLED: "bool",
    CONFIG_MAX_TRANSCODES: "int",
    CONFIG_BLOCKED_AGENTS: "str",
    CONFIG_ALLOWED_AGENTS: "str",
    # Drive 302 直链总开关（#957，playback_strategy.KEY_DRIVE_DIRECT_PLAY 定义，
    # video_stream 读它决定 302/410；键名本身就是页面字段名，无需短名映射）
    KEY_DRIVE_DIRECT_PLAY: "bool",
}

# 各键的出厂默认值（字符串形态，与 _config_bool / _config_int / _agent_list 的缺省语义一致）。
# 配置自愈（backend/config_self_heal.py）引用这份表补缺失行 —— 默认值只许在这里定义一次。
POLICY_DEFAULTS = {
    CONFIG_TRANSCODE_ENABLED: "true",
    CONFIG_MAX_TRANSCODES: "0",
    CONFIG_BLOCKED_AGENTS: "",
    CONFIG_ALLOWED_AGENTS: "",
    KEY_DRIVE_DIRECT_PLAY: "true",  # 与 playback_strategy.drive_direct_play_enabled 缺省一致
}

# 页面字段短名 → SystemConfig 键（GET policy_payload 返回短名，PUT 必须认短名；
# 2026-10-17 实锤：前端一直送短名，write_policy 只认长键名导致保存静默失效）
FIELD_TO_KEY = {
    "transcode_enabled": CONFIG_TRANSCODE_ENABLED,
    "max_concurrent_transcodes": CONFIG_MAX_TRANSCODES,
    "blocked_agents": CONFIG_BLOCKED_AGENTS,
    "allowed_agents": CONFIG_ALLOWED_AGENTS,
    "drive_direct_play_enabled": KEY_DRIVE_DIRECT_PLAY,
}


def _raw(db: Session, key: str) -> str:
    """统一热读（只许这一套）：短 TTL 缓存，保存时失效；缺失返回空串"""
    return store.get_value(db, key, "")


def _config_bool(db: Session, key: str, default: bool) -> bool:
    raw = _raw(db, key)
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip().lower() == "true"


def _config_int(db: Session, key: str, default: int) -> int:
    raw = _raw(db, key)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def transcode_enabled(db: Session) -> bool:
    """是否允许服务端转码（缺省允许；关掉只放直连）"""
    return _config_bool(db, CONFIG_TRANSCODE_ENABLED,
                        POLICY_DEFAULTS[CONFIG_TRANSCODE_ENABLED] == "true")


def max_transcodes(db: Session) -> int:
    """并发转码上限；0 = 用进程内置上限（``EMBY_MAX_TRANSCODES`` / CPU 核数）"""
    return max(0, _config_int(db, CONFIG_MAX_TRANSCODES,
                        int(POLICY_DEFAULTS[CONFIG_MAX_TRANSCODES])))


def _agent_list(db: Session, key: str) -> list[str]:
    raw = (_raw(db, key) or "").strip()
    if not raw:
        return []
    parts = raw.replace("，", ",").replace("\n", ",").split(",")
    return [p.strip().lower() for p in parts if p.strip()]


def client_denied_reason(db: Session, user_agent: Optional[str], *, is_staff: bool = False) -> Optional[str]:
    """这个客户端能不能进（None = 放行）

    白名单优先于黑名单（都配了就按白名单放行，与运维直觉一致）；
    UA 为空的行（脚本 / 测试）只在白名单模式下被拦，黑名单不误伤。
    """
    if is_staff:
        return None
    ua = (user_agent or "").strip().lower()
    allowed = _agent_list(db, CONFIG_ALLOWED_AGENTS)
    if allowed:
        if not ua:
            return "本站只允许指定客户端播放，请使用官方客户端"
        if not any(rule in ua for rule in allowed):
            return "当前客户端不在允许列表内，请联系管理员"
        return None
    blocked = _agent_list(db, CONFIG_BLOCKED_AGENTS)
    if blocked and ua and any(rule in ua for rule in blocked):
        return "当前客户端已被停用（版本过旧或不受支持），请升级后再试"
    return None


def ensure_client_allowed(db: Session, user, user_agent: Optional[str]) -> None:
    """播放入口统一调用：被策略拦下的客户端直接 403，并说清原因"""
    reason = client_denied_reason(db, user_agent, is_staff=bool(getattr(user, "is_staff", False)))
    if reason:
        raise HTTPException(status_code=403, detail=reason)


def ensure_transcode_allowed(db: Session, user, *, running: int, capacity: int) -> None:
    """建立转码会话前校验：关掉转码 / 并发已满都直接拒绝

    与 ``streaming.enforce_transcode_capacity`` 的分工：那里只回收**闲置**会话、宁可短暂
    超限也不打断正在看的人；这里的上限是运营**明确设定**的（``playback_max_concurrent_transcodes``），
    超了就该让新请求排队或降级直连，而不是把整台机器的 CPU 打满、所有人都卡。
    """
    if getattr(user, "is_staff", False):
        return
    if not transcode_enabled(db):
        raise HTTPException(status_code=403, detail="本站已关闭服务端转码，请使用直连播放")
    limit = max_transcodes(db)
    if limit and running >= limit:
        raise HTTPException(
            status_code=503,
            detail=f"当前转码路数已达上限（{running}/{limit}），请稍后再试或使用直连播放",
        )


def policy_payload(db: Session) -> dict:
    """当前策略（面板读一份，前端不维护默认值）"""
    return {
        "transcode_enabled": transcode_enabled(db),
        "max_concurrent_transcodes": max_transcodes(db),
        "blocked_agents": _raw(db, CONFIG_BLOCKED_AGENTS) or "",
        "allowed_agents": _raw(db, CONFIG_ALLOWED_AGENTS) or "",
        # Drive 302 直链总开关（#957 的回滚闸；缺省开）
        "drive_direct_play_enabled": _config_bool(db, KEY_DRIVE_DIRECT_PLAY, True),
    }


def write_policy(db: Session, values: dict) -> dict:
    """写回策略（只认白名单里的键；非法值保持原值不动）。

    配置瘦身第 4 批 4-5：落库动作收敛到 ``config_service.save_config``。
    校验/归一保留在本函数（短名→键翻译、白名单、类型钳制、未知键与非法值
    跳过），行为与收敛前逐字一致；落库+失效走正统通道。
    """
    applied: dict = {}
    for field, value in (values or {}).items():
        # 页面送的是短名（policy_payload 的字段名），先翻译成 SystemConfig 键；
        # 直接送长键名也兼容（FIELD_TO_KEY.get 缺省回退自身）
        key = FIELD_TO_KEY.get(field, field)
        if key not in POLICY_KEYS:
            continue
        if POLICY_KEYS[key] == "int":
            try:
                number = int(float(str(value).strip() or "0"))
            except (TypeError, ValueError):
                continue
            text = str(max(0, number))
        elif POLICY_KEYS[key] == "bool":
            text = "true" if str(value).strip().lower() in ("true", "1", "yes", "on") else "false"
        else:
            text = str(value or "").strip()[:500]
        applied[key] = text
    if applied:
        # 统一写入通道：校验（纵深防御，非法值本函数已过滤）→ 批量落库 → 写时失效。
        # 原来这里的 store.invalidate() 全清由正统通道的按键失效承接（只动了
        # applied 里的键，读侧等价；提交仍由调用方按现有事务边界决定）。
        config_service.save_config(db, applied, schema="playback_policy")
    return applied
