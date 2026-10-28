"""能力：智能字幕（翻译提供方配置）

只提供能力：提供方、端点、凭据一律由管理员自己填，项目不内置任何 key。
后台「系统设置 → 能力中心」按这里的 SPEC 渲染表单，前端不写死任何字段。

读写口径（与生成侧的桥接一致，见 ``backend/emby_server/smart_subtitles.py``）：

- 面板值（``system_configs`` 行）为准；面板没填时回退到同名环境变量；
- 面板永远不写生产 ``.env``；环境变量里的值在面板上只体现为生效态与
  密钥掩码，绝不回显明文；
- 面板清空某字段 = 删除面板值、回退环境变量（不会删除环境变量里的值），
  这一点在字段 hint 里写明，不让“清空”看起来像“彻底删除”。

卡片徽标口径（沿用 ai 能力的约定）：

- ``enabled`` = 开关开且三件套齐全（开关开了但翻译没配齐，生成仍会 503，
  徽标不能谎称已启用）；表单里的开关本身单独显示开关态。
- ``configured`` = 提供方、端点、凭据三者齐全（不看开关）。
- 没有 ``test()``：翻译测试会真实调外部提供方、花掉调用量，不属于
  “点一下验证连通性”的范畴，故不挂测试按钮（框架据此隐藏按钮）。
"""
from __future__ import annotations

import os

ENABLED_KEY = "SMART_SUBTITLES_ENABLED"
PROVIDER_KEY = "SMART_SUBTITLE_TRANSLATE_PROVIDER"
ENDPOINT_KEY = "SMART_SUBTITLE_TRANSLATE_ENDPOINT"
API_KEY_KEY = "SMART_SUBTITLE_TRANSLATE_API_KEY"

_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}

SPEC = {
    "title": "智能字幕",
    "desc": "智能字幕的翻译提供方配置：开关、提供方、端点与凭据；生成逻辑本身见字幕模块。",
    "group": "AI 与智能",
    "docs_hint": "面板值优先于同名环境变量；面板留空 = 回退环境变量（未配则为未配置）。",
    "fields": [
        {"key": ENABLED_KEY, "label": "启用智能字幕", "type": "bool",
         "hint": "关 = 全部字幕入口拒绝；开 = 按下面三件套走配置化翻译。面板没填时按环境变量显示。"},
        {"key": PROVIDER_KEY, "label": "翻译提供方", "type": "str",
         "hint": "留空 = 回退环境变量 SMART_SUBTITLE_TRANSLATE_PROVIDER。"},
        {"key": ENDPOINT_KEY, "label": "翻译端点", "type": "str",
         "hint": "留空 = 回退环境变量 SMART_SUBTITLE_TRANSLATE_ENDPOINT。"},
        {"key": API_KEY_KEY, "label": "翻译凭据（API Key）", "type": "secret",
         "hint": "只显示是否已配置；提交 ****** = 不修改；清空 = 回退环境变量（不会删除环境变量里的密钥）。"},
    ],
}


def _effective(values: dict) -> dict:
    """生效值：面板行优先，缺行/空值回退同名环境变量。

    开关归一化成 "true"/"false"（表单开关与框架的 =="true" 判定都认这个写法）；
    三件套只做去空白，不改内容。密钥原文只在这里出现、随后即被框架掩码。
    """
    out = dict(values or {})
    raw = (out.get(ENABLED_KEY) or "").strip().lower()
    if raw in _TRUTHY:
        out[ENABLED_KEY] = "true"
    elif raw in _FALSY:
        out[ENABLED_KEY] = "false"
    else:
        out[ENABLED_KEY] = ("true" if (os.getenv(ENABLED_KEY, "") or "").strip().lower() in _TRUTHY
                            else "false")
    for key in (PROVIDER_KEY, ENDPOINT_KEY, API_KEY_KEY):
        if not (out.get(key) or "").strip():
            out[key] = (os.getenv(key, "") or "").strip()
    return out


def effective_values(db, values: dict) -> dict:
    """能力框架钩子：表单回显用生效值（db 仅占位，与框架钩子签名一致）。

    没有这一步时，面板没填的字段会按 SPEC 缺省回显——生产 .env 里已开的开关
    在面板上会显示成关（睁眼说关）。合并后表单开关与三件套都显示真实生效态。
    """
    return _effective(values)


def is_configured(values: dict) -> bool:
    """三件套齐全才算配过（不看开关；只看生效值，面板或环境变量都算）。"""
    eff = _effective(values)
    return bool(eff.get(PROVIDER_KEY)) and bool(eff.get(ENDPOINT_KEY)) and bool(eff.get(API_KEY_KEY))


def is_enabled(values: dict) -> bool:
    """开关开且三件套齐全才算生效（沿用 ai 能力的口径）。"""
    eff = _effective(values)
    return eff.get(ENABLED_KEY) == "true" and is_configured(eff)
