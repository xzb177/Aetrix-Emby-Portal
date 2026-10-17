"""刮削图片本地化：把 TMDB 的远程图落成本服务器上的文件

**为什么要有这一层**：条目的封面原先只有 ``primary_image_url``（TMDB CDN 地址），
客户端每取一次图都要由服务器**代理去第三方拉一次**——源站慢一点、被限速、临时不可达，
整个媒体库的封面就跟着抖；条件请求（304）只省了带宽，冷启动与缓存穿透的延迟还是压在用户身上。

本地化之后图片走本机磁盘：客户端到本服务器一次往返拿到，不依赖第三方可达性。
磁盘上的那份是**缓存**，不是唯一副本：

- 条目上仍然留着远程图地址，缓存文件被清掉/被删掉都能自愈（取图时按需再落一份，见
  ``media_routes.item_image``）；
- 维护周期会清掉没被任何条目引用的文件（例如换过海报的旧文件），并按上限从旧到新淘汰。

配置（都在环境里，缺省值面向小机器）：

======================  ==========================================================
``EMBY_LOCALIZE_IMAGES``  ``0`` 关闭（默认开启）
``EMBY_IMAGE_DIR``        图片缓存目录（默认 ``<EMBY_TRANSCODE_DIR>/images``）
``EMBY_IMAGE_CACHE_MB``   缓存上限，默认 2048 MB
``EMBY_IMAGE_GRACE_SECONDS`` 新增文件保护期，默认 3600 秒（刚落盘、还没写库的文件不被清理）
``EMBY_IMAGE_TIMEOUT``    单张图下载超时（秒），默认 15
``EMBY_IMAGE_RETRIES``    预热下载的重试次数（默认 2，共 3 次尝试），0.5s 起指数退避加抖动
======================  ==========================================================
"""
from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import random
import re
import select
import subprocess
import sys
import threading
import time
from typing import Optional

from backend.emby_server import scan_progress as progress

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_MAX_IMAGE_BYTES = 8 * 1024 * 1024      # 单张图上限：海报/背景图不该有更大的
_LOCKS: dict = {}
_LOCKS_LOCK = threading.Lock()
_STATS = {"downloaded": 0, "failed": 0, "served": 0, "throttled": 0}

# ---------- 缩略图 ----------
# 海报墙下发 TMDB 原图（500KB–2MB/张）是带宽杀手：缩略图把单张压到几十 KB。
# 尺寸全部参数化（EMBY_THUMB_WIDTHS），只提供能力，不写死业务尺寸。
# “有界”旋钮（最大边/超大图拒绝/并发信号量/内存上限）见下面的 _env_int 之后那段。
_THUMB_QUALITY = 82            # JPEG 质量：82 是体积/观感的甜点
_THUMB_WIDTHS_ENV = "EMBY_THUMB_WIDTHS"

try:  # Pillow 缺席时模块仍可导入（缩略图能力静默关闭，原图逻辑不受影响）
    from PIL import Image as _PILImage
    _PIL_OK = True
except Exception:  # noqa: BLE001
    _PILImage = None
    _PIL_OK = False

# ---------- 缩略图引擎：libvips 跑在常驻 worker 子进程 ----------
# 为什么 vips 不能直接 import 进本进程：
# libvips 依赖的系统库（libharfbuzz/libfreetype）与 Pillow manylinux wheel 自带的
# 同名库版本不同；同进程加载两份会破坏 Pillow 的文字排版——实测：封面渲染用的
# 中文字体在 shaping 阶段读到垃圾字形 advance，直接抛 DecompressionBombError，
# 且每次垃圾值都不同（native 内存踩坏）。封面画字必须留在 Pillow（红线），所以
# vips 只跑在隔离的子进程里：父进程永不 import pyvips。
# 形态：一个常驻 worker 子进程（启动开销只付一次），父子之间走行式 JSON 协议；
# 子进程崩了自动重建、单张超时/失败则回退 Pillow。缩略图落盘缓存后，
# 后续请求直接命中文件，不再经过这里。
_VIPS_TIMEOUT = 120  # worker 单张超时（秒）：正常 < 5s，超时则回退 Pillow
_vips_ok_cache: bool | None = None
_vips_proc = None
_vips_proc_lock = threading.Lock()

_VIPS_WORKER_PY = r"""
import sys, json
import pyvips

def handle(req):
    thumb = pyvips.Image.thumbnail(req["src"], req["box_w"], height=req["box_h"],
                                   size="down", crop="none", no_rotate=True)
    if thumb.hasalpha():
        thumb = thumb.extract_band(0, n=3)  # 只丢通道不做合成，对齐 Pillow convert("RGB")
    thumb.write_to_file(req["tmp"], Q=req["q"], optimize_coding=True,
                        interlace=True, strip=True)
    return {"ok": True}

def main():
    inp, out = sys.stdin, sys.stdout
    for line in inp:
        line = line.strip()
        if not line:
            continue
        try:
            resp = handle(json.loads(line))
        except Exception as exc:  # noqa: BLE001 — 报错回父进程，由父进程回退 Pillow
            resp = {"ok": False, "error": str(exc)[:300]}
        out.write(json.dumps(resp) + "\n")
        out.flush()

main()
"""


