"""存储挂载：把不同来源的内容接到媒体库上

一个**挂载**就是一种「把内容接进媒体库」的方式。挂载本身不拥有条目，媒体库通过
``Library.mount_ids`` 引用它，所以同一个挂载可以被多个库共用。

**只支持四种来源**（v2.42.12 起，其余类型连同代码一并删除，不再维护；
``drive`` 是之后按同一套提供者接口新加的 API 直调实现，不是恢复当年下线的
Google Drive 原生类型）：

============================  ====================================================
``local``                     本机硬盘。路径以 ``/media`` 开头（本机真实目录）。
``115``                       115 网盘直挂。路径以 ``115:/`` 开头，账号用 Cookie
                              配置档（``pan115_accounts`` 表）。
``rclone``                    rclone 任意后端。路径以 ``rclone:`` 开头，形如
                              ``rclone:gdrive/Movies``；rclone.conf 由用户自己粘贴，
                              落盘到 ``data/rclone/rclone.conf``，调用时 ``--config``
                              指过去。
``drive``                     Google 云盘 API 直读。路径以 ``drive:`` 开头，后接
                              共享盘 ID（个人盘 ``drive:myDrive:<remote 名>``）；
                              列目录走 drive_changes 的服务账号轮换 + 全局限流，
                              不经 rclone、不绑单个账号。实现在 ``mount_drive.py``。
============================  ====================================================

**路径前缀即类型**（``detect_mount_type``）：表单只填一条路径，前缀决定它是哪种来源，
和「先选类型再填一堆字段」是两种交互。这样做是因为这几种来源的配置本来就只有一条路径
外加少量可选项，多一套类型下拉只会让人有机会选出不匹配的组合。

类型元数据是**唯一事实来源**：后台下拉、表单字段、必填校验、密钥脱敏都读同一份
``MOUNT_TYPES``，前端不自己维护一份。

约定：

- **本机可读的挂载**（``local``）条目仍然存真实文件路径，探测、图片、外挂字幕都走本机。
  目录里的 ``.strm`` 小文件照样认（内容是播放直链），不需要单独的 STRM 类型。
- **远程挂载**（``115`` / ``rclone`` / ``drive``）的条目路径形如 ``mount://<挂载 id>/<相对路径>``，
  播放时由提供者解析成真实 URL，再由 EA 按 Range **代理转发**：Cookie / 令牌不出服务器，
  客户端拿到的仍然是本服务器的地址。带 ``drive_file_id`` 的条目（``drive`` 挂载全部、
  rclone 的 Drive 后端）例外：api 层直接 302 到 Drive 直链，不经代理转发。
- 解析结果同时用于**扫描探测**（ffprobe 直接读 URL）与**播放**，不会出现「能扫到但播不了」。
"""
from __future__ import annotations

import contextlib
import functools
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Iterator, Optional

from sqlalchemy.orm import Session

from backend.emby_server import disc_filter
from backend.emby_server import models as em
from backend.emby_server.cpu_budget import capped_workers as _capped_workers
# 远程 IO 计数与「扫描会话」标记（只依赖标准库，不会形成循环导入）
from backend.emby_server import scan_progress as progress
from backend.emby_server.playback_security import (
    validate_local_file_path, validate_remote_url, safe_local_path,
)
from backend.emby_server.env_util import env_float, env_int
from backend.emby_server import subtitles, transfer115

logger = logging.getLogger(__name__)

MOUNT_LOCAL = "local"
MOUNT_PAN115 = "115"
MOUNT_RCLONE = "rclone"
MOUNT_DRIVE = "drive"

#: 路径前缀 → 挂载类型。表单只填一条路径，类型由前缀决定（``detect_mount_type``）。
PATH_TYPE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("115:/", MOUNT_PAN115),
    ("rclone:", MOUNT_RCLONE),
    ("drive:", MOUNT_DRIVE),
    ("/media", MOUNT_LOCAL),
)

#: 路径没带任何已知前缀时按什么处理。约定是 /media（容器里媒体盘的挂载点），但
#: 裸机部署的媒体常在 /mnt/media、/srv/media 这类地方，所以**绝对路径一律当本地硬盘**——
#: 把它判成「未知类型」只会让一台装得好好的机器建不出挂载。
DEFAULT_PATH_TYPE = MOUNT_LOCAL

# 挂载类型元数据：后台下拉与「这类挂载要填什么」的说明都来自这里
#
# 每个类型：
#   value / label / hint   —— 展示
#   kind                   —— local（本机可读）| remote（条目存 mount://，播放时代理）
#   group                  —— 后台图标分组：local / cloud / gateway
#   needs_path             —— 是否要填本机目录（走 Library.paths 那一套）
#   browse                 —— 是否支持后台目录浏览（远程类型为 True）
#   root_key               —— 可写回表单的「根目录标识」字段名（浏览时点目录即写入）
#   fields[]               —— 配置字段：key / label / placeholder / secret / required / type
#                             type=account115 表示渲染成 115 账号下拉；type=select 配合 options
MOUNT_TYPES: list[dict] = [
    {
        "value": MOUNT_LOCAL,
        "label": "本地硬盘",
        "kind": "local",
        "group": "local",
        "hint": "服务器本机目录，任意绝对路径都行（/media 只是约定，裸机常在 /mnt/media）。"
                "目录里的 .strm 小文件照样认。",
        "needs_path": True,
        "browse": True,
        "root_key": "",
        "fields": [],
    },
    {
        "value": MOUNT_PAN115,
        "label": "115 网盘",
        "kind": "remote",
        "group": "cloud",
        "hint": "Cookie 型 API 直读网盘，不用挂到本机。路径以 115:/ 开头；"
                "目录 ID 填 0 表示根目录，可先浏览再选。",
        "needs_path": False,
        "browse": True,
        "root_key": "cid",
        "fields": [
            {"key": "cid", "label": "目录 ID", "placeholder": "0 = 根目录"},
            {"key": "account_id", "label": "115 账号（留空用默认账号）", "type": "account115"},
        ],
    },
    {
        "value": MOUNT_RCLONE,
        "label": "rclone",
        "kind": "remote",
        "group": "gateway",
        "hint": "路径以 rclone: 开头，例如 rclone:gdrive/Movies。"
                "rclone.conf 在「rclone 配置」页粘贴，本面板只负责用它跑 rclone 命令。",
        "needs_path": False,
        "browse": True,
        "root_key": "fs",
        "remotes": True,
        "fields": [
            {
                "key": "mode", "label": "调用方式", "type": "select",
                "options": [{"label": "RC API（需你自己跑 rclone rcd --rc-serve）", "value": "rc"},
                            {"label": "直接调用 rclone 命令（用粘贴的 rclone.conf）", "value": "cli"}],
            },
            {"key": "rc_url", "label": "RC 地址", "placeholder": "http://127.0.0.1:5572"},
            {"key": "rc_user", "label": "RC 用户名（可选）"},
            {"key": "rc_pass", "label": "RC 密码（可选）", "secret": True},
            {"key": "serve_url", "label": "取流地址（可选）",
             "placeholder": "http://127.0.0.1:8080",
             "hint": "留空则用 RC 地址取流；填了则媒体字节走该端点（rclone serve http + VFS 缓存）。"},
            {"key": "rclone_bin", "label": "rclone 路径（可选）", "placeholder": "默认从 PATH 找 rclone"},
            {"key": "rclone_config", "label": "rclone.conf 路径（可选）",
             "placeholder": "默认 data/rclone/rclone.conf"},
        ],
    },
    {
        "value": MOUNT_DRIVE,
        "label": "Google 云盘",
        "kind": "remote",
        "group": "cloud",
        "hint": "Drive API 直读，不经 rclone、不绑单个账号。路径以 drive: 开头，后接"
                "共享盘 ID，例如 drive:0AER…；个人盘写 drive:myDrive:<rclone remote 名>。"
                "列目录走 100 个服务账号轮换 + 全局限流（个人盘用它自己的 OAuth）。",
        "needs_path": False,
        "browse": True,
        "root_key": "",
        "fields": [
            {"key": "root_id", "label": "根目录 ID（可选）",
             "placeholder": "留空 = 盘根目录；填了就把挂载根钉在这个文件夹"},
        ],
    },
]


def detect_mount_type(path: str) -> str:
    """按路径判断挂载类型（认不出来时返回空串，让调用方明确报错）

    顺序重要：``115:/`` 与 ``rclone:`` 都是「前缀 + 冒号」，必须先比字面量更具体的，
    否则 ``115:/x`` 会被当成 rclone 的 ``115`` remote。

    判不出来时：**绝对路径当本地硬盘**（见 ``DEFAULT_PATH_TYPE``）；其余（``s3://x``、
    漏了 ``rclone:`` 前缀的 ``gdrive:Movies``、空路径）一律返回空串——宁可报错，
    也别静默存成一种错的来源。
    """
    raw = (path or "").strip()
    for prefix, mount_type in PATH_TYPE_PREFIXES:
        if raw.startswith(prefix):
            return mount_type
    if raw.startswith(("/", "./", "../", "~")):
        return DEFAULT_PATH_TYPE
    return ""

MOUNT_TYPE_LABELS = {t["value"]: t["label"] for t in MOUNT_TYPES}
MOUNT_TYPE_MAP = {t["value"]: t for t in MOUNT_TYPES}

# 与类型元数据无关的通用密钥键（兼容老配置与云盘令牌）
LEGACY_SECRET_KEYS = {"password", "token", "cookie", "access_token"}


def register_mount_types(entries) -> None:
    """注册挂载类型（扩展模块调用）：追加元数据并刷新两张查找表"""
    global MOUNT_TYPE_LABELS, MOUNT_TYPE_MAP
    for entry in entries or ():
        if any(t["value"] == entry["value"] for t in MOUNT_TYPES):
            continue
        MOUNT_TYPES.append(dict(entry))
    MOUNT_TYPE_LABELS = {t["value"]: t["label"] for t in MOUNT_TYPES}
    MOUNT_TYPE_MAP = {t["value"]: t for t in MOUNT_TYPES}


def register_providers(mapping) -> None:
    """注册挂载提供者（扩展模块调用）"""
    _PROVIDERS.update(mapping or {})


def type_meta(mount_type: str) -> dict:
    return MOUNT_TYPE_MAP.get((mount_type or "").strip(), {})


def supports_browse(mount_type: str) -> bool:
    return bool(type_meta(mount_type).get("browse"))


def root_key(mount_type: str) -> str:
    """该类型的「根目录标识」字段名（空字符串表示没有这个概念）"""
    return type_meta(mount_type).get("root_key") or ""


def required_fields(mount_type: str) -> list[dict]:
    return [f for f in type_meta(mount_type).get("fields", []) if f.get("required")]


def secret_config_keys() -> set[str]:
    """不上报明文的配置键：历史密钥键 + 类型元数据里所有 secret 字段"""
    keys = set(LEGACY_SECRET_KEYS)
    for t in MOUNT_TYPES:
        for field in t.get("fields", ()):
            if field.get("secret"):
                keys.add(field["key"])
    return keys

# 远程挂载识别到的媒体扩展名（与 scanner.VIDEO_EXTS 对齐，外加 strm）
REMOTE_MEDIA_EXTS = {
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m2ts", ".ts", ".m4v", ".strm",
    ".iso",
}
STRM_EXT = ".strm"

REMOTE_SUBTITLE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}

MOUNT_PATH_PREFIX = "mount://"

# 播放代理 / 探测共用：远程请求超时（秒）
MOUNT_TIMEOUT = env_float("MOUNT_TIMEOUT", 20.0)
# ffprobe / ffmpeg 读远程源时的 UA（多数网盘直链对 UA 有要求）
MOUNT_UA = os.getenv("MOUNT_UA", transfer115.PAN115_UA)

#: 115 配置档可选的 UA（后台下拉用）。115 的直链接口对 UA 有偏好，不同档可以指不同设备；
#: 空值 = 用服务器级 MOUNT_UA。**由后端下发**，前端不自己维护一份。
UA_PRESETS: tuple[dict[str, str], ...] = (
    {"value": "", "label": "跟随服务器默认（MOUNT_UA）"},
    {"value": transfer115.PAN115_UA, "label": "Chrome / Windows"},
    {"value": ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"),
     "label": "iPhone / Safari"},
    {"value": ("Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36"),
     "label": "Android / Chrome"},
    {"value": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
     "label": "Firefox / Windows"},
    {"value": "Lufia/2.2", "label": "115 官方 App（部分接口需要）"},
)
MOUNT_RANGE_CHUNK = 1024 * 256
# 存储读不动时，补全条目打回 pending 后等多久再试（不是 failed，attempts 不涨）。
# 原本这个值叫 MOUNT_BREAKER_RETRY_SEC、绑在熔断器上；熔断器删掉后它讲的是自己的
# 事情（避免一个挂不上的网盘把队列烧成 failed），所以改名而不是跟着一起删。
MOUNT_UNAVAILABLE_RETRY_SEC = env_int("MOUNT_UNAVAILABLE_RETRY_SEC", 1800, 60)


class MountError(RuntimeError):
    """挂载不可用（配置错误、网络错误、路径不存在）"""


class MountMethodMissing(MountError):
    """rclone RC **没有这个方法**。

    必须与「目录/文件不存在」分开：rclone 对两者都返 HTTP 404，只有响应体里的
    ``error`` 字段能区分（``couldn't find method "x"`` vs ``directory not found``）。
    以前一律当 404 = 方法缺失，于是配错目录的人看到的是「该 rclone 版本不提供
    这个接口」——完全指错方向。
    """


