"""人物刮削（FMBY 高级刮削候选 4，第二批）。

人物详情 / 图片从已有元数据源补齐进 ``emby_people``，口径钉死：

- 只填空缺，永不覆盖：已有非空值的字段一律不动；空值定义为
  None / 空串 / 纯空白串。新增数据永远不把旧数据换掉。
- 可填字段 = ``emby_people`` 现有可补列：``role``（饰演角色）、
  ``image``（头像远程 URL）、``person_tmdb_id``（TMDB 人物 id）。
  ``name`` / ``item_id`` / ``sort_order`` 是键与顺序，永不改写。
  注意：``emby_people`` 现无简介 / 生日等详情列，本模块不擅自加列
  ——超出三列的详情字段须另立迁移项，先有列再谈补齐。
- 证据口径与识别 / 补资料两段式复用：人物锚点 = ``person_tmdb_id``
  或 ``name``；抓回的记录锚点对不上（双方 tmdb id 俱在且不同，
  或名字对不上）时整条跳过，不把 A 的详情填到 B 身上。抓回记录自带的
  ``sources`` 列表原样记进结果的 evidence，不编来源名。
- 失败隔离：批量里单人抓取失败（fetcher 抛异常 / 返回空）只记
  该人失败 / 跳过，继续处理下一个人，绝不拖垮整批。
- 不写库：本模块只产“填什么”的纯数据结果；对象适配
  ``apply_filled_to_person`` 也只改内存属性，session / 提交归
  调用方（后续接线方）的现行写库路径。本模块不 import 任何 DB 层。
- 总开关 ``PEOPLE_SCRAPE_ENABLED``（默认关）。关时
  ``scrape_people`` 不调用 fetcher、不产任何填补，行为与今天一致。
"""

from __future__ import annotations

import logging

from backend.emby_server.env_util import env_int

logger = logging.getLogger(__name__)

#: 人物刮削总开关（默认关）。关时整批直接返回空结果，不问任何源。
PEOPLE_SCRAPE_ENABLED = env_int("PEOPLE_SCRAPE_ENABLED", 0, 0, 1)

#: 可填字段：与 emby_people 现有可补列一一对应，键与顺序不在内。
FILLABLE_FIELDS = ("role", "image", "person_tmdb_id")


def people_scrape_enabled() -> bool:
    """人物刮削是否开启（全仓读开关的唯一入口；测试 patch 本模块常量）。"""
    return PEOPLE_SCRAPE_ENABLED > 0


def _is_empty(value) -> bool:
    """空值判定：None / 空串 / 纯空白串；数字 0 不算空（不在本表字段里）。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def _clean(value):
    """清洗抓回值：字符串去空白并转 str；空值回 None（不编数字）。"""
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    return str(value)


def _anchors_match(record: dict, fetched: dict) -> bool:
    """锚点校验：tmdb id 对上，或名字对上（去空白、不区分大小写）。

    双方 tmdb id 俱在且不同时直接判不匹配（不看名字）；
    记录无 tmdb id 时只看名字；抓回既无 id 又无名字时判不匹配。
    """
    rec_id = _clean(record.get("person_tmdb_id"))
    fet_id = _clean(fetched.get("person_tmdb_id"))
    if rec_id and fet_id and rec_id != fet_id:
        # 从严：双方 id 俱在且不同时直接判不匹配（同名不同人是
        # 人物刮削的经典撞名坑，名字对上也不能把 id 不同的详情混填）。
        return False
    if rec_id and fet_id:
        return True
    rec_name = _clean(record.get("name"))
    fet_name = _clean(fetched.get("name"))
    if rec_name and fet_name and rec_name.lower() == fet_name.lower():
        return True
    return False


def fill_person(record: dict, fetched: dict) -> dict:
    """单人只填空缺（纯函数，不改输入 dict）。

    返回 ``{"record", "filled", "skipped"}``：record 是填补后的副本，
    filled 是实际填上的字段名，skipped 是抓回有值但记录已有值而
    未覆盖的字段名（有据可查“没覆盖”）。锚点不匹配时返回
    ``{"record", "filled": [], "skipped": [], "reason": "anchor_mismatch"}``。
    """
    record = dict(record or {})
    fetched = fetched or {}
    if not _anchors_match(record, fetched):
        return {"record": record, "filled": [], "skipped": [],
                "reason": "anchor_mismatch"}
    filled, skipped = [], []
    for field_name in FILLABLE_FIELDS:
        new_value = _clean(fetched.get(field_name))
        if new_value is None:
            continue
        if _is_empty(record.get(field_name)):
            record[field_name] = new_value
            filled.append(field_name)
        else:
            skipped.append(field_name)
    return {"record": record, "filled": filled, "skipped": skipped}


def apply_filled_to_person(person_obj, filled_record: dict) -> list:
    """把 ``fill_person`` 的结果写回一个人物对象（仅内存属性）。

    只写 FILLABLE_FIELDS 里、在对象上当前为空的字段；session 提交
    归调用方。本函数不 import DB、不提交、不删改任何已有值。
    返回实际改动的字段名。
    """
    changed = []
    for field_name in FILLABLE_FIELDS:
        if field_name not in filled_record:
            continue
        current = getattr(person_obj, field_name, None)
        if _is_empty(current):
            setattr(person_obj, field_name, filled_record[field_name])
            changed.append(field_name)
    return changed


def scrape_people(records, fetcher):
    """批量人物刮削（失败隔离，永不因单人失败中断整批）。

    ``records`` 是人物 dict 列表（至少含 ``name``；可含 ``role`` /
    ``image`` / ``person_tmdb_id``）；``fetcher(record) -> dict | None``
    是调用方注入的已有元数据源抓取（TMDB / 豆瓣等由接线方定，
    本模块不内置任何源客户端、不写死源名）。

    返回 ``{"processed", "filled", "skipped", "failed"}``：filled 每项
    含 ``record`` / ``filled`` / ``evidence``（抓回 sources 原样）；
    skipped 含 ``reason``；failed 含单人异常文本。开关关时 fetcher
    一次不调用，返回全零结果。
    """
    result = {"processed": 0, "filled": [], "skipped": [], "failed": []}
    if not people_scrape_enabled():
        return result
    for record in records or []:
        record = dict(record or {})
        result["processed"] += 1
        try:
            fetched = fetcher(record)
        except Exception as exc:  # 单人失败隔离：记失败，继续下一个人
            logger.warning("人物刮削单人失败（%s）: %s",
                           record.get("name"), exc)
            result["failed"].append(
                {"record": record, "error": str(exc)[:500]})
            continue
        if not isinstance(fetched, dict) or not fetched:
            result["skipped"].append({"record": record, "reason": "empty_fetch"})
            continue
        outcome = fill_person(record, fetched)
        if outcome.get("reason") == "anchor_mismatch":
            result["skipped"].append(
                {"record": record, "reason": "anchor_mismatch"})
            continue
        sources = fetched.get("sources")
        evidence = {"sources": list(sources) if isinstance(sources, (list, tuple)) else []}
        if outcome["filled"]:
            result["filled"].append({
                "record": outcome["record"],
                "filled": outcome["filled"],
                "skipped_fields": outcome["skipped"],
                "evidence": evidence,
            })
        else:
            result["skipped"].append({
                "record": outcome["record"],
                "reason": "nothing_to_fill",
                "skipped_fields": outcome["skipped"],
                "evidence": evidence,
            })
    return result
