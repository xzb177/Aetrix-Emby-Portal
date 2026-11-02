# -*- coding: utf-8 -*-
"""苹果 CMS 新媒体库类型一期试点跑数（验收衔接单）。

对一期试点 3 家（非凡/最大/暴风，生产 upstream_sources id 7/8/9）
各拉有界页数走一遍真实同步工人（临时 SQLite 库，不碰生产库），
产出定二期口径要的真实数字：

- 同步行数（added / mapped / 各类 skipped / dedupe_hits / pages）
- detail 请求量（同步期设计为 0——本脚本按请求 URL 实数）
- 每源耗时与总耗时
- 被限速情况（HTTP 429 计数）与其它状态码分布
- TMDB 对上率抽样：对同步出的前 N 行用与 enrich 同一个
  ``tmdb_client.search``（标题+年份高置信搜索）实测命中率；
  环境没配 TMDB key 时该项输出 null 并注明未测，不编数字。

用法（仓库根目录）：
    python3 scripts/applecms_library_pilot.py [--pages 3]
        [--sample 20] [--json out.json]

红线：只读源站公开接口；不读生产库、不写生产库、不碰任何用户
凭据；源地址是公开采集端点（与生产 upstream_sources 里登记的
一致），UA 用与生产登记一致的浏览器 UA。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("UPSTREAM_ITEM_MAPPING_ENABLED", "1")

PILOT_SOURCES = [
    ("非凡资源", "http://ffzy5.tv"),
    ("最大资源", "https://api.zuidapi.com"),
    ("暴风资源", "https://bfzyapi.com"),
]
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0.0.0 Safari/537.36")


class CountingHttpGet:
    """包住 fetcher 的默认 GET：统计请求数/状态码/耗时，透传结果。"""

    def __init__(self):
        from backend.emby_server import applecms_fetcher as acf
        self._real = acf._default_cms_http_get
        self.requests = 0
        self.detail_requests = 0
        self.statuses = Counter()
        self.seconds = 0.0

    def __call__(self, url, headers=None, timeout=None):
        started = time.monotonic()
        self.requests += 1
        query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        if (query.get("ac") or [""])[0] == "detail":
            self.detail_requests += 1
        try:
            status, payload = self._real(
                url, dict(headers or {}),
                timeout if timeout is not None else 20.0)
        finally:
            self.seconds += time.monotonic() - started
        self.statuses[str(status)] += 1
        return status, payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pages", type=int, default=3,
                        help="每源最多拉多少页 list（默认 3）")
    parser.add_argument("--sample", type=int, default=20,
                        help="TMDB 对上率抽样行数（默认 20，0=不测）")
    parser.add_argument("--json", default="",
                        help="结果 JSON 写到该路径（同时打到 stdout）")
    args = parser.parse_args()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from backend import models as base_models
    from backend.emby_server import applecms_library_sync as als
    from backend.emby_server import models as em

    engine = create_engine("sqlite:///:memory:")
    base_models.Base.metadata.create_all(engine)
    em.Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()

    for idx, (name, base_url) in enumerate(PILOT_SOURCES, start=7):
        db.add(em.UpstreamSource(
            id=idx, name=name, source_type="applecms",
            base_url=base_url, enabled=True, user_agent=BROWSER_UA))
    lib = em.Library(
        guid="pilot-cms-movies", name="CMS电影（试点）",
        collection_type="movies", is_enabled=True, kind="applecms",
        upstream_source_ids=",".join(
            str(i) for i in range(7, 7 + len(PILOT_SOURCES))))
    db.add(lib)
    db.commit()

    http = CountingHttpGet()
    started = time.monotonic()
    stats = als.sync_library(db, lib, http_get=http, full=True,
                             max_pages=args.pages, sleep_fn=lambda s: None)
    wall = time.monotonic() - started

    report = {
        "pages_per_source_cap": args.pages,
        "sync": {k: stats[k] for k in (
            "added", "updated", "existing", "mapped", "pages",
            "skipped_category", "skipped_series_shape",
            "skipped_invalid", "skipped_other_library",
            "dedupe_hits", "errors", "blocked")},
        "seen_by_source": {str(k): len(v) for k, v in
                           (stats.get("seen_by_source") or {}).items()},
        "http": {"requests": http.requests,
                 "detail_requests": http.detail_requests,
                 "statuses": dict(http.statuses),
                 "rate_limited_429": http.statuses.get("429", 0),
                 "seconds": round(http.seconds, 2)},
        "wall_seconds": round(wall, 2),
        "tmdb_match": None,
    }

    if args.sample > 0:
        report["tmdb_match"] = _tmdb_sample(db, lib, args.sample)

    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0


def _tmdb_sample(db, lib, sample: int) -> dict:
    """对同步出的前 N 行实测 TMDB 高置信搜索命中率（enrich 同口径）。

    环境没配 key 时如实报未测；瞬态失败的行单列，不混进未命中。
    """
    from backend.emby_server import models as em
    from backend.emby_server.tmdb import tmdb_client

    if not tmdb_client.configured:
        return {"measured": False,
                "reason": "tmdb_not_configured_in_this_env"}
    rows = (db.query(em.MediaItem)
            .filter(em.MediaItem.library_id == lib.id)
            .order_by(em.MediaItem.id)
            .limit(sample).all())
    hits = misses = transient = 0
    started = time.monotonic()
    for row in rows:
        try:
            found = tmdb_client.search(
                row.name, row.production_year, "movie")
        except Exception:
            transient += 1
            continue
        if found:
            hits += 1
        else:
            misses += 1
    done = hits + misses
    return {
        "measured": True, "sampled": len(rows), "hits": hits,
        "misses": misses, "transient_errors": transient,
        "match_rate": round(hits / done, 4) if done else None,
        "seconds": round(time.monotonic() - started, 2),
    }


if __name__ == "__main__":
    raise SystemExit(main())
