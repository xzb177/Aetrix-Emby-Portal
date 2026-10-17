"""libvips 缩略图引擎：vips 路径行为对齐 + Pillow 回退"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.emby_server import image_store

needs_vips = pytest.mark.skipif(
    not image_store.vips_available(), reason="libvips/pyvips 未安装"
)
needs_pil = pytest.mark.skipif(
    not image_store.pil_available(), reason="Pillow 未安装"
)


@pytest.fixture()
def imgdir(tmp_path, monkeypatch):
    d = tmp_path / "images"
    d.mkdir()
    monkeypatch.setattr(image_store, "image_dir", lambda: str(d))
    monkeypatch.setenv("EMBY_THUMB_WIDTHS", "320,640")
    return str(d)


def _make_jpeg(path, w=800, h=1200, color=(200, 30, 30), exif_orientation=None):
    from PIL import Image

    img = Image.new("RGB", (w, h), color)
    if exif_orientation is not None:
        exif = Image.Exif()
        exif[274] = exif_orientation  # 274 = Orientation
        img.save(path, "JPEG", quality=90, exif=exif)
    else:
        img.save(path, "JPEG", quality=90)
    return path


def _make_rgba_png(path, w=800, h=600):
    from PIL import Image

    img = Image.new("RGBA", (w, h), (10, 20, 30, 128))
    img.save(path, "PNG")
    return path


def _open_size(path):
    from PIL import Image

    with Image.open(path) as im:
        return im.size, im.mode


@needs_vips
def test_vips_engine_selected(imgdir):
    assert image_store.thumb_engine() == "vips"


@needs_vips
def test_vips_basic(imgdir):
    src = _make_jpeg(os.path.join(imgdir, "abc123.jpg"))
    thumb = image_store.resized_variant(src, max_width=320)
    assert thumb and os.path.isfile(thumb) and thumb != src
    (w, h), mode = _open_size(thumb)
    assert (w, h) == (320, 480)  # 等比，与 Pillow 路径一致
    assert mode == "RGB"
    assert os.path.getsize(thumb) < os.path.getsize(src)


@needs_vips
def test_vips_no_upscale(imgdir):
    src = _make_jpeg(os.path.join(imgdir, "small.jpg"), w=200, h=300)
    assert image_store.resized_variant(src, max_width=320) == ""


@needs_vips
def test_vips_height_only(imgdir):
    src = _make_jpeg(os.path.join(imgdir, "h.jpg"))
    thumb = image_store.resized_variant(src, max_height=600)
    assert thumb and os.path.isfile(thumb)
    (w, h), _ = _open_size(thumb)
    assert h == 600 and w == 400


@needs_vips
def test_vips_oversize_refused(imgdir, monkeypatch):
    monkeypatch.setattr(image_store, "_THUMB_MAX_PIXELS", 100)
    src = _make_jpeg(os.path.join(imgdir, "big.jpg"))
    assert image_store.resized_variant(src, max_width=320) == ""


@needs_vips
def test_vips_alpha_dropped_not_composited(imgdir):
    src = _make_rgba_png(os.path.join(imgdir, "a.png"))
    thumb = image_store.resized_variant(src, max_width=320)
    assert thumb and os.path.isfile(thumb)
    _, mode = _open_size(thumb)
    assert mode == "RGB"  # 对齐 Pillow convert("RGB") 的丢通道语义


@needs_vips
@needs_pil
def test_vips_exif_not_rotated_like_pillow(imgdir):
    # 现行 Pillow 路径不做 EXIF 旋转；vips 路径 no_rotate=True 与之对齐。
    # orientation=6 的 800x1200 图：旋转会变成 1200x800，不旋转保持 320x480。
    src = _make_jpeg(os.path.join(imgdir, "exif.jpg"), exif_orientation=6)
    thumb = image_store.resized_variant(src, max_width=320)
    (w, h), _ = _open_size(thumb)
    assert (w, h) == (320, 480), "vips 路径不应自动旋转（与 Pillow 路径对齐）"
    from PIL import Image

    with Image.open(thumb) as im:
        assert not im.getexif(), "输出不应带 EXIF（strip=True）"


@needs_vips
@needs_pil
def test_vips_and_pillow_same_dims(imgdir, monkeypatch):
    src = _make_jpeg(os.path.join(imgdir, "cmp.jpg"))
    thumb_vips = image_store.resized_variant(src, max_width=320)
    dims_vips, _ = _open_size(thumb_vips)
    # 强制走 Pillow 路径再做一张（删掉 vips 产物逼它重做）
    os.remove(thumb_vips)
    monkeypatch.setattr(image_store, "_vips_ok_cache", False)
    assert image_store.thumb_engine() == "pillow"
    thumb_pil = image_store.resized_variant(src, max_width=320)
    dims_pil, _ = _open_size(thumb_pil)
    assert dims_vips == dims_pil == (320, 480)


@needs_pil
def test_fallback_pillow_when_vips_absent(imgdir, monkeypatch):
    monkeypatch.setattr(image_store, "_vips_ok_cache", False)
    assert image_store.thumb_engine() == "pillow"
    src = _make_jpeg(os.path.join(imgdir, "fb.jpg"))
    thumb = image_store.resized_variant(src, max_width=320)
    assert thumb and os.path.isfile(thumb)
    (w, h), _ = _open_size(thumb)
    assert (w, h) == (320, 480)


@needs_vips
@needs_pil
def test_engine_failure_falls_back(imgdir, monkeypatch):
    # vips 单张失败 → 自动换 Pillow 重试一次
    def _boom(*a, **k):
        raise RuntimeError("vips boom")

    monkeypatch.setattr(image_store, "_thumb_vips", _boom)
    src = _make_jpeg(os.path.join(imgdir, "boom.jpg"))
    thumb = image_store.resized_variant(src, max_width=320)
    assert thumb and os.path.isfile(thumb)
    (w, h), _ = _open_size(thumb)
    assert (w, h) == (320, 480)


def test_no_engine_returns_empty(imgdir, monkeypatch):
    monkeypatch.setattr(image_store, "_vips_ok_cache", False)
    monkeypatch.setattr(image_store, "_PIL_OK", False)
    assert image_store.thumb_engine() == "none"
    assert image_store.resized_variant("/tmp/nope.jpg", max_width=320) == ""


@needs_vips
def test_worker_crash_recovers(imgdir):
    # worker 被杀后下一次调用自动重建，不影响业务
    src = _make_jpeg(os.path.join(imgdir, "crash.jpg"))
    t1 = os.path.join(imgdir, "c1.jpg")
    tmp = image_store._thumb_vips(src, t1, 320, 10**7)
    assert os.path.isfile(tmp)
    proc = image_store._vips_proc
    assert proc is not None and proc.poll() is None
    proc.kill()
    proc.wait()
    t2 = os.path.join(imgdir, "c2.jpg")
    tmp2 = image_store._thumb_vips(src, t2, 320, 10**7)
    assert os.path.isfile(tmp2)
    (w, h), _ = _open_size(tmp2)
    assert (w, h) == (320, 480)


@needs_vips
def test_worker_concurrent_calls(imgdir):
    # 多线程并发走同一 worker：行协议不串话
    import threading

    src = _make_jpeg(os.path.join(imgdir, "conc.jpg"))
    errors, outs = [], []

    def _one(i):
        try:
            target = os.path.join(imgdir, f"cc{i}.jpg")
            tmp = image_store._thumb_vips(src, target, 320, 10**7)
            outs.append(tmp)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_one, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(outs) == 6 and all(os.path.isfile(p) for p in outs)


@needs_vips
def test_parent_process_never_loads_libvips():
    # 红线：父进程永不 import pyvips（否则破坏 Pillow 封面画字）
    assert "pyvips" not in __import__("sys").modules