def _vips_probe() -> bool:
    """一次性子进程里 import pyvips 探活：成功返回 True，本进程不受影响。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", "import pyvips; pyvips.version(0)"],
            capture_output=True, timeout=30,
        )
        return proc.returncode == 0
    except Exception:  # noqa: BLE001 — 超时/缺解释器等一律视为不可用
        return False


def vips_available() -> bool:
    """libvips 是否可用（探活结果按进程缓存；测试可直接改 _vips_ok_cache）"""
    global _vips_ok_cache
    if _vips_ok_cache is None:
        _vips_ok_cache = _vips_probe()
    return _vips_ok_cache


def _vips_spawn():
    """起一个常驻 worker（-u 无缓冲，stdout 行协议）"""
    return subprocess.Popen(
        [sys.executable, "-u", "-c", _VIPS_WORKER_PY],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )


def _vips_call(req: dict) -> dict:
    """向 worker 发一次请求；worker 死了就重建后重试一次，仍失败抛异常。"""
    global _vips_proc
    payload = (json.dumps(req) + "\n").encode("utf-8")
    with _vips_proc_lock:
        for _ in range(2):
            proc = _vips_proc
            if proc is None or proc.poll() is not None:
                proc = _vips_proc = _vips_spawn()
            try:
                proc.stdin.write(payload)
                proc.stdin.flush()
                ready, _, _ = select.select([proc.stdout], [], [], _VIPS_TIMEOUT)
                if not ready:
                    raise RuntimeError(f"vips worker 响应超时（{_VIPS_TIMEOUT}s）")
                line = proc.stdout.readline()
                if not line:
                    raise RuntimeError("vips worker 异常退出")
                return json.loads(line.decode("utf-8"))
            except (OSError, ValueError, RuntimeError) as exc:
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
                _vips_proc = None
                last = exc
        raise RuntimeError(f"vips worker 不可用: {last}")


def pil_available() -> bool:
    return _PIL_OK


def thumb_engine() -> str:
    """当前缩略图引擎：vips 优先，Pillow 兜底，都没有返回 "none"。"""
    if vips_available():
        return "vips"
    if _PIL_OK:
        return "pillow"
    return "none"


def thumb_widths() -> list[int]:
    """后台预生成的缩略图宽度档（环境变量 EMBY_THUMB_WIDTHS，逗号分隔）

    默认 160,320,640：分别对应列表行、海报卡片、详情/背景。
    早先默认是 320,640，但前端实际请求 160/300/342，一次都命不中预热产物，
    100% 的缩略图都落到请求线程里现做——反而把线程池打满。
    """
    raw = (os.getenv(_THUMB_WIDTHS_ENV) or "160,320,640").strip()
    out: list[int] = []
    for part in raw.split(","):
        try:
            w = int(part.strip())
        except ValueError:
            continue
        if 1 <= w <= _THUMB_MAX_DIM and w not in out:
            out.append(w)
    return out or [320]


def thumb_variant_path(path: str, max_width: int | None = None,
                       max_height: int | None = None) -> str:
    """缩略图的文件路径（内容寻址：原图 digest + 尺寸，后缀统一 .jpg）

    - 原图在我们自己的图片缓存里（``local_path`` 落下的 ``<sha1>.jpg``）：文件名本身
      就是内容寻址的 digest，直接沿用；
    - 原图在缓存**之外**（扫描到的同目录外挂图：``poster.jpg`` / ``folder.jpg`` /
      ``cover.jpg`` / ``fanart.jpg``……）：文件名在全库里大量重名，绝不能拿来当 digest——
      否则所有叫 ``poster.jpg`` 的海报共用一张 ``poster_w320.jpg``，先被请求的那部片子的
      缩略图会发给后面每一部（首页「本周入库」海报与片名对不上就是这么来的）。
      这里改用「绝对路径 + mtime + 大小」的 sha1：不同条目各有各的缩略图，
      外挂图被替换后也会自然换新。
    """
    digest = os.path.basename(path).rsplit(".", 1)[0]
    if not is_cached_path(path):
        try:
            st = os.stat(path)
            sig = f"{os.path.abspath(path)}|{int(st.st_mtime)}|{st.st_size}"
        except OSError:
            sig = os.path.abspath(path)
        digest = hashlib.sha1(sig.encode("utf-8", "ignore")).hexdigest()
    tag = ""
    if max_width:
        tag += f"_w{max_width}"
    if max_height:
        tag += f"_h{max_height}"
    return os.path.join(image_dir(), digest + tag + ".jpg")


def _clamp_dim(value) -> int | None:
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    return min(v, _THUMB_MAX_DIM)


def _snap_to_configured_width(value) -> int | None:
    """把请求宽度吸附到「已配置档位」上；无匹配返回 None（调用方回退原图）。

    为什么必须有这一步：``resized_variant`` 原来只钳上限，于是 ``?maxWidth=1..4096``
    里任何值都会真的落一个 ``<digest>_wN.jpg``。而 prune 会把**被引用原图的每一个
    缩略图**都标进 protected，容量淘汰对 protected 一律跳过——于是这些按需生成的
    变体既不受 ``EMBY_IMAGE_CACHE_MB`` 约束也永远不会被清理。循环请求
    ``maxWidth=1..2000`` 就能为每张封面生成上千个永不淘汰的文件，撑爆所在卷。

    吸附到配置档位后：文件总数有上界（档位数 × 原图数），且预热命中率回到 100%
    （原来前端请求 342、预热 320，一次都命不中，等于白预热）。
    """
    v = _clamp_dim(value)
    if not v:
        return None
    widths = thumb_widths()
    if v in widths:
        return v
    # 就近吸附：取不小于请求宽度的最小档（宁可略大，也不要生成不在册的变体）
    larger = [w for w in widths if w >= v]
    return min(larger) if larger else max(widths)


def _probe_dims(path: str):
    """只读图片头拿尺寸（不解码像素）：Pillow 读头即可，失败返回 None。

    注意：不用 vips 探——父进程永不加载 libvips（见上）；读头这点活 Pillow 足够。
    """
    if _PIL_OK:
        try:
            with _PILImage.open(path) as im:
                return im.size
        except Exception:  # noqa: BLE001
            pass
    return None


def _thumb_vips(path: str, target: str, box_w: int, box_h: int) -> str:
    """libvips 路径：发给常驻 worker 子进程做（本进程不加载 libvips，见上）。

    行为对齐点（与现行 Pillow 路径一致）：
    - 只缩小不放大：size="down"；
    - 去 alpha 只丢通道不做合成（对齐 Pillow convert("RGB") 的丢弃语义）；
    - no_rotate=True：现行 Pillow 路径不做 EXIF 旋转，vips 默认会自动转，
      这里显式关掉以保持输出一致（差异与取舍见 PR 描述）；
    - JPEG 质量=_THUMB_QUALITY、optimize_coding/interlace 对齐
      Pillow 的 optimize/progressive、strip=True 对齐 Pillow 不写 EXIF。
    worker 失败/超时一律抛异常，由调用方回退 Pillow。
    """
    # 临时文件带 .jpg 后缀：vips 按扩展名选编码器；换名后与现行命名一致
    tmp = f"{target}.part{os.getpid()}.jpg"
    resp = _vips_call({"src": path, "tmp": tmp, "box_w": box_w, "box_h": box_h,
                       "q": _THUMB_QUALITY})
    if not resp.get("ok"):
        raise RuntimeError(f"vips worker 失败: {resp.get('error')}")
    if not os.path.isfile(tmp):
        raise RuntimeError("vips worker 未产出文件")
    return tmp


def _vips_shutdown():
    """进程退出时回收 worker（子进程 stdin 是管道，父进程退出它会自行结束）"""
    global _vips_proc
    proc, _vips_proc = _vips_proc, None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


atexit.register(_vips_shutdown)


def _thumb_pillow(path: str, target: str, box_w: int, box_h: int) -> str:
    """Pillow 路径（现状行为，原样保留作兜底）"""
    with _PILImage.open(path) as img:
        img.load()
        if img.mode in ("RGBA", "LA", "PA", "P"):
            img = img.convert("RGB")
        img.thumbnail((box_w, box_h), _PILImage.LANCZOS)
        tmp = f"{target}.part{os.getpid()}"
        img.save(tmp, "JPEG", quality=_THUMB_QUALITY,
                 optimize=True, progressive=True)
    return tmp


def resized_variant(path: str, max_width=None, max_height=None) -> str:
    """按需生成等比缩放版本，返回缩略图路径；不需要/失败返回空串（调用方用原图）

    - 只缩小不放大：原图本来就小就直接返回 ""（省一次转码）；
    - 超大原图（默认 >25MP）直接拒绝：Pillow 解码要吃掉几百 MB 内存；
    - 有界并发：同一时刻最多 _THUMB_CONCURRENCY（默认 4）个转码，海报墙
      几十张同时请求也不会把 CPU 打满（海报墙/预热/补生成三条路共用信号量）；
    - **非阻塞**获取名额：抢不到就返回 "" 让调用方发原图，绝不排队。
      原来用 ``with _GEN_SEMAPHORE`` 阻塞等待，而这个函数跑在 Starlette 的
      anyio 线程池里（上限 40）。海报墙冷缓存 60 张并发 → 40 个请求线程全部
      进入本函数，其中 4 个真在转码、36 个攥着线程阻塞等名额；这段时间内
      **所有其它同步端点都拿不到线程**，整个部署表现为假死。
      有界资源 + 无界排队 = 限流形同虚设。
    - 同一尺寸并发只生成一次（沿用 _LOCKS 的按路径单飞）；
    - 写文件走「临时文件 → fsync → os.replace」，中途被杀不留半张图。
    - 缩略图引擎：libvips（pyvips）优先、Pillow 兜底（见 _thumb_vips/_thumb_pillow）。
    """
    if not (_PIL_OK or vips_available()):
        return ""
    # 宽度吸附到已配置档位：变体文件总数因此有上界，否则按需生成的文件
    # 会被 prune 当成"被引用的缩略图"永久保护，既不受容量上限约束也永不清理
    width = _snap_to_configured_width(max_width) if max_width else None
    height = _clamp_dim(max_height)
    if not width and not height:
        return ""
    if not path or not os.path.isfile(path):
        return ""
    target = thumb_variant_path(path, width, height)
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        return target
    with _LOCKS_LOCK:
        lock = _LOCKS.get(target) or threading.RLock()
        _LOCKS[target] = lock
    try:
        with lock:
            if os.path.isfile(target) and os.path.getsize(target) > 0:
                return target
            # 非阻塞抢名额：抢不到就直接回原图，绝不攥着请求线程排队
            if not _GEN_SEMAPHORE.acquire(blocking=False):
                _STATS["throttled"] = _STATS.get("throttled", 0) + 1
                return ""
            try:
                box_w = width or 10 ** 7
                box_h = height or 10 ** 7
                tmp = None
                # 政策检查先行（只读图片头，不解码）：>25MP 拒绝、已够小不转。
                # 放在信号量之后、引擎之前，两条引擎走同一套口径。
                dims = _probe_dims(path)
                if dims is None:
                    return ""  # 头都读不出来：坏文件，调用方发原图
                ow, oh = dims
                if ow * oh > _THUMB_MAX_PIXELS:
                    logger.info("原图过大(%dMP)跳过缩略图 %s",
                                ow * oh // 1_000_000, path)
                    return ""
                if ow <= box_w and oh <= box_h:
                    return ""  # 已经够小，不用转
                os.makedirs(image_dir(), exist_ok=True)
                # 引擎调度：vips 优先；单张失败换另一条引擎重试一次
                engine = "vips" if vips_available() else "pillow"
                try:
                    if engine == "vips":
                        tmp = _thumb_vips(path, target, box_w, box_h)
                    else:
                        tmp = _thumb_pillow(path, target, box_w, box_h)
                except Exception as exc:  # noqa: BLE001 — 单张引擎失败，换引擎重试
                    logger.info("缩略图%s引擎失败，换引擎重试 %s: %s",
                                engine, path, exc)
                    if engine == "vips" and _PIL_OK:
                        tmp = _thumb_pillow(path, target, box_w, box_h)
                    elif engine == "pillow" and vips_available():
                        tmp = _thumb_vips(path, target, box_w, box_h)
                    else:
                        raise
                with open(tmp, "rb") as fh:
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
                tmp = None  # 已换名成功，不用再清
            except Exception as exc:  # noqa: BLE001 — 缩略图失败只是"没小图"，原图照发
                logger.info("缩略图生成失败 %s: %s", path, exc)
                if tmp:
                    try:
                        if os.path.isfile(tmp):
                            os.remove(tmp)
                    except OSError:
                        pass
                return ""
            finally:
                _GEN_SEMAPHORE.release()  # 必须在 try 上，否则提前 return 会漏放
            return target
    finally:
        with _LOCKS_LOCK:
            _LOCKS.pop(target, None)


# ---------- 后台预热队列 ----------
# 入库/刮削落下原图后，把缩略图生成扔进这个单线程队列：不阻塞入库主流程，
# 单线程串行也天然限速，不会跟扫描抢 CPU。
_THUMB_QUEUE = None  # queue.Queue[str]，延迟初始化
_THUMB_QUEUE_LOCK = threading.Lock()


def _thumb_worker() -> None:
    import queue as _queue_mod

    assert _THUMB_QUEUE is not None
    while True:
        path = _THUMB_QUEUE.get()
        try:
            if path and os.path.isfile(path):
                for w in thumb_widths():
                    resized_variant(path, max_width=w)
        except Exception as exc:  # noqa: BLE001
            logger.info("缩略图预热失败 %s: %s", path, exc)
        finally:
            _THUMB_QUEUE.task_done()


def enqueue_thumb_warm(path: str) -> None:
    """原图落盘后调用：后台生成各档缩略图。失败/关闭都静默（按需生成会兜底）。"""
    global _THUMB_QUEUE
    if not (_PIL_OK or vips_available()) or not path:
        return
    try:
        import queue as _queue_mod

        with _THUMB_QUEUE_LOCK:
            if _THUMB_QUEUE is None:
                _THUMB_QUEUE = _queue_mod.Queue()
                t = threading.Thread(target=_thumb_worker, name="thumb-warm",
                                     daemon=True)
                t.start()
            q = _THUMB_QUEUE
        q.put_nowait(path)
    except Exception:  # noqa: BLE001 — 预热永远不能影响主流程
        pass


def _env_flag(name: str, default: bool = True) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int((os.getenv(name) or "").strip() or default))
    except ValueError:
        return default


# ---------- 缩略图“有界”旋钮（借鉴 go-emby：并发 / 内存 / 尺寸三处设上限） ----------
# 海报墙一次几十张图同时请求缩略图：不限并发会把 CPU 打满，不限内存会把服务
# 器吃光，不限尺寸会被恶意参数拖死。这里三处都有上限，全部可环境变量调。
_THUMB_MAX_DIM = _env_int("EMBY_THUMB_MAX_DIM", 4096, 64)
# 单次请求允许的最大边长（防滥用）：超出钳制到这个值
_THUMB_MAX_PIXELS = _env_int("EMBY_THUMB_MAX_MEGAPIXELS", 25, 1) * 1_000_000
# 原图超过这么多像素直接拒绝转码（Pillow 解码 25MP ≈ 75MB 内存，再大不碰）
_THUMB_CONCURRENCY = _env_int("EMBY_THUMB_CONCURRENCY", 4, 1)
# 同时转码的缩略图数（信号量）：海报墙/预热/补生成三条路共用一把
_GEN_SEMAPHORE = threading.Semaphore(_THUMB_CONCURRENCY)
# 同时进行的远程下载数：下载吃带宽，但**同样占住请求线程**——_download 超时默认
# 15s，一张打不通的远程图就能占死一个 anyio 线程。海报墙 40 张冷缓存同时进来
# 会把线程池占满，其它同步端点（/api/*、管理后台）全部拿不到线程 → 服务假死。
# 抢不到名额就直接退回远程地址（调用方本来就支持），不排队。
_DOWNLOAD_CONCURRENCY = _env_int("EMBY_IMAGE_DL_CONCURRENCY", 8, 1)
_DL_SEMAPHORE = threading.Semaphore(_DOWNLOAD_CONCURRENCY)
_THUMB_MEM_CAP = _env_int("EMBY_THUMB_MEM_MB", 32, 0) * 1024 * 1024
# 内存缓存总量上限（默认 32MB）：一张缩略图几十 KB，32MB ≈ 缓存上千张热图
_THUMB_MEM_ENTRY_MAX = 2 * 1024 * 1024  # 单条目上限：超过 2MB 的不进内存（异常）


class _ThumbMemCache:
    """缩略图内存 LRU：总量有上限，key 是内容寻址文件名（≈ ETag:宽:高）

    缩略图文件名 = <原图 sha1>_w320.jpg：原图一变文件名就变，宽高也在名
    字里，所以 key 天然等价于「ETag:宽:高」，不会取到过期内容。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, bytes] = {}
        self._bytes = 0

    def get(self, key: str):
        with self._lock:
            data = self._data.get(key)
            if data is None:
                return None
            # LRU：命中的挪到队尾
            del self._data[key]
            self._data[key] = data
            return data

    def put(self, key: str, data: bytes) -> None:
        if not key or not data or len(data) > _THUMB_MEM_ENTRY_MAX:
            return
        if _THUMB_MEM_CAP <= 0:
            return
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._bytes -= len(old)
            self._data[key] = data
            self._bytes += len(data)
            while self._data and self._bytes > _THUMB_MEM_CAP:
                oldest = next(iter(self._data))  # py3.7+ dict 有序：队头最老
                self._bytes -= len(self._data.pop(oldest))

    def drop(self, key: str) -> None:
        with self._lock:
            old = self._data.pop(key, None)
            if old is not None:
                self._bytes -= len(old)

    def snapshot(self) -> dict:
        with self._lock:
            return {"entries": len(self._data), "bytes": self._bytes,
                    "cap_bytes": _THUMB_MEM_CAP}


