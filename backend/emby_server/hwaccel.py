# -*- coding: utf-8 -*-
"""硬件转码检测（Linger 借鉴）

启动时做一次真实的一帧编码自检，按 NVENC → QSV → VAAPI 顺序选可用后端。
设备/驱动/编码器不完整 → 静默保持软件转码，不影响播放。

环境变量 HARDWARE_ACCELERATION: none / auto / vaapi / qsv / nvenc（默认 none）
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)

_DETECT_ORDER = ("nvenc", "qsv", "vaapi")

_ENCODERS = {
    "nvenc": "h264_nvenc",
    "qsv": "h264_qsv", 
    "vaapi": "h264_vaapi",
    "software": "libx264",
}

_current_backend: str = "software"
_hw_enabled: bool = False


def _test_encoder(backend: str) -> bool:
    """用一帧真实编码测试后端是否可用（P1 修复 D-P1-3：与生产共用参数构造）"""
    encoder = _ENCODERS.get(backend)
    if not encoder:
        return False
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            out_path = f.name
        # 使用与生产一致的参数构造（get_hw_device_args/get_hw_filter/get_encoder_args）
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=0.04:size=128x128:rate=25"]
        cmd.extend(get_hw_device_args(backend))
        hw_filter = get_hw_filter(backend)
        if hw_filter:
            cmd.extend(["-vf", hw_filter])
        # 编码器名（去掉 -c:v 前缀，直接用 encoder）
        cmd.extend(["-c:v", encoder, "-frames:v", "1", "-y", out_path])
        result = subprocess.run(cmd, timeout=10, capture_output=True)
        try:
            os.unlink(out_path)
        except:
            pass
        return result.returncode == 0
    except Exception as e:
        logger.debug(f"硬件编码器 {backend} 自检失败: {e}")
        return False


def detect_hardware() -> str:
    """启动时检测硬件转码后端"""
    global _current_backend, _hw_enabled
    setting = os.getenv("HARDWARE_ACCELERATION", "none").lower().strip()
    if setting == "none":
        _current_backend = "software"
        _hw_enabled = False
        logger.info("硬件转码已关闭（HARDWARE_ACCELERATION=none），使用软件转码")
        return _current_backend
    if setting == "auto":
        to_test = _DETECT_ORDER
    elif setting in _DETECT_ORDER:
        to_test = (setting,)
    else:
        logger.warning(f"未知的 HARDWARE_ACCELERATION 值: {setting}，使用软件转码")
        _current_backend = "software"
        _hw_enabled = False
        return _current_backend
    for backend in to_test:
        logger.info(f"正在自检硬件编码器: {backend}...")
        if _test_encoder(backend):
            _current_backend = backend
            _hw_enabled = True
            logger.info(f"Hardware transcoding enabled: {backend} ({_ENCODERS[backend]})")
            return _current_backend
        else:
            logger.info(f"硬件编码器 {backend} 不可用，尝试下一个")
    _current_backend = "software"
    _hw_enabled = False
    logger.info("所有硬件编码器都不可用，静默回退到软件转码（libx264），不影响播放")
    return _current_backend


def get_video_encoder() -> str:
    return _ENCODERS.get(_current_backend, "libx264")


def get_backend() -> str:
    return _current_backend


def is_hardware_enabled() -> bool:
    return _hw_enabled


def get_encoder_args(backend: str = None) -> list:
    """返回指定后端的完整编码器参数（P1 修复 D-P1-3）。

    此前自检命令带 -vaapi_device/hwupload 但生产参数缺这些，
    导致自检通过但实际转码必失败。现在两处共用同一套参数构造。
    """
    b = backend or _current_backend
    if b == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4"]
    elif b == "qsv":
        # qsv 需要 hwupload 滤镜链（与自检命令一致）
        return ["-c:v", "h264_qsv", "-preset", "veryfast"]
    elif b == "vaapi":
        # vaapi 需要设备和上传滤镜（与自检命令一致）
        # 注意：-vaapi_device 和 -vf 由调用方（build_hls_command）负责拼接，
        # 这里返回编码器部分；调用方必须同时处理设备初始化。
        return ["-c:v", "h264_vaapi"]
    else:
        return ["-c:v", "libx264", "-preset", "veryfast"]


def get_hw_device_args(backend: str = None) -> list:
    """返回硬件加速的设备初始化参数（P1 修复 D-P1-3）。

    与 _test_encoder 自检命令保持一致，调用方（build_hls_command）应将
    这些参数拼接到 ffmpeg 命令行中。
    """
    b = backend or _current_backend
    if b == "vaapi":
        return ["-vaapi_device", "/dev/dri/renderD128"]
    return []


def get_hw_filter(backend: str = None) -> str:
    """返回硬件加速的滤镜链（P1 修复 D-P1-3）。

    与 _test_encoder 自检命令保持一致。
    """
    b = backend or _current_backend
    if b == "vaapi":
        return "format=nv12,hwupload"
    elif b == "qsv":
        return "format=nv12,hwupload=extra_hw_frames=64"
    return ""
