# -*- coding: utf-8 -*-
"""上游独有资源进库（一期：电影先行，2026-11-01 用户按推荐方案拍板）。

聚合配对之后，上游有、本地没有的作品此前只在对账计数里露面
（identity_no_local_match），用户在站里看不见、播不了。本模块把
其中「未配对 + Type=Movie + 带 tmdb_id」的独有电影落成本地行：

- 落点：按上游库名找本地**同名**库（只认非虚拟库，同名多个取 id
  最小）；找不到同名库本期跳过并计数——自动建库是二期；
- 建行：guid 按「源 id + 上游 Item Id」md5 确定性派生（32 位 hex，
  与 MediaItem.guid 列宽对齐），tmdb_id 落行，sort_name 走
  pinyin_sort.make_sort_name（与 scanner 同一构造点），打上游托管
  标记 MediaItem.upstream_source_id=源 id，probe_status 直接置
  done（无本地文件可探，probe 侧 needs 判定本就跳过无路径行，
  这里把状态写明），enrich 保持默认 pending——托管行无 file_path，
  enrich 的 TMDB-only 路径已兜住（见 enrich_worker._enrich_fetch_pre
  的托管分支），图片/简介走 TMDB 回填，绝不直用上游图（上游图片
  地址带对方 api_key，直用等于外泄凭据）；
- 同时写 upstream_item_mappings（local_item_id 指向新行）：播放三环
  （详情线路 / PlaybackInfo / 取流口）全靠映射寻址，没有映射的行
  播不了——所以映射写开关（UPSTREAM_ITEM_MAPPING_ENABLED）关时
  **不建行**，只计数（blocked=mapping_off），绝不建孤儿行；
- 幂等：guid 已存在即视为已进库（计数 existing，映射走
  record_mapping 的 upsert 刷新），重复跑不重复建行；
- limit：内部口（上线试点用），只限**新建**行数，调度路径不传
  = 不限量；已存在的行不占限额（试点重跑不被旧行挤掉名额）。

前置门（任一不过整批不建行，stats["blocked"] 记原因）：
- 源进库开关 import_enabled 关 → "switch_off"；
- 映射写开关关 → "mapping_off"。

候选口径与对账的 identity_no_local_match 等价：聚合作品任一线路
（本源）出现在本窗配对结果里即视为已配对，整部跳过——同一作品
在同源有多个条目时，第一条未配对线路建行，其余线路的映射一并
挂到这一行（同作品多线路与配对侧多版本同理）。

本模块不做（二期及以后）：剧/季/集进库、已配对剧缺集补齐、
生命周期对账（上游下架跟随）、自动建库、无身份条目进库。

—— 二期（2026-11-01，剧全量＋生命周期对账＋自动建库）：

- ``import_unpaired_series_tree``：独有剧整棵进库。剧行口径与
  一期电影同（guid 派生、托管标记、probe done、enrich pending、
  映射必写）；季行按 (剧行, 季号) 幂等合成（名「第 N 季」、
  照 scanner._ensure_season_row 惯例），集行挂季行（parent_id）
  与剧行（series_id），名用单集本名（聚合层 episode_name）。
  集找剧的定位顺序（身份级优先、标题只作最后手段，与 O-08
  系列级配对同纪律）：本窗配对结果（SeriesId → 已配对本地剧）
  → 映射表（SeriesId → 本地剧行）→ 托管 guid（SeriesId 派生）
  → 本窗已建/已配对（按归一剧名）→ 本源托管剧按名唯一 →
  本地活剧按名唯一；对不上计数 no_series 跳过。集缺季/集号
  计数 no_position 跳过（挂不到树上不硬塞）。
- 已配对剧补集：剧本身已配对（pairs 里有 Series 对）时不建剧
  行，其未配对单集按上款定位挂到本地剧行下（集行仍打托管
  标记、写映射）——本地剧缺的集从此能播，剧行本身不动。
- 无身份剧（无 tmdb_id，含方案拍板的 36 部）：不进库，计数
  no_identity；其单集因所属剧无行可挂一并 no_series 跳过。
  enrich 的 TMDB 路径与货架身份门都要求 tmdb，硬进只会生
  无图无线路的死行（拍板 7 原话：暂不进、补上身份再进）。
- 自动建库：落点解析顺序 = UpstreamLibraryLink 关系 → 同名
  真实库（一期口径）→ 自动新建同名真实库并落关系。只在调用
  方带上游库 id 时才建（无 id 无从落关系，保持一期「无同名
  跳过计数」）；建库按管理端 create_library 的 ORM 口径
  （guid uuid 前 32 位、realm 取当前服、默认策略），paths 为
  空（内容全是托管行，无本地路径可扫）、非虚拟；库的
  collection_type 取上游 CollectionType（抓取面已补收），
  取不到按内容类型推断。管理员改名后按关系找库，不重复建。
- 剧数限额（``series_limit``，内部试点口）：行限额做不出小
  批量完整树试点（窗内先建剧行后处理集行，行限额取值不是
  只有光秃剧行就是上百部），故另设按「新建独有剧树」计数
  的限额：接纳整树进、未接纳整树不进（其集按 SeriesId
  确定性跳过，不落标题回落误挂）；接纳/拒绝集合跨窗存活
  （``_SeriesBudget``，run 层整轮线程化）。默认 None =
  不限，旧口径逐字不变。
- ``reconcile_hosted_lifecycle``：生命周期对账（机制 4）。
  调用方（聚合 run）在整轮可信时调用：本轮见到的托管行清零
  缺失计数（对账藏过的恢复可见），没见到的 +1；连续
  HOSTED_HIDE_AFTER_MISSING_ROUNDS（2）轮不见隐藏、再 2 轮
  （累计 HOSTED_DELETE_AFTER_MISSING_ROUNDS=4）不见软删。
  剧隐藏/软删级联其托管季/集；单集单独消失只动单集；无子
  托管季行（集全删光）随轮回收。软删后上游重现：进库按 guid
  复活同一行（见 _usable_row_by_guid），不建双行。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Set

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.emby_server import models as em
from backend.emby_server import soft_delete
from backend.emby_server import upstream_aggregation as agg
from backend.emby_server import upstream_item_mappings as uim
from backend.emby_server.pinyin_sort import make_sort_name

logger = logging.getLogger(__name__)

#: 进库映射的置信度/证据：身份来自上游条目自带的 Tmdb ProviderId，
#: 与配对侧 tmdb 精确命中同档（CONFIDENCE_TMDB=1.0、evidence="tmdb"）。
IMPORT_CONFIDENCE = 1.0
IMPORT_EVIDENCE = "tmdb"


def hosted_guid(source_id: int, upstream_item_id: str) -> str:
    """托管行 guid：md5("upstream:{source_id}:{upstream_item_id}")。

    确定性派生是幂等的根：同一上游条目无论跑多少轮都算出同一个
    guid，存在性检查即可去重，不需要额外登记表。
    """
    return hashlib.md5(
        f"upstream:{source_id}:{upstream_item_id}".encode("utf-8")
    ).hexdigest()


def hosted_season_guid(source_id: int, series_local_id: int,
                       season_number: int) -> str:
    """托管季行 guid：md5("upstream:{source}:season:{剧行 id}:{季号}")。

    上游季不进聚合抓取（IncludeItemTypes 只有 Movie/Series/
    Episode），季行是按集号合成的结构行，没有上游条目 id 可
    派生；以所属剧的本地行 id 为锚（剧行一旦落定 id 恒定），
    与 scanner._ensure_season_row 的 (series.id, season_no)
    幂等键同义。
    """
    return hashlib.md5(
        f"upstream:{source_id}:season:{series_local_id}:{season_number}"
        .encode("utf-8")).hexdigest()


def _usable_row_by_guid(db: Session, guid: str):
    """按 guid 找托管行并规整到「可用行」，找不到返回 None。

    - 活行（未软删、未合并）→ 原行；
    - 软删未合并 → 复活（清 deleted_at / is_hidden）后返回：
      生命周期软删过的作品在上游重现时必须回到同一行，绝不
      撞 guid 唯一键另建双行；
    - 已合并 → 跟随 merged_into_id 返回存活行：映射在合并当时
      已迁到存活行，调用方对存活行记 existing 即可；跟随设
      上限防脏数据成环。
    """
    row = (db.query(em.MediaItem)
           .filter(em.MediaItem.guid == guid).first())
    if row is None:
        with soft_delete.include_deleted():
            row = (db.query(em.MediaItem)
                   .filter(em.MediaItem.guid == guid).first())
        if row is None:
            return None
        if row.deleted_at is not None and row.merged_into_id is None:
            row.deleted_at = None
            row.is_hidden = False
            db.flush()
            logger.info("上游托管行复活 guid=%s local_item_id=%s",
                        guid, row.id)
    hops = 0
    while row.merged_into_id is not None and hops < 8:
        target = (db.query(em.MediaItem)
                  .filter(em.MediaItem.id == row.merged_into_id).first())
        if target is None:
            return row
        row = target
        hops += 1
    return row


def _resolve_target_library(db: Session, upstream_library_name: str):
    """按上游库名找本地同名库：只认非虚拟库，同名多个取 id 最小。

    与 upstream_aggregate_run.resolve_target_library 的筛法同口径
    （那边是备注分类对位，这边是进库落点，两处都只认真实库）。
    """
    name = (upstream_library_name or "").strip()
    if not name:
        return None
    rows = (
        db.query(em.Library)
        .filter(em.Library.name == name)
        .order_by(em.Library.id.asc())
        .all()
    )
    for row in rows:
        if not row.is_virtual:  # NULL 与 False 同口径：非虚拟
            return row
    return None


#: 上游 CollectionType → 本地 collection_type 的直通映射（Emby 与
#: 本地同名同义的几类）；其余（homevideos / books 等本地无对应
#: 语义的）不硬套，按内容类型推断（见 _infer_collection_type）。
_COLLECTION_TYPE_MAP = {
    "movies": "movies", "tvshows": "tvshows",
    "mixed": "mixed", "music": "music",
}


def _infer_collection_type(upstream_collection_type: str,
                           content_hint: str) -> str:
    """自动建库的 collection_type：上游类型优先，推断兜底。

    方案定「按上游库类型取（抓取面补收 CollectionType）」；
    上游没给或给了本地无对应语义的类型时，按本窗内容推断
    （Movie→movies、Series/Episode→tvshows），再兜底 mixed。
    """
    raw = (upstream_collection_type or "").strip().lower()
    if raw in _COLLECTION_TYPE_MAP:
        return _COLLECTION_TYPE_MAP[raw]
    if content_hint == "Movie":
        return "movies"
    if content_hint in ("Series", "Episode"):
        return "tvshows"
    return "mixed"


def _linked_library(db: Session, source_id: int, upstream_library_id):
    """按 UpstreamLibraryLink 关系找本地库（自动建库的后续落点）。

    关系在、目标库已删 → 返回 None（调用方按无关系继续走同名/
    新建并刷新关系行）；目标是虚拟库同样不认（关系只由自动建库
    写，恒指向真实库，出现虚拟即数据异常，不拿它当落点）。
    """
    if not upstream_library_id:
        return None
    link = (db.query(em.UpstreamLibraryLink)
            .filter(em.UpstreamLibraryLink.source_id == source_id,
                    em.UpstreamLibraryLink.upstream_library_id
                    == str(upstream_library_id))
            .first())
    if link is None:
        return None
    lib = (db.query(em.Library)
           .filter(em.Library.id == link.local_library_id).first())
    if lib is None or lib.is_virtual:
        return None
    return lib


def _create_hosted_library(db: Session, source, *,
                           upstream_library_id: str,
                           upstream_library_name: str,
                           upstream_collection_type: str,
                           content_hint: str):
    """自动新建同名真实库 + 落关系行（同一 SAVEPOINT，幂等）。

    ORM 口径对齐管理端 create_library：guid = uuid4 hex 前 32
    位、realm 取当前服、scrape_policy 等走列默认；与管理端口径
    的刻意差别只有两点（本模块存在的理由）：不要求路径/挂载
    （内容全是托管行，paths 恒空）、不是 staff 接口而是系统
    内部路径。建库即审计：关系行（源/上游库/本地库/类型/时间）
    与一行 info 日志同时落。并发撞唯一键（另一轮刚建好）时
    降级为按关系/同名重查复用，不让整窗回滚（照 scanner 季行
    并发先例）。
    """
    name = (upstream_library_name or "").strip()
    ctype = _infer_collection_type(upstream_collection_type, content_hint)
    realm_id = None
    try:
        from backend import realms
        realm_id = realms.active_realm_id(db)
    except Exception:  # noqa: BLE001 — 服解析失败不许打死建库，realm 可空
        logger.warning("自动建库时取当前服失败，realm_id 留空 name=%r",
                       name, exc_info=True)
    lib = em.Library(
        guid=uuid.uuid4().hex[:32], name=name, collection_type=ctype,
        paths="", realm_id=realm_id, is_enabled=True, is_virtual=False)
    try:
        with db.begin_nested():
            db.add(lib)
            db.flush()
            link = (db.query(em.UpstreamLibraryLink)
                    .filter(
                        em.UpstreamLibraryLink.source_id == source.id,
                        em.UpstreamLibraryLink.upstream_library_id
                        == str(upstream_library_id))
                    .first())
            if link is None:
                db.add(em.UpstreamLibraryLink(
                    source_id=source.id,
                    upstream_library_id=str(upstream_library_id),
                    upstream_library_name=name,
                    local_library_id=lib.id, collection_type=ctype))
            else:
                link.local_library_id = lib.id
                link.collection_type = ctype
            db.flush()
    except IntegrityError:
        logger.info(
            "上游自动建库撞唯一键，降级复用 source_id=%s "
            "upstream_library_id=%s name=%r",
            source.id, upstream_library_id, name)
        lib = (_linked_library(db, source.id, upstream_library_id)
               or _resolve_target_library(db, name))
        return lib, False
    logger.info(
        "上游独有分类自动建库 source_id=%s upstream_library_id=%s "
        "local_library_id=%s name=%r collection_type=%s",
        source.id, upstream_library_id, lib.id, name, ctype)
    return lib, True


def _resolve_or_create_library(db: Session, source, *,
                               upstream_library_id,
                               upstream_library_name: str,
                               upstream_collection_type: str = "",
                               content_hint: str = ""):
    """二期落点解析：关系 → 同名真实库 → 自动建库。返回 (库, 是否新建)。

    只在带上游库 id 时才建库（关系要落 id，无 id 的调用保持
    一期口径：无同名即 (None, False)，由调用方计数跳过）。
    """
    lib = _linked_library(db, source.id, upstream_library_id)
    if lib is not None:
        return lib, False
    lib = _resolve_target_library(db, upstream_library_name)
    if lib is not None:
        return lib, False
    if not upstream_library_id:
        return None, False
    if not (upstream_library_name or "").strip():
        return None, False
    return _create_hosted_library(
        db, source, upstream_library_id=str(upstream_library_id),
        upstream_library_name=upstream_library_name,
        upstream_collection_type=upstream_collection_type,
        content_hint=content_hint)


def _zero_stats() -> Dict[str, object]:
    return {
        "candidates": 0, "imported": 0, "existing": 0,
        "no_library": 0, "limited": 0, "blocked": None,
        "libraries_created": 0,
    }


def import_unpaired_movies(db: Session, source, items: Iterable,
                           paired_upstream_ids: Set[str], *,
                           upstream_library_name: str,
                           limit: Optional[int] = None,
                           upstream_library_id: Optional[str] = None,
                           upstream_collection_type: str = ""
                           ) -> Dict[str, object]:
    """把一窗聚合作品里的未配对独有电影进库，返回统计 dict。

    只 flush 不 commit（调用方事务，与聚合写映射同口径）。
    ``paired_upstream_ids`` 是本窗配对命中的上游条目 id 集合。
    统计键：candidates（候选作品数）/ imported（新建行）/ existing
    （guid 已存在）/ no_library（无同名库跳过）/ limited（超限额
    跳过）/ blocked（前置门原因或 None）。
    """
    stats = _zero_stats()
    if not getattr(source, "import_enabled", True):
        stats["blocked"] = "switch_off"
        return stats
    if not uim.mapping_enabled():
        # 没映射的行播不了：绝不建孤儿行。
        stats["blocked"] = "mapping_off"
        return stats
    paired = set(paired_upstream_ids or ())
    target_library = None
    target_resolved = False
    for item in items or ():
        if (getattr(item, "item_type", "") or "") != "Movie":
            continue
        tmdb_id = (getattr(item, "tmdb_id", None) or "").strip()
        if not tmdb_id:
            continue
        source_lines = [
            line for line in (getattr(item, "lines", None) or [])
            if str(getattr(line, "source_id", "")) == str(source.id)
        ]
        if not source_lines:
            continue
        if any(line.upstream_item_id in paired for line in source_lines):
            continue  # 任一线路已配对 = 作品已有本地行，整部跳过
        stats["candidates"] += 1
        if not target_resolved:
            if upstream_library_id:
                target_library, _created = _resolve_or_create_library(
                    db, source, upstream_library_id=upstream_library_id,
                    upstream_library_name=upstream_library_name,
                    upstream_collection_type=upstream_collection_type,
                    content_hint="Movie")
                if _created:
                    stats["libraries_created"] += 1
            else:
                target_library = _resolve_target_library(
                    db, upstream_library_name)
            target_resolved = True
        if target_library is None:
            stats["no_library"] += 1
            continue
        local_row = None
        for line in source_lines:
            guid = hosted_guid(source.id, line.upstream_item_id)
            row = _usable_row_by_guid(db, guid)
            if row is None:
                if limit is not None and stats["imported"] >= limit:
                    stats["limited"] += 1
                    break
                row = em.MediaItem(
                    guid=guid,
                    library_id=target_library.id,
                    item_type="movie",
                    name=item.name,
                    sort_name=make_sort_name(item.name),
                    production_year=getattr(item, "production_year", None),
                    tmdb_id=tmdb_id,
                    imdb_id=(getattr(item, "imdb_id", None) or None),
                    upstream_source_id=source.id,
                    probe_status="done",
                )
                db.add(row)
                db.flush()
                stats["imported"] += 1
                logger.info(
                    "上游独有电影进库 source_id=%s upstream_item_id=%s "
                    "local_item_id=%s library_id=%s name=%r",
                    source.id, line.upstream_item_id, row.id,
                    target_library.id, item.name)
            elif local_row is None:
                stats["existing"] += 1
            local_row = local_row or row
            # 同作品多线路共用一行：映射逐条挂到同一本地行。
            uim.record_mapping(
                db, local_item_id=local_row.id, source_id=source.id,
                upstream_item_id=line.upstream_item_id,
                confidence=IMPORT_CONFIDENCE, evidence=IMPORT_EVIDENCE)
    return stats


# ---------------------------------------------------------------------------
# 二期：剧整棵进库（剧 / 季 / 集）＋ 已配对剧补集
# ---------------------------------------------------------------------------

def _zero_series_stats() -> Dict[str, object]:
    stats = _zero_stats()
    stats.update({
        "series_imported": 0, "episodes_imported": 0,
        "seasons_created": 0, "no_identity": 0, "no_series": 0,
        "no_position": 0, "series_limited": 0,
    })
    return stats


def _source_lines(item, source_id: int) -> List:
    return [
        line for line in (getattr(item, "lines", None) or [])
        if str(getattr(line, "source_id", "")) == str(source_id)
    ]


class _Limit:
    """本窗新建行限额（None = 不限）：剧/季/集共用一个计数。"""

    def __init__(self, limit: Optional[int]):
        self.remaining = limit

    def take(self) -> bool:
        if self.remaining is None:
            return True
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


class _SeriesBudget:
    """整轮剧数预算（跨窗存活）：series_limit 的接纳/拒绝登记。

    与 ``_Limit``（行限额）分工：``_Limit`` 管「一共能建多少
    行」，本对象管「能接纳几部新剧」——计数与接纳/拒绝集合
    必须跨窗存活（run 层整轮建一个、逐窗传入），单窗内的
    ``_Limit`` 计数方式套不到这里。

    - ``limit=None``：未启用——``can_admit`` 恒真但不登记，
      ``is_admitted`` / ``is_rejected`` 恒假，series_limit
      语义完全不介入，行为与旧口径逐字一致；
    - 接纳只在**新建剧行落成时**登记（``register_admitted``），
      计数只数真正新建的独有剧树；已有托管行（guid 命中 /
      软删复活 / 合并跟随）不计数、不接纳；
    - 拒绝登记 ``rejected_ids``（该剧全部线路的上游条目
      id）：被拒新剧的集在后续窗按 SeriesId 命中即跳过，
      绝不让它落到标题回落上误挂同名本地剧（O-08 红线）；
    - ``is_admitted`` 供集行豁免 ``_Limit``：接纳剧的季/集
      无论在哪个窗到达都不被行限额拦截——整棵树要么全进、
      要么（未接纳）一行不进，不许半截。
    """

    def __init__(self, limit: Optional[int]):
        self.limit = None if limit is None else max(0, int(limit))
        self.admitted_count = 0
        self.admitted_ids: Set[str] = set()
        self.admitted_local_ids: Set[int] = set()
        self.rejected_ids: Set[str] = set()

    @property
    def active(self) -> bool:
        return self.limit is not None

    def can_admit(self, upstream_ids: Set[str]) -> bool:
        if not self.active:
            return True
        if upstream_ids & self.admitted_ids:
            return True
        return self.admitted_count < self.limit

    def register_admitted(self, upstream_ids: Set[str],
                          local_id: Optional[int]) -> None:
        if not self.active:
            return
        if not (upstream_ids & self.admitted_ids):
            self.admitted_count += 1
        self.admitted_ids |= upstream_ids
        if local_id:
            self.admitted_local_ids.add(local_id)

    def register_rejected(self, upstream_ids: Set[str]) -> None:
        if self.active:
            self.rejected_ids |= upstream_ids

    def is_rejected(self, upstream_id: str) -> bool:
        return upstream_id in self.rejected_ids

    def is_admitted(self, upstream_id: str,
                    local_id: Optional[int]) -> bool:
        if not self.active:
            return False
        if upstream_id and upstream_id in self.admitted_ids:
            return True
        return bool(local_id) and local_id in self.admitted_local_ids


def _resolve_series_row(db: Session, source, item, series_ctx: Dict,
                        paired_series_local: Dict[str, int]):
    """给一集定位所属剧行（定位顺序见模块头二期段）。

    命中返回 MediaItem（剧行）；定不到返回 None（调用方计数
    no_series 跳过——挂错剧比不挂更糟，照 O-08 防错红线）。
    """
    sid = (getattr(item, "series_upstream_id", None) or "").strip()
    if sid:
        local_id = paired_series_local.get(sid)
        if local_id:
            row = db.query(em.MediaItem).filter(
                em.MediaItem.id == local_id,
                em.MediaItem.merged_into_id.is_(None)).first()
            if row is not None:
                return row
        mapping = (db.query(em.UpstreamItemMapping)
                   .filter(
                       em.UpstreamItemMapping.source_id == source.id,
                       em.UpstreamItemMapping.upstream_item_id == sid)
                   .order_by(em.UpstreamItemMapping.id)
                   .first())
        if mapping is not None:
            row = db.query(em.MediaItem).filter(
                em.MediaItem.id == mapping.local_item_id,
                em.MediaItem.item_type == "series",
                em.MediaItem.merged_into_id.is_(None)).first()
            if row is not None:
                return row
        row = _usable_row_by_guid(db, hosted_guid(source.id, sid))
        if (row is not None and row.item_type == "series"
                and row.merged_into_id is None):
            return row
    title_key = agg._normalize_title(
        getattr(item, "series_name", "") or "")
    if title_key:
        entry = series_ctx.get(title_key)
        if entry is not None:
            # 本窗对该剧名已有结论时只认结论，不再往下走标题池：
            # hopeless（无身份/无落点/同名多义）直接定不到——
            # 本窗剧没配上/没进成，其集绝不许靠标题回落挂进
            # 疑似同名异版（O-08 防错红线同款）。
            if entry.get("hopeless"):
                return None
            if entry.get("row") is not None:
                return entry["row"]
            if entry.get("local_id"):
                row = db.query(em.MediaItem).filter(
                    em.MediaItem.id == entry["local_id"],
                    em.MediaItem.merged_into_id.is_(None)).first()
                if row is not None:
                    return row
            return None
        # 本源托管剧按名唯一：同源同名两部（翻拍）时不定，跳过。
        hosted = (db.query(em.MediaItem)
                  .filter(em.MediaItem.item_type == "series",
                          em.MediaItem.upstream_source_id == source.id,
                          em.MediaItem.merged_into_id.is_(None))
                  .all())
        hits = [r for r in hosted
                if agg._normalize_title(r.name or "") == title_key]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            return None
        # 本地活剧按名唯一（scan_title 回落 name，与 pair 口径同）。
        locals_ = (db.query(em.MediaItem)
                   .filter(em.MediaItem.item_type == "series",
                           em.MediaItem.merged_into_id.is_(None),
                           em.MediaItem.is_hidden == False)  # noqa: E712
                   .all())
        hits = [r for r in locals_
                if agg._normalize_title(
                    (r.scan_title or r.name or "")) == title_key]
        if len(hits) == 1:
            return hits[0]
    return None


def _ensure_hosted_season(db: Session, source, series_row,
                          season_number: int, budget: _Limit,
                          stats: Dict[str, object]):
    """保证 (剧行, 季号) 的季行存在（幂等键与 scanner 同口径）。

    已有活季行（本地剧自带的优先）直接复用；没有才合成托管季行
    （打托管标记：扫描豁免、开关可见性、生命周期级联都靠它）。
    限额不足返回 None（调用方把该集计 limited）。
    """
    row = (db.query(em.MediaItem)
           .filter(em.MediaItem.item_type == "season",
                   em.MediaItem.series_id == series_row.id,
                   em.MediaItem.season_number == season_number,
                   em.MediaItem.merged_into_id.is_(None))
           .order_by(em.MediaItem.id)
           .first())
    if row is not None:
        return row
    guid = hosted_season_guid(source.id, series_row.id, season_number)
    row = _usable_row_by_guid(db, guid)
    if row is not None:
        if (row.series_id == series_row.id
                and row.season_number == season_number):
            return row
        # guid 锚（剧行 id + 季号）对不上 = 数据异常，绝不硬挂。
        logger.warning(
            "托管季行 guid 锚不一致 guid=%s row_series=%s want=%s",
            guid, row.series_id, series_row.id)
        return None
    if not budget.take():
        return None
    row = em.MediaItem(
        guid=guid, library_id=series_row.library_id,
        item_type="season", name=f"第 {season_number} 季",
        parent_id=series_row.id, series_id=series_row.id,
        season_number=season_number,
        upstream_source_id=source.id, probe_status="done")
    db.add(row)
    db.flush()
    stats["seasons_created"] += 1
    stats["imported"] += 1
    return row


def _ctx_note(series_ctx: Dict, title_key: str, entry: dict) -> None:
    """登记本窗剧名结论（单集标题定位用）：同名多义即标 hopeless。

    同一窗里两个不同作品共用一个归一剧名时，标题定位失去唯一性：
    后到的不同结论把先前的顶成 hopeless——宁可该集本窗定不到
    （计 no_series，下窗还有机会），不许靠标题猜挂（O-08 红线）。
    同一作品重复登记（同结论）不触发；hopeless 只在尚无真实
    结论时登记（真实结论优先，不被后来的跳过结论顶掉）。
    """
    if not title_key:
        return
    old = series_ctx.get(title_key)
    if old is None:
        series_ctx[title_key] = entry
    elif entry.get("hopeless"):
        return
    elif not old.get("hopeless") and old != entry:
        series_ctx[title_key] = {"hopeless": True}


def import_unpaired_series_tree(db: Session, source, items: Iterable,
                                pairs: Optional[List[dict]], *,
                                upstream_library_name: str,
                                upstream_library_id: Optional[str] = None,
                                upstream_collection_type: str = "",
                                limit: Optional[int] = None,
                                series_limit: Optional[int] = None,
                                series_budget: Optional[_SeriesBudget] = None
                                ) -> Dict[str, object]:
    """把一窗聚合作品里的未配对独有剧整棵进库＋已配对剧补集。

    ``pairs`` 是本窗 pair_aggregates 的完整配对列表（不只是 id
    集合：补集要靠 Series 对里的 local_item_id 定位本地剧行）。
    只 flush 不 commit（调用方事务）。统计键除一期同名键外加：
    series_imported / episodes_imported / seasons_created /
    no_identity（无 tmdb 剧跳过）/ no_series（集定不到所属剧）/
    no_position（集缺季或集号）/ libraries_created /
    series_limited（被剧数限额整树拒掉的新剧与其集）。

    ``series_limit``（内部试点口，默认 None = 不限）：只统计
    **新建的独有剧树**（未配对 Series 新建行；已有剧行不占
    额、已配对剧补集不占额），达到上限后不再接纳新剧，其
    季/集一行不建。接纳是整树语义：一部剧一旦接纳，它在
    本轮后续任何窗出现的季/集照常落库，不受 ``limit`` 行
    限额拦截（但仍计入 imported 统计——整轮行余量由 run
    层按 imported 递减，口径不变）；往轮已进库的剧本轮不
    算接纳，其新集仍受行限额（口径不变）。跨窗状态（计数
    与接纳/拒绝集合）由 ``series_budget`` 承载：run 层整轮
    建一个逐窗传进来，未传时按 ``series_limit`` 为本窗
    新建（单窗调用的旧形态）。``series_limit=None`` 且未传
    budget 时预算对象不介入，行为与旧口径逐字一致。
    """
    stats = _zero_series_stats()
    if not getattr(source, "import_enabled", True):
        stats["blocked"] = "switch_off"
        return stats
    if not uim.mapping_enabled():
        stats["blocked"] = "mapping_off"
        return stats
    pairs = list(pairs or ())
    paired_ids = {p["upstream_item_id"] for p in pairs}
    paired_series_local = {
        p["upstream_item_id"]: p["local_item_id"]
        for p in pairs if p.get("item_type") == "Series"}
    items = list(items or ())
    budget = _Limit(limit)
    if series_budget is None:
        series_budget = _SeriesBudget(series_limit)
    target_library = None
    target_resolved = False

    def _target():
        nonlocal target_library, target_resolved
        if not target_resolved:
            if upstream_library_id:
                target_library, created = _resolve_or_create_library(
                    db, source, upstream_library_id=upstream_library_id,
                    upstream_library_name=upstream_library_name,
                    upstream_collection_type=upstream_collection_type,
                    content_hint="Series")
                if created:
                    stats["libraries_created"] += 1
            else:
                target_library = _resolve_target_library(
                    db, upstream_library_name)
            target_resolved = True
        return target_library

    # 本窗剧名索引：title_key → {"row": 托管剧行} 或 {"local_id": 本地剧行}
    series_ctx: Dict[str, Dict[str, object]] = {}

    # ---- 第一遍：剧 ----
    for item in items:
        if (getattr(item, "item_type", "") or "") != "Series":
            continue
        source_lines = _source_lines(item, source.id)
        if not source_lines:
            continue
        title_key = agg._normalize_title(
            getattr(item, "name", "") or "")
        if any(line.upstream_item_id in paired_ids
               for line in source_lines):
            # 已配对：不建行，只登记本窗定位（供第二遍补集）。
            for line in source_lines:
                local_id = paired_series_local.get(line.upstream_item_id)
                if local_id:
                    _ctx_note(series_ctx, title_key, {"local_id": local_id})
            continue
        tmdb_id = (getattr(item, "tmdb_id", None) or "").strip()
        if not tmdb_id:
            # 无身份剧不进（拍板 7）：计数，集随后 no_series。
            stats["no_identity"] += 1
            _ctx_note(series_ctx, title_key, {"hopeless": True})
            continue
        stats["candidates"] += 1
        library = _target()
        if library is None:
            stats["no_library"] += 1
            _ctx_note(series_ctx, title_key, {"hopeless": True})
            continue
        line_ids = {line.upstream_item_id for line in source_lines}
        if not any(_usable_row_by_guid(
                db, hosted_guid(source.id, lid)) is not None
                for lid in line_ids) \
                and not series_budget.can_admit(line_ids):
            # 剧数限额已满且本剧确是新剧（任一线路都无既有托管
            # 行）：整树拒绝——登记拒绝集，其集随后按 SeriesId
            # 确定性跳过；本窗标题结论标 hopeless，集也不许靠
            # 标题回落挂错剧。已有剧行不占额，不走这道门。
            stats["series_limited"] += 1
            series_budget.register_rejected(line_ids)
            _ctx_note(series_ctx, title_key, {"hopeless": True})
            continue
        local_row = None
        for line in source_lines:
            guid = hosted_guid(source.id, line.upstream_item_id)
            row = _usable_row_by_guid(db, guid)
            if row is None:
                if not budget.take():
                    stats["limited"] += 1
                    break
                row = em.MediaItem(
                    guid=guid, library_id=library.id,
                    item_type="series", name=item.name,
                    sort_name=make_sort_name(item.name),
                    production_year=getattr(item, "production_year", None),
                    tmdb_id=tmdb_id,
                    imdb_id=(getattr(item, "imdb_id", None) or None),
                    upstream_source_id=source.id,
                    probe_status="done")
                db.add(row)
                db.flush()
                stats["imported"] += 1
                stats["series_imported"] += 1
                series_budget.register_admitted(line_ids, row.id)
                logger.info(
                    "上游独有剧进库 source_id=%s upstream_item_id=%s "
                    "local_item_id=%s library_id=%s name=%r",
                    source.id, line.upstream_item_id, row.id,
                    library.id, item.name)
            elif local_row is None:
                stats["existing"] += 1
            local_row = local_row or row
            uim.record_mapping(
                db, local_item_id=local_row.id, source_id=source.id,
                upstream_item_id=line.upstream_item_id,
                confidence=IMPORT_CONFIDENCE, evidence=IMPORT_EVIDENCE)
        if local_row is not None:
            _ctx_note(series_ctx, title_key, {"row": local_row})

    # ---- 第二遍：集（含补集） ----
    for item in items:
        if (getattr(item, "item_type", "") or "") != "Episode":
            continue
        source_lines = _source_lines(item, source.id)
        if not source_lines:
            continue
        if any(line.upstream_item_id in paired_ids
               for line in source_lines):
            continue  # 任一线路已配对 = 本地已有此集
        sid = (getattr(item, "series_upstream_id", None)
               or "").strip()
        if sid and series_budget.is_rejected(sid):
            # 所属新剧已被剧数限额整树拒绝：按 SeriesId 确定性
            # 跳过，绝不落到 _resolve_series_row 的标题回落上
            # 误挂同名异剧（O-08 防错红线），跨窗同理。
            stats["series_limited"] += 1
            continue
        season_number = getattr(item, "season_number", None)
        ep_numbers = {line.episode_index for line in source_lines}
        if (season_number is None or len(ep_numbers) != 1
                or None in ep_numbers):
            stats["no_position"] += 1
            continue
        episode_number = next(iter(ep_numbers))
        series_row = _resolve_series_row(
            db, source, item, series_ctx, paired_series_local)
        if series_row is None:
            stats["no_series"] += 1
            continue
        stats["candidates"] += 1
        # 接纳剧的季/集豁免行限额：整棵树要么全进、要么不进，
        # 不许行限额把已接纳的树拦成半截；未接纳路径（补集、
        # 往轮剧的新集）仍走原 budget，口径不变。
        bypass_limit = series_budget.is_admitted(sid, series_row.id)
        season_row = _ensure_hosted_season(
            db, source, series_row, season_number,
            _Limit(None) if bypass_limit else budget, stats)
        if season_row is None:
            stats["limited"] += 1
            continue
        ep_name = (getattr(item, "episode_name", None)
                   or getattr(item, "name", None) or "")
        local_row = None
        for line in source_lines:
            guid = hosted_guid(source.id, line.upstream_item_id)
            row = _usable_row_by_guid(db, guid)
            if row is None:
                if not bypass_limit and not budget.take():
                    stats["limited"] += 1
                    break
                row = em.MediaItem(
                    guid=guid, library_id=series_row.library_id,
                    item_type="episode", name=ep_name,
                    sort_name=make_sort_name(ep_name),
                    production_year=getattr(
                        item, "production_year", None),
                    parent_id=season_row.id, series_id=series_row.id,
                    season_number=season_number,
                    episode_number=episode_number,
                    tmdb_id=((getattr(item, "tmdb_id", None) or "")
                             .strip() or None),
                    imdb_id=(getattr(item, "imdb_id", None) or None),
                    upstream_source_id=source.id,
                    probe_status="done")
                db.add(row)
                db.flush()
                stats["imported"] += 1
                stats["episodes_imported"] += 1
                logger.info(
                    "上游独有集进库 source_id=%s upstream_item_id=%s "
                    "local_item_id=%s series_id=%s s=%s e=%s name=%r",
                    source.id, line.upstream_item_id, row.id,
                    series_row.id, season_number, episode_number,
                    ep_name)
            elif local_row is None:
                stats["existing"] += 1
            local_row = local_row or row
            uim.record_mapping(
                db, local_item_id=local_row.id, source_id=source.id,
                upstream_item_id=line.upstream_item_id,
                confidence=IMPORT_CONFIDENCE, evidence=IMPORT_EVIDENCE,
                episode_index=episode_number)
    return stats


# ---------------------------------------------------------------------------
# 二期：生命周期对账（上游下架跟随）
# ---------------------------------------------------------------------------

#: 连续不见 N 轮 → 隐藏（方案拍板 4）。
HOSTED_HIDE_AFTER_MISSING_ROUNDS = 2
#: 连续不见 M 轮（= 隐藏后再 2 轮）→ 软删。
HOSTED_DELETE_AFTER_MISSING_ROUNDS = 4


def _delete_rows(db: Session, rows: List) -> int:
    """托管行下架：软删优先（与 scanner 清理同一口径）。

    软删走 soft_delete.mark_soft_deleted（带合并子行孤儿守卫）；
    全局关软删时回落硬删（站点既定策略，不在本模块另立口径）。
    """
    ids = [r.id for r in rows if r is not None and getattr(r, "id", None)]
    if not ids:
        return 0
    if soft_delete.soft_delete_enabled():
        return soft_delete.mark_soft_deleted(db, ids)
    count = 0
    for row in rows:
        db.delete(row)
        count += 1
    db.flush()
    return count


def _hosted_subtree(db: Session, source, series_row) -> List:
    """某托管剧行名下的托管季/集行（级联隐藏/软删/恢复的对象）。"""
    seasons = (db.query(em.MediaItem)
               .filter(em.MediaItem.item_type == "season",
                       em.MediaItem.parent_id == series_row.id,
                       em.MediaItem.upstream_source_id == source.id,
                       em.MediaItem.merged_into_id.is_(None))
               .all())
    episodes = (db.query(em.MediaItem)
                .filter(em.MediaItem.item_type == "episode",
                        em.MediaItem.series_id == series_row.id,
                        em.MediaItem.upstream_source_id == source.id,
                        em.MediaItem.merged_into_id.is_(None))
                .all())
    return seasons + episodes


def reconcile_hosted_lifecycle(db: Session, source,
                               seen_upstream_ids: Iterable) -> Dict[str, object]:
    """一轮生命周期对账：本轮没见到的托管行按宽限隐藏/软删。

    调用前提（调用方保证）：本轮是整轮可信的完整对账——源的
    全部库都成功拉完（有失败/跳过库时整轮 seen 集合不完整，
    计数会把抖动误判成下架，调用方不许调）；``seen_upstream_ids``
    是本轮该源目录里出现的全部上游条目 id（配对与进库同源同集）。
    行的「见到」判定走它名下的映射（任一线路见到即见到）。
    开关 import_enabled 关时整表不动（关开关只是隐藏，绝不许
    对账在关着的时候把行删了——可逆性优先）。

    只 flush 不 commit（调用方事务）。返回统计：tracked / seen /
    missing / hidden / deleted / restored / seasons_pruned /
    blocked。
    """
    stats: Dict[str, object] = {
        "tracked": 0, "seen": 0, "missing": 0, "hidden": 0,
        "deleted": 0, "restored": 0, "seasons_pruned": 0,
        "blocked": None,
    }
    if not getattr(source, "import_enabled", True):
        stats["blocked"] = "switch_off"
        return stats
    seen = {str(x) for x in (seen_upstream_ids or ())}
    upstream_by_local: Dict[int, Set[str]] = {}
    for mapping in (db.query(em.UpstreamItemMapping)
                    .filter(em.UpstreamItemMapping.source_id == source.id)
                    .all()):
        upstream_by_local.setdefault(mapping.local_item_id, set()).add(
            str(mapping.upstream_item_id))
    states = {
        row.local_item_id: row
        for row in (db.query(em.UpstreamHostedLifecycle)
                    .filter(em.UpstreamHostedLifecycle.source_id
                            == source.id)
                    .all())
    }

    def _state(local_id: int):
        state = states.get(local_id)
        if state is None:
            state = em.UpstreamHostedLifecycle(
                source_id=source.id, local_item_id=local_id,
                missing_rounds=0, hidden_by_lifecycle=False)
            db.add(state)
            db.flush()
            states[local_id] = state
        return state

    now = datetime.now()
    rows = (db.query(em.MediaItem)
            .filter(em.MediaItem.upstream_source_id == source.id,
                    em.MediaItem.merged_into_id.is_(None),
                    em.MediaItem.item_type.in_(
                        ("movie", "series", "episode")))
            .all())
    for row in rows:
        stats["tracked"] += 1
        state = _state(row.id)
        if upstream_by_local.get(row.id, set()) & seen:
            stats["seen"] += 1
            state.last_seen_at = now
            if state.missing_rounds or state.hidden_by_lifecycle:
                state.missing_rounds = 0
                if state.hidden_by_lifecycle:
                    state.hidden_by_lifecycle = False
                    row.is_hidden = False
                    stats["restored"] += 1
                    logger.info(
                        "上游托管行重现恢复可见 source_id=%s "
                        "local_item_id=%s", source.id, row.id)
                    if row.item_type == "series":
                        # 级联恢复：只掀对账自己藏的——集行若有
                        # 自己的对账隐藏标记，归它自己的计数管。
                        for child in _hosted_subtree(db, source, row):
                            child_state = states.get(child.id)
                            if (child_state is not None
                                    and child_state.hidden_by_lifecycle):
                                continue
                            child.is_hidden = False
            continue
        stats["missing"] += 1
        state.missing_rounds = (state.missing_rounds or 0) + 1
        if state.missing_rounds >= HOSTED_DELETE_AFTER_MISSING_ROUNDS:
            targets = [row]
            if row.item_type == "series":
                targets += _hosted_subtree(db, source, row)
            stats["deleted"] += _delete_rows(db, targets)
            logger.info(
                "上游托管行连续 %s 轮不见，软删 source_id=%s "
                "local_item_id=%s type=%s",
                state.missing_rounds, source.id, row.id, row.item_type)
        elif state.missing_rounds >= HOSTED_HIDE_AFTER_MISSING_ROUNDS:
            if not row.is_hidden:
                row.is_hidden = True
                state.hidden_by_lifecycle = True
                stats["hidden"] += 1
                logger.info(
                    "上游托管行连续 %s 轮不见，隐藏 source_id=%s "
                    "local_item_id=%s type=%s",
                    state.missing_rounds, source.id, row.id,
                    row.item_type)
                if row.item_type == "series":
                    for child in _hosted_subtree(db, source, row):
                        child.is_hidden = True
            # 已隐藏且不是对账藏的（源开关/管理员）：不抢归属，
            # 恢复可见时不许替别人掀盖，这里什么都不做。
    db.flush()
    # 无子托管季行回收：集被逐个删光后季行只剩空壳（带托管标记
    # 扫描不碰），随对账软删；还有活子行的季行不动。
    season_rows = (db.query(em.MediaItem)
                   .filter(em.MediaItem.item_type == "season",
                           em.MediaItem.upstream_source_id == source.id,
                           em.MediaItem.merged_into_id.is_(None))
                   .all())
    for season in season_rows:
        child = (db.query(em.MediaItem.id)
                 .filter(em.MediaItem.parent_id == season.id)
                 .first())
        if child is None:
            stats["seasons_pruned"] += _delete_rows(db, [season])
    db.flush()
    return stats
