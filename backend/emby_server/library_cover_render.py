"""媒体库封面渲染：从库里最近的海报自动拼一张 1920×1080 的横版封面

后台「媒体库封面」三种自动样式，管理员不用传图配字，系统自己去库里挑：

- ``visual``    景深横幅：宽底图压暗铺满 + 三张海报右侧错落（中间抬高）+
  投影 + 左下角衬线大标题、金色装饰线、字距拉开的副标题
- ``poster``    编辑拼贴：左侧深色面板做排印（金色眉题 + 大标题 + 分隔线 +
  副标题），右侧宽图满幅，三张小海报在面板内居中陈列
- ``filmstrip`` 暗房胶片：深灰底 + 顶部微光，四张海报等距陈列带细边框和
  投影，标题居中、副标题两侧配金色短线

**选图规则**「最新入库的海报」：按 ``emby_items.last_scraped_at``（刮削完成时刻）倒序，
取最近入库且有海报的条目。不能用 ``updated_at``——进度刷盘每几秒就刷一次，
它永远是「刚刚」，排不出新片。海报优先用 ``poster_path``（已本地化到磁盘
的文件），没有才退回 ``primary_image_url`` 远程地址。宽底图（visual/poster
用）优先 ``backdrop_path``，没有就退回 ``backdrop_image_url``，再没有就用
第一张海报压暗顶上。这样刚补完刮削的库会立刻换成新海报，不用手动点重新生成。

标题 / 副标题支持三个变量：``{library}`` 媒体库名、``{type}`` 内容类型、
``{year}`` 当前年份（内容年份要从条目聚合才有意义，这里刻意不查库，
避免每改一个字都打一次库）。

**字体不可用时自动省略文字**：字体缺失/损坏/Pillow 没装时仍出图（纯底图），
不抛异常——封面是锦上添花，不能因字体问题连上传和展示都做不了。

输出统一 1920×1080 WebP（面板展示就是横版，与自动生成样式一致）。
"""
from __future__ import annotations

import io
import logging
import os
from datetime import datetime
from typing import List, Optional

logger = logging.getLogger("aetrix.library_cover")

CANVAS_W = 1920
CANVAS_H = 1080

TEMPLATES = ("poster", "visual", "filmstrip")
DEFAULT_TEMPLATE = "poster"
# 各模板需要几张海报（不够就少放几张，不补空白）
TEMPLATE_POSTER_COUNT = 3       # poster / visual：右侧或面板内三张小海报
TEMPLATE_FILMSTRIP_COUNT = 4    # filmstrip：横向四张

# 设计语言：深海军蓝底 + 金色点缀，全模板统一
_INK = (13, 16, 24, 255)        # 面板 / 深色底
_ACCENT = (212, 162, 78, 255)   # 金色装饰线
_TITLE_FILL = (255, 255, 255, 255)
_SUB_FILL = (170, 178, 192, 255)
_BRAND_KICKER = "AETRIX · 媒体库"  # poster 模板左上角眉题，品牌固定文案

_FONT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "assets", "fonts"
)
_FONT_BOLD = os.path.join(_FONT_DIR, "NotoSansSC-Bold.ttf")
_FONT_REGULAR = os.path.join(_FONT_DIR, "NotoSansSC-Regular.ttf")

_font_cache: dict = {}


def _pil():
    """Pillow 不可用时返回 None，调用方据此走「无字出图」"""
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont

        return Image, ImageDraw, ImageFilter, ImageFont
    except ImportError:  # pragma: no cover
        logger.warning("Pillow 不可用，封面将不渲染文字")
        return None