_THUMB_MEM = _ThumbMemCache()


def thumb_mem_get(name: str):
    """内存缓存读（未命中返回 None，调用方回退读盘）"""
    return _THUMB_MEM.get(name)


def thumb_mem_put(name: str, data: bytes) -> None:
    """内存缓存写（超上限自动淘汰最老，失败静默）"""
    _THUMB_MEM.put(name, data)


def thumb_mem_drop(name: str) -> None:
    """删文件时顺手清内存（prune 用）"""
    _THUMB_MEM.drop(name)


def is_thumb_variant(path: str) -> bool:
    """是不是缩略图文件（服务层用它决定走内存缓存还是 FileResponse）"""
    return _thumb_digest(os.path.basename(path or "")) is not None


def pick_dim(*values) -> int | None:
    """多个尺寸别名取第一个合法值：标准 Emby maxWidth/maxHeight 优先，其次 w/h"""
    for v in values:
        d = _clamp_dim(v)
        if d:
            return d
    return None


def enabled() -> bool:
    return _env_flag("EMBY_LOCALIZE_IMAGES", True)


def image_dir() -> str:
    root = os.getenv("EMBY_IMAGE_DIR", "").strip()
    if not root:
        base = os.getenv("EMBY_TRANSCODE_DIR", "/tmp/emby_transcode")
        root = os.path.join(base, "images")
    return root


