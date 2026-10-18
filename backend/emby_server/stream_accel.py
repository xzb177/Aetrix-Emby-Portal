"""流媒体加速开关：管理后台一个开关 + 一个域名，点保存即生效。

极简设计（2026-10-07 用户拍板）：
- 管理后台「系统设置 → 运营参数」里一个开关「流媒体加速」+ 一个输入框「加速域名」
- 打开后自动做四件事（无需重启，域名守卫侧最长 30 秒生效）：
  1. 强制域名访问（直接 IP 一律 403）—— backend/domain_guard.py（读本模块的 DB 配置）
  2. 信任 CF 头（CF-Connecting-IP 还原真实 IP）
  3. 全部播放优化默认已开启（playback_tune：rclone 参数 / SA 轮换 / 并发限流 / 4K 降级）
  4. 播放 URL 统一用该域名生成（本模块 rewrite_url_domain）
- 不要脚本、不要命令、不要密码、不要云账号（用户全否了）。

配置键（SystemConfig）：
- ``stream_accel_enabled``：\"true\"/\"false\"（前端开关序列化格式）
- ``stream_accel_domain``：如 cdn.example.com（不带 scheme）
"""

from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

CONFIG_ENABLED = "stream_accel_enabled"
CONFIG_DOMAIN = "stream_accel_domain"

# 域名格式：字母数字点横杠，至少一个点，TLD 至少 2 字母；拒绝 scheme/路径/端口/IP
_DOMAIN_RE = re.compile(
    r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    r"(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*\.[A-Za-z]{2,}$"
)


def normalize_domain(raw: Optional[str]) -> str:
    """清洗用户输入：去 scheme/路径/端口/首尾空白，转小写。"""
    text = (raw or "").strip().lower()
    # 去掉 scheme
    if "://" in text:
        text = text.split("://", 1)[1]
    # 去掉路径和查询串
    text = text.split("/", 1)[0].split("?", 1)[0]
    # 去掉端口（[::1]:8080 这类 IPv6 字面量不做加速域名，直接留空让校验拒绝）
    if text.startswith("["):
        return ""
    if ":" in text:
        text = text.split(":", 1)[0]
    return text.strip().strip(".")


def validate_domain(raw: Optional[str]) -> str:
    """校验加速域名；合法返回清洗后的域名，非法抛 ValueError。"""
    domain = normalize_domain(raw)
    if not domain:
        raise ValueError("加速域名不能为空（例如 cdn.example.com）")
    if len(domain) > 253:
        raise ValueError("加速域名过长")
    if not _DOMAIN_RE.match(domain):
        raise ValueError(f"加速域名格式非法：{raw!r}（应为类似 cdn.example.com 的域名）")
    # 拒绝纯 IP：加速域名必须是域名（IP 直连走域名守卫没有意义）
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", domain):
        raise ValueError("加速域名不能是 IP 地址，请填写域名")
    if domain in ("localhost",):
        raise ValueError("加速域名不能是 localhost")
    return domain


def _is_true(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def get_config(db) -> dict:
    """读加速开关配置（管理后台展示用）。"""
    from backend.integrations import store

    vals = store.read_values(
        db, [CONFIG_ENABLED, CONFIG_DOMAIN],
        {CONFIG_ENABLED: "false", CONFIG_DOMAIN: ""},
    )
    return {
        "enabled": _is_true(vals[CONFIG_ENABLED]),
        "domain": normalize_domain(vals[CONFIG_DOMAIN]),
    }


def get_effective_domain(db) -> str:
    """开关打开时返回清洗后的域名，否则返回 \"\"。播放链路用。"""
    cfg = get_config(db)
    if not cfg["enabled"]:
        return ""
    try:
        return validate_domain(cfg["domain"])
    except ValueError:
        # DB 里存了非法值（老数据/手动改库）：宁可不用，也不生成坏 URL
        logger.warning("流媒体加速域名非法，已忽略：%r", cfg["domain"])
        return ""


def rewrite_url_domain(url: str, base: str, domain: str) -> str:
    """把以 base 开头的播放 URL 基址换成 https://{domain}。

    对不上 base 的 URL（远端流节点 / CDN 改写过的）原样返回，绝不造坏 URL。
    """
    if not url or not base or not domain:
        return url
    base = base.rstrip("/")
    if not url.startswith(base):
        return url
    new_base = f"https://{domain}"
    if base == new_base:
        return url
    return new_base + url[len(base):]
def rewrite_playback_urls_unified(
    db, stream_url: str, base: str
) -> tuple[str, str | None]:
    """统一播放 URL 改写（横切能力只许一套）。

    优先级：流节点 > 加速域名 > 原样。
    - 有健康远端流节点时改写到节点（复用 stream_nodes.pick_stream_node）
    - 否则加速开关打开时改写到加速域名
    - 都没命中时原样返回

    转码已删除（2026-10-17）：不再处理 TranscodingUrl。
    返回 ``(stream_url, source)``，source 为 'node' / 'accel' / None。
    """
    # 1. 流节点（需要时懒导入，避免循环导入）
    try:
        from backend.emby_server import stream_nodes

        node_hit = stream_nodes.rewrite_playback_urls(
            db, stream_url, base
        )
        if node_hit[1]:
            return node_hit[0], "node"
    except Exception:
        logger.debug("流节点改写跳过", exc_info=True)

    # 2. 加速域名
    domain = get_effective_domain(db)
    if domain:
        new_stream = rewrite_url_domain(stream_url, base, domain)
        if new_stream != stream_url:
            return new_stream, "accel"

    # 3. 原样
    return stream_url, None
