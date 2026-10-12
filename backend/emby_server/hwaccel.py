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
    """用一帧真实编码测试后端是否可用

    命令组装与生产路径共用 get_hw_device_args / get_hw_filter / get_encoder_args，
    保证自检通过等价于生产可转码（修复 D-P1-3）。
    """
    encoder = _ENCODERS.get(backend)
    if not encoder:
        return False
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
            out_path = f.name
        # 与生产路径顺序一致：设备参数前置 → 输入 → 滤镜 → 编码器
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        cmd += get_hw_device_args(backend)
        cmd += ["-f", "lavfi", "-i", "testsrc=duration=0.04:size=128x128:rate=25"]
        hw_filter = get_hw_filter(backend)
        if hw_filter:
            cmd += ["-vf", hw_filter]
        cmd += get_encoder_args(backend) + ["-frames:v", "1", "-y", out_path]
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


def get_hw_device_args(backend: str = None) -> list:
    """返回后端所需的输入前置参数（如 vaapi 设备节点）

    这些选项必须放在 ffmpeg 的 -i 之前，非 vaapi 后端返回空列表。
    """
    b = backend or _current_backend
    if b == "vaapi":
        return ["-vaapi_device", "/dev/dri/renderD128"]
    return []


def get_hw_filter(backend: str = None) -> str:
    """返回后端所需的硬件上传滤镜片段，无需滤镜时返回空串

    调用方需与自身滤镜（如 scale）用逗号合并成一条 -vf。
    """
    b = backend or _current_backend
    if b == "vaapi":
        return "format=nv12,hwupload"
    if b == "qsv":
        return "format=nv12,hwupload=extra_hw_frames=64"
    return ""


def get_encoder_args(backend: str = None) -> list:
    """返回编码器参数（仅 -c:v 及其编码器私有参数）

    注意：调用方必须同时拼接 get_hw_device_args()（置于 -i 之前）
    和 get_hw_filter()（并入 -vf），否则硬件加速转码会失败。
    """
    b = backend or _current_backend
    if b == "nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4"]
    elif b == "qsv":
        return ["-c:v", "h264_qsv", "-preset", "veryfast"]
    elif b == "vaapi":
        return ["-c:v", "h264_vaapi"]
    else:
        return ["-c:v", "libx264", "-preset", "veryfast"]