def is_cached_path(path: Optional[str]) -> bool:
    """这个路径是不是我们自己的图片缓存（用来判断是不是「本地化过的图」）"""
    if not path or path.startswith("http"):
        return False
    root = os.path.abspath(image_dir())
    return os.path.abspath(path).startswith(root + os.sep)


def local_path(url: str) -> str:
    """远程图的本地文件路径（内容寻址：同一张图只落一份）"""
    digest = hashlib.sha1(url.encode("utf-8", "ignore")).hexdigest()[:24]
    ext = os.path.splitext(url.split("?", 1)[0])[1].lower()
    if ext not in _IMAGE_EXTS:
        ext = ".jpg"
    return os.path.join(image_dir(), digest + ext)


def _download_attempts() -> int:
    """图片下载总尝试次数 = 1 + ``EMBY_IMAGE_RETRIES``（默认 2 次重试，共 3 次）"""
    return 1 + max(0, int(os.getenv("EMBY_IMAGE_RETRIES", "2") or 2))


def _fetch_once(url: str, timeout: int) -> tuple:
    """单次下载：返回 (内容, 是否值得重试)（问题一：以前没有重试这个概念）

    瞬态失败（网络异常 / 429 / 5xx）→ ``(None, True)``——网络抖动时该退避重打；
    确定性失败（404、超大、内容类型不对，重试也不会变）→ ``(None, False)``。
    """
    try:
        import httpx

        # v2.42.9：图片走 image.tmdb.org（另一个 CDN，不吃 api_key 配额），单独计时——
        # 它以前既不占 TMDB 限流、也不进任何可见指标，是整条链上最容易被忽视的尾巴
        with progress.stage_timer("image_dl"):
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.get(url)
        if resp.status_code >= 429 or resp.status_code >= 500:
            logger.info("图片下载暂不可达（HTTP %s，将重试）: %s", resp.status_code, url)
            return None, True
        if resp.status_code >= 400:
            logger.info("图片本地化跳过（HTTP %s）: %s", resp.status_code, url)
            return None, False
        content = resp.content or b""
        if not content or len(content) > _MAX_IMAGE_BYTES:
            logger.info("图片本地化跳过（大小 %s 字节）: %s", len(content), url)
            return None, False
        ctype = (resp.headers.get("content-type") or "").lower()
        if ctype and not ctype.startswith("image/"):
            logger.info("图片本地化跳过（内容类型 %s）: %s", ctype, url)
            return None, False
        return content, False
    except Exception as exc:  # noqa: BLE001 — 第三方取不到图不该影响刮削/播放
        logger.info("图片下载失败（将重试）: %s（%s）", url, exc)
        return None, True


