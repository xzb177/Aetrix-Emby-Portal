"""自建 Emby 服务器：字幕投递

Emby 客户端取字幕走
``GET /Videos/{Id}/{MediaSourceId}/Subtitles/{Index}/Stream.{Format}``，
本模块负责把它落到本地：外挂字幕直接读取（含中文常见 GBK 编码嗅探），
内封文本字幕用 ffmpeg 抽取为 WebVTT 并按「条目 + 轨道」缓存。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import uuid
from typing import Optional

from fastapi import HTTPException, Response

logger = logging.getLogger(__name__)

# 注意：这里的 FFMPEG 只用于内封字幕轨抽取为 WebVTT（非视频转码），
# 转码删除后仍保留；字幕烧录（把字幕烧进视频流）本服务从未实现。
FFMPEG = os.getenv("EMBY_FFMPEG_PATH", "ffmpeg")
SUB_CACHE_DIR = os.getenv(
    "EMBY_SUBTITLE_DIR",
    os.path.join(os.getenv("EMBY_CACHE_DIR", "/tmp/aetrix_cache"), "subs"),
)

# 文本类字幕编码名（可转换为 VTT）；图片类（PGS/VobSub）无法转文本
TEXT_SUB_CODECS = {
    "subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text", "vtt", "smi", "sami", "ttml",
}
IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "pgssub", "dvd_subtitle", "dvdsub", "dvb_subtitle", "xsub"}

TEXT_EXTS = {".srt", ".vtt", ".ass", ".ssa", ".sub", ".smi", ".sami", ".ttml", ".txt"}
# 源文件扩展名 → 客户端可直接按该格式取原始文件
RAW_OK = {
    "srt": {".srt"},
    "vtt": {".vtt"},
    "ass": {".ass"},
    "ssa": {".ssa"},
    "sub": {".sub"},
    "smi": {".smi", ".sami"},
}
MIME = {
    "vtt": "text/vtt; charset=utf-8",
    "srt": "application/x-subrip; charset=utf-8",
    "ass": "text/x-ssa; charset=utf-8",
    "ssa": "text/x-ssa; charset=utf-8",
}
DEFAULT_MIME = "text/plain; charset=utf-8"

_SRT_TS = re.compile(r"(\d{2}:\d{2}:\d{2}),(\d{3})")
_VTT_TS = re.compile(r"(\d{2}:\d{2}:\d{2})\.(\d{3})")
_ASS_TAG = re.compile(r"\{[^}]*\}")
_ASS_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[.:](\d{1,3})")
_ASS_SKIP = (
    "format:", "title:", "scripttype:", "wrapstyle:", "scaledborderandshadow:",
    "collisions:", "playres", "yscrolledby:", "timer:", "synctime",
)


def decode_text(raw: bytes) -> str:
    """按常见中文编码顺序尝试解码（UTF-8 → GB18030/Big5 → 兜底忽略错误）"""
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5", "utf-16"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="ignore")


def srt_to_vtt(text: str) -> str:
    body = _SRT_TS.sub(lambda m: f"{m.group(1)}.{m.group(2)}", text.replace("\r\n", "\n"))
    return "WEBVTT\n\n" + body.lstrip("\ufeff").strip() + "\n"


def vtt_to_srt(text: str) -> str:
    body = text.replace("\r\n", "\n")
    if body.lstrip().startswith("WEBVTT"):
        body = body.lstrip()[len("WEBVTT"):].lstrip("\n")
    return _VTT_TS.sub(lambda m: f"{m.group(1)},{m.group(2)}", body).strip() + "\n"


def ass_to_vtt(text: str) -> str:
    """ASS/SSA 事件行 → WebVTT（丢掉样式标签与特效，保留时间轴与文本）"""
    out = ["WEBVTT", ""]
    in_events = False
    for raw_line in text.replace("\r\n", "\n").split("\n"):
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_events = line.lower() == "[events]"
            continue
        if not in_events or not line or line.lower().startswith(_ASS_SKIP):
            continue
        # Dialogue: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
        parts = line.split(",", 9)
        if len(parts) < 10 or not parts[0].lower().startswith("dialogue"):
            continue

        def _ts(value: str) -> Optional[str]:
            m = _ASS_TIME.fullmatch(value.strip())
            if not m:
                return None
            h, mnt, sec, frac = m.groups()
            return f"{int(h):02d}:{mnt}:{sec}.{int(frac.ljust(3, '0')[:3]):03d}"

        start, end = _ts(parts[1]), _ts(parts[2])
        if not start or not end:
            continue
        body = _ASS_TAG.sub("", parts[9]).replace("\\N", "\n").replace("\\n", "\n").strip()
        if body:
            out += [f"{start} --> {end}", body, ""]
    return "\n".join(out)


def to_vtt(text: str) -> str:
    """把任意文本字幕规范化为 WebVTT"""
    stripped = text.lstrip("\ufeff \t\r\n")
    if stripped.startswith("WEBVTT"):
        return text
    if "[Script Info]" in text or _ASS_TIME.search(text):
        return ass_to_vtt(text)
    return srt_to_vtt(text)


def is_text_track(codec: Optional[str]) -> bool:
    codec = (codec or "").lower()
    if codec in IMAGE_SUB_CODECS:
        return False
    return codec in TEXT_SUB_CODECS or codec == ""


def _cache_path(item_guid: str, stream_index: int) -> str:
    return os.path.join(SUB_CACHE_DIR, f"{item_guid}_{stream_index}.vtt")


# ==================== 挂载来源解析钩子 ====================
# 条目的 file_path 可能是存储挂载的虚拟路径（mount://<挂载 id>/<相对路径>）。
# 本模块只认路径字符串，因此由 mounts 模块注册一个解析器，把虚拟路径换成
# 「直链 + 鉴权头」；字幕端点不需要认识挂载，也不需要碰 Cookie / 令牌。
_MOUNT_RESOLVER = None


def register_mount_resolver(resolver) -> None:
    """注册挂载解析器：``callable(path) -> (url, headers) | None``（非挂载路径返回 None）"""
    global _MOUNT_RESOLVER
    _MOUNT_RESOLVER = resolver


def resolve_mount_source(path: Optional[str]):
    if not path or _MOUNT_RESOLVER is None:
        return None
    try:
        return _MOUNT_RESOLVER(path)
    except Exception as exc:  # noqa: BLE001 — 解析失败不应变成 500，交给上层 404
        logger.warning("挂载来源解析失败 %s: %s", path, exc)
        return None


def is_remote_source(path: Optional[str]) -> bool:
    """是否为远程直链（挂载来源）：ffmpeg 能直接读 http(s)，但不能按本地文件检查"""
    return bool(path) and str(path).startswith(("http://", "https://"))


def fetch_remote_bytes(url: str, headers: Optional[dict] = None, timeout: float = 30.0) -> bytes:
    """取远程小文件（外挂字幕）；失败给 404 / 504，与本地文件缺失的语义一致"""
    import httpx

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers=headers or {})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=504, detail=f"远程字幕取回失败: {exc}") from exc
    if resp.status_code >= 400:
        raise HTTPException(status_code=404, detail=f"远程字幕取不到（HTTP {resp.status_code}）")
    return resp.content


def extract_embedded(media_path: str, item_guid: str, stream_index: int, subtitle_ordinal: int,
                     input_headers: Optional[dict] = None) -> str:
    """用 ffmpeg 把内封字幕轨抽取为 WebVTT，结果按条目+轨道缓存到磁盘

    ``media_path`` 可以是本机文件，也可以是远程直链（挂载来源），后者凭据经
    ``input_headers`` 传入。
    """
    cached = _cache_path(item_guid, stream_index)
    if os.path.isfile(cached) and os.path.getsize(cached) > 0:
        return cached
    # 挂载来源：把 mount://<id>/<rel> 换成真实直链（凭据只在本机使用）
    resolved = resolve_mount_source(media_path)
    if resolved:
        media_path, input_headers = resolved
    if not media_path or not (is_remote_source(media_path) or os.path.isfile(media_path)):
        raise HTTPException(status_code=404, detail="Media file not found")
    if not shutil.which(FFMPEG):
        raise HTTPException(status_code=503, detail="服务器未安装 ffmpeg，无法抽取内封字幕")
    os.makedirs(SUB_CACHE_DIR, exist_ok=True)
    # A-121：tmp 名必须每路唯一（pid+uuid）。固定名下同轨并发首抽时，
    # 先完成者 os.replace 后共享 tmp 路径消失，后完成者误报 404；且败者
    # 持有已发布 inode 的写 fd 可续写已发布缓存。进程内锁不够
    # （多进程部署下仍撞），故唯一后缀是必选项。
    tmp = cached + f".{os.getpid()}.{uuid.uuid4().hex}.part"
    head: list = []
    if input_headers:
        head = ["-headers", "".join(f"{k}: {v}\r\n" for k, v in input_headers.items())]
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        *head, "-i", media_path, "-map", f"0:s:{subtitle_ordinal}", "-c:s", "webvtt", tmp,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="字幕抽取超时")
    def _peer_published() -> bool:
        try:
            return os.path.isfile(cached) and os.path.getsize(cached) > 0
        except OSError:
            return False

    if proc.returncode != 0 or not os.path.isfile(tmp) or os.path.getsize(tmp) == 0:
        if os.path.isfile(tmp):
            os.remove(tmp)
        # 同轨并发下另一路可能已发布缓存：回缓存，不再误报 404。
        if _peer_published():
            return cached
        raise HTTPException(status_code=404, detail="该字幕轨无法抽取为文本字幕")
    try:
        os.replace(tmp, cached)
    except OSError:
        if _peer_published():
            return cached
        raise HTTPException(status_code=404, detail="该字幕轨无法抽取为文本字幕")
    logger.info("内封字幕已抽取 %s 轨道 %s", item_guid, stream_index)
    return cached


def render_external_text(text: str, ext: str, fmt: str) -> Response:
    """投递外挂字幕文本（本机读取与远程挂载读取共用同一套格式处理）"""
    ext = (ext or "").lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    if ext not in TEXT_EXTS:
        raise HTTPException(status_code=415, detail="该外挂字幕不是文本字幕")
    # 源格式与请求格式一致时原样返回，避免无意义转换
    if ext in RAW_OK.get(fmt, set()):
        return Response(content=text, media_type=MIME.get(fmt, DEFAULT_MIME))
    vtt = to_vtt(text)
    if fmt == "vtt":
        return Response(content=vtt, media_type=MIME["vtt"])
    if fmt == "srt":
        return Response(content=vtt_to_srt(vtt), media_type=MIME["srt"])
    return Response(content=vtt, media_type=DEFAULT_MIME)


def render_external_source(path: str, fmt: str, external_text: Optional[str] = None,
                           external_ext: str = "") -> Response:
    """外挂字幕三种来源：本机文件 / 调用方取好的文本 / 挂载来源（自己取回）"""
    ext = external_ext or os.path.splitext(path)[1]
    if os.path.isfile(path):
        with open(path, "rb") as f:
            return render_external_text(decode_text(f.read()), ext, fmt)
    if external_text is not None:
        return render_external_text(external_text, ext, fmt)
    resolved = resolve_mount_source(path)
    if resolved:
        url, headers = resolved
        return render_external_text(decode_text(fetch_remote_bytes(url, headers)), ext, fmt)
    raise HTTPException(status_code=404, detail="Subtitle file not found")


def render_subtitle(
    *,
    item_guid: str,
    media_path: Optional[str],
    stream,
    fmt: str,
    subtitle_ordinal: int,
    external_text: Optional[str] = None,
    external_ext: str = "",
    media_headers: Optional[dict] = None,
) -> Response:
    """产出字幕响应体

    stream: em.MediaStream 行（需有 codec / is_external / external_path / stream_index）

    外挂字幕有两种来源：本机文件（``external_path`` 是本机路径）与远程挂载
    （``external_path`` 是 ``mount://…``，由调用方取回文本后通过 ``external_text`` 传入）。
    """
    fmt = (fmt or "vtt").lower().lstrip(".")
    if fmt not in RAW_OK:
        fmt = "vtt"

    external = (stream.external_path or "").strip() if stream.is_external else ""
    if external:
        return render_external_source(external, fmt, external_text, external_ext)

    if not is_text_track(stream.codec):
        raise HTTPException(status_code=415, detail="图片类字幕（PGS/VobSub）不支持直接投递")
    path = extract_embedded(
        media_path or "", item_guid, stream.stream_index, subtitle_ordinal, media_headers,
    )
    with open(path, "rb") as f:
        vtt = decode_text(f.read())
    if fmt == "srt":
        return Response(content=vtt_to_srt(vtt), media_type=MIME["srt"])
    if fmt == "vtt":
        return Response(content=vtt, media_type=MIME["vtt"])
    return Response(content=vtt, media_type=DEFAULT_MIME)