class MountAuthError(MountError):
    """挂载的凭据失效（Cookie / 令牌 / 密码）——调用方应提示重新配置"""


@dataclass
class MountFile:
    """挂载里枚举到的一个媒体文件"""

    rel: str           # 相对挂载根，以 "/" 开头
    name: str
    size: int = 0
    is_strm: bool = False
    # 远端文件稳定 ID（Drive file_id）：改名/移动不变
    # 扫描器用它做文件身份，改名不丢元数据。
    file_id: str = ""
    #: 远端最后修改时间（Unix 秒；拿不到就是 0.0）。rclone 的 list_dir 会填
    #: ModTime 解析值；扫描器把它混入文件指纹，同尺寸内容替换也能被增量扫描发现
    #: （完美审查 C-P2-3）。
    mod_ts: float = 0.0


@dataclass
class MountEntry:
    """目录项（供后台浏览选择）

    ``entry_id`` 是提供者眼里的目录标识（115 的 cid）；本机 / WebDAV / AList 不需要它。
    """

    name: str
    rel: str
    is_dir: bool
    size: int = 0
    entry_id: str = ""
    #: 文件稳定 ID（Drive file_id）：目录为空串。list_dir 透出给扫描器。
    file_id: str = ""
    #: 远端最后修改时间（Unix 秒；拿不到就是 0.0）。追新靠它做「新增」窗口过滤——
    #: 之前追新为此绕开公共通道直接读 rclone 原始 JSON 的 ModTime，等于自带一条
    #: 无限流的旁路（2026-10 rclone 请求风暴）。带在条目上，公共通道就能直接复用。
    mod_ts: float = 0.0


@dataclass
class PlayTarget:
    """媒体可播放目标

    - ``kind="local"``：本机文件，调用方直接按文件读；
    - ``kind="url"``：远程地址，调用方用 ``headers`` 代理转发（不要下发给客户端）。

    **没有「客户端直连地址」这种形态**：曾经有个可选的 ``direct`` 字段给
    302 直链用（拼一条带 access_token 的 Google 地址），现已删除——Drive 的
    ``alt=media`` 要 Authorization 头而重定向带不过去，token 放 URL 又会被限流，
    所以播放只有「服务端代理转发」这一种走法。
    """

    kind: str
    value: str
    headers: dict = field(default_factory=dict)


# ==================== 路径约定 ====================

def mount_path(mount_id: int, rel: str) -> str:
    """构造远程挂载条目的入库路径"""
    clean = "/" + (rel or "").lstrip("/")
    return f"{MOUNT_PATH_PREFIX}{mount_id}{clean}"


def is_mount_path(path: Optional[str]) -> bool:
    return bool(path) and str(path).startswith(MOUNT_PATH_PREFIX)


def parse_mount_path(path: Optional[str]) -> Optional[tuple[int, str]]:
    """``mount://3/Movies/a.mkv`` → ``(3, "/Movies/a.mkv")``"""
    if not is_mount_path(path):
        return None
    rest = str(path)[len(MOUNT_PATH_PREFIX):]
    mount_id, _, rel = rest.partition("/")
    if not mount_id.isdigit():
        return None
    return int(mount_id), ("/" + rel if rel else "/")


def parse_config(mount) -> dict:
    """读取挂载的 JSON 配置（坏数据不抛异常，退化成空配置）"""
    raw = getattr(mount, "config", "") or "{}"
    if isinstance(raw, dict):
        return dict(raw)
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — 配置坏了按空处理，由 test() 报可读错误
        return {}


def dump_config(config: dict) -> str:
    return json.dumps(config or {}, ensure_ascii=False, separators=(",", ":"))


def mount_label(mount) -> str:
    """``115 影库（115 网盘直挂）`` 这样的展示名"""
    kind = MOUNT_TYPE_LABELS.get(getattr(mount, "mount_type", ""), getattr(mount, "mount_type", ""))
    return f"{getattr(mount, 'name', '挂载')}（{kind}）"


def _is_strm_name(name: Optional[str]) -> bool:
    """文件名是否为 ``.strm``（容忍 None：条目可能没有本地路径）"""
    return bool(name) and str(name).lower().endswith(STRM_EXT)


def strm_url(content: str) -> str:
    """从 .strm 文件内容里取直链（容忍 BOM、空行、注释与前后空白）"""
    for line in (content or "").replace("\ufeff", "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", line):
            return line
    return ""


def strm_container(url: str) -> str:
    """直链里能看出真实容器就用它，看不出来交给客户端按 strm 处理"""
    try:
        suffix = os.path.splitext(urllib.parse.urlparse(url).path)[1].lstrip(".").lower()
    except Exception:  # noqa: BLE001
        suffix = ""
    return suffix if suffix in {e.lstrip(".") for e in REMOTE_MEDIA_EXTS} else "strm"


def _strm_play_target(content: str, path_hint: str) -> PlayTarget:
    """服务端 .strm 内容 → PlayTarget（local_play_target 与 resolve_final 共用）。

    纸条签名（strm_sign）已随 strm_gen 删除：不再验签，直接用文件内容里的
    原文直链（Google 会忽略多余的 aexp/asig 参数），残留纸条行继续可播。
    - Drive 直链自动附加 SA Bearer 头（非 Drive / 拿不到 token 时为空 dict，不阻断）
    """
    url = strm_url(content)
    if not url:
        raise MountError(f"STRM 文件里没有可用的直链: {path_hint}")
    target_url = url
    from backend.emby_server import drive_auth
    headers = {"User-Agent": MOUNT_UA, **drive_auth.drive_auth_headers(target_url)}
    return PlayTarget("url", validate_remote_url(target_url), headers)


def local_play_target(path: str) -> PlayTarget:
    """本机文件的播放目标；``.strm`` 读内容当直链（STRM 无需挂载也能播）"""
    try:
        path = validate_local_file_path(path)
    except ValueError as exc:
        raise MountError(str(exc)) from exc
    if _is_strm_name(path):
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except OSError as exc:
            raise MountError(f"读取 STRM 文件失败: {path} ({exc})") from exc
        return _strm_play_target(content, path)
    return PlayTarget("local", path)


# ==================== 提供者（按挂载类型解析）====================

# ==================== 目录列举缓存（扫描与播放共用）====================
# 远程挂载的每一次列目录都是一次网络往返。扫描方向本来就有「本次扫描内单飞」的缓存，
# 但**播放**方向完全裸奔，而且 ``resolve_play_target`` 是**每次请求新建一个提供者**：
# 提供者实例上的 cid/pickcode 缓存对播放根本不存在，于是一次播放要重新逐级列目录
# （115 的 ``/电影/2024/x.mkv`` 就是 3 次网盘往返），多个人同时播就是几十次。
#
# 所以把缓存**下沉到提供者层**，按（挂载、类型指纹、目录）做 TTL 缓存，扫描与播放共用：
#   MOUNT_LIST_CACHE_SECONDS  目录列举缓存有效期（0 = 关闭，默认 30）
#   MOUNT_LIST_CACHE_MAX      最多缓存多少个目录（满了整体清空，内存有上限）
# 失败**不缓存**（网盘抖动不该被固化 TTL 秒），返回值是**拷贝**（调用方会排序/裁剪，
# 共享同一个 list 对象会让它们互相污染）。
MOUNT_LIST_CACHE_SECONDS = env_float("MOUNT_LIST_CACHE_SECONDS", 30.0, 0.0)
MOUNT_LIST_CACHE_MAX = env_int("MOUNT_LIST_CACHE_MAX", 2000, 50)
# 远程并发上限（v2.27.0）：同时最多几个远程请求在飞。WebDAV / rclone / 网盘代理在高并发下
# 并不会更快——它们要么排队、要么限流，四个库同时扫一个端点时延迟被放大到十几倍。
# 本机目录（local / strm / rclone 最终落到本机路径的用法）不吃这个名额。
MOUNT_REMOTE_CONCURRENCY = env_int("MOUNT_REMOTE_CONCURRENCY", 2, 1, 16)
# 追新（chase_new）的**独立**远程名额（2026-10 rclone 请求风暴修复）：
# 追新原来绕开本模块直接调 rc_call，无上限地把请求打向 rcd——生产 24 小时 5.2 万条
# 报错（context canceled 2.6 万 / connection reset 1.9 万）就是这么来的。改成走公共
# 通道后它会和扫描抢同一批名额，而扫描是主任务、追新只是低频后台任务，所以给它一个
# **更小的独立名额**：追新慢一点没关系，绝不能把扫描饿着（反过来也一样）。
CHASE_REMOTE_CONCURRENCY = env_int("CHASE_REMOTE_CONCURRENCY", 1, 1, 8)

_LIST_CACHE: dict = {}
_LIST_LOCKS: dict = {}
_LIST_CACHE_LOCK = threading.Lock()
_REMOTE_SEM: Optional[threading.BoundedSemaphore] = None
_REMOTE_SEM_SIZE = MOUNT_REMOTE_CONCURRENCY
_CHASE_SEM: Optional[threading.BoundedSemaphore] = None
_CHASE_SEM_SIZE = CHASE_REMOTE_CONCURRENCY
_REMOTE_SEM_LOCK = threading.Lock()
_LIST_CACHE_STATS = {"hits": 0, "misses": 0, "expired": 0, "evictions": 0, "waits": 0}


def list_cache_stats() -> dict:
    """缓存命中情况（健康检查 / 测试用）"""
    with _LIST_CACHE_LOCK:
        return {**_LIST_CACHE_STATS, "entries": len(_LIST_CACHE),
                "ttl_seconds": MOUNT_LIST_CACHE_SECONDS,
                "remote_concurrency": MOUNT_REMOTE_CONCURRENCY,
                "chase_concurrency": CHASE_REMOTE_CONCURRENCY,
                "scan_session": progress.in_scan_session()}


def invalidate_list_cache(mount_id: Optional[int] = None) -> None:
    """清掉目录列举缓存

    扫描开始前应当清一次（``scanner.clear_dir_cache`` 已经代劳）：扫描必须看到**当下**
    的目录，不能被播放刚填进去的缓存挡住——否则新加的文件要等下一轮扫描才入腹。
    """
    with _LIST_CACHE_LOCK:
        if mount_id is None:
            _LIST_CACHE.clear()
            return
        for key in [k for k in _LIST_CACHE if k[0] == mount_id]:
            _LIST_CACHE.pop(key, None)


def _config_fingerprint(provider) -> str:
    """挂载配置指纹：配置改了（换目录 / 换地址）就不该命中旧缓存

    只在进程内当缓存键用，不落日志、不外传（:36 里可能含密钥）。新建的临时挂载
    （测试连接，id 为空）也靠它区分，不至于和别的临时挂载串味。
    """
    config = getattr(provider, "config", None)
    if not isinstance(config, dict):
        return ""
    try:
        return json.dumps(config, sort_keys=True, default=str)
    except Exception:  # noqa: BLE001 — 指纹算不出来就退化成“不区分配置”
        return ""


def _list_cache_key(provider, kind: str, rel: str) -> tuple:
    mount = getattr(provider, "mount", None)
    return (
        getattr(mount, "id", None),
        (getattr(mount, "mount_type", "") or "").strip(),
        _config_fingerprint(provider),
        kind,
        rel or "/",
    )


def _cache_get(key: tuple) -> Optional[list]:
    """取缓存（命中返回**拷贝**；过期则删除并当未命中）

    扫描会话期间（``scan_progress.in_scan_session()``）**不看过期时间**：一轮扫描里同一个目录
    会被反复问到（目录指纹、外挂字幕、播放侧预取），TTL 只有 5 秒——扫描跑几分钟就是几十次
    多余的 PROPFIND，日志里那种「同一路径每 5 秒重复一次」正是这么来的。会话开始时
    ``scanner.clear_dir_cache`` 已经清过一次缓存，所以这里留下的都是**本轮**的数据。
    """
    now = time.monotonic()
    with _LIST_CACHE_LOCK:
        row = _LIST_CACHE.get(key)
        if row is not None and (row[0] > now or progress.in_scan_session()):
            _LIST_CACHE_STATS["hits"] += 1
            return list(row[1])
        if row is not None:
            _LIST_CACHE.pop(key, None)
            _LIST_CACHE_STATS["expired"] += 1
        _LIST_CACHE_STATS["misses"] += 1
        return None


def _cache_put(key: tuple, entries) -> None:
    with _LIST_CACHE_LOCK:
        if len(_LIST_CACHE) >= MOUNT_LIST_CACHE_MAX:
            _LIST_CACHE.clear()          # 满了整体清空：换来内存有上限，不会随库变大
            _LIST_CACHE_STATS["evictions"] += 1
        _LIST_CACHE[key] = (time.monotonic() + MOUNT_LIST_CACHE_SECONDS, tuple(entries))


def _single_flight_lock(key: tuple) -> threading.RLock:
    """拿这个目录的单飞锁（已在请求中就等它，别让并发请求各自去问一遍网盘）

    必须是**可重入锁**：子类的列目录实现可以包装在别的被缓存的方法上（同一提供者实例、
    同一个键），不可重入的话同一个线程会把自己锁死在第二层上。
    """
    with _LIST_CACHE_LOCK:
        lock = _LIST_LOCKS.get(key)
        if lock is not None:
            _LIST_CACHE_STATS["waits"] += 1
            return lock
        lock = threading.RLock()
        _LIST_LOCKS[key] = lock
        return lock