def _download(url: str, attempts: Optional[int] = None) -> Optional[bytes]:
    """下载远程图：瞬态失败**指数退避重试**，重试耗尽返回 None（退回远程地址）

    ``attempts=None`` 取 ``_download_attempts()``（``EMBY_IMAGE_RETRIES``，默认 3）。
    确定性失败不浪费重试；重试耗尽打 **WARNING** ——旧实现只打 INFO，
    「图片下不下来」在日志里完全看不见（问题一的可见性缺口）。
    """
    if attempts is None:
        attempts = _download_attempts()
    attempts = max(1, int(attempts))
    timeout = _env_int("EMBY_IMAGE_TIMEOUT", 15, 1)
    delay = 0.5
    for n in range(1, attempts + 1):
        content, retryable = _fetch_once(url, timeout)
        if content:
            return content
        if not retryable or n >= attempts:
            if retryable:  # 确定性失败已在 _fetch_once 里各自打了日志
                logger.warning("图片下载失败（尝试 %s 次后放弃，退回远程图）: %s", n, url)
            return None
        # 加抖动：8 个 worker 同时撞上抖动时不要齐步重打
        time.sleep(delay + random.random() * 0.25)
        delay = min(delay * 2, 4.0)
    return None


def localize(url: Optional[str], allow_download: bool = True,
             attempts: int = 1) -> str:
    """把远程图落成本地文件，返回本地路径；失败或未开启返回空串

    同一张图并发请求只下载一次（按 URL 单飞）；文件已经存在就直接复用。
    写文件走「临时文件 → fsync → os.replace」：中途被杀不会留下半张图。

    ``allow_download=False``：只认「文件已经在本地」，不在这里发 HTTP。**数据库写事务里
    必须用这一档** —— 一张图超时 15s，就等于攥着写锁 15s。v2.42.9 把下载统一挪到 IO 阶段
    （见 ``prewarm``），写库阶段只落字段；没预热上的图交给取图时的按需自愈（media_routes）。

    ``attempts``：下载尝试次数。默认 1 —— 取图时的按需自愈要快，失败了客户端
    本来就能退回远程地址；IO 阶段的 ``prewarm`` 传 ``_download_attempts()``
    （`EMBY_IMAGE_RETRIES`，默认 3 次 + 指数退避），那才是刮削链上「图片下不下来」
    的主战场（问题一）。
    """
    if not url or not url.startswith("http") or not enabled():
        return ""
    path = local_path(url)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    if not allow_download:
        return ""
    with _LOCKS_LOCK:
        lock = _LOCKS.get(path) or threading.RLock()
        _LOCKS[path] = lock
    with lock:
        try:
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return path
            # 非阻塞抢下载名额：抢不到就退回远程地址（调用方本来就支持），
            # 绝不攥着请求线程排队——下载超时 15s，排队会把整个线程池拖死
            if not _DL_SEMAPHORE.acquire(blocking=False):
                _STATS["throttled"] += 1
                return ""
            try:
                content = _download(url, attempts=attempts)
            finally:
                _DL_SEMAPHORE.release()
            if not content:
                _STATS["failed"] += 1
                return ""
            os.makedirs(image_dir(), exist_ok=True)
            tmp = f"{path}.part{os.getpid()}"
            with open(tmp, "wb") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            _STATS["downloaded"] += 1
            # 原图落盘后，后台预热各档缩略图（不阻塞调用方，失败静默）
            enqueue_thumb_warm(path)
            return path
        except Exception as exc:  # noqa: BLE001 — 落盘失败同样只是“没本地化”
            logger.warning("图片本地化写盘失败 %s: %s", url, exc)
            _STATS["failed"] += 1
            return ""
        finally:
            with _LOCKS_LOCK:
                _LOCKS.pop(path, None)


