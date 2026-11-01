"""播放变体枚举 + 变体清单下发（FMBY 学习批二 #11，PR-1）

学的是 FMBY 的 PlaybackInfo 变体清单：一个条目有几种播法，在清单阶段
就一次列全（每条带来源/协议/能力申报），客户端自己选；配套的是它
**诚实申报**的纪律——不会的就写不会（它的 SupportsTranscoding 恒
false，因为它服务端不转码）。

对应魂 3（调度与数据面分离）：服务端负责算清「这个条目真实能怎么播」
并下发清单，播放决策权还给客户端；取流执行仍在 video_stream 的既有
分支里（按变体直达是 PR-2 的事），本模块不搬一个字节。

我方主线只有四种播放目标（定稿 §三逐项对过 video_stream 的分支）：
``drive_relay``（Drive SA 中继，字节过服务器）/ ``drive_direct``
（Drive 302 直链，客户端自拉）/ ``mount_relay``（挂载中继，115/
WebDAV/AList/STRM 走 provider 凭据代理）/ ``local_file``（本机文件
直出）。服务端转码已随转码删除整套移除（2026-10-24），
``SupportsTranscoding`` 全面恒 false。
FMBY 有的「先转存」「云端转码流」我方**没有**，枚举里不许
虚列——列了就是虚报。

横切只许一套（本模块存在的理由）：
- 变体可用性只许经 :func:`list_variants` 判定，playback_info 与
  后续 video_stream 都只许问它，不许各自再拼一遍开关组合；
- 转码能力不再申报：历史上 ``_media_source`` 里
  ``SupportsTranscoding: True`` 是硬编码，PlaybackInfo 面会被策略
  覆写、详情 DTO 面不会，两面申报不一致；转码删除后两面恒 false，
  不再有任何转码申报单口。

可用性判定只许用便宜信号（策略开关 / 条目静态属性 / 挂载可定位的
一次主键查询），**不许在清单阶段真解析直链**——解析成败仍由取流时
回答（与 #1205 解析缓存正交，缓存键与失效矩阵不归本模块管）。

总开关：env ``PLAY_VARIANTS_ENABLED``（默认关），import 时读一次
（与 play_events 同口径）。关 = PlaybackInfo 下发单源、与改造前逐字
一致，是本功能的唯一回滚手段。变体可用性不进 PlaybackInfo 缓存：
每次现算（只读 store 热缓存里的开关），策略开关变更的生效延迟
≤ store 的 60 秒 TTL；PlaybackInfo 基础体缓存（300 秒）只存
``_media_source`` 基础体；转码删除后其 SupportsTranscoding 恒
False，不再有现算覆写。
"""
from __future__ import annotations

import copy
import logging
import os
from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.emby_server import mounts as mount_lib
from backend.emby_server import playback_strategy

logger = logging.getLogger(__name__)

# import 时读一次（与 play_events 同口径）：热路径不查 env
_ENABLED = os.getenv("PLAY_VARIANTS_ENABLED", "0") not in (
    "0", "false", "False", "")

