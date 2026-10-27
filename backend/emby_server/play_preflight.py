"""播放前体检（第二批第 2 单，#1357 格式一致化的延伸）

点播前对候选变体做预检：容器、编码、HDR、音轨等申报信息与
取流口径先验一遍，选最匹配的直链。绝不转码（转码已删除）、
绝不静默回落共享 SA 中继。

只读三不：
- 不下发播放链接：不算 ``_play_target``、不签名、不拼流地址；
- 不改动变体清单：输入的可用变体名单只读，输出是新结果对象；
- 不写库、不碰他人盘：调用方只许传本用户可见条目的申报信息
  （API 层走 ``_require_visible_item`` + 本用户绑定信号）。

开关：``PLAY_PREFLIGHT_ENABLED``（默认关）。关时调用方接口返回
404，全站逐字不变；本模块的 ``preflight_enabled`` 每次调用现读
env，供测试用 ``monkeypatch.setenv`` 覆盖。

判定口径（写死在本模块，调用方不另定）：
- 变体身份沿用 ``play_variants`` 的既有语义：名字不在变体枚举里
  → 404（与 ``resolve_requested_variant`` 同款）；名字已知但不在
  本条目可用清单里 → 409（同款，绝不静默改走默认条）。
- 每个检查只有三种结论：``pass``（申报齐全且合规）、``unknown``
  （申报缺失）、``fail``（申报明确违规）。总体结论：
  任一 ``fail`` → ``blocked``；无 ``fail`` 但有 ``unknown`` →
  ``degraded``（仍可选，见降级策略）；全 ``pass`` → ``ready``。
- 降级策略（明确写出，缺失绝不等于失败）：
  - 容器未申报 → ``unknown``：不挡。PlaybackInfo 层仍可用扩展名
    回退（``_container_of``）；预检只报缺失，不替它猜。
  - 编码未申报 → ``unknown``：不挡。本模块不编造客户端编码白名单，
    没有“支持的编码表”可供 fail——缺编码是探测未完成，不是违规。
  - HDR 未申报（无视频轨、或视频轨 ``video_range`` 全空）→
    ``unknown``：不挡、不假设 SDR、也不因此切中继；客户端按未知
    动态范围处理，HDR/SDR 结论只报已申报的值。
  - 音轨未申报（无音频轨、且无条目级 ``audio_codec``）→
    ``unknown``：不挡。缺音轨是“可能无声/探测未完成”，不是取流
    失败；挡音轨会连带剥夺画面播放，本模块不这么做。
  - 音轨有但无 ``IsDefault`` → 仍 ``pass``：默认轨回落首条音频轨
    是 ``_default_audio_stream_index`` 的既有口径，不算缺失。
- 容器可流式检查只适用于 Http 取流变体（直链与中继），不适用于
  ``local_file``（播放器直接开本机文件，不走 /stream 后缀规则）：
  Http 变体 + 已申报容器不在可流式白名单（如 iso）→ ``fail`` →
  ``blocked``（409），因为转码已删、无合法兜底；绝不因此静默切
  另一条变体。
- 选直链：``select_best_direct`` 只在可用清单顺序里挑第一个结论
  为 ``ready`` 或 ``degraded`` 的直链变体（``drive_direct`` /
  ``byos115_direct``，即 ``play_variants`` 里 ``is_remote=True``
  的两条）。它永远不返回中继变体；没有直链通过时返回 None，
  调用方必须把 blocked 原因明文报给用户，**绝不静默回落共享
  SA 中继**（``drive_relay``），也不静默回落用户自己的中继。
- 可流式白名单与 ``api._DIRECT_STREAM_EXTS`` 同值；API 层调用时
  显式传入 ``api`` 的集合，纯函数默认值与之一致——两处若漂移，
  以 API 层传入的为准（见调用方注释）。

预检结论不污染播放态：本模块不写任何播放会话、进度、变体选择
记录；同一次点播重复预检结果恒定（输入不变则输出不变）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple

#: 播放前体检总开关 env 键（默认关）。
PLAY_PREFLIGHT_ENV = "PLAY_PREFLIGHT_ENABLED"

#: 变体枚举（与 play_variants._VARIANTS 的键同值；本模块不 import
#: play_variants，避免把 mounts/playback_strategy 拖进纯函数层）。
KNOWN_VARIANTS = frozenset({
    "drive_relay", "drive_direct", "mount_relay", "local_file",
    "byos115_direct", "byos_drive_relay",
})

#: 直链变体（play_variants 里 is_remote=True 的两条）。
DIRECT_VARIANTS = frozenset({"drive_direct", "byos115_direct"})

#: 可流式容器白名单（与 api._DIRECT_STREAM_EXTS 同值，见模块头）。
STREAMABLE_CONTAINERS = frozenset({
    "mkv", "mp4", "avi", "mov", "ts", "m2ts", "webm", "flv", "wmv", "mpeg",
})


def preflight_enabled() -> bool:
    """总开关是否开启（每次调用现读 env，默认关）。"""
    return (str(os.getenv(PLAY_PREFLIGHT_ENV, "0") or "0")
            .strip().lower() not in {"0", "false", "no", "off", ""})


def _field(entry, name: str, default=None):
    """读流条目字段：支持 dict 与对象两种形态（测试与 ORM 通吃）。"""
    if isinstance(entry, dict):
        return entry.get(name, default)
    return getattr(entry, name, default)


@dataclass(frozen=True)
class PreflightCheck:
    """单项检查结论：pass / unknown / fail 三态之一。"""

    name: str
    verdict: str  # "pass" | "unknown" | "fail"
    detail: str = ""


@dataclass(frozen=True)
class PreflightResult:
    """单个变体的预检结论（新对象，绝不改输入的变体清单）。"""

    variant: str
    verdict: str  # "ready" | "degraded" | "blocked"
    error_status: Optional[int] = None  # 404（未知变体）/ 409（不可用）/ None
    checks: Tuple[PreflightCheck, ...] = ()
    fallback_variant: Optional[str] = None  # 恒为 None：预检绝不提兜底变体

    @property
    def failures(self) -> Tuple[str, ...]:
        return tuple(c.detail or c.name for c in self.checks
                     if c.verdict == "fail")

    @property
    def warnings(self) -> Tuple[str, ...]:
        return tuple(c.detail or c.name for c in self.checks
                     if c.verdict == "unknown")


def _stream_entries(streams) -> List[dict]:
    """把流条目归一为 dict（type/codec/video_range/is_default）。"""
    out: List[dict] = []
    for entry in streams or []:
        stream_type = str(_field(entry, "stream_type",
                                 _field(entry, "Type", "")) or "").lower()
        out.append({
            "type": stream_type,
            "codec": _field(entry, "codec", _field(entry, "Codec", "")) or "",
            "video_range": (_field(entry, "video_range",
                                   _field(entry, "VideoRange", ""))
                            or ""),
            "is_default": bool(_field(entry, "is_default",
                                      _field(entry, "IsDefault", False))),
        })
    return out


def preflight_variant(variant: str, available_names: Iterable[str], *,
                      container: Optional[str] = None,
                      video_codec: Optional[str] = None,
                      audio_codec: Optional[str] = None,
                      streams=None,
                      streamable_containers=None) -> PreflightResult:
    """对单个候选变体做预检（纯函数，不碰 DB、不发 HTTP、不写库）。

    ``available_names`` 是调用方用 ``play_variants.list_variants``
    算好的本条目可用变体名（唯一判定口，本模块不重算可用性）。
    ``container`` 应传调用方算好的有效容器（PlaybackInfo 口径的
    ``_container_of``：条目 container 优先、缺失时扩展名回退），
    传 None 时只看“未申报”分支，不在本模块内猜扩展名。
    """
    name = (variant or "").strip()
    available = list(available_names or [])
    streamable = (STREAMABLE_CONTAINERS if streamable_containers is None
                  else frozenset(streamable_containers))
    checks: List[PreflightCheck] = []

    if name not in KNOWN_VARIANTS:
        checks.append(PreflightCheck(
            "variant_known", "fail",
            f"unknown_variant: 变体 {name or '(空)'} 不在变体枚举里"))
        return PreflightResult(variant=name, verdict="blocked",
                               error_status=404, checks=tuple(checks))
    checks.append(PreflightCheck("variant_known", "pass",
                                 f"变体 {name} 在枚举里"))
    if name not in available:
        checks.append(PreflightCheck(
            "variant_available", "fail",
            f"variant_unavailable: 变体 {name} 不在本条目可用清单里"))
        return PreflightResult(variant=name, verdict="blocked",
                               error_status=409, checks=tuple(checks))
    checks.append(PreflightCheck("variant_available", "pass",
                                 f"变体 {name} 在本条目可用清单里"))

    normalized = (container or "").strip().lower()
    if name != "local_file":
        if not normalized:
            checks.append(PreflightCheck(
                "container_declared", "unknown",
                "container_undeclared: 容器未申报（PlaybackInfo 仍可扩展名回退）"))
        elif normalized in streamable:
            checks.append(PreflightCheck(
                "container_streamable", "pass", f"容器 {normalized} 可流式播放"))
        else:
            checks.append(PreflightCheck(
                "container_streamable", "fail",
                f"container_not_streamable: 容器 {normalized} 不可流式播放，"
                "且服务端已无转码兜底"))
    else:
        # 本机文件变体不走 /stream 后缀规则，容器可流式检查不适用。
        checks.append(PreflightCheck(
            "container_streamable", "pass",
            "local_file 不走流式后缀规则，容器检查不适用"))

    if (video_codec or "").strip():
        checks.append(PreflightCheck(
            "video_codec_declared", "pass",
            f"视频编码已申报：{(video_codec or '').strip()}"))
    else:
        checks.append(PreflightCheck(
            "video_codec_declared", "unknown",
            "video_codec_undeclared: 视频编码未申报（探测未完成，不挡播放）"))

    entries = _stream_entries(streams)
    video_entries = [e for e in entries if e["type"] == "video"]
    audio_entries = [e for e in entries if e["type"] == "audio"]
    ranges = sorted({e["video_range"] for e in video_entries
                     if e["video_range"]})
    if ranges:
        checks.append(PreflightCheck(
            "hdr_declared", "pass",
            "动态范围已申报：" + "/".join(ranges)))
    elif video_entries:
        checks.append(PreflightCheck(
            "hdr_declared", "unknown",
            "hdr_undeclared: 视频轨存在但动态范围未申报（不假设 SDR，不切中继）"))
    else:
        checks.append(PreflightCheck(
            "hdr_declared", "unknown",
            "video_stream_undeclared: 无视频轨申报，动态范围未知（不挡播放）"))

    if audio_entries:
        checks.append(PreflightCheck(
            "audio_declared", "pass",
            f"音频轨已申报：{len(audio_entries)} 条"
            + ("（有默认轨）" if any(e["is_default"] for e in audio_entries)
               else "（无默认轨，回落首条音频轨）")))
    elif (audio_codec or "").strip():
        checks.append(PreflightCheck(
            "audio_declared", "unknown",
            "audio_tracks_undeclared: 仅有条目级音频编码，音轨清单未申报"))
    else:
        checks.append(PreflightCheck(
            "audio_declared", "unknown",
            "audio_undeclared: 音轨与音频编码均未申报（可能无声，不挡画面播放）"))

    if any(c.verdict == "fail" for c in checks):
        verdict, error_status = "blocked", 409
    elif any(c.verdict == "unknown" for c in checks):
        verdict, error_status = "degraded", None
    else:
        verdict, error_status = "ready", None
    return PreflightResult(variant=name, verdict=verdict,
                           error_status=error_status,
                           checks=tuple(checks))


def preflight_all(available_names: Iterable[str], **declared) -> List[PreflightResult]:
    """对可用清单里的每个变体各做一次预检（保持清单顺序）。"""
    return [preflight_variant(name, available_names, **declared)
            for name in (available_names or [])]


def select_best_direct(results: Iterable[PreflightResult]) -> Optional[PreflightResult]:
    """在预检结果（保持可用清单顺序）里选最匹配的直链。

    只返回结论为 ready/degraded 的直链变体；中继变体永远不返回；
    没有直链通过时返回 None（调用方须明文报 blocked 原因，绝不
    静默回落共享 SA 中继）。
    """
    for result in results or []:
        if result.variant in DIRECT_VARIANTS and result.verdict in ("ready", "degraded"):
            return result
    return None


def result_to_dict(result: PreflightResult) -> dict:
    """预检结论序列化（不含任何播放链接与凭据）。"""
    return {
        "variant": result.variant,
        "verdict": result.verdict,
        "error_status": result.error_status,
        "fallback_variant": result.fallback_variant,
        "failures": list(result.failures),
        "warnings": list(result.warnings),
        "checks": [
            {"name": c.name, "verdict": c.verdict, "detail": c.detail}
            for c in result.checks
        ],
    }