def _prewarm_one(url: str) -> bool:
    """单张图预热（线程工作函数）：成功返回 True，任何异常返回 False。"""
    try:
        # 预热是刮削链上的图片下载主战场：带上重试与退避（问题一）。取图时的
        # 按需自愈（localize 默认 1 次）不加：那里要快，失败了反正还有远程地址
        return bool(localize(url, attempts=_download_attempts()))
    except Exception:  # noqa: BLE001 — 预热绝不该影响主流程
        return False


def prewarm(urls) -> int:
    """IO 阶段把一批图先落盘，返回成功缓存的张数（v2.42.9）

    与 ``localize()`` 是同一个函数、同一份内容寻址文件、同一把锁：预热过的图在写事务里
    调 ``localize(url, allow_download=False)`` 只是一次 ``isfile``，不发 HTTP、不攥写锁。

    预热本身就是一次真下载（TMDB 走 image.tmdb.org，不吃 api_key 配额）。预热失败、或
    被并发旋钮挡住（``_STATS["throttled"]``）都只是「这轮不本地化」，不影响刮削。

    v2.57.0 提速包 item4：单批内 4 线程并行下载（生产采样：串行 2.9-4.5s/条
    → 约 1s，图片占电影条目耗时 ~90%，2026-10-13）。全局 _DL_SEMAPHORE
    （8 并发、非阻塞）总量不变：抢不到名额的线程直接跳过、不攥着排队。
    localize 内部按 path 加锁 + 内容寻址去重，线程安全。
    """
    import concurrent.futures

    batch: list = []
    seen: set = set()
    for url in urls or ():
        # 同一张图在同一批里出现多次很正常（搜索命中与详情都给同一个 backdrop）：
        # 去重后计数才是「真的备好了几张图」
        if not url or url in seen:
            continue
        seen.add(url)
        batch.append(url)
    if not batch:
        return 0
    done = 0
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="img-prewarm") as ex:
        for fut in concurrent.futures.as_completed(
                [ex.submit(_prewarm_one, u) for u in batch]):
            try:
                if fut.result():
                    done += 1
            except Exception:  # noqa: BLE001 — 预热绝不该影响主流程
                continue
    return done


def referenced_basenames(db) -> set:
    """所有条目引用到的**本地图片文件名**（分批查，避免整库读进内存）；包含人物头像 URL 经 local_path 映射出的本地文件名"""
    from sqlalchemy import select

    from backend.emby_server import models as em

    found: set = set()
    last_id = 0
    while True:
        rows = db.execute(
            select(em.MediaItem.id, em.MediaItem.poster_path, em.MediaItem.backdrop_path)
            .where(em.MediaItem.id > last_id)
            .order_by(em.MediaItem.id)
            .limit(5000)
        ).all()
        if not rows:
            break
        last_id = rows[-1][0]
        for _id, poster, backdrop in rows:
            for path in (poster, backdrop):
                if is_cached_path(path):
                    found.add(os.path.basename(path))

    last_id = 0
    while True:
        rows = db.execute(
            select(em.EmbyPerson.id, em.EmbyPerson.image)
            .where(em.EmbyPerson.id > last_id)
            .order_by(em.EmbyPerson.id)
            .limit(5000)
        ).all()
        if not rows:
            break
        last_id = rows[-1][0]
        for _id, image_url in rows:
            if image_url and image_url.lower().startswith(("http://", "https://")):
                found.add(os.path.basename(local_path(image_url)))
    return found