def _remote_semaphore() -> threading.BoundedSemaphore:
    """远程请求名额（上限是 MOUNT_REMOTE_CONCURRENCY；测试改了常量会重新建一个）"""
    global _REMOTE_SEM, _REMOTE_SEM_SIZE
    with _REMOTE_SEM_LOCK:
        if _REMOTE_SEM is None or _REMOTE_SEM_SIZE != MOUNT_REMOTE_CONCURRENCY:
            _REMOTE_SEM = threading.BoundedSemaphore(MOUNT_REMOTE_CONCURRENCY)
            _REMOTE_SEM_SIZE = MOUNT_REMOTE_CONCURRENCY
        return _REMOTE_SEM


def _chase_semaphore() -> threading.BoundedSemaphore:
    """追新专用的远程名额（更小，见 CHASE_REMOTE_CONCURRENCY 的说明）"""
    global _CHASE_SEM, _CHASE_SEM_SIZE
    with _REMOTE_SEM_LOCK:
        if _CHASE_SEM is None or _CHASE_SEM_SIZE != CHASE_REMOTE_CONCURRENCY:
            _CHASE_SEM = threading.BoundedSemaphore(CHASE_REMOTE_CONCURRENCY)
            _CHASE_SEM_SIZE = CHASE_REMOTE_CONCURRENCY
        return _CHASE_SEM


# 用途标记是**线程局部**的：调用方（追新）在自己的线程里声明「我这一段算追新」，
# 于是它经由 cached_listing → _call_remote → remote_io_slot 走的每一跳都自动落到
# 追新名额上。用线程局部而不是给 list_dir / _call_remote 一路加参数，是因为公共通道
# 有九个子类、几十个调用点，加参数要么改遍所有签名，要么就漏掉某一跳——限流一旦有
# 漏网的路径，限流就等于没有。
PURPOSE_SCAN = "scan"
PURPOSE_CHASE = "chase"
_IO_PURPOSE = threading.local()


@contextlib.contextmanager
def remote_io_purpose(purpose: str):
    """声明本线程接下来这一段远程 IO 属于哪个用途（决定它占哪个名额）

    必须是 ``with`` 作用域而不是全局开关：扫描 / 补全 / 追新跑在不同线程上，
    全局开关会让追新顺手把扫描的名额也换掉。
    """
    previous = getattr(_IO_PURPOSE, "purpose", PURPOSE_SCAN)
    _IO_PURPOSE.purpose = purpose or PURPOSE_SCAN
    try:
        yield
    finally:
        _IO_PURPOSE.purpose = previous


def current_io_purpose() -> str:
    """当前线程的用途标记（统计 / 日志用）"""
    return getattr(_IO_PURPOSE, "purpose", PURPOSE_SCAN) or PURPOSE_SCAN


@contextlib.contextmanager
def remote_io_slot():
    """占用一个「远程请求」名额（v2.27.0 的远程限流）

    只包住**真的会发出网络请求**的那一步（列目录、远程探测），缓存命中与单飞等待都不占名额——
    否则并发请求同一个目录时，等锁的线程会把名额白白占住，反而降低吞吐。

    名额按当前线程的用途标记分流（``remote_io_purpose``）：默认走扫描/补全的共享名额，
    追新走自己那个更小的独立名额，两边互不抢。
    """
    sem = _chase_semaphore() if current_io_purpose() == PURPOSE_CHASE else _remote_semaphore()
    sem.acquire()
    progress.note_remote_inflight(1)
    try:
        yield
    finally:
        progress.note_remote_inflight(-1)
        sem.release()


def _is_remote_provider(provider) -> bool:
    """这个提供者背后是不是远程端点（按挂载类型的 kind 判定，本机目录不吃名额）"""
    mount = getattr(provider, "mount", None)
    meta = type_meta(getattr(mount, "mount_type", "") or "")
    return (meta.get("kind") or "") == "remote"


def parse_mod_ts(value) -> float:
    """把远端返回的最后修改时间解析成 Unix 秒（解析不了就是 0.0）

    远端给的是 RFC3339 字符串，但**精度与时区写法因后端而异**：rclone 的
    ``ModTime`` 是纳秒（``2026-10-01T12:00:00.000000123Z``），而 Python 3.10 的
    ``fromisoformat`` 只接受 3 或 6 位小数——直接扔给它会解析失败、退化成 0.0，
    于是追新永远看不到新文件（静默漏检，比报错更难查）。所以先把小数秒截到微秒
    再解析；时区偏移（``+08:00``）原样保留，交给 ``fromisoformat``。
    """
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if not text:
        return 0.0
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    head, sep, frac = text.partition(".")
    if sep:
        # 小数部分后面还跟着时区偏移（12:00:00.123+08:00）
        digits = ""
        for ch in frac:
            if ch.isdigit():
                digits += ch
            else:
                break
        tail = frac[len(digits):]
        text = f"{head}.{digits[:6].ljust(6, '0')}{tail}"
    try:
        return datetime.fromisoformat(text).timestamp()
    except (ValueError, AttributeError, TypeError):
        return 0.0


# ==================== 远程列举的收口点 ====================
# 扫描、追新、补全都通过这里列目录，所以它是「远程 I/O 排队 + 阶段耗时」的唯一口径。
# v2.42.12：这里原本还挂着按挂载的熔断器（连续失败 N 次快速失败），已整块删除——
# 它把「网盘临时连不上」和「配置写错了」当成同一件事，前者被误判成后者，
# 结果是整个挂载被拉黑几分钟；现在失败就如实抛，由调用方按自己的退避策略处理。


def _call_remote(provider, fn: Callable, rel: str) -> list:
    """真的列一次目录（远程会占用名额，并记一次「远程列举」用于统计）"""
    if not _is_remote_provider(provider):
        with progress.stage_timer("local_list"):
            return fn(provider, rel)
    with progress.stage_timer("remote_list"):
        with remote_io_slot():
            entries = fn(provider, rel)
    progress.note_remote_listing()
    return entries


def cached_listing(fn: Callable) -> Callable:
    """把提供者的「列目录」实现包一层共用 TTL 缓存

    用装饰器而不是在基类里转发，是为了不重命名/不改动各个子类的实现（九个子类、五种
    协议），同时保证 ``self.list_dir`` 的任何调用点（包括基类的 ``exists`` / ``size``
    和子类自己的 ``walk_media``）都走缓存。
    """

    @functools.wraps(fn)
    def wrapper(self, rel: str = "/", fresh: bool = False):
        # BYOS 隔离：带绑定凭据的提供者实例（_byos_no_cache）永不读写
        # 共享目录缓存——缓存键只有挂载 id + 配置，不含凭据；同一挂载
        # 行在共享凭据与用户绑定凭据下会算出同一键，复用即跨用户串
        # 目录（名、cid、pickcode 皆属列出它的那个账号）。
        if (fresh or MOUNT_LIST_CACHE_SECONDS <= 0
                or getattr(self, "_byos_no_cache", False)):
            return _call_remote(self, fn, rel)
        key = _list_cache_key(self, fn.__name__, rel)
        hit = _cache_get(key)
        if hit is not None:
            progress.note_remote_listing(reused=True)
            return hit
        lock = _single_flight_lock(key)
        with lock:
            try:
                hit = _cache_get(key)        # 等锁期间别人可能已经列完了
                if hit is not None:
                    progress.note_remote_listing(reused=True)
                    return hit
                entries = _call_remote(self, fn, rel)   # 失败就往上抛：异常不进缓存
                _cache_put(key, entries)
                return list(entries)
            finally:
                with _LIST_CACHE_LOCK:
                    _LIST_LOCKS.pop(key, None)

    return wrapper


def list_dir_uncached(provider, rel: str = "/") -> list:
    """绕过缓存列目录（后台目录选择器 / 管理页的「刷新」用；看当下而不是 5 秒前的）"""
    impl = getattr(type(provider).list_dir, "__wrapped__", None)
    if impl is None:            # 没被装饰（自定义提供者）：照常调用
        return _call_remote(provider, lambda prov, path: prov.list_dir(path), rel)
    return _call_remote(provider, impl, rel)


@dataclass
class ProviderCapabilities:
    """提供者五能力位声明（FMBY 批三 #23，魂 4 能力声明式扩展的地基）。

    - ``can_list``：能列目录 / 遍历媒体（list_dir / walk_media 可用）。
    - ``can_random_read``：能按段取字节。语义是 open_range 能向调用方
      交出正确的 [start, end) 字节：本机 seek 直读，或远程 Range GET；
      上游回 200（无视 Range）时 open_range 在本地切片兜底，正确性仍
      有保证，只是多拉了整段字节（性能代价，不是正确性缺口）。
    - ``can_read_sidecar``：能读文本旁挂（NFO / 字幕 / .strm 内容）。
      注意粒度：本位只管文本旁挂；图片旁挂走 display-asset 语义
      （open_range 取字节），不由本位宣称。
    - ``can_generate_play_target``：能换链（resolve_play_target 可用）。
    - ``can_refresh_credentials``：凭据可刷新（refresh_credentials 可用
      且有真实刷新路径；静态凭据 / 无凭据的提供者声明 False）。
    """

    can_list: bool = False
    can_random_read: bool = False
    can_read_sidecar: bool = False
    can_generate_play_target: bool = False
    can_refresh_credentials: bool = False

    def as_dict(self) -> dict:
        return {
            "can_list": self.can_list,
            "can_random_read": self.can_random_read,
            "can_read_sidecar": self.can_read_sidecar,
            "can_generate_play_target": self.can_generate_play_target,
            "can_refresh_credentials": self.can_refresh_credentials,
        }


def read_sidecar_text_from(provider, rel: str) -> str:
    """旁挂文本读取的过渡口：优先走提供者的 ``read_sidecar_text`` 新口；
    只实现旧 ``read_text`` 的 duck-type 提供者（扫描器历史上的假提供者
    与第三方提供者都属此类）回落到 ``read_text``。

    扫描器对 provider 一直是 duck-type 调用（list_dir / read_text 都不
    要求继承 MountProvider），直接改口会静默打断这类提供者，故过渡口
    必须保留回落；MountProvider 子类永远走新口。
    """
    read = getattr(provider, "read_sidecar_text", None)
    if callable(read):
        return read(rel)
    return provider.read_text(rel)


