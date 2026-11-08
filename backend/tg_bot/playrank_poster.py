"""每日播放排行榜海报图：与 playrank 文字榜同发的一张竖版图。

形态照站主给的参考图（竞品「今日热播榜」）：深色底、红色点缀，
大标题「今日热播榜」＋右上日期（MM.DD），上半电影区、下半剧集区，
每区固定前 5 张海报横排（与 bot_playrank_top_n 无关），第一名
海报略大、名次号红色，名次号＋片名在各自海报下方。

数据与文字榜同一份聚合（playrank.item_play_rank 的行）：本模块
只做「行 → 海报格」的组装与渲染，不另算排名。取图走站内图片
口径（与 updatedigest._backdrop_file 同路）：poster_path 本地
文件优先，只有远程 primary_image_url 时走 image_store.localize
落站内缓存后再用；取不到图的条目用带片名的占位块顶上，不缺格、
不整图失败。

发送（playrank.push_daily_rank 调用）：sendPhoto 一条，文字榜
全文作 caption（调用方已判过 caption 上限）；生成/发送失败只记
日志回 (False, None)，绝不向上抛、不堵文字榜降级路径。
开关 bot_playrank_poster_enabled 默认开（playrank 读侧判定）。
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

from backend.emby_server import image_store
from backend.emby_server import models as em
from backend.tg_bot import sender
from backend.tg_bot.rank_poster import _fit_text, _font, _pil

logger = logging.getLogger(__name__)

POSTER_LIMIT = 5

# 画布几何（宽固定 1080，竖版；高度随分区数变）。
WIDTH = 1080
MARGIN = 56
GAP = 16
FIRST_W = 232
CELL_W = 168  # (1080 - 2*56 - 4*16 - 232) / 4
FIRST_H = 348
CELL_H = 252
ROW_H = FIRST_H  # 海报区高度，小海报底对齐
CAPTION_TOP_GAP = 14
CAPTION_H = 96  # 名次＋片名最多 3 行（行高 31）
SECTION_HEAD_H = 58
SECTION_H = SECTION_HEAD_H + ROW_H + CAPTION_TOP_GAP + CAPTION_H
SECTION_GAP = 34
HEADER_BOTTOM = 222
BOTTOM_PAD = 30

# 配色：近黑底、红色点缀（照参考图）。
_BG_TOP = (13, 12, 16)
_BG_BOTTOM = (5, 5, 7)
_RED = (216, 58, 48)
_INK = (238, 238, 241)
_MUTED = (154, 152, 160)
_RANK_REST = (172, 170, 178)
_LINE = (255, 255, 255, 26)
_PLACEHOLDER_BG = (34, 31, 38)
_PLACEHOLDER_EDGE = (96, 90, 104)


# ---------------- 取图与组装 ----------------

def poster_file_for_item(item) -> str | None:
    """条目海报 → 本地文件路径（站内图片口径）；取不到返回 None。

    poster_path 本地文件优先；只有远程 primary_image_url 时走
    image_store.localize 落站内内容寻址缓存，绝不把外链直接丢给
    Telegram 拉取。
    """
    if item is None:
        return None
    local = getattr(item, "poster_path", None)
    if local and not str(local).startswith(("http://", "https://")):
        if os.path.isfile(str(local)):
            return str(local)
    url = getattr(item, "primary_image_url", None) or (
        local if local and str(local).startswith(("http://", "https://")) else None
    )
    if url:
        try:
            path = image_store.localize(str(url))
        except Exception:
            logger.exception(
                "playrank 海报本地化失败 item=%s", getattr(item, "id", "?")
            )
            path = ""
        if path and os.path.isfile(path):
            return path
    return None


def build_sections(db, movies, series) -> dict:
    """文字榜行 → 海报格：{"movies": [{name, poster}], "series": [...]}。

    每区截到 POSTER_LIMIT；poster 为本地文件路径，取不到图为 None
    （渲染时占位）。名次与标题与文字榜同一份行，不另算。
    """
    rows = {
        "movies": list(movies or [])[:POSTER_LIMIT],
        "series": list(series or [])[:POSTER_LIMIT],
    }
    ids = [
        int(r["item_id"])
        for r in rows["movies"] + rows["series"]
        if r.get("item_id")
    ]
    items: dict[int, em.MediaItem] = {}
    if ids:
        for it in db.query(em.MediaItem).filter(em.MediaItem.id.in_(ids)).all():
            items[int(it.id)] = it

    def cell(row):
        item = items.get(int(row.get("item_id") or 0))
        return {
            "name": row.get("name") or (item.name if item is not None else ""),
            "poster": poster_file_for_item(item),
        }

    return {key: [cell(r) for r in part] for key, part in rows.items()}


# ---------------- 渲染 ----------------

def canvas_height(section_count: int) -> int:
    n = max(0, int(section_count))
    return HEADER_BOTTOM + n * SECTION_H + max(0, n - 1) * SECTION_GAP + BOTTOM_PAD


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _background(Image, ImageDraw, ImageFilter, height: int):
    """近黑竖向渐变底，中段一道很淡的红色辉光（照参考图氛围）。"""
    img = Image.new("RGB", (WIDTH, height), _BG_BOTTOM)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        t = y / max(1, height - 1)
        draw.line([(0, y), (WIDTH, y)], fill=_lerp(_BG_TOP, _BG_BOTTOM, t))
    glow = Image.new("RGBA", (WIDTH, height), (0, 0, 0, 0))
    glow_draw = ImageDraw.Draw(glow)
    glow_draw.ellipse(
        [WIDTH * 0.06, height * 0.30, WIDTH * 0.94, height * 0.80],
        fill=(168, 30, 22, 32),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(130))
    return Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB")


def _wrap(draw, text: str, font, max_width: int) -> list[str]:
    """逐字折行（中英文混排都按字宽断，与 rank_poster 截断同口径）。"""
    lines: list[str] = []
    cur = ""
    for ch in str(text or ""):
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        if cur and draw.textlength(cur + ch, font=font) > max_width:
            # 优先在行内靠后的空格处断行，避免把英文单词劈开。
            sp = cur.rfind(" ")
            if 0 < sp and sp >= len(cur) // 2:
                lines.append(cur[:sp])
                cur = cur[sp + 1:] + ch
            else:
                lines.append(cur)
                cur = ch
        else:
            cur += ch
    if cur or not lines:
        lines.append(cur)
    return lines


def _cover(Image, im, w: int, h: int):
    """等比放大后居中裁切，铺满 w×h（海报比例不一时的统一口径）。"""
    iw, ih = im.size
    scale = max(w / iw, h / ih)
    nw = max(w, round(iw * scale))
    nh = max(h, round(ih * scale))
    im = im.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return im.crop((left, top, left + w, top + h))


def _placeholder(Image, ImageDraw, ImageFont, name: str, w: int, h: int):
    """缺图占位块：深灰底＋红点缀线＋片名居中，不缺格。"""
    tile = Image.new("RGB", (w, h), _PLACEHOLDER_BG)
    draw = ImageDraw.Draw(tile)
    draw.rectangle([0, 0, w - 1, h - 1], outline=_PLACEHOLDER_EDGE, width=2)
    font = _font(ImageFont, "NotoSansSC-Bold.ttf", 26)
    lines = _wrap(draw, name, font, w - 28)[:4]
    line_h = 36
    total_h = len(lines) * line_h
    y = (h - total_h) // 2
    draw.rectangle([w // 2 - 14, y - 18, w // 2 + 14, y - 14], fill=_RED)
    for line in lines:
        lw = draw.textlength(line, font=font)
        draw.text(((w - lw) // 2, y), line, font=font, fill=_INK)
        y += line_h
    return tile


def _poster_tile(Image, ImageDraw, ImageFont, cell: dict, w: int, h: int):
    path = cell.get("poster")
    if path:
        try:
            with Image.open(path) as im:
                return _cover(Image, im.convert("RGB"), w, h)
        except Exception:
            logger.exception("playrank 海报图读取失败 path=%s", path)
    return _placeholder(Image, ImageDraw, ImageFont, cell.get("name") or "", w, h)


def render_playrank_poster(date_sh, movies, series, out_path=None) -> Path:
    """把海报格绘成竖版 PNG，返回文件路径。

    movies / series 是 build_sections 的格列表（{name, poster}）；
    空区省略，两区皆空抛 ValueError（调用方据此走纯文字降级）。
    out_path 为空时落系统临时目录（调用方负责发送后删除）。
    """
    Image, ImageDraw, ImageFont = _pil()
    from PIL import ImageFilter

    sections = []
    if movies:
        sections.append(("电影", "FILMS", list(movies)[:POSTER_LIMIT]))
    if series:
        sections.append(("剧集", "SERIES", list(series)[:POSTER_LIMIT]))
    if not sections:
        raise ValueError("playrank 海报至少要一个分区有条目")

    height = canvas_height(len(sections))
    img = _background(Image, ImageDraw, ImageFilter, height)

    f_kicker = _font(ImageFont, "NotoSansSC-Regular.ttf", 23)
    f_en_small = _font(ImageFont, "NotoSansSC-Regular.ttf", 19)
    f_date = _font(ImageFont, "NotoSansSC-Bold.ttf", 56)
    f_title = _font(ImageFont, "NotoSansSC-Bold.ttf", 98)
    f_ghost = _font(ImageFont, "NotoSansSC-Bold.ttf", 168)
    f_sec_cn = _font(ImageFont, "NotoSansSC-Bold.ttf", 40)
    f_sec_en = _font(ImageFont, "NotoSansSC-Regular.ttf", 22)
    f_rank = _font(ImageFont, "NotoSansSC-Bold.ttf", 30)
    f_name = _font(ImageFont, "NotoSansSC-Regular.ttf", 24)

    # 分区大字背景（FILMS / SERIES 幽灵字，压在内容之下）。
    tops = [
        HEADER_BOTTOM + i * (SECTION_H + SECTION_GAP) for i in range(len(sections))
    ]
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    probe = ImageDraw.Draw(img)
    for (_cn, en, _cells), top in zip(sections, tops):
        tw = probe.textlength(en, font=f_ghost)
        od.text(
            (WIDTH - MARGIN + 6 - tw, top - 64),
            en,
            font=f_ghost,
            fill=(255, 255, 255, 13),
        )
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(img)

    # 页眉：左上栏目名、右上日期（照参考图）。
    draw.rectangle([MARGIN, 52, MARGIN + 30, 57], fill=_RED)
    x = MARGIN + 44
    for ch in "PLAYBACK RANKING":
        draw.text((x, 44), ch, font=f_kicker, fill=_MUTED)
        x += draw.textlength(ch, font=f_kicker) + 4
    date_text = f"{date_sh.month:02d}.{date_sh.day:02d}"
    date_w = draw.textlength(date_text, font=f_date)
    date_x = WIDTH - MARGIN - date_w
    draw.text((date_x, 40), date_text, font=f_date, fill=_RED)
    for j, small in enumerate(("DAILY EDITION", "24 HOURS / CST")):
        sw = draw.textlength(small, font=f_en_small)
        draw.text(
            (date_x - 20 - sw, 50 + j * 27), small, font=f_en_small, fill=_MUTED
        )
    draw.text((MARGIN - 4, 92), "今日热播榜", font=f_title, fill=_INK)

    for si, ((cn, en, cells), top) in enumerate(zip(sections, tops)):
        if si > 0:
            div_y = top - SECTION_GAP // 2
            draw.line(
                [(MARGIN, div_y), (WIDTH - MARGIN, div_y)], fill=_LINE, width=1
            )
            draw.line(
                [(WIDTH // 2 - 32, div_y), (WIDTH // 2 + 32, div_y)],
                fill=_RED,
                width=2,
            )
        # 分区标题：电影左对齐、剧集右对齐（照参考图）。
        range_label = f"{en} · 01–{len(cells):02d}"
        cn_w = draw.textlength(cn, font=f_sec_cn)
        en_w = draw.textlength(range_label, font=f_sec_en)
        if si == 0:
            draw.rectangle([MARGIN, top - 16, MARGIN + 56, top - 11], fill=_RED)
            draw.text((MARGIN, top), cn, font=f_sec_cn, fill=_INK)
            draw.text(
                (MARGIN + cn_w + 14, top + 16),
                range_label,
                font=f_sec_en,
                fill=_MUTED,
            )
        else:
            start_x = WIDTH - MARGIN - (cn_w + 14 + en_w)
            draw.rectangle(
                [WIDTH - MARGIN - 56, top - 16, WIDTH - MARGIN, top - 11],
                fill=_RED,
            )
            draw.text((start_x, top), cn, font=f_sec_cn, fill=_INK)
            draw.text(
                (start_x + cn_w + 14, top + 16),
                range_label,
                font=f_sec_en,
                fill=_MUTED,
            )
        # 海报横排：第一名略大，其余等距，底对齐。
        posters_top = top + SECTION_HEAD_H
        row_bottom = posters_top + ROW_H
        x = MARGIN
        for i, cell in enumerate(cells):
            w = FIRST_W if i == 0 else CELL_W
            h = FIRST_H if i == 0 else CELL_H
            y0 = row_bottom - h
            tile = _poster_tile(Image, ImageDraw, ImageFont, cell, w, h)
            img.paste(tile, (x, y0))
            if i == 0:
                draw.rectangle([x, y0, x + w - 1, y0 + h - 1], outline=_RED, width=2)
            else:
                draw.rectangle(
                    [x, y0, x + w - 1, y0 + h - 1],
                    outline=(255, 255, 255, 40),
                    width=1,
                )
            # 名次号＋片名（海报下方，片名最多 3 行）。
            cy = row_bottom + CAPTION_TOP_GAP
            rank_text = f"{i + 1:02d}"
            draw.text(
                (x, cy),
                rank_text,
                font=f_rank,
                fill=_RED if i == 0 else _RANK_REST,
            )
            rank_w = draw.textlength(rank_text, font=f_rank)
            title_x = x + rank_w + 10
            lines = _wrap(draw, cell.get("name") or "", f_name, x + w - title_x)
            if len(lines) > 3:
                lines = lines[:3]
                lines[2] = _fit_text(draw, lines[2], f_name, x + w - title_x)
            for li, line in enumerate(lines):
                draw.text(
                    (title_x, cy + li * 31), line, font=f_name, fill=_INK
                )
            x += w + GAP

    if out_path is None:
        fd, tmp = tempfile.mkstemp(prefix="aetrix-playrank-", suffix=".png")
        os.close(fd)
        out_path = Path(tmp)
    out_path = Path(out_path)
    img.save(str(out_path), format="PNG")
    logger.info(
        "playrank poster rendered movies=%s series=%s path=%s",
        len(movies or []),
        len(series or []),
        out_path,
    )
    return out_path


# ---------------- 发送 ----------------

def send_rank_photo(db, chat_id, date_sh, movies, series, caption=None):
    """组装→渲染→sendPhoto。返回 (ok, message_id)。

    caption 非空即图文合一（调用方已判过上限）；为空则只发图。
    两区皆空不发；任何失败只记日志回 (False, None)，临时图删净。
    movies / series 是文字榜的行（{item_id, name, ...}）。
    """
    path = None
    try:
        sections = build_sections(db, movies, series)
        if not sections["movies"] and not sections["series"]:
            return False, None
        path = render_playrank_poster(
            date_sh, sections["movies"], sections["series"]
        )
        ok, err, message_id = sender.send_photo_ex(
            db, chat_id, caption or "", photo_path=path, parse_mode=None
        )
        if not ok:
            logger.warning("playrank 海报发送失败 err=%s", err)
            return False, None
        return True, message_id
    except Exception:
        logger.exception("playrank 海报生成/发送异常")
        return False, None
    finally:
        if path is not None:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass
