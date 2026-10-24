"""播放变体枚举 + 变体清单下发（FMBY 学习批二 #11，PR-1）

学的是 FMBY 的 PlaybackInfo 变体清单：一个条目有几种播法，在清单阶段
就一次列全（每条带来源/协议/能力申报），客户端自己选；配套的是它
**诚实申报**的纪律——不会的就写不会（它的 SupportsTranscoding 恒
false，因为它服务端不转码）。

对应魂 3（调度与数据面分离）：服务端负责算清「这个条目真实能怎么播」
并下发清单，播放决策权还给客户端；取流执行仍在 video_stream 的既有
分支里（按变体直达是 PR-2 的事），本模块不搬一个字节。

我方实际只有五种播放目标（定稿 §三逐项对过 video_stream 的分支）：
``drive_relay``（Drive SA 中继，字节过服务器）/ ``drive_direct``
（Drive 302 直链，客户端自拉）/ ``mount_relay``（挂载中继，115/
WebDAV/AList/STRM 走 provider 凭据代理）/ ``local_file``（本机文件
直出）/ ``transcode_hls``（服务端 ffmpeg 转码，策略开关管辖的真实
路径）。FMBY 有的「先转存」「云端转码流」我方**没有**，枚举里不许
虚列——列了就是虚报。

横切只许一套（本模块存在的理由）：
- 变体可用性只许经 :func:`list_variants` 判定，playback_info 与
  后续 video_stream 都只许问它，不许各自再拼一遍开关组合；
- 转码能力申报只许经 :func:`transcode_allowed` 生成——历史上
  ``_media_source`` 里 ``SupportsTranscoding: True`` 是硬编码，
  PlaybackInfo 面会被策略覆写、详情 DTO 面不会，两面申报不一致，
  本模块就是来消灭这件事的。

可用性判定只许用便宜信号（策略开关 / 条目静态属性 / 挂载可定位的
一次主键查询），**不许在清单阶段真解析直链**——解析成败仍由取流时
回答（与 #1205 解析缓存正交，缓存键与失效矩阵不归本模块管）。

总开关：env ``PLAY_VARIANTS_ENABLED``（默认关），import 时读一次
（与 play_events 同口径）。关 = PlaybackInfo 下发单源、与改造前逐字
一致，是本功能的唯一回滚手段。变体可用性不进 PlaybackInfo 缓存：
每次现算（只读 store 热缓存里的开关），策略开关变更的生效延迟
≤ store 的 60 秒 TTL；PlaybackInfo 基础体缓存（300 秒）只存
``_media_source`` 基础体，其 SupportsTranscoding 由 handler 每次
用 :func:`transcode_allowed` 现算覆写，不吃缓存里的旧值。
"""
from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend import playback_policy
from backend.emby_server import mounts as mount_lib
from backend.emby_server import playback_strategy

logger = logging.getLogger(__name__)

# import 时读一次（与 play_events 同口径）：热路径不查 env
_ENABLED = os.getenv("PLAY_VARIANTS_ENABLED", "0") not in (
    "0", "false", "False", "")

#: 变体枚举值（与 video_stream 五条互斥分支一一对应，定稿 §三）
DRIVE_RELAY = "drive_relay"
DRIVE_DIRECT = "drive_direct"
MOUNT_RELAY = "mount_relay"
LOCAL_FILE = "local_file"
TRANSCODE_HLS = "transcode_hls"


@dataclass(frozen=True)
class Variant:
    """一个播放变体的静态元数据（展示与申报用，不含任何条目状态）。"""

    #: 枚举值（下发时进 MediaSourceId 后缀与 stream URL 的 variant 参数）
    name: str
    #: 可读名（拼进 MediaSource 的 Name，如「片名 · Drive 中继」）
    label: str
    #: 协议申报：经本机 HTTP 端点取流的写 Http，本机文件写 File
    protocol: str
    #: 客户端最终从远端（网盘）自拉字节的写 True；经服务器端点的一律 False
    is_remote: bool
    #: 字节要过服务器（中继/转码）的写 True——成本与带宽口径靠它区分
    needs_relay: bool