_THUMB_NAME_RE = re.compile(r"^[0-9a-f]{8,}(_[wh]\d+)+$")


def _thumb_digest(name: str) -> Optional[str]:
    """缩略图文件名反查原图 digest；不是缩略图返回 None

    缩略图命名：<hex digest>_w320.jpg / _h640.jpg / _w320_h640.jpg
    （digest 是原图文件名的 sha1，40 位；tag 一定是 _w数字 / _h数字 组合，
    原图文件名里不会出现这种后缀，所以不会误判）
    """
    base, dot, _ext = name.partition(".")
    if not dot or _THUMB_NAME_RE.match(base) is None:
        return None
    return base.split("_", 1)[0]


def prune(db) -> dict:
    """维护周期的图片缓存清理：先删没被引用的，再按上限从旧到新淘汰

    - **有引用的文件一张都不删**：宁可超出上限（只记日志），也不能把正在用的封面删掉；
    - 刚落下还没落库的文件有保护期（``EMBY_IMAGE_GRACE_SECONDS``）。
    """
    result = {"removed": 0, "freed_bytes": 0, "files": 0, "bytes": 0, "over_limit": False}
    if not enabled():
        return result
    root = image_dir()
    if not os.path.isdir(root):
        return result
    referenced = referenced_basenames(db)
    # 下面两段是缩略图感知的清理：
    # - 被引用的原图的缩略图同样受保护（不删）；
    # - 删掉原图时，它的缩略图一起删（不留孤儿小图）。
    referenced_digests = {name.rsplit(".", 1)[0] for name in referenced}
    thumbs_of: dict[str, list[str]] = {}
    for entry in os.scandir(root):
        if not entry.is_file():
            continue
        digest = _thumb_digest(entry.name)
        if digest:
            thumbs_of.setdefault(digest, []).append(entry.name)
    # 读事务到此为止：下面要删的是磁盘文件（可能几万个 os.remove，走网络挂载更慢），
    # 不能让一次长事务陪着它挂着——SQLite 的 WAL 检查点会被挂着的事务挡住，
    # 表现就是 WAL 文件长期不收敛（见 docs/performance.md 的「事务范围」）。
    # 这里没有待提交的改动（只有上面那次查询），所以 rollback 与 commit 等价，语义更明确。
    db.rollback()
    grace = _env_int("EMBY_IMAGE_GRACE_SECONDS", 3600, 0)
    now = time.time()
    kept: list[tuple[str, int, float]] = []
    protected = set(referenced)  # 原图：清理与淘汰都不动
    # 被引用原图的**配置档位**缩略图同样要保护（正在下发的海报就是它）。
    # 早先这里把每一个 _wN.jpg 都塞进 protected，容量淘汰对 protected 一律跳过，
    # 于是任何按 maxWidth 现生成的变体都永久受保护、不受 EMBY_IMAGE_CACHE_MB 约束
    # 也永不清理 → 撑爆所在卷。现在宽度已吸附到配置档位
    # （_snap_to_configured_width），这里只白名单这有限的几个档位。
    _cfg_tags = {f"_w{w}" for w in thumb_widths()}

    def _remove_file(name: str) -> int:
        """删一个文件（含它的缩略图），返回释放的字节数"""
        freed = 0
        candidates = [name]
        digest = name.rsplit(".", 1)[0]
        candidates.extend(n for n in thumbs_of.get(digest, []) if n != name)
        for cname in candidates:
            try:
                st = os.stat(os.path.join(root, cname))
                os.remove(os.path.join(root, cname))
                thumb_mem_drop(cname)  # 内存里那份也清掉（key 是文件名，内容寻址）
                freed += st.st_size
                result["removed"] += 1
                result["freed_bytes"] += st.st_size
            except OSError as exc:
                logger.info("删除图片缓存失败 %s: %s", cname, exc)
        return freed

    for entry in os.scandir(root):
        if not entry.is_file():
            continue
        digest = _thumb_digest(entry.name)
        if entry.name in referenced or (digest and digest in referenced_digests):
            # 只保护「原图自身 + 配置档位的缩略图」，非档位的现生成变体照常淘汰
            if digest and digest in referenced_digests and entry.name not in referenced:
                base = entry.name.rsplit(".", 1)[0]
                if any(tag in base for tag in _cfg_tags):
                    protected.add(entry.name)
            try:
                stat = entry.stat()
            except OSError:
                continue
            kept.append((entry.name, stat.st_size, stat.st_mtime))
            continue
        if digest and digest not in referenced_digests:
            # 孤儿缩略图（原图早被删了）：直接清掉，不占保护期
            try:
                stat = entry.stat()
                os.remove(entry.path)
                thumb_mem_drop(entry.name)
                result["removed"] += 1
                result["freed_bytes"] += stat.st_size
            except OSError as exc:
                logger.info("删除图片缓存失败 %s: %s", entry.path, exc)
            continue
        try:
            stat = entry.stat()
        except OSError:
            continue
        if grace and now - stat.st_mtime < grace:
            kept.append((entry.name, stat.st_size, stat.st_mtime))
            continue
        freed_before = result["freed_bytes"]
        _remove_file(entry.name)
        if result["freed_bytes"] == freed_before:
            kept.append((entry.name, stat.st_size, stat.st_mtime))

    total = sum(size for _n, size, _m in kept)
    cap = _env_int("EMBY_IMAGE_CACHE_MB", 2048, 0) * 1024 * 1024
    attempted = set()
    if cap and total > cap:
        for name, size, _mtime in sorted(kept, key=lambda item: item[2]):
            if total <= cap:
                break
            if name in protected:
                continue            # 正在用的封面（含它的缩略图）不淘汰
            if name in attempted:
                continue
            attempted.add(name)
            try:
                os.remove(os.path.join(root, name))
                thumb_mem_drop(name)  # 内存里那份也得清，否则白占 LRU 配额再也发不出去
            except OSError:
                continue
            total -= size
            result["removed"] += 1
            result["freed_bytes"] += size
        if total > cap:
            # 第二遍：仍超限时淘汰可再生的缩略图变体（即使被引用保护）
            for name, size, _mtime in sorted(kept, key=lambda item: item[2]):
                if total <= cap:
                    break
                if name in attempted:
                    continue
                if _thumb_digest(name) is None:
                    continue        # 原图非缩略图，第二轮绝不淘汰
                attempted.add(name)
                try:
                    os.remove(os.path.join(root, name))
                    thumb_mem_drop(name)
                except OSError:
                    continue
                total -= size
                result["removed"] += 1
                result["freed_bytes"] += size
        result["over_limit"] = total > cap
        if result["over_limit"]:
            logger.info("图片缓存仍超出上限（被引用的图不淘汰）：%.0f MB",
                        total / 1024 / 1024)
    result["files"] = sum(1 for entry in os.scandir(root) if entry.is_file())
    result["bytes"] = sum(entry.stat().st_size for entry in os.scandir(root)
                          if entry.is_file())
    return result