#: 变体枚举值（与 video_stream 四条互斥分支一一对应，定稿 §三）
DRIVE_RELAY = "drive_relay"
DRIVE_DIRECT = "drive_direct"
MOUNT_RELAY = "mount_relay"
LOCAL_FILE = "local_file"
#: BYOS P1：绑定用户自己的 115 直链（服务端按绑定凭据现签现发
#: 302，播放器自拉，字节不过服务器）。只在该用户有 active 的
#: pan115 绑定且条目挂载为 115 时才进清单；第一条仍恒为现行
#: 主线变体（mount_relay），老客户端行为不变。
BYOS115_DIRECT = "byos115_direct"
#: BYOS Drive 中继：绑定用户自己的 Drive 令牌中继（服务端凭该用户
#: 令牌取流、字节过服务器，走该用户自己的额度，绝不走共享 SA 池）。
#: 注意不是“直链”：Drive alt=media 取流须带 Authorization 头，302
#: 给播放器会丢令牌，无法直链；故本变体诚实申报为中继
#: （is_remote=False / needs_relay=True），与 byos115_direct 的
#: 302 直链形态不同。只在条目是 byos:// 路径、路径绑定是该用户
#: active 的 Drive 绑定且条目有 drive_file_id 时才进清单。
BYOS_DRIVE_RELAY = "byos_drive_relay"
#: 夸克二期：绑定用户自己的夸克副本中继（服务端凭该用户 Cookie
#: 现取直链后中继出流——夸克 CDN 校验 Cookie/Referer/UA 三件套，
#: 不能像 115 那样 302 让播放器自拉，故诚实申报为中继
#: is_remote=False / needs_relay=True）。本变体只作 play_start
#: 打点口径：转存副本走主线源出流（与 115 副本同款，不进
#: list_variants 显式清单），不许拿它当可选变体下发。
BYOS_QUARK_RELAY = "byos_quark_relay"


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
    # is_remote=True（客户端从 115 自拉）/ needs_relay=False
    # （字节不过服务器，零服务器带宽）——与 drive_direct 同形。
    BYOS115_DIRECT: Variant(BYOS115_DIRECT, "我的 115 直链", "Http", True, False),
    # 中继形态：字节过服务器（凭用户令牌取流），故 is_remote=False /
    # needs_relay=True——与 drive_relay 同形，只是凭据换成用户自己的。
    BYOS_DRIVE_RELAY: Variant(BYOS_DRIVE_RELAY, "我的 Drive 中继", "Http", False, True),
    # 中继形态：字节过服务器（凭用户 Cookie 现取链取流），故
    # is_remote=False / needs_relay=True——与 drive 中继同形。
    BYOS_QUARK_RELAY: Variant(BYOS_QUARK_RELAY, "我的夸克中继", "Http", False, True),
}


def variants_enabled() -> bool:
    """变体清单下发总开关（env PLAY_VARIANTS_ENABLED，默认关）。"""
    return _ENABLED


def list_variants(db: Session, item, *,
                  drive_mount_locatable: bool = False,
                  mount_can_generate_play_target: bool | None = None,
                  user115_direct_available: bool = False,
                  user_drive_byos_available: bool = False) -> list[Variant]:
    """按条目算可用变体清单（唯一判定口），第一条恒为现行主线变体。

    排序即兼容：第一条就是今天 video_stream 自动判定实际会走的那条
    分支，只取第一条源的老客户端行为与今天一致。判定顺序与
    video_stream 的分支顺序对齐（Drive 快路径在前、回落解析在后）：

    - 有 drive_file_id 且直链开关开：主线是直链（2026-10-27 用户改令：
      直链为主线）；中继开关开且挂载可定位时，中继作为可切换兜底
      列在直链之后；两条都可用时都列（直链第一）；中继关时只列直链，
      单条不是清单失效——准备中态标注仍对单条生效（见
      apply_transfer_status，PlaybackInfo 对单条同样覆盖）。
    - mount:// 条目：挂载中继；
    - 其余（本机路径、直链开关关时的 Drive 条目回落）：本机文件或
      按 file_path 形态落挂载中继。

    服务端转码已删除，不再有转码变体；``SupportsTranscoding`` 全面
    恒 false（见 :func:`build_media_sources`）。

    ``drive_mount_locatable`` 由调用方用 ``_drive_mount_id_for_item``
    （一次主键查询）算好传进来——本模块不反向依赖 api.py。

    ``mount_can_generate_play_target`` 由调用方按条目挂载行的类型派生
    能力算好传进来（一次主键查询，见 api._mount_can_generate_play_target）：
    False 时挂载中继不进清单（清单第 2 项能力门控）；None 时保持旧行为
    （调用方未提供能力信号时，不因查不到挂载就把变体扣掉）。

    ``user_drive_byos_available`` 由调用方用 api._user_drive_byos_available
    算好传进来：条目是 byos:// 路径、路径绑定是该用户 active 的 Drive
    绑定且条目有 drive_file_id 时才 True；True 时在主线之后追加
    byos_drive_relay（第一条仍是现行主线，老客户端无感）。清单阶段
    不真取令牌——令牌只在取流时现取现用，失效时取流报 409/503，
    不静默回落共享 SA 池。
    """
    fid = (getattr(item, "drive_file_id", "") or "").strip()
    file_path = getattr(item, "file_path", "") or ""
    names: list[str] = []
    if fid and playback_strategy.drive_direct_play_enabled(db):
        if drive_mount_locatable and playback_strategy.drive_relay_enabled(db):
            names += [DRIVE_DIRECT, DRIVE_RELAY]
        else:
            names += [DRIVE_DIRECT]
    elif mount_lib.is_mount_path(file_path):
        # 清单第 2 项能力门控：声明 can_generate_play_target=False 的源
        # 不进变体清单；None = 未提供信号，保持旧行为。
        if mount_can_generate_play_target is not False:
            names.append(MOUNT_RELAY)
        # BYOS P1：调用方已算好“该用户有 active 的 pan115 绑定且
        # 条目挂载为 115”时，追加绑定直链（排在主线之后，第一条
        # 仍是现行主线，老客户端无感）。清单阶段不真解析直链。
        if user115_direct_available:
            names.append(BYOS115_DIRECT)
    else:
        names.append(LOCAL_FILE)
    # BYOS Drive：排在主线之后（第一条仍是现行主线），无绑定或
    # 非 byos 条目时恒不追加——不虚列、不伪装可播。本函数内再以
    # drive_file_id 兜底：无 fid 的条目（本机文件等）即使调用方误传
    # 信号也不追加，信号不得外溢（与 byos115 只在挂载分支追加同款
    # 纪律；api 侧的 _user_drive_byos_available 本就要求 byos 路径
    # + fid，这里是第二道门）。
    if user_drive_byos_available and fid:
        names.append(BYOS_DRIVE_RELAY)
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
    - ``(None, 404)``：名字不在变体枚举里，或 variant 参数与
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


