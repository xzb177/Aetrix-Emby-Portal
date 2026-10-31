"""观影榜海报绘图（借鉴清单第 5 项）：PIL 电影票根版，运行时生成。

- 深青渐变底上放一张米色纸票：票头、榜单行、虚线撕线与票根分开。
- 字体复用 backend/assets/fonts 的 NotoSansSC（与库封面同一批字体文件）。
- 图内不放 emoji（字体无彩色 emoji 字形）；第一名以红色「冠军」印章强调。
- 成图只落临时目录（或调用方给定路径），发送后由调用方删除，不进仓库。
"""
from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

POSTER_WIDTH = 900

# 票面几何：高度只随榜单行数变化，票头、撕线下票根与上下留白固定。
_TICKET_X = 48
_TICKET_TOP = 62
_TICKET_BOTTOM_MARGIN = 62
_ROWS_OFFSET = 240
_ROW_H = 84
_ROWS_TO_STUB_GAP = 38
_STUB_H = 178
_TICKET_RADIUS = 30
_CONTENT_INSET = 52
_NAME_OFFSET = 84

# 深青底与米色纸票的配色。
_BG_TOP = (8, 46, 51)
_BG_BOTTOM = (3, 24, 28)
_PAPER = (247, 241, 227)
_SHADOW = (2, 18, 21)
_INK = (35, 42, 38)
_MUTED = (122, 110, 84)
_TAN = (150, 128, 84)
_HEADER_LINE = (222, 212, 184)
_ROW_LINE = (230, 221, 196)
_DASH = (196, 186, 158)
_NOTCH = (4, 30, 35)
_STAMP = (190, 52, 38)
_RANK_COLORS = {
    1: (178, 44, 32),
    2: (110, 122, 124),
    3: (176, 116, 62),
}
_RANK_FALLBACK = (160, 148, 116)
_BARCODE_WIDTHS = (
    3, 1, 2, 1, 1, 3, 2, 2, 1, 1, 3, 1, 2, 1, 3, 2,
    1, 1, 2, 3, 1, 2, 1, 1, 3, 1, 2, 2, 1, 3, 1, 2,
)

_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"


def poster_height(rows: int) -> int:
    count = max(0, int(rows))
    return (
        _TICKET_TOP
        + _ROWS_OFFSET
        + _ROW_H * count
        + _ROWS_TO_STUB_GAP
        + _STUB_H
        + _TICKET_BOTTOM_MARGIN
    )


def _pil():
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as exc:  # pragma: no cover - 环境缺 Pillow 时明确失败
        raise RuntimeError("Pillow 未安装，无法生成观影榜海报") from exc
    return Image, ImageDraw, ImageFont


def _font(ImageFont, name: str, size: int):
    path = _FONT_DIR / name
    if path.exists():
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def format_duration(seconds: int) -> str:
    """时长展示：与 features._dur 口径一致（X小时Y分 / Y分）。"""
    secs = max(0, int(seconds or 0))
    h, rem = divmod(secs, 3600)
    m = rem // 60
    if h:
        return f"{h}小时{m}分" if m else f"{h}小时"
    return f"{m}分" if m else "不到1分"


def _fit_text(draw, text: str, font, max_width: int) -> str:
    """超宽用户名截断加省略号，保证一行放得下。"""
    text = str(text or "")
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text + "…"


def _date_label(period: str, window_start: datetime) -> str:
    if period == "week":
        end = window_start + timedelta(days=6)
        return f"{window_start.month}月{window_start.day}日 – {end.month}月{end.day}日"
    return f"{window_start.year}年{window_start.month}月{window_start.day}日"


