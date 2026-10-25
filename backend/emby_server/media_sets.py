"""媒体库合集域 L3（后端 only，前端不动）。

学的是 FMBY 合集的机制（合集与真实库并列成架、成员/排序/
可见性/封面四事独立演进），不抄它的表名、字段名、接口路径：
本域表名走 Aetrix 现有 ``emby_*`` 前缀（``emby_media_sets`` 等），
接口走 Aetrix 现有 ``/api/admin`` / ``/api/user`` 前缀。

开关（全域只在这里读 env，测试 patch 本模块常量即可）：
- ``COLLECTIONS_ENABLED``（默认 0）：总闸。关时本域全部接口
  惰性 404、现有浏览/库接口逐字不变、没有后台刷新工人可启动
  （刷新只由管理端显式调用触发，本来就没有定时任务）。
- ``COLLECTION_SHOW_UNBOUND``（默认 0）：D1 待定项的全局单一
  策略开关。0 = 合集内未绑定成员受案 A 门控、不露；
  1 = 全露。露/藏判定与 min_effective_members 门都在读列表时
  实时算，不存入成员表——将来 D1 反向只改本键默认值，
  不返工、不重算。

范围边界（越界即返工，写死在这里）：
- 不碰 libraries 表结构（库的 sort_order 是 L4 的地盘）；
- 不算封面图（artwork_mode 只存策略，读口不输出图 URL）；
- 不做库内列表投影键/游标（L5 的地盘）；
- 不做可见性治理流水线（L7 的地盘；本域只有合集自身的
  visibility_status 一个字段）。
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import and_, exists, func, or_, select

from backend.emby_server import models as em
from backend.emby_server.env_util import env_int
from backend.emby_server.identity_gate import identity_clause

logger = logging.getLogger(__name__)

#: 合集总闸（默认关）。启动时读一次，改需重启。
COLLECTIONS_ENABLED = env_int("COLLECTIONS_ENABLED", 0, 0, 1)
#: D1 开关（默认 0 = 未绑定成员受案 A 门控、不露）。全代码只在此处读它。
COLLECTION_SHOW_UNBOUND = env_int("COLLECTION_SHOW_UNBOUND", 0, 0, 1)

SOURCE_TYPES = ("manual", "rule", "path", "preset", "douban_doulist")
VISIBILITY_STATUSES = ("visible", "hidden")
ARTWORK_MODES = ("auto_collage", "none")
RULE_KINDS = ("person", "genre", "studio", "year", "decade", "rating",
              "library", "media_type")
TEXT_RULE_KINDS = ("person", "genre", "studio", "media_type")
INT_RULE_KINDS = ("year", "decade", "library")
FLOAT_RULE_KINDS = ("rating",)
ITEM_TYPES = ("movie", "series", "season", "episode")
#: 刷新时由刷新管理的成员来源（manual 行永不由刷新增删）。
COMPUTED_MEMBER_SOURCES = ("rule", "path", "preset")


def collections_enabled() -> bool:
    """总闸是否开启（读本模块常量；测试可直接 patch 常量）。"""
    return COLLECTIONS_ENABLED > 0


def show_unbound() -> bool:
    """D1 策略：合集内未绑定成员是否露出（读本模块常量）。"""
    return COLLECTION_SHOW_UNBOUND > 0


# ---------------------------------------------------------------- 验证


def validate_rule(kind, is_exclusion, value_text, value_int, value_float):
    """校验一条规则的值类型；错型直接 ValueError（路由转 400）。

    三型与规则种一一对应，不许用近似值冒充：
    text 型 person/genre/studio/media_type，int 型 year/decade/library，
    float 型 rating。person 的 text 是人物 TMDB id 或姓名（二者任一
    命中即算该人，见 _person_predicate）。
    """
    if kind not in RULE_KINDS:
        raise ValueError(f"未知的规则类型: {kind!r}")
    if kind in TEXT_RULE_KINDS:
        if not (value_text or "").strip():
            raise ValueError(f"{kind} 规则必须给 text 值")
        if kind == "media_type" and value_text.strip() not in ITEM_TYPES:
            raise ValueError(f"media_type 规则值须为 {ITEM_TYPES}")
    elif kind in INT_RULE_KINDS:
        if value_int is None:
            raise ValueError(f"{kind} 规则必须给 int 值")
        if kind == "decade" and value_int % 10 != 0:
            raise ValueError("decade 规则值须为 10 的倍数（如 1990）")
    else:  # rating
        if value_float is None:
            raise ValueError("rating 规则必须给 float 值")
    return {
        "rule_kind": kind,
        "is_exclusion": bool(is_exclusion),
        "value_text": (value_text.strip() if value_text else None),
        "value_int": value_int,
        "value_float": value_float,
    }


def validate_template_rules(rules_json):
    """校验模板规则集：每条同规则行校验，且不许夹带条目 id 清单。

    模板只存规则——出现 item_id / item_ids 键即拒绝，防止模板
    被写成 hardcode 片单的壳。
    """
    try:
        rules = json.loads(rules_json) if isinstance(rules_json, str) else rules_json
    except (TypeError, ValueError) as exc:
        raise ValueError("模板规则集不是合法 JSON") from exc
    if not isinstance(rules, list):
        raise ValueError("模板规则集须为规则 dict 列表")
    out = []
    for raw in rules:
        if not isinstance(raw, dict):
            raise ValueError("模板规则须为 dict")
        if "item_id" in raw or "item_ids" in raw:
            raise ValueError("模板只存规则，不许夹带条目 id 清单")
        out.append(validate_rule(
            raw.get("rule_kind"), raw.get("is_exclusion", False),
            raw.get("value_text"), raw.get("value_int"),
            raw.get("value_float")))
    return out


# ---------------------------------------------------------------- 规则谓词


def _like_escape(value: str) -> str:
    return (value.replace("\\", "\\\\").replace("%", "\\%")
            .replace("_", "\\_"))


def _csv_contains(column, value: str):
    """逗号分隔列（如 genres/studios）含某值：去空格后按逗号定界匹配。

    不用近似字段冒充：genres/studios 本来就是逗号分隔文本列，
    定界匹配避免 "动作" 命中 "动作片" 这类子串误伤。
    """
    padded = ("," + func.replace(column, " ", "") + ",")
    return padded.like(f"%,{_like_escape(value.strip())},%", escape="\\")


def _person_predicate(value_text: str):
    """人物规则谓词：条目在 emby_people 里有同人出演行。

    关联就是 EmbyPerson.item_id → emby_items.id（models.py 实查，
    无独立关联表）。text 命中 person_tmdb_id 或 name 任一即算该人：
    emby_people 没有人物类型列（演员/导演不区分），本规则不区分
    人物身份——这是现有模型的真实边界，不编造类型字段。
    """
    text = value_text.strip()
    sub = select(em.EmbyPerson.id).where(
        and_(
            em.EmbyPerson.item_id == em.MediaItem.id,
            or_(em.EmbyPerson.person_tmdb_id == text,
                em.EmbyPerson.name == text),
        )
    )
    return exists(sub)


def _rule_predicate(rule):
    kind = rule.rule_kind
    if kind == "person":
        return _person_predicate(rule.value_text or "")
    if kind == "genre":
        return _csv_contains(em.MediaItem.genres, rule.value_text or "")
    if kind == "studio":
        return _csv_contains(em.MediaItem.studios, rule.value_text or "")
    if kind == "year":
        return em.MediaItem.production_year == rule.value_int
    if kind == "decade":
        return and_(em.MediaItem.production_year >= rule.value_int,
                    em.MediaItem.production_year <= rule.value_int + 9)
    if kind == "rating":
        return em.MediaItem.community_rating >= rule.value_float
    if kind == "library":
        return em.MediaItem.library_id == rule.value_int
    if kind == "media_type":
        return em.MediaItem.item_type == (rule.value_text or "").strip()
    raise ValueError(f"未知的规则类型: {kind!r}")


def _bound_person_item_ids(db, bound_person_id):
    """绑定人物的条目 id 集：按绑定行的人物身份展开。

    bound_person_id 指向 emby_people 的一行（某条目下某人的一次
    出演记录），人物身份取该行的 person_tmdb_id（优先；为空时按
    name）。行已删则绑定悬空，返回空集、不报错。
    """
    row = db.query(em.EmbyPerson).filter(
        em.EmbyPerson.id == bound_person_id).first()
    if row is None:
        return set()
    ident = (row.person_tmdb_id or "").strip()
    if ident:
        cond = em.EmbyPerson.person_tmdb_id == ident
    else:
        cond = em.EmbyPerson.name == row.name
    return {r[0] for r in db.query(em.EmbyPerson.item_id).filter(cond).all()}


def candidate_item_ids(db, media_set) -> set:
    """算合集的候选成员条目 id 集（规则/path/preset 刷新用）。

    组合语义（写死，不留歧义）：
    - 同种包含规则取并集（genre=A 或 genre=B），不同种包含规则
      取交集（genre=A 且 year=1994）；反选规则逐条取反后取交集；
    - 无包含规则的 rule/preset 集候选为空（绝不默认全库——
      空规则集全量入集是灾难性默认）；
    - path 集候选 = file_path 落在 path_prefix 下的条目；
    - bound_person_id 若有值，把同人条目并入候选（加分项）；
    - 软删除行由全局 ORM 钩子自动排除；已合并副本
      （merged_into_id 非空）排除，合集只收主记录。
    """
    base = db.query(em.MediaItem).filter(
        em.MediaItem.merged_into_id.is_(None))
    if media_set.source_type == "path":
        prefix = (media_set.path_prefix or "").strip()
        if not prefix:
            return set()
        ids = {r[0] for r in base.filter(
            em.MediaItem.file_path.like(
                f"{_like_escape(prefix)}%", escape="\\")
        ).with_entities(em.MediaItem.id).all()}
    elif media_set.source_type in ("rule", "preset"):
        rules = db.query(em.EmbyMediaSetRule).filter(
            em.EmbyMediaSetRule.set_id == media_set.id).all()
        inclusions = [r for r in rules if not r.is_exclusion]
        exclusions = [r for r in rules if r.is_exclusion]
        if not inclusions:
            ids = set()
        else:
            by_kind: dict[str, list] = {}
            for rule in inclusions:
                by_kind.setdefault(rule.rule_kind, []).append(rule)
            query = base
            for kind_rules in by_kind.values():
                query = query.filter(
                    or_(*[_rule_predicate(r) for r in kind_rules]))
            for rule in exclusions:
                query = query.filter(~_rule_predicate(rule))
            ids = {r[0] for r in query.with_entities(em.MediaItem.id).all()}
    else:
        return set()
    if media_set.bound_person_id:
        ids |= _bound_person_item_ids(db, media_set.bound_person_id)
    return ids


# ---------------------------------------------------------------- 刷新


def bump_requested(media_set):
    """成员可能变了：顶 requested_revision（不提交，调用方统一提交）。

    顶号的改动只有：规则增删、path_prefix 改动、bound_person_id
    改动、手工成员增删。排序/可见性/名字/封面策略/min 门改动
    不顶号（它们不影响成员构成）。
    """
    media_set.requested_revision = (media_set.requested_revision or 0) + 1


def refresh_media_set(db, media_set) -> dict:
    """刷新合集成员（管理端显式调用，无后台工人、无定时任务）。

    增量语义（写实，不谎称）：
    - 写路径是增量：只对“候选集 vs 现有计算行”做差集插删，
      手工行（member_source=manual）永不由刷新增删；
    - 候选集查询本身仍需按规则/路径扫一遍候选条目（单合集一次
      查询），不是全库所有合集重算，也不是定时全量重算；
    - manual 集刷新是 no-op：成员即手工事实，只把 completed
      追平 requested（结算修订号），不碰成员行；
    - douban_doulist 集恒返回 not_implemented：接口位已留、
      导入未实现，绝不假装导入成功，completed 不追平。
    """
    if media_set.source_type == "manual":
        media_set.completed_revision = media_set.requested_revision
        media_set.refresh_status = "done"
        db.commit()
        return {"status": "done", "added": 0, "removed": 0}
    if media_set.source_type == "douban_doulist":
        media_set.refresh_status = "not_implemented"
        db.commit()
        return {"status": "not_implemented", "added": 0, "removed": 0}
    candidates = candidate_item_ids(db, media_set)
    existing = db.query(em.EmbyMediaSetMember).filter(
        em.EmbyMediaSetMember.set_id == media_set.id).all()
    computed = [m for m in existing
                if m.member_source in COMPUTED_MEMBER_SOURCES]
    computed_ids = {m.item_id for m in computed}
    added = 0
    for item_id in sorted(candidates - computed_ids):
        db.add(em.EmbyMediaSetMember(
            set_id=media_set.id, item_id=item_id, order_index=0,
            member_source=media_set.source_type))
        added += 1
    removed = 0
    for member in computed:
        if member.item_id not in candidates:
            db.delete(member)
            removed += 1
    media_set.completed_revision = media_set.requested_revision
    media_set.refresh_status = "done"
    db.commit()
    return {"status": "done", "added": added, "removed": removed}


# ---------------------------------------------------------------- 读时门控与排序


def visible_items_query(db, set_id):
    """合集成员的读查询（露/藏判定与有效计数都走它，不另起口径）。

    - 已合并副本排除；软删除行由全局钩子排除；
    - D1 开关关（默认）时挂案 A 身份条件（复用 identity_gate
      的唯一判据，不私自判 tmdb_id）：未绑定成员不露；
      开关开时全露。
    """
    query = db.query(em.MediaItem).join(
        em.EmbyMediaSetMember,
        em.EmbyMediaSetMember.item_id == em.MediaItem.id,
    ).filter(
        em.EmbyMediaSetMember.set_id == set_id,
        em.MediaItem.merged_into_id.is_(None),
    )
    if not show_unbound():
        query = query.filter(identity_clause())
    return query


def effective_count(db, media_set) -> int:
    """有效成员数：读时实时算（不存成员表、不物化）。"""
    return visible_items_query(db, media_set.id).count()


def shelf_eligible(db, media_set) -> bool:
    """是否上架：自身可见 + 有效成员数达到 min 门（读时算）。"""
    if media_set.visibility_status != "visible":
        return False
    return effective_count(db, media_set) >= (media_set.min_effective_members or 0)


def _release_key(pair):
    member, item = pair
    year = item.production_year
    premiere = item.premiere_date
    return (year is None, year if year is not None else 0,
            premiere is None, premiere, item.id)


def order_members(pairs, track: str):
    """双轨排序（所有合集标配）：

    - 上映轨（release）：按 production_year / premiere_date 升序
      （空值排最后），同值按 id 兜底；
    - 观影轨（watch）：按成员 order_index 升序；全员 order_index
      均为 0（未编排）时默认同上映轨；部分编排时未编排行
      （order_index=0）按上映序排在已编排行之后——已编排的
      顺序意图优先，未编排的不插队到已编排行前面。
    """
    if track == "watch" and any(m.order_index != 0 for m, _ in pairs):
        arranged = [p for p in pairs if p[0].order_index != 0]
        unarranged = [p for p in pairs if p[0].order_index == 0]
        arranged.sort(key=lambda p: (p[0].order_index, _release_key(p)))
        unarranged.sort(key=_release_key)
        return arranged + unarranged
    ordered = sorted(pairs, key=_release_key)
    return ordered


def list_shelf(db) -> dict:
    """架列表：真实库（现有顺序：启用库按 id 升序，未设序的库
    顺序不动）+ 合集区（合集按 sort_order、id 出架）。

    合集区只收 shelf_eligible 的合集：自身 hidden 的、有效成员
    未达 min 门的都不进架。封面只出 artwork_mode 策略，不出图 URL。
    """
    libraries = db.query(em.Library).filter(
        em.Library.is_enabled == True).order_by(em.Library.id).all()  # noqa: E712
    lib_rows = [{"kind": "library", "id": lib.id, "name": lib.name,
                 "collection_type": lib.collection_type} for lib in libraries]
    sets = db.query(em.EmbyMediaSet).order_by(
        em.EmbyMediaSet.sort_order, em.EmbyMediaSet.id).all()
    set_rows = []
    for media_set in sets:
        if not shelf_eligible(db, media_set):
            continue
        set_rows.append({
            "kind": "collection", "id": media_set.id, "name": media_set.name,
            "sort_order": media_set.sort_order,
            "artwork_mode": media_set.artwork_mode,
            "member_count": effective_count(db, media_set),
            "min_effective_members": media_set.min_effective_members,
        })
    return {"libraries": lib_rows, "collections": set_rows}


# ---------------------------------------------------------------- 序列化


def set_to_dict(media_set, db=None) -> dict:
    out = {
        "id": media_set.id, "name": media_set.name,
        "source_type": media_set.source_type,
        "visibility_status": media_set.visibility_status,
        "sort_order": media_set.sort_order,
        "min_effective_members": media_set.min_effective_members,
        "artwork_mode": media_set.artwork_mode,
        "path_prefix": media_set.path_prefix,
        "bound_person_id": media_set.bound_person_id,
        "requested_revision": media_set.requested_revision,
        "completed_revision": media_set.completed_revision,
        "refresh_status": media_set.refresh_status,
    }
    if db is not None:
        out["member_count"] = db.query(em.EmbyMediaSetMember).filter(
            em.EmbyMediaSetMember.set_id == media_set.id).count()
        out["effective_count"] = effective_count(db, media_set)
        out["shelf_eligible"] = shelf_eligible(db, media_set)
    return out


def member_to_dict(member) -> dict:
    return {"id": member.id, "set_id": member.set_id,
            "item_id": member.item_id, "order_index": member.order_index,
            "member_source": member.member_source}


def rule_to_dict(rule) -> dict:
    return {"id": rule.id, "set_id": rule.set_id,
            "rule_kind": rule.rule_kind,
            "is_exclusion": bool(rule.is_exclusion),
            "value_text": rule.value_text, "value_int": rule.value_int,
            "value_float": rule.value_float}


def template_to_dict(template) -> dict:
    try:
        rules = json.loads(template.rules_json or "[]")
    except ValueError:
        rules = []
    return {"id": template.id, "name": template.name, "rules": rules}
