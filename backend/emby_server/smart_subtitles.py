"""智能字幕（第三批第一单，默认关）

只针对用户自己的条目：条目可见性由调用方（路由层）按
``backend.library_scope.item_allowed`` 判定，本模块不做鉴权、不查库。

复用既有字幕基础设施，不另起一套：
- 匹配与语言识别：``subtitle_match.match_subtitle_names`` /
  ``subtitle_match.subtitle_language``（同名判定、语言标签 → 三字码）。
- 投递与缓存：既有 ``subtitles.SUB_CACHE_DIR``（服务端字幕缓存区）；
  本模块生成的双语轨只写进该缓存区下的 ``smart/`` 子目录，
  **绝不写盘内媒体文件、绝不写用户盘非转存目录**。

能力边界（产品红线）：
- 不转码、不做自研播放器：双语轨是 VTT 文本轨，供第三方播放器
  经既有字幕投递链路消费，本模块不改播放链路。
- 不爬取、不保存他人盘内容：输入只来自调用方给出的字幕文本与
  同目录候选文件名，不主动读任何挂载/远端文件。
- AI 翻译走配置化提供方接口，默认不启用任何外部调用：
  提供方、端点、凭据三者缺一即明确报错（``TranslateNotConfigured``），
  不静默回落、不编造字幕、不拿原文冒充译文。
- 总开关 ``SMART_SUBTITLES_ENABLED``（默认关），每次调用现读 env，
  关 = 全部入口拒绝，唯一回滚手段。

桥接口径（管理后台字幕能力，system_configs 为准、env 做默认回退）：

- ``resolve_enabled(db)`` / ``resolve_provider_config(db)``：有 db 时走
  ``integrations.store.get_value``（面板行优先、缺行回退同名 env，写时失效、
  60 秒热缓存）；``db=None`` 时与原来一样只读 env（本模块默认仍是纯函数）。
- 管理端永远不写生产 ``.env``：面板保存只写 ``system_configs`` 行。
- 用户端路由调用时传 db，面板改完即生效（同进程写时失效）；直调核心函数
  不传 db 时行为与原来逐字一致。

本模块是纯函数 + 受控写盘（仅缓存区），不依赖 DB、不依赖网络
（除非调用方显式传入 ``http_post`` 或配好提供方后调翻译）。
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from pathlib import Path
from typing import Callable, Optional, Sequence

from backend.emby_server import subtitle_match
from backend.emby_server import subtitles as subtitle_delivery

ENABLED_ENV = "SMART_SUBTITLES_ENABLED"
PROVIDER_ENV = "SMART_SUBTITLE_TRANSLATE_PROVIDER"
ENDPOINT_ENV = "SMART_SUBTITLE_TRANSLATE_ENDPOINT"
API_KEY_ENV = "SMART_SUBTITLE_TRANSLATE_API_KEY"

_TRUTHY = {"1", "true", "yes", "on"}


def enabled() -> bool:
    """总开关：现读 env，默认关。"""
    return (os.getenv(ENABLED_ENV, "0") or "0").strip().lower() in _TRUTHY


def _db_value(db, env_key: str, default: str = "") -> str:
    """DB 行优先、env 回退的单键读取（走 store 热缓存；面板保存时写时失效）。

    store 导入放函数内：本模块默认不依赖 DB，调用方不传 db 时不引入
    integrations 的导入链（避免循环导入与纯函数测试的额外依赖）。

    注意 store 的行语义：行存在即用行值、哪怕是空串（面板清空会写 "" 行，
    不是删行）。但本模块的面板口径是“面板没填 = 回退同名 env”（与表单
    回显的 _effective 一致），所以空串行必须同样回退 env，否则面板上
    显示“已配置（回退 env）”、生成侧却报“未配置”，两边打架。
    """
    from backend.integrations import store as _store
    env_fallback = os.getenv(env_key, default)
    value = _store.get_value(db, env_key, env_fallback)
    if not (value or "").strip():
        return env_fallback
    return value


def resolve_enabled(db=None) -> bool:
    """生效开关：有 db 时面板行优先、缺行回退 env；无 db 时与 enabled() 一致。"""
    if db is None:
        return enabled()
    return _db_value(db, ENABLED_ENV, "0").strip().lower() in _TRUTHY


def resolve_provider_config(db=None) -> tuple[str, str, str]:
    """生效三件套：有 db 时面板行优先、缺行回退 env；无 db 时与 _provider_config() 一致。

    缺任一即 ``TranslateNotConfigured``，错误信息只写缺了哪一项的
    环境变量名，绝不回显凭据值（与 _provider_config() 逐字一致）。
    """
    if db is None:
        return _provider_config()
    provider = _db_value(db, PROVIDER_ENV, "").strip()
    endpoint = _db_value(db, ENDPOINT_ENV, "").strip()
    api_key = _db_value(db, API_KEY_ENV, "")
    if not provider:
        raise TranslateNotConfigured(
            "翻译提供方未配置：请设置 SMART_SUBTITLE_TRANSLATE_PROVIDER"
        )
    if not endpoint:
        raise TranslateNotConfigured(
            "翻译端点未配置：请设置 SMART_SUBTITLE_TRANSLATE_ENDPOINT"
        )
    if not api_key:
        raise TranslateNotConfigured(
            "翻译凭据未配置：请设置 SMART_SUBTITLE_TRANSLATE_API_KEY"
        )
    return provider, endpoint, api_key


class SmartSubtitleDisabled(Exception):
    """开关未开启。"""


class TranslateNotConfigured(Exception):
    """翻译提供方未配置（缺提供方/端点/凭据任一），明确失败、不回落。"""


class TranslateFailed(Exception):
    """翻译提供方调用失败（非 200 / 响应结构不对 / 条数不对），不回落。"""


class CueMismatch(Exception):
    """原文与译文条数不一致，拒绝生成错位双语轨。"""


class UnsafeCachePath(ValueError):
    """生成的缓存路径不在服务端字幕缓存区内，拒绝写盘。"""


def require_enabled(db=None) -> None:
    if not resolve_enabled(db):
        raise SmartSubtitleDisabled(
            "智能字幕未开启：SMART_SUBTITLES_ENABLED=1 后重试（默认关）"
        )


# ---------------- 季集对齐 ----------------

_SEASON_EP_RE = re.compile(r"[sS](\d{1,2})[eE](\d{1,3})")
_X_EP_RE = re.compile(r"(?<![0-9])(\d{1,2})[xX](\d{1,3})(?![0-9])")
_CN_EP_RE = re.compile(r"第\s*(\d{1,3})\s*[集话話]")


def episode_of(name: str) -> Optional[tuple[Optional[int], int]]:
    """从文件名提取（季，集）；无季信息时季为 None；无集信息返回 None。"""
    text = name or ""
    m = _SEASON_EP_RE.search(text)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = _X_EP_RE.search(text)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = _CN_EP_RE.search(text)
    if m:
        return (None, int(m.group(1)))
    return None


def match_for_item(item_name: str, season: Optional[int], episode: Optional[int],
                   candidates: Sequence[str]) -> list[dict]:
    """为条目匹配字幕候选。

    先走 ``subtitle_match.match_subtitle_names``（同名/分辨率/集头判定），
    再做季集对齐：
    - 条目有集号且候选带集号：集号不等 → 剔除；季号都有且不等 → 剔除。
    - 条目是电影（无集号）且候选带集号 → 剔除（不把剧集字幕认给电影）。
    - 候选无集号时保留（同名电影字幕、整季包等不靠集号区分的情形）。

    返回 ``[{name, language}]``，language 为 Emby 三字码。
    """
    matched = subtitle_match.match_subtitle_names(item_name, list(candidates))
    out: list[dict] = []
    for name in matched:
        cand_ep = episode_of(name)
        if cand_ep is not None:
            cand_season, cand_episode = cand_ep
            if episode is not None and cand_episode != episode:
                continue
            if (season is not None and cand_season is not None
                    and cand_season != season):
                continue
            if episode is None:
                continue
        out.append({"name": name, "language": subtitle_match.subtitle_language(name)})
    return out


# ---------------- 双语轨结构 ----------------

_TS_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})")


def parse_cues(text: str) -> list[dict]:
    """解析 SRT/VTT 文本为 cue 列表 [{start, end, text}]。

    容忍：WEBVTT 头、序号行、时间戳逗号/点号、cue 内多行。
    空 cue、缺时间戳的块直接丢弃（不猜时间轴）。
    """
    body = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    if body.lstrip().startswith("WEBVTT"):
        body = body.lstrip()[len("WEBVTT"):].lstrip("\n")
    cues: list[dict] = []
    for block in re.split(r"\n\s*\n", body.strip()):
        lines = [ln.strip() for ln in block.strip().split("\n") if ln.strip()]
        if not lines:
            continue
        ts_idx = None
        for i, ln in enumerate(lines):
            if _TS_RE.search(ln):
                ts_idx = i
                break
        if ts_idx is None:
            continue
        m = _TS_RE.search(lines[ts_idx])
        assert m is not None
        start = m.group(1).replace(",", ".")
        end = m.group(2).replace(",", ".")
        cue_text = "\n".join(lines[ts_idx + 1:]).strip()
        if not cue_text:
            continue
        cues.append({"start": start, "end": end, "text": cue_text})
    return cues


def build_bilingual_vtt(source_text: str, translations: Sequence[str],
                        source_lang: str, target_lang: str) -> str:
    """原文 cue + 逐条译文合成双语 VTT。

    每条 cue 内原文在上、译文在下；条数不一致即 ``CueMismatch``，
    绝不截断/补空对齐（错位比缺失更坏）。
    """
    cues = parse_cues(source_text)
    translations = list(translations)
    if len(cues) != len(translations):
        raise CueMismatch(
            f"原文 {len(cues)} 条、译文 {len(translations)} 条，条数不一致，"
            "拒绝生成双语轨"
        )
    out = ["WEBVTT", f"NOTE smart-subtitle {source_lang}->{target_lang}", ""]
    for cue, trans in zip(cues, translations):
        out.append(f"{cue['start']} --> {cue['end']}")
        out.append(cue["text"])
        out.append(trans.strip())
        out.append("")
    return "\n".join(out)


# ---------------- 配置化翻译提供方 ----------------

def _provider_config() -> tuple[str, str, str]:
    """返回（提供方，端点，凭据键名对应的值）。

    缺任一即 ``TranslateNotConfigured``；错误信息只写缺了哪一项的
    环境变量名，绝不回显凭据值。
    """
    provider = (os.getenv(PROVIDER_ENV, "") or "").strip()
    endpoint = (os.getenv(ENDPOINT_ENV, "") or "").strip()
    api_key = os.getenv(API_KEY_ENV, "") or ""
    if not provider:
        raise TranslateNotConfigured(
            "翻译提供方未配置：请设置 SMART_SUBTITLE_TRANSLATE_PROVIDER"
        )
    if not endpoint:
        raise TranslateNotConfigured(
            "翻译端点未配置：请设置 SMART_SUBTITLE_TRANSLATE_ENDPOINT"
        )
    if not api_key:
        raise TranslateNotConfigured(
            "翻译凭据未配置：请设置 SMART_SUBTITLE_TRANSLATE_API_KEY"
        )
    return provider, endpoint, api_key


def translate_texts(texts: Sequence[str], source_lang: str, target_lang: str,
                    http_post: Optional[Callable] = None, db=None) -> list[str]:
    """经配置化提供方翻译一批文本。

    提供方协议（调用方按此实现服务端点）：POST JSON
    ``{provider, source_lang, target_lang, texts}``，成功响应
    ``{translations: [...]}`` 且条数与入参一致；否则 ``TranslateFailed``。
    ``http_post`` 仅供测试注入（签名 ``(url, headers, payload) -> dict``）；
    未注入时走 urllib，超时 30 秒。失败一律抛错，不回落原文。
    ``db`` 不为 None 时三件套走桥接（面板行优先、缺行回退 env）；为 None 时只读 env。
    """
    provider, endpoint, api_key = resolve_provider_config(db)
    payload = {
        "provider": provider,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "texts": list(texts),
    }
    if http_post is not None:
        data = http_post(endpoint, {"Authorization": "Bearer ***"}, payload)
    else:
        req = urllib.request.Request(
            endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 — 翻译失败统一口径，不回落
            raise TranslateFailed(f"翻译提供方调用失败：{exc}") from exc
    translations = data.get("translations") if isinstance(data, dict) else None
    if not isinstance(translations, list) or len(translations) != len(payload["texts"]):
        raise TranslateFailed("翻译提供方响应结构异常：translations 条数与入参不一致")
    return [str(t) for t in translations]


# ---------------- 缓存区写盘（只写服务端字幕缓存区） ----------------

_LANG_RE = re.compile(r"^[a-z]{2,3}$")


def bilingual_cache_path(item_id: int, source_lang: str, target_lang: str,
                         cache_dir: Optional[str] = None) -> Path:
    """双语轨落盘路径：<字幕缓存区>/smart/item_<id>_<源>-<目标>.vtt。"""
    if not _LANG_RE.match(source_lang or "") or not _LANG_RE.match(target_lang or ""):
        raise ValueError("语言码须为 2~3 位小写字母（如 chi/eng）")
    base = Path(cache_dir) if cache_dir else Path(subtitle_delivery.SUB_CACHE_DIR)
    return base / "smart" / f"item_{int(item_id)}_{source_lang}-{target_lang}.vtt"


def write_bilingual_track(item_id: int, vtt_text: str, source_lang: str,
                          target_lang: str,
                          cache_dir: Optional[str] = None) -> str:
    """把双语 VTT 写进服务端字幕缓存区，返回文件名（不回显绝对路径）。

    落盘前校验解析后路径仍在缓存区内；缓存区外的任何路径一律拒绝。
    """
    base = Path(cache_dir) if cache_dir else Path(subtitle_delivery.SUB_CACHE_DIR)
    path = bilingual_cache_path(item_id, source_lang, target_lang,
                                cache_dir=str(base))
    resolved_base = base.resolve()
    resolved = path.resolve()
    if resolved_base not in resolved.parents:
        raise UnsafeCachePath("双语轨落盘路径不在服务端字幕缓存区内，拒绝写盘")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(vtt_text, encoding="utf-8")
    return resolved.name


def generate_bilingual_track(item_id: int, source_text: str,
                             source_lang: str, target_lang: str,
                             translations: Optional[Sequence[str]] = None,
                             http_post: Optional[Callable] = None,
                             cache_dir: Optional[str] = None,
                             db=None) -> dict:
    """一站式：开关 → 翻译（未给译文时调提供方）→ 合成 → 缓存区落盘。

    调用方已给 ``translations`` 时不做任何外部调用；未给时缺配置即
    ``TranslateNotConfigured``，不静默回落。
    ``db`` 不为 None 时开关与三件套走桥接（面板行优先、缺行回退 env）。
    """
    require_enabled(db)
    cues = parse_cues(source_text)
    if not cues:
        raise ValueError("原文中没有可解析的字幕 cue，拒绝生成空轨")
    if translations is None:
        translations = translate_texts(
            [c["text"] for c in cues], source_lang, target_lang,
            http_post=http_post, db=db)
    vtt = build_bilingual_vtt(source_text, translations, source_lang, target_lang)
    filename = write_bilingual_track(item_id, vtt, source_lang, target_lang,
                                     cache_dir=cache_dir)
    return {"filename": filename, "cue_count": len(cues),
            "source_lang": source_lang, "target_lang": target_lang}
