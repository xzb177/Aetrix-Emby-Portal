"""播放与客户端策略（v2.26.0，2026-10 简化；2026-10-17 转码删除版）

以前这些策略散在两处：一部分是环境变量（``EMBY_MAX_TRANSCODES`` / ``EMBY_TRANSCODE_IDLE``，
改一次要登机器重启进程），一部分是设置页里的下载 / 设备开关。运营想要的是
「客户端这边到底允许做什么」——一句话能回答的问题。

2026-10-17 用户拍板**删除服务端转码整套**（生产实查近 7 天零使用）：
``playback_transcode_enabled`` / ``playback_max_concurrent_transcodes`` 两键作废，
写了也不生效、payload 里不出现（旧库残留行照常忽略）。剩下的策略只有：

- **Drive 302 直链开关**（``drive_direct_play_enabled``，默认开）：关掉则 Drive 条目
  走服务端 302 到对象存储直链名单（playback_strategy.direct），否则 302 到谷歌直链。
（2026-10-17 瘦身：客户端黑白名单 ``client_allowed_agents`` / ``client_blocked_agents``
已随用户拍板删除，不再做 UA 准入。）

2026-10 删除服务端码率钳制：客户端要多少码率给多少（学 Linger 薄服务器思路）。

判定口径与 ``backend/subscriptions.py`` 一致：**管理员不受限**（排障时不能被自己的策略挡住）。

落库仍然是 ``SystemConfig`` 键值（EM 与 EA 用的是同一个库，见 deploy-ea.md），
所以面板上改完最多 60 秒生效（同一进程内保存即生效，跨进程靠短 TTL 兜底）。
"""

from __future__ import annotations


from sqlalchemy.orm import Session

from backend import models
from backend.emby_server.playback_strategy import KEY_DRIVE_DIRECT_PLAY
from backend.integrations import store

# 转码已删除（2026-10-17 用户拍板）：playback_transcode_enabled /
# playback_max_concurrent_transcodes 两键作废，不再读写。

# 键 → 类型（设置页与 PUT 接口共用一份，避免两边各写一套白名单）
POLICY_KEYS = {    # Drive 302 直链总开关（#957，playback_strategy.KEY_DRIVE_DIRECT_PLAY 定义，
    # video_stream 读它决定 302/410；键名本身就是页面字段名，无需短名映射）
    KEY_DRIVE_DIRECT_PLAY: "bool",
}

# 各键的出厂默认值（字符串形态，与 _config_bool / _config_int 的缺省语义一致）。
# 配置自愈（backend/config_self_heal.py）引用这份表补缺失行 —— 默认值只许在这里定义一次。
POLICY_DEFAULTS = {
    KEY_DRIVE_DIRECT_PLAY: "true",  # 与 playback_strategy.drive_direct_play_enabled 缺省一致
}

# 页面字段短名 → SystemConfig 键（GET policy_payload 返回短名，PUT 必须认短名；
# 2026-10-17 实锤：前端一直送短名，write_policy 只认长键名导致保存静默失效）
FIELD_TO_KEY = {
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


def policy_payload(db: Session) -> dict:
    """当前策略（面板读一份，前端不维护默认值）"""
    return {        # Drive 302 直链总开关（#957 的回滚闸；缺省开）
        "drive_direct_play_enabled": _config_bool(db, KEY_DRIVE_DIRECT_PLAY, True),
    }


def write_policy(db: Session, values: dict) -> dict:
    """写回策略（只认白名单里的键；非法值保持原值不动）"""
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
        row = db.query(models.SystemConfig).filter(models.SystemConfig.key == key).first()
        if row:
            row.value = text
        else:
            db.add(models.SystemConfig(key=key, value=text))
        applied[key] = text
    store.invalidate()  # 写时失效：同一进程内保存即生效
    return applied