def _font(size: int, bold: bool = False):
    """取一个已缓存的中文字体；取不到返回 None（调用方省略文字）"""
    pil = _pil()
    if pil is None:
        return None
    _img, _draw, _filter, ImageFont = pil
    path = _FONT_BOLD if bold else _FONT_REGULAR
    key = (path, size)
    if key in _font_cache:
        return _font_cache[key]
    if not os.path.isfile(path):
        logger.warning("封面字体缺失：%s，文字将被省略", path)
        _font_cache[key] = None
        return None
    try:
        loaded = ImageFont.truetype(path, size)
    except Exception:  # noqa: BLE001
        logger.warning("封面字体无法加载：%s，文字将被省略", path, exc_info=True)
        loaded = None
    _font_cache[key] = loaded
    return loaded


# collection_type 存的是 API 名（tvshows/movies…），直接显示给管理员看是英文，
# 这里映射成中文；未知值原样透出，至少不空。
TYPE_LABELS = {
    "tvshows": "剧集", "tvshow": "剧集", "series": "剧集",
    "movies": "电影", "movie": "电影", "film": "电影",
    "music": "音乐", "musicalbum": "音乐", "audio": "音乐",
    "mixed": "混合", "boxset": "合集",
}


def human_media_type(media_type: str) -> str:
    """把 collection_type 的 API 名翻成中文（未知值原样返回）"""
    key = (media_type or "").strip().lower()
    return TYPE_LABELS.get(key, media_type or "")


def render_text(text: str, library_name: str, media_type: str,
                year: Optional[int] = None) -> str:
    """把 ``{library}`` / ``{type}`` / ``{year}`` 换成实际值

    未知占位符原样保留（便于管理员看出写错了），纯空白返回空串。
    """
    if not text:
        return ""
    return (text
            .replace("{library}", library_name or "")
            .replace("{type}", human_media_type(media_type))
            .replace("{year}", str(year or datetime.now().year))
            ).strip()


# ---------------- 选图 ----------------

def _usable_source(value) -> Optional[str]:
    """海报/横图值 → 可读路径或远程 URL；不可用返回 None

    ``*_path`` 存的就是磁盘上的本地文件，**不能**再过
    ``image_store.local_path()``——那个函数是给远程 URL 算内容寻址
    缓存路径的（sha1(url)[:24] + 后缀），把本地路径喂进去会得到一个
    根本不存在的文件名，于是「明明有海报却一张都选不出来」。
    """
    if not value:
        return None
    text = str(value)
    if text.startswith(("http://", "https://")):
        return text  # 远程：留给出图阶段按需下载
    return text if os.path.isfile(text) else None


def pick_recent_posters(db, library, limit: int) -> List[str]:
    """取该库**最新入库**且有海报的若干条，返回可用的本地文件路径

    排序用 ``last_scraped_at``（刮削完成时刻）。两个坑：

    - 不能用 ``updated_at``：进度刷盘每几秒写一次，它永远是「刚刚」；
    - 表上没有 ``created_at``（模型有、PG 库没补上这列），而刮削完成才
      意味着海报就位，所以 ``last_scraped_at`` 才是「新入库且有图」的准确
      时刻，排出来的正是刚补完刮削的那批新片。

    海报优先 ``poster_path``（已本地化到磁盘），其次 ``primary_image_url``。
    取不够 ``limit`` 张时继续往后翻，不足就少给——渲染层会按实际张数排版。
    """
    from backend.emby_server.models import MediaItem

    query = (db.query(MediaItem)
             .filter(MediaItem.library_id == library.id)
             .filter(MediaItem.last_scraped_at.isnot(None))
             .filter((MediaItem.poster_path.isnot(None)) | (MediaItem.primary_image_url.isnot(None)))
             .order_by(MediaItem.last_scraped_at.desc())
             .limit(max(limit * 4, 20)))
    rows = query.all()

    picked: List[str] = []
    for item in rows:
        for candidate in (item.poster_path, item.primary_image_url):
            usable = _usable_source(candidate)
            if usable:
                picked.append(usable)
                break
        if len(picked) >= limit:
            break
    return picked[:limit]


