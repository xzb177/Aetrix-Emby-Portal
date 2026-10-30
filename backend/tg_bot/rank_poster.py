"""观影榜海报绘图（借鉴清单第 5 项）：PIL 竖版海报，运行时生成。

- 版式与 #1515 品牌卡同语言：深青渐变底（#016a71 系）、薄荷点缀、
  白字；标题行＋逐行「名次 · 用户名 | 时长」＋日期＋品牌尾。
- 字体复用 backend/assets/fonts 的 NotoSansSC（与库封面同一批字体文件）。
- 图内不放 emoji（字体无彩色 emoji 字形）：名次用数字、Top3 用点缀色。
- 成图只落临时目录（或调用方给定路径），发送后由调用方删除，不进仓库。
"""
from __future__ import annotations

import logging
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

POSTER_WIDTH = 900
HEADER_H = 216
ROW_H = 68
FOOTER_H = 96
SIDE = 64

# 品牌色（与 tg_bot/assets/aetrix-card.png 卡面一致的深青系）
_TOP = (6, 85, 90)
_BOTTOM = (3, 47, 52)
_ACCENT = (40, 205, 183)
_GOLD = (245, 197, 66)
_WHITE = (255, 255, 255)
_DIM = (168, 216, 211)
_LINE = (23, 110, 116)

_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"


def poster_height(rows: int) -> int:
    return HEADER_H + ROW_H * max(0, int(rows)) + FOOTER_H


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


def render_rank_poster(period: str, rows: list[dict], window_start: datetime,
                       out_path=None) -> Path:
    """把榜单行绘成竖版海报 PNG，返回文件路径。

    rows：queries.watch_duration_rank 的返回（已排序、已截断）。
    out_path 为空时落系统临时目录（调用方负责发送后删除）。
    """
    Image, ImageDraw, ImageFont = _pil()
    rows = list(rows or [])
    height = poster_height(len(rows))
    img = Image.new("RGB", (POSTER_WIDTH, height), _BOTTOM)
    draw = ImageDraw.Draw(img)
    # 深青竖向渐变底
    for y in range(height):
        t = y / max(1, height - 1)
        draw.line(
            [(0, y), (POSTER_WIDTH, y)],
            fill=tuple(int(_TOP[i] + (_BOTTOM[i] - _TOP[i]) * t) for i in range(3)),
        )

    f_kicker = _font(ImageFont, "NotoSansSC-Regular.ttf", 30)
    f_title = _font(ImageFont, "NotoSansSC-Bold.ttf", 56)
    f_date = _font(ImageFont, "NotoSansSC-Regular.ttf", 30)
    f_rank = _font(ImageFont, "NotoSansSC-Bold.ttf", 36)
    f_name = _font(ImageFont, "NotoSansSC-Regular.ttf", 34)
    f_dur = _font(ImageFont, "NotoSansSC-Bold.ttf", 34)
    f_foot = _font(ImageFont, "NotoSansSC-Regular.ttf", 26)

    is_week = period == "week"
    draw.text((SIDE, 44), "AETRIX · 公益服", font=f_kicker, fill=_ACCENT)
    draw.text(
        (SIDE, 88),
        "本周观影榜" if is_week else "今日观影榜",
        font=f_title,
        fill=_WHITE,
    )
    draw.text((SIDE, 164), _date_label(period, window_start), font=f_date, fill=_DIM)
    draw.line(
        [(SIDE, HEADER_H - 14), (POSTER_WIDTH - SIDE, HEADER_H - 14)],
        fill=_ACCENT,
        width=3,
    )

    name_max = POSTER_WIDTH - SIDE - 120 - 260
    for i, row in enumerate(rows):
        y = HEADER_H + i * ROW_H
        cy = y + ROW_H // 2
        rank = i + 1
        rank_color = _GOLD if rank <= 3 else _DIM
        draw.text((SIDE, cy - 24), str(rank), font=f_rank, fill=rank_color)
        name = _fit_text(draw, row.get("username") or "", f_name, name_max)
        draw.text((SIDE + 92, cy - 22), name, font=f_name, fill=_WHITE)
        dur = format_duration(int(row.get("seconds") or 0))
        dw = draw.textlength(dur, font=f_dur)
        draw.text((POSTER_WIDTH - SIDE - dw, cy - 22), dur, font=f_dur, fill=_ACCENT)
        if i < len(rows) - 1:
            draw.line(
                [(SIDE, y + ROW_H - 1), (POSTER_WIDTH - SIDE, y + ROW_H - 1)],
                fill=_LINE,
                width=1,
            )

    fy = height - FOOTER_H + 28
    draw.text(
        (SIDE, fy),
        "Aetrix · 每日 20:30 更新" if not is_week else "Aetrix · 每周日 21:00 更新",
        font=f_foot,
        fill=_DIM,
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
