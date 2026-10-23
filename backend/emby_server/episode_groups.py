"""StrmAssistant #15 对标：TMDB 剧集组刮削

TMDB Episode Groups：同一剧有多种集顺序（首播顺序/DVD 顺序/绝对顺序），
如《火影忍者》按 DVD 版分季与按播出分季不同。

StrmAssistant 只有 UI 选项字符串，无实际实现。本实现提供基础能力：
1. 取某剧的剧集组列表（/tv/{id}/episode_groups）
2. 取某组的集映射（/tv/episode_group/{id}）
3. 允许用户选择用哪个分组的顺序展示

数据模型：复用 SystemConfig 存用户选择的分组（按剧）。
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

# SystemConfig key 前缀：某剧选用的剧集组
# 格式：tmdb_episode_group_{tmdb_id} = group_id
EPISODE_GROUP_CONFIG_PREFIX = "tmdb_episode_group_"


# 剧集组数据几乎静态（TMDB 上几年不变），磁盘长 TTL 缓存（默认 30 天）。
# 避免详情页每次打开都打 TMDB、烧配额。


def get_episode_groups(tmdb_id: str) -> list[dict]:
    """取某剧的所有剧集组（对标 TMDB /tv/{id}/episode_groups）。

    返回 [{"id": group_id, "name": 组名, "type": 类型, "episode_count": 集数}]
    磁盘缓存（数据几乎静态）。
    """
    from backend.emby_server import tmdb_cache
    from backend.emby_server.tmdb import tmdb_client

    tmdb_id = str(tmdb_id or "").strip()
    if not tmdb_id:
        return []
    # 先读缓存
    try:
        hit, cached = tmdb_cache.load_details("episode_groups", tmdb_id, "en-US")
        if hit and isinstance(cached, list):
            return cached
    except Exception:
        pass

    client = tmdb_client
    try:
        data = client._get(f"/tv/{tmdb_id}/episode_groups", {"language": "en-US"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("取剧集组失败 %s: %s", tmdb_id, exc)
        return []

    groups = (data or {}).get("results") or []
    out = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        try:
            episode_count = int(g.get("episode_count") or 0)
        except (TypeError, ValueError):
            episode_count = 0
        out.append({
            "id": str(g.get("id") or ""),
            "name": str(g.get("name") or ""),
            "type": str(g.get("type") or ""),
            "episode_count": episode_count,
        })
    result = [g for g in out if g["id"]]
    # 写缓存
    try:
        tmdb_cache.save_details("episode_groups", tmdb_id, result, "en-US", raw=result)
    except Exception:
        pass
    return result


def get_episode_group_detail(group_id: str) -> Optional[dict]:
    """取某剧集组的详细信息（含每集的顺序映射）。

    返回 {"id":..., "name":..., "groups": [{"id":..., "name":..., "episodes":[...]}]}
    每个 episode 含 season_number/episode_number/order。
    磁盘缓存 30 天（数据几乎静态）。
    """
    from backend.emby_server import tmdb_cache
    from backend.emby_server.tmdb import tmdb_client

    group_id = str(group_id or "").strip()
    if not group_id:
        return None
    try:
        hit, cached = tmdb_cache.load_details("episode_group", group_id, "en-US")
        if hit and isinstance(cached, dict):
            return cached
    except Exception:
        pass

    client = tmdb_client
    try:
        data = client._get(f"/tv/episode_group/{group_id}", {"language": "en-US"})
    except Exception as exc:  # noqa: BLE001
        logger.debug("取剧集组详情失败 %s: %s", group_id, exc)
        return None
    if data:
        try:
            tmdb_cache.save_details("episode_group", group_id, data, "en-US", raw=data)
        except Exception:
            pass
    return data


def get_selected_group(db, tmdb_id: str) -> str:
    """取用户为某剧选择的剧集组（空串 = 用默认播出顺序）。"""
    from backend.integrations import store

    return store.get_value(db, f"{EPISODE_GROUP_CONFIG_PREFIX}{tmdb_id}", "")


def set_selected_group(db, tmdb_id: str, group_id: str) -> None:
    """设置用户为某剧选择的剧集组（空串 = 恢复默认）。"""
    from backend.integrations import store

    store.write_values(db, {f"{EPISODE_GROUP_CONFIG_PREFIX}{tmdb_id}": group_id or ""})
    db.commit()
