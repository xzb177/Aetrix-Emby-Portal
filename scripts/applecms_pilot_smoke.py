#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""苹果 CMS 试点冒烟（设计稿 §6，实播除外）。

用法：python scripts/applecms_pilot_smoke.py [--base-url URL]
或环境变量 APPLECMS_PILOT_BASE_URL。无 URL 时打印
``no pilot configured`` 后正常退出（不是报错）——试点站地址
只能由操作方显式给出，本脚本绝不内置、预置任何资源站地址。

跑的项（设计稿 §6 的 1–4、6 项；§6.6 在本脚本按「分页循环」
实现——设计稿原文不在本工作区，见末尾注记）：
  1. list 取回：ac=list 返回 code==1 且 list 为数组；
  2. detail 取回：取 list 首条 vod_id 调 detail，code==1 且
     vod_id 一致；
  3. 分隔符解析：detail 的 vod_play_from/vod_play_url 经
     parse_play_groups 解析出 ≥1 组、每组 ≥1 集；
  4. 分类对应：以首条的 type_id 为 t 再请求 list，返回条目
     type_id 均与该 t 一致；
  6. 分页循环：iter_pages 前两页 vod_id 不重复、页码递进。

逐项打印 PASS / FAIL / SKIP（无数据可验时 SKIP，不算通过
也不算失败）；任一 FAIL 则退出码 1。

明确不做、不宣称：实播（§6 第 5 项）是试点实测项，本批不做——
本脚本只取目录与详情、只解析播放字段，绝不请求播放地址、
绝不下发播放，末尾会明文打印「实播未验证」。
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.emby_server import applecms_fetcher as acf


def _report(results):
    for name, outcome, note in results:
        print(f"[{outcome}] {name}" + (f"：{note}" if note else ""))
    failed = sum(1 for _, outcome, _ in results if outcome == "FAIL")
    skipped = sum(1 for _, outcome, _ in results if outcome == "SKIP")
    print(f"合计：{len(results)} 项，"
          f"{len(results) - failed - skipped} 通过，"
          f"{failed} 失败，{skipped} 跳过")
    print("实播未验证（试点实测项，本批不做）")
    return 1 if failed else 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="苹果 CMS 试点冒烟（不含实播）")
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args(argv)
    base_url = (args.base_url or os.getenv("APPLECMS_PILOT_BASE_URL") or "").strip()
    if not base_url:
        print("no pilot configured")
        return 0
    results = []
    try:
        page1 = acf.fetch_page(base_url, ac="list", pg=1)
    except acf.AppleCMSError as exc:
        results.append(("1 list 取回", "FAIL", f"kind={exc.kind}"))
        return _report(results)
    items = page1.get("list") or []
    if isinstance(items, list):
        results.append(("1 list 取回", "PASS", f"{len(items)} 条"))
    else:
        results.append(("1 list 取回", "FAIL", "list 不是数组"))
        return _report(results)
    if not items:
        results.append(("2 detail 取回", "SKIP", "list 为空"))
        results.append(("3 分隔符解析", "SKIP", "无详情可验"))
        results.append(("4 分类对应", "SKIP", "无条目可验"))
    else:
        first = items[0] if isinstance(items[0], dict) else {}
        vod_id = first.get("vod_id")
        try:
            detail = acf.fetch_detail(base_url, vod_id)
        except acf.AppleCMSError as exc:
            results.append(("2 detail 取回", "FAIL", f"kind={exc.kind}"))
            detail = None
        if detail is not None and str(detail.get("vod_id")) == str(vod_id):
            results.append(("2 detail 取回", "PASS", f"vod_id={vod_id}"))
        elif detail is not None:
            results.append(("2 detail 取回", "FAIL", "vod_id 不一致"))
        if detail:
            groups = acf.parse_play_groups(detail.get("vod_play_from"),
                                           detail.get("vod_play_url"))
            if groups and all(g["episodes"] for g in groups):
                total_eps = sum(len(g["episodes"]) for g in groups)
                results.append(("3 分隔符解析", "PASS",
                                f"{len(groups)} 组、{total_eps} 集"))
            elif groups:
                results.append(("3 分隔符解析", "FAIL", "存在空组"))
            else:
                results.append(("3 分隔符解析", "SKIP", "该条目无播放字段"))
        type_id = first.get("type_id")
        if type_id is None:
            results.append(("4 分类对应", "SKIP", "条目无 type_id"))
        else:
            try:
                typed = acf.fetch_page(base_url, ac="list", pg=1, t=type_id)
            except acf.AppleCMSError as exc:
                results.append(("4 分类对应", "FAIL", f"kind={exc.kind}"))
                typed = None
            if typed is not None:
                typed_items = typed.get("list") or []
                if typed_items and all(
                        isinstance(it, dict)
                        and str(it.get("type_id")) == str(type_id)
                        for it in typed_items):
                    results.append(("4 分类对应", "PASS",
                                    f"t={type_id}，{len(typed_items)} 条一致"))
                elif not typed_items:
                    results.append(("4 分类对应", "SKIP", "该分类下无条目"))
                else:
                    results.append(("4 分类对应", "FAIL", "存在 type_id 不一致条目"))
    try:
        page1_ids = [it.get("vod_id") for it in items
                     if isinstance(it, dict)
                     and it.get("vod_id") is not None]
        page2 = acf.fetch_page(base_url, ac="list", pg=2)
        page2_ids = [it.get("vod_id") for it in (page2.get("list") or [])
                     if isinstance(it, dict)
                     and it.get("vod_id") is not None]
        if not page2_ids:
            results.append(("6 分页循环", "SKIP", "第 2 页为空"))
        elif set(page1_ids) & set(page2_ids):
            results.append(("6 分页循环", "FAIL", "第 1、2 页 vod_id 重复"))
        else:
            results.append(("6 分页循环", "PASS",
                            f"第 1 页 {len(page1_ids)} 条、"
                            f"第 2 页 {len(page2_ids)} 条、无重复"))
    except acf.AppleCMSError as exc:
        results.append(("6 分页循环", "FAIL", f"kind={exc.kind}"))
    return _report(results)


if __name__ == "__main__":
    raise SystemExit(main())
