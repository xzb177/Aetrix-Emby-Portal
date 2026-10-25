"""苹果 CMS 生产接线（组装层）：把同步工人的可注入依赖接到真实实现。

定位（用户已定，苹果 CMS 与 Emby/Jellyfin 并列，绝不做兜底定位）：
- 本模块只给 ``applecms_sync`` 供生产适配器：抓取（W2 fetcher）、
  绑定列出（W1）、本地候选（本库 MediaItem）、映射写入（W1）、
  健康写入（25-1）。不建货架面、不读播放地址、不新增路由。
- 手动触发先行：唯一入口 ``production_trigger_applecms_sync`` 供
  未来管理路由调用；定时调度后置——本批不碰 ``worker.py``，不
  发明调度间隔（见 PR 说明跟进项）。
- 事务口径：映射与健康只 flush，入口成功时一次 commit、异常时
  rollback 后重抛；调用方不另行 commit。
- 本地候选缓存：一次触发内只查一遍库（首个绑定调用时加载，后
  续绑定复用）；新触发重新加载，不跨触发缓存。
- 健康只写允许的三值：适配器经 ``set_health_status`` 写库（该函
  数自带允许集校验，非法值直接报错，绝不静默写脏值）。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional

from backend.emby_server import applecms_fetcher as acf
from backend.emby_server import applecms_sync as acs
from backend.emby_server import models as em
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server import upstream_sources as ups


def _source_headers(source) -> dict:
    """发往 CMS 的请求头：照 ``upstream_playback._source_headers`` 语义
    镜像（源上配的 UA / Referer / 附加头；凭据绝不走这里）。"""
    headers: dict = {"Accept": "*/*"}
    if getattr(source, "user_agent", None):
        headers["User-Agent"] = source.user_agent
    if getattr(source, "referer", None):
        headers["Referer"] = source.referer
    try:
        extra = json.loads(getattr(source, "extra_headers_json", None) or "{}")
    except (TypeError, ValueError):
        extra = {}
    if isinstance(extra, dict):
        for key, value in extra.items():
            if isinstance(key, str) and isinstance(value, str):
                headers[key] = value
    return headers


def make_fetch_page(http_get: Optional[Callable] = None):
    """生产抓取页：``sync_source`` 的 ``fetch_page(source, category,
    page, h=None)`` 签名 → W2 ``fetch_page``（ac=list，t=分类）。"""
    def fetch_page(source, category, page, h=None):
        return acf.fetch_page(
            source.base_url, ac="list", pg=page, t=category, h=h,
            http_get=http_get, headers=_source_headers(source))
    return fetch_page


def make_fetch_detail(http_get: Optional[Callable] = None):
    """生产详情补全：只在 list 条目缺名/缺年时由同步工人调用一次。"""
    def fetch_detail(source, vod_id):
        return acf.fetch_detail(
            source.base_url, vod_id,
            http_get=http_get, headers=_source_headers(source))
    return fetch_detail


def make_list_bindings(db):
    """生产绑定列出：W1 ``list_category_bindings``（含停用的行，
    启用过滤由同步工人的 ``_binding_enabled`` 做）。"""
    def list_bindings(source):
        return ups.list_category_bindings(db, source.id)
    return list_bindings


def _load_live_locals(db) -> List[dict]:
    """本库可配对候选：只取可播叶子（电影 + 单集），排除系列/季
    结构行、软删除行与合并副本行。

    - 电影：自带标题与年份。
    - 单集：标题与年份取所属系列行（集标题是剧情标题，不用于配
      对）；系列行缺失时标题/年份为空，同步工人的键函数会跳过它。
    - item_type 归一化为同步工人识别的 ``Movie`` / ``Episode``，
      不依赖库里大小写口径；集号原样透传，组内排序用它。
    """
    rows = (db.query(em.MediaItem)
            .filter(em.MediaItem.deleted_at.is_(None),
                    em.MediaItem.merged_into_id.is_(None))
            .all())
    series = {r.id: r for r in rows
              if (r.item_type or "").lower() == "series"}
    locals_: List[dict] = []
    for row in rows:
        kind = (row.item_type or "").lower()
        if kind == "movie":
            locals_.append({
                "id": row.id, "title": row.name,
                "production_year": row.production_year,
                "item_type": "Movie", "episode_number": None,
            })
        elif kind == "episode":
            parent = series.get(row.series_id) if row.series_id else None
            locals_.append({
                "id": row.id,
                "title": parent.name if parent is not None else None,
                "production_year": (parent.production_year
                                    if parent is not None else None),
                "item_type": "Episode",
                "episode_number": row.episode_number,
            })
    return locals_


def make_list_locals(db):
    """生产本地候选：一次触发内首调加载、后续复用（见模块头）。"""
    cache: Dict[str, List[dict]] = {}

    def list_locals(binding):
        if "items" not in cache:
            cache["items"] = _load_live_locals(db)
        return cache["items"]
    return list_locals


def make_record_map(db):
    """生产映射写入：W1 ``record_mapping``（幂等 upsert，只 flush；
    episode_index 由同步工人传入，电影为 None）。"""
    def record_map(**kwargs):
        return uim.record_mapping(db, **kwargs)
    return record_map


def make_set_health(db):
    """生产健康写入：25-1 ``set_health_status``（只 flush，commit
    归入口；源行不存在时静默跳过，不毁同步结果）。"""
    def set_health(source_id, health):
        row = ups.get_source(db, source_id)
        if row is None:
            return None
        return ups.set_health_status(db, row, health)
    return set_health


def production_trigger_applecms_sync(db, source_id, *,
                                     http_get: Optional[Callable] = None,
                                     **sync_kwargs: Any):
    """手动触发一次苹果 CMS 同步（生产接线版）。

    接线全部生产适配器；成功时一次 commit（含映射与健康），异常
    时 rollback 后重抛。两闸关时同步工人直接返回 disabled 结果，
    不写任何东西（commit 为空操作）。``http_get`` 仅供测试注入，
    生产调用不传（走真实 HTTP）。
    """
    try:
        result = acs.trigger_applecms_sync(
            source_id, db=db,
            fetch_page=make_fetch_page(http_get),
            fetch_detail=make_fetch_detail(http_get),
            list_bindings=make_list_bindings(db),
            list_locals=make_list_locals(db),
            record_map=make_record_map(db),
            set_health=make_set_health(db),
            **sync_kwargs)
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
