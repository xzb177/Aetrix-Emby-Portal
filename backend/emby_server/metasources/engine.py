"""多源元数据：采集引擎（按顺序取字段 · 失败隔离 · 外部 ID 合并）

## 三条规则

1. **按顺序取字段**：每个字段（标题 / 简介 / 类型 / 评分 / 年份 / 海报）独立地
   「谁先给就用谁」。不是「第一个搜到的源通吃」——某个源常常只有标题没有简介，
   让它把简介也让出去，结果就是简介永远空着。
2. **单源失败不中断**：任何一个源抛异常（网络、限流、密钥失效、反爬）都被隔离成一条
   失败记录，**继续问下一个源**。失败原因逐条返回，管理后台能看出「是哪一站在抽风」。
3. **外部 ID 合并**：所有有命中的源的外部 ID 一起保存（`MediaItem.external_ids`），
   与「谁赢了标题」无关 —— 这是以后换源、跨源去重唯一能用的线索。

## 中文优先开关怎么生效

它只影响**已经命中且语言不同**的两个源之间怎么取舍：开启时，中文源的标题 / 简介
压过优先级更高但给英文的源；关闭时严格按配置顺序。
类型、评分、海报、外部 ID 不受它影响 —— 那些不是「本地化」问题。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from backend.emby_server.metasources import config as cfg_mod
from backend.emby_server.metasources import keypool, sources

logger = logging.getLogger(__name__)

# 会被「按序填充」的字段（外部 ID 是合并，不是取第一个）
MERGE_FIELDS = ("title", "original_title", "overview", "genres", "rating", "year", "poster")
# 中文优先只作用在这两个字段上（本地化问题）
LOCALIZED_FIELDS = ("title", "overview")

# 安全上限：一次采集最多问几个源 / 多久（见 config.py 的说明）
MAX_SOURCES = cfg_mod.MAX_SOURCES
COLLECT_TIMEOUT = cfg_mod.COLLECT_TIMEOUT_SEC


@dataclass
class SourceOutcome:
    """一个源这一轮的结果（成功 / 没搜到 / 失败，失败原因原样带上）"""

    source: str
    label: str
    ok: bool
    hit: bool = False
    error: str = ""
    elapsed_ms: int = 0
    skipped: str = ""

    def as_dict(self) -> dict:
        return {
            "source": self.source, "label": self.label, "ok": self.ok, "hit": self.hit,
            "error": self.error, "elapsed_ms": self.elapsed_ms, "skipped": self.skipped,
        }


@dataclass
class CollectResult:
    """一轮采集的完整结论"""

    fields: dict = field(default_factory=dict)
    external_ids: dict = field(default_factory=dict)
    outcomes: list = field(default_factory=list)
    prefer_chinese: bool = True
    _primary: str = ""

    @property
    def primary(self) -> str:
        """哪个源赢了标题（管理后台显示用；空字符串 = 都没命中）"""
        if self._primary:
            return self._primary
        return self.outcomes[0].source if self.outcomes and self.outcomes[0].hit else ""

    @property
    def any_hit(self) -> bool:
        return bool(self.fields)

    def as_dict(self) -> dict:
        return {
            "fields": dict(self.fields),
            "external_ids": dict(self.external_ids),
            "outcomes": [o.as_dict() for o in self.outcomes],
            "primary": self.primary,
            "prefer_chinese": self.prefer_chinese,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "CollectResult":
        """从 :meth:`as_dict` 的产物重建（IO 阶段与写库阶段分开的场合用）"""
        payload = payload or {}
        outcomes = [SourceOutcome(**{k: v for k, v in (o or {}).items() if k in
                                     SourceOutcome.__dataclass_fields__})
                    for o in (payload.get("outcomes") or [])]
        result = cls(prefer_chinese=bool(payload.get("prefer_chinese", True)))
        result.fields = dict(payload.get("fields") or {})
        result.external_ids = dict(payload.get("external_ids") or {})
        result.outcomes = outcomes
        result._primary = str(payload.get("primary") or "")
        return result


def _douban_switch_on(db) -> bool:
    """豆瓣可选开关（``douban_enabled``）是否开启。

    ``douban.enabled`` 本身读不到配置时已 fail-closed（返回 False），
    这里再兜一层异常同样按关处理：引擎默认不打豆瓣请求。
    """
    try:
        from backend.emby_server import douban as _dbn
        return bool(_dbn.enabled(db))
    except Exception:  # noqa: BLE001
        return False


def _douban_banned_now() -> bool:
    """豆瓣全局冷却是否生效（封禁中）。

    判定不了（异常）时返回 False：不改变现有行为，保守。
    """
    try:
        from backend.emby_server import douban as _dbn
        return _dbn.ban_remaining() > 0
    except Exception:  # noqa: BLE001
        return False


def _fetch_one(source_cfg, title: str, year: Optional[int], kind: str) -> Optional[sources.Hit]:
    """问一个源；失败抛 ``SourceError``（由调用方隔离）"""
    spec = sources.SPEC_BY_ID.get(source_cfg.id)
    if spec is None:
        raise sources.SourceError("未知数据源")
    pool = keypool.pool_for(source_cfg.id, source_cfg.keys)
    gate = keypool.RateGate(source_cfg.id, source_cfg.rate)
    if spec.requires_key and not pool.usable:
        raise sources.SourceError("没有可用密钥")
    return spec.search(title, year, kind, pool=pool, gate=gate)


def collect(db, title: str, year: Optional[int] = None, kind: str = "series",
            *, limit: Optional[int] = None, only: Optional[str] = None,
            ignore_switch: bool = False, snapshot=None) -> CollectResult:
    """按配置顺序采集一条，返回归并后的字段与逐源结果

    总开关关掉时**直接返回空结果**（调用方据此走原来的 NFO → TMDB → 豆瓣链路），
    不做任何网络请求。

    ``only`` / ``ignore_switch`` 只给后台的「试采集」用：开一个源之前先看它
    能不能搜到这部片——这时不应该因为开关还关着、或源被关掉了就什么都不问。
    ``snapshot`` 可以由调用方传入（worker 已经开过会话了，不必再开一次）。
    """
    snapshot = snapshot if snapshot is not None else cfg_mod.snapshot(db, sources.SPECS)
    result = CollectResult(prefer_chinese=snapshot.prefer_chinese)
    if not snapshot.enabled and not ignore_switch:
        return result

    # 试采集（only / ignore_switch）时不按开关过滤，否则“测一下这个源”永远测不到东西
    targets = ([s for s in snapshot.sources if s.id == only] if only
               else list(snapshot.sources))
    if limit is not None:
        targets = targets[:max(0, int(limit))]

    hits: list = []
    started = time.monotonic()
    asked = 0
    for source_cfg in targets:
        spec = sources.SPEC_BY_ID.get(source_cfg.id)
        label = spec.label if spec else source_cfg.id
        # 不参与的源也要在结果里说清楚“为什么没问”——否则界面只能看到少了几行
        if only is None:
            if not source_cfg.enabled:
                result.outcomes.append(SourceOutcome(
                    source_cfg.id, label, False, skipped="已在后台关闭"))
                continue
            if source_cfg.needs_key:
                result.outcomes.append(SourceOutcome(
                    source_cfg.id, label, False, skipped="需要密钥但一个都没配"))
                continue
            # 批 1（刮削瘦身）：豆瓣降为可选开关——开关关闭时引擎内豆瓣源
            # 直接跳过，不打出任何豆瓣请求。只拦自动采集：后台「试采集」
            # （only 指定源）是管理员手动点的，不拦。
            if source_cfg.id == "douban" and not _douban_switch_on(db):
                result.outcomes.append(SourceOutcome(
                    source_cfg.id, label, False, skipped="豆瓣开关已关闭，已跳过"))
                continue
            # 链路规划 Step 0.5：豆瓣封禁期跳过豆瓣源——search_douban 自己直发
            # HTTP、不查封禁状态，这里拦下才能保证封禁期零豆瓣请求（反风控红线）。
            # 只拦自动采集：后台「试采集」（only 指定源）是管理员手动点的，不拦。
            if source_cfg.id == "douban" and _douban_banned_now():
                result.outcomes.append(SourceOutcome(
                    source_cfg.id, label, False, skipped="豆瓣封禁中，已跳过"))
                continue
            if asked >= MAX_SOURCES:
                result.outcomes.append(SourceOutcome(
                    source_cfg.id, label, False,
                    skipped=f"本轮最多问 {MAX_SOURCES} 个源，已达上限"))
                continue
        if time.monotonic() - started > COLLECT_TIMEOUT:
            result.outcomes.append(SourceOutcome(
                source_cfg.id, label, False, skipped="本轮时间预算用尽，后面的源没问"))
            continue
        began = time.monotonic()
        asked += 1
        try:
            hit = _fetch_one(source_cfg, title, year, kind)
        except sources.SourceError as exc:
            result.outcomes.append(SourceOutcome(
                source_cfg.id, label, False, error=str(exc)[:200],
                elapsed_ms=int((time.monotonic() - began) * 1000)))
            continue
        except Exception as exc:  # noqa: BLE001 — 任何意外都不能带崩整轮采集
            result.outcomes.append(SourceOutcome(
                source_cfg.id, label, False, error=f"{type(exc).__name__}: {exc}"[:200],
                elapsed_ms=int((time.monotonic() - began) * 1000)))
            continue
        elapsed = int((time.monotonic() - began) * 1000)
        if hit is None:
            result.outcomes.append(SourceOutcome(source_cfg.id, label, True, hit=False,
                                                 elapsed_ms=elapsed))
            continue
        hits.append(hit)
        result.outcomes.append(SourceOutcome(source_cfg.id, label, True, hit=True,
                                             elapsed_ms=elapsed))

    _merge(result, hits)
    return result


def _merge(result: CollectResult, hits: list) -> None:
    """按顺序填充字段 + 合并外部 ID（中文优先只影响 title / overview）"""
    for hit in hits:
        for name, value in hit.external_ids.items():
            if value:
                result.external_ids.setdefault(name, value)

    ordered = list(hits)
    if result.prefer_chinese:
        # 中文源的标题 / 简介排前面（只动这两个字段的取值顺序）。
        # hit.lang 由各源自己标（AniList 能看出 native 是不是中文），没标就用源的属性。
        def _lang(hit) -> str:
            return str(getattr(hit, "lang", "") or "")

        zh = [h for h in hits if _lang(h) == "zh"]
        rest = [h for h in hits if _lang(h) != "zh"]
        ordered = zh + rest

    for name in MERGE_FIELDS:
        if name in LOCALIZED_FIELDS and result.prefer_chinese:
            candidates = ordered
        else:
            candidates = hits
        for hit in candidates:
            if hit.has(name):
                result.fields[name] = getattr(hit, name)
                break


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------

def _load_external_ids(item) -> dict:
    raw = getattr(item, "external_ids", "") or ""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def apply_to_item(item, result: CollectResult, *, fill_missing_only: bool = True) -> list:
    """把归并结果落到条目上，返回实际写入的字段名列表

    默认**只补空缺**（与原豆瓣兜底同口径）：多源是补漏的，不是推翻重来的。
    修复队列（``repair_requested_at``）触发的这轮可以覆盖（``fill_missing_only=False``）。
    """
    if not result.fields and not result.external_ids:
        return []
    changed: list = []

    title = result.fields.get("title")
    if title and (not fill_missing_only or not (item.name or "").strip()):
        from backend.emby_server.tmdb import clean_title
        # 统一标题口径（一套横切）：冒号副标题/控制符不进 item.name（问题二）；
        # 清完为空（全是脏字符）就不写，保持空名等下一轮
        cleaned = clean_title(title)
        if cleaned and cleaned != item.name:
            item.name = cleaned
            changed.append("name")
    overview = result.fields.get("overview")
    if overview and (not fill_missing_only or not (item.overview or "").strip()):
        if overview != item.overview:
            item.overview = overview
            changed.append("overview")
    genres = result.fields.get("genres")
    if genres and (not fill_missing_only or not (item.genres or "").strip()):
        text = ",".join(str(g) for g in genres if g)
        if text and text != (item.genres or ""):
            item.genres = text
            changed.append("genres")
    rating = result.fields.get("rating")
    if rating is not None and (not fill_missing_only or item.community_rating is None):
        if item.community_rating != rating:
            item.community_rating = rating
            changed.append("community_rating")
    year = result.fields.get("year")
    if year and (not fill_missing_only or not item.production_year):
        if item.production_year != year:
            item.production_year = year
            changed.append("production_year")
    poster = result.fields.get("poster")
    # 只写 URL，不在这里下载：写库阶段发 HTTP 会攥住数据库写锁（本模块自己的纪律）。
    # 本地化交给图片预热 / 取图时的按需自愈，与 TMDB / 豆瓣兜底图同一口径。
    if poster and (not fill_missing_only or not (item.poster_path or item.primary_image_url)):
        if poster != item.primary_image_url:
            item.primary_image_url = poster
            changed.append("primary_image_url")

    before_ids = _load_external_ids(item)
    ids = dict(before_ids)
    ids.update({k: v for k, v in (result.external_ids or {}).items() if v})
    # 与已有独立列保持一致（前端与求片链路都直接读 tmdb_id / imdb_id）。
    # **列里的值优先于本轮采到的**：已有 tmdb_id 的条目重刮时不能让新采的值
    # 把 JSON 里的对应项改掉，否则两份记录就对不上了。
    if item.tmdb_id:
        ids["tmdb"] = item.tmdb_id
    elif result.external_ids.get("tmdb"):
        item.tmdb_id = result.external_ids["tmdb"]
        changed.append("tmdb_id")
    if item.imdb_id:
        ids["imdb"] = item.imdb_id
    elif result.external_ids.get("imdb"):
        item.imdb_id = result.external_ids["imdb"]
        changed.append("imdb_id")
    if ids != before_ids:
        item.external_ids = json.dumps(ids, ensure_ascii=False, sort_keys=True)
        changed.append("external_ids")

    if changed and not getattr(item, "metadata_source", None):
        item.metadata_source = result.primary or "multisource"
    return changed


__all__ = ["CollectResult", "MERGE_FIELDS", "SourceOutcome", "apply_to_item", "collect"]
