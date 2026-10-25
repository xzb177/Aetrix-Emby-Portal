"""外挂字幕的同名判定与语言识别（从 scanner.py 拆出）

扫描器判断「同目录里哪个 .srt 属于这个视频」这件事，本机路径与远程挂载（115 / WebDAV /
AList）走的是**同一套判定**——远程只有目录列表里的一串文件名，没有本机路径。
把它和发布标签正则一起放在这里，本机与远程两条路径共用一份实现。

与 `subtitles.py` 的分工：那里负责**投递**（外挂/内封字幕转 VTT 并缓存），
这里只负责**认出哪条字幕属于哪个视频、是哪种语言**。本模块是纯函数 + 正则，
不依赖扫描器的目录缓存、线程池等状态；需要本机目录列表的 `find_external_subtitles`
留在 scanner.py（它要用扫描期的目录缓存）。
"""
from __future__ import annotations

import os
import re

from backend.emby_server import mounts as mount_lib

SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}

# 语言标签 → Emby 三字码（客户端按这个选字幕轨）
_SUB_LANG_TAGS: tuple[tuple[str, str], ...] = (
    ("zh-cn", "chi"), ("zh-tw", "chi"), ("zh-hans", "chi"), ("zh-hant", "chi"),
    ("chs", "chi"), ("cht", "chi"), ("chi", "chi"), ("zho", "chi"), ("zh", "chi"),
    ("sc", "chi"), ("tc", "chi"), ("简", "chi"), ("繁", "chi"),
    ("中英", "chi"), ("双语", "chi"), ("中字", "chi"), ("中文", "chi"),
    ("eng", "eng"), ("english", "eng"), ("en", "eng"), ("英文", "eng"),
    ("jpn", "jpn"), ("japanese", "jpn"), ("jp", "jpn"), ("日", "jpn"),
    ("kor", "kor"), ("ko", "kor"), ("韩", "kor"),
)
# 发布标签：比较“是不是同一条媒体”时要先去掉
_RELEASE_TAG_RE = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|hdr10\+?|hdr|dolby|dv|web-?dl|webrip|web|bluray|"
    r"blu-ray|bdrip|brrip|remux|hdtv|dvdrip|x264|x265|h264|h265|hevc|avc|aac|ac3|eac3|"
    r"dts|dts-hd|flac|truehd|atmos|repack|proper|multi|internal|complete|10bit|8bit)\b",
    re.IGNORECASE,
)
_EP_MARK_RE = re.compile(
    r"([sS]\d{1,2}\s?[\s._-]*[eE]\d{1,3}|\d{1,2}x\d{1,3}|第\s*\d{1,3}\s*[集话話]|\b[eE][pP]\.?\s?\d{1,3}(?!\d))"
)


def _subtitle_core(text: str) -> str:
    """字幕/视频名的“核心”形式：去发布标签与分隔符，用于同名判定"""
    core = _RELEASE_TAG_RE.sub(" ", text or "")
    core = re.sub(r"[\.\-_\[\]()【】]+", " ", core)
    return re.sub(r"\s+", " ", core).strip().lower()


_RESOLUTION_RE = re.compile(
    r"(?<![A-Za-z0-9])(2160p|1080p|720p|480p|4k|uhd)(?![A-Za-z0-9])", re.IGNORECASE
)


def _resolution_of(text: str) -> str:
    """分辨率归一（4k/uhd 都算 2160p）——用于区分同一部片子的多个版本"""
    m = _RESOLUTION_RE.search(text or "")
    if not m:
        return ""
    value = m.group(1).lower()
    return "2160p" if value in ("4k", "uhd") else value


def _episode_key(text: str) -> str:
    m = _EP_MARK_RE.search(text or "")
    if not m:
        return ""
    return re.sub(r"[\s._-]+", "", m.group(1)).lower()


def subtitle_language(name: str) -> str:
    """从字幕文件名猜语言（返回 Emby 三字码）"""
    raw = name or ""
    low = raw.lower()
    for tag, lang in _SUB_LANG_TAGS:
        if not tag:
            continue
        if tag.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(tag)}(?![a-z0-9])", low):
                return lang
        elif tag in raw:
            return lang
    return "chi" if re.search(r"[\u4e00-\u9fff]", raw) else "eng"