_VARIANTS: dict[str, Variant] = {
    DRIVE_RELAY: Variant(DRIVE_RELAY, "Drive 中继", "Http", False, True),
    DRIVE_DIRECT: Variant(DRIVE_DIRECT, "Drive 直链", "Http", True, False),
    MOUNT_RELAY: Variant(MOUNT_RELAY, "挂载中继", "Http", False, True),
    LOCAL_FILE: Variant(LOCAL_FILE, "本地文件", "File", False, False),
    TRANSCODE_HLS: Variant(TRANSCODE_HLS, "转码", "Http", False, True),
}


def variants_enabled() -> bool:
    """变体清单下发总开关（env PLAY_VARIANTS_ENABLED，默认关）。"""
    return _ENABLED


def transcode_allowed(db: Session, user) -> bool:
    """转码能力申报的唯一口径：(策略开关开 or 管理员) 且 用户个人开关开。

    与 PlaybackInfo 历史口径逐项一致，此前散在 handler 里现算、
    ``_media_source`` 里却硬编码 True——现在两面都只许调这里。
    ``user=None`` 时只能核策略（详情等拿不到用户本人的场景），
    按策略值申报；不许在任何调用点自己再拼一遍这组条件。
    """
    policy_on = bool(playback_policy.transcode_enabled(db))
    if user is None:
        return policy_on
    return (policy_on or bool(getattr(user, "is_staff", False))) and bool(
        getattr(user, "enable_video_transcoding", True))


def list_variants(db: Session, item, *, allow_transcode: bool,
                  drive_mount_locatable: bool = False) -> list[Variant]:
    """按条目算可用变体清单（唯一判定口），第一条恒为现行主线变体。

    排序即兼容：第一条就是今天 video_stream 自动判定实际会走的那条
    分支，只取第一条源的老客户端行为与今天一致。判定顺序与
    video_stream 的分支顺序对齐（Drive 快路径在前、回落解析在后）：

    - 有 drive_file_id 且直链开关开：主线是中继（中继开关开且挂载
      可定位时）或直链；两条都可用时都列（中继第一）；
    - mount:// 条目：挂载中继；
    - 其余（本机路径、直链开关关时的 Drive 条目回落）：本机文件或
      按 file_path 形态落挂载中继；
    - ``allow_transcode`` 为真时转码恒列最后（它是客户端主动选的
      兜底路径，永远不是自动主线）。

    ``drive_mount_locatable`` 由调用方用 ``_drive_mount_id_for_item``
    （一次主键查询）算好传进来——本模块不反向依赖 api.py。
    """
    fid = (getattr(item, "drive_file_id", "") or "").strip()
    file_path = getattr(item, "file_path", "") or ""
    names: list[str] = []
    if fid and playback_strategy.drive_direct_play_enabled(db):
        if drive_mount_locatable and playback_strategy.drive_relay_enabled(db):
            names += [DRIVE_RELAY, DRIVE_DIRECT]
        else:
            names += [DRIVE_DIRECT]
    elif mount_lib.is_mount_path(file_path):
        names.append(MOUNT_RELAY)
    else:
        names.append(LOCAL_FILE)
    if allow_transcode:
        names.append(TRANSCODE_HLS)
    return [_VARIANTS[n] for n in names]


def parse_requested_variant(query_params) -> tuple:
    """从 video_stream 请求里读客户端选的变体（PR-2，纯函数、不碰 DB）。

    读两个双保险位（PR-1 下发时同时打上）：``variant`` 参数优先，
    没有参数时读 ``MediaSourceId`` 的 ``{guid}:{variant}`` 后缀。
    返回 ``(name_or_None, mismatch_bool)``：两者都出现且不一致时
    mismatch=True（身份对不上，调用方按 404 处理），只出现一个时
    以那个为准；两个都没有时 name=None（老客户端，走原判定）。
    """
    raw_variant = query_params.get("variant")
    variant = (raw_variant or "").strip() or None
    msid = query_params.get("MediaSourceId") or ""
    suffix = None
    if ":" in msid:
        suffix = msid.rsplit(":", 1)[1].strip() or None
    if variant and suffix and variant != suffix:
        return None, True
    return variant or suffix, False


