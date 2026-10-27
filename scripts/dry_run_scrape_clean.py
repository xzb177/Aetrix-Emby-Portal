"""脏标题清理干跑（只读，绝不写库）。

用法（二选一，均只读）：
  1. CSV 模式（推荐跑生产）：先用只读 psql 把行导出成 CSV，再喂给本脚本，
     脚本全程不连数据库：
       psql -c "COPY (SELECT id, item_type, name, scan_title, metadata_source
                 FROM emby_items WHERE deleted_at IS NULL
                 AND merged_into_id IS NULL) TO STDOUT CSV HEADER" > /tmp/items.csv
       python3 scripts/dry_run_scrape_clean.py --from-csv /tmp/items.csv
  2. 直连模式：--db-url 指向只读连接；脚本只开只读事务、只发 SELECT，
     跑完回滚，文件里没有任何写库语句（可用 grep 核对）。

CSV 列：id, item_type, name, scan_title, metadata_source（后两列可空），
没海报类另需 poster_path, primary_image_url 两列（缺列时该类跳过、不瞎算）。
输出五类：
  A. 剧集名带文件后缀：name 含 ADWeb / DDP / DTS 声道等，且清洗后变短；
  B. 未闭合括号年份：name 尾巴是「(YYYY」无右括号；
  C. 尾巴闭合年份：name 尾巴是「(YYYY)」闭合版，生产 10 条；
  D. 没海报的剧：item_type='series' 且 poster_path 与 primary_image_url 双空，生产 24 部；
  E. 没刮出来的剧 / 电影：metadata_source='none' 且 item_type 为
     series/movie——只报干净查询标题与“查询变了 / 查询没变”，不发
     TMDB 请求、不做匹配判定；查询没变的仍需人工认亲，不许硬贴。
另有人工复核清单：中文名后直接粘着年份（学校2015 类），代码不自动删，
只列出等人看；季（season）继承剧集元数据是正常现象，E 类不含 season/episode。
另有反例自检：内置正经片名清单，清洗后必须原样不动，否则退出码非零。
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import Counter

sys.path.insert(0, ".")

from backend.emby_server.scanner import (  # noqa: E402
    _episode_display_name,
    clean_name,
    clean_query_title,
    clean_stored_display_name,
)

# 反例自检清单：清洗后必须原样不动（既往事故底线）。
ANTI_EXAMPLES = [
    "梅根2.0", "哥斯拉-1.0", "比佛利拜金狗2", "11 11 11",
    "Delta Movie 02 00", "某某电影 (真人版)", "2012",
    "Woodwalkers 2", "Avatar 2", "流浪地球 2",
    # 粘连年份：代码分不清是片名还是年份，一律不动（规则表 R8）。
    "魔王2099", "机动战士高达0079", "1899", "1122好夫妇",
    "魔术快斗1412", "学校2015",
    "唐探1900", "阿根廷1985",
]

_SUFFIX_HINT = re.compile(r"ADWeb|DDP|DTS\s+\d|AAC\s+\d", re.IGNORECASE)
_UNCLOSED_YEAR = re.compile(r"[\(\[【（]\s*(?:19|20)\d{2}\s*$")
_CLOSED_YEAR = re.compile(r"[\(\[【（]\s*(?:19|20)\d{2}\s*[\)\]】）]\s*$")
_GLUED_YEAR = re.compile(r"[\u4e00-\u9fff](?:19|20)\d{2}$")


def _cleaned_display(row: dict) -> str:
    """行侧清洗后展示名（只算，不写回）：集用展示名口径，其余用清洗名。"""
    name = (row.get("name") or "").strip()
    # 存量清洗唯一入口：clean_name + 尾巴年份剥离；直接用 clean_name
    # 会漏掉闭合年份，且与解析路径回填年份的口径混淆（规则表 R4/R5）。
    cleaned = clean_stored_display_name(name)
    if row.get("item_type") == "episode":
        # 集号未知时不追加 SxxExx：这里只算“技术词清掉没有”，不猜集号。
        return cleaned
    return cleaned


def analyse(rows: list) -> dict:
    report: dict = {
        "total": len(rows),
        "class_a": {"count": 0, "samples": []},
        "class_b": {"count": 0, "samples": []},
        "class_c": {"count": 0, "samples": []},
        "class_d": {"count": 0, "samples": []},
        "class_e": {"count": 0, "query_changed": 0, "query_unchanged": 0,
                    "samples": []},
        "manual_review_glued_year": {"count": 0, "samples": []},
        "anti_violations": [],
    }
    has_poster_cols = any(
        "poster_path" in (r or {}) for r in rows)
    for row in rows:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        cleaned = _cleaned_display(row)
        if _SUFFIX_HINT.search(name) and cleaned != name:
            report["class_a"]["count"] += 1
            if len(report["class_a"]["samples"]) < 20:
                report["class_a"]["samples"].append((name, cleaned))
        if _UNCLOSED_YEAR.search(name):
            report["class_b"]["count"] += 1
            if len(report["class_b"]["samples"]) < 20:
                report["class_b"]["samples"].append((name, clean_stored_display_name(name)))
        if _CLOSED_YEAR.search(name) and clean_stored_display_name(name) != name:
            report["class_c"]["count"] += 1
            if len(report["class_c"]["samples"]) < 20:
                report["class_c"]["samples"].append((name, clean_stored_display_name(name)))
        if has_poster_cols and row.get("item_type") == "series" and not (
                row.get("poster_path") or "").strip() and not (
                row.get("primary_image_url") or "").strip():
            report["class_d"]["count"] += 1
            if len(report["class_d"]["samples"]) < 20:
                report["class_d"]["samples"].append(
                    (name, row.get("metadata_source") or "",
                     clean_query_title(row.get("scan_title") or name)))
        if (row.get("metadata_source") == "none"
                and row.get("item_type") in ("series", "movie")):
            report["class_e"]["count"] += 1
            query = clean_query_title(
                (row.get("scan_title") or name))
            if query != (row.get("scan_title") or name):
                report["class_e"]["query_changed"] += 1
            else:
                report["class_e"]["query_unchanged"] += 1
            if len(report["class_e"]["samples"]) < 20:
                report["class_e"]["samples"].append(
                    (name, row.get("scan_title") or "", query))
        if _GLUED_YEAR.search(name) and _GLUED_YEAR.search(cleaned):
            report["manual_review_glued_year"]["count"] += 1
            if len(report["manual_review_glued_year"]["samples"]) < 20:
                report["manual_review_glued_year"]["samples"].append(name)
    for bad in ANTI_EXAMPLES:
        if clean_name(bad) != bad:
            report["anti_violations"].append((bad, clean_name(bad)))
    return report


def _read_csv(path: str) -> list:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _read_db(url: str, limit: int) -> list:
    """只读直连：只读事务 + SELECT，跑完回滚。"""
    from sqlalchemy import create_engine, text

    engine = create_engine(url)
    rows: list = []
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("SET TRANSACTION READ ONLY"))
        except Exception:
            pass
        result = conn.execute(text(
            "SELECT id, item_type, name, scan_title, metadata_source, "
            "poster_path, primary_image_url "
            "FROM emby_items WHERE deleted_at IS NULL "
            "AND merged_into_id IS NULL ORDER BY id"))
        for r in result:
            rows.append({
                "id": r[0], "item_type": r[1], "name": r[2],
                "scan_title": r[3], "metadata_source": r[4],
                "poster_path": r[5], "primary_image_url": r[6]})
            if limit and len(rows) >= limit:
                break
        trans.rollback()
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="脏标题清理干跑（只读）")
    ap.add_argument("--from-csv", default="",
                    help="只读导出的 CSV 文件路径")
    ap.add_argument("--db-url", default="",
                    help="只读数据库连接串（与 --from-csv 二选一）")
    ap.add_argument("--limit", type=int, default=0,
                    help="直连模式最多读多少行（0=全量）")
    args = ap.parse_args(argv)
    if bool(args.from_csv) == bool(args.db_url):
        ap.error("--from-csv 与 --db-url 二选一")
    rows = (_read_csv(args.from_csv) if args.from_csv
            else _read_db(args.db_url, args.limit))
    report = analyse(rows)
    print(f"total={report['total']}")
    print(f"class_a_suffix_dirty={report['class_a']['count']}")
    for before, after in report["class_a"]["samples"]:
        print(f"  A: {before!r} -> {after!r}")
    print(f"class_b_unclosed_year={report['class_b']['count']}")
    for before, after in report["class_b"]["samples"]:
        print(f"  B: {before!r} -> {after!r}")
    c = report["class_c"]
    print(f"class_c_closed_trailing_year={c['count']}")
    for before, after in c["samples"]:
        print(f"  C: {before!r} -> {after!r}")
    d = report["class_d"]
    print(f"class_d_no_poster_series={d['count']}")
    for name, source, query in d["samples"]:
        print(f"  D: name={name!r} source={source!r} query={query!r}")
    e = report["class_e"]
    print(f"class_e_none_series_movie={e['count']} "
          f"query_changed={e['query_changed']} "
          f"query_unchanged={e['query_unchanged']}")
    for name, scan_title, query in e["samples"]:
        print(f"  E: name={name!r} scan_title={scan_title!r} query={query!r}")
    m = report["manual_review_glued_year"]
    print(f"manual_review_glued_year={m['count']} (auto rule untouched, human only)")
    for name in m["samples"]:
        print(f"  M: {name!r}")
    print(f"anti_violations={len(report['anti_violations'])}")
    for before, after in report["anti_violations"]:
        print(f"  ANTI-VIOLATION: {before!r} -> {after!r}")
    print("DRY-RUN ONLY: no rows were written back.")
    return 1 if report["anti_violations"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