def pick_recent_backdrop(db, library) -> Optional[str]:
    """取该库最新入库且有横图的条目，供 visual/poster 模板铺底

    优先 ``backdrop_path``（本地），其次 ``backdrop_image_url``（远程）。
    一个都没有返回 None，调用方用第一张海报压暗顶上。
    """
    from backend.emby_server.models import MediaItem

    rows = (db.query(MediaItem)
            .filter(MediaItem.library_id == library.id)
            .filter(MediaItem.last_scraped_at.isnot(None))
            .filter((MediaItem.backdrop_path.isnot(None)) | (MediaItem.backdrop_image_url.isnot(None)))
            .order_by(MediaItem.last_scraped_at.desc())
            .limit(20)).all()
    for item in rows:
        for candidate in (item.backdrop_path, item.backdrop_image_url):
            usable = _usable_source(candidate)
            if usable:
                return usable
    return None


# ---------------- 出图 ----------------

def _open_source(Image_, src: str):
    """打开一张海报：本地路径直接读，远程 URL 现下载（带超时）"""
    if str(src).startswith(("http://", "https://")):
        import httpx

        try:
            with httpx.Client(timeout=15, follow_redirects=True) as client:
                resp = client.get(src)
                if resp.status_code >= 400:
                    return None
                return Image_.open(io.BytesIO(resp.content))
        except Exception:  # noqa: BLE001
            logger.debug("封面底图下载失败：%s", src, exc_info=True)
            return None
    try:
        return Image_.open(src)
    except Exception:  # noqa: BLE001
        logger.debug("封面底图无法打开：%s", src, exc_info=True)
        return None


def _cover_fit(crop_anchor: float = 0.5):
    """等比缩放到目标框内并居中裁切；``crop_anchor`` 纵向取景位置"""
    def fit(image, box_w: int, box_h: int, Image_):
        scale = max(box_w / image.width, box_h / image.height)
        big = image.resize((max(1, int(image.width * scale)),
                            max(1, int(image.height * scale))), Image_.LANCZOS)
        left = (big.width - box_w) // 2
        top = int((big.height - box_h) * crop_anchor)
        return big.crop((left, top, left + box_w, top + box_h))
    return fit


def _apply_vignette(base_rgb, Image_, ImageFilter, strength: float = 0.5):
    """四角压暗，让主体从背景里跳出来（在 RGB 底图上操作）"""
    w, h = base_rgb.size
    mask = Image_.new("L", (w, h), 0)
    from PIL import ImageDraw as _ID

    m = _ID.Draw(mask)
    m.ellipse((-w * 0.25, -h * 0.55, w * 1.25, h * 1.55), fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=int(w * 0.12)))
    dark = Image_.new("RGB", (w, h), (0, 0, 0))
    inv = Image_.eval(mask, lambda v: int((1 - v / 255) * 255 * strength))
    return Image_.composite(dark, base_rgb, inv)


def _bottom_shade(img, Image_, top_ratio: float = 0.5, alpha: int = 215) -> None:
    """底部渐变遮罩，给左下角标题腾出可读区域"""
    w, h = img.size
    strip = Image_.new("L", (1, h), 0)
    for y in range(h):
        t = max(0.0, (y / h - top_ratio) / (1 - top_ratio))
        strip.putpixel((0, y), int(alpha * (t ** 1.7)))
    layer = Image_.new("RGBA", (w, h), (5, 8, 14, 0))
    layer.putalpha(strip.resize((w, h)))
    img.alpha_composite(layer)