def resolve_requested_variant(query_params, available_names) -> tuple:
    """校验客户端选的变体（PR-2，纯函数、不碰 DB）。

    返回 ``(variant_or_None, error_status_or_None)``：
    - ``(None, None)``：没带变体，走原判定；
    - ``(name, None)``：带了且在可用清单里；
    - ``(None, 404)``：名字不在五变体枚举里，或 variant 参数与
      MediaSourceId 后缀不一致（身份对不上）；
    - ``(None, 409)``：名字是已知变体，但该条目当下不可用——调用方
      必须明确报错，**不许静默改走默认条**（静默改走会让客户端的
      选择权变成假的，也会让打点口径说谎）。
    待复验标注（迁移方案 §七）：变体回落语义等 FMBY 数值未实测到，
    本函数不编任何 FMBY 阈值，只做「在清单里 / 不在清单里」判定。
    """
    name, mismatch = parse_requested_variant(query_params)
    if mismatch:
        return None, 404
    if name is None:
        return None, None
    if name not in _VARIANTS:
        return None, 404
    if name not in list(available_names):
        return None, 409
    return name, None


def variantize_url(url: str, guid: str, variant_name: str) -> str:
    """给播放 URL 打上变体标记：MediaSourceId 带后缀 + variant 参数双保险。

    签名串（uid/exp/sign、api_key）原样保留——变体参数只是附加的
    查询参数，验签不看它；PR-2 的 video_stream 按变体取流时优先
    读这个参数。URL 里没有 MediaSourceId 时只加参数，不硬拼。
    """
    if not url:
        return url
    out = url.replace(
        f"MediaSourceId={guid}", f"MediaSourceId={guid}:{variant_name}", 1)
    sep = "&" if "?" in out else "?"
    return f"{out}{sep}variant={variant_name}"


def build_media_sources(base_source: dict, variants: list[Variant], *,
                        guid: str, stream_url: str, transcoding_url: str,
                        direct_stream_ok: bool,
                        allow_transcode: bool) -> list[dict]:
    """把 ``_media_source`` 基础体按变体清单复制成多条 MediaSource。

    基础体（含真实探测轨道/章节）每变体深拷贝一份，只改身份与申报
    字段：``Id`` = ``{guid}:{variant}`` 互不相同、``Name`` 带变体
    可读名、Protocol/IsRemote 按变体元数据、URL 按变体打标。
    ``SupportsTranscoding`` 每条都由 :func:`transcode_allowed` 的
    结果（调用方传入的 ``allow_transcode``）生成，不许逐条另判；
    转码变体本体是例外：它就是转码路径，DirectPlay/DirectStream
    置假、只带 TranscodingUrl。
    """
    base_name = base_source.get("Name") or ""
    out: list[dict] = []
    for v in variants:
        src = copy.deepcopy(base_source)
        src["Id"] = f"{guid}:{v.name}"
        src["Name"] = f"{base_name} · {v.label}" if base_name else v.label
        src["Protocol"] = v.protocol
        src["IsRemote"] = v.is_remote
        if v.name == TRANSCODE_HLS:
            src["SupportsDirectPlay"] = False
            src["SupportsDirectStream"] = False
            src["SupportsTranscoding"] = True
            src.pop("DirectPlayUrl", None)
            src.pop("DirectStreamUrl", None)
            src["TranscodingUrl"] = variantize_url(
                transcoding_url, guid, v.name)
        else:
            surl = variantize_url(stream_url, guid, v.name)
            src["DirectPlayUrl"] = surl
            src["DirectStreamUrl"] = surl
            src["SupportsDirectPlay"] = True
            src["SupportsDirectStream"] = bool(direct_stream_ok)
            src["SupportsTranscoding"] = bool(allow_transcode)
            if allow_transcode:
                src["TranscodingUrl"] = variantize_url(
                    transcoding_url, guid, v.name)
            else:
                src.pop("TranscodingUrl", None)
        out.append(src)
    return out