class MountProvider:
    """挂载提供者基类"""

    mount_type = ""
    kind = "local"

    def __init__(self, mount, db: Optional[Session] = None, library=None):
        self.mount = mount
        self.db = db
        self.library = library
        self.config = parse_config(mount)

    # ---- 能力 ----
    # FMBY 批三 #23（阶段 2 骨架）：五能力位声明。语义是「保证」不是
    # 「尽力」——声明 True 的能力，上层可以无条件编排；不能保证的声明
    # False，哪怕现行某条路碰巧能走通。
    # 清单第 2 项起声明即门控：扫描（mount_sources.read_library_sources）、
    # 变体清单（play_variants.list_variants）、刷新重试
    # （refresh_credentials_if_supported）按本声明编排，不再只描述不执法。
    # 旧 capabilities() 曾返回 {"browse", "local"} 且在 af2769a2 上全仓
    # 零调用方（生产与测试均无调用），属于死代码，本次直接替换为五能力位
    # 结构，不保留旧 dict 形态。

    @property
    def local_root(self) -> Optional[str]:
        """本机可读时的根目录（远程挂载返回 None）"""
        return None

    def capabilities(self) -> "ProviderCapabilities":
        """本提供者的能力声明；未知提供者默认全 False（保守口径）。"""
        return ProviderCapabilities()

    # ---- 接口 ----

    def test(self) -> dict:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def list_dir(self, rel: str = "/") -> list[MountEntry]:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    def walk_media(self, root: str = "/") -> Iterator[MountFile]:  # pragma: no cover - 由子类实现
        """遍历媒体文件；``root`` 为挂载内的起始子目录（"/" = 整个挂载）。

        产出的 ``MountFile.rel`` 始终是挂载根相对路径（以 ``/`` 开头），与 ``root`` 无关。
        """
        raise NotImplementedError

    def read_text(self, rel: str) -> str:  # pragma: no cover - 由子类实现
        raise NotImplementedError

    # ---- 协议分离（FMBY 批三 #23 阶段 2 之二步）----
    # 旧 resolve 一个函数混了四件事：定位 / 取凭据 / 铸链 / 读旁挂。
    # 拆开后四口各管一件事，上层按能力声明编排时不再靠类型特例猜：
    # - resolve_play_target：换链，只回 PlayTarget，不读文件内容；
    # - open_range：搬字节，只按 Range 取段，不换链给客户端；
    # - read_sidecar_text：文本旁挂（NFO / 字幕 / .strm 内容）；
    # - refresh_credentials：凭据刷新，单独一口。
    # 清单第 2 项：旧 ``resolve()`` 已删——换链实现全部迁入各子类的
    # ``resolve_play_target``，旧调用方已全部迁移（全仓 grep 零调用方）；
    # ``read_text()`` 仍保留为旁挂文本的旧名。resolve_final 的
    # 语义逐字不变，只是 .strm 那条路现在显式走旁挂口，不再藏在换链里。

    def resolve_play_target(self, rel: str) -> PlayTarget:  # pragma: no cover - 由子类实现
        """换链口：返回文件的播放链（PlayTarget），不读文件内容。

        .strm 语义不在这里：.strm 要内容解析走 ``resolve_final``。
        子类直接覆写本口实现换链；未覆写的提供者调本口即
        ``NotImplementedError``，不许静默回落到别的实现。
        """
        raise NotImplementedError

    def read_sidecar_text(self, rel: str) -> str:
        """文本旁挂口（NFO / 字幕 / .strm 内容）。

        默认走既有 ``read_text``，覆写了 ``read_text`` 的子类行为原样
        保留；新调用方一律用本口，能力门控只认
        ``can_read_sidecar``。图片旁挂不走本口，走 ``open_range``
        的 display-asset 语义（按段取字节）。
        """
        return self.read_text(rel)

    def open_range(self, rel: str, start: int, end: Optional[int] = None) -> bytes:
        """按段取字节口：返回文件 [start, end) 的字节（end 为 None 读到尾）。

        本机挂载：seek 直读。远程挂载：子类覆写（换链 + Range GET）。
        注意：本口读的是文件本身；.strm 文件读到的是 .strm 文本，
        不是它指向的目标——目标解析是 ``resolve_final`` 的事。
        """
        if start < 0 or (end is not None and end <= start):
            raise MountError(f"无效的取段范围: start={start} end={end}")
        root = self.local_root
        if root is not None:
            try:
                path = safe_local_path(root, (rel or "/").lstrip("/"))
            except ValueError as exc:
                raise MountError(str(exc)) from exc
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    return f.read() if end is None else f.read(end - start)
            except OSError as exc:
                raise MountError(f"按段读取失败: {rel} ({exc})") from exc
        raise MountError("该提供者不支持按段取字节")

    def refresh_credentials(self) -> bool:
        """凭据刷新口：有真实刷新路径返回 True（失败抛 MountAuthError），
        无刷新路径返回 False（不是错误，调用方据此不重试刷新）。"""
        return False

    def refresh_credentials_if_supported(self) -> bool:
        """刷新重试唯一入口（清单第 2 项能力门控，横切只许一套）。

        先查 ``can_refresh_credentials`` 再刷新：声明 False 的提供者
        直接回 False，不调 ``refresh_credentials``、不重试——静态凭据
        （115 Cookie / rclone conf / 本机无凭据）重试刷新毫无意义，
        只会把一次凭据错刷成多次。声明 True 的提供者调其
        ``refresh_credentials()``（失败抛 ``MountAuthError``，调用方按
        既有口径处理）。刷新重试不许绕过本口另写一套。
        """
        try:
            caps = self.capabilities()
        except Exception:  # noqa: BLE001 — 声明都读不到的提供者按无刷新能力处理
            return False
        if not caps.can_refresh_credentials:
            return False
        return self.refresh_credentials()

    def resolve_final(self, rel: str) -> PlayTarget:
        """播放真正使用的解析入口：.strm 先取文件内容，再当直链播

        STRM 在**任何**挂载里都按内容解析（本机 / 115 / WebDAV / AList / 云盘），
        否则客户端会收到一个文本文件。为此不得不看文件内容时才走
        ``read_sidecar_text``（旁挂口），避免与换链口互相递归。
        非 .strm 直接走换链口。
        """
        if _is_strm_name(rel):
            return _strm_play_target(self.read_sidecar_text(rel), rel)
        return self.resolve_play_target(rel)

    # ---- 解析结果缓存的凭据拆分（FMBY 批一 #1）----
    # ``resolve_play_target`` 会把解析结果按 (挂载, 类型, 配置指纹, rel) 缓存
    # ``RESOLVE_TARGET_CACHE_TTL_SEC`` 秒，省掉每次播放重走的逐级列目录 /
    # 子进程换链。凭据头（Cookie / Authorization）**禁止进缓存值**：凭据状态
    # 只归既有唯一入口维护（115 的 ``transfer115.resolve_cookie``、Drive 的
    # ``drive_auth``），缓存里存一份凭据等于第二套状态，轮换 / 401 按值失效
    # 之后必然漂移——正是 2026-10-11 全局 token 缓存架空 SA 轮询事故的同款
    # 形态。所以子类把「解析」与「挂凭据」拆成两段：``cacheable_play_target``
    # 返回剥掉凭据头的可缓存副本（无法安全缓存时返回 None），
    # ``reattach_play_target`` 在缓存命中后现挂凭据。
    # 默认实现取保守口径：headers 里含凭据头就**不缓存**——没覆写的提供者
    # 最多是没收益，绝不许把凭据漏进缓存。

    def cacheable_play_target(self, target: PlayTarget) -> Optional[PlayTarget]:
        """解析结果进缓存前的脱凭据副本；不可安全缓存返回 None。"""
        headers = dict(target.headers or {})
        if any(name in headers for name in ("Cookie", "Authorization")):
            return None
        return PlayTarget(target.kind, target.value, headers)

    def reattach_play_target(self, stored: PlayTarget) -> PlayTarget:
        """缓存命中后现挂凭据；默认无凭据可挂，返回一份拷贝（缓存对象不外借）。"""
        return PlayTarget(stored.kind, stored.value, dict(stored.headers or {}))

    # ---- 公共实现 ----

    def exists(self, rel: str) -> bool:
        root = self.local_root
        if root is not None:
            # 与 list_dir / resolve / read_text 同一套越界口径（v2.30.0）：
            # 这里以前直接 os.path.join(root, rel)，``../`` 或符号链接能探到挂载根之外。
            # 越界的 rel 一律当作「不存在」，不是 500。
            try:
                return os.path.exists(safe_local_path(root, rel.lstrip("/")))
            except ValueError:
                logger.warning("挂载路径越界，按不存在处理: mount=%s rel=%s",
                               getattr(self.mount, "id", "?"), rel)
                return False
        try:
            parent, _, name = rel.rstrip("/").rpartition("/")
            return any(e.name == name for e in self.list_dir(parent or "/"))
        except MountError:
            return False

    def size(self, rel: str) -> int:
        root = self.local_root
        if root is not None:
            try:
                return os.path.getsize(safe_local_path(root, rel.lstrip("/")))
            except (OSError, ValueError):
                # 越界与读不到都返回 0：调用方（扫描 / 播放信息）只需知道「拿不到大小」
                return 0
        try:
            parent, _, name = rel.rstrip("/").rpartition("/")
            for e in self.list_dir(parent or "/"):
                if e.name == name:
                    return e.size
        except MountError:
            pass
        return 0


class LocalMount(MountProvider):
    """本机目录（``local`` / ``strm``）

    这两类挂载对扫描器完全没有新要求：目录就在本机，探测、图片、字幕都走本地文件。
    单独做成挂载的价值在于**可测试、可停用、可被多个库共用**，以及 STRM 目录的播放
    直链解析。``strm`` 与 ``local`` 的区别只有一个：目录里的 ``.strm`` 文件在扫描时
    也被当作媒体条目（``local`` 只扫视频文件）。
    """

    def __init__(self, mount, db=None, library=None):
        super().__init__(mount, db, library)
        self.kind = "local"
        self.mount_type = getattr(mount, "mount_type", MOUNT_LOCAL) or MOUNT_LOCAL
        self.path = (getattr(mount, "path", "") or "").strip()

    @property
    def local_root(self) -> Optional[str]:
        return self.path or None

    def capabilities(self) -> ProviderCapabilities:
        # 本机目录：列举 / 按段读（seek）/ 文本旁挂 / 换链（本机路径）
        # 全是真能力；无凭据可刷新。
        return ProviderCapabilities(
            can_list=True, can_random_read=True, can_read_sidecar=True,
            can_generate_play_target=True, can_refresh_credentials=False,
        )

    def _require_path(self) -> str:
        if not self.path:
            raise MountError("未配置目录路径")
        if not os.path.isdir(self.path):
            raise MountError(f"目录不可用: {self.path}")
        return self.path

    def test(self) -> dict:
        path = self._require_path()
        count = 0
        try:
            with os.scandir(path) as it:
                for _ in it:
                    count += 1
        except OSError as exc:
            raise MountError(f"目录不可读: {exc}") from exc
        return {"ok": True, "message": f"目录可读（顶层 {count} 项）", "path": path}

    @cached_listing
    def list_dir(self, rel: str = "/") -> list[MountEntry]:
        root = self._require_path()
        try:
            target = safe_local_path(root, (rel or "/").lstrip("/"))
        except ValueError as exc:
            raise MountError(str(exc)) from exc
        if not os.path.isdir(target):
            raise MountError(f"目录不存在: {rel}")
        entries: list[MountEntry] = []
        for name in sorted(os.listdir(target)):
            if name.startswith("."):
                continue
            full = os.path.join(target, name)
            is_dir = os.path.isdir(full)
            size = 0 if is_dir else (os.path.getsize(full) if os.path.exists(full) else 0)
            entries.append(MountEntry(
                name=name,
                rel=f"/{os.path.relpath(full, root).replace(os.sep, '/')}",
                is_dir=is_dir, size=size,
            ))
        return entries

    def walk_media(self, root: str = "/") -> Iterator[MountFile]:
        fs_root = self._require_path()
        # rel 始终按挂载根计算（入库路径不变），只把遍历起点挪到子目录
        walk_root = os.path.join(fs_root, root.lstrip("/")) if root.strip("/") else fs_root
        # 子目录列举失败不能静默吞掉：下面文件没列出来，清理阶段会误判为
        # 「已删除」而下架条目。收集起来，遍历结束后统一抛错，上层按来源
        # 不可用处理并跳过清理（其他目录照常扫完，不中断整库扫描）。
        walk_errors: list[str] = []

        def _onerr(err: OSError) -> None:
            walk_errors.append(
                f"{getattr(err, 'filename', '') or walk_root}: "
                f"{getattr(err, 'strerror', '') or err}"
            )

        for dirpath, _dirnames, filenames in os.walk(
            walk_root,
            # 原盘结构目录（BDMV/STREAM、CERTIFICATE…）整棵剪掉：
            # 里面的 .m2ts 是码流片段，当成电影会产出一堆没法刮削的条目
            topdown=True,
            onerror=_onerr,
        ):
            _dirnames[:] = [
                d for d in _dirnames if not disc_filter.is_disc_subtree_dir(d)
            ]
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in REMOTE_MEDIA_EXTS:
                    continue
                # 普通目录里混着 .strm 也认（很多人把 strm 和视频放一起），
                # 播放时统一由 local_play_target 读文件内容取直链。
                is_strm = _is_strm_name(fname)
                full = os.path.join(dirpath, fname)
                rel = "/" + os.path.relpath(full, fs_root).replace(os.sep, "/")
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                yield MountFile(rel=rel, name=fname, size=size, is_strm=is_strm)
        if walk_errors:
            # 生成器收尾时抛：已产出的文件照常处理，上层 _guarded 会把错误记进
            # failed_roots 并跳过清理阶段，避免误删该子目录下的条目。
            raise MountError(
                f"本地挂载 {len(walk_errors)} 个子目录列举失败: "
                + "; ".join(walk_errors[:5])
                + ("..." if len(walk_errors) > 5 else "")
            )

    def resolve_play_target(self, rel: str) -> PlayTarget:
        root = self._require_path()
        try:
            path = safe_local_path(root, (rel or "/").lstrip("/"))
        except ValueError as exc:
            raise MountError(str(exc)) from exc
        return local_play_target(path)

    def read_text(self, rel: str) -> str:
        root = self._require_path()
        try:
            path = safe_local_path(root, (rel or "/").lstrip("/"))
        except ValueError as exc:
            raise MountError(str(exc)) from exc
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
        except OSError as exc:
            raise MountError(f"读取文件失败: {rel} ({exc})") from exc