def match_subtitle_names(base_name: str, names) -> list[str]:
    """从同目录文件名里挑出与 ``base_name`` 匹配的外挂字幕文件名

    覆盖实际会碰到的各种命名（旧实现只认「与视频完全同名」或「同名 + 点后缀」）：

    - 标准同名：`Show.S01E01.mkv` + `Show.S01E01.chi.srt`
    - 较短字幕名：`Show.S01E01.1080p.WEB-DL.mkv` + `Show.S01E01.ass`
    - 发行组差异：视频 `x265-GROUP`，字幕只写 `Show.S01E01.chs.ass`
    - 多版本媒体：`Show.S01E01.v2.mkv` / `Movie.2024.UHD.mkv` 各带自己的字幕
    - rclone / GD sidecar：`Show.S01E01.mkv.zh.srt`（字幕名以视频全名加点开头）

    不会把同目录里**别的集**的字幕认给本集（旧实现只比前缀）。
    单独抽出来是为了让**远程挂载**（115 / WebDAV / AList）也能用同一套判定：
    远程只有目录列表里的一串文件名，没有本机路径。
    """
    base_name = os.path.basename(base_name)
    stem = os.path.splitext(base_name)[0]
    video_core = _subtitle_core(stem)
    video_res = _resolution_of(stem)
    ep_key = _episode_key(stem)
    ep_head = _subtitle_core(stem[: _EP_MARK_RE.search(stem).start()]) if ep_key else ""

    def _same_media(name: str) -> bool:
        if name == base_name or name.startswith(base_name + "."):
            return True  # rclone/GD sidecar：字幕名 = 视频全名 + 语言后缀
        # 多版本：字幕自己标了分辨率时，必须与视频一致
        # （Movie.2024.2160p.srt 不应被认给 Movie.2024.1080p.mkv）
        sub_res = _resolution_of(name)
        if sub_res and video_res and sub_res != video_res:
            return False
        sub_core = _subtitle_core(name)
        if not sub_core or not video_core:
            return False
        if sub_core == video_core:
            return True
        # 去掉发布标签后互为前缀（字幕名更短、或多一个语言/版本后缀）
        short, long_ = sorted((sub_core, video_core), key=len)
        if short and long_.startswith(short):
            return True
        # 同一集号 + 同一剧名核心：Show.S01E01.ass ↔ Show.S01E01.1080p.WEB-DL.mkv
        if ep_key and _episode_key(name) == ep_key:
            mark = _EP_MARK_RE.search(name)
            head = _subtitle_core(name[: mark.start()]) if mark else ""
            if not ep_head or not head or head.startswith(ep_head) or ep_head.startswith(head):
                return True
        return False

    found: list[str] = []
    for f in names:
        base, ext = os.path.splitext(f)
        if ext.lower() not in SUBTITLE_EXTS:
            continue
        probe = base
        for _ in range(2):  # 去掉 sidecar 命名里残留的视频扩展名
            probe = re.sub(r"\.(mkv|mp4|avi|mov|ts|m2ts|m4v|wmv|flv|webm)$", "", probe, flags=re.IGNORECASE)
        if _same_media(probe):
            found.append(f)
    return found


def find_external_subtitles_in(names, file_path: str) -> list[tuple[str, str]]:
    """在已经拿到的目录列表里挑外挂字幕（同 find_local_images_in 的理由）"""
    d = os.path.dirname(file_path)
    return [
        (subtitle_language(os.path.splitext(f)[0]), os.path.join(d, f))
        for f in match_subtitle_names(os.path.basename(file_path), names)
    ]


def find_external_subtitles_remote(base_name: str, entries, mount_id: int,
                                   dir_rel: str, *,
                                   path_prefix: str = None) -> list[tuple[str, str]]:
    """远程挂载的外挂字幕：返回 [(lang, mount://<id>/<相对路径>)]。

    ``path_prefix`` 给了就按它拼路径（BYOS 绑定线传 ``byos://<绑定 id>``，
    匹配语义与挂载线同一份 ``match_subtitle_names``，只换路径命名空间，
    绝不产出 ``mount://None/...`` 这种假挂载路径）；缺省走现行挂载路径。
    """
    names = [e.name for e in entries if not e.is_dir]
    base_dir = "/" + (dir_rel or "/").strip("/")
    out = []
    for f in match_subtitle_names(base_name, names):
        sub_rel = f"{base_dir.rstrip('/')}/{f}"
        if path_prefix is not None:
            path = f"{path_prefix.rstrip('/')}{sub_rel}"
        else:
            path = mount_lib.mount_path(mount_id, sub_rel)
        out.append((subtitle_language(os.path.splitext(f)[0]), path))
    return out