def _drop_shadow(base, box, Image_, ImageFilter,
                 radius: int = 34, y_offset: int = 22, opacity: int = 150) -> None:
    """给矩形卡片画投影（圆角），让海报从底图上浮起来"""
    from PIL import ImageDraw as _ID

    sh = Image_.new("RGBA", base.size, (0, 0, 0, 0))
    d = _ID.Draw(sh)
    x0, y0, x1, y1 = box
    d.rounded_rectangle([x0, y0 + y_offset, x1, y1 + y_offset],
                        radius=radius, fill=(0, 0, 0, opacity))
    sh = sh.filter(ImageFilter.GaussianBlur(radius=max(1, radius // 2)))
    base.alpha_composite(sh)


def _spaced_text(draw, xy, text: str, font, fill, spacing: int = 8) -> None:
    """逐字排字距（中文排印呼吸感就靠它）"""
    x, y = xy
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        x += draw.textlength(ch, font=font) + spacing


def _poster_card(img, src, box, Image_, ImageFilter, border_alpha: int = 40) -> bool:
    """贴一张海报卡片：投影 + 本体 + 细边框。打不开返回 False"""
    from PIL import ImageDraw as _ID

    card_img = _open_source(Image_, src)
    if card_img is None:
        return False
    x0, y0, x1, y1 = box
    card = _cover_fit(0.5)(card_img.convert("RGB"), x1 - x0, y1 - y0, Image_)
    _drop_shadow(img, box, Image_, ImageFilter)
    img.paste(card, (x0, y0))
    d = _ID.Draw(img)
    d.rounded_rectangle(box, radius=10,
                        outline=(255, 255, 255, border_alpha), width=2)
    return True


# ---------------- 三个样式 ----------------

def _template_visual(img, posters, backdrop_src, title_text: str, sub_text: str,
                     pil) -> None:
    """景深横幅：宽底图压暗铺满 + 三张海报右侧错落 + 左下角大标题"""
    Image_, ImageDraw, ImageFilter, _ImageFont = pil
    from PIL import ImageDraw as _ID

    bg_img = _open_source(Image_, backdrop_src) if backdrop_src else None
    if bg_img is None:
        bg_img = _open_source(Image_, posters[0])
    if bg_img is None:
        img.paste(Image_.new("RGBA", img.size, _INK), (0, 0))
    else:
        bg = _cover_fit(0.42)(bg_img.convert("RGB"), img.width, img.height, Image_)
        # 压暗底图：降亮 + 暗角，海报和字才能跳出来（拒绝把原图模糊当背景）
        bg = Image_.blend(bg, Image_.new("RGB", (img.width, img.height), (8, 10, 16)), 0.38)
        bg = _apply_vignette(bg, Image_, ImageFilter, 0.5)
        img.paste(bg.convert("RGBA"), (0, 0))
    _bottom_shade(img, Image_, top_ratio=0.5, alpha=215)

    # 三张海报右侧排布，中间一张抬高形成层次；张数不够就按实际张数居中
    pw, ph = 300, 450
    gap = 36
    count = len(posters)
    total = count * pw + (count - 1) * gap
    x = img.width - total - 90
    base_top = 150
    for i, src in enumerate(posters):
        top = base_top + (0 if (count == 1 or i == count // 2) else 46)
        _poster_card(img, src, (x, top, x + pw, top + ph), Image_, ImageFilter)
        x += pw + gap

    title_font = _font(118, bold=True) if title_text else None
    sub_font = _font(40) if sub_text else None
    if title_font is None and sub_font is None:
        return
    d = _ID.Draw(img)
    pad = 90
    if title_font is not None:
        d.line([(pad, img.height - 300), (pad + 120, img.height - 300)],
               fill=_ACCENT, width=6)
        d.text((pad, img.height - 285), title_text, font=title_font, fill=_TITLE_FILL)
    if sub_font is not None:
        _spaced_text(d, (pad + 4, img.height - 130), sub_text, sub_font,
                     (214, 220, 232, 255), spacing=8)


def _template_poster(img, posters, backdrop_src, title_text: str, sub_text: str,
                     pil) -> None:
    """编辑拼贴：左侧深色面板排印 + 右侧宽图满幅 + 三张小海报面板内居中"""
    Image_, ImageDraw, ImageFilter, _ImageFont = pil
    from PIL import ImageDraw as _ID

    right_w = 1180
    panel_w = img.width - right_w + 60  # 面板含 60px 渐隐衔接带
    bg_img = _open_source(Image_, backdrop_src) if backdrop_src else None
    if bg_img is None and posters:
        bg_img = _open_source(Image_, posters[0])
    if bg_img is not None:
        bg = _cover_fit(0.45)(bg_img.convert("RGB"), right_w, img.height, Image_)
        bg = _apply_vignette(bg, Image_, ImageFilter, 0.35)
        img.paste(bg.convert("RGBA"), (img.width - right_w, 0))
    # 左侧深色面板
    img.paste(Image_.new("RGBA", (panel_w, img.height), _INK), (0, 0))
    # 面板右缘渐隐，和右图衔接
    fade = Image_.new("L", (220, img.height), 0)
    for fx in range(220):
        v = int(255 * (1 - fx / 220) ** 1.4)
        for fy in range(0, img.height, 8):
            fade.putpixel((fx, fy), v)
    fade = fade.filter(ImageFilter.GaussianBlur(6))
    edge = Image_.new("RGBA", (220, img.height), _INK[:3] + (0,))
    edge.putalpha(fade)
    img.alpha_composite(edge, (img.width - right_w - 160, 0))

    # 三张小海报面板内居中（不够三张按实际张数排）
    pw, ph = 180, 270
    gap = 26
    count = len(posters)
    total = count * pw + max(0, count - 1) * gap
    x0 = (panel_w - total) // 2
    for i, src in enumerate(posters):
        _poster_card(img, src, (x0 + i * (pw + gap), 620,
                                x0 + i * (pw + gap) + pw, 620 + ph),
                     Image_, ImageFilter, border_alpha=36)

    d = _ID.Draw(img)
    pad = 70
    kick_font = _font(34, bold=True)
    title_font = _font(132, bold=True) if title_text else None
    sub_font = _font(38) if sub_text else None
    if kick_font is not None:
        _spaced_text(d, (pad, 120), _BRAND_KICKER, kick_font, _ACCENT, spacing=10)
    if title_font is not None:
        d.text((pad, 190), title_text, font=title_font, fill=_TITLE_FILL)
        d.line([(pad, 380), (pad + 200, 380)], fill=(255, 255, 255, 60), width=2)
    if sub_font is not None:
        _spaced_text(d, (pad, 410), sub_text, sub_font, _SUB_FILL, spacing=8)


def _template_filmstrip(img, posters, _backdrop_src, title_text: str,
                        sub_text: str, pil) -> None:
    """暗房胶片：深灰底 + 顶部微光 + 海报等距陈列 + 居中标题"""
    Image_, ImageDraw, ImageFilter, _ImageFont = pil
    from PIL import ImageDraw as _ID

    img.paste(Image_.new("RGBA", img.size, (19, 22, 30, 255)), (0, 0))
    # 顶部微光：拒绝死黑
    glow = Image_.new("L", (1, img.height), 0)
    for y in range(img.height):
        glow.putpixel((0, y), int(26 * max(0, 1 - y / (img.height * 0.7))))
    sheen = Image_.new("RGBA", img.size, (70, 90, 130, 0))
    sheen.putalpha(glow.resize(img.size))
    img.alpha_composite(sheen)

    pw, ph = 300, 450
    gap = 44
    count = len(posters)
    total = count * pw + max(0, count - 1) * gap
    x = (img.width - total) // 2
    top = 170
    for src in posters:
        _poster_card(img, src, (x, top, x + pw, top + ph),
                     Image_, ImageFilter, border_alpha=30)
        x += pw + gap

    d = _ID.Draw(img)
    title_font = _font(92, bold=True) if title_text else None
    sub_font = _font(36) if sub_text else None
    if title_font is None and sub_font is None:
        return
    if title_font is not None:
        tw = d.textlength(title_text, font=title_font)
        d.text(((img.width - tw) / 2, 700), title_text,
               font=title_font, fill=(245, 246, 248, 255))
    if sub_font is not None:
        sw = sum(d.textlength(c, font=sub_font) + 10 for c in sub_text)
        _spaced_text(d, ((img.width - sw) / 2, 830), sub_text, sub_font,
                     (150, 158, 172, 255), spacing=10)
        cx = img.width / 2
        d.line([(cx - sw / 2 - 90, 848), (cx - sw / 2 - 30, 848)],
               fill=_ACCENT, width=3)
        d.line([(cx + sw / 2 + 30, 848), (cx + sw / 2 + 90, 848)],
               fill=_ACCENT, width=3)


def render_collage_bytes(
    posters: List[str],
    *,
    title_text: str = "",
    sub_text: str = "",
    backdrop: Optional[str] = None,
    template: str = DEFAULT_TEMPLATE,
    label: str = "",
) -> Optional[bytes]:
    """把已选好的海报拼成 1920×1080 WebP。

    库封面与合集封面共用这一段：选图各走各的（库按最近入库，合集按
    可见成员），拼图、字体回落、失败只记日志都不另起一套。``label``
    只进日志（如 ``媒体库「名字」``），拼不出图返回 None。
    """
    pil = _pil()
    if pil is None:
        return None
    Image_, _draw, ImageFilter, _font_mod = pil
    if template not in TEMPLATES:
        template = DEFAULT_TEMPLATE
    need = TEMPLATE_POSTER_COUNT if template in ("poster", "visual") \
        else TEMPLATE_FILMSTRIP_COUNT
    posters = [p for p in posters if p][:need]
    if not posters:
        logger.info("%s没有可用海报，跳过封面生成", label or "")
        return None
    if template in ("poster", "visual") and not backdrop:
        backdrop = posters[0]

    try:
        img = Image_.new("RGBA", (CANVAS_W, CANVAS_H), (12, 15, 24, 255))
        if template == "visual":
            _template_visual(img, posters, backdrop, title_text, sub_text, pil)
        elif template == "poster":
            _template_poster(img, posters, backdrop, title_text, sub_text, pil)
        else:
            _template_filmstrip(img, posters, backdrop, title_text, sub_text, pil)
        out = io.BytesIO()
        img.convert("RGB").save(out, format="WEBP", quality=86, method=5)
        return out.getvalue()
    except Exception:  # noqa: BLE001 - 封面是锦上添花，失败不该中断保存
        logger.warning("%s封面渲染失败", label or "", exc_info=True)
        return None


def render_cover_bytes(
    db,
    library,
    template: str = DEFAULT_TEMPLATE,
    title: str = "",
    subtitle: str = "",
) -> Optional[bytes]:
    """按模板渲染库封面并返回 WebP 字节；无法渲染时返回 None

    选图与渲染都在这里完成，调用方只需给 db + library（库名与内容类型
    从 library 自身取，避免两处传参对不上）。任何异常只记日志返回 None，
    调用方据此保留原封面。拼图本身见 :func:`render_collage_bytes`。
    """
    if template not in TEMPLATES:
        template = DEFAULT_TEMPLATE
    need = TEMPLATE_POSTER_COUNT if template in ("poster", "visual") \
        else TEMPLATE_FILMSTRIP_COUNT
    posters = pick_recent_posters(db, library, need)
    backdrop = None
    if posters and template in ("poster", "visual"):
        backdrop = pick_recent_backdrop(db, library) or posters[0]
    name = getattr(library, "name", "?")
    title_text = render_text(title, name, getattr(library, "collection_type", None) or "")
    sub_text = render_text(subtitle, name, getattr(library, "collection_type", None) or "")
    return render_collage_bytes(
        posters,
        title_text=title_text,
        sub_text=sub_text,
        backdrop=backdrop,
        template=template,
        label=f"媒体库「{name}」",
    )