def _background(Image, ImageDraw, height: int):
    """深青竖向渐变底，叠两处很淡的装饰圆。"""
    img = Image.new("RGB", (POSTER_WIDTH, height), _BG_BOTTOM)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / max(1, height - 1)
        draw.line(
            [(0, y), (POSTER_WIDTH, y)],
            fill=tuple(int(_BG_TOP[i] + (_BG_BOTTOM[i] - _BG_TOP[i]) * t) for i in range(3)),
        )

    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_draw.ellipse(
        [POSTER_WIDTH - 240, -180, POSTER_WIDTH + 110, 170],
        fill=(255, 255, 255, 9),
    )
    overlay_draw.ellipse(
        [-140, height - 260, 120, height],
        fill=(255, 255, 255, 6),
    )
    return Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")


def _draw_spaced_text(draw, xy, text: str, font, fill, gap: int = 4) -> None:
    """逐字绘制票头小字，给英文与符号留出均匀字距。"""
    x, y = xy
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        x += draw.textlength(char, font=font) + gap


def _champion_stamp(Image, ImageDraw, ImageFont):
    """红色圆形印章单独成层，旋转后再贴回票面。"""
    stamp = Image.new("RGBA", (190, 190), (0, 0, 0, 0))
    draw = ImageDraw.Draw(stamp)
    draw.ellipse([8, 8, 182, 182], outline=_STAMP + (235,), width=7)
    draw.ellipse([24, 24, 166, 166], outline=_STAMP + (160,), width=2)
    draw.text(
        (95, 95),
        "冠军",
        font=_font(ImageFont, "NotoSansSC-Bold.ttf", 56),
        fill=_STAMP + (235,),
        anchor="mm",
    )
    return stamp.rotate(12, expand=True, resample=Image.BICUBIC)


def _draw_barcode(draw, x: int, y: int) -> None:
    """票根装饰条码使用固定条纹，不承载榜单数据。"""
    for unit in _BARCODE_WIDTHS:
        bar_width = unit * 2
        draw.rectangle([x, y, x + bar_width, y + 84], fill=_INK)
        x += bar_width + 4


def render_rank_poster(period: str, rows: list[dict], window_start: datetime,
                       out_path=None) -> Path:
    """把榜单行绘成电影票根海报 PNG，返回文件路径。

    rows：queries.watch_duration_rank 的返回（已排序、已截断）。
    out_path 为空时落系统临时目录（调用方负责发送后删除）。
    """
    Image, ImageDraw, ImageFont = _pil()
    rows = list(rows or [])
    height = poster_height(len(rows))
    img = _background(Image, ImageDraw, height)
    draw = ImageDraw.Draw(img)

    ticket_left = _TICKET_X
    ticket_top = _TICKET_TOP
    ticket_right = POSTER_WIDTH - _TICKET_X
    rows_top = ticket_top + _ROWS_OFFSET
    stub_y = rows_top + len(rows) * _ROW_H + _ROWS_TO_STUB_GAP
    ticket_bottom = stub_y + _STUB_H
    content_left = ticket_left + _CONTENT_INSET
    content_right = ticket_right - _CONTENT_INSET

    # 纸票本体与阴影。
    draw.rounded_rectangle(
        [ticket_left + 6, ticket_top + 10, ticket_right + 6, ticket_bottom + 10],
        radius=_TICKET_RADIUS,
        fill=_SHADOW,
    )
    draw.rounded_rectangle(
        [ticket_left, ticket_top, ticket_right, ticket_bottom],
        radius=_TICKET_RADIUS,
        fill=_PAPER,
    )

    # 撕线两侧的半圆缺口与虚线。
    for center_x in (ticket_left, ticket_right):
        draw.ellipse(
            [center_x - 17, stub_y - 17, center_x + 17, stub_y + 17],
            fill=_NOTCH,
        )
    dash_x = ticket_left + 34
    while dash_x < ticket_right - 30:
        draw.line([(dash_x, stub_y), (dash_x + 16, stub_y)], fill=_DASH, width=3)
        dash_x += 30

    f_kicker = _font(ImageFont, "NotoSansSC-Regular.ttf", 25)
    f_title = _font(ImageFont, "NotoSansSC-Bold.ttf", 62)
    f_date = _font(ImageFont, "NotoSansSC-Regular.ttf", 28)
    f_rank = _font(ImageFont, "NotoSansSC-Bold.ttf", 40)
    f_name = _font(ImageFont, "NotoSansSC-Regular.ttf", 34)
    f_name_first = _font(ImageFont, "NotoSansSC-Bold.ttf", 36)
    f_duration = _font(ImageFont, "NotoSansSC-Bold.ttf", 34)
    f_admit = _font(ImageFont, "NotoSansSC-Bold.ttf", 34)
    f_foot = _font(ImageFont, "NotoSansSC-Regular.ttf", 25)

    is_week = period == "week"
    _draw_spaced_text(
        draw,
        (content_left, ticket_top + 40),
        "AETRIX CINEMA · 公益服",
        f_kicker,
        _TAN,
    )
    draw.text(
        (content_left, ticket_top + 78),
        "本周观影榜" if is_week else "今日观影榜",
        font=f_title,
        fill=_INK,
    )
    session_label = "周日 21:00 场" if is_week else "晚场 20:30"
    draw.text(
        (content_left, ticket_top + 162),
        f"{_date_label(period, window_start)} · {session_label}",
        font=f_date,
        fill=_MUTED,
    )
    draw.line(
        [(content_left, ticket_top + 214), (content_right, ticket_top + 214)],
        fill=_HEADER_LINE,
        width=2,
    )

    for i, row in enumerate(rows):
        rank = i + 1
        y = rows_top + i * _ROW_H
        center_y = y + _ROW_H // 2
        name_font = f_name_first if rank == 1 else f_name
        duration = format_duration(int(row.get("seconds") or 0))
        duration_width = draw.textlength(duration, font=f_duration)
        name_max = content_right - duration_width - (content_left + _NAME_OFFSET) - 24
        name = _fit_text(draw, row.get("username") or "", name_font, max(0, int(name_max)))

        draw.text(
            (content_left, center_y),
            str(rank),
            font=f_rank,
            fill=_RANK_COLORS.get(rank, _RANK_FALLBACK),
            anchor="lm",
        )
        draw.text(
            (content_left + _NAME_OFFSET, center_y),
            name,
            font=name_font,
            fill=_INK,
            anchor="lm",
        )
        draw.text(
            (content_right - duration_width, center_y),
            duration,
            font=f_duration,
            fill=_INK,
            anchor="lm",
        )
        if i < len(rows) - 1:
            draw.line(
                [(content_left, y + _ROW_H), (content_right, y + _ROW_H)],
                fill=_ROW_LINE,
                width=1,
            )

    if rows:
        stamp = _champion_stamp(Image, ImageDraw, ImageFont)
        img.paste(stamp, (int(content_right - 218), ticket_top + 6), stamp)
        draw = ImageDraw.Draw(img)

    _draw_barcode(draw, content_left, stub_y + 44)
    admit_text = "ADMIT ONE"
    admit_width = draw.textlength(admit_text, font=f_admit)
    draw.text(
        (content_right - admit_width, stub_y + 42),
        admit_text,
        font=f_admit,
        fill=_TAN,
    )
    update_text = (
        "Aetrix · 每周日 21:00 更新" if is_week else "Aetrix · 每日 20:30 更新"
    )
    update_width = draw.textlength(update_text, font=f_foot)
    draw.text(
        (content_right - update_width, stub_y + 96),
        update_text,
        font=f_foot,
        fill=_MUTED,
    )

    if out_path is None:
        import os

        fd, tmp = tempfile.mkstemp(prefix="aetrix-rank-", suffix=".png")
        os.close(fd)
        out_path = Path(tmp)
    out_path = Path(out_path)
    img.save(str(out_path), format="PNG")
    logger.info("rank poster rendered period=%s rows=%s path=%s", period, len(rows), out_path)
    return out_path
