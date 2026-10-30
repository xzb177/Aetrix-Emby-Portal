"""Telegram Bot 卡片式版式骨架（用户 2026-10-30 批准）。

所有面板共用同一套呈现约定，业务模块只负责取值与语义：

- 标题行：``▎<b>标题</b>``，主界面改用 ✨ 标语＋问候行；
- 信息行：``· emoji <b>标签</b> | 值``，一行一个数据；
- 说明、步骤与列表摘要可以跟在信息行之后，但不再另起一套段标题格式；
- 按钮由调用方按两列网格组织，末行用 :func:`home_row` 整行返回主界面。

本模块不查数据库、不做业务判断。``CAPTION_LIMIT`` 是 Telegram
sendPhoto/editMessageCaption 的 caption 上限，发送层据此决定图卡或纯文本。
"""
from __future__ import annotations

import html
from pathlib import Path

CAPTION_LIMIT = 1024
BRAND_IMAGE = Path(__file__).with_name("assets") / "aetrix-card.png"
REDPACKET_IMAGE = Path(__file__).with_name("assets") / "redpacket-cover.jpg"


def panel_image(override=None):
    """面板图解析：给了专属图且文件在就用专属图，缺失一律回退品牌图。"""
    if override is not None and Path(override).is_file():
        return Path(override)
    return BRAND_IMAGE


HOME_CALLBACK = "menu:main"


def esc(value) -> str:
    """HTML 转义任意展示值（None 显示为空串）。"""
    return html.escape(str(value or ""))


def title(text: str, username: str | None = None) -> str:
    """标准标题行：``▎<b>标题</b>``，可选在标题后展示用户名。"""
    suffix = f"{esc(username)}" if username else ""
    return f"▎<b>{text}</b>{suffix}"


def row(emoji: str, label: str, value) -> str:
    """标准信息行：``· emoji <b>标签</b> | 值``（标签统一加粗）。值允许调用方传入已转义 HTML。"""
    return f"· {emoji} <b>{label}</b> | {value}"


def card(title_line: str, rows: list[str] | tuple[str, ...] = (),
         notes: list[str] | tuple[str, ...] = ()) -> str:
    """组装卡片正文：标题、信息行、可选说明行。"""
    lines = [title_line]
    lines.extend(rows)
    if notes:
        lines.append("")
        lines.extend(notes)
    return "\n".join(lines)


def caption_fits(text: str) -> bool:
    """caption 是否能完整放进 Telegram 的 1024 字符上限。"""
    return len(text or "") <= CAPTION_LIMIT


def home_row(text: str = "♻️ 主界面") -> list[dict]:
    """整行返回主界面的按钮行。"""
    return [{"text": text, "callback_data": HOME_CALLBACK}]


def back_markup(text: str = "♻️ 主界面") -> dict:
    """只有整行主界面按钮的 markup（菜单子页统一返回入口）。"""
    return {"inline_keyboard": [home_row(text)]}


def append_home_row(markup: dict | None, text: str = "♻️ 主界面") -> dict:
    """在既有按钮网格末尾追加整行主界面按钮；已有时不重复追加。"""
    rows = [list(r) for r in (markup or {}).get("inline_keyboard", [])]
    if not any(
        button.get("callback_data") == HOME_CALLBACK
        for r in rows for button in r
    ):
        rows.append(home_row(text))
    return {"inline_keyboard": rows}