def apply_transfer_status(sources: list[dict], transfer_status: str) -> None:
    """转存准备中态覆盖（2026-10-26 用户亲定“先转存再播”）。

    只对 preparing/failed 生效，其它值（含 None/ready）逐字不动：
    各源加 ``TransferStatus`` 字段、SupportsDirectPlay 与
    SupportsDirectStream 置 False、删 DirectPlayUrl/DirectStreamUrl
    ——清单里保留该条、但点不了（FMBY“先转存再播”口径，不要做成
    清单里根本不列）。原地修改，调用方在缓存取/存之后再算状态
    并覆盖（状态每请求新鲜算，不污染 5 分钟基础体缓存）。
    同时写就绪态分级字段 ``Readiness``（preparing/failed），与
    :func:`compute_readiness` 同口径；ready 态由
    :func:`build_media_sources` 预置，不由本函数补。
    """
    if transfer_status not in ("preparing", "failed"):
        return
    for src in sources:
        src["TransferStatus"] = transfer_status
        src["Readiness"] = transfer_status
        src["SupportsDirectPlay"] = False
        src["SupportsDirectStream"] = False
        src.pop("DirectPlayUrl", None)
        src.pop("DirectStreamUrl", None)


def stable_variant_id(kind: str, height=None, label: str = "") -> str:
    """FMBY 式稳定变体 id（不含条目 guid，同一变体跨请求恒定）。

    有高度时用 ``{kind}-h{高度}``（如 ``drive_direct-h1080``）；
    无高度时用标签归一化后的 slug（小写、非字母数字折成 ``-``、
    首尾去 ``-``，空则回落 kind）；冲突后缀由
    :func:`assign_stable_variant_ids` 统一加 ``-2``/``-3``，本函数
    本身不做冲突处理。注意：Emby 的 MediaSourceId 仍用
    ``{guid}:{variant_name}``（兼容老客户端与既有签名口径），本 id
    只用于清单描述子（PlaybackVariants），两者不要混用。
    """
    kind = (kind or "").strip() or "variant"
    if height:
        try:
            h = int(height)
        except (TypeError, ValueError):
            h = 0
        if h > 0:
            return f"{kind}-h{h}"
    slug = "".join(
        ch.lower() if ch.isalnum() else "-" for ch in (label or ""))
    slug = "-".join(part for part in slug.split("-") if part)
    return slug or kind


def assign_stable_variant_ids(kinds_labels) -> list[str]:
    """给一批 (kind, height, label) 分配互不冲突的稳定 id。

    同 id 第二次出现加 ``-2``，第三次 ``-3``，依此类推（FMBY
    侧车 id 生成规则：高度优先、标签 slug 兜底、冲突加后缀）。
    """
    seen: dict[str, int] = {}
    out: list[str] = []
    for kind, height, label in kinds_labels:
        base = stable_variant_id(kind, height, label)
        n = seen.get(base, 0) + 1
        seen[base] = n
        out.append(base if n == 1 else f"{base}-{n}")
    return out