class Pan115Mount(MountProvider):
    """115 网盘直挂（Cookie 型 API，不依赖 115 OpenAPI）"""

    kind = "remote"
    mount_type = MOUNT_PAN115

    def __init__(self, mount, db=None, library=None, cookie_override=None):
        super().__init__(mount, db, library)
        # BYOS 绑定凭据：调用方（user_bindings）传入该用户自己的
        # Cookie 后，本实例只用它（不读挂载配置/账号档/服务器级
        # Cookie），且永不读写共享目录缓存（见 cached_listing 的
        # _byos_no_cache 分支）——凭据绝不跨用户复用。
        self._cookie_override = (
            transfer115.normalize_cookie(cookie_override)
            if cookie_override else None
        )
        self._byos_no_cache = self._cookie_override is not None
        self.root_cid = str(self.config.get("cid") or "0")
        # rel → cid / pickcode 缓存（只在本次提供者实例内，不跨请求）
        self._cid_cache: dict[str, str] = {"": self.root_cid, "/": self.root_cid}
        self._pick_cache: dict[str, str] = {}

    def capabilities(self) -> ProviderCapabilities:
        # 115：列举 / 按段读（下载直链 Range，见 open_range）/ 文本旁挂 /
        # 换链全是真能力；Cookie 是静态凭据、过期只报 MountAuthError，
        # 没有刷新路径——这是全仓唯一确定的否定声明。
        return ProviderCapabilities(
            can_list=True, can_random_read=True, can_read_sidecar=True,
            can_generate_play_target=True, can_refresh_credentials=False,
        )

    # ---- Cookie ----
    def _cookie(self) -> str:
        if self._cookie_override:
            return self._cookie_override
        account_id = self.config.get("account_id")
        try:
            account_id = int(account_id) if account_id not in (None, "", "0") else None
        except (TypeError, ValueError):
            account_id = None
        if self.db is None:
            # 没有数据库会话（例如单独构造提供者做测试）：只认挂载内 Cookie 与服务器级兜底
            explicit = transfer115.normalize_cookie(self.config.get("cookie") or "")
            cookie = explicit or transfer115.normalize_cookie(
                os.getenv(transfer115.PAN115_COOKIE_ENV, "")
            )
            source = "挂载配置" if explicit else f"服务器级 {transfer115.PAN115_COOKIE_ENV}"
        else:
            cookie, source = transfer115.resolve_cookie(
                self.db, library=self.library, account_id=account_id,
            )
        if not cookie:
            raise MountAuthError(f"未解析到 115 Cookie（来源：{source}）")
        return cookie

    def _ua(self) -> str:
        """这个挂载发 115 请求时用的 UA（与 Cookie 同一套账号优先级）"""
        if self._cookie_override:
            # 绑定凭据实例不用挂载的账号 UA（那是另一个账号的设备
            # 身份），回服务器级默认 UA。
            return ""
        if self.db is None:
            return ""
        try:
            account_id = int(self.config.get("account_id") or 0) or None
        except (TypeError, ValueError):
            account_id = None
        return transfer115.resolve_ua(
            self.db, library=self.library, account_id=account_id,
            explicit_ua=self.config.get("ua") or "",
        )

    def _client(self) -> transfer115.Pan115Client:
        return transfer115.Pan115Client(self._cookie(), ua=self._ua())

    def _wrap(self, exc: Exception) -> MountError:
        if isinstance(exc, transfer115.Pan115AuthError):
            return MountAuthError(str(exc))
        return MountError(str(exc))

    # ---- 目录 ----
    def test(self) -> dict:
        client = self._client()
        try:
            info = client.check()
            entries = client.list_dir(self.root_cid)
        except (transfer115.Pan115Error, transfer115.Pan115AuthError) as exc:
            raise self._wrap(exc) from exc
        root = "根目录" if self.root_cid == "0" else f"目录 {self.root_cid}"
        return {
            "ok": True,
            "message": f"Cookie 有效（uid={info.get('uid') or '未知'}），{root} 下有 {len(entries)} 项",
            "uid": info.get("uid"),
        }

    @cached_listing
    def _raw_list(self, cid: str) -> list[dict]:
        try:
            entries = self._client().list_dir(cid or "0")
        except (transfer115.Pan115Error, transfer115.Pan115AuthError) as exc:
            raise self._wrap(exc) from exc
        for entry in entries:
            entry["cid"] = str(entry.get("cid") or "")
        return entries

    @cached_listing
    def list_dir(self, rel: str = "/") -> list[MountEntry]:
        raw = self._raw_list(self._cid_of(rel))
        prefix = ("/" + (rel or "").lstrip("/")).rstrip("/")
        entries = []
        for item in raw:
            name = item.get("name") or ""
            child_rel = f"{prefix}/{name}"
            self._cid_cache.setdefault(child_rel, item.get("cid") or "")
            entries.append(MountEntry(
                name=name, rel=child_rel,
                is_dir=bool(item.get("is_dir")),
                size=int(item.get("size") or 0),
                entry_id=item.get("cid") or "",
            ))
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def walk_media(self, root: str = "/") -> Iterator[MountFile]:
        # 子目录不存在时 _cid_of 直接抛 MountError（上层按来源不可用处理）
        stack: list[tuple[str, str]] = [(root, self._cid_of(root))]
        seen: set[str] = set()
        while stack:
            rel, cid = stack.pop()
            if cid in seen and cid != self.root_cid:
                continue  # 防目录环（115 正常不会出现，但扫描不能因此死循环）
            seen.add(cid)
            for item in self._raw_list(cid):
                name = item.get("name") or ""
                child_rel = f"{rel.rstrip('/')}/{name}"
                if item.get("is_dir"):
                    if disc_filter.is_disc_subtree_dir(name):
                        continue
                    self._cid_cache[child_rel] = item.get("cid") or ""
                    stack.append((child_rel, item.get("cid") or ""))
                    continue
                if os.path.splitext(name)[1].lower() not in REMOTE_MEDIA_EXTS:
                    continue
                self._pick_cache[child_rel] = str(item.get("pickcode") or "")
                yield MountFile(rel=child_rel, name=name,
                                size=int(item.get("size") or 0), is_strm=_is_strm_name(name))

    def _cid_of(self, rel: str) -> str:
        """把相对路径解析成 cid（逐级列目录，结果缓存到本次提供者实例）"""
        rel = "/" + (rel or "").lstrip("/")
        rel = rel.rstrip("/") or "/"
        if rel in self._cid_cache and self._cid_cache[rel]:
            return self._cid_cache[rel]
        cid = self.root_cid
        for part in [p for p in rel.split("/") if p]:
            child = next(
                (e for e in self._raw_list(cid) if e.get("name") == part and e.get("is_dir")),
                None,
            )
            if child is None:
                raise MountError(f"115 上找不到目录: {rel}")
            cid = child.get("cid") or ""
        self._cid_cache[rel] = cid
        return cid

    def _locate(self, rel: str) -> dict:
        """定位文件项：返回 ``{pickcode, name, size}``"""
        rel = "/" + (rel or "").lstrip("/")
        if rel in self._pick_cache:
            return {"pickcode": self._pick_cache[rel], "name": os.path.basename(rel)}
        parent, _, name = rel.rpartition("/")
        for item in self._raw_list(self._cid_of(parent or "/")):
            if item.get("name") == name:
                return {
                    "pickcode": str(item.get("pickcode") or ""),
                    "name": name,
                    "size": int(item.get("size") or 0),
                }
        raise MountError(f"115 上找不到文件: {rel}")

    def resolve_play_target(self, rel: str) -> PlayTarget:
        entry = self._locate(rel)
        if not entry.get("pickcode"):
            raise MountError(f"115 未返回该文件的 pickcode: {rel}")
        try:
            pickcode_115 = entry["pickcode"]
            url = _get_cached_115_url(pickcode_115)
            if not url:
                url = self._client().download_url(pickcode_115)
                _set_cached_115_url(pickcode_115, url)
        except (transfer115.Pan115Error, transfer115.Pan115AuthError) as exc:
            raise self._wrap(exc) from exc
        # 直链自带签名，但仍带上 UA/Cookie：EA 代理转发，客户端看不到这些头
        return PlayTarget("url", url, {
            "User-Agent": self._ua() or MOUNT_UA,
            "Referer": "https://115.com/",
            "Cookie": self._cookie(),
            "X-Pickcode": pickcode_115,
        })

    # 解析缓存的凭据拆分：Cookie 每次现取（``transfer115.resolve_cookie`` 是
    # 唯一状态入口），缓存里只留 UA / Referer 与 X-Pickcode——X-Pickcode 不是
    # 凭据，是失效钥匙：中继收到上游 403/404/410 时靠它连带清解析缓存。
    def cacheable_play_target(self, target: PlayTarget) -> Optional[PlayTarget]:
        headers = dict(target.headers or {})
        headers.pop("Cookie", None)
        return PlayTarget(target.kind, target.value, headers)

    def reattach_play_target(self, stored: PlayTarget) -> PlayTarget:
        headers = dict(stored.headers or {})
        headers["Cookie"] = self._cookie()
        return PlayTarget(stored.kind, stored.value, headers)

    def read_text(self, rel: str) -> str:
        target = self.resolve_play_target(rel)
        client = _shared_http_client("pan115")
        resp = client.get(target.value, headers=target.headers)
        if resp.status_code >= 400:
            raise MountError(f"读取 115 文件失败: HTTP {resp.status_code}")
        return resp.content.decode("utf-8", errors="ignore")

    def open_range(self, rel: str, start: int, end: Optional[int] = None) -> bytes:
        """115 按段取字节：换链 + Range GET（与 RemoteMount 同款切片兜底，
        复用既有远程 IO 名额；115 不继承 RemoteMount，故单写一份）。"""
        if start < 0 or (end is not None and end <= start):
            raise MountError(f"无效的取段范围: start={start} end={end}")
        target = self.resolve_play_target(rel)
        range_header = (
            f"bytes={start}-" if end is None else f"bytes={start}-{end - 1}"
        )
        headers = dict(target.headers or {})
        headers["Range"] = range_header
        client = _shared_http_client("pan115")
        with remote_io_slot():
            resp = client.get(target.value, headers=headers)
        if resp.status_code >= 400:
            raise MountError(
                f"按段读取 115 文件失败: HTTP {resp.status_code}")
        body = resp.content or b""
        if resp.status_code == 206:
            return body
        return body[start:end] if end is not None else body[start:]


# 模块级共享 HTTP 客户端池：按 (base, timeout) 复用连接，避免扫描时
# 每次请求都新建连接池（借鉴 go-emby 的连接复用思路）。httpx.Client
# 的同步 API 是线程安全的，可在扫描线程池里共用。
_shared_clients: dict = {}
# 模块级直接初始化：懒初始化（if None: 建锁）在多线程首次并发调用时存在
# 竞态——两线程可各建一把锁导致互斥被架空。本文件 _LIST_CACHE_LOCK 即此口径。
_shared_clients_lock = threading.Lock()


# 115 direct URL cache: pickcode -> (url, expire_at). URLs valid ~4h, cache 3h.
_pan115_url_cache = {}
# 同上：模块级直接初始化，不许懒初始化。
_pan115_url_cache_lock = threading.Lock()

def _get_cached_115_url(pickcode):
    with _pan115_url_cache_lock:
        entry = _pan115_url_cache.get(pickcode)
        if entry and entry[1] > time.time():
            return entry[0]
        elif entry:
            del _pan115_url_cache[pickcode]
    return None

def _set_cached_115_url(pickcode, url, ttl=10800):
    with _pan115_url_cache_lock:
        if len(_pan115_url_cache) >= 1000:
            sk = sorted(_pan115_url_cache, key=lambda k: _pan115_url_cache[k][1])
            for k in sk[:500]:
                del _pan115_url_cache[k]
        _pan115_url_cache[pickcode] = (url, time.time() + ttl)

def _invalidate_115_url(pickcode):
    with _pan115_url_cache_lock:
        _pan115_url_cache.pop(pickcode, None)


def _shared_http_client(key: str = "default", timeout=None):
    """取（或创建）具名共享客户端。"""
    import httpx

    with _shared_clients_lock:
        client = _shared_clients.get(key)
        if client is None:
            client = httpx.Client(
                timeout=timeout or MOUNT_TIMEOUT, follow_redirects=True
            )
            _shared_clients[key] = client
        return client


def walk_workers_limit() -> int:
    """扫描遍历的并发线程数（SCAN_WALK_WORKERS，默认 8）。

    非法值回退默认，保证配错了也不炸扫描。

    2026-10 从 16 降到 8：并发主要是为了掩盖网盘/Rclone 的延迟，不是为了提高吞吐。
    16 个线程同时打向同一个 rclone RC 端点时，rcd 侧排队把延迟放大到十几倍，
    并且把大量请求堆成 context canceled / connection reset（生产 24 小时 5.2 万条）。
    降一半后单轮扫描慢一些，但成功率与错误量都大幅改善。
    """
    # 按 CPU 自适应（cpu_budget）：默认 8，手动配置时截断到核数-1，永远给前台留一核
    return _capped_workers("SCAN_WALK_WORKERS", 8)