def stats() -> dict:
    """图片缓存的当前状态（健康检查 / 测试用）"""
    root = image_dir()
    files = 0
    size = 0
    thumbs = 0
    if os.path.isdir(root):
        for entry in os.scandir(root):
            if not entry.is_file():
                continue
            files += 1
            if _thumb_digest(entry.name):
                thumbs += 1
            try:
                size += entry.stat().st_size
            except OSError:
                pass
    return {"enabled": enabled(), "dir": root, "files": files, "thumbs": thumbs,
            "bytes": size, "pil": _PIL_OK, "vips": vips_available(),
            "engine": thumb_engine(),
            "thumb_max_dim": _THUMB_MAX_DIM,
            "thumb_concurrency": _THUMB_CONCURRENCY,
            "thumb_mem": _THUMB_MEM.snapshot(), **_STATS}


# ---------- 历史补生成 ----------
# 3 万+条目已有海报但没缩略图：按 id 水位增量补，断点续跑。
# 限速：每批有条数上限 + 单批时间上限 + 条目间小睡，不跟扫描抢资源。
_BACKFILL_KEY = "thumb_backfill_last_id"
_BACKFILL_BATCH = 200
_BACKFILL_MAX_SECONDS = 240
_BACKFILL_SLEEP = 0.05


def backfill_status(db) -> dict:
    """补生成进度（管理后台展示用）"""
    from backend.integrations import store

    vals = store.read_values(db, [_BACKFILL_KEY], {_BACKFILL_KEY: "0"})
    try:
        last_id = int(vals[_BACKFILL_KEY] or 0)
    except ValueError:
        last_id = 0
    return {"last_id": last_id, "widths": thumb_widths(), "pil": _PIL_OK,
            "vips": vips_available(), "engine": thumb_engine(),
            "enabled": enabled()}


def backfill_thumbnails(db, batch_size: int = _BACKFILL_BATCH,
                        max_seconds: float = _BACKFILL_MAX_SECONDS) -> dict:
    """补生成一批缩略图（幂等、可重入）

    - 从 SystemConfig 的水位键继续，不重复做；
    - 每批最多 batch_size 条 / max_seconds 秒，超时即停，下次继续；
    - 条目间睡 _BACKFILL_SLEEP 秒，给扫描让路；
    - 每批结束提交水位，进程被杀也不丢进度。
    """
    from sqlalchemy import select

    from backend.emby_server import models as em
    from backend.integrations import store

    result = {"processed": 0, "generated": 0, "skipped": 0, "last_id": 0,
              "done": False}
    if not enabled() or not (_PIL_OK or vips_available()):
        return result
    vals = store.read_values(db, [_BACKFILL_KEY], {_BACKFILL_KEY: "0"})
    try:
        last_id = int(vals[_BACKFILL_KEY] or 0)
    except ValueError:
        last_id = 0
    widths = thumb_widths()
    deadline = time.time() + max(1.0, max_seconds)
    batch = max(1, min(batch_size, 2000))
    while result["processed"] < batch and time.time() < deadline:
        rows = db.execute(
            select(em.MediaItem.id, em.MediaItem.poster_path,
                   em.MediaItem.backdrop_path)
            .where(em.MediaItem.id > last_id)
            .order_by(em.MediaItem.id)
            .limit(500)
        ).all()
        if not rows:
            result["done"] = True
            break
        for _id, poster, backdrop in rows:
            last_id = _id
            result["processed"] += 1
            made = False
            for path in (poster, backdrop):
                if not path or not is_cached_path(path):
                    continue
                if not os.path.isfile(path):
                    continue
                for w in widths:
                    if resized_variant(path, max_width=w):
                        made = True
            if made:
                result["generated"] += 1
            else:
                result["skipped"] += 1
            time.sleep(_BACKFILL_SLEEP)
            if result["processed"] >= batch or time.time() >= deadline:
                break
        store.write_values(db, {_BACKFILL_KEY: str(last_id)},
                           {_BACKFILL_KEY: "缩略图补生成水位（条目 id）"})
        db.commit()
    result["last_id"] = last_id
    return result
