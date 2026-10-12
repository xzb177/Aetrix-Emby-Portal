"""媒体库封面的「自动生成」接口：预览 / 保存 / 重新生成

和直传封面（``portal.upload_library_cover``）并存，规则：

- ``cover_template`` 为空 → 沿用直传的 ``cover_path``，本模块不做任何事；
- 填了模板 → 从库里挑**最新入库**的海报自动拼一张 1920×1080 WebP，
  存回 ``cover_path``（同一张图，客户端侧 URL 不变，不用改任何读图逻辑）。

生成失败（库里没海报 / 字体缺失 / Pillow 没装）**一律不动已有封面**：
封面是锦上添花，不能因为生成不出来把管理员手动传的图也弄丢。
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session

from backend.database import get_db
from backend.emby_server import image_store, models as em
from backend.emby_server.portal import require_staff  # 与 admin_emby_router 同一个依赖
from backend.emby_server.library_cover_render import (
    DEFAULT_TEMPLATE,
    TEMPLATES,
    human_media_type,
    render_cover_bytes,
)

logger = logging.getLogger("aetrix.library_cover_api")

# 鉴权跟 portal.py 的 admin_emby_router 同一口径：路由级 require_staff，
# 不靠逐个 handler 挂依赖（漏一个就是越权）。
router = APIRouter(
    prefix="/api/admin/emby/libraries",
    tags=["管理端-媒体库封面"],
    dependencies=[Depends(require_staff)],
)

# 预览图也要有上限，否则管理员连点几下就能把内存打满
_PREVIEW_MAX_BYTES = 8 * 1024 * 1024


def _load_library(db: Session, lib_id: int) -> em.Library:
    lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
    if not lib:
        raise HTTPException(status_code=404, detail="媒体库不存在")
    return lib


def _validate_template(template: Optional[str]) -> str:
    value = (template or "").strip()
    if value not in TEMPLATES:
        raise HTTPException(
            status_code=400,
            detail=f"封面样式必须是 { '、'.join(TEMPLATES) } 之一",
        )
    return value


def _write_generated_cover(lib: em.Library, data: bytes) -> tuple[str, str | None]:
    """把生成的 WebP 落盘并更新 cover_path；返回 (新相对路径, 旧相对路径或None)

    与直传接口同一套路径规则（``library-covers/<guid>.webp``），所以生成的
    封面和上传的封面在客户端侧是同一个 URL，切换时不用清缓存逻辑。

    F-P1-19：先写版本化文件，DB 提交成功后再清理旧文件。
    原逻辑是 os.replace 直接覆盖旧图后才 db.commit()，提交失败时旧图已丢。
    F-P1-20：临时文件名加入 uuid，防同进程并发写同一库时交错。

    返回旧路径由调用方在 commit 成功后调用 _cleanup_old_cover_by_path() 删除。
    不直接删：提交失败时旧图必须还在。
    """
    # F-P1-19：版本化文件名，提交成功前旧文件原封不动
    version = uuid.uuid4().hex[:8]
    relative = os.path.join("library-covers", f"{lib.guid}.{version}.webp")
    # 注意：image_store.local_path() 是给远程 URL 做内容哈希的，不能解析本地相对路径
    target = os.path.abspath(os.path.join(image_store.image_dir(), relative))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    # F-P1-20：uuid 防同名竞争（原只用 pid）
    temporary = f"{target}.{os.getpid()}.{uuid.uuid4().hex[:8]}.rendering"
    try:
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except Exception:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    old = lib.cover_path
    lib.cover_path = relative
    # F-P1-19：返回旧路径，调用方在 commit 成功后删除
    old_to_cleanup = old if (old and old != relative) else None
    return relative, old_to_cleanup


def _cleanup_old_cover_by_path(old_relative: str | None) -> None:
    """DB 提交成功后调用：删除旧封面文件（F-P1-19）"""
    if old_relative:
        old_abs = image_store.local_path(old_relative)
        if old_abs and os.path.isfile(old_abs):
            try:
                os.remove(old_abs)
            except OSError:
                logger.debug("旧封面删除失败：%s", old_abs, exc_info=True)


@router.post("/{lib_id}/cover/preview")
def preview_library_cover(
    lib_id: int,
    payload: dict,
    db: Session = Depends(get_db),
):
    """按传入的模板/标题渲染一张预览图，**不落库**

    管理员每改一个字就调一次，所以不写盘、不动 cover_path，只回图片字节。
    """
    lib = _load_library(db, lib_id)
    template = _validate_template(payload.get("template"))
    data = render_cover_bytes(
        db, lib,
        template=template,
        title=(payload.get("title") or "")[:100],
        subtitle=(payload.get("subtitle") or "")[:100],
    )
    if not data:
        raise HTTPException(
            status_code=422,
            detail="生成失败：这个库还没有可用海报（等刮削补完再试）",
        )
    return Response(
        content=data,
        media_type="image/webp",
        headers={
            "Cache-Control": "no-store",
            "Content-Length": str(min(len(data), _PREVIEW_MAX_BYTES)),
        },
    )


@router.post("/{lib_id}/cover/render")
def render_library_cover(
    lib_id: int,
    payload: dict,
    db: Session = Depends(get_db),
):
    """按传入配置生成并保存封面（落库 + 落盘），返回新的封面 URL"""
    # F-P1-21：按 lib_id 加锁，防与后台 worker 交错覆盖。
    # 同一库正在渲染时，同步保存等待或直接拒绝，避免配置与文件互相覆盖。
    with _COVER_LOCK:
        if lib_id in _COVER_RUNNING:
            raise HTTPException(
                status_code=409,
                detail="该媒体库封面正在后台生成中，请稍后再试",
            )
        # 标记为运行中，防 worker 同时启动
        _COVER_RUNNING.add(lib_id)
    try:
        lib = _load_library(db, lib_id)
        template = _validate_template(payload.get("template"))
        data = render_cover_bytes(
            db, lib,
            template=template,
            title=(payload.get("title") or "")[:100],
            subtitle=(payload.get("subtitle") or "")[:100],
        )
        if not data:
            raise HTTPException(
                status_code=422,
                detail="生成失败：这个库还没有可用海报（等刮削补完再试）",
            )
        _, old_to_cleanup = _write_generated_cover(lib, data)
        # 配置落库：不然「按最新海报重新生成」读不到上次用的样式/标题
        lib.cover_template = template
        lib.cover_title = (payload.get("title") or "")[:100] or None
        lib.cover_subtitle = (payload.get("subtitle") or "")[:100] or None
        db.commit()
        # F-P1-19：提交成功后再清理旧文件
        _cleanup_old_cover_by_path(old_to_cleanup)
    finally:
        with _COVER_LOCK:
            _COVER_RUNNING.discard(lib_id)
    return {
        "success": True,
        "cover_url": f"/api/admin/emby/libraries/{lib.id}/cover",
        "content_type": "image/webp",
        "template": template,
        "title": payload.get("title") or "",
        "subtitle": payload.get("subtitle") or "",
        "library_name": lib.name,
        "media_type": human_media_type(lib.collection_type or ""),
    }


class CoverRegenError(RuntimeError):
    """封面重生成失败（带 HTTP 状态码，手动接口与自动更新共用同一套语义）

    单独一个异常类型而不是直接抛 HTTPException：自动更新的调用方在**扫描线程**里，
    不该（也无法）往 HTTP 响应里塞东西，它要的是“失败了就记日志，别影响扫描结果”。
    """

    def __init__(self, detail: str, status_code: int = 422):
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def regenerate_cover_for_library(db: Session, lib: em.Library) -> tuple[str, str | None]:
    """按库里已保存的模板/标题重新生成封面（手动按钮与“新片入库后自动更新”共用）

    返回 (新 cover_path, 旧 cover_path或None)。**不 commit**：手动接口与扫描钩子
    各自决定什么时候落库，commit 成功后调用 _cleanup_old_cover_by_path() 清理旧文件。
    失败抛 :class:`CoverRegenError`，两种调用方都不应该让“封面没画出来”变成别的后果。
    """
    if not lib.cover_template:
        raise CoverRegenError("该媒体库没有配置封面样式，先选一个样式并保存", 400)
    # 标题为空时默认用库名：重建库/未配置标题时封面也要有字，不能出无字封面
    data = render_cover_bytes(
        db, lib,
        template=lib.cover_template,
        title=lib.cover_title or lib.name or "",
        subtitle=lib.cover_subtitle or "",
    )
    if not data:
        raise CoverRegenError("生成失败：这个库还没有可用海报（等刮削补完再试）")
    return _write_generated_cover(lib, data)


# ==================== 异步重生成队列（v2.48.0）====================
# 以前扫描尾部**同步**渲染封面：渲染慢就把整轮扫描拖住，而且一批文件落库会在
# 同一次扫描里反复触发。现在改成后台线程 + 按库合并：
#   - 同一媒体库排队期间来多少请求都只算**一个**任务（转场 200 个文件 → 1 次渲染）
#   - 扫描线程只管入队，立刻返回；封面渲染与扫描完全解耦
#   - 失败保留旧封面（_write_generated_cover 只在拿到完整数据后才 os.replace）
_COVER_LOCK = threading.RLock()
_COVER_PENDING: set[int] = set()      # 排队中（尚未开始）
_COVER_RUNNING: set[int] = set()      # 正在渲染
_COVER_STATUS: dict[int, dict] = {}   # lib_id → {state, at, error}
_COVER_THREAD: Optional[threading.Thread] = None
_COVER_STOP: Optional[threading.Event] = None
#: 有新任务时叫醒 worker：空闲就真的阻塞着，不做「每秒醒一次看看有没有活」
_COVER_WAKE = threading.Event()
#: 两个任务之间的最小间隔（秒）：批量导入时不给渲染器喘息
COVER_MIN_INTERVAL_SEC = max(0, float(os.getenv("COVER_MIN_INTERVAL_SEC", "2") or 2))


def cover_status(lib_id: int) -> dict:
    """封面生成状态（前端展示用）：idle / pending / running / done / failed"""
    with _COVER_LOCK:
        if lib_id in _COVER_RUNNING:
            return {"state": "running"}
        if lib_id in _COVER_PENDING:
            return {"state": "pending"}
        return dict(_COVER_STATUS.get(lib_id) or {"state": "idle"})


def _set_cover_state(lib_id: int, state: str, error: str = "") -> None:
    with _COVER_LOCK:
        _COVER_STATUS[lib_id] = {"state": state, "at": time.time(), "error": error}
        if state in ("pending", "running"):
            _COVER_STATUS[lib_id].pop("error", None)


def _cover_worker(stop: threading.Event) -> None:
    from backend.database import SessionLocal

    last_done = 0.0
    while not stop.is_set():
        with _COVER_LOCK:
            lib_id = next(iter(_COVER_PENDING), None)
            if lib_id is not None:
                _COVER_PENDING.discard(lib_id)
                _COVER_RUNNING.add(lib_id)
        if lib_id is None:
            # 空闲就真阻塞着（等入队叫醒或停机），不做每秒一次的空转轮询
            _COVER_WAKE.wait()
            _COVER_WAKE.clear()
            continue
        # 限速：批量导入时不把 CPU 全占光
        gap = time.monotonic() - last_done
        if gap < COVER_MIN_INTERVAL_SEC:
            stop.wait(COVER_MIN_INTERVAL_SEC - gap)
        try:
            _set_cover_state(lib_id, "running")
            db = SessionLocal()
            try:
                lib = db.query(em.Library).filter(em.Library.id == lib_id).first()
                if lib is None or not lib.cover_template:
                    _set_cover_state(lib_id, "done")
                else:
                    try:
                        _regen_result = regenerate_cover_for_library(db, lib)
                        # F-P1-19：兼容旧 mock（返回 str）和新实现（返回 tuple）
                        if isinstance(_regen_result, tuple):
                            _, old_to_cleanup = _regen_result
                        else:
                            old_to_cleanup = None
                        db.commit()
                        # F-P1-19：提交成功后再清理旧文件
                        _cleanup_old_cover_by_path(old_to_cleanup)
                        _set_cover_state(lib_id, "done")
                    except CoverRegenError as exc:
                        db.rollback()
                        # 失败保旧：旧封面原封不动，只记状态
                        _set_cover_state(lib_id, "failed", str(exc.detail))
                    except Exception:  # noqa: BLE001 — 渲染崩了也不能拖垮 worker
                        db.rollback()
                        logger.warning("封面异步重生成失败 library_id=%s", lib_id,
                                       exc_info=True)
                        _set_cover_state(lib_id, "failed", "生成失败，已保留原封面")
            finally:
                db.close()
        finally:
            with _COVER_LOCK:
                _COVER_RUNNING.discard(lib_id)
            last_done = time.monotonic()


def enqueue_cover_regeneration(lib_id: int) -> bool:
    """排一个封面重生成任务 → ``是否新建了任务``

    **合并**：排队中重复调用直接返回 False，不会堆出 N 个任务。扫描侧只管调它，
    不等渲染结果——封面慢或失败都不影响本轮扫描结果。
    """
    global _COVER_THREAD, _COVER_STOP
    if lib_id is None:
        return False
    with _COVER_LOCK:
        if lib_id in _COVER_PENDING or lib_id in _COVER_RUNNING:
            return False
        _COVER_PENDING.add(lib_id)
        _set_cover_state(lib_id, "pending")
        _COVER_WAKE.set()          # 叫醒可能正阻塞着的 worker
        need_thread = _COVER_THREAD is None or not _COVER_THREAD.is_alive()
        if need_thread:
            _COVER_STOP = threading.Event()
            _COVER_THREAD = threading.Thread(target=_cover_worker, args=(_COVER_STOP,),
                                             name="cover-regen", daemon=True)
            _COVER_THREAD.start()
    return True


def stop_cover_worker(timeout: float = 5.0) -> None:
    """停掉后台线程（进程收尾时调用）：正在渲染的那一个最多等 timeout 秒"""
    global _COVER_THREAD, _COVER_STOP
    with _COVER_LOCK:
        thread, _COVER_THREAD = _COVER_THREAD, None
        stop_event, _COVER_STOP = _COVER_STOP, None
    if stop_event is not None:
        stop_event.set()
    _COVER_WAKE.set()          # 正在阻塞等任务的 worker 也要能立刻醒过来退出
    if thread is not None:
        thread.join(timeout=timeout)


@router.post("/{lib_id}/cover/regenerate")
def regenerate_library_cover(
    lib_id: int,
    db: Session = Depends(get_db),
):
    """按库里已保存的模板/标题重新生成（刮削补完新片后点一下就换新封面）"""
    lib = _load_library(db, lib_id)
    try:
        _regen_result = regenerate_cover_for_library(db, lib)
        # F-P1-19：兼容返回 tuple（新）或 str（旧 mock）
        old_to_cleanup = _regen_result[1] if isinstance(_regen_result, tuple) else None
    except CoverRegenError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    db.commit()
    # F-P1-19：提交成功后再清理旧文件
    _cleanup_old_cover_by_path(old_to_cleanup)
    return {
        "success": True,
        "cover_url": f"/api/admin/emby/libraries/{lib.id}/cover",
        "content_type": "image/webp",
    }