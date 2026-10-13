#!/usr/bin/env python3
"""头像菜单已删条目防复发契约检查：用户铁律——删过的东西不许以任何形式加回。

事故史（都在 user_frontend/src/components/AppHeader.vue 头像下拉菜单）：
  - #769/#780 两次删除头像菜单里的「消息中心」「外观」行（#776 曾误恢复过一次）；
  - r27/r28 完美主义迭代轮以「窄屏无外观/消息入口」为由两次恢复；
  - 2026-10-13 用户手机截图实锤第三次复发：菜单底部出现「消息中心」
    「外观：白日（点击切换）」「退出登录」三行。

复发机制：迭代轮的审查者每次都把「窄屏下顶栏按钮被 .wide-only 隐藏 ⇒
菜单必须补入口」当缺陷修回去，而注释留档拦不住下一轮。
只有 CI 里的一条硬断言能拦住。

检查两条规则（对象是源码，不是构建产物）：
  1. AppHeader.vue 里不得出现 `dropdown-narrow-only`（它是这两次复活行的
     唯一专有标记：模板类 + CSS 规则 + 媒体查询规则都是为它服务的）；
  2. 头像下拉菜单区块（user-dropdown）里不得出现文本行「消息中心」
     与「外观：」主题行——顶栏的外观按钮（theme-btn，点一下直接切换）与
     消息铃铛是用户认可的入口，不在此限。

用法：python3 scripts/check_avatar_menu_guards.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NoReturn

ROOT = Path(__file__).resolve().parent.parent
HEADER = ROOT / "user_frontend" / "src" / "components" / "AppHeader.vue"

TEMPLATE_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
CSS_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)


def fail(msg: str) -> NoReturn:
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def main() -> None:
    src = HEADER.read_text(encoding="utf-8")

    # 规则 1：专有标记零容忍（注释里也不许留，防止"注释先行、代码跟上"）
    hits = [m.start() for m in re.finditer(r"dropdown-narrow-only", src)]
    if hits:
        fail(
            "AppHeader.vue 仍含 `dropdown-narrow-only`（%d 处，首处行号 %d）——"
            "这是已删两次的头像菜单「消息中心/外观」行的复活标记，"
            "用户铁律：删过的东西不许加回。" % (len(hits), src[: hits[0]].count("\n") + 1)
        )

    # 规则 2：在头像下拉菜单区块内查文本行。先剥掉所有注释，避免注释里提及
    # 「消息中心」被误判（历史注释会提到这些词）。
    clean = TEMPLATE_COMMENT_RE.sub("", src)
    clean = CSS_COMMENT_RE.sub("", clean)
    m = re.search(r'<div v-if="userMenuOpen" class="user-dropdown"', clean)
    if not m:
        fail("找不到头像下拉菜单区块（user-dropdown），组件结构可能被重写，契约需同步更新")
    dropdown = clean[m.start() :]
    # 区块到 </Transition> 为止（用户菜单 Teleport 块的结束标记）
    end = dropdown.find("</Transition>")
    dropdown = dropdown[:end] if end != -1 else dropdown

    if "消息中心" in dropdown:
        fail("头像下拉菜单区块内出现「消息中心」文本行——已删条目复发")
    # 主题行是运行时拼的 `外观：${label}（点击切换）`，模板里出现 {{ themeTitle }}
    # 即视为复发；顶栏按钮的 themeTitle 用法不在本区块内。
    if "themeTitle" in dropdown:
        fail("头像下拉菜单区块内出现主题行（themeTitle）——已删条目复发")

    print("OK: 头像菜单防复发契约通过（无 dropdown-narrow-only；菜单区无消息中心/外观行）")


if __name__ == "__main__":
    main()