class RemoteMount(MountProvider):
    """远程挂载基类：统一 HTTP 调用、错误翻译与递归扫描

    v2.42.12：随 s3 / aliyun / quark / onedrive 一起从 mount_cloud.py 搬到这里。
    原来它和那四个具体后端住在同一个文件里，现在唯一的子类是 rclone，
    留在 mounts.py 里才不用为一个基类单开一个模块。
    """

    kind = "remote"
    #: 出错提示里用的名字（如「阿里云盘」）
    what = "云端存储"

    def _client(self, follow_redirects: bool = True):
        import httpx

        return httpx.Client(timeout=MOUNT_TIMEOUT, follow_redirects=follow_redirects)

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict] = None,
        params: Optional[dict] = None,
        json_body: Optional[dict] = None,
        data: Optional[dict] = None,
        follow_redirects: bool = True,
    ):
        try:
            with self._client(follow_redirects) as client:
                return client.request(
                    method, url, headers=headers or {}, params=params,
                    json=json_body, data=data,
                )
        except Exception as exc:  # noqa: BLE001 — 网络层异常统一成可读提示
            raise MountError(f"连接{self.what}失败: {exc}") from exc

    def _request_json(self, method: str, url: str, **kwargs) -> dict:
        resp = self._request(method, url, **kwargs)
        try:
            body = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise MountError(f"{self.what}返回了无法解析的响应: {exc}") from exc
        return body if isinstance(body, dict) else {}

    def _entries(self, rel: str) -> list[MountEntry]:
        """列目录。

        子目录读不到时**抛错**（携带失败目录）：``walk_media`` 会收集这些失败、
        扫完其他目录后统一抛 ``MountError``，扫描器据此跳过清理阶段。
        「读不到」不能当成「文件已删除」——否则该子目录下的条目会被误删
        （之前这里返回空列表静默跳过，正是误删的根因之一）。
        根目录失败直接抛 ``MountError``（来源不可用）。
        """
        try:
            return self.list_dir(rel)
        except MountError as exc:
            if rel in ("", "/"):
                raise
            raise MountError(f"子目录列举失败 {rel}: {exc}") from exc

    def walk_workers(self) -> int:
        """本挂载遍历的并发上限（默认全局 ``SCAN_WALK_WORKERS``）。

        给「有硬配额」的后端一个下调口子：Google Drive 是 100 秒 10000 次查询的
        per-user 配额，用全局的 16 线程并发打过去会直接撞 ``rateLimitExceeded``，
        那时整轮扫描只能失败重试。子类覆写这一项即可，115 / S3 等已稳定的类型
        保持原样。

        用 ``getattr`` 兜底：测试里的假挂载常直接继承本类而只实现必需方法，
        少实现一个钩子不该让整轮遍历抛 AttributeError——退回全局默认即可。
        """
        return walk_workers_limit()

    def walk_media(self, max_depth: int = 32, root: str = "/") -> Iterator[MountFile]:
        """远程挂载的通用遍历：靠 ``list_dir`` 递归，自动跳过过深目录

        ``root`` 为挂载内的起始子目录（"/" = 整个挂载）；产出的 ``rel`` 始终是
        挂载根相对路径，与 ``root`` 无关。

        起始目录读不到时直接抛错（不吞掉）：上层按「来源不可用」处理并跳过清理，
        避免把「读不到」当成「文件已删除」而误删条目。

        分层并行：同一层级的目录用 16 线程并发列举（网盘 API 延迟是瓶颈）。
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        self.list_dir(root)
        seen: set[str] = set()
        seen.add(root)
        current = [(root, 0)]
        # 文件级去重：rclone lsjson 偶发在单次列举里返回同一文件两次（网盘侧重复
        # 条目 / 分页异常；2026-09-25 生产事故：同一 mkv 被产出两次，扫描器在同一
        # 事务内两次 INSERT 撞 emby_items.guid 唯一键、整库扫描 abort）。扫描器在
        # _prepare_and_prefetch 按 guid 去重兜底，这里在源头先拦一道，也省掉重复
        # 的 ffprobe 与 TMDB 预取。rel 全局唯一，误杀不了正常文件。
        seen_files: set[str] = set()
        # 分层 BFS：每层目录并发列举
        # 并发数可配（SCAN_WALK_WORKERS，默认 8）：网盘 API 延迟是瓶颈，并发主要
        # 是掩盖延迟；但并发越高，同时打向 rclone RC / 网盘的请求越多，rcd 侧的
        # 内存峰值也越高。内存吃紧的机器可调小（如 8），扫描会慢一些。
        # 并发数走 ``getattr(self, "walk_workers", None)`` 而不是直接调方法：
        # 测试里的假挂载常只实现必需方法、没继承这个钩子，直接调会 AttributeError
        # 让整轮遍历失败（2026-10 加这个钩子时踩过）。取不到就退回全局默认。
        workers = self.walk_workers() if hasattr(self, "walk_workers") else walk_workers_limit()
        # 子目录列举失败不能静默吞掉：下面文件没列出来，清理阶段会误判为
        # 「已删除」而下架条目。收集起来，遍历结束后统一抛错——其他目录照常
        # 扫完（不中断整库扫描），上层按来源不可用处理并跳过清理。
        walk_errors: list[str] = []
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="walk") as pool:
            while current:
                fut_to_dir = {
                    pool.submit(self._entries, rel): (rel, depth)
                    for rel, depth in current
                }
                current = []
                for fut in as_completed(fut_to_dir):
                    rel, depth = fut_to_dir[fut]
                    try:
                        entries = fut.result()
                    except Exception as e:  # noqa: BLE001 — 单个子目录失败不中断整库，统一收集后上报
                        logger.warning("%s 并行列目录失败 %s: %s", self.what, rel, e)
                        walk_errors.append(f"{rel}: {e}")
                        continue
                    for entry in entries:
                        if entry.is_dir:
                            # 原盘结构目录（BDMV/STREAM、CERTIFICATE…）整棵跳过，
                            # 否则每条 .m2ts 都会被当成一部电影
                            if disc_filter.is_disc_subtree_dir(entry.name):
                                continue
                            if depth < max_depth and entry.rel not in seen:
                                seen.add(entry.rel)
                                current.append((entry.rel, depth + 1))
                            continue
                        if os.path.splitext(entry.name)[1].lower() not in REMOTE_MEDIA_EXTS:
                            continue
                        if entry.rel in seen_files:
                            logger.debug("%s 遍历跳过重复文件条目：%s", self.what, entry.rel)
                            continue
                        seen_files.add(entry.rel)
                        yield MountFile(rel=entry.rel, name=entry.name, size=entry.size,
                                        is_strm=_is_strm_name(entry.name),
                                        file_id=getattr(entry, "file_id", "") or "",
                                        # P2（完美审查 C-P2-3）：把远端 ModTime 透给扫描器，
                                        # 用于文件指纹/目录指纹，同尺寸替换可检出。
                                        mod_ts=getattr(entry, "mod_ts", 0.0) or 0.0)
        if walk_errors:
            # 生成器收尾时抛：已产出的文件照常处理，上层 _guarded 会把错误记进
            # failed_roots 并跳过清理阶段，避免误删这些子目录下的条目。
            raise MountError(
                f"{self.what} {len(walk_errors)} 个子目录列举失败: "
                + "; ".join(walk_errors[:5])
                + ("..." if len(walk_errors) > 5 else "")
            )

    def read_text(self, rel: str) -> str:
        """默认实现：解析成直链后把内容当文本读（用于 .strm 与字幕）"""
        target = self.resolve_play_target(rel)
        resp = self._request("GET", target.value, headers=target.headers)
        return resp.content.decode("utf-8", errors="ignore")

    def open_range(self, rel: str, start: int, end: Optional[int] = None) -> bytes:
        """远程按段取字节：换链 + Range GET，复用既有远程 IO 名额。

        上游回 206 直接返回；上游回 200（无视 Range）时在本地按段切片
        兜底——调用方拿到的字节一定是 [start, end)，正确性有保证，
        代价是多拉了整段字节。网络请求只包在 remote_io_slot 里，
        不另起第二套限流。
        """
        if start < 0 or (end is not None and end <= start):
            raise MountError(f"无效的取段范围: start={start} end={end}")
        target = self.resolve_play_target(rel)
        range_header = (
            f"bytes={start}-" if end is None else f"bytes={start}-{end - 1}"
        )
        headers = dict(target.headers or {})
        headers["Range"] = range_header
        with remote_io_slot():
            resp = self._request("GET", target.value, headers=headers)
        if resp.status_code >= 400:
            raise MountError(
                f"按段读取失败: {rel} (HTTP {resp.status_code})")
        body = resp.content or b""
        if resp.status_code == 206:
            return body
        # 200 或其他 2xx：上游无视 Range，本地切片兜底。
        return body[start:end] if end is not None else body[start:]


_PROVIDERS = {
    MOUNT_LOCAL: LocalMount,
    MOUNT_PAN115: Pan115Mount,
}


def build_provider(mount, db: Optional[Session] = None, library=None) -> MountProvider:
    """按挂载类型构造提供者（未知类型 → 明确报错，而不是静默当成空库）"""
    mount_type = (getattr(mount, "mount_type", "") or "").strip()
    provider_cls = _PROVIDERS.get(mount_type)
    if provider_cls is None:
        # 已下线类型（WebDAV / AList / S3 / 夸克 / OneDrive / Google Drive 原生…）在库里
        # 可能还有残留行。**不自动删**：自动删 = 删别人机器上的数据且无法撤销。
        # 所以这里把改法一起说出来，而不是只报一个「不支持的类型」让人猜。
        supported = "、".join(f"{t['value']}" for t in MOUNT_TYPES)
        raise MountError(
            f"不支持的挂载类型：{mount_type or '(空)'}（这类型已在 v2.42.12 下线，"
            f"数据仍然保留）。请在服务器管理里改成受支持的类型之一：{supported}。"
            "Google 云盘选 drive 类型（API 直读）；其余后端 rclone 都支持，"
            "粘一份 rclone.conf 就能接上。")
    return provider_cls(mount, db, library)


def test_mount(mount, db: Optional[Session] = None, library=None) -> dict:
    """测试连接：返回 ``{ok, message, detail}``；失败不抛异常，交给后台展示"""
    try:
        result = build_provider(mount, db, library).test()
        return {"ok": True, "message": result.get("message", "连接正常"), "detail": result}
    except MountAuthError as exc:
        return {"ok": False, "message": str(exc), "auth_error": True}
    except MountError as exc:
        return {"ok": False, "message": str(exc), "auth_error": False}
    except Exception as exc:  # noqa: BLE001 — 兜底：任何异常都要变成可读提示
        logger.warning("挂载测试异常 %s: %s", getattr(mount, "id", None), exc)
        return {"ok": False, "message": f"连接失败: {exc}", "auth_error": False}


# ==================== 媒体库 → 来源 ====================

@dataclass
class LibrarySource:
    """扫描来源：本机目录 or 一个挂载"""

    label: str
    kind: str                      # local / mount
    path: str = ""                 # 本机目录（kind=local）
    mount: Any = None              # StorageMount（kind=mount）
    provider: Optional[MountProvider] = None
    subpath: str = "/"             # kind=mount 时只扫描挂载下的这个子目录（"/" = 整个挂载）
    #: 新表 library_sources 行 id（清单第 8 项身份锚：入库写 item.source_id 用）。
    #: 旧列回落路径没有行，恒为 None；扫描器只在有行 id 时写锚，绝不反查补写。
    source_row_id: Optional[int] = None


#: 库 id 的合理上限（SQLite/PG 主键都是 64 位整数）：粘进来的超长数字串直接丢掉
MAX_ID_VALUE = 2 ** 63 - 1


def parse_id_list(raw: Any) -> list[int]:
    """把 ``"1,2,  3"`` 这种字串解析成**去重后的整数列表**（脏值忽略，不报错）

    全仓**唯一**的 id 字串解析口（v2.46.0）：``Library.mount_ids`` 列与追新配置里的
    排除/包含清单都走它。以前追新自己写了一份，两边对「全角逗号 / 上限 / 去重」的
    处理并不一致——同一串 id 在两处可能解析出不同结果。

    规则：全角逗号当半角、去空白、非数字丢弃、重复丢弃、超过 64 位上限丢弃。
    """
    out: list[int] = []
    for part in str(raw or "").replace("，", ",").split(","):
        part = part.strip()
        if not part.isdigit():
            continue
        value = int(part)
        if 0 < value <= MAX_ID_VALUE and value not in out:
            out.append(value)
    return out


def parse_mount_ids(library) -> list[int]:
    """库里绑定的挂载 id 列表（``Library.mount_ids`` 列）"""
    return parse_id_list(getattr(library, "mount_ids", ""))


# ==================== 路径与存储后端分离（界面上不出现 mount://）====================
#
# 入库的 ``Library.paths`` 始终保持老形态（本机绝对路径 / ``mount://<id>/<子目录>`` /
# ``115:/`` / ``rclone:`` 前缀），扫描、播放、追新全都按它工作——**一行代码都不改**。
# 只有「界面怎么显示」与「界面怎么写回来」这一层做了转换：
#
#   存 → assemble_library_path：裸路径 + 后端 + 挂载 id ⇒ mount://3/电影
#   读 → library_path_entries：  mount://3/电影      ⇒ {path: /电影, backend: 115}
#
# 为什么值得加这一层：``mount://1/paul_emby/video/剧集/国产剧`` 这类路径对人没有信息量
# （1 是数据库主键、paul_emby 是挂载名），管理员既看不懂也改不动。

#: 存储后端 → 展示名（**由后端下发**，前端不自己维护一份）
STORAGE_BACKEND_LABELS: dict[str, str] = {
    MOUNT_LOCAL: "本地文件",
    MOUNT_RCLONE: "Rclone",
    MOUNT_PAN115: "115 网盘",
}

#: 归一时要认的别名（界面上传的值、扩展模块注册的写法都从这里过）
_BACKEND_ALIASES: dict[str, str] = {
    "local": MOUNT_LOCAL, "disk": MOUNT_LOCAL, "本地": MOUNT_LOCAL,
    "rclone": MOUNT_RCLONE, "115": MOUNT_PAN115, "pan115": MOUNT_PAN115,
}


def normalize_storage_backend(value: Optional[str]) -> str:
    """归一存储后端标识：``local`` / ``rclone`` / ``115``（扩展类型原样小写）

    认不出来时返回**空串**而不是猜一个——猜错会让界面把「115 网盘」标成「Rclone」，
    比空着更误导（空着时界面显示「未知来源」，一眼就知道该去核对这个挂载）。
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return _BACKEND_ALIASES.get(raw, raw)


def storage_backend_label(backend: Optional[str]) -> str:
    """后端展示名（认不出来的原样回显，总比空白好懂）"""
    key = normalize_storage_backend(backend)
    return STORAGE_BACKEND_LABELS.get(key) or MOUNT_TYPE_LABELS.get(key) or key or "未知来源"


def split_library_paths(raw: Any) -> list[str]:
    """把 ``Library.paths`` 拆成路径列表（逗号 / 中文逗号分隔，去重去空）"""
    out: list[str] = []
    for part in str(raw or "").replace("，", ",").split(","):
        item = part.strip()
        if item and item not in out:
            out.append(item)
    return out