def compute_readiness(has_stream_url: bool, transfer_verdict=None) -> str:
    """就绪态分级（FMBY readiness 的我方口径，纯函数）。

    由“有没有流地址＋转存准备状态”共同算出（requires_transfer
    已由门禁 verdict 编码：preparing/failed 就是“需要转存且未就绪”，
    proceed/serve_115_copy/serve_quark_copy 就是“转存要求已满足”，不再另设第四输入）：
    - verdict failed → ``failed``（取流报 409/503，明文，不静默改走）；
    - verdict preparing → ``preparing``（清单保留、点不了）；
    - verdict error → ``unavailable``（门禁本身不可用，取流 503）；
    - 其余：有流地址 → ``ready``，无流地址 → ``unavailable``。
    """
    if transfer_verdict == "failed":
        return "failed"
    if transfer_verdict == "preparing":
        return "preparing"
    if transfer_verdict == "error":
        return "unavailable"
    return "ready" if has_stream_url else "unavailable"


def build_variant_descriptors(variants: list[Variant], *,
                              transfer_verdict=None,
                              requires_transfer: bool = False) -> list[dict]:
    """FMBY 形清单描述子：便宜、可缓存、不带任何真实流地址。

    这是清单与取流分离的第一步：描述子只含稳定 id、种类、可读名、
    是否默认、是否需要转存、就绪态——**不含任何 URL 键**（调用方
    不许事后往里塞地址；地址只在取流时由 video_stream 按变体
    第二步解析）。Emby 协议要求 MediaSources 带 DirectPlayUrl，
    故 PlaybackInfo 的 MediaSources 仍按原口径下发本站 /stream
    内链（那不是上游解析出的真实地址，上游解析只在 video_stream
    时发生）；描述子是给“清单便宜、可缓存、不泄露上游地址”用的
    另一面，两面不要混成一面。
    ``has_stream_url`` 对描述子恒按“在可用清单里”算：变体在清单
    里即视为有取流入口（取流成败仍由取流时回答），不在清单里的
    变体根本不进描述子。``requires_transfer`` 由调用方按门禁
     verdict 传入（转存未完成或已有用户副本时为 True）。
    """
    heights_labels = [(v.name, None, v.label) for v in variants]
    stable_ids = assign_stable_variant_ids(heights_labels)
    out: list[dict] = []
    for i, (v, sid) in enumerate(zip(variants, stable_ids)):
        out.append({
            "stable_id": sid,
            "kind": v.name,
            "label": v.label,
            "is_default": i == 0,
            "requires_transfer": bool(requires_transfer),
            "readiness": compute_readiness(True, transfer_verdict),
        })
    return out


def build_media_sources(base_source: dict, variants: list[Variant], *,
                        guid: str, stream_url: str,
                        direct_stream_ok: bool,
                        transfer_status: str = None) -> list[dict]:
    """把 ``_media_source`` 基础体按变体清单复制成多条 MediaSource。

    基础体（含真实探测轨道/章节）每变体深拷贝一份，只改身份与申报
    字段：``Id`` = ``{guid}:{variant}`` 互不相同、``Name`` 带变体
    可读名、Protocol/IsRemote 按变体元数据、URL 按变体打标。
    服务端转码已删除：``SupportsTranscoding`` 每条恒 False，
    不再下发 ``TranscodingUrl``。
    ``transfer_status`` 为 preparing/failed 时建完即覆盖准备中态
    （见 apply_transfer_status；默认 None = 逐字不变）。
    """
    base_name = base_source.get("Name") or ""
    out: list[dict] = []
    for v in variants:
        src = copy.deepcopy(base_source)
        src["Id"] = f"{guid}:{v.name}"
        src["Name"] = f"{base_name} · {v.label}" if base_name else v.label
        src["Protocol"] = v.protocol
        src["IsRemote"] = v.is_remote
        surl = variantize_url(stream_url, guid, v.name)
        src["DirectPlayUrl"] = surl
        src["DirectStreamUrl"] = surl
        src["SupportsDirectPlay"] = True
        src["SupportsDirectStream"] = bool(direct_stream_ok)
        src["SupportsTranscoding"] = False
        src["Readiness"] = "ready"
        src.pop("TranscodingUrl", None)
        out.append(src)
    apply_transfer_status(out, transfer_status)
    return out
