"""Cloudflare 边缘缓存：通用能力（后端部分）

**定位**：把"CF 缓存视频"做成部署时定一次的能力——管理员在环境变量里选模式，
按文档在 CF 后台配好 Cache Rule，热门视频的首字节就从"回源悉尼"变成"CF 边缘命中"。
不配 = 今天的行为逐字节不变。

**三档模式**（环境变量 ``EDGE_CACHE_MODE``，热读，默认 ``off``）：

- ``off``（默认）：现状。只走 CF 代理，不声明缓存。服务端行为与今天逐字节一致。
- ``query_ignore``：声明"我已在 CF 配了忽略查询串的 Cache Rule"。
  服务端零行为变更（响应头本来就是可缓存的），只是在 /stream 响应上多带一个
  ``X-Edge-Cache-Mode: query-ignore`` 调试头，方便管理员验证 CF 是否真的命中。
  全客户端兼容（SenPlayer/Infuse/Emby 官方都无感知）。
- ``header``：预留（Phase 2，另立项）。本期只做声明位，不改变任何行为。

**安全**（模式 query_ignore）：见 ``docs/edge-cache.md`` §安全分析。核心是 opt-in——
默认 off，管理员知情后才开；共享 URL 在缓存 TTL 内可被未授权访问（对比现状：
签名 URL 本来就有 15 分钟～6 小时窗口）。

**降级**：CF 没配好 = MISS 回源走完整鉴权，不坏；切回 off 即刻生效（热读，无需重启）。
"""
from __future__ import annotations

import os

# 环境变量名（部署时定一次；docker-compose.yml / env.example 有注释说明）
ENV_EDGE_CACHE_MODE = "EDGE_CACHE_MODE"

# 合法模式。非法值一律归一化为 off（fail-closed：配错了也不开缓存声明）。
MODE_OFF = "off"
MODE_QUERY_IGNORE = "query_ignore"
MODE_HEADER = "header"
_VALID_MODES = (MODE_OFF, MODE_QUERY_IGNORE, MODE_HEADER)

# 调试头：query_ignore 模式下 /stream 响应带上，管理员看响应头就知道服务端声明了什么模式。
DEBUG_HEADER = "X-Edge-Cache-Mode"
DEBUG_HEADER_VALUE_QUERY_IGNORE = "query-ignore"


def mode() -> str:
    """当前边缘缓存模式（热读：每次调用都重新读环境变量）。

    返回 "off" / "query_ignore" / "header" 之一；未设置或非法值 → "off"。
    """
    raw = (os.environ.get(ENV_EDGE_CACHE_MODE) or "").strip().lower()
    # 兼容几种常见写法
    _aliases = {
        "query-ignore": MODE_QUERY_IGNORE,
        "queryignore": MODE_QUERY_IGNORE,
    }
    raw = _aliases.get(raw, raw)
    if raw in _VALID_MODES:
        return raw
    return MODE_OFF


def debug_header_value() -> str | None:
    """query_ignore 模式下的调试头值；其它模式返回 None（不加头）。"""
    if mode() == MODE_QUERY_IGNORE:
        return DEBUG_HEADER_VALUE_QUERY_IGNORE
    return None