def dump_storage_backends(backends: list[str]) -> str:
    """后端列表 → 入库字符串（与 paths 逐条对应）"""
    return ",".join(normalize_storage_backend(b) for b in backends)


def _stored_backends(library) -> list[str]:
    return split_library_paths(getattr(library, "storage_backends", ""))


def library_path_entries(library, mounts: Optional[dict] = None) -> list[dict]:
    """把媒体库的 ``paths`` 拆成**界面友好的路径条目**（隐藏 mount:// 前缀）

    每条：``{path, backend, backend_label, mount_id, mount_name, source, raw}``

    - ``path``：挂载来源给挂载内路径（``/视频/剧集/国产剧``），本机来源给绝对路径；
    - ``backend``：``local`` / ``rclone`` / ``115``，老数据靠「挂载类型 + 路径前缀」现推；
    - ``raw``：原样保留，界面不显示它，但改动对比 / 排障要用。

    ``mounts`` 是 ``{id: StorageMount}``；不传时只按前缀推断（拿不到挂载名与真实类型）。
    """
    raws = split_library_paths(getattr(library, "paths", ""))
    stored = _stored_backends(library)
    mounts = mounts or {}
    entries: list[dict] = []
    for idx, raw in enumerate(raws):
        remembered = normalize_storage_backend(stored[idx]) if idx < len(stored) else ""
        parsed = parse_mount_path(raw)
        if parsed is not None:
            mount_id, rel = parsed
            mount = mounts.get(mount_id)
            # 挂载还在时**以挂载类型为准**（它是真实情况：挂载可能被改过类型）；
            # 记住的后端只接挂载已经查不到时的班，否则会把删掉的挂载标错。
            backend = (normalize_storage_backend(getattr(mount, "mount_type", ""))
                       or remembered)
            entries.append({
                "path": rel or "/",
                "backend": backend,
                "backend_label": storage_backend_label(backend),
                "mount_id": mount_id,
                "mount_name": getattr(mount, "name", "") if mount is not None else "",
                "source": "mount",
                "raw": raw,
            })
            continue
        backend = (remembered or normalize_storage_backend(detect_mount_type(raw))
                   or MOUNT_LOCAL)
        entries.append({
            "path": raw,
            "backend": backend,
            "backend_label": storage_backend_label(backend),
            "mount_id": None,
            "mount_name": "",
            "source": "local" if raw.startswith("/") else "prefix",
            "raw": raw,
        })
    return entries


def assemble_library_path(entry: dict) -> str:
    """界面条目 → 入库路径（**自动拼回** ``mount://<id>/<子目录>``）

    - 本机来源：原样（必须是绝对路径，前缀路径如 ``rclone:gdrive/Movies`` 也原样保留，
      扫描器认得这两个前缀，不需要挂载）；
    - 远程来源且选了挂载：拼 ``mount://<挂载id>/<子目录>``；
    - 远程来源但没选挂载：只有当路径自带 ``115:/`` / ``rclone:`` 前缀时才放行
      （不建挂载直连的老用法），否则报错——写进去只能扫不出东西。
    """
    raw_path = str(entry.get("path") or "").strip()
    backend = normalize_storage_backend(entry.get("backend")) or MOUNT_LOCAL
    mount_id = entry.get("mount_id")
    try:
        mount_id = int(mount_id) if mount_id not in (None, "") else None
    except (TypeError, ValueError):
        mount_id = None
    if mount_id is not None:
        return mount_path(mount_id, raw_path)
    if backend == MOUNT_LOCAL:
        return raw_path
    if not raw_path:
        raise MountError(f"「{storage_backend_label(backend)}」来源需要填写目录路径")
    if detect_mount_type(raw_path) == backend:
        return raw_path
    raise MountError(
        f"「{storage_backend_label(backend)}」来源请先选择对应的存储挂载"
        f"（挂载在「服务器」页配置），或在高级模式里直接手写 {backend}: 前缀路径"
    )


def backend_matches_mount(backend: Optional[str], mount) -> bool:
    """界面选的存储后端与挂载实际类型是否一致（不一致就拒绝，别存出自相矛盾的来源）"""
    chosen = normalize_storage_backend(backend)
    actual = normalize_storage_backend(getattr(mount, "mount_type", ""))
    return bool(chosen) and chosen == actual


def _unwrap_path_wrapper(path: str) -> str:
    """剥掉外层粘进来的方括号 / 引号

    左右各剥几层，**不要求成对**：从 JSON 数组里连逗号一起复制的多元素列表，
    会被上层按逗号切成 ``["mount://1/a"`` 与 ``"mount://1/b"]`` 这种半截形状，
    只认成对括号就漏了这两种。
    """
    s = (path or "").strip()
    for _ in range(3):
        before = s
        if s and s[0] in "[\"'":
            s = s[1:].strip()
        if s and s[-1] in "]\"'":
            s = s[:-1].strip()
        if s == before:
            break
    return s


def _mount_prefix_typo(path: str) -> Optional[str]:
    """像挂载来源、但前缀没写成 ``mount://`` 时的说明（写对了返回 None）"""
    s = (path or "").strip()
    if not s:
        return None
    if is_mount_path(s):
        rest = s[len(MOUNT_PATH_PREFIX):]
        if rest and not rest.split("/", 1)[0].isdigit():
            return (f"挂载来源里的「挂载 ID」必须是数字，要写成 "
                    f"{MOUNT_PATH_PREFIX}<挂载ID>/<子目录>")
        return None
    low = s.lower()
    if low.startswith(MOUNT_PATH_PREFIX):
        return (f"挂载来源要写成小写的 {MOUNT_PATH_PREFIX}<挂载ID>/<子目录>"
                f"（前缀区分大小写）")
    if low.startswith("mount") or s.startswith("挂载"):
        return (f"挂载来源要写成 {MOUNT_PATH_PREFIX}<挂载ID>/<子目录>"
                f"（注意是英文冒号加两个斜杠）")
    return None


def explain_local_path_failure(path: str) -> str:
    """本机目录不存在时，尽量说清真原因；实在看不出来才退回通用措辞。

    只在 ``os.path.isdir`` 已经失败之后调用，所以不会误伤真实存在的本机目录。

    起因：媒体库的 ``paths`` 里存过 ``["mount://1/nastool/剧集/儿童"]`` ——
    从 JSON / 列表里连方括号一起复制过来的写法，它不是挂载路径，会被当成本机
    目录，于是**每次扫描都报「目录不存在或不可读」**，把人带去查挂载、账号和
    网络，而问题其实只在格式上。两种真实写法错误：

    - 带壳：``["mount://1/剧集/儿童"]`` / ``"mount://1/剧集/儿童"`` / ``[mount://…]``；
    - ``mount://`` 前缀没写对：全角冒号、大小写、挂载 ID 不是数字。
    """
    s = (path or "").strip()
    inner = _unwrap_path_wrapper(s)
    if inner != s:
        hint = f"去掉外层括号/引号后是 {inner}。" if inner else ""
        return (f"看起来是从 JSON 或列表里连括号/引号一起复制的：{hint}"
                f"每条来源单独写一行，不要带括号或引号")
    typo = _mount_prefix_typo(s)
    if typo:
        return typo
    return "目录不存在或不可读"


def build_library_sources(local_paths, mount_subpaths, mount_ids, db: Session,
                          library=None, *, enforce_can_list: bool = False,
                          ) -> tuple[list[LibrarySource], list[dict]]:
    """把已拆好的来源输入解析成扫描来源（旧列解析与新表读取共用，唯一实现）。

    - ``local_paths``：本机目录原文列表；
    - ``mount_subpaths``：``[(原文标签, 挂载 id, 子目录)]``；
    - ``mount_ids``：整挂载来源的挂载 id 列表；子目录胜出过滤在本函数内做，
      与旧行为逐字一致：同挂载既有子目录行又有整挂载行时只留子目录行
      （2026-09-26 演唱会事故教训），没有子目录的库仍按整挂载扫；
    - ``enforce_can_list``：True 时按能力门控——``can_list=False`` 的挂载
      不进扫描（记 failed，不静默跳过）；False 时保持旧行为（旧函数与
      回落路径用，不执法）。

    返回 ``(可用来源, 不可用来源)``：不可用来源会记进 ``failed``，扫描器
    据此**跳过清理阶段**，避免把「读不到」当成「文件已删除」。
    """
    sources: list[LibrarySource] = []
    failed: list[dict] = []

    for path in local_paths:
        if not os.path.isdir(path):
            # 写错格式（带方括号/引号、mount:// 前缀不对）也会走到这里，
            # 提示要说清真原因，别让人去查挂载和网络。
            failed.append({"label": path, "reason": explain_local_path_failure(path)})
            continue
        sources.append(LibrarySource(label=path, kind="local", path=path))

    # 一个库可能同时留下旧字段：paths 写了 mount://<id>/<子目录>，
    # mount_ids 又保留了同一个挂载 id。子目录来源已经代表这个挂载，
    # 再追加 mount_ids 的根来源会把整个云盘也扫进去（严重时把别的库内容
    # 写进当前库）。子路径优先，过滤掉重复的整挂载来源；没有 paths 子路径
    # 的库仍按 mount_ids 扫整个挂载，保持旧行为。
    subpath_mount_ids = {mount_id for _, mount_id, _ in mount_subpaths}
    mount_ids = [mount_id for mount_id in mount_ids if mount_id not in subpath_mount_ids]
    needed_ids = set(mount_ids) | subpath_mount_ids
    mounts = (
        {m.id: m for m in db.query(em.StorageMount).filter(em.StorageMount.id.in_(needed_ids)).all()}
        if needed_ids
        else {}
    )

    def _mount_source(label: str, mount_id: int, subpath: str) -> None:
        mount = mounts.get(mount_id)
        if mount is None:
            failed.append({"label": label, "reason": f"挂载 #{mount_id} 不存在（可能已被删除）"})
            return
        if not mount.is_enabled:
            failed.append({"label": label, "reason": f"挂载「{mount.name}」已停用"})
            return
        try:
            provider = build_provider(mount, db, library)
            if enforce_can_list:
                try:
                    caps = provider.capabilities()
                except Exception as exc:  # noqa: BLE001 — 声明都读不到的挂载按不可用处理
                    failed.append({"label": label, "reason": f"能力声明读取失败: {exc}"})
                    return
                if not caps.can_list:
                    failed.append({"label": label,
                                   "reason": f"挂载「{mount.name}」声明 can_list=False，能力门控跳过扫描"})
                    return
            if subpath.strip("/"):
                # 子目录必须真实存在，否则这个来源不可用（扫描器会跳过清理，避免误删条目）
                provider.list_dir(subpath)
        except MountError as exc:
            reason = f"子目录不可读: {exc}" if subpath.strip("/") else str(exc)
            failed.append({"label": label, "reason": reason})
            return
        sources.append(LibrarySource(
            label=label, kind="mount", mount=mount, provider=provider, subpath=subpath,
        ))

    for path, mount_id, rel in mount_subpaths:
        _mount_source(path, mount_id, rel)
    for mount_id in mount_ids:
        _mount_source(mount_label(mounts.get(mount_id)) if mounts.get(mount_id) else f"挂载 #{mount_id}",
                      mount_id, "/")
    return sources, failed


def library_sources(library, db: Session) -> tuple[list[LibrarySource], list[dict]]:
    """把媒体库的 ``paths`` + 挂载解析成扫描来源（旧列解析）。

    ``paths`` 里除了本机目录，还可以写 ``mount://<挂载 id>/<子目录>``（例如
    ``mount://2/video/剧集/动漫剧``），表示只扫描该挂载下的这个子目录。

    本函数是回落路径与双读基线：新表读取走
    ``mount_sources.read_library_sources``，新表读不到时回落到这里；
    双读校验（``compare_library_sources``）以本函数的输出为旧侧基线。
    不执法能力门控（门控只在新表读取里开）。
    """
    # 先把 paths 拆成「本机目录」和「mount:// 子目录」两类
    local_paths: list[str] = []
    mount_subpaths: list[tuple[str, int, str]] = []  # (原文, 挂载 id, 子目录)
    for raw in (getattr(library, "paths", "") or "").split(","):
        path = raw.strip()
        if not path:
            continue
        parsed = parse_mount_path(path)
        if parsed is not None:
            mount_subpaths.append((path, parsed[0], parsed[1]))
        else:
            local_paths.append(path)

    return build_library_sources(local_paths, mount_subpaths,
                                 parse_mount_ids(library), db, library)


def check_mount_subpath(db: Session, mount_id: int, rel: str) -> None:
    """校验 ``mount://<挂载 id>/<子目录>`` 可用：挂载存在、启用、子目录可读。

    不满足时抛 ``MountError``（说明原因），满足时静默返回。给后台保存媒体库时校验用。
    """
    mount = db.query(em.StorageMount).filter(em.StorageMount.id == mount_id).first()
    if mount is None:
        raise MountError(f"挂载 #{mount_id} 不存在（可能已被删除）")
    if not mount.is_enabled:
        raise MountError(f"挂载「{mount.name}」已停用")
    provider = build_provider(mount, db)
    if (rel or "/").strip("/"):
        provider.list_dir(rel)  # 子目录不存在/不可读时抛 MountError


def load_mount(db: Session, mount_id: int):
    return db.query(em.StorageMount).filter(em.StorageMount.id == mount_id).first()


# ==================== 解析结果缓存（FMBY 批一 #1）====================
# ``resolve_play_target`` 的输出（kind + 直链 + 静态头）按 (挂载, 类型,
# 配置指纹, rel) 在进程内缓存 ``RESOLVE_TARGET_CACHE_TTL_SEC`` 秒（默认 600，
# 0 = 关闭；启动时读一次，热路径不碰 env）。动机：播放器一次播放 = 多次
# stream 请求（起播探测 + 每个 Range 分片），此前每次请求都要重走一遍解析
# ——115 逐级列目录、rclone CLI 每次拉一个子进程、STRM 每次读文件内容。
# 学的是 FMBY 的 remote_target_cache：调度层（换链/选路）加带过期的缓存，
# 数据面（中继取流）一行不动。
#
# 硬约束（定稿 §3，防 2026-10-11 全局 token 缓存架空 SA 轮询事故重演）：
# - **凭据头禁止进缓存值**：Cookie / Authorization 一律经提供者的
#   ``cacheable_play_target`` 剥掉、命中后经 ``reattach_play_target`` 现挂，
#   凭据状态仍只归既有唯一入口维护（魂 6）。STRM 目标的 Bearer 在本层统一
#   剥/挂（它由 ``_strm_play_target`` 拼、亲和键就是 url 本身，与提供者无关）。
# - ``X-Pickcode`` 随缓存保留：它不是凭据，是 115 的失效钥匙（中继 403/
#   404/410 时靠它反查连带清本缓存，见 ``invalidate_resolve_cache_by_pickcode``）。
# - 缓存的是「解析结果快照」，不是「文件仍在」的断言——存在性另有正缓存
#   与墓碑，不在这里重复担保。同路径换文件靠 TTL 自然收敛 + 115 的 403
#   连带失效兜底，最多陈旧一个 TTL，这是本项接受的已知窗口。
# - 进程内 dict（与 115 换链缓存 / 目录缓存同口径）：api 与 ea 两进程各
#   一份、失效不广播，靠 TTL 收敛；要跨进程共享须先保证凭据已拆出，再迁
#   CacheManager 的 playback 域，另行立项。
RESOLVE_TARGET_CACHE_TTL_SEC = env_int("RESOLVE_TARGET_CACHE_TTL_SEC", 600, 0, 3600)
#: 上限与 Drive 存在性正缓存同数（10000）；满员先清过期、仍满整体清空。
RESOLVE_TARGET_CACHE_MAX = 10000

# 模块级直接初始化，不许懒初始化（同 _pan115_url_cache_lock 的口径）。
_RESOLVE_CACHE: dict = {}
_RESOLVE_CACHE_LOCK = threading.Lock()
_RESOLVE_CACHE_STATS = {"hits": 0, "misses": 0, "expired": 0, "evictions": 0}


def resolve_cache_stats() -> dict:
    """解析缓存命中情况（健康检查 / 测试用）"""
    with _RESOLVE_CACHE_LOCK:
        return {**_RESOLVE_CACHE_STATS, "entries": len(_RESOLVE_CACHE),
                "ttl_seconds": RESOLVE_TARGET_CACHE_TTL_SEC}


def reset_resolve_cache() -> None:
    """清空解析缓存与计数（测试隔离用，照 #1203 熔断 ``reset_all`` 先例）。"""
    with _RESOLVE_CACHE_LOCK:
        _RESOLVE_CACHE.clear()
        for name in _RESOLVE_CACHE_STATS:
            _RESOLVE_CACHE_STATS[name] = 0


def _resolve_cache_key(provider, rel: str) -> tuple:
    """缓存键 = (挂载 id, 类型, 配置指纹, rel)，与目录缓存同法（mounts._list_cache_key）。

    配置指纹进键：挂载改配置 / 换 Cookie 后指纹变，旧条目自然打不中，无需
    主动清理。键里**不许**放 user_id / token / Cookie——解析结果与用户无关
    （权限在播放入口已先行判定），放用户维度只会把命中率打碎。
    """
    mount = getattr(provider, "mount", None)
    return (
        getattr(mount, "id", None),
        (getattr(mount, "mount_type", "") or "").strip(),
        _config_fingerprint(provider),
        rel or "/",
    )


def _resolve_cache_get(key: tuple) -> Optional[PlayTarget]:
    """取缓存（命中返回缓存里的脱凭据副本；过期顺手删并当未命中）。TTL=0 恒未命中。"""
    if RESOLVE_TARGET_CACHE_TTL_SEC <= 0:
        return None
    now = time.monotonic()
    with _RESOLVE_CACHE_LOCK:
        row = _RESOLVE_CACHE.get(key)
        if row is not None and row[0] > now:
            _RESOLVE_CACHE_STATS["hits"] += 1
            return row[1]
        if row is not None:
            _RESOLVE_CACHE.pop(key, None)
            _RESOLVE_CACHE_STATS["expired"] += 1
        _RESOLVE_CACHE_STATS["misses"] += 1
        return None


def _resolve_cache_put(key: tuple, stored: Optional[PlayTarget]) -> None:
    if stored is None or RESOLVE_TARGET_CACHE_TTL_SEC <= 0:
        return
    now = time.monotonic()
    with _RESOLVE_CACHE_LOCK:
        if len(_RESOLVE_CACHE) >= RESOLVE_TARGET_CACHE_MAX:
            # 防无限增长：先清过期条目；仍满则整体清空（与存在性正缓存同
            # 口径；最坏只是多走几次解析，不影响正确性）
            for k in [k for k, row in _RESOLVE_CACHE.items() if row[0] <= now]:
                _RESOLVE_CACHE.pop(k, None)
            if len(_RESOLVE_CACHE) >= RESOLVE_TARGET_CACHE_MAX:
                _RESOLVE_CACHE.clear()
            _RESOLVE_CACHE_STATS["evictions"] += 1
        _RESOLVE_CACHE[key] = (now + RESOLVE_TARGET_CACHE_TTL_SEC, stored)


def invalidate_resolve_cache(mount_id: Optional[int] = None,
                             rel: Optional[str] = None) -> None:
    """清解析缓存：mount_id + rel 只清一条；只给 mount_id 清整个挂载；都不给全清。"""
    with _RESOLVE_CACHE_LOCK:
        if mount_id is None and rel is None:
            _RESOLVE_CACHE.clear()
            return
        for key in [k for k in _RESOLVE_CACHE
                    if (mount_id is None or k[0] == mount_id)
                    and (rel is None or k[3] == rel)]:
            _RESOLVE_CACHE.pop(key, None)


def invalidate_resolve_cache_by_pickcode(pickcode: str) -> None:
    """按 X-Pickcode 反查清解析缓存（115 中继上游 403/404/410 时连带调用）。

    只清 115 换链缓存不够：解析缓存里还留着同一条死链，不连带清会被它
    续命最多一个 TTL。永不抛——失效失败最多多留 TTL 秒陈旧。
    """
    if not pickcode:
        return
    with _RESOLVE_CACHE_LOCK:
        for key in [k for k, row in _RESOLVE_CACHE.items()
                    if (row[1].headers or {}).get("X-Pickcode") == pickcode]:
            _RESOLVE_CACHE.pop(key, None)


def invalidate_resolve_cache_for_path(file_path: Optional[str]) -> None:
    """按条目 ``file_path`` 清一条解析缓存（扫描确认丢失 / 管理端删条目 /
    分享补开见 404 时，在 ``invalidate_drive_exists_for_item`` 的调用点同点位
    连带调用）。非挂载路径直接跳过；永不抛，不许打断调用方的主流程。"""
    try:
        parsed = parse_mount_path(file_path)
        if parsed is None:
            return
        invalidate_resolve_cache(parsed[0], parsed[1])
    except Exception:  # noqa: BLE001 — 失效是尽力而为，TTL 兜底
        logger.debug("解析缓存按路径失效失败 path=%s", file_path, exc_info=True)


def _stored_play_target(provider, target: PlayTarget, rel: str) -> Optional[PlayTarget]:
    """解析结果进缓存前的脱凭据副本；不可安全缓存返回 None。

    .strm 目标的凭据只有 ``_strm_play_target`` 拼的 Drive Bearer（亲和键是
    url 本身），与提供者无关，在这一层统一剥；其余目标交给提供者自己的
    ``cacheable_play_target`` 拆分。
    """
    if _is_strm_name(rel):
        headers = dict(target.headers or {})
        headers.pop("Authorization", None)
        return PlayTarget(target.kind, target.value, headers)
    return provider.cacheable_play_target(target)


def _reattach_cached_target(provider, stored: PlayTarget, rel: str) -> PlayTarget:
    """缓存命中后现挂凭据，返回一份新 PlayTarget（缓存里的对象不外借）。"""
    if _is_strm_name(rel):
        headers = dict(stored.headers or {})
        from backend.emby_server import drive_auth
        # 与 _strm_play_target 当初的拼法逐字一致：亲和键缺省就是 url 本身
        headers.update(drive_auth.drive_auth_headers(stored.value))
        return PlayTarget(stored.kind, stored.value, headers)
    return provider.reattach_play_target(stored)


def resolve_play_target(file_path: Optional[str], db: Session, library=None, *,
                        anchor_mount=None) -> PlayTarget:
    """把条目的 ``file_path`` 解析成播放/探测目标

    - 本机路径（含 ``.strm``）→ 直接读文件，strm 读内容是直链；
    - ``mount://<id>/<rel>`` → 由提供者解析成远程直链（带鉴权头，只在本机使用）。

    条目没有路径时（虚拟库聚合条目、容器类型、源文件被删后重扫前的残留行）
    明确报 MountError → 上层转成 404，而不是让 ``.lower()`` 抛 500。

    ``anchor_mount``（清单第 8 项身份锚双读）：调用方按身份锚定位到的挂载。
    只有当它与路径解析出的挂载 id 一致且启用时才采用（跳过一次 load_mount
    查询）；不一致 / 停用 / 为空时一律走现行路径——锚只确认挂载身份，绝不把
    播放重定向到路径之外的挂载，rel 仍取自 file_path。
    """
    if not file_path:
        raise MountError("该条目没有可播放的媒体路径（虚拟库聚合条目或源文件已丢失）")
    parsed = parse_mount_path(file_path)
    if parsed is None:
        return local_play_target(file_path)
    mount_id, rel = parsed
    mount = None
    if (anchor_mount is not None
            and getattr(anchor_mount, "id", None) == mount_id
            and getattr(anchor_mount, "is_enabled", False)):
        mount = anchor_mount
    if mount is None:
        mount = load_mount(db, mount_id)
    if mount is None:
        raise MountError(f"挂载 #{mount_id} 不存在（该条目来自已删除的挂载）")
    if not mount.is_enabled:
        raise MountError(f"挂载「{mount.name}」已停用")
    provider = build_provider(mount, db, library)
    # 解析缓存的查询必须排在 load_mount + is_enabled 检查**之后**：停用挂载
    # 不许被 TTL 内的缓存残量短路（定稿 §5 的失效矩阵钉死这一条顺序）。
    cache_key = _resolve_cache_key(provider, rel)
    stored = _resolve_cache_get(cache_key)
    if stored is not None:
        return _reattach_cached_target(provider, stored, rel)
    target = provider.resolve_final(rel)
    if target.kind == "url":
        target.value = validate_remote_url(target.value)
        _resolve_cache_put(cache_key, _stored_play_target(provider, target, rel))
    return target


def media_exists(file_path: str, db: Session, library=None) -> bool:
    """条目对应的媒体当前是否可读（本机路径 / 远程挂载都支持）"""
    if not file_path:
        return False
    parsed = parse_mount_path(file_path)
    if parsed is None:
        return os.path.exists(file_path)
    mount = load_mount(db, parsed[0])
    if mount is None or not mount.is_enabled:
        return False
    try:
        return build_provider(mount, db, library).exists(parsed[1])
    except MountError:
        return False


# ==================== 字幕模块的挂载解析钩子 ====================

def mount_source_resolver(path: str):
    """给 ``subtitles`` 模块用的解析器：``mount://<id>/<rel>`` → ``(直链, 请求头)``

    字幕模块只处理路径字符串、没有数据库会话；这里开一个短命 Session 自己解析
    （与扫描 / 播放同一口径），避免把 db 传进一个工具模块。
    """
    if parse_mount_path(path) is None:
        return None
    from backend.database import SessionLocal

    db = SessionLocal()
    try:
        target = resolve_play_target(path, db)
    except MountError as exc:
        logger.warning("字幕来源解析失败 %s: %s", path, exc)
        return None
    finally:
        db.close()
    return (target.value, target.headers) if target.kind == "url" else None


subtitles.register_mount_resolver(mount_source_resolver)


# ==================== rclone / drive 挂载类型 ====================
# rclone 的实现单独放在 mount_rclone.py（依赖 rclone 命令 / RC，不适合塞进本文件）；
# drive 的实现单独放在 mount_drive.py（Drive API 直调，凭据层走 drive_changes 横切能力）。
# 两者导入时调用 register_providers，因此提供者在本模块定义完 _PROVIDERS 之后就已经可用了。
# 注意：这里必须显式自导入——EA 进程（aetrix-ea）只导入本模块，不走
# backend.api.servers / change_watcher 的导入副作用，不自导入会导致 drive 在其进程里未注册。
from backend.emby_server import mount_rclone  # noqa: E402,F401  (导入即注册)
from backend.emby_server import mount_drive  # noqa: E402,F401  (导入即注册)
